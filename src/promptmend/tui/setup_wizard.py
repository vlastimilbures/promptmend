"""The setup wizard: `promptmend setup` in a terminal, and Home's "Setup…". One step at a time
(setup_guide.STEPS: Welcome, cloud or local, keys, profile, output, Espanso, test, done), each
picked from a list rather than typed, each change made through the same service as its
headless command and logged as that command. The test runs the path of the cloud-or-local
answer's triggers, never PROMPT_PROVIDER (#390). What each step says comes from
setup_guide.py, and what it decides and writes from setup_flow.py, shared with `setup
--non-interactive` (neither imports Textual)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    ContentSwitcher,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option, OptionDoesNotExist

from .. import config, deploy, setup_flow, setup_guide, smoke
from ..config import secret_names
from . import teach
from .base import error_message, plan_digest, result_text
from .state import State

if TYPE_CHECKING:
    from .app import ManageApp


@dataclass(frozen=True)
class SetupOptions:
    """`setup`'s options the wizard honours; ``standalone`` when `setup` opened it (Done then
    quits), else it was opened from Home and closes back to it. ``use``, ``profile`` and
    ``output`` preselect a choice, ``deploy`` True puts the focus on Install (still a press)
    and False skips that step, as ``smoke_test`` False skips the test."""

    espanso_dir: str | None = None
    launcher: str | None = None
    no_restart: bool = False
    standalone: bool = False
    use: str | None = None
    profile: str | None = None
    output: str | None = None
    deploy: bool | None = None
    smoke_test: bool = True


def make_plan(options: SetupOptions) -> tuple[deploy.Plan, str | None]:
    """The deploy plan for ``options`` (as `setup --espanso-dir --launcher` makes it), and why
    the default Espanso folder was used when `espanso path config` gave none."""
    return setup_flow.read_plan(options.espanso_dir, options.launcher)


class SetupScreen(Screen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, options: SetupOptions | None = None) -> None:
        super().__init__()
        self.options = options or SetupOptions()
        self.order = self._steps()
        self.at = "welcome"
        self.plan: deploy.Plan | None = None
        self.plan_error: str | None = None
        self.plan_asked = False
        self.tested: bool | None = None
        self.working = False
        self.keys: list[setup_guide.KeyRow] = []
        # The Cloud or local step's answer, once Next was pressed on it.
        self.use: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="setup"):
            yield Static("", id="setup-rail")
            with Vertical(id="setup-body"):
                yield Static("", id="setup-title")
                with ContentSwitcher(initial="setup-welcome", id="setup-steps"):
                    with VerticalScroll(id="setup-welcome"):
                        yield Static(setup_guide.WELCOME, markup=False)
                        yield Static("", id="setup-welcome-notes", classes="note", markup=False)
                    with VerticalScroll(id="setup-use"):
                        yield Static(setup_guide.USE_INTRO, classes="note", markup=False)
                        yield OptionList(id="setup-uses")
                        yield Static("", id="setup-use-note", classes="note", markup=False)
                    with VerticalScroll(id="setup-keys"):
                        yield Static(
                            setup_guide.KEYS_INTRO,
                            id="setup-keys-intro",
                            classes="note",
                            markup=False,
                        )
                        for name in secret_names():
                            yield Static("", id=f"setup-line-{name}", markup=False)
                            yield Input(
                                password=True,
                                placeholder=f"paste {name} (hidden)",
                                id=f"setup-key-{name}",
                            )
                        yield Horizontal(
                            self._button("setup-save-keys", "Save keys"), classes="buttons"
                        )
                    with VerticalScroll(id="setup-profile"):
                        yield Static(setup_guide.PROFILE_INTRO, classes="note", markup=False)
                        yield OptionList(id="setup-profiles")
                    with VerticalScroll(id="setup-output"):
                        yield Static("", id="setup-output-intro", classes="note", markup=False)
                        yield OptionList(id="setup-outputs")
                    with VerticalScroll(id="setup-espanso"):
                        yield Static(setup_guide.ESPANSO_INTRO, classes="note", markup=False)
                        yield Static("Reading the match folder…", id="setup-files", markup=False)
                        yield Horizontal(
                            self._button("setup-deploy", "Install the match files"),
                            classes="buttons",
                        )
                    with VerticalScroll(id="setup-test"):
                        yield Static("", id="setup-test-intro", classes="note", markup=False)
                        yield Static("", id="setup-servers", classes="note", markup=False)
                        yield Horizontal(self._button("setup-run-test", "Run the test"))
                    with VerticalScroll(id="setup-done"):
                        yield Static("", id="setup-summary")
                        yield Static(setup_guide.NEXT_STEP, id="setup-next", markup=False)
                        yield Horizontal(
                            self._button("setup-open", "Open PromptMend"),
                            self._button("setup-quit", "Quit"),
                            classes="buttons",
                        )
                yield Static("", classes="result", markup=False)
                yield Horizontal(
                    self._button("setup-back", "◀ Back"),
                    self._button("setup-next-step", "Next ▶"),
                    id="setup-nav",
                    classes="buttons",
                )
        yield Footer()

    @property
    def skipped(self) -> list[str]:
        """The steps `setup --no-deploy`/`--no-smoke-test` leave out."""
        return [
            step
            for step, skip in (
                ("espanso", self.options.deploy is False),
                ("test", not self.options.smoke_test),
            )
            if skip
        ]

    def _steps(self) -> list[str]:
        return [s for s in setup_guide.STEPS if s not in self.skipped]

    @staticmethod
    def _button(id_: str, label: str) -> Button:
        return Button(label, id=id_, tooltip=teach.tooltip(id_))

    @property
    def manage(self) -> ManageApp:
        return cast("ManageApp", self.app)

    def answer(self, settings: config.Settings) -> str:
        """The Cloud or local answer: the one Next was pressed on, else what the settings
        give (local while PROMPT_LOCAL_ONLY is true, else cloud)."""
        return self.use or setup_flow.current_use(settings)

    def on_mount(self) -> None:
        if not self.options.standalone:
            self.query_one("#setup-quit", Button).label = "Close"
            self.query_one("#setup-open", Button).display = False
        self.go("welcome")
        if self.manage.state is not None:
            self.show(self.manage.state)

    # --- What is shown --------------------------------------------------------------------

    def show(self, state: State) -> None:
        """Fill every step from a fresh State (the app calls this after each reload)."""
        notes = []
        where = setup_flow.storage()
        if where.legacy:
            notes.append(setup_guide.LEGACY_NOTE)
        elif where.unread:
            notes.append(where.unread)
        notes.append(setup_guide.history_line(state.settings.history))
        self.query_one("#setup-welcome-notes", Static).update("\n".join(notes))
        self.query_one("#setup-use-note", Static).update(
            setup_guide.local_note(state.triggers, state.settings)
        )
        self._fill(
            "#setup-uses",
            setup_guide.use_choices(state.triggers, state.settings),
            self.options.use,
        )
        self._fill(
            "#setup-profiles",
            setup_guide.profile_choices(state.settings, state.profiles),
            self.options.profile,
        )
        self.query_one("#setup-output-intro", Static).update(
            setup_guide.output_intro(state.settings)
        )
        self._fill(
            "#setup-outputs", setup_guide.output_choices(state.settings), self.options.output
        )
        self._show_test_intro(state)
        self.keys = setup_guide.key_rows(state.layers, state.settings, state.triggers)
        legacy = where.legacy
        self.query_one("#setup-keys-intro", Static).update(
            setup_guide.legacy_keys_intro(legacy) if legacy else setup_guide.KEYS_INTRO
        )
        # Nothing to save while PROMPTMEND_ENV is set: no fields, so Next always moves on.
        for name in secret_names():
            self.query_one(f"#setup-key-{name}", Input).display = not legacy
        self.query_one("#setup-save-keys", Button).display = not legacy
        for row in self.keys:
            self.query_one(f"#setup-line-{row.name}", Static).update(setup_guide.key_line(row))
        servers = next((c for c in state.report.checks if c.id == "local_servers"), None)
        line = servers.message if servers else ""
        if not state.full:  # a quick State
            line = (
                "Checking whether a local server answers…"
                if state.failed is None
                else "Not checked whether a local server answers: the checks could not run."
            )
        self.query_one("#setup-servers", Static).update(line)
        # The Espanso step reads its own plan: once per reload (its full State), and at once
        # the first time, so a quick State never asks Espanso twice.
        if state.full or not self.plan_asked:
            self.plan_asked = True
            self._refresh_plan()
        # Each stage of a reload comes here: the person's place (a typed key field, a
        # button) keeps the focus; it moves only when nothing in this step has it.
        self.go(self.at, refocus=not self._focus_in_step())

    def _focus_in_step(self) -> bool:
        """Whether the focus is in the current step or on its Back/Next row."""
        focused = self.focused
        if focused is None:
            return False
        holders = (self.query_one(f"#setup-{self.at}"), self.query_one("#setup-nav"))
        return any(holder in focused.ancestors_with_self for holder in holders)

    def _fill(self, selector: str, choices: list[setup_guide.Choice], preset: str | None) -> None:
        options = self.query_one(selector, OptionList)
        highlighted = options.highlighted_option.id if options.highlighted_option else None
        options.clear_options()
        for choice in choices:
            line = Text(choice.title, style="bold")
            if choice.current:
                line.append("  (current)", style="green")
            line.append(f"\n  {choice.detail}", style="dim")
            options.add_option(Option(line, id=choice.value))
        # Else the first: no answer of the Cloud or local step is current unless it is local.
        keep = (
            highlighted
            or preset
            or next((c.value for c in choices if c.current), None)
            or next((c.value for c in choices), None)
        )
        if keep is not None:
            try:
                options.highlighted = options.get_option_index(keep)
            except OptionDoesNotExist:
                options.highlighted = 0

    def go(self, step: str, *, refocus: bool = True) -> None:
        self.at = step
        self.query_one("#setup-steps", ContentSwitcher).current = f"setup-{step}"
        n = self.order.index(step) + 1 if step in self.order else 1
        title = f"Step {n} of {len(self.order)} · {setup_guide.STEPS[step]}"
        self.query_one("#setup-title", Static).update(title)
        rail = Text()
        for i, id_ in enumerate(self.order):
            mark = "▶ " if id_ == step else "✓ " if i < n - 1 else "  "
            rail.append(f"{mark}{setup_guide.STEPS[id_]}\n", style="bold" if id_ == step else "")
        self.query_one("#setup-rail", Static).update(rail)
        done = step == "done"
        # Hidden on the last step, and disabled too: Textual can focus a button whose row is
        # hidden (focusable ignores an ancestor's display), so a focus already on its way to
        # Next, or Next's hiding moving the focus to its sibling Back, would leave the focus
        # on a button nobody sees. Disabled, neither can take it.
        self.query_one("#setup-back", Button).disabled = step == self.order[0] or done
        self.query_one("#setup-next-step", Button).disabled = done
        self.query_one("#setup-nav").display = not done
        if step == "done":
            # Its own focus (Quit): the hidden Next must not take it after.
            self._show_done(focus=refocus)
            return
        focus = {
            "use": "#setup-uses",
            "profile": "#setup-profiles",
            "output": "#setup-outputs",
        }.get(step)
        if (
            step == "espanso"
            and self.options.deploy
            and not self.query_one("#setup-deploy", Button).disabled
        ):
            focus = "#setup-deploy"
        if not refocus:
            return
        if focus:
            self.call_after_refresh(self.query_one(focus).focus)
        else:
            self.call_after_refresh(self.query_one("#setup-next-step").focus)

    def report(self, message: str, *, error: bool = False, command: str | None = None) -> None:
        self.query_one(".result", Static).update(result_text(message, command))
        if error:
            self.app.notify(message, severity="error", markup=False)
        if command is not None:
            self.manage.log_action(teach.Entry(command, message, error))

    def attempt(self, action: Callable[[], str | None], command: str | None = None) -> bool:
        try:
            message = action()
        except Exception as exc:
            self.report(error_message(exc), error=True, command=command)
            return False
        if message:
            self.report(message, command=command)
            self.manage.reload()
        return True

    # --- Moving ---------------------------------------------------------------------------

    @on(Button.Pressed, "#setup-next-step")
    def action_next(self) -> None:
        if self.working:
            return
        if self.at == "use" and not self._save_use():
            return
        if self.at == "keys" and not self._save_keys():
            return
        if self.at == "profile" and not self._save_picked("PROMPT_PROFILE", "#setup-profiles"):
            return
        if self.at == "output" and not self._save_picked("PROMPT_OUTPUT", "#setup-outputs"):
            return
        i = self.order.index(self.at) if self.at in self.order else 0
        self.report("")
        self.go(self.order[min(i + 1, len(self.order) - 1)])

    @on(Button.Pressed, "#setup-back")
    def action_back(self) -> None:
        if self.working:
            return
        i = self.order.index(self.at) if self.at in self.order else 0
        if i == 0:
            if not self.options.standalone:
                self.dismiss()
            return
        self.report("")
        self.go(self.order[i - 1])

    @on(OptionList.OptionSelected)
    def _picked(self) -> None:
        self.action_next()

    # --- Steps ----------------------------------------------------------------------------

    def _save_use(self) -> bool:
        """The Cloud or local answer: PROMPT_LOCAL_ONLY=true for local, removed for cloud or
        both; kept for the test and Done steps even where it cannot be saved."""
        option = self.query_one("#setup-uses", OptionList).highlighted_option
        if option is None or option.id is None:
            return True
        if not self._save_picked(setup_flow.LOCAL_ONLY, "#setup-uses", setup_flow.local_only_value):
            return False
        self.use = option.id
        if self.manage.state is not None:
            self._show_test_intro(self.manage.state)
        return True

    def _save_picked(self, name: str, selector: str, value_of: Callable[[str], str] = str) -> bool:
        option = self.query_one(selector, OptionList).highlighted_option
        if option is None or option.id is None:
            return True
        value = value_of(option.id)
        legacy = setup_flow.storage().legacy
        if legacy:
            # Nothing can be saved (as headless setup's writable=False): say so and move on.
            if setup_flow.current_choice(name) != value:
                self.app.notify(
                    f"{name} not saved: {config.ENV_FILE_VAR} is set, so settings stay in "
                    f"{legacy}; edit that file instead.",
                    severity="warning",
                    markup=False,
                )
            return True
        try:
            message, argv = setup_flow.save_choice(name, value)
        except Exception as exc:
            command = teach.equivalent("config", *setup_flow.choice_command(name, value))
            self.report(error_message(exc), error=True, command=command)
            return False
        if message:
            self.report(message, command=teach.equivalent("config", *argv))
            self.manage.reload()
        return True

    def _save_keys(self) -> bool:
        """Save every key field typed into; an empty one keeps what is there."""
        typed = [
            (name, field.value.strip())
            for name in secret_names()
            if (field := self.query_one(f"#setup-key-{name}", Input)).value.strip()
        ]
        if not typed or setup_flow.storage().legacy:
            return True
        command = "; ".join(teach.for_setting("set-key", name) for name, _ in typed)
        ok = self.attempt(lambda: setup_flow.save_keys(typed), command=command)
        if ok:
            for name, _ in typed:
                self.query_one(f"#setup-key-{name}", Input).value = ""
        return ok

    @on(Button.Pressed, "#setup-save-keys")
    def _save_keys_pressed(self) -> None:
        self._save_keys()

    # The Espanso step.

    def _refresh_plan(self) -> None:
        self._read_plan(self.options)

    @work(thread=True, group="setup-plan", exclusive=True, exit_on_error=False)
    def _read_plan(self, options: SetupOptions) -> None:
        try:
            plan, fallback = make_plan(options)
        except Exception as exc:
            self.app.call_from_thread(self._show_plan, None, None, error_message(exc))
            return
        self.app.call_from_thread(self._show_plan, plan, fallback, None)

    def _show_plan(self, plan: deploy.Plan | None, fallback: str | None, error: str | None) -> None:
        self.plan, self.plan_error = plan, error
        button = self.query_one("#setup-deploy", Button)
        if plan is None:
            self.query_one("#setup-files", Static).update(
                f"Cannot read the match folder: {error}\nIs Espanso installed? See "
                "docs/install.md; then `promptmend espanso deploy`."
            )
            button.disabled = True
            return
        lines = [f"Match folder: {plan.espanso_dir / 'match'}"]
        if fallback:
            lines.append(f"({fallback}; Espanso may read another folder)")
        lines.append("")
        width = max((len(name) for name, _ in setup_guide.file_rows(plan)), default=0)
        lines += [f"  {name:<{width}}  {words}" for name, words in setup_guide.file_rows(plan)]
        lines += ["", setup_guide.deploy_summary(plan)]
        self.query_one("#setup-files", Static).update("\n".join(lines))
        button.disabled = setup_flow.in_sync(plan)
        if self.at == "done":
            self._show_done(focus=not self._focus_in_step())

    @on(Button.Pressed, "#setup-deploy")
    def _deploy(self) -> None:
        if self.plan is None or self.working:
            return
        self.working = True
        self.report("Installing the match files…")
        self._apply(self.plan, self.options)

    @work(thread=True, group="setup-action", exit_on_error=False)
    def _apply(self, shown: deploy.Plan, options: SetupOptions) -> None:
        try:
            plan, _ = make_plan(options)
            if plan_digest(plan) != plan_digest(shown):
                raise deploy.DeployError(
                    "the match files changed since they were read; nothing was written. "
                    "Look at the new list and install again"
                )
            outcome = setup_flow.install(plan)  # an edited file is kept, never overwritten
            lines = list(outcome.lines)
            if outcome.changed:
                if options.no_restart:
                    lines.append("Espanso was not restarted (--no-restart).")
                elif not deploy.restart_espanso():
                    lines.append("Could not restart Espanso; run `espanso restart` yourself.")
                else:
                    lines.append("Espanso restarted.")
            if outcome.kept:
                lines.append("Files you edited were kept: the Triggers tab lets you choose.")
            message, error = "\n".join(lines) or "Nothing to write.", False
        except Exception as exc:
            message, error = error_message(exc), True
        self.app.call_from_thread(self._applied, message, error)

    def _applied(self, message: str, error: bool) -> None:
        self.working = False
        self.report(message, error=error, command=teach.equivalent("espanso", "deploy"))
        self._refresh_plan()
        self.manage.reload()

    # The test step.

    def _runs(self, state: State) -> list[setup_guide.SmokeRun]:
        return setup_guide.smoke_runs(self.answer(state.settings), state.triggers, state.settings)

    def _show_test_intro(self, state: State) -> None:
        self.query_one("#setup-test-intro", Static).update(
            setup_guide.smoke_intro(self._runs(state))
        )

    @on(Button.Pressed, "#setup-run-test")
    def _test(self) -> None:
        if self.working or self.manage.state is None:
            return
        self.working = True
        self.report("Running the test against the stub…")
        self._run_test(self._runs(self.manage.state))

    @work(thread=True, group="setup-action", exit_on_error=False)
    def _run_test(self, runs: list[setup_guide.SmokeRun]) -> None:
        results = []
        for run in runs:
            try:
                result = smoke.run(run.provider)
                ok, message = result.ok, result.message
            except Exception as exc:
                ok, message = False, error_message(exc)
            results.append((ok, setup_guide.smoke_result(run, ok, message)))
        self.app.call_from_thread(self._tested, results)

    def _tested(self, results: list[tuple[bool, str]]) -> None:
        self.working = False
        ok = all(passed for passed, _ in results)
        self.tested = ok
        self.report("\n".join(line for _, line in results), error=not ok)

    # The last step.

    def _show_done(self, *, focus: bool = True) -> None:
        """The checklist; ``focus`` puts the focus on Quit (Close), off when a refresh comes
        while the person is already somewhere in this step."""
        state = self.manage.state
        if state is None:
            return
        summary = Text()
        for row in setup_guide.done_rows(
            state.settings,
            self.answer(state.settings),
            self.keys,
            self.plan,
            self.tested,
            setup_flow.storage().legacy,
            self.skipped,
        ):
            summary.append("✓ " if row.ok else "✗ ", style="green" if row.ok else "yellow")
            summary.append(f"{row.label}: ", style="bold")
            summary.append(f"{row.text}\n")
        self.query_one("#setup-summary", Static).update(summary)
        if focus:
            self.call_after_refresh(self.query_one("#setup-quit").focus)

    @on(Button.Pressed, "#setup-open")
    def _open(self) -> None:
        self.dismiss()

    @on(Button.Pressed, "#setup-quit")
    def _quit(self) -> None:
        if self.options.standalone:
            self.app.exit(0)
        else:
            self.dismiss()
