"""`stats` and `history last / export / prune / reset`, over the usage history store
(history.py). Everything shown is metadata; the store never holds a draft, rewrite, persona or
key. The row helpers (ms(), tokens(), money(), last_cells()) are plain functions the History
tab uses too, so both show the same text."""

from __future__ import annotations

import dataclasses
import re
import sys
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer

from .. import history, settings_service
from ..config import Settings
from ..redaction import looks_like_a_key, safe_repr
from . import common
from .common import guard

# Stated in `stats --help`, the JSON and `stats --verbose` (the plain text names
# --verbose instead).
CAVEATS = (
    "These are local observations on this device, not provider billing.",
    "A call counts once the CLI rendered its output; rendered does not mean pasted.",
    "Triggers with an explicit provider (-i-, -ip-, -if-, -iok-, -il-, -ilm-) ignore "
    "PROMPT_PROVIDER.",
)

stats_app = typer.Typer(name="stats", add_completion=False)
history_app = typer.Typer(
    name="history",
    no_args_is_help=True,
    add_completion=False,
    help="List, export, prune or reset the local usage history (metadata only, this device only).",
)


def store(settings: Settings) -> history.HistoryStore:
    return history.HistoryStore.from_settings(settings)


def summary(settings: Settings) -> str:
    """One line on the history for plain `stats`: on or off, where, and how to read more."""
    path = history.history_path()
    if not settings.history:
        return f"Usage history is off (PROMPT_HISTORY=false); nothing is recorded. File: {path}"
    return (
        f"Usage history: metadata only, on this device ({path}). "
        "`promptmend stats --verbose` says what is recorded and how to switch it off."
    )


# How a call recorded with no trigger is listed: a bare `promptmend improve`, or a match
# that named a trigger id not on the list.
NO_TRIGGER = "no trigger (direct call)"


def disclosure(settings: Settings) -> list[str]:
    """What the history stores, where, and the one command that switches it off (D-HIST-0)."""
    path = history.history_path()
    if not settings.history:
        return [f"Usage history is off (PROMPT_HISTORY=false); nothing is recorded. File: {path}"]
    off = settings_service.history_off(legacy=bool(settings_service.legacy_env()))
    return [
        "Usage history is on (PROMPT_HISTORY=true, the default): metadata only (when a trigger "
        "ran, which trigger, profile, provider and model, the outcome, latency, tokens and the "
        "cost the provider reported), never your draft, clipboard, rewrite, persona or keys.",
        f"It stays on this device, in {path}, for {settings.history_retention_days} days.",
        f"Switch it off: {off}; `promptmend history reset` deletes the records.",
    ]


def money(amounts: dict[str, Decimal]) -> str:
    return ", ".join(f"{amount.normalize():f} {unit}" for unit, amount in sorted(amounts.items()))


def tokens(row: history.StatsRow) -> tuple[int | None, int | None]:
    inputs = [row.tokens.get(n) for n in ("input_uncached", "cache_read", "cache_write")]
    known = [n for n in inputs if n is not None]
    return (sum(known) if known else None), row.tokens.get("output")


def ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


# `history last`: the most runs one call lists, and the default.
LAST_MAX = 1000
LAST_DEFAULT = 10
# The columns of `history last` and of the History tab's "Last runs" table.
LAST_COLUMNS = ("Local time", "Trigger", "Outcome", "Error", "ms", "Provider", "Model")
LAST_LATENCY = LAST_COLUMNS.index("ms")
_STORED_TIME = "%Y-%m-%dT%H:%M:%S.%fZ"
_COUNT = re.compile(r"[0-9]{1,6}")


def local_zone() -> tzinfo | None:
    """The zone `history last` and the History tab show times in: None, this machine's own.
    Tests pin it."""
    return None


