# plugctx — Pluggable Context System Spec

## Overview

plugctx is a built-in command system that lets users load and unload structured context into the agent's system prompt. Contexts are organized as folders on a configurable filesystem root, addressed by dot-notation paths (e.g., `general.image.generation`). Unlike tool-call-based memory, plugctx is user-driven, reversible, and token-aware.

## Configuration

```yaml
# config.yaml
plugctx:
  ctx_root: .workspace/contexts        # single configurable root
  auto_load:                            # loaded on session start
    - general.coding
    - project.miniclaw
```

## Filesystem Layout

```
$CTX_ROOT/
├── general/
│   ├── coding/
│   │   ├── manifest.yaml
│   │   ├── CONTEXT.md
│   │   └── python/
│   │       ├── manifest.yaml
│   │       └── CONTEXT.md
│   └── image/
│       ├── manifest.yaml
│       ├── CONTEXT.md
│       └── generation/
│           ├── manifest.yaml
│           └── CONTEXT.md
└── project/
    └── miniclaw/
        ├── manifest.yaml
        └── CONTEXT.md
```

Each context is a folder containing:

| File | Required | Purpose |
|------|----------|---------|
| `CONTEXT.md` | Yes | Pure content injected into system prompt |
| `manifest.yaml` | No | Metadata, dependencies, future middleware declarations |

## manifest.yaml Schema

### MVP Fields

```yaml
name: image-generation              # human-readable name
description: "Context for ..."      # one-liner
requires:                           # auto-resolved dependencies (deduped)
  - general.image
tags: [image, generation]           # for discoverability in /plugctx list
```

### Future Fields (middleware contract — design only, not implemented in MVP)

```yaml
tools:
  add: [remote_shell]              # register additional tools when loaded
  remove: [shell]                  # hide tools from agent when loaded

hooks:
  tool_pre:
    - match: "shell.*"             # glob pattern on tool names
      action: ssh_wrap             # action resolved from hook registry
  tool_post:
    - match: "file_*"
      action: sync_back
```

Middleware semantics:
- `tools.add` / `tools.remove`: modify the tool registry for the session while the context is loaded. Reverted on unload.
- `hooks.tool_pre`: intercept tool calls before execution. The `action` references a registered hook handler. Transparent to the agent.
- `hooks.tool_post`: intercept tool results after execution. Same resolution.
- Hook handlers are registered separately (future design — likely a Python plugin discovery mechanism).

## CLI Interface

All commands are built-in (not tools — no token cost for invocation).

### `/plugctx load <dotted.path>`

1. Resolve `dotted.path` to `$CTX_ROOT/dotted/path/`
2. Verify `CONTEXT.md` exists; error if missing
3. Parse `manifest.yaml` (if present) for `requires`
4. Auto-resolve dependencies recursively (skip already-loaded; detect cycles)
5. Load `CONTEXT.md` content into session's active context set
6. Display: loaded context name, token estimate, and any auto-loaded deps
7. If the folder has child contexts, list them:
   ```
   Loaded: general.image (~320 tokens)
   Auto-loaded dependency: general.coding (~450 tokens)
   Available sub-contexts: general.image.generation, general.image.editing
   ```

### `/plugctx unload <dotted.path>`

1. Remove the context from the session's active set
2. Do NOT auto-unload dependents (they may still be useful standalone)
3. Warn if other loaded contexts declared this as a dependency
4. Display confirmation with freed token estimate

### `/plugctx list`

Show all available contexts in `$CTX_ROOT` as a tree, with loaded status:

```
general/
  coding/          [loaded] (~450 tokens)
    python/        [available]
  image/           [available]
    generation/    [available]
    editing/       [available]
project/
  miniclaw/        [loaded] (~280 tokens)
```

### `/plugctx status`

Show currently loaded contexts with details:

```
Loaded contexts (3 total, ~1050 tokens):
  1. general.coding        (~450 tokens)  [auto-loaded]
  2. general.image         (~320 tokens)  [manual]
  3. project.miniclaw      (~280 tokens)  [auto-loaded]
```

