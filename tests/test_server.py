"""Comprehensive tests for Bloom Headless HTTP & SSE API Server.

Tests:
- Discovery endpoints (/api/info, /api/agents, /api/models, /api/tools, /api/skills)
- Model switching (/api/models/switch) and api_key redaction
- Session lifecycle (create, list, get, delete, rewind-candidates, rewind, compact)
- Session ID validation & path traversal prevention
- Pinned agent enforcement (rejecting mismatched agent)
- Rejection of slash commands in prompt turns (400)
- Rejection of non-dict JSON bodies (400)
- Turn lock 409 Conflict on concurrent turn requests and model switches
- Turn abort via POST /api/session/{id}/abort (including abort during approval)
- Token authentication with BLOOM_API_TOKEN (401 Unauthorized)
- Direct runner unit tests (event order, tool rounds, max_rounds reason)
"""

import asyncio
import json
import os
import tempfile
from typing import Any
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from bloom import llm, manifest, runner, session, tools
from bloom.server import configure_agent, handle_request, run_server
from bloom.ui import ui


class TestServer(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        os.environ.setdefault("DEEPRELAY_API_KEY", "mock-key")
        os.environ.setdefault("OPENAI_API_KEY", "mock-key")
        ui.set_quiet(True)
        self.td = tempfile.TemporaryDirectory()
        self.old_bloom_home = os.environ.get("BLOOM_HOME")
        os.environ["BLOOM_HOME"] = self.td.name
        configure_agent("assistant")

        self.server = await asyncio.start_server(handle_request, "127.0.0.1", 0)
        self.host, self.port = self.server.sockets[0].getsockname()

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        if self.old_bloom_home:
            os.environ["BLOOM_HOME"] = self.old_bloom_home
        else:
            os.environ.pop("BLOOM_HOME", None)
        self.td.cleanup()

    async def _request(self, method: str, path: str, body: Any = None, headers: dict = None) -> tuple[int, dict, str]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        hdrs = {
            "Host": f"{self.host}:{self.port}",
            "Connection": "close",
        }
        if headers:
            hdrs.update(headers)
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        if data:
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(data))

        req_lines = [f"{method} {path} HTTP/1.1"]
        for k, v in hdrs.items():
            req_lines.append(f"{k}: {v}")
        req_lines.append("")
        req_lines.append("")

        writer.write("\r\n".join(req_lines).encode("utf-8") + data)
        await writer.drain()

        # Read response
        status_line = await reader.readline()
        status_code = int(status_line.split()[1])

        resp_headers = {}
        while True:
            line = await reader.readline()
            if not line or line == b"\r\n":
                break
            k, v = line.decode("utf-8").split(":", 1)
            resp_headers[k.strip().lower()] = v.strip()

        resp_body = await reader.read()
        writer.close()
        await writer.wait_closed()

        try:
            parsed = json.loads(resp_body.decode("utf-8"))
        except Exception:
            parsed = None

        return status_code, parsed, resp_body.decode("utf-8", errors="replace")

    async def test_api_info(self):
        status, data, _ = await self._request("GET", "/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ready")
        self.assertIn("model", data)
        self.assertEqual(data["version"], "0.2.0")
        self.assertIn("agents", data)
        self.assertIn("assistant", data["agents"])

    async def test_api_agents_list_and_get(self):
        status, agents, _ = await self._request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertIsInstance(agents, list)
        self.assertTrue(any(a["name"] == "assistant" for a in agents))

        # GET /api/agents/assistant
        status2, agent_detail, _ = await self._request("GET", "/api/agents/assistant")
        self.assertEqual(status2, 200)
        self.assertEqual(agent_detail["name"], "assistant")
        self.assertIn("permissions", agent_detail)

        # Path traversal rejection
        status_bad, _, _ = await self._request("GET", "/api/agents/../../etc/passwd")
        self.assertEqual(status_bad, 404)

    async def test_api_models_and_switch(self):
        status, data, _ = await self._request("GET", "/api/models")
        self.assertEqual(status, 200)
        self.assertIn("models", data)
        self.assertIn("deeprelay-flash", data["models"])
        # Ensure api_key secrets are redacted
        flash_spec = data["models"]["deeprelay-flash"]
        self.assertNotIn("api_key", flash_spec)

        # Switch model
        status_sw, data_sw, _ = await self._request("POST", "/api/models/switch", {"model": "deeprelay-flash"})
        self.assertEqual(status_sw, 200)
        self.assertEqual(data_sw["active_model"], "deeprelay-flash")

    async def test_api_tools_and_describe(self):
        status, tools_list, _ = await self._request("GET", "/api/tools")
        self.assertEqual(status, 200)
        self.assertTrue(any(t["name"] == "filesystem.file_read" for t in tools_list))

        status_desc, schema, _ = await self._request("GET", "/api/tools/filesystem.file_read/describe")
        self.assertEqual(status_desc, 200)
        self.assertEqual(schema["name"], "filesystem.file_read")
        self.assertIn("properties", schema["parameters"])

    async def test_api_session_lifecycle(self):
        # 1. Create session
        status, created, _ = await self._request("POST", "/api/session")
        self.assertEqual(status, 201)
        sid = created["id"]
        self.assertTrue(sid)

        # 2. List sessions
        status, sessions_list, _ = await self._request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertTrue(any(s["id"] == sid for s in sessions_list))

        # 3. Get session transcript
        status, transcript, _ = await self._request("GET", f"/api/session/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(transcript["id"], sid)
        self.assertEqual(transcript["agent"], "assistant")
        self.assertTrue(len(transcript["messages"]) >= 1)

        # 4. Rewind candidates
        status, cands, _ = await self._request("GET", f"/api/session/{sid}/rewind-candidates")
        self.assertEqual(status, 200)
        self.assertIsInstance(cands, list)

        # 5. Delete session
        status, del_res, _ = await self._request("DELETE", f"/api/session/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(del_res["status"], "deleted")

    async def test_api_session_id_validation_rejects_traversal(self):
        status, _, _ = await self._request("GET", "/api/session/..%2F..%2Fevil")
        self.assertEqual(status, 400)

        # Reject prompt on non-existent session
        status_prompt, p_data, _ = await self._request("POST", "/api/session/nonexistent_id/prompt", {"text": "hello"})
        self.assertEqual(status_prompt, 404)

    async def test_api_pinned_agent_rejects_mismatched_agent(self):
        status, data, _ = await self._request("GET", "/api/info?agent=other_agent")
        self.assertEqual(status, 400)
        self.assertIn("pinned to agent", data.get("error", ""))

    async def test_api_rejects_slash_command_in_prompt(self):
        status, created, _ = await self._request("POST", "/api/session")
        sid = created["id"]
        status_cmd, cmd_data, _ = await self._request("POST", f"/api/session/{sid}/prompt", {"text": "/compact"})
        self.assertEqual(status_cmd, 400)
        self.assertIn("not supported in prompt turns", cmd_data.get("error", ""))

    async def test_api_rejects_non_dict_json(self):
        status, data, _ = await self._request("POST", "/api/session", body=[1, 2, 3])
        self.assertEqual(status, 400)
        self.assertIn("Invalid JSON body", data.get("error", ""))

    async def test_api_prompt_streaming_sse(self):
        status, created, _ = await self._request("POST", "/api/session")
        sid = created["id"]

        mock_msg = MagicMock()
        mock_msg.content = "Hello! I am ready to automate."
        mock_msg.tool_calls = None
        mock_usage = {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cached_tokens": 80,
        }

        with patch("bloom.llm.call_llm", AsyncMock(return_value=(mock_msg, mock_usage))):
            reader, writer = await asyncio.open_connection(self.host, self.port)
            body = json.dumps({"text": "Hi assistant"}).encode("utf-8")
            req = (
                f"POST /api/session/{sid}/prompt HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("utf-8") + body

            writer.write(req)
            await writer.drain()

            events = []
            while True:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8").strip()
                if line_str.startswith("data: "):
                    events.append(json.loads(line_str[6:]))

            writer.close()
            await writer.wait_closed()

            event_types = [e.get("type") for e in events]
            self.assertIn("start", event_types)
            self.assertIn("thinking", event_types)
            self.assertIn("delta", event_types)
            self.assertIn("usage", event_types)
            self.assertIn("done", event_types)

            text_events = [e for e in events if e.get("type") == "delta"]
            self.assertEqual(text_events[0]["text"], "Hello! I am ready to automate.")

            done_event = [e for e in events if e.get("type") == "done"][0]
            self.assertEqual(done_event.get("reason"), "stop")

    async def test_api_human_approval_gate_over_sse(self):
        status, created, _ = await self._request("POST", "/api/session")
        sid = created["id"]

        mock_tc = {
            "id": "call_del_1",
            "type": "function",
            "function": {
                "name": "tool_run",
                "arguments": json.dumps({
                    "tool_name": "filesystem.file_delete",
                    "arguments": {"file_path": "workspace/write/probe.txt"},
                }),
            },
        }

        mock_msg_with_tool = MagicMock()
        mock_msg_with_tool.content = "Deleting file now."
        mock_msg_with_tool.tool_calls = [mock_tc]

        mock_msg_final = MagicMock()
        mock_msg_final.content = "File deletion handled."
        mock_msg_final.tool_calls = None

        mock_usage = {"prompt_tokens": 150, "completion_tokens": 20}

        calls = [
            (mock_msg_with_tool, mock_usage),
            (mock_msg_final, mock_usage),
        ]

        async def mock_call_llm(msgs):
            return calls.pop(0)

        with patch("bloom.llm.call_llm", side_effect=mock_call_llm), \
             patch("bloom.mcp_client.mcp_client.call_tool", AsyncMock(return_value="File deleted")):
            reader, writer = await asyncio.open_connection(self.host, self.port)
            body = json.dumps({"text": "delete probe.txt"}).encode("utf-8")
            req = (
                f"POST /api/session/{sid}/prompt HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("utf-8") + body

            writer.write(req)
            await writer.drain()

            permission_request_id = None
            events = []

            while True:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8").strip()
                if line_str.startswith("data: "):
                    ev = json.loads(line_str[6:])
                    events.append(ev)
                    if ev.get("type") == "permission":
                        permission_request_id = ev.get("requestId")
                        self.assertIn("action", ev)
                        self.assertIn("resources", ev)
                        break

            self.assertIsNotNone(permission_request_id)

            # Check GET /api/permissions
            p_status, pending_res, _ = await self._request("GET", "/api/permissions")
            self.assertEqual(p_status, 200)
            pending_items = pending_res.get("pending", pending_res) if isinstance(pending_res, dict) else pending_res
            target_p = next((p for p in pending_items if p["requestId"] == permission_request_id), None)
            self.assertIsNotNone(target_p)
            self.assertIn("action", target_p)
            self.assertIn("resources", target_p)

            # Approve via POST /api/session/{id}/approval with Accept: application/json
            app_status, app_res, _ = await self._request(
                "POST",
                f"/api/session/{sid}/approval",
                {"requestId": permission_request_id, "decision": "approved", "stream": False},
                headers={"Accept": "application/json"},
            )
            self.assertEqual(app_status, 200)
            self.assertTrue(app_res["approved"])

            while True:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8").strip()
                if line_str.startswith("data: "):
                    events.append(json.loads(line_str[6:]))

            writer.close()
            await writer.wait_closed()

            event_types = [e.get("type") for e in events]
            self.assertIn("permission_settled", event_types)
            self.assertIn("tool", event_types)
            tool_events = [e for e in events if e.get("type") == "tool"]
            self.assertTrue(any(t.get("state") == "running" for t in tool_events))
            self.assertTrue(any(t.get("state") == "completed" for t in tool_events))
            self.assertIn("done", event_types)

    async def test_api_turn_lock_409_conflict_and_abort(self):
        status, created, _ = await self._request("POST", "/api/session")
        sid = created["id"]

        slow_llm_event = asyncio.Event()

        async def slow_call_llm(msgs):
            await slow_llm_event.wait()
            mock_msg = MagicMock()
            mock_msg.content = "Finished after delay."
            mock_msg.tool_calls = None
            return mock_msg, {"total_tokens": 50}

        with patch("bloom.llm.call_llm", side_effect=slow_call_llm):
            reader, writer = await asyncio.open_connection(self.host, self.port)
            body = json.dumps({"text": "Slow turn"}).encode("utf-8")
            req = (
                f"POST /api/session/{sid}/prompt HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("utf-8") + body
            writer.write(req)
            await writer.drain()

            # Concurrent turn attempt immediately returns 409 Conflict without needing arbitrary sleep
            c_status, c_data, _ = await self._request("POST", f"/api/session/{sid}/prompt", {"text": "Concurrent"})
            self.assertEqual(c_status, 409)
            self.assertIn("turn is currently in progress", c_data.get("error", ""))

            # Model switch during active turn also returns 409 Conflict
            ms_status, ms_data, _ = await self._request("POST", "/api/models/switch", {"model": "deeprelay-flash"})
            self.assertEqual(ms_status, 409)

            # Abort the running turn via POST /api/session/{id}/abort
            ab_status, ab_data, _ = await self._request("POST", f"/api/session/{sid}/abort")
            self.assertEqual(ab_status, 200)
            self.assertEqual(ab_data["status"], "aborted")

            writer.close()
            await writer.wait_closed()

    async def test_api_abort_during_approval_stops_turn(self):
        status, created, _ = await self._request("POST", "/api/session")
        sid = created["id"]

        mock_tc = {
            "id": "call_del_abort",
            "type": "function",
            "function": {
                "name": "tool_run",
                "arguments": json.dumps({
                    "tool_name": "filesystem.file_delete",
                    "arguments": {"file_path": "workspace/write/probe.txt"},
                }),
            },
        }

        mock_msg_with_tool = MagicMock()
        mock_msg_with_tool.content = "Waiting for approval..."
        mock_msg_with_tool.tool_calls = [mock_tc]

        with patch("bloom.llm.call_llm", AsyncMock(return_value=(mock_msg_with_tool, {"total_tokens": 50}))), \
             patch("bloom.mcp_client.mcp_client.call_tool", AsyncMock(side_effect=RuntimeError("Must not execute!"))):
            reader, writer = await asyncio.open_connection(self.host, self.port)
            body = json.dumps({"text": "delete probe.txt"}).encode("utf-8")
            req = (
                f"POST /api/session/{sid}/prompt HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("utf-8") + body

            writer.write(req)
            await writer.drain()

            # Read until permission event
            while True:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8").strip()
                if line_str.startswith("data: "):
                    ev = json.loads(line_str[6:])
                    if ev.get("type") == "permission":
                        break

            # Now abort the session while waiting for approval
            ab_status, ab_data, _ = await self._request("POST", f"/api/session/{sid}/abort")
            self.assertEqual(ab_status, 200)

            events = []
            while True:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8").strip()
                if line_str.startswith("data: "):
                    events.append(json.loads(line_str[6:]))

            writer.close()
            await writer.wait_closed()

            done_events = [e for e in events if e.get("type") == "done"]
            self.assertTrue(len(done_events) >= 1)
            self.assertEqual(done_events[0].get("reason"), "aborted")

    async def test_api_token_auth_enforcement(self):
        with patch.dict(os.environ, {"BLOOM_API_TOKEN": "secret-123"}):
            status, _, _ = await self._request("GET", "/api/info")
            self.assertEqual(status, 401)

            status_auth, data_auth, _ = await self._request(
                "GET", "/api/info", headers={"Authorization": "Bearer secret-123"}
            )
            self.assertEqual(status_auth, 200)
            self.assertEqual(data_auth["status"], "ready")

            status_query, data_query, _ = await self._request("GET", "/api/info?token=secret-123")
            self.assertEqual(status_query, 200)

    async def test_api_payload_too_large_413(self):
        reader, writer = await asyncio.open_connection(self.host, self.port)
        oversized = 3 * 1024 * 1024
        req = (
            f"POST /api/session HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {oversized}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(req)
        await writer.drain()

        status_line = await reader.readline()
        status_code = int(status_line.split()[1])
        self.assertEqual(status_code, 413)
        writer.close()
        await writer.wait_closed()

    async def test_api_session_compact_and_rewind(self):
        status, created, _ = await self._request("POST", "/api/session")
        sid = created["id"]

        session.open_session(sid)
        sample_msgs = [
            {"role": "system", "content": "You are bloom."},
            {"role": "user", "content": "First query"},
            {"role": "assistant", "content": "First reply"},
            {"role": "user", "content": "Second query"},
            {"role": "assistant", "content": "Second reply"},
        ]
        session.save(sample_msgs)

        rc_status, cands, _ = await self._request("GET", f"/api/session/{sid}/rewind-candidates")
        self.assertEqual(rc_status, 200)
        self.assertTrue(len(cands) >= 2)

        # Invalid rewind index (< 1 or >= len(msgs)) returns 400
        rew_bad, _, _ = await self._request("POST", f"/api/session/{sid}/rewind", {"targetIndex": -1})
        self.assertEqual(rew_bad, 400)

        # Valid rewind to targetIndex 3
        rew_status, rew_data, _ = await self._request("POST", f"/api/session/{sid}/rewind", {"targetIndex": 3})
        self.assertEqual(rew_status, 200)
        self.assertEqual(rew_data["status"], "rewound")
        self.assertEqual(rew_data["remaining_messages"], 3)

        reloaded = session.load(sid)
        self.assertEqual(len(reloaded), 3)

        # Compact returns not_compacted if not enough messages
        comp_status, comp_data, _ = await self._request("POST", f"/api/session/{sid}/compact")
        self.assertEqual(comp_status, 200)
        self.assertIn("status", comp_data)

    async def test_api_skills_discovery_and_permission_guard(self):
        status, skills_list, _ = await self._request("GET", "/api/skills")
        self.assertEqual(status, 200)
        self.assertIsInstance(skills_list, list)

        status_sk, sk_data, _ = await self._request("GET", "/api/skills/nonexistent_skill")
        self.assertIn(status_sk, (403, 404))

    async def test_runner_run_turn_direct(self):
        messages = [{"role": "system", "content": "System prompt"}]
        mock_msg = MagicMock()
        mock_msg.content = "Runner direct response."
        mock_msg.tool_calls = None
        mock_usage = {"prompt_tokens": 40, "completion_tokens": 10}

        events = []
        async def event_collector(ev):
            events.append(ev)

        with patch("bloom.llm.call_llm", AsyncMock(return_value=(mock_msg, mock_usage))):
            updated_msgs, final_usage = await runner.run_turn(
                messages,
                "Hello runner",
                on_event=event_collector,
            )

        self.assertEqual(len(updated_msgs), 3)
        self.assertEqual(updated_msgs[-1]["content"], "Runner direct response.")
        event_types = [e.get("type") for e in events]
        self.assertIn("thinking", event_types)
        self.assertIn("delta", event_types)
        self.assertIn("usage", event_types)
        self.assertIn("done", event_types)
        done_ev = next(e for e in events if e.get("type") == "done")
        self.assertEqual(done_ev.get("reason"), "stop")

    async def test_runner_run_turn_max_rounds(self):
        messages = [{"role": "system", "content": "System prompt"}]
        mock_tc = {
            "id": "loop_call",
            "type": "function",
            "function": {"name": "filesystem.file_read", "arguments": "{}"},
        }
        loop_msg = MagicMock()
        loop_msg.content = "Looping..."
        loop_msg.tool_calls = [mock_tc]

        events = []
        async def event_collector(ev):
            events.append(ev)

        with patch("bloom.llm.call_llm", AsyncMock(return_value=(loop_msg, {"total_tokens": 10}))), \
             patch("bloom.tools.execute", AsyncMock(return_value=({}, "tool output"))):
            updated_msgs, _ = await runner.run_turn(
                messages,
                "Start looping",
                max_tool_rounds=2,
                on_event=event_collector,
            )

        event_types = [e.get("type") for e in events]
        self.assertIn("error", event_types)
        done_ev = next(e for e in events if e.get("type") == "done")
        self.assertEqual(done_ev.get("reason"), "max_rounds")

    async def test_api_model_singular_catalog_and_switch(self):
        status, data, _ = await self._request("GET", "/api/model")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ready")
        self.assertIn("current", data)
        self.assertIn("groups", data)
        self.assertIsInstance(data["groups"], list)

        # POST /api/model switch
        sw_status, sw_data, _ = await self._request("POST", "/api/model", {"model": "deeprelay-flash"})
        self.assertEqual(sw_status, 200)
        self.assertEqual(sw_data["model"], "deeprelay-flash")
        self.assertEqual(sw_data["current"], "deeprelay-flash")

    async def test_api_session_active_and_usage(self):
        status, sess, _ = await self._request("POST", "/api/session")
        sid = sess["id"]

        act_status, act_data, _ = await self._request("GET", "/api/session/active")
        self.assertEqual(act_status, 200)
        self.assertIn("data", act_data)

        use_status, use_data, _ = await self._request("GET", f"/api/session/{sid}/usage")
        self.assertEqual(use_status, 200)
        self.assertIn("promptTokens", use_data)
        self.assertIn("contextLimit", use_data)

    async def test_api_session_message_history(self):
        status, sess, _ = await self._request("POST", "/api/session")
        sid = sess["id"]

        # Populate sample messages
        sample = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Run tool"},
            {
                "role": "assistant",
                "content": "Executing...",
                "tool_calls": [
                    {
                        "id": "call_hist_1",
                        "type": "function",
                        "function": {
                            "name": "tool_run",
                            "arguments": json.dumps({
                                "tool_name": "filesystem.file_read",
                                "arguments": {"file_path": "a.txt"},
                            }),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_hist_1", "content": "file contents here"},
        ]
        session.save(sample)

        msg_status, msg_data, _ = await self._request("GET", f"/api/session/{sid}/message")
        self.assertEqual(msg_status, 200)
        self.assertIn("data", msg_data)
        entries = msg_data["data"]
        self.assertEqual(len(entries), 2)  # 1 user, 1 assistant
        self.assertEqual(entries[0]["role"], "user")
        self.assertEqual(entries[1]["role"], "assistant")

        # Verify assistant parts structure
        parts = entries[1]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[0]["text"], "Executing...")
        self.assertEqual(parts[1]["type"], "tool")
        self.assertEqual(parts[1]["tool"], "filesystem.file_read")
        self.assertEqual(parts[1]["callId"], "call_hist_1")
        self.assertEqual(parts[1]["output"], "file contents here")

    async def test_api_mcp_readiness(self):
        status, mcp_data, _ = await self._request("GET", "/api/mcp")
        self.assertEqual(status, 200)
        self.assertEqual(mcp_data["status"], "ready")
        self.assertIn("servers", mcp_data)
        self.assertIsInstance(mcp_data["servers"], list)
        self.assertTrue(any(s["name"] == "filesystem" for s in mcp_data["servers"]))

    async def test_api_prompt_with_file_attachments(self):
        status, sess, _ = await self._request("POST", "/api/session")
        sid = sess["id"]

        mock_msg = MagicMock()
        mock_msg.content = "Received files."
        mock_msg.tool_calls = None

        with patch("bloom.llm.call_llm", AsyncMock(return_value=(mock_msg, {"total_tokens": 30}))):
            reader, writer = await asyncio.open_connection(self.host, self.port)
            body = json.dumps({
                "text": "Please check this",
                "files": [{"name": "spec.pdf", "uri": "/tmp/spec.pdf"}],
            }).encode("utf-8")
            req = (
                f"POST /api/session/{sid}/prompt HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("utf-8") + body
            writer.write(req)
            await writer.drain()

            while True:
                line = await reader.readline()
                if not line:
                    break
            writer.close()
            await writer.wait_closed()

        # Check saved session messages
        saved = session.load(sid)
        user_msg = next(m for m in saved if m["role"] == "user")
        self.assertIn("Please check this", user_msg["content"])
        self.assertIn("[Attachment: spec.pdf (/tmp/spec.pdf)]", user_msg["content"])

    async def test_custom_session_id_lifecycle(self):
        custom_id = "ses_custom_test_123"
        status, body, _ = await self._request("POST", "/api/session", body={"id": custom_id})
        self.assertEqual(status, 201)
        self.assertEqual(body["id"], custom_id)
        self.assertEqual(session.CURRENT, custom_id)

        # Verify saved transcript exists and loads
        loaded = session.load(custom_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["role"], "system")

        # Invalid ID should return 400
        bad_status, bad_body, _ = await self._request("POST", "/api/session", body={"id": "bad/id/with/slash"})
        self.assertEqual(bad_status, 400)
        self.assertIn("error", bad_body)

    async def test_api_info_and_model_return_catalog_model_key(self):
        status, info, _ = await self._request("GET", "/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(info["model"], "deeprelay-flash")

        status, model_info, _ = await self._request("GET", "/api/model")
        self.assertEqual(status, 200)
        self.assertEqual(model_info["current"], "deeprelay-flash")
        self.assertIn("groups", model_info)

    async def test_post_session_idempotent_no_duplicate_system_prompt(self):
        test_sid = "ses_idempotent_probe"
        # First POST creates the session (201)
        st1, b1, _ = await self._request("POST", "/api/session", body={"id": test_sid})
        self.assertEqual(st1, 201)
        self.assertEqual(b1["id"], test_sid)

        p = session.path_for(test_sid)
        lines1 = p.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines1), 1)

        # Second POST to the same session returns 200 and DOES NOT duplicate the system prompt
        st2, b2, _ = await self._request("POST", "/api/session", body={"id": test_sid})
        self.assertEqual(st2, 200)
        self.assertEqual(b2["id"], test_sid)

        lines2 = p.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines2), 1, f"Expected 1 system line, found {len(lines2)}: {lines2}")

    async def test_prompt_model_key_comparison_and_error(self):
        st, created, _ = await self._request("POST", "/api/session")
        sid = created["id"]

        # 1. Invalid model returns 400
        st_bad, body_bad, _ = await self._request(
            "POST",
            f"/api/session/{sid}/prompt",
            body={"text": "hello", "model": "nonexistent-model-xyz"}
        )
        self.assertEqual(st_bad, 400)
        self.assertIn("Failed to switch model", body_bad.get("error", ""))

        # 2. Matching model_key does not invoke switch_model
        with patch("bloom.llm.switch_model") as mock_switch, \
             patch("bloom.llm.call_llm") as mock_call:
            mock_msg = MagicMock()
            mock_msg.content = "pong"
            mock_msg.tool_calls = None
            mock_call.return_value = (mock_msg, {"total_tokens": 10})

            # Send current model key
            st_ok, _, _ = await self._request(
                "POST",
                f"/api/session/{sid}/prompt",
                body={"text": "ping", "model": "deeprelay-flash"},
                headers={"Accept": "text/event-stream"}
            )
            mock_switch.assert_not_called()

    async def test_session_message_unpaginated_returns_all(self):
        """Test B5: GET /api/session/{id}/message returns all entries unless limit is explicitly requested."""
        st, created, _ = await self._request("POST", "/api/session")
        sid = created["id"]

        # Populate with 150 dummy messages
        loaded = session.load(sid) or []
        for i in range(150):
            loaded.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"msg_{i}"})
        session.save(loaded, session_id=sid)

        # Unpaginated GET returns all 150 entries
        st_get, data_all, _ = await self._request("GET", f"/api/session/{sid}/message")
        self.assertEqual(st_get, 200)
        self.assertEqual(len(data_all["data"]), 150)
        self.assertEqual(data_all["total"], 150)

        # Explicit limit returns sliced entries
        st_get2, data_sliced, _ = await self._request("GET", f"/api/session/{sid}/message?limit=50&offset=0")
        self.assertEqual(st_get2, 200)
        self.assertEqual(len(data_sliced["data"]), 50)
        self.assertEqual(data_sliced["total"], 150)
        self.assertEqual(data_sliced["limit"], 50)


if __name__ == "__main__":
    unittest.main()
