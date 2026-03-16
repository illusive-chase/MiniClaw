"""Wire protocol serialization for Remote CCAgent WebSocket transport.

JSON messages over WebSocket, multiplexed by session_id.

Client -> Server: spawn, interaction_response, send_message, cancel, ping
Server -> Client: spawn_ack, text_delta, activity, interaction_request,
                  interrupted, usage, turn_complete, session_error, pong
"""

from __future__ import annotations

from typing import Any

from miniclaw.activity import ActivityEvent, ActivityKind, ActivityStatus
from miniclaw.interactions import (
    InteractionRequest,
    InteractionResponse,
    InteractionType,
)
from miniclaw.types import AgentEvent, InterruptedEvent, TextDelta, UsageEvent
from miniclaw.usage import UsageStats

# ---------------------------------------------------------------------------
# Server -> Client: serialize AgentEvent for the wire
# ---------------------------------------------------------------------------

def serialize_event(session_id: str, event: AgentEvent) -> dict[str, Any] | None:
    """Convert an AgentEvent to a JSON-serializable dict for the wire.

    Returns None for event types that should not be serialized (HistoryUpdate,
    SessionControl — consumed by the remote Session internally).
    """
    if isinstance(event, TextDelta):
        return {
            "type": "text_delta",
            "session_id": session_id,
            "text": event.text,
        }

    if isinstance(event, ActivityEvent):
        return {
            "type": "activity",
            "session_id": session_id,
            "kind": event.kind.value,
            "status": event.status.value,
            "id": event.id,
            "name": event.name,
            "summary": event.summary,
        }

    if isinstance(event, InteractionRequest):
        return {
            "type": "interaction_request",
            "session_id": session_id,
            "interaction_id": event.id,
            "interaction_type": event.type.value,
            "tool_name": event.tool_name,
            "tool_input": event.tool_input,
            "suggestions": event.suggestions,
        }

    if isinstance(event, InterruptedEvent):
        return {
            "type": "interrupted",
            "session_id": session_id,
        }

    if isinstance(event, UsageEvent):
        u = event.usage
        return {
            "type": "usage",
            "session_id": session_id,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens,
            "cache_creation_tokens": u.cache_creation_tokens,
            "total_cost_usd": u.total_cost_usd,
            "total_duration_ms": u.total_duration_ms,
        }

    # HistoryUpdate and SessionControl are not serialized
    return None


# ---------------------------------------------------------------------------
# Client-side: deserialize wire message -> AgentEvent
# ---------------------------------------------------------------------------

def deserialize_event(msg: dict[str, Any]) -> AgentEvent | None:
    """Convert a wire message dict back into an AgentEvent.

    Returns None for message types that are not AgentEvents (spawn_ack,
    turn_complete, session_error, pong).
    """
    msg_type = msg.get("type")

    if msg_type == "text_delta":
        return TextDelta(text=msg["text"])

    if msg_type == "activity":
        return ActivityEvent(
            kind=ActivityKind(msg["kind"]),
            status=ActivityStatus(msg["status"]),
            id=msg["id"],
            name=msg["name"],
            summary=msg.get("summary", ""),
        )

    if msg_type == "interaction_request":
        return InteractionRequest(
            id=msg["interaction_id"],
            type=InteractionType(msg["interaction_type"]),
            tool_name=msg["tool_name"],
            tool_input=msg.get("tool_input", {}),
            suggestions=msg.get("suggestions", []),
            _future=None,  # local driver manages its own futures
        )

    if msg_type == "interrupted":
        return InterruptedEvent()

    if msg_type == "usage":
        return UsageEvent(
            usage=UsageStats(
                input_tokens=msg.get("input_tokens", 0),
                output_tokens=msg.get("output_tokens", 0),
                cache_read_tokens=msg.get("cache_read_tokens", 0),
                cache_creation_tokens=msg.get("cache_creation_tokens", 0),
                total_cost_usd=msg.get("total_cost_usd", 0.0),
                total_duration_ms=msg.get("total_duration_ms", 0),
            )
        )

    return None


# ---------------------------------------------------------------------------
# InteractionResponse serialization (Client -> Server)
# ---------------------------------------------------------------------------

def serialize_interaction_response(
    session_id: str,
    interaction_id: str,
    response: InteractionResponse,
) -> dict[str, Any]:
    """Serialize an InteractionResponse for the wire."""
    return {
        "type": "interaction_response",
        "session_id": session_id,
        "interaction_id": interaction_id,
        "allow": response.allow,
        "message": response.message,
        "updated_input": response.updated_input,
        "permission_mode": response.permission_mode,
        "clear_context": response.clear_context,
    }


def deserialize_interaction_response(msg: dict[str, Any]) -> InteractionResponse:
    """Deserialize a wire message into an InteractionResponse."""
    return InteractionResponse(
        id=msg["interaction_id"],
        allow=msg.get("allow", False),
        message=msg.get("message", ""),
        updated_input=msg.get("updated_input"),
        permission_mode=msg.get("permission_mode"),
        clear_context=msg.get("clear_context", False),
    )


# ---------------------------------------------------------------------------
# Spawn request serialization (Client -> Server)
# ---------------------------------------------------------------------------

def serialize_spawn(
    session_id: str,
    agent_type: str,
    task: str,
    agent_config: dict[str, Any] | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Serialize a spawn request for the wire."""
    msg: dict[str, Any] = {
        "type": "spawn",
        "session_id": session_id,
        "agent_type": agent_type,
        "task": task,
    }
    if agent_config is not None:
        msg["agent_config"] = agent_config
    if cwd is not None:
        msg["cwd"] = cwd
    return msg


# ---------------------------------------------------------------------------
# Simple control messages
# ---------------------------------------------------------------------------

def serialize_send_message(session_id: str, text: str) -> dict[str, Any]:
    return {"type": "send_message", "session_id": session_id, "text": text}


def serialize_cancel(session_id: str) -> dict[str, Any]:
    return {"type": "cancel", "session_id": session_id}


def serialize_ping() -> dict[str, Any]:
    return {"type": "ping"}
