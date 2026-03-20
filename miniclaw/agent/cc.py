"""CCAgent — wraps claude-agent-sdk as an agent backend."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
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
from miniclaw.agent.config import AgentConfig
from miniclaw.cancellation import CancellationToken
from miniclaw.interactions import (
    InteractionRequest,
    InteractionResponse,
    InteractionType,
)
from miniclaw.log import truncate
from miniclaw.providers.base import ChatMessage
from miniclaw.types import (
    AgentEvent,
    HistoryUpdate,
    SessionControl,
    TextDelta,
    UsageEvent,
)
from miniclaw.usage import TokenUsage, UsageStats

logger = logging.getLogger(__name__)

_SESSION_MARKER = "__cc_session__:"


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
        context_window: int = 0,
    ) -> None:
        self._system_prompt = system_prompt
        self._default_model = default_model
        self._permission_mode = permission_mode
        self._allowed_tools = allowed_tools
        self._cwd = cwd or "."
        self._max_turns = max_turns
        self._thinking = thinking
        self._effort = effort
        self._context_window = context_window

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

        effective_model = config.model or self._default_model
        logger.info(
            "[CC] process start: text_len=%d, history_len=%d, model=%s",
            len(text), len(history), effective_model,
        )
        sdk_session_id = self._sdk_session_id or self._extract_sdk_session_id(history)
        plugctx_prompt = config.extra.get("_plugctx_prompt", "")
        effective_cwd = config.extra.get("_effective_cwd") or self._cwd
        runtime_env = config.extra.get("_runtime_env")
        options = self._build_options(sdk_session_id, effective_model, client_key=key, extra_prompt=plugctx_prompt, cwd=effective_cwd, env=runtime_env)
        logger.debug("[CC] Client ready: sdk_session_id=%s", sdk_session_id)

        reply_parts: list[str] = []
        new_session_id: str | None = self._sdk_session_id
        pending_tools: dict[str, ActivityEvent] = {}
        turn_usage = UsageStats()  # per-message usage (yielded to channel)
        last_token_usage: TokenUsage | None = None
        last_context_tokens: int = 0
        text_tail = ""       # last 2 chars of yielded text (for block-sep detection)
        had_nontext = False  # a non-text event was yielded since last TextDelta

        # Set up output queue for interaction routing
        output_queue: asyncio.Queue = asyncio.Queue()
        self._output_queues[key] = output_queue

        async def _run_sdk() -> None:
            try:
                async with ClaudeSDKClient(options=options) as client:
                    logger.debug("[CC] SDK query task started")
                    await client.query(text)
                    async for message in client.receive_response():
                        await output_queue.put(("sdk", message))
                    await output_queue.put(("done", None))
                    logger.debug("[CC] SDK query task completed")
            except Exception as exc:
                logger.debug("[CC] SDK query task error: %s", exc)
                await output_queue.put(("error", exc))

        task = asyncio.create_task(_run_sdk())

        try:
            while True:
                try:
                    tag, payload = await asyncio.wait_for(output_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    # Only check cancellation when the queue is idle so
                    # that a pending plan_action is never skipped due to
                    # a token that was cancelled by the SDK interrupt.
                    if token.is_cancelled:
                        task.cancel()
                        break
                    continue

                if tag == "done":
                    break

                if tag == "error":
                    raise payload

                if tag == "plan_action":
                    logger.info("[CC] PlanExecuteAction received")
                    yield SessionControl(
                        action="plan_execute",
                        payload={
                            "plan_content": payload.plan_content,
                            "permission_mode": payload.permission_mode,
                        },
                    )
                    break

                if tag == "interaction":
                    logger.info(
                        "[CC] InteractionRequest: type=%s, tool=%s, id=%s",
                        payload.type.value if hasattr(payload.type, 'value') else payload.type,
                        payload.tool_name, payload.id,
                    )
                    yield payload  # InteractionRequest — channel resolves
                    had_nontext = True
                    continue

                # tag == "sdk"
                message = payload

                if isinstance(message, TaskStartedMessage):
                    logger.debug(
                        "[CC] TaskStarted: id=%s, type=%s",
                        message.task_id, message.task_type,
                    )
                    yield ActivityEvent(
                        kind=ActivityKind.AGENT,
                        status=ActivityStatus.START,
                        id=message.task_id,
                        name=message.task_type or "agent",
                        summary=message.description,
                    )
                    had_nontext = True

                elif isinstance(message, TaskProgressMessage):
                    logger.debug(
                        "[CC] TaskProgress: id=%s, tool=%s",
                        message.task_id, message.last_tool_name,
                    )
                    yield ActivityEvent(
                        kind=ActivityKind.AGENT,
                        status=ActivityStatus.PROGRESS,
                        id=message.task_id,
                        name=message.last_tool_name or "",
                        summary=message.description,
                    )
                    had_nontext = True

                elif isinstance(message, TaskNotificationMessage):
                    logger.debug(
                        "[CC] TaskNotification: id=%s, status=%s",
                        message.task_id, message.status,
                    )
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
                    had_nontext = True

                elif isinstance(message, SystemMessage):
                    if message.subtype == "init":
                        new_session_id = message.data.get("session_id")
                        logger.debug("[CC] SystemMessage(init): session_id=%s", new_session_id)

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            t = block.text if block.text.endswith("\n") else block.text + "\n"
                            # Ensure markdown block separation after non-text events
                            if had_nontext and text_tail:
                                if not text_tail.endswith("\n\n"):
                                    sep = "\n" if text_tail.endswith("\n") else "\n\n"
                                    yield TextDelta(sep)
                                had_nontext = False
                            reply_parts.append(t)
                            yield TextDelta(t)
                            text_tail = (text_tail + t)[-2:]
                        elif isinstance(block, ToolUseBlock):
                            logger.info(
                                "[CC] ToolUseBlock: name=%s, id=%s, input=%s",
                                block.name, block.id,
                                truncate(str(block.input)),
                            )
                            event = ActivityEvent(
                                kind=ActivityKind.TOOL,
                                status=ActivityStatus.START,
                                id=block.id,
                                name=block.name,
                                summary=str(block.input),
                            )
                            pending_tools[block.id] = event
                            yield event
                            had_nontext = True
                        elif isinstance(block, ThinkingBlock):
                            pass

                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            logger.info(
                                "[CC] ToolResultBlock: id=%s, is_error=%s",
                                block.tool_use_id, block.is_error,
                            )
                            pending = pending_tools.pop(block.tool_use_id, None)
                            if pending is not None:
                                pending.status = (
                                    ActivityStatus.FAILED if block.is_error else ActivityStatus.FINISH
                                )
                                yield pending
                                had_nontext = True

                elif isinstance(message, ResultMessage):
                    self._usage.setdefault(key, UsageStats()).accumulate(message)
                    turn_usage.accumulate(message)

                    # Extract per-call TokenUsage for rich display
                    if message.usage:
                        u = message.usage
                        last_token_usage = TokenUsage(
                            input_tokens=u.get("input_tokens", 0),
                            output_tokens=u.get("output_tokens", 0),
                            cache_read_tokens=u.get("cache_read_input_tokens", 0),
                            cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
                        )
                        last_context_tokens = (
                            last_token_usage.input_tokens +
                            last_token_usage.cache_read_tokens +
                            last_token_usage.cache_creation_tokens
                        )

                    logger.info(
                        "[CC] ResultMessage: duration_ms=%d, api_ms=%d, "
                        "input=%d, output=%d, cache_read=%d, cache_write=%d, "
                        "num_turns=%d, cost=$%.4f",
                        message.duration_ms, message.duration_api_ms,
                        last_token_usage.input_tokens if last_token_usage else 0,
                        last_token_usage.output_tokens if last_token_usage else 0,
                        last_token_usage.cache_read_tokens if last_token_usage else 0,
                        last_token_usage.cache_creation_tokens if last_token_usage else 0,
                        message.num_turns,
                        message.total_cost_usd or 0.0,
                    )

                    # Intermediate usage update — lets the channel show running token count
                    yield UsageEvent(
                        usage=turn_usage.copy(), final=False,
                        context_tokens=last_context_tokens,
                        context_window=self._context_window or None,
                        last_usage=last_token_usage,
                    )

                else:
                    logger.warning(
                        "[CC] Unknown SDK message type: %s",
                        type(message).__name__,
                    )

        except (CLINotFoundError, CLIConnectionError) as exc:
            error_msg = f"Claude Agent SDK error: {exc}"
            logger.error(error_msg)
            reply_parts = [error_msg]
            yield TextDelta(error_msg)
        except Exception as exc:
            error_msg = f"Unexpected error: {exc}"
            logger.error(error_msg, exc_info=True)
            reply_parts = [error_msg]
            yield TextDelta(error_msg)
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

        logger.info(
            "[CC] process end: duration_ms=%d, reply_len=%d, history_len=%d",
            int((time.monotonic() - t0) * 1000),
            len(reply), len(updated_history),
        )

        yield UsageEvent(
            usage=turn_usage,
            context_tokens=last_context_tokens,
            context_window=self._context_window or None,
            last_usage=last_token_usage,
        )
        yield HistoryUpdate(history=updated_history)

    async def reset(self) -> None:
        """Reset session state, forcing fresh SDK session on next use."""
        logger.info("[CC] reset: closing client and clearing session")
        self._sdk_session_id = None

    async def shutdown(self) -> None:
        """Clean up agent resources."""
        logger.info("[CC] shutdown: closing client")
        pass

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

            logger.debug(
                "[CC] can_use_tool callback: tool=%s, interaction_type=%s",
                tool_name, itype.value if hasattr(itype, 'value') else itype,
            )

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
        extra_prompt: str = "",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ClaudeAgentOptions:
        opts: dict = {}

        system_prompt_config: dict[str, str] = {
            "type": "preset",
            "preset": "claude_code",
        }
        append_parts = []
        if self._system_prompt:
            append_parts.append(self._system_prompt)
        if extra_prompt:
            append_parts.append(extra_prompt)
        if append_parts:
            system_prompt_config["append"] = "\n\n".join(append_parts)
        opts["system_prompt"] = system_prompt_config

        if sdk_session_id:
            opts["resume"] = sdk_session_id
        if model:
            opts["model"] = model
        if self._permission_mode:
            opts["permission_mode"] = self._permission_mode
        if self._allowed_tools:
            opts["allowed_tools"] = self._allowed_tools

        effective_cwd = cwd or self._cwd
        if effective_cwd:
            opts["cwd"] = effective_cwd
        if self._max_turns is not None:
            opts["max_turns"] = self._max_turns

        opts["can_use_tool"] = self._make_can_use_tool(client_key)

        if self._thinking is not None:
            opts["thinking"] = self._thinking
        if self._effort is not None:
            opts["effort"] = self._effort

        # Inject runtime environment variables
        if env:
            opts["env"] = env

        return ClaudeAgentOptions(**opts)

