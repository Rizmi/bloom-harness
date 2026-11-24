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
        cls.agent_cfg = manifest.load_manifest("coder")

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
        rev_cfg = manifest.load_manifest("reviewer")
        tools.setup(rev_cfg)
        fs.configure(read_paths=rev_cfg.get("read_paths", ["."]), write_paths=rev_cfg.get("write_paths", []))
        sk.configure(skill_paths=rev_cfg.get("skill_paths", []))

        # Denied write
        with self.assertRaises(PermissionError):
            fs._check_write("forbidden.txt")

        # Denied skills (empty skill_paths)
        with self.assertRaises(PermissionError):
            sk._resolve_skills_dir()

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
                session.set_agent("coder")
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

                with patch("sys.argv", ["neuralcode", "--agent", "coder"]), \
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


if __name__ == "__main__":
    unittest.main()
