"""Agent Manifest Loader.

Loads agent JSON manifest from direct path or agents/ directory.
"""

import json
from pathlib import Path
from typing import Any, Dict, Tuple

PKG_DIR = Path(__file__).resolve().parent
AGENTS_DIR = PKG_DIR / "agents"


def list_agents() -> list[str]:
    return [f.stem for f in AGENTS_DIR.glob("*.json")] if AGENTS_DIR.is_dir() else []


def resolve(name_or_path: str) -> Tuple[str, Dict[str, Any]]:
    """Resolve an agent name or path to (agent_name, agent_cfg)."""
    p = Path(name_or_path)
    if not p.is_file():
        p = AGENTS_DIR / f"{name_or_path}.json"
        if not p.is_file():
            p = AGENTS_DIR / name_or_path
            if not p.is_file():
                available = list_agents()
                raise FileNotFoundError(
                    f"Agent manifest '{name_or_path}' not found. Available in agents/: {available}"
                )
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Malformed JSON in agent manifest '{p}': {e.msg}", e.doc, e.pos
        )
    if not isinstance(cfg, dict):
        raise ValueError(f"Agent manifest '{p}' must be a JSON object, got {type(cfg).__name__}.")
    return p.stem, cfg


def load_manifest(name_or_path: str) -> Dict[str, Any]:
    """Load and return agent config dict."""
    _name, cfg = resolve(name_or_path)
    return cfg



