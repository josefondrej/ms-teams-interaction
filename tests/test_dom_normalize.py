"""Unit tests for :func:`teams_interaction.dom.normalize_teams_url`."""

from __future__ import annotations

import pytest

from teams_interaction.dom import normalize_teams_url


def test_normalize_teams_url_https() -> None:
    """Valid Teams HTTPS URL is returned unchanged."""
    u = "https://teams.microsoft.com/l/channel/abc"
    assert normalize_teams_url(u) == u


def test_normalize_teams_url_strips_whitespace() -> None:
    """Surrounding whitespace is stripped from the URL."""
    assert normalize_teams_url("  https://teams.microsoft.com/x  ") == "https://teams.microsoft.com/x"


def test_normalize_teams_url_requires_teams_host() -> None:
    """Non-Teams host raises ``ValueError``."""
    with pytest.raises(ValueError, match="teams.microsoft.com"):
        normalize_teams_url("https://slack.com/app")


def test_normalize_teams_url_requires_http_scheme() -> None:
    """URL without an ``http`` scheme raises ``ValueError``."""
    with pytest.raises(ValueError, match="https://"):
        normalize_teams_url("teams.microsoft.com/foo")
