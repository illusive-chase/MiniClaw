"""SubAgentDriver — dual-role Channel for child session + notifier for parent session."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from miniclaw.channels.base import Channel
from miniclaw.interactions import InteractionRequest, InteractionResponse
from miniclaw.types import AgentEvent, InterruptedEvent, TextDelta, UsageEvent

if TYPE_CHECKING:
    from miniclaw.session import Session

logger = logging.getLogger(__name__)


class SubAgentDriver(Channel):
    """Dual-role component:
      1. Channel for child CCAgent session (consumes agent event stream)
      2. Notifier for parent session (injects sub-agent events via submit)

    Handles:
      - Auto-resolving InteractionRequests for allowed tools
      - Forwarding other InteractionRequests to parent session
      - Notifying parent on completion/failure
    """

    def __init__(
        self,
        session_id: str,
        parent_session: Session,
        allowed_tools: list[str],
        child_session: Session,
    ) -> None:
        self._session_id = session_id
        self._parent_session = parent_session
        self._allowed_tools = set(allowed_tools)
        self._child_session = child_session

        self._status: str = "running"  # running | completed | failed | interrupted
        self._result: str | None = None
        self._pending_interactions: dict[str, InteractionRequest] = {}
        self._task: asyncio.Task | None = None

    # --- Channel interface (consumed by child session) ---

    async def send_stream(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Consume agent event stream from child session."""
        text_parts: list[str] = []

        async for event in stream:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)

            elif isinstance(event, InteractionRequest):
                await self._handle_interaction(event)

            elif isinstance(event, InterruptedEvent):
                self._status = "interrupted"
                self._notify_parent(
                    "interrupted",
                    f"Sub-agent {self._session_id} was interrupted.",
                )

            elif isinstance(event, UsageEvent):
                pass  # silently consumed

            # ActivityEvents silently consumed (parent doesn't need tool-level detail)

        # Capture final text as result
        full_text = "".join(text_parts)
        if full_text:
            self._result = full_text

    async def send(self, text: str) -> None:
        """Send a simple text message (not used for sub-agents, but required by Channel)."""
        pass

    # --- Interaction handling ---

    async def _handle_interaction(self, request: InteractionRequest) -> None:
        """Handle an InteractionRequest from the child agent."""
        # Auto-resolve if tool is in allowed list
        if request.tool_name in self._allowed_tools:
            request.resolve(InteractionResponse(id=request.id, allow=True))
            logger.debug(
                "Auto-resolved interaction %s (tool=%s, allowed)",
                request.id,
                request.tool_name,
            )
            return

        # Store pending and forward to parent
        self._pending_interactions[request.id] = request
        self._notify_parent(
            "permission_required",
            (
                f"Sub-agent {self._session_id} needs permission for "
                f"tool '{request.tool_name}'. "
                f"Use reply_agent to respond."
            ),
            extra={
                "session_id": self._session_id,
                "interaction_id": request.id,
                "tool_name": request.tool_name,
                "tool_input": str(request.tool_input)[:500],
            },
        )

    def resolve_interaction(
        self, interaction_id: str, action: str, reason: str | None = None
    ) -> str:
        """Resolve a pending interaction (called by RuntimeContext)."""
        request = self._pending_interactions.pop(interaction_id, None)
        if request is None:
            return f"No pending interaction with id '{interaction_id}'"

        allow = action.lower() in ("allow", "approve", "yes")
        response = InteractionResponse(
            id=interaction_id,
            allow=allow,
            message=reason or "",
        )
        request.resolve(response)
        return f"Interaction {interaction_id} resolved: {'allowed' if allow else 'denied'}"

    # --- Parent notification ---

    def _notify_parent(
        self,
        event_type: str,
        text: str,
        extra: dict | None = None,
    ) -> None:
        """Inject a sub-agent notification into the parent session's queue."""
        metadata = {
            "event_type": event_type,
            "session_id": self._session_id,
            "notification_text": text,
        }
        if extra:
            metadata.update(extra)

        self._parent_session.submit(
            text=text,
            source="sub_agent",
            metadata=metadata,
        )

    # --- Lifecycle ---

    def start(self) -> None:
        """Start the background consumer loop."""
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """Main loop: consume child session.run() and pipe through send_stream."""
        try:
            async for stream, source in self._child_session.run():
                await self.send_stream(stream)

            # Clean exit
            if self._status == "running":
                self._status = "completed"
                self._notify_parent(
                    "completed",
                    (
                        f"Sub-agent {self._session_id} completed. "
                        f"Result: {(self._result or '(no output)')[:500]}"
                    ),
                )
        except asyncio.CancelledError:
            if self._status == "running":
                self._status = "interrupted"
        except Exception as e:
            self._status = "failed"
            logger.error("SubAgentDriver %s failed: %s", self._session_id, e)
            self._notify_parent(
                "failed",
                f"Sub-agent {self._session_id} failed: {e}",
            )

    # --- State queries ---

    @property
    def status(self) -> str:
        return self._status

    @property
    def result(self) -> str | None:
        return self._result

    def pending_interaction_ids(self) -> list[str]:
        return list(self._pending_interactions.keys())