def _stand_in(value: object) -> str:
    """A stored value that is not what history.py writes (a database edited by hand or by
    another tool), shown quoted through safe_repr() from its printable characters; `?` when
    it has none."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = "".join(c for c in str(value) if c.isprintable() and c != "�")
    return safe_repr(text) if text.strip() else "?"


def local_time(utc: object, *, short: bool = False) -> str:
    """A stored UTC time (``occurred_at_utc``) in local time, to the second (``short``:
    month-day only). Never raises: a value of another shape is its _stand_in()."""
    try:
        if not isinstance(utc, str):
            raise TypeError("not a time")
        when = datetime.strptime(utc, _STORED_TIME).replace(tzinfo=UTC)
        shape = "%m-%d %H:%M:%S" if short else "%Y-%m-%d %H:%M:%S"
        return when.astimezone(local_zone()).strftime(shape)
    except (ValueError, TypeError, OverflowError, OSError):
        return _stand_in(utc)


# The longest latency (ms, about 11.5 days) and identifier shown as they are; history.py
# keeps a model id to 200 characters and the other identifiers shorter.
_LATENCY_MAX = 1e9
_WORD_MAX = 200


def _latency(value: object) -> str:
    if (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value <= _LATENCY_MAX
    ):
        return ms(value)
    return "-" if value is None else _stand_in(value)


def _word(value: object, missing: str = "-") -> str:
    """A stored identifier as it is, `missing` for none; its _stand_in() when it is not a
    short printable string or looks like a key (a row another tool edited)."""
    if value is None or value == "":
        return missing
    if (
        isinstance(value, str)
        and len(value) <= _WORD_MAX
        and value.isprintable()
        and not looks_like_a_key(value)
    ):
        return value
    return _stand_in(value)


def last_cells(run: history.LastRun, *, compact: bool = False) -> tuple[str, ...]:
    """One run's cells, in LAST_COLUMNS' order; a missing value is `-`. Never raises:
    history.py validates what it writes, but a row edited by another tool shows as a
    stand-in instead of hiding the other runs. ``compact`` (the History tab): the time
    without the year, and `(none)` for a direct call, as the tab's stats table says."""
    return (
        local_time(run.occurred_at_utc, short=compact),
        _word(run.trigger_id, "(none)" if compact else NO_TRIGGER),
        _word(run.outcome),
        _word(run.error_kind),
        _latency(run.latency_ms),
        _word(run.provider),
        _word(run.requested_model),
    )


def last_lines(runs: list[history.LastRun]) -> list[str]:
    """`history last`'s table: a header and a line per run, in columns as wide as the
    widest value shown (the latency right-aligned), the model last and never padded."""
    rows = [LAST_COLUMNS, *(last_cells(run) for run in runs)]
    latency = LAST_LATENCY
    widths = [max(len(row[i]) for row in rows) for i in range(len(LAST_COLUMNS) - 1)]
    lines = []
    for row in rows:
        cells = [
            cell.rjust(width) if i == latency else cell.ljust(width)
            for i, (cell, width) in enumerate(zip(row, widths, strict=False))
        ]
        lines.append("  ".join([*cells, row[-1]]).rstrip())
    return lines


def _row_text(row: history.StatsRow) -> list[str]:
    tokens_in, tokens_out = tokens(row)
    cost = []
    if row.reported:
        cost.append(f"reported {money(dict(row.reported))}")
    if row.estimated:
        cost.append(f"estimated {money(dict(row.estimated))}")
    if row.unknown_cost_attempts:
        cost.append(f"{row.unknown_cost_attempts} attempt(s) with an unknown cost")
    if row.not_applicable_attempts:
        cost.append(f"{row.not_applicable_attempts} local (no cost)")
    return [
        f"{row.key or NO_TRIGGER}: {row.operations} call(s), {row.attempts} request(s), last "
        f"{row.last_used_utc[:19]}Z",
        f"    latency p50 {ms(row.latency_p50_ms)} ms, p95 {ms(row.latency_p95_ms)} ms; tokens "
        f"in {'-' if tokens_in is None else tokens_in}, out "
        f"{'-' if tokens_out is None else tokens_out}",
        f"    cost: {'; '.join(cost) or 'none recorded'}",
    ]


def _row_json(row: history.StatsRow) -> dict[str, Any]:
    data = dataclasses.asdict(row)
    for key in ("reported", "estimated"):
        data[key] = {unit: str(amount) for unit, amount in data[key].items()}
    return data


