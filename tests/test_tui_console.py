"""Home's command line (#111, stage 2): what it completes, what its help line says, and that
it runs nothing. Every word it offers parses against the CLI (the drift test), a key is never
offered outside `secrets`, and a line that holds a key is never echoed or kept."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from typing import Any

import pyperclip
import pytest
from textual.pilot import Pilot
from textual.widgets import Button, OptionList, RichLog, Static, TabbedContent
from typer._click.exceptions import UsageError
from typer._types import TyperChoice
from typer.core import TyperArgument, TyperGroup, TyperOption

from promptmend import config, console, prompt_builder, smoke
from promptmend.tui import console as tui_console
from promptmend.tui import home_pane, teach
from promptmend.tui.app import ManageApp
from promptmend.tui.modals import FormModal, PickModal

# Built at runtime, so no key-shaped literal lands in the repo (gitleaks).
KEY = "sk-or-v1-" + "ab12" * 16
SETTINGS = set(config.env_names()) - set(config.secret_names())
SECRETS = set(config.secret_names())
# The real runner, kept before conftest's isolated_env replaces console.run with a refusal
# in every test: only the runner tests (subprocess.run faked) and the one --version smoke
# test call it, on purpose.
REAL_RUN = console.run


# --- Candidates -----------------------------------------------------------------------------


def test_top_level_offers_every_visible_command() -> None:
    words = console.words_for([])
    assert {"config", "doctor", "espanso", "secrets", "ui", "--version"} <= set(words)
    assert words == sorted(words)


def test_config_set_offers_settings_never_a_key() -> None:
    words = set(console.words_for(["config", "set"]))
    assert words >= SETTINGS
    assert not words & SECRETS
    assert console.suggest("config set PROMPT_") == "config set PROMPT_EXTRA_PATTERNS"
    assert console.suggest("config set OPENROUTER_API") is None
    for command in ("get", "unset"):
        assert not set(console.words_for(["config", command])) & SECRETS


def test_secrets_set_offers_only_key_names() -> None:
    for command in ("set", "remove"):
        names = {w for w in console.words_for(["secrets", command]) if not w.startswith("-")}
        assert names == SECRETS
    assert console.suggest("secrets set O") == "secrets set OPENROUTER_API_KEY"
    assert console.words_for(["secrets", "status"]) == []


def test_profiles_after_a_profile_setting_and_the_profile_option() -> None:
    folder = prompt_builder.user_profiles_dir()
    folder.mkdir(parents=True)
    (folder / "mine.md").write_text("x", encoding="utf-8")
    (folder / "Bad Name.md").write_text("x", encoding="utf-8")
    expected = sorted({*prompt_builder.PROFILES, "mine"})
    for prefix in (
        ["config", "set", "PROMPT_PROFILE"],
        ["config", "set", "PROMPT_PRO_PROFILE"],
        ["improve", "--profile"],
        ["setup", "--profile"],
    ):
        assert console.words_for(prefix) == expected, prefix
    assert console.words_for(["config", "set", "PROMPT_HISTORY"]) == []
    assert console.suggest("improve --profile mi") == "improve --profile mine"


def test_hidden_options_and_used_ones_are_not_offered() -> None:
    assert "--trigger-id" not in console.words_for(["improve"])
    assert console.words_for(["persona"]) == []
    assert "--yes" not in console.words_for(["espanso", "deploy", "-y"])
    assert console.words_for(["no-such"]) == []
    assert console.words_for(["--version"]) == []


def test_option_values_and_choices() -> None:
    assert "--profile" not in console.words_for(["improve", "--profile", "general"])
    assert "--profile" not in console.words_for(["improve", "--profile=general"])
    assert console.words_for(["improve", "--model"]) == []
    choice = TyperOption(param_decls=["--colour"], type=TyperChoice(["red", "blue"]))
    assert console._option_values(choice, "--colour") == ["blue", "red"]
    argument = TyperArgument(param_decls=["colour"], type=TyperChoice(["red", "blue"]))
    assert console._argument_values(("x",), argument, []) == ["blue", "red"]


def test_suggest_completes_only_the_word_being_typed() -> None:
    assert console.suggest("esp") == "espanso"
    assert console.suggest("promptmend esp") == "promptmend esp" + "anso"
    assert console.suggest("espanso st") == "espanso status"
    assert console.suggest("espanso status --d") == "espanso status --diff"
    assert console.suggest("doctor ") is None
    assert console.suggest("") is None
    assert console.suggest('config set "PROMPT_') is None  # open quote
    assert console.suggest("doctor") is None  # complete already
    assert console.suggest("zzz") is None
    assert console.suggest("promptmend") is None


def test_the_suggester_is_uncached_and_case_sensitive() -> None:
    suggester = tui_console.CommandSuggester()
    assert suggester.cache is None
    assert suggester.case_sensitive
    assert asyncio.run(suggester.get_suggestion("espanso de")) == "espanso deploy"


# --- Drift: every word offered parses ---------------------------------------------------------

# A value for an option or argument that has no candidates of its own.
PLACEHOLDER = "x"


def _walk() -> Iterator[tuple[str, ...]]:
    """Every command path of the CLI: groups and leaves."""

    def walk(path: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
        yield path
        found = console.resolve(path, resilient=True, partial=True)
        if isinstance(found.command, TyperGroup):
            for name in console.words_for(path):
                if not name.startswith("-"):
                    yield from walk((*path, name))

    return walk(())


PATHS = list(_walk())


def test_the_walk_reaches_every_leaf() -> None:
    assert ("config", "set") in PATHS
    assert ("espanso", "deploy") in PATHS
    assert ("history", "prune") in PATHS
    assert len(PATHS) > 25


@pytest.mark.parametrize("path", PATHS, ids=lambda p: " ".join(p) or "promptmend")
def test_every_candidate_parses(path: tuple[str, ...]) -> None:
    found = console.resolve(path, resilient=True, partial=True)
    options = {
        opt: param
        for param in found.command.params
        if isinstance(param, TyperOption)
        for opt in (*param.opts, *param.secondary_opts)
    }
    for word in console.words_for(path):
        argv = (*path, word)
        option = options.get(word)
        if option is not None and not option.is_flag:
            values = console.words_for(argv) or [PLACEHOLDER]
            for value in values:
                console.resolve((*argv, value), resilient=True, partial=True)
        else:
            console.resolve(argv, resilient=True, partial=True)
            # A value the next argument takes (config set PROMPT_PROFILE <profile>).
            for value in console.words_for(argv):
                if not value.startswith("-"):
                    console.resolve((*argv, value), resilient=True)
        # A key name is offered only where a key is named.
        if word in SECRETS:
            assert path in console.SECRET_ARGUMENTS, argv


def test_the_value_tables_name_real_arguments() -> None:
    def arguments(path: tuple[str, ...]) -> set[str]:
        command = console.resolve(path, resilient=True).command
        return {p.name or "" for p in command.params if isinstance(p, TyperArgument)}

    for path in console.SETTING_ARGUMENTS | console.SECRET_ARGUMENTS:
        assert console.ARGUMENT in arguments(path), path
    assert console.VALUE_ARGUMENT in arguments(("config", "set"))
    assert set(console.PROFILE_SETTINGS) <= SETTINGS
    improve = console.resolve(("improve",)).command
    assert any(console.PROFILE_OPTION in getattr(p, "opts", ()) for p in improve.params)


def test_resolve_raises_the_cli_usage_errors() -> None:
    for wrong in [
        ("espanso", "deplo"),
        ("history", "prune", "--days", "3"),
        ("config",),
        ("config", "set", "PROMPT_HISTORY"),
        ("doctor", "extra"),
        ("--nope",),
    ]:
        with pytest.raises(UsageError):
            console.resolve(wrong)
    # A half-typed line is fine where it may still become one.
    console.resolve(("config", "set", "PROMPT_HISTORY"), resilient=True)
    console.resolve(("history", "prune", "--older-than"), resilient=True)
    with pytest.raises(UsageError, match="Missing option --from"):
        console.resolve(("config", "retire"))
    with pytest.raises(UsageError, match="Missing argument VALUE"):
        console.resolve(("config", "set", "PROMPT_HISTORY"))
    assert console.resolve(("--version",), partial=True).path == ()


def test_resolve_runs_no_callback(capsys: pytest.CaptureFixture[str]) -> None:
    console.resolve(("--version",), partial=True)
    console.resolve(("doctor", "--help"))
    assert capsys.readouterr().out == ""


# --- The help line ----------------------------------------------------------------------------


def test_help_for_a_valid_command() -> None:
    text = console.describe("espanso status --diff").plain
    assert text.startswith("Usage: promptmend espanso status [OPTIONS]")
    assert "\n" in text
    group = console.describe("config").plain
    assert "Commands: show, get, set" in group
    assert console.describe("promptmend doctor").plain.startswith("Usage: promptmend doctor")


def test_help_for_a_usage_error_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    assert console.describe("espanso deplo").plain == "error: No such command 'deplo'."
    # Behind the withheld notice, a usage error quoting a key is redacted too.
    monkeypatch.setattr(console, "holds_a_key", lambda _: False)
    text = console.describe(f"espanso {KEY}").plain
    assert text.startswith("error: No such command")
    assert KEY not in text
    assert "<redacted" in text
    assert console.describe('config set "x').plain == "error: No closing quotation"


def test_help_while_typing_explains_the_word_being_typed() -> None:
    text = console.describe("espanso st", typing=True).plain
    assert text.startswith("Usage: promptmend espanso [OPTIONS] COMMAND")
    assert text.endswith("Matches: status")
    assert console.describe("espanso zz", typing=True).plain.startswith("error:")
    assert console.describe("espanso st ", typing=True).plain.startswith("error:")


def test_empty_line_shows_recipes() -> None:
    text = console.describe("").plain
    for recipe in teach.RECIPES[: console.HELP_RECIPES]:
        assert f"$ {teach.equivalent(*recipe.argv)}" in text


def test_a_key_is_never_echoed() -> None:
    for line in (f"config set X {KEY}", KEY, f"secrets set {KEY}", f'config set X "{KEY}'):
        for typing in (True, False):
            text = console.describe(line, typing=typing).plain
            assert KEY not in text
            assert KEY[:12] not in text
            assert text.startswith(teach.WITHHELD)
    assert console.suggest(f"config set X {KEY}") is None


# --- In the interface -------------------------------------------------------------------------

SIZE = (120, 50)


async def settle(pilot: Pilot[int]) -> None:
    for _ in range(4):
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def drive(scenario: Callable[[ManageApp, Pilot[int]], Awaitable[None]]) -> ManageApp:
    app = ManageApp(intro=False)

    async def main() -> None:
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await scenario(app, pilot)

    asyncio.run(main())
    return app


def _line(app: ManageApp) -> tui_console.CommandLine:
    return app.main.query_one("#home-command", tui_console.CommandLine)


def _help(app: ManageApp) -> str:
    return str(app.main.query_one("#home-command-help", Static).render())


def _tab(app: ManageApp) -> str:
    return app.main.query_one(TabbedContent).active


class FakeRun:
    """A runner that records each argv and answers with ``ran``."""

    def __init__(self, ran: console.Ran | None = None) -> None:
        self.ran = ran or console.Ran(0, "fine\n")
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> console.Ran:
        self.calls.append(tuple(argv))
        return self.ran


@pytest.fixture
def nothing_runs(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Once the state has loaded, any subprocess or smoke test fails the test."""

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("the command line ran something")

    def arm() -> None:
        monkeypatch.setattr(subprocess, "run", refuse)
        monkeypatch.setattr(subprocess, "Popen", refuse)
        monkeypatch.setattr(smoke, "run", refuse)

    return arm


