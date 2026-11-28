"""The Profiles tab: the built-in and your own profiles, the default, and editing one of
yours in $EDITOR."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.widgets import Button, DataTable, Static

from ..prompt_builder import ADDED, ALIASES, OVERRIDES, PROFILES, user_profiles_dir
from .base import Pane, add_row, button_row
from .modals import Field, FormModal
from .state import State


def editor_command(
    environ: Mapping[str, str] = os.environ,
    *,
    windows: bool = os.name == "nt",
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """$VISUAL or $EDITOR, else Notepad on Windows and vi elsewhere, as an argv. On Windows the
    quotes around a token are dropped (`"C:\\Program Files\\…\\ed.exe" -w`), and the program
    is looked up on PATH, so `code` finds code.cmd (PATHEXT)."""
    chosen = environ.get("VISUAL") or environ.get("EDITOR")
    if not chosen:
        argv = ["notepad"] if windows else ["vi"]
    else:
        argv = shlex.split(chosen, posix=not windows)
        if windows:
            argv = [
                token[1:-1] if len(token) > 1 and token[0] == token[-1] == '"' else token
                for token in argv
            ]
    return [which(argv[0]) or argv[0], *argv[1:]]


def run_editor(path: Path) -> int:
    """Open ``path`` in the editor and wait for it; tests replace it. No timeout: a person is
    editing."""
    return subprocess.run([*editor_command(), str(path)], check=False).returncode  # noqa: S603


class ProfilesPane(Pane):
    def compose(self) -> ComposeResult:
        yield Static("", markup=False, id="profile-settings")
        yield DataTable(id="profiles", cursor_type="row", zebra_stripes=True)
        yield button_row(
            ("set-profile", "Set default"),
            ("edit-profile", "Edit in $EDITOR"),
        )
        yield self.result()

    def setup(self) -> None:
        self.query_one("#profiles", DataTable).add_columns("Profile", "Where", "State")

    def show(self, state: State) -> None:
        cfg = state.settings
        self.query_one("#profile-settings", Static).update(
            f"PROMPT_PROFILE: {cfg.profile}\n"
            f"PROMPT_PRO_PROFILE: {cfg.pro_profile or '(empty: PROMPT_PROFILE)'}\n"
            f"Your profiles: {user_profiles_dir()}"
        )
        table = self.query_one("#profiles", DataTable)
        # Each stage of a reload refills the table: the cursor stays on its profile.
        kept = (
            table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            if table.row_count
            else None
        )
        table.clear()
        for name in PROFILES:
            add_row(table, name, "built in", "default" if name == cfg.profile else "", key=name)
        for alias, target in ALIASES.items():
            add_row(table, alias, "built in", f"retired; means {target}", key=alias)
        for profile in state.profiles:
            add_row(table, profile.name, "yours", profile.status, key=f"user:{profile.name}")
        if kept is not None and kept in table.rows:
            table.move_cursor(row=table.get_row_index(kept), animate=False)

    @on(Button.Pressed, "#set-profile")
    def _set_profile(self) -> None:
        if self.state is None:
            return
        own = [p.name for p in self.state.profiles if p.status == ADDED]
        value = self.highlighted(self.state)
        fields = [Field("profile", "PROMPT_PROFILE", [*PROFILES, *own], value=value)]
        self.app.push_screen(
            FormModal("Default profile", fields, focus="profile"), self._save_profile
        )

    def highlighted(self, state: State) -> str:
        """The profile the highlighted row names: a built-in one, a retired name's target, or
        one of yours that a trigger uses when it is picked (added, or replacing a built-in
        through PROMPT_PROFILE_OVERRIDES). A file of yours that is not used (shadowed, an
        invalid name, an override without its file) names nothing to pick, so it, like an
        empty table, gives the current default."""
        table = self.query_one("#profiles", DataTable)
        key = (
            table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or ""
            if table.row_count
            else ""
        )
        if key in PROFILES:
            return key
        if key in ALIASES:
            return ALIASES[key]
        used = {p.name for p in state.profiles if p.status in (ADDED, OVERRIDES)}
        name = key.removeprefix("user:")
        return name if key.startswith("user:") and name in used else state.settings.profile

    def _save_profile(self, values: dict[str, str] | None) -> None:
        if values is not None:
            self.save_setting("PROMPT_PROFILE", values["profile"])

    def selected(self) -> Path | None:
        table = self.query_one("#profiles", DataTable)
        if self.state is None or not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or ""
        name = key.removeprefix("user:")
        found = [p.path for p in self.state.profiles if key.startswith("user:") and p.name == name]
        return found[0] if found else None

    @on(Button.Pressed, "#edit-profile")
    def _edit(self) -> None:
        from textual.app import SuspendNotSupported

        path = self.selected()
        if path is None:
            self.report(
                "Select one of your profiles. Built-in ones ship with the package, and an "
                "upgrade replaces them.",
                error=True,
            )
            return
        try:
            with self.app.suspend():
                code = run_editor(path)
        except SuspendNotSupported:
            self.report("This terminal cannot hand over to an editor.", error=True)
            return
        except OSError as exc:
            self.report(f"error: could not start the editor ({exc})", error=True)
            return
        self.report(f"{path.name}: the editor exited with {code}", error=code != 0)
        self.manage.reload()
