"""Managed Espanso deployment: plan, apply and reverse what promptmend writes into
Espanso's `match/` folder (#86).

Each packaged match file (assets.py) is rendered with the launcher path and a version stamp,
compared with the manifest of what we deployed before and with what is on disk, and written
only after the caller confirms. A file the user edited is never overwritten silently, only
files we own are removed, and nothing is ever written to Espanso's `config/` (#37).

Imported lazily by the `espanso` commands, never on the trigger path.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, assets
from .config import user_data_dir
from .match_history import KNOWN_SOURCES

PLACEHOLDER = "__PROMPT_WORKFLOW__"
STAMP = "# promptmend {version} (managed; edit at your own risk)\n"
# 0.18.0 and earlier stamped `# prompt-workflow <version> ...` (#169): both are ours.
_STAMP_LINE = re.compile(
    r"# (?:promptmend|prompt-workflow) \S+ \(managed; edit at your own risk\)\n"
)
MANIFEST_NAME = "espanso-manifest.json"
MANIFEST_FORMAT = 1
# Our backups of one file kept after a deploy; older ones we made are deleted.
KEEP_BACKUPS = 2
# A side-by-side copy is not a .yml file, so Espanso does not load its triggers twice.
SIDE_SUFFIX = ".promptmend-new"
COMMAND_TIMEOUT = 30
# After a timed-out command is killed, how long to wait for its output pipes.
GIVE_UP_TIMEOUT = 5

# Plan states (status) and conflict choices (deploy).
MISSING, IN_SYNC, STALE, MODIFIED, FOREIGN = "missing", "in sync", "stale", "modified", "foreign"
KEEP, OURS, SIDE = "keep", "ours", "side"
CHOICES = (KEEP, OURS, SIDE)

# The launcher is the first item of a script var's args (#18), a double-quoted YAML string
# that Espanso hands to the OS as the program, with no shell. Refuse what YAML would read as
# an escape or the string's end (a quote, a backslash), any character YAML cannot hold
# (control characters, lone surrogates, U+FFFE/U+FFFF), `{{`, which Espanso fills from its
# variables in every script param, and its own %HOME%, %CONFIG% and %PACKAGES%, which it
# replaces in every arg, rather than try to escape them. On Windows the path is checked
# after its backslashes become slashes.
_UNSAFE = re.compile(
    r'["\\\x00-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]|\{\{|%(?:HOME|CONFIG|PACKAGES)%'
)
# The launcher as the match files hold it: the first script arg, `args: ["<path>", ...`
# (since #18), or the quoted path in a shell cmd line as every earlier release wrote it,
# `cmd: "\"<path>\" improve`.
_SCRIPT_LAUNCHER = re.compile(r'args: \["([^"\\]+)"')
_QUOTED_LAUNCHER = re.compile(r'\\"([^"\\]+)\\"')
# A path component that names a release (0.15.0, 0.15.0_1): gone after the next upgrade.
_VERSIONED = re.compile(r"\d+(\.\d+)+([._-].*)?")


class DeployError(Exception):
    """A deployment that cannot go ahead; the message says why."""


@dataclass(frozen=True)
class CommandFailure:
    """Why a command gave no output: not found, timed out, or its exit code and the first
    stderr line that says something (#115)."""

    found: bool = False
    path: str | None = None
    returncode: int | None = None
    error: str | None = None
    timed_out: bool = False

    def describe(self, argv: Sequence[str]) -> str:
        name, command = argv[0], " ".join(argv)
        if not self.found:
            if name == "espanso" and WINDOWS_ESPANSO:
                return f"{name} was not found (PATH, %LOCALAPPDATA%\\Programs\\Espanso, registry)"
            return f"{name} was not found on PATH"
        if self.timed_out:
            return f"`{command}` timed out after {COMMAND_TIMEOUT} s"
        detail = f": {self.error}" if self.error else ""
        if self.returncode is None:
            return f"`{command}` could not be run{detail}"
        return f"`{command}` failed (exit {self.returncode}){detail}"


# Runs a command and returns its stdout, or a CommandFailure. A fake may answer None, read as
# not found. Every function that runs one looks run_command up when called, so tests can
# replace it.
Runner = Callable[[Sequence[str]], str | CommandFailure | None]
# A Rust panic's first line names only the source location; the message follows it. Newer
# Rust puts the thread id after the name: `thread 'main' (3545792) panicked at src/main.rs:1:2:`.
# Before Rust 1.73 the message was quoted in that line, and a backtrace note followed it.
_PANIC_HEADER = re.compile(r"thread '[^']*'( \(\d+\))? panicked at ")
# The quoted message can run over several lines, so its closing `', file:line:col` is optional.
_OLD_PANIC = re.compile(r"thread '[^']*' panicked at '(?P<message>.+?)(?:', \S+:\d+:\d+)?$")
_BACKTRACE_NOTE = "note: run with `RUST_BACKTRACE"
_ERROR_LINE_MAX = 200


def _error_line(stderr: str) -> str | None:
    """The first stderr line worth showing, cut to length, with only printable characters
    (no control, bidi or other format characters reach a terminal or a bug report)."""
    for raw in stderr.splitlines():
        line = "".join(c for c in raw if c.isprintable()).strip()
        old = _OLD_PANIC.match(line)
        if old:
            line = old["message"]
        elif line.startswith(_BACKTRACE_NOTE):
            continue
        if line and not _PANIC_HEADER.match(line):
            return line if len(line) <= _ERROR_LINE_MAX else line[: _ERROR_LINE_MAX - 1] + "…"
    return None


# On Windows Espanso's output never reaches a pipe and a daemon it starts would share our
# console and pipes: espanso_windows answers those calls without reading it (#209-#211).
WINDOWS_ESPANSO = sys.platform == "win32"


def run_command(argv: Sequence[str]) -> str | CommandFailure:
    if WINDOWS_ESPANSO and argv and argv[0] == "espanso":
        from . import espanso_windows

        answer: str | CommandFailure = espanso_windows.run(argv)
        return answer
    # Resolved through PATH (and PATHEXT) first: on Windows `espanso` is `espanso.cmd`, which
    # CreateProcess alone never finds, so an installed Espanso looked missing (#115).
    exe = shutil.which(argv[0])
    if exe is None:
        return CommandFailure()
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [exe, *argv[1:]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Espanso, uv and brew write UTF-8; never the locale's code page, never a crash.
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandFailure(found=True, path=exe, error=_error_line(str(exc)))
    try:
        stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        _stop(proc)
        return CommandFailure(found=True, path=exe, timed_out=True)
    if proc.returncode == 0:
        return stdout
    return CommandFailure(
        found=True, path=exe, returncode=proc.returncode, error=_error_line(stderr or "")
    )


def _stop(proc: subprocess.Popen[str]) -> None:
    """Stop a timed-out command without waiting on what it started. On Windows `espanso` is a
    .cmd whose cmd.exe starts espansod.exe: killing cmd.exe alone leaves the pipes open, and
    subprocess.run would then wait for them forever, so the whole tree is killed."""
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],  # noqa: S607
                capture_output=True,
                timeout=10,
                check=False,
            )
    proc.kill()
    # A grandchild may still hold the pipes: give up on them rather than hang, then close
    # them and reap the killed child, so neither leaks.
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=GIVE_UP_TIMEOUT)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=GIVE_UP_TIMEOUT)


