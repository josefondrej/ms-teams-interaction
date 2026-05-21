"""DOM interaction helpers for Microsoft Teams (browser-based, no Azure registration).

Provides async functions built on top of Playwright for navigating channels,
scraping messages, sending plain-text posts, and switching between channels via
the Teams left-rail navigation.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

# ---------------------------------------------------------------------------
# Teams date-divider detection
# ---------------------------------------------------------------------------

_DATE_SEP_RE = re.compile(
    r"^(?:"
    r"today|yesterday|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|wed|thu|fri|sat|sun|"
    r"\d{1,2}\s+\w+\s+\d{4}|"  # "21 May 2026"
    r"\w+\s+\d{1,2},?\s+\d{4}|"  # "May 21, 2026"
    r"\d{1,2}/\d{1,2}/\d{2,4}|"  # "5/21/26"
    r"\d{4}-\d{2}-\d{2}"  # "2026-05-21"
    r")$",
    re.IGNORECASE,
)


def _is_date_separator(text: str, author: str | None) -> bool:
    """Return True when *text* looks like a Teams date-divider row (e.g. 'Today').

    These DOM elements have no author and contain only a short date string; they
    are not real chat messages and should be dropped from the scrape output.
    """
    if author:
        return False
    stripped = text.strip()
    if "\n" in stripped or len(stripped) > 40:
        return False
    return bool(_DATE_SEP_RE.match(stripped))


from playwright.async_api import Locator, Page

from teams_interaction import selectors as sel
from teams_interaction.types import ChannelMessage

log = logging.getLogger(__name__)


async def _visible(loc: Locator, timeout_ms: float = 500) -> bool:
    """Return ``True`` if *loc* becomes visible within *timeout_ms* milliseconds.

    Args:
        loc: The Playwright :class:`~playwright.async_api.Locator` to probe.
        timeout_ms: Maximum wait time in milliseconds before giving up.

    Returns:
        ``True`` when the element is visible; ``False`` on timeout or any
        other error.
    """
    try:
        await loc.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def goto_channel(page: Page, url: str, timeout_ms: float = 30_000) -> None:
    """Navigate *page* to *url* and wait for the DOM to load.

    Args:
        page: The active Playwright page.
        url: The Teams deep-link (or any URL) to navigate to.
        timeout_ms: Maximum wait time in milliseconds for the navigation.
    """
    log.info("goto_channel: navigating to %s (timeout=%dms)", url, timeout_ms)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    log.debug("goto_channel: page loaded (url=%s)", page.url)


def _norm_text(value: str) -> str:
    """Normalise *value* for case-insensitive, whitespace-collapsed comparison.

    Args:
        value: Raw string to normalise.

    Returns:
        Lower-cased string with consecutive whitespace collapsed to a single
        space.
    """
    return " ".join(value.casefold().split())


def _looks_like_transcript_blob(value: str) -> bool:
    """Return ``True`` when *value* resembles a multi-line meeting transcript.

    Heuristic: more than 1 500 characters **or** more than 12 non-empty lines.

    Args:
        value: The candidate text to inspect.

    Returns:
        ``True`` if the text looks like a collapsed transcript blob that should
        be skipped during scraping.
    """
    text = value.strip()
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return len(text) > 1500 or len(lines) > 12


def _clean_message_text(text: str, author: str | None) -> str:
    """Remove Teams DOM artifacts where inner_text includes a truncated preview + accessibility
    text + full message, eg: ``{truncated}... by {Author} {Author} {real text}``.

    Args:
        text: Raw inner-text extracted from a message DOM node.
        author: Display name of the message author, or ``None``.

    Returns:
        Cleaned message text with accessibility noise removed.
    """
    if not text:
        return text

    if author:
        escaped = re.escape(author)
        # "…truncated… by Author Author real text"
        match = re.search(
            r"\.{3}\s+by\s+" + escaped + r"\s+" + escaped + r"\s+([\s\S]+)",
            text,
        )
        if match:
            return match.group(1).strip()

        # "…truncated… by Author\n real text" (author not repeated)
        match = re.search(r"\.{3}\s+by\s+" + escaped + r"[\s,]+([\s\S]+)", text)
        if match:
            candidate = match.group(1).strip()
            # strip leading author name if it snuck in
            if candidate.startswith(author):
                candidate = candidate[len(author) :].lstrip()
            return candidate

        # "real text by Author" – accessibility suffix on short messages
        text = re.sub(r"\s+by\s+" + escaped + r"\s*$", "", text).strip()

    return text


async def active_channel_name(page: Page) -> str | None:
    """Return the visible heading/title of the currently active Teams channel.

    Tries all :data:`~teams_interaction.selectors.ACTIVE_CHANNEL_TITLE`
    selectors in order, then falls back to inspecting the nav rail for a
    selected item.

    Args:
        page: The Playwright page showing the Teams web client.

    Returns:
        The channel title string, or ``None`` if it could not be determined.
    """
    for selector in sel.ACTIVE_CHANNEL_TITLE:
        nodes = page.locator(selector)
        try:
            node_count = await nodes.count()
        except Exception:
            continue
        for index in range(min(node_count, 4)):
            node = nodes.nth(index)
            if not await _visible(node, timeout_ms=180):
                continue
            try:
                text = (await node.inner_text()).strip()
            except Exception:
                continue
            if text:
                return text
    return await _active_channel_nav_text(page)


async def _wait_for_nav_ready(
    page: Page,
    min_items: int = 1,
    timeout_ms: float = 10_000,
    poll_ms: float = 250,
) -> None:
    """Wait until the Teams navigation pane has rendered enough channel/chat entries.

    Polls all CHANNEL_NAV_ITEM selectors every *poll_ms* milliseconds until the
    total number of visible items reaches *min_items*, or *timeout_ms* elapses.

    Args:
        page: The Playwright page showing the Teams web client.
        min_items: Minimum number of visible nav items required before
            returning.
        timeout_ms: Maximum wait time in milliseconds.
        poll_ms: Polling interval in milliseconds.

    Raises:
        TimeoutError: if the nav pane does not become ready within *timeout_ms*.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        count = 0
        for selector in sel.CHANNEL_NAV_ITEM:
            try:
                nodes = page.locator(selector)
                node_count = await nodes.count()
            except Exception:
                continue
            for index in range(min(node_count, 20)):
                if await _visible(nodes.nth(index), timeout_ms=80):
                    count += 1
                    if count >= min_items:
                        log.debug("_wait_for_nav_ready: nav ready (%d visible items)", count)
                        return
        await page.wait_for_timeout(poll_ms)

    raise TimeoutError(f"Teams navigation pane did not show {min_items}+ items within {timeout_ms:.0f} ms")


