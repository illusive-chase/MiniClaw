# CCAgent (MiniClaw) vs Official Claude Code — Gap Analysis

## What CCAgent Is

MiniClaw is a Python agent runtime that wraps the `claude-agent-sdk` (via `ClaudeSDKClient`) to provide a custom frontend for Claude Code's backend. It reimplements the **I/O layer** (listeners, channels, session management) while delegating the actual **agentic loop** (tool execution, system prompt, context management) to the SDK subprocess. It also ships a **NativeAgent** that runs its own tool loop with raw provider calls, but this report focuses on the CCAgent path.

---

## 1. Functionality Parity

| Capability | Official Claude Code | CCAgent (MiniClaw) | Status |
|---|---|---|---|
| **Core agentic loop** | Built-in | Delegated to SDK | **Parity** — SDK runs the real Claude Code subprocess |
| **Tool execution** (Read, Write, Edit, Bash, Glob, Grep, etc.) | Built-in | SDK handles it; CCAgent configures `allowed_tools` | **Parity** |
| **System prompt** | `claude_code` preset + appended custom prompt | Same — uses `{"type":"preset","preset":"claude_code","append":...}` | **Parity** |
| **Streaming text** | Real-time token streaming | SDK messages → `TextDelta` events → Rich Live panel | **Parity** |
| **Tool permission prompts** | Interactive allow/deny per tool | `can_use_tool` callback → `InteractionRequest` → CLI prompt | **Parity** |
| **AskUserQuestion** | Multi-option question UI | Mapped to `InteractionType.ASK_USER` → numbered CLI prompt | **Parity** |
| **Plan mode (EnterPlanMode/ExitPlanMode)** | Plan → approve → execute with elevated perms | `PLAN_APPROVAL` interaction → 4 options (clear+accept, accept, manual, reject) → `SessionControl(plan_execute)` | **Parity** |
| **Session resume** | `claude -c` / `claude --resume <id>` | SDK session ID stored as system message marker; `/resume <id>` command | **Partial** — works but no `continue_conversation` shortcut |
| **Permission modes** | `default`, `plan`, `acceptEdits` — switchable at runtime | Configurable at startup; plan approval can switch modes | **Partial** — no `/mode` slash command |
| **MCP support** | Full MCP server integration | Inherited from SDK subprocess | **Parity** (SDK handles it) |
| **CLAUDE.md** | Auto-loaded from project | SDK subprocess reads it | **Parity** |
| **Context compression** | Automatic on long conversations | SDK handles it | **Parity** |
| **WebSearch / WebFetch** | Built-in tools | Configured in `allowed_tools` | **Parity** |
| **TodoWrite** | Built-in tool | Configured in `allowed_tools` | **Parity** |
| **NotebookEdit** | Built-in tool | Configured in `allowed_tools` | **Parity** |
| **Skills system** | Extensible skill registration | Not reimplemented; SDK handles it | **Parity** |

---

## 2. Behavioral Gaps

### 2.1 Interrupt / Cancel (Not Implemented)

Official Claude Code lets users press `Ctrl+C` / `Esc` to interrupt mid-generation. The SDK exposes `client.interrupt()`. CCAgent has a `CancellationToken` mechanism, but `_run_sdk()` runs in a background task and `token.is_cancelled` only checks between queue reads — it cannot interrupt the SDK client mid-stream. Pressing `Ctrl+C` calls `session.interrupt()` which sets the token, but the SDK query runs to completion.

### 2.2 ThinkingBlock Rendering (Silently Dropped)

When extended thinking is enabled, `AssistantMessage.content` may contain `ThinkingBlock` objects. CCAgent explicitly `pass`es on them (`cc.py:266-267`). Official Claude Code renders thinking content in a collapsible section. Users get no visibility into the model's reasoning chain.

### 2.3 AssistantMessage Error Handling (Not Implemented)

The SDK's `AssistantMessage.error` field (types: `authentication_failed`, `billing_error`, `rate_limit`, `server_error`, etc.) is never checked. If a response arrives with an error and no content, the user sees `(no response)` with no explanation.

### 2.4 Background Task Lifecycle (Partially Implemented)

CCAgent now yields `ActivityEvent` for `TaskStartedMessage`, `TaskProgressMessage`, and `TaskNotificationMessage` — the activity footer shows background task status. However:

- No `client.stop_task(task_id)` integration — users can't cancel background tasks
- `TaskOutput` / task polling from the user side isn't exposed
- No `/tasks` command equivalent

### 2.5 Live Model Switching (Wasteful)

The `/model` command updates `session.agent_config.model`, but since CCAgent creates a new `ClaudeSDKClient` per `process()` call, the model change takes effect by creating an entirely new SDK subprocess (losing all SDK-side state). Official Claude Code uses `client.set_model()` to switch in-place.

### 2.6 Session History Mismatch

CCAgent maintains a **parallel history** — MiniClaw's `session.history` only stores the user text + flattened assistant reply text. All intermediate tool calls, tool results, and multi-turn context live solely in the SDK subprocess. This means:

- `/fork` creates a MiniClaw-level fork but the SDK subprocess is fresh (no tool context carried over)
- History replay shows only user/assistant text, not the rich tool-use trace
- Session persistence captures a thin summary, not the full agentic trace

### 2.7 Effort Level "max" Not Supported

The `/effort` command only accepts `low`, `medium`, `high` — it rejects `max` (`cli.py:263`). Official Claude Code supports `max` on Opus 4.6 models.

---

## 3. Agent-Configurable Items Gap

### 3.1 Items Configurable in Both

