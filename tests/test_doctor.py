"""Each doctor check's verdicts (doctor.py), offline: the runner, the clipboard and the
history store are faked; the CLI-level tests are in tests/test_commands.py."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import promptmend
from promptmend import assets, config, deploy, doctor, update_check
from promptmend.config import ConfigLayers, Settings
from promptmend.history import Health

LAUNCHER = "/Users/me/.local/bin/promptmend"


def _runner(answers: dict[str, str] | None = None) -> deploy.Runner:
    found = answers or {}
    return lambda argv: found.get(" ".join(argv))


def _status(check_id: str, report: doctor.Report) -> doctor.Check:
    return next(c for c in report.checks if c.id == check_id)


@pytest.fixture
def no_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyperclip

    from promptmend import clipboard_guard

    monkeypatch.setattr(clipboard_guard, "is_concealed", lambda: None)
    monkeypatch.setattr(pyperclip, "paste", lambda: "")


# --- install ------------------------------------------------------------------------------


def test_install_reports_the_channel(tmp_path: Path) -> None:
    exe = tmp_path / "bin" / f"promptmend{EXE}"
    exe.parent.mkdir()
    exe.write_text("", "utf-8")
    check = doctor._install_check(deploy.Launcher(exe, "uv"), None)
    assert (check.status, check.data["channel"], check.data["launcher"]) == ("ok", "uv", str(exe))


def test_install_without_a_launcher_guesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = doctor._install_check(None, "nothing found")
    assert (check.status, check.data["channel"]) == ("warn", "unknown")
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("", "utf-8")
    assert doctor._install_check(None, "x").data["channel"] == "editable"


# --- path_shadow --------------------------------------------------------------------------


EXE = ".exe" if os.name == "nt" else ""


def _bin(folder: Path, *names: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        exe = folder / (name + EXE)
        exe.write_text("", "utf-8")
        exe.chmod(0o755)
    return folder


def test_path_shadow_ok_with_the_own_launcher(tmp_path: Path) -> None:
    own = _bin(tmp_path / "own", "promptmend", "prompt-workflow")
    found = deploy.Launcher(own / f"promptmend{EXE}", "uv")
    check = doctor._path_shadow_check(found, environ={"PATH": str(own)})
    assert check.status == "ok"
    assert check.data["found"] == str(own / f"promptmend{EXE}")


def test_path_shadow_info_when_not_on_path_or_no_launcher(tmp_path: Path) -> None:
    own = _bin(tmp_path / "own", "promptmend")
    found = deploy.Launcher(own / f"promptmend{EXE}", "uv")
    check = doctor._path_shadow_check(found, environ={"PATH": str(tmp_path / "empty")})
    assert (check.status, check.message) == ("info", "`promptmend` is not on PATH")
    assert doctor._path_shadow_check(None, environ={"PATH": str(own)}).status == "info"


@pytest.mark.parametrize(
    ("folder", "venv", "hint"),
    [
        ("old/bin", None, "uninstall the old CLI, or put {own} earlier on PATH"),
        ("venv/bin", "venv", "a virtual environment is active: run `deactivate`"),
        ("repo/.venv/bin", None, "remove {old} from PATH"),
    ],
)
def test_path_shadow_warns_with_a_hint(
    tmp_path: Path, folder: str, venv: str | None, hint: str
) -> None:
    own = _bin(tmp_path / "own", "promptmend")
    old = _bin(tmp_path / folder, "promptmend")
    environ = {"PATH": os.pathsep.join((str(old), str(own)))}
    if venv:
        environ["VIRTUAL_ENV"] = str(tmp_path / venv)
    check = doctor._path_shadow_check(
        deploy.Launcher(own / f"promptmend{EXE}", "uv"), environ=environ
    )
    assert check.status == "warn"
    assert check.message == (
        f"`promptmend` on PATH is {old / f'promptmend{EXE}'}, not the installed launcher "
        f"{own / f'promptmend{EXE}'}: " + hint.format(own=own, old=old)
    )
    assert check.data == {
        "command": "promptmend",
        "found": str(old / f"promptmend{EXE}"),
        "launcher": str(own / f"promptmend{EXE}"),
    }


def test_path_shadow_warns_about_the_legacy_alias(tmp_path: Path) -> None:
    # This install has no alias next to it: samefile() fails, and the old one is reported.
    own = _bin(tmp_path / "own", "promptmend")
    old = _bin(tmp_path / "old", "prompt-workflow")
    environ = {"PATH": os.pathsep.join((str(old), str(own)))}
    check = doctor._path_shadow_check(
        deploy.Launcher(own / f"promptmend{EXE}", "uv"), environ=environ
    )
    assert check.status == "warn"
    assert check.message.startswith(
        f"`prompt-workflow` on PATH is {old / f'prompt-workflow{EXE}'},"
    )
    assert check.data["launcher"] == str(own / f"prompt-workflow{EXE}")


# --- version and update (#197) ------------------------------------------------------------


def _install(channel: str, editable: bool = False) -> doctor.Check:
    return doctor.Check("install", "ok", "", {"channel": channel, "editable": editable})


def test_version_with_a_newer_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "__version__", "0.21.0")
    update = update_check.UpdateStatus(update_check.AVAILABLE, "0.22.0", "2026-10-07T12:00:00Z")
    check = doctor._version_check(update, _install("homebrew"))
    assert check.status == doctor.INFO
    assert check.message == (
        "promptmend 0.21.0, 0.22.0 available: brew update && brew upgrade promptmend"
    )
    assert {k: check.data[k] for k in ("latest", "update_available", "checked_at")} == {
        "latest": "0.22.0",
        "update_available": True,
        "checked_at": "2026-10-07T12:00:00Z",
    }
    assert set(check.data) == set(doctor.DATA_KEYS["version"])
    # A checkout's own update wins over its uv launcher.
    editable = doctor._version_check(update, _install("uv", editable=True))
    assert editable.message.endswith(f"available: {update_check.upgrade_command('editable')}")
    assert doctor._version_check(update, None).message.endswith("see docs/install.md")


@pytest.mark.parametrize(
    ("state", "available"),
    [(update_check.LATEST, False), (update_check.UNKNOWN, None), (update_check.OFF, None)],
)
def test_version_without_a_newer_release(
    monkeypatch: pytest.MonkeyPatch, state: str, available: bool | None
) -> None:
    monkeypatch.setattr(doctor, "__version__", "0.21.0")
    check = doctor._version_check(update_check.UpdateStatus(state), _install("uv"))
    assert (check.status, check.message) == (doctor.INFO, "promptmend 0.21.0")
    assert check.data["update_available"] is available


def test_run_checks_for_an_update_unless_given_one(
    espanso: Path, no_clipboard: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[bool] = []

    def fake_check(on: bool, now: object) -> update_check.UpdateStatus:
        asked.append(on)
        return update_check.UpdateStatus(update_check.AVAILABLE, "99.0.0", "now")

    monkeypatch.setattr(update_check, "_check", fake_check)
    report = doctor.run(espanso_dir=espanso, launcher=LAUNCHER, runner=_runner())
    version = _status("version", report)
    assert asked == [True]
    assert (version.status, version.data["latest"]) == (doctor.INFO, "99.0.0")
    assert "99.0.0 available" in version.message
    assert [c.id for c in report.checks] == list(doctor.CHECK_IDS)
    given = update_check.UpdateStatus(update_check.LATEST, "1.0.0", "then")
    report = doctor.run(espanso_dir=espanso, launcher=LAUNCHER, runner=_runner(), update=given)
    assert asked == [True]
    assert _status("version", report).data["latest"] == "1.0.0"


def test_run_asks_nothing_when_the_setting_is_bad(
    espanso: Path, no_clipboard: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: a rejected PROMPT_UPDATE_CHECK may have meant off, so nothing is asked,
    although repair mode falls back to the default (on) for the rest of the report."""
    monkeypatch.setenv("PROMPT_UPDATE_CHECK", "maybe")
    asked: list[bool] = []

    def fake_fetch() -> str:
        asked.append(True)
        return "99.0.0"

    monkeypatch.setattr(update_check, "_fetch", fake_fetch)
    report = doctor.run(espanso_dir=espanso, launcher=LAUNCHER, runner=_runner())
    assert asked == []
    version = _status("version", report)
    assert (version.message, version.data["update_available"]) == (
        f"promptmend {promptmend.__version__}",
        None,
    )


