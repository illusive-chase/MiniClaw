"""File write tool."""

from pathlib import Path

from .base import Tool, ToolResult


class FileWriteTool(Tool):
    def __init__(self, workspace_dir: str = "."):
        self._workspace_dir = Path(workspace_dir)

    def name(self) -> str:
        return "file_write"

    def description(self) -> str:
        return "Write content to a file. Creates the file and parent directories if they don't exist. Overwrites existing content."

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write (relative to workspace or absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, args: dict) -> ToolResult:
        path_str = args.get("path", "")
        content = args.get("content", "")
        if not path_str:
            return ToolResult(output="No path provided", success=False)
        path = Path(path_str)
        if not path.is_absolute():
            path = self._workspace_dir / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            return ToolResult(output=f"Written {len(content)} bytes to {path}")
        except Exception as e:
            return ToolResult(output=f"Error writing file: {e}", success=False)
