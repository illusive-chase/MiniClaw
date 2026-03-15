# Test: Runtime — session lifecycle, agent registry, listener supervision

**Feature**: Runtime is the top-level orchestrator. It manages agent registration, session creation/fork/attach/pipe, listener supervision with exponential backoff, and graceful shutdown.

**Architecture Spec**: §8 (Runtime)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

---

## Test 1: Agent registration and session creation

**What this tests**: Runtime registers agent factories and creates sessions with the correct agent type.

**Steps**:
1. Start the application (main.py registers "native", cc_main.py registers "ccagent")
2. The REPL starts — a session has been created

**Expected Behavior**:
- No errors on startup
- A session is created and bound to the CLIChannel
- The session's agent matches the registered type
- Session has a timestamped ID format: `YYYYMMDD_HHMMSS_<6hex>`

---

## Test 2: Multiple sessions via get_or_create_session

**What this tests**: Runtime finds existing sessions by sender_id tag, or creates new ones.

**Steps** (Feishu mode):
1. Start with Feishu listener
2. User A sends a message → session created with tag `sender_id: feishu:<user_a_id>`
3. User A sends another message → same session reused
4. User B sends a message → new session created with different sender_id

**For CLI testing**:
1. Use `/dump` to save session
2. Check `/sessions` — each session has a unique ID

**Expected Behavior**:
- Sessions are keyed by sender_id tag
- Same sender reuses existing session
- Different senders get different sessions

---

## Test 3: Listener supervision — restart on failure

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

## Test 4: Graceful shutdown

**Steps**:
1. Start the application
2. Build some conversation history
3. Exit with `/quit` or Ctrl+C (at idle prompt) or Ctrl+D

**Expected Behavior**:
- Runtime._shutdown() is called:
  1. `_shutting_down` flag set to True
  2. All listeners receive `shutdown()` call
  3. All active pipes are disconnected (drivers shut down via `driver.shutdown()`)
  4. All sessions are persisted to disk
  5. All agents receive `shutdown()` call
- Log shows: `Runtime shutting down...` → `Runtime shutdown complete`
- No data loss — sessions with history are saved
- No lingering pipe tasks after shutdown
- Process exits cleanly

---

## Test 5: Persist and restore session via Runtime

**Steps**:
1. Build conversation history
2. `/dump` (calls `runtime.persist_session()`)
3. `/sessions` to see saved sessions
4. `/resume <id>` (calls `runtime.restore_session()`)

**Expected Behavior**:
- Session serialized to `.workspace/.sessions/<id>.json`
- JSON file contains all fields: id, sender_id, created_at, updated_at, name, messages, agent_type, agent_config, agent_state, metadata
- `agent_type` reflects the session's agent (e.g., "native" or "ccagent")
- `agent_config` contains the full serialized AgentConfig (model, tools, effort, etc.)
- `agent_state` contains agent-specific state (e.g., `{"sdk_session_id": "..."}` for CCAgent, `{}` for native)
- `metadata` contains `forked_from` and `tags` (including `sender_id`)
- Restore creates a new Session with:
  - Deserialized history
  - Agent created from persisted `agent_type` (not hardcoded "native")
  - AgentConfig rebuilt from persisted `agent_config` (not default)
  - `agent.restore_state()` called with persisted `agent_state`
  - Full SessionMetadata rebuilt including `forked_from` and `tags`
- The restored session is added to `runtime.sessions`
- Backward compat: old JSON files without extended fields restore as "native" with default config

---

## Test 6: Fork session via Runtime

**Steps**:
1. Build history, `/dump`
2. `/fork <session_id>`

**Expected Behavior**:
- Runtime calls `source.agent.serialize_state()` → `source.agent.on_fork()` → new agent, `restore_state()`
- New session created with:
  - New unique ID
  - Copied history (shallow copy)
  - `metadata.forked_from = source.id`
- New session registered in `runtime.sessions`
- Log: `Forked session <source_id> -> <new_id> (agent=<type>)`

---

## Test 7: Connect pipe via Runtime

**Steps**:
1. Have two sessions (fork one)
2. `/pipe <other_session_id>`

**Expected Behavior**:
- `create_pipe()` creates linked PipeEnd pair
- Two PipeDriver tasks started via `asyncio.create_task()`
- Pipe stored in `runtime._pipes` dict keyed by sorted session IDs
- Log: `Pipe connected: <id_a> <-> <id_b>`
- Sessions can communicate through the pipe

---

## Test 8: Disconnect pipe via Runtime

**Steps**:
1. Have two sessions connected via pipe (use `/pipe`)
2. Type `/unpipe <other_session_id>`

**Expected Behavior**:
- `Pipe disconnected: <session_id> <-> <other_session_id>`
- Both PipeDrivers shut down cleanly (POISON_PILL sent to each PipeEnd)
- Pipe removed from `runtime._pipes` dict
- PipeDriver tasks exit (no lingering background tasks)
- Both sessions continue to work normally (no more pipe input)
- Log: `Pipe disconnected: <id_a> <-> <id_b>`

---

## Test 9: Disconnect nonexistent pipe

**Steps**:
1. Have a session with no active pipes
2. Type `/unpipe <some_other_session_id>`

**Expected Behavior**:
- Error displayed: `No pipe between <session_id> and <other_session_id>`
- No crash
