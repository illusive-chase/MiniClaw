"""Glob tool for finding files by pattern."""

from pathlib import Path

from miniclaw.plugctx.vpath import CTX_SCHEME, WORKSPACE_SCHEME, detect_protocol, resolve_ctx, resolve_workspace

from .base import Tool, ToolPathContext, ToolResult


class GlobTool(Tool):
    def __init__(self, cwd: str = "."):
        self._cwd = Path(cwd)
        self._path_ctx: ToolPathContext | None = None

    def name(self) -> str:
        return "glob"

    def description(self) -> str:
        return (
            "Find files matching a glob pattern. "
            "Examples: '**/*.py' (all Python files), 'src/**/*.ts' (TypeScript under src/), "
            "'*.md' (markdown in current dir). Supports ctx:// and workspace:// base paths."
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
                    "description": "Directory to search in (relative, absolute, ctx://, or workspace://). Defaults to workspace root.",
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
            protocol = detect_protocol(base)
            if protocol is not None:
                scheme, relative = protocol
                if scheme == CTX_SCHEME:
                    if self._path_ctx and self._path_ctx.ctx_root:
                        search_dir = resolve_ctx(relative, self._path_ctx.ctx_root)
                    else:
                        return ToolResult(output="ctx:// paths require an active plugctx root.", success=False)
                elif scheme == WORKSPACE_SCHEME:
                    if self._path_ctx and self._path_ctx.workspace:
                        if self._path_ctx.remote and self._path_ctx.remote_reader:
                            abs_path = resolve_workspace(relative, self._path_ctx.workspace)
                            try:
                                matches = await self._path_ctx.remote_reader.glob(abs_path, pattern)
                                if not matches:
                                    return ToolResult(output="No files matched the pattern.")
                                if len(matches) > 500:
                                    matches = matches[:500]
                                    matches.append(f"... (truncated, {len(matches)} total)")
                                return ToolResult(output="\n".join(matches))
                            except Exception as e:
                                return ToolResult(output=f"Remote glob error: {e}", success=False)
                        else:
                            search_dir = Path(resolve_workspace(relative, self._path_ctx.workspace))
                    else:
                        return ToolResult(output="workspace:// paths require an active project context with a workspace.", success=False)
                else:
                    return ToolResult(output=f"Unknown protocol: {scheme}", success=False)
            else:
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
