"""RemoteDaemon — WebSocket server for remote CCAgent execution.

Runs on a remote machine via ``minicode --serve``.  Accepts WebSocket
connections at ``/ws``, multiplexes sessions by ``session_id``.

Session lifecycle:
  - Sessions survive client disconnect for a configurable grace period
    (default 5 min) and are re-attachable via ``spawn`` with the same
    ``session_id``.
  - Max concurrent sessions configurable (default 10).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from aiohttp import WSMsgType, web

from miniclaw.agent.config import AgentConfig
from miniclaw.channels.base import Channel
from miniclaw.interactions import InteractionRequest, InteractionResponse
from miniclaw.remote.protocol import (
    deserialize_interaction_response,
    serialize_event,
)
from miniclaw.types import AgentEvent

logger = logging.getLogger(__name__)

# Grace period (seconds) before orphaned sessions are reaped.
DEFAULT_GRACE_PERIOD = 1800  # 30 min
DEFAULT_MAX_SESSIONS = 10


class DaemonSessionHandler(Channel):
    """Per-session Channel on the daemon side.

    Serializes AgentEvents from the child session and sends them over the
    WebSocket to the connected client.
    """

    def __init__(self, session_id: str, ws: web.WebSocketResponse) -> None:
        self._session_id = session_id
        self._ws = ws
        self._pending_interactions: dict[str, InteractionRequest] = {}
        self._consumer_task: asyncio.Task | None = None

    # --- Channel interface ---

    async def send_stream(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Serialize events and send over WS."""
        text_parts: list[str] = []

        async for event in stream:
            if isinstance(event, InteractionRequest):
                self._pending_interactions[event.id] = event

            wire = serialize_event(self._session_id, event)
            if wire is not None:
                try:
                    await self._ws.send_json(wire)
                except (ConnectionResetError, RuntimeError):
                    logger.warning(
                        "[DAEMON %s] WS send failed (client disconnected?)",
                        self._session_id,
                    )
                    break

            from miniclaw.types import TextDelta
            if isinstance(event, TextDelta):
                text_parts.append(event.text)

        # Send turn_complete with concatenated result
        result = "".join(text_parts)
        try:
            await self._ws.send_json({
                "type": "turn_complete",
                "session_id": self._session_id,
                "result": result,
            })
        except (ConnectionResetError, RuntimeError):
            pass

    async def send(self, text: str) -> None:
        """Not used for daemon sessions."""
        pass

    # --- Interaction resolution ---

    def resolve_interaction(self, msg: dict[str, Any]) -> None:
        """Resolve a real InteractionRequest future from a client message."""
        response = deserialize_interaction_response(msg)
        request = self._pending_interactions.pop(response.id, None)
        if request is None:
            logger.warning(
                "[DAEMON %s] resolve_interaction: unknown id=%s",
                self._session_id, response.id,
            )
            return
        request.resolve(response)
        logger.debug(
            "[DAEMON %s] Resolved interaction %s (allow=%s)",
            self._session_id, response.id, response.allow,
        )

    # --- Session consumer ---

    def start_consumer(self, session: Any) -> None:
        """Start background task consuming session.run()."""
        self._consumer_task = asyncio.create_task(self._consume(session))

    async def _consume(self, session: Any) -> None:
        """Background loop: consume session.run() streams."""
        try:
            async for stream, source in session.run():
                await self.send_stream(stream)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                "[DAEMON %s] Consumer failed: %s", self._session_id, e,
                exc_info=True,
            )
            try:
                await self._ws.send_json({
                    "type": "session_error",
                    "session_id": self._session_id,
                    "error": str(e),
                })
            except Exception:
                pass

    def cancel_consumer(self) -> None:
        """Cancel the consumer task."""
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()

    def deny_pending_interactions(self) -> None:
        """Deny all pending interactions (used on disconnect)."""
        for req_id, request in list(self._pending_interactions.items()):
            response = InteractionResponse(
                id=req_id,
                allow=False,
                message="Client disconnected",
            )
            request.resolve(response)
        self._pending_interactions.clear()


class _ManagedSession:
    """Internal state for a daemon-managed session."""

    def __init__(
        self,
        session: Any,
        handler: DaemonSessionHandler,
    ) -> None:
        self.session = session
        self.handler = handler
        self.last_active: float = time.monotonic()
        self.connected: bool = True


