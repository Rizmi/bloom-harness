# AGENTS.md — Bloom Harness Developer Guide

> **Core Rule**: Minimal everything. Zero bloat. No fallbacks. No silent defaults. Direct work or raise an error. Never write 100 lines for a 10-line job.

---

## 1. Golden Invariants (Non-Negotiable)

1. **Direct Work, No Fallbacks, No Silent Defaults**:
   - Never implement silent fallback chains that swallow errors or guess user intent.
   - Never invent silent defaults when required config, env vars (`${VAR}`), manifests, agents, models, or tool inputs are missing/invalid — fail immediately with an explicit error.
2. **Preserve KV Prefix Cache**:
   - Keep `messages[0]` deterministic and frozen at prefix byte 0 (`llm.SYSTEM_PROMPT` with sorted tool/skill ordering). Never inject timestamps, turn numbers, or mutable state into the static prefix.
   - Keep transcripts strictly append-only; never rewrite earlier turns in-place (rewinds append `{"rewind_to": N}`, compactions append `{"compacted": [...]}` with `clean_base` first and `<summary>` appended after it).
   - Register only the two static meta-tools (`tool_describe`, `tool_run`) with the LLM so tool schemas never mutate mid-session or bloat context.
3. **Minimal, Domain-Agnostic Code**:
   - Keep functions dense and standard-library-driven (`openai`, `rich`, `prompt-toolkit` only). Never add wrappers, abstractions, or microservices.
   - `/mnt/Data/Dev/Projects/bloom-herness` is the **only** source of truth for Bloom (`bloom/*.py`) and the default Web UI (`web/`). Both must stay 100% domain-agnostic and receive verbatim copies in consumer apps (never edit copied files downstream).

---

## 2. Plug-and-Play & Strict Per-Agent Scoping

Everything in Bloom is plug-and-play via JSON/Markdown configs and **strictly scoped to the active agent**:

1. **Agents (`agents/<role>.json`)**: CLI requires `--agent` explicitly; server pins `_PINNED_AGENT` and rejects cross-agent requests (`?agent=` / `X-Agent`) with `400`.
2. **MCP Servers (`bloom/servers.json` + `permissions.{allow,ask}`)**:
   - **Gate 1 (Machine Registry):** Servers not declared in `servers.json` are skipped at startup. Unresolved `${ENV_VAR}` disables that server fail-closed.
   - **Gate 2 (Per-Agent Permissions):** Strictly default-deny. Every tool must be individually listed in `permissions.allow` (runs immediately) or `permissions.ask` (CLI `ui.approve()`, Server in-memory `asyncio.Future` + `permission` SSE). **Never use wildcards** (`server.*`).
3. **Procedural Skills (`bloom/skills.json` + `skill_paths` / `skills`)**:
   - **Gate 1:** Root directories must be registered in `bloom/skills.json`.
   - **Gate 2:** Agent `skill_paths` must stay inside Gate 1 roots; if `skills` list is present, only explicitly listed skill folder names are exposed/loadable via `skills.skill_load`.
4. **Workspace Path Scopes (`read_paths`, `write_paths`)**: All filesystem operations resolve within the agent's declared paths with path-traversal blocked; empty `write_paths: []` enforces read-only mode.
5. **Models (`bloom/models.json`)**: Pluggable OpenAI-compatible entries with strict `${ENV_VAR}` key resolution; `/api/model` redacts `api_key` before returning responses.

---

## 3. Architecture & File Layout

```text
bloom-herness/
├── bloom/
│   ├── __main__.py      # Module & directory CLI entrypoint
│   ├── agent.py         # Terminal interactive CLI loop & --serve launcher
│   ├── commands.py      # Slash commands (/help, /rewind, /sessions, /compact)
│   ├── compact.py       # KV-preserving context budget & summary compaction
│   ├── history.py       # Transcript output cap & token estimator
│   ├── llm.py           # OpenAI client, models.json loader, runtime switch
│   ├── manifest.py      # Safe agent manifest resolution & ${ENV} expansion
│   ├── mcp_client.py    # Local & external MCP server loader/caller
│   ├── prompt.py        # System prompt builder
│   ├── runner.py        # Shared unified turn engine (CLI & Server)
│   ├── server.py        # Pure stdlib asyncio HTTP/1.1 & SSE API server
│   ├── session.py       # Append-only JSONL persistence & per-session cursors
│   ├── skills.py        # Gate 1 & 2 skill discovery & authorization
│   ├── tools.py         # Two-meta-tool dispatch & allow/ask/deny enforcement
│   ├── ui.py            # Rich terminal rendering
│   ├── models.json      # Base model catalog
│   ├── servers.json     # Machine MCP server registry
│   └── skills.json      # Machine skills root registry
├── web/                 # Optional default Web Component UI (strict /api/* client)
│   ├── api-client.js    # Dependency-free REST & SSE SDK
│   ├── bloom-chat.js    # Shadow-DOM <bloom-chat> custom element
│   ├── index.html       # Standalone launcher shell
│   └── lib/             # Offline vendored JS libs (never name vendor/ for sync tools)
├── mcp/
│   ├── filesystem/      # Bundled scoped filesystem MCP server
│   └── skills/          # Bundled scoped skills discovery MCP server
└── tests/
    ├── test_harness.py  # Core harness, compaction, permissions, MCP/skill gate tests
    └── test_server.py   # HTTP REST, SSE, auth, upload, compact/rewind, static tests
```

