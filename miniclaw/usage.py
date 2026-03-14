"""Shared token usage and cost tracking for all agent backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_agent_sdk import ResultMessage


@dataclass
class TokenUsage:
    """Raw token counts from a single LLM call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class UsageStats:
    """Cumulative token usage and cost for a session."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    total_api_duration_ms: int = 0
    num_turns: int = 0
    num_requests: int = 0

    def accumulate(self, result: ResultMessage) -> None:
        """Accumulate stats from a Claude Agent SDK ResultMessage."""
        if result.total_cost_usd is not None:
            self.total_cost_usd += result.total_cost_usd
        self.total_duration_ms += result.duration_ms
        self.total_api_duration_ms += result.duration_api_ms
        self.num_turns += result.num_turns
        self.num_requests += 1
        if result.usage:
            self.input_tokens += result.usage.get("input_tokens", 0)
            self.output_tokens += result.usage.get("output_tokens", 0)
            self.cache_read_tokens += result.usage.get("cache_read_input_tokens", 0)
            self.cache_creation_tokens += result.usage.get("cache_creation_input_tokens", 0)

    def accumulate_token_usage(self, usage: TokenUsage | None, duration_ms: int = 0) -> None:
        """Accumulate stats from a TokenUsage (regular agent path)."""
        self.num_requests += 1
        self.total_duration_ms += duration_ms
        if usage is not None:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.cache_read_tokens += usage.cache_read_tokens
            self.cache_creation_tokens += usage.cache_creation_tokens
