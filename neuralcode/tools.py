"""Builtin Minimal Meta-Tools: tool_describe and tool_run.

All external capabilities are routed through MCP servers.
Only tool_describe and tool_run are ever registered with the LLM.
Tool permissions are strictly default-deny based on the active agent manifest.
"""

import json
from typing import Optional, Set, Tuple, List

from . import history
from .mcp_client import mcp_client

ALLOW_TOOLS: Set[str] = set()
ASK_TOOLS: Set[str] = set()


def permission_lists(cfg: dict) -> Tuple[List[str], List[str]]:
    """Extract and validate (allow, ask) tool lists from manifest."""
    has_perms = "permissions" in cfg
    has_tools = "tools" in cfg

    if has_perms and has_tools:
        raise ValueError("Manifest cannot declare both 'permissions' and 'tools'. Use 'permissions'.")

    if has_perms:
        perms = cfg["permissions"]
        if not isinstance(perms, dict):
            raise ValueError("'permissions' in manifest must be a dictionary with 'allow' and/or 'ask' lists.")
        for key in ("allow", "ask"):
            val = perms.get(key, [])
            if not isinstance(val, list) or not all(isinstance(t, str) for t in val):
                raise ValueError(f"'permissions.{key}' must be a list of tool-name strings.")
        allow = list(dict.fromkeys(t.strip() for t in perms.get("allow", [])))
        ask = list(dict.fromkeys(t.strip() for t in perms.get("ask", [])))
    else:
        raw = cfg.get("tools", [])
        if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
            raise ValueError("'tools' in manifest must be a list of tool-name strings.")
        allow = list(dict.fromkeys(t.strip() for t in raw))
        ask = []

    overlap = set(allow) & set(ask)
    if overlap:
        raise ValueError(f"Tools cannot be declared in both 'allow' and 'ask' tiers: {overlap}")

    return allow, ask


def check_permission(tool_name: str) -> str:
    """Return permission tier ('allow', 'ask', or 'deny') for tool."""
    clean = tool_name.strip()
    if clean in ALLOW_TOOLS:
        return "allow"
    if clean in ASK_TOOLS:
        return "ask"
    return "deny"


def setup(agent_cfg: Optional[dict] = None) -> None:
    """Configure and validate allowed and ask tools from agent manifest."""
    global ALLOW_TOOLS, ASK_TOOLS
    from .mcp_client import LOCAL_MODULES, mcp_client

    if not agent_cfg:
        ALLOW_TOOLS = set()
        ASK_TOOLS = set()
        return

    cfg = agent_cfg
    allow_list, ask_list = permission_lists(cfg)

    all_declared = allow_list + ask_list
    for t in all_declared:
        if "." not in t:
            raise ValueError(f"Tool '{t}' in agent manifest must be in 'server.tool' format.")
        server, tool_name = t.split(".", 1)
        if server in LOCAL_MODULES:
            mod = LOCAL_MODULES[server]
            tools_map = getattr(mod, "TOOLS", {})
            if tool_name not in tools_map:
                raise ValueError(
                    f"Tool '{t}' declared in manifest is not a registered tool on server '{server}'. "
                    f"Available: {list(tools_map.keys())}"
                )
        elif server not in mcp_client.external_servers:
            raise ValueError(f"Unknown server '{server}' for tool '{t}' in agent manifest.")

    ALLOW_TOOLS = set(allow_list)
    ASK_TOOLS = set(ask_list)


async def tool_describe(tool_name: str) -> str:
    """Inspect schema, parameters, and documentation for an available tool."""
    clean = tool_name.strip()
    perm = check_permission(clean)
    if perm == "deny":
        return json.dumps({
            "error": f"Tool '{tool_name}' is not permitted for this agent. Default deny."
        })

    schema = await mcp_client.get_tool_schema(clean)
    if perm == "ask" and isinstance(schema, dict) and "description" in schema:
        schema["description"] += " [Note: Requires human confirmation before execution]"
    return json.dumps(schema, indent=2)


async def tool_run(tool_name: str, arguments: Optional[dict] = None) -> str:
    """Execute a permitted tool with input arguments, prompting if in 'ask' tier."""
    clean = tool_name.strip()
    perm = check_permission(clean)
    if perm == "deny":
        return json.dumps({
            "error": f"Tool '{tool_name}' is not permitted for this agent. Default deny."
        })

    args = arguments or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}

    if perm == "ask":
        from .ui import ui
        raw_args = json.dumps(args, indent=2) if args else "(no arguments)"
        formatted_args = raw_args[:1000] + "\n… (arguments truncated)" if len(raw_args) > 1000 else raw_args
        if not ui.approve(f"Tool '{clean}' requires confirmation. Arguments:\n{formatted_args}"):
            return json.dumps({
                "cancelled": f"Execution of tool '{clean}' was cancelled by user. Do not retry without user direction."
            })

    result = await mcp_client.call_tool(clean, args)
    if isinstance(result, (dict, list)):
        return json.dumps(result, indent=2)
    return str(result)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "tool_describe",
            "description": "Inspect schema, parameters, and documentation for an available tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Qualified tool name in 'server.tool' format (e.g. 'filesystem.file_read').",
                    }
                },
                "required": ["tool_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_run",
            "description": "Execute an allowed tool with input arguments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Qualified tool name in 'server.tool' format (e.g. 'filesystem.file_read').",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Dictionary of arguments to pass to the tool.",
                    },
                },
                "required": ["tool_name"],
            },
        },
    },
]

TOOLS = {
    "tool_describe": tool_describe,
    "tool_run": tool_run,
}


async def execute(tool_call):
    """Run a tool call safely without crashing the session."""
    name = tool_call.function.name
    raw = getattr(tool_call.function, "arguments", None) or "{}"
    if isinstance(raw, dict):
        args = raw
    else:
        try:
            args = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as broken:
            return {}, f"Error: arguments were not valid JSON ({broken})."

    if name not in TOOLS:
        return args, f"Error: no tool named '{name}'. Available: {', '.join(TOOLS)}."

    try:
        raw_result = await TOOLS[name](**args)
        return args, history.cap(str(raw_result))
    except TypeError as mismatch:
        return args, f"Error: wrong arguments for {name} ({mismatch})."
    except KeyError as missing:
        return args, f"Error: {name} needs an argument you did not send: {missing}."
    except Exception as failure:
        return args, f"Error: {name} failed - {type(failure).__name__}: {failure}"
