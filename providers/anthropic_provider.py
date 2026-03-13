"""Anthropic Claude provider."""

from anthropic import AsyncAnthropic

from providers.base import ChatMessage, ChatResponse, Provider, ToolCall


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
        )
