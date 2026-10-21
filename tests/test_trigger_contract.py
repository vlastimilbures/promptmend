"""Freezes what every Espanso trigger depends on: the exact bytes improve and persona print,
and what running them imports. Espanso pastes stdout verbatim and starts a fresh process for
each trigger, so a changed byte reaches the user and a heavy import slows every expansion.
Parser (usage) errors exit 2 and are not part of this contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from promptmend.cli import app
from promptmend.config import env_names
from promptmend.providers.base import ProviderError
from promptmend.providers.openai_compatible import OpenAICompatibleProvider

if TYPE_CHECKING:
    from conftest import FakeHttp, StubProvider

runner = CliRunner()

PLACEHOLDER = b"I am working as [role] in [company]."

# What a newline in the output becomes on this platform. _emit() writes text to sys.stdout,
# which on Windows translates "\n" to "\r\n" (CPython opens the standard streams with
# newline=None there, and _main()'s reconfigure() keeps that), so a multi-line rewrite and the
# "sent despite" note reach Espanso with CRLF on Windows and LF elsewhere. CliRunner's stdout
# wrapper translates the same way. Frozen as it is, so a change either way fails here.
EOL = os.linesep.encode()


# Every in-process golden test runs with the usage history on (the default) and off: recording
# a run must not change a byte of it.
@pytest.fixture(autouse=True, params=["true", "false"], ids=["history-on", "history-off"])
def _history_setting(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", request.param)


def _golden(args: list[str], expected: bytes) -> None:
    """``expected`` is written with LF; each newline is checked as this platform prints it."""
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert result.stdout_bytes == expected.replace(b"\n", EOL)
    assert not result.stdout_bytes.endswith(b"\n")


IMPROVE = ["improve", "--provider", "ollama", "--source", "argument", "--text", "a draft"]


# The rewrite is pasted as-is, UTF-8, cleaned of control characters, with no newline added.
def test_improve_success(stub_provider: StubProvider) -> None:
    stub_provider.result = "Přepiš —\ndone\x1b[201~ ✓"
    _golden(IMPROVE, "Přepiš —\ndone[201~ ✓".encode())


# Each failure class becomes one marker; only an unexpected error gets the prefix.
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ProviderError("Ollama request failed"), b"[promptmend: Ollama request failed]"),
        (
            ValueError("OLLAMA_TIMEOUT is not a number"),
            b"[promptmend: OLLAMA_TIMEOUT is not a number]",
        ),
        (RuntimeError("kaput"), b"[promptmend: unexpected error: kaput]"),
    ],
    ids=["provider-error", "value-error", "unexpected"],
)
def test_improve_error_marker(stub_provider: StubProvider, exc: Exception, expected: bytes) -> None:
    stub_provider.exc = exc
    _golden(IMPROVE, expected)


# A bad setting fails before any provider is built, with the same marker shape.
SETTINGS_ERROR = b"[promptmend: PROMPT_LOCAL_ONLY must be true or false, got 'maybe']"


def test_improve_settings_error_marker(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "maybe")
    _golden(IMPROVE, SETTINGS_ERROR)
    assert not stub_provider.built


# --allow-flagged opens the paste with the note, on success and when the call fails after
# the gate let the draft through.
DRAFT = "CONFIDENTIAL: summarise the board minutes for jane@example.com"
NOTE = b"[promptmend: sent despite: email, confidential_label]\n\n"


FLAGGED = ["improve", "--provider", "openrouter", "--allow-flagged", "--source", "argument"]


def test_improve_sent_despite_note(monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_http.reply({"choices": [{"message": {"content": "rewrite"}}]})
    _golden([*FLAGGED, "--text", DRAFT], NOTE + b"rewrite")


def test_improve_sent_despite_note_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def fail(self: OpenAICompatibleProvider, prompt: str, system_prompt: str) -> None:
        raise ProviderError("upstream down")

    monkeypatch.setattr(OpenAICompatibleProvider, "generate", fail)
    _golden([*FLAGGED, "--text", DRAFT], NOTE + b"[promptmend: upstream down]")


# Clipboard output (PROMPT_OUTPUT=clipboard, #134): a success prints nothing at all, the note
# alone is printed without its blank line, and error markers are unchanged.
def test_improve_clipboard_output(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider
) -> None:
    monkeypatch.setattr("pyperclip.copy", lambda text: None)
    monkeypatch.setenv("PROMPT_OUTPUT", "clipboard")
    _golden(IMPROVE, b"")
    stub_provider.exc = ProviderError("Ollama request failed")
    _golden(IMPROVE, b"[promptmend: Ollama request failed]")


def test_improve_clipboard_output_sent_despite(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp
) -> None:
    monkeypatch.setattr("pyperclip.copy", lambda text: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_http.reply({"choices": [{"message": {"content": "rewrite"}}]})
    _golden([*FLAGGED, "--output", "clipboard", "--text", DRAFT], NOTE.rstrip(b"\n"))


def test_persona_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_PERSONA", "I am a risk analyst at Česká banka.")
    _golden(["persona"], "I am a risk analyst at Česká banka.".encode())


def test_persona_unset_placeholder() -> None:
    _golden(["persona"], PLACEHOLDER)


# Settings that fail to load never give a marker or a blank: an unrelated bad value still
# leaves the persona, and a persona that cannot be read gives the placeholder.
def test_persona_when_settings_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROMPT_PERSONA", "I am a tester.")
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "maybe")
    _golden(["persona"], b"I am a tester.")
    monkeypatch.delenv("PROMPT_PERSONA")
    (tmp_path / ".env").write_bytes("PROMPT_PERSONA=never shown\n".encode("utf-16"))
    _golden(["persona"], PLACEHOLDER)


# Runs the trigger commands in a fresh interpreter, the way Espanso does, with the real
# provider and gate over httpx.MockTransport, then lists every module they imported. Each
# scenario takes a different branch (clipboard, gate, provider error, settings error, clipboard
# output), since a
# heavy import can hide in any of them. The clipboard and its concealed-item probe are stubbed:
# a test never touches the real clipboard.
_TRIGGER_RUN = """
import sys
started = set(sys.modules)
import json
import os
from pathlib import Path