async def _wait_for_loading_screen_gone(page: Page, timeout_ms: float = 30_000) -> None:
    """Wait until the Teams full-page loading overlay (#loading-screen) is gone.

    The loading screen intercepts pointer events and must be dismissed before
    any click can land.  Silently returns if it is already absent.

    Args:
        page: The Playwright page showing the Teams web client.
        timeout_ms: Maximum wait time in milliseconds.
    """
    try:
        overlay = page.locator("#loading-screen")
        # If it's not even present, this resolves immediately.
        await overlay.wait_for(state="hidden", timeout=timeout_ms)
        log.debug("_wait_for_loading_screen_gone: loading overlay gone")
    except Exception:
        # Either it was never there or timed out – either way, proceed.
        pass


async def switch_to_channel(page: Page, channel_name: str, timeout_ms: float = 30_000) -> None:
    """Click the nav-rail entry for *channel_name* and wait for it to become active.

    If the requested channel is already active the function returns immediately.

    Args:
        page: The Playwright page showing the Teams web client.
        channel_name: Visible display name of the target channel or chat.
        timeout_ms: Maximum wait time (ms) for the channel to become active
            after clicking.

    Raises:
        ValueError: If *channel_name* is empty.
        RuntimeError: If the channel cannot be found in the navigation pane or
            does not become active within *timeout_ms*.
    """
    target = _norm_text(channel_name)
    if not target:
        raise ValueError("channel_name must not be empty")

    log.info("switch_to_channel: looking for %r", channel_name)
    if await _is_channel_active(page, target):
        log.info("switch_to_channel: %r already active", channel_name)
        return

    await _wait_for_nav_ready(page, timeout_ms=timeout_ms)

    # Retry finding the nav item until the full timeout expires – the nav rail
    # may still be loading contacts/chats even after the minimum item count is reached.
    deadline = time.monotonic() + timeout_ms / 1000.0
    retry_interval_ms = 1_000
    item = None
    while True:
        item = await _find_channel_nav_item(page, channel_name)
        if item is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        log.info(
            "switch_to_channel: %r not found yet, retrying (%.0fs remaining)",
            channel_name,
            remaining,
        )
        await page.wait_for_timeout(min(retry_interval_ms, remaining * 1000))

    if item is None:
        raise RuntimeError(f"Could not find channel/chat '{channel_name}' in the Teams navigation pane")

    log.info("switch_to_channel: found nav item for %r, clicking", channel_name)
    remaining_ms = max(0.0, (deadline - time.monotonic()) * 1000)
    await _wait_for_loading_screen_gone(page, timeout_ms=remaining_ms)
    await item.scroll_into_view_if_needed()

    # Retry the click until the loading overlay is gone and the click lands.
    while True:
        remaining_ms = max(1.0, (deadline - time.monotonic()) * 1000)
        try:
            await item.click(timeout=remaining_ms)
            break
        except Exception as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Timed out waiting to click nav item for '{channel_name}': {exc}") from exc
            log.info(
                "switch_to_channel: click blocked (loading screen?), retrying (%.0fs remaining): %s",
                remaining,
                exc,
            )
            await _wait_for_loading_screen_gone(page, timeout_ms=min(5_000, remaining * 1000))

    remaining_ms = max(1.0, (deadline - time.monotonic()) * 1000)
    await _wait_for_channel_active(page, target, item, timeout_ms=remaining_ms)
    actual = await active_channel_name(page)
    log.info("switch_to_channel: %r is now active (heading=%r)", channel_name, actual)