def test_not_focused_at_launch_and_c_focuses_it() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert not _line(app).has_focus
        await pilot.press("3")
        assert _tab(app) == "profiles"
        await pilot.press("c")
        await pilot.pause()
        assert _tab(app) == "home"
        assert _line(app).has_focus
        # Every binding's key is text while it has the focus.
        await pilot.press(*"qrta12c")
        await pilot.pause()
        assert _line(app).value == "qrta12c"
        assert _tab(app) == "home"
        assert app.is_running
        assert app.theme != "high-contrast"
        await pilot.press("escape")
        assert not _line(app).has_focus
        await pilot.press("2")
        assert _tab(app) == "settings"

    drive(scenario)


def test_typing_shows_suggestion_and_help() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await pilot.press("c", *"espanso st")
        await settle(pilot)
        assert _line(app)._suggestion == "espanso status"
        assert "Matches: status" in _help(app)
        await pilot.press("tab")
        assert _line(app).value == "espanso status"
        await settle(pilot)
        assert _help(app).startswith("Usage: promptmend espanso status")
        # With no suggestion, Tab moves on as anywhere else.
        await pilot.press("tab")
        assert not _line(app).has_focus

    drive(scenario)


def test_enter_runs_it_and_up_recalls(nothing_runs: Callable[[], None]) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        nothing_runs()
        line = _line(app)
        fake = FakeRun()
        line.runner = fake
        await pilot.press("c", *"doctor --json", "enter")
        await settle(pilot)
        assert line.value == ""
        assert fake.calls == [("doctor", "--json", "--no-clipboard")]
        assert line.history == ["doctor --json"]
        await pilot.press(*"stats", "enter", "up")
        assert line.value == "stats"
        await pilot.press("up")
        assert line.value == "doctor --json"
        await pilot.press("up")
        assert line.value == "doctor --json"
        await pilot.press("down", "down")
        assert line.value == ""
        # An empty Enter or an open quote keeps nothing.
        await pilot.press("enter", *'config set "x', "enter")
        await settle(pilot)
        assert line.history == ["doctor --json", "stats"]
        assert "No closing quotation" in _help(app)
        await pilot.press("ctrl+u", *"espanso deplo", "enter")
        await settle(pilot)
        assert _help(app) == "error: No such command 'deplo'."
        await pilot.press("up")
        assert line.value == "espanso deplo"
        await settle(pilot)
        assert [e.command for e in app.session] == [
            "promptmend doctor --json --no-clipboard",
            "promptmend stats",
        ]
        assert fake.calls[1:] == [("stats",)]

    drive(scenario)


