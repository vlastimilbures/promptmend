"""Local usage history: a per-device SQLite store of call metadata, and its services.

The store holds metadata only, from a fixed allowlist of columns (OPERATION_COLUMNS,
ATTEMPT_COLUMNS): never prompt, clipboard, output, persona, key, form or raw-body text. A
record is a plain mapping; only allowlisted keys are ever read from it, and a text value must
be a known enum or a short identifier with no spaces, so prose cannot get in.

Writes (HistoryStore.record) are fail-open: a locked, read-only, full or corrupt database
never raises to the caller, and a write gives up within a fixed time budget (a little more for
the one write that creates the database). A write that misses its budget is kept in a small
spool file of the same allowlisted columns, which the next record(), stats(), export() or
replay() stores (#213). A record that is truly dropped (the spool is full or cannot be
written) bumps a small sidecar marker, which health() reports, so lost tracking stays visible.

sqlite3, tomllib, csv and decimal are imported inside the functions that use them, so
importing this module (for the paths) costs nothing on the trigger path.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal

from . import __version__
from .config import LEGACY_APP_DIR, MAX_RETENTION_DAYS, _user_config_dir, user_data_dir
from .redaction import compile_extra, is_bare_token, scan

if TYPE_CHECKING:
    import sqlite3
    from decimal import Decimal

    from .config import Settings

DB_NAME = "history.sqlite3"
# Sidecar marker of dropped writes, next to the database (JSON: count and last time).
LOST_NAME = "history.lost"
# Records whose write missed its budget, next to the database, until a later write stores them
# (JSON: {"version": 1, "records": [{"operation": {column: value}, "attempts": [...]}]}).
SPOOL_NAME = "history.spool"
_SPOOL_VERSION = 1
# Most records the spool holds; a record past it is dropped and counted as lost.
_SPOOL_LIMIT = 100
# Largest spool file read or written; a larger one counts as corrupt.
_SPOOL_BYTES = 512 * 1024
# A database file smaller than this has no schema yet (one with both tables is ~24 KiB), so the
# write that finally creates it still gets _CREATE_EXTRA: a first write that ran out of time
# can leave an empty file behind.
_NO_SCHEMA_BYTES = 8192
# The optional user price table for estimated costs, in the config dir (D-HIST-1).
PRICES_NAME = "prices.toml"

# Time budget of one record() call, from its start: validation, reading the price table,
# importing sqlite3, connecting, waiting for a lock, writing and, for a dropped write, the
# sidecar marker. The trigger waits for it while the user waits for the paste, so it stays at
# what a person barely notices even under contention; a write that would take longer is
# dropped and counted as lost. A file-system call the OS itself blocks (a stalled network
# drive) cannot be interrupted, and neither can the interpreter's own pauses; those are the
# only things outside it.
_BUDGET = 0.25
# The database's share of _BUDGET (its busy timeout), the rest is for the sidecar marker.
# SQLite's own lock retries overshoot the busy timeout by up to ~0.06 s (measured in WAL mode
# on a write blocked by an exclusive lock), so the share leaves room for that: a write blocked
# on both the database and the sidecar lock returned after 0.23-0.24 s.
_WRITE_BUDGET = 0.15
# Kept back from _BUDGET for writing the sidecar once its lock is taken.
_MARGIN = 0.02
# Added to both budgets for a write that has to create the database (once per device, and
# after a reset): creating the file, setting its journal mode and running the migrations costs
# several syncs, which took longer than the whole budget on a slow CI disk.
_CREATE_EXTRA = 0.75
# Most old operations an automatic prune (record(prune=True)) deletes in one write, so a
# lowered retention is caught up over a few triggers instead of in one long transaction.
_PRUNE_BATCH = 100
# How many SQLite VM steps run between checks of the time budget.
_PROGRESS_STEPS = 1000
# A sidecar lock older than this was left by a killed process (it is held for a few ms).
_STALE_LOCK = 10.0
# Busy timeout for management commands (stats, export, prune, reset), which nobody pastes.
_SERVICE_TIMEOUT = 5.0
# WAL could rarely reset a database before SQLite 3.51.3, so older libraries keep the
# rollback journal (journal_mode DELETE), and a database left in WAL is switched back.
WAL_FIXED = (3, 51, 3)

ORIGINS = ("espanso_managed", "direct")
KINDS = ("improve", "persona", "static")
ENDPOINTS = ("loopback", "remote")
COST_STATES = ("reported", "estimated", "unknown", "not_applicable")
UNITS = ("credits", "USD")
TOKEN_FIELDS = ("input_uncached", "cache_read", "cache_write", "output", "reasoning")
SERVER_FIELDS = ("server_total_ms", "server_load_ms", "server_input_ms", "server_output_ms")
GROUP_BY = ("trigger", "provider", "model", "day")

# Every column, by table. tests/test_history.py fails when the schema has any other.
OPERATION_COLUMNS = (
    "id",
    "occurred_at_utc",
    "origin",
    "trigger_id",
    "kind",
    "profile_id",
    "outcome",
    "latency_ms",
    "app_version",
)
# The AttemptUsage fields of providers/usage.py, keyed by (operation_id, seq), plus the
# optional estimate (D-HIST-1).
ATTEMPT_COLUMNS = (
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
    *TOKEN_FIELDS,
    "charged_amount",
    "charged_unit",
    "upstream_cost",
    "upstream_unit",
    *SERVER_FIELDS,
    "estimated_amount",
    "estimated_unit",
    "price_table_version",
)

# Forward migrations: _MIGRATIONS[n] takes a database from schema version n to n + 1. Append
# a new step for a schema change; never edit a released one.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE operations (
            id TEXT PRIMARY KEY,
            occurred_at_utc TEXT NOT NULL,
            origin TEXT NOT NULL CHECK (origin IN ('espanso_managed', 'direct')),
            trigger_id TEXT,
            kind TEXT NOT NULL CHECK (kind IN ('improve', 'persona', 'static')),
            profile_id TEXT,
            outcome TEXT NOT NULL,
            latency_ms REAL,
            app_version TEXT NOT NULL
        )""",
        "CREATE INDEX operations_occurred ON operations (occurred_at_utc)",
        # Money is exact decimal text with its unit, never a float. Credits are never USD.
        """CREATE TABLE attempts (
            operation_id TEXT NOT NULL REFERENCES operations (id) ON DELETE CASCADE,
            seq INTEGER NOT NULL CHECK (seq > 0),
            provider TEXT NOT NULL,
            requested_model TEXT NOT NULL,
            endpoint TEXT NOT NULL CHECK (endpoint IN ('loopback', 'remote')),
            attempt INTEGER NOT NULL,
            status INTEGER,
            error_kind TEXT,
            latency_ms REAL NOT NULL,
            cost_state TEXT NOT NULL
                CHECK (cost_state IN ('reported', 'estimated', 'unknown', 'not_applicable')),
            returned_model TEXT,
            returned_provider TEXT,
            generation_id TEXT,
            input_uncached INTEGER,
            cache_read INTEGER,
            cache_write INTEGER,
            output INTEGER,
            reasoning INTEGER,
            charged_amount TEXT,
            charged_unit TEXT CHECK (charged_unit IN ('credits', 'USD')),
            upstream_cost TEXT,
            upstream_unit TEXT CHECK (upstream_unit IN ('credits', 'USD')),
            server_total_ms REAL,
            server_load_ms REAL,
            server_input_ms REAL,
            server_output_ms REAL,
            estimated_amount TEXT,
            estimated_unit TEXT CHECK (estimated_unit IN ('credits', 'USD')),
            price_table_version TEXT,
            PRIMARY KEY (operation_id, seq),
            CHECK ((charged_amount IS NULL) = (charged_unit IS NULL)),
            CHECK ((upstream_cost IS NULL) = (upstream_unit IS NULL)),
            CHECK ((estimated_amount IS NULL) = (estimated_unit IS NULL))
        )""",
    ),
)
SCHEMA_VERSION = len(_MIGRATIONS)

