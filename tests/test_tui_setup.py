"""The setup wizard (tui/setup_wizard.py): driven with Pilot over a real State, saving through
the real services into per-test folders; Espanso is a fake runner and the smoke test a fake."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
from test_tui import KEY, FakeRunner, drive, press, settle
from textual.containers import VerticalScroll
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Static

from promptmend import (
    config,
    config_store,
    deploy,
    prompt_builder,
    settings_service,
    setup_guide,
    smoke,
)
from promptmend.commands import common
from promptmend.commands import setup as setup_cmd
from promptmend.commands import ui as ui_cmd
from promptmend.tui.app import ManageApp
from promptmend.tui.setup_wizard import SetupOptions, SetupScreen
from promptmend.tui.state import needs_setup


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
    return config.user_config_dir()


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


def _highlighted(app: ManageApp, selector: str) -> str | None:
    option = app.screen.query_one(selector, OptionList).highlighted_option
    return option.id if option else None


def _marked(app: ManageApp, selector: str) -> list[str]:
    """The choices shown as (current)."""
    options = app.screen.query_one(selector, OptionList)
    return [
        str(o.id)
        for o in (options.get_option_at_index(i) for i in range(options.option_count))
        if "(current)" in str(o.prompt)
    ]


def _rail(app: ManageApp) -> dict[str, str]:
    """The rail as shown: each step's name -> its mark ("" for none)."""
    marks = {}
    for line in _text(app, "#setup-rail").splitlines():
        marks[line[2:]] = line[:1].strip()
    return marks


def _agrees(app: ManageApp) -> list[setup_guide.DoneRow]:
    """On Done, the rail says what the checklist says (#393): each row is listed with its
    mark, and its step carries that mark on the rail (the worst of them for the keys)."""
    wizard = _wizard(app)
    assert wizard.at == "done"
    rows = wizard._rows()
    assert rows is not None
    summary = _text(app, "#setup-summary").splitlines()
    rail = _rail(app)
    assert rail["Done"] == "▶"
    assert rail["Welcome"] == "✓"
    for row, line in zip(rows, summary, strict=True):
        assert line.startswith(f"{row.mark} {row.label}: "), (row, line)
        name = setup_guide.STEPS[row.step]
        assert rail[name] == setup_guide.MARKS[setup_guide.step_status(row.step, rows)]
        if row.step != "keys":
            assert rail[name] == row.mark, (row, rail)
    return rows


def _options(espanso: FakeRunner, **kw: object) -> SetupOptions:
    return SetupOptions(espanso_dir=str(espanso.root), standalone=True, **kw)  # type: ignore[arg-type]


