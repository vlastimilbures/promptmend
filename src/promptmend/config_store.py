"""Saving configuration: validated, atomic writes to config.toml and the secrets file.

These are the services behind the management commands. Nothing here runs on a trigger,
and nothing here prints: errors are ConfigStoreError or ValueError, whose messages name
settings and files but never repeat a secret.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, config_files, paths
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
    return paths.user_config_dir(_env(environ))


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
        texts = {k: config.saved_text(k, v) for k, v in self.table.items() if k in known}
        return {k: v for k, v in texts.items() if v is not None}


def read_settings(environ: Mapping[str, str] | None = None) -> SavedSettings:
    """Read config.toml (an empty snapshot when it does not exist). Raises ConfigFileError
    when it exists but cannot be read or parsed."""
    path = paths.settings_file(_env(environ))
    return _snapshot(path)


def _snapshot(path: Path) -> SavedSettings:
    loaded = config_files.load_toml(path)
    if loaded is None:
        return SavedSettings(path, {}, None)
    return SavedSettings(path, loaded[0], loaded[1])


def _known(name: str) -> str:
    if name not in _FIELDS:
        raise ValueError(f"{safe_repr(name)} is not a setting")
    return name


def secret_refusal(name: str, verb: str) -> str | None:
    """Why ``name`` cannot be given a value (``verb`` "set") or removed ("remove") in
    config.toml, or None: a secret goes only in the secrets file, through `secrets` (a hidden
    prompt or --stdin), never as an argument. The one wording of that rule, for `config
    set|unset` (settings_service) and save_settings()."""
    if name not in secret_names():
        return None
    return (
        f"{name} is a secret: {verb} it with `promptmend secrets {verb} {name}` (a hidden "
        "prompt or --stdin), never as an argument"
    )


def toml_value(name: str, raw: str) -> Any:
    """``raw`` validated by the setting's own parser, as the TOML value to save: true/false,
    a number or text. Raises the same ValueError a bad value in a .env does, and one naming
    an unknown setting. A value that
    parses to None (an empty PROMPT_TEMPERATURE) is saved as the empty text, since None
    means "remove the setting" to save_settings(). A list (PROMPT_PROFILE_OVERRIDES,
    PROMPT_GATE_PACKS) is saved as its canonical comma-separated text: config.toml holds
    only scalars (config_files.scalar_text), and the setting's parser reads the text back."""
    value = config.parse_setting(name, _FIELDS[_known(name)].metadata["parse"], raw)
    if value is None:
        return raw
    return ",".join(value) if isinstance(value, tuple) else value


def _changed_keys(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[str, ...]:
    known = env_names()
    changed = [k for k in {*old, *new} if old.get(k) != new.get(k)]
    return tuple(sorted(k if k in known else safe_repr(k) for k in changed))


def save_settings(snapshot: SavedSettings, changes: Mapping[str, str | None]) -> SavedSettings:
    """Write ``changes`` (env var name to raw value; None removes the setting, so its
    default applies) into the config.toml that ``snapshot`` read, and return it read again.

    Every value is validated first. A secret is refused: it belongs in the secrets file, and
    no secret is ever written to config.toml. Refused too: a file with a newer
    config_version (ReadOnlyConfigError), and a file that changed since ``snapshot`` was
    read (StaleEditError, naming the settings that differ). Settings this version does not
    know are kept unless a change removes one (None); comments are not (tomli-w writes
    plain TOML).
    """
    if snapshot.read_only:
        raise ReadOnlyConfigError(
            f"{snapshot.path.name} has an unknown {VERSION_KEY}; this version "
            f"({CONFIG_VERSION}) only reads it"
        )
    updates: dict[str, Any] = {}
    for name, raw in changes.items():
        if raw is None and name in snapshot.table and name not in _FIELDS and name != VERSION_KEY:
            updates[name] = None  # a key that is no setting here (one a release removed)
            continue
        refused = secret_refusal(_known(name), "set" if raw is not None else "remove")
        if refused:
            raise ValueError(refused)
        updates[name] = None if raw is None else toml_value(name, raw)
    held = [k for k in snapshot.table if k in secret_names()]
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


def secret_name(name: str) -> str:
    """``name`` when it is a secret, else a ValueError naming the secrets (`secrets
    set|remove` give it as their usage error)."""
    if name not in secret_names():
        raise ValueError(
            f"{safe_repr(name)} is not a secret; choose from {', '.join(secret_names())}"
        )
    return name


def save_secret(name: str, value: str, environ: Mapping[str, str] | None = None) -> None:
    """Save an API key in the secrets file (never config.toml). Raises SecretStoreError when
    the store fails; nothing is then written anywhere else."""
    cleaned = value.strip()
    if not cleaned or any(c.isspace() or not c.isprintable() for c in cleaned):
        raise ValueError(f"{secret_name(name)} must be one line of visible characters")
    config_files.secret_store(config_dir(environ)).set(secret_name(name), cleaned)


def delete_secret(name: str, environ: Mapping[str, str] | None = None) -> None:
    config_files.secret_store(config_dir(environ)).delete(secret_name(name))


def saved_secret_names(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Which secrets the store holds; their values are never returned."""
    saved = config_files.secret_store(config_dir(environ)).read()
    return tuple(name for name in secret_names() if saved.get(name))
