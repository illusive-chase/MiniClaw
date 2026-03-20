"""PlugCtx — structured context loading for agent system prompts.

Public facade that combines loader, resolver, and registry.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from miniclaw.plugctx.loader import (
    ContextEntry,
    ContextManifest,
    RuntimeConfig,
    discover_all_contexts,
    dotted_to_fs_path,
    list_child_contexts,
    load_context_entry,
)
from miniclaw.plugctx.registry import ContextRegistry
from miniclaw.plugctx.resolver import CircularDependencyError, resolve_dependencies

__all__ = [
    "PlugCtxManager",
    "ContextEntry",
    "ContextManifest",
    "RuntimeConfig",
    "CircularDependencyError",
]

logger = logging.getLogger(__name__)


class PlugCtxManager:
    """Per-session manager for loading/unloading context folders.

    Contexts live as folders under ctx_root, addressed by dotted paths
    (e.g. "general.coding" → ctx_root/general/coding/CONTEXT.md).
    """

    def __init__(
        self,
        ctx_root: str | Path,
        auto_load_paths: list[str] | None = None,
    ) -> None:
        self._ctx_root = Path(ctx_root)
        self._auto_load_paths = auto_load_paths or []
        self._registry = ContextRegistry()

    # --- CLI command handlers (return dicts for transport-agnostic formatting) ---

    def init_context(
        self, dotted_path: str, ctx_type: str, requires: list[str], workspace: str = ""
    ) -> dict:
        """Create a new context folder with scaffold files.

        Returns: {path, created: True/False, error: str|None}
        """
        fs_path = dotted_to_fs_path(self._ctx_root, dotted_path)
        if fs_path.exists():
            return {"path": str(fs_path), "created": False, "error": f"Directory already exists: {fs_path}"}

        # Derive name from last path segment
        name = dotted_path.rsplit(".", 1)[-1]

        try:
            fs_path.mkdir(parents=True, exist_ok=True)

            # Write CONTEXT.md
            context_md = (
                f"# {name}\n"
                "\n"
                "<!-- Describe this context here. This content is injected into the agent's system prompt. -->\n"
            )
            (fs_path / "CONTEXT.md").write_text(context_md, encoding="utf-8")

            # Write manifest.yaml
            manifest_data: dict = {
                "name": name,
                "description": "",
                "type": ctx_type,
                "requires": requires,
                "tags": [],
            }
            if ctx_type == "project" and workspace:
                manifest_data["runtime"] = {"workspace": workspace}
            (fs_path / "manifest.yaml").write_text(
                yaml.dump(manifest_data, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )

            # Write .gitignore
            gitignore = (
                "# Ignore everything by default\n"
                "*\n"
                "\n"
                "# Allow context files\n"
                "!CONTEXT.md\n"
                "!manifest.yaml\n"
                "!.gitignore\n"
                "\n"
                "# Uncomment to include additional files:\n"
                "# !scripts/\n"
                "# !scripts/**\n"
            )
            (fs_path / ".gitignore").write_text(gitignore, encoding="utf-8")

            logger.info("Created context '%s' at %s", dotted_path, fs_path)
            return {"path": str(fs_path), "created": True, "error": None}
        except Exception as e:
            return {"path": str(fs_path), "created": False, "error": str(e)}

    def load(self, dotted_path: str, allow_project: bool = True) -> dict:
        """Load a context and its dependencies.

        Args:
            dotted_path: Dotted path to the context to load.
            allow_project: If False, reject project-type contexts (used for ccagent subagents).

        Returns: {loaded: [...], already_loaded: [...], failed: [...],
                  total_tokens: int, children: [...], warnings: [...]}
        """
        loaded: list[str] = []
        already_loaded: list[str] = []
        failed: list[str] = []
        warnings: list[str] = []

        # Resolve dependencies
        try:
            load_order = resolve_dependencies(
                self._ctx_root,
                dotted_path,
                set(self._registry.loaded_paths()),
            )
        except CircularDependencyError as e:
            return {
                "loaded": [],
                "already_loaded": [],
                "failed": [],
                "error": str(e),
                "total_tokens": self._registry.total_tokens(),
                "children": [],
                "warnings": [],
            }

        # Project exclusivity: check if target is a project and swap if needed
        try:
            target_entry = load_context_entry(self._ctx_root, dotted_path)
            if target_entry.manifest.type == "project":
                if not allow_project:
                    return {
                        "loaded": [],
                        "already_loaded": [],
                        "failed": [],
                        "error": "Project-type contexts cannot be loaded in ccagent sessions.",
                        "total_tokens": self._registry.total_tokens(),
                        "children": [],
                        "warnings": [],
                    }
                existing_project = self._registry.active_project()
                if existing_project is not None and existing_project.path != dotted_path:
                    warnings.append(
                        f"Swapping project ctx '{existing_project.path}' -> '{dotted_path}'"
                    )
                    self._registry.remove(existing_project.path)
                    logger.info(
                        "Unloaded project context '%s' (swapped for '%s')",
                        existing_project.path, dotted_path,
                    )
        except FileNotFoundError:
            pass  # will be caught below during load

        # Load each in dependency order
        for path in load_order:
            if self._registry.is_loaded(path):
                already_loaded.append(path)
                continue
            try:
                source = "dependency" if path != dotted_path else "manual"
                entry = load_context_entry(self._ctx_root, path, source=source)
                self._registry.add(entry)
                loaded.append(path)
                logger.info("Loaded context '%s' (~%d tokens)", path, entry.token_estimate)
            except FileNotFoundError:
                failed.append(path)
                logger.warning("Failed to load context '%s': not found", path)

        # List children for discovery
        children = list_child_contexts(self._ctx_root, dotted_path)
        children = [c for c in children if not self._registry.is_loaded(c)]

        return {
            "loaded": loaded,
            "already_loaded": already_loaded,
            "failed": failed,
            "total_tokens": self._registry.total_tokens(),
            "children": children,
            "warnings": warnings,
        }

    def unload(self, dotted_path: str) -> dict:
        """Unload a context.

        Returns: {unloaded: str | None, freed_tokens: int, warnings: [...]}
        """
        warnings: list[str] = []

        # Check for dependents
        dependents = self._registry.dependents_of(dotted_path)
        if dependents:
            warnings.append(
                f"Warning: the following loaded contexts depend on '{dotted_path}': "
                + ", ".join(dependents)
            )

        entry = self._registry.remove(dotted_path)
        if entry is None:
            return {
                "unloaded": None,
                "freed_tokens": 0,
                "warnings": [f"Context '{dotted_path}' is not loaded."],
            }

        logger.info(
            "Unloaded context '%s' (freed ~%d tokens)", dotted_path, entry.token_estimate
        )
        return {
            "unloaded": dotted_path,
            "freed_tokens": entry.token_estimate,
            "warnings": warnings,
        }

    def list_contexts(self) -> list[dict]:
        """List all available contexts with loaded status."""
        all_paths = discover_all_contexts(self._ctx_root)
        result: list[dict] = []
        for path in all_paths:
            loaded = self._registry.is_loaded(path)
            entry = self._registry.get(path) if loaded else None
            result.append({
                "path": path,
                "loaded": loaded,
                "token_estimate": entry.token_estimate if entry else None,
                "source": entry.source if entry else None,
            })
        return result

    def status(self) -> dict:
        """Return details about currently loaded contexts."""
        entries = self._registry.all_entries()
        return {
            "loaded": [
                {
                    "path": e.path,
                    "token_estimate": e.token_estimate,
                    "source": e.source,
                    "name": e.manifest.name,
                    "description": e.manifest.description,
                }
                for e in entries
            ],
            "total_tokens": self._registry.total_tokens(),
        }

    def info(self, dotted_path: str) -> dict:
        """Return manifest details and content preview for a context."""
        try:
            entry = load_context_entry(self._ctx_root, dotted_path)
        except FileNotFoundError:
            return {"error": f"Context '{dotted_path}' not found."}

        preview = entry.content[:500]
        if len(entry.content) > 500:
            preview += "\n..."

        children = list_child_contexts(self._ctx_root, dotted_path)

        return {
            "path": dotted_path,
            "name": entry.manifest.name,
            "description": entry.manifest.description,
            "requires": entry.manifest.requires,
            "tags": entry.manifest.tags,
            "token_estimate": entry.token_estimate,
            "loaded": self._registry.is_loaded(dotted_path),
            "preview": preview,
            "children": children,
        }

    # --- Agent integration ---

    def active_runtime(self) -> RuntimeConfig | None:
        """Return the RuntimeConfig of the active project context, or None."""
        project = self._registry.active_project()
        if project is not None and project.manifest.runtime is not None:
            return project.manifest.runtime
        return None

    def active_project_cwd(self) -> str | None:
        """Return the filesystem directory of the active project-type context, or None."""
        project = self._registry.active_project()
        if project is not None:
            return str(dotted_to_fs_path(self._ctx_root, project.path))
        return None

    @property
    def ctx_root(self) -> Path:
        return self._ctx_root

    def render_prompt_section(self) -> str:
        """Render all loaded contexts as a system prompt section."""
        return self._registry.render_prompt_section()

    # --- Persistence ---

    def loaded_paths(self) -> list[str]:
        """Return dotted paths of all loaded contexts (for serialization)."""
        return self._registry.loaded_paths()

    def restore_from_paths(self, paths: list[str]) -> list[str]:
        """Reload contexts from a list of dotted paths (e.g. after resume).

        Returns list of paths that failed to load.
        """
        failed: list[str] = []
        for path in paths:
            if self._registry.is_loaded(path):
                continue
            try:
                entry = load_context_entry(self._ctx_root, path, source="manual")
                self._registry.add(entry)
            except FileNotFoundError:
                failed.append(path)
                logger.warning("Failed to restore context '%s': not found", path)
        return failed

    # --- Startup ---

    def auto_load(self) -> list[str]:
        """Load auto_load contexts from config. Returns failed paths."""
        failed: list[str] = []
        for path in self._auto_load_paths:
            result = self.load(path)
            failed.extend(result.get("failed", []))
            if "error" in result:
                failed.append(path)
                logger.warning("Auto-load failed for '%s': %s", path, result["error"])
        return failed
