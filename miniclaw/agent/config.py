"""Agent configuration shared across all agent types."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """Configuration for an agent session.

    Owned by Session, passed to agent on every process() call.
    Per-session overrides (model, effort) work naturally.
    """

    model: str = ""
    system_prompt: str = ""
    tools: list[str] | None = None  # allowed tool names (None = all)
    max_iterations: int = 30
    thinking: bool = False
    effort: str = "medium"  # "low" / "medium" / "high"
    temperature: float = 0.7
    extra: dict = field(default_factory=dict)  # agent-type-specific overrides
