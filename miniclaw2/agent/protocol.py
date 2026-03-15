"""AgentProtocol — uniform interface for all agent implementations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from miniclaw.providers.base import ChatMessage

from miniclaw2.agent.config import AgentConfig
from miniclaw2.cancellation import CancellationToken
from miniclaw2.types import AgentEvent


@runtime_checkable
class AgentProtocol(Protocol):
    """Uniform interface for all agent types (native, ccagent, custom).

    Agents are stateless per call (native) or stateful (ccagent).
    They receive history and config, yield typed events, and let
    Session handle state updates and control flow.
    """

    @property
    def agent_type(self) -> str:
        """Agent type identifier, e.g., 'native', 'ccagent'."""
        ...

    @property
    def default_model(self) -> str:
        """Fallback model name when no override is provided."""
        ...

    async def process(
        self,
        text: str,
        history: list[ChatMessage],
        config: AgentConfig,
        token: CancellationToken,
    ) -> AsyncIterator[AgentEvent]:
        """Process a user message. Yields events until done.

        The last meaningful event must be HistoryUpdate.
        Session intercepts HistoryUpdate and SessionControl;
        the rest (TextDelta, ActivityEvent, InteractionRequest) reach the Channel.
        """
        ...

    async def reset(self) -> None:
        """Discard internal state (e.g., close SDK client).

        Called on plan_execute, session clear, etc.
        """
        ...

    async def shutdown(self) -> None:
        """Release all resources. Called on Runtime shutdown."""
        ...

    def serialize_state(self) -> dict:
        """Return agent-specific state for session persistence.

        E.g., CCAgent returns {'sdk_session_id': '...'}.
        Native Agent returns {}.
        """
        ...

    async def restore_state(self, state: dict) -> None:
        """Restore from serialized state on session resume."""
        ...

    async def on_fork(self, source_state: dict) -> dict:
        """Create new agent state for a forked session.

        Returns a state dict for the forked agent.
        E.g., CCAgent returns {} (fresh SDK, no session reuse).
        """
        ...
