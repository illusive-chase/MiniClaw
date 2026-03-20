"""File read tool."""

from pathlib import Path

from miniclaw.plugctx.vpath import CTX_SCHEME, WORKSPACE_SCHEME, detect_protocol, resolve_ctx, resolve_workspace

from .base import Tool, ToolPathContext, ToolResult


class FileReadTool(Tool):
    def __init__(self, cwd: str = "."):
        self._cwd = Path(cwd)
        self._path_ctx: ToolPathContext | None = None

    def name(self) -> str:
        return "file_read"

    def description(self) -> str:
        return "Read the contents of a file. Supports ctx:// and workspace:// virtual paths."

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (relative to workspace, absolute, ctx://, or workspace://)",
                },
            },
            "required": ["path"],
        }

    async def execute(self, args: dict) -> ToolResult:
        path_str = args.get("path", "")
        if not path_str:
            return ToolResult(output="No path provided", success=False)

        # Virtual protocol resolution
        protocol = detect_protocol(path_str)
        if protocol is not None:
            scheme, relative = protocol
            if scheme == CTX_SCHEME:
                if self._path_ctx and self._path_ctx.ctx_root:
                    path = resolve_ctx(relative, self._path_ctx.ctx_root)
                else:
                    return ToolResult(output="ctx:// paths require an active plugctx root.", success=False)
            elif scheme == WORKSPACE_SCHEME:
                if self._path_ctx and self._path_ctx.workspace:
                    if self._path_ctx.remote and self._path_ctx.remote_reader:
                        # Remote workspace: delegate to remote reader
                        abs_path = resolve_workspace(relative, self._path_ctx.workspace)
                        try:
                            content = await self._path_ctx.remote_reader.file_read(abs_path)
                            if len(content) > 50000:
                                content = content[:50000] + "\n... (truncated)"
                            return ToolResult(output=content)
                        except Exception as e:
                            return ToolResult(output=f"Remote read error: {e}", success=False)
                    else:
                        path = Path(resolve_workspace(relative, self._path_ctx.workspace))
                else:
                    return ToolResult(output="workspace:// paths require an active project context with a workspace.", success=False)
            else:
                return ToolResult(output=f"Unknown protocol: {scheme}", success=False)
        else:
            path = Path(path_str)
            if not path.is_absolute():
                path = self._cwd / path

        resolved = path.resolve()
        path = resolved
        try:
            content = path.read_text(errors="replace")
            if len(content) > 50000:
                content = content[:50000] + "\n... (truncated)"
            return ToolResult(output=content)
        except FileNotFoundError:
            return ToolResult(output=f"File not found: {path}", success=False)
        except Exception as e:
            return ToolResult(output=f"Error reading file: {e}", success=False)
