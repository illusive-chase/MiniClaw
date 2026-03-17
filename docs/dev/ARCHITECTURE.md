# MiniClaw Architecture Spec

## 1. Design Principles

| # | Principle | Implication |
|---|-----------|-------------|
| 1 | **Session is the nexus** | Session owns conversation state. Everything else (agent, channel) binds to it. |
| 2 | **Agent-channel agnosticism** | Agent produces typed events. Channel consumes them. Neither knows about the other. |
| 3 | **Listener/Channel split** | Listener = input routing. Channel = output rendering. Separate concerns. |
| 4 | **SubAgentDriver-as-Channel** | Background sub-agent communication uses the same Channel abstraction. SubAgentDriver acts as Channel for the child and notifier for the parent. |
| 5 | **Typed event stream** | All agent output flows through `AgentEvent` union. Session intercepts internal events, forwards the rest. |
| 6 | **Cooperative interrupts + signals** | SignalToken (extends CancellationToken) passed from Session to Agent. Agents check at defined checkpoints. Sub-agent notifications are delivered mid-turn via the signal queue. |
| 7 | **Extensible via protocol** | New agents, channels, listeners implement protocols and register with Runtime. |
| 8 | **Input queue model** | Session accepts messages via `submit()` queue, consumed by `run()`. Enables multi-source input (user + sub-agent notifications). |

---

## 2. Component Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            RUNTIME                                       │
│                                                                          │
│  Supervises      ┌──────────────┐   ┌──────────────┐                   │
│  listeners:      │ CLIListener   │   │FeishuListener │                   │
│                  │ (REPL loop)   │   │(WebSocket +   │                   │
│                  │  submit()     │   │ submit())     │                   │
│                  └──────┬───────┘   └──────┬────────┘                   │
│                         │                  │                             │
│  Routes to      ┌──────▼──────────────────▼─────────────────────┐      │
│  sessions:      │              SESSION REGISTRY                   │      │
│                 │                                                  │      │
│                 │  session_A ── agent: native,  ch: CLI            │      │
│                 │               runtime_context: RuntimeContext_A  │      │
│                 │  session_B ── agent: ccagent, ch: Feishu         │      │
│                 │               observers: [CLI]                   │      │
│                 │  session_C ── agent: ccagent, ch: SubAgentDriver │      │
│                 │               (background sub-agent of A)        │      │
│                 └──────────────────────────────────────────────────┘      │
│                                                                          │
│  Manages:        session lifecycle (create, fork, attach, persist)       │
│                  agent registry ("native" -> factory, "ccagent" -> ...)  │
│                  two-phase session init (Session → RuntimeContext → Agent)│
│                  auto-persist on history update                          │
│                  listener supervision (restart with backoff)             │
│                  graceful shutdown (drain + persist)                     │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Session

Session is the central entity. It owns conversation state and coordinates agent execution with channel delivery.

### 3.1 Structure

```
Session
├── id: str                            # "20260315_181530_abc123"
├── metadata: SessionMetadata
│   ├── created_at: str                # ISO 8601
│   ├── updated_at: str                # ISO 8601
│   ├── name: str | None
│   ├── forked_from: str | None        # source session id
│   └── tags: dict[str, str]
│
├── history: list[ChatMessage]         # OWNED — portable, serializable
├── agent_config: AgentConfig          # model, system_prompt, tools, etc.
│
├── agent: AgentProtocol               # BOUND by Runtime, not owned
├── primary_channel: Channel | None    # who can send input
├── observers: list[ObserverBinding]   # read-only watchers
├── on_history_update: Callable | None # auto-persist callback, set by Runtime
│
├── _input_queue: asyncio.Queue[InputMessage]  # push-based input
├── runtime_context: RuntimeContext | None     # bridge to Runtime for sub-agents
│
├── _lock: asyncio.Lock                # one message at a time
├── _current_token: SignalToken | None  # cancellation + signal queue
└── status: str                        # "active" | "paused" | "archived"
```

### 3.2 Ownership Rules

- Session **owns**: `history`, `agent_config`, `metadata`, `status`, `_input_queue`
- Session **borrows**: `agent` (bound by Runtime), `primary_channel` (bound by Listener), `runtime_context` (set by Runtime)
- Session **does NOT own**: Channel lifecycle, Agent lifecycle, persistence

This separation enables fork (copy state, rebind to different agent/channel) and attach (add observer channel without touching state).

### 3.3 Input Queue Model

Session uses a push-based input queue. Messages are submitted via `submit()` and consumed by `run()`.

```python
@dataclass
class InputMessage:
    text: str
    source: str = "user"    # "user" | "sub_agent" | "system"
    metadata: dict | None = None

def submit(self, text, source="user", metadata=None) -> None:
    """Non-blocking enqueue."""
    self._input_queue.put_nowait(InputMessage(text, source, metadata))

async def run(self) -> AsyncIterator[tuple[AsyncIterator[AgentEvent], str]]:
    """Continuous loop: pull from queue, yield (stream, source) pairs."""
    while True:
        msg = await self._input_queue.get()
        if msg.source == "sub_agent" and msg.metadata:
            process_text = self._format_sub_agent_message(msg.metadata)
        else:
            process_text = msg.text
        stream = self._process(process_text)
        yield stream, msg.source
```

This enables:
- **Multi-source input**: user messages, sub-agent notifications, and system events all flow through the same queue
- **Notification formatting**: sub-agent events are formatted as descriptive user-role text before processing
- **Decoupled consumption**: Listeners own the `run()` consumer loop, not Session

### 3.4 Core Method: `_process()`

```python
async def _process(self, text: str) -> AsyncIterator[AgentEvent]:
    """Internal: process a single message through the bound agent.

    Yields: TextDelta, ActivityEvent, InteractionRequest, InterruptedEvent, UsageEvent.
    Consumes internally: HistoryUpdate, SessionControl.
    """
    token = CancellationToken()
    self._current_token = token
    interrupted_text = None

    try:
        async with self._lock:
            pending_text = text

            # Restart loop: handles SessionControl("plan_execute")
            while pending_text is not None:
                interrupted_text = pending_text
                restart_text = None

                async for event in self.agent.process(
                    pending_text, list(self.history), self.agent_config, token
                ):
                    if isinstance(event, HistoryUpdate):
                        self.history = event.history
                        self.metadata.touch()
                        if self.on_history_update is not None:
                            self.on_history_update()

                    elif isinstance(event, SessionControl):
                        if event.action == "plan_execute":
                            self.history = []
                            await self.agent.reset()
                            restart_text = event.payload.get(
                                "plan_content", "Execute the plan."
                            )
                            break

                    else:
                        # Forward to caller + broadcast to observers
                        await self._broadcast(event)
                        yield event

                pending_text = restart_text

    except CancelledError:
        # Record interrupted prompt + marker in history
        if interrupted_text is not None:
            self.history.append(ChatMessage(role="user", content=interrupted_text))
            self.history.append(
                ChatMessage(role="assistant", content="[interrupted by user]")
            )
            self.metadata.touch()
            if self.on_history_update is not None:
                self.on_history_update()
        event = InterruptedEvent(partial_history=list(self.history))
        await self._broadcast(event)
        yield event

    finally:
        # Flush any undelivered signals to the input queue
        if self._current_token is not None:
            remaining = self._current_token.drain()
            for sig in remaining:
                self.submit(text=sig.payload, source=sig.source or "sub_agent",
                            metadata=sig.metadata)
        self._current_token = None
```

