# Test: CCAgent — SDK-backed agent with plan mode

**Feature**: CCAgent wraps the Claude Agent SDK. It manages SDK clients, routes interactions through a queue, supports plan approval, and handles SessionControl for plan_execute restart. Stateful: maintains SDK session ID across messages.

**Architecture Spec**: §5.2 (CCAgent SDK-backed)

---

## Prerequisites

```bash
cd mini-agent
python cc_main.py
```

Ensure `config.yaml` has valid `ccagent` settings with an Anthropic API key.

---

## Test 1: Basic CCAgent conversation

**Steps**:
1. Start the CCAgent REPL
2. Send: `What is 2 + 2?`

**Expected Behavior**:
- Response streams progressively
- SDK processes the message and returns a text response
- No tool calls for a simple math question
- Prompt returns for next message

---

## Test 2: SDK tool execution with permission

**Steps**:
1. Send: `Create a file called hello.txt with the content "Hello World"`
2. Permission prompt appears

**Expected Behavior**:
- Activity footer shows tool lifecycle (START → waiting for permission)
- Permission panel shows tool name and file path
- Choose `[1] Allow` → tool executes → FINISH event
- Response confirms file creation
- Verify file exists: check `hello.txt` in the working directory

---

## Test 3: Permission denial

**Steps**:
1. Send: `Delete all files in /tmp/test_dir`
2. Permission prompt appears
3. Choose `[2] Deny` and enter reason: "Too dangerous"

**Expected Behavior**:
- Agent receives denial with reason
- Agent adjusts behavior (may ask for alternative or explain)
- No destructive action is taken

---

## Test 4: Plan mode — approval and execution

**Steps**:
1. Send a complex task: `Refactor the config.py file to use pydantic models instead of raw dicts`
2. Wait for plan approval prompt
3. Choose `[1] Yes, clear context and auto-accept edits`

**Expected Behavior**:
- Plan Review panel appears with green border showing the plan
- After choosing option 1:
  - `SessionControl("plan_execute")` is triggered internally
  - History is cleared
  - Agent resets (fresh SDK client)
  - Processing restarts with the plan content
  - Tool executions proceed with auto-accepted edits

---

## Test 5: Plan mode — rejection and continued planning

**Steps**:
1. Send a complex task
2. When plan approval appears, choose `[4] No, keep planning`
3. Enter feedback: "Focus only on the config section"

**Expected Behavior**:
- Agent receives rejection feedback
- Agent continues planning with the feedback incorporated
- A new plan may be proposed

---

## Test 6: Session continuity (SDK session ID)

**Steps**:
1. Send: `Remember that my project uses FastAPI`
2. Wait for response
3. Send: `What framework does my project use?`

**Expected Behavior**:
- Second response references "FastAPI"
- SDK session is reused across messages (same `_sdk_session_id`)
- Conversation context is maintained within the SDK

---

## Test 7: CCAgent reset

**Steps**:
1. Have an ongoing conversation
2. Use `/reset` to clear history

**Expected Behavior**:
- History is cleared
- Agent's `reset()` is called (SDK client closed, session ID cleared)
- Next message starts a fresh conversation
