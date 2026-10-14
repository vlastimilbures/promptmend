"""The full-screen interface (#93), driven headless through Textual's Pilot: one test per
screen's main action, each through the same services the headless commands use. Espanso,
uv and brew run through a fake runner, the editor and the smoke test are replaced, and no
test calls a provider."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Select, Static
from textual.worker import WorkerCancelled

from promptmend import config, config_store, deploy, history, smoke
from promptmend import profiles as profile_service
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
from promptmend.tui.app import HIGH_CONTRAST, ManageApp
from promptmend.tui.home import pill
from promptmend.tui.modals import ConfirmModal, EditModal, FormModal, PickModal, TextModal
from promptmend.tui.previous import PreviousInstallScreen
from promptmend.tui.state import State, gather

if TYPE_CHECKING:
    from conftest import SeedHistory
    from textual.widget import Widget
    from typer.testing import Result

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
    return config._user_config_dir()


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


def drive(scenario: Callable[[ManageApp, Pilot[int]], Awaitable[None]], **kwargs: Any) -> ManageApp:
    app = ManageApp(**kwargs)

    async def main() -> None:
        async with app.run_test(size=SIZE) as pilot:
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


def _previous_screen(app: ManageApp) -> PreviousInstallScreen:
    screen = app.screen
    assert isinstance(screen, PreviousInstallScreen)
    return screen


def _plan() -> deploy.Plan:
    plan = gather().plan
    assert plan is not None
    return plan


def test_gather_asks_each_command_once(
    espanso: FakeRunner, saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#242: doctor, the plan and the previous-install lookup (gate open: saved mode, nothing
    saved yet) share one answer per command per reload."""
    launcher = deploy.resolve_launcher()

    def resolve(*, runner: deploy.Runner | None = None, **_: Any) -> deploy.Launcher:
        (runner or deploy.run_command)(["uv", "tool", "dir"])
        return launcher

    monkeypatch.setattr(deploy, "resolve_launcher", resolve)
    assert gather().plan is not None
    calls = [" ".join(call) for call in espanso.calls]
    assert calls.count("espanso path config") == 1
    assert calls.count("uv tool dir") == 1


def pane(app: ManageApp, tab: str) -> base.Pane:
    return app.main.query_one(f"#{tab}-pane", base.Pane)


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


