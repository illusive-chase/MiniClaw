# Remote CCAgent Execution via WebSocket

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
Exports: `RemoteSubAgentDriver`, `RemoteDaemon`, `serve_main`, `SSHTunnel`, `TunnelManager`, `TunnelError`.

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
    __init__(config, host="127.0.0.1", port=9100)
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

### 6. `miniclaw/remote/tunnel.py` — SSH tunnel manager

```
class TunnelError(Exception):
    Raised when an SSH tunnel cannot be established.

class SSHTunnel:
    __init__(ssh_host, remote_port=9100, local_port=0, ssh_user=None, ssh_port=22, ssh_key=None)
    start() -> int         → launch SSH subprocess, return actual local port
    close()                → terminate SSH process
    is_alive -> bool       → property: process still running
    local_port -> int      → property
    ws_url -> str          → property: "ws://127.0.0.1:<port>/ws"
    _find_free_port()      → static: socket.bind(('127.0.0.1', 0))

class TunnelManager:
    get_or_create(key, config) -> SSHTunnel   → reuses alive tunnels, replaces dead ones
    close(key)                                → close and remove one tunnel
    close_all()                               → close all managed tunnels
```

**SSH command**: `ssh -N -L <local>:127.0.0.1:<remote> [user@]host` with options `BatchMode=yes`, `StrictHostKeyChecking=accept-new`, `ExitOnForwardFailure=yes`, `ServerAliveInterval=30`, `ServerAliveCountMax=3`.

**Port auto-assignment**: When `local_port=0`, finds a free port via `socket.bind(('127.0.0.1', 0))`.

**Startup check**: Waits 5s for the process; if it exits within that window, reads stderr and raises `TunnelError`.

**Tunnel sharing**: `TunnelManager` keys tunnels by remote config name. Multiple sessions to the same remote share one SSH connection.

**Config format** (under `remotes` in config.yaml):
```yaml
remotes:
  server1:
    ssh_host: "remote-host"       # required
    ssh_user: "deploy"            # optional
    ssh_port: 22                  # optional, default 22
    ssh_key: "~/.ssh/id_rsa"     # optional
    daemon_port: 9100             # optional, default 9100
    local_port: 0                 # optional, 0 = auto-assign
  # Backward compatible: string values still work (direct URL, no tunnel)
  local_test: "ws://localhost:9100/ws"
```

---

## Modifications to Existing Files

### 7. `miniclaw/cc_main.py`
- Add `argparse`: `--serve`, `--host` (default `127.0.0.1`), `--port` (default `9100`).
- If `--serve`: call `serve_main()` and return early.
- Pass `remotes_config` to Runtime.

### 8. `miniclaw/runtime_context.py`
- Add `remote: str | None = None` parameter to `spawn()`.
- Add `_spawn_remote()` method: creates `RemoteSubAgentDriver` instead of `SubAgentDriver`.
- `_resolve_remote_url(remote)` is **async**: resolves config key or passthrough `ws://` URL. If the config entry is a dict with `ssh_host`, calls `TunnelManager.get_or_create()` to establish an SSH tunnel and returns the tunnel's local `ws_url`.
- String config entries are returned directly (backward compatible).

### 9. `miniclaw/runtime.py`
- Add `remotes_config: dict[str, str] | None = None` to `Runtime.__init__()`, store as `self._remotes_config`.
- Add `self._tunnel_manager = TunnelManager()` in `__init__`.
- Add `tunnel_manager` property accessor.
- Add `await self._tunnel_manager.close_all()` in `_shutdown()`.

### 10. `miniclaw/remote/daemon.py`
- Default `host` changed from `"0.0.0.0"` to `"127.0.0.1"` (constructor + docstring).

### 11. `miniclaw/remote/serve.py`
- Default `host` changed from `"0.0.0.0"` to `"127.0.0.1"`.

### 12. `miniclaw/config.py`
- Add `"remotes": {}` to `DEFAULT_CONFIG`.

### 13. `miniclaw/tools/session_tools.py`
- Add `"remote"` parameter to `LaunchAgentTool.parameters_schema()`.
- Pass `remote=args.get("remote")` to `self._ctx.spawn()`.

### 14. `pyproject.toml`
- Add `"aiohttp>=3.9"` to dependencies.

---

## Implementation Order

1. `miniclaw/remote/protocol.py` — pure serialization, no deps on other new code
2. `miniclaw/remote/daemon.py` + `miniclaw/remote/serve.py` — server side
3. `miniclaw/cc_main.py` — add `--serve` flag
4. `miniclaw/remote/remote_driver.py` — local-side client (most complex: interaction bridging + reconnection)
5. `miniclaw/remote/tunnel.py` — SSH tunnel manager (SSHTunnel, TunnelManager, TunnelError)
6. `miniclaw/runtime_context.py` — add `remote` param to `spawn()`, async `_resolve_remote_url()` with tunnel support
7. `miniclaw/config.py`, `miniclaw/runtime.py` — config plumbing + TunnelManager lifecycle
8. `miniclaw/tools/session_tools.py` — expose `remote` param in tool schema
9. `pyproject.toml` — add aiohttp dep
10. `miniclaw/remote/__init__.py` — package exports

---

## Verification

1. **Daemon startup**: `minicode --serve` → logs "listening on ws://127.0.0.1:9100/ws", stays alive, Ctrl+C shuts down cleanly. Verify external connections refused (bound to localhost only).
2. **Basic remote spawn**: Configure `remotes: {local_test: "ws://localhost:9100/ws"}`. From local minicode, ask agent to `launch_agent(type="ccagent", task="read config.yaml", remote="local_test")`. Verify daemon creates session, streams result back, parent gets turn_complete notification.
3. **SSH tunnel spawn**: Configure dict-style remote with `ssh_host`. Verify SSH tunnel process started, local port assigned, `RemoteSubAgentDriver` connects via `ws://127.0.0.1:<port>/ws`.
4. **Tunnel reuse**: Two remote spawns to same server → verify single SSH tunnel process, both sessions work.
5. **InteractionRequest round-trip**: Remote ccagent triggers a permission request (e.g., Write tool). Verify it's serialized → sent to local → parent notified → parent resolves via `reply_agent` → response sent to remote → CCAgent unblocks.
6. **Cancel**: Launch remote task, cancel via `cancel_agent`. Verify remote session interrupted.
7. **Follow-up message**: `message_agent(session_id, "also check X")`. Verify remote session processes it.
8. **Cleanup**: Ctrl+C runtime → verify SSH tunnel processes terminated.
9. **Disconnect/reconnect**: Kill daemon during task, restart, verify local driver reconnects (or fails gracefully).
10. **Backward compat**: Use old `ws://` URL string style → verify still works without tunnel.