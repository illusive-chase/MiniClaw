"""RemoteSubAgentDriver — local-side WebSocket client for remote CCAgent sessions.

Replaces SubAgentDriver when ``remote`` is specified in a spawn() call.
Connects to a RemoteDaemon over WebSocket, translates wire messages back into
local AgentEvents, and bridges InteractionRequests to the parent session.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import aiohttp

from miniclaw.cancellation import Signal, SignalType
from miniclaw.channels.base import Channel
from miniclaw.interactions import (
    InteractionRequest,
    InteractionResponse,
    InteractionType,
)
from miniclaw.remote.protocol import (
    serialize_cancel,
    serialize_interaction_response,
    serialize_send_message,
    serialize_spawn,
    serialize_terminate,
)
from miniclaw.types import AgentEvent

if TYPE_CHECKING:
    from miniclaw.session import Session

logger = logging.getLogger(__name__)

# Reconnection parameters
_RECONNECT_BASE = 2.0
_RECONNECT_MAX = 60.0
_RECONNECT_RETRIES = 5


class RemoteSubAgentDriver(Channel):
    """Local-side client that bridges a remote CCAgent session via WebSocket.

    Acts as a Channel from the parent session's perspective (has status,
    result, pending_interaction_ids, resolve_interaction, cancel, send).

    Internally connects to a RemoteDaemon WS endpoint, sends a spawn request,
    and processes the resulting event stream.
    """

    def __init__(
        self,
        session_id: str,
        parent_session: Session,
        ws_url: str,
        agent_type: str,
        task: str,
        agent_config: dict[str, Any] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._parent_session = parent_session
        self._ws_url = ws_url
        self._agent_type = agent_type
        self._task = task
        self._agent_config = agent_config
        self._cwd = cwd

        self._status: str = "running"  # running | completed | failed | interrupted
        self._result: str | None = None

        # Interaction bridging: interaction_id -> (proxy InteractionRequest, asyncio.Future)
        self._pending_interactions: dict[str, InteractionRequest] = {}
        self._pending_futures: dict[str, asyncio.Future] = {}
        self._waiter_tasks: dict[str, asyncio.Task] = {}

        self._task_handle: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._aio_session: aiohttp.ClientSession | None = None
        self._done = asyncio.Event()

    # --- Channel interface (not used directly — events come over WS) ---

    async def send_stream(self, stream: AsyncIterator[AgentEvent]) -> None:
        raise NotImplementedError("RemoteSubAgentDriver receives events over WS, not locally")

    async def send(self, text: str) -> None:
        """Send a follow-up message to the remote session."""
        if self._ws is not None and not self._ws.closed:
            msg = serialize_send_message(self._session_id, text)
            await self._ws.send_json(msg)

    # --- Lifecycle ---

    def start(self) -> None:
        """Start the background WS connection and event loop."""
        self._task_handle = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """Main loop: connect, send spawn, receive events. Reconnect on failure."""
        retries = 0
        backoff = _RECONNECT_BASE

        try:
            while retries <= _RECONNECT_RETRIES and self._status == "running":
                try:
                    self._aio_session = aiohttp.ClientSession()
                    try:
                        self._ws = await self._aio_session.ws_connect(self._ws_url)
                        logger.info(
                            "[REMOTE %s] Connected to %s", self._session_id, self._ws_url,
                        )

                        # Send spawn
                        spawn_msg = serialize_spawn(
                            self._session_id, self._agent_type, self._task, self._agent_config,
                            cwd=self._cwd,
                        )
                        await self._ws.send_json(spawn_msg)

                        # Wait for spawn_ack
                        ack = await self._ws.receive_json()
                        if not ack.get("ok"):
                            error = ack.get("error", "unknown error")
                            logger.error(
                                "[REMOTE %s] Spawn rejected: %s", self._session_id, error,
                            )
                            self._status = "failed"
                            self._notify_parent("failed", f"Remote spawn failed: {error}")
                            return

                        # Reset backoff on successful connect
                        retries = 0
                        backoff = _RECONNECT_BASE

                        # Receive loop
                        await self._receive_loop()

                    finally:
                        if self._ws and not self._ws.closed:
                            await self._ws.close()
                        await self._aio_session.close()
                        self._ws = None
                        self._aio_session = None

                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                    logger.warning(
                        "[REMOTE %s] Connection failed (retry %d/%d): %s",
                        self._session_id, retries, _RECONNECT_RETRIES, e,
                    )
                    retries += 1
                    if retries > _RECONNECT_RETRIES:
                        self._status = "failed"
                        self._deny_all_pending("Connection lost")
                        self._notify_parent(
                            "failed",
                            f"Remote agent {self._session_id} connection lost after {_RECONNECT_RETRIES} retries",
                        )
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _RECONNECT_MAX)

                except asyncio.CancelledError:
                    self._status = "interrupted"
                    return
        finally:
            self._done.set()

    async def _receive_loop(self) -> None:
        """Process incoming WS messages until connection drops or session ends."""
        assert self._ws is not None
        text_parts: list[str] = []

        async for ws_msg in self._ws:
            if ws_msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    msg = json.loads(ws_msg.data)
                except json.JSONDecodeError:
                    continue
                await self._handle_message(msg, text_parts)

            elif ws_msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    async def _handle_message(self, msg: dict[str, Any], text_parts: list[str]) -> None:
        """Process a single wire message from the remote daemon."""
        msg_type = msg.get("type")

        if msg_type == "text_delta":
            text_parts.append(msg.get("text", ""))

        elif msg_type == "interaction_request":
            await self._bridge_interaction(msg)

        elif msg_type == "interrupted":
            self._status = "interrupted"
            self._notify_parent(
                "interrupted",
                f"Remote sub-agent {self._session_id} was interrupted.",
            )

        elif msg_type == "turn_complete":
            result = msg.get("result", "")
            if result:
                self._result = result
            self._notify_parent(
                "turn_complete",
                self._result or "",
                extra={"session_id": self._session_id},
            )
            text_parts.clear()

        elif msg_type == "session_error":
            error = msg.get("error", "unknown")
            self._status = "failed"
            self._notify_parent("failed", f"Remote agent error: {error}")

        elif msg_type == "usage":
            pass  # silently consumed

        elif msg_type == "activity":
            pass  # silently consumed (parent doesn't need tool-level detail)

        elif msg_type == "pong":
            pass

    # --- Interaction bridging ---

    async def _bridge_interaction(self, msg: dict[str, Any]) -> None:
        """Bridge a remote interaction_request to the local parent session.

        1. Create a local proxy InteractionRequest with a new asyncio.Future.
        2. Notify parent ("permission_required").
        3. Spawn a waiter that awaits the future and sends the response over WS.
        """
        interaction_id = msg["interaction_id"]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[InteractionResponse] = loop.create_future()

        proxy_request = InteractionRequest(
            id=interaction_id,
            type=InteractionType(msg.get("interaction_type", "permission")),
            tool_name=msg.get("tool_name", ""),
            tool_input=msg.get("tool_input", {}),
            suggestions=msg.get("suggestions", []),
            _future=None,  # we manage our own future, not the SDK's
        )

        self._pending_interactions[interaction_id] = proxy_request
        self._pending_futures[interaction_id] = future

        self._notify_parent(
            "permission_required",
            json.dumps(proxy_request.tool_input, ensure_ascii=False, default=str),
            extra={
                "session_id": self._session_id,
                "interaction_id": interaction_id,
                "tool_name": proxy_request.tool_name,
            },
        )

        # Spawn waiter task
        task = asyncio.create_task(self._interaction_waiter(interaction_id, future))
        self._waiter_tasks[interaction_id] = task

    async def _interaction_waiter(
        self,
        interaction_id: str,
        future: asyncio.Future[InteractionResponse],
    ) -> None:
        """Wait for local resolution, then forward to remote daemon."""
        try:
            response = await future
            if self._ws is not None and not self._ws.closed:
                wire = serialize_interaction_response(
                    self._session_id, interaction_id, response,
                )
                await self._ws.send_json(wire)
        except asyncio.CancelledError:
            pass
        finally:
            self._waiter_tasks.pop(interaction_id, None)

    # --- Public API (called by RuntimeContext) ---

    def resolve_interaction(
        self,
        interaction_id: str,
        action: str,
        reason: str | None = None,
        answers: dict[str, str] | None = None,
    ) -> str:
        """Resolve a pending interaction — sets the local future, triggering WS send."""
        request = self._pending_interactions.pop(interaction_id, None)
        future = self._pending_futures.pop(interaction_id, None)

        if request is None or future is None:
            return f"No pending interaction with id '{interaction_id}'"

        allow = action.lower() in ("allow", "approve", "yes")

        updated_input: dict | None = None
        if answers and request.type == InteractionType.ASK_USER:
            updated_input = {**request.tool_input, "answers": answers}

        response = InteractionResponse(
            id=interaction_id,
            allow=allow,
            message=reason or "",
            updated_input=updated_input,
        )

        if not future.done():
            future.set_result(response)

        return f"Interaction {interaction_id} resolved: {'allowed' if allow else 'denied'}"

    def cancel(self) -> None:
        """Send cancel to remote session."""
        if self._ws is not None and not self._ws.closed:
            msg = serialize_cancel(self._session_id)
            asyncio.create_task(self._ws.send_json(msg))

    async def close(self) -> None:
        """Graceful shutdown: tell daemon to reap, then tear down local state."""
        # 1. Send terminate (best-effort — daemon may already be gone)
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.send_json(serialize_terminate(self._session_id))
            except Exception:
                pass

        # 2. Deny all pending interactions to unblock waiting futures
        self._deny_all_pending("Driver shutting down")

        # 3. Cancel interaction waiter tasks
        for task in self._waiter_tasks.values():
            task.cancel()
        self._waiter_tasks.clear()

        # 4. Cancel main _run() task (its finally block closes WS + aiohttp session)
        if self._task_handle is not None and not self._task_handle.done():
            self._task_handle.cancel()
            try:
                await self._task_handle
            except (asyncio.CancelledError, Exception):
                pass

    def _deny_all_pending(self, reason: str) -> None:
        """Deny all pending interactions (on disconnect)."""
        for iid, future in list(self._pending_futures.items()):
            if not future.done():
                response = InteractionResponse(
                    id=iid, allow=False, message=reason,
                )
                future.set_result(response)
        self._pending_interactions.clear()
        self._pending_futures.clear()

    # --- Parent notification ---

    def _notify_parent(
        self,
        event_type: str,
        text: str,
        extra: dict | None = None,
    ) -> None:
        """Inject a sub-agent notification into the parent session.

        If the parent is mid-turn (has a current token), route via the
        signal queue so the agent sees it immediately.  Otherwise fall
        back to the session input queue (dequeued on the next turn).
        """
        metadata = {
            "event_type": event_type,
            "session_id": self._session_id,
            "notification_text": text,
        }
        if extra:
            metadata.update(extra)

        token = self._parent_session._current_token
        if token is not None:
            token.send(Signal(
                type=SignalType.NOTIFICATION,
                payload=text,
                source="sub_agent",
                metadata=metadata,
            ))
        else:
            self._parent_session.submit(
                text=text,
                source="sub_agent",
                metadata=metadata,
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
