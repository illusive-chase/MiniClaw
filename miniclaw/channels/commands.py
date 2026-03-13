"""Slash-command system for channel-level commands."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from miniclaw.ui import console

logger = logging.getLogger(__name__)


@dataclass
class CommandContext:
    """Runtime context passed to every command's execute()."""

    channel: Any  # CLIChannel reference
    logging_handles: Any  # LoggingHandles from ui.py (may be None)
    agent_commands: list[dict] = field(default_factory=list)  # [{name, description, usage}]
    session_manager: Any = None  # SessionManager (may be None)
    agent: Any = None  # Agent (may be None)


class Command(ABC):
    """Abstract base for a slash command."""

    @abstractmethod
    def name(self) -> str: ...

    def aliases(self) -> list[str]:
        return []

    @abstractmethod
    def description(self) -> str: ...

    def usage(self) -> str:
        return f"/{self.name()}"

    @abstractmethod
    async def execute(self, args: str, ctx: CommandContext) -> str | None:
        """Execute the command. Return a message to display, or None."""
        ...


class CommandRegistry:
    """Holds and resolves commands by name/alias, using longest-prefix match."""

    def __init__(self):
        self._commands: list[Command] = []
        self._lookup: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        self._commands.append(command)
        self._lookup[command.name()] = command
        for alias in command.aliases():
            self._lookup[alias] = command

    def resolve(self, line: str) -> tuple[Command, str] | None:
        """Find the command matching the longest prefix of *line*.

        Returns (command, remaining_args) or None.
        """
        best: tuple[Command, str] | None = None
        best_len = 0
        for key, cmd in self._lookup.items():
            if line == key or line.startswith(key + " "):
                if len(key) > best_len:
                    best_len = len(key)
                    args = line[len(key):].strip()
                    best = (cmd, args)
        return best

    def all_commands(self) -> list[Command]:
        return list(self._commands)


# ---------- Concrete commands ----------


class QuitCommand(Command):
    def name(self) -> str:
        return "quit"

    def aliases(self) -> list[str]:
        return ["exit", "q"]

    def description(self) -> str:
        return "Exit the CLI"

    async def execute(self, args: str, ctx: CommandContext) -> str | None:
        raise SystemExit(0)


class HelpCommand(Command):
    def name(self) -> str:
        return "help"

    def aliases(self) -> list[str]:
        return ["?"]

    def description(self) -> str:
        return "List available commands"

    async def execute(self, args: str, ctx: CommandContext) -> str | None:
        lines = ["[bold]Channel commands:[/]"]
        for cmd in ctx.channel._registry.all_commands():
            lines.append(f"  {cmd.usage():30s} {cmd.description()}")
        if ctx.agent_commands:
            lines.append("")
            lines.append("[bold]Agent commands:[/]")
            for entry in ctx.agent_commands:
                usage = entry.get("usage", f"/{entry['name']}")
                lines.append(f"  {usage:30s} {entry['description']}")
        return "\n".join(lines)


class OutputCommand(Command):
    def name(self) -> str:
        return "output"

    def description(self) -> str:
        return "Output settings (subcommands: markdown, show-logging)"

    def usage(self) -> str:
        return "/output <subcommand>"

    async def execute(self, args: str, ctx: CommandContext) -> str | None:
        return (
            "Subcommands:\n"
            "  /output markdown [on|off]          Toggle Markdown rendering\n"
            "  /output show-logging [level|off]    Set console log level"
        )


class OutputMarkdownCommand(Command):
    def name(self) -> str:
        return "output markdown"

    def description(self) -> str:
        return "Toggle Markdown panel rendering (on/off)"

    def usage(self) -> str:
        return "/output markdown [on|off]"

    async def execute(self, args: str, ctx: CommandContext) -> str | None:
        if args.lower() in ("off", "false", "0"):
            ctx.channel._render_markdown = False
            return "Markdown rendering disabled."
        elif args.lower() in ("on", "true", "1"):
            ctx.channel._render_markdown = True
            return "Markdown rendering enabled."
        else:
            current = "on" if ctx.channel._render_markdown else "off"
            return f"Markdown rendering is currently {current}. Use /output markdown on|off."


