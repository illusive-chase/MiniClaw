"""CCAgent — wraps claude-agent-sdk as an agent backend."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from miniclaw.activity import ActivityEvent, ActivityKind, ActivityStatus
from miniclaw.interactions import (
    InteractionRequest,
    InteractionResponse,
    InteractionType,
)
from miniclaw.providers.base import ChatMessage
from miniclaw.usage import UsageStats

from miniclaw2.agent.config import AgentConfig
from miniclaw2.cancellation import CancellationToken
from miniclaw2.types import (
    AgentEvent,
    HistoryUpdate,
    SessionControl,
    TextDelta,
)

logger = logging.getLogger(__name__)

_SESSION_MARKER = "__cc_session__:"


@dataclass
class _ClientEntry:
    """Tracks a live ClaudeSDKClient and the config it was created with."""

    client: ClaudeSDKClient
    model: str | None
    thinking: dict | None = None
    effort: str | None = None


class CCAgent:
    """Agent backend that delegates the agentic loop to Claude Agent SDK.

    Stateful: maintains persistent SDK clients across messages.
    """

    def __init__(
        self,
        system_prompt: str = "",
        default_model: str = "claude-sonnet-4-6",
        permission_mode: str = "default",
        allowed_tools: list[str] | None = None,
        cwd: str | None = None,
        max_turns: int | None = None,
        thinking: dict | None = None,
        effort: str | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._default_model = default_model
        self._permission_mode = permission_mode
        self._allowed_tools = allowed_tools
        self._cwd = cwd or "."
        self._max_turns = max_turns
        self._thinking = thinking
        self._effort = effort

        self._clients: dict[str, _ClientEntry] = {}
        self._output_queues: dict[str, asyncio.Queue] = {}
        self._usage: dict[str, UsageStats] = {}
        self._sdk_session_id: str | None = None

    # --- AgentProtocol ---

    @property
    def agent_type(self) -> str:
        return "ccagent"

    @property
    def default_model(self) -> str:
        return self._default_model

    async def process(
        self,
        text: str,
        history: list[ChatMessage],
        config: AgentConfig,
        token: CancellationToken,
    ) -> AsyncIterator[AgentEvent]:
        """Process a message via SDK. Yields AgentEvent items."""
        t0 = time.monotonic()
        key = "_default"

        client = await self._get_or_create_client(
            history, config.model or self._default_model
        )

        reply_parts: list[str] = []
        new_session_id: str | None = self._sdk_session_id
        pending_tools: dict[str, ActivityEvent] = {}

        # Set up output queue for interaction routing
        output_queue: asyncio.Queue = asyncio.Queue()
        self._output_queues[key] = output_queue

        async def _run_sdk() -> None:
            try:
                await client.query(text)
                async for message in client.receive_response():
                    await output_queue.put(("sdk", message))
                await output_queue.put(("done", None))
            except Exception as exc:
                await output_queue.put(("error", exc))

        task = asyncio.create_task(_run_sdk())

        try:
            while True:
                # Check cancellation between queue reads
                if token.is_cancelled:
                    task.cancel()
                    break

                try:
                    tag, payload = await asyncio.wait_for(output_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                if tag == "done":
                    break

                if tag == "error":
                    raise payload

                if tag == "plan_action":
                    yield SessionControl(
                        action="plan_execute",
                        payload={
                            "plan_content": payload.plan_content,
                            "permission_mode": payload.permission_mode,
                        },
                    )
                    break

                if tag == "interaction":
                    yield payload  # InteractionRequest — channel resolves
                    continue

                # tag == "sdk"
                message = payload

                if isinstance(message, TaskStartedMessage):
                    yield ActivityEvent(
                        kind=ActivityKind.AGENT,
                        status=ActivityStatus.START,
                        id=message.task_id,
                        name=message.task_type or "agent",
                        summary=message.description,
                    )

                elif isinstance(message, TaskProgressMessage):
                    yield ActivityEvent(
                        kind=ActivityKind.AGENT,
                        status=ActivityStatus.PROGRESS,
                        id=message.task_id,
                        name=message.last_tool_name or "",
                        summary=message.description,
                    )

                elif isinstance(message, TaskNotificationMessage):
                    yield ActivityEvent(
                        kind=ActivityKind.AGENT,
                        status=(
                            ActivityStatus.FINISH
                            if message.status == "completed"
                            else ActivityStatus.FAILED
                        ),
                        id=message.task_id,
                        name="",
                        summary=message.summary,
                    )

                elif isinstance(message, SystemMessage):
                    if message.subtype == "init":
                        new_session_id = message.data.get("session_id")

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            t = block.text if block.text.endswith("\n") else block.text + "\n"
                            reply_parts.append(t)
                            yield TextDelta(t)
                        elif isinstance(block, ToolUseBlock):
                            event = ActivityEvent(
                                kind=ActivityKind.TOOL,
                                status=ActivityStatus.START,
                                id=block.id,
                                name=block.name,
                                summary=str(block.input),
                            )
                            pending_tools[block.id] = event
                            yield event
                        elif isinstance(block, ThinkingBlock):
                            pass

                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            pending = pending_tools.pop(block.tool_use_id, None)
                            if pending is not None:
                                pending.status = (
                                    ActivityStatus.FAILED if block.is_error else ActivityStatus.FINISH
                                )
                                yield pending

                elif isinstance(message, ResultMessage):
                    self._usage.setdefault(key, UsageStats()).accumulate(message)

        except (CLINotFoundError, CLIConnectionError) as exc:
            error_msg = f"Claude Agent SDK error: {exc}"
            logger.error(error_msg)
            reply_parts = [error_msg]
            yield TextDelta(error_msg)
            await self._close_client(key)
        except Exception as exc:
            error_msg = f"Unexpected error: {exc}"
            logger.error(error_msg, exc_info=True)
            reply_parts = [error_msg]
            yield TextDelta(error_msg)
            await self._close_client(key)
        finally:
            self._output_queues.pop(key, None)
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Track SDK session ID
        if new_session_id:
            self._sdk_session_id = new_session_id

        reply = "".join(reply_parts) if reply_parts else "(no response)"

        # Build updated visible history
        visible_history = [
            m for m in history
            if not (m.role == "system" and m.content and m.content.startswith(_SESSION_MARKER))
        ]
        updated_history = list(visible_history)
        updated_history.append(ChatMessage(role="user", content=text))
        updated_history.append(ChatMessage(role="assistant", content=reply))
        if new_session_id:
            updated_history = self._inject_session_marker(updated_history, new_session_id)

        yield HistoryUpdate(history=updated_history)

    async def reset(self) -> None:
        """Close SDK client, forcing fresh client on next use."""
        await self._close_client("_default")
        self._sdk_session_id = None

    async def shutdown(self) -> None:
        """Close all managed clients."""
        for key in list(self._clients):
            await self._close_client(key)

    def serialize_state(self) -> dict:
        return {"sdk_session_id": self._sdk_session_id}

    async def restore_state(self, state: dict) -> None:
        self._sdk_session_id = state.get("sdk_session_id")

    async def on_fork(self, source_state: dict) -> dict:
        # Fresh SDK for forked session — don't reuse session
        return {}

    # --- Usage ---

    def get_usage(self) -> UsageStats:
        return self._usage.get("_default", UsageStats())

    def get_effort(self) -> str | None:
        return self._effort

    def set_effort(self, effort: str | None) -> None:
        self._effort = effort

    # --- SDK session markers ---

    @staticmethod
    def _extract_sdk_session_id(history: list[ChatMessage]) -> str | None:
        for msg in history:
            if msg.role == "system" and msg.content and msg.content.startswith(_SESSION_MARKER):
                return msg.content[len(_SESSION_MARKER):]
        return None

    @staticmethod
    def _inject_session_marker(
        history: list[ChatMessage], sdk_session_id: str
    ) -> list[ChatMessage]:
        marker = ChatMessage(role="system", content=f"{_SESSION_MARKER}{sdk_session_id}")
        filtered = [
            m for m in history
            if not (m.role == "system" and m.content and m.content.startswith(_SESSION_MARKER))
        ]
        return [marker] + filtered

    # --- Client management ---

    def _make_can_use_tool(self, client_key: str):
        """Create a can_use_tool callback bound to a specific client key."""

        async def callback(
            tool_name: str,
            tool_input: dict[str, Any],
            context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            queue = self._output_queues.get(client_key)
            if queue is None:
                return PermissionResultAllow()

            if tool_name == "AskUserQuestion":
                itype = InteractionType.ASK_USER
            elif tool_name == "ExitPlanMode":
                itype = InteractionType.PLAN_APPROVAL
            else:
                itype = InteractionType.PERMISSION

            loop = asyncio.get_running_loop()
            future = loop.create_future()

            request = InteractionRequest(
                id=str(uuid4()),
                type=itype,
                tool_name=tool_name,
                tool_input=tool_input,
                suggestions=list(context.suggestions) if context.suggestions else [],
                _future=future,
            )

            await queue.put(("interaction", request))
            response: InteractionResponse = await future

            # ExitPlanMode branching
            if itype == InteractionType.PLAN_APPROVAL:
                if response.clear_context:
                    from miniclaw.interactions import PlanExecuteAction

                    action = PlanExecuteAction(
                        plan_content=response.message or "Execute the plan as discussed.",
                        permission_mode=response.permission_mode or "acceptEdits",
                    )
                    await queue.put(("plan_action", action))
                    return PermissionResultDeny(interrupt=True)

                if response.allow and response.permission_mode:
                    return PermissionResultAllow(
                        updated_permissions=[
                            PermissionUpdate(
                                type="setMode",
                                mode=response.permission_mode,
                                destination="session",
                            )
                        ],
                    )

                if not response.allow:
                    return PermissionResultDeny(message=response.message)

            if response.allow:
                return PermissionResultAllow(updated_input=response.updated_input)
            else:
                return PermissionResultDeny(message=response.message)

        return callback

    def _build_options(
        self,
        sdk_session_id: str | None,
        model: str | None,
        client_key: str,
    ) -> ClaudeAgentOptions:
        opts: dict = {}

        system_prompt_config: dict[str, str] = {
            "type": "preset",
            "preset": "claude_code",
        }
        if self._system_prompt:
            system_prompt_config["append"] = self._system_prompt
        opts["system_prompt"] = system_prompt_config

        if sdk_session_id:
            opts["resume"] = sdk_session_id
        if model:
            opts["model"] = model
        if self._permission_mode:
            opts["permission_mode"] = self._permission_mode
        if self._allowed_tools:
            opts["allowed_tools"] = self._allowed_tools
        if self._cwd:
            opts["cwd"] = self._cwd
        if self._max_turns is not None:
            opts["max_turns"] = self._max_turns

        opts["can_use_tool"] = self._make_can_use_tool(client_key)

        if self._thinking is not None:
            opts["thinking"] = self._thinking
        if self._effort is not None:
            opts["effort"] = self._effort

        return ClaudeAgentOptions(**opts)

    async def _get_or_create_client(
        self,
        history: list[ChatMessage],
        model: str | None,
    ) -> ClaudeSDKClient:
        key = "_default"
        effective_model = model or self._default_model

        entry = self._clients.get(key)
        if entry is not None:
            config_changed = (
                entry.model != effective_model
                or entry.thinking != self._thinking
                or entry.effort != self._effort
            )
            if config_changed:
                await self._close_client(key)
            else:
                return entry.client

        sdk_session_id = self._sdk_session_id or self._extract_sdk_session_id(history)
        options = self._build_options(sdk_session_id, model, client_key=key)

        client = ClaudeSDKClient(options=options)
        await client.__aenter__()
        self._clients[key] = _ClientEntry(
            client=client, model=effective_model,
            thinking=self._thinking, effort=self._effort,
        )
        return client

    async def _close_client(self, key: str) -> None:
        entry = self._clients.pop(key, None)
        if entry is not None:
            try:
                await entry.client.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("Error closing client: %s", exc)
