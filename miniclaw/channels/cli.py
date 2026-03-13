"""CLI channel for local testing via stdin/stdout."""

import asyncio
import sys
from typing import Awaitable, Callable

from rich.markdown import Markdown
from rich.panel import Panel

from miniclaw.ui import LoggingHandles, console

from .base import Channel, ChannelMessage, SendMessage
from .commands import CommandContext, create_default_registry


class CLIChannel(Channel):
    """Interactive command-line channel for testing."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        self._render_markdown = config.get("render_markdown", True)
        self._registry = create_default_registry()
        self._logging_handles: LoggingHandles | None = None
        self._agent_commands: list[dict] = []
        self._session_manager = None
        self._agent = None

    def bind_logging_handles(self, handles: LoggingHandles) -> None:
        """Attach logging handles so /output show-logging can adjust levels."""
        self._logging_handles = handles

    def register_agent_commands(self, descriptions: list[dict]) -> None:
        """Register agent-level command descriptions for /help display."""
        self._agent_commands = descriptions

    def bind_session_manager(self, sm) -> None:
        """Attach a SessionManager for session commands."""
        self._session_manager = sm

    def bind_agent(self, agent) -> None:
        """Attach the Agent for session commands."""
        self._agent = agent

    async def listen(self, callback: Callable[[ChannelMessage], Awaitable[None]]) -> None:
        console.print(Panel("MiniClaw CLI", subtitle="type /help for commands", style="bold cyan"))
        loop = asyncio.get_event_loop()
        while True:
            try:
                console.print("\n[bold green]You:[/] ", end="")
                line = await loop.run_in_executor(None, sys.stdin.readline)
                line = line.strip()
                if not line:
                    continue

                # Backward compat: bare quit/exit
                if line.lower() in ("quit", "exit"):
                    console.print("[dim]Goodbye![/dim]")
                    break

                # Slash commands
                if line.startswith("/"):
                    ctx = CommandContext(
                        channel=self,
                        logging_handles=self._logging_handles,
                        agent_commands=self._agent_commands,
                        session_manager=self._session_manager,
                        agent=self._agent,
                    )
                    resolved = self._registry.resolve(line[1:])
                    if resolved:
                        cmd, args = resolved
                        try:
                            result = await cmd.execute(args, ctx)
                            if result:
                                console.print(result)
                        except SystemExit:
                            console.print("[dim]Goodbye![/dim]")
                            break
                    else:
                        # Not a channel command — forward as agent command
                        parts = line[1:].split(None, 1)
                        command_name = parts[0] if parts else ""
                        command_args = parts[1] if len(parts) > 1 else ""
                        msg = ChannelMessage(
                            text=line,
                            sender_id="cli_user",
                            channel_id="cli",
                            command=command_name,
                            command_args=command_args,
                        )
                        await callback(msg)
                    continue

                # Regular message — show spinner
                msg = ChannelMessage(
                    text=line,
                    sender_id="cli_user",
                    channel_id="cli",
                )
                with console.status("[bold cyan]Thinking...", spinner="dots"):
                    await callback(msg)
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye![/dim]")
                break

    async def send(self, message: SendMessage) -> None:
        if self._render_markdown:
            console.print(Panel(Markdown(message.text), title="Assistant", border_style="blue"))
        else:
            console.print(f"\nAssistant: {message.text}")
