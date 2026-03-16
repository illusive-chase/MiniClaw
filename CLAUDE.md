# MiniClaw

Python agent runtime connecting LLM providers to messaging channels.

Two agent backends: **NativeAgent** (custom tool loop, stateless) and **CCAgent** (claude-agent-sdk wrapper, stateful).

```
Listener → submit() → Session → Agent.process() → AgentEvent stream → Channel
```

Entry points: `miniclaw` (NativeAgent), `minicode` (CCAgent). Config: `config.yaml`.

For detailed architecture, internals, and dev workflow rules, load the dev context: `/plugctx load dev.cc.general`
