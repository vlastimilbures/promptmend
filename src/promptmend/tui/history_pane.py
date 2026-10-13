"""The History tab: the usage history's stats, export, prune and reset (`stats`, `history
export|prune|reset`). Metadata only."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.widgets import Button, DataTable, Select, Static

from .. import history
from ..commands import common, usage
from ..console import shown_arg
from . import teach
from .base import Pane, add_row, button_row
from .modals import ConfirmModal, Field, FormModal
from .state import State


def _cost(row: history.StatsRow) -> str:
    badges = []
    if row.reported:
        badges.append(f"[reported] {usage.money(dict(row.reported))}")
    if row.estimated:
        badges.append(f"[estimated] {usage.money(dict(row.estimated))}")
    if row.unknown_cost_attempts:
        badges.append(f"[unknown] {row.unknown_cost_attempts}")
    if row.not_applicable_attempts:
        badges.append(f"[local] {row.not_applicable_attempts}")
    return "; ".join(badges) or "none recorded"


class HistoryPane(Pane):
    def compose(self) -> ComposeResult:
        yield Static(
            " ".join(usage.CAVEATS),
            classes="note",
            markup=False,
        )
        yield Static("", markup=False, id="disclosure")
        yield Select(
            [(f"By {g}", g) for g in history.GROUP_BY],
            value="trigger",
            allow_blank=False,
            id="group-by",
        )
        yield DataTable(id="stats", cursor_type="none", zebra_stripes=True)
        yield Static("", markup=False, id="stats-note")
        yield button_row(
            ("export", "Export"),
            ("prune", "Prune"),
            ("reset", "Reset"),
        )
        yield self.result()

    def setup(self) -> None:
        self.query_one("#stats", DataTable).add_columns(
            "Group",
            "Calls",
            "Requests",
            "Last used, UTC",
            "p50 ms",
            "p95 ms",
            "Tok in",
            "Tok out",
            "Cost",
        )

    def show(self, state: State) -> None:
        self.query_one("#disclosure", Static).update("\n".join(usage.disclosure(state.settings)))
        table = self.query_one("#stats", DataTable)
        table.clear()
        for row in state.stats:
            tokens_in, tokens_out = usage.tokens(row)
            add_row(
                table,
                row.key or "(none)",
                str(row.operations),
                str(row.attempts),
                row.last_used_utc[:16].replace("T", " "),
                usage.ms(row.latency_p50_ms),
                usage.ms(row.latency_p95_ms),
                "-" if tokens_in is None else str(tokens_in),
                "-" if tokens_out is None else str(tokens_out),
                _cost(row),
            )
        note = state.stats_error and f"error: {state.stats_error}"
        if not note and not state.stats:
            note = "No usage recorded yet."
        self.query_one("#stats-note", Static).update(note or "")

    @on(Select.Changed, "#group-by")
    def _group(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value != self.manage.group_by:
            self.manage.group_by = event.value
            self.manage.reload()

    def _store(self) -> history.HistoryStore:
        _, settings = common.load_layers()
        return usage.store(settings)

    @on(Button.Pressed, "#export")
    def _export(self) -> None:
        fields = [
            Field("format", "Format", ("json", "csv")),
            Field("path", "Write to a new file (an existing one is never replaced)"),
        ]
        preview = "Metadata only: no draft, rewrite, persona or key is ever recorded."
        self.app.push_screen(
            FormModal("Export the usage history", fields, submit="Export", preview=preview),
            self._write_export,
        )

    def _write_export(self, values: dict[str, str] | None) -> None:
        if values is None:
            return

        def export() -> str:
            raw = values["path"].strip()
            if not raw:
                raise common.CommandError("enter the file to write")
            common.no_key(raw, "the file")
            path = Path(raw).expanduser()
            fmt: Any = values["format"]
            with path.open("x", encoding="utf-8", newline="") as out:
                count = self._store().export(out, fmt)
            return f"Exported {count} call(s) to {path}"

        fmt, raw = values["format"], values["path"].strip()
        argv = ("history", "export", "--format", fmt, "-o", shown_arg(raw) if raw else "<FILE>")
        self.attempt(export, reload=False, command=teach.equivalent(*argv))

    @on(Button.Pressed, "#prune")
    def _prune(self) -> None:
        self.prune()

    def prune(self, days: str | None = None) -> None:
        """Ask how old (``days`` filled in, else the retention), then confirm (`history
        prune`)."""
        if days is None:
            days = str(self.state.settings.history_retention_days) if self.state else ""
        fields = [Field("days", "Delete the records older than this many days", value=days)]
        self.app.push_screen(
            FormModal("Prune the usage history", fields, submit="Next…"), self._ask_prune
        )

    def _ask_prune(self, values: dict[str, str] | None) -> None:
        if values is None:
            return
        found = usage.AGE.fullmatch(values["days"].strip())
        if found is None:
            self.report("error: enter a number of days, e.g. 30", error=True)
            return
        days = int(found.group(1))
        cutoff = history.HistoryStore.cutoff(timedelta(days=days))[:16].replace("T", " ")

        def prune() -> str:
            deleted = self._store().prune(timedelta(days=days))
            return f"Deleted {deleted} call(s) older than {days} day(s)."

        def done(yes: bool | None) -> None:
            if yes:
                self.attempt(
                    prune, command=teach.equivalent("history", "prune", "--older-than", str(days))
                )

        self.app.push_screen(
            ConfirmModal(
                f"Delete every record older than {days} day(s)?",
                f"Everything recorded before {cutoff} UTC, with its requests, is deleted; this "
                "cannot be undone.",
                confirm="Delete",
            ),
            done,
        )

    @on(Button.Pressed, "#reset")
    def _reset(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Confirm, then delete every usage record (`history reset`)."""
        target = self._store()

        def reset() -> str:
            target.reset()
            return "The usage history is empty."

        def done(yes: bool | None) -> None:
            if yes:
                self.attempt(reset, command=teach.equivalent("history", "reset"))

        self.app.push_screen(
            ConfirmModal(
                f"Delete every usage record in {target.path}?",
                "This also clears the lost-write counter, and cannot be undone.",
                confirm="Delete all",
            ),
            done,
        )
