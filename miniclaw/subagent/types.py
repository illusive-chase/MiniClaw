"""Subagent type definitions, prompts, and data structures."""

from dataclasses import dataclass, field
from datetime import datetime

SUBAGENT_TYPES: dict[str, list[str]] = {
    "reader": ["file_read"],
    "editor": ["file_read", "file_edit", "file_write"],
    "executor": ["file_read", "file_edit", "file_write", "git", "shell"],
}

SUBAGENT_DESCS: dict[str, str] = {
    "reader": "Read files.",
    "editor": "Read/edit files.",
    "executor": "Read/edit files; execute shell commands.",
}

SUBAGENT_PROMPTS: dict[str, str] = {
    "reader": (
        "You are a file reader assistant. Your job is to read and analyze files "
        "as requested. You can only read files — you cannot modify or create them."
    ),
    "editor": (
        "You are a file editor assistant. Your job is to read, edit, and create files "
        "as requested. Be precise with edits and preserve existing formatting."
    ),
    "executor": (
        "You are a task executor assistant. Your job is to read/edit files and run "
        "shell commands as requested. Be careful with destructive operations."
    ),
}


@dataclass
class SubagentRecord:
    """Record of a single subagent execution."""

    id: str
    type: str
    task: str
    status: str  # "running", "completed", "failed"
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    runtime_ms: int | None = None
    result_preview: str = ""
    model: str | None = None
