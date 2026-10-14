"""Home's rows, headline and the header's status pill (#112, Mockup B): pure functions of a
State, checked here without the interface. The Pilot tests in test_tui.py check that the
pane shows them."""

from __future__ import annotations

import dataclasses

import pytest
from test_tui_snapshots import fixed_state

from promptmend import deploy, doctor, update_check
from promptmend.config import OUTPUTS, ConfigLayers
from promptmend.tui import home
from promptmend.tui.home import READY, HomeRow, headline, home_rows, pill
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
    assert found["Checks"].text == "14 ok · 0 warn · 0 fail"


def _update(state: State, status: str, latest: str | None = "0.22.0") -> State:
    return dataclasses.replace(state, update=update_check.UpdateStatus(status, latest, "x"))


def test_version_row_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home, "__version__", "0.21.0")
    row = _rows(_update(ready(), update_check.LATEST, "0.21.0"))["Version"]
    assert (row.status, row.text, row.detail) == (doctor.OK, "0.21.0 · latest", "")


@pytest.mark.parametrize(
    ("status", "text"),
    [(update_check.UNKNOWN, "0.21.0"), (update_check.OFF, "0.21.0 · check off")],
)
def test_version_row_without_an_answer(
    monkeypatch: pytest.MonkeyPatch, status: str, text: str
) -> None:
    monkeypatch.setattr(home, "__version__", "0.21.0")
    row = _rows(_update(ready(), status, None))["Version"]
    assert (row.status, row.text) == (doctor.OK, text)


def test_a_newer_release_is_a_note_with_its_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home, "__version__", "0.21.0")
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
    assert checks.text == "11 ok · 2 warn · 1 fail"
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
