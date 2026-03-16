"""Git operations tool for self-backup and version control."""

import asyncio

from .base import Tool, ToolResult


class GitTool(Tool):
    def __init__(self, cwd: str = "."):
        self._cwd = cwd

    def name(self) -> str:
        return "git"

    def description(self) -> str:
        return (
            "Run git commands for version control. Supports: status, add, commit, log, diff, stash. "
            "Use this to create backups before modifying files and to track changes."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "subcommand": {
                    "type": "string",
                    "description": "Git subcommand: status, add, commit, log, diff, stash",
                    "enum": ["status", "add", "commit", "log", "diff", "stash"],
                },
                "args": {
                    "type": "string",
                    "description": "Additional arguments for the git subcommand",
                    "default": "",
                },
            },
            "required": ["subcommand"],
        }

    async def execute(self, args: dict) -> ToolResult:
        subcommand = args.get("subcommand", "")
        extra_args = args.get("args", "")
        if not subcommand:
            return ToolResult(output="No subcommand provided", success=False)

        allowed = {"status", "add", "commit", "log", "diff", "stash"}
        if subcommand not in allowed:
            return ToolResult(output=f"Subcommand not allowed: {subcommand}", success=False)

        cmd = f"git {subcommand}"
        if extra_args:
            cmd += f" {extra_args}"

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = stdout.decode(errors="replace")
            if stderr:
                output += "\n" + stderr.decode(errors="replace")
            output = output.strip()
            return ToolResult(output=output or "(no output)", success=proc.returncode == 0)
        except asyncio.TimeoutError:
            return ToolResult(output="Git command timed out", success=False)
        except Exception as e:
            return ToolResult(output=f"Error: {e}", success=False)
