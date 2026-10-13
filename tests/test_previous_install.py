"""Detecting an earlier checkout install (#110, previous_install.py), offline: the runner is
faked, and every checkout, receipt and match folder is built under tmp_path."""

from __future__ import annotations

import builtins
import io
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from promptmend import (
    assets,
    config,
    config_files,
    config_store,
    deploy,
    doctor,
    previous_install,
    profiles,
)
from promptmend.cli import app
from promptmend.config import Settings
from promptmend.config_store import MigrationError
from promptmend.previous_install import ENTERED, LAUNCHER, MANIFEST, RECEIPT

WINDOWS = os.name == "nt"
UV_BIN = "/Users/me/.local/bin/promptmend"
LEGACY_PROJECT = "espanso-prompt-rewriter"  # the project name before the rename (#169)


class FakeRunner:
    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str | None:
        self.calls.append(list(argv))
        return self.answers.get(" ".join(argv))


def checkout(
    tmp_path: Path,
    name: str = previous_install.PROJECT_NAME,
    *,
    env: bool = True,
    legacy: bool = False,
) -> Path:
    """A checkout as an editable install left it: pyproject.toml, a .env with one setting and
    one key (built at runtime, so no scanner takes it for a credential), an edited profile.
    ``legacy``: one from before the rename (#169), with the old project and package names."""
    root = tmp_path / "Projects" / "epr"
    prompts = root / (profiles.LEGACY_PROMPTS_PATH if legacy else profiles.PROMPTS_PATH)
    prompts.mkdir(parents=True)
    if legacy and name == previous_install.PROJECT_NAME:
        name = LEGACY_PROJECT
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', "utf-8")
    if env:
        key = "sk-or-v1-" + "0" * 8
        (root / ".env").write_text(f"PROMPT_TIMEOUT=45\nOPENROUTER_API_KEY={key}\n", "utf-8")
    (prompts / "mine.md").write_text("Mine.\n", "utf-8")
    return root


def venv_launcher(root: Path, command: str = "promptmend") -> Path:
    if WINDOWS:
        return root / ".venv" / "Scripts" / f"{command}.exe"
    return root / ".venv" / "bin" / command


def deploy_old(espanso: Path, launcher: str) -> None:
    """The match files as the old install script rendered them for ``launcher``."""
    (espanso / "match").mkdir(parents=True, exist_ok=True)
    # install_windows.ps1 turned backslashes into slashes; install_macos.sh kept the path.
    path = launcher.replace("\\", "/") if WINDOWS else launcher
    for name in assets.match_names():
        text = assets.read_match(name).replace(deploy.PLACEHOLDER, path)
        (espanso / "match" / name).write_bytes(text.encode("utf-8"))


def receipt(tools: Path, project: str = previous_install.PROJECT_NAME, **source: Any) -> None:
    folder = tools / project
    folder.mkdir(parents=True, exist_ok=True)
    ((key, value),) = source.items()
    item = f'{{ name = "{project}", {key} = "{value}" }}'
    text = f"[tool]\nrequirements = [{item}]\n"
    (folder / "uv-receipt.toml").write_text(text.replace("\\", "\\\\"), "utf-8")


@pytest.fixture
def espanso(tmp_path: Path) -> Path:
    root = tmp_path / "espanso"
    (root / "match").mkdir(parents=True)
    return root


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> MutableMapping[str, str]:
    """The process environment the tests run under (conftest's temp dirs), without a legacy
    PROMPTMEND_ENV and with a PATH that holds no promptmend."""
    monkeypatch.delenv("PROMPTMEND_ENV", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    return os.environ


def _roots(found: previous_install.Detection) -> dict[Path, frozenset[str]]:
    return {c.root: c.signals for c in found.candidates}


# --- Root from a launcher -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("launcher", "root"),
    [
        ("/Users/me/epr/.venv/bin/promptmend", PurePosixPath("/Users/me/epr")),
        ("/Users/me/epr/.venv/bin/prompt-workflow", PurePosixPath("/Users/me/epr")),  # #169
        ("C:/Users/me/epr/.venv/Scripts/prompt-workflow.exe", PureWindowsPath("C:/Users/me/epr")),
        ("C:/Users/me/epr/.venv/Scripts/promptmend.exe", PureWindowsPath("C:/Users/me/epr")),
        (
            "C:\\Users\\me\\epr\\.venv\\Scripts\\promptmend.exe",
            PureWindowsPath("C:/Users/me/epr"),
        ),
        ("C:/Users/me/epr/.VENV/scripts/Prompt-Workflow.EXE", PureWindowsPath("C:/Users/me/epr")),
        (UV_BIN, None),  # a uv tool bin: the same path for the editable install and the wheel
        ("/opt/homebrew/bin/promptmend", None),
        ("C:/Users/me/scoop/shims/promptmend.exe", None),
        ("/Users/me/epr/.venv/lib/promptmend", None),
        (".venv/bin/promptmend", None),  # relative: never resolved against the cwd
    ],
)
def test_checkout_root_of(launcher: str, root: PurePath | None) -> None:
    assert previous_install.checkout_root_of(launcher) == root


# --- Signals ------------------------------------------------------------------------------


def test_detects_the_launcher_in_old_match_files(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    launcher = str(venv_launcher(root))
    deploy_old(espanso, launcher)
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso)
    (candidate,) = found.candidates
    assert candidate.root == root.resolve()
    assert candidate.signals == {LAUNCHER}
    assert candidate.env_file == root.resolve() / ".env"
    assert candidate.profiles_dir == root.resolve() / "src" / "promptmend" / "prompts"
    assert candidate.launchers_in_root == (launcher.replace("\\", "/") if WINDOWS else launcher,)
    assert found.gated is None


