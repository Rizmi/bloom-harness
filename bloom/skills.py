"""Skills Registry and Gatekeeper for Bloom Harness.

First Gate: Harness discovers and registers skills from bloom/skills.json.
Second Gate: Agent manifests specify which skill paths or skills are authorized.
"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_skills_file() -> Path:
    if "BLOOM_SKILLS_FILE" in os.environ:
        return Path(os.environ["BLOOM_SKILLS_FILE"])
    return Path(__file__).resolve().parent / "skills.json"


def resolve_skill_root(path_str: str, config_dir: Optional[Path] = None) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p.resolve()
    if config_dir is not None:
        cand = (config_dir / p).resolve()
        if cand.is_dir():
            return cand
    return (PROJECT_ROOT / p).resolve()


def load_registered_skills() -> Tuple[List[Path], Dict[str, Path]]:
    """Gate 1: Scan directories explicitly configured in bloom/skills.json.

    Returns:
        (registered_roots, registered_skills_dict)
        where registered_skills_dict maps skill_name -> directory containing SKILL.md.
    """
    skills_file = get_skills_file()
    if not skills_file.is_file():
        return [], {}

    try:
        data = json.loads(skills_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Warning: Failed to parse skills configuration '{skills_file}': {e}", file=sys.stderr)
        return [], {}

    if isinstance(data, dict):
        raw_paths = data.get("skill_paths") or data.get("skills") or data.get("paths") or []
    elif isinstance(data, list):
        raw_paths = data
    else:
        print(f"Warning: Malformed skills configuration '{skills_file}': expected object or list.", file=sys.stderr)
        return [], {}

    registered_roots: List[Path] = []
    registered_skills: Dict[str, Path] = {}

    for rp in raw_paths:
        root = resolve_skill_root(str(rp), skills_file.parent)
        if not root.is_dir():
            continue
        registered_roots.append(root)
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and not sub.name.startswith((".", "__")):
                skill_md = sub / "SKILL.md"
                if skill_md.is_file():
                    registered_skills[sub.name.lower()] = sub

    return registered_roots, registered_skills


def validate_agent_skills(
    agent_cfg: dict, agent_name: str = "agent"
) -> Tuple[List[str], Optional[List[str]]]:
    """Gate 2: Validate requested agent skill paths and individual skills against Gate 1.

    Returns:
        (authorized_roots, authorized_skills)
        where authorized_roots is a list of directory path strings and
        authorized_skills is an optional list of allowed skill names.
    """
    registered_roots, registered_skills = load_registered_skills()

    raw_req_paths = (
        agent_cfg.get("skill_paths")
        or agent_cfg.get("skills_paths")
        or ([agent_cfg["skills_path"]] if "skills_path" in agent_cfg else [])
    )
    if isinstance(raw_req_paths, str):
        req_paths = [raw_req_paths]
    elif isinstance(raw_req_paths, list):
        req_paths = raw_req_paths
    else:
        print(f"Warning: 'skill_paths' in agent '{agent_name}' must be a list of strings.", file=sys.stderr)
        req_paths = []

    authorized_roots: List[str] = []
    for rp in req_paths:
        if not isinstance(rp, str):
            continue
        resolved = resolve_skill_root(rp)
        is_allowed = False
        for root in registered_roots:
            try:
                resolved.relative_to(root)
                is_allowed = True
                break
            except ValueError:
                continue
        if is_allowed and resolved.is_dir():
            authorized_roots.append(str(resolved))
        else:
            print(
                f"Warning: Skill path '{rp}' requested by agent '{agent_name}' "
                f"is not within registered roots in bloom/skills.json. Skipped.",
                file=sys.stderr,
            )

    req_skills = agent_cfg.get("skills")
    authorized_skills: Optional[List[str]] = None
    if req_skills is not None:
        if isinstance(req_skills, list):
            auth_list = []
            for s in req_skills:
                clean = str(s).strip().lower()
                if clean in registered_skills:
                    auth_list.append(clean)
                else:
                    print(
                        f"Warning: Skill '{s}' requested by agent '{agent_name}' "
                        f"is not found in registered skill paths. Skipped.",
                        file=sys.stderr,
                    )
            authorized_skills = auth_list
        else:
            print(
                f"Warning: 'skills' field in agent '{agent_name}' must be a list of strings.",
                file=sys.stderr,
            )
            authorized_skills = []

    return authorized_roots, authorized_skills