import httpx
import pyperclip

import promptmend.cli as cli
from promptmend.providers import base

real_client = httpx.Client
status = [200]


def handler(request):
    if status[0] != 200:
        return httpx.Response(status[0], json={"error": {"message": "down"}})
    if "/api/chat" in str(request.url):
        reply = {"message": {"content": "local\\nline"}, "done_reason": "stop"}
        return httpx.Response(200, json=reply)
    choice = {"message": {"content": "cloud"}, "finish_reason": "stop"}
    return httpx.Response(200, json={"choices": [choice]})


def client(*args, **kwargs):
    return real_client(*args, **{**kwargs, "transport": httpx.MockTransport(handler)})


httpx.Client = client
base._sleep = lambda seconds: None
cli.is_concealed = lambda: None
pyperclip.paste = lambda: "rewrite this draft"
copied = []
pyperclip.copy = copied.append

local = ["improve", "--provider", "ollama", "--source", "argument", "--text", "x"]
cloud = ["improve", "--provider", "openrouter", "--source", "clipboard"]
scenarios = [
    (local, {}, 200),
    (cloud, {}, 200),
    (cloud, {}, 500),
    (cloud, {"PROMPT_LOCAL_ONLY": "maybe"}, 200),
    (["persona"], {}, 200),
    (["persona"], {"PROMPT_LOCAL_ONLY": "maybe"}, 200),
    (cloud, {"PROMPT_OUTPUT": "clipboard"}, 200),
]
codes = []
for argv, env, code in scenarios:
    os.environ.pop("PROMPT_LOCAL_ONLY", None)
    os.environ.pop("PROMPT_OUTPUT", None)
    os.environ.update(env)
    status[0] = code
    try:
        cli.app(argv)
    except SystemExit as exc:
        codes.append(exc.code)
    sys.stdout.write("\\x1e")
    sys.stdout.flush()
