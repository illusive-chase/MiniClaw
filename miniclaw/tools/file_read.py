"""File read tool."""

from pathlib import Path

from .base import Tool, ToolResult


class FileReadTool(Tool):
    def __init__(self, cwd: str = "."):
        self._cwd = Path(cwd)

    def name(self) -> str:
        return "file_read"

    def description(self) -> str:
        return "Read the contents of a file. Returns the file content as text."

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (relative to workspace or absolute)",
                },
            },
            "required": ["path"],
        }

    async def execute(self, args: dict) -> ToolResult:
        path_str = args.get("path", "")
        if not path_str:
            return ToolResult(output="No path provided", success=False)
        path = Path(path_str)
        if not path.is_absolute():
            path = self._cwd / path
        try:
            content = path.read_text(errors="replace")
            if len(content) > 50000:
                content = content[:50000] + "\n... (truncated)"
            return ToolResult(output=content)
        except FileNotFoundError:
            return ToolResult(output=f"File not found: {path}", success=False)
        except Exception as e:
            return ToolResult(output=f"Error reading file: {e}", success=False)