def test_wizard_walks_every_step_and_saves(
    saved: Path, espanso: FakeRunner, smoke_ok: list[str]
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        assert wizard.order == [
            "welcome",
            "use",
            "keys",
            "profile",
            "output",
            "espanso",
            "test",
            "done",
        ]
        assert "Step 1 of 8" in _text(app, "#setup-title")
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "use"
        assert "Step 2 of 8 · Cloud or local" in _text(app, "#setup-title")
        # What local does is said before Next, with the triggers from the match files.
        intro = _text(app, "#setup-use-note")
        assert "-i-, -iok-, -ip- and -if- then paste a refusal instead of sending" in intro
        assert "Use -il- (Ollama) or -ilm- (LM Studio)." in intro
        # Cloud and both are not stored: nothing is current, the highlight starts on cloud.
        assert _marked(app, "#setup-uses") == []
        assert _highlighted(app, "#setup-uses") == "cloud"
        await _pick(app, pilot, "#setup-uses", "local")
        assert wizard.at == "keys"
        assert settings_service.load_layers()[1].local_only
        # The refused triggers need no key, and the line says why.
        assert "not needed: PROMPT_LOCAL_ONLY=true refuses -i-" in _text(
            app, "#setup-line-OPENROUTER_API_KEY"
        )
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "profile"
        await _pick(app, pilot, "#setup-profiles", "general")
        assert wizard.at == "output"
        assert "Espanso shows nothing while a rewrite runs" in _text(app, "#setup-output-intro")
        assert _marked(app, "#setup-outputs") == ["paste"]
        await _pick(app, pilot, "#setup-outputs", "clipboard")
        assert wizard.at == "espanso"
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
        # The test takes the path of the answer's triggers, never PROMPT_PROVIDER's.
        assert "`promptmend improve --provider ollama` (the path -il- takes)" in _text(
            app, "#setup-test-intro"
        )
        await press(app, pilot, "#setup-run-test")
        assert smoke_ok == ["ollama"]
        assert "-il- (ollama): passed" in str(wizard.query_one(".result", Static).render())
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "done"
        summary = _text(app, "#setup-summary")
        assert "✓ Triggers: local (PROMPT_LOCAL_ONLY=true: the cloud triggers refuse)" in summary
        assert "✓ Profile: general" in summary
        assert "✓ Output: clipboard" in summary
        assert "OPENROUTER_API_KEY" not in summary
        assert "✓ Match files: installed" in summary
        assert "✓ Test: passed" in summary
        _agrees(app)
        assert set(_rail(app).values()) == {"✓", "▶"}
        await press(app, pilot, "#setup-quit")

    app = drive(scenario, setup=_options(espanso))
    assert app.return_code == 0
    settings = settings_service.load_layers()[1]
    assert (settings.local_only, settings.profile, settings.output) == (
        True,
        "general",
        "clipboard",
    )
    assert settings.provider == "openrouter"  # never asked
    commands = [entry.command for entry in app.session]
    assert commands[:3] == [
        "promptmend config set PROMPT_LOCAL_ONLY true",
        "promptmend config set PROMPT_PROFILE general",
        "promptmend config set PROMPT_OUTPUT clipboard",
    ]
    assert "promptmend espanso deploy" in commands


def test_wizard_both_asks_for_the_key_and_tests_both_paths(
    saved: Path, espanso: FakeRunner, smoke_ok: list[str]
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "both")
        assert wizard.at == "keys"
        assert "needed by -i-" in _text(app, "#setup-line-OPENROUTER_API_KEY")
        app.screen.query_one("#setup-key-OPENROUTER_API_KEY", Input).value = KEY
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "profile"
        assert app.screen.query_one("#setup-key-OPENROUTER_API_KEY", Input).value == ""
        wizard.go("test")
        await settle(pilot)
        intro = _text(app, "#setup-test-intro")
        assert "(the path -i- takes) and `promptmend improve --provider ollama`" in intro
        await press(app, pilot, "#setup-run-test")
        assert smoke_ok == ["openrouter", "ollama"]
        wizard.go("done")
        await settle(pilot)
        summary = _text(app, "#setup-summary")
        assert "✓ Triggers: both" in summary
        assert "✓ OPENROUTER_API_KEY: set" in summary

    app = drive(scenario, setup=_options(espanso))
    assert settings_service.load_layers()[0].entries["OPENROUTER_API_KEY"].value == KEY
    assert not settings_service.load_layers()[1].local_only
    assert not any(KEY in entry.command or KEY in entry.message for entry in app.session)


def test_wizard_test_failure_names_the_path(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(provider: str) -> smoke.SmokeResult:
        if provider == "ollama":
            raise OSError("no python")
        return smoke.SmokeResult(True, smoke.REPLY, 1, "fine")

    monkeypatch.setattr(smoke, "run", fake)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "both")
        wizard.go("test")
        await settle(pilot)
        await press(app, pilot, "#setup-run-test")
        result = str(wizard.query_one(".result", Static).render())
        assert "-i- (openrouter): passed: fine" in result
        assert "-il- (ollama): failed: error: no python" in result
        assert wizard.tested is False

    drive(scenario, setup=_options(espanso))


def test_wizard_local_saves_although_the_shell_says_true_and_says_so(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Espanso does not start from the shell, so config.toml must hold the answer; the
    save's warning stays on screen on the next step."""
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "local")
        assert wizard.at == "keys"
        result = str(wizard.query_one(".result", Static).render())
        # Only the warning is carried; what was saved, and where, is in the log.
        assert "saved in" not in result
        assert "the environment variable PROMPT_LOCAL_ONLY is set and overrides" in result
        assert "PROMPT_LOCAL_ONLY=true saved in " in app.session[-1].message

    app = drive(scenario, setup=_options(espanso))
    assert config_store.read_settings().table.get("PROMPT_LOCAL_ONLY") is True
    assert [entry.command for entry in app.session] == [
        "promptmend config set PROMPT_LOCAL_ONLY true"
    ]


def test_wizard_done_names_an_environment_variable_that_disagrees(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "false")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "local")
        # The keys follow the answer: no cloud key is asked for after local.
        assert "not needed: PROMPT_LOCAL_ONLY=true refuses -i-" in _text(
            app, "#setup-line-OPENROUTER_API_KEY"
        )
        wizard.go("done")
        await settle(pilot)
        summary = _text(app, "#setup-summary")
        assert "✗ Triggers: local, but the environment variable PROMPT_LOCAL_ONLY overrides" in (
            summary
        )
        assert "remove it from the environment" in summary
        assert "config set PROMPT_LOCAL_ONLY" not in summary
        assert "OPENROUTER_API_KEY" not in summary
        assert "type -il- " in _text(app, "#setup-next")

    drive(scenario, setup=_options(espanso))
    assert config_store.read_settings().table.get("PROMPT_LOCAL_ONLY") is True


