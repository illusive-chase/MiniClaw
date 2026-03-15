"""Cooperative cancellation token for interrupting agent processing."""

from __future__ import annotations

import asyncio


class CancelledError(Exception):
    """Raised by CancellationToken.check() when cancelled."""


class CancellationToken:
    """Cooperative cancellation passed from Session to Agent.

    Agents call ``check()`` at defined checkpoints:
      1. Before each provider.chat() call
      2. Before each tool.execute() call
      3. (Optional) Between stream chunks
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        """Signal cancellation."""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise CancelledError if cancelled. Call at checkpoints in agent loop."""
        if self._event.is_set():
            raise CancelledError("Processing interrupted by user")