async def _wait_for_channel_active(
    page: Page,
    channel_name_norm: str,
    clicked_item: Locator | None = None,
    timeout_ms: float = 15000,
) -> None:
    """Poll until the channel heading matches *channel_name_norm* or the item is selected.

    Args:
        page: The Playwright page showing the Teams web client.
        channel_name_norm: Normalised (lower-case, collapsed whitespace) channel
            name returned by :func:`_norm_text`.
        clicked_item: The nav locator that was clicked; used as a fallback
            ``aria-selected`` check.
        timeout_ms: Maximum wait time in milliseconds.

    Raises:
        RuntimeError: If the channel does not become active within *timeout_ms*.
    """
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

    Args:
        page: The Playwright page showing the Teams web client.
        channel_name: Visible display name of the target channel or chat.

    Returns:
        The best-matching :class:`~playwright.async_api.Locator`, or ``None``
        if no match was found.
    """
    target = _norm_text(channel_name)
    wants_you_suffix = "(you)" in channel_name.casefold()

    # Collect (score, locator, display_text) across all nav selectors.
    best_score: int = -1
    best_loc: Locator | None = None
    best_text: str = ""

    for selector in sel.CHANNEL_NAV_ITEM:
        matches = page.locator(selector)
        try:
            node_count = await matches.count()
        except Exception:
            continue
        log.debug("_find_channel_nav_item: selector=%r found %d nodes", selector, node_count)
        for index in range(min(node_count, 250)):
            candidate = matches.nth(index)
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

            name_blob = "\n".join(value for value in (display_text, aria_label, title_text) if value)
            raw = name_blob or await _candidate_text(candidate)
            if not raw:
                continue

            lowered_meta = "\n".join((aria_label, title_text, data_tid)).casefold()
            lowered_display = display_text.casefold()
            # Ignore elements that are clearly profile/avatar affordances.
            if any(tag in lowered_meta for tag in ("avatar", "profile", "contact-card", "contact card")):
                log.debug(
                    "  candidate ignored (profile/avatar meta) sel=%r i=%d meta=%r", selector, index, lowered_meta[:80]
                )
                continue
            # By default avoid matching self-profile entries unless explicitly requested.
            if "(you)" in lowered_display and not wants_you_suffix:
                log.debug("  candidate ignored (self entry) sel=%r i=%d text=%r", selector, index, display_text[:80])
                continue
            lowered_raw = raw.casefold()
            if "(you)" in lowered_raw and not wants_you_suffix:
                log.debug("  candidate ignored (self raw) sel=%r i=%d raw=%r", selector, index, raw[:80])
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

            log.debug("  candidate score=%d sel=%r i=%d text=%r", score, selector, index, raw[:100])
            if score > best_score:
                best_score = score
                best_loc = candidate
                best_text = raw
                if best_score == 3:
                    break  # can't do better than exact
        if best_score == 3:
            break

    if best_loc is None:
        log.warning("_find_channel_nav_item: no match found for %r in any nav selector", channel_name)
    else:
        log.info(
            "_find_channel_nav_item: best match score=%d text=%r for %r",
            best_score,
            best_text[:80],
            channel_name,
        )
    return best_loc


async def _candidate_text(node: Locator) -> str:
    """Extract all textual representations from a nav candidate node.

    Combines ``inner_text``, ``aria-label``, ``title``, and ``data-tid``
    attributes into a single newline-separated string.

    Args:
        node: A Playwright :class:`~playwright.async_api.Locator` for the
            candidate element.

    Returns:
        Newline-joined string of all non-empty text values found on the node.
    """
    values: list[str] = []
    try:
        values.append(await node.inner_text())
    except Exception:
        pass
    for attr in ("aria-label", "title", "data-tid"):
        try:
            attr_value = await node.get_attribute(attr)
        except Exception:
            attr_value = None
        if attr_value:
            values.append(attr_value)
    return "\n".join(value.strip() for value in values if value and value.strip())


async def _active_channel_nav_text(page: Page) -> str | None:
    """Return the inner text of the currently selected nav-rail channel entry.

    Looks for nav items carrying ``aria-selected='true'``,
    ``aria-current='page'``, or ``data-tid*='active'``.

    Args:
        page: The Playwright page showing the Teams web client.

    Returns:
        The visible text of the selected nav entry, or ``None`` if not found.
    """
    attrs = ["aria-selected='true'", "aria-current='page'", "data-tid*='active' i"]
    for selector in sel.CHANNEL_NAV_ITEM:
        for attr in attrs:
            loc = page.locator(f"{selector}[{attr}]")
            try:
                node_count = await loc.count()
            except Exception:
                continue
            for index in range(min(node_count, 5)):
                item = loc.nth(index)
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
    """Return ``True`` when the active channel heading or selected nav item matches *channel_name_norm*.

    Args:
        page: The Playwright page showing the Teams web client.
        channel_name_norm: Normalised channel name (output of :func:`_norm_text`).

    Returns:
        ``True`` if the channel is currently active.
    """
    active_title = await active_channel_name(page)
    if active_title and channel_name_norm in _norm_text(active_title):
        return True
    nav_text = await _active_channel_nav_text(page)
    if nav_text and channel_name_norm in _norm_text(nav_text):
        return True
    return False


async def _scrape_messages_js(page: Page, max_items: int = 80) -> list[ChannelMessage] | None:
    """
    Fast path: run all DOM scraping in a single JS evaluation (one browser round-trip).

    Returns a list of ChannelMessage objects on success, or None if JS scraping found nothing
    (so the caller can fall back to the slow Playwright-per-element path).

    Args:
        page: The Playwright page showing the Teams web client.
        max_items: Maximum number of message items to extract.

    Returns:
        A list of :class:`~teams_interaction.types.ChannelMessage` objects, or
        ``None`` when the JS pass found no messages.
    """
    result = await page.evaluate(
        """
        ({regionSelectors, itemSelectors, authorSelectors, bodySelectors, maxItems}) => {
            // Find the message list container
            let region = document.body;
            let regionSel = 'body';
            for (const s of regionSelectors) {
                const el = document.querySelector(s);
                if (el && el.offsetParent !== null) { // visible check
                    region = el;
                    regionSel = s;
                    break;
                }
            }

            // Try each MESSAGE_ITEM selector inside the region
            for (const itemSel of itemSelectors) {
                const items = Array.from(region.querySelectorAll(itemSel));
                if (items.length === 0) continue;

                const messages = [];
                for (const item of items.slice(0, maxItems)) {
                    // Skip non-visible items
                    const rect = item.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) continue;

                    // Extract data-mid
                    let mid = item.getAttribute('data-mid') || null;
                    if (!mid) {
                        const midEl = item.querySelector('[data-mid]');
                        if (midEl) mid = midEl.getAttribute('data-mid');
                    }

                    // Extract author
                    let author = null;
                    for (const asel of authorSelectors) {
                        const el = item.querySelector(asel);
                        if (el) {
                            const t = (el.innerText || '').trim();
                            if (t) { author = t; break; }
                        }
                    }
                    // Fallback: aria-label first segment
                    if (!author) {
                        const lbl = item.getAttribute('aria-label');
                        if (lbl) {
                            const part = lbl.split(',')[0].trim();
                            if (part) author = part;
                        }
                    }

                    // Extract body text — collect ALL matching body nodes first
                    const bodyNodes = [];
                    for (const bsel of bodySelectors) {
                        const els = Array.from(item.querySelectorAll(bsel));
                        if (els.length > 0) {
                            for (const el of els) {
                                const t = (el.innerText || '').trim();
                                if (t) bodyNodes.push({ el, text: t });
                            }
                            break; // stop at first selector that yields results
                        }
                    }

                    // Multi-body item with no author → emit each body node as its own message
                    // (mirrors the slow-path fall-through to _scrape_messages_from_body_nodes)
                    if (bodyNodes.length > 1 && !author) {
                        for (let bi = 0; bi < bodyNodes.length; bi++) {
                            const t = bodyNodes[bi].text;
                            if (!t) continue;
                            // Attempt to find a data-mid on the body node or closest ancestor
                            let bmid = bodyNodes[bi].el.getAttribute('data-mid') || null;
                            if (!bmid) {
                                let anc = bodyNodes[bi].el.closest('[data-mid]');
                                if (anc) bmid = anc.getAttribute('data-mid');
                            }
                            messages.push({ mid: bmid || mid, author: null, text: t, itemSel, regionSel, bodyIndex: bi });
                        }
                        continue;
                    }

                    let text = bodyNodes.length > 0 ? bodyNodes[0].text : null;

                    // Fallback: full item inner text (trimmed, first 40 lines, <8000 chars)
                    if (!text) {
                        const raw = (item.innerText || '').trim();
                        if (raw && raw.length < 8000) {
                            const lines = raw.split('\\n').map(l => l.trim()).filter(Boolean);
                            if (lines.length > 0) text = lines.slice(0, 40).join('\\n');
                        }
                    }

                    if (!text || !text.trim()) continue;

                    messages.push({ mid, author, text, itemSel, regionSel });
                }

                if (messages.length > 0) {
                    return { messages, itemSel, regionSel };
                }
            }
            return null;
        }
        """,
        {
            "regionSelectors": sel.MESSAGE_LIST_REGION,
            "itemSelectors": sel.MESSAGE_ITEM,
            "authorSelectors": sel.AUTHOR,
            "bodySelectors": sel.BODY,
            "maxItems": max_items,
        },
    )

    if not result or not result.get("messages"):
        return None

    item_sel = result.get("itemSel", "js")
    region_sel = result.get("regionSel", "body")
    log.debug("scrape(js): item_sel=%r region_sel=%r count=%d", item_sel, region_sel, len(result["messages"]))

    out: list[ChannelMessage] = []
    seen: set[str] = set()
    for msg_index, raw in enumerate(result["messages"]):
        text = (raw.get("text") or "").strip()
        author = raw.get("author") or None
        mid = raw.get("mid") or None

        text = _clean_message_text(text, author)
        if not text:
            continue

        if _is_date_separator(text, author):
            log.debug("scrape(js): skipping date-separator %r", text)
            continue

        if mid:
            stable = f"mid:{mid}"
        else:
            content_hash = hashlib.sha256(f"{author or ''}|{text[:500]}".encode("utf-8", errors="ignore")).hexdigest()[
                :24
            ]
            stable = f"hash:{content_hash}"

        if stable in seen:
            continue
        seen.add(stable)
        out.append(
            ChannelMessage(
                stable_id=stable,
                text=text,
                author=author,
                raw={"item_selector": item_sel, "index": msg_index, "js": True},
            )
        )

    log.info("scrape(js): returning %d unique messages (region=%r)", len(out), region_sel)
    return out


async def scrape_top_level_messages(page: Page, max_items: int = 80) -> list[ChannelMessage]:
    """Best-effort: collect top-level posts currently in the virtualized viewport + nearby.

    Uses a fast single-JS-evaluation path first; falls back to the slower
    per-element Playwright path only when the JS pass returns nothing.

    Args:
        page: The Playwright page showing the Teams web client.
        max_items: Maximum number of unique messages to return.

    Returns:
        Deduplicated list of :class:`~teams_interaction.types.ChannelMessage`
        instances in DOM order.
    """
    # --- Fast path: one JS round-trip ---
    try:
        js_msgs = await _scrape_messages_js(page, max_items=max_items)
    except Exception as exc:
        log.debug("scrape(js): evaluation failed (%s), falling back to slow path", exc)
        js_msgs = None

    if js_msgs is not None:
        return js_msgs

    log.debug("scrape(js): returned nothing – falling back to slow per-element path")

    # --- Slow fallback path (original Playwright-per-element approach) ---
    region_sel = None
    for selector in sel.MESSAGE_LIST_REGION:
        loc = page.locator(selector)
        try:
            if await loc.count() == 0:
                continue
        except Exception:
            continue
        if await _visible(loc.first, timeout_ms=200):
            region_sel = selector
            log.debug("scrape: message list region matched by %r", selector)
            break
    if not region_sel:
        log.warning("scrape: no message list region found via any selector – falling back to body")
        region_sel = "body"

    region = page.locator(region_sel)
    messages: list[ChannelMessage] = []

    for item_sel in sel.MESSAGE_ITEM:
        items = region.locator(item_sel)
        try:
            item_count = await items.count()
        except Exception:
            continue
        log.debug("scrape: item selector=%r count=%d", item_sel, item_count)
        if item_count == 0:
            continue
        for index in range(min(item_count, max_items)):
            item = items.nth(index)
            if not await _visible(item, timeout_ms=200):
                continue
            msg = await _parse_message_item(item, item_sel, index)
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
    for message in messages:
        if message.stable_id in seen:
            continue
        seen.add(message.stable_id)
        out.append(message)

    log.info("scrape: returning %d unique messages (region=%r)", len(out), region_sel)
    return out


async def inspect_message_dom(page: Page, max_samples: int = 5) -> dict[str, Any]:
    """Collect a compact diagnostic snapshot of message-related DOM state.

    Iterates over all known region, item, and body selectors, recording counts
    and text samples for each. Also injects a ``scraped_messages`` key with the
    top *max_samples* messages from :func:`scrape_top_level_messages`.

    Args:
        page: The Playwright page showing the Teams web client.
        max_samples: Maximum number of sample DOM nodes/messages to include per
            selector entry.

    Returns:
        A dictionary with keys ``url``, ``active_channel``,
        ``message_regions``, ``message_items``, ``body_nodes``, and
        ``scraped_messages``.
    """
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
            for index in range(min(count, max_samples)):
                node = loc.nth(index)
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
            for index in range(min(count, max_samples)):
                node = loc.nth(index)
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
    """Fallback pass: scrape directly from message body nodes across the page.

    Used when neither the JS fast-path nor the item-selector pass yields any
    messages.

    Args:
        page: The Playwright page showing the Teams web client.
        max_items: Maximum number of unique messages to return.

    Returns:
        List of :class:`~teams_interaction.types.ChannelMessage` instances,
        deduplicated by normalised text.
    """
    out: list[ChannelMessage] = []
    seen_text: set[str] = set()

    for body_sel in sel.BODY:
        nodes = page.locator(body_sel)
        try:
            node_count = await nodes.count()
        except Exception:
            continue
        if node_count == 0:
            continue
        log.debug("fallback: body selector=%r count=%d", body_sel, node_count)
        for index in range(min(node_count, max_items * 2)):
            if len(out) >= max_items:
                return out
            node = nodes.nth(index)
            if not await _visible(node, timeout_ms=120):
                continue
            try:
                raw = (await node.inner_text()).strip()
            except Exception:
                continue
            if not raw or len(raw) < 6:
                continue
            if _looks_like_transcript_blob(raw):
                log.debug("fallback: skipping transcript-like body node selector=%r index=%d", body_sel, index)
                continue
            if _is_date_separator(raw, None):
                log.debug("fallback: skipping date-separator %r selector=%r index=%d", raw, body_sel, index)
                continue
            norm = _norm_text(raw)
            if norm in seen_text:
                continue
            seen_text.add(norm)

            # Best effort: grab data-mid from nearest ancestor-ish wrapper.
            stable_prefix = "hash"
            stable_value = ""
            try:
                wrapper_loc = node.locator("xpath=ancestor-or-self::*[@data-mid][1]")
                if await wrapper_loc.count() > 0:
                    mid = await wrapper_loc.first.get_attribute("data-mid")
                    if mid:
                        stable_prefix = "mid"
                        stable_value = mid
            except Exception:
                pass
            if not stable_value:
                stable_value = hashlib.sha256(f"{raw[:500]}".encode("utf-8", errors="ignore")).hexdigest()[:24]

            out.append(
                ChannelMessage(
                    stable_id=f"{stable_prefix}:{stable_value}",
                    text=raw,
                    author=None,
                    raw={"fallback": True, "body_selector": body_sel, "index": index},
                )
            )
    log.info("fallback: extracted %d message body node(s)", len(out))
    return out


async def _parse_message_item(item: Locator, item_sel: str, index: int) -> ChannelMessage | None:
    """Parse a single message-item DOM node into a :class:`ChannelMessage`.

    Attempts to extract the author via ``AUTHOR`` selectors (with ``aria-label``
    fallback) and the body text via ``BODY`` selectors (with full item
    inner-text fallback).

    Args:
        item: Playwright locator for the message-item root element.
        item_sel: CSS selector string that matched *item* (stored in ``raw``).
        index: Position of *item* within its parent locator (for logging).

    Returns:
        A :class:`~teams_interaction.types.ChannelMessage`, or ``None`` when
        the item should be skipped (empty text, date-separator, multi-message
        container, etc.).
    """
    text_parts: list[str] = []
    author: str | None = None

    for author_sel in sel.AUTHOR:
        al = item.locator(author_sel)
        try:
            if await al.count() == 0:
                continue
        except Exception:
            continue
        if await _visible(al.first, timeout_ms=150):
            try:
                author_text = (await al.inner_text()).strip()
                if author_text:
                    author = author_text
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

    for body_sel in sel.BODY:
        bl = item.locator(body_sel)
        try:
            cnt = await bl.count()
        except Exception:
            continue
        for body_index in range(min(cnt, 3)):
            cell = bl.nth(body_index)
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

    if _is_date_separator(text, author):
        log.debug("_parse_message_item: index=%d looks like date separator %r, skipping", index, text)
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
    """Compute a stable deduplication ID for a message-item locator.

    Prefers the ``data-mid`` attribute on the element (or any nested child).
    Falls back to a SHA-256 content hash when ``data-mid`` is absent.

    Args:
        item: Playwright locator for the message-item root element.
        item_sel: CSS selector string that matched *item* (kept for signature
            symmetry).
        index: Zero-based index of the item (kept for signature symmetry).
        text: Visible message text used to compute the fallback hash.
        author: Display name of the author (included in hash input).

    Returns:
        A string of the form ``"mid:<value>"`` or ``"hash:<hex-prefix>"``.
    """
    # Check data-mid on the item itself first, then on any nested element.
    try:
        mid = await item.get_attribute("data-mid")
        if mid:
            return f"mid:{mid}"
    except Exception:
        pass
    try:
        child_loc = item.locator("[data-mid]")
        if await child_loc.count() > 0:
            mid = await child_loc.first.get_attribute("data-mid")
            if mid:
                return f"mid:{mid}"
    except Exception:
        pass
    content_hash = hashlib.sha256(f"{author or ''}|{text[:500]}".encode("utf-8", errors="ignore")).hexdigest()[:24]
    return f"hash:{content_hash}"


async def send_plain_text(page: Page, text: str) -> None:
    """Type *text* into the Teams compose box and submit it.

    Locates the compose textbox using
    :data:`~teams_interaction.selectors.COMPOSE` selectors, fills it with
    *text*, then clicks the send button (or presses Enter as a fallback).

    Args:
        page: The Playwright page showing the Teams web client with an active
            channel ready for input.
        text: Plain-text message to send.

    Raises:
        RuntimeError: If no compose textbox can be found.
    """
    box: Locator | None = None
    for selector in sel.COMPOSE:
        loc = page.locator(selector).first
        if await _visible(loc, timeout_ms=600):
            box = loc
            break
    if box is None:
        raise RuntimeError("Could not find compose textbox; UI may have changed.")

    await box.click()
    await box.fill("")
    await page.keyboard.insert_text(text)

    clicked = False
    for selector in sel.SEND_BUTTON:
        btn = page.locator(selector).first
        if await _visible(btn, timeout_ms=400):
            await btn.click()
            clicked = True
            break
    if not clicked:
        await page.keyboard.press("Enter")


def normalize_teams_url(url: str) -> str:
    """Validate and strip whitespace from a Teams channel URL.

    Args:
        url: Raw URL string, optionally surrounded by whitespace.

    Returns:
        The stripped URL.

    Raises:
        ValueError: If *url* does not contain ``teams.microsoft.com`` or does
            not start with ``http``.
    """
    stripped_url = url.strip()
    if "teams.microsoft.com" not in stripped_url:
        raise ValueError("channel url must be a teams.microsoft.com link")
    if not stripped_url.startswith("http"):
        raise ValueError("channel url must start with https://")
    return stripped_url