# Each identifier column has its own shape, as tight as its real values allow. None takes a
# space, `@` or `\\`, so a sentence, an email, a `user:pass@host` or a Windows path never fits.
# Trigger names (-i-, -ip-, -iok-).
_TRIGGER = re.compile(r"-[a-z]{1,8}-")
# Profile names, built-in or the user's own (lower case, like the prompts/*.md file names).
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# Provider names (factory.PROVIDER_NAMES) and the slug of the endpoint that served a call.
_PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Model ids need `/` (vendor/model, hf.co/user/repo) and `:` (qwen3:8b, a :free variant),
# so only the two model columns allow them; never at the start, so an absolute path fails.
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")
# Response ids of the shapes providers use: OpenRouter gen-…, Anthropic msg_…, OpenAI-style
# chatcmpl-…. Anything else is not kept.
_GENERATION_ID = re.compile(r"(?:gen|msg|chatcmpl)[-_][A-Za-z0-9_-]{1,120}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}")
_OUTCOME = re.compile(r"[a-z][a-z0-9_]{0,39}")
_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
# The error kinds providers/base.py records (see AttemptUsage); tests/test_history.py checks
# that the two lists match.
ERROR_KINDS = (
    "timeout",
    "non_2xx",
    "invalid_json",
    "body_error",
    "refused",
    "transport",
    "invalid_url",
    "invalid_header",
    "unexpected",
)


class HistoryError(Exception):
    """A history service (stats, export, prune, reset) could not read or change the store."""


class UnusableHistory(HistoryError):
    """The database is corrupt or was written by a newer version: reset() may delete it."""


class InvalidRecord(ValueError):
    """A record value is not on the allowlist; the whole record is dropped."""


# One record as database rows: the operation row and its attempt rows.
_Spooled = tuple[tuple[object, ...], list[tuple[object, ...]]]


def wal_is_safe(version: str) -> bool | None:
    """Whether SQLite ``version`` (``sqlite3.sqlite_version``) is WAL_FIXED or later; None
    for an unparsable version (callers that must choose treat it as not safe)."""
    try:
        parts = tuple(int(part) for part in version.split("."))
    except ValueError:
        return None
    return parts >= WAL_FIXED


def history_path(environ: Mapping[str, str] = os.environ) -> Path:
    return user_data_dir(environ) / DB_NAME


def price_table_path(environ: Mapping[str, str] = os.environ) -> Path:
    return _user_config_dir(environ) / PRICES_NAME


def new_operation_id() -> str:
    """A random id for one CLI invocation (per process, never derived from its input)."""
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(UTC).strftime(_TIME_FORMAT)


def _timestamp(value: object) -> str:
    if value is None:
        return _now()
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).strftime(_TIME_FORMAT)
    raise InvalidRecord("occurred_at_utc must be an aware datetime")


def _choice(value: object, allowed: Sequence[str], *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if value in allowed:
        return str(value)
    raise InvalidRecord("not an allowed value")


def _looks_secret(value: str, extra: tuple[re.Pattern[str], ...]) -> bool:
    """Whether the gate would flag ``value`` (a key, a password assignment, a user pattern
    from PROMPT_EXTRA_PATTERNS), or any part of it between `/ : . _ -` is a password- or
    token-like word. Real model ids look token-like whole (Llama-3.3-70B-Instruct:free), so
    the bare-token check runs on the parts."""
    if scan(value, extra):
        return True
    return any(is_bare_token(part) for part in re.split(r"[/:._-]", value))


def _ident(
    value: object,
    shape: re.Pattern[str],
    extra: tuple[re.Pattern[str], ...] = (),
    *,
    required: bool = False,
) -> str | None:
    """``value`` when it has ``shape`` and does not look like a secret, else None (or
    InvalidRecord for a ``required`` column, which drops the record)."""
    if isinstance(value, str) and shape.fullmatch(value) and not _looks_secret(value, extra):
        return value
    if required:
        raise InvalidRecord("not an identifier")
    return None


def _int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _float(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _money(value: object) -> str | None:
    """Exact decimal text of a finite, non-negative amount (a Decimal or an int, or a float
    or string as its decimal text), never in exponent form and never `-0`; None for anything
    else, including a missing amount."""
    from decimal import Decimal, InvalidOperation

    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)) if isinstance(value, int | float | str) else value
    except InvalidOperation:
        return None
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
        return None
    return format(amount.copy_abs(), "f")


