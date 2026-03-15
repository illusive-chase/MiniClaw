# Test: FeishuChannel — Feishu/Lark message rendering

**Feature**: FeishuChannel is an output-only channel that collects TextDelta chunks, auto-resolves all InteractionRequests, and sends the final text via the Feishu/Lark API.

**Architecture Spec**: §6.3 (FeishuChannel)

---

## Prerequisites

- A Feishu/Lark app with valid `app_id` and `app_secret`
- The app has `im:message:send` permission
- A test chat/group where the bot is a member

```bash
cd mini-agent
```

Configure `config.yaml`:
```yaml
channel:
  type: feishu

feishu:
  app_id: ${FEISHU_APP_ID}
  app_secret: ${FEISHU_APP_SECRET}
```

---

## Test 1: Basic text response

**Steps**:
1. Start the Feishu listener
2. Send a text message to the bot in Feishu: `Hello, what can you do?`

**Expected Behavior**:
- Bot replies with a text message in the same chat
- Reply is the complete agent response (all TextDelta chunks concatenated)
- No partial/streaming messages (Feishu sends final text only)

---

## Test 2: Auto-resolve interactions

**What this tests**: FeishuChannel auto-resolves all InteractionRequests with `allow=True`.

**Steps**:
1. Send a task that would normally trigger a permission prompt: `Write a file called test.txt`
2. Observe the response

**Expected Behavior**:
- No interactive prompt appears in Feishu (no buttons, no card)
- The agent proceeds with the action auto-approved
- Response confirms the action was taken

---

## Test 3: InterruptedEvent handling

**Steps**:
1. Send a message and then immediately send another (if the agent is still processing)
2. Or simulate an interrupt scenario

**Expected Behavior**:
- If interrupted, `[interrupted]` text is appended to the response
- The partial text is sent to Feishu

---

## Test 4: Empty response handling

**Steps**:
1. Send a message where the agent produces no text (e.g., only tool activity)

**Expected Behavior**:
- If `full_text` is empty after stripping, no message is sent to Feishu
- No error in the bot

---

## Test 5: Reply threading

**Steps**:
1. Send a message in a Feishu group
2. Observe the reply

**Expected Behavior**:
- If `reply_to` (message_id) is set, the bot replies as a thread reply to the original message
- If no `reply_to`, the bot sends a new message to the chat_id

---

## Test 6: API error handling

**What this tests**: Feishu API failure logging.

**Steps**:
1. Temporarily use invalid credentials or permissions
2. Send a message

**Expected Behavior**:
- Error logged: `Failed to send Feishu message: <code> - <msg>`
- No crash; the channel handles the error gracefully

---

## Test 7: Session per sender

**Steps**:
1. Have two different Feishu users send messages to the bot

**Expected Behavior**:
- Each user gets their own session (identified by `sender_id = feishu:<open_id>`)
- Conversations are independent — user A's context doesn't leak to user B
- Subsequent messages from the same user continue their session
