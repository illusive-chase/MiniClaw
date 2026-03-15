# MiniClaw Architecture Spec

## 1. Design Principles

| # | Principle | Implication |
|---|-----------|-------------|
| 1 | **Session is the nexus** | Session owns conversation state. Everything else (agent, channel) binds to it. |
| 2 | **Agent-channel agnosticism** | Agent produces typed events. Channel consumes them. Neither knows about the other. |
| 3 | **Listener/Channel split** | Listener = input routing. Channel = output rendering. Separate concerns. |
| 4 | **SubAgentDriver-as-Channel** | Background sub-agent communication uses the same Channel abstraction. SubAgentDriver acts as Channel for the child and notifier for the parent. |
| 5 | **Typed event stream** | All agent output flows through `AgentEvent` union. Session intercepts internal events, forwards the rest. |
| 6 | **Cooperative interrupts** | CancellationToken passed from Session to Agent. Agent checks at defined checkpoints. |
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
│   ├── created_at: datetime
│   ├── updated_at: datetime
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
│
├── _input_queue: asyncio.Queue[InputMessage]  # push-based input
├── runtime_context: RuntimeContext | None     # bridge to Runtime for sub-agents
│
├── _lock: asyncio.Lock                # one message at a time
├── _current_token: CancellationToken | None
└── status: ACTIVE | PAUSED | ARCHIVED
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
            self._inject_sub_agent_notification(msg.metadata)
        stream = self._process(msg.text if msg.source != "sub_agent"
                               else "[System] Sub-agent event received.")
        yield stream, msg.source
```

This enables:
- **Multi-source input**: user messages, sub-agent notifications, and system events all flow through the same queue
- **Notification injection**: sub-agent events are injected as synthetic tool-call/result pairs in history before processing
- **Decoupled consumption**: Listeners own the `run()` consumer loop, not Session

### 3.4 Core Method: `_process()`

```python
async def _process(self, text: str) -> AsyncIterator[AgentEvent]:
    """Internal: process a single message through the bound agent.

    Yields: TextDelta, ActivityEvent, InteractionRequest, InterruptedEvent.
    Consumes internally: HistoryUpdate, SessionControl.
    """
    token = CancellationToken()
    self._current_token = token

    try:
        async with self._lock:
            pending_text = text

            # Restart loop: handles SessionControl("plan_execute")
            while pending_text is not None:
                restart_text = None

                async for event in self.agent.process(
                    pending_text, self.history, self.agent_config, token
                ):
                    if isinstance(event, HistoryUpdate):
                        self.history = event.history

                    elif isinstance(event, SessionControl):
                        if event.action == "plan_execute":
                            self.history = []
                            await self.agent.reset()
                            restart_text = event.payload["plan_content"]
                            break

                    else:
                        # Forward to caller + broadcast to observers
                        await self._broadcast(event)
                        yield event

                pending_text = restart_text

    except CancelledError:
        yield InterruptedEvent()

    finally:
        self._current_token = None
```

The backward-compat `process()` wrapper delegates to `_process()` directly (bypasses queue).

### 3.5 Sub-Agent Notification Injection

When a sub-agent event arrives, `_inject_sub_agent_notification()` appends a synthetic tool-call/result pair to history:

```python
def _inject_sub_agent_notification(self, metadata: dict) -> None:
    call_id = f"sub_agent_event_{len(self.history)}"
    self.history.append(ChatMessage(
        role="assistant", content=None,
        tool_calls=[ToolCall(id=call_id, name="sub_agent_event", arguments=event_data)]
    ))
    self.history.append(ChatMessage(
        role="tool", content=notification_text, tool_call_id=call_id
    ))
```

This ensures the agent sees sub-agent events as part of its conversation context and can react accordingly (e.g., calling `reply_agent` to approve a permission).

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
            await binding.queue.put(event)
        except Exception:
            pass  # observer failure doesn't affect primary
```

Each observer has an `ObserverBinding`:

```python
@dataclass
class ObserverBinding:
    channel: Channel
    queue: asyncio.Queue[AgentEvent]
    task: asyncio.Task  # reads queue, calls channel.on_observe()
```

### 3.8 Fork

Fork is handled by **Runtime** (§8.2), not by Session, because forking
requires creating a new agent via the agent registry and registering the
new session — both of which are Runtime responsibilities. Session does
not own agent lifecycle (§3.2).

See `Runtime.fork_session()` in §8.2 for the full implementation.

### 3.9 Attach / Detach Observer

