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
    """Fast estimate of tokens from messages, accounting for high token density in non-ASCII scripts (e.g. Sinhala/Tamil/CJK)."""
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content") or ""
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif isinstance(p, str):
                    parts.append(p)
            content = "\n".join(parts)
        elif not isinstance(content, str):
            content = str(content)

        tool_calls = m.get("tool_calls") or []
        for tc in tool_calls:
            if isinstance(tc, dict):
                content += json.dumps(tc)

        rc = m.get("reasoning_content")
        if isinstance(rc, str) and rc:
            content += rc

        if content.isascii():
            ascii_chars = len(content)
            non_ascii_chars = 0
        else:
            ascii_chars = len(content.encode("ascii", "ignore"))
            non_ascii_chars = len(content) - ascii_chars
        total += (ascii_chars // 4) + int(non_ascii_chars / 1.5) + 4
    return max(1, total)
