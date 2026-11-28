"""Home's rows, headline, fix and the header's status pill (#112, Mockup B): pure functions
of a State, checked here without the interface. The Pilot tests in test_tui.py check that the
pane shows them; the ones at the end here drive Home's list and fix button (#395)."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable

import pytest
from rich.text import Text
from test_tui_snapshots import fixed_state, quick_of
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Select, TabbedContent, Tabs

from promptmend import deploy, doctor, update_check
from promptmend.config import OUTPUTS, ConfigLayers
from promptmend.tui import base, home, home_pane, triggers_pane
from promptmend.tui.app import ManageApp
from promptmend.tui.home import READY, HomeRow, headline, home_rows, pill
from promptmend.tui.home_pane import FIX_BUTTONS, HomePane
from promptmend.tui.modals import FormModal
from promptmend.tui.state import State


def _report(state: State, **statuses: str) -> doctor.Report:
    """The fixed report with some checks' statuses (and messages) replaced."""
    return doctor.Report(
        tuple(
            dataclasses.replace(c, status=statuses[c.id], message=f"{c.id} says so")
            if c.id in statuses
            else c
            for c in state.report.checks
        )
    )


def _plan(state: State, **states: str) -> deploy.Plan:
    assert state.plan is not None
    steps = [
        dataclasses.replace(s, state=states.get(s.name, deploy.IN_SYNC)) for s in state.plan.steps
    ]
    return dataclasses.replace(state.plan, steps=steps)


def ready() -> State:
    """Every check ok and every match file in sync."""
    state = fixed_state()
    return dataclasses.replace(
        state, report=_report(state, match_files=doctor.OK), plan=_plan(state)
    )


def _rows(state: State) -> dict[str, HomeRow]:
    return {row.label: row for row in home_rows(state)}


def _with_env(state: State, **environ: str) -> State:
    base = {"XDG_CONFIG_HOME": "/home/me/.config", "HOME": "/home/me", **environ}
    layers = ConfigLayers.resolve(base, strict=False)
    return dataclasses.replace(state, layers=layers, settings=layers.settings())


def test_ready_install() -> None:
    rows = home_rows(ready())
    labels = ["Version", "Rewrites", "Pro (-ip-)", "Triggers", "History", "Output", "Checks"]
    assert [r.label for r in rows] == labels
    assert {r.status for r in rows} == {doctor.OK}
    assert headline(rows) == (READY, None)
    assert pill(ready().report) == "ok"
    found = _rows(ready())
    assert found["Rewrites"].text == "openrouter · google/gemini-3.5-flash-lite"
    assert found["Rewrites"].detail == "key set"
    assert found["Pro (-ip-)"].text == "openrouter · openai/gpt-6-luna"
    assert found["Triggers"].text == "3 of 3 match files in sync · Espanso running"
    assert found["History"].text == "on · 57 calls recorded"
    assert found["Checks"].text == "15 ok · 0 warn · 0 fail"


def _update(state: State, status: str, latest: str | None = "0.22.0") -> State:
    return dataclasses.replace(state, update=update_check.UpdateStatus(status, latest, "x"))


def test_version_row_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home, "installed_version", lambda: "0.21.0")
    row = _rows(_update(ready(), update_check.LATEST, "0.21.0"))["Version"]
    assert (row.status, row.text, row.detail) == (doctor.OK, "0.21.0 · latest", "")


@pytest.mark.parametrize(
    ("status", "text"),
    [(update_check.UNKNOWN, "0.21.0"), (update_check.OFF, "0.21.0 · check off")],
)
def test_version_row_without_an_answer(
    monkeypatch: pytest.MonkeyPatch, status: str, text: str
) -> None:
    monkeypatch.setattr(home, "installed_version", lambda: "0.21.0")
    row = _rows(_update(ready(), status, None))["Version"]
    assert (row.status, row.text) == (doctor.OK, text)


def test_a_newer_release_is_a_note_with_its_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home, "installed_version", lambda: "0.21.0")
    state = _update(ready(), update_check.AVAILABLE)
    row = _rows(state)["Version"]
    # The header shows the installed version; About (a) the update command.
    assert (row.status, row.text, row.detail) == (home.NOTE, "0.22.0 available", home.HOW_TO_UPDATE)
    # An update is no problem: still ready, and the pill counts only doctor's checks.
    assert headline(home_rows(state)) == (READY, None)
    assert pill(state.report) == "ok"


