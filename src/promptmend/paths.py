"""The user folders (settings, per-device data), the variable that names a legacy .env
instead of them, and the root of an editable install."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from . import config_files

# Root of an editable install (the repo checkout).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# The user folders' name.
APP_DIR = "promptmend"
# The .env that replaces config.toml and the secrets file (legacy mode), when set (non-empty).
ENV_FILE_VAR = "PROMPTMEND_ENV"


def env_file_override(environ: Mapping[str, str] = os.environ) -> str | None:
    """The .env PROMPTMEND_ENV names, or None: then it alone holds the saved settings. An
    empty value counts as unset."""
    return environ.get(ENV_FILE_VAR) or None


def user_config_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """The settings, the secrets file and the prices: %APPDATA% on Windows, $XDG_CONFIG_HOME
    or ~/.config elsewhere."""
    if os.name == "nt":
        base = Path(environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_DIR


def user_data_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """Per-device data (the usage history): never roamed or synced with the settings, since
    it describes this machine's calls. %LOCALAPPDATA% on Windows, $XDG_DATA_HOME or
    ~/.local/share elsewhere."""
    if os.name == "nt":
        base = Path(environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_DIR


def settings_file(environ: Mapping[str, str] = os.environ) -> Path:
    """The user TOML settings file (non-secret settings), the saved source unless
    PROMPTMEND_ENV names a .env."""
    return user_config_dir(environ) / config_files.SETTINGS_FILE


def unread_env_files(environ: Mapping[str, str] = os.environ) -> list[Path]:
    """The .env files earlier releases read and this one does not: the user config
    folder's, and an editable install's checkout's. Their settings would be lost without a
    word, so repair mode and setup name them."""
    candidates = [user_config_dir(environ) / ".env"]
    if (_PROJECT_ROOT / "pyproject.toml").is_file():
        candidates.append(_PROJECT_ROOT / ".env")
    return [path for path in candidates if config_files.is_file(path)]
