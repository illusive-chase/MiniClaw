"""Listener ABC — long-running input source that routes messages to sessions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from miniclaw.runtime import Runtime


class Listener(ABC):
    """Long-running input source that feeds messages into sessions.

    Listeners are supervised by Runtime with exponential backoff on failure.
    Each listener type handles its own transport (stdin, HTTP polling, pipe queue).
    """

    @abstractmethod
    async def run(self, runtime: Runtime) -> None:
        """Main loop. Called by Runtime. Must be long-running.

        Supervised: if this raises, Runtime restarts with backoff.
        """
        ...

    async def shutdown(self) -> None:
        """Graceful shutdown. Cancel in-flight work, drain queues."""
        pass
