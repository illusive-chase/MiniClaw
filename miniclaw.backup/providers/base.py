"""Provider ABC and data classes for LLM interaction."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from miniclaw.usage import TokenUsage


@dataclass
class ToolCall:
    """A single tool invocation request from the LLM."""

    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class ChatMessage:
    """Unified message type for conversation history."""

    role: str  # "system", "user", "assistant", "tool"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


@dataclass
class ChatResponse:
    """LLM response with optional tool calls."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage | None = None


class Provider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResponse:
        """Send messages to the LLM and get a response."""
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[str | ChatResponse]:
        """Stream a response. Yields str text deltas, then a final ChatResponse."""
        ...
        # Make it an async generator so subclasses can use `yield`
        yield  # type: ignore[misc]  # pragma: no cover
