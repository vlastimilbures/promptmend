import contextlib
import csv
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from promptmend import history
from promptmend.config import Settings, user_data_dir
from promptmend.history import HistoryError, HistoryStore, load_price_table

SRC = Path(history.__file__).resolve().parents[1]

# The allowlist, written out here on purpose: a column added to the schema without being
# added to this list (after a privacy review) fails test_schema_matches_allowlist.
ALLOWED = {
    "operations": [
        "id",
        "occurred_at_utc",
        "origin",
        "trigger_id",
        "kind",
        "profile_id",
        "outcome",
        "latency_ms",
        "app_version",
    ],
    "attempts": [
        "operation_id",
        "seq",
        "provider",
        "requested_model",
        "endpoint",
        "attempt",
        "status",
        "error_kind",
        "latency_ms",
        "cost_state",
        "returned_model",
        "returned_provider",
        "generation_id",
        "input_uncached",
        "cache_read",
        "cache_write",
        "output",
        "reasoning",
        "charged_amount",
        "charged_unit",
        "upstream_cost",
        "upstream_unit",
        "server_total_ms",
        "server_load_ms",
        "server_input_ms",
        "server_output_ms",
        "estimated_amount",
        "estimated_unit",
        "price_table_version",
    ],
}


_REAL_BUDGET = (history._BUDGET, history._WRITE_BUDGET)


@pytest.fixture(autouse=True)
def _generous_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real ~0.25 s bound drops writes on a loaded CI runner (the first write also creates
    # the database), which made tests that only check what was stored flaky.
    monkeypatch.setattr(history, "_BUDGET", 2.25)
    monkeypatch.setattr(history, "_WRITE_BUDGET", 2.0)


def _use_real_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """For a test that checks a blocked write gives up: call after its set-up writes."""
    monkeypatch.setattr(history, "_BUDGET", _REAL_BUDGET[0])
    monkeypatch.setattr(history, "_WRITE_BUDGET", _REAL_BUDGET[1])


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "data" / "promptmend" / history.DB_NAME)


def _op(**changes: Any) -> dict[str, Any]:
    return {
        "id": history.new_operation_id(),
        "origin": "espanso_managed",
        "trigger_id": "-i-",
        "kind": "improve",
        "profile_id": "default",
        "outcome": "ok",
        "latency_ms": 1234.5,
        **changes,
    }


def _attempt(**changes: Any) -> dict[str, Any]:
    return {
        "provider": "openrouter",
        "requested_model": "google/gemini-3.5-flash-lite",
        "endpoint": "remote",
        "attempt": 1,
        "status": 200,
        "error_kind": None,
        "latency_ms": 800.0,
        "cost_state": "reported",
        "input_uncached": 900,
        "cache_read": 100,
        "output": 300,
        "charged_amount": Decimal("0.000123456789012345678901"),
        "charged_unit": "credits",
        **changes,
    }


