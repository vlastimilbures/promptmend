"""Per-attempt usage metadata: tokens, cost and status, reported to an optional observer.

Every HTTP attempt post_json() makes for a provider that was given an observer produces one
AttemptUsage, both attempts of a retry included, and failed ones too. A record holds only
sanitised metadata: never the prompt, the response text, a raw body or a key.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    # Imported where a cost is parsed, not here: _decimal costs a few ms on every trigger.
    from decimal import Decimal

CostState = Literal["reported", "estimated", "unknown", "not_applicable"]
# Longest model, provider or generation id kept from a response; anything longer is dropped.
_ID_MAX = 200


@dataclass(frozen=True)
class AttemptUsage:
    """One HTTP attempt, as sanitised metadata.

    ``returned_model``, ``returned_provider`` and ``generation_id`` are set only when the
    response reports them. ``error_kind`` is None for a 2xx reply with no error inside it;
    otherwise one of ``timeout``, ``non_2xx``, ``invalid_json``, ``body_error``, ``refused``,
    ``transport``, ``invalid_url``, ``invalid_header`` or ``unexpected``.

    Every token count is None when the response does not report it. ``input_uncached``,
    ``cache_read``, ``cache_write`` and ``output`` do not overlap; ``reasoning`` is the part of
    ``output`` the model spent reasoning, where the provider reports it.

    ``charged_amount`` is what the provider charged, in ``charged_unit``; ``upstream_cost`` is
    what an upstream charged a bring-your-own-key account (OpenRouter BYOK), never included in
    ``charged_amount``. A cost the response does not report is None with ``cost_state``
    ``unknown``, never 0; a reported 0 is 0 with ``reported``. Inference known to run on this
    machine is ``not_applicable``.
    """

    provider: str
    requested_model: str
    endpoint: Literal["loopback", "remote"]
    attempt: int
    status: int | None
    error_kind: str | None
    latency_ms: float
    cost_state: CostState = "unknown"
    returned_model: str | None = None
    returned_provider: str | None = None
    generation_id: str | None = None
    input_uncached: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    output: int | None = None
    reasoning: int | None = None
    charged_amount: Decimal | None = None
    charged_unit: Literal["credits", "USD"] | None = None
    upstream_cost: Decimal | None = None
    upstream_unit: Literal["credits", "USD"] | None = None
    # Ollama's own timings for the reply, in milliseconds.
    server_total_ms: float | None = None
    server_load_ms: float | None = None
    server_input_ms: float | None = None
    server_output_ms: float | None = None


class UsageObserver(Protocol):
    """Receives one AttemptUsage per HTTP attempt. An exception it raises is swallowed: it
    never changes the output and never causes a retry.

    It runs synchronously inside the call, while Espanso waits, so it must be quick and never
    do blocking I/O: collect the records and write them after generate() returns. Its time is
    not charged to the call's time limit, so a slow observer cannot cost the retry."""

    def __call__(self, usage: AttemptUsage, /) -> None: ...


# Adds what a provider's response body reports to the transport facts of an attempt.
UsageParser = Callable[[Mapping[str, object], AttemptUsage], AttemptUsage]


@dataclass(frozen=True)
class Meter:
    """What post_json() needs to report a provider's attempts: the observer, the provider and
    requested model, the provider's body parser, and whether inference runs on this machine
    (a loopback Ollama or LM Studio that is not a cloud model), which has no cost to report."""

    observer: UsageObserver
    provider: str
    model: str
    parse: UsageParser
    local: bool = False

    def record(
        self,
        *,
        loopback: bool,
        attempt: int,
        status: int | None,
        error_kind: str | None,
        body: object,
        latency: float,
    ) -> None:
        """Build the attempt's record and pass it to the observer; nothing raised here,
        by a parser or by the observer, reaches the caller. A body the parser cannot read
        still leaves the transport facts on record, with no tokens and an unknown cost."""
        base = AttemptUsage(
            provider=self.provider,
            requested_model=self.model,
            endpoint="loopback" if loopback else "remote",
            attempt=attempt,
            status=status,
            error_kind=error_kind,
            latency_ms=round(latency * 1000, 1),
            cost_state="not_applicable" if self.local else "unknown",
        )
        usage = base
        if isinstance(body, Mapping):
            try:
                usage = self.parse(body, base)
            except Exception:
                usage = base
        if usage.charged_amount is not None:
            usage = dataclasses.replace(usage, cost_state="reported")
        with contextlib.suppress(Exception):
            self.observer(usage)


