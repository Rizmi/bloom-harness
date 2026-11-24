"""Workflow Skills MCP Server.

Provides procedural workflow instructions and skill discovery from an explicitly provided directory:
- Listing registered workflow skills.
- Loading skill instructions (SKILL.md) into execution context.
"""

import argparse
import os
from pathlib import Path
from typing import List, Optional

try:
    from mcp.server.fastmcp import FastMCP
    _fastmcp = FastMCP("skills")
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


_SKILL_PATHS: List[str] = []


def configure(skill_paths: Optional[List[str]] = None) -> None:
    """Configure authorized skill root paths."""
    global _SKILL_PATHS
    if skill_paths is not None:
        _SKILL_PATHS = list(skill_paths)


def _is_allowed(p: Path, allowed_roots: List[str]) -> bool:
    if not allowed_roots:
        return False
    for root_str in allowed_roots:
        root = Path(root_str).expanduser().resolve()
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _resolve_skills_dir(skills_path: Optional[str] = None) -> Path:
    if not _SKILL_PATHS:
        raise PermissionError("Access denied: No skill_paths configured for this agent.")
    target = skills_path or _SKILL_PATHS[0]
    p = Path(target).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    resolved = p.resolve()
    if not _is_allowed(resolved, _SKILL_PATHS):
        raise PermissionError(f"Access denied: skills path '{target}' is outside authorized skill_paths {_SKILL_PATHS}.")
    return resolved


# -----------------------------------------------------------------------------
# Skills Tools
# -----------------------------------------------------------------------------
@mcp.tool(meta={"summary": "List available procedural skills from configured skill directory"})
def skill_list() -> List[str]:
    """List available procedural skills defined in agent's configured skills directory.

    Returns:
        Folder names of all discovered skills containing a SKILL.md.
    """
    skills_dir = _resolve_skills_dir()
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []
    return [
        d.name
        for d in sorted(skills_dir.iterdir())
        if d.is_dir()
        and not d.name.startswith((".", "__"))
        and (d / "SKILL.md").exists()
        and _is_allowed(d.resolve(), _SKILL_PATHS)
        and _is_allowed((d / "SKILL.md").resolve(), _SKILL_PATHS)
    ]


@mcp.tool(meta={"summary": "Load step-by-step workflow instructions from a skill"})
def skill_load(skill_name: str) -> str:
    """Load a skill markdown file (SKILL.md) into context.

    Args:
        skill_name: The folder name or identifier of the skill.
    """
    skills_dir = _resolve_skills_dir()
    clean = skill_name.strip().lower()
    if not skills_dir.exists():
        return f"Skills directory not found: {skills_dir}"

    target_file = None
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir() or not _is_allowed(skill_dir.resolve(), _SKILL_PATHS):
            continue
        name_low = skill_dir.name.lower()
        if name_low == clean or name_low.endswith(f"-{clean}") or clean.endswith(f"-{name_low}"):
            sf = (skill_dir / "SKILL.md").resolve()
            if sf.exists() and _is_allowed(sf, _SKILL_PATHS):
                target_file = sf
                break

    if not target_file:
        return f"Skill '{skill_name}' not found in {skills_dir}."

    try:
        return target_file.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading skill '{skill_name}': {e}"


TOOLS = {
    "skill_list": skill_list,
    "skill_load": skill_load,
}


# -----------------------------------------------------------------------------
# Main Runner
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Workflow Skills MCP Server")
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
        default=int(os.getenv("MCP_PORT_SKILLS", os.getenv("MCP_PORT", "8102"))),
        help="Port for SSE transport",
    )
    parser.add_argument("--skill-paths", nargs="*", default=None, help="Authorized skill roots")
    args = parser.parse_args()

    configure(skill_paths=args.skill_paths)

    if args.transport == "sse":
        mcp.run("sse", host=args.host, port=args.port)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
