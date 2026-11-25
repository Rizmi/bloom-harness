"""Clean Filesystem Operations MCP Server.

Provides safe, atomic filesystem access:
- Reading (line-numbered, paginated, cached mtime),
- Editing (snippet return with context, smart diff errors, CRLF handling),
- Multi-editing (atomic batch edits),
- Line replacement/insertion/deletion,
- Writing (atomic, overwrite protection),
- Grep (ripgrep/regex with line numbers),
- Glob (newest-first pattern match),
- Listing, and Deletion.
"""

import argparse
import difflib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from mcp.server.fastmcp import FastMCP
    _fastmcp = FastMCP("filesystem")
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

DEFAULT_READ_LINES = 400
MAX_READ_LINES = 2000
MAX_OUTPUT_CHARS = 40_000
MAX_LINE_CHARS = 2000
MAX_FILE_BYTES = 5 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}

_known_mtime: Dict[str, int] = {}

_READ_PATHS: List[str] = []
_WRITE_PATHS: List[str] = []


def configure(
    agent_cfg: Optional[Dict[str, Any]] = None,
    *,
    read_paths: Optional[List[str]] = None,
    write_paths: Optional[List[str]] = None,
) -> None:
    """Configure authorized read and write boundary paths."""
    global _READ_PATHS, _WRITE_PATHS
    if isinstance(agent_cfg, dict):
        if read_paths is None:
            read_paths = agent_cfg.get("read_paths", [])
        if write_paths is None:
            write_paths = agent_cfg.get("write_paths", [])
    if read_paths is not None:
        _READ_PATHS = list(read_paths)
    if write_paths is not None:
        _WRITE_PATHS = list(write_paths)


def _resolve_path(target_str: str) -> Path:
    p = Path(target_str).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


def _resolved_roots(allowed_roots: List[str]) -> List[Path]:
    return [Path(r).expanduser().resolve() for r in allowed_roots]


def _is_resolved_allowed(p: Path, resolved_roots: List[Path]) -> bool:
    return any(p.is_relative_to(root) for root in resolved_roots)


def _is_path_allowed(p: Path, allowed_roots: List[str]) -> bool:
    return _is_resolved_allowed(p, _resolved_roots(allowed_roots))


def _check_read(target_str: str) -> Path:
    p = _resolve_path(target_str)
    if not _is_path_allowed(p, _READ_PATHS):
        raise PermissionError(f"Access denied: path '{target_str}' is outside authorized read_paths {_READ_PATHS}.")
    return p


def _check_write(target_str: str) -> Path:
    p = _resolve_path(target_str)
    if not _is_path_allowed(p, _WRITE_PATHS):
        raise PermissionError(f"Access denied: path '{target_str}' is outside authorized write_paths {_WRITE_PATHS}.")
    return p


def _check_size(p: Path, label: str) -> Optional[str]:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return f"Error: File '{label}' exceeds maximum size of {MAX_FILE_BYTES} bytes."
    except OSError:
        pass
    return None


def _load(p: Path):
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"File '{p.name}' exceeds maximum size of {MAX_FILE_BYTES} bytes.")
    try:
        _known_mtime[str(p)] = p.stat().st_mtime_ns
    except OSError:
        pass
    text = p.read_bytes().decode("utf-8", errors="replace")
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    return text, crlf


def _save(p: Path, text: str, crlf: bool):
    """Atomic write, preserves CRLF and refreshes mtime state."""
    if crlf:
        text = text.replace("\n", "\r\n")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if p.exists():
            shutil.copymode(p, tmp)
        os.replace(tmp, p)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    _known_mtime[str(p)] = p.stat().st_mtime_ns


def _stale(p: Path) -> Optional[str]:
    """Error if file changed on disk since we last read/wrote it."""
    k = str(p)
    if k in _known_mtime:
        try:
            cur_mtime = p.stat().st_mtime_ns
        except OSError:
            return None
        if _known_mtime[k] != cur_mtime:
            return f"Error: '{p.name}' changed on disk since last read. Call file_read again, then retry."
    return None


