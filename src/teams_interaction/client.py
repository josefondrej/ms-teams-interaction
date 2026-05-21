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
        """Launch (or reattach to) the browser and create a context.

        When *persistent* is ``True`` (the default), a ``user_data_dir`` is
        used so the user stays signed in between runs.  If a ``.cdp-port``
        file exists, attempts to connect to an already-running browser over
        CDP first.

        When *persistent* is ``False``, a plain ephemeral browser is launched
        (no profile, no CDP reuse).  The user must sign in every time.

        This method is idempotent: calling it more than once is safe.
        """
        async with self._run_guard:
            if self._context:
                return
            self._playwright = await async_playwright().start()

            if not self._persistent:
                # ── Ephemeral (fresh) browser – no profile, no CDP reuse ─────
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

            # ── Persistent profile path ───────────────────────────────────────
            self._profile_dir.mkdir(parents=True, exist_ok=True)

            # ── Try to reuse an already-running browser instance ──────────────
            port_file = self._port_file()
            if port_file.exists():
                try:
                    port = int(port_file.read_text().strip())
                    log.info("start: found CDP port %d – connecting to existing browser", port)
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}"
                    )
                    # Persistent-context browsers expose their context as contexts[0]
                    self._context = (
                        self._browser.contexts[0]
                        if self._browser.contexts
                        else await self._browser.new_context()
                    )
                    self._owns_browser = False
                    await self._close_non_teams_tabs()
                    log.info("start: attached to existing browser (pid unknown, port=%d)", port)
                    return
                except Exception as exc:
                    log.info("start: could not reuse existing browser (%s) – launching fresh", exc)
                    port_file.unlink(missing_ok=True)
                    self._browser = None

            # ── Launch a brand-new browser and advertise its debug port ───────
            port = self._free_port()
            base: dict[str, Any] = dict(
                user_data_dir=str(self._profile_dir),
                headless=self._headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    f"--remote-debugging-port={port}",
                    # Prevent Edge/Chrome from opening extra tabs on launch
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-session-crashed-bubble",
                    "--restore-last-session=false",
                ],
            )
            launch_ok = False
            try:
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
                launch_ok = True
            except Exception as launch_exc:
                # The profile directory may be locked by another process that has
                # not yet written its port file (race condition) or whose port file
                # we could not connect to earlier.  Poll for the port file and try
                # CDP-attach before giving up.
                log.info(
                    "start: launch_persistent_context failed (%s) – "
                    "waiting for another process to advertise its CDP port …",
                    launch_exc,
                )
                attached = await self._wait_and_attach_cdp(port_file, timeout=30.0)
                if not attached:
                    raise

            if self._context is None:
                # Should not happen, but guard against it.
                raise RuntimeError("start: browser context is None after launch/attach")

            if launch_ok:
                self._owns_browser = True
                port_file.write_text(str(port))
                log.info("start: launched new browser, CDP port=%d written to %s", port, port_file)
            await self._close_non_teams_tabs()

    async def close(self) -> None:
        """Cancel background tasks and tear down the browser connection.

        If this client owns the browser (i.e. it launched it), the browser /
        context is closed.  For persistent-profile browsers the port file is
        also removed.  If the client only attached to an existing browser, the
        context and browser are left alive for other consumers.
        """
        async with self._run_guard:
            for t in self._tasks:
                t.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            if self._context:
                if self._owns_browser:
                    if self._persistent:
                        # Closing the persistent context also closes the browser
                        await self._context.close()
                        self._port_file().unlink(missing_ok=True)
                    else:
                        # Ephemeral: close context then browser separately
                        await self._context.close()
                # If we only attached, leave the context/browser alive for other consumers
                self._context = None
            if self._browser:
                if not self._owns_browser:
                    # Detach without killing the browser
                    try:
                        await self._browser.close()
                    except Exception:
                        pass
                elif not self._persistent:
                    # Ephemeral – we own it, kill it
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
        page = await self._context.new_page()
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
        page = await self._context.new_page()
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

    async def _wait_and_attach_cdp(self, port_file: Path, *, timeout: float = 30.0) -> bool:
        """Poll *port_file* until another process writes a CDP port, then attach.

        Used as a fallback when ``launch_persistent_context`` fails because the
        profile directory is already locked by a sibling CLI process.  Waits up
        to *timeout* seconds for the port file to appear / become readable, then
        connects via CDP and populates ``self._browser`` / ``self._context``.

        Args:
            port_file: Path to the ``.cdp-port`` file to watch.
            timeout: Maximum number of seconds to wait.

        Returns:
            ``True`` when the attachment succeeded, ``False`` when it timed out
            or every connection attempt failed.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        poll_interval = 0.25
        last_port: int | None = None
        log.info("_wait_and_attach_cdp: polling %s for up to %.0fs …", port_file, timeout)
        while asyncio.get_event_loop().time() < deadline:
            try:
                if port_file.exists():
                    port = int(port_file.read_text().strip())
                    if port != last_port:
                        last_port = port
                        log.info("_wait_and_attach_cdp: found port %d – trying CDP …", port)
                    try:
                        self._browser = await self._playwright.chromium.connect_over_cdp(
                            f"http://127.0.0.1:{port}"
                        )
                        self._context = (
                            self._browser.contexts[0]
                            if self._browser.contexts
                            else await self._browser.new_context()
                        )
                        self._owns_browser = False
                        log.info("_wait_and_attach_cdp: attached to existing browser on port %d", port)
                        return True
                    except Exception as exc:
                        log.debug("_wait_and_attach_cdp: connect attempt failed (%s), retrying …", exc)
            except Exception as exc:
                log.debug("_wait_and_attach_cdp: error reading port file (%s), retrying …", exc)
            await asyncio.sleep(poll_interval)
        log.warning("_wait_and_attach_cdp: timed out after %.0fs", timeout)
        return False

    async def _close_non_teams_tabs(self) -> None:
        """Close all blank / stale tabs that are not a Teams page.

        Called after launching or reattaching to the persistent browser so that
        blank tabs created automatically by the browser (or left over from a
        previous session) are cleaned up.  Only tabs whose URL is in the
        well-known blank-URL set are closed; any other non-Teams URL is left
        alone to avoid accidentally closing something the user cares about.
        """
        if not self._context:
            return
        for page in list(self._context.pages):
            if page.is_closed():
                continue
            if "teams.microsoft.com" not in page.url and page.url in self._BLANK_URLS:
                log.info("start: closing blank tab (url=%r)", page.url)
                try:
                    await page.close()
                except Exception as exc:
                    log.debug("start: could not close blank tab: %s", exc)

    async def _acquire_page(self, url: str, *, owns_page_out: list[bool]) -> Any:
        """Return a page suitable for Teams interaction.

        Strategy (in order):

        1. Reuse an existing Teams tab — no navigation needed.
        2. Reuse an existing blank/newtab — navigate it to *url* instead of
           spawning a second blank tab alongside it.
        3. Open a brand-new page and navigate it to *url*.

        *owns_page_out* is a single-element list used as an out-parameter; it
        is set to ``[True]`` when the caller should close the page after use,
        and ``[False]`` when an already-open Teams page was reused.
        """
        pages = [p for p in self._context.pages if not p.is_closed()]  # type: ignore[union-attr]

        for p in pages:
            if "teams.microsoft.com" in p.url:
                log.info("_acquire_page: reusing existing Teams page (url=%s)", p.url)
                owns_page_out[0] = False
                return p

        for p in pages:
            if p.url in self._BLANK_URLS:
                log.info("_acquire_page: reusing blank tab, navigating to %s", url)
                owns_page_out[0] = True
                await goto_channel(p, url, timeout_ms=self._nav_timeout_ms)
                return p

        log.info("_acquire_page: opening new page, url=%s", url)
        p = await self._context.new_page()  # type: ignore[union-attr]
        owns_page_out[0] = True
        await goto_channel(p, url, timeout_ms=self._nav_timeout_ms)
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

