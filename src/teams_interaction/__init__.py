"""Browser-only Teams (M365) interaction — no Azure app registration."""

from teams_interaction.client import TeamsClient
from teams_interaction.types import ChannelMessage, ChannelRef

__all__ = ["TeamsClient", "ChannelMessage", "ChannelRef"]
