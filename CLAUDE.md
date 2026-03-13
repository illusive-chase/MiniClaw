# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MiniClaw is a minimal Python agent runtime that connects LLM providers to messaging channels with tool-use capabilities. It runs an agentic loop: user message → LLM → tool calls → LLM → reply.

Requires Python 3.12+.

## Commands

```bash
# Run the agent (CLI mode, default)
python main.py

# Run with overrides
python main.py --channel cli --provider anthropic --model claude-sonnet-4-6 -v

# Install as editable package
pip install -e .
```

There are no tests or linting configured yet.

## Architecture

The system follows a layered design where each layer has a single responsibility:

```
main.py (CLI entrypoint — builds all components, wires them together)
  ↓
Gateway (session service — owns sessions, routes messages, concurrency-safe per-session locks)
  ↓
Agent (pure LLM engine — runs the tool-call loop, no session/state ownership)
  ↓
Provider (LLM API adapter) + ToolRegistry (tool execution) + Memory (persistent recall)
```

**Key design rule:** Agent is stateless per call — it receives history and returns updated history. Gateway owns all session state and conversation history. Channels never talk to Agent directly.

### Extension Points (ABC-based)

All extension points use abstract base classes. Add new implementations and register them in the corresponding factory (`__init__.py`).

- **Provider** (`miniclaw/providers/base.py`) — LLM API adapters. Implementations: OpenAI-compatible, Anthropic. Tool specs use OpenAI format internally; AnthropicProvider converts on the fly.
- **Channel** (`miniclaw/channels/base.py`) — Message transports. Implementations: CLI (interactive stdin/stdout), Feishu (WebSocket via lark-oapi).
- **Tool** (`miniclaw/tools/base.py`) — Agent tools. Auto-discovered: any `Tool` subclass in a non-underscore `.py` file under `miniclaw/tools/` is instantiated automatically by `create_registry()`. Constructor injection supports `workspace_dir` and `memory` params.
- **Memory** (`miniclaw/memory/base.py`) — Persistent key-value store. Implementation: JSON file backend with substring search.

### Tool Auto-Discovery

`miniclaw/tools/__init__.py` scans the tools directory at startup, imports all modules, finds `Tool` subclasses, and instantiates them. To add a new tool: create a `.py` file in `miniclaw/tools/`, define a class extending `Tool`, and it will be registered automatically. No manual wiring needed.

### Slash Commands

Channel-level commands (`/help`, `/model`, `/reset`, `/sessions`, `/resume`, `/rename`, `/output`) are defined in `miniclaw/channels/commands.py`. The `CommandRegistry` resolves commands by longest-prefix match (enables subcommands like `/output markdown`).

### Configuration

`config.yaml` with `${ENV_VAR}` interpolation. Merged over defaults in `miniclaw/config.py`. Config sections: `provider`, `channel`, `agent`, `memory`, `logging`. CLI flags (`--channel`, `--provider`, `--model`, `--verbose`, `--log-level`) override config values.

### Session Persistence

Sessions are JSON files under `$workspace_dir/.sessions/`. `SessionManager` handles create/save/load/list. Gateway holds in-memory `SessionState` (session + history + per-session model override) and dumps to disk on shutdown.
