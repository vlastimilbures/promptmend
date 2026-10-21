from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import Field, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from . import config_files
from .config_files import CONFIG_VERSION, VERSION_KEY, ConfigFileError, SecretStoreError
from .providers.registry import PROVIDERS
from .redaction import InvalidExtraPattern, compile_extra, safe_repr, set_user_patterns


def _parse_value(raw: str) -> str:
    """A .env value: the text inside a leading quote pair, else the text before an inline
    ` # comment` (so `OPENROUTER_PROVIDER=openai  # note` is just `openai`)."""
    value = raw.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end > 0:
            return value[1:end]
    return re.split(r"\s+#", value, maxsplit=1)[0]


def _cut_at_comment(raw: str) -> bool:
    """Whether _parse_value() dropped an unquoted ` #...` from ``raw``: a comment, or part of
    the value (a regex, a persona) that needed quotes. Only the user can tell."""
    return _parse_value(raw) != raw.strip() and raw.strip()[:1] not in ("'", '"')


# Free-text settings whose value may well hold ` #` (a regex for `#12345`, "I am #1"), so a cut
# there is reported. Elsewhere ` # note` is the usual dotenv comment and stays silent: no
# model slug, profile name, number or key holds a space, and a base URL never needs ` #`.
_HASH_IN_VALUE = frozenset({"PROMPT_EXTRA_PATTERNS", "PROMPT_PERSONA"})
_EXTRA = "PROMPT_EXTRA_PATTERNS"
# Settings a release removed: a saved one is skipped without a finding, since the user saved
# it when it was a setting (and `config unset` no longer takes the name).
RETIRED = frozenset({"PROMPT_UI_INTRO"})  # the interface's intro (#245)


def _env_text(data: bytes) -> str:
    """The text of a .env file's bytes. UTF-8, with a leading byte order mark (Windows
    Notepad) dropped, so it never becomes part of the first key."""
    return data.decode("utf-8-sig")


# Root of an editable install (the repo checkout).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# The user folders' name (#169).
APP_DIR = "promptmend"
# The .env that replaces config.toml and the secret store (legacy mode), when set (non-empty).
ENV_FILE_VAR = "PROMPTMEND_ENV"


def env_file_override(environ: Mapping[str, str] = os.environ) -> str | None:
    """The .env PROMPTMEND_ENV names, or None: then it alone holds the saved settings. An
    empty value counts as unset."""
    return environ.get(ENV_FILE_VAR) or None


