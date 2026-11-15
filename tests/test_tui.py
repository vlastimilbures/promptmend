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
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Select, Static
from textual.worker import WorkerCancelled

from promptmend import config, config_store, deploy, history, smoke, update_check
from promptmend.history import HistoryStore
from promptmend.tui import (
    base,
    history_pane,
    home_pane,
    profiles_pane,
    settings_model,
    settings_pane,
    triggers_pane,
)
from promptmend.tui.app import HIGH_CONTRAST, TABS, ManageApp
from promptmend.tui.home import pill
from promptmend.tui.modals import ConfirmModal, EditModal, FormModal, PickModal, TextModal
from promptmend.tui.state import FULL, QUICK, State, gather, quick

if TYPE_CHECKING:
    from conftest import SeedHistory
    from textual.widget import Widget
    from typer.testing import Result

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
    """Saved mode: config.toml and the secret store are read and written (per-test dirs)."""
    monkeypatch.delenv("PROMPTMEND_ENV")
    return config.user_config_dir()


async def settle(pilot: Pilot[int]) -> None:
    """Let every worker (the state load, an action) and what it set off finish."""
    for _ in range(4):
        await pilot.pause()
        # A newer state load cancels an older one (exclusive group "load"), as a deploy's
        # early reload and _done()'s do on a slow runner (#215): that one never completes.
        waits = [worker.wait() for worker in pilot.app.workers]
        for result in await asyncio.gather(*waits, return_exceptions=True):
            if isinstance(result, BaseException) and not isinstance(result, WorkerCancelled):
                raise result
    await pilot.pause()


