"""Data types shared across the ms-teams-interaction package."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChannelRef:
    """Channel identity used for navigation (prefer a full URL from Teams).

    Attributes:
        url: The canonical ``https://teams.microsoft.com/…`` deep-link URL.
    """

    url: str

    @staticmethod
    def from_url(url: str) -> "ChannelRef":
        """Create a :class:`ChannelRef` from a Teams deep-link URL.

        Args:
            url: A ``teams.microsoft.com`` URL, optionally surrounded by
                whitespace.

        Returns:
            A new :class:`ChannelRef` with the stripped URL.

        Raises:
            ValueError: If *url* does not start with
                ``https://teams.microsoft.com``.
        """
        stripped_url = url.strip()
        if not stripped_url.startswith("https://teams.microsoft.com"):
            raise ValueError("Expected a teams.microsoft.com URL")
        return ChannelRef(url=stripped_url)


@dataclass
class ChannelMessage:
    """A single top-level post in the channel feed (best-effort DOM parse).

    Attributes:
        stable_id: Deduplication key; either ``mid:<data-mid>`` (preferred)
            or ``hash:<sha256-prefix>`` when no ``data-mid`` is present.
        text: Visible plain-text body of the message.
        author: Display name of the sender, or ``None`` when not found in
            the DOM.
        raw: Arbitrary scraping metadata (selector used, index, etc.).
    """

    stable_id: str
    text: str
    author: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
