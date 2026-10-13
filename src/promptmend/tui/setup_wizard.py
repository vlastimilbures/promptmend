"""The setup wizard: `promptmend setup` in a terminal, and Home's "Setup…". One step at a time
(Welcome, earlier settings, provider, profile, keys, Espanso, test, done), each picked from a
list rather than typed, each change made through the same service as its headless command and
logged as that command. What each step says comes from setup_guide.py (no Textual)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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

from .. import config, config_files, config_store, deploy, setup_guide, smoke
from ..commands import common
from ..commands import settings as settings_cmd
from ..commands import setup as setup_cmd
from ..config import secret_names
from . import teach
from .base import EXPECTED, confirm_migration, error_message, plan_digest, result_text
from .previous import wants_offer
from .state import State

if TYPE_CHECKING:
    from .app import ManageApp


@dataclass(frozen=True)
class SetupOptions:
    """`setup`'s options the wizard honours; ``standalone`` when `setup` opened it (Done then
    quits), else it was opened from Home and closes back to it. ``provider`` and ``profile``
    preselect a choice, ``migrate_from`` is the earlier install to look at, ``deploy`` True
    puts the focus on Install (still a press) and False skips that step, as ``smoke_test``
    False skips the test (#244)."""

    espanso_dir: str | None = None
    launcher: str | None = None
    no_restart: bool = False
    standalone: bool = False
    provider: str | None = None
    profile: str | None = None
    migrate_from: Path | None = None
    deploy: bool | None = None
    smoke_test: bool = True


def make_plan(options: SetupOptions) -> tuple[deploy.Plan, str | None]:
    """The deploy plan for ``options`` (as `setup --espanso-dir --launcher` makes it), and why
    the default Espanso folder was used when `espanso path config` gave none."""
    fallback = None
    if options.espanso_dir is not None:
        common.no_key(options.espanso_dir, "--espanso-dir")
        target = Path(options.espanso_dir).expanduser().resolve()
    else:
        found = deploy.locate_espanso_dir()
        target, fallback = found.path.resolve(), found.fallback
    if options.launcher is not None:
        common.no_key(options.launcher, "--launcher")
        launcher = options.launcher
    else:
        launcher = str(deploy.resolve_launcher().path)
    return deploy.plan(target, deploy.launcher_text(launcher), deploy.Manifest.load()), fallback


def _earlier(state: State) -> bool:
    """Whether there are settings to take over: a .env to migrate or an earlier checkout."""
    if common.legacy_env():
        return False
    if wants_offer(state.previous):
        return True
    try:
        return config_store.plan_migration().status == "ready"
    except EXPECTED:
        return False


class SetupScreen(Screen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, options: SetupOptions | None = None) -> None:
        super().__init__()
        self.options = options or SetupOptions()
        self.order = self._steps(earlier=False)
        self.at = "welcome"
        self.plan: deploy.Plan | None = None
        self.plan_error: str | None = None
        self.tested: bool | None = None
        self.working = False
        self.keys: list[setup_guide.KeyRow] = []

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
                    with VerticalScroll(id="setup-earlier"):
                        yield Static("", id="setup-earlier-text", markup=False)
                        yield Horizontal(
                            self._button("setup-migrate-env", "Move the .env settings…"),
                            self._button("setup-previous", "Earlier install…"),
                            classes="buttons",
                        )
                    with VerticalScroll(id="setup-provider"):
                        yield Static(setup_guide.PROVIDER_INTRO, classes="note", markup=False)
                        yield OptionList(id="setup-providers")
                    with VerticalScroll(id="setup-profile"):
                        yield Static(setup_guide.PROFILE_INTRO, classes="note", markup=False)
                        yield OptionList(id="setup-profiles")
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
                    with VerticalScroll(id="setup-espanso"):
                        yield Static(setup_guide.ESPANSO_INTRO, classes="note", markup=False)
                        yield Static("Reading the match folder…", id="setup-files", markup=False)
                        yield Horizontal(
                            self._button("setup-deploy", "Install the match files"),
                            classes="buttons",
                        )
                    with VerticalScroll(id="setup-test"):
                        yield Static(setup_guide.TEST_INTRO, classes="note", markup=False)
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

    def _steps(self, earlier: bool) -> list[str]:
        skipped = {"espanso"} if self.options.deploy is False else set()
        if not self.options.smoke_test:
            skipped.add("test")
        earlier = earlier or self.options.migrate_from is not None
        return [s for s in setup_guide.steps(earlier) if s not in skipped]

    @staticmethod
    def _button(id_: str, label: str) -> Button:
        return Button(label, id=id_, tooltip=teach.tooltip(id_))

    @property
    def manage(self) -> ManageApp:
        return cast("ManageApp", self.app)

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
        self.order = self._steps(_earlier(state))
        if self.at not in self.order:
            self.at = "provider"
        notes = []
        if common.legacy_env():
            notes.append(
                "PROMPTMEND_ENV is set, so your settings and keys stay in that file; this "
                "setup cannot save them (edit that file instead)."
            )
        notes.append(_history_line(state))
        self.query_one("#setup-welcome-notes", Static).update("\n".join(notes))
        self._show_earlier(state)
        self._fill(
            "#setup-providers",
            setup_guide.provider_choices(state.settings, state.triggers),
            self.options.provider,
        )
        self._fill(
            "#setup-profiles",
            setup_guide.profile_choices(state.settings, state.profiles),
            self.options.profile,
        )
        self.keys = setup_guide.key_rows(state.layers, state.settings, state.triggers)
        legacy = common.legacy_env()
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
        self.query_one("#setup-servers", Static).update(servers.message if servers else "")
        self._refresh_plan()
        self.go(self.at)

    def _show_earlier(self, state: State) -> None:
        lines = []
        try:
            migration = config_store.plan_migration()
        except EXPECTED as exc:
            migration = None
            lines.append(error_message(exc))
        ready = migration is not None and migration.status == "ready"
        if ready and migration is not None:
            lines.append(
                "Your settings are in a .env. Moving them to config.toml and the secret store "
                "lets this setup and the interface change them:"
            )
            lines += [f"  {line}" for line in migration.describe()]
        offer = wants_offer(state.previous) or self.options.migrate_from is not None
        if self.options.migrate_from is not None:
            lines.append(f"Earlier install… looks at {self.options.migrate_from}.")
        elif offer:
            lines.append(
                "An earlier install ran from a checkout of this project. Earlier install… "
                "copies its settings, keys and profiles, step by step."
            )
        if not lines:
            lines.append("Nothing to take over.")
        self.query_one("#setup-earlier-text", Static).update("\n".join(lines))
        self.query_one("#setup-migrate-env", Button).display = ready
        self.query_one("#setup-previous", Button).display = offer

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
        keep = highlighted or preset or next((c.value for c in choices if c.current), None)
        if keep is not None:
            try:
                options.highlighted = options.get_option_index(keep)
            except OptionDoesNotExist:
                options.highlighted = 0

    def go(self, step: str) -> None:
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
        self.query_one("#setup-back", Button).disabled = step == self.order[0]
        self.query_one("#setup-nav").display = step != "done"
        if step == "done":
            self._show_done()
        focus = {"provider": "#setup-providers", "profile": "#setup-profiles"}.get(step)
        if (
            step == "espanso"
            and self.options.deploy
            and not self.query_one("#setup-deploy", Button).disabled
        ):
            focus = "#setup-deploy"
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
        if self.at == "provider" and not self._save_picked("PROMPT_PROVIDER", "#setup-providers"):
            return
        if self.at == "profile" and not self._save_picked("PROMPT_PROFILE", "#setup-profiles"):
            return
        if self.at == "keys" and not self._save_keys():
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

    def _save_picked(self, name: str, selector: str) -> bool:
        option = self.query_one(selector, OptionList).highlighted_option
        if option is None or option.id is None:
            return True
        value = option.id
        legacy = common.legacy_env()
        if legacy:
            # Nothing can be saved (as headless setup's writable=False): say so and move on.
            if setup_cmd.current_choice(name) != value:
                self.app.notify(
                    f"{name} not saved: {config.env_file_var()} is set, so settings stay in "
                    f"{legacy}; edit that file instead.",
                    severity="warning",
                    markup=False,
                )
            return True
        try:
            message, argv = setup_cmd.save_choice(name, value)
        except Exception as exc:
            command = teach.equivalent("config", *setup_cmd.choice_command(name, value))
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
        if not typed or common.legacy_env():
            return True

        def save() -> str:
            saved = []
            for name, value in typed:
                settings_cmd.store_key(name, value)
                saved.append("; ".join([f"{name} saved", *settings_cmd.after_save(name)]))
            return "; ".join(saved)

        command = "; ".join(teach.for_setting("set-key", name) for name, _ in typed)
        ok = self.attempt(save, command=command)
        if ok:
            for name, _ in typed:
                self.query_one(f"#setup-key-{name}", Input).value = ""
        return ok

    @on(Button.Pressed, "#setup-save-keys")
    def _save_keys_pressed(self) -> None:
        self._save_keys()

    @on(Button.Pressed, "#setup-migrate-env")
    def _migrate_env(self) -> None:
        confirm_migration(
            self,
            title="Move the .env settings?",
            confirm="Move",
            note="A backup is kept.",
            done=lambda result: (
                f"Moved; backup in {result.backup}. `promptmend config rollback` undoes it."
            ),
        )

    @on(Button.Pressed, "#setup-previous")
    def _previous(self) -> None:
        self.manage.open_previous(self.options.migrate_from)

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
        button.disabled = plan.is_noop or plan.only_forgets
        if self.at == "done":
            self._show_done()

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
            outcome = deploy.apply(plan, {})  # an edited file is kept, never overwritten
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

    @on(Button.Pressed, "#setup-run-test")
    def _test(self) -> None:
        if self.working or self.manage.state is None:
            return
        self.working = True
        self.report("Running the test against the stub…")
        self._run_test(self.manage.state.settings.provider)

    @work(thread=True, group="setup-action", exit_on_error=False)
    def _run_test(self, provider: str) -> None:
        try:
            result = smoke.run(provider)
            ok, message = result.ok, result.message
        except Exception as exc:
            ok, message = False, error_message(exc)
        self.app.call_from_thread(self._tested, provider, ok, message)

    def _tested(self, provider: str, ok: bool, message: str) -> None:
        self.working = False
        self.tested = ok
        text = f"passed: {message}" if ok else f"failed (improve --provider {provider}): {message}"
        self.report(text, error=not ok)

    # The last step.

    def _show_done(self) -> None:
        state = self.manage.state
        if state is None:
            return
        summary = Text()
        for row in setup_guide.done_rows(
            state.settings, self.keys, self.plan, self.tested, common.legacy_env()
        ):
            summary.append("✓ " if row.ok else "✗ ", style="green" if row.ok else "yellow")
            summary.append(f"{row.label}: ", style="bold")
            summary.append(f"{row.text}\n")
        self.query_one("#setup-summary", Static).update(summary)
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


def _history_line(state: State) -> str:
    if not state.settings.history:
        return "Usage history is off; nothing is recorded."
    return (
        "Usage history is on: metadata only (trigger, model, outcome, tokens, cost), never "
        "your text or keys, kept on this device. Off: `promptmend config set PROMPT_HISTORY "
        "false`."
    )


def needs_setup(state: State) -> bool:
    """Nothing is set up yet: no config.toml, no key, and no match file installed. A bare
    `promptmend` then opens the wizard by itself, once."""
    if common.legacy_env() or state.plan is None:
        return False
    if config_files.is_file(config.settings_file()):
        return False
    if any(state.layers.entries[name].value for name in secret_names()):
        return False
    return all(step.state == deploy.MISSING for step in state.plan.steps)
