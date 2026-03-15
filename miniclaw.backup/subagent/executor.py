"""Subagent executor — spawns isolated sub-agents with restricted tool sets."""

import logging

from miniclaw.memory.base import Memory
from miniclaw.providers.base import Provider
from miniclaw.tools import ToolRegistry

from .tracker import ExecutionTracker
from .types import (
    SUBAGENT_DEFAULT_MODELS,
    SUBAGENT_DESCS,
    SUBAGENT_PROMPTS,
    SUBAGENT_TYPES,
)

logger = logging.getLogger(__name__)


class SubagentExecutor:
    """Spawns typed sub-agents with filtered tool sets."""

    def __init__(
        self,
        provider: Provider,
        tool_registry: ToolRegistry,
        memory: Memory,
        default_model: str | None = None,
        temperature: float = 0.7,
        max_iterations: int = 8,
    ):
        self._provider = provider
        self._main_registry = tool_registry
        self._memory = memory
        self._default_model = default_model
        self._temperature = temperature
        self._max_iterations = max_iterations

    async def run(self, args: dict, tracker: ExecutionTracker) -> str:
        """Run a subagent. Returns the text reply (isolation preserved)."""
        # Lazy import to avoid circular dependency
        from miniclaw.agent import Agent

        agent_type = args.get("type", "")
        task = args.get("task", "")
        model = args.get("model") or SUBAGENT_DEFAULT_MODELS.get(agent_type) or self._default_model

        if agent_type not in SUBAGENT_TYPES:
            return f"Unknown subagent type '{agent_type}'. Valid types: {', '.join(SUBAGENT_TYPES)}"
        if not task:
            return "Task is required."

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

        # Start tracking
        record = tracker.start(type=agent_type, task=task, model=model)

        # Create sub-agent WITHOUT subagent_executor/execution_tracker (prevents recursion)
        agent = Agent(
            provider=self._provider,
            tool_registry=filtered_registry,
            memory=self._memory,
            system_prompt=SUBAGENT_PROMPTS.get(agent_type, ""),
            max_tool_iterations=self._max_iterations,
            default_model=self._default_model,
            temperature=self._temperature,
            subagent_executor=None,
            execution_tracker=None,
        )

        try:
            reply, _ = await agent.process_message(task, history=[], model=model)
            tracker.complete(record, reply)
            return reply
        except Exception as e:
            error_msg = str(e)
            logger.error("Subagent [%s] failed: %s", agent_type, error_msg)
            tracker.fail(record, error_msg)
            return f"Subagent failed: {error_msg}"

    @staticmethod
    def subagent_spec() -> dict:
        """Return OpenAI-format tool spec for the subagent built-in."""
        types_desc = ", ".join(
            f"{t} ({', '.join(tools)})" for t, tools in SUBAGENT_TYPES.items()
        )
        return {
            "type": "function",
            "function": {
                "name": "subagent",
                "description": (
                    "Spawn a typed sub-agent that runs independently with a restricted tool set. "
                    f"Available types: {types_desc}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": list(SUBAGENT_TYPES.keys()),
                            "description": (
                                "Sub-agent type determining which tools it can use. "
                                + " ".join(
                                    f"{t.capitalize()}: {d}"
                                    for t, d in SUBAGENT_DESCS.items()
                                )
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
                },
            },
        }

    @staticmethod
    def threads_spec() -> dict:
        """Return OpenAI-format tool spec for the threads built-in."""
        return {
            "type": "function",
            "function": {
                "name": "threads",
                "description": (
                    "Query the execution history of subagent runs. "
                    "Returns a table showing id, type, status, runtime, and task "
                    "for all subagent invocations in this session."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        }
