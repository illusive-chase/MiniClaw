"""FeishuListener — WebSocket event loop for Feishu/Lark messages."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from miniclaw.agent.config import AgentConfig
from miniclaw.channels.feishu import FeishuChannel
from miniclaw.listeners.base import Listener

if TYPE_CHECKING:
    from miniclaw.runtime import Runtime
    from miniclaw.session import Session

logger = logging.getLogger(__name__)


class FeishuListener(Listener):
    """Long-running input source that listens for Feishu messages via WebSocket.

    Routes messages to sessions per sender, creating FeishuChannels
    for each conversation. Uses session.submit() + background consumers.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        verification_token: str = "",
        agent_type: str = "native",
        agent_config: AgentConfig | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._verification_token = verification_token
        self._agent_type = agent_type
        self._agent_config = agent_config or AgentConfig()
        self._client = None
        self._consumer_tasks: dict[str, asyncio.Task] = {}

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

        # Track latest channel per session for response routing
        latest_channels: dict[str, FeishuChannel] = {}

        def handle_message(ctx: lark.Context, event: P2ImMessageReceiveV1) -> None:
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

                if not text.strip():
                    return

                sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
                chat_id = msg.chat_id or ""
                message_id = msg.message_id or ""

                # Get or create session for this sender
                session = runtime.get_or_create_session(
                    sender_id,
                    self._agent_type,
                    self._agent_config,
                )

                # Create channel for this conversation and track it
                channel = FeishuChannel(
                    client=self._client,
                    chat_id=chat_id,
                    reply_to=message_id,
                )
                session.bind_primary(channel)
                latest_channels[session.id] = channel

                # Ensure background consumer is running
                self._ensure_consumer(
                    session,
                    lambda _sid=session.id: latest_channels.get(_sid),
                )

                # Submit message to session queue
                session.submit(text.strip(), "user")

            except Exception as e:
                logger.error("Error handling Feishu message: %s", e, exc_info=True)

        event_handler = lark.EventDispatcherHandler.builder("", self._verification_token) \
            .register_p2_im_message_receive_v1(handle_message) \
            .build()

        ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        logger.info("Starting Feishu WebSocket connection...")

        # Run the WebSocket client in a thread to avoid blocking
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, ws_client.start)

    async def shutdown(self) -> None:
        """Graceful shutdown — cancel consumer tasks."""
        for task in self._consumer_tasks.values():
            task.cancel()
        self._consumer_tasks.clear()
