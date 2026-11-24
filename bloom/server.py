"""Bloom Headless HTTP/1.1 & Server-Sent Events (SSE) API Server.

Enables web dashboards, automation pipelines, CI jobs, and scripts to drive
Bloom agents headlessly with:
- Full REST endpoints for agent/model/tool discovery and session management.
- Live streaming of text deltas, tool executions, token metrics, and active context %.
- Interactive human confirmation approval gates (ask tier) over HTTP.
- Zero extra external dependencies (pure Python standard library asyncio).
"""

import asyncio
import json
import os
import re
import sys
import urllib.parse
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import commands
from . import llm
from . import manifest
from . import session
from . import tools
from .mcp_client import mcp_client
from .runner import run_turn, unwrap_tool_info
from .ui import ui

_PINNED_AGENT = "assistant"
_IS_BUSY = False
_BUSY_OP: Optional[str] = None
_PENDING_APPROVALS: Dict[str, Dict[str, Any]] = {}
_ACTIVE_TURNS: Dict[str, asyncio.Task] = {}
_SESSION_USAGES: Dict[str, Dict[str, Any]] = {}


def configure_agent(agent_name: str) -> Tuple[str, Dict[str, Any]]:
    """Initialize agent manifest, permissions, tools, and LLM config."""
    global _PINNED_AGENT
    try:
        name, cfg = manifest.resolve_safe(agent_name)
    except Exception:
        name, cfg = manifest.resolve(agent_name)
    _PINNED_AGENT = name
    session.set_agent(name)
    mcp_client.reload_servers()
    from . import skills
    auth_skill_paths, auth_skills = skills.validate_agent_skills(cfg, name)
    tools.setup(cfg)
    mcp_client.configure_local_servers(
        cfg, skill_paths=auth_skill_paths, allowed_skills=auth_skills
    )
    llm.setup(cfg, skill_paths=auth_skill_paths, allowed_skills=auth_skills)
    return name, cfg


def _format_agent_manifest(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "model": cfg.get("model"),
        "system_prompt": cfg.get("system_prompt"),
        "read_paths": cfg.get("read_paths", []),
        "write_paths": cfg.get("write_paths", []),
        "skill_paths": cfg.get("skill_paths", []),
        "permissions": cfg.get("permissions", {}),
    }


# -----------------------------------------------------------------------------
# HTTP Helpers
# -----------------------------------------------------------------------------

def _cors_headers() -> bytes:
    origin = os.environ.get("BLOOM_CORS_ORIGIN", "*").strip()
    if not origin:
        origin = "*"
    return (
        f"Access-Control-Allow-Origin: {origin}\r\n"
        f"Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n"
        f"Access-Control-Allow-Headers: Content-Type, Authorization, X-Agent\r\n"
    ).encode("utf-8")


def _check_auth(headers: Dict[str, str], query: Dict[str, List[str]]) -> bool:
    """Validate Bearer token if BLOOM_API_TOKEN is set in the environment."""
    token = os.environ.get("BLOOM_API_TOKEN", "").strip()
    if not token:
        return True
    auth_hdr = headers.get("authorization", "")
    if auth_hdr.startswith("Bearer "):
        if auth_hdr[7:].strip() == token:
            return True
    query_token = query.get("token", [""])[0].strip()
    if query_token == token:
        return True
    return False


async def send_json(writer: asyncio.StreamWriter, status_code: int, data: Any):
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")
    status_text = {
        200: "OK", 201: "Created", 204: "No Content",
        400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
        404: "Not Found", 409: "Conflict", 413: "Payload Too Large",
        500: "Internal Server Error"
    }.get(status_code, "OK")
    headers = (
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
    ).encode("utf-8") + _cors_headers() + b"\r\n"
    writer.write(headers + body)
    await writer.drain()


async def send_options(writer: asyncio.StreamWriter):
    headers = b"HTTP/1.1 204 No Content\r\n" + _cors_headers() + b"\r\n"
    writer.write(headers)
    await writer.drain()


async def start_sse(writer: asyncio.StreamWriter):
    headers = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: keep-alive\r\n"
    ) + _cors_headers() + b"\r\n"
    writer.write(headers)
    await writer.drain()


