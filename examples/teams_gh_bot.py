"""
teams_gh_bot.py — Bridge a Microsoft Teams chat with the GitHub Copilot CLI.

Each new message that arrives in the watched Teams channel/chat is forwarded
to the Copilot CLI agent via `copilot -p "<message>" -s --allow-all-tools`.
The reply is sent straight back to the same channel.

Requirements
------------
- ms-teams-interaction installed  (`pip install -e ".[cli]"` from the project root)
- GitHub Copilot CLI installed and on PATH  (`copilot --version` should work)
- Authenticated with Copilot  (`copilot login`)

Quick start
-----------
1.  Open Teams at least once so the persistent browser profile is seeded:

        teams-interaction open --channel "General"

2.  Run the bot:

        python examples/teams_gh_bot.py --channel "General"

        # DM / 1-on-1 chat
        python examples/teams_gh_bot.py --channel "Jane Doe"

Press Ctrl+C to stop.

Environment variables (all optional)
-------------------------------------
TEAMS_CHANNEL        Visible channel or DM name (overridden by --channel).
TEAMS_URL            Teams deep-link URL to navigate to first.
COPILOT_EXTRA_ARGS   Extra flags appended to every `copilot -p` call.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import textwrap

from teams_interaction import ChannelMessage, TeamsClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


log = logging.getLogger("teams_gh_bot")

# Regex that matches ANSI escape sequences so we can strip them from gh output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mABCDEFGHJKSTfnsuhl]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI colour / cursor-control escape codes from *text*."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# GitHub Copilot CLI helper
# ---------------------------------------------------------------------------

async def ask_github_copilot(user_message: str, *, extra_args: list[str] | None = None) -> str:
    """Send *user_message* to the GitHub Copilot CLI agent and return its reply.

    Runs::

        copilot -p "<user_message>" -s --allow-all-tools [extra_args…]

    - ``-p`` / ``--prompt``      — non-interactive prompt
    - ``-s`` / ``--silent``      — output only the agent response (no stats)
    - ``--allow-all-tools``      — required for fully non-interactive operation

    Args:
        user_message: The text received from Teams.
        extra_args: Optional extra flags appended to the command
            (e.g. ``["--model", "gpt-4o"]``).

    Returns:
        The plain-text response string, or a human-readable error message.
    """
    cmd = ["copilot", "-p", user_message, "-s", "--allow-all-tools"]
    if extra_args:
        cmd.extend(extra_args)

    log.debug("copilot command: %s", " ".join(cmd))

    env = {**os.environ, "NO_COLOR": "1"}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except FileNotFoundError:
        return (
            "⚠️ `copilot` binary not found. "
            "Install the GitHub Copilot CLI and make sure it is on your PATH, "
            "then run `copilot login`."
        )
    except asyncio.TimeoutError:
        return "⚠️ Copilot CLI timed out after 120 s."
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Unexpected error calling Copilot CLI: {exc}"

    if proc.returncode != 0:
        error_text = _strip_ansi(stderr.decode(errors="replace")).strip()
        log.warning("copilot failed (rc=%d): %s", proc.returncode, error_text)
        return f"⚠️ copilot error (exit {proc.returncode}): {error_text or '(no stderr)'}"

    reply = _strip_ansi(stdout.decode(errors="replace")).strip()
    return reply or "(no response)"


# ---------------------------------------------------------------------------
# Bot logic
# ---------------------------------------------------------------------------

class TeamsGhBot:
    """Watch a Teams channel and reply to every new message via the GitHub Copilot CLI.

    New messages are placed in an asyncio queue and processed by a dedicated
    background sender task, so the poll loop is never blocked while a reply is
    being composed or sent.

    Each message is forwarded as::

        copilot -p "<text>" -s --allow-all-tools [extra_args…]

    Args:
        channel_name: Visible Teams channel or DM contact name.
        channel_url: Optional Teams deep-link URL to navigate to first.
        extra_args: Extra flags appended to every ``copilot -p`` call
            (e.g. ``["--model", "gpt-4o"]``).
        poll_interval: Seconds between DOM-scrape passes.
        bot_prefix: String prepended to every reply so users know it is automated.
        ignore_self: When ``True`` (default), skip messages whose author matches
            *bot_author* to prevent the bot from responding to its own replies.
        bot_author: Display name that appears when *this* account sends a message.
    """

    def __init__(
        self,
        *,
        channel_name: str,
        channel_url: str | None = None,
        extra_args: list[str] | None = None,
        poll_interval: float = 1.0,
        bot_prefix: str = "🤖 ",
        ignore_self: bool = True,
        bot_author: str = "",
    ) -> None:
        self.channel_name = channel_name
        self.channel_url = channel_url
        self.extra_args = extra_args or []
        self.poll_interval = poll_interval
        self.bot_prefix = bot_prefix
        self.ignore_self = ignore_self
        self.bot_author = bot_author.strip().lower()

        self._client = TeamsClient()
        # Queue of (author, text) tuples for the background sender task.
        self._reply_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    # ------------------------------------------------------------------

    async def on_message(self, msg: ChannelMessage) -> None:
        """Callback invoked by TeamsClient for every new channel message.

        Filters out bot-authored messages and enqueues the text for the
        background sender task — does NOT await any I/O so the poll loop
        is never blocked.

        Args:
            msg: The newly detected Teams message.
        """
        author = (msg.author or "").strip()
        text = msg.text.strip()

        if not text:
            return

        # Skip messages that start with the bot prefix — these are our own
        # replies.  This is the most reliable self-filter: it works regardless
        # of how the author name appears in the DOM.
        if text.startswith(self.bot_prefix.strip()):
            log.debug("Skipping own reply (starts with bot prefix)")
            return

        # Secondary filter: skip by explicit author name when --bot-author is set.
        if self.ignore_self and self.bot_author and author.lower() == self.bot_author:
            log.debug("Skipping own message from %r", author)
            return

        print(f"\n📨  {author or '(unknown)'}: {text[:200]}", flush=True)
        # Non-blocking enqueue — the background sender task will pick this up.
        await self._reply_queue.put((author, text))

    # ------------------------------------------------------------------

    async def _sender_worker(self) -> None:
        """Background task: drain the reply queue one item at a time.

        For each queued message:
        1. Runs ``copilot -p "<text>" -s --allow-all-tools`` to get a response.
        2. Sends the response back to Teams via the reusable send page
           (no new browser window/tab is opened after the first call).

        Runs until cancelled (e.g. on Ctrl+C).
        """
        while True:
            author, text = await self._reply_queue.get()
            try:
                print("   ↳ copilot -p … -s --allow-all-tools", flush=True)
                reply = await ask_github_copilot(text, extra_args=self.extra_args)

                wrapped = "\n".join(
                    textwrap.fill(line, width=120) if len(line) > 120 else line
                    for line in reply.splitlines()
                )
                full_reply = f"{self.bot_prefix}{wrapped}"

                print(f"   ↳ sending reply ({len(full_reply)} chars) …", flush=True)
                log.debug("Reply text: %s", full_reply[:300])

                await self._client.send_message(
                    self.channel_url,
                    full_reply,
                    channel_name=self.channel_name,
                )
                print("   ✅ reply sent.", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Sender worker error: %s", exc, exc_info=True)
                print(f"   ❌ could not send reply: {exc}", flush=True)
            finally:
                self._reply_queue.task_done()

    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the browser, begin watching, and block until interrupted."""
        await self._client.start()
        full_cmd = "copilot -p \"…\" -s --allow-all-tools" + (
            f" {' '.join(self.extra_args)}" if self.extra_args else ""
        )
        print(
            f"\n🚀  Teams-Copilot bot started.\n"
            f"    Channel  : {self.channel_name}\n"
            f"    Command  : {full_cmd}\n"
            f"    Polling  : every {self.poll_interval} s\n"
            f"\nWaiting for messages … (Ctrl+C to stop)\n",
            flush=True,
        )

        # Background task that calls gh copilot and sends replies — completely
        # decoupled from the poll loop so the watcher is never blocked.
        sender_task = asyncio.create_task(self._sender_worker())

        self._client.watch_channel(
            self.channel_url,
            self.on_message,
            channel_name=self.channel_name,
            include_existing=False,
            poll_interval=self.poll_interval,
        )

        try:
            await asyncio.Future()  # run forever
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n⏹  Shutting down …", flush=True)
        finally:
            sender_task.cancel()
            await asyncio.gather(sender_task, return_exceptions=True)
            await self._client.close()
            print("👋  Browser closed.  Bye!", flush=True)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="teams_gh_bot",
        description=(
            "Watch a Teams channel and reply to new messages using "
            "the GitHub Copilot CLI (`copilot -p \"…\" -s --allow-all-tools`)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        epilog=textwrap.dedent(
            """\
            Examples:
              python examples/teams_gh_bot.py --channel "General"
              python examples/teams_gh_bot.py --channel "Jane Doe"
              python examples/teams_gh_bot.py --channel "General" --model gpt-4o
              python examples/teams_gh_bot.py --channel "General" --bot-author "Your Name"
            """
        ),
    )

    parser.add_argument(
        "--channel",
        default=os.environ.get("TEAMS_CHANNEL", ""),
        required=not os.environ.get("TEAMS_CHANNEL"),
        help="Visible Teams channel or DM contact name  [env: TEAMS_CHANNEL]",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("TEAMS_URL"),
        help="Optional Teams deep-link URL to navigate to first  [env: TEAMS_URL]",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="AI model to use (passed as --model <MODEL> to copilot)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="Polling interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--bot-prefix",
        default="🤖 ",
        dest="bot_prefix",
        help="String prepended to every bot reply (default: '🤖 ')",
    )
    parser.add_argument(
        "--bot-author",
        default="",
        dest="bot_author",
        help=(
            "Your Teams display name.  When set, the bot ignores messages "
            "sent under this name to avoid replying to itself."
        ),
    )
    parser.add_argument(
        "--no-ignore-self",
        action="store_false",
        dest="ignore_self",
        default=True,
        help="Disable self-message filtering (may cause reply loops)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging to stderr",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    extra_args: list[str] = []
    if args.model:
        # Guard against accidentally passing old --mode values (suggest/explain)
        # as the model name.
        _bad_mode_values = {"suggest", "explain", "interactive", "plan", "autopilot"}
        if args.model.lower() in _bad_mode_values:
            print(
                f"❌  '--model {args.model}' looks like a mode name, not a model name.\n"
                "    This flag sets the AI model (e.g. gpt-4o, claude-3-5-sonnet).\n"
                "    Remove '--model' to use the default model.",
                file=sys.stderr,
            )
            sys.exit(1)
        extra_args.extend(["--model", args.model])

    # Honour COPILOT_EXTRA_ARGS env var as well.
    if os.environ.get("COPILOT_EXTRA_ARGS"):
        extra_args.extend(os.environ["COPILOT_EXTRA_ARGS"].split())

    bot = TeamsGhBot(
        channel_name=args.channel,
        channel_url=args.url,
        extra_args=extra_args or None,
        poll_interval=args.interval,
        bot_prefix=args.bot_prefix,
        ignore_self=args.ignore_self,
        bot_author=args.bot_author,
    )

    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
