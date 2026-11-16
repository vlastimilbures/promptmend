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
from promptmend.smoke import PLACEHOLDER_KEY

if TYPE_CHECKING:
    from conftest import FakeHttp, StubProvider

runner = CliRunner()

PLACEHOLDER = b"I am working as [role] in [company]."

# What a newline in the output becomes on this platform. trigger._emit() writes text to
# sys.stdout, which on Windows translates "\n" to "\r\n" (CPython opens the standard streams
# with newline=None there, and _main()'s reconfigure() keeps that), so a multi-line rewrite and
# the "sent despite" note reach Espanso with CRLF on Windows and LF elsewhere. CliRunner's
# stdout wrapper translates the same way. Frozen as it is, so a change either way fails here.
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
import promptmend.pipeline as pipeline
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
pipeline.is_concealed = lambda: None
pyperclip.paste = lambda: "rewrite this draft"
copied = []
pyperclip.copy = copied.append

# Whether the version was read by the time each output was printed (#318).
import promptmend.trigger as trigger

real_emit, at_output = trigger._emit, []


def emit(*args, **kwargs):
    real_emit(*args, **kwargs)
    at_output.append("importlib.metadata" in sys.modules)


trigger._emit = cli._emit = emit

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
    "at_output": at_output,
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
    "promptmend.wordings",
)
# The usage history (#89) writes each run after its output, so sqlite3 loads on the trigger
# path when tracking is on (PROMPT_HISTORY, the default), and never when it is off.
HISTORY_MODULES = ("promptmend.history", "sqlite3", "_sqlite3")

# Modules the trigger scenarios add to a bare interpreter, macOS: 307 on Python 3.12 and 306 on
# 3.14 with the usage history on, 296 on both with it off (see "Trigger start-up budget" in
# CONTRIBUTING.md), +3 since #281 (sanitize, pipeline, trigger). A coarse ceiling, not a
# timing: a new dependency tree on the trigger path crosses it; ordinary growth does not.
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
VERSION_FORBIDDEN = (
    *STARTUP_FORBIDDEN,
    "typer",
    "click",
    "rich",
    "promptmend.cli",
    "promptmend.trigger",
    "promptmend.pipeline",
)
IMPROVE_FORBIDDEN = (*STARTUP_FORBIDDEN, "importlib.metadata")
# Modules each run adds to a bare interpreter, macOS, Python 3.12, 2026-10-08: `--version`
# 108 (importlib.metadata is most of them), `improve` up to the provider call 149; both were
# 221 before #223; `improve` +3 since #281 (sanitize, pipeline, trigger). Python 3.13,
# 2026-11-15 (#312): `improve` up to the provider call and `persona` (history off) 145 each,
# from 167 before the built-in profiles stopped going through importlib.resources and
# hashlib, ctypes, tempfile and unicodedata moved into the functions that use them. Coarse,
# like MODULE_CEILING: a new import tree crosses them.
VERSION_CEILING = 150
IMPROVE_CEILING = 175
PERSONA_CEILING = 175

# The extension modules a Windows CPython ships as their own .pyd in DLLs/ that matter to the
# trigger path (not every one shipped: the test modules and the like are left out; the rest
# is built into pythonXY.dll). An endpoint scanner inspects each as it loads (#312),
# so none loads before the provider call (or, for persona, at all) unless the call needs it:
# the clipboard probe's _ctypes only when the clipboard is read, the HTTP client's _socket
# and _ssl only for the call, the usage history's _sqlite3 and _decimal only after the output.
WINDOWS_EXTENSIONS = (
    "_asyncio",
    "_bz2",
    "_ctypes",
    "_decimal",
    "_elementtree",
    "_hashlib",
    "_lzma",
    "_multiprocessing",
    "_overlapped",
    "_queue",
    "_socket",
    "_sqlite3",
    "_ssl",
    "_uuid",
    "_wmi",
    "_zoneinfo",
    "_zstd",
    "pyexpat",
    "select",
    "unicodedata",
    "winsound",
)
# What `import typer` itself may load of them: typer.main imports shutil (which tries bz2,
# lzma and, from 3.14, compression.zstd), subprocess (select, on POSIX only), uuid and
# platform (_wmi, on Windows from 3.12); on Windows its vendored click also imports ctypes
# for the console (typer._click._compat -> _winconsole), so _ctypes is Typer's there too.
# Which ones load depends on the OS and the Python version, so the typer_extensions fixture
# measures them in a fresh interpreter, held to this ceiling. They stay until a trigger entry
# runs without Typer (#313). Because Typer has already loaded _ctypes on Windows, "the
# clipboard probe's _ctypes only when the clipboard is read" is effectively checked on POSIX.
TYPER_EXTENSIONS_ALLOWED = ("_bz2", "_ctypes", "_lzma", "select", "_uuid", "_wmi", "_zstd")


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


