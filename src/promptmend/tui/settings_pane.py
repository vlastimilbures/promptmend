"""The Settings tab: every setting and key in one keyboard list, saved through the
same services as `config set|unset` and `secrets set|remove`. Keys are shown only as set or
not set, never their value, and the persona only as set.

The list (#389) opens with Triggers: two lines saying what -i- and -ip- run, in Home's words
(home.trigger_row()), then the settings that decide it. The rarely changed settings sit
behind the Advanced fold, its last line; Enter or Space there shows or hides them. The fold
and the two lines are display only: no setting, no command. A filter searches every setting,
fold or not."""

from __future__ import annotations

from typing import ClassVar

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.geometry import Region
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import config_store, settings_service
from ..commands import common
from ..commands import doctor as doctor_cmd
from ..config import secret_names
from . import home, settings_model, teach
from .base import EXPECTED, Pane, error_message
from .modals import USE_DEFAULT, ConfirmModal, EditModal, Field, FormModal, PickModal, UseDefault
from .state import State

# The name column fits the longest setting (OPENROUTER_PRO_REASONING_EFFORT); the source
# column the longest file name (secrets.toml).
NAME_WIDTH = 31
SOURCE_WIDTH = 12
# A summary line's last column fits Home's longest key word (no key needed).
SUMMARY_WIDTH = 13
# The Advanced fold (#389): the list's last line, its option id, and its help.
FOLD = "fold:advanced"
FOLD_CLOSED = "▸ Advanced: {count} more settings"
FOLD_OPEN = "▾ Advanced (Enter hides these settings)"
# Short enough for one line at 70 columns.
FOLD_HELP = "Base URLs, token caps, efforts, temperatures, endpoint pins and more."
# The triggers the lines under Triggers summarise, with Home's row label for each.
SUMMARY = (("-i-", "Rewrites"), ("-ip-", "Pro (-ip-)"))
WITHHELD_CURRENT = (
    "The current value looks like a key, so it is not shown here. Saving an empty field "
    "keeps it; type a new value to replace it."
)


def setting_line(row: settings_model.Row, *, cursor: bool, accent: str | None = None) -> Table:
    """One row of the Settings list: the cursor, the dot, the name, the value (cut
    with … when narrow) and where it comes from. The name and the value always have the
    normal text colour; only the source of a value at its built-in default (`default`) is
    muted. The dot is a glyph as well as a colour, so it reads without colour too: a bold ●
    when the value is set by you (``row.custom``), in ``accent`` (the theme's accent colour),
    else a plain ○, neither bold nor dimmed. The cursor row is all in the cursor bar's own
    text colour, the ● and `default` too: neither theme's accent reads on its bar (orange on
    blue, magenta on yellow), nor a dimmed word."""
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(width=NAME_WIDTH, no_wrap=True, overflow="ellipsis")
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(width=SOURCE_WIDTH, no_wrap=True, overflow="ellipsis")
    mark = Text()
    mark.append("▶" if cursor else " ", style="bold")
    if row.custom:
        mark.append("●", style="bold" if cursor or not accent else f"bold {accent}")
    else:
        mark.append("○")
    source = Text(row.source, style="" if cursor or row.custom else "dim")
    grid.add_row(mark, Text(row.name), Text(row.value), source)
    return grid


def summary_line(trigger: str, row: home.HomeRow) -> Table:
    """One of the two lines under Triggers: what ``trigger`` runs, in Home's own words
    (provider · model, then the key's state: `key set`, `key not set`, `refused`, …, or
    Home's status word when it has none and is not ok). Not a
    setting: no cursor, no dot; indented under the heading and in italics, never bold, so it
    reads apart from the headings and from the setting rows."""
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(width=3, no_wrap=True)
    grid.add_column(width=4, no_wrap=True)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(width=SUMMARY_WIDTH, no_wrap=True, overflow="ellipsis")
    # A row that is not ok with no detail of its own (a provider no route knows) shows Home's
    # status word there, so it never reads as healthy.
    healthy = row.status in (home.OK, home.NOTE, home.WAIT)
    detail = row.detail or ("" if healthy else doctor_cmd.LABEL[row.status][0])
    grid.add_row("", *(Text(words, style="italic") for words in (trigger, row.text, detail)))
    return grid


