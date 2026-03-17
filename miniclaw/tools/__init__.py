"""Tool registry with auto-discovery."""

import importlib
import inspect
import logging
import os
from pathlib import Path

from .base import Tool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of available tools."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name()] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_specs(self) -> list[dict]:
        return [tool.spec() for tool in self._tools.values()]

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def set_cwd(self, cwd: str) -> None:
        """Update cwd on all tools that support it."""
        for tool in self._tools.values():
            if hasattr(tool, '_cwd'):
                tool._cwd = Path(cwd) if isinstance(tool._cwd, Path) else cwd


def discover_tools(tools_dir: Path) -> list[Tool]:
    """Scan directory for .py files, import them, find Tool subclasses, instantiate."""
    discovered = []
    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name == "base.py":
            continue
        module_name = f"miniclaw.tools.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, Tool) and obj is not Tool:
                    # Skip classes that require manual registration (e.g., session tools)
                    if getattr(obj, "_manual_registration", False):
                        continue
                    discovered.append(obj)
        except Exception as e:
            logger.warning("Failed to import %s: %s", module_name, e)
    return discovered


def create_registry(config: dict, runtime_context=None) -> ToolRegistry:
    """Create a tool registry with built-in tools and auto-discovered tools.

    Args:
        config: Application config dict.
        runtime_context: Optional RuntimeContext for session management tools.
    """
    registry = ToolRegistry()
    cwd = os.getcwd()
    deny_set = set(config.get("agent", {}).get("tool_deny_list", []))

    # Auto-discover tool classes
    tools_dir = Path(__file__).parent
    tool_classes = discover_tools(tools_dir)

    for cls in tool_classes:
        try:
            sig = inspect.signature(cls.__init__)
            params = list(sig.parameters.keys())
            if "cwd" in params:
                tool = cls(cwd=cwd)
            else:
                tool = cls()
            if tool.name() in deny_set:
                logger.info("Tool '%s' excluded by deny list", tool.name())
                continue
            registry.register(tool)
        except Exception as e:
            logger.warning("Failed to instantiate %s: %s", cls.__name__, e)

    # Register session management tools if runtime_context is available
    if runtime_context is not None:
        from miniclaw.tools.session_tools import (
            CancelAgentTool,
            LaunchAgentTool,
            # MessageAgentTool, # NOTE: we does not support multi-turn sub-agent for now
            ReplyAgentTool,
            WaitAgentTool,
        )

        for cls in (
            LaunchAgentTool,
            ReplyAgentTool,
            # MessageAgentTool, # NOTE: we does not support multi-turn sub-agent for now
            CancelAgentTool,
            WaitAgentTool,
        ):
            tool = cls(runtime_context=runtime_context)
            if tool.name() not in deny_set:
                registry.register(tool)

    return registry
