import os
import tempfile
import unittest
from pathlib import Path

from mcp.memory import server as memory_server


class TestMemoryMCP(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.memory_path = Path(self.tmp_dir.name) / "MEMORY.md"
        memory_server.configure(memory_path=str(self.memory_path))

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_memory_read_nonexistent(self):
        res = memory_server.memory_read()
        self.assertIn("does not exist yet", res)

    def test_memory_write_and_read(self):
        write_res = memory_server.memory_write("# Memory Title\nKey fact 1")
        self.assertIn("Memory file written", write_res)
        read_res = memory_server.memory_read()
        self.assertEqual(read_res, "# Memory Title\nKey fact 1")

    def test_memory_edit_success(self):
        memory_server.memory_write("Line 1\nTarget Line\nLine 3")
        edit_res = memory_server.memory_edit("Target Line", "Updated Line")
        self.assertIn("Memory file written", edit_res)
        self.assertEqual(memory_server.memory_read(), "Line 1\nUpdated Line\nLine 3")

    def test_memory_edit_not_found(self):
        memory_server.memory_write("Line 1\nLine 2")
        edit_res = memory_server.memory_edit("Missing", "Replacement")
        self.assertIn("Error: old_string not found", edit_res)

    def test_memory_edit_multiple_occurrences(self):
        memory_server.memory_write("Duplicate\nDuplicate\nLine 3")
        edit_res = memory_server.memory_edit("Duplicate", "New")
        self.assertIn("matches 2 occurrences", edit_res)

    def test_guard_symlink(self):
        real_file = Path(self.tmp_dir.name) / "real.md"
        real_file.write_text("Secret")
        symlink_file = Path(self.tmp_dir.name) / "symlink_mem.md"
        symlink_file.symlink_to(real_file)

        memory_server.configure(memory_path=str(symlink_file))
        read_res = memory_server.memory_read()
        self.assertIn("Access denied", read_res)
        self.assertIn("symlink", read_res)


if __name__ == "__main__":
    unittest.main()


import asyncio
from bloom import manifest, tools
from bloom.mcp_client import mcp_client


class TestMemoryBloomIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.mem_file = Path(self.tmp_dir.name) / "MEMORY.md"
        memory_server.configure(memory_path=str(self.mem_file))
        mcp_client.reload_servers()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_tool_discovery_and_execution(self):
        agent_cfg = manifest.load_manifest("assistant")
        tools.setup(agent_cfg)

        self.assertEqual(tools.check_permission("memory.memory_read"), "allow")
        self.assertEqual(tools.check_permission("memory.memory_write"), "allow")
        self.assertEqual(tools.check_permission("memory.memory_edit"), "allow")

        summary = mcp_client.get_tool_summary("memory.memory_read")
        self.assertIn("long-lived memory", summary)

        # Execute write via tools
        res_write = asyncio.run(tools.tool_run("memory.memory_write", {"content": "Integration Test Memory"}))
        self.assertIn("Memory file written", res_write)

        # Execute read via tools
        res_read = asyncio.run(tools.tool_run("memory.memory_read", {}))
        self.assertEqual(res_read, "Integration Test Memory")

        # Execute edit via tools
        res_edit = asyncio.run(tools.tool_run("memory.memory_edit", {"old_string": "Test", "new_string": "Verified"}))
        self.assertIn("Memory file written", res_edit)
        self.assertEqual(asyncio.run(tools.tool_run("memory.memory_read", {})), "Integration Verified Memory")
