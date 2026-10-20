"""What the Settings tab lists (#199): every setting in a group, with its kind (a switch, a
choice, a number or text), its choices and a one-line help, and the rows worked out from a
State. Pure, with no Textual, so a test can check it without the interface:
`tests/test_tui_settings_model.py` ties it to `config.env_names()` and parses every choice
with the config parser."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..commands import common
from ..config import (
    DATA_COLLECTION,
    DEFAULT_SOURCE,
    EFFORTS,
    ENV_SOURCE,
    OUTPUTS,
    Entry,
    setting_fields,
)
from ..factory import PROVIDER_NAMES
from ..prompt_builder import ADDED, PROFILES
from .state import State

BOOL, CHOICE, INT, TEXT, KEY = "bool", "choice", "int", "text", "key"
# A choice list made from the profiles (built in and the user's added ones), worked out per
# State: the user's profiles change.
PROFILE_CHOICES = ("<profiles>",)
HIDDEN = "<set, hidden>"
EMPTY = "(empty)"


@dataclass(frozen=True)
class Meta:
    name: str
    group: str
    kind: str
    help: str
    choices: tuple[str, ...] = ()


def _m(group: str, name: str, kind: str, help_: str, choices: Sequence[str] = ()) -> Meta:
    return Meta(name, group, kind, help_, tuple(choices))


_OUT, _KEYS, _PRIV, _MOD, _HIST = ("Output", "Keys", "Privacy", "Models", "History")
GROUPS = (_OUT, _KEYS, _PRIV, _MOD, _HIST)

# Every setting, in the order the list shows it: each config.env_names() name exactly once.
SETTINGS: tuple[Meta, ...] = (
    _m(
        _OUT,
        "PROMPT_OUTPUT",
        CHOICE,
        "Paste the rewrite (paste) or copy it to the clipboard (clipboard).",
        OUTPUTS,
    ),
    _m(
        _OUT,
        "PROMPT_PROFILE",
        CHOICE,
        "The profile (system prompt) a rewrite uses.",
        PROFILE_CHOICES,
    ),
    _m(_OUT, "PROMPT_PERSONA", TEXT, "Who you are, opening the rewrite's CONTEXT; never shown."),
    _m(_OUT, "PROMPT_TIMEOUT_SECONDS", TEXT, "Seconds before a rewrite call gives up."),
    _m(_OUT, "PROMPT_TEMPERATURE", TEXT, "Sampling temperature (not Anthropic); empty: none."),
    _m(_KEYS, "OPENROUTER_API_KEY", KEY, "OpenRouter's API key, kept in the secret store."),
    _m(_KEYS, "ANTHROPIC_API_KEY", KEY, "Anthropic's API key, kept in the secret store."),
    _m(_PRIV, "PROMPT_LOCAL_ONLY", BOOL, "Refuse every provider that sends the draft away."),
    _m(_PRIV, "PROMPT_GATE_LOCAL", BOOL, "Run the data-protection gate for local models too."),
    _m(_PRIV, "ALLOW_CLOUD_OVERRIDE", BOOL, "Let a draft the gate flags reach a cloud provider."),
    _m(_PRIV, "PROMPT_EXTRA_PATTERNS", TEXT, "Your own ';'-separated regexes the gate blocks."),
    _m(_PRIV, "PROMPT_UPDATE_CHECK", BOOL, "Ask PyPI once a day for a newer release."),
    _m(
        _PRIV,
        "OPENROUTER_DATA_COLLECTION",
        CHOICE,
        "deny: only endpoints that never store the request; empty sends no field.",
        ("", *DATA_COLLECTION),
    ),
    _m(
        _MOD,
        "PROMPT_PROVIDER",
        CHOICE,
        "Provider of a bare `promptmend improve` and the Try tab; triggers name their own.",
        PROVIDER_NAMES,
    ),
    _m(_MOD, "OPENROUTER_MODEL", TEXT, "OpenRouter's model for -i-."),
    _m(_MOD, "OPENROUTER_BASE_URL", TEXT, "OpenRouter's API address (https)."),
    _m(_MOD, "OPENROUTER_PROVIDER", TEXT, "The OpenRouter endpoint pinned; empty routes freely."),
    _m(
        _MOD,
        "OPENROUTER_REASONING_EFFORT",
        CHOICE,
        "Reasoning effort; empty sends none.",
        ("", *EFFORTS),
    ),
    _m(_MOD, "OPENROUTER_MAX_TOKENS", INT, "Output cap of an OpenRouter call, in tokens."),
    _m(_MOD, "OPENROUTER_ALLOW_FALLBACKS", BOOL, "Let OpenRouter use another endpoint if down."),
    _m(_MOD, "OPENROUTER_PRO_MODEL", TEXT, "The pro tier's model (-ip-)."),
    _m(_MOD, "OPENROUTER_PRO_PROVIDER", TEXT, "The pro tier's pinned endpoint."),
    _m(
        _MOD,
        "OPENROUTER_PRO_REASONING_EFFORT",
        CHOICE,
        "The pro tier's reasoning effort; empty sends none.",
        ("", *EFFORTS),
    ),
    _m(_MOD, "OPENROUTER_PRO_MAX_TOKENS", TEXT, "The pro tier's output cap; empty: the same."),
    _m(_MOD, "PROMPT_PRO_TIMEOUT_SECONDS", TEXT, "Seconds before a pro call gives up."),
    _m(
        _MOD,
        "PROMPT_PRO_PROFILE",
        CHOICE,
        "The pro tier's profile; empty uses PROMPT_PROFILE.",
        ("", *PROFILE_CHOICES),
    ),
    _m(
        _MOD,
        "PROMPT_PROFILE_OVERRIDES",
        TEXT,
        "Built-in profiles your own same-named file replaces.",
    ),
    _m(_MOD, "OLLAMA_BASE_URL", TEXT, "Ollama's address."),
    _m(_MOD, "OLLAMA_MODEL", TEXT, "Ollama's model (-il-)."),
    _m(_MOD, "OLLAMA_THINK", BOOL, "Let an Ollama model think before it answers."),
    _m(_MOD, "OLLAMA_NUM_CTX", TEXT, "Ollama's context window in tokens; empty: Ollama's own."),
    _m(_MOD, "LMSTUDIO_BASE_URL", TEXT, "LM Studio's address."),
    _m(_MOD, "LMSTUDIO_MODEL", TEXT, "LM Studio's model (-ilm-)."),
    _m(_MOD, "ANTHROPIC_BASE_URL", TEXT, "Anthropic's API address (https)."),
    _m(_MOD, "ANTHROPIC_MODEL", TEXT, "Anthropic's model."),
    _m(_MOD, "ANTHROPIC_MAX_TOKENS", INT, "Output cap of an Anthropic call, in tokens."),
    _m(_MOD, "ANTHROPIC_TEMPERATURE", TEXT, "Anthropic's sampling temperature; empty: none."),
    _m(_HIST, "PROMPT_HISTORY", BOOL, "Keep the local usage history (metadata only)."),
    _m(_HIST, "PROMPT_HISTORY_RETENTION_DAYS", INT, "Days a usage record is kept."),
)
BY_NAME = {meta.name: meta for meta in SETTINGS}


def default(name: str) -> str:
    """A setting's built-in default, as text."""
    return next(str(f.metadata["default"]) for f in setting_fields() if f.metadata["env"] == name)


