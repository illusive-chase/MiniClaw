"""RuntimeContext — bridge between tool layer and Runtime for sub-agent management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from miniclaw.runtime import Runtime
    from miniclaw.session import Session

logger = logging.getLogger(__name__)


class RuntimeContext:
    """Bridge passed to tools, enabling them to spawn and manage sub-agent sessions.

    Created per-session by Runtime. Provides:
      - spawn(): create a background sub-agent session
      - resolve(): answer a pending interaction from a sub-agent
      - send(): send a follow-up message to a sub-agent
      - list_agents(): query running/completed sub-agents
      - cancel(): interrupt a sub-agent
    """

    def __init__(self, runtime: Runtime, parent_session: Session) -> None:
        self._runtime = runtime
        self._parent = parent_session
        self._drivers: dict[str, Any] = {}  # session_id -> SubAgentDriver

    async def spawn(
        self,
        agent_type: str,
        task: str,
    ) -> str:
        """Spawn a background sub-agent session.

        Returns the new session's ID.
        """
        from miniclaw.agent.config import AgentConfig
        from miniclaw.subagent_driver import SubAgentDriver

        # Create child session via Runtime
        agent_config = AgentConfig()
        child_session = self._runtime.create_session(agent_type, agent_config)

        # Create SubAgentDriver (dual-role: Channel for child, notifier for parent)
        driver = SubAgentDriver(
            session_id=child_session.id,
            parent_session=self._parent,
            child_session=child_session,
        )
        self._drivers[child_session.id] = driver

        # Bind driver as primary channel for child session
        child_session.bind_primary(driver)

        # Submit initial task to child session
        child_session.submit(task, "user")

        # Start the driver's background loop
        driver.start()

        logger.info(
            "Spawned sub-agent session %s (type=%s) from parent %s",
            child_session.id,
            agent_type,
            self._parent.id,
        )
        return child_session.id

    def resolve(
        self,
        session_id: str,
        interaction_id: str,
        action: str,
        reason: str | None = None,
        answers: dict[str, str] | None = None,
    ) -> str:
        """Resolve a pending interaction in a sub-agent session.

        action: "allow" | "deny"
        answer: optional dict of answers for AskUserQuestion interactions.
        Returns a status message.
        """
        driver = self._drivers.get(session_id)
        if driver is None:
            return f"No sub-agent session found: {session_id}"

        return driver.resolve_interaction(interaction_id, action, reason, answers)

    async def send(self, session_id: str, text: str) -> str:
        """Send a follow-up message to a sub-agent session.

        Returns a status message.
        """
        driver = self._drivers.get(session_id)
        if driver is None:
            return f"No sub-agent session found: {session_id}"

        child = driver._child_session
        child.submit(text, "user")
        return f"Message sent to sub-agent {session_id}"

    def list_agents(self) -> list[dict]:
        """List all sub-agents spawned by the parent session."""
        results = []
        for sid, driver in self._drivers.items():
            pending = driver.pending_interaction_ids()
            results.append(
                {
                    "session_id": sid,
                    "status": driver.status,
                    "result_preview": (driver.result or "")[:200],
                    "pending_interactions": pending,
                }
            )
        return results

    def cancel(self, session_id: str) -> str:
        """Cancel (interrupt) a running sub-agent session.

        Returns a status message.
        """
        driver = self._drivers.get(session_id)
        if driver is None:
            return f"No sub-agent session found: {session_id}"

        driver._child_session.interrupt()
        return f"Sub-agent {session_id} interrupted"
