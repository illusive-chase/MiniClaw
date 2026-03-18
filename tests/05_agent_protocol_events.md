# Test: AgentProtocol & AgentEvent — typed event stream

**Feature**: All agent output flows through the `AgentEvent` union type. The event types are: TextDelta, ActivityEvent, InteractionRequest, HistoryUpdate, SessionControl, InterruptedEvent. Session routes them according to the spec's routing table.

**Architecture Spec**: §4.1 (AgentProtocol), §4.2 (AgentEvent Union), §4.5 (Supporting Data Types)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

---

## Test 1: TextDelta — progressive text rendering

**Steps**:
1. Send: `Explain what Python is in 3 sentences`
2. Watch the output panel

**Expected Behavior**:
- Text appears progressively (character by character or chunk by chunk)
- The Rich Live panel updates in real-time
- Final output is complete markdown-rendered text

---

## Test 2: ActivityEvent — tool lifecycle display

**Steps**:
1. Send: `What files are in the current directory?` (triggers glob or shell tool)
2. Watch the activity footer below the panel

**Expected Behavior**:
- Activity footer appears: `Tools: 0/1 done`
- Tool entry shows: `● tool_name("...")  [Xs]` (running, yellow dot)
- On completion: `✓ tool_name("...")  [Xs]` (green checkmark)
- Footer shows: `Tools: 1/1 done`
- Final text includes the file listing

---

## Test 3: InteractionRequest — permission prompt

**Command** (CCAgent mode):
```bash
python cc_main.py
```

**Steps**:
1. Send: `Create a file called test_hello.py with a hello world function`
2. When the permission prompt appears, observe the format

**Expected Behavior**:
- Live panel pauses
- A "Permission Request" panel appears with yellow border
- Shows tool name and key arguments (file path, command, etc.)
- Options: `[1] Allow  [2] Deny`
- Choosing `1` resumes processing; choosing `2` sends denial to agent

---

## Test 4: InteractionRequest — ask user question

**Command** (CCAgent mode):
```bash
python cc_main.py
```

**Steps**:
1. Send an ambiguous task: `Set up a web server` (the agent may ask clarifying questions)
2. Observe the interaction prompt

**Expected Behavior**:
- An "Agent Question" panel with magenta border appears
- Shows the question text and numbered options
- Last option is always "Other (type your answer)"
- Selecting a number returns that choice
- Selecting "Other" prompts for custom text input

---

## Test 5: Event routing — internal events never reach channel

**What this tests**: HistoryUpdate and SessionControl are consumed by Session and never forwarded.

**Steps**:
1. Send any message and observe all visible output
2. Check the log file (`.workspace/*.log`) for any SessionControl or HistoryUpdate references

**Expected Behavior**:
- No "HistoryUpdate" text appears in the CLI output
- No "SessionControl" text appears in the CLI output
- Only TextDelta (text), ActivityEvent (footer), InteractionRequest (prompts), and InterruptedEvent are visible
- Log file may show SessionControl handling for plan_execute scenarios
