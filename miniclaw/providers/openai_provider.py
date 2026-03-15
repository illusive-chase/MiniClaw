"""OpenAI-compatible provider (works with OpenAI, Ollama, vLLM, Azure)."""

import json
import logging
import time
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from miniclaw.log import truncate
from miniclaw.usage import TokenUsage

from .base import ChatMessage, ChatResponse, Provider, ToolCall

logger = logging.getLogger(__name__)


class OpenAIProvider(Provider):
    """Provider for OpenAI-compatible APIs."""

    def __init__(self, api_key: str, base_url: str | None = None, model: str = "gpt-4o", max_tokens: int = 8192):
        self._model = model
        self._max_tokens = max_tokens
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
        )

    def _to_api_messages(self, messages: list[ChatMessage]) -> list[dict]:
        api_msgs = []
        for msg in messages:
            m = {"role": msg.role, "content": msg.content or ""}
            if msg.tool_calls:
                m["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in msg.tool_calls
                ]
                if not msg.content:
                    m["content"] = None
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            api_msgs.append(m)
        return api_msgs

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResponse:
        effective_model = model or self._model
        kwargs = {
            "model": effective_model,
            "messages": self._to_api_messages(messages),
            "temperature": temperature,
            "max_tokens": self._max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        logger.info(
            "[PROVIDER] OpenAI chat: model=%s, messages=%d, tools=%d, temperature=%.2f",
            effective_model, len(messages), len(tools) if tools else 0, temperature,
        )
        logger.debug(
            "[PROVIDER] OpenAI chat details: max_tokens=%d",
            self._max_tokens,
        )

        t0 = time.monotonic()
        response = await self._client.chat.completions.create(**kwargs)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        usage = None
        if response.usage:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
            )

        logger.info(
            "[PROVIDER] OpenAI chat done: duration_ms=%d, input_tokens=%d, "
            "output_tokens=%d, tool_calls=%d",
            elapsed_ms,
            usage.input_tokens if usage else 0,
            usage.output_tokens if usage else 0,
            len(tool_calls),
        )
        logger.debug(
            "[PROVIDER] OpenAI chat response: text_preview=%s",
            truncate(message.content or "", 200),
        )

        return ChatResponse(
            text=message.content,
            tool_calls=tool_calls,
            usage=usage,
        )

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[str | ChatResponse]:
        effective_model = model or self._model
        kwargs = {
            "model": effective_model,
            "messages": self._to_api_messages(messages),
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        logger.info(
            "[PROVIDER] OpenAI stream: model=%s, messages=%d, tools=%d",
            effective_model, len(messages), len(tools) if tools else 0,
        )
        logger.debug(
            "[PROVIDER] OpenAI stream details: max_tokens=%d, temperature=%.2f",
            self._max_tokens, temperature,
        )

        full_text_parts: list[str] = []
        # Accumulate tool calls by index: {index: {id, name, arg_fragments}}
        pending_tools: dict[int, dict] = {}
        usage: TokenUsage | None = None
        text_chunks = 0

        t0 = time.monotonic()
        response = await self._client.chat.completions.create(**kwargs)
        async for chunk in response:
            # The final chunk (with stream_options) has usage but empty choices
            if chunk.usage:
                usage = TokenUsage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                )
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # Text content
            if delta.content:
                full_text_parts.append(delta.content)
                text_chunks += 1
                yield delta.content

            # Tool call fragments
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in pending_tools:
                        pending_tools[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arg_fragments": [],
                        }
                    entry = pending_tools[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arg_fragments"].append(tc_delta.function.arguments)

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # Build final tool calls
        tool_calls: list[ToolCall] = []
        for idx in sorted(pending_tools):
            info = pending_tools[idx]
            raw = "".join(info["arg_fragments"])
            try:
                args = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_calls.append(ToolCall(
                id=info["id"],
                name=info["name"],
                arguments=args,
            ))

        logger.info(
            "[PROVIDER] OpenAI stream done: duration_ms=%d, input_tokens=%d, "
            "output_tokens=%d, tool_calls=%d, text_chunks=%d",
            elapsed_ms,
            usage.input_tokens if usage else 0,
            usage.output_tokens if usage else 0,
            len(tool_calls), text_chunks,
        )

        yield ChatResponse(
            text="".join(full_text_parts) if full_text_parts else None,
            tool_calls=tool_calls,
            usage=usage,
        )
