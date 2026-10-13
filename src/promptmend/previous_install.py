"""Find an earlier checkout (editable) install whose settings this install does not see (#110).

A wheel or channel install reads no checkout `.env`, so after a switch the settings look gone.
Detection only looks where the old install left a trace: the launcher in the deployed match
files, the deploy manifest, the uv tool receipt, and a path the user typed. It never guesses and
never scans the disk, and it runs no command `doctor` does not run already. A `.env` is only
checked for existence here, never read.

Used by the management commands (setup, doctor, the interface); never on the trigger path.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from . import config, config_files, deploy
from .deploy import inside
from .deploy import resolved as resolved  # config_store uses previous_install.resolved
from .profiles import prompts_path

PROJECT_NAME = "promptmend"
# The project's and the command's names before the rename (#169): an earlier checkout has them.
PROJECT_NAMES = (PROJECT_NAME, "espanso-prompt-rewriter")
COMMANDS = ("promptmend", deploy.LEGACY_COMMAND)
SKIP_FILE = "previous-install.json"
SKIP_VERSION = 1

# Candidate.signals values, strongest first.
LAUNCHER, MANIFEST, RECEIPT, ENTERED = "launcher", "manifest", "receipt", "entered"
SIGNALS = (LAUNCHER, MANIFEST, RECEIPT, ENTERED)

# Detection.gated values: why nothing is looked for (D-MIG-4).
LEGACY, SAVED, SECRETS = "legacy", "saved", "secrets"


@dataclass(frozen=True)
class Candidate:
    """A checkout of this project that an earlier install ran from."""

    root: Path
    signals: frozenset[str]
    env_file: Path | None  # the checkout's .env, when one exists (never read here)
    launchers_in_root: tuple[str, ...]  # deployed launchers that still point inside it
    profiles_dir: Path | None


@dataclass(frozen=True)
class Shadow:
    """The `promptmend` the shell finds first is not this install's launcher."""

    path: str
    launcher: str
    hint: str
    command: str = "promptmend"  # or the alias `prompt-workflow` (#169)


@dataclass(frozen=True)
class CopyRecord:
    """A checkout whose settings a copy-mode migration copied while its .env stayed in place:
    the redeploy and the retire step are still to come."""

    root: Path
    env_file: Path
    launchers_in_root: tuple[str, ...]  # deployed launchers that still point inside it


@dataclass(frozen=True)
class Detection:
    candidates: tuple[Candidate, ...] = ()
    gated: str | None = None
    shadow: Shadow | None = None
    # Read from migration.json whatever the gate says: the copy itself closes the gate.
    pending: CopyRecord | None = None


def gate(environ: Mapping[str, str]) -> str | None:
    """Why detection does not run, or None: legacy mode keeps its own .env, and once
    config.toml or the secret store exists the user's settings are no longer missing. A deploy
    manifest does not count: an editable install had one before the switch."""
    if config.env_file_override(environ):
        return LEGACY
    if config_files.is_file(config.settings_file(environ)):
        return SAVED
    # The file store's path; a keyring store (config_files.secret_store's TODO) needs its own test.
    if config_files.is_file(config._user_config_dir(environ) / config_files.SECRETS_FILE):
        return SECRETS
    return None


def checkout_root_of(launcher: str) -> PurePath | None:
    """The checkout a launcher belongs to: only `<root>/.venv/bin/promptmend` or
    `<root>\\.venv\\Scripts\\promptmend.exe` (deployed with forward slashes), or the same
    with the pre-rename `prompt-workflow` (#169), never a walk upward, so a uv, Homebrew or
    Scoop launcher gives none."""
    windows = PureWindowsPath(launcher)
    if windows.drive:
        tail = tuple(p.lower() for p in windows.parts[-3:])
        if tail in {(".venv", "scripts", f"{name}.exe") for name in COMMANDS}:
            return windows.parents[2]
        return None
    posix = PurePosixPath(launcher)
    if posix.is_absolute() and posix.parts[-3:] in {(".venv", "bin", name) for name in COMMANDS}:
        return posix.parents[2]
    return None


def _project_name(root: Path) -> str | None:
    import tomllib

    try:
        table = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = table.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    return name if isinstance(name, str) else None


def is_checkout(root: Path) -> bool:
    """``root`` holds a pyproject.toml naming this project (by its new or its old name)."""
    return config_files.is_file(root / "pyproject.toml") and _project_name(root) in PROJECT_NAMES


