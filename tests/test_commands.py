"""The management commands (#92): setup, config, secrets, profiles, stats, history, doctor and
--version. They run offline: Espanso, uv and brew through a fake runner, the clipboard mocked,
and setup's smoke test against its own stub on 127.0.0.1."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import typer

# typer vendors click (no click package): get_command() returns its classes.
from typer._click.core import Command, Context, Parameter
from typer.testing import CliRunner, Result

from promptmend import (
    __version__,
    assets,
    config,
    config_store,
    deploy,
    doctor,
    history,
    settings_service,
    setup_guide,
    smoke,
)
from promptmend.cli import app
from promptmend.commands import common
from promptmend.config import secret_names
from promptmend.history import HistoryStore

if TYPE_CHECKING:
    from conftest import SeedHistory

runner = CliRunner()
LAUNCHER = "/Users/me/.local/bin/promptmend"
# Built at runtime, so no key-shaped literal lands in the repo (gitleaks).
KEY = "sk-or-v1-" + "ab12" * 16
PERSONA = "I am the sentinel persona of Example Corp."
ALLOWED_EXITS = {0, 1, 2, 3, 4}


class FakeRunner:
    def __init__(
        self, answers: Mapping[str, str | deploy.CommandFailure | None] | None = None
    ) -> None:
        self.answers = dict(answers or {})
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str | deploy.CommandFailure | None:
        self.calls.append(list(argv))
        return self.answers.get(" ".join(argv))


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    fake = FakeRunner({"espanso restart": ""})
    monkeypatch.setattr(deploy, "run_command", fake)
    return fake


def _changes(calls: list[list[str]]) -> list[list[str]]:
    """The commands that change something: the launcher lookup reads `uv tool dir` too."""
    return [call for call in calls if call != ["uv", "tool", "dir"]]


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Saved mode: no PROMPTMEND_ENV, so config.toml and the secrets file are read (in
    conftest's per-test config dir)."""
    monkeypatch.delenv("PROMPTMEND_ENV")
    return config.user_config_dir()


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "stdin_is_tty", lambda: True)


@pytest.fixture
def clipboard(monkeypatch: pytest.MonkeyPatch) -> str:
    import pyperclip

    from promptmend import clipboard_guard

    text = "clipboard sentinel text 42"
    monkeypatch.setattr(pyperclip, "paste", lambda: text)
    monkeypatch.setattr(clipboard_guard, "is_concealed", lambda: None)
    return text


@pytest.fixture
def espanso(tmp_path: Path) -> Path:
    root = tmp_path / "espanso"
    (root / "match").mkdir(parents=True)
    return root


def _run(*args: str, input: str | None = None) -> Result:
    return runner.invoke(app, list(args), input=input)


def _plain(text: str) -> str:
    """``text`` without ANSI colours, Rich's panel borders or line wrapping: Typer's help and
    error panels are coloured and boxed under GitHub Actions (FORCE_COLOR, COLUMNS)."""
    # Colours go without a trace (Rich may colour the two halves of `--runs` apart); borders
    # become spaces.
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", text)).split())


def _tree(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


# --- --version, help, lazy loading --------------------------------------------------------


def test_version() -> None:
    result = _run("--version")
    assert result.exit_code == 0
    assert result.stdout == f"{__version__}\n"


def test_help_lists_every_command() -> None:
    result = _run("--help")
    assert result.exit_code == 0
    for name in (
        "improve",
        "persona",
        "espanso",
        "setup",
        "config",
        "secrets",
        "profiles",
        "stats",
        "history",
        "doctor",
    ):
        assert name in result.stdout


# --- exit codes, NO_COLOR -----------------------------------------------------------------


def test_unknown_setting_is_a_usage_error() -> None:
    result = _run("config", "get", "NOPE")
    assert result.exit_code == 2


def test_no_terminal_never_prompts(saved: Path) -> None:
    config_store_secret(saved)
    result = _run("secrets", "remove", "OPENROUTER_API_KEY")
    assert result.exit_code == common.NEEDS_TERMINAL
    assert "not a terminal" in result.stderr
    assert "OPENROUTER_API_KEY" in (saved / "secrets.toml").read_text("utf-8")


@pytest.mark.parametrize(
    ("no_color", "tty_out", "colored"),
    [
        ("", True, True),
        ("1", True, False),
        ("", False, False),
    ],
)
def test_no_color(
    monkeypatch: pytest.MonkeyPatch, no_color: str, tty_out: bool, colored: bool
) -> None:
    monkeypatch.setenv("NO_COLOR", no_color)
    monkeypatch.setattr(common, "stdout_is_tty", lambda: tty_out)
    assert ("\x1b[" in common.paint("ok", "green")) is colored


# --- config -------------------------------------------------------------------------------


def config_store_secret(directory: Path, value: str = KEY) -> None:
    from promptmend import config_store

    config_store.save_secret("OPENROUTER_API_KEY", value)
    assert (directory / "secrets.toml").is_file()


def test_config_show_provenance_and_masking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        f"PROMPT_PROFILE=general\nPROMPT_PERSONA={PERSONA}\nOPENROUTER_API_KEY={KEY}\n", "utf-8"
    )
    monkeypatch.setenv("PROMPT_PROFILE", "default")
    result = _run("config", "show", "--raw")
    assert result.exit_code == 0, result.output
    line = next(x for x in result.stdout.splitlines() if x.startswith("PROMPT_PROFILE "))
    assert "[environment; overrides" in line
    assert str(tmp_path / ".env") in line
    assert KEY not in result.output
    assert PERSONA not in result.output
    assert "<set>" in result.stdout
    # Not even a key's length (#222).
    assert f"{len(KEY)} chars" not in result.stdout


def test_config_get_never_prints_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    result = _run("config", "get", "OPENROUTER_API_KEY")
    assert result.exit_code == 1
    assert KEY not in result.output
    monkeypatch.setenv("PROMPT_PROFILE", "general")
    assert _run("config", "get", "PROMPT_PROFILE").stdout == "general\n"


def test_config_set_and_unset(saved: Path) -> None:
    result = _run("config", "set", "PROMPT_PROFILE", "general")
    assert result.exit_code == 0, result.output
    assert 'PROMPT_PROFILE = "general"' in (saved / "config.toml").read_text("utf-8")
    assert _run("config", "get", "PROMPT_PROFILE").stdout == "general\n"
    assert _run("config", "unset", "PROMPT_PROFILE").exit_code == 0
    assert "PROMPT_PROFILE" not in (saved / "config.toml").read_text("utf-8")


def test_config_set_validates(saved: Path) -> None:
    result = _run("config", "set", "PROMPT_LOCAL_ONLY", "maybe")
    assert result.exit_code == 1
    assert "true or false" in result.stderr
    assert not (saved / "config.toml").exists()


# OPENROUTER_PRO_MAX_TOKENS takes a whole number above 0, or empty to inherit
# OPENROUTER_MAX_TOKENS (#31).
def test_config_set_pro_max_tokens(saved: Path) -> None:
    result = _run("config", "set", "OPENROUTER_PRO_MAX_TOKENS", "0")
    assert result.exit_code == 1
    assert "empty or a whole number above 0" in result.stderr
    assert _run("config", "set", "OPENROUTER_PRO_MAX_TOKENS", "4000").exit_code == 0
    assert "OPENROUTER_PRO_MAX_TOKENS = 4000" in (saved / "config.toml").read_text("utf-8")
    assert _run("config", "validate").exit_code == 0
    assert _run("config", "set", "OPENROUTER_PRO_MAX_TOKENS", "").exit_code == 0
    assert _run("config", "get", "OPENROUTER_PRO_MAX_TOKENS").stdout == "\n"
    assert _run("config", "validate").exit_code == 0


def test_config_set_refuses_a_secret_and_a_key_shaped_value(saved: Path) -> None:
    for name in secret_names():
        result = _run("config", "set", name, KEY)
        assert result.exit_code == 2
        assert KEY not in result.output
    result = _run("config", "set", "PROMPT_PERSONA", KEY)
    assert result.exit_code == 2
    assert KEY not in result.output
    assert not (saved / "config.toml").exists()


def test_config_set_refuses_in_legacy_mode(tmp_path: Path) -> None:
    result = _run("config", "set", "PROMPT_PROFILE", "general")
    assert result.exit_code == 1
    assert "PROMPTMEND_ENV is set" in result.stderr
    assert not config.settings_file().exists()


# A .env in the user config folder is no longer read (#275), so saving config.toml cannot
# orphan it: `config set` saves, and `config validate` reports the unread file.
def test_config_set_saves_beside_an_unread_dotenv(saved: Path) -> None:
    env = _dotenv(saved)
    result = _run("config", "set", "PROMPT_TIMEOUT_SECONDS", "10")
    assert result.exit_code == 0, result.output
    assert "PROMPT_TIMEOUT_SECONDS = 10" in (saved / "config.toml").read_text("utf-8")
    result = _run("config", "validate")
    assert result.exit_code == common.PROBLEMS
    assert f"{env}: .env is no longer read" in result.stdout
    assert KEY not in result.output


def test_config_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run("config", "validate").exit_code == 0
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "maybe")
    result = _run("config", "validate")
    assert result.exit_code == common.PROBLEMS
    assert "PROMPT_LOCAL_ONLY must be true or false" in result.stdout


def test_config_validate_and_set_reject_an_invalid_extra_pattern(
    monkeypatch: pytest.MonkeyPatch, saved: Path
) -> None:
    result = _run("config", "set", "PROMPT_EXTRA_PATTERNS", "falcon;(")
    assert result.exit_code == 1
    assert "entry 2 (custom_2) is not a valid regex" in result.stderr
    assert not (saved / "config.toml").exists()
    monkeypatch.setenv("PROMPT_EXTRA_PATTERNS", "falcon;(")
    result = _run("config", "validate")
    assert result.exit_code == common.PROBLEMS
    assert "PROMPT_EXTRA_PATTERNS must be" in result.stdout
    assert "falcon" not in result.output


