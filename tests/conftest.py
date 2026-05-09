from __future__ import annotations

import pytest_asyncio
from playwright.async_api import Page, async_playwright


@pytest_asyncio.fixture
async def page() -> Page:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        pg = await context.new_page()
        yield pg
        await context.close()
        await browser.close()
