# Getting Started: Local NativeAgent + Remote CCAgent

This guide walks through setting up a local NativeAgent that can spawn remote CCAgent sub-agents via WebSocket. The local agent handles conversation and tool orchestration, while heavy coding tasks are delegated to a Claude Code-backed CCAgent running on a remote server.

## Architecture Overview

The daemon binds to `127.0.0.1` (localhost only) by default. Remote access is secured via SSH tunnels — the local machine opens an SSH connection and forwards a local port through it to the daemon.

```
┌─ Local Machine ──────────────────────┐     ┌─ Remote Server ──────────────────┐
│                                      │     │                                  │
│  CLIListener                         │     │  RemoteDaemon (127.0.0.1:9100)  │
│    ↓                                 │     │    ↓                             │
│  Session (NativeAgent)               │ SSH │  Session (CCAgent)               │
│    ↓                                 │◄───►│    ↓                             │
│  launch_agent(remote="server1")      │     │  Claude Agent SDK subprocess     │
│    → TunnelManager (SSH tunnel)      │     │    → DaemonSessionHandler        │
│    → RemoteSubAgentDriver ──────────►│     │                                  │
│      ws://127.0.0.1:<local>/ws       │     │  InteractionRequest bridging     │
│                                      │     │    ← permission_required         │
│  reply_agent / cancel_agent          │     │                                  │
│    ← interaction forwarding ◄────────│     │                                  │
└──────────────────────────────────────┘     └──────────────────────────────────┘
```

**Key point**: The NativeAgent decides *when* and *what* to delegate. The remote CCAgent executes autonomously, forwarding permission requests back to the local agent for approval. All traffic is encrypted and authenticated via SSH.

## Prerequisites

- Python 3.12+
- An Anthropic API key
- Claude Code CLI installed on the **remote** server (CCAgent wraps `claude-agent-sdk` which requires the CLI)
- SSH access to the remote server (key-based auth recommended; `ssh-agent` supported)

## Step 1: Install MiniClaw on Both Machines

```bash
git clone <repo-url> && cd mini-agent
pip install -e .
```

This installs both `miniclaw` (NativeAgent entry point) and `minicode` (CCAgent entry point) commands, plus all dependencies including `aiohttp` for WebSocket transport.

## Step 2: Configure the Remote Server

Create `config.yaml` on the remote server:

```yaml
provider:
  type: "anthropic"
  api_key: "${MINICLAW_ANTHROPIC_API_KEY}"
  model: "claude-sonnet-4-6"

ccagent:
  model: "claude-sonnet-4-6"
  permission_mode: "default"
  allowed_tools:
    - "Read"
    - "Write"
    - "Edit"
    - "Bash"
    - "Glob"
    - "Grep"
  thinking:
    type: "adaptive"
  effort: "high"
  cwd: "/path/to/project"     # working directory for the CCAgent

agent:
  workspace_dir: ".workspace"  # session persistence directory

logging:
  file_level: "debug"
  console_level: "info"
```

Set the API key:

```bash
export MINICLAW_ANTHROPIC_API_KEY="sk-ant-..."
```

## Step 3: Start the Remote Daemon

```bash
minicode --serve
```

Or equivalently:

```bash
python miniclaw/cc_main.py --serve
```

You should see:

```
RemoteDaemon listening on ws://127.0.0.1:9100/ws
```

The daemon binds to `127.0.0.1` by default — it is **not** exposed to the network. Remote clients connect through SSH tunnels (configured in Step 4). If you need to override the bind address (e.g. for testing on a private network), pass `--host 0.0.0.0`.

The daemon:
- Accepts WebSocket connections at `/ws`
- Spawns CCAgent sessions on demand per client request
- Supports up to 10 concurrent sessions (configurable)
- Keeps orphaned sessions alive for 5 minutes (grace period for reconnection)
- Registers both `ccagent` and `native` agent factories (remote can spawn either type)

## Step 4: Configure the Local Machine

Create `config.yaml` on the local machine:

```yaml
provider:
  type: "anthropic"
  api_key: "${MINICLAW_ANTHROPIC_API_KEY}"
  model: "claude-sonnet-4-6"
  temperature: 0.7

channel:
  type: "cli"

agent:
  system_prompt: >
    You are a helpful assistant with access to tools.
    Use tools when needed to answer questions and complete tasks.
    You can delegate complex coding tasks to a remote Claude Code agent
    using the launch_agent tool with remote="server1".
  max_tool_iterations: 50
  workspace_dir: ".workspace"

# Map friendly names to remote daemons.
# SSH tunnel config (recommended) — encrypted, authenticated:
remotes:
  server1:
    ssh_host: "remote-host"       # hostname or IP
    ssh_user: "deploy"            # optional, defaults to current user / ssh config
    ssh_port: 22                  # optional, default 22
    ssh_key: "~/.ssh/id_rsa"     # optional, uses ssh-agent if omitted
    daemon_port: 9100             # optional, default 9100
    local_port: 0                 # optional, 0 = auto-assign free port

logging:
  file_level: "debug"
  console_level: "info"
```

