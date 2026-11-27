"""try_run.run(), the Try tab's run without the interface (#408): a stub run (placeholder key,
gated as the real settings would be, nothing recorded) and a real run answered by fake_http
(recorded as a direct improve call from -try-, after the outcome was shown). No Textual app,
no network, no clipboard."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pyperclip
import pytest

from promptmend import clipboard_guard, pipeline, smoke, try_run
from promptmend.config import ConfigLayers, Settings
from promptmend.providers.usage import AttemptUsage
from promptmend.try_run import Outcome, usage_line

if TYPE_CHECKING:
    from conftest import FakeHttp, HistoryRows

# Built at runtime, so no key-shaped literal lands in the repo (gitleaks).
KEY = "sk-or-v1-" + "cd34" * 16
REAL_KEY = "-".join(("test", "key"))
DRAFT = "Write a short plan for the quarterly report review."
REPLY = {
    "id": "gen-abc123",
    "model": "google/gemini-3.5-flash-lite",
    "choices": [{"message": {"content": "A better prompt."}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 30, "cost": 0.0012},
}


@pytest.fixture(autouse=True)
def no_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    def touched(*_: Any) -> Any:
        raise AssertionError("a Try run touched the clipboard")

    monkeypatch.setattr(pyperclip, "paste", touched)
    monkeypatch.setattr(pyperclip, "copy", touched)
    monkeypatch.setattr(clipboard_guard, "is_concealed", touched)
    monkeypatch.setattr(pipeline, "is_concealed", touched)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """For stub runs: any request to a host other than 127.0.0.1 fails the test."""
    requests: list[httpx.Request] = []
    real_send = httpx.Client.send

    def send(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        assert request.url.host == "127.0.0.1", f"a stub run reached {request.url}"
        requests.append(request)
        return real_send(client, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    return requests


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", REAL_KEY)


def settings(provider: str = "openrouter") -> Settings:
    return ConfigLayers.resolve().settings().for_call("standard", provider=provider)


def test_stub_run_uses_the_placeholder_key_and_records_nothing(
    cloud: None, sent: list[httpx.Request], history_rows: HistoryRows
) -> None:
    shown: list[Outcome] = []
    outcome = try_run.run(DRAFT, "openrouter", None, settings(), stub=True, show=shown.append)
    assert shown == [outcome]
    assert (outcome.text, outcome.error, outcome.requests) == (smoke.REPLY, False, 1)
    assert outcome.recorded is False
    assert "cost not applicable (dry run)" in outcome.usage
    (request,) = sent
    assert request.headers["authorization"] == f"Bearer {smoke.PLACEHOLDER_KEY}"
    assert REAL_KEY not in request.headers["authorization"]
    assert history_rows("operations") == []


def test_stub_run_gate_blocks_before_the_stub_is_called(
    cloud: None, sent: list[httpx.Request]
) -> None:
    outcome = try_run.run(
        f"Use the key {KEY} to call the API.", "openrouter", None, settings(), stub=True
    )
    assert outcome.error
    assert outcome.text.startswith("[promptmend: ")
    assert outcome.requests == 0
    assert "no request was sent" in outcome.usage
    assert sent == []


def test_stub_run_is_refused_under_local_only(
    monkeypatch: pytest.MonkeyPatch, sent: list[httpx.Request]
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")
    outcome = try_run.run(DRAFT, "openrouter", None, settings(), stub=True)
    assert outcome.error
    assert outcome.text.startswith("[promptmend: PROMPT_LOCAL_ONLY=true")
    assert outcome.requests == 0
    assert sent == []


def test_too_long_draft_has_the_one_wording(sent: list[httpx.Request]) -> None:
    draft = "x" * (pipeline.MAX_DRAFT_CHARS + 1)
    outcome = try_run.run(draft, "ollama", None, settings("ollama"), stub=True)
    assert (
        outcome.text == f"[promptmend: Input is too long (over {pipeline.MAX_DRAFT_CHARS} chars)]"
    )
    assert sent == []


def test_real_run_is_shown_then_recorded(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    fake_http.reply(REPLY)
    rows_when_shown: list[int] = []
    outcome = try_run.run(
        DRAFT,
        "openrouter",
        "general",
        settings(),
        stub=False,
        show=lambda _: rows_when_shown.append(len(history_rows("operations"))),
    )
    assert rows_when_shown == [0]
    assert (outcome.text, outcome.error, outcome.requests, outcome.recorded) == (
        "A better prompt.",
        False,
        1,
        True,
    )
    assert "tokens in 120, out 30 · cost 0.0012 credits reported" in outcome.usage
    (op,) = history_rows("operations")
    assert (op["origin"], op["trigger_id"], op["kind"], op["outcome"], op["profile_id"]) == (
        "direct",
        "-try-",
        "improve",
        "ok",
        "general",
    )


def test_a_failing_real_run_is_still_recorded(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    fake_http.reply({"error": {"message": "no credit"}}, status_code=402)
    outcome = try_run.run(DRAFT, "openrouter", None, settings(), stub=False)
    assert outcome.error
    assert outcome.text.startswith("[promptmend: ")
    assert outcome.recorded
    assert "cost unknown" in outcome.usage
    (op,) = history_rows("operations")
    assert (op["origin"], op["outcome"]) == ("direct", "error_marker")


def test_a_real_run_is_recorded_when_showing_it_fails(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    fake_http.reply(REPLY)

    def lost(_: Outcome) -> None:
        raise RuntimeError("the app went away")

    with pytest.raises(RuntimeError, match="the app went away"):
        try_run.run(DRAFT, "openrouter", None, settings(), stub=False, show=lost)
    (op,) = history_rows("operations")
    assert (op["outcome"], op["profile_id"]) == ("ok", "default")
    assert op["latency_ms"] is not None


def test_real_run_with_history_off_records_nothing(
    cloud: None,
    fake_http: FakeHttp,
    history_rows: HistoryRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", "false")
    fake_http.reply(REPLY)
    outcome = try_run.run(DRAFT, "openrouter", None, settings(), stub=False)
    assert not outcome.recorded
    assert history_rows("operations") == []
    # The usage line comes from the attempts even with the history off.
    assert "tokens in 120, out 30" in outcome.usage


def test_usage_line() -> None:
    def attempt(**fields: Any) -> AttemptUsage:
        return AttemptUsage("openrouter", "m", "remote", 1, 200, None, 10.0, **fields)

    assert usage_line([], 3.2, stub=False) == "3 ms · no request was sent"
    reported = attempt(
        cost_state="reported",
        charged_amount=Decimal("0.5"),
        charged_unit="credits",
        input_uncached=10,
        cache_read=5,
        output=3,
    )
    assert usage_line([reported], 12.4, stub=False) == (
        "12 ms · 1 request · tokens in 15, out 3 · cost 0.5 credits reported"
    )
    unknown = attempt()
    assert usage_line([unknown], 1.0, stub=False).endswith(
        "tokens in unknown, out unknown · cost unknown"
    )
    assert usage_line([unknown, reported], 1.0, stub=False).endswith(
        "2 requests · tokens in 15, out 3 · cost 0.5 credits reported (some unknown)"
    )
    local = attempt(cost_state="not_applicable")
    assert usage_line([local], 1.0, stub=False).endswith(
        "cost not applicable (runs on this machine)"
    )
    # A stub run is named for what it is, before anything else on the line (#391).
    assert usage_line([], 3.2, stub=True) == (
        "Dry run (checks the wiring, no rewrite) · 3 ms · no request was sent"
    )
    assert usage_line([unknown], 1.0, stub=True) == (
        "Dry run (checks the wiring, no rewrite) · 1 ms · 1 request · tokens in unknown, "
        "out unknown · cost not applicable (dry run)"
    )


# --- usable(): where the Try tab's "Run against" starts (#391) ------------------------------


def usable(provider: str, environ: dict[str, str]) -> bool:
    layers = ConfigLayers.resolve(environ, strict=False)
    return try_run.usable(provider, layers.settings(), layers.entries)


@pytest.mark.parametrize(
    ("provider", "environ", "expected"),
    [
        ("openrouter", {"OPENROUTER_API_KEY": REAL_KEY}, True),
        ("openrouter", {}, False),
        ("openrouter", {"OPENROUTER_API_KEY": ""}, False),
        # Another provider's key does not make this one usable.
        ("anthropic", {"OPENROUTER_API_KEY": REAL_KEY}, False),
        ("anthropic", {"ANTHROPIC_API_KEY": REAL_KEY}, True),
        # A local provider takes no key.
        ("ollama", {}, True),
        ("lmstudio", {}, True),
        # PROMPT_LOCAL_ONLY refuses a cloud provider, its key set or not, not a local one...
        ("openrouter", {"OPENROUTER_API_KEY": REAL_KEY, "PROMPT_LOCAL_ONLY": "true"}, False),
        ("ollama", {"PROMPT_LOCAL_ONLY": "true"}, True),
        # ...but an Ollama that leaves the machine (a remote URL) too.
        (
            "ollama",
            {"OLLAMA_BASE_URL": "https://ollama.example", "PROMPT_LOCAL_ONLY": "true"},
            False,
        ),
        ("no-such-provider", {}, False),
    ],
)
def test_usable(provider: str, environ: dict[str, str], expected: bool) -> None:
    assert usable(provider, environ) is expected
