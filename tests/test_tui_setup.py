"""The setup wizard (tui/setup_wizard.py): driven with Pilot over a real State, saving through
the real services into per-test folders; Espanso is a fake runner and the smoke test a fake."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from test_tui import KEY, FakeRunner, drive, press, settle
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Static

from promptmend import config, config_store, deploy, smoke
from promptmend.commands import common
from promptmend.commands import setup as setup_cmd
from promptmend.commands import ui as ui_cmd
from promptmend.tui.app import ManageApp
from promptmend.tui.setup_wizard import SetupOptions, SetupScreen, needs_setup


@pytest.fixture
def espanso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """As test_tui's: Espanso found and running through a fake runner, a fixed launcher."""
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
    monkeypatch.delenv("PROMPTMEND_ENV")
    return config._user_config_dir()


@pytest.fixture
def smoke_ok(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake(provider: str) -> smoke.SmokeResult:
        calls.append(provider)
        return smoke.SmokeResult(True, smoke.REPLY, 1, "improve reached the stub")

    monkeypatch.setattr(smoke, "run", fake)
    return calls


def _wizard(app: ManageApp) -> SetupScreen:
    screen = app.screen
    assert isinstance(screen, SetupScreen)
    return screen


def _text(app: ManageApp, selector: str) -> str:
    return str(app.screen.query_one(selector, Static).render())


async def _pick(app: ManageApp, pilot: Pilot[int], selector: str, value: str) -> None:
    options = app.screen.query_one(selector, OptionList)
    options.highlighted = options.get_option_index(value)
    await press(app, pilot, "#setup-next-step")


def _options(espanso: FakeRunner, **kw: object) -> SetupOptions:
    return SetupOptions(espanso_dir=str(espanso.root), standalone=True, **kw)  # type: ignore[arg-type]


def test_wizard_walks_every_step_and_saves(
    saved: Path, espanso: FakeRunner, smoke_ok: list[str]
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        assert wizard.order == ["welcome", "provider", "profile", "keys", "espanso", "test", "done"]
        assert "Step 1 of 7" in _text(app, "#setup-title")
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "provider"
        await _pick(app, pilot, "#setup-providers", "ollama")
        assert wizard.at == "profile"
        await _pick(app, pilot, "#setup-profiles", "general")
        assert wizard.at == "keys"
        # Ollama is the provider, yet -i- still runs on OpenRouter: its key is asked for.
        assert "needed by -i-" in _text(app, "#setup-line-OPENROUTER_API_KEY")
        app.screen.query_one("#setup-key-OPENROUTER_API_KEY", Input).value = KEY
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "espanso"
        assert app.screen.query_one("#setup-key-OPENROUTER_API_KEY", Input).value == ""
        assert "not installed yet" in _text(app, "#setup-files")
        await press(app, pilot, "#setup-deploy")
        assert sorted(p.name for p in (espanso.root / "match").iterdir()) == [
            "prompts-core.yml",
            "prompts-llm.yml",
            "prompts-template.yml",
        ]
        assert ["espanso", "restart"] in espanso.calls
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "test"
        await press(app, pilot, "#setup-run-test")
        assert smoke_ok == ["ollama"]
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "done"
        summary = _text(app, "#setup-summary")
        assert "✓ OPENROUTER_API_KEY: set" in summary
        assert "✓ Match files: installed" in summary
        assert "✓ Test: passed" in summary
        await press(app, pilot, "#setup-quit")

    app = drive(scenario, setup=_options(espanso))
    assert app.return_code == 0
    layers, settings = common.load_layers()
    assert (settings.provider, settings.profile) == ("ollama", "general")
    assert layers.entries["OPENROUTER_API_KEY"].value == KEY
    commands = [entry.command for entry in app.session]
    assert "promptmend config set PROMPT_PROVIDER ollama" in commands
    assert "promptmend espanso deploy" in commands
    assert not any(KEY in entry.command or KEY in entry.message for entry in app.session)


def test_wizard_unchanged_choice_writes_nothing(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-providers", "openrouter")
        await _pick(app, pilot, "#setup-profiles", "default")

    app = drive(scenario, setup=_options(espanso))
    assert not config.settings_file().exists()
    assert app.session == []


def test_wizard_default_choice_is_removed_not_pinned(saved: Path, espanso: FakeRunner) -> None:
    config_store.save_settings(config_store.read_settings(), {"PROMPT_PROVIDER": "ollama"})

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-providers", "openrouter")

    app = drive(scenario, setup=_options(espanso))
    assert "PROMPT_PROVIDER" not in config_store.read_settings().table
    assert common.load_layers()[1].provider == "openrouter"
    # The log names the command that ran, not a `config set` (#237).
    assert [entry.command for entry in app.session] == ["promptmend config unset PROMPT_PROVIDER"]


def test_wizard_moves_on_in_legacy_env_mode(espanso: FakeRunner) -> None:
    """PROMPTMEND_ENV set (conftest's default): nothing can be saved, so Next says why and
    moves on instead of refusing on every step (#237)."""
    notes: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-providers", "ollama")
        assert wizard.at == "profile"
        await _pick(app, pilot, "#setup-profiles", "default")  # unchanged: no note
        assert wizard.at == "keys"
        # Keys go in that file too: no fields to type into, and Next moves on.
        assert "add each one there" in _text(app, "#setup-keys-intro")
        field = app.screen.query_one("#setup-key-OPENROUTER_API_KEY", Input)
        assert not field.display
        assert not app.screen.query_one("#setup-save-keys", Button).display
        field.value = KEY
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "espanso"

    app = drive(scenario, setup=_options(espanso))
    assert notes == [
        f"PROMPT_PROVIDER not saved: PROMPTMEND_ENV is set, so settings stay in "
        f"{common.legacy_env()}; edit that file instead."
    ]
    assert app.session == []
    layers, settings = common.load_layers()
    assert settings.provider == "openrouter"
    assert layers.entries["OPENROUTER_API_KEY"].value != KEY


def test_wizard_back_and_escape(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "provider"
        await pilot.press("escape")
        await settle(pilot)
        assert wizard.at == "welcome"
        # Standalone (`setup`): Escape on the first step does not leave.
        await pilot.press("escape")
        await settle(pilot)
        assert isinstance(app.screen, SetupScreen)

    drive(scenario, setup=_options(espanso))


def test_wizard_from_home_closes_back_to_it(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await press(app, pilot, "#home-setup")
        wizard = _wizard(app)
        assert not wizard.query_one("#setup-open").display
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is app.main

    drive(scenario)


def test_wizard_shows_earlier_step_for_a_dotenv(saved: Path, espanso: FakeRunner) -> None:
    saved.mkdir(parents=True, exist_ok=True)
    (saved / ".env").write_text("PROMPT_PROVIDER=ollama\n", "utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        assert "earlier" in wizard.order
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "earlier"
        assert "Your settings are in a .env" in _text(app, "#setup-earlier-text")
        await press(app, pilot, "#setup-migrate-env")
        app.screen.query_one("#confirm", Button).press()
        await settle(pilot)
        # Migrated: the step is no longer needed.
        assert "earlier" not in wizard.order

    drive(scenario, setup=_options(espanso))
    assert config.settings_file().is_file()
    assert common.load_layers()[1].provider == "ollama"


def test_wizard_deploy_refused_when_the_plan_changed(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        assert wizard.plan is not None
        (espanso.root / "match" / "prompts-core.yml").write_text("matches: []\n", "utf-8")
        await press(app, pilot, "#setup-deploy")
        assert "changed since they were read" in str(wizard.query_one(".result", Static).render())

    drive(scenario, setup=_options(espanso))
    assert not (espanso.root / "match" / "prompts-llm.yml").exists()


def test_wizard_without_espanso_says_so(saved: Path, espanso: FakeRunner, tmp_path: Path) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert "Cannot read the match folder" in _text(app, "#setup-files")
        assert app.screen.query_one("#setup-deploy").disabled

    drive(scenario, setup=SetupOptions(launcher='/a"b/promptmend', standalone=True))


def test_first_run_opens_the_wizard_once(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        assert app.state is not None
        assert needs_setup(app.state)
        wizard.dismiss()
        app.reload()
        await settle(pilot)
        assert not isinstance(app.screen, SetupScreen)

    drive(scenario, first_run=True)


def test_first_run_skips_the_wizard_once_a_key_is_saved(saved: Path, espanso: FakeRunner) -> None:
    config_store.save_secret("OPENROUTER_API_KEY", KEY)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert app.screen is app.main

    drive(scenario, first_run=True)


def test_setup_on_a_terminal_opens_the_wizard(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from promptmend.cli import app

    opened: list[dict[str, object]] = []
    monkeypatch.setattr(common, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    monkeypatch.setattr(ui_cmd, "open_setup", lambda **kw: opened.append(kw))
    result = CliRunner().invoke(app, ["setup", "--espanso-dir", "/x", "--no-restart"])
    assert result.exit_code == 0, result.output
    assert opened == [{"espanso_dir": "/x", "launcher": None, "no_restart": True}]
    # --plain, or an option the wizard does not take, asks line by line instead.
    monkeypatch.setattr(setup_cmd, "_deploy_step", lambda *a, **k: None)
    monkeypatch.setattr(smoke, "run", lambda p: smoke.SmokeResult(True, "", 1, "ok"))
    monkeypatch.setattr(common, "_getpass", lambda prompt: "")
    result = CliRunner().invoke(app, ["setup", "--plain"], input="\n\n")
    assert result.exit_code == 0, result.output
    assert "Provider (" in result.stdout
    assert len(opened) == 1


def test_needs_setup_false_without_a_plan(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert app.state is not None
        assert needs_setup(app.state)
        assert not needs_setup(dataclasses.replace(app.state, plan=None))

    drive(scenario)


def test_make_plan_with_options(saved: Path, espanso: FakeRunner) -> None:
    from promptmend.tui.setup_wizard import make_plan

    plan, fallback = make_plan(SetupOptions(espanso_dir=str(espanso.root), launcher="/bin/pm"))
    assert plan.espanso_dir == espanso.root.resolve()
    assert fallback is None
    assert all(step.state == deploy.MISSING for step in plan.steps)