def _amount(record: Mapping[str, object], amount: str, unit: str) -> tuple[str | None, ...]:
    """(amount, unit), or (None, None) when either is missing or not valid: a bad amount
    leaves the cost unknown, never 0, and keeps the rest of the record."""
    value, unit_value = _money(record.get(amount)), record.get(unit)
    if value is None or unit_value not in UNITS:
        return None, None
    return value, str(unit_value)


def _generation_id(value: object, extra: tuple[re.Pattern[str], ...]) -> str | None:
    """A response id of a known shape, else None. Its part after the prefix is random, so
    the bare-token check would drop every id; only the gate's patterns apply."""
    if isinstance(value, str) and _GENERATION_ID.fullmatch(value) and not scan(value, extra):
        return value
    return None


def _operation_row(
    op: Mapping[str, object], extra: tuple[re.Pattern[str], ...]
) -> tuple[str, tuple[object, ...]]:
    op_id = op.get("id")
    if not (isinstance(op_id, str) and _OPERATION_ID.fullmatch(op_id)):
        raise InvalidRecord("id must come from new_operation_id()")
    outcome = op.get("outcome")
    if not (isinstance(outcome, str) and _OUTCOME.fullmatch(outcome)):
        raise InvalidRecord("outcome must be a short lower-case word")
    return op_id, (
        op_id,
        _timestamp(op.get("occurred_at_utc")),
        _choice(op.get("origin"), ORIGINS),
        _ident(op.get("trigger_id"), _TRIGGER, extra),
        _choice(op.get("kind"), KINDS),
        _ident(op.get("profile_id"), _PROFILE, extra),
        outcome,
        _float(op.get("latency_ms")),
        _ident(op.get("app_version", __version__), _VERSION, extra) or "unknown",
    )


def _attempt_row(
    op_id: str,
    seq: int,
    at: Mapping[str, object],
    prices: PriceTable | None,
    extra: tuple[re.Pattern[str], ...],
) -> tuple[object, ...]:
    charged, charged_unit = _amount(at, "charged_amount", "charged_unit")
    upstream, upstream_unit = _amount(at, "upstream_cost", "upstream_unit")
    tokens = {name: _int(at.get(name)) for name in TOKEN_FIELDS}
    model = _ident(at.get("requested_model"), _MODEL, extra, required=True)
    # `reported` exactly when there is a charge, `estimated` only from our own price table.
    cost_state = _choice(at.get("cost_state", "unknown"), COST_STATES)
    if charged is not None:
        cost_state = "reported"
    elif cost_state != "not_applicable":
        cost_state = "unknown"
    estimate = None
    if cost_state == "unknown" and prices is not None and model is not None:
        estimate = prices.estimate(model, tokens)
        if estimate is not None:
            cost_state = "estimated"
    error_kind = at.get("error_kind")
    return (
        op_id,
        seq,
        _ident(at.get("provider"), _PROVIDER, extra, required=True),
        model,
        _choice(at.get("endpoint"), ENDPOINTS),
        _int(at.get("attempt")) or seq,
        _int(at.get("status")),
        error_kind if error_kind in ERROR_KINDS else None,
        _float(at.get("latency_ms")) or 0.0,
        cost_state,
        _ident(at.get("returned_model"), _MODEL, extra),
        _ident(at.get("returned_provider"), _PROVIDER, extra),
        _generation_id(at.get("generation_id"), extra),
        *tokens.values(),
        charged,
        charged_unit,
        upstream,
        upstream_unit,
        *(_float(at.get(name)) for name in SERVER_FIELDS),
        *(estimate or (None, None, None)),
    )


@dataclass(frozen=True)
class PriceTable:
    """A user-maintained price table (D-HIST-1): per model, a price per million tokens for
    each token category, in one unit. Estimates are kept apart from reported charges."""

    version: str
    unit: str
    models: Mapping[str, Mapping[str, Decimal]]

    def estimate(self, model: str, tokens: Mapping[str, int | None]) -> tuple[str, str, str] | None:
        """(amount, unit, table version) for an attempt's tokens, or None when the model is
        not listed, no token count is known, or a known count has no price. ``reasoning`` is
        part of ``output`` (see providers/usage.py), so it is never priced on its own."""
        from decimal import Decimal

        prices = self.models.get(model)
        counted = {k: v for k, v in tokens.items() if k != "reasoning" and v is not None}
        if prices is None or not counted or not counted.keys() <= prices.keys():
            return None
        total = sum((prices[k] * v for k, v in counted.items()), Decimal(0))
        return format(total / 1_000_000, "f"), self.unit, self.version


def load_price_table(path: Path) -> PriceTable | None:
    """The price table at ``path``, None when there is no file; ValueError when it is
    malformed. Format::

        version = "2026-10-01"
        unit = "USD"            # or "credits"
        [models."claude-sonnet-5"]
        input_uncached = 3.00   # per million tokens
        cache_read = 0.30
        cache_write = 3.75
        output = 15.00
    """
    import tomllib
    from decimal import Decimal

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{PRICES_NAME} cannot be read") from exc
    try:
        data = tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{PRICES_NAME} is not valid TOML: {exc}") from None
    version, unit, models = data.get("version"), data.get("unit"), data.get("models")
    if not (isinstance(version, str) and _VERSION.fullmatch(version)):
        raise ValueError(f'{PRICES_NAME} needs a version such as "2026-10-01"')
    if unit not in UNITS:
        raise ValueError(f"{PRICES_NAME} unit must be one of {', '.join(UNITS)}")
    if not isinstance(models, dict):
        raise ValueError(f'{PRICES_NAME} needs a [models."<model>"] table')
    table: dict[str, dict[str, Decimal]] = {}
    for model, prices in models.items():
        if not isinstance(prices, dict):
            raise ValueError(f"{PRICES_NAME}: models.{model} must be a table")
        table[model] = {}
        for category, price in prices.items():
            if category not in TOKEN_FIELDS[:4] or isinstance(price, bool):
                raise ValueError(f"{PRICES_NAME}: unknown price {model}.{category}")
            if not isinstance(price, int | Decimal) or not Decimal(price).is_finite() or price < 0:
                raise ValueError(f"{PRICES_NAME}: {model}.{category} must be a number of 0 or more")
            table[model][category] = Decimal(price)
    return PriceTable(version, unit, table)