def _rows(store: HistoryStore, table: str) -> list[dict[str, Any]]:
    with contextlib.closing(sqlite3.connect(store.path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]  # noqa: S608


def _lost(store: HistoryStore) -> int:
    lost: int = json.loads(store.lost_path.read_text("utf-8"))["lost_writes"]
    return lost


# -- settings and paths --------------------------------------------------------------------


def test_history_is_on_by_default_with_a_year_of_retention() -> None:
    cfg = Settings.load()
    assert cfg.history is True
    assert cfg.history_retention_days == 365


def test_history_settings_parse_strictly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", "off")
    with pytest.raises(ValueError, match="PROMPT_HISTORY must be true or false"):
        Settings.load()
    monkeypatch.setenv("PROMPT_HISTORY", "false")
    monkeypatch.setenv("PROMPT_HISTORY_RETENTION_DAYS", "0")
    with pytest.raises(ValueError, match="PROMPT_HISTORY_RETENTION_DAYS"):
        Settings.load()


def test_data_dir_is_per_device(tmp_path: Path) -> None:
    if os.name == "nt":
        assert user_data_dir({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "promptmend"
        assert user_data_dir({}).parent.name == "Local"
    else:
        assert user_data_dir({"XDG_DATA_HOME": str(tmp_path)}) == tmp_path / "promptmend"
        assert user_data_dir({}) == Path.home() / ".local" / "share" / "promptmend"


def test_from_settings_uses_the_data_and_config_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", "false")
    monkeypatch.setenv("PROMPT_HISTORY_RETENTION_DAYS", "30")
    store = HistoryStore.from_settings(Settings.load())
    assert store.path == tmp_path / "data" / "promptmend" / history.DB_NAME
    assert store.prices_path == tmp_path / "config" / "promptmend" / history.PRICES_NAME
    assert (store.enabled, store.retention_days) == (False, 30)


def test_disabled_history_records_nothing(store: HistoryStore) -> None:
    store.enabled = False
    assert store.record(_op(), [_attempt()]) is True
    assert not store.path.parent.exists()


def test_importing_the_cli_or_history_loads_no_sqlite() -> None:
    code = (
        "import sys, promptmend.cli; bad = {'sqlite3', 'promptmend.history'}; "
        "print(sorted(bad & sys.modules.keys()))\n"
        "import promptmend.history; bad = {'sqlite3', 'tomllib', 'decimal'}; "
        "print(sorted(bad & sys.modules.keys()))"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )
    assert result.stdout.split() == ["[]", "[]"], result.stderr


# -- schema and round trip -----------------------------------------------------------------


def test_schema_matches_allowlist(store: HistoryStore) -> None:
    assert store.record(_op(), [_attempt()])
    with contextlib.closing(sqlite3.connect(store.path)) as conn:
        tables = {
            name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert tables == ALLOWED.keys()
        for table, columns in ALLOWED.items():
            found = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            assert found == columns, table
        assert conn.execute("PRAGMA user_version").fetchone()[0] == history.SCHEMA_VERSION
        mode = "wal" if history.wal_is_safe(sqlite3.sqlite_version) else "delete"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == mode
    assert list(history.OPERATION_COLUMNS) == ALLOWED["operations"]
    assert list(history.ATTEMPT_COLUMNS) == ALLOWED["attempts"]


@pytest.mark.parametrize(
    ("version", "safe"),
    [
        ("unknown", None),
        ("", None),
        ("3.50.4", False),
        ("3.51.2", False),
        ("3.51.3", True),
        ("3.52.0", True),
        ("4.0", True),
    ],
)
def test_wal_is_safe(version: str, safe: bool | None) -> None:
    assert history.wal_is_safe(version) is safe


def _journal(store: HistoryStore) -> str:
    with contextlib.closing(sqlite3.connect(store.path)) as conn:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])


@pytest.mark.parametrize(("version", "mode"), [("3.50.4", "delete"), ("3.51.3", "wal")])
def test_journal_mode_follows_the_library(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch, version: str, mode: str
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version", version)
    assert store.record(_op())
    assert _journal(store) == mode
    assert store.health().journal_mode == mode


def test_old_library_switches_a_wal_database_back(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.51.3")
    assert store.record(_op())
    assert _journal(store) == "wal"
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.50.4")
    assert store.record(_op())
    assert _journal(store) == "delete"
    assert not Path(f"{store.path}-wal").exists()
    assert len(_rows(store, "operations")) == 2


def test_blocked_switch_still_records(store: HistoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.51.3")
    assert store.record(_op())
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.50.4")
    # Another connection holds the WAL database open, so leaving WAL fails.
    with contextlib.closing(sqlite3.connect(store.path)) as other:
        other.execute("SELECT COUNT(*) FROM operations").fetchone()
        assert store.record(_op())
        assert store.health().journal_mode == "wal"
    assert store.health().lost_writes == 0
    assert len(_rows(store, "operations")) == 2
    assert store.record(_op())
    assert _journal(store) == "delete"


def test_money_round_trips_exactly_with_its_unit(store: HistoryStore) -> None:
    amount = Decimal("0.000123456789012345678901")
    upstream = Decimal("1E-9")
    assert store.record(
        _op(), [_attempt(upstream_cost=upstream, upstream_unit="USD"), _attempt(attempt=2)]
    )
    rows = _rows(store, "attempts")
    assert rows[0]["charged_amount"] == "0.000123456789012345678901"
    assert Decimal(rows[0]["charged_amount"]) == amount
    assert rows[0]["charged_unit"] == "credits"
    assert rows[0]["upstream_cost"] == "0.000000001"
    assert rows[0]["upstream_unit"] == "USD"
    with contextlib.closing(sqlite3.connect(store.path)) as conn:
        types = set(conn.execute("SELECT typeof(charged_amount) FROM attempts"))
    assert types == {("text",)}
    exported = io.StringIO()
    store.export(exported, "json")
    attempt = json.loads(exported.getvalue())["attempts"][0]
    assert (attempt["charged_amount"], attempt["charged_unit"]) == (str(amount), "credits")


def test_a_reported_zero_stays_zero_and_a_missing_cost_stays_unknown(store: HistoryStore) -> None:
    free = _attempt(charged_amount=Decimal(0))
    missing = _attempt(charged_amount=None, charged_unit=None, cost_state="unknown")
    assert store.record(_op(), [free, missing])
    rows = _rows(store, "attempts")
    assert (rows[0]["charged_amount"], rows[0]["cost_state"]) == ("0", "reported")
    assert (rows[1]["charged_amount"], rows[1]["cost_state"]) == (None, "unknown")


def test_a_retried_write_never_duplicates(store: HistoryStore) -> None:
    op = _op()
    attempts = [_attempt(), _attempt(attempt=2)]
    assert store.record(op, attempts)
    assert store.record(op, attempts)
    assert len(_rows(store, "operations")) == 1
    assert [(r["operation_id"], r["seq"]) for r in _rows(store, "attempts")] == [
        (op["id"], 1),
        (op["id"], 2),
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"id": "not-a-random-id"},
        {"origin": "somewhere"},
        {"kind": "rewrite"},
        {"outcome": "Failed with a long message"},
        {"occurred_at_utc": "2026-10-04"},
        {"occurred_at_utc": datetime(2026, 10, 4)},  # naive on purpose
    ],
)
def test_an_invalid_record_is_dropped_and_counted(
    store: HistoryStore, change: dict[str, object]
) -> None:
    assert store.record(_op(**change)) is False
    assert _lost(store) == 1


@pytest.mark.parametrize("change", [{"endpoint": "cloud"}, {"provider": "open router"}])
def test_an_invalid_attempt_drops_the_whole_record(
    store: HistoryStore, change: dict[str, str]
) -> None:
    assert store.record(_op(), [_attempt(), _attempt(**change)]) is False
    assert not store.path.exists() or _rows(store, "operations") == []


@pytest.mark.parametrize(
    "change",
    [
        {"charged_unit": None},
        {"charged_unit": "EUR"},
        {"charged_amount": Decimal(-1)},
        {"charged_amount": Decimal("NaN")},
        {"charged_amount": "lots"},
        {"charged_amount": [1]},
    ],
)
def test_a_bad_cost_is_unknown_and_the_record_is_kept(
    store: HistoryStore, change: dict[str, object]
) -> None:
    assert store.record(_op(), [_attempt(**change)])
    row = _rows(store, "attempts")[0]
    assert (row["charged_amount"], row["charged_unit"], row["cost_state"]) == (
        None,
        None,
        "unknown",
    )
    assert row["output"] == 300


def test_a_negative_zero_is_stored_as_zero(store: HistoryStore) -> None:
    assert store.record(_op(), [_attempt(charged_amount=Decimal("-0.000"))])
    row = _rows(store, "attempts")[0]
    assert (row["charged_amount"], row["cost_state"]) == ("0.000", "reported")


def test_error_kinds_match_what_post_json_records() -> None:
    base = (SRC / "promptmend" / "providers" / "base.py").read_text("utf-8")
    recorded = set(re.findall(r'error_kind(?: or)? = .*?"([a-z_0-9]+)"', base))
    assert recorded == set(history.ERROR_KINDS)


# -- privacy -------------------------------------------------------------------------------

KEY = "sk-or-v1-" + "0123456789abcdef" * 4
# Values that must never be stored in any identifier column.
HOSTILE = [
    "SENTINEL draft about the merger",
    "I am SENTINEL Head of Data",
    "/Users/SENTINEL/secret.txt",
    "C:\\Users\\SENTINEL\\secret.txt",
    "SENTINEL:p4ss@host",
    "SENTINEL@example.com",
    "SENTINEL" + "aB3xY9" * 6,  # a bare token
    "x/SENTINEL" + "Q7z!K2" * 4,  # one inside a model-like path
    '{"choices": "SENTINEL body"}',
    KEY,
]
OPERATION_IDENTS = ("trigger_id", "profile_id", "app_version")
ATTEMPT_IDENTS = (
    "provider",
    "requested_model",
    "returned_model",
    "returned_provider",
    "generation_id",
    "error_kind",
)
REQUIRED = {"provider", "requested_model"}


def _assert_nothing_leaked(store: HistoryStore, *needles: Any) -> None:
    stored = b"".join(p.read_bytes() for p in store.path.parent.iterdir() if p.is_file())
    as_json, as_csv = io.StringIO(), io.StringIO()
    store.export(as_json, "json")
    store.export(as_csv, "csv")
    texts = [as_json.getvalue(), as_csv.getvalue(), repr(store.health())]
    for needle in needles:
        assert needle.encode() not in stored
        assert all(needle not in text for text in texts)


def test_sentinels_never_reach_any_column_exports_or_health(store: HistoryStore) -> None:
    unlisted = {
        "prompt": "SENTINEL draft",
        "output": "SENTINEL rewrite",
        "persona": "SENTINEL persona",
        "form": "SENTINEL-form-pick",
        "raw_body": "SENTINEL body",
        "api_key": KEY,
    }
    assert store.record(_op(**unlisted), [_attempt(**unlisted)])
    for value in HOSTILE:
        for column in OPERATION_IDENTS:
            assert store.record(_op(**{column: value}), [_attempt()]), (column, value)
        for column in ATTEMPT_IDENTS:
            stored = store.record(_op(), [_attempt(**{column: value})])
            assert stored is (column not in REQUIRED), (column, value)
    assert store.record(_op(latency_ms="SENTINEL"), [_attempt(latency_ms="SENTINEL")])
    _assert_nothing_leaked(store, "SENTINEL", KEY)
    for row in _rows(store, "operations")[1:]:
        assert {row["trigger_id"], row["profile_id"]} <= {"-i-", "default", None}


def test_extra_patterns_keep_a_slug_shaped_sentinel_out(store: HistoryStore) -> None:
    # A slug is indistinguishable from a model or provider name by shape; the user's own
    # PROMPT_EXTRA_PATTERNS catch it, and an invalid pattern drops the write instead.
    store.extra_patterns = "sentinel"
    for column in (*OPERATION_IDENTS, *ATTEMPT_IDENTS):
        op = _op(**{column: "SENTINEL-form-pick"} if column in OPERATION_IDENTS else {})
        attempt = _attempt(**{column: "SENTINEL-form-pick"} if column in ATTEMPT_IDENTS else {})
        assert store.record(op, [attempt]) is (column not in REQUIRED)
    _assert_nothing_leaked(store, "SENTINEL")
    store.extra_patterns = "("
    assert store.record(_op(), [_attempt()]) is False


def test_real_identifiers_are_kept(store: HistoryStore) -> None:
    models = [
        "google/gemini-3.5-flash-lite",
        "meta-llama/Llama-3.3-70B-Instruct:free",
        "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
        "qwen3:8b",
        "claude-sonnet-5",
    ]
    for model in models:
        assert store.record(
            _op(trigger_id="-iok-", profile_id="my-profile_2"),
            [
                _attempt(
                    requested_model=model,
                    returned_model=model,
                    returned_provider="Google",
                    generation_id="gen-1759-AbC123xyz",
                    error_kind="timeout",
                ),
                _attempt(provider="anthropic", generation_id="msg_01ABCdefGHI"),
            ],
        )
    rows = _rows(store, "attempts")
    assert [r["requested_model"] for r in rows[::2]] == models
    assert [r["returned_model"] for r in rows[::2]] == models
    assert {r["generation_id"] for r in rows} == {"gen-1759-AbC123xyz", "msg_01ABCdefGHI"}
    assert {r["error_kind"] for r in rows} == {"timeout", None}
    assert {r["returned_provider"] for r in rows} == {"Google", None}
    ops = _rows(store, "operations")
    assert {(r["trigger_id"], r["profile_id"]) for r in ops} == {("-iok-", "my-profile_2")}


# -- fail-open writer ----------------------------------------------------------------------


def test_a_locked_database_spools_the_write_within_the_budget(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert store.record(_op())
    with contextlib.closing(sqlite3.connect(store.path, isolation_level=None)) as other:
        other.execute("BEGIN EXCLUSIVE")
        _use_real_budget(monkeypatch)
        started = time.monotonic()
        assert store.record(_op()) is False
        # Generous bound: the budget is 0.25 s; this only catches a writer that blocks.
        assert time.monotonic() - started < 5
        other.execute("ROLLBACK")
    assert not store.lost_path.exists()
    health = store.health()
    assert (health.lost_writes, health.spooled, health.operations) == (0, 1, 1)
    assert not health.tracking_incomplete


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_a_read_only_database_never_raises(store: HistoryStore) -> None:
    assert store.record(_op())
    store.path.chmod(stat.S_IREAD)
    try:
        assert store.record(_op()) is False
    finally:
        # SQLite gives a -wal/-shm it creates the database's mode, so restore those too.
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                Path(f"{store.path}{suffix}").chmod(stat.S_IREAD | stat.S_IWRITE)
    assert store.health().spooled == 1
    assert store.replay() == 1
    assert store.health().operations == 2


def test_a_corrupt_database_never_raises_and_reset_recovers(store: HistoryStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b"this is not an sqlite database, just junk " * 200)
    assert store.record(_op()) is False
    health = store.health()
    assert health.error == "corrupt"
    assert (health.lost_writes, health.spooled) == (0, 1)
    with pytest.raises(HistoryError, match="corrupt"):
        store.stats()
    store._mark_lost()
    store.reset()
    assert not store.lost_path.exists()
    assert not store.spool_path.exists()
    assert store.record(_op())
    assert store.health().operations == 1


def test_when_database_and_sidecar_both_fail_nothing_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = HistoryStore(blocker / "promptmend" / history.DB_NAME)
    assert store.record(_op(), [_attempt()]) is False
    assert store._mark_lost() is False
    health = store.health()
    assert (health.exists, health.tracking_incomplete) == (False, True)


# -- spool (#213) ---------------------------------------------------------------------------


def _spool_one(store: HistoryStore, monkeypatch: pytest.MonkeyPatch, **op: Any) -> str:
    """Spools one record (with an attempt) behind an exclusive lock; returns its id."""
    assert store.record(_op())
    record = _op(**op)
    with contextlib.closing(sqlite3.connect(store.path, isolation_level=None)) as other:
        other.execute("BEGIN EXCLUSIVE")
        with monkeypatch.context() as patch:
            patch.setattr(history, "_WRITE_BUDGET", 0.05)
            assert store.record(record, [_attempt()]) is False
        other.execute("ROLLBACK")
    op_id: str = record["id"]
    return op_id


def _spool(store: HistoryStore) -> dict[str, Any]:
    spool: dict[str, Any] = json.loads(store.spool_path.read_text("utf-8"))
    return spool


def test_a_spooled_record_is_stored_by_the_next_write(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    spooled = _spool_one(store, monkeypatch)
    assert store.health().spooled == 1
    assert store.record(_op())
    assert not store.spool_path.exists()
    assert not store.lost_path.exists()
    ops = {r["id"] for r in _rows(store, "operations")}
    assert spooled in ops
    assert len(ops) == 3
    attempts = _rows(store, "attempts")
    assert [(a["operation_id"], a["charged_amount"]) for a in attempts] == [
        (spooled, "0.000123456789012345678901")
    ]
    # A second replay of the same record never duplicates a row.
    assert store.record(_op())
    assert len(_rows(store, "attempts")) == 1


def test_the_spool_holds_only_the_allowlisted_columns(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spool_one(store, monkeypatch)
    spool = _spool(store)
    assert set(spool) == {"version", "records"}
    (entry,) = spool["records"]
    assert set(entry) == {"operation", "attempts"}
    assert set(entry["operation"]) == set(history.OPERATION_COLUMNS)
    assert {key for at in entry["attempts"] for key in at} == set(history.ATTEMPT_COLUMNS)
    # Planted keys are never read back.
    entry["draft"] = "SENTINEL draft text"
    entry["operation"]["draft"] = "SENTINEL draft text"
    entry["attempts"][0]["response"] = "SENTINEL reply text"
    store.spool_path.write_text(json.dumps(spool), "utf-8")
    assert store.replay() == 1
    assert b"SENTINEL" not in store.path.read_bytes()
    assert not store.spool_path.exists()


def test_a_secret_planted_in_the_spool_is_rejected_on_replay(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = _spool_one(store, monkeypatch)
    spool = _spool(store)
    bad = json.loads(json.dumps(spool["records"][0]))
    bad["operation"]["id"] = planted = history.new_operation_id()
    bad["attempts"][0]["requested_model"] = KEY
    spool["records"].insert(0, bad)
    store.spool_path.write_text(json.dumps(spool), "utf-8")
    assert store.record(_op())
    ids = {r["id"] for r in _rows(store, "operations")}
    assert keep in ids
    assert planted not in ids
    assert KEY.encode() not in store.path.read_bytes()
    assert _lost(store) == 1
    assert not store.spool_path.exists()


@pytest.mark.parametrize(
    "content",
    [
        b"\x00 not json at all",
        b"[1, 2, 3]",
        json.dumps({"version": 99, "records": []}).encode(),
        json.dumps({"version": 1, "records": "nope"}).encode(),
        b" " * (512 * 1024 + 1),
    ],
    ids=["junk", "list", "version", "records", "oversize"],
)
def test_a_corrupt_spool_is_ignored_and_counted(store: HistoryStore, content: bytes) -> None:
    store.path.parent.mkdir(parents=True)
    store.spool_path.write_bytes(content)
    assert store.health().tracking_incomplete
    assert store.record(_op())
    assert _lost(store) == 1
    assert not store.spool_path.exists()
    assert store.health().operations == 1


def test_bad_spool_entries_are_skipped_and_counted(store: HistoryStore) -> None:
    good = {
        "operation": {**_op(), "occurred_at_utc": "2026-10-07T13:40:59.566218Z"},
        "attempts": [],
    }
    bad_time = {"operation": {**_op(), "occurred_at_utc": "yesterday"}, "attempts": []}
    store.path.parent.mkdir(parents=True)
    records = [good, "text", {"operation": [], "attempts": []}, bad_time, {**good, "attempts": [1]}]
    store.spool_path.write_text(json.dumps({"version": 1, "records": records}), "utf-8")
    assert store.replay() == 1
    assert _lost(store) == 4
    assert [r["occurred_at_utc"] for r in _rows(store, "operations")] == [
        "2026-10-07T13:40:59.566218Z"
    ]


def test_a_full_spool_counts_the_record_as_lost(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history, "_SPOOL_LIMIT", 2)
    assert store.record(_op())
    with contextlib.closing(sqlite3.connect(store.path, isolation_level=None)) as other:
        other.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(history, "_WRITE_BUDGET", 0.05)
        for _ in range(3):
            assert store.record(_op()) is False
        other.execute("ROLLBACK")
    health = store.health()
    assert (health.spooled, health.lost_writes) == (2, 1)


def test_a_spool_that_cannot_be_written_counts_the_record_as_lost(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    def timeout(*args: Any, **kwargs: Any) -> None:
        raise TimeoutError("history write budget spent")

    monkeypatch.setattr(HistoryStore, "_save_spool", fail)
    monkeypatch.setattr(HistoryStore, "_write", timeout)
    assert store.record(_op()) is False
    assert _lost(store) == 1
    assert not store.spool_path.exists()


# #213: the first write on a slow machine (endpoint scanning) ran past its budget and was lost.
def test_a_first_write_that_times_out_is_replayed_by_stats(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_real_budget(monkeypatch)
    real_write = HistoryStore._write
    calls: list[float] = []

    def slow_first(self: HistoryStore, *args: Any, **kwargs: Any) -> None:
        calls.append(args[2])
        if len(calls) == 1:
            raise TimeoutError("history write budget spent")
        real_write(self, *args, **kwargs)

    monkeypatch.setattr(HistoryStore, "_write", slow_first)
    assert store.record(_op(trigger_id="-i-"), [_attempt()]) is False
    assert store.health().spooled == 1
    (row,) = store.stats()
    assert (row.key, row.operations, row.attempts) == ("-i-", 1, 1)
    health = store.health()
    assert (health.spooled, health.lost_writes) == (0, 0)


# #241: an unlocked reader made another process's os.replace of the spool fail on Windows.
def test_the_spool_is_read_only_under_the_sidecar_lock(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spool_one(store, monkeypatch)
    lock = store.lost_path.with_name(f"{history.LOST_NAME}.lock")
    real_load = HistoryStore._load_spool
    locked: list[bool] = []

    def load(self: HistoryStore) -> list[object]:
        locked.append(lock.exists())
        return real_load(self)

    monkeypatch.setattr(HistoryStore, "_load_spool", load)
    assert store.record(_op())
    assert locked
    assert all(locked)
    assert not store.spool_path.exists()


def test_a_spool_locked_by_another_process_is_left_for_later(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    spooled = _spool_one(store, monkeypatch)
    lock = store.lost_path.with_name(f"{history.LOST_NAME}.lock")
    lock.touch()  # another process is reading or writing the spool
    assert store._replayable((), time.monotonic() + 0.05) == ([], False)
    # Waiting for the lock would spend the write budget and spool this record too.
    assert store.record(_op())
    assert spooled not in {r["id"] for r in _rows(store, "operations")}
    assert len(_spool(store)["records"]) == 1
    lock.unlink()
    records, pending = store._replayable((), time.monotonic() + 0.05)
    assert pending
    assert [op[0] for op, _ in records] == [spooled]


def test_without_a_spool_record_takes_no_sidecar_lock(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_lock(self: HistoryStore, deadline: float) -> None:
        raise AssertionError("sidecar lock taken")

    monkeypatch.setattr(HistoryStore, "_sidecar_lock", no_lock)
    assert store.record(_op())
    assert len(_rows(store, "operations")) == 1


def test_health_reads_the_sidecar_only_under_its_lock(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_lock = HistoryStore._sidecar_lock
    taken: list[float] = []

    def lock(self: HistoryStore, deadline: float) -> Any:
        taken.append(deadline)
        return real_lock(self, deadline)

    monkeypatch.setattr(HistoryStore, "_sidecar_lock", lock)
    store.health()
    assert taken == []  # no sidecar files: nothing to lock, nothing created
    assert not store.path.parent.exists()
    _spool_one(store, monkeypatch)
    taken.clear()
    assert store.health().spooled == 1
    assert len(taken) == 1
    held = store.lost_path.with_name(f"{history.LOST_NAME}.lock")
    held.touch()  # another process holds it
    health = store.health()
    assert (health.spooled, health.tracking_incomplete) == (0, True)
    held.unlink()


def test_an_empty_database_file_still_gets_the_creating_budget(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadlines: list[float] = []
    monkeypatch.setattr(
        HistoryStore, "_write", lambda self, op, rows, deadline, *a, **k: deadlines.append(deadline)
    )
    clock = SimpleNamespace(monotonic=lambda: 100.0, time=time.time, sleep=time.sleep)
    monkeypatch.setattr(history, "time", clock)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b"")
    assert store.record(_op())
    assert deadlines == [100.0 + history._WRITE_BUDGET + history._CREATE_EXTRA]


def test_export_replays_the_spool(store: HistoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    spooled = _spool_one(store, monkeypatch)
    out = io.StringIO()
    assert store.export(out) == 2
    assert spooled in out.getvalue()


def test_a_database_from_a_newer_version_is_never_written(store: HistoryStore) -> None:
    assert store.record(_op())
    with contextlib.closing(sqlite3.connect(store.path)) as conn:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    assert store.record(_op()) is False
    with pytest.raises(HistoryError, match="newer"):
        store.stats()
    assert store.health().schema_version == 99


def test_migrations_run_forward(store: HistoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    assert store.record(_op())
    step = ("CREATE INDEX attempts_model ON attempts (requested_model)",)
    monkeypatch.setattr(history, "_MIGRATIONS", (*history._MIGRATIONS, step))
    monkeypatch.setattr(history, "SCHEMA_VERSION", history.SCHEMA_VERSION + 1)
    assert store.record(_op())
    with contextlib.closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == history.SCHEMA_VERSION
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(attempts)")}
    assert "attempts_model" in indexes
    assert len(_rows(store, "operations")) == 2


def test_health_reports_without_creating_the_database(store: HistoryStore) -> None:
    health = store.health()
    assert health.sqlite_version == sqlite3.sqlite_version
    assert (health.exists, health.schema_version, health.operations) == (False, None, None)
    assert health.journal_mode is None
    assert not store.path.parent.exists()
    assert store.record(_op(), [_attempt(), _attempt(attempt=2)])
    health = store.health()
    assert (health.exists, health.schema_version) == (True, history.SCHEMA_VERSION)
    assert (health.operations, health.attempts, health.lost_writes) == (1, 2, 0)
    assert health.path == str(store.path)
    assert health.tracking_incomplete is False


def test_an_unreadable_sidecar_marks_tracking_incomplete(store: HistoryStore) -> None:
    store.lost_path.parent.mkdir(parents=True)
    store.lost_path.write_text("{half a file", "utf-8")
    assert store.health().tracking_incomplete is True


# -- concurrency (separate processes, as Espanso runs one CLI per trigger) -----------------

_WRITER = """
import json, sys
from pathlib import Path
from promptmend.history import HistoryStore
store = HistoryStore(Path(sys.argv[1]))
worker = int(sys.argv[2])
stored = []
for n in range({count}):
    op = {{"id": f"{{worker:016x}}{{n:016x}}", "origin": "direct", "kind": "improve",
          "outcome": "ok", "latency_ms": 1.0}}
    attempts = [{{"provider": "ollama", "requested_model": "qwen3:8b", "endpoint": "loopback",
                 "latency_ms": 1.0, "cost_state": "not_applicable"}}] * 2
    # The second write is a retry of the first: it must never add a row.
    if any([store.record(op, attempts), store.record(op, attempts)]):
        stored.append(op["id"])
print(json.dumps(stored))
"""


def _run_workers(
    tmp_path: Path, script: str, args_for: Callable[[int], list[str]], workers: int = 4
) -> list[Any]:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, *args_for(worker)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for worker in range(workers)
    ]
    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        outputs.append(json.loads(out))
    return outputs


def test_concurrent_writers_complete_or_drop_without_duplicates(
    store: HistoryStore, tmp_path: Path
) -> None:
    count = 15
    outputs = _run_workers(
        tmp_path,
        _WRITER.format(count=count),
        lambda worker: [str(store.path), str(worker)],
    )
    stored = {op_id for out in outputs for op_id in out}
    # A write that lost the race waits in the spool (#213) and is stored by a later one.
    store.replay()
    ops = _rows(store, "operations")
    ids = {row["id"] for row in ops}
    assert stored <= ids
    assert ids <= {f"{worker:016x}{n:016x}" for worker in range(4) for n in range(count)}
    assert len(ops) == len(ids)
    assert store.health().spooled == 0
    keys = [(row["operation_id"], row["seq"]) for row in _rows(store, "attempts")]
    assert len(keys) == len(set(keys)) == 2 * len(ids)
    # Each write is one transaction: no operation without its attempts.
    assert {op_id for op_id, _ in keys} == ids
    # Most writes get through; the rest were spooled (and are stored now) or counted as lost.
    assert len(stored) > count
    if len(ids) < 4 * count:
        assert store.health().lost_writes > 0


_MARKER = """
import sys
from pathlib import Path
from promptmend.history import HistoryStore
store = HistoryStore(Path(sys.argv[1]))
print(sum(store._mark_lost() for _ in range(25)))
"""


def test_the_sidecar_marker_is_updated_atomically(store: HistoryStore, tmp_path: Path) -> None:
    store.path.parent.mkdir(parents=True)
    outputs = _run_workers(tmp_path, _MARKER, lambda worker: [str(store.path)])
    # Every successful update is counted (no lost update), and the file is whole JSON.
    assert _lost(store) == sum(outputs) > 0
    assert sorted(p.name for p in store.path.parent.iterdir()) == [history.LOST_NAME]


# -- services ------------------------------------------------------------------------------


def _seed(store: HistoryStore) -> None:
    day1 = datetime(2026, 10, 1, 9, tzinfo=UTC)
    day2 = datetime(2026, 10, 2, 9, tzinfo=UTC)
    store.prices_path = None
    assert store.record(
        _op(occurred_at_utc=day1, latency_ms=100.0),
        [
            _attempt(status=503, error_kind="non_2xx", charged_amount=None, charged_unit=None),
            _attempt(attempt=2, charged_amount=Decimal("0.001")),
        ],
    )
    assert store.record(
        _op(occurred_at_utc=day2, latency_ms=300.0),
        [_attempt(charged_amount=Decimal("0.002"), cache_read=None)],
    )
    assert store.record(
        _op(occurred_at_utc=day2, trigger_id="-ic-", latency_ms=200.0),
        [
            _attempt(
                provider="anthropic",
                requested_model="claude-sonnet-5",
                charged_amount=None,
                charged_unit=None,
                cost_state="unknown",
                cache_read=None,
            )
        ],
    )
    assert store.record(_op(occurred_at_utc=day2, trigger_id="-p-", kind="persona"))


def test_stats_by_trigger_keep_costs_apart(store: HistoryStore) -> None:
    _seed(store)
    rows = {row.key: row for row in store.stats("trigger")}
    i = rows["-i-"]
    assert (i.operations, i.attempts) == (2, 3)
    assert i.reported == {"credits": Decimal("0.003")}
    assert i.estimated == {}
    assert i.unknown_cost_attempts == 1  # the 503 reported no cost: unknown, never 0
    assert (i.latency_p50_ms, i.latency_p95_ms) == (100.0, 300.0)
    assert i.tokens["output"] == 900
    assert i.tokens["cache_read"] == 200
    assert i.tokens["cache_write"] is None  # never reported: unknown, not 0
    assert i.last_used_utc.startswith("2026-10-02T09:00:00")
    ic = rows["-ic-"]
    assert (ic.reported, ic.unknown_cost_attempts) == ({}, 1)
    assert ic.tokens["cache_read"] is None
    p = rows["-p-"]
    assert (p.operations, p.attempts, p.tokens["output"]) == (1, 0, None)


def test_stats_by_provider_model_and_day(store: HistoryStore) -> None:
    _seed(store)
    providers = {row.key: row for row in store.stats("provider")}
    assert set(providers) == {"openrouter", "anthropic"}  # persona made no request
    assert (providers["openrouter"].operations, providers["openrouter"].attempts) == (2, 3)
    assert providers["openrouter"].latency_p50_ms == 800.0
    models = {row.key for row in store.stats("model")}
    assert models == {"google/gemini-3.5-flash-lite", "claude-sonnet-5"}
    days = store.stats("day")
    assert [row.key for row in days] == ["2026-10-02", "2026-10-01"]
    assert days[0].operations == 3
    with pytest.raises(ValueError, match="group_by"):
        store.stats("profile")


def test_stats_and_export_of_no_history_are_empty(store: HistoryStore) -> None:
    assert store.stats() == []
    out = io.StringIO()
    assert store.export(out, "json") == 0
    assert json.loads(out.getvalue())["operations"] == []
    assert store.prune() == 0
    assert not store.path.exists()


def test_export_csv_is_one_row_per_attempt(store: HistoryStore) -> None:
    _seed(store)
    out = io.StringIO()
    assert store.export(out, "csv") == 4
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert len(rows) == 5  # 2 + 1 + 1 attempts, and the persona with none
    assert rows[0]["operation_trigger_id"] == "-i-"
    assert rows[1]["charged_amount"] == "0.001"
    assert rows[1]["charged_unit"] == "credits"
    persona = [row for row in rows if row["operation_kind"] == "persona"]
    assert [row["provider"] for row in persona] == [""]
    assert set(rows[0]) == {
        *(f"operation_{c}" for c in ALLOWED["operations"]),
        *ALLOWED["attempts"][1:],
    }
    with pytest.raises(ValueError, match="csv or json"):
        store.export(io.StringIO(), "xml")  # type: ignore[arg-type]


def test_prune_deletes_old_operations_and_their_attempts(store: HistoryStore) -> None:
    now = datetime.now(UTC)
    old = _op(occurred_at_utc=now - timedelta(days=400))
    assert store.record(old, [_attempt()])
    assert store.record(_op(occurred_at_utc=now - timedelta(days=10)), [_attempt()])
    assert store.prune() == 1  # retention_days defaults to 365
    assert [r["operation_id"] for r in _rows(store, "attempts")] != [old["id"]]
    assert len(_rows(store, "attempts")) == 1
    assert store.prune(older_than=timedelta(days=5)) == 1
    assert store.health().operations == 0
    with pytest.raises(ValueError, match="negative"):
        store.prune(older_than=timedelta(days=-1))


# record(prune=True), the trigger recorder's write, also deletes a batch of old operations,
# oldest first; a plain record() never prunes.
def test_record_can_prune_a_batch(store: HistoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "_PRUNE_BATCH", 2)
    now = datetime.now(UTC)
    old = [_op(occurred_at_utc=now - timedelta(days=400 + n)) for n in range(3)]
    for op in old:
        assert store.record(op, [_attempt()])
    assert store.health().operations == 3
    assert store.record(_op(), [_attempt()], prune=True)
    left = {r["id"] for r in _rows(store, "operations")}
    assert old[0]["id"] in left  # the newest of the old ones waits for the next write
    assert len(left) == 2
    assert len(_rows(store, "attempts")) == 2


# A prune that fails is rolled back on its own: the record it rode along with is kept.
def test_failed_prune_keeps_the_record(store: HistoryStore) -> None:
    old = _op(occurred_at_utc=datetime.now(UTC) - timedelta(days=400))
    assert store.record(old)
    with contextlib.closing(sqlite3.connect(store.path)) as conn:
        conn.execute(
            "CREATE TRIGGER no_delete BEFORE DELETE ON operations "
            "BEGIN SELECT RAISE(ABORT, 'no'); END"
        )
        conn.commit()
    new = _op()
    assert store.record(new, [_attempt()], prune=True)
    assert {r["id"] for r in _rows(store, "operations")} == {old["id"], new["id"]}
    assert store.health().lost_writes == 0


# A prune the time budget interrupts is rolled back (SQLite rolls back its whole transaction
# on an interrupt), and the record, already committed, stays. No clock: the prune is handed a
# deadline already past and checks it at every VM step.
def test_interrupted_prune_keeps_the_record(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = [_op(occurred_at_utc=datetime.now(UTC) - timedelta(days=400)) for _ in range(5)]
    for op in old:
        assert store.record(op, [_attempt()])
    real_prune = history._prune_batch
    calls: list[str] = []

    def past_deadline(conn: sqlite3.Connection, cutoff: str, deadline: float) -> None:
        calls.append(cutoff)
        real_prune(conn, cutoff, float("-inf"))

    monkeypatch.setattr(history, "_prune_batch", past_deadline)
    monkeypatch.setattr(history, "_PROGRESS_STEPS", 1)
    new = _op()
    assert store.record(new, [_attempt()], prune=True)
    assert calls
    ids = {r["id"] for r in _rows(store, "operations")}
    assert ids == {new["id"], *(op["id"] for op in old)}
    assert len(_rows(store, "attempts")) == 6
    assert store.health().lost_writes == 0
    # The connection was left usable: the next write and prune go through.
    monkeypatch.setattr(history, "_prune_batch", real_prune)
    assert store.record(_op(), prune=True)
    assert store.health().operations == 2


# The write that creates the database gets _CREATE_EXTRA on top of the budget; later ones not.
def test_creating_write_gets_extra_time(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadlines: list[float] = []
    real_write = HistoryStore._write

    def spy(
        self: HistoryStore,
        op: tuple[object, ...],
        rows: list[tuple[object, ...]],
        deadline: float,
        prune_before: str | None = None,
        **kwargs: Any,
    ) -> None:
        deadlines.append(deadline)
        return real_write(self, op, rows, deadline, prune_before, **kwargs)

    monkeypatch.setattr(HistoryStore, "_write", spy)
    _use_real_budget(monkeypatch)
    # A clock that stands still, so the deadlines are exact rather than timed.
    clock = SimpleNamespace(monotonic=lambda: 100.0, time=time.time, sleep=time.sleep)
    monkeypatch.setattr(history, "time", clock)
    assert store.record(_op())
    assert store.record(_op())
    assert deadlines == [
        100.0 + history._WRITE_BUDGET + history._CREATE_EXTRA,
        100.0 + history._WRITE_BUDGET,
    ]


def test_reset_deletes_every_record_and_the_marker(store: HistoryStore) -> None:
    _seed(store)
    store._mark_lost()
    store.reset()
    health = store.health()
    assert (health.operations, health.attempts, health.lost_writes) == (0, 0, 0)
    store.reset()  # nothing left to reset
    # Every connection is closed: the files can be deleted (Windows refuses an open one).
    for path in store.path.parent.iterdir():
        path.unlink()


# -- estimated costs (D-HIST-1) ------------------------------------------------------------

PRICES = """
version = "2026-10-01"
unit = "USD"
[models."claude-sonnet-5"]
input_uncached = 3.00
cache_read = 0.30
cache_write = 3.75
output = 15
"""


def test_estimates_come_only_from_a_price_table(store: HistoryStore, tmp_path: Path) -> None:
    unknown = {"charged_amount": None, "charged_unit": None, "cost_state": "unknown"}
    sonnet = _attempt(
        provider="anthropic",
        requested_model="claude-sonnet-5",
        input_uncached=1000,
        cache_read=2000,
        cache_write=None,
        output=500,
        reasoning=400,
        **unknown,
    )
    assert store.record(_op(), [sonnet])  # no table: unknown
    store.prices_path = tmp_path / "prices.toml"
    store.prices_path.write_text(PRICES, "utf-8")
    store._prices_loaded = False
    assert store.record(
        _op(),
        [
            sonnet,
            _attempt(),  # reported: never estimated
            _attempt(requested_model="unlisted", **unknown),
            _attempt(provider="ollama", cost_state="not_applicable", charged_amount=None),
        ],
    )
    rows = _rows(store, "attempts")
    assert (rows[0]["cost_state"], rows[0]["estimated_amount"]) == ("unknown", None)
    estimate = rows[1]
    # 1000 * 3 + 2000 * 0.30 + 500 * 15 per million tokens; reasoning is part of output.
    assert Decimal(estimate["estimated_amount"]) == Decimal("0.0111")
    assert (estimate["cost_state"], estimate["estimated_unit"]) == ("estimated", "USD")
    assert estimate["price_table_version"] == "2026-10-01"
    assert estimate["charged_amount"] is None
    assert [r["estimated_amount"] for r in rows[2:]] == [None, None, None]
    assert [r["cost_state"] for r in rows[2:]] == ["reported", "unknown", "not_applicable"]
    row = next(r for r in store.stats("trigger"))
    assert row.estimated == {"USD": Decimal("0.0111")}
    assert row.reported == {"credits": _attempt()["charged_amount"]}


def test_a_count_without_a_price_gets_no_estimate(tmp_path: Path) -> None:
    path = tmp_path / "prices.toml"
    path.write_text(PRICES.replace("cache_write = 3.75\n", ""), "utf-8")
    table = load_price_table(path)
    assert table is not None
    assert table.estimate("claude-sonnet-5", {"input_uncached": 1, "cache_write": 1}) is None
    assert table.estimate("claude-sonnet-5", {"output": None}) is None
    assert table.estimate("claude-sonnet-5", {"output": 1_000_000}) == ("15", "USD", "2026-10-01")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("version = ", "not valid TOML"),
        ('unit = "USD"\n[models.m]\noutput = 1', "needs a version"),
        ('version = "1"\nunit = "EUR"\n[models.m]\noutput = 1', "unit"),
        ('version = "1"\nunit = "USD"', "models"),
        ('version = "1"\nunit = "USD"\n[models.m]\ntokens = 1', "unknown price"),
        ('version = "1"\nunit = "USD"\n[models.m]\noutput = -1', "0 or more"),
        ('version = "1"\nunit = "USD"\n[models.m]\noutput = "cheap"', "0 or more"),
        ('version = "1"\nunit = "USD"\nmodels = {m = 1}', "must be a table"),
    ],
)
def test_a_malformed_price_table_is_reported(tmp_path: Path, text: str, error: str) -> None:
    path = tmp_path / "prices.toml"
    path.write_text(text, "utf-8")
    with pytest.raises(ValueError, match=error):
        load_price_table(path)


def test_a_broken_price_table_never_stops_a_write(store: HistoryStore, tmp_path: Path) -> None:
    assert load_price_table(tmp_path / "missing.toml") is None
    store.prices_path = tmp_path / "prices.toml"
    store.prices_path.write_text("version = ", "utf-8")
    unknown = _attempt(charged_amount=None, charged_unit=None, cost_state="unknown")
    assert store.record(_op(), [unknown])
    assert _rows(store, "attempts")[0]["cost_state"] == "unknown"


def test_a_stale_sidecar_lock_is_broken_and_a_held_one_times_out(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.path.parent.mkdir(parents=True)
    lock = store.lost_path.with_name(f"{history.LOST_NAME}.lock")
    lock.touch()
    old = time.time() - 60
    os.utime(lock, (old, old))
    assert store._mark_lost() is True  # left by a killed process
    lock.touch()
    _use_real_budget(monkeypatch)
    assert store._mark_lost() is False  # held: give up within the lock budget
    assert _lost(store) == 1
    lock.unlink()


def test_a_service_migrates_an_empty_database(store: HistoryStore) -> None:
    store.path.parent.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(store.path)) as conn:
        conn.execute("PRAGMA user_version = 0")
    assert store.stats() == []
    assert store.health().schema_version == history.SCHEMA_VERSION


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("file is not a database", "corrupt"),
        ("database disk image is malformed", "corrupt"),
        ("database is locked", "locked"),
        ("attempt to write a readonly database", "read-only"),
        ("disk I/O error", "unreadable"),
    ],
)
def test_database_errors_become_fixed_words(message: str, kind: str) -> None:
    assert history._error_kind(sqlite3.DatabaseError(message)) == kind


def test_reset_never_deletes_a_database_that_is_only_locked(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history, "_SERVICE_TIMEOUT", 0.05)
    assert store.record(_op())
    with contextlib.closing(sqlite3.connect(store.path, isolation_level=None)) as other:
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(HistoryError, match="locked") as caught:
            store.reset()
        assert not isinstance(caught.value, history.UnusableHistory)
        other.execute("ROLLBACK")
    assert store.path.exists()
    assert store.health().operations == 1


@pytest.mark.parametrize("text", ["[]", "5", '"x"', '{"lost_writes": true}', "\xff"])
def test_a_malformed_sidecar_never_breaks_health_and_is_rewritten(
    store: HistoryStore, text: str
) -> None:
    store.lost_path.parent.mkdir(parents=True)
    store.lost_path.write_text(text, "latin-1")
    assert store.health().tracking_incomplete is True
    assert store._mark_lost() is True
    assert _lost(store) == 1


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX directory modes, which root ignores",
)
def test_health_shows_a_store_that_cannot_be_written(store: HistoryStore) -> None:
    assert store.record(_op())
    assert store.health().writable is True
    store.path.chmod(stat.S_IREAD)
    store.path.parent.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        assert store.record(_op()) is False  # and the sidecar cannot be written either
        health = store.health()
    finally:
        store.path.parent.chmod(stat.S_IRWXU)
        store.path.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert not store.lost_path.exists()
    assert (health.writable, health.tracking_incomplete, health.lost_writes) == (False, True, 0)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_health_shows_a_read_only_database(store: HistoryStore) -> None:
    assert store.record(_op())
    store.path.chmod(stat.S_IREAD)
    try:
        health = store.health()
    finally:
        store.path.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert (health.writable, health.tracking_incomplete) == (False, True)


def test_retention_is_capped_and_prune_never_overflows(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPT_HISTORY_RETENTION_DAYS", "36501")
    with pytest.raises(ValueError, match="PROMPT_HISTORY_RETENTION_DAYS must be a whole number"):
        Settings.load()
    monkeypatch.setenv("PROMPT_HISTORY_RETENTION_DAYS", "36500")
    assert Settings.load().history_retention_days == 36500
    assert store.record(_op())
    assert store.prune(older_than=timedelta(days=10**8)) == 0
    store.retention_days = 10**8
    assert store.prune() == 0


def test_a_fresh_lock_is_never_broken_and_only_the_holder_removes_it(store: HistoryStore) -> None:
    store.path.parent.mkdir(parents=True)
    lock = store.lost_path.with_name(f"{history.LOST_NAME}.lock")
    lock.write_text("someone else", "ascii")
    assert history._break_stale(lock) is False
    assert lock.read_text("ascii") == "someone else"
    lock.unlink()
    with store._sidecar_lock(time.monotonic() + 1):
        # Another process broke our lock as stale and took its own.
        lock.write_text("someone else", "ascii")
    assert lock.read_text("ascii") == "someone else"
    lock.unlink()


def test_a_write_blocked_everywhere_gives_up(
    store: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert store.record(_op())
    lock = store.lost_path.with_name(f"{history.LOST_NAME}.lock")
    lock.write_text("held", "ascii")
    with contextlib.closing(sqlite3.connect(store.path, isolation_level=None)) as other:
        other.execute("BEGIN EXCLUSIVE")
        _use_real_budget(monkeypatch)
        started = time.monotonic()
        assert store.record(_op()) is False
        # Generous bound; _BUDGET (0.25 s) is checked by reading the code, not the clock.
        assert time.monotonic() - started < 5
        other.execute("ROLLBACK")
    lock.unlink()
    assert not store.lost_path.exists()