# --- config and keys ----------------------------------------------------------------------


def _config(**changes: Any) -> doctor.Check:
    layers = ConfigLayers.resolve(strict=False)
    return doctor._config_check(layers, changes.get("strict"))


def test_config_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _config().data["mode"] == "legacy"
    monkeypatch.delenv("PROMPTMEND_ENV")
    assert _config().data["mode"] == "defaults"
    assert _config().status == "ok"
    env = config._user_config_dir() / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("PROMPT_PROFILE=general\n", "utf-8")
    assert _config().data["mode"] == "env"
    config.settings_file().write_text("config_version = 1\n", "utf-8")
    check = _config()
    assert check.data["mode"] == "saved"
    assert check.status == "warn"  # the .env is now ignored: a finding


def test_config_strict_error_fails() -> None:
    check = _config(strict="PROMPT_LOCAL_ONLY must be true or false")
    assert (check.status, check.data["valid"]) == ("fail", False)


def _keys(
    monkeypatch: pytest.MonkeyPatch,
    provider: str = "openrouter",
    local_only: bool = False,
    triggers: list[assets.Trigger] | None = None,
    deployed: bool = True,
    **env: str,
) -> doctor.Check:
    monkeypatch.setenv("PROMPT_PROVIDER", provider)
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true" if local_only else "false")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    layers = ConfigLayers.resolve(strict=False)
    shipped = assets.triggers() if triggers is None else triggers
    return doctor._keys_check(layers, layers.settings(), shipped, deployed)


