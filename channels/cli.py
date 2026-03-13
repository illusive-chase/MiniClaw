"""CLI channel for local testing via stdin/stdout."""

import asyncio
import sys
from typing import Awaitable, Callable

from rich.markdown import Markdown
from rich.panel import Panel

from channels.base import Channel, ChannelMessage, SendMessage
from ui import console


class CLIChannel(Channel):
    """Interactive command-line channel for testing."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        self._render_markdown = config.get("render_markdown", True)

    async def listen(self, callback: Callable[[ChannelMessage], Awaitable[None]]) -> None:
        console.print(Panel("MiniClaw CLI", subtitle="type 'quit' to exit", style="bold cyan"))
        loop = asyncio.get_event_loop()
        while True:
            try:
                console.print("\n[bold green]You:[/] ", end="")
                line = await loop.run_in_executor(None, sys.stdin.readline)
                line = line.strip()
                if not line:
                    continue
                if line.lower() in ("quit", "exit"):
                    console.print("[dim]Goodbye![/dim]")
                    break
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
