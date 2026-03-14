"""Activity tracking for real-time tool/subagent status display.

Agent-agnostic module — no SDK dependency. Provides:
- ActivityEvent: yielded in streams alongside text/interactions
- ActivitySnapshot: frozen state for rendering
- ActivityTracker: stateful event processor that produces snapshots
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class ActivityKind(Enum):
    TOOL = "tool"
    AGENT = "agent"

class ActivityStatus(Enum):
    START = "start"
    FINISH = "finish"
    FAILED = "failed"
    PROGRESS = "progress"

@dataclass
class ActivityEvent:
    """Yielded in the stream alongside str/InteractionRequest."""

    kind: ActivityKind
    status: ActivityStatus
    id: str  # ToolUseBlock.id or task_id
    name: str  # Tool name or agent type
    summary: str = ""
    timestamp: float = field(default_factory=time.monotonic)
    finished: float | None = None  # set on FINISH/FAILED

    def update(self, other: ActivityEvent) -> None:
        self.status = other.status
        self.finished = other.finished

@dataclass
class ActivitySnapshot:
    """Frozen state for rendering — produced by ActivityTracker.snapshot()."""

    tool_earliest: float = 0.0
    tool_finished: float | None = None  # latest tool finish; None if any still running
    agent_earliest: float = 0.0
    agent_finished: float | None = None  # latest agent finish; None if any still running
    agent_recents: list[ActivityEvent] = field(default_factory=list)
    tool_recents: list[ActivityEvent] = field(default_factory=list)
    tool_done: int = 0
    tool_total: int = 0
    agent_done: int = 0
    agent_total: int = 0

    @property
    def has_activity(self) -> bool:
        return (self.tool_total + self.agent_total) > 0


class ActivityTracker:
    """Maintains activity state, applies events, produces snapshots."""

    def __init__(self) -> None:
        self._active_tools: dict[str, ActivityEvent] = {}
        self._active_agents: dict[str, ActivityEvent] = {}

    def apply(self, event: ActivityEvent) -> None:
        if event.status == ActivityStatus.PROGRESS:
            return
        if event.status in (ActivityStatus.FINISH, ActivityStatus.FAILED):
            event.finished = time.monotonic()
        if event.kind == ActivityKind.TOOL:
            self._active_tools.setdefault(event.id, event).update(event)
        elif event.kind == ActivityKind.AGENT:
            self._active_agents.setdefault(event.id, event).update(event)

    def snapshot(self, n: int = 5) -> ActivitySnapshot:
        if not self._active_tools and not self._active_agents:
            return ActivitySnapshot()

        # Per-category earliest start and latest finish
        tool_earliest = 0.0
        tool_finished: float | None = None
        if self._active_tools:
            tool_earliest = min(e.timestamp for e in self._active_tools.values())
            all_done = all(
                e.status in (ActivityStatus.FINISH, ActivityStatus.FAILED)
                for e in self._active_tools.values()
            )
            if all_done:
                tool_finished = max(e.finished for e in self._active_tools.values())  # type: ignore[arg-type]

        agent_earliest = 0.0
        agent_finished: float | None = None
        if self._active_agents:
            agent_earliest = min(e.timestamp for e in self._active_agents.values())
            all_done = all(
                e.status in (ActivityStatus.FINISH, ActivityStatus.FAILED)
                for e in self._active_agents.values()
            )
            if all_done:
                agent_finished = max(e.finished for e in self._active_agents.values())  # type: ignore[arg-type]

        done_statuses = (ActivityStatus.FINISH, ActivityStatus.FAILED)

        return ActivitySnapshot(
            tool_earliest=tool_earliest,
            tool_finished=tool_finished,
            agent_earliest=agent_earliest,
            agent_finished=agent_finished,
            agent_recents=sorted(self._active_agents.values(), key=lambda e: e.timestamp, reverse=True)[:n],
            tool_recents=sorted(self._active_tools.values(), key=lambda e: e.timestamp, reverse=True)[:n],
            tool_done=sum(1 for e in self._active_tools.values() if e.status in done_statuses),
            tool_total=len(self._active_tools),
            agent_done=sum(1 for e in self._active_agents.values() if e.status in done_statuses),
            agent_total=len(self._active_agents),
        )

    def reset(self) -> None:
        self._active_tools.clear()
        self._active_agents.clear()