def test_keys_name_the_triggers_that_need_them(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _keys(monkeypatch)
    assert check.status == "fail"
    assert check.message == (
        "-i-, -iok-, -ip-, -if- use OpenRouter, but OPENROUTER_API_KEY is not set"
    )
    assert check.data["triggers"] == {"OPENROUTER_API_KEY": ["-i-", "-iok-", "-ip-", "-if-"]}
    assert "PROMPT_PROVIDER" not in check.message
    # Before a deploy: what one would write, as a warning.
    check = _keys(monkeypatch, deployed=False)
    assert check.status == "warn"
    assert "will use OpenRouter once deployed" in check.message
    # PROMPT_LOCAL_ONLY refuses OpenRouter, so its key is never needed.
    assert _keys(monkeypatch, local_only=True).status == "ok"


def test_keys_only_count_active_rewrite_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    local = [
        assets.Trigger("-il-", "prompts-llm.yml", True, "improve", "ollama"),
        assets.Trigger("-ic-", "prompts-llm.yml", False, "improve", "anthropic"),
        assets.Trigger("-p-", "prompts-template.yml", True, "persona"),
    ]
    assert _keys(monkeypatch, "ollama", triggers=local).status == "ok"
    # A trigger naming no provider runs on PROMPT_PROVIDER.
    bare = [assets.Trigger("-x-", "prompts-llm.yml", True, "improve")]
    check = _keys(monkeypatch, "anthropic", triggers=bare)
    assert check.message == "-x- use Anthropic, but ANTHROPIC_API_KEY is not set"


def test_keys_mention_prompt_provider_only_for_a_bare_improve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "-".join(("test", "key"))
    check = _keys(monkeypatch, "anthropic", OPENROUTER_API_KEY=key)
    assert check.status == "warn"
    assert check.message == (
        "PROMPT_PROVIDER (a bare `promptmend improve`, the Try tab) is anthropic, "
        "but ANTHROPIC_API_KEY is not set"
    )
    check = _keys(monkeypatch, OPENROUTER_API_KEY=key)
    assert check.status == "ok"
    assert check.data["keys"]["OPENROUTER_API_KEY"] == {"set": True, "source": "env"}
    assert key not in check.message


def _deploy(espanso: Path, name: str, text: str) -> None:
    (espanso / "match").mkdir(parents=True, exist_ok=True)
    (espanso / "match" / name).write_text(text, "utf-8")


def test_deployed_triggers_read_the_files_espanso_has(tmp_path: Path) -> None:
    shipped, deployed = doctor.deployed_triggers(tmp_path)
    assert (shipped, deployed) == (assets.triggers(), False)
    text = deploy.render(assets.read_match("prompts-llm.yml"), LAUNCHER)
    # A user who commented out -ip- in the deployed file.
    _deploy(tmp_path, "prompts-llm.yml", text.replace('- trigger: "-ip-"', '# - trigger: "-ip-"'))
    found, deployed = doctor.deployed_triggers(tmp_path)
    assert deployed
    active = [t.trigger for t in found if t.active and t.command == "improve"]
    assert active == ["-i-", "-iok-", "-if-", "-il-", "-ilm-"]
    assert {t.provider for t in found if t.trigger == "-il-"} == {"ollama"}


# --- local servers (#220) -----------------------------------------------------------------


def _servers(monkeypatch: pytest.MonkeyPatch, answering: set[str]) -> doctor.Check:
    from promptmend import local_probe

    asked: list[str] = []

    def get(url: str) -> None:
        asked.append(url)
        if not any(port in url for port in answering):
            raise OSError("refused")

    monkeypatch.setattr(local_probe, "_get", get)
    settings = ConfigLayers.resolve(strict=False).settings()
    check = doctor._local_servers_check(settings, assets.triggers(), True)
    assert asked == ["http://localhost:11434/api/tags", "http://localhost:1234/v1/models"]
    return check


def test_local_servers_say_what_a_local_trigger_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _servers(monkeypatch, set())
    assert check.status == "info"
    assert check.message == (
        "-il- needs Ollama: no answer at OLLAMA_BASE_URL; "
        "-ilm- needs LM Studio: no answer at LMSTUDIO_BASE_URL"
    )
    assert check.data["servers"]["ollama"] == {"answers": False, "triggers": ["-il-"]}
    check = _servers(monkeypatch, {"11434", "1234"})
    assert check.message == "Ollama answers (-il-); LM Studio answers (-ilm-)"


def test_local_servers_never_ask_another_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend import local_probe

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com")
    monkeypatch.setattr(local_probe, "_get", lambda url: pytest.fail(f"asked {url}"))
    settings = ConfigLayers.resolve(strict=False).settings()
    only = [t for t in assets.triggers() if t.trigger == "-il-"]
    check = doctor._local_servers_check(settings, only, True)
    assert check.message == "Ollama (-il-) is not on this machine; not asked"
    assert check.data["servers"]["ollama"]["answers"] is None
    none = doctor._local_servers_check(settings, [], False)
    assert none.message == "no trigger uses Ollama or LM Studio"


def test_cli_path_names_the_windows_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "promptmend.exe"
    exe.write_text("", "utf-8")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / f"promptmend{EXE}")])
    monkeypatch.setattr(sys, "platform", "win32")
    assert doctor._cli_check().data["path"] == str(exe)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert doctor._cli_check().data["path"] == str(tmp_path / f"promptmend{EXE}")


