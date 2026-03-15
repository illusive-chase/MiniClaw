"""Native Agent — custom tools, system prompt, provider-driven tool loop."""

from __future__ import annotations

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

from miniclaw.agent.config import AgentConfig
from miniclaw.cancellation import CancellationToken
from miniclaw.types import AgentEvent, HistoryUpdate, TextDelta, UsageEvent

if TYPE_CHECKING:
    from miniclaw.subagent.executor import SubagentExecutor
    from miniclaw.subagent.tracker import ExecutionTracker

logger = logging.getLogger(__name__)


class NativeAgent:
    """Native agent: custom tools, system prompt, provider-driven tool loop.

    Stateless per call — all conversation state lives in history
    passed by Session.
    """

    def __init__(
        self,
        provider: Provider,
        tool_registry: ToolRegistry,
        memory: Memory,
        system_prompt: str = "",
        default_model: str = "",
        temperature: float = 0.7,
        subagent_executor: SubagentExecutor | None = None,
        execution_tracker: ExecutionTracker | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._memory = memory
        self._system_prompt = system_prompt
        self._default_model = default_model
        self._temperature = temperature
        self._subagent_executor = subagent_executor
        self._execution_tracker = execution_tracker
        self._usage: dict[str, UsageStats] = {}

    # --- AgentProtocol ---

    @property
    def agent_type(self) -> str:
        return "native"

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
        """Process a message with streaming. Yields AgentEvent items."""
        # Build messages
        messages: list[ChatMessage] = []
        system_parts = [config.system_prompt or self._system_prompt]

        # Add available tool names
        tool_names = self._tools.list_names()
        if self._subagent_executor is not None:
            tool_names = tool_names + ["subagent", "threads"]
        if tool_names:
            system_parts.append(f"Available tools: {', '.join(tool_names)}")

        # Add memory context
        if config.memory_enabled:
            context = await self._build_context(text)
            if context:
                system_parts.append(context)

        system_text = "\n\n".join(p for p in system_parts if p)
        if system_text:
            messages.append(ChatMessage(role="system", content=system_text))

        messages.extend(history)
        user_msg = ChatMessage(role="user", content=text)
        messages.append(user_msg)

        # Tool specs
        tool_specs = self._tools.all_specs()
        if self._subagent_executor is not None:
            tool_specs = tool_specs + [
                self._subagent_executor.subagent_spec(),
                self._subagent_executor.threads_spec(),
            ]

        effective_model = config.model or self._default_model
        max_iterations = config.max_iterations
        pre_loop_len = len(messages)
        reply = ""

        # Tool loop
        for iteration in range(max_iterations):
            token.check()  # checkpoint 1: before LLM call

            t0 = time.monotonic()
            response: ChatResponse | None = None

            async for item in self._provider.chat_stream(
                messages=messages,
                tools=tool_specs if tool_specs else None,
                model=effective_model or None,
                temperature=config.temperature or self._temperature,
            ):
                if isinstance(item, ChatResponse):
                    response = item
                else:
                    token.check()  # checkpoint 1b: between stream chunks
                    yield TextDelta(item)  # str chunk

            if response is None:
                break

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            self._accumulate_usage(response, elapsed_ms)

            if not response.tool_calls:
                reply = response.text or ""
                break

            # Append assistant message with tool calls
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.text,
                    tool_calls=response.tool_calls,
                )
            )

            # Execute each tool call
            for tc in response.tool_calls:
                token.check()  # checkpoint 2: before each tool

                tc_event_id = tc.id or str(uuid4())
                logger.info("Tool call: %s(%s)", tc.name, json.dumps(tc.arguments, ensure_ascii=False)[:200])

                yield ActivityEvent(
                    kind=ActivityKind.TOOL,
                    status=ActivityStatus.START,
                    id=tc_event_id,
                    name=tc.name,
                )

                result_text = await self._execute_tool(tc.name, tc.arguments)

                tool_status = (
                    ActivityStatus.FAILED
                    if result_text.startswith(("[FAILED]", "Error:"))
                    else ActivityStatus.FINISH
                )
                yield ActivityEvent(
                    kind=ActivityKind.TOOL,
                    status=tool_status,
                    id=tc_event_id,
                    name=tc.name,
                )

                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result_text,
                        tool_call_id=tc.id,
                    )
                )
        else:
            # Max iterations reached
            last_text = messages[-1].content if messages else ""
            reply = f"{last_text}\n\n(Warning: reached maximum tool iterations ({max_iterations}))"

        # Build updated history (strip system prompt)
        updated_history = list(history)
        updated_history.append(user_msg)
        updated_history.extend(messages[pre_loop_len:])
        updated_history.append(ChatMessage(role="assistant", content=reply))

        yield UsageEvent(usage=self.get_usage())
        yield HistoryUpdate(history=updated_history)

    async def reset(self) -> None:
        pass  # stateless

    async def shutdown(self) -> None:
        pass

    def serialize_state(self) -> dict:
        return {}

    async def restore_state(self, state: dict) -> None:
        pass

    async def on_fork(self, source_state: dict) -> dict:
        return {}

    # --- Internal ---

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
            logger.debug("Memory recall failed: %s", e)
        return ""

    async def _execute_tool(self, name: str, arguments: dict) -> str:
        """Execute a single tool call."""
        if name == "subagent" and self._subagent_executor is not None:
            result_text = await self._subagent_executor.run(
                arguments, self._execution_tracker
            )
        elif name == "threads" and self._execution_tracker is not None:
            result_text = self._execution_tracker.summary()
        else:
            tool = self._tools.get(name)
            if tool is None:
                result_text = f"Error: unknown tool '{name}'"
                logger.warning(result_text)
            else:
                result = await tool.execute(arguments)
                result_text = result.output
                if not result.success:
                    result_text = f"[FAILED] {result_text}"
        logger.info("Tool result (%s): %s", name, result_text[:200])
        return result_text

    def _accumulate_usage(self, response: ChatResponse, duration_ms: int = 0) -> None:
        stats = self._usage.setdefault("_default", UsageStats())
        stats.accumulate_token_usage(response.usage, duration_ms)

    def get_usage(self) -> UsageStats:
        return self._usage.get("_default", UsageStats())