def test_a_key_line_is_cleared_and_never_recalled(nothing_runs: Callable[[], None]) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        nothing_runs()
        line = _line(app)
        await pilot.press("c")
        line.value = f"config set X {KEY}"
        await settle(pilot)
        assert KEY not in _help(app)
        await pilot.press("enter")
        await settle(pilot)
        assert line.value == ""
        assert line.history == []
        assert "Settings" in _help(app)
        assert KEY not in _help(app)
        await pilot.press("up")
        assert line.value == ""
        assert all(KEY not in entry.command for entry in app.session)

    drive(scenario)


def test_the_history_is_capped() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        line = _line(app)
        await pilot.press("c")
        for n in range(tui_console.HISTORY_SIZE + 5):
            line.value = f"stats --by {n}"
            await line.action_submit()
        assert len(line.history) == tui_console.HISTORY_SIZE
        assert line.history[-1] == f"stats --by {tui_console.HISTORY_SIZE + 4}"

    drive(scenario)


# --- What Enter does --------------------------------------------------------------------------

LEAVES = {
    path
    for path in PATHS
    if not isinstance(console.resolve(path, resilient=True, partial=True).command, TyperGroup)
}


def test_every_visible_command_has_a_rule() -> None:
    assert set(console.POLICY) == LEAVES
    kinds = {console.RUN, console.DIALOG, console.TERMINAL, console.REFUSE}
    assert {rule.kind for rule in console.POLICY.values()} == kinds
    for path, rule in console.POLICY.items():
        if rule.dry_run:
            command = console.resolve(path, resilient=True).command
            assert any(console.DRY_RUN in getattr(p, "opts", ()) for p in command.params), path


