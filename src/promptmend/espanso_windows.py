"""Espanso on Windows, without reading its output.

`espansod.exe` is a GUI-subsystem program that attaches to its parent's console and writes
there, not to a pipe: `espanso path config` and `espanso status` return nothing to us, while
their answer lands in the user's terminal. A daemon it starts inherits our pipes (so a
worker never sees EOF and blocks the interface's exit) and our console (so Ctrl+C or closing
the terminal kills Espanso). So on Windows nothing here reads Espanso's output:

- the executable is found where Espanso puts it, not only on PATH, which a long-lived
  Windows Terminal keeps stale: the running daemon, PATH, the per-user install
  folder, the uninstall registry entry;
- the config folder is worked out as Espanso does: a portable `.espanso` folder next to the
  executable, else `%APPDATA%\\espanso`;
- "running" means Espanso's named pipe exists (listing the pipes connects to none);
- start and restart go through `cmd.exe` in a console of its own with no window, every stdio
  handle the null device: Espanso attaches to that hidden console, never ours.

deploy.run_command() hands every `espanso ...` call here on Windows and keeps the answer's
shape (stdout, or a CommandFailure), so its callers do not change. Inside one_look() (an
interface reload, a `doctor` run) `path config` and `status` share one process enumeration
and one registry walk, and the registry is walked only when no earlier candidate is a file.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DAEMON = "espansod.exe"
PIPE_NAME = "espansodaemonv2"
PIPE_DIR = "\\\\.\\pipe\\"
PORTABLE_DIR = ".espanso"
_UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
# subprocess's names exist only on Windows; the values are the Win32 ones.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
# How long a start or restart may take before we look at the pipe, and then how long the
# daemon gets to open it.
START_TIMEOUT = 20.0
READY_TIMEOUT = 5.0
POLL_INTERVAL = 0.25


class Unreadable(Exception):
    """A probe that could not answer (no pipe listing, no process list)."""


def _registry_locations() -> list[Path]:  # pragma: no cover - Windows registry only
    """Espanso's InstallLocation from the per-user, then the machine uninstall entries."""
    try:
        import winreg
    except ImportError:
        return []
    found: list[Path] = []
    reg: Any = winreg
    for hive in (reg.HKEY_CURRENT_USER, reg.HKEY_LOCAL_MACHINE):
        with contextlib.suppress(OSError), reg.OpenKey(hive, _UNINSTALL) as root:
            for index in range(reg.QueryInfoKey(root)[0]):
                with (
                    contextlib.suppress(OSError),
                    reg.OpenKey(root, reg.EnumKey(root, index)) as sub,
                ):
                    name = str(reg.QueryValueEx(sub, "DisplayName")[0])
                    if name.lower().startswith("espanso"):
                        location = str(reg.QueryValueEx(sub, "InstallLocation")[0])
                        if location:
                            found.append(Path(location))
    return found


