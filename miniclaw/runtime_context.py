"""RuntimeContext — bridge between tool layer and Runtime for sub-agent management."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from miniclaw.runtime import Runtime
    from miniclaw.session import Session

logger = logging.getLogger(__name__)


class SpawnLimitError(Exception):
    """Raised when a sub-agent spawn exceeds configured limits."""


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
        self._total_spawns: int = 0

    async def spawn(
        self,
        agent_type: str,
        task: str,
        remote: str | None = None,
        cwd: str | None = None,
        single_turn: bool = True,
    ) -> tuple[str, str]:
        """Spawn a background sub-agent session.

        Args:
            agent_type: Agent type to spawn (e.g. "ccagent", "native").
            task: Task instruction for the sub-agent.
            remote: Optional remote target — either a config key from
                ``config.remotes`` (e.g. "server1") or a raw ``ws://`` URL.
                If provided, spawns on a RemoteDaemon via WebSocket.
            single_turn: If True (default), auto-terminate the sub-agent
                after its first completed turn.

        Returns a tuple of (session_id, warning_text).
        Raises SpawnLimitError if limits are exceeded.
        """
        # Enforce spawn limits
        config = self._parent.agent_config
        running = sum(1 for d in self._drivers.values() if d.status == "running")

        if config.max_concurrent_agents is not None and running >= config.max_concurrent_agents:
            raise SpawnLimitError(
                f"Cannot spawn: {running} agents already running "
                f"(max_concurrent_agents={config.max_concurrent_agents}). "
                f"Use wait_agent first."
            )
        if config.max_total_spawns is not None and self._total_spawns >= config.max_total_spawns:
            raise SpawnLimitError(
                f"Cannot spawn: {self._total_spawns} total spawns reached "
                f"(max_total_spawns={config.max_total_spawns})."
            )
        if remote:
            session_id = await self._spawn_remote(agent_type, task, remote, cwd, single_turn)
            self._total_spawns += 1
            warning = self._spawn_warning(running + 1, config)
            return session_id, warning

        from miniclaw.agent.config import AgentConfig
        from miniclaw.subagent_driver import SubAgentDriver

        logger.debug(
            "[RUNTIME] spawn: agent_type=%s, parent_id=%s, task_preview=%.100s",
            agent_type, self._parent.id, task,
        )

        # Inherit parent's effective CWD when none explicitly provided
        if not cwd:
            cwd, _ = self._parent.effective_cwd()

        # Create child session via Runtime
        agent_config = AgentConfig()
        child_session = self._runtime.create_session(agent_type, agent_config)
        child_session.cwd_override = cwd

        # Create SubAgentDriver (dual-role: Channel for child, notifier for parent)
        driver = SubAgentDriver(
            session_id=child_session.id,
            parent_session=self._parent,
            child_session=child_session,
            single_turn=single_turn,
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
        self._total_spawns += 1
        warning = self._spawn_warning(running + 1, config)
        return child_session.id, warning

    async def _spawn_remote(
        self,
        agent_type: str,
        task: str,
        remote: str,
        cwd: str | None = None,
        single_turn: bool = True,
    ) -> str:
        """Spawn a sub-agent on a remote daemon via WebSocket."""
        from miniclaw.remote.remote_driver import RemoteSubAgentDriver
        from miniclaw.session import generate_session_id

        ws_url = await self._resolve_remote_url(remote)
        session_id = generate_session_id()

        logger.debug(
            "[RUNTIME] _spawn_remote: agent_type=%s, remote=%s, ws_url=%s, "
            "parent_id=%s, task_preview=%.100s",
            agent_type, remote, ws_url, self._parent.id, task,
        )

        driver = RemoteSubAgentDriver(
            session_id=session_id,
            parent_session=self._parent,
            ws_url=ws_url,
            agent_type=agent_type,
            task=task,
            cwd=cwd,
            single_turn=single_turn,
        )
        self._drivers[session_id] = driver
        driver.start()

        logger.info(
            "Spawned remote sub-agent %s (type=%s, remote=%s) from parent %s",
            session_id, agent_type, remote, self._parent.id,
        )
        return session_id

    async def _resolve_remote_url(self, remote: str) -> str:
        """Resolve a remote target to a WebSocket URL.

        If ``remote`` starts with ``ws://`` or ``wss://``, use it directly.
        Otherwise, look it up in ``runtime._remotes_config``.  If the config
        entry is a dict with ``ssh_host``, an SSH tunnel is created via
        the runtime's TunnelManager.
        """
        if remote.startswith("ws://") or remote.startswith("wss://"):
            return remote

        remotes = getattr(self._runtime, "_remotes_config", None) or {}
        entry = remotes.get(remote)
        if not entry:
            raise ValueError(
                f"Unknown remote '{remote}'. Configure it under "
                f"'remotes' in config.yaml or pass a ws:// URL directly."
            )

        # Dict config → SSH tunnel
        if isinstance(entry, dict):
            from miniclaw.remote.tunnel import TunnelError

            ssh_host = entry.get("ssh_host")
            if not ssh_host:
                raise ValueError(
                    f"Remote '{remote}' dict config missing 'ssh_host'."
                )
            tunnel_mgr = self._runtime.tunnel_manager
            try:
                tunnel = await tunnel_mgr.get_or_create(remote, entry)
            except TunnelError as exc:
                raise ValueError(
                    f"Failed to establish SSH tunnel for remote '{remote}': {exc}"
                ) from exc
            return tunnel.ws_url

        # String config → direct URL (backward compat)
        return entry

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
            logger.warning(
                "[RUNTIME] resolve: driver not found for session_id=%s",
                session_id,
            )
            return f"No sub-agent session found: {session_id}"

        logger.info(
            "[RUNTIME] resolve: session_id=%s, interaction_id=%s, action=%s",
            session_id, interaction_id, action,
        )
        return driver.resolve_interaction(interaction_id, action, reason, answers)

    async def send(self, session_id: str, text: str) -> str:
        """Send a follow-up message to a sub-agent session.

        Returns a status message.
        """
        from miniclaw.remote.remote_driver import RemoteSubAgentDriver

        driver = self._drivers.get(session_id)
        if driver is None:
            logger.warning(
                "[RUNTIME] send: driver not found for session_id=%s",
                session_id,
            )
            return f"No sub-agent session found: {session_id}"

        logger.debug(
            "[RUNTIME] send: session_id=%s, text_len=%d",
            session_id, len(text),
        )

        if isinstance(driver, RemoteSubAgentDriver):
            await driver.send(text)
        else:
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
        logger.debug("[RUNTIME] list_agents: count=%d", len(results))
        return results

    async def cancel(self, session_id: str) -> str:
        """Hard-terminate a running sub-agent session.

        - Remote: sends terminate to daemon and tears down the WS connection.
        - Local: cancels the driver's asyncio task, which propagates
          CancelledError through the session and kills the CC process.

        Returns a status message.
        """
        from miniclaw.remote.remote_driver import RemoteSubAgentDriver

        driver = self._drivers.get(session_id)
        if driver is None:
            logger.warning(
                "[RUNTIME] cancel: driver not found for session_id=%s",
                session_id,
            )
            return f"No sub-agent session found: {session_id}"

        logger.info("[RUNTIME] cancel (hard-kill): session_id=%s", session_id)
        if isinstance(driver, RemoteSubAgentDriver):
            await driver.close()
        else:
            # Cancel the asyncio task — CancelledError propagates through
            # session.run() → agent.process() → SDK finally block kills CC CLI
            if driver._task is not None and not driver._task.done():
                driver._task.cancel()
                try:
                    await driver._task
                except (asyncio.CancelledError, Exception):
                    pass
            driver._status = "interrupted"
        return f"Sub-agent {session_id} terminated"

    async def shutdown(self) -> None:
        """Close all sub-agent drivers (sends terminate to remote daemons)."""
        for driver in list(self._drivers.values()):
            try:
                if hasattr(driver, "close"):
                    await driver.close()
                elif hasattr(driver, "_child_session"):
                    driver._child_session.interrupt()
            except Exception as e:
                logger.error("Error closing sub-agent driver: %s", e)

    @staticmethod
    def _spawn_warning(current_running: int, config) -> str:
        """Return a soft warning string if the threshold is crossed, else empty."""
        if (
            config.spawn_warn_threshold is not None
            and current_running >= config.spawn_warn_threshold
        ):
            return (
                f"\nWarning: {current_running} concurrent agents running "
                f"(warn threshold={config.spawn_warn_threshold})."
            )
        return ""
