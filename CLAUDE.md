# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

MiniClaw: a minimal Python agent runtime that connects LLM providers to messaging channels with tool-use capabilities and persistent memory. The agent runs a tool-call loop — it sends messages to an LLM, executes any requested tool calls, feeds results back, and repeats until the LLM responds with plain text.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run with CLI channel (interactive terminal)
python main.py

# Run with config overrides
python main.py --provider anthropic --model claude-sonnet-4-6 --channel cli --verbose

# Run with a custom config file
python main.py --config path/to/config.yaml
```

No test suite exists yet. Python 3.12+ required (uses `str | None` union syntax).

## Architecture

Four ABC-based extension points, each with a factory function in its `__init__.py`:

| Layer | ABC | Factory | Implementations |
|-------|-----|---------|-----------------|
| **Providers** | `providers/base.py:Provider` | `create_provider()` | `OpenAIProvider` (OpenAI-compatible APIs), `AnthropicProvider` (Claude API) |
| **Channels** | `channels/base.py:Channel` | `create_channel()` | `CLIChannel` (stdin/stdout), `FeishuChannel` (Lark WebSocket) |
| **Tools** | `tools/base.py:Tool` | `create_registry()` | `shell`, `file_read`, `file_write`, `file_edit`, `git`, `memory` |
| **Memory** | `memory/base.py:Memory` | `create_memory()` | `JsonMemory` (JSON file with substring search) |

### Key wiring

- `main.py` — CLI entrypoint. Parses args, loads config, builds all components via factories, assembles `Agent`, runs `agent.run_channel(channel)`.
- `agent.py:Agent` — Core orchestration. `process_message()` builds the message list (system prompt + memory context + conversation history), then enters `_run_tool_call_loop()` which iterates up to `max_tool_iterations` times.
- `config.py` — Loads YAML, merges with `DEFAULT_CONFIG`, interpolates `${ENV_VAR}` patterns.

### Tool auto-discovery

`tools/__init__.py:discover_tools()` scans `tools/*.py` for `Tool` subclasses (skipping `_`-prefixed and `base.py`). `create_registry()` instantiates them, injecting `workspace_dir` or `memory` based on `__init__` signature introspection. To add a tool: create a new `.py` file in `tools/` with a class that extends `Tool` — it will be picked up automatically.

### Provider format translation

Tool specs are always in OpenAI format (`{"type": "function", "function": {...}}`). `AnthropicProvider` translates to Anthropic format internally via `_to_api_tools()` and `_to_api_messages()`. The `Tool.spec()` base method produces OpenAI format.

### Conversation state

Per-sender conversation history is held in `Agent._conversations` (in-memory dict, capped at 20 messages per sender). No persistence across restarts.

## Configuration

`config.yaml` uses `${ENV_VAR}` syntax for secrets. Required env vars depend on provider/channel:
- Anthropic: `MINICLAW_ANTHROPIC_API_KEY`
- Feishu: `FEISHU_APP_ID`, `FEISHU_APP_SECRET`

## Adding New Components

- **Provider**: Subclass `Provider` in `providers/`, add to factory switch in `providers/__init__.py`.
- **Channel**: Subclass `Channel` in `channels/`, add to factory switch in `channels/__init__.py`.
- **Tool**: Create a new file in `tools/` with a `Tool` subclass. No registration needed — auto-discovered. Constructor can accept `workspace_dir: str` or `memory: Memory` for automatic injection.
- **Memory backend**: Subclass `Memory` in `memory/`, add to factory in `memory/__init__.py`.