def _process_images() -> list[str]:  # pragma: no cover - Win32 only
    """The full executable path of every process we may query."""
    import ctypes
    from ctypes import wintypes

    win_dll = getattr(ctypes, "WinDLL")  # noqa: B009 - absent from ctypes off Windows
    psapi, kernel32 = win_dll("psapi"), win_dll("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    size = 4096
    pids = (wintypes.DWORD * size)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        raise Unreadable("EnumProcesses failed")
    images: list[str] = []
    for pid in pids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                images.append(buffer.value)
        finally:
            kernel32.CloseHandle(handle)
    return images


def _pipes() -> list[str]:
    try:
        return os.listdir(PIPE_DIR)
    except OSError as exc:
        raise Unreadable(str(exc)) from exc


def _cmd_exe(environ: Mapping[str, str]) -> str:
    """cmd.exe from the system folder, never one found through PATH or the cwd."""
    return str(Path(environ.get("SystemRoot") or "C:\\Windows") / "System32" / "cmd.exe")


@dataclass(frozen=True)
class System:
    """What the probes read and start; tests pass fakes."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    which: Callable[[str], str | None] = shutil.which
    registry: Callable[[], list[Path]] = _registry_locations
    images: Callable[[], list[str]] = _process_images
    pipes: Callable[[], list[str]] = _pipes
    popen: Callable[..., Any] = subprocess.Popen
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def once(self) -> System:
        """This system, enumerating the processes and walking the registry at most once each
        (a failure is remembered too): for read-only queries only, since a start polls for the
        daemon and must see it appear."""
        return replace(self, images=_remembered(self.images), registry=_remembered(self.registry))


def _remembered[T](probe: Callable[[], T]) -> Callable[[], T]:
    answer: list[T] = []
    failed: list[Exception] = []

    def ask() -> T:
        if not answer and not failed:
            try:
                answer.append(probe())
            except Exception as exc:
                failed.append(exc)
        if failed:
            raise failed[0]
        return answer[0]

    return ask


# The read-only queries one_look() answers from one look at the system.
QUERIES = (("path", "config"), ("status",))
_shared: contextvars.ContextVar[System | None] = contextvars.ContextVar(
    "promptmend_espanso_look", default=None
)


@contextlib.contextmanager
def one_look() -> Iterator[None]:
    """While it is open, in this thread, run()'s QUERIES share one System.once(); a nested one
    keeps the outer look."""
    if _shared.get() is not None:
        yield
        return
    token = _shared.set(System().once())
    try:
        yield
    finally:
        _shared.reset(token)


def _running_daemons(system: System) -> list[Path]:
    try:
        images = system.images()
    except Exception:
        return []
    return [Path(image) for image in images if Path(image).name.lower() == DAEMON]


def _candidates(system: System) -> Iterator[Path]:
    yield from _running_daemons(system)
    for name in ("espansod", "espanso"):
        hit = system.which(name)
        if hit:
            yield Path(hit).with_name(DAEMON)
    local = system.environ.get("LOCALAPPDATA")
    if local:
        yield Path(local) / "Programs" / "Espanso" / DAEMON
    # Last and only when asked for: walking the uninstall keys is the slowest probe.
    try:
        locations = system.registry()
    except Exception:
        return
    yield from (location / DAEMON for location in locations)


def daemon_candidates(system: System) -> list[Path]:
    """Where espansod.exe may be, best first: the running one (the right install when a
    portable and an installed Espanso coexist), then PATH (`espanso.cmd` sits next to
    `espansod.exe`), the per-user install folder, the uninstall registry entry."""
    return list(_candidates(system))


def find_daemon(system: System) -> Path | None:
    """The first candidate that is a file; the later ones (the registry) are not looked up."""
    return next((path for path in _candidates(system) if path.is_file()), None)


def config_dir(daemon: Path, system: System) -> Path:
    """Espanso's own order: a portable `.espanso` next to the executable, else
    `%APPDATA%\\espanso` (its default, even before it exists)."""
    portable = daemon.parent / PORTABLE_DIR
    if portable.is_dir():
        return portable
    appdata = system.environ.get("APPDATA")
    return Path(appdata or Path.home() / "AppData" / "Roaming") / "espanso"


def is_running(system: System) -> bool | None:
    """True while Espanso's daemon pipe exists; None when neither the pipes nor the process
    list can be read."""
    try:
        return PIPE_NAME in system.pipes()
    except Unreadable:
        pass
    try:
        images = system.images()
    except Exception:
        return None
    return any(Path(image).name.lower() == DAEMON for image in images)


def launch(daemon: Path, verb: str, system: System) -> int | None:
    """Run `espansod.exe <verb>` behind a cmd.exe with its own hidden console: no pipe, no
    console and no Ctrl+C group shared with us. The exit code, or None when it is still
    running after START_TIMEOUT (it is left alone: it may be the daemon itself)."""
    process = system.popen(
        [_cmd_exe(system.environ), "/d", "/c", str(daemon), verb],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
    )
    try:
        return int(process.wait(timeout=START_TIMEOUT))
    except subprocess.TimeoutExpired:
        return None


def _wait_running(system: System) -> bool:
    deadline = system.clock() + READY_TIMEOUT
    while True:
        if is_running(system):
            return True
        if system.clock() >= deadline:
            return False
        system.sleep(POLL_INTERVAL)


def run(argv: Sequence[str], system: System | None = None) -> Any:
    """deploy.run_command() for an `espanso ...` argv on Windows: stdout as a string, or a
    deploy.CommandFailure, answered without reading Espanso's output."""
    from .deploy import CommandFailure

    args = tuple(argv[1:])
    if system is None:
        shared = _shared.get() if args in QUERIES else None
        system = shared or System()
    daemon = find_daemon(system)
    if daemon is None:
        return CommandFailure()
    path = str(daemon)
    if args == ("path", "config"):
        return f"{config_dir(daemon, system)}\n"
    if args == ("status",):
        running = is_running(system)
        if running:
            return "espanso is running\n"
        error = "espanso is not running" if running is False else "cannot tell if it runs"
        return CommandFailure(found=True, path=path, returncode=1, error=error)
    if args in (("start",), ("restart",)):
        # `restart` fails when Espanso is not running: start it then.
        verb = "restart" if is_running(system) else "start"
        try:
            code = launch(daemon, verb, system)
        except (OSError, subprocess.SubprocessError) as exc:
            return CommandFailure(found=True, path=path, error=str(exc))
        if code not in (0, None):
            return CommandFailure(found=True, path=path, returncode=code)
        if _wait_running(system):
            return ""
        return CommandFailure(found=True, path=path, error="Espanso did not start")
    return CommandFailure(found=True, path=path, error="not run on Windows")
