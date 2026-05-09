from __future__ import annotations

import asyncio

import typer

from teams_interaction.client import TeamsClient
from teams_interaction.types import ChannelMessage

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("open")
def open_channel_cmd(url: str = typer.Option(..., "--url", help="teams.microsoft.com channel URL")) -> None:
    """Open Teams in a persistent browser profile (sign in if needed)."""

    async def run() -> None:
        client = TeamsClient()
        await client.start()
        await client.open_channel(url)
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
    url: str = typer.Option(..., "--url"),
    text: str = typer.Option(..., "--text"),
) -> None:
    """Send a plain-text message to a channel."""

    async def run() -> None:
        client = TeamsClient()
        await client.start()
        try:
            await client.send_message(url, text)
        finally:
            await client.close()

    asyncio.run(run())


@app.command()
def watch(
    url: str = typer.Option(..., "--url"),
    interval: float = typer.Option(2.0, "--interval", help="Poll seconds"),
) -> None:
    """Print new channel messages as they appear."""

    async def run() -> None:
        client = TeamsClient()
        await client.start()

        async def on_message(m: ChannelMessage) -> None:
            who = m.author or "?"
            typer.echo(f"{who}\t{m.text.replace(chr(10), ' ')}")

        client.watch_channel(url, on_message, poll_interval=interval)
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


if __name__ == "__main__":
    app()
