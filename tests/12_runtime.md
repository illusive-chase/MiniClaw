# Test: Runtime — session lifecycle, agent registry, listener supervision

**Feature**: Runtime is the top-level orchestrator. It manages agent registration (with per-session factories), two-phase session creation (Session → RuntimeContext → Agent), fork/attach, listener supervision with exponential backoff, and graceful shutdown.

**Architecture Spec**: §8 (Runtime), §8.5 (RuntimeContext)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

---

## Test 1: Agent registration and session creation

**What this tests**: Runtime registers agent factories and creates sessions with the correct agent type using two-phase init.

**Steps**:
1. Start the application (main.py registers both "native" and "ccagent" factories)
2. The REPL starts — a session has been created

**Expected Behavior**:
- No errors on startup
- A session is created and bound to the CLIChannel
- The session's agent matches the registered type
- Session has a timestamped ID format: `YYYYMMDD_HHMMSS_<6hex>`
- The session has a `runtime_context` attribute set (RuntimeContext instance)
- Agent factory received `(config, runtime_context)` — both arguments

---

## Test 2: Two-phase session init

**What this tests**: `create_session()` uses two-phase init: Session with placeholder → RuntimeContext → Agent with context.

**Steps**:
1. Start the application
2. The session's agent should have access to session management tools

**Expected Behavior**:
- The session's RuntimeContext is created with a reference to both the Runtime and the session itself
- The agent's tool registry includes session management tools (`launch_agent`, `reply_agent`, etc.)
- These tools have a reference to the RuntimeContext

---

## Test 3: Multiple sessions via get_or_create_session

**What this tests**: Runtime finds existing sessions by sender_id tag, or creates new ones.

**Steps** (Feishu mode):
1. Start with Feishu listener
2. User A sends a message → session created with tag `sender_id: feishu:<user_a_id>`
3. User A sends another message → same session reused
4. User B sends a message → new session created with different sender_id

**For CLI testing**:
1. Check `/sessions` — each session has a unique ID

**Expected Behavior**:
- Sessions are keyed by sender_id tag
- Same sender reuses existing session
- Different senders get different sessions

---

## Test 4: Listener supervision — restart on failure

**What this tests**: Runtime._supervise() restarts listeners with exponential backoff.

**Steps**:
1. Start the application
2. Observe normal operation
3. (Simulate failure by temporarily breaking the listener — e.g., invalid config)

**Expected Behavior**:
- If a listener fails, Runtime logs: `Listener X failed: <error>`
- Listener restarts after backoff delay (starting at 2s, doubling up to 60s)
- After recovery, the listener works normally again
- Backoff schedule: 2s → 4s → 8s → 16s → 32s → 60s (capped)

---

## Test 5: Graceful shutdown

**Steps**:
1. Start the application
2. Build some conversation history
3. Exit with `/quit` or Ctrl+C (at idle prompt) or Ctrl+D

**Expected Behavior**:
- Runtime._shutdown() is called:
  1. `_shutting_down` flag set to True
  2. All listeners receive `shutdown()` call
  3. All sessions are persisted to disk
  4. All agents receive `shutdown()` call
- Log shows: `Runtime shutting down...` → `Runtime shutdown complete`
- No data loss — sessions with history are saved
- Process exits cleanly

---

## Test 6: Persist and restore session via Runtime

**Steps**:
1. Build conversation history
2. Check `/sessions` to see auto-persisted sessions
3. `/resume <id>` (calls `runtime.restore_session()`)

**Expected Behavior**:
- Session serialized to `.workspace/.sessions/<id>.json`
- JSON file contains all fields: id, sender_id, created_at, updated_at, name, messages, agent_type, agent_config, agent_state, metadata
- `agent_type` reflects the session's agent (e.g., "native" or "ccagent")
- `agent_config` contains the full serialized AgentConfig (model, tools, effort, etc.)
- `agent_state` contains agent-specific state (e.g., `{"sdk_session_id": "..."}` for CCAgent, `{}` for native)
- `metadata` contains `forked_from` and `tags` (including `sender_id`)
- Restore uses two-phase init: Session → RuntimeContext → Agent
- Agent's `restore_state()` called with persisted `agent_state`
- The restored session has a RuntimeContext with session management tools
- Backward compat: old JSON files without extended fields restore as "native" with default config

---

## Test 7: Fork session via Runtime

**Steps**:
1. Build history
2. `/fork <session_id>`

**Expected Behavior**:
- Runtime calls `source.agent.serialize_state()` → `source.agent.on_fork()` → new agent, `restore_state()`
- Fork uses two-phase init: Session → RuntimeContext → Agent
- New session created with:
  - New unique ID
  - Copied history (shallow copy)
  - `metadata.forked_from = source.id`
  - Own RuntimeContext instance
- New session registered in `runtime.sessions`
- Log: `Forked session <source_id> -> <new_id> (agent=<type>)`

---

## Test 8: Per-session agent factory

**What this tests**: Each session gets a fresh agent instance (not a singleton).

**Steps**:
1. Start the application
2. Fork a session: `/fork <session_id>`
3. Both sessions should have independent agents

**Expected Behavior**:
- Each session has its own NativeAgent (or CCAgent) instance
- Each agent has its own tool registry with its own RuntimeContext
- Modifying one session's agent config doesn't affect the other
- Session management tools in each session reference that session's RuntimeContext

---

## Test 9: RuntimeContext available to tools

**What this tests**: Session management tools are available in the tool registry.

**Steps**:
1. Start the application
2. Ask the agent: `What tools do you have available?`

**Expected Behavior**:
- The response includes session management tools: `launch_agent`, `reply_agent`, `message_agent`, `check_agents`, `cancel_agent`
- These tools are in addition to the standard tools (file_read, shell, etc.)
- The tools are functional — they can spawn sub-agents via RuntimeContext
