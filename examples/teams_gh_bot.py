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
import collections
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

# Number of recent messages kept as conversation context.
_CONTEXT_WINDOW = 20

# System prompt injected before the conversation history.
_SYSTEM_PROMPT = """\
You are a helpful assistant embedded in a Microsoft Teams channel.
You will be shown the last few messages from the conversation for context, \
followed by the latest message you must reply to.

Rules:
- Respond naturally and helpfully to the latest message.
- Keep replies concise and on-topic.
- The conversation history is for context only; only reply to the LATEST MESSAGE.
"""


def _strip_ansi(text: str) -> str:
    """Remove ANSI colour / cursor-control escape codes from *text*."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# GitHub Copilot CLI helper
# ---------------------------------------------------------------------------


async def ask_github_copilot(
    user_message: str,
    *,
    history: list[tuple[str, str]] | None = None,
    extra_args: list[str] | None = None,
) -> str:
    """Send *user_message* (with optional conversation history) to the GitHub
    Copilot CLI agent and return its reply.

    The prompt is structured as::

        <system instructions>

        [Conversation history]
        <author>: <text>
        …

        [Latest message]
        <author>: <text>

        ---
        Respond to the latest message, or output exactly "WAITING FOR RESPONSE"
        if no reply is needed.

    Args:
        user_message: The text received from Teams (formatted as ``"author: text"``).
        history: Optional list of ``(author, text)`` tuples representing the
            recent conversation context (oldest first, excluding the latest).
        extra_args: Optional extra flags appended to the command.

    Returns:
        The plain-text response string, or a human-readable error message.
    """
    # Build the enriched prompt.
    parts: list[str] = [_SYSTEM_PROMPT, ""]

    if history:
        parts.append("[Conversation history]")
        for author, text in history:
            parts.append(f"{author or 'Unknown'}: {text}")
        parts.append("")

    parts.append("[Latest message]")
    parts.append(user_message)
    parts.append("")

    full_prompt = "\n".join(parts)

    cmd = ["copilot", "-p", full_prompt, "-s", "--allow-all-tools"]
    if extra_args:
        cmd.extend(extra_args)

    log.debug("copilot command (prompt length=%d)", len(full_prompt))

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

    Each message is forwarded with a rolling window of the last
    *context_window* messages so the LLM can decide whether a reply is
    warranted.  If the LLM responds with exactly ``"WAITING FOR RESPONSE"``,
    no message is sent.

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
        context_window: Number of recent messages to include as context (default 20).
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
        context_window: int = _CONTEXT_WINDOW,
    ) -> None:
        self.channel_name = channel_name
        self.channel_url = channel_url
        self.extra_args = extra_args or []
        self.poll_interval = poll_interval
        self.bot_prefix = bot_prefix
        self.ignore_self = ignore_self
        self.bot_author = bot_author.strip().lower()
        self.context_window = context_window

        self._client = TeamsClient()
        # Queue of (author, text, history_snapshot) tuples for the background sender task.
        self._reply_queue: asyncio.Queue[tuple[str, str, list[tuple[str, str]]]] = asyncio.Queue()
        # Rolling buffer of (author, text) for the last *context_window* messages.
        self._history: collections.deque[tuple[str, str]] = collections.deque(maxlen=context_window)

    # ------------------------------------------------------------------

    async def on_message(self, msg: ChannelMessage) -> None:
        """Callback invoked by TeamsClient for every new channel message.

        Filters out bot-authored messages, updates the conversation history
        buffer, and enqueues the message for the background sender task.

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
            # Still record the bot's own reply in history so the LLM has context.
            self._history.append((author or "Bot", text))
            return

        # Secondary filter: skip by explicit author name when --bot-author is set.
        if self.ignore_self and self.bot_author and author.lower() == self.bot_author:
            log.debug("Skipping own message from %r", author)
            return

        print(f"\n📨  {author or '(unknown)'}: {text[:200]}", flush=True)

        # Snapshot history *before* appending the new message so the sender
        # gets [context] + [latest] as separate arguments.
        history_snapshot = list(self._history)
        self._history.append((author or "Unknown", text))

        # Non-blocking enqueue — the background sender task will pick this up.
        await self._reply_queue.put((author, text, history_snapshot))

    # ------------------------------------------------------------------

    async def _sender_worker(self) -> None:
        """Background task: drain the reply queue one item at a time.

        For each queued message:
        1. Builds a prompt with the conversation history context.
        2. Runs ``copilot -p "<prompt>" -s --allow-all-tools`` to get a response.
        3. If the response is exactly ``"WAITING FOR RESPONSE"``, skips sending.
        4. Otherwise sends the response back to Teams.

        Runs until cancelled (e.g. on Ctrl+C).
        """
        while True:
            author, text, history = await self._reply_queue.get()
            try:
                ctx_count = len(history)
                print(
                    f"   ↳ asking copilot (context: {ctx_count} message(s)) …",
                    flush=True,
                )
                latest_message = f"{author or 'Unknown'}: {text}"
                reply = await ask_github_copilot(
                    latest_message,
                    history=history,
                    extra_args=self.extra_args,
                )

                wrapped = "\n".join(
                    textwrap.fill(line, width=120) if len(line) > 120 else line for line in reply.splitlines()
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
        full_cmd = 'copilot -p "…" -s --allow-all-tools' + (f" {' '.join(self.extra_args)}" if self.extra_args else "")
        print(
            f"\n🚀  Teams-Copilot bot started.\n"
            f"    Channel  : {self.channel_name}\n"
            f"    Command  : {full_cmd}\n"
            f"    Polling  : every {self.poll_interval} s\n"
            f"    Context  : last {self.context_window} messages\n"
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
            'the GitHub Copilot CLI (`copilot -p "…" -s --allow-all-tools`).'
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
        "--context-window",
        type=int,
        default=_CONTEXT_WINDOW,
        dest="context_window",
        metavar="N",
        help=f"Number of recent messages sent as context to the LLM (default: {_CONTEXT_WINDOW})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
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
        context_window=args.context_window,
    )

    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