# --- Espanso, match files, launcher -------------------------------------------------------


def test_espanso_not_found_and_not_running(tmp_path: Path) -> None:
    check, target = doctor._espanso_check(_runner(), None)
    assert (check.status, check.data["found"], check.data["running"]) == ("warn", False, None)
    assert target == deploy.default_espanso_dir()
    check, _ = doctor._espanso_check(_runner({"espanso path config": str(tmp_path)}), None)
    assert (check.status, check.data["running"]) == ("warn", False)
    answers = {"espanso path config": str(tmp_path), "espanso status": "espanso is not running"}
    assert doctor._espanso_check(_runner(answers), None)[0].data["running"] is False


@pytest.fixture
def espanso(tmp_path: Path) -> Path:
    root = tmp_path / "espanso"
    (root / "match").mkdir(parents=True)
    return root


def test_match_files_in_sync_edited_and_unknown(espanso: Path) -> None:
    manifest = deploy.Manifest.load()
    check = doctor._match_check(espanso, LAUNCHER, manifest)
    assert check.status == "fail"  # all missing
    deploy.apply(deploy.plan(espanso, LAUNCHER, manifest))
    assert doctor._match_check(espanso, LAUNCHER, deploy.Manifest.load()).status == "ok"
    target = espanso / "match" / "prompts-core.yml"
    target.write_text(target.read_text("utf-8") + "# mine\n", "utf-8")
    check = doctor._match_check(espanso, LAUNCHER, deploy.Manifest.load())
    assert check.status == "warn"
    assert {"name": "prompts-core.yml", "state": "modified"} in check.data["files"]
    assert "no launcher" in doctor._match_check(espanso, None, deploy.Manifest.load()).message
    assert "manifest" in doctor._match_check(espanso, LAUNCHER, None).message


