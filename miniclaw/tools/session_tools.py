"""Session management tools — launch, reply, message, check, cancel, wait sub-agents."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from miniclaw.runtime_context import SpawnLimitError
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
            "Launch a background sub-agent session that runs autonomously.\n\n"
            "IMPORTANT — async behavior:\n"
            "- This tool returns immediately with a session ID. The sub-agent runs in the background.\n"
            "- You will receive a notification automatically when the sub-agent completes or needs permission.\n"
            "- Do NOT re-launch agents for the same task. If you haven't received results yet, either:\n"
            "  (a) Use wait_agent to block until completion, or\n"
            "  (b) End your current turn and wait for the notification.\n"
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
                "remote": {
                    "type": "string",
                    "description": (
                        "Optional remote target. A config key from 'remotes' "
                        "(e.g. 'server1') or a raw ws:// URL. If provided, the "
                        "sub-agent runs on a remote daemon."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Optional working directory. If provided, the sub-agent "
                        "runs in this directory instead."
                    ),
                },
            },
            "required": ["type", "task"],
        }

    async def execute(self, args: dict) -> ToolResult:
        agent_type = args.get("type", "ccagent")
        task = args.get("task", "")
        remote = args.get("remote")
        cwd = args.get("cwd")

        if not task:
            return ToolResult(output="Error: 'task' is required.", success=False)

        try:
            session_id, warning = await self._ctx.spawn(
                agent_type=agent_type,
                task=task,
                remote=remote,
                cwd=cwd,
            )
            location = f" (remote: {remote})" if remote else ""
            output = (
                f"Sub-agent launched successfully{location}.\n"
                f"Session ID: {session_id}\n"
                f"Type: {agent_type}\n"
                f"Task: {task[:200]}\n"
                f"CWD: {cwd or 'default'}\n"
            )
            if warning:
                output += warning
            return ToolResult(output=output)
        except SpawnLimitError as e:
            return ToolResult(output=f"Spawn blocked: {e}", success=False)
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
            "sub-agent. The notification message contains the interaction_id — "
            "use that exact value. For AskUserQuestion, include your answers."
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
                    "description": "The interaction ID from the notification.",
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
                "answers": {
                    "type": "object",
                    "description": (
                        "Answers for an AskUserQuestion interaction. "
                        "Keys are the question text, values are the chosen answer."
                    ),
                },
            },
            "required": ["session_id", "interaction_id", "action"],
        }

    async def execute(self, args: dict) -> ToolResult:
        session_id = args.get("session_id", "")
        interaction_id = args.get("interaction_id", "")
        action = args.get("action", "")
        reason = args.get("reason")
        answers = args.get("answers")

        if not session_id or not interaction_id or not action:
            return ToolResult(
                output="Error: session_id, interaction_id, and action are required.",
                success=False,
            )

        result = self._ctx.resolve(session_id, interaction_id, action, reason, answers)
        return ToolResult(output=result)


class MessageAgentTool(Tool):
    """Send a follow-up message to a idle sub-agent."""

    _manual_registration = True

    def __init__(self, runtime_context: RuntimeContext) -> None:
        self._ctx = runtime_context

    def name(self) -> str:
        return "message_agent"

    def description(self) -> str:
        return (
            "Send a follow-up message to an IDLE sub-agent (status: completed or waiting).\n\n"
            "WARNING: This is for providing additional instructions AFTER a sub-agent has completed a turn.\n"
            "Do NOT use this to 'nudge' or check on running agents — they will notify you when done.\n"
            "If the agent is still running, your message will be queued until it finishes."
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
            "List all background sub-agents, their status (running/completed/failed/interrupted), "
            "and result preview.\n"
            "Use this BEFORE launching new agents to avoid duplicating work already in progress."
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
        return (
            "Cancel a running sub-agent. Use only when:\n"
            "- The agent's task is no longer needed\n"
            "- You want to change approach and re-launch with different instructions\n"
            "Do NOT cancel agents just because they haven't responded yet — they may still be working."
        )

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


class WaitAgentTool(Tool):
    """Wait for sub-agents to complete and return their results."""

    _manual_registration = True

    def __init__(self, runtime_context: RuntimeContext) -> None:
        self._ctx = runtime_context

    def name(self) -> str:
        return "wait_agent"

    def description(self) -> str:
        return (
            "Wait for background sub-agents to complete and return their results.\n\n"
            "- If session_ids is provided, waits for those specific agents.\n"
            "- If session_ids is omitted, waits for ALL currently running agents.\n"
            "- Returns the combined results of all waited agents.\n"
            "- Use this after launch_agent when you need the results before continuing.\n\n"
            "Pattern: launch_agent -> wait_agent -> use results in your response."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "session_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of sub-agent session IDs to wait for. "
                        "If omitted, waits for all currently running agents."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        "Maximum seconds to wait (default 300). Returns whatever "
                        "results are available when the timeout is reached."
                    ),
                },
            },
        }

    async def execute(self, args: dict) -> ToolResult:
        session_ids = args.get("session_ids")
        timeout = args.get("timeout", 300)

        # Collect target drivers
        if session_ids:
            drivers = {
                sid: self._ctx._drivers[sid]
                for sid in session_ids
                if sid in self._ctx._drivers
            }
            if not drivers:
                return ToolResult(
                    output="No matching sub-agent sessions found.",
                    success=False,
                )
        else:
            drivers = {
                sid: d
                for sid, d in self._ctx._drivers.items()
                if d.status == "running"
            }

        if not drivers:
            return ToolResult(output="No running agents to wait for.")

        # Wait for all to complete (with timeout + cancellation check)
        poll_interval = 2.0
        elapsed = 0.0
        while elapsed < timeout:
            all_done = all(d.status != "running" for d in drivers.values())
            if all_done:
                break
            # Check parent cancellation
            token = self._ctx._parent._current_token
            if token and token.is_cancelled:
                break
            await asyncio.sleep(min(poll_interval, timeout - elapsed))
            elapsed += poll_interval

        # Collect results
        results = []
        for sid, driver in drivers.items():
            results.append(
                f"[{sid}] Status: {driver.status}\n"
                f"{driver.result or '(no result)'}"
            )
        return ToolResult(output="\n\n---\n\n".join(results))
