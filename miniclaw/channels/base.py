"""Channel ABC and data classes for message transport."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
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

    async def send_stream(self, stream: AsyncIterator[str]) -> None:
        """Send a streamed response. Default: buffer and call send()."""
        chunks: list[str] = []
        async for chunk in stream:
            chunks.append(chunk)
        if chunks:
            await self.send(SendMessage(text="".join(chunks)))

    def command_descriptions(self) -> list[dict]:
        """Return command descriptions for /help. Default: empty."""
        return []

    def log_handler(self) -> logging.Handler | None:
        """Return this channel's log forwarding handler, or None."""
        return None

    def set_log_level(self, level: int) -> None:
        """Adjust forwarding level. Calls adjust_root_level() after change."""
        handler = self.log_handler()
        if handler is not None:
            handler.setLevel(level)
            from miniclaw.log import adjust_root_level
            adjust_root_level()
