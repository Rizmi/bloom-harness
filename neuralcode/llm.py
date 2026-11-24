"""LLM calling layer tied to the active Agent manifest and MCP tools."""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from .manifest import load_manifest
from .mcp_client import mcp_client
from .tools import TOOL_SCHEMAS

PKG_DIR = Path(__file__).resolve().parent
MODELS_PATH = PKG_DIR / "models.json"

AGENT: Dict[str, Any] = {}
SPEC: Dict[str, Any] = {}
API_KEY: str = ""
BASE_URL: str = ""
MODEL: str = ""
CONTEXT_WINDOW: int = 64000
SYSTEM_PROMPT: str = ""

_client = None


def _resolve_api_key(raw_key: str, model_name: str) -> str:
    pattern = re.compile(r"\$\{([^}]+)\}")
    for match in pattern.finditer(raw_key):
        env_var = match.group(1)
        val = os.environ.get(env_var, "").strip()
        if not val:
            raise RuntimeError(
                f"Missing required environment variable '{env_var}' for model '{model_name}' in models.json.\n"
                f"Please add it to .env or run: export {env_var}=<your-api-key>"
            )
        raw_key = raw_key.replace(match.group(0), val)
    return raw_key


def setup(agent_cfg: Optional[Dict[str, Any]] = None):
    """Initialize LLM settings and system prompt from agent manifest."""
    global AGENT, SPEC, API_KEY, BASE_URL, MODEL, CONTEXT_WINDOW, SYSTEM_PROMPT, _client

    AGENT = agent_cfg or load_manifest("coder")

    with open(MODELS_PATH, encoding="utf-8") as f:
        data = json.load(f)
        models = data.get("models", data)

    model_key = AGENT.get("model", "deepseek-v4-flash")
    if model_key not in models:
        raise KeyError(f"Model '{model_key}' declared in agent manifest not found in models.json.")

    SPEC = models[model_key]
    API_KEY = _resolve_api_key(SPEC.get("api_key", ""), model_key)
    BASE_URL = SPEC["base_url"]
    MODEL = SPEC["model"]
    CONTEXT_WINDOW = SPEC.get("context_limit", 64000)
    _client = None

    # Build static system prompt with allowed MCP tool summaries and boundaries
    base_prompt = AGENT.get("system_prompt", "You are a helpful AI assistant.")
    allowed_tools = AGENT.get("tools", [])
    read_paths = AGENT.get("read_paths", ["."])
    write_paths = AGENT.get("write_paths", ["."])
    skill_paths = (
        AGENT.get("skill_paths")
        or AGENT.get("skills_paths")
        or ([AGENT["skills_path"]] if "skills_path" in AGENT else [])
    )

    tool_summaries = []
    for t_name in allowed_tools:
        summary = mcp_client.get_tool_summary(t_name)
        tool_summaries.append(f"- `{t_name}`: {summary}")

    prompt_parts = [base_prompt]
    if tool_summaries:
        prompt_parts.append(
            "\n\n## Available Tools\n"
            + "\n".join(tool_summaries)
            + "\n\nTo use tools:\n"
            "- Call `tool_describe(tool_name)` to inspect parameters and documentation.\n"
            "- Call `tool_run(tool_name, arguments)` to execute."
        )

    paths_desc = [
        f"- Read: {read_paths}",
        f"- Write: {write_paths}",
    ]
    if skill_paths:
        paths_desc.append(f"- Skills: {skill_paths}")

    prompt_parts.append(
        f"\n\n## Authorized Workspace Paths\n" + "\n".join(paths_desc)
    )

    SYSTEM_PROMPT = "".join(prompt_parts).strip()


# Default setup on import using coder.json (with silent fallback if env keys unset until runtime)
try:
    setup()
except RuntimeError:
    pass


def get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY or "none")
    return _client


async def call_llm(messages, tools=None):
    client = get_client()
    kwargs = {
        "model": MODEL,
        "messages": messages,
        "timeout": 120.0,
    }
    if "temperature" in SPEC:
        kwargs["temperature"] = SPEC["temperature"]

    active_tools = tools if tools is not None else TOOL_SCHEMAS
    if active_tools:
        kwargs["tools"] = active_tools

    response = await client.chat.completions.create(**kwargs)
    message = response.choices[0].message

    resp_usage = getattr(response, "usage", None)
    completion_details = getattr(resp_usage, "completion_tokens_details", None)
    prompt_details = getattr(resp_usage, "prompt_tokens_details", None)

    usage = {
        "prompt_tokens": getattr(resp_usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(resp_usage, "completion_tokens", 0) or 0,
        "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None),
        "cached_tokens": getattr(prompt_details, "cached_tokens", None),
    }

    return message, usage
