"""Terminal presentation layer.

Knows nothing about LLMs, providers or tools - it only receives plain strings
and dicts and decides how they look.
"""

import json
from contextlib import contextmanager

from rich.console import Console, Group
from rich.json import JSON
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from . import prompt

ACCENT = "#7aa2f7"
USER = "#9ece6a"
TOOL = "#e0af68"
MUTED = "#565f89"

MAX_TOOL_OUTPUT_LINES = 12


class UI:
    def __init__(self):
        self.console = Console()
        self._totals = {}

    # ---------------------------------------------------------------- input

    def banner(self):
        self.console.print()
        self.console.print(
            Rule(Text(" bloom harness ", style=f"bold {ACCENT}"), style=MUTED)
        )
        self.console.print(
            Padding(Text("opt-enter for a newline  ·  ctrl-d to exit", style=MUTED), (0, 0, 0, 2))
        )

    def clear(self):
        self.console.clear()

    def resumed(self, messages, label="resumed"):
        turns = sum(1 for m in messages if m["role"] == "user")
        self.console.print(
            Padding(
                Text(f"{label} · {len(messages)} messages · {turns} turns", style=MUTED),
                (0, 0, 0, 2),
            )
        )

    def replay(self, messages):
        """Redraw a loaded transcript so the screen matches the history."""
        results = {
            m["tool_call_id"]: m.get("content", "")
            for m in messages
            if m.get("role") == "tool" and "tool_call_id" in m
        }
        for message in messages:
            if message["role"] == "user":
                self.user(message["content"])
            elif message["role"] == "assistant":
                if message.get("content"):
                    self.agent(message["content"])
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function")
                    if not isinstance(fn, dict):
                        continue
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                    except Exception:
                        args = {"raw": raw_args}
                    self.tool(
                        fn.get("name", "unknown"),
                        args,
                        results.get(call.get("id"), ""),
                    )

    def approve(self, reason):
        self.console.print(Padding(Text(reason, style=f"bold {TOOL}"), (1, 0, 0, 2)))
        try:
            answer = prompt.read_sync("  allow? (y/n)> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")

    def note(self, text):
        self.console.print(Padding(Text(text, style=MUTED), (1, 0, 0, 2)))

    def pick(self, title, rows):
        """Numbered list; returns the chosen index or None."""
        self.console.print(Padding(Text(title, style=f"bold {ACCENT}"), (1, 0, 0, 2)))
        for i, row in enumerate(rows):
            self.console.print(Padding(Text(f"{i:>3}  {row}", style=MUTED), (0, 0, 0, 2)))
        try:
            answer = prompt.read_sync("\n  number> ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return int(answer) if answer.isdigit() and int(answer) < len(rows) else None

    async def ask(self):
        self.console.print()
        try:
            val = await prompt.read("> ")
            return val.strip()
        except EOFError:
            self.console.print()
            return None
        except KeyboardInterrupt:
            self.console.print()
            return ""

    # --------------------------------------------------------------- output

    def user(self, text):
        self.console.print(
            Padding(Text(text.strip(), style=f"bold {USER}"), (1, 0, 0, 2))
        )

    def agent(self, text):
        self.console.print(
            Padding(
                Group(
                    Text("agent", style=f"bold {ACCENT}"),
                    Padding(Markdown(text.strip()), (1, 0, 0, 0)),
                ),
                (1, 2, 0, 2),
            )
        )

    def tool(self, name, args, result, nested=False):
        header = Text.assemble(
            (f"{name} ", f"bold {TOOL}"),
            (self._format_args(args), MUTED),
        )
        self.console.print(
            Padding(
                Panel(
                    Group(header, Rule(style=MUTED), self._format_result(result)),
                    border_style=MUTED,
                    padding=(0, 1),
                ),
                (1, 2, 0, 6 if nested else 2),
            )
        )

    def debug(self, data):
        self.console.print(
            Padding(
                Panel(
                    JSON.from_data(data),
                    title=Text("raw response", style=f"italic {MUTED}"),
                    title_align="left",
                    border_style=TOOL,
                    padding=(0, 1),
                ),
                (1, 2, 0, 2),
            )
        )

    @contextmanager
    def working(self, label="thinking"):
        with self.console.status(
            Text(label, style=MUTED), spinner="dots", spinner_style=ACCENT
        ):
            yield

    # ---------------------------------------------------------------- usage

    def usage(self, stats):
        for key, value in stats.items():
            self._totals[key] = self._totals.get(key, 0) + (value or 0)

        parts = " · ".join(
            f"{value:,} {key.replace('_tokens', '')}"
            for key, value in stats.items()
            if value
        )
        self.console.print(Padding(Text(parts, style=MUTED), (1, 0, 0, 2)))

    def summary(self):
        if not self._totals:
            return

        table = Table.grid(padding=(0, 2))
        table.add_column(style=MUTED)
        table.add_column(style=f"bold {ACCENT}", justify="right")
        for key, value in self._totals.items():
            table.add_row(key.replace("_", " "), f"{value:,}")

        self.console.print(Padding(table, (1, 2)))
        self.console.print(Rule(style=MUTED))
        self.console.print()

    def compacted(self, before, messages):
        summary = next(
            (m["content"] for m in messages if "<summary>" in (m.get("content") or "")),
            "",
        )
        self.console.print(
            Padding(
                Panel(
                    Markdown(summary.replace("<summary>", "").replace("</summary>", "")),
                    title=Text(
                        f"compacted · {before} → {len(messages)} messages",
                        style=f"bold {TOOL}",
                    ),
                    title_align="left",
                    border_style=TOOL,
                    padding=(0, 1),
                ),
                (1, 2, 0, 2),
            )
        )

    # -------------------------------------------------------------- helpers

    def _format_args(self, args):
        if len(args) == 1:
            return str(next(iter(args.values())))
        return json.dumps(args)

    def _format_result(self, result):
        lines = result.strip().splitlines() or ["(no output)"]
        shown = lines[:MAX_TOOL_OUTPUT_LINES]
        body = Text("\n".join(shown), style=MUTED)
        hidden = len(lines) - len(shown)
        if hidden > 0:
            body.append(f"\n… {hidden} more lines", style=f"italic {TOOL}")
        return body


ui = UI()