def output(answer: str | CommandFailure | None) -> str | None:
    """A runner's answer as stdout, or None when the command gave none."""
    return answer if isinstance(answer, str) else None


def failure(answer: str | CommandFailure | None) -> CommandFailure | None:
    """Why a runner's answer is not stdout, or None when it is."""
    if isinstance(answer, str):
        return None
    return answer or CommandFailure()


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Launcher -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Launcher:
    path: Path
    channel: str  # uv, homebrew, scoop, winget, script or explicit


def launcher_text(path: Path | str, *, windows: bool = os.name == "nt") -> str:
    """The launcher as written into the match files, or DeployError when it is unsafe there."""
    text = str(path)
    if windows:
        # Espanso YAML uses forward slashes; normalised for safety inside quotes.
        text = text.replace("\\", "/")
    if _UNSAFE.search(text):
        raise DeployError(
            "The CLI path contains a quote, a backslash, a control character, `{{` or one "
            f"of Espanso's %HOME%, %CONFIG% or %PACKAGES%: {path}"
        )
    return text


def _is_stable(path: Path) -> bool:
    """False for a path an upgrade or a removed checkout venv would take away."""
    return not any(part in ("Cellar", ".venv") or _VERSIONED.fullmatch(part) for part in path.parts)


def resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def inside(path: Path, parent: Path) -> bool:
    return resolved(path).is_relative_to(resolved(parent))


