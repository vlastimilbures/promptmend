"""The Settings tab (#199): every setting and key in one keyboard list, saved through the
same services as `config set|unset` and `secrets set|remove`. Keys are shown only as set or
not set, never their value, and the persona only as set."""

from __future__ import annotations

from typing import ClassVar

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import config_store, smoke
from ..commands import common
from ..commands import settings as settings_cmd
from ..config import secret_names
from ..console import shown_arg
from ..factory import PROVIDER_NAMES
from . import settings_model, teach
from .base import EXPECTED, Pane, button_row, confirm_migration
from .modals import ConfirmModal, EditModal, Field, FormModal, PickModal
from .state import State

# The name column fits the longest setting (OPENROUTER_PRO_REASONING_EFFORT); the source
# column the longest file name (secrets.toml).
NAME_WIDTH = 31
SOURCE_WIDTH = 12
WITHHELD_CURRENT = (
    "The current value looks like a key, so it is not shown here. Saving an empty field "
    "keeps it; type a new value to replace it."
)


def setting_line(row: settings_model.Row, *, cursor: bool) -> Table:
    """One row of the Settings list (#199): the cursor, the dot, the name, the value (cut
    with … when narrow) and where it comes from. The dot is a glyph as well as a colour, so
    it reads without colour too: a green ● for a switch on, a key set or a value that is not
    the default; a grey ○ and a dimmed row otherwise."""
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(width=NAME_WIDTH, no_wrap=True, overflow="ellipsis")
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(width=SOURCE_WIDTH, no_wrap=True, overflow="ellipsis")
    mark = Text("▶" if cursor else " ", style="bold")
    mark.append("●" if row.on else "○", style="bold green" if row.on else "dim")
    dim = "" if row.on else "dim"
    grid.add_row(
        mark, Text(row.name, style=dim), Text(row.value, style=dim), Text(row.source, style=dim)
    )
    return grid