result = {
    "codes": codes,
    "copied": copied,
    "added": sorted(set(sys.modules) - started),
    "loaded": sorted(sys.modules),
}
Path(sys.argv[1]).write_text(json.dumps(result), encoding="utf-8")
"""

# Never on the trigger path: the Textual interface (#93), config writers (#84), the keyring
# and the Espanso deploy service (#86), the management commands and their services (#92).
# rich is installed with Typer but not loaded today: Typer imports it only for help and usage
# errors.
FORBIDDEN = (
    "textual",
    "rich.console",
    "tomli_w",
    "tomlkit",
    "keyring",
    "promptmend.deploy",
    "promptmend.espanso_windows",
    "promptmend.commands",
    "promptmend.doctor",
    "promptmend.smoke",
    "promptmend.config_store",
    "promptmend.update_check",
    "promptmend.local_probe",
)
# The usage history (#89) writes each run after its output, so sqlite3 loads on the trigger
# path when tracking is on (PROMPT_HISTORY, the default), and never when it is off.
HISTORY_MODULES = ("promptmend.history", "sqlite3", "_sqlite3")

# Modules the trigger scenarios add to a bare interpreter, macOS: 307 on Python 3.12 and 306 on
# 3.14 with the usage history on, 296 on both with it off (see "Trigger start-up budget" in
# CONTRIBUTING.md). A coarse ceiling, not a timing: a new dependency tree on the trigger path
# crosses it; ordinary growth does not.
MODULE_CEILING = 400

# Variables that would make the child load modules before the script runs (coverage's .pth
# hook preloads sqlite3) or from somewhere else, which would hide or fake an import.
_PRELOADING = ("COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG", "PYTHONSTARTUP", "PYTHONPATH")


@pytest.fixture(scope="module", params=[True, False], ids=["tracking-on", "tracking-off"])
def trigger_run(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[list[bytes], dict[str, Any]]:
    tmp = tmp_path_factory.mktemp("trigger")
    (tmp / ".env").write_text("", encoding="utf-8")
    # Module-scoped, so conftest's per-test isolation has not run yet: drop every setting here,
    # and keep the run's config, data (the usage history) and home in this temp dir.
    dropped = {*env_names(), *_PRELOADING}
    env = {key: value for key, value in os.environ.items() if key not in dropped}
    env["PROMPTMEND_ENV"] = str(tmp / ".env")
    for name in ("XDG_CONFIG_HOME", "APPDATA", "XDG_DATA_HOME", "LOCALAPPDATA"):
        env[name] = str(tmp / name.lower())
    for name in ("HOME", "USERPROFILE"):
        env[name] = str(tmp / "home")
    if not request.param:
        env["PROMPT_HISTORY"] = "false"
    # Built at runtime, so no key-shaped literal lands in the repo.
    env["OPENROUTER_API_KEY"] = "-".join(("test", "key"))
    out = tmp / "modules.json"
    proc = subprocess.run(
        [sys.executable, "-c", _TRIGGER_RUN, str(out)],
        capture_output=True,
        env=env,
        cwd=tmp,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    data = json.loads(out.read_text(encoding="utf-8"))
    data["tracking"] = request.param
    return proc.stdout.split(b"\x1e"), data


# The real process prints the same contract as the in-process runner and exits 0 every time,
# with tracking on and off.
def test_trigger_run_output(trigger_run: tuple[list[bytes], dict[str, Any]]) -> None:
    outputs, data = trigger_run
    local, cloud, cloud_error, settings_error, persona, persona_fallback, copied, rest = outputs
    assert (local, cloud, persona, persona_fallback, copied, rest) == (
        b"local" + EOL + b"line",
        b"cloud",
        PLACEHOLDER,
        PLACEHOLDER,
        b"",
        b"",
    )
    assert cloud_error.startswith(b"[promptmend: ")
    assert cloud_error.endswith(b"]")
    assert settings_error == SETTINGS_ERROR
    assert data["codes"] == [0] * 7
    assert data["copied"] == ["cloud"]


# Checked against everything loaded, not only what the run added, so a module loaded before
# the script started cannot make the check pass vacuously.
@pytest.mark.parametrize("module", FORBIDDEN + HISTORY_MODULES)
def test_trigger_path_does_not_import(
    trigger_run: tuple[list[bytes], dict[str, Any]], module: str
) -> None:
    _, data = trigger_run
    if data["tracking"] and module in HISTORY_MODULES:
        pytest.skip("history is on: the run is recorded")
    assert not [m for m in data["loaded"] if m == module or m.startswith(module + ".")]


# With tracking on, the guard above is not vacuous: the history really was loaded.
def test_tracking_run_loads_history(trigger_run: tuple[list[bytes], dict[str, Any]]) -> None:
    _, data = trigger_run
    loaded = set(data["loaded"])
    assert (
        set(HISTORY_MODULES) <= loaded if data["tracking"] else not (set(HISTORY_MODULES) & loaded)
    )


def test_trigger_path_module_count(trigger_run: tuple[list[bytes], dict[str, Any]]) -> None:
    _, data = trigger_run
    added = len(data["added"])
    assert added <= MODULE_CEILING, f"the trigger run added {added} modules"


# Start-up budget (#223): what a call loads before it does any work, each in a fresh
# interpreter through the console script's entry.main(), as Espanso and a terminal run it.
# `--version` is answered before the CLI is imported; `improve` loads no HTTP client and no
# clipboard library before it reaches the provider (a --text draft never touches the
# clipboard) and nothing of the usage history before it records.
_VERSION_RUN = """
import sys
out = sys.argv[1]
started = set(sys.modules)
sys.argv = ["promptmend", "--version"]
from promptmend.entry import main
main()
added = sorted(set(sys.modules) - started)
import json
from pathlib import Path
Path(out).write_text(json.dumps({"added": added}), encoding="utf-8")
"""

_IMPROVE_RUN = """
import sys
out = sys.argv[1]
started = set(sys.modules)
sys.argv = ["promptmend", "improve", "--provider", "ollama", "--source", "argument", "--text", "x"]
from promptmend.providers import ollama
seen = []


def post_json(*args, **kwargs):
    seen.append(sorted(set(sys.modules) - started))
    return {"message": {"content": "done"}, "done_reason": "stop"}