# WinGet's package id (#185) and where it puts a portable package: the zip unpacked under
# `Packages\<id>_<source>\`, the command alias in `Links\`. Neither path holds the version,
# so the match files survive `winget upgrade`.
WINGET_ID = "vlastimilbures.PromptMend"


# A release number anywhere in a frozen exe's path (`promptmend-0.22.0-windows-x64`, the
# zip's own name as a folder): the next release would unpack somewhere else.
_RELEASE_IN_PATH = re.compile(r"\d+\.\d+\.\d+")
FROZEN_UNSTABLE = (
    "The promptmend folder is in a temporary or versioned place ({path}): move it to a fixed "
    "place such as %LOCALAPPDATA%\\Programs\\promptmend, or pass --launcher"
)


def _temp_dir() -> Path:
    import tempfile

    return Path(tempfile.gettempdir())


def _frozen_stable(path: Path) -> bool:
    """A frozen exe's path that survives the next release: no release number in any part
    and not under the temp folder (Explorer opens a zip's exe from `Temp1_<zip name>`)."""
    if not _is_stable(path) or any(_RELEASE_IN_PATH.search(part) for part in path.parts):
        return False
    try:
        path.resolve().relative_to(_temp_dir().resolve())
    except (OSError, ValueError):
        return True
    return False


def _winget_roots(environ: Mapping[str, str]) -> list[Path]:
    """WinGet's folders for portable packages: per user, then machine-wide (`--scope
    machine`)."""
    roots = []
    if local := environ.get("LOCALAPPDATA"):
        roots.append(Path(local) / "Microsoft" / "WinGet")
    if program_files := environ.get("ProgramFiles"):
        roots.append(Path(program_files) / "WinGet")
    return roots


def _winget_launcher(executable: Path, environ: Mapping[str, str]) -> Launcher | None:
    """The WinGet install of the running frozen exe: its `Links` alias when that resolves to
    this exe, else the exe itself inside WinGet's package folder for our id."""
    roots = _winget_roots(environ)
    for winget in roots:
        link = winget / "Links" / "promptmend.exe"
        with contextlib.suppress(OSError):
            if os.path.samefile(link, executable):
                return Launcher(link, "winget")
    for winget in roots:
        try:
            inside = executable.resolve().relative_to((winget / "Packages").resolve())
        except (OSError, ValueError):
            continue
        folder = inside.parts[0] if inside.parts else ""
        stable = not any(_RELEASE_IN_PATH.search(part) for part in inside.parts)
        if folder.lower().startswith(f"{WINGET_ID.lower()}_") and _is_stable(inside) and stable:
            return Launcher(executable, "winget")
    return None


