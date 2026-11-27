"""setup_flow.py (#282): the decisions and writes both setup flows share, driven directly (the
flows themselves are tested in test_commands.py and test_tui_setup.py)."""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

from promptmend import (
    config,
    config_files,
    config_store,
    deploy,
    settings_service,
    setup_flow,
    setup_guide,
)

KEY = "sk-or-v1-" + "ab12" * 16


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Saved mode: config.toml and the secrets file are read (conftest's per-test dir)."""
    monkeypatch.delenv("PROMPTMEND_ENV")
    return config.user_config_dir()


def _settings_file() -> Path:
    return config_store.read_settings().path


def test_history_line() -> None:
    assert setup_guide.history_line(False) == "Usage history is off; nothing is recorded."
    assert setup_guide.history_line(True).startswith("Usage history is on: metadata only")


def test_storage_in_legacy_mode() -> None:
    where = setup_flow.storage()
    assert where.legacy is not None
    assert where.unread is None
    assert not where.writable


def test_storage_names_an_unread_dotenv(
    saved: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = tmp_path / "old" / ".env"
    monkeypatch.setattr(config, "unread_env_files", lambda: [old])
    where = setup_flow.storage()
    assert where.writable
    assert where.unread is not None
    assert where.unread.startswith(f"{old} is no longer read")


def test_save_choice_saves_and_unsets_a_default(saved: Path) -> None:
    assert setup_flow.save_choice("PROMPT_PROVIDER", "openrouter") == (None, [])
    message, argv = setup_flow.save_choice("PROMPT_PROVIDER", "ollama")
    assert message is not None
    assert message.startswith("PROMPT_PROVIDER=ollama saved in ")
    assert argv == ["set", "PROMPT_PROVIDER", "ollama"]
    assert config_store.read_settings().values == {"PROMPT_PROVIDER": "ollama"}
    message, argv = setup_flow.save_choice("PROMPT_PROVIDER", "openrouter")
    assert message is not None
    assert "its default, openrouter, applies" in message
    assert argv == ["unset", "PROMPT_PROVIDER"]
    assert config_store.read_settings().values == {}
    assert setup_flow.choice_command("PROMPT_PROFILE", "default") == ["unset", "PROMPT_PROFILE"]
    assert setup_flow.choice_command("PROMPT_PROFILE", "general") == [
        "set",
        "PROMPT_PROFILE",
        "general",
    ]


def test_local_only_follows_the_answer(saved: Path) -> None:
    """#390: local saves PROMPT_LOCAL_ONLY=true; cloud and both remove it again, so its
    default applies; an unchanged answer writes nothing."""
    settings = settings_service.load_layers()[1]
    assert setup_flow.current_use(settings) == "cloud"
    assert [setup_flow.local_only_value(use) for use in setup_guide.USES] == [
        "false",
        "true",
        "false",
    ]
    assert setup_flow.save_choice("PROMPT_LOCAL_ONLY", "false") == (None, [])
    message, argv = setup_flow.save_choice("PROMPT_LOCAL_ONLY", "true")
    assert message is not None
    assert message.startswith("PROMPT_LOCAL_ONLY=true saved in ")
    assert argv == ["set", "PROMPT_LOCAL_ONLY", "true"]
    settings = settings_service.load_layers()[1]
    assert settings.local_only
    assert setup_flow.current_use(settings) == "local"
    assert setup_flow.save_choice("PROMPT_LOCAL_ONLY", "true") == (None, [])
    message, argv = setup_flow.save_choice("PROMPT_LOCAL_ONLY", "false")
    assert message is not None
    assert "its default, false, applies" in message
    assert argv == ["unset", "PROMPT_LOCAL_ONLY"]
    assert "PROMPT_LOCAL_ONLY" not in config_store.read_settings().table


def test_output_choice_saves_and_unsets(saved: Path) -> None:
    message, argv = setup_flow.save_choice("PROMPT_OUTPUT", "clipboard")
    assert message is not None
    assert argv == ["set", "PROMPT_OUTPUT", "clipboard"]
    assert setup_flow.current_choice("PROMPT_OUTPUT") == "clipboard"
    _, argv = setup_flow.save_choice("PROMPT_OUTPUT", "paste")
    assert argv == ["unset", "PROMPT_OUTPUT"]
    assert config_store.read_settings().values == {}


def _file() -> dict[str, object]:
    return {k: v for k, v in config_store.read_settings().table.items() if k != "config_version"}


def test_save_choice_writes_the_answer_whatever_the_environment_says(
    saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.toml is what the deployed triggers read (Espanso does not start from the
    shell): an environment variable that already says true must not keep "local" unsaved."""
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")
    message, argv = setup_flow.save_choice("PROMPT_LOCAL_ONLY", "true")
    assert message is not None
    assert message.startswith("PROMPT_LOCAL_ONLY=true saved in ")
    assert "the environment variable PROMPT_LOCAL_ONLY is set and overrides" in message
    assert argv == ["set", "PROMPT_LOCAL_ONLY", "true"]
    assert _file() == {"PROMPT_LOCAL_ONLY": True}
    # Held by the file now: unchanged.
    assert setup_flow.save_choice("PROMPT_LOCAL_ONLY", "true") == (None, [])


def test_save_choice_removes_a_saved_value_the_environment_hides(
    saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_store.save_settings(config_store.read_settings(), {"PROMPT_LOCAL_ONLY": "true"})
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "false")
    message, argv = setup_flow.save_choice("PROMPT_LOCAL_ONLY", "false")
    assert message is not None
    assert message.startswith("PROMPT_LOCAL_ONLY removed from ")
    assert argv == ["unset", "PROMPT_LOCAL_ONLY"]
    assert _file() == {}


def test_save_choice_never_pins_a_default(saved: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A default answer removes the file's entry, never writes it, even while an environment
    variable makes another value apply (a pinned default would not beat it either)."""
    for name, default, other in (
        ("PROMPT_LOCAL_ONLY", "false", "true"),
        ("PROMPT_OUTPUT", "paste", "clipboard"),
        ("PROMPT_PROFILE", "default", "general"),
        ("PROMPT_PROVIDER", "openrouter", "ollama"),
    ):
        monkeypatch.setenv(name, other)
        assert setup_flow.save_choice(name, default) == (None, [])
        config_store.save_settings(config_store.read_settings(), {name: other})
        message, argv = setup_flow.save_choice(name, default)
        assert argv == ["unset", name]
        assert message is not None
        assert "applies" not in message
        assert f"the environment variable {name} is set and overrides" in message
        assert _file() == {}


def test_key_provider_follows_the_answer() -> None:
    settings = settings_service.load_layers()[1]
    assert setup_flow.key_provider("local", settings) is None
    assert setup_flow.key_provider("cloud", settings) == "openrouter"
    assert setup_flow.key_provider("both", settings) == "openrouter"


def test_save_choices_writes_an_empty_settings_file_once(saved: Path) -> None:
    lines: list[str] = []
    defaults = [("PROMPT_PROFILE", "default"), ("PROMPT_OUTPUT", "paste")]
    first = setup_flow.save_choices(defaults, lines.append)
    assert first == setup_flow.ChoicesSaved(created=_settings_file())
    assert config_files.is_file(_settings_file())
    assert setup_flow.save_choices(defaults) == setup_flow.ChoicesSaved()
    changed = setup_flow.save_choices(
        [("PROMPT_LOCAL_ONLY", "true"), ("PROMPT_PROFILE", "general")], lines.append
    )
    assert changed.created is None
    assert changed.error is None
    assert len(changed.messages) == 2
    assert lines == list(changed.messages)


def test_save_choices_stops_at_the_first_error(saved: Path) -> None:
    result = setup_flow.save_choices(
        [("PROMPT_OUTPUT", "clipboard"), ("PROMPT_PROFILE", "sk-or-v1-" + "cd34" * 16)]
    )
    assert result.error is not None
    assert "looks like a key" in result.error
    assert len(result.messages) == 1  # the output was saved before


def test_save_choices_reports_a_failed_settings_file(
    saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> None:
        raise config_store.ConfigStoreError("read-only")

    monkeypatch.setattr(config_store, "save_settings", refuse)
    assert setup_flow.save_choices([("PROMPT_PROFILE", "default")]).error == "read-only"


def test_choice_value_and_profile() -> None:
    settings = settings_service.load_layers()[1]
    assert setup_flow.choice_value("PROMPT_PROVIDER", settings) == settings.provider
    local = dataclasses.replace(settings, local_only=True)
    assert setup_flow.choice_value("PROMPT_LOCAL_ONLY", local) == "true"
    setup_flow.check_profile("general", ())
    with pytest.raises(ValueError, match="nosuch"):
        setup_flow.check_profile("nosuch", ())


def test_keys(saved: Path) -> None:
    assert setup_flow.stdin_key("openrouter") == "OPENROUTER_API_KEY"
    assert setup_flow.stdin_key("ollama") is None
    layers, settings = settings_service.load_layers()
    rows = setup_flow.needed_keys(layers, settings, "cloud")
    assert [row.name for row in rows] == ["OPENROUTER_API_KEY"]
    assert all(row.needed for row in rows)
    # The keys follow the answer, not an effective PROMPT_LOCAL_ONLY that disagrees with it.
    assert setup_flow.needed_keys(layers, settings, "local") == []
    local = dataclasses.replace(settings, local_only=True)
    assert [row.name for row in setup_flow.needed_keys(layers, local, "both")] == [
        "OPENROUTER_API_KEY"
    ]
    message = setup_flow.save_keys([("OPENROUTER_API_KEY", KEY)])
    assert message.startswith("OPENROUTER_API_KEY saved")
    assert KEY not in message
    assert settings_service.load_layers()[0].entries["OPENROUTER_API_KEY"].value == KEY
    assert not _settings_file().exists()  # a key never goes in config.toml


def test_save_key_refused_in_legacy_mode() -> None:
    with pytest.raises(setup_flow.KEY_ERRORS):
        setup_flow.save_key("OPENROUTER_API_KEY", KEY)


def test_read_plan_install_and_in_sync(tmp_path: Path) -> None:
    root = tmp_path / "espanso"
    (root / "match").mkdir(parents=True)
    plan, fallback = setup_flow.read_plan(str(root), "/bin/pm")
    assert fallback is None
    assert not setup_flow.in_sync(plan)
    outcome = setup_flow.install(plan)
    assert outcome.changed
    plan, _ = setup_flow.read_plan(str(root), "/bin/pm")
    assert setup_flow.in_sync(plan)
    # Another folder's entries, its folder gone: only those are left to drop.
    old = tmp_path / "old-espanso"
    (old / "match").mkdir(parents=True)
    setup_flow.install(setup_flow.read_plan(str(old), "/bin/pm")[0])
    shutil.rmtree(old)
    plan, _ = setup_flow.read_plan(str(root), "/bin/pm")
    assert plan.only_forgets
    assert setup_flow.in_sync(plan)
    assert setup_flow.forget(plan).lines
    assert setup_flow.read_plan(str(root), "/bin/pm")[0].is_noop


def test_read_plan_refuses_a_key_in_an_option(tmp_path: Path) -> None:
    # settings_service.refuse_key()'s usage error; `setup --espanso-dir`/`--launcher` give its
    # text as Click's (commands.common.no_key()).
    with pytest.raises(settings_service.ServiceError, match="--espanso-dir looks like a key") as e:
        setup_flow.read_plan(KEY, "/bin/pm")
    assert e.value.usage
    with pytest.raises(settings_service.ServiceError, match="--launcher looks like a key"):
        setup_flow.read_plan(str(tmp_path), KEY)


def test_read_plan_finds_espanso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        deploy, "locate_espanso_dir", lambda: deploy.EspansoDir(tmp_path, "no answer")
    )
    monkeypatch.setattr(deploy, "resolve_launcher", lambda: deploy.Launcher(Path("/bin/pm"), "uv"))
    plan, fallback = setup_flow.read_plan(None, None)
    assert plan.espanso_dir == tmp_path.resolve()
    assert fallback == "no answer"


def test_forget_refuses_a_plan_that_writes(tmp_path: Path) -> None:
    root = tmp_path / "espanso"
    (root / "match").mkdir(parents=True)
    plan, _ = setup_flow.read_plan(str(root), "/bin/pm")
    with pytest.raises(ValueError, match="only drops gone manifest entries"):
        setup_flow.forget(plan)
    assert list((root / "match").iterdir()) == []


def test_setup_reports_a_failed_settings_step(saved: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: the first choice's line on stdout, then the failure on stderr, exit 1."""
    from typer.testing import CliRunner

    from promptmend.cli import app

    real = config_store.save_settings
    writes: list[object] = []

    def second_fails(*args: object, **kwargs: object) -> config_store.SavedSettings:
        writes.append(args)
        if len(writes) == 2:
            raise config_store.ConfigStoreError("disk full")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(config_store, "save_settings", second_fails)
    args = ["--use", "local", "--profile", "general", "--no-deploy", "--no-smoke-test"]
    result = CliRunner().invoke(app, ["setup", "--non-interactive", *args])
    assert result.exit_code == 1, result.output
    assert "  PROMPT_LOCAL_ONLY=true saved in " in result.stdout
    assert "PROMPT_PROFILE" not in result.stdout.split("saved in ")[1].split("API keys")[0]
    assert "  failed: disk full" in result.stderr
    assert "settings failed" in result.stderr
