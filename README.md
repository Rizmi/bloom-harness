# Bloom Harness

A lightweight, Linux-native, **plug-and-play automation harness** for running LLM-driven automation tasks under strict, per-agent scopes.

Bloom is built around a single principle: **one minimal, immutable harness can drive any automation role** (coding, testing, reviewing, indexing, deployment) by plugging in decoupled JSON manifests, MCP servers, procedural skills, and model configurations without changing harness code.

---

## The Plug-and-Play Architecture

Bloom cleanly separates the execution engine from configuration so everything is hot-pluggable:

| Component | How to Plug It In | What It Controls |
|---|---|---|
| **Agents / Roles** | Drop a `.json` manifest into `agents/` | System prompt, allowed/denied tools, human confirmation gates, read/write path boundaries |
| **Tools (MCP)** | Add an entry to `bloom/servers.json` | Local Python scripts, stdio binaries, or remote Streamable HTTP MCP servers |
| **Skills (Workflows)** | Drop a folder with `SKILL.md` into `skills/` | On-demand procedural SOPs and multi-step runbooks loaded via `skills.skill_load` |
| **LLM Providers** | Add an entry to `bloom/models.json` | Any OpenAI-compatible provider (DeepRelay, OpenRouter, Ollama, vLLM, OpenAI) |
| **Target Workspaces** | Run `bloom` in any target folder or set `BLOOM_ROOT` | File operations resolve relative to your target repository with zero harness lock-in |

```text
               ┌─────────────────────────────────────────┐
               │           OpenAI-Compatible LLM         │
               └────────────────────▲────────────────────┘
                                    │ (tool_describe, tool_run)
               ┌────────────────────▼────────────────────┐
               │           Bloom Base Harness            │
               │   Frozen KV Cache Prefix & 2 Meta-Tools │
               └────────────────────┬────────────────────┘
                                    │ Enforces Active Agent Scope
         ┌──────────────────────────┼──────────────────────────┐
         ▼                          ▼                          ▼
┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│  Pluggable Tools │       │ Pluggable Skills │       │ Pluggable Scopes │
│   servers.json   │       │   skills.json    │       │   agents/*.json  │
│ local/stdio/HTTP │       │     SKILL.md     │       │ read/write gates │
└──────────────────┘       └──────────────────┘       └──────────────────┘
```

---

## Core Pillars

1. **One Harness, Arbitrary Roles**
   - Run any automation task through the same engine: `bloom --agent <role>`.
   - Each role is defined in an isolated JSON manifest (e.g. `./agents/<role>.json` or via `$BLOOM_AGENTS`) with explicit boundaries:
     - `read_paths`: Authorized directories the agent can inspect.
     - `write_paths`: Authorized directories the agent can modify (empty `[]` enforces read-only mode).
     - `skill_paths`: Folders containing authorized workflow runbooks (`SKILL.md`).
     - `permissions`: Three-tier gate (`allow`, `ask`, `deny`):
       - `allow`: Safe operations executed autonomously without stopping.
       - `ask`: Destructive or irreversible operations (e.g. `file_delete`) requiring interactive human confirmation in the terminal.
       - `deny`: Fail-closed default. Any unlisted tool is blocked immediately.

2. **Two-Tool Meta Interface (`tool_describe` & `tool_run`)**
   - The LLM context is never bloated with dozens of static tool schemas.
   - The LLM only receives a 1-line tool index and two meta-tools:
     - `tool_describe(tool_name)`: Fetches parameter schemas on-demand.
     - `tool_run(tool_name, arguments)`: Validates signatures and executes with scope checks.
   - Scales to 100+ tools with zero prompt overhead.

3. **Maximum KV Prefix Caching & Live Context Tracking**
   - Deterministic, frozen system prompt and schema ordering for optimal KV cache hits.
   - Append-only conversation history; past turns are never mutated in-place.
   - Terminal UI displays real-time prompt tokens, cache hits, and percentage of the model's context window limit (e.g. `8,709 prompt (0.8% of 1M)`).
   - Context compaction merges past transcripts into a single `<summary>` block in `messages[0]`, keeping active context lean while preserving history.

4. **Fail-Closed & Atomic**
   - File edits and writes are atomic (`os.replace` via temporary files) with stale-mtime checks.
   - Path traversal and unauthorized directory access are blocked before execution.

---

## Quickstart

### 1. Installation

Requires Linux and Python >= 3.10:

