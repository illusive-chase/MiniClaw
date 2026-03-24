"""Virtual path protocol resolution for ctx:// and workspace://."""

from __future__ import annotations

import re
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


# Matches workspace:// or ctx:// followed by the relative path.
# Stops at whitespace, quotes, backticks, angle brackets, parens, etc.
_VPATH_RE = re.compile(r'(workspace://|ctx://)([^\s"\'`()<>,;\[\]]*)')


def resolve_virtual_paths(
    text: str,
    ctx_root: Path | None,
    workspace: str | None,
) -> str:
    """Replace all workspace:// and ctx:// URIs in *text* with absolute paths.

    URIs whose scheme cannot be resolved (missing ctx_root or workspace)
    are left unchanged.
    """

    def _replace(m: re.Match) -> str:
        scheme = m.group(1)
        relative = m.group(2)
        if scheme == WORKSPACE_SCHEME:
            if workspace is not None:
                return resolve_workspace(relative, workspace)
            return m.group(0)  # leave as-is
        if scheme == CTX_SCHEME:
            if ctx_root is not None:
                return str(resolve_ctx(relative, ctx_root))
            return m.group(0)
        return m.group(0)

    return _VPATH_RE.sub(_replace, text)


def build_mapping_prompt(
    ctx_root: Path | None,
    workspace: str | None,
) -> str:
    """Build a human-readable mapping section for virtual path protocols.

    Returns an empty string if neither root is available.
    """
    lines: list[str] = []
    if workspace:
        lines.append(f"- `workspace://` → {workspace}/")
    if ctx_root:
        lines.append(
            f"- `ctx://` → Resolve by replacing dots with `/` under {ctx_root}/\n"
            f"  Example: ctx://skill.feishu/docs/foo.md → {ctx_root}/skill/feishu/docs/foo.md"
        )
    if not lines:
        return ""
    return (
        "## Virtual Path Mapping\n"
        "The following virtual path protocols may appear in referenced documents:\n"
        + "\n".join(lines)
        + "\nWhen you encounter these protocols, translate them to real filesystem paths using the mapping above."
    )