def test_a_note_never_outranks_a_problem() -> None:
    state = _update(fixed_state(), update_check.AVAILABLE)
    assert headline(home_rows(state)) == (
        "Almost ready: one match file was edited since the last deploy.",
        "triggers",
    )
    assert home.worst(home.NOTE) == doctor.OK


def test_the_snapshot_state_reads_as_mockup_b() -> None:
    rows = home_rows(fixed_state())
    assert headline(rows) == (
        "Almost ready: one match file was edited since the last deploy.",
        "triggers",
    )
    assert _rows(fixed_state())["Triggers"].text.startswith("2 of 3 match files in sync")
    assert pill(fixed_state().report) == "1 warning"


def test_a_missing_key_is_the_headline_and_points_to_providers() -> None:
    state = _with_env(ready())
    found = _rows(state)
    assert found["Rewrites"].status == doctor.FAIL
    assert found["Rewrites"].detail == "key not set"
    assert headline(home_rows(state)) == (
        "Not ready: OPENROUTER_API_KEY is not set, so -i- cannot rewrite.",
        "settings",
    )


# -ip- names no provider: its row follows PROMPT_PRO_PROVIDER, with that provider's own model
# and key (#278).
def test_pro_row_follows_the_pro_provider() -> None:
    state = _with_env(
        ready(), OPENROUTER_API_KEY="-".join(("a", "b")), PROMPT_PRO_PROVIDER="openai"
    )
    row = _rows(state)["Pro (-ip-)"]
    assert (row.status, row.text, row.detail) == (doctor.FAIL, "openai · gpt-6-luna", "key not set")
    assert row.problem == "OPENAI_API_KEY is not set, so -ip- cannot rewrite"
    state = _with_env(ready(), ANTHROPIC_API_KEY="k", PROMPT_PRO_PROVIDER="anthropic")
    row = _rows(state)["Pro (-ip-)"]
    assert (row.status, row.detail) == (doctor.OK, "key set")
    assert row.text == f"anthropic · {state.settings.anthropic_model}"


# An older deployed -ip- still names OpenRouter: Home shows what Espanso really runs (no false
# "OPENAI_API_KEY is not set") and warns that PROMPT_PRO_PROVIDER needs a redeploy (#278).
def test_pro_row_follows_an_older_deployed_ip() -> None:
    older = [
        dataclasses.replace(t, provider="openrouter") if t.trigger == "-ip-" else t
        for t in ready().triggers
    ]
    base = dataclasses.replace(ready(), deployed=older)
    state = _with_env(base, OPENROUTER_API_KEY="-".join(("a", "b")), PROMPT_PRO_PROVIDER="openai")
    row = _rows(state)["Pro (-ip-)"]
    assert (row.status, row.text, row.detail, row.jump) == (
        doctor.WARN,
        "openrouter · openai/gpt-6-luna",
        "redeploy",
        "triggers",
    )
    assert "PROMPT_PRO_PROVIDER=openai does not apply" in row.problem
    assert "promptmend espanso deploy" in row.problem
    # The default PROMPT_PRO_PROVIDER: the older file runs as the setting says, no warning.
    state = _with_env(base, OPENROUTER_API_KEY="-".join(("a", "b")))
    assert _rows(state)["Pro (-ip-)"].status == doctor.OK
    # The current file deployed: the setting applies.
    current = dataclasses.replace(ready(), deployed=list(ready().triggers))
    state = _with_env(current, OPENAI_API_KEY="k", PROMPT_PRO_PROVIDER="openai")
    assert _rows(state)["Pro (-ip-)"].text == "openai · gpt-6-luna"


# A file deployed by v0.20.0 or earlier (the shell `cmd:` form) is read: its -i- names
# OpenRouter, so PROMPT_PROVIDER=ollama does not make Home say it stays local (#278).
def test_rows_read_a_shell_form_deployed_file() -> None:
    from test_doctor import V0_20_0_BLOCKS

    from promptmend import assets

    deployed = assets.triggers_in(V0_20_0_BLOCKS, "prompts-llm.yml")
    base = dataclasses.replace(ready(), deployed=deployed)
    state = _with_env(base, PROMPT_PROVIDER="ollama", OPENROUTER_API_KEY="-".join(("a", "b")))
    found = _rows(state)
    assert found["Rewrites"].text == "openrouter · google/gemini-3.5-flash-lite"
    assert found["Pro (-ip-)"].text == "openrouter · openai/gpt-6-luna"