@stats_app.command(
    "stats",
    short_help="Calls, latency, tokens and costs from the local usage history.",
    help="Calls, latency, tokens and costs from the local usage history.\n\n"
    + "\n\n".join(CAVEATS),
)
@guard
def stats(
    by: str = typer.Option(
        "trigger", "--by", help=f"Group by {', '.join(history.GROUP_BY)}", show_default=True
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text"),
    verbose: bool = typer.Option(
        False, "--verbose", help="Also say what the history records and how to read the numbers"
    ),
) -> None:
    if by not in history.GROUP_BY:
        raise typer.BadParameter(
            f"--by must be one of {', '.join(history.GROUP_BY)}, got {safe_repr(by)}"
        )
    layers, settings = settings_service.load_layers()
    common.show_findings(layers)
    rows = store(settings).stats(by)
    if as_json:
        common.json_dump(
            {
                "schema_version": 1,
                "group_by": by,
                "caveats": list(CAVEATS),
                "history": {
                    "enabled": settings.history,
                    "path": str(history.history_path()),
                    "retention_days": settings.history_retention_days,
                },
                "rows": [_row_json(row) for row in rows],
            }
        )
        return
    if verbose:
        for line in disclosure(settings):
            typer.echo(line)
        typer.echo("")
        for caveat in CAVEATS:
            typer.echo(f"Note: {caveat}")
    else:
        typer.echo(summary(settings))
    typer.echo("")
    if not rows:
        typer.echo("No usage recorded yet.")
        return
    typer.echo(f"By {by}, most recent first:")
    for row in rows:
        for line in _row_text(row):
            typer.echo(line)


@history_app.command("last", short_help="The newest runs, one line each.")
@guard
def history_last(
    count: str = typer.Option(
        str(LAST_DEFAULT), "-n", "--count", help=f"How many runs, 1 to {LAST_MAX}"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text"),
) -> None:
    """The newest runs first, one line each: when (local time), trigger, outcome, error kind,
    latency, provider and model. After a retry, the provider, model and error kind are the
    last request's. --json gives the times in UTC, as stored."""
    found = _COUNT.fullmatch(count.strip())
    n = int(found.group(0)) if found else 0
    if not 1 <= n <= LAST_MAX:
        raise typer.BadParameter(
            f"-n must be a whole number from 1 to {LAST_MAX}, got {safe_repr(count)}"
        )
    layers, settings = settings_service.load_layers()
    common.show_findings(layers)
    runs = store(settings).last(n)
    if as_json:
        common.json_dump(
            {
                "schema_version": 1,
                "time_zone": "UTC",
                "history": {
                    "enabled": settings.history,
                    "path": str(history.history_path()),
                    "retention_days": settings.history_retention_days,
                },
                "runs": [dataclasses.asdict(run) for run in runs],
            }
        )
        return
    if not settings.history:
        typer.echo(summary(settings))
        if runs:
            typer.echo("Recorded while it was on:")
    if not runs:
        typer.echo("No usage recorded yet.")
        return
    for line in last_lines(runs):
        typer.echo(line)


@history_app.command("export")
@guard
def history_export(
    fmt: str = typer.Option("json", "--format", help="json or csv"),
    file: str | None = typer.Option(
        None, "--file", "-o", help="Write to this new file instead of stdout"
    ),
    # The spelling before 0.28.0 (#405), hidden and still accepted; 0.29.0 removes it.
    old_output: str | None = typer.Option(None, "--output", hidden=True),
) -> None:
    """Write every record (metadata only) as JSON or CSV."""
    if fmt not in ("json", "csv"):
        raise typer.BadParameter(f"--format must be json or csv, got {safe_repr(fmt)}")
    common.no_key(file, "--file")
    common.no_key(old_output, "--output")
    if file is not None and old_output is not None and file != old_output:
        raise typer.BadParameter("give the file once, with --file (--output is its old name)")
    output = file if file is not None else old_output
    _, settings = settings_service.load_layers()
    if output is None:
        store(settings).export(sys.stdout, fmt)  # type: ignore[arg-type]
        return
    path = Path(output).expanduser()
    # Exclusive: an existing file is never replaced.
    with path.open("x", encoding="utf-8", newline="") as out:
        count = store(settings).export(out, fmt)  # type: ignore[arg-type]
    typer.echo(f"Exported {count} call(s) to {path}")


AGE = re.compile(r"(\d{1,6})d?")


@history_app.command("prune", short_help="Delete the records older than the given age.")
@guard
def history_prune(
    older_than: str | None = typer.Option(
        None,
        "--older-than",
        help="Days, e.g. 30 or 30d; default: PROMPT_HISTORY_RETENTION_DAYS",
        show_default=False,
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do it without asking"),
) -> None:
    """Delete the records older than the given age (and their requests). An age shorter than
    PROMPT_HISTORY_RETENTION_DAYS asks first (--yes skips)."""
    age = None
    if older_than is not None:
        found = AGE.fullmatch(older_than.strip())
        if found is None:
            raise typer.BadParameter(
                f"--older-than must be a number of days, got {safe_repr(older_than)}"
            )
        age = timedelta(days=int(found.group(1)))
    _, settings = settings_service.load_layers()
    if age is not None and age.days < settings.history_retention_days:
        common.confirm(
            f"Delete every record older than {age.days} day(s), sooner than the "
            f"{settings.history_retention_days}-day retention?",
            yes=yes,
        )
    deleted = store(settings).prune(age)
    days = settings.history_retention_days if age is None else age.days
    typer.echo(f"Deleted {deleted} call(s) older than {days} day(s).")


@history_app.command("reset")
@guard
def history_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do it without asking"),
) -> None:
    """Delete every record and the lost-write counter."""
    _, settings = settings_service.load_layers()
    target = store(settings)
    common.confirm(f"Delete every usage record in {target.path}?", yes=yes)
    target.reset()
    typer.echo("The usage history is empty.")