ollama.post_json = post_json
from promptmend.entry import main
try:
    main()
except SystemExit as exc:
    code = exc.code
import json
from pathlib import Path
Path(out).write_text(json.dumps({"code": code, "added": seen[0]}), encoding="utf-8")
"""

# Never loaded by `--version`, nor by `improve` before its provider call. `--version` reads
# the installed metadata; `improve` does not need it.
STARTUP_FORBIDDEN = ("httpx", "pyperclip", "sqlite3", "textual", *FORBIDDEN)
VERSION_FORBIDDEN = (*STARTUP_FORBIDDEN, "typer", "click", "rich", "promptmend.cli")
IMPROVE_FORBIDDEN = (*STARTUP_FORBIDDEN, "importlib.metadata")
# Modules each run adds to a bare interpreter, macOS, Python 3.12, 2026-10-08: `--version`
# 108 (importlib.metadata is most of them), `improve` up to the provider call 149; both were
# 221 before #223. Coarse, like MODULE_CEILING: a new import tree crosses them.
VERSION_CEILING = 150
IMPROVE_CEILING = 200


def _isolated_env(tmp: Path) -> dict[str, str]:
    """The environment without any setting or preloading variable, with the config, data
    and home folders in ``tmp``."""
    env = {
        key: value for key, value in os.environ.items() if key not in {*env_names(), *_PRELOADING}
    }
    env["PROMPTMEND_ENV"] = str(tmp / ".env")
    for name in ("XDG_CONFIG_HOME", "APPDATA", "XDG_DATA_HOME", "LOCALAPPDATA"):
        env[name] = str(tmp / name.lower())
    for name in ("HOME", "USERPROFILE"):
        env[name] = str(tmp / "home")
    return env


def _startup(script: str, tmp: Path) -> tuple[bytes, dict[str, Any]]:
    out = tmp / "modules.json"
    proc = subprocess.run(
        [sys.executable, "-c", script, str(out)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=_isolated_env(tmp),
        cwd=tmp,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout, json.loads(out.read_text(encoding="utf-8"))


def _loaded(added: list[str], modules: tuple[str, ...]) -> list[str]:
    return [m for m in added if any(m == x or m.startswith(x + ".") for x in modules)]


def test_version_startup_budget(tmp_path: Path) -> None:
    from promptmend import installed_version

    stdout, data = _startup(_VERSION_RUN, tmp_path)
    assert stdout == f"{installed_version()}\n".encode().replace(b"\n", EOL)
    assert _loaded(data["added"], VERSION_FORBIDDEN) == []
    assert len(data["added"]) <= VERSION_CEILING, f"--version added {len(data['added'])}"


def test_improve_startup_budget(tmp_path: Path) -> None:
    stdout, data = _startup(_IMPROVE_RUN, tmp_path)
    assert (stdout, data["code"]) == (b"done", 0)
    assert _loaded(data["added"], IMPROVE_FORBIDDEN) == []
    assert len(data["added"]) <= IMPROVE_CEILING, f"improve added {len(data['added'])}"


# The headless management commands, --help and the bare command without a terminal never load
# the interface (#93): Textual is imported only once it opens. Run in one fresh interpreter,
# with Espanso, uv and brew answering nothing and the clipboard never read.
_MANAGEMENT_RUN = """
import json
import sys
from pathlib import Path

from promptmend import deploy
import promptmend.cli as cli

deploy.run_command = lambda argv: None
scenarios = [
    [],
    ["--help"],
    ["doctor", "--no-clipboard"],
    ["doctor", "--json", "--no-clipboard"],
    ["config", "show"],
    ["secrets", "status"],
    ["profiles", "list"],
    ["stats"],
    ["espanso", "status"],
    ["ui"],
]
codes = []
for argv in scenarios:
    try:
        cli.app(argv)
    except SystemExit as exc:
        codes.append(exc.code)
Path(sys.argv[1]).write_text(json.dumps({"codes": codes, "loaded": sorted(sys.modules)}))
"""


def test_management_commands_do_not_import_the_interface(tmp_path: Path) -> None:
    env = _isolated_env(tmp_path)
    out = tmp_path / "modules.json"
    proc = subprocess.run(
        [sys.executable, "-c", _MANAGEMENT_RUN, str(out)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    data = json.loads(out.read_text(encoding="utf-8"))
    # Bare without a terminal: help, exit 2. `ui` without a terminal: exit 3 (#92's code),
    # before Textual loads.
    assert data["codes"][0] == 2
    assert data["codes"][-1] == 3
    loaded = data["loaded"]
    assert "promptmend.commands.ui" in loaded  # --help listed it, so the guard is real
    for module in ("textual", "promptmend.tui"):
        assert not [m for m in loaded if m == module or m.startswith(module + ".")], module
