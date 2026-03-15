"""Core agent loop — pure LLM engine with provider, tools, and memory."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from uuid import uuid4

from miniclaw.activity import ActivityEvent, ActivityKind, ActivityStatus
from miniclaw.memory.base import Memory
from miniclaw.providers.base import ChatMessage, ChatResponse, Provider
from miniclaw.tools import ToolRegistry
from miniclaw.usage import UsageStats

if TYPE_CHECKING:
    from miniclaw.subagent.executor import SubagentExecutor
    from miniclaw.subagent.tracker import ExecutionTracker

logger = logging.getLogger(__name__)


class _Interrupted(Exception):
    """Raised by _interruptible() when the interrupt event fires."""


class Agent:
    """Pure LLM engine: takes history + config per call, returns results.

    Does NOT own conversations, sessions, or command handlers.
    """

    def __init__(
        self,
        provider: Provider,
        tool_registry: ToolRegistry,
        memory: Memory,
        system_prompt: str = "",
        max_tool_iterations: int = 15,
        default_model: str | None = None,
        temperature: float = 0.7,
        subagent_executor: SubagentExecutor | None = None,
        execution_tracker: ExecutionTracker | None = None,
    ):
        self._provider = provider
        self._tools = tool_registry
        self._memory = memory
        self._system_prompt = system_prompt
        self._max_tool_iterations = max_tool_iterations
        self._default_model = default_model
        self._temperature = temperature
        self._subagent_executor = subagent_executor
        self._execution_tracker = execution_tracker
        self._usage: dict[str, UsageStats] = {}
        self._interrupt_events: dict[str, asyncio.Event] = {}

    def _accumulate_usage(self, response: ChatResponse, session_id: str | None, duration_ms: int = 0) -> None:
        """Accumulate token usage from a ChatResponse."""
        key = session_id or "_default"
        stats = self._usage.setdefault(key, UsageStats())
        stats.accumulate_token_usage(response.usage, duration_ms)

    async def interrupt(self, session_id: str | None = None) -> None:
        """Signal the agent to stop processing the current turn."""
        key = session_id or "_default"
        event = self._interrupt_events.get(key)
        if event is not None:
            logger.info("Interrupt requested (session=%s)", key)
            event.set()
        else:
            logger.warning("No active turn to interrupt (session=%s)", key)

    def interrupt_sync(self, session_id: str | None = None) -> None:
        """Synchronous interrupt — safe to call from signal handlers."""
        key = session_id or "_default"
        event = self._interrupt_events.get(key)
        if event is not None:
            event.set()

    async def _interruptible(self, coro, session_id: str | None = None):
        """Run a coroutine, cancelling it if the interrupt event fires.

        Returns the coroutine result, or raises ``_Interrupted`` if interrupted.
        """
        key = session_id or "_default"
        event = self._interrupt_events.get(key)
        if event is None or event.is_set():
            if event and event.is_set():
                raise _Interrupted()
            return await coro

        task = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(event.wait())
        try:
            done, pending = await asyncio.wait(
                {task, waiter}, return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
                try:
                    await p
                except asyncio.CancelledError:
                    pass

            if task in done:
                return task.result()

            # Interrupted — cancel the task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise _Interrupted()
        except asyncio.CancelledError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise

    async def _run_tool_call_loop(
        self,
        messages: list[ChatMessage],
        tool_specs: list[dict],
        model: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Run the tool call loop until the LLM returns a text-only response."""
        effective_model = model or self._default_model
        for iteration in range(self._max_tool_iterations):
            try:
                t0 = time.monotonic()
                response = await self._interruptible(
                    self._provider.chat(
                        messages=messages,
                        tools=tool_specs if tool_specs else None,
                        model=effective_model,
                        temperature=self._temperature,
                    ),
                    session_id,
                )
            except _Interrupted:
                last_text = messages[-1].content if messages else ""
                return f"{last_text}\n\n(Interrupted by user)"

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            self._accumulate_usage(response, session_id, elapsed_ms)

            if not response.tool_calls:
                return response.text or ""

            # Append assistant message with tool calls
            messages.append(ChatMessage(
                role="assistant",
                content=response.text,
                tool_calls=response.tool_calls,
            ))

            # Execute each tool call
            for tc in response.tool_calls:
                logger.info(f"Tool call: {tc.name}({json.dumps(tc.arguments, ensure_ascii=False)[:200]})")

                # Built-in dispatch (subagent/threads) before ToolRegistry
                try:
                    if tc.name == "subagent" and self._subagent_executor is not None:
                        result_text = await self._interruptible(
                            self._subagent_executor.run(
                                tc.arguments, self._execution_tracker
                            ),
                            session_id,
                        )
                        logger.info(f"Tool result ({tc.name}): {result_text[:200]}")
                    elif tc.name == "threads" and self._execution_tracker is not None:
                        result_text = self._execution_tracker.summary()
                        logger.info(f"Tool result ({tc.name}): {result_text[:200]}")
                    else:
                        tool = self._tools.get(tc.name)
                        if tool is None:
                            result_text = f"Error: unknown tool '{tc.name}'"
                            logger.warning(result_text)
                        else:
                            result = await self._interruptible(
                                tool.execute(tc.arguments), session_id,
                            )
                            result_text = result.output
                            if not result.success:
                                result_text = f"[FAILED] {result_text}"
                            logger.info(f"Tool result ({tc.name}): {result_text[:200]}")
                except _Interrupted:
                    return response.text or "(Interrupted by user)"

                messages.append(ChatMessage(
                    role="tool",
                    content=result_text,
                    tool_call_id=tc.id,
                ))

        # Max iterations reached
        last_text = messages[-1].content if messages else ""
        return f"{last_text}\n\n(Warning: reached maximum tool iterations ({self._max_tool_iterations}))"

    async def _build_context(self, user_text: str) -> str:
        """Build context from memory relevant to the user's message."""
        try:
            memories = await self._memory.recall(user_text, limit=3)
            if memories:
                lines = ["Relevant memories:"]
                for m in memories:
                    lines.append(f"- [{m['key']}]: {m['content']}")
                return "\n".join(lines)
        except Exception as e:
            logger.debug(f"Memory recall failed: {e}")
        return ""

    async def process_message(
        self,
        user_text: str,
        history: list[ChatMessage],
        model: str | None = None,
        session_id: str | None = None,
    ) -> tuple[str, list[ChatMessage]]:
        """Process a message. Returns (reply, updated_history).

        Caller provides history; Agent returns updated history.
        ``model`` overrides ``_default_model`` if provided.
        """
        # Build messages
        messages: list[ChatMessage] = []

        # System prompt
        system_parts = [self._system_prompt] if self._system_prompt else []

        # Add available tool names to system prompt
        tool_names = self._tools.list_names()
        if self._subagent_executor is not None:
            tool_names = tool_names + ["subagent", "threads"]
        if tool_names:
            system_parts.append(f"Available tools: {', '.join(tool_names)}")

        # Add memory context
        context = await self._build_context(user_text)
        if context:
            system_parts.append(context)

        if system_parts:
            messages.append(ChatMessage(role="system", content="\n\n".join(system_parts)))

        # Add conversation history
        messages.extend(history)

        # Add current user message
        user_msg = ChatMessage(role="user", content=user_text)
        messages.append(user_msg)

        # Get tool specs
        tool_specs = self._tools.all_specs()
        if self._subagent_executor is not None:
            tool_specs = tool_specs + [
                self._subagent_executor.subagent_spec(),
                self._subagent_executor.threads_spec(),
            ]

        # Run the loop (mutates messages in place with tool-call intermediates)
        pre_loop_len = len(messages)
        # Track turn
        key = session_id or "_default"
        self._usage.setdefault(key, UsageStats()).num_turns += 1
        # Set up interrupt event for this turn
        self._interrupt_events[key] = asyncio.Event()
        try:
            reply = await self._run_tool_call_loop(messages, tool_specs, model=model, session_id=session_id)
        finally:
            self._interrupt_events.pop(key, None)

        # Build updated history: prior + user + tool-call intermediates + final reply
        updated_history = list(history)
        updated_history.append(user_msg)
        updated_history.extend(messages[pre_loop_len:])  # assistant+tool_calls, tool results
        updated_history.append(ChatMessage(role="assistant", content=reply))

        return reply, updated_history

    async def process_message_stream(
        self,
        user_text: str,
        history: list[ChatMessage],
        model: str | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[str | ActivityEvent | tuple[str, list[ChatMessage]]]:
        """Stream a response. Yields str chunks, ActivityEvents, and a final sentinel tuple."""
        # Build messages (same setup as process_message)
        messages: list[ChatMessage] = []

        system_parts = [self._system_prompt] if self._system_prompt else []
        tool_names = self._tools.list_names()
        if self._subagent_executor is not None:
            tool_names = tool_names + ["subagent", "threads"]
        if tool_names:
            system_parts.append(f"Available tools: {', '.join(tool_names)}")

        context = await self._build_context(user_text)
        if context:
            system_parts.append(context)

        if system_parts:
            messages.append(ChatMessage(role="system", content="\n\n".join(system_parts)))

        messages.extend(history)
        user_msg = ChatMessage(role="user", content=user_text)
        messages.append(user_msg)

        tool_specs = self._tools.all_specs()
        if self._subagent_executor is not None:
            tool_specs = tool_specs + [
                self._subagent_executor.subagent_spec(),
                self._subagent_executor.threads_spec(),
            ]

        effective_model = model or self._default_model
        pre_loop_len = len(messages)
        reply = ""

        # Track turn
        key = session_id or "_default"
        self._usage.setdefault(key, UsageStats()).num_turns += 1
        # Set up interrupt event for this turn
        interrupt_event = asyncio.Event()
        self._interrupt_events[key] = interrupt_event

        for iteration in range(self._max_tool_iterations):
            # Check for interrupt before each LLM call
            if interrupt_event.is_set():
                break
            # Stream the LLM response
            t0 = time.monotonic()
            response: ChatResponse | None = None
            async for item in self._provider.chat_stream(
                messages=messages,
                tools=tool_specs if tool_specs else None,
                model=effective_model,
                temperature=self._temperature,
            ):
                if isinstance(item, ChatResponse):
                    response = item
                else:
                    yield item  # str text delta

            if response is None:
                break

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            self._accumulate_usage(response, session_id, elapsed_ms)

            if not response.tool_calls:
                reply = response.text or ""
                break

            # Append assistant message with tool calls
            messages.append(ChatMessage(
                role="assistant",
                content=response.text,
                tool_calls=response.tool_calls,
            ))

            # Execute each tool call with activity events
            interrupted = False
            for tc in response.tool_calls:
                # Check for interrupt before each tool call
                if interrupt_event.is_set():
                    interrupted = True
                    break

                tc_event_id = tc.id or str(uuid4())
                logger.info(f"Tool call: {tc.name}({json.dumps(tc.arguments, ensure_ascii=False)[:200]})")

                yield ActivityEvent(
                    kind=ActivityKind.TOOL,
                    status=ActivityStatus.START,
                    id=tc_event_id,
                    name=tc.name,
                )

                try:
                    if tc.name == "subagent" and self._subagent_executor is not None:
                        agent_id = str(uuid4())
                        yield ActivityEvent(
                            kind=ActivityKind.AGENT,
                            status=ActivityStatus.START,
                            id=agent_id,
                            name="subagent",
                        )
                        try:
                            result_text = await self._interruptible(
                                self._subagent_executor.run(
                                    tc.arguments, self._execution_tracker
                                ),
                                session_id,
                            )
                            yield ActivityEvent(
                                kind=ActivityKind.AGENT,
                                status=ActivityStatus.FINISH,
                                id=agent_id,
                                name="subagent",
                            )
                        except Exception as e:
                            if isinstance(e, _Interrupted):
                                raise
                            result_text = f"Error: {e}"
                            yield ActivityEvent(
                                kind=ActivityKind.AGENT,
                                status=ActivityStatus.FAILED,
                                id=agent_id,
                                name="subagent",
                            )
                        logger.info(f"Tool result ({tc.name}): {result_text[:200]}")
                    elif tc.name == "threads" and self._execution_tracker is not None:
                        result_text = self._execution_tracker.summary()
                        logger.info(f"Tool result ({tc.name}): {result_text[:200]}")
                    else:
                        tool = self._tools.get(tc.name)
                        if tool is None:
                            result_text = f"Error: unknown tool '{tc.name}'"
                            logger.warning(result_text)
                        else:
                            result = await self._interruptible(
                                tool.execute(tc.arguments), session_id,
                            )
                            result_text = result.output
                            if not result.success:
                                result_text = f"[FAILED] {result_text}"
                            logger.info(f"Tool result ({tc.name}): {result_text[:200]}")
                except _Interrupted:
                    yield ActivityEvent(
                        kind=ActivityKind.TOOL,
                        status=ActivityStatus.FAILED,
                        id=tc_event_id,
                        name=tc.name,
                    )
                    interrupted = True
                    break

                tool_status = (
                    ActivityStatus.FAILED if result_text.startswith(("[FAILED]", "Error:"))
                    else ActivityStatus.FINISH
                )
                yield ActivityEvent(
                    kind=ActivityKind.TOOL,
                    status=tool_status,
                    id=tc_event_id,
                    name=tc.name,
                )

                messages.append(ChatMessage(
                    role="tool",
                    content=result_text,
                    tool_call_id=tc.id,
                ))

            if interrupted:
                break
        else:
            # Max iterations reached
            last_text = messages[-1].content if messages else ""
            reply = f"{last_text}\n\n(Warning: reached maximum tool iterations ({self._max_tool_iterations}))"

        # Build updated history and yield sentinel
        self._interrupt_events.pop(key, None)
        updated_history = list(history)
        updated_history.append(user_msg)
        updated_history.extend(messages[pre_loop_len:])
        updated_history.append(ChatMessage(role="assistant", content=reply))

        yield (reply, updated_history)

    def get_usage(self, session_id: str | None = None) -> UsageStats:
        """Return cumulative usage stats for a session."""
        key = session_id or "_default"
        return self._usage.setdefault(key, UsageStats())