### `/plugctx info <dotted.path>`

Show a context's manifest, dependencies, and CONTEXT.md preview:

```
general.image.generation
  Description: Context for image generation workflows
  Tags: image, generation
  Requires: general.image
  Tokens: ~520
  ---
  # Image Generation Context       (first 5 lines of CONTEXT.md)
  You are an expert in ...
  ...
```

## System Prompt Integration

Loaded contexts are injected into the system prompt as a dedicated section, ordered by load sequence:

```
[base system prompt]

[tool list]

--- Loaded Contexts ---

<!-- context: general.coding -->
{content of general/coding/CONTEXT.md}

<!-- context: project.miniclaw -->
{content of project/miniclaw/CONTEXT.md}

--- End Contexts ---

[memory context (if enabled)]
```

- Load order determines injection order (user controls by loading sequence)
- Auto-loaded contexts (from config) are loaded in config declaration order
- Dependency-resolved contexts are loaded before their dependents
- Context delimiters (HTML comments) are included for debuggability

## Dependency Resolution

Follows Python-import semantics:

1. When loading `A`, parse `A/manifest.yaml` for `requires: [B, C]`
2. For each dependency, recursively resolve its dependencies
3. Skip any context already in the session's loaded set (dedup)
4. Detect cycles (A requires B requires A) and error with a clear message
5. Load in dependency order: deepest deps first, then the requested context

Example:
```
/plugctx load general.image.generation

Resolution order:
  1. general.image (dependency, not yet loaded)
  2. general.image.generation (requested)
```

## Session Persistence

The loaded context set is part of `SessionState`:

```python
@dataclass
class SessionState:
    history: list[ChatMessage]
    model: str | None
    loaded_contexts: list[str]     # ordered list of dotted paths
    # ... existing fields
```

On session resume (`/resume`), contexts are re-loaded from disk using the persisted paths. If a context file has changed on disk since the session was saved, the updated content is used. If a context path no longer exists, a warning is shown and it's skipped.

## Token Tracking

- Token estimates are computed using a fast tokenizer approximation (e.g., `len(text) / 4` or tiktoken if available)
- Displayed on load, unload, status, and list commands
- No hard budget enforcement — user decides what to load

## Integration Points

### Where plugctx touches the codebase

| Component | Change |
|-----------|--------|
| `config.py` | Add `plugctx` config section parsing |
| `session.py` | Add `loaded_contexts` to `SessionState`; context load/unload methods |
| `agent/native.py` | Inject loaded context content into system prompt construction |
| `agent/cc.py` | Inject loaded context content into SDK system prompt append |
| `listeners/cli.py` | Register `/plugctx` command with subcommand dispatch |
| `persistence.py` | Persist/restore `loaded_contexts` in session save/load |
| New: `plugctx/` | Module for context resolution, loading, dependency graph, token estimation |

### New module: `miniclaw/plugctx/`

```
miniclaw/plugctx/
├── __init__.py           # public API: PlugCtxManager
├── resolver.py           # path resolution, dependency graph, cycle detection
├── loader.py             # read CONTEXT.md + manifest.yaml, token estimation
└── registry.py           # tracks loaded contexts per session, ordering
```

## MVP Scope

**In scope:**
- Configurable `ctx_root`
- Folder contract: `CONTEXT.md` + optional `manifest.yaml`
- CLI commands: `load`, `unload`, `list`, `status`, `info`
- Dependency auto-resolution with dedup and cycle detection
- System prompt injection (both NativeAgent and CCAgent)
- Session persistence of loaded contexts
- Token estimation display
- Config-based auto-load

**Out of scope (future):**
- Middleware: `tools.add/remove`, `hooks.tool_pre/tool_post`
- Remote context registries (install from URL)
- Context versioning
- Semantic search over available contexts
- Per-context token budgets