The backward-compat `process()` wrapper delegates to `_process()` directly (bypasses queue).

The signal flush ensures that any sub-agent notifications that arrived during the turn but were not consumed by the agent (e.g., because the turn ended before the next checkpoint) are re-queued to the input queue for processing on the next turn.

### 3.5 Sub-Agent Message Formatting

When a sub-agent event arrives via the input queue, `_format_sub_agent_message()` converts the metadata into a descriptive user-role text message:

```python
@staticmethod
def _format_sub_agent_message(metadata: dict) -> str:
    """Build a single user-role message from sub-agent notification metadata."""
    event_type = metadata.get("event_type", "")
    session_id = metadata.get("session_id", "unknown")

    if event_type == "permission_required":
        interaction_id = metadata.get("interaction_id", "")
        tool_name = metadata.get("tool_name", "")
        notification_text = metadata.get("notification_text", "")
        return (
            f"[Sub-agent notification] session_id={session_id}\n"
            f"Permission required — interaction_id={interaction_id}, "
            f"tool={tool_name}\n"
            f"tool_input: {notification_text}\n"
            f"Use reply_agent to allow/deny. "
            f"For AskUserQuestion, include answers."
        )

    if event_type == "turn_complete":
        notification_text = metadata.get("notification_text", "")
        return (
            f"[Sub-agent notification] session_id={session_id}\n"
            f"Turn complete. Response:\n{notification_text}\n"
        )

    # Fallback
    notification_text = metadata.get("notification_text", "Sub-agent event.")
    return f"[Sub-agent notification] session_id={session_id}\n{notification_text}"
```

This formatted text is then processed by the agent as a regular user message, allowing it to see sub-agent events and react accordingly (e.g., calling `reply_agent` to approve a permission).

### 3.6 Interrupt

```python
def interrupt(self) -> None:
    """Cancel the current in-progress processing."""
    if self._current_token:
        self._current_token.cancel()
```

Called by Channel/Listener when user sends Ctrl+C or `/stop`.

### 3.7 Observer Broadcasting

```python
async def _broadcast(self, event: AgentEvent) -> None:
    """Push event to all observer channels."""
    for binding in self.observers:
        try:
            binding.queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # drop event rather than block
        except Exception:
            pass  # observer failure doesn't affect primary
```

Each observer has an `ObserverBinding`:

```python
@dataclass
class ObserverBinding:
    channel: Channel
    queue: asyncio.Queue[AgentEvent]  # maxsize=1000
    task: asyncio.Task | None = None
```

### 3.8 Fork

Fork is handled by **Runtime** (§8.2), not by Session, because forking
requires creating a new agent via the agent registry and registering the
new session — both of which are Runtime responsibilities. Session does
not own agent lifecycle (§3.2).

See `Runtime.fork_session()` in §8.2 for the full implementation.

### 3.9 Attach / Detach Observer

```python
def attach_observer(self, channel: Channel) -> ObserverBinding:
    queue = asyncio.Queue(maxsize=1000)

    async def _observer_loop():
        await channel.replay(self.history)
        await channel.on_observe(_queue_iter(queue))

    task = asyncio.create_task(_observer_loop())
    binding = ObserverBinding(channel=channel, queue=queue, task=task)
    self.observers.append(binding)
    return binding

def detach_observer(self, channel: Channel) -> None:
    for binding in self.observers:
        if binding.channel is channel:
            if binding.task is not None:
                binding.task.cancel()
            self.observers.remove(binding)
            break
```

---

## 4. AgentProtocol

### 4.1 Protocol Definition

```python
@runtime_checkable
class AgentProtocol(Protocol):
    @property
    def agent_type(self) -> str: ...       # "native", "ccagent"

    @property
    def default_model(self) -> str: ...

    async def process(
        self,
        text: str,
        history: list[ChatMessage],
        config: AgentConfig,
        token: CancellationToken,
    ) -> AsyncIterator[AgentEvent]: ...

    async def reset(self) -> None: ...
    async def shutdown(self) -> None: ...

    def serialize_state(self) -> dict: ...
    async def restore_state(self, state: dict) -> None: ...
    async def on_fork(self, source_state: dict) -> dict: ...
```

### 4.2 AgentEvent Union

```python
AgentEvent = Union[
    TextDelta,            # str chunk — progressive display
    ActivityEvent,        # tool/subagent lifecycle (START/FINISH/FAILED/PROGRESS)
    InteractionRequest,   # permission/question/plan — blocks until resolved
    HistoryUpdate,        # updated history — session-internal, never forwarded
    SessionControl,       # control commands — session-internal, never forwarded
    InterruptedEvent,     # processing was interrupted — forwarded to channel
    UsageEvent,           # cumulative token usage — forwarded to channel
]
```

**Routing in Session._process():**

| Event | Forwarded to Channel? | Forwarded to Observers? | Session action |
|-------|----------------------|------------------------|----------------|
| TextDelta | Yes | Yes | — |
| ActivityEvent | Yes | Yes | — |
| InteractionRequest | Yes (primary resolves) | Yes (display only, auto-resolve) | — |
| HistoryUpdate | No | No | `self.history = event.history`, `on_history_update()` |
| SessionControl | No | No | Handle action (e.g., plan_execute restart) |
| InterruptedEvent | Yes | Yes | — |
| UsageEvent | Yes | Yes | — |

### 4.3 AgentConfig

```python
@dataclass
class AgentConfig:
    model: str = ""
    system_prompt: str = ""
    tools: list[str] | None = None          # allowed tool names (None = all)
    max_iterations: int = 30                # tool loop cap
    memory_enabled: bool = True
    thinking: bool = False
    effort: str = "medium"                  # "low" / "medium" / "high"
    temperature: float = 0.7
    extra: dict = field(default_factory=dict)  # agent-type-specific overrides

    # Sub-agent spawn limits (None = unlimited)
    max_concurrent_agents: int | None = None   # hard block at N running
    max_total_spawns: int | None = None        # hard limit on total spawns per session
    spawn_warn_threshold: int | None = None    # soft warning threshold
```

### 4.4 SignalToken (CancellationToken)