```python
def attach_observer(self, channel: Channel) -> None:
    queue = asyncio.Queue()

    async def observer_loop():
        # Replay history first
        await channel.replay(self.history)
        # Then stream live events
        await channel.on_observe(queue_to_async_iter(queue))

    task = asyncio.create_task(observer_loop())
    self.observers.append(ObserverBinding(channel, queue, task))

def detach_observer(self, channel: Channel) -> None:
    for binding in self.observers:
        if binding.channel is channel:
            binding.task.cancel()
            self.observers.remove(binding)
            break
```

---

## 4. AgentProtocol

### 4.1 Protocol Definition

```python
class AgentProtocol(Protocol):
    agent_type: str                    # "native", "ccagent"
    default_model: str

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
]
```

**Routing in Session._process():**

| Event | Forwarded to Channel? | Forwarded to Observers? | Session action |
|-------|----------------------|------------------------|----------------|
| TextDelta | Yes | Yes | — |
| ActivityEvent | Yes | Yes | — |
| InteractionRequest | Yes (primary resolves) | Yes (display only, auto-resolve) | — |
| HistoryUpdate | No | No | `self.history = event.history` |
| SessionControl | No | No | Handle action (e.g., plan_execute restart) |
| InterruptedEvent | Yes | Yes | — |

### 4.3 AgentConfig

```python
@dataclass
class AgentConfig:
    model: str                              # "claude-sonnet-4-6"
    system_prompt: str                      # custom system prompt
    tools: list[str] | None = None          # allowed tool names (None = all)
    max_iterations: int = 30                # tool loop cap
    memory_enabled: bool = True
    thinking: bool = False
    effort: str = "medium"                  # "low" / "medium" / "high"
    extra: dict = field(default_factory=dict)  # agent-type-specific overrides
```

### 4.4 CancellationToken