# A commented-out copy, or one whose command line was not read, never stands in for the
# trigger: the active deployed one does, else the shipped one.
def test_rows_skip_inactive_and_unread_deployed_triggers() -> None:
    from promptmend import assets

    deployed = [
        assets.Trigger("-i-", "prompts-llm.yml", False, "improve", "ollama"),
        assets.Trigger("-i-", "prompts-llm.yml", True, "improve", "lmstudio"),
        assets.Trigger("-ip-", "prompts-llm.yml", True),  # a command line not read
    ]
    state = _with_env(dataclasses.replace(ready(), deployed=deployed), PROMPT_PROVIDER="ollama")
    found = _rows(state)
    assert found["Rewrites"].text.startswith("lmstudio · ")
    assert found["Pro (-ip-)"].text == "openrouter · openai/gpt-6-luna"  # the shipped -ip-


# A stale -ip- whose key is missing: the FAIL also says to redeploy, and points to Triggers.
def test_stale_pro_row_with_a_missing_key_points_to_triggers() -> None:
    older = [
        dataclasses.replace(t, provider="openrouter") if t.trigger == "-ip-" else t
        for t in ready().triggers
    ]
    state = _with_env(dataclasses.replace(ready(), deployed=older), PROMPT_PRO_PROVIDER="openai")
    row = _rows(state)["Pro (-ip-)"]
    assert (row.status, row.detail, row.jump) == (doctor.FAIL, "key not set", "triggers")
    assert row.problem.startswith("OPENROUTER_API_KEY is not set, so -ip- cannot rewrite; ")
    assert "redeploy to apply it" in row.problem
    refused = _with_env(
        dataclasses.replace(ready(), deployed=older),
        PROMPT_PRO_PROVIDER="openai",
        PROMPT_LOCAL_ONLY="true",
    )
    row = _rows(refused)["Pro (-ip-)"]
    assert (row.status, row.detail, row.jump) == (doctor.FAIL, "refused", "settings")
    assert "redeploy to apply it" in row.problem


def test_local_only_refuses_the_cloud_triggers() -> None:
    state = _with_env(ready(), OPENROUTER_API_KEY="-".join(("a", "b")), PROMPT_LOCAL_ONLY="true")
    found = _rows(state)
    assert (found["Rewrites"].status, found["Rewrites"].detail) == (doctor.FAIL, "refused")
    assert (
        "PROMPT_LOCAL_ONLY is on, so -i- (openrouter) is refused" in headline(home_rows(state))[0]
    )


def test_a_trigger_on_a_local_provider_needs_no_key() -> None:
    state = ready()
    triggers = [
        dataclasses.replace(t, provider="ollama") if t.trigger == "-i-" else t
        for t in state.triggers
    ]
    found = _rows(dataclasses.replace(state, triggers=triggers))
    assert found["Rewrites"].text == "ollama · qwen3:8b"
    assert found["Rewrites"].detail == "stays local"
    unknown = [
        dataclasses.replace(t, provider="nowhere") if t.trigger == "-i-" else t
        for t in state.triggers
    ]
    row = _rows(dataclasses.replace(state, triggers=unknown))["Rewrites"]
    assert (row.status, row.problem) == (doctor.FAIL, "-i- names nowhere")
    # A warning with no sentence of its own still makes a headline.
    assert headline([HomeRow(doctor.WARN, "Triggers", "odd")]) == (
        "Almost ready: Triggers: odd.",
        None,
    )


@pytest.mark.parametrize(
    ("states", "status", "problem"),
    [
        (
            {"prompts-llm.yml": deploy.STALE},
            doctor.WARN,
            "one match file is not deployed or out of date",
        ),
        (
            {"prompts-llm.yml": deploy.MISSING, "prompts-core.yml": deploy.STALE},
            doctor.WARN,
            "two match files are not deployed or out of date",
        ),
        (
            {"prompts-llm.yml": deploy.MODIFIED, "prompts-core.yml": deploy.FOREIGN},
            doctor.WARN,
            "two match files were edited since the last deploy",
        ),
    ],
)
def test_triggers_row_names_what_deploy_would_change(
    states: dict[str, str], status: str, problem: str
) -> None:
    state = ready()
    row = _rows(dataclasses.replace(state, plan=_plan(state, **states)))["Triggers"]
    assert (row.status, row.problem, row.jump) == (status, problem, "triggers")