@dataclass(frozen=True)
class Health:
    """What doctor shows about the store; never any recorded value."""

    path: str
    exists: bool
    schema_version: int | None
    operations: int | None
    attempts: int | None
    lost_writes: int
    last_lost_utc: str | None
    # Writes were lost, the sidecar marker cannot be read, or the files cannot be written.
    tracking_incomplete: bool
    # Whether the files could be created or changed (see HistoryStore._writable).
    writable: bool
    sqlite_version: str
    error: str | None = None
    # The database's journal mode ("wal", "delete", ...), None while there is no database.
    journal_mode: str | None = None
    # Records waiting in the spool for a later write to store them (0 when it cannot be read).
    spooled: int = 0


@dataclass(frozen=True)
class StatsRow:
    """One group of stats. Costs are per unit and kept apart: ``reported`` (what a provider
    charged), ``estimated`` (from the price table) and the number of attempts whose cost is
    unknown. They are never summed together, and an unknown cost never counts as 0. A token
    sum is None when no attempt in the group reported that category."""

    key: str | None
    operations: int
    attempts: int
    last_used_utc: str
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    tokens: Mapping[str, int | None]
    reported: Mapping[str, Decimal] = field(default_factory=dict)
    estimated: Mapping[str, Decimal] = field(default_factory=dict)
    unknown_cost_attempts: int = 0
    not_applicable_attempts: int = 0


