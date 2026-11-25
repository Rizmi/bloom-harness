"""LLM calling layer tied to the active Agent manifest and MCP tools."""

import inspect
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

from .mcp_client import mcp_client
from .tools import TOOL_SCHEMAS

PKG_DIR = Path(__file__).resolve().parent

def get_models_path() -> Path:
    val = os.environ.get("BLOOM_MODELS_FILE")
    if val:
        p = Path(val).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Configured BLOOM_MODELS_FILE does not exist: {p}")
        return p
    return PKG_DIR / "models.json"

AGENT: Dict[str, Any] = {}
SPEC: Dict[str, Any] = {}
API_KEY: str = ""
BASE_URL: str = ""
MODEL: str = ""
MODEL_KEY: str = ""
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


def setup(
    agent_cfg: Optional[Dict[str, Any]] = None,
    skill_paths: Optional[list] = None,
    allowed_skills: Optional[list] = None,
):
    """Initialize LLM settings and system prompt from agent manifest."""
    global MODEL_KEY, AGENT, SPEC, API_KEY, BASE_URL, MODEL, CONTEXT_WINDOW, SYSTEM_PROMPT, _client

    if not agent_cfg:
        AGENT = {}
        SYSTEM_PROMPT = ""
        return

    AGENT = agent_cfg

    with open(get_models_path(), encoding="utf-8") as f:
        data = json.load(f)
        models = data.get("models", data)

    model_key = AGENT.get("model")
    if not model_key or not str(model_key).strip():
        raise ValueError("Missing required 'model' field in agent manifest.")
    if model_key not in models:
        raise KeyError(
            f"Model '{model_key}' declared in agent manifest not found in models.json. Available: {list(models.keys())}"
        )

    SPEC = models[model_key]
    MODEL_KEY = model_key
    API_KEY = _resolve_api_key(SPEC.get("api_key", ""), model_key)
    BASE_URL = SPEC["base_url"]
    MODEL = SPEC["model"]
    CONTEXT_WINDOW = SPEC.get("context_limit", 64000)
    _client = None

    # Build static system prompt with allowed MCP tool summaries and boundaries
    base_prompt = AGENT.get("system_prompt", "You are a helpful AI assistant.")
    from . import tools

    allowed_tools = sorted(tools.ALLOW_TOOLS | tools.ASK_TOOLS)

    read_paths = AGENT.get("read_paths", [])
    write_paths = AGENT.get("write_paths", [])
    if skill_paths is None:
        from .skills import validate_agent_skills
        skill_paths, allowed_skills = validate_agent_skills(AGENT, AGENT.get("name", "agent"))

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

    prompt_parts.append(
        f"\n\n## Authorized Workspace Paths\n" + "\n".join(paths_desc)
    )

    has_skills_tool = any(t.startswith("skills.") for t in allowed_tools)
    has_skills_config = bool(AGENT.get("skill_paths") or AGENT.get("skills"))
    if has_skills_tool or has_skills_config:
        from .skills import get_available_skills
        available_skills = get_available_skills(
            AGENT,
            AGENT.get("name", "agent"),
            auth_roots=skill_paths,
            auth_skills=allowed_skills,
        )
        prompt_parts.append(
            "\n\n## Available Skills\n"
            f"- Available: {available_skills}\n"
            '- Load a skill: `tool_run("skills.skill_load", {"skill_name": "<name>"})`'
        )

    SYSTEM_PROMPT = "".join(prompt_parts).strip()




def get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY or "none")
    return _client


def _prepare_messages(messages: list) -> list:
    """Sanitize message fields for provider compatibility."""
    strip_reasoning = SPEC.get("strip_reasoning")
    if strip_reasoning is None:
        provider = (SPEC.get("provider") or "").lower()
        base = (BASE_URL or "").lower()
        # Keep reasoning only for deepseek or deeprelay endpoints
        strip_reasoning = (
            provider not in ("deepseek", "deeprelay")
            and "deepseek" not in base
            and "deeprelay" not in base
        )
    if strip_reasoning:
        cleaned = []
        for m in messages:
            if isinstance(m, dict) and "reasoning_content" in m:
                copy_m = dict(m)
                copy_m.pop("reasoning_content", None)
                cleaned.append(copy_m)
            else:
                cleaned.append(m)
        return cleaned
    return messages


