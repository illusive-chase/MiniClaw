"""Per-session context registry — tracks loaded contexts and renders prompt section."""

from __future__ import annotations

from miniclaw.plugctx.loader import ContextEntry


class ContextRegistry:
    """Ordered collection of loaded context entries for a session."""

    def __init__(self) -> None:
        self._entries: dict[str, ContextEntry] = {}  # insertion-ordered

    def add(self, entry: ContextEntry) -> None:
        self._entries[entry.path] = entry

    def remove(self, path: str) -> ContextEntry | None:
        return self._entries.pop(path, None)

    def is_loaded(self, path: str) -> bool:
        return path in self._entries

    def get(self, path: str) -> ContextEntry | None:
        return self._entries.get(path)

    def loaded_paths(self) -> list[str]:
        return list(self._entries.keys())

    def all_entries(self) -> list[ContextEntry]:
        return list(self._entries.values())

    def total_tokens(self) -> int:
        return sum(e.token_estimate for e in self._entries.values())

    def dependents_of(self, path: str) -> list[str]:
        """Return loaded contexts whose manifest.requires includes `path`."""
        return [
            e.path
            for e in self._entries.values()
            if path in e.manifest.requires
        ]

    def render_prompt_section(self) -> str:
        """Render the loaded contexts into a system prompt section."""
        if not self._entries:
            return ""

        parts = ["--- Loaded Contexts ---\n"]
        for entry in self._entries.values():
            parts.append(f"<!-- context: {entry.path} -->")
            parts.append(entry.content.rstrip())
            parts.append("")  # blank line between entries

        parts.append("--- End Contexts ---")
        return "\n".join(parts)
