"""Persistent session management — dump/resume conversation history as JSON files."""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from miniclaw.providers.base import ChatMessage, ToolCall


@dataclass
class PersistedSession:
    """A single saved conversation session (persistence format).

    Top-level fields (sender_id, created_at, etc.) are kept for backward
    compatibility with JSON files written before the extended format.
    """

    id: str
    sender_id: str
    created_at: str
    updated_at: str
    name: str | None = None
    messages: list[dict] = field(default_factory=list)
    agent_type: str = "native"
    agent_config: dict = field(default_factory=dict)
    agent_state: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


class SessionManager:
    """Manages session persistence under ``$workspace/.sessions/``.

    Pure persistence layer — no concept of "current" session.
    """

    def __init__(self, workspace_dir: str):
        self._sessions_dir = Path(workspace_dir) / ".sessions"
        self._cache_dir = Path(workspace_dir) / ".cache"
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ---- lifecycle ----------------------------------------------------------

    def create_session(self, sender_id: str) -> PersistedSession:
        """Create a fresh session and return it."""
        ts = datetime.now(timezone.utc)
        token = sha256(os.urandom(16)).hexdigest()[:6]
        session_id = ts.strftime("%Y%m%d_%H%M%S") + "_" + token
        return PersistedSession(
            id=session_id,
            sender_id=sender_id,
            created_at=ts.isoformat(),
            updated_at=ts.isoformat(),
        )

    def save(self, session: PersistedSession, messages: list[ChatMessage]) -> None:
        """Persist a session and its messages to disk (no-op if empty)."""
        if not messages:
            return
        session.messages = self.serialize_messages(messages)
        session.updated_at = datetime.now(timezone.utc).isoformat()
        path = self._sessions_dir / f"{session.id}.json"
        with open(path, "w") as f:
            json.dump(asdict(session), f, indent=2, ensure_ascii=False)

    def load_session(self, session_id: str) -> PersistedSession:
        """Load a session from disk by exact ID."""
        path = self._sessions_dir / f"{session_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"No session file: {session_id}")
        try:
            with open(path) as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Corrupt session file {session_id}: {exc}") from exc
        return PersistedSession(**data)

    def list_sessions(self) -> list[PersistedSession]:
        """Return all saved sessions, newest first (skips corrupt files)."""
        sessions: list[PersistedSession] = []
        for p in self._sessions_dir.glob("*.json"):
            try:
                with open(p) as f:
                    data = json.load(f)
                sessions.append(PersistedSession(**data))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def resolve_prefix(self, prefix: str) -> PersistedSession:
        """Find a unique session by ID prefix or name prefix.

        Raises ``ValueError`` on zero or ambiguous matches.
        """
        prefix_lower = prefix.lower()
        matches: list[PersistedSession] = []
        for session in self.list_sessions():
            if session.id.startswith(prefix):
                matches.append(session)
            elif session.name and session.name.lower().startswith(prefix_lower):
                matches.append(session)
        if not matches:
            raise ValueError(f"No session matching '{prefix}'")
        if len(matches) > 1:
            labels = [f"  {s.id} ({s.name or 'unnamed'})" for s in matches]
            raise ValueError(
                f"Ambiguous prefix '{prefix}', matches:\n" + "\n".join(labels)
            )
        return matches[0]

    # ---- serialization helpers ---------------------------------------------

    @staticmethod
    def serialize_messages(messages: list[ChatMessage]) -> list[dict]:
        result: list[dict] = []
        for m in messages:
            d: dict = {"role": m.role, "content": m.content}
            if m.tool_calls:
                d["tool_calls"] = [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in m.tool_calls
                ]
            if m.tool_call_id:
                d["tool_call_id"] = m.tool_call_id
            result.append(d)
        return result

    @staticmethod
    def deserialize_messages(data: list[dict]) -> list[ChatMessage]:
        result: list[ChatMessage] = []
        for d in data:
            tool_calls = None
            if "tool_calls" in d:
                tool_calls = [
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments", {}))
                    for tc in d["tool_calls"]
                ]
            result.append(
                ChatMessage(
                    role=d["role"],
                    content=d.get("content"),
                    tool_calls=tool_calls,
                    tool_call_id=d.get("tool_call_id"),
                )
            )
        return result