```python
class CancellationToken:
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
```

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
```

ActivityEvent and InteractionRequest retain their existing definitions from the current codebase.

---

## 5. Agent Implementations

### 5.1 Agent (native)

```
Agent.process(text, history, config, token):
│
├─ Build system prompt (config.system_prompt + memory + tool list + datetime)
├─ Build messages: [system, *history, user(text)]
├─ tool_specs = registry.specs(filter=config.tools)
│
├─ TOOL LOOP (max config.max_iterations):
│   ├─ token.check()                          ← checkpoint 1
│   ├─ provider.chat_stream(messages, specs, config.model)
│   │   ├─ str chunks → yield TextDelta
│   │   └─ ChatResponse → response
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
│       ├─ append assistant message
│       ├─ yield HistoryUpdate(messages[1:])   ← strip system msg
│       └─ return
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
├─ client = get_or_create_sdk_client(config, self._sdk_session_id)
├─ queue = asyncio.Queue()
│
├─ BACKGROUND (_run_sdk):
│   ├─ can_use_tool callback:
│   │   ├─ Create InteractionRequest
│   │   ├─ queue.put(("interaction", request))
│   │   ├─ await request.future            ← blocks SDK
│   │   └─ If plan_execute: queue.put(("plan_execute", ...)) + interrupt SDK
│   ├─ client.query(text)
│   ├─ async for msg in receive_response():
│   │   ├─ TextBlock    → queue.put(("text", chunk))
│   │   ├─ ToolUseBlock → queue.put(("activity", START))
│   │   ├─ ToolResult   → queue.put(("activity", FINISH|FAILED))
│   │   └─ TaskStarted  → queue.put(("activity", AGENT_START))
│   └─ queue.put(("done", None))
│
├─ MAIN LOOP (consume queue):
│   ├─ "text"         → yield TextDelta
│   ├─ "activity"     → yield ActivityEvent
│   ├─ "interaction"  → yield InteractionRequest
│   ├─ "plan_execute" → yield SessionControl("plan_execute", payload); break
│   ├─ "done"         → break
│   └─ "error"        → raise
│
├─ self._sdk_session_id = client.session_id
├─ yield HistoryUpdate(visible_history)
│
├─ Stateful properties:
│   reset()           → close SDK client, clear _sdk_session_id
│   serialize_state() → {"sdk_session_id": self._sdk_session_id}
│   restore_state()   → self._sdk_session_id = state["sdk_session_id"]
│   on_fork()         → {} (fresh SDK, don't reuse session)
│   shutdown()        → close all SDK clients
```

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
        """

    @abstractmethod
    async def send(self, text: str) -> None:
        """Send a simple text message (notifications, errors)."""

    async def on_observe(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Render events as read-only observer.

        Default: same as send_stream but auto-resolve interactions.
        Override for custom observer UX.
        """
        async def auto_resolve(source):
            async for event in source:
                if isinstance(event, InteractionRequest):
                    event.resolve(InteractionResponse.auto_allow())
                    yield ActivityEvent(...)  # show what was auto-resolved
                else:
                    yield event
        await self.send_stream(auto_resolve(stream))

    async def replay(self, history: list[ChatMessage]) -> None:
        """Replay past history when attaching/resuming. Optional."""
        pass
```

### 6.2 CLIChannel

```
CLIChannel
├── _console: Rich Console
├── _console_handler: RichHandler (logging)
│
├── send_stream(stream):
│   ├─ Start Rich Live panel
│   ├─ async for event in stream:
│   │   ├─ TextDelta        → buffer += text, re-render Markdown panel
│   │   ├─ ActivityEvent    → tracker.apply(event), update footer
│   │   ├─ InteractionRequest:
│   │   │   ├─ Pause Live
│   │   │   ├─ Render interaction panel (permission/question/plan)
│   │   │   ├─ Prompt user for response
│   │   │   ├─ event.resolve(response)
│   │   │   └─ Resume Live
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
    └─ Same as send_stream but interactions displayed as "[auto-allowed]"

Note: User input (prompt) is handled by CLIListener (§7.2), not
CLIChannel. Channel is an output-only endpoint (Principle #3).
```

### 6.3 FeishuChannel

```
FeishuChannel(transport, chat_id)
├── _transport: FeishuTransport (shared, handles API calls)
├── _chat_id: str (specific chat/user)
│
├── send_stream(stream):
│   ├─ Collect text chunks into buffer
│   ├─ For ActivityEvents: optionally update card with progress
│   ├─ For InteractionRequests: send interactive card with buttons
│   │   └─ Wait for callback from Feishu (button click)
│   │   └─ event.resolve(response)
│   └─ Send final message card via Feishu API
│
├── send(text):
│   └─ transport.send_text(chat_id, text)
│
└── replay(history):
    └─ No-op or send summary card
```

### 6.4 SubAgentDriver

SubAgentDriver is a dual-role component: Channel for a child session and notifier for a parent session. It lives at `miniclaw/subagent_driver.py` (not in `channels/` to avoid circular imports).

```
SubAgentDriver(session_id, parent_session, allowed_tools, child_session)
├── _session_id: str
├── _parent_session: Session          # receives notifications
├── _allowed_tools: set[str]          # auto-approved tools
├── _child_session: Session           # drives this session
├── _pending_interactions: dict       # interaction_id -> InteractionRequest
├── _status: str                      # running | completed | failed | interrupted
├── _result: str | None               # final text output
│
├── send_stream(stream):              # Channel interface
│   ├─ Collect TextDelta chunks → buffer
│   ├─ InteractionRequest:
│   │   ├─ If tool_name in allowed_tools → auto-resolve(allow)
│   │   └─ Else → store in _pending_interactions, notify parent
│   ├─ InterruptedEvent → set status=interrupted, notify parent
│   └─ Capture final text as result
│
├── send(text): no-op
│
├── _handle_interaction(request):
│   ├─ Auto-resolve if tool in allowed_tools
│   └─ Otherwise: store pending, parent.submit(source="sub_agent", metadata=...)
│
├── resolve_interaction(id, action, reason):
│   └─ Pop from _pending, resolve with allow/deny → unblocks child agent
│
├── _notify_parent(event_type, text, extra):
│   └─ parent.submit(text, source="sub_agent", metadata={event_type, ...})
│
├── start():
│   └─ asyncio.create_task(_run())
│
├── _run():                           # background loop
│   ├─ async for stream, source in child_session.run():
│   │   └─ await self.send_stream(stream)
│   └─ On completion: set status=completed, notify parent with result
│
├── status → str (property)
├── result → str | None (property)
└── pending_interaction_ids() → list[str]
```

**Key behaviors:**
- InteractionRequest for tool in `allowed_tools` → auto-resolve (allow)
- InteractionRequest for other tools → store in `_pending_interactions`, notify parent via `parent.submit(source="sub_agent")`
- On completion → notify parent with result text
- `resolve_interaction()` resolves the InteractionRequest's `_future`, unblocking the child agent's `can_use_tool` callback

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
│   │   ├─ /reset       → session.clear_history()
│   │   ├─ /resume <id> → session = runtime.restore_session(id)
│   │   │                  session.bind_primary(cli_channel)
│   │   │                  await cli_channel.replay(session.history)
│   │   ├─ /fork <id>   → session = runtime.fork_session(id)
│   │   │                  session.bind_primary(cli_channel)
│   │   ├─ /attach <id> → runtime.attach_observer(id, cli_channel)
│   │   ├─ /detach      → runtime.detach_observer(current_observed_id, cli_channel)
│   │   ├─ /sessions    → runtime.list_sessions()
│   │   ├─ /model [name]→ session.agent_config.model = name
│   │   ├─ /effort [lvl]→ session.agent_config.effort = level
│   │   └─ /cost        → session.usage_summary()
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

```
FeishuListener.run(runtime):
│
├─ Setup lark_oapi WebSocket client
├─ latest_channels: dict[str, FeishuChannel]  # per session
├─ consumer_tasks: dict[str, asyncio.Task]    # per session
│
├─ handle_message(event):
│   ├─ session = runtime.get_or_create_session(sender_id, type, config)
│   ├─ channel = FeishuChannel(client, chat_id, reply_to)
│   ├─ session.bind_primary(channel)
│   ├─ latest_channels[session.id] = channel
│   ├─ ensure_consumer(session, get_channel=lambda: latest_channels[sid])
│   └─ session.submit(text, "user")
│
├─ _consume(session, get_channel):    # background task per session
│   └─ async for stream, source in session.run():
│       └─ await get_channel().send_stream(stream)
│
└─ shutdown(): cancel all consumer tasks
```

---

## 8. Runtime

### 8.1 Structure

```python
class Runtime:
    sessions: dict[str, Session]
    session_manager: SessionManager          # persistence
    agent_registry: dict[str, Callable]      # "native" -> factory(config, runtime_context)
    listeners: list[Listener]
    _listener_tasks: list[asyncio.Task]
    _shutting_down: bool
```

### 8.2 Session Lifecycle

```python
# Create (two-phase init)
def create_session(self, agent_type: str, config: AgentConfig) -> Session:
    sid = generate_session_id()

    # Phase 1: Session with placeholder agent
    session = Session(id=sid, agent=None, agent_config=config, ...)

    # Phase 2: RuntimeContext
    ctx = RuntimeContext(self, session)
    session.runtime_context = ctx

    # Phase 3: Agent with RuntimeContext
    agent = self.create_agent(agent_type, config, runtime_context=ctx)
    session.agent = agent

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
    self.session_manager.save(legacy, session.history)

# Restore session from disk (two-phase init)
async def restore_session(self, session_id: str) -> Session:
    loaded = self.session_manager.load_session(session_id)
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

    self.sessions[session.id] = session
    return session
```

### 8.4 Listener Supervision

```python
async def run(self) -> None:
    """Start runtime. Supervise all listeners. Block until shutdown."""
    self._listener_tasks = [
        asyncio.create_task(self._supervise(listener))
        for listener in self.listeners
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
    for listener in self.listeners:
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

    async def spawn(self, agent_type, task, config=None, allowed_tools=None) -> str:
        """Create child session → SubAgentDriver → submit task → start loop → return session_id."""

    def resolve(self, session_id, interaction_id, action, reason=None) -> str:
        """Find driver → resolve_interaction() → unblocks child agent."""

    async def send(self, session_id, text) -> str:
        """Submit follow-up message to child session."""

    def list_agents(self) -> list[dict]:
        """Return status of all spawned sub-agents."""

    def cancel(self, session_id) -> str:
        """Interrupt child session via CancellationToken."""
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
|------------|----------|-----------------|
| Before provider.chat() | Start of each tool loop iteration | Unnecessary LLM call |
| Before tool.execute() | Before each tool execution | Unnecessary tool work |
| Between stream chunks | During streaming (optional) | Long stream responses |

---

## 10. SubAgentDriver — Full Lifecycle

### 10.1 Spawn

```
Parent agent calls launch_agent tool:
  → LaunchAgentTool.execute(type="ccagent", task="...", allowed_tools=["Bash", "Read"])
  → RuntimeContext.spawn():
      1. runtime.create_session("ccagent", config) → child_session
      2. SubAgentDriver(session_id, parent, allowed_tools, child_session)
      3. child_session.bind_primary(driver)
      4. child_session.submit(task, "user")
      5. driver.start() → asyncio.create_task(driver._run())
      6. return child_session.id
```

### 10.2 Permission Flow

```
Child CCAgent needs to run a tool not in allowed_tools:
  │
  ▼
CCAgent.can_use_tool callback → InteractionRequest
  │
  ▼
SubAgentDriver.send_stream() receives InteractionRequest
  │
  ▼
driver._handle_interaction(request):
  ├─ tool_name in allowed_tools? → auto-resolve(allow), return
  └─ else:
      ├─ _pending_interactions[request.id] = request
      └─ _notify_parent("permission_required", text, metadata)
            │
            ▼
      parent_session.submit(source="sub_agent", metadata={...})
            │
            ▼
      Parent session._inject_sub_agent_notification(metadata)
            │
            ▼
      Parent agent sees notification in history, calls reply_agent tool
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

### 10.3 Completion

```
Child session finishes processing (no more items in queue):
  │
  ▼
SubAgentDriver._run() exits the async for loop
  │
  ▼
driver._status = "completed"
driver._notify_parent("completed", result_text)
  │
  ▼
Parent agent sees completion notification, can use the result
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
   c. CLI renders in real-time (read-only)
6. Developer sees everything but cannot send messages
7. To interact: /detach + /fork session_B
```

### 11.3 Feishu Delegates to Background CCAgent

```
1. Feishu user sends complex task to session_A (agent: native)
2. NativeAgent decides to delegate: calls launch_agent tool
   → type="ccagent", task="Implement feature X", allowed_tools=["Bash", "Read", "Write"]
3. RuntimeContext.spawn():
   a. Creates session_C (agent: ccagent)
   b. Creates SubAgentDriver (channel for C, notifier for A)
   c. Starts background loop
4. CCAgent in session_C works autonomously:
   a. Reads files, writes code — auto-approved (in allowed_tools)
   b. Needs to run git push — NOT in allowed_tools → permission forwarded to A
5. Parent session_A receives notification via submit(source="sub_agent")
   → Agent sees sub_agent_event in history
   → Calls reply_agent(session_id=C, interaction_id=..., action="allow")
6. CCAgent in session_C continues, completes task
7. Parent session_A receives completion notification with result
8. Developer on CLI: /attach session_C to inspect the background work
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
    "tools": ["shell", "file_read", "file_write", "grep", "glob"],
    "max_iterations": 30,
    "memory_enabled": true,
    "thinking": true,
    "effort": "high",
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

---

## 13. Extension Guide

### Adding a New Agent Type

```python
class MyAgent:
    agent_type = "my_agent"
    default_model = "my-model"

    async def process(self, text, history, config, token):
        # Must yield AgentEvent items
        # Must yield HistoryUpdate as final event
        yield TextDelta("response text")
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
            allowed_tools=["Bash", "Read", "Write"],
        )
        return ToolResult(output=f"Sub-agent launched: {session_id}")
