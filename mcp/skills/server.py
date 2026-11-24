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
_ALLOWED_SKILLS: Optional[List[str]] = None


def configure(
    skill_paths: Optional[List[str]] = None,
    allowed_skills: Optional[List[str]] = None,
) -> None:
    """Configure authorized skill root paths and optionally allowed skills."""
    global _SKILL_PATHS, _ALLOWED_SKILLS
    if skill_paths is not None:
        _SKILL_PATHS = list(skill_paths)
    if allowed_skills is not None:
        _ALLOWED_SKILLS = [s.strip().lower() for s in allowed_skills]
    else:
        _ALLOWED_SKILLS = None


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
    resolved = Path(target).expanduser().resolve()
    if not _is_allowed(resolved, _SKILL_PATHS):
        raise PermissionError(f"Access denied: skills path '{target}' is outside authorized skill_paths {_SKILL_PATHS}.")
    return resolved


# -----------------------------------------------------------------------------
# Skills Tools
# -----------------------------------------------------------------------------
@mcp.tool(meta={"summary": "List available procedural skills from configured skill directory"})
def skill_list() -> List[str]:
    """List available procedural skills defined in agent's configured skills directories."""
    skills = []
    for sp in _SKILL_PATHS:
        resolved = Path(sp).expanduser().resolve()
        if not resolved.is_dir() or not _is_allowed(resolved, _SKILL_PATHS):
            continue
        for d in sorted(resolved.iterdir()):
            if d.is_dir() and not d.name.startswith((".", "__")) and (d / "SKILL.md").exists():
                if _is_allowed(d.resolve(), _SKILL_PATHS) and _is_allowed((d / "SKILL.md").resolve(), _SKILL_PATHS):
                    skills.append(d.name)
    if _ALLOWED_SKILLS is not None:
        skills = [s for s in skills if s.lower() in _ALLOWED_SKILLS]
    return sorted(list(dict.fromkeys(skills)))


@mcp.tool(meta={"summary": "Load step-by-step workflow instructions from a skill"})
def skill_load(skill_name: str) -> str:
    """Load a skill markdown file (SKILL.md) into context."""
    if not _SKILL_PATHS:
        return "Access denied: No skill_paths configured for this agent."
    clean = skill_name.strip().lower()
    if _ALLOWED_SKILLS is not None and clean not in _ALLOWED_SKILLS:
        return f"Skill '{skill_name}' is not permitted for this agent."

    target_file = None
    for sp in _SKILL_PATHS:
        skills_dir = Path(sp).expanduser().resolve()
        if not skills_dir.is_dir() or not _is_allowed(skills_dir, _SKILL_PATHS):
            continue
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir() or not _is_allowed(skill_dir.resolve(), _SKILL_PATHS):
                continue
            name_low = skill_dir.name.lower()
            if name_low == clean or name_low.endswith(f"-{clean}") or clean.endswith(f"-{name_low}"):
                sf = (skill_dir / "SKILL.md").resolve()
                if sf.exists() and _is_allowed(sf, _SKILL_PATHS):
                    target_file = sf
                    break
        if target_file:
            break

    if not target_file:
        return f"Skill '{skill_name}' not found in {_SKILL_PATHS}."

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
