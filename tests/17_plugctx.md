# Test: PlugCtx — structured context loading for agent system prompts

**Feature**: PlugCtx lets users load/unload structured context folders into the agent's system prompt. Contexts live as folders under a configurable root (`ctx_root`), addressed by dot-notation paths (e.g., `general.coding`). Each folder has `CONTEXT.md` (required) + optional `manifest.yaml` with dependencies. CLI commands: `/plugctx load|unload|list|status|info`.

**Modules**: `miniclaw/plugctx/` (loader, resolver, registry, PlugCtxManager), session injection, agent prompt reading, CLI commands, runtime lifecycle.

---

## Prerequisites

```bash
cd mini-agent

# Create sample context folders for testing
mkdir -p .workspace/contexts/general/coding
mkdir -p .workspace/contexts/general/testing
mkdir -p .workspace/contexts/project/myapp

cat > .workspace/contexts/general/coding/CONTEXT.md << 'EOF'
# Coding Guidelines

- Use type hints on all function signatures
- Prefer composition over inheritance
- Keep functions under 30 lines
EOF

cat > .workspace/contexts/general/testing/CONTEXT.md << 'EOF'
# Testing Standards

- Every module must have unit tests
- Use pytest as the test framework
- Aim for 80% coverage
EOF

cat > .workspace/contexts/general/testing/manifest.yaml << 'EOF'
name: Testing Standards
description: Project-wide testing guidelines
requires:
  - general.coding
tags:
  - qa
  - testing
EOF

cat > .workspace/contexts/project/myapp/CONTEXT.md << 'EOF'
# MyApp Project

MyApp is a REST API built with FastAPI.
Entry point: src/main.py
EOF
```

---

## Test 1: /plugctx list — discover available contexts

**Steps**:
1. Start `python main.py`
2. Type `/plugctx list`

**Expected Behavior**:
- A "Contexts" panel with cyan border appears
- Lists all three contexts: `general.coding`, `general.testing`, `project.myapp`
- None are marked as loaded (no green `*` marker)

---

## Test 2: /plugctx load — load a single context

**Steps**:
1. Type `/plugctx load general.coding`

**Expected Behavior**:
- Output shows: `+ general.coding`
- Shows total token count: `Total context tokens: ~XX`
- No errors, no failed loads

---

## Test 3: /plugctx load — dependency auto-resolution

**Steps**:
1. If `general.coding` is already loaded, type `/plugctx unload general.coding` first
2. Type `/plugctx load general.testing`

**Expected Behavior**:
- Two contexts loaded: `general.coding` (as dependency) and `general.testing`
- Output shows both with `+` markers
- `general.coding` is loaded before `general.testing` (dependency order)
- Total token count reflects both contexts

---

## Test 4: /plugctx load — already loaded context

**Steps**:
1. Ensure `general.coding` is loaded (from Test 3)
2. Type `/plugctx load general.coding`

**Expected Behavior**:
- Shows: `(already loaded) general.coding`
- No duplicate loading, token count unchanged

---

## Test 5: /plugctx status — view loaded contexts

**Steps**:
1. Ensure at least one context is loaded
2. Type `/plugctx status`

**Expected Behavior**:
- A "Loaded Contexts" panel with cyan border
- Each loaded context shows: path, token estimate, source (manual/dependency)
- Contexts loaded as dependencies show source `dependency`
- Total token count at the bottom

---

## Test 6: /plugctx info — view context details

**Steps**:
1. Type `/plugctx info general.testing`

**Expected Behavior**:
- A panel showing:
  - Path: `general.testing`
  - Name: `Testing Standards`
  - Desc: `Project-wide testing guidelines`
  - Requires: `general.coding`
  - Tags: `qa, testing`
  - Token estimate
  - Loaded status (yes/no)
  - Content preview (first 500 chars)

---

## Test 7: /plugctx unload — unload with dependent warning

**Steps**:
1. Load both `general.coding` and `general.testing` (via `/plugctx load general.testing`)
2. Type `/plugctx unload general.coding`

**Expected Behavior**:
- Yellow warning: `Warning: the following loaded contexts depend on 'general.coding': general.testing`
- Context is still unloaded (warning is advisory, not blocking)
- Shows freed token count

---

## Test 8: /plugctx unload — unload non-loaded context

**Steps**:
1. Type `/plugctx unload nonexistent.ctx`

**Expected Behavior**:
- Shows: `Context 'nonexistent.ctx' is not loaded.`

---

