"""Memory Operations MCP Server.

Provides persistent agent memory storage (MEMORY.md):
- Reading memory file (memory_read).
- Atomic writing / replacing memory file (memory_write).
- Exact unique snippet editing (memory_edit).
"""

import argparse
import os
import tempfile
from pathlib import Path
from typing import Optional

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

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
MEMORY_FILE: Path = (ROOT_DIR / "MEMORY.md").resolve()


def configure(
    agent_cfg: Optional[dict] = None,
    memory_path: Optional[str] = None,
    **kwargs,
) -> None:
    """Configure target MEMORY.md file path."""
    global MEMORY_FILE
    if memory_path:
        p = Path(memory_path)
    elif agent_cfg and agent_cfg.get("memory_file"):
        p = Path(agent_cfg["memory_file"])
    else:
        env_val = os.getenv("BLOOM_MEMORY_FILE") or os.getenv("TECHENO_MEMORY_FILE") or os.getenv("ADK_MEMORY_FILE")
        p = Path(env_val) if env_val else (ROOT_DIR / "MEMORY.md")
    
    if not p.is_absolute():
        p = ROOT_DIR / p
    MEMORY_FILE = p


def _guard_memory(path: Path) -> Optional[str]:
    """Reject a memory path that is a symlink or cannot be resolved."""
    if path.is_symlink():
        return f"Access denied: memory file '{path}' is a symlink. Refusing to follow it."
    try:
        path.resolve()
    except OSError as exc:
        return f"Error: memory path could not be resolved: {exc}"
    return None


@mcp.tool(meta={"summary": "Read agent's long-lived memory (MEMORY.md)"})
def memory_read() -> str:
    """Read the agent's persistent memory file (MEMORY.md).

    Returns the entire file as text. Use this before editing so changes are
    based on the current content.
    """
    path = MEMORY_FILE
    denied = _guard_memory(path)
    if denied:
        return denied
    if not path.exists():
        return f"Memory file does not exist yet: {path}"
    if not path.is_file():
        return f"Memory path is not a regular file: {path}"

    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error reading memory file: {exc}"


@mcp.tool(meta={"summary": "Overwrite long-lived memory (MEMORY.md)"})
def memory_write(content: str) -> str:
    """Overwrite the agent's memory file with new content.

    Use this to restructure or compact memory. Writes are atomic.

    Args:
        content: The full new text of the memory file.
    """
    path = MEMORY_FILE
    denied = _guard_memory(path)
    if denied:
        return denied

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".memory.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_name, path)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return f"Memory file written ({len(content)} bytes)."
    except Exception as exc:
        return f"Error writing memory file: {exc}"


@mcp.tool(meta={"summary": "Replace exact text snippet in long-lived memory (MEMORY.md)"})
def memory_edit(old_string: str, new_string: str) -> str:
    """Replace an exact snippet in the agent's memory file.

    Args:
        old_string: Exact text to find. Must occur exactly once.
        new_string: Replacement text.
    """
    path = MEMORY_FILE
    denied = _guard_memory(path)
    if denied:
        return denied
    if not path.exists():
        return f"Memory file does not exist yet: {path}"

    try:
        content = path.read_text(encoding="utf-8")
        if old_string not in content:
            return "Error: old_string not found in memory file."
        count = content.count(old_string)
        if count > 1:
            return (
                f"Error: old_string matches {count} occurrences in memory. "
                "Provide a larger unique context."
            )

        updated = content.replace(old_string, new_string, 1)
        return memory_write(updated)
    except Exception as exc:
        return f"Error editing memory file: {exc}"


TOOLS = {
    "memory_read": memory_read,
    "memory_write": memory_write,
    "memory_edit": memory_edit,
}


def main():
    parser = argparse.ArgumentParser(description="Memory Operations MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="Transport type (stdio or sse)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST", "127.0.0.1"),
        help="Host for SSE transport",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MCP_PORT_MEMORY", os.getenv("MCP_PORT", "8103"))),
        help="Port for SSE transport",
    )
    parser.add_argument("--memory-file", default=None, help="Target MEMORY.md file path")
    args = parser.parse_args()

    configure(memory_path=args.memory_file)

    if args.transport == "sse":
        mcp.run("sse", host=args.host, port=args.port)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
