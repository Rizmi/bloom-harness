"""Agent Manifest Loader.

Loads agent JSON manifest from direct path or agents/ directory.
"""

import json
from pathlib import Path
from typing import Any, Dict, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = PROJECT_ROOT / "agents"


def list_agents() -> list[str]:
    return sorted([f.stem for f in AGENTS_DIR.glob("*.json")]) if AGENTS_DIR.is_dir() else []


def resolve(name_or_path: str) -> Tuple[str, Dict[str, Any]]:
    """Resolve an agent name or direct file path to (name, config)."""
    p = Path(name_or_path)
    if not p.is_file():
        p = AGENTS_DIR / f"{name_or_path}.json"
        if not p.is_file():
            p = AGENTS_DIR / name_or_path
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
    return p.stem, cfg


def load_manifest(name_or_path: str) -> Dict[str, Any]:
    _name, cfg = resolve(name_or_path)
    return cfg
