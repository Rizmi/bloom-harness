# NeuralCode

A lightweight, Linux-native automation base harness for running LLM-driven automation tools under strict, per-agent scopes.

NeuralCode is built around a single principle: **one minimal harness can drive many different automation roles** (coding, reviewing, testing, indexing, deployment) by enforcing strict path permissions, tool access, and procedural skills through decoupled JSON configurations.

---

## Core Pillars

1. **One Harness, Multiple Scopes**
   - Run any automation task through the same engine: `neuralcode --agent <role>`.
   - Each role is defined in `agents/<role>.json` with explicit boundaries:
     - `read_paths`: Directories the agent can read.
     - `write_paths`: Directories the agent can modify (empty `[]` enforces read-only mode).
     - `skill_paths`: Folders containing authorized workflow instructions (`SKILL.md`).
     - `permissions`: Three-tier gate (`allow`, `ask`, `deny`):
       - `allow`: Safe operations executed autonomously without stopping.
       - `ask`: Destructive or irreversible operations (e.g. `file_delete`) requiring interactive human confirmation.
       - `deny`: Default-deny. Any unlisted tool is blocked immediately.

2. **Two-Tool Meta Interface (`tool_describe` & `tool_run`)**
   - The LLM context is never bloated with dozens of tool schemas.
   - The LLM only receives a 1-line tool index and two meta-tools:
     - `tool_describe(tool_name)`: Fetches parameter schemas on-demand.
     - `tool_run(tool_name, arguments)`: Executes the tool with permission checks.
   - Scales to 100+ tools with zero prompt overhead.

3. **Maximum KV Prefix Caching**
   - Frozen system prompt and frozen schema order.
   - Conversation history is strictly append-only; past turns are never mutated in-place.
   - Context compaction merges past transcripts into a single `<summary>` block in `messages[0]`, preventing token leaks and maintaining cache hits.

4. **Fail-Closed & Atomic**
   - File edits and writes are atomic (`os.replace` via temporary files) with stale-mtime checks.
   - Path traversal and unauthorized directory access are blocked before execution.

---

## Architecture

```text
               ┌────────────────────────────────┐
               │              LLM               │
               └───────────────▲────────────────┘
                               │ (tool_describe, tool_run)
               ┌───────────────▼────────────────┐
               │    NeuralCode Base Harness     │
               │         (agent.py)             │
               └───────────────┬────────────────┘
                               │ Enforces Scope (agents/<name>.json)
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│    Filesystem    │  │  Skills Engine   │  │   External MCP   │
│ read/write_paths │  │   skill_paths    │  │   servers.json   │
└──────────────────┘  └──────────────────┘  └──────────────────┘
```

---

## Quickstart

### 1. Installation

Requires Linux and Python >= 3.10:

```bash
git clone https://github.com/your-username/neural-code.git
cd neural-code

# Create virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Configuration

Create a `.env` file in the project root:

```bash
# OpenAI-compatible API key
DEEPRELAY_API_KEY="your-api-key"
# Or standard OpenAI key
OPENAI_API_KEY="your-api-key"
```

Configure models in `neuralcode/models.json` if using custom endpoints or models.

### 3. Running an Automation Scope

Run the default coding agent:
```bash
neuralcode --agent coder
```

Run a strictly read-only reviewer agent:
```bash
neuralcode --agent reviewer
```

Resume the previous session:
```bash
neuralcode --resume
```

---

## Defining a New Automation Scope

To create a new automation tool or role, add a JSON configuration under `neuralcode/agents/<name>.json`:

```json
{
  "name": "tester",
  "description": "Runs test suites and reads logs with zero write access to source code",
  "model": "deepseek-coder",
  "read_paths": [".", "tests", "/var/log"],
  "write_paths": [],
  "skill_paths": ["skills/testing"],
  "permissions": {
    "allow": [
      "filesystem.file_read",
      "filesystem.file_find",
      "filesystem.file_grep",
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
neuralcode --agent tester
```

---

## Built-In Capabilities

- **`filesystem`**:
  - `file_read`: Line-numbered, paginated reads.
  - `file_edit` & `file_multi_edit`: Atomic snippet replacement with diff verification.
  - `file_write`: Atomic file creation.
  - `file_replace_lines`: Range-based line editing.
  - `file_grep` & `file_glob` & `file_find`: Pattern and directory searching.
  - `file_list` & `file_delete`: Directory listing and cleanup.
- **`skills`**:
  - `skill_list`: Discover registered workflow instruction documents.
  - `skill_load`: Inject procedural workflow instructions (`SKILL.md`) into context.
- **External MCP Integration**:
  - Add standard Streamable HTTP or stdio servers in `neuralcode/mcp/servers.json`.

---

## Slash Commands

- `/rewind`: Rewind the conversation to an earlier prompt without breaking tool call pairs.
- `/compact`: Manually trigger context compaction into a handoff summary.
- `/sessions`: Switch between past saved session logs.