def _startup(
    script: str, tmp: Path, settings: dict[str, str] | None = None
) -> tuple[bytes, dict[str, Any]]:
    """Run ``script`` in a fresh interpreter in _isolated_env(), plus ``settings``."""
    out = tmp / "modules.json"
    proc = subprocess.run(
        [sys.executable, "-c", script, str(out)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env={**_isolated_env(tmp), **(settings or {})},
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


_TYPER_RUN = """
import sys
out = sys.argv[1]
started = set(sys.modules)
import typer
import json
from pathlib import Path
Path(out).write_text(json.dumps({"added": sorted(set(sys.modules) - started)}), encoding="utf-8")
"""


@pytest.fixture(scope="module")
def typer_extensions(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    """The WINDOWS_EXTENSIONS that `import typer` loads in a fresh interpreter, on this OS and
    Python: the only ones a trigger may load before its provider call."""
    _, data = _startup(_TYPER_RUN, tmp_path_factory.mktemp("typer"))
    return set(_loaded(data["added"], WINDOWS_EXTENSIONS))


def test_typer_loads_only_the_allowed_extensions(typer_extensions: set[str]) -> None:
    assert typer_extensions <= set(TYPER_EXTENSIONS_ALLOWED)


def test_improve_startup_budget(tmp_path: Path, typer_extensions: set[str]) -> None:
    stdout, data = _startup(_IMPROVE_RUN, tmp_path)
    assert (stdout, data["code"]) == (b"done", 0)
    assert _loaded(data["added"], IMPROVE_FORBIDDEN) == []
    assert len(data["added"]) <= IMPROVE_CEILING, f"improve added {len(data['added'])}"
    assert set(_loaded(data["added"], WINDOWS_EXTENSIONS)) <= typer_extensions


# The same through a gated cloud provider (OpenRouter, a placeholder key): the gate scans the
# draft before the call, and an ASCII draft needs no unicodedata for that (#312). The single
# HTTP function is stubbed both where the provider calls it and at its source, and every
# proxy points at a closed loopback port, so a stray call cannot leave the machine.
_CLOUD_RUN = (
    _IMPROVE_RUN.replace('"ollama"', '"openrouter"')
    .replace("import ollama", "import base, openai_compatible")
    .replace(
        "ollama.post_json = post_json",
        "openai_compatible.post_json = post_json\nbase.post_json = post_json",
    )
    .replace(
        '{"message": {"content": "done"}, "done_reason": "stop"}',
        '{"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}',
    )
)
_DEAD_PROXY = "http://127.0.0.1:9"
CLOUD_KEY = {
    "OPENROUTER_API_KEY": PLACEHOLDER_KEY,
    **dict.fromkeys(("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"), _DEAD_PROXY),
    "NO_PROXY": "",
}


def test_cloud_improve_startup_budget(tmp_path: Path, typer_extensions: set[str]) -> None:
    assert "ollama" not in _CLOUD_RUN
    assert "base.post_json = post_json" in _CLOUD_RUN
    stdout, data = _startup(_CLOUD_RUN, tmp_path, CLOUD_KEY)
    assert (stdout, data["code"]) == (b"done", 0)
    assert "promptmend.gate" in data["added"]
    assert _loaded(data["added"], IMPROVE_FORBIDDEN) == []
    assert len(data["added"]) <= IMPROVE_CEILING, f"improve added {len(data['added'])}"
    assert set(_loaded(data["added"], WINDOWS_EXTENSIONS)) <= typer_extensions


# The whole persona run (the -p- snippet), with the usage history off: with it on, the
# history row loads sqlite3 after the output, as for improve (HISTORY_MODULES).
NO_HISTORY = {"PROMPT_HISTORY": "false"}
_PERSONA_RUN = """
import sys
out = sys.argv[1]
started = set(sys.modules)
sys.argv = ["promptmend", "persona"]
from promptmend.entry import main
try:
    main()
except SystemExit as exc:
    if exc.code:
        raise
added = sorted(set(sys.modules) - started)
import json
from pathlib import Path
Path(out).write_text(json.dumps({"added": added}), encoding="utf-8")
"""


def test_persona_startup_budget(tmp_path: Path, typer_extensions: set[str]) -> None:
    stdout, data = _startup(_PERSONA_RUN, tmp_path, NO_HISTORY)
    assert stdout == PLACEHOLDER
    assert _loaded(data["added"], IMPROVE_FORBIDDEN) == []
    assert len(data["added"]) <= PERSONA_CEILING, f"persona added {len(data['added'])}"
    assert set(_loaded(data["added"], WINDOWS_EXTENSIONS)) <= typer_extensions


# With Typer imported before the count starts, the trigger path itself adds no Windows
# extension module: the ones it loads are Typer's, not a module of ours hiding behind it.
# Its own clipboard probe stays out too: a --text draft reads no clipboard.
@pytest.mark.parametrize(
    "script", [_IMPROVE_RUN, _CLOUD_RUN, _PERSONA_RUN], ids=["improve", "cloud", "persona"]
)
def test_trigger_path_adds_no_windows_extension_of_its_own(tmp_path: Path, script: str) -> None:
    after_typer = script.replace(
        "started = set(sys.modules)", "import typer\nstarted = set(sys.modules)", 1
    )
    assert after_typer != script
    _, data = _startup(after_typer, tmp_path, {**NO_HISTORY, **CLOUD_KEY})
    assert "typer" not in data["added"]
    assert _loaded(data["added"], WINDOWS_EXTENSIONS) == []


# The boundary of the #281 split: pipeline.py (the rewrite's steps) imports neither Typer nor
# the CLI nor the trigger run, so a Typer-free trigger entry (#299) can build on it, and the
# interface's Try tab reaches the pipeline without importing cli.py. Each in a fresh interpreter.
_IMPORT_RUN = """
import sys
out, module = sys.argv[1], sys.argv[2]
import importlib
importlib.import_module(module)
import json
from pathlib import Path
Path(out).write_text(json.dumps({"loaded": sorted(sys.modules)}), encoding="utf-8")
"""


def _imports(module: str, tmp: Path) -> list[str]:
    out = tmp / "modules.json"
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_RUN, str(out), module],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=_isolated_env(tmp),
        cwd=tmp,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    loaded: list[str] = json.loads(out.read_text(encoding="utf-8"))["loaded"]
    return loaded


def test_pipeline_imports_no_typer_or_cli(tmp_path: Path) -> None:
    loaded = _imports("promptmend.pipeline", tmp_path)
    assert "promptmend.pipeline" in loaded
    assert _loaded(loaded, ("typer", "click", "promptmend.cli", "promptmend.trigger")) == []


# The built-in profiles are read as plain files, on first use: importlib.resources would bring
# zipfile, and with it bz2 and lzma (#312).
def test_prompt_builder_imports_no_resources_or_zipfile(tmp_path: Path) -> None:
    loaded = _imports("promptmend.prompt_builder", tmp_path)
    assert "promptmend.prompt_builder" in loaded
    assert _loaded(loaded, ("importlib.resources", "zipfile", "bz2", "lzma")) == []


def test_try_pane_does_not_import_cli(tmp_path: Path) -> None:
    loaded = _imports("promptmend.tui.try_pane", tmp_path)
    assert "promptmend.pipeline" in loaded
    assert _loaded(loaded, ("promptmend.cli", "promptmend.trigger")) == []


# The version is read where it is used, never at import (#318): importing the usage history
# does not load importlib.metadata, and a trigger run reads the installed metadata only to
# record the run (its app_version), after the output is printed: not yet loaded when the first
# output is printed, and never with tracking off (which also catches a dependency that starts
# importing importlib.metadata eagerly). Later outputs in the same process follow the first
# run's record, so only the first is checked in the tracking-on run.
def test_history_import_does_not_read_the_version(tmp_path: Path) -> None:
    loaded = _imports("promptmend.history", tmp_path)
    assert "promptmend.history" in loaded
    assert _loaded(loaded, ("importlib.metadata",)) == []


def test_trigger_run_reads_the_version_only_after_output_to_record(
    trigger_run: tuple[list[bytes], dict[str, Any]],
) -> None:
    _, data = trigger_run
    assert len(data["at_output"]) == 7  # one output per scenario
    assert data["at_output"][0] is False
    if not data["tracking"]:
        assert not any(data["at_output"])
    assert ("importlib.metadata" in data["loaded"]) is data["tracking"]


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