# A .env value cut at an unquoted ` #` is a problem for `config validate`, named by setting
# only (#32).
def test_config_validate_reports_a_value_cut_at_a_comment(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PROMPT_EXTRA_PATTERNS=ticket #\\d{5}\n")
    result = _run("config", "validate")
    assert result.exit_code == common.PROBLEMS
    assert "the value of PROMPT_EXTRA_PATTERNS was cut at ' #'" in result.stdout
    assert "ticket" not in result.output
    (tmp_path / ".env").write_text(
        'PROMPT_EXTRA_PATTERNS="ticket #\\d{5}"\nOLLAMA_MODEL=m  # an ordinary comment\n'
    )
    assert _run("config", "validate").exit_code == 0


def test_config_validate_redacts_a_value_matching_a_user_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPT_EXTRA_PATTERNS", "falcon")
    monkeypatch.setenv("PROMPT_TIMEOUT_SECONDS", "falcon")
    result = _run("config", "validate")
    assert result.exit_code == common.PROBLEMS
    assert "PROMPT_TIMEOUT_SECONDS must be" in result.stdout
    assert "falcon" not in result.output


# The persona goes with every call, unscanned by the gate: validate scans it once and names
# the findings only (#29 B).
def test_config_validate_scans_the_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run("config", "validate").exit_code == 0  # no persona
    monkeypatch.setenv("PROMPT_PERSONA", "I am a data engineer at Example Corp.")
    assert _run("config", "validate").exit_code == 0
    # One word with a digit and three classes is bare_token in a draft, not in a persona.
    monkeypatch.setenv("PROMPT_PERSONA", "DataOps2Lead")
    assert _run("config", "validate").exit_code == 0
    address = "jane.doe" + "@" + "example.com"
    monkeypatch.setenv("PROMPT_PERSONA", f"I am a falcon analyst, mail {address}.")
    monkeypatch.setenv("PROMPT_EXTRA_PATTERNS", "falcon")
    result = _run("config", "validate")
    assert result.exit_code == common.PROBLEMS
    assert (
        "problem: PROMPT_PERSONA matches the data-protection patterns: email, custom_1; it is "
        "sent unscanned with every cloud call"
    ) in result.stdout
    assert "falcon" not in result.output
    assert address not in result.output
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")  # nothing leaves the machine
    assert _run("config", "validate").exit_code == 0
    monkeypatch.setenv("PROMPT_GATE_LOCAL", "true")  # ...unless localhost may relay
    assert _run("config", "validate").exit_code == common.PROBLEMS
    monkeypatch.delenv("PROMPT_LOCAL_ONLY")
    monkeypatch.setenv("PROMPT_PROFILE", "general")  # no {{PERSONA_RULE}}: never sent
    assert _run("config", "validate").exit_code == 0


def test_config_validate_unknown_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_PROFILE", "nosuch")
    result = _run("config", "validate")
    assert result.exit_code == common.PROBLEMS
    assert "Unknown profile" in result.stdout


def _dotenv(saved: Path) -> Path:
    saved.mkdir(parents=True, exist_ok=True)
    env = saved / ".env"
    env.write_text(f"PROMPT_PROFILE=general\nOPENROUTER_API_KEY={KEY}\n", "utf-8")
    return env


# --- secrets ------------------------------------------------------------------------------


def test_secrets_set_from_stdin_status_and_remove(saved: Path) -> None:
    result = _run("secrets", "set", "OPENROUTER_API_KEY", "--stdin", input=f"{KEY}\n")
    assert result.exit_code == 0, result.output
    assert KEY not in result.output
    status = _run("secrets", "status")
    assert "OPENROUTER_API_KEY  set  from" in status.stdout
    assert "ANTHROPIC_API_KEY   not set" in status.stdout
    assert KEY not in status.output
    assert _run("secrets", "remove", "OPENROUTER_API_KEY", "--yes").exit_code == 0
    assert "not set" in _run("secrets", "status").stdout.splitlines()[-1]


def test_secrets_set_hidden_prompt(saved: Path, tty: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "_getpass", lambda prompt: KEY)
    assert _run("secrets", "set", "ANTHROPIC_API_KEY").exit_code == 0
    assert KEY in (saved / "secrets.toml").read_text("utf-8")


def test_secrets_set_without_terminal_or_stdin(saved: Path) -> None:
    result = _run("secrets", "set", "OPENROUTER_API_KEY")
    assert result.exit_code == common.NEEDS_TERMINAL
    assert "--stdin" in result.stderr


def test_secrets_refused_in_legacy_mode() -> None:
    result = _run("secrets", "set", "OPENROUTER_API_KEY", "--stdin", input=f"{KEY}\n")
    assert result.exit_code == 1
    assert "PROMPTMEND_ENV is set" in result.stderr


def test_secrets_set_rejects_a_non_secret_name(saved: Path) -> None:
    assert _run("secrets", "set", "PROMPT_PROFILE", "--stdin", input="x\n").exit_code == 2


# --- No command accepts a key as an argument ----------------------------------------------


def _leaves(
    command: Command | None, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Command]]:
    from typer.core import TyperGroup

    assert command is not None
    if isinstance(command, TyperGroup):
        ctx = Context(command)
        for name in command.list_commands(ctx):
            yield from _leaves(command.get_command(ctx, name), (*path, name))
    else:
        yield path, command


def _all_leaves() -> dict[tuple[str, ...], Command]:
    import typer

    return dict(_leaves(typer.main.get_command(app)))


# improve takes the draft (--text), which the data-protection gate scans; it is data, never a
# setting. Every other value a command takes is listed here with a harmless stand-in.
TRIGGER_COMMANDS = {("improve",), ("persona",)}
REQUIRED: dict[tuple[str, ...], list[str]] = {
    ("config", "get"): ["PROMPT_PROFILE"],
    ("config", "set"): ["PROMPT_PERSONA", "x"],
    ("config", "unset"): ["PROMPT_PROFILE"],
    ("secrets", "set"): ["OPENROUTER_API_KEY"],
    ("secrets", "remove"): ["OPENROUTER_API_KEY"],
}
BASE: dict[tuple[str, ...], list[str]] = {
    ("setup",): ["--non-interactive", "--no-smoke-test"],
}
# Option names that suggest a secret. Allowed: the improve output cap.
SUSPICIOUS = ("key", "secret", "password", "token", "credential")
ALLOWED_NAMES = {"max_tokens"}  # an output cap


def test_no_option_is_named_like_a_secret() -> None:
    for path, command in _all_leaves().items():
        for param in command.params:
            name = param.name or ""
            if name in ALLOWED_NAMES:
                continue
            flag_only = getattr(param, "is_flag", False)
            assert flag_only or not any(w in name for w in SUSPICIOUS), (path, name)


def test_secrets_set_takes_no_value_argument() -> None:
    command = _all_leaves()[("secrets", "set")]
    values = [p.name for p in command.params if not getattr(p, "is_flag", False)]
    assert values == ["name"]


def _value_params(command: Command) -> list[Parameter]:
    return [p for p in command.params if not getattr(p, "is_flag", False) and p.name != "help"]


@pytest.mark.parametrize("path", sorted(set(_all_leaves()) - TRIGGER_COMMANDS), ids=" ".join)
def test_no_command_accepts_a_key_as_an_argument(
    path: tuple[str, ...],
    saved: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clipboard: str,
) -> None:
    """Each value a command takes, given a key: the key is never saved anywhere and never
    becomes a setting or a secret."""
    monkeypatch.setattr(deploy, "run_command", FakeRunner())
    monkeypatch.setattr(smoke, "run", lambda provider: pytest.fail("no smoke test here"))
    command = _all_leaves()[path]
    for param in _value_params(command):
        args = list(REQUIRED.get(path, []))
        if param.param_type_name == "argument":
            position = [p for p in command.params if p.param_type_name == "argument"].index(param)
            args[position] = KEY
        else:
            args += [param.opts[0], KEY]
        result = _run(*path, *BASE.get(path, []), *args, input="")
        assert result.exit_code in ALLOWED_EXITS, (param.name, result.output)
        assert KEY not in result.output, param.name
        layers = config.ConfigLayers.resolve(strict=False)
        assert KEY not in [e.value for e in layers.entries.values()], param.name
        for file in _tree(tmp_path):
            assert KEY.encode() not in file.read_bytes(), (param.name, file)


# --- Repair mode: every command runs on a broken config ------------------------------------


def _commands(espanso: Path) -> dict[tuple[str, ...], list[list[str]]]:
    where = ["--espanso-dir", str(espanso), "--launcher", LAUNCHER]
    return {
        ("setup",): [
            ["setup", "--non-interactive", "--no-smoke-test", *where],
        ],
        ("config", "show"): [["config", "show"], ["config", "show", "--raw"]],
        ("config", "get"): [["config", "get", "PROMPT_PROFILE"]],
        ("config", "set"): [["config", "set", "PROMPT_PROFILE", "general"]],
        ("config", "unset"): [["config", "unset", "PROMPT_PROFILE"]],
        ("config", "validate"): [["config", "validate"]],
        ("secrets", "set"): [["secrets", "set", "OPENROUTER_API_KEY", "--stdin"]],
        ("secrets", "status"): [["secrets", "status"]],
        ("secrets", "remove"): [["secrets", "remove", "OPENROUTER_API_KEY", "--yes"]],
        ("profiles", "list"): [["profiles", "list"]],
        ("stats",): [["stats"], ["stats", "--json", "--by", "day"]],
        ("history", "export"): [["history", "export"]],
        ("history", "prune"): [["history", "prune"]],
        ("history", "reset"): [["history", "reset", "--yes"]],
        ("doctor",): [
            ["doctor"],
            ["doctor", "--json", *where],
            ["doctor", "--timing", "--runs", "1", "--no-clipboard"],
        ],
        ("espanso", "status"): [["espanso", "status", *where]],
        ("espanso", "deploy"): [["espanso", "deploy", "--dry-run", *where]],
        ("espanso", "detach"): [["espanso", "detach", "--yes"]],
        ("improve",): [["improve", "--source", "argument", "--text", "a draft"]],
        ("persona",): [["persona"]],
        # Without a terminal (as here) the interface exits 3 before reading anything.
        ("ui",): [["ui"]],
    }