def test_header_shows_the_name_and_installed_version(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#112: the header names the tool and its version on every screen."""
    from textual.widgets._header import HeaderTitle

    from promptmend.tui import app as app_module

    monkeypatch.setattr(app_module, "__version__", "9.8.7")
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
    def broken(group_by: str) -> State:
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
        await press(app, pilot, "#show-diff")
        assert "No plan" in pane(app, "triggers").last_message

    drive(scenario, loader=broken)


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

    def loader(group_by: str) -> State:
        calls.append(group_by)
        return gather(group_by)

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


def test_settings_migrate_reports_a_refusal(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(environ: Mapping[str, str] | None = None, *, source: Path | None = None) -> None:
        raise config_store.MigrationError("the marker is damaged")

    monkeypatch.setattr(config_store, "plan_migration", refused)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("2")
        await press(app, pilot, "#migrate-env")
        assert pane(app, "settings").last_message == "error: the marker is damaged"

    drive(scenario)


def test_settings_space_toggles_history(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
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

    def loader(group_by: str) -> State:
        calls.append(group_by)
        return gather(group_by)

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

    def loader(group_by: str) -> State:
        if blocking.is_set():
            release.wait(10)
        return gather(group_by)

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

    drive(scenario, loader=loader)


def test_settings_a_withheld_value_is_kept_by_an_empty_save(
    saved: Path, espanso: FakeRunner
) -> None:
    """Review of #199: a value that looks like a key opens as an empty field, says so, and
    saving the field empty keeps it (never a silent wipe)."""
    saved.mkdir(parents=True, exist_ok=True)
    toml = saved / "config.toml"
    toml.write_text(f'OLLAMA_MODEL = "{KEY}"\n', encoding="utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
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
        assert list(settings.rows) == ["OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"]
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

    def loader(group_by: str) -> State:
        calls.append(group_by)
        return gather(group_by)

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


def test_settings_migrate_env_with_preview(saved: Path, espanso: FakeRunner) -> None:
    saved.mkdir(parents=True)
    (saved / ".env").write_text(
        f"PROMPT_PROFILE=general\nOPENROUTER_API_KEY={KEY}\n", encoding="utf-8"
    )

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("2")
        await press(app, pilot, "#migrate-env")
        assert isinstance(app.screen, ConfirmModal)
        assert KEY not in _dialog(app).preview
        await pilot.press("n")
        await settle(pilot)
        assert (saved / ".env").is_file()
        await press(app, pilot, "#migrate-env")
        await press(app, pilot, "#confirm")
        assert pane(app, "settings").last_message.startswith("Migrated. Backup:")
        await press(app, pilot, "#migrate-env")
        assert "already" in pane(app, "settings").last_message

    drive(scenario)
    assert config_store.saved_secret_names() == ("OPENROUTER_API_KEY",)


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


def test_profiles_migrate_copies_after_confirming(
    espanso: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "project" / profile_service.PROMPTS_PATH
    source.mkdir(parents=True)
    (source / "general.md").write_text("Edited.", encoding="utf-8")
    (source / "extra.md").write_text("Mine.", encoding="utf-8")
    monkeypatch.setattr(
        profile_service, "git_pristine_profiles", lambda root, rev=None: {"general": "Old."}
    )

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("3")
        await press(app, pilot, "#migrate-profiles")
        assert "extra.md (added)" in _dialog(app).preview
        await press(app, pilot, "#confirm")
        assert pane(app, "profiles").last_message == (
            "extra: copied\ngeneral: copied\nYour general.md replaces the built-in only once "
            "PROMPT_PROFILE_OVERRIDES lists it: "
            "`promptmend config set PROMPT_PROFILE_OVERRIDES general`."
        )

    drive(scenario)
    from promptmend.prompt_builder import user_profiles_dir

    assert sorted(p.name for p in user_profiles_dir().iterdir()) == ["extra.md", "general.md"]


def test_profiles_migrate_default_and_nothing_to_copy(
    espanso: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "project" / profile_service.PROMPTS_PATH
    source.mkdir(parents=True)
    (source / "default.md").write_text("Edited.", encoding="utf-8")
    pristine = {"default": "Old."}
    monkeypatch.setattr(profile_service, "git_pristine_profiles", lambda root, rev=None: pristine)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("3")
        await press(app, pilot, "#migrate-profiles")
        await press(app, pilot, "#cancel")
        assert pane(app, "profiles").last_message == ""
        await press(app, pilot, "#migrate-profiles")
        await press(app, pilot, "#confirm")
        assert "PROMPT_PROFILE_OVERRIDES default`" in pane(app, "profiles").last_message
        pristine["default"] = "Edited."
        await press(app, pilot, "#migrate-profiles")
        assert pane(app, "profiles").last_message == profile_service.NOTHING_CHANGED

    drive(scenario)


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


def test_profiles_migrate_without_a_checkout_asks_for_one(
    espanso: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No checkout found and no editable install: the button asks for the folder instead
    of naming a --checkout flag the interface cannot pass (#237)."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / profile_service.PROMPTS_PATH).mkdir(parents=True)
    (elsewhere / profile_service.PROMPTS_PATH / "mine.md").write_text("Mine.", "utf-8")
    monkeypatch.setattr(profile_service, "git_pristine_profiles", lambda root, rev=None: {})

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        profiles = pane(app, "profiles")
        await pilot.press("3")
        await press(app, pilot, "#migrate-profiles")
        await fill(app, pilot, previous_path="  ")
        assert profiles.last_message == "No path entered."
        await press(app, pilot, "#migrate-profiles")
        await fill(app, pilot, previous_path=str(tmp_path / "nothing"))
        assert "is it a checkout of this project?" in profiles.last_message
        assert "--checkout" not in profiles.last_message
        await press(app, pilot, "#migrate-profiles")
        await fill(app, pilot, previous_path="sk-or-v1-" + "ab12" * 16)
        assert "looks like a key" in profiles.last_message
        await press(app, pilot, "#migrate-profiles")
        await fill(app, pilot, previous_path=f'"{elsewhere}"')
        assert f"From {elsewhere}:" in _dialog(app).preview
        assert "mine.md (added)" in _dialog(app).preview

    drive(scenario)


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


def test_triggers_deploy_retires_the_legacy_file(espanso: FakeRunner) -> None:
    legacy = espanso.root / "match" / "base.yml"
    legacy.write_text('matches:\n  - trigger: "-p-"  # promptmend\n', encoding="utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("4")
        await press(app, pilot, "#deploy")
        assert "legacy    base.yml will be retired" in _dialog(app).preview
        await press(app, pilot, "#submit")
        assert "retired legacy" in pane(app, "triggers").last_message

    drive(scenario)
    assert not legacy.exists()


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

    def loader(group_by: str) -> State:
        n = len(loaded)
        loaded.append(n)
        if n == 1:  # the second load is slow and finishes last
            started.set()
            release.wait(10)
        state = gather(group_by)
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


# --- Previous install (#110) --------------------------------------------------------------


def _old_checkout(tmp_path: Path, espanso: FakeRunner) -> Path:
    """A checkout an editable install ran from: a .env with one setting and the key, an
    edited profile, and the match files rendered with its .venv launcher."""
    from promptmend import assets, previous_install

    root = tmp_path / "Projects" / "epr"
    (root / profile_service.PROMPTS_PATH).mkdir(parents=True)
    name = previous_install.PROJECT_NAME
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', "utf-8")
    (root / ".env").write_text(f"PROMPT_TIMEOUT_SECONDS=45\nOPENROUTER_API_KEY={KEY}\n", "utf-8")
    (root / profile_service.PROMPTS_PATH / "mine.md").write_text("Mine.\n", "utf-8")
    if os.name == "nt":
        launcher = (root / ".venv" / "Scripts" / "promptmend.exe").as_posix()
    else:
        launcher = str(root / ".venv" / "bin" / "promptmend")
    for match in assets.match_names():
        text = assets.read_match(match).replace(deploy.PLACEHOLDER, launcher)
        (espanso.root / "match" / match).write_bytes(text.encode("utf-8"))
    return root


@pytest.fixture
def previous(
    saved: Path, espanso: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:

    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    return _old_checkout(tmp_path, espanso)


def _found(app: ManageApp) -> str:
    return str(app.screen.query_one("#previous-found").render())


def _disabled(app: ManageApp, step: str) -> bool:
    return app.screen.query_one(f"#previous-{step}", Button).disabled


def test_previous_install_walks_copy_deploy_retire(previous: Path) -> None:
    from promptmend.tui.previous import PreviousInstallScreen

    root = previous.resolve()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert isinstance(app.screen, PreviousInstallScreen)
        assert f"Previous install: {root} (launcher); .env present" in _found(app)
        assert _disabled(app, "retire")
        await press(app, pilot, "#previous-copy")
        assert isinstance(app.screen, ConfirmModal)
        assert "OPENROUTER_API_KEY" in _dialog(app).preview
        assert KEY not in _dialog(app).preview
        await press(app, pilot, "#confirm")
        assert _previous_screen(app).last_message.startswith("Copied; backup in")
        assert f"Settings copied from {root}" in _found(app)
        assert _disabled(app, "copy")
        assert _disabled(app, "retire")  # the match files still run the checkout's CLI
        await press(app, pilot, "#previous-deploy")
        assert isinstance(app.screen, FormModal)
        await press(app, pilot, "#submit")
        assert isinstance(app.screen, PreviousInstallScreen)
        assert not _disabled(app, "retire")
        await press(app, pilot, "#previous-retire")
        assert isinstance(app.screen, ConfirmModal)
        await press(app, pilot, "#confirm")
        assert _previous_screen(app).last_message.startswith(f"Retired: {root / '.env'}")

    drive(scenario)
    assert not (previous / ".env").exists()
    from promptmend.config import Settings

    settings = Settings.load()
    assert settings.timeout == 45
    assert settings.openrouter_api_key == KEY


def test_previous_install_cancel_writes_nothing_and_deploy_warns(previous: Path) -> None:
    before = {p.name: p.read_bytes() for p in previous.parent.parent.rglob("*.yml")}

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("1")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmModal)
        await press(app, pilot, "#cancel")
        await pilot.press("4")  # retire is disabled: the key does nothing
        await settle(pilot)
        assert not isinstance(app.screen, ConfirmModal)
        await press(app, pilot, "#previous-deploy")
        assert isinstance(app.screen, ConfirmModal)
        assert "not copied yet" in app.screen.dialog_title
        assert app.screen.focused.id == "cancel"
        await press(app, pilot, "#cancel")

    drive(scenario)
    assert not config.settings_file().exists()
    assert not (config._user_config_dir() / "secrets.toml").exists()
    after = {p.name: p.read_bytes() for p in previous.parent.parent.rglob("*.yml")}
    assert after == before


def test_previous_install_shown_once_skip_and_home_reopens(previous: Path) -> None:
    from promptmend import previous_install
    from promptmend.tui.previous import PreviousInstallScreen

    async def first(app: ManageApp, pilot: Pilot[int]) -> None:
        assert isinstance(app.screen, PreviousInstallScreen)
        await pilot.press("escape")
        await settle(pilot)
        app.reload()
        await settle(pilot)
        assert not isinstance(app.screen, PreviousInstallScreen)  # once per session
        await press(app, pilot, "#home-previous")
        assert isinstance(app.screen, PreviousInstallScreen)
        await press(app, pilot, "#previous-skip")

    drive(first)
    assert previous_install.skipped_roots() == {str(previous.resolve())}

    async def second(app: ManageApp, pilot: Pilot[int]) -> None:
        assert not isinstance(app.screen, PreviousInstallScreen)
        # Nothing left to offer, yet the button stays: it is the way to enter a path (#237).
        assert app.main.query_one("#home-previous", Button).display
        await press(app, pilot, "#home-previous")
        await settle(pilot)
        assert "No previous install found" in _found(app)
        assert _disabled(app, "skip")

    drive(second)


def test_previous_install_entered_path(
    saved: Path, espanso: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptmend.tui.previous import PreviousInstallScreen

    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    root = _old_checkout(tmp_path, espanso)
    for path in (espanso.root / "match").iterdir():
        path.unlink()  # no launcher signal: only the path the user types finds it

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert not isinstance(app.screen, PreviousInstallScreen)
        await press(app, pilot, "#home-previous")
        assert "No previous install found" in _found(app)
        await press(app, pilot, "#previous-enter")
        await fill(app, pilot, previous_path=str(root))
        assert f"Previous install: {root.resolve()} (entered)" in _found(app)
        await press(app, pilot, "#previous-enter")
        await fill(app, pilot, previous_path="sk-or-v1-" + "ab12" * 16)
        assert "looks like a key" in _previous_screen(app).last_message

    drive(scenario)


def test_previous_install_profiles_tab_uses_the_detected_root(
    previous: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(profile_service, "git_pristine_profiles", lambda root, rev=None: {})

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("escape")
        await settle(pilot)
        await pilot.press("3")
        await press(app, pilot, "#migrate-profiles")
        assert isinstance(app.screen, ConfirmModal)
        assert f"From {previous.resolve()}:" in _dialog(app).preview
        assert "mine.md (added)" in _dialog(app).preview

    drive(scenario)


def test_previous_install_detect_failure_is_shown(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptmend import previous_install

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("no access")

    monkeypatch.setattr(previous_install, "detect", broken)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await press(app, pilot, "#home-previous")
        assert "Could not look for a previous install: no access" in _found(app)

    drive(scenario)


def test_previous_install_steps_wait_for_a_deploy(previous: Path) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        app.main.query_one("#triggers-pane", triggers_pane.TriggersPane).busy = True
        for step in ("copy", "profiles", "deploy", "enter"):
            await press(app, pilot, f"#previous-{step}")
            assert not isinstance(app.screen, ConfirmModal | FormModal), step
            assert _previous_screen(app).last_message.startswith("A deploy is in progress")

    drive(scenario)


def test_previous_install_retire_needs_espanso_to_name_its_folder(
    previous: Path, espanso: FakeRunner
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await press(app, pilot, "#previous-copy")
        await press(app, pilot, "#confirm")
        await press(app, pilot, "#previous-deploy")
        await press(app, pilot, "#submit")
        del espanso.answers["espanso path config"]
        await press(app, pilot, "#previous-retire")
        assert not isinstance(app.screen, ConfirmModal)
        message = _previous_screen(app).last_message
        assert "cannot check which CLI the match files run" in message
        assert f"config retire --from {previous.resolve()} --espanso-dir PATH" in message

    drive(scenario)
    assert (previous / ".env").is_file()


def test_previous_install_entered_path_that_is_no_checkout(
    saved: Path, espanso: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    root = _old_checkout(tmp_path, espanso)
    for path in (espanso.root / "match").iterdir():
        path.unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        app.open_previous()  # Home hides its button: nothing was found
        await settle(pilot)
        await press(app, pilot, "#previous-enter")
        await fill(app, pilot, previous_path=str(elsewhere))
        assert _previous_screen(app).last_message.startswith(f"{elsewhere} is not a checkout")
        assert _previous_screen(app).entered is None
        await press(app, pilot, "#previous-enter")
        await fill(app, pilot, previous_path=f'"{root}"')  # pasted with its quotes
        assert f"Previous install: {root.resolve()} (entered)" in _found(app)

    drive(scenario)


# --- About (#112) ------------------------------------------------------------------------


def test_about_lists_version_runtime_and_folders(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptmend import config_store
    from promptmend.tui import app as app_module
    from promptmend.tui import brand
    from promptmend.tui.intro import AboutScreen

    monkeypatch.setattr(app_module, "__version__", "9.8.7")

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

    def broken(group_by: str) -> State:
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


def test_every_button_has_its_command_as_tooltip(previous: Path) -> None:
    from promptmend.tui import teach
    from promptmend.tui.previous import PreviousInstallScreen

    seen: dict[str, object] = {}

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert isinstance(app.screen, PreviousInstallScreen)
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


def test_home_previous_install_hidden_once_settings_are_saved(
    saved: Path, espanso: FakeRunner
) -> None:
    """Shown while a previous install could still be copied, even with nothing found (#237);
    hidden once config.toml exists, since a copy is then refused (D-MIG-4)."""

    async def shown(app: ManageApp, pilot: Pilot[int]) -> None:
        assert app.main.query_one("#home-previous", Button).display

    drive(shown)
    config_store.save_settings(config_store.read_settings(), {"PROMPT_TIMEOUT_SECONDS": "45"})

    async def hidden(app: ManageApp, pilot: Pilot[int]) -> None:
        assert not app.main.query_one("#home-previous", Button).display

    drive(hidden)