def test_triggers_row_follows_doctor_and_espanso() -> None:
    state = ready()
    stopped = dataclasses.replace(state, report=_report(state, espanso=doctor.WARN))
    espanso = next(c for c in stopped.report.checks if c.id == "espanso")
    stopped = dataclasses.replace(
        stopped,
        report=doctor.Report(
            tuple(
                dataclasses.replace(c, data={"found": True, "running": False})
                if c is espanso
                else c
                for c in stopped.report.checks
            )
        ),
    )
    row = _rows(stopped)["Triggers"]
    assert row.status == doctor.WARN
    assert row.text.endswith("Espanso not running")
    assert row.problem == "espanso says so"
    assert state.plan is not None
    unplanned = dataclasses.replace(state, plan=None, plan_error="no launcher found")
    row = _rows(unplanned)["Triggers"]
    assert (row.status, row.problem) == (
        doctor.WARN,
        "cannot compare with Espanso: no launcher found",
    )


@pytest.mark.parametrize(
    ("data", "words"),
    [
        ({"found": False}, "Espanso not found"),
        ({"found": True, "query_failed": True}, "Espanso folder unknown"),
        ({}, "Espanso unknown"),
    ],
)
def test_espanso_words(data: dict[str, object], words: str) -> None:
    state = ready()
    checks = tuple(
        dataclasses.replace(c, data=data, status=doctor.WARN) if c.id == "espanso" else c
        for c in state.report.checks
    )
    row = _rows(dataclasses.replace(state, report=doctor.Report(checks)))["Triggers"]
    assert row.text.endswith(words)


def test_history_row() -> None:
    state = ready()
    assert _rows(_with_env(state, PROMPT_HISTORY="false"))["History"].text == "off"
    broken = dataclasses.replace(state, stats_error="database is locked")
    row = _rows(broken)["History"]
    assert (row.status, row.problem) == (doctor.WARN, "database is locked")
    lost = dataclasses.replace(state, report=_report(state, history=doctor.WARN))
    row = _rows(lost)["History"]
    assert (row.status, row.text) == (doctor.WARN, "on · tracking incomplete")
    counted = doctor.Report(
        tuple(
            dataclasses.replace(c, data={"operations": 1}) if c.id == "history" else c
            for c in state.report.checks
        )
    )
    assert (
        _rows(dataclasses.replace(state, report=counted))["History"].text == "on · 1 call recorded"
    )


# PROMPT_OUTPUT (#134): where the rewrite goes, every output mode named.
@pytest.mark.parametrize(
    ("value", "text"), [("paste", "paste into the app"), ("clipboard", "copy to the clipboard")]
)
def test_output_row(value: str, text: str) -> None:
    assert set(OUTPUTS) == set(home.OUTPUTS)
    row = _rows(_with_env(ready(), PROMPT_OUTPUT=value))["Output"]
    assert (row.status, row.text) == (doctor.OK, text)


def test_checks_row_and_pill_count_every_check() -> None:
    state = ready()
    report = _report(state, persona=doctor.WARN, keys=doctor.FAIL, sqlite=doctor.WARN)
    rows = home_rows(dataclasses.replace(state, report=report))
    checks = {r.label: r for r in rows}["Checks"]
    assert checks.text == "12 ok · 2 warn · 1 fail"
    assert (checks.status, checks.problem) == (doctor.FAIL, "keys: keys says so")
    assert headline(rows) == ("Not ready: keys: keys says so.", "diagnostics")
    assert pill(report) == "1 problem, 2 warnings"
    assert pill(_report(state, keys=doctor.FAIL, sqlite=doctor.FAIL)) == "2 problems"