def _decide(line: str) -> console.Decision:
    words = console.split(line)
    try:
        found = console.resolve(words)
    except UsageError:
        lenient = console.loose(words)
        assert lenient is not None, line
        found = lenient
    return console.decide(found, words)


@pytest.mark.parametrize(
    ("line", "kind", "argv"),
    [
        ("doctor", console.RUN, ("doctor", "--no-clipboard")),
        ("doctor --clipboard", console.RUN, ("doctor", "--clipboard")),
        ("doctor --no-clipboard", console.RUN, ("doctor", "--no-clipboard")),
        ("promptmend config show", console.RUN, ("config", "show")),
        ("config set PROMPT_HISTORY false", console.RUN, None),
        ("espanso status --diff", console.RUN, None),
        ("history export --format csv", console.RUN, None),
        ("espanso deploy", console.DIALOG, None),
        ("espanso deploy --dry-run", console.RUN, None),
        ("config migrate", console.DIALOG, None),
        ("config migrate --dry-run", console.RUN, None),
        ("config migrate --from /old", console.TERMINAL, None),
        ("espanso deploy --espanso-dir /e", console.TERMINAL, None),
        ("config rollback", console.TERMINAL, None),
        ("config rollback --dry-run", console.RUN, None),
        ("profiles migrate", console.TERMINAL, None),
        ("profiles migrate --dry-run", console.RUN, None),
        ("setup", console.TERMINAL, None),
        ("secrets remove", console.DIALOG, None),
        ("secrets set OPENROUTER_API_KEY", console.DIALOG, None),
        ("history prune", console.DIALOG, None),
        ("history reset --yes", console.DIALOG, None),
        ("improve", console.REFUSE, None),
        ("persona", console.REFUSE, None),
        ("ui", console.REFUSE, None),
        ("improve --help", console.RUN, ("improve", "--help")),
        ("config set --help", console.RUN, ("config", "set", "--help")),
        ("config --help", console.RUN, None),
        ("--version", console.RUN, ("--version",)),
    ],
)
def test_policy_decisions(line: str, kind: str, argv: tuple[str, ...] | None) -> None:
    decision = _decide(line)
    assert decision.kind == kind
    if argv is not None:
        assert decision.argv == argv
    if kind == console.TERMINAL:
        assert decision.message.startswith("Quit and run it in a terminal: $ promptmend ")
    if kind == console.REFUSE:
        assert decision.message.startswith("Not from here:")