---

## 4. Server & Web Contract (`bloom/server.py`)

`bloom/server.py` is purely headless HTTP + SSE (`web/` talks exclusively over `/api/*`; passing `--web` only serves `web/` statically with path-traversal checks and no UI logic in Python). Auth (`BLOOM_API_TOKEN`) applies to all `/api/*` routes.

- **Discovery & Config Endpoints**:
  - `GET /api/info` → `{ status, model, version, active_agent, active_model, context_limit, agents, models }`
  - `GET /api/agents` / `GET /api/agents/{name}` → Safe agent manifest summaries (scoped by `resolve_safe`)
  - `GET /api/model` / `GET /api/models` → `{ status, current, active, groups, models }` (`api_key` redacted)
  - `POST /api/model` / `POST /api/models/switch` → Switches active model via `llm.switch_model()` (`409` if turn busy)
  - `GET /api/tools` / `GET /api/tools/{name}/describe` → Scoped `allow`/`ask` tool list & JSON schema
  - `GET /api/skills` / `GET /api/skills/{name}` → Authorized skill list & `skills.skill_load` content
  - `GET /api/mcp` → Connected local & external MCP servers

- **Session, Upload & Turn Execution Endpoints**:
  - `GET /api/sessions` → Lists saved session IDs & titles (newest first)
  - `GET /api/session/active` → `{ data: { <sid>: true } }` for running turn tasks
  - `POST /api/session` → Creates/opens custom session `id` idempotently (`201` new / `200` existing)
  - `GET /api/session/{id}` → Raw transcript messages (`404` if missing)
  - `DELETE /api/session/{id}` → Deletes transcript (`404` if missing, `409` if busy)
  - `GET /api/session/{id}/message` → `{ data: [...], total, offset, limit, usage }` (`limit`/`offset` opt-in)
  - `GET /api/session/{id}/usage` → Usage metrics (`promptTokens`, `candidatesTokens`, `reasoningTokens`, `thoughtsTokens`, `totalTokens`, `cachedTokens`, `contextTokens`, `contextLimit`)
  - `POST /api/session/{id}/prompt` → Streams SSE turn execution (rejects `/...` slash commands with `400`; `409` if busy)
  - `POST /api/session/{id}/compact` → Runs compaction with explicit `session_id` and persists/returns updated `usage` estimate
  - `GET /api/session/{id}/rewind-candidates` & `POST /api/session/{id}/rewind` → Lists valid checkpoints and rewinds transcript + updates persisted `usage` estimate
  - `POST /api/session/{id}/abort` → Cancels active turn task and pending approvals
  - `GET /api/permissions` → Pending `ask`-tier confirmation requests (optional `?sessionId=`)
  - `POST /api/session/{id}/approval` → Resolves `asyncio.Future` and returns JSON `{ status: "resolved", requestId, approved, success, decision }`
  - `POST /api/attach` → Saves binary upload up to `BLOOM_MAX_ATTACH_BYTES` (25 MB limit) into `BLOOM_UPLOADS_DIR` (defaults to `uploads/`)
  - `GET /`, `GET /web/*` *(only with `--web`)* → Read-only static serving from `web/` (auth-exempt static bytes; path traversal rejected)

- **SSE Event Vocabulary**:
  - `start` (`sessionId`, `agent`)
  - `thinking`
  - `compacted` (`before`, `after`)
  - `delta` (`field: "text"|"reasoning"`, `text`)
  - `tool` (`state: "running"|"completed"`, `callId`, `tool`, `arguments`, `input`, `output`)
  - `permission` (`requestId`, `sessionId`, `tool`, `arguments`, `prompt`, `action`, `hint`, `resources`)
  - `permission_settled` (`requestId`, `sessionId`, `approved`)
  - `usage` (`promptTokens`, `completionTokens`, `candidatesTokens`, `reasoningTokens`, `thoughtsTokens`, `totalTokens`, `cachedTokens`, `contextTokens`, `contextLimit`, `contextPercentage`, `raw_usage`)
  - `error` (`error`)
  - `done` (`reason: "stop"|"max_rounds"|"aborted"|"error"`)
  - Keepalive comment `: ping` every 15s (cancels turn on dead socket only if no pending approval is waiting)

---

## 5. Development & Sync Workflow

1. **Edit only in `bloom-herness`** (`bloom/`, `web/`, `mcp/`, `tests/`). Keep `bloom/` and `web/` generic.
2. **Verify before finishing**:
   ```bash
   cd /mnt/Data/Dev/Projects/bloom-herness
   uv run python -m unittest tests.test_harness tests.test_server
   ```
   *All reported tests must pass 100%.*
3. **Sync downstream**: Copy `bloom/*.py` and `web/` verbatim into consumer projects — **never** overwrite consumer `servers.json`, `skills.json`, `models.json`, or `agents/*.json`.