def test_headline_shows_no_markdown_backticks() -> None:
    # Doctor marks a command with backticks; the headline is plain text (#174).
    state = ready()
    message = "the deployed launcher is gone; run `promptmend espanso deploy`."
    report = doctor.Report(
        tuple(
            dataclasses.replace(c, status=doctor.FAIL, message=message) if c.id == "launcher" else c
            for c in state.report.checks
        )
    )
    rows = home_rows(dataclasses.replace(state, report=report))
    text, _ = headline(rows)
    assert "`" not in text
    assert text == "Not ready: the deployed launcher is gone; run promptmend espanso deploy."


# --- The headline's fix (#395) --------------------------------------------------------------


@pytest.mark.parametrize(
    "states",
    [
        {"prompts-llm.yml": deploy.STALE},
        {"prompts-llm.yml": deploy.MISSING},
        {"prompts-llm.yml": deploy.MODIFIED},
        {"prompts-core.yml": deploy.FOREIGN},
    ],
)
def test_fix_deploys_match_files_not_deployed_out_of_date_or_edited(
    states: dict[str, str],
) -> None:
    state = ready()
    step = home.fix(home_rows(dataclasses.replace(state, plan=_plan(state, **states))))
    assert step == home.Fix(home.DEPLOY, "triggers")
    assert step is not None
    assert step.label == "Deploy or repair…"


def _older_ip() -> State:
    """An older deployed -ip-, which still names OpenRouter (#278)."""
    older = [
        dataclasses.replace(t, provider="openrouter") if t.trigger == "-ip-" else t
        for t in ready().triggers
    ]
    return dataclasses.replace(ready(), deployed=older)


def test_fix_sets_the_missing_key() -> None:
    step = home.fix(home_rows(_with_env(ready())))
    assert step == home.Fix(home.SET_KEY, "settings", "OPENROUTER_API_KEY")
    assert step is not None
    assert step.label == "Set OPENROUTER_API_KEY…"
    # -i- is read first, so its missing key is the headline even beside a stale -ip-.
    stale = _with_env(_older_ip(), PROMPT_PRO_PROVIDER="openai")
    assert home.fix(home_rows(stale)) == home.Fix(home.SET_KEY, "settings", "OPENROUTER_API_KEY")


# A stale -ip- whose key is missing points to Triggers: after the redeploy it runs on
# PROMPT_PRO_PROVIDER (here Anthropic, whose key is set), so the button deploys, as the
# pointer says, and never asks for the OpenRouter key the redeploy makes unneeded.
def test_fix_follows_the_pointer_of_a_stale_ip_with_a_missing_key() -> None:
    base = _older_ip()
    base = dataclasses.replace(
        base,
        triggers=[
            dataclasses.replace(t, provider="ollama") if t.trigger == "-i-" else t
            for t in base.triggers
        ],
        deployed=[
            dataclasses.replace(t, provider="ollama") if t.trigger == "-i-" else t
            for t in base.deployed or []
        ],
    )
    state = _with_env(base, PROMPT_PRO_PROVIDER="anthropic", ANTHROPIC_API_KEY="k")
    rows = home_rows(state)
    row = home.headline_row(rows)
    assert row is not None
    assert (row.label, row.detail, row.jump) == ("Pro (-ip-)", "key not set", "triggers")
    assert headline(rows)[1] == "triggers"
    assert home.fix(rows) == home.Fix(home.DEPLOY, "triggers")


def test_fix_redeploys_an_older_ip() -> None:
    key = "-".join(("a", "b"))
    state = _with_env(_older_ip(), OPENROUTER_API_KEY=key, PROMPT_PRO_PROVIDER="openai")
    assert home.fix(home_rows(state)) == home.Fix(home.DEPLOY, "triggers")


def test_fix_otherwise_opens_the_tab_the_row_names() -> None:
    state = ready()
    # Espanso not running: a deploy does not fix that; the Triggers tab says more.
    stopped = dataclasses.replace(state, report=_report(state, espanso=doctor.WARN))
    step = home.fix(home_rows(stopped))
    assert step == home.Fix(home.OPEN, "triggers")
    assert step is not None
    assert step.label == f"Open {home.TAB_LABELS['triggers']}"
    failing = dataclasses.replace(state, report=_report(state, keys=doctor.FAIL))
    assert home.fix(home_rows(failing)) == home.Fix(home.OPEN, "diagnostics")
    refused = _with_env(state, OPENROUTER_API_KEY="-".join(("a", "b")), PROMPT_LOCAL_ONLY="true")
    assert home.fix(home_rows(refused)) == home.Fix(home.OPEN, "settings")


