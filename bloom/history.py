"""Keeping the transcript small enough to send (append-only helpers)."""

import json

CAP = 10_000
TRIMMED = "[output trimmed:"


def cap(text: str) -> str:
    if len(text) <= CAP:
        return text
    cut = len(text) - CAP
    return (
        text[:CAP]
        + f"\n\n{TRIMMED} {cut} of {len(text)} chars cut. "
        "Narrow your query or paginate results.]"
    )


def estimate(messages) -> int:
    """Fast estimate of tokens from JSON-serialized messages (chars // 4)."""
    return sum(len(json.dumps(m)) for m in messages) // 4
