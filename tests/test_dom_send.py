from __future__ import annotations

from playwright.async_api import Page

from teams_interaction.dom import send_plain_text


async def test_send_plain_text_finds_compose_and_send(page: Page) -> None:
    html = """<!DOCTYPE html>
<html><body>
<div data-tid="ckeditor"><div role="textbox" contenteditable="true"></div></div>
<button type="button" data-tid="sendButton" aria-label="Send">Send</button>
<script>
document.querySelector('[data-tid="sendButton"]').addEventListener('click', () => {
  document.body.setAttribute('data-sent', '1');
});
</script>
</body></html>"""
    await page.set_content(html)

    await send_plain_text(page, "hello fixture")

    assert await page.locator("body").get_attribute("data-sent") == "1"
    box = page.locator('div[data-tid="ckeditor"] div[role="textbox"]').first
    assert "hello fixture" in (await box.inner_text())