def drive(
    scenario: Callable[[ManageApp, Pilot[int]], Awaitable[None]],
    *,
    settled: bool = True,
    **kwargs: Any,
) -> ManageApp:
    """Run ``scenario`` on a ManageApp(**kwargs), once every load has ended (``settled``)
    or at once (a scenario whose loader blocks waits itself, see until())."""
    app = ManageApp(**kwargs)

    async def main() -> None:
        async with app.run_test(size=SIZE) as pilot:
            if settled:
                await settle(pilot)
            await scenario(app, pilot)

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
    Espanso folder too (its plan, of the resolved folder, is not shared then)."""
    from promptmend import doctor

    if linked:
        link = tmp_path / "linked-espanso"
        try:
            link.symlink_to(espanso.root, target_is_directory=True)
        except OSError:
            pytest.skip("no symlinks here")
        espanso.answers["espanso path config"] = f"{link}\n"
    state = gather().settled()
    assert state.pending is None
    alone = doctor.run(clipboard=False, update=state.update)
    assert state.report.to_json() == alone.to_json()


class BlockedPypi:
    """An update check that waits until released, as a proxy that drops pypi.org does."""

    def __init__(self) -> None:
        import threading

        self.release = threading.Event()
        self.answer = update_check.UpdateStatus(
            update_check.AVAILABLE, "99.0.0", "2026-11-15T09:00:00+00:00"
        )

    def __call__(self) -> update_check.UpdateStatus:
        assert self.release.wait(10), "never released"
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
        wizard.go("keys")
        await pilot.pause()
        field = wizard.query_one("#setup-key-OPENROUTER_API_KEY", Input)
        field.focus()
        field.value = "typed"
        await pilot.pause()
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

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        state = _state(app)
        newer = dataclasses.replace(
            state, update=update_check.UpdateStatus(update_check.LATEST, "1.0.0", "x")
        )
        app.post_message(LateUpdate(newer, app.generation - 1))
        await pilot.pause()
        assert app.state is state

    drive(scenario)


# --- Two stages (#317) ----------------------------------------------------------------------


async def until(
    pilot: Pilot[int], done: Callable[[], bool], why: Callable[[], object] = lambda: ""
) -> None:
    """Pause (at most ~10 s of pauses) until ``done()``: for a scenario whose full load is
    blocked, which settle() would wait for. ``why()`` says what was seen when it never is."""
    for _ in range(1000):
        if done():
            return
        await pilot.pause(0.01)
    raise AssertionError(f"never happened: {why()}")


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
        assert self.release.wait(10), "never released"
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
        table.move_cursor(row=2)
        await pilot.pause()
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        app.open_setup()
        await until(pilot, lambda: isinstance(app.screen, SetupScreen))
        wizard = app.screen
        assert isinstance(wizard, SetupScreen)
        await pilot.pause()
        wizard.go("keys")
        await pilot.pause()
        field = wizard.query_one("#setup-key-OPENROUTER_API_KEY", Input)
        field.focus()
        field.value = "typed"
        await pilot.pause()
        blocked.release.set()
        await settle(pilot)
        assert _state(app).full
        assert table.cursor_row == 2
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


def test_the_done_step_keeps_its_focus_across_stages(espanso: FakeRunner) -> None:
    """Review of #317: on the last step, the full State (and the plan read with it) leaves the
    focus where the person put it, instead of moving it back to Quit."""
    from promptmend.tui.setup_wizard import SetupOptions, SetupScreen

    blocked = BlockedLoad()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        # The wizard mounted, shown the quick State and read its plan: nothing of its start
        # is still on its way (a focus moves after a refresh, so each step below waits for it).
        await until(pilot, lambda: app.state is not None)
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and app.screen.is_mounted)
        wizard = app.screen
        assert isinstance(wizard, SetupScreen)
        await until(pilot, lambda: wizard.plan is not None or wizard.plan_error is not None)
        quit_ = wizard.query_one("#setup-quit", Button)
        wizard.go("done")
        await until(pilot, lambda: app.focused is quit_, lambda: app.focused)
        # The hidden Back and Next never take the focus here, not even one already on its
        # way (Textual lets a button in a hidden row be focused; the Windows CI did, #317).
        for hidden in ("#setup-next-step", "#setup-back"):
            wizard.query_one(hidden, Button).focus()
            await pilot.pause()
            assert app.focused is quit_, hidden
        opened = wizard.query_one("#setup-open", Button)
        opened.focus()
        await until(pilot, lambda: app.focused is opened, lambda: app.focused)
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


def test_the_stages_of_an_older_reload_never_replace_a_newer_one(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#317 with #315's generation guard: within a reload the quick State comes before the
    full one; a reload started while the first is still reading shows its own quick and full
    States, and the first one's full State, landing after, is dropped. The update check is
    held, so its late update (which legitimately replaces the State object) lands only at the
    end, and it is the newer reload's."""
    import threading

    blocked = BlockedPypi()
    monkeypatch.setattr(update_check, "check_configured", blocked)
    release = threading.Event()
    started = threading.Event()
    loads: list[int] = []

    def loader(group_by: str, first: State | None = None) -> State:
        loads.append(1)
        if len(loads) == 1:  # the first reload's full read is the slow one
            started.set()
            assert release.wait(10)
        return gather(group_by, first)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        seen: list[tuple[int, str]] = []
        newer_full = threading.Event()
        late = threading.Event()
        real = app._show

        def show(state: State, generation: int) -> None:
            seen.append((generation, state.stage))
            real(state, generation)
            if generation == 2 and state.full:
                newer_full.set()
            if generation == 1 and state.full:
                late.set()

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
        await until(pilot, late.is_set, why)
        await settle(pilot)
        assert app.state is newer  # the first reload's full State was dropped
        # The first reload's quick State may have been shown before its show() was
        # wrapped; what was seen is in order, each reload quick first.
        assert seen[-1] == (1, FULL)
        assert seen[-3:-1] == [(2, QUICK), (2, FULL)]
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
    """Open Settings, put the list's cursor on ``name`` and press ``keys`` there."""
    await pilot.press("2")
    await settle(pilot)
    listing = settings_tab(app).query_one(settings_pane.SettingsList)
    assert listing.has_focus
    listing.highlighted = listing.get_option_index(name)
    await pilot.pause()
    assert settings_tab(app).cursor == name
    if keys:
        await pilot.press(*keys)
        await settle(pilot)


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
        ("2", "#smoke"),
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


def test_no_color_is_left_to_textual(monkeypatch: pytest.MonkeyPatch, espanso: FakeRunner) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert app.no_color

    drive(scenario)


# --- Home ---------------------------------------------------------------------------------


