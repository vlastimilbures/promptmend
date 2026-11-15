"""espanso_windows answers `espanso ...` on Windows without reading Espanso's output (#209,
#210, #211). Every probe is a fake, so these run on any OS."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from promptmend import deploy, espanso_windows
from promptmend.deploy import CommandFailure
from promptmend.espanso_windows import System

# Kept at import, before conftest refuses it in every test.
REAL_RUN_COMMAND = deploy.run_command


class FakeProcess:
    def __init__(self, code: int | None) -> None:
        self.code = code

    def wait(self, timeout: float) -> int:
        if self.code is None:
            raise subprocess.TimeoutExpired("cmd", timeout)
        return self.code


class Fakes:
    """A Windows box: files under tmp_path, a pipe list and a process list we control."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.pipes: list[str] | None = []
        self.images: list[str] | None = []
        self.on_path: dict[str, str] = {}
        self.registry: list[Path] = []
        self.launched: list[tuple[list[str], dict[str, Any]]] = []
        self.exit_code: int | None = 0
        self.starts_daemon = True
        self.now = 0.0
        self.environ = {
            "LOCALAPPDATA": str(tmp_path / "local"),
            "APPDATA": str(tmp_path / "roaming"),
            "SystemRoot": str(tmp_path / "Windows"),
        }

    def exe(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "espansod.exe"
        path.write_bytes(b"")
        return path

    def _pipes(self) -> list[str]:
        if self.pipes is None:
            raise espanso_windows.Unreadable("no listing")
        return self.pipes

    def _images(self) -> list[str]:
        if self.images is None:
            raise OSError("no process list")
        return self.images

    def _popen(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.launched.append((argv, kwargs))
        if self.starts_daemon and self.pipes is not None:
            self.pipes.append(espanso_windows.PIPE_NAME)
        return FakeProcess(self.exit_code)

    def _sleep(self, seconds: float) -> None:
        self.now += seconds

    def system(self) -> System:
        return System(
            environ=self.environ,
            which=self.on_path.get,
            registry=lambda: self.registry,
            images=self._images,
            pipes=self._pipes,
            popen=self._popen,
            sleep=self._sleep,
            clock=lambda: self.now,
        )

    def run(self, *args: str) -> Any:
        return espanso_windows.run(["espanso", *args], self.system())


@pytest.fixture
def box(tmp_path: Path) -> Fakes:
    return Fakes(tmp_path)


def test_not_found_anywhere(box: Fakes) -> None:
    assert box.run("path", "config") == CommandFailure()
    assert box.run("status") == CommandFailure()


# #211: a stale PATH (Windows Terminal) still finds the per-user install.
def test_finds_the_per_user_install_without_path(box: Fakes) -> None:
    box.exe(box.tmp / "local" / "Programs" / "Espanso")
    assert box.run("path", "config") == f"{box.tmp / 'roaming' / 'espanso'}\n"


def test_finds_it_next_to_espanso_cmd_and_in_the_registry(box: Fakes) -> None:
    on_path = box.exe(box.tmp / "path")
    box.on_path["espanso"] = str(on_path.with_name("espanso.cmd"))
    assert espanso_windows.find_daemon(box.system()) == on_path
    box.on_path.clear()
    installed = box.exe(box.tmp / "registry")
    box.registry = [installed.parent]
    assert espanso_windows.find_daemon(box.system()) == installed

    def broken() -> list[Path]:
        raise OSError("no registry")

    assert (
        espanso_windows.find_daemon(
            System(environ={}, which=lambda _: None, registry=broken, images=list)
        )
        is None
    )


# #210: a portable Espanso keeps its config in `.espanso` next to the executable, and the
# running one wins over another install.
def test_portable_config_of_the_running_daemon(box: Fakes) -> None:
    box.exe(box.tmp / "local" / "Programs" / "Espanso")
    portable = box.exe(box.tmp / "portable")
    (portable.parent / ".espanso").mkdir()
    box.images = [r"C:\Windows\explorer.exe", str(portable)]
    assert box.run("path", "config") == f"{portable.parent / '.espanso'}\n"
    box.images = None
    assert box.run("path", "config") == f"{box.tmp / 'roaming' / 'espanso'}\n"


# #210: running is the daemon's pipe, never "the command printed something".
def test_status_from_the_pipe_then_the_process_list(box: Fakes) -> None:
    exe = box.exe(box.tmp / "local" / "Programs" / "Espanso")
    stopped = box.run("status")
    assert stopped == CommandFailure(
        found=True, path=str(exe), returncode=1, error="espanso is not running"
    )
    box.pipes = ["other", espanso_windows.PIPE_NAME]
    assert box.run("status") == "espanso is running\n"
    box.pipes = None
    box.images = [str(exe).upper()]
    assert box.run("status") == "espanso is running\n"
    box.images = None
    assert box.run("status").error == "cannot tell if it runs"


# #209: started behind a hidden cmd.exe console, every stdio handle the null device.
def test_start_is_detached_and_waits_for_the_pipe(box: Fakes) -> None:
    exe = box.exe(box.tmp / "local" / "Programs" / "Espanso")
    assert box.run("restart") == ""
    ((argv, kwargs),) = box.launched
    assert argv == [
        str(box.tmp / "Windows" / "System32" / "cmd.exe"),
        "/d",
        "/c",
        str(exe),
        "start",
    ]
    assert kwargs["stdin"] is kwargs["stdout"] is kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["creationflags"] == (
        espanso_windows.CREATE_NO_WINDOW | espanso_windows.CREATE_NEW_PROCESS_GROUP
    )
    assert box.run("restart") == ""
    assert box.launched[-1][0][-1] == "restart"


def test_start_failures(box: Fakes) -> None:
    exe = str(box.exe(box.tmp / "local" / "Programs" / "Espanso"))
    box.exit_code, box.starts_daemon = 3, False
    assert box.run("start") == CommandFailure(found=True, path=exe, returncode=3)
    box.exit_code = None
    assert box.run("start") == CommandFailure(found=True, path=exe, error="Espanso did not start")
    assert box.now >= espanso_windows.READY_TIMEOUT

    def refused(argv: Sequence[str], **kwargs: Any) -> Any:
        raise PermissionError("access denied")

    system = System(
        environ=box.environ,
        which=lambda _: None,
        registry=list,
        images=list,
        pipes=list,
        popen=refused,
    )
    failed = espanso_windows.run(["espanso", "start"], system)
    assert failed == CommandFailure(found=True, path=exe, error="access denied")
    assert box.run("edit").error == "not run on Windows"


def test_default_config_without_appdata(box: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = box.exe(box.tmp / "x")
    monkeypatch.setattr(Path, "home", lambda: box.tmp / "home")
    got = espanso_windows.config_dir(exe, System(environ={}))
    assert got == box.tmp / "home" / "AppData" / "Roaming" / "espanso"
    assert espanso_windows._cmd_exe({}) == str(Path("C:\\Windows") / "System32" / "cmd.exe")


def test_pipe_listing_error_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(path: str) -> list[str]:
        raise FileNotFoundError(path)

    monkeypatch.setattr(os, "listdir", fail)
    with pytest.raises(espanso_windows.Unreadable):
        espanso_windows._pipes()
    monkeypatch.setattr(os, "listdir", lambda path: ["a"])
    assert espanso_windows._pipes() == ["a"]


def test_run_command_hands_espanso_to_it_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Sequence[str]] = []

    def fake(argv: Sequence[str]) -> str:
        seen.append(argv)
        return "C:/espanso\n"

    monkeypatch.setattr(deploy, "WINDOWS_ESPANSO", True)
    monkeypatch.setattr(espanso_windows, "run", fake)
    assert REAL_RUN_COMMAND(deploy.PATH_CONFIG) == "C:/espanso\n"
    assert seen == [deploy.PATH_CONFIG]


def test_not_found_names_where_it_looked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy, "WINDOWS_ESPANSO", True)
    assert CommandFailure().describe(deploy.PATH_CONFIG) == (
        "espanso was not found (PATH, %LOCALAPPDATA%\\Programs\\Espanso, registry)"
    )
    assert CommandFailure().describe(["uv"]) == "uv was not found on PATH"


# --- one look per reload (#315) -------------------------------------------------------------


def test_the_registry_is_walked_only_when_nothing_earlier_is_found(box: Fakes) -> None:
    walks: list[int] = []

    def registry() -> list[Path]:
        walks.append(1)
        return box.registry

    on_path = box.exe(box.tmp / "path")
    box.on_path["espansod"] = str(on_path)
    system = dataclasses.replace(box.system(), registry=registry)
    assert espanso_windows.find_daemon(system) == on_path
    assert walks == []
    box.on_path.clear()
    installed = box.exe(box.tmp / "registry")
    box.registry = [installed.parent]
    assert espanso_windows.find_daemon(system) == installed
    assert walks == [1]


def test_once_remembers_each_probe_and_its_failure(box: Fakes) -> None:
    looks = {"images": 0, "registry": 0}

    def images() -> list[str]:
        looks["images"] += 1
        raise OSError("no process list")

    def registry() -> list[Path]:
        looks["registry"] += 1
        return []

    system = dataclasses.replace(box.system(), images=images, registry=registry).once()
    box.pipes = None  # so status would fall back to the process list
    for _ in range(2):
        assert espanso_windows.run(["espanso", "path", "config"], system) == CommandFailure()
        assert espanso_windows.run(["espanso", "status"], system) == CommandFailure()
        assert espanso_windows.is_running(system) is None
    assert looks == {"images": 1, "registry": 1}


def test_one_look_shares_the_process_list_between_queries(
    box: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside one_look(), `path config` and `status` enumerate the processes once; a start
    looks again (it polls for the daemon), and so does a query after the look."""
    daemon = box.exe(box.tmp / "running")
    box.pipes = None
    enumerations: list[int] = []

    def images() -> list[str]:
        enumerations.append(1)
        return [str(daemon)]

    fake = dataclasses.replace(box.system(), images=images)
    monkeypatch.setattr(espanso_windows, "System", lambda: fake)
    with espanso_windows.one_look():
        with espanso_windows.one_look():  # a nested one keeps the outer look
            assert espanso_windows.run(["espanso", "path", "config"]) == (
                f"{box.tmp / 'roaming' / 'espanso'}\n"
            )
        assert espanso_windows.run(["espanso", "status"]) == "espanso is running\n"
        assert len(enumerations) == 1
        assert espanso_windows.run(["espanso", "start"]) == ""
        assert len(enumerations) > 1
    before = len(enumerations)
    assert espanso_windows.run(["espanso", "status"]) == "espanso is running\n"
    assert len(enumerations) == before + 2  # find_daemon, then is_running
