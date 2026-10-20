"""The usage history of improve and persona runs (recorder.py): one operation per run, one
attempt per HTTP attempt, the outcome of each kind of failure, the --trigger-id allowlist, and
that nothing about history changes what a trigger pastes."""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import pyperclip
import pytest
from typer.testing import CliRunner

from promptmend import history, recorder
from promptmend.cli import app
from promptmend.history import HistoryStore
from promptmend.providers.usage import AttemptUsage

if TYPE_CHECKING:
    from conftest import FakeHttp, HistoryRows, SeedHistory, StubProvider

runner = CliRunner()

LOCAL = ["improve", "--provider", "ollama", "--source", "argument", "--text", "a draft"]
CLOUD = ["improve", "--provider", "openrouter", "--source", "argument", "--text", "a draft"]
REPLY = {
    "id": "gen-abc123",
    "model": "google/gemini-3.5-flash-lite",
    "choices": [{"message": {"content": "rewrite"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0002},
}


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    # Built at runtime, so no key-shaped literal lands in the repo.
    monkeypatch.setenv("OPENROUTER_API_KEY", "-".join(("test", "key")))


def _run(args: list[str]) -> str:
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    return result.stdout


def _only_op(history_rows: HistoryRows) -> dict[str, Any]:
    (op,) = history_rows("operations")
    return op


# A terminal call with no --trigger-id is direct; the operation keeps no draft or output.
def test_direct_call_is_recorded(stub_provider: StubProvider, history_rows: HistoryRows) -> None:
    assert _run(LOCAL) == "improved"
    op = _only_op(history_rows)
    assert (op["origin"], op["trigger_id"], op["kind"], op["outcome"]) == (
        "direct",
        None,
        "improve",
        "ok",
    )
    assert op["profile_id"] == "default"
    assert op["latency_ms"] is not None
    assert op["latency_ms"] >= 0


@pytest.mark.parametrize(
    ("value", "origin", "stored"),
    [
        ("i", "espanso_managed", "-i-"),
        ("ilm", "espanso_managed", "-ilm-"),
        ("p", "espanso_managed", "-p-"),
        ("-i-", "espanso_managed", None),
        ("I", "espanso_managed", None),
        ("reg", "espanso_managed", None),
        ("i i", "espanso_managed", None),
        ("", "espanso_managed", None),
    ],
)
def test_trigger_id_allowlist(
    stub_provider: StubProvider,
    history_rows: HistoryRows,
    value: str,
    origin: str,
    stored: str | None,
) -> None:
    # An unknown value is recorded unattributed (managed, no trigger), apart from a terminal
    # call (no option), and never fails the run.
    assert _run([*LOCAL, "--trigger-id", value]) == "improved"
    op = _only_op(history_rows)
    assert (op["origin"], op["trigger_id"]) == (origin, stored)


def test_attribution_covers_the_allowlist() -> None:
    assert [recorder.attribution(t)[1] for t in recorder.TRIGGER_IDS] == [
        "-i-",
        "-iok-",
        "-ip-",
        "-if-",
        "-il-",
        "-ilm-",
        "-ic-",
        "-io-",
        "-p-",
    ]


# The trigger is never inferred from the other options, however much they look like one.
def test_trigger_is_never_inferred(stub_provider: StubProvider, history_rows: HistoryRows) -> None:
    _run([*CLOUD, "--allow-flagged", "--tier", "pro"])
    op = _only_op(history_rows)
    assert (op["origin"], op["trigger_id"]) == ("direct", None)


def test_trigger_id_is_hidden_from_help() -> None:
    result = runner.invoke(app, ["improve", "--help"])
    assert "--trigger-id" not in result.stdout


def test_persona_is_recorded(history_rows: HistoryRows) -> None:
    assert _run(["persona"]) == "I am working as [role] in [company]."
    op = _only_op(history_rows)
    assert (op["kind"], op["origin"], op["profile_id"], op["outcome"]) == (
        "persona",
        "direct",
        None,
        "ok",
    )


# A successful cloud call: one operation and its attempt with tokens and the reported charge.
def test_cloud_call_records_its_attempt(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    fake_http.reply(REPLY)
    assert _run([*CLOUD, "--trigger-id", "i"]) == "rewrite"
    op = _only_op(history_rows)
    (at,) = history_rows("attempts")
    assert at["operation_id"] == op["id"]
    assert (at["seq"], at["attempt"], at["status"], at["error_kind"]) == (1, 1, 200, None)
    assert (at["input_uncached"], at["output"]) == (10, 5)
    assert (at["charged_amount"], at["charged_unit"], at["cost_state"]) == (
        "0.0002",
        "credits",
        "reported",
    )
    assert at["generation_id"] == "gen-abc123"


# A retried call is one operation with two attempts.
def test_retry_adds_two_attempts_to_one_operation(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    fake_http.queue({"status_code": 429, "json_data": {"error": {"message": "slow down"}}})
    fake_http.reply(REPLY)
    assert _run(CLOUD) == "rewrite"
    op = _only_op(history_rows)
    attempts = history_rows("attempts")
    assert [(a["seq"], a["attempt"], a["status"]) for a in attempts] == [(1, 1, 429), (2, 2, 200)]
    assert {a["operation_id"] for a in attempts} == {op["id"]}


def _outcome(history_rows: HistoryRows) -> Any:
    return _only_op(history_rows)["outcome"]


def test_error_marker_outcome(cloud: None, fake_http: FakeHttp, history_rows: HistoryRows) -> None:
    fake_http.reply({"error": {"message": "bad key"}}, status_code=401)
    assert _run(CLOUD).startswith("[promptmend: OpenRouter returned HTTP 401")
    assert _outcome(history_rows) == "error_marker"
    ((at),) = history_rows("attempts")
    assert (at["status"], at["error_kind"]) == (401, "non_2xx")


def test_override_error_is_recorded(stub_provider: StubProvider, history_rows: HistoryRows) -> None:
    # Settings loaded, so PROMPT_HISTORY is known; the call's own option was bad.
    assert _run([*LOCAL, "--timeout", "soon"]).startswith("[promptmend: ")
    assert _outcome(history_rows) == "error_marker"


# --tier pro on a local provider is refused before any call: an error marker, no attempt.
def test_openrouter_only_refusal_is_recorded(
    fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    assert _run([*LOCAL, "--tier", "pro"]).startswith("[promptmend: --tier pro applies")
    assert _outcome(history_rows) == "error_marker"
    assert history_rows("attempts") == []
    assert fake_http.requests == []


def test_empty_input_is_an_error_marker(
    stub_provider: StubProvider, history_rows: HistoryRows
) -> None:
    _run(["improve", "--provider", "ollama", "--source", "argument", "--text", "  "])
    assert _outcome(history_rows) == "error_marker"


def test_gate_blocked_outcome(cloud: None, fake_http: FakeHttp, history_rows: HistoryRows) -> None:
    draft = "CONFIDENTIAL: summarise the board minutes"
    out = _run(["improve", "--provider", "openrouter", "--source", "argument", "--text", draft])
    assert out.startswith("[promptmend: Blocked cloud call.")
    assert _outcome(history_rows) == "gate_blocked"
    assert history_rows("attempts") == []
    assert fake_http.requests == []


# The other data-protection refusals count as gate_blocked too; the markers are unchanged.
def test_local_only_refusal_is_gate_blocked(
    monkeypatch: pytest.MonkeyPatch, cloud: None, history_rows: HistoryRows
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")
    refusal = "PROMPT_LOCAL_ONLY=true: openrouter would send the draft off this machine"
    assert _run(CLOUD) == f"[promptmend: {refusal}]"
    assert _outcome(history_rows) == "gate_blocked"


def test_plaintext_cloud_url_is_gate_blocked(
    monkeypatch: pytest.MonkeyPatch, cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://openrouter.example/api/v1")
    assert _run(CLOUD) == "[promptmend: OPENROUTER_BASE_URL must be an https:// URL]"
    assert _outcome(history_rows) == "gate_blocked"
    assert fake_http.requests == []


def test_missing_key_is_an_error_marker(fake_http: FakeHttp, history_rows: HistoryRows) -> None:
    assert _run(CLOUD) == "[promptmend: OPENROUTER_API_KEY is not configured]"
    assert _outcome(history_rows) == "error_marker"


# A 2xx reply whose content is rejected: the marker is pasted, and the charge is kept.
def test_validation_failed_keeps_the_charge(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    empty = {**REPLY, "choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
    fake_http.reply(empty)
    assert _run(CLOUD) == "[promptmend: OpenRouter returned empty content]"
    assert _outcome(history_rows) == "validation_failed"
    ((at),) = history_rows("attempts")
    assert (at["charged_amount"], at["output"]) == ("0.0002", 5)


def test_malformed_reply_is_validation_failed(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    fake_http.reply({"choices": []})
    assert _run(CLOUD) == "[promptmend: OpenRouter response was malformed]"
    assert _outcome(history_rows) == "validation_failed"


# --copy fails after a charged reply: the marker and the rewrite are pasted, the charge kept.
def test_clipboard_failure_after_charged_reply(
    monkeypatch: pytest.MonkeyPatch, cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    def broken(text: str) -> None:
        raise pyperclip.PyperclipException("no clipboard")

    monkeypatch.setattr(pyperclip, "copy", broken)
    fake_http.reply(REPLY)
    assert _run([*CLOUD, "--copy"]) == (
        "[promptmend: Clipboard unavailable: no clipboard; the rewrite is pasted instead]"
        "\n\nrewrite"
    )
    assert _outcome(history_rows) == "clipboard_failed"
    ((at),) = history_rows("attempts")
    assert at["charged_amount"] == "0.0002"


def test_clipboard_read_failure(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, history_rows: HistoryRows
) -> None:
    def broken() -> None:
        raise pyperclip.PyperclipException("no clipboard")

    monkeypatch.setattr(pyperclip, "paste", broken)
    _run(["improve", "--provider", "ollama"])
    assert _outcome(history_rows) == "clipboard_failed"


def test_concealed_refused_outcome(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, history_rows: HistoryRows
) -> None:
    import promptmend.cli as cli

    monkeypatch.setattr(cli, "is_concealed", lambda: True)
    monkeypatch.setattr(pyperclip, "copy", lambda text: None)
    _run(["improve", "--provider", "ollama"])
    assert _outcome(history_rows) == "concealed_refused"
    assert not stub_provider.calls


def test_unexpected_error_outcome(stub_provider: StubProvider, history_rows: HistoryRows) -> None:
    stub_provider.exc = RuntimeError("kaput")
    assert _run(LOCAL) == "[promptmend: unexpected error: kaput]"
    assert _outcome(history_rows) == "unexpected_error"


# Settings that do not load leave PROMPT_HISTORY unknown, so nothing is recorded.
@pytest.mark.parametrize("args", [LOCAL, ["persona"]], ids=["improve", "persona"])
def test_settings_error_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    history_rows: HistoryRows,
    args: list[str],
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "maybe")
    _run(args)
    assert not history.history_path().exists()


@pytest.mark.parametrize("args", [LOCAL, ["persona"]], ids=["improve", "persona"])
def test_history_off_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    history_rows: HistoryRows,
    args: list[str],
) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", "false")
    _run(args)
    assert not history.history_path().parent.exists()
    if args == LOCAL:
        assert stub_provider.options[0]["observer"] is None


# A history that cannot be written changes neither the paste nor the exit code, and never
# makes the provider call again.
def test_locked_history_changes_nothing(cloud: None, fake_http: FakeHttp) -> None:
    fake_http.reply(REPLY)
    assert _run(CLOUD) == "rewrite"  # creates the database
    path = history.history_path()
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as other:
        other.execute("BEGIN EXCLUSIVE")
        assert _run(CLOUD) == "rewrite"
        other.execute("ROLLBACK")
    assert len(fake_http.requests) == 2
    health = HistoryStore(path).health()
    assert (health.lost_writes, health.spooled, health.operations) == (0, 1, 1)
    # The next run stores the spooled record too (#213).
    assert _run(CLOUD) == "rewrite"
    assert HistoryStore(path).health().operations == 3


def test_failing_history_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, cloud: None, fake_http: FakeHttp
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(HistoryStore, "record", explode)
    fake_http.reply({"error": {"message": "down"}}, status_code=500)
    result = runner.invoke(app, CLOUD)
    assert (result.exit_code, result.stdout) == (
        0,
        "[promptmend: OpenRouter returned HTTP 500: provider unavailable, try again; down]",
    )
    assert len(fake_http.requests) == 1


def test_read_only_history_dir_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))
    monkeypatch.setenv("LOCALAPPDATA", str(blocker))
    assert _run(LOCAL) == "improved"


# Writing the same run twice (a retried write) never duplicates a row.
def test_finishing_twice_writes_once(
    monkeypatch: pytest.MonkeyPatch, history_rows: HistoryRows
) -> None:
    from promptmend.config import Settings

    rec = recorder.Recorder("improve", "i")
    rec.track(Settings.load())
    usage = AttemptUsage("openrouter", "m/x", "remote", 1, 200, None, 12.0)
    assert rec.observer is not None
    rec.observer(usage)
    rec.emitted()
    rec.finish()
    rec.finish()
    assert len(history_rows("operations")) == 1
    assert len(history_rows("attempts")) == 1


# Each recorded run also prunes, a batch at a time, what is older than the retention.
def test_recording_prunes_old_operations(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    history_rows: HistoryRows,
    seed_history: SeedHistory,
) -> None:
    monkeypatch.setattr(history, "_PRUNE_BATCH", 2)
    old = datetime.now(UTC) - timedelta(days=400)
    for _ in range(3):
        op = {
            "id": history.new_operation_id(),
            "occurred_at_utc": old,
            "origin": "direct",
            "kind": "improve",
            "outcome": "ok",
        }
        seed_history(op, [{"provider": "ollama", "requested_model": "m", "endpoint": "loopback"}])
    _run(LOCAL)
    assert len(history_rows("operations")) == 2  # 3 old - 2 pruned + this run
    _run(LOCAL)
    ops = history_rows("operations")
    assert len(ops) == 2
    assert all(op["occurred_at_utc"] > old.strftime("%Y-%m-%d") for op in ops)
    assert history_rows("attempts") == []


def test_retention_setting_drives_the_prune(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    history_rows: HistoryRows,
    seed_history: SeedHistory,
) -> None:
    monkeypatch.setenv("PROMPT_HISTORY_RETENTION_DAYS", "5")
    op = {
        "id": history.new_operation_id(),
        "occurred_at_utc": datetime.now(UTC) - timedelta(days=10),
        "origin": "direct",
        "kind": "improve",
        "outcome": "ok",
    }
    seed_history(op)
    _run(LOCAL)
    assert len(history_rows("operations")) == 1


# Clipboard output (#134): a success is still `ok`; a failed copy, whose rewrite is pasted
# after the marker instead, is `clipboard_failed` with the charge kept.
def test_clipboard_output_outcomes(
    monkeypatch: pytest.MonkeyPatch, cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    monkeypatch.setenv("PROMPT_OUTPUT", "clipboard")
    copied: list[str] = []
    monkeypatch.setattr(pyperclip, "copy", copied.append)
    fake_http.reply(REPLY)
    assert _run(CLOUD) == ""
    assert copied == ["rewrite"]

    def broken(text: str) -> NoReturn:
        raise pyperclip.PyperclipException("no clipboard")

    monkeypatch.setattr(pyperclip, "copy", broken)
    assert _run(CLOUD).endswith("the rewrite is pasted instead]\n\nrewrite")
    assert [op["outcome"] for op in history_rows("operations")] == ["ok", "clipboard_failed"]
    assert [at["charged_amount"] for at in history_rows("attempts")] == ["0.0002", "0.0002"]