def user_config_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """The settings, the secret store and the prices: %APPDATA% on Windows, $XDG_CONFIG_HOME
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


def unread_env_file(environ: Mapping[str, str] = os.environ) -> Path | None:
    """A .env in the user config folder, which no release since the legacy paths were removed
    (#275) reads: its settings would be lost without a word, so repair mode reports it."""
    path = user_config_dir(environ) / ".env"
    return path if config_files.is_file(path) else None


def _parse_env_text(text: str) -> tuple[dict[str, str], list[int], list[str]]:
    """KEY=VALUE pairs of .env text, the numbers of the lines skipped for lacking `=`, and the
    _HASH_IN_VALUE keys whose unquoted value was cut at ` #` (see _cut_at_comment)."""
    pairs = {}
    skipped = []
    cut = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip().removeprefix("export ")
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            skipped.append(number)
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = _parse_value(value)
        if key.strip() in _HASH_IN_VALUE and _cut_at_comment(value):
            cut.append(key.strip())
    return pairs, skipped, cut


def _cut_value_error(key: str) -> str:
    return f"the value of {key} was cut at ' #' (a comment); quote the value"


def _read_env_file(
    path: Path,
    note: Callable[[str, str], None],
    fail: Callable[..., None],
) -> dict[str, str] | None:
    """The pairs of the .env PROMPTMEND_ENV names, or None when it cannot be read. A file
    that exists but cannot be read is noted, as is a line without `=` (by line number or
    setting name, never content), and a persona cut at ` #`. A file that is not UTF-8 text
    goes to ``fail``: its settings are lost, which strict mode must not hide. So does
    PROMPT_EXTRA_PATTERNS cut at ` #`: a gate missing part of its patterns fails closed, and
    repair mode drops the cut value (the next layer's applies)."""
    source = f"file:{path}"
    try:
        raw_text = _env_text(path.read_bytes())
    except UnicodeDecodeError:
        fail(source, f"the .env named by {ENV_FILE_VAR} is not UTF-8 text; save it as UTF-8")
        return None
    except OSError:
        if config_files.lexists(path):
            note(source, ".env cannot be read; it was skipped")
        return None
    pairs, skipped, cut = _parse_env_text(raw_text)
    for number in skipped:
        note(source, f"line {number} of .env has no '=' and was ignored")
    for key in cut:
        if key == "PROMPT_EXTRA_PATTERNS":
            fail(source, _cut_value_error(key), (key,))
            del pairs[key]
        else:
            note(source, _cut_value_error(key))
    return pairs


def _saved_layer(
    path: Path,
    fail: Callable[..., None],
    note: Callable[[str, str], None],
) -> Layer | None:
    """The settings in config.toml, or None when it does not exist. A file that exists but
    cannot be parsed still returns a layer (an empty one), so saved mode stays on and a .env
    never steps in for a broken config.toml."""
    source = f"file:{path}"
    name = config_files.SETTINGS_FILE
    try:
        loaded = config_files.load_toml(path)
    except ConfigFileError as exc:
        fail(source, str(exc))
        return Layer(source, {})
    if loaded is None:
        if config_files.lexists(path):
            note(source, f"{name} is not a file; it was ignored")
        return None
    table = loaded[0]
    version = config_files.config_version(table)
    if version is None:
        fail(source, f"{VERSION_KEY} in {name} must be a whole number above 0")
    elif version > CONFIG_VERSION:
        note(
            source,
            f"{name} has {VERSION_KEY} {version}, newer than this version reads "
            f"({CONFIG_VERSION}); it is read-only here",
        )
    known, secrets = env_names(), secret_names()
    values = {}
    for key, value in table.items():
        if key == VERSION_KEY or key in RETIRED:
            continue
        if key not in known:
            note(source, f"{safe_repr(key)} in {name} is not a setting; it was ignored")
            continue
        text = config_files.scalar_text(value)
        if text is None:
            fail(source, f"{key} in {name} must be text, a number or true/false", (key,))
            continue
        if key in secrets:
            note(source, f"{key} is a secret; move it from {name} to the secret store")
        values[key] = text
    return Layer(source, values)


def _secrets_layer(
    directory: Path,
    strict: bool,
    fail: Callable[[str, str], None],
    note: Callable[[str, str], None],
) -> Layer | None:
    """The API keys in the secret store, or None when it holds none. A store that fails
    fails the settings: there is no fallback to another store."""
    store = config_files.secret_store(directory)
    try:
        values = store.read()
    except SecretStoreError as exc:
        fail(store.source, str(exc))
        return None
    path = store.path
    if not strict and config_files.lexists(path) and not config_files.is_file(path):
        note(store.source, f"{path.name} is not a file; it was ignored")
    if not values:
        return None
    secrets = secret_names()
    for key in values:
        if key not in secrets:
            note(store.source, f"{safe_repr(key)} is not a secret setting; it was ignored")
    if not strict and store.exposed():
        note(store.source, "the secrets file can be read by other users; make it private (600)")
    return Layer(store.source, {k: v for k, v in values.items() if k in secrets})


def _merged_lines(pairs: Mapping[str, str]) -> list[str]:
    """Keys whose value holds another assignment (a setting name in any case, or any
    `UPPER_CASE` name, then `=`): a .env saved without the newline between two lines, whose
    rest would otherwise become part of this setting's value. PROMPT_EXTRA_PATTERNS is
    exempt, since a user regex may well match such text."""
    known = "|".join(re.escape(name) for name in env_names())
    assignment = re.compile(rf"(?:(?i:{known})|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\s*=")
    return [
        key
        for key, value in pairs.items()
        if key != "PROMPT_EXTRA_PATTERNS" and assignment.search(value)
    ]


def _merged_line_error(key: str) -> str:
    # An unknown key is shown only through safe_repr: a key line merged in front of it
    # would otherwise be repeated.
    shown = key if key in env_names() else safe_repr(key)
    return f"{shown} in .env runs into the next line; add the missing newline"


# Settings whose rejected value is never quoted, not even through safe_repr: a user regex may
# describe the very data it guards (a project code name), and errors are pasted into the
# focused app. The parser's message names the position instead.
_UNQUOTED = frozenset({"PROMPT_EXTRA_PATTERNS"})


def parse_setting(name: str, parse: Callable[[str], Any], raw: str) -> Any:
    try:
        return parse(raw)
    except ValueError as exc:
        got = "" if name in _UNQUOTED else f", got {safe_repr(raw)}"
        raise ValueError(f"{name} must be {exc}{got}") from None


def _env(
    name: str, default: str, parse: Callable[[str], Any] = str, *, secret: bool = False
) -> Any:
    """A dataclass field read from env var `name` when Settings() is instantiated (no .env:
    Settings.load() builds from ConfigLayers instead). A ``secret`` stays out of repr(), so
    a traceback or test failure cannot print it."""

    def read() -> Any:
        return parse_setting(name, parse, os.getenv(name, default))

    metadata = {"env": name, "default": default, "parse": parse, "secret": secret}
    return field(default_factory=read, repr=not secret, metadata=metadata)


def setting_fields() -> tuple[Field[Any], ...]:
    """The Settings fields read from a setting (env var, .env, config.toml), in field order;
    a per-call field such as call_max_tokens has none."""
    return tuple(f for f in fields(Settings) if "env" in f.metadata)


def env_names() -> tuple[str, ...]:
    """Every environment variable Settings reads, in field order."""
    return tuple(f.metadata["env"] for f in setting_fields())


def secret_names() -> tuple[str, ...]:
    """The settings that hold a secret (API keys): saved only in the secret store, never in
    config.toml, and kept out of repr()."""
    return tuple(f.metadata["env"] for f in setting_fields() if f.metadata["secret"])


# Parsers raise ValueError(what the value must be); _env and _override name the setting.
def _bool(raw: str) -> bool:
    # Strict, so a typo such as OLLAMA_THINK=1 is reported instead of silently meaning false.
    if raw.lower() not in ("true", "false"):
        raise ValueError("true or false")
    return raw.lower() == "true"


def _number(
    cast: Callable[[str], float], expected: str, ok: Callable[[float], bool]
) -> Callable[[str], float]:
    """Parser for a finite number that passes ``ok``: an `inf` timeout would hang Espanso."""

    def parse(raw: str) -> float:
        try:
            value = cast(raw)
        except ValueError:
            value = math.nan
        if not (math.isfinite(value) and ok(value)):
            raise ValueError(expected)
        return value

    return parse


_positive_int = _number(int, "a whole number above 0", lambda v: v > 0)
_positive_float = _number(float, "a number above 0", lambda v: v > 0)
_non_negative_float = _number(float, "a number of 0 or more", lambda v: v >= 0)


def _temperature(raw: str) -> float | None:
    """PROMPT_TEMPERATURE, ANTHROPIC_TEMPERATURE or OPENAI_TEMPERATURE: empty (set, but to
    nothing) sends no temperature at all, for models that reject the field; unset keeps the
    default."""
    if raw == "":
        return None
    try:
        return _non_negative_float(raw)
    except ValueError:
        raise ValueError("empty or a number of 0 or more") from None


def _optional_positive_int(raw: str) -> int | None:
    """An optional number such as OPENROUTER_PRO_MAX_TOKENS or OLLAMA_NUM_CTX: empty (the
    default) means none of its own, so the caller falls back to another setting or sends
    nothing."""
    if raw == "":
        return None
    try:
        return int(_positive_int(raw))
    except ValueError:
        raise ValueError("empty or a whole number above 0") from None


# Longest usage-history retention, 100 years: a longer one overflows the date arithmetic.
MAX_RETENTION_DAYS = 36500
_retention_days = _number(
    int, f"a whole number from 1 to {MAX_RETENTION_DAYS}", lambda v: 0 < v <= MAX_RETENTION_DAYS
)


def _builtin_profiles(raw: str) -> tuple[str, ...]:
    """A comma-separated list of built-in profile names, duplicates dropped."""
    names = tuple(dict.fromkeys(name.strip() for name in raw.split(",") if name.strip()))
    if not names:
        return ()
    from .prompt_builder import PROFILES  # deferred: prompt_builder imports this module

    if any(name not in PROFILES for name in names):
        raise ValueError(f"empty or a comma-separated list of {', '.join(PROFILES)}")
    return names


def _regexes(raw: str) -> str:
    """PROMPT_EXTRA_PATTERNS, kept as text (the gate and the history compile it), but
    rejected here if an entry is not a valid regex. There is no timing probe for a
    catastrophic pattern: it would run on every trigger and flake on a busy machine;
    docs/privacy.md warns against nested quantifiers instead."""
    try:
        compile_extra(raw)
    except InvalidExtraPattern as exc:
        raise ValueError(
            f"valid ';'-separated regexes; entry {exc.entry} (custom_{exc.entry}) is not a "
            "valid regex"
        ) from None
    return raw


TIERS = ("standard", "pro")
# OpenRouter `reasoning.effort` and OpenAI `reasoning_effort` values accepted by --effort.
EFFORTS = ("none", "minimal", "low", "medium", "high")


def _effort(raw: str) -> str:
    # A typo would otherwise reach the API and come back as an opaque HTTP 400.
    if raw.lower() not in ("", *EFFORTS):
        raise ValueError(f"empty or one of {', '.join(EFFORTS)}")
    return raw.lower()


# OpenRouter `provider.data_collection` values: `deny` routes only to endpoints that do not
# store or train on requests; empty sends no field (OpenRouter's account setting applies).
DATA_COLLECTION = ("allow", "deny")


def _data_collection(raw: str) -> str:
    if raw.lower() not in ("", *DATA_COLLECTION):
        raise ValueError(f"empty or one of {', '.join(DATA_COLLECTION)}")
    return raw.lower()


# Where improve puts a successful rewrite (PROMPT_OUTPUT, --output): printed for Espanso to
# paste at the caret, or copied to the clipboard with only markers printed (#134).
PASTE = "paste"
CLIPBOARD = "clipboard"
OUTPUTS = (PASTE, CLIPBOARD)


def _output(raw: str) -> str:
    if raw.lower() not in OUTPUTS:
        raise ValueError(" or ".join(OUTPUTS))
    return raw.lower()


# Option value meaning "keep the configured setting"; the -if- popup's choice lists
# cannot express "unset", so each one offers this word instead.
KEEP = "default"
# --model value suffix `@auto` drops the endpoint pin (OpenRouter's blended routing).
AUTO_ENDPOINT = "auto"


def has_pro_tier(provider: str) -> bool:
    """Whether ``provider`` has the pro tier: OpenRouter alone (its spec's ``router``)."""
    spec = PROVIDERS.get(provider)
    return spec is not None and spec.router


def check_provider_options(provider: str, tier: str, effort: str | None) -> None:
    """Refuse --tier pro for a provider without the pro tier and --effort for one without a
    reasoning-effort setting (registry.ProviderSpec ``router`` and ``effort``); silently
    ignoring them would paste a rewrite the user did not ask for. The neutral values (--tier
    standard, --effort default) pass."""
    spec = PROVIDERS.get(provider)
    refused = []
    if tier == "pro" and not (spec and spec.router):
        refused.append(("--tier pro", [s.label for s in PROVIDERS.values() if s.router]))
    if effort not in (None, KEEP) and not (spec and spec.effort):
        refused.append(("--effort", [s.label for s in PROVIDERS.values() if s.effort]))
    if refused:
        parts = [
            f"{option}{' applies' if i == 0 else ''} only to {' and '.join(labels)}"
            for i, (option, labels) in enumerate(refused)
        ]
        raise ValueError(f"{', '.join(parts)}, not {safe_repr(provider)}")


def split_model_spec(spec: str | None) -> tuple[str | None, str | None]:
    """Split `slug@endpoint` into (slug, endpoint pin); a bare slug has no pin (None).

    The popup's model list carries the endpoint with the model because the configured pin
    (e.g. google-ai-studio/flex) only serves one vendor's models. `@auto` maps to "".
    """
    if not spec or "@" not in spec:
        return spec, None
    model, _, endpoint = spec.partition("@")
    if not model or not endpoint:
        raise ValueError(f"--model must be slug or slug@endpoint, got {safe_repr(spec)}")
    return model, "" if endpoint == AUTO_ENDPOINT else endpoint


def _override[T](raw: str | None, option: str, parse: Callable[[str], T]) -> T | None:
    """Parse an optional per-call override; None or `default` means keep the setting."""
    if raw is None or raw == KEEP:
        return None
    try:
        return parse(raw)
    except ValueError as exc:
        raise ValueError(f"{option} must be {exc} or {KEEP}, got {safe_repr(raw)}") from None


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved at instantiation, not import time."""

    provider: str = _env("PROMPT_PROVIDER", "openrouter")
    profile: str = _env("PROMPT_PROFILE", "default")
    # Opening sentence of the default profile's CONTEXT and of the -p- snippet, e.g.
    # "I am working as a Head of Data at Example Corp." Empty: no fixed persona.
    persona: str = _env("PROMPT_PERSONA", "")
    # paste: improve prints the rewrite for Espanso to paste. clipboard: it copies the rewrite
    # and prints only markers (errors, the sent-despite note, a cut-off note), so a successful
    # trigger just vanishes and the user pastes when ready.
    output: str = _env("PROMPT_OUTPUT", PASTE, _output)
    timeout: float = _env("PROMPT_TIMEOUT_SECONDS", "30", _positive_float)
    # Low by default: the rewrite must reproduce fixed template wordings verbatim.
    # Empty omits the temperature from every OpenRouter, Ollama and LM Studio request (models
    # that reject the field); Anthropic and OpenAI have their own (anthropic_temperature,
    # openai_temperature).
    temperature: float | None = _env("PROMPT_TEMPERATURE", "0.2", _temperature)
    ollama_base_url: str = _env("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = _env("OLLAMA_MODEL", "qwen3:8b")
    ollama_think: bool = _env("OLLAMA_THINK", "false", _bool)
    # options.num_ctx, the context window in tokens (#168): Ollama truncates a prompt longer
    # than it without an error. Empty (the default) sends nothing, so Ollama's own applies.
    ollama_num_ctx: int | None = _env("OLLAMA_NUM_CTX", "", _optional_positive_int)
    openrouter_base_url: str = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    openrouter_model: str = _env("OPENROUTER_MODEL", "google/gemini-3.5-flash-lite")
    # Stripped: a space pasted along with a key is never part of it.
    openrouter_api_key: str = _env("OPENROUTER_API_KEY", "", str.strip, secret=True)
    openrouter_max_tokens: int = _env("OPENROUTER_MAX_TOKENS", "2400", _positive_int)
    # OpenRouter routes one model slug across many hosts, and the host drives latency,
    # cost and template fidelity (see docs/benchmark.md). Pin one
    # endpoint tag; empty string restores OpenRouter's own blended routing.
    openrouter_provider: str = _env("OPENROUTER_PROVIDER", "google-ai-studio/flex")
    # OpenRouter `reasoning.effort` (none/minimal/low/medium/high); empty omits the field
    # for models without a reasoning control. Gemini 3.x cannot switch thinking off, and
    # minimal keeps the inline rewrite at ~2 s.
    openrouter_reasoning_effort: str = _env("OPENROUTER_REASONING_EFFORT", "minimal", _effort)
    # Preference, not constraint: a pinned endpoint can be down, and in Espanso that
    # surfaces as an error marker pasted into the editor. Set false for a hard pin.
    openrouter_allow_fallbacks: bool = _env("OPENROUTER_ALLOW_FALLBACKS", "true", _bool)
    # OpenRouter `provider.data_collection` for every OpenRouter call, both tiers: `deny`
    # routes only to endpoints that do not store or train on requests (a pin that does not
    # qualify then fails). Empty (the default) sends no field, so the body is unchanged.
    openrouter_data_collection: str = _env("OPENROUTER_DATA_COLLECTION", "", _data_collection)
    # The `pro` tier (-ip-): a reasoning model for hard, multi-part drafts. It
    # swaps in these OpenRouter settings; everything else is shared with the default tier.
    openrouter_pro_model: str = _env("OPENROUTER_PRO_MODEL", "openai/gpt-6-luna")
    openrouter_pro_provider: str = _env("OPENROUTER_PRO_PROVIDER", "openai")
    openrouter_pro_reasoning_effort: str = _env("OPENROUTER_PRO_REASONING_EFFORT", "low", _effort)
    # Output cap for the pro tier, whose reasoning counts against it; empty (the default)
    # uses OPENROUTER_MAX_TOKENS, so both tiers share one cap.
    openrouter_pro_max_tokens: int | None = _env(
        "OPENROUTER_PRO_MAX_TOKENS", "", _optional_positive_int
    )
    pro_timeout: float = _env("PROMPT_PRO_TIMEOUT_SECONDS", "60", _positive_float)
    # Profile for the pro tier; empty (the default) uses PROMPT_PROFILE, so both tiers send
    # the same prompt. An escape hatch for a prompt tuned to OPENROUTER_PRO_MODEL.
    pro_profile: str = _env("PROMPT_PRO_PROFILE", "")
    # Built-in profiles that a same-named file in the user profile directory replaces
    # (prompt_builder.user_profiles_dir()). Empty: such a file is ignored, never a silent swap.
    profile_overrides: tuple[str, ...] = _env("PROMPT_PROFILE_OVERRIDES", "", _builtin_profiles)
    lmstudio_base_url: str = _env("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
    lmstudio_model: str = _env("LMSTUDIO_MODEL", "local-model")
    anthropic_base_url: str = _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    anthropic_model: str = _env("ANTHROPIC_MODEL", "claude-sonnet-5")
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY", "", str.strip, secret=True)
    anthropic_max_tokens: int = _env("ANTHROPIC_MAX_TOKENS", "2400", _positive_int)
    # Current Claude models (Opus 4.7, Sonnet 5 and later) return HTTP 400 for a temperature
    # (most for any value, the rest for any but the default, #273), so empty (the default)
    # sends none; a set value is sent as is, for a model that accepts one.
    anthropic_temperature: float | None = _env("ANTHROPIC_TEMPERATURE", "", _temperature)
    # OpenAI's own API (#274), called at /chat/completions with the cap as
    # max_completion_tokens, which its reasoning models require.
    openai_base_url: str = _env("OPENAI_BASE_URL", "https://api.openai.com/v1")
    # The model -ip- runs through OpenRouter (openai/gpt-6-luna), benchmarked at effort low.
    openai_model: str = _env("OPENAI_MODEL", "gpt-6-luna")
    openai_api_key: str = _env("OPENAI_API_KEY", "", str.strip, secret=True)
    openai_max_tokens: int = _env("OPENAI_MAX_TOKENS", "2400", _positive_int)
    # A top-level `reasoning_effort`: without one a reasoning model takes its default effort,
    # too slow for a trigger. Empty omits the field, for a model without a reasoning control.
    openai_reasoning_effort: str = _env("OPENAI_REASONING_EFFORT", "low", _effort)
    # OpenAI's reasoning models return HTTP 400 for any temperature but the default, so empty
    # (the default) sends none; a set value is sent as is, for a model that accepts one.
    openai_temperature: float | None = _env("OPENAI_TEMPERATURE", "", _temperature)
    allow_cloud_override: bool = _env("ALLOW_CLOUD_OVERRIDE", "false", _bool)
    # Refuse every provider that can send the draft off this machine (factory.make_provider),
    # whatever --provider a trigger passes.
    local_only: bool = _env("PROMPT_LOCAL_ONLY", "false", _bool)
    # Also run the data-protection gate for Ollama and LM Studio on loopback (factory.py),
    # for a localhost server that relays to a cloud API. Changes neither PROMPT_LOCAL_ONLY nor
    # what counts as leaving this machine.
    gate_local: bool = _env("PROMPT_GATE_LOCAL", "false", _bool)
    # Extra `;`-separated regexes the data-protection gate blocks on, e.g. internal project
    # code names or customer-ID formats. Compiled by redaction.compile_extra().
    extra_patterns: str = _env("PROMPT_EXTRA_PATTERNS", "", _regexes)
    # Local usage history (history.py): metadata only, never prompt, clipboard, output,
    # persona or key text, kept in user_data_dir() on this device. false records nothing.
    history: bool = _env("PROMPT_HISTORY", "true", _bool)
    # Days a history record is kept; HistoryStore.prune() deletes older ones.
    history_retention_days: int = _env("PROMPT_HISTORY_RETENTION_DAYS", "365", _retention_days)
    # Whether doctor and the interface's Home ask PyPI, at most once a day, for a newer
    # release (update_check.py, #197); false makes no request. The triggers never ask.
    update_check: bool = _env("PROMPT_UPDATE_CHECK", "true", _bool)
    # Not a setting (no env var, see setting_fields()): the --max-tokens of one call, for the
    # providers without a cap setting (Ollama num_predict, LM Studio max_tokens). None sends
    # them no cap, as before.
    call_max_tokens: int | None = field(default=None)

    @classmethod
    def load(cls) -> Settings:
        """Settings from ConfigLayers.resolve(): default < config.toml or .env < secret store
        < real environment. Each call reads the files again, and os.environ is never
        written."""
        return ConfigLayers.resolve().settings()

    def for_tier(self, tier: str) -> Settings:
        """Settings for a quality tier: `standard` as-is, `pro` with the OPENROUTER_PRO_*
        model, endpoint, effort and output cap (if set), and PROMPT_PRO_PROFILE if set. Only
        OpenRouter has a pro tier."""
        if tier == "standard":
            return self
        if tier == "pro":
            return replace(
                self,
                openrouter_model=self.openrouter_pro_model,
                openrouter_provider=self.openrouter_pro_provider,
                openrouter_reasoning_effort=self.openrouter_pro_reasoning_effort,
                openrouter_max_tokens=self.openrouter_pro_max_tokens or self.openrouter_max_tokens,
                timeout=self.pro_timeout,
                profile=self.pro_profile or self.profile,
            )
        raise ValueError(f"Unknown tier: {safe_repr(tier)}. Choose from: {', '.join(TIERS)}")

    def for_call(
        self,
        tier: str,
        *,
        provider: str = "openrouter",
        model: str | None = None,
        effort: str | None = None,
        max_tokens: str | None = None,
        timeout: str | None = None,
        output: str | None = None,
    ) -> Settings:
        """Settings for one CLI call on ``provider``: for_tier(), then the per-call overrides.
        Only OpenRouter has a pro tier (has_pro_tier()), so another provider keeps the standard
        settings (the tier name is still checked). A set PROMPT_PRO_PROFILE is meant for
        OPENROUTER_PRO_MODEL, so a pro call that runs another model (one picked in the -if-
        popup) uses PROMPT_PROFILE instead."""
        pro = tier == "pro" and has_pro_tier(provider)
        tiered = self.for_tier(tier)
        cfg = (tiered if pro else self).with_overrides(
            model=model, effort=effort, max_tokens=max_tokens, timeout=timeout, output=output
        )
        if pro and cfg.openrouter_model != self.openrouter_pro_model:
            return replace(cfg, profile=self.profile)
        return cfg

    def with_overrides(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        max_tokens: str | None = None,
        timeout: str | None = None,
        output: str | None = None,
    ) -> Settings:
        """Per-call overrides from the CLI (the -if- popup, --output). Values arrive as strings;
        None or `default` keeps the setting. ``model`` applies to whichever provider runs, and its
        `slug@endpoint` form also sets the OpenRouter pin. Bad values raise ValueError, which
        the CLI prints inline."""
        if effort not in (None, KEEP, *EFFORTS):
            raise ValueError(f"--effort must be one of {', '.join((KEEP, *EFFORTS))}")
        slug, endpoint = split_model_spec(None if model == KEEP else model)
        tokens = _override(max_tokens, "--max-tokens", _positive_int)
        seconds = _override(timeout, "--timeout", _positive_float)
        destination = _override(output, "--output", _output)
        changes: dict[str, Any] = {}
        if slug:
            changes |= dict.fromkeys((f"{name}_model" for name in PROVIDERS), slug)
        if endpoint is not None:
            changes["openrouter_provider"] = endpoint
        if effort not in (None, KEEP):
            changes |= dict.fromkeys(
                [spec.effort for spec in PROVIDERS.values() if spec.effort], effort
            )
        if tokens is not None:
            caps = [spec.cap for spec in PROVIDERS.values() if spec.cap]
            changes |= dict.fromkeys([*caps, "call_max_tokens"], tokens)
        if seconds is not None:
            changes["timeout"] = seconds
        if destination is not None:
            changes["output"] = destination
        return replace(self, **changes) if changes else self


# Provenance of a setting value: a built-in default, a settings file (`file:<path>`: a .env,
# config.toml or secrets.toml) or the real environment.
DEFAULT_SOURCE = "default"
ENV_SOURCE = "env"


@dataclass(frozen=True)
class Layer:
    """One source of setting values, as unparsed strings, keyed by env var name."""

    source: str
    values: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class Entry:
    """A setting's effective value and the layer it came from. ``shadows`` lists the other
    layers (not the default) that also set it and lost, e.g. a .env value overridden by a
    real environment variable. ``rejected`` lists the higher layers whose value repair mode
    refused (see ConfigLayers.findings), so the value fell back to this one."""

    value: str = field(repr=False)  # may be an API key
    source: str
    shadows: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    """A problem repair mode skipped instead of raising; ``message`` is strict mode's error."""

    source: str
    message: str


@dataclass(frozen=True)
class ConfigLayers:
    """The layers that make up the settings, lowest precedence first, and the effective
    value of every setting in env_names().

    A pure merge: building it reads the environment mapping and the .env file but never
    writes os.environ, so a second resolve sees an edited file and child processes inherit
    nothing from it.
    """

    layers: tuple[Layer, ...]
    entries: Mapping[str, Entry]
    findings: tuple[Finding, ...] = ()

    @classmethod
    def resolve(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        strict: bool = True,
        only: str | None = None,
    ) -> ConfigLayers:
        """Merge default < saved settings < secret store < ``environ`` (os.environ by default).

        The saved settings are the .env named by PROMPTMEND_ENV (legacy mode: that file alone,
        no config.toml or secret store), else config.toml in the user config dir. No other
        .env is ever read: repair mode reports one left in the user config dir (#275). The
        secret store (secrets.toml) is read outside legacy mode.

        Only env_names() keys are taken from either source: any other key (HTTP_PROXY,
        SSL_CERT_FILE, ...) would change how httpx connects, which is not what a settings
        file is for. Strict mode (``improve``, ``persona``) raises the first problem as a
        ValueError. Repair mode records each as a Finding instead and falls back to the next
        lower layer, so management commands still run on a broken config. Repair mode also
        notes what strict mode skips silently: a .env that exists but cannot be read, a line
        without `=` (by number, never its text), and the unread .env.

        With ``only`` (a setting name), strict mode raises only the problems that can change
        that setting: an unreadable settings file, or its own value. Another setting's bad
        value or a failing secret store (``only`` not a secret) is recorded as a finding and
        falls back as in repair mode, so ``persona`` still finds PROMPT_PERSONA when an
        unrelated setting is broken; a file problem is tolerated too when ``only`` is set in
        ``environ``. Only ``entries[only]`` is meant to be read then.

        PROMPT_EXTRA_PATTERNS resolves first and is registered with
        redaction.set_user_patterns(), so safe_repr() describes any later rejected value
        that matches a user pattern. An error raised before that (a file-level one, a merged
        line) is quoted with the built-in patterns only, as is a Click usage error before
        any settings load; a later load that fails keeps the patterns registered last.
        """
        environ = os.environ if environ is None else environ
        findings: list[Finding] = []

        def note(source: str, message: str) -> None:
            # Repair mode only: strict mode has always skipped these silently.
            if not strict:
                findings.append(Finding(source, message))

        def fail(source: str, message: str, affects: tuple[str, ...] | None = None) -> None:
            # ``affects``: the settings the problem can change; None means any of them, except
            # one set in the real environment, which no file problem can change.
            if strict and (
                only is None
                or (affects is None and only not in environ)
                or (affects is not None and only in affects)
            ):
                raise ValueError(message)
            findings.append(Finding(source, message))

        def fail_secrets(source: str, message: str) -> None:
            fail(source, message, secret_names())

        known = env_names()
        defaults = {f.metadata["env"]: f.metadata["default"] for f in setting_fields()}
        layers = [Layer(DEFAULT_SOURCE, defaults)]
        named = env_file_override(environ)
        if named is None:
            saved = _saved_layer(settings_file(environ), fail, note)
            if saved is not None:
                layers.append(saved)
            if not strict and (unread := unread_env_file(environ)) is not None:
                note(
                    f"file:{unread}",
                    ".env is no longer read: save its settings with `promptmend setup` (or set "
                    f"{ENV_FILE_VAR} to it), then remove it",
                )
            if secrets := _secrets_layer(user_config_dir(environ), strict, fail_secrets, note):
                layers.append(secrets)
        elif (pairs := _read_env_file(Path(named).expanduser(), note, fail)) is not None:
            source = f"file:{Path(named).expanduser()}"
            merged = _merged_lines(pairs)
            for key in merged:
                fail(source, _merged_line_error(key), (key,))
            values = {k: v for k, v in pairs.items() if k in known and k not in merged}
            layers.append(Layer(source, values))
        layers.append(Layer(ENV_SOURCE, {k: environ[k] for k in known if k in environ}))

        entries: dict[str, Entry] = {}
        # PROMPT_EXTRA_PATTERNS first: once it is known, every other rejected value that
        # matches one of the user's patterns is described, never quoted (safe_repr).
        ordered = sorted(setting_fields(), key=lambda f: f.metadata["env"] != _EXTRA)
        for f in ordered:
            name = f.metadata["env"]
            setters = [layer for layer in reversed(layers) if name in layer.values]
            rejected: list[str] = []
            for index, layer in enumerate(setters):
                raw = layer.values[name]
                try:
                    parse_setting(name, f.metadata["parse"], raw)
                except ValueError as exc:
                    fail(layer.source, str(exc), (name,))
                    rejected.append(layer.source)
                    continue
                lost = setters[index + 1 :]
                shadows = tuple(s.source for s in lost if s.source != DEFAULT_SOURCE)
                entries[name] = Entry(raw, layer.source, shadows, tuple(rejected))
                if name == _EXTRA:
                    set_user_patterns(compile_extra(raw))
                break
        entries = {name: entries[name] for name in known if name in entries}
        return cls(tuple(layers), entries, tuple(findings))

    def settings(self) -> Settings:
        """Settings built from the effective values."""
        return Settings(
            **{
                f.name: parse_setting(
                    f.metadata["env"], f.metadata["parse"], self.entries[f.metadata["env"]].value
                )
                for f in setting_fields()
            }
        )
