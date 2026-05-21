"""High-level async client for browser-driven Microsoft Teams interaction."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, async_playwright

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

    Uses Playwright with Chromium/Edge; set TEAMS_BROWSER_DIST / TEAMS_BROWSER_EXECUTABLE.
    """

    def __init__(
        self,
        *,
        profile_dir: Path | str | None = None,
        browser_channel: str | None = None,
        executable_path: str | None = None,
        headless: bool = False,
        persistent: bool = True,
    ) -> None:
        """Initialise the client with browser configuration.

        All parameters are keyword-only and fall back to environment variables
        when ``None``.

        Args:
            profile_dir: Path to the persistent Chromium profile directory.
                Defaults to ``$TEAMS_PROFILE_DIR`` or
                ``~/.cache/ms-teams-interaction/browser-profile``.
            browser_channel: Playwright browser channel (e.g. ``"msedge"``
                or ``"chrome"``).  Defaults to ``$TEAMS_BROWSER_DIST``
                (``"msedge"`` if unset).
            executable_path: Absolute path to a custom browser binary.
                Overrides *browser_channel*.  Defaults to
                ``$TEAMS_BROWSER_EXECUTABLE``.
            headless: Launch the browser in headless mode.  Defaults to
                ``False`` (visible window, needed for Teams SSO).
            persistent: When ``True`` (default), use a persistent browser
                profile so the user stays signed in between runs.  When
                ``False``, launch a clean ephemeral browser (you will need
                to sign in every time).
        """
        self._profile_dir = Path(
            profile_dir
            or os.environ.get(
                "TEAMS_PROFILE_DIR",
                str(Path.home() / ".cache" / "ms-teams-interaction" / "browser-profile"),
            )
        )
        self._browser_channel = browser_channel or os.environ.get("TEAMS_BROWSER_DIST", "msedge")
        exe = executable_path or os.environ.get("TEAMS_BROWSER_EXECUTABLE")
        self._executable_path = exe if exe else None
        self._headless = headless
        self._persistent = persistent
        # Non-persistent browsers start cold (no cache, no session) so give
        # navigation and channel-switch operations more time to complete.
        self._nav_timeout_ms: float = 15_000 if persistent else 120_000
        self._playwright: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._owns_browser: bool = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._run_guard = asyncio.Lock()
        # Pages opened by *this* client instance.  Used by _acquire_page to avoid
        # stealing tabs that belong to a sibling CLI process sharing the browser.
        self._my_pages: set[Any] = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _free_port() -> int:
        """Return an ephemeral free TCP port on localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _port_file(self) -> Path:
        """Return the path of the file used to advertise the browser's CDP port.

        Returns:
            A :class:`~pathlib.Path` inside the profile directory.
        """
        return self._profile_dir / ".cdp-port"

    async def start(self) -> None:
        """Launch the browser and create a context.

        When *persistent* is ``True`` (the default), a ``user_data_dir`` is
        used so the user stays signed in between runs.  If the configured
        profile directory is already locked by another process, numbered
        slots are tried in order (``browser-profile``, ``browser-profile-1``,
        ``browser-profile-2``, …) so that each CLI process gets its own
        independent browser window.

        When *persistent* is ``False``, a plain ephemeral browser is launched
        (no profile).  The user must sign in every time.

        This method is idempotent: calling it more than once is safe.
        """
        async with self._run_guard:
            if self._context:
                return
            self._playwright = await async_playwright().start()

            if not self._persistent:
                # ── Ephemeral (fresh) browser – no profile ────────────────────
                log.info("start: launching ephemeral browser (no persistent profile)")
                launch_kwargs: dict[str, Any] = dict(
                    headless=self._headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                if self._executable_path:
                    launch_kwargs["executable_path"] = self._executable_path
                    self._browser = await self._playwright.chromium.launch(**launch_kwargs)
                else:
                    try:
                        launch_kwargs["channel"] = self._browser_channel
                        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
                    except Exception:
                        launch_kwargs.pop("channel", None)
                        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
                self._context = await self._browser.new_context()
                self._owns_browser = True
                return

            # ── Persistent profile: try numbered slots until one is free ──────
            # Each client gets its own browser window so processes are fully
            # independent — closing one does not affect any other.
            base_dir = self._profile_dir
            _MAX_SLOTS = 16
            last_exc: Exception | None = None
            for slot in range(_MAX_SLOTS):
                profile_dir = base_dir if slot == 0 else Path(str(base_dir) + f"-{slot}")
                profile_dir.mkdir(parents=True, exist_ok=True)
                port = self._free_port()
                launch_args: dict[str, Any] = dict(
                    user_data_dir=str(profile_dir),
                    headless=self._headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        f"--remote-debugging-port={port}",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-session-crashed-bubble",
                        "--restore-last-session=false",
                    ],
                )
                try:
                    if self._executable_path:
                        launch_args["executable_path"] = self._executable_path
                        self._context = await self._playwright.chromium.launch_persistent_context(
                            **launch_args
                        )
                    else:
                        try:
                            launch_args["channel"] = self._browser_channel
                            self._context = await self._playwright.chromium.launch_persistent_context(
                                **launch_args
                            )
                        except Exception:
                            launch_args.pop("channel", None)
                            self._context = await self._playwright.chromium.launch_persistent_context(
                                **launch_args
                            )
                except Exception as exc:
                    log.info(
                        "start: slot %d (%s) unavailable (%s) – trying next …",
                        slot,
                        profile_dir.name,
                        exc,
                    )
                    last_exc = exc
                    self._context = None
                    continue

                # ── Slot acquired ─────────────────────────────────────────────
                # Update profile_dir so _port_file() / close() use the right path.
                self._profile_dir = profile_dir
                self._owns_browser = True
                self._port_file().write_text(str(port))
                log.info(
                    "start: launched browser in slot %d (profile=%s, port=%d)",
                    slot,
                    profile_dir.name,
                    port,
                )
                await self._cleanup_startup_tabs()
                return

            raise RuntimeError(
                f"start: could not launch browser in any of {_MAX_SLOTS} profile slots; "
                "all profile directories appear to be locked"
            ) from last_exc

    async def close(self) -> None:
        """Cancel background tasks and tear down the browser connection.

        Each persistent client owns its own browser window (launched in its
        own profile slot), so the browser is always closed here.  The port
        file for the slot is also removed so future processes can reuse the
        same slot.
        """
        async with self._run_guard:
            for t in self._tasks:
                t.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            if self._context:
                try:
                    await self._context.close()
                except Exception as exc:
                    log.debug("close: error closing context (may already be closed): %s", exc)
                if self._persistent:
                    self._port_file().unlink(missing_ok=True)
                self._my_pages.clear()
                self._context = None
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

    async def open_channel(self, channel_url: str | None = None, *, channel_name: str | None = None) -> None:
        """Open Teams in a new tab and optionally switch to a channel by visible name.

        Args:
            channel_url: Full Teams URL to navigate to.  Falls back to
                ``https://teams.microsoft.com/v2/`` when ``None``.
            channel_name: Visible display name of the channel/chat to select
                after loading.  Skipped when ``None``.

        Raises:
            RuntimeError: If :meth:`start` has not been called yet.
        """
        await self._require_context()
        url = self._resolve_url(channel_url)

        owns_page_out: list[bool] = [False]
        page = await self._acquire_page(url, owns_page_out=owns_page_out)

        if channel_name:
            await switch_to_channel(page, channel_name, timeout_ms=self._nav_timeout_ms)

    async def send_message(self, channel_url: str | None, text: str, *, channel_name: str | None = None) -> None:
        """Open a channel, type *text* into the compose box, and send it.

        A fresh page is opened for the operation and closed afterwards.

        Args:
            channel_url: Full Teams URL to navigate to.  Falls back to the
                default Teams URL when ``None``.
            text: Plain-text content to send.
            channel_name: Optional visible channel/chat name to switch to after
                loading the URL.

        Raises:
            RuntimeError: If :meth:`start` has not been called yet, or if the
                compose box cannot be found.
        """
        await self._require_context()
        url = self._resolve_url(channel_url)
        page = await self._new_page()
        try:
            await goto_channel(page, url, timeout_ms=self._nav_timeout_ms)
            if channel_name:
                await switch_to_channel(page, channel_name, timeout_ms=self._nav_timeout_ms)
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
        """Navigate to a channel and return a diagnostic DOM snapshot.

        Args:
            channel_url: Full Teams URL to navigate to.  Falls back to the
                default Teams URL when ``None``.
            channel_name: Optional visible channel/chat name to switch to.
            max_samples: Maximum number of DOM sample nodes/messages to collect
                per selector entry.

        Returns:
            The dictionary produced by
            :func:`~teams_interaction.dom.inspect_message_dom`.

        Raises:
            RuntimeError: If :meth:`start` has not been called yet.
        """
        await self._require_context()
        url = self._resolve_url(channel_url)
        page = await self._new_page()
        try:
            await goto_channel(page, url, timeout_ms=self._nav_timeout_ms)
            if channel_name:
                await switch_to_channel(page, channel_name, timeout_ms=self._nav_timeout_ms)
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
        poll_interval: float = 0.25,
    ) -> asyncio.Task[None]:
        """
        Poll the channel on a dedicated tab and invoke ``on_message`` for new top-level posts.

        Primes immediately on startup (no waiting for initial render).
        IDs are best-effort (``data-mid`` when present, else a content hash).

        Args:
            channel_url: Full Teams URL to navigate to.  Falls back to the
                default Teams URL when ``None``.
            on_message: Async or sync callable invoked for each new message.
            channel_name: Visible channel/chat name to switch to after loading.
            include_existing: When ``True``, the messages already in the DOM at
                startup are forwarded to *on_message*.
            poll_interval: Seconds between successive DOM scrapes.

        Returns:
            The :class:`asyncio.Task` running the polling loop.  Cancel it (or
            let the client close) to stop watching.
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
        """Background coroutine that continuously scrapes and emits new messages.

        Reuses an existing Teams page if one is already open; otherwise opens
        a new page and owns its lifecycle.

        Args:
            channel_url: Full Teams URL to navigate to.
            on_message: Callable invoked for every new
                :class:`~teams_interaction.types.ChannelMessage`.
            poll_interval: Seconds to sleep between scrape passes.
            channel_name: Optional channel/chat name to select after loading.
            include_existing: When ``True``, messages found during the priming
                pass are forwarded to *on_message*.
        """
        await self._require_context()
        url = self._resolve_url(channel_url)

        # ── Page selection ────────────────────────────────────────────────────
        # Delegate to _acquire_page which reuses a Teams tab, recycles a blank
        # tab, or opens a fresh one — avoiding duplicate blank tabs.
        owns_page_out: list[bool] = [False]
        page = await self._acquire_page(url, owns_page_out=owns_page_out)
        owns_page = owns_page_out[0]

        if channel_name:
            await switch_to_channel(page, channel_name, timeout_ms=self._nav_timeout_ms)

        seen: set[str] = set()

        # ── Priming phase ────────────────────────────────────────────────────
        # Snapshot whatever is currently in the DOM right now — no waiting.
        try:
            initial_msgs = await scrape_top_level_messages(page)
        except Exception:
            initial_msgs = []
        for m in initial_msgs:
            seen.add(m.stable_id)
            if include_existing:
                out = on_message(m)
                if asyncio.iscoroutine(out):
                    await out
        log.info("watch: primed with %d existing message id(s)", len(seen))

        # ── Poll loop ─────────────────────────────────────────────────────────
        poll_count = 0
        try:
            while True:
                await asyncio.sleep(poll_interval)
                poll_count += 1
                try:
                    msgs = await scrape_top_level_messages(page)
                    log.debug("watch: poll #%d – scraped %d messages", poll_count, len(msgs))
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
        finally:
            log.info("watch: loop ended%s", ", closing page" if owns_page else "")
            if owns_page:
                await page.close()

    _BLANK_URLS: frozenset[str] = frozenset(
        {"", "about:blank", "about:newtab", "chrome://newtab/", "edge://newtab/"}
    )


    async def _cleanup_startup_tabs(self) -> None:
        """Reduce the browser to a single usable tab after launch.

        Closes every page that is not the one we want to keep, including
        leftover tabs restored from a previous session.  The surviving tab is
        registered in ``self._my_pages`` so ``_acquire_page`` can recycle it.

        Priority:
        1. If a Teams tab exists, keep the first one and close everything else.
        2. Otherwise keep the first blank tab and close everything else.
        3. If there are no pages at all, do nothing (``_new_page`` will create
           one when needed).

        At least one tab is always left alive because Chrome becomes unstable
        with zero open tabs.
        """
        if not self._context:
            return

        all_pages = [p for p in self._context.pages if not p.is_closed()]
        if not all_pages:
            return

        teams_pages = [p for p in all_pages if "teams.microsoft.com" in p.url]
        blank_pages = [p for p in all_pages if p.url in self._BLANK_URLS]

        # Decide which single tab to keep.
        if teams_pages:
            keeper = teams_pages[0]
        elif blank_pages:
            keeper = blank_pages[0]
        else:
            # Non-blank, non-Teams leftovers only — keep the first one and
            # navigate it to Teams later via _acquire_page.
            keeper = all_pages[0]

        self._my_pages.add(keeper)

        # Close everything else.
        closed = 0
        for p in all_pages:
            if p is keeper:
                continue
            try:
                await p.close()
                closed += 1
                log.debug("_cleanup_startup_tabs: closed tab url=%r", p.url)
            except Exception as exc:
                log.debug("_cleanup_startup_tabs: could not close tab url=%r: %s", p.url, exc)

        log.info(
            "_cleanup_startup_tabs: kept url=%r, closed %d other tab(s)",
            keeper.url,
            closed,
        )

    async def _acquire_page(self, url: str, *, owns_page_out: list[bool]) -> Any:
        """Return a page suitable for Teams interaction.

        Strategy (in order):

        1. Reuse an existing Teams tab owned by this client — no navigation needed.
        2. Reuse an existing blank/newtab owned by this client — navigate it to
           *url* instead of spawning a second blank tab alongside it.
        3. Open a brand-new page and navigate it to *url*.

        *owns_page_out* is a single-element list used as an out-parameter; it
        is set to ``[True]`` when the caller should close the page after use,
        and ``[False]`` when an already-open Teams page was reused.
        """
        pages = [p for p in self._context.pages if not p.is_closed()]  # type: ignore[union-attr]

        # Only consider pages that *this* client opened — never steal a tab
        # from another process that shares the same browser context via CDP.
        my_pages = [p for p in pages if p in self._my_pages]

        for p in my_pages:
            if "teams.microsoft.com" in p.url:
                log.info("_acquire_page: reusing existing Teams page (url=%s)", p.url)
                owns_page_out[0] = False
                return p

        for p in my_pages:
            if p.url in self._BLANK_URLS:
                log.info("_acquire_page: reusing blank tab, navigating to %s", url)
                owns_page_out[0] = True
                await goto_channel(p, url, timeout_ms=self._nav_timeout_ms)
                return p

        log.info("_acquire_page: opening new page, url=%s", url)
        p = await self._new_page()
        owns_page_out[0] = True
        await goto_channel(p, url, timeout_ms=self._nav_timeout_ms)
        return p

    async def _new_page(self) -> Any:
        """Open a new browser tab, with a ``window.open`` fallback.

        ``BrowserContext.new_page()`` sends ``Target.createTarget`` over the
        CDP debug port.  Chrome refuses this for the default persistent context
        when connected externally; in that case we fall back to calling
        ``window.open('about:blank', '_blank')`` on an existing page, which
        bypasses the restriction because the tab is created by the renderer.

        The new page is registered in ``self._my_pages`` automatically.

        Returns:
            A :class:`playwright.async_api.Page` for the new tab.
        """
        try:
            p = await self._context.new_page()  # type: ignore[union-attr]
            self._my_pages.add(p)
            return p
        except Exception as exc:
            log.warning(
                "_new_page: context.new_page() failed (%s) – using window.open fallback", exc
            )
            return await self._open_page_via_js()

    async def _open_page_via_js(self) -> Any:
        """Open a new blank tab via ``window.open`` on an existing page.

        This method is used as a fallback when :meth:`_new_page` fails (e.g.
        when Chrome disallows ``Target.createTarget`` over an external CDP
        connection for a persistent context).

        Returns:
            The newly opened :class:`playwright.async_api.Page`.

        Raises:
            RuntimeError: If there are no open pages to trigger ``window.open`` from.
        """
        pages = [p for p in self._context.pages if not p.is_closed()]  # type: ignore[union-attr]
        if not pages:
            raise RuntimeError(
                "_open_page_via_js: no open pages available to trigger window.open"
            )
        async with self._context.expect_page() as page_info:  # type: ignore[union-attr]
            await pages[0].evaluate("() => window.open('about:blank', '_blank')")
        p = await page_info.value
        self._my_pages.add(p)
        log.info("_open_page_via_js: opened new tab via window.open")
        return p

    async def _require_context(self) -> None:
        """Raise ``RuntimeError`` if the browser context is not initialised.

        Raises:
            RuntimeError: If :meth:`start` has not been called yet.
        """
        if not self._context:
            raise RuntimeError("Call await client.start() first")

    def _resolve_url(self, channel_url: str | None) -> str:
        """Return a validated Teams URL, defaulting to the Teams v2 root.

        Args:
            channel_url: A raw Teams URL, or ``None`` to use the default.

        Returns:
            A normalised ``https://teams.microsoft.com/…`` URL string.
        """
        if not channel_url:
            return DEFAULT_TEAMS_URL
        return normalize_teams_url(channel_url)

