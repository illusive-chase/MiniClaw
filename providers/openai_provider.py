"""OpenAI-compatible provider (works with OpenAI, Ollama, vLLM, Azure)."""

import json
from openai import AsyncOpenAI

from providers.base import Provider, ChatMessage, ChatResponse, ToolCall


class OpenAIProvider(Provider):
    """Provider for OpenAI-compatible APIs."""

    def __init__(self, api_key: str, base_url: str | None = None, model: str = "gpt-4o"):
        self._model = model
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
        kwargs = {
            "model": model or self._model,
            "messages": self._to_api_messages(messages),
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)
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

        return ChatResponse(
            text=message.content,
            tool_calls=tool_calls,
        )