def test_wizard_test_result_goes_with_its_answer(
    saved: Path, espanso: FakeRunner, smoke_ok: list[str]
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "cloud")
        wizard.go("test")
        await settle(pilot)
        await press(app, pilot, "#setup-run-test")
        assert wizard.tested is True
        wizard.go("use")
        await settle(pilot)
        await _pick(app, pilot, "#setup-uses", "local")
        assert wizard.tested is None
        wizard.go("done")
        await settle(pilot)
        summary = _text(app, "#setup-summary")
        # Not run is not a failure: the rail and the checklist both say a dash (#393).
        assert "\u2013 Test: not run" in summary
        assert "✓ Test" not in summary
        assert _rail(app)["Test"] == "\u2013"
        _agrees(app)

    drive(scenario, setup=_options(espanso))
    assert smoke_ok == ["openrouter"]


def test_wizard_local_tests_the_trigger_that_stays_local(
    saved: Path, espanso: FakeRunner, smoke_ok: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "local")
        wizard.go("test")
        await settle(pilot)
        assert "(the path -ilm- takes)" in _text(app, "#setup-test-intro")
        await press(app, pilot, "#setup-run-test")
        monkeypatch.setenv("LMSTUDIO_BASE_URL", "https://lm.example.com/v1")
        app.reload()
        await settle(pilot)
        assert "Nothing runs on the local path" in _text(app, "#setup-test-intro")
        await press(app, pilot, "#setup-run-test")
        assert wizard.tested is False
        assert "local: not run: no trigger stays on this computer" in str(
            wizard.query_one(".result", Static).render()
        )

    drive(scenario, setup=_options(espanso))
    assert smoke_ok == ["lmstudio"]


