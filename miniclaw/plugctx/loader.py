"""Filesystem I/O for loading context entries from disk."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class RuntimeConfig:
    """Runtime environment for project-type contexts."""

    workspace: str = ""     # absolute path on target machine
    remote: str = ""        # optional, references config.yaml remotes.<name>
    env: dict[str, str] = field(default_factory=dict)  # injected into ccagent


@dataclass
class ContextManifest:
    """Parsed manifest.yaml for a context folder."""

    name: str = ""
    description: str = ""
    requires: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    type: str = "skill"       # "project" | "skill"
    only_plan: bool = False    # unload automatically when plan is approved
    runtime: RuntimeConfig | None = None  # project-type only


@dataclass
class ContextEntry:
    """A loaded context: dotted path + content + metadata."""

    path: str  # dotted path e.g. "general.coding"
    content: str  # raw CONTEXT.md text
    manifest: ContextManifest
    token_estimate: int  # len(content) // 4
    source: str = "manual"  # "manual" | "auto" | "dependency"


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def dotted_to_fs_path(ctx_root: Path, dotted_path: str) -> Path:
    """Convert a dotted path like 'general.coding' to a filesystem path."""
    parts = dotted_path.split(".")
    return ctx_root.joinpath(*parts)


def load_context_entry(
    ctx_root: Path, dotted_path: str, source: str = "manual"
) -> ContextEntry:
    """Load a single context entry from disk.

    Reads CONTEXT.md (required) and manifest.yaml (optional).
    Raises FileNotFoundError if CONTEXT.md is missing.
    """
    fs_path = dotted_to_fs_path(ctx_root, dotted_path)
    context_file = fs_path / "CONTEXT.md"

    if not context_file.exists():
        raise FileNotFoundError(
            f"No CONTEXT.md found at '{dotted_path}' ({context_file})"
        )

    content = context_file.read_text(encoding="utf-8")

    # Load optional manifest
    manifest = ContextManifest()
    manifest_file = fs_path / "manifest.yaml"
    if manifest_file.exists():
        try:
            data = yaml.safe_load(manifest_file.read_text(encoding="utf-8")) or {}
            ctx_type = data.get("type", "skill")

            # Parse runtime config
            runtime: RuntimeConfig | None = None
            runtime_data = data.get("runtime")
            if runtime_data and isinstance(runtime_data, dict):
                runtime = RuntimeConfig(
                    workspace=runtime_data.get("workspace", ""),
                    remote=runtime_data.get("remote", ""),
                    env=runtime_data.get("env", {}),
                )
            elif data.get("workspace"):
                # Backward compat: migrate top-level workspace to runtime
                runtime = RuntimeConfig(workspace=data["workspace"])

            # Skill contexts should not have runtime
            if ctx_type == "skill" and runtime is not None:
                logger.warning(
                    "Ignoring runtime config on skill-type context '%s'",
                    dotted_path,
                )
                runtime = None

            manifest = ContextManifest(
                name=data.get("name", ""),
                description=data.get("description", ""),
                requires=data.get("requires", []),
                tags=data.get("tags", []),
                type=ctx_type,
                only_plan=bool(data.get("only_plan", False)),
                runtime=runtime,
            )
        except Exception:
            logger.warning("Failed to parse manifest.yaml for '%s'", dotted_path)

    return ContextEntry(
        path=dotted_path,
        content=content,
        manifest=manifest,
        token_estimate=estimate_tokens(content),
        source=source,
    )


def discover_all_contexts(ctx_root: Path) -> list[str]:
    """Walk the context tree and return all dotted paths with a CONTEXT.md."""
    if not ctx_root.is_dir():
        return []

    results: list[str] = []
    for context_file in ctx_root.rglob("CONTEXT.md"):
        rel = context_file.parent.relative_to(ctx_root)
        dotted = ".".join(rel.parts)
        if dotted:
            results.append(dotted)

    results.sort()
    return results


def list_child_contexts(ctx_root: Path, dotted_path: str) -> list[str]:
    """Return immediate children of a dotted path that have CONTEXT.md."""
    parent_dir = dotted_to_fs_path(ctx_root, dotted_path) if dotted_path else ctx_root
    if not parent_dir.is_dir():
        return []

    children: list[str] = []
    for child in sorted(parent_dir.iterdir()):
        if child.is_dir() and (child / "CONTEXT.md").exists():
            child_dotted = f"{dotted_path}.{child.name}" if dotted_path else child.name
            children.append(child_dotted)

    return children
