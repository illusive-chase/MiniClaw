"""Execution tracker for subagent runs."""

from datetime import datetime

from .types import SubagentRecord


class ExecutionTracker:
    """Tracks subagent execution history for supervisory queries."""

    def __init__(self):
        self._records: list[SubagentRecord] = []
        self._counter: int = 0

    def start(self, type: str, task: str, model: str | None = None) -> SubagentRecord:
        """Create a new 'running' record and return it."""
        self._counter += 1
        record = SubagentRecord(
            id=f"sub-{self._counter}",
            type=type,
            task=task,
            status="running",
            model=model,
        )
        self._records.append(record)
        return record

    def complete(self, record: SubagentRecord, result: str) -> None:
        """Mark a record as completed with result preview."""
        record.status = "completed"
        record.finished_at = datetime.now()
        record.runtime_ms = int(
            (record.finished_at - record.started_at).total_seconds() * 1000
        )
        record.result_preview = result[:200]

    def fail(self, record: SubagentRecord, error: str) -> None:
        """Mark a record as failed."""
        record.status = "failed"
        record.finished_at = datetime.now()
        record.runtime_ms = int(
            (record.finished_at - record.started_at).total_seconds() * 1000
        )
        record.result_preview = f"[ERROR] {error[:180]}"

    def summary(self) -> str:
        """Return a formatted text table of all records."""
        if not self._records:
            return "No subagent runs recorded."

        lines = ["ID        | Type     | Status    | Runtime  | Task"]
        lines.append("--------- | -------- | --------- | -------- | ----")
        for r in self._records:
            runtime = f"{r.runtime_ms}ms" if r.runtime_ms is not None else "..."
            task_preview = r.task[:50] + ("..." if len(r.task) > 50 else "")
            lines.append(
                f"{r.id:<9} | {r.type:<8} | {r.status:<9} | {runtime:<8} | {task_preview}"
            )
        return "\n".join(lines)

    def clear(self) -> None:
        """Reset all records."""
        self._records.clear()
        self._counter = 0
