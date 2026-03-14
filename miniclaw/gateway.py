"""Gateway — session service that owns Agent, SessionManager, and Channels."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from miniclaw.interactions import InteractionRequest
from miniclaw.providers.base import ChatMessage
from miniclaw.session import Session, SessionManager

if TYPE_CHECKING:
    from miniclaw.agent import Agent
    from miniclaw.channels.base import Channel

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """Per-session state held by Gateway."""

    session: Session
    history: list[ChatMessage] = field(default_factory=list)
    model: str | None = None  # per-session model override


class Gateway:
    """Session service: owns sessions, routes messages through Agent."""

    def __init__(self, agent: Agent, session_manager: SessionManager):
        self._agent = agent
        self._sm = session_manager
        self._states: dict[str, SessionState] = {}  # session_id -> state
        self._locks: dict[str, asyncio.Lock] = {}  # per-session locks
        self._channels: list[Channel] = []

    # --- helpers ---

    async def _with_session_lock(self, session_id: str, fn):
        """Acquire per-session lock and run fn."""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await fn()

    # --- Channel registration & startup ---

    def register_channel(self, channel: Channel) -> None:
        self._channels.append(channel)

    async def run(self) -> None:
        """Start all channels as concurrent async tasks."""
        tasks = [asyncio.create_task(ch.start(self)) for ch in self._channels]
        try:
            await asyncio.gather(*tasks)
        finally:
            self._dump_all()
            if hasattr(self._agent, "aclose"):
                await self._agent.aclose()

    # --- Session lifecycle ---

    def allocate_session(self, sender_id: str) -> str:
        """Create a new session. Returns session_id."""
        session = self._sm.create_session(sender_id)
        self._states[session.id] = SessionState(session=session)
        return session.id

    # --- Message processing (the only path to Agent) ---

    async def process_message(self, session_id: str, text: str) -> str:
        """Process a user message within a session. Concurrency-safe per session."""

        async def _do():
            state = self._states[session_id]
            reply, updated = await self._agent.process_message(
                text, list(state.history), model=state.model,
                session_id=session_id,
            )
            state.history = updated
            return reply

        return await self._with_session_lock(session_id, _do)

    async def process_message_stream(
        self, session_id: str, text: str
    ) -> AsyncIterator[str | InteractionRequest]:
        """Stream a response. Yields str chunks and InteractionRequests.

        Updates session state on completion.
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            state = self._states[session_id]
            if hasattr(self._agent, "process_message_stream"):
                async for item in self._agent.process_message_stream(
                    text, list(state.history), model=state.model,
                    session_id=session_id,
                ):
                    if isinstance(item, tuple):  # sentinel: (reply, history)
                        state.history = item[1]
                    elif isinstance(item, InteractionRequest):
                        yield item  # channel handles interaction
                    else:
                        yield item  # str chunk
            else:
                # Fallback for regular Agent (non-streaming)
                reply, updated = await self._agent.process_message(
                    text, list(state.history), model=state.model,
                    session_id=session_id,
                )
                state.history = updated
                yield reply

    # --- Session state queries (called by channel commands) ---

    def list_sessions(self) -> list[Session]:
        """List all persisted sessions (read-only, no lock needed)."""
        return self._sm.list_sessions()

    def get_default_model(self) -> str | None:
        """Return the agent's default model (read-only)."""
        return self._agent._default_model

    def get_active_session_id(self) -> str | None:
        """Return the first active session id, if any."""
        if self._states:
            return next(iter(self._states))
        return None

    async def get_session(self, session_id: str) -> Session:
        async def _do():
            return self._states[session_id].session
        return await self._with_session_lock(session_id, _do)

    async def get_conversation(self, session_id: str) -> list[ChatMessage]:
        async def _do():
            return list(self._states[session_id].history)
        return await self._with_session_lock(session_id, _do)

    async def clear_conversation(self, session_id: str) -> int:
        """Clear conversation history. Returns number of messages removed."""

        async def _do():
            state = self._states[session_id]
            count = len(state.history)
            state.history = []
            return count

        return await self._with_session_lock(session_id, _do)

    async def rename_session(self, session_id: str, name: str) -> None:
        async def _do():
            self._states[session_id].session.name = name
        return await self._with_session_lock(session_id, _do)

    async def dump_session(self, session_id: str) -> None:
        """Persist session to disk."""

        async def _do():
            state = self._states[session_id]
            self._sm.save(state.session, state.history)

        return await self._with_session_lock(session_id, _do)

    async def get_session_model(self, session_id: str) -> str | None:
        async def _do():
            return self._states[session_id].model
        return await self._with_session_lock(session_id, _do)

    async def set_session_model(self, session_id: str, model: str | None) -> None:
        async def _do():
            self._states[session_id].model = model
        return await self._with_session_lock(session_id, _do)

    async def switch_session(
        self, current_id: str, target_prefix: str
    ) -> tuple[str, Session, list[ChatMessage]]:
        """Dump current session, resolve & load target.

        Returns (new_session_id, session, history).
        """

        async def _do():
            # Dump current session
            state = self._states[current_id]
            self._sm.save(state.session, state.history)

            # Resolve and load target
            target = self._sm.resolve_prefix(target_prefix)
            loaded = self._sm.load_session(target.id)
            restored = SessionManager.deserialize_messages(loaded.messages)

            # Register the loaded session in gateway state
            self._states[loaded.id] = SessionState(
                session=loaded,
                history=restored,
            )

            return loaded.id, loaded, restored

        return await self._with_session_lock(current_id, _do)

    # --- internal ---

    def _dump_all(self) -> None:
        """Persist all active sessions on shutdown."""
        for session_id, state in self._states.items():
            try:
                self._sm.save(state.session, state.history)
            except Exception as e:
                logger.error(f"Failed to dump session {session_id}: {e}")