```python
class SignalType(Enum):
    CANCEL = "cancel"
    NOTIFICATION = "notification"
    INJECT = "inject"          # future: user "btw" mid-turn

@dataclass
class Signal:
    type: SignalType
    payload: str = ""
    source: str = ""           # "user" | "sub_agent" | "system"
    metadata: dict | None = None

class SignalToken:
    """Cooperative cancellation + signal queue passed from Session to Agent.

    Extends the original CancellationToken with a deque-based signal queue
    so sub-agent notifications can be delivered mid-turn.
    """

    def cancel(self) -> None:
        """Signal cancellation."""

    @property
    def is_cancelled(self) -> bool: ...

    def check(self) -> None:
        """Raise CancelledError if cancelled.

        Agents call this at checkpoints:
          1. Before each provider.chat() call
          2. Before each tool.execute() call
          3. (Optional) Between stream chunks
        """

    def send(self, signal: Signal) -> None:
        """Enqueue a signal for the agent to pick up at the next checkpoint."""

    def drain(self, types: set[SignalType] | None = None) -> list[Signal]:
        """Remove and return queued signals, optionally filtered by type."""

    @property
    def has_pending(self) -> bool: ...

CancellationToken = SignalToken  # backward-compat alias
```

**Signal delivery flow:**
1. Sub-agent completes a turn → driver calls `_notify_parent()`
2. If parent is mid-turn (`_current_token is not None`): enqueue via `token.send(Signal(...))`
3. If parent is idle: fall back to `parent.submit()` (dequeued on next turn)
4. Agent drains signals at each checkpoint and injects them as user-role messages
5. On turn end, `Session._process()` flushes remaining signals back to the input queue

### 4.5 Supporting Data Types

```python
@dataclass
class TextDelta:
    text: str

@dataclass
class HistoryUpdate:
    history: list[ChatMessage]

@dataclass
class SessionControl:
    action: str              # "plan_execute"
    payload: dict            # {"plan_content": "...", "permission_mode": "..."}

@dataclass
class InterruptedEvent:
    partial_history: list[ChatMessage] | None = None

@dataclass
class UsageEvent:
    usage: UsageStats        # cumulative token counts and cost
```

ActivityEvent and InteractionRequest retain their existing definitions from the current codebase.

---

## 5. Agent Implementations

### 5.1 Agent (native)

```
NativeAgent.process(text, history, config, token):
│
├─ Build system prompt (config.system_prompt + tool list + memory context)
├─ Build messages: [system, *history, user(text)]
├─ tool_specs = registry.all_specs()
├─ turn_usage = UsageStats()
│
├─ TOOL LOOP (max config.max_iterations):
│   ├─ token.check()                          ← checkpoint 1
│   ├─ _drain_and_inject_signals(token, msgs) ← inject sub-agent notifications
│   ├─ provider.chat_stream(messages, specs, config.model, config.temperature)
│   │   ├─ str chunks → yield TextDelta (with block-separation logic)
│   │   └─ ChatResponse → response
│   │
│   ├─ turn_usage.accumulate_token_usage(response.usage)
│   │
│   ├─ if response.tool_calls:
│   │   ├─ append assistant message to messages
│   │   ├─ for each tool_call:
│   │   │   ├─ token.check()                  ← checkpoint 2
│   │   │   ├─ yield ActivityEvent(TOOL, START)
│   │   │   ├─ result = tool.execute(args)
│   │   │   ├─ append tool result to messages
│   │   │   └─ yield ActivityEvent(TOOL, FINISH|FAILED)
│   │   └─ continue loop
│   │
│   └─ else (text-only):
│       └─ break
│
├─ Build updated history:
│   updated = [*history, user_msg, *new_messages, assistant(reply)]
├─ yield UsageEvent(turn_usage)
├─ yield HistoryUpdate(updated)
│
├─ Stateless properties:
│   reset()          → no-op
│   serialize_state() → {}
│   restore_state()  → no-op
│   on_fork()        → {}
│   shutdown()       → no-op
```

The tool registry may include session management tools (`launch_agent`, `reply_agent`, etc.) if a `RuntimeContext` was provided during creation.

### 5.2 CCAgent (SDK-backed)

```
CCAgent.process(text, history, config, token):
│
├─ sdk_session_id = self._sdk_session_id or _extract_sdk_session_id(history)
├─ options = _build_options(sdk_session_id, model, client_key)
├─ output_queue = asyncio.Queue()
├─ turn_usage = UsageStats()
│
├─ BACKGROUND (_run_sdk):
│   ├─ async with ClaudeSDKClient(options) as client:
│   │   ├─ client.query(text)
│   │   └─ async for message in client.receive_response():
│   │       └─ output_queue.put(("sdk", message))
│   └─ output_queue.put(("done", None))
│
├─ _make_can_use_tool callback (bound to client_key):
│   ├─ Determine InteractionType:
│   │   ├─ AskUserQuestion   → ASK_USER
│   │   ├─ ExitPlanMode      → PLAN_APPROVAL
│   │   └─ Other             → PERMISSION
│   ├─ Create InteractionRequest with asyncio Future
│   ├─ output_queue.put(("interaction", request))
│   ├─ await future                       ← blocks SDK
│   ├─ If PLAN_APPROVAL + clear_context:
│   │   └─ output_queue.put(("plan_action", ...)) + return Deny(interrupt=True)
│   └─ Return Allow or Deny based on response
│
├─ MAIN LOOP (consume output_queue):
│   ├─ "sdk":
│   │   ├─ SystemMessage(init) → capture session_id
│   │   ├─ TaskStartedMessage  → yield ActivityEvent(AGENT, START)
│   │   ├─ TaskProgressMessage → yield ActivityEvent(AGENT, PROGRESS)
│   │   ├─ TaskNotificationMsg → yield ActivityEvent(AGENT, FINISH|FAILED)
│   │   ├─ AssistantMessage:
│   │   │   ├─ TextBlock    → yield TextDelta (with block-separation logic)
│   │   │   ├─ ToolUseBlock → yield ActivityEvent(TOOL, START)
│   │   │   └─ ThinkingBlock → ignored
│   │   ├─ UserMessage:
│   │   │   └─ ToolResultBlock → yield ActivityEvent(TOOL, FINISH|FAILED)
│   │   └─ ResultMessage → turn_usage.accumulate(message)
│   ├─ "interaction"  → yield InteractionRequest
│   ├─ "plan_action"  → yield SessionControl("plan_execute", payload); break
│   ├─ "done"         → break
│   └─ "error"        → raise
│
├─ Track SDK session ID: self._sdk_session_id = new_session_id
├─ Build visible history (strip session markers)
├─ Inject session marker for SDK session tracking:
│   [system("__cc_session__:<id>"), ...visible_history, user, assistant]
├─ yield UsageEvent(turn_usage)
├─ yield HistoryUpdate(updated_history)
│
├─ Stateful properties:
│   reset()           → clear _sdk_session_id
│   serialize_state() → {"sdk_session_id": self._sdk_session_id}
│   restore_state()   → self._sdk_session_id = state["sdk_session_id"]
│   on_fork()         → {} (fresh SDK, don't reuse session)
│   shutdown()        → no-op
```

**SDK session markers:** CCAgent persists the SDK session ID by injecting a `system` message with prefix `__cc_session__:` into the history. On restore, it extracts the session ID from this marker to resume the SDK session.

---