def test_launcher_checks(tmp_path: Path, espanso: Path) -> None:
    assert doctor._launcher_check(LAUNCHER, None).status == "warn"
    assert doctor._launcher_check(LAUNCHER, deploy.Manifest.load()).status == "info"
    exe = tmp_path / f"promptmend{EXE}"
    exe.write_text("", "utf-8")
    deploy.apply(deploy.plan(espanso, str(exe), deploy.Manifest.load()))
    check = doctor._launcher_check(str(exe), deploy.Manifest.load())
    assert (check.status, check.data["drift"]) == ("ok", False)


# #169: match files still calling the deprecated `prompt-workflow` alias get a redeploy hint.
def test_launcher_check_warns_about_the_old_command(tmp_path: Path, espanso: Path) -> None:
    old = tmp_path / "prompt-workflow"
    old.write_text("", "utf-8")
    deploy.apply(deploy.plan(espanso, str(old), deploy.Manifest.load()))
    check = doctor._launcher_check(str(tmp_path / f"promptmend{EXE}"), deploy.Manifest.load())
    assert check.status == "warn"
    assert "redeploy with `promptmend espanso deploy`" in check.message
    assert set(check.data) == set(doctor.DATA_KEYS["launcher"])
    assert check.data["drift"] is True


def test_launcher_check_warns_about_another_launcher(tmp_path: Path, espanso: Path) -> None:
    other = tmp_path / "other" / f"promptmend{EXE}"
    other.parent.mkdir()
    other.write_text("", "utf-8")
    deploy.apply(deploy.plan(espanso, str(other), deploy.Manifest.load()))
    check = doctor._launcher_check(str(tmp_path / f"promptmend{EXE}"), deploy.Manifest.load())
    assert (check.status, check.data["drift"]) == ("warn", True)
    assert "redeploy" not in check.message


# #212: the match files hold a Windows launcher with `/`; the same path is no drift.
def test_same_launcher_ignores_slashes_and_case_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ntpath
    import types

    monkeypatch.setattr(doctor, "os", types.SimpleNamespace(path=ntpath))
    deployed = "C:/Users/me/.local/bin/promptmend.exe"
    assert doctor._same_launcher(deployed, r"C:\Users\me\.local\bin\promptmend.exe")
    assert doctor._same_launcher(deployed, r"c:\users\me\.local\bin\PROMPTMEND.EXE")
    assert not doctor._same_launcher(deployed, r"C:\Users\me\other\promptmend.exe")
    assert not doctor._same_launcher(deployed, None)


def test_same_launcher_keeps_case_on_posix() -> None:
    assert doctor._same_launcher(LAUNCHER, LAUNCHER)
    assert doctor._same_launcher("/Users/me/.local/bin/../bin/promptmend", LAUNCHER)
    if os.name != "nt":
        assert not doctor._same_launcher(LAUNCHER.upper(), LAUNCHER)


def test_launcher_check_skips_entries_whose_file_is_gone(tmp_path: Path, espanso: Path) -> None:
    """A deleted folder (or another Espanso config folder) leaves manifest entries behind:
    their launcher calls nothing, so a gone one is no FAIL; a live file's gone launcher is."""
    exe = tmp_path / f"promptmend{EXE}"
    exe.write_text("", "utf-8")
    old = tmp_path / "old-espanso"
    (old / "match").mkdir(parents=True)
    deploy.apply(
        deploy.plan(old, str(tmp_path / "gone" / f"promptmend{EXE}"), deploy.Manifest.load())
    )
    deploy.apply(deploy.plan(espanso, str(exe), deploy.Manifest.load()))
    for path in (old / "match").iterdir():
        path.unlink()
    (old / "match").rmdir()
    check = doctor._launcher_check(str(exe), deploy.Manifest.load())
    assert check.status == "ok"
    assert check.data["missing"] == []
    assert check.data["deployed"] == [str(exe)]
    assert check.data["orphans"] == sorted(str(old / "match" / n) for n in assets.match_names())
    assert "no longer exist" in check.message
    exe.unlink()  # a live file's launcher gone still fails
    check = doctor._launcher_check(str(exe), deploy.Manifest.load())
    assert (check.status, check.data["missing"]) == ("fail", [str(exe)])
    for path in (espanso / "match").iterdir():
        path.unlink()
    assert doctor._launcher_check(None, deploy.Manifest.load()).status == "info"


