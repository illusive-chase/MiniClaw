"""Runtime — top-level orchestrator.

Manages session lifecycle, listener supervision, agent registry,
persistence, and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
from copy import deepcopy
from typing import Any, Callable

from miniclaw.agent.config import AgentConfig
from miniclaw.agent.protocol import AgentProtocol
from miniclaw.channels.base import Channel
from miniclaw.listeners.base import Listener
from miniclaw.persistence import SessionManager
from miniclaw.session import Session, SessionMetadata, generate_session_id

logger = logging.getLogger(__name__)


class Runtime:
    """Top-level orchestrator.

    Responsibilities:
      - Session lifecycle (create, fork, attach, persist, restore)
      - Agent registry ("native" -> factory, "ccagent" -> factory)
      - Listener supervision (restart with exponential backoff)
      - Graceful shutdown (drain + persist)
    """

    def __init__(self, session_manager: SessionManager, plugctx_config: dict | None = None, remotes_config: dict[str, str] | None = None) -> None:
        self.sessions: dict[str, Session] = {}
        self._session_manager = session_manager
        self._agent_registry: dict[str, Callable[..., AgentProtocol]] = {}
        self._listeners: list[Listener] = []
        self._listener_tasks: list[asyncio.Task] = []
        self._shutting_down = False
        self._plugctx_config = plugctx_config or {}
        self._remotes_config = remotes_config or {}
        self.env: dict[str, str] = {}

        from miniclaw.remote.tunnel import TunnelManager
        self._tunnel_manager = TunnelManager()

    @property
    def tunnel_manager(self) -> Any:
        return self._tunnel_manager

    # --- Agent registry ---

    def register_agent(
        self, agent_type: str, factory: Callable[..., AgentProtocol]
    ) -> None:
        """Register an agent factory.

        factory signature: (config: AgentConfig, runtime_context: RuntimeContext | None) -> AgentProtocol
        """
        self._agent_registry[agent_type] = factory

    def create_agent(
        self,
        agent_type: str,
        config: AgentConfig | None = None,
        runtime_context: Any = None,
    ) -> AgentProtocol:
        """Create an agent instance from registry."""
        factory = self._agent_registry.get(agent_type)
        if factory is None:
            raise ValueError(
                f"Unknown agent type '{agent_type}'. "
                f"Registered: {list(self._agent_registry.keys())}"
            )
        return factory(config or AgentConfig(), runtime_context)

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
        """Create a new session with a fresh agent.

        Uses two-phase init:
          1. Create Session with agent=None placeholder
          2. Create RuntimeContext for the session
          3. Create agent with RuntimeContext
          4. Bind agent to session
        """
        from miniclaw.runtime_context import RuntimeContext

        sid = session_id or generate_session_id()

        # Phase 1: Create session with a placeholder agent
        # We use a temporary None and set it below
        session = Session(
            id=sid,
            agent=None,  # type: ignore[arg-type]
            agent_config=config,
            metadata=metadata or SessionMetadata(),
        )

        # Phase 2: Create RuntimeContext
        ctx = RuntimeContext(self, session)
        session.runtime_context = ctx

        # Phase 3: Create agent with RuntimeContext
        agent = self.create_agent(agent_type, config, runtime_context=ctx)
        session.agent = agent

        # Phase 4: Create PlugCtxManager if configured
        self._setup_plugctx(session)

        session.on_history_update = lambda _sid=sid: self.persist_session(_sid)
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
        from miniclaw.runtime_context import RuntimeContext

        source = self.sessions[source_id]
        source_agent_state = source.agent.serialize_state()
        forked_agent_state = await source.agent.on_fork(source_agent_state)

        config = new_config or deepcopy(source.agent_config)
        agent_type = new_agent_type or source.agent.agent_type

        forked = Session(
            id=generate_session_id(),
            agent=None,  # type: ignore[arg-type]
            agent_config=config,
            metadata=SessionMetadata(forked_from=source.id),
            history=list(source.history),
        )

        ctx = RuntimeContext(self, forked)
        forked.runtime_context = ctx

        agent = self.create_agent(agent_type, config, runtime_context=ctx)
        await agent.restore_state(forked_agent_state)
        forked.agent = agent

        self.sessions[forked.id] = forked
        forked.cwd_override = source.cwd_override
        forked.on_history_update = lambda _sid=forked.id: self.persist_session(_sid)

        # Copy plugctx from source
        source_ctx_paths = source.plugctx.loaded_paths() if source.plugctx is not None else []
        self._setup_plugctx(forked, restore_paths=source_ctx_paths or None)

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

    # --- Persistence ---

    def _setup_plugctx(
        self,
        session: Session,
        restore_paths: list[str] | None = None,
    ) -> None:
        """Create and attach a PlugCtxManager to a session."""
        if not self._plugctx_config:
            return

        from miniclaw.plugctx import PlugCtxManager

        ctx_root = self._plugctx_config["ctx_root"]
        auto_load_paths = self._plugctx_config.get("auto_load", [])

        mgr = PlugCtxManager(ctx_root=ctx_root, auto_load_paths=auto_load_paths)
        session.plugctx = mgr

        if restore_paths:
            failed = mgr.restore_from_paths(restore_paths)
            if failed:
                logger.warning("Failed to restore contexts: %s", failed)
        else:
            failed = mgr.auto_load()
            if failed:
                logger.warning("Failed to auto-load contexts: %s", failed)

    @staticmethod
    def _make_serializable(obj: object) -> object:
        """Recursively convert *obj* to JSON-safe primitives.

        Unlike ``dataclasses.asdict``, this never uses ``copy.deepcopy`` /
        pickle, so it is safe for objects that contain unpicklable types
        (e.g. aiohttp's CIMultiDictProxy).
        """
        import dataclasses
        from pathlib import PurePath

        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {
                f.name: Runtime._make_serializable(getattr(obj, f.name))
                for f in dataclasses.fields(obj)
            }
        if isinstance(obj, dict):
            return {k: Runtime._make_serializable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [Runtime._make_serializable(v) for v in obj]
        if isinstance(obj, PurePath):
            return str(obj)
        # Last resort — str() is always safe and preserves debuggability.
        return str(obj)

    def persist_session(self, session_id: str) -> None:
        """Save session to disk."""
        session = self.sessions[session_id]
        if not session.history:
            return

        from miniclaw.persistence import PersistedSession

        # Build config dict using _make_serializable instead of
        # dataclasses.asdict — the latter deep-copies via pickle, which
        # fails on non-picklable objects (e.g. aiohttp's CIMultiDictProxy)
        # stored in extra under _-prefixed internal keys.
        sanitized_config = self._make_serializable(session.agent_config)
        # Strip internal (_-prefixed) keys from extra — they hold runtime-only
        # objects (ToolPathContext, RemoteReader, …) that must not be persisted.
        sanitized_config["extra"] = {
            k: v
            for k, v in sanitized_config.get("extra", {}).items()
            if not k.startswith("_")
        }

        legacy = PersistedSession(
            id=session.id,
            sender_id=session.metadata.tags.get("sender_id", "unknown"),
            created_at=session.metadata.created_at,
            updated_at=session.metadata.updated_at,
            name=session.metadata.name,
            agent_type=session.agent.agent_type,
            agent_config=sanitized_config,
            agent_state=session.agent.serialize_state(),
            metadata={
                "forked_from": session.metadata.forked_from,
                "tags": dict(session.metadata.tags),
                "loaded_contexts": (
                    session.plugctx.loaded_paths()
                    if session.plugctx is not None
                    else []
                ),
                "cwd_override": session.cwd_override,
            },
        )
        self._session_manager.save(legacy, session.history)
        logger.debug("Persisted session %s", session_id)

    async def restore_session(self, session_id: str) -> Session:
        """Restore a session from disk."""
        from miniclaw.runtime_context import RuntimeContext

        loaded = self._session_manager.load_session(session_id)
        history = SessionManager.deserialize_messages(loaded.messages)

        # Use persisted agent_type, fall back to "native" for old files
        agent_type = loaded.agent_type or "native"

        # Backward compat: old sessions persisted with agent_type="cctmux"
        if agent_type == "cctmux":
            agent_type = "ccagent"
            if loaded.agent_config:
                loaded.agent_config.setdefault("backend", "cctmux")

        # Rebuild AgentConfig from persisted data, fall back to defaults
        config = AgentConfig(**loaded.agent_config) if loaded.agent_config else AgentConfig()

        # Rebuild full metadata
        meta_tags = dict(loaded.metadata.get("tags", {})) if loaded.metadata else {}
        if not meta_tags.get("sender_id"):
            meta_tags["sender_id"] = loaded.sender_id

        session = Session(
            id=loaded.id,
            agent=None,  # type: ignore[arg-type]
            agent_config=config,
            metadata=SessionMetadata(
                created_at=loaded.created_at,
                updated_at=loaded.updated_at,
                name=loaded.name,
                forked_from=loaded.metadata.get("forked_from") if loaded.metadata else None,
                tags=meta_tags,
            ),
            history=history,
        )

        ctx = RuntimeContext(self, session)
        session.runtime_context = ctx

        agent = self.create_agent(agent_type, config, runtime_context=ctx)

        # Restore agent-specific state (e.g., CCAgent sdk_session_id)
        if loaded.agent_state:
            await agent.restore_state(loaded.agent_state)

        session.agent = agent
        self.sessions[session.id] = session

        # Restore cwd_override
        persisted_cwd = loaded.metadata.get("cwd_override") if loaded.metadata else None
        if persisted_cwd and os.path.isdir(persisted_cwd):
            session.cwd_override = persisted_cwd

        # Restore plugctx
        loaded_contexts = loaded.metadata.get("loaded_contexts", []) if loaded.metadata else []
        self._setup_plugctx(session, restore_paths=loaded_contexts or None)

        session.on_history_update = lambda _sid=session.id: self.persist_session(_sid)
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

        # Shutdown runtime contexts (notify remote daemons to reap sessions)
        for session in self.sessions.values():
            if session.runtime_context is not None:
                try:
                    await session.runtime_context.shutdown()
                except Exception as e:
                    logger.error("Error shutting down runtime context: %s", e)

        # Close all SSH tunnels
        await self._tunnel_manager.close_all()

        logger.info("Runtime shutdown complete")