def test_no_fix_when_ready_checking_or_not_checked() -> None:
    assert home.fix(home_rows(ready())) is None
    assert home.headline_row(home_rows(ready())) is None
    checking = quick_of(ready())
    assert home.fix(home_rows(checking)) is None
    # Not checked: no tab fixes it; Check again (r) is already Home's button.
    assert home.fix(home_rows(checking.not_checked("RuntimeError: no"))) is None


# --- Home's list and fix button, driven through Pilot (#395) --------------------------------


def _drive(
    loader: Callable[[], State],
    scenario: Callable[[ManageApp, Pilot[int]], Awaitable[None]],
    quick_loader: Callable[[], State] | None = None,
) -> None:
    """Run ``scenario`` on the interface, each reload's full State read from ``loader`` and
    its quick one from ``quick_loader`` (by default the full one's first stage)."""
    first = quick_loader or (lambda: quick_of(loader()))
    app = ManageApp(loader=lambda _g, _first: loader(), quick_loader=lambda _g: first())

    async def main() -> None:
        async with app.run_test(size=(110, 36)) as pilot:
            await _settle(app, pilot)
            await scenario(app, pilot)

    asyncio.run(main())


async def _settle(app: ManageApp, pilot: Pilot[int]) -> None:
    for _ in range(3):
        await pilot.pause()
        await app.workers.wait_for_complete()
    await pilot.pause()


def _home(app: ManageApp) -> HomePane:
    return app.main.query_one("#home-pane", HomePane)


def _listing(app: ManageApp) -> OptionList:
    return _home(app).query_one("#home-rows", OptionList)


def _highlighted(app: ManageApp) -> str | None:
    listing = _listing(app)
    index = listing.highlighted
    return None if index is None else listing.options[index].id


def _active(app: ManageApp) -> str:
    return app.main.query_one(TabbedContent).active


def _fix_buttons(app: ManageApp) -> dict[str, str]:
    """The fix buttons shown, by id, with their labels."""
    found = {}
    for id_ in FIX_BUTTONS.values():
        button = _home(app).query_one(f"#{id_}", Button)
        if button.display:
            found[id_] = str(button.label)
    return found


def _on_row(app: ManageApp, label: str) -> None:
    listing = _listing(app)
    listing.highlighted = next(i for i, o in enumerate(listing.options) if o.id == label)


def test_enter_on_a_row_opens_its_tab_and_digits_still_work() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        listing = _listing(app)
        # The list has the focus when the interface opens, on its first row.
        assert app.focused is listing
        assert _highlighted(app) == "Version"
        # A row with no tab and no pointer (Version, latest): Enter changes nothing.
        await pilot.press("enter")
        await _settle(app, pilot)
        assert _active(app) == "home"
        _on_row(app, "Checks")
        await pilot.press("enter")
        await _settle(app, pilot)
        assert _active(app) == "diagnostics"
        # Leaving Home gives the focus to the tab bar: no hidden row acts on Enter.
        assert isinstance(app.focused, Tabs)
        await pilot.press("1")
        await _settle(app, pilot)
        assert app.focused is listing
        _on_row(app, "Triggers")
        await pilot.press("enter")
        await _settle(app, pilot)
        assert _active(app) == "triggers"
        await pilot.press("1")
        await _settle(app, pilot)
        # The digits stay bindings while the list has the focus.
        assert app.focused is listing
        await pilot.press("5")
        await _settle(app, pilot)
        assert _active(app) == "history"

    _drive(fixed_state, scenario)


def test_enter_on_a_not_checked_row_checks_again() -> None:
    reads: list[int] = []

    def broken() -> State:
        reads.append(1)
        raise RuntimeError("no doctor")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert _fix_buttons(app) == {}
        _on_row(app, "Triggers")
        before = len(reads)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert len(reads) == before + 1
        assert _active(app) == "home"
        assert _highlighted(app) == "Triggers"

    _drive(broken, scenario, quick_loader=lambda: quick_of(ready()))


