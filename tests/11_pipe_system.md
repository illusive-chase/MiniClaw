# Test: Pipe System — PipeEnd & PipeDriver bidirectional communication

**Feature**: Pipes connect two sessions for bidirectional agent-to-agent communication. PipeEnd is a Channel implementation with linked inbox queues. PipeDriver reads from the pipe and feeds messages to the session.

**Architecture Spec**: §6.4 (PipeEnd), §7.4 (PipeDriver), §10 (Pipe Full Lifecycle)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

You need two sessions to test pipes. Create one, dump it, then use `/pipe`.

---

## Test 1: Create a pipe between two sessions

**Steps**:
1. Start the CLI agent — this creates session A
2. Send a message to establish context: `You are a project planner. When you receive test results, analyze them.`
3. `/dump` session A
4. Note session A's ID from `/sessions`
5. Create a second session by forking: `/fork <session_A_id>` — this creates session B
6. Note session B's ID
7. Type `/pipe <session_A_id>` (connects current session B to session A)

**Expected Behavior**:
- `Pipe connected: <session_B_id> <-> <session_A_id>`
- Two PipeDriver tasks are spawned in the background
- Both sessions can now exchange messages through the pipe

---

## Test 2: Message flow through pipe

**What this tests**: The full message flow as described in §10.2.

**Scenario**: After connecting sessions via pipe:
1. Session A's agent produces text → collected by PipeEnd A → pushed to PipeEnd B's inbox
2. PipeDriver B reads from inbox → feeds to session B via `session.process()`
3. Session B's agent responds → collected by PipeEnd B → pushed to PipeEnd A's inbox
4. PipeDriver A reads → feeds to session A

**Steps**:
1. After pipe connection, send a message to one session
2. Watch for activity indicating the piped session is processing

**Expected Behavior**:
- Messages flow bidirectionally
- Each session processes incoming pipe messages through its agent
- InteractionRequests on pipes are auto-resolved (no human on a pipe)

---

## Test 3: PipeEnd auto-resolves interactions

**What this tests**: PipeEnd.send_stream() auto-resolves all InteractionRequests since there's no human on a pipe endpoint.

**Steps**:
1. Connect two sessions via pipe
2. One session triggers an action that would normally require permission

**Expected Behavior**:
- Permission is auto-granted
- No blocking prompt on the pipe
- Processing continues automatically

---

## Test 4: Disconnect pipe via /unpipe

**What this tests**: `Runtime.disconnect_pipe()` tears down a pipe by calling `shutdown()` on both PipeDrivers, which sends POISON_PILL to terminate each driver's loop.

**Steps**:
1. Connect two sessions via pipe: `/pipe <session_B_id>`
2. Verify the pipe is active (messages flow between sessions)
3. Type `/unpipe <session_B_id>`

**Expected Behavior**:
- `Pipe disconnected: <session_A_id> <-> <session_B_id>`
- Both PipeDrivers exit their loops cleanly
- Pipe removed from `runtime._pipes` tracking dict
- No error or crash
- The disconnected sessions return to normal (no more pipe input)
- Log: `Pipe <name> disconnected` for each PipeDriver

---

## Test 5: POISON_PILL direct disconnect

**What this tests**: PipeEnd.disconnect() sends POISON_PILL directly (low-level mechanism used by PipeDriver.shutdown()).

**Steps**:
1. Connect two sessions via pipe
2. Observe that the pipe is active
3. (Programmatic test) Call `pipe_end.disconnect()` on one end

**Expected Behavior**:
- POISON_PILL is placed in the inbox
- PipeDriver's `listen()` returns None
- PipeDriver exits its loop cleanly
- No error or crash

---

## Test 6: Pipe with different agent types

**Scenario**: Connect a NativeAgent session with a CCAgent session.

**Steps**:
1. Start with `python main.py` (NativeAgent)
2. Register a CCAgent session (requires both agent types registered)
3. Connect via pipe

**Expected Behavior**:
- Both agents communicate through the pipe
- Each processes messages according to its own capabilities
- The pipe abstraction is agent-agnostic — only text flows through

---

## Notes

- Use `/unpipe <session_id>` to disconnect a pipe, or `Runtime.disconnect_pipe()` programmatically
- Runtime tracks active pipes in `_pipes` dict keyed by sorted session IDs
- All active pipes are torn down during `Runtime._shutdown()`
- Pipes auto-resolve all interactions; there is no human-in-the-loop on pipe channels