# #169: the realistic previous install is a checkout from before the rename: the old project
# name, the old package folder and a `prompt-workflow` launcher in the match files.
def test_detects_a_checkout_from_before_the_rename(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path, legacy=True)
    launcher = str(venv_launcher(root, "prompt-workflow"))
    deploy_old(espanso, launcher)
    tools = tmp_path / "uv" / "tools"
    receipt(tools, LEGACY_PROJECT, editable=str(root))
    runner = FakeRunner({"uv tool dir": f"{tools}\n"})
    found = previous_install.detect(runner=runner, espanso_dir=espanso)
    (candidate,) = found.candidates
    assert candidate.root == root.resolve()
    assert candidate.signals == {LAUNCHER, RECEIPT}
    assert candidate.profiles_dir == root.resolve() / profiles.LEGACY_PROMPTS_PATH
    assert candidate.launchers_in_root == (launcher.replace("\\", "/") if WINDOWS else launcher,)


def test_detects_a_manifest_entry(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    launcher = str(venv_launcher(root)).replace("\\", "/")
    deploy.apply(deploy.plan(espanso, launcher, deploy.Manifest.load()))
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso)
    assert _roots(found) == {root.resolve(): {LAUNCHER, MANIFEST}}


def test_a_manifest_entry_whose_file_is_gone_is_no_signal(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    launcher = str(venv_launcher(root)).replace("\\", "/")
    deploy.apply(deploy.plan(espanso, launcher, deploy.Manifest.load()))
    for name in assets.match_names():
        (espanso / "match" / name).unlink()
    assert previous_install.detect(runner=FakeRunner(), espanso_dir=espanso).candidates == ()


@pytest.mark.parametrize("kind", ["editable", "directory"])
def test_detects_the_uv_receipt(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], kind: str
) -> None:
    root = checkout(tmp_path)
    tools = tmp_path / "uv" / "tools"
    receipt(tools, **{kind: str(root)})
    runner = FakeRunner({"uv tool dir": f"{tools}\n"})
    found = previous_install.detect(runner=runner, espanso_dir=espanso)
    assert _roots(found) == {root.resolve(): {RECEIPT}}


def test_a_wheel_receipt_gives_nothing(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    checkout(tmp_path)
    tools = tmp_path / "uv" / "tools"
    receipt(tools, url="https://example.invalid/promptmend-0.16.1-py3-none-any.whl")
    runner = FakeRunner({"uv tool dir": str(tools)})
    assert previous_install.detect(runner=runner, espanso_dir=espanso).candidates == ()


@pytest.mark.parametrize("text", ["not toml [", "[tool]\nrequirements = 3\n", ""])
def test_a_damaged_receipt_gives_nothing(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], text: str
) -> None:
    tools = tmp_path / "uv" / "tools"
    (tools / previous_install.PROJECT_NAME).mkdir(parents=True)
    (tools / previous_install.PROJECT_NAME / "uv-receipt.toml").write_text(text, "utf-8")
    assert previous_install.receipt_root(FakeRunner({"uv tool dir": str(tools)})) is None


def test_detects_an_entered_path(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso, entered=root)
    assert _roots(found) == {root.resolve(): {ENTERED}}


def test_signals_for_one_root_merge(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    tools = tmp_path / "uv" / "tools"
    receipt(tools, editable=str(root))
    runner = FakeRunner({"uv tool dir": str(tools)})
    found = previous_install.detect(runner=runner, espanso_dir=espanso, entered=root)
    assert _roots(found) == {root.resolve(): {LAUNCHER, RECEIPT, ENTERED}}


def test_a_uv_bin_launcher_alone_gives_nothing(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    checkout(tmp_path)
    deploy_old(espanso, UV_BIN)
    assert previous_install.detect(runner=FakeRunner(), espanso_dir=espanso).candidates == ()


@pytest.mark.parametrize("name", ["something-else", None])
def test_a_folder_that_is_not_this_project_gives_nothing(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], name: str | None
) -> None:
    root = checkout(tmp_path, name or "x")
    if name is None:
        (root / "pyproject.toml").unlink()
    deploy_old(espanso, str(venv_launcher(root)))
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso, entered=root)
    assert found.candidates == ()


def test_the_running_checkout_gives_nothing(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checkout(tmp_path)
    monkeypatch.setattr(config, "_PROJECT_ROOT", root)
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso, entered=root)
    assert found.candidates == ()


def test_detection_never_reads_the_env_file(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checkout(tmp_path)
    opened = []
    real_open = io.open

    def guarded(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(Path(os.fspath(file)).name if not isinstance(file, int) else "")
        return real_open(file, *args, **kwargs)

    # Path.read_text/read_bytes and open() all go through io.open.
    monkeypatch.setattr(io, "open", guarded)
    monkeypatch.setattr(builtins, "open", guarded)
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso, entered=root)
    assert found.candidates[0].env_file is not None
    assert opened, "the guard saw no file at all"
    assert ".env" not in opened


@pytest.mark.skipif(WINDOWS, reason="POSIX permissions")
def test_an_unreadable_env_file_is_still_reported(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    (root / ".env").chmod(0)
    try:
        found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso, entered=root)
    finally:
        (root / ".env").chmod(0o600)
    assert found.candidates[0].env_file == root.resolve() / ".env"


@pytest.mark.skipif(WINDOWS, reason="POSIX permissions")
def test_an_unreadable_checkout_folder_never_fails_doctor(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], no_clipboard: None
) -> None:
    root = checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    (root / "src").chmod(0)
    try:
        report = doctor.run(espanso_dir=espanso, launcher=UV_BIN, runner=FakeRunner())
    finally:
        (root / "src").chmod(0o700)
    check = _check(report)
    assert check.status == "info"
    assert check.data["roots"] == [str(root.resolve())]


