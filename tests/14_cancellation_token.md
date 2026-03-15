# Test: CancellationToken — cooperative interrupt mechanism

**Feature**: CancellationToken provides cooperative cancellation from Session to Agent. It uses an asyncio.Event internally. Agents call `check()` at defined checkpoints: before provider.chat(), before tool.execute(), and optionally between stream chunks.

**Architecture Spec**: §4.4 (CancellationToken), §9 (Interrupt Flow)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

---

## Test 1: Token creation and initial state

**What this tests**: A fresh CancellationToken is not cancelled.

**Verification** (code-level):
```python
from miniclaw.cancellation import CancellationToken
token = CancellationToken()
assert not token.is_cancelled
token.check()  # Should NOT raise
```

**Expected Behavior**:
- `is_cancelled` is `False` on creation
- `check()` does not raise
- Token is clean and ready for use

---

## Test 2: Token cancellation

**Verification** (code-level):
```python
from miniclaw.cancellation import CancellationToken, CancelledError
token = CancellationToken()
token.cancel()
assert token.is_cancelled
try:
    token.check()
    assert False, "Should have raised"
except CancelledError:
    pass  # Expected
```

**Expected Behavior**:
- After `cancel()`, `is_cancelled` is `True`
- `check()` raises `CancelledError` with message "Processing interrupted by user"

---

## Test 3: Checkpoint before provider.chat()

**What this tests**: The agent calls `token.check()` before making an LLM call.

**Steps**:
1. Send a task that triggers multi-step tool use
2. Press `Ctrl+C` after one tool completes but before the next LLM call

**Expected Behavior**:
- The agent does NOT make another LLM call
- `[interrupted]` appears
- Prevents unnecessary API calls after cancellation

---

## Test 4: Checkpoint before tool.execute()

**What this tests**: The agent calls `token.check()` before executing each tool.

**Steps**:
1. Send a task that triggers multiple tool calls in sequence
2. Press `Ctrl+C` after one tool starts but before the next one executes

**Expected Behavior**:
- The next tool does NOT execute
- `[interrupted]` appears
- Prevents unnecessary tool work after cancellation

---

## Test 5: Full interrupt chain

**What this tests**: The complete flow: Ctrl+C → SIGINT → CLIListener → session.interrupt() → token.cancel() → check() raises → Session catches → InterruptedEvent

**Steps**:
1. Send: `Search for all Python files and read each one` (long multi-tool task)
2. Press `Ctrl+C` during processing

**Expected Behavior** (full chain):
1. SIGINT caught by CLIListener's signal handler
2. `session.interrupt()` called → `self._current_token.cancel()`
3. Agent's next `token.check()` raises `CancelledError`
4. Session.process() catches `CancelledError`
5. `InterruptedEvent(partial_history=...)` yielded
6. CLIChannel renders `[interrupted]`
7. Session lock released
8. `self._current_token` set to `None`
9. Prompt ready for next message

---

## Test 6: Token cleared after processing

**What this tests**: `self._current_token` is set to `None` in the `finally` block of Session.process().

**Steps**:
1. Send a message, wait for completion
2. Press `Ctrl+C` at the idle prompt

**Expected Behavior**:
- `session.interrupt()` sees `_current_token is None` → no-op
- No error, no crash
- SIGINT falls through to CLIListener's KeyboardInterrupt handler

---

## Test 7: Custom CancelledError (not asyncio.CancelledError)

**What this tests**: MiniClaw uses its own `CancelledError` (not `asyncio.CancelledError`) to avoid interfering with asyncio's internal cancellation.

**Expected Behavior**:
- `miniclaw.cancellation.CancelledError` is a plain `Exception` subclass
- It does not propagate as `asyncio.CancelledError`
- Session.process() catches it specifically, not all asyncio cancellations