## 6. Channel

### 6.1 Channel ABC

```python
class Channel(ABC):
    """Output endpoint — renders agent events to a transport."""

    @abstractmethod
    async def send_stream(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Consume and render a full agent event stream.

        Handles:
          TextDelta           → progressive rendering
          ActivityEvent       → status display (footer, indicators)
          InteractionRequest  → prompt user, call request.resolve()
          InterruptedEvent    → display interruption notice
          UsageEvent          → display token usage stats
        """

    @abstractmethod
    async def send(self, text: str) -> None:
        """Send a simple text message (notifications, errors)."""

    async def on_observe(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Render events as read-only observer.

        Default: same as send_stream but auto-resolve interactions.
        Override for custom observer UX.
        """
        async def _auto_resolve(source):
            async for event in source:
                if isinstance(event, InteractionRequest):
                    event.resolve(InteractionResponse(id=event.id, allow=True))
                else:
                    yield event
        await self.send_stream(_auto_resolve(stream))

    async def replay(self, history: list[ChatMessage]) -> None:
        """Replay past history when attaching/resuming. Optional."""
        pass

    def log_handler(self) -> logging.Handler | None:
        """Return this channel's log forwarding handler, or None."""
        return None
```

### 6.2 CLIChannel

```
CLIChannel
├── _console: Rich Console
│
├── send_stream(stream):
│   ├─ Start Rich Live panel (8 fps)
│   ├─ Show spinner: "Thinking..."
│   ├─ async for event in stream:
│   │   ├─ TextDelta        → buffer += text, re-render Markdown panel
│   │   ├─ ActivityEvent    → tracker.apply(event), update footer
│   │   │   (ActivityFooter shows: "Tools: 2/5 done [3s]" + per-tool status)
│   │   ├─ InteractionRequest:
│   │   │   ├─ Pause Live
│   │   │   ├─ Render interaction panel (dispatch by type):
│   │   │   │   ├─ PERMISSION:    show tool/command, [1] Allow [2] Deny
│   │   │   │   ├─ ASK_USER:      show questions + numbered options
│   │   │   │   └─ PLAN_APPROVAL: show plan, 4 options (clear+accept, accept, manual, reject)
│   │   │   ├─ event.resolve(response)
│   │   │   └─ Resume Live
│   │   ├─ UsageEvent       → append "tokens: N (Xin + Yout)" to buffer
│   │   └─ InterruptedEvent → render "[interrupted]"
│   └─ Stop Live, final render
│
├── send(text):
│   └─ console.print(Panel(Markdown(text)))
│
├── replay(history):
│   └─ For each message: render role + content in panel
│
└── on_observe(stream):
    ├─ Custom implementation (NOT same as send_stream):
    │   auto-resolve interactions, lazy Live display
    │   (Live only created when events arrive, stopped on turn end)
    │   Prevents "infinite Thinking..." when idle
    └─ UsageEvent/InterruptedEvent → stop Live for that turn

Note: User input (prompt) is handled by CLIListener (§7.2), not
CLIChannel. Channel is an output-only endpoint (Principle #3).
```

### 6.3 FeishuChannel

```
FeishuChannel(client, chat_id, reply_to="")
├── _client: lark_oapi.Client            # shared client (async API methods)
├── _chat_id: str                        # target chat
├── _reply_to: str                       # message_id for thread replies ("" if none)
├── _sent_message_id: str | None         # for progressive updates
│
├── send_stream(stream):
│   ├─ Collect TextDelta chunks into buffer
│   ├─ For InteractionRequests: auto-resolve (allow=True)
│   │   (no interactive card UI — bot has no callback endpoint for buttons)
│   ├─ For ActivityEvents: silently consumed
│   ├─ For InterruptedEvent: append "[interrupted]"
│   ├─ Debounced progressive update:
│   │   ├─ First substantial text → _send_text() as interactive card, store _sent_message_id
│   │   └─ Every ~3s of new text → _patch_message() with accumulated text
│   └─ Final: patch complete message (or send if none sent yet)
│
├── send(text):
│   └─ _send_text(text)
│
├── replay(history):
│   └─ No-op (silent resume)
│
├── _build_card(text) → str:
│   └─ JSON interactive card wrapping text as markdown element
│       (Feishu PATCH API only supports interactive cards)
│
├── _send_text(text) → str | None:
│   ├─ Builds interactive card via _build_card()
│   ├─ If _reply_to: ReplyMessageRequest → thread reply (areply)
│   ├─ Else: CreateMessageRequest → new message to chat (acreate)
│   ├─ msg_type = "interactive" (card, not plain text)
│   └─ Returns message_id on success for progressive updates
│
└── _patch_message(message_id, text):     # for progressive updates
    └─ PatchMessageRequest via async apatch with interactive card content
```

**Async API methods:** The `lark_oapi` client provides async variants of its
REST methods — `message.areply()`, `message.acreate()`, `message.apatch()` —
which use `Transport.aexecute()` internally. These are proper coroutines,
eliminating the need for `run_in_executor`.

**Progressive updates:** The stream consumer sends an initial message on first
substantial text, then debounce-patches every ~3 seconds as new text arrives.
The final patch ensures the complete text is displayed. For short responses,
only one message is sent.

**Interactive cards:** All messages are sent as interactive cards (`msg_type="interactive"`)
rather than plain text, because the Feishu PATCH API only supports updating
interactive card messages.

### 6.4 SubAgentDriver

SubAgentDriver is a dual-role component: Channel for a child session and notifier for a parent session. It lives at `miniclaw/subagent_driver.py` (not in `channels/` to avoid circular imports).

```
SubAgentDriver(session_id, parent_session, child_session)
├── _session_id: str
├── _parent_session: Session          # receives notifications
├── _child_session: Session           # drives this session
├── _pending_interactions: dict       # interaction_id -> InteractionRequest
├── _status: str                      # running | completed | failed | interrupted
├── _result: str | None               # final text output
├── _task: asyncio.Task | None
├── _done: asyncio.Event              # set when _run() exits (for wait_agent)
│
├── send_stream(stream):              # Channel interface
│   ├─ Collect TextDelta chunks → buffer
│   ├─ InteractionRequest:
│   │   └─ Store in _pending_interactions, notify parent
│   │     (all interactions forwarded — no auto-resolution)
│   ├─ InterruptedEvent → set status=interrupted, notify parent
│   ├─ UsageEvent → silently consumed
│   ├─ ActivityEvent → silently consumed
│   └─ Capture final text as result
│
├── send(text): no-op
│
├── _handle_interaction(request):
│   ├─ Store in _pending_interactions
│   └─ _notify_parent("permission_required", tool_input_json,
│        extra={session_id, interaction_id, tool_name})
│
├── resolve_interaction(id, action, reason, answers):
│   ├─ Pop from _pending_interactions
│   ├─ Build InteractionResponse (allow/deny, optional answers for ASK_USER)
│   └─ request.resolve(response) → unblocks child agent
│
├── _notify_parent(event_type, text, extra):
│   ├─ If parent._current_token is not None (parent mid-turn):
│   │   └─ token.send(Signal(NOTIFICATION, text, "sub_agent", metadata))
│   └─ Else (parent idle):
│       └─ parent.submit(text, source="sub_agent", metadata={event_type, ...})
│
├── start():
│   └─ asyncio.create_task(_run())
│
├── _run():                           # background loop
│   ├─ try:
│   │   ├─ async for stream, source in child_session.run():
│   │   │   ├─ await self.send_stream(stream)
│   │   │   └─ if result: _notify_parent("turn_complete", result)
│   │   └─ On clean exit: set status=completed
│   │   └─ On error: set status=failed, notify parent("failed")
│   └─ finally: self._done.set()
│
├── status → str (property)
├── result → str | None (property)
└── pending_interaction_ids() → list[str]
```