def _snippet(text: str, first: int, last: int, ctx: int = 3) -> str:
    lines = text.split("\n")
    a = max(1, first - ctx)
    b = min(len(lines), last + ctx)
    return "\n".join(f"{i}\t{lines[i - 1][:MAX_LINE_CHARS]}" for i in range(a, b + 1))


def _hint(text: str, old: str) -> str:
    first = next((l.strip() for l in old.split("\n") if l.strip()), "")
    if not first:
        return ""
    lines = text.split("\n")
    stripped = [l.strip() for l in lines]
    m = difflib.get_close_matches(first, stripped, n=1, cutoff=0.6)
    if m:
        i = stripped.index(m[0])
        return f" Closest line {i + 1}: {lines[i][:200]!r} (check whitespace/indentation)."
    return " Re-read the file, it may have changed."


def _apply_edit(text: str, old: str, new: str, replace_all: bool):
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")
    if old == "":
        raise ValueError("old_string is empty. Use file_write to create a file.")
    if old == new:
        raise ValueError("old_string and new_string are identical.")
    n = text.count(old)
    if n == 0:
        raise ValueError("old_string not found." + _hint(text, old))
    if n > 1 and not replace_all:
        locs, pos = [], 0
        while len(locs) < 10:
            pos = text.find(old, pos)
            if pos == -1:
                break
            locs.append(text[:pos].count("\n") + 1)
            pos += len(old)
        raise ValueError(
            f"old_string matches {n} places (lines {locs}). "
            "Add more surrounding context to make it unique, or set replace_all=True."
        )
    first_idx = text.find(old)
    out = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    first_line = text[:first_idx].count("\n") + 1
    return out, n, first_line


# -----------------------------------------------------------------------------
# Read & Search Tools
# -----------------------------------------------------------------------------

@mcp.tool(meta={"summary": "Read a file (line-numbered, paginated). Use file_grep to locate things first."})
def file_read(file_path: str, start_line: int = 1, max_lines: int = DEFAULT_READ_LINES) -> str:
    """Read a file with line numbers. Returns at most max_lines (default 400, cap 2000)."""
    if not file_path:
        return "Error: file_path is required."
    start_line = max(1, int(start_line))
    max_lines = min(max(1, int(max_lines)), MAX_READ_LINES)
    p = _check_read(file_path)
    if not p.is_file():
        return f"Error: File not found: {file_path}"
    if p.stat().st_size > MAX_FILE_BYTES:
        return f"Error: File '{file_path}' exceeds maximum size of {MAX_FILE_BYTES} bytes."

    text, _ = _load(p)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total == 0:
        return "(empty file)"
    if start_line > total:
        return f"start_line {start_line} is beyond end of file ({total} lines)."

    out, size, end = [], 0, start_line - 1
    for i in range(start_line, min(total, start_line + max_lines - 1) + 1):
        ln = lines[i - 1]
        if len(ln) > MAX_LINE_CHARS:
            ln = ln[:MAX_LINE_CHARS] + " ...[line truncated]"
        row = f"{i}\t{ln}"
        if size + len(row) > MAX_OUTPUT_CHARS:
            break
        out.append(row)
        size += len(row) + 1
        end = i

    footer = f"\n[lines {start_line}-{end} of {total}]"
    if end < total:
        footer += f" more available: start_line={end + 1}"
    return "\n".join(out) + footer


@mcp.tool(meta={"summary": "Regex search across files"})
def file_grep(pattern: str, path: str = ".", glob: Optional[str] = None,
              ignore_case: bool = False, max_results: int = 100) -> str:
    """Search file contents with a regex. Returns path:line:text."""
    if not pattern:
        return "Error: pattern is required."
    root = _check_read(path)
    if not root.exists():
        return f"Path not found: {path}"

    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as ex:
        return f"Bad regex: {ex}"

    allowed_roots = _resolved_roots(_READ_PATHS)

    if root.is_file():
        files = [root]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            dp = Path(dirpath)
            for fname in sorted(filenames):
                fp = dp / fname
                if glob and not fp.relative_to(root).match(glob):
                    continue
                files.append(fp)

    res = []
    cwd = Path.cwd()
    for f in files:
        resolved = f.resolve()
        if not _is_resolved_allowed(resolved, allowed_roots):
            continue
        try:
            if resolved.stat().st_size > MAX_FILE_BYTES:
                continue
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        try:
                            rel_f = f.relative_to(cwd)
                        except ValueError:
                            rel_f = f
                        res.append(f"{rel_f}:{i}:{line.rstrip()[:300]}")
                        if len(res) >= max_results:
                            return "\n".join(res) + "\n[truncated]"
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(res) if res else "No matches."