# --help or --dry-run taken as another option's value: the parser did not read them as
# flags, so they decide nothing (review of #111 PR 5).
BYPASSES = {
    "improve --profile --help": console.REFUSE,
    "persona --trigger-id --help": console.REFUSE,
    "setup --migrate-from --help": console.TERMINAL,
    "espanso deploy --launcher --help --yes": console.TERMINAL,
    "espanso detach --espanso-dir --help --yes": console.TERMINAL,
    "history prune --older-than --help --yes": console.DIALOG,
    "espanso deploy --espanso-dir --dry-run --yes": console.TERMINAL,
    "espanso deploy --launcher --dry-run --yes": console.TERMINAL,
    "config migrate --from --dry-run --yes": console.TERMINAL,
}


@pytest.mark.parametrize("line", BYPASSES)
def test_a_swallowed_help_or_dry_run_never_runs(line: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _decide(line).kind == BYPASSES[line]
    words = console.split(line)
    assert not console.runnable(words)

    def spawn(*_: Any, **__: Any) -> Any:
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "run", spawn)
    with pytest.raises(ValueError, match="refused to run"):
        REAL_RUN(words)


def test_runnable_is_a_fresh_decision_on_the_exact_argv() -> None:
    assert console.runnable(["doctor", "--no-clipboard"])
    assert not console.runnable(["doctor"])  # decide() would add --no-clipboard
    assert console.runnable(["--version"])
    assert console.runnable(["config", "set", "--help"])
    assert console.runnable(["espanso", "deploy", "--dry-run"])
    for argv in (
        ["improve"],
        ["espanso", "deploy"],
        ["espanso", "deploy", "--espanso-dir", "/e", "--dry-run"],
        ["secrets", "set", "OPENROUTER_API_KEY"],
        ["history", "reset", "--yes"],
        ["ui"],
        ["doctor", "extra"],
        ["config"],
    ):
        assert not console.runnable(argv), argv


def test_dialog_decisions_carry_values_and_ignored_options() -> None:
    prune = _decide("history prune --older-than 9 -y")
    assert prune.values["older_than"] == "9"
    assert prune.ignored == ("-y",)
    assert _decide("history prune --older-than").values.get("older_than") is None
    assert _decide("espanso detach --remove-all").values["keep_static"] is False
    deploy = _decide("espanso deploy --on-conflict=ours --no-restart")
    assert deploy.ignored == ("--on-conflict=ours", "--no-restart")
    assert _decide("secrets remove ANTHROPIC_API_KEY").values["name"] == "ANTHROPIC_API_KEY"
    # A group with only its own options needs a command.
    group = console.decide(console.resolve(["config"], partial=True), ["config"])
    assert group.kind == console.REFUSE
    assert group.message == "config needs a command."
    assert console.loose(["doctor", "extra"]) is None
    assert console.loose(["config", "set", "X"]) is None  # no --help, no dialog


