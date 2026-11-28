"""The Home tab: the headline, one row per part and the session log. The
rows come from home.py; nothing here calls a provider.

The rows are a keyboard list: Enter (or a click) on a row does what its pointer says,
opening the tab it names. One button opens the headline's fix (home.fix()): the Triggers
tab's deploy preview, the Settings tab's Set key dialog or the tab the row names, through
the same pane method as that tab's own button. The list has the focus while Home is shown;
leaving Home gives it to the tab bar, so no key of the list acts unseen."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.widgets import Button, OptionList, Static, TabbedContent, Tabs
from textual.widgets.option_list import Option

from .. import doctor
from ..commands import doctor as doctor_cmd
from . import teach
from .base import Pane, button_row
from .home import (
    CHECK_AGAIN,
    DEPLOY,
    HOW_TO_UPDATE,
    NOTE,
    OPEN,
    SET_KEY,
    TAB_LABELS,
    WAIT,
    HomeRow,
    fix,
    headline,
    home_rows,
)
from .state import State

if TYPE_CHECKING:
    from .settings_pane import SettingsPane
    from .triggers_pane import TriggersPane


def _jump(tab: str | None) -> str:
    return f"-> {TAB_LABELS[tab]}" if tab else ""


# Home's word and colour for a newer release: its own, apart from doctor's four, so the
# word says it in either theme and without colour.
_NOTE_LABEL = ("new", "bold magenta")
# A row still being checked: muted; its text says `checking`.
_WAIT_LABEL = ("…", "dim")
_OWN_LABELS = {NOTE: _NOTE_LABEL, WAIT: _WAIT_LABEL}


def _side(row: HomeRow) -> str:
    """The right-hand column: the detail and, for a row that is not ok, the tab to go to."""
    side = [row.detail] if row.detail else []
    if row.status not in (doctor.OK, NOTE, WAIT):
        side.append(_jump(row.jump))
    return "  ".join(p for p in side if p)


def home_lines(rows: Sequence[HomeRow], cursor: str | None = None) -> list[Table]:
    """Home's rows, one grid each, their columns as wide across the rows as one table's: the
    status word (coloured, but the word says it), the bold label, the text (cut with … when
    the terminal is narrow, never wrapped) and, right-aligned, the detail and, for a row that
    is not ok, the tab to go to. The ``cursor`` row (by label) is all in the highlight's own
    text colour, its status word too: no status colour reads on either theme's bar (as the
    Settings list's cursor row, #388)."""
    label_width = max((len(row.label) for row in rows), default=0)
    side_width = max((len(_side(row)) for row in rows), default=0)
    lines = []
    for row in rows:
        word, color = _OWN_LABELS.get(row.status) or doctor_cmd.LABEL[row.status]
        if row.label == cursor:
            color = ""
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(width=4, no_wrap=True)
        grid.add_column(width=label_width, no_wrap=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(width=side_width, no_wrap=True, justify="right")
        grid.add_row(
            Text(word, style=color),
            Text(row.label, style="bold"),
            Text(row.text),
            Text(_side(row)),
        )
        lines.append(grid)
    return lines


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
    """Home's read-only session log: what each action of this session was in a
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


# The fix button's ids, one per home.fix() action: one shows at a time, none while no row is
# a problem (ready, or only rows still checking). Each has its own tooltip (tui/teach.py).
FIX_BUTTONS = {DEPLOY: "home-deploy", SET_KEY: "home-set-key", OPEN: "home-open"}


class HomePane(Pane):
    """This install at a glance: a headline naming the worst problem, then one row per part.
    The Checks tab has every check; nothing here calls a provider."""

    rows: tuple[HomeRow, ...] = ()
    # The deploy preview reads the plan, as the Triggers tab's own Deploy or repair does.
    FULL_ONLY = (FIX_BUTTONS[DEPLOY],)

    def compose(self) -> ComposeResult:
        yield Static("Checking…", markup=False, id="home-headline")
        yield OptionList(id="home-rows")
        yield button_row(
            *((id_, "") for id_ in FIX_BUTTONS.values()),
            ("home-setup", "Setup…"),
            ("home-reload", "Check again"),
            ("home-copy", "Copy last"),
        )
        yield self.result()
        yield Static(session_text(()), id="home-session")

    def show(self, state: State) -> None:
        """Each stage of a reload redraws the rows: the highlight stays on its row (by its
        label) and the focus where it is."""
        self.rows = tuple(home_rows(state))
        self.query_one("#home-headline", Static).update(home_headline(self.rows))
        listing = self.query_one("#home-rows", OptionList)
        index = listing.highlighted
        keep = listing.options[index].id if index is not None else None
        labels = [row.label for row in self.rows]
        if keep not in labels:
            keep = labels[0] if labels else None
        listing.set_options(
            Option(line, id=label)
            for label, line in zip(labels, home_lines(self.rows, keep), strict=True)
        )
        if keep is not None:
            listing.highlighted = labels.index(keep)
        self.show_fix()

    @on(OptionList.OptionHighlighted, "#home-rows")
    def _moved(self, event: OptionList.OptionHighlighted) -> None:
        """The highlight moved: the row under it is redrawn in the highlight's colour, the
        one it left in its status colour again."""
        listing = self.query_one("#home-rows", OptionList)
        lines = home_lines(self.rows, event.option.id)
        for row, line in zip(self.rows, lines, strict=True):
            listing.replace_option_prompt(row.label, line)

    def show_fix(self) -> None:
        """The one button that opens the headline's fix, labelled for it; none while no row
        is a problem. On the quick State a problem it already knows (a missing key) has its
        button at once; Deploy or repair stays disabled until the full State (FULL_ONLY)."""
        step = fix(list(self.rows))
        for action, id_ in FIX_BUTTONS.items():
            button = self.query_one(f"#{id_}", Button)
            shown = step is not None and step.action == action
            button.display = shown
            if step is not None and shown:
                button.label = step.label

    def show_update(self, state: State) -> None:
        # Only the Version row and the headline can change; the redraw keeps the highlight.
        self.show(state)

    def setup(self) -> None:
        self.query_one("#home-copy", Button).disabled = not self.manage.session
        self.show_fix()
        self.call_after_refresh(self._focus_list)
        self.watch(self.query_ancestor(TabbedContent), "active", self._tab_changed, init=False)

    def _tab_changed(self, tab: str) -> None:
        """Called synchronously as the active tab changes, before the switch is drawn or any
        handler runs. Home left with the focus in it: the tab bar takes the focus now, never
        deferred. A Home widget that kept it would be blurred as Home hides, and Textual
        hands the focus to a sibling, still in Home: focus inside a tab pane makes that tab
        active again, which pulled Home back (#395 review). Home shown: its list takes the
        focus after the refresh, once it is displayed, unless another tab is active by
        then."""
        if tab == "home":
            self.call_after_refresh(self._focus_list)
            return
        screen = self.screen
        if screen.focused is not None and self in screen.focused.ancestors:
            screen.set_focus(self.query_ancestor(TabbedContent).query_one(Tabs))

    def _focus_list(self) -> None:
        """Focus the rows, only while Home is still the active tab: a deferred focus never
        acts for a tab that is no longer shown (it would make Home active again)."""
        if self.query_ancestor(TabbedContent).active == "home":
            self.query_one("#home-rows", OptionList).focus()

    @on(OptionList.OptionSelected, "#home-rows")
    async def _row_picked(self, event: OptionList.OptionSelected) -> None:
        """Enter or a click on a row does what its pointer says: open the tab it names, read
        everything again (`not checked`, r) or show About (a newer release, a)."""
        row = next((r for r in self.rows if r.label == event.option.id), None)
        if row is None:
            return
        if row.jump:
            await self.manage.main.action_show(row.jump)
        elif row.detail == CHECK_AGAIN:
            self.manage.reload()
        elif row.detail == HOW_TO_UPDATE:
            self.manage.action_about()

    @on(Button.Pressed, "#home-deploy")
    async def _fix_deploy(self) -> None:
        """The Triggers tab's deploy preview, as its own Deploy or repair button opens it."""
        step = fix(list(self.rows))
        if step is None or step.action != DEPLOY:
            return
        main = self.manage.main
        pane = await main.build("triggers")
        await main.action_show("triggers")
        cast("TriggersPane", pane).preview_deploy()

    @on(Button.Pressed, "#home-set-key")
    async def _fix_key(self) -> None:
        """The Settings tab's Set key dialog for the missing key, as Enter on that key."""
        step = fix(list(self.rows))
        if step is None or step.action != SET_KEY:
            return
        main = self.manage.main
        pane = await main.build("settings")
        await main.action_show("settings")
        cast("SettingsPane", pane).set_key(step.key)

    @on(Button.Pressed, "#home-open")
    async def _fix_open(self) -> None:
        step = fix(list(self.rows))
        if step is not None and step.action == OPEN:
            await self.manage.main.action_show(step.tab)

    def show_session(self, entries: Sequence[teach.Entry]) -> None:
        self.query_one("#home-session", Static).update(session_text(entries))
        self.query_one("#home-copy", Button).disabled = not entries

    @on(Button.Pressed, "#home-reload")
    def _reload(self) -> None:
        self.manage.reload()

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
