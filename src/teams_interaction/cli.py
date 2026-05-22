"""Command-line interface for ms-teams-interaction.

Exposes five Typer commands:

* ``open``    – open Teams in a persistent browser window.
* ``send``    – send a plain-text message to a channel.
* ``watch``   – stream new channel messages to stdout.
* ``inspect`` – dump message-DOM diagnostics as JSON.
* ``chat``    – interactively type and send messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import typer

from teams_interaction.client import TeamsClient
from teams_interaction.types import ChannelMessage

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = logging.getLogger(__name__)

_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_AUTHOR_COLORS = [
    typer.colors.CYAN,
    typer.colors.MAGENTA,
    typer.colors.YELLOW,
    typer.colors.GREEN,
    typer.colors.RED,
    typer.colors.BLUE,
    typer.colors.BRIGHT_CYAN,
    typer.colors.BRIGHT_MAGENTA,
    typer.colors.BRIGHT_YELLOW,
    typer.colors.BRIGHT_GREEN,
    typer.colors.BRIGHT_RED,
    typer.colors.BRIGHT_BLUE,
]


def _get_color_for_author(author: str) -> str:
    """Return a consistent Typer color for a given author name."""
    if not author:
        return typer.colors.WHITE
    # Use a stable hash to keep colors consistent across runs
    h = hashlib.md5(author.encode("utf-8")).hexdigest()
    idx = int(h, 16) % len(_AUTHOR_COLORS)
    return _AUTHOR_COLORS[idx]


def _setup_logging(level: int = logging.WARNING) -> None:
    """Configure a ``StreamHandler`` on ``stderr`` for the package logger.

    Args:
        level: The numeric logging level (e.g. ``logging.DEBUG``).
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"))
    for name in ("teams_interaction",):
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers.clear()
        lg.addHandler(handler)


def _resolve_level(log_level: str, verbose: bool) -> int:
    """Resolve the effective logging level from CLI flags.

    Args:
        log_level: String level name (e.g. ``"WARNING"``).
        verbose: When ``True``, returns ``logging.DEBUG`` regardless of
            *log_level*.

    Returns:
        An integer logging level suitable for :func:`logging.Logger.setLevel`.
    """
    if verbose:
        return logging.DEBUG
    return getattr(logging, log_level.upper(), logging.WARNING)


@app.command("open")
def open_channel_cmd(
    url: str | None = typer.Option(None, "--url", help="Teams URL (defaults to https://teams.microsoft.com/v2/)"),
    channel: str | None = typer.Option(None, "--channel", help="Visible channel name to select after load"),
    no_persistent: bool = typer.Option(
        False, "--no-persistent", help="Launch a fresh browser (no saved profile; you will need to sign in)"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable DEBUG logging (shorthand for --log-level DEBUG)"
    ),
    log_level: str = typer.Option(
        "WARNING", "--log-level", "-l", help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL", show_choices=True
    ),
) -> None:
    """Open Teams in a persistent browser profile (sign in if needed)."""
    _setup_logging(_resolve_level(log_level, verbose))

    async def run() -> None:
        client = TeamsClient(persistent=not no_persistent)
        await client.start()
        await client.open_channel(url, channel_name=channel)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
        finally:
            await client.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


@app.command()
def send(
    url: str | None = typer.Option(None, "--url", help="Teams URL (defaults to https://teams.microsoft.com/v2/)"),
    channel: str | None = typer.Option(None, "--channel", help="Visible channel name to select before sending"),
    text: str = typer.Option(..., "--text"),
    no_persistent: bool = typer.Option(
        False, "--no-persistent", help="Launch a fresh browser (no saved profile; you will need to sign in)"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable DEBUG logging (shorthand for --log-level DEBUG)"
    ),
    log_level: str = typer.Option(
        "WARNING", "--log-level", "-l", help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL"
    ),
) -> None:
    """Send a plain-text message to a channel."""
    _setup_logging(_resolve_level(log_level, verbose))

    async def run() -> None:
        client = TeamsClient(persistent=not no_persistent)
        await client.start()
        try:
            await client.send_message(url, text, channel_name=channel)
        finally:
            await client.close()

    asyncio.run(run())


