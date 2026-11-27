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


def _trigger(name: str, provider: str) -> assets.Trigger:
    return assets.Trigger(name, "x.yml", True, "improve", provider)


# Another match set than the shipped one: a label typed in rather than derived fails here.
OTHER = [_trigger("-a-", "anthropic"), _trigger("-o-", "openai"), _trigger("-l-", "lmstudio")]


def test_use_choices_name_the_triggers_from_the_match_files() -> None:
    choices = setup_guide.use_choices(assets.triggers(), _settings())
    assert [c.value for c in choices] == list(setup_guide.USES)
    by = {c.value: c for c in choices}
    assert (by["cloud"].title, by["cloud"].detail) == (
        "Cloud (OpenRouter)",
        "-i- -iok- -ip- -if- · the draft leaves; needs a key",
    )
    assert (by["local"].title, by["local"].detail) == (
        "Local (Ollama, LM Studio)",
        "-il- -ilm- · PROMPT_LOCAL_ONLY=true: the rest refuse",
    )
    assert (by["both"].title, by["both"].detail) == (
        "Both",
        "every trigger above; needs the cloud key",
    )
    # Cloud and both are not stored: none is current unless PROMPT_LOCAL_ONLY is true.
    assert not any(c.current for c in choices)
    choices = setup_guide.use_choices(assets.triggers(), _settings(local_only=True))
    assert [c.value for c in choices if c.current] == ["local"]
    other = {c.value: c for c in setup_guide.use_choices(OTHER, _settings())}
    assert other["cloud"].title == "Cloud (Anthropic, OpenAI)"
    assert other["cloud"].detail.startswith("-a- -o- · ")
    assert other["local"].title == "Local (LM Studio)"
    assert other["local"].detail.startswith("-l- · ")


def test_local_note_names_what_is_refused_and_what_to_use() -> None:
    assert setup_guide.local_note(assets.triggers(), _settings()) == (
        "Local saves PROMPT_LOCAL_ONLY=true: -i-, -iok-, -ip- and -if- then paste a refusal "
        "instead of sending your draft to OpenRouter. Use -il- (Ollama) or -ilm- (LM Studio). "
        "Cloud or Both turns it off again."
    )
    assert setup_guide.local_note(OTHER, _settings(), headless=True) == (
        "Local saves PROMPT_LOCAL_ONLY=true: -a- and -o- then paste a refusal instead of "
        "sending your draft to Anthropic and OpenAI. Use -l- (LM Studio). `--use cloud` or "
        "`--use both` turns it off again."
    )


def test_local_note_follows_where_a_local_server_is() -> None:
    """A non-loopback Ollama leaves the machine (factory.leaves_machine()), so local refuses
    -il- too and the note does not offer it."""
    settings = _settings(ollama_base_url="http://192.0.2.7:11434")
    note = setup_guide.local_note(assets.triggers(), settings)
    assert (
        "-if- and -il- then paste a refusal instead of sending your draft to OpenRouter and "
        "Ollama." in note
    )
    assert " Use -ilm- (LM Studio). " in note
    by = {c.value: c for c in setup_guide.use_choices(assets.triggers(), settings)}
    assert by["cloud"].title == "Cloud (OpenRouter, Ollama)"
    assert by["local"].title == "Local (LM Studio)"
    assert by["local"].detail.startswith("-ilm- · ")
    every_one = _settings(
        ollama_base_url="http://192.0.2.7:11434", lmstudio_base_url="http://192.0.2.7:1234/v1"
    )
    note = setup_guide.local_note(assets.triggers(), every_one)
    assert (
        " No trigger stays on this computer (OLLAMA_BASE_URL points elsewhere; "
        "LMSTUDIO_BASE_URL points elsewhere). " in note
    )
    assert "Use " not in note
    by = {c.value: c for c in setup_guide.use_choices(assets.triggers(), every_one)}
    assert by["local"].title == "Local (none stays on this computer)"
    cloud_model = setup_guide.local_note(
        [_trigger("-l-", "ollama")], _settings(ollama_model="gpt-oss:120b-cloud")
    )
    assert "(OLLAMA_MODEL is a cloud model)" in cloud_model
    assert setup_guide.local_note([], _settings()).startswith(
        "Local saves PROMPT_LOCAL_ONLY=true. No trigger stays on this computer. "
    )


def test_smoke_runs_follow_the_answer_not_the_provider() -> None:
    triggers = assets.triggers()
    settings = _settings(provider="anthropic")
    cloud = setup_guide.SmokeRun("-i-", "openrouter")
    local = setup_guide.SmokeRun("-il-", "ollama")
    assert setup_guide.smoke_runs("cloud", triggers, settings) == [cloud]
    assert setup_guide.smoke_runs("local", triggers, settings) == [local]
    assert setup_guide.smoke_runs("both", triggers, settings) == [cloud, local]
    # No trigger on that path: the cloud path itself, and no local run at all.
    assert setup_guide.smoke_runs("both", [], settings) == [
        setup_guide.SmokeRun(None, "openrouter"),
        setup_guide.SmokeRun(None, None),
    ]


