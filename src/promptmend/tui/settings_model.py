"""What the Settings tab lists: every setting in a group, with its kind (a switch, a
choice, a number or text), its choices, a one-line help and the word an empty value shows,
and the rows worked out from a State. A row's dot says one thing only: the value is set by
you (config.toml, the secrets file or the environment), not the built-in default. Pure, with
no Textual, so a test can check it without the interface: `tests/test_tui_settings_model.py`
ties it to `config.env_names()` and parses every choice with the config parser."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..commands import common
from ..config import (
    ANTHROPIC_EFFORTS,
    DATA_COLLECTION,
    DEFAULT_SOURCE,
    EFFORTS,
    ENV_SOURCE,
    OUTPUTS,
    PRO_PROVIDERS,
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
# The empty-value words that are not the plain "none" (Meta.empty). BROKEN is for a setting
# whose parser takes an empty value although the next call then fails (no provider, profile
# or model): the row must not look valid.
NOT_SENT = "not sent"
BROKEN = "empty"


@dataclass(frozen=True)
class Meta:
    name: str
    group: str
    kind: str
    help: str
    choices: tuple[str, ...] = ()
    # What an empty value is shown as, saying what it does: in the list, as the pick list's
    # empty choice and on the edit dialog's Default line. Never one of ``choices``.
    empty: str = "none"


def _m(
    group: str,
    name: str,
    kind: str,
    help_: str,
    choices: Sequence[str] = (),
    *,
    empty: str = "none",
) -> Meta:
    return Meta(name, group, kind, help_, tuple(choices), empty)


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
        empty=BROKEN,
    ),
    _m(_OUT, "PROMPT_PERSONA", TEXT, "Who you are, opening the rewrite's CONTEXT; never shown."),
    _m(_OUT, "PROMPT_TIMEOUT_SECONDS", TEXT, "Seconds before a rewrite call gives up."),
    _m(
        _OUT,
        "PROMPT_TEMPERATURE",
        TEXT,
        "Temperature (not Anthropic, OpenAI); empty: not sent.",
        empty=NOT_SENT,
    ),
    _m(_KEYS, "OPENROUTER_API_KEY", KEY, "OpenRouter's API key, kept in the secrets file."),
    _m(_KEYS, "ANTHROPIC_API_KEY", KEY, "Anthropic's API key, kept in the secrets file."),
    _m(_KEYS, "OPENAI_API_KEY", KEY, "OpenAI's API key, kept in the secrets file."),
    _m(_PRIV, "PROMPT_LOCAL_ONLY", BOOL, "Refuse every provider that sends the draft away."),
    _m(_PRIV, "PROMPT_GATE_LOCAL", BOOL, "Run the data-protection gate for local models too."),
    _m(_PRIV, "ALLOW_CLOUD_OVERRIDE", BOOL, "Let a draft the gate flags reach a cloud provider."),
    _m(_PRIV, "PROMPT_EXTRA_PATTERNS", TEXT, "Your own ';'-separated regexes the gate blocks."),
    _m(
        _PRIV,
        "PROMPT_GATE_PACKS",
        TEXT,
        "Pattern packs the gate scans with (vn); empty turns them off and weakens the gate.",
        empty="off",
    ),
    _m(_PRIV, "PROMPT_UPDATE_CHECK", BOOL, "Ask PyPI once a day for a newer release."),
    _m(
        _PRIV,
        "OPENROUTER_DATA_COLLECTION",
        CHOICE,
        "deny: only endpoints that never store the request; empty: not sent.",
        ("", *DATA_COLLECTION),
        empty=NOT_SENT,
    ),
    _m(
        _MOD,
        "PROMPT_PROVIDER",
        CHOICE,
        "Provider of a bare `promptmend improve` and the Try tab; -ip- uses PROMPT_PRO_PROVIDER.",
        PROVIDER_NAMES,
        empty=BROKEN,
    ),
    _m(_MOD, "OPENROUTER_MODEL", TEXT, "OpenRouter's model for -i-.", empty=BROKEN),
    _m(_MOD, "OPENROUTER_BASE_URL", TEXT, "OpenRouter's API address (https)."),
    _m(
        _MOD,
        "OPENROUTER_PROVIDER",
        TEXT,
        "The OpenRouter endpoint pinned; empty: any endpoint.",
        empty="any endpoint",
    ),
    _m(
        _MOD,
        "OPENROUTER_REASONING_EFFORT",
        CHOICE,
        "Reasoning effort; empty: not sent.",
        ("", *EFFORTS),
        empty=NOT_SENT,
    ),
    _m(_MOD, "OPENROUTER_MAX_TOKENS", INT, "Output cap of an OpenRouter call, in tokens."),
    _m(_MOD, "OPENROUTER_ALLOW_FALLBACKS", BOOL, "Let OpenRouter use another endpoint if down."),
    _m(_MOD, "OPENROUTER_PRO_MODEL", TEXT, "The pro tier's model (-ip-).", empty=BROKEN),
    _m(
        _MOD,
        "OPENROUTER_PRO_PROVIDER",
        TEXT,
        "The pro tier's pinned endpoint; empty: any endpoint.",
        empty="any endpoint",
    ),
    _m(
        _MOD,
        "OPENROUTER_PRO_REASONING_EFFORT",
        CHOICE,
        "The pro tier's reasoning effort; empty: not sent.",
        ("", *EFFORTS),
        empty=NOT_SENT,
    ),
    _m(
        _MOD,
        "OPENROUTER_PRO_MAX_TOKENS",
        TEXT,
        "The pro tier's output cap; empty: as standard (OPENROUTER_MAX_TOKENS).",
        empty="as standard",
    ),
    _m(_MOD, "PROMPT_PRO_TIMEOUT_SECONDS", TEXT, "Seconds before a pro call gives up."),
    _m(
        _MOD,
        "PROMPT_PRO_PROVIDER",
        CHOICE,
        "Which API -ip- calls, once deployed: OpenRouter, Anthropic or OpenAI.",
        PRO_PROVIDERS,
        empty="as openrouter",
    ),
    _m(
        _MOD,
        "PROMPT_PRO_PROFILE",
        CHOICE,
        "The pro tier's profile; empty: as PROMPT_PROFILE.",
        ("", *PROFILE_CHOICES),
        empty="as PROMPT_PROFILE",
    ),
    _m(
        _MOD,
        "PROMPT_PROFILE_OVERRIDES",
        TEXT,
        "Built-in profiles your own same-named file replaces.",
    ),
    _m(_MOD, "OLLAMA_BASE_URL", TEXT, "Ollama's address."),
    _m(_MOD, "OLLAMA_MODEL", TEXT, "Ollama's model (-il-).", empty=BROKEN),
    _m(_MOD, "OLLAMA_THINK", BOOL, "Let an Ollama model think before it answers."),
    _m(
        _MOD,
        "OLLAMA_NUM_CTX",
        TEXT,
        "Ollama's context window in tokens; empty: Ollama's own.",
        empty="Ollama's own",
    ),
    _m(_MOD, "LMSTUDIO_BASE_URL", TEXT, "LM Studio's address."),
    _m(_MOD, "LMSTUDIO_MODEL", TEXT, "LM Studio's model (-ilm-).", empty=BROKEN),
    _m(_MOD, "ANTHROPIC_BASE_URL", TEXT, "Anthropic's API address (https)."),
    _m(_MOD, "ANTHROPIC_MODEL", TEXT, "Anthropic's model.", empty=BROKEN),
    _m(_MOD, "ANTHROPIC_MAX_TOKENS", INT, "Output cap of an Anthropic call, in tokens."),
    _m(
        _MOD,
        "ANTHROPIC_EFFORT",
        CHOICE,
        "Anthropic's effort (how much Claude thinks); empty: not sent.",
        ("", *ANTHROPIC_EFFORTS),
        empty=NOT_SENT,
    ),
    _m(
        _MOD,
        "ANTHROPIC_TEMPERATURE",
        TEXT,
        "Anthropic's sampling temperature; empty: not sent.",
        empty=NOT_SENT,
    ),
    _m(_MOD, "OPENAI_BASE_URL", TEXT, "OpenAI's API address (https)."),
    _m(_MOD, "OPENAI_MODEL", TEXT, "OpenAI's model.", empty=BROKEN),
    _m(
        _MOD,
        "OPENAI_REASONING_EFFORT",
        CHOICE,
        "OpenAI's reasoning effort; empty: not sent.",
        ("", *EFFORTS),
        empty=NOT_SENT,
    ),
    _m(_MOD, "OPENAI_MAX_TOKENS", INT, "Output cap of an OpenAI call, in tokens."),
    _m(
        _MOD,
        "OPENAI_TEMPERATURE",
        TEXT,
        "OpenAI's sampling temperature; empty: not sent.",
        empty=NOT_SENT,
    ),
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
    """One line of the list. ``custom`` is the filled dot: the value comes from config.toml,
    the secrets file or the environment, not the built-in default (even when it is the same
    text as the default); an unset key is never custom. ``switched_on`` is a switch's state,
    which Space flips. ``source`` is the file name, ``where`` the full place for the help
    line (both empty for an unset key)."""

    meta: Meta
    value: str
    custom: bool
    source: str
    where: str
    switched_on: bool = False

    @property
    def name(self) -> str:
        return self.meta.name


def _value(meta: Meta, entry: Entry) -> str:
    if meta.kind == KEY:
        return "set" if entry.value else "not set"
    if meta.kind == BOOL:
        return "on" if entry.value.lower() == "true" else "off"
    if meta.name in common.PRIVATE:
        return HIDDEN if entry.value else meta.empty
    if not entry.value:
        return meta.empty
    return common.shown_value(meta.name, entry)


def row(meta: Meta, entry: Entry) -> Row:
    unset_key = meta.kind == KEY and not entry.value
    return Row(
        meta,
        _value(meta, entry),
        not unset_key and entry.source != DEFAULT_SOURCE,
        "" if unset_key else source_name(entry.source),
        "" if unset_key else common.short_label(entry.source),
        switched_on=meta.kind == BOOL and entry.value.lower() == "true",
    )


def rows(state: State, query: str = "") -> list[Row]:
    """The list's rows, in order, those whose name or group holds ``query`` (any case)."""
    wanted = query.strip().lower()
    return [
        row(meta, state.layers.entries[meta.name])
        for meta in SETTINGS
        if not wanted or wanted in meta.name.lower() or wanted in meta.group.lower()
    ]
