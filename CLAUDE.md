# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MiniClaw is a minimal Python agent runtime that connects LLM providers to messaging channels with tool-use capabilities. It runs an agentic loop: user message → LLM → tool calls → LLM → reply.

Requires Python 3.12+.

## Agentic Loop

┌─────────────────────────── AGENTIC LOOP FLOW ────────────────────────────┐
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │  CHANNEL (CLIChannel)                                             │   │
│  │  Owns: user I/O, Rich rendering, command dispatch                 │   │
│  │                                                                   │   │
│  │  start() loop:                                                    │   │
│  │    1. stdin.readline() ─── user text ───┐                         │   │
│  │    2. /commands? ──→ CommandRegistry    │                         │   │
│  │    3. regular msg? ─────────────────────┼────────────────┐        │   │
│  │                                         ▼                │        │   │
│  │  send_stream() consumes the AsyncIterator:               │        │   │
│  │    ├─ str chunk    ──→ buffer += chunk, render Markdown  │        │   │
│  │    ├─ ActivityEvent──→ tracker.apply() → footer.update() │        │   │
│  │    ├─ InteractionReq→ pause Live, prompt user, .resolve()│        │   │
│  │    └─ (end)        ──→ live.stop()                       │        │   │
│  └──────────────────────────────────────────────────────────┼────────┘   │
│                                                             │            │
│                              gateway.process_message_stream(sid, text)   │
│                                                             │            │
│  ┌──────────────────────────────────────────────────────────▼────────┐   │
│  │  GATEWAY                                                          │   │
│  │  Owns: sessions, history, per-session locks                       │   │
│  │                                                                   │   │
│  │  process_message_stream(session_id, text):                        │   │
│  │    1. acquire per-session lock                                    │   │
│  │    2. load SessionState (history, model override)                 │   │
│  │    3. call agent.process_message_stream(text, history, model)     │   │
│  │    4. async for item in agent stream:                             │   │
│  │       ├─ str chunk        ──→ yield to channel                    │   │
│  │       ├─ ActivityEvent    ──→ yield to channel                    │   │
│  │       ├─ InteractionReq   ──→ yield to channel                    │   │
│  │       ├─ PlanExecuteAction──→ capture (don't yield)               │   │
│  │       └─ (reply, history) ──→ state.history = history             │   │
│  │    5. if PlanExecuteAction: reset client, replay with plan        │   │
│  └───────────────────────────────────────────────────────────┬───────┘   │
│                                                              │           │
│                              agent.process_message_stream(text, history) │
│                                                              │           │
│  ┌───────────────────────────────────────────────────────────▼───────┐   │
│  │  AGENT (Agent or CCAgent)                                         │   │
│  │  Owns: nothing — stateless per call                               │   │
│  │                                                                   │   │
│  │  ┌─── Agent (native) ─────────────────────────────────────────┐   │   │
│  │  │  process_message():                                        │   │   │
│  │  │    build system prompt + memory context                    │   │   │
│  │  │    loop (max_iterations):                                  │   │   │
│  │  │      ┌──→ provider.chat(messages, tools)                   │   │   │
│  │  │      │    ├─ text only? → return reply                     │   │   │
│  │  │      │    └─ tool_calls? → execute each → append results   │   │   │
│  │  │      └────────────── repeat ──────────────────────────┘    │   │   │
│  │  └────────────────────────────────────────────────────────────┘   │   │
│  │                                                                   │   │
│  │  ┌─── CCAgent (SDK-backed) ───────────────────────────────────┐   │   │
│  │  │  process_message_stream():                                 │   │   │
│  │  │    get_or_create_client(session_id)                        │   │   │
│  │  │    output_queue ←──── can_use_tool callback pushes here    │   │   │
│  │  │    _run_sdk task:                                          │   │   │
│  │  │      client.query(text) → receive_response()               │   │   │
│  │  │    consume queue:                                          │   │   │
│  │  │      ├─ AssistantMsg/TextBlock  ──→ yield str              │   │   │
│  │  │      ├─ AssistantMsg/ToolUseBlock─→ yield ActivityEvent    │   │   │
│  │  │      ├─ TaskStarted/Progress/etc──→ yield ActivityEvent    │   │   │
│  │  │      ├─ interaction tag         ──→ yield InteractionReq   │   │   │
│  │  │      ├─ plan_action tag         ──→ yield PlanExecuteAction│   │   │
│  │  │      └─ done                    ──→ yield (reply, history) │   │   │
│  │  └────────────────────────────────────────────────────────────┘   │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘

STREAM ITEM TYPES (what flows through the async iterator):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  str                → text chunk (progressive display)
  ActivityEvent      → tool/subagent lifecycle (status footer)
  InteractionRequest → permission/question/plan (blocks SDK, user resolves)
  PlanExecuteAction  → gateway-only signal (clear context + re-run)
  (reply, history)   → sentinel (gateway captures to update session state)