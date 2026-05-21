from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, async_playwright

from teams_interaction.dom import (
    goto_channel,
    inspect_message_dom,
    normalize_teams_url,
    scrape_top_level_messages,
    send_plain_text,
    switch_to_channel,
)
from teams_interaction.types import ChannelMessage

MessageHandler = Callable[[ChannelMessage], Awaitable[None] | None]
DEFAULT_TEAMS_URL = "https://teams.microsoft.com/v2/"

log = logging.getLogger(__name__)


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

    async def open_channel(self, channel_url: str | None = None, *, channel_name: str | None = None) -> None:
        """Open Teams in a new tab and optionally switch to a channel by visible name."""
        await self._require_context()
        url = self._resolve_url(channel_url)
        page = await self._context.new_page()
        await goto_channel(page, url)
        if channel_name:
            await switch_to_channel(page, channel_name)

    async def send_message(self, channel_url: str | None, text: str, *, channel_name: str | None = None) -> None:
        await self._require_context()
        url = self._resolve_url(channel_url)
        page = await self._context.new_page()
        try:
            await goto_channel(page, url)
            if channel_name:
                await switch_to_channel(page, channel_name)
            await send_plain_text(page, text)
        finally:
            await page.close()

    async def inspect_channel(
        self,
        channel_url: str | None = None,
        *,
        channel_name: str | None = None,
        max_samples: int = 5,
    ) -> dict[str, Any]:
        await self._require_context()
        url = self._resolve_url(channel_url)
        page = await self._context.new_page()
        try:
            await goto_channel(page, url)
            if channel_name:
                await switch_to_channel(page, channel_name)
            return await inspect_message_dom(page, max_samples=max_samples)
        finally:
            await page.close()

    def watch_channel(
        self,
        channel_url: str | None,
        on_message: MessageHandler,
        *,
        channel_name: str | None = None,
        include_existing: bool = False,
        poll_interval: float = 2.0,
    ) -> asyncio.Task[None]:
        """
        Poll the channel on a dedicated tab and invoke ``on_message`` for new top-level posts.

        The first successful scrape primes state (no callbacks). IDs are best-effort
        (``data-mid`` when present, else a content hash).
        """
        task = asyncio.create_task(
            self._watch_channel_loop(channel_url, on_message, poll_interval, channel_name, include_existing)
        )
        self._tasks.append(task)
        return task

    async def _watch_channel_loop(
        self,
        channel_url: str | None,
        on_message: MessageHandler,
        poll_interval: float,
        channel_name: str | None,
        include_existing: bool,
    ) -> None:
        await self._require_context()
        url = self._resolve_url(channel_url)
        log.info("watch: opening page, url=%s channel=%r", url, channel_name)
        page = await self._context.new_page()
        await goto_channel(page, url)
        if channel_name:
            await switch_to_channel(page, channel_name)
        seen: set[str] = set()
        primed = False
        poll_count = 0
        prime_started = time.monotonic()
        max_prime_wait_s = 20.0
        try:
            while True:
                poll_count += 1
                try:
                    msgs = await scrape_top_level_messages(page)
                    log.debug("watch: poll #%d – scraped %d messages", poll_count, len(msgs))
                    if not primed:
                        if not msgs:
                            waited = time.monotonic() - prime_started
                            if waited < max_prime_wait_s:
                                log.info(
                                    "watch: waiting for initial message render before priming (%.1fs/%.1fs)",
                                    waited,
                                    max_prime_wait_s,
                                )
                                await asyncio.sleep(poll_interval)
                                continue
                            primed = True
                            log.info("watch: primed empty after startup timeout (%.1fs)", waited)
                            await asyncio.sleep(poll_interval)
                            continue

                        for m in msgs:
                            if m.stable_id in seen:
                                continue
                            seen.add(m.stable_id)
                            if include_existing:
                                out = on_message(m)
                                if asyncio.iscoroutine(out):
                                    await out
                        primed = True
                        log.info("watch: primed with %d existing message ids", len(seen))
                    else:
                        new_count = 0
                        for m in msgs:
                            if m.stable_id in seen:
                                continue
                            seen.add(m.stable_id)
                            new_count += 1
                            out = on_message(m)
                            if asyncio.iscoroutine(out):
                                await out
                        if new_count:
                            log.info("watch: poll #%d delivered %d new message(s)", poll_count, new_count)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("watch: poll #%d error (will retry): %s", poll_count, exc, exc_info=True)
                await asyncio.sleep(poll_interval)
        finally:
            log.info("watch: loop ended, closing page")
            await page.close()

    async def _require_context(self) -> None:
        if not self._context:
            raise RuntimeError("Call await client.start() first")

    def _resolve_url(self, channel_url: str | None) -> str:
        if not channel_url:
            return DEFAULT_TEAMS_URL
        return normalize_teams_url(channel_url)

