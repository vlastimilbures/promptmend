"""The full-screen interface (#93), driven headless through Textual's Pilot: one test per
screen's main action, each through the same services the headless commands use. Espanso,
uv and brew run through a fake runner, the editor and the smoke test are replaced, and no
test calls a provider."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from conftest import resolved_settings
from rich.cells import cell_len
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Select, Static
from textual.worker import WorkerCancelled

from promptmend import (
    config,
    config_store,
    deploy,
    history,
    paths,
    settings_service,
    smoke,
    update_check,
)
from promptmend.factory import PROVIDER_NAMES
from promptmend.history import HistoryStore
from promptmend.prompt_builder import PROFILES
from promptmend.tui import (
    base,
    history_pane,
    home,
    home_pane,
    profiles_pane,
    settings_model,
    settings_pane,
    triggers_pane,
    try_pane,
)
from promptmend.tui.app import HIGH_CONTRAST, TABS, ManageApp
from promptmend.tui.home import pill
from promptmend.tui.modals import ConfirmModal, EditModal, Field, FormModal, PickModal, TextModal
from promptmend.tui.state import FULL, QUICK, State, gather, quick

if TYPE_CHECKING:
    from conftest import SeedHistory
    from textual.app import App
    from textual.widget import Widget
    from typer.testing import Result

    from promptmend.tui.setup_wizard import SetupScreen

# Kept at import, before the espanso fixture replaces it.
REAL_RESOLVE_LAUNCHER = deploy.resolve_launcher
# Built at runtime, so no key-shaped literal lands in the repo (gitleaks).
KEY = "sk-or-v1-" + "cd34" * 16
SIZE = (120, 50)


class FakeRunner:
    def __init__(self, answers: dict[str, str | deploy.CommandFailure]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []
        # The Espanso folder the espanso fixture made (it answers `espanso path config`).
        self.root = Path()

    def __call__(self, argv: list[str]) -> str | deploy.CommandFailure | None:
        self.calls.append(list(argv))
        return self.answers.get(" ".join(argv))


@pytest.fixture
def espanso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Espanso's folder, found and running through a fake runner, and this install's
    launcher resolved to a fixed path."""
    root = tmp_path / "espanso"
    (root / "match").mkdir(parents=True)
    fake = FakeRunner(
        {
            "espanso path config": f"{root}\n",
            "espanso status": "espanso is running",
            "espanso restart": "",
        }
    )
    monkeypatch.setattr(deploy, "run_command", fake)
    launcher = tmp_path / "bin" / "promptmend"
    launcher.parent.mkdir()
    launcher.write_text("", encoding="utf-8")
    monkeypatch.setattr(deploy, "resolve_launcher", lambda **_: deploy.Launcher(launcher, "uv"))
    fake.root = root
    return fake


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Saved mode: config.toml and the secrets file are read and written (per-test dirs)."""
    monkeypatch.delenv("PROMPTMEND_ENV")
    return paths.user_config_dir()


async def settle(pilot: Pilot[int]) -> None:
    """Let every worker (the state load, an action) and what it set off finish."""
    for _ in range(4):
        await pilot.pause()
        # A newer state load cancels an older one (exclusive group "load"), as a deploy's
        # early reload and _done()'s do on a slow runner (#215): that one never completes.
        waits = [worker.wait() for worker in pilot.app.workers]
        # Bounded (#375): a worker that never ends fails here with every thread's stack.
        try:
            results = await asyncio.wait_for(asyncio.gather(*waits, return_exceptions=True), 30)
        except TimeoutError:
            raise AssertionError(f"a worker never ended\n{threads_now(pilot.app)}") from None
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, WorkerCancelled):
                raise result
    await pilot.pause()


def drive(
    scenario: Callable[[ManageApp, Pilot[int]], Awaitable[None]],
    *,
    settled: bool = True,
    size: tuple[int, int] = SIZE,
    **kwargs: Any,
) -> ManageApp:
    """Run ``scenario`` on a ManageApp(**kwargs) in a ``size`` terminal, once every load has
    ended (``settled``) or at once (a scenario whose loader blocks waits itself, see
    until())."""
    app = ManageApp(**kwargs)

    async def main() -> None:
        async with app.run_test(size=size) as pilot:
            if settled:
                await settle(pilot)
            await scenario(app, pilot)
            # A check still on its way would land while run_test() tears the screens down,
            # which repaints a pane whose widgets are gone (#350): it lands here first.
            if app.state is not None and app.state.pending is not None:
                await landed(app, pilot)

    asyncio.run(main())
    return app


def _state(app: ManageApp) -> State:
    assert app.state is not None
    return app.state


def _dialog(app: ManageApp) -> ConfirmModal | FormModal | TextModal:
    screen = app.screen
    assert isinstance(screen, ConfirmModal | FormModal | TextModal)
    return screen


def _plan() -> deploy.Plan:
    plan = gather().plan
    assert plan is not None
    return plan


def test_gather_asks_each_command_once(
    espanso: FakeRunner, saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#242: doctor and the plan share one answer per command per reload."""
    launcher = deploy.resolve_launcher()

    def resolve(*, runner: deploy.Runner | None = None, **_: Any) -> deploy.Launcher:
        (runner or deploy.run_command)(["uv", "tool", "dir"])
        return launcher

    monkeypatch.setattr(deploy, "resolve_launcher", resolve)
    assert gather().plan is not None
    calls = [" ".join(call) for call in espanso.calls]
    assert calls.count("espanso path config") == 1
    assert calls.count("uv tool dir") == 1