def test_the_repair_list_covers_every_command(tmp_path: Path) -> None:
    assert set(_commands(tmp_path)) == set(_all_leaves())


def _break(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if kind == "dotenv":
        (tmp_path / ".env").write_text(
            "PROMPT_LOCAL_ONLY=maybe\nOPENROUTER_MODEL=xPROMPT_PROFILE=general\nno equals\n\xff\n",
            "utf-8",
        )
        return
    monkeypatch.delenv("PROMPTMEND_ENV")
    directory = config.user_config_dir()
    directory.mkdir(parents=True)
    name = "config.toml" if kind == "toml" else "secrets.toml"
    (directory / name).write_text("this = = not toml\n[[\n", "utf-8")


@pytest.mark.parametrize("kind", ["dotenv", "toml", "secrets"])
def test_every_command_runs_on_a_broken_config(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, espanso: Path, clipboard: str
) -> None:
    _break(kind, tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "run_command", FakeRunner())
    monkeypatch.setattr(smoke, "time_runs", _fake_time_runs)
    for argv_list in _commands(espanso).values():
        for argv in argv_list:
            result = _run(*argv, input="")
            assert result.exception is None or isinstance(result.exception, SystemExit), (
                argv,
                result.exception,
            )
            assert result.exit_code in ALLOWED_EXITS, (argv, result.output)
            assert "Traceback" not in result.output, argv


# --- doctor -------------------------------------------------------------------------------


def _doctor(*args: str) -> Result:
    return _run("doctor", "--launcher", LAUNCHER, *args)


def _fake_time_runs(
    argv: Sequence[str], env: Mapping[str, str], runs: int, expect: object
) -> tuple[list[float], str | None]:
    """smoke.time_runs without a child: 0.3 s cold, then 0.1 s a run; persona fails."""
    if "persona" in argv:
        return [0.3], "wrote to stderr"
    return [0.3] + [0.1] * runs, None


def test_plain_doctor_starts_no_child(
    espanso: Path, monkeypatch: pytest.MonkeyPatch, fake_run: FakeRunner
) -> None:
    import subprocess

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"plain doctor started a child: {args}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    for args in ([], ["--json"]):
        result = _doctor("--no-clipboard", "--espanso-dir", str(espanso), *args)
        assert result.exit_code in (0, common.PROBLEMS), result.output
        assert "Timing" not in result.output
    assert "timing" not in json.loads(result.stdout)


def test_doctor_timing_json_is_additive(
    espanso: Path, monkeypatch: pytest.MonkeyPatch, fake_run: FakeRunner
) -> None:
    monkeypatch.setattr(smoke, "time_runs", _fake_time_runs)
    result = _doctor("--no-clipboard", "--espanso-dir", str(espanso), "--json", "--timing")
    data = json.loads(result.stdout)
    assert set(data) == {"schema_version", "version", "status", "checks", "timing"}
    assert data["schema_version"] == 1
    assert tuple(data["checks"]) == doctor.CHECK_IDS
    timing = data["timing"]
    assert set(timing) == {
        "warm_runs",
        "launcher",
        "launcher_note",
        "drift_note",
        "interpreter",
        "runs",
        "checks_ms",
    }
    assert timing["warm_runs"] == 5
    assert timing["launcher"] is None  # LAUNCHER is not a file here
    for run in timing["runs"]:
        assert set(run) == {
            "command",
            "via",
            "status",
            "cold_ms",
            "warm_median_ms",
            "warm_runs",
            "message",
        }
    persona = next(r for r in timing["runs"] if r["command"] == "persona")
    assert (persona["status"], persona["message"]) == ("failed", "wrote to stderr")
    assert set(timing["checks_ms"]) <= set(doctor.CHECK_IDS)
    # The exit code still comes from the checks alone (here the missing key).
    assert result.exit_code == common.PROBLEMS
    assert data["status"] == "fail"


def test_doctor_timing_table(
    espanso: Path, monkeypatch: pytest.MonkeyPatch, fake_run: FakeRunner
) -> None:
    monkeypatch.setattr(smoke, "time_runs", _fake_time_runs)
    result = _doctor("--no-clipboard", "--espanso-dir", str(espanso), "--timing", "--runs", "2")
    out = result.stdout
    assert "Timing against a local stub (no provider call)" in out
    assert "median of 2 warm" in out
    # Normalised as doctor.timing() does it: on Windows the POSIX literal gains a drive letter.
    shown = Path(os.path.abspath(Path(LAUNCHER).expanduser()))
    assert f"launcher:    {shown} is not a file; timed through the interpreter only" in out
    assert re.search(r"--version\s+interpreter\s+300\s+100\n", out)
    assert re.search(r"persona\s+interpreter\s+300\s+-\s+failed: wrote to stderr", out)
    assert "doctor's checks: version " in out
    assert "Traceback" not in result.output
    # The checks come first, the timing after them.
    assert out.index("local_servers:") < out.index("Timing against")


def test_doctor_runs_needs_timing() -> None:
    result = _doctor("--runs", "3")
    assert result.exit_code == common.USAGE
    assert "--runs needs --timing" in _plain(result.output)


@pytest.mark.parametrize("runs", ["0", "51"])
def test_doctor_runs_is_bounded(runs: str) -> None:
    result = _doctor("--timing", "--runs", runs)
    assert result.exit_code == common.USAGE


def test_doctor_json_schema_is_stable(
    espanso: Path, clipboard: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner({"espanso path config": str(espanso), "espanso status": "espanso is running"})
    monkeypatch.setattr(deploy, "run_command", fake)
    result = _doctor("--json")
    data = json.loads(result.stdout)
    assert set(data) == {"schema_version", "version", "status", "checks"}
    assert data["schema_version"] == doctor.SCHEMA_VERSION == 1
    assert tuple(data["checks"]) == doctor.CHECK_IDS
    for check in data["checks"].values():
        assert set(check) == {"status", "message", "data"}
        assert check["status"] in {"ok", "warn", "fail", "info"}
    checks = data["checks"]
    for check_id, check in checks.items():
        assert tuple(check["data"]) == doctor.DATA_KEYS[check_id]
    assert set(checks["keys"]["data"]) == {"keys", "provider", "triggers"}
    assert set(checks["espanso"]["data"]) == {
        "found",
        "config_dir",
        "running",
        "query_failed",
        "exit_code",
        "timed_out",
        "error",
    }
    assert set(checks["match_files"]["data"]) == {"espanso_dir", "files", "legacy"}
    assert set(checks["launcher"]["data"]) == {
        "current",
        "deployed",
        "drift",
        "missing",
        "orphans",
    }
    assert set(checks["clipboard"]["data"]) == {"read", "length", "concealed", "error"}
    assert set(checks["sqlite"]["data"]) == {"version", "wal_reset_bug", "journal_mode"}
    assert checks["espanso"]["data"]["running"] is True
    # The key is missing, so the report fails, with the documented exit code.
    assert data["status"] == "fail"
    assert result.exit_code == common.PROBLEMS
    # Only read-only Espanso commands are run.
    assert {tuple(c) for c in fake.calls if c[0] == "espanso"} <= {
        ("espanso", "path", "config"),
        ("espanso", "status"),
    }


# #115: each way `espanso path config` can fail, as text and as JSON.
_PANIC = deploy.CommandFailure(
    found=True, path="/opt/bin/espanso", returncode=101, error="unable to load config"
)
_ESPANSO_CASES: list[tuple[deploy.CommandFailure | None, str, dict[str, Any]]] = [
    (
        None,
        "espanso was not found on PATH",
        {"found": False, "query_failed": False, "exit_code": None, "timed_out": False},
    ),
    (
        _PANIC,
        "espanso found at /opt/bin/espanso, but `espanso path config` failed (exit 101): "
        "unable to load config; start Espanso once (`espanso start`) or check its config "
        "(using the default folder ",
        {"found": True, "query_failed": True, "exit_code": 101, "timed_out": False},
    ),
    (
        deploy.CommandFailure(found=True, timed_out=True),
        "espanso found, but `espanso path config` timed out after 30 s (using the default ",
        {"found": True, "query_failed": True, "exit_code": None, "timed_out": True},
    ),
]


@pytest.mark.parametrize(("answer", "message", "data"), _ESPANSO_CASES)
def test_doctor_tells_espanso_failures_apart(
    answer: deploy.CommandFailure | None,
    message: str,
    data: dict[str, Any],
    clipboard: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRunner({"espanso status": "espanso is running"})
    fake.answers["espanso path config"] = answer
    monkeypatch.setattr(deploy, "run_command", fake)
    text = _doctor().stdout
    assert f"espanso: {message}" in " ".join(text.split())
    espanso = json.loads(_doctor("--json").stdout)["checks"]["espanso"]
    assert espanso["status"] == "warn"
    assert espanso["message"].startswith(message)
    assert {k: espanso["data"][k] for k in data} == data
    assert espanso["data"]["error"] == (answer.error if answer else None)
    assert espanso["data"]["config_dir"] == str(deploy.default_espanso_dir())
    # Status is asked only when espanso is on PATH; both commands stay read-only.
    assert (["espanso", "status"] in fake.calls) is data["found"]
    assert {tuple(c) for c in fake.calls if c[0] == "espanso"} <= {
        ("espanso", "path", "config"),
        ("espanso", "status"),
    }


@pytest.mark.parametrize(("answer", "message", "data"), _ESPANSO_CASES)
def test_deploy_and_setup_say_when_they_use_the_default_folder(
    answer: deploy.CommandFailure | None,
    message: str,
    data: dict[str, Any],
    saved: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    default = tmp_path / "default-espanso"
    (default / "match").mkdir(parents=True)
    monkeypatch.setattr(deploy, "default_espanso_dir", lambda: default)
    monkeypatch.setattr(deploy, "run_command", FakeRunner({"espanso path config": answer}))
    reason = deploy.locate_espanso_dir().fallback
    notice = f"{reason}; using the default Espanso folder {default.resolve()}"
    result = _run("espanso", "deploy", "--dry-run", "--launcher", LAUNCHER)
    assert result.exit_code == 0, result.output
    assert notice in " ".join(result.stderr.split())
    assert f"Espanso match folder: {default.resolve() / 'match'}" in result.stdout
    result = _run(
        "setup",
        "--non-interactive",
        "--no-smoke-test",
        "--provider",
        "ollama",
        "--launcher",
        LAUNCHER,
    )
    assert result.exit_code == 0, result.output
    assert notice in " ".join(result.stderr.split())
    # Found by Espanso: no notice.
    monkeypatch.setattr(deploy, "run_command", FakeRunner({"espanso path config": f"{default}\n"}))
    result = _run("espanso", "deploy", "--dry-run", "--launcher", LAUNCHER)
    assert "default Espanso folder" not in result.stderr


def test_doctor_drift_case_reports_stale_and_no_secret(
    saved: Path, espanso: Path, clipboard: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #25 drift case: a prompts-template.yml an older version deployed."""
    monkeypatch.setattr(deploy, "run_command", FakeRunner({"espanso status": "running"}))
    config_store_secret(saved)
    _run("config", "set", "PROMPT_PERSONA", PERSONA)
    real = assets.read_match
    monkeypatch.setattr(deploy, "installed_version", lambda: "0.15.0")
    monkeypatch.setattr(
        assets,
        "read_match",
        lambda name: "# old\nmatches: []\n" if name == "prompts-template.yml" else real(name),
    )
    deploy.apply(deploy.plan(espanso, LAUNCHER, deploy.Manifest.load()))
    monkeypatch.setattr(deploy, "installed_version", lambda: "0.16.0")
    monkeypatch.setattr(assets, "read_match", real)

    text = _doctor("--espanso-dir", str(espanso))
    assert "prompts-template.yml: stale" in text.stdout
    assert text.exit_code == common.PROBLEMS
    as_json = _doctor("--espanso-dir", str(espanso), "--json")
    files = json.loads(as_json.stdout)["checks"]["match_files"]["data"]["files"]
    assert {"name": "prompts-template.yml", "state": "stale"} in files
    for result in (text, as_json):
        for sentinel in (KEY, PERSONA, clipboard):
            assert sentinel not in result.output


def test_doctor_clipboard_reports_the_length_only(
    clipboard: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy, "run_command", FakeRunner())
    result = _doctor()
    assert f"readable: {len(clipboard)} character(s)" in result.stdout
    assert clipboard not in result.output
    assert "skipped" in _doctor("--no-clipboard").stdout


def test_doctor_never_reads_a_concealed_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyperclip

    from promptmend import clipboard_guard

    monkeypatch.setattr(clipboard_guard, "is_concealed", lambda: True)
    monkeypatch.setattr(pyperclip, "paste", lambda: pytest.fail("read a concealed item"))
    monkeypatch.setattr(deploy, "run_command", FakeRunner())
    assert "not read" in _doctor().stdout


def test_doctor_launcher_drift_and_missing(
    espanso: Path, clipboard: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deploy, "run_command", FakeRunner())
    deploy.apply(deploy.plan(espanso, LAUNCHER, deploy.Manifest.load()))
    exe = tmp_path / "bin" / "promptmend"
    exe.parent.mkdir()
    exe.write_text("", "utf-8")
    data = json.loads(
        _run("doctor", "--json", "--launcher", str(exe), "--espanso-dir", str(espanso)).stdout
    )
    launcher = data["checks"]["launcher"]
    assert launcher["data"]["drift"] is True
    assert launcher["data"]["missing"] == [LAUNCHER]
    assert launcher["data"]["orphans"] == []
    assert launcher["status"] == "fail"


# --- stats and history --------------------------------------------------------------------


@pytest.fixture
def record_ops(seed_history: SeedHistory) -> Callable[..., None]:
    """record_ops(n) seeds n recorded -i- calls to OpenRouter."""

    def record(n: int = 2) -> None:
        for _ in range(n):
            op = {
                "id": history.new_operation_id(),
                "origin": "espanso_managed",
                "trigger_id": "-i-",
                "kind": "improve",
                "profile_id": "default",
                "outcome": "ok",
                "latency_ms": 1000.0,
            }
            attempt = {
                "provider": "openrouter",
                "requested_model": "google/gemini-3.5-flash-lite",
                "endpoint": "remote",
                "status": 200,
                "latency_ms": 900.0,
                "output": 30,
                "input_uncached": 100,
                "charged_amount": Decimal("0.0001"),
                "charged_unit": "credits",
            }
            seed_history(op, [attempt])

    return record


def test_stats_text_states_the_caveats_and_the_history(
    monkeypatch: pytest.MonkeyPatch, record_ops: Callable[..., None]
) -> None:
    # Plain stats: one line on the history, then the numbers (#222).
    result = _run("stats")
    assert result.exit_code == 0
    assert "Note:" not in result.stdout
    assert str(history.history_path()) in result.stdout
    assert "`promptmend stats --verbose`" in result.stdout
    result = _run("stats", "--verbose")
    assert result.exit_code == 0
    for caveat in (
        "not provider billing",
        "rendered does not mean pasted",
        "ignore PROMPT_PROVIDER",
        "config set PROMPT_HISTORY false",
        str(history.history_path()),
    ):
        assert caveat in result.stdout
    assert "No usage recorded yet." in result.stdout
    record_ops()
    result = _run("stats", "--by", "provider")
    assert "openrouter: 2 call(s), 2 request(s)" in result.stdout
    assert "reported 0.0002 credits" in result.stdout


def test_stats_help_states_the_caveats(monkeypatch: pytest.MonkeyPatch) -> None:
    # Help panels wrap at the terminal width, which differs on CI runners (Windows too).
    monkeypatch.setenv("COLUMNS", "400")
    out = _plain(_run("stats", "--help").stdout)
    assert "not provider billing" in out
    assert "rendered does not mean pasted" in out
    assert "ignore PROMPT_PROVIDER" in out


def test_stats_name_calls_without_a_trigger(seed_history: Callable[..., None]) -> None:
    for trigger in (None, "-try-"):
        op = {
            "id": history.new_operation_id(),
            "origin": "direct",
            "trigger_id": trigger,
            "kind": "improve",
            "profile_id": "default",
            "outcome": "ok",
            "latency_ms": 10.0,
        }
        seed_history(op, [])
    out = _run("stats").stdout
    assert "no trigger (direct call): 1 call(s)" in out
    assert "-try-: 1 call(s)" in out
    assert "(none)" not in out


def test_stats_json(record_ops: Callable[..., None]) -> None:
    record_ops()
    data = json.loads(_run("stats", "--json").stdout)
    assert data["group_by"] == "trigger"
    assert len(data["caveats"]) == 3
    assert data["history"]["enabled"] is True
    assert data["rows"][0]["key"] == "-i-"
    assert data["rows"][0]["reported"] == {"credits": "0.0002"}


def test_stats_bad_group() -> None:
    assert _run("stats", "--by", "week").exit_code == 2


def test_history_export_prune_reset(tmp_path: Path, record_ops: Callable[..., None]) -> None:
    record_ops()
    data = json.loads(_run("history", "export").stdout)
    assert len(data["operations"]) == 2
    out = tmp_path / "out.csv"
    assert _run("history", "export", "--format", "csv", "-o", str(out)).exit_code == 0
    assert out.read_text("utf-8").count("\n") == 3
    assert _run("history", "export", "-o", str(out)).exit_code == 1  # never replaced
    assert "Deleted 0" in _run("history", "prune").stdout
    assert "Deleted 2" in _run("history", "prune", "--older-than", "0d", "--yes").stdout
    record_ops(1)
    assert _run("history", "reset").exit_code == common.NEEDS_TERMINAL
    assert _run("history", "reset", "--yes").exit_code == 0
    assert json.loads(_run("history", "export").stdout)["operations"] == []
    assert _run("history", "prune", "--older-than", "soon").exit_code == 2


# --- profiles -----------------------------------------------------------------------------


def test_profiles_list(monkeypatch: pytest.MonkeyPatch) -> None:
    folder = config.user_config_dir() / "profiles"
    folder.mkdir(parents=True)
    (folder / "mine.md").write_text("mine", "utf-8")
    (folder / "general.md").write_text("x", "utf-8")
    result = _run("profiles", "list")
    assert result.exit_code == 0
    assert "  default" in result.stdout
    assert "mine: added" in result.stdout
    assert "general: shadowed" in result.stdout


# --- espanso deploy --dry-run -------------------------------------------------------------


def test_espanso_deploy_dry_run_and_no_terminal(espanso: Path, fake_run: FakeRunner) -> None:
    where = ["--espanso-dir", str(espanso), "--launcher", LAUNCHER]
    result = _run("espanso", "deploy", "--dry-run", *where)
    assert result.exit_code == 0
    assert "Dry run" in result.stdout
    assert not list((espanso / "match").iterdir())
    result = _run("espanso", "deploy", *where)
    assert result.exit_code == common.NEEDS_TERMINAL
    assert not list((espanso / "match").iterdir())


def _boom(*args: object, **kwargs: object) -> None:
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    ("argv", "target", "name"),
    [
        (["status"], deploy, "plan"),
        (["deploy", "--yes"], deploy, "plan"),
        (["detach", "--yes"], deploy.Manifest, "load"),
    ],
)
def test_espanso_commands_run_under_the_guard(
    espanso: Path,
    fake_run: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    target: object,
    name: str,
) -> None:
    """An unexpected failure is one `error:` line, never a traceback (#401)."""
    monkeypatch.setattr(target, name, _boom)
    where = ["--espanso-dir", str(espanso)] + (
        ["--launcher", LAUNCHER] if argv[0] != "detach" else []
    )
    result = _run("espanso", *argv, *where)
    assert result.exit_code == common.FAILED
    assert isinstance(result.exception, SystemExit)
    errors = [line for line in result.stderr.splitlines() if line.startswith("error:")]
    assert errors == ["error: unexpected RuntimeError: boom"]
    assert "Traceback" not in result.output
    assert not list((espanso / "match").iterdir())
    assert _changes(fake_run.calls) == []


@pytest.mark.parametrize("kind", [ValueError, RuntimeError])
def test_guard_redacts_a_key_in_the_message(
    kind: type[Exception], capsys: pytest.CaptureFixture[str]
) -> None:
    """Expected and unexpected errors are redacted like a trigger marker (#401)."""

    @common.guard
    def command() -> None:
        raise kind(f"the value {KEY} is not valid")

    with pytest.raises(typer.Exit) as raised:
        command()
    assert raised.value.exit_code == common.FAILED
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "is not valid" in err
    assert KEY not in err
    assert KEY[:16] not in err


# A management command's error keeps what the user needs to find the file: an email address in
# a cloud-drive path, a folder named like a label. Only secrets are hidden (#401).
def test_guard_keeps_an_email_and_a_label_in_a_path(capsys: pytest.CaptureFixture[str]) -> None:
    path = "/Users/jane/Library/CloudStorage/GoogleDrive-jane.doe@example.com/CONFIDENTIAL/x.yml"

    @common.guard
    def command() -> None:
        raise PermissionError(13, "Permission denied", path)

    with pytest.raises(typer.Exit):
        command()
    assert capsys.readouterr().err == f"error: [Errno 13] Permission denied: '{path}'\n"


# The interface words and redacts an error as the guard does.
def test_interface_error_message_matches_the_guard() -> None:
    from promptmend.tui.base import error_message

    assert error_message(ValueError(f"the value {KEY} is not valid")) == (
        f"error: the value <redacted, {len(KEY)} chars> is not valid"
    )
    unexpected = error_message(RuntimeError(f"bad {KEY}"))
    assert unexpected.startswith("error: unexpected RuntimeError: bad <redacted")
    assert KEY not in unexpected
    assert error_message(settings_service.ServiceError("PROMPTMEND_ENV is set")) == (
        "error: PROMPTMEND_ENV is set"
    )


@pytest.mark.parametrize(("usage", "code"), [(True, common.USAGE), (False, common.FAILED)])
def test_guard_keeps_a_service_error_as_written(
    capsys: pytest.CaptureFixture[str], usage: bool, code: int
) -> None:
    @common.guard
    def command() -> None:
        raise settings_service.ServiceError(f"PROMPTMEND_ENV is set ({LAUNCHER})", usage=usage)

    with pytest.raises(typer.Exit) as raised:
        command()
    assert raised.value.exit_code == code
    assert capsys.readouterr().err == f"error: PROMPTMEND_ENV is set ({LAUNCHER})\n"


# --- setup --------------------------------------------------------------------------------


def _setup(espanso: Path, *args: str, input: str | None = None) -> Result:
    return _run("setup", "--espanso-dir", str(espanso), "--launcher", LAUNCHER, *args, input=input)


def test_setup_non_interactive_end_to_end(saved: Path, espanso: Path, fake_run: FakeRunner) -> None:
    """CI acceptance: config written, deploy in dry run, the real improve against the stub."""
    result = _setup(espanso, "--non-interactive", "--api-key-stdin", input=f"{KEY}\n")
    assert result.exit_code == 0, result.output
    assert (saved / "config.toml").is_file()
    assert KEY in (saved / "secrets.toml").read_text("utf-8")
    assert KEY not in result.output
    assert "Dry run: nothing was written." in result.stdout
    assert not list((espanso / "match").iterdir())
    assert "ok: -i- (openrouter): passed: improve reached the stub" in result.stdout
    assert "Usage history is on" in result.stdout
    assert "config set PROMPT_HISTORY false" in result.stdout
    assert _changes(fake_run.calls) == []  # no restart, nothing deployed
    # The stub improve is a health check, not usage: nothing is recorded (#116).
    assert json.loads(_run("history", "export").stdout)["operations"] == []


def test_setup_needs_a_terminal_or_non_interactive(espanso: Path) -> None:
    result = _setup(espanso)
    assert result.exit_code == common.NEEDS_TERMINAL
    assert "--non-interactive" in result.stderr
    # The wizard needs stdout too: piped, it is refused, never asked line by line (#244).
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(common, "stdin_is_tty", lambda: True)
        result = _setup(espanso, "--use", "local")
    assert result.exit_code == common.NEEDS_TERMINAL


def test_setup_deploy_applies(
    saved: Path, espanso: Path, fake_run: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smoke, "run", lambda p: smoke.SmokeResult(True, smoke.REPLY, 1, "ok"))
    result = _setup(espanso, "--non-interactive", "--deploy", "--provider", "ollama")
    assert result.exit_code == 0, result.output
    assert len(list((espanso / "match").iterdir())) == 3
    assert _changes(fake_run.calls) == [["espanso", "restart"]]
    # Deprecated (#390): still saved for one release, with a note that no trigger reads it.
    assert 'PROMPT_PROVIDER = "ollama"' in (saved / "config.toml").read_text("utf-8")
    note = setup_guide.PROVIDER_DEPRECATED
    assert f"  {note}" in result.stdout
    # The note's facts, so an emptied or reworded constant fails here.
    assert note.startswith("--provider is deprecated and will be removed in a later release")
    assert "no trigger reads it" in note
    assert "--use cloud|local|both" in note


def test_setup_help_marks_provider_deprecated_and_names_use() -> None:
    out = " ".join(_plain(_run("setup", "--help").stdout).split())
    assert "--provider" in out
    assert "Deprecated, will be removed in a later release (use --use): saves PROMPT_PROVIDER" in (
        out
    )
    assert "which no trigger reads" in out
    assert "--use" in out
    assert (
        "Which triggers you use: cloud, local, both (local saves PROMPT_LOCAL_ONLY=true; "
        "cloud and both remove it)" in out
    )


def test_setup_smoke_failure_exits_1(
    saved: Path, espanso: Path, fake_run: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smoke, "run", lambda p: smoke.SmokeResult(False, "", 0, "[pw: down]"))
    result = _setup(espanso, "--non-interactive")
    assert result.exit_code == 1
    assert "smoke test failed" in result.stderr


# --plain is --non-interactive (#244). A .env setup no longer reads is named, never changed.
def test_setup_names_an_unread_dotenv(
    saved: Path, espanso: Path, fake_run: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smoke, "run", lambda p: smoke.SmokeResult(True, smoke.REPLY, 1, "ok"))
    env = _dotenv(saved)
    before = env.read_bytes()
    result = _setup(espanso, "--plain", "--provider", "ollama", "--profile", "general")
    assert result.exit_code == 0, result.output
    assert f"{env} is no longer read" in result.stdout
    assert 'PROMPT_PROFILE = "general"' in (saved / "config.toml").read_text("utf-8")
    assert env.read_bytes() == before
    assert KEY not in result.output


def test_setup_in_legacy_mode_writes_no_settings(
    espanso: Path, fake_run: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smoke, "run", lambda p: smoke.SmokeResult(True, smoke.REPLY, 1, "ok"))
    result = _setup(espanso, "--non-interactive")
    assert result.exit_code == 0, result.output
    assert "PROMPTMEND_ENV is set" in result.stdout
    assert not config.settings_file().exists()


@pytest.mark.parametrize("option", ["--provider", "--use", "--output"])
def test_setup_rejects_a_bad_choice(espanso: Path, option: str) -> None:
    result = _setup(espanso, "--non-interactive", option, "nope")
    assert result.exit_code == 2
    assert f"{option} must be one of" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


# --- the smoke test -----------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["openrouter", "anthropic", "ollama", "lmstudio"])
def test_smoke_runs_the_real_improve_against_the_stub(provider: str) -> None:
    result = smoke.run(provider)
    assert result.ok, result.message
    assert result.requests == 1


def test_smoke_env_points_every_provider_at_the_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    seen: dict[str, str] = {}

    def fake(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, bytes]:
        seen.update(env)
        return 0, smoke.REPLY.encode()

    result = smoke.run("openrouter", runner=fake)
    assert not result.ok  # the fake never reached the stub
    assert KEY not in seen.values()
    for name in (
        "OPENROUTER_BASE_URL",
        "ANTHROPIC_BASE_URL",
        "OLLAMA_BASE_URL",
        "LMSTUDIO_BASE_URL",
    ):
        assert seen[name].startswith("http://127.0.0.1:")
    assert seen["PROMPT_HISTORY"] == "false"


def _history_rows() -> str:
    out = io.StringIO()
    HistoryStore(history.history_path()).export(out)
    return out.getvalue()


def test_smoke_and_setup_add_no_history_row(
    saved: Path, espanso: Path, fake_run: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#116: a real smoke run reaches the stub but never the real (per-test) history."""
    monkeypatch.setenv("PROMPT_HISTORY", "true")

    def recorded(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, bytes]:
        return smoke.run_cli(argv, {**env, "PROMPT_HISTORY": "true"})

    # The same run with the history forced on is recorded, so the checks below are not vacuous.
    assert smoke.run("ollama", runner=recorded).ok
    store = HistoryStore(history.history_path())
    before = _history_rows()
    data = json.loads(before)
    assert [(op["origin"], op["kind"], op["outcome"]) for op in data["operations"]] == [
        ("direct", "improve", "ok")
    ]
    assert [(a["provider"], a["endpoint"]) for a in data["attempts"]] == [("ollama", "loopback")]

    assert smoke.run("ollama").ok
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--use", "local")
    assert result.exit_code == 0, result.output
    assert "ok: -il- (ollama): passed: improve reached the stub" in result.stdout
    assert store.health().operations == 1
    assert _history_rows() == before


@pytest.mark.parametrize(
    "argv",
    [
        [KEY],
        ["doctor", KEY],
        ["secrets", "set", "OPENROUTER_API_KEY", KEY],
        ["config", "set", "PROMPT_PROFILE", "general", KEY],
        ["stats", "--by", KEY],
        ["history", "prune", "--older-than", KEY],
        ["config", KEY],
        ["improve", KEY],
    ],
    ids=["command", "doctor", "secrets-set", "config-set", "stats", "prune", "group", "improve"],
)
def test_usage_errors_never_repeat_a_key(argv: list[str]) -> None:
    result = _run(*argv)
    assert result.exit_code == 2
    assert KEY not in result.output
    assert "<redacted" in result.output


def test_a_key_under_a_setting_name_is_never_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_MODEL", KEY)
    show = _run("config", "show")
    assert KEY not in show.output
    assert "<set>" in show.stdout
    get = _run("config", "get", "OPENROUTER_MODEL")
    assert get.exit_code == 1
    assert KEY not in get.output


def test_prune_sooner_than_retention_asks(tty: None, record_ops: Callable[..., None]) -> None:
    record_ops()
    assert _run("history", "prune", "--older-than", "0", input="n\n").exit_code == 1
    assert len(json.loads(_run("history", "export").stdout)["operations"]) == 2


def test_prune_sooner_than_retention_needs_yes_without_terminal(
    record_ops: Callable[..., None],
) -> None:
    record_ops()
    assert _run("history", "prune", "--older-than", "0").exit_code == common.NEEDS_TERMINAL
    assert "Deleted 2" in _run("history", "prune", "--older-than", "0", "--yes").stdout


def test_stdin_at_a_terminal_asks_hidden(
    saved: Path, tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = []

    def getpass(prompt: str) -> str:
        prompts.append(prompt)
        return KEY

    monkeypatch.setattr(common, "_getpass", getpass)
    assert _run("secrets", "set", "OPENROUTER_API_KEY", "--stdin").exit_code == 0
    assert prompts
    assert KEY in (saved / "secrets.toml").read_text("utf-8")


def test_stdin_drops_a_byte_order_mark(saved: Path) -> None:
    result = _run("secrets", "set", "OPENROUTER_API_KEY", "--stdin", input=f"\ufeff{KEY}\r\n")
    assert result.exit_code == 0, result.output
    from promptmend import config_store

    assert config_store.saved_secret_names() == ("OPENROUTER_API_KEY",)
    assert f'"{KEY}"' in (saved / "secrets.toml").read_text("utf-8")


def test_doctor_data_keys_are_stable_when_a_check_fails(
    clipboard: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy, "run_command", FakeRunner())

    def broken(settings: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "_profiles_check", broken)
    monkeypatch.setattr(doctor, "_history_checks", broken)
    data = json.loads(_doctor("--json").stdout)
    for check_id, check in data["checks"].items():
        assert tuple(check["data"]) == doctor.DATA_KEYS[check_id]
    assert data["checks"]["profiles"]["status"] == "fail"
    assert data["checks"]["profiles"]["data"] == dict.fromkeys(doctor.DATA_KEYS["profiles"])


# --- setup, step by step ------------------------------------------------------------------


@pytest.fixture
def smoke_ok(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls = []

    def fake(provider: str) -> smoke.SmokeResult:
        calls.append(provider)
        return smoke.SmokeResult(True, smoke.REPLY, 1, "ok")

    monkeypatch.setattr(smoke, "run", fake)
    return calls


def test_setup_clipboard_output_passes_the_test_and_leaves_the_clipboard(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_in_process: list[str]
) -> None:
    result = _setup(
        espanso, "--non-interactive", "--use", "cloud", "--output", "clipboard", "--no-deploy"
    )
    assert result.exit_code == 0, result.output
    assert "ok: -i- (openrouter): passed" in result.stdout
    assert smoke_in_process == []


def test_setup_keeps_what_config_toml_holds_whatever_the_shell_says(
    saved: Path, espanso: Path, fake_run: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An option not given keeps config.toml's value: a shell variable is never written into
    it, nor a saved value removed, and the line says what the shell changes."""
    saved.mkdir(parents=True, exist_ok=True)
    (saved / "config.toml").write_text('PROMPT_OUTPUT = "clipboard"\n', "utf-8")
    monkeypatch.setenv("PROMPT_OUTPUT", "paste")
    monkeypatch.setenv("PROMPT_PROFILE", "general")
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--no-smoke-test")
    assert result.exit_code == 0, result.output
    assert "  Unchanged." in result.stdout
    assert "removed from" not in result.stdout
    assert (saved / "config.toml").read_text("utf-8") == 'PROMPT_OUTPUT = "clipboard"\n'
    assert (
        "Triggers: cloud; profile: default (the environment variable PROMPT_PROFILE overrides "
        "it wherever it is set: general); output: clipboard (the environment variable "
        "PROMPT_OUTPUT overrides it wherever it is set: paste)" in " ".join(result.stdout.split())
    )


def test_setup_next_step_follows_config_toml_not_the_shell(
    saved: Path, espanso: Path, fake_run: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved.mkdir(parents=True, exist_ok=True)
    (saved / "config.toml").write_text('PROMPT_OUTPUT = "clipboard"\n', "utf-8")
    monkeypatch.setenv("PROMPT_OUTPUT", "paste")
    result = _setup(
        espanso, "--non-interactive", "--use", "local", "--no-deploy", "--no-smoke-test"
    )
    assert result.exit_code == 0, result.output
    assert "Setup finished. Try it:" in result.stdout
    assert "type -il- " in result.stdout
    assert "The rewrite then waits on the clipboard" in result.stdout


def test_setup_local_only_in_the_shell_alone_is_not_set(
    saved: Path, espanso: Path, fake_run: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Espanso's -i- still sends drafts: the answer read back is cloud, and the shell's
    value is a to-do."""
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--no-smoke-test")
    out = " ".join(result.stdout.split())
    assert "Triggers: cloud;" in out
    assert "is set" not in out.split("API keys")[0]
    assert (
        "to do: cloud, but the environment variable PROMPT_LOCAL_ONLY overrides the saved "
        "value, so the cloud triggers refuse wherever it is set: remove it from the "
        "environment" in out
    )
    assert "No key needed" not in out
    assert "OPENROUTER_API_KEY" in out
    assert "Setup finished." not in out
    assert "PROMPT_LOCAL_ONLY" not in config_store.read_settings().table


def test_setup_local_note_is_set_only_when_config_toml_holds_it(
    saved: Path, espanso: Path, fake_run: FakeRunner
) -> None:
    saved.mkdir(parents=True, exist_ok=True)
    (saved / "config.toml").write_text("PROMPT_LOCAL_ONLY = true\n", "utf-8")
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--no-smoke-test")
    out = " ".join(result.stdout.split())
    assert "Triggers: local;" in out
    assert "PROMPT_LOCAL_ONLY=true is set: -i-" in out
    assert "`--use cloud` or `--use both` turns it off again." in out


def test_setup_in_legacy_mode_says_to_remove_what_only_the_file_can_turn_off(
    espanso: Path, fake_run: FakeRunner
) -> None:
    legacy = os.environ["PROMPTMEND_ENV"]
    Path(legacy).write_text("PROMPT_LOCAL_ONLY=true\n", "utf-8")
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--no-smoke-test")
    out = " ".join(result.stdout.split())
    assert "PROMPT_LOCAL_ONLY=true is set: -i-" in out
    assert f"Remove it from {legacy} to turn it off." in out
    assert "turns it off again" not in out
    result = _setup(
        espanso, "--non-interactive", "--use", "cloud", "--no-deploy", "--no-smoke-test"
    )
    assert f"to do: remove PROMPT_LOCAL_ONLY from {legacy}" in " ".join(result.stdout.split())


def test_setup_local_provider_takes_no_key(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    for args in (("--use", "local"), ("--provider", "ollama")):
        result = _setup(espanso, "--non-interactive", *args, "--api-key-stdin", input=f"{KEY}\n")
        assert result.exit_code == 1
        assert f"--api-key-stdin was given, but {args[1]} takes no key" in result.stderr
        assert not (saved / "secrets.toml").exists()
    # The test follows the answer (local, still saved), never PROMPT_PROVIDER.
    assert smoke_ok == ["ollama", "ollama"]


# --- setup: cloud or local, and the output (#390) -----------------------------------------


def test_setup_provider_on_a_terminal_opens_the_wizard_and_saves_nothing(
    saved: Path, espanso: Path, tty: None, monkeypatch: pytest.MonkeyPatch, fake_run: FakeRunner
) -> None:
    """Deprecated but still accepted for one release: on a terminal it opens the wizard, which
    names it on Welcome and never saves it; a local provider only preselects Local."""
    from promptmend.commands import ui as ui_cmd
    from promptmend.tui.setup_wizard import SetupOptions

    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    opened: list[SetupOptions] = []
    monkeypatch.setattr(ui_cmd, "open_setup", opened.append)
    assert _setup(espanso, "--provider", "ollama").exit_code == 0
    assert _setup(espanso, "--provider", "anthropic").exit_code == 0
    assert _setup(espanso, "--provider", "lmstudio", "--use", "cloud").exit_code == 0
    assert [(o.provider, o.use) for o in opened] == [
        ("ollama", "local"),
        ("anthropic", "cloud"),
        ("lmstudio", "cloud"),
    ]
    assert not (saved / "config.toml").exists()


def test_setup_use_local_saves_although_the_shell_says_true(
    saved: Path,
    espanso: Path,
    fake_run: FakeRunner,
    smoke_ok: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Espanso does not start from the shell: config.toml must hold the answer."""
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--use", "local")
    assert result.exit_code == 0, result.output
    assert config_store.read_settings().table.get("PROMPT_LOCAL_ONLY") is True
    assert "the environment variable PROMPT_LOCAL_ONLY is set and overrides" in result.stdout


def test_setup_use_local_with_the_shell_saying_false_says_so(
    saved: Path,
    espanso: Path,
    fake_run: FakeRunner,
    smoke_ok: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "false")
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--use", "local")
    assert result.exit_code == 0, result.output
    assert config_store.read_settings().table.get("PROMPT_LOCAL_ONLY") is True
    assert (
        "to do: local, but the environment variable PROMPT_LOCAL_ONLY overrides the saved "
        "value, so the cloud triggers still send drafts off this computer wherever it is set"
        in result.stdout
    )
    # The keys follow the answer: the cloud key is neither needed nor left to do.
    assert "OPENROUTER_API_KEY" not in result.stdout
    assert "No key needed: PROMPT_LOCAL_ONLY=true refuses" in result.stdout
    assert "Setup finished with 1 thing(s) still to do" in result.stdout


def test_setup_use_cloud_removes_a_saved_local_only_the_shell_hides(
    saved: Path,
    espanso: Path,
    fake_run: FakeRunner,
    smoke_ok: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_store.save_settings(config_store.read_settings(), {"PROMPT_LOCAL_ONLY": "true"})
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "false")
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--use", "cloud")
    assert result.exit_code == 0, result.output
    assert "PROMPT_LOCAL_ONLY" not in config_store.read_settings().table
    assert "PROMPT_LOCAL_ONLY removed from " in result.stdout


@pytest.mark.parametrize(
    "env",
    [{"OLLAMA_BASE_URL": "https://ollama.example.com"}, {"OLLAMA_MODEL": "gpt-oss:120b-cloud"}],
)
def test_setup_local_tests_a_trigger_that_stays_local(
    saved: Path,
    espanso: Path,
    fake_run: FakeRunner,
    smoke_ok: list[str],
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--use", "local")
    assert result.exit_code == 0, result.output
    assert smoke_ok == ["lmstudio"]
    assert "ok: -ilm- (lmstudio): passed" in result.stdout
    assert "type -ilm- and wait" in result.stdout


def test_setup_local_with_no_local_trigger_runs_nothing(
    saved: Path,
    espanso: Path,
    fake_run: FakeRunner,
    smoke_ok: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com")
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "https://lm.example.com/v1")
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--use", "local")
    assert result.exit_code == 0, result.output
    assert smoke_ok == []
    assert f"to do: local: not run: {setup_guide.NO_LOCAL_TRIGGER}" in result.stdout
    assert "ok:" not in result.stdout


def test_setup_next_step_follows_the_answer_and_the_output(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    config_store.save_secret("OPENROUTER_API_KEY", KEY)
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--use", "local")
    assert "Setup finished. Try it: copy a rough draft, click into any text field, type -il- " in (
        result.stdout
    )
    result = _setup(
        espanso, "--non-interactive", "--no-deploy", "--use", "cloud", "--output", "clipboard"
    )
    assert "type -i- and wait without typing. The rewrite then waits on the clipboard" in (
        result.stdout
    )


def test_setup_local_note_says_what_is_true(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--use", "local")
    assert "  Local saves PROMPT_LOCAL_ONLY=true: " in result.stdout
    # A re-run that asks nothing: it is set, not saved again.
    result = _setup(espanso, "--non-interactive", "--no-deploy")
    assert "  PROMPT_LOCAL_ONLY=true is set: " in result.stdout
    assert "Local saves" not in result.stdout


def test_setup_use_local_refuses_the_cloud_triggers(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    result = _setup(espanso, "--non-interactive", "--use", "local")
    assert result.exit_code == 0, result.output
    assert "  Triggers: local; profile: default; output: paste" in result.stdout
    # What local does is said, with the triggers from the match files.
    assert "-i-, -iok-, -ip- and -if- then paste a refusal instead of sending" in result.stdout
    assert "PROMPT_LOCAL_ONLY=true saved in " in result.stdout
    assert settings_service.load_layers()[1].local_only
    assert "No key needed: PROMPT_LOCAL_ONLY=true refuses" in result.stdout
    assert "OPENROUTER_API_KEY is not set" not in result.stdout
    assert smoke_ok == ["ollama"]
    assert "ok: -il- (ollama): passed" in result.stdout
    # A re-run without --use keeps the answer: nothing about PROMPT_LOCAL_ONLY changes.
    result = _setup(espanso, "--non-interactive")
    assert "  Triggers: local;" in result.stdout
    assert settings_service.load_layers()[1].local_only
    assert smoke_ok == ["ollama", "ollama"]


def test_setup_use_cloud_after_local_removes_local_only(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    assert _setup(espanso, "--non-interactive", "--use", "local").exit_code == 0
    result = _setup(espanso, "--non-interactive", "--use", "cloud")
    assert result.exit_code == 0, result.output
    assert "PROMPT_LOCAL_ONLY removed from " in result.stdout
    assert "PROMPT_LOCAL_ONLY" not in (saved / "config.toml").read_text("utf-8")
    assert not settings_service.load_layers()[1].local_only
    assert "OPENROUTER_API_KEY: not set; needed by -i-" in result.stdout
    assert smoke_ok == ["ollama", "openrouter"]
    assert "ok: -i- (openrouter): passed" in result.stdout


def test_setup_use_both_runs_both_paths_and_reads_the_openrouter_key(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    result = _setup(espanso, "--non-interactive", "--use", "both", "--api-key-stdin", input=KEY)
    assert result.exit_code == 0, result.output
    assert smoke_ok == ["openrouter", "ollama"]
    assert settings_service.load_layers()[0].entries["OPENROUTER_API_KEY"].value == KEY
    assert KEY not in result.output


def test_setup_both_paths_fail_alone(
    saved: Path, espanso: Path, fake_run: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        smoke, "run", lambda p: smoke.SmokeResult(p == "ollama", "", 1, f"{p} said so")
    )
    result = _setup(espanso, "--non-interactive", "--use", "both", "--no-deploy")
    assert result.exit_code == 1
    assert "-i- (openrouter): failed: openrouter said so" in (result.stderr)
    assert "ok: -il- (ollama): passed: ollama said so" in result.stdout


def test_setup_output_clipboard_and_back(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    result = _setup(espanso, "--non-interactive", "--output", "clipboard", "--no-smoke-test")
    assert result.exit_code == 0, result.output
    assert "PROMPT_OUTPUT=clipboard saved in " in result.stdout
    assert settings_service.load_layers()[1].output == "clipboard"
    result = _setup(espanso, "--non-interactive", "--output", "paste", "--no-smoke-test")
    assert "PROMPT_OUTPUT removed from " in result.stdout
    assert "PROMPT_OUTPUT" not in (saved / "config.toml").read_text("utf-8")


def test_setup_in_legacy_mode_saves_neither_new_choice(
    espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    result = _setup(espanso, "--non-interactive", "--use", "local", "--output", "clipboard")
    assert result.exit_code == 0, result.output
    # Nothing can be saved here: the note says what to add, never that setup saves it.
    assert "Local saves" not in result.stdout
    assert "Local needs PROMPT_LOCAL_ONLY=true, which this setup cannot save: add it to " in (
        result.stdout
    )
    legacy = os.environ["PROMPTMEND_ENV"]
    assert (
        f"to do: add PROMPT_LOCAL_ONLY=true to {legacy}; add PROMPT_OUTPUT=clipboard to {legacy}"
        in result.stdout
    )
    # Not needed once it is set, which it is not yet.
    assert "No key needed once PROMPT_LOCAL_ONLY=true is set: " in result.stdout
    assert not config.settings_file().exists()
    settings = settings_service.load_layers()[1]
    assert (settings.local_only, settings.output) == (False, "paste")
    # The test still follows the answer.
    assert smoke_ok == ["ollama"]


def test_setup_key_from_stdin_in_legacy_mode_is_refused(
    espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    result = _setup(espanso, "--non-interactive", "--api-key-stdin", input=f"{KEY}\n")
    assert result.exit_code == 1
    assert "the key was not saved" in result.stderr


def test_setup_refuses_a_malformed_key(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    result = _setup(espanso, "--non-interactive", "--api-key-stdin", input="two words\n")
    assert result.exit_code == 1
    assert "one line of visible characters" in result.stderr
    assert not (saved / "secrets.toml").exists()


def test_setup_reports_a_set_key_without_showing_it(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    config_store_secret(saved)
    result = _setup(espanso, "--non-interactive")
    assert result.exit_code == 0
    assert "OPENROUTER_API_KEY is set (from" in result.stdout
    assert KEY not in result.output


def test_setup_deploys_then_finds_the_files_in_sync(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    args = ("--provider", "ollama", "--profile", "general")
    result = _setup(espanso, "--non-interactive", "--deploy", *args)
    assert result.exit_code == 0, result.output
    assert len(list((espanso / "match").iterdir())) == 3
    # A second run finds nothing to deploy.
    result = _setup(espanso, "--non-interactive")
    assert "Every match file is in sync." in result.stdout


def test_setup_forgets_gone_entries_without_asking(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str], tmp_path: Path
) -> None:
    """Every file in sync and only gone entries on record: nothing to write, so no question,
    no dry run and no "later" step; the entries are forgotten and the files left as they are."""
    deploy.apply(deploy.plan(espanso, LAUNCHER, deploy.Manifest.load()))
    old = tmp_path / "old-espanso"
    (old / "match").mkdir(parents=True)
    deploy.apply(deploy.plan(old, LAUNCHER, deploy.Manifest.load()))
    shutil.rmtree(old)
    before = {p: p.read_bytes() for p in (espanso / "match").iterdir()}
    result = _setup(espanso, "--non-interactive")
    assert result.exit_code == 0, result.output
    assert "Every match file is in sync." in result.stdout
    assert "(already gone)" in result.stdout
    assert "Dry run" not in result.stdout
    assert "deploy the match files" not in result.stdout
    assert _changes(fake_run.calls) == []
    assert {p: p.read_bytes() for p in (espanso / "match").iterdir()} == before
    assert all(Path(t).parent.parent == espanso for t in deploy.Manifest.load().entries)


def test_setup_deploy_keeps_an_edited_file(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    target = espanso / "match" / "prompts-llm.yml"
    target.write_text("# mine\n", "utf-8")
    result = _setup(espanso, "--non-interactive", "--deploy")
    assert result.exit_code == 0, result.output
    assert target.read_text("utf-8") == "# mine\n"
    assert "files you edited were kept" in result.stdout


def test_setup_deploy_error_fails_the_step(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    result = _run(
        "setup", "--non-interactive", "--espanso-dir", str(espanso), "--launcher", '/a"b/pw'
    )
    assert result.exit_code == 1
    assert "deploy failed" in result.stderr


def test_setup_no_deploy_and_no_smoke(
    saved: Path, espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    result = _setup(espanso, "--non-interactive", "--no-deploy", "--no-smoke-test")
    assert result.exit_code == 0
    assert "Skipped (--no-deploy)." in result.stdout
    assert "Skipped (--no-smoke-test)." in result.stdout
    assert smoke_ok == []


def test_setup_option_errors(
    saved: Path, espanso: Path, tty: None, monkeypatch: pytest.MonkeyPatch, fake_run: FakeRunner
) -> None:
    from promptmend.commands import ui as ui_cmd

    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    opened: list[object] = []
    monkeypatch.setattr(ui_cmd, "open_setup", opened.append)
    assert _setup(espanso, "--api-key-stdin").exit_code == 2
    # On a terminal too, a bad choice is refused before the wizard opens.
    for option, value in (("--provider", "nope"), ("--use", "nope"), ("--output", "nope")):
        assert _setup(espanso, option, value).exit_code == 2
    assert _setup(espanso, "--profile", "nosuch").exit_code == 2
    assert opened == []
    assert _setup(espanso, "--non-interactive", "--profile", "nosuch").exit_code == 2
    # PROMPT_PROVIDER is no longer setup's business: a bad one does not stop it (doctor says).
    monkeypatch.setenv("PROMPT_PROVIDER", "nope")
    assert _setup(espanso, "--non-interactive", "--no-smoke-test").exit_code == 0


def test_setup_in_legacy_mode_lists_what_to_set(
    espanso: Path, fake_run: FakeRunner, smoke_ok: list[str]
) -> None:
    result = _setup(espanso, "--non-interactive", "--provider", "ollama")
    assert result.exit_code == 0
    assert f"to do: add PROMPT_PROVIDER=ollama to {os.environ['PROMPTMEND_ENV']}" in result.stdout


def test_smoke_reports_a_cli_that_cannot_start() -> None:
    def broken(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, bytes]:
        raise OSError("no python")

    result = smoke.run("openrouter", runner=broken)
    assert not result.ok
    assert "could not run the CLI" in result.message


# --- odds and ends ------------------------------------------------------------------------


def test_stats_shows_estimated_unknown_and_local_costs(
    monkeypatch: pytest.MonkeyPatch, seed_history: SeedHistory
) -> None:
    for attempt in (
        {"provider": "ollama", "requested_model": "qwen3:8b", "cost_state": "not_applicable"},
        {"provider": "anthropic", "requested_model": "claude-sonnet-5"},
    ):
        op = {
            "id": history.new_operation_id(),
            "origin": "direct",
            "kind": "improve",
            "outcome": "ok",
        }
        seed_history(op, [{"endpoint": "remote", "status": 200, **attempt}])
    out = _run("stats", "--by", "provider").stdout
    assert "1 local (no cost)" in out
    assert "1 attempt(s) with an unknown cost" in out
    estimated: dict[str, Any] = {"estimated": {"USD": Decimal("0.5")}, "reported": {}}
    row = history.StatsRow("x", 1, 1, "2026-10-05T00:00:00.000000Z", None, None, {}, **estimated)
    from promptmend.commands import usage

    assert "estimated 0.5 USD" in " ".join(usage._row_text(row))


def test_stats_when_history_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", "false")
    assert "Usage history is off" in _run("stats").stdout


def test_an_unexpected_error_is_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend.commands import usage

    def broken(settings: Any) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(usage, "store", broken)
    result = _run("stats")
    assert result.exit_code == 1
    assert result.stderr == "error: unexpected RuntimeError: kaput\n"


def test_terminal_probes_survive_a_closed_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    class Closed:
        def isatty(self) -> None:
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr("sys.stdin", Closed())
    monkeypatch.setattr("sys.stdout", Closed())
    assert common.stdin_is_tty() is False
    assert common.stdout_is_tty() is False


def test_getpass_is_hidden_input(monkeypatch: pytest.MonkeyPatch) -> None:
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt: f"typed for {prompt}")
    assert common._getpass("KEY: ") == "typed for KEY: "


def test_secrets_set_empty_hidden_input(
    saved: Path, tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, "_getpass", lambda prompt: " ")
    result = _run("secrets", "set", "OPENROUTER_API_KEY")
    assert result.exit_code == 1
    assert "no value entered" in result.stderr


def test_config_show_quotes_an_unprintable_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_EXTRA_PATTERNS", "a\tb")
    assert "PROMPT_EXTRA_PATTERNS            'a\\tb'" in _run("config", "show").stdout


def test_config_set_warns_when_the_environment_wins(
    saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPT_PROFILE", "default")
    result = _run("config", "set", "PROMPT_PROFILE", "general")
    assert result.exit_code == 0
    assert "environment variable PROMPT_PROFILE is set" in result.stderr


def test_config_unset_of_an_unsaved_setting(saved: Path) -> None:
    result = _run("config", "unset", "PROMPT_PROFILE")
    assert result.exit_code == 0
    assert "nothing to do" in result.stdout
    assert not (saved / "config.toml").exists()


def test_secrets_remove_says_where_a_key_still_comes_from(
    saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_store_secret(saved)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    result = _run("secrets", "remove", "OPENROUTER_API_KEY", "--yes")
    assert result.exit_code == 0
    assert "still set, from environment" in result.stdout
    assert KEY not in result.output
    assert "nothing to do" in _run("secrets", "remove", "OPENROUTER_API_KEY").stdout


# config set saves a persona the gate's patterns match (exit 0) and warns on stderr with the
# finding names only, as validate would (#29 follow-up). The same exemptions apply.
def test_config_set_warns_about_a_flagged_persona(
    saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "jane.doe" + "@" + "example.com"
    result = _run("config", "set", "PROMPT_PERSONA", f"I am an analyst, mail {address}.")
    assert result.exit_code == 0, result.output
    assert "PROMPT_PERSONA saved in" in result.stdout
    assert (
        "warning: PROMPT_PERSONA matches the data-protection patterns: email; it is sent "
        "unscanned with every cloud call"
    ) in result.stderr
    assert address not in result.output
    assert address in (saved / "config.toml").read_text("utf-8")
    # A setting that makes the persona go out warns as well (the interface sets these).
    assert _run("config", "set", "PROMPT_PROFILE", "general").stderr == ""
    assert "PROMPT_PERSONA matches" in _run("config", "set", "PROMPT_PROFILE", "default").stderr
    assert _run("config", "set", "PROMPT_TIMEOUT_SECONDS", "30").stderr == ""
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")  # nothing leaves the machine
    result = _run("config", "set", "PROMPT_PERSONA", f"I am an analyst, mail {address}.")
    assert result.exit_code == 0
    assert result.stderr == ""
    monkeypatch.delenv("PROMPT_LOCAL_ONLY")
    assert _run("config", "set", "PROMPT_PERSONA", "I am a data engineer.").stderr == ""


# An empty value set on purpose reads "(empty)" in config show; one left at an empty default
# stays blank, and config get prints the raw value for scripts.
def test_config_show_marks_an_empty_value(saved: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run("config", "set", "PROMPT_TEMPERATURE", "").exit_code == 0
    monkeypatch.setenv("OPENROUTER_PRO_MAX_TOKENS", "")
    lines = _run("config", "show").stdout.splitlines()
    for name in ("PROMPT_TEMPERATURE", "OPENROUTER_PRO_MAX_TOKENS"):
        line = next(x for x in lines if x.startswith(f"{name} "))
        assert line.split()[1:2] == ["(empty)"], line
    line = next(x for x in lines if x.startswith("OPENROUTER_PROVIDER "))
    assert "(empty)" not in line
    assert _run("config", "get", "PROMPT_TEMPERATURE").stdout == "\n"
