"""Dependency resolution for plugctx with cycle detection."""

from __future__ import annotations

from pathlib import Path

from miniclaw.plugctx.loader import load_context_entry


class CircularDependencyError(Exception):
    """Raised when a circular dependency is detected."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(f"Circular dependency: {' -> '.join(cycle)}")


def resolve_dependencies(
    ctx_root: Path,
    target: str,
    already_loaded: set[str],
) -> list[str]:
    """Resolve dependencies for a target context using DFS topological sort.

    Returns an ordered list of dotted paths to load (deepest deps first),
    excluding anything in already_loaded.

    Uses GRAY/BLACK coloring for cycle detection:
      - WHITE (not visited): not in any set
      - GRAY (in progress): currently on the DFS stack
      - BLACK (done): fully processed
    """
    gray: set[str] = set()
    black: set[str] = set()
    order: list[str] = []

    def _visit(path: str, stack: list[str]) -> None:
        if path in black or path in already_loaded:
            return
        if path in gray:
            # Find the cycle start in the stack
            cycle_start = stack.index(path)
            raise CircularDependencyError(stack[cycle_start:] + [path])

        gray.add(path)
        stack.append(path)

        # Load manifest to find requires
        try:
            entry = load_context_entry(ctx_root, path)
            for dep in entry.manifest.requires:
                _visit(dep, stack)
        except FileNotFoundError:
            pass  # missing dep — will be reported by the caller

        stack.pop()
        gray.discard(path)
        black.add(path)
        order.append(path)

    _visit(target, [])
    return order
