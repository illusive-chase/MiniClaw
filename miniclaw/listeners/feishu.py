"""FeishuListener — WebSocket event loop for Feishu/Lark messages."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import TYPE_CHECKING

from miniclaw.agent.config import AgentConfig
from miniclaw.channels.feishu import FeishuChannel
from miniclaw.listeners.base import Listener

if TYPE_CHECKING:
    from miniclaw.runtime import Runtime
    from miniclaw.session import Session

logger = logging.getLogger(__name__)

# Regex to strip @bot mentions in group chats (e.g. "@_user_1 hello" → "hello")
_MENTION_RE = re.compile(r"@_user_\d+\s*")


class FeishuListener(Listener):
    """Long-running input source that listens for Feishu messages via WebSocket.

    Routes messages to sessions per sender, creating FeishuChannels
    for each conversation. Uses session.submit() + background consumers.

    Threading model: lark_oapi.ws.Client.start() blocks forever in its own
    thread. Event callbacks run on the SDK thread. All asyncio operations
    are marshaled to the main loop via call_soon_threadsafe().
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        agent_type: str = "native",
        agent_config: AgentConfig | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._agent_type = agent_type
        self._agent_config = agent_config or AgentConfig()
        self._client = None
        self._consumer_tasks: dict[str, asyncio.Task] = {}
        self._shutdown_event: asyncio.Event | None = None

    def _setup_client(self):
        import lark_oapi as lark

        self._client = lark.Client.builder() \
            .app_id(self._app_id) \
            .app_secret(self._app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

    async def _consume(
        self, session: Session, get_channel: callable
    ) -> None:
        """Background consumer for a session: renders responses via the latest channel."""
        try:
            async for stream, source in session.run():
                channel = get_channel()
                if channel is not None:
                    await channel.send_stream(stream)
        except asyncio.CancelledError:
            pass

    def _ensure_consumer(self, session: Session, get_channel: callable) -> None:
        """Ensure a background consumer task exists for this session."""
        sid = session.id
        task = self._consumer_tasks.get(sid)
        if task is None or task.done():
            self._consumer_tasks[sid] = asyncio.create_task(
                self._consume(session, get_channel)
            )

    async def run(self, runtime: Runtime) -> None:
        """Start WebSocket connection and listen for messages."""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        self._setup_client()
        self._shutdown_event = asyncio.Event()

        # Capture the main event loop BEFORE spawning the SDK thread
        main_loop = asyncio.get_running_loop()

        # Track latest channel per session for response routing
        latest_channels: dict[str, FeishuChannel] = {}

        def _dispatch(
            sender_id: str, chat_id: str, message_id: str, text: str
        ) -> None:
            """Runs on the main event loop — safe for asyncio operations."""
            try:
                session = runtime.get_or_create_session(
                    sender_id,
                    self._agent_type,
                    self._agent_config,
                )

                channel = FeishuChannel(
                    client=self._client,
                    chat_id=chat_id,
                    reply_to=message_id,
                )
                session.bind_primary(channel)
                latest_channels[session.id] = channel

                self._ensure_consumer(
                    session,
                    lambda _sid=session.id: latest_channels.get(_sid),
                )

                session.submit(text, "user")
            except Exception as e:
                logger.error("Error dispatching Feishu message: %s", e, exc_info=True)

        def handle_message(event: P2ImMessageReceiveV1) -> None:
            """Runs on the SDK thread — must NOT call asyncio directly."""
            try:
                msg = event.event.message
                sender = event.event.sender

                # Only handle text messages
                if msg.message_type != "text":
                    logger.debug("Skipping non-text message type: %s", msg.message_type)
                    return

                # Parse text content
                try:
                    content = json.loads(msg.content)
                    text = content.get("text", "")
                except (json.JSONDecodeError, TypeError):
                    text = msg.content or ""

                # Strip @bot mentions (group chats)
                text = _MENTION_RE.sub("", text)

                if not text.strip():
                    return

                sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
                chat_id = msg.chat_id or ""
                message_id = msg.message_id or ""

                # Marshal to main event loop — thread-safe
                main_loop.call_soon_threadsafe(
                    _dispatch, sender_id, chat_id, message_id, text.strip()
                )
            except Exception as e:
                logger.error("Error handling Feishu message: %s", e, exc_info=True)

        # Both args must be empty strings in WebSocket mode (auth is via app credentials)
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(handle_message) \
            .build()

        logger.info("Starting Feishu WebSocket connection...")

        # Build and run ws.Client inside the thread so it never captures the
        # main event loop.  The lark SDK's Client.__init__ / start() calls
        # asyncio.get_event_loop(), which would return the (already-running)
        # main loop if constructed on the main thread.
        def _run_ws():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            # The SDK caches a module-level `loop` at import time, which is
            # the (already-running) main loop. Replace it so start() uses ours.
            import lark_oapi.ws.client as _ws_mod
            _ws_mod.loop = new_loop
            ws_client = lark.ws.Client(
                self._app_id,
                self._app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,
            )
            ws_client.start()

        ws_thread = threading.Thread(target=_run_ws, daemon=True)
        ws_thread.start()

        # Block until shutdown is requested
        await self._shutdown_event.wait()

    async def shutdown(self) -> None:
        """Graceful shutdown — unblock run() and cancel consumer tasks."""
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        for task in self._consumer_tasks.values():
            task.cancel()
        self._consumer_tasks.clear()
