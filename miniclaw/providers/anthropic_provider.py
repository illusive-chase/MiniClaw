"""Anthropic Claude provider."""

import json
import logging
import time
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic

from miniclaw.log import truncate
from miniclaw.usage import TokenUsage

from .base import ChatMessage, ChatResponse, Provider, ToolCall

logger = logging.getLogger(__name__)


class AnthropicProvider(Provider):
    """Provider for Anthropic Claude API."""

    def __init__(self, api_key: str, base_url: str = None, model: str = "claude-sonnet-4-6", max_tokens: int = 8192):
        self._model = model
        self._max_tokens = max_tokens
        self._client = AsyncAnthropic(api_key=api_key, base_url=base_url)

    @staticmethod
    def _mark_last_block(blocks: list[dict]) -> None:
        """Add cache_control to the last block in a list (in-place)."""
        if blocks:
            blocks[-1]["cache_control"] = {"type": "ephemeral"}

    def _to_api_messages(self, messages: list[ChatMessage]) -> tuple[str | list[dict], list[dict]]:
        """Convert ChatMessages to Anthropic format. Returns (system, messages).

        System is returned as a list of content blocks (with cache_control on the last one)
        when non-empty, or empty string when absent.
        """
        system_text = ""
        api_msgs = []
        for msg in messages:
            if msg.role == "system":
                system_text = msg.content or ""
                continue
            if msg.role == "assistant":
                content = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        content.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        })
                api_msgs.append({"role": "assistant", "content": content or msg.content or ""})
            elif msg.role == "tool":
                api_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": msg.content or "",
                    }],
                })
            else:
                api_msgs.append({"role": msg.role, "content": msg.content or ""})

        # Mark the last content block of the last message for caching
        

        # Convert system to block format with cache_control
        if system_text:
            system = [{"type": "text", "text": system_text}]
            if api_msgs:
                last_msg = api_msgs[-1]
                if isinstance(last_msg["content"], list):
                    self._mark_last_block(last_msg["content"])
                elif isinstance(last_msg["content"], str) and last_msg["content"]:
                    # Convert plain string to block format so we can attach cache_control
                    last_msg["content"] = [
                        {"type": "text", "text": last_msg["content"], "cache_control": {"type": "ephemeral"}},
                    ]
            else:
                self._mark_last_block(system)
        else:
            system = ""

        return system, api_msgs

    def _to_api_tools(self, tools: list[dict]) -> list[dict]:
        """Convert OpenAI-format tool specs to Anthropic format."""
        anthropic_tools = []
        for tool in tools:
            func = tool.get("function", {})
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return anthropic_tools

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResponse:
        system, api_msgs = self._to_api_messages(messages)
        effective_model = model or self._model

        kwargs = {
            "model": effective_model,
            "messages": api_msgs,
            "max_tokens": self._max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._to_api_tools(tools)

        logger.info(
            "[PROVIDER] Anthropic chat: model=%s, messages=%d, tools=%d, temperature=%.2f",
            effective_model, len(api_msgs), len(tools) if tools else 0, temperature,
        )
        logger.debug(
            "[PROVIDER] Anthropic chat details: max_tokens=%d, system_len=%d",
            self._max_tokens, len(system),
        )

        t0 = time.monotonic()
        response = await self._client.messages.create(**kwargs)
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {},
                ))

        usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )

        logger.info(
            "[PROVIDER] Anthropic chat done: duration_ms=%d, input_tokens=%d, "
            "output_tokens=%d, tool_calls=%d",
            elapsed_ms, usage.input_tokens, usage.output_tokens, len(tool_calls),
        )
        logger.debug(
            "[PROVIDER] Anthropic chat response: text_preview=%s, "
            "cache_read=%d, cache_creation=%d",
            truncate("\n".join(text_parts)) if text_parts else "(none)",
            usage.cache_read_tokens, usage.cache_creation_tokens,
        )

        return ChatResponse(
            text="\n".join(text_parts) if text_parts else None,
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
        system, api_msgs = self._to_api_messages(messages)
        effective_model = model or self._model

        kwargs = {
            "model": effective_model,
            "messages": api_msgs,
            "max_tokens": self._max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._to_api_tools(tools)

        logger.info(
            "[PROVIDER] Anthropic stream: model=%s, messages=%d, tools=%d",
            effective_model, len(api_msgs), len(tools) if tools else 0,
        )
        logger.debug(
            "[PROVIDER] Anthropic stream details: max_tokens=%d, temperature=%.2f",
            self._max_tokens, temperature,
        )

        full_text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        # Track in-progress tool_use blocks: index -> {id, name, json_fragments}
        pending_tools: dict[int, dict] = {}
        block_index = 0
        text_chunks = 0

        t0 = time.monotonic()
        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        pending_tools[event.index] = {
                            "id": block.id,
                            "name": block.name,
                            "json_fragments": [],
                        }
                        logger.debug(
                            "[PROVIDER] Anthropic stream tool_use start: name=%s, id=%s",
                            block.name, block.id,
                        )
                    block_index = event.index
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        full_text_parts.append(delta.text)
                        text_chunks += 1
                        yield delta.text
                    elif delta.type == "input_json_delta":
                        idx = event.index
                        if idx in pending_tools:
                            pending_tools[idx]["json_fragments"].append(delta.partial_json)
                elif event.type == "content_block_stop":
                    idx = event.index
                    if idx in pending_tools:
                        info = pending_tools.pop(idx)
                        raw = "".join(info["json_fragments"])
                        try:
                            args = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            args = {}
                        tool_calls.append(ToolCall(
                            id=info["id"],
                            name=info["name"],
                            arguments=args if isinstance(args, dict) else {},
                        ))
                        logger.debug(
                            "[PROVIDER] Anthropic stream tool_use complete: name=%s",
                            info["name"],
                        )

            final_message = await stream.get_final_message()

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        usage = TokenUsage(
            input_tokens=final_message.usage.input_tokens,
            output_tokens=final_message.usage.output_tokens,
            cache_read_tokens=getattr(final_message.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(final_message.usage, "cache_creation_input_tokens", 0) or 0,
        )

        logger.info(
            "[PROVIDER] Anthropic stream done: duration_ms=%d, input_tokens=%d, "
            "output_tokens=%d, tool_calls=%d, text_chunks=%d",
            elapsed_ms, usage.input_tokens, usage.output_tokens,
            len(tool_calls), text_chunks,
        )

        yield ChatResponse(
            text="".join(full_text_parts) if full_text_parts else None,
            tool_calls=tool_calls,
            usage=usage,
        )
