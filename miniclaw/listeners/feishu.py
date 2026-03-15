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

logger = logging.getLogger(__name__)


class FeishuListener(Listener):
    """Long-running input source that listens for Feishu messages via WebSocket.

    Routes messages to sessions per sender, creating FeishuChannels
    for each conversation.
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

    def _setup_client(self):
        import lark_oapi as lark

        self._client = lark.Client.builder() \
            .app_id(self._app_id) \
            .app_secret(self._app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

    async def run(self, runtime: Runtime) -> None:
        """Start WebSocket connection and listen for messages."""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        self._setup_client()

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

                async def _process():
                    try:
                        # Get or create session for this sender
                        session = runtime.get_or_create_session(
                            sender_id,
                            self._agent_type,
                            self._agent_config,
                        )

                        # Create channel for this conversation
                        channel = FeishuChannel(
                            client=self._client,
                            chat_id=chat_id,
                            reply_to=message_id,
                        )
                        session.bind_primary(channel)

                        # Process message
                        stream = session.process(text.strip())
                        await channel.send_stream(stream)

                    except Exception as e:
                        logger.error("Error processing Feishu message: %s", e, exc_info=True)
                        try:
                            error_channel = FeishuChannel(
                                client=self._client,
                                chat_id=chat_id,
                                reply_to=message_id,
                            )
                            await error_channel.send(f"Sorry, an error occurred: {e}")
                        except Exception:
                            logger.error("Failed to send error reply", exc_info=True)

                # Schedule async processing
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(_process())
                else:
                    loop.run_until_complete(_process())

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
        """Graceful shutdown — WebSocket client handles its own cleanup."""
        pass
