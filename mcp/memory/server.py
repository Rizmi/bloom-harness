"""Memory Operations MCP Server.

Provides persistent agent memory storage (MEMORY.md).
"""

import os
import tempfile
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
    _fastmcp = FastMCP("memory")
except ImportError:
    _fastmcp = None


class Server:
    def tool(self, *args, meta=None, **kwargs):
        def decorator(fn):
            fn._mcp_meta = meta or {}
            if _fastmcp is not None:
                _fastmcp.tool(*args, **kwargs)(fn)
            return fn
        return decorator

    def run(self, *args, **kwargs):
        if _fastmcp is not None:
            _fastmcp.run(*args, **kwargs)


mcp = Server()

MEMORY_FILE = Path(os.environ.get("MEMORY_FILE", "MEMORY.md")).resolve()


@mcp.tool(meta={"summary": "Read agent's long-lived memory (MEMORY.md)"})
def memory_read() -> str:
    """Read the agent's persistent memory file (MEMORY.md)."""
    if MEMORY_FILE.is_symlink():
        return f"Access denied: memory file '{MEMORY_FILE}' is a symlink."
    if not MEMORY_FILE.exists():
        return f"Memory file does not exist yet: {MEMORY_FILE}"
    try:
        return MEMORY_FILE.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error reading memory file: {exc}"


@mcp.tool(meta={"summary": "Overwrite long-lived memory (MEMORY.md)"})
def memory_write(content: str) -> str:
    """Overwrite the agent's memory file with new content atomically."""
    if MEMORY_FILE.is_symlink():
        return f"Access denied: memory file '{MEMORY_FILE}' is a symlink."
    try:
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=str(MEMORY_FILE.parent), prefix=".memory.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_name, MEMORY_FILE)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return f"Memory file written ({len(content)} bytes)."
    except Exception as exc:
        return f"Error writing memory file: {exc}"


@mcp.tool(meta={"summary": "Replace exact text snippet in long-lived memory (MEMORY.md)"})
def memory_edit(old_string: str, new_string: str) -> str:
    """Replace an exact unique snippet in the agent's memory file."""
    if not MEMORY_FILE.exists():
        return f"Memory file does not exist yet: {MEMORY_FILE}"

    try:
        content = MEMORY_FILE.read_text(encoding="utf-8")
        if old_string not in content:
            return "Error: old_string not found in memory file."
        count = content.count(old_string)
        if count > 1:
            return f"Error: old_string matches {count} occurrences in memory. Provide larger unique context."

        return memory_write(content.replace(old_string, new_string, 1))
    except Exception as exc:
        return f"Error editing memory file: {exc}"


TOOLS = {
    "memory_read": memory_read,
    "memory_write": memory_write,
    "memory_edit": memory_edit,
}


if __name__ == "__main__":
    mcp.run("stdio")
