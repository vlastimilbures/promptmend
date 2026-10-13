"""setup_guide.py: what setup says, from settings and triggers alone (no Textual)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from promptmend import assets, deploy, setup_guide
from promptmend.commands import common
from promptmend.config import Settings
from promptmend.factory import PROVIDER_NAMES


def _settings(**kw: Any) -> Settings:
    return dataclasses.replace(common.load_layers()[1], **kw)


def test_steps_hide_earlier_unless_needed() -> None:
    assert "earlier" not in setup_guide.steps(False)
    assert setup_guide.steps(True)[1] == "earlier"


def test_provider_choices_name_their_triggers() -> None:
    choices = setup_guide.provider_choices(_settings(), assets.triggers())
    assert sorted(c.value for c in choices) == sorted(PROVIDER_NAMES)
    assert choices[0].value == "openrouter"
    by = {c.value: c for c in choices}
    assert "-i-" in by["openrouter"].detail
    assert "-il-" in by["ollama"].detail
    assert by["openrouter"].current


def test_provider_refused_while_local_only() -> None:
    choices = setup_guide.provider_choices(_settings(local_only=True), assets.triggers())
    assert "refused" in {c.value: c for c in choices}["openrouter"].detail


def test_profile_choices_mark_current() -> None:
    choices = setup_guide.profile_choices(_settings(profile="general"), [])
    assert {c.value for c in choices} >= {"default", "general"}
    assert [c.value for c in choices if c.current] == ["general"]


def test_key_rows_follow_the_triggers_not_only_the_provider() -> None:
    layers, _ = common.load_layers()
    rows = {
        r.name: r
        for r in setup_guide.key_rows(layers, _settings(provider="ollama"), assets.triggers())
    }
    assert rows["OPENROUTER_API_KEY"].needed
    assert "-i-" in rows["OPENROUTER_API_KEY"].triggers
    assert not rows["ANTHROPIC_API_KEY"].needed  # -ic- is commented out
    line = setup_guide.key_line(rows["ANTHROPIC_API_KEY"])
    assert line == "ANTHROPIC_API_KEY: not set; not needed by any active trigger"
    rows = {
        r.name: r
        for r in setup_guide.key_rows(layers, _settings(provider="anthropic"), assets.triggers())
    }
    assert rows["ANTHROPIC_API_KEY"].needed
    assert "bare `promptmend improve`" in setup_guide.key_line(rows["ANTHROPIC_API_KEY"])


def test_key_rows_local_only_needs_none() -> None:
    layers, _ = common.load_layers()
    rows = setup_guide.key_rows(layers, _settings(local_only=True), assets.triggers())
    assert not any(r.needed for r in rows)


def test_key_line_names_the_source() -> None:
    row = setup_guide.KeyRow("OPENROUTER_API_KEY", True, "file:/x/secrets.toml", ("-i-",), True)
    assert setup_guide.key_line(row) == "OPENROUTER_API_KEY: set (secrets.toml); needed by -i-"
    row = dataclasses.replace(row, source="env")
    assert "(environment variable)" in setup_guide.key_line(row)


def test_file_states_cover_every_deploy_state() -> None:
    states = {deploy.MISSING, deploy.IN_SYNC, deploy.STALE, deploy.MODIFIED, deploy.FOREIGN}
    assert set(setup_guide.FILE_STATES) == states


def _plan(tmp_path: Path) -> deploy.Plan:
    (tmp_path / "match").mkdir()
    return deploy.plan(
        tmp_path, deploy.launcher_text("/bin/pm"), deploy.Manifest(tmp_path / "manifest.json")
    )


def test_deploy_summary_and_done_rows(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    assert setup_guide.deploy_summary(plan) == (
        "Install writes 3 file(s) and then restarts Espanso."
    )
    assert all(words.startswith("not installed") for _, words in setup_guide.file_rows(plan))
    key = setup_guide.KeyRow("OPENROUTER_API_KEY", False, None, ("-i-",), True)
    unused = setup_guide.KeyRow("ANTHROPIC_API_KEY", False, None, (), False)
    rows = setup_guide.done_rows(_settings(), [key, unused], plan, None)
    assert [(r.ok, r.label) for r in rows] == [
        (True, "Provider"),
        (False, "OPENROUTER_API_KEY"),
        (False, "Match files"),
        (False, "Test"),
    ]
    rows = setup_guide.done_rows(_settings(), [], None, False)
    assert rows[1].text == "Espanso's folder was not found"
    assert rows[2].text.startswith("failed")


def test_done_rows_count_a_kept_edited_file_as_installed(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan.steps = [dataclasses.replace(s, state=deploy.MODIFIED) for s in plan.steps]
    assert setup_guide.deploy_summary(plan).startswith("Nothing to write")
    match = setup_guide.done_rows(_settings(), [], plan, None)[1]
    assert (match.ok, match.text) == (
        True,
        f"installed in {tmp_path / 'match'}; 3 file(s) you changed were kept",
    )


def test_done_rows_say_a_skipped_step_is_skipped() -> None:
    rows = setup_guide.done_rows(_settings(), [], None, None, skipped=["espanso", "test"])
    assert [(r.ok, r.label, r.text) for r in rows[1:]] == [
        (True, "Match files", "skipped (--no-deploy)"),
        (True, "Test", "skipped (--no-smoke-test)"),
    ]


def test_done_rows_point_a_missing_key_at_the_legacy_env_file() -> None:
    key = setup_guide.KeyRow("OPENROUTER_API_KEY", False, None, ("-i-",), True)
    rows = setup_guide.done_rows(_settings(), [key], None, None, "/x/.env")
    assert rows[1].text == "not set: add OPENROUTER_API_KEY= to /x/.env"