**Key behaviors:**
- All InteractionRequests are forwarded to the parent session via `_notify_parent("permission_required", ...)`
- **Dual delivery:** If the parent is mid-turn, notifications go to the `SignalToken` signal queue (immediate). If idle, they go to the session input queue (next turn).
- Parent agent sees the notification as a formatted text message (§3.5), calls `reply_agent` tool
- `resolve_interaction()` resolves the InteractionRequest's `_future`, unblocking the child agent's `can_use_tool` callback
- On each turn completion, notifies parent with `"turn_complete"` and the result text
- `_done` event is set when `_run()` exits, used by `wait_agent` tool to block until completion

---

## 7. Listener

### 7.1 Listener ABC

```python
class Listener(ABC):
    """Long-running input source that routes messages to sessions."""

    @abstractmethod
    async def run(self, runtime: Runtime) -> None:
        """Main loop. Supervised by Runtime with exponential backoff."""

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        pass
```

### 7.2 CLIListener

Uses the input queue model: submits messages via `session.submit()` and runs a background consumer task.

```
CLIListener.run(runtime):
│
├─ session = runtime.create_session("native", config)
├─ cli_channel = CLIChannel(console)
├─ session.bind_primary(cli_channel)
├─ Register SIGINT → session.interrupt()
├─ Start background: consume_task = asyncio.create_task(_consume(session, channel))
│
├─ REPL LOOP:
│   ├─ text = await prompt_session.prompt_async()
│   │
│   ├─ if command:
│   │   ├─ /help        → show all available commands
│   │   ├─ /reset       → session.clear_history()
│   │   ├─ /sessions    → runtime.list_persisted_sessions()
│   │   ├─ /resume <id> → session = runtime.restore_session(id)
│   │   │                  session.bind_primary(cli_channel)
│   │   │                  await cli_channel.replay(session.history)
│   │   ├─ /fork <id>   → session = runtime.fork_session(id)
│   │   │                  session.bind_primary(cli_channel)
│   │   ├─ /attach <id> → runtime.attach_observer(id, cli_channel)
│   │   ├─ /detach      → runtime.detach_observer(current_observed_id, cli_channel)
│   │   ├─ /model [name]→ session.agent_config.model = name
│   │   ├─ /effort [lvl]→ session.agent_config.effort = level
│   │   ├─ /cost        → session.agent.get_usage() → display stats
│   │   ├─ /rename <n>  → session.metadata.name = n
│   │   ├─ /logging <l> → set console log level
│   │   └─ /quit, /exit, /q → exit
│   │
│   └─ else (regular message):
│       ├─ response_done.clear()
│       ├─ session.submit(text, "user")
│       └─ await response_done.wait()
│
├─ _consume(session, channel):        # background task
│   └─ async for stream, source in session.run():
│       ├─ await channel.send_stream(stream)
│       └─ response_done.set()
│
└─ finally: cancel consume_task
```

### 7.3 FeishuListener

Uses the input queue model with per-session background consumers.

**Threading model:** The `lark_oapi.ws.Client.start()` method is blocking — it
owns its own asyncio event loop internally (`loop.run_until_complete()`). It
must run in a separate thread. Event callbacks (`handle_message`) execute inside
the SDK's event loop on that thread. All interaction with our main asyncio loop
(session creation, queue submission, task creation) must be marshaled via
`main_loop.call_soon_threadsafe()`.

```
FeishuListener.run(runtime):
│
├─ main_loop = asyncio.get_running_loop()   # capture BEFORE spawning thread
├─ Setup lark_oapi REST client
├─ latest_channels: dict[str, FeishuChannel]  # per session
├─ consumer_tasks: dict[str, asyncio.Task]    # per session
├─ _shutdown_event: asyncio.Event
│
├─ handle_message(event):                    # ⚠ runs on SDK thread
│   ├─ Parse text, sender_id, chat_id, message_id
│   ├─ Strip @bot mentions from text (for group chats)
│   ├─ Skip non-text and empty messages
│   └─ main_loop.call_soon_threadsafe(       # marshal to main loop
│           _dispatch, sender_id, chat_id, message_id, text
│       )
│
├─ _dispatch(sender_id, chat_id, message_id, text):
│   │  # Runs on main event loop — safe for asyncio operations
│   ├─ session = runtime.get_or_create_session(sender_id, type, config)
│   ├─ channel = FeishuChannel(client, chat_id, reply_to=message_id)
│   ├─ session.bind_primary(channel)
│   ├─ latest_channels[session.id] = channel
│   ├─ ensure_consumer(session, get_channel=lambda: latest_channels[sid])
│   └─ session.submit(text, "user")
│
├─ Build and start ws.Client inside daemon thread:
│   ├─ def _run_ws():
│   │   ├─ new_loop = asyncio.new_event_loop()
│   │   ├─ asyncio.set_event_loop(new_loop)
│   │   ├─ Patch lark_oapi.ws.client.loop = new_loop  # avoid main loop capture
│   │   ├─ ws_client = lark.ws.Client(app_id, app_secret, event_handler)
│   │   └─ ws_client.start()
│   ├─ ws_thread = Thread(target=_run_ws, daemon=True)
│   ├─ ws_thread.start()
│   └─ await _shutdown_event.wait()          # block until shutdown
│
├─ _consume(session, get_channel):           # background task per session
│   └─ async for stream, source in session.run():
│       └─ await get_channel().send_stream(stream)
│
└─ shutdown():
    ├─ _shutdown_event.set()                 # unblock run()
    └─ cancel all consumer tasks
```

**Why daemon thread instead of `run_in_executor`:** The SDK's `start()` blocks
forever (internal `loop.run_until_complete(_select())` that sleeps infinitely).
A daemon thread dies automatically with the process. Using `run_in_executor`
would work but ties up a thread-pool slot indefinitely.

**Event loop isolation:** The ws.Client is constructed *inside* the daemon thread
function `_run_ws()`, which creates its own asyncio event loop. This is necessary
because the lark SDK caches a module-level `loop` variable at import time. The
thread patches `lark_oapi.ws.client.loop` to its local loop so `start()` uses the
correct one rather than the already-running main loop.

