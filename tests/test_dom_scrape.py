from __future__ import annotations

from playwright.async_api import Page

from teams_interaction.dom import scrape_top_level_messages


async def test_scrape_top_level_messages_minimal_fixture(page: Page) -> None:
    html = """<!DOCTYPE html>
<html><body>
<div role="main">
  <div data-tid="chat-pane-item" data-mid="mid-1">
    <span data-tid="message-author-name">Alice</span>
    <div data-tid="messageBodyContent">Hello from tests</div>
  </div>
  <div data-tid="chat-pane-item" data-mid="mid-2">
    <span data-tid="message-author-name">Bob</span>
    <div data-tid="messageBodyContent">Second line</div>
  </div>
</div>
</body></html>"""
    await page.set_content(html)

    msgs = await scrape_top_level_messages(page, max_items=10)

    assert len(msgs) == 2
    assert msgs[0].stable_id == "mid:mid-1"
    assert msgs[0].author == "Alice"
    assert msgs[0].text == "Hello from tests"
    assert msgs[1].stable_id == "mid:mid-2"
    assert msgs[1].author == "Bob"
    assert msgs[1].text == "Second line"


async def test_scrape_top_level_messages_dedupes_duplicate_mid(page: Page) -> None:
    html = """<!DOCTYPE html>
<html><body>
<div role="main">
  <div data-tid="chat-pane-item" data-mid="shared">
    <span data-tid="message-author-name">A</span>
    <div data-tid="messageBodyContent">First</div>
  </div>
  <div data-tid="chat-pane-item" data-mid="shared">
    <span data-tid="message-author-name">A</span>
    <div data-tid="messageBodyContent">Second</div>
  </div>
</div>
</body></html>"""
    await page.set_content(html)

    msgs = await scrape_top_level_messages(page, max_items=10)
    assert [m.stable_id for m in msgs] == ["mid:shared"]
    assert msgs[0].text == "First"
