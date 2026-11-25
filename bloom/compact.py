"""The compaction agent.

A second agent with one job: read a transcript that has grown too big and
write the handoff note a fresh agent would need to carry on.
"""

import os
import re

from . import llm
from .history import estimate

def compact_at() -> float:
    return float(os.environ.get("COMPACT_AT", 0.85))


def compact_to() -> float:
    return float(os.environ.get("COMPACT_TO", 0.35))


def reserved_output() -> int:
    return int(os.environ.get("RESERVED_OUTPUT", 4000))


def compact_window() -> int:
    val = os.environ.get("COMPACT_WINDOW")
    if val and val.strip():
        return min(llm.CONTEXT_WINDOW, int(val.strip()))
    return llm.CONTEXT_WINDOW

SYSTEM_PROMPT = """
You are compacting the transcript of a session. The session is out of
context window. Write the handoff note that lets a fresh agent pick the work up
without re-reading anything.

Use these sections, in this order. Skip any that would be empty.

## Goal
What the user asked for. Quote them where the exact wording matters.

## What happened
Decisions taken and the reasoning behind them. Include approaches that were
tried and abandoned, and why.

## State
What works, what is broken, what was left half-finished.

## Next
The immediate next step.

Rules:
- Be specific.
- Keep anything the user explicitly asked for, corrected, or rejected.
- Never invent progress. If something was not finished, say it was not.
- No preamble and no sign-off. Start at the first heading.
"""

HANDOFF = """<summary>
Everything before this point has been compacted out of the context window to
free up room. This is the record of it - treat it as your own memory of the
work so far, not as something the user told you.

{summary}
</summary>"""


def needed(usage):
    """Has the last request grown past the point where we rebuild?"""
    budget = max(1000, (compact_window() - reserved_output()) * compact_at())
    return (usage.get("prompt_tokens") or 0) > budget


def needed_before(messages):
    """Check if the estimated transcript size exceeds the threshold before an API call."""
    budget = max(1000, (compact_window() - reserved_output()) * compact_at())
    return estimate(messages) > budget


ROLES = {"user": "USER", "assistant": "ASSISTANT", "tool": "TOOL RESULT"}


def render(messages):
    """Flatten the transcript into something the summariser can read."""
    lines = []
    for message in messages:
        if message["role"] == "system":
            continue

        content = message.get("content") or ""
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict):
                content += f"\n[called {function.get('name', 'unknown')}: {function.get('arguments', '')}]"

        lines.append(f"{ROLES.get(message['role'], message['role'])}: {content}")
    return "\n\n".join(lines)


async def summarize(messages):
    """One LLM call, no tools. Returns the handoff note."""
    timeout = float(os.environ.get("COMPACT_TIMEOUT", 120.0))
    max_tokens = int(os.environ.get("COMPACT_MAX_TOKENS", 4000))
    response = await llm.get_client().chat.completions.create(
        model=llm.MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": render(messages)},
        ],
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return response.choices[0].message.content


def safe_boundary(messages, start):
    for index in range(max(start, 1), len(messages)):
        previous = messages[index - 1]
        if messages[index]["role"] == "tool" or previous.get("tool_calls"):
            continue
        return index
    return len(messages)


def tail_start(messages, budget):
    total = 0
    for index in range(len(messages) - 1, 0, -1):
        total += estimate([messages[index]])
        if total > budget:
            cut = safe_boundary(messages, index)
            if cut < len(messages):
                return cut
            # Do not wipe the trailing incomplete/active block if index fell inside tool results.
            for back in range(index - 1, 0, -1):
                if messages[back]["role"] != "tool" and not messages[back - 1].get("tool_calls"):
                    return back
            return 1
    return safe_boundary(messages, 1)


async def compact(messages):
    cut = tail_start(messages, compact_window() * compact_to())
    if cut <= 1:
        return messages

    raw_head = messages[0].get("content") or ""
    clean_base = re.sub(r"<summary>.*?</summary>", "", raw_head, flags=re.DOTALL).strip()
    if messages[0].get("role") != "system" or not clean_base:
        clean_base = llm.SYSTEM_PROMPT

    to_summarize = messages[1:cut]
    max_input_tokens = int(os.environ.get("COMPACT_MAX_INPUT_TOKENS", "32000"))
    if estimate(to_summarize) > max_input_tokens and len(to_summarize) > 2:
        accum = 0
        slice_start = 1
        for idx in range(len(to_summarize) - 1, 0, -1):
            accum += estimate([to_summarize[idx]])
            if accum >= max_input_tokens - 1000:
                slice_start = safe_boundary(to_summarize, idx)
                break
        to_summarize = [to_summarize[0]] + to_summarize[slice_start:]

    summary = await summarize(to_summarize)
    if not summary or not summary.strip():
        raise RuntimeError("Compaction failed: summarizer returned empty summary.")
    handoff_text = HANDOFF.format(summary=summary.strip())
    kept = [
        {"role": "system", "content": f"{clean_base}\n\n{handoff_text}"},
        *messages[cut:],
    ]

    return kept
