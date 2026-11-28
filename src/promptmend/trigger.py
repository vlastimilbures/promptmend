"""The trigger runs' output and time limit, below their Typer commands in cli.py: emit(),
the only way anything a trigger prints reaches Espanso; Watchdog, the hard time limit of
improve (and of persona's settings load) and the one decision of what a run prints; and
run_improve(), the run itself, which reads the draft, calls pipeline.rewrite() and prints the
rewrite or its marker, then records the run.

emit(), flush_output() and run_improve() share this module because each acts on the
watchdog. It sits between cli.py (which imports it) and pipeline.py (which it imports, and
which never imports it back), and keeps the Typer it needs for printing and typer.Exit out of
pipeline.py. Each pipeline name has one lookup site, the pipeline module, at call time
(pipeline.rewrite(), never a name imported into this module), so one patch reaches an improve
run and the Try tab alike: a test patches pipeline.rewrite, pipeline.marker,
pipeline.make_provider, pipeline.is_concealed or pipeline._clipboard. cli.py likewise calls
these through the module (trigger.emit() and the rest), so a patch of trigger.emit reaches
persona too."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import typer

from . import pipeline, recorder
from .config import CLIPBOARD, Settings, check_provider_options, loading_limit
from .factory import PROVIDER_NAMES, timeout_hint
from .providers.base import split_stop_note, timed_out_message
from .providers.registry import PROVIDERS
from .sanitize import clean


def emit(text: str, watch: Watchdog | None = None) -> None:
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
# _WATCHDOG_HISTORY seconds; otherwise the watchdog does. Until the settings have loaded the
# limit is config.loading_limit() (#331). What users and scripts see: docs/commands.md "The
# time limit"; how the watchdog keeps it: CONTRIBUTING.md "The trigger's time limit".
WATCHDOG_MARGIN = 2.0
# Of WATCHDOG_MARGIN, the most the watchdog waits for the usage history before its marker.
_WATCHDOG_HISTORY = 0.5
# Of _WATCHDOG_HISTORY, what the history wait leaves for writing the marker and for a late
# wake (Windows' run up to ~15 ms late), so the marker still lands by the deadline.
_WATCHDOG_HEADROOM = 0.1
# A settings load that took this many seconds or more (a healthy one takes milliseconds) is
# named in the watchdog's timed-out marker, which would otherwise blame the provider alone.
_SLOW_LOAD = 1.0
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

# The watchdog's states. The main thread moves idle -> (reading stdin -> idle ->)
# (delivering ->) claimed -> flushed; the watchdog moves any state but claimed to fired.
_IDLE = "idle"
# The draft is being read from stdin (--source stdin, or a pipe without --source, #407).
_READING_STDIN = "reading stdin"
# A finished rewrite is being copied to the clipboard (clipboard output or --copy).
_DELIVERING = "delivering"
# emit() is printing the run's own output; the watchdog leaves it alone.
_CLAIMED = "claimed"
# The output is out and flushed; only the history is left.
_FLUSHED = "flushed"
_FIRED = "fired"


class Alarm(Protocol):
    """The watchdog's one wake-up: it calls its ``on_wake`` once the clock reaches the time
    last set, on a thread of its own and never from inside set()."""

    def set(self, at: float) -> None:
        """Wake at ``at`` on the watchdog's clock (at once if it has passed), in place of any
        time set before and not yet reached."""

    def cancel(self) -> None:
        """No wake from now on (one already under way still runs)."""


# Makes the watchdog's alarm from its clock and its wake (Watchdog's ``alarms``); raises when
# no thread can be started.
AlarmFactory = Callable[[Callable[[], float], Callable[[], None]], Alarm]


class _ThreadAlarm:
    """The watchdog's alarm in production: one daemon thread that sleeps on a condition until
    the time set, the wait worked out from the clock each time round (clamped to
    0..threading.TIMEOUT_MAX, so an early or spurious return only loops), then calls
    ``on_wake`` and waits for the next set() or cancel().

    Lock order: a caller may hold the watchdog's lock while it calls set() or cancel() (which
    take the condition's lock), and on_wake takes the watchdog's lock; so this thread never
    calls on_wake with the condition's lock held, and set() called from on_wake is safe."""

    def __init__(self, clock: Callable[[], float], on_wake: Callable[[], None]) -> None:
        self._clock, self._on_wake = clock, on_wake
        self._cond = threading.Condition(threading.Lock())
        self._at: float | None = None
        self._cancelled = False
        self._thread = threading.Thread(target=self._run, name="promptmend-watchdog", daemon=True)
        self._thread.start()

    def set(self, at: float) -> None:
        with self._cond:
            self._at = at
            self._cond.notify()

    def cancel(self) -> None:
        with self._cond:
            self._cancelled = True
            self._cond.notify()

    def _due(self) -> bool:
        """Wait until the time set is reached (True, the time then cleared) or the alarm is
        cancelled (False)."""
        with self._cond:
            while not self._cancelled:
                if self._at is None:
                    self._cond.wait()
                    continue
                wait = self._at - self._clock()
                if wait <= 0:
                    self._at = None
                    return True
                self._cond.wait(min(wait, threading.TIMEOUT_MAX))
            return False

    def _run(self) -> None:
        while self._due():
            try:
                self._on_wake()
            except Exception:
                # Reported as an uncaught thread exception is, and the wait goes on: a time
                # set before the failure must still wake.
                threading.excepthook(
                    threading.ExceptHookArgs([*sys.exc_info(), threading.current_thread()])
                )


