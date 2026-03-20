"""Tool ABC and data classes for tool execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ToolResult:
    """Result of a tool execution."""

    output: str
    success: bool = True


@dataclass
class ToolPathContext:
    """Context for virtual path resolution, set per-message."""

    cwd: Path                                     # planspace or default cwd
    ctx_root: Path | None = None                  # plugctx root for ctx:// resolution
    workspace: str | None = None                  # absolute workspace path for workspace://
    remote: str | None = None                     # remote name (if workspace is remote)
    remote_reader: Any = None                     # async callable for remote file ops


class Tool(ABC):
    """Abstract base class for agent tools."""

    @abstractmethod
    def name(self) -> str:
        """Unique tool name."""
        ...

    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what the tool does."""
        ...

    @abstractmethod
    def parameters_schema(self) -> dict:
        """JSON Schema for the tool's parameters."""
        ...

    @abstractmethod
    async def execute(self, args: dict) -> ToolResult:
        """Execute the tool with the given arguments."""
        ...

    def spec(self) -> dict:
        """Build OpenAI-format tool specification."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": self.parameters_schema(),
            },
        }
