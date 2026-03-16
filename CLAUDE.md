# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MiniClaw is a Python agent runtime that connects LLM providers to messaging channels. It ships two agent backends:

- **NativeAgent** — custom tool loop with raw OpenAI/Anthropic provider calls (stateless per turn)
- **CCAgent** — wraps `claude-agent-sdk` to use official Claude Code as the backend (stateful SDK subprocess)

## Running the Project

```bash
# NativeAgent mode (custom tools + provider)
python main.py

# CCAgent mode (Claude Code backend)
python cc_main.py
```

Both read `config.yaml` at startup. Config supports `${ENV_VAR}` interpolation.

Required env vars: `MINICLAW_ANTHROPIC_API_KEY`. For Feishu channel: `FEISHU_APP_ID`, `FEISHU_APP_SECRET`.

There are no automated tests. The `tests/` directory contains markdown test specifications (manual scenarios).

## Architecture

### Data Flow

```
Listener (CLIListener / FeishuListener)
  → submit() → Session (input queue + conversation state)
    → process() → Agent (NativeAgent or CCAgent)
      → async AgentEvent stream
        → Channel (CLIChannel / FeishuChannel)
```

### Core Design Principles

1. **Session is the nexus** — Session owns conversation history, agent config, and metadata. Agent and Channel are borrowed/bound by Runtime.
2. **Agent-Channel agnosticism** — Agents produce typed `AgentEvent` unions (`TextDelta`, `ActivityEvent`, `InteractionRequest`, `HistoryUpdate`, `SessionControl`). Channels consume them. Neither knows about the other.
3. **Listener/Channel split** — Listener handles input routing, Channel handles output rendering. These are separate concerns.
4. **Input queue model** — `Session.submit()` accepts messages from multiple sources (user, sub-agents, system). `Session.run()` consumes them.
5. **Cooperative interrupts** — `CancellationToken` is passed from Session to Agent; agent checks at defined checkpoints.

### Key Modules (`miniclaw/`)

| Module | Purpose |
|---|---|
| `runtime.py` | Top-level orchestrator: session lifecycle, listener supervision, agent registry, auto-persist |
| `session.py` | Central entity: owns history, input queue, coordinates agent+channel, per-session lock |
| `agent/protocol.py` | `AgentProtocol` — uniform async generator interface for all agents |
| `agent/native.py` | NativeAgent: manages own tool loop, calls providers directly |
| `agent/cc.py` | CCAgent: creates `ClaudeSDKClient` per process() call, translates SDK messages to AgentEvents |
| `providers/` | LLM provider abstraction (OpenAI, Anthropic) with unified `ChatMessage` format |
| `tools/` | Tool registry with auto-discovery; tools implement `Tool` ABC from `tools/base.py` |
| `channels/` | Output rendering: `CLIChannel` (Rich panels), `FeishuChannel` (card messages) |
| `listeners/` | Input sources: `CLIListener` (REPL with prompt_toolkit), `FeishuListener` (WebSocket) |
| `types.py` | `AgentEvent` union type definitions |
| `subagent/` | Sub-agent lifecycle: `SubAgentDriver` acts as Channel for child sessions |
| `persistence.py` | JSON-based session save/load in `.workspace/.sessions/` |
| `runtime_context.py` | Bridge for sub-agent ↔ parent session communication |
| `config.py` | Config loading with env var interpolation |
| `activity.py` | Real-time tool/sub-agent status tracking |

### Two-Phase Session Init

Runtime creates sessions in two phases: (1) create `Session` with config, (2) bind `RuntimeContext` then create `Agent` via registered factory. This allows the agent factory to receive the runtime context for sub-agent support.

### Agent Factories

Agents are created per-session via factory functions registered with `Runtime.register_agent(name, factory)`. Factory signature: `(AgentConfig, RuntimeContext | None) -> AgentProtocol`. Both entry points register both agent types ("native" and "ccagent").

### Tool Registry

Tools in `miniclaw/tools/` are auto-discovered. Each tool extends `Tool` ABC. The registry supports deny lists and injects `RuntimeContext` for session management tools (`session_tools.py` provides `launch_agent`, `reply_agent`, `cancel_agent`).

### CCAgent vs NativeAgent

- **CCAgent** delegates the agentic loop to `claude-agent-sdk`. It creates a new `ClaudeSDKClient` per `process()` call and translates SDK messages (`TextDelta`, `ToolUse`, `InteractionRequest`, etc.) into MiniClaw's `AgentEvent` stream. Maintains parallel thin history (user text + assistant text only).
- **NativeAgent** runs its own tool loop: calls provider → parses tool calls → executes tools → feeds results back. Stateless per turn. Supports both OpenAI and Anthropic providers.

### CLI Commands (CLIListener)

`/reset`, `/sessions`, `/resume <id>`, `/fork [id]`, `/attach <id>`, `/model <name>`, `/effort <level>`, `/help`

## Config Structure (`config.yaml`)

```yaml
provider:    # type, api_key, base_url, model, temperature
channel:     # type ("cli" | "feishu"), app_id, app_secret
agent:       # system_prompt, max_tool_iterations, workspace_dir
ccagent:     # model, permission_mode, allowed_tools, thinking, effort, cwd, max_turns
memory:      # path
logging:     # file_level, console_level
```

## Key Files for Common Tasks

- Adding a new tool: create a file in `miniclaw/tools/`, extend `Tool` from `tools/base.py`
- Adding a new channel: implement `Channel` ABC from `channels/base.py`
- Adding a new listener: implement `Listener` ABC from `listeners/base.py`
- Adding a new provider: implement the provider ABC from `providers/base.py`
- Modifying agent behavior: `agent/native.py` (tool loop) or `agent/cc.py` (SDK translation)
