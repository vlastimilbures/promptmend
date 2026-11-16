"""Whether a newer release exists, for `doctor` and the interface's Home, never for a
trigger (tests/test_trigger_contract.py).

One GET of PyPI's JSON for promptmend, at most once a day: the answer is cached in
user_data_dir()/update-check.json. A failed request is cached too, as ``"latest": null``, and
asked again only after RETRY_AFTER. PROMPT_UPDATE_CHECK=false makes no request and reads no
file. Every failure (network, a bad reply, a damaged cache) is ``unknown``; nothing raises.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, config_files, installed_version

URL = "https://pypi.org/pypi/promptmend/json"
SETTING = "PROMPT_UPDATE_CHECK"
# The whole request, connect to last byte; httpx's own timeout is per phase.
TIMEOUT = 3.0
MAX_AGE = timedelta(hours=24)
# A failed request is not repeated sooner than this.
RETRY_AFTER = timedelta(hours=1)
CACHE_FILE = "update-check.json"
LATEST, AVAILABLE, UNKNOWN, OFF = "latest", "available", "unknown", "off"
# Only the interface's, while the check still runs beside the rest of a reload: doctor
# and check() never answer it.
CHECKING = "checking"
# A final release only: X.Y.Z, no pre, dev, rc, post or local suffix.
_RELEASE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
# What makes an installed X.Y.Z older than the final X.Y.Z (PEP 440 pre and dev releases).
_PRE = re.compile(r"[-_.]?(a|b|c|rc|alpha|beta|pre|preview|dev)", re.IGNORECASE)
_UV = (
    "uv tool install --force promptmend -c "
    "https://github.com/vlastimilbures/promptmend/releases/latest/download/constraints.txt"
)
# On Windows the update asks uv for Python 3.13 or later, as install.ps1 and
# install_windows.ps1 do: a --force install picks its Python again, and on Windows 3.12 the
# interface's background threads starve (#348). The quotes keep PowerShell from reading `>`
# as a redirection.
_UV_WIN32 = _UV.replace("--force ", '--force --python ">=3.13" ', 1)
# The Windows one-command install (docs/install.md, README); its default channel installs the
# frozen exe, and running it again updates it in place.
INSTALL_PS1 = (
    'powershell -ExecutionPolicy ByPass -c "irm '
    'https://github.com/vlastimilbures/promptmend/releases/latest/download/install.ps1 | iex"'
)
# Install channel (doctor's install check) -> the command that updates it (docs/install.md,
# "Update"). uv's own `uv tool upgrade` keeps the old constraints and a Release wheel's URL.
# The `script` channel is any other console script (pipx, pip, a venv): no one command.
UPGRADE_COMMANDS = {
    "uv": _UV,
    "homebrew": "brew update && brew upgrade promptmend",
    # The frozen Windows build, installed as a portable WinGet package.
    "winget": "winget upgrade vlastimilbures.PromptMend",
    # The frozen Windows build in install.ps1's folder: the install command again.
    "installer": INSTALL_PS1,
}
# A checkout (CONTRIBUTING.md, "Set up"): its install script syncs the tool with uv.lock.
_CHECKOUT = {
    "darwin": "git pull, then ./scripts/install_macos.sh",
    "win32": r"git pull, then .\scripts\install_windows.ps1",
}
_CHECKOUT_ELSE = "git pull, then reinstall as CONTRIBUTING.md says"
# The last answer this process got (latest, checked_at), so a cache that cannot be written
# never makes a reload ask again.
_last: tuple[str | None, str] | None = None


@dataclass(frozen=True)
class UpdateStatus:
    """``state`` is latest, available, unknown or off (checking only in the interface);
    ``latest`` the newest release PyPI lists and ``checked_at`` when it was asked (ISO-8601
    UTC), when known."""

    state: str = UNKNOWN
    latest: str | None = None
    checked_at: str | None = None


def upgrade_command(channel: str, platform: str = sys.platform) -> str:
    """How to update an install of ``channel`` (doctor's install check)."""
    if channel == "editable":
        return _CHECKOUT.get(platform, _CHECKOUT_ELSE)
    if channel == "uv" and platform == "win32":
        return _UV_WIN32
    return UPGRADE_COMMANDS.get(channel, "see docs/install.md")


def _release(text: object) -> tuple[int, int, int] | None:
    match = _RELEASE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _installed() -> tuple[int, int, int, int] | None:
    """The running version as numbers: its leading X.Y.Z, then 0 for a pre or dev release
    (older than the final X.Y.Z) and 1 otherwise (a post release or a local build)."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)(.*)", installed_version())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups()[:3])
    return major, minor, patch, 0 if _PRE.match(match.group(4)) else 1


def newest(releases: Any) -> str | None:
    """The newest final release in PyPI's ``releases`` map whose files are not all yanked."""
    found: list[tuple[tuple[int, int, int], str]] = []
    for name, files in releases.items():
        number = _release(name)
        if number is None or not isinstance(files, list) or not files:
            continue
        if all(isinstance(f, dict) and f.get("yanked") for f in files):
            continue
        found.append((number, name))
    return max(found)[1] if found else None


def _fetch() -> str:
    """Ask PyPI for the newest release; raises on any failure. conftest replaces it, so no
    test reaches pypi.org."""
    import httpx

    headers = {"User-Agent": f"promptmend/{installed_version()}"}
    # Per phase (connect, read, ...); _within() bounds the whole call.
    with httpx.Client(timeout=httpx.Timeout(TIMEOUT)) as client:
        response = client.get(URL, headers=headers)
    response.raise_for_status()
    data = response.json()
    releases = data.get("releases")
    if isinstance(releases, dict):
        latest = newest(releases)
    else:  # an answer without the release list: the project's current version
        version = data["info"]["version"]
        latest = version if _release(version) else None
    if latest is None:
        raise ValueError("no release listed")
    return latest


def _within(seconds: float) -> str:
    """_fetch() in a daemon thread, given up after ``seconds``: a slow server can never hold
    doctor or the interface longer, whatever httpx's per-phase timeouts allow."""
    outcome: list[str | BaseException] = []

    def run() -> None:
        try:
            outcome.append(_fetch())
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run, name="promptmend-update-check", daemon=True)
    worker.start()
    worker.join(seconds)
    if not outcome:
        raise TimeoutError("pypi.org did not answer in time")
    if isinstance(outcome[0], BaseException):
        raise outcome[0]
    return outcome[0]


