"""Core agent loop — orchestrates provider, tools, memory, and channels."""

import json
import logging

from miniclaw.channels.base import Channel, ChannelMessage, SendMessage
from miniclaw.memory.base import Memory
from miniclaw.providers.base import ChatMessage, Provider
from miniclaw.session import SessionManager
from miniclaw.tools import ToolRegistry

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
        session_manager: SessionManager | None = None,
    ):
        self._provider = provider
        self._tools = tool_registry
        self._memory = memory
        self._system_prompt = system_prompt
        self._max_tool_iterations = max_tool_iterations
        self._model = model
        self._temperature = temperature
        self._conversations: dict[str, list[ChatMessage]] = {}
        self._session_manager = session_manager
        self._command_handlers: dict[str, dict] = {
            "model": {
                "handler": self._cmd_model,
                "description": "Show or change the current model",
                "usage": "/model [model_name]",
            },
            "reset": {
                "handler": self._cmd_reset,
                "description": "Clear conversation history",
                "usage": "/reset",
            },
        }

    async def _cmd_model(self, args: str, sender_id: str) -> str:
        """Show or set the active model."""
        if not args:
            return f"Current model: {self._model or '(default)'}"
        old = self._model
        self._model = args
        return f"Model changed: {old or '(default)'} → {self._model}"

    async def _cmd_reset(self, args: str, sender_id: str) -> str:
        """Clear conversation history for the sender."""
        removed = len(self._conversations.pop(sender_id, []))
        return f"Conversation reset ({removed} messages cleared)."

    async def handle_command(self, command: str, args: str, sender_id: str) -> str | None:
        """Dispatch an agent-level slash command. Returns a response string."""
        entry = self._command_handlers.get(command)
        if entry is None:
            return f"Unknown command: /{command}. Type /help for available commands."
        return await entry["handler"](args, sender_id)

    def command_help(self) -> list[dict]:
        """Return descriptions of agent-level commands for /help display."""
        return [
            {"name": name, "description": e["description"], "usage": e["usage"]}
            for name, e in self._command_handlers.items()
        ]

    def get_conversation(self, sender_id: str) -> list[ChatMessage]:
        """Return the conversation history for a sender."""
        return self._conversations.get(sender_id, [])

    def set_conversation(self, sender_id: str, messages: list[ChatMessage]) -> None:
        """Replace the conversation history for a sender."""
        self._conversations[sender_id] = messages

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
        # Lazily create a session on first message
        if self._session_manager and self._session_manager.current is None:
            self._session_manager.new_session(sender_id)

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
            if msg.command:
                logger.info(f"Command from {msg.sender_id}: /{msg.command} {msg.command_args or ''}")
                result = await self.handle_command(msg.command, msg.command_args or "", msg.sender_id)
                if result:
                    await channel.send(SendMessage(
                        text=result,
                        channel_id=msg.channel_id,
                        reply_to=msg.message_id,
                    ))
            else:
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
        try:
            await channel.listen(on_message)
        finally:
            self._dump_session_on_exit()

    def _dump_session_on_exit(self) -> None:
        """Persist the current session's conversation to disk."""
        if self._session_manager is None or self._session_manager.current is None:
            return
        sender = self._session_manager.current.sender_id
        messages = self._conversations.get(sender, [])
        self._session_manager.dump_current(messages)
