"""Typed subagent tool — spawns zero-context sub-agents with preset tool allowlists."""

import logging

from miniclaw.tools import ToolRegistry
from miniclaw.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

SUBAGENT_TYPES: dict[str, list[str]] = {
    "reader": ["file_read"],
    "editor": ["file_read", "file_edit", "file_write"],
    "executor": ["file_read", "file_edit", "file_write", "git", "shell"],
}

SUBAGENT_DESCS: dict[str, str] = {
    "reader": "Read files.",
    "editor": "Read/edit files.",
    "executor": "Read/edit files; execute shell commands.",
}


class SubagentTool(Tool):
    """Spawn a typed sub-agent with a restricted tool set."""

    def __init__(
        self,
        provider,
        tool_registry: ToolRegistry,
        memory,
        system_prompt: str,
        max_tool_iterations: int,
        default_model: str | None,
        temperature: float,
    ):
        self._provider = provider
        self._main_registry = tool_registry
        self._memory = memory
        self._system_prompt = system_prompt
        self._max_tool_iterations = max_tool_iterations
        self._default_model = default_model
        self._temperature = temperature

    def name(self) -> str:
        return "subagent"

    def description(self) -> str:
        types_desc = ", ".join(
            f"{t} ({', '.join(tools)})" for t, tools in SUBAGENT_TYPES.items()
        )
        return (
            "Spawn a typed sub-agent that runs independently with a restricted tool set. "
            f"Available types: {types_desc}."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": list(SUBAGENT_TYPES.keys()),
                    "description": (
                        "Sub-agent type determining which tools it can use. " +
                        ' '.join(f' {t.capitalize()}: {d}' for t, d in SUBAGENT_DESCS.items())
                    ),
                },
                "task": {
                    "type": "string",
                    "description": "Instruction for the sub-agent to execute.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for this sub-agent run.",
                },
            },
            "required": ["type", "task"],
        }

    async def execute(self, args: dict) -> ToolResult:
        # Lazy import to avoid circular dependency
        from miniclaw.agent import Agent

        agent_type = args.get("type", "")
        task = args.get("task", "")
        model = args.get("model")

        if agent_type not in SUBAGENT_TYPES:
            return ToolResult(
                output=f"Unknown subagent type '{agent_type}'. Valid types: {', '.join(SUBAGENT_TYPES)}",
                success=False,
            )
        if not task:
            return ToolResult(output="Task is required.", success=False)

        # Build filtered registry with only the allowed tools
        allowed_names = set(SUBAGENT_TYPES[agent_type])
        filtered_registry = ToolRegistry()
        for tool_name in allowed_names:
            tool = self._main_registry.get(tool_name)
            if tool is not None:
                filtered_registry.register(tool)

        available = filtered_registry.list_names()
        logger.info(
            "Subagent [%s] starting with tools=%s, task=%s",
            agent_type,
            available,
            task[:100],
        )

        agent = Agent(
            provider=self._provider,
            tool_registry=filtered_registry,
            memory=self._memory,
            system_prompt=self._system_prompt,
            max_tool_iterations=self._max_tool_iterations,
            default_model=self._default_model,
            temperature=self._temperature,
        )

        try:
            reply, _ = await agent.process_message(task, history=[], model=model)
            return ToolResult(output=reply)
        except Exception as e:
            logger.error("Subagent [%s] failed: %s", agent_type, e)
            return ToolResult(output=f"Subagent failed: {e}", success=False)
