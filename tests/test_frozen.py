"""The frozen Windows build (#185): what changes when promptmend runs as its own exe
(``sys.frozen``), the WinGet launcher, and scripts/build_windows.py's offline parts. The build
itself runs only in CI (test.yml's frozen job), on windows-latest."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from promptmend import deploy, doctor, smoke, update_check
from promptmend.entry import self_command

REPO = Path(__file__).resolve().parents[1]
_SCRIPT = REPO / "scripts" / "build_windows.py"
if TYPE_CHECKING:
    import build_windows as bw
else:
    _spec = importlib.util.spec_from_file_location("build_windows", _SCRIPT)
    assert _spec
    assert _spec.loader
    bw = importlib.util.module_from_spec(_spec)
    sys.modules["build_windows"] = bw
    _spec.loader.exec_module(bw)

PACKAGE = "vlastimilbures.PromptMend_Microsoft.Winget.Source_8wekyb3d8bbwe"


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)


# --- Child processes ---------------------------------------------------------------------------


def test_self_command_runs_the_module_with_this_interpreter() -> None:
    assert not getattr(sys, "frozen", False)
    assert self_command() == [sys.executable, "-P", "-m", "promptmend.cli"]


@pytest.mark.usefixtures("frozen")
def test_self_command_of_a_frozen_build_is_its_exe() -> None:
    assert self_command() == [sys.executable]


@pytest.mark.usefixtures("frozen")
def test_the_smoke_test_runs_the_frozen_exe() -> None:
    seen: list[Sequence[str]] = []

    def runner(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, bytes]:
        seen.append(argv)
        return 0, smoke.REPLY.encode()

    smoke.run("ollama", runner=runner)
    assert seen[0][:2] == [sys.executable, "improve"]


# --- doctor --------------------------------------------------------------------------------------


def test_cli_check_says_whether_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    check = doctor._cli_check()
    assert check.data["frozen"] is False
    assert "frozen" not in check.message
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    check = doctor._cli_check()
    assert check.data["frozen"] is True
    assert check.message.endswith("(frozen build)")


def test_winget_upgrade_command() -> None:
    assert update_check.upgrade_command("winget") == "winget upgrade vlastimilbures.PromptMend"
    assert doctor.upgrade_hint({"channel": "winget", "editable": False}) == (
        "winget upgrade vlastimilbures.PromptMend"
    )


# --- The WinGet launcher ---------------------------------------------------------------------


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


@pytest.fixture(autouse=True)
def os_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The system temp folder, for deploy: a folder of its own, since pytest's tmp_path
    itself lies under the real one."""
    temp = tmp_path / "OsTemp"
    temp.mkdir()
    monkeypatch.setattr(deploy, "_temp_dir", lambda: temp)
    return temp


def _winget(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """(environ, the Links alias path, the package's exe) under a fake %LOCALAPPDATA%."""
    local = tmp_path / "Local"
    root = local / "Microsoft" / "WinGet"
    exe = _file(root / "Packages" / PACKAGE / "promptmend" / "promptmend.exe")
    return {"LOCALAPPDATA": str(local)}, root / "Links" / "promptmend.exe", exe


def _resolve(exe: Path, environ: Mapping[str, str], *, windows: bool = True) -> deploy.Launcher:
    def no_runner(argv: Sequence[str]) -> str | None:
        raise AssertionError(f"a frozen build asks no tool: {argv}")

    return deploy.resolve_launcher(
        runner=no_runner,
        frozen=True,
        executable=exe,
        environ=environ,
        windows=windows,
    )


def _link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link)
    except OSError:  # Windows without the symlink privilege: a hard link is the same file
        os.link(target, link)


def test_winget_links_alias_wins(tmp_path: Path) -> None:
    environ, link, exe = _winget(tmp_path)
    _link(link, exe)
    assert _resolve(exe, environ) == deploy.Launcher(link, "winget")
    # The bootloader may report the alias itself as the executable.
    assert _resolve(link, environ) == deploy.Launcher(link, "winget")
    windows = deploy.launcher_text(
        r"C:\Users\me\AppData\Local\Microsoft\WinGet\Links\promptmend.exe", windows=True
    )
    assert windows == "C:/Users/me/AppData/Local/Microsoft/WinGet/Links/promptmend.exe"


def test_winget_package_folder_without_the_alias(tmp_path: Path) -> None:
    environ, _, exe = _winget(tmp_path)
    assert _resolve(exe, environ) == deploy.Launcher(exe, "winget")


def test_an_alias_to_another_exe_is_not_ours(tmp_path: Path) -> None:
    environ, link, exe = _winget(tmp_path)
    _file(link)  # a different promptmend.exe
    assert _resolve(exe, environ) == deploy.Launcher(exe, "winget")


