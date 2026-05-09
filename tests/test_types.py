from __future__ import annotations

import pytest

from teams_interaction.types import ChannelRef


def test_channel_ref_from_url_accepts_teams_link() -> None:
    url = "https://teams.microsoft.com/l/channel/foo"
    ref = ChannelRef.from_url(url)
    assert ref.url == url


def test_channel_ref_from_url_strips_whitespace() -> None:
    ref = ChannelRef.from_url("  https://teams.microsoft.com/x  ")
    assert ref.url == "https://teams.microsoft.com/x"


def test_channel_ref_from_url_rejects_non_teams_host() -> None:
    with pytest.raises(ValueError, match="teams.microsoft.com"):
        ChannelRef.from_url("https://example.com/phishing")
