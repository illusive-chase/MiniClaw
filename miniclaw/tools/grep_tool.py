"""Grep tool for searching file contents."""

import re
from pathlib import Path

from miniclaw.plugctx.vpath import CTX_SCHEME, WORKSPACE_SCHEME, detect_protocol, resolve_ctx, resolve_workspace

from .base import Tool, ToolPathContext, ToolResult


class GrepTool(Tool):
    def __init__(self, cwd: str = "."):
        self._cwd = Path(cwd)
        self._path_ctx: ToolPathContext | None = None

    def name(self) -> str:
        return "grep"

    def description(self) -> str:
        return (
            "Search file contents for a regex pattern. "
            "Returns matching lines with file paths and line numbers. "
            "Supports ctx:// and workspace:// paths. "
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
                    "description": "File or directory to search in (relative, absolute, ctx://, or workspace://). Defaults to workspace root.",
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

        # Virtual protocol resolution
        if target:
            protocol = detect_protocol(target)
            if protocol is not None:
                scheme, relative = protocol
                if scheme == CTX_SCHEME:
                    if self._path_ctx and self._path_ctx.ctx_root:
                        search_path = resolve_ctx(relative, self._path_ctx.ctx_root)
                    else:
                        return ToolResult(output="ctx:// paths require an active plugctx root.", success=False)
                elif scheme == WORKSPACE_SCHEME:
                    if self._path_ctx and self._path_ctx.workspace:
                        if self._path_ctx.remote and self._path_ctx.remote_reader:
                            abs_path = resolve_workspace(relative, self._path_ctx.workspace)
                            file_glob = args.get("glob", "")
                            try:
                                matches = await self._path_ctx.remote_reader.grep(abs_path, pattern, file_glob)
                                if not matches:
                                    return ToolResult(output="No matches found.")
                                output = "\n".join(matches)
                                if len(matches) >= 200:
                                    output += "\n... (truncated at 200 matches)"
                                return ToolResult(output=output)
                            except Exception as e:
                                return ToolResult(output=f"Remote grep error: {e}", success=False)
                        else:
                            search_path = Path(resolve_workspace(relative, self._path_ctx.workspace))
                    else:
                        return ToolResult(output="workspace:// paths require an active project context with a workspace.", success=False)
                else:
                    return ToolResult(output=f"Unknown protocol: {scheme}", success=False)
            else:
                search_path = Path(target)
                if not search_path.is_absolute():
                    search_path = self._cwd / search_path
        else:
            search_path = self._cwd

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
                rel = str(fpath.relative_to(self._cwd)) if str(fpath).startswith(str(self._cwd)) else str(fpath)
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
