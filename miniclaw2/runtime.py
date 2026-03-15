"""Runtime — top-level orchestrator replacing Gateway.

Manages session lifecycle, listener supervision, agent registry,
persistence, and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from miniclaw.providers.base import ChatMessage, ToolCall
from miniclaw.session import SessionManager

from miniclaw2.agent.config import AgentConfig
from miniclaw2.agent.protocol import AgentProtocol
from miniclaw2.channels.base import Channel
from miniclaw2.listeners.base import Listener
from miniclaw2.session import Session, SessionMetadata, generate_session_id

logger = logging.getLogger(__name__)


class Runtime:
    """Top-level orchestrator. Replaces Gateway.

    Responsibilities:
      - Session lifecycle (create, fork, attach, connect_pipe, persist, restore)
      - Agent registry ("native" -> factory, "ccagent" -> factory)
      - Listener supervision (restart with exponential backoff)
      - Graceful shutdown (drain + persist)
    """

    def __init__(self, session_manager: SessionManager) -> None:
        self.sessions: dict[str, Session] = {}
        self._session_manager = session_manager
        self._agent_registry: dict[str, Callable[..., AgentProtocol]] = {}
        self._listeners: list[Listener] = []
        self._listener_tasks: list[asyncio.Task] = []
        self._shutting_down = False

    # --- Agent registry ---

    def register_agent(
        self, agent_type: str, factory: Callable[..., AgentProtocol]
    ) -> None:
        """Register an agent factory.

        factory signature: (config: AgentConfig) -> AgentProtocol
        """
        self._agent_registry[agent_type] = factory

    def create_agent(self, agent_type: str, config: AgentConfig | None = None) -> AgentProtocol:
        """Create an agent instance from registry."""
        factory = self._agent_registry.get(agent_type)
        if factory is None:
            raise ValueError(
                f"Unknown agent type '{agent_type}'. "
                f"Registered: {list(self._agent_registry.keys())}"
            )
        return factory(config or AgentConfig())

    # --- Listener management ---

    def add_listener(self, listener: Listener) -> None:
        """Register a listener to be supervised."""
        self._listeners.append(listener)

    # --- Session lifecycle ---

    def create_session(
        self,
        agent_type: str,
        config: AgentConfig,
        session_id: str | None = None,
        metadata: SessionMetadata | None = None,
    ) -> Session:
        """Create a new session with a fresh agent."""
        agent = self.create_agent(agent_type, config)
        sid = session_id or generate_session_id()
        session = Session(
            id=sid,
            agent=agent,
            agent_config=config,
            metadata=metadata or SessionMetadata(),
        )
        self.sessions[sid] = session
        return session

    def get_or_create_session(
        self,
        sender_id: str,
        agent_type: str,
        config: AgentConfig,
    ) -> Session:
        """Find session by sender_id tag, or create one."""
        for s in self.sessions.values():
            if s.metadata.tags.get("sender_id") == sender_id:
                return s
        session = self.create_session(agent_type, config)
        session.metadata.tags["sender_id"] = sender_id
        return session

    async def fork_session(
        self,
        source_id: str,
        new_agent_type: str | None = None,
        new_config: AgentConfig | None = None,
    ) -> Session:
        """Fork a session: copy history, optionally switch agent/config."""
        source = self.sessions[source_id]
        source_agent_state = source.agent.serialize_state()
        forked_agent_state = await source.agent.on_fork(source_agent_state)

        config = new_config or deepcopy(source.agent_config)
        agent_type = new_agent_type or source.agent.agent_type
        agent = self.create_agent(agent_type, config)
        await agent.restore_state(forked_agent_state)

        forked = Session(
            id=generate_session_id(),
            agent=agent,
            agent_config=config,
            metadata=SessionMetadata(forked_from=source.id),
            history=list(source.history),
        )
        self.sessions[forked.id] = forked
        logger.info(
            "Forked session %s -> %s (agent=%s)",
            source.id, forked.id, agent_type,
        )
        return forked

    def attach_observer(self, session_id: str, channel: Channel) -> None:
        """Attach a read-only observer channel to a session."""
        session = self.sessions[session_id]
        session.attach_observer(channel)
        logger.info("Observer attached to session %s", session_id)

    def detach_observer(self, session_id: str, channel: Channel) -> None:
        """Detach an observer channel from a session."""
        session = self.sessions[session_id]
        session.detach_observer(channel)
        logger.info("Observer detached from session %s", session_id)

    def connect_pipe(self, session_a_id: str, session_b_id: str) -> None:
        """Connect two sessions via a bidirectional pipe."""
        from miniclaw2.channels.pipe import create_pipe
        from miniclaw2.listeners.pipe import PipeDriver

        sa = self.sessions[session_a_id]
        sb = self.sessions[session_b_id]
        pipe_a, pipe_b = create_pipe(session_a_id, session_b_id)

        driver_a = PipeDriver(sa, pipe_a)
        driver_b = PipeDriver(sb, pipe_b)
        asyncio.create_task(driver_a.run(self))
        asyncio.create_task(driver_b.run(self))
        logger.info("Pipe connected: %s <-> %s", session_a_id, session_b_id)

    # --- Persistence ---

    def persist_session(self, session_id: str) -> None:
        """Save session to disk."""
        session = self.sessions[session_id]
        if not session.history:
            return

        from miniclaw.session import Session as LegacySession

        legacy = LegacySession(
            id=session.id,
            sender_id=session.metadata.tags.get("sender_id", "unknown"),
            created_at=session.metadata.created_at,
            updated_at=session.metadata.updated_at,
            name=session.metadata.name,
        )
        self._session_manager.save(legacy, session.history)
        logger.debug("Persisted session %s", session_id)

    async def restore_session(self, session_id: str) -> Session:
        """Restore a session from disk."""
        loaded = self._session_manager.load_session(session_id)
        history = SessionManager.deserialize_messages(loaded.messages)

        # Determine agent type from saved data (default to "native")
        # For now, we restore as native. Future: save agent_type in session file.
        agent_type = "native"
        config = AgentConfig()

        agent = self.create_agent(agent_type, config)

        session = Session(
            id=loaded.id,
            agent=agent,
            agent_config=config,
            metadata=SessionMetadata(
                created_at=loaded.created_at,
                updated_at=loaded.updated_at,
                name=loaded.name,
                tags={"sender_id": loaded.sender_id},
            ),
            history=history,
        )
        self.sessions[session.id] = session
        logger.info("Restored session %s (%d messages)", session.id, len(history))
        return session

    def list_persisted_sessions(self) -> list:
        """List all persisted sessions."""
        return self._session_manager.list_sessions()

    # --- Runtime lifecycle ---

    async def run(self) -> None:
        """Start runtime. Supervise all listeners. Block until shutdown."""
        self._listener_tasks = [
            asyncio.create_task(self._supervise(listener))
            for listener in self._listeners
        ]
        try:
            await asyncio.gather(*self._listener_tasks)
        finally:
            await self._shutdown()

    async def _supervise(self, listener: Listener) -> None:
        """Restart listener on failure with exponential backoff."""
        backoff = 2.0
        max_backoff = 60.0

        while not self._shutting_down:
            try:
                await listener.run(self)
                break  # clean exit
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Listener %s failed: %s", listener, e)
                logger.debug("Restarting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _shutdown(self) -> None:
        """Graceful shutdown: stop listeners, persist sessions, close agents."""
        self._shutting_down = True
        logger.info("Runtime shutting down...")

        # Stop listeners
        for listener in self._listeners:
            try:
                await listener.shutdown()
            except Exception as e:
                logger.error("Error shutting down listener: %s", e)

        # Persist all sessions
        for session_id in list(self.sessions):
            try:
                self.persist_session(session_id)
            except Exception as e:
                logger.error("Failed to persist session %s: %s", session_id, e)

        # Shutdown all agents
        seen_agents: set[int] = set()
        for session in self.sessions.values():
            agent_id = id(session.agent)
            if agent_id not in seen_agents:
                seen_agents.add(agent_id)
                try:
                    await session.agent.shutdown()
                except Exception as e:
                    logger.error("Error shutting down agent: %s", e)

        logger.info("Runtime shutdown complete")