def _rendered(widget: Widget, selector: str, width: int = 116) -> str:
    """What a Static shows, as plain text at ``width`` columns (Home's grids are Rich
    tables, not Text)."""
    from rich.console import Console

    console = Console(width=width, record=True, color_system=None, file=io.StringIO())
    console.print(widget.query_one(selector, Static).content)
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
        assert "fail  Rewrites" not in shown  # the label is FAIL, as in Diagnostics
        assert "FAIL  Rewrites" in shown
        assert "-> 2 Settings" in shown
        await press(app, pilot, "#home-reload")
        await pilot.press("r")
        await settle(pilot)

    drive(scenario, loader=loader)
    assert calls == ["trigger"] * 3
    # Doctor never read the clipboard, and only read-only Espanso commands ran.
    assert {" ".join(c[:2]) for c in espanso.calls} <= {"espanso path", "espanso status"}


# A persona matching the gate's patterns is a warning on Home and in Diagnostics, by finding
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
        assert (row.value, row.on, row.source) == ("set", True, "secrets.toml")
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
            "ANTHROPIC_API_KEY removed from the secret store."
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
        assert (row.value, row.on, row.source) == ("on", True, "default")
        await on_row(app, pilot, "PROMPT_HISTORY", "space")
        assert not _state(app).settings.history
        row = settings_rows(app)["PROMPT_HISTORY"]
        assert (row.value, row.on, row.source) == ("off", False, "config.toml")
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
        # The current value is marked and highlighted; Escape changes nothing.
        assert screen.choices == [("paste  (current)", "paste"), ("clipboard", "clipboard")]
        assert screen.query_one("#pick", OptionList).highlighted == 0
        await pilot.press("escape")
        await settle(pilot)
        assert app.session == []
        await pick(app, pilot, "PROMPT_OUTPUT", "clipboard")
        assert _state(app).settings.output == "clipboard"
        row = settings_rows(app)["PROMPT_OUTPUT"]
        assert (row.value, row.on, row.source) == ("clipboard", True, "config.toml")
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
        assert settings_rows(app)["PROMPT_OUTPUT"].on is False
        await pilot.press("u")
        await settle(pilot)
        assert "nothing to do" in pane(app, "settings").last_message

    drive(scenario)
    assert "PROMPT_OUTPUT" not in (saved / "config.toml").read_text("utf-8")


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

    def first(group_by: str) -> State:
        # The quick stage would read the change back at once (#317): it waits as well.
        if blocking.is_set():
            release.wait(10)
        return quick(group_by)

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

    drive(scenario, loader=loader, quick_loader=first)


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
        assert "looks like a key" in str(screen.query_one("#edit-error", Static).render())
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
        assert settings_rows(app)["PROMPT_PERSONA"].value == "(empty)"
        await edit_to(app, pilot, "PROMPT_PERSONA", persona)
        row = settings_rows(app)["PROMPT_PERSONA"]
        assert (row.value, row.on) == ("<set, hidden>", True)
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


