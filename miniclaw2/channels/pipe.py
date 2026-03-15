"""Pipe — bidirectional channel connecting two sessions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from miniclaw.interactions import InteractionRequest, InteractionResponse
from miniclaw.providers.base import ChatMessage

from miniclaw2.channels.base import Channel
from miniclaw2.types import AgentEvent, InterruptedEvent, TextDelta

logger = logging.getLogger(__name__)

# Sentinel to signal pipe disconnection
POISON_PILL = object()


class PipeEnd(Channel):
    """One end of a bidirectional pipe between two sessions.

    Implements Channel: send_stream extracts final text and pushes
    it to the other end's inbox queue. The PipeDriver on the other
    side reads the inbox and calls session.process().
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._other: PipeEnd | None = None

    async def send_stream(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Consume agent event stream, forward final text to other end."""
        text_parts: list[str] = []
        async for event in stream:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, InteractionRequest):
                # Auto-resolve: no human on a pipe
                event.resolve(InteractionResponse(id=event.id, allow=True))
            elif isinstance(event, InterruptedEvent):
                logger.info("Pipe %s: processing interrupted", self.name)
            # ActivityEvents are silently consumed

        full_text = "".join(text_parts)
        if full_text and self._other is not None:
            await self._other._inbox.put(full_text)

    async def send(self, text: str) -> None:
        """Send a simple text message to the other end."""
        if self._other is not None:
            await self._other._inbox.put(text)

    async def listen(self) -> str | None:
        """Wait for the next message from the other end.

        Returns None on POISON_PILL (pipe disconnected).
        """
        item = await self._inbox.get()
        if item is POISON_PILL:
            return None
        return item

    def disconnect(self) -> None:
        """Send poison pill to signal disconnection."""
        self._inbox.put_nowait(POISON_PILL)


def create_pipe(name_a: str, name_b: str) -> tuple[PipeEnd, PipeEnd]:
    """Create a linked pair of PipeEnds."""
    a = PipeEnd(name_a)
    b = PipeEnd(name_b)
    a._other = b
    b._other = a
    return a, b