def parse(name: str, raw: str) -> Any:
    """``raw`` read as the CLI reads ``name``; ValueError naming the setting otherwise."""
    field = next(f for f in setting_fields() if f.metadata["env"] == name)
    return config.parse_setting(name, field.metadata["parse"], raw)


def choices(meta: Meta, state: State | None) -> tuple[str, ...]:
    """``meta``'s choices, the profiles filled in: built in, then the user's added ones."""
    if PROFILE_CHOICES[0] not in meta.choices:
        return meta.choices
    own = [p.name for p in state.profiles if p.status == ADDED] if state else []
    profiles = tuple(dict.fromkeys([*PROFILES, *own]))
    return tuple(c for c in meta.choices if c not in PROFILE_CHOICES) + profiles


def source_name(source: str) -> str:
    """Where a value comes from, short enough for the list: the file name only."""
    if source == DEFAULT_SOURCE:
        return "default"
    if source == ENV_SOURCE:
        return "environment"
    return Path(source.removeprefix("file:")).name


@dataclass(frozen=True)
class Row:
    """One line of the list: ``on`` is the green dot (a switch on, a key set, a value that is
    not the default); ``source`` the file name, ``where`` the full place for the help line."""

    meta: Meta
    value: str
    on: bool
    source: str
    where: str

    @property
    def name(self) -> str:
        return self.meta.name


def _value(meta: Meta, entry: Entry) -> str:
    if meta.kind == KEY:
        return "set" if entry.value else "not set"
    if meta.kind == BOOL:
        return "on" if entry.value.lower() == "true" else "off"
    if meta.name in common.PRIVATE:
        return HIDDEN if entry.value else EMPTY
    if not entry.value:
        return EMPTY
    return common.shown_value(meta.name, entry)


def _on(meta: Meta, entry: Entry) -> bool:
    if meta.kind == KEY:
        return bool(entry.value)
    if meta.kind == BOOL:
        return entry.value.lower() == "true"
    return entry.source != DEFAULT_SOURCE or entry.value != default(meta.name)


def row(meta: Meta, entry: Entry) -> Row:
    unset_key = meta.kind == KEY and not entry.value
    return Row(
        meta,
        _value(meta, entry),
        _on(meta, entry),
        "" if unset_key else source_name(entry.source),
        "" if unset_key else common.short_label(entry.source),
    )


def rows(state: State, query: str = "") -> list[Row]:
    """The list's rows, in order, those whose name or group holds ``query`` (any case)."""
    wanted = query.strip().lower()
    return [
        row(meta, state.layers.entries[meta.name])
        for meta in SETTINGS
        if not wanted or wanted in meta.name.lower() or wanted in meta.group.lower()
    ]