@mcp.tool(meta={"summary": "List files by glob pattern, newest first"})
def file_glob(pattern: str = "**/*", path: str = ".", max_results: int = 200) -> str:
    """List files matching a glob, newest first."""
    root = _check_read(path)
    if not root.is_dir():
        return f"Not a directory: {path}"

    allowed_roots = _resolved_roots(_READ_PATHS)
    found = []

    if pattern.startswith("**/"):
        subpat = pattern[3:] or "*"
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            dp = Path(dirpath)
            for fname in sorted(filenames):
                fp = dp / fname
                if subpat != "*" and not fp.relative_to(root).match(subpat):
                    continue
                if _is_resolved_allowed(fp.resolve(), allowed_roots):
                    found.append(fp)
    else:
        for f in root.glob(pattern):
            if not f.is_file():
                continue
            resolved = f.resolve()
            if not _is_resolved_allowed(resolved, allowed_roots):
                continue
            try:
                rel_parts = f.relative_to(root).parts
            except ValueError:
                rel_parts = resolved.parts[-1:]
            if set(rel_parts) & SKIP_DIRS:
                continue
            found.append(f)

    def _safe_mtime(f: Path) -> float:
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    found.sort(key=_safe_mtime, reverse=True)
    if not found:
        return "No files."
    shown = found[:max_results]
    more = f"\n[{len(found) - len(shown)} more not shown]" if len(found) > len(shown) else ""
    lines = []
    cwd = Path.cwd()
    for f in shown:
        try:
            lines.append(str(f.relative_to(cwd)))
        except ValueError:
            lines.append(str(f))
    return "\n".join(lines) + more


@mcp.tool(meta={"summary": "List files and folders inside a directory"})
def file_list(directory: str = ".", path: Optional[str] = None) -> List[str]:
    """List files and folders inside a directory."""
    target = path if path is not None else directory
    p = _check_read(target)
    if not p.exists() or not p.is_dir():
        return [f"Error: Directory not found: {target}"]

    allowed_roots = _resolved_roots(_READ_PATHS)
    results = []
    cwd = Path.cwd()
    for item in sorted(p.iterdir()):
        if item.name in SKIP_DIRS:
            continue
        if not _is_resolved_allowed(item.resolve(), allowed_roots):
            continue
        try:
            rel = item.relative_to(cwd).as_posix()
            results.append(f"{rel}/" if item.is_dir() else rel)
        except ValueError:
            results.append(f"{item.as_posix()}/" if item.is_dir() else item.as_posix())
        if len(results) >= 100:
            break
    return results


# -----------------------------------------------------------------------------
# Write & Edit Tools
# -----------------------------------------------------------------------------

