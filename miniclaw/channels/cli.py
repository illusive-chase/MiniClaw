"""CLI channel for local testing via stdin/stdout."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.theme import Theme

from .base import Channel, SendMessage
from .commands import CommandContext, create_default_registry

if TYPE_CHECKING:
    from miniclaw.gateway import Gateway

logger = logging.getLogger(__name__)


class CLIChannel(Channel):
    """Interactive command-line channel for testing."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        self._render_markdown = config.get("render_markdown", True)
        self._registry = create_default_registry()
        self._gw: Gateway | None = None
        self._session_id: str | None = None

        self._console = Console(theme=Theme({
            "markdown.code": "bold magenta on white",
            "markdown.code_block": "magenta on white",
            "markdown.hr": "gray70",
        }))

        console_level = config.get("console_level", logging.INFO)
        self._console_handler = RichHandler(
            console=self._console,
            rich_tracebacks=True,
            tracebacks_show_locals=False,
        )
        self._console_handler.setFormatter(
            logging.Formatter("%(message)s", datefmt="[%X]")
        )
        self._console_handler.setLevel(console_level)

    def log_handler(self) -> logging.Handler | None:
        return self._console_handler

    def replay_message(self, role: str, text: str) -> None:
        """Replay a historical message during session resume."""
        if role == "user":
            self._console.print(f"\n[bold green]You:[/] {text}")
        elif role == "assistant":
            if self._render_markdown:
                self._console.print(Panel(Markdown(text), title="Assistant", border_style="blue"))
            else:
                self._console.print(f"\nAssistant: {text}")

    def command_descriptions(self) -> list[dict]:
        """Return all command descriptions for /help."""
        return [
            {"name": cmd.name(), "description": cmd.description(), "usage": cmd.usage()}
            for cmd in self._registry.all_commands()
        ]

    async def start(self, gateway: Gateway) -> None:
        """Called by Gateway. Allocate session and begin listen loop."""
        self._gw = gateway
        self._session_id = gateway.allocate_session("cli_user")

        self._console.print(Panel("MiniClaw CLI", subtitle="type /help for commands", style="bold cyan"))
        loop = asyncio.get_event_loop()
        while True:
            try:
                self._console.print("\n[bold green]You:[/] ", end="")
                line = await loop.run_in_executor(None, sys.stdin.readline)
                line = line.strip()
                if not line:
                    continue

                # Backward compat: bare quit/exit
                if line.lower() in ("quit", "exit"):
                    self._console.print("[dim]Goodbye![/dim]")
                    break

                # Slash commands
                if line.startswith("/"):
                    ctx = CommandContext(
                        channel=self,
                        gateway=self._gw,
                        session_id=self._session_id,
                    )
                    resolved = self._registry.resolve(line[1:])
                    if resolved:
                        cmd, args = resolved
                        try:
                            result = await cmd.execute(args, ctx)
                            # Update session_id in case a command changed it (e.g. /resume)
                            self._session_id = ctx.channel._session_id
                            if result:
                                self._console.print(result)
                        except SystemExit:
                            self._console.print("[dim]Goodbye![/dim]")
                            break
                    else:
                        self._console.print(f"Unknown command: {line}. Type /help for available commands.")
                    continue

                # Regular message — use streaming if gateway supports it
                if hasattr(self._gw, "process_message_stream"):
                    stream = self._gw.process_message_stream(self._session_id, line)
                    await self.send_stream(stream)
                else:
                    with self._console.status("[bold cyan]Thinking...", spinner="dots"):
                        reply = await self._gw.process_message(self._session_id, line)
                    await self.send(SendMessage(text=reply))
            except (EOFError, KeyboardInterrupt):
                self._console.print("\n[dim]Goodbye![/dim]")
                break

    async def send(self, message: SendMessage) -> None:
        if self._render_markdown:
            self._console.print(Panel(Markdown(message.text), title="Assistant", border_style="blue"))
        else:
            self._console.print(f"\nAssistant: {message.text}")

    async def send_stream(self, stream: AsyncIterator[str]) -> None:
        """Stream response chunks directly to the console."""
        self._console.print("\n[blue]Assistant:[/blue] ", end="")
        async for chunk in stream:
            self._console.print(chunk, end="", highlight=False)
        self._console.print()  # final newline
