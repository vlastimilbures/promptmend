"""The setting value parsers: a raw string in, a typed value or a ValueError (what the
value must be) out; and the values they accept."""

from __future__ import annotations

import math
from collections.abc import Callable

from .providers.registry import PROVIDERS
from .redaction import GATE_PACKS, InvalidExtraPattern, compile_extra


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


def _gate_packs(raw: str) -> tuple[str, ...]:
    """PROMPT_GATE_PACKS: a comma-separated list of redaction.GATE_PACKS names, duplicates
    dropped; empty turns every pack off. An unknown name is an error, never a pack silently
    left out of the gate."""
    names = tuple(dict.fromkeys(name.strip().lower() for name in raw.split(",") if name.strip()))
    if any(name not in GATE_PACKS for name in names):
        raise ValueError(f"empty or a comma-separated list of {', '.join(GATE_PACKS)}")
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


# The providers with the pro tier (registry.ProviderSpec ``pro``): PROMPT_PRO_PROVIDER's
# values, and the ones --tier pro accepts.
PRO_PROVIDERS = tuple(name for name, spec in PROVIDERS.items() if spec.pro)
# OpenRouter `reasoning.effort` and OpenAI `reasoning_effort` values accepted by --effort.
EFFORTS = ("none", "minimal", "low", "medium", "high")
# Anthropic Messages API `output_config.effort` values (ANTHROPIC_EFFORT). Not --effort's set:
# Anthropic has no none or minimal, so --effort stays refused for Anthropic.
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def is_valid_url(url: str) -> bool:
    """Whether ``url`` is an http:// or https:// URL that urlsplit() can take apart, with a
    host and a usable port: `http://[::1` (no closing bracket) and `http://host:99999` make
    urlsplit() or ``.port`` raise ValueError, which would surface later as a traceback."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a port out of range or not a number
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _base_url(raw: str) -> str:
    """A provider's base URL (#368). Whether it must be https is make_provider()'s check: a
    cloud provider may use plain http only to loopback."""
    if not is_valid_url(raw):
        raise ValueError("an http:// or https:// URL with a valid host and port")
    return raw


def _effort(raw: str) -> str:
    # A typo would otherwise reach the API and come back as an opaque HTTP 400.
    if raw.lower() not in ("", *EFFORTS):
        raise ValueError(f"empty or one of {', '.join(EFFORTS)}")
    return raw.lower()


def _anthropic_effort(raw: str) -> str:
    if raw.lower() not in ("", *ANTHROPIC_EFFORTS):
        raise ValueError(f"empty or one of {', '.join(ANTHROPIC_EFFORTS)}")
    return raw.lower()


# OpenRouter `provider.data_collection` values: `deny` routes only to endpoints that do not
# store or train on requests; empty sends no field (OpenRouter's account setting applies).
DATA_COLLECTION = ("allow", "deny")


# PROMPT_PRO_PROVIDER's default, which an empty value also means.
DEFAULT_PRO_PROVIDER = "openrouter"


def _pro_provider(raw: str) -> str:
    """PROMPT_PRO_PROVIDER: a provider with the pro tier; empty means the default, as for a
    line left as `PROMPT_PRO_PROVIDER=`. Strict otherwise: a typo would surface only when -ip-
    runs."""
    name = raw.strip().lower() or DEFAULT_PRO_PROVIDER
    if name not in PRO_PROVIDERS:
        raise ValueError(f"empty or one of {', '.join(PRO_PROVIDERS)}")
    return name


def _data_collection(raw: str) -> str:
    if raw.lower() not in ("", *DATA_COLLECTION):
        raise ValueError(f"empty or one of {', '.join(DATA_COLLECTION)}")
    return raw.lower()


# Where improve puts a successful rewrite (PROMPT_OUTPUT, --output): printed for Espanso to
# paste at the caret, or copied to the clipboard with only markers printed.
PASTE = "paste"
CLIPBOARD = "clipboard"
OUTPUTS = (PASTE, CLIPBOARD)


def _output(raw: str) -> str:
    if raw.lower() not in OUTPUTS:
        raise ValueError(" or ".join(OUTPUTS))
    return raw.lower()
