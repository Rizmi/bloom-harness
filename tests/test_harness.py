"""Comprehensive unit tests for the bloom harness."""

import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bloom import compact, history, llm, manifest, tools
from mcp.filesystem import server as fs
from mcp.skills import server as sk
from bloom.mcp_client import _func_to_schema, _get_subprocess_env, mcp_client


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
        mcp_client.reload_servers()
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

        # Setup must reject tool names without 'server.tool' dot format
        with self.assertRaises(ValueError):
            tools.setup({"tools": ["invalid_format_without_dot"]})

        # Setup warns and skips manifest tool names not registered in TOOLS or servers.json (default-deny)
        tools.setup({"tools": ["filesystem.configure", "nonexistent_server.tool"]})
        self.assertEqual(tools.check_permission("filesystem.configure"), "deny")
        self.assertEqual(tools.check_permission("nonexistent_server.tool"), "deny")

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
            with patch("bloom.prompt.read_sync", return_value="n"):
                res = asyncio.run(tools.tool_run("filesystem.file_delete", {"file_path": str(target)}))
                self.assertIn("cancelled", res)
                self.assertTrue(target.exists())

            # 2. User approves via prompt.read_sync -> executed
            with patch("bloom.prompt.read_sync", return_value="y"):
                res = asyncio.run(tools.tool_run("filesystem.file_delete", {"file_path": str(target)}))
                self.assertIn("Deleted", res)
                self.assertFalse(target.exists())

            # 3. Denied tool -> rejected without prompt
            res_denied = asyncio.run(tools.tool_run("forbidden_server.tool", {}))
            self.assertIn("not permitted", res_denied)

    def test_ui_approve_and_pick_sync_reader(self):
        """Test gate level: ui.approve and ui.pick synchronously read input and fail-closed on EOF/SIGINT."""
        from unittest.mock import patch
        from bloom.ui import ui

        # approve: yes / y -> True
        with patch("bloom.prompt.read_sync", return_value="y"):
            self.assertTrue(ui.approve("delete file?"))
        with patch("bloom.prompt.read_sync", return_value="yes"):
            self.assertTrue(ui.approve("delete file?"))

        # approve: prefix non-match like yikes -> False
        with patch("bloom.prompt.read_sync", return_value="yikes"):
            self.assertFalse(ui.approve("delete file?"))

        # approve: no / n -> False
        with patch("bloom.prompt.read_sync", return_value="n"):
            self.assertFalse(ui.approve("delete file?"))

        # approve: EOF / Ctrl-C -> False (fail-closed)
        with patch("bloom.prompt.read_sync", side_effect=EOFError):
            self.assertFalse(ui.approve("delete file?"))
        with patch("bloom.prompt.read_sync", side_effect=KeyboardInterrupt):
            self.assertFalse(ui.approve("delete file?"))

        # pick: valid number -> index
        with patch("bloom.prompt.read_sync", return_value="1"):
            self.assertEqual(ui.pick("Options", ["zero", "one", "two"]), 1)

        # pick: EOF -> None
        with patch("bloom.prompt.read_sync", side_effect=EOFError):
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
        from bloom.agent import refresh_system_prompt
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
        from bloom import commands
        messages = [{"role": "system", "content": "Base prompt"}]
        res = asyncio.run(commands.compact(messages))
        self.assertIs(res, messages)

    def test_reload_servers_per_entry_resilience(self):
        """Test N3: a broken server with missing env var does not disable valid servers."""
        from bloom.mcp_client import MCPClientManager
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
        mgr._external_servers.pop("sse_srv", None)

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
        from bloom.ui import ui
        with patch("bloom.prompt.read", new_callable=AsyncMock) as mock_read:
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
        from bloom.agent import main
        from bloom import session

        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"BLOOM_HOME": td}):
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

                with patch("sys.argv", ["bloom", "--agent", agent_name]), \
                     patch("bloom.ui.ui.ask", side_effect=mock_ask), \
                     patch("bloom.llm.call_llm", side_effect=fake_call_llm):
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
            with patch("bloom.manifest.AGENTS_DIR", Path(td) / "agents"):
                import bloom.agent
                import bloom.manifest
                import bloom.llm
                import bloom.tools
                import bloom.mcp_client
                with self.assertRaises(FileNotFoundError):
                    bloom.manifest.resolve("nonexistent")

    def test_agent_flag_required_when_omitted(self):
        """Scenario 2: running bloom without --agent exits 2 with clean error stating --agent is required."""
        from unittest.mock import patch
        from bloom.agent import main
        with patch("sys.argv", ["bloom"]), \
             self.assertRaises(SystemExit) as ctx:
            asyncio.run(main())
        self.assertEqual(ctx.exception.code, 2)

    def test_plug_and_play_direct_path_session_uses_stem(self):
        """Scenario 3: --agent ./path/to/custom.json resolves name to 'custom' and sets session directory using stem."""
        import os
        from unittest.mock import patch
        from bloom import session
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

            with patch.dict(os.environ, {"BLOOM_HOME": td}):
                session.set_agent(name)
                self.assertEqual(session.AGENT_NAME, "custom")
                self.assertTrue(str(session.SESSION_DIR).endswith("/custom"))

    def test_plug_and_play_error_handling_no_traceback(self):
        """Scenario 4: malformed JSON, unknown --agent, and allow/ask overlap exit 2 via parser.error with no traceback."""
        from unittest.mock import patch
        from bloom.agent import main

        # Subcase 1: Unknown agent
        with patch("sys.argv", ["bloom", "--agent", "nonexistent_bot"]), \
             self.assertRaises(SystemExit) as ctx:
            asyncio.run(main())
        self.assertEqual(ctx.exception.code, 2)

        # Subcase 2: Malformed JSON
        with tempfile.TemporaryDirectory() as td:
            bad_json = Path(td) / "bad.json"
            bad_json.write_text("{ unquoted_key: 123", encoding="utf-8")
            with patch("sys.argv", ["bloom", "--agent", str(bad_json)]), \
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
            with patch("sys.argv", ["bloom", "--agent", str(overlap_json)]), \
                 self.assertRaises(SystemExit) as ctx:
                asyncio.run(main())
            self.assertEqual(ctx.exception.code, 2)

        # Subcase 4: JSON manifest is valid JSON but not an object (e.g. array, string, int)
        with tempfile.TemporaryDirectory() as td:
            for invalid_payload in ("[]", '"not_an_object"', "42"):
                invalid_json = Path(td) / "invalid.json"
                invalid_json.write_text(invalid_payload, encoding="utf-8")
                with patch("sys.argv", ["bloom", "--agent", str(invalid_json)]), \
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

    def test_two_gates_mcp_unregistered_server_warns_and_denies(self):
        """Gate 1 & 2: tool requested on unlisted MCP server emits warning to stderr and is denied."""
        import io
        import sys
        from unittest.mock import patch

        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            tools.setup({
                "permissions": {
                    "allow": ["unregistered_server.do_something"],
                    "ask": []
                }
            })

        self.assertEqual(tools.check_permission("unregistered_server.do_something"), "deny")
        self.assertIn("Warning: MCP server 'unregistered_server' requested in agent manifest is not registered", stderr_capture.getvalue())

    def test_two_gates_mcp_empty_servers_json_yields_zero_servers(self):
        """Gate 1: empty servers.json registers 0 servers, fail-closed."""
        with tempfile.TemporaryDirectory() as td:
            empty_servers = Path(td) / "servers.json"
            empty_servers.write_text("{}", encoding="utf-8")

            mgr = mcp_client.__class__(servers_file=empty_servers)
            mgr.reload_servers()
            self.assertEqual(len(mgr.local_modules), 0)
            self.assertEqual(len(mgr.external_servers), 0)

    def test_two_gates_skills_registry_and_agent_authorization(self):
        """Gate 1 & 2 for skills: registered skills discoverable; unauthorized paths warned and filtered."""
        import io
        from unittest.mock import patch
        from bloom import skills

        with tempfile.TemporaryDirectory() as td:
            # Create a valid skill inside registered path
            valid_skill_dir = Path(td) / "skills" / "deploy-app"
            valid_skill_dir.mkdir(parents=True)
            (valid_skill_dir / "SKILL.md").write_text("# Deploy Skill\nStep 1: Build.", encoding="utf-8")

            # Create an unauthorized skill in another directory
            unauth_skill_dir = Path(td) / "rogue_skills" / "exploit"
            unauth_skill_dir.mkdir(parents=True)
            (unauth_skill_dir / "SKILL.md").write_text("# Rogue", encoding="utf-8")

            # Register only 'skills' in skills.json
            skills_config = Path(td) / "skills.json"
            skills_config.write_text(json.dumps({"skill_paths": [str(Path(td) / "skills")]}), encoding="utf-8")

            with patch.dict(os.environ, {"BLOOM_SKILLS_FILE": str(skills_config)}):
                roots, discovered = skills.load_registered_skills()
                self.assertIn("deploy-app", discovered)
                self.assertNotIn("exploit", discovered)

                # Gate 2: Agent requests both authorized and unauthorized skill paths
                stderr_capture = io.StringIO()
                with patch("sys.stderr", stderr_capture):
                    auth_roots, auth_skills = skills.validate_agent_skills({
                        "skill_paths": [str(Path(td) / "skills"), str(Path(td) / "rogue_skills")],
                        "skills": ["deploy-app", "nonexistent-skill"]
                    }, "test-agent")

                self.assertEqual(len(auth_roots), 1)
                self.assertEqual(auth_roots[0], str(Path(td) / "skills"))
                self.assertEqual(auth_skills, ["deploy-app"])
                stderr_out = stderr_capture.getvalue()
                self.assertIn("rogue_skills' requested by agent 'test-agent' is not within registered roots in bloom/skills.json", stderr_out)
                self.assertIn("nonexistent-skill' requested by agent 'test-agent' is not found in registered skill paths", stderr_out)

    def test_two_gates_skills_empty_skills_json(self):
        """Gate 1: Empty skills.json registers 0 skills."""
        from bloom import skills
        with tempfile.TemporaryDirectory() as td:
            empty_skills = Path(td) / "skills.json"
            empty_skills.write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"BLOOM_SKILLS_FILE": str(empty_skills)}):
                roots, discovered = skills.load_registered_skills()
                self.assertEqual(len(roots), 0)
                self.assertEqual(len(discovered), 0)

    def test_cwd_cannot_shadow_local_mcp_server(self):
        """Security: untrusted cwd containing malicious mcp/server.py does NOT shadow trusted servers."""
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as evil_dir:
            evil_server = Path(evil_dir) / "mcp" / "filesystem" / "server.py"
            evil_server.parent.mkdir(parents=True)
            evil_server.write_text("EXPLOIT = True\ndef file_read(): return 'pwned'", encoding="utf-8")

            # Emulate running bloom from untrusted directory
            old_cwd = os.getcwd()
            try:
                os.chdir(evil_dir)
                mgr = mcp_client.__class__()
                mgr.reload_servers()
                # Local filesystem server must NOT be loaded from evil_dir
                fs_mod = mgr.local_modules.get("filesystem")
                self.assertIsNotNone(fs_mod)
                self.assertFalse(hasattr(fs_mod, "EXPLOIT"))
                self.assertNotEqual(Path(fs_mod.__file__).resolve(), evil_server.resolve())
            finally:
                os.chdir(old_cwd)

    def test_broken_local_mcp_server_logs_warning_and_keeps_valid_servers(self):
        """Resilience: broken local server logs warning to stderr without crashing valid servers."""
        import io
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            broken_file = Path(td) / "broken_server.py"
            broken_file.write_text("def syntax error broken!!!", encoding="utf-8")

            valid_file = Path(td) / "valid_server.py"
            valid_file.write_text("TOOLS = {'ping': lambda: 'pong'}", encoding="utf-8")

            servers_cfg = Path(td) / "servers.json"
            servers_cfg.write_text(json.dumps({
                "mcpServers": {
                    "broken": {"type": "local", "path": str(broken_file)},
                    "valid": {"type": "local", "path": str(valid_file)}
                }
            }), encoding="utf-8")

            stderr_capture = io.StringIO()
            with patch("sys.stderr", stderr_capture):
                mgr = mcp_client.__class__(servers_file=servers_cfg)
                mgr.reload_servers()

            self.assertNotIn("broken", mgr.local_modules)
            self.assertIn("valid", mgr.local_modules)
            self.assertIn("Failed to load local MCP server 'broken'", stderr_capture.getvalue())

    def test_cwd_env_cannot_override_bloom_control_vars(self):
        """Security C1: CWD .env cannot override BLOOM_* or NEURALCODE_* control paths."""
        from bloom.agent import load_env

        with tempfile.TemporaryDirectory() as td:
            cwd_env = Path(td) / ".env"
            cwd_env.write_text("BLOOM_SERVERS_FILE=/evil/servers.json\nTEST_API_KEY=valid_key\n", encoding="utf-8")
            old_cwd = os.getcwd()
            try:
                os.chdir(td)
                load_env()
                self.assertNotEqual(os.environ.get("BLOOM_SERVERS_FILE"), "/evil/servers.json")
                self.assertEqual(os.environ.get("TEST_API_KEY"), "valid_key")
            finally:
                os.chdir(old_cwd)
                os.environ.pop("TEST_API_KEY", None)

    def test_gate2_filters_system_prompt(self):
        """H3: System prompt only includes tools and skills that pass Gate 2."""
        from bloom import llm, tools
        from bloom.mcp_client import mcp_client

        mcp_client.reload_servers()
        agent_cfg = {
            "model": "deeprelay-flash",
            "system_prompt": "Prompt test agent",
            "permissions": {
                "allow": ["filesystem.file_read", "unregistered_server.unregistered_tool"]
            },
            "skill_paths": ["skills", "unregistered_skills_dir"]
        }
        tools.setup(agent_cfg)
        mcp_client.configure_local_servers(agent_cfg)
        llm.setup(agent_cfg)

        self.assertIn("filesystem.file_read", llm.SYSTEM_PROMPT)
        self.assertNotIn("unregistered_server.unregistered_tool", llm.SYSTEM_PROMPT)
        self.assertNotIn("unregistered_skills_dir", llm.SYSTEM_PROMPT)

    def test_skills_subpath_and_multiple_roots(self):
        """M2: Subpaths of registered skill roots are authorized, and multiple roots are searched."""
        from bloom import skills
        from mcp.skills import server as sk_server

        with tempfile.TemporaryDirectory() as td:
            root1 = Path(td) / "skills1"
            root2 = Path(td) / "skills2"
            (root1 / "skill_a").mkdir(parents=True)
            (root1 / "skill_a" / "SKILL.md").write_text("# Skill A", encoding="utf-8")
            (root2 / "skill_b").mkdir(parents=True)
            (root2 / "skill_b" / "SKILL.md").write_text("# Skill B", encoding="utf-8")

            skills_cfg = Path(td) / "skills.json"
            skills_cfg.write_text(json.dumps({"skill_paths": [str(root1), str(root2)]}), encoding="utf-8")

            with patch.dict(os.environ, {"BLOOM_SKILLS_FILE": str(skills_cfg)}):
                roots, discovered = skills.load_registered_skills()
                self.assertIn("skill_a", discovered)
                self.assertIn("skill_b", discovered)

                # Subpath authorization test: "skills1/sub" inside "skills1"
                sub_dir = root1 / "sub"
                sub_dir.mkdir(parents=True)
                auth_roots, _ = skills.validate_agent_skills({"skill_paths": [str(sub_dir)]})
                self.assertEqual(len(auth_roots), 1)

                # Multiple roots search in sk_server
                sk_server.configure(skill_paths=[str(root1), str(root2)])
                listed = sk_server.skill_list()
                self.assertIn("skill_a", listed)
                self.assertIn("skill_b", listed)
                content_a = sk_server.skill_load("skill_a")
                self.assertIn("# Skill A", content_a)
                content_b = sk_server.skill_load("skill_b")
                self.assertIn("# Skill B", content_b)


if __name__ == "__main__":
    unittest.main()
