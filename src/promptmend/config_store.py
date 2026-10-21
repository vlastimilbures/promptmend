"""Saving configuration: validated, atomic writes to config.toml and the secret store.

These are the services behind the management commands (#92). Nothing here runs on a trigger,
and nothing here prints: errors are ConfigStoreError or ValueError, whose messages name
settings and files but never repeat a secret.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, config_files
from .config import env_names, secret_names, setting_fields
from .config_files import CONFIG_VERSION, VERSION_KEY
from .redaction import safe_repr

_FIELDS = {f.metadata["env"]: f for f in setting_fields()}


class ConfigStoreError(Exception):
    """A save that was refused or failed; the message is safe to show."""


class ReadOnlyConfigError(ConfigStoreError):
    """config.toml has a config_version this version does not know, so it is never written."""


class StaleEditError(ConfigStoreError):
    """config.toml changed between the read and the write. ``changed`` names the settings
    whose value differs (never the values); it is empty when only comments or layout did."""

    def __init__(self, path: Path, changed: tuple[str, ...]) -> None:
        self.changed = changed
        what = ", ".join(changed) if changed else "comments or layout only"
        super().__init__(f"{path.name} changed since it was read ({what}); read it again")


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def config_dir(environ: Mapping[str, str] | None = None) -> Path:
    return config.user_config_dir(_env(environ))


# --- config.toml --------------------------------------------------------------------------


@dataclass(frozen=True)
class SavedSettings:
    """config.toml as read: its table and the sha256 of its bytes (None when it does not
    exist), which save_settings() checks to refuse a write over an edit made since."""

    path: Path
    table: Mapping[str, Any] = field(repr=False)
    digest: str | None

    @property
    def version(self) -> int | None:
        """The config_version (a missing one is the current version); None when invalid."""
        return config_files.config_version(self.table)

    @property
    def read_only(self) -> bool:
        version = self.version
        return version is None or version > CONFIG_VERSION

    @property
    def values(self) -> dict[str, str]:
        """The settings in the file, as strings, keyed by env var name."""
        known = env_names()
        texts = {k: config_files.scalar_text(v) for k, v in self.table.items() if k in known}
        return {k: v for k, v in texts.items() if v is not None}


def read_settings(environ: Mapping[str, str] | None = None) -> SavedSettings:
    """Read config.toml (an empty snapshot when it does not exist). Raises ConfigFileError
    when it exists but cannot be read or parsed."""
    path = config.settings_file(_env(environ))
    return _snapshot(path)


def _snapshot(path: Path) -> SavedSettings:
    loaded = config_files.load_toml(path)
    if loaded is None:
        return SavedSettings(path, {}, None)
    return SavedSettings(path, loaded[0], loaded[1])


def _toml_value(name: str, raw: str) -> Any:
    """``raw`` validated by the setting's own parser, as the TOML value to save: true/false,
    a number or text. Raises the same ValueError a bad value in a .env does. A value that
    parses to None (an empty PROMPT_TEMPERATURE) is saved as the empty text, since None
    means "remove the setting" to save_settings()."""
    value = config.parse_setting(name, _FIELDS[name].metadata["parse"], raw)
    return raw if value is None else value


def _changed_keys(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[str, ...]:
    known = env_names()
    changed = [k for k in {*old, *new} if old.get(k) != new.get(k)]
    return tuple(sorted(k if k in known else safe_repr(k) for k in changed))


def save_settings(snapshot: SavedSettings, changes: Mapping[str, str | None]) -> SavedSettings:
    """Write ``changes`` (env var name to raw value; None removes the setting, so its
    default applies) into the config.toml that ``snapshot`` read, and return it read again.

    Every value is validated first. A secret is refused: it belongs in the secret store, and
    no secret is ever written to config.toml. Refused too: a file with a newer
    config_version (ReadOnlyConfigError), and a file that changed since ``snapshot`` was
    read (StaleEditError, naming the settings that differ). Settings this version does not
    know are kept; comments are not (tomli-w writes plain TOML).
    """
    if snapshot.read_only:
        raise ReadOnlyConfigError(
            f"{snapshot.path.name} has an unknown {VERSION_KEY}; this version "
            f"({CONFIG_VERSION}) only reads it"
        )
    known, secrets = env_names(), secret_names()
    updates: dict[str, Any] = {}
    for name, raw in changes.items():
        if name not in known:
            raise ValueError(f"{safe_repr(name)} is not a setting")
        if name in secrets:
            raise ValueError(f"{name} is a secret; save it in the secret store, not config.toml")
        updates[name] = None if raw is None else _toml_value(name, raw)
    held = [k for k in snapshot.table if k in secrets]
    if held:
        raise ConfigStoreError(
            f"{', '.join(held)} in {snapshot.path.name} is a secret; move it to the secret "
            "store before saving"
        )
    current = _snapshot(snapshot.path)
    if current.digest != snapshot.digest:
        raise StaleEditError(snapshot.path, _changed_keys(snapshot.table, current.table))

    table = {VERSION_KEY: CONFIG_VERSION}
    table |= {k: v for k, v in snapshot.table.items() if k != VERSION_KEY}
    for name, value in updates.items():
        if value is None:
            table.pop(name, None)
        else:
            table[name] = value
    config_files.write_atomic(snapshot.path, config_files.dump_toml(table), private=False)
    return _snapshot(snapshot.path)


# --- secrets ------------------------------------------------------------------------------


def _secret_name(name: str) -> str:
    if name not in secret_names():
        raise ValueError(f"{safe_repr(name)} is not a secret setting")
    return name


def save_secret(name: str, value: str, environ: Mapping[str, str] | None = None) -> None:
    """Save an API key in the secret store (never config.toml). Raises SecretStoreError when
    the store fails; nothing is then written anywhere else."""
    cleaned = value.strip()
    if not cleaned or any(c.isspace() or not c.isprintable() for c in cleaned):
        raise ValueError(f"{_secret_name(name)} must be one line of visible characters")
    config_files.secret_store(config_dir(environ)).set(_secret_name(name), cleaned)


def delete_secret(name: str, environ: Mapping[str, str] | None = None) -> None:
    config_files.secret_store(config_dir(environ)).delete(_secret_name(name))


def saved_secret_names(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Which secrets the store holds; their values are never returned."""
    saved = config_files.secret_store(config_dir(environ)).read()
    return tuple(name for name in secret_names() if saved.get(name))
