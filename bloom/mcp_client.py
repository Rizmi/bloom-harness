"""MCP Client Manager for Local and External MCP Servers.

Provides tool schema discovery, summary extraction, and async tool execution.
"""

import asyncio
import inspect
import json
import os
import re
import subprocess
import sys
import typing
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

def get_project_root() -> Path:
    val = os.environ.get("BLOOM_ROOT", "")
    if val and val.strip():
        return Path(val).resolve()
    return Path(__file__).resolve().parent.parent


def get_servers_file() -> Path:
    val = os.environ.get("BLOOM_SERVERS_FILE", "")
    if val and val.strip():
        return Path(val)
    cwd_file = Path.cwd() / "servers.json"
    if cwd_file.is_file():
        return cwd_file.resolve()
    return Path(__file__).resolve().parent / "servers.json"


def resolve_server_path(path_str: str) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p.resolve()
    candidate = (get_project_root() / p).resolve()
    if candidate.is_file():
        return candidate
    pkg_parent = Path(__file__).resolve().parent.parent
    candidate_pkg = (pkg_parent / p).resolve()
    if candidate_pkg.is_file():
        return candidate_pkg
    return candidate


def _load_local_server(name: str, server_path: Path) -> Optional[Any]:
    if not server_path.is_file():
        print(f"Warning: Local MCP server '{name}' file not found at {server_path}", file=sys.stderr)
        return None

    resolved_str = str(server_path.resolve())
    for mod in list(sys.modules.values()):
        if getattr(mod, "__file__", None) and str(Path(mod.__file__).resolve()) == resolved_str:
            return mod

    try:
        import importlib.util
        mod_name = f"bloom_local_{name}"
        spec = importlib.util.spec_from_file_location(mod_name, resolved_str)
        if spec is None or spec.loader is None:
            print(f"Warning: Could not create module spec for local MCP server '{name}' from {server_path}", file=sys.stderr)
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"Warning: Failed to load local MCP server '{name}' from {server_path}: {e}", file=sys.stderr)
        return None


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
    def __init__(self, servers_file: Optional[Path] = None):
        self.servers_file = servers_file
        self._local_modules: Dict[str, Any] = {}
        self._external_servers: Dict[str, dict] = {}
        self._failed_servers: set = set()
        self._external_tools_cache: Dict[str, dict] = {}
        self._initialized_servers: set = set()
        self._server_sessions: Dict[str, str] = {}
        self._servers_loaded: bool = False

    def get_servers_path(self) -> Path:
        return self.servers_file if self.servers_file is not None else get_servers_file()

    def _ensure_servers(self):
        if not self._servers_loaded:
            self.reload_servers()

    @property
    def local_modules(self) -> Dict[str, Any]:
        self._ensure_servers()
        return self._local_modules

    @property
    def external_servers(self) -> Dict[str, dict]:
        self._ensure_servers()
        return self._external_servers

    @property
    def failed_servers(self) -> set:
        self._ensure_servers()
        return self._failed_servers

    def configure_local_servers(
        self,
        agent_cfg: Optional[Dict[str, Any]] = None,
        skill_paths: Optional[List[str]] = None,
        allowed_skills: Optional[List[str]] = None,
    ):
        if not agent_cfg:
            return
        self._ensure_servers()
        cfg = agent_cfg

        for name, mod in self._local_modules.items():
            if not hasattr(mod, "configure"):
                continue
            tools_map = getattr(mod, "TOOLS", {})
            try:
                sig = inspect.signature(mod.configure)
                accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

                if "file_read" in tools_map or name == "filesystem":
                    kwargs = {
                        "agent_cfg": cfg,
                        "read_paths": cfg.get("read_paths", []),
                        "write_paths": cfg.get("write_paths", []),
                    }
                elif "skill_list" in tools_map or name == "skills":
                    if skill_paths is None:
                        from . import skills
                        skill_paths, allowed_skills = skills.validate_agent_skills(cfg, cfg.get("name", "agent"))
                    kwargs = {
                        "skill_paths": skill_paths,
                        "allowed_skills": allowed_skills,
                        "agent_cfg": cfg,
                    }
                else:
                    kwargs = {"agent_cfg": cfg} if ("agent_cfg" in sig.parameters or accepts_var_kw) else {}

                filtered = {k: v for k, v in kwargs.items() if accepts_var_kw or k in sig.parameters}
                if not filtered and not accepts_var_kw and len(sig.parameters) == 1:
                    mod.configure(cfg)
                else:
                    mod.configure(**filtered)
            except Exception as e:
                print(f"Warning: Error configuring local server '{name}': {e}", file=sys.stderr)

    def reload_servers(self):
        self._servers_loaded = True
        self._local_modules.clear()
        self._external_servers.clear()
        self._failed_servers.clear()
        self._external_tools_cache.clear()
        self._initialized_servers.clear()
        self._server_sessions.clear()

        srv_file = self.get_servers_path()
        if not srv_file.is_file():
            return

        try:
            data = json.loads(srv_file.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", data)
            if not isinstance(servers, dict):
                return
        except Exception as e:
            print(f"Warning: Failed to parse MCP server config {srv_file}: {e}", file=sys.stderr)
            return

        new_external = {}
        for name, cfg in servers.items():
            if not isinstance(cfg, dict) or cfg.get("disabled", False):
                continue
            entry = dict(cfg)
            entry["name"] = name.lower()

            if entry.get("type") == "local" or "path" in entry:
                raw_path = entry.get("path")
                if raw_path:
                    resolved = resolve_server_path(str(raw_path))
                    mod = _load_local_server(entry["name"], resolved)
                    if mod is not None:
                        self._local_modules[entry["name"]] = mod
                    else:
                        self._failed_servers.add(entry["name"])
                continue

            try:
                if "url" in entry:
                    entry["url"] = _expand_env(entry["url"])
                if "command" in entry:
                    entry["command"] = _expand_env(entry["command"])
                if "args" in entry and isinstance(entry["args"], list):
                    entry["args"] = [_expand_env(a) if isinstance(a, str) else a for a in entry["args"]]
                if "cwd" in entry and isinstance(entry["cwd"], str):
                    entry["cwd"] = _expand_env(entry["cwd"])
                if "env" in entry and isinstance(entry["env"], dict):
                    entry["env"] = {k: _expand_env(v) for k, v in entry["env"].items()}
                new_external[entry["name"]] = entry
            except Exception as e:
                print(f"Warning: Disabling server '{name}': {e}", file=sys.stderr)
                self._failed_servers.add(name.lower())

        self._external_servers = new_external

    def get_tool_summary(self, qualified_name: str) -> str:
        """Return a 1-line summary for prompt injection."""
        self._ensure_servers()
        if "." not in qualified_name:
            return qualified_name

        server, tool = qualified_name.split(".", 1)
        server = server.lower()

        if server in self._local_modules:
            mod = self._local_modules[server]
            func = getattr(mod, "TOOLS", {}).get(tool)
            if func:
                meta = getattr(func, "_mcp_meta", {}) or {}
                if "summary" in meta:
                    return meta["summary"]
                doc = inspect.getdoc(func) or ""
                return doc.splitlines()[0] if doc else f"Tool {qualified_name}"

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
        self._ensure_servers()
        if "." not in qualified_name:
            return {"error": f"Tool '{qualified_name}' must be in 'server.tool' format."}

        server, tool = qualified_name.split(".", 1)
        server = server.lower()

        if server in self._local_modules:
            mod = self._local_modules[server]
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

        srv = self._external_servers.get(server)
        if not srv:
            return {"error": f"Unknown MCP server '{server}' for tool '{qualified_name}'."}

        if srv.get("transport") == "sse":
            return {"error": f"Transport 'sse' is deprecated; use 'streamable_http' for server '{server}'."}

        if qualified_name not in self._external_tools_cache:
            await self._fetch_external_tools(server)

        t = self._external_tools_cache.get(qualified_name)
        if not t:
            return {"error": f"Tool '{tool}' not found on server '{server}'."}

        return {
            "name": qualified_name,
            "description": t.get("description", ""),
            "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
        }

    def _call_external_http(self, srv: dict, server_name: str, payload: dict) -> dict:
        url = srv.get("url", "")
        if server_name not in self._initialized_servers:
            init_req = urllib.request.Request(
                url,
                data=json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "bloom", "version": "1.0.0"},
                    },
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                method="POST",
            )
            with urllib.request.urlopen(init_req, timeout=10) as r:
                init_resp = _parse_mcp_http_response(r)
                if isinstance(init_resp, dict) and "error" in init_resp:
                    return {"error": f"MCP initialization failed on '{server_name}': {init_resp['error']}"}
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
        with urllib.request.urlopen(req, timeout=30) as r:
            return _parse_mcp_http_response(r)

    def _call_external_stdio(self, srv: dict, server_name: str, payload: dict) -> dict:
        cmd = [srv["command"]] + srv.get("args", [])
        clean_env = _get_subprocess_env(srv)
        proc = subprocess.Popen(
            cmd,
            cwd=srv.get("cwd"),
            env=clean_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        input_lines = [
            json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "bloom", "version": "1.0.0"},
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
            return {"error": f"MCP server '{server_name}' timed out after 30 seconds."}
        except Exception as e:
            proc.kill()
            return {"error": f"Stdio execution failed for '{server_name}': {e}"}

        target_id = payload.get("id")
        if stdout:
            for line in stdout.strip().splitlines():
                try:
                    resp = json.loads(line)
                    if resp.get("id") == target_id:
                        return resp
                except Exception:
                    continue
        return {"error": stderr or stdout or f"No response matching request ID {target_id} from stdio server."}

    async def _fetch_external_tools(self, server_name: str) -> Dict[str, dict]:
        srv = self._external_servers.get(server_name.lower())
        if not srv:
            return {}

        if srv.get("transport") == "sse":
            print(f"Warning: Server '{server_name}' uses deprecated 'sse' transport; please use 'streamable_http'.", file=sys.stderr)
            return {}

        payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        try:
            if srv.get("transport") in ("http", "streamable_http") and "url" in srv:
                resp = await asyncio.to_thread(self._call_external_http, srv, server_name, payload)
            elif srv.get("transport") == "stdio" and "command" in srv:
                resp = await asyncio.to_thread(self._call_external_stdio, srv, server_name, payload)
            else:
                return {}
        except Exception as e:
            print(f"Warning: Failed to fetch tools from '{server_name}': {e}", file=sys.stderr)
            return {}

        tools_list = []
        if isinstance(resp, dict) and "result" in resp:
            result = resp["result"]
            if isinstance(result, dict):
                raw_tools = result.get("tools")
                tools_list = raw_tools if isinstance(raw_tools, list) else []
                for t in tools_list:
                    if isinstance(t, dict):
                        t_name = t.get("name")
                        if t_name:
                            self._external_tools_cache[f"{server_name}.{t_name}"] = t
        elif isinstance(resp, dict) and "error" in resp:
            print(f"Warning: MCP server '{server_name}' returned error on tools/list: {resp['error']}", file=sys.stderr)
        return {t["name"]: t for t in tools_list if isinstance(t, dict) and "name" in t}

    async def call_tool(self, qualified_name: str, arguments: Optional[dict] = None) -> Any:
        """Execute a tool call against local or external MCP server."""
        self._ensure_servers()
        if "." not in qualified_name:
            return {"error": f"Tool '{qualified_name}' must be in 'server.tool' format."}

        server, tool = qualified_name.split(".", 1)
        server = server.lower()
        args = arguments if isinstance(arguments, dict) else {}

        if server in self._local_modules:
            mod = self._local_modules[server]
            func = getattr(mod, "TOOLS", {}).get(tool)
            if not func:
                return {"error": f"Tool '{tool}' not found on local server '{server}'."}

            try:
                sig = inspect.signature(func)
                bound_args = sig.bind(**args)
                bound_args.apply_defaults()
            except TypeError as te:
                valid_params = list(sig.parameters.keys())
                return {"error": f"Invalid arguments for tool '{tool}': {te}. Supported parameters: {valid_params}"}

            try:
                if inspect.iscoroutinefunction(func):
                    return await func(*bound_args.args, **bound_args.kwargs)
                return await asyncio.to_thread(func, *bound_args.args, **bound_args.kwargs)
            except Exception as e:
                return {"error": str(e)}

        srv = self._external_servers.get(server)
        if not srv:
            return {"error": f"Unknown MCP server '{server}' for tool '{qualified_name}'."}

        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }

        if srv.get("transport") == "sse":
            return {"error": f"Transport 'sse' is deprecated; use 'streamable_http' for server '{server}'."}

        try:
            if srv.get("transport") in ("http", "streamable_http") and "url" in srv:
                resp = await asyncio.to_thread(self._call_external_http, srv, server, payload)
            elif srv.get("transport") == "stdio" and "command" in srv:
                resp = await asyncio.to_thread(self._call_external_stdio, srv, server, payload)
            else:
                return {"error": f"Unsupported transport for server '{server}'."}
        except Exception as e:
            return {"error": f"External MCP call failed for '{qualified_name}': {e}"}

        if isinstance(resp, dict):
            if "error" in resp:
                err_info = resp["error"]
                return {"error": err_info.get("message", str(err_info)) if isinstance(err_info, dict) else str(err_info)}
            if "result" in resp:
                content = resp["result"].get("content", [])
                texts = [c["text"] for c in content if isinstance(c, dict) and "text" in c]
                return "\n".join(texts) if texts else resp["result"]
        return resp


mcp_client = MCPClientManager()
