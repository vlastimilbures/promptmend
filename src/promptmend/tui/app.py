"""The interface's app: seven tabs over one State, read again in a worker thread after every
change, a high-contrast theme, and key bindings that every terminal delivers (letters, digits
and Ctrl; no Alt or Cmd). Textual honours NO_COLOR itself.

Only Home is built at start: every other tab's pane, and its module, the first time
that tab opens, and the setup wizard the first time it is opened, so the first paint loads
none of them (tests/test_ui_startup.py pins what it loads). A pane built later shows the
current State as it mounts (Pane.on_mount).

Each reload has two stages, one after the other in its worker: the quick State
(settings, profiles, triggers; local reads of a few milliseconds) is read on the app's loop
and shown at once, the full one, read in a thread, replaces it. Every screen gets
the same State object at each stage. The generation guard orders them: a stage of an
older reload never replaces a newer reload's, so a screen goes quick, full, late update
within one reload, and a newer reload's quick State supersedes everything older."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, ClassVar, cast

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import Footer, Header, TabbedContent, TabPane

from .. import installed_version
from ..update_check import UpdateStatus
from . import brand, teach
from .base import Pane
from .home import TAB_LABELS, status_pill
from .home_pane import HomePane
from .intro import AboutScreen
from .state import State, _beside, gather, needs_setup, quick

if TYPE_CHECKING:
    from .setup_wizard import SetupOptions, SetupScreen

DEFAULT_THEME = "textual-dark"
# Full reads (doctor, the plan, the stats) running at once, see ManageApp._read_full().
FULL_READS = 3
# The GIL switch interval while the interface runs on Windows with Python 3.12, see
# threads_get_their_turn().
WINDOWS_312_SWITCH_INTERVAL = 0.0005


@contextlib.contextmanager
def threads_get_their_turn() -> Iterator[None]:
    """While the interface runs on Windows with Python < 3.13, a shorter GIL switch interval,
    the previous one restored after. There the app's loop keeps the GIL so much that every
    other thread (the full read, the update check, the setup plan) waits about one interval
    each time it gets it back after a system call: a quick() that takes 3 ms on the loop
    took 11-13 s in a thread while the app ran, against 3 ms on 3.13 and 3.14 (#348).
    Process-wide, so only for the interface's run: never the triggers or the headless
    commands, which never import this module."""
    if sys.platform != "win32" or sys.version_info >= (3, 13):
        yield
        return
    previous = sys.getswitchinterval()
    sys.setswitchinterval(WINDOWS_312_SWITCH_INTERVAL)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


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

# Tab id -> (label, the pane's module and class in this package). The digit in each label is
# its key. Named, not imported: a pane's module loads when its tab first opens.
_PANES: dict[str, tuple[str, str]] = {
    "home": ("home_pane", "HomePane"),
    "settings": ("settings_pane", "SettingsPane"),
    "profiles": ("profiles_pane", "ProfilesPane"),
    "triggers": ("triggers_pane", "TriggersPane"),
    "history": ("history_pane", "HistoryPane"),
    "diagnostics": ("diagnostics_pane", "DiagnosticsPane"),
    "try": ("try_pane", "TryPane"),
}
TABS: dict[str, tuple[str, tuple[str, str]]] = {
    tab: (TAB_LABELS[tab], pane) for tab, pane in _PANES.items()
}
_SETUP_MODULE = f"{__package__}.setup_wizard"


def pane_class(tab: str) -> type[Pane]:
    """``tab``'s pane, its module imported now if it is not yet."""
    module, name = _PANES[tab]
    return cast("type[Pane]", getattr(importlib.import_module(f".{module}", __package__), name))


def _setup_screen_class() -> type[SetupScreen]:
    from .setup_wizard import SetupScreen

    return SetupScreen


CSS = """
Pane { padding: 0 1; }
/* Below 80 columns one space between tabs, not two, so all seven fit 70 columns (#394). */
MainScreen.-narrow ContentTab { padding: 0 0 0 1; }
.note { color: $text-muted; margin-bottom: 1; }
DataTable { height: auto; max-height: 14; margin-bottom: 1; }
/* The Triggers tab (#396): a Provider or Profile cell a setting supplies is two lines, so
   the table takes the height the note, the buttons and the deploy target leave and scrolls
   inside itself, and Show diff, Deploy and Detach (under the table, above the target) stay
   on screen down to 70x24. The result sits right under the buttons. */
#triggers { height: 1fr; max-height: 100%; min-height: 3; }
#triggers-pane .result { margin-top: 0; }
#policy, #deploy-target, #disclosure, #layers, #findings, #all-checks, #profile-settings {
    margin-bottom: 1;
}
#group-by { width: 30; margin-bottom: 1; }
.buttons { height: auto; margin-top: 1; }
.buttons Button { margin-right: 1; }
/* Home's buttons (its fix button and three more) as wide as their labels, so the row fits
   70 columns (#111). */
#home-pane .buttons Button { min-width: 0; }
.result { margin-top: 1; }
#home-headline { margin: 1 0; }
/* Home's rows (#395): a list with no border or padding of its own, so they line up as the
   plain rows did; the highlight marks the row Enter opens. */
#home-rows { height: auto; border: none; padding: 0; background: transparent; }
#home-session { margin-top: 1; text-wrap: nowrap; text-overflow: ellipsis; }
/* The Settings tab (#199): a one-line filter, the list filling the tab, two help lines. */
#settings-pane { overflow-y: hidden; }
#settings-top { height: 1; }
#settings-filter { border: none; height: 1; padding: 0 1; width: 1fr; }
#settings-count { width: auto; color: $text-muted; padding: 0 1; }
#settings-list { height: 1fr; min-height: 4; border: none; padding: 0; }
/* Headings (bold in their own text) and the two lines under Triggers (italic) are disabled
   options: in the normal text colour, not dimmed (#389). */
#settings-list > .option-list--option-disabled { color: $text; }
#settings-help { height: 2; }
#settings-pane .result { margin-top: 0; height: auto; max-height: 3; }
#try-draft { height: 6; }
#try-options { height: auto; margin-top: 1; }
/* The four compact pickers in one row (#394): Provider and Tier as wide as their longest
   value ("openrouter", "standard") plus the arrow, the other two share the rest, so at 70
   columns "Real provider" and "as configured" fit; a longer value (a profile's name) is cut,
   never wrapped. */
.try-option { width: 1fr; height: auto; margin-right: 1; }
#try-provider-option { width: 14; }
#try-tier-option { width: 12; }
#try-options SelectCurrent Static#label { text-wrap: nowrap; text-overflow: ellipsis; }
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
/* No border: the Cloud or local step (its question, three choices and the note) fits at
   80x24 with the list focused; the highlight marks the choice. */
#setup-steps OptionList { height: auto; max-height: 14; border: none; padding: 0; }
#setup-use-intro, #setup-use-note { margin: 0; }
#setup-welcome-notes { margin-top: 1; }
#setup-steps Input { margin-bottom: 1; }
#setup-next { margin-top: 1; text-style: bold; }
#setup-nav { height: auto; }
"""


class LateUpdate(Message):
    """The update check of reload ``generation`` landed: ``state`` is that reload's State with
    it filled in."""

    def __init__(self, state: State, generation: int) -> None:
        super().__init__()
        self.state = state
        self.generation = generation


class MainScreen(Screen[None]):
    # The screen has the class -narrow below 80 columns, -wide from 80 on (the tab bar's
    # spacing, see CSS). Not annotated ClassVar: Textual's own `ClassVar[...] | None` reads to
    # mypy as an instance variable, which a ClassVar cannot override.
    HORIZONTAL_BREAKPOINTS = [  # noqa: RUF012
        (0, "-narrow"),
        (80, "-wide"),
    ]
    # On this screen, not the app, so no letter reaches the app while a dialog is open. About
    # and High contrast stay off the footer, so Quit fits it at 70 columns on every tab (#394);
    # docs/interface.md lists both keys.
    BINDINGS: ClassVar[list[BindingType]] = [
        *(
            Binding(str(n), f"show('{tab}')", label, show=False)
            for n, (tab, (label, _)) in enumerate(TABS.items(), start=1)
        ),
        Binding("a", "app.about", "About", show=False),
        Binding("r", "app.reload", "Reload"),
        Binding("t", "app.toggle_contrast", "High contrast", show=False),
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="home"):
            for tab, (label, _) in TABS.items():
                with TabPane(label, id=tab):
                    if tab == "home":
                        yield HomePane(id="home-pane")
        yield Footer()

    def built(self, tab: str) -> Pane | None:
        """``tab``'s pane, or None while that tab has not been opened."""
        found = self.query(f"#{tab}-pane").results(Pane)
        return next(found, None)

    async def build(self, tab: str) -> Pane:
        """``tab``'s pane, built (and its module imported) the first time; it shows the
        current State as it mounts."""
        pane = self.built(tab)
        if pane is None:
            pane = pane_class(tab)(id=f"{tab}-pane")
            await self.query_one(f"TabPane#{tab}", TabPane).mount(pane)
        return pane

    async def action_show(self, tab: str) -> None:
        # Built before it is shown, so a digit never shows the tab empty for a frame.
        await self.build(tab)
        self.query_one(TabbedContent).active = tab

    @on(TabbedContent.TabActivated)
    async def _tab_shown(self, event: TabbedContent.TabActivated) -> None:
        """A tab opened by a click or the arrows is built here. The Settings list takes the
        focus as its tab opens, so the arrows move in it at once; leaving the tab drops
        it, so its keys (`u`, Space) never act unseen."""
        tab = event.pane.id
        if tab in TABS:
            await self.build(tab)
        settings = self.built("settings")
        if settings is None:
            return
        if tab == "settings":
            self.call_after_refresh(self._focus_settings_list)
        elif self.focused is not None and settings in self.focused.ancestors_with_self:
            self.set_focus(None)

    def on_screen_resume(self) -> None:
        """Back on the tabs (a dialog or About closed): a first-run offer held back while it
        was open comes now; it does nothing when there is none."""
        cast("ManageApp", self.app)._maybe_offer()

    def _focus_settings_list(self) -> None:
        """After the refresh, once the Settings pane is mounted and filled (looked up then, so
        a tab activated while it is still mounting never queries a list not there yet)."""
        settings = self.built("settings")
        # Only while Settings is still the active tab: focus in a hidden pane would make its
        # tab active again (#395 review).
        if self.query_one(TabbedContent).active != "settings":
            return
        if settings is not None and settings.ready:
            settings.query_one("#settings-list").focus()


class ManageApp(App[int]):
    TITLE = brand.NAME  # Plus the installed version, set in __init__.
    SUB_TITLE = "set up and manage"
    CSS = CSS
    # The palette would offer screenshots written to the working directory and other extras
    # with no headless counterpart.
    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        *,
        loader: Callable[[str, State | None], State] = gather,
        quick_loader: Callable[[str], State] | None = quick,
        setup: SetupOptions | None = None,
        first_run: bool = False,
    ) -> None:
        super().__init__()
        # Read here, not at import, so the snapshot tests can pin it and a release changes none.
        self.title = f"{self.TITLE} {installed_version()}"
        # The full read: given the group-by and this reload's quick State (or None), whose
        # settings, profiles and triggers it reuses (gather's ``first``).
        self.loader = loader
        # The first stage; None: one stage, the loader's State only.
        self.quick_loader = quick_loader
        self.group_by = "trigger"
        self.state: State | None = None
        # Each reload's number. A thread cannot be stopped, so a slower, older load may finish
        # after a newer one: only the newest is shown.
        self.generation = 0
        # The reload whose State is on screen (0: none yet).
        self.shown = 0
        # Full reads at once, a cancelled reload's thread counted until it ends: a hung
        # Espanso and a held `r` cannot start thread after thread (and child after child).
        self.full_reads = asyncio.Semaphore(FULL_READS)
        self.main = MainScreen()
        # This session's actions as commands, shown on Home.
        self.session: list[teach.Entry] = []
        # `setup` opens the wizard at once; `first_run` opens it by itself once when nothing
        # is set up yet (a bare `promptmend`; off in the tests).
        self.setup_options = setup
        self.first_run = first_run
        self.register_theme(HIGH_CONTRAST)
        self.theme = DEFAULT_THEME

    async def _process_messages(self, *args: Any, **kwargs: Any) -> None:
        """The whole of the app's run, run() and run_test() alike (Textual's own entry point
        for both), with threads_get_their_turn() around it."""
        with threads_get_their_turn():
            await super()._process_messages(*args, **kwargs)

    def on_mount(self) -> None:
        self.push_screen(self.main)
        self.reload()
        if self.setup_options is not None:
            self.first_run = False
            self.push_screen(_setup_screen_class()(self.setup_options))

    def _about_facts(self) -> list[str]:
        report = self.state.report if self.state else None
        install = (
            next((c.data for c in report.checks if c.id == "install"), None) if report else None
        )
        if self.state is not None and not self.state.full and self.state.failed is None:
            install = {"channel": "checking"}  # doctor's install check is still running
        update = self.state.update if self.state else None
        return brand.about_facts(install, installed_version(), update)

    def action_about(self) -> None:
        self.push_screen(AboutScreen(self._about_facts()))

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

    async def _read_full[T](self, read: Callable[[], T]) -> T:
        """``read()`` in a daemon thread of its own (state._beside), awaited here, once fewer
        than FULL_READS full reads are running. Not a thread worker: those share asyncio's
        default thread pool, which a slow full read (doctor waiting on Espanso) would hold,
        and on Windows Textual's own timers sleep in that pool too. Cancelled (a newer
        reload), the thread still ends on its own and its answer is dropped; its slot is given
        back when the thread ends, since a thread cannot be stopped. A newer reload waiting
        for one still shows its quick State."""
        await self.full_reads.acquire()
        loop = asyncio.get_running_loop()

        def free(_: Future[T]) -> None:
            with contextlib.suppress(RuntimeError):  # the app's loop closed: nothing to free
                loop.call_soon_threadsafe(self.full_reads.release)

        try:
            future = _beside(read, "promptmend-reload")
        except BaseException:  # no thread started (RuntimeError: can't start new thread)
            self.full_reads.release()
            raise
        future.add_done_callback(free)
        return await asyncio.wrap_future(future)

    @work(exclusive=True, group="load", exit_on_error=False)
    async def _load(self, generation: int) -> None:
        """One reload: the quick State, read here on the app's loop, then the full one, read
        in a thread; both shown here. A newer reload cancels this one (exclusive), and the
        generation guard in _show() drops whatever an older one would still show."""
        first: State | None = None
        group_by, quick_loader = self.group_by, self.quick_loader
        if quick_loader is not None:
            try:
                # On the loop, not in a thread: local reads of a few milliseconds, so the
                # first paint never waits for a thread to be scheduled (on Windows with
                # Python 3.12 a thread can wait seconds for its turn while the app runs;
                # see threads_get_their_turn()). The update answer on screen stays until
                # the full State has its own.
                first = quick_loader(group_by).known_update(self.state)
            except Exception:  # noqa: S110 - the full read below reads the same and reports it
                pass
            else:
                self._show(first, generation)
        try:
            state = await self._read_full(lambda: self.loader(group_by, first))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.notify(f"Could not read the state: {error}", severity="error", markup=False)
            # The quick State stays, its waiting rows saying they were not checked. With no
            # quick State of its own, a still-waiting one on screen (an older reload's, whose
            # full State the guard drops) says so too, never `checking` for ever.
            waiting = self.state if first is None else first
            if waiting is not None and not waiting.full and waiting.failed is None:
                self._show(waiting.not_checked(error), generation)
            return
        self._show(state, generation)
        if state.pending is not None:
            # The update check lands after the rest. Nothing waits for it, so this
            # worker ends now and quitting never waits either; when it lands, its thread
            # posts the next State (post_message is thread-safe and never blocks). Only a
            # full State has one pending.
            full = state

            def landed(done: Future[UpdateStatus]) -> None:
                if generation == self.shown:
                    self.post_message(LateUpdate(full.landed(done), generation))

            state.pending.add_done_callback(landed)

    @on(LateUpdate)
    def _late_update(self, message: LateUpdate) -> None:
        """The same moment with the update check filled in: every pane gets the new State
        object, but only what shows the update repaints (Home, the Checks tab, an
        open About), so no table cursor moves and the setup screen keeps its focus."""
        # The reload on screen: a newer one that failed showed nothing, so this one's Version
        # row still fills in; one that showed its own State (quick or full) drops this.
        if message.generation != self.shown or self.state is None or not self.state.full:
            return
        state = message.state
        self.state = state
        self.sub_title = f"{status_pill(state)} · {self.SUB_TITLE}"
        for pane in self.main.query(Pane):
            if pane.ready:
                pane.show_update(state)
        for screen in self.screen_stack:
            if isinstance(screen, AboutScreen) and screen.is_mounted:
                screen.show_facts(self._about_facts())

    def _show(self, state: State, generation: int) -> None:
        """One stage of reload ``generation`` on every screen: the same State object for
        each. A stage of an older reload is dropped; within one reload the worker shows
        the quick State before the full one."""
        if generation != self.generation:
            return  # an older load that finished late
        self.state = state
        self.shown = generation
        self.sub_title = f"{status_pill(state)} · {self.SUB_TITLE}"
        for pane in self.main.query(Pane):
            if pane.ready:
                pane.paint(state)
        wizard = self._open_wizard()
        if wizard is not None and wizard.is_mounted:
            wizard.show(state)
        for screen in self.screen_stack:
            if isinstance(screen, AboutScreen) and screen.is_mounted:
                screen.show_facts(self._about_facts())
        self._maybe_offer()

    def _open_wizard(self) -> SetupScreen | None:
        """The setup wizard when it is open. Its module is imported only when it opens, so
        until then there is none to look for."""
        if _SETUP_MODULE not in sys.modules:
            return None
        wizard = _setup_screen_class()
        return next((s for s in self.screen_stack if isinstance(s, wizard)), None)

    def _maybe_offer(self) -> None:
        """Open the setup wizard by itself, once, when nothing is set up yet: asked of the
        full State only, which has the plan. The offer is used up once the wizard is
        open (opened by hand during the quick stage, say); a dialog or About over the tabs
        keeps it for the next State, so it never opens over them."""
        if self.state is None or not self.state.full or not self.first_run:
            return
        if self._open_wizard() is not None:
            self.first_run = False
            return
        if self.screen is not self.main:
            return
        self.first_run = False
        if needs_setup(self.state):
            self.open_setup()

    def open_setup(self) -> None:
        """Show the setup wizard over the tabs, unless it is already open. Opened by hand, it
        uses up the first run's offer."""
        self.first_run = False
        if self._open_wizard() is None:
            self.push_screen(_setup_screen_class()())