| Config Item | Official Claude Code | CCAgent |
|---|---|---|
| **Model** | `--model` flag, `/model` command, `model` option | `ccagent.model` in config.yaml, `/model` command |
| **System prompt** | `--system-prompt`, `system_prompt` option | `ccagent.system_prompt` in config.yaml |
| **Permission mode** | `--permission-mode`, `/permissions` | `ccagent.permission_mode` in config.yaml |
| **Allowed tools** | `--allowed-tools`, `allowed_tools` option | `ccagent.allowed_tools` in config.yaml |
| **Max turns** | `--max-turns`, `max_turns` option | `ccagent.max_turns` in config.yaml |
| **CWD** | `--cwd`, `cwd` option | `ccagent.cwd` in config.yaml |
| **Thinking** | `--thinking`, `thinking` option | `ccagent.thinking` in config.yaml |
| **Effort** | `--effort`, `/effort` command | `ccagent.effort` in config, `/effort` command (missing `max`) |
| **Resume session** | `--resume`, `-c` | `/resume` command |

### 3.2 Items Missing from CCAgent

| Config Item | Official Claude Code | CCAgent |
|---|---|---|
| **`output_format`** (structured output) | JSON Schema for machine-readable output | Not supported |
| **`fork_session`** | `--fork-session` — fork from a checkpoint | Not supported (SDK-level fork) |
| **`continue_conversation`** | `-c` — auto-continue last session | Not supported |
| **`disallowed_tools`** | Blocklist specific tools | Not supported (only allowlist) |
| **`append_system_prompt`** | Append to system prompt per-query | Supported in config but not per-query |
| **`mcp_servers`** / `CLAUDE_MCP_SERVERS` | Configure MCP servers | Not explicitly configurable (relies on env) |
| **Permission mode runtime switch** | `client.set_permission_mode()` | No slash command; only via plan approval flow |
| **`--verbose`** | Verbose output mode | Not configurable (fixed Rich rendering) |
| **`--no-markdown`** | Disable markdown rendering | Not supported |
| **Auto-memory (`/memory`)** | Persistent memory across sessions | NativeAgent has memory; CCAgent relies on SDK |
| **Keybindings** | `~/.claude/keybindings.json` | Not applicable (uses prompt_toolkit defaults) |
| **Hooks** | `~/.claude/settings.json` hooks | Not supported |
| **`/init`** | Generate CLAUDE.md for a project | Not supported |
| **`/doctor`** | Diagnostic health check | Not supported |
| **`/review`** | Code review workflow | Not supported |
| **`/compact`** | Compact conversation context | Not supported (SDK manages it internally) |
| **`/clear`** | Clear conversation + reset | `/reset` exists but doesn't reset SDK state properly |
| **Status line** | Configurable status bar | Not supported |
| **Cost tracking per-session** | Detailed per-session cost | Basic token counts, no per-model pricing |
| **Worktrees** | `EnterWorktree` / `--worktree` | Not supported |
| **Git safety protocol** | Built into system prompt | Inherited from SDK but no additional enforcement |

---

## 4. UX Gaps

| Feature | Official Claude Code | CCAgent |
|---|---|---|
| **Diff display** | Syntax-highlighted inline diffs for Edit/Write | No diff rendering — just activity status |
| **File preview** | Shows file content before/after edit | Not shown |
| **Spinner/progress** | Contextual spinners per tool | Single "Thinking..." spinner, then activity footer |
| **Compact mode** | Progressively compresses old context | SDK handles it; no user control |
| **Multi-line input** | `\` continuation, heredoc-style | Single-line prompt_toolkit (supports paste but no explicit multi-line mode) |
| **Tab completion** | Commands, file paths | No completion |
| **Color themes** | Follows terminal theme | Fixed Rich styling |
| **Image/PDF display** | Renders in terminal | Not supported in CLI channel |
| **Token cost display** | Per-model pricing, running total | Raw token counts only |

---

## 5. Architecture Differences (Strengths of CCAgent)

CCAgent isn't purely a subset — it adds capabilities official Claude Code doesn't have:

| Feature | CCAgent Unique |
|---|---|
| **Multi-channel output** | Same session can render to CLI + Feishu simultaneously |
| **Feishu/Lark integration** | Native Feishu channel with WebSocket listener, card rendering, debounced updates |
| **Observer mode** | `/attach` to watch another session read-only |
| **Sub-agent spawning** | NativeAgent can spawn sub-agent sessions via `launch_agent` tool |
| **Session forking (MiniClaw-level)** | Fork conversation with different agent type |
| **Dual agent registry** | Switch between CCAgent and NativeAgent per session |
| **Pluggable providers** | NativeAgent supports both OpenAI and Anthropic providers |
| **Persistent memory** | JSON-backed memory with recall (NativeAgent) |

---

## 6. Summary of Critical Gaps (Ranked by Impact)

1. **No interrupt/cancel** — Users cannot stop a runaway query. High frustration potential.
2. **Thinking blocks invisible** — Extended thinking (a premium feature) produces no visible output.
3. **SDK errors swallowed** — Auth failures, rate limits, billing errors show as `(no response)`.
4. **Thin history** — Session persistence loses all tool-use context; forks start from scratch.
5. **No structured output** — Blocks programmatic/headless use cases.
6. **No `continue_conversation`** — Must manually find and `/resume` session IDs.
7. **Effort `max` missing** — Can't use Opus 4.6's maximum reasoning mode.
8. **No task cancellation** — Background tasks run to completion with no user control.
9. **No diff rendering** — File edits are opaque; users can't review changes inline.
10. **Model switch destroys context** — Changing models restarts the SDK subprocess.
