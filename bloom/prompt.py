"""The input line.

`input()` cannot edit a line that has wrapped past the screen width - the
terminal owns the wrapping and readline cannot see it. prompt_toolkit redraws
the line itself, so deleting, word jumps and history all keep working once the
text is longer than the screen.
"""

import asyncio
import os
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

bindings = KeyBindings()


@bindings.add("escape", "left")
def _word_left(event):
    document = event.current_buffer.document
    event.current_buffer.cursor_position += (
        document.find_previous_word_beginning(count=1) or 0
    )


@bindings.add("escape", "right")
def _word_right(event):
    document = event.current_buffer.document
    event.current_buffer.cursor_position += (
        document.find_next_word_ending(count=1) or 0
    )


@bindings.add("escape", "enter")
def _newline(event):
    """Option-enter starts a new line instead of sending the message."""
    event.current_buffer.insert_text("\n")


def get_base_dir() -> Path:
    val = os.environ.get("BLOOM_HOME", "") or os.environ.get("BLOOM_ROOT", "")
    if val and val.strip():
        return Path(val).resolve()
    return Path(__file__).resolve().parent.parent


STYLE = Style.from_dict({"prompt": "bold #9ece6a"})
SESSION = None


def get_session():
    global SESSION
    if SESSION is None:
        base_dir = get_base_dir()
        history_path = base_dir / ".history"
        try:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            hist = FileHistory(str(history_path))
        except OSError:
            from prompt_toolkit.history import InMemoryHistory
            hist = InMemoryHistory()

        SESSION = PromptSession(
            history=hist,
            key_bindings=bindings,
            style=STYLE,
        )
    return SESSION


async def read(prompt="> "):
    """Read one message asynchronously."""
    session = get_session()
    return await session.prompt_async(HTML(f"<prompt>{prompt}</prompt>"))


def read_sync(prompt="> "):
    """Read one message synchronously."""
    try:
        if sys.stdin.isatty():
            session = get_session()
            try:
                loop = asyncio.get_running_loop()
                in_thread = loop.is_running()
            except RuntimeError:
                in_thread = False
            return session.prompt(HTML(f"<prompt>{prompt}</prompt>"), in_thread=in_thread)
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        return input(prompt)
