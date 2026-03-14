"""CCAgent — wraps claude-agent-sdk as an alternative agent backend."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
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
        """Build ClaudeAgentOptions for a new client."""
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

    # --- Client pool management ---

    async def _get_or_create_client(
        self,
        session_id: str | None,
        history: list[ChatMessage],
        model: str | None,
    ) -> ClaudeSDKClient:
        """Return an existing client or create a new one for the session."""
        effective_model = model or self._default_model
        key = session_id or "_default"

        # Check for existing client
        entry = self._clients.get(key)
        if entry is not None:
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

        # Extract SDK session from history for cross-restart resume
        sdk_session_id = self._extract_sdk_session_id(history)
        logger.debug("SDK session extracted from history: %s", sdk_session_id)

        options = self._build_options(sdk_session_id, model)
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
        """
        t0 = time.monotonic()
        effective_model = model or self._default_model
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
            # Connection is broken, remove the client
            await self._close_client(session_id or "_default")
        except Exception as exc:
            error_msg = f"Unexpected error: {exc}"
            logger.error(error_msg, exc_info=True)
            reply_parts = [error_msg]
            await self._close_client(session_id or "_default")

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
        session_id: str | None = None,
    ) -> AsyncIterator[str | tuple[str, list[ChatMessage]]]:
        """Stream a response. Yields str chunks, then a final (reply, history) tuple."""
        t0 = time.monotonic()
        effective_model = model or self._default_model
        logger.debug("Message stream processing started (session=%s)", session_id)

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
                            yield text
                        elif isinstance(block, ToolUseBlock):
                            logger.info("Tool call: %s", block.name)
                            logger.debug("Tool args: %s(%s)", block.name, block.input)

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
            await self._close_client(session_id or "_default")
        except Exception as exc:
            error_msg = f"Unexpected error: {exc}"
            logger.error(error_msg, exc_info=True)
            reply_parts = [error_msg]
            yield error_msg
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
        # Final sentinel: tuple signals end of stream with updated state
        yield (reply, updated_history)

    def get_updated_history(self) -> list[ChatMessage] | None:
        """Return the last computed history (fallback for non-sentinel callers)."""
        return self._last_history
