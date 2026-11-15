"""setup_flow.py (#282): the decisions and writes both setup flows share, driven directly (the
flows themselves are tested in test_commands.py and test_tui_setup.py)."""

from __future__ import annotations

import ast
import dataclasses
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import typer

from promptmend import config, config_files, config_store, deploy, setup_flow, setup_guide
from promptmend.commands import common

KEY = "sk-or-v1-" + "ab12" * 16


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Saved mode: config.toml and the secret store are read (conftest's per-test dir)."""
    monkeypatch.delenv("PROMPTMEND_ENV")
    return config.user_config_dir()


def _settings_file() -> Path:
    return config_store.read_settings().path


def test_imports_neither_typer_nor_textual() -> None:
    tree = ast.parse(Path(setup_flow.__file__).read_text(encoding="utf-8"))
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    names |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not {n for n in names if n.split(".")[0] in ("typer", "textual", "click")}


def test_import_loads_no_textual() -> None:
    code = (
        "import sys, promptmend.setup_flow; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'textual'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == "[]"


def test_history_line() -> None:
    assert setup_guide.history_line(False) == "Usage history is off; nothing is recorded."
    assert setup_guide.history_line(True).startswith("Usage history is on: metadata only")


def test_storage_in_legacy_mode() -> None:
    where = setup_flow.storage()
    assert where.legacy is not None
    assert where.unread is None
    assert not where.writable


def test_storage_names_an_unread_dotenv(saved: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "unread_env_files", lambda: [Path("/old/.env")])
    where = setup_flow.storage()
    assert where.writable
    assert where.unread is not None
    assert where.unread.startswith("/old/.env is no longer read")


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


def test_save_choices_writes_an_empty_settings_file_once(saved: Path) -> None:
    lines: list[str] = []
    first = setup_flow.save_choices("openrouter", "default", lines.append)
    assert first == setup_flow.ChoicesSaved(created=_settings_file())
    assert config_files.is_file(_settings_file())
    assert setup_flow.save_choices("openrouter", "default") == setup_flow.ChoicesSaved()
    changed = setup_flow.save_choices("ollama", "general", lines.append)
    assert changed.created is None
    assert changed.error is None
    assert len(changed.messages) == 2
    assert lines == list(changed.messages)


def test_save_choices_stops_at_the_first_error(saved: Path) -> None:
    result = setup_flow.save_choices("ollama", "sk-or-v1-" + "cd34" * 16)
    assert result.error is not None
    assert "looks like a key" in result.error
    assert len(result.messages) == 1  # the provider was saved before


def test_save_choices_reports_a_failed_settings_file(
    saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> None:
        raise config_store.ConfigStoreError("read-only")

    monkeypatch.setattr(config_store, "save_settings", refuse)
    assert setup_flow.save_choices("openrouter", "default").error == "read-only"


def test_chosen_provider_and_profile() -> None:
    settings = common.load_layers()[1]
    assert setup_flow.chosen_provider(None, settings) == settings.provider
    assert setup_flow.chosen_provider("ollama", settings) == "ollama"
    with pytest.raises(ValueError, match="is not a provider; pass --provider"):
        setup_flow.chosen_provider(None, dataclasses.replace(settings, provider="nope"))
    setup_flow.check_profile("general", ())
    with pytest.raises(ValueError, match="nosuch"):
        setup_flow.check_profile("nosuch", ())


def test_keys(saved: Path) -> None:
    assert setup_flow.stdin_key("openrouter") == "OPENROUTER_API_KEY"
    assert setup_flow.stdin_key("ollama") is None
    rows = setup_flow.needed_keys(*common.load_layers())
    assert [row.name for row in rows] == ["OPENROUTER_API_KEY"]
    assert all(row.needed for row in rows)
    message = setup_flow.save_keys([("OPENROUTER_API_KEY", KEY)])
    assert message.startswith("OPENROUTER_API_KEY saved")
    assert KEY not in message
    assert common.load_layers()[0].entries["OPENROUTER_API_KEY"].value == KEY
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
    # common.no_key()'s usage error, as `setup --espanso-dir`/`--launcher` give it.
    with pytest.raises(typer.BadParameter, match="--espanso-dir looks like a key"):
        setup_flow.read_plan(KEY, "/bin/pm")
    with pytest.raises(typer.BadParameter, match="--launcher looks like a key"):
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
    args = ["--provider", "ollama", "--profile", "general", "--no-deploy", "--no-smoke-test"]
    result = CliRunner().invoke(app, ["setup", "--non-interactive", *args])
    assert result.exit_code == 1, result.output
    assert "  PROMPT_PROVIDER=ollama saved in " in result.stdout
    assert "PROMPT_PROFILE" not in result.stdout.split("Provider: ollama")[1].split("API keys")[0]
    assert "  failed: disk full" in result.stderr
    assert "settings failed" in result.stderr