async def send_sse(writer: asyncio.StreamWriter, event_dict: dict):
    line = f"data: {json.dumps(event_dict, separators=(',', ':'))}\n\n".encode("utf-8")
    writer.write(line)
    await writer.drain()


# -----------------------------------------------------------------------------
# Request Dispatcher
# -----------------------------------------------------------------------------

async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
        except asyncio.TimeoutError:
            writer.close()
            return

        if not request_line:
            writer.close()
            return

        parts = request_line.decode("utf-8", errors="replace").split()
        if len(parts) < 2:
            writer.close()
            return
        method, full_path = parts[0].upper(), parts[1]

        headers: Dict[str, str] = {}
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            except asyncio.TimeoutError:
                writer.close()
                return
            if not line or line == b"\r\n":
                break
            line_str = line.decode("utf-8", errors="replace")
            if ":" in line_str:
                k, v = line_str.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        parsed_url = urllib.parse.urlparse(full_path)
        path = parsed_url.path.rstrip("/")
        if not path:
            path = "/"
        query = urllib.parse.parse_qs(parsed_url.query)

        if method == "OPTIONS":
            await send_options(writer)
            return

        if not _check_auth(headers, query):
            await send_json(writer, 401, {"error": "Unauthorized: Missing or invalid API token."})
            return

        body = b""
        if "content-length" in headers:
            try:
                length = int(headers["content-length"])
                if length > 2 * 1024 * 1024:
                    await send_json(writer, 413, {"error": "Payload Too Large: Request body exceeds 2MB limit."})
                    return
                body = await asyncio.wait_for(reader.readexactly(length), timeout=30.0)
            except Exception:
                pass

        json_body = {}
        if body:
            try:
                parsed_json = json.loads(body.decode("utf-8", errors="replace"))
                if not isinstance(parsed_json, dict):
                    await send_json(writer, 400, {"error": "Invalid JSON body. Expected a JSON object."})
                    return
                json_body = parsed_json
            except Exception:
                await send_json(writer, 400, {"error": "Malformed JSON in request body."})
                return

        await dispatch(method, path, query, json_body, headers, writer)

    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as ex:
        print(f"Server error handling {method} {path}: {ex}", file=sys.stderr)
        try:
            await send_json(writer, 500, {"error": str(ex)})
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def dispatch(
    method: str,
    path: str,
    query: Dict[str, List[str]],
    body: Dict[str, Any],
    headers: Dict[str, str],
    writer: asyncio.StreamWriter,
):
    global _IS_BUSY, _BUSY_OP

    # Agent scope guard: enforce pinned agent
    req_agent = query.get("agent", [headers.get("x-agent", body.get("agent", ""))])[0].strip()
    if req_agent and req_agent != _PINNED_AGENT:
        await send_json(writer, 400, {
            "error": f"Server is pinned to agent '{_PINNED_AGENT}'. Requested agent '{req_agent}' is not permitted."
        })
        return

    # ----------------------------------------------------------------- /api/info
    if path == "/api/info" and method == "GET":
        cur_model = getattr(llm, "MODEL_KEY", None) or getattr(llm, "MODEL", "unknown")
        await send_json(writer, 200, {
            "status": "ready",
            "model": cur_model,
            "version": "0.2.0",
            "active_agent": _PINNED_AGENT,
            "active_model": cur_model,
            "context_limit": getattr(llm, "CONTEXT_WINDOW", 64000),
            "agents": manifest.list_agents(),
            "models": list(llm.list_models().keys()),
        })
        return

    # ----------------------------------------------------------------- /api/agents
    if path == "/api/agents" and method == "GET":
        results = []
        for a in manifest.list_agents():
            try:
                _, cfg = manifest.resolve_safe(a)
                results.append(_format_agent_manifest(a, cfg))
            except Exception:
                pass
        await send_json(writer, 200, results)
        return

    if path.startswith("/api/agents/") and method == "GET":
        name = path[len("/api/agents/"):]
        try:
            _, cfg = manifest.resolve_safe(name)
            await send_json(writer, 200, _format_agent_manifest(name, cfg))
        except Exception as e:
            await send_json(writer, 404, {"error": f"Agent '{name}' not found: {e}"})
        return

    # ----------------------------------------------------------------- /api/model & /api/models
    if path in ("/api/model", "/api/models") and method == "GET":
        raw_models = llm.list_models()
        sanitized_models = {}
        groups_map: Dict[str, List[Dict[str, str]]] = {}
        for m_name, m_spec in raw_models.items():
            provider = "default"
            label = m_name
            if isinstance(m_spec, dict):
                sanitized_models[m_name] = {k: v for k, v in m_spec.items() if k != "api_key"}
                provider = m_spec.get("provider", "default")
                label = m_spec.get("label", m_name)
            else:
                sanitized_models[m_name] = m_spec
            if provider not in groups_map:
                groups_map[provider] = []
            groups_map[provider].append({"id": m_name, "name": label})

        groups = [{"provider": p, "models": m_list} for p, m_list in groups_map.items()]
        cur_model = getattr(llm, "MODEL_KEY", None) or getattr(llm, "MODEL", None)
        await send_json(writer, 200, {
            "status": "ready",
            "current": cur_model,
            "active": cur_model,
            "groups": groups,
            "models": sanitized_models,
        })
        return

    if path in ("/api/model", "/api/models/switch") and method == "POST":
        if _IS_BUSY:
            await send_json(writer, 409, {"error": f"Operation rejected: server is currently busy with {_BUSY_OP}."})
            return
        target_model = body.get("model")
        if not target_model:
            await send_json(writer, 400, {"error": "Missing 'model' field in request body."})
            return
        _IS_BUSY = True
        _BUSY_OP = "model_switch"
        try:
            switched = llm.switch_model(target_model)
            await send_json(writer, 200, {
                "status": "ok",
                "active_model": switched,
                "model": switched,
                "current": switched,
            })
        except KeyError as ke:
            await send_json(writer, 404, {"error": str(ke)})
        except Exception as e:
            await send_json(writer, 400, {"error": f"Model switch failed: {e}"})
        finally:
            _IS_BUSY = False
            _BUSY_OP = None
        return

    # ----------------------------------------------------------------- /api/tools
    if path == "/api/tools" and method == "GET":
        tool_items = []
        for t in sorted(tools.ALLOW_TOOLS | tools.ASK_TOOLS):
            tier = "ask" if t in tools.ASK_TOOLS else "allow"
            tool_items.append({"name": t, "tier": tier})
        await send_json(writer, 200, tool_items)
        return

    if path.startswith("/api/tools/") and path.endswith("/describe") and method == "GET":
        tool_name = path[len("/api/tools/"):-len("/describe")]
        try:
            schema_str = await tools.tool_describe(tool_name)
            parsed = json.loads(schema_str)
            if "error" in parsed:
                await send_json(writer, 403, parsed)
            else:
                await send_json(writer, 200, parsed)
        except Exception as e:
            await send_json(writer, 404, {"error": f"Tool '{tool_name}' description error: {e}"})
        return

    # ----------------------------------------------------------------- /api/skills
    if path == "/api/skills" and method == "GET":
        from . import skills
        try:
            _, a_cfg = manifest.resolve_safe(_PINNED_AGENT)
            auth_skill_paths, auth_skills = skills.validate_agent_skills(a_cfg, _PINNED_AGENT)
            avail = skills.get_available_skills(
                a_cfg, _PINNED_AGENT,
                auth_roots=auth_skill_paths, auth_skills=auth_skills
            )
            await send_json(writer, 200, avail)
        except Exception as e:
            await send_json(writer, 400, {"error": str(e)})
        return

    if path.startswith("/api/skills/") and method == "GET":
        skill_name = path[len("/api/skills/"):]
        perm = tools.check_permission("skills.skill_load")
        if perm == "deny":
            await send_json(writer, 403, {"error": f"Permission denied: 'skills.skill_load' is not permitted for agent '{_PINNED_AGENT}'."})
            return
        res = await mcp_client.call_tool("skills.skill_load", {"skill_name": skill_name})
        if isinstance(res, str) and res.startswith("Error:"):
            await send_json(writer, 404, {"error": res})
        elif isinstance(res, dict) and "error" in res:
            await send_json(writer, 404, {"error": res["error"]})
        else:
            await send_json(writer, 200, {"skill": skill_name, "content": res})
        return

    # ----------------------------------------------------------------- /api/sessions & /api/session/active
    if path == "/api/session/active" and method == "GET":
        active_map = {sid: True for sid, t in _ACTIVE_TURNS.items() if not t.done()}
        await send_json(writer, 200, {"data": active_map})
        return

    if path == "/api/sessions" and method == "GET":
        sessions_list = session.all_sessions(_PINNED_AGENT)
        await send_json(writer, 200, sessions_list)
        return

    if path == "/api/session" and method == "POST":
        if _IS_BUSY:
            await send_json(writer, 409, {"error": f"Operation rejected: server is currently busy with {_BUSY_OP}."})
            return
        _IS_BUSY = True
        _BUSY_OP = "session_create"
        try:
            req_sid = body.get("id") or body.get("sessionId")
            if req_sid and not session.is_valid_session_id(req_sid):
                await send_json(writer, 400, {"error": f"Invalid session ID format: '{req_sid}'."})
                return
            sid = session.new_session(req_sid)
            session.save([{"role": "system", "content": llm.SYSTEM_PROMPT}])
            await send_json(writer, 201, {
                "id": sid,
                "agent": _PINNED_AGENT,
                "data": {"id": sid, "agent": _PINNED_AGENT},
            })
        finally:
            _IS_BUSY = False
            _BUSY_OP = None
        return

    # ----------------------------------------------------------------- /api/session/{id}/message
    m_msg = re.match(r"^/api/session/([^/]+)/message$", path)
    if m_msg and method == "GET":
        sid = m_msg.group(1)
        if not session.is_valid_session_id(sid):
            await send_json(writer, 400, {"error": f"Invalid session ID format: '{sid}'."})
            return
        msgs = session.load(sid, _PINNED_AGENT)
        if not msgs:
            await send_json(writer, 404, {"error": f"Session '{sid}' not found."})
            return

        entries = []
        tool_outputs: Dict[str, str] = {}
        for m in msgs:
            if m.get("role") == "tool":
                tc_id = m.get("tool_call_id")
                if tc_id:
                    tool_outputs[tc_id] = m.get("content", "")

        for idx, m in enumerate(msgs):
            role = m.get("role")
            if role == "system":
                continue
            if role == "user":
                entries.append({
                    "id": f"msg_{idx}",
                    "role": "user",
                    "content": [{"type": "text", "text": m.get("content", "")}],
                })
            elif role == "assistant":
                parts = []
                if m.get("content"):
                    parts.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    tc_id = tc.get("id")
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", {})
                    display_tool, display_args = unwrap_tool_info(fn_name, fn_args)
                    parts.append({
                        "type": "tool",
                        "callId": tc_id,
                        "name": display_tool,
                        "tool": display_tool,
                        "input": display_args,
                        "output": tool_outputs.get(tc_id, ""),
                        "state": "completed",
                    })
                entries.append({
                    "id": f"msg_{idx}",
                    "role": "assistant",
                    "content": parts,
                })

        u = _SESSION_USAGES.get(sid, {})
        context_limit = getattr(llm, "CONTEXT_WINDOW", 64000) or 64000
        p_tok = u.get("prompt_tokens", 0) or 0
        c_tok = u.get("completion_tokens", 0) or 0
        k_tok = u.get("cached_tokens", 0) or 0
        usage_obj = {
            "promptTokens": p_tok,
            "candidatesTokens": c_tok,
            "totalTokens": p_tok + c_tok,
            "cachedTokens": k_tok,
            "contextTokens": p_tok,
            "contextLimit": context_limit,
        }
        await send_json(writer, 200, {"data": entries, "usage": usage_obj})
        return

    # ----------------------------------------------------------------- /api/session/{id}/usage
    m_usage = re.match(r"^/api/session/([^/]+)/usage$", path)
    if m_usage and method == "GET":
        sid = m_usage.group(1)
        if not session.is_valid_session_id(sid):
            await send_json(writer, 400, {"error": f"Invalid session ID format: '{sid}'."})
            return
        u = _SESSION_USAGES.get(sid, {})
        context_limit = getattr(llm, "CONTEXT_WINDOW", 64000) or 64000
        p_tok = u.get("prompt_tokens", 0) or 0
        c_tok = u.get("completion_tokens", 0) or 0
        k_tok = u.get("cached_tokens", 0) or 0
        await send_json(writer, 200, {
            "promptTokens": p_tok,
            "candidatesTokens": c_tok,
            "totalTokens": p_tok + c_tok,
            "cachedTokens": k_tok,
            "contextTokens": p_tok,
            "contextLimit": context_limit,
        })
        return

    # ----------------------------------------------------------------- /api/session/{id}
    m_sess = re.match(r"^/api/session/([^/]+)$", path)
    if m_sess:
        sid = m_sess.group(1)
        if not session.is_valid_session_id(sid):
            await send_json(writer, 400, {"error": f"Invalid session ID format: '{sid}'."})
            return
        if method == "GET":
            msgs = session.load(sid, _PINNED_AGENT)
            if not msgs:
                await send_json(writer, 404, {"error": f"Session '{sid}' not found."})
                return
            await send_json(writer, 200, {"id": sid, "agent": _PINNED_AGENT, "messages": msgs})
            return
        elif method == "DELETE":
            if _IS_BUSY:
                await send_json(writer, 409, {"error": f"Operation rejected: server is currently busy with {_BUSY_OP}."})
                return
            _IS_BUSY = True
            _BUSY_OP = f"delete_session_{sid}"
            try:
                deleted = session.delete_session(sid, _PINNED_AGENT)
                if deleted:
                    _SESSION_USAGES.pop(sid, None)
                    await send_json(writer, 200, {"status": "deleted", "id": sid})
                else:
                    await send_json(writer, 404, {"error": f"Session '{sid}' not found."})
            finally:
                _IS_BUSY = False
                _BUSY_OP = None
            return

    # ----------------------------------------------------------------- /api/session/{id}/compact
    m_compact = re.match(r"^/api/session/([^/]+)/compact$", path)
    if m_compact and method == "POST":
        sid = m_compact.group(1)
        if not session.is_valid_session_id(sid):
            await send_json(writer, 400, {"error": f"Invalid session ID format: '{sid}'."})
            return
        if _IS_BUSY:
            await send_json(writer, 409, {"error": f"Operation rejected: server is currently busy with {_BUSY_OP}."})
            return
        _IS_BUSY = True
        _BUSY_OP = f"compact_{sid}"
        try:
            msgs = session.open_session(sid, _PINNED_AGENT)
            if msgs is None:
                await send_json(writer, 404, {"error": f"Session '{sid}' not found."})
                return
            before_count = len(msgs)
            compacted = await commands.compact(msgs)
            if compacted is msgs or len(compacted) == before_count:
                await send_json(writer, 200, {
                    "status": "not_compacted",
                    "reason": "No messages old enough to compact.",
                    "message_count": before_count,
                })
            else:
                await send_json(writer, 200, {
                    "status": "compacted",
                    "before_count": before_count,
                    "after_count": len(compacted),
                })
        finally:
            _IS_BUSY = False
            _BUSY_OP = None
        return

    # ----------------------------------------------------------------- /api/session/{id}/rewind-candidates
    m_rewind_cand = re.match(r"^/api/session/([^/]+)/rewind-candidates$", path)
    if m_rewind_cand and method == "GET":
        sid = m_rewind_cand.group(1)
        if not session.is_valid_session_id(sid):
            await send_json(writer, 400, {"error": f"Invalid session ID format: '{sid}'."})
            return
        msgs = session.load(sid, _PINNED_AGENT)
        if not msgs:
            await send_json(writer, 404, {"error": f"Session '{sid}' not found."})
            return
        cands = commands.get_rewind_candidates(msgs)
        await send_json(writer, 200, cands)
        return

    # ----------------------------------------------------------------- /api/session/{id}/rewind
    m_rewind = re.match(r"^/api/session/([^/]+)/rewind$", path)
    if m_rewind and method == "POST":
        sid = m_rewind.group(1)
        if not session.is_valid_session_id(sid):
            await send_json(writer, 400, {"error": f"Invalid session ID format: '{sid}'."})
            return
        if _IS_BUSY:
            await send_json(writer, 409, {"error": f"Operation rejected: server is currently busy with {_BUSY_OP}."})
            return
        target_idx = body.get("targetIndex")
        msgs = session.open_session(sid, _PINNED_AGENT)
        if msgs is None:
            await send_json(writer, 404, {"error": f"Session '{sid}' not found."})
            return
        if not isinstance(target_idx, int) or target_idx <= 0 or target_idx >= len(msgs):
            await send_json(writer, 400, {
                "error": f"Invalid 'targetIndex': {target_idx}. Must be an integer between 1 and {len(msgs) - 1}."
            })
            return
        _IS_BUSY = True
        _BUSY_OP = f"rewind_{sid}"
        try:
            session.rewind_to(target_idx)
            rewound_msgs = msgs[:target_idx]
            await send_json(writer, 200, {
                "status": "rewound",
                "targetIndex": target_idx,
                "remaining_messages": len(rewound_msgs),
            })
        finally:
            _IS_BUSY = False
            _BUSY_OP = None
        return

    # ----------------------------------------------------------------- /api/permissions
    if path in ("/api/permissions", "/api/agent/permissions") and method == "GET":
        sid_filter = query.get("sessionId", [""])[0].strip()
        pending = [
            {
                "requestId": k,
                "sessionId": v["sessionId"],
                "tool": v["tool"],
                "arguments": v["arguments"],
                "prompt": v["prompt"],
                "action": v.get("action") or f"Execute {v['tool']}",
                "hint": v.get("hint") or v["prompt"],
                "resources": v.get("resources") or [],
                "createdAt": v.get("created_at"),
            }
            for k, v in _PENDING_APPROVALS.items()
            if not sid_filter or v["sessionId"] == sid_filter
        ]
        await send_json(writer, 200, {"pending": pending})
        return

    # ----------------------------------------------------------------- /api/session/{id}/approval
    m_approval = re.match(r"^/api/session/([^/]+)/approval$", path)
    if m_approval and method == "POST":
        sid = m_approval.group(1)
        req_id = body.get("requestId")
        decision = body.get("decision", "rejected")
        info = _PENDING_APPROVALS.get(req_id)
        if not req_id or not info or info.get("sessionId") != sid:
            await send_json(writer, 404, {"error": f"No pending approval '{req_id}' for session '{sid}'."})
            return

        fut = info["future"]
        approved = decision in ("approved", "allow", "yes", "y", "once")

        if not fut.done():
            fut.set_result(approved)

        # Emit permission_settled to the prompt SSE stream
        settle_event = {
            "type": "permission_settled",
            "requestId": req_id,
            "sessionId": sid,
            "approved": approved,
        }
        emit_all_fn = info.get("emit_all")
        if emit_all_fn:
            try:
                await emit_all_fn(settle_event)
            except Exception:
                pass

        await send_json(writer, 200, {
            "status": "resolved",
            "requestId": req_id,
            "approved": approved,
            "success": True,
            "decision": "approved" if approved else "rejected",
        })
        return

    # ----------------------------------------------------------------- /api/session/{id}/abort
    m_abort = re.match(r"^/api/session/([^/]+)/abort$", path)
    if m_abort and method == "POST":
        sid = m_abort.group(1)
        task = _ACTIVE_TURNS.get(sid)
        if task and not task.done():
            task.cancel()
            for r_id, r_info in list(_PENDING_APPROVALS.items()):
                if r_info.get("sessionId") == sid:
                    fut = r_info["future"]
                    if not fut.done():
                        fut.cancel()
                    _PENDING_APPROVALS.pop(r_id, None)
            _ACTIVE_TURNS.pop(sid, None)
            await send_json(writer, 200, {"status": "aborted", "sessionId": sid, "success": True, "aborted": True})
        else:
            await send_json(writer, 200, {"status": "not_running", "sessionId": sid, "success": True, "aborted": False})
        return

    # ----------------------------------------------------------------- /api/mcp
    if path == "/api/mcp" and method == "GET":
        mcp_client._ensure_servers()
        servers = []
        for name in sorted(mcp_client.local_modules.keys()):
            servers.append({"name": name, "type": "local", "status": "connected"})
        for name, cfg in sorted(mcp_client.external_servers.items()):
            transport = cfg.get("transport", "external")
            servers.append({"name": name, "type": transport, "status": "connected"})
        await send_json(writer, 200, {
            "status": "ready",
            "data": servers,
            "servers": servers,
        })
        return

    # ----------------------------------------------------------------- /api/session/{id}/prompt (SSE Stream)
    m_prompt = re.match(r"^/api/session/([^/]+)/prompt$", path)
    if m_prompt and method == "POST":
        sid = m_prompt.group(1)
        if not session.is_valid_session_id(sid):
            await send_json(writer, 400, {"error": f"Invalid session ID format: '{sid}'."})
            return

        if _IS_BUSY:
            await send_json(writer, 409, {"error": f"A turn is currently in progress ({_BUSY_OP}). Bloom processes one turn at a time."})
            return

        user_text = body.get("text", "").strip()
        req_model = body.get("model")
        if req_model and req_model != getattr(llm, "MODEL", None):
            try:
                llm.switch_model(req_model)
            except Exception:
                pass

        files = body.get("files", [])
        if isinstance(files, list) and files:
            file_tags = []
            for f in files:
                if isinstance(f, dict):
                    fn = f.get("name") or f.get("filename") or "attachment"
                    fu = f.get("uri") or f.get("url") or ""
                    file_tags.append(f"[Attachment: {fn} ({fu})]")
            if file_tags:
                prefix = f"{user_text}\n\n" if user_text else ""
                user_text = prefix + "\n".join(file_tags)

        if not user_text:
            await send_json(writer, 400, {"error": "Missing 'text' in request body."})
            return

        if user_text.startswith("/"):
            await send_json(writer, 400, {
                "error": f"Slash command '{user_text.split()[0]}' is not supported in prompt turns. Use dedicated REST API endpoints."
            })
            return

        msgs = session.open_session(sid, _PINNED_AGENT)
        if msgs is None:
            await send_json(writer, 404, {"error": f"Session '{sid}' not found. Create it with POST /api/session first."})
            return

        # Synchronously lock BEFORE any await
        _IS_BUSY = True
        _BUSY_OP = f"prompt_{sid}"

        await start_sse(writer)
        await run_turn_sse(sid, msgs, user_text, writer)
        return

    await send_json(writer, 404, {"error": f"Route '{method} {path}' not found."})