class SettingsList(OptionList):
    """The Settings tab's list. Its keys are bound here, so they act only while it has the
    focus: never under a dialog, in the filter or on another tab. Reset is `u` (as `config
    unset`), so `r` reloads here too (#215)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("space", "switch", "Toggle"),
        Binding("enter", "select", "Edit"),
        Binding("u", "reset", "Reset"),
        Binding("slash", "filter", "Filter"),
    ]

    @property
    def pane(self) -> SettingsPane:
        return self.query_ancestor(SettingsPane)

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
    """Every setting and key in one keyboard list (#199), grouped, each with a dot saying at
    a glance what is on. Saves go through `config set`'s service (save_setting()), a reset
    through `config unset`'s, keys through the Set key and Remove key dialogs; each change
    shows and logs its command."""

    # The filter's text, and the rows it shows by name.
    filter_text = ""
    rows: dict[str, settings_model.Row]
    # The row the ▶ is on.
    cursor: str | None = None
    # A change was saved and the State that shows it is still loading (see waiting()).
    pending = False

    def compose(self) -> ComposeResult:
        self.rows = {}
        with Horizontal(id="settings-top"):
            yield FilterInput(placeholder="/ filter", id="settings-filter")
            yield Static("", id="settings-count", markup=False)
        yield SettingsList(id="settings-list")
        yield Static("", id="settings-help", markup=False)
        yield button_row(("migrate-env", "Migrate .env"), ("smoke", "Test call (local stub)"))
        yield self.result()

    def show(self, state: State) -> None:
        self.pending = False
        self.fill()

    def fill(self) -> None:
        """Rebuild the list from the State and the filter, keeping the cursor's setting."""
        state = self.state
        if state is None:
            return
        listing = self.query_one(SettingsList)
        found = settings_model.rows(state, self.filter_text)
        self.rows = {row.name: row for row in found}
        keep = self.cursor if self.cursor in self.rows else next(iter(self.rows), None)
        options: list[Option] = []
        group = None
        for row in found:
            if row.meta.group != group:
                group = row.meta.group
                options.append(
                    Option(Text(group, style="bold"), id=f"group:{group}", disabled=True)
                )
            options.append(Option(setting_line(row, cursor=row.name == keep), id=row.name))
        listing.set_options(options)
        self.cursor = keep
        if keep is not None:
            listing.highlighted = listing.get_option_index(keep)
        total = len(settings_model.SETTINGS)
        count = f"{total} settings" if not self.filter_text else f"{len(found)} of {total}"
        self.query_one("#settings-count", Static).update(count)
        self.show_help()

    def show_help(self) -> None:
        """The cursor row's help, and where its value comes from with the command that sets
        it in a terminal."""
        row = self.rows.get(self.cursor or "")
        if row is None:
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
        if name is None or name not in self.rows or name == self.cursor:
            return
        listing = self.query_one(SettingsList)
        if self.cursor in self.rows:
            old = self.cursor or ""
            listing.replace_option_prompt(old, setting_line(self.rows[old], cursor=False))
        listing.replace_option_prompt(name, setting_line(self.rows[name], cursor=True))
        self.cursor = name
        self.show_help()

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
        """Space: switch a bool and save it at once."""
        row = self.current()
        if self.waiting():
            return
        if row is not None and row.meta.kind == settings_model.BOOL:
            self.save(row.name, "false" if row.on else "true")

    @on(OptionList.OptionSelected, "#settings-list")
    def _selected(self) -> None:
        self.edit()

    def edit(self) -> None:
        """Enter: a key's Set key dialog, a switch toggled, a choice picked from its list, or
        a value typed in a field prefilled with the current one."""
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
        now = next((n for n, c in enumerate(choices) if c in (current, current.lower())), None)
        labels = [
            (f"{c or settings_model.EMPTY}{'  (current)' if n == now else ''}", c)
            for n, c in enumerate(choices)
        ]

        def picked(value: str | None) -> None:
            if value is not None:
                self.save(meta.name, value)

        preview = f"{meta.help}\nEnter picks · Escape cancels"
        self.app.push_screen(PickModal(meta.name, labels, preview, selected=now), picked)

    def _type(self, meta: settings_model.Meta, current: str) -> None:
        name = meta.name
        own = name != common.EXTRA_PATTERNS
        # A value that looks like a key is never shown, not even here: the field opens empty,
        # the dialog says why, and an empty submit then keeps the value (it is a cancel).
        withheld = bool(current) and common.looks_like_a_key(current, user_patterns=own)
        value = "" if withheld else current

        def check(typed: str) -> str | None:
            if common.looks_like_a_key(typed, user_patterns=own):
                return "That looks like a key or password, which never goes in config.toml."
            try:
                settings_model.parse(name, typed)
            except ValueError as exc:
                return str(exc)
            return None

        default = settings_model.default(name) or settings_model.EMPTY
        preview = f"{meta.help}\nDefault: {default}"
        if withheld:
            preview += f"\n{WITHHELD_CURRENT}"

        def typed(new: str | None) -> None:
            if new is None or (withheld and new == ""):
                return
            self.save(name, new)

        self.app.push_screen(EditModal(name, value, check, preview), typed)

    def save(self, name: str, value: str) -> None:
        """Save through `config set`'s service; the command is logged (a private or key-like
        value withheld)."""
        shown_value = teach.WITHHELD if name in common.PRIVATE else shown_arg(value)

        def save() -> str:
            saved = settings_cmd.save_setting(name, value)
            self.pending = True  # until show() reads it back
            notes = settings_cmd.after_save(name)
            return "; ".join([f"{name} saved in {saved.path}", *notes])

        self.attempt(save, command=teach.for_setting("set", name, shown_value))

    def reset(self) -> None:
        """r: a key's Remove key dialog, or a setting back to its default (`config unset`)."""
        row = self.current()
        if row is None or self.waiting():
            return
        name = row.name
        if row.meta.kind == settings_model.KEY:
            self.remove_key(name)
            return

        def unset() -> str:
            removed, path = settings_cmd.unset_setting(name)
            self.pending = removed  # until show() reads it back
            if not removed:
                elsewhere = f" It comes from {row.where}." if row.source != "default" else ""
                return f"{name} is not saved in {path}; nothing to do.{elsewhere}"
            notes = settings_cmd.after_save(name)
            return "; ".join([f"{name} removed from {path}; its default applies", *notes])

        self.attempt(unset, command=teach.for_setting("reset", name))

    def set_key(self, name: str | None = None) -> None:
        """The hidden Set key dialog, with ``name`` (a key's name) picked (`secrets set`)."""
        fields = [
            Field("key-name", "Key", secret_names(), value=name or ""),
            Field("key-value", "Value (hidden as you type; never shown again)", secret=True),
        ]
        self.app.push_screen(
            FormModal("Save an API key in the secret store", fields), self._save_key
        )

    def _save_key(self, values: dict[str, str] | None) -> None:
        if values is None:
            return
        name, value = values["key-name"], values["key-value"].strip()
        if not value:
            self.report(f"no value entered; {name} was not changed", error=True)
            return

        def save() -> str:
            return "; ".join([settings_cmd.store_key(name, value), *settings_cmd.after_save(name)])

        self.attempt(save, command=teach.for_setting("set-key", name))

    def remove_key(self, name: str | None = None) -> None:
        """Pick a saved key (``name`` first, when saved), then confirm (`secrets remove`)."""
        try:
            common.refuse_in_legacy_mode("the secret store")
            saved = config_store.saved_secret_names()
        except EXPECTED as exc:
            self.report(f"error: {exc}", error=True)
            return
        if not saved:
            self.report("The secret store holds no key; nothing to do.")
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
            ConfirmModal(f"Delete {name} from the secret store?", confirm="Delete"), done
        )

    def _delete(self, name: str) -> str:
        config_store.delete_secret(name)
        layers, _ = common.load_layers()
        entry = layers.entries[name]
        if entry.value:
            still = common.source_label(entry.source)
            return f"{name} removed from the secret store; it is still set, from {still}."
        return f"{name} removed from the secret store."

    @on(Button.Pressed, "#migrate-env")
    def _migrate(self) -> None:
        self.migrate_env()

    def migrate_env(self) -> None:
        """Preview the .env migration, then apply it on Yes (`config migrate`)."""
        confirm_migration(self)

    @on(Button.Pressed, "#smoke")
    def _smoke(self) -> None:
        current = self.state.settings.provider if self.state else PROVIDER_NAMES[0]
        preview = (
            "Runs improve against a stub on 127.0.0.1 with a placeholder key. No provider is "
            "called, and neither your key nor your clipboard is sent."
        )
        fields = [Field("provider", "Provider", PROVIDER_NAMES, value=current)]
        self.app.push_screen(
            FormModal("Test call", fields, submit="Run", preview=preview), self._run_smoke
        )

    def _run_smoke(self, values: dict[str, str] | None) -> None:
        if values is not None:
            self.report(f"Running improve --provider {values['provider']} against the stub…")
            provider = values["provider"]
            self.background(lambda: self._smoke_now(provider))

    def _smoke_now(self, provider: str) -> None:
        result = smoke.run(provider)
        message = f"{'ok' if result.ok else 'failed'}: {result.message}"
        self.app.call_from_thread(self.report, message, error=not result.ok)