def test_run_compares_with_the_deployed_launcher(espanso: Path, no_clipboard: None) -> None:
    """With no launcher found or given, the files are compared with what was deployed."""
    deploy.apply(deploy.plan(espanso, LAUNCHER, deploy.Manifest.load()))
    report = doctor.run(espanso_dir=espanso, runner=_runner())
    files = _status("match_files", report).data["files"]
    assert {f["state"] for f in files} == {deploy.IN_SYNC}
    assert _status("launcher", report).data["missing"] == [LAUNCHER]


def test_run_compares_with_a_live_entry_not_an_orphan(
    tmp_path: Path, espanso: Path, no_clipboard: None
) -> None:
    """With no launcher found, a gone file's launcher (here sorting first) is not the one the
    live files are compared with."""
    old = tmp_path / "old-espanso"
    (old / "match").mkdir(parents=True)
    deploy.apply(deploy.plan(espanso, LAUNCHER, deploy.Manifest.load()))
    deploy.apply(deploy.plan(old, "/A/gone/promptmend", deploy.Manifest.load()))
    for path in (old / "match").iterdir():
        path.unlink()
    report = doctor.run(espanso_dir=espanso, runner=_runner())
    files = _status("match_files", report).data["files"]
    assert {f["state"] for f in files} == {deploy.IN_SYNC}


