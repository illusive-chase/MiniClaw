"""Tool registry with auto-discovery."""

import importlib
import inspect
import logging
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
                    discovered.append(obj)
        except Exception as e:
            logger.warning("Failed to import %s: %s", module_name, e)
    return discovered


def create_registry(config: dict, memory=None) -> ToolRegistry:
    """Create a tool registry with built-in tools and auto-discovered tools."""
    registry = ToolRegistry()
    workspace_dir = config.get("agent", {}).get("workspace_dir", ".workspace")
    deny_set = set(config.get("agent", {}).get("tool_deny_list", []))

    # Auto-discover tool classes
    tools_dir = Path(__file__).parent
    tool_classes = discover_tools(tools_dir)

    for cls in tool_classes:
        try:
            sig = inspect.signature(cls.__init__)
            params = list(sig.parameters.keys())
            if "memory" in params and memory is not None:
                tool = cls(memory=memory)
            elif "workspace_dir" in params:
                tool = cls(workspace_dir=workspace_dir)
            else:
                tool = cls()
            if tool.name() in deny_set:
                logger.info("Tool '%s' excluded by deny list", tool.name())
                continue
            registry.register(tool)
        except Exception as e:
            logger.warning("Failed to instantiate %s: %s", cls.__name__, e)

    return registry