```

Register manually in `create_registry()` when `runtime_context` is provided.

---

## 14. Session Management Tools

Five tools provide the agent with sub-agent management capabilities. They are registered in the tool registry only when a `RuntimeContext` is available (i.e., when the agent is created through the standard Runtime flow).

### 14.1 Tool Reference

| Tool | Name | Key Parameters | RuntimeContext Method |
|------|------|---------------|----------------------|
| `LaunchAgentTool` | `launch_agent` | type, task, allowed_tools, model | `ctx.spawn()` |
| `ReplyAgentTool` | `reply_agent` | session_id, interaction_id, action, reason | `ctx.resolve()` |
| `MessageAgentTool` | `message_agent` | session_id, text | `ctx.send()` |
| `CheckAgentsTool` | `check_agents` | (none) | `ctx.list_agents()` |
| `CancelAgentTool` | `cancel_agent` | session_id | `ctx.cancel()` |

### 14.2 Example Flow: Delegated Task

```
1. User: "Refactor the auth module and update tests"

2. Agent calls launch_agent:
   {
     "type": "ccagent",
     "task": "Refactor src/auth.py: extract middleware, update tests",
     "allowed_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
   }
   → Returns session_id "20260316_120000_abc123"

3. Agent calls check_agents:
   → Returns:
     Session: 20260316_120000_abc123
     Status: running
     Pending interactions: (none)

4. Background CCAgent hits a tool not in allowed_tools (e.g., git push):
   → Agent receives sub_agent_event notification in history
   → Agent calls reply_agent:
     {
       "session_id": "20260316_120000_abc123",
       "interaction_id": "interaction_456",
       "action": "deny",
       "reason": "Don't push yet — wait for review"
     }

5. Background CCAgent completes:
   → Agent receives completion notification with result text

6. Agent reports to user: "Auth module refactored. Here's what changed..."
```

### 14.3 Registry Wiring

Session tools use `_manual_registration = True` to prevent auto-discovery (they require `RuntimeContext` in `__init__`). They are explicitly instantiated in `create_registry()` when `runtime_context` is not None:

```python
def create_registry(config, memory=None, runtime_context=None):
    registry = ToolRegistry()
    # ... auto-discover standard tools ...

    if runtime_context is not None:
        for cls in (LaunchAgentTool, ReplyAgentTool, MessageAgentTool,
                    CheckAgentsTool, CancelAgentTool):
            tool = cls(runtime_context=runtime_context)
            registry.register(tool)

    return registry
```
