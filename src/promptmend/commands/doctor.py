"""`doctor [--json]`: the doctor service's report, as text or stable JSON."""

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
) -> None:
    """Check the install, settings, keys (set or not, never the value), Espanso, the deployed
    match files, the launcher, the usage history and the clipboard. Exit 4 when a check fails.
    Safe to paste into a bug report: no key, persona or clipboard text is shown."""
    common.no_key(espanso_dir, "--espanso-dir")
    common.no_key(launcher, "--launcher")
    report = service.run(
        espanso_dir=Path(espanso_dir).expanduser() if espanso_dir else None,
        launcher=launcher,
        clipboard=clipboard,
    )
    if as_json:
        common.json_dump(report.to_json())
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
    if report.status == service.FAIL:
        raise typer.Exit(common.PROBLEMS)
