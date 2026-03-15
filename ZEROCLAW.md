ZeroClaw Message Flow Architecture

The Big Picture

┌──────────────────────────────────────────────────────────────────────┐
│                        ENTRY POINTS                                  │
│                                                                      │
│  CLI ("zeroclaw agent")    Gateway HTTP/WS     Channels (long-poll)  │
│         │                     │                      │               │
│         │                     │                      │               │
│         ▼                     ▼                      ▼               │
│  ┌─────────────┐    ┌────────────────┐    ┌──────────────────┐      │
│  │ Interactive  │    │ Axum Server    │    │ Telegram/Discord │      │
│  │ REPL loop   │    │ (mod.rs)       │    │ Slack/Signal/etc │      │
│  └──────┬──────┘    └───────┬────────┘    └────────┬─────────┘      │
│         │                   │                      │                 │
│         └─────────┬─────────┴──────────────────────┘                 │
│                   ▼                                                   │
│          ChannelMessage { id, sender, content, channel, ... }        │
│                   │                                                   │
│                   ▼                                                   │
│          ┌────────────────────────────────────────┐                  │
│          │          AGENT LOOP (loop_.rs)          │                  │
│          │                                        │                  │
│          │  1. Load memory context (recall)       │                  │
│          │  2. Build system prompt                │                  │
│          │  3. Append user msg to history         │                  │
│          │  4. Call Provider.chat(messages, tools) │                  │
│          │          │                              │                  │
│          │          ▼                              │                  │
│          │  ┌──────────────┐                      │                  │
│          │  │ LLM Provider │ (Anthropic/OpenAI/   │                  │
│          │  │              │  Gemini/Ollama/etc)   │                  │
│          │  └──────┬───────┘                      │                  │
│          │         │                              │                  │
│          │         ▼                              │                  │
│          │  ChatResponse { text, tool_calls }     │                  │
│          │         │                              │                  │
│          │    ┌────┴────┐                         │                  │
│          │    │ Tools?  │                         │                  │
│          │    └────┬────┘                         │                  │
│          │    Yes  │  No                          │                  │
│          │    ▼    └──────────────────►  Final    │                  │
│          │  Execute tool(s)              Response  │                  │
│          │    │                              │     │                  │
│          │    ▼                              │     │                  │
│          │  Feed ToolResult back to LLM     │     │                  │
│          │  (loop up to max_iterations=10)  │     │                  │
│          │                                  │     │                  │
│          └──────────────────────────────────┘     │                  │
│                   │                                                   │
│                   ▼                                                   │
│          SendMessage { content, recipient }                           │
│                   │                                                   │
│                   ▼                                                   │
│          Channel.send() → platform-specific API                      │
└──────────────────────────────────────────────────────────────────────┘

---
1. Inbound: Three Entry Points

┌────────────────────────┬─────────────────────┬──────────────────────────┐
│      Entry Point       │    How it works     │        Agent mode        │
├────────────────────────┼─────────────────────┼──────────────────────────┤
│ CLI (zeroclaw agent)   │ Interactive REPL,   │ Full agent with tools    │
│                        │ reads stdin         │                          │
├────────────────────────┼─────────────────────┼──────────────────────────┤
│ Gateway (zeroclaw      │ Axum HTTP server on │ Dual: simple chat        │
│ gateway start)         │  configurable       │ (webhook/WS) or full     │
│                        │ host:port           │ agent (channel webhooks) │
├────────────────────────┼─────────────────────┼──────────────────────────┤
│ Channels (zeroclaw     │ Long-running        │ Full agent with tools,   │
│ daemon or              │ listeners per       │ per-sender history       │
│ start_channels)        │ platform            │                          │
└────────────────────────┴─────────────────────┴──────────────────────────┘

---
2. Gateway Layer (src/gateway/)

The gateway is an Axum HTTP server with these key routes:

- POST /webhook — Generic JSON {"message":"..."} → simple chat (no tools)
- POST /whatsapp, /linq, /wati, /nextcloud-talk — Platform webhooks → full
agent with tools
- GET /ws/chat — WebSocket → streaming chat (currently simple, no tools)
- GET /api/events — SSE event stream for real-time observability

Security layers applied in order:
1. Rate limiting (sliding window per IP, configurable per endpoint)
2. Bearer token auth (obtained via one-time pairing code at POST /pair)
3. Webhook signature verification (HMAC-SHA256 for WhatsApp/Linq/Nextcloud)
4. Idempotency dedup (X-Idempotency-Key header)
5. Request size limit (64KB default) + 30s timeout

---
3. Core Data Structures

Inbound (from channels):
ChannelMessage { id, sender, reply_target, content, channel, timestamp,
thread_ts }

