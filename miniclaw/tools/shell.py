"""Shell command execution tool."""

import asyncio

from .base import Tool, ToolResult


class ShellTool(Tool):
    def __init__(self, workspace_dir: str = ".workspace"):
        self._workspace_dir = workspace_dir

    def name(self) -> str:
        return "shell"

    def description(self) -> str:
        return "Execute a shell command and return its output. Use for system commands, listing files, running scripts, etc."

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Optional timeout in seconds for the command execution. Default is 180 seconds.",
                },
            },
            "required": ["command"],
        }

    async def execute(self, args: dict) -> ToolResult:
        command = args.get("command", "")
        timeout = args.get("timeout", 180)
        if not command:
            return ToolResult(output="No command provided", success=False)
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._workspace_dir,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode(errors="replace")
            if stderr:
                output += "\n" + stderr.decode(errors="replace")
            output = output.strip()
            if len(output) > 10000:
                output = output[:10000] + "\n... (truncated)"
            return ToolResult(output=output or "(no output)", success=proc.returncode == 0)
        except asyncio.TimeoutError:
            return ToolResult(output=f"Command timed out after {timeout} seconds", success=False)
        except Exception as e:
            return ToolResult(output=f"Error: {e}", success=False)
