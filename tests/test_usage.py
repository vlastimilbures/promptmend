from __future__ import annotations

import dataclasses
import ssl
from collections.abc import Callable, Mapping
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from promptmend.config import Settings
from promptmend.factory import make_provider
from promptmend.prompt_builder import PERSONA_TOKEN, render
from promptmend.providers import base
from promptmend.providers.anthropic import AnthropicProvider
from promptmend.providers.base import Provider, ProviderError
from promptmend.providers.ollama import OllamaProvider
from promptmend.providers.openai_compatible import OpenAICompatibleProvider
from promptmend.providers.usage import (
    AttemptUsage,
    Meter,
    UsageObserver,
    anthropic_usage,
    ollama_usage,
    openai_usage,
    openrouter_usage,
)

if TYPE_CHECKING:
    from conftest import FakeHttp


def _openai_body(
    text: str = "ok", usage: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        **extra,
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _openrouter(
    monkeypatch: pytest.MonkeyPatch, records: list[AttemptUsage], **env: str
) -> Provider:
    # Built at runtime, so a secret scanner never sees a key-shaped literal.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-" + "t" * 12)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return make_provider("openrouter", Settings(), observer=records.append)


# --- Per-provider mapping. ------------------------------------------------------------------


# OpenRouter: tokens with cache details taken out of the uncached input, the serving provider,
# the credits charged and, for a BYOK call, the upstream's USD cost kept apart from them.
def test_openrouter_mapping(fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[AttemptUsage] = []
    fake_http.reply(
        _openai_body(
            "rewritten",
            id="gen-123",
            model="google/gemini-flash-lite",
            provider="Google",
            usage={
                "prompt_tokens": 1000,
                "completion_tokens": 300,
                "prompt_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100},
                "completion_tokens_details": {"reasoning_tokens": 120},
                "cost": 0.000123,
                "is_byok": True,
                "cost_details": {"upstream_inference_cost": 0.0042},
            },
        )
    )
    provider = _openrouter(monkeypatch, records, OPENROUTER_MODEL="google/gemini-flash-lite")
    assert provider.generate("draft", "sys") == "rewritten"
    (record,) = records
    assert record == AttemptUsage(
        provider="openrouter",
        requested_model="google/gemini-flash-lite",
        endpoint="remote",
        attempt=1,
        status=200,
        error_kind=None,
        latency_ms=record.latency_ms,
        cost_state="reported",
        returned_model="google/gemini-flash-lite",
        returned_provider="Google",
        generation_id="gen-123",
        input_uncached=300,
        cache_read=600,
        cache_write=100,
        output=300,
        reasoning=120,
        charged_amount=Decimal("0.000123"),
        charged_unit="credits",
        upstream_cost=Decimal("0.0042"),
        upstream_unit="USD",
    )
    assert record.latency_ms >= 0


# The request no longer asks for usage accounting: OpenRouter always returns it.
def test_openrouter_request_has_no_usage_include(
    fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_http.reply(_openai_body())
    _openrouter(monkeypatch, []).generate("draft", "sys")
    assert "usage" not in fake_http.calls[0]["json"]


# A cost of 0 is reported as 0; a missing cost is unknown, never 0.
@pytest.mark.parametrize(
    ("usage", "amount", "unit", "state"),
    [
        ({"cost": 0}, Decimal(0), "credits", "reported"),
        ({"cost": 0.0}, Decimal("0.0"), "credits", "reported"),
        ({"prompt_tokens": 5}, None, None, "unknown"),
        (None, None, None, "unknown"),
        ({"cost": None}, None, None, "unknown"),
        ({"cost": "0.1"}, None, None, "unknown"),
        ({"cost": True}, None, None, "unknown"),
        ({"cost": -1}, None, None, "unknown"),
    ],
)
def test_openrouter_cost_state(
    fake_http: FakeHttp,
    monkeypatch: pytest.MonkeyPatch,
    usage: dict[str, Any] | None,
    amount: Decimal | None,
    unit: str | None,
    state: str,
) -> None:
    records: list[AttemptUsage] = []
    fake_http.reply(_openai_body(usage=usage))
    _openrouter(monkeypatch, records).generate("draft", "sys")
    (record,) = records
    assert (record.charged_amount, record.charged_unit, record.cost_state) == (amount, unit, state)
    if amount is not None:
        assert record.charged_amount == 0
    assert record.upstream_cost is None


# LM Studio on this machine: tokens if present, and no cost to report.
def test_lmstudio_local_mapping(fake_http: FakeHttp) -> None:
    records: list[AttemptUsage] = []
    fake_http.reply(
        _openai_body(model="qwen3-8b", usage={"prompt_tokens": 40, "completion_tokens": 9})
    )
    provider = make_provider("lmstudio", Settings(), observer=records.append)
    assert provider.generate("draft", "sys") == "ok"
    (record,) = records
    assert record.provider == "lmstudio"
    assert record.endpoint == "loopback"
    assert (record.input_uncached, record.output, record.cache_read) == (40, 9, None)
    assert record.returned_model == "qwen3-8b"
    assert (record.charged_amount, record.cost_state) == (None, "not_applicable")
    assert record.returned_provider is None


# An LM Studio on another machine is not verified local inference: its cost is unknown, and a
# usage.cost it sends is not read (only OpenRouter's unit is known).
def test_lmstudio_remote_cost_is_unknown(
    fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "https://lm.example.com/v1")
    records: list[AttemptUsage] = []
    fake_http.reply(_openai_body(usage={"prompt_tokens": 4, "cost": 0.5}))
    make_provider("lmstudio", Settings(), observer=records.append).generate("draft", "sys")
    (record,) = records
    assert (record.endpoint, record.cost_state, record.charged_amount) == (
        "remote",
        "unknown",
        None,
    )
    assert record.input_uncached == 4


# Anthropic: the four non-overlapping token counts; no cost is reported.
def test_anthropic_mapping(fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "t" * 12)
    records: list[AttemptUsage] = []
    fake_http.reply(
        {
            "id": "msg_01",
            "model": "claude-x",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 12,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 50,
                "output_tokens": 77,
            },
        }
    )
    make_provider("anthropic", Settings(), observer=records.append).generate("draft", "sys")
    (record,) = records
    assert record.provider == "anthropic"
    assert (record.input_uncached, record.cache_read, record.cache_write, record.output) == (
        12,
        800,
        50,
        77,
    )
    assert (record.generation_id, record.returned_model) == ("msg_01", "claude-x")
    assert (record.charged_amount, record.cost_state, record.reasoning) == (None, "unknown", None)


_OLLAMA_BODY = {
    "model": "qwen3:8b",
    "message": {"content": "ok"},
    "done_reason": "stop",
    "prompt_eval_count": 31,
    "eval_count": 17,
    "total_duration": 2_500_000_000,
    "load_duration": 4_000_000,
    "prompt_eval_duration": 120_000_000,
    "eval_duration": 2_300_000_000,
}


# Ollama on this machine: prompt and output tokens, its timings in ms, no cost.
def test_ollama_local_mapping(fake_http: FakeHttp) -> None:
    records: list[AttemptUsage] = []
    fake_http.reply(_OLLAMA_BODY)
    make_provider("ollama", Settings(), observer=records.append).generate("draft", "sys")
    (record,) = records
    assert (record.provider, record.endpoint, record.cost_state) == (
        "ollama",
        "loopback",
        "not_applicable",
    )
    assert (record.input_uncached, record.output) == (31, 17)
    assert (record.server_total_ms, record.server_load_ms) == (2500.0, 4.0)
    assert (record.server_input_ms, record.server_output_ms) == (120.0, 2300.0)
    assert record.returned_model == "qwen3:8b"


# A cloud-tagged Ollama model runs on ollama.com even through a loopback server: unknown.
def test_ollama_cloud_model_cost_is_unknown(
    fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "gpt-oss:120b-cloud")
    records: list[AttemptUsage] = []
    fake_http.reply(_OLLAMA_BODY)
    make_provider("ollama", Settings(), observer=records.append).generate("draft", "sys")
    (record,) = records
    assert (record.endpoint, record.cost_state) == ("loopback", "unknown")


# A provider built directly, without the factory's verdict, never claims local inference.
def test_unverified_endpoint_defaults_to_unknown(fake_http: FakeHttp) -> None:
    records: list[AttemptUsage] = []
    fake_http.reply(_OLLAMA_BODY)
    OllamaProvider("http://localhost:11434", "m", observer=records.append).generate("d", "s")
    assert records[0].cost_state == "unknown"


# Counts that are not non-negative integers, and identifiers that are not short printable
# strings, are dropped rather than stored.
def test_parsers_drop_malformed_values() -> None:
    base = AttemptUsage("p", "m", "remote", 1, 200, None, 1.0)
    odd = {
        "id": "x" * 500,
        "model": "bad\nmodel",
        "provider": 7,
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": True,
            "prompt_tokens_details": {"cached_tokens": 9},
            "completion_tokens_details": "n/a",
        },
    }
    record = openrouter_usage(odd, base)
    assert (record.generation_id, record.returned_model, record.returned_provider) == (
        None,
        None,
        None,
    )
    # More cached tokens than prompt tokens is inconsistent: the uncached count is unknown.
    assert (record.input_uncached, record.cache_read, record.output) == (None, 9, None)
    assert openai_usage({"usage": "none"}, base) == base
    # json.loads accepts Infinity and NaN; neither is a cost.
    for cost in (float("inf"), float("nan")):
        assert openrouter_usage({"usage": {"cost": cost}}, base).charged_amount is None
    assert anthropic_usage({"usage": {"input_tokens": -3}}, base).input_uncached is None
    assert ollama_usage({"eval_count": 1.5, "total_duration": "x"}, base) == base


# --- Attempts: retries, timeouts, failures. ------------------------------------------------


def _remote_ollama(records: list[AttemptUsage], timeout: float = 30) -> OllamaProvider:
    return OllamaProvider("http://x", "m", timeout=timeout, observer=records.append)


# A retried call (503, then 200) produces two records, one per attempt.
def test_retry_produces_two_records(fake_http: FakeHttp) -> None:
    records: list[AttemptUsage] = []
    fake_http.queue({"status_code": 503}, {"json_data": _OLLAMA_BODY})
    assert _remote_ollama(records).generate("d", "s") == "ok"
    assert [(r.attempt, r.status, r.error_kind) for r in records] == [
        (1, 503, "non_2xx"),
        (2, 200, None),
    ]
    assert records[0].output is None
    assert records[1].output == 17


# A timeout produces one record and no retry.
def test_timeout_produces_one_record(fake_http: FakeHttp) -> None:
    records: list[AttemptUsage] = []
    fake_http.queue(httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError, match="timed out"):
        _remote_ollama(records).generate("d", "s")
    assert [(r.attempt, r.status, r.error_kind) for r in records] == [(1, None, "timeout")]
    assert records[0].cost_state == "unknown"


# Each way an attempt can fail is named in its record.
@pytest.mark.parametrize(
    ("reply", "kind", "status"),
    [
        ({"bad_json": True}, "invalid_json", 200),
        ({"status_code": 400}, "non_2xx", 400),
        ({"json_data": {"error": {"code": 400, "message": "no"}}}, "body_error", 200),
        (httpx.RemoteProtocolError("dropped"), "transport", None),
    ],
)
def test_failed_attempt_is_recorded(
    fake_http: FakeHttp, reply: dict[str, Any] | Exception, kind: str, status: int | None
) -> None:
    records: list[AttemptUsage] = []
    fake_http.queue(reply)
    with pytest.raises(ProviderError):
        _remote_ollama(records).generate("d", "s")
    assert [(r.error_kind, r.status) for r in records] == [(kind, status)]


# A refused connection to another machine is retried: both attempts are recorded.
def test_refused_connection_records_each_attempt(fake_http: FakeHttp) -> None:
    records: list[AttemptUsage] = []
    fake_http.queue(httpx.ConnectError("refused"), httpx.ConnectError("refused"))
    with pytest.raises(ProviderError, match="refused"):
        _remote_ollama(records).generate("d", "s")
    assert [(r.attempt, r.error_kind) for r in records] == [(1, "refused"), (2, "refused")]


def _tls_connect_error(cause: ssl.SSLError) -> httpx.ConnectError:
    """A ConnectError chained as httpx raises it: from httpcore's ConnectError, from ssl."""
    middle = OSError("httpcore.ConnectError")
    middle.__cause__ = cause
    failure = httpx.ConnectError(cause.args[-1])
    failure.__cause__ = middle
    return failure


# A certificate failure (httpx reports it as a ConnectError) fails the same way again, so it
# is reported at once and not as transient: one request, one record, the same marker text.
def test_certificate_failure_is_not_retried(fake_http: FakeHttp) -> None:
    records: list[AttemptUsage] = []
    cause = ssl.SSLCertVerificationError("certificate verify failed")
    fake_http.queue(_tls_connect_error(cause), {"json_data": _OLLAMA_BODY})
    with pytest.raises(
        ProviderError, match=r"^Ollama request failed: certificate verify"
    ) as caught:
        _remote_ollama(records).generate("d", "s")
    assert caught.value.transient is False
    assert len(fake_http.requests) == 1
    assert [(r.attempt, r.error_kind) for r in records] == [(1, "refused")]


# A handshake the network dropped may pass on a second try, so it keeps its retry.
def test_dropped_handshake_is_retried(fake_http: FakeHttp) -> None:
    records: list[AttemptUsage] = []
    cause = ssl.SSLEOFError("EOF occurred in violation of protocol")
    fake_http.queue(_tls_connect_error(cause), {"json_data": _OLLAMA_BODY})
    assert _remote_ollama(records).generate("d", "s") == "ok"
    assert len(fake_http.requests) == 2


# Usage reaches the observer before the content is finalised, so a 200 whose content is then
# rejected still leaves its tokens and cost on record.
def test_usage_kept_when_finalize_fails(
    fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    records: list[AttemptUsage] = []
    fake_http.reply(
        _openai_body(
            "",
            usage={"prompt_tokens": 50, "completion_tokens": 400, "cost": 0.002},
        )
    )
    with pytest.raises(ProviderError, match="empty content"):
        _openrouter(monkeypatch, records).generate("draft", "sys")
    (record,) = records
    assert (record.status, record.error_kind, record.output) == (200, None, 400)
    assert (record.charged_amount, record.cost_state) == (Decimal("0.002"), "reported")


# The gate blocks before any request is sent, so no attempt is recorded.
def test_gate_blocks_before_any_record(
    fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    records: list[AttemptUsage] = []
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    with pytest.raises(ProviderError, match="Blocked cloud call"):
        _openrouter(monkeypatch, records).generate(f"key {secret}", "sys")
    assert records == []
    assert fake_http.requests == []


# An observer that raises changes neither the output nor the number of attempts.
def test_failing_observer_is_swallowed(fake_http: FakeHttp) -> None:
    def explode(usage: AttemptUsage) -> None:
        raise RuntimeError("observer broke")

    fake_http.reply(_OLLAMA_BODY)
    provider = OllamaProvider("http://x", "m", observer=explode)
    assert provider.generate("d", "s") == "ok"
    assert len(fake_http.requests) == 1

    fake_http.queue({"status_code": 503}, {"json_data": _OLLAMA_BODY})
    assert provider.generate("d", "s") == "ok"
    assert len(fake_http.requests) == 3


# A parser that fails on an unexpected body still leaves the transport facts on record,
# with no tokens and an unknown cost.
def test_failing_parser_keeps_the_transport_record() -> None:
    def parse(body: Mapping[str, object], usage: AttemptUsage) -> AttemptUsage:
        raise ValueError("bad body")

    records: list[AttemptUsage] = []
    meter = Meter(records.append, "p", "m", parse)
    meter.record(loopback=False, attempt=2, status=503, error_kind="non_2xx", body={}, latency=0.1)
    assert records == [AttemptUsage("p", "m", "remote", 2, 503, "non_2xx", 100.0)]


# A cost too large for a float is invalid, not a crash: the attempt keeps its tokens.
def test_huge_cost_is_dropped(fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[AttemptUsage] = []
    fake_http.reply(
        _openai_body(usage={"prompt_tokens": 5, "completion_tokens": 7, "cost": 10**400})
    )
    assert _openrouter(monkeypatch, records).generate("draft", "sys") == "ok"
    (record,) = records
    assert (record.input_uncached, record.output) == (5, 7)
    assert (record.charged_amount, record.cost_state) == (None, "unknown")


# The observer's time is not charged to the time limit: a slow one (seen through a fake
# clock) does not cost the call its retry.
def test_slow_observer_keeps_the_retry(
    fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(base, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    records: list[AttemptUsage] = []

    def slow(usage: AttemptUsage) -> None:
        records.append(usage)
        clock[0] += 100  # far longer than the whole 30 s time limit

    fake_http.queue({"status_code": 503}, {"json_data": _OLLAMA_BODY})
    assert OllamaProvider("http://x", "m", timeout=30, observer=slow).generate("d", "s") == "ok"
    assert len(fake_http.requests) == 2
    assert [r.attempt for r in records] == [1, 2]
    assert fake_http.client_kwargs[1]["timeout"].read == 29


# With no observer, the request and the result are the same as with one.
@pytest.mark.parametrize(
    ("build", "body"),
    [
        (lambda observer: OllamaProvider("http://x", "m", observer=observer), _OLLAMA_BODY),
        (
            lambda observer: OpenAICompatibleProvider("http://x/v1", "m", observer=observer),
            _openai_body(),
        ),
        (
            lambda observer: AnthropicProvider("http://x", "m", "k", observer=observer),
            {"content": [{"type": "text", "text": "ok"}]},
        ),
    ],
)
def test_observer_does_not_change_the_request(
    fake_http: FakeHttp,
    build: Callable[[UsageObserver | None], Provider],
    body: dict[str, Any],
) -> None:
    fake_http.reply(body)
    records: list[AttemptUsage] = []
    assert build(None).generate("d", "s") == build(records.append).generate("d", "s") == "ok"
    first, second = fake_http.calls
    assert first == second
    assert len(records) == 1


# --- Records hold metadata only. ------------------------------------------------------------


# No record ever holds the prompt, the response, the API key, the persona or an error body.
def test_records_hold_no_sensitive_text(
    fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = "please rewrite promptsentinel draft"
    response = "RESPONSE-SENTINEL-9a2c"
    persona = "PERSONA-SENTINEL-77b0"
    key = "sk-or-" + "KEYSENTINEL" + "x" * 8
    error = "ERROR-BODY-SENTINEL-31e5"
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    system = render(f"Rules. {PERSONA_TOKEN}", persona)
    assert persona in system
    records: list[AttemptUsage] = []
    provider = make_provider("openrouter", Settings(), observer=records.append)
    fake_http.queue(
        {"status_code": 503, "json_data": {"error": {"message": error}}},
        {"json_data": _openai_body(response, usage={"prompt_tokens": 3, "cost": 0.1})},
    )
    assert provider.generate(prompt, system) == response
    fake_http.reply(_openai_body("", usage={"completion_tokens": 3}))
    with pytest.raises(ProviderError):
        provider.generate(prompt, system)
    assert len(records) == 3
    dumped = repr(records) + repr([dataclasses.astuple(r) for r in records])
    for sentinel in ("promptsentinel", response, persona, "KEYSENTINEL", error, "Rules."):
        assert sentinel not in dumped


# The trigger path never loads decimal: it is imported only where a cost is parsed, which
# needs an observer.
def test_trigger_path_does_not_import_decimal() -> None:
    import subprocess
    import sys

    code = "import sys, promptmend.cli; print('decimal' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert result.stdout.strip() == "False"
