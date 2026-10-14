"""The Home tab (Mockup B, #112): the headline, one row per part and the session log. The
rows come from home.py; nothing here calls a provider."""

from __future__ import annotations

from collections.abc import Sequence

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.widgets import Button, Static

from .. import doctor
from ..commands import doctor as doctor_cmd
from . import teach
from .base import Pane, button_row
from .home import NOTE, TAB_LABELS, HomeRow, headline, home_rows
from .state import State


def _jump(tab: str | None) -> str:
    return f"-> {TAB_LABELS[tab]}" if tab else ""


# Home's word and colour for a newer release (#197): its own, apart from doctor's four, so the
# word says it in either theme and without colour.
_NOTE_LABEL = ("new", "bold magenta")


def home_table(rows: Sequence[HomeRow]) -> Table:
    """Home's rows as a grid: the status word (coloured, but the word says it), the label, the
    text (cut with … when the terminal is narrow), the detail and, for a row that is not ok,
    the tab to go to."""
    grid = Table.grid(padding=(0, 2), expand=True)
    grid.add_column(width=4, no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(no_wrap=True, justify="right")
    for row in rows:
        label, color = _NOTE_LABEL if row.status == NOTE else doctor_cmd.LABEL[row.status]
        side = [row.detail] if row.detail else []
        if row.status not in (doctor.OK, NOTE):
            side.append(_jump(row.jump))
        grid.add_row(
            Text(label, style=color),
            Text(row.label, style="bold"),
            Text(row.text),
            Text("  ".join(p for p in side if p)),
        )
    return grid


def home_headline(rows: Sequence[HomeRow]) -> Table:
    text, jump = headline(list(rows))
    grid = Table.grid(padding=(0, 2), expand=True)
    grid.add_column(ratio=1)
    grid.add_column(no_wrap=True, justify="right")
    grid.add_row(Text(text, style="bold"), Text(_jump(jump)))
    return grid


# The session log shows this many of the latest actions.
SESSION_LINES = 6


def session_text(entries: Sequence[teach.Entry]) -> Text:
    """Home's read-only session log (#111): what each action of this session was in a
    terminal, newest last, with the first line of its result. Before any, a few recipes."""
    if not entries:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("In a terminal, try:\n", style="bold")
        width = max(len(teach.equivalent(*r.argv)) for r in teach.RECIPES)
        for recipe in teach.RECIPES:
            text.append(f"  $ {teach.equivalent(*recipe.argv):<{width}}", style="dim")
            text.append(f"  {recipe.what}\n")
        text.append("Each button's tooltip shows its command too.", style="dim")
        return text
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("This session, as commands:\n", style="bold")
    for entry in entries[-SESSION_LINES:]:
        text.append(f"  $ {entry.command}\n", style="dim")
        mark = "error: " if entry.error and not entry.summary.startswith("error") else ""
        text.append(f"    {mark}{entry.summary}\n")
    return text


class HomePane(Pane):
    """This install at a glance: a headline naming the worst problem, then one row per part
    (Mockup B, #112). The Diagnostics tab has every check; nothing here calls a provider."""

    rows: tuple[HomeRow, ...] = ()

    def compose(self) -> ComposeResult:
        yield Static("Checking…", markup=False, id="home-headline")
        yield Static("", id="home-rows")
        yield button_row(
            ("home-setup", "Setup…"),
            ("home-reload", "Check again"),
            ("home-previous", "Previous install…"),
            ("home-copy", "Copy last"),
        )
        yield self.result()
        yield Static(session_text(()), id="home-session")

    def show(self, state: State) -> None:
        self.rows = tuple(home_rows(state))
        self.query_one("#home-headline", Static).update(home_headline(self.rows))
        self.query_one("#home-rows", Static).update(home_table(self.rows))
        # While a previous install could still be copied (not gated: "Enter a path…" finds
        # one detection missed or the user skipped, #237), or there is one to show (#222).
        found = state.previous
        shown = not found.gated or found.candidates or found.pending or state.previous_error
        self.query_one("#home-previous", Button).display = bool(shown)

    def setup(self) -> None:
        self.query_one("#home-copy", Button).disabled = not self.manage.session
        self.query_one("#home-previous", Button).display = False

    def show_session(self, entries: Sequence[teach.Entry]) -> None:
        self.query_one("#home-session", Static).update(session_text(entries))
        self.query_one("#home-copy", Button).disabled = not entries

    @on(Button.Pressed, "#home-reload")
    def _reload(self) -> None:
        self.manage.reload()

    @on(Button.Pressed, "#home-previous")
    def _previous(self) -> None:
        self.manage.open_previous()

    @on(Button.Pressed, "#home-setup")
    def _setup(self) -> None:
        self.manage.open_setup()

    @on(Button.Pressed, "#home-copy")
    def _copy(self) -> None:
        """Send this session's latest command, as the session log shows it (withheld values
        and placeholders included), to the terminal's clipboard (Textual's OSC 52): written
        only, the clipboard is never read. The terminal may drop it, so the report says sent."""
        if not self.manage.session:
            return
        command = self.manage.session[-1].command
        self.app.copy_to_clipboard(command)
        self.report(f"Sent to the terminal clipboard (OSC 52): {command}")
