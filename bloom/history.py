"""Keeping the transcript small enough to send (append-only helpers)."""

import json
import tempfile
from pathlib import Path

CAP = 10_000
TRIMMED = "[output trimmed:"
SPILLS = []


def spill(text: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix="bloom-", suffix=".txt", delete=False, encoding="utf-8"
    )
    handle.write(text)
    handle.close()
    SPILLS.append(Path(handle.name))
    return handle.name


def cap(text: str) -> str:
    if len(text) <= CAP:
        return text

    try:
        path = spill(text)
    except OSError:
        return text[:CAP] + f"\n\n{TRIMMED} {len(text) - CAP} chars cut and the rest could not be saved.]"
    return (
        text[:CAP] + f"\n\n{TRIMMED} {len(text) - CAP} of {len(text)} chars cut. "
        f"The whole output is at {path} - page through it with "
        "head, tail, sed -n or grep. It is deleted when this turn ends.]"
    )


def sweep() -> None:
    for path in SPILLS:
        path.unlink(missing_ok=True)
    SPILLS.clear()


def estimate(messages) -> int:
    """Fast estimate of tokens from JSON-serialized messages (chars // 4)."""
    return sum(len(json.dumps(m)) for m in messages) // 4