**@mention stripping:** In group chats, Feishu delivers the bot-trigger text
as `"@_user_1 <actual message>"`. The listener strips the mention prefix
before submitting to the session.

**Channel-per-message:** Each incoming message creates a fresh `FeishuChannel`
with that message's `message_id` as `reply_to`, so the response is threaded
to the correct message. The `latest_channels` dict ensures the consumer always
uses the most recent channel for a given session.

---

## 8. Runtime

### 8.1 Structure

```python
class Runtime:
    sessions: dict[str, Session]
    _session_manager: SessionManager          # persistence
    _agent_registry: dict[str, Callable]      # "native" -> factory(config, runtime_context)
    _listeners: list[Listener]
    _listener_tasks: list[asyncio.Task]
    _shutting_down: bool
```

### 8.2 Session Lifecycle

```python
# Create (two-phase init)
def create_session(
    self,
    agent_type: str,
    config: AgentConfig,
    session_id: str | None = None,
    metadata: SessionMetadata | None = None,
) -> Session:
    sid = session_id or generate_session_id()

    # Phase 1: Session with placeholder agent
    session = Session(id=sid, agent=None, agent_config=config, metadata=metadata)

    # Phase 2: RuntimeContext
    ctx = RuntimeContext(self, session)
    session.runtime_context = ctx

    # Phase 3: Agent with RuntimeContext
    agent = self.create_agent(agent_type, config, runtime_context=ctx)
    session.agent = agent

    # Auto-persist on every history update
    session.on_history_update = lambda: self.persist_session(sid)
    self.sessions[session.id] = session
    return session

# Get or create (for multiplexed channels like Feishu)
def get_or_create_session(
    self, sender_id: str, agent_type: str, config: AgentConfig,
) -> Session:
    # Lookup by sender_id tag
    for s in self.sessions.values():
        if s.metadata.tags.get("sender_id") == sender_id:
            return s
    session = self.create_session(agent_type, config)
    session.metadata.tags["sender_id"] = sender_id
    return session

# Fork
async def fork_session(
    self,
    source_id: str,
    new_agent_type: str | None = None,
    new_config: AgentConfig | None = None,
) -> Session:
    source = self.sessions[source_id]
    # Two-phase init for forked session too
    forked = Session(id=..., agent=None, ...)
    ctx = RuntimeContext(self, forked)
    forked.runtime_context = ctx
    agent = self.create_agent(agent_type, config, runtime_context=ctx)
    await agent.restore_state(forked_agent_state)
    forked.agent = agent
    forked.on_history_update = lambda: self.persist_session(forked.id)
    self.sessions[forked.id] = forked
    return forked

# Attach observer
def attach_observer(self, session_id: str, channel: Channel) -> None:
    self.sessions[session_id].attach_observer(channel)

# Detach observer
def detach_observer(self, session_id: str, channel: Channel) -> None:
    self.sessions[session_id].detach_observer(channel)
```

### 8.3 Persistence

```python
# Save session to disk
def persist_session(self, session_id: str) -> None:
    session = self.sessions[session_id]
    if not session.history:
        return
    legacy = PersistedSession(
        id=session.id,
        sender_id=session.metadata.tags.get("sender_id", "unknown"),
        created_at=session.metadata.created_at,
        updated_at=session.metadata.updated_at,
        name=session.metadata.name,
        agent_type=session.agent.agent_type,
        agent_config=asdict(session.agent_config),
        agent_state=session.agent.serialize_state(),
        metadata={
            "forked_from": session.metadata.forked_from,
            "tags": dict(session.metadata.tags),
        },
    )
    self._session_manager.save(legacy, session.history)

# Restore session from disk (two-phase init)
async def restore_session(self, session_id: str) -> Session:
    loaded = self._session_manager.load_session(session_id)
    history = SessionManager.deserialize_messages(loaded.messages)
    agent_type = loaded.agent_type or "native"
    config = AgentConfig(**loaded.agent_config) if loaded.agent_config else AgentConfig()

    session = Session(id=loaded.id, agent=None, ...)
    ctx = RuntimeContext(self, session)
    session.runtime_context = ctx

    agent = self.create_agent(agent_type, config, runtime_context=ctx)
    if loaded.agent_state:
        await agent.restore_state(loaded.agent_state)
    session.agent = agent

    session.on_history_update = lambda: self.persist_session(session.id)
    self.sessions[session.id] = session
    return session
```

### 8.4 Listener Supervision

```python
async def run(self) -> None:
    """Start runtime. Supervise all listeners. Block until shutdown."""
    self._listener_tasks = [
        asyncio.create_task(self._supervise(listener))
        for listener in self._listeners
    ]
    try:
        await asyncio.gather(*self._listener_tasks)
    finally:
        await self._shutdown()

async def _supervise(self, listener: Listener) -> None:
    """Restart listener on failure with exponential backoff."""
    backoff = 2.0
    max_backoff = 60.0
    while not self._shutting_down:
        try:
            await listener.run(self)
            break  # clean exit
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Listener %s failed: %s", listener, e)
            logger.debug("Restarting in %.1fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

async def _shutdown(self) -> None:
    """Graceful shutdown: stop listeners, persist sessions, close agents."""
    self._shutting_down = True
    for listener in self._listeners:
        await listener.shutdown()
    for session in self.sessions.values():
        self.persist_session(session.id)
        await session.agent.shutdown()
```

### 8.5 RuntimeContext

RuntimeContext bridges the tool layer to Runtime for sub-agent session management. Created per-session during two-phase init.

```python
class RuntimeContext:
    def __init__(self, runtime, parent_session):
        self._runtime = runtime
        self._parent = parent_session
        self._drivers: dict[str, SubAgentDriver] = {}
        self._total_spawns: int = 0

    async def spawn(self, agent_type, task, remote=None, cwd=None) -> tuple[str, str]:
        """Create child session → SubAgentDriver → submit task → start loop.

        Returns (session_id, warning_text).
        Raises SpawnLimitError if limits are exceeded:
          - max_concurrent_agents: blocks if too many running
          - max_total_spawns: blocks if lifetime spawn count exceeded
        Emits soft warning if spawn_warn_threshold is crossed.
        """

    def resolve(self, session_id, interaction_id, action, reason=None, answers=None) -> str:
        """Find driver → resolve_interaction() → unblocks child agent.

        answers: optional dict for AskUserQuestion interactions.
        """

    async def send(self, session_id, text) -> str:
        """Submit follow-up message to child session."""

    def list_agents(self) -> list[dict]:
        """Return status of all spawned sub-agents."""

    def cancel(self, session_id) -> str:
        """Interrupt child session via CancellationToken."""

class SpawnLimitError(Exception):
    """Raised when a sub-agent spawn exceeds configured limits."""
```

---

## 9. Interrupt Flow

