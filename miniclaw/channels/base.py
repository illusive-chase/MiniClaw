"""Channel ABC — output endpoint for rendering agent events."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from miniclaw.interactions import InteractionRequest, InteractionResponse
from miniclaw.providers.base import ChatMessage

from miniclaw.types import AgentEvent

logger = logging.getLogger(__name__)


class Channel(ABC):
    """Output endpoint — renders agent events to a specific transport.

    Channels are agent-agnostic. They consume a typed AgentEvent stream
    and render/send it via their transport (terminal, HTTP API, pipe, etc.).
    """

    @abstractmethod
    async def send_stream(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Consume and render a full agent event stream.

        Handles:
          TextDelta           -> progressive rendering
          ActivityEvent       -> status display (footer, indicators)
          InteractionRequest  -> prompt user, call request.resolve()
          InterruptedEvent    -> display interruption notice
        """
        ...

    @abstractmethod
    async def send(self, text: str) -> None:
        """Send a simple text message (notifications, errors)."""
        ...

    async def on_observe(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Render events as a read-only observer.

        Default: same as send_stream but auto-resolve interactions.
        Override for custom observer UX.
        """

        async def _auto_resolve(source: AsyncIterator[AgentEvent]) -> AsyncIterator[AgentEvent]:
            async for event in source:
                if isinstance(event, InteractionRequest):
                    event.resolve(InteractionResponse(id=event.id, allow=True))
                else:
                    yield event

        await self.send_stream(_auto_resolve(stream))

    async def replay(self, history: list[ChatMessage]) -> None:
        """Replay past history when attaching/resuming. Optional."""
        pass

    def log_handler(self) -> logging.Handler | None:
        """Return this channel's log forwarding handler, or None."""
        return None
