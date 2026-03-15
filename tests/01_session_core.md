# Test: Session Core — process(), submit(), run(), history, event routing

**Feature**: Session is the central entity that owns conversation state. It accepts input via `submit()` (push-based queue) or `process()` (backward-compat wrapper). The `run()` async generator continuously pulls from the input queue. Internally, `_process()` acquires a per-session lock, calls the bound agent, intercepts internal events (HistoryUpdate, SessionControl), and forwards user-visible events to the channel.

**Architecture Spec**: §3 (Session), §3.3 (Input Queue Model), §3.4 (_process()), §4.2 (Event routing table)

---

## Prerequisites

```bash
cd mini-agent
pip install -e .
```

Ensure `config.yaml` has a valid provider configured (OpenAI or Anthropic with a working API key).

---

## Test 1: Basic message round-trip

**Command**:
```bash
python main.py
```

**Steps**:
1. Start the CLI agent
2. Type: `What is 2 + 2?`
3. Wait for the response
4. Quit

**Expected Behavior**:
- An "Assistant" panel renders with progressive markdown output
- The response contains "4" (or equivalent correct answer)
- After the response completes, the prompt returns for the next message
- No errors in the log file (check `.workspace/*.log`)
- Tokens usage is displayed after the response completes
- The session is automatically persisted (check `.workspace/.sessions/`)

---

## Test 2: History accumulation

**Steps**:
1. Start the CLI agent
2. Type: `My name is Alice`
3. Wait for response
4. Type: `What is my name?`

**Expected Behavior**:
- Second response should reference "Alice" — proves history is passed to the agent
- The agent receives the full conversation history on each `_process()` call

---

## Test 3: HistoryUpdate interception

**What this tests**: Session intercepts `HistoryUpdate` events from the agent and updates `self.history`. These events are never forwarded to the channel.

**Steps**:
1. Start the CLI agent
2. Send any message
3. Observe the output

**Expected Behavior**:
- No raw `HistoryUpdate` object appears in the CLI output
- Only `TextDelta` (rendered as markdown), `ActivityEvent` (tool status), and `InteractionRequest` (prompts) are visible
- After the response, the session's internal history has been updated (verifiable by asking a follow-up that references prior context)

---

## Test 4: Session lock serialization

**What this tests**: Only one message is processed at a time per session (asyncio.Lock in `_process()`).

**Steps**:
1. Start the CLI agent
2. Send a message that triggers tool use (e.g., `Read the file main.py`)
3. While the response is streaming, try to send another message

**Expected Behavior**:
- The second message waits until the first completes (the prompt is blocked during processing)
- No interleaved responses or race conditions
- The CLI only shows one prompt at a time

---

## Test 5: SessionControl — plan_execute restart

**What this tests**: When a `SessionControl("plan_execute")` event is yielded, Session clears history, resets the agent, and restarts with the plan content.

**Command**:
```bash
python cc_main.py
```

**Steps**:
1. Start the CCAgent (SDK-backed agent)
2. Send a complex task that triggers plan mode (e.g., `Create a Python script that calculates fibonacci numbers, with tests. Use Plan mode.`)
3. When the plan approval prompt appears, choose option `[1] Yes, clear context and auto-accept edits`

**Expected Behavior**:
- The agent enters plan mode and presents a plan
- After approval with option 1, the session clears history and restarts
- The agent begins executing the plan from scratch with fresh context
- Activity indicators show new tool executions

---

## Test 6: Input queue — submit() and run()

**What this tests**: The push-based input queue model. Messages submitted via `submit()` are consumed by `run()`.

**Steps**:
1. Start the CLI agent
2. Send a message normally

**Expected Behavior**:
- The CLIListener calls `session.submit(text, "user")` instead of `session.process(text)` directly
- A background consumer task runs `session.run()` which pulls from `_input_queue`
- The `_response_done` event fires when the response completes, re-enabling the prompt
- Behavior is identical to the old direct-call model from the user's perspective

---

## Test 7: Sub-agent notification injection

**What this tests**: When a sub-agent event arrives via `submit(source="sub_agent")`, Session injects a synthetic tool-call/result pair into history before processing.

**Steps**:
1. Start the CLI agent (with session management tools available)
2. Ask the agent to launch a background sub-agent (e.g., `Launch a ccagent to read main.py and summarize it`)
3. Wait for the sub-agent to complete or request permission

**Expected Behavior**:
- Sub-agent notifications appear as tool-call/result pairs in the parent session's history
- The parent agent can react to these notifications (e.g., approve permissions, acknowledge completion)
- The `_inject_sub_agent_notification()` method appends a `sub_agent_event` tool call and tool result to history

```
We are going to test subagent functionality. Launch a ccagent to call its AskUserQuestion tool for test and then summarize your choices. You are the main agent, that is able to interact with the subagent and to process subagent's request. Expected behavior: you can successfully (1) launch a ccagent to call its AskUserQuestion; (2) process its request; (3) see its output after it exits; (4) observe that the output is consistent with your choices. Now, let's start.
```
