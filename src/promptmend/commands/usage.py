"""`stats` and `history export / prune / reset`, over the usage history store (history.py,
#88). Everything shown is metadata; the store never holds a draft, rewrite, persona or key."""

from __future__ import annotations

import dataclasses
import re
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer

from .. import history
from ..config import Settings
from ..redaction import safe_repr
from . import common
from .common import guard

# Stated in `stats --help`, the JSON and `stats --verbose` (#92; the plain text names
# --verbose instead, #222).
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
    help="Export, prune or reset the local usage history (metadata only, this device only).",
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
    return [
        "Usage history is on (PROMPT_HISTORY=true, the default): metadata only (when a trigger "
        "ran, which trigger, profile, provider and model, the outcome, latency, tokens and the "
        "cost the provider reported), never your draft, clipboard, rewrite, persona or keys.",
        f"It stays on this device, in {path}, for {settings.history_retention_days} days.",
        "Switch it off: `promptmend config set PROMPT_HISTORY false` (or "
        "PROMPT_HISTORY=false in your .env); `promptmend history reset` deletes the records.",
    ]


def _money(amounts: dict[str, Decimal]) -> str:
    return ", ".join(f"{amount.normalize():f} {unit}" for unit, amount in sorted(amounts.items()))


def _tokens(row: history.StatsRow) -> tuple[int | None, int | None]:
    inputs = [row.tokens.get(n) for n in ("input_uncached", "cache_read", "cache_write")]
    known = [n for n in inputs if n is not None]
    return (sum(known) if known else None), row.tokens.get("output")


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def _row_text(row: history.StatsRow) -> list[str]:
    tokens_in, tokens_out = _tokens(row)
    cost = []
    if row.reported:
        cost.append(f"reported {_money(dict(row.reported))}")
    if row.estimated:
        cost.append(f"estimated {_money(dict(row.estimated))}")
    if row.unknown_cost_attempts:
        cost.append(f"{row.unknown_cost_attempts} attempt(s) with an unknown cost")
    if row.not_applicable_attempts:
        cost.append(f"{row.not_applicable_attempts} local (no cost)")
    return [
        f"{row.key or NO_TRIGGER}: {row.operations} call(s), {row.attempts} request(s), last "
        f"{row.last_used_utc[:19]}Z",
        f"    latency p50 {_ms(row.latency_p50_ms)} ms, p95 {_ms(row.latency_p95_ms)} ms; tokens "
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
    layers, settings = common.load_layers()
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


@history_app.command("export")
@guard
def history_export(
    fmt: str = typer.Option("json", "--format", help="json or csv"),
    output: str | None = typer.Option(
        None, "--output", "-o", help="Write to this new file instead of stdout"
    ),
) -> None:
    """Write every record (metadata only) as JSON or CSV."""
    if fmt not in ("json", "csv"):
        raise typer.BadParameter(f"--format must be json or csv, got {safe_repr(fmt)}")
    common.no_key(output, "--output")
    _, settings = common.load_layers()
    if output is None:
        store(settings).export(sys.stdout, fmt)  # type: ignore[arg-type]
        return
    path = Path(output).expanduser()
    # Exclusive: an existing file is never replaced.
    with path.open("x", encoding="utf-8", newline="") as out:
        count = store(settings).export(out, fmt)  # type: ignore[arg-type]
    typer.echo(f"Exported {count} call(s) to {path}")


_AGE = re.compile(r"(\d{1,6})d?")


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
        found = _AGE.fullmatch(older_than.strip())
        if found is None:
            raise typer.BadParameter(
                f"--older-than must be a number of days, got {safe_repr(older_than)}"
            )
        age = timedelta(days=int(found.group(1)))
    _, settings = common.load_layers()
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
    _, settings = common.load_layers()
    target = store(settings)
    common.confirm(f"Delete every usage record in {target.path}?", yes=yes)
    target.reset()
    typer.echo("The usage history is empty.")