def test_enter_on_a_newer_release_shows_about(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend.tui.intro import AboutScreen

    monkeypatch.setattr(home, "installed_version", lambda: "0.21.0")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("enter")  # on Version: `0.22.0 available  a: how to update`
        await _settle(app, pilot)
        assert isinstance(app.screen, AboutScreen)

    _drive(lambda: _update(ready(), update_check.AVAILABLE), scenario)


def _tab_changes(app: ManageApp) -> list[str]:
    """Every value TabbedContent.active takes from now on."""
    changes: list[str] = []

    def changed(tab: str) -> None:
        changes.append(tab)

    app.main.watch(app.main.query_one(TabbedContent), "active", changed, init=False)
    return changes


async def _press_fix(app: ManageApp, pilot: Pilot[int], button: str, how: str) -> None:
    """Press Home's fix button the way a person does: Tab from the list, then Enter (the
    button has the focus as Home is left), or a click."""
    if how == "keyboard":
        await pilot.press("tab")
        assert app.focused is _home(app).query_one(f"#{button}", Button)
        await pilot.press("enter")
    else:
        await pilot.click(f"#{button}")
    await _settle(app, pilot)


# The fix button leaves Home with the focus on it (#395 review): the tab changes once, the
# dialog opens over the right tab, and closing it leaves the person there.
@pytest.mark.parametrize("how", ["keyboard", "click"])
def test_the_fix_button_opens_the_deploy_preview(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    plan = fixed_state().plan
    monkeypatch.setattr(triggers_pane, "current_plan", lambda: plan)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert _fix_buttons(app) == {"home-deploy": "Deploy or repair…"}
        changes = _tab_changes(app)
        await _press_fix(app, pilot, "home-deploy", how)
        assert changes == ["triggers"]
        dialog = app.screen
        assert isinstance(dialog, FormModal)
        assert app.focused is dialog.query_one("#cancel", Button)
        await pilot.press("enter")  # Cancel: nothing is deployed
        await _settle(app, pilot)
        assert not isinstance(app.screen, FormModal)
        assert changes == ["triggers"]
        assert isinstance(app.focused, Tabs)
        assert not app.main.query_one("#triggers-pane", triggers_pane.TriggersPane).busy

    _drive(fixed_state, scenario)


def test_the_deploy_fix_only_reports_while_the_quick_state_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never() -> deploy.Plan:
        raise AssertionError("the plan was read on the quick State")

    monkeypatch.setattr(triggers_pane, "current_plan", never)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert app.state is not None
        # A reload's quick State is in while the button still shows the full one's fix.
        app.state = quick_of(app.state)
        _home(app).query_one("#home-deploy", Button).press()
        await _settle(app, pilot)
        assert _active(app) == "triggers"
        assert not isinstance(app.screen, FormModal)
        triggers = app.main.query_one("#triggers-pane", triggers_pane.TriggersPane)
        assert triggers.last_message == base.WAITING
        assert not triggers.busy

    _drive(fixed_state, scenario)


@pytest.mark.parametrize("how", ["keyboard", "click"])
def test_the_fix_button_opens_set_key_for_the_missing_key(how: str) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert _fix_buttons(app) == {"home-set-key": "Set OPENROUTER_API_KEY…"}
        changes = _tab_changes(app)
        await _press_fix(app, pilot, "home-set-key", how)
        assert changes == ["settings"]
        dialog = app.screen
        assert isinstance(dialog, FormModal)
        assert dialog.query_one("#key-name", Select).value == "OPENROUTER_API_KEY"
        # The Set key dialog opens in its hidden value field (#392).
        assert app.focused is dialog.query_one("#key-value", Input)
        settings = app.main.query_one("#settings-pane")
        main_focus = app.main.focused
        assert main_focus is not None
        assert settings in main_focus.ancestors
        await pilot.press("escape")
        await _settle(app, pilot)
        assert changes == ["settings"]
        assert _active(app) == "settings"
        assert app.focused is settings.query_one("#settings-list")

    _drive(lambda: _with_env(ready()), scenario)


@pytest.mark.parametrize("how", ["keyboard", "click"])
def test_the_fix_button_opens_the_tab_otherwise(how: str) -> None:
    state = ready()
    failing = dataclasses.replace(state, report=_report(state, keys=doctor.FAIL))

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert _fix_buttons(app) == {"home-open": f"Open {home.TAB_LABELS['diagnostics']}"}
        changes = _tab_changes(app)
        await _press_fix(app, pilot, "home-open", how)
        assert changes == ["diagnostics"]
        assert isinstance(app.focused, Tabs)

    _drive(lambda: failing, scenario)


# Leaving Home by a digit or Enter on a row, wherever Home's focus is: one tab change, and no
# hidden Home widget keeps the focus or pulls Home back.
@pytest.mark.parametrize("focus", ["home-rows", "home-reload", "home-set-key"])
def test_leaving_home_changes_the_tab_once(focus: str) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("2", "1")  # the Settings pane built, as after a visit
        await _settle(app, pilot)
        _home(app).query_one(f"#{focus}").focus()
        await _settle(app, pilot)
        changes = _tab_changes(app)
        await pilot.press("2")
        await _settle(app, pilot)
        assert changes == ["settings"]
        assert app.focused is app.main.query_one("#settings-list")
        changes.clear()
        await pilot.press("1")
        await _settle(app, pilot)
        assert changes == ["home"]
        assert app.focused is _listing(app)
        changes.clear()
        _on_row(app, "Output")
        await pilot.press("enter")
        await _settle(app, pilot)
        assert changes == ["settings"]
        assert app.focused is app.main.query_one("#settings-list")
        changes.clear()
        await pilot.click("#--content-tab-home")
        await _settle(app, pilot)
        assert changes == ["home"]
        assert app.focused is _listing(app)

    _drive(lambda: _with_env(ready()), scenario)


# The Try tab (its draft) and the setup wizard over Home: still one tab change each way.
def test_try_tab_and_setup_wizard_change_the_tab_once() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        changes = _tab_changes(app)
        await pilot.press("7")
        await _settle(app, pilot)
        assert changes == ["try"]
        await pilot.press("escape", "1")
        await _settle(app, pilot)
        assert changes == ["try", "home"]
        assert app.focused is _listing(app)
        app.open_setup()
        await _settle(app, pilot)
        assert app.screen is not app.main
        await pilot.press("q")  # Quit setup (#393): opened from Home, back to Home
        await _settle(app, pilot)
        assert app.screen is app.main
        assert changes == ["try", "home"]
        assert app.main.focused is _listing(app)

    _drive(fixed_state, scenario)


# A hidden fix button whose fix is not the current one does nothing when called anyway.
def test_a_hidden_fix_button_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def never() -> deploy.Plan:
        raise AssertionError("the plan was read")

    monkeypatch.setattr(triggers_pane, "current_plan", never)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert _fix_buttons(app) == {}
        changes = _tab_changes(app)
        # Called directly: Button.press() itself skips a hidden button.
        pane = _home(app)
        for handler in (pane._fix_deploy, pane._fix_key, pane._fix_open):
            await handler()
            await _settle(app, pilot)
        assert changes == []
        assert app.screen is app.main

    _drive(ready, scenario)


# The highlighted row is all in the highlight's colour (#388's rule): its status word too.
def test_the_highlighted_rows_status_word_takes_the_highlights_colour() -> None:
    rows = home_rows(fixed_state())
    lines = home_pane.home_lines(rows, cursor="Triggers")
    styles = {}
    for row, line in zip(rows, lines, strict=True):
        word = next(iter(line.columns[0].cells))
        assert isinstance(word, Text)
        styles[row.label] = word.style
    assert styles["Triggers"] == ""
    assert styles["Checks"] != ""


def test_no_fix_button_when_ready_or_checking() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert _fix_buttons(app) == {}
        # Tab from the list goes straight to Setup…
        await pilot.press("tab")
        assert app.focused is _home(app).query_one("#home-setup", Button)

    _drive(ready, scenario)
    _drive(lambda: quick_of(fixed_state()), scenario)


def test_the_highlighted_row_and_the_focus_survive_a_reload() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        _on_row(app, "History")
        await pilot.press("down")
        assert _highlighted(app) == "Output"
        app.reload()  # the quick State, then the full one: each repaints Home
        await _settle(app, pilot)
        assert _highlighted(app) == "Output"
        assert app.focused is _listing(app)
        assert _fix_buttons(app) == {"home-deploy": "Deploy or repair…"}

    _drive(fixed_state, scenario)
