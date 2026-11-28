"""`doctor [--json] [--timing [--runs N]]`: the doctor service's report, as text or stable
JSON; --timing adds start-up times measured against the smoke test's stub. The text ends with
the service's verdict line (doctor.verdict()), which --json carries as `verdict`."""

from __future__ import annotations

from pathlib import Path

import typer

from .. import doctor as service
from . import common
from .common import guard

app = typer.Typer(name="doctor", add_completion=False)

LABEL = {
    service.OK: ("ok", "green"),
    service.WARN: ("warn", "yellow"),
    service.FAIL: ("FAIL", "red"),
    service.INFO: ("info", "blue"),
}


@app.command(
    "doctor", short_help="Check the install, settings, keys, Espanso, matches and history."
)
@guard
def doctor(
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON"),
    espanso_dir: str | None = typer.Option(
        None, "--espanso-dir", help="Default: `espanso path config`"
    ),
    launcher: str | None = typer.Option(
        None, "--launcher", help="The CLI path the matches should call; default: this install's"
    ),
    clipboard: bool = typer.Option(
        True, "--clipboard/--no-clipboard", help="Test reading the clipboard (length only)"
    ),
    timing: bool = typer.Option(
        False,
        "--timing",
        help="Also time --version, persona and the -i- trigger through the launcher and the "
        "interpreter, against a local stub (no provider call)",
    ),
    runs: int | None = typer.Option(
        None,
        "--runs",
        min=service.RUNS_RANGE[0],
        max=service.RUNS_RANGE[1],
        help=f"With --timing: warm runs after each cold one (default {service.WARM_RUNS})",
    ),
) -> None:
    """Check the install, settings, keys (set or not, never the value), Espanso, the deployed
    match files, the launcher, the usage history and the clipboard. The last line counts the
    problems and says what to do first. Exit 4 when a check fails.
    Safe to paste into a bug report: no key, persona or clipboard text is shown."""
    common.no_key(espanso_dir, "--espanso-dir")
    common.no_key(launcher, "--launcher")
    if runs is not None and not timing:
        raise typer.BadParameter("--runs needs --timing")
    durations: dict[str, float] | None = {} if timing else None
    report = service.run(
        espanso_dir=Path(espanso_dir).expanduser() if espanso_dir else None,
        launcher=launcher,
        clipboard=clipboard,
        durations=durations,
    )
    warm = service.WARM_RUNS if runs is None else runs
    if as_json:
        data = report.to_json()
        if timing:
            data["timing"] = service.timing(report, durations or {}, warm).to_json()
        common.json_dump(data)
    else:
        for check in report.checks:
            label, color = LABEL[check.status]
            typer.echo(f"[{common.paint(f'{label:^4}', color)}] {check.id}: {check.message}")
            if check.id == "match_files":
                for item in check.data.get("files", []):
                    typer.echo(f"         {item['name']}: {item['state']}")
            if check.id == "config":
                for finding in check.data.get("findings", []):
                    source = common.source_label(finding["source"])
                    typer.echo(f"         {source}: {finding['message']}")
        if timing:
            # The checks are on screen first; the children take a few seconds.
            typer.echo("timing...", err=True)
            _print_timing(service.timing(report, durations or {}, warm))
        # The last line: how many problems, and what to do first (#402).
        typer.echo("")
        typer.echo(service.verdict(report).line)
    if report.status == service.FAIL:
        raise typer.Exit(common.PROBLEMS)


def _cell(ms: float | None) -> str:
    return "-" if ms is None else f"{ms:.0f}"


def _print_timing(timed: service.Timing) -> None:
    """The timing table: ms per run, the first one cold, then the median of the warm ones."""
    typer.echo("")
    typer.echo(
        f"Timing against a local stub (no provider call), ms: the first run, then the median "
        f"of {timed.warm_runs} warm"
    )
    typer.echo(f"  launcher:    {timed.launcher or timed.launcher_note}")
    if timed.drift_note:
        typer.echo(f"  note:        {timed.drift_note}")
    typer.echo(f"  interpreter: {' '.join(timed.interpreter)}")
    width = max(len(r.command) for r in timed.runs)
    typer.echo(f"  {'command':<{width}}  {'via':<11}  {'cold':>6}  {'warm':>6}")
    for run in timed.runs:
        row = f"  {run.command:<{width}}  {run.via:<11}  {_cell(_ms(run.cold)):>6}  "
        row += f"{_cell(_ms(run.warm)):>6}"
        if run.status != service.TIMED_OK:
            row += f"  {run.status}: {run.message}"
        typer.echo(row)
    checks = ", ".join(f"{k} {_cell(_ms(v))}" for k, v in timed.checks.items())
    typer.echo(f"  doctor's checks: {checks}")


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else seconds * 1000