```
User presses Ctrl+C
       │
       ▼
CLIListener catches SIGINT
       │
       ▼
session.interrupt()
       │
       ▼
self._current_token.cancel()
       │
       ▼
Agent's next token.check() raises CancelledError
       │
       ▼
Session._process() catches CancelledError
       │
       ├─ Append interrupted prompt + "[interrupted by user]" to history
       ├─ Touch metadata, trigger on_history_update
       │
       ▼
yield InterruptedEvent(partial_history)
       │
       ▼
CLIChannel.send_stream() renders "[interrupted]"
       │
       ▼
Session lock released, ready for next message
```

CancellationToken is cooperative. Checkpoints in the agent:

| Checkpoint | Location | What it prevents |
|------------|----------|--------------------|
| Before provider.chat() | Start of each tool loop iteration | Unnecessary LLM call |
| Signal drain | After cancel check, before LLM call | Delayed sub-agent notifications |
| Before tool.execute() | Before each tool execution | Unnecessary tool work |
| Between stream chunks | During streaming (optional) | Long stream responses |

---

## 10. SubAgentDriver — Full Lifecycle

### 10.1 Spawn

```
Parent agent calls launch_agent tool:
  → LaunchAgentTool.execute(type="ccagent", task="...")
  → RuntimeContext.spawn():
      0. Check spawn limits (max_concurrent_agents, max_total_spawns)
         → SpawnLimitError if exceeded → tool returns error to LLM
      1. runtime.create_session("ccagent", AgentConfig()) → child_session
      2. SubAgentDriver(session_id, parent, child_session)
      3. child_session.bind_primary(driver)
      4. child_session.submit(task, "user")
      5. driver.start() → asyncio.create_task(driver._run())
      6. self._total_spawns += 1
      7. return (child_session.id, warning)
```

### 10.2 Permission Flow

```
Child CCAgent needs to run a tool:
  │
  ▼
CCAgent.can_use_tool callback → InteractionRequest
  │
  ▼
SubAgentDriver.send_stream() receives InteractionRequest
  │
  ▼
driver._handle_interaction(request):
  ├─ _pending_interactions[request.id] = request
  └─ _notify_parent("permission_required", tool_input_json,
        extra={session_id, interaction_id, tool_name})
        │
        ▼
  parent_session.submit(source="sub_agent", metadata={...})
        │
        ▼
  Parent session._format_sub_agent_message(metadata) → text
        │
        ▼
  Parent agent sees formatted text as user message, calls reply_agent tool
        │
        ▼
  ReplyAgentTool.execute() → RuntimeContext.resolve()
        │
        ▼
  driver.resolve_interaction(id, "allow") → request.resolve(response)
        │
        ▼
  CCAgent.can_use_tool callback unblocks → proceeds with tool
```

### 10.3 Turn Completion

```
Child session finishes processing a turn:
  │
  ▼
SubAgentDriver.send_stream() captures final text as result
  │
  ▼
SubAgentDriver._run() loop iteration ends
  │
  ▼
driver._notify_parent("turn_complete", result_text)
  │
  ▼
Parent agent sees notification, can use the result or send follow-up
```

### 10.4 Cancellation

```
Parent agent calls cancel_agent tool:
  → CancelAgentTool.execute() → RuntimeContext.cancel()
  → child_session.interrupt() → CancellationToken.cancel()
  → Child agent's next token.check() raises CancelledError
  → Session._process() catches, yields InterruptedEvent
  → SubAgentDriver.send_stream() receives InterruptedEvent
  → driver._status = "interrupted", notify parent
```

---

## 11. Fork & Attach — Full Scenarios

### 11.1 Fork a Feishu Session to CLI for Debugging

```
1. Feishu user has been chatting → session_B (agent: native, ch: feishu)
2. Developer on CLI: /fork session_B
3. Runtime:
   a. source = sessions["session_B"]
   b. forked = await runtime.fork_session("session_B")  # copies history, creates new agent
   c. forked.bind_primary(cli_channel)                   # CLI drives the fork
   d. sessions[forked.id] = forked
4. Developer sees full history replayed in CLI
5. Developer can now interact with the forked session independently
6. Original session_B continues unaffected
```

### 11.2 Attach CLI to Observe a Feishu Session

```
1. Developer on CLI: /attach session_B
2. Runtime.attach_observer("session_B", cli_channel)
3. Session B adds cli_channel as observer
4. cli_channel.replay(session_B.history)        # catch up
5. When Feishu user sends next message:
   a. Session B processes → events yielded to FeishuChannel
   b. Same events broadcast to CLIChannel (observer)
   c. CLI renders in real-time (read-only, lazy Live display)
6. Developer sees everything but cannot send messages
7. To interact: /detach + /fork session_B
```

### 11.3 Feishu Delegates to Background CCAgent

```
1. Feishu user sends complex task to session_A (agent: native)
2. NativeAgent decides to delegate: calls launch_agent tool
   → type="ccagent", task="Implement feature X"
3. RuntimeContext.spawn():
   a. Creates session_C (agent: ccagent)
   b. Creates SubAgentDriver (channel for C, notifier for A)
   c. Starts background loop
4. CCAgent in session_C works autonomously:
   a. All tool calls → forwarded to parent as permission_required
   b. Parent agent sees formatted notification text, calls reply_agent
5. CCAgent in session_C completes a turn:
   → Parent receives "turn_complete" notification with result text
6. Developer on CLI: /attach session_C to inspect the background work
```

---

## 12. Persistence Format

```json
{
  "id": "20260315_181530_abc123",
  "sender_id": "feishu:user123",
  "created_at": "2026-03-15T18:15:30+00:00",
  "updated_at": "2026-03-15T18:45:00+00:00",
  "name": "debug feishu bot",
  "agent_type": "ccagent",
  "agent_config": {
    "model": "claude-sonnet-4-6",
    "system_prompt": "You are a helpful assistant.",
    "tools": null,
    "max_iterations": 30,
    "memory_enabled": true,
    "thinking": false,
    "effort": "medium",
    "temperature": 0.7,
    "extra": {}
  },
  "agent_state": {
    "sdk_session_id": "cc_abc123def456"
  },
  "metadata": {
    "forked_from": "20260315_170000_xyz789",
    "tags": {
      "origin_channel": "feishu",
      "sender_id": "feishu:user123"
    }
  },
  "messages": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi! How can I help?"},
    {"role": "user", "content": "Read main.py"},
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {"id": "tc_001", "name": "file_read", "arguments": {"path": "main.py"}}
      ]
    },
    {"role": "tool", "content": "...", "tool_call_id": "tc_001"},
    {"role": "assistant", "content": "Here's the content of main.py..."}
  ]
}
```

**Backward compatibility:** Top-level `sender_id`, `created_at`, `updated_at`, `name`
are kept for old JSON files. New fields (`agent_type`, `agent_config`, `agent_state`,
`metadata`) use defaults when absent, so old files load without error.

**Sub-agent sessions** are regular sessions in the session registry. They are persisted
and restored like any other session. The `SubAgentDriver` connection is transient
(not persisted) — it exists only during the runtime session.

**Auto-persist:** Sessions are automatically persisted on every `HistoryUpdate` event
via the `on_history_update` callback set by Runtime during session creation. This
ensures conversation state is saved incrementally, not just on shutdown.