def test_another_package_or_a_versioned_folder_is_not_winget(tmp_path: Path) -> None:
    environ, _, _ = _winget(tmp_path)
    packages = Path(environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Packages"
    other = _file(packages / "Someone.Else_x" / "promptmend.exe")
    assert _resolve(other, environ) == deploy.Launcher(other, "script")
    versioned = _file(packages / PACKAGE / "1.2.3" / "promptmend.exe")
    with pytest.raises(deploy.DeployError, match="versioned place"):
        _resolve(versioned, environ)
    assert not deploy._RELEASE_IN_PATH.search(PACKAGE)


def test_winget_machine_scope(tmp_path: Path) -> None:
    root = tmp_path / "Program Files" / "WinGet"
    exe = _file(root / "Packages" / PACKAGE / "promptmend" / "promptmend.exe")
    environ = {"ProgramFiles": str(tmp_path / "Program Files")}
    assert _resolve(exe, environ) == deploy.Launcher(exe, "winget")
    link = root / "Links" / "promptmend.exe"
    _link(link, exe)
    both = {**environ, "LOCALAPPDATA": str(tmp_path / "Local")}
    assert _resolve(exe, both) == deploy.Launcher(link, "winget")


def test_a_downloaded_or_temporary_exe_is_refused(tmp_path: Path, os_temp: Path) -> None:
    downloads = tmp_path / "Downloads" / "promptmend-0.22.0-windows-x64" / "promptmend"
    for exe in (
        _file(downloads / "promptmend.exe"),
        _file(os_temp / "Temp1_promptmend-x.zip" / "promptmend" / "promptmend.exe"),
    ):
        with pytest.raises(deploy.DeployError, match=r"Programs\\promptmend, or pass --launcher"):
            _resolve(exe, {})


def test_a_frozen_exe_elsewhere_is_a_script(tmp_path: Path) -> None:
    exe = _file(tmp_path / "Tools" / "promptmend" / "promptmend.exe")
    assert _resolve(exe, {}) == deploy.Launcher(exe, "script")
    environ, _, packaged = _winget(tmp_path)
    assert _resolve(exe, environ) == deploy.Launcher(exe, "script")
    assert _resolve(packaged, environ, windows=False) == deploy.Launcher(packaged, "script")
    with pytest.raises(deploy.DeployError, match="was not found"):
        _resolve(tmp_path / "gone.exe", {})


@pytest.mark.usefixtures("frozen")
def test_resolve_launcher_reads_sys_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environ, _, exe = _winget(tmp_path)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setenv("LOCALAPPDATA", environ["LOCALAPPDATA"])
    found = deploy.resolve_launcher(windows=True)
    assert found == deploy.Launcher(exe, "winget")
    check = doctor._install_check(found, None)
    assert check.data["channel"] == "winget"


# --- scripts/build_windows.py --------------------------------------------------------------


def test_zip_name_and_version() -> None:
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))
    assert bw.version() == pyproject["project"]["version"]
    assert bw.zip_name("1.2.3") == "promptmend-1.2.3-windows-x64.zip"


def _onedir(folder: Path) -> Path:
    _file(folder / "promptmend.exe")
    for name in bw.REQUIRED:
        _file(folder / name)
    return folder


def test_make_zip_nests_the_onedir_folder(tmp_path: Path) -> None:
    folder = _onedir(tmp_path / "promptmend")
    _file(folder / "_internal" / "base_library.zip")
    out = tmp_path / "dist" / "x.zip"
    names = bw.make_zip(folder, out)
    assert names == [
        "promptmend/_internal/base_library.zip",
        "promptmend/_internal/promptmend/espanso/match/prompts-llm.yml",
        "promptmend/_internal/promptmend/espanso/match/prompts-template.yml",
        "promptmend/_internal/promptmend/prompts/default.md",
        "promptmend/promptmend.exe",
    ]
    with zipfile.ZipFile(out) as archive:
        assert archive.namelist() == names
        assert {i.date_time for i in archive.infolist()} == {bw.FIXED_DATE}
    first = out.read_bytes()
    bw.make_zip(folder, out)
    assert out.read_bytes() == first  # the same files make the same zip


def test_make_zip_refuses_a_folder_without_the_exe(tmp_path: Path) -> None:
    _file(tmp_path / "promptmend" / "promptmend.exe")
    with pytest.raises(FileNotFoundError, match="_internal folder"):
        bw.make_zip(tmp_path / "promptmend", tmp_path / "x.zip")


@pytest.mark.parametrize("name", bw.REQUIRED)
def test_make_zip_refuses_a_folder_without_its_package_data(tmp_path: Path, name: str) -> None:
    folder = _onedir(tmp_path / "promptmend")
    (folder / name).unlink()
    with pytest.raises(FileNotFoundError, match="lacks the package data"):
        bw.make_zip(folder, tmp_path / "x.zip")
    assert not (tmp_path / "x.zip").exists()


def test_build_runs_pyinstaller_then_zips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str], **kwargs: object) -> None:
        calls.append(argv)
        _onedir(tmp_path / "work" / "dist" / "promptmend")

    monkeypatch.setattr(subprocess, "run", fake)
    out = bw.build(tmp_path / "out", tmp_path / "work")
    assert out == tmp_path / "out" / bw.zip_name(bw.version())
    assert out.is_file()
    ((*head, spec),) = calls
    assert head[:3] == [sys.executable, "-m", "PyInstaller"]
    assert spec == str(bw.SPEC)
    assert bw.SPEC.is_file()


def test_trigger_args_are_the_i_trigger_with_the_draft_as_argument() -> None:
    args = bw.trigger_args()
    assert args[:3] == ["improve", "--trigger-id", "i"]
    assert args[-4:] == ["--source", "argument", "--text", "draft"]
    with pytest.raises(LookupError):
        bw.trigger_args("-nope-")


def test_time_and_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("PROMPT_HISTORY", "false")
    bw.main(["time", "--runs", "1", "--", sys.executable, "-m", "promptmend.entry"])
    text = summary.read_text("utf-8")
    assert "first (cold)" in text
    assert "warm, median of 1" in text


def test_a_failed_trigger_run_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="the trigger failed"):
        bw.time_trigger([sys.executable, "-c", "import sys; sys.exit(3)"], runs=1)


def test_main_build(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(bw, "build", lambda: Path("dist/x.zip"))
    bw.main(["build"])
    assert capsys.readouterr().out.strip() == str(Path("dist/x.zip"))
