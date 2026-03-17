"""Session — the central entity owning conversation state."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from os import urandom
from typing import TYPE_CHECKING, Any, Callable

from miniclaw.agent.config import AgentConfig
from miniclaw.cancellation import CancellationToken, CancelledError
from miniclaw.providers.base import ChatMessage
from miniclaw.types import (
    AgentEvent,
    HistoryUpdate,
    InterruptedEvent,
    SessionControl,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from miniclaw.agent.protocol import AgentProtocol
    from miniclaw.channels.base import Channel

logger = logging.getLogger(__name__)


@dataclass
class InputMessage:
    """A message submitted to the session input queue."""

    text: str
    source: str = "user"  # "user" | "sub_agent" | "system"
    metadata: dict | None = None


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
        self.on_history_update: Callable[[], None] | None = None

        self._input_queue: asyncio.Queue[InputMessage] = asyncio.Queue()
        self.runtime_context: Any = None  # Set by Runtime; avoids circular import
        self.plugctx: Any = None  # Set by Runtime; PlugCtxManager instance

        self._lock = asyncio.Lock()
        self._current_token: CancellationToken | None = None
        self.status: str = "active"  # active | paused | archived
        self.cwd_override: str | None = None  # Set via /cd command

        logger.debug(
            "[SESSION %s] init: status=%s",
            self.id, self.status,
        )

    # --- Core: submit and process ---

    def submit(
        self, text: str, source: str = "user", metadata: dict | None = None
    ) -> None:
        """Submit a message to the session's input queue (non-blocking)."""
        logger.debug(
            "[SESSION %s] submit: source=%s, queue_size=%d, text_len=%d",
            self.id, source, self._input_queue.qsize(), len(text),
        )
        self._input_queue.put_nowait(InputMessage(text, source, metadata))

    async def run(self) -> AsyncIterator[tuple[AsyncIterator[AgentEvent], str]]:
        """Continuous loop: pull from input queue, yield (stream, source) pairs.

        Used by Listeners as the main consumption loop.
        """
        while True:
            msg = await self._input_queue.get()
            logger.debug(
                "[SESSION %s] Dequeued message: source=%s, queue_remaining=%d, "
                "text_preview=%.100s",
                self.id,
                msg.source,
                self._input_queue.qsize(),
                msg.text,
            )
            if msg.source == "sub_agent" and msg.metadata:
                process_text = self._format_sub_agent_message(msg.metadata)
            else:
                process_text = msg.text
            stream = self._process(process_text)
            yield stream, msg.source

    async def process(self, text: str) -> AsyncIterator[AgentEvent]:
        """Process user input through the bound agent (backward-compat wrapper).

        Yields: TextDelta, ActivityEvent, InteractionRequest, InterruptedEvent.
        Consumes internally: HistoryUpdate, SessionControl.
        """
        async for event in self._process(text):
            yield event

    async def _process(self, text: str) -> AsyncIterator[AgentEvent]:
        """Internal: process a single message through the bound agent.

        Yields: TextDelta, ActivityEvent, InteractionRequest, InterruptedEvent.
        Consumes internally: HistoryUpdate, SessionControl.
        """
        logger.info(
            "[SESSION %s] _process entry: text_len=%d, history_len=%d",
            self.id, len(text), len(self.history),
        )
        interrupted_text: str | None = None

        try:
            async with self._lock:
                pending_text: str | None = text

                # Restart loop: handles SessionControl("plan_execute")
                while pending_text is not None:
                    # Fresh token per iteration so a restart after
                    # plan_execute is not poisoned by the previous
                    # iteration's cancellation state.
                    token = CancellationToken()
                    self._current_token = token
                    interrupted_text = pending_text
                    restart_text: str | None = None

                    # Inject plugctx content into agent config
                    if self.plugctx is not None:
                        self.agent_config.extra["_plugctx_prompt"] = (
                            self.plugctx.render_prompt_section()
                        )
                    else:
                        self.agent_config.extra.pop("_plugctx_prompt", None)

                    # Inject effective cwd for tools
                    cwd, _ = self.effective_cwd()
                    self.agent_config.extra["_effective_cwd"] = cwd

                    async for event in self.agent.process(
                        pending_text,
                        list(self.history),
                        self.agent_config,
                        token,
                    ):
                        if isinstance(event, HistoryUpdate):
                            self.history = event.history
                            self.metadata.touch()
                            logger.debug(
                                "[SESSION %s] HistoryUpdate: new_len=%d",
                                self.id, len(event.history),
                            )
                            if self.on_history_update is not None:
                                try:
                                    self.on_history_update()
                                except Exception:
                                    logger.warning(
                                        "on_history_update failed (session=%s)",
                                        self.id,
                                        exc_info=True,
                                    )

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
                                logger.info(
                                    "[SESSION %s] plan_execute restart: text_len=%d",
                                    self.id, len(restart_text),
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
            logger.info("[SESSION %s] Interrupted by user (CancelledError)", self.id)
            # Record the interrupted prompt and a marker so the agent
            # knows what happened on the next turn.
            if interrupted_text is not None:
                self.history.append(
                    ChatMessage(role="user", content=interrupted_text)
                )
                self.history.append(
                    ChatMessage(
                        role="assistant",
                        content="[interrupted by user]",
                    )
                )
                self.metadata.touch()
                if self.on_history_update is not None:
                    try:
                        self.on_history_update()
                    except Exception:
                        logger.warning(
                            "on_history_update failed (session=%s)",
                            self.id,
                            exc_info=True,
                        )

            event = InterruptedEvent(partial_history=list(self.history))
            await self._broadcast(event)
            yield event

        finally:
            if self._current_token is not None:
                remaining = self._current_token.drain()
                for sig in remaining:
                    self.submit(
                        text=sig.payload,
                        source=sig.source or "sub_agent",
                        metadata=sig.metadata,
                    )
            self._current_token = None

    # --- Sub-agent message formatting ---

    def effective_cwd(self) -> tuple[str, str]:
        """Return (effective_cwd_path, source_label)."""
        if self.cwd_override:
            return self.cwd_override, "override"
        if self.plugctx is not None:
            project_cwd = self.plugctx.active_project_cwd()
            if project_cwd:
                return project_cwd, "project context"
        return os.getcwd(), "default"

    @staticmethod
    def _format_sub_agent_message(metadata: dict) -> str:
        """Build a single user-role message from sub-agent notification metadata."""
        event_type = metadata.get("event_type", "")
        session_id = metadata.get("session_id", "unknown")

        if event_type == "permission_required":
            interaction_id = metadata.get("interaction_id", "")
            tool_name = metadata.get("tool_name", "")
            notification_text = metadata.get("notification_text", "")
            return (
                f"[Sub-agent notification] session_id={session_id}\n"
                f"Permission required — interaction_id={interaction_id}, "
                f"tool={tool_name}\n"
                f"tool_input: {notification_text}\n"
                f"Use reply_agent to allow/deny. "
                f"For AskUserQuestion, include answers."
            )

        if event_type == "turn_complete":
            notification_text = metadata.get("notification_text", "")
            return (
                f"[Sub-agent notification] session_id={session_id}\n"
                f"Turn complete. Response:\n{notification_text}\n"
            )

        # Fallback for unknown event types
        notification_text = metadata.get("notification_text", "Sub-agent event.")
        return f"[Sub-agent notification] session_id={session_id}\n{notification_text}"

    # --- Interrupt ---

    def interrupt(self) -> None:
        """Cancel the current in-progress processing."""
        logger.info("[SESSION %s] interrupt requested", self.id)
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
        logger.debug(
            "[SESSION %s] attach_observer: total observers=%d",
            self.id, len(self.observers),
        )
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
        logger.info("[SESSION %s] clear_history: removed %d messages", self.id, count)
        return count

    def bind_primary(self, channel: Channel) -> None:
        """Bind a primary channel (can send input)."""
        self.primary_channel = channel


async def _queue_iter(queue: asyncio.Queue[AgentEvent]) -> AsyncIterator[AgentEvent]:
    """Convert an asyncio.Queue into an AsyncIterator."""
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
            yield event
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
