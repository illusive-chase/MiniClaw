"""Cooperative cancellation token with signal queue for mid-turn notifications."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum


class CancelledError(Exception):
    """Raised by CancellationToken.check() when cancelled."""


class SignalType(Enum):
    CANCEL = "cancel"
    NOTIFICATION = "notification"
    INJECT = "inject"  # future: user "btw" mid-turn


@dataclass
class Signal:
    type: SignalType
    payload: str = ""
    source: str = ""  # "user" | "sub_agent" | "system"
    metadata: dict | None = None


class SignalToken:
    """Cooperative cancellation + signal queue passed from Session to Agent.

    Agents call ``check()`` at defined checkpoints:
      1. Before each provider.chat() call
      2. Before each tool.execute() call
      3. (Optional) Between stream chunks

    Signals allow sub-agent notifications to be delivered mid-turn
    via ``send()`` / ``drain()``.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._signals: deque[Signal] = deque()

    # --- Existing cancellation API (unchanged) ---

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

    # --- New signal API ---

    def send(self, signal: Signal) -> None:
        """Enqueue a signal for the agent to pick up at the next checkpoint."""
        self._signals.append(signal)

    def drain(self, types: set[SignalType] | None = None) -> list[Signal]:
        """Remove and return queued signals, optionally filtered by type."""
        if types is None:
            result = list(self._signals)
            self._signals.clear()
            return result
        matched: list[Signal] = []
        kept: deque[Signal] = deque()
        for sig in self._signals:
            (matched if sig.type in types else kept).append(sig)
        self._signals = kept
        return matched

    @property
    def has_pending(self) -> bool:
        return bool(self._signals)


# Backward-compat alias
CancellationToken = SignalToken
