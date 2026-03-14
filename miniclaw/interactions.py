"""Interaction data types for agent-channel communication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class InteractionType(Enum):
    PERMISSION = "permission"        # Tool permission (allow/deny)
    ASK_USER = "ask_user"            # AskUserQuestion (questions + options)
    PLAN_APPROVAL = "plan_approval"  # ExitPlanMode (approve/reject plan)


@dataclass
class InteractionRequest:
    """A request from the agent that requires user interaction."""

    id: str                           # Unique ID for correlation
    type: InteractionType
    tool_name: str                    # e.g., "Bash", "AskUserQuestion", "ExitPlanMode"
    tool_input: dict[str, Any]        # Tool input (command, questions, plan, etc.)
    suggestions: list[Any] = field(default_factory=list)  # Permission suggestions from SDK
    _future: asyncio.Future | None = field(default=None, repr=False)

    def resolve(self, response: InteractionResponse) -> None:
        """Resolve this request with a response, unblocking the can_use_tool callback."""
        if self._future is not None and not self._future.done():
            self._future.get_loop().call_soon_threadsafe(self._future.set_result, response)


@dataclass
class InteractionResponse:
    """A response to an InteractionRequest."""

    id: str                                    # Matches InteractionRequest.id
    allow: bool                                # True = allow/approve, False = deny/reject
    message: str = ""                          # Denial reason, or user's text answer
    updated_input: dict[str, Any] | None = None  # Modified tool input (e.g., user's answers)
