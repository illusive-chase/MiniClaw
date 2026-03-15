# Test: Session Core — process(), history, event routing

**Feature**: Session is the central entity that owns conversation state, acquires a per-session lock, calls the bound agent, intercepts internal events (HistoryUpdate, SessionControl), and forwards user-visible events to the channel.

**Architecture Spec**: §3 (Session), §3.3 (process()), §4.2 (Event routing table)

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
- The session is automatically dumped (check `.workspace/.session`)

---

## Test 2: History accumulation

**Steps**:
1. Start the CLI agent
2. Type: `My name is Alice`
3. Wait for response
4. Type: `What is my name?`

**Expected Behavior**:
- Second response should reference "Alice" — proves history is passed to the agent
- The agent receives the full conversation history on each `process()` call

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

**What this tests**: Only one message is processed at a time per session (asyncio.Lock).

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
2. Send a complex task that triggers plan mode (e.g., `Create a Python script that calculates fibonacci numbers, with tests`)
3. When the plan approval prompt appears, choose option `[1] Yes, clear context and auto-accept edits`

**Expected Behavior**:
- The agent enters plan mode and presents a plan
- After approval with option 1, the session clears history and restarts
- The agent begins executing the plan from scratch with fresh context
- Activity indicators show new tool executions
