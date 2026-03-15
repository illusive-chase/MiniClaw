"""Subagent type definitions, prompts, and data structures."""

from dataclasses import dataclass, field
from datetime import datetime

SUBAGENT_TYPES: dict[str, list[str]] = {
    "reader": ["file_read", "glob", "grep"],
    "editor": ["file_read", "file_edit", "file_write", "glob", "grep"],
    "executor": ["file_read", "file_edit", "file_write", "git", "shell", "glob", "grep"],
}

SUBAGENT_DESCS: dict[str, str] = {
    "reader": "Search/read files",
    "editor": "Search/read/edit files.",
    "executor": "Search/read/edit files; execute shell commands.",
}

SUBAGENT_DEFAULT_MODELS: dict[str, str] = {
    "reader": "claude-sonnet-4-6",
    "editor": "claude-opus-4-6",
    "executor": "claude-opus-4-6",
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
        "You are a general assistant. Your can to read/edit files and run "
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
