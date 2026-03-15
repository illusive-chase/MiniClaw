# Test: Session Interrupt — cooperative cancellation

**Feature**: CancellationToken is passed from Session to Agent. The agent checks at defined checkpoints (before provider.chat(), before tool.execute()). Ctrl+C triggers SIGINT → CLIListener → session.interrupt() → token.cancel() → CancelledError → InterruptedEvent.

**Architecture Spec**: §3.6 (Interrupt), §4.4 (CancellationToken), §9 (Interrupt Flow)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

---

## Test 1: Interrupt during text generation

**Steps**:
1. Send a prompt that produces a long response: `Write a detailed essay about the history of computing, at least 2000 words`
2. While the response is streaming, press `Ctrl+C`

**Expected Behavior**:
- The streaming stops within 1-2 seconds
- `[interrupted]` text appears at the end of the partial output
- The panel border changes to yellow (interrupt indicator)
- The prompt returns immediately — ready for next input
- No crash, no traceback in the terminal

---

## Test 2: Interrupt during tool execution

**Steps**:
1. Send: `Run the command: sleep 30` (or a long shell command)
2. While the tool is executing (activity footer shows the tool as running), press `Ctrl+C`

**Expected Behavior**:
- The tool execution is cancelled
- `[interrupted]` appears in the output
- The prompt returns — the session is not stuck
- The next message processes normally (session lock released)

---

## Test 3: Interrupt records prompt and marker in history

**Steps**:
1. Send: `My favorite color is blue`
2. Wait for response to complete
3. Send: `Write a very long poem about nature`
4. Press `Ctrl+C` during streaming
5. Send: `What is my favorite color?`

**Expected Behavior**:
- The response to step 5 should mention "blue"
- History from before the interrupt is preserved
- The interrupted prompt ("Write a very long poem about nature") is recorded in history as a user message
- An `[interrupted by user]` assistant message is appended immediately after the prompt
- The agent sees both entries in subsequent turns and knows what was attempted

---

## Test 4: Multiple rapid interrupts

**Steps**:
1. Send any message
2. Press `Ctrl+C` repeatedly (3-4 times quickly)

**Expected Behavior**:
- No crash or unhandled exception
- The session recovers cleanly
- Next message processes normally

---

## Test 5: Interrupt with no active processing

**Steps**:
1. At the idle prompt (no message being processed), press `Ctrl+C`

**Expected Behavior**:
- Graceful exit with "Goodbye!" message (CLIListener catches KeyboardInterrupt)
- OR the SIGINT handler sees `_current_token is None` and does nothing (no-op)