@app.command()
def watch(
    channel: str = typer.Option(..., "--channel", help="Visible channel name to watch"),
    include_existing: bool = typer.Option(
        False,
        "--include-existing",
        help="Emit currently visible messages before polling for new ones",
    ),
    interval: float = typer.Option(0.25, "--interval", help="Poll interval in seconds"),
    no_persistent: bool = typer.Option(
        False, "--no-persistent", help="Launch a fresh browser (no saved profile; you will need to sign in)"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable DEBUG logging (shorthand for --log-level DEBUG)"
    ),
    log_level: str = typer.Option(
        "WARNING", "--log-level", "-l", help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL"
    ),
) -> None:
    """Print new channel messages as they appear (navigates to Teams and selects the channel by name)."""
    _setup_logging(_resolve_level(log_level, verbose))

    async def run() -> None:
        client = TeamsClient(persistent=not no_persistent)
        await client.start()

        async def on_message(message: ChannelMessage) -> None:
            who = message.author or "?"
            ts = datetime.now().strftime("%H:%M:%S")
            body = message.text.replace("\n", " ").strip()
            author_styled = typer.style(who, fg=_get_color_for_author(who), bold=True)
            ts_styled = typer.style(f"[{ts}]", fg=typer.colors.BRIGHT_BLACK)
            typer.echo(f"{ts_styled} {author_styled}: {body}")

        task = client.watch_channel(
            None,
            on_message,
            channel_name=channel,
            include_existing=include_existing,
            poll_interval=interval,
        )
        try:
            # Await both the sentinel future and the watch task so that
            # exceptions from the task (e.g. channel not found) surface immediately.
            await asyncio.wait(
                [task, asyncio.ensure_future(asyncio.sleep(1e9))],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # If the watch task finished first it likely raised – retrieve the exception.
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    log.error("watch task exited with error: %s", exc, exc_info=exc)
                    raise SystemExit(1)
        except asyncio.CancelledError:
            pass
        finally:
            await client.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


@app.command()
def inspect(
    channel: str = typer.Option(..., "--channel", help="Visible channel/chat name to inspect"),
    url: str | None = typer.Option(None, "--url", help="Teams URL (defaults to https://teams.microsoft.com/v2/)"),
    samples: int = typer.Option(5, "--samples", min=1, max=20, help="How many sample DOM nodes/messages to print"),
    out: Path | None = typer.Option(None, "--out", help="Optional path to write the JSON snapshot"),
    no_persistent: bool = typer.Option(
        False, "--no-persistent", help="Launch a fresh browser (no saved profile; you will need to sign in)"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable DEBUG logging (shorthand for --log-level DEBUG)"
    ),
    log_level: str = typer.Option(
        "WARNING", "--log-level", "-l", help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL"
    ),
) -> None:
    """Open Teams, switch to a chat/channel, and dump message-related DOM diagnostics as JSON."""
    _setup_logging(_resolve_level(log_level, verbose))

    async def run() -> None:
        client = TeamsClient(persistent=not no_persistent)
        await client.start()
        try:
            data = await client.inspect_channel(url, channel_name=channel, max_samples=samples)
            rendered = json.dumps(data, indent=2, ensure_ascii=False)
            if out is not None:
                out.write_text(rendered + "\n", encoding="utf-8")
                typer.echo(f"Wrote DOM snapshot to {out}")
            typer.echo(rendered)
        finally:
            await client.close()

    asyncio.run(run())


@app.command()
def chat(
    channel: str = typer.Option(..., "--channel", help="Visible channel/chat name to open"),
    url: str | None = typer.Option(None, "--url", help="Teams URL (defaults to https://teams.microsoft.com/v2/)"),
    no_persistent: bool = typer.Option(
        False, "--no-persistent", help="Launch a fresh browser (no saved profile; you will need to sign in)"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable DEBUG logging (shorthand for --log-level DEBUG)"
    ),
    log_level: str = typer.Option(
        "WARNING", "--log-level", "-l", help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL"
    ),
) -> None:
    """Interactively chat in a Teams channel: type messages to send."""
    _setup_logging(_resolve_level(log_level, verbose))

    async def run() -> None:
        client = TeamsClient(persistent=not no_persistent)
        await client.start()

        # Open the channel on a persistent page that we keep alive
        await client.open_channel(url, channel_name=channel)

        # Retrieve the Teams page that open_channel used (reused or newly opened)
        page = next(
            (
                page
                for page in reversed(client._context.pages)
                if not page.is_closed() and "teams.microsoft.com" in page.url
            ),
            client._context.pages[-1],
        )

        from teams_interaction.dom import send_plain_text

        typer.echo(
            typer.style(
                f"Chatting in '{channel}'. Type a message and press Enter to send. Ctrl+C to quit.",
                fg=typer.colors.GREEN,
            )
        )

        loop = asyncio.get_event_loop()

        def _read_line() -> str:
            sys.stdout.write(">>> ")
            sys.stdout.flush()
            return sys.stdin.readline()

        input_queue: asyncio.Queue[str] = asyncio.Queue()

        async def stdin_reader() -> None:
            """Read stdin lines in a thread and push them onto the input queue."""
            while True:
                line = await loop.run_in_executor(None, _read_line)
                if not line:  # EOF
                    await input_queue.put("")
                    return
                await input_queue.put(line.rstrip("\n"))

        async def sender() -> None:
            """Read from the input queue and send messages to Teams."""
            while True:
                text = await input_queue.get()
                if text == "":
                    # EOF – stop
                    raise SystemExit(0)
                if text.strip():
                    try:
                        await send_plain_text(page, text)
                        log.debug("chat: sent: %r", text)
                    except Exception as exc:
                        typer.echo(typer.style(f"[error sending: {exc}]", fg=typer.colors.RED), err=True)

        stdin_task = asyncio.create_task(stdin_reader())
        send_task = asyncio.create_task(sender())

        try:
            done, pending = await asyncio.wait(
                [stdin_task, send_task],
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for completed_task in done:
                exc = completed_task.exception() if not completed_task.cancelled() else None
                if exc and not isinstance(exc, SystemExit):
                    log.error("chat task error: %s", exc, exc_info=exc)
        except asyncio.CancelledError:
            pass
        finally:
            for task in (stdin_task, send_task):
                task.cancel()
            await asyncio.gather(stdin_task, send_task, return_exceptions=True)
            await client.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    app()
