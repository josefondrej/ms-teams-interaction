"""Unit tests for :meth:`teams_interaction.client.TeamsClient._watch_channel_loop`."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from teams_interaction.client import TeamsClient
from teams_interaction.types import ChannelMessage


class _FakePage:
    """Minimal stub for a Playwright page used in watch-loop tests."""

    def __init__(self, url: str = "") -> None:
        self.url = url
        self.closed = False

    def is_closed(self) -> bool:
        """Return whether the page has been closed."""
        return self.closed

    async def close(self) -> None:
        """Mark the page as closed."""
        self.closed = True


class _FakeContext:
    """Minimal stub for a Playwright browser context used in watch-loop tests."""

    def __init__(self, page: _FakePage, existing_pages: list[_FakePage] | None = None) -> None:
        self._page = page
        self.pages: list[_FakePage] = existing_pages or []

    async def new_page(self) -> _FakePage:
        """Return the pre-configured fake page."""
        return self._page


def _make_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``goto_channel`` and ``switch_to_channel`` with async no-ops."""

    async def fake_noop(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("teams_interaction.client.goto_channel", fake_noop)
    monkeypatch.setattr("teams_interaction.client.switch_to_channel", fake_noop)


@pytest.mark.asyncio
async def test_watch_channel_does_not_emit_initial_batch_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``include_existing=False`` the priming batch is never forwarded to the handler."""
    page = _FakePage()
    client = TeamsClient()
    client._context = _FakeContext(page)  # type: ignore[assignment]

    msg = ChannelMessage(stable_id="mid:1", text="hello", author="A")
    scrape_calls = 0

    async def fake_scrape(_: Any) -> list[ChannelMessage]:
        nonlocal scrape_calls
        scrape_calls += 1
        if scrape_calls == 1:
            return [msg]
        raise asyncio.CancelledError

    _make_helpers(monkeypatch)
    monkeypatch.setattr("teams_interaction.client.scrape_top_level_messages", fake_scrape)

    seen: list[ChannelMessage] = []

    async def on_message(m: ChannelMessage) -> None:
        seen.append(m)

    with pytest.raises(asyncio.CancelledError):
        await client._watch_channel_loop(None, on_message, 0.0, "General", False)

    assert seen == []
    assert page.closed


@pytest.mark.asyncio
async def test_watch_channel_can_emit_initial_batch_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``include_existing=True`` the priming batch is forwarded to the handler."""
    page = _FakePage()
    client = TeamsClient()
    client._context = _FakeContext(page)  # type: ignore[assignment]

    msg = ChannelMessage(stable_id="mid:1", text="hello", author="A")
    scrape_calls = 0

    async def fake_scrape(_: Any) -> list[ChannelMessage]:
        nonlocal scrape_calls
        scrape_calls += 1
        if scrape_calls == 1:
            return [msg]
        raise asyncio.CancelledError

    _make_helpers(monkeypatch)
    monkeypatch.setattr("teams_interaction.client.scrape_top_level_messages", fake_scrape)

    seen: list[ChannelMessage] = []

    async def on_message(m: ChannelMessage) -> None:
        seen.append(m)

    with pytest.raises(asyncio.CancelledError):
        await client._watch_channel_loop(None, on_message, 0.0, "General", True)

    assert [m.stable_id for m in seen] == ["mid:1"]
    assert page.closed


@pytest.mark.asyncio
async def test_watch_channel_emits_messages_that_appear_after_empty_prime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When priming returns nothing, messages that arrive later are treated as new."""
    page = _FakePage()
    client = TeamsClient()
    client._context = _FakeContext(page)  # type: ignore[assignment]

    msg = ChannelMessage(stable_id="mid:late", text="late history", author="A")
    scrape_calls = 0

    async def fake_scrape(_: Any) -> list[ChannelMessage]:
        nonlocal scrape_calls
        scrape_calls += 1
        if scrape_calls == 1:
            return []  # nothing visible at prime time
        if scrape_calls == 2:
            return [msg]  # appeared by first poll
        raise asyncio.CancelledError

    _make_helpers(monkeypatch)
    monkeypatch.setattr("teams_interaction.client.scrape_top_level_messages", fake_scrape)

    seen: list[ChannelMessage] = []

    async def on_message(m: ChannelMessage) -> None:
        seen.append(m)

    with pytest.raises(asyncio.CancelledError):
        await client._watch_channel_loop(None, on_message, 0.0, "General", True)

    assert [m.stable_id for m in seen] == ["mid:late"]
    assert page.closed


@pytest.mark.asyncio
async def test_watch_channel_prime_error_is_swallowed_and_polling_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the prime scrape throws, watch starts with an empty seen-set and polls on."""
    page = _FakePage()
    client = TeamsClient()
    client._context = _FakeContext(page)  # type: ignore[assignment]

    msg = ChannelMessage(stable_id="mid:1", text="hello", author="A")
    scrape_calls = 0

    async def fake_scrape(_: Any) -> list[ChannelMessage]:
        nonlocal scrape_calls
        scrape_calls += 1
        if scrape_calls == 1:
            raise RuntimeError("DOM not ready")  # prime fails
        if scrape_calls == 2:
            return [msg]  # first poll succeeds
        raise asyncio.CancelledError

    _make_helpers(monkeypatch)
    monkeypatch.setattr("teams_interaction.client.scrape_top_level_messages", fake_scrape)

    seen: list[ChannelMessage] = []

    async def on_message(m: ChannelMessage) -> None:
        seen.append(m)

    with pytest.raises(asyncio.CancelledError):
        await client._watch_channel_loop(None, on_message, 0.0, "General", False)

    # Message from first poll is emitted because seen-set was empty after failed prime
    assert [m.stable_id for m in seen] == ["mid:1"]
    assert page.closed


@pytest.mark.asyncio
async def test_watch_reuses_existing_teams_page_and_does_not_close_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a Teams page is already open (e.g. 'chat' is running), watch reuses it
    and does NOT close it when done."""
    # Simulate a pre-existing Teams tab (as opened by 'chat')
    teams_page = _FakePage(url="https://teams.microsoft.com/v2/#/channel/General")
    new_page = _FakePage()  # should never be opened

    client = TeamsClient()
    client._context = _FakeContext(new_page, existing_pages=[teams_page])  # type: ignore[assignment]
    client._my_pages = {teams_page}  # tell the client it owns this page

    msg = ChannelMessage(stable_id="mid:1", text="hello", author="A")
    scrape_calls = 0

    async def fake_scrape(p: Any) -> list[ChannelMessage]:
        nonlocal scrape_calls
        assert p is teams_page, "watch must scrape the reused page, not a new one"
        scrape_calls += 1
        if scrape_calls == 1:
            return [msg]
        raise asyncio.CancelledError

    _make_helpers(monkeypatch)
    monkeypatch.setattr("teams_interaction.client.scrape_top_level_messages", fake_scrape)

    seen: list[ChannelMessage] = []

    async def on_message(m: ChannelMessage) -> None:
        seen.append(m)

    with pytest.raises(asyncio.CancelledError):
        await client._watch_channel_loop(None, on_message, 0.0, "General", False)

    # Pre-existing Teams page must NOT be closed — chat is still using it
    assert not teams_page.closed
    # new_page() must never have been called
    assert not new_page.closed
