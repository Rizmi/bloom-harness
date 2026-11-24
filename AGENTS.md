# AGENTS.md — Bloom Harness Developer Guide

> **Core Rule**: Keep everything minimal. Zero bloat. No fallbacks. Direct work or raise an error. Never write 100 lines for a 10-line job.

---

## 1. Golden Invariants

1. **Direct Work or Fail-Fast**:
   - Never implement silent fallback chains that swallow errors or guess user intent. If configuration is missing or invalid, fail immediately with a clear error.
2. **Single Responsibility & Minimal Code**:
   - Keep functions concise, dense, and standard-library-driven. Bloom's dependencies are strictly `openai`, `rich`, and `prompt-toolkit`.
   - Never introduce unnecessary abstraction layers, wrappers, or microservices.
3. **Single Source of Truth**:
   - `/mnt/Data/Dev/Projects/bloom-herness` is the **only** place where Bloom Python code (`bloom/*.py`) is developed or edited.
   - Applications that vendor Bloom receive copies of `.py` files from this repo. Never edit vendored `.py` files in consumer apps.

---

## 2. Architecture & File Layout

```text
bloom-herness/
├── bloom/
│   ├── agent.py         # Terminal interactive CLI loop & entrypoint
│   ├── commands.py      # Slash commands (/help, /rewind, /sessions, /compact)
│   ├── compact.py       # Context window budget & summary compaction
│   ├── history.py       # Transcript formatting
│   ├── llm.py           # OpenAI client, model catalog, and switch logic
│   ├── manifest.py      # Agent manifest discovery & resolution
│   ├── mcp_client.py    # Local and external MCP transport & execution
│   ├── prompt.py        # Static & dynamic prompt builders
│   ├── runner.py        # Shared unified turn execution engine (CLI & Server)
│   ├── server.py        # Headless HTTP & SSE API server
│   ├── session.py       # JSONL session persistence & cursor management
│   ├── skills.py        # Gate 1 & 2 procedural skill discovery & loading
│   ├── tools.py         # Permission enforcement (allow/ask) & tool dispatch
│   ├── ui.py            # Rich terminal rendering
│   ├── models.json      # Base model catalog
│   ├── servers.json     # MCP server registry
│   └── skills.json      # Skills root directory registry
├── mcp/
│   ├── filesystem/      # Bundled local filesystem MCP server
│   └── skills/          # Bundled local skills discovery MCP server
└── tests/
    ├── test_harness.py  # Unit tests for CLI, compaction, permissions, tools
    └── test_server.py   # Unit tests for REST endpoints, SSE streams, locks
```

---

## 3. Server & Dashboard Contract

The headless server (`bloom/server.py`) powers dashboard integrations via HTTP and SSE:

- **Endpoints**:
  - `GET /api/info` → Returns `{ status: "ready", model: MODEL_KEY, agents: [...], agent: PINNED_AGENT }`.
  - `GET /api/model` → Returns `{ status: "ready", current: MODEL_KEY, groups: [...] }`.
  - `POST /api/model` → Switches active model via `llm.switch_model()`.
  - `POST /api/session` → Accepts custom session IDs (`id`), resets `CURRENT` and `WRITTEN=0`, initializes system prompt.
  - `GET /api/session/{id}/message` → Returns `{ data: [ADK formatted parts], usage: {...} }`. Returns 404 if session doesn't exist.
  - `GET /api/session/active` → Returns `{ data: { <sid>: true } }` for active turns.
  - `GET /api/session/{id}/usage` → Returns token metrics object.
  - `POST /api/session/{id}/prompt` → Streams SSE turn execution.
  - `POST /api/session/{id}/abort` → Aborts running turn task and clears pending approvals.
  - `GET /api/permissions` → Returns pending tool confirmation gates.
  - `POST /api/session/{id}/approval` → Resolves confirmation gate in memory and immediately returns JSON `{ status: "resolved", approved: bool }`. Never opens a second SSE stream.
  - `DELETE /api/session/{id}` → Deletes session transcript file.

- **SSE Event Vocabulary**:
  - `delta` (`field: "text"|"reasoning"`, `text: "..."`)
  - `tool` (`state: "running"|"completed"`, `callId`, `tool`, `arguments`, `output`) — *Unified, no separate tool_call/tool_result duplicates.*
  - `permission` (`requestId`, `tool`, `arguments`, `action`, `hint`, `resources`)
  - `permission_settled` (`requestId`, `sessionId`, `approved`)
  - `usage` (`promptTokens`, `candidatesTokens`, `totalTokens`, `cachedTokens`, `contextTokens`, `contextLimit`)
  - `done` (`reason: "stop"|"max_rounds"|"aborted"|"error"`)
  - `ping` (15s heartbeat keepalive)

---

## 4. MCP & Skill Two-Gate Security Model

1. **Gate 1 (Machine Registry)**:
   - MCP servers must be explicitly declared in `servers.json`. If a server is not in `servers.json`, its tools are skipped at startup.
   - Skill root directories must be declared in `skills.json`.
2. **Gate 2 (Agent Manifest Permissions)**:
   - Declared in `agents/<agent>.json`.
   - `permissions.allow`: Tools executed immediately without confirmation.
   - `permissions.ask`: Tools that pause execution and require human confirmation:
     - **CLI mode**: Interactive `[y/N]` prompt in terminal via `ui.approve()`.
     - **Server mode**: Held in an in-memory `asyncio.Future` and emitted via `permission` SSE until approved via `POST /api/session/{id}/approval`.
   - **Never use wildcards** (`clients.*`). Every tool must be individually enumerated.

---

## 5. Development & Sync Workflow

1. **Make changes here in `bloom-herness`**:
   - Write or update code in `bloom/` or `mcp/`.
2. **Test twice**:
   ```bash
   cd /mnt/Data/Dev/Projects/bloom-herness
   PYTHONPATH=. .venv/bin/python -m unittest discover tests
   ```
   *All 98 tests must pass 100%.*
3. **Commit**:
   ```bash
   git commit -am "feat/fix: description"
   ```
4. **Sync to Consumer Applications**:
   - Copy updated `bloom/*.py` files to your consumer application's `bloom/` directory.
   - **DO NOT overwrite** consumer configuration files (`servers.json`, `skills.json`, or `models.json`).
