"""FeishuChannel — output-only channel for Feishu/Lark messaging."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from miniclaw.channels.base import Channel
from miniclaw.interactions import InteractionRequest, InteractionResponse
from miniclaw.providers.base import ChatMessage
from miniclaw.types import AgentEvent, InterruptedEvent, TextDelta

logger = logging.getLogger(__name__)


class FeishuChannel(Channel):
    """Output endpoint for a single Feishu conversation.

    Renders agent events by collecting text and sending the final
    message via the Feishu/Lark API.
    """

    def __init__(self, client, chat_id: str, reply_to: str = "") -> None:
        self._client = client
        self._chat_id = chat_id
        self._reply_to = reply_to

    async def send_stream(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Consume agent event stream, send final text to Feishu."""
        text_parts: list[str] = []

        async for event in stream:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, InteractionRequest):
                # Auto-resolve: no interactive UI on Feishu
                event.resolve(InteractionResponse(id=event.id, allow=True))
            elif isinstance(event, InterruptedEvent):
                text_parts.append("\n\n[interrupted]")
            # ActivityEvents are silently consumed

        full_text = "".join(text_parts).strip()
        if full_text:
            await self._send_text(full_text)

    async def send(self, text: str) -> None:
        """Send a simple text message."""
        await self._send_text(text)

    async def replay(self, history: list[ChatMessage]) -> None:
        """No replay on Feishu — sessions resume silently."""
        pass

    async def _send_text(self, text: str) -> None:
        """Send text via Feishu API."""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        content = json.dumps({"text": text})

        if self._reply_to:
            request = ReplyMessageRequest.builder() \
                .message_id(self._reply_to) \
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content)
                    .msg_type("text")
                    .build()
                ) \
                .build()
            response = self._client.im.v1.message.reply(request)
        else:
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(self._chat_id)
                    .content(content)
                    .msg_type("text")
                    .build()
                ) \
                .build()
            response = self._client.im.v1.message.create(request)

        if not response.success():
            logger.error(
                "Failed to send Feishu message: %s - %s",
                response.code,
                response.msg,
            )