def cache_path() -> Path:
    return config.user_data_dir() / CACHE_FILE


def _fresh(latest: object, checked_at: object, now: datetime) -> bool:
    """Whether a cached answer still holds: a release for MAX_AGE, a failure (None) for
    RETRY_AFTER. A bad value or a time in the future (a clock set back) does not."""
    if latest is not None and _release(latest) is None:
        return False
    if not isinstance(checked_at, str):
        return False
    try:
        when = datetime.fromisoformat(checked_at)
        age = now - when
    except (ValueError, TypeError):  # not a time, or one without a time zone
        return False
    return timedelta(0) <= age < (MAX_AGE if latest is not None else RETRY_AFTER)


def _cached(now: datetime) -> tuple[str | None, str] | None:
    """The answer this process or the cache file holds while it is fresh."""
    if _last is not None and _fresh(*_last, now):
        return _last
    try:
        data = json.loads(cache_path().read_text("utf-8"))
        latest, checked_at = data["latest"], data["checked_at"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return (latest, checked_at) if _fresh(latest, checked_at, now) else None


def _remember(latest: str | None, checked_at: str) -> None:
    global _last
    _last = (latest, checked_at)
    try:
        body = json.dumps({"checked_at": checked_at, "latest": latest}) + "\n"
        config_files.write_atomic(cache_path(), body.encode("utf-8"), private=False)
    except Exception:  # noqa: S110 - _last keeps this process from asking again
        pass


def _status(latest: str | None, checked_at: str) -> UpdateStatus:
    installed = _installed()
    number = _release(latest)
    if latest is None or installed is None or number is None:
        return UpdateStatus(UNKNOWN, None, checked_at)
    # An install newer than PyPI (an editable dev build) is up to date; a pre-release of
    # the listed version is not.
    state = AVAILABLE if (*number, 1) > installed else LATEST
    return UpdateStatus(state, latest, checked_at)


def check(cfg: config.Settings, now: datetime | None = None) -> UpdateStatus:
    """Whether a newer release exists; never raises. Off: no request and no file read."""
    return _check(cfg.update_check, now)


def _check(on: bool, now: datetime | None) -> UpdateStatus:
    if not on:
        return UpdateStatus(OFF)
    try:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        cached = _cached(now)
        if cached is not None:
            return _status(*cached)
        checked_at = now.isoformat(timespec="seconds")
        try:
            latest: str | None = _within(TIMEOUT)
        except Exception:
            latest = None
        _remember(latest, checked_at)
        return _status(latest, checked_at)
    except Exception:
        return UpdateStatus(UNKNOWN)


def check_configured(now: datetime | None = None) -> UpdateStatus:
    """check() for the saved settings, failing closed: when PROMPT_UPDATE_CHECK itself was
    rejected, or a settings file that could change it cannot be read, nothing is asked
    (unknown), since the user may have turned it off there. Never raises."""
    try:
        layers = config.ConfigLayers.resolve(only=SETTING)
        on = config._bool(layers.entries[SETTING].value)
    except Exception:
        return UpdateStatus(UNKNOWN)
    return _check(on, now)
