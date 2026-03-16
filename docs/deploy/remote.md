# Getting Started: Local NativeAgent + Remote CCAgent

This guide walks through setting up a local NativeAgent that can spawn remote CCAgent sub-agents via WebSocket. The local agent handles conversation and tool orchestration, while heavy coding tasks are delegated to a Claude Code-backed CCAgent running on a remote server.

## Architecture Overview

```
┌─ Local Machine ──────────────────────┐     ┌─ Remote Server ──────────────────┐
│                                      │     │                                  │
│  CLIListener                         │     │  RemoteDaemon (ws://host:9100)   │
│    ↓                                 │     │    ↓                             │
│  Session (NativeAgent)               │ WS  │  Session (CCAgent)               │
│    ↓                                 │◄───►│    ↓                             │
│  launch_agent(remote="server1")      │     │  Claude Agent SDK subprocess     │
│    → RemoteSubAgentDriver ──────────►│     │    → DaemonSessionHandler        │
│                                      │     │                                  │
│  reply_agent / cancel_agent          │     │  InteractionRequest bridging     │
│    ← interaction forwarding ◄────────│     │    ← permission_required         │
└──────────────────────────────────────┘     └──────────────────────────────────┘
```

**Key point**: The NativeAgent decides *when* and *what* to delegate. The remote CCAgent executes autonomously, forwarding permission requests back to the local agent for approval.

## Prerequisites

- Python 3.12+
- An Anthropic API key
- Claude Code CLI installed on the **remote** server (CCAgent wraps `claude-agent-sdk` which requires the CLI)
- Network connectivity between local and remote machines on the chosen port (default 9100)

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
minicode --serve --host 0.0.0.0 --port 9100
```

Or equivalently:

```bash
python miniclaw/cc_main.py --serve --host 0.0.0.0 --port 9100
```

You should see:

```
RemoteDaemon listening on ws://0.0.0.0:9100/ws
```

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

# Map friendly names to remote daemon WebSocket URLs
remotes:
  server1: "ws://YOUR_REMOTE_IP:9100/ws"

logging:
  file_level: "debug"
  console_level: "info"
```

Replace `YOUR_REMOTE_IP` with the remote server's IP or hostname. You can define multiple remotes:

```yaml
remotes:
  server1: "ws://192.168.1.100:9100/ws"
  server2: "ws://10.0.0.50:9100/ws"
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

1. `RuntimeContext.spawn()` detects `remote="server1"`, resolves it to `ws://...` via `config.remotes`
2. Creates a `RemoteSubAgentDriver` that connects to the daemon over WebSocket
3. Sends a `spawn` message; daemon creates a CCAgent session and starts processing
4. Events stream back over WebSocket (`text_delta`, `activity`, `turn_complete`)
5. When the remote CCAgent needs permission (e.g., to write a file), an `interaction_request` is forwarded to the local NativeAgent
6. The NativeAgent resolves it via `reply_agent`, and the response is sent back over WebSocket
7. On completion, a `turn_complete` notification is injected into the parent session

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

- Verify the daemon is running: you should see `RemoteDaemon listening on ws://...`
- Check firewall rules — port 9100 must be open
- Verify the URL in `config.yaml` matches the daemon's bind address

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
