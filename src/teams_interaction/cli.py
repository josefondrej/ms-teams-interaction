from __future__ import annotations

import asyncio
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


def _setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"))
    for name in ("teams_interaction",):
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers.clear()
        lg.addHandler(handler)


@app.command("open")
def open_channel_cmd(
    url: str | None = typer.Option(None, "--url", help="Teams URL (defaults to https://teams.microsoft.com/v2/)"),
    channel: str | None = typer.Option(None, "--channel", help="Visible channel name to select after load"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable DEBUG logging to stderr"),
) -> None:
    """Open Teams in a persistent browser profile (sign in if needed)."""
    _setup_logging(logging.DEBUG if verbose else logging.INFO)

    async def run() -> None:
        client = TeamsClient()
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable DEBUG logging to stderr"),
) -> None:
    """Send a plain-text message to a channel."""
    _setup_logging(logging.DEBUG if verbose else logging.INFO)

    async def run() -> None:
        client = TeamsClient()
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
    interval: float = typer.Option(2.0, "--interval", help="Poll seconds"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable DEBUG logging to stderr"),
) -> None:
    """Print new channel messages as they appear (navigates to Teams and selects the channel by name)."""
    _setup_logging(logging.DEBUG if verbose else logging.INFO)

    async def run() -> None:
        client = TeamsClient()
        await client.start()

        async def on_message(m: ChannelMessage) -> None:
            who = m.author or "?"
            ts = datetime.now().strftime("%H:%M:%S")
            body = m.text.replace("\n", " ").strip()
            author_styled = typer.style(who, fg=typer.colors.CYAN, bold=True)
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable DEBUG logging to stderr"),
) -> None:
    """Open Teams, switch to a chat/channel, and dump message-related DOM diagnostics as JSON."""
    _setup_logging(logging.DEBUG if verbose else logging.INFO)

    async def run() -> None:
        client = TeamsClient()
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


if __name__ == "__main__":
    app()
