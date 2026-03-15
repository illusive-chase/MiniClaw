# Test: SubAgentDriver — background sub-agent lifecycle

**Feature**: SubAgentDriver is a dual-role component: Channel for a child session and notifier for a parent session. It auto-resolves InteractionRequests for allowed tools, forwards others to the parent session for approval, and notifies the parent on completion/failure/interruption.

**Architecture Spec**: §6.4 (SubAgentDriver), §8.5 (RuntimeContext), §10 (SubAgentDriver Lifecycle), §14 (Session Management Tools)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

Ensure both "native" and "ccagent" agent types are registered. `main.py` registers both by default.

---

## Test 1: Launch a background sub-agent

**Steps**:
1. Start the CLI agent
2. Ask the agent to delegate a task: `Launch a background ccagent to read the file config.yaml and summarize its contents`
3. The agent should call the `launch_agent` tool

**Expected Behavior**:
- The `launch_agent` tool call appears in the activity footer
- Tool result includes the new session ID (e.g., `Sub-agent launched successfully. Session ID: 20260316_120000_abc123`)
- A background sub-agent session is created and starts processing
- The parent agent receives a response and can continue interacting with the user

---

## Test 2: Sub-agent completes and notifies parent

**Steps**:
1. Launch a sub-agent with a simple task (from Test 1)
2. Wait for the sub-agent to complete

**Expected Behavior**:
- When the sub-agent finishes, the parent session receives a notification
- The notification is injected as a synthetic `sub_agent_event` tool call/result pair in parent's history
- The parent agent processes the notification and can report the result to the user
- The SubAgentDriver's status changes to "completed"

---

## Test 3: Check sub-agent status

**Steps**:
1. Launch a sub-agent
2. Ask the agent: `Check the status of running sub-agents`
3. The agent should call the `check_agents` tool

**Expected Behavior**:
- Tool result lists all sub-agents with:
  - Session ID
  - Status (running / completed / failed / interrupted)
  - Result preview (for completed agents)
  - Pending interactions (if any)

---

## Test 4: Sub-agent permission forwarding

**What this tests**: When a sub-agent needs a tool not in `allowed_tools`, the permission request is forwarded to the parent.

**Steps**:
1. Launch a sub-agent with limited allowed_tools: `Launch a ccagent to create a test file. Only allow Read and Glob tools.`
2. The sub-agent will need to use Write (not in allowed_tools)
3. The parent session receives a permission notification

**Expected Behavior**:
- Sub-agent's InteractionRequest for "Write" tool is NOT auto-resolved (not in allowed_tools)
- The request is stored in `_pending_interactions`
- Parent session receives a `sub_agent_event` notification with:
  - `event_type: "permission_required"`
  - `session_id`, `interaction_id`, `tool_name`, `tool_input`
- The parent agent sees the notification and can call `reply_agent` to approve/deny

---

## Test 5: Reply to sub-agent permission request

**Steps**:
1. From Test 4, a permission request is pending
2. Ask the agent: `Allow the sub-agent to use the Write tool`
3. The agent should call `reply_agent` with `action: "allow"`

**Expected Behavior**:
- The `reply_agent` tool resolves the pending InteractionRequest
- The sub-agent's `can_use_tool` callback unblocks
- The sub-agent continues processing with the allowed tool
- Tool result: `Interaction <id> resolved: allowed`

---

## Test 6: Deny sub-agent permission

**Steps**:
1. When a permission request is pending
2. Ask the agent: `Deny the sub-agent's request to use that tool`
3. The agent calls `reply_agent` with `action: "deny"` and a reason

**Expected Behavior**:
- The InteractionRequest is resolved with `allow=False`
- The sub-agent receives the denial and adjusts its approach
- Tool result: `Interaction <id> resolved: denied`

---

## Test 7: Auto-resolve allowed tools

**What this tests**: Tools in the `allowed_tools` list are auto-approved without forwarding to parent.

**Steps**:
1. Launch a sub-agent with specific allowed tools: `Launch a ccagent to read all Python files. Allow Bash, Read, Glob, and Grep tools.`
2. The sub-agent uses Bash, Read, Glob, Grep during its work

**Expected Behavior**:
- No permission requests forwarded to parent for allowed tools
- The parent does NOT receive `permission_required` notifications for these tools
- The sub-agent works autonomously using only the allowed tools
- Log shows: `Auto-resolved interaction <id> (tool=<name>, allowed)`

---

## Test 8: Send follow-up message to sub-agent

**Steps**:
1. Launch a sub-agent
2. While it's running, ask the agent: `Send a message to the sub-agent telling it to also check for TODO comments`
3. The agent should call `message_agent` with the session ID and text

**Expected Behavior**:
- The message is submitted to the sub-agent's input queue via `session.submit(text, "user")`
- The sub-agent processes the follow-up message after its current work
- Tool result: `Message sent to sub-agent <session_id>`

---

## Test 9: Cancel a running sub-agent

**Steps**:
1. Launch a sub-agent with a long-running task
2. Ask the agent: `Cancel the background sub-agent`
3. The agent should call `cancel_agent` with the session ID

**Expected Behavior**:
- The sub-agent's CancellationToken is triggered via `session.interrupt()`
- The sub-agent's processing stops at the next checkpoint
- SubAgentDriver status changes to "interrupted"
- Parent receives an interruption notification
- Tool result: `Sub-agent <session_id> interrupted`

---

## Test 10: Observe a sub-agent session via /attach

**Steps**:
1. Launch a sub-agent, note its session ID
2. Type `/attach <sub_agent_session_id>`

**Expected Behavior**:
- History from the sub-agent session replays in the CLI
- Live events from the sub-agent stream in real-time (read-only)
- InteractionRequests are auto-resolved in the observer
- Use `/detach` to stop observing

---

## Test 11: Sub-agent with different agent types

**Scenario**: Launch a native agent sub-agent from a ccagent parent, or vice versa.

**Steps**:
1. Start with `python cc_main.py` (CCAgent as primary)
2. Ask: `Launch a native agent to list files in the current directory`

**Expected Behavior**:
- A native agent session is created (different agent type from parent)
- The sub-agent uses its own tool registry
- Communication flows correctly through the SubAgentDriver
- Completion notification received by parent

---

## Notes

- SubAgentDriver lives at `miniclaw/subagent_driver.py` (not in `channels/` to avoid circular imports)
- Session management tools (`launch_agent`, `reply_agent`, etc.) are registered via `create_registry(runtime_context=ctx)` — they only appear when a RuntimeContext is available
- Sub-agent sessions are regular sessions in `runtime.sessions` — they persist and can be restored
- The SubAgentDriver connection is transient (not persisted) — it exists only during the runtime session
