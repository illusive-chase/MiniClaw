# Test: Session Fork — copy history, rebind agent

**Feature**: Forking creates a new session with copied history and metadata. The new session can optionally use a different agent type or config. The original session is unaffected.

**Architecture Spec**: §3.8 (Fork), §8.2 (Runtime.fork_session), §11.1 (Fork scenario)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

---

## Test 1: Fork a session and verify history copy

**Steps**:
1. Start the CLI agent
2. Send: `My secret code is ALPHA-7`
3. Wait for response
4. Send: `Remember this: the project deadline is March 30`
5. Wait for response
6. Use `/dump` to save the session
7. Note the session ID from `/sessions`
8. Type `/fork <session_id>`

**Expected Behavior**:
- `Forked to session <new_id>` confirmation
- History replay shows all previous messages (both user and assistant messages rendered)
- The forked session has a new unique session ID
- Send: `What is my secret code?` — response should mention "ALPHA-7"
- The forked session has `metadata.forked_from` set to the source session ID

---

## Test 2: Original session unaffected after fork

**Steps**:
1. After forking (from Test 1), send new messages in the forked session
2. Use `/resume <original_session_id>` to return to the original
3. Check history

**Expected Behavior**:
- The original session's history is unchanged
- Messages sent in the forked session do not appear in the original
- Both sessions operate independently

---

## Test 3: Fork with empty history

**Steps**:
1. Start a fresh session
2. Use `/dump` to save it
3. `/fork <session_id>` immediately (no messages sent)

**Expected Behavior**:
- Fork succeeds with empty history
- No replay messages shown
- The forked session works normally for new messages

---

## Test 4: Fork preserves agent state

**What this tests**: `serialize_state()` → `on_fork()` → `restore_state()` chain.

**For NativeAgent**:
- NativeAgent is stateless, so `serialize_state()` returns `{}` and `on_fork()` returns `{}`
- Fork should work seamlessly

**For CCAgent** (run with `python cc_main.py`):
- CCAgent's `on_fork()` returns `{}` (fresh SDK session, no reuse)
- The forked CCAgent session starts with a fresh SDK client
- History is copied but SDK state is not shared

**Expected Behavior**:
- Fork works for both agent types
- No errors during the serialize/fork/restore cycle