The `TunnelManager` automatically creates an SSH tunnel (`ssh -N -L`) when the remote is first used. Multiple sessions to the same remote share a single SSH connection. Tunnels are cleaned up on shutdown.

You can define multiple remotes:

```yaml
remotes:
  server1:
    ssh_host: "192.168.1.100"
    ssh_user: "deploy"
  server2:
    ssh_host: "10.0.0.50"
    ssh_key: "~/.ssh/id_ed25519"
    daemon_port: 9200
```

**Direct URL mode (backward compatible, no tunnel)** — for testing or private networks:

```yaml
remotes:
  local_test: "ws://localhost:9100/ws"
  staging: "wss://staging.example.com:9100/ws"   # TLS
```

Set the API key (needed for the local NativeAgent's own LLM calls):

```bash
export MINICLAW_ANTHROPIC_API_KEY="sk-ant-..."
```

## Step 5: Start the Local NativeAgent

```bash
python miniclaw/main.py
```

Or via the installed entry point:

```bash
miniclaw
```

You'll get a CLI prompt. The NativeAgent now has access to the `launch_agent` tool with a `remote` parameter.

## Step 6: Delegate Tasks to the Remote CCAgent

In the CLI, ask the NativeAgent to spawn a remote sub-agent:

```
> Can you ask a remote ccagent to read the README.md in the project and summarize it?
```

The NativeAgent will call:

```json
{
  "tool": "launch_agent",
  "args": {
    "type": "ccagent",
    "task": "Read README.md and provide a summary",
    "remote": "server1"
  }
}
```

You can also use a raw WebSocket URL instead of a config alias:

```json
{
  "remote": "ws://192.168.1.100:9100/ws"
}
```

### What Happens Under the Hood

1. `RuntimeContext.spawn()` detects `remote="server1"`, calls `_resolve_remote_url()`
2. `_resolve_remote_url()` finds the dict config, calls `TunnelManager.get_or_create()`
3. `TunnelManager` launches `ssh -N -L <local>:127.0.0.1:9100 deploy@remote-host`
4. Returns `ws://127.0.0.1:<local_port>/ws` — a local endpoint for the tunnel
5. Creates a `RemoteSubAgentDriver` that connects to this local endpoint
6. Traffic flows through the SSH tunnel to the remote daemon on `127.0.0.1:9100`
7. Events stream back over WebSocket (`text_delta`, `activity`, `turn_complete`)
8. When the remote CCAgent needs permission (e.g., to write a file), an `interaction_request` is forwarded to the local NativeAgent
9. The NativeAgent resolves it via `reply_agent`, and the response is sent back over WebSocket
10. On completion, a `turn_complete` notification is injected into the parent session

## Managing Remote Sub-Agents

The NativeAgent has several tools for managing spawned sub-agents:

| Tool | Purpose |
|------|---------|
| `launch_agent` | Spawn a new sub-agent (local or remote) |
| `reply_agent` | Respond to a permission request from a sub-agent |
| `message_agent` | Send a follow-up message to a running sub-agent |
| `check_agents` | List all sub-agents and their status |
| `cancel_agent` | Interrupt a running sub-agent |

### Handling Permission Requests

When the remote CCAgent triggers a permission request (e.g., writing a file), the local NativeAgent receives a notification like:

```
[sub_agent] permission_required: {"command": "echo hello > test.txt", ...}
  session_id: 20260316_143022_abc123
  interaction_id: int_xyz789
  tool_name: Bash
```

The NativeAgent can then call:

```json
{
  "tool": "reply_agent",
  "args": {
    "session_id": "20260316_143022_abc123",
    "interaction_id": "int_xyz789",
    "action": "allow"
  }
}
```

## Troubleshooting

### Connection refused

- Verify the daemon is running: you should see `RemoteDaemon listening on ws://127.0.0.1:9100/ws`
- If using SSH tunnels, check that SSH key auth works: `ssh deploy@remote-host echo ok`
- If using direct URLs, verify the daemon was started with `--host 0.0.0.0` and the firewall allows port 9100
- Verify the remote config in `config.yaml` matches the daemon's bind address and port

### SSH tunnel fails to start

- Check that `ssh` is available on PATH
- Verify key-based auth works without password prompts (tunnels use `BatchMode=yes`)
- Check `~/.ssh/config` for conflicting settings
- Look for the SSH error message in the exception: `TunnelError: SSH tunnel exited immediately (rc=...): <stderr>`

### Spawn rejected: Max sessions reached

The daemon defaults to 10 concurrent sessions. Wait for existing sessions to complete, or restart the daemon to clear them.

### Remote agent fails immediately

- Check the daemon's logs (`.workspace/miniclaw.log`) for errors
- Verify the API key is set on the remote server
- Ensure Claude Code CLI is installed on the remote (`claude --version`)

### Connection lost during task

The `RemoteSubAgentDriver` retries with exponential backoff (2s to 60s, max 5 attempts). The daemon keeps the session alive for 5 minutes. If the client reconnects within the grace period, it re-attaches to the existing session.

### Permission requests timing out

If the local NativeAgent doesn't resolve an interaction in time and the WebSocket disconnects, all pending interactions are auto-denied. Ensure the local agent is running and responsive.
