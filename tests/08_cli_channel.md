# Test: CLIChannel — terminal rendering and interaction prompts

**Feature**: CLIChannel renders agent events to the terminal using Rich. It handles progressive markdown rendering, activity footer display, interaction prompts (permission, question, plan approval), interruption notices, and history replay.

**Architecture Spec**: §6.2 (CLIChannel)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

---

## Test 1: Progressive markdown rendering

**Steps**:
1. Send: `Write a markdown document with a heading, a list, and a code block`
2. Watch the panel

**Expected Behavior**:
- Rich Live panel with "Assistant" title and blue border
- Text appears progressively
- Markdown elements render correctly:
  - Headings are bold/styled
  - Lists have bullet points
  - Code blocks have syntax highlighting
- Panel stops updating when response is complete

---

## Test 2: Activity footer with tool tracking

**Steps**:
1. Send: `Read config.yaml and then list all Python files`
2. Watch the footer below the panel

**Expected Behavior**:
- Footer appears below the main panel
- Shows `Tools: X/Y done` counter
- Each tool has a status indicator:
  - `●` (yellow) — running, with elapsed time `[Xs]`
  - `✓` (green) — completed, with duration `[Xs...Ys]`
  - `✗` (red) — failed
- Tool names and summaries shown (e.g., `file_read("config.yaml")`)
- Footer updates in real-time

---

## Test 3: Interaction — permission prompt

**Command**:
```bash
python cc_main.py
```

**Steps**:
1. Send a task that requires file writes
2. Observe permission prompt

**Expected Behavior**:
- Live panel pauses (Live.stop())
- Yellow-bordered "Permission Request" panel appears
- Shows: Tool name, key arguments (file path, command, etc.)
- Prompt: `[1] Allow  [2] Deny`
- After response, Live resumes

---

## Test 4: Interaction — ask user question

**Steps** (CCAgent mode):
1. Trigger an ambiguous request that makes the agent ask a question
2. Observe the question prompt

**Expected Behavior**:
- Cyan-bordered "Agent Question" panel
- Question text in bold
- Numbered options
- Last option: "Other (type your answer)"
- After answering, processing continues

---

## Test 5: Interaction — plan approval

**Steps** (CCAgent mode):
1. Trigger plan mode (complex refactoring task)
2. Observe the plan panel

**Expected Behavior**:
- Green-bordered "Plan Review" panel with plan content in markdown
- Four options displayed:
  1. Yes, clear context and auto-accept edits
  2. Yes, auto-accept edits
  3. Yes, manually approve edits
  4. No, keep planning
- Option 4 prompts for feedback text

---

## Test 6: send() — simple text message

**What this tests**: `channel.send(text)` for notifications/errors.

**Steps**:
1. Use a slash command like `/reset`

**Expected Behavior**:
- A clean message appears: `Cleared X messages.`
- Uses `console.print(Panel(Markdown(text)))` internally
- No Live panel, no activity footer

---

## Test 7: replay() — history rendering

**Steps**:
1. Build history: send 2-3 messages
2. `/dump` to save
3. `/resume <session_id>` to restore

**Expected Behavior**:
- History replays in the terminal:
  - User messages: `You: <message text>`
  - Assistant messages: rendered in blue-bordered Panel with markdown
- After replay, the prompt is ready for new input
- Context from replayed history is available

---

## Test 8: InterruptedEvent rendering

**Steps**:
1. Send a long-running task
2. Press `Ctrl+C`

**Expected Behavior**:
- `[interrupted]` text appended to the buffer
- Panel border changes to yellow
- Live stops cleanly
