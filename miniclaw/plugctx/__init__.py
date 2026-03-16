"""PlugCtx — structured context loading for agent system prompts.

Public facade that combines loader, resolver, and registry.
"""

from __future__ import annotations

import logging
from pathlib import Path

from miniclaw.plugctx.loader import (
    ContextEntry,
    ContextManifest,
    discover_all_contexts,
    list_child_contexts,
    load_context_entry,
)
from miniclaw.plugctx.registry import ContextRegistry
from miniclaw.plugctx.resolver import CircularDependencyError, resolve_dependencies

__all__ = [
    "PlugCtxManager",
    "ContextEntry",
    "ContextManifest",
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

    def load(self, dotted_path: str) -> dict:
        """Load a context and its dependencies.

        Returns: {loaded: [...], already_loaded: [...], failed: [...],
                  total_tokens: int, children: [...]}
        """
        loaded: list[str] = []
        already_loaded: list[str] = []
        failed: list[str] = []

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
            }

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
