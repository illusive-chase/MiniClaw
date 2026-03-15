# Test: Channel ABC — output endpoint protocol

**Feature**: Channel is the abstract base class for all output endpoints. It defines `send_stream()`, `send()`, `on_observe()` (with auto-resolve default), and `replay()`. Channels are agent-agnostic — they consume typed AgentEvent streams.

**Architecture Spec**: §6.1 (Channel ABC)

---

## Prerequisites

```bash
cd mini-agent
```

---

## Test 1: Channel interface completeness

**What this tests**: All Channel implementations provide the required methods.

**Verification** (code-level):
```python
from miniclaw.channels.cli import CLIChannel
from miniclaw.channels.feishu import FeishuChannel
from miniclaw.channels.pipe import PipeEnd

# All must be subclasses of Channel
from miniclaw.channels.base import Channel
assert issubclass(CLIChannel, Channel)
assert issubclass(FeishuChannel, Channel)
assert issubclass(PipeEnd, Channel)

# All must implement send_stream and send
for cls in [CLIChannel, FeishuChannel, PipeEnd]:
    assert hasattr(cls, 'send_stream')
    assert hasattr(cls, 'send')
    assert hasattr(cls, 'on_observe')
    assert hasattr(cls, 'replay')
```

**Expected Behavior**:
- All three channel implementations are valid Channel subclasses
- All required abstract methods are implemented

---

## Test 2: on_observe() default — auto-resolve interactions

**What this tests**: The default `on_observe()` wraps the stream to auto-resolve InteractionRequests.

**Steps** (observable via /attach):
1. Start CLI, create session A
2. `/dump` session A
3. Start a second session, `/attach <session_A_id>`
4. From another channel, send a message to session A that triggers an InteractionRequest

**Expected Behavior**:
- The primary channel (session A's) gets the InteractionRequest as a prompt
- The observer's `on_observe()` auto-resolves the interaction with `allow=True`
- Observer sees the event as an auto-allowed activity, not as an interactive prompt

---

## Test 3: replay() — optional method

**What this tests**: `replay()` renders past history when attaching or resuming.

**CLIChannel**: Renders user messages as text, assistant messages in panels
**FeishuChannel**: No-op (returns immediately)
**PipeEnd**: No replay (not applicable for pipes)

**Steps**:
1. Build history
2. `/dump`, then `/resume <id>`

**Expected Behavior**:
- CLIChannel shows full history replay
- FeishuChannel silently skips replay
- PipeEnd has no replay concept

---

## Test 4: log_handler() — optional logging hook

**What this tests**: Channels can optionally return a logging.Handler for log forwarding.

**Verification**:
```python
from miniclaw.channels.cli import CLIChannel
ch = CLIChannel()
handler = ch.log_handler()  # Returns None (base default)
```

**Expected Behavior**:
- Default returns `None`
- No channel currently overrides this (extensibility hook for future use)