def test_run_with_a_damaged_manifest(espanso: Path, no_clipboard: None) -> None:
    path = config.user_data_dir() / deploy.MANIFEST_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{not json", "utf-8")
    report = doctor.run(espanso_dir=espanso, launcher=LAUNCHER, runner=_runner())
    assert _status("launcher", report).status == "warn"
    assert "manifest" in _status("match_files", report).message


def test_run_survives_a_failing_espanso_probe(espanso: Path, no_clipboard: None) -> None:
    def runner(argv: Sequence[str]) -> None:
        if argv[0] == "espanso":
            raise RuntimeError("probe broke")

    report = doctor.run(espanso_dir=espanso, launcher=LAUNCHER, runner=runner)
    assert _status("espanso", report).status == "fail"
    assert [c.id for c in report.checks] == list(doctor.CHECK_IDS)


def test_every_data_key_is_in_the_json_schema(espanso: Path, no_clipboard: None) -> None:
    """to_json() keeps only DATA_KEYS, so a key a check sets but the schema lacks is
    silently dropped (as launcher's orphans was)."""
    deploy.apply(deploy.plan(espanso, LAUNCHER, deploy.Manifest.load()))
    report = doctor.run(espanso_dir=espanso, launcher=LAUNCHER, runner=_runner())
    for check in report.checks:
        assert set(check.data) <= set(doctor.DATA_KEYS[check.id]), check.id


# --- history and SQLite -------------------------------------------------------------------


def _health(**changes: Any) -> Health:
    values: dict[str, Any] = {
        "path": "/x/history.sqlite3",
        "exists": True,
        "schema_version": 1,
        "operations": 3,
        "attempts": 3,
        "lost_writes": 0,
        "last_lost_utc": None,
        "tracking_incomplete": False,
        "writable": True,
        "sqlite_version": "3.51.3",
        **changes,
    }
    return Health(**values)


@pytest.mark.parametrize(
    ("history_on", "health", "status", "words"),
    [
        (True, {}, "ok", "3 call(s)"),
        (True, {"exists": False, "operations": None}, "ok", "no calls yet (/x/history.sqlite3"),
        (False, {}, "info", "off"),
        (True, {"lost_writes": 2, "tracking_incomplete": True}, "warn", "2 write(s) lost"),
        (True, {"writable": False, "tracking_incomplete": True}, "warn", "not writable"),
        (True, {"error": "corrupt"}, "warn", "corrupt"),
        (True, {"lost_writes": 1, "tracking_incomplete": True}, "warn", "endpoint scanning"),
        (True, {"spooled": 2}, "warn", "2 record(s) waiting in history.spool"),
        (True, {"error": "locked", "spooled": 1}, "warn", "locked; 1 record(s) waiting"),
        (True, {"tracking_incomplete": True}, "warn", "cannot be read"),
    ],
)
def test_history_check(
    monkeypatch: pytest.MonkeyPatch,
    history_on: bool,
    health: dict[str, Any],
    status: str,
    words: str,
) -> None:
    from promptmend import history

    monkeypatch.setattr(history.HistoryStore, "health", lambda self: _health(**health))
    check, sqlite = doctor._history_checks(Settings(history=history_on))
    assert check.status == status
    assert words in check.message
    assert sqlite.status == "ok"


@pytest.mark.parametrize(
    ("version", "journal", "status", "bug", "words"),
    [
        ("3.45.1", "wal", "warn", True, "still in WAL"),
        ("3.45.1", "delete", "ok", True, "rollback journal"),
        ("3.45.1", None, "ok", True, "rollback journal"),
        ("3.51.3", "wal", "ok", False, "SQLite 3.51.3"),
        ("unknown", None, "info", False, "version unknown"),
    ],
)
def test_sqlite_check(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    journal: str | None,
    status: str,
    bug: bool,
    words: str,
) -> None:
    from promptmend import history

    monkeypatch.setattr(
        history.HistoryStore,
        "health",
        lambda self: _health(sqlite_version=version, journal_mode=journal),
    )
    sqlite = doctor._history_checks(Settings())[1]
    assert (sqlite.status, sqlite.data["wal_reset_bug"]) == (status, bug)
    assert sqlite.data["journal_mode"] == journal
    assert words in sqlite.message


# --- clipboard and profiles ---------------------------------------------------------------


def test_clipboard_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyperclip

    from promptmend import clipboard_guard

    def broken() -> None:
        raise pyperclip.PyperclipException("no copy/paste mechanism")

    monkeypatch.setattr(clipboard_guard, "is_concealed", lambda: None)
    monkeypatch.setattr(pyperclip, "paste", broken)
    check = doctor._clipboard_check(True)
    assert (check.status, check.data["error"]) == ("warn", "PyperclipException")


# --- persona (#29 B) -----------------------------------------------------------------------


def test_persona_check_without_a_persona() -> None:
    check = doctor._persona_check(Settings())
    assert (check.status, check.message) == ("ok", "PROMPT_PERSONA is not set")
    assert check.data == {"set": False, "local_only": False, "findings": []}


def test_persona_check_clean_persona() -> None:
    check = doctor._persona_check(Settings(persona="I am a data engineer at Example Corp."))
    assert (check.status, check.data["findings"]) == ("ok", [])
    # bare_token is the gate's rule for a one-word draft, not for a persona.
    assert doctor._persona_check(Settings(persona="DataOps2Lead")).status == "ok"


def test_persona_check_warns_with_labels_only() -> None:
    address = "jane.doe" + "@" + "example.com"
    persona = f"I am a falcon analyst, mail {address}."
    check = doctor._persona_check(Settings(persona=persona, extra_patterns="falcon"))
    assert check.status == "warn"
    assert check.message == (
        "PROMPT_PERSONA matches the data-protection patterns: email, custom_1; it is sent "
        "unscanned with every cloud call"
    )
    assert check.data == {"set": True, "local_only": False, "findings": ["email", "custom_1"]}
    shown = str(check.message) + str(check.data)
    assert "falcon" not in shown
    assert address not in shown
    local = doctor._persona_check(
        Settings(persona=persona, extra_patterns="falcon", local_only=True)
    )
    assert (local.status, local.message) == (
        "info",
        "matches email, custom_1; not sent (PROMPT_LOCAL_ONLY=true)",
    )
    # A loopback server that may relay to a cloud API (PROMPT_GATE_LOCAL) can send it on.
    relay = Settings(persona=persona, local_only=True, gate_local=True)
    assert (doctor._persona_check(relay).status, doctor._persona_check(relay).message) == (
        "warn",
        "PROMPT_PERSONA matches the data-protection patterns: email; it is sent unscanned "
        "with every call through a local relay (PROMPT_GATE_LOCAL=true)",
    )


# Flagged only when a configured profile sends it: `general` has no {{PERSONA_RULE}}.
def test_persona_check_only_when_a_profile_sends_it() -> None:
    address = "jane.doe" + "@" + "example.com"
    unused = doctor._persona_check(Settings(persona=f"Mail {address}", profile="general"))
    assert (unused.status, unused.message) == (
        "info",
        "matches email; not used by the configured profiles",
    )
    pro = Settings(persona=f"Mail {address}", profile="general", pro_profile="default")
    assert doctor._persona_check(pro).status == "warn"
    folder = config._user_config_dir() / "profiles"
    folder.mkdir(parents=True)
    (folder / "mine.md").write_text("Rewrite it. {{PERSONA_RULE}}", "utf-8")
    (folder / "plain.md").write_text("Rewrite it.", "utf-8")
    assert doctor._persona_check(Settings(persona=address, profile="mine")).status == "warn"
    assert doctor._persona_check(Settings(persona=address, profile="plain")).status == "info"
    # A profile that cannot load sends nothing; the profiles check reports it.
    assert doctor._persona_check(Settings(persona=address, profile="nosuch")).status == "info"


def test_persona_check_when_the_persona_cannot_be_read(
    tmp_path: Path, espanso: Path, no_clipboard: None
) -> None:
    # A merged line: the value holds another setting's NAME=, so it is rejected.
    (tmp_path / ".env").write_text("PROMPT_PERSONA=I am Jane OLLAMA_MODEL=qwen3:8b\n", "utf-8")
    report = doctor.run(espanso_dir=espanso, launcher=LAUNCHER, runner=_runner())
    check = _status("persona", report)
    assert (check.status, check.message) == ("info", "PROMPT_PERSONA could not be read; see config")
    assert "Jane" not in str(report.to_json())


def test_persona_check_in_the_report(
    monkeypatch: pytest.MonkeyPatch, espanso: Path, no_clipboard: None
) -> None:
    monkeypatch.setenv("PROMPT_PERSONA", "Reach me at " + "jane" + "@" + "example.com")
    report = doctor.run(espanso_dir=espanso, launcher=LAUNCHER, runner=_runner())
    check = _status("persona", report)
    assert check.status == "warn"
    assert report.to_json()["checks"]["persona"]["data"]["findings"] == ["email"]


def test_profiles_check(monkeypatch: pytest.MonkeyPatch) -> None:
    assert doctor._profiles_check(Settings(profile="nosuch")).status == "fail"
    folder = config._user_config_dir() / "profiles"
    folder.mkdir(parents=True)
    (folder / "general.md").write_text("x", "utf-8")
    check = doctor._profiles_check(Settings())
    hint = (
        "Your general.md replaces the built-in only once PROMPT_PROFILE_OVERRIDES lists it: "
        "`promptmend config set PROMPT_PROFILE_OVERRIDES general`."
    )
    assert (check.status, check.message) == ("warn", f"general: shadowed; {hint}")
    assert {"name": "general", "status": "shadowed"} in check.data["user"]


def test_a_check_that_raises_is_reported(
    no_clipboard: None, espanso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "_cli_check", boom)
    report = doctor.run(espanso_dir=espanso, launcher=LAUNCHER, runner=_runner())
    check = _status("cli", report)
    assert (check.status, check.message) == ("fail", "check failed: RuntimeError: boom")
    assert report.status == "fail"


def test_overrides_hint_keeps_the_names_already_listed() -> None:
    from promptmend.profiles import overrides_hint

    assert overrides_hint(["mine", "default-pro"]) is None  # not a built-in
    assert overrides_hint(["general"], ("general",)) is None
    hint = overrides_hint(["mine", "general"], ("default",))
    assert hint is not None
    assert hint.startswith("Your general.md replaces")
    assert hint.endswith("`promptmend config set PROMPT_PROFILE_OVERRIDES default,general`.")
