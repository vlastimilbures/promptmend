"""What the interface's tabs and screens share (#93): the Pane every tab is, its buttons,
results and errors, and the flows more than one place opens (the .env migration, copying
profiles from a checkout, the deploy preview's digest)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import typer
from rich.text import Text
from textual import work
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Static

from .. import config_store, deploy, history
from .. import profiles as profile_service
from ..commands import common
from ..commands import profiles as profiles_cmd
from ..config_files import SecretStoreError
from ..prompt_builder import user_profiles_dir
from . import teach
from .modals import ConfirmModal, Field
from .state import State

if TYPE_CHECKING:
    from textual.widget import Widget
    from textual.widget import Widget as _Mixin

    from .app import ManageApp
else:
    # A plain mixin at run time: as a Widget subclass it would bring Widget's default CSS
    # ahead of the container's and change the layout.
    _Mixin = object

# What a save, a deletion or a deploy may raise for the person to read: the same errors the
# headless commands turn into `error: …` (commands/common.guard).
EXPECTED = (
    common.CommandError,
    ValueError,
    OSError,
    config_store.ConfigStoreError,
    SecretStoreError,
    deploy.DeployError,
    history.HistoryError,
    typer.BadParameter,
)


def add_row(table: DataTable[Any], *cells: str, key: str | None = None) -> None:
    """A row of plain text: a path or a value is never read as markup."""
    table.add_row(*(Text(cell) for cell in cells), key=key)


def button_row(*buttons: tuple[str, str]) -> Horizontal:
    """A row of buttons, each with its headless command as the tooltip (#111)."""
    return Horizontal(
        *(Button(label, id=id_, tooltip=teach.tooltip(id_)) for id_, label in buttons),
        classes="buttons",
    )


def result_text(message: str, command: str | None) -> Text:
    """An action's result, under the command it was (muted) when it has one."""
    if command is None:
        return Text(message)
    shown = Text()
    shown.append(f"$ {command}\n", style="dim")
    shown.append(message)
    return shown


class Reports(_Mixin):
    """report() and attempt() for a Pane or a screen that has a ``.result`` Static."""

    last_message = ""

    @property
    def manage(self) -> ManageApp:
        return cast("ManageApp", self.app)

    def report(self, message: str, *, error: bool = False, command: str | None = None) -> None:
        """Show ``message`` here and as a notification; with ``command`` (the same action in
        a terminal, teach.equivalent()), show it above and add both to Home's session log."""
        self.last_message = message
        self.query_one(".result", Static).update(result_text(message, command))
        self.app.notify(message, severity="error" if error else "information", markup=False)
        if command is not None:
            self.manage.log_action(teach.Entry(command, message, error))

    def attempt(
        self, action: Callable[[], str], *, reload: bool = True, command: str | None = None
    ) -> None:
        """Run a change; show its message, or the error a headless command would print."""
        try:
            message = action()
        except Exception as exc:
            self.report(error_message(exc), error=True, command=command)
            return
        self.report(message, command=command)
        if reload:
            self.manage.reload()


class Pane(Reports, VerticalScroll):
    """One tab. ``show()`` fills it from a fresh State; ``report()`` shows an action's result
    on the pane and as a notification."""

    ready = False
    # A deploy or detach dialog is open or running (TriggersPane).
    busy = False

    def on_mount(self) -> None:
        # A State read before this pane was composed is shown now (the load runs in a thread).
        self.setup()
        self.ready = True
        if self.state is not None:
            self.show(self.state)

    def setup(self) -> None:
        """Add the table columns; called once the pane's widgets exist."""

    @property
    def state(self) -> State | None:
        return self.manage.state

    def show(self, state: State) -> None:
        """Fill the pane from ``state``; each tab has its own."""

    def result(self) -> Static:
        return Static("", classes="result", markup=False)

    @work(thread=True, group="action", exit_on_error=False)
    def background(self, job: Callable[[], None]) -> None:
        """Run ``job`` in a thread (it may call a command or wait on Espanso); a failure is
        shown like attempt()'s, never a crashed screen."""
        try:
            job()
        except Exception as exc:
            self.app.call_from_thread(self.failed, exc)

    def failed(self, exc: Exception) -> None:
        self.busy = False
        self.report(error_message(exc), error=True)
        self.manage.reload()


def error_message(exc: Exception) -> str:
    """As commands/common.guard: a service error's own message; a bug named as unexpected."""
    if isinstance(exc, EXPECTED):
        return f"error: {str(exc) or type(exc).__name__}"
    return f"error: unexpected {type(exc).__name__}: {exc}"


CHECKOUT_FIELD = Field("checkout-path", "The checkout's folder", placeholder="~/Projects/…")


def checkout_path(owner: Reporter, values: dict[str, str] | None) -> Path | None:
    """The folder typed into CHECKOUT_FIELD, or None (and why) for none or a key."""
    if values is None:
        return None
    # A pasted path may keep its quotes (Windows "Copy as path").
    text = values[CHECKOUT_FIELD.id].strip().strip("\"'").strip()
    try:
        common.no_key(text, "the path")
    except EXPECTED as exc:
        owner.report(error_message(exc), error=True)
        return None
    if not text:
        owner.report("No path entered.", error=True)
        return None
    return Path(text).expanduser()


class Reporter(Protocol):
    def report(self, message: str, *, error: bool = False, command: str | None = None) -> None: ...

    def attempt(self, action: Callable[[], str], *, command: str | None = None) -> object: ...


_ROLLBACK_NOTE = "A backup is kept; `promptmend config rollback` undoes it."


def _migrated(result: config_store.MigrationResult) -> str:
    lines = [f"Migrated. Backup: {result.backup}"]
    lines += [f"{source} was not moved; it is no longer read" for source in result.left]
    return "\n".join(lines)


def confirm_migration(
    owner: Reporter,
    *,
    title: str = "Migrate the .env?",
    confirm: str = "Migrate",
    note: str = _ROLLBACK_NOTE,
    done: Callable[[config_store.MigrationResult], str] = _migrated,
) -> None:
    """Preview the .env migration, then apply it on Yes, as `config migrate` does. The
    dialog's words and the message ``done`` makes of the result are the caller's."""
    try:
        plan = config_store.plan_migration()
    except EXPECTED as exc:
        owner.report(error_message(exc), error=True)
        return
    if plan.status != "ready":
        owner.report("\n".join(plan.describe()))
        return

    def apply() -> str:
        return done(config_store.apply_migration(consent=plan.token))

    def answered(yes: bool | None) -> None:
        if yes:
            owner.attempt(apply, command=teach.equivalent("config", "migrate"))

    preview = "\n".join([*plan.describe(), "", note])
    cast("Widget", owner).app.push_screen(ConfirmModal(title, preview, confirm=confirm), answered)


def copy_profiles(owner: Reporter, root: Path | None) -> None:
    """Preview the profiles added or edited in the checkout at ``root`` (None: this editable
    install's), then copy them on Yes, as `profiles migrate --checkout` does. The preview
    runs git, so it is read in a thread."""
    widget = cast("Widget", owner)

    def look() -> None:
        try:
            checkout = profiles_cmd.find_checkout(None if root is None else str(root))
            source = checkout / profile_service.prompts_path(checkout)
            pristine = profile_service.git_pristine_profiles(checkout)
            changed = profile_service.changed_profiles(source, pristine)
        except Exception as exc:
            widget.app.call_from_thread(owner.report, error_message(exc), error=True)
            return
        widget.app.call_from_thread(_offer_copy, owner, checkout, source, pristine, changed)

    widget.run_worker(look, thread=True, group="copy-profiles", exclusive=True, exit_on_error=False)


def _offer_copy(
    owner: Reporter, root: Path, source: Path, pristine: dict[str, str], changed: dict[str, str]
) -> None:
    if not changed:
        owner.report(profile_service.NOTHING_CHANGED)
        return
    dest = user_profiles_dir()
    preview = "\n".join(
        [f"From {root}:"]
        + [f"{name}.md ({change}) -> {dest / f'{name}.md'}" for name, change in changed.items()]
    )

    command = teach.equivalent("profiles", "migrate", "--checkout", str(root))

    def done(yes: bool | None) -> None:
        if yes:
            owner.attempt(lambda: _copy(source, pristine, dest, list(changed)), command=command)

    cast("Widget", owner).app.push_screen(
        ConfirmModal("Copy these profiles? (copies only, never overwrites)", preview), done
    )


def _copy(source: Path, pristine: dict[str, str], dest: Path, names: list[str]) -> str:
    lines = [
        f"{item.name}: {item.status}"
        for item in profile_service.migrate_profiles(source, pristine, dest)
    ]
    hint = profile_service.overrides_hint(names, common.load_layers()[1].profile_overrides)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def manifest_digest(manifest: deploy.Manifest) -> list[tuple[str, dict[str, Any]]]:
    return sorted((key, vars(entry)) for key, entry in manifest.entries.items())


def plan_digest(plan: deploy.Plan) -> tuple[object, ...]:
    """What a deploy preview showed: each file's state and content, the
    launcher and the manifest it was compared with."""
    return (
        plan.espanso_dir,
        plan.launcher,
        [(s.name, s.state, s.current) for s in plan.steps],
        manifest_digest(plan.manifest),
        plan.orphans,
    )
