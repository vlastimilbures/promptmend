"""The switch from an earlier checkout install (#110): a checklist over what detection found,
each step previewed in a dialog whose Cancel has the focus, then done through the same
service as its headless command (`config migrate --from`, `profiles migrate --checkout`,
`espanso deploy`, `config retire --from`). Keys appear by name only, never their value."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from .. import config_store, deploy, previous_install
from ..profiles import prompts_path
from . import teach
from .base import (
    CHECKOUT_FIELD,
    EXPECTED,
    checkout_path,
    confirm_migration,
    copy_profiles,
    error_message,
    previous_root,
    result_text,
)
from .modals import ConfirmModal, FormModal
from .state import State
from .triggers_pane import TriggersPane

if TYPE_CHECKING:
    from .app import ManageApp

INTRO = (
    "An earlier install ran from a checkout of this project. This install never reads that "
    "checkout's .env or profiles, so copy what you need, in this order. Each step shows a "
    "preview first; nothing is deleted, and `promptmend config rollback` undoes the copy "
    "(the match files stay deployed)."
)
GATED = {
    previous_install.LEGACY: "PROMPTMEND_ENV is set, so that file holds the settings",
    previous_install.SAVED: "settings are already saved in config.toml",
    previous_install.SECRETS: "a key is already saved in the secret store",
}


def wants_offer(found: previous_install.Detection) -> bool:
    """Open the screen by itself: a checkout to copy from, or a copy whose old .env is still
    in place and was not skipped."""
    if found.candidates:
        return True
    pending = found.pending
    return pending is not None and str(pending.root) not in previous_install.skipped_roots()


# Step button id -> label, in the order to do them.
STEPS = {
    "previous-copy": "Copy settings and keys…",
    "previous-profiles": "Copy edited profiles…",
    "previous-deploy": "Deploy the match files…",
    "previous-retire": "Retire the old .env…",
}


class PreviousInstallScreen(Screen[None]):
    # The digit in each step's label is its key, as on the tabs.
    BINDINGS: ClassVar[list[BindingType]] = [
        *(Binding(str(n), f"step('{step}')", show=False) for n, step in enumerate(STEPS, 1)),
        Binding("escape", "close", "Close"),
    ]

    last_message = ""

    def __init__(
        self,
        entered: Path | None = None,
        *,
        espanso_dir: Path | None = None,
        launcher: Path | None = None,
        plan_of: Callable[[], deploy.Plan] | None = None,
    ) -> None:
        super().__init__()
        # The path typed into "Enter a path…" (or setup's --migrate-from): detected again
        # with it on every refresh.
        self.entered = entered
        # setup's --espanso-dir and --launcher: detection, the deploy and the retire use them.
        self.espanso_dir = espanso_dir
        self.launcher = launcher
        self.plan_of = plan_of
        self.found = previous_install.Detection()

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(INTRO, classes="note", markup=False)
            yield Static("Looking…", markup=False, id="previous-found")
            with Vertical(id="previous-steps"):
                for n, (id_, label) in enumerate(STEPS.items(), 1):
                    with Horizontal(classes="step"):
                        yield Button(f"{n} {label}", id=id_, tooltip=teach.tooltip(id_))
                        yield Static("", markup=False, id=f"{id_}-state", classes="step-state")
            yield Horizontal(
                Button("Enter a path…", id="previous-enter"),
                Button("Skip this checkout", id="previous-skip"),
                Button("Close", id="previous-close"),
                classes="buttons",
            )
            yield Static("", classes="result", markup=False)
        yield Footer()

    @property
    def manage(self) -> ManageApp:
        return cast("ManageApp", self.app)

    def on_mount(self) -> None:
        if self.manage.state is not None:
            self.show(self.manage.state)

    # --- What is shown --------------------------------------------------------------------

    def show(self, state: State) -> None:
        """Fill the screen from a fresh State; with an entered path, detect again with it."""
        if self.entered is None and self.espanso_dir is None and self.launcher is None:
            self.render_found(state.previous, state.previous_error)
        else:
            self._detect(self.entered)

    @work(thread=True, group="previous", exclusive=True, exit_on_error=False)
    def _detect(self, entered: Path | None) -> None:
        try:
            found = previous_install.detect(
                entered=entered, espanso_dir=self.espanso_dir, launcher=self.launcher
            )
        except EXPECTED as exc:
            self.app.call_from_thread(self.render_found, previous_install.Detection(), str(exc))
            return
        self.app.call_from_thread(self._show_entered, entered, found)

    def _show_entered(self, entered: Path | None, found: previous_install.Detection) -> None:
        self.render_found(found, None)
        if (
            entered is None
            or found.gated
            or any(previous_install.ENTERED in c.signals for c in found.candidates)
        ):
            return
        # Not a checkout: say so, and stop looking there on every reload.
        self.entered = None
        self.report(
            f"{entered} is not a checkout of {previous_install.PROJECT_NAME} (a folder whose "
            "pyproject.toml names it), or it is this install's own",
            error=True,
        )

    def render_found(self, found: previous_install.Detection, error: str | None) -> None:
        self.found = found
        best = previous_install.best(found.candidates)
        pending = found.pending
        lines: list[str] = []
        if error:
            lines.append(f"Could not look for a previous install: {error}")
        for candidate in found.candidates:
            signals = ", ".join(sorted(candidate.signals))
            env = ".env present" if candidate.env_file else "no .env"
            lines.append(f"Previous install: {candidate.root} ({signals}); {env}")
        if pending is not None:
            lines.append(f"Settings copied from {pending.root}; its .env is still in place.")
        if not found.candidates and pending is None:
            if found.gated:
                lines.append(f"Nothing to look for: {GATED.get(found.gated, found.gated)}.")
            else:
                lines.append(
                    "No previous install found. If you ran it from a checkout, enter its path."
                )
        if found.shadow:
            lines.append(
                f"`{found.shadow.command}` on PATH is {found.shadow.path}, "
                f"not {found.shadow.launcher}: "
                f"{found.shadow.hint}"
            )
        self.query_one("#previous-found", Static).update("\n".join(lines))

        inside = (best.launchers_in_root if best else ()) or (
            pending.launchers_in_root if pending else ()
        )
        copied = pending is not None
        root = previous_root(found)
        steps = {
            "previous-copy": (
                (False, "done: the settings were copied")
                if copied
                else (True, f"to do: from {best.env_file}")
                if best and best.env_file
                else (False, "nothing to copy: no .env")
            ),
            "previous-profiles": (
                (
                    True,
                    f"from {root / prompts_path(root)}; the preview lists the added or edited ones",
                )
                if root is not None
                and ((best and best.profiles_dir) or (best is None and pending is not None))
                else (False, "no profiles folder")
            ),
            "previous-deploy": (
                (True, f"to do: the match files still run {', '.join(inside)}")
                if inside
                else (True, "the match files no longer run the old CLI")
            ),
            "previous-retire": (
                (True, f"ready: {pending.env_file}")
                if pending is not None and not pending.launchers_in_root
                else (False, "after steps 1 and 3")
                if best or pending
                else (False, "nothing to retire")
            ),
        }
        for id_, (enabled, text) in steps.items():
            self.query_one(f"#{id_}", Button).disabled = not enabled
            self.query_one(f"#{id_}-state", Static).update(text)
        self.query_one("#previous-skip", Button).disabled = best is None and pending is None

    def report(self, message: str, *, error: bool = False, command: str | None = None) -> None:
        """As Pane.report(): with ``command``, shown above and kept in Home's session log."""
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

    # --- Steps ----------------------------------------------------------------------------

    def action_step(self, step: str) -> None:
        button = self.query_one(f"#{step}", Button)
        if not button.disabled:
            button.press()

    @property
    def triggers(self) -> TriggersPane:
        return self.manage.main.query_one("#triggers-pane", TriggersPane)

    def busy(self) -> bool:
        """A deploy reads its plan in a worker and opens its dialog later: until it is done,
        no other step opens a dialog, so none lands on top of another."""
        if self.triggers.busy:
            self.report("A deploy is in progress; finish or cancel it first.", error=True)
            return True
        return False

    def _ask(
        self, title: str, preview: str, confirm: str, action: Callable[[], str], command: str
    ) -> None:
        def done(yes: bool | None) -> None:
            if yes:
                self.attempt(action, command=command)

        self.app.push_screen(ConfirmModal(title, preview, confirm=confirm), done)

    @on(Button.Pressed, "#previous-copy")
    def _copy(self) -> None:
        best = previous_install.best(self.found.candidates)
        if best is None or self.busy():
            return
        confirm_migration(
            self,
            best.root,
            title=f"Copy the settings of {best.root}?",
            confirm="Copy",
            done=lambda result: (
                f"Copied; backup in {result.backup}. Next: deploy the match files (step 3), "
                "so the triggers use these settings."
            ),
        )

    @on(Button.Pressed, "#previous-profiles")
    def _profiles(self) -> None:
        root = previous_root(self.found)
        if root is not None and not self.busy():
            copy_profiles(self, root)

    @on(Button.Pressed, "#previous-deploy")
    def _deploy(self) -> None:
        triggers = self.triggers
        if self.busy():
            return
        best = previous_install.best(self.found.candidates)
        if best is None or best.env_file is None or self.found.pending is not None:
            triggers.start_deploy(self.plan_of)
            return

        def done(yes: bool | None) -> None:
            if yes:
                triggers.start_deploy(self.plan_of)

        # As setup's deploy step (#110): deploying before the copy leaves the triggers with
        # neither the old settings nor the new ones.
        self.app.push_screen(
            ConfirmModal(
                f"The settings of {best.root} are not copied yet. Deploy anyway?",
                "Deploying now points the triggers at this install, which has none of those "
                "settings and no key. Copy them first (step 1).",
                confirm="Deploy anyway",
            ),
            done,
        )

    @on(Button.Pressed, "#previous-retire")
    def _retire(self) -> None:
        pending = self.found.pending
        if pending is None or self.busy():
            return
        self.report("Reading the match files…")
        self._plan_retire(pending.root)

    @work(thread=True, group="previous-retire", exclusive=True, exit_on_error=False)
    def _plan_retire(self, root: Path) -> None:
        """`espanso path config` may take a while: asked here, in a worker, never on the UI
        thread. Without its answer retire is refused, as in `config retire --from`."""
        try:
            if self.espanso_dir is not None:
                folder = self.espanso_dir
                plan = config_store.plan_retire(root, espanso_dir=folder)
                self.app.call_from_thread(self._ask_retire, root, folder, plan)
                return
            found = deploy.locate_espanso_dir()
            if found.fallback:
                raise config_store.MigrationError(
                    f"cannot check which CLI the match files run ({found.fallback}); in a "
                    f"terminal: `promptmend config retire --from {root} --espanso-dir PATH`"
                )
            folder = found.path
            plan = config_store.plan_retire(root, espanso_dir=folder)
        except Exception as exc:
            self.app.call_from_thread(self.report, error_message(exc), error=True)
            return
        self.app.call_from_thread(self._ask_retire, root, folder, plan)

    def _ask_retire(self, root: Path, folder: Path, plan: config_store.RetirePlan) -> None:
        def apply() -> str:
            place = config_store.apply_retire(root, espanso_dir=folder, consent=plan.token)
            return f"Retired: {plan.env_file} -> {place}"

        command = teach.equivalent("config", "retire", "--from", str(root))
        self._ask(
            f"Retire the old .env of {root}?", "\n".join(plan.describe()), "Retire", apply, command
        )

    @on(Button.Pressed, "#previous-enter")
    def _enter(self) -> None:
        if self.busy():
            return
        self.app.push_screen(
            FormModal("Where did you run it from?", [CHECKOUT_FIELD], submit="Look"),
            self._entered,
        )

    def _entered(self, values: dict[str, str] | None) -> None:
        entered = checkout_path(self, values)
        if entered is not None:
            self.entered = entered
            self._detect(entered)

    @on(Button.Pressed, "#previous-skip")
    def _skip(self) -> None:
        roots = [c.root for c in self.found.candidates]
        if self.found.pending is not None:
            roots.append(self.found.pending.root)
        try:
            for root in roots:
                previous_install.skip(root)
        except OSError as exc:
            self.report(error_message(exc), error=True)
            return
        self.app.notify(
            "Skipped. To see that checkout again: Home > Previous install… > Enter a path…",
            markup=False,
        )
        self.dismiss()
        self.manage.reload()

    @on(Button.Pressed, "#previous-close")
    def action_close(self) -> None:
        self.dismiss()
