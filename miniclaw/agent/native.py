"""Native Agent — custom tools, system prompt, provider-driven tool loop."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from miniclaw.activity import ActivityEvent, ActivityKind, ActivityStatus
from miniclaw.agent.config import AgentConfig
from miniclaw.cancellation import CancellationToken
from miniclaw.interactions import InteractionRequest, InteractionResponse, InteractionType
from miniclaw.log import truncate
from miniclaw.providers.base import ChatMessage, ChatResponse, Provider
from miniclaw.tools import ToolRegistry
from miniclaw.types import AgentEvent, HistoryUpdate, TextDelta, UsageEvent
from miniclaw.usage import UsageStats

# Tool spec injected into the LLM so it can ask the user questions mid-turn.
_ASK_USER_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "AskUserQuestion",
        "description": (
            "Ask the user a question with predefined options. Use this when you "
            "need clarification, a decision between approaches, or user preferences "
            "before proceeding."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "Questions to ask the user (1-4 questions).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The question text.",
                            },
                            "options": {
                                "type": "array",
                                "description": "Available choices (2-4 options).",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "Short display text for the option.",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "Explanation of the option.",
                                        },
                                    },
                                    "required": ["label", "description"],
                                },
                            },
                        },
                        "required": ["question", "options"],
                    },
                },
            },
            "required": ["questions"],
        },
    },
}

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
        system_prompt: str = "",
        default_model: str = "",
        temperature: float = 0.7,
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._system_prompt = system_prompt
        self._default_model = default_model
        self._temperature = temperature
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
        t0_total = time.monotonic()
        logger.info(
            "[NATIVE] process start: text_len=%d, history_len=%d, model=%s, "
            "max_iterations=%d",
            len(text), len(history), config.model or self._default_model,
            config.max_iterations,
        )

        # Build messages
        messages: list[ChatMessage] = []
        system_parts = [config.system_prompt or self._system_prompt]

        # Add available tool names
        tool_names = self._tools.list_names()
        if tool_names:
            system_parts.append(f"Available tools: {', '.join(tool_names)}")

        # Add plugctx content (loaded contexts)
        plugctx_content = config.extra.get("_plugctx_prompt", "")
        if plugctx_content:
            system_parts.append(plugctx_content)

        system_text = "\n\n".join(p for p in system_parts if p)
        if system_text:
            messages.append(ChatMessage(role="system", content=system_text))

        logger.debug(
            "[NATIVE] System prompt: %d parts, %d total chars",
            len([p for p in system_parts if p]), len(system_text),
        )

        messages.extend(history)
        user_msg = ChatMessage(role="user", content=text)
        messages.append(user_msg)

        # Tool specs (include built-in AskUserQuestion)
        tool_specs = list(self._tools.all_specs() or [])
        tool_specs.append(_ASK_USER_TOOL_SPEC)
        logger.debug(
            "[NATIVE] Tool specs: %d tools (%s)",
            len(tool_specs),
            ", ".join(t.get("function", {}).get("name", "") for t in tool_specs),
        )

        effective_model = config.model or self._default_model
        max_iterations = config.max_iterations
        pre_loop_len = len(messages)
        reply = ""
        turn_usage = UsageStats()  # per-message usage (yielded to channel)
        text_tail = ""       # last 2 chars of yielded text (for block-sep detection)
        had_nontext = False  # a non-text event was yielded since last TextDelta

        # Tool loop
        for iteration in range(max_iterations):
            token.check()  # checkpoint 1: before LLM call

            logger.debug(
                "[NATIVE iter=%d] Starting LLM call, messages=%d",
                iteration,
                len(messages),
            )

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
                    # Ensure markdown block separation after non-text events
                    if had_nontext and text_tail:
                        if not text_tail.endswith("\n\n"):
                            sep = "\n" if text_tail.endswith("\n") else "\n\n"
                            yield TextDelta(sep)
                        had_nontext = False
                    yield TextDelta(item)  # str chunk
                    text_tail = (text_tail + item)[-2:]

            if response is None:
                break

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            self._accumulate_usage(response, elapsed_ms)
            turn_usage.accumulate_token_usage(response.usage, elapsed_ms)

            logger.info(
                "[NATIVE iter=%d] LLM call done: duration_ms=%d, input_tokens=%d, "
                "output_tokens=%d",
                iteration, elapsed_ms,
                response.usage.input_tokens if response.usage else 0,
                response.usage.output_tokens if response.usage else 0,
            )

            if not response.tool_calls:
                reply = response.text or ""
                logger.debug(
                    "[NATIVE iter=%d] LLM returned text-only (no tool calls), ending turn",
                    iteration,
                )
                break

            logger.debug(
                "[NATIVE iter=%d] LLM returned %d tool call(s): %s",
                iteration,
                len(response.tool_calls),
                ", ".join(tc.name for tc in response.tool_calls),
            )

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

                # --- AskUserQuestion: yield InteractionRequest, await response ---
                if tc.name == "AskUserQuestion":
                    loop = asyncio.get_running_loop()
                    future: asyncio.Future[InteractionResponse] = loop.create_future()

                    request = InteractionRequest(
                        id=str(uuid4()),
                        type=InteractionType.ASK_USER,
                        tool_name="AskUserQuestion",
                        tool_input=tc.arguments,
                        _future=future,
                    )
                    yield request  # channel prompts user and calls resolve()

                    ir_response: InteractionResponse = await future
                    answers = (
                        ir_response.updated_input.get("answers", {})
                        if ir_response.updated_input
                        else {}
                    )
                    result_text = json.dumps(
                        {"answers": answers}, ensure_ascii=False,
                    )
                    logger.info(
                        "[NATIVE] AskUserQuestion resolved: %s",
                        truncate(result_text),
                    )
                    had_nontext = True

                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=result_text,
                            tool_call_id=tc.id,
                        )
                    )
                    continue

                # --- Normal tool execution ---
                yield ActivityEvent(
                    kind=ActivityKind.TOOL,
                    status=ActivityStatus.START,
                    id=tc_event_id,
                    name=tc.name,
                )
                had_nontext = True

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
                had_nontext = True

                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result_text,
                        tool_call_id=tc.id,
                    )
                )
        else:
            # Max iterations reached
            logger.warning(
                "[NATIVE] Max iterations reached (%d) — ending turn",
                max_iterations,
            )
            last_text = messages[-1].content if messages else ""
            reply = f"{last_text}\n\n(Warning: reached maximum tool iterations ({max_iterations}))"

        # Build updated history (strip system prompt)
        updated_history = list(history)
        updated_history.append(user_msg)
        updated_history.extend(messages[pre_loop_len:])
        updated_history.append(ChatMessage(role="assistant", content=reply))

        logger.info(
            "[NATIVE] process end: duration_ms=%d, reply_len=%d, history_len=%d",
            int((time.monotonic() - t0_total) * 1000),
            len(reply), len(updated_history),
        )

        yield UsageEvent(usage=turn_usage)
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

    async def _execute_tool(self, name: str, arguments: dict) -> str:
        """Execute a single tool call."""
        logger.debug(
            "[NATIVE] Tool execute: name=%s, args=%s",
            name, truncate(json.dumps(arguments, ensure_ascii=False)),
        )
        tool = self._tools.get(name)
        if tool is None:
            result_text = f"Error: unknown tool '{name}'"
            logger.warning(result_text)
        else:
            result = await tool.execute(arguments)
            result_text = result.output
            if not result.success:
                result_text = f"[FAILED] {result_text}"
            logger.debug(
                "[NATIVE] Tool result: name=%s, success=%s, len=%d, preview=%s",
                name, result.success, len(result_text),
                truncate(result_text),
            )
        logger.info("Tool result (%s): %s", name, truncate(result_text))
        return result_text

    def _accumulate_usage(self, response: ChatResponse, duration_ms: int = 0) -> None:
        stats = self._usage.setdefault("_default", UsageStats())
        stats.accumulate_token_usage(response.usage, duration_ms)

    def get_usage(self) -> UsageStats:
        return self._usage.get("_default", UsageStats())
