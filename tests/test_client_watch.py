from __future__ import annotations

import asyncio
from typing import Any

import pytest

from teams_interaction.client import TeamsClient
from teams_interaction.types import ChannelMessage


class _FakePage:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def new_page(self) -> _FakePage:
        return self._page


@pytest.mark.asyncio
async def test_watch_channel_does_not_emit_initial_batch_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
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

    async def fake_noop(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("teams_interaction.client.goto_channel", fake_noop)
    monkeypatch.setattr("teams_interaction.client.switch_to_channel", fake_noop)
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

    async def fake_noop(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("teams_interaction.client.goto_channel", fake_noop)
    monkeypatch.setattr("teams_interaction.client.switch_to_channel", fake_noop)
    monkeypatch.setattr("teams_interaction.client.scrape_top_level_messages", fake_scrape)

    seen: list[ChannelMessage] = []

    async def on_message(m: ChannelMessage) -> None:
        seen.append(m)

    with pytest.raises(asyncio.CancelledError):
        await client._watch_channel_loop(None, on_message, 0.0, "General", True)

    assert [m.stable_id for m in seen] == ["mid:1"]
    assert page.closed


@pytest.mark.asyncio
async def test_watch_channel_include_existing_waits_for_delayed_initial_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage()
    client = TeamsClient()
    client._context = _FakeContext(page)  # type: ignore[assignment]

    msg = ChannelMessage(stable_id="mid:late", text="late history", author="A")
    scrape_calls = 0

    async def fake_scrape(_: Any) -> list[ChannelMessage]:
        nonlocal scrape_calls
        scrape_calls += 1
        if scrape_calls == 1:
            return []
        if scrape_calls == 2:
            return [msg]
        raise asyncio.CancelledError

    async def fake_noop(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("teams_interaction.client.goto_channel", fake_noop)
    monkeypatch.setattr("teams_interaction.client.switch_to_channel", fake_noop)
    monkeypatch.setattr("teams_interaction.client.scrape_top_level_messages", fake_scrape)

    seen: list[ChannelMessage] = []

    async def on_message(m: ChannelMessage) -> None:
        seen.append(m)

    with pytest.raises(asyncio.CancelledError):
        await client._watch_channel_loop(None, on_message, 0.0, "General", True)

    assert [m.stable_id for m in seen] == ["mid:late"]
    assert page.closed


