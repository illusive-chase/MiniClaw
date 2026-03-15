"""Session management tools — launch, reply, message, check, cancel sub-agents."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from miniclaw.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from miniclaw.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)


class LaunchAgentTool(Tool):
    """Launch a background sub-agent session."""

    _manual_registration = True

    def __init__(self, runtime_context: RuntimeContext) -> None:
        self._ctx = runtime_context

    def name(self) -> str:
        return "launch_agent"

    def description(self) -> str:
        return (
            "Launch a background sub-agent session. The sub-agent runs autonomously "
            "and you will be notified of its progress, completion, or if it needs "
            "permission for a tool. Use check_agents to monitor status and "
            "reply_agent to handle permission requests."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": (
                        "Agent type to spawn. Usually 'ccagent' for a Claude Code "
                        "backed sub-agent, or 'native' for a tool-loop agent."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": "The task instruction for the sub-agent.",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of tool names the sub-agent is pre-authorized to use "
                        "without asking for permission. Other tools will require "
                        "your approval via reply_agent."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for the sub-agent.",
                },
            },
            "required": ["type", "task"],
        }

    async def execute(self, args: dict) -> ToolResult:
        agent_type = args.get("type", "ccagent")
        task = args.get("task", "")
        allowed_tools = args.get("allowed_tools", [])
        model = args.get("model")

        if not task:
            return ToolResult(output="Error: 'task' is required.", success=False)

        config = {}
        if model:
            config["model"] = model

        try:
            session_id = await self._ctx.spawn(
                agent_type=agent_type,
                task=task,
                config=config if config else None,
                allowed_tools=allowed_tools,
            )
            return ToolResult(
                output=(
                    f"Sub-agent launched successfully.\n"
                    f"Session ID: {session_id}\n"
                    f"Type: {agent_type}\n"
                    f"Task: {task[:200]}\n"
                    f"Allowed tools: {', '.join(allowed_tools) if allowed_tools else '(none — all require approval)'}"
                )
            )
        except Exception as e:
            return ToolResult(output=f"Failed to launch sub-agent: {e}", success=False)


class ReplyAgentTool(Tool):
    """Reply to a pending interaction from a sub-agent."""

    _manual_registration = True

    def __init__(self, runtime_context: RuntimeContext) -> None:
        self._ctx = runtime_context

    def name(self) -> str:
        return "reply_agent"

    def description(self) -> str:
        return (
            "Reply to a pending permission request or question from a background "
            "sub-agent. Use check_agents to see pending interactions."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The sub-agent session ID.",
                },
                "interaction_id": {
                    "type": "string",
                    "description": "The interaction ID to respond to.",
                },
                "action": {
                    "type": "string",
                    "enum": ["allow", "deny"],
                    "description": "Whether to allow or deny the requested action.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for the decision.",
                },
            },
            "required": ["session_id", "interaction_id", "action"],
        }

    async def execute(self, args: dict) -> ToolResult:
        session_id = args.get("session_id", "")
        interaction_id = args.get("interaction_id", "")
        action = args.get("action", "")
        reason = args.get("reason")

        if not session_id or not interaction_id or not action:
            return ToolResult(
                output="Error: session_id, interaction_id, and action are required.",
                success=False,
            )

        result = self._ctx.resolve(session_id, interaction_id, action, reason)
        return ToolResult(output=result)


class MessageAgentTool(Tool):
    """Send a follow-up message to a running sub-agent."""

    _manual_registration = True

    def __init__(self, runtime_context: RuntimeContext) -> None:
        self._ctx = runtime_context

    def name(self) -> str:
        return "message_agent"

    def description(self) -> str:
        return (
            "Send a follow-up message to a running background sub-agent. "
            "Use this to provide additional context or instructions."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The sub-agent session ID.",
                },
                "text": {
                    "type": "string",
                    "description": "The message text to send.",
                },
            },
            "required": ["session_id", "text"],
        }

    async def execute(self, args: dict) -> ToolResult:
        session_id = args.get("session_id", "")
        text = args.get("text", "")

        if not session_id or not text:
            return ToolResult(
                output="Error: session_id and text are required.",
                success=False,
            )

        result = await self._ctx.send(session_id, text)
        return ToolResult(output=result)


class CheckAgentsTool(Tool):
    """Check status of all background sub-agents."""

    _manual_registration = True

    def __init__(self, runtime_context: RuntimeContext) -> None:
        self._ctx = runtime_context

    def name(self) -> str:
        return "check_agents"

    def description(self) -> str:
        return (
            "List all background sub-agents spawned from this session, "
            "their status, result preview, and any pending interactions "
            "that need your response."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, args: dict) -> ToolResult:
        agents = self._ctx.list_agents()
        if not agents:
            return ToolResult(output="No sub-agents have been launched.")

        lines = []
        for a in agents:
            lines.append(f"Session: {a['session_id']}")
            lines.append(f"  Status: {a['status']}")
            if a["result_preview"]:
                lines.append(f"  Result: {a['result_preview']}")
            if a["pending_interactions"]:
                lines.append(
                    f"  Pending interactions: {', '.join(a['pending_interactions'])}"
                )
            lines.append("")

        return ToolResult(output="\n".join(lines))


class CancelAgentTool(Tool):
    """Cancel a running background sub-agent."""

    _manual_registration = True

    def __init__(self, runtime_context: RuntimeContext) -> None:
        self._ctx = runtime_context

    def name(self) -> str:
        return "cancel_agent"

    def description(self) -> str:
        return "Cancel (interrupt) a running background sub-agent session."

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The sub-agent session ID to cancel.",
                },
            },
            "required": ["session_id"],
        }

    async def execute(self, args: dict) -> ToolResult:
        session_id = args.get("session_id", "")
        if not session_id:
            return ToolResult(
                output="Error: session_id is required.",
                success=False,
            )

        result = self._ctx.cancel(session_id)
        return ToolResult(output=result)