## Test 9: /plugctx load — nonexistent context

**Steps**:
1. Type `/plugctx load does.not.exist`

**Expected Behavior**:
- Shows: `! does.not.exist (not found)`
- No crash, graceful error handling

---

## Test 10: System prompt injection — NativeAgent

**Steps**:
1. Start `python main.py`
2. Type `/plugctx load general.coding`
3. Send a message: `What coding guidelines should I follow?`

**Expected Behavior**:
- The agent's response references the loaded context content ("type hints", "composition over inheritance", "30 lines")
- The loaded context is visible in the system prompt (check debug logs for `[NATIVE] System prompt:` entries)
- The `--- Loaded Contexts ---` block appears in the system prompt

---

## Test 11: System prompt injection — CCAgent

**Command**:
```bash
python cc_main.py
```

**Steps**:
1. Type `/plugctx load general.coding`
2. Send a message: `What coding guidelines should I follow?`

**Expected Behavior**:
- The CCAgent's response references the loaded context content
- The context is appended to the SDK system prompt via `system_prompt.append`

---

## Test 12: No context loaded — no injection

**Steps**:
1. Start fresh (no contexts loaded)
2. Send a message: `Hello`

**Expected Behavior**:
- Normal behavior, no `--- Loaded Contexts ---` block in the system prompt
- No errors related to plugctx

---

## Test 13: /plugctx — no subcommand

**Steps**:
1. Type `/plugctx`

**Expected Behavior**:
- Shows usage: `Usage: /plugctx <load|unload|list|status|info> [args]`

---

## Test 14: /plugctx status — no contexts loaded

**Steps**:
1. Ensure no contexts are loaded (unload all or start fresh)
2. Type `/plugctx status`

**Expected Behavior**:
- Shows: `No contexts loaded.`

---

## Test 15: Persistence — loaded contexts survive /resume

**Steps**:
1. Load a context: `/plugctx load general.coding`
2. Send a message to build history
3. Note the session ID from `/sessions`
4. Type `/reset`
5. Type `/resume <session_id>`
6. Type `/plugctx status`

**Expected Behavior**:
- After resume, `general.coding` is still loaded
- The persisted session JSON contains `metadata.loaded_contexts: ["general.coding"]`
- `/plugctx status` shows the context as loaded

---

## Test 16: Persistence — fork copies loaded contexts

**Steps**:
1. Load contexts: `/plugctx load general.testing` (loads coding as dependency too)
2. Send a message to build history
3. Note the session ID from `/sessions`
4. Type `/fork <session_id>`
5. Type `/plugctx status`

**Expected Behavior**:
- The forked session has the same contexts loaded as the source
- Both `general.coding` and `general.testing` appear in status

---

## Test 17: Config auto-load

**Steps**:
1. Add to `config.yaml`:
   ```yaml
   plugctx:
     ctx_root: ".workspace/contexts"
     auto_load:
       - general.coding
   ```
2. Start `python main.py`
3. Type `/plugctx status`

**Expected Behavior**:
- `general.coding` is automatically loaded at startup
- Status shows it as loaded

---

## Test 18: Circular dependency detection

**Setup**:
```bash
mkdir -p .workspace/contexts/cycle/a
mkdir -p .workspace/contexts/cycle/b

echo "# Context A" > .workspace/contexts/cycle/a/CONTEXT.md
cat > .workspace/contexts/cycle/a/manifest.yaml << 'EOF'
requires:
  - cycle.b
EOF

echo "# Context B" > .workspace/contexts/cycle/b/CONTEXT.md
cat > .workspace/contexts/cycle/b/manifest.yaml << 'EOF'
requires:
  - cycle.a
EOF
```

**Steps**:
1. Type `/plugctx load cycle.a`

**Expected Behavior**:
- Error message containing "Circular dependency"
- The cycle path is shown: `cycle.a -> cycle.b -> cycle.a`
- No contexts are loaded (load is aborted)

---

## Test 19: /help includes /plugctx

**Steps**:
1. Type `/help`

**Expected Behavior**:
- The help panel includes: `/plugctx <cmd>    Manage loaded contexts (load/unload/list/status/info)`

---

## Test 20: plugctx not configured — graceful handling

**Steps**:
1. Remove `plugctx` section from `config.yaml` (or set `plugctx: null`)
2. Start `python main.py`
3. Type `/plugctx list`

**Expected Behavior**:
- Shows: `plugctx is not configured for this session.`
- No crash, normal operation continues
