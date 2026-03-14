"""CCAgent — wraps claude-agent-sdk as an alternative agent backend."""

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
    PlanExecuteAction,
)
from miniclaw.providers.base import ChatMessage

logger = logging.getLogger(__name__)

_SESSION_MARKER = "__cc_session__:"


@dataclass
class _ClientEntry:
    """Tracks a live ClaudeSDKClient and the model it was created with."""

    client: ClaudeSDKClient
    model: str | None


class CCAgent:
    """Agent backend that delegates the agentic loop to Claude Agent SDK.

    Uses a pool of persistent ``ClaudeSDKClient`` instances keyed by
    session_id, giving us multi-turn conversation without per-message
    process startup overhead.

    Implements the same ``process_message()`` interface as ``Agent`` so
    Gateway can work with both interchangeably.
    """

    def __init__(
        self,
        system_prompt: str = "",
        default_model: str | None = None,
        permission_mode: str = "default",
        allowed_tools: list[str] | None = None,
        cwd: str | None = None,
        max_turns: int | None = None,
    ):
        self._system_prompt = system_prompt
        self._default_model = default_model  # Gateway reads this directly
        self._permission_mode = permission_mode
        self._allowed_tools = allowed_tools
        self._cwd = cwd or "."
        self._max_turns = max_turns
        self._last_history: list[ChatMessage] | None = None
        self._clients: dict[str, _ClientEntry] = {}
        # Per-client output queues for interaction routing
        self._output_queues: dict[str, asyncio.Queue] = {}

    # --- SDK session mapping via history marker ---

    @staticmethod
    def _extract_sdk_session_id(history: list[ChatMessage]) -> str | None:
        """Scan history for the SDK session marker, return session_id or None."""
        for msg in history:
            if msg.role == "system" and msg.content and msg.content.startswith(_SESSION_MARKER):
                return msg.content[len(_SESSION_MARKER):]
        return None

    @staticmethod
    def _inject_session_marker(
        history: list[ChatMessage], sdk_session_id: str
    ) -> list[ChatMessage]:
        """Insert or update the session marker at the start of history."""
        marker = ChatMessage(role="system", content=f"{_SESSION_MARKER}{sdk_session_id}")
        # Replace existing marker if present
        filtered = [m for m in history if not (m.role == "system" and m.content and m.content.startswith(_SESSION_MARKER))]
        return [marker] + filtered

    # --- can_use_tool callback ---

    def _make_can_use_tool(self, client_key: str):
        """Create a can_use_tool callback bound to a specific client key."""

        async def callback(
            tool_name: str,
            tool_input: dict[str, Any],
            context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            queue = self._output_queues.get(client_key)
            if queue is None:
                # No active stream — auto-allow (e.g., non-streaming process_message)
                return PermissionResultAllow()

            # Classify interaction type by tool name
            if tool_name == "AskUserQuestion":
                itype = InteractionType.ASK_USER
            elif tool_name == "ExitPlanMode":
                itype = InteractionType.PLAN_APPROVAL
            else:
                itype = InteractionType.PERMISSION

            # Create a Future that the channel will resolve
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

            # Push to output queue — the stream generator will yield this
            await queue.put(("interaction", request))

            # Block until the channel resolves the interaction
            response: InteractionResponse = await future

            # --- ExitPlanMode branching ---
            if itype == InteractionType.PLAN_APPROVAL:
                if response.clear_context:
                    # Option 1: clear context + execute plan
                    plan_content = response.message or "Execute the plan as discussed."
                    action = PlanExecuteAction(
                        plan_content=plan_content,
                        permission_mode=response.permission_mode or "acceptEdits",
                    )
                    await queue.put(("plan_action", action))
                    return PermissionResultDeny(interrupt=True)

                if response.allow and response.permission_mode:
                    # Options 2/3: approve plan and switch permission mode
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
                    # Option 4: keep planning
                    return PermissionResultDeny(message=response.message)

            # --- Default handling for non-plan interactions ---
            if response.allow:
                return PermissionResultAllow(
                    updated_input=response.updated_input,
                )
            else:
                return PermissionResultDeny(
                    message=response.message,
                )

        return callback

    # --- Options builder ---

    def _build_options(
        self,
        sdk_session_id: str | None,
        model: str | None,
        client_key: str,
        permission_mode_override: str | None = None,
    ) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions for a new client."""
        effective_model = model or self._default_model
        effective_permission_mode = permission_mode_override or self._permission_mode
        opts: dict = {}

        # System prompt — use Claude Code preset, optionally with appended text
        system_prompt_config: dict[str, str] = {
            "type": "preset",
            "preset": "claude_code",
        }
        if self._system_prompt:
            system_prompt_config["append"] = self._system_prompt
        opts["system_prompt"] = system_prompt_config

        if sdk_session_id:
            opts["resume"] = sdk_session_id
        if effective_model:
            opts["model"] = effective_model
        if effective_permission_mode:
            opts["permission_mode"] = effective_permission_mode
        if self._allowed_tools:
            opts["allowed_tools"] = self._allowed_tools
        if self._cwd:
            opts["cwd"] = self._cwd
        if self._max_turns is not None:
            opts["max_turns"] = self._max_turns

        # Attach the interaction callback
        opts["can_use_tool"] = self._make_can_use_tool(client_key)

        return ClaudeAgentOptions(**opts)

    # --- Client pool management ---

    async def _get_or_create_client(
        self,
        session_id: str | None,
        history: list[ChatMessage],
        model: str | None,
        permission_mode_override: str | None = None,
    ) -> ClaudeSDKClient:
        """Return an existing client or create a new one for the session."""
        effective_model = model or self._default_model
        key = session_id or "_default"

        # Check for existing client (skip reuse if permission_mode_override forces a new client)
        entry = self._clients.get(key)
        if entry is not None and permission_mode_override is None:
            # If model changed, close old client and create new one
            if entry.model != effective_model:
                logger.info(
                    "Model changed (%s -> %s), recreating client (session=%s)",
                    entry.model, effective_model, key,
                )
                await self._close_client(key)
            else:
                logger.info("Reusing existing client (session=%s)", key)
                logger.debug("Active clients: %d", len(self._clients))
                return entry.client
        elif entry is not None and permission_mode_override is not None:
            logger.info(
                "Permission mode override (%s), recreating client (session=%s)",
                permission_mode_override, key,
            )
            await self._close_client(key)

        # Extract SDK session from history for cross-restart resume
        sdk_session_id = self._extract_sdk_session_id(history)
        logger.debug("SDK session extracted from history: %s", sdk_session_id)

        options = self._build_options(
            sdk_session_id, model, client_key=key,
            permission_mode_override=permission_mode_override,
        )
        logger.debug("ClaudeAgentOptions: %s", options)
        logger.info(
            "Creating ClaudeSDKClient (session=%s, resume=%s, model=%s)",
            key, sdk_session_id, effective_model,
        )

        client = ClaudeSDKClient(options=options)
        await client.__aenter__()
        self._clients[key] = _ClientEntry(client=client, model=effective_model)
        logger.debug("Active clients: %d", len(self._clients))
        return client

    async def _close_client(self, key: str) -> None:
        """Close and remove a single client by key."""
        entry = self._clients.pop(key, None)
        if entry is not None:
            try:
                await entry.client.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("Error closing client (session=%s): %s", key, exc)

    async def reset_client(self, session_id: str | None = None) -> None:
        """Close a client by session_id, forcing a fresh client on next use."""
        key = session_id or "_default"
        logger.info("Resetting client (session=%s)", key)
        await self._close_client(key)

    async def aclose(self) -> None:
        """Close all managed clients. Call on shutdown."""
        count = len(self._clients)
        if count:
            logger.info("Closing %d client(s)", count)
        for key in list(self._clients):
            await self._close_client(key)

    # --- Core interface ---

    async def process_message(
        self,
        user_text: str,
        history: list[ChatMessage],
        model: str | None = None,
        session_id: str | None = None,
    ) -> tuple[str, list[ChatMessage]]:
        """Process a message. Returns (reply, updated_history).

        Same interface as Agent.process_message().
        Note: interactions auto-allow when no output queue is active.
        """
        t0 = time.monotonic()
        logger.debug("Message processing started (session=%s)", session_id)

        client = await self._get_or_create_client(session_id, history, model)

        reply_parts: list[str] = []
        new_session_id: str | None = self._extract_sdk_session_id(history)

        try:
            await client.query(user_text)

            async for message in client.receive_response():
                logger.info("Received %s message", type(message).__name__)

                if isinstance(message, SystemMessage):
                    logger.debug("SystemMessage[%s]: %s", message.subtype, message.data)
                    if message.subtype == "init":
                        new_session_id = message.data.get("session_id")

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text = block.text if block.text.endswith("\n") else block.text + "\n"
                            reply_parts.append(text)
                        elif isinstance(block, ToolUseBlock):
                            logger.info("Tool call: %s", block.name)
                            logger.debug("Tool args: %s(%s)", block.name, block.input)

                elif isinstance(message, ResultMessage):
                    if message.result:
                        reply_parts.append(message.result)
                    elapsed = time.monotonic() - t0
                    total_chars = sum(len(p) for p in reply_parts)
                    logger.info(
                        "Completed in %.1fs — %d chars (session=%s)",
                        elapsed, total_chars, session_id,
                    )

        except (CLINotFoundError, CLIConnectionError) as exc:
            error_msg = f"Claude Agent SDK error: {exc}"
            logger.error(error_msg)
            reply_parts = [error_msg]
            await self._close_client(session_id or "_default")
        except Exception as exc:
            error_msg = f"Unexpected error: {exc}"
            logger.error(error_msg, exc_info=True)
            reply_parts = [error_msg]
            await self._close_client(session_id or "_default")

        reply = "".join(reply_parts) if reply_parts else "(no response)"

        # Build updated history
        visible_history = [m for m in history if not (m.role == "system" and m.content and m.content.startswith(_SESSION_MARKER))]
        updated_history = list(visible_history)
        updated_history.append(ChatMessage(role="user", content=user_text))
        updated_history.append(ChatMessage(role="assistant", content=reply))
        if new_session_id:
            updated_history = self._inject_session_marker(updated_history, new_session_id)

        self._last_history = updated_history
        return reply, updated_history

    async def process_message_stream(
        self,
        user_text: str,
        history: list[ChatMessage],
        model: str | None = None,
        session_id: str | None = None,
        permission_mode: str | None = None,
    ) -> AsyncIterator[str | tuple[str, list[ChatMessage]] | InteractionRequest | PlanExecuteAction | ActivityEvent]:
        """Stream a response.

        Yields:
            str — text chunks for progressive display
            InteractionRequest — permission/question/plan-approval requests
            ActivityEvent — tool/subagent lifecycle events for status display
            tuple[str, list[ChatMessage]] — final sentinel with reply and history
        """
        t0 = time.monotonic()
        key = session_id or "_default"
        logger.debug("Message stream processing started (session=%s)", session_id)

        client = await self._get_or_create_client(
            session_id, history, model, permission_mode_override=permission_mode,
        )

        reply_parts: list[str] = []
        new_session_id: str | None = self._extract_sdk_session_id(history)

        # Pending tool events
        pending_tools: dict[str, ActivityEvent] = {}

        # Set up the output queue for this stream — the can_use_tool callback
        # pushes InteractionRequests here alongside SDK messages.
        output_queue: asyncio.Queue = asyncio.Queue()
        self._output_queues[key] = output_queue

        async def _run_sdk():
            """Background task: run query + receive_response, push to queue."""
            try:
                await client.query(user_text)
                async for message in client.receive_response():
                    await output_queue.put(("sdk", message))
                await output_queue.put(("done", None))
            except Exception as exc:
                await output_queue.put(("error", exc))

        task = asyncio.create_task(_run_sdk())

        try:
            while True:
                tag, payload = await output_queue.get()

                if tag == "done":
                    break

                if tag == "error":
                    raise payload

                if tag == "plan_action":
                    # PlanExecuteAction: yield to gateway (not to channel).
                    # The SDK turn was interrupted (PermissionResultDeny with
                    # interrupt=True), so break — remaining messages will be
                    # the interrupt error which we should ignore.
                    yield payload
                    break

                if tag == "interaction":
                    # Yield InteractionRequest to the channel for user interaction.
                    # The SDK is blocked (awaiting the Future) until the channel
                    # calls request.resolve(response).
                    yield payload
                    continue

                # tag == "sdk"
                message = payload

                # Check task subclasses BEFORE generic SystemMessage
                if isinstance(message, TaskStartedMessage):
                    logger.info("Subagent started: %s (task=%s)", message.task_type, message.task_id)
                    yield ActivityEvent(
                        kind=ActivityKind.AGENT,
                        status=ActivityStatus.START,
                        id=message.task_id,
                        name=message.task_type or "agent",
                        summary=message.description,
                    )

                elif isinstance(message, TaskProgressMessage):
                    logger.info("Subagent progress: %s (task=%s)", message.last_tool_name, message.task_id)
                    yield ActivityEvent(
                        kind=ActivityKind.AGENT,
                        status=ActivityStatus.PROGRESS,
                        id=message.task_id,
                        name=message.last_tool_name or "",
                        summary=message.description,
                    )

                elif isinstance(message, TaskNotificationMessage):
                    logger.info("Subagent finished: %s (task=%s)", message.status, message.task_id)
                    yield ActivityEvent(
                        kind=ActivityKind.AGENT,
                        status=ActivityStatus.FINISH if message.status == "completed" else ActivityStatus.FAILED,
                        id=message.task_id,
                        name="",
                        summary=message.summary,
                    )

                elif isinstance(message, SystemMessage):
                    logger.info("Received SystemMessage[%s]", message.subtype)
                    logger.debug("SystemMessage[%s]: %s", message.subtype, message.data)
                    if message.subtype == "init":
                        new_session_id = message.data.get("session_id")

                elif isinstance(message, AssistantMessage):
                    logger.info("Received AssistantMessage with %d blocks", len(message.content))
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text = block.text if block.text.endswith("\n") else block.text + "\n"
                            reply_parts.append(text)
                            yield text
                        elif isinstance(block, ToolUseBlock):
                            short_desc = [v for v in block.input.values() if isinstance(v, str)]
                            short_desc = short_desc[0] or ''
                            if len(short_desc) > 40:
                                short_desc = short_desc[:40] + "..."
                            logger.info("Tool call: %s(%s)", block.name, short_desc)
                            logger.debug("Tool args: %s[id=%s](%s)", block.name, block.id, block.input)
                            # Emit tool START event and track as pending
                            event = ActivityEvent(
                                kind=ActivityKind.TOOL,
                                status=ActivityStatus.START,
                                id=block.id,
                                name=block.name,
                                summary=short_desc,
                            )
                            pending_tools[block.id] = event
                            yield event
                        elif isinstance(block, ThinkingBlock):
                            logger.debug("Thinking: %s", block.thinking)

                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            logger.debug("Tool call: %s (id=%s)", "succeeded" if not block.is_error else "failed", block.tool_use_id)
                            pending = pending_tools.pop(block.tool_use_id, None)
                            if pending is None:
                                logger.warning("Tool result for unknown call: %s", block.tool_use_id)
                            else:
                                pending.status = ActivityStatus.FAILED if block.is_error else ActivityStatus.FINISH
                                logger.info("Tool call %s: %s", pending.name, pending.status.value)
                                yield pending

                elif isinstance(message, ResultMessage):
                    elapsed = time.monotonic() - t0
                    total_chars = sum(len(p) for p in reply_parts)
                    logger.info(
                        "Completed in %.1fs — %d chars (session=%s)",
                        elapsed, total_chars, session_id,
                    )

        except (CLINotFoundError, CLIConnectionError) as exc:
            error_msg = f"Claude Agent SDK error: {exc}"
            logger.error(error_msg)
            reply_parts = [error_msg]
            yield error_msg
            await self._close_client(key)
        except Exception as exc:
            error_msg = f"Unexpected error: {exc}"
            logger.error(error_msg, exc_info=True)
            reply_parts = [error_msg]
            yield error_msg
            await self._close_client(key)
        finally:
            self._output_queues.pop(key, None)
            # Ensure background task completes
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        reply = "".join(reply_parts) if reply_parts else "(no response)"

        # Build updated history
        visible_history = [m for m in history if not (m.role == "system" and m.content and m.content.startswith(_SESSION_MARKER))]
        updated_history = list(visible_history)
        updated_history.append(ChatMessage(role="user", content=user_text))
        updated_history.append(ChatMessage(role="assistant", content=reply))
        if new_session_id:
            updated_history = self._inject_session_marker(updated_history, new_session_id)

        self._last_history = updated_history
        # Final sentinel: tuple signals end of stream with updated state
        yield (reply, updated_history)

    def get_updated_history(self) -> list[ChatMessage] | None:
        """Return the last computed history (fallback for non-sentinel callers)."""
        return self._last_history