def test_secrets_set_takes_only_a_key_name() -> None:
    assert not console.refused_secret(["secrets", "set"])
    assert not console.refused_secret(["secrets", "set", "OPENROUTER_API_KEY"])
    assert not console.refused_secret(["secrets", "set", "--help"])
    assert not console.refused_secret(["config", "set", "A", "B"])
    for rest in (
        ["OPENROUTER_API_KEY", "value"],
        ["OPENROUTER_API_KEY", "--stdin"],
        ["not-a-key-name"],
        [KEY],
        ["OPENROUTER_API_KEY", "--help", "Hunter2xyz"],
        ["--help", "Hunter2xyz"],
    ):
        assert console.refused_secret(["secrets", "set", *rest]), rest


def test_config_never_takes_a_key_value() -> None:
    # A key's name with a value after it under config, or a key-like value: the value would
    # reach a child's argv before the command refuses it.
    for words in (
        ["config", "set", "OPENROUTER_API_KEY", "hunter2"],
        ["config", "get", "ANTHROPIC_API_KEY", "hunter2"],
        ["config", "unset", "OPENROUTER_API_KEY", "x"],
        ["config", "set", "PROMPT_PROFILE", KEY],
    ):
        assert console.refused_secret(words), words
    assert not console.refused_secret(["config", "get", "OPENROUTER_API_KEY"])
    assert not console.refused_secret(["config", "set", "PROMPT_PROFILE", "general"])
    assert not console.refused_secret(["config", "show"])


def test_shown_withholds_keys_and_the_persona() -> None:
    assert console.shown(["config", "set", "X", KEY]) == f"promptmend config set X {teach.WITHHELD}"
    assert console.shown(["config", "set", "PROMPT_PERSONA", "I am"]) == (
        f"promptmend config set PROMPT_PERSONA {teach.WITHHELD}"
    )
    assert console.shown(["config", "get", "PROMPT_PERSONA"]) == (
        "promptmend config get PROMPT_PERSONA"
    )


# --- The runner -------------------------------------------------------------------------------


def test_the_runner_starts_this_cli_without_shell_stdin_or_colour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["args"], seen["kwargs"] = args, kwargs
        return subprocess.CompletedProcess(args, 4, stdout="out\n")

    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setenv("SOMETHING", "kept")
    ran = REAL_RUN(["doctor", "--no-clipboard"])
    assert ran == console.Ran(4, "out\n")
    assert seen["args"] == [
        sys.executable,
        "-P",
        "-m",
        "promptmend.cli",
        "doctor",
        "--no-clipboard",
    ]
    kwargs = seen["kwargs"]
    assert "shell" not in kwargs
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["timeout"] == console.TIMEOUT == 120
    assert kwargs["env"]["NO_COLOR"] == "1"
    assert kwargs["env"]["SOMETHING"] == "kept"
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def test_the_runner_reports_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(args: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=b"half")

    monkeypatch.setattr(subprocess, "run", slow)
    ran = REAL_RUN(["stats"])
    assert ran == console.Ran(None, "half")
    shown = console.transcript(["stats"], ran)
    assert shown.status == "timeout after 120 s"
    assert shown.error
    assert console._capped(None) == ""


def test_the_runner_caps_the_output(monkeypatch: pytest.MonkeyPatch) -> None:
    big = "x" * (console.OUTPUT_CAP + 10)
    monkeypatch.setattr(
        subprocess, "run", lambda args, **_: subprocess.CompletedProcess(args, 0, stdout=big)
    )
    ran = REAL_RUN(["config", "show"])
    assert ran.output.endswith("\n" + console.CUT)
    assert len(ran.output) == console.OUTPUT_CAP + 1 + len(console.CUT)


def test_the_transcript_redacts_each_line() -> None:
    ran = console.Ran(0, f"\n  first line\nOPENROUTER_API_KEY={KEY}\n")
    shown = console.transcript(["config", "show"], ran)
    assert shown.command == "promptmend config show"
    assert KEY not in "\n".join(shown.lines)
    assert "<redacted" in shown.lines[2]
    assert shown.summary == "first line"
    assert shown.status == "exit 0"
    assert not shown.error
    assert console.transcript(["stats"], console.Ran(2, "")).summary == "exit 2"


def test_a_real_child_prints_the_version() -> None:
    from promptmend import __version__

    ran = REAL_RUN(["--version"])
    assert ran.exit_code == 0
    assert __version__ in ran.output


# --- Running from the interface ---------------------------------------------------------------


def _output(app: ManageApp) -> str:
    log = app.main.query_one("#home-output", RichLog)
    return "\n".join(strip.text for strip in log.lines)


def _home(app: ManageApp) -> home_pane.HomePane:
    return app.main.query_one("#home-pane", home_pane.HomePane)