def resolve_launcher(
    *,
    runner: Runner | None = None,
    prefix: Path | None = None,
    script: Path | None = None,
    windows: bool = os.name == "nt",
    frozen: bool | None = None,
    executable: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Launcher:
    """The stable entry point of the install that is running now: the channel's own bin
    (uv tool's bin dir, Homebrew's `<prefix>/bin`, Scoop's shim, WinGet's alias), never a
    versioned path such as a Homebrew Cellar, else the running console script when that path
    is stable. A frozen build (the Windows zip, #185) is its own exe: WinGet's, else itself."""
    if getattr(sys, "frozen", False) if frozen is None else frozen:
        exe_path = Path(sys.executable) if executable is None else executable
        if windows:
            found = _winget_launcher(exe_path, os.environ if environ is None else environ)
            if found is not None:
                return found
        if not (exe_path.is_absolute() and exe_path.is_file()):
            raise DeployError(f"The running promptmend.exe was not found ({exe_path})")
        if not _frozen_stable(exe_path):
            raise DeployError(FROZEN_UNSTABLE.format(path=exe_path))
        return Launcher(exe_path, "script")
    runner = runner or run_command
    prefix = Path(sys.prefix) if prefix is None else prefix
    script = Path(sys.argv[0]) if script is None else script
    exe = "promptmend.exe" if windows else "promptmend"

    tool_dir = output(runner(["uv", "tool", "dir"]))
    if tool_dir and inside(prefix, Path(tool_dir.strip())):
        bin_dir = output(runner(["uv", "tool", "dir", "--bin"]))
        if bin_dir and (Path(bin_dir.strip()) / exe).is_file():
            return Launcher(Path(bin_dir.strip()) / exe, "uv")

    # A formula's virtualenv is `<Cellar>/<formula>/<version>/libexec`, reached either by that
    # path or through the `<brew prefix>/opt/<formula>` link to it.
    if "Cellar" in prefix.parts or "opt" in prefix.parts:
        brew = output(runner(["brew", "--prefix"]))
        root = Path(brew.strip()) if brew and brew.strip() else None
        formula = None
        if root is not None and "Cellar" in prefix.parts[:-1]:
            formula = prefix.parts[prefix.parts.index("Cellar") + 1]
        elif root is not None and prefix.is_relative_to(root / "opt") and prefix != root / "opt":
            formula = prefix.relative_to(root / "opt").parts[0]
        if root is not None and formula is not None:
            for candidate in (root / "bin" / exe, root / "opt" / formula / "bin" / exe):
                if candidate.is_file():
                    return Launcher(candidate, "homebrew")

    lowered = [part.lower() for part in prefix.parts]
    if windows and "apps" in lowered and "scoop" in lowered[: lowered.index("apps")]:
        shim = Path(*prefix.parts[: lowered.index("apps")]) / "shims" / exe
        if shim.is_file():
            return Launcher(shim, "scoop")

    for candidate in (script, script.with_name(exe)):
        if candidate.is_absolute() and candidate.is_file() and candidate.name == exe:
            if _is_stable(candidate):
                return Launcher(candidate, "script")
            break
    raise DeployError(
        "Could not find a stable promptmend launcher (install it with `uv tool install` "
        "or Homebrew, or pass --launcher)"
    )


# --- Espanso folder -----------------------------------------------------------------------


def default_espanso_dir(
    environ: Mapping[str, str] = os.environ, *, platform: str = sys.platform
) -> Path:
    """Espanso's own default config folder, used when `espanso path config` cannot answer."""
    if platform == "win32":
        appdata = environ.get("APPDATA")
        return Path(appdata or Path.home() / "AppData" / "Roaming") / "espanso"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "espanso"
    return Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "espanso"


# 'espanso path config' prints just the config dir (read-only), unlike 'espanso path'.
PATH_CONFIG = ("espanso", "path", "config")


@dataclass(frozen=True)
class EspansoDir:
    """Espanso's config folder, and why it is the default one when Espanso could not say."""

    path: Path
    fallback: str | None = None


def locate_espanso_dir(runner: Runner | None = None) -> EspansoDir:
    answer = (runner or run_command)(PATH_CONFIG)
    out = output(answer)
    if out and out.strip():
        return EspansoDir(Path(out.strip()))
    why = failure(answer)
    reason = why.describe(PATH_CONFIG) if why else "`espanso path config` printed nothing"
    return EspansoDir(default_espanso_dir(), reason)


def espanso_dir(runner: Runner | None = None) -> Path:
    return locate_espanso_dir(runner).path


def restart_espanso(runner: Runner | None = None) -> bool:
    """`espanso restart`, else `espanso start` (restart fails when Espanso is not running)."""
    runner = runner or run_command
    return any(output(runner(["espanso", verb])) is not None for verb in ("restart", "start"))


# --- Manifest -----------------------------------------------------------------------------


@dataclass
class Entry:
    """One deployed file as we last wrote it."""

    target: str
    asset_version: str
    digest: str
    launcher: str
    calls_cli: bool
    backups: list[str] = field(default_factory=list)


_ENTRY_TYPES = {
    "target": str,
    "asset_version": str,
    "digest": str,
    "launcher": str,
    "calls_cli": bool,
    "backups": list,
}


def _entry_problem(item: object) -> str | None:
    """Why a manifest entry is not one we wrote, or None. Checked field by field, since a
    path in it is later written, read or deleted."""
    if not isinstance(item, dict):
        return "an entry is not an object"
    if set(item) != set(_ENTRY_TYPES):
        return f"an entry has the fields {sorted(item)}"
    for key, kind in _ENTRY_TYPES.items():
        if not isinstance(item[key], kind):
            return f"{key} is not a {kind.__name__}"
    if not all(isinstance(b, str) for b in item["backups"]):
        return "backups is not a list of paths"
    return None


@dataclass
class Manifest:
    path: Path
    entries: dict[str, Entry] = field(default_factory=dict)  # keyed by target path

    @classmethod
    def load(cls, path: Path | None = None) -> Manifest:
        path = path or user_data_dir() / MANIFEST_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError) as exc:
            raise DeployError(f"The deploy manifest {path} cannot be read: {exc}") from None
        if not isinstance(raw, dict) or raw.get("format") != MANIFEST_FORMAT:
            raise DeployError(f"The deploy manifest {path} has an unknown format")
        files = raw.get("files", [])
        if not isinstance(files, list):
            raise DeployError(f"The deploy manifest {path} is damaged: files is not a list")
        entries = {}
        for item in files:
            problem = _entry_problem(item)
            if problem:
                raise DeployError(f"The deploy manifest {path} is damaged: {problem}")
            entries[item["target"]] = Entry(**item)
        return cls(path, entries)

    def save(self) -> None:
        if not self.entries:
            self.path.unlink(missing_ok=True)
            return
        files = [vars(self.entries[key]) for key in sorted(self.entries)]
        data = json.dumps({"format": MANIFEST_FORMAT, "files": files}, indent=2) + "\n"
        _write(self.path, data)


