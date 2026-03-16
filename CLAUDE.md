# MiniClaw Development Context

You are developing the **MiniClaw** codebase — a Python agent runtime that connects LLM providers to messaging channels.

---

## Project Overview

MiniClaw ships two agent backends:

- **NativeAgent** (`agent/native.py`) — custom tool loop with direct OpenAI/Anthropic provider calls. Stateless per turn: receives full history, runs LLM → tool → LLM loop, yields events, returns updated history.
- **CCAgent** (`agent/cc.py`) — wraps `claude-agent-sdk`. Stateful: maintains SDK subprocess across turns via session ID. Translates SDK messages into MiniClaw's event stream.

Both backends produce the same `AgentEvent` async stream consumed by Channels.

## Python Environment

- **Python Version:** 3.12
- **Python Executable:** `/Users/bytedance/miniconda3/bin/python`

## Architecture

```
Listener → submit() → Session (queue + history) → Agent.process() → AgentEvent stream → Channel
```

### Core Components

| Layer | Files | Role |
|-------|-------|------|
| **Runtime** | `runtime.py` | Top-level orchestrator. Manages session lifecycle, agent factory registry, listener supervision, auto-persist. |
| **Session** | `session.py` | Central entity. Owns conversation history, input queue, agent config, per-session lock. Intercepts `HistoryUpdate` and `SessionControl` events; forwards the rest to the bound Channel. |
| **Agent** | `agent/protocol.py`, `agent/native.py`, `agent/cc.py`, `agent/config.py` | `AgentProtocol` defines `process()` → `AsyncIterator[AgentEvent]`, plus `reset`, `shutdown`, `serialize_state`, `restore_state`, `on_fork`. |
| **Channel** | `channels/base.py`, `channels/cli.py`, `channels/feishu.py` | Output rendering. `CLIChannel` uses Rich; `FeishuChannel` sends card messages. Channels consume `AgentEvent` streams. |
| **Listener** | `listeners/base.py`, `listeners/cli.py`, `listeners/feishu.py` | Input sources. `CLIListener` is a REPL with prompt_toolkit and slash commands. |
| **Provider** | `providers/base.py`, `providers/anthropic_provider.py`, `providers/openai_provider.py` | LLM abstraction. Unified `ChatMessage` / `ChatResponse` format. Used by NativeAgent only. |
| **Tools** | `tools/base.py`, `tools/__init__.py`, `tools/*.py` | `Tool` ABC with auto-discovery from directory. `ToolRegistry` supports deny lists. `session_tools.py` provides sub-agent management (needs `RuntimeContext`). |
| **PlugCtx** | `plugctx/loader.py`, `plugctx/registry.py`, `plugctx/resolver.py` | Context injection system. Loads `CONTEXT.md` + `manifest.yaml` from disk, resolves dependencies, renders into system prompt. |
| **Remote** | `remote/protocol.py`, `remote/remote_driver.py`, `remote/tunnel.py`, `remote/daemon.py`, `remote/serve.py` | Remote agent execution via SSH tunnels + WebSocket. |
| **Persistence** | `persistence.py` | JSON-based session save/load in `.workspace/.sessions/`. |
| **Types** | `types.py` | `AgentEvent` union: `TextDelta`, `ActivityEvent`, `InteractionRequest`, `HistoryUpdate`, `SessionControl`, `InterruptedEvent`, `UsageEvent`. |
| **Misc** | `runtime_context.py`, `subagent_driver.py`, `activity.py`, `cancellation.py`, `interactions.py`, `usage.py`, `config.py` | Runtime↔tool bridge, sub-agent channel adapter, activity tracking, cooperative cancellation, interaction request types, token tracking, config loading with `${ENV}` interpolation. |

### Key Patterns

- **Event streaming**: All agents yield `AsyncIterator[AgentEvent]`. Session intercepts control events; channels render the rest.
- **Agent-Channel agnosticism**: Agents and channels are unaware of each other. Events are the only interface.
- **Two-phase session init**: Runtime creates Session first, then binds RuntimeContext and creates Agent via registered factory. This gives agent factories access to the runtime context for sub-agent support.
- **Cooperative interruption**: `CancellationToken` flows from Session → Agent. Agent checks at defined checkpoints. User SIGINT triggers `session.interrupt()`.
- **Sub-agent pattern**: `RuntimeContext.spawn()` creates a child session. `SubAgentDriver` acts as Channel for the child and notifier for the parent. Parent resolves child's interaction requests.
- **Tool auto-discovery**: Files in `miniclaw/tools/` are scanned for `Tool` subclasses and auto-registered. Session tools injected if `RuntimeContext` is available.

## Entry Points

```bash
python main.py     # or `miniclaw`  — NativeAgent REPL
python cc_main.py  # or `minicode`  — CCAgent REPL
```

Both read `config.yaml` at startup. Required env: `MINICLAW_ANTHROPIC_API_KEY`.

## How To: Common Tasks

- **Add a tool**: Create a file in `miniclaw/tools/`, extend `Tool` from `tools/base.py`. It will be auto-discovered.
- **Add a channel**: Implement `Channel` ABC from `channels/base.py`.
- **Add a listener**: Implement `Listener` ABC from `listeners/base.py`.
- **Add a provider**: Implement the ABC from `providers/base.py`.
- **Modify agent behavior**: Edit `agent/native.py` (tool loop) or `agent/cc.py` (SDK translation layer).
- **Modify session lifecycle**: Edit `session.py` (input/output flow) or `runtime.py` (creation/persistence).
- **Add a CLI command**: Edit `listeners/cli.py` (slash command handling).
- **Add a context**: Create a folder under `.workspace/contexts/` with `CONTEXT.md` and optional `manifest.yaml`.

## Known Gaps

See `TODO.md` for tracked CCAgent gaps (interrupt, live model switching, error handling, effort/thinking config, background tasks, structured output, fork/continue session).

## Tech Stack

- Python 3.12+, fully async (asyncio)
- `openai`, `anthropic` — provider SDKs
- `claude-agent-sdk` — CCAgent backend
- `rich` — terminal rendering
- `prompt_toolkit` — interactive REPL
- `pyyaml` — config parsing
- `aiohttp` — async HTTP
- `lark-oapi` — Feishu API
- No automated tests (manual markdown specs in `tests/`)
