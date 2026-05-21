"""Shared pytest fixtures for the ms-teams-interaction test suite."""

from __future__ import annotations

import pytest_asyncio
from playwright.async_api import Page, async_playwright


@pytest_asyncio.fixture
async def page() -> Page:
    """Yield a headless Chromium :class:`~playwright.async_api.Page` for each test.

    The browser and context are created fresh per test and torn down
    afterwards, so tests are fully isolated.

    Yields:
        A ready-to-use Playwright :class:`~playwright.async_api.Page`.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        pg = await context.new_page()
        yield pg
        await context.close()
        await browser.close()