---

## 13. Extension Guide

### Adding a New Agent Type

```python
class MyAgent:
    agent_type = "my_agent"
    default_model = "my-model"

    async def process(self, text, history, config, token):
        # Must yield AgentEvent items
        # Must yield UsageEvent as penultimate event (optional but recommended)
        # Must yield HistoryUpdate as final event
        yield TextDelta("response text")
        yield UsageEvent(usage=turn_usage)
        yield HistoryUpdate(history=[*history, user_msg, assistant_msg])

    async def reset(self): pass
    async def shutdown(self): pass
    def serialize_state(self) -> dict: return {}
    async def restore_state(self, state): pass
    async def on_fork(self, source_state) -> dict: return {}

# Register with runtime (factory accepts config and runtime_context)
runtime.register_agent("my_agent", lambda config, runtime_context=None: MyAgent(config))
```

### Adding a New Channel

```python
class MyChannel(Channel):
    async def send_stream(self, stream):
        async for event in stream:
            if isinstance(event, TextDelta):
                await self._send_text(event.text)
            elif isinstance(event, ActivityEvent):
                await self._update_status(event)
            elif isinstance(event, InteractionRequest):
                response = await self._prompt_user(event)
                event.resolve(response)
            elif isinstance(event, UsageEvent):
                await self._display_usage(event.usage)

    async def send(self, text):
        await self._send_text(text)
```

### Adding a New Listener (Queue Model)

```python
class MyListener(Listener):
    async def run(self, runtime):
        session = runtime.create_session("native", self._config)
        channel = MyChannel(self._transport)
        session.bind_primary(channel)

        # Start background consumer
        consume_task = asyncio.create_task(self._consume(session, channel))

        try:
            while True:
                msg = await self._wait_for_input()
                session.submit(msg.text, "user")
        finally:
            consume_task.cancel()

    async def _consume(self, session, channel):
        async for stream, source in session.run():
            await channel.send_stream(stream)

    async def shutdown(self):
        self._transport.close()
```

### Spawning Sub-Agents from Tools

Tools that need to spawn sub-agents receive a `RuntimeContext` via `__init__()`:

```python
class MyDelegationTool(Tool):
    _manual_registration = True  # skip auto-discovery

    def __init__(self, runtime_context):
        self._ctx = runtime_context

    async def execute(self, args):
        # Spawn a background sub-agent
        session_id = await self._ctx.spawn(
            agent_type="ccagent",
            task="Implement feature X",
        )
        return ToolResult(output=f"Sub-agent launched: {session_id}")
```

Register manually in `create_registry()` when `runtime_context` is provided.

---

## 14. Session Management Tools

Seven tools provide the agent with sub-agent management capabilities. They are registered in the tool registry only when a `RuntimeContext` is available (i.e., when the agent is created through the standard Runtime flow).

### 14.1 Tool Reference

| Tool | Name | Key Parameters | RuntimeContext Method |
|------|------|---------------|----------------------|
| `LaunchAgentTool` | `launch_agent` | type, task, remote, cwd | `ctx.spawn()` |
| `ReplyAgentTool` | `reply_agent` | session_id, interaction_id, action, reason, answers | `ctx.resolve()` |
| `MessageAgentTool` | `message_agent` | session_id, text | `ctx.send()` |
| `CheckAgentsTool` | `check_agents` | (none) | `ctx.list_agents()` |
| `CancelAgentTool` | `cancel_agent` | session_id | `ctx.cancel()` |
| `WaitAgentTool` | `wait_agent` | session_ids (optional), timeout | Polls `driver.status` |

**Tool descriptions** are comprehensive about async semantics to prevent redundant spawning:
- `launch_agent`: Explains that results arrive asynchronously; instructs the LLM to use `wait_agent` or end the turn rather than re-launching.
- `check_agents`: Instructs the LLM to check status BEFORE launching new agents.
- `wait_agent`: Documents the `launch_agent → wait_agent → use results` pattern.
- `message_agent`: Warns against messaging running agents.
- `cancel_agent`: Advises against cancelling agents just because they haven't responded yet.

### 14.2 wait_agent Tool

`WaitAgentTool` is a blocking tool that lets the agent synchronously wait for sub-agents to finish:

```python
class WaitAgentTool(Tool):
    async def execute(self, args):
        session_ids = args.get("session_ids")   # specific agents, or None = all running
        timeout = args.get("timeout", 300)

        # Collect target drivers (specific or all running)
        # Poll every 2s until all done, timeout, or parent cancelled
        # Return combined results with status per agent
```

**Pattern:** `launch_agent → wait_agent → use results in response`

This directly addresses the root cause of redundant spawning: the agent now has a mechanism to block until results are available instead of spawning new agents for the same task.

### 14.3 Spawn Guards

`RuntimeContext.spawn()` enforces configurable limits from `AgentConfig`:

| Config Field | Type | Behavior |
|-------------|------|----------|
| `max_concurrent_agents` | `int \| None` | Hard block if N agents are currently `running` |
| `max_total_spawns` | `int \| None` | Hard block if lifetime spawn count reached |
| `spawn_warn_threshold` | `int \| None` | Soft warning appended to tool output |

When a limit is exceeded, `SpawnLimitError` is raised and `LaunchAgentTool` returns a descriptive error to the LLM so it can adjust its strategy.

### 14.4 Example Flow: Delegated Task

```
1. User: "Refactor the auth module and update tests"

2. Agent calls launch_agent:
   {
     "type": "ccagent",
     "task": "Refactor src/auth.py: extract middleware, update tests"
   }
   → Returns session_id "20260316_120000_abc123"

3. Agent calls wait_agent to block until completion:
   {
     "session_ids": ["20260316_120000_abc123"]
   }
   → Blocks until sub-agent finishes (or timeout)
   → Returns combined results with status

4. If sub-agent hits a permission request mid-wait:
   → Signal delivered to parent via SignalToken
   → NativeAgent sees it at next checkpoint, calls reply_agent
   → Sub-agent unblocked, continues

5. Agent reports to user: "Auth module refactored. Here's what changed..."
```

### 14.5 Registry Wiring

Session tools use `_manual_registration = True` to prevent auto-discovery (they require `RuntimeContext` in `__init__`). They are explicitly instantiated in `create_registry()` when `runtime_context` is not None:

```python
def create_registry(config, runtime_context=None):
    registry = ToolRegistry()
    # ... auto-discover standard tools ...

    if runtime_context is not None:
        from miniclaw.tools.session_tools import (
            CancelAgentTool,
            CheckAgentsTool,
            LaunchAgentTool,
            MessageAgentTool,
            ReplyAgentTool,
            WaitAgentTool,
        )
        for cls in (LaunchAgentTool, ReplyAgentTool, MessageAgentTool,
                    CancelAgentTool, CheckAgentsTool, WaitAgentTool):
            tool = cls(runtime_context=runtime_context)
            registry.register(tool)

    return registry
```