# -----------------------------------------------------------------------------
# Streaming Turn Execution Engine (SSE)
# -----------------------------------------------------------------------------

async def run_turn_sse(
    session_id: str,
    messages: list,
    user_text: str,
    writer: asyncio.StreamWriter,
):
    global _IS_BUSY, _BUSY_OP
    current_task = asyncio.current_task()
    _ACTIVE_TURNS[session_id] = current_task

    approval_timeout = float(os.environ.get("BLOOM_APPROVAL_TIMEOUT", "300.0"))

    async def emit_all(ev: dict):
        await send_sse(writer, ev)

    async def sse_approval_handler(tool_name: str, args: dict, prompt_text: str) -> bool:
        req_id = f"req_{uuid.uuid4().hex[:8]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        action_text = f"Execute {tool_name}"
        res_list = [f"{k}={v}" for k, v in args.items()] if isinstance(args, dict) else []
        _PENDING_APPROVALS[req_id] = {
            "sessionId": session_id,
            "future": fut,
            "tool": tool_name,
            "arguments": args,
            "prompt": prompt_text,
            "action": action_text,
            "hint": prompt_text,
            "resources": res_list,
            "created_at": datetime.now().isoformat(),
            "emit_all": emit_all,
        }

        perm_event = {
            "type": "permission",
            "requestId": req_id,
            "sessionId": session_id,
            "tool": tool_name,
            "arguments": args,
            "prompt": prompt_text,
            "action": action_text,
            "hint": prompt_text,
            "resources": res_list,
        }
        await emit_all(perm_event)

        try:
            approved = await asyncio.wait_for(fut, timeout=approval_timeout)
            return bool(approved)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return False
        finally:
            _PENDING_APPROVALS.pop(req_id, None)

    old_approval_handler = tools.get_approval_handler()
    tools.set_approval_handler(sse_approval_handler)

    heartbeat_task = None
    async def heartbeat_loop():
        try:
            while True:
                await asyncio.sleep(15.0)
                writer.write(b": ping\n\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, ConnectionError):
            if current_task and not current_task.done():
                current_task.cancel()
        except asyncio.CancelledError:
            pass

    heartbeat_task = asyncio.create_task(heartbeat_loop())

    try:
        await emit_all({"type": "start", "sessionId": session_id, "agent": _PINNED_AGENT})

        from .agent import refresh_system_prompt
        refresh_system_prompt(messages, llm.SYSTEM_PROMPT)

        try:
            max_tool_rounds = max(1, int(os.environ.get("BLOOM_MAX_TOOL_ROUNDS", "50")))
        except ValueError:
            max_tool_rounds = 50

        msgs_res, turn_usage = await run_turn(
            messages,
            user_text,
            max_tool_rounds=max_tool_rounds,
            on_event=emit_all,
        )
        if turn_usage:
            _SESSION_USAGES[session_id] = turn_usage

    except asyncio.CancelledError:
        try:
            await emit_all({"type": "error", "error": "Turn aborted by client."})
            await emit_all({"type": "done", "reason": "aborted"})
        except Exception:
            pass
    except Exception as ex:
        try:
            await emit_all({"type": "error", "error": str(ex)})
            await emit_all({"type": "done", "reason": "error"})
        except Exception:
            pass
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        tools.set_approval_handler(old_approval_handler)
        if _ACTIVE_TURNS.get(session_id) is current_task:
            _ACTIVE_TURNS.pop(session_id, None)
        _IS_BUSY = False
        _BUSY_OP = None


# -----------------------------------------------------------------------------
# Server Lifecycle
# -----------------------------------------------------------------------------

async def run_server(host: str = "127.0.0.1", port: int = 4096, initial_agent: str = "assistant"):
    """Start the Bloom headless HTTP/SSE server."""
    from .agent import load_env
    load_env()
    ui.set_quiet(True)
    try:
        configure_agent(initial_agent)
    except Exception as e:
        sys.exit(f"bloom server: error: failed to initialize agent '{initial_agent}': {e}")

    cors_origin = os.environ.get("BLOOM_CORS_ORIGIN", "*")
    token_configured = bool(os.environ.get("BLOOM_API_TOKEN"))

    server = await asyncio.start_server(handle_request, host, port)
    print(f"\n───────────────────────────────  bloom server  ────────────────────────────────")
    print(f"  Headless API Endpoint : http://{host}:{port}/api/info")
    print(f"  Pinned Agent Scope    : {_PINNED_AGENT}")
    print(f"  REST + SSE Streaming  : ENABLED (CORS: {cors_origin})")
    print(f"  Authentication Token  : {'CONFIGURED' if token_configured else 'NONE (Open)'}")
    if host == "0.0.0.0" and not token_configured:
        print(f"  SECURITY WARNING      : Binding to 0.0.0.0 without BLOOM_API_TOKEN is dangerous!")
    print(f"────────────────────────────────────────────────────────────────────────────────\n")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(prog="bloom.server")
    parser.add_argument("--host", default="127.0.0.1", help="host to bind API server (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=4096, help="port to bind API server (default: 4096)")
    parser.add_argument("--agent", default="assistant", help="agent name or manifest path")
    args = parser.parse_args()
    asyncio.run(run_server(host=args.host, port=args.port, initial_agent=args.agent))