class RemoteDaemon:
    """WebSocket server for remote CCAgent execution.

    Usage::

        daemon = RemoteDaemon(config, host="127.0.0.1", port=9100)
        await daemon.run()
    """

    def __init__(
        self,
        config: dict[str, Any],
        host: str = "127.0.0.1",
        port: int = 9100,
        grace_period: int = DEFAULT_GRACE_PERIOD,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
    ) -> None:
        self._config = config
        self._host = host
        self._port = port
        self._grace_period = grace_period
        self._max_sessions = max_sessions

        self._runtime: Any = None  # set in run()
        self._managed: dict[str, _ManagedSession] = {}

    async def run(self) -> None:
        """Start aiohttp web app, serve WS at /ws."""
        self._runtime = self._build_runtime()

        app = web.Application()
        app.router.add_get("/ws", self._handle_connection)
        app.on_shutdown.append(self._on_shutdown)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()

        logger.info("RemoteDaemon listening on ws://%s:%d/ws", self._host, self._port)
        print(f"RemoteDaemon listening on ws://{self._host}:{self._port}/ws")

        # Run reaper loop and wait forever
        reaper = asyncio.create_task(self._reaper_loop())
        try:
            await asyncio.Event().wait()  # block until cancelled
        except asyncio.CancelledError:
            pass
        finally:
            reaper.cancel()
            await runner.cleanup()

    def _build_runtime(self) -> Any:
        """Build a Runtime instance with agent factories (same as cc_main)."""
        from miniclaw.agent.cc import CCAgent
        from miniclaw.persistence import SessionManager
        from miniclaw.runtime import Runtime

        cc_cfg = self._config.get("ccagent", {})
        workspace_dir = self._config["agent"]["workspace_dir"]

        def build_ccagent(cfg: AgentConfig, runtime_context: Any = None) -> Any:
            return CCAgent(
                system_prompt=cc_cfg.get("system_prompt", cfg.system_prompt or ""),
                default_model=cc_cfg.get("model", cfg.model or "claude-sonnet-4-6"),
                permission_mode=cc_cfg.get("permission_mode", "default"),
                allowed_tools=cc_cfg.get("allowed_tools"),
                cwd=cc_cfg.get("cwd"),
                max_turns=cc_cfg.get("max_turns"),
                thinking=cc_cfg.get("thinking"),
                effort=cc_cfg.get("effort"),
            )

        def build_native_agent(cfg: AgentConfig, runtime_context: Any = None) -> Any:
            from miniclaw.agent.native import NativeAgent
            from miniclaw.providers import create_provider
            from miniclaw.tools import create_registry

            provider = create_provider(self._config["provider"])
            registry = create_registry(self._config, runtime_context=runtime_context)
            return NativeAgent(
                provider=provider,
                tool_registry=registry,
                system_prompt=cfg.system_prompt or "",
                default_model=cfg.model or self._config["provider"].get("model", ""),
                temperature=cfg.temperature or 0.7,
                context_window=self._config["provider"].get("context_window", 0),
            )

        sm = SessionManager(workspace_dir)
        runtime = Runtime(sm, plugctx_config=self._config.get("plugctx"))
        runtime.register_agent("ccagent", build_ccagent)
        runtime.register_agent("native", build_native_agent)
        return runtime

    # --- WebSocket handler ---

    async def _handle_connection(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        logger.info("New WS connection from %s", request.remote)

        try:
            async for ws_msg in ws:
                if ws_msg.type == WSMsgType.TEXT:
                    try:
                        msg = json.loads(ws_msg.data)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON from client")
                        continue
                    await self._dispatch(ws, msg)
                elif ws_msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        except Exception as e:
            logger.error("WS handler error: %s", e, exc_info=True)
        finally:
            # Mark all sessions from this WS as disconnected
            for managed in self._managed.values():
                if managed.handler._ws is ws:
                    managed.connected = False
                    managed.last_active = time.monotonic()
                    managed.handler.deny_pending_interactions()
            logger.info("WS connection closed from %s", request.remote)

        return ws

    async def _dispatch(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        """Route incoming message to the appropriate handler."""
        msg_type = msg.get("type")

        if msg_type == "ping":
            await ws.send_json({"type": "pong"})
            return

        if msg_type == "healthcheck":
            await self._handle_healthcheck(ws, msg)
        elif msg_type == "spawn":
            await self._handle_spawn(ws, msg)
        elif msg_type == "interaction_response":
            self._handle_interaction_response(msg)
        elif msg_type == "send_message":
            self._handle_send_message(msg)
        elif msg_type == "cancel":
            self._handle_cancel(msg)
        elif msg_type == "terminate":
            self._handle_terminate(msg)
        elif msg_type == "file_read":
            await self._handle_file_read(ws, msg)
        elif msg_type == "file_glob":
            await self._handle_file_glob(ws, msg)
        elif msg_type == "file_grep":
            await self._handle_file_grep(ws, msg)
        else:
            logger.warning("Unknown message type: %s", msg_type)

    async def _handle_healthcheck(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        """Run `claude -p 'hi'` locally and report success/failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", "hi",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            await ws.send_json({
                "type": "healthcheck_result",
                "ok": proc.returncode == 0,
                "output": stdout.decode(errors="replace").strip(),
                "error": stderr.decode(errors="replace").strip(),
            })
        except asyncio.TimeoutError:
            await ws.send_json({
                "type": "healthcheck_result",
                "ok": False,
                "output": "",
                "error": "healthcheck timed out after 30s",
            })
        except Exception as e:
            await ws.send_json({
                "type": "healthcheck_result",
                "ok": False,
                "output": "",
                "error": str(e),
            })

    async def _handle_spawn(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        """Handle a spawn request — create or re-attach a session."""
        session_id = msg.get("session_id", "")
        agent_type = msg.get("agent_type", "ccagent")
        task = msg.get("task", "")

        # Re-attach to existing session?
        managed = self._managed.get(session_id)
        if managed is not None:
            logger.info("[DAEMON] Re-attaching to session %s", session_id)
            # Update WS and handler
            managed.handler._ws = ws
            managed.connected = True
            managed.last_active = time.monotonic()
            await ws.send_json({
                "type": "spawn_ack",
                "session_id": session_id,
                "ok": True,
            })
            return

        # Check capacity
        if len(self._managed) >= self._max_sessions:
            await ws.send_json({
                "type": "spawn_ack",
                "session_id": session_id,
                "ok": False,
                "error": f"Max sessions ({self._max_sessions}) reached",
            })
            return

        # Create new session
        try:
            agent_config_data = msg.get("agent_config", {})
            agent_config = AgentConfig(**agent_config_data) if agent_config_data else AgentConfig()

            # Pass runtime env from client if provided
            env = msg.get("env")
            if env:
                agent_config.extra["_runtime_env"] = env

            session = self._runtime.create_session(
                agent_type, agent_config, session_id=session_id,
            )

            # Apply client-requested cwd override
            cwd = msg.get("cwd")
            if cwd:
                session.cwd_override = cwd

            handler = DaemonSessionHandler(session_id, ws)
            session.bind_primary(handler)

            managed = _ManagedSession(session=session, handler=handler)
            self._managed[session_id] = managed

            # Submit the initial task
            session.submit(task, "user")

            # Start consuming session output
            handler.start_consumer(session)

            await ws.send_json({
                "type": "spawn_ack",
                "session_id": session_id,
                "ok": True,
            })
            logger.info(
                "[DAEMON] Spawned session %s (agent_type=%s)", session_id, agent_type,
            )
        except Exception as e:
            logger.error("[DAEMON] Failed to spawn session %s: %s", session_id, e, exc_info=True)
            await ws.send_json({
                "type": "spawn_ack",
                "session_id": session_id,
                "ok": False,
                "error": str(e),
            })

    def _handle_interaction_response(self, msg: dict[str, Any]) -> None:
        session_id = msg.get("session_id", "")
        managed = self._managed.get(session_id)
        if managed is None:
            logger.warning("[DAEMON] interaction_response for unknown session %s", session_id)
            return
        managed.handler.resolve_interaction(msg)
        managed.last_active = time.monotonic()

    def _handle_send_message(self, msg: dict[str, Any]) -> None:
        session_id = msg.get("session_id", "")
        text = msg.get("text", "")
        managed = self._managed.get(session_id)
        if managed is None:
            logger.warning("[DAEMON] send_message for unknown session %s", session_id)
            return
        managed.session.submit(text, "user")
        managed.last_active = time.monotonic()

    def _handle_cancel(self, msg: dict[str, Any]) -> None:
        session_id = msg.get("session_id", "")
        managed = self._managed.get(session_id)
        if managed is None:
            logger.warning("[DAEMON] cancel for unknown session %s", session_id)
            return
        managed.session.interrupt()
        managed.last_active = time.monotonic()

    def _handle_terminate(self, msg: dict[str, Any]) -> None:
        """Immediately reap a session (client graceful shutdown)."""
        session_id = msg.get("session_id", "")
        if not session_id:
            return
        managed = self._managed.pop(session_id, None)
        if managed:
            logger.info("[DAEMON] Terminating session %s (client requested)", session_id)
            managed.handler.cancel_consumer()
            managed.session.interrupt()

    # --- File operation RPCs (workspace:// reads) ---

    async def _handle_file_read(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        """Read a file and return its content."""
        from pathlib import Path

        fpath = msg.get("path", "")
        try:
            content = Path(fpath).read_text(errors="replace")
            if len(content) > 50000:
                content = content[:50000] + "\n... (truncated)"
            await ws.send_json({"type": "file_read_result", "content": content, "ok": True})
        except Exception as e:
            await ws.send_json({"type": "file_read_result", "ok": False, "error": str(e)})

    async def _handle_file_glob(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        """Glob files in a directory."""
        from pathlib import Path

        base = msg.get("path", "")
        pattern = msg.get("pattern", "")
        try:
            search_dir = Path(base)
            if not search_dir.is_dir():
                await ws.send_json({"type": "file_glob_result", "ok": False, "error": f"Directory not found: {base}"})
                return
            matches = sorted(str(p.relative_to(search_dir)) for p in search_dir.glob(pattern) if p.is_file())
            if len(matches) > 500:
                matches = matches[:500]
            await ws.send_json({"type": "file_glob_result", "matches": matches, "ok": True})
        except Exception as e:
            await ws.send_json({"type": "file_glob_result", "ok": False, "error": str(e)})

    async def _handle_file_grep(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        """Grep files for a regex pattern."""
        import re
        from pathlib import Path

        base = msg.get("path", "")
        pattern = msg.get("pattern", "")
        file_glob = msg.get("glob", "")
        max_results = 200
        try:
            regex = re.compile(pattern)
            search_path = Path(base)
            if not search_path.exists():
                await ws.send_json({"type": "file_grep_result", "ok": False, "error": f"Path not found: {base}"})
                return

            if search_path.is_file():
                files = [search_path]
            else:
                glob_pattern = file_glob if file_glob else "**/*"
                files = sorted(p for p in search_path.glob(glob_pattern) if p.is_file())

            results: list[str] = []
            for fpath in files:
                if len(results) >= max_results:
                    break
                try:
                    text = fpath.read_text(errors="replace")
                except Exception:
                    continue
                rel = str(fpath.relative_to(search_path)) if str(fpath).startswith(str(search_path)) else str(fpath)
                for line_num, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        results.append(f"{rel}:{line_num}: {line.rstrip()}")
                        if len(results) >= max_results:
                            break

            await ws.send_json({"type": "file_grep_result", "matches": results, "ok": True})
        except Exception as e:
            await ws.send_json({"type": "file_grep_result", "ok": False, "error": str(e)})

    # --- Session reaper ---

    async def _reaper_loop(self) -> None:
        """Periodically reap disconnected sessions past grace period."""
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            to_reap = [
                sid for sid, m in self._managed.items()
                if not m.connected and (now - m.last_active) > self._grace_period
            ]
            for sid in to_reap:
                logger.info("[DAEMON] Reaping session %s (grace period expired)", sid)
                managed = self._managed.pop(sid, None)
                if managed:
                    managed.handler.cancel_consumer()
                    managed.session.interrupt()

    async def _on_shutdown(self, app: web.Application) -> None:
        """Clean up all sessions on server shutdown."""
        for sid, managed in self._managed.items():
            managed.handler.cancel_consumer()
            managed.session.interrupt()
        self._managed.clear()
        logger.info("[DAEMON] All sessions cleaned up")
