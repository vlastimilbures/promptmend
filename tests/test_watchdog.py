"""improve's hard time limit (#307): whatever blocks (name resolution, a proxy lookup, the
clipboard, a server that never answers or trickles its reply), the run ends within its limit
plus trigger.WATCHDOG_MARGIN with exactly one result on stdout, exit code 0 and empty stderr.

The in-process tests patch trigger._hard_exit, so the watchdog never ends pytest; the
real-process tests run the CLI the way Espanso does, against smoke.stub_server()'s slow modes,
with a 1 s limit to keep the suite fast."""

from __future__ import annotations

import contextlib
import math
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import typer
from typer.testing import CliRunner

from promptmend import factory, pipeline, recorder, smoke, trigger
from promptmend.cli import app
from promptmend.config import Settings
from promptmend.gate import GatedProvider
from promptmend.pipeline import Rewrite
from promptmend.providers import base
from promptmend.providers.base import post_json, timed_out_message
from promptmend.providers.usage import AttemptUsage, Meter, RunEnded, openai_usage
from promptmend.trigger import _Watchdog

if TYPE_CHECKING:
    from conftest import FakeHttp, HistoryRows, StubProvider

runner = CliRunner()

LIMIT = 1.0
MARKER = f"[promptmend: OpenRouter timed out after {LIMIT}s]"
# Process start-up and teardown on a loaded CI runner (windows-latest above all), on top of
# limit + margin. The cases it guards would otherwise run for 30 s or forever.
SLACK = 8.0


class Exits:
    """Stands in for os._exit: records each code and runs ``then`` (to release a block)."""

    def __init__(self, then: threading.Event | None = None) -> None:
        self.codes: list[int] = []
        self.called = threading.Event()
        self._then = then

    def __call__(self, code: int) -> None:
        self.codes.append(code)
        if self._then is not None:
            self._then.set()
        self.called.set()


@pytest.fixture
def exits(monkeypatch: pytest.MonkeyPatch) -> Exits:
    fake = Exits()
    monkeypatch.setattr(trigger, "_hard_exit", fake)
    return fake


def _watchdog(provider: str = "openrouter") -> tuple[_Watchdog, recorder.Recorder]:
    rec = recorder.Recorder("improve", "i")
    watch = _Watchdog(rec, Rewrite())
    watch._marker = f"[promptmend: {timed_out_message('OpenRouter', LIMIT)}]"
    return watch, rec


# --- The lock: exactly one result --------------------------------------------------------


# Once the watchdog has fired, every later step of the main thread ends the run instead.
@pytest.mark.parametrize("step", ["proceed", "claim", "flushed", "delivering"])
def test_steps_after_firing_end_the_run(exits: Exits, step: str) -> None:
    watch, _ = _watchdog()
    watch._fire()
    call = (
        (lambda: watch.delivering("", "the rewrite"))
        if step == "delivering"
        else getattr(watch, step)
    )
    with pytest.raises(typer.Exit):
        call()


# The output is out and flushed, only the history is left: the deadline ends the process
# silently, without a second result or a second history row.
def test_flushed_run_ends_silently(exits: Exits, capfd: pytest.CaptureFixture[str]) -> None:
    watch, rec = _watchdog()
    trigger._emit("the rewrite", watch)
    watch.flushed()
    watch._fire()
    assert capfd.readouterr().out == "the rewrite"
    assert exits.codes == [0]
    assert rec.outcome == recorder.OK


