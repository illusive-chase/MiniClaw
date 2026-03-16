"""Glob tool for finding files by pattern."""

from pathlib import Path

from .base import Tool, ToolResult


class GlobTool(Tool):
    def __init__(self, cwd: str = "."):
        self._cwd = Path(cwd)

    def name(self) -> str:
        return "glob"

    def description(self) -> str:
        return (
            "Find files matching a glob pattern. "
            "Examples: '**/*.py' (all Python files), 'src/**/*.ts' (TypeScript under src/), "
            "'*.md' (markdown in current dir). Returns matching file paths."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files (e.g. '**/*.py', 'src/*.ts')",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (relative to workspace or absolute). Defaults to workspace root.",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, args: dict) -> ToolResult:
        pattern = args.get("pattern", "")
        if not pattern:
            return ToolResult(output="No pattern provided", success=False)

        base = args.get("path", "")
        if base:
            search_dir = Path(base)
            if not search_dir.is_absolute():
                search_dir = self._cwd / search_dir
        else:
            search_dir = self._cwd

        if not search_dir.is_dir():
            return ToolResult(output=f"Directory not found: {search_dir}", success=False)

        try:
            matches = sorted(str(p.relative_to(search_dir)) for p in search_dir.glob(pattern) if p.is_file())
            if not matches:
                return ToolResult(output="No files matched the pattern.")
            if len(matches) > 500:
                matches = matches[:500]
                matches.append(f"... (truncated, {len(matches)} total)")
            return ToolResult(output="\n".join(matches))
        except Exception as e:
            return ToolResult(output=f"Error: {e}", success=False)
