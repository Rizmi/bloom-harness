"""Comprehensive unit tests for the neuralcode harness."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from neuralcode import compact, history, llm, manifest, tools
from neuralcode.mcp.filesystem import server as fs
from neuralcode.mcp.skills import server as sk
from neuralcode.mcp_client import _func_to_schema, _get_subprocess_env, mcp_client


class TestHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.agent_cfg = manifest.load_manifest("assistant")
        except FileNotFoundError:
            cls.agent_cfg = {
                "model": "deeprelay-flash",
                "system_prompt": "General-purpose workspace assistant.",
                "read_paths": ["workspace/"],
                "write_paths": ["workspace/"],
                "skill_paths": ["skills"],
                "permissions": {
                    "allow": [
                        "filesystem.file_read",
                        "filesystem.file_write",
                        "filesystem.file_edit",
                        "filesystem.file_multi_edit",
                        "filesystem.file_replace_lines",
                        "filesystem.file_grep",
                        "filesystem.file_list",
                        "filesystem.file_glob",
                        "skills.skill_list",
                        "skills.skill_load",
                    ],
                    "ask": [
                        "filesystem.file_delete",
                    ],
                },
            }

    def setUp(self):
        tools.setup(self.agent_cfg)
        fs.configure(read_paths=["."], write_paths=["."])
        sk.configure(skill_paths=["skills"])

    def test_schema_items_and_required(self):
        """Test R16 and P0: List[Dict] emits array with object items, required fields present."""
        schema = _func_to_schema(fs.file_multi_edit)
        props = schema["inputSchema"]["properties"]
        self.assertEqual(props["edits"]["type"], "array")
        self.assertEqual(props["edits"]["items"]["type"], "object")
        self.assertIn("file_path", schema["inputSchema"]["required"])
        self.assertIn("edits", schema["inputSchema"]["required"])

        read_schema = _func_to_schema(fs.file_read)
        self.assertEqual(read_schema["inputSchema"]["required"], ["file_path"])
        self.assertNotIn("path", read_schema["inputSchema"]["properties"])

    def test_tool_run_json_content_not_mangled(self):
        """Test R4: file_write with JSON string content is preserved as str, not parsed into dict."""
        with tempfile.TemporaryDirectory() as td:
            fs.configure(read_paths=[td], write_paths=[td])
            test_file = Path(td) / "test.json"
            json_text = '{"name": "test_app", "version": "1.0.0"}'

            res = asyncio.run(tools.tool_run("filesystem.file_write", {
                "file_path": str(test_file),
                "content": json_text,
            }))
            self.assertIn("Wrote", res)
            saved_content = test_file.read_text(encoding="utf-8")
            self.assertEqual(saved_content, json_text)

    def test_file_read_clamping_and_repeat_content(self):
        """Test R5 and P0: start_line=0 is clamped to 1, repeated reads always return content."""
        with tempfile.TemporaryDirectory() as td:
            f_path = Path(td) / "test.txt"
            f_path.write_text("line1\nline2\nline3\n", encoding="utf-8")
            fs.configure(read_paths=[td], write_paths=[td])
            r1 = fs.file_read(str(f_path), start_line=0, max_lines=2)
            self.assertIn("1\tline1", r1)
            self.assertIn("2\tline2", r1)

            # Repeat read must return content, never a stub
            r2 = fs.file_read(str(f_path), start_line=1, max_lines=2)
            self.assertIn("1\tline1", r2)
            self.assertNotIn("(File", r2)

    def test_local_registry_guards(self):
        """Test R6: dispatch and setup reject module attributes not in TOOLS (e.g. configure)."""
        # Calling an unregistered function via mcp_client must fail
        res = asyncio.run(mcp_client.call_tool("filesystem.configure", {"read_paths": ["/"]}))
        self.assertIn("error", res)
        self.assertIn("not found on local server", res["error"])

        # Setup must reject manifest tool names not in TOOLS
        with self.assertRaises(ValueError):
            tools.setup({"tools": ["filesystem.configure"]})

        with self.assertRaises(ValueError):
            tools.setup({"tools": ["nonexistent_server.tool"]})

    def test_reviewer_permissions_matrix(self):
        """Test security: reviewer manifest cannot write and cannot load skills."""
        try:
            rev_cfg = manifest.load_manifest("reviewer")
        except FileNotFoundError:
            rev_cfg = {
                "model": "deeprelay-flash",
                "system_prompt": "You are a code review assistant. You only read code and provide review feedback.",
                "read_paths": ["."],
                "write_paths": [],
                "skill_paths": [],
                "permissions": {
                    "allow": [
                        "filesystem.file_read",
                        "filesystem.file_grep",
                        "filesystem.file_list",
                        "filesystem.file_glob",
                    ],
                    "ask": [],
                },
            }
        tools.setup(rev_cfg)
        fs.configure(read_paths=rev_cfg.get("read_paths", ["."]), write_paths=rev_cfg.get("write_paths", []))
        sk.configure(skill_paths=rev_cfg.get("skill_paths", []))

        # Denied write
        with self.assertRaises(PermissionError):
            fs._check_write("forbidden.txt")

        # Denied skills (empty skill_paths)
        with self.assertRaises(PermissionError):
            sk._resolve_skills_dir()

        # In reviewer, file_delete is default-deny
        self.assertEqual(tools.check_permission("filesystem.file_delete"), "deny")
        self.assertEqual(tools.check_permission("filesystem.file_write"), "deny")
        self.assertEqual(tools.check_permission("filesystem.file_read"), "allow")

    def test_permissions_allow_ask_deny_tiers(self):
        """Test permissions: allow runs directly, ask prompts, unlisted is default-deny."""
        tools.setup(self.agent_cfg)
        self.assertEqual(tools.check_permission("filesystem.file_read"), "allow")
        self.assertEqual(tools.check_permission("filesystem.file_delete"), "ask")
        self.assertEqual(tools.check_permission("filesystem.unknown_tool"), "deny")

    def test_permissions_ask_prompt_approved_and_cancelled(self):
        """Test ask tier: user rejection cancels execution, user approval allows execution through real ui.approve."""
        from unittest.mock import patch
        tools.setup(self.agent_cfg)

        with tempfile.TemporaryDirectory() as td:
            fs.configure(read_paths=[td], write_paths=[td])
            target = Path(td) / "to_delete.txt"
            target.write_text("temporary", encoding="utf-8")

            # 1. User rejects via prompt.read_sync -> cancelled
            with patch("neuralcode.prompt.read_sync", return_value="n"):
                res = asyncio.run(tools.tool_run("filesystem.file_delete", {"file_path": str(target)}))
                self.assertIn("cancelled", res)
                self.assertTrue(target.exists())

            # 2. User approves via prompt.read_sync -> executed
            with patch("neuralcode.prompt.read_sync", return_value="y"):
                res = asyncio.run(tools.tool_run("filesystem.file_delete", {"file_path": str(target)}))
                self.assertIn("Deleted", res)
                self.assertFalse(target.exists())

            # 3. Denied tool -> rejected without prompt
            res_denied = asyncio.run(tools.tool_run("forbidden_server.tool", {}))
            self.assertIn("not permitted", res_denied)

    def test_ui_approve_and_pick_sync_reader(self):
        """Test gate level: ui.approve and ui.pick synchronously read input and fail-closed on EOF/SIGINT."""
        from unittest.mock import patch
        from neuralcode.ui import ui

        # approve: yes / y -> True
        with patch("neuralcode.prompt.read_sync", return_value="y"):
            self.assertTrue(ui.approve("delete file?"))
        with patch("neuralcode.prompt.read_sync", return_value="yes"):
            self.assertTrue(ui.approve("delete file?"))

        # approve: prefix non-match like yikes -> False
        with patch("neuralcode.prompt.read_sync", return_value="yikes"):
            self.assertFalse(ui.approve("delete file?"))

        # approve: no / n -> False
        with patch("neuralcode.prompt.read_sync", return_value="n"):
            self.assertFalse(ui.approve("delete file?"))

        # approve: EOF / Ctrl-C -> False (fail-closed)
        with patch("neuralcode.prompt.read_sync", side_effect=EOFError):
            self.assertFalse(ui.approve("delete file?"))
        with patch("neuralcode.prompt.read_sync", side_effect=KeyboardInterrupt):
            self.assertFalse(ui.approve("delete file?"))

        # pick: valid number -> index
        with patch("neuralcode.prompt.read_sync", return_value="1"):
            self.assertEqual(ui.pick("Options", ["zero", "one", "two"]), 1)

        # pick: EOF -> None
        with patch("neuralcode.prompt.read_sync", side_effect=EOFError):
            self.assertIsNone(ui.pick("Options", ["zero", "one", "two"]))

    def test_manifest_permission_shape_validation(self):
        """Test validation: reject overlapping tiers, mixed formats, and invalid types."""
        # Both permissions and tools
        with self.assertRaises(ValueError):
            tools.setup({"permissions": {"allow": []}, "tools": []})

        # Overlapping allow and ask
        with self.assertRaises(ValueError):
            tools.setup({"permissions": {"allow": ["filesystem.file_read"], "ask": ["filesystem.file_read"]}})

        # Non-dict permissions
        with self.assertRaises(ValueError):
            tools.setup({"permissions": ["filesystem.file_read"]})

        # String instead of list
        with self.assertRaises(ValueError):
            tools.setup({"permissions": {"allow": "filesystem.file_read"}})

        # Integer instead of list
        with self.assertRaises(ValueError):
            tools.setup({"permissions": {"allow": 42}})

        # List containing non-string element
        with self.assertRaises(ValueError):
            tools.setup({"permissions": {"allow": ["filesystem.file_read", 1]}})

        # Legacy tools containing non-string element
        with self.assertRaises(ValueError):
            tools.setup({"tools": [123]})

    def test_execute_malformed_arguments_handling(self):
        """Test R3: execute() handles None and malformed arguments without raising TypeError."""
        class DummyCall:
            class Function:
                name = "tool_run"
                arguments = None
            function = Function()
            id = "call_123"

        args, err = asyncio.run(tools.execute(DummyCall()))
        self.assertIn("Error", err)

    def test_subprocess_env_isolation(self):
        """Test security: subprocess env drops private keys and handles proxy vars."""
        env = _get_subprocess_env({"env": {"TEST_PORT": "9000"}})
        self.assertEqual(env.get("TEST_PORT"), "9000")
        self.assertNotIn("DEEPRELAY_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_compaction_single_system_message(self):
        """Test R8: compaction produces exactly one system message with summary merged."""
        messages = [
            {"role": "system", "content": "System prompt base"},
            {"role": "user", "content": "User request 1"},
            {"role": "assistant", "content": "Assistant response 1"},
            {"role": "user", "content": "User request 2"},
        ]
        # Mock summarize
        async def fake_summarize(msgs):
            return "Compact handoff summary"

        orig_summarize = compact.summarize
        orig_window = llm.CONTEXT_WINDOW
        compact.summarize = fake_summarize
        llm.CONTEXT_WINDOW = 20
        try:
            compacted = asyncio.run(compact.compact(messages))
            # Verify exactly 1 system message at index 0
            self.assertEqual(compacted[0]["role"], "system")
            self.assertIn("System prompt base", compacted[0]["content"])
            self.assertIn("Compact handoff summary", compacted[0]["content"])
            # Verify no consecutive same-role messages
            for i in range(1, len(compacted)):
                self.assertNotEqual(compacted[i]["role"], "system")
        finally:
            compact.summarize = orig_summarize
            llm.CONTEXT_WINDOW = orig_window

    def test_compaction_no_summary_accumulation(self):
        """Test P0-2: Compacting an already-compacted transcript strips old summary and keeps exactly one."""
        messages = [
            {"role": "system", "content": "System prompt base\n\n<summary>\nOld summary 1\n</summary>"},
            {"role": "user", "content": "User request 1"},
            {"role": "assistant", "content": "Assistant response 1"},
            {"role": "user", "content": "User request 2"},
        ]
        async def fake_summarize(msgs):
            return "Fresh summary 2"

        orig_summarize = compact.summarize
        orig_window = llm.CONTEXT_WINDOW
        compact.summarize = fake_summarize
        llm.CONTEXT_WINDOW = 20
        try:
            compacted = asyncio.run(compact.compact(messages))
            content = compacted[0]["content"]
            self.assertEqual(content.count("<summary>"), 1)
            self.assertEqual(content.count("</summary>"), 1)
            self.assertIn("Fresh summary 2", content)
            self.assertNotIn("Old summary 1", content)
        finally:
            compact.summarize = orig_summarize
            llm.CONTEXT_WINDOW = orig_window

    def test_resume_preserves_compacted_summary(self):
        """Test N1: refresh_system_prompt preserves <summary> block on resume, or leaves fresh prompt if none."""
        from neuralcode.agent import refresh_system_prompt
        # Case 1: summary present -> preserved
        messages_with_summary = [
            {"role": "system", "content": "Old prompt\n\n<summary>\nPersisted memory\n</summary>"},
            {"role": "user", "content": "User follow up"},
        ]
        refresh_system_prompt(messages_with_summary, "Fresh SYSTEM_PROMPT")
        self.assertIn("Fresh SYSTEM_PROMPT", messages_with_summary[0]["content"])
        self.assertIn("<summary>\nPersisted memory\n</summary>", messages_with_summary[0]["content"])

        # Case 2: negative case: no summary -> exactly fresh prompt
        messages_without_summary = [
            {"role": "system", "content": "Old prompt with no summary"},
            {"role": "user", "content": "User follow up"},
        ]
        refresh_system_prompt(messages_without_summary, "Fresh SYSTEM_PROMPT")
        self.assertEqual(messages_without_summary[0]["content"], "Fresh SYSTEM_PROMPT")

    def test_commands_compact_identity_contract(self):
        """Test N2: commands.compact preserves identity check contract when cut <= 1."""
        from neuralcode import commands
        messages = [{"role": "system", "content": "Base prompt"}]
        res = asyncio.run(commands.compact(messages))
        self.assertIs(res, messages)

    def test_reload_servers_per_entry_resilience(self):
        """Test N3: a broken server with missing env var does not disable valid servers."""
        from neuralcode.mcp_client import MCPClientManager
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f:
            cfg = {
                "mcpServers": {
                    "broken": {"transport": "http", "url": "${NONEXISTENT_VAR_12345}"},
                    "valid": {"transport": "http", "url": "http://127.0.0.1:8000/mcp"}
                }
            }
            json.dump(cfg, f)
            f_path = f.name
        try:
            mgr = MCPClientManager()
            mgr.servers_file = Path(f_path)
            mgr.reload_servers()
            self.assertNotIn("broken", mgr._external_servers)
            self.assertIn("valid", mgr._external_servers)
        finally:
            Path(f_path).unlink(missing_ok=True)

    def test_sse_transport_error_reported(self):
        """Test N4: sse transport returns deprecation error in schema and call."""
        mgr = mcp_client
        mgr._external_servers["sse_srv"] = {"transport": "sse", "url": "http://127.0.0.1:8000/sse"}
        schema_res = asyncio.run(mgr.get_tool_schema("sse_srv.some_tool"))
        self.assertIn("error", schema_res)
        self.assertIn("streamable_http", schema_res["error"])

        call_res = asyncio.run(mgr.call_tool("sse_srv.some_tool", {}))
        self.assertIn("error", call_res)
        self.assertIn("streamable_http", call_res["error"])

    def test_multi_edit_item_properties_described(self):
        """Test N8: file_multi_edit edits item schema describes old_string, new_string, replace_all."""
        schema = _func_to_schema(fs.file_multi_edit)
        edits_prop = schema["inputSchema"]["properties"]["edits"]
        item_props = edits_prop["items"]["properties"]
        self.assertIn("old_string", item_props)
        self.assertIn("new_string", item_props)
        self.assertIn("replace_all", item_props)

    def test_ui_ask_eof_returns_none(self):
        """Test R1: ui.ask returns None on EOFError and empty string on KeyboardInterrupt."""
        from unittest.mock import patch, AsyncMock
        from neuralcode.ui import ui
        with patch("neuralcode.prompt.read", new_callable=AsyncMock) as mock_read:
            mock_read.side_effect = EOFError()
            val = asyncio.run(ui.ask())
            self.assertIsNone(val)

            mock_read.side_effect = KeyboardInterrupt()
            val = asyncio.run(ui.ask())
            self.assertEqual(val, "")

            mock_read.side_effect = None
            mock_read.return_value = "hello"
            val = asyncio.run(ui.ask())
            self.assertEqual(val, "hello")


    def test_agent_loop_tool_call_integration(self):
        """Integration test: mock user input and LLM response with tool_run, verify tool executed and persisted."""
        import os
        from unittest.mock import patch
        from neuralcode.agent import main
        from neuralcode import session

        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"NEURALCODE_HOME": td}):
                agents = manifest.list_agents()
                agent_name = agents[0] if agents else "assistant"
                session.set_agent(agent_name)
                user_prompts = ["Read test file", None]

                async def mock_ask():
                    return user_prompts.pop(0)

                class FakeToolCall:
                    id = "call_test_1"
                    class Function:
                        name = "tool_describe"
                        arguments = '{"tool_name": "filesystem.file_read"}'
                    function = Function()

                class FakeMsg1:
                    content = "Checking tool"
                    tool_calls = [FakeToolCall()]
                    def model_dump(self, **kwargs):
                        return {
                            "role": "assistant",
                            "content": self.content,
                            "tool_calls": [{"id": "call_test_1", "type": "function", "function": {"name": "tool_describe", "arguments": '{"tool_name": "filesystem.file_read"}'}}]
                        }

                class FakeMsg2:
                    content = "Tool executed successfully."
                    tool_calls = None
                    def model_dump(self, **kwargs):
                        return {"role": "assistant", "content": self.content}

                call_count = 0
                async def fake_call_llm(messages, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        return FakeMsg1(), {"total_tokens": 100}
                    return FakeMsg2(), {"total_tokens": 50}

                with patch("sys.argv", ["neuralcode", "--agent", agent_name]), \
                     patch("neuralcode.ui.ui.ask", side_effect=mock_ask), \
                     patch("neuralcode.llm.call_llm", side_effect=fake_call_llm):
                    asyncio.run(main())

                sessions = session.all_sessions()
                self.assertTrue(len(sessions) > 0)
                recent = session.open_session(sessions[0]["id"])
                roles = [m["role"] for m in recent]
                self.assertIn("user", roles)
                self.assertIn("tool", roles)
                tool_msg = next(m for m in recent if m["role"] == "tool")
                self.assertEqual(tool_msg["tool_call_id"], "call_test_1")
                self.assertIn("filesystem.file_read", tool_msg["content"])

    def test_plug_and_play_import_is_inert_with_zero_agents(self):
        """Scenario 1: importing packages with zero manifests present raises no error and produces no side effects."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            with patch("neuralcode.manifest.AGENTS_DIR", Path(td) / "agents"):
                import neuralcode.agent
                import neuralcode.manifest
                import neuralcode.llm
                import neuralcode.tools
                import neuralcode.mcp_client
                with self.assertRaises(FileNotFoundError):
                    neuralcode.manifest.resolve("nonexistent")

    def test_agent_flag_required_when_omitted(self):
        """Scenario 2: running neuralcode without --agent exits 2 with clean error stating --agent is required."""
        from unittest.mock import patch
        from neuralcode.agent import main
        with patch("sys.argv", ["neuralcode"]), \
             self.assertRaises(SystemExit) as ctx:
            asyncio.run(main())
        self.assertEqual(ctx.exception.code, 2)

    def test_plug_and_play_direct_path_session_uses_stem(self):
        """Scenario 3: --agent ./path/to/custom.json resolves name to 'custom' and sets session directory using stem."""
        import os
        from unittest.mock import patch
        from neuralcode import session
        with tempfile.TemporaryDirectory() as td:
            custom_path = Path(td) / "custom.json"
            custom_path.write_text(json.dumps({
                "model": "m",
                "system_prompt": "Custom direct path",
                "permissions": {"allow": []}
            }), encoding="utf-8")

            name, cfg = manifest.resolve(str(custom_path))
            self.assertEqual(name, "custom")
            self.assertEqual(cfg["system_prompt"], "Custom direct path")

            with patch.dict(os.environ, {"NEURALCODE_HOME": td}):
                session.set_agent(name)
                self.assertEqual(session.AGENT_NAME, "custom")
                self.assertTrue(str(session.SESSION_DIR).endswith("/custom"))

    def test_plug_and_play_error_handling_no_traceback(self):
        """Scenario 4: malformed JSON, unknown --agent, and allow/ask overlap exit 2 via parser.error with no traceback."""
        from unittest.mock import patch
        from neuralcode.agent import main

        # Subcase 1: Unknown agent
        with patch("sys.argv", ["neuralcode", "--agent", "nonexistent_bot"]), \
             self.assertRaises(SystemExit) as ctx:
            asyncio.run(main())
        self.assertEqual(ctx.exception.code, 2)

        # Subcase 2: Malformed JSON
        with tempfile.TemporaryDirectory() as td:
            bad_json = Path(td) / "bad.json"
            bad_json.write_text("{ unquoted_key: 123", encoding="utf-8")
            with patch("sys.argv", ["neuralcode", "--agent", str(bad_json)]), \
                 self.assertRaises(SystemExit) as ctx:
                asyncio.run(main())
            self.assertEqual(ctx.exception.code, 2)

        # Subcase 3: Overlap between allow and ask tiers
        with tempfile.TemporaryDirectory() as td:
            overlap_json = Path(td) / "overlap.json"
            overlap_json.write_text(json.dumps({
                "model": "deeprelay-flash",
                "permissions": {
                    "allow": ["filesystem.file_read"],
                    "ask": ["filesystem.file_read"]
                }
            }), encoding="utf-8")
            with patch("sys.argv", ["neuralcode", "--agent", str(overlap_json)]), \
                 self.assertRaises(SystemExit) as ctx:
                asyncio.run(main())
            self.assertEqual(ctx.exception.code, 2)

        # Subcase 4: JSON manifest is valid JSON but not an object (e.g. array, string, int)
        with tempfile.TemporaryDirectory() as td:
            for invalid_payload in ("[]", '"not_an_object"', "42"):
                invalid_json = Path(td) / "invalid.json"
                invalid_json.write_text(invalid_payload, encoding="utf-8")
                with patch("sys.argv", ["neuralcode", "--agent", str(invalid_json)]), \
                     self.assertRaises(SystemExit) as ctx:
                    asyncio.run(main())
                self.assertEqual(ctx.exception.code, 2)

    def test_plug_and_play_env_vars_in_servers_json_loaded_properly(self):
        """Scenario 5: External server using ${TEST_ENV_VAR} from .env loads successfully after load_env()."""
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            servers_file = Path(td) / "servers.json"
            servers_file.write_text(json.dumps({
                "mcpServers": {
                    "test_env_server": {
                        "command": "python",
                        "args": ["-m", "test"],
                        "env": {"API_KEY": "${TEST_PLUGIN_SECRET}"}
                    }
                }
            }), encoding="utf-8")

            mgr = mcp_client.__class__(servers_file=servers_file)
            mgr.reload_servers()
            self.assertNotIn("test_env_server", mgr.external_servers)

            with patch.dict(os.environ, {"TEST_PLUGIN_SECRET": "secret_123"}):
                mgr.reload_servers()
                self.assertIn("test_env_server", mgr.external_servers)
                self.assertEqual(mgr.external_servers["test_env_server"]["env"]["API_KEY"], "secret_123")


if __name__ == "__main__":
    unittest.main()
