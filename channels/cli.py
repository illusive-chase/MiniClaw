"""CLI channel for local testing via stdin/stdout."""

import asyncio
import sys
from typing import Callable, Awaitable

from channels.base import Channel, ChannelMessage, SendMessage


class CLIChannel(Channel):
    """Interactive command-line channel for testing."""

    async def listen(self, callback: Callable[[ChannelMessage], Awaitable[None]]) -> None:
        print("Mini-Agent CLI (type 'quit' to exit)")
        print("-" * 40)
        loop = asyncio.get_event_loop()
        while True:
            try:
                print("\nYou: ", end="", flush=True)
                line = await loop.run_in_executor(None, sys.stdin.readline)
                line = line.strip()
                if not line:
                    continue
                if line.lower() in ("quit", "exit"):
                    print("Goodbye!")
                    break
                msg = ChannelMessage(
                    text=line,
                    sender_id="cli_user",
                    channel_id="cli",
                )
                await callback(msg)
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

    async def send(self, message: SendMessage) -> None:
        print(f"\nAssistant: {message.text}")