def _past(limit: float) -> str:
    """The end of a marker for a run that used up its limit before its call."""
    return f"past the {limit}s time limit; nothing was sent"


def _write_fd(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


class Watchdog:
    """improve's hard time limit, and the one decision of what is printed: the run's
    own output (emit()) or the watchdog's marker, exactly one of them.

    The watchdog is one daemon thread (its alarm, _ThreadAlarm), started by arm_loading()
    before the settings load and set to a provisional limit, config.loading_limit() (the
    call's --timeout, else the built-in default), since a stalled file system can block
    Settings.load() too (#331). Firing on it, the watchdog writes only its own marker ("the
    settings did not load within ...s") and no history: whether the history is on is itself a
    setting, and history must not be imported when it is off. arm() moves the deadline to the
    real limit once the call's limit is known, on the same start, so a longer configured limit
    is unaffected; its marker names a load that took _SLOW_LOAD seconds or more (measured from
    arm_loading(), not counting start-up), instead of blaming the provider. A run whose
    start-up and load used up the whole limit ends in arm()'s caller with a marker saying so,
    before its draft is read or sent. persona sets only the provisional limit. arm() and
    claim() start no thread: a process with none left fails in arm_loading(), before the
    load, and each caller prints its own output then (run_improve()'s marker, persona's text).

    Each wake (_wake()) decides from the state and the clock, read under the lock, what is due
    now, so a deadline arm() moved later is data, not a timer to retire: a wake that finds the
    fire time not yet reached goes back to sleep until it. At the fire time, by state:

    - reading stdin (stdin has not been closed: a pipe left open): the same as idle, with
      stdin_marker() in place of the timed-out marker, since nothing was called and a longer
      limit would only wait longer (#407).
    - idle (the run is still reading its input or calling the provider): it writes the
      timed-out marker, the sent-despite note first, cleaned, UTF-8 (a lone surrogate
      replaced, as sys.stdout does), with this platform's newlines as sys.stdout writes them,
      straight to file descriptor 1 (sys.stdout's lock may be held by the stuck main thread,
      which has printed nothing, so nothing is left to flush). History: outcome error_marker,
      the attempt in flight kept as a timeout.
    - delivering (the rewrite came back, its clipboard copy has not returned): CLIPBOARD_STALLED
      and the rewrite, the way a failed copy prints them; outcome clipboard_failed.
    - claimed (emit() is printing): nothing now; the run finishes on its own, and the thread
      sleeps on until the hard deadline.
    - flushed (only the run's history write is left): the process ends at once, silently.
      That write is then cut off: its row is lost without the lost-write count going up, and
      a sidecar lock or temp file it held stays until history_spool._STALE_LOCK lets the next write
      take it over.

    A claimed run that has not flushed by the hard deadline (limit + WATCHDOG_MARGIN) is left
    alone, never printed over (a run blocked writing its own output is never cut off); the
    wake at that deadline ends a flushed run the same silent way, and a run that flushes only
    after that wake passed ends at once in flushed(), so a slow first history write cannot
    keep it running past the deadline. That is the thread's last wake; cancel() (the run's
    end) stops it sooner.

    The fire time is _WATCHDOG_HISTORY seconds before the deadline. Firing first closes the
    run's usage observer, then gives the history at most _WATCHDOG_HISTORY - _WATCHDOG_HEADROOM
    seconds in a thread of its own, and only then writes its marker and ends the process with
    exit code 0 (1 with --fail-on-marker, whose stalled-copy marker counts too; a flushed run
    keeps the code its own output had), so the marker is out by the deadline. That history
    wait is less than the very first write may take (history._BUDGET plus its _CREATE_EXTRA,
    for the file it creates), so such a run's row can be lost; a later write is not affected.

    No request is started after the watchdog steps in: a closed observer makes every later
    attempt, a retry included, raise providers.usage.RunEnded before its request
    (Meter.begin()); proceed() stops the run earlier still, before the provider is built. An
    attempt that began before the observer closed and is still blocked before its request (a
    name lookup, a connect, TLS) may send it during the history wait, or in the few
    milliseconds between the marker and the exit while this thread waits for the GIL; its
    result is ignored. A run whose settings failed to load keeps the provisional watchdog,
    for its marker and history (none: no settings) until it is out.
    """

    def __init__(
        self,
        rec: recorder.Recorder,
        ran: pipeline.Rewrite,
        *,
        fail_on_marker: bool = False,
        clock: Callable[[], float] = time.monotonic,
        alarms: AlarmFactory = _ThreadAlarm,
    ) -> None:
        """``clock`` (monotonic seconds) is every time read of the watchdog and ``alarms``
        makes its one alarm (its thread); a test passes a fake pair, to move time on a thread
        of its choosing. The history's bounded wait in _finish_run() is real time either way."""
        from . import entry

        self._rec, self._ran = rec, ran
        # --fail-on-marker (#407): a run that ended in a failure marker exits 1, however it
        # ends (raise typer.Exit or _hard_exit). The run says so with failed() before its
        # marker goes out; the watchdog's own timed-out marker sets it in _wake().
        self._fail_on_marker = fail_on_marker
        self._failed = False
        self._clock = clock
        self._alarms = alarms
        self._started = entry.take_start() or clock()
        self._lock = threading.Lock()
        self._state = _IDLE
        self._done = threading.Event()
        # Made by the first arm_loading() (or arm()); None: unarmed, nothing wakes.
        self._alarm: Alarm | None = None
        self._deadline = 0.0
        self._marker = ""
        # The call's limit, for stdin_marker(); the provisional one until arm().
        self._limit = 0.0
        self._delivered = ("", "")
        # The wake at the hard deadline came while the run was still printing (see flushed()).
        self._final_missed = False
        # The provisional limit is set (arm_loading()) and not yet replaced (arm()).
        self._loading = False
        # When the settings load began (arm_loading()), so arm() words the load alone, not
        # the start-up before it (imports, a scanned machine).
        self._load_started = self._started

    def arm_loading(self, timeout: str | None) -> None:
        """Before Settings.load(): the provisional limit, from the call's ``timeout`` option
        or the built-in default; nothing about the settings is known yet."""
        limit = self._limit = loading_limit(timeout)
        now = self._load_started = self._clock()
        self._loading = True
        self._deadline = self._started + limit + WATCHDOG_MARGIN
        if now >= self._deadline - _WATCHDOG_HISTORY:
            # Start-up alone ran past the limit: the watchdog fires at once, and not because
            # of the settings, which have not begun to load.
            self._marker = (
                f"[promptmend: starting up took {now - self._started:.1f}s, {_past(limit)}]"
            )
        else:
            self._marker = f"[promptmend: the settings did not load within {limit}s]"
        # The only thread start of a run: a process with no thread left fails here, before
        # the load (each caller turns that into its output, see run_improve() and persona).
        with self._lock:
            self._set_alarm(self._deadline - _WATCHDOG_HISTORY)

    def arm(self, cfg: Settings, provider: str) -> str | None:
        """The call's limit is known: move the deadline from the provisional limit to the real
        one, counted from the same start. Does nothing once the provisional one has fired.

        Returns the run's marker when start-up and the settings load have already used up the
        whole limit: the run then ends with it at once and sends nothing (no call that could
        not finish in time, nothing paid). The watchdog stays set, with that same marker, so
        whichever of the two prints, the text is the same."""
        spec = PROVIDERS.get(provider)
        label = spec.label if spec is not None else _UNKNOWN_LABEL
        now = self._clock()
        took, loading = now - self._started, now - self._load_started
        expired = took >= cfg.timeout
        limit = _past(cfg.timeout)
        if expired and loading >= _SLOW_LOAD:
            text = f"the settings took {loading:.1f}s to load, {took:.1f}s in all, {limit}"
        elif expired:
            text = f"starting up took {took:.1f}s, {limit}"
        elif loading >= _SLOW_LOAD:
            loaded = f", {loading:.1f}s of it loading the settings"
            text = timed_out_message(label, cfg.timeout, timeout_hint(cfg), loaded)
        else:
            # The provider's own timed-out marker has the same text (make_provider()).
            text = timed_out_message(label, cfg.timeout, timeout_hint(cfg))
        with self._lock:
            if self._state == _FIRED:
                return None
            self._loading = False
            self._marker = f"[promptmend: {text}]"
            self._limit = cfg.timeout
            self._deadline = self._started + cfg.timeout + WATCHDOG_MARGIN
            if expired:
                # The run ends at once with its marker: give its history row the same margin
                # a run within its limit has, rather than a deadline already past.
                self._deadline = max(self._deadline, now + WATCHDOG_MARGIN)
            # A wake already under way for the provisional time takes the lock after this
            # and finds the fire time moved on (_wake()).
            self._set_alarm(self._deadline - _WATCHDOG_HISTORY)
        return self._marker if expired else None

    def _set_alarm(self, at: float) -> None:
        """Wake at ``at`` on the clock; the first call makes the alarm (starts its thread).
        Called with the lock held: the lock order is this lock, then the alarm's own."""
        if self._alarm is None:
            self._alarm = self._alarms(self._clock, self._wake)
        self._alarm.set(at)

    def failed(self) -> None:
        """The run's output is a failure marker, not a rewrite (--fail-on-marker)."""
        self._failed = True

    def exit_code(self) -> int:
        """1 for a run that ended in a failure marker under --fail-on-marker, else 0 (the
        Espanso contract)."""
        return 1 if self._fail_on_marker and self._failed else 0

    def cancel(self) -> None:
        """The run is over: no wake from now on (its thread ends)."""
        if self._alarm is not None:
            self._alarm.cancel()

    def _move(self, state: str) -> None:
        """Move the main thread's state on, or, once the watchdog has fired, wait for it to
        end the process (the run's own output must not follow its marker)."""
        with self._lock:
            fired = self._state == _FIRED
            if not fired:
                self._state = state
        if fired:
            self._done.wait()  # returns only where _hard_exit does not end the process
            raise typer.Exit(self.exit_code())

    def reading(self, source: str) -> None:
        """Before read_input(): a stdin ``source`` words the watchdog's marker as a draft that
        never arrived (stdin_marker()) until the next step (proceed(), or the marker's
        claim()) moves the state on."""
        if source == "stdin":
            self._move(_READING_STDIN)

    def stdin_marker(self) -> str:
        """The watchdog's marker for a run still reading stdin: nothing was sent. Worded from
        what is known: read_input() reads to the end, so a pipe that delivered a draft and
        stayed open ends here too."""
        return (
            f"[promptmend: stdin was not closed within {self._limit}s, so nothing was sent: "
            "close stdin after the draft, or pass --source clipboard]"
        )

    def proceed(self) -> None:
        """Before the provider call: a run the watchdog has ended sends nothing."""
        self._move(_IDLE)

    def delivering(self, note: str, rewrite: str) -> None:
        """Before the clipboard copy of a finished rewrite: the sent-despite ``note`` and the
        ``rewrite``, which the watchdog prints around CLIPBOARD_STALLED if the copy stalls."""
        self._delivered = (note, rewrite)
        self._move(_DELIVERING)

    def claim(self) -> None:
        """Take the output for emit(); an armed watchdog then wakes on to the hard deadline,
        for a run that flushed but is still writing its history. Starts nothing: the thread
        was started in arm_loading()."""
        self._move(_CLAIMED)

    def flushed(self) -> None:
        """The output is out and flushed. If the wake at the hard deadline already came and
        went while the run was printing, the process ends at once, silently."""
        self._move(_FLUSHED)
        with self._lock:
            late = self._final_missed and self._state == _FLUSHED
            if late:
                self._state = _FIRED
        if late:
            self._exit()

    def _wake(self) -> None:
        """The alarm's wake, on its thread: what is due now, read under the lock from the
        clock, the deadline and the state. Before the fire time (arm() moved it later): sleep
        on until it. At it, by state (class docstring): fire, end a flushed run, or, for a
        claimed one, sleep on until the hard deadline, where a flushed run ends and one still
        printing is left to flushed(). Once fired: nothing."""
        with self._lock:
            now, was = self._clock(), self._state
            fire_at = self._deadline - _WATCHDOG_HISTORY
            if was == _FIRED:
                return
            if now < fire_at:
                self._set_alarm(fire_at)
                return
            if was == _CLAIMED:
                if now < self._deadline:
                    self._set_alarm(self._deadline)
                else:
                    # Still printing: never cut off or printed over; flushed() ends it later.
                    self._final_missed = True
                return
            # A reply may have just arrived and the main thread be about to claim the output:
            # the marker still wins, and that rewrite is dropped (an accepted race).
            self._state = _FIRED
            loading, text = self._loading, self._marker
            if was == _READING_STDIN:
                text = self.stdin_marker()
            if was != _FLUSHED:
                # Every marker of its own is a failure (a stalled copy's too, #407); a
                # flushed run keeps what it printed.
                self._failed = True
        # Nothing raised here may stop the exit below.
        try:
            if was != _FLUSHED:
                self._finish_run(text, delivering=was == _DELIVERING, loading=loading)
        finally:
            self._exit()

    def _exit(self) -> None:
        try:
            _hard_exit(self.exit_code())
        finally:
            self._done.set()  # reached only where _hard_exit returns or raises (the tests)

    def _finish_run(self, text: str, *, delivering: bool, loading: bool) -> None:
        """Close the run's observer (no attempt may start from here), give the history its
        bounded write, then print the watchdog's marker ``text``; _wake() ends the process
        right after it. The history is written first so its wait falls before the marker:
        after the marker only an attempt already begun can still act, for the few
        milliseconds until this thread holds the GIL again and exits (see the class
        docstring). While ``loading`` the settings there is no history to write."""
        rec = self._rec
        with contextlib.suppress(Exception):
            rec.timed_out()
        if not loading:
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
                text = pipeline.sent_despite_note(self._ran.built) + text
            data = clean(text).replace("\n", os.linesep).encode("utf-8", errors="replace")
            # From here to _hard_exit the main thread can run only while os.write's system
            # call releases the GIL, a few milliseconds: no new request can be started (the
            # observer is closed), but one already in a connect or TLS call may still write
            # its bytes.
            _write_fd(1, data)


@dataclass(frozen=True)
class ImproveOptions:
    """The options of one improve call as cli.improve() receives them, raw: the strings are
    parsed later (Settings.for_call() and the rest), so a bad value is still a marker, reported
    in the same order. --fail-on-marker and --trigger-id are not here: the first is the
    watchdog's, the second the recorder's."""

    provider: str | None
    profile: str | None
    model: str | None
    tier: str
    effort: str | None
    max_tokens: str | None
    timeout: str | None
    source: str | None
    text: str | None
    output: str | None
    copy: bool
    allow_flagged: bool


def run_improve(
    watch: Watchdog, rec: recorder.Recorder, ran: pipeline.Rewrite, options: ImproveOptions
) -> None:
    """One improve run. ``source`` None is chosen by pipeline.choose_source(); the exit code
    is the watchdog's (exit_code()): 0 unless --fail-on-marker and a failure marker."""
    try:
        # A stalled file system can block the load: bounded from the start (#331).
        watch.arm_loading(options.timeout)
        loaded = Settings.load()
        rec.track(loaded)
        provider = options.provider or loaded.provider_for(options.tier)
        cfg = loaded.for_call(
            options.tier,
            provider=provider,
            model=options.model,
            effort=options.effort,
            max_tokens=options.max_tokens,
            timeout=options.timeout,
            output=options.output,
        )
        # From here the run has a hard deadline: reading the clipboard can block too.
        expired = watch.arm(cfg, provider)
        if expired is None:
            # An unknown name is left to make_provider(), which reports it as such.
            if provider in PROVIDER_NAMES:
                check_provider_options(provider, options.tier, options.effort)
            source = pipeline.choose_source(options.source, options.text)
            watch.reading(source)
            raw = pipeline.read_input(source, options.text)
            watch.proceed()
            # The rewrite comes back cleaned, which matters here too: --copy and clipboard
            # output put it on the clipboard.
            result = pipeline.rewrite(
                raw,
                provider,
                cfg,
                options.profile,
                ran,
                allow_flagged=options.allow_flagged,
                observer=rec.observer,
                source=source,
            )
    except typer.Exit:
        # proceed()'s: the watchdog has ended the run, so no marker of its own follows.
        raise
    except Exception as exc:
        # Nothing may traceback or exit nonzero: Espanso cannot surface stderr, so emit a
        # visible bracketed marker instead of a blank expansion (exit 1 only with
        # --fail-on-marker, which no match passes).
        watch.failed()
        emit(pipeline.sent_despite_note(ran.built) + pipeline.marker(exc), watch)
        rec.emitted()
        flush_output(watch)
        rec.profile_id = ran.profile_id
        rec.outcome = pipeline.failure_outcome(exc, rec)
        # After the paste text is out, so history can never change it (see recorder.py).
        rec.finish()
        raise typer.Exit(watch.exit_code()) from None

    if expired is not None:
        # No time left for the call: end here, with nothing sent (#331). Outside the try, as
        # the rewrite's own output is, so a failing write cannot lead to a second emit.
        watch.failed()
        emit(expired, watch)
        rec.emitted()
        flush_output(watch)
        rec.outcome = recorder.ERROR_MARKER
        rec.finish()
        raise typer.Exit(watch.exit_code())
    rec.profile_id = ran.profile_id
    note = pipeline.sent_despite_note(ran.built)
    if cfg.output == CLIPBOARD or options.copy:
        # Before the copy, so a watchdog firing during it says the clipboard stalled, not that
        # the call timed out.
        watch.delivering(note, result)
    if cfg.output == CLIPBOARD:
        printed = pipeline.deliver(result, note, rec)
    else:
        # Like clipboard output, the stop note is printed but not copied.
        printed = (
            note
            + (pipeline.copy_failed(split_stop_note(result)[0], rec) if options.copy else "")
            + result
        )
    if rec.outcome == recorder.CLIPBOARD_FAILED:
        # The copy failed: its marker is a failure for --fail-on-marker, rewrite pasted or not.
        watch.failed()
    emit(printed, watch)
    rec.emitted()
    flush_output(watch)
    rec.finish()
    if watch.exit_code():
        raise typer.Exit(watch.exit_code())


def flush_output(watch: Watchdog) -> None:
    """Flush the output, then tell the watchdog only the history is left."""
    with contextlib.suppress(Exception):
        sys.stdout.flush()
    watch.flushed()