def _extract_usage(resp_usage: Any) -> Dict[str, Any]:
    completion_details = getattr(resp_usage, "completion_tokens_details", None)
    prompt_details = getattr(resp_usage, "prompt_tokens_details", None)
    return {
        "prompt_tokens": getattr(resp_usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(resp_usage, "completion_tokens", 0) or 0,
        "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None),
        "cached_tokens": (
            getattr(prompt_details, "cached_tokens", None)
            or getattr(resp_usage, "prompt_cache_hit_tokens", None)
        ),
    }


async def _emit_delta(on_delta: Optional[Callable[[str, str], Any]], field: str, text: str):
    if on_delta is None or not text:
        return
    res = on_delta(field, text)
    if inspect.isawaitable(res):
        await res


def _stream_options() -> Optional[Dict[str, Any]]:
    """Return stream_options only when explicitly configured or known supported."""
    opt = SPEC.get("stream_options")
    if opt is False:
        return None
    if isinstance(opt, dict):
        return opt
    provider = (SPEC.get("provider") or "").lower()
    base = (BASE_URL or "").lower()
    if opt is True or provider in ("openai", "deepseek", "deeprelay") or any(
        h in base for h in ("openai.com", "deepseek", "deeprelay")
    ):
        return {"include_usage": True}
    return None


async def call_llm(messages, tools=None, on_delta: Optional[Callable[[str, str], Any]] = None):
    client = get_client()
    timeout_val = float(SPEC.get("timeout", os.environ.get("BLOOM_LLM_TIMEOUT", 300.0)))
    kwargs = {
        "model": MODEL,
        "messages": _prepare_messages(messages),
        "timeout": timeout_val,
        "stream": True,
    }
    stream_opts = _stream_options()
    if stream_opts is not None:
        kwargs["stream_options"] = stream_opts
    if "temperature" in SPEC:
        kwargs["temperature"] = SPEC["temperature"]

    active_tools = tools if tools is not None else TOOL_SCHEMAS
    if active_tools:
        kwargs["tools"] = active_tools

    response = await client.chat.completions.create(**kwargs)

    # Support non-streaming mock objects in unit tests cleanly
    if not hasattr(response, "__aiter__"):
        message = response.choices[0].message
        return message, _extract_usage(getattr(response, "usage", None))

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_acc: Dict[int, Dict[str, Any]] = {}
    resp_usage = None

    async for chunk in response:
        cu = getattr(chunk, "usage", None)
        if cu is not None:
            resp_usage = cu
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue

        rc = getattr(delta, "reasoning_content", None)
        if isinstance(rc, str) and rc:
            reasoning_parts.append(rc)
            await _emit_delta(on_delta, "reasoning", rc)

        txt = getattr(delta, "content", None)
        if isinstance(txt, str) and txt:
            content_parts.append(txt)
            await _emit_delta(on_delta, "text", txt)

        for tc in getattr(delta, "tool_calls", None) or []:
            idx = getattr(tc, "index", 0) or 0
            slot = tool_acc.setdefault(idx, {"id": "", "type": "function", "name": "", "arguments": ""})
            tc_id = getattr(tc, "id", None)
            if tc_id:
                slot["id"] = tc_id
            tc_type = getattr(tc, "type", None)
            if tc_type:
                slot["type"] = tc_type
            fn = getattr(tc, "function", None)
            if fn is not None:
                fn_name = getattr(fn, "name", None)
                if isinstance(fn_name, str) and fn_name:
                    slot["name"] += fn_name
                fn_args = getattr(fn, "arguments", None)
                if isinstance(fn_args, str) and fn_args:
                    slot["arguments"] += fn_args

    built_tools = [
        SimpleNamespace(
            id=slot["id"],
            type=slot["type"],
            function=SimpleNamespace(name=slot["name"], arguments=slot["arguments"]),
        )
        for _, slot in sorted(tool_acc.items())
    ]
    message = SimpleNamespace(
        content="".join(content_parts),
        reasoning_content="".join(reasoning_parts) or None,
        tool_calls=built_tools or None,
    )
    return message, _extract_usage(resp_usage)


def list_models() -> Dict[str, Any]:
    """Return all configured models from models.json."""
    with open(get_models_path(), encoding="utf-8") as f:
        data = json.load(f)
    return data.get("models", data)


def switch_model(model_key: str) -> str:
    """Switch active model at runtime."""
    global MODEL_KEY, SPEC, API_KEY, BASE_URL, MODEL, CONTEXT_WINDOW, _client
    models = list_models()
    if model_key not in models:
        raise KeyError(f"Model '{model_key}' not found in models.json. Available: {list(models.keys())}")
    SPEC = models[model_key]
    MODEL_KEY = model_key
    API_KEY = _resolve_api_key(SPEC.get("api_key", ""), model_key)
    BASE_URL = SPEC["base_url"]
    MODEL = SPEC["model"]
    CONTEXT_WINDOW = SPEC.get("context_limit", 64000)
    _client = None
    if AGENT is not None:
        AGENT["model"] = model_key
    return model_key
