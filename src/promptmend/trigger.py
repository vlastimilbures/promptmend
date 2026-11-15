"""The improve trigger's run, below its Typer command in cli.py: _emit(), the only way
anything a trigger prints reaches Espanso; _Watchdog, improve's hard time limit and the
one decision of what a run prints; and _improve(), the run itself, which reads the draft,
calls pipeline.rewrite() and prints the rewrite or its marker, then records the run.

_emit(), _flushed() and _improve() share this module because each acts on the
watchdog. It sits between cli.py (which imports it) and pipeline.py (which it imports, and
which never imports it back), and keeps the Typer it needs for printing and typer.Exit out of
pipeline.py. The pipeline names below are imported into this module, so _improve() looks them
up here: a test patches trigger.marker for an improve run, pipeline.rewrite for the Try tab,
and pipeline.make_provider, pipeline.is_concealed or pipeline._clipboard for both."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable

import typer

from . import recorder
from .config import CLIPBOARD, Settings, check_provider_options
from .factory import PROVIDER_NAMES
from .pipeline import (
    Rewrite,
    _copy_failed,
    _deliver,
    _read_input,
    failure_outcome,
    marker,
    rewrite,
    sent_despite_note,
)
from .providers.base import split_stop_note, timed_out_message
from .providers.registry import PROVIDERS
from .sanitize import clean


def _emit(text: str, watch: _Watchdog | None = None) -> None:
    """The only way anything reaches Espanso: unsafe characters stripped, and no trailing
    newline, since Espanso inserts stdout verbatim. With improve's ``watch``, it first claims
    the output; if the watchdog already has, nothing is printed and the run ends there."""
    if watch is not None:
        watch.claim()
    typer.echo(clean(text), nl=False)


# improve's hard deadline: the process ends within the call's time limit plus this many
# seconds, counted from entry.main() (from improve() when the CLI is started another way),
# with the timed-out marker, whatever blocks: name resolution, a proxy lookup, the clipboard,
# a server trickling its reply. The provider's own timeout (providers/base.py) words the same
# marker and records the attempt itself, but counts from its request: it ends a slow call
# first only when start-up and reading the draft took under WATCHDOG_MARGIN -
# _WATCHDOG_HISTORY seconds; otherwise the watchdog does. Documented in docs/usage.md.
WATCHDOG_MARGIN = 2.0
# Of WATCHDOG_MARGIN, the most the watchdog waits for the usage history before its marker.
_WATCHDOG_HISTORY = 0.5
# Of _WATCHDOG_HISTORY, what the history wait leaves for writing the marker and for a late
# timer (Windows' run up to ~15 ms late), so the marker still lands by the deadline.
_WATCHDOG_HEADROOM = 0.1
# Names the call in the watchdog's marker when the provider name is not one of ours.
_UNKNOWN_LABEL = "The call"
# The watchdog's marker when the copy of a finished rewrite (clipboard output, --copy) has
# not returned by the deadline: the rewrite is pasted after it, as when a copy fails.
CLIPBOARD_STALLED = (
    "[promptmend: the clipboard did not answer in time, so the rewrite may not have been "
    "copied; it is pasted instead]\n\n"
)
# How the watchdog ends the process: os._exit, since the main thread may be stuck where no
# exception reaches it. Replaced in every test (conftest's no_hard_exit), so a watchdog can
# never end pytest.
_hard_exit: Callable[[int], object] = os._exit

# The watchdog's states. The main thread moves idle -> (delivering ->) claimed -> flushed;
# the watchdog moves any state but claimed to fired.
_IDLE = "idle"
# A finished rewrite is being copied to the clipboard (clipboard output or --copy).
_DELIVERING = "delivering"
# _emit() is printing the run's own output; the watchdog leaves it alone.
_CLAIMED = "claimed"
# The output is out and flushed; only the history is left.
_FLUSHED = "flushed"
_FIRED = "fired"


def _start_timer(at: float, action: Callable[[], None]) -> threading.Timer:
    """A daemon timer running ``action`` at monotonic time ``at`` (at once if it has passed).
    Clamped: a wait past TIMEOUT_MAX would fail in the timer's thread instead of waiting."""
    delay = min(max(at - time.monotonic(), 0.0), threading.TIMEOUT_MAX)
    timer = threading.Timer(delay, action)
    timer.daemon = True
    timer.start()
    return timer


