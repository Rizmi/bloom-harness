"""Builtin Minimal Meta-Tools: tool_describe and tool_run.

All external capabilities are routed through MCP servers.
Only tool_describe and tool_run are ever registered with the LLM.
Tool permissions are strictly default-deny based on coder.json.
"""

import json
from pathlib import Path
from typing import Optional, Set

from . import history
from .manifest import load_manifest
from .mcp_client import mcp_client

ALLOWED_TOOLS: Set[str] = set()


def setup(agent_cfg: Optional[dict] = None) -> None:
    """Configure and validate allowed tools from agent manifest."""
    global ALLOWED_TOOLS
    from .mcp_client import LOCAL_MODULES, mcp_client

    cfg = agent_cfg or load_manifest("coder")
    raw_tools = cfg.get("tools", [])

    for t in raw_tools:
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
        elif server not in mcp_client._external_servers:
            raise ValueError(f"Unknown server '{server}' for tool '{t}' in agent manifest.")

    ALLOWED_TOOLS = set(raw_tools)


try:
    setup()
except Exception:
    pass


async def tool_describe(tool_name: str) -> str:
    """Inspect schema, parameters, and documentation for an available tool."""
    clean = tool_name.strip()
    if clean not in ALLOWED_TOOLS:
        return json.dumps({
            "error": f"Tool '{tool_name}' is not permitted for this agent. Default deny."
        })

    schema = await mcp_client.get_tool_schema(clean)
    return json.dumps(schema, indent=2)


async def tool_run(tool_name: str, arguments: Optional[dict] = None) -> str:
    """Execute an allowed tool with input arguments."""
    clean = tool_name.strip()
    if clean not in ALLOWED_TOOLS:
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
