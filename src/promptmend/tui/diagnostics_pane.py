"""The Diagnostics tab: the provider routes, the settings layers and every doctor check."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import DataTable, Static

from .. import doctor
from ..commands import common
from ..commands import doctor as doctor_cmd
from ..commands import settings as settings_cmd
from ..factory import routes
from .base import Pane, add_row
from .state import State


def _status(check: doctor.Check) -> Text:
    label, color = doctor_cmd.LABEL[check.status]
    line = Text("[")
    line.append(f"{label:^4}", style=color)
    line.append(f"] {check.id}: {check.message}")
    return line


def _checks(report: doctor.Report, ids: Sequence[str] | None = None) -> Text:
    return Text("\n").join(_status(c) for c in report.checks if ids is None or c.id in ids)


def show_routes(pane: Pane, state: State) -> None:
    """The providers at a glance (read-only, #199 moved it from the old Providers tab): the
    PROMPT_LOCAL_ONLY and PROMPT_PROVIDER policy, and each provider's model, base URL, whether
    the draft leaves this machine and whether its key is set."""
    entries = state.layers.entries
    cfg = state.settings
    policy = (
        "on: only providers that keep the draft on this machine run"
        if cfg.local_only
        else "off: cloud providers run, behind the data-protection gate"
    )
    pane.query_one("#policy", Static).update(
        f"PROMPT_LOCAL_ONLY is {policy}.\nPROMPT_PROVIDER: {cfg.provider} (the default "
        "of a bare `improve`; the triggers name their own provider)."
    )
    table = pane.query_one("#routes", DataTable)
    table.clear()
    for route in routes(cfg):
        where = "leaves this machine" if route.remote else "stays local"
        if route.refused:
            where = "refused (local only)"
        key = "not needed"
        if route.key:
            key = "set" if entries[route.key].value else "not set"
        model, url = entries[route.model_setting], entries[route.url_setting]
        add_row(
            table,
            route.name,
            common.shown_value(route.model_setting, model),
            common.shown_value(route.url_setting, url),
            where,
            key,
        )


class DiagnosticsPane(Pane):
    def compose(self) -> ComposeResult:
        yield Static("", markup=False, id="policy")
        yield DataTable(id="routes", cursor_type="none", zebra_stripes=True)
        yield Static("", markup=False, id="layers")
        yield Static("", markup=False, id="findings")
        yield Static("", markup=False, id="all-checks")
        yield Static("", markup=False, id="store")

    def setup(self) -> None:
        self.query_one("#routes", DataTable).add_columns(
            "Provider", "Model", "Base URL", "The draft", "Key"
        )

    def show(self, state: State) -> None:
        show_routes(self, state)
        layers = state.layers
        self.query_one("#layers", Static).update(
            "\n".join(settings_cmd.layers_lines(layers, raw=False))
        )
        findings = [f"{common.source_label(f.source)}: {f.message}" for f in layers.findings]
        self.query_one("#findings", Static).update(
            "Findings:\n" + "\n".join(f"  {line}" for line in findings)
            if findings
            else "No problems found in the settings files."
        )
        self.query_one("#all-checks", Static).update(_checks(state.report))
        data = {c.id: c.data for c in state.report.checks}
        stored, sqlite = data.get("history", {}), data.get("sqlite", {})
        lost = stored.get("lost_writes") or 0
        last = stored.get("last_lost_utc")
        self.query_one("#store", Static).update(
            f"SQLite {sqlite.get('version') or 'unknown'}, journal "
            f"{sqlite.get('journal_mode') or 'none yet'}; WAL reset bug (before 3.51.3): "
            f"{'yes' if sqlite.get('wal_reset_bug') else 'no'}\n"
            f"Lost history writes: {lost}{f' (last {last})' if last else ''}; tracking "
            f"{'incomplete' if stored.get('tracking_incomplete') else 'complete'}"
        )