def is_running_checkout(root: Path) -> bool:
    # An editable install that reads its own .env is `config migrate`'s case (#84).
    return resolved(root) == resolved(config._PROJECT_ROOT)


def receipt_root(runner: deploy.Runner) -> Path | None:
    """The source path uv recorded for an editable (or directory) tool install, from
    `<uv tool dir>/promptmend/uv-receipt.toml`, or the pre-rename
    `<uv tool dir>/espanso-prompt-rewriter/uv-receipt.toml`. A wheel URL gives none, and so
    does a receipt that `uv tool install --force` of a wheel has overwritten."""
    import tomllib

    tool_dir = deploy.output(runner(["uv", "tool", "dir"]))
    if not tool_dir or not tool_dir.strip():
        return None
    for name in PROJECT_NAMES:
        receipt = Path(tool_dir.strip()) / name / "uv-receipt.toml"
        try:
            table = tomllib.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        tool = table.get("tool")
        requirements = tool.get("requirements") if isinstance(tool, dict) else None
        for item in requirements if isinstance(requirements, list) else []:
            if not isinstance(item, dict) or item.get("name") != name:
                continue
            for key in ("editable", "directory"):
                source = item.get(key)
                if isinstance(source, str) and Path(source).is_absolute():
                    return Path(source)
    return None


# --- Skip marker (D-MIG-3) ----------------------------------------------------------------


def skip_file(environ: Mapping[str, str] | None = None) -> Path:
    return config.user_data_dir(os.environ if environ is None else environ) / SKIP_FILE


