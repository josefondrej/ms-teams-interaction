from __future__ import annotations

import hashlib

from playwright.async_api import Locator, Page

from teams_interaction import selectors as sel
from teams_interaction.types import ChannelMessage


async def _visible(loc: Locator, timeout_ms: float = 500) -> bool:
    try:
        await loc.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def goto_channel(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)


async def scrape_top_level_messages(page: Page, max_items: int = 80) -> list[ChannelMessage]:
    """Best-effort: collect top-level posts currently in the virtualized viewport + nearby."""
    region_sel = None
    for s in sel.MESSAGE_LIST_REGION:
        loc = page.locator(s).first
        if await _visible(loc, timeout_ms=800):
            region_sel = s
            break
    if not region_sel:
        region_sel = "body"

    region = page.locator(region_sel)
    messages: list[ChannelMessage] = []

    for item_sel in sel.MESSAGE_ITEM:
        items = region.locator(item_sel)
        try:
            n = await items.count()
        except Exception:
            continue
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
            break

    # Dedupe by stable_id while preserving order
    seen: set[str] = set()
    out: list[ChannelMessage] = []
    for m in messages:
        if m.stable_id in seen:
            continue
        seen.add(m.stable_id)
        out.append(m)
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

    if not text_parts:
        try:
            fallback = (await item.inner_text()).strip()
            # Skip huge blobs (collapsed thread chrome)
            if fallback and len(fallback) < 8000:
                lines = [ln.strip() for ln in fallback.splitlines() if ln.strip()]
                if lines:
                    text_parts = ["\n".join(lines[:40])]
        except Exception:
            pass

    text = "\n".join(text_parts).strip()
    if not text:
        return None

    stable = await _stable_id_for_item(item, item_sel, index, text, author)
    return ChannelMessage(
        stable_id=stable,
        text=text,
        author=author,
        raw={"item_selector": item_sel, "index": index},
    )


async def _stable_id_for_item(item: Locator, item_sel: str, index: int, text: str, author: str | None) -> str:
    try:
        mid = await item.get_attribute("data-mid")
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