```bash
git clone https://github.com/your-username/bloom.git
cd bloom

# Create virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Both `bloom` and `neuralcode` are registered CLI commands.

### 2. Configuration

Create a `.env` file in the project root:

```bash
# OpenAI-compatible API key (e.g. DeepRelay, OpenAI, OpenRouter)
DEEPRELAY_API_KEY="your-api-key"
OPENAI_API_KEY="your-api-key"
```

Configure models in `bloom/models.json` if using custom endpoints or local models (e.g. Ollama, vLLM).

### 3. Running an Automation Scope

Run an agent by name (searches `$BLOOM_ROOT/agents/<name>.json`, defaulting to `./agents/<name>.json` in a local checkout):
```bash
bloom --agent assistant
```

Run directly from an arbitrary manifest file path:
```bash
bloom --agent ./path/to/custom_agent.json
```

Resume the previous session for an agent:
```bash
bloom --agent assistant --resume
```

### 4. Running the Headless API Server (Web Dashboard / CI / Scripts)

Launch Bloom as a headless API server for web dashboards, automation scripts, and background CI jobs:
```bash
bloom --serve --port 4096
```

Runs with zero third-party framework dependencies (pure Python `asyncio`), REST management endpoints, and live Server-Sent Events (SSE) streaming.

> [!NOTE]
> In `--serve` daemon mode, `--agent` strictly resolves named agent manifests from `BLOOM_AGENTS_DIR` (`resolve_safe`) to eliminate path traversal and arbitrary file execution risks over the network. Full file paths are reserved for local CLI usage.

#### Security & Authentication
- **Authentication**: Set `BLOOM_API_TOKEN="your-secret-token"` in your environment or `.env`. When configured, all requests require `Authorization: Bearer <token>` or `?token=<token>`. Requests without a valid token receive `401 Unauthorized`.
- **CORS**: Configure `BLOOM_CORS_ORIGIN` (defaults to `*`).
- **Network Exposure Warning**: Binding to `--host 0.0.0.0` exposes agent tool execution over the network. Always configure `BLOOM_API_TOKEN` and restrict `BLOOM_CORS_ORIGIN` if binding to public or shared network interfaces.

---

## Defining a New Automation Scope

To create a new automation role, drop a JSON manifest into `$BLOOM_ROOT/agents/<name>.json` (or any directory pointed to by `$BLOOM_AGENTS`):

```json
{
  "model": "deeprelay-flash",
  "system_prompt": "Runs test suites and reads logs with zero write access to source code.",
  "read_paths": [".", "tests", "/var/log"],
  "write_paths": [],
  "skill_paths": ["skills/testing"],
  "permissions": {
    "allow": [
      "filesystem.file_read",
      "filesystem.file_list",
      "filesystem.file_grep",
      "filesystem.file_glob",
      "skills.skill_list",
      "skills.skill_load"
    ],
    "ask": [
      "filesystem.file_delete"
    ]
  }
}
```

Run it immediately:
```bash
bloom --agent tester
```

---

## Configuration & Environment Variables

- `BLOOM_ROOT`: Root project directory for pluggable discovery (`agents/`, `mcp/`, `skills/`). When running from a source checkout, defaults to the repository root. When using an installed package (`pip install bloom`), set `BLOOM_ROOT` to your project workspace directory containing `agents/`, `mcp/`, and `skills/`.
- `BLOOM_AGENTS_DIR` / `BLOOM_AGENTS`: Directory containing agent manifests (defaults to `$BLOOM_ROOT/agents`).
- `BLOOM_SERVERS_FILE`: Path to custom MCP servers configuration (defaults to `bloom/servers.json`; relative server paths inside resolve against `$BLOOM_ROOT`).
- `BLOOM_SKILLS_FILE`: Path to custom skills configuration (defaults to `bloom/skills.json`; relative skill roots inside resolve against `$BLOOM_ROOT`).
- `BLOOM_HOME`: Base directory for sessions and prompt history (defaults to `$BLOOM_ROOT`).
- `BLOOM_MAX_TOOL_ROUNDS`: Maximum autonomous tool execution rounds per turn (default: `50`).
- **Working Directory**: File operations (`file_read`, `file_write`, `file_grep`, `file_glob`) resolve relative to the current working directory (`CWD`), allowing agents to operate directly on the target project where `bloom` is executed.

---

## Built-In Capabilities

- **`filesystem`**:
  - `file_read`: Line-numbered, paginated reads with max line limits.
  - `file_edit` & `file_multi_edit`: Atomic snippet replacement with diff verification.
  - `file_write`: Atomic file creation (overwrite protection).
  - `file_replace_lines`: Range-based line editing and insertion.
  - `file_grep`: Regex search across files with line numbers.
  - `file_glob`: Pattern match file discovery (newest first).
  - `file_list`: Directory listing with folder indicators.
  - `file_delete`: File removal (typically mapped to `ask` tier).
- **`skills`**:
  - `skill_list`: Discover registered workflow instruction documents.
  - `skill_load`: Inject procedural workflow instructions (`SKILL.md`) into context.
- **MCP Servers & Skills (Two-Gate Registration)**:
  - Gate 1 (Harness Level): Configure local and external servers explicitly in `bloom/servers.json` and authorized skill directories in `bloom/skills.json`. If unlisted, 0 servers are loaded.
  - Gate 2 (Agent Scope Level): The active agent manifest explicitly permits which tools and skills the agent can call. Any unlisted server, tool, or skill requested by an agent manifest emits a warning and is denied.

---

## Slash Commands

- `/help`: Show all available slash commands and descriptions.
- `/rewind`: Rewind the conversation to an earlier prompt without breaking tool call pairs.
- `/compact`: Manually trigger context compaction into a handoff summary.
- `/sessions`: Switch between past saved session logs.

---

## API Reference (Headless Mode)

All endpoints return JSON. Prompt turns stream live events via Server-Sent Events (`text/event-stream`).

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/info` | Server status, active pinned agent, active model, agent list, model list |
| `GET` | `/api/agents` | All agent definitions (manifest, permissions, boundaries) |
| `GET` | `/api/agents/{name}` | Specific agent manifest details |
| `GET` | `/api/models` | Models catalog from `models.json` (api keys redacted) |
| `POST` | `/api/models/switch` | Switch active model `{"model": "..."}` |
| `GET` | `/api/tools` | List authorized tools and permission tiers for pinned agent |
| `GET` | `/api/tools/{name}/describe` | Inspect tool parameter schema |
| `GET` | `/api/skills` | List available procedural workflow skills for pinned agent |
| `GET` | `/api/skills/{name}` | Load skill markdown content |
| `GET` | `/api/sessions` | List saved chat sessions for pinned agent |
| `POST` | `/api/session` | Create new session |
| `GET` | `/api/session/{id}` | Full session message history |
| `DELETE` | `/api/session/{id}` | Delete a session |
| `POST` | `/api/session/{id}/compact` | Manually trigger context compaction |
| `GET` | `/api/session/{id}/rewind-candidates` | Get safe rewind turn indices and previews |
| `POST` | `/api/session/{id}/rewind` | Rewind session to target turn `{"targetIndex": ...}` |
| `POST` | `/api/session/{id}/abort` | Abort a running turn |
| `GET` | `/api/permissions` | View pending approval requests |
| `POST` | `/api/session/{id}/approval` | Resolve human approval `{"requestId": "...", "decision": "approved"\|"rejected"}` |
| `POST` | `/api/session/{id}/prompt` | **Stream a prompt turn via SSE** `{"text": "..."}` (Returns `409 Conflict` if busy) |

### SSE Event Contract (`/api/session/{id}/prompt`)

```jsonc
// Stream start
{"type": "start", "sessionId": "20261124-...", "agent": "assistant"}

// Model thinking / generation start
{"type": "thinking"}

// Streaming response text delta
{"type": "delta", "field": "text", "text": "..."}

// Tool execution event (emitted when a tool executes, pauses for permission, or completes)
{"type": "tool", "callId": "call_1", "tool": "filesystem.file_read", "name": "filesystem.file_read", "input": {...}, "output": "...", "state": "completed"}

// Human approval required (ask tier) - turn pauses until client POSTs /api/session/{id}/approval
{"type": "permission", "requestId": "req_abc123", "sessionId": "...", "tool": "filesystem.file_delete", "arguments": {...}, "prompt": "..."}

// Real-time usage and context limit tracking
{"type": "usage", "promptTokens": 8709, "cachedTokens": 8448, "completionTokens": 185, "contextLimit": 1048576, "contextPercentage": "0.8%"}

// Context compacted
{"type": "compacted", "before": 10, "after": 2}

// Turn completed (reason: "stop" | "max_rounds" | "aborted" | "error")
{"type": "done", "reason": "stop"}
```