@mcp.tool(meta={"summary": "Replace an exact snippet in a file (must be unique unless replace_all)"})
def file_edit(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace old_string with new_string."""
    if not file_path:
        return "Error: file_path is required."
    p = _check_write(file_path)
    if not p.is_file():
        return f"Error: File not found: {file_path}"
    s = _stale(p)
    if s:
        return s
    try:
        text, crlf = _load(p)
        new_text, n, first_line = _apply_edit(text, old_string, new_string, replace_all)
        _save(p, new_text, crlf)
    except ValueError as ve:
        return f"Error: {ve}"
    except Exception as ex:
        return f"Error editing file: {ex}"
    span = new_string.count("\n")
    return (f"File '{file_path}' edited successfully ({n} replacement{'s' if n > 1 else ''}). "
            f"Now:\n{_snippet(new_text, first_line, first_line + span)}")


@mcp.tool(meta={
    "summary": "Apply several edits to one file in a single atomic call",
    "parameters": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "old_string": {"type": "string", "description": "Exact text to find and replace."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)."},
                },
                "required": ["old_string", "new_string"],
            },
            "description": "List of edit operations to apply sequentially.",
        }
    }
})
def file_multi_edit(file_path: str, edits: List[Dict[str, Any]]) -> str:
    """Apply multiple edits to ONE file in order."""
    if not file_path:
        return "Error: file_path is required."
    p = _check_write(file_path)
    if not edits:
        return "Error: edits list is empty."
    if not p.is_file():
        return f"Error: File not found: {file_path}"
    s = _stale(p)
    if s:
        return s
    try:
        text, crlf = _load(p)
        for i, ed in enumerate(edits, 1):
            try:
                text, _, _ = _apply_edit(text, ed.get("old_string", ""), ed.get("new_string", ""),
                                         bool(ed.get("replace_all", False)))
            except ValueError as ve:
                return f"Error in edit #{i}: {ve} (nothing was written)"
        _save(p, text, crlf)
    except Exception as ex:
        return f"Error editing file: {ex}"
    return f"Applied {len(edits)} edits to {p.name}. File is now {text.count(chr(10)) + 1} lines."


@mcp.tool(meta={"summary": "Replace or insert a line range (for big edits, no need to repeat old text)"})
def file_replace_lines(file_path: str, start_line: int, end_line: int, new_content: str = "") -> str:
    """Replace lines start_line..end_line with new_content."""
    if not file_path:
        return "Error: file_path is required."
    p = _check_write(file_path)
    if not p.is_file():
        return f"Error: File not found: {file_path}"
    s = _stale(p)
    if s:
        return s
    try:
        text, crlf = _load(p)
        lines = text.split("\n")
        had_trailing_nl = lines and lines[-1] == ""
        if had_trailing_nl:
            lines.pop()
        total = len(lines)
        if start_line < 1 or start_line > total + 1 or end_line < start_line - 1 or end_line > total:
            return f"Error: invalid range {start_line}-{end_line} (file has {total} lines)."
        new_lines = new_content.splitlines() if new_content != "" else []
        lines[start_line - 1:end_line] = new_lines
        new_text = "\n".join(lines) + ("\n" if had_trailing_nl and lines else "")
        _save(p, new_text, crlf)
    except Exception as ex:
        return f"Error editing file: {ex}"
    last = start_line + max(len(new_lines) - 1, 0)
    return f"Replaced lines {start_line}-{end_line} in {p.name}. Now:\n{_snippet(new_text, start_line, last)}"


@mcp.tool(meta={"summary": "Create a new file (or fully overwrite with overwrite=True)"})
def file_write(file_path: str, content: str = "", overwrite: bool = False) -> str:
    """Create a new file or overwrite existing."""
    if not file_path:
        return "Error: file_path is required."
    p = _check_write(file_path)
    if p.exists() and not overwrite:
        return f"File '{file_path}' already exists. Use overwrite=True or prefer file_edit for changes."
    try:
        _save(p, content, False)
    except Exception as ex:
        return f"Error writing file: {ex}"
    return f"Wrote {len(content)} characters to '{file_path}'."


@mcp.tool(meta={"summary": "Delete a file"})
def file_delete(file_path: str) -> str:
    """Delete a file."""
    if not file_path:
        return "Error: file_path is required."
    p = _check_write(file_path)
    if not p.is_file():
        return f"Error: File not found: {file_path}"
    try:
        p.unlink()
        _known_mtime.pop(str(p), None)
    except Exception as ex:
        return f"Error deleting file: {ex}"
    return f"Deleted '{file_path}' successfully."


TOOLS = {
    "file_read": file_read,
    "file_grep": file_grep,
    "file_glob": file_glob,
    "file_list": file_list,
    "file_edit": file_edit,
    "file_multi_edit": file_multi_edit,
    "file_replace_lines": file_replace_lines,
    "file_write": file_write,
    "file_delete": file_delete,
}


def main():
    parser = argparse.ArgumentParser(description="Filesystem Operations MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--read-paths", nargs="*", default=None, help="Authorized read roots")
    parser.add_argument("--write-paths", nargs="*", default=None, help="Authorized write roots")
    args = parser.parse_args()

    configure(read_paths=args.read_paths, write_paths=args.write_paths)

    if args.transport == "sse":
        mcp.run("sse", host=args.host, port=args.port)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
