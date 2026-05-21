from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

from playwright.async_api import Locator, Page

from teams_interaction import selectors as sel
from teams_interaction.types import ChannelMessage

log = logging.getLogger(__name__)


async def _visible(loc: Locator, timeout_ms: float = 500) -> bool:
    try:
        await loc.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def goto_channel(page: Page, url: str) -> None:
    log.info("goto_channel: navigating to %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    log.debug("goto_channel: page loaded (url=%s)", page.url)


def _norm_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _looks_like_transcript_blob(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return len(text) > 1500 or len(lines) > 12


def _clean_message_text(text: str, author: str | None) -> str:
    """Remove Teams DOM artifacts where inner_text includes a truncated preview + accessibility
    text + full message, eg: ``{truncated}... by {Author} {Author} {real text}``.
    """
    if not text:
        return text

    if author:
        escaped = re.escape(author)
        # "…truncated… by Author Author real text"
        m = re.search(
            r"\.{3}\s+by\s+" + escaped + r"\s+" + escaped + r"\s+([\s\S]+)",
            text,
        )
        if m:
            return m.group(1).strip()

        # "…truncated… by Author\n real text" (author not repeated)
        m = re.search(r"\.{3}\s+by\s+" + escaped + r"[\s,]+([\s\S]+)", text)
        if m:
            candidate = m.group(1).strip()
            # strip leading author name if it snuck in
            if candidate.startswith(author):
                candidate = candidate[len(author):].lstrip()
            return candidate

    return text


async def active_channel_name(page: Page) -> str | None:
    for s in sel.ACTIVE_CHANNEL_TITLE:
        nodes = page.locator(s)
        try:
            n = await nodes.count()
        except Exception:
            continue
        for i in range(min(n, 4)):
            node = nodes.nth(i)
            if not await _visible(node, timeout_ms=180):
                continue
            try:
                text = (await node.inner_text()).strip()
            except Exception:
                continue
            if text:
                return text
    return await _active_channel_nav_text(page)


async def switch_to_channel(page: Page, channel_name: str, timeout_ms: float = 15000) -> None:
    target = _norm_text(channel_name)
    if not target:
        raise ValueError("channel_name must not be empty")

    log.info("switch_to_channel: looking for %r", channel_name)
    if await _is_channel_active(page, target):
        log.info("switch_to_channel: %r already active", channel_name)
        return

    item = await _find_channel_nav_item(page, channel_name)
    if item is None:
        raise RuntimeError(
            f"Could not find channel/chat '{channel_name}' in the Teams navigation pane"
        )

    log.info("switch_to_channel: found nav item for %r, clicking", channel_name)
    await item.scroll_into_view_if_needed()
    await item.click()
    await _wait_for_channel_active(page, target, item, timeout_ms=timeout_ms)
    actual = await active_channel_name(page)
    log.info("switch_to_channel: %r is now active (heading=%r)", channel_name, actual)


async def _wait_for_channel_active(
    page: Page,
    channel_name_norm: str,
    clicked_item: Locator | None = None,
    timeout_ms: float = 15000,
) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if await _is_channel_active(page, channel_name_norm):
            return
        # Fallback: if the nav item we clicked is now aria-selected, trust the navigation.
        if clicked_item is not None:
            try:
                sel_val = await clicked_item.get_attribute("aria-selected")
                if sel_val == "true":
                    log.debug("_wait_for_channel_active: nav item is aria-selected=true, accepting")
                    return
            except Exception:
                pass
        await page.wait_for_timeout(175)
    current = await active_channel_name(page)
    raise RuntimeError(
        f"Timed out waiting for channel '{channel_name_norm}' to become active"
        + (f" (current heading: {current!r})" if current else "")
    )


async def _find_channel_nav_item(page: Page, channel_name: str) -> Locator | None:
    """
    Find the nav item for *channel_name*.

    Scoring (higher = better match):
      3 – entire normalised candidate text equals target
      2 – first non-empty line of candidate text equals target
      1 – normalised candidate text starts with target
      0 – target is a substring of normalised candidate text (last resort)

    This avoids matching group-chat items like "Josef Ondrej, Santiago …"
    when looking for "Josef Ondrej".
    """
    target = _norm_text(channel_name)
    wants_you_suffix = "(you)" in channel_name.casefold()

    # Collect (score, locator, display_text) across all nav selectors.
    best_score: int = -1
    best_loc: Locator | None = None
    best_text: str = ""

    for s in sel.CHANNEL_NAV_ITEM:
        matches = page.locator(s)
        try:
            n = await matches.count()
        except Exception:
            continue
        log.debug("_find_channel_nav_item: selector=%r found %d nodes", s, n)
        for i in range(min(n, 250)):
            candidate = matches.nth(i)
            if not await _visible(candidate, timeout_ms=120):
                continue

            # Prefer user-facing text for matching; treat data-tid only as metadata.
            try:
                display_text = (await candidate.inner_text()).strip()
            except Exception:
                display_text = ""
            try:
                aria_label = (await candidate.get_attribute("aria-label") or "").strip()
            except Exception:
                aria_label = ""
            try:
                title_text = (await candidate.get_attribute("title") or "").strip()
            except Exception:
                title_text = ""
            try:
                data_tid = (await candidate.get_attribute("data-tid") or "").strip()
            except Exception:
                data_tid = ""

            name_blob = "\n".join(v for v in (display_text, aria_label, title_text) if v)
            raw = name_blob or await _candidate_text(candidate)
            if not raw:
                continue

            lowered_meta = "\n".join((aria_label, title_text, data_tid)).casefold()
            lowered_display = display_text.casefold()
            # Ignore elements that are clearly profile/avatar affordances.
            if any(tag in lowered_meta for tag in ("avatar", "profile", "contact-card", "contact card")):
                log.debug("  candidate ignored (profile/avatar meta) sel=%r i=%d meta=%r", s, i, lowered_meta[:80])
                continue
            # By default avoid matching self-profile entries unless explicitly requested.
            if "(you)" in lowered_display and not wants_you_suffix:
                log.debug("  candidate ignored (self entry) sel=%r i=%d text=%r", s, i, display_text[:80])
                continue
            lowered_raw = raw.casefold()
            if "(you)" in lowered_raw and not wants_you_suffix:
                log.debug("  candidate ignored (self raw) sel=%r i=%d raw=%r", s, i, raw[:80])
                continue

            norm = _norm_text(raw)
            first_line = _norm_text(raw.splitlines()[0]) if raw.strip() else ""

            if norm == target:
                score = 3
            elif first_line == target:
                score = 2
            elif norm.startswith(target + " ") or norm.startswith(target + ","):
                score = 1
            elif target in norm:
                score = 0
            else:
                continue

            log.debug("  candidate score=%d sel=%r i=%d text=%r", score, s, i, raw[:100])
            if score > best_score:
                best_score = score
                best_loc = candidate
                best_text = raw
                if best_score == 3:
                    break   # can't do better than exact
        if best_score == 3:
            break

    if best_loc is None:
        log.warning("_find_channel_nav_item: no match found for %r in any nav selector", channel_name)
    else:
        log.info(
            "_find_channel_nav_item: best match score=%d text=%r for %r",
            best_score, best_text[:80], channel_name,
        )
    return best_loc


async def _candidate_text(node: Locator) -> str:
    values: list[str] = []
    try:
        values.append(await node.inner_text())
    except Exception:
        pass
    for attr in ("aria-label", "title", "data-tid"):
        try:
            v = await node.get_attribute(attr)
        except Exception:
            v = None
        if v:
            values.append(v)
    return "\n".join(v.strip() for v in values if v and v.strip())


async def _active_channel_nav_text(page: Page) -> str | None:
    attrs = ["aria-selected='true'", "aria-current='page'", "data-tid*='active' i"]
    for s in sel.CHANNEL_NAV_ITEM:
        for attr in attrs:
            loc = page.locator(f"{s}[{attr}]")
            try:
                n = await loc.count()
            except Exception:
                continue
            for i in range(min(n, 5)):
                item = loc.nth(i)
                if not await _visible(item, timeout_ms=120):
                    continue
                try:
                    text = (await item.inner_text()).strip()
                except Exception:
                    continue
                if text:
                    return text
    return None


async def _is_channel_active(page: Page, channel_name_norm: str) -> bool:
    active_title = await active_channel_name(page)
    if active_title and channel_name_norm in _norm_text(active_title):
        return True
    nav_text = await _active_channel_nav_text(page)
    if nav_text and channel_name_norm in _norm_text(nav_text):
        return True
    return False


async def scrape_top_level_messages(page: Page, max_items: int = 80) -> list[ChannelMessage]:
    """Best-effort: collect top-level posts currently in the virtualized viewport + nearby."""
    region_sel = None
    for s in sel.MESSAGE_LIST_REGION:
        loc = page.locator(s).first
        if await _visible(loc, timeout_ms=800):
            region_sel = s
            log.debug("scrape: message list region matched by %r", s)
            break
    if not region_sel:
        log.warning("scrape: no message list region found via any selector – falling back to body")
        region_sel = "body"

    region = page.locator(region_sel)
    messages: list[ChannelMessage] = []

    for item_sel in sel.MESSAGE_ITEM:
        items = region.locator(item_sel)
        try:
            n = await items.count()
        except Exception:
            continue
        log.debug("scrape: item selector=%r count=%d", item_sel, n)
        if n == 0:
            continue
        for i in range(min(n, max_items)):
            item = items.nth(i)
            if not await _visible(item, timeout_ms=200):
                continue
            msg = await _parse_message_item(item, item_sel, i)
            if msg and msg.text.strip():
                messages.append(msg)
        if messages:
            log.debug("scrape: selector=%r yielded %d messages", item_sel, len(messages))
            break
        log.debug("scrape: selector=%r yielded 0 parseable messages", item_sel)

    if not messages:
        log.warning("scrape: item selectors found no parseable messages, trying global body fallback")
        messages = await _scrape_messages_from_body_nodes(page, max_items=max_items)

    # Dedupe by stable_id while preserving order
    seen: set[str] = set()
    out: list[ChannelMessage] = []
    for m in messages:
        if m.stable_id in seen:
            continue
        seen.add(m.stable_id)
        out.append(m)

    log.info("scrape: returning %d unique messages (region=%r)", len(out), region_sel)
    return out


async def inspect_message_dom(page: Page, max_samples: int = 5) -> dict[str, Any]:
    """Collect a compact diagnostic snapshot of message-related DOM state."""
    result: dict[str, Any] = {
        "url": page.url,
        "active_channel": await active_channel_name(page),
        "message_regions": [],
        "message_items": [],
        "body_nodes": [],
        "scraped_messages": [],
    }

    for selector in sel.MESSAGE_LIST_REGION:
        loc = page.locator(selector)
        try:
            count = await loc.count()
        except Exception:
            count = -1
        visible = False
        sample = None
        if count > 0:
            first = loc.first
            visible = await _visible(first, timeout_ms=150)
            if visible:
                try:
                    sample = (await first.inner_text()).strip()[:200]
                except Exception:
                    sample = None
        result["message_regions"].append({"selector": selector, "count": count, "visible": visible, "sample": sample})

    for selector in sel.MESSAGE_ITEM:
        loc = page.locator(selector)
        try:
            count = await loc.count()
        except Exception:
            count = -1
        samples: list[str] = []
        if count > 0:
            for i in range(min(count, max_samples)):
                node = loc.nth(i)
                if not await _visible(node, timeout_ms=100):
                    continue
                try:
                    txt = (await node.inner_text()).strip()
                except Exception:
                    continue
                if txt:
                    samples.append(txt[:200])
        result["message_items"].append({"selector": selector, "count": count, "samples": samples})

    for selector in sel.BODY:
        loc = page.locator(selector)
        try:
            count = await loc.count()
        except Exception:
            count = -1
        samples: list[str] = []
        if count > 0:
            for i in range(min(count, max_samples)):
                node = loc.nth(i)
                if not await _visible(node, timeout_ms=100):
                    continue
                try:
                    txt = (await node.inner_text()).strip()
                except Exception:
                    continue
                if txt:
                    samples.append(txt[:200])
        result["body_nodes"].append({"selector": selector, "count": count, "samples": samples})

    for msg in (await scrape_top_level_messages(page, max_items=max_samples))[:max_samples]:
        result["scraped_messages"].append(
            {"stable_id": msg.stable_id, "author": msg.author, "text": msg.text[:300], "raw": msg.raw}
        )
    return result


async def _scrape_messages_from_body_nodes(page: Page, max_items: int) -> list[ChannelMessage]:
    """Fallback pass: scrape directly from message body nodes across the page."""
    out: list[ChannelMessage] = []
    seen_text: set[str] = set()

    for body_sel in sel.BODY:
        nodes = page.locator(body_sel)
        try:
            n = await nodes.count()
        except Exception:
            continue
        if n == 0:
            continue
        log.debug("fallback: body selector=%r count=%d", body_sel, n)
        for i in range(min(n, max_items * 2)):
            if len(out) >= max_items:
                return out
            node = nodes.nth(i)
            if not await _visible(node, timeout_ms=120):
                continue
            try:
                raw = (await node.inner_text()).strip()
            except Exception:
                continue
            if not raw or len(raw) < 6:
                continue
            if _looks_like_transcript_blob(raw):
                log.debug("fallback: skipping transcript-like body node selector=%r index=%d", body_sel, i)
                continue
            norm = _norm_text(raw)
            if norm in seen_text:
                continue
            seen_text.add(norm)

            # Best effort: grab data-mid from nearest ancestor-ish wrapper.
            stable_prefix = "hash"
            stable_value = ""
            try:
                wrapper = node.locator("xpath=ancestor-or-self::*[@data-mid][1]").first
                mid = await wrapper.get_attribute("data-mid")
                if mid:
                    stable_prefix = "mid"
                    stable_value = mid
            except Exception:
                pass
            if not stable_value:
                stable_value = hashlib.sha256(f"{body_sel}|{i}|{raw[:500]}".encode("utf-8", errors="ignore")).hexdigest()[:24]

            out.append(
                ChannelMessage(
                    stable_id=f"{stable_prefix}:{stable_value}",
                    text=raw,
                    author=None,
                    raw={"fallback": True, "body_selector": body_sel, "index": i},
                )
            )
    log.info("fallback: extracted %d message body node(s)", len(out))
    return out


async def _parse_message_item(item: Locator, item_sel: str, index: int) -> ChannelMessage | None:
    text_parts: list[str] = []
    author: str | None = None

    for a in sel.AUTHOR:
        al = item.locator(a).first
        if await _visible(al, timeout_ms=150):
            try:
                t = (await al.inner_text()).strip()
                if t:
                    author = t
                    break
            except Exception:
                pass

    # Fallback: item-level aria-label sometimes encodes author
    if not author:
        try:
            lbl = await item.get_attribute("aria-label")
            if lbl:
                part = lbl.split(",")[0].strip()
                if part:
                    author = part
        except Exception:
            pass

    for b in sel.BODY:
        bl = item.locator(b)
        try:
            cnt = await bl.count()
        except Exception:
            continue
        for j in range(min(cnt, 3)):
            cell = bl.nth(j)
            if await _visible(cell, timeout_ms=100):
                try:
                    raw = (await cell.inner_text()).strip()
                    if raw:
                        text_parts.append(raw)
                except Exception:
                    pass
        if text_parts:
            break

    if len(text_parts) > 1 and not author:
        log.debug("_parse_message_item: index=%d looks like multi-message container, deferring to body fallback", index)
        return None

    if not text_parts:
        try:
            fallback = (await item.inner_text()).strip()
            # Skip huge blobs (collapsed thread chrome)
            if fallback and len(fallback) < 8000:
                lines = [ln.strip() for ln in fallback.splitlines() if ln.strip()]
                if lines:
                    blob = "\n".join(lines[:40])
                    if not (not author and (len(lines) > 1 or _looks_like_transcript_blob(blob))):
                        text_parts = [blob]
        except Exception:
            pass

    text = "\n".join(text_parts).strip()
    if not text:
        log.debug("_parse_message_item: index=%d produced empty text, skipping", index)
        return None

    text = _clean_message_text(text, author)
    if not text:
        log.debug("_parse_message_item: index=%d empty after cleaning, skipping", index)
        return None

    stable = await _stable_id_for_item(item, item_sel, index, text, author)
    log.debug("_parse_message_item: index=%d author=%r id=%s text=%r", index, author, stable, text[:80])
    return ChannelMessage(
        stable_id=stable,
        text=text,
        author=author,
        raw={"item_selector": item_sel, "index": index},
    )


async def _stable_id_for_item(item: Locator, item_sel: str, index: int, text: str, author: str | None) -> str:
    # Check data-mid on the item itself first, then on any nested element.
    for locator in (item, item.locator("[data-mid]").first):
        try:
            mid = await locator.get_attribute("data-mid")
            if mid:
                return f"mid:{mid}"
        except Exception:
            pass
    h = hashlib.sha256(f"{item_sel}|{index}|{author or ''}|{text[:500]}".encode("utf-8", errors="ignore")).hexdigest()[
        :24
    ]
    return f"hash:{h}"


async def send_plain_text(page: Page, text: str) -> None:
    box: Locator | None = None
    for s in sel.COMPOSE:
        loc = page.locator(s).first
        if await _visible(loc, timeout_ms=600):
            box = loc
            break
    if box is None:
        raise RuntimeError("Could not find compose textbox; UI may have changed.")

    await box.click()
    await box.fill("")
    await page.keyboard.insert_text(text)

    clicked = False
    for s in sel.SEND_BUTTON:
        btn = page.locator(s).first
        if await _visible(btn, timeout_ms=400):
            await btn.click()
            clicked = True
            break
    if not clicked:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(500)


def normalize_teams_url(url: str) -> str:
    u = url.strip()
    if "teams.microsoft.com" not in u:
        raise ValueError("channel url must be a teams.microsoft.com link")
    if not u.startswith("http"):
        raise ValueError("channel url must start with https://")
    return u
