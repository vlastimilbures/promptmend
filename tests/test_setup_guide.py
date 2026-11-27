"""setup_guide.py: what setup says, from settings and triggers alone (no Textual)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from promptmend import assets, deploy, settings_service, setup_guide
from promptmend.config import Settings


def _settings(**kw: Any) -> Settings:
    return dataclasses.replace(settings_service.load_layers()[1], **kw)


def test_unread_env_note(tmp_path: Path) -> None:
    assert setup_guide.unread_env_note([]) is None
    note = setup_guide.unread_env_note([tmp_path / ".env"])
    assert note is not None
    assert note.startswith(f"{tmp_path / '.env'} is no longer read: choose its settings again")


def test_steps_ask_cloud_or_local_first_and_never_the_provider() -> None:
    assert list(setup_guide.STEPS) == [
        "welcome",
        "use",
        "keys",
        "profile",
        "output",
        "espanso",
        "test",
        "done",
    ]
    assert setup_guide.STEPS["use"] == "Cloud or local"
    assert "provider" not in setup_guide.STEPS


def test_use_choices_name_the_triggers_from_the_match_files() -> None:
    choices = setup_guide.use_choices(assets.triggers(), _settings())
    assert [c.value for c in choices] == list(setup_guide.USES)
    by = {c.value: c for c in choices}
    assert by["cloud"].title == "Cloud: an OpenRouter key"
    assert by["cloud"].detail.startswith("-i- -iok- -ip- -if- · ")
    assert by["local"].title == "Local: Ollama or LM Studio"
    assert by["local"].detail == (
        "-il- -ilm- · PROMPT_LOCAL_ONLY=true: the cloud triggers then refuse"
    )
    # Cloud and both are not stored: none is current unless PROMPT_LOCAL_ONLY is true.
    assert not any(c.current for c in choices)
    choices = setup_guide.use_choices(assets.triggers(), _settings(local_only=True))
    assert [c.value for c in choices if c.current] == ["local"]


def test_local_note_names_what_is_refused_and_what_to_use() -> None:
    note = setup_guide.local_note(assets.triggers(), _settings())
    assert note.startswith(
        "Local saves PROMPT_LOCAL_ONLY=true: -i-, -iok-, -ip-, -if- then answer with a "
        "refusal marker instead of calling OpenRouter. The triggers to use are -il- (Ollama) "
        "or -ilm- (LM Studio)."
    )
    assert "an Ollama cloud model" in note
    assert "cannot see a local server that passes the draft on" in note


def test_local_note_follows_where_a_local_server_is() -> None:
    """A non-loopback Ollama leaves the machine (factory.leaves_machine()), so local refuses
    -il- too and the note does not offer it."""
    settings = _settings(ollama_base_url="http://192.0.2.7:11434")
    note = setup_guide.local_note(assets.triggers(), settings)
    assert "-if-, -il- then answer with a refusal marker instead of calling OpenRouter, " in note
    assert "Ollama" in note
    assert "The triggers to use are -ilm- (LM Studio)." in note
    by = {c.value: c for c in setup_guide.use_choices(assets.triggers(), settings)}
    assert by["local"].detail.startswith("-ilm- · ")
    every_one = _settings(
        ollama_base_url="http://192.0.2.7:11434", lmstudio_base_url="http://192.0.2.7:1234/v1"
    )
    note = setup_guide.local_note(assets.triggers(), every_one)
    assert "The triggers to use are none: no trigger stays on this computer." in note
    none_away = setup_guide.local_note([], _settings())
    assert none_away.startswith("Local saves PROMPT_LOCAL_ONLY=true. The triggers to use are none")


def test_smoke_runs_follow_the_answer_not_the_provider() -> None:
    triggers = assets.triggers()
    settings = _settings(provider="anthropic")
    cloud = setup_guide.SmokeRun("-i-", "openrouter")
    local = setup_guide.SmokeRun("-il-", "ollama")
    assert setup_guide.smoke_runs("cloud", triggers, settings) == [cloud]
    assert setup_guide.smoke_runs("local", triggers, settings) == [local]
    assert setup_guide.smoke_runs("both", triggers, settings) == [cloud, local]
    # No trigger on that path: the path itself, with no trigger to name.
    assert setup_guide.smoke_runs("both", [], settings) == [
        setup_guide.SmokeRun(None, "openrouter"),
        setup_guide.SmokeRun(None, "ollama"),
    ]


def test_smoke_intro_and_result_name_the_trigger() -> None:
    cloud = setup_guide.SmokeRun("-i-", "openrouter")
    local = setup_guide.SmokeRun("-il-", "ollama")
    assert setup_guide.smoke_intro([cloud, local]).startswith(
        "Runs `promptmend improve --provider openrouter` (the path -i- takes) and "
        "`promptmend improve --provider ollama` (the path -il- takes) against a stub server "
        "on 127.0.0.1"
    )
    assert "(no active trigger takes it)" in setup_guide.smoke_intro(
        [setup_guide.SmokeRun(None, "ollama")]
    )
    assert setup_guide.smoke_result(cloud, True, "ok") == "-i- (openrouter): passed: ok"
    assert setup_guide.smoke_result(local, False, "down") == (
        "-il- (ollama): failed (improve --provider ollama): down"
    )
    assert setup_guide.smoke_result(setup_guide.SmokeRun(None, "ollama"), True, "ok") == (
        "ollama: passed: ok"
    )


def test_output_step() -> None:
    intro = setup_guide.output_intro(_settings())
    assert intro.startswith(
        "Espanso shows nothing while a rewrite runs (up to 30 s, 45 s on the pro tier) and "
        "pastes it wherever the focus is when it arrives;"
    )
    assert "up to 12.5 s, 60 s" in setup_guide.output_intro(
        _settings(timeout=12.5, pro_timeout=60.0)
    )
    choices = setup_guide.output_choices(_settings())
    assert [(c.value, c.current) for c in choices] == [("paste", True), ("clipboard", False)]
    choices = setup_guide.output_choices(_settings(output="clipboard"))
    assert [c.value for c in choices if c.current] == ["clipboard"]


def test_profile_choices_mark_current() -> None:
    choices = setup_guide.profile_choices(_settings(profile="general"), [])
    assert {c.value for c in choices} >= {"default", "general"}
    assert [c.value for c in choices if c.current] == ["general"]


def test_key_rows_follow_the_triggers_not_only_the_provider() -> None:
    layers, _ = settings_service.load_layers()
    rows = {
        r.name: r
        for r in setup_guide.key_rows(layers, _settings(provider="ollama"), assets.triggers())
    }
    assert rows["OPENROUTER_API_KEY"].needed
    assert "-i-" in rows["OPENROUTER_API_KEY"].triggers
    assert not rows["ANTHROPIC_API_KEY"].needed  # -ic- is commented out
    assert not rows["OPENAI_API_KEY"].needed  # -io- too
    line = setup_guide.key_line(rows["ANTHROPIC_API_KEY"])
    assert line == "ANTHROPIC_API_KEY: not set; not needed by any active trigger"
    rows = {
        r.name: r
        for r in setup_guide.key_rows(layers, _settings(provider="anthropic"), assets.triggers())
    }
    assert rows["ANTHROPIC_API_KEY"].needed
    assert "bare `promptmend improve`" in setup_guide.key_line(rows["ANTHROPIC_API_KEY"])


def test_key_rows_local_only_needs_none() -> None:
    layers, _ = settings_service.load_layers()
    rows = setup_guide.key_rows(layers, _settings(local_only=True), assets.triggers())
    assert not any(r.needed for r in rows)
    # The refused triggers are named as refused, never as needing the key.
    line = setup_guide.key_line(next(r for r in rows if r.name == "OPENROUTER_API_KEY"))
    assert line == (
        "OPENROUTER_API_KEY: not set; not needed: PROMPT_LOCAL_ONLY=true refuses -i-, -iok-, "
        "-ip-, -if-"
    )
    assert setup_guide.no_key_line(_settings(local_only=True)).startswith(
        "No key needed: PROMPT_LOCAL_ONLY=true refuses"
    )
    assert setup_guide.no_key_line(_settings()).startswith("No key needed: every active trigger")


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
    rows = setup_guide.done_rows(_settings(), "cloud", [key, unused], plan, None)
    assert [(r.ok, r.label, r.text) for r in rows[:3]] == [
        (True, "Triggers", "cloud"),
        (True, "Profile", "default"),
        (True, "Output", "paste"),
    ]
    assert [(r.ok, r.label) for r in rows[3:]] == [
        (False, "OPENROUTER_API_KEY"),
        (False, "Match files"),
        (False, "Test"),
    ]
    rows = setup_guide.done_rows(_settings(), "both", [], None, False)
    assert rows[3].text == "Espanso's folder was not found"
    assert rows[4].text.startswith("failed")


def test_done_rows_check_the_answer_against_local_only() -> None:
    def first(settings: Settings, use: str, legacy: str | None = None) -> tuple[bool, str]:
        row = setup_guide.done_rows(settings, use, [], None, None, legacy)[0]
        return row.ok, row.text

    assert first(_settings(local_only=True), "local") == (
        True,
        "local (PROMPT_LOCAL_ONLY=true refuses the cloud ones)",
    )
    # Not saved (legacy mode, or an environment variable outranks config.toml): said, with
    # the fix, so no cloud trigger calls out unannounced.
    assert first(_settings(), "local") == (
        False,
        "local, but PROMPT_LOCAL_ONLY is off, so the cloud ones still call out: "
        "`promptmend config set PROMPT_LOCAL_ONLY true`",
    )
    assert first(_settings(), "local", "/x/.env")[1].endswith(
        "add PROMPT_LOCAL_ONLY=true to /x/.env"
    )
    assert first(_settings(local_only=True), "cloud") == (
        False,
        "cloud, but PROMPT_LOCAL_ONLY=true refuses the cloud ones: "
        "`promptmend config unset PROMPT_LOCAL_ONLY`",
    )
    assert first(_settings(local_only=True), "both", "/x/.env")[1].endswith(
        "remove PROMPT_LOCAL_ONLY from /x/.env"
    )


def test_done_rows_count_a_kept_edited_file_as_installed(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan.steps = [dataclasses.replace(s, state=deploy.MODIFIED) for s in plan.steps]
    assert setup_guide.deploy_summary(plan).startswith("Nothing to write")
    match = setup_guide.done_rows(_settings(), "cloud", [], plan, None)[3]
    assert (match.ok, match.text) == (
        True,
        f"installed in {tmp_path / 'match'}; 3 file(s) you changed were kept",
    )


def test_done_rows_say_a_skipped_step_is_skipped() -> None:
    rows = setup_guide.done_rows(_settings(), "cloud", [], None, None, skipped=["espanso", "test"])
    assert [(r.ok, r.label, r.text) for r in rows[3:]] == [
        (True, "Match files", "skipped (--no-deploy)"),
        (True, "Test", "skipped (--no-smoke-test)"),
    ]


def test_done_rows_point_a_missing_key_at_the_legacy_env_file() -> None:
    key = setup_guide.KeyRow("OPENROUTER_API_KEY", False, None, ("-i-",), True)
    rows = setup_guide.done_rows(_settings(), "cloud", [key], None, None, "/x/.env")
    assert rows[3].text == "not set: add OPENROUTER_API_KEY= to /x/.env"