class OutputShowLoggingCommand(Command):
    _LEVEL_MAP: dict[str, int] = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "warn": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
        "off": logging.CRITICAL + 1,
    }

    def name(self) -> str:
        return "output show-logging"

    def description(self) -> str:
        return "Set console log level (debug/info/warning/error/off)"

    def usage(self) -> str:
        return "/output show-logging <level>"

    async def execute(self, args: str, ctx: CommandContext) -> str | None:
        if ctx.logging_handles is None:
            return "Logging handles not available."
        level = self._LEVEL_MAP.get(args.lower().strip())
        if level is None:
            valid = ", ".join(sorted(self._LEVEL_MAP.keys()))
            return f"Unknown level '{args}'. Valid: {valid}"
        ctx.logging_handles.console_handler.setLevel(level)
        label = args.lower().strip()
        return f"Console log level set to {label}."


class SessionsCommand(Command):
    def name(self) -> str:
        return "sessions"

    def description(self) -> str:
        return "List saved sessions"

    async def execute(self, args: str, ctx: CommandContext) -> str | None:
        sm = ctx.session_manager
        if sm is None:
            return "Session manager not available."
        sessions = sm.list_sessions()
        if not sessions:
            return "No saved sessions."
        current = sm.current
        lines = ["[bold]Saved sessions:[/]"]
        for s in sessions:
            marker = " *" if current and s.id == current.id else ""
            label = s.name or "(unnamed)"
            msg_count = len(s.messages)
            lines.append(f"  {s.id}  {label}  ({msg_count} msgs)  {s.updated_at}{marker}")
        return "\n".join(lines)


class RenameCommand(Command):
    def name(self) -> str:
        return "rename"

    def description(self) -> str:
        return "Rename the current session"

    def usage(self) -> str:
        return "/rename <name>"

    async def execute(self, args: str, ctx: CommandContext) -> str | None:
        sm = ctx.session_manager
        if sm is None:
            return "Session manager not available."
        if not args.strip():
            return "Usage: /rename <name>"
        try:
            sm.rename_current(args.strip())
        except ValueError as e:
            return str(e)
        return f"Session renamed to '{args.strip()}'."


class ResumeCommand(Command):
    def name(self) -> str:
        return "resume"

    def description(self) -> str:
        return "Resume a previous session"

    def usage(self) -> str:
        return "/resume <id_or_prefix>"

    async def execute(self, args: str, ctx: CommandContext) -> str | None:
        sm = ctx.session_manager
        agent = ctx.agent
        if sm is None or agent is None:
            return "Session manager not available."
        if not args.strip():
            return "Usage: /resume <id_or_prefix>"

        # Dump current session first if it has messages
        if sm.current is not None:
            sender = sm.current.sender_id
            messages = agent.get_conversation(sender)
            sm.dump_current(messages)

        # Resolve and load the target session
        try:
            target = sm.resolve_prefix(args.strip())
        except ValueError as e:
            return str(e)

        loaded = sm.load_session(target.id)
        from miniclaw.session import SessionManager
        restored = SessionManager.deserialize_messages(loaded.messages)
        agent.set_conversation(loaded.sender_id, restored)

        # Set loaded session as current
        sm._current = loaded
        msg_count = len(restored)
        label = loaded.name or loaded.id

        # Replay conversation history so it looks like a natural session
        from miniclaw.channels.base import SendMessage

        for msg in restored:
            if msg.role == "user" and msg.content:
                console.print(f"\n[bold green]You:[/] {msg.content}")
            elif msg.role == "assistant" and msg.content:
                await ctx.channel.send(SendMessage(text=msg.content))

        return f"\nResumed session '{label}' ({msg_count} messages restored)."


# ---------- Factory ----------


def create_default_registry() -> CommandRegistry:
    """Build a CommandRegistry with all built-in channel commands."""
    registry = CommandRegistry()
    registry.register(QuitCommand())
    registry.register(HelpCommand())
    registry.register(OutputCommand())
    registry.register(OutputMarkdownCommand())
    registry.register(OutputShowLoggingCommand())
    registry.register(SessionsCommand())
    registry.register(RenameCommand())
    registry.register(ResumeCommand())
    return registry
