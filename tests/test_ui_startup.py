"""Pins what the interface loads before its first paint (#316), in the style of
test_trigger_contract.py: only Home is built at start, so the other tabs' modules, the setup
wizard and what they import (the Try tab's smoke stub and its http.server) wait until they
open. Run in a fresh interpreter, headless, with a stub loader that never returns before the
first frame, so the count is the app's own and does not race the State load."""

from __future__ import annotations

from pathlib import Path

from test_trigger_contract import _loaded, _startup

# The app up to Textual's Ready event (sent once the first frame is displayed), then the Try
# tab opened, to show that what was left out is loaded on first use. The loader blocks until
# then: no State, no doctor run, no Espanso query, nothing read from the network.
_UI_RUN = """
import sys
started = set(sys.modules)
import threading

from promptmend.tui.app import ManageApp

out = sys.argv[1]
release = threading.Event()
seen = {}


def loader(group_by):
    release.wait(30)
    raise RuntimeError("the stub loader reads nothing")


class FirstPaint(ManageApp):
    async def on_ready(self):
        seen["added"] = sorted(set(sys.modules) - started)
        seen["built"] = [tab for tab in ("home", "settings", "try") if self.main.built(tab)]
        await self.main.action_show("try")
        seen["after_try"] = sorted(set(sys.modules) - started)
        release.set()
        self.exit(0)


app = FirstPaint(loader=loader)
app.run(headless=True, size=(110, 36))
import json
from pathlib import Path
seen["code"] = app.return_code
Path(out).write_text(json.dumps(seen), encoding="utf-8")
"""

# Never loaded before the first paint: the Try tab's stub server and the provider client
# (httpx, which a Try run or the Test call needs), the usage history's SQLite (read by the
# State load in its thread, after the paint), the tabs other than Home and the setup wizard.
FORBIDDEN = (
    "http.server",
    "sqlite3",
    "_sqlite3",
    "httpx",
    "promptmend.smoke",
    "promptmend.cli",
    "promptmend.pipeline",
    "promptmend.tui.settings_pane",
    "promptmend.tui.settings_model",
    "promptmend.tui.profiles_pane",
    "promptmend.tui.triggers_pane",
    "promptmend.tui.history_pane",
    "promptmend.tui.diagnostics_pane",
    "promptmend.tui.try_pane",
    "promptmend.tui.modals",
    "promptmend.tui.setup_wizard",
)
# Modules the app adds to a bare interpreter up to its first paint, macOS, Python 3.13,
# 2026-11-16: 470 (Textual is about 160 of them, rich 48, promptmend 36), from 507 before #316
# built every tab and imported the setup wizard at start. Coarse, like the trigger ceilings:
# a new import tree crosses it, ordinary growth does not.
UI_CEILING = 500


def test_first_paint_loads_only_home(tmp_path: Path) -> None:
    _, data = _startup(_UI_RUN, tmp_path)
    assert data["code"] == 0
    assert data["built"] == ["home"]
    added = data["added"]
    assert "promptmend.tui.home_pane" in added
    assert _loaded(added, FORBIDDEN) == []
    assert len(added) <= UI_CEILING, f"the first paint added {len(added)} modules"
    # Opening the Try tab loads its module, and with it the smoke stub, on first use.
    assert {"promptmend.tui.try_pane", "promptmend.smoke"} <= set(data["after_try"])
