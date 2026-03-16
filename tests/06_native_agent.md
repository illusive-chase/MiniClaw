# Test: NativeAgent — tool loop, streaming, checkpoints

**Feature**: NativeAgent runs a stateless tool loop: build system prompt → provider.chat_stream() → if tool_calls, execute each → repeat → yield HistoryUpdate. Supports cancellation checkpoints, streaming TextDelta, and ActivityEvent for tool lifecycle.

**Architecture Spec**: §5.1 (Agent native)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

Ensure `config.yaml` has a working provider (OpenAI or Anthropic).

---

## Test 1: Text-only response (no tools)

**Steps**:
1. Send: `What is the capital of France?`

**Expected Behavior**:
- Response streams progressively: "Paris" (or similar)
- No activity footer (no tools invoked)
- Response completes and prompt returns
- Single iteration of the tool loop (text-only → return)

---

## Test 2: Single tool call

**Steps**:
1. Send: `List the files in the current directory`

**Expected Behavior**:
- Activity footer shows tool execution (e.g., `glob` or `shell`)
- Tool START event → footer shows running indicator
- Tool FINISH event → footer shows completion
- Agent incorporates tool result into final response
- Response includes file listing

---

## Test 3: Multi-tool iteration

**Steps**:
1. Send: `Read the file config.yaml and tell me what provider is configured`

**Expected Behavior**:
- Multiple tool calls in sequence (possibly glob to find file, then file_read)
- Activity footer tracks each tool independently
- Each tool shows START → FINISH cycle
- Agent produces a final text response summarizing the config
- Tool loop iterates multiple times before returning

---

## Test 4: Max iterations cap

**What this tests**: The tool loop respects `max_iterations` (default 30 in AgentConfig).

**Steps**:
1. Send a task that might trigger many tool calls: `Find all Python files in this project and count the lines in each one`
2. Monitor the activity footer

**Expected Behavior**:
- Tool calls proceed up to the max_iterations limit
- If the limit is reached, the agent returns with whatever it has
- No infinite loop

---

## Test 5: Cancellation checkpoints

**What this tests**: `token.check()` is called before `provider.chat()` and before `tool.execute()`.

**Steps**:
1. Send a task that triggers tool use: `Run the command: sleep 10`
2. Press `Ctrl+C` immediately after the tool starts (visible in footer)

**Expected Behavior**:
- The tool loop exits at the next checkpoint
- `[interrupted]` appears
- Session recovers cleanly

---

## Test 6: Streaming TextDelta during provider response

**Steps**:
1. Send: `Write a Python function to sort a list using merge sort, with detailed comments`
2. Watch the panel update

**Expected Behavior**:
- Text appears word-by-word or chunk-by-chunk (not all at once)
- The Rich Live panel re-renders progressively
- Code blocks render with proper markdown formatting

---

## Test 7: AskUserQuestion — single question with option selection

**What this tests**: NativeAgent yields an `InteractionRequest` when the LLM calls `AskUserQuestion`. The CLI channel renders options and the user's answer flows back as a tool result.

**Steps**:
1. Send: `I want to create a new file. Ask me what programming language I'd like to use.`
2. The agent should invoke `AskUserQuestion` with language options
3. A cyan "Agent Question" panel appears with numbered choices + "Other"
4. Type a number (e.g., `1`) and press Enter

**Expected Behavior**:
- The CLI renders a panel titled "Agent Question" with the question and numbered options
- After selecting an option, the agent continues and incorporates the choice into its response
- No `ActivityEvent` (START/FINISH) is emitted for `AskUserQuestion` — only the interaction prompt
- The prompt returns normally after the agent finishes

---

## Test 8: AskUserQuestion — "Other" free-text answer

**Steps**:
1. Send: `Help me pick a name for a variable. Ask me what the variable represents.`
2. When the question panel appears, choose the "Other" option (the last number)
3. Type a custom answer at the `Your answer:` prompt

**Expected Behavior**:
- The "Other" option is always listed as the last choice
- After typing a custom answer, the agent receives it and uses it in its response
- The custom text appears verbatim in the tool result sent back to the LLM

---

## Test 9: AskUserQuestion — agent continues tool loop after answer

**What this tests**: After `AskUserQuestion` resolves, the tool loop continues normally (the agent can call more tools in subsequent iterations).

**Steps**:
1. Send: `Ask me which file I want to read, then read it for me.`
2. Answer the question (e.g., pick or type `config.yaml`)
3. Observe that the agent then invokes `file_read` (or similar) as a follow-up tool call

**Expected Behavior**:
- AskUserQuestion fires first (interaction panel appears)
- After answering, the agent makes a second LLM call that includes a normal tool call
- Activity footer shows the follow-up tool's START → FINISH cycle
- Final response incorporates both the user's choice and the tool's output
