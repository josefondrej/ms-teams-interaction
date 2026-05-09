from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChannelRef:
    """Channel identity for navigation (prefer full URL from Teams)."""

    url: str

    @staticmethod
    def from_url(url: str) -> "ChannelRef":
        u = url.strip()
        if not u.startswith("https://teams.microsoft.com"):
            raise ValueError("Expected a teams.microsoft.com URL")
        return ChannelRef(url=u)


@dataclass
class ChannelMessage:
    """A single top-level post in the channel feed (best-effort DOM parse)."""

    stable_id: str
    text: str
    author: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
