"""File edit tool — string-replace based editing."""

from pathlib import Path

from miniclaw.plugctx.vpath import detect_protocol

from .base import Tool, ToolResult


class FileEditTool(Tool):
    def __init__(self, cwd: str = "."):
        self._cwd = Path(cwd)

    def name(self) -> str:
        return "file_edit"

    def description(self) -> str:
        return "Edit a file by replacing an exact string match with new content. The old_string must appear exactly once in the file."

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact string to find and replace (must be unique in the file)",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement string",
                },
            },
            "required": ["path", "old_string", "new_string"],
        }

    async def execute(self, args: dict) -> ToolResult:
        path_str = args.get("path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        if not path_str:
            return ToolResult(output="No path provided", success=False)
        if not old_string:
            return ToolResult(output="No old_string provided", success=False)

        # Block virtual protocols
        protocol = detect_protocol(path_str)
        if protocol is not None:
            return ToolResult(
                output=f"Cannot edit {protocol[0]} paths. Write operations are restricted to the planspace (current directory).",
                success=False,
            )

        path = Path(path_str)
        if not path.is_absolute():
            path = self._cwd / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self._cwd.resolve()):
            return ToolResult(output=f"Path {path_str} is outside the working directory", success=False)
        path = resolved
        try:
            content = path.read_text()
            count = content.count(old_string)
            if count == 0:
                return ToolResult(output="old_string not found in file", success=False)
            if count > 1:
                return ToolResult(
                    output=f"old_string found {count} times — must be unique. Provide more context.",
                    success=False,
                )
            new_content = content.replace(old_string, new_string, 1)
            path.write_text(new_content)
            return ToolResult(output=f"Edited {path}")
        except FileNotFoundError:
            return ToolResult(output=f"File not found: {path}", success=False)
        except Exception as e:
            return ToolResult(output=f"Error editing file: {e}", success=False)