def test_a_check_that_raises_is_a_warning_not_a_failure(
    espanso: Path,
    env: MutableMapping[str, str],
    no_clipboard: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(**_: Any) -> None:
        raise PermissionError("no")

    monkeypatch.setattr(previous_install, "detect", broken)
    report = doctor.run(espanso_dir=espanso, launcher=UV_BIN, runner=FakeRunner())
    assert _check(report).status == "warn"


def test_without_espanso_dir_it_asks_espanso(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    runner = FakeRunner({"espanso path config": str(espanso)})
    assert _roots(previous_install.detect(runner=runner)) == {root.resolve(): {LAUNCHER}}
    assert ["espanso", "path", "config"] in runner.calls


# --- Gate (D-MIG-4) -----------------------------------------------------------------------


def test_legacy_mode_detects_nothing(
    tmp_path: Path, espanso: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checkout(tmp_path)
    monkeypatch.setenv("PROMPTMEND_ENV", str(tmp_path / "legacy.env"))
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso, entered=root)
    assert (found.candidates, found.gated) == ((), previous_install.LEGACY)


def test_a_saved_config_detects_nothing(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    path = config.settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("config_version = 1\n", "utf-8")
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso, entered=root)
    assert (found.candidates, found.gated) == ((), previous_install.SAVED)


def test_a_secret_store_detects_nothing(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    store = config.settings_file().with_name("secrets.toml")
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("", "utf-8")
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso, entered=root)
    assert (found.candidates, found.gated) == ((), previous_install.SECRETS)


def test_a_config_toml_folder_is_not_a_saved_config(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    config.settings_file().mkdir(parents=True)
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso, entered=root)
    assert found.gated is None
    assert found.candidates


def test_a_manifest_alone_does_not_close_the_gate(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    """The editable install deployed (with a manifest) before the switch, so a manifest
    pointing into the checkout must still be found."""
    root = checkout(tmp_path)
    launcher = str(venv_launcher(root)).replace("\\", "/")
    deploy.apply(deploy.plan(espanso, launcher, deploy.Manifest.load()))
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso)
    assert found.gated is None
    assert _roots(found)


# --- Skip marker (D-MIG-3) ----------------------------------------------------------------


def test_a_skipped_root_is_offered_only_when_entered(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    previous_install.skip(root.resolve())
    assert previous_install.detect(runner=FakeRunner(), espanso_dir=espanso).candidates == ()
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso, entered=root)
    assert _roots(found) == {root.resolve(): {LAUNCHER, ENTERED}}


@pytest.mark.skipif(WINDOWS, reason="symlinks need privileges on Windows")
def test_a_root_skipped_through_a_link_is_skipped(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = checkout(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    deploy_old(espanso, str(venv_launcher(root)))
    previous_install.skip(link)
    assert previous_install.detect(runner=FakeRunner(), espanso_dir=espanso).candidates == ()


def test_a_skip_keeps_earlier_times(tmp_path: Path, env: MutableMapping[str, str]) -> None:
    path = previous_install.skip_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    first = str((tmp_path / "a").resolve())
    record = {"version": 1, "skipped": [{"root": first, "at": "2026-01-01T00:00:00Z"}]}
    path.write_text(json.dumps(record), "utf-8")
    previous_install.skip(tmp_path / "b")
    items = json.loads(path.read_text("utf-8"))["skipped"]
    assert {"root": first, "at": "2026-01-01T00:00:00Z"} in items
    assert len(items) == 2


def test_another_checkout_is_offered_after_a_skip(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    previous_install.skip(tmp_path / "elsewhere")
    root = checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    assert previous_install.detect(runner=FakeRunner(), espanso_dir=espanso).candidates


@pytest.mark.parametrize("text", ["{", "[]", '{"skipped": 3}', '{"skipped": [3, {"root": 4}]}'])
def test_a_damaged_skip_marker_counts_as_empty(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], text: str
) -> None:
    path = previous_install.skip_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    assert previous_install.skipped_roots() == set()
    root = checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    assert previous_install.detect(runner=FakeRunner(), espanso_dir=espanso).candidates
    previous_install.skip(root)
    assert previous_install.skipped_roots() == {str(root)}


# --- Shadowed CLI -------------------------------------------------------------------------


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    launcher = tmp_path / "uv-bin" / "promptmend"
    launcher.parent.mkdir()
    launcher.write_text("", "utf-8")
    monkeypatch.setattr(deploy, "resolve_launcher", lambda **_: deploy.Launcher(launcher, "uv"))
    return launcher


def _which(
    monkeypatch: pytest.MonkeyPatch, path: Path | str, alias: Path | str | None = None
) -> None:
    """`promptmend` resolves to ``path``, its alias `prompt-workflow` to ``alias`` (#169)."""
    found = {"promptmend": str(path), "prompt-workflow": None if alias is None else str(alias)}
    monkeypatch.setattr(shutil, "which", lambda cmd, **k: found.get(cmd))


def test_an_active_venv_shadows_the_launcher(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checkout(tmp_path)
    old = venv_launcher(root)
    old.parent.mkdir(parents=True)
    old.write_text("", "utf-8")
    _which(monkeypatch, old)
    environ = {"VIRTUAL_ENV": str(root / ".venv"), "PATH": str(old.parent)}
    found = previous_install.shadow(environ, FakeRunner())
    assert found is not None
    assert (found.path, found.launcher) == (str(old), str(installed))
    assert "deactivate" in found.hint


def test_a_venv_on_path_without_virtual_env(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = venv_launcher(checkout(tmp_path))
    old.parent.mkdir(parents=True)
    old.write_text("", "utf-8")
    _which(monkeypatch, old)
    found = previous_install.shadow({"PATH": str(old.parent)}, FakeRunner())
    assert found is not None
    assert str(old.parent) in found.hint


def test_another_cli_on_path(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "pipx" / "promptmend"
    other.parent.mkdir()
    other.write_text("", "utf-8")
    _which(monkeypatch, other)
    found = previous_install.shadow({"PATH": str(other.parent)}, FakeRunner())
    assert found is not None
    assert "uninstall" in found.hint


def test_no_shadow_when_path_finds_the_launcher(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _which(monkeypatch, installed)
    assert previous_install.shadow({"PATH": str(installed.parent)}, FakeRunner()) is None


def test_the_alias_next_to_the_launcher_is_no_shadow(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = installed.with_name("prompt-workflow")
    alias.write_text("", "utf-8")
    _which(monkeypatch, installed, alias)
    assert previous_install.shadow({"PATH": str(installed.parent)}, FakeRunner()) is None


# #169: an old checkout's `prompt-workflow` first on PATH shadows the alias, even when the
# new `promptmend` is found where it belongs.
def test_an_old_prompt_workflow_on_path_shadows_the_alias(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = checkout(tmp_path) / ".venv" / "bin" / "prompt-workflow"
    old.parent.mkdir(parents=True)
    old.write_text("", "utf-8")
    _which(monkeypatch, installed, old)
    found = previous_install.shadow({"PATH": str(old.parent)}, FakeRunner())
    assert found is not None
    assert (found.command, found.path) == ("prompt-workflow", str(old))
    assert found.launcher == str(installed.with_name("prompt-workflow"))
    assert str(old.parent) in found.hint


def test_no_shadow_when_nothing_is_on_path(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    assert previous_install.shadow({}, FakeRunner()) is None


def test_no_shadow_without_a_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _which(monkeypatch, tmp_path / "promptmend")

    def nothing(**_: Any) -> None:
        raise deploy.DeployError("none")

    monkeypatch.setattr(deploy, "resolve_launcher", nothing)
    assert previous_install.shadow({}, FakeRunner()) is None


def test_a_given_launcher_is_not_looked_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(**_: Any) -> None:
        raise AssertionError("looked up")

    monkeypatch.setattr(deploy, "resolve_launcher", unexpected)
    other = tmp_path / "other" / "promptmend"
    _which(monkeypatch, other)
    given = tmp_path / "given" / "promptmend"
    found = previous_install.shadow({}, FakeRunner(), given)
    assert found is not None
    assert found.launcher == str(given)
    assert previous_install.shadow({}, FakeRunner(), look_up=False) is None


# --- doctor -------------------------------------------------------------------------------


def _check(report: doctor.Report) -> doctor.Check:
    return next(c for c in report.checks if c.id == "previous_install")


@pytest.fixture
def no_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyperclip

    from promptmend import clipboard_guard

    monkeypatch.setattr(clipboard_guard, "is_concealed", lambda: None)
    monkeypatch.setattr(pyperclip, "paste", lambda: "")


def test_doctor_reports_a_previous_install(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], no_clipboard: None
) -> None:
    root = checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    report = doctor.run(espanso_dir=espanso, launcher=UV_BIN, runner=FakeRunner())
    check = _check(report)
    assert check.status == "info"
    assert str(root.resolve()) in check.message
    assert "not migrated" in check.message
    data = report.to_json()["checks"]["previous_install"]["data"]
    assert data["roots"] == [str(root.resolve())]
    assert data["signals"] == [LAUNCHER]
    assert data["env_file"] == str(root.resolve() / ".env")
    assert data["gated"] is None
    assert data["shadow"] is None
    # Only that the .env exists: never a value from it.
    assert "sk-or" not in repr(report.to_json())


def test_doctor_with_nothing_found(
    espanso: Path, env: MutableMapping[str, str], no_clipboard: None
) -> None:
    report = doctor.run(espanso_dir=espanso, launcher=UV_BIN, runner=FakeRunner())
    assert (_check(report).status, _check(report).message) == ("ok", "no previous install found")


def test_doctor_after_migration_looks_for_nothing(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], no_clipboard: None
) -> None:
    root = checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    path = config.settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("config_version = 1\n", "utf-8")
    report = doctor.run(espanso_dir=espanso, launcher=UV_BIN, runner=FakeRunner())
    assert _check(report).status == "ok"
    assert report.to_json()["checks"]["previous_install"]["data"]["gated"] == "saved"


def test_doctor_warns_about_a_shadowed_cli(
    tmp_path: Path,
    espanso: Path,
    env: MutableMapping[str, str],
    installed: Path,
    no_clipboard: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = venv_launcher(checkout(tmp_path))
    old.parent.mkdir(parents=True)
    old.write_text("", "utf-8")
    _which(monkeypatch, old)
    # doctor compares PATH with its --launcher, not a lookup of its own.
    report = doctor.run(espanso_dir=espanso, launcher=str(installed), runner=FakeRunner())
    check = _check(report)
    assert check.status == "warn"
    assert str(old) in check.message
    assert report.to_json()["checks"]["previous_install"]["data"]["shadow"] == str(old)


# --- Copy mode, retire and rollback (#110 PR b) -------------------------------------------

# Built at runtime, so no key-shaped literal lands in the repo (gitleaks).
COPIED_KEY = "sk-or-v1-" + "cd34" * 16
FOREIGN_ENV = (
    "OPENROUTER_MODEL=vendor/old-model\nOLLAMA_MODEL=foreign\nOPENROUTER_MAX_TOKENS=2400\n"
    f"HTTPS_PROXY=http://127.0.0.1:9\nOPENROUTER_API_KEY={COPIED_KEY}\n"
)


def old_checkout(tmp_path: Path, text: str = FOREIGN_ENV) -> Path:
    root = checkout(tmp_path, env=False)
    (root / ".env").write_bytes(text.encode("utf-8"))
    return root


def user_dir() -> Path:
    directory = config._user_config_dir(os.environ)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _copy(
    root: Path, **kwargs: Any
) -> tuple[config_store.MigrationPlan, config_store.MigrationResult]:
    plan = config_store.plan_migration(source=root)
    return plan, config_store.apply_migration(source=root, consent=plan.token, **kwargs)


# Copy mode refuses an earlier .env whose value was cut at an unquoted ` #` (#32).
def test_copy_refuses_a_value_cut_at_a_comment(
    tmp_path: Path, env: MutableMapping[str, str]
) -> None:
    root = old_checkout(tmp_path, "PROMPT_EXTRA_PATTERNS=ticket #\\d{5}\n")
    with pytest.raises(config_store.MigrationError, match="was cut at ' #'"):
        config_store.plan_migration(source=root)


def test_copy_fills_only_settings_at_their_default(
    tmp_path: Path, env: MutableMapping[str, str]
) -> None:
    root = old_checkout(tmp_path)
    original = (root / ".env").read_bytes()
    active = user_dir() / ".env"
    active.write_text("OLLAMA_MODEL=mine\n", "utf-8")

    plan = config_store.plan_migration(source=root)

    assert plan.status == "ready"
    assert plan.copy is not None
    assert plan.copy.path == root.resolve() / ".env"
    assert [s.path for s in plan.sources] == [active]
    assert plan.copied == ("OPENROUTER_MODEL", "OPENROUTER_API_KEY")
    assert plan.kept == ("OLLAMA_MODEL",)
    assert "OPENROUTER_MAX_TOKENS" in plan.defaults
    assert plan.copy_ignored == ("'HTTPS_PROXY'",)
    preview = "\n".join(plan.describe())
    assert "stays in place" in preview
    assert "Already set here, kept as it is: OLLAMA_MODEL" in preview
    assert COPIED_KEY not in preview

    result = config_store.apply_migration(source=root, consent=plan.token)

    settings = Settings.load()
    assert settings.ollama_model == "mine"
    assert settings.openrouter_model == "vendor/old-model"
    assert settings.openrouter_api_key == COPIED_KEY
    # The .env in use moved into the backup as always; the old checkout's stays, unchanged.
    assert not active.exists()
    assert result.moved[active].parent == result.backup
    assert result.copied == root.resolve() / ".env"
    assert (root / ".env").read_bytes() == original
    record = json.loads((user_dir() / "migration.json").read_text("utf-8"))
    assert record["mode"] == "copy"
    copied = [s for s in record["sources"] if s.get("copied")]
    assert copied == [
        {
            "from": str(root.resolve() / ".env"),
            "to": None,
            "in_place": None,
            "sha256": config_files.digest(original),
            "copied": True,
            "root": str(root.resolve()),
        }
    ]


def test_copy_never_overrides_a_stored_key_or_a_real_variable(
    tmp_path: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = old_checkout(tmp_path)
    stored = "sk-or-v1-" + "ef56" * 16
    config_store.save_secret("OPENROUTER_API_KEY", stored)
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/from-the-shell")
    # A secret store alone closes detection, but an explicit copy still runs.
    plan, _ = _copy(root)
    assert plan.kept == ("OPENROUTER_MODEL", "OPENROUTER_API_KEY")
    assert plan.copied == ("OLLAMA_MODEL",)
    assert Settings.load().openrouter_api_key == stored


def test_copy_needs_the_preview_token_and_writes_nothing_without_it(
    tmp_path: Path, env: MutableMapping[str, str]
) -> None:
    root = old_checkout(tmp_path)
    plan = config_store.plan_migration(source=root)
    for consent in ("", "yes", plan.token[::-1]):
        with pytest.raises(MigrationError, match="not confirmed"):
            config_store.apply_migration(source=root, consent=consent)
    # The token covers the old .env's bytes: an edit after the preview needs a new preview.
    (root / ".env").write_text(FOREIGN_ENV + "OLLAMA_MODEL=edited\n", "utf-8")
    with pytest.raises(MigrationError, match="not confirmed"):
        config_store.apply_migration(source=root, consent=plan.token)
    # And the plain migration's token is not a copy's.
    with pytest.raises(MigrationError):
        config_store.apply_migration(consent=plan.token)
    assert sorted(p.name for p in user_dir().iterdir()) == []


@pytest.mark.parametrize(
    ("make", "error"),
    [
        (lambda tmp: checkout(tmp, "something-else"), "is not a checkout"),
        (lambda tmp: checkout(tmp, env=False), "has no .env to copy"),
        (lambda tmp: old_checkout(tmp, "OPENROUTER_MAX_TOKENS=lots\n"), "fix .* first"),
        (
            lambda tmp: old_checkout(tmp, "OPENROUTER_MODEL=aOLLAMA_MODEL=b\n"),
            "runs into the next line",
        ),
    ],
)
def test_copy_refusals(
    tmp_path: Path, env: MutableMapping[str, str], make: Callable[[Path], Path], error: str
) -> None:
    with pytest.raises(MigrationError, match=error):
        config_store.plan_migration(source=make(tmp_path))
    assert not config.settings_file().exists()


def test_copy_refuses_the_running_checkout(
    tmp_path: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = old_checkout(tmp_path)
    monkeypatch.setattr(config, "_PROJECT_ROOT", root)
    with pytest.raises(MigrationError, match="this install's own checkout"):
        config_store.plan_migration(source=root)


def test_copy_in_legacy_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="PROMPTMEND_ENV is set"):
        config_store.plan_migration(source=old_checkout(tmp_path))


def test_a_failed_copy_check_undoes_everything(
    tmp_path: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = old_checkout(tmp_path)
    plan = config_store.plan_migration(source=root)
    real = config_store._effective
    calls = []

    def effective(environ: Mapping[str, str]) -> dict[str, tuple[Any, str]]:
        calls.append(1)
        values = real(environ)
        if len(calls) == 2:  # the reload after the writes
            values["OPENROUTER_MODEL"] = ("vendor/other", "env")
        return values

    monkeypatch.setattr(config_store, "_effective", effective)
    with pytest.raises(MigrationError, match="reloading changed OPENROUTER_MODEL"):
        config_store.apply_migration(source=root, consent=plan.token)
    assert not config.settings_file().exists()
    assert not (user_dir() / "secrets.toml").exists()
    assert not (user_dir() / "migration.json").exists()


def test_plain_migration_keeps_a_value_a_real_variable_shadows(
    tmp_path: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The .env's value is migrated, the variable still wins: nothing the CLI uses changes.
    (user_dir() / ".env").write_text(
        f"OLLAMA_MODEL=from-dotenv\nOPENROUTER_API_KEY={COPIED_KEY}x\n"
    )
    monkeypatch.setenv("OLLAMA_MODEL", "from-shell")
    monkeypatch.setenv("OPENROUTER_API_KEY", COPIED_KEY)
    plan = config_store.plan_migration()
    config_store.apply_migration(consent=plan.token)
    assert Settings.load().ollama_model == "from-shell"
    assert 'OLLAMA_MODEL = "from-dotenv"' in config.settings_file().read_text("utf-8")


def test_copy_keeps_a_shadowed_value_of_the_env_in_use(
    tmp_path: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = old_checkout(tmp_path)
    (user_dir() / ".env").write_text("OPENROUTER_MAX_TOKENS=900\n", "utf-8")
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", "1200")
    _copy(root)
    assert Settings.load().openrouter_max_tokens == 1200


def _redeployed(espanso: Path) -> None:
    for path in (espanso / "match").iterdir():
        path.unlink()
    deploy_old(espanso, UV_BIN)


def test_retire_waits_for_the_redeploy(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = old_checkout(tmp_path)
    original = (root / ".env").read_bytes()
    deploy_old(espanso, str(venv_launcher(root)))
    _copy(root)
    with pytest.raises(MigrationError, match=r"the match files still run .*deploy them first"):
        config_store.plan_retire(root, espanso_dir=espanso)

    _redeployed(espanso)
    plan = config_store.plan_retire(root, espanso_dir=espanso)
    assert COPIED_KEY not in "\n".join(plan.describe())
    for consent in ("", plan.token[::-1]):
        with pytest.raises(MigrationError, match="not confirmed"):
            config_store.apply_retire(root, espanso_dir=espanso, consent=consent)
    assert (root / ".env").read_bytes() == original

    place = config_store.apply_retire(root, espanso_dir=espanso, consent=plan.token)

    assert not (root / ".env").exists()
    assert place == plan.target
    assert place.name == "1-checkout.env.inactive"
    assert place.read_bytes() == original
    assert previous_install.copied_env(os.environ) is None
    with pytest.raises(MigrationError, match="already retired"):
        config_store.plan_retire(root, espanso_dir=espanso)


def test_retire_refuses_while_the_manifest_names_the_checkout(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = old_checkout(tmp_path)
    launcher = str(venv_launcher(root)).replace("\\", "/")
    deploy.apply(deploy.plan(espanso, launcher, deploy.Manifest.load()))
    _copy(root)
    # The files no longer name the checkout, but the manifest still does.
    deploy_old(espanso, UV_BIN)
    with pytest.raises(MigrationError, match="the match files still run"):
        config_store.plan_retire(root, espanso_dir=espanso)


def test_retire_refuses_when_espanso_cannot_say_where_its_files_are(
    tmp_path: Path, env: MutableMapping[str, str]
) -> None:
    root = old_checkout(tmp_path)
    _copy(root)
    with pytest.raises(MigrationError, match="pass --espanso-dir"):
        config_store.plan_retire(root, runner=FakeRunner())


def test_a_retire_cut_short_can_be_done_again(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = old_checkout(tmp_path)
    _copy(root)
    plan = config_store.plan_retire(root, espanso_dir=espanso)

    def interrupted(source: Path, target: Path) -> bool:
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        patched.setattr(config_store, "_move", interrupted)
        with pytest.raises(KeyboardInterrupt):
            config_store.apply_retire(root, espanso_dir=espanso, consent=plan.token)
    # The marker names both places, but the .env is still where it was: not retired.
    assert previous_install.copied_env(os.environ) == (root.resolve(), root.resolve() / ".env")
    plan = config_store.plan_retire(root, espanso_dir=espanso)
    place = config_store.apply_retire(root, espanso_dir=espanso, consent=plan.token)
    assert place.is_file()
    assert not (root / ".env").exists()


def test_retire_in_legacy_mode_is_refused(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = old_checkout(tmp_path)
    _copy(root)
    monkeypatch.setenv("PROMPTMEND_ENV", str(root / ".env"))
    with pytest.raises(MigrationError, match="PROMPTMEND_ENV is set"):
        config_store.plan_retire(root, espanso_dir=espanso)


def test_retire_refusals(tmp_path: Path, espanso: Path, env: MutableMapping[str, str]) -> None:
    root = old_checkout(tmp_path)
    with pytest.raises(MigrationError, match="nothing was copied"):
        config_store.plan_retire(root, espanso_dir=espanso)
    _copy(root)
    other = old_checkout(tmp_path / "other")
    with pytest.raises(MigrationError, match="nothing was copied"):
        config_store.plan_retire(other, espanso_dir=espanso)
    (root / ".env").write_text("OLLAMA_MODEL=edited\n", "utf-8")
    with pytest.raises(MigrationError, match="changed since its settings were copied"):
        config_store.plan_retire(root, espanso_dir=espanso)
    (root / ".env").unlink()
    with pytest.raises(MigrationError, match="is gone"):
        config_store.plan_retire(root, espanso_dir=espanso)


def _rollback() -> None:
    config_store.apply_rollback(consent=config_store.plan_rollback().token)


def test_rollback_of_a_copy_leaves_the_old_env_alone(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = old_checkout(tmp_path)
    original = (root / ".env").read_bytes()
    active = user_dir() / ".env"
    active.write_text("OLLAMA_MODEL=mine\n", "utf-8")
    deploy_old(espanso, str(venv_launcher(root)))
    _copy(root)
    plan = config_store.plan_rollback()
    # The moved user-config .env is read again; the copied checkout's never is.
    assert (plan.unread, plan.read_again) == ((root.resolve(),), True)
    assert f"This install does not read {root.resolve() / '.env'}" in plan.describe()[-1]
    _rollback()
    assert active.read_text("utf-8") == "OLLAMA_MODEL=mine\n"
    assert (root / ".env").read_bytes() == original
    assert not config.settings_file().exists()
    assert not (user_dir() / "secrets.toml").exists()
    # The gate is open again, so the checkout is offered again.
    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso)
    assert _roots(found) == {root.resolve(): {LAUNCHER}}


def test_rollback_after_a_retire_puts_the_old_env_back(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = old_checkout(tmp_path)
    original = (root / ".env").read_bytes()
    _copy(root)
    plan = config_store.plan_retire(root, espanso_dir=espanso)
    config_store.apply_retire(root, espanso_dir=espanso, consent=plan.token)
    rollback = config_store.plan_rollback()
    assert [(s.name, o) for s, o in rollback.restores] == [
        ("1-checkout.env.inactive", root.resolve() / ".env")
    ]
    assert not rollback.read_again
    _rollback()
    assert (root / ".env").read_bytes() == original
    assert not config.settings_file().exists()


def test_rollback_refuses_a_new_env_where_a_retired_one_goes_back(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = old_checkout(tmp_path)
    _copy(root)
    plan = config_store.plan_retire(root, espanso_dir=espanso)
    config_store.apply_retire(root, espanso_dir=espanso, consent=plan.token)
    (root / ".env").write_text("OLLAMA_MODEL=new\n", "utf-8")
    with pytest.raises(MigrationError, match="exists again"):
        config_store.plan_rollback()


def test_the_copy_is_pending_after_the_gate_closes(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], no_clipboard: None
) -> None:
    root = old_checkout(tmp_path)
    launcher = str(venv_launcher(root)).replace("\\", "/") if WINDOWS else str(venv_launcher(root))
    deploy_old(espanso, str(venv_launcher(root)))
    _copy(root)

    found = previous_install.detect(runner=FakeRunner(), espanso_dir=espanso)
    assert found.gated == previous_install.SAVED
    assert found.candidates == ()
    assert found.pending == previous_install.CopyRecord(
        root.resolve(), root.resolve() / ".env", (launcher,)
    )
    report = doctor.run(espanso_dir=espanso, launcher=UV_BIN, runner=FakeRunner())
    check = _check(report)
    assert check.status == "info"
    assert "espanso deploy" in check.message
    assert report.to_json()["checks"]["previous_install"]["data"]["retire_pending"] is None

    _redeployed(espanso)
    report = doctor.run(espanso_dir=espanso, launcher=UV_BIN, runner=FakeRunner())
    check = _check(report)
    assert check.status == "warn"
    assert f"config retire --from {root.resolve()}" in check.message
    data = report.to_json()["checks"]["previous_install"]["data"]
    assert data["retire_pending"] == str(root.resolve() / ".env")
    assert COPIED_KEY not in repr(report.to_json())

    plan = config_store.plan_retire(root, espanso_dir=espanso)
    config_store.apply_retire(root, espanso_dir=espanso, consent=plan.token)
    report = doctor.run(espanso_dir=espanso, launcher=UV_BIN, runner=FakeRunner())
    assert _check(report).status == "ok"


@pytest.mark.parametrize("text", ["{", "[]", '{"mode": "copy", "sources": {}}'])
def test_a_damaged_marker_has_nothing_pending(env: MutableMapping[str, str], text: str) -> None:
    (user_dir() / "migration.json").write_text(text, "utf-8")
    assert previous_install.copied_env(os.environ) is None


# --- The commands ----------------------------------------------------------------------------


def _cli(*args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(app, list(args), input=input)


def _token(output: str) -> str:
    match = re.search(r"Preview token: (\w+)", output)
    assert match
    return match.group(1)


def test_config_migrate_from_and_retire(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = old_checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    preview = _cli("config", "migrate", "--from", str(root), "--dry-run")
    assert preview.exit_code == 0, preview.output
    assert "stays in place" in preview.stdout
    assert "Dry run: nothing was changed." in preview.stdout
    # --yes needs this preview's token.
    assert _cli("config", "migrate", "--from", str(root), "--yes").exit_code == 2
    token = _token(preview.stdout)
    done = _cli("config", "migrate", "--from", str(root), "--yes", "--preview-token", token)
    assert done.exit_code == 0, done.output
    assert f"config retire --from {root.resolve()}" in done.stdout
    assert Settings.load().openrouter_api_key == COPIED_KEY

    refused = _cli("config", "retire", "--from", str(root), "--espanso-dir", str(espanso))
    assert refused.exit_code == 1
    assert "deploy them first" in refused.stderr

    _redeployed(espanso)
    where = ["--from", str(root), "--espanso-dir", str(espanso)]
    preview = _cli("config", "retire", *where, "--dry-run")
    assert preview.exit_code == 0, preview.output
    token = _token(preview.stdout)
    done = _cli("config", "retire", *where, "--yes", "--preview-token", token)
    assert done.exit_code == 0, done.output
    assert not (root / ".env").exists()
    for result in (preview, done):
        assert COPIED_KEY not in result.output


def test_setup_non_interactive_offers_the_copy_and_writes_nothing(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = old_checkout(tmp_path)
    deploy_old(espanso, str(venv_launcher(root)))
    monkeypatch.setattr(deploy, "run_command", FakeRunner())
    result = _cli(
        "setup",
        "--non-interactive",
        "--no-smoke-test",
        "--espanso-dir",
        str(espanso),
        "--launcher",
        UV_BIN,
    )
    assert result.exit_code == 0, result.output
    assert f"Previous install: {root.resolve()} (launcher)" in result.stdout
    todo = f"to do: copy the settings of {root.resolve()}: `promptmend config migrate --from"
    assert todo in result.stdout
    assert "Taken from" in result.stdout
    assert COPIED_KEY not in result.output
    assert not config.settings_file().exists()
    assert (root / ".env").is_file()


def _interactive(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, str] | None = None
) -> FakeRunner:
    from promptmend import smoke
    from promptmend.commands import common

    fake = FakeRunner({"espanso restart": "", **(answers or {})})
    monkeypatch.setattr(deploy, "run_command", fake)
    monkeypatch.setattr(common, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(common, "_getpass", lambda prompt: "")
    monkeypatch.setattr(smoke, "run", lambda p: smoke.SmokeResult(True, smoke.REPLY, 1, "ok"))
    return fake


def test_setup_warns_before_deploying_ahead_of_the_copy(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = old_checkout(tmp_path)
    old = str(venv_launcher(root))
    deploy_old(espanso, old)
    before = {p.name: p.read_bytes() for p in (espanso / "match").iterdir()}
    _interactive(monkeypatch)
    result = _cli("setup", "--non-interactive", "--espanso-dir", str(espanso), "--launcher", UV_BIN)
    assert result.exit_code == 0, result.output
    assert "are not copied yet" in result.stderr
    assert f"config migrate --from {root.resolve()}" in result.stdout
    assert {p.name: p.read_bytes() for p in (espanso / "match").iterdir()} == before
    assert not config.settings_file().exists()


def test_setup_carries_on_past_a_broken_env_it_found_itself(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = old_checkout(tmp_path, "OPENROUTER_MAX_TOKENS=lots\n")
    deploy_old(espanso, str(venv_launcher(root)))
    monkeypatch.setattr(deploy, "run_command", FakeRunner())
    args = ["setup", "--non-interactive", "--no-smoke-test", "--provider", "ollama"]
    result = _cli(*args, "--espanso-dir", str(espanso), "--launcher", UV_BIN)
    assert result.exit_code == 0, result.output
    assert "its settings were not copied" in result.stdout
    assert f"config migrate --from {root.resolve()}`" in result.stdout
    # Nothing is written, so the offer stays open for the next run (D-MIG-4).
    assert not config.settings_file().exists()
    assert not (config._user_config_dir() / config_files.SECRETS_FILE).exists()
    assert previous_install.gate(os.environ) is None
    # Named with --migrate-from, the same checkout fails setup: the user asked for it.
    result = _cli(*args, "--espanso-dir", str(espanso), "--migrate-from", str(root))
    assert result.exit_code == 1


def test_setup_after_a_copy_offers_the_retire_once_deployed(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptmend import smoke

    root = old_checkout(tmp_path)
    _copy(root)
    monkeypatch.setattr(deploy, "run_command", FakeRunner({"espanso restart": ""}))
    monkeypatch.setattr(smoke, "run", lambda p: smoke.SmokeResult(True, smoke.REPLY, 1, "ok"))
    args = ["--espanso-dir", str(espanso), "--launcher", UV_BIN, "--migrate-from", str(root)]
    result = _cli("setup", "--non-interactive", "--deploy", *args)
    assert result.exit_code == 0, result.output
    assert f"to do: retire the old .env: `promptmend config retire --from {root.resolve()}`" in (
        result.stdout
    )
    assert (root / ".env").exists()
    assert COPIED_KEY not in result.output


def test_rollback_of_a_retired_copy_never_says_the_env_is_read_again(
    tmp_path: Path, espanso: Path, env: MutableMapping[str, str]
) -> None:
    root = old_checkout(tmp_path)
    _copy(root)
    plan = config_store.plan_retire(root, espanso_dir=espanso)
    config_store.apply_retire(root, espanso_dir=espanso, consent=plan.token)
    preview = _cli("config", "rollback", "--dry-run").stdout
    assert f"This install does not read {root.resolve() / '.env'}" in preview
    assert f"`promptmend config migrate --from {root.resolve()}`" in preview
    token = preview.split("Preview token: ")[1].split()[0]
    result = _cli("config", "rollback", "--yes", "--preview-token", token)
    assert result.exit_code == 0, result.output
    assert result.stdout.endswith("Rolled back.\n")
    assert (root / ".env").is_file()
