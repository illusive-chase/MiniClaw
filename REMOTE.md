# Plan: Remote CCAgent Execution via WebSocket

## Context

The main agent runs locally but needs to work on remote projects via Claude Code. Today, CCAgent wraps `claude_agent_sdk.ClaudeSDKClient` which connects to a **local** Claude Code CLI subprocess — there is no remote transport. Sub-agent spawning (`RuntimeContext.spawn()` → `SubAgentDriver`) is entirely in-process using `asyncio.Queue` and `Future`.

**Goal**: Enable `launch_agent(type="ccagent", task="...", remote="server1")` so the local main agent spawns a CCAgent session running on a remote server, with full InteractionRequest forwarding back to the local parent.

**User choices**: WebSocket transport, forward permissions to local, `minicode --serve` flag, full round-trip implementation.

---

## WebSocket Protocol

JSON messages over WebSocket, multiplexed by `session_id`.

### Client → Server (Local → Remote)

| type | Key fields |
|------|-----------|
| `spawn` | `session_id`, `agent_type`, `task`, `agent_config?` |
| `interaction_response` | `session_id`, `interaction_id`, `allow`, `message?`, `updated_input?`, `permission_mode?`, `clear_context?` |
| `send_message` | `session_id`, `text` |
| `cancel` | `session_id` |
| `ping` | — |

### Server → Client (Remote → Local)

| type | Key fields |
|------|-----------|
| `spawn_ack` | `session_id`, `ok`, `error?` |
| `text_delta` | `session_id`, `text` |
| `activity` | `session_id`, `kind`, `status`, `id`, `name`, `summary` |
| `interaction_request` | `session_id`, `interaction_id`, `interaction_type`, `tool_name`, `tool_input`, `suggestions` |
| `interrupted` | `session_id` |
| `usage` | `session_id`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `total_cost_usd`, `total_duration_ms` |
| `turn_complete` | `session_id`, `result` |
| `session_error` | `session_id`, `error` |
| `pong` | — |

`HistoryUpdate` and `SessionControl` are **not** serialized — they are consumed by the remote `Session` internally, same as today.

---

## New Files

### 1. `miniclaw/remote/__init__.py`
Exports: `RemoteSubAgentDriver`, `RemoteDaemon`, `serve_main`.

### 2. `miniclaw/remote/protocol.py` — Wire protocol serialization
- `serialize_event(session_id, event: AgentEvent) -> dict` — Converts TextDelta/ActivityEvent/InteractionRequest/InterruptedEvent/UsageEvent to JSON-serializable dict.
- `deserialize_event(msg: dict) -> AgentEvent` — Inverse. For `interaction_request`, creates an `InteractionRequest` with `_future=None` (the local driver manages its own futures).
- `serialize_interaction_response(session_id, response: InteractionResponse) -> dict`
- `deserialize_interaction_response(msg: dict) -> InteractionResponse`
- `serialize_spawn(session_id, agent_type, task, agent_config?) -> dict`

### 3. `miniclaw/remote/remote_driver.py` — Local-side client (replaces `SubAgentDriver` for remote targets)

```
class RemoteSubAgentDriver(Channel):
    __init__(session_id, parent_session, ws_url, agent_type, task, agent_config?)
    start()           → spawns _run() as background task
    _run()            → connect WS, send spawn, receive loop
    send_stream(...)  → NotImplementedError (events come over WS, not local stream)
    send(text)        → sends send_message over WS
    resolve_interaction(interaction_id, action, reason?, answers?) → resolves local proxy future, which triggers WS send
    cancel()          → sends cancel over WS
    _notify_parent()  → identical to SubAgentDriver._notify_parent()
    status, result, pending_interaction_ids()
```

**InteractionRequest bridging**: When a remote `interaction_request` arrives:
1. Create a local proxy `InteractionRequest` with a new `asyncio.Future`.
2. Store mapping `interaction_id → future`.
3. Call `_notify_parent("permission_required", ...)` to alert the parent agent.
4. Spawn a waiter task: `await future` → serialize response → send `interaction_response` over WS.

When `resolve_interaction()` is called (by `RuntimeContext.resolve()`), it sets the future → waiter sends over WS → remote daemon resolves the real CCAgent future.

