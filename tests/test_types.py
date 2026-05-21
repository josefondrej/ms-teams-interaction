"""Unit tests for :mod:`teams_interaction.types`."""

from __future__ import annotations

import pytest

from teams_interaction.types import ChannelRef


def test_channel_ref_from_url_accepts_teams_link() -> None:
    """Valid ``teams.microsoft.com`` URL is stored unchanged."""
    url = "https://teams.microsoft.com/l/channel/foo"
    ref = ChannelRef.from_url(url)
    assert ref.url == url


def test_channel_ref_from_url_strips_whitespace() -> None:
    """Surrounding whitespace is stripped before storing the URL."""
    ref = ChannelRef.from_url("  https://teams.microsoft.com/x  ")
    assert ref.url == "https://teams.microsoft.com/x"


def test_channel_ref_from_url_rejects_non_teams_host() -> None:
    """Non-Teams URLs raise ``ValueError``."""
    with pytest.raises(ValueError, match="teams.microsoft.com"):
        ChannelRef.from_url("https://example.com/phishing")