Internal (conversation history):
enum ConversationMessage {
    Chat(ChatMessage { role, content }),                    // 
user/assistant/system
    AssistantToolCalls { text, tool_calls, reasoning },     // LLM wants tools
    ToolResults(Vec<ToolResultMessage>),                    // tool outputs
}

To/from LLM:
ChatRequest  { messages: Vec<ChatMessage>, tools: Option<ToolsPayload> }
ChatResponse { text, tool_calls: Vec<ToolCall>, usage, reasoning_content }

Outbound (to channels):
SendMessage { content, recipient, subject, thread_ts }

---
4. Agent Loop (src/agent/loop_.rs)

This is the core orchestration — run_tool_call_loop():

1. Memory injection — mem.recall(user_msg) retrieves relevant past context,
prepended to the user message
2. System prompt assembly — Identity + tools + safety rules + workspace info +
datetime + skills + bootstrap files (AGENTS.md, SOUL.md, etc.)
3. Provider call — provider.chat(ChatRequest { messages, tools }) →
ChatResponse
4. Tool dispatch — Two strategies:
- Native (NativeToolDispatcher): Uses provider's structured tool calling
(Anthropic/OpenAI/Gemini)
- Prompt-guided (XmlToolDispatcher): Parses <tool_call>JSON</tool_call> from
text — used for providers without native tool support
5. Tool execution — Security policy check → tool.execute(args) → ToolResult
- Parallel execution supported (configurable)
- Credential scrubbing on tool output before feeding back
- Deduplication of identical tool calls in same turn
6. Loop — Results fed back to LLM, repeat until no more tool calls or
max_tool_iterations (default 10)
7. History management — Capped at 50 messages per sender, with auto-compaction
via LLM summarization

---
5. Channels Layer (src/channels/)

Each channel implements the Channel trait:

trait Channel {
    fn name(&self) -> &str;
    async fn send(&self, message: &SendMessage);
    async fn listen(&self, tx: Sender<ChannelMessage>);  // long-running
    async fn health_check(&self) -> bool;
    // Optional: typing indicators, draft updates, reactions, pinning
}

Channel startup (start_channels):
1. Creates resilient provider (with fallback chain)
2. Collects configured channels from config
3. Spawns supervised listener per channel (exponential backoff 2s→60s)
4. All listeners feed into a shared mpsc::channel(100) message bus
5. Main loop processes messages with 4 concurrent in-flight per channel
6. Per-sender conversation history maintained in HashMap<sender_id, 
Vec<ChatMessage>>

Progressive streaming — Channels that support it (Telegram, Discord, Slack):
- send_draft() → creates initial message
- update_draft() → edits in-place as tokens arrive (rate-limited ~750ms)
- finalize_draft() → applies final formatting

---
6. Providers Layer (src/providers/)

Each provider implements the Provider trait:

trait Provider {
    async fn chat(&self, request: ChatRequest, model: &str, temp: f64) ->
ChatResponse;
    fn capabilities(&self) -> ProviderCapabilities;  // native_tool_calling, 
vision
    fn convert_tools(&self, tools: &[ToolSpec]) -> ToolsPayload;
    // + simple_chat, chat_with_history, streaming variants, warmup
}

40+ providers supported — Anthropic, OpenAI, Gemini, Ollama, OpenRouter,
Bedrock, Azure, Groq, DeepSeek, Mistral, and many more (most via an
OpenAiCompatibleProvider wrapper).

Resilient provider wraps the primary with a fallback chain (e.g., Claude →
GPT-4o → GPT-3.5).

---
7. Security in the Message Path (src/security/)

The SecurityPolicy gates every tool execution:
- AutonomyLevel: ReadOnly / Supervised / Full
- Workspace boundary: restricts file/shell ops to workspace dir
- Command risk classification: Low/Medium/High
- Rate limiting: max_actions_per_hour
- Cost cap: max_cost_per_day_cents
- Approval manager: Interactive CLI prompt for risky ops in Supervised mode

---
Concrete Example: Telegram Message → Response

1. User sends "list my cron jobs" in Telegram
2. TelegramChannel.listen() polls getUpdates, yields ChannelMessage
3. Message bus delivers to main loop
4. Agent loads memory context for this sender
5. System prompt built (identity + tools + safety + context)
6. Provider.chat() called with messages + tool specs
7. LLM returns: tool_call { name: "shell", args: { command: "crontab -l" } }
8. SecurityPolicy checks: shell allowed? workspace boundary? risk level?
9. ShellTool.execute({ command: "crontab -l" }) → ToolResult { output: "..." }
10. Result fed back to LLM
11. LLM returns final text: "Here are your cron jobs: ..."
12. TelegramChannel.send() → sendMessage API (chunked if >4096 chars,
Markdown→HTML)
13. User sees response in Telegram

The architecture is fully trait-driven — extending it means implementing
Channel, Provider, Tool, or Memory and registering in the respective factory
module.
