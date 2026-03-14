"""CCAgent — wraps claude-agent-sdk as an alternative agent backend."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from miniclaw.providers.base import ChatMessage

logger = logging.getLogger(__name__)

_SESSION_MARKER = "__cc_session__:"


class CCAgent:
    """Agent backend that delegates the agentic loop to Claude Agent SDK.

    Implements the same ``process_message()`` interface as ``Agent`` so Gateway
    can work with both interchangeably.
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

    def _build_options(
        self, sdk_session_id: str | None, model: str | None
    ) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions for a query call."""
        effective_model = model or self._default_model
        opts: dict = {}
        if sdk_session_id:
            opts["resume"] = sdk_session_id
        if self._system_prompt:
            opts["system_prompt"] = self._system_prompt
        if effective_model:
            opts["model"] = effective_model
        if self._permission_mode:
            opts["permission_mode"] = self._permission_mode
        if self._allowed_tools:
            opts["allowed_tools"] = self._allowed_tools
        if self._cwd:
            opts["cwd"] = self._cwd
        if self._max_turns is not None:
            opts["max_turns"] = self._max_turns
        return ClaudeAgentOptions(**opts)

    # --- Core interface ---

    async def process_message(
        self,
        user_text: str,
        history: list[ChatMessage],
        model: str | None = None,
    ) -> tuple[str, list[ChatMessage]]:
        """Process a message. Returns (reply, updated_history).

        Same interface as Agent.process_message().
        """
        sdk_session_id = self._extract_sdk_session_id(history)
        options = self._build_options(sdk_session_id, model)
        effective_model = model or self._default_model
        logger.info("Processing message (sdk_session=%s, model=%s)", sdk_session_id, effective_model)

        reply_parts: list[str] = []
        new_session_id: str | None = sdk_session_id

        try:
            async for message in query(prompt=user_text, options=options):
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    new_session_id = message.data.get("session_id")

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            reply_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            logger.info("Tool use: %s", block.name)

                elif isinstance(message, ResultMessage):
                    if message.result:
                        reply_parts.append(message.result)
                    logger.info(
                        "Reply (%d chars, session=%s)",
                        sum(len(p) for p in reply_parts),
                        new_session_id,
                    )
        except (CLINotFoundError, CLIConnectionError) as exc:
            error_msg = f"Claude Agent SDK error: {exc}"
            logger.error(error_msg)
            reply_parts = [error_msg]
        except Exception as exc:
            error_msg = f"Unexpected error: {exc}"
            logger.error(error_msg, exc_info=True)
            reply_parts = [error_msg]

        reply = "".join(reply_parts) if reply_parts else "(no response)"

        # Build updated history: session marker + prior user/assistant msgs + new exchange
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
    ) -> AsyncIterator[str | tuple[str, list[ChatMessage]]]:
        """Stream a response. Yields str chunks, then a final (reply, history) tuple."""
        sdk_session_id = self._extract_sdk_session_id(history)
        options = self._build_options(sdk_session_id, model)
        effective_model = model or self._default_model
        logger.info("Processing message stream (sdk_session=%s, model=%s)", sdk_session_id, effective_model)

        reply_parts: list[str] = []
        new_session_id: str | None = sdk_session_id

        try:
            async for message in query(prompt=user_text, options=options):
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    new_session_id = message.data.get("session_id")

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            reply_parts.append(block.text)
                            yield block.text
                        elif isinstance(block, ToolUseBlock):
                            logger.info("Tool use: %s", block.name)

                elif isinstance(message, ResultMessage):
                    # ResultMessage.result may contain the final assembled text.
                    # We already yielded from AssistantMessage blocks, so just log.
                    logger.info(
                        "Reply (%d chars, session=%s)",
                        sum(len(p) for p in reply_parts),
                        new_session_id,
                    )

        except (CLINotFoundError, CLIConnectionError) as exc:
            error_msg = f"Claude Agent SDK error: {exc}"
            logger.error(error_msg)
            reply_parts = [error_msg]
            yield error_msg
        except Exception as exc:
            error_msg = f"Unexpected error: {exc}"
            logger.error(error_msg, exc_info=True)
            reply_parts = [error_msg]
            yield error_msg

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
