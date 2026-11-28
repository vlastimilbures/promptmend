"""The Checks tab (id `diagnostics`): the provider routes, the settings layers and every
doctor check, and the Test call under the routes: `improve` on a provider the person picks
against a stub on 127.0.0.1 with a placeholder key (smoke.run(), as setup's Test step), never
a real provider or the real key (#396: moved here from the Settings tab). The interface's
other stub runs are setup's Test step and the Try tab's Dry run; outside it, `doctor
--timing` runs a stub improve too, to time it. It needs only the settings, so it works on the
quick State too."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.widgets import Button, DataTable, Static

from .. import doctor, smoke
from ..commands import common
from ..commands import doctor as doctor_cmd
from ..commands import settings as settings_cmd
from ..factory import PROVIDER_NAMES, routes
from .base import Pane, add_row, button_row
from .modals import Field, FormModal
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
    """The providers at a glance (read-only): the PROMPT_LOCAL_ONLY and PROMPT_PROVIDER
    policy, and each provider's model, base URL, whether the draft leaves this machine and
    whether its key is set."""
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
        yield button_row(("smoke", "Test call (local stub)"))
        yield self.result()
        yield Static("", markup=False, id="layers")
        yield Static("", markup=False, id="findings")
        yield Static("", markup=False, id="all-checks")
        yield Static("", markup=False, id="store")

    def setup(self) -> None:
        self.query_one("#routes", DataTable).add_columns(
            "Provider", "Model", "Base URL", "The draft", "Key"
        )

    def show_update(self, state: State) -> None:
        self.query_one("#all-checks", Static).update(_checks(state.report))

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
        if not state.full:
            # A quick State: the checks and the history store are still being read.
            waiting = (
                "Checking every part (doctor's checks)…"
                if state.failed is None
                else f"Not checked: the checks could not run ({state.failed}); r checks again."
            )
            self.query_one("#all-checks", Static).update(waiting)
            self.query_one("#store", Static).update("")
            return
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

    @on(Button.Pressed, "#smoke")
    def _smoke(self) -> None:
        current = self.state.settings.provider if self.state else PROVIDER_NAMES[0]
        preview = (
            "Runs improve against a stub on 127.0.0.1 with a placeholder key. No provider is "
            "called, and neither your key nor your clipboard is sent."
        )
        fields = [Field("provider", "Provider", PROVIDER_NAMES, value=current)]
        self.app.push_screen(
            FormModal("Test call", fields, submit="Run", preview=preview, focus="provider"),
            self._run_smoke,
        )

    def _run_smoke(self, values: dict[str, str] | None) -> None:
        if values is not None:
            self.report(f"Running improve --provider {values['provider']} against the stub…")
            provider = values["provider"]
            self.background(lambda: self._smoke_now(provider))

    def _smoke_now(self, provider: str) -> None:
        result = smoke.run(provider)
        message = f"{'ok' if result.ok else 'failed'}: {result.message}"
        self.app.call_from_thread(self.report, message, error=not result.ok)
