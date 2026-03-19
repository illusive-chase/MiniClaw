"""TUI output parser for CC CLI interactive mode.

Consumes raw tmux capture-pane output, diffs against previous snapshot,
and produces structured ParseEvent objects via a state machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class TuiState(Enum):
    STARTUP = "startup"
    IDLE = "idle"
    RESPONDING = "responding"
    TOOL_RUNNING = "tool"
    PERMISSION = "permission"
    INTERACTION = "interaction"


@dataclass
class ParseEvent:
    """Structured event extracted from TUI output diff."""

    kind: str  # text, tool_start, tool_end, permission, idle, error, cost, ask_user, plan_approval
    data: dict = field(default_factory=dict)


# --- Regex patterns for TUI elements ---

_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_PROMPT = re.compile(r"^\s*❯\s*$")
_RE_TOOL_START = re.compile(r"^\s*⏺\s+(\w+)\((.*)$")
_RE_RESPONSE_MARKER = re.compile(r"^\s*⏺\s*(.*)")
_RE_TOOL_RESULT = re.compile(r"^\s*⎿\s?(.*)")
_RE_PERMISSION = re.compile(
    r"(?:Allow|allow)\s+(.+?)\s*\?|Do you want to proceed|Yes\s*/\s*No"
)
_RE_COST = re.compile(r"\$(\d+\.\d+)")
_RE_SEPARATOR = re.compile(r"^─{5,}")
_RE_ERROR = re.compile(r"(?:^|\s)(?:Error|✗|Failed)(?:\s|:|$)", re.IGNORECASE)
_RE_OPTION_LINE = re.compile(r"^\s*(\d+)\.\s+(.+)")  # "1. Option text"
_RE_ASK_PROMPT = re.compile(r"^\s*❯\s+(.+)")  # Selection prompt indicator

# Tool names that trigger interaction handling
_INTERACTION_TOOLS = {"AskUserQuestion", "ExitPlanMode"}


class TuiParser:
    """State machine that parses CC CLI TUI output diffs into ParseEvents."""

    def __init__(self) -> None:
        self._state = TuiState.STARTUP
        self._prev_lines: list[str] = []
        self._current_tool: str | None = None
        self._tool_result_lines: list[str] = []
        self._last_content_hash: int = 0
        # Interaction tracking (AskUserQuestion / ExitPlanMode)
        self._interaction_tool: str | None = None
        self._interaction_lines: list[str] = []

    def reset(self, state: TuiState = TuiState.STARTUP) -> None:
        self._state = state
        self._prev_lines = []
        self._current_tool = None
        self._tool_result_lines = []
        self._last_content_hash = 0
        self._interaction_tool = None
        self._interaction_lines = []

    def feed(self, raw: str) -> list[ParseEvent]:
        """Feed raw capture-pane output. Returns new ParseEvents since last call."""
        cleaned = _RE_ANSI.sub("", raw)
        lines = cleaned.splitlines()

        # Fast path: no change
        content_hash = hash(cleaned)
        if content_hash == self._last_content_hash:
            return []
        self._last_content_hash = content_hash

        new_lines = self._diff_lines(lines)
        self._prev_lines = lines

        if not new_lines:
            return []

        events: list[ParseEvent] = []
        for line in new_lines:
            events.extend(self._process_line(line))
        return events

    def _diff_lines(self, new_lines: list[str]) -> list[str]:
        """Find new lines by suffix-matching against previous snapshot."""
        if not self._prev_lines:
            return new_lines

        prev = self._prev_lines
        # Find where the old content ends in the new content.
        # Walk backwards from the end of prev to find a matching suffix in new.
        prev_len = len(prev)
        new_len = len(new_lines)

        if new_len <= prev_len:
            # Screen may have been cleared or reflowed — check tail match
            if new_lines == prev[-new_len:]:
                return []

        # Find the last line of prev in new_lines (searching from end)
        match_idx = -1
        search_start = max(0, new_len - prev_len - 50)  # don't search too far back
        for i in range(new_len - 1, search_start - 1, -1):
            if prev_len > 0 and new_lines[i] == prev[-1]:
                # Verify a few more lines match
                ok = True
                check_count = min(3, prev_len)
                for j in range(1, check_count):
                    pi = prev_len - 1 - j
                    ni = i - j
                    if ni < 0 or prev[pi] != new_lines[ni]:
                        ok = False
                        break
                if ok:
                    match_idx = i
                    break

        if match_idx >= 0 and match_idx + 1 < new_len:
            return new_lines[match_idx + 1:]

        # Fallback: simple length-based diff
        if new_len > prev_len:
            return new_lines[prev_len:]

        return []

    def _process_line(self, line: str) -> list[ParseEvent]:
        """Run a single line through the state machine."""
        stripped = line.strip()
        if not stripped:
            return []

        events: list[ParseEvent] = []

        # --- Idle prompt detection (any state) ---
        if _RE_PROMPT.match(line):
            # Flush pending interaction
            events.extend(self._flush_interaction())
            # Flush pending tool result
            events.extend(self._flush_tool())
            self._state = TuiState.IDLE
            events.append(ParseEvent(kind="idle"))
            return events

        # --- INTERACTION state: collect lines until prompt/idle ---
        if self._state == TuiState.INTERACTION:
            # Check for selection prompt (❯) which signals options are done
            m_ask = _RE_ASK_PROMPT.match(line)
            if m_ask:
                # Flush — the interaction is now waiting for input
                events.extend(self._flush_interaction())
                return events
            # Accumulate interaction content lines
            self._interaction_lines.append(stripped)
            return events

        # --- Permission detection (any state except INTERACTION) ---
        m_perm = _RE_PERMISSION.search(stripped)
        if m_perm:
            tool_name = m_perm.group(1) or ""
            self._state = TuiState.PERMISSION
            events.append(ParseEvent(
                kind="permission",
                data={"tool_name": tool_name.strip(), "raw": stripped},
            ))
            return events

        # --- Separator lines — skip ---
        if _RE_SEPARATOR.match(stripped):
            return []

        # --- Cost line ---
        m_cost = _RE_COST.search(stripped)
        if m_cost and ("cost" in stripped.lower() or "total" in stripped.lower()):
            events.append(ParseEvent(
                kind="cost", data={"usd": float(m_cost.group(1))}
            ))
            return events

        # --- State-specific processing ---

        # STARTUP: ignore everything until first ❯ (handled above)
        if self._state == TuiState.STARTUP:
            return []

        # Tool start: ⏺ ToolName(...)
        m_tool = _RE_TOOL_START.match(line)
        if m_tool:
            events.extend(self._flush_tool())
            tool_name = m_tool.group(1)
            args = m_tool.group(2).rstrip(")")

            # Check if this is an interaction tool
            if tool_name in _INTERACTION_TOOLS:
                self._interaction_tool = tool_name
                self._interaction_lines = [args] if args else []
                self._state = TuiState.INTERACTION
                return events

            self._current_tool = tool_name
            self._tool_result_lines = []
            self._state = TuiState.TOOL_RUNNING
            events.append(ParseEvent(
                kind="tool_start",
                data={"tool_name": tool_name, "args": args},
            ))
            return events

        # Tool result lines: ⎿ ...
        m_result = _RE_TOOL_RESULT.match(line)
        if m_result and self._state == TuiState.TOOL_RUNNING:
            self._tool_result_lines.append(m_result.group(1))
            return []

        # Response marker: ⏺ text
        m_resp = _RE_RESPONSE_MARKER.match(line)
        if m_resp:
            # Flush any pending tool
            events.extend(self._flush_tool())
            text = m_resp.group(1)
            if text:
                self._state = TuiState.RESPONDING
                events.append(ParseEvent(kind="text", data={"text": text}))
            return events

        # Continuation text in RESPONDING state (no marker prefix)
        if self._state == TuiState.RESPONDING:
            # Error detection
            if _RE_ERROR.search(stripped):
                events.append(ParseEvent(
                    kind="error", data={"text": stripped}
                ))
            else:
                events.append(ParseEvent(kind="text", data={"text": stripped}))
            return events

        # Continuation text in TOOL_RUNNING (tool output without ⎿ prefix)
        if self._state == TuiState.TOOL_RUNNING:
            self._tool_result_lines.append(stripped)
            return []

        return events

    def _flush_tool(self) -> list[ParseEvent]:
        """Emit tool_end if a tool is currently tracked."""
        if self._current_tool is None:
            return []
        result = "\n".join(self._tool_result_lines)
        event = ParseEvent(
            kind="tool_end",
            data={"tool_name": self._current_tool, "result": result},
        )
        self._current_tool = None
        self._tool_result_lines = []
        return [event]

    def _flush_interaction(self) -> list[ParseEvent]:
        """Emit ask_user or plan_approval if an interaction tool is being tracked."""
        if self._interaction_tool is None:
            return []
        raw = "\n".join(self._interaction_lines)
        # Parse numbered options from accumulated lines
        options: list[dict[str, str]] = []
        question_parts: list[str] = []
        for ln in self._interaction_lines:
            m = _RE_OPTION_LINE.match(ln)
            if m:
                options.append({"number": m.group(1), "label": m.group(2).strip()})
            else:
                question_parts.append(ln)

        if self._interaction_tool == "AskUserQuestion":
            kind = "ask_user"
            data = {
                "questions": question_parts,
                "options": options,
                "raw": raw,
            }
        else:  # ExitPlanMode
            kind = "plan_approval"
            data = {
                "plan": "\n".join(question_parts),
                "raw": raw,
            }

        event = ParseEvent(kind=kind, data=data)
        self._interaction_tool = None
        self._interaction_lines = []
        return [event]
