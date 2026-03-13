"""Core agent loop — orchestrates provider, tools, memory, and channels."""

import asyncio
import json
import logging

from providers.base import Provider, ChatMessage, ChatResponse, ToolCall
from channels.base import Channel, ChannelMessage, SendMessage
from tools import ToolRegistry
from memory.base import Memory

logger = logging.getLogger(__name__)


class Agent:
    """The core agent that ties together provider, tools, memory, and channels."""

    def __init__(
        self,
        provider: Provider,
        tool_registry: ToolRegistry,
        memory: Memory,
        system_prompt: str = "",
        max_tool_iterations: int = 15,
        model: str | None = None,
        temperature: float = 0.7,
    ):
        self._provider = provider
        self._tools = tool_registry
        self._memory = memory
        self._system_prompt = system_prompt
        self._max_tool_iterations = max_tool_iterations
        self._model = model
        self._temperature = temperature
        self._conversations: dict[str, list[ChatMessage]] = {}

    async def _run_tool_call_loop(
        self,
        messages: list[ChatMessage],
        tool_specs: list[dict],
    ) -> str:
        """Run the tool call loop until the LLM returns a text-only response."""
        for iteration in range(self._max_tool_iterations):
            response = await self._provider.chat(
                messages=messages,
                tools=tool_specs if tool_specs else None,
                model=self._model,
                temperature=self._temperature,
            )

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
                tool = self._tools.get(tc.name)
                if tool is None:
                    result_text = f"Error: unknown tool '{tc.name}'"
                    logger.warning(result_text)
                else:
                    result = await tool.execute(tc.arguments)
                    result_text = result.output
                    if not result.success:
                        result_text = f"[FAILED] {result_text}"
                    logger.info(f"Tool result ({tc.name}): {result_text[:200]}")

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

    async def process_message(self, user_text: str, sender_id: str = "default") -> str:
        """Process a single user message and return the agent's response."""
        # Build messages
        messages = []

        # System prompt
        system_parts = [self._system_prompt] if self._system_prompt else []

        # Add available tool names to system prompt
        tool_names = self._tools.list_names()
        if tool_names:
            system_parts.append(f"Available tools: {', '.join(tool_names)}")

        # Add memory context
        context = await self._build_context(user_text)
        if context:
            system_parts.append(context)

        if system_parts:
            messages.append(ChatMessage(role="system", content="\n\n".join(system_parts)))

        # Add conversation history for this sender
        history = self._conversations.get(sender_id, [])
        messages.extend(history)

        # Add current user message
        user_msg = ChatMessage(role="user", content=user_text)
        messages.append(user_msg)

        # Get tool specs
        tool_specs = self._tools.all_specs()

        # Run the loop
        reply = await self._run_tool_call_loop(messages, tool_specs)

        # Update conversation history (keep last 20 messages to avoid unbounded growth)
        history.append(user_msg)
        history.append(ChatMessage(role="assistant", content=reply))
        if len(history) > 20:
            history = history[-20:]
        self._conversations[sender_id] = history

        return reply

    async def run_channel(self, channel: Channel):
        """Run the agent on a channel, processing messages as they arrive."""

        async def on_message(msg: ChannelMessage):
            logger.info(f"Received from {msg.sender_id}: {msg.text[:100]}")
            try:
                reply = await self.process_message(msg.text, sender_id=msg.sender_id)
                await channel.send(SendMessage(
                    text=reply,
                    channel_id=msg.channel_id,
                    reply_to=msg.message_id,
                ))
            except Exception as e:
                logger.error(f"Error processing message: {e}", exc_info=True)
                await channel.send(SendMessage(
                    text=f"Sorry, an error occurred: {e}",
                    channel_id=msg.channel_id,
                    reply_to=msg.message_id,
                ))

        logger.info("Agent starting on channel...")
        await channel.listen(on_message)
