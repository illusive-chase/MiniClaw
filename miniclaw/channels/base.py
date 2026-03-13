"""Channel ABC and data classes for message transport."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from miniclaw.gateway import Gateway


@dataclass
class ChannelMessage:
    """An incoming message from a channel."""

    text: str
    sender_id: str
    channel_id: str = ""
    message_id: str = ""
    command: str | None = None
    command_args: str | None = None


@dataclass
class SendMessage:
    """An outgoing message to a channel."""

    text: str
    channel_id: str = ""
    reply_to: str = ""


class Channel(ABC):
    """Abstract base class for message channels."""

    @abstractmethod
    async def start(self, gateway: Gateway) -> None:
        """Called by Gateway. Store gateway ref, allocate session, begin listening."""
        ...

    @abstractmethod
    async def send(self, message: SendMessage) -> None:
        """Send a message to the channel."""
        ...

    def replay_message(self, role: str, text: str) -> None:
        """Replay a historical message during session resume. Default: no-op."""
        pass

    def command_descriptions(self) -> list[dict]:
        """Return command descriptions for /help. Default: empty."""
        return []
