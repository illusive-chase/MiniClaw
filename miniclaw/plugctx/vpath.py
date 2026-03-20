"""Virtual path protocol resolution for ctx:// and workspace://."""

from __future__ import annotations

from pathlib import Path

from miniclaw.plugctx.loader import dotted_to_fs_path

CTX_SCHEME = "ctx://"
WORKSPACE_SCHEME = "workspace://"


def detect_protocol(path: str) -> tuple[str, str] | None:
    """Return (scheme, relative_path) if path uses a virtual protocol, else None."""
    if path.startswith(CTX_SCHEME):
        return CTX_SCHEME, path[len(CTX_SCHEME):]
    if path.startswith(WORKSPACE_SCHEME):
        return WORKSPACE_SCHEME, path[len(WORKSPACE_SCHEME):]
    return None


def resolve_ctx(relative: str, ctx_root: Path) -> Path:
    """Resolve ctx://skill.feishu.cards/docs/foo.md to absolute local path.

    First path segment (up to /) is the dotted context path.
    Remainder is the file path within that context folder.
    """
    parts = relative.split("/", 1)
    dotted = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    ctx_dir = dotted_to_fs_path(ctx_root, dotted)
    return ctx_dir / rest if rest else ctx_dir


def resolve_workspace(relative: str, workspace: str) -> str:
    """Resolve workspace://path to absolute path string."""
    return str(Path(workspace) / relative)
