"""Feishu/Lark channel using lark-oapi SDK."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from .base import Channel, SendMessage

if TYPE_CHECKING:
    from miniclaw.gateway import Gateway

logger = logging.getLogger(__name__)


class FeishuChannel(Channel):
    """Feishu/Lark messaging channel using lark-oapi SDK."""

    def __init__(self, app_id: str, app_secret: str, verification_token: str = ""):
        self._app_id = app_id
        self._app_secret = app_secret
        self._verification_token = verification_token
        self._client = None
        self._gw: Gateway | None = None

    def _setup_client(self):
        import lark_oapi as lark

        self._client = lark.Client.builder() \
            .app_id(self._app_id) \
            .app_secret(self._app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

    def _create_event_handler(self):
        import lark_oapi as lark

        handler = lark.EventDispatcherHandler.builder("", self._verification_token) \
            .register_p2_im_message_receive_v1(self._make_message_handler()) \
            .build()
        return handler

    def _make_message_handler(self):
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        def handle(ctx: lark.Context, event: P2ImMessageReceiveV1) -> None:
            try:
                msg = event.event.message
                sender = event.event.sender

                # Only handle text messages
                if msg.message_type != "text":
                    logger.debug(f"Skipping non-text message type: {msg.message_type}")
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
                channel_id = msg.chat_id or ""

                # Allocate a session for this sender if not already tracked
                gw = self._gw
                session_id = gw.allocate_session(sender_id)

                async def _process():
                    try:
                        reply = await gw.process_message(session_id, text.strip())
                        await self.send(SendMessage(
                            text=reply,
                            channel_id=channel_id,
                            reply_to=msg.message_id or "",
                        ))
                    except Exception as e:
                        logger.error(f"Error processing message: {e}", exc_info=True)
                        await self.send(SendMessage(
                            text=f"Sorry, an error occurred: {e}",
                            channel_id=channel_id,
                            reply_to=msg.message_id or "",
                        ))

                # Run the async processing in the event loop
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(_process())
                else:
                    loop.run_until_complete(_process())

            except Exception as e:
                logger.error(f"Error handling Feishu message: {e}", exc_info=True)

        return handle

    async def start(self, gateway: Gateway) -> None:
        """Called by Gateway. Store gateway ref and begin listening."""
        import lark_oapi as lark

        self._gw = gateway
        self._setup_client()
        event_handler = self._create_event_handler()

        # Use WebSocket client (no public URL needed)
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

    async def send(self, message: SendMessage) -> None:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        if not self._client:
            self._setup_client()

        content = json.dumps({"text": message.text})

        if message.reply_to:
            # Reply to a specific message
            request = ReplyMessageRequest.builder() \
                .message_id(message.reply_to) \
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content)
                    .msg_type("text")
                    .build()
                ) \
                .build()
            response = self._client.im.v1.message.reply(request)
        else:
            # Send to a chat
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(message.channel_id)
                    .content(content)
                    .msg_type("text")
                    .build()
                ) \
                .build()
            response = self._client.im.v1.message.create(request)

        if not response.success():
            logger.error(f"Failed to send Feishu message: {response.code} - {response.msg}")