def _count(value: object) -> int | None:
    """A token count, or None for anything that is not a non-negative integer."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _ident(value: object) -> str | None:
    """A short printable identifier (model, provider, generation id), or None."""
    if isinstance(value, str) and 0 < len(value) <= _ID_MAX and value.isprintable():
        return value
    return None


def _money(value: object) -> Decimal | None:
    """A finite, non-negative amount as a Decimal (exact for the JSON text), or None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    # A huge JSON integer (10**400) overflows the float conversion inside isfinite.
    try:
        if not math.isfinite(value) or value < 0:
            return None
    except OverflowError:
        return None
    from decimal import Decimal

    return Decimal(str(value))


def _ms(nanoseconds: object) -> float | None:
    count = _count(nanoseconds)
    return None if count is None else round(count / 1_000_000, 1)


def _dict(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _common(body: Mapping[str, object], usage: AttemptUsage) -> AttemptUsage:
    return dataclasses.replace(
        usage, returned_model=_ident(body.get("model")), generation_id=_ident(body.get("id"))
    )


def openai_usage(body: Mapping[str, object], usage: AttemptUsage) -> AttemptUsage:
    """Token counts from an OpenAI-compatible reply (OpenAI, LM Studio, a custom endpoint),
    which reports no cost.

    ``prompt_tokens`` includes the cached tokens, which are taken out of ``input_uncached``;
    with no cache details it is all counted as uncached.
    """
    data = _dict(body.get("usage"))
    prompt = _dict(data.get("prompt_tokens_details"))
    completion = _dict(data.get("completion_tokens_details"))
    cache_read = _count(prompt.get("cached_tokens"))
    cache_write = _count(prompt.get("cache_write_tokens"))
    uncached = _count(data.get("prompt_tokens"))
    if uncached is not None:
        uncached = _count(uncached - (cache_read or 0) - (cache_write or 0))
    return dataclasses.replace(
        _common(body, usage),
        input_uncached=uncached,
        cache_read=cache_read,
        cache_write=cache_write,
        output=_count(data.get("completion_tokens")),
        reasoning=_count(completion.get("reasoning_tokens")),
    )


def openrouter_usage(body: Mapping[str, object], usage: AttemptUsage) -> AttemptUsage:
    """OpenRouter's reply: the OpenAI-compatible tokens, the serving provider, ``usage.cost``
    in credits and, for a BYOK call, the upstream's cost in USD, kept apart from it."""
    usage = openai_usage(body, usage)
    data = _dict(body.get("usage"))
    cost = _money(data.get("cost"))
    upstream = _money(_dict(data.get("cost_details")).get("upstream_inference_cost"))
    return dataclasses.replace(
        usage,
        returned_provider=_ident(body.get("provider")),
        charged_amount=cost,
        charged_unit="credits" if cost is not None else None,
        upstream_cost=upstream,
        upstream_unit="USD" if upstream is not None else None,
    )


def anthropic_usage(body: Mapping[str, object], usage: AttemptUsage) -> AttemptUsage:
    """Anthropic's four token counts, which do not overlap; it reports no cost."""
    data = _dict(body.get("usage"))
    return dataclasses.replace(
        _common(body, usage),
        input_uncached=_count(data.get("input_tokens")),
        cache_read=_count(data.get("cache_read_input_tokens")),
        cache_write=_count(data.get("cache_creation_input_tokens")),
        output=_count(data.get("output_tokens")),
    )


def ollama_usage(body: Mapping[str, object], usage: AttemptUsage) -> AttemptUsage:
    """Ollama's prompt and output token counts and its timings (reported in nanoseconds)."""
    return dataclasses.replace(
        usage,
        returned_model=_ident(body.get("model")),
        input_uncached=_count(body.get("prompt_eval_count")),
        output=_count(body.get("eval_count")),
        server_total_ms=_ms(body.get("total_duration")),
        server_load_ms=_ms(body.get("load_duration")),
        server_input_ms=_ms(body.get("prompt_eval_duration")),
        server_output_ms=_ms(body.get("eval_duration")),
    )
