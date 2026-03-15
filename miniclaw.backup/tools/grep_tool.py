"""Grep tool for searching file contents."""

import re
from pathlib import Path

from .base import Tool, ToolResult


class GrepTool(Tool):
    def __init__(self, workspace_dir: str = ".workspace"):
        self._workspace_dir = Path(workspace_dir)

    def name(self) -> str:
        return "grep"

    def description(self) -> str:
        return (
            "Search file contents for a regex pattern. "
            "Returns matching lines with file paths and line numbers. "
            "Optionally filter by file glob (e.g. '*.py')."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for in file contents",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (relative to workspace or absolute). Defaults to workspace root.",
                },
                "glob": {
                    "type": "string",
                    "description": "File glob filter (e.g. '*.py', '*.ts'). Only search files matching this pattern.",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search. Defaults to false.",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, args: dict) -> ToolResult:
        pattern = args.get("pattern", "")
        if not pattern:
            return ToolResult(output="No pattern provided", success=False)

        flags = re.IGNORECASE if args.get("case_insensitive", False) else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(output=f"Invalid regex: {e}", success=False)

        target = args.get("path", "")
        if target:
            search_path = Path(target)
            if not search_path.is_absolute():
                search_path = self._workspace_dir / search_path
        else:
            search_path = self._workspace_dir

        if not search_path.exists():
            return ToolResult(output=f"Path not found: {search_path}", success=False)

        file_glob = args.get("glob", "")
        results: list[str] = []
        max_results = 200

        try:
            if search_path.is_file():
                files = [search_path]
            else:
                glob_pattern = file_glob if file_glob else "**/*"
                files = sorted(p for p in search_path.glob(glob_pattern) if p.is_file())

            for fpath in files:
                if len(results) >= max_results:
                    break
                try:
                    text = fpath.read_text(errors="replace")
                except Exception:
                    continue
                rel = str(fpath.relative_to(self._workspace_dir)) if str(fpath).startswith(str(self._workspace_dir)) else str(fpath)
                for line_num, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        results.append(f"{rel}:{line_num}: {line.rstrip()}")
                        if len(results) >= max_results:
                            break

            if not results:
                return ToolResult(output="No matches found.")
            output = "\n".join(results)
            if len(results) >= max_results:
                output += f"\n... (truncated at {max_results} matches)"
            return ToolResult(output=output)
        except Exception as e:
            return ToolResult(output=f"Error: {e}", success=False)
