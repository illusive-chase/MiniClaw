"""Subagent module — typed sub-agent execution with tracking."""

from .executor import SubagentExecutor
from .tracker import ExecutionTracker
from .types import (
    SUBAGENT_DEFAULT_MODELS,
    SUBAGENT_DESCS,
    SUBAGENT_PROMPTS,
    SUBAGENT_TYPES,
    SubagentRecord,
)

__all__ = [
    "SubagentExecutor",
    "ExecutionTracker",
    "SubagentRecord",
    "SUBAGENT_TYPES",
    "SUBAGENT_PROMPTS",
    "SUBAGENT_DESCS",
    "SUBAGENT_DEFAULT_MODELS",
]