class HistoryStore:
    """The usage history at ``path``. ``record()`` never raises; the services (stats, export,
    prune, reset) raise HistoryError. Every connection is closed before a method returns, so
    the files can be deleted (Windows cannot delete an open file)."""

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        retention_days: int = 365,
        prices_path: Path | None = None,
        extra_patterns: str = "",
    ) -> None:
        self.path = path
        self.enabled = enabled
        self.retention_days = retention_days
        self.prices_path = prices_path
        # PROMPT_EXTRA_PATTERNS: an identifier one of them matches is never stored. Compiled
        # in record(), so an invalid one drops the write instead of raising.
        self.extra_patterns = extra_patterns
        self.lost_path = path.with_name(LOST_NAME)
        self.spool_path = path.with_name(SPOOL_NAME)
        self._prices: PriceTable | None = None
        self._prices_loaded = False

    @classmethod
    def from_settings(cls, cfg: Settings, environ: Mapping[str, str] = os.environ) -> HistoryStore:
        return cls(
            history_path(environ),
            enabled=cfg.history,
            retention_days=cfg.history_retention_days,
            prices_path=price_table_path(environ),
            extra_patterns=cfg.extra_patterns,
        )

    # -- writing ---------------------------------------------------------------------------

    def record(
        self,
        operation: Mapping[str, object],
        attempts: Sequence[Mapping[str, object]] = (),
        *,
        prune: bool = False,
    ) -> bool:
        """Store one operation and its attempts in one short transaction, together with any
        records waiting in the spool. True when stored (or already stored: a retried write is
        keyed by (operation_id, seq) and never duplicates a row) or when history is off; False
        when not stored now: a write that missed its budget is kept in the spool for a later
        write, and an invalid record (or one the full or unwritable spool cannot take) bumps
        the lost-write marker. Never raises.

        ``prune`` then also deletes up to _PRUNE_BATCH operations older than the retention, in
        a second short transaction after the record committed and only while the budget
        lasts, so a prune that fails or runs out of time never costs the record. The trigger
        recorder passes it; the index on occurred_at_utc makes the check a single seek when
        there is nothing to delete."""
        if not self.enabled:
            return True
        start = time.monotonic()
        extra_time = _CREATE_EXTRA
        with contextlib.suppress(Exception):
            if self.path.stat().st_size >= _NO_SCHEMA_BYTES:
                extra_time = 0.0
        sidecar_deadline = start + _BUDGET + extra_time - _MARGIN
        try:
            extra = compile_extra(self.extra_patterns)
            op_id, op = _operation_row(operation, extra)
            rows = [
                _attempt_row(op_id, seq, at, self._price_table(), extra)
                for seq, at in enumerate(attempts, start=1)
            ]
            cutoff = self.cutoff(timedelta(days=self.retention_days)) if prune else None
        except Exception:
            self._mark_lost(sidecar_deadline)
            return False
        # One lock try: a busy lock leaves the spool for later instead of eating the write budget.
        spooled, pending = self._replayable(extra, time.monotonic())
        try:
            self._write(op, rows, start + _WRITE_BUDGET + extra_time, cutoff, spooled=spooled)
        except Exception:
            self._spool_or_mark(op, rows, sidecar_deadline)
            return False
        if pending:
            self._settle_spool({row[0] for row, _ in spooled}, extra, sidecar_deadline)
        return True

    def replay(self) -> int:
        """Store the records waiting in the spool now, with a management command's time
        (stats, export and doctor call it first). Each is validated again; one that is not
        valid is dropped and counted as lost. Returns how many were stored; never raises."""
        try:
            extra = compile_extra(self.extra_patterns)
            spooled, pending = self._replayable(extra, time.monotonic() + _SERVICE_TIMEOUT)
            if not pending:
                return 0
            if spooled:
                op, rows = spooled[0]
                deadline = time.monotonic() + _SERVICE_TIMEOUT
                self._write(op, rows, deadline, spooled=spooled[1:])
            self._settle_spool(
                {op[0] for op, _ in spooled}, extra, time.monotonic() + _SERVICE_TIMEOUT
            )
        except Exception:
            return 0
        return len(spooled)

    @staticmethod
    def cutoff(age: timedelta) -> str:
        # A longer age would overflow the date arithmetic; nothing is that old anyway.
        age = min(age, timedelta(days=MAX_RETENTION_DAYS))
        return (datetime.now(UTC) - age).strftime(_TIME_FORMAT)

    def _price_table(self) -> PriceTable | None:
        # Read once, on the first attempt to price; a broken table means no estimates.
        if not self._prices_loaded:
            self._prices_loaded = True
            if self.prices_path is not None:
                with contextlib.suppress(Exception):
                    self._prices = load_price_table(self.prices_path)
        return self._prices

    def _write(
        self,
        op: tuple[object, ...],
        rows: list[tuple[object, ...]],
        deadline: float,
        prune_before: str | None = None,
        *,
        spooled: Sequence[_Spooled] = (),
    ) -> None:
        import sqlite3

        def remaining_ms() -> int:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("history write budget spent")
            return max(1, int(left * 1000))

        _make_folder(self.path.parent)
        with self._connect(timeout=remaining_ms() / 1000) as conn:
            # Abort a statement that runs past the budget (checked every _PROGRESS_STEPS VM steps).
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), _PROGRESS_STEPS)
            conn.execute(f"PRAGMA busy_timeout = {remaining_ms()}")
            target = "wal" if wal_is_safe(sqlite3.sqlite_version) else "delete"
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != target:
                # Leaving WAL needs every other connection closed: if one is open (or the
                # file is locked), write in the current mode and let the next write retry.
                # No wait for it, so a busy database does not spend the budget here.
                conn.execute("PRAGMA busy_timeout = 0")
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute(f"PRAGMA journal_mode = {target}")
            conn.execute(f"PRAGMA busy_timeout = {remaining_ms()}")
            conn.execute("BEGIN IMMEDIATE")
            try:
                _migrate(conn)
                remaining_ms()
                records = [*spooled, (op, rows)]
                placeholders = ", ".join("?" * len(OPERATION_COLUMNS))
                conn.executemany(
                    f"INSERT OR IGNORE INTO operations VALUES ({placeholders})",  # noqa: S608
                    [record[0] for record in records],
                )
                placeholders = ", ".join("?" * len(ATTEMPT_COLUMNS))
                conn.executemany(
                    f"INSERT OR IGNORE INTO attempts VALUES ({placeholders})",  # noqa: S608
                    [row for record in records for row in record[1]],
                )
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            # Only after the record is committed, and only with time left.
            if prune_before is not None and deadline - time.monotonic() > _MARGIN:
                _prune_batch(conn, prune_before, deadline)

    @contextlib.contextmanager
    def _connect(self, *, timeout: float, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        import sqlite3

        if readonly:
            conn = sqlite3.connect(
                f"{self.path.absolute().as_uri()}?mode=ro",
                timeout=timeout,
                uri=True,
                isolation_level=None,
            )
        else:
            conn = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = NORMAL")
            yield conn
        finally:
            conn.close()

    def _mark_lost(self, deadline: float | None = None) -> bool:
        """Count one dropped write in the sidecar marker: read, then replace it atomically
        (temp file + os.replace) under a short lock file, so concurrent writers never lose a
        count or leave half a file. A marker that is not valid is counted from 0 again. Gives
        up when the lock is not free by ``deadline``; False when that or anything else fails.
        Never raises."""
        try:
            with self._sidecar_lock(deadline or time.monotonic() + _BUDGET - _WRITE_BUDGET):
                self._count_lost(1)
        except Exception:
            return False
        return True

    def _count_lost(self, dropped: int) -> None:
        """Add ``dropped`` to the marker; the caller holds the sidecar lock."""
        try:
            count = self._read_lost()[0]
        except ValueError:
            count = 0
        payload = json.dumps({"lost_writes": count + dropped, "last_lost_utc": _now()})
        # Not config_files.write_atomic(): its fsync and private-folder chmod do not fit
        # record()'s budget, and relocate moves this `.<pid>.tmp` name as a companion.
        temp = self.lost_path.with_name(f"{LOST_NAME}.{os.getpid()}.tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.lost_path)

    # -- spool -----------------------------------------------------------------------------

    def _load_spool(self) -> list[object]:
        """The spool's entries as written, [] with no spool; OSError when it cannot be read,
        ValueError when it is not a valid spool (too large, not JSON, unknown version)."""
        try:
            with self.spool_path.open("rb") as handle:
                data = handle.read(_SPOOL_BYTES + 1)
        except FileNotFoundError:
            return []
        if len(data) > _SPOOL_BYTES:
            raise ValueError("history spool is too large")
        spool = json.loads(data)
        if not isinstance(spool, dict) or spool.get("version") != _SPOOL_VERSION:
            raise ValueError("not a history spool")
        records = spool.get("records")
        if not isinstance(records, list):
            raise ValueError("not a history spool")
        return records

    def _save_spool(self, records: list[object]) -> None:
        """Replace the spool with ``records`` (temp file + os.replace), or remove it when
        there are none; the caller holds the sidecar lock. ValueError when it would be larger
        than _SPOOL_BYTES."""
        if not records:
            self.spool_path.unlink(missing_ok=True)
            return
        payload = json.dumps({"version": _SPOOL_VERSION, "records": records})
        if len(payload.encode("utf-8")) > _SPOOL_BYTES:
            raise ValueError("history spool is full")
        # Written like the lost marker, not by write_atomic() (see _count_lost()).
        temp = self.spool_path.with_name(f"{SPOOL_NAME}.{os.getpid()}.tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.spool_path)

    def _from_spool(self, entry: object, extra: tuple[re.Pattern[str], ...]) -> _Spooled:
        """One spool entry as database rows, validated again exactly as record() validates a
        new record (shapes, enums, _looks_secret()); InvalidRecord (or another error) when it
        is not valid. Only allowlisted keys are read. An estimate is priced again from the
        current price table."""
        if not isinstance(entry, dict):
            raise InvalidRecord("not a spool entry")
        operation, attempts = entry.get("operation"), entry.get("attempts")
        if not isinstance(operation, dict) or not isinstance(attempts, list):
            raise InvalidRecord("not a spool entry")
        occurred = operation.get("occurred_at_utc")
        if not isinstance(occurred, str):
            raise InvalidRecord("occurred_at_utc must be a time")
        try:
            when = datetime.strptime(occurred, _TIME_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            raise InvalidRecord("occurred_at_utc must be a time") from None
        op = {name: operation.get(name) for name in OPERATION_COLUMNS}
        op_id, op_row = _operation_row({**op, "occurred_at_utc": when}, extra)
        rows = []
        for seq, attempt in enumerate(attempts, start=1):
            if not isinstance(attempt, dict):
                raise InvalidRecord("not a spool entry")
            at = {name: attempt.get(name) for name in ATTEMPT_COLUMNS}
            rows.append(_attempt_row(op_id, seq, at, self._price_table(), extra))
        return op_row, rows

    def _replayable(
        self, extra: tuple[re.Pattern[str], ...], deadline: float
    ) -> tuple[list[_Spooled], bool]:
        """The valid spooled records (at most _SPOOL_LIMIT), and whether the spool has
        anything to settle (a record, an invalid entry, or a file that is not a spool). The
        spool is read under the sidecar lock (an unlocked reader would make another
        process's os.replace fail on Windows, #241); with no spool the lock is not taken. An
        unreadable spool, or a lock not had by ``deadline``, is left for later. Never
        raises."""
        if not self.spool_path.exists():
            return [], False
        try:
            with self._sidecar_lock(deadline):
                entries = self._load_spool()
        except ValueError:
            return [], True
        except Exception:
            return [], False
        records = []
        for entry in entries[:_SPOOL_LIMIT]:
            with contextlib.suppress(Exception):
                records.append(self._from_spool(entry, extra))
        return records, bool(entries)

    def _settle_spool(
        self, stored: set[object], extra: tuple[re.Pattern[str], ...], deadline: float
    ) -> None:
        """After a commit: drop the spooled records now in the database (``stored``, their
        ids) and every entry that is not valid, which counts as lost (a file that is not a
        spool counts once). Read again under the lock, so an entry another process added
        since is kept. Gives up at ``deadline`` (the records then replay again later, which
        INSERT OR IGNORE makes harmless). Never raises."""
        with contextlib.suppress(Exception), self._sidecar_lock(deadline):
            try:
                entries = self._load_spool()
            except ValueError:
                entries, dropped = [], 1
            else:
                dropped = 0
            kept = []
            for entry in entries:
                try:
                    op_id = self._from_spool(entry, extra)[0][0]
                except Exception:
                    dropped += 1
                    continue
                if op_id not in stored:
                    kept.append(entry)
            self._save_spool(kept[:_SPOOL_LIMIT])
            dropped += max(0, len(kept) - _SPOOL_LIMIT)
            if dropped:
                self._count_lost(dropped)

    def _spool_or_mark(
        self, op: tuple[object, ...], rows: list[tuple[object, ...]], deadline: float
    ) -> bool:
        """Keep a record whose write failed in the spool, for a later write to store: only
        the allowlisted columns of the rows record() already validated. When the spool is full
        or cannot be written, count it as lost instead. A spool that is not valid is replaced
        (and counted once). Gives up at ``deadline``. True when spooled; never raises."""
        try:
            with self._sidecar_lock(deadline):
                try:
                    try:
                        entries = self._load_spool()
                    except ValueError:
                        self._count_lost(1)
                        entries = []
                    if len(entries) >= _SPOOL_LIMIT:
                        raise ValueError("history spool is full")
                    # Written like the lost marker, not by write_atomic() (see _count_lost()).
                    entry = {
                        "operation": dict(zip(OPERATION_COLUMNS, op, strict=True)),
                        "attempts": [dict(zip(ATTEMPT_COLUMNS, row, strict=True)) for row in rows],
                    }
                    self._save_spool([*entries, entry])
                except Exception:
                    self._count_lost(1)
                    return False
        except Exception:
            return False
        return True

    @contextlib.contextmanager
    def _sidecar_lock(self, deadline: float) -> Iterator[None]:
        """Hold the sidecar's lock file, which holds a random nonce: only the holder whose
        nonce is in it removes it. One left by a killed process is broken after _STALE_LOCK.
        Tries at least once, however little time is left."""
        lock = self.lost_path.with_name(f"{LOST_NAME}.lock")
        _make_folder(lock.parent)
        nonce = uuid.uuid4().hex
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _break_stale(lock):
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("history sidecar is locked") from None
                time.sleep(min(0.002, max(0.0, deadline - time.monotonic())))
                continue
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(nonce)
            break
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                if lock.read_text(encoding="ascii") == nonce:
                    lock.unlink()

    def _read_lost(self) -> tuple[int, str | None]:
        """(dropped writes, time of the last one), (0, None) with no marker; OSError when
        it cannot be read, ValueError when it is not a valid marker."""
        try:
            text = self.lost_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0, None
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("bad lost-write marker")
        count, last = data.get("lost_writes"), data.get("last_lost_utc")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("bad lost-write marker")
        return count, last if isinstance(last, str) else None

    def _writable(self) -> bool:
        """Whether a write could create or change the files: the nearest existing directory
        takes new files (the database, its WAL, the sidecar) and existing files are not
        read-only. A probe, not a promise: a lock or a full disk still drops a write."""
        try:
            directory = self.path.parent
            while not directory.exists():
                if directory.parent == directory:
                    return False
                directory = directory.parent
            if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
                return False
            return all(
                os.access(path, os.W_OK)
                for path in (self.path, self.lost_path, self.spool_path)
                if path.exists()
            )
        except Exception:
            return False

    # -- services --------------------------------------------------------------------------

    def health(self) -> Health:
        """Path, schema version, row counts, the lost-write marker, the spooled records,
        whether a write could
        succeed, the SQLite library version and the database's journal mode (WAL had a rare
        reset bug before 3.51.3, so older libraries use the rollback journal).
        ``tracking_incomplete`` is set when writes were lost, the marker cannot be read or the
        files cannot be written, so a store whose database and sidecar both fail still shows.
        Spooled records are not lost: they show in ``spooled``.
        Never raises; never creates the database."""
        info: dict[str, Any] = {
            "path": str(self.path),
            "exists": False,
            "schema_version": None,
            "operations": None,
            "attempts": None,
            "lost_writes": 0,
            "last_lost_utc": None,
            "tracking_incomplete": True,
            "writable": False,
            "sqlite_version": "unknown",
        }
        try:
            import sqlite3

            info["sqlite_version"] = sqlite3.sqlite_version
        except Exception:
            info["error"] = "no sqlite3"
        try:
            # Read under the lock, like a write (#241); with neither file there is nothing to lock.
            sidecar = self.lost_path.exists() or self.spool_path.exists()
            with (
                self._sidecar_lock(time.monotonic() + 1.0) if sidecar else contextlib.nullcontext()
            ):
                info["lost_writes"], info["last_lost_utc"] = self._read_lost()
                info["spooled"] = len(self._load_spool())
            sidecar_ok = True
        except Exception:
            sidecar_ok = False
        info["writable"] = self._writable()
        info["tracking_incomplete"] = (
            info["lost_writes"] > 0 or not sidecar_ok or not info["writable"]
        )
        with contextlib.suppress(Exception):
            info["exists"] = self.path.is_file()
        if not info["exists"] or "error" in info:
            return Health(**info)
        try:
            with self._connect(timeout=_SERVICE_TIMEOUT, readonly=True) as conn:
                info["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
                info["journal_mode"] = str(
                    conn.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower()
                if info["schema_version"] >= 1:
                    for table in ("operations", "attempts"):
                        sql = f"SELECT COUNT(*) FROM {table}"  # noqa: S608
                        info[table] = conn.execute(sql).fetchone()[0]
        except Exception as exc:
            info["error"] = _error_kind(exc)
        return Health(**info)

    def stats(self, group_by: str = "trigger") -> list[StatsRow]:
        """Counts, last use, latency percentiles, token sums and costs per group, most
        recently used first. ``trigger`` and ``day`` (UTC) group operations, with their
        latency; ``provider`` and ``model`` (the requested model) group attempts, with theirs,
        so an operation that made no request (persona, a blocked draft) is not in them."""
        from decimal import Decimal

        if group_by not in GROUP_BY:
            raise ValueError(f"group_by must be one of {', '.join(GROUP_BY)}")
        self.replay()
        ops, attempts = self._read_all()
        by_op: dict[str, list[dict[str, Any]]] = {}
        for at in attempts:
            by_op.setdefault(at["operation_id"], []).append(at)
        # Per key: its operations (by id) and its attempts.
        groups: dict[str | None, tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]] = {}
        if group_by in ("trigger", "day"):
            for op_id, op in ops.items():
                key = op["trigger_id"] if group_by == "trigger" else op["occurred_at_utc"][:10]
                group = groups.setdefault(key, ({}, []))
                group[0][op_id] = op
                group[1].extend(by_op.get(op_id, []))
        else:
            column = "provider" if group_by == "provider" else "requested_model"
            for at in attempts:
                group = groups.setdefault(at[column], ({}, []))
                group[0][at["operation_id"]] = ops[at["operation_id"]]
                group[1].append(at)

        rows = []
        for key, (op_map, group_attempts) in groups.items():
            group_ops = list(op_map.values())
            timed = group_ops if group_by in ("trigger", "day") else group_attempts
            latencies = sorted(x["latency_ms"] for x in timed if x["latency_ms"] is not None)
            reported: dict[str, Decimal] = {}
            estimated: dict[str, Decimal] = {}
            for at in group_attempts:
                if at["charged_amount"] is not None:
                    unit = at["charged_unit"]
                    reported[unit] = reported.get(unit, Decimal(0)) + Decimal(at["charged_amount"])
                if at["estimated_amount"] is not None:
                    unit = at["estimated_unit"]
                    estimated[unit] = estimated.get(unit, Decimal(0)) + Decimal(
                        at["estimated_amount"]
                    )
            rows.append(
                StatsRow(
                    key=key,
                    operations=len(group_ops),
                    attempts=len(group_attempts),
                    last_used_utc=max(op["occurred_at_utc"] for op in group_ops),
                    latency_p50_ms=_percentile(latencies, 50),
                    latency_p95_ms=_percentile(latencies, 95),
                    tokens={name: _sum(at[name] for at in group_attempts) for name in TOKEN_FIELDS},
                    reported=reported,
                    estimated=estimated,
                    unknown_cost_attempts=sum(
                        at["cost_state"] == "unknown" for at in group_attempts
                    ),
                    not_applicable_attempts=sum(
                        at["cost_state"] == "not_applicable" for at in group_attempts
                    ),
                )
            )
        return sorted(rows, key=lambda row: row.last_used_utc, reverse=True)

    def export(self, out: IO[str], fmt: Literal["csv", "json"] = "json") -> int:
        """Write every record to ``out``, metadata only: JSON as {"schema_version",
        "operations", "attempts"}, CSV as one row per attempt joined with its operation (an
        operation without attempts gets one row). Money stays exact text with its unit.
        Returns the number of operations."""
        self.replay()
        ops, attempts = self._read_all()
        if fmt == "json":
            json.dump(
                {
                    "schema_version": SCHEMA_VERSION,
                    "operations": list(ops.values()),
                    "attempts": attempts,
                },
                out,
                indent=2,
            )
        elif fmt == "csv":
            import csv

            attempt_columns = [c for c in ATTEMPT_COLUMNS if c != "operation_id"]
            writer = csv.writer(out, lineterminator="\n")
            writer.writerow([*(f"operation_{c}" for c in OPERATION_COLUMNS), *attempt_columns])
            by_op: dict[str, list[dict[str, Any]]] = {}
            for at in attempts:
                by_op.setdefault(at["operation_id"], []).append(at)
            for op_id, op in ops.items():
                op_values = [op[c] for c in OPERATION_COLUMNS]
                for at in by_op.get(op_id) or [{}]:
                    writer.writerow([*op_values, *(at.get(c) for c in attempt_columns)])
        else:
            raise ValueError("fmt must be csv or json")
        return len(ops)

    def prune(self, older_than: timedelta | None = None) -> int:
        """Delete operations (and their attempts) older than ``older_than``, by default the
        retention setting (PROMPT_HISTORY_RETENTION_DAYS). Returns how many were deleted."""
        age = older_than if older_than is not None else timedelta(days=self.retention_days)
        if age < timedelta(0):
            raise ValueError("older_than must not be negative")
        if not self.path.is_file():
            return 0
        cutoff = self.cutoff(age)
        with self._service() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute(
                "DELETE FROM operations WHERE occurred_at_utc < ?", (cutoff,)
            ).rowcount
            conn.execute("COMMIT")
        return int(deleted)

    def reset(self) -> None:
        """Delete every record (spooled ones too), compact the file and clear the lost-write
        marker. Rows are
        deleted rather than the file, so a writer holding it open (or Windows, which cannot
        delete an open file) does not stop it."""
        if self.path.is_file():
            try:
                with self._service() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("DELETE FROM attempts")
                    conn.execute("DELETE FROM operations")
                    conn.execute("COMMIT")
                    conn.execute("VACUUM")
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except UnusableHistory:
                # Corrupt, or written by a newer version: start over with no file. A locked
                # or read-only database raises instead: it may be fine and in use.
                for suffix in ("", "-wal", "-shm"):
                    try:
                        Path(f"{self.path}{suffix}").unlink(missing_ok=True)
                    except OSError as exc:
                        raise HistoryError(f"cannot delete {self.path}{suffix}") from exc
        try:
            with self._sidecar_lock(time.monotonic() + _SERVICE_TIMEOUT):
                self.spool_path.unlink(missing_ok=True)
                self.lost_path.unlink(missing_ok=True)
        except (OSError, TimeoutError) as exc:
            raise HistoryError(f"cannot clear {self.spool_path} or {self.lost_path}") from exc

    @contextlib.contextmanager
    def _service(self) -> Iterator[sqlite3.Connection]:
        """A connection for a service; any database error becomes a HistoryError."""
        import sqlite3

        try:
            with self._connect(timeout=_SERVICE_TIMEOUT) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise UnusableHistory(
                        f"history schema version {version} is newer than this version of "
                        f"promptmend supports ({SCHEMA_VERSION})"
                    )
                if version < SCHEMA_VERSION:
                    conn.execute("BEGIN IMMEDIATE")
                    _migrate(conn)
                    conn.execute("COMMIT")
                yield conn
        except sqlite3.DatabaseError as exc:
            kind = _error_kind(exc)
            error = UnusableHistory if kind == "corrupt" else HistoryError
            raise error(f"history database is {kind}") from exc

    def _read_all(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Every operation (by id, oldest first) and attempt, as dicts of allowlisted columns."""
        if not self.path.is_file():
            return {}, []
        with self._service() as conn:
            sql = f"SELECT {', '.join(OPERATION_COLUMNS)} FROM operations"  # noqa: S608
            ops = {
                row[0]: dict(zip(OPERATION_COLUMNS, row, strict=True))
                for row in conn.execute(sql + " ORDER BY occurred_at_utc, id")
            }
            sql = f"SELECT {', '.join(ATTEMPT_COLUMNS)} FROM attempts"  # noqa: S608
            attempts = [
                dict(zip(ATTEMPT_COLUMNS, row, strict=True))
                for row in conn.execute(sql + " ORDER BY operation_id, seq")
            ]
        return ops, attempts


def _make_folder(folder: Path) -> None:
    # Never re-create the legacy folder relocate moved away (#241): the record is dropped uncounted.
    if folder.name != LEGACY_APP_DIR:
        folder.mkdir(parents=True, exist_ok=True)


def _break_stale(lock: Path) -> bool:
    """Remove ``lock`` if a killed process left it (older than _STALE_LOCK). It is first
    renamed to a unique name, which only one process can do, and checked again there: a
    fresh lock another process took in between is put back instead."""
    try:
        if time.time() - lock.stat().st_mtime <= _STALE_LOCK:
            return False
        grave = lock.with_name(f"{lock.name}.{uuid.uuid4().hex}.stale")
        os.replace(lock, grave)
    except OSError:
        return False
    try:
        if time.time() - grave.stat().st_mtime <= _STALE_LOCK:
            with contextlib.suppress(OSError):
                os.link(grave, lock)
            return False
    finally:
        with contextlib.suppress(OSError):
            grave.unlink()
    return True


def _prune_batch(conn: sqlite3.Connection, cutoff: str, deadline: float) -> None:
    """Delete up to _PRUNE_BATCH operations (their attempts cascade) older than ``cutoff``,
    oldest first, in a short transaction of its own, after the caller's record committed. It
    stops at ``deadline``: SQLite then rolls back the prune (an interrupt rolls back the whole
    transaction, which is why the record is never in it). Never raises."""
    try:
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), _PROGRESS_STEPS)
        conn.execute(f"PRAGMA busy_timeout = {max(1, int((deadline - time.monotonic()) * 1000))}")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM operations WHERE id IN (SELECT id FROM operations "
            "WHERE occurred_at_utc < ? ORDER BY occurred_at_utc LIMIT ?)",
            (cutoff, _PRUNE_BATCH),
        )
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(Exception):
            conn.set_progress_handler(None, 0)
            conn.execute("ROLLBACK")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring the schema to SCHEMA_VERSION, inside the caller's write transaction. A database
    from a newer version is never written."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"history schema {version} is newer than {SCHEMA_VERSION}")
    for step in range(version, SCHEMA_VERSION):
        for statement in _MIGRATIONS[step]:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {step + 1}")


def _error_kind(exc: Exception) -> str:
    """A fixed word for a database error, so nothing from the file reaches a message."""
    text = str(exc).lower()
    if "not a database" in text or "malformed" in text:
        return "corrupt"
    if "locked" in text or "busy" in text:
        return "locked"
    if "readonly" in text or "read-only" in text:
        return "read-only"
    return "unreadable"


def _percentile(values: list[float], pct: int) -> float | None:
    """Nearest-rank percentile of sorted ``values``; None when there are none."""
    if not values:
        return None
    return values[max(0, math.ceil(pct / 100 * len(values)) - 1)]


def _sum(values: Iterator[int | None]) -> int | None:
    """Sum of the known values; None when none is known (missing is never 0)."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None
