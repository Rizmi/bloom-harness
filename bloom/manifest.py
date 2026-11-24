"""Agent Manifest Loader.

Loads agent JSON manifest from direct path or agents/ directory.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

def get_project_root() -> Path:
    val = os.environ.get("BLOOM_ROOT", "")
    if val and val.strip():
        return Path(val).resolve()
    if (Path.cwd() / "agents").is_dir():
        return Path.cwd()
    return Path(__file__).resolve().parent.parent


def get_agents_dir() -> Path:
    val = os.environ.get("BLOOM_AGENTS_DIR") or os.environ.get("BLOOM_AGENTS")
    if val and val.strip():
        p = Path(val).resolve()
        return p.parent if p.is_file() else p
    if (Path.cwd() / "agents").is_dir():
        return Path.cwd() / "agents"
    return get_project_root() / "agents"


def list_agents() -> list[str]:
    agents_dir = get_agents_dir()
    return sorted([f.stem for f in agents_dir.glob("*.json")]) if agents_dir.is_dir() else []


def resolve(name_or_path: str) -> Tuple[str, Dict[str, Any]]:
    """Resolve an agent name or direct file path to (name, config)."""
    p = Path(name_or_path)
    if not p.is_file():
        p = get_agents_dir() / f"{name_or_path}.json"
        if not p.is_file():
            raise FileNotFoundError(
                f"Agent manifest '{name_or_path}' not found. Available in agents/: {list_agents()}"
            )
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(f"Malformed JSON in agent manifest '{p}': {e.msg}", e.doc, e.pos)
    if not isinstance(cfg, dict):
        raise ValueError(f"Agent manifest '{p}' must be a JSON object, got {type(cfg).__name__}.")

    for path_key in ("read_paths", "write_paths"):
        if path_key in cfg:
            val = cfg[path_key]
            if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
                raise ValueError(f"'{path_key}' in agent manifest '{p}' must be a list of non-empty strings.")

    return p.stem, cfg


def resolve_safe(name: str) -> Tuple[str, Dict[str, Any]]:
    """Strictly resolve an agent persona by clean name from agents/, disallowing arbitrary paths."""
    clean = str(name).strip()
    if "/" in clean or "\\" in clean or clean.startswith(".") or not clean:
        raise ValueError(f"Invalid agent name '{name}'. Path traversals are not permitted.")
    available = list_agents()
    if clean not in available:
        raise FileNotFoundError(f"Agent '{clean}' not found. Available: {available}")
    p = get_agents_dir() / f"{clean}.json"
    return resolve(str(p))


def load_manifest(name_or_path: str) -> Dict[str, Any]:
    _name, cfg = resolve(name_or_path)
    return cfg
