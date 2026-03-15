"""Session — the central entity owning conversation state."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from os import urandom
from typing import TYPE_CHECKING

from miniclaw.providers.base import ChatMessage

from miniclaw2.agent.config import AgentConfig
from miniclaw2.cancellation import CancelledError, CancellationToken
from miniclaw2.types import (
    AgentEvent,
    HistoryUpdate,
    InterruptedEvent,
    SessionControl,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from miniclaw2.agent.protocol import AgentProtocol
    from miniclaw2.channels.base import Channel

logger = logging.getLogger(__name__)


def generate_session_id() -> str:
    """Generate a timestamped session ID with random suffix."""
    ts = datetime.now(timezone.utc)
    token = sha256(urandom(16)).hexdigest()[:6]
    return ts.strftime("%Y%m%d_%H%M%S") + "_" + token


@dataclass
class SessionMetadata:
    """Immutable and mutable metadata for a session."""

    created_at: str = ""
    updated_at: str = ""
    name: str | None = None
    forked_from: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.created_at:
            now = datetime.now(timezone.utc).isoformat()
            self.created_at = now
            self.updated_at = now

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


@dataclass
class ObserverBinding:
    """A read-only channel attached to a session."""

    channel: Channel
    queue: asyncio.Queue[AgentEvent]
    task: asyncio.Task | None = None


class Session:
    """The central entity owning conversation state.

    Session owns: history, agent_config, metadata, status.
    Session borrows: agent (bound by Runtime), primary_channel (bound by Listener).
    Session does NOT own: Channel lifecycle, Agent lifecycle, persistence.
    """

    def __init__(
        self,
        id: str,
        agent: AgentProtocol,
        agent_config: AgentConfig,
        metadata: SessionMetadata | None = None,
        history: list[ChatMessage] | None = None,
    ) -> None:
        self.id = id
        self.agent = agent
        self.agent_config = agent_config
        self.metadata = metadata or SessionMetadata()
        self.history: list[ChatMessage] = history or []

        self.primary_channel: Channel | None = None
        self.observers: list[ObserverBinding] = []

        self._lock = asyncio.Lock()
        self._current_token: CancellationToken | None = None
        self.status: str = "active"  # active | paused | archived

    # --- Core: process a message ---

    async def process(self, text: str) -> AsyncIterator[AgentEvent]:
        """Process user input through the bound agent.

        Yields: TextDelta, ActivityEvent, InteractionRequest, InterruptedEvent.
        Consumes internally: HistoryUpdate, SessionControl.
        """
        token = CancellationToken()
        self._current_token = token

        try:
            async with self._lock:
                pending_text: str | None = text

                # Restart loop: handles SessionControl("plan_execute")
                while pending_text is not None:
                    restart_text: str | None = None

                    async for event in self.agent.process(
                        pending_text,
                        list(self.history),
                        self.agent_config,
                        token,
                    ):
                        if isinstance(event, HistoryUpdate):
                            self.history = event.history
                            self.metadata.touch()

                        elif isinstance(event, SessionControl):
                            if event.action == "plan_execute":
                                logger.info(
                                    "SessionControl(plan_execute) — clearing history "
                                    "and restarting (session=%s)",
                                    self.id,
                                )
                                self.history = []
                                await self.agent.reset()
                                restart_text = event.payload.get(
                                    "plan_content", "Execute the plan."
                                )
                                break
                            else:
                                logger.warning(
                                    "Unknown SessionControl action: %s",
                                    event.action,
                                )

                        else:
                            # Forward to caller + broadcast to observers
                            await self._broadcast(event)
                            yield event

                    pending_text = restart_text

        except CancelledError:
            event = InterruptedEvent(partial_history=list(self.history))
            await self._broadcast(event)
            yield event

        finally:
            self._current_token = None

    # --- Interrupt ---

    def interrupt(self) -> None:
        """Cancel the current in-progress processing."""
        if self._current_token is not None:
            self._current_token.cancel()

    # --- Observer management ---

    async def _broadcast(self, event: AgentEvent) -> None:
        """Push event to all observer channels."""
        for binding in self.observers:
            try:
                binding.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "Observer queue full for session %s, dropping event",
                    self.id,
                )
            except Exception:
                pass  # observer failure doesn't affect primary

    def attach_observer(self, channel: Channel) -> ObserverBinding:
        """Attach a read-only observer channel."""
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=1000)

        async def _observer_loop() -> None:
            # Replay history
            await channel.replay(self.history)
            # Stream live events
            await channel.on_observe(_queue_iter(queue))

        task = asyncio.create_task(_observer_loop())
        binding = ObserverBinding(channel=channel, queue=queue, task=task)
        self.observers.append(binding)
        return binding

    def detach_observer(self, channel: Channel) -> None:
        """Remove an observer channel."""
        for binding in self.observers:
            if binding.channel is channel:
                if binding.task is not None:
                    binding.task.cancel()
                self.observers.remove(binding)
                break

    # --- State management ---

    def clear_history(self) -> int:
        """Clear conversation history. Returns number of messages removed."""
        count = len(self.history)
        self.history = []
        return count

    def bind_primary(self, channel: Channel) -> None:
        """Bind a primary channel (can send input)."""
        self.primary_channel = channel


async def _queue_iter(queue: asyncio.Queue[AgentEvent]) -> AsyncIterator[AgentEvent]:
    """Convert an asyncio.Queue into an AsyncIterator."""
    sentinel = object()
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
            yield event
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