def is_gone(entry: Entry) -> bool:
    """True when an entry's target no longer exists, not even as a dangling link: its folder
    was deleted or Espanso moved to another config folder. Such an entry only records history.
    A target that cannot be looked at (an unreadable or privacy-guarded folder) is not gone."""
    try:
        os.lstat(entry.target)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


# Not config_files.write_atomic(): that one writes through a symlink (#236), this must replace it.
def _write(path: Path, text: str) -> None:
    """Write UTF-8 with the text's own newlines, through a new temp file in the same folder
    and an atomic rename. mkstemp creates the temp file exclusively, so a planted link there
    is never followed, and the rename replaces a link at ``path`` instead of writing through it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        return ""  # not text we wrote: never equal to ours


# --- Plan ---------------------------------------------------------------------------------


def render(source: str, launcher: str, version: str | None = None) -> str:
    """A deployed match file: the version stamp (#25), then the shipped file with the
    launcher in place of the placeholder (literally, so no character in it is special)."""
    return STAMP.format(version=version or __version__) + source.replace(PLACEHOLDER, launcher)


@dataclass
class FileStep:
    name: str
    target: Path
    state: str
    current: str | None
    rendered: str
    calls_cli: bool
    entry: Entry | None

    def diff(self) -> str:
        """A unified diff from what is on disk to what deploy would write."""
        lines = difflib.unified_diff(
            (self.current or "").splitlines(keepends=True),
            self.rendered.splitlines(keepends=True),
            fromfile=f"{self.target} ({'missing' if self.current is None else 'on disk'})",
            tofile=f"{self.target} (promptmend {__version__})",
        )
        return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


@dataclass
class Plan:
    espanso_dir: Path
    launcher: str
    steps: list[FileStep]
    manifest: Manifest
    # Manifest entries outside this plan whose file is gone: apply forgets them.
    orphans: list[str] = field(default_factory=list)
    # The other match files in the folder (Espanso's base.yml, the user's overlays): listed
    # by status only (#38); deploy and detach never touch them.
    yours: list[Path] = field(default_factory=list)

    @property
    def _files_in_sync(self) -> bool:
        return all(
            s.state == IN_SYNC and s.entry is not None and s.entry.digest == _digest(s.rendered)
            for s in self.steps
        )

    @property
    def is_noop(self) -> bool:
        return self._files_in_sync and not self.orphans

    @property
    def only_forgets(self) -> bool:
        """Every file is in sync and the only work is forgetting gone entries: apply then
        touches no file, only the manifest, so it needs no question."""
        return self._files_in_sync and bool(self.orphans)

    @property
    def conflicts(self) -> list[FileStep]:
        return [s for s in self.steps if s.state in (MODIFIED, FOREIGN)]


def launchers_in(text: str) -> set[str]:
    """The launcher paths a match file calls: each script var's first arg, or the quoted
    path of a shell cmd line (every release before #18 quoted it the same way). Windows
    paths have forward slashes."""
    return {*_SCRIPT_LAUNCHER.findall(text), *_QUOTED_LAUNCHER.findall(text)}


def deployed_launchers(espanso: Path) -> set[str]:
    """The launchers the packaged match files in ``espanso``'s match folder call, whoever
    deployed them (an install script, a deploy, a hand copy). Reads only; an unreadable or
    missing file adds nothing."""
    found: set[str] = set()
    for name in assets.match_names():
        try:
            found |= launchers_in((espanso / "match" / name).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return found


def _released_rendering(name: str, current: str, launchers: Sequence[str]) -> bool:
    """True when ``current`` is exactly what an install script or an earlier deploy wrote from
    a source some release shipped (match_history): its stamp, if any, dropped and the
    launcher it holds put back as the placeholder. Any edit changes the digest."""
    known = KNOWN_SOURCES.get(name, frozenset())
    # Before v0.11 (.gitattributes eol=lf) a Windows checkout held CRLF sources, which the
    # Windows script copied byte for byte; the digests are of the LF sources.
    body = _STAMP_LINE.sub("", current.removeprefix("\ufeff").replace("\r\n", "\n"), count=1)
    candidates = {*launchers_in(body), *launchers}
    for launcher in [None, *sorted(candidates)]:
        source = body if launcher is None else body.replace(launcher, PLACEHOLDER)
        if hashlib.sha256(source.encode("utf-8")).hexdigest() in known:
            return True
    return False


def _state(
    name: str, target: Path, current: str | None, rendered: str, entry: Entry | None, launcher: str
) -> str:
    if current is None:
        return MISSING
    if target.is_symlink():
        return FOREIGN  # the user's own link (a dotfiles repo): never replaced unasked
    if current == rendered:
        return IN_SYNC
    if entry is not None and _digest(current) == entry.digest:
        return STALE
    # No entry, or one that no longer matches: still ours if it is a released rendering
    # (an older install script wrote it, or the manifest was lost).
    if _released_rendering(name, current, [launcher]):
        return STALE
    return MODIFIED if entry is not None else FOREIGN


def _yours(match_dir: Path) -> list[Path]:
    """The ``.yml``/``.yaml`` files (or links to one) directly in ``match_dir`` that are not
    ours: no packaged name. Our backups and
    side-by-side copies end in another suffix, so Espanso never loads them and neither are
    listed; subfolders (packages/) are skipped. Names only, nothing is opened or followed;
    an unreadable folder lists nothing."""
    ours = set(assets.match_names())
    found = []
    try:
        children = sorted(match_dir.iterdir())
    except OSError:
        return []
    for path in children:
        if path.name in ours or path.suffix.lower() not in (".yml", ".yaml"):
            continue
        try:
            if path.is_symlink() or path.is_file():  # a link is listed, never followed
                found.append(path)
        except OSError:
            continue
    return found


def plan(espanso: Path, launcher: str, manifest: Manifest) -> Plan:
    match_dir = espanso / "match"
    steps = []
    for name in assets.match_names():
        source = assets.read_match(name)
        target = match_dir / name
        rendered = render(source, launcher)
        current = _read(target)
        entry = manifest.entries.get(str(target))
        steps.append(
            FileStep(
                name,
                target,
                _state(name, target, current, rendered, entry, launcher),
                current,
                rendered,
                PLACEHOLDER in source,
                entry,
            )
        )
    planned = {str(s.target) for s in steps}
    orphans = sorted(
        key for key, e in manifest.entries.items() if key not in planned and is_gone(e)
    )
    return Plan(espanso, launcher, steps, manifest, orphans, _yours(match_dir))


# --- Apply --------------------------------------------------------------------------------


@dataclass
class Outcome:
    """What apply or detach did, one line per file, for the command to print."""

    lines: list[str] = field(default_factory=list)
    changed: bool = False
    # Files left as the user has them, so not up to date: the command warns about each.
    kept: list[Path] = field(default_factory=list)

    def add(self, line: str, *, changed: bool = True) -> None:
        self.lines.append(line)
        self.changed = self.changed or changed


def _stamp() -> str:
    return time.strftime("%Y%m%d%H%M%S")


def _backup(path: Path, stamp: str) -> Path:
    """Copy ``path`` to a new `<name>.bak-<stamp>[-n]` next to it, never through a link."""
    data = path.read_bytes()
    for n in range(1000):
        backup = path.with_name(f"{path.name}.bak-{stamp}" + (f"-{n}" if n else ""))
        # Windows follows a dangling link on exclusive create, writing outside the folder.
        if backup.is_symlink():
            continue
        try:
            with backup.open("xb") as out:  # exclusive: an existing file is never replaced
                out.write(data)
        except FileExistsError:
            continue  # two deploys within one second
        return backup
    raise DeployError(f"Could not find a free backup name for {path}")


def _is_our_backup(path: Path, target: Path) -> bool:
    """A backup _backup() made of ``target``: in its folder and named exactly like one."""
    pattern = re.escape(target.name) + r"\.bak-\d{14}(-\d+)?"
    try:
        same_dir = path.parent.resolve() == target.parent.resolve()
    except OSError:
        return False
    return same_dir and bool(re.fullmatch(pattern, path.name)) and not path.is_symlink()


def _prune(backups: list[str], target: Path) -> list[str]:
    """Delete all but the newest KEEP_BACKUPS of our own backups of ``target`` (listed oldest
    first). A listed path that is not one is dropped from the list, never deleted."""
    ours = [b for b in backups if _is_our_backup(Path(b), target)]
    for old in ours[:-KEEP_BACKUPS]:
        Path(old).unlink(missing_ok=True)
    return ours[-KEEP_BACKUPS:]


def apply(the_plan: Plan, choices: Mapping[str, str] | None = None) -> Outcome:
    """Carry out a plan. ``choices`` maps a conflicting file's name to keep, ours or side;
    a conflict without a choice keeps the user's file."""
    choices = choices or {}
    outcome = Outcome()
    stamp = _stamp()
    manifest = the_plan.manifest
    match_dir = the_plan.espanso_dir / "match"

    for step in the_plan.steps:
        # Every write lands in Espanso's match/ folder; config/ is never touched (#37).
        if step.target.parent != match_dir:
            raise DeployError(f"Refusing to write outside {match_dir}: {step.target}")
        backups = list(step.entry.backups) if step.entry else []
        if step.state in (MODIFIED, FOREIGN):
            choice = choices.get(step.name, KEEP)
            if choice not in CHOICES:
                raise DeployError(f"Unknown choice {choice!r} for {step.name}")
            if choice == KEEP:
                outcome.add(f"kept your {step.state} {step.target}", changed=False)
                outcome.kept.append(step.target)
                continue
            if choice == SIDE:
                side = step.target.with_name(step.target.name + SIDE_SUFFIX)
                _write(side, step.rendered)
                outcome.add(f"wrote ours next to your {step.target.name}: {side}", changed=False)
                outcome.kept.append(step.target)
                continue
            backups.append(str(_backup(step.target, stamp)))
            outcome.add(f"replaced {step.target} (yours backed up to {Path(backups[-1]).name})")
        elif step.state == IN_SYNC:
            if step.entry is not None and step.entry.digest == _digest(step.rendered):
                continue
            outcome.add(f"adopted {step.target} (already up to date)", changed=False)
        else:
            outcome.add(f"{'wrote' if step.state == MISSING else 'updated'} {step.target}")

        if step.state != IN_SYNC:
            _write(step.target, step.rendered)
        manifest.entries[str(step.target)] = Entry(
            target=str(step.target),
            asset_version=__version__,
            digest=_digest(step.rendered),
            launcher=the_plan.launcher,
            calls_cli=step.calls_cli,
            backups=_prune(backups, step.target),
        )
    for key in the_plan.orphans:
        entry = manifest.entries.get(key)
        if entry is not None and is_gone(entry):  # checked again: never forget a live file
            del manifest.entries[key]
            outcome.add(f"forgot {entry.target} (already gone)", changed=False)
    manifest.save()
    return outcome


# --- Detach -------------------------------------------------------------------------------


def detach(manifest: Manifest, espanso: Path, *, remove_all: bool = False) -> Outcome:
    """Remove the files we own. --keep-static (the default, D-UNI-1) removes only the files
    that call the CLI, so -risk- stays as a plain static snippet; --remove-all removes
    every owned file. A file the user edited since our last deploy is kept and reported, and
    our backups (the user's earlier versions) are never deleted here. Only entries for one of
    our match files in ``espanso``'s match/ folder are acted on; any other is reported."""
    outcome = Outcome()
    match_dir = (espanso / "match").resolve()
    names = set(assets.match_names())
    for key, entry in sorted(manifest.entries.items()):
        target = Path(entry.target)
        if target.name not in names or target.parent.resolve() != match_dir:
            outcome.add(f"left {target} alone: not one of ours in {match_dir}", changed=False)
            continue
        if target.is_symlink():
            outcome.add(f"left {target} alone: it is a link", changed=False)
            continue
        current = _read(target)
        if current is None:
            del manifest.entries[key]
            outcome.add(f"forgot {target} (already gone)", changed=False)
        elif _digest(current) != entry.digest:
            outcome.add(f"kept {target}: edited since promptmend deployed it", changed=False)
        elif remove_all or entry.calls_cli:
            target.unlink()
            del manifest.entries[key]
            outcome.add(f"removed {target}")
        else:
            outcome.add(f"kept static snippets in {target}", changed=False)
    manifest.save()
    return outcome
