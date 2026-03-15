# MiniClaw Architecture Spec

## 1. Design Principles

| # | Principle | Implication |
|---|-----------|-------------|
| 1 | **Session is the nexus** | Session owns conversation state. Everything else (agent, channel) binds to it. |
| 2 | **Agent-channel agnosticism** | Agent produces typed events. Channel consumes them. Neither knows about the other. |
| 3 | **Listener/Channel split** | Listener = input routing. Channel = output rendering. Separate concerns. |
| 4 | **Pipe-as-Channel** | Inter-session communication uses the same Channel abstraction. No special-case code. |
| 5 | **Typed event stream** | All agent output flows through `AgentEvent` union. Session intercepts internal events, forwards the rest. |
| 6 | **Cooperative interrupts** | CancellationToken passed from Session to Agent. Agent checks at defined checkpoints. |
| 7 | **Extensible via protocol** | New agents, channels, listeners implement protocols and register with Runtime. |

---

## 2. Component Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            RUNTIME                                       │
│                                                                          │
│  Supervises      ┌──────────────┐   ┌──────────────┐   ┌────────────┐  │
│  listeners:      │ CLIListener   │   │FeishuListener │   │PipeDriver  │  │
│                  │ (REPL loop)   │   │(poll + backoff│   │(per pipe)  │  │
│                  └──────┬───────┘   │ + semaphore)  │   └─────┬──────┘  │
│                         │           └──────┬────────┘         │         │
│                         │                  │                  │         │
│  Routes to      ┌──────▼──────────────────▼──────────────────▼───┐    │
│  sessions:      │              SESSION REGISTRY                    │    │
│                 │                                                  │    │
│                 │  session_A ── agent: native,  ch: CLI            │    │
│                 │  session_B ── agent: ccagent, ch: Feishu         │    │
│                 │               observers: [CLI]                   │    │
│                 │  session_C ── agent: native,  ch: PipeEnd_C     │    │
│                 │  session_D ── agent: native,  ch: PipeEnd_D     │    │
│                 │               (C <-> D piped)                    │    │
│                 └──────────────────────────────────────────────────┘    │
│                                                                          │
│  Manages:        session lifecycle (create, fork, attach, persist)       │
│                  agent registry ("native" -> Agent, "ccagent" -> CCAgent)│
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
├── _lock: asyncio.Lock                # one message at a time
├── _current_token: CancellationToken | None
└── status: ACTIVE | PAUSED | ARCHIVED
```

### 3.2 Ownership Rules

- Session **owns**: `history`, `agent_config`, `metadata`, `status`
- Session **borrows**: `agent` (bound by Runtime), `primary_channel` (bound by Listener)
- Session **does NOT own**: Channel lifecycle, Agent lifecycle, persistence

This separation enables fork (copy state, rebind to different agent/channel) and attach (add observer channel without touching state).

### 3.3 Core Method: `process()`

```python
async def process(self, text: str) -> AsyncIterator[AgentEvent]:
    """Process user input through the bound agent.

    Yields: TextDelta, ActivityEvent, InteractionRequest
    Consumes internally: HistoryUpdate, SessionControl
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

### 3.4 Interrupt

```python
def interrupt(self) -> None:
    """Cancel the current in-progress processing."""
    if self._current_token:
        self._current_token.cancel()
```

Called by Channel/Listener when user sends Ctrl+C or `/stop`.

### 3.5 Observer Broadcasting

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

### 3.6 Fork

Fork is handled by **Runtime** (§8.2), not by Session, because forking
requires creating a new agent via the agent registry and registering the
new session — both of which are Runtime responsibilities. Session does
not own agent lifecycle (§3.2).

See `Runtime.fork_session()` in §8.2 for the full implementation.

### 3.7 Attach / Detach Observer

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

**Routing in Session.process():**

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

### 6.4 PipeEnd

```
PipeEnd(name)
├── _inbox: asyncio.Queue[str]
├── _other: PipeEnd (linked pair)
│
├── send_stream(stream):
│   ├─ Collect all TextDelta chunks → full_text
│   ├─ InteractionRequests: auto-resolve (no human on a pipe)
│   └─ self._other._inbox.put(full_text)
│
├── send(text):
│   └─ self._other._inbox.put(text)
│
└── listen() -> AsyncIterator[str]:
    └─ while True: yield await self._inbox.get()

create_pipe(name_a, name_b) -> tuple[PipeEnd, PipeEnd]:
    a, b = PipeEnd(name_a), PipeEnd(name_b)
    a._other, b._other = b, a
    return a, b
```

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

```
CLIListener.run(runtime):
│
├─ session = runtime.create_session("native", config)
├─ cli_channel = CLIChannel(console)
├─ session.bind_primary(cli_channel)
├─ Register SIGINT → session.interrupt()
│
├─ REPL LOOP:
│   ├─ text = await cli_channel.prompt()
│   │
│   ├─ if command:
│   │   ├─ /reset      → session.clear_history()
│   │   ├─ /resume <id> → session = runtime.restore_session(id)
│   │   │                  session.bind_primary(cli_channel)
│   │   │                  await cli_channel.replay(session.history)
│   │   ├─ /fork <id>   → session = runtime.fork_session(id)
│   │   │                  session.bind_primary(cli_channel)
│   │   ├─ /attach <id> → runtime.attach_observer(id, cli_channel)
│   │   ├─ /detach      → runtime.detach_observer(current_observed_id, cli_channel)
│   │   ├─ /pipe <id>   → runtime.connect_pipe(session.id, id)
│   │   ├─ /unpipe <id> → runtime.disconnect_pipe(session.id, id)
│   │   ├─ /sessions    → runtime.list_sessions()
│   │   ├─ /model [name]→ session.agent_config.model = name
│   │   └─ /cost        → session.usage_summary()
│   │
│   └─ else (regular message):
│       ├─ stream = session.process(text)
│       └─ await cli_channel.send_stream(stream)
```

### 7.3 FeishuListener

```
FeishuListener.run(runtime):
│
├─ transport = FeishuTransport(app_id, app_secret)
├─ channels: dict[str, FeishuChannel] = {}  # per-sender
├─ semaphore = asyncio.Semaphore(4)          # max 4 concurrent
├─ backoff = ExponentialBackoff(min=2s, max=60s)
│
├─ POLL LOOP:
│   ├─ try:
│   │   ├─ events = await transport.poll()
│   │   ├─ backoff.reset()
│   │   └─ for event in events:
│   │       ├─ await semaphore.acquire()
│   │       └─ asyncio.create_task(_handle(event, ...))
│   └─ except TransportError:
│       └─ await backoff.wait()   # 2s → 4s → 8s → ... → 60s
│
├─ _handle(event, transport, runtime):
│   ├─ sender = f"feishu:{event.user_id}"
│   ├─ session = runtime.get_or_create_session(sender, agent_type, config)
│   ├─ channel = channels.setdefault(sender, FeishuChannel(transport, event.chat_id))
│   ├─ session.bind_primary(channel)
│   ├─ stream = session.process(event.text)
│   ├─ await channel.send_stream(stream)
│   └─ semaphore.release()
```

### 7.4 PipeDriver

```
PipeDriver.run(session, pipe_end):
│
├─ LOOP:
│   ├─ text = await pipe_end.listen()   # blocks on inbox queue
│   │
│   ├─ if text is POISON_PILL:
│   │   └─ break                        # pipe disconnected
│   │
│   ├─ stream = session.process(text)
│   └─ await pipe_end.send_stream(stream)  # forwards to other end
```

---

## 8. Runtime

### 8.1 Structure

```python
class Runtime:
    sessions: dict[str, Session]
    session_manager: SessionManager          # persistence
    agent_registry: dict[str, Callable]      # "native" -> Agent factory
    listeners: list[Listener]
    _listener_tasks: list[asyncio.Task]
    _pipes: dict[tuple[str, str], tuple]     # sorted session IDs -> (driver_a, driver_b, task_a, task_b)
    _shutting_down: bool
```

### 8.2 Session Lifecycle

```python
# Create
def create_session(self, agent_type: str, config: AgentConfig) -> Session:
    agent = self.agent_registry[agent_type](config)
    session = Session(id=generate_id(), agent=agent, agent_config=config, ...)
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
    forked = await source.fork(new_agent_type, new_config)
    self.sessions[forked.id] = forked
    return forked

# Attach observer
def attach_observer(self, session_id: str, channel: Channel) -> None:
    self.sessions[session_id].attach_observer(channel)

# Detach observer
def detach_observer(self, session_id: str, channel: Channel) -> None:
    self.sessions[session_id].detach_observer(channel)

# Connect pipe
def connect_pipe(self, session_a_id: str, session_b_id: str) -> None:
    pipe_a, pipe_b = create_pipe(session_a_id, session_b_id)
    sa, sb = self.sessions[session_a_id], self.sessions[session_b_id]

    driver_a = PipeDriver(sa, pipe_a)
    driver_b = PipeDriver(sb, pipe_b)
    task_a = asyncio.create_task(driver_a.run(self))
    task_b = asyncio.create_task(driver_b.run(self))

    key = tuple(sorted([session_a_id, session_b_id]))
    self._pipes[key] = (driver_a, driver_b, task_a, task_b)

# Disconnect pipe
async def disconnect_pipe(self, session_a_id: str, session_b_id: str) -> None:
    key = tuple(sorted([session_a_id, session_b_id]))
    driver_a, driver_b, task_a, task_b = self._pipes.pop(key)
    await driver_a.shutdown()
    await driver_b.shutdown()
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

# Restore session from disk
async def restore_session(self, session_id: str) -> Session:
    loaded = self.session_manager.load_session(session_id)
    history = SessionManager.deserialize_messages(loaded.messages)

    # Use persisted agent_type, fall back to "native" for old files
    agent_type = loaded.agent_type or "native"
    config = AgentConfig(**loaded.agent_config) if loaded.agent_config else AgentConfig()

    agent = self.create_agent(agent_type, config)
    if loaded.agent_state:
        await agent.restore_state(loaded.agent_state)

    # Rebuild full metadata, merging backward-compat fields
    meta_tags = dict(loaded.metadata.get("tags", {})) if loaded.metadata else {}
    if not meta_tags.get("sender_id"):
        meta_tags["sender_id"] = loaded.sender_id

    session = Session(
        id=loaded.id,
        agent=agent,
        agent_config=config,
        metadata=SessionMetadata(
            created_at=loaded.created_at,
            updated_at=loaded.updated_at,
            name=loaded.name,
            forked_from=loaded.metadata.get("forked_from") if loaded.metadata else None,
            tags=meta_tags,
        ),
        history=history,
    )
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
    """Graceful shutdown: stop listeners, disconnect pipes, persist sessions, close agents."""
    self._shutting_down = True
    for listener in self.listeners:
        await listener.shutdown()
    for key in list(self._pipes):
        driver_a, driver_b, _, _ = self._pipes.pop(key)
        await driver_a.shutdown()
        await driver_b.shutdown()
    for session in self.sessions.values():
        self.persist_session(session.id)
        await session.agent.shutdown()
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
Session.process() catches CancelledError
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

## 10. Pipe — Full Lifecycle

### 10.1 Creation

```
User: /pipe session_B

Runtime.connect_pipe(session_A.id, session_B.id):
  pipe_a, pipe_b = create_pipe("A", "B")
  driver_a = PipeDriver(session_A, pipe_a)
  driver_b = PipeDriver(session_B, pipe_b)
  asyncio.create_task(driver_a.run())
  asyncio.create_task(driver_b.run())
```

### 10.2 Message Flow

```
Session A's agent produces: "Please run the tests"
  │
  ▼
PipeEnd A.send_stream(stream)
  ├─ Collects TextDelta → "Please run the tests"
  ├─ Auto-resolves any InteractionRequests
  └─ pipe_a._other._inbox.put("Please run the tests")
  │
  ▼
PipeEnd B._inbox receives "Please run the tests"
  │
  ▼
PipeDriver B: text = await pipe_b.listen()
  │
  ▼
stream = session_B.process("Please run the tests")
  │
  ▼
Session B's agent executes tools, produces: "Tests passed: 42/42"
  │
  ▼
PipeEnd B.send_stream(stream)
  └─ pipe_b._other._inbox.put("Tests passed: 42/42")
  │
  ▼
PipeEnd A._inbox receives "Tests passed: 42/42"
  │
  ▼
PipeDriver A: text = await pipe_a.listen()
  │
  ▼
stream = session_A.process("Tests passed: 42/42")
  └─ Session A's agent processes the result
```

### 10.3 Teardown

```
User: /unpipe session_B

Runtime.disconnect_pipe(session_A.id, session_B.id):
  key = sorted([session_A.id, session_B.id])
  driver_a, driver_b, task_a, task_b = self._pipes.pop(key)
  await driver_a.shutdown()   # sends POISON_PILL, PipeDriver exits loop
  await driver_b.shutdown()   # sends POISON_PILL, PipeDriver exits loop

Runtime tracks active pipes in _pipes: dict[tuple[str, str], (driver_a, driver_b, task_a, task_b)].
Key is sorted session IDs so lookup is direction-independent.
Pipes are also torn down during Runtime._shutdown().
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

### 11.3 Connect Two Sessions via Pipe

```
1. session_A: "orchestrator" agent (plans tasks)
   session_B: "executor" agent (runs code)
2. Developer: /pipe session_A session_B
3. Runtime.connect_pipe(A, B)
4. Developer sends to session_A: "Run the test suite and fix failures"
5. Session A's agent:
   a. Plans approach
   b. Replies "Running tests" → displayed to CLI
   c. Via pipe: sends "run pytest and report results" → session_B
6. Session B's agent:
   a. Executes shell tool: pytest
   b. Replies via pipe: "3 failures in test_auth.py"
7. Session A's agent receives pipe response:
   a. Continues planning
   b. Via pipe: sends "fix the 3 failures in test_auth.py"
8. Session B's agent fixes code
9. Loop until done
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

# Register with runtime
runtime.register_agent("my_agent", lambda config: MyAgent(config))
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

### Adding a New Listener

```python
class MyListener(Listener):
    async def run(self, runtime):
        while True:
            msg = await self._wait_for_input()
            session = runtime.get_or_create_session(
                msg.sender_id, "native", self._config
            )
            channel = MyChannel(self._transport, msg.chat_id)
            session.bind_primary(channel)
            stream = session.process(msg.text)
            await channel.send_stream(stream)

    async def shutdown(self):
        self._transport.close()
```

