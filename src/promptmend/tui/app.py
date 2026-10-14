"""The interface's app: seven tabs over one State, read again in a worker thread after every
change, a high-contrast theme, and key bindings that every terminal delivers (letters, digits
and Ctrl; no Alt or Cmd). Textual honours NO_COLOR itself."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import Footer, Header, TabbedContent, TabPane

from .. import __version__
from . import brand, teach
from .base import Pane
from .diagnostics_pane import DiagnosticsPane
from .history_pane import HistoryPane
from .home import TAB_LABELS, pill
from .home_pane import HomePane
from .intro import AboutScreen
from .profiles_pane import ProfilesPane
from .settings_pane import SettingsPane
from .setup_wizard import SetupOptions, SetupScreen, needs_setup
from .state import State, gather
from .triggers_pane import TriggersPane
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
        setup: SetupOptions | None = None,
        first_run: bool = False,
    ) -> None:
        super().__init__()
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
            self.push_screen(SetupScreen(self.setup_options))

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
            if isinstance(screen, SetupScreen) and screen.is_mounted:
                screen.show(state)
        self._maybe_offer()

    def _maybe_offer(self) -> None:
        """Open the setup wizard by itself, once, when nothing is set up yet."""
        if self.state is None or not self.first_run:
            return
        if any(isinstance(s, SetupScreen) for s in self.screen_stack):
            return
        self.first_run = False
        if needs_setup(self.state):
            self.open_setup()

    def open_setup(self) -> None:
        """Show the setup wizard over the tabs, unless it is already open."""
        if not any(isinstance(s, SetupScreen) for s in self.screen_stack):
            self.push_screen(SetupScreen())