def test_a_run_shows_its_output_logs_it_and_reloads() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        assert not app.main.query_one("#home-output", RichLog).display
        fake = FakeRun(console.Ran(1, f"first\nkey {KEY}\n"))
        _line(app).runner = fake
        before = app.generation
        await pilot.press("c", *"config validate", "enter")
        await settle(pilot)
        assert fake.calls == [("config", "validate")]
        assert app.main.query_one("#home-output", RichLog).display
        shown = _output(app)
        assert "$ promptmend config validate" in shown
        assert "first" in shown
        assert KEY not in shown
        assert "exit 1" in shown
        assert app.session[-1] == teach.Entry("promptmend config validate", "first", True)
        assert app.generation > before
        assert not _home(app).running

    drive(scenario)


def test_a_runner_that_fails_is_shown_as_output() -> None:
    def broken(argv: Sequence[str]) -> console.Ran:
        raise OSError("no interpreter")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        _line(app).runner = broken
        await pilot.press("c", *"stats", "enter")
        await settle(pilot)
        assert "error: no interpreter" in _output(app)
        assert app.session[-1].error
        assert not _home(app).running

    drive(scenario)


def test_enter_during_a_run_starts_nothing() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        fake = FakeRun()
        line = _line(app)
        line.runner = fake
        _home(app).running = True
        await pilot.press("c", *"stats", "enter")
        await settle(pilot)
        assert fake.calls == []
        assert _help(app) == tui_console.RUNNING
        assert line.value == "stats"
        assert app.session == []

    drive(scenario)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("setup", "Quit and run it in a terminal: $ promptmend setup"),
        ("config retire --from /old", "Quit and run it in a terminal: $ promptmend config"),
        ("improve", "Not from here: the Espanso triggers run it"),
        ("persona", "Not from here: the -p- trigger"),
        ("ui", "Not from here: this is the interface"),
    ],
)
def test_refused_lines_run_nothing(line: str, expected: str) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        fake = FakeRun()
        _line(app).runner = fake
        await pilot.press("c", *line, "enter")
        await settle(pilot)
        assert fake.calls == []
        assert _help(app).startswith(expected)
        assert _line(app).history == [line]
        assert app.session == []
        assert _tab(app) == "home"

    drive(scenario)


@pytest.mark.parametrize("line", BYPASSES)
def test_bypass_lines_start_nothing_from_the_interface(line: str) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        fake = FakeRun()
        _line(app).runner = fake
        await pilot.press("c")
        _line(app).value = line
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)
        assert fake.calls == []
        assert not _home(app).running

    drive(scenario)


def test_run_line_refuses_what_a_fresh_parse_would_not_run() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        fake = FakeRun()
        _home(app).run_line(("improve", "--profile", "--help"), fake)
        await settle(pilot)
        assert fake.calls == []
        assert not _home(app).running
        assert _home(app).last_message.startswith("error: refused to run")

    drive(scenario)


@pytest.mark.parametrize(
    "rest",
    [
        "OPENROUTER_API_KEY my-value",
        "OPENROUTER_API_KEY --stdin",
        "hunter2",
        KEY,
        "OPENROUTER_API_KEY --help Hunter2xyz",
    ],
)
def test_secrets_set_with_a_value_is_cleared_and_not_kept(rest: str) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        line = _line(app)
        await pilot.press("c")
        line.value = f"secrets set {rest}"
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        assert line.value == ""
        assert line.history == []
        assert rest not in _help(app)
        assert teach.WITHHELD in _help(app) or "Settings" in _help(app)
        assert app.session == []
        await pilot.press("up")
        assert line.value == ""

    drive(scenario)


# --- Recipes and Copy command (#111) ------------------------------------------------------------


def _picker(app: ManageApp) -> PickModal:
    screen = app.screen
    assert isinstance(screen, PickModal)
    return screen


async def _open_recipes(app: ManageApp, pilot: Pilot[int]) -> PickModal:
    app.main.query_one("#home-recipes", Button).press()
    await settle(pilot)
    picker = _picker(app)
    assert picker.query_one("#pick", OptionList).has_focus
    return picker


async def _pick(app: ManageApp, pilot: Pilot[int], argv: tuple[str, ...]) -> None:
    picker = await _open_recipes(app, pilot)
    options = picker.query_one("#pick", OptionList)
    options.highlighted = [r.argv for r in teach.RECIPES].index(argv)
    await pilot.press("enter")
    await settle(pilot)


