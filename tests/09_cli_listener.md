# Test: CLIListener — REPL loop and slash commands

**Feature**: CLIListener runs the interactive REPL loop. It handles user input via prompt_toolkit, routes slash commands, installs SIGINT handler, and connects sessions to CLIChannel.

**Architecture Spec**: §7.2 (CLIListener)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

---

## Test 1: REPL startup

**Steps**:
1. Run `python main.py`

**Expected Behavior**:
- A "MiniClaw" panel appears with subtitle "type /help for commands"
- Green "You:" prompt appears
- Cursor is ready for input
- Input history file created at `.workspace/.cli_history`

---

## Test 2: /help command

**Steps**:
1. Type `/help`

**Expected Behavior**:
- A "Help" panel with cyan border lists all available commands:
  - /help, /reset, /sessions, /resume, /fork, /attach, /detach, /pipe
  - /model, /effort, /cost, /dump, /rename, /logging, /quit, /exit, /q

---

## Test 3: /reset — clear history

**Steps**:
1. Send a few messages to build history
2. Type `/reset`
3. Ask: `What did I just say?`

**Expected Behavior**:
- After `/reset`: `Cleared X messages.` (X = number of messages cleared)
- The follow-up question gets no context from prior messages
- Agent doesn't know what was said before the reset

---

## Test 4: /model — show and change model

**Steps**:
1. Type `/model` (no arguments)
2. Type `/model gpt-4o-mini` (or another valid model name)
3. Type `/model` again

**Expected Behavior**:
1. Shows: `Current model: <current_model>`
2. Shows: `Model set to: gpt-4o-mini`
3. Shows: `Current model: gpt-4o-mini`
- Subsequent messages use the new model

---

## Test 5: /effort — show and set thinking effort

**Steps**:
1. Type `/effort`
2. Type `/effort high`
3. Type `/effort invalid_level`

**Expected Behavior**:
1. Shows: `Current effort: medium` (or current setting)
2. Shows: `Effort set to: high`
3. Shows: `Valid effort levels: low, medium, high`

---

## Test 6: /dump and /sessions — save and list

**Steps**:
1. Send at least one message
2. Type `/dump`
3. Type `/sessions`

**Expected Behavior**:
- `/dump`: `Session saved: <session_id>`
- `/sessions`: A "Sessions" panel listing saved sessions with ID, name, and timestamp
- Sessions sorted by most recent first

---

## Test 7: /resume — restore a saved session

**Steps**:
1. Build history, use `/dump`
2. Note the session ID from `/sessions`
3. Type `/resume <session_id>`

**Expected Behavior**:
- `Resumed session <session_id>`
- History replays in the terminal (user and assistant messages)
- Context from the restored session is available
- Sending new messages continues the conversation

---

## Test 8: /rename — rename a session

**Steps**:
1. Type `/rename my-debug-session`

**Expected Behavior**:
- `Session renamed to: my-debug-session`
- After `/dump` and `/sessions`, the name appears in the session list

---

## Test 9: /cost — usage statistics

**Steps**:
1. Send a few messages
2. Type `/cost`

**Expected Behavior**:
- Shows token counts: `tokens: X,XXX (X,XXXin + X,XXXout)`
- If cost tracking is available: `cost: $X.XXXX`
- Or: `No usage data available.` if the agent doesn't support usage tracking

---

## Test 10: /logging — change log level

**Steps**:
1. Type `/logging`
2. Type `/logging DEBUG`
3. Type `/logging POTATO`

**Expected Behavior**:
1. Shows: `Current console log level: WARNING` (or current level)
2. Shows: `Console log level set to: DEBUG`
3. Shows: `Valid levels: DEBUG, INFO, WARNING, ERROR`

---

## Test 11: /quit, /exit, /q — exit

**Steps**:
1. Type `/quit` (or `/exit` or `/q`)

**Expected Behavior**:
- `Goodbye!` message
- Process exits cleanly

---

## Test 12: Unknown command

**Steps**:
1. Type `/foobar`

**Expected Behavior**:
- `Unknown command: /foobar. Type /help for available commands.`

---

## Test 13: Empty input

**Steps**:
1. Press Enter without typing anything

**Expected Behavior**:
- Prompt returns immediately, no message sent
- No errors

---

## Test 14: Bare "quit"/"exit" text

**Steps**:
1. Type `quit` or `exit` (without slash)

**Expected Behavior**:
- `Goodbye!` message and clean exit (CLIListener treats these as exit commands)
