"""Slash commands. Anything typed starting with / lands here."""

from . import compact as compaction
from . import session
from .ui import ui

COMMANDS = {
    "/help": "show available commands",
    "/rewind": "jump back to an earlier point in this chat",
    "/sessions": "open a past chat",
    "/compact": "summarise the history so far and free up the context window",
}


def preview(message):
    if message.get("tool_calls"):
        return "-> " + message["tool_calls"][0]["function"]["name"]
    return " ".join(str(message.get("content") or "").split())[:70]


def redraw(messages, label):
    """The screen no longer matches the history, so wipe it and draw again."""
    ui.clear()
    ui.banner()
    ui.resumed(messages, label)
    ui.replay(messages)
    return messages


def get_rewind_candidates(messages):
    candidates = []
    for i, m in enumerate(messages):
        if i == 0 and m.get("role") == "system":
            continue
        if i + 1 >= len(messages):
            continue
        if m.get("role") == "user" or (m.get("role") == "assistant" and not m.get("tool_calls")):
            candidates.append({"index": i, "target_index": i + 1, "role": m.get("role"), "preview": preview(m)})
    return candidates


def rewind(messages):
    cand_list = get_rewind_candidates(messages)
    if not cand_list:
        ui.note("nothing to rewind")
        return messages
    rows = [f"{c['role']:<9} {c['preview']}" for c in cand_list]
    choice = ui.pick("rewind to", rows)
    if choice is None:
        return messages
    target_idx = cand_list[choice]["target_index"]
    session.rewind_to(target_idx)
    return redraw(messages[:target_idx], "rewound")


def sessions(messages):
    saved = session.all_sessions()
    if not saved:
        ui.note("no saved chats yet")
        return messages
    rows = [f"{s['id']}  {s['title']}" for s in saved]
    choice = ui.pick("open chat", rows)
    if choice is None:
        return messages

    return redraw(session.open_session(saved[choice]["id"]), "opened")


async def compact(messages, session_id=None, raise_on_error=False):
    before = len(messages)
    try:
        with ui.working("compacting"):
            compacted = await compaction.compact(messages)
    except Exception as failure:  # noqa: BLE001
        if raise_on_error:
            raise
        # Compaction is one more API call, and it fires when the window is
        # nearly full - the worst moment to lose the session over a rate limit.
        ui.note(f"compaction failed ({type(failure).__name__}); transcript kept as is")
        return messages
    if compacted is messages:
        ui.note("nothing old enough to compact yet")
        return messages
    session.compacted(compacted, session_id=session_id)
    ui.compacted(before, compacted)
    return compacted


async def handle(command, messages):
    if command == "/compact":
        return await compact(messages)
    if command == "/rewind":
        return rewind(messages)
    if command == "/sessions":
        return sessions(messages)
    ui.note("\n".join(f"{name}  -  {help}" for name, help in COMMANDS.items()))
    return messages
