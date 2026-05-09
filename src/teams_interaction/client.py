from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, async_playwright

from teams_interaction.dom import goto_channel, normalize_teams_url, scrape_top_level_messages, send_plain_text
from teams_interaction.types import ChannelMessage

MessageHandler = Callable[[ChannelMessage], Awaitable[None] | None]


class TeamsClient:
    """
    Browser-driven Teams client (persistent profile, no Azure registration).

    Uses Playwright with Chromium/Edge; set TEAMS_BROWSER_CHANNEL / TEAMS_BROWSER_EXECUTABLE.
    """

    def __init__(
        self,
        *,
        profile_dir: Path | str | None = None,
        browser_channel: str | None = None,
        executable_path: str | None = None,
        headless: bool = False,
    ) -> None:
        self._profile_dir = Path(
            profile_dir
            or os.environ.get(
                "TEAMS_PROFILE_DIR",
                str(Path.home() / ".cache" / "ms-teams-interaction" / "browser-profile"),
            )
        )
        self._browser_channel = browser_channel or os.environ.get("TEAMS_BROWSER_CHANNEL", "msedge")
        exe = executable_path or os.environ.get("TEAMS_BROWSER_EXECUTABLE")
        self._executable_path = exe if exe else None
        self._headless = headless
        self._playwright: Any = None
        self._context: BrowserContext | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._run_guard = asyncio.Lock()

    async def start(self) -> None:
        async with self._run_guard:
            if self._context:
                return
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_playwright().start()
            base: dict[str, Any] = dict(
                user_data_dir=str(self._profile_dir),
                headless=self._headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            if self._executable_path:
                base["executable_path"] = self._executable_path
                self._context = await self._playwright.chromium.launch_persistent_context(**base)
            else:
                try:
                    base["channel"] = self._browser_channel
                    self._context = await self._playwright.chromium.launch_persistent_context(**base)
                except Exception:
                    base.pop("channel", None)
                    self._context = await self._playwright.chromium.launch_persistent_context(**base)

    async def close(self) -> None:
        async with self._run_guard:
            for t in self._tasks:
                t.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            if self._context:
                await self._context.close()
                self._context = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

    async def open_channel(self, channel_url: str) -> None:
        """Open a channel in a new tab (useful for first login)."""
        await self._require_context()
        url = normalize_teams_url(channel_url)
        page = await self._context.new_page()
        await goto_channel(page, url)

    async def send_message(self, channel_url: str, text: str) -> None:
        await self._require_context()
        url = normalize_teams_url(channel_url)
        page = await self._context.new_page()
        try:
            await goto_channel(page, url)
            await send_plain_text(page, text)
        finally:
            await page.close()

    def watch_channel(
        self,
        channel_url: str,
        on_message: MessageHandler,
        *,
        poll_interval: float = 2.0,
    ) -> asyncio.Task[None]:
        """
        Poll the channel on a dedicated tab and invoke ``on_message`` for new top-level posts.

        The first successful scrape primes state (no callbacks). IDs are best-effort
        (``data-mid`` when present, else a content hash).
        """
        task = asyncio.create_task(self._watch_channel_loop(channel_url, on_message, poll_interval))
        self._tasks.append(task)
        return task

    async def _watch_channel_loop(
        self,
        channel_url: str,
        on_message: MessageHandler,
        poll_interval: float,
    ) -> None:
        await self._require_context()
        url = normalize_teams_url(channel_url)
        page = await self._context.new_page()
        await goto_channel(page, url)
        seen: set[str] = set()
        primed = False
        try:
            while True:
                try:
                    msgs = await scrape_top_level_messages(page)
                    if not primed:
                        for m in msgs:
                            seen.add(m.stable_id)
                        primed = True
                    else:
                        for m in msgs:
                            if m.stable_id in seen:
                                continue
                            seen.add(m.stable_id)
                            out = on_message(m)
                            if asyncio.iscoroutine(out):
                                await out
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Keep polling; UI may be loading or selectors outdated.
                    pass
                await asyncio.sleep(poll_interval)
        finally:
            await page.close()

    async def _require_context(self) -> None:
        if not self._context:
            raise RuntimeError("Call await client.start() first")