def test_the_local_run_is_one_that_local_leaves_usable() -> None:
    """The local run follows factory.leaves_machine(), as PROMPT_LOCAL_ONLY and the note do:
    a remote Ollama or an Ollama cloud model is skipped for -ilm-, never tested as -il-."""
    triggers = assets.triggers()
    lmstudio = [setup_guide.SmokeRun("-ilm-", "lmstudio")]
    remote = _settings(ollama_base_url="https://ollama.example.com")
    assert setup_guide.smoke_runs("local", triggers, remote) == lmstudio
    cloud_model = _settings(ollama_model="gpt-oss:120b-cloud")
    assert setup_guide.smoke_runs("local", triggers, cloud_model) == lmstudio
    nothing = _settings(
        ollama_base_url="https://ollama.example.com", lmstudio_base_url="https://lm.example.com/v1"
    )
    runs = setup_guide.smoke_runs("local", triggers, nothing)
    assert runs == [setup_guide.SmokeRun(None, None)]
    assert setup_guide.smoke_intro(runs).startswith("Runs nothing on the local path (no trigger")
    assert setup_guide.smoke_result(runs[0], True, "") == (
        f"local: not run: {setup_guide.NO_LOCAL_TRIGGER}"
    )


def test_local_note_says_what_is_true_now() -> None:
    triggers = assets.triggers()
    assert setup_guide.local_note(triggers, _settings()).startswith(
        "Local saves PROMPT_LOCAL_ONLY=true: -i-, -iok-, -ip- and -if- then paste"
    )
    already = setup_guide.local_note(triggers, _settings(local_only=True), already=True)
    assert already.startswith("PROMPT_LOCAL_ONLY=true is set: -i-, -iok-, -ip- and -if- paste")
    assert "Local saves" not in already
    legacy = setup_guide.local_note(triggers, _settings(), legacy="/x/.env")
    assert legacy.startswith(
        "Local needs PROMPT_LOCAL_ONLY=true, which this setup cannot save: add it to /x/.env. "
        "Then -i-, -iok-, -ip- and -if- paste a refusal"
    )
    assert "Cloud or both" not in legacy
    assert setup_guide.local_note([], _settings(), already=True).startswith(
        "PROMPT_LOCAL_ONLY=true is set. No trigger"
    )
    assert setup_guide.local_note([], _settings(), legacy="/x/.env") == (
        "Local needs PROMPT_LOCAL_ONLY=true, which this setup cannot save: add it to /x/.env. "
        "No trigger stays on this computer."
    )


def test_a_remote_ollama_is_not_called_a_cloud_api() -> None:
    settings = _settings(ollama_base_url="https://ollama.example.com")
    cloud = setup_guide.use_choices(assets.triggers(), settings)[0]
    assert (cloud.title, cloud.detail) == (
        "Cloud (OpenRouter, Ollama)",
        "-i- -iok- -ip- -if- -il- · the draft leaves; needs a key",
    )


def test_next_step_names_a_trigger_the_answer_leaves_usable() -> None:
    triggers = assets.triggers()
    assert "type -i- and wait without typing or switching windows. The rewrite replaces the" in (
        setup_guide.next_step("cloud", triggers, _settings())
    )
    assert "type -i- " in setup_guide.next_step("both", triggers, _settings())
    local = setup_guide.next_step("local", triggers, _settings(local_only=True))
    assert "type -il- " in local
    assert "-i- " not in local
    remote = _settings(local_only=True, ollama_base_url="https://ollama.example.com")
    assert "type -ilm- " in setup_guide.next_step("local", triggers, remote)
    clipboard = setup_guide.next_step("cloud", triggers, _settings(output="clipboard"))
    assert "replaces the trigger" not in clipboard
    assert "The rewrite then waits on the clipboard: paste it where you want it." in clipboard
    nothing = _settings(
        ollama_base_url="https://ollama.example.com", lmstudio_base_url="https://lm.example.com/v1"
    )
    assert setup_guide.next_step("local", triggers, nothing).startswith("Nothing to try yet: ")


def test_use_problem_names_an_environment_variable() -> None:
    assert setup_guide.use_problem(_settings(local_only=True), "local") is None
    assert setup_guide.use_problem(_settings(), "cloud") is None
    assert setup_guide.use_problem(_settings(), "local", from_env=True) == (
        "local, but the environment variable PROMPT_LOCAL_ONLY overrides the saved value, so "
        "the cloud triggers still send drafts off this computer wherever it is set: remove it "
        "from the environment"
    )
    assert setup_guide.use_problem(_settings(local_only=True), "both", from_env=True) == (
        "both, but the environment variable PROMPT_LOCAL_ONLY overrides the saved value, so "
        "the cloud triggers refuse wherever it is set: remove it from the environment"
    )
    row = setup_guide.done_rows(_settings(), "local", [], None, None, from_env=True)[0]
    assert not row.ok
    assert "config set" not in row.text
    assert row.text.endswith("remove it from the environment")


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
    # Its promise: no provider is called and the real key is never sent.
    assert setup_guide.smoke_intro([cloud]).endswith(
        "against a stub server on 127.0.0.1 that answers like the provider. No provider is "
        "called and your real key is never sent; it checks that the CLI starts and reads its "
        "settings."
    )
    assert setup_guide.smoke_result(cloud, True, "ok") == "-i- (openrouter): passed: ok"
    assert setup_guide.smoke_result(local, False, "down") == "-il- (ollama): failed: down"
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
        "local (PROMPT_LOCAL_ONLY=true: the cloud triggers refuse)",
    )
    # Not saved (legacy mode, or an environment variable outranks config.toml): said, with
    # the fix, so no cloud trigger calls out unannounced.
    assert first(_settings(), "local") == (
        False,
        "local, but PROMPT_LOCAL_ONLY is off, so the cloud triggers still send drafts off "
        "this computer: `promptmend config set PROMPT_LOCAL_ONLY true`",
    )
    assert first(_settings(), "local", "/x/.env")[1].endswith(
        "add PROMPT_LOCAL_ONLY=true to /x/.env"
    )
    assert first(_settings(local_only=True), "cloud") == (
        False,
        "cloud, but PROMPT_LOCAL_ONLY=true makes the cloud triggers refuse: "
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
