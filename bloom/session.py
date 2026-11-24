"""Transcripts on disk. One JSONL file per chat."""

import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path


def get_base_dir() -> Path:
    val = os.environ.get("BLOOM_HOME", "")
    if val and val.strip():
        return Path(val).resolve()
    return Path.cwd()


AGENT_NAME = "default"


def get_session_dir(agent_name=None) -> Path:
    agent = agent_name or AGENT_NAME
    return get_base_dir() / "sessions" / agent


SESSION_DIR = get_session_dir()
CURRENT = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
WRITTEN = 0  # how many messages are already on disk


def set_agent(name: str):
    """Partition session logs by active agent persona."""
    global AGENT_NAME, SESSION_DIR
    AGENT_NAME = name
    SESSION_DIR = get_session_dir()


def is_valid_session_id(session_id: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_-]{1,128}$", str(session_id).strip()))


def path_for(session_id, agent_name=None):
    clean = str(session_id).strip()
    if not is_valid_session_id(clean):
        raise ValueError(f"Invalid session ID '{session_id}'. Must match ^[A-Za-z0-9_-]{{1,128}}$.")
    return get_session_dir(agent_name) / f"{clean}.jsonl"


def new_session(session_id=None):
    """Create a unique session ID and reset written cursor."""
    global CURRENT, WRITTEN
    if session_id:
        clean = str(session_id).strip()
        if not is_valid_session_id(clean):
            raise ValueError(f"Invalid session ID '{session_id}'. Must match ^[A-Za-z0-9_-]{{1,128}}$.")
        CURRENT = clean
    else:
        CURRENT = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    WRITTEN = 0
    return CURRENT


def delete_session(session_id, agent_name=None):
    """Delete a session log file if it exists."""
    try:
        p = path_for(session_id, agent_name)
        if p.is_file():
            p.unlink()
            return True
    except (ValueError, OSError):
        pass
    return False


def save(messages):
    """Append what is new. Never rewrite what is already on disk."""
    global WRITTEN
    try:
        sdir = get_session_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        with path_for(CURRENT).open("a", encoding="utf-8") as f:
            for message in messages[WRITTEN:]:
                f.write(json.dumps(message) + "\n")
        WRITTEN = len(messages)
    except OSError as e:
        print(f"Warning: Failed to save session transcript: {e}", file=sys.stderr)


def rewind_to(count):
    """Record a rewind as an entry, so the old messages stay in the file."""
    global WRITTEN
    try:
        with path_for(CURRENT).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"rewind_to": count}) + "\n")
        WRITTEN = count
    except OSError as e:
        print(f"Warning: Failed to save session transcript: {e}", file=sys.stderr)


def compacted(messages):
    """Compaction rewrites history, so record the result and start from it."""
    global WRITTEN
    try:
        with path_for(CURRENT).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"compacted": messages}) + "\n")
        WRITTEN = len(messages)
    except OSError as e:
        print(f"Warning: Failed to save session transcript: {e}", file=sys.stderr)


def load(session_id, agent_name=None):
    """Replay the log: messages accumulate, rewinds cut them back."""
    messages = []
    p = path_for(session_id, agent_name)
    if not p.is_file():
        return messages
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return messages

    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if "rewind_to" in entry and isinstance(entry["rewind_to"], int):
            del messages[entry["rewind_to"]:]
        elif "compacted" in entry and isinstance(entry["compacted"], list):
            messages = [
                m for m in entry["compacted"]
                if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant", "tool")
            ]
        elif entry.get("role") in ("system", "user", "assistant", "tool"):
            messages.append(entry)

    sanitized = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "tool":
            i += 1
            continue
        sanitized.append(msg)
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            if not isinstance(msg["tool_calls"], list):
                i += 1
                continue

            expected_ids = []
            for idx, tc in enumerate(msg["tool_calls"]):
                tc_id = getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else "")
                if not tc_id:
                    tc_id = f"synthesized_call_{idx}"
                    if isinstance(tc, dict):
                        tc["id"] = tc_id
                    elif hasattr(tc, "id"):
                        try:
                            tc.id = tc_id
                        except Exception:
                            pass
                expected_ids.append(tc_id)

            answered_msgs = {}
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                t_msg = messages[j]
                tid = t_msg.get("tool_call_id")
                if tid and tid in expected_ids and tid not in answered_msgs:
                    answered_msgs[tid] = t_msg
                j += 1

            for tid in expected_ids:
                if tid in answered_msgs:
                    sanitized.append(answered_msgs[tid])
                else:
                    sanitized.append({
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": "Error: tool call not executed in recorded transcript.",
                    })
            i = j
            continue
        i += 1
    return sanitized


def open_session(session_id, agent_name=None):
    """Switch to a past chat and become it. Validates first without state corruption."""
    global CURRENT, WRITTEN
    clean = str(session_id).strip()
    if not is_valid_session_id(clean):
        raise ValueError(f"Invalid session ID '{session_id}'. Must match ^[A-Za-z0-9_-]{{1,128}}$.")
    p = path_for(clean, agent_name)
    if not p.is_file():
        return None
    messages = load(clean, agent_name)
    CURRENT = clean
    WRITTEN = len(messages)
    return messages


def title(messages):
    if not isinstance(messages, list):
        return "(empty)"
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            return " ".join(str(message.get("content") or "").split())[:60]
    return "(empty)"


def extract_title_from_file(path: Path) -> str:
    """Read first user prompt for fast title extraction without parsing full file."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"role": "user"' in line or '"role":"user"' in line:
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict) and entry.get("role") == "user":
                            return " ".join(str(entry.get("content") or "").split())[:60]
                    except Exception:
                        pass
    except OSError:
        pass
    return "(empty)"


def all_sessions(agent_name=None):
    """Newest first."""
    sdir = get_session_dir(agent_name)
    if not sdir.exists():
        return []
    try:
        files = sorted(
            sdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        return [{"id": p.stem, "title": extract_title_from_file(p)} for p in files]
    except OSError:
        return []
