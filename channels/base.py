"""Channel ABC and data classes for message transport."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Awaitable


@dataclass
class ChannelMessage:
    """An incoming message from a channel."""

    text: str
    sender_id: str
    channel_id: str = ""
    message_id: str = ""


@dataclass
class SendMessage:
    """An outgoing message to a channel."""

    text: str
    channel_id: str = ""
    reply_to: str = ""


class Channel(ABC):
    """Abstract base class for message channels."""

    @abstractmethod
    async def listen(
        self, callback: Callable[[ChannelMessage], Awaitable[None]]
    ) -> None:
        """Long-running listener that calls callback on each incoming message."""
        ...

    @abstractmethod
    async def send(self, message: SendMessage) -> None:
        """Send a message to the channel."""
        ...
