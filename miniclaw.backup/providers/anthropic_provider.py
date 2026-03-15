"""Anthropic Claude provider."""

import json
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic

from miniclaw.usage import TokenUsage

from .base import ChatMessage, ChatResponse, Provider, ToolCall


class AnthropicProvider(Provider):
    """Provider for Anthropic Claude API."""

    def __init__(self, api_key: str, base_url: str = None, model: str = "claude-sonnet-4-6"):
        self._model = model
        self._client = AsyncAnthropic(api_key=api_key, base_url=base_url)

    def _to_api_messages(self, messages: list[ChatMessage]) -> tuple[str, list[dict]]:
        """Convert ChatMessages to Anthropic format. Returns (system, messages)."""
        system = ""
        api_msgs = []
        for msg in messages:
            if msg.role == "system":
                system = msg.content or ""
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

        kwargs = {
            "model": model or self._model,
            "messages": api_msgs,
            "max_tokens": 4096,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._to_api_tools(tools)

        response = await self._client.messages.create(**kwargs)

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

        return ChatResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            ),
        )

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[str | ChatResponse]:
        system, api_msgs = self._to_api_messages(messages)

        kwargs = {
            "model": model or self._model,
            "messages": api_msgs,
            "max_tokens": 4096,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._to_api_tools(tools)

        full_text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        # Track in-progress tool_use blocks: index -> {id, name, json_fragments}
        pending_tools: dict[int, dict] = {}
        block_index = 0

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
                    block_index = event.index
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        full_text_parts.append(delta.text)
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

            final_message = await stream.get_final_message()

        usage = TokenUsage(
            input_tokens=final_message.usage.input_tokens,
            output_tokens=final_message.usage.output_tokens,
            cache_read_tokens=getattr(final_message.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(final_message.usage, "cache_creation_input_tokens", 0) or 0,
        )

        yield ChatResponse(
            text="".join(full_text_parts) if full_text_parts else None,
            tool_calls=tool_calls,
            usage=usage,
        )