def test_recipes_list_every_recipe_with_what_it_does() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        picker = await _open_recipes(app, pilot)
        options = picker.query_one("#pick", OptionList)
        shown = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert len(shown) == len(teach.RECIPES)
        for line, recipe in zip(shown, teach.RECIPES, strict=True):
            assert line.startswith(f"$ {teach.equivalent(*recipe.argv)} ")
            assert line.endswith(recipe.what)

    drive(scenario)


def test_a_recipe_prefills_the_line_and_runs_nothing(nothing_runs: Callable[[], None]) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        nothing_runs()
        fake = FakeRun()
        line = _line(app)
        line.runner = fake
        await _pick(app, pilot, ("stats", "--by", "model"))
        assert not isinstance(app.screen, PickModal)
        assert line.value == "stats --by model"
        assert line.has_focus
        assert line.cursor_position == len(line.value)
        assert _help(app).startswith("Usage: promptmend stats")
        assert fake.calls == []
        assert app.session == []
        assert line.history == []
        # Enter then runs it, as typed.
        await pilot.press("enter")
        await settle(pilot)
        assert fake.calls == [("stats", "--by", "model")]

    drive(scenario)


def test_the_key_recipe_prefills_only_the_name(nothing_runs: Callable[[], None]) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        nothing_runs()
        line = _line(app)
        await _pick(app, pilot, ("secrets", "set", "OPENROUTER_API_KEY"))
        assert line.value == "secrets set OPENROUTER_API_KEY"
        assert app.session == []
        # Enter opens the hidden key dialog, as for a typed line.
        await pilot.press("enter")
        await settle(pilot)
        assert _tab(app) == "settings"
        assert isinstance(app.screen, FormModal)
        assert app.screen.values() == {"key-name": "OPENROUTER_API_KEY", "key-value": ""}

    drive(scenario)


@pytest.mark.parametrize("how", ["esc", "btn"])
def test_cancel_keeps_the_typed_line(how: str) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        line = _line(app)
        await pilot.press("c", *"doctor --js")
        await settle(pilot)
        help_before = _help(app)
        await _open_recipes(app, pilot)
        if how == "esc":
            await pilot.press("escape")
        else:
            _picker(app).query_one("#cancel", Button).press()
        await settle(pilot)
        assert not isinstance(app.screen, PickModal)
        assert line.value == "doctor --js"
        assert _help(app) == help_before

    drive(scenario)


@pytest.fixture
def no_pyperclip(monkeypatch: pytest.MonkeyPatch) -> None:
    def touched(*_: Any) -> Any:
        raise AssertionError("pyperclip was used")

    monkeypatch.setattr(pyperclip, "paste", touched)
    monkeypatch.setattr(pyperclip, "copy", touched)


def test_copy_command_copies_the_latest_command(
    no_pyperclip: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
        button = app.main.query_one("#home-copy", Button)
        assert button.disabled
        _line(app).runner = FakeRun()
        await pilot.press("c", *"stats", "enter")
        await settle(pilot)
        await pilot.press(*"doctor --json", "enter")
        await settle(pilot)
        assert not button.disabled
        button.press()
        await settle(pilot)
        assert copied == ["promptmend doctor --json --no-clipboard"]
        assert _home(app).last_message == (
            "Sent to the terminal clipboard (OSC 52): promptmend doctor --json --no-clipboard"
        )
        # Copying is not an action of its own in the session log.
        assert len(app.session) == 2

    drive(scenario)


def test_copy_command_with_an_empty_session_copies_nothing(
    no_pyperclip: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
        _home(app)._copy()
        await settle(pilot)
        assert copied == []

    drive(scenario)


@pytest.mark.parametrize("recipe", teach.RECIPES, ids=lambda r: r.argv[0][:6] + str(len(r.argv)))
def test_a_recipe_line_splits_back_and_parses(recipe: teach.Recipe) -> None:
    line = teach.line(*recipe.argv)
    assert console.split(line) == list(recipe.argv)
    console.resolve(console.split(line))


def test_line_quotes_a_value_that_needs_it() -> None:
    line = teach.line("config", "set", "X", "a b'c")
    assert line.startswith("config set X ")
    assert console.split(line) == ["config", "set", "X", "a b'c"]
