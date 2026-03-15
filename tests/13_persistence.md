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
- File contains all persistence fields:
  ```json
  {
    "id": "YYYYMMDD_HHMMSS_xxxxxx",
    "sender_id": "unknown",
    "created_at": "2026-...",
    "updated_at": "2026-...",
    "name": null,
    "messages": [
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."}
    ],
    "agent_type": "native",
    "agent_config": {
      "model": "",
      "system_prompt": "",
      "tools": null,
      "max_iterations": 30,
      "memory_enabled": true,
      "thinking": false,
      "effort": "medium",
      "temperature": 0.7,
      "extra": {}
    },
    "agent_state": {},
    "metadata": {
      "forked_from": null,
      "tags": {"sender_id": "unknown"}
    }
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

---

## Test 9: Backward compatibility — old format without extended fields

**What this tests**: Old session JSON files (without `agent_type`, `agent_config`, `agent_state`, `metadata`) still load correctly via defaults.

**Steps**:
1. Create an old-format JSON file in `.workspace/.sessions/`:
   ```bash
   cat > .workspace/.sessions/old_format_test.json << 'EOF'
   {
     "id": "old_format_test",
     "sender_id": "user1",
     "created_at": "2026-01-01T00:00:00+00:00",
     "updated_at": "2026-01-01T00:00:00+00:00",
     "name": "old session",
     "messages": [
       {"role": "user", "content": "hello"},
       {"role": "assistant", "content": "hi there"}
     ]
   }
   EOF
   ```
2. Type `/sessions` — verify the old session appears in the list
3. Type `/resume old_format_test`

**Expected Behavior**:
- Session loads without error
- Defaults applied: `agent_type = "native"`, `agent_config = {}`, `agent_state = {}`, `metadata = {}`
- History replays correctly (2 messages)
- Session restored as a "native" agent with default `AgentConfig`
- `sender_id` from top-level field is preserved in metadata tags

---

## Test 10: CCAgent session persistence with agent_state

**What this tests**: CCAgent sessions persist `agent_type: "ccagent"` and `agent_state` containing `sdk_session_id`.

**Steps**:
1. Start with `python cc_main.py`
2. Send a few messages
3. `/dump`
4. Inspect the JSON file

**Expected Behavior**:
- JSON file contains:
  ```json
  {
    "agent_type": "ccagent",
    "agent_state": {
      "sdk_session_id": "..."
    }
  }
  ```
- The `agent_config` field reflects the CCAgent configuration (model, tools, etc.)

---

## Test 11: Restore preserves agent type and config

**What this tests**: `restore_session()` uses persisted `agent_type` and `agent_config` instead of hardcoding "native".

**Steps**:
1. Start with `python cc_main.py`, send messages, `/dump`, note session ID
2. Restart the application
3. `/resume <session_id>`

**Expected Behavior**:
- Session restores as "ccagent" (not "native")
- Agent config (model, tools, effort, etc.) matches what was persisted
- If `agent_state` contained `sdk_session_id`, `agent.restore_state()` is called
- Context from the previous conversation is available

---

## Test 12: Persist session with forked_from metadata

**What this tests**: Forked sessions persist `metadata.forked_from` correctly.

**Steps**:
1. Build conversation, `/dump`, note session ID
2. `/fork <session_id>`
3. `/dump` the forked session
4. Inspect the forked session's JSON file

**Expected Behavior**:
- JSON contains `"metadata": {"forked_from": "<source_session_id>", "tags": {...}}`
- On restore, `session.metadata.forked_from` is correctly populated
