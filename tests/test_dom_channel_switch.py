"""Integration tests for channel switching in :mod:`teams_interaction.dom`."""

from __future__ import annotations

from playwright.async_api import Page

from teams_interaction.dom import active_channel_name, switch_to_channel


async def test_switch_to_channel_uses_dom_state_not_url(page: Page) -> None:
    """Clicking an ``Engineering`` treeitem updates the heading and marks it aria-selected."""
    html = """<!DOCTYPE html>
<html><body>
<div id="nav">
  <div role="treeitem" aria-selected="true">General</div>
  <div role="treeitem">Engineering</div>
</div>
<header><h1 role="heading">General</h1></header>
<script>
const items = Array.from(document.querySelectorAll('[role="treeitem"]'));
for (const item of items) {
  item.addEventListener('click', () => {
    for (const n of items) n.removeAttribute('aria-selected');
    item.setAttribute('aria-selected', 'true');
    document.querySelector('h1[role="heading"]').textContent = item.textContent.trim();
  });
}
</script>
</body></html>"""
    await page.set_content(html)

    await switch_to_channel(page, "Engineering", timeout_ms=2000)

    assert await active_channel_name(page) == "Engineering"
    assert (await page.locator('[role="treeitem"][aria-selected="true"]').inner_text()).strip() == "Engineering"


async def test_switch_to_channel_noop_if_already_active(page: Page) -> None:
    """``switch_to_channel`` returns immediately when the target is already active."""
    html = """<!DOCTYPE html>
<html><body>
<div role="treeitem" aria-selected="true">General</div>
<header><h1 role="heading">General</h1></header>
</body></html>"""
    await page.set_content(html)

    await switch_to_channel(page, "General", timeout_ms=500)

    assert await active_channel_name(page) == "General"


async def test_switch_to_channel_matches_aria_label_chat_entry(page: Page) -> None:
    """``aria-label`` of a listitem is used as the display name for matching."""
    html = """<!DOCTYPE html>
<html><body>
<ul>
  <li role="listitem" aria-label="General"></li>
  <li role="listitem" aria-label="Josef Ondrej (You)"></li>
</ul>
<header><h1 role="heading">General</h1></header>
<script>
const items = Array.from(document.querySelectorAll('[role="listitem"]'));
for (const item of items) {
  item.addEventListener('click', () => {
    for (const n of items) n.removeAttribute('aria-selected');
    item.setAttribute('aria-selected', 'true');
    document.querySelector('h1[role="heading"]').textContent = item.getAttribute('aria-label');
  });
}
</script>
</body></html>"""
    await page.set_content(html)

    await switch_to_channel(page, "Josef Ondrej (You)", timeout_ms=2000)

    assert await active_channel_name(page) == "Josef Ondrej (You)"


async def test_switch_to_channel_skips_self_profile_entry(page: Page) -> None:
    """When searching for a name, self-profile entries containing ``(You)`` are skipped."""
    html = """<!DOCTYPE html>
<html><body>
<div id="nav">
  <div role="treeitem" data-tid="chat-title-avatar">Josef Ondrej (You)</div>
  <div role="treeitem" data-tid="chat-list-item">Josef Ondrej</div>
</div>
<header><h1 role="heading">General</h1></header>
<script>
const items = Array.from(document.querySelectorAll('[role="treeitem"]'));
for (const item of items) {
  item.addEventListener('click', () => {
    for (const n of items) n.removeAttribute('aria-selected');
    item.setAttribute('aria-selected', 'true');
    document.querySelector('h1[role="heading"]').textContent = item.textContent.trim();
  });
}
</script>
</body></html>"""
    await page.set_content(html)

    await switch_to_channel(page, "Josef Ondrej", timeout_ms=2000)

    assert await active_channel_name(page) == "Josef Ondrej"