# The run printed first: the watchdog firing afterwards writes nothing and never exits.
def test_emit_first_leaves_the_watchdog_nothing(
    exits: Exits, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _watchdog()
    trigger._emit("the rewrite", watch)
    watch._fire()
    assert capfd.readouterr().out == "the rewrite"
    assert exits.codes == []


# The watchdog fired first: its marker goes straight to fd 1, it exits 0, and the run's own
# output is never printed (the run ends at its _emit()).
def test_watchdog_first_wins_alone(exits: Exits, capfd: pytest.CaptureFixture[str]) -> None:
    watch, rec = _watchdog()
    watch._fire()
    with pytest.raises(typer.Exit) as raised:
        trigger._emit("the rewrite", watch)
    assert raised.value.exit_code == 0
    assert capfd.readouterr().out == MARKER
    assert exits.codes == [0]
    assert rec.outcome == recorder.ERROR_MARKER


# A race of the two prints exactly one result, every round.
def test_race_prints_exactly_one(exits: Exits, capfd: pytest.CaptureFixture[str]) -> None:
    for _ in range(20):
        watch, _ = _watchdog()
        started = threading.Barrier(2)

        def fire(watch: _Watchdog = watch, started: threading.Barrier = started) -> None:
            started.wait()
            watch._fire()

        thread = threading.Thread(target=fire)
        thread.start()
        started.wait()
        # The loser of the race is _emit() when its Exit says so.
        with contextlib.suppress(typer.Exit):
            trigger._emit("the rewrite", watch)
        thread.join()
        assert capfd.readouterr().out in ("the rewrite", MARKER)


# The marker is cleaned like every other output, keeps a sent-despite note, and a newline
# becomes this platform's, as sys.stdout writes it.
def test_marker_is_cleaned_with_note(
    exits: Exits, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    watch, _ = _watchdog()

    class Built(GatedProvider):
        def __init__(self) -> None:
            self.sent_despite = ("email",)

    watch._ran.built = Built()
    watch._marker = "[promptmend: x\x1b[201~ timed out]"
    watch._fire()
    # What sys.stdout writes for "\n" here (see test_trigger_contract.EOL), spelled out.
    eol = b"\r\n" if sys.platform == "win32" else b"\n"
    note = b"[promptmend: sent despite: email]" + eol + eol
    assert capfdbinary.readouterr().out == note + b"[promptmend: x[201~ timed out]"


# A history write that hangs never holds the exit beyond _WATCHDOG_HISTORY.
def test_slow_history_does_not_delay_exit(
    monkeypatch: pytest.MonkeyPatch, exits: Exits, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(trigger, "_WATCHDOG_HISTORY", 0.1)
    watch, rec = _watchdog()
    release = threading.Event()
    monkeypatch.setattr(rec, "finish", release.wait)
    started = time.monotonic()
    watch._fire()
    release.set()
    assert time.monotonic() - started < 2
    assert capfd.readouterr().out == MARKER
    assert exits.codes == [0]


# Exceptions in the marker write or the history never stop the exit.
def test_failures_still_exit(monkeypatch: pytest.MonkeyPatch, exits: Exits) -> None:
    watch, rec = _watchdog()

    def broken(*args: Any) -> None:
        raise OSError("closed")

    monkeypatch.setattr(trigger, "_write_fd", broken)
    monkeypatch.setattr(rec, "timed_out", broken)
    watch._fire()
    assert exits.codes == [0]


# arm() words the marker after the provider's label, a name it does not know generically, and
# never asks threading for a wait past TIMEOUT_MAX.
@pytest.mark.parametrize(
    ("provider", "label"),
    [("openrouter", "OpenRouter"), ("lmstudio", "LM Studio"), ("nonsense", "The call")],
)
def test_arm_words_marker(provider: str, label: str) -> None:
    watch, _ = _watchdog()
    cfg = Settings(timeout=1e300)
    watch.arm(cfg, provider)
    try:
        assert watch._marker == f"[promptmend: {label} timed out after 1e+300s]"
        assert watch._timer is not None
        assert watch._timer.interval == threading.TIMEOUT_MAX
    finally:
        watch.cancel()


def test_cancel_before_arm_is_harmless() -> None:
    watch, _ = _watchdog()
    watch.cancel()


# --- The recorder keeps the attempt in flight ----------------------------------------------


def test_timed_out_keeps_the_attempt_in_flight() -> None:
    rec = recorder.Recorder("improve", "i")
    rec.track(Settings())
    observer = rec.observer
    assert observer is not None
    meter = Meter(observer, "openrouter", "some/model", openai_usage)
    meter.record(
        loopback=False, attempt=1, status=503, error_kind="non_2xx", body=None, latency=0.1
    )
    meter.begin(loopback=False, attempt=2)
    rec.timed_out()
    first, second = rec.attempts
    assert (first.attempt, first.error_kind) == (1, "non_2xx")
    assert (second.attempt, second.status, second.error_kind, second.endpoint) == (
        2,
        None,
        "timeout",
        "remote",
    )
    assert second.cost_state == "unknown"
    rec.timed_out()  # nothing in flight any more: nothing added
    assert len(rec.attempts) == 2
    # An attempt that ends after the marker is not counted again.
    meter.record(loopback=False, attempt=2, status=200, error_kind=None, body={}, latency=3)
    with pytest.raises(RunEnded):  # nor may a new one start
        meter.begin(loopback=False, attempt=3)
    rec.timed_out()
    assert len(rec.attempts) == 2


def test_finished_attempt_is_not_pending() -> None:
    rec = recorder.Recorder("improve")
    rec.track(Settings())
    observer = rec.observer
    assert observer is not None
    meter = Meter(observer, "ollama", "m", openai_usage, local=True)
    meter.begin(loopback=True, attempt=1)
    meter.record(loopback=True, attempt=1, status=200, error_kind=None, body={}, latency=0.1)
    rec.timed_out()
    assert [a.error_kind for a in rec.attempts] == [None]


# A plain callable observer (the Try tab's list.append) is not told an attempt began.
def test_begin_without_begin_method() -> None:
    seen: list[AttemptUsage] = []
    Meter(seen.append, "ollama", "m", openai_usage).begin(loopback=True, attempt=1)
    assert seen == []


# --- In process: a name lookup that never returns ------------------------------------------


def test_blocking_name_lookup_in_process(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    history_rows: HistoryRows,
) -> None:
    release = threading.Event()

    def getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        release.wait(30)
        raise socket.gaierror("released by the test")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    fake = Exits(then=release)
    monkeypatch.setattr(trigger, "_hard_exit", fake)
    # Fires 0.1 s after the limit, with time for history_rows' generous write budget.
    monkeypatch.setattr(trigger, "_WATCHDOG_HISTORY", 5.0)
    monkeypatch.setattr(trigger, "WATCHDOG_MARGIN", 5.1)
    monkeypatch.setenv("OPENROUTER_API_KEY", "-".join(("test", "key")))
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.invalid/api/v1")
    started = time.monotonic()
    result = runner.invoke(
        app,
        [
            "improve",
            "--provider",
            "openrouter",
            "--source",
            "argument",
            "--text",
            "a draft",
            "--timeout",
            "1",
            "--trigger-id",
            "i",
        ],
    )
    assert time.monotonic() - started < 10
    assert result.exit_code == 0
    assert result.stdout == ""  # the run's own marker was never printed
    assert capfd.readouterr().out == "[promptmend: OpenRouter timed out after 1.0s]"
    assert fake.codes == [0]
    (op,) = history_rows("operations")
    assert (op["outcome"], op["trigger_id"], op["profile_id"]) == ("error_marker", "-i-", "default")
    (attempt,) = history_rows("attempts")
    assert (attempt["error_kind"], attempt["status"], attempt["endpoint"]) == (
        "timeout",
        None,
        "remote",
    )


# A run that ends normally cancels its timer: the timer's thread ends without firing.
def test_normal_run_cancels_the_timer(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, exits: Exits
) -> None:
    timers: list[threading.Timer] = []
    arm = _Watchdog.arm

    def recorded(self: _Watchdog, cfg: Settings, provider: str) -> None:
        arm(self, cfg, provider)
        assert self._timer is not None
        timers.append(self._timer)

    monkeypatch.setattr(_Watchdog, "arm", recorded)
    args = ["improve", "--provider", "ollama", "--source", "argument", "--text", "x"]
    result = runner.invoke(app, args)
    assert (result.exit_code, result.stdout) == (0, "improved")
    (timer,) = timers
    timer.join(5)
    assert not timer.is_alive()
    assert timer.finished.is_set()
    assert exits.codes == []


# A watchdog that fired while the draft was being read stops the run before the provider
# call: nothing is sent after the marker.
def test_no_provider_call_after_the_marker(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    capfd: pytest.CaptureFixture[str],
) -> None:
    release = threading.Event()

    def stalled(op: str, *args: str) -> str:
        release.wait(30)
        return "a draft"

    monkeypatch.setattr(pipeline, "_clipboard", stalled)
    fake = Exits(then=release)
    monkeypatch.setattr(trigger, "_hard_exit", fake)
    monkeypatch.setattr(trigger, "WATCHDOG_MARGIN", 0.6)
    result = runner.invoke(app, ["improve", "--provider", "ollama", "--timeout", "0.05"])
    assert (result.exit_code, result.stdout) == (0, "")
    assert capfd.readouterr().out == "[promptmend: Ollama timed out after 0.05s]"
    assert stub_provider.calls == []


# The rewrite came back but its clipboard copy (clipboard output, or --copy) stalls: the
# watchdog says so, pastes the rewrite instead, and records clipboard_failed.
@pytest.mark.parametrize(
    ("args", "env"),
    [([], {"PROMPT_OUTPUT": "clipboard"}), (["--copy"], {})],
    ids=["clipboard-output", "paste-with-copy"],
)
def test_stalled_copy(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    capfdbinary: pytest.CaptureFixture[bytes],
    history_rows: HistoryRows,
    args: list[str],
    env: dict[str, str],
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    release = threading.Event()

    def stalled(op: str, *args: str) -> None:
        assert op == "copy"
        release.wait(30)

    monkeypatch.setattr(pipeline, "_clipboard", stalled)
    fake = Exits(then=release)
    monkeypatch.setattr(trigger, "_hard_exit", fake)
    monkeypatch.setattr(trigger, "_WATCHDOG_HISTORY", 5.0)
    monkeypatch.setattr(trigger, "WATCHDOG_MARGIN", 5.1)
    command = ["improve", "--provider", "ollama", "--source", "argument", "--text", "x"]
    result = runner.invoke(app, [*command, "--timeout", "1", "--trigger-id", "i", *args])
    assert (result.exit_code, result.stdout) == (0, "")
    expected = trigger.CLIPBOARD_STALLED + "improved"
    assert capfdbinary.readouterr().out == expected.replace("\n", os.linesep).encode()
    assert fake.codes == [0]
    (op,) = history_rows("operations")
    assert op["outcome"] == "clipboard_failed"


# entry.main() leaves the time it started an improve run for that run's watchdog, read once;
# other commands leave none.
def test_entry_start_is_taken_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend import entry

    monkeypatch.setattr(sys, "argv", ["promptmend", "improve"])
    monkeypatch.setattr("promptmend.cli.app", lambda: None)
    monkeypatch.setattr(entry, "quiet_trigger", lambda argv: False)
    before = time.monotonic()
    entry.main()
    started = entry.take_start()
    assert started is not None
    assert before <= started <= time.monotonic()
    assert entry.take_start() is None
    monkeypatch.setattr(sys, "argv", ["promptmend", "persona"])
    entry.main()
    assert entry.take_start() is None


# The limit counts from that start: a run that began long ago fires at once.
def test_limit_counts_from_entry_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend import entry

    monkeypatch.setattr(entry, "_started", time.monotonic() - 100)
    watch, _ = _watchdog()
    monkeypatch.setattr(_Watchdog, "_fire", lambda self: None)
    watch.arm(Settings(timeout=1), "ollama")
    try:
        assert watch._timer is not None
        assert watch._timer.interval == 0.0
    finally:
        watch.cancel()


# --- Nothing is sent after the marker ------------------------------------------------------


def _closed_observer() -> tuple[recorder.Recorder, Meter]:
    rec = recorder.Recorder("improve", "i")
    observer = rec.observer
    assert observer is not None  # given even with the history off (track() not called)
    return rec, Meter(observer, "openrouter", "m", openai_usage)


# Once the run is over, an attempt raises RunEnded before its request: none is sent.
def test_closed_observer_stops_the_request(fake_http: FakeHttp) -> None:
    rec, meter = _closed_observer()
    rec.timed_out()
    with pytest.raises(RunEnded):
        post_json("OpenRouter", "https://openrouter.ai/api/v1/x", 5, json={}, meter=meter)
    assert fake_http.requests == []


# ...nor its retry: the run ending during the wait before it (a 503) stops the second request.
def test_closed_observer_stops_the_retry(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp
) -> None:
    rec, meter = _closed_observer()
    fake_http.queue({"status_code": 503})
    monkeypatch.setattr(base, "_sleep", lambda seconds: rec.timed_out())
    with pytest.raises(RunEnded):
        post_json("OpenRouter", "https://openrouter.ai/api/v1/x", 5, json={}, meter=meter)
    assert len(fake_http.requests) == 1


# The watchdog fires while the provider is being built (after proceed()): with the history
# off, the run still sends nothing after the marker.
def test_no_request_after_marker_while_building(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "-".join(("test", "key")))
    fake_http.reply({"choices": [{"message": {"content": "rewrite"}}]})
    release = threading.Event()
    make_provider = factory.make_provider

    def slow_build(*args: Any, **kwargs: Any) -> Any:
        release.wait(30)
        return make_provider(*args, **kwargs)

    monkeypatch.setattr(pipeline, "make_provider", slow_build)
    monkeypatch.setattr(trigger, "_hard_exit", Exits(then=release))
    monkeypatch.setattr(trigger, "WATCHDOG_MARGIN", 0.6)
    args = ["improve", "--provider", "openrouter", "--source", "argument", "--text", "x"]
    result = runner.invoke(app, [*args, "--timeout", "0.05"])
    assert (result.exit_code, result.stdout) == (0, "")
    assert capfd.readouterr().out == "[promptmend: OpenRouter timed out after 0.05s]"
    assert fake_http.requests == []


# --- The final timer: a claimed run that flushed late ----------------------------------------


# Claiming the output of an armed run starts a timer at the hard deadline (limit + margin).
# trigger's clock is held still: each timer turns its deadline into a wait when it starts, so
# on a real clock the time between arm() and claim() would come off the final timer's wait.
def test_claim_starts_the_final_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptmend import entry

    monkeypatch.setattr(_Watchdog, "_fire", lambda self: None)
    monkeypatch.setattr(_Watchdog, "_final_exit", lambda self: None)
    monkeypatch.setattr(entry, "_started", None)
    monkeypatch.setattr(trigger, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    watch, _ = _watchdog()
    watch.arm(Settings(timeout=30), "ollama")
    try:
        watch.claim()
        assert watch._final is not None
        assert watch._timer is not None
        deadline = 30 + trigger.WATCHDOG_MARGIN
        assert watch._timer.interval == pytest.approx(deadline - trigger._WATCHDOG_HISTORY)
        assert watch._final.interval == pytest.approx(deadline)
    finally:
        watch.cancel()
    assert watch._final.finished.is_set()


# Unarmed (the settings did not load), claiming starts nothing.
def test_claim_unarmed_starts_nothing() -> None:
    watch, _ = _watchdog()
    watch.claim()
    assert watch._final is None


# At the deadline a claimed run still printing is left alone; a flushed one ends silently.
def test_final_exit_by_state(exits: Exits, capfd: pytest.CaptureFixture[str]) -> None:
    watch, _ = _watchdog()
    watch.claim()
    watch._final_exit()
    assert exits.codes == []
    watch.flushed()
    watch._final_exit()
    assert exits.codes == [0]
    assert capfd.readouterr().out == ""
    watch._final_exit()  # fired already: nothing more
    assert exits.codes == [0]


# A stand-in exit that raises still releases a main thread waiting on the watchdog.
def test_raising_exit_still_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(code: int) -> None:
        raise RuntimeError("stand-in")

    monkeypatch.setattr(trigger, "_hard_exit", boom)
    watch, _ = _watchdog()
    watch.flushed()
    with pytest.raises(RuntimeError):
        watch._fire()
    assert watch._done.is_set()


# The final timer came while the run was still printing; the run flushes after the deadline:
# it ends at once, silently, so the history write after it is not left unbounded.
def test_flush_after_the_final_timer_ends_at_once(
    exits: Exits, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _watchdog()
    trigger._emit("the rewrite", watch)
    watch._final_exit()  # still printing: left alone
    assert exits.codes == []
    watch.flushed()
    assert exits.codes == [0]
    assert capfd.readouterr().out == "the rewrite"
    watch._final_exit()  # ended already: nothing more
    assert exits.codes == [0]


# A rewrite holding a lone surrogate still reaches the paste after the clipboard-stalled
# marker, the surrogate replaced as sys.stdout replaces it, instead of the whole text vanishing.
def test_stalled_copy_with_lone_surrogate(
    exits: Exits, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    watch, _ = _watchdog()
    watch.delivering("", "a\ud800b")
    watch._fire()
    stalled = trigger.CLIPBOARD_STALLED.replace("\n", os.linesep).encode()
    assert capfdbinary.readouterr().out == stalled + b"a?b"
    assert exits.codes == [0]


# An attempt that began before the watchdog fired and is still blocked before its request (a
# name lookup, say) and is released while the history is written sends it before the marker:
# the marker is written last and the process ends right after it.
def test_attempt_begun_before_the_marker_sends_before_it(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "-".join(("test", "key")))
    fake_http.reply({"choices": [{"message": {"content": "rewrite"}}]})
    events: list[str] = []
    begun, unblock, sent = threading.Event(), threading.Event(), threading.Event()
    exchange = base._exchange

    def blocked(*args: Any, **kwargs: Any) -> Any:
        events.append("begun")  # after Meter.begin(): the observer was still open
        begun.set()
        unblock.wait(30)
        return exchange(*args, **kwargs)

    handler = fake_http.handler

    def handled(request: Any) -> Any:
        events.append("request")
        sent.set()
        return handler(request)

    finish = recorder.Recorder.finish

    def history_write(self: recorder.Recorder) -> None:
        # The watchdog's history write: the blocked attempt goes on now.
        unblock.set()
        sent.wait(10)
        finish(self)

    write_fd = trigger._write_fd

    def marker_out(fd: int, data: bytes) -> None:
        events.append("marker")
        write_fd(fd, data)

    fire = _Watchdog._fire

    def fire_once_begun(self: _Watchdog) -> None:
        begun.wait(10)  # however slow the run is to reach its request
        fire(self)

    class Exit(Exits):
        def __call__(self, code: int) -> None:
            events.append("exit")
            super().__call__(code)

    monkeypatch.setattr(base, "_exchange", blocked)
    monkeypatch.setattr(_Watchdog, "_fire", fire_once_begun)
    monkeypatch.setattr(fake_http, "handler", handled)
    monkeypatch.setattr(recorder.Recorder, "finish", history_write)
    monkeypatch.setattr(trigger, "_write_fd", marker_out)
    monkeypatch.setattr(trigger, "_hard_exit", Exit(then=unblock))
    # A history wait far longer than the test needs: the order comes from the events alone.
    monkeypatch.setattr(trigger, "_WATCHDOG_HISTORY", 10.0)
    monkeypatch.setattr(trigger, "WATCHDOG_MARGIN", 10.1)
    args = ["improve", "--provider", "openrouter", "--source", "argument", "--text", "x"]
    result = runner.invoke(app, [*args, "--timeout", "0.05"])
    assert (result.exit_code, result.stdout) == (0, "")
    assert capfd.readouterr().out == "[promptmend: OpenRouter timed out after 0.05s]"
    assert events == ["begun", "request", "marker", "exit"]


# The watchdog ended the run while the draft was read: proceed()'s Exit ends the run as it is,
# never taken for a failure of the run with a marker of its own.
def test_proceed_exit_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider
) -> None:
    release = threading.Event()
    monkeypatch.setattr(pipeline, "_clipboard", lambda op, *args: release.wait(30) and "a draft")
    monkeypatch.setattr(trigger, "_hard_exit", Exits(then=release))
    monkeypatch.setattr(trigger, "WATCHDOG_MARGIN", 0.6)
    failures: list[Exception] = []

    def failed(exc: Exception) -> str:
        failures.append(exc)
        return ""

    monkeypatch.setattr(trigger, "marker", failed)
    result = runner.invoke(app, ["improve", "--provider", "ollama", "--timeout", "0.05"])
    assert (result.exit_code, result.stdout) == (0, "")
    assert failures == []
    assert stub_provider.calls == []


# With the history off nothing stores a usage body, so none is parsed (the trigger path's
# cost); the transport facts still reach the observer, and the run's stop still holds.
@pytest.mark.parametrize("history", [True, False])
def test_usage_parsed_only_when_recorded(history: bool) -> None:
    rec = recorder.Recorder("improve", "i")
    rec.track(Settings(history=history))
    parsed: list[object] = []

    def parse(body: Any, usage: AttemptUsage) -> AttemptUsage:
        parsed.append(body)
        return usage

    observer = rec.observer
    assert observer is not None
    meter = Meter(observer, "openrouter", "m", parse)
    meter.begin(loopback=False, attempt=1)
    meter.record(loopback=False, attempt=1, status=200, error_kind=None, body={}, latency=0.1)
    assert len(parsed) == (1 if history else 0)
    assert [(a.status, a.error_kind) for a in rec.attempts] == [(200, None)]
    rec.timed_out()
    with pytest.raises(RunEnded):
        meter.begin(loopback=False, attempt=2)


# --- As a real process, the way Espanso runs it --------------------------------------------

_ARGS = [
    "improve",
    "--provider",
    "openrouter",
    "--source",
    "argument",
    "--text",
    "draft",
    "--timeout",
    str(LIMIT),
    "--trigger-id",
    "i",
]
_LOUD = {"PYTHONWARNINGS": "always", "PYTHONDEVMODE": "1"}


def _run(argv: list[str], env: dict[str, str]) -> tuple[subprocess.CompletedProcess[bytes], float]:
    started = time.monotonic()
    proc = subprocess.run(
        argv,
        env={**env, **_LOUD},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    return proc, time.monotonic() - started


def _assert_marker(proc: subprocess.CompletedProcess[bytes], seconds: float) -> None:
    assert (proc.returncode, proc.stderr) == (0, b"")
    assert proc.stdout.decode("utf-8") == MARKER
    assert seconds <= LIMIT + trigger.WATCHDOG_MARGIN + SLACK


_ENTRY = [sys.executable, "-m", "promptmend.entry"]


# Accepts the request and never answers: httpx's read timeout (the provider's own limit) ends
# this one and words the marker, before the watchdog would; it guards that path stays bounded.
def test_never_answering_server() -> None:
    with smoke.stub_server(math.inf) as stub:
        proc, seconds = _run([*_ENTRY, *_ARGS], smoke.stub_env(stub.port, os.environ))
        assert stub.requests == 1
    _assert_marker(proc, seconds)


# A byte every 0.2 s, headers included: no read ever times out and the reply would take
# about a minute, so only the watchdog ends it.
def test_trickling_server() -> None:
    with smoke.stub_server(trickle=0.2) as stub:
        proc, seconds = _run([*_ENTRY, *_ARGS], smoke.stub_env(stub.port, os.environ))
    _assert_marker(proc, seconds)


# A name lookup that never returns (simulated in the child: nothing here resolves a name).
_BLOCKED_LOOKUP = """
import socket
import sys
import threading

socket.getaddrinfo = lambda *args, **kwargs: threading.Event().wait()
sys.argv = ["promptmend", *sys.argv[1:]]
from promptmend.entry import main

main()
"""


def test_name_lookup_that_never_returns() -> None:
    env = {
        **smoke.stub_env(9, os.environ),
        "OPENROUTER_BASE_URL": "https://openrouter.invalid/api/v1",
    }
    proc, seconds = _run([sys.executable, "-c", _BLOCKED_LOOKUP, *_ARGS], env)
    _assert_marker(proc, seconds)


# A reply that lands before the limit prints only the rewrite.
def test_reply_just_under_the_limit() -> None:
    with smoke.stub_server(0.3) as stub:
        proc, _ = _run([*_ENTRY, *_ARGS], smoke.stub_env(stub.port, os.environ))
    assert (proc.returncode, proc.stderr) == (0, b"")
    assert proc.stdout.decode("utf-8") == smoke.REPLY


@pytest.mark.parametrize(
    ("delay", "trickle"), [(-1.0, 0.0), (math.nan, 0.0), (0.0, math.inf), (0.0, -1.0)]
)
def test_stub_server_refuses_bad_waits(delay: float, trickle: float) -> None:
    with pytest.raises(ValueError, match="non-negative"), smoke.stub_server(delay, trickle=trickle):
        pass