def fold_line(*, opened: bool, cursor: bool) -> Text:
    """The Advanced fold's line: closed, how many settings it holds; open, how to close it.
    Its ▸ or ▾ sits in the dot's column, after the cursor's ▶."""
    words = FOLD_OPEN if opened else FOLD_CLOSED.format(count=settings_model.ADVANCED_COUNT)
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append("▶" if cursor else " ", style="bold")
    line.append(words)
    return line


def _matching(choices: tuple[str, ...], value: str) -> int | None:
    """The index of the choice ``value`` is (as typed, or in lower case), or None."""
    return next((n for n, c in enumerate(choices) if c in (value, value.lower())), None)


class SettingsList(OptionList):
    """The Settings tab's list. Its keys are bound here, so they act only while it has the
    focus: never under a dialog, in the filter or on another tab. Use default is `u` (as
    `config unset`; "Reset" names only the History tab's delete-everything), so `r` reloads
    here too."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("space", "switch", "Toggle"),
        Binding("enter", "select", "Edit"),
        Binding("u", "reset", "Use default"),
        Binding("slash", "filter", "Filter"),
    ]

    @property
    def pane(self) -> SettingsPane:
        return self.query_ancestor(SettingsPane)

    def action_page_up(self) -> None:
        """Page Up, landing on the first setting rather than on nothing when only a heading
        or a summary line is above it (Textual's own leaves no line highlighted)."""
        # Not super().action_page_up(): Textual's is unannotated, which strict mypy refuses.
        if self.highlighted is not None:
            self._move_page(-1)
        if self.highlighted is None:
            self.action_first()

    def scroll_to_highlight(self, top: bool = False) -> None:
        """Scroll the highlighted option into view together with the disabled lines right
        above it: its group's heading and, for the first setting, the Triggers heading and the
        two summary lines. They are never highlighted, so Textual's own scroll (the least
        needed) would leave them out of reach once scrolled away. ``top`` puts that block at
        the top of the view."""
        highlighted = self.highlighted
        if highlighted is None or not self.is_mounted:
            return
        start = highlighted
        while start > 0 and self.options[start - 1].disabled:
            start -= 1
        self._update_lines()
        try:
            first = self._index_to_line[start]
            end = self._index_to_line[highlighted] + self._heights[highlighted]
        except KeyError:
            return
        self.scroll_to_region(
            Region(0, first, self.scrollable_content_region.width, end - first),
            force=True,
            animate=False,
            top=top,
            immediate=True,
        )

    def action_switch(self) -> None:
        self.pane.toggle()

    def action_reset(self) -> None:
        self.pane.reset()

    def action_filter(self) -> None:
        self.pane.query_one(FilterInput).focus()


class FilterInput(Input):
    """The Settings tab's filter: Escape clears it and goes back to the list, as do Enter
    and Down (keeping it)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "clear", "Clear filter"),
        Binding("down", "leave", "List", show=False),
    ]

    def action_clear(self) -> None:
        self.value = ""
        self.action_leave()

    def action_leave(self) -> None:
        self.query_ancestor(SettingsPane).query_one(SettingsList).focus()


class SettingsPane(Pane):
    """Every setting and key in one keyboard list, grouped, each with a dot saying at
    a glance whether you set it; the rarely changed ones behind the Advanced fold. Saves go
    through `config set`'s service (save_setting()), a reset through `config unset`'s, keys
    through the Set key and Remove key dialogs; each change shows and logs its command."""

    # The filter's text, and the setting rows shown, by name.
    filter_text = ""
    rows: dict[str, settings_model.Row]
    # The setting the ▶ is on, or FOLD.
    cursor: str | None = None
    # Whether the Advanced fold is open: kept while the app runs (a save, a reload, another
    # tab), never saved; it starts closed.
    advanced = False
    # A change was saved and the State that shows it is still loading (see waiting()).
    pending = False

    def compose(self) -> ComposeResult:
        self.rows = {}
        with Horizontal(id="settings-top"):
            yield FilterInput(placeholder="/ filter", id="settings-filter")
            yield Static("", id="settings-count", markup=False)
        yield SettingsList(id="settings-list")
        yield Static("", id="settings-help", markup=False)
        yield self.result()

    def setup(self) -> None:
        # The filled dot is drawn in the theme's accent: a theme change redraws the rows.
        self.watch(self.app, "theme", self.fill, init=False)

    def accent(self) -> str | None:
        """The active theme's accent colour, for the filled dot."""
        return self.app.current_theme.accent

    def show(self, state: State) -> None:
        self.pending = False
        self.fill()

    def filtering(self) -> bool:
        """Whether the filter holds text: then every matching setting shows, fold or not."""
        return bool(self.filter_text.strip())

    def fill(self) -> None:
        """Rebuild the list from the State, the filter and the fold, keeping the cursor's
        setting (or the fold line). A setting the fold now hides puts the cursor on the fold
        line, which is where it went."""
        state = self.state
        if state is None:
            return
        listing = self.query_one(SettingsList)
        everything = settings_model.rows(state, self.filter_text)
        filtering = self.filtering()
        shown = [r for r in everything if filtering or self.advanced or not r.meta.advanced]
        if filtering:
            # No fold line under a filter: one run per group, basic rows first (a stable sort).
            shown.sort(key=lambda r: settings_model.GROUPS.index(r.meta.group))
        self.rows = {row.name: row for row in shown}
        # Without a filter, the fold line and the two lines under Triggers show.
        fold = not filtering
        keep: str | None
        if self.cursor in self.rows or (fold and self.cursor == FOLD):
            keep = self.cursor
        elif fold and self.cursor in settings_model.BY_NAME:
            keep = FOLD  # a setting the closed fold hides
        else:
            keep = next(iter(self.rows), None)
        accent = self.accent()
        options: list[Option] = []
        # A heading per run of one group. Its id names the part too: a group can head basic
        # rows and, under the fold, advanced ones.
        # Under a filter, ``shown`` is already in its order (one run per group).
        basic = [row for row in shown if filtering or not row.meta.advanced]
        behind = [row for row in shown if not filtering and row.meta.advanced]
        group: str | None = None
        for row in [*basic, *([None] if fold else []), *behind]:
            if row is None:  # the fold line, between the basic rows and the advanced ones
                fold_prompt = fold_line(opened=self.advanced, cursor=keep == FOLD)
                options.append(Option(fold_prompt, id=FOLD))
                group = None
                continue
            if row.meta.group != group:
                group = row.meta.group
                part = "advanced" if row.meta.advanced else "basic"
                heading = Text(group, style="bold")
                options.append(Option(heading, id=f"group:{part}:{group}", disabled=True))
                if fold and group == settings_model.GROUPS[0] and not row.meta.advanced:
                    options += self._summary(state)
            line = setting_line(row, cursor=row.name == keep, accent=accent)
            options.append(Option(line, id=row.name))
        listing.set_options(options)
        self.cursor = keep
        if keep is not None:
            listing.highlighted = listing.get_option_index(keep)
        if keep == FOLD and self.advanced:
            # On the open fold, its line goes to the top, with what it shows below it.
            listing.scroll_to_highlight(top=True)
        total = len(settings_model.SETTINGS)
        if filtering:
            count = f"{len(shown)} of {total}"
        elif self.advanced:
            count = f"{total} settings"
        else:
            count = f"{len(shown)} of {total} settings"
        self.query_one("#settings-count", Static).update(count)
        self.show_help()

    @staticmethod
    def _summary(state: State) -> list[Option]:
        """The two lines under Triggers, from Home's own rows (home.trigger_row()): never
        selectable, so the cursor skips them."""
        return [
            Option(
                summary_line(trigger, home.trigger_row(state, label, trigger)),
                id=f"summary:{trigger}",
                disabled=True,
            )
            for trigger, label in SUMMARY
        ]

    def line(self, option_id: str, *, cursor: bool) -> Table | Text:
        """The prompt of a line the cursor can be on: a setting or the fold."""
        if option_id == FOLD:
            return fold_line(opened=self.advanced, cursor=cursor)
        return setting_line(self.rows[option_id], cursor=cursor, accent=self.accent())

    def toggle_fold(self) -> None:
        """Enter or Space on the fold line: show or hide the advanced settings, the cursor
        staying on it; fill() puts the open fold's line at the top, so what it shows is in
        view."""
        self.advanced = not self.advanced
        self.fill()

    def show_help(self) -> None:
        """The cursor row's help, and where its value comes from with the command that sets
        it in a terminal; on the fold line, what is behind it."""
        row = self.rows.get(self.cursor or "")
        if self.cursor == FOLD:
            text = Text(no_wrap=True, overflow="ellipsis")
            text.append(f"{FOLD_HELP}\n")
            verb = "hides" if self.advanced else "shows"
            text.append(f"Rarely changed settings; Enter or Space {verb} them.", style="dim")
        elif row is None:
            text = Text(f"No setting matches {self.filter_text!r}; Escape there clears it.")
        else:
            kind = row.meta.kind
            command = teach.for_setting(
                "set-key" if kind == settings_model.KEY else "set", row.name
            ).removeprefix(f"{teach.PROGRAM} ")
            where = f"From {row.where}" if row.where else "Not set"
            text = Text(no_wrap=True, overflow="ellipsis")
            text.append(f"{row.meta.help}\n")
            text.append(f"{where} · {command}", style="dim")
        self.query_one("#settings-help", Static).update(text)

    @on(OptionList.OptionHighlighted, "#settings-list")
    def _moved(self, event: OptionList.OptionHighlighted) -> None:
        name = event.option.id
        if name is None or not self._selectable(name) or name == self.cursor:
            return
        listing = self.query_one(SettingsList)
        old = self.cursor
        if old is not None and self._selectable(old):
            listing.replace_option_prompt(old, self.line(old, cursor=False))
        listing.replace_option_prompt(name, self.line(name, cursor=True))
        self.cursor = name
        self.show_help()

    def _selectable(self, option_id: str) -> bool:
        """Whether the cursor can be on ``option_id`` now: a shown setting or the fold."""
        return option_id in self.rows or (option_id == FOLD and not self.filtering())

    @on(Input.Changed, "#settings-filter")
    def _filtered(self, event: Input.Changed) -> None:
        self.filter_text = event.value
        self.fill()

    @on(Input.Submitted, "#settings-filter")
    def _filter_done(self) -> None:
        self.query_one(SettingsList).focus()

    def current(self) -> settings_model.Row | None:
        return self.rows.get(self.cursor or "")

    def waiting(self) -> bool:
        """Whether a change is saved but not yet read back. The rows show the state before
        it, so an action now would act on a stale value (a second Space saving the same
        value again): it is refused, visibly, until show() has the fresh state."""
        if self.pending:
            self.app.notify("Still reading back the last change; try again.", markup=False)
        return self.pending

    def toggle(self) -> None:
        """Space: switch a bool and save it at once; on the fold line, open or close it."""
        if self.cursor == FOLD:
            self.toggle_fold()
            return
        row = self.current()
        if self.waiting():
            return
        if row is not None and row.meta.kind == settings_model.BOOL:
            self.save(row.name, "false" if row.switched_on else "true")

    @on(OptionList.OptionSelected, "#settings-list")
    def _selected(self) -> None:
        self.edit()

    def edit(self) -> None:
        """Enter: a key's Set key dialog, a switch toggled, a choice picked from its list, or
        a value typed in a field prefilled with the current one; on the fold line, open or
        close it."""
        if self.cursor == FOLD:
            self.toggle_fold()
            return
        row, state = self.current(), self.state
        if row is None or state is None or self.waiting():
            return
        meta, entry = row.meta, state.layers.entries[row.name]
        if meta.kind == settings_model.KEY:
            self.set_key(row.name)
        elif meta.kind == settings_model.BOOL:
            self.toggle()
        elif meta.kind == settings_model.CHOICE:
            self._pick(meta, entry.value)
        else:
            self._type(meta, entry.value)

    def _pick(self, meta: settings_model.Meta, current: str) -> None:
        choices = settings_model.choices(meta, self.state)
        now = _matching(choices, current)
        default = _matching(choices, settings_model.default(meta.name))

        def label(n: int, choice: str) -> str:
            marks = ", ".join(m for m, at in (("current", now), ("default", default)) if n == at)
            return f"{choice or meta.empty}  ({marks})" if marks else choice or meta.empty

        labels = [(label(n, c), c) for n, c in enumerate(choices)]

        def picked(value: str | UseDefault | None) -> None:
            if value is USE_DEFAULT:
                self.reset()
            elif value is not None:
                self.save(meta.name, value)

        preview = f"{meta.help}\nEnter picks · Escape cancels"
        self.app.push_screen(
            PickModal(meta.name, labels, preview, selected=now, use_default=True), picked
        )

    def _type(self, meta: settings_model.Meta, current: str) -> None:
        name = meta.name
        # A value that looks like a key is never shown, not even here: the field opens empty,
        # the dialog says why, and an empty submit then keeps the value (it is a cancel).
        withheld = bool(current) and settings_service.key_like(name, current)
        value = "" if withheld else current

        def check(typed: str) -> str | None:
            """`config set`'s own checks (settings_service.check_setting()), as typed."""
            try:
                settings_service.check_setting(name, typed)
            except (settings_service.ServiceError, ValueError) as exc:
                return str(exc)
            return None

        default = settings_model.default(name) or meta.empty
        preview = f"{meta.help}\nDefault: {default}"
        if withheld:
            preview += f"\n{WITHHELD_CURRENT}"

        def typed(new: str | UseDefault | None) -> None:
            if new is USE_DEFAULT:
                self.reset()
            elif new is not None and not (withheld and new == ""):
                self.save(name, new)

        self.app.push_screen(EditModal(name, value, check, preview, use_default=True), typed)

    def save(self, name: str, value: str) -> None:
        """Save through `config set`'s service (Pane.save_setting()); the command is logged (a
        private or key-like value withheld)."""

        def saved() -> None:
            self.pending = True  # until show() reads it back

        self.save_setting(name, value, saved)

    def reset(self) -> None:
        """u (or Use default in a setting's dialog): a key's Remove key dialog, or a setting
        back to its default (`config unset`)."""
        row = self.current()
        if row is None or self.waiting():
            return
        name = row.name
        if row.meta.kind == settings_model.KEY:
            self.remove_key(name)
            return

        def unset() -> str:
            removed, path = settings_service.unset_setting(name)
            self.pending = removed  # until show() reads it back
            if not removed:
                elsewhere = f" It comes from {row.where}." if row.custom else ""
                return f"{name} is not saved in {path}; nothing to do.{elsewhere}"
            notes = settings_service.after_save(name)
            return "; ".join([f"{name} removed from {path}; its default applies", *notes])

        self.attempt(unset, command=teach.for_setting("reset", name))

    def set_key(self, name: str | None = None) -> None:
        """The hidden Set key dialog, with ``name`` (a key's name) picked (`secrets set`)."""
        fields = [
            Field("key-name", "Key", secret_names(), value=name or ""),
            Field("key-value", "Value (hidden as you type; never shown again)", secret=True),
        ]
        # The cursor starts in the value: Enter on the empty field changes nothing, and a
        # saved key is replaced only by a value typed there (as `secrets set` does). The key
        # picker has the focus only for a call without a name (the interface always gives one).
        focus = "key-value" if name else "key-name"
        self.app.push_screen(
            FormModal("Save an API key in the secrets file", fields, focus=focus), self._save_key
        )

    def _save_key(self, values: dict[str, str] | None) -> None:
        if values is None:
            return
        name, value = values["key-name"], values["key-value"].strip()
        if not value:
            self.report(f"no value entered; {name} was not changed", error=True)
            return

        def save() -> str:
            saved = settings_service.store_key(name, value)
            return "; ".join([saved, *settings_service.after_save(name)])

        self.attempt(save, command=teach.for_setting("set-key", name))

    def remove_key(self, name: str | None = None) -> None:
        """Pick a saved key (``name`` first, when saved), then confirm (`secrets remove`)."""
        try:
            settings_service.refuse_in_legacy_mode("the secrets file")
            saved = config_store.saved_secret_names()
        except EXPECTED as exc:
            self.report(error_message(exc), error=True)
            return
        if not saved:
            self.report("The secrets file holds no key; nothing to do.")
            return
        self.app.push_screen(
            FormModal(
                "Remove a key",
                [Field("key-name", "Key", saved, value=name or "")],
                submit="Remove…",
            ),
            self._confirm_remove,
        )

    def _confirm_remove(self, values: dict[str, str] | None) -> None:
        if values is None:
            return
        name = values["key-name"]

        def done(yes: bool | None) -> None:
            if yes:
                self.attempt(
                    lambda: self._delete(name),
                    command=teach.for_setting("remove-key", name),
                )

        self.app.push_screen(
            ConfirmModal(f"Delete {name} from the secrets file?", confirm="Delete"), done
        )

    def _delete(self, name: str) -> str:
        config_store.delete_secret(name)
        layers, _ = settings_service.load_layers()
        entry = layers.entries[name]
        if entry.value:
            still = common.source_label(entry.source)
            return f"{name} removed from the secrets file; it is still set, from {still}."
        return f"{name} removed from the secrets file."
