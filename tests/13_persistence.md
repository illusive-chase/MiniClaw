# Test: Persistence — session save, load, list, message serialization

**Feature**: SessionManager handles session persistence under `.workspace/.sessions/` as JSON files. It serializes/deserializes ChatMessage objects including tool calls, and supports prefix-based session lookup.

**Architecture Spec**: §8.3 (Persistence), §12 (Persistence Format)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

---

## Test 1: Save session to disk

**Steps**:
1. Send a few messages
2. Type `/dump`
3. Check the file system:
   ```bash
   ls .workspace/.sessions/
   cat .workspace/.sessions/<session_id>.json
   ```

**Expected Behavior**:
- `Session saved: <session_id>`
- JSON file exists at `.workspace/.sessions/<session_id>.json`
- File contains:
  ```json
  {
    "id": "YYYYMMDD_HHMMSS_xxxxxx",
    "sender_id": "unknown",
    "created_at": "2026-...",
    "updated_at": "2026-...",
    "name": null,
    "messages": [
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."},
      ...
    ]
  }
  ```

---

## Test 2: Message serialization with tool calls

**Steps**:
1. Send a message that triggers tool use: `Read the file config.yaml`
2. Wait for response
3. `/dump`
4. Inspect the JSON file

**Expected Behavior**:
- Messages include tool call entries:
  ```json
  {
    "role": "assistant",
    "content": null,
    "tool_calls": [
      {"id": "...", "name": "file_read", "arguments": {"path": "config.yaml"}}
    ]
  }
  ```
- Tool result messages:
  ```json
  {
    "role": "tool",
    "content": "...",
    "tool_call_id": "..."
  }
  ```
- All messages serialize/deserialize correctly

---

## Test 3: Load session from disk

**Steps**:
1. Save a session with `/dump`
2. Note the session ID
3. Restart the application
4. Type `/resume <session_id>`

**Expected Behavior**:
- Session loaded from `.workspace/.sessions/<session_id>.json`
- History replays in the terminal
- Previous context is available (ask a follow-up question)
- Session registered in Runtime's sessions dict

---

## Test 4: List saved sessions

**Steps**:
1. Save multiple sessions (with different names using `/rename`)
2. Type `/sessions`

**Expected Behavior**:
- Sessions listed newest first (sorted by `updated_at` desc)
- Each entry shows: `<id>  <name>  (<updated_at>)`
- Maximum 20 sessions displayed
- Corrupt JSON files are silently skipped

---

## Test 5: Session naming and rename

**Steps**:
1. `/rename my-debug-session`
2. `/dump`
3. `/sessions`

**Expected Behavior**:
- Name appears in the session listing: `<id>  my-debug-session  (<timestamp>)`
- Name persisted in the JSON file: `"name": "my-debug-session"`

---

## Test 6: Resolve by prefix

**What this tests**: `SessionManager.resolve_prefix()` finds sessions by ID or name prefix.

**Steps**:
1. Save sessions with names: "auth-debug", "auth-feature"
2. Try `/resume auth` (ambiguous — two matches)
3. Try `/resume auth-d` (unique match)

**Expected Behavior**:
- Ambiguous prefix: error listing all matches
- Unique prefix: resolves to the correct session
- Note: `/resume` currently uses `runtime.restore_session()` which does exact ID match. Prefix resolution is available in SessionManager but may not be wired to the CLI command.

---

## Test 7: Empty session not saved

**Steps**:
1. Start a fresh session (no messages)
2. Type `/dump`

**Expected Behavior**:
- No file created (SessionManager.save() returns early if messages list is empty)
- Or a message indicating nothing to save

---

## Test 8: Corrupt file handling

**Steps**:
1. Create a corrupt JSON file in `.workspace/.sessions/`:
   ```bash
   echo "not valid json" > .workspace/.sessions/corrupt_test.json
   ```
2. Type `/sessions`

**Expected Behavior**:
- The corrupt file is silently skipped
- Other valid sessions are listed normally
- No crash or error visible to the user
