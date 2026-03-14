# CCAgent — Functionality Gaps

Tracked gaps between CCAgent (MiniClaw's `claude_agent_sdk` wrapper) and official Claude Code.

---

## 1. Interrupt / Cancel Running Query

**Status:** Not implemented

CCAgent has no mechanism to cancel a query mid-execution. Once `client.query()` is called and `client.receive_response()` is being consumed, the user must wait for the full response to complete — even if it's clearly going down the wrong path or taking too long.

The SDK exposes `client.interrupt()` which sends an interrupt signal to the underlying Claude Code process, causing it to stop generation and return a partial result. This is equivalent to pressing `Ctrl+C` in interactive Claude Code.

Currently in `process_message_stream`, the background `_run_sdk()` task runs to completion. If the user wants to abort, there is no signal path from the channel back to the SDK client. The `task.cancel()` in the `finally` block only fires on exception/cleanup, not on user-initiated cancellation.

---

## 2. Live Model Switching

**Status:** Partially implemented (wasteful)

The `/model` command in `commands.py` updates `SessionState.model`, but when the next message is processed, `_get_or_create_client()` detects the model mismatch and **destroys the entire client** (`await self._close_client(key)`) to create a new one. This loses the SDK-side conversation context (the Claude Code subprocess is killed and restarted).

The SDK provides `client.set_model(model: str)` which changes the model for subsequent turns **without restarting the process**, preserving the full conversation context, CLAUDE.md state, MCP connections, and any in-flight background tasks. This is a simple method call on the existing client.

---

## 3. Live Permission Mode Switching

**Status:** Partially implemented (via client recreation)

Permission mode changes currently happen in two ways:
1. Plan approval flow: `PlanExecuteAction` triggers client recreation with a new `permission_mode_override`.
2. `PermissionUpdate` with `type="setMode"` is returned via `PermissionResultAllow.updated_permissions`, which the SDK handles internally.

However, there is no user-facing command (e.g., a `/permissions` or `/mode` slash command) that lets the user switch permission modes on the fly. More importantly, for programmatic switching outside of plan approval, the only path is to recreate the client entirely.

The SDK provides `client.set_permission_mode(mode: str)` which changes the mode in-place — no client restart needed. This would enable a `/mode` command that switches between `default`, `plan`, `acceptEdits`, etc. instantly.

---

## 4. AssistantMessage Error Handling

**Status:** Not implemented

`AssistantMessage` from the SDK carries an optional `.error` field of type `AssistantMessageError` with a `.type` attribute. The error types are:

- `"authentication_failed"` — API key invalid or expired
- `"billing_error"` — account billing issue (quota exceeded, payment failed)
- `"rate_limit"` — rate limited by the API
- `"invalid_request"` — malformed request sent by the SDK
- `"server_error"` — Anthropic API server error
- `"unknown"` — unclassified error

Currently, `process_message_stream` iterates over `AssistantMessage.content` blocks but never checks `message.error`. If an `AssistantMessage` arrives with an error and empty content, the user sees nothing (or `(no response)`). If it arrives with partial content plus an error, the error is silently swallowed.

These errors should be detected, logged, and surfaced to the user as distinct error messages (not just appended to the reply text), so the user understands *why* a response failed and can take action (re-authenticate, wait for rate limit, check billing, etc.).

---

## 5. Effort / Thinking Configuration

**Status:** Not implemented

Claude Code supports three related controls for reasoning depth:

1. **Effort level** (`effort` in `ClaudeAgentOptions`): A high-level knob with values `"low"`, `"medium"`, `"high"`, `"max"`. This controls how much reasoning effort the model applies. `"max"` is only available on Opus 4.6. Official Claude Code exposes this via `/effort` command and `Alt+P` model picker.

2. **Thinking configuration** (`thinking` in `ClaudeAgentOptions`): Fine-grained control over extended thinking:
   - `{"type": "adaptive"}` — model decides when to think
   - `{"type": "enabled", "budget_tokens": N}` — always think with a token budget
   - `{"type": "disabled"}` — no extended thinking

3. **ThinkingBlock in responses**: When thinking is enabled, `AssistantMessage.content` may contain `ThinkingBlock` objects (with `.thinking` text and `.signature`). CCAgent currently only handles `TextBlock` and `ToolUseBlock` — any `ThinkingBlock` is silently skipped.

None of these are configurable in `cc_main.py` or via slash commands. There is no `--effort` CLI flag, no `/effort` command, no config section for thinking, and thinking content is invisible to the user even when the SDK returns it.

---

## 6. Background Task Management

**Status:** Not implemented

Claude Code supports background tasks — the model can spawn long-running operations (via the `Agent` tool with `run_in_background: true`) that execute concurrently while the main conversation continues. The SDK surfaces these through three specialized `SystemMessage` subtypes:

- **`TaskStartedMessage`**: Emitted when a background task begins. Contains `task_id`, `description`, `session_id`, `tool_use_id`, `task_type`.
- **`TaskProgressMessage`**: Periodic progress updates. Contains `task_id`, `description`, `usage` (tokens, tool uses, duration), `last_tool_name`.
- **`TaskNotificationMessage`**: Emitted when a task finishes. Contains `task_id`, `status` (`"completed"` | `"failed"` | `"stopped"`), `output_file`, `summary`, `usage`.

CCAgent handles `SystemMessage` only for the `"init"` subtype (to extract `session_id`). All other system messages — including the three task lifecycle messages — are logged at debug level and discarded. The user has no visibility into:
- Whether background tasks are running
- What progress they've made
- Whether they succeeded or failed
- What their output was

Additionally, the SDK provides `client.stop_task(task_id)` to cancel a background task, but there is no path to invoke this.

---

## 7. Structured Output

**Status:** Not implemented

The SDK supports `output_format` in `ClaudeAgentOptions`, which accepts a JSON Schema dict. When set, the model's final `ResultMessage` includes a `.structured_output` field containing the validated, parsed output conforming to the schema.

This is useful for programmatic integrations where the consumer needs machine-readable output (e.g., a CI pipeline extracting a structured code review, a downstream system consuming a JSON report, or an API endpoint wrapping CCAgent).

Currently, `ResultMessage` processing only reads `.result` (the plain text). The `.structured_output` field is never checked. There is no way to pass `output_format` via config or CLI args. This gap matters for non-interactive (headless/API) use cases more than for the CLI channel.

---

## 8. Fork / Continue Session

**Status:** Not implemented

The SDK supports two session continuation modes beyond simple `resume`:

1. **`continue_conversation: bool`**: When `True`, automatically continues the most recent conversation in the working directory without needing a session ID. Equivalent to `claude -c` in official Claude Code.

2. **`fork_session: bool`**: When `True` (used with `resume`), creates a **new** session that starts with the full context of the resumed session but diverges from that point. The original session remains untouched. Equivalent to `claude --fork-session` — useful for exploring alternative approaches from a checkpoint without losing the original conversation.

CCAgent's session resume is implemented via `_extract_sdk_session_id` / `_inject_session_marker`, which stores the SDK session ID in the MiniClaw history and passes it as `resume` to `ClaudeAgentOptions`. However:
- There is no `continue_conversation` path — the user must explicitly use `/resume <id>`.
- There is no fork mechanism — resuming always reuses the same session, so the original history is modified in-place.
- The `/resume` command in `commands.py` switches the MiniClaw session but also passes the SDK session ID for resume, with no option to fork.
