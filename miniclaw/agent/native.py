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
from miniclaw.cancellation import CancellationToken, CancelledError, SignalType
from miniclaw.interactions import (
    InteractionRequest,
    InteractionResponse,
    InteractionType,
)
from miniclaw.log import truncate
from miniclaw.providers.base import ChatMessage, ChatResponse, Provider
from miniclaw.tools import ToolRegistry
from miniclaw.types import AgentEvent, HistoryUpdate, TextDelta, UsageEvent
from miniclaw.usage import TokenUsage, UsageStats, compute_token_cost

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
        context_window: int = 0,
        pricing: dict | None = None,
        quota_factor: float = 1.0,
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._system_prompt = system_prompt
        self._default_model = default_model
        self._temperature = temperature
        self._context_window = context_window
        self._pricing = pricing
        self._quota_factor = quota_factor
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

        # Apply path context (includes cwd) or fall back to effective_cwd
        path_ctx = config.extra.get("_path_ctx")
        if path_ctx:
            self._tools.set_path_context(path_ctx)
        else:
            effective_cwd = config.extra.get("_effective_cwd")
            if effective_cwd:
                self._tools.set_cwd(effective_cwd)

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
        last_input_tokens = 0  # input_tokens from most recent LLM call (context size)
        last_cache_read_tokens = 0  # cache_read_tokens from most recent LLM call
        last_token_usage: TokenUsage | None = None  # raw TokenUsage from last LLM call

        # Tool loop
        for iteration in range(max_iterations):
            token.check()  # checkpoint 1: before LLM call
            self._drain_and_inject_signals(token, messages)  # inject sub-agent signals

            logger.debug(
                "[NATIVE iter=%d] Starting LLM call, messages=%d",
                iteration,
                len(messages),
            )

            t0 = time.monotonic()
            response: ChatResponse | None = None

            async for item in self._cancellable_aiter(
                self._provider.chat_stream(
                    messages=messages,
                    tools=tool_specs if tool_specs else None,
                    model=effective_model or None,
                    temperature=config.temperature or self._temperature,
                ),
                token,
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
            last_input_tokens = response.usage.input_tokens if response.usage else 0
            last_cache_read_tokens = response.usage.cache_read_tokens if response.usage else 0
            last_token_usage = response.usage

            # Accumulate cost into turn_usage
            if response.usage and self._pricing:
                cost = compute_token_cost(response.usage, self._pricing) * self._quota_factor
                turn_usage.total_cost_usd += cost

            # Intermediate usage update — lets the channel show running token count
            yield UsageEvent(
                usage=turn_usage.copy(), final=False,
                context_tokens=last_input_tokens + last_cache_read_tokens,
                context_window=self._context_window or None,
                last_usage=last_token_usage,
            )

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

        yield UsageEvent(
            usage=turn_usage,
            context_tokens=last_input_tokens + last_cache_read_tokens,
            context_window=self._context_window or None,
            last_usage=last_token_usage,
        )
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

    @staticmethod
    async def _cancellable_aiter(aiter, token: CancellationToken):
        """Yield items from *aiter*, raising CancelledError when *token* fires."""
        it = aiter.__aiter__()
        cancel_fut = asyncio.ensure_future(token.wait_cancelled())
        try:
            while True:
                next_fut = asyncio.ensure_future(it.__anext__())
                done, _ = await asyncio.wait(
                    {next_fut, cancel_fut},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_fut in done:
                    next_fut.cancel()
                    try:
                        await next_fut
                    except BaseException:
                        pass
                    raise CancelledError("Processing interrupted by user")
                try:
                    yield next_fut.result()
                except StopAsyncIteration:
                    return
        finally:
            cancel_fut.cancel()

    @staticmethod
    def _drain_and_inject_signals(
        token: CancellationToken,
        messages: list[ChatMessage],
    ) -> bool:
        """Drain pending notification/inject signals and append as user messages."""
        signals = token.drain({SignalType.NOTIFICATION, SignalType.INJECT})
        if not signals:
            return False
        from miniclaw.session import Session  # local import avoids circular

        for sig in signals:
            if sig.source == "sub_agent" and sig.metadata:
                text = Session._format_sub_agent_message(sig.metadata)
            else:
                text = sig.payload
            messages.append(ChatMessage(role="user", content=text))
        return True

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