def _write_fd(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


class _Watchdog:
    """improve's hard time limit, and the one decision of what is printed: the run's
    own output (_emit()) or the watchdog's marker, exactly one of them.

    arm() starts a daemon timer once the call's limit is known. When it fires, by state:

    - idle (the run is still reading its input or calling the provider): it writes the
      timed-out marker, the sent-despite note first, cleaned, UTF-8 (a lone surrogate
      replaced, as sys.stdout does), with this platform's newlines as sys.stdout writes them,
      straight to file descriptor 1 (sys.stdout's lock may be held by the stuck main thread,
      which has printed nothing, so nothing is left to flush). History: outcome error_marker,
      the attempt in flight kept as a timeout.
    - delivering (the rewrite came back, its clipboard copy has not returned): CLIPBOARD_STALLED
      and the rewrite, the way a failed copy prints them; outcome clipboard_failed.
    - claimed (_emit() is printing): nothing; the run finishes on its own.
    - flushed (only the run's history write is left): the process ends at once, silently.
      That write is then cut off: its row is lost without the lost-write count going up, and
      a sidecar lock or temp file it held stays until history._STALE_LOCK lets the next write
      take it over.

    A claimed run that has not flushed by the hard deadline (limit + WATCHDOG_MARGIN) is left
    alone, never printed over (a run blocked writing its own output is never cut off); a
    final timer at that deadline ends a flushed run the same silent way, and a run that
    flushes only after that timer passed ends at once in flushed(), so a slow first history
    write cannot keep it running past the deadline.

    The timer fires _WATCHDOG_HISTORY seconds before the deadline. It first closes the run's
    usage observer, then gives the history at most _WATCHDOG_HISTORY - _WATCHDOG_HEADROOM
    seconds in a thread of its own, and only then writes its marker and ends the process with
    exit code 0, so the marker is out by the deadline. That history wait is less than the
    very first write may take (history._BUDGET plus its _CREATE_EXTRA, for the file it
    creates), so such a run's row can be lost; a later write is not affected.

    No request is started after the watchdog steps in: a closed observer makes every later
    attempt, a retry included, raise providers.usage.RunEnded before its request
    (Meter.begin()); proceed() stops the run earlier still, before the provider is built. An
    attempt that began before the observer closed and is still blocked before its request (a
    name lookup, a connect, TLS) may send it during the history wait, or in the few
    milliseconds between the marker and the exit while this thread waits for the GIL; its
    result is ignored. A run whose
    settings did not load has no watchdog (no limit is known, and its marker is already on
    its way).
    """

    def __init__(self, rec: recorder.Recorder, ran: Rewrite) -> None:
        from . import entry

        self._rec, self._ran = rec, ran
        self._started = entry.take_start() or time.monotonic()
        self._lock = threading.Lock()
        self._state = _IDLE
        self._done = threading.Event()
        self._timer: threading.Timer | None = None
        self._final: threading.Timer | None = None
        self._deadline = 0.0
        self._marker = ""
        self._delivered = ("", "")
        # The final timer fired while the run was still printing (see flushed()).
        self._final_missed = False

    def arm(self, cfg: Settings, provider: str) -> None:
        spec = PROVIDERS.get(provider)
        label = spec.label if spec is not None else _UNKNOWN_LABEL
        self._marker = f"[promptmend: {timed_out_message(label, cfg.timeout)}]"
        self._deadline = self._started + cfg.timeout + WATCHDOG_MARGIN
        self._timer = _start_timer(self._deadline - _WATCHDOG_HISTORY, self._fire)

    def cancel(self) -> None:
        for timer in (self._timer, self._final):
            if timer is not None:
                timer.cancel()

    def _move(self, state: str) -> None:
        """Move the main thread's state on, or, once the watchdog has fired, wait for it to
        end the process (the run's own output must not follow its marker)."""
        with self._lock:
            fired = self._state == _FIRED
            if not fired:
                self._state = state
        if fired:
            self._done.wait()  # returns only where _hard_exit does not end the process
            raise typer.Exit(0)

    def proceed(self) -> None:
        """Before the provider call: a run the watchdog has ended sends nothing."""
        self._move(_IDLE)

    def delivering(self, note: str, rewrite: str) -> None:
        """Before the clipboard copy of a finished rewrite: the sent-despite ``note`` and the
        ``rewrite``, which the watchdog prints around CLIPBOARD_STALLED if the copy stalls."""
        self._delivered = (note, rewrite)
        self._move(_DELIVERING)

    def claim(self) -> None:
        """Take the output for _emit(); an armed watchdog then keeps a final timer at the hard
        deadline, for a run that flushed but is still writing its history."""
        self._move(_CLAIMED)
        if self._timer is not None and self._final is None:
            self._final = _start_timer(self._deadline, self._final_exit)

    def flushed(self) -> None:
        """The output is out and flushed. If the final timer already came and went while the
        run was printing, the hard deadline has passed: the process ends at once, silently."""
        self._move(_FLUSHED)
        with self._lock:
            late = self._final_missed and self._state == _FLUSHED
            if late:
                self._state = _FIRED
        if late:
            self._exit()

    def _fire(self) -> None:
        with self._lock:
            was = self._state
            if was in (_CLAIMED, _FIRED):
                return
            # A reply may have just arrived and the main thread be about to claim the output:
            # the marker still wins, and that rewrite is dropped (an accepted race).
            self._state = _FIRED
        # Nothing raised here may stop the exit below.
        try:
            if was != _FLUSHED:
                self._finish_run(delivering=was == _DELIVERING)
        finally:
            self._exit()

    def _final_exit(self) -> None:
        with self._lock:
            if self._state != _FLUSHED:
                # Still printing: never cut off or printed over; flushed() ends it later.
                self._final_missed = True
                return
            self._state = _FIRED
        self._exit()

    def _exit(self) -> None:
        try:
            _hard_exit(0)
        finally:
            self._done.set()  # reached only where _hard_exit returns or raises (the tests)

    def _finish_run(self, *, delivering: bool) -> None:
        """Close the run's observer (no attempt may start from here), give the history its
        bounded write, then print the watchdog's marker; _fire() ends the process right after
        it. The history is written first so its wait falls before the marker: after the marker
        only an attempt already begun can still act, for the few milliseconds until this
        thread holds the GIL again and exits (see the class docstring)."""
        rec = self._rec
        with contextlib.suppress(Exception):
            rec.timed_out()
        with contextlib.suppress(Exception):
            rec.profile_id = self._ran.profile_id
            rec.emitted()
            if delivering:
                rec.outcome = recorder.CLIPBOARD_FAILED
            # Bounded: a locked or slow history must not keep the process past its limit.
            writer = threading.Thread(target=rec.finish, daemon=True)
            writer.start()
            writer.join(max(_WATCHDOG_HISTORY - _WATCHDOG_HEADROOM, 0.0))
        with contextlib.suppress(Exception):
            if delivering:
                note, rewrite = self._delivered
                text = note + CLIPBOARD_STALLED + rewrite
            else:
                text = sent_despite_note(self._ran.built) + self._marker
            data = clean(text).replace("\n", os.linesep).encode("utf-8", errors="replace")
            # From here to _hard_exit the main thread can run only while os.write's system
            # call releases the GIL, a few milliseconds: no new request can be started (the
            # observer is closed), but one already in a connect or TLS call may still write
            # its bytes.
            _write_fd(1, data)


def _improve(
    watch: _Watchdog,
    rec: recorder.Recorder,
    ran: Rewrite,
    *,
    provider: str | None,
    profile: str | None,
    model: str | None,
    tier: str,
    effort: str | None,
    max_tokens: str | None,
    timeout: str | None,
    source: str,
    text: str | None,
    output: str | None,
    copy: bool,
    allow_flagged: bool,
) -> None:
    try:
        loaded = Settings.load()
        rec.track(loaded)
        provider = provider or loaded.provider
        cfg = loaded.for_call(
            tier,
            provider=provider,
            model=model,
            effort=effort,
            max_tokens=max_tokens,
            timeout=timeout,
            output=output,
        )
        # From here the run has a hard deadline: reading the clipboard can block too.
        watch.arm(cfg, provider)
        # An unknown name is left to make_provider(), which reports it as such.
        if provider in PROVIDER_NAMES:
            check_provider_options(provider, tier, effort)
        raw = _read_input(source, text)
        watch.proceed()
        # The rewrite comes back cleaned, which matters here too: --copy and clipboard output
        # put it on the clipboard.
        result = rewrite(
            raw, provider, cfg, profile, ran, allow_flagged=allow_flagged, observer=rec.observer
        )
    except typer.Exit:
        # proceed()'s: the watchdog has ended the run, so no marker of its own follows.
        raise
    except Exception as exc:
        # Nothing may traceback or exit nonzero: Espanso cannot surface stderr, so emit a
        # visible bracketed marker instead of a blank expansion.
        _emit(sent_despite_note(ran.built) + marker(exc), watch)
        rec.emitted()
        _flushed(watch)
        rec.profile_id = ran.profile_id
        rec.outcome = failure_outcome(exc, rec)
        # After the paste text is out, so history can never change it (see recorder.py).
        rec.finish()
        raise typer.Exit(0) from None

    rec.profile_id = ran.profile_id
    note = sent_despite_note(ran.built)
    if cfg.output == CLIPBOARD or copy:
        # Before the copy, so a watchdog firing during it says the clipboard stalled, not that
        # the call timed out.
        watch.delivering(note, result)
    if cfg.output == CLIPBOARD:
        _emit(_deliver(result, note, rec), watch)
    else:
        # Like clipboard output, the stop note is printed but not copied.
        printed = note + (_copy_failed(split_stop_note(result)[0], rec) if copy else "") + result
        _emit(printed, watch)
    rec.emitted()
    _flushed(watch)
    rec.finish()


def _flushed(watch: _Watchdog) -> None:
    """Flush the output, then tell the watchdog only the history is left."""
    with contextlib.suppress(Exception):
        sys.stdout.flush()
    watch.flushed()