def _skipped(environ: Mapping[str, str] | None) -> dict[str, str]:
    """Each skipped root and when it was skipped; a missing or damaged marker counts as empty,
    so the offer comes back rather than any command failing."""
    try:
        raw = json.loads(skip_file(environ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = raw.get("skipped") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return {}
    return {
        i["root"]: i["at"] if isinstance(i.get("at"), str) else ""
        for i in items
        if isinstance(i, dict) and isinstance(i.get("root"), str)
    }


def skipped_roots(environ: Mapping[str, str] | None = None) -> set[str]:
    """The roots the user chose to skip, as detect() names them (resolved)."""
    return set(_skipped(environ))


def skip(root: Path, environ: Mapping[str, str] | None = None) -> None:
    """Remember that the user skipped ``root``; a different checkout found later is offered."""
    skipped = _skipped(environ)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    skipped.setdefault(str(resolved(root.expanduser())), now)
    items = [{"root": r, "at": at} for r, at in sorted(skipped.items())]
    record = {"version": SKIP_VERSION, "skipped": items}
    path = skip_file(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    config_files.write_atomic(path, data, private=False)


# --- Shadowed CLI -------------------------------------------------------------------------


def shadow(
    environ: Mapping[str, str],
    runner: deploy.Runner,
    launcher: Path | None = None,
    *,
    look_up: bool = True,
) -> Shadow | None:
    """The `promptmend` first on PATH, or its alias `prompt-workflow` (#169), when it is not
    this install's own: the shell (and anything started from it) then runs another CLI with
    other settings. ``launcher`` is the one to compare with (the alias sits next to it);
    without it, it is looked up unless ``look_up`` is False (the caller already found none)."""
    path = environ.get("PATH")
    found = {command: shutil.which(command, path=path) for command in COMMANDS}
    if not any(found.values()):
        return None
    if launcher is None:
        if not look_up:
            return None
        try:
            launcher = deploy.resolve_launcher(runner=runner).path
        except deploy.DeployError:
            return None
    for command, where in found.items():
        if not where:
            continue
        own = launcher.with_name(command + launcher.suffix)
        try:
            if os.path.samefile(where, own):
                continue
        except OSError:
            pass
        return Shadow(where, str(own), _shadow_hint(environ, Path(where), own), command)
    return None


def _shadow_hint(environ: Mapping[str, str], found: Path, launcher: Path) -> str:
    venv = environ.get("VIRTUAL_ENV")
    if venv and inside(found, Path(venv)):
        return "a virtual environment is active: run `deactivate`"
    if ".venv" in found.parts:
        return f"remove {found.parent} from PATH"
    return f"uninstall the old CLI, or put {launcher.parent} earlier on PATH"


# --- Detection ----------------------------------------------------------------------------


def copied_env(environ: Mapping[str, str]) -> tuple[Path, Path] | None:
    """The (root, .env) a copy-mode migration copied and nobody retired yet, while that .env
    is still there; None otherwise, also for a missing or damaged migration.json."""
    from .config_store import MARKER_FILE, retired

    try:
        raw = json.loads((config._user_config_dir(environ) / MARKER_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or raw.get("mode") != "copy" or not isinstance(sources, list):
        return None
    for entry in sources:
        if not isinstance(entry, dict) or not entry.get("copied") or retired(entry):
            continue
        root, env_file = entry.get("root"), entry.get("from")
        if not isinstance(root, str) or not isinstance(env_file, str):
            continue
        if config_files.is_file(Path(env_file)):
            return resolved(Path(root)), Path(env_file)
    return None


def launchers_inside(launchers: set[str], root: Path) -> tuple[str, ...]:
    """The ``launchers`` that belong to the checkout at (resolved) ``root``."""
    return tuple(
        sorted(
            deployed
            for deployed in launchers
            if (owner := checkout_root_of(deployed)) is not None and resolved(Path(owner)) == root
        )
    )


def manifest_launchers() -> set[str]:
    try:
        manifest = deploy.Manifest.load()
    except deploy.DeployError:
        return set()
    return {e.launcher for e in manifest.entries.values() if not deploy.is_gone(e)}


def best(candidates: tuple[Candidate, ...]) -> Candidate | None:
    """The candidate to offer first: the one the user entered, else the first with a .env."""
    ranked = sorted(candidates, key=lambda c: (ENTERED not in c.signals, c.env_file is None))
    return ranked[0] if ranked else None


def detect(
    environ: Mapping[str, str] | None = None,
    *,
    runner: deploy.Runner | None = None,
    espanso_dir: Path | None = None,
    entered: Path | None = None,
    launcher: Path | None = None,
    look_up_launcher: bool = True,
) -> Detection:
    """Every earlier checkout install the signals name, unless the gate is closed. A root the
    user skipped is left out unless they entered it. ``launcher`` (and ``look_up_launcher``)
    go to shadow(). Reads files; runs only commands doctor runs too, through ``runner``:
    `espanso path config` (when ``espanso_dir`` is not given), `uv tool dir`, and the
    launcher lookup (`uv tool dir --bin`, `brew --prefix`) when no ``launcher`` is given."""
    env = os.environ if environ is None else environ
    run = runner or deploy.run_command
    found_shadow = shadow(env, run, launcher, look_up=look_up_launcher)
    copied = copied_env(env)
    why = gate(env)
    if why and copied is None:
        return Detection(gated=why, shadow=found_shadow)

    espanso = espanso_dir or deploy.espanso_dir(run)
    in_files = deploy.deployed_launchers(espanso)
    in_manifest = manifest_launchers()
    launchers = in_files | in_manifest
    pending = None
    if copied is not None:
        pending = CopyRecord(copied[0], copied[1], launchers_inside(launchers, copied[0]))
    if why:
        return Detection(gated=why, shadow=found_shadow, pending=pending)
    signals: dict[Path, set[str]] = {}

    def add(root: PurePath | Path | None, signal: str) -> None:
        if root is not None:
            signals.setdefault(resolved(Path(root)), set()).add(signal)

    for deployed in sorted(in_files):
        add(checkout_root_of(deployed), LAUNCHER)
    for deployed in sorted(in_manifest):
        add(checkout_root_of(deployed), MANIFEST)
    add(receipt_root(run), RECEIPT)
    if entered is not None:
        add(entered.expanduser(), ENTERED)

    skipped = skipped_roots(env)
    candidates = []
    for root, kinds in sorted(signals.items()):
        if not is_checkout(root) or is_running_checkout(root):
            continue
        if str(root) in skipped and ENTERED not in kinds:
            continue
        inside = launchers_inside(launchers, root)
        env_file = root / ".env"
        profiles = root / prompts_path(root)
        candidates.append(
            Candidate(
                root=root,
                signals=frozenset(kinds),
                env_file=env_file if config_files.is_file(env_file) else None,
                launchers_in_root=inside,
                # os.path.isdir never raises (Path.is_dir does on EACCES before 3.14).
                profiles_dir=profiles if os.path.isdir(profiles) else None,
            )
        )
    return Detection(candidates=tuple(candidates), shadow=found_shadow, pending=pending)