def test_a_uv_install_starts_without_a_child_process(
    espanso: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#315: the launcher comes from uv's receipt, so a reload runs no `uv` (nor `brew`):
    only the two read-only Espanso queries, each once."""
    import sys

    monkeypatch.setattr(deploy, "resolve_launcher", REAL_RESOLVE_LAUNCHER)
    prefix = tmp_path / "uv" / "tools" / "promptmend"
    prefix.mkdir(parents=True)
    exe = "promptmend.exe" if os.name == "nt" else "promptmend"
    launcher = tmp_path / "uv-bin" / exe
    launcher.parent.mkdir()
    launcher.write_text("", encoding="utf-8")
    (prefix / deploy.UV_RECEIPT).write_text(
        "[tool]\nentrypoints = [\n"
        f"    {{ name = 'promptmend', install-path = '{launcher}', from = 'promptmend' }},\n]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "prefix", str(prefix))
    state = gather()
    assert state.plan is not None
    assert state.plan.launcher == deploy.launcher_text(launcher)
    install = next(c for c in state.report.checks if c.id == "install")
    assert install.data["channel"] == "uv"
    assert sorted(" ".join(c) for c in espanso.calls) == ["espanso path config", "espanso status"]


def test_gather_makes_one_plan_one_manifest_load_and_one_repair_resolve(
    espanso: FakeRunner, saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#315: doctor reuses the reload's settings, launcher, manifest and plan."""
    counts = {"plan": 0, "load": 0, "repair": 0}
    real_plan, real_load = deploy.plan, deploy.Manifest.load
    real_resolve = config.ConfigLayers.resolve

    def plan(*args: Any, **kwargs: Any) -> deploy.Plan:
        counts["plan"] += 1
        return real_plan(*args, **kwargs)

    def load(path: Path | None = None) -> deploy.Manifest:
        counts["load"] += 1
        return real_load(path)

    def resolve(*args: Any, **kwargs: Any) -> config.ConfigLayers:
        counts["repair"] += kwargs.get("strict") is False
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(deploy, "plan", plan)
    monkeypatch.setattr(deploy.Manifest, "load", staticmethod(load))
    monkeypatch.setattr(config.ConfigLayers, "resolve", staticmethod(resolve))
    state = gather()
    assert state.plan is not None
    assert counts == {"plan": 1, "load": 1, "repair": 1}
    match_files = next(c for c in state.report.checks if c.id == "match_files")
    assert match_files.data["files"] == [
        {"name": s.name, "state": s.state} for s in state.plan.steps
    ]


@pytest.mark.parametrize("linked", [False, True])
def test_gather_shows_the_same_report_as_doctor(
    espanso: FakeRunner, saved: Path, tmp_path: Path, linked: bool
) -> None:
    """What the reload passes doctor changes nothing in its report (#315), for a linked
    Espanso folder too (#339: both plan the resolved folder, so doctor shares the plan and
    sees the deployed files as deployed, an edited one as modified, none as foreign)."""
    from promptmend import doctor

    if linked:
        link = tmp_path / "linked-espanso"
        try:
            link.symlink_to(espanso.root, target_is_directory=True)
        except OSError:
            pytest.skip("no symlinks here")
        espanso.answers["espanso path config"] = f"{link}\n"
    deploy.apply(_plan())
    target = espanso.root / "match" / "prompts-core.yml"
    target.write_text(target.read_text("utf-8") + "# mine\n", "utf-8")
    state = gather().settled()
    assert state.pending is None
    alone = doctor.run(clipboard=False, update=state.update)
    assert state.report.to_json() == alone.to_json()
    match_files = next(c for c in alone.checks if c.id == "match_files")
    states = {f["name"]: f["state"] for f in match_files.data["files"]}
    assert states.pop("prompts-core.yml") == deploy.MODIFIED
    assert set(states.values()) == {deploy.IN_SYNC}


class BlockedPypi:
    """An update check that waits until released, as a proxy that drops pypi.org does."""

    def __init__(self) -> None:
        import threading

        self.release = threading.Event()
        self.answer = update_check.UpdateStatus(
            update_check.AVAILABLE, "99.0.0", "2026-11-15T09:00:00+00:00"
        )

    def __call__(self) -> update_check.UpdateStatus:
        assert self.release.wait(10), f"never released\n{threads_now()}"
        return self.answer


def test_a_blocked_pypi_delays_no_other_row(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptmend.tui.home import home_rows

    blocked = BlockedPypi()
    monkeypatch.setattr(update_check, "check_configured", blocked)
    state = gather()  # returns while the check still waits
    assert state.update.state == update_check.CHECKING
    assert state.pending is not None
    assert not state.pending.done()
    rows = {row.label: row for row in home_rows(state)}
    assert rows["Version"].text.endswith(" · checking")
    assert rows["Triggers"].text.endswith("Espanso running")
    version = next(c for c in state.report.checks if c.id == "version")
    assert version.data["update_available"] is None
    blocked.release.set()
    later = state.settled()
    assert later.update == blocked.answer
    assert later.pending is None
    assert home_rows(later)[0].text == "99.0.0 available"
    version = next(c for c in later.report.checks if c.id == "version")
    assert version.data["latest"] == "99.0.0"
    assert "99.0.0 available" in version.message
    # Only the version check changed; everything else is the same moment.
    assert [c for c in later.report.checks if c.id != "version"] == [
        c for c in state.report.checks if c.id != "version"
    ]
    assert later.plan is state.plan
    assert later.layers is state.layers


def test_an_update_check_that_never_lands_is_unknown(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import Future

    from promptmend.tui import state as state_module

    monkeypatch.setattr(state_module, "SETTLE_TIMEOUT", 0.0)
    never: Future[update_check.UpdateStatus] = Future()
    monkeypatch.setattr(state_module, "_beside", lambda work, name: never)
    state = gather()
    assert state.settled().update == update_check.UpdateStatus(update_check.UNKNOWN)
    broken: Future[update_check.UpdateStatus] = Future()
    broken.set_exception(RuntimeError("boom"))
    monkeypatch.setattr(state_module, "_beside", lambda work, name: broken)
    # Already landed (here: failed), so gather settles it itself.
    assert gather().update == update_check.UpdateStatus(update_check.UNKNOWN)


def test_a_landed_update_check_makes_one_state(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached answer lands before the rest is read: no `checking`, nothing pending."""
    from concurrent.futures import Future

    from promptmend.tui import state as state_module

    landed: Future[update_check.UpdateStatus] = Future()
    landed.set_result(update_check.UpdateStatus(update_check.LATEST, "0.1.0", "x"))
    monkeypatch.setattr(state_module, "_beside", lambda work, name: landed)
    state = gather()
    assert state.pending is None
    assert state.update.state == update_check.LATEST
    assert state.settled() is state


async def focus_a_key_field(wizard: SetupScreen, pilot: Pilot[int]) -> Input:
    """The wizard on its keys step, the focus on the OpenRouter key field, `typed` in it. go()
    moves the focus after the next refresh (to Next, on this step), so the field is focused
    only once every such move has landed: one landing after it took the focus back to Next
    (Windows 3.12, #350). The refresh wait gives the order (the screen runs its after-refresh
    callbacks first in, first out, so go()'s ran before it returns); waiting for Next only
    asserts the premise, that Next is where go() puts the focus."""
    wizard.go("keys")
    try:
        await asyncio.wait_for(wizard.wait_for_refresh(), 10)  # after the go()s' own callbacks
    except TimeoutError:
        raise AssertionError(f"the keys step never refreshed\n{threads_now(pilot.app)}") from None
    next_step = wizard.query_one("#setup-next-step", Button)
    await until(pilot, lambda: pilot.app.focused is next_step, lambda: pilot.app.focused)
    field = wizard.query_one("#setup-key-OPENROUTER_API_KEY", Input)
    field.focus()
    await until(pilot, lambda: pilot.app.focused is field, lambda: pilot.app.focused)
    field.value = "typed"
    await pilot.pause()
    return field


async def landed(app: ManageApp, pilot: Pilot[int]) -> State:
    """Wait (pausing, at most ~10 s) until the late update check is on screen."""
    for _ in range(1000):
        if app.state is not None and app.state.update.state != update_check.CHECKING:
            return app.state
        await pilot.pause(0.01)
    raise AssertionError("the update check never landed")


def test_the_version_row_fills_in_on_every_screen(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#315 and #83: the first State says `checking`; nothing waits for pypi.org (the load
    worker has ended), and when it answers every pane has the same next State, from the same
    reload; an open About screen says so too."""
    from promptmend.tui.intro import AboutScreen

    blocked = BlockedPypi()
    monkeypatch.setattr(update_check, "check_configured", blocked)
    seen: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        first = _state(app)  # settle() returned: no worker waits for the check
        assert first.update.state == update_check.CHECKING
        await open_tab(app, pilot, *(t for t in TABS if t != "try"))
        home = app.main.query_one("#home-pane", home_pane.HomePane)
        assert "· checking" in _rendered(home, "#home-rows")
        await pilot.press("a")
        await settle(pilot)
        assert isinstance(app.screen, AboutScreen)
        seen.append(_rendered(app.screen, "#about-facts"))
        blocked.release.set()
        state = await landed(app, pilot)
        await pilot.pause()
        assert state.update == blocked.answer
        assert state.plan is first.plan
        assert app.state is state
        # Every pane reads app.state (Pane.state), none keeps a State of its own.
        for each in app.main.query(base.Pane):
            assert not [v for v in vars(each).values() if isinstance(v, State)]
        assert "99.0.0 available" in _rendered(home, "#home-rows")
        assert "99.0.0 available" in _rendered(app.screen, "#about-facts")
        diagnostics = pane(app, "diagnostics")
        assert "99.0.0 available" in _rendered(diagnostics, "#all-checks")
        version = next(c for c in state.report.checks if c.id == "version")
        assert version.data["latest"] == "99.0.0"
        # A tab first opened after the update landed shows it too (#316).
        await open_tab(app, pilot, "try")
        assert pane(app, "try").ready
        assert pane(app, "try").state is state

    drive(scenario)
    assert "checking pypi.org" in seen[0]


def test_a_late_update_keeps_cursors_and_focus(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#315: only what shows the update repaints; a table keeps its cursor and the setup
    screen its focused key field (and what was typed in it)."""
    from textual.widgets import DataTable

    from promptmend.tui.setup_wizard import SetupScreen

    blocked = BlockedPypi()
    monkeypatch.setattr(update_check, "check_configured", blocked)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("3")
        await settle(pilot)
        table = pane(app, "profiles").query_one("#profiles", DataTable)
        assert table.row_count > 1
        table.move_cursor(row=1)
        app.open_setup()
        await settle(pilot)
        wizard = app.screen
        assert isinstance(wizard, SetupScreen)
        field = await focus_a_key_field(wizard, pilot)
        blocked.release.set()
        await landed(app, pilot)
        await pilot.pause()
        assert wizard.at == "keys"
        assert app.screen is wizard
        assert app.focused is field
        assert field.value == "typed"
        assert table.cursor_row == 1

    drive(scenario)


def test_a_failed_reload_still_lets_the_shown_state_fill_in(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer reload that reads nothing (both stages fail, #317) shows nothing, so the State
    on screen still gets its update check: the Version row never stays `checking`."""
    blocked = BlockedPypi()
    monkeypatch.setattr(update_check, "check_configured", blocked)
    loads: list[int] = []

    def loader(group_by: str, first: State | None = None) -> State:
        loads.append(1)
        if len(loads) > 1:
            raise RuntimeError("broken")
        return gather(group_by, first)

    def first(group_by: str) -> State:
        if loads:
            raise RuntimeError("broken too")
        return quick(group_by)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert _state(app).update.state == update_check.CHECKING
        app.reload()
        await settle(pilot)
        assert app.generation == 2
        blocked.release.set()
        assert (await landed(app, pilot)).update == blocked.answer

    drive(scenario, loader=loader, quick_loader=first)
    assert len(loads) == 2


def test_a_late_update_of_an_older_reload_is_dropped(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptmend.tui.app import LateUpdate

    # This reload's own update check is held, so only the stale one posted here can arrive:
    # its own landing legitimately replaces the State object (seen on ubuntu 3.14, #317).
    blocked = BlockedPypi()
    monkeypatch.setattr(update_check, "check_configured", blocked)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        state = _state(app)
        newer = dataclasses.replace(
            state, update=update_check.UpdateStatus(update_check.LATEST, "1.0.0", "x")
        )
        app.post_message(LateUpdate(newer, app.generation - 1))
        await pilot.pause()
        assert app.state is state
        # This reload's own check lands before the scenario ends: run_test() tears the screens
        # down while the app still handles messages, so one landing then repaints a Home
        # pane already emptied (NoMatches, ubuntu 3.14, #350). A real run stops handling
        # messages before it closes the screens.
        blocked.release.set()
        assert (await landed(app, pilot)).update == blocked.answer

    drive(scenario)


# --- Two stages (#317) ----------------------------------------------------------------------

# The default pool, and one of a single thread (see one_pool_thread()). Not on Windows: there
# every Textual timer sleeps in that pool, so one thread is no pool a real run ever has.
ONE_THREAD = pytest.param(
    "one thread",
    marks=pytest.mark.skipif(
        sys.platform == "win32", reason="Textual's timers sleep in the pool on Windows"
    ),
)


def threads_now(app: App[Any] | None = None) -> str:
    """Every thread's stack (and the app's workers): what a wait that never ended was held by,
    for a CI run no one can step through (the Windows 3.12 runs of #317)."""
    import sys
    import threading
    import traceback

    frames = sys._current_frames()
    parts: list[str] = []
    for thread in threading.enumerate():
        frame = frames.get(thread.ident or -1)
        stack = "".join(traceback.format_stack(frame)) if frame else "  (no frame)\n"
        parts.append(f"--- thread {thread.name!r} daemon={thread.daemon}\n{stack}")
    if app is not None:
        workers = [f"{w.group}/{w.name}: {w.state.name}" for w in app.workers]
        parts.append(f"--- workers {workers}")
    return "\n".join(parts)


async def until(
    pilot: Pilot[int], done: Callable[[], bool], why: Callable[[], object] = lambda: ""
) -> None:
    """Pause (at most ~10 s of pauses) until ``done()``: for a scenario whose full load is
    blocked, which settle() would wait for. ``why()`` says what was seen when it never is,
    followed by every thread's stack."""
    for _ in range(1000):
        if done():
            return
        await pilot.pause(0.01)
    raise AssertionError(f"never happened: {why()}\n{threads_now(pilot.app)}")


class BlockedLoad:
    """gather(), held until released: the full stage of a slow doctor and plan."""

    def __init__(self) -> None:
        import threading

        self.release = threading.Event()
        self.calls = 0
        self.firsts: list[State | None] = []

    def __call__(self, group_by: str, first: State | None = None) -> State:
        self.calls += 1
        self.firsts.append(first)
        assert self.release.wait(10), f"never released\n{threads_now()}"
        return gather(group_by, first)


def _home_text(app: ManageApp) -> str:
    home = app.main.query_one("#home-pane", home_pane.HomePane)
    return _rendered(home, "#home-headline") + _rendered(home, "#home-rows")


def _disabled(app: ManageApp, tab: str, *ids: str) -> list[bool]:
    return [pane(app, tab).query_one(f"#{id_}", Button).disabled for id_ in ids]


WAITING_BUTTONS = {"triggers": ("show-diff", "deploy", "detach"), "history": ("prune", "reset")}


def test_home_paints_the_quick_state_and_actions_wait_for_the_full_one(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#317: Home shows the settings rows before doctor and the plan are read, the rest say
    `checking`; every pane has the same quick State; deploy, detach, the diff, prune and reset
    wait (disabled, and refused when called anyway) until the full State replaces it."""
    from promptmend.tui.home import CHECKING_HEADLINE

    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    blocked = BlockedLoad()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: app.state is not None)
        first = _state(app)
        assert (first.stage, first.full, first.plan) == (QUICK, False, None)
        assert first.report.checks == ()
        shown = _home_text(app)
        assert CHECKING_HEADLINE in shown
        assert "Rewrites" in shown
        assert "key set" in shown  # a quick row: from the settings alone
        assert "checking the match files and Espanso" in shown
        assert "checking every part" in shown
        assert app.sub_title.startswith("checking · ")
        for tab in ("triggers", "history", "diagnostics", "settings"):
            await app.main.build(tab)
        await pilot.pause()
        for each in app.main.query(base.Pane):
            assert each.state is first
        for tab, ids in WAITING_BUTTONS.items():
            assert all(_disabled(app, tab, *ids)), tab
        assert "Checking the match files" in _rendered(pane(app, "triggers"), "#deploy-target")
        assert "Reading the usage history" in _rendered(pane(app, "history"), "#stats-note")
        assert "Checking every part" in _rendered(pane(app, "diagnostics"), "#all-checks")
        rows = table_rows(app, "triggers", "#triggers")
        assert rows["-i-"][4] == "checking"
        # An action pressed now: the button does nothing, the call says it waits, no dialog.
        triggers = pane(app, "triggers")
        assert isinstance(triggers, triggers_pane.TriggersPane)
        triggers.query_one("#deploy", Button).press()
        await pilot.pause()
        assert app.screen is app.main
        for call in (triggers._deploy, triggers.ask_detach, triggers._diff):
            call()
            await pilot.pause()
            assert app.screen is app.main
            assert triggers.last_message == base.WAITING
            assert not triggers.busy
        history = pane(app, "history")
        assert isinstance(history, history_pane.HistoryPane)
        for call in (history.prune, history.reset):
            call()
            await pilot.pause()
            assert app.screen is app.main
            assert history.last_message == base.WAITING
        assert app.session == []
        assert espanso.calls == []  # the quick stage runs no command
        # What needs only the settings does not wait: the Settings list is filled.
        assert settings_rows(app)["PROMPT_HISTORY"].value == "on"
        blocked.release.set()
        await settle(pilot)
        full = _state(app)
        assert full.stage == FULL
        assert full is not first
        # The full read got this reload's quick State and reused its reads (one per reload).
        assert blocked.firsts == [first]
        assert full.layers is first.layers
        assert full.triggers is first.triggers
        assert full.profiles is first.profiles
        for each in app.main.query(base.Pane):
            assert each.state is full
        shown = _home_text(app)
        assert "checking" not in shown.replace("· checking", "")  # the Version row may wait
        assert "Espanso running" in shown
        for tab, ids in WAITING_BUTTONS.items():
            assert not any(_disabled(app, tab, *ids)), tab
        await press(app, pilot, "#deploy")
        assert isinstance(app.screen, FormModal)

    drive(scenario, settled=False, loader=blocked)
    assert blocked.calls == 1


def test_the_setup_offer_waits_for_the_full_state(saved: Path, espanso: FakeRunner) -> None:
    """#317: the quick State has no plan, so a first run asks whether anything is set up only
    once the full State is in; it opens the wizard then, once."""
    from promptmend.tui.setup_wizard import SetupScreen

    blocked = BlockedLoad()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: app.state is not None)
        assert not _state(app).full
        await pilot.pause()
        assert app.screen is app.main
        assert app.first_run
        blocked.release.set()
        await settle(pilot)
        assert isinstance(app.screen, SetupScreen)
        assert not app.first_run

    drive(scenario, settled=False, loader=blocked, first_run=True)


def test_a_failed_full_read_leaves_the_quick_state_not_checked(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#317: the quick State stays when the full read fails; its waiting rows say `not
    checked` (never `checking` for ever), the header says so, and the actions that need the
    full State still wait, saying r checks again."""

    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    def broken(group_by: str, first: State | None = None) -> State:
        raise RuntimeError("boom")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        state = _state(app)
        assert (state.stage, state.failed) == (QUICK, "RuntimeError: boom")
        assert state.update.state == update_check.UNKNOWN
        shown = _home_text(app)
        assert "not checked" in shown
        assert "checking" not in shown
        assert "Almost ready: the checks could not run (RuntimeError: boom); r checks" in shown
        assert "r: check again" in shown
        assert "-> 4 Triggers" not in shown  # no tab fixes it
        assert app.sub_title.startswith("not checked · ")
        await open_tab(app, pilot, "triggers", "history", "diagnostics")
        assert all(_disabled(app, "triggers", "show-diff", "deploy", "detach"))
        triggers = pane(app, "triggers")
        assert isinstance(triggers, triggers_pane.TriggersPane)
        assert "Not compared with Espanso" in _rendered(triggers, "#deploy-target")
        assert table_rows(app, "triggers", "#triggers")["-i-"][4] == "not checked"
        triggers.ask_detach()
        assert triggers.last_message == base.NOT_CHECKED
        assert "Not read." in _rendered(pane(app, "history"), "#stats-note")
        assert "Not checked" in _rendered(pane(app, "diagnostics"), "#all-checks")
        app.open_setup()
        await settle(pilot)
        servers = str(app.screen.query_one("#setup-servers", Static).render())
        assert servers.startswith("Not checked whether a local server answers")

    drive(scenario, loader=broken)


def test_a_reload_that_reads_nothing_leaves_no_state_checking(espanso: FakeRunner) -> None:
    """Review of #317: a reload whose quick and full reads both fail, while an older reload's
    quick State is on screen (its full State, read later, is dropped), shows that quick
    State `not checked`, never `checking` for ever."""
    import threading

    release = threading.Event()
    calls: list[str] = []

    def quick_loader(group_by: str) -> State:
        calls.append("quick")
        if len(calls) > 1:
            raise RuntimeError("no settings")
        return quick(group_by)

    def loader(group_by: str, first: State | None = None) -> State:
        calls.append("full")
        if first is None:
            raise RuntimeError("boom")
        assert release.wait(10), f"never released\n{threads_now()}"
        return gather(group_by, first)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: calls == ["quick", "full"])
        older = _state(app)
        assert (older.stage, older.failed) == (QUICK, None)
        app.reload()
        await until(pilot, lambda: app.shown == 2, lambda: (calls, app.shown))
        state = _state(app)
        assert (state.stage, state.failed) == (QUICK, "RuntimeError: boom")
        assert state.layers is older.layers
        assert "checking" not in _home_text(app)
        release.set()
        await settle(pilot)
        assert app.state is state  # the older reload's full State is dropped

    drive(scenario, settled=False, loader=loader, quick_loader=quick_loader)


def test_a_held_reload_key_runs_at_most_a_few_full_reads(espanso: FakeRunner) -> None:
    """Review of #317: with every full read hanging (Espanso does not answer) and `r` pressed
    again and again, at most FULL_READS of them run; each reload still shows its quick State,
    and once the reads end the newest one's full State is shown."""
    import threading

    from promptmend.tui.app import FULL_READS

    release = threading.Event()
    entered: list[int] = []

    def loader(group_by: str, first: State | None = None) -> State:
        entered.append(1)
        assert release.wait(10), f"never released\n{threads_now()}"
        return gather(group_by, first)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        reloads = FULL_READS + 3
        for number in range(1, reloads + 1):
            if number > 1:
                app.reload()
            await until(pilot, lambda: app.shown == app.generation, lambda: (app.shown, entered))
            if number <= FULL_READS:
                await until(pilot, lambda: len(entered) == app.generation, lambda: entered)
        for _ in range(10):
            await pilot.pause()
        assert len(entered) == FULL_READS
        assert not _state(app).full
        release.set()
        await until(pilot, lambda: _state(app).full, lambda: (app.shown, entered))
        assert app.shown == reloads
        # The reloads cancelled while they waited for a slot never read.
        assert len(entered) == FULL_READS + 1

    drive(scenario, settled=False, loader=loader)


@pytest.mark.parametrize(
    ("platform", "version", "shortened"),
    [
        ("win32", (3, 12, 10), True),
        ("win32", (3, 13, 0), False),
        ("win32", (3, 14, 0), False),
        ("darwin", (3, 12, 10), False),
        ("linux", (3, 12, 10), False),
    ],
)
def test_threads_get_their_turn_only_on_windows_before_3_13(
    monkeypatch: pytest.MonkeyPatch, platform: str, version: tuple[int, ...], shortened: bool
) -> None:
    """#348: a shorter GIL switch interval only on Windows with Python 3.12, restored after,
    even when the run ends with an error."""
    import sys

    from promptmend.tui import app as app_module

    before = sys.getswitchinterval()
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "version_info", version)
    with app_module.threads_get_their_turn():
        inside = sys.getswitchinterval()
    assert inside == (app_module.WINDOWS_312_SWITCH_INTERVAL if shortened else before)
    assert sys.getswitchinterval() == before
    with pytest.raises(RuntimeError), app_module.threads_get_their_turn():
        raise RuntimeError("the app failed")
    assert sys.getswitchinterval() == before


def test_the_interface_runs_with_threads_getting_their_turn(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#348: the app's whole run (run_test here, run() for a person: both go through
    _process_messages) is inside threads_get_their_turn()."""
    import contextlib

    from promptmend.tui import app as app_module

    events: list[str] = []

    @contextlib.contextmanager
    def recorded() -> Iterator[None]:
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    monkeypatch.setattr(app_module, "threads_get_their_turn", recorded)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert events == ["enter"]

    drive(scenario)
    assert events == ["enter", "exit"]


def test_the_full_stage_keeps_the_persons_place(espanso: FakeRunner) -> None:
    """Review of #317: the full State repaints every pane, but the Profiles cursor stays on
    its profile and the setup wizard keeps its step, its focused key field and what was typed
    in it."""
    from textual.widgets import DataTable

    from promptmend.tui.setup_wizard import SetupScreen

    blocked = BlockedLoad()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: app.state is not None)
        await app.main.build("profiles")
        await pilot.pause()
        table = pane(app, "profiles").query_one("#profiles", DataTable)
        table.move_cursor(row=1)  # general: off the first row (default-pro was row 2, #414)
        await pilot.pause()
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        app.open_setup()
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and app.screen.is_mounted)
        wizard = app.screen
        assert isinstance(wizard, SetupScreen)
        field = await focus_a_key_field(wizard, pilot)
        blocked.release.set()
        await settle(pilot)
        assert _state(app).full
        assert table.cursor_row == 1
        assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key == key
        assert wizard.at == "keys"
        assert app.screen is wizard
        assert app.focused is field
        assert field.value == "typed"

    drive(scenario, settled=False, loader=blocked)


def test_opening_setup_by_hand_uses_up_the_first_run_offer(
    saved: Path, espanso: FakeRunner
) -> None:
    """Review of #317: setup opened during the quick stage is the offer; the full State never
    opens a second wizard, nor again after that one is closed."""
    from promptmend.tui.setup_wizard import SetupScreen

    blocked = BlockedLoad()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: app.state is not None)
        app.open_setup()
        await pilot.pause()
        assert not app.first_run
        blocked.release.set()
        await settle(pilot)
        wizards = [s for s in app.screen_stack if isinstance(s, SetupScreen)]
        assert len(wizards) == 1
        wizards[0].dismiss()
        app.reload()
        await settle(pilot)
        assert app.screen is app.main

    drive(scenario, settled=False, loader=blocked, first_run=True)


def test_a_wizard_already_open_uses_up_the_offer(saved: Path, espanso: FakeRunner) -> None:
    """Review of #317: a wizard on screen when the full State lands, however it was opened,
    is the offer: no second one, and none later."""
    from promptmend.tui.setup_wizard import SetupScreen

    blocked = BlockedLoad()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: app.state is not None)
        app.push_screen(SetupScreen())  # not through open_setup()
        await pilot.pause()
        assert app.first_run
        blocked.release.set()
        await settle(pilot)
        assert not app.first_run
        assert len([s for s in app.screen_stack if isinstance(s, SetupScreen)]) == 1

    drive(scenario, settled=False, loader=blocked, first_run=True)


def test_the_first_run_offer_waits_while_about_is_open(saved: Path, espanso: FakeRunner) -> None:
    """Review of #317: About over the tabs when the full State lands keeps the offer (the
    wizard never opens over it); closing About makes it, with no reload. About also says the
    install channel is being checked on the quick State, and fills it in."""
    from promptmend.tui.intro import AboutScreen
    from promptmend.tui.setup_wizard import SetupScreen

    blocked = BlockedLoad()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: app.state is not None)
        await pilot.press("a")
        await until(pilot, lambda: isinstance(app.screen, AboutScreen))
        about = app.screen
        assert "Installed: checking" in _rendered(about, "#about-facts")
        blocked.release.set()
        await settle(pilot)
        assert _state(app).full
        assert app.screen is about
        # Repainted by the full State: the channel doctor found.
        assert "Installed: uv" in _rendered(about, "#about-facts")
        assert app.first_run
        generation = app.generation
        await pilot.press("escape")
        await settle(pilot)
        assert isinstance(app.screen, SetupScreen)
        assert not app.first_run
        assert app.generation == generation  # no reload made it

    drive(scenario, settled=False, loader=blocked, first_run=True)


@pytest.mark.parametrize("pool", ["default", ONE_THREAD])
def test_the_done_step_keeps_its_focus_across_stages(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch, pool: str
) -> None:
    """Review of #317: on the last step, the full State (and the plan read with it) leaves the
    focus where the person put it, instead of moving it back to Quit. With one pool thread the
    wizard's own plan read still runs while the full read waits (it used to queue behind it)."""
    from promptmend.tui.setup_wizard import SetupOptions, SetupScreen

    if pool == "one thread":
        one_pool_thread(monkeypatch)

    blocked = BlockedLoad()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        # The wizard mounted, shown the quick State and read its plan: nothing of its start
        # is still on its way (a focus moves after a refresh, so each step below waits for it).
        def why() -> object:
            screen = app.screen
            return {
                "state": app.state.stage if app.state else None,
                "shown": app.shown,
                "screen": type(screen).__name__,
                "mounted": screen.is_mounted,
                "at": getattr(screen, "at", None),
                "plan": getattr(screen, "plan", None) is not None,
                "plan_error": getattr(screen, "plan_error", None),
                "focused": app.focused,
            }

        await until(pilot, lambda: app.state is not None, why)
        await until(
            pilot, lambda: isinstance(app.screen, SetupScreen) and app.screen.is_mounted, why
        )
        wizard = app.screen
        assert isinstance(wizard, SetupScreen)
        await until(pilot, lambda: wizard.plan is not None or wizard.plan_error is not None, why)
        quit_ = wizard.query_one("#setup-quit", Button)
        wizard.go("done")
        await until(pilot, lambda: app.focused is quit_, why)
        # The hidden Back and Next never take the focus here, not even one already on its
        # way (Textual lets a button in a hidden row be focused; the Windows CI did, #317).
        for hidden in ("#setup-next-step", "#setup-back"):
            wizard.query_one(hidden, Button).focus()
            await pilot.pause()
            assert app.focused is quit_, hidden
        opened = wizard.query_one("#setup-open", Button)
        opened.focus()
        await until(pilot, lambda: app.focused is opened, why)
        blocked.release.set()
        await settle(pilot)
        assert _state(app).full
        assert wizard.at == "done"
        assert app.focused is opened

    drive(scenario, settled=False, loader=blocked, setup=SetupOptions(standalone=True))


def test_a_quick_state_keeps_the_update_answer_on_screen(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #317: a reload's quick State shows the update answer the screen already has
    (no flicker back to `checking`), says off at once when the settings turn the check off,
    and the first load, with nothing known yet, says checking."""
    from promptmend.tui.home import home_rows

    latest = update_check.UpdateStatus(update_check.LATEST, "0.1.0", "x")
    full = dataclasses.replace(gather().settled(), update=latest)
    first = quick()
    assert first.update.state == update_check.CHECKING
    assert first.known_update(None) is first
    carried = first.known_update(full)
    assert carried.update == latest
    assert carried.stage == QUICK
    assert "latest" in home_rows(carried)[0].text
    # Nothing to carry from a quick State, a check still running or one turned off.
    assert first.known_update(first) is first
    still = dataclasses.replace(full, update=update_check.UpdateStatus(update_check.CHECKING))
    assert first.known_update(still) is first
    was_off = dataclasses.replace(full, update=update_check.UpdateStatus(update_check.OFF))
    assert first.known_update(was_off) is first
    monkeypatch.setenv("PROMPT_UPDATE_CHECK", "false")
    off = quick()
    assert off.update.state == update_check.OFF
    assert off.known_update(full) is off
    assert home_rows(off)[0].text.endswith(" · check off")


def test_the_checking_headline_names_only_what_is_checked(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #317: with the usage history off, the History row says off and the headline
    does not say it is being checked."""
    from promptmend.tui.home import (
        CHECKING_HEADLINE,
        CHECKING_HEADLINE_NO_HISTORY,
        headline,
        home_rows,
    )

    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    assert headline(home_rows(quick())) == (CHECKING_HEADLINE, None)
    monkeypatch.setenv("PROMPT_HISTORY", "false")
    rows = home_rows(quick())
    assert next(r for r in rows if r.label == "History").text == "off"
    assert headline(rows) == (CHECKING_HEADLINE_NO_HISTORY, None)


def one_pool_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncio's default thread pool down to one thread, as busy as the Windows CI's was: Textual
    runs thread workers there, and on Windows each of its timers sleeps in it (#317)."""
    from concurrent.futures import ThreadPoolExecutor

    real = ManageApp.on_mount

    def on_mount(self: ManageApp) -> None:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(1))
        real(self)

    monkeypatch.setattr(ManageApp, "on_mount", on_mount)


@pytest.mark.parametrize("pool", ["default", ONE_THREAD])
def test_the_stages_of_an_older_reload_never_replace_a_newer_one(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch, pool: str
) -> None:
    """#317 with #315's generation guard: within a reload the quick State comes before the
    full one; a reload started while the first is still reading shows its own quick and full
    States at once (the first one's slow read holds no thread a newer reload needs), and the
    first one's full State, read after, is never shown. The update check is held, so its late
    update (which legitimately replaces the State object) lands only at the end, and it is
    the newer reload's."""
    import threading

    if pool == "one thread":
        # A reload's reads never wait for that pool, nor hold it (the Windows CI's failure:
        # reload 2 never showed even its quick State while reload 1's full read waited).
        one_pool_thread(monkeypatch)
    blocked = BlockedPypi()
    monkeypatch.setattr(update_check, "check_configured", blocked)
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    loads: list[int] = []

    def loader(group_by: str, first: State | None = None) -> State:
        loads.append(1)
        if len(loads) > 1:
            return gather(group_by, first)
        # The first reload's full read is the slow one.
        started.set()
        assert release.wait(10), f"never released\n{threads_now()}"
        try:
            return gather(group_by, first)
        finally:
            finished.set()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        seen: list[tuple[int, str]] = []
        newer_full = threading.Event()
        real = app._show

        def show(state: State, generation: int) -> None:
            seen.append((generation, state.stage))
            real(state, generation)
            if generation == 2 and state.full:
                newer_full.set()

        def why() -> object:
            return {"seen": seen, "generation": app.generation, "shown": app.shown}

        app._show = show  # type: ignore[method-assign]
        await until(pilot, started.is_set, why)  # reload 1 is in its full read
        assert app.state is not None  # its quick State was shown before (call_from_thread)
        app.reload()
        await until(pilot, newer_full.is_set, why)
        newer = _state(app)
        assert (app.shown, newer.full, newer.update.state) == (2, True, update_check.CHECKING)
        release.set()
        await until(pilot, finished.is_set, why)  # the first reload's full read ended
        await settle(pilot)
        assert app.state is newer  # the first reload's full State was never shown
        # The first reload's quick State may have been shown before its show() was
        # wrapped; what was seen is in order, each reload quick first.
        assert (1, FULL) not in seen
        assert seen[-2:] == [(2, QUICK), (2, FULL)]
        # The held update check lands now: the newer reload's State with it filled in.
        blocked.release.set()
        landed_state = await landed(app, pilot)
        assert app.shown == 2
        assert landed_state.update == blocked.answer
        assert landed_state.layers is newer.layers

    drive(scenario, settled=False, loader=loader)
    assert len(loads) == 2


def test_a_late_answer_that_cannot_be_shown_is_unknown(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#315: the Version row never stays `checking`, even when the answer breaks the
    version check."""
    from concurrent.futures import Future

    from promptmend import doctor

    blocked = BlockedPypi()
    monkeypatch.setattr(update_check, "check_configured", blocked)
    blocked.release.set()
    state = gather()
    real = doctor.with_update

    def broken(report: doctor.Report, update: update_check.UpdateStatus) -> doctor.Report:
        if update.state == update_check.AVAILABLE:
            raise RuntimeError("cannot show it")
        return real(report, update)

    monkeypatch.setattr(doctor, "with_update", broken)
    done: Future[update_check.UpdateStatus] = Future()
    done.set_result(blocked.answer)
    assert state.landed(done).update.state == update_check.UNKNOWN
    monkeypatch.setattr(doctor, "with_update", lambda *_: 1 / 0)
    later = state.landed(done)
    assert (later.update.state, later.pending) == (update_check.UNKNOWN, None)


def pane(app: ManageApp, tab: str) -> base.Pane:
    return app.main.query_one(f"#{tab}-pane", base.Pane)


async def open_tab(app: ManageApp, pilot: Pilot[int], *tabs: str) -> None:
    """Build each tab's pane, as opening it does: only Home is built at start (#316)."""
    for tab in tabs:
        await app.main.build(tab)
    await settle(pilot)


def table_rows(app: ManageApp, tab: str, selector: str) -> dict[str, list[str]]:
    """A DataTable's rows by their first cell, from its data: a rendered line can lag (#118)."""
    from textual.widgets import DataTable

    table = pane(app, tab).query_one(selector, DataTable)
    rows = (table.get_row_at(i) for i in range(table.row_count))
    found = {row[0].plain: [cell.plain for cell in row] for row in rows}
    assert len(found) == table.row_count, "two rows share a first cell"
    return found


async def press(app: ManageApp, pilot: Pilot[int], selector: str) -> None:
    app.screen.query_one(selector, Button).press()
    await settle(pilot)


async def fill(app: ManageApp, pilot: Pilot[int], **values: str) -> None:
    """Set a dialog's fields by id and submit it."""
    for field_id, value in values.items():
        widget = app.screen.query_one(f"#{field_id.replace('_', '-')}")
        assert isinstance(widget, Input | Select)
        widget.value = value
    await pilot.pause()
    await press(app, pilot, "#submit")


def settings_tab(app: ManageApp) -> settings_pane.SettingsPane:
    return app.main.query_one("#settings-pane", settings_pane.SettingsPane)


def settings_rows(app: ManageApp) -> dict[str, settings_model.Row]:
    return settings_tab(app).rows


async def on_row(app: ManageApp, pilot: Pilot[int], name: str, *keys: str) -> None:
    """Open Settings, put the list's cursor on ``name`` and press ``keys`` there. A setting
    behind the Advanced fold (#389) is reached as a user would: Enter on the fold line first."""
    await pilot.press("2")
    await settle(pilot)
    settings = settings_tab(app)
    listing = settings.query_one(settings_pane.SettingsList)
    assert listing.has_focus
    if name not in settings.rows and settings_model.BY_NAME[name].advanced:
        await open_fold(app, pilot)
    listing.highlighted = listing.get_option_index(name)
    await pilot.pause()
    assert settings_tab(app).cursor == name
    if keys:
        await pilot.press(*keys)
        await settle(pilot)


async def to_fold(app: ManageApp, pilot: Pilot[int]) -> None:
    """Put the Settings list's cursor on the Advanced fold line, its last line."""
    listing = settings_tab(app).query_one(settings_pane.SettingsList)
    listing.highlighted = listing.get_option_index(settings_pane.FOLD)
    await pilot.pause()
    assert settings_tab(app).cursor == settings_pane.FOLD


async def open_fold(app: ManageApp, pilot: Pilot[int]) -> None:
    """Enter on the Advanced fold line: the advanced settings show."""
    await to_fold(app, pilot)
    await pilot.press("enter")
    await settle(pilot)
    assert settings_tab(app).advanced


async def edit_to(app: ManageApp, pilot: Pilot[int], name: str, value: str) -> None:
    """Enter on ``name``'s row, then type ``value`` into its field and save."""
    await on_row(app, pilot, name, "enter")
    assert isinstance(app.screen, EditModal)
    app.screen.query_one("#edit-value", Input).value = value
    await pilot.pause()
    await press(app, pilot, "#submit")


async def pick(app: ManageApp, pilot: Pilot[int], name: str, value: str) -> None:
    """Enter on ``name``'s row, then pick ``value`` from its list."""
    await on_row(app, pilot, name, "enter")
    screen = app.screen
    assert isinstance(screen, PickModal)
    listing = screen.query_one("#pick", OptionList)
    listing.highlighted = [v for _, v in screen.choices].index(value)
    await pilot.pause()
    await pilot.press("enter")
    await settle(pilot)


@pytest.fixture
def record_ops(seed_history: SeedHistory) -> Callable[..., None]:
    """record_ops(n) seeds n recorded -i- calls to OpenRouter."""

    def record(n: int = 2) -> None:
        for _ in range(n):
            op = {
                "id": history.new_operation_id(),
                "origin": "espanso_managed",
                "trigger_id": "-i-",
                "kind": "improve",
                "profile_id": "default",
                "outcome": "ok",
                "latency_ms": 1000.0,
            }
            attempt = {
                "provider": "openrouter",
                "requested_model": "google/gemini-3.5-flash-lite",
                "endpoint": "remote",
                "status": 200,
                "latency_ms": 900.0,
                "output": 30,
                "input_uncached": 100,
                "charged_amount": Decimal("0.0001"),
                "charged_unit": "credits",
            }
            seed_history(op, [attempt])

    return record


# --- app, keys, theme ---------------------------------------------------------------------


def test_digits_switch_tabs_and_t_toggles_contrast(espanso: FakeRunner) -> None:
    seen = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        from textual.widgets import TabbedContent

        for key, tab in zip(
            "7654321",
            ("try", "diagnostics", "history", "triggers", "profiles", "settings", "home"),
            strict=True,
        ):
            await pilot.press(key)
            seen.append(app.main.query_one(TabbedContent).active == tab)
        await pilot.press("t")
        seen.append(app.theme == HIGH_CONTRAST.name)
        await pilot.press("t")
        seen.append(app.theme == "textual-dark")
        await pilot.press("q")

    drive(scenario)
    assert seen == [True] * 9


# The digit of each tab, as its label shows it.
DIGITS = {tab: str(n) for n, tab in enumerate(TABS, start=1)}
# What has the focus as a tab opens: its list on Home and Settings, the tab bar elsewhere.
ARRIVAL = {"home": "#home-rows", "settings": "#settings-list"}


def tab_changes(app: ManageApp) -> list[str]:
    """Every value TabbedContent.active takes from now on."""
    from textual.widgets import TabbedContent

    changes: list[str] = []

    def changed(tab: str) -> None:
        changes.append(tab)

    app.main.watch(app.main.query_one(TabbedContent), "active", changed, init=False)
    return changes


def _takes_text(widget: Widget) -> bool:
    """An Input or a TextArea that can be typed into: a digit typed there is text."""
    from textual.widgets import TextArea

    return isinstance(widget, Input) or (isinstance(widget, TextArea) and not widget.read_only)


def _arrived(app: ManageApp, tab: str) -> bool:
    """``tab`` is shown and has its arrival focus; nothing in a hidden pane has it."""
    from textual.widgets import TabbedContent, Tabs

    if app.main.query_one(TabbedContent).active != tab:
        return False
    if tab in ARRIVAL:
        return app.focused is pane(app, tab).query_one(ARRIVAL[tab])
    return isinstance(app.focused, Tabs)


async def _walk_to(app: ManageApp, pilot: Pilot[int], widget: Widget) -> None:
    """Move the focus to ``widget`` with Tab or Shift+Tab, whichever is shorter, as a person
    does."""
    chain = app.main.focus_chain
    start = chain.index(app.focused) if app.focused in chain else 0
    ahead = (chain.index(widget) - start) % len(chain)
    key = "tab" if ahead <= len(chain) - ahead else "shift+tab"
    for _ in range(len(chain)):
        if app.focused is widget:
            return
        await pilot.press(key)
    assert app.focused is widget, f"Tab never reaches {widget!r}"


# A tab's steps in the test below: one per widget to leave, at least one per digit; more
# means a pane gives new widgets on each visit, and the test would never end.
MAX_STEPS = 20


# #428: a digit leaves a tab whatever of the tab has the focus, changing the tab exactly once
# (a hidden button, select or table took the focus back and its tab bounced back). One app run
# per tab: every focusable widget of the tab (found at run time, so a widget added later is
# covered too) is left by a digit, every other tab's digit is pressed once, and the tab's own
# digit brings the tour back. A widget that takes text keeps the digit as text. No animation:
# the tab bar's underline was a third of the time.
@pytest.mark.parametrize("tab", list(TABS))
def test_a_digit_changes_the_tab_once_wherever_the_focus_is(
    espanso: FakeRunner, record_ops: Callable[..., None], tab: str
) -> None:
    from textual.widgets import TabPane

    record_ops()  # History's table, picker and buttons shown
    others = [t for t in TABS if t != tab]
    digits = list(others)  # the other tabs whose digit is still to be pressed here
    left: set[int] = set()  # the widgets already left, by id()
    seen: list[tuple[str, str, list[str]]] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        app.animation_level = "none"
        await open_tab(app, pilot, *TABS)
        changes = tab_changes(app)
        await pilot.click(f"#--content-tab-{tab}")
        await settle(pilot)
        here = app.main.query_one(f"TabPane#{tab}", TabPane)

        def to_leave() -> list[Widget]:
            return [w for w in app.main.focus_chain if here in w.ancestors and id(w) not in left]

        for widget in [w for w in to_leave() if _takes_text(w)]:
            await _walk_to(app, pilot, widget)
            changes.clear()
            await pilot.press(DIGITS[others[0]])
            await settle(pilot)
            assert changes == [], widget
            assert app.focused is widget
            text = widget.value if isinstance(widget, Input) else getattr(widget, "text", "")
            assert text.endswith(DIGITS[others[0]]), widget
            await pilot.press("backspace")  # the Settings filter shows every row again
            left.add(id(widget))
        for _ in range(MAX_STEPS):
            todo = to_leave()
            if not todo and not digits:
                return
            # The next widget to leave, else the arrival focus; the next digit to press.
            leaving = todo[0] if todo else app.focused
            assert leaving is not None
            other = digits.pop(0) if digits else others[0]
            name = f"{type(leaving).__name__}#{leaving.id}"
            await _walk_to(app, pilot, leaving)
            changes.clear()
            await pilot.press(DIGITS[other])
            await settle(pilot)
            seen.append((name, other, list(changes)))
            assert changes == [other], (name, other)
            assert _arrived(app, other), (name, other, app.focused)
            left.add(id(leaving))
            # Back by this tab's digit, from the arrival focus.
            await pilot.press(DIGITS[tab])
            await settle(pilot)
            assert changes == [other, tab], (name, other)
            assert _arrived(app, tab), (name, other, app.focused)
        raise AssertionError(f"{tab}: still to leave after {MAX_STEPS} steps: {to_leave()}")

    drive(scenario)
    assert {other for _, other, _ in seen} == set(others)


# The other ways of changing the tab with a tab's button focused (#428): a click on a tab,
# a digit while a reload is still running, the tab bar's arrows: each changes it once.
def test_a_click_a_reload_or_the_arrows_change_the_tab_once(espanso: FakeRunner) -> None:
    from textual.widgets import TabPane, Tabs

    blocked = BlockedLoad()
    blocked.release.set()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        app.animation_level = "none"
        await open_tab(app, pilot, *TABS)
        changes = tab_changes(app)

        async def on_a_button(tab: str) -> None:
            await pilot.click(f"#--content-tab-{tab}")
            await settle(pilot)
            here = app.main.query_one(f"TabPane#{tab}", TabPane)
            button = next(
                w for w in app.main.focus_chain if isinstance(w, Button) and here in w.ancestors
            )
            await _walk_to(app, pilot, button)
            changes.clear()

        await on_a_button("triggers")
        await pilot.click("#--content-tab-history")
        await settle(pilot)
        assert changes == ["history"]
        assert _arrived(app, "history")
        await on_a_button("profiles")
        blocked.release.clear()
        await pilot.press("r", DIGITS["triggers"])
        await until(pilot, lambda: blocked.calls == 2, lambda: blocked.calls)
        assert changes == ["triggers"]  # while the reload's full read still waits
        assert _arrived(app, "triggers")
        blocked.release.set()
        await settle(pilot)
        assert changes == ["triggers"]
        assert _arrived(app, "triggers")
        await on_a_button("history")
        await _walk_to(app, pilot, app.main.query_one(Tabs))  # back to the tab bar
        assert changes == []
        await pilot.press("right")
        await settle(pilot)
        assert changes == ["diagnostics"]
        assert _arrived(app, "diagnostics")

    drive(scenario, loader=blocked)


def _quit_shown(app: ManageApp, width: int) -> bool:
    """The footer's Quit key is drawn whole inside a ``width``-column screen."""
    from textual.widgets._footer import FooterKey

    keys = [k for k in app.screen.query(FooterKey) if k.description == "Quit"]
    return len(keys) == 1 and keys[0].region.width > 0 and keys[0].region.right <= width


# At 70 columns (#394), every tab label lies whole inside the screen and the footer still
# shows Quit on every tab, each opened from Home's focused rows (#395): Home with its list
# focused and the Settings list focused (Edit, Toggle, Use default, Filter added) included. About
# and High contrast are off the footer and still work. Pilot, not a snapshot, so Windows
# checks it too.
def test_every_tab_and_quit_fit_70_columns(espanso: FakeRunner) -> None:
    from textual.widgets._footer import FooterKey
    from textual.widgets._tabbed_content import ContentTab

    width = 70
    seen: dict[str, Any] = {}

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        tabs = list(app.main.query(ContentTab))
        seen["labels"] = [str(t.label) for t in tabs]
        seen["inside"] = [t.region.x >= 0 and t.region.right <= width for t in tabs]
        seen["narrow"] = app.main.has_class("-narrow")
        quit_by_tab = {}
        home_list = []
        for n, tab in enumerate(TABS, start=1):
            # Each tab opened from Home's rows, which have the focus there (#395).
            await pilot.press("1")
            await settle(pilot)
            focused = app.focused
            home_list.append(focused is not None and focused.id == "home-rows")
            quit_by_tab[f"home list, before {tab}"] = _quit_shown(app, width)
            await pilot.press(str(n))
            await settle(pilot)
            if tab == "settings":
                focused = app.focused
                seen["settings focused"] = focused is not None and focused.id == "settings-list"
                # `u` is Use default there: "Reset" names only History's delete-all (#396).
                seen["settings footer"] = {
                    k.description: k.region.right <= width
                    for k in app.screen.query(FooterKey)
                    if k.description in ("Use default", "Reset")
                }
                # t works with the list focused too, as docs/interface.md "Keys" says.
                await pilot.press("t")
                seen["t on the list"] = app.theme == HIGH_CONTRAST.name
                await pilot.press("t")
            quit_by_tab[tab] = _quit_shown(app, width)
        seen["quit"] = quit_by_tab
        seen["home list"] = home_list
        # In the draft a letter is typed, not a key: the footer shows only Leave (Escape).
        app.main.query_one("#try-draft").focus()
        await settle(pilot)
        seen["draft footer"] = [
            (k.description, k.region.right <= width) for k in app.screen.query(FooterKey)
        ]
        app.main.set_focus(None)
        await pilot.press("t")
        seen["t"] = app.theme == HIGH_CONTRAST.name
        await pilot.press("a")
        await pilot.pause()
        seen["a"] = type(app.screen).__name__ == "AboutScreen"

    drive(scenario, size=(width, 24))
    assert seen.pop("labels") == [label for label, _ in TABS.values()]
    assert seen.pop("inside") == [True] * len(TABS)
    assert seen.pop("quit") == {
        key: True for tab in TABS for key in (f"home list, before {tab}", tab)
    }
    assert seen.pop("home list") == [True] * len(TABS)
    assert seen.pop("draft footer") == [("Leave", True)]
    assert seen == {
        "narrow": True,
        "settings focused": True,
        "settings footer": {"Use default": True},
        "t on the list": True,
        "t": True,
        "a": True,
    }


# The tab bar keeps Textual's two spaces between tabs from 80 columns on (#394).
def test_the_tab_spacing_narrows_only_below_80_columns(espanso: FakeRunner) -> None:
    from textual.widgets._tabbed_content import ContentTab

    widths: dict[int, list[int]] = {}
    for width in (80, 79, 70):

        async def scenario(app: ManageApp, pilot: Pilot[int], width: int = width) -> None:
            widths[width] = [t.region.width for t in app.main.query(ContentTab)]

        drive(scenario, size=(width, 24))
    labels = [len(label) for label, _ in TABS.values()]
    assert widths[80] == [n + 2 for n in labels]
    assert widths[79] == widths[70] == [n + 1 for n in labels]


# The Try tab's four pickers (#394): compact, one row, one line each, and every built-in value
# drawn whole at 80 and 70 columns (no ellipsis): each target, provider and tier, "as
# configured" and the built-in profiles. Measured against the cells the value gets on screen,
# not the label's text, which keeps the whole value however it is cut. A failure names the
# width in app.CSS to raise.
_TRY_PICKERS = {
    "try-target": (
        {value: text for text, value in try_pane.TARGETS},
        "the 1fr of #try-target-option",
    ),
    "try-provider": ({n: n for n in PROVIDER_NAMES}, "#try-provider-option"),
    "try-profile": (
        {try_pane.CONFIGURED: "as configured", **{n: n for n in PROFILES}},
        "the 1fr of #try-profile-option",
    ),
    "try-tier": ({t: t for t in config.TIERS}, "#try-tier-option"),
}


@pytest.mark.parametrize("width", [80, 70])
def test_the_try_pickers_never_wrap_or_cut(width: int, espanso: FakeRunner) -> None:
    cut: list[str] = []
    texts: list[str] = []
    rows: set[int] = set()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("7")
        await settle(pilot)
        for id_, (values, css) in _TRY_PICKERS.items():
            select = app.main.query_one(f"#{id_}", Select)
            rows.add(select.region.y)
            assert select.region.right <= width
            label = select.query_one("SelectCurrent #label", Static)
            for value, text in values.items():
                select.value = value
                await pilot.pause()
                texts.append(str(label.render()))
                if select.region.height != 1 or label.region.width < cell_len(text):
                    cut.append(
                        f"{text!r} needs {cell_len(text)} cells, {label.region.width} drawn "
                        f"at {width} columns: raise {css} in app.CSS"
                    )

    drive(scenario, size=(width, 24))
    assert len(rows) == 1
    assert texts == [text for values, _ in _TRY_PICKERS.values() for text in values.values()]
    assert cut == []


def test_only_home_is_built_until_a_tab_opens(espanso: FakeRunner) -> None:
    """#316: the other tabs are built the first time they open, by a digit, by the arrows on
    the tab bar or by a click, and each shows the current State at once."""
    built: list[list[str]] = []

    def now(app: ManageApp) -> list[str]:
        return [tab for tab in TABS if app.main.built(tab) is not None]

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        from textual.widgets import TabbedContent, Tabs

        built.append(now(app))
        await pilot.press("4")
        await settle(pilot)
        built.append(now(app))
        triggers = pane(app, "triggers")
        assert triggers.ready
        assert table_rows(app, "triggers", "#triggers")
        # The tab bar's arrows open the next tab, and build it.
        app.main.query_one(Tabs).focus()
        await pilot.press("right")
        await settle(pilot)
        assert app.main.query_one(TabbedContent).active == "history"
        built.append(now(app))
        await pilot.click("#--content-tab-diagnostics")
        await settle(pilot)
        assert app.main.query_one(TabbedContent).active == "diagnostics"
        assert "version:" in str(pane(app, "diagnostics").query_one("#all-checks").render())
        built.append(now(app))
        # Opening a tab again builds nothing new.
        await pilot.press("4", "1", "4")
        await settle(pilot)
        assert pane(app, "triggers") is triggers
        assert len(app.main.query("#triggers-pane")) == 1

    drive(scenario)
    assert built == [
        ["home"],
        ["home", "triggers"],
        ["home", "triggers", "history"],
        ["home", "triggers", "history", "diagnostics"],
    ]


def test_header_shows_the_name_and_installed_version(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#112: the header names the tool and its version on every screen."""
    from textual.widgets._header import HeaderTitle

    from promptmend.tui import app as app_module

    monkeypatch.setattr(app_module, "installed_version", lambda: "9.8.7")
    seen = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        seen.append(str(app.main.query_one(HeaderTitle).render()))
        await pilot.press("6")
        seen.append(str(app.main.query_one(HeaderTitle).render()))
        assert app.state is not None
        seen.append(pill(app.state.report))

    drive(scenario)
    # Then the status pill (#112), first so a narrow terminal cuts the tagline, not the status
    # (#174): the espanso fixture leaves the key unset, a problem.
    assert "problem" in seen[-1]
    assert seen[:2] == [f"PromptMend 9.8.7 — {seen[-1]} · set up and manage"] * 2


def test_a_failing_load_is_shown_not_raised(espanso: FakeRunner) -> None:
    def broken(group_by: str, first: State | None = None) -> State:
        raise RuntimeError("boom")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert app.state is None
        # Before any state, an action needing it does nothing or says what is missing.
        await pilot.press("3")
        await press(app, pilot, "#set-profile")
        assert not isinstance(app.screen, FormModal)
        await press(app, pilot, "#edit-profile")
        assert "Select one of your profiles" in pane(app, "profiles").last_message
        await pilot.press("4")
        await settle(pilot)
        triggers = pane(app, "triggers")
        assert isinstance(triggers, triggers_pane.TriggersPane)
        # Before any State the buttons that need the full one are disabled (#317); called
        # anyway, the diff says what is missing.
        assert all(_disabled(app, "triggers", "show-diff", "deploy", "detach"))
        triggers._diff()
        assert "No plan" in triggers.last_message

    drive(scenario, loader=broken, quick_loader=broken)


@pytest.mark.parametrize(
    ("tab", "button"),
    [
        ("6", "#smoke"),
        ("3", "#set-profile"),
        ("5", "#export"),
        ("5", "#prune"),
    ],
)
def test_cancelling_a_dialog_changes_nothing(
    saved: Path, espanso: FakeRunner, tab: str, button: str
) -> None:
    config_store.save_secret("OPENROUTER_API_KEY", KEY)
    before = sorted((p, p.read_bytes()) for p in saved.rglob("*") if p.is_file())

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press(tab)
        await press(app, pilot, button)
        assert isinstance(app.screen, FormModal)
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, FormModal)

    drive(scenario)
    assert sorted((p, p.read_bytes()) for p in saved.rglob("*") if p.is_file()) == before


def _focused_id(app: ManageApp) -> str | None:
    return app.focused.id if app.focused is not None else None


@pytest.mark.parametrize(
    ("tab", "button", "field"),
    [
        ("6", "#smoke", "provider"),
        ("3", "#set-profile", "profile"),
        ("5", "#export", "format"),
        ("5", "#prune", "days"),
    ],
)
def test_a_form_that_deletes_nothing_opens_in_its_field(
    espanso: FakeRunner, tab: str, button: str, field: str
) -> None:
    """#392: only a dialog that destroys something opens on Cancel."""

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press(tab)
        await press(app, pilot, button)
        assert isinstance(app.screen, FormModal)
        assert _focused_id(app) == field

    drive(scenario)


def test_a_form_refuses_to_focus_a_field_it_does_not_have() -> None:
    with pytest.raises(ValueError, match="no field 'nope'"):
        FormModal("Title", [Field("days", "Days")], focus="nope")


def test_set_key_opens_with_the_cursor_in_a_field(saved: Path, espanso: FakeRunner) -> None:
    """#392: with the key picked, typing goes into the hidden value and Enter saves it;
    without one, the key picker has the focus."""

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "OPENROUTER_API_KEY", "enter")
        assert isinstance(app.screen, FormModal)
        assert _focused_id(app) == "key-value"
        await pilot.press(*"sk-typed", "enter")
        await settle(pilot)
        assert not isinstance(app.screen, FormModal)
        assert settings_rows(app)["OPENROUTER_API_KEY"].value == "set"
        settings_tab(app).set_key()
        await settle(pilot)
        assert isinstance(app.screen, FormModal)
        assert _focused_id(app) == "key-name"

    drive(scenario)
    assert config_store.saved_secret_names() == ("OPENROUTER_API_KEY",)


def test_a_destructive_dialog_still_opens_on_cancel(saved: Path, espanso: FakeRunner) -> None:
    """#392: Deploy, Detach, Remove a key and every confirmation keep Cancel focused, so
    Enter right after one opens changes nothing."""
    config_store.save_secret("ANTHROPIC_API_KEY", KEY)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "ANTHROPIC_API_KEY", "u")
        assert isinstance(app.screen, FormModal)
        assert _focused_id(app) == "cancel"
        await press(app, pilot, "#submit")
        assert isinstance(app.screen, ConfirmModal)
        assert _focused_id(app) == "cancel"
        await pilot.press("enter")
        await settle(pilot)
        assert config_store.saved_secret_names() == ("ANTHROPIC_API_KEY",)

        await pilot.press("4")
        await press(app, pilot, "#deploy")
        assert isinstance(app.screen, FormModal)
        assert _focused_id(app) == "cancel"
        await pilot.press("enter")
        await settle(pilot)
        assert not list((espanso.root / "match").iterdir())
        await press(app, pilot, "#deploy")
        await press(app, pilot, "#submit")
        await press(app, pilot, "#detach")
        assert isinstance(app.screen, FormModal)
        assert _focused_id(app) == "cancel"
        await pilot.press("enter")
        await settle(pilot)
        assert (espanso.root / "match" / "prompts-llm.yml").is_file()

    drive(scenario)


@pytest.mark.parametrize(
    ("row", "picked"),
    [
        ("general", "general"),  # built in, not the list's first choice
        ("user:other", "other"),  # one of yours, added
        ("user:general", "general"),  # yours, replacing a built-in (overrides)
        ("user:default-pro", "default-pro"),  # no longer a retired name (#414): yours, added
        ("user:Bad", "mine"),  # an invalid name: the current default
        ("user:default", "mine"),  # an override without its file (missing): the same
    ],
)
def test_set_default_opens_on_the_highlighted_profile(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch, row: str, picked: str
) -> None:
    """#392: Set default starts at the highlighted row, not at the current profile. The
    current one (`mine`) is not the list's first choice, so a value the list does not offer
    (which falls back to that first choice) cannot pass for the fallback."""
    from promptmend.prompt_builder import ADDED, INVALID_NAME, MISSING, OVERRIDES

    monkeypatch.setenv("PROMPT_PROFILE", "mine")
    monkeypatch.setenv("PROMPT_PROFILE_OVERRIDES", "general,default")
    for name in ("mine", "other", "general", "default-pro", "Bad"):
        _user_profile(name)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        from textual.widgets import DataTable

        await pilot.press("3")
        await settle(pilot)
        statuses = {p.name: p.status for p in _state(app).profiles}
        assert statuses == {
            "mine": ADDED,
            "other": ADDED,
            "general": OVERRIDES,
            "default-pro": ADDED,
            "Bad": INVALID_NAME,
            "default": MISSING,
        }
        profiles = pane(app, "profiles")
        assert isinstance(profiles, profiles_pane.ProfilesPane)
        table = profiles.query_one("#profiles", DataTable)
        table.move_cursor(row=table.get_row_index(row))
        assert profiles.highlighted(_state(app)) == picked
        await press(app, pilot, "#set-profile")
        assert isinstance(app.screen, FormModal)
        assert app.screen.query_one("#profile", Select).value == picked

    drive(scenario)


def test_no_color_is_left_to_textual(monkeypatch: pytest.MonkeyPatch, espanso: FakeRunner) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert app.no_color

    drive(scenario)


# --- Home ---------------------------------------------------------------------------------


def _rendered(widget: Widget, selector: str, width: int = 116) -> str:
    """What a Static or an OptionList (Home's rows, #395) shows, as plain text at ``width``
    columns (Home's grids are Rich tables, not Text)."""
    from rich.console import Console

    console = Console(width=width, record=True, color_system=None, file=io.StringIO())
    found = widget.query_one(selector)
    if isinstance(found, OptionList):
        for option in found.options:
            console.print(option.prompt)
    else:
        assert isinstance(found, Static)
        console.print(found.content)
    return console.export_text()


def test_home_shows_doctor_and_checks_again(espanso: FakeRunner) -> None:
    calls: list[str] = []

    def loader(group_by: str, first: State | None = None) -> State:
        calls.append(group_by)
        return gather(group_by, first)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        home = app.main.query_one("#home-pane", home_pane.HomePane)
        rows = {row.label: row for row in home.rows}
        assert rows["Triggers"].text.endswith("Espanso running")
        assert (rows["Rewrites"].status, rows["Rewrites"].detail) == ("fail", "key not set")
        # Nothing deployed yet: the missing files fail; the key names the triggers (#217).
        assert rows["Checks"].problem.startswith("match_files: prompts-core.yml: missing")
        assert app.state is not None
        keys = next(c for c in app.state.report.checks if c.id == "keys")
        assert keys.message == (
            "-i-, -iok-, -ip-, -if- will use OpenRouter once deployed, "
            "but OPENROUTER_API_KEY is not set"
        )
        shown = _rendered(home, "#home-headline") + _rendered(home, "#home-rows")
        assert "Not ready: OPENROUTER_API_KEY is not set, so -i- cannot rewrite." in shown
        assert "fail  Rewrites" not in shown  # the label is FAIL, as on the Checks tab
        assert "FAIL  Rewrites" in shown
        assert "-> 2 Settings" in shown
        await press(app, pilot, "#home-reload")
        await pilot.press("r")
        await settle(pilot)

    drive(scenario, loader=loader)
    assert calls == ["trigger"] * 3
    # Doctor never read the clipboard, and only read-only Espanso commands ran.
    assert {" ".join(c[:2]) for c in espanso.calls} <= {"espanso path", "espanso status"}


# A persona matching the gate's patterns is a warning on Home and on the Checks tab, by finding
# label only (#29 B).
def test_home_warns_of_a_persona_matching_the_patterns(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "jane.doe" + "@" + "example.com"
    monkeypatch.setenv("PROMPT_PERSONA", f"I am an analyst, mail {address}.")
    warning = (
        "persona: PROMPT_PERSONA matches the data-protection patterns: email; it is sent "
        "unscanned with every cloud call"
    )

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "diagnostics")
        home = _rendered(pane(app, "home"), "#home-rows")
        home_tab = app.main.query_one("#home-pane", home_pane.HomePane)
        checks = {row.label: row for row in home_tab.rows}["Checks"]
        assert checks.status == "fail"  # the missing key; the persona is the warning
        assert " 0 warn" not in checks.text
        everything = str(pane(app, "diagnostics").query_one("#all-checks").render())
        assert warning in everything
        assert address not in home + everything

    drive(scenario)


# --- Settings -----------------------------------------------------------------------------


def test_settings_set_a_key_never_shows_it(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "settings")
        assert settings_rows(app)["OPENROUTER_API_KEY"].value == "not set"
        # Space on a key's row does nothing; Enter opens the hidden Set key dialog.
        await on_row(app, pilot, "OPENROUTER_API_KEY", "space")
        assert not isinstance(app.screen, FormModal)
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, FormModal)
        assert app.screen.query_one("#key-name", Select).value == "OPENROUTER_API_KEY"
        assert app.screen.query_one("#key-value", Input).password
        await fill(app, pilot, key_value=f"  {KEY} ")
        assert KEY not in app.export_screenshot()
        row = settings_rows(app)["OPENROUTER_API_KEY"]
        assert (row.value, row.custom, row.source) == ("set", True, "secrets.toml")
        assert KEY not in pane(app, "settings").last_message
        assert app.session[-1].command == "promptmend secrets set OPENROUTER_API_KEY"

    drive(scenario)
    assert config_store.saved_secret_names() == ("OPENROUTER_API_KEY",)
    assert KEY in (saved / "secrets.toml").read_text("utf-8")


def test_settings_refuse_an_empty_key_and_legacy_mode(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "OPENROUTER_API_KEY", "enter")
        # Enter in a field submits the dialog, like its Save button.
        app.screen.query_one("#key-value", Input).focus()
        await pilot.press("space", "enter")
        await settle(pilot)
        assert "no value entered" in pane(app, "settings").last_message
        await pilot.press("enter")
        await settle(pilot)
        await fill(app, pilot, key_value=KEY)
        message = pane(app, "settings").last_message
        assert message.startswith("error: PROMPTMEND_ENV is set")
        assert KEY not in message
        await pilot.press("u")
        await settle(pilot)
        assert "PROMPTMEND_ENV is set" in pane(app, "settings").last_message

    drive(scenario)


def test_settings_remove_a_key_after_confirming(saved: Path, espanso: FakeRunner) -> None:
    config_store.save_secret("ANTHROPIC_API_KEY", KEY)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        # u on a key's row is Remove key, with that key picked.
        await on_row(app, pilot, "ANTHROPIC_API_KEY", "u")
        assert isinstance(app.screen, FormModal)
        assert app.screen.query_one("#key-name", Select).value == "ANTHROPIC_API_KEY"
        await press(app, pilot, "#submit")
        assert isinstance(app.screen, ConfirmModal)
        await press(app, pilot, "#cancel")
        assert config_store.saved_secret_names() == ("ANTHROPIC_API_KEY",)
        await pilot.press("u")
        await settle(pilot)
        await fill(app, pilot)
        await pilot.press("y")
        await settle(pilot)
        assert pane(app, "settings").last_message == (
            "ANTHROPIC_API_KEY removed from the secrets file."
        )
        assert app.session[-1].command == "promptmend secrets remove ANTHROPIC_API_KEY"
        assert settings_rows(app)["ANTHROPIC_API_KEY"].value == "not set"
        await pilot.press("u")
        await settle(pilot)
        assert "holds no key" in pane(app, "settings").last_message

    drive(scenario)
    assert config_store.saved_secret_names() == ()


def test_settings_remove_a_key_still_set_elsewhere(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_store.save_secret("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "OPENROUTER_API_KEY", "u")
        await fill(app, pilot)
        await press(app, pilot, "#confirm")
        assert pane(app, "settings").last_message.endswith("it is still set, from environment.")

    drive(scenario)


def test_an_unexpected_error_is_shown_not_raised(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bug(name: str, value: str) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(config_store, "save_secret", bug)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "OPENROUTER_API_KEY", "enter")
        await fill(app, pilot, key_value=KEY)
        message = pane(app, "settings").last_message
        assert message == "error: unexpected RuntimeError: kaput"

    drive(scenario)


def test_settings_space_toggles_history(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "settings")
        row = settings_rows(app)["PROMPT_HISTORY"]
        assert (row.value, row.custom, row.source) == ("on", False, "default")
        await on_row(app, pilot, "PROMPT_HISTORY", "space")
        assert not _state(app).settings.history
        row = settings_rows(app)["PROMPT_HISTORY"]
        assert (row.value, row.custom, row.source) == ("off", True, "config.toml")
        assert app.session[-1].command == "promptmend config set PROMPT_HISTORY false"
        assert pane(app, "settings").last_message.startswith("PROMPT_HISTORY saved in ")
        # The cursor stays on the row after the reload; Enter on a switch toggles it too.
        assert settings_tab(app).cursor == "PROMPT_HISTORY"
        await pilot.press("enter")
        await settle(pilot)
        assert _state(app).settings.history
        # Space on a row that is not a switch does nothing.
        await on_row(app, pilot, "PROMPT_OUTPUT", "space")
        assert not isinstance(app.screen, PickModal)
        assert len(app.session) == 2

    drive(scenario)
    assert "PROMPT_HISTORY = true" in (saved / "config.toml").read_text("utf-8")


def test_settings_enter_picks_the_output_and_u_resets_it(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_OUTPUT", "enter")
        screen = app.screen
        assert isinstance(screen, PickModal)
        # The current value is marked and highlighted, as is the default; Escape changes
        # nothing.
        assert screen.choices == [
            ("paste  (current, default)", "paste"),
            ("clipboard", "clipboard"),
        ]
        assert screen.query_one("#pick", OptionList).highlighted == 0
        await pilot.press("escape")
        await settle(pilot)
        assert app.session == []
        await pick(app, pilot, "PROMPT_OUTPUT", "clipboard")
        assert _state(app).settings.output == "clipboard"
        row = settings_rows(app)["PROMPT_OUTPUT"]
        assert (row.value, row.custom, row.source) == ("clipboard", True, "config.toml")
        assert app.session[-1].command == "promptmend config set PROMPT_OUTPUT clipboard"
        # Home's Output row says it, and where to change it (#198).
        rows = {r.label: r for r in app.main.query_one("#home-pane", home_pane.HomePane).rows}
        assert (rows["Output"].text, rows["Output"].detail) == (
            "copy to the clipboard",
            "change: 2 Settings",
        )
        # u resets it (config unset): back to the default, logged as its command.
        await pilot.press("u")
        await settle(pilot)
        assert _state(app).settings.output == "paste"
        assert app.session[-1].command == "promptmend config unset PROMPT_OUTPUT"
        assert "removed from" in pane(app, "settings").last_message
        assert settings_rows(app)["PROMPT_OUTPUT"].custom is False
        await pilot.press("u")
        await settle(pilot)
        assert "nothing to do" in pane(app, "settings").last_message

    drive(scenario)
    assert "PROMPT_OUTPUT" not in (saved / "config.toml").read_text("utf-8")


def test_settings_use_default_in_the_pick_dialog_resets(saved: Path, espanso: FakeRunner) -> None:
    """#388: the pick dialog marks the default and offers Use default, which is `u` (config
    unset): the same service, logged as its command."""
    from promptmend.tui import teach

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pick(app, pilot, "PROMPT_OUTPUT", "clipboard")
        await on_row(app, pilot, "PROMPT_OUTPUT", "enter")
        screen = app.screen
        assert isinstance(screen, PickModal)
        assert screen.choices == [
            ("paste  (default)", "paste"),
            ("clipboard  (current)", "clipboard"),
        ]
        assert screen.query_one("#pick", OptionList).has_focus
        button = screen.query_one("#use-default", Button)
        assert button.tooltip == teach.tooltip("use-default")
        assert button.tooltip == "In a terminal:\n$ promptmend config unset <NAME>"
        await press(app, pilot, "#use-default")
        assert not isinstance(app.screen, PickModal)
        assert _state(app).settings.output == "paste"
        assert app.session[-1].command == "promptmend config unset PROMPT_OUTPUT"
        assert "removed from" in pane(app, "settings").last_message
        assert settings_rows(app)["PROMPT_OUTPUT"].custom is False

    drive(scenario)
    assert "PROMPT_OUTPUT" not in (saved / "config.toml").read_text("utf-8")


def test_settings_an_effort_pick_list_keeps_not_sent_apart_from_none(
    saved: Path, espanso: FakeRunner
) -> None:
    """#388: an empty effort is `not sent`, never `none`, which is a real effort."""

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "OPENAI_REASONING_EFFORT", "enter")
        screen = app.screen
        assert isinstance(screen, PickModal)
        assert screen.choices[:3] == [("not sent", ""), ("none", "none"), ("minimal", "minimal")]
        assert ("low  (current, default)", "low") in screen.choices
        await pilot.press("escape")
        await settle(pilot)
        assert app.session == []

    drive(scenario)


def test_settings_use_default_on_an_environment_value_unsets_nothing(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPT_TIMEOUT_SECONDS", "40")
    before = sorted((p, p.read_bytes()) for p in saved.rglob("*") if p.is_file())

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_TIMEOUT_SECONDS", "enter")
        assert isinstance(app.screen, EditModal)
        await press(app, pilot, "#use-default")
        assert app.session[-1].command == "promptmend config unset PROMPT_TIMEOUT_SECONDS"
        message = pane(app, "settings").last_message
        assert message.endswith("nothing to do. It comes from environment.")
        assert _state(app).settings.timeout == 40

    drive(scenario)
    assert sorted((p, p.read_bytes()) for p in saved.rglob("*") if p.is_file()) == before


def test_settings_use_default_in_the_edit_dialog_resets(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await edit_to(app, pilot, "PROMPT_TIMEOUT_SECONDS", "40")
        assert settings_rows(app)["PROMPT_TIMEOUT_SECONDS"].custom
        await pilot.press("enter")
        await settle(pilot)
        screen = app.screen
        assert isinstance(screen, EditModal)
        assert screen.query_one("#edit-value", Input).has_focus
        assert "Default: 30" in screen.preview
        # Escape still changes nothing; Use default resets, whatever the field holds.
        await pilot.press("escape")
        await settle(pilot)
        assert app.session[-1].command == "promptmend config set PROMPT_TIMEOUT_SECONDS 40"
        await pilot.press("enter")
        await settle(pilot)
        app.screen.query_one("#edit-value", Input).value = "55"
        await press(app, pilot, "#use-default")
        assert not isinstance(app.screen, EditModal)
        assert _state(app).settings.timeout == 30
        assert app.session[-1].command == "promptmend config unset PROMPT_TIMEOUT_SECONDS"
        assert settings_rows(app)["PROMPT_TIMEOUT_SECONDS"].custom is False
        # An empty default is shown as its word, not "(empty)".
        await on_row(app, pilot, "OLLAMA_NUM_CTX", "enter")
        assert "Default: Ollama's own" in app.screen.preview  # type: ignore[attr-defined]

    drive(scenario)
    assert "PROMPT_TIMEOUT_SECONDS" not in (saved / "config.toml").read_text("utf-8")


def test_settings_use_default_resets_a_withheld_value(saved: Path, espanso: FakeRunner) -> None:
    saved.mkdir(parents=True, exist_ok=True)
    toml = saved / "config.toml"
    toml.write_text(f'OLLAMA_MODEL = "{KEY}"\n', encoding="utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "OLLAMA_MODEL", "enter")
        screen = app.screen
        assert isinstance(screen, EditModal)
        assert screen.query_one("#edit-value", Input).value == ""
        await press(app, pilot, "#use-default")
        assert app.session[-1].command == "promptmend config unset OLLAMA_MODEL"
        assert _state(app).settings.ollama_model == settings_model.default("OLLAMA_MODEL")
        assert KEY not in app.export_screenshot()

    drive(scenario)
    assert KEY not in toml.read_text("utf-8")


def test_settings_a_theme_change_redraws_the_dot(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pick(app, pilot, "PROMPT_OUTPUT", "clipboard")
        await on_row(app, pilot, "PROMPT_PROFILE")
        listing = settings_tab(app).query_one(settings_pane.SettingsList)

        def dot_colour() -> str:
            from rich.console import Console
            from rich.table import Table

            prompt = listing.get_option("PROMPT_OUTPUT").prompt
            assert isinstance(prompt, Table)
            segment = next(s for s in Console().render(prompt) if s.text == "●")
            assert segment.style is not None
            assert segment.style.color is not None
            return segment.style.color.name

        assert dot_colour() == app.current_theme.accent
        app.action_toggle_contrast()
        await settle(pilot)
        assert app.theme == HIGH_CONTRAST.name
        assert dot_colour() == HIGH_CONTRAST.accent

    drive(scenario)


def test_setting_line_dims_only_the_default_source() -> None:
    """#388: a row at its default is not dimmed (only the word `default` is muted, and not
    under the cursor); a value set by you has a filled dot in the accent, and in the bar's own
    colour under the cursor."""
    from rich.console import Console
    from rich.style import Style

    def parts(row: settings_model.Row, **kwargs: Any) -> dict[str, Style]:
        line = settings_pane.setting_line(row, **kwargs)
        segments = Console(width=80).render(line)
        return {s.text.strip(): s.style or Style() for s in segments if s.text.strip()}

    meta = settings_model.BY_NAME["PROMPT_TIMEOUT_SECONDS"]
    at_default = settings_model.row(meta, config.Entry("30", config.DEFAULT_SOURCE))
    seen = parts(at_default, cursor=False, accent="#ffa62b")
    assert set(seen) == {"○", "PROMPT_TIMEOUT_SECONDS", "30", "default"}
    assert {text for text, style in seen.items() if style.dim} == {"default"}
    assert seen["○"].color is None
    assert not seen["○"].bold

    custom = settings_model.row(meta, config.Entry("40", config.ENV_SOURCE))
    seen = parts(custom, cursor=False, accent="#ffa62b")
    assert set(seen) == {"●", "PROMPT_TIMEOUT_SECONDS", "40", "environment"}
    assert not any(style.dim for style in seen.values())
    assert seen["●"].bold
    assert seen["●"].color is not None
    assert seen["●"].color.name == "#ffa62b"
    # Under the cursor bar the dot is bold in the bar's text colour (with no accent too),
    # and nothing is dimmed.
    for kwargs in ({"cursor": True, "accent": "#ffa62b"}, {"cursor": False}):
        dot = next(style for text, style in parts(custom, **kwargs).items() if "●" in text)
        assert dot.bold
        assert dot.color is None
    assert not any(style.dim for style in parts(at_default, cursor=True).values())
    # A value from a file named `default` is yours: filled, and its source not muted.
    from_file = settings_model.row(meta, config.Entry("40", "file:/x/default"))
    seen = parts(from_file, cursor=False, accent="#ffa62b")
    assert from_file.source == "default"
    assert "●" in seen
    assert not any(style.dim for style in seen.values())


def test_settings_r_reloads_and_never_resets(saved: Path, espanso: FakeRunner) -> None:
    """#215: `r` is Reload on every tab, the Settings list too; reset is `u`."""
    calls: list[str] = []

    def loader(group_by: str, first: State | None = None) -> State:
        calls.append(group_by)
        return gather(group_by, first)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pick(app, pilot, "PROMPT_OUTPUT", "clipboard")
        loads = len(calls)
        await on_row(app, pilot, "PROMPT_OUTPUT", "r")
        assert len(calls) == loads + 1
        assert _state(app).settings.output == "clipboard"
        assert app.session[-1].command == "promptmend config set PROMPT_OUTPUT clipboard"

    drive(scenario, loader=loader)
    assert 'PROMPT_OUTPUT = "clipboard"' in (saved / "config.toml").read_text("utf-8")


def test_settings_ignore_a_second_change_until_the_first_is_read_back(
    saved: Path, espanso: FakeRunner
) -> None:
    """Review of #199: the rows show the state before a save until the reload ends, so a
    quick second Space would save the same value again; it is refused, with a notice."""
    import threading

    release = threading.Event()
    blocking = threading.Event()

    def loader(group_by: str, first: State | None = None) -> State:
        if blocking.is_set():
            release.wait(10)
        return gather(group_by, first)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_HISTORY")
        blocking.set()
        notices: list[str] = []
        real_notify = app.notify

        def notify(message: str, **kwargs: Any) -> None:
            notices.append(message)
            real_notify(message, **kwargs)

        app.notify = notify  # type: ignore[method-assign]
        await pilot.press("space", "space", "enter", "u")
        await pilot.pause()
        assert [e.command for e in app.session] == ["promptmend config set PROMPT_HISTORY false"]
        assert notices.count("Still reading back the last change; try again.") == 3
        assert not isinstance(app.screen, PickModal | EditModal)
        blocking.clear()
        release.set()
        await settle(pilot)
        assert not settings_tab(app).pending
        assert settings_rows(app)["PROMPT_HISTORY"].value == "off"
        await pilot.press("space")
        await settle(pilot)
        assert app.session[-1].command == "promptmend config set PROMPT_HISTORY true"
        assert _state(app).settings.history

    # One stage: the quick one reads the change back on the loop at once (#317, #348), so a
    # slow read-back is the full read's.
    drive(scenario, loader=loader, quick_loader=None)


def test_settings_a_withheld_value_is_kept_by_an_empty_save(
    saved: Path, espanso: FakeRunner
) -> None:
    """Review of #199: a value that looks like a key opens as an empty field, says so, and
    saving the field empty keeps it (never a silent wipe)."""
    saved.mkdir(parents=True, exist_ok=True)
    toml = saved / "config.toml"
    toml.write_text(f'OLLAMA_MODEL = "{KEY}"\n', encoding="utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "settings")
        assert KEY not in settings_rows(app)["OLLAMA_MODEL"].value
        await on_row(app, pilot, "OLLAMA_MODEL", "enter")
        screen = app.screen
        assert isinstance(screen, EditModal)
        assert screen.query_one("#edit-value", Input).value == ""
        assert settings_pane.WITHHELD_CURRENT in screen.preview
        assert KEY not in screen.preview
        await press(app, pilot, "#submit")
        assert not isinstance(app.screen, EditModal)
        assert app.session == []
        # A value that does not look like a key opens prefilled, and may be saved empty.
        await edit_to(app, pilot, "PROMPT_TEMPERATURE", "")
        assert _state(app).settings.temperature is None
        await on_row(app, pilot, "OLLAMA_NUM_CTX", "enter")
        assert settings_pane.WITHHELD_CURRENT not in app.screen.preview  # type: ignore[attr-defined]

    drive(scenario)
    assert f'OLLAMA_MODEL = "{KEY}"' in toml.read_text("utf-8")  # kept, not wiped


def test_settings_reset_says_where_a_value_comes_from(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPT_TIMEOUT_SECONDS", "40")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_TIMEOUT_SECONDS", "u")
        message = pane(app, "settings").last_message
        assert message.endswith("nothing to do. It comes from environment.")

    drive(scenario)


def test_settings_edit_checks_the_value_in_the_dialog(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_TIMEOUT_SECONDS", "enter")
        screen = app.screen
        assert isinstance(screen, EditModal)
        field = screen.query_one("#edit-value", Input)
        assert field.value == "30"
        assert field.has_focus
        assert "Default: 30" in screen.preview
        # A bad value stays in the dialog with the parser's message; nothing is saved.
        field.value = "soon"
        await press(app, pilot, "#submit")
        assert app.screen is screen
        error = str(screen.query_one("#edit-error", Static).render())
        assert error.startswith("PROMPT_TIMEOUT_SECONDS must be a number above 0")
        field.value = KEY
        await pilot.pause()
        await pilot.press("enter")  # Enter in the field submits too
        await settle(pilot)
        assert app.screen is screen
        refused = str(screen.query_one("#edit-error", Static).render())
        assert refused == settings_service.KEY_LIKE  # `config set`'s own words (#408)
        assert KEY not in str(screen.query_one("#edit-error", Static).render())
        await pilot.press("escape")
        await settle(pilot)
        assert app.session == []
        await edit_to(app, pilot, "PROMPT_TIMEOUT_SECONDS", "45")
        assert _state(app).settings.timeout == 45
        assert settings_rows(app)["PROMPT_TIMEOUT_SECONDS"].value == "45.0"  # as saved
        result = _rendered(pane(app, "settings"), ".result")
        assert result.startswith("$ promptmend config set PROMPT_TIMEOUT_SECONDS 45\n")

    drive(scenario)
    text = (saved / "config.toml").read_text("utf-8")
    assert "PROMPT_TIMEOUT_SECONDS = " in text
    assert KEY not in text


def test_settings_local_only_shows_in_the_diagnostics_routes(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPT_PROFILE", "general")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "diagnostics")
        await on_row(app, pilot, "PROMPT_LOCAL_ONLY", "space")
        assert _state(app).settings.local_only
        routes = table_rows(app, "diagnostics", "#routes")
        assert routes["openrouter"][3] == routes["anthropic"][3] == "refused (local only)"
        assert routes["ollama"][3] == "stays local"
        policy = str(pane(app, "diagnostics").query_one("#policy").render())
        assert policy.startswith("PROMPT_LOCAL_ONLY is on")
        # A real environment variable outranks what is saved, and the result says so.
        await pick(app, pilot, "PROMPT_PROFILE", "default")
        assert "overrides the saved value" in pane(app, "settings").last_message

    drive(scenario)
    assert "PROMPT_LOCAL_ONLY = true" in (saved / "config.toml").read_text("utf-8")


def test_settings_never_show_the_persona(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    persona = "I am the head of a secret project"

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "settings")
        assert settings_rows(app)["PROMPT_PERSONA"].value == "none"
        await edit_to(app, pilot, "PROMPT_PERSONA", persona)
        row = settings_rows(app)["PROMPT_PERSONA"]
        assert (row.value, row.custom) == ("<set, hidden>", True)
        assert persona not in app.export_screenshot()
        assert app.session[-1].command == "promptmend config set PROMPT_PERSONA <value withheld>"
        assert persona not in pane(app, "settings").last_message
        # Its own dialog is prefilled (only the owner sees it); the list never shows it.
        await pilot.press("enter")
        await settle(pilot)
        assert app.screen.query_one("#edit-value", Input).value == persona
        await pilot.press("escape")
        await settle(pilot)
        await pilot.press("6")
        assert persona not in app.export_screenshot()

    drive(scenario)
    assert persona in (saved / "config.toml").read_text("utf-8")


def option_ids(app: ManageApp) -> list[str | None]:
    """The Settings list's lines, by option id: headings, summary lines, settings, the fold."""
    listing = settings_tab(app).query_one(settings_pane.SettingsList)
    return [listing.get_option_at_index(n).id for n in range(listing.option_count)]


def test_settings_filter(espanso: FakeRunner) -> None:
    total = len(settings_model.SETTINGS)
    basic = sum(not m.advanced for m in settings_model.SETTINGS)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "settings")
        settings = settings_tab(app)
        count = settings.query_one("#settings-count", Static)
        assert str(count.render()) == f"{basic} of {total} settings"
        await on_row(app, pilot, "PROMPT_OUTPUT", "slash")
        assert settings.query_one(settings_pane.FilterInput).has_focus
        await pilot.press(*"hist")
        await settle(pilot)
        # Every match shows, one behind the closed fold too; no fold line, no summary lines.
        assert list(settings.rows) == ["PROMPT_HISTORY", "PROMPT_HISTORY_RETENTION_DAYS"]
        assert option_ids(app) == [
            "group:basic:History",
            "PROMPT_HISTORY",
            "PROMPT_HISTORY_RETENTION_DAYS",
        ]
        assert not settings.advanced
        assert str(count.render()) == f"2 of {total}"
        assert settings.cursor == "PROMPT_HISTORY"
        # A group's name matches too; Enter and Down go back to the list, keeping the filter.
        await pilot.press("enter")
        assert settings.query_one(settings_pane.SettingsList).has_focus
        await pilot.press("slash", *"privacy")  # the filter's text is selected: typing replaces it
        await settle(pilot)
        assert list(settings.rows) == [
            "PROMPT_LOCAL_ONLY",
            "PROMPT_GATE_LOCAL",
            "ALLOW_CLOUD_OVERRIDE",
            "PROMPT_UPDATE_CHECK",
            "PROMPT_EXTRA_PATTERNS",
            "PROMPT_GATE_PACKS",
            "OPENROUTER_DATA_COLLECTION",
        ]
        assert option_ids(app).count("group:basic:Privacy") == 1  # one heading for both
        await pilot.press("down")
        assert settings.query_one(settings_pane.SettingsList).has_focus
        await pilot.press("slash", *"zzz")
        await settle(pilot)
        assert settings.rows == {}
        assert option_ids(app) == []
        assert "No setting matches 'zzz'" in str(settings.query_one("#settings-help").render())
        # Nothing to act on: the keys do nothing.
        settings.toggle()
        settings.edit()
        settings.reset()
        assert app.session == []
        # Escape clears it and goes back to the list, the fold closed as before.
        await pilot.press("escape")
        await settle(pilot)
        assert settings.query_one(settings_pane.SettingsList).has_focus
        assert len(settings.rows) == basic
        assert option_ids(app)[-1] == settings_pane.FOLD
        assert str(count.render()) == f"{basic} of {total} settings"

    drive(scenario)


def test_settings_a_filter_finds_an_advanced_row_and_clearing_closes_the_fold(
    saved: Path, espanso: FakeRunner
) -> None:
    """#389: the filter searches all 48 whatever the fold; cleared, the fold is as it was,
    and the cursor on a setting it hides goes to the fold line."""

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_OUTPUT", "slash", *"num_ctx", "enter")
        settings = settings_tab(app)
        assert not settings.advanced
        assert list(settings.rows) == ["OLLAMA_NUM_CTX"]
        assert settings.cursor == "OLLAMA_NUM_CTX"
        assert settings_pane.FOLD not in option_ids(app)
        assert not any(str(i).startswith("summary:") for i in option_ids(app))
        # It can be changed from there, through config set as ever.
        await edit_to(app, pilot, "OLLAMA_NUM_CTX", "8192")
        assert _state(app).settings.ollama_num_ctx == 8192
        assert list(settings.rows) == ["OLLAMA_NUM_CTX"]
        await pilot.press("slash", "escape")
        await settle(pilot)
        assert not settings.advanced
        assert "OLLAMA_NUM_CTX" not in settings.rows
        assert settings.cursor == settings_pane.FOLD
        # With the fold open, clearing a filter leaves it open.
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press("slash", *"think", "escape")
        await settle(pilot)
        assert settings.advanced
        assert len(settings.rows) == len(settings_model.SETTINGS)

    drive(scenario)


@pytest.mark.parametrize("query", ["prompt", "o", "privacy", "openrouter"])
def test_settings_a_filter_shows_each_group_once(espanso: FakeRunner, query: str) -> None:
    """#389: under a filter there is no fold line, so each group has one heading, its basic
    matches first, then its advanced ones."""

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_OUTPUT", "slash", *query, "enter")
        settings = settings_tab(app)
        ids = option_ids(app)
        headings = [str(i).rsplit(":", 1)[1] for i in ids if str(i).startswith("group:")]
        assert headings
        assert len(headings) == len(set(headings))
        assert headings == sorted(headings, key=settings_model.GROUPS.index)
        shown = [settings_model.BY_NAME[n] for n in settings.rows]
        assert [n for n in ids if n in settings_model.BY_NAME] == list(settings.rows)
        assert shown == sorted(
            shown, key=lambda m: (settings_model.GROUPS.index(m.group), m.advanced)
        )
        assert {m.name for m in shown} == {r.name for r in settings_model.rows(_state(app), query)}

    drive(scenario)


def in_view(app: ManageApp, option_id: str) -> bool:
    """Whether the Settings list's line ``option_id`` is on screen (one line per option)."""
    listing = settings_tab(app).query_one(settings_pane.SettingsList)
    line = listing.get_option_index(option_id)
    return listing.scroll_y <= line < listing.scroll_y + listing.size.height


@pytest.mark.parametrize(
    "keys",
    # Page Down skips a heading it lands on, so one Page Up can stop a setting short of the
    # first: a second one gets there (the list is a line taller since #396 moved Test call).
    [("pagedown", "pageup", "pageup"), ("end", "home"), ("k", "j")],
    ids=["page", "end-home", "k-j"],
)
def test_settings_the_summary_lines_come_back_into_view(
    espanso: FakeRunner, keys: tuple[str, ...]
) -> None:
    """#389, review: the heading and summary lines are never highlighted, so moving back to
    the first setting scrolls them into view with it."""

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "OPENROUTER_MODEL")
        listing = settings_tab(app).query_one(settings_pane.SettingsList)
        await pilot.press(keys[0])
        await pilot.pause()
        assert listing.scroll_y > 0
        await pilot.press(*keys[1:])
        await pilot.pause()
        assert settings_tab(app).cursor == "OPENROUTER_MODEL"
        assert listing.scroll_y == 0
        for line in ("group:basic:Triggers", "summary:-i-", "summary:-ip-"):
            assert in_view(app, line)

    drive(scenario, size=(80, 24))


def test_settings_moving_up_into_a_group_shows_its_heading(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_OUTPUT")
        await pilot.press("end")
        await pilot.pause()
        await on_row(app, pilot, "PROMPT_GATE_LOCAL", "up")
        assert settings_tab(app).cursor == "PROMPT_LOCAL_ONLY"
        assert in_view(app, "group:basic:Privacy")
        assert in_view(app, "PROMPT_LOCAL_ONLY")

    drive(scenario, size=(80, 24))


@pytest.mark.parametrize("key", ["r", "t"])
def test_settings_a_refill_keeps_the_open_fold_at_the_top(espanso: FakeRunner, key: str) -> None:
    """#389, review: on the open fold's line, a reload or a theme change keeps the fold line at
    the top with the advanced rows below it."""

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "OPENROUTER_MODEL", "end", "enter")
        settings = settings_tab(app)
        listing = settings.query_one(settings_pane.SettingsList)
        assert settings.advanced
        fold = listing.get_option_index(settings_pane.FOLD)
        assert listing.scroll_y == fold
        await pilot.press(key)
        await settle(pilot)
        assert settings.advanced
        assert settings.cursor == settings_pane.FOLD
        assert listing.scroll_y == fold
        assert in_view(app, "PROMPT_TEMPERATURE")
        # Closed again, it is the list's last line, in view at the bottom.
        await pilot.press("enter")
        await settle(pilot)
        assert in_view(app, settings_pane.FOLD)

    drive(scenario, size=(80, 24))


def test_settings_first_screen_shows_what_the_triggers_run(espanso: FakeRunner) -> None:
    """#389, at 80x24: with the fold closed the list fits two screens, and the first shows
    the Triggers heading, its two lines and the four settings under it."""

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "settings")
        await pilot.press("2")
        await settle(pilot)
        listing = settings_tab(app).query_one(settings_pane.SettingsList)
        height = listing.size.height
        assert height >= 10
        ids = option_ids(app)
        assert len(ids) <= 2 * height
        assert ids[-1] == settings_pane.FOLD
        assert listing.scroll_y == 0
        first = [
            "group:basic:Triggers",
            "summary:-i-",
            "summary:-ip-",
            "OPENROUTER_MODEL",
            "OPENROUTER_PRO_MODEL",
            "PROMPT_PRO_PROVIDER",
            "OPENROUTER_API_KEY",
        ]
        assert ids[: len(first)] == first
        assert len(first) <= height
        assert settings_tab(app).cursor == "OPENROUTER_MODEL"
        # The fold line says how many settings it holds.
        fold = str(listing.get_option(settings_pane.FOLD).prompt)
        assert fold.endswith(f"▸ Advanced: {settings_model.ADVANCED_COUNT} more settings")

    drive(scenario, size=(80, 24))


def summary_words(app: ManageApp, trigger: str) -> str:
    """A summary line under Triggers as plain text, its spaces squeezed."""
    from rich.console import Console

    listing = settings_tab(app).query_one(settings_pane.SettingsList)
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(listing.get_option(f"summary:{trigger}").prompt)
    return " ".join(console.export_text().split())


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"OPENROUTER_API_KEY": KEY},
        {"OPENROUTER_API_KEY": KEY, "PROMPT_PRO_PROVIDER": "anthropic"},
        {"OPENROUTER_API_KEY": KEY, "PROMPT_PRO_PROVIDER": "openai"},
        {"OPENROUTER_API_KEY": KEY, "PROMPT_LOCAL_ONLY": "true"},
    ],
    ids=["no-key", "key", "pro-anthropic", "pro-openai", "local-only"],
)
def test_settings_summary_lines_say_what_home_says(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]
) -> None:
    """#389: the two lines under Triggers are Home's Rewrites and Pro rows, word for word;
    a key not set or a refused provider is said in words, never the key."""
    for name, value in environ.items():
        monkeypatch.setenv(name, value)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "settings")
        await pilot.press("2")
        await settle(pilot)
        # Home's own rows, found by their labels: a wrong label in SUMMARY fails here.
        rows = {r.label: r for r in home.home_rows(_state(app))}
        for trigger, label in (("-i-", "Rewrites"), ("-ip-", "Pro (-ip-)")):
            row = rows[label]
            assert summary_words(app, trigger) == " ".join(
                f"{trigger} {row.text} {row.detail}".split()
            )
        words = summary_words(app, "-i-") + summary_words(app, "-ip-")
        if "OPENROUTER_API_KEY" not in environ:
            assert words.count("key not set") == 2
        if environ.get("PROMPT_PRO_PROVIDER") == "anthropic":
            assert summary_words(app, "-ip-").startswith("-ip- anthropic · ")
            assert summary_words(app, "-ip-").endswith("key not set")
            assert summary_words(app, "-i-").endswith("key set")
        if environ.get("PROMPT_PRO_PROVIDER") == "openai":
            assert summary_words(app, "-ip-").startswith("-ip- openai · ")
            assert summary_words(app, "-ip-").endswith("key not set")
        if environ.get("PROMPT_LOCAL_ONLY"):
            assert words.count("refused") == 2
        assert KEY not in words
        screen = app.export_screenshot()
        assert "OPENROUTER_PRO_MODEL" in screen  # the Settings tab's, not Home's
        assert KEY not in screen

    drive(scenario)


def _summary_text(trigger: str, row: home.HomeRow) -> str:
    from rich.console import Console

    console = Console(width=200, record=True, file=io.StringIO())
    console.print(settings_pane.summary_line(trigger, row))
    return " ".join(console.export_text().split())


def test_settings_summary_line_of_a_failing_row_without_detail() -> None:
    """#389: a deployed trigger naming a provider no route knows is a FAIL with no detail;
    its summary line says so in Home's status word, never looking healthy. A stale deployed
    -ip- says redeploy, as Home does."""
    from test_tui_home import _with_env, ready

    unknown = [
        dataclasses.replace(t, provider="nosuch") if t.trigger == "-i-" else t
        for t in ready().triggers
    ]
    state = dataclasses.replace(ready(), deployed=unknown)
    row = home.trigger_row(state, "Rewrites", "-i-")
    assert (row.status, row.text, row.detail) == (home.FAIL, "nosuch", "")
    assert _summary_text("-i-", row) == "-i- nosuch FAIL"
    older = [
        dataclasses.replace(t, provider="openrouter") if t.trigger == "-ip-" else t
        for t in ready().triggers
    ]
    stale = _with_env(
        dataclasses.replace(ready(), deployed=older),
        OPENROUTER_API_KEY="-".join(("a", "b")),
        PROMPT_PRO_PROVIDER="openai",
    )
    row = home.trigger_row(stale, "Pro (-ip-)", "-ip-")
    assert row.status == home.WARN
    assert _summary_text("-ip-", row) == f"-ip- {row.text} redeploy"
    # An ok row with its detail, and a row still being checked, show only their own words.
    ok = home.HomeRow(home.OK, "Rewrites", "ollama · qwen3:8b", "stays local")
    assert _summary_text("-i-", ok) == "-i- ollama · qwen3:8b stays local"
    waiting = home.HomeRow(home.WAIT, "Rewrites", "checking")
    assert _summary_text("-i-", waiting) == "-i- checking"


def test_settings_cursor_skips_the_summary_lines(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "OPENROUTER_PRO_MODEL", "up")
        settings = settings_tab(app)
        assert settings.cursor == "OPENROUTER_MODEL"
        listing = settings.query_one(settings_pane.SettingsList)
        # Up from the first setting wraps to the fold line, past the summary lines.
        for key in ("up", "home", "pageup", "down", "up"):
            await pilot.press(key)
            await pilot.pause()
            highlighted = listing.highlighted
            assert highlighted is not None
            assert not str(listing.get_option_at_index(highlighted).id).startswith("summary:")
            assert settings.cursor in (*settings.rows, settings_pane.FOLD)
        await pilot.press("home", "up")
        await pilot.pause()
        assert settings.cursor == settings_pane.FOLD
        await pilot.press("pagedown", "pageup", "pageup", "pageup")
        await pilot.pause()
        assert settings.cursor == "OPENROUTER_MODEL"
        # Enter and Space on a summary line cannot happen: it is never highlighted.
        assert listing.get_option("summary:-i-").disabled

    drive(scenario)


def test_settings_the_advanced_fold_opens_and_closes(saved: Path, espanso: FakeRunner) -> None:
    """#389: Enter and Space on the fold line open and close it, the cursor staying there;
    u there does nothing; it stays open across a save, a reload and a tab switch."""
    total = len(settings_model.SETTINGS)
    basic = total - settings_model.ADVANCED_COUNT

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "settings")
        await pilot.press("2")
        await settle(pilot)
        settings = settings_tab(app)
        listing = settings.query_one(settings_pane.SettingsList)
        count = settings.query_one("#settings-count", Static)
        help_line = settings.query_one("#settings-help", Static)
        await to_fold(app, pilot)
        assert "endpoint pins" in str(help_line.render())
        assert "Enter or Space shows them." in str(help_line.render())
        assert str(listing.get_option(settings_pane.FOLD).prompt).startswith("▶▸ Advanced")
        for key, opened in (("enter", True), ("space", False), ("space", True)):
            await pilot.press(key)
            await settle(pilot)
            assert settings.advanced is opened
            assert settings.cursor == settings_pane.FOLD
            assert listing.highlighted == listing.get_option_index(settings_pane.FOLD)
            assert len(settings.rows) == (total if opened else basic)
            shown = f"{total} settings" if opened else f"{basic} of {total} settings"
            assert str(count.render()) == shown
        prompt = str(listing.get_option(settings_pane.FOLD).prompt)
        assert prompt == f"▶{settings_pane.FOLD_OPEN}"
        assert "hides" in prompt
        assert "Enter or Space hides them." in str(help_line.render())
        # The advanced rows follow the fold line, under their own group headings.
        ids = option_ids(app)
        after = ids[ids.index(settings_pane.FOLD) + 1 :]
        assert after[0] == "group:advanced:Output"
        assert [i for i in after if i in settings_model.BY_NAME] == [
            m.name for m in settings_model.SETTINGS if m.advanced
        ]
        assert len(ids) == len(set(ids))
        # u on the fold line does nothing.
        await pilot.press("u")
        await settle(pilot)
        assert app.session == []
        assert settings.advanced
        assert app.screen is app.main
        # A save keeps the fold open and the cursor on the setting.
        await on_row(app, pilot, "OLLAMA_THINK", "space")
        assert _state(app).settings.ollama_think
        assert settings.advanced
        assert settings.cursor == "OLLAMA_THINK"
        # So do a reload and a tab switch.
        await pilot.press("r")
        await settle(pilot)
        await pilot.press("1")
        await settle(pilot)
        await pilot.press("2")
        await settle(pilot)
        assert settings.advanced
        assert settings.cursor == "OLLAMA_THINK"

    drive(scenario)


def test_settings_every_setting_is_reachable(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_HISTORY")
        await open_fold(app, pilot)
        settings = settings_tab(app)
        listing = settings.query_one(settings_pane.SettingsList)
        assert list(settings.rows) == [m.name for m in settings_model.SETTINGS]
        for meta in settings_model.SETTINGS:
            listing.highlighted = listing.get_option_index(meta.name)
            await pilot.pause()
            assert settings.cursor == meta.name
            assert meta.help in str(settings.query_one("#settings-help").render())

    drive(scenario)


def test_settings_keys_act_only_on_their_tab(espanso: FakeRunner) -> None:
    calls: list[str] = []

    def loader(group_by: str, first: State | None = None) -> State:
        calls.append(group_by)
        return gather(group_by, first)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_HISTORY")
        await pilot.press("1")
        await settle(pilot)
        # Home's list takes the focus as its tab opens (#395): nothing of Settings keeps it.
        assert app.focused is pane(app, "home").query_one("#home-rows")
        # r is Reload, as on every tab, and Space nothing.
        await pilot.press("r", "space")
        await settle(pilot)
        assert app.session == []
        assert len(calls) == 2

    drive(scenario, loader=loader)


@pytest.mark.parametrize(
    ("name", "key", "modal"),
    [
        ("OPENROUTER_API_KEY", "enter", FormModal),
        ("OPENROUTER_API_KEY", "u", FormModal),
        ("PROMPT_PROVIDER", "enter", PickModal),
        ("PROMPT_PRO_PROFILE", "enter", PickModal),
        ("OLLAMA_MODEL", "enter", EditModal),
    ],
    ids=["set-key", "remove-key", "pick", "pick-profile", "edit"],
)
def test_cancelling_a_settings_dialog_changes_nothing(
    saved: Path, espanso: FakeRunner, name: str, key: str, modal: type[Widget]
) -> None:
    config_store.save_secret("OPENROUTER_API_KEY", KEY)
    before = sorted((p, p.read_bytes()) for p in saved.rglob("*") if p.is_file())

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, name, key)
        assert isinstance(app.screen, modal)
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, modal)
        assert app.session == []

    drive(scenario)
    assert sorted((p, p.read_bytes()) for p in saved.rglob("*") if p.is_file()) == before


def test_settings_profile_choices_include_your_own(saved: Path, espanso: FakeRunner) -> None:
    _user_profile("mine")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await on_row(app, pilot, "PROMPT_PRO_PROFILE", "enter")
        screen = app.screen
        assert isinstance(screen, PickModal)
        values = [v for _, v in screen.choices]
        assert values[0] == ""
        assert screen.choices[0][0] == "as PROMPT_PROFILE  (current, default)"
        assert {"default", "general", "mine"} <= set(values)
        await pilot.press("escape")
        await settle(pilot)
        await pick(app, pilot, "PROMPT_PRO_PROFILE", "mine")
        assert _state(app).settings.pro_profile == "mine"

    drive(scenario)


# A change that makes a flagged persona go out says so, as `config set` does: the finding
# names only, and the setting stays saved.
def test_settings_change_warns_about_a_flagged_persona(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "jane.doe" + "@" + "example.com"
    monkeypatch.setenv("PROMPT_PERSONA", f"I am an analyst, mail {address}.")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pick(app, pilot, "PROMPT_PROFILE", "general")
        assert "PROMPT_PERSONA" not in pane(app, "settings").last_message
        await pick(app, pilot, "PROMPT_PROFILE", "default")
        message = pane(app, "settings").last_message
        assert message.startswith("PROMPT_PROFILE saved in ")
        assert "PROMPT_PERSONA matches the data-protection patterns: email" in message
        assert address not in message

    drive(scenario)
    assert 'PROMPT_PROFILE = "default"' in (saved / "config.toml").read_text("utf-8")


def test_checks_test_call_uses_the_stub_service(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []

    def fake_run(provider: str) -> smoke.SmokeResult:
        called.append(provider)
        return smoke.SmokeResult(True, smoke.REPLY, 1, "improve reached the stub")

    monkeypatch.setattr(smoke, "run", fake_run)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("6")
        await press(app, pilot, "#smoke")
        assert "No provider is called" in _dialog(app).preview
        await press(app, pilot, "#cancel")
        assert called == []
        await press(app, pilot, "#smoke")
        await fill(app, pilot, provider="ollama")
        assert pane(app, "diagnostics").last_message == "ok: improve reached the stub"

    drive(scenario)
    assert called == ["ollama"]


def test_checks_test_call_adds_no_history_row(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#116: the real Test call (a child improve against the stub) is not usage."""
    monkeypatch.setenv("PROMPT_HISTORY", "true")
    store = HistoryStore(history.history_path())

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("6")
        await press(app, pilot, "#smoke")
        await fill(app, pilot, provider="ollama")
        for _ in range(100):  # the child process runs in a worker
            if pane(app, "diagnostics").last_message.startswith("ok: "):
                break
            await asyncio.sleep(0.1)
            await settle(pilot)
        assert pane(app, "diagnostics").last_message.startswith("ok: improve reached the stub")

    drive(scenario)
    assert store.health().operations in (None, 0)


# #396: the one Test call is on the Checks tab, and it needs only the settings: on the quick
# State (the full checks still running) a real click opens its dialog, focused on the
# provider, and Run reports the stub's answer under the button. The Settings tab has no
# button row left.
def test_checks_test_call_works_on_the_quick_state(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def fake_run(provider: str) -> smoke.SmokeResult:
        called.append(provider)
        return smoke.SmokeResult(True, smoke.REPLY, 1, "improve reached the stub")

    monkeypatch.setattr(smoke, "run", fake_run)
    blocked = BlockedLoad()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: app.state is not None)
        await pilot.press("2")
        await until(pilot, lambda: settings_tab(app).ready)
        assert not settings_tab(app).query("#smoke")
        assert not settings_tab(app).query(".buttons")
        await pilot.press("6")
        checks = app.main.query_one("#diagnostics-pane", base.Pane)
        await until(pilot, lambda: checks.ready)
        assert not _state(app).full
        assert not checks.query_one("#smoke", Button).disabled
        await pilot.click("#smoke")
        await until(pilot, lambda: isinstance(app.screen, FormModal))
        assert _dialog(app).dialog_title == "Test call"
        # The dialog focuses its field once it is mounted: on a slow runner the screen is
        # there a moment before the focus is (seen on windows-latest, 3.12).
        await until(pilot, lambda: _focused_id(app) == "provider")
        await pilot.click("#submit")
        await until(pilot, lambda: checks.last_message.startswith("ok: "), lambda: called)
        assert checks.last_message == "ok: improve reached the stub"
        assert not _state(app).full
        blocked.release.set()
        await settle(pilot)

    drive(scenario, settled=False, loader=blocked)
    assert called == [resolved_settings().provider]


# --- Profiles -----------------------------------------------------------------------------


def _user_profile(name: str = "mine") -> Path:
    from promptmend.prompt_builder import user_profiles_dir

    folder = user_profiles_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text("Rewrite the draft.", encoding="utf-8")
    return path


def test_profiles_set_default_and_edit_in_editor(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _user_profile()
    edited = []

    def editor(target: Path) -> int:
        edited.append(target)
        return 0

    monkeypatch.setattr(profiles_pane, "run_editor", editor)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        from contextlib import nullcontext

        from textual.widgets import DataTable

        # the headless driver cannot hand the terminal over
        monkeypatch.setattr(app, "suspend", nullcontext)
        await pilot.press("3")
        await press(app, pilot, "#edit-profile")
        assert "Select one of your profiles" in pane(app, "profiles").last_message
        table = pane(app, "profiles").query_one("#profiles", DataTable)
        table.move_cursor(row=table.get_row_index("user:mine"))
        await press(app, pilot, "#edit-profile")
        assert pane(app, "profiles").last_message == "mine.md: the editor exited with 0"
        await press(app, pilot, "#set-profile")
        await fill(app, pilot, profile="mine")
        assert _state(app).settings.profile == "mine"

    drive(scenario)
    assert edited == [path]
    assert 'PROMPT_PROFILE = "mine"' in (saved / "config.toml").read_text("utf-8")


def test_profiles_editor_unsupported_terminal(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user_profile()
    monkeypatch.setattr(profiles_pane, "run_editor", lambda target: pytest.fail("editor ran"))

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        from textual.widgets import DataTable

        await open_tab(app, pilot, "profiles")
        table = pane(app, "profiles").query_one("#profiles", DataTable)
        table.move_cursor(row=table.get_row_index("user:mine"))
        await pilot.press("3")
        await press(app, pilot, "#edit-profile")
        assert "cannot hand over" in pane(app, "profiles").last_message

    drive(scenario)


def test_editor_command() -> None:
    def nowhere(name: str) -> None:
        return None

    assert profiles_pane.editor_command({}, windows=False, which=nowhere) == ["vi"]
    assert profiles_pane.editor_command({}, windows=True, which=nowhere) == ["notepad"]
    both = {"VISUAL": "vim", "EDITOR": "code --wait"}
    assert profiles_pane.editor_command(both, windows=False, which=nowhere) == ["vim"]


def test_profiles_editor_that_cannot_start(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user_profile()

    def missing(target: Path) -> int:
        raise FileNotFoundError("no-such-editor")

    monkeypatch.setattr(profiles_pane, "run_editor", missing)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        from contextlib import nullcontext

        from textual.widgets import DataTable

        monkeypatch.setattr(app, "suspend", nullcontext)
        await pilot.press("3")
        table = pane(app, "profiles").query_one("#profiles", DataTable)
        table.move_cursor(row=table.get_row_index("user:mine"))
        await press(app, pilot, "#edit-profile")
        assert "could not start the editor" in pane(app, "profiles").last_message

    drive(scenario)


def test_run_editor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    seen: list[list[str]] = []

    class Done:
        returncode = 0

    def run(argv: list[str], check: bool) -> Done:
        seen.append(argv)
        return Done()

    monkeypatch.setenv("VISUAL", "myeditor -w")
    monkeypatch.setattr(subprocess, "run", run)
    assert profiles_pane.run_editor(tmp_path / "mine.md") == 0
    assert seen == [["myeditor", "-w", str(tmp_path / "mine.md")]]


# --- Triggers -----------------------------------------------------------------------------


def test_triggers_say_when_espanso_cannot_name_its_folder(espanso: FakeRunner) -> None:
    """#115: deploy and detach then use the default folder; the Triggers tab says so."""
    espanso.answers["espanso path config"] = deploy.CommandFailure(
        found=True, returncode=101, error="unable to load config"
    )

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        target = str(pane(app, "triggers").query_one("#deploy-target").render())
        assert "`espanso path config` failed (exit 101): unable to load config" in target
        assert "using the default folder" in target

    drive(scenario)


# An older deployed -ip- still names OpenRouter: the Triggers tab shows that provider until a
# redeploy, and Home warns, when PROMPT_PRO_PROVIDER names another (#278).
def test_triggers_show_an_older_deployed_ip(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptmend import assets

    monkeypatch.setenv("PROMPT_PRO_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENROUTER_API_KEY", "-".join(("test", "key")))
    old = '"--trigger-id", "ip", "--provider", "openrouter", "--tier"'
    text = assets.read_match("prompts-llm.yml").replace('"--trigger-id", "ip", "--tier"', old)
    (espanso.root / "match" / "prompts-llm.yml").write_text(text, "utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        rows = table_rows(app, "triggers", "#triggers")
        assert rows["-ip-"][1:3] == ["openrouter until redeployed", "pro"]
        assert rows["-if-"][1] == "openrouter"
        pro = next(line for line in _home_text(app).splitlines() if "Pro (-ip-)" in line)
        assert "openrouter · openai/gpt-6-luna" in pro
        assert "redeploy" in pro

    drive(scenario)


# Without a plan (here: the launcher cannot be resolved) the State still reads the deployed
# triggers from the folder doctor used, so Home and doctor agree (#278).
def test_gather_reads_the_deployed_triggers_without_a_plan(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptmend import assets
    from promptmend.tui import state as state_module

    old = '"--trigger-id", "ip", "--provider", "openrouter", "--tier"'
    text = assets.read_match("prompts-llm.yml").replace('"--trigger-id", "ip", "--tier"', old)
    (espanso.root / "match" / "prompts-llm.yml").write_text(text, "utf-8")

    def no_plan(*_: object) -> deploy.Plan:
        raise deploy.DeployError("no launcher")

    monkeypatch.setattr(state_module, "_plan", no_plan)
    monkeypatch.setenv("PROMPT_PRO_PROVIDER", "anthropic")
    gathered = state_module.gather()
    assert gathered.plan is None
    assert gathered.deployed is not None
    assert {t.trigger: t.provider for t in gathered.deployed}["-ip-"] == "openrouter"
    check = next(c for c in gathered.report.checks if c.id == "pro_provider")
    assert check.status == "warn"


def test_triggers_show_fixed_providers_and_deploy(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        rows = table_rows(app, "triggers", "#triggers")
        # The value a call runs with, the setting that supplied it under it (#396); a value
        # the command line names stands alone.
        assert rows["-i-"][1:5] == ["openrouter", "standard", "default\nPROMPT_PROFILE", "missing"]
        assert rows["-ip-"][1:4] == [
            "openrouter\nPROMPT_PRO_PROVIDER",
            "pro",
            "default\nPROMPT_PROFILE",
        ]
        assert rows["-il-"][1:4] == ["ollama", "standard", "general"]
        assert rows["-ic-"][4] == "commented out"
        assert "whatever PROMPT_PROVIDER says" in str(
            pane(app, "triggers").query_one(".note").render()
        )
        await press(app, pilot, "#deploy")
        assert isinstance(app.screen, FormModal)
        await press(app, pilot, "#cancel")
        assert not list((espanso.root / "match").iterdir())
        await press(app, pilot, "#deploy")
        await press(app, pilot, "#submit")
        assert pane(app, "triggers").last_message.endswith("The match files are up to date.")
        rows = table_rows(app, "triggers", "#triggers")
        # Tests never reach a local model server, so the local triggers need one (#220).
        assert (rows["-il-"][4], rows["-ilm-"][4]) == ("needs Ollama", "needs LM Studio")
        states = {row[4] for row in rows.values()}
        assert states == {"in sync", "commented out", "needs Ollama", "needs LM Studio"}
        await press(app, pilot, "#deploy")
        assert pane(app, "triggers").last_message == "Nothing to do: every match file is in sync."
        await press(app, pilot, "#show-diff")
        assert isinstance(app.screen, TextModal)
        assert _dialog(app).preview == "Every match file is in sync."
        await press(app, pilot, "#close")

    drive(scenario)
    assert sorted(p.name for p in (espanso.root / "match").iterdir()) == sorted(
        s.name for s in _plan().steps
    )
    assert ["espanso", "restart"] in espanso.calls


def test_triggers_deploy_keeps_an_edited_file_by_default(espanso: FakeRunner) -> None:
    target = espanso.root / "match" / "prompts-core.yml"
    target.write_text("matches: []  # mine\n", encoding="utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        await press(app, pilot, "#show-diff")
        assert "# mine" in _dialog(app).preview
        await pilot.press("escape")
        await press(app, pilot, "#deploy")
        assert app.screen.query_one("#choice-0", Select).value == deploy.KEEP
        await press(app, pilot, "#submit")
        assert "WARNING: kept as you have them and NOT updated: prompts-core.yml" in (
            pane(app, "triggers").last_message
        )

    drive(scenario)
    assert target.read_text("utf-8") == "matches: []  # mine\n"


def test_triggers_deploy_side_by_side(espanso: FakeRunner) -> None:
    target = espanso.root / "match" / "prompts-core.yml"
    target.write_text("matches: []  # mine\n", encoding="utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        await press(app, pilot, "#deploy")
        await fill(app, pilot, choice_0=deploy.SIDE)

    drive(scenario)
    assert target.read_text("utf-8") == "matches: []  # mine\n"
    assert (target.parent / f"prompts-core.yml{deploy.SIDE_SUFFIX}").is_file()


def test_triggers_detach_after_confirming(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        await press(app, pilot, "#detach")
        assert "no deployed match files" in pane(app, "triggers").last_message
        await press(app, pilot, "#deploy")
        await press(app, pilot, "#submit")
        await press(app, pilot, "#detach")
        assert "prompts-llm.yml" in _dialog(app).preview
        await press(app, pilot, "#cancel")
        assert (espanso.root / "match" / "prompts-llm.yml").is_file()
        await press(app, pilot, "#detach")
        await press(app, pilot, "#submit")
        assert "removed" in pane(app, "triggers").last_message

    drive(scenario)
    left = sorted(p.name for p in (espanso.root / "match").iterdir())
    assert left == ["prompts-core.yml"]  # --keep-static: the static snippets stay


def test_triggers_refresh_before_espanso_restarts(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#215: the table shows the written files without `r`, even while the restart after a
    deploy or detach still runs (it can take seconds, on Windows it hung)."""
    import threading

    restarted = threading.Event()
    released = threading.Event()

    def runner(argv: list[str]) -> str | deploy.CommandFailure | None:
        if argv == ["espanso", "restart"]:
            restarted.set()
            released.wait(timeout=10)
        return espanso(argv)

    monkeypatch.setattr(deploy, "run_command", runner)

    async def states(app: ManageApp, pilot: Pilot[int], want: set[str]) -> None:
        """Wait for the rows' states without settle(): the action worker is still blocked."""
        for _ in range(200):
            await pilot.pause(0.02)
            if {row[4] for row in table_rows(app, "triggers", "#triggers").values()} == want:
                return
        pytest.fail(f"the rows never showed {want}")

    async def submit_and_check(app: ManageApp, pilot: Pilot[int], want: set[str]) -> None:
        restarted.clear()
        released.clear()
        app.screen.query_one("#submit", Button).press()
        await states(app, pilot, want)
        assert restarted.is_set()
        assert not released.is_set()  # still restarting
        assert pane(app, "triggers").busy
        released.set()
        await settle(pilot)
        assert not pane(app, "triggers").busy

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        await press(app, pilot, "#deploy")
        deployed = {"in sync", "commented out", "needs Ollama", "needs LM Studio"}
        await submit_and_check(app, pilot, deployed)
        assert pane(app, "triggers").last_message.endswith("The match files are up to date.")
        await press(app, pilot, "#detach")
        # keep-static: the CLI's match files go, the static snippets stay.
        await submit_and_check(app, pilot, {"missing", "in sync", "commented out"})
        assert "removed" in pane(app, "triggers").last_message

    drive(scenario)


def test_triggers_report_failures(espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    del espanso.answers["espanso restart"]  # neither restart nor start succeeds

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        await press(app, pilot, "#deploy")
        await press(app, pilot, "#submit")
        message = pane(app, "triggers").last_message
        assert "Could not restart Espanso; run `espanso restart` yourself." in message
        await press(app, pilot, "#detach")
        await press(app, pilot, "#submit")
        assert "Could not restart Espanso" in pane(app, "triggers").last_message

        def fail(*args: Any, **kwargs: Any) -> None:
            raise deploy.DeployError("the disk is full")

        monkeypatch.setattr(deploy, "apply", fail)
        monkeypatch.setattr(deploy, "detach", fail)
        # Detach removed prompts-llm.yml, so there is something to deploy again.
        await press(app, pilot, "#deploy")
        await press(app, pilot, "#submit")
        assert pane(app, "triggers").last_message == "error: the disk is full"
        await press(app, pilot, "#detach")
        await press(app, pilot, "#submit")
        assert pane(app, "triggers").last_message == "error: the disk is full"
        monkeypatch.setattr(deploy.Manifest, "load", classmethod(lambda cls, path=None: fail()))
        await press(app, pilot, "#detach")
        assert pane(app, "triggers").last_message == "error: the disk is full"

    drive(scenario)


def test_triggers_without_a_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy, "run_command", lambda argv: None)

    def no_launcher(**_: Any) -> None:
        raise deploy.DeployError("Could not find a stable promptmend launcher")

    monkeypatch.setattr(deploy, "resolve_launcher", no_launcher)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        text = str(pane(app, "triggers").query_one("#deploy-target").render())
        assert text.startswith("Cannot compare with Espanso: Could not find"), (text, app.state)
        await press(app, pilot, "#show-diff")
        assert "No plan" in pane(app, "triggers").last_message
        await press(app, pilot, "#deploy")
        assert pane(app, "triggers").last_message.startswith("error: Could not find")

    drive(scenario)


# --- History ------------------------------------------------------------------------------


def test_history_stats_export_prune_reset(
    espanso: FakeRunner, tmp_path: Path, record_ops: Callable[..., None]
) -> None:
    record_ops()
    out = tmp_path / "out.json"

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        from textual.widgets import DataTable

        await pilot.press("5")
        history_pane = pane(app, "history")
        assert "not provider billing" in str(history_pane.query_one(".note").render())
        table = history_pane.query_one("#stats", DataTable)
        row = [c.plain for c in table.get_row_at(0)]
        assert row[:3] == ["-i-", "2", "2"]
        assert row[-1] == "[reported] 0.0002 credits"
        history_pane.query_one("#group-by", Select).value = "provider"
        await settle(pilot)
        assert _state(app).group_by == "provider"
        assert table.get_row_at(0)[0].plain == "openrouter"

        await press(app, pilot, "#export")
        await fill(app, pilot, path=str(out))
        assert history_pane.last_message == f"Exported 2 call(s) to {out}"
        await press(app, pilot, "#export")
        await fill(app, pilot, path=str(out))
        assert history_pane.last_message.startswith("error:")  # never replaced

        await press(app, pilot, "#prune")
        await fill(app, pilot, days="soon")
        assert "number of days" in history_pane.last_message
        await press(app, pilot, "#prune")
        await fill(app, pilot, days="30")
        await press(app, pilot, "#confirm")
        assert history_pane.last_message == "Deleted 0 call(s) older than 30 day(s)."

        await press(app, pilot, "#reset")
        await press(app, pilot, "#cancel")
        assert _state(app).stats
        await press(app, pilot, "#reset")
        await press(app, pilot, "#confirm")
        assert history_pane.last_message == "The usage history is empty."
        assert not _state(app).stats
        assert "No usage recorded yet." in str(history_pane.query_one("#stats-note").render())

    drive(scenario)
    assert len(json.loads(out.read_text("utf-8"))["operations"]) == 2


PLUS_TWO = "UTC+2"


def _plus_two(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta, timezone

    from promptmend.commands import usage

    monkeypatch.setattr(usage, "local_zone", lambda: timezone(timedelta(hours=2)))


def _seed_runs(seed_history: SeedHistory, count: int) -> None:
    """``count`` -i- runs to OpenRouter's default model a second apart; the newest failed."""
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 11, 28, 9, 0, tzinfo=UTC)
    for second in range(count):
        failed = second == count - 1
        op = {
            "id": history.new_operation_id(),
            "occurred_at_utc": start + timedelta(seconds=second),
            "origin": "espanso_managed",
            "trigger_id": "-i-",
            "kind": "improve",
            "outcome": "error_marker" if failed else "ok",
            "latency_ms": 1000.0 + second,
        }
        attempt = {
            "provider": "openrouter",
            "requested_model": "google/gemini-3.5-flash-lite",
            "endpoint": "remote",
            "status": 401 if failed else 200,
            "error_kind": "non_2xx" if failed else None,
            "latency_ms": 900.0,
        }
        seed_history(op, [attempt])


def _runs(app: ManageApp) -> list[tuple[str, ...]]:
    from textual.widgets import DataTable

    table = pane(app, "history").query_one("#runs", DataTable)
    return [tuple(c.plain for c in table.get_row_at(i)) for i in range(table.row_count)]


async def _tab_to(app: ManageApp, pilot: Pilot[int], button_id: str) -> Button:
    """Press Tab until ``button_id`` has the focus; it is then on screen."""
    for _ in range(40):
        if app.focused is not None and app.focused.id == button_id:
            break
        await pilot.press("tab")
        await pilot.pause()
    button = app.screen.query_one(f"#{button_id}", Button)
    assert app.focused is button
    assert button.region.height > 0
    assert app.screen.region.contains_region(button.region)
    return button


@pytest.mark.parametrize("size", [(80, 24), (70, 24)])
def test_history_last_runs_table(
    espanso: FakeRunner,
    seed_history: SeedHistory,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
) -> None:
    """#400: under the stats, the newest LAST_RUNS runs as `history last` lists them (local
    time, here without the year); the buttons below stay reachable by Tab at 80x24 and
    70x24, the pane scrolling to the focused one; Reset empties the table."""
    from textual.widgets import DataTable

    from promptmend.commands import usage
    from promptmend.tui.state import LAST_RUNS

    _plus_two(monkeypatch)
    _seed_runs(seed_history, LAST_RUNS + 2)
    stored = history.HistoryStore(history.history_path()).last(LAST_RUNS)
    expected = [usage.last_cells(run, compact=True) for run in stored]

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("5")
        await settle(pilot)
        history_tab = pane(app, "history")
        table = history_tab.query_one("#runs", DataTable)
        rows = _runs(app)
        assert rows == expected
        assert len(rows) == LAST_RUNS
        assert rows[0] == (
            "11-28 11:00:06",
            "-i-",
            "error_marker",
            "non_2xx",
            "1006",
            "openrouter",
            "google/gemini-3.5-flash-lite",
        )
        assert [str(c.label) for c in table.columns.values()] == list(usage.LAST_COLUMNS)
        # Every column but the model is whole at 80 and at 70 columns; only the model is cut
        # (docs/interface.md says so: this notices when it fits and the sentence is stale).
        widths = [column.get_render_width(table) for column in table.columns.values()]
        assert table.virtual_size.width == sum(widths)
        assert widths[0] == len("11-28 11:00:06") + 2  # the short time, with its padding
        assert sum(widths[:-1]) <= table.size.width
        assert sum(widths) > table.size.width
        # Tab to the last button and press it: the pane scrolls it on screen.
        await _tab_to(app, pilot, "reset")
        await pilot.press("enter")
        await settle(pilot)
        await pilot.click("#confirm")
        await settle(pilot)
        assert history_tab.last_message == "The usage history is empty."
        assert _runs(app) == []

    drive(scenario, size=size)


def test_history_last_runs_wait_for_the_full_state(
    espanso: FakeRunner, seed_history: SeedHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quick State has no runs: the table is empty while the stats note says it reads."""
    _plus_two(monkeypatch)
    _seed_runs(seed_history, 2)
    blocked = BlockedLoad()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: app.state is not None)
        await app.main.build("history")
        await pilot.pause()
        assert _runs(app) == []
        assert "Reading the usage history" in _rendered(pane(app, "history"), "#stats-note")
        blocked.release.set()
        await settle(pilot)
        assert [row[0] for row in _runs(app)] == ["11-28 11:00:01", "11-28 11:00:00"]

    drive(scenario, settled=False, loader=blocked)


def test_history_last_runs_on_a_read_error(
    espanso: FakeRunner, seed_history: SeedHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_runs(seed_history, 2)

    def locked(self: history.HistoryStore, n: int = 10, *, replay: bool = True) -> None:
        raise history.HistoryError("history database is locked")

    monkeypatch.setattr(history.HistoryStore, "last", locked)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("5")
        await settle(pilot)
        assert _runs(app) == []
        note = _rendered(pane(app, "history"), "#stats-note")
        assert "error: last runs: history database is locked" in note

    drive(scenario)


def _odd_rows() -> None:
    """Rows no record() writes (another tool edited the database), newer than the rest."""
    import sqlite3

    rows = [
        (b"\x00\xff", 1.0),
        ("9999-12-31T23:59:59.999999Z", 1.0),
        ("2027-01-01 00:00:00", 1.0),
        ("2026-12-01T00:00:00.000000Z", "soon"),
    ]
    with sqlite3.connect(history.history_path()) as conn:
        for when, latency in rows:
            conn.execute(
                "INSERT INTO operations VALUES (?, ?, 'direct', NULL, 'improve', NULL, 'ok', ?,"
                " '1.0')",
                (history.new_operation_id(), when, latency),
            )
    conn.close()


def test_odd_stored_rows_never_break_the_history_tab(
    espanso: FakeRunner, seed_history: SeedHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #400: a row edited by another tool shows as a stand-in; the other runs, the
    gated buttons and the tabs painted after History are all there."""
    _plus_two(monkeypatch)
    _seed_runs(seed_history, 1)
    _odd_rows()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("5")
        await settle(pilot)
        rows = _runs(app)
        assert [row[0] for row in rows] == [
            "?",
            "'9999-12-31T23:59:59.999999Z'",
            "'2027-01-01 00:00:00'",
            "12-01 02:00:00",
            "11-28 11:00:00",
        ]
        assert rows[3][1] == "(none)"
        assert rows[3][4] == "'soon'"
        assert rows[4][-1] == "google/gemini-3.5-flash-lite"
        reset = await _tab_to(app, pilot, "reset")
        assert not reset.disabled
        await pilot.press("6")
        await settle(pilot)
        assert "Checking every part" not in _rendered(pane(app, "diagnostics"), "#all-checks")

    drive(scenario)


@pytest.mark.parametrize(
    ("when", "trigger"),
    [(b"2026-11-28T09:00:00.000000Z", "-i-"), ("2026-11-28T09:00:00.000000Z", b"-i-")],
    ids=["blob-time", "blob-trigger"],
)
def test_a_single_blob_row_never_breaks_the_stats_table(
    espanso: FakeRunner, when: object, trigger: object
) -> None:
    """Review of #400: on a history of one hand-edited row, stats() returns a key or a last
    use that is not text; the stats table says it cannot be shown, in one line, and the tab
    is still painted, gated and reachable."""
    import sqlite3

    store = history.HistoryStore(history.history_path())
    assert store.record(
        {"id": history.new_operation_id(), "origin": "direct", "kind": "improve", "outcome": "ok"}
    )
    with sqlite3.connect(store.path) as conn:
        conn.execute("DELETE FROM operations")
        conn.execute(
            "INSERT INTO operations VALUES (?, ?, 'direct', ?, 'improve', NULL, 'ok', 1.0, '1.0')",
            (history.new_operation_id(), when, trigger),
        )
    conn.close()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        from textual.widgets import DataTable

        await pilot.press("5")
        await settle(pilot)
        history_tab = pane(app, "history")
        assert history_tab.query_one("#stats", DataTable).row_count == 0
        note = _rendered(history_tab, "#stats-note")
        assert "error: the stats cannot be shown (TypeError)" in note
        assert len(_runs(app)) == 1
        reset = await _tab_to(app, pilot, "reset")
        assert not reset.disabled

    drive(scenario)


def test_a_failure_drawing_the_runs_is_one_line(
    espanso: FakeRunner, seed_history: SeedHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever goes wrong filling the runs table, the tab is still painted and gated and
    says so in one line; the Checks tab, painted after it, is too."""
    from promptmend.commands import usage

    _seed_runs(seed_history, 1)

    def broken(run: history.LastRun, *, compact: bool = False) -> tuple[str, ...]:
        raise RuntimeError("kaput")

    monkeypatch.setattr(usage, "last_cells", broken)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("5")
        await settle(pilot)
        history_tab = pane(app, "history")
        assert _runs(app) == []
        note = _rendered(history_tab, "#stats-note")
        assert "error: the last runs cannot be shown (RuntimeError)" in note
        assert "kaput" not in note
        assert not history_tab.query_one("#reset", Button).disabled
        await pilot.press("6")
        await settle(pilot)
        assert "Checking every part" not in _rendered(pane(app, "diagnostics"), "#all-checks")

    drive(scenario)


def test_cost_badges() -> None:
    row = history.StatsRow(
        "m",
        1,
        2,
        "2026-10-04T09:12:31.000000Z",
        None,
        None,
        {},
        estimated={"USD": Decimal("0.5")},
        unknown_cost_attempts=1,
    )
    assert history_pane._cost(row) == "[estimated] 0.5 USD; [unknown] 1"
    row = history.StatsRow("m", 1, 1, "2026-10-04T09:12:31.000000Z", None, None, {})
    assert history_pane._cost(row) == "none recorded"


def test_history_export_needs_a_path(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("5")
        await press(app, pilot, "#export")
        await fill(app, pilot, path=" ")
        assert pane(app, "history").last_message == "error: enter the file to write"

    drive(scenario)


def test_history_unreadable_store(espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: HistoryStore, group_by: str = "trigger") -> None:
        raise history.HistoryError("the database is damaged")

    monkeypatch.setattr(HistoryStore, "stats", broken)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "history")
        note = str(pane(app, "history").query_one("#stats-note").render())
        assert note == "error: the database is damaged"

    drive(scenario)


# --- Checks (diagnostics_pane) ------------------------------------------------------------


def test_diagnostics_store_and_layers(espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("6")
        diagnostics = pane(app, "diagnostics")
        assert KEY not in app.export_screenshot()
        assert "Read from, lowest precedence first:" in str(
            diagnostics.query_one("#layers").render()
        )
        store = str(diagnostics.query_one("#store").render())
        assert "Lost history writes: 0" in store
        assert "SQLite" in store
        assert ", journal none yet;" in store

    drive(scenario)


def test_diagnostics_on_a_broken_config(espanso: FakeRunner, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PROMPT_LOCAL_ONLY=maybe\n", encoding="utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("6")
        diagnostics = pane(app, "diagnostics")
        assert "PROMPT_LOCAL_ONLY must be true or false" in str(
            diagnostics.query_one("#findings").render()
        )

    drive(scenario)


def test_files_under_home_show_as_tilde(
    monkeypatch: pytest.MonkeyPatch, espanso: FakeRunner
) -> None:
    # #174: display only, so the From column keeps the file name on a narrow terminal.
    monkeypatch.delenv("PROMPTMEND_ENV")
    # The config dir is $XDG_CONFIG_HOME on POSIX and %APPDATA% on Windows.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    monkeypatch.setenv("APPDATA", str(Path.home() / ".config"))
    config_store.save_secret("OPENROUTER_API_KEY", KEY)
    folder = config_store.config_dir()
    (folder / "config.toml").write_text('PROMPT_PROFILE = "general"\n', encoding="utf-8")
    monkeypatch.setenv("PROMPT_PROFILE", "default")
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        # The list shows the file name; its help line the place, ~/… under the home folder.
        await on_row(app, pilot, "PROMPT_PROFILE")
        row = settings_rows(app)["PROMPT_PROFILE"]
        assert (row.source, row.where) == ("environment", "environment")
        await on_row(app, pilot, "PROMPT_HISTORY")
        await on_row(app, pilot, "OPENROUTER_API_KEY")
        help_line = str(settings_tab(app).query_one("#settings-help").render())
        assert "From environment · secrets set OPENROUTER_API_KEY" in help_line
        monkeypatch.delenv("PROMPT_PROFILE")
        monkeypatch.delenv("OPENROUTER_API_KEY")
        app.reload()
        await settle(pilot)
        assert settings_rows(app)["PROMPT_PROFILE"].source == "config.toml"
        assert settings_rows(app)["PROMPT_PROFILE"].where == "~/.config/promptmend/config.toml"
        assert settings_rows(app)["OPENROUTER_API_KEY"].source == "secrets.toml"

    drive(scenario)


def test_short_label_only_shortens_files_under_home(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend.commands import common

    home = Path.home()
    assert common.short_label(f"file:{home / 'a' / 'b.toml'}") == "~/a/b.toml"
    assert common.short_label(f"file:{home}") == "~"
    elsewhere = str(Path("/elsewhere/config.toml").resolve())
    assert common.short_label(f"file:{elsewhere}") == elsewhere
    assert common.short_label(config.ENV_SOURCE) == "environment"
    assert common.short_label(config.DEFAULT_SOURCE) == "default"


# --- services the interface added ---------------------------------------------------------


def test_routes_classify_each_provider() -> None:
    from promptmend.config import Settings
    from promptmend.factory import routes

    cfg = Settings(ollama_model="gpt-oss:120b-cloud", local_only=True)
    found = {r.name: r for r in routes(cfg)}
    assert (found["ollama"].remote, found["ollama"].refused) == (True, True)
    assert (found["lmstudio"].remote, found["lmstudio"].refused) == (False, False)
    assert found["openrouter"].key == "OPENROUTER_API_KEY"
    assert found["lmstudio"].key is None


def test_triggers_match_the_match_files() -> None:
    import yaml

    from promptmend import assets

    found = {t.trigger: t for t in assets.triggers()}
    match_dir = Path(__file__).parents[1] / "espanso" / "match"
    shipped = {
        m["trigger"]
        for path in match_dir.glob("*.yml")
        for m in yaml.safe_load(path.read_text("utf-8"))["matches"]
    }
    assert {t for t, item in found.items() if item.active} == shipped
    assert (found["-ip-"].provider, found["-ip-"].tier) == (None, "pro")  # PROMPT_PRO_PROVIDER
    assert (found["-if-"].provider, found["-if-"].tier) == ("openrouter", "pro")
    assert (found["-ilm-"].provider, found["-ilm-"].profile) == ("lmstudio", "general")
    assert (found["-ic-"].active, found["-ic-"].provider) == (False, "anthropic")
    assert (found["-io-"].active, found["-io-"].provider) == (False, "openai")
    assert found["-io-"].profile is None  # PROMPT_PROFILE, as -i- and -ip-
    assert (found["-p-"].command, found["-risk-"].command) == ("persona", None)


# --- launching: the bare command and `ui` (D-UI-1) -----------------------------------------


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Make stdin and stdout read as terminals, each switchable, and record each launch."""
    from promptmend.commands import common

    state: dict[str, Any] = {"stdin": True, "stdout": True, "launched": 0, "code": None}
    monkeypatch.setattr(common, "stdin_is_tty", lambda: state["stdin"])
    monkeypatch.setattr(common, "stdout_is_tty", lambda: state["stdout"])

    def run(self: ManageApp, *args: Any, **kwargs: Any) -> None:
        state["launched"] += 1
        self._return_code = state["code"]

    monkeypatch.setattr(ManageApp, "run", run)
    return state


def _invoke(*args: str) -> Result:
    from typer.testing import CliRunner

    from promptmend.cli import app

    return CliRunner().invoke(app, list(args))


EOL = os.linesep.encode()
GOLDEN = Path(__file__).parent / "golden" / "no-args-help.txt"


def _words(text: str) -> str:
    """Help text without colour, box drawing, the program name or line wrapping, which
    differ by terminal width, CI and OS."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"Usage: .*? \[OPTIONS\]", "Usage: promptmend [OPTIONS]", text)
    # Every box-drawing character: Windows draws square corners where macOS draws round ones.
    return " ".join(re.sub(r"[\u2500-\u257f|+]", " ", text).split())


def test_words_ignore_the_border_style() -> None:
    rounded = "╭─ Options ─╮\n│ --help │\n╰───────────╯"
    square = "┌─ Options ─┐\n│ --help │\n└───────────┘"
    assert _words(rounded) == _words(square) == "Options --help"


def test_bare_command_without_a_terminal_prints_the_help_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Help panels wrap at the terminal width, which differs on CI runners (Windows too).
    monkeypatch.setenv("COLUMNS", "100")
    bare, helped = _invoke(), _invoke("--help")
    # As no_args_is_help always did: --help's bytes less its last newline, on stdout, exit 2.
    assert (bare.exit_code, helped.exit_code) == (2, 0)
    assert bare.stdout_bytes + EOL == helped.stdout_bytes
    assert bare.stderr_bytes == helped.stderr_bytes == b""
    # The same help as before #93 (tests/golden, captured from it), plus the `ui` row.
    ui_row = "ui Open the full-screen interface to set up and manage (a terminal)."
    before = _words(GOLDEN.read_text("utf-8"))
    assert _words(bare.stdout) == f"{before} {ui_row}"


@pytest.mark.parametrize(("stdin", "stdout"), [(True, False), (False, True)])
def test_bare_command_needs_both_streams_on_a_terminal(
    terminal: dict[str, Any], stdin: bool, stdout: bool
) -> None:
    terminal.update(stdin=stdin, stdout=stdout)
    result = _invoke()
    assert result.exit_code == 2
    assert "Usage:" in result.stdout
    assert terminal["launched"] == 0


@pytest.mark.parametrize("args", [(), ("ui",)])
@pytest.mark.parametrize("code", [None, 0, 1])
def test_terminal_opens_the_interface_and_exits_with_its_code(
    terminal: dict[str, Any], args: tuple[str, ...], code: int | None
) -> None:
    terminal["code"] = code
    result = _invoke(*args)
    assert terminal["launched"] == 1
    assert result.exit_code == (code or 0)
    assert result.stdout == ""


def test_ui_without_a_terminal_exits_3(terminal: dict[str, Any]) -> None:
    from promptmend.commands.common import NEEDS_TERMINAL

    terminal["stdout"] = False
    result = _invoke("ui")
    assert result.exit_code == NEEDS_TERMINAL == 3
    assert result.stderr.startswith("error: the interface needs a terminal")
    assert terminal["launched"] == 0


# The real process with pipes, not a terminal: the bare command prints the same bytes as
# --help and exits 2, as no_args_is_help always did.
def test_bare_command_in_a_real_process(tmp_path: Path) -> None:
    import subprocess
    import sys

    env = {**os.environ, "COLUMNS": "100", "PROMPTMEND_ENV": str(tmp_path / ".env")}
    env.pop("GITHUB_ACTIONS", None)

    def run(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-m", "promptmend.entry", *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=60,
            check=False,
        )

    bare, helped = run(), run("--help")
    assert (bare.returncode, helped.returncode) == (2, 0)
    assert bare.stdout + EOL == helped.stdout
    assert bare.stderr == helped.stderr == b""


# --- review of #109: regressions ----------------------------------------------------------


def test_settings_never_show_credentials_in_a_base_url(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "hunter" + "2pass"
    url = f"https://bob:{secret}@llm.example.com/v1"
    monkeypatch.setenv("OPENROUTER_BASE_URL", url)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("2")
        assert secret not in app.export_screenshot()
        await pilot.press("6")
        assert secret not in app.export_screenshot()

    drive(scenario)


def test_an_older_slower_load_never_overwrites_a_newer_one(espanso: FakeRunner) -> None:
    import threading

    release, started = threading.Event(), threading.Event()
    loaded: list[int] = []

    def loader(group_by: str, first: State | None = None) -> State:
        n = len(loaded)
        loaded.append(n)
        if n == 1:  # the second load is slow and finishes last
            started.set()
            release.wait(10)
        state = gather(group_by, first)
        object.__setattr__(state, "plan_error", f"load {n}")
        return state

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        app.reload()
        while not started.is_set():
            await pilot.pause(0.01)
        app.reload()
        await settle(pilot)
        assert _state(app).plan_error == "load 2"
        release.set()
        for _ in range(20):
            await pilot.pause(0.02)
        assert _state(app).plan_error == "load 2"

    drive(scenario, loader=loader)
    assert loaded == [0, 1, 2]


def test_a_failing_worker_is_reported_not_raised(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bug(provider: str) -> smoke.SmokeResult:
        raise RuntimeError("kaput")

    monkeypatch.setattr(smoke, "run", bug)

    def bad_apply(*args: Any, **kwargs: Any) -> None:
        raise ValueError("bad choice")

    monkeypatch.setattr(deploy, "apply", bad_apply)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("6")
        await press(app, pilot, "#smoke")
        await fill(app, pilot)
        assert pane(app, "diagnostics").last_message == "error: unexpected RuntimeError: kaput"
        await pilot.press("4")
        await press(app, pilot, "#deploy")
        await press(app, pilot, "#submit")
        assert pane(app, "triggers").last_message == "error: bad choice"
        assert app.is_running

    drive(scenario)


def test_deploy_twice_opens_one_dialog(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        buttons = app.screen.query_one("#deploy", Button), app.screen.query_one("#detach", Button)
        buttons[0].press()
        buttons[0].press()
        buttons[1].press()
        await settle(pilot)
        assert isinstance(app.screen, FormModal)
        assert "in progress" in pane(app, "triggers").last_message
        await press(app, pilot, "#cancel")
        assert not isinstance(app.screen, FormModal)
        # Once the dialog is closed, Deploy works again.
        await press(app, pilot, "#deploy")
        assert isinstance(app.screen, FormModal)
        await press(app, pilot, "#submit")
        assert pane(app, "triggers").last_message.endswith("The match files are up to date.")

    drive(scenario)


def test_deploy_refuses_a_plan_that_changed_since_the_preview(espanso: FakeRunner) -> None:
    target = espanso.root / "match" / "prompts-core.yml"

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        await press(app, pilot, "#deploy")
        target.write_text("matches: []  # written meanwhile\n", encoding="utf-8")
        await press(app, pilot, "#submit")
        assert "changed since the preview" in pane(app, "triggers").last_message

    drive(scenario)
    assert target.read_text("utf-8") == "matches: []  # written meanwhile\n"
    assert not (espanso.root / "match" / "prompts-llm.yml").exists()


def test_detach_refuses_a_manifest_that_changed_since_the_preview(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        await press(app, pilot, "#deploy")
        await press(app, pilot, "#submit")
        await press(app, pilot, "#detach")
        manifest = deploy.Manifest.load()
        manifest.entries.popitem()
        manifest.save()
        await press(app, pilot, "#submit")
        assert "changed since the preview" in pane(app, "triggers").last_message

    drive(scenario)
    assert (espanso.root / "match" / "prompts-llm.yml").is_file()


def test_prune_enter_does_not_prune(espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    pruned: list[int | None] = []

    def prune(self: HistoryStore, age: int | None = None) -> int:
        pruned.append(age)
        return 0

    monkeypatch.setattr(HistoryStore, "prune", prune)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("5")
        # The days field has the focus (#392): Enter there only opens the confirmation,
        # whose Cancel has the focus, so a second Enter closes it.
        await press(app, pilot, "#prune")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("enter")
        await settle(pilot)
        assert not isinstance(app.screen, FormModal | ConfirmModal)
        assert pruned == []
        await press(app, pilot, "#prune")
        await fill(app, pilot, days="30")
        assert isinstance(app.screen, ConfirmModal)
        assert "older than 30 day(s)" in app.screen.dialog_title
        await pilot.press("enter")
        await settle(pilot)
        assert pruned == []
        await press(app, pilot, "#prune")
        await fill(app, pilot, days="30")
        await press(app, pilot, "#confirm")

    drive(scenario)
    from datetime import timedelta

    assert pruned == [timedelta(days=30)]


@pytest.mark.parametrize(
    ("windows", "value", "expected"),
    [
        (False, "code --wait", ["/bin/code", "--wait"]),
        (False, "'/opt/my editor/ed' -w", ["/opt/my editor/ed", "-w"]),
        (True, "code --wait", ["C:/bin/code.cmd", "--wait"]),
        (True, '"C:\\Program Files\\Ed\\ed.exe" -w', ["C:\\Program Files\\Ed\\ed.exe", "-w"]),
    ],
)
def test_editor_command_forms(windows: bool, value: str, expected: list[str]) -> None:
    found = {"code": "/bin/code"} if not windows else {"code": "C:/bin/code.cmd"}
    command = profiles_pane.editor_command(
        {"EDITOR": value}, windows=windows, which=lambda name: found.get(name)
    )
    assert command == expected


def test_children_ignore_modules_planted_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "ran"
    for name in ("json", "typer"):
        (tmp_path / f"{name}.py").write_text(
            f"open({str(marker)!r}, 'w').write('planted')\nraise SystemExit(9)\n",
            encoding="utf-8",
        )
    assert smoke.run("ollama").ok
    assert not marker.exists()


def test_builtin_profiles_load_in_a_fixed_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Linux snapshot listed `general` before `default`: a directory's order is the file
    # system's, so the loader sorts.
    from promptmend import prompt_builder

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

        def read_text(self, encoding: str) -> str:
            return self.name

    class Folder:
        def iterdir(self) -> Iterator[Entry]:
            return iter([Entry("general.md"), Entry("zeta.md"), Entry("default.md")])

    monkeypatch.setattr(prompt_builder, "_PROMPT_DIR", Folder())
    assert list(prompt_builder._load_profiles()) == ["default", "general", "zeta"]


# --- About (#112) ------------------------------------------------------------------------


def test_about_lists_version_runtime_and_folders(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptmend import config_store
    from promptmend.tui import app as app_module
    from promptmend.tui import brand
    from promptmend.tui.intro import AboutScreen

    monkeypatch.setattr(app_module, "installed_version", lambda: "9.8.7")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("a")
        await settle(pilot)
        assert isinstance(app.screen, AboutScreen)
        facts = "\n".join(app.screen.facts)
        assert f"{brand.NAME} 9.8.7" in facts
        assert "Installed: uv" in facts
        assert str(config_store.config_dir()) in facts
        assert "Licence:   MIT" in facts
        assert KEY not in facts
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, AboutScreen)
        await pilot.press("a")
        await settle(pilot)
        await pilot.press("a")
        await settle(pilot)
        assert not isinstance(app.screen, AboutScreen)
        await pilot.press("a")
        await settle(pilot)
        await press(app, pilot, "#about-close")
        assert not isinstance(app.screen, AboutScreen)

    drive(scenario)


def test_about_before_the_state_is_read(espanso: FakeRunner) -> None:
    from promptmend.tui.intro import AboutScreen

    def broken(group_by: str, first: State | None = None) -> State:
        raise RuntimeError("boom")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("a")
        await settle(pilot)
        assert isinstance(app.screen, AboutScreen)
        assert "Installed: unknown" in app.screen.facts

    drive(scenario, loader=broken)


@pytest.mark.parametrize("args", [("ui",), ()])
def test_ui_opens(terminal: dict[str, Any], args: tuple[str, ...]) -> None:
    result = _invoke(*args)
    assert result.exit_code == 0, result.output
    assert terminal["launched"] == 1


# The intro is gone (#245), and so, since #414, is its `--no-intro`: a usage error, which
# opens nothing.
def test_ui_no_intro_is_a_usage_error(terminal: dict[str, Any]) -> None:
    from test_commands import _plain

    result = _invoke("ui", "--no-intro")
    assert result.exit_code == 2
    assert "No such option: --no-intro" in _plain(result.output)
    assert terminal["launched"] == 0


def test_brand_is_ascii() -> None:
    from promptmend.tui import brand

    assert brand.TAGLINE.isascii()
    assert brand.LOGO.isascii()
    assert max(map(len, brand.LOGO.splitlines())) < brand.NARROW
    assert brand.LOGO.splitlines() == [*brand.MARK, "", brand.TAGLINE]
    assert all(line == line.rstrip() for line in brand.LOGO.splitlines())


def test_about_says_whether_a_newer_release_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend import doctor, update_check
    from promptmend.tui import brand

    monkeypatch.setattr(brand, "runtime", lambda: {"python": "3", "textual": "8"})

    def update_line(update: update_check.UpdateStatus | None) -> str:
        install = {"channel": "uv", "editable": False}
        return next(f for f in brand.about_facts(install, "0.21.0", update) if "Update:" in f)

    newer = update_check.UpdateStatus(update_check.AVAILABLE, "0.22.0", "x")
    hint = doctor.upgrade_hint({"channel": "uv", "editable": False})
    assert hint.endswith(update_check.upgrade_command("uv"))
    assert update_line(newer) == f"Update:    0.22.0 available: {hint}"
    assert update_line(None).endswith("unknown (pypi.org could not be asked)")
    assert update_line(update_check.UpdateStatus(update_check.OFF)).endswith(
        "not checked (PROMPT_UPDATE_CHECK=false)"
    )
    latest = update_check.UpdateStatus(update_check.LATEST, "0.21.0", "x")
    assert update_line(latest).endswith("this is the latest release")


# --- Every action shows its command (#111, stage 1) ---------------------------------------


def test_every_button_has_its_command_as_tooltip(espanso: FakeRunner) -> None:
    from promptmend.tui import teach

    seen: dict[str, object] = {}

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, *TABS)
        for screen in (app.main, app.screen):
            for button in screen.query(Button):
                seen[button.id or ""] = button.tooltip

    drive(scenario)
    known = set(teach.BUTTONS) | set(teach.NO_COMMAND)
    assert set(seen) - known == set(), "give the new button a command in tui/teach.py"
    assert {i: teach.tooltip(i) for i in seen} == seen
    assert seen["deploy"] == "In a terminal:\n$ promptmend espanso deploy"


# Copy last (#111) takes what another tab logged, withheld as the log shows it: never the key.
def test_copy_last_takes_another_tabs_entry_withheld(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyperclip

    def touched(*_: Any) -> Any:
        raise AssertionError("pyperclip was used")

    monkeypatch.setattr(pyperclip, "paste", touched)
    monkeypatch.setattr(pyperclip, "copy", touched)
    copied: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
        copy = app.main.query_one("#home-copy", Button)
        assert copy.disabled
        # The edit dialog refuses a key-like value; behind it, save_setting() refuses too.
        await on_row(app, pilot, "PROMPT_PROFILE")
        settings_tab(app).save("PROMPT_PROFILE", KEY)
        await settle(pilot)
        assert app.session[-1].error
        await pilot.press("1")
        assert not copy.disabled
        await press(app, pilot, "#home-copy")
        assert copied == ["promptmend config set PROMPT_PROFILE <value withheld>"]
        assert KEY not in pane(app, "home").last_message

    drive(scenario)
    assert all(KEY not in text for text in copied)


def test_results_show_their_command_and_home_keeps_the_session(
    saved: Path, espanso: FakeRunner
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        home = pane(app, "home")
        before = _rendered(home, "#home-session")
        # Before any action, one line (#396): no recipes.
        assert before.rstrip("\n") == home_pane.SESSION_EMPTY
        await edit_to(app, pilot, "PROMPT_TIMEOUT_SECONDS", "45")
        result = _rendered(pane(app, "settings"), ".result")
        assert result.startswith("$ promptmend config set PROMPT_TIMEOUT_SECONDS 45\n")
        # A value that looks like a key is never shown, even refused.
        settings_tab(app).save("PROMPT_PROFILE", KEY)
        await settle(pilot)
        await pilot.press("5")
        await press(app, pilot, "#reset")
        await press(app, pilot, "#confirm")
        await pilot.press("1")
        session = _rendered(home, "#home-session")
        assert "This session, as commands:" in session
        assert "$ promptmend config set PROMPT_TIMEOUT_SECONDS 45" in session
        assert "$ promptmend config set PROMPT_PROFILE '<value withheld>'" not in session
        assert "$ promptmend config set PROMPT_PROFILE <value withheld>" in session
        assert "    error: " in session
        assert "$ promptmend history reset" in session
        assert KEY not in session
        assert [e.command.split()[1:3] for e in app.session] == [
            ["config", "set"],
            ["config", "set"],
            ["history", "reset"],
        ]

    drive(scenario)


def test_session_log_keeps_the_latest(espanso: FakeRunner) -> None:
    from promptmend.tui import home_pane, teach

    entries = [teach.Entry(f"promptmend doctor {n}", f"done {n}") for n in range(9)]
    text = home_pane.session_text(entries).plain
    assert "doctor 2\n" not in text
    assert text.count("$ promptmend doctor") == home_pane.SESSION_LINES
    failed = home_pane.session_text([teach.Entry("c", "boom", error=True)]).plain
    assert "    error: boom" in failed


# #396: the empty session log is one muted line, whole at 70 columns (Home's pane has one
# column of padding each side), instead of a title, six recipes and a note.
def test_the_empty_session_log_is_one_line() -> None:
    from promptmend.tui import home_pane

    empty = home_pane.session_text(())
    assert empty.plain == home_pane.SESSION_EMPTY
    assert "\n" not in empty.plain
    assert cell_len(empty.plain) <= 70 - 2
    assert str(empty.style) == "dim"
    assert "tooltip" in empty.plain


# #396: the Triggers table's Provider and Profile cells show the value a call runs with and,
# under it, the setting that supplied it; a value the command line names stands alone. The
# pro tier takes PROMPT_PRO_PROFILE when it is set, on a provider with the pro tier only.
@pytest.mark.parametrize(
    ("env", "ip", "if_"),
    [
        (
            {"PROMPT_PRO_PROFILE": "general"},
            ["openrouter\nPROMPT_PRO_PROVIDER", "pro", "general\nPROMPT_PRO_PROFILE"],
            ["openrouter", "pro", "general\nPROMPT_PRO_PROFILE"],
        ),
        (
            {"PROMPT_PRO_PROFILE": "general", "PROMPT_PRO_PROVIDER": "anthropic"},
            ["anthropic\nPROMPT_PRO_PROVIDER", "pro", "general\nPROMPT_PRO_PROFILE"],
            ["openrouter", "pro", "general\nPROMPT_PRO_PROFILE"],
        ),
        (
            {"PROMPT_PROFILE": "general", "PROMPT_PROVIDER": "ollama"},
            ["openrouter\nPROMPT_PRO_PROVIDER", "pro", "general\nPROMPT_PROFILE"],
            ["openrouter", "pro", "general\nPROMPT_PROFILE"],
        ),
    ],
    ids=["pro-profile", "pro-provider", "standard-settings"],
)
def test_triggers_cells_name_the_setting_a_value_comes_from(
    espanso: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    ip: list[str],
    if_: list[str],
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    standard = env.get("PROMPT_PROFILE", "default")
    seen: dict[str, list[str]] = {}

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        await settle(pilot)
        seen.update(table_rows(app, "triggers", "#triggers"))

    drive(scenario)
    assert seen["-ip-"][1:4] == ip
    assert seen["-if-"][1:4] == if_
    # -i- names openrouter: PROMPT_PROVIDER never changes it; its profile is PROMPT_PROFILE's.
    assert seen["-i-"][1:4] == ["openrouter", "standard", f"{standard}\nPROMPT_PROFILE"]
    assert seen["-il-"][1:4] == ["ollama", "standard", "general"]
    assert seen["-p-"][1:4] == ["prints PROMPT_PERSONA", "-", "-"]


# Review of #396: the -ip- Provider cell draws the provider a call runs, as Home's Pro row
# names it, not PROMPT_PRO_PROVIDER's raw text (empty means openrouter; the parser strips
# and lowercases it).
@pytest.mark.parametrize(
    ("raw", "runs"), [("", "openrouter"), ("OpenAI", "openai"), (" anthropic ", "anthropic")]
)
def test_triggers_ip_cell_and_home_name_the_same_provider(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch, raw: str, runs: str
) -> None:
    monkeypatch.setenv("PROMPT_PRO_PROVIDER", raw)
    seen: dict[str, Any] = {}

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        seen["home"] = next(line for line in _home_text(app).splitlines() if "Pro (-ip-)" in line)
        await pilot.press("4")
        await settle(pilot)
        seen["rows"] = table_rows(app, "triggers", "#triggers")
        seen["runs"] = _state(app).settings.pro_provider

    drive(scenario)
    assert seen["runs"] == runs
    assert seen["rows"]["-ip-"][1] == f"{runs}\nPROMPT_PRO_PROVIDER"
    assert f"{runs} · " in seen["home"]


# Review of #396: the Triggers table takes the height the note, the deploy target and the
# buttons leave and scrolls inside itself, so Show diff, Deploy and Detach (under the table)
# and the deploy target's first line are on screen without scrolling the pane, and Down on
# the table reaches the last trigger.
@pytest.mark.parametrize("size", [(70, 24), (80, 24), (110, 36)])
def test_triggers_buttons_stay_on_screen_and_the_table_scrolls(
    espanso: FakeRunner, size: tuple[int, int]
) -> None:
    from textual.widgets import DataTable

    width, height = size
    seen: dict[str, Any] = {}

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        await settle(pilot)
        triggers = pane(app, "triggers")
        seen["pane scroll"] = (triggers.max_scroll_y, triggers.scroll_y)
        inside = []
        for id_ in ("show-diff", "deploy", "detach"):
            region = triggers.query_one(f"#{id_}").region
            inside.append(region.height > 0 and region.bottom <= height - 1)
            inside.append(region.x >= 0 and region.right <= width)
        # The deploy target's first line too (its paths wrap; tests' temp paths are long).
        target = triggers.query_one("#deploy-target").region
        inside.append(target.height > 0 and target.y < height - 1)
        seen["inside"] = inside
        table = triggers.query_one("#triggers", DataTable)
        table.focus()
        await pilot.pause()
        for _ in range(table.virtual_size.height):
            await pilot.press("down")
        await pilot.pause()
        # Scrolled as far as it goes: the last trigger's row is the table's last line.
        seen["table"] = (table.scroll_y, table.max_scroll_y, table.size.height > 1)

    drive(scenario, size=size)
    assert seen["pane scroll"][1] == 0
    assert seen["inside"] == [True] * 7
    scroll_y, max_scroll_y, shown = seen["table"]
    assert shown
    assert scroll_y == max_scroll_y
