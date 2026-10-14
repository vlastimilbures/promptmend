"""The interface's app: seven tabs over one State, read again in a worker thread after every
change, a high-contrast theme, and key bindings that every terminal delivers (letters, digits
and Ctrl; no Alt or Cmd). Textual honours NO_COLOR itself."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import Footer, Header, Input, TabbedContent, TabPane

from .. import __version__
from . import brand, teach
from .base import Pane
from .diagnostics_pane import DiagnosticsPane
from .history_pane import HistoryPane
from .home import TAB_LABELS, pill
from .home_pane import HomePane
from .intro import AboutScreen, IntroScreen
from .previous import PreviousInstallScreen, wants_offer
from .profiles_pane import ProfilesPane
from .settings_pane import SettingsPane
from .setup_wizard import SetupOptions, SetupScreen, make_plan, needs_setup
from .state import State, detect_previous, gather
from .triggers_pane import DeployTarget, TriggersPane
from .try_pane import TryPane

DEFAULT_THEME = "textual-dark"
HIGH_CONTRAST = Theme(
    name="high-contrast",
    primary="#ffff00",
    secondary="#00ffff",
    accent="#ff00ff",
    foreground="#ffffff",
    background="#000000",
    surface="#000000",
    panel="#1a1a1a",
    success="#00ff00",
    warning="#ffff00",
    error="#ff6060",
    dark=True,
)

# Tab id -> (label, pane). The digit in each label is its key.
_PANES: dict[str, type[Pane]] = {
    "home": HomePane,
    "settings": SettingsPane,
    "profiles": ProfilesPane,
    "triggers": TriggersPane,
    "history": HistoryPane,
    "diagnostics": DiagnosticsPane,
    "try": TryPane,
}
TABS: dict[str, tuple[str, type[Pane]]] = {
    tab: (TAB_LABELS[tab], pane) for tab, pane in _PANES.items()
}

CSS = """
Pane { padding: 0 1; }
.note { color: $text-muted; margin-bottom: 1; }
DataTable { height: auto; max-height: 14; margin-bottom: 1; }
#policy, #deploy-target, #disclosure, #layers, #findings, #all-checks, #profile-settings {
    margin-bottom: 1;
}
#group-by { width: 30; margin-bottom: 1; }
.buttons { height: auto; margin-top: 1; }
.buttons Button { margin-right: 1; }
/* Home's four buttons as wide as their labels, so the row fits 70 columns (#111). */
#home-pane .buttons Button { min-width: 0; }
.result { margin-top: 1; }
#home-headline { margin: 1 0; }
#home-rows { height: auto; }
#home-command { margin-top: 1; }
#home-command-help { color: $text-muted; }
#home-output { height: 12; margin-top: 1; border: round $panel-lighten-2; }
#home-session { margin-top: 1; text-wrap: nowrap; text-overflow: ellipsis; }
/* The Settings tab (#199): a one-line filter, the list filling the tab, two help lines. */
#settings-pane { overflow-y: hidden; }
#settings-top { height: 1; }
#settings-filter { border: none; height: 1; padding: 0 1; width: 1fr; }
#settings-count { width: auto; color: $text-muted; padding: 0 1; }
#settings-list { height: 1fr; min-height: 4; border: none; padding: 0; }
#settings-list > .option-list--option-disabled { color: $text; text-style: bold; }
#settings-help { height: 2; }
#settings-pane .buttons { margin-top: 0; }
#settings-pane .buttons { height: 1; }
#settings-pane .buttons Button { min-width: 0; height: 1; border: none; }
#settings-pane .result { margin-top: 0; height: auto; max-height: 3; }
#try-draft { height: 6; }
#try-options { height: auto; margin-top: 1; }
.try-option { width: 1fr; height: auto; margin-right: 1; }
#try-usage { margin-top: 1; color: $text-muted; }
#try-result { height: 10; }
ModalScreen { align: center middle; }
.dialog {
    width: 100; max-width: 95%; height: auto; max-height: 90%;
    border: thick $primary; background: $surface; padding: 1 2;
}
.dialog-title { text-style: bold; margin-bottom: 1; }
.preview { height: auto; max-height: 18; border: round $panel-lighten-2; margin-bottom: 1; }
.dialog Select, .dialog Input { margin-bottom: 1; }
#previous-found { margin-bottom: 1; }
#previous-steps, .step { height: auto; }
.step Button { width: 34; margin-right: 1; }
.step-state { width: 1fr; padding-top: 1; }
/* The setup wizard: the steps on the left, one step at a time on the right. */
#setup { height: 1fr; }
#setup-rail { width: 20; padding: 1 1 1 2; border-right: tall $panel-lighten-2; }
#setup-body { padding: 1 2; }
#setup-title { text-style: bold; margin-bottom: 1; }
#setup-steps { height: 1fr; }
#setup-steps OptionList { height: auto; max-height: 14; }
#setup-welcome-notes { margin-top: 1; }
#setup-steps Input { margin-bottom: 1; }
#setup-next { margin-top: 1; text-style: bold; }
#setup-nav { height: auto; }
"""


class MainScreen(Screen[None]):
    # On this screen, not the app, so no letter reaches the app while a dialog is open.
    BINDINGS: ClassVar[list[BindingType]] = [
        *(
            Binding(str(n), f"show('{tab}')", label, show=False)
            for n, (tab, (label, _)) in enumerate(TABS.items(), start=1)
        ),
        Binding("c", "command", "Command"),
        Binding("a", "app.about", "About"),
        Binding("r", "app.reload", "Reload"),
        Binding("t", "app.toggle_contrast", "High contrast"),
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="home"):
            for tab, (label, pane) in TABS.items():
                with TabPane(label, id=tab):
                    yield pane(id=f"{tab}-pane")
        yield Footer()

    def action_show(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    @on(TabbedContent.TabActivated)
    def _tab_shown(self, event: TabbedContent.TabActivated) -> None:
        """The Settings list takes the focus as its tab opens (#199), so the arrows move in it
        at once; leaving the tab drops it, so its keys (`u`, Space) never act unseen."""
        settings = self.query_one("#settings-pane")
        if event.pane.id == "settings":
            self.call_after_refresh(settings.query_one("#settings-list").focus)
        elif self.focused is not None and settings in self.focused.ancestors_with_self:
            self.set_focus(None)

    def action_command(self) -> None:
        """Home's command line (#111), focused. Typed into, it keeps every letter and digit
        (its own), so no binding here fires until Escape leaves it."""
        self.action_show("home")
        self.query_one("#home-command", Input).focus()


class ManageApp(App[int]):
    TITLE = brand.NAME  # Plus the installed version, set in __init__ (#112).
    SUB_TITLE = "set up and manage"
    CSS = CSS
    # The palette would offer screenshots written to the working directory and other extras
    # with no headless counterpart.
    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        *,
        loader: Callable[[str], State] = gather,
        intro: bool = True,
        setup: SetupOptions | None = None,
        first_run: bool = False,
    ) -> None:
        super().__init__()
        # Whether the intro opens (#112); it then stays until a key or click (#173). Off for
        # `ui --no-intro`, PROMPT_UI_INTRO=false and the tests.
        self.intro = intro
        # Read here, not at import, so the snapshot tests can pin it and a release changes none.
        self.title = f"{self.TITLE} {__version__}"
        self.loader = loader
        self.group_by = "trigger"
        self.state: State | None = None
        # Each reload's number. A thread cannot be stopped, so a slower, older load may finish
        # after a newer one: only the newest is shown.
        self.generation = 0
        self.main = MainScreen()
        # This session's actions as commands (#111), shown on Home.
        self.session: list[teach.Entry] = []
        # The previous install screen opens by itself once per session (#110).
        self.offered = False
        # `setup` opens the wizard at once; `first_run` opens it by itself once when nothing
        # is set up yet (a bare `promptmend`; off in the tests).
        self.setup_options = setup
        self.first_run = first_run
        self.register_theme(HIGH_CONTRAST)
        self.theme = DEFAULT_THEME

    def on_mount(self) -> None:
        self.push_screen(self.main)
        self.reload()
        if self.setup_options is not None:
            self.first_run = False
            self.offered = True  # its Earlier settings step offers the previous install
            self.push_screen(SetupScreen(self.setup_options))
        elif self.intro:
            # Over the main screen while the state loads; the previous install offer waits
            # until it closes.
            self.push_screen(IntroScreen(__version__), lambda _: self._maybe_offer())

    def action_about(self) -> None:
        report = self.state.report if self.state else None
        install = (
            next((c.data for c in report.checks if c.id == "install"), None) if report else None
        )
        update = self.state.update if self.state else None
        self.push_screen(AboutScreen(brand.about_facts(install, __version__, update)))

    def log_action(self, entry: teach.Entry) -> None:
        """Keep an action in the session log and show it on Home."""
        self.session.append(entry)
        home = self.main.query_one("#home-pane", HomePane)
        if home.ready:
            home.show_session(self.session)

    def reload(self) -> None:
        """Read everything again (settings, doctor, plan, stats) and refill every tab."""
        self.generation += 1
        self._load(self.generation)

    def action_reload(self) -> None:
        self.reload()

    def action_toggle_contrast(self) -> None:
        self.theme = DEFAULT_THEME if self.theme == HIGH_CONTRAST.name else HIGH_CONTRAST.name

    @work(thread=True, exclusive=True, group="load", exit_on_error=False)
    def _load(self, generation: int) -> None:
        try:
            state = self.loader(self.group_by)
            named = self.setup_options
            if named is not None and named.espanso_dir:
                # `setup --espanso-dir`: offer the earlier install deployed there, not in the
                # default folder the loader looked at.
                previous, error = detect_previous(
                    espanso_dir=Path(named.espanso_dir).expanduser(),
                    launcher=Path(named.launcher).expanduser() if named.launcher else None,
                )
                state = dataclasses.replace(state, previous=previous, previous_error=error)
        except Exception as exc:
            self.call_from_thread(
                self.notify,
                f"Could not read the state: {type(exc).__name__}: {exc}",
                severity="error",
                markup=False,
            )
            return
        self.call_from_thread(self._show, state, generation)

    def _show(self, state: State, generation: int) -> None:
        if generation != self.generation:
            return  # an older load that finished late
        self.state = state
        self.sub_title = f"{pill(state.report)} · {self.SUB_TITLE}"
        for pane in self.main.query(Pane):
            if pane.ready:
                pane.show(state)
        for screen in self.screen_stack:
            if isinstance(screen, (PreviousInstallScreen, SetupScreen)) and screen.is_mounted:
                screen.show(state)
        self._maybe_offer()

    def _maybe_offer(self) -> None:
        """Open the previous install screen by itself, once per session, when the state found
        one, and not while the intro is still showing (its close calls this again)."""
        if self.state is None:
            return
        if any(isinstance(s, (IntroScreen, SetupScreen)) for s in self.screen_stack):
            return
        if self.first_run:
            self.first_run = False
            if needs_setup(self.state):
                self.offered = True  # the wizard's Earlier settings step offers it
                self.open_setup()
                return
        if self.offered or not wants_offer(self.state.previous):
            return
        self.open_previous()

    def open_setup(self) -> None:
        """Show the setup wizard over the tabs, unless it is already open."""
        if not any(isinstance(s, SetupScreen) for s in self.screen_stack):
            self.push_screen(SetupScreen())

    def open_previous(
        self, entered: Path | None = None, options: SetupOptions | None = None
    ) -> None:
        """Show the switch from an earlier checkout install, unless it is already open; with
        ``entered`` (`setup --migrate-from`), detected with that path, and with the wizard's
        ``options``, its --espanso-dir and --launcher for detection, deploy and retire."""
        self.offered = True
        if any(isinstance(s, PreviousInstallScreen) for s in self.screen_stack):
            return
        if options is None or not (options.espanso_dir or options.launcher or options.no_restart):
            self.push_screen(PreviousInstallScreen(entered))
            return
        named = options
        args = [
            *(("--espanso-dir", named.espanso_dir) if named.espanso_dir else ()),
            *(("--launcher", named.launcher) if named.launcher else ()),
            *(("--no-restart",) if named.no_restart else ()),
        ]
        self.push_screen(
            PreviousInstallScreen(
                entered,
                espanso_dir=Path(named.espanso_dir).expanduser() if named.espanso_dir else None,
                launcher=Path(named.launcher).expanduser() if named.launcher else None,
                target=DeployTarget(lambda: make_plan(named)[0], named.no_restart, tuple(args)),
            )
        )
