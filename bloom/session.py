"""Transcripts on disk. One JSONL file per chat."""

import json
import os
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_NAME = "default"


def get_base_dir() -> Path:
    return Path(os.environ.get("BLOOM_HOME", PROJECT_ROOT))


def get_session_dir() -> Path:
    return get_base_dir() / "sessions" / AGENT_NAME


SESSION_DIR = get_session_dir()
CURRENT = datetime.now().strftime("%Y%m%d-%H%M%S")
WRITTEN = 0  # how many messages are already on disk


def set_agent(name: str):
    """Partition session logs by active agent persona."""
    global AGENT_NAME, SESSION_DIR
    AGENT_NAME = name
    SESSION_DIR = get_session_dir()


def path_for(session_id):
    return get_session_dir() / f"{session_id}.jsonl"


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
    except OSError:
        pass


def rewind_to(count):
    """Record a rewind as an entry, so the old messages stay in the file."""
    global WRITTEN
    try:
        with path_for(CURRENT).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"rewind_to": count}) + "\n")
        WRITTEN = count
    except OSError:
        pass


def compacted(messages):
    """Compaction rewrites history, so record the result and start from it."""
    global WRITTEN
    try:
        with path_for(CURRENT).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"compacted": messages}) + "\n")
        WRITTEN = len(messages)
    except OSError:
        pass


def load(session_id):
    """Replay the log: messages accumulate, rewinds cut them back."""
    messages = []
    p = path_for(session_id)
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
            messages = [m for m in entry["compacted"] if isinstance(m, dict)]
        elif entry.get("role") in ("system", "user", "assistant", "tool"):
            messages.append(entry)
    return messages


def open_session(session_id):
    """Switch to a past chat and become it."""
    global CURRENT, WRITTEN
    CURRENT = session_id
    messages = load(session_id)
    WRITTEN = len(messages)
    return messages


def title(messages):
    if not isinstance(messages, list):
        return "(empty)"
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            return " ".join(str(message.get("content") or "").split())[:60]
    return "(empty)"


def all_sessions():
    """Newest first."""
    sdir = get_session_dir()
    if not sdir.exists():
        return []
    try:
        files = sorted(
            sdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        return [{"id": p.stem, "title": title(load(p.stem))} for p in files]
    except OSError:
        return []
