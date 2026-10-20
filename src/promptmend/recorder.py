"""Records each improve and persona run in the usage history (history.py), best effort.

A run is one operation, plus one attempt per HTTP attempt its provider made. The provider's
usage observer only collects the attempts in memory (it runs while Espanso waits, so it must
not do I/O); finish() writes the operation and all of them in one history transaction, after
the output was emitted and flushed. So the usage of a reply that was then rejected (content
validation, the --copy clipboard step) is kept, and nothing about history can change what
was printed, the exit code or whether the provider retried.

With PROMPT_HISTORY=false, or when the settings did not load (the setting is then unknown),
nothing is recorded and history.py, and with it sqlite3, is never imported.
"""

from __future__ import annotations

import contextlib
import dataclasses
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .providers.usage import AttemptUsage, UsageObserver

# The managed Espanso matches pass these as --trigger-id, each as a literal argument. Stored
# as the trigger itself (`i` -> `-i-`), the shape history.py keeps.
TRIGGER_IDS = ("i", "iok", "ip", "if", "il", "ilm", "ic", "io", "p")
# Stored for a real call from the interface's Try tab (a direct call, no match), so stats can
# tell it from a bare `promptmend improve` (#222). Never a --trigger-id value.
TRY_TAB = "-try-"

# Outcomes of a run, as stored. Each failure prints a marker; the outcome says which kind.
OK = "ok"
# A provider or settings error, or a draft that was empty or too long.
ERROR_MARKER = "error_marker"
GATE_BLOCKED = "gate_blocked"
# A 2xx reply whose content was then rejected (malformed, empty, cut off by an error).
VALIDATION_FAILED = "validation_failed"
# The clipboard could not be read, or --copy or clipboard output (PROMPT_OUTPUT) could not
# write it (clipboard output then pastes the rewrite after the marker instead).
CLIPBOARD_FAILED = "clipboard_failed"
# The clipboard held a password-manager item, which was cleared and not sent.
CONCEALED_REFUSED = "concealed_refused"
# Anything else (the marker says "unexpected error").
UNEXPECTED_ERROR = "unexpected_error"


def attribution(trigger_id: str | None) -> tuple[str, str | None]:
    """(origin, stored trigger) for a --trigger-id value. An allowlisted value is that
    managed trigger; any other value is a match that named itself but is not on the list,
    recorded unattributed (managed, no trigger); no option at all is a direct call. Never
    inferred from the other options."""
    if trigger_id is None:
        return "direct", None
    if trigger_id in TRIGGER_IDS:
        return "espanso_managed", f"-{trigger_id}-"
    return "espanso_managed", None


class Recorder:
    """One CLI run's record. Never raises out of any method."""

    def __init__(self, kind: str, trigger_id: str | None = None) -> None:
        self._started = time.monotonic()
        self._occurred = time.time()
        self.kind = kind
        self.origin, self.trigger = attribution(trigger_id)
        self.profile_id: str | None = None
        self.outcome = OK
        self.latency_ms: float | None = None
        self.attempts: list[AttemptUsage] = []
        self._settings: Settings | None = None
        self._done = False

    def track(self, cfg: Settings) -> None:
        """The settings loaded: record this run unless PROMPT_HISTORY is off."""
        self._settings = cfg if cfg.history else None

    @property
    def observer(self) -> UsageObserver | None:
        """For make_provider(observer=...): collects each attempt; None when not recording."""
        return self.attempts.append if self._settings is not None else None

    @property
    def answered(self) -> bool:
        """Whether the last HTTP attempt got a 2xx reply with no error in it, so a failure
        after it was in validating the content."""
        if not self.attempts:
            return False
        last = self.attempts[-1]
        return last.error_kind is None and last.status is not None and 200 <= last.status < 300

    def emitted(self) -> None:
        """The output was printed: latency runs from the start of the run to here."""
        self.latency_ms = round((time.monotonic() - self._started) * 1000, 1)

    def finish(self) -> None:
        """Flush stdout, then write the operation and its attempts (once; history's own time
        budget bounds it). Never raises; a failed write is history's lost-write count."""
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        if self._settings is None or self._done:
            return
        self._done = True
        with contextlib.suppress(Exception):
            from datetime import UTC, datetime

            from . import history

            store = history.HistoryStore.from_settings(self._settings)
            operation = {
                "id": history.new_operation_id(),
                "occurred_at_utc": datetime.fromtimestamp(self._occurred, UTC),
                "origin": self.origin,
                "trigger_id": self.trigger,
                "kind": self.kind,
                "profile_id": self.profile_id,
                "outcome": self.outcome,
                "latency_ms": self.latency_ms,
            }
            attempts = [dataclasses.asdict(usage) for usage in self.attempts]
            store.record(operation, attempts, prune=True)