**Reconnection**: Exponential backoff (2s→60s), max 5 retries. On reconnect, re-send `spawn` with same `session_id` (daemon re-attaches). Pending interactions resolved with `allow=False` on disconnect.

### 4. `miniclaw/remote/daemon.py` — Remote server

```
class RemoteDaemon:
    __init__(config, host="0.0.0.0", port=9100)
    run()                    → starts aiohttp web app, serves WS at /ws
    _handle_connection(req)  → per-connection WS handler

class DaemonSessionHandler(Channel):
    __init__(session_id, ws)
    send_stream(stream)      → serialize events, send over WS
    send(text)               → no-op
    resolve_interaction(msg)  → resolve real InteractionRequest future
    _consume()               → background task: async for (stream, source) in session.run()
```

**Session lifecycle**: Sessions survive client disconnect for a grace period (default 5 min). Re-attachable via `spawn` with existing `session_id`. Max sessions configurable (default 10).

### 5. `miniclaw/remote/serve.py` — Entry point for `minicode --serve`

```python
def serve_main(config, host, port):
    daemon = RemoteDaemon(config, host, port)
    asyncio.run(daemon.run())
```

Reuses `load_config()`, `setup_file_logging()`, same agent factory registration as `cc_main.py`.

---

## Modifications to Existing Files

### 6. `miniclaw/cc_main.py`
- Add `argparse`: `--serve`, `--host` (default `0.0.0.0`), `--port` (default `9100`).
- If `--serve`: call `serve_main()` and return early.
- Pass `remotes_config` to Runtime.

### 7. `miniclaw/runtime_context.py`
- Add `remote: str | None = None` parameter to `spawn()`.
- Add `_spawn_remote()` method: creates `RemoteSubAgentDriver` instead of `SubAgentDriver`.
- Add `_resolve_remote_url(remote)`: resolves config key or passthrough `ws://` URL.

### 8. `miniclaw/runtime.py`
- Add `remotes_config: dict[str, str] | None = None` to `Runtime.__init__()`, store as `self._remotes_config`.

### 9. `miniclaw/config.py`
- Add `"remotes": {}` to `DEFAULT_CONFIG`.

### 10. `miniclaw/tools/session_tools.py`
- Add `"remote"` parameter to `LaunchAgentTool.parameters_schema()`.
- Pass `remote=args.get("remote")` to `self._ctx.spawn()`.

### 11. `pyproject.toml`
- Add `"aiohttp>=3.9"` to dependencies.

---

## Implementation Order

1. `miniclaw/remote/protocol.py` — pure serialization, no deps on other new code
2. `miniclaw/remote/daemon.py` + `miniclaw/remote/serve.py` — server side
3. `miniclaw/cc_main.py` — add `--serve` flag
4. `miniclaw/remote/remote_driver.py` — local-side client (most complex: interaction bridging + reconnection)
5. `miniclaw/runtime_context.py` — add `remote` param to `spawn()`
6. `miniclaw/config.py`, `miniclaw/runtime.py` — config plumbing
7. `miniclaw/tools/session_tools.py` — expose `remote` param in tool schema
8. `pyproject.toml` — add aiohttp dep
9. `miniclaw/remote/__init__.py` — package exports

---

## Verification

1. **Daemon startup**: `minicode --serve --port 9100` → logs "listening on ws://0.0.0.0:9100/ws", stays alive, Ctrl+C shuts down cleanly.
2. **Basic remote spawn**: Configure `remotes: {local_test: "ws://localhost:9100/ws"}`. From local minicode, ask agent to `launch_agent(type="ccagent", task="read config.yaml", remote="local_test")`. Verify daemon creates session, streams result back, parent gets turn_complete notification.
3. **InteractionRequest round-trip**: Remote ccagent triggers a permission request (e.g., Write tool). Verify it's serialized → sent to local → parent notified → parent resolves via `reply_agent` → response sent to remote → CCAgent unblocks.
4. **Cancel**: Launch remote task, cancel via `cancel_agent`. Verify remote session interrupted.
5. **Follow-up message**: `message_agent(session_id, "also check X")`. Verify remote session processes it.
6. **Disconnect/reconnect**: Kill daemon during task, restart, verify local driver reconnects (or fails gracefully).