"""FeishuChannel — output-only channel for Feishu/Lark messaging."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator

from miniclaw.channels.base import Channel
from miniclaw.interactions import InteractionRequest, InteractionResponse
from miniclaw.providers.base import ChatMessage
from miniclaw.types import AgentEvent, InterruptedEvent, TextDelta

logger = logging.getLogger(__name__)

# Minimum interval between progressive patches (seconds)
_PATCH_DEBOUNCE = 3.0


class FeishuChannel(Channel):
    """Output endpoint for a single Feishu conversation.

    Renders agent events by collecting text and progressively updating
    the message via the Feishu/Lark async API.
    """

    def __init__(self, client, chat_id: str, reply_to: str = "") -> None:
        self._client = client
        self._chat_id = chat_id
        self._reply_to = reply_to
        self._sent_message_id: str | None = None

    @staticmethod
    def _build_card(text: str) -> str:
        """Build interactive card JSON wrapping text as markdown.

        The Feishu PATCH API only supports interactive cards, so all messages
        that may be progressively updated must be sent as cards.
        """
        card = {
            "elements": [
                {"tag": "markdown", "content": text},
            ],
        }
        return json.dumps(card)

    async def send_stream(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Consume agent event stream with debounced progressive updates."""
        text_parts: list[str] = []
        last_patch_time: float = 0.0

        async for event in stream:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)

                now = time.monotonic()
                # Send initial message on first substantial text
                if self._sent_message_id is None and len("".join(text_parts).strip()) > 0:
                    full = "".join(text_parts).strip()
                    self._sent_message_id = await self._send_text(full)
                    last_patch_time = now
                elif (
                    self._sent_message_id is not None
                    and now - last_patch_time >= _PATCH_DEBOUNCE
                ):
                    # Debounced progressive patch
                    full = "".join(text_parts).strip()
                    await self._patch_message(self._sent_message_id, full)
                    last_patch_time = now

            elif isinstance(event, InteractionRequest):
                # Auto-resolve: no interactive UI on Feishu
                event.resolve(InteractionResponse(id=event.id, allow=True))
            elif isinstance(event, InterruptedEvent):
                text_parts.append("\n\n[interrupted]")
            # ActivityEvents are silently consumed

        # Final send/patch with complete text
        full_text = "".join(text_parts).strip()
        if full_text:
            if self._sent_message_id is not None:
                await self._patch_message(self._sent_message_id, full_text)
            else:
                await self._send_text(full_text)

    async def send(self, text: str) -> None:
        """Send a simple text message."""
        await self._send_text(text)

    async def replay(self, history: list[ChatMessage]) -> None:
        """No replay on Feishu — sessions resume silently."""
        pass

    async def _send_text(self, text: str) -> str | None:
        """Send text as interactive card via async Feishu API. Returns message_id on success."""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        content = self._build_card(text)

        if self._reply_to:
            request = ReplyMessageRequest.builder() \
                .message_id(self._reply_to) \
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content)
                    .msg_type("interactive")
                    .build()
                ) \
                .build()
            response = await self._client.im.v1.message.areply(request)
        else:
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(self._chat_id)
                    .content(content)
                    .msg_type("interactive")
                    .build()
                ) \
                .build()
            response = await self._client.im.v1.message.acreate(request)

        if not response.success():
            logger.error(
                "Failed to send Feishu message: %s - %s",
                response.code,
                response.msg,
            )
            return None

        # Extract message_id from response for progressive updates
        if response.data and response.data.message_id:
            return response.data.message_id
        return None

    async def _patch_message(self, message_id: str, text: str) -> None:
        """Patch an existing message with updated text via async Feishu API."""
        from lark_oapi.api.im.v1 import (
            PatchMessageRequest,
            PatchMessageRequestBody,
        )

        content = self._build_card(text)
        request = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(
                PatchMessageRequestBody.builder()
                .content(content)
                .build()
            ) \
            .build()

        response = await self._client.im.v1.message.apatch(request)
        if not response.success():
            logger.error(
                "Failed to patch Feishu message %s: %s - %s",
                message_id,
                response.code,
                response.msg,
            )
