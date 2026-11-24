"""Memory Operations MCP Server.

Provides persistent agent memory storage (MEMORY.md).
Requires MEMORY_FILE environment variable.
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

MEMORY_FILE: Path | None = None


def get_memory_file() -> Path:
    global MEMORY_FILE
    if MEMORY_FILE is not None:
        return MEMORY_FILE
    if "MEMORY_FILE" not in os.environ:
        raise RuntimeError("Missing required environment variable 'MEMORY_FILE' in .env")
    return Path(os.environ["MEMORY_FILE"]).resolve()


@mcp.tool(meta={"summary": "Read agent's long-lived memory (MEMORY.md)"})
def memory_read() -> str:
    """Read the agent's persistent memory file (MEMORY.md)."""
    target = get_memory_file()
    if target.is_symlink():
        return f"Access denied: memory file '{target}' is a symlink. Refusing to follow it."
    if not target.exists():
        return f"Memory file does not exist yet: {target}"
    if not target.is_file():
        return f"Memory path is not a regular file: {target}"
    return target.read_text(encoding="utf-8")


@mcp.tool(meta={"summary": "Overwrite long-lived memory (MEMORY.md)"})
def memory_write(content: str) -> str:
    """Overwrite the agent's memory file with new content atomically."""
    target = get_memory_file()
    if target.is_symlink():
        return f"Access denied: memory file '{target}' is a symlink. Refusing to follow it."
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".memory.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, target)
        return f"Memory file written ({len(content)} bytes)."
    except Exception as exc:
        Path(temp_name).unlink(missing_ok=True)
        return f"Error writing memory file: {exc}"


@mcp.tool(meta={"summary": "Replace exact text snippet in long-lived memory (MEMORY.md)"})
def memory_edit(old_string: str, new_string: str) -> str:
    """Replace an exact unique snippet in the agent's memory file."""
    target = get_memory_file()
    if target.is_symlink():
        return f"Access denied: memory file '{target}' is a symlink. Refusing to follow it."
    if not target.exists():
        return f"Memory file does not exist yet: {target}"

    content = target.read_text(encoding="utf-8")
    if old_string not in content:
        return "Error: old_string not found in memory file."
    count = content.count(old_string)
    if count > 1:
        return f"Error: old_string matches {count} occurrences in memory. Provide larger unique context."

    return memory_write(content.replace(old_string, new_string, 1))


TOOLS = {
    "memory_read": memory_read,
    "memory_write": memory_write,
    "memory_edit": memory_edit,
}


if __name__ == "__main__":
    mcp.run("stdio")
