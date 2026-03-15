"""Core event types flowing through the Agent -> Session -> Channel pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from miniclaw.activity import ActivityEvent
from miniclaw.interactions import InteractionRequest
from miniclaw.providers.base import ChatMessage
from miniclaw.usage import UsageStats


@dataclass
class TextDelta:
    """Progressive text chunk from the agent."""

    text: str


@dataclass
class HistoryUpdate:
    """Updated conversation history — consumed by Session, never forwarded to Channel."""

    history: list[ChatMessage]


@dataclass
class SessionControl:
    """Session-level control command — consumed by Session, never forwarded to Channel.

    Actions:
        plan_execute: Clear history, reset agent, restart with payload["plan_content"].
    """

    action: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class InterruptedEvent:
    """Signals that processing was interrupted by the user."""

    partial_history: list[ChatMessage] | None = None


@dataclass
class UsageEvent:
    """Cumulative token usage stats — yielded at the end of each agent response."""

    usage: UsageStats


# Union of all event types yielded by agents.
# Session intercepts HistoryUpdate and SessionControl; the rest reach the Channel.
AgentEvent = Union[
    TextDelta,
    ActivityEvent,
    InteractionRequest,
    HistoryUpdate,
    SessionControl,
    InterruptedEvent,
    UsageEvent,
]
