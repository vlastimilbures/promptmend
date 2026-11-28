"""The settings services every front end shares: what `config set|unset` and `secrets set`
check and save, the repair-mode settings, and the legacy-mode refusal.

The commands (commands/settings.py), setup (setup_flow.py) and the interface (tui/) all go
through these, so a rule about what may be saved lives here once. Nothing here prints or
imports Typer, Click or Textual (tests/test_setup_flow.py checks it): a refusal is a
ServiceError, whose text is this project's own and is shown as it is; commands.common.guard
turns it into `error: …` and an exit code (USAGE when ``usage``, else FAILED), and
tui/base.error_message into a message. Writes go through config_store.py. Never on the
trigger path (tests/test_trigger_contract.py)."""

from __future__ import annotations

import os
from pathlib import Path

from . import config_files, config_store, paths
from .config import ConfigLayers, Settings
from .redaction import looks_like_a_key

# The user's own credential patterns: its value may match itself, so it is never hidden or
# refused by them.
EXTRA_PATTERNS = "PROMPT_EXTRA_PATTERNS"

KEY_LIKE = (
    "that value looks like a key or password, which never goes in config.toml; use "
    "`promptmend secrets set NAME` for an API key"
)


def key_like(name: str, value: str) -> bool:
    """Whether ``value``, a value of setting ``name``, looks like a key
    (redaction.looks_like_a_key()): the one rule for what is never saved in config.toml or
    shown. PROMPT_EXTRA_PATTERNS' own value is checked without the user's patterns, which may
    match their own text."""
    return looks_like_a_key(value, user_patterns=name != EXTRA_PATTERNS)


class ServiceError(Exception):
    """A refused change; the message says why and never holds a secret value. ``usage``
    marks a refusal of what was given (a secret's name, a key-like value), which a command
    exits 2 for; anything else exits 1."""

    def __init__(self, message: str, *, usage: bool = False) -> None:
        super().__init__(message)
        self.usage = usage


# --- the settings --------------------------------------------------------------------------


def load_layers() -> tuple[ConfigLayers, Settings]:
    """The settings in repair mode: a broken .env or TOML is reported, not raised, and a
    rejected value falls back to the next lower layer, so every command still runs."""
    layers = ConfigLayers.resolve(strict=False)
    # Cannot raise: a rejected value fell back to a lower layer, at worst the default.
    return layers, layers.settings()


def legacy_env() -> str | None:
    """The .env PROMPTMEND_ENV names: then it alone is read, never config.toml
    or the secrets file, so a command that would write them refuses."""
    return paths.env_file_override()


def history_off(*, legacy: bool) -> str:
    """How to switch the usage history off, true in either mode (#396): `config set`, or in
    legacy mode (``legacy``: PROMPTMEND_ENV set), where `config set` refuses, the line to add
    to the file it names (never its path, as factory.key_hint()). The one source of the
    advice for `stats --verbose`, `setup` and the History and setup screens."""
    if legacy:
        return f"add PROMPT_HISTORY=false to the .env that {paths.ENV_FILE_VAR} names"
    return "`promptmend config set PROMPT_HISTORY false`"


def refuse_in_legacy_mode(what: str) -> None:
    """Raise ServiceError when PROMPTMEND_ENV means ``what`` would not be read."""
    path = legacy_env()
    if path:
        name = paths.ENV_FILE_VAR
        raise ServiceError(
            f"{name} is set ({path}), so {what} would not be read; edit that file, "
            f"or unset {name} and save the settings with `promptmend setup`"
        )


def refuse_key(value: str | None, option: str) -> None:
    """Refuse a key given to an option or a field that takes a path or a name, before it is
    used or echoed back (a command prints the paths it works on): a usage ServiceError.
    commands.common.no_key() gives the same text as Click's usage error."""
    if value and looks_like_a_key(value):
        raise ServiceError(f"{option} looks like a key; a key is never an argument", usage=True)


def _not_a_secret(name: str, verb: str) -> None:
    refused = config_store.secret_refusal(name, verb)
    if refused:
        raise ServiceError(refused, usage=True)


def check_setting(name: str, value: str) -> None:
    """What `config set` and the Settings dialog check before ``value`` is saved for
    ``name``: not a secret and not a value that looks like a key (ServiceError, usage), and
    readable as the CLI reads it (the setting's own ValueError)."""
    _not_a_secret(name, "set")
    if key_like(name, value):
        raise ServiceError(KEY_LIKE, usage=True)
    config_store.toml_value(name, value)


def save_setting(name: str, value: str) -> config_store.SavedSettings:
    """`config set` without the printing, shared with setup and the interface: check_setting(),
    refuse legacy mode (ServiceError), then save ``value`` in config.toml."""
    check_setting(name, value)
    refuse_in_legacy_mode(config_files.SETTINGS_FILE)
    return config_store.save_settings(config_store.read_settings(), {name: value})


def unset_setting(name: str) -> tuple[bool, Path]:
    """`config unset` without the printing, shared with setup and the interface's reset:
    refuse a secret and legacy mode (ServiceError), then remove ``name`` from config.toml.
    Whether it was saved there, and the file's path."""
    _not_a_secret(name, "remove")
    refuse_in_legacy_mode(config_files.SETTINGS_FILE)
    snapshot = config_store.read_settings()
    if name not in snapshot.table:
        return False, snapshot.path
    config_store.save_settings(snapshot, {name: None})
    return True, snapshot.path


def store_key(name: str, value: str) -> str:
    """`secrets set` without the printing, shared with setup and the interface: refuse legacy
    mode (ServiceError), save ``value`` in the secrets file and say so. after_save() gives the
    warnings to show with it."""
    refuse_in_legacy_mode("the secrets file")
    config_store.save_secret(name, value)
    return f"{name} saved in the secrets file ({config_store.config_dir()})"


# --- after a save --------------------------------------------------------------------------


def env_override(name: str) -> str | None:
    """The warning to show after saving ``name`` while a real environment variable outranks
    it, or None."""
    if name in os.environ:
        return f"the environment variable {name} is set and overrides the saved value"
    return None


# The settings settings_checks.persona_problem() reads: saving one can make the persona go out.
_PERSONA_INPUTS = frozenset(
    {
        "PROMPT_PERSONA",
        "PROMPT_PROFILE",
        "PROMPT_PRO_PROFILE",
        "PROMPT_PROFILE_OVERRIDES",
        "PROMPT_EXTRA_PATTERNS",
        "PROMPT_GATE_PACKS",
        "PROMPT_LOCAL_ONLY",
        "PROMPT_GATE_LOCAL",
    }
)


def persona_warning(name: str) -> str | None:
    """The warning to show after saving ``name`` when the effective persona now matches the
    data-protection patterns and is sent (`config validate`'s problem: finding names only,
    never the text), or None. The value stays saved: a persona never blocks a call."""
    if name not in _PERSONA_INPUTS:
        return None
    from .settings_checks import persona_problem

    return persona_problem(load_layers()[1])


def after_save(name: str) -> list[str]:
    """The warnings to show after saving or removing ``name``; setup and the interface show
    them too."""
    return [note for note in (env_override(name), persona_warning(name)) if note]
