"""Agent Manifest Loader.

Loads agent definitions from JSON files (e.g. agents/coder.json).
"""

import json
from pathlib import Path
from typing import Any, Dict

PKG_DIR = Path(__file__).resolve().parent
AGENTS_DIR = PKG_DIR / "agents"


def load_manifest(name_or_path: str = "coder") -> Dict[str, Any]:
    """Load agent manifest by name (e.g. 'coder') or direct file path."""
    p = Path(name_or_path)
    if not p.is_file():
        p = AGENTS_DIR / f"{name_or_path}.json"
        if not p.is_file():
            p = AGENTS_DIR / name_or_path
            if not p.is_file():
                available = [f.stem for f in AGENTS_DIR.glob("*.json")] if AGENTS_DIR.is_dir() else []
                raise FileNotFoundError(
                    f"Agent manifest '{name_or_path}' not found in {AGENTS_DIR}. Available: {available}"
                )
    return json.loads(p.read_text(encoding="utf-8"))
