"""Provider ABC and data classes for LLM interaction."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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
