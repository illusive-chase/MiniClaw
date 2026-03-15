# Test: Session Observers — attach, detach, broadcast, replay

**Feature**: Sessions support read-only observers via `/attach`. Observers receive a replay of history followed by live event streaming. Observer channels auto-resolve InteractionRequests. `/detach` removes the observer.

**Architecture Spec**: §3.5 (Observer Broadcasting), §3.7 (Attach/Detach), §7.2 (CLIListener /attach, /detach), §11.2 (Attach CLI to Observe)

---

## Prerequisites

This test requires two active sessions. You can achieve this by:
1. Running one session and saving it with `/dump`
2. Or by using the Feishu listener to create a second session concurrently

For CLI-only testing, use `/dump` and session IDs.

```bash
cd mini-agent
python main.py
```

---

## Test 1: Attach as observer

**Steps**:
1. Start the CLI agent — note the session ID printed or use `/dump` to save
2. Send a few messages to build history
3. Use `/dump` to persist the session
4. Note the session ID (visible in `/sessions`)
5. The current session has live activity — use `/attach <session_id>` on another session's ID

**Expected Behavior**:
- `Attached as observer to <session_id>. Use /detach to leave.` confirmation
- History replay: past messages from the target session are rendered in the CLI
- When the observed session receives new messages (from another channel), the observer sees live events

---

## Test 2: Observer sees live events

**Scenario**: Requires two channels — e.g., Feishu + CLI, or two pipe-connected sessions.

**Alternative manual approach**:
1. Start CLI, create a session, note its ID
2. Use `/pipe <other_session_id>` to trigger activity on another session
3. Use `/attach <other_session_id>` to observe

**Expected Behavior**:
- TextDelta events render as progressive markdown in the observer
- ActivityEvent events show in the activity footer
- InteractionRequests are auto-resolved (observer cannot interact)

---

## Test 3: Detach from observer

**Steps**:
1. After attaching via `/attach <id>`
2. Type `/detach`

**Expected Behavior**:
- `Detached from <session_id>` confirmation
- Observer task is cancelled
- No more events forwarded from the observed session
- CLI returns to normal input mode

---

## Test 4: Observer failure does not affect primary

**What this tests**: If an observer's queue is full or the observer errors, the primary channel continues unaffected.

**Steps**:
1. Attach an observer to a session
2. Send a long message to the session that produces many events
3. Observer should gracefully handle high event volume

**Expected Behavior**:
- Primary channel renders all events correctly
- If observer queue fills (maxsize=1000), events are dropped silently
- No error visible to the primary user
- Warning in log file: "Observer queue full for session X, dropping event"

---

## Test 5: Detach when not attached

**Steps**:
1. Without attaching to any session, type `/detach`

**Expected Behavior**:
- Message: `Not attached to any session.`
- No error or crash
