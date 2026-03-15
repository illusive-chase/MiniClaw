# Test: AgentConfig — per-session configuration

**Feature**: AgentConfig is owned by Session and passed to the agent on every `process()` call. It supports per-session overrides for model, tools, max_iterations, memory, thinking, effort, and extra agent-type-specific settings.

**Architecture Spec**: §4.3 (AgentConfig)

---

## Prerequisites

```bash
cd mini-agent
python main.py
```

---

## Test 1: Default values

**What this tests**: AgentConfig has sensible defaults.

**Verification** (code-level):
```python
from miniclaw.agent.config import AgentConfig
config = AgentConfig()
assert config.model == ""
assert config.system_prompt == ""
assert config.tools is None  # None = all tools allowed
assert config.max_iterations == 30
assert config.memory_enabled is True
assert config.thinking is False
assert config.effort == "medium"
assert config.temperature == 0.7
assert config.extra == {}
```

**Expected Behavior**:
- All fields have documented default values
- `tools=None` means all tools are available

---

## Test 2: Model override via /model

**Steps**:
1. Type `/model` to see current model
2. Type `/model gpt-4o-mini` to change
3. Send a message

**Expected Behavior**:
- `session.agent_config.model` is updated to "gpt-4o-mini"
- The agent uses the overridden model for the next `process()` call
- Provider receives the new model name

---

## Test 3: Effort override via /effort

**Steps**:
1. Type `/effort high`
2. Send a message

**Expected Behavior**:
- `session.agent_config.effort` is updated to "high"
- If the agent supports effort (CCAgent), it adjusts behavior accordingly

---

## Test 4: Tools filtering

**What this tests**: `config.tools` list restricts which tools the agent can use.

**Verification** (code-level):
```python
config = AgentConfig(tools=["file_read", "glob"])
# When passed to agent.process(), only file_read and glob tool specs are included
```

**Expected Behavior**:
- Agent only sees tool specs for tools in the allow list
- Other tools are excluded from the LLM's tool selection
- `tools=None` means all registered tools are available

---

## Test 5: Extra field for agent-specific overrides

**What this tests**: The `extra` dict passes agent-type-specific configuration.

**Verification**:
```python
config = AgentConfig(extra={"custom_setting": "value"})
# Agent implementations can read config.extra["custom_setting"]
```

**Expected Behavior**:
- Extra fields are passed through to the agent
- Unknown keys are safely ignored by agents that don't use them
- Useful for agent-specific extensions without modifying the shared schema