def test_wizard_names_the_deprecated_provider_and_saves_it_not(
    saved: Path, espanso: FakeRunner
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        notes = _text(app, "#setup-welcome-notes")
        assert "--provider is deprecated and will be removed in a later release" in notes
        assert "this setup has no step for it, so PROMPT_PROVIDER was not saved" in notes
        assert "no trigger reads it" in notes
        assert "`promptmend config set PROMPT_PROVIDER ollama` saves it" in notes
        await press(app, pilot, "#setup-next-step")
        assert _highlighted(app, "#setup-uses") == "local"
        assert wizard.at == "use"

    drive(scenario, setup=_options(espanso, provider="ollama", use="local"))
    assert not config.settings_file().exists()


def _row_visible(app: ManageApp, step: str, selector: str) -> bool:
    """Whether the list's highlighted choice shows: inside the list's own scroll view and the
    step's. (Textual's line map is private; a test may read it, the wizard does not.)"""
    options = app.screen.query_one(selector, OptionList)
    scroller = app.screen.query_one(f"#setup-{step}", VerticalScroll)
    index = options.highlighted
    assert index is not None
    in_list = options._index_to_line[index] - options.scroll_y
    height = options._heights[index]
    if in_list < 0 or in_list + height > options.scrollable_content_region.height:
        return False
    top = options.virtual_region.y + in_list
    return scroller.scroll_y <= top and top + height <= scroller.scroll_y + scroller.size.height


def test_wizard_shows_what_config_toml_holds_and_a_plain_next_writes_nothing(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The current value of each choice is config.toml's (or the default), whatever the shell
    says: a re-run with a plain Next on every step writes nothing."""
    saved.mkdir(parents=True, exist_ok=True)
    (saved / "config.toml").write_text('PROMPT_OUTPUT = "clipboard"\n', "utf-8")
    monkeypatch.setenv("PROMPT_OUTPUT", "paste")
    monkeypatch.setenv("PROMPT_PROFILE", "general")
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        assert _marked(app, "#setup-uses") == []
        assert _highlighted(app, "#setup-uses") == "cloud"
        assert _marked(app, "#setup-profiles") == ["default"]
        assert _marked(app, "#setup-outputs") == ["clipboard"]
        assert _highlighted(app, "#setup-outputs") == "clipboard"
        while wizard.at != "espanso":
            await press(app, pilot, "#setup-next-step")
        wizard.go("done")
        await settle(pilot)
        summary = _text(app, "#setup-summary")
        assert "✓ Profile: default (the environment variable PROMPT_PROFILE overrides it" in (
            summary
        )
        assert (
            "✓ Output: clipboard (the environment variable PROMPT_OUTPUT overrides it wherever "
            "it is set: paste)" in summary
        )
        # Read back as cloud (config.toml holds no PROMPT_LOCAL_ONLY): the shell's true is a
        # mismatch, not the answer.
        assert "✗ Triggers: cloud, but the environment variable PROMPT_LOCAL_ONLY" in summary
        assert "waits on the clipboard" in _text(app, "#setup-next")

    app = drive(scenario, setup=_options(espanso))
    assert app.session == []
    assert (saved / "config.toml").read_text("utf-8") == 'PROMPT_OUTPUT = "clipboard"\n'


def test_wizard_local_note_says_is_set_only_when_config_toml_holds_it(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert _text(app, "#setup-use-note").startswith("Local saves PROMPT_LOCAL_ONLY=true:")

    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")  # the shell alone: not set for Espanso
    drive(scenario, setup=_options(espanso))
    monkeypatch.delenv("PROMPT_LOCAL_ONLY")
    saved.mkdir(parents=True, exist_ok=True)
    (saved / "config.toml").write_text("PROMPT_LOCAL_ONLY = true\n", "utf-8")

    async def again(app: ManageApp, pilot: Pilot[int]) -> None:
        note = _text(app, "#setup-use-note")
        assert note.startswith("PROMPT_LOCAL_ONLY=true is set: -i-")
        assert note.endswith("Cloud or Both turns it off again.")
        assert _marked(app, "#setup-uses") == ["local"]

    drive(again, setup=_options(espanso))


def test_wizard_legacy_local_only_says_to_remove_it_from_the_file(
    espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = Path(os.environ["PROMPTMEND_ENV"])
    legacy.write_text("PROMPT_LOCAL_ONLY=true\n", "utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        note = _text(app, "#setup-use-note")
        assert note.startswith("PROMPT_LOCAL_ONLY=true is set: ")
        assert note.endswith(f"Remove it from {legacy} to turn it off.")
        assert "turns it off again" not in note

    drive(scenario, setup=_options(espanso))


def test_wizard_clipboard_output_passes_the_test_and_leaves_the_clipboard(
    saved: Path, espanso: FakeRunner, smoke_in_process: list[str]
) -> None:
    """The stub run prints its reply (paste output) whatever PROMPT_OUTPUT says, so it
    passes and never copies the stub's reply."""

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        wizard.go("output")
        await settle(pilot)
        await _pick(app, pilot, "#setup-outputs", "clipboard")
        wizard.go("test")
        await settle(pilot)
        await press(app, pilot, "#setup-run-test")
        assert "-i- (openrouter): passed" in str(wizard.query_one(".result", Static).render())
        assert wizard.tested is True

    drive(scenario, setup=_options(espanso))
    assert config_store.read_settings().table.get("PROMPT_OUTPUT") == "clipboard"
    assert smoke_in_process == []


@pytest.mark.parametrize("size", [(80, 24), (70, 24)])
def test_wizard_a_carried_warning_never_hides_the_choice_enter_picks(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    monkeypatch.setenv("PROMPT_PROFILE", "default")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "local")
        while wizard.at != "profile":
            await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-profiles", "general")
        assert wizard.at == "output"
        result = str(wizard.query_one(".result", Static).render())
        assert "the environment variable PROMPT_PROFILE is set and overrides" in result
        assert "saved in" not in result
        assert app.screen.focused is wizard.query_one("#setup-outputs", OptionList)
        assert _row_visible(app, "output", "#setup-outputs")

    drive(scenario, setup=_options(espanso), size=size)


@pytest.mark.parametrize("step", ["use", "profile", "output"])
def test_wizard_a_tall_message_still_shows_the_highlighted_choice(
    saved: Path, espanso: FakeRunner, step: str
) -> None:
    """However many rows a carried message takes, the step scrolls (no further than needed)
    to the choice the focused list has highlighted."""
    lists = {"use": "#setup-uses", "profile": "#setup-profiles", "output": "#setup-outputs"}

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        options = wizard.query_one(lists[step], OptionList)
        options.highlighted = options.option_count - 1
        wizard.report("", shown="\n".join(f"a warning line {n}" for n in range(9)))
        wizard.go(step)
        await settle(pilot)
        assert app.screen.focused is options
        assert _row_visible(app, step, lists[step])

    drive(scenario, setup=_options(espanso), size=(80, 24))


@pytest.mark.parametrize("pick", ["first", "last"])
def test_wizard_a_profile_list_taller_than_the_screen_shows_the_highlighted_choice(
    saved: Path, espanso: FakeRunner, pick: str
) -> None:
    """Many added profiles: the list is capped to the step's visible height and scrolls
    inside itself to the highlighted choice."""
    folder = prompt_builder.user_profiles_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(20):
        (folder / f"mine{n:02}.md").write_text("Rewrite the draft.", encoding="utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        options = wizard.query_one("#setup-profiles", OptionList)
        assert options.option_count == 22
        options.highlighted = 0 if pick == "first" else options.option_count - 1
        wizard.go("profile")
        await settle(pilot)
        assert app.screen.focused is options
        step = wizard.query_one("#setup-profile", VerticalScroll)
        assert options.virtual_size.height > step.scrollable_content_region.height
        assert _row_visible(app, "profile", "#setup-profiles")

    drive(scenario, setup=_options(espanso), size=(80, 24))


@pytest.mark.parametrize("size", [(80, 24), (70, 24)])
@pytest.mark.parametrize("step", ["use", "profile", "output"])
def test_wizard_a_list_step_that_fits_opens_on_its_question(
    saved: Path, espanso: FakeRunner, size: tuple[int, int], step: str
) -> None:
    lists = {"use": "#setup-uses", "profile": "#setup-profiles", "output": "#setup-outputs"}

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        wizard.go(step)
        await settle(pilot)
        assert app.screen.focused is wizard.query_one(lists[step], OptionList)
        assert wizard.query_one(f"#setup-{step}", VerticalScroll).scroll_y == 0
        assert _row_visible(app, step, lists[step])

    drive(scenario, setup=_options(espanso), size=size)


@pytest.mark.parametrize("size", [(80, 24), (70, 24)])
def test_wizard_done_opens_at_the_top(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "false")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "local")
        wizard.go("done")
        await settle(pilot)
        done = wizard.query_one("#setup-done", VerticalScroll)
        assert done.max_scroll_y > 0  # taller than the screen, so opening at the top matters
        assert done.scroll_y == 0
        assert app.screen.focused is wizard.query_one("#setup-quit", Button)

    drive(scenario, setup=_options(espanso), size=size)


def test_wizard_unchanged_choice_writes_nothing(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "cloud")
        await press(app, pilot, "#setup-next-step")  # keys: none typed
        await _pick(app, pilot, "#setup-profiles", "default")
        await _pick(app, pilot, "#setup-outputs", "paste")
        assert _wizard(app).at == "espanso"

    app = drive(scenario, setup=_options(espanso))
    assert not config.settings_file().exists()
    assert app.session == []


def test_wizard_default_choice_is_removed_not_pinned(saved: Path, espanso: FakeRunner) -> None:
    config_store.save_settings(
        config_store.read_settings(), {"PROMPT_LOCAL_ONLY": "true", "PROMPT_OUTPUT": "clipboard"}
    )

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        # A re-run reads the answer back: local is current while PROMPT_LOCAL_ONLY is true.
        assert _marked(app, "#setup-uses") == ["local"]
        assert _highlighted(app, "#setup-uses") == "local"
        await _pick(app, pilot, "#setup-uses", "cloud")
        assert wizard.at == "keys"
        wizard.go("output")
        await settle(pilot)
        assert _highlighted(app, "#setup-outputs") == "clipboard"
        await _pick(app, pilot, "#setup-outputs", "paste")
        # Back from the next step returns to it, the new value now current.
        await press(app, pilot, "#setup-back")
        assert wizard.at == "output"
        assert _marked(app, "#setup-outputs") == ["paste"]

    app = drive(scenario, setup=_options(espanso))
    assert config_store.read_settings().values == {}
    assert not settings_service.load_layers()[1].local_only
    # The log names the command that ran, not a `config set` (#237).
    assert [entry.command for entry in app.session] == [
        "promptmend config unset PROMPT_LOCAL_ONLY",
        "promptmend config unset PROMPT_OUTPUT",
    ]


def test_wizard_shows_a_refused_save(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> None:
        raise config_store.ConfigStoreError("read-only")

    monkeypatch.setattr(config_store, "save_settings", refuse)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "local")
        # Not saved: the step stays, says why and names the command it tried.
        assert wizard.at == "use"
        assert wizard.use is None
        assert "read-only" in str(wizard.query_one(".result", Static).render())

    app = drive(scenario, setup=_options(espanso))
    assert [entry.command for entry in app.session] == [
        "promptmend config set PROMPT_LOCAL_ONLY true"
    ]


def test_wizard_moves_on_in_legacy_env_mode(espanso: FakeRunner) -> None:
    """PROMPTMEND_ENV set (conftest's default): nothing can be saved, so Next says why and
    moves on instead of refusing on every step (#237)."""
    notes: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "local")
        assert wizard.at == "keys"
        # Keys go in that file too: no fields to type into, and Next moves on.
        assert "add each one there" in _text(app, "#setup-keys-intro")
        field = app.screen.query_one("#setup-key-OPENROUTER_API_KEY", Input)
        assert not field.display
        assert not app.screen.query_one("#setup-save-keys", Button).display
        field.value = KEY
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "profile"
        await _pick(app, pilot, "#setup-profiles", "default")  # unchanged: no note
        await _pick(app, pilot, "#setup-outputs", "clipboard")
        assert wizard.at == "espanso"
        # The answer was not saved: Done says so, with what to add to that file.
        wizard.go("done")
        await settle(pilot)
        summary = _text(app, "#setup-summary")
        assert "✗ Triggers: local, but PROMPT_LOCAL_ONLY is off" in summary
        assert f"add PROMPT_LOCAL_ONLY=true to {settings_service.legacy_env()}" in summary

    app = drive(scenario, setup=_options(espanso))
    legacy = settings_service.legacy_env()
    assert notes == [
        f"{name} not saved: PROMPTMEND_ENV is set, so settings stay in {legacy}; edit that "
        "file instead."
        for name in ("PROMPT_LOCAL_ONLY", "PROMPT_OUTPUT")
    ]
    assert app.session == []
    layers, settings = settings_service.load_layers()
    assert (settings.local_only, settings.output) == (False, "paste")
    assert layers.entries["OPENROUTER_API_KEY"].value != KEY


def test_wizard_back_and_escape(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "use"
        await pilot.press("escape")
        await settle(pilot)
        assert wizard.at == "welcome"
        # Standalone (`setup`): Escape on the first step leaves, as Quit does.
        await pilot.press("escape")
        await settle(pilot)

    app = drive(scenario, setup=_options(espanso))
    assert app.return_code == 0


def test_wizard_from_home_closes_back_to_it(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await press(app, pilot, "#home-setup")
        wizard = _wizard(app)
        assert not wizard.query_one("#setup-open").display
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is app.main

    drive(scenario)


# A .env no release reads any more (#275) is named on Welcome; the wizard never touches it.
def test_wizard_names_an_unread_dotenv(saved: Path, espanso: FakeRunner) -> None:
    saved.mkdir(parents=True, exist_ok=True)
    env = saved / ".env"
    env.write_text("PROMPT_PROVIDER=ollama\n", "utf-8")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        assert wizard.order[:2] == ["welcome", "use"]
        assert f"{env} is no longer read" in _text(app, "#setup-welcome-notes")

    drive(scenario, setup=_options(espanso))
    assert env.read_text("utf-8") == "PROMPT_PROVIDER=ollama\n"
    assert settings_service.load_layers()[1].provider == "openrouter"


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

    opened: list[SetupOptions] = []
    monkeypatch.setattr(common, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    monkeypatch.setattr(ui_cmd, "open_setup", opened.append)
    result = CliRunner().invoke(app, ["setup", "--espanso-dir", "/x", "--no-restart"])
    assert result.exit_code == 0, result.output
    assert opened == [SetupOptions("/x", None, True, standalone=True)]
    # The other options prefill the wizard rather than switch to another mode (#244).
    args = ["--use", "local", "--profile", "general", "--output", "clipboard"]
    result = CliRunner().invoke(app, ["setup", *args, "--deploy", "--no-smoke-test"])
    assert result.exit_code == 0, result.output
    assert opened[1] == SetupOptions(
        standalone=True,
        use="local",
        profile="general",
        output="clipboard",
        deploy=True,
        smoke_test=False,
    )
    # --plain is --non-interactive: it prints the steps and opens nothing.
    monkeypatch.setattr(setup_cmd, "_deploy_step", lambda *a, **k: None)
    monkeypatch.setattr(smoke, "run", lambda p: smoke.SmokeResult(True, "", 1, "ok"))
    result = CliRunner().invoke(app, ["setup", "--plain"])
    assert result.exit_code == 0, result.output
    assert "Triggers: cloud; profile: default; output: paste" in result.stdout
    assert len(opened) == 2


def test_wizard_options_prefill_and_skip_steps(
    saved: Path, espanso: FakeRunner, smoke_ok: list[str]
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        assert wizard.order == ["welcome", "use", "keys", "profile", "output", "done"]
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "use"
        # Preselected, not saved: Next saves it, as for a pick.
        assert _highlighted(app, "#setup-uses") == "local"
        assert config_store.read_settings().values == {}
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "keys"
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "profile"
        await press(app, pilot, "#setup-next-step")
        assert wizard.at == "output"
        assert _highlighted(app, "#setup-outputs") == "clipboard"
        await press(app, pilot, "#setup-next-step")
        assert config_store.read_settings().values == {
            "PROMPT_LOCAL_ONLY": "true",
            "PROMPT_PROFILE": "general",
            "PROMPT_OUTPUT": "clipboard",
        }
        wizard.go("done")
        await settle(pilot)
        summary = _text(app, "#setup-summary")
        assert "\u2013 Match files: skipped (--no-deploy)" in summary
        assert "\u2013 Test: skipped (--no-smoke-test)" in summary
        # Left out of the steps, still on the rail, marked as Done lists them.
        assert list(_rail(app)) == list(setup_guide.STEPS.values())
        assert (_rail(app)["Espanso"], _rail(app)["Test"]) == ("\u2013", "\u2013")
        _agrees(app)

    drive(
        scenario,
        setup=_options(
            espanso,
            use="local",
            profile="general",
            output="clipboard",
            deploy=False,
            smoke_test=False,
        ),
    )
    assert smoke_ok == []


def test_wizard_deploy_option_focuses_install_but_writes_nothing(
    saved: Path, espanso: FakeRunner
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        wizard.go("espanso")
        await settle(pilot)
        assert app.focused is app.screen.query_one("#setup-deploy", Button)
        assert list((espanso.root / "match").iterdir()) == []

    drive(scenario, setup=_options(espanso, deploy=True))


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


def test_wizard_rail_marks_failed_skipped_and_not_reached(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        assert _rail(app) == {
            "Welcome": "▶",
            **{name: "" for step, name in setup_guide.STEPS.items() if step != "welcome"},
        }
        await press(app, pilot, "#setup-next-step")
        await _pick(app, pilot, "#setup-uses", "cloud")
        await press(app, pilot, "#setup-next-step")  # no key typed: OPENROUTER_API_KEY missing
        rail = _rail(app)
        assert (rail["Welcome"], rail["Cloud or local"], rail["API keys"]) == ("✓", "✓", "✗")
        assert (rail["Profile"], rail["Output"], rail["Test"]) == ("▶", "", "")
        wizard.go("test")
        await settle(pilot)
        assert _rail(app)["Espanso"] == "✗"  # passed without installing
        # Repainted when the facts change, not only on a move: here the plan read again.
        assert wizard.plan is not None
        steps = [dataclasses.replace(s, state=deploy.IN_SYNC) for s in wizard.plan.steps]
        wizard._show_plan(dataclasses.replace(wizard.plan, steps=steps), None, None)
        assert _rail(app)["Espanso"] == "✓"
        wizard._show_plan(None, None, "gone")
        assert _rail(app)["Espanso"] == "✗"
        wizard.go("done")
        await settle(pilot)
        rows = _agrees(app)
        assert [(r.label, r.mark) for r in rows] == [
            ("Triggers", "✓"),
            ("Profile", "✓"),
            ("Output", "✓"),
            ("OPENROUTER_API_KEY", "✗"),
            ("Match files", "✗"),
            ("Test", "\u2013"),
        ]

    drive(scenario, setup=_options(espanso))


def test_wizard_rail_follows_the_plan_and_the_test(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = iter([False, True])
    monkeypatch.setattr(
        smoke, "run", lambda p: smoke.SmokeResult(next(results), smoke.REPLY, 1, "stub")
    )

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        config_store.save_secret("OPENROUTER_API_KEY", KEY)
        app.reload()
        await settle(pilot)
        wizard.go("espanso")
        await settle(pilot)
        await press(app, pilot, "#setup-deploy")
        wizard.go("test")
        await settle(pilot)
        assert _rail(app)["Espanso"] == "✓"
        await press(app, pilot, "#setup-run-test")
        assert wizard.tested is False
        wizard.go("done")
        await settle(pilot)
        assert "✗ Test: failed (see the Test step)" in _text(app, "#setup-summary")
        assert _rail(app)["Test"] == "✗"
        _agrees(app)
        wizard.go("test")
        await settle(pilot)
        await press(app, pilot, "#setup-run-test")
        wizard.go("done")
        await settle(pilot)
        assert "✓ Test: passed" in _text(app, "#setup-summary")
        _agrees(app)

    drive(scenario, setup=_options(espanso))


def test_wizard_rail_and_done_agree_without_espanso(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        wizard.go("done")
        await settle(pilot)
        summary = _text(app, "#setup-summary")
        assert "✗ Match files: Espanso's folder was not found" in summary
        assert "\u2013 Test: not run" in summary
        assert (_rail(app)["Espanso"], _rail(app)["Test"]) == ("✗", "\u2013")
        _agrees(app)

    drive(scenario, setup=SetupOptions(launcher='/a"b/promptmend', standalone=True))


def _quit_shown(app: ManageApp) -> bool | None:
    """Whether the footer shows Quit setup: True, greyed out (None) or hidden (False)."""
    active = app.screen.active_bindings.get("q")
    if active is None:
        return False
    assert active.binding.description == "Quit setup"
    return True if active.enabled else None


def test_wizard_quit_is_shown_on_every_step_and_quits(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        for step in wizard.order:
            wizard.go(step)
            await settle(pilot)
            assert _quit_shown(app) is True, step
        wizard.go("output")
        await settle(pilot)
        await pilot.press("q")
        await settle(pilot)

    app = drive(scenario, setup=_options(espanso))
    assert app.return_code == 0  # as the Done step's Quit


def test_wizard_quit_is_hidden_in_a_key_field_and_refused_while_working(
    saved: Path, espanso: FakeRunner
) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        wizard.go("keys")
        await settle(pilot)
        field = wizard.query_one("#setup-key-OPENROUTER_API_KEY", Input)
        field.focus()
        await settle(pilot)
        # The field takes the letter, so the footer does not offer it.
        assert _quit_shown(app) is False
        await pilot.press("q")
        await settle(pilot)
        assert field.value == "q"
        assert isinstance(app.screen, SetupScreen)
        field.value = ""
        wizard.query_one("#setup-next-step", Button).focus()
        await settle(pilot)
        assert _quit_shown(app) is True
        wizard.working = True  # an install or the test is running
        await settle(pilot)
        assert _quit_shown(app) is None
        await pilot.press("q")
        await settle(pilot)
        assert app.screen is wizard
        wizard.working = False
        await settle(pilot)
        assert _quit_shown(app) is True

    drive(scenario, setup=_options(espanso))


def test_wizard_quit_from_home_returns_to_it(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await press(app, pilot, "#home-setup")
        wizard = _wizard(app)
        wizard.go("profile")
        await settle(pilot)
        assert _quit_shown(app) is True
        await pilot.press("q")
        await settle(pilot)
        assert app.screen is app.main
        # Escape on Welcome closes it too.
        await press(app, pilot, "#home-setup")
        assert _wizard(app).at == "welcome"
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is app.main

    app = drive(scenario)
    assert app.return_code is None


def test_wizard_done_follows_a_plan_read_that_fails_while_it_is_open(
    saved: Path, espanso: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full State landing on Done reads the plan again; when that read fails, the
    checklist changes with the rail, not the rail alone."""
    from promptmend import setup_flow

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        config_store.save_secret("OPENROUTER_API_KEY", KEY)
        wizard.go("espanso")
        await settle(pilot)
        await press(app, pilot, "#setup-deploy")
        wizard.go("done")
        await settle(pilot)
        assert "✓ Match files: installed in " in _text(app, "#setup-summary")
        assert _rail(app)["Espanso"] == "✓"
        _agrees(app)

        def gone(*_: object) -> None:
            raise deploy.DeployError("espanso went away")

        monkeypatch.setattr(setup_flow, "read_plan", gone)
        app.reload()
        await settle(pilot)
        assert wizard.plan is None
        assert "✗ Match files: Espanso's folder was not found" in _text(app, "#setup-summary")
        assert _rail(app)["Espanso"] == "✗"
        _agrees(app)

    drive(scenario, setup=_options(espanso))


def test_wizard_says_the_match_folder_is_being_read(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        wizard.go("done")
        await settle(pilot)
        # As before the first read lands: no plan and no error yet.
        wizard.plan, wizard.plan_error = None, None
        wizard._repaint()
        await settle(pilot)
        summary = _text(app, "#setup-summary")
        assert "\u2026 Match files: the match folder is being read" in summary
        assert "not found" not in summary
        assert _rail(app)["Espanso"] == "\u2026"
        _agrees(app)

    drive(scenario, setup=_options(espanso))


def test_wizard_greys_out_back_while_working(saved: Path, espanso: FakeRunner) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        wizard = _wizard(app)
        wizard.go("profile")
        await settle(pilot)
        assert app.screen.active_bindings["escape"].enabled
        wizard.working = True
        await settle(pilot)
        assert not app.screen.active_bindings["escape"].enabled
        await pilot.press("escape")
        await settle(pilot)
        assert wizard.at == "profile"
        wizard.working = False

    drive(scenario, setup=_options(espanso))
