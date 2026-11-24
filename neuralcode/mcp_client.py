"""MCP Client Manager for Local and External MCP Servers.

Provides tool schema discovery, summary extraction, and async tool execution.
"""

import asyncio
import inspect
import json
import os
import re
import subprocess
import typing
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .manifest import load_manifest
from .mcp.filesystem import server as fs_server
from .mcp.skills import server as sk_server

PKG_DIR = Path(__file__).resolve().parent
SERVERS_FILE = PKG_DIR / "mcp" / "servers.json"

LOCAL_MODULES: Dict[str, Any] = {
    "filesystem": fs_server,
    "skills": sk_server,
}

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SAFE_ENV_VARS = {
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "SHELL", "TMPDIR", "TEMP", "TMP",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
}


def _expand_env(value: str) -> str:
    if not isinstance(value, str):
        return value

    def _replace(m):
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            raise ValueError(f"Unresolved environment variable '${{{var}}}' in server configuration.")
        return val

    return _ENV_REF.sub(_replace, value)


def _get_subprocess_env(srv: dict) -> dict:
    base_env = {k: v for k, v in os.environ.items() if k in SAFE_ENV_VARS}
    declared = {k: _expand_env(v) for k, v in (srv.get("env") or {}).items()}
    return {**base_env, **declared}