def test_settings_filter(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await open_tab(app, pilot, "settings")
        settings = settings_tab(app)
        count = settings.query_one("#settings-count", Static)
        assert str(count.render()) == f"{len(settings_model.SETTINGS)} settings"
        await on_row(app, pilot, "PROMPT_OUTPUT", "slash")
        assert settings.query_one(settings_pane.FilterInput).has_focus
        await pilot.press(*"hist")
        await settle(pilot)
        assert list(settings.rows) == ["PROMPT_HISTORY", "PROMPT_HISTORY_RETENTION_DAYS"]
        assert str(count.render()) == f"2 of {len(settings_model.SETTINGS)}"
        assert settings.cursor == "PROMPT_HISTORY"
        # A group's name matches too; Enter and Down go back to the list, keeping the filter.
        await pilot.press("enter")
        assert settings.query_one(settings_pane.SettingsList).has_focus
        await pilot.press("slash", *"keys")  # the filter's text is selected: typing replaces it
        await settle(pilot)
        assert list(settings.rows) == ["OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
        await pilot.press("down")
        assert settings.query_one(settings_pane.SettingsList).has_focus
        await pilot.press("slash", *"zzz")
        await settle(pilot)
        assert settings.rows == {}
        assert "No setting matches 'zzz'" in str(settings.query_one("#settings-help").render())
        # Nothing to act on: the keys do nothing.
        settings.toggle()
        settings.edit()
        settings.reset()
        assert app.session == []
        # Escape clears it and goes back to the list.
        await pilot.press("escape")
        await settle(pilot)
        assert settings.query_one(settings_pane.SettingsList).has_focus
        assert len(settings.rows) == len(settings_model.SETTINGS)

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
        assert app.focused is None
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
        assert screen.choices[0][0] == "(empty)  (current)"
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


def test_settings_test_call_uses_the_stub_service(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []

    def fake_run(provider: str) -> smoke.SmokeResult:
        called.append(provider)
        return smoke.SmokeResult(True, smoke.REPLY, 1, "improve reached the stub")

    monkeypatch.setattr(smoke, "run", fake_run)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("2")
        await press(app, pilot, "#smoke")
        assert "No provider is called" in _dialog(app).preview
        await press(app, pilot, "#cancel")
        assert called == []
        await press(app, pilot, "#smoke")
        await fill(app, pilot, provider="ollama")
        assert pane(app, "settings").last_message == "ok: improve reached the stub"

    drive(scenario)
    assert called == ["ollama"]


def test_settings_test_call_adds_no_history_row(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#116: the real Test call (a child improve against the stub) is not usage."""
    monkeypatch.setenv("PROMPT_HISTORY", "true")
    store = HistoryStore(history.history_path())

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("2")
        await press(app, pilot, "#smoke")
        await fill(app, pilot, provider="ollama")
        for _ in range(100):  # the child process runs in a worker
            if pane(app, "settings").last_message.startswith("ok: "):
                break
            await asyncio.sleep(0.1)
            await settle(pilot)
        assert pane(app, "settings").last_message.startswith("ok: improve reached the stub")

    drive(scenario)
    assert store.health().operations in (None, 0)


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


def test_triggers_show_fixed_providers_and_deploy(espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        rows = table_rows(app, "triggers", "#triggers")
        assert rows["-i-"][1:5] == ["openrouter", "standard", "PROMPT_PROFILE", "missing"]
        assert rows["-il-"][1:4] == ["ollama", "standard", "general"]
        assert rows["-ic-"][4] == "commented out"
        assert "PROMPT_PROVIDER does not change these" in str(
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


# --- Diagnostics --------------------------------------------------------------------------


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
    assert (found["-ip-"].provider, found["-ip-"].tier) == ("openrouter", "pro")
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
            [sys.executable, "-m", "promptmend.cli", *args],
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
        await pilot.press("2")
        await press(app, pilot, "#smoke")
        await fill(app, pilot)
        assert pane(app, "settings").last_message == "error: unexpected RuntimeError: kaput"
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
        await press(app, pilot, "#prune")
        await pilot.press("enter")
        await settle(pilot)
        assert not isinstance(app.screen, FormModal | ConfirmModal)
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


@pytest.mark.parametrize("args", [("ui",), (), ("ui", "--no-intro")])
def test_ui_opens_and_no_intro_is_a_hidden_no_op(
    terminal: dict[str, Any], args: tuple[str, ...]
) -> None:
    # The intro is gone (#245); `--no-intro` is still accepted so old scripts keep working.
    result = _invoke(*args)
    assert result.exit_code == 0, result.output
    assert terminal["launched"] == 1
    assert "--no-intro" not in _invoke("ui", "--help").output


def test_brand_is_ascii() -> None:
    from promptmend.tui import brand

    assert brand.TAGLINE.isascii()
    assert brand.LOGO.isascii()
    assert max(map(len, brand.LOGO.splitlines())) < brand.NARROW
    assert brand.LOGO.splitlines() == [*brand.MARK, "", brand.TAGLINE]
    assert all(line == line.rstrip() for line in brand.LOGO.splitlines())


def test_about_says_whether_a_newer_release_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend import update_check
    from promptmend.tui import brand

    monkeypatch.setattr(brand, "runtime", lambda: {"python": "3", "textual": "8"})

    def update_line(update: update_check.UpdateStatus | None) -> str:
        install = {"channel": "uv", "editable": False}
        return next(f for f in brand.about_facts(install, "0.21.0", update) if "Update:" in f)

    newer = update_check.UpdateStatus(update_check.AVAILABLE, "0.22.0", "x")
    assert update_line(newer) == f"Update:    0.22.0 available: {update_check._UV}"
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
        assert "In a terminal, try:" in before
        assert "$ promptmend config show" in before
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
