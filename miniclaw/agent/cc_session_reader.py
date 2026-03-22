"""Session file reader for CC CLI JSONL session files.

Reads ~/.claude/projects/<project-hash>/<session-id>.jsonl and extracts
assistant text, tool calls, usage stats, and raw messages for history
reconstruction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from miniclaw.usage import TokenUsage, UsageStats

logger = logging.getLogger(__name__)


@dataclass
class ToolCallInfo:
    """A single tool call extracted from a session file."""

    name: str
    input: dict
    result: str | None = None
    tool_use_id: str = ""
    is_error: bool = False


@dataclass
class SessionTurnResult:
    """Result of reading new messages from a session file."""

    assistant_text: str = ""
    tool_calls: list[ToolCallInfo] = field(default_factory=list)
    usage: UsageStats = field(default_factory=UsageStats)
    raw_messages: list[dict] = field(default_factory=list)
    watermark: int = 0


def _project_hash(cwd: str) -> str:
    """Convert absolute path to CC CLI project hash.

    CC CLI uses the absolute path with '/' replaced by '-'.
    e.g. /root/foo -> -root-foo
    """
    return cwd.replace("/", "-")


def find_session_file(cwd: str, session_id: str) -> Path | None:
    """Locate the session JSONL file for a given project and session ID."""
    project_dir = Path.home() / ".claude" / "projects" / _project_hash(cwd)
    candidate = project_dir / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
    # Fallback: search for session ID in all project dirs
    claude_projects = Path.home() / ".claude" / "projects"
    if claude_projects.exists():
        for pdir in claude_projects.iterdir():
            c = pdir / f"{session_id}.jsonl"
            if c.exists():
                return c
    return None


class SessionReader:
    """Reads CC CLI session JSONL files incrementally."""

    def __init__(self, cwd: str, session_id: str) -> None:
        self._cwd = cwd
        self._session_id = session_id
        self._file_path: Path | None = None

    @property
    def file_path(self) -> Path | None:
        if self._file_path is None:
            self._file_path = find_session_file(self._cwd, self._session_id)
        return self._file_path

    def read_new_messages(self, after_line: int = 0) -> SessionTurnResult:
        """Read JSONL file and return messages after the watermark line.

        Args:
            after_line: Line number to start reading after (0-based count).

        Returns:
            SessionTurnResult with extracted data and new watermark.
        """
        path = self.file_path
        if path is None:
            logger.warning(
                "[SessionReader] Session file not found: cwd=%s, id=%s",
                self._cwd, self._session_id,
            )
            return SessionTurnResult(watermark=after_line)

        try:
            lines = path.read_text().splitlines()
        except OSError as exc:
            logger.error("[SessionReader] Failed to read %s: %s", path, exc)
            return SessionTurnResult(watermark=after_line)

        new_lines = lines[after_line:]
        if not new_lines:
            return SessionTurnResult(watermark=len(lines))

        result = SessionTurnResult(watermark=len(lines))
        text_parts: list[str] = []
        # Map tool_use_id -> ToolCallInfo for matching results
        tool_map: dict[str, ToolCallInfo] = {}

        for raw_line in new_lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            result.raw_messages.append(record)
            msg_type = record.get("type", "")

            if msg_type == "assistant":
                self._process_assistant(record, text_parts, tool_map, result)
            elif msg_type == "user":
                self._process_user(record, tool_map)

        result.assistant_text = "".join(text_parts)
        result.tool_calls = list(tool_map.values())
        return result

    def _process_assistant(
        self,
        record: dict,
        text_parts: list[str],
        tool_map: dict[str, ToolCallInfo],
        result: SessionTurnResult,
    ) -> None:
        """Process an assistant-type JSONL record."""
        message = record.get("message", {})
        stop_reason = message.get("stop_reason")

        # Only process complete messages (non-null stop_reason)
        if stop_reason is None:
            return

        # Skip synthetic messages (e.g. "No response requested." on resume)
        if message.get("model") == "<synthetic>":
            return

        # Extract usage
        usage = message.get("usage")
        if usage:
            tu = TokenUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            )
            result.usage.accumulate_token_usage(tu)

        # Extract content blocks
        content = message.get("content", [])
        for block in content:
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_id = block.get("id", "")
                tc = ToolCallInfo(
                    name=block.get("name", ""),
                    input=block.get("input", {}),
                    tool_use_id=tool_id,
                )
                tool_map[tool_id] = tc

    def _process_user(
        self,
        record: dict,
        tool_map: dict[str, ToolCallInfo],
    ) -> None:
        """Process a user-type JSONL record to match tool results."""
        message = record.get("message", {})
        content = message.get("content")
        if not isinstance(content, list):
            return

        for block in content:
            if block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id", "")
                tc = tool_map.get(tool_id)
                if tc is not None:
                    # Extract result text
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        parts = [
                            b.get("text", "")
                            for b in result_content
                            if b.get("type") == "text"
                        ]
                        result_content = "\n".join(parts)
                    tc.result = str(result_content)
                    tc.is_error = block.get("is_error", False)