def _func_to_schema(func: Callable, summary: Optional[str] = None) -> dict:
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or summary or ""
    properties = {}
    required = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls") or param.kind in (
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            continue

        ann = param.annotation
        origin = typing.get_origin(ann)
        args = typing.get_args(ann)

        # Unwrap Union / Optional (both typing.Union and PEP 604 UnionType)
        if origin is Union or getattr(origin, "__name__", "") == "UnionType":
            non_none = [a for a in args if a is not type(None)]
            if non_none:
                ann = non_none[0]
                origin = typing.get_origin(ann)

        prop: Dict[str, Any] = {"type": "string"}
        if ann is int:
            prop["type"] = "integer"
        elif ann is bool:
            prop["type"] = "boolean"
        elif ann is float:
            prop["type"] = "number"
        elif ann in (list, List) or origin in (list, list):
            prop["type"] = "array"
            item_args = typing.get_args(ann)
            if item_args:
                item_t = item_args[0]
                item_orig = typing.get_origin(item_t) or item_t
                if item_orig in (dict, Dict):
                    prop["items"] = {"type": "object"}
                elif item_orig is str:
                    prop["items"] = {"type": "string"}
                elif item_orig is int:
                    prop["items"] = {"type": "integer"}
                elif item_orig is bool:
                    prop["items"] = {"type": "boolean"}
                else:
                    prop["items"] = {"type": "object"}
            else:
                prop["items"] = {"type": "object"}
        elif ann in (dict, Dict) or origin in (dict, dict):
            prop["type"] = "object"

        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            prop["default"] = param.default

        properties[name] = prop

    meta = getattr(func, "_mcp_meta", {}) or {}
    for p_name, p_override in meta.get("parameters", {}).items():
        if p_name in properties:
            properties[p_name].update(p_override)
        else:
            properties[p_name] = p_override

    if "Args:" in doc:
        args_block = doc.split("Args:", 1)[1].split("\n\n")[0]
        for line in args_block.splitlines():
            line = line.strip()
            if ":" in line and not line.startswith("-"):
                pname, pdesc = line.split(":", 1)
                pname = pname.strip().split()[0]
                if pname in properties and "description" not in properties[pname]:
                    properties[pname]["description"] = pdesc.strip()

    return {
        "description": doc,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def _parse_mcp_http_response(r) -> dict:
    raw = r.read().decode("utf-8")
    content_type = r.headers.get("Content-Type", "")
    if "text/event-stream" in content_type or "data:" in raw:
        last_msg = {}
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    msg = json.loads(line)
                    if "result" in msg or "error" in msg:
                        last_msg = msg
                except Exception:
                    continue
        if last_msg:
            return last_msg
    try:
        return json.loads(raw)
    except Exception:
        return {"error": f"Failed to parse response: {raw[:200]}"}


class MCPClientManager:
    def __init__(self, servers_file: Path = SERVERS_FILE):
        self.servers_file = servers_file
        self._external_servers: Dict[str, dict] = {}
        self._external_tools_cache: Dict[str, dict] = {}
        self._initialized_servers: set = set()
        self._server_sessions: Dict[str, str] = {}
        self.reload_servers()
        self.configure_local_servers()

    def configure_local_servers(self, agent_cfg: Optional[Dict[str, Any]] = None):
        cfg = agent_cfg
        if cfg is None:
            try:
                cfg = load_manifest("coder")
            except Exception:
                cfg = {}
        if not cfg:
            return

        if "filesystem" in LOCAL_MODULES and hasattr(LOCAL_MODULES["filesystem"], "configure"):
            LOCAL_MODULES["filesystem"].configure(
                read_paths=cfg.get("read_paths", []),
                write_paths=cfg.get("write_paths", []),
            )
        if "skills" in LOCAL_MODULES and hasattr(LOCAL_MODULES["skills"], "configure"):
            skill_paths = (
                cfg.get("skill_paths")
                or cfg.get("skills_paths")
                or ([cfg["skills_path"]] if "skills_path" in cfg else [])
            )
            LOCAL_MODULES["skills"].configure(skill_paths=skill_paths)

    def reload_servers(self):
        if not self.servers_file.is_file():
            return
        new_servers = {}
        try:
            data = json.loads(self.servers_file.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", data)
        except Exception as e:
            import sys
            print(f"Warning: Failed to parse MCP server config {self.servers_file}: {e}", file=sys.stderr)
            return

        for name, cfg in servers.items():
            if cfg.get("disabled", False):
                continue
            entry = dict(cfg)
            entry["name"] = name.lower()
            try:
                if "url" in entry:
                    entry["url"] = _expand_env(entry["url"])
                if "command" in entry:
                    entry["command"] = _expand_env(entry["command"])
                if "env" in entry and isinstance(entry["env"], dict):
                    entry["env"] = {k: _expand_env(v) for k, v in entry["env"].items()}
                new_servers[name.lower()] = entry
            except Exception as e:
                import sys
                print(f"Warning: Disabling server '{name}': {e}", file=sys.stderr)

        self._external_servers = new_servers

    def get_tool_summary(self, qualified_name: str) -> str:
        """Return a 1-line summary for prompt injection."""
        if "." not in qualified_name:
            return qualified_name

        server, tool = qualified_name.split(".", 1)
        server = server.lower()

        # 1. Local server
        if server in LOCAL_MODULES:
            mod = LOCAL_MODULES[server]
            func = getattr(mod, "TOOLS", {}).get(tool)
            if func:
                meta = getattr(func, "_mcp_meta", {}) or {}
                if "summary" in meta:
                    return meta["summary"]
                doc = inspect.getdoc(func) or ""
                return doc.splitlines()[0] if doc else f"Tool {qualified_name}"

        # 2. External server cache or registry summary
        cached = self._external_tools_cache.get(qualified_name)
        if cached:
            doc = cached.get("description", "")
            return doc.splitlines()[0] if doc else f"Tool {qualified_name}"

        srv = self._external_servers.get(server)
        if srv and "description" in srv:
            return srv["description"]

        return f"Tool {qualified_name}"

    async def get_tool_schema(self, qualified_name: str) -> dict:
        """Return parameters schema and documentation for tool_describe."""
        if "." not in qualified_name:
            return {"error": f"Tool '{qualified_name}' must be in 'server.tool' format."}

        server, tool = qualified_name.split(".", 1)
        server = server.lower()

        # 1. Local server
        if server in LOCAL_MODULES:
            mod = LOCAL_MODULES[server]
            func = getattr(mod, "TOOLS", {}).get(tool)
            if not func:
                return {"error": f"Tool '{tool}' not found on local server '{server}'."}
            meta = getattr(func, "_mcp_meta", {}) or {}
            schema_data = _func_to_schema(func, meta.get("summary"))
            return {
                "name": qualified_name,
                "description": schema_data["description"],
                "parameters": schema_data["inputSchema"],
            }

        # 2. External server
        srv = self._external_servers.get(server)
        if not srv:
            return {"error": f"Unknown MCP server '{server}' for tool '{qualified_name}'."}

        if srv.get("transport") == "sse":
            return {"error": f"Transport 'sse' is deprecated; use 'streamable_http' for server '{server}'."}

        # Query tools/list if not cached
        if qualified_name not in self._external_tools_cache:
            await self._fetch_external_tools(server)

        t = self._external_tools_cache.get(qualified_name)
        if not t:
            return {
                "name": qualified_name,
                "description": f"Tool on MCP server '{server}'",
                "parameters": {"type": "object", "properties": {}},
            }

        return {
            "name": qualified_name,
            "description": t.get("description", ""),
            "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
        }

    async def _fetch_external_tools(self, server_name: str) -> Dict[str, dict]:
        srv = self._external_servers.get(server_name.lower())
        if not srv:
            return {}

        if srv.get("transport") == "sse":
            import sys
            print(f"Warning: Server '{server_name}' uses deprecated 'sse' transport; please use 'streamable_http'.", file=sys.stderr)
            return {}

        tools_list = []
        payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

        # 1. HTTP / Streamable HTTP
        if srv.get("transport") in ("http", "streamable_http") and "url" in srv:
            url = srv["url"]

            def _fetch_http():
                if server_name not in self._initialized_servers:
                    try:
                        init_req = urllib.request.Request(
                            url,
                            data=json.dumps({
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "initialize",
                                "params": {
                                    "protocolVersion": "2024-11-05",
                                    "capabilities": {},
                                    "clientInfo": {"name": "neuralcode", "version": "1.0.0"},
                                },
                            }).encode("utf-8"),
                            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                            method="POST",
                        )
                        with urllib.request.urlopen(init_req, timeout=10) as r:
                            init_resp = _parse_mcp_http_response(r)
                            if "error" in init_resp:
                                return []
                            session_id = r.headers.get("Mcp-Session-Id")
                            if session_id:
                                self._server_sessions[server_name] = session_id

                        notif_headers = {"Content-Type": "application/json"}
                        if server_name in self._server_sessions:
                            notif_headers["Mcp-Session-Id"] = self._server_sessions[server_name]
                        notif_req = urllib.request.Request(
                            url,
                            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8"),
                            headers=notif_headers,
                            method="POST",
                        )
                        with urllib.request.urlopen(notif_req, timeout=10) as r:
                            r.read()
                        self._initialized_servers.add(server_name)
                    except Exception:
                        return []

                req_headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2024-11-05",
                }
                if server_name in self._server_sessions:
                    req_headers["Mcp-Session-Id"] = self._server_sessions[server_name]
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode("utf-8"), headers=req_headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    return _parse_mcp_http_response(r)

            try:
                resp = await asyncio.to_thread(_fetch_http)
                tools_list = resp.get("result", {}).get("tools", [])
            except Exception:
                pass

        # 2. Stdio
        elif srv.get("transport") == "stdio" and "command" in srv:
            def _fetch_stdio():
                cmd = [srv["command"]] + srv.get("args", [])
                clean_env = _get_subprocess_env(srv)
                try:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=srv.get("cwd"),
                        env=clean_env,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                except Exception:
                    return []
                lines = [
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "neuralcode", "version": "1.0.0"},
                        },
                    }),
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                    json.dumps(payload),
                ]
                try:
                    stdout, _ = proc.communicate(input="\n".join(lines) + "\n", timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    return []
                except Exception:
                    proc.kill()
                    return []
                if stdout:
                    for line in stdout.strip().splitlines():
                        try:
                            msg = json.loads(line)
                            if msg.get("id") == 2 and "result" in msg:
                                return msg["result"].get("tools", [])
                        except Exception:
                            continue
                return []

            try:
                tools_list = await asyncio.to_thread(_fetch_stdio)
            except Exception:
                pass

        for t in tools_list:
            self._external_tools_cache[f"{server_name}.{t['name']}"] = t
        return {t["name"]: t for t in tools_list}

    async def call_tool(self, qualified_name: str, arguments: Optional[dict] = None) -> Any:
        """Execute a tool call against local or external MCP server."""
        if "." not in qualified_name:
            return {"error": f"Tool '{qualified_name}' must be in 'server.tool' format."}

        server, tool = qualified_name.split(".", 1)
        server = server.lower()
        args = arguments or {}

        # 1. Local tool execution (in-process, fast)
        if server in LOCAL_MODULES:
            mod = LOCAL_MODULES[server]
            func = getattr(mod, "TOOLS", {}).get(tool)
            if not func:
                return {"error": f"Tool '{tool}' not found on local server '{server}'."}

            try:
                if inspect.iscoroutinefunction(func):
                    return await func(**args)
                return await asyncio.to_thread(func, **args)
            except Exception as e:
                return {"error": str(e)}

        # 2. External MCP server execution (over HTTP/SSE or stdio)
        srv = self._external_servers.get(server)
        if not srv:
            return {"error": f"Unknown MCP server '{server}' for tool '{qualified_name}'."}

        target_id = 2
        payload = {
            "jsonrpc": "2.0",
            "id": target_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }

        if srv.get("transport") == "sse":
            return {"error": f"Transport 'sse' is deprecated; use 'streamable_http' for server '{server}'."}

        # HTTP / Streamable HTTP
        if srv.get("transport") in ("http", "streamable_http") and "url" in srv:
            url = srv["url"]

            def _post():
                # Perform protocol initialization if not yet initialized
                if server not in self._initialized_servers:
                    try:
                        init_req = urllib.request.Request(
                            url,
                            data=json.dumps({
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "initialize",
                                "params": {
                                    "protocolVersion": "2024-11-05",
                                    "capabilities": {},
                                    "clientInfo": {"name": "neuralcode", "version": "1.0.0"},
                                },
                            }).encode("utf-8"),
                            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                            method="POST",
                        )
                        with urllib.request.urlopen(init_req, timeout=10) as r:
                            init_resp = _parse_mcp_http_response(r)
                            if "error" in init_resp:
                                return {"error": f"MCP initialization failed on '{server}': {init_resp['error']}"}
                            session_id = r.headers.get("Mcp-Session-Id")
                            if session_id:
                                self._server_sessions[server] = session_id

                        notif_headers = {"Content-Type": "application/json"}
                        if server in self._server_sessions:
                            notif_headers["Mcp-Session-Id"] = self._server_sessions[server]
                        notif_req = urllib.request.Request(
                            url,
                            data=json.dumps({
                                "jsonrpc": "2.0",
                                "method": "notifications/initialized",
                            }).encode("utf-8"),
                            headers=notif_headers,
                            method="POST",
                        )
                        with urllib.request.urlopen(notif_req, timeout=10) as r:
                            r.read()
                        self._initialized_servers.add(server)
                    except Exception as e:
                        return {"error": f"MCP initialization failed on '{server}': {e}"}

                req_headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2024-11-05",
                }
                if server in self._server_sessions:
                    req_headers["Mcp-Session-Id"] = self._server_sessions[server]

                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url, data=data, headers=req_headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    resp = _parse_mcp_http_response(r)
                    if isinstance(resp, dict):
                        if "error" in resp:
                            err_info = resp["error"]
                            return {"error": err_info.get("message", str(err_info))}
                        if "result" in resp:
                            content = resp["result"].get("content", [])
                            texts = [c["text"] for c in content if isinstance(c, dict) and "text" in c]
                            return "\n".join(texts) if texts else resp["result"]
                    return resp

            try:
                return await asyncio.to_thread(_post)
            except Exception as e:
                return {"error": f"External MCP call failed for '{qualified_name}': {e}"}

        # Stdio
        if srv.get("transport") == "stdio" and "command" in srv:
            def _stdio():
                cmd = [srv["command"]] + srv.get("args", [])
                clean_env = _get_subprocess_env(srv)
                try:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=srv.get("cwd"),
                        env=clean_env,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                except Exception as e:
                    return {"error": f"Failed to spawn MCP server '{server}' ({cmd[0]}): {e}"}

                input_lines = [
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "neuralcode", "version": "1.0.0"},
                        },
                    }),
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                    json.dumps(payload),
                ]
                try:
                    stdout, stderr = proc.communicate(input="\n".join(input_lines) + "\n", timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    return {"error": f"MCP server '{server}' timed out after 30 seconds."}
                except Exception as e:
                    proc.kill()
                    return {"error": f"Stdio execution failed for '{qualified_name}': {e}"}
                if stdout:
                    for line in stdout.strip().splitlines():
                        try:
                            resp = json.loads(line)
                            # Ensure we match the response for tools/call (id: target_id), not initialize (id: 1)
                            if resp.get("id") == target_id:
                                if "error" in resp:
                                    err_info = resp["error"]
                                    return {"error": err_info.get("message", str(err_info))}
                                if "result" in resp:
                                    content = resp["result"].get("content", [])
                                    texts = [c["text"] for c in content if isinstance(c, dict) and "text" in c]
                                    return "\n".join(texts) if texts else resp["result"]
                        except Exception:
                            continue
                return {"error": stderr or stdout or f"No response matching request ID {target_id} from stdio server."}

            try:
                return await asyncio.to_thread(_stdio)
            except Exception as e:
                return {"error": f"Stdio execution failed for '{qualified_name}': {e}"}

        return {"error": f"Unsupported transport for server '{server}'."}


mcp_client = MCPClientManager()
