"""improve's hard time limit (#307): whatever blocks (name resolution, a proxy lookup, the
clipboard, a server that never answers or trickles its reply), the run ends within its limit
plus trigger.WATCHDOG_MARGIN with exactly one result on stdout, exit code 0 and empty stderr.

The in-process tests patch trigger._hard_exit, so the watchdog never ends pytest, and run the
watchdog on conftest's FakeClock: its alarm wakes when the test moves the clock on, on the
test's thread (or one it starts), never on the wall clock. Only the history's bounded wait is
real time. The real-process tests run the CLI the way Espanso does, with its real thread,
against smoke.stub_server()'s slow modes, with a 1 s limit to keep the suite fast; the real
alarm (trigger._ThreadAlarm) has a few tests of its own, with tiny real waits."""

from __future__ import annotations

import contextlib
import math
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest
import typer
from conftest import FakeClock, resolved_settings
from typer.testing import CliRunner

from promptmend import config, factory, pipeline, recorder, smoke, trigger
from promptmend.cli import app
from promptmend.config import Settings, loading_limit
from promptmend.gate import GatedProvider
from promptmend.pipeline import Rewrite
from promptmend.providers import base
from promptmend.providers.base import post_json, timed_out_message
from promptmend.providers.usage import (
    AttemptList,
    AttemptUsage,
    Meter,
    RunEnded,
    openai_usage,
)
from promptmend.trigger import Watchdog

if TYPE_CHECKING:
    from conftest import FakeHttp, HistoryRows, StubProvider

runner = CliRunner()

LIMIT = 1.0
# The next step a timed-out marker ends with: a call that passed --timeout (as these do) is
# told to pass a longer one; one on the configured limit, to raise that setting (#398).
FLAG_HINT = "try again with a longer --timeout"
SETTING_HINT = "try again, or raise PROMPT_TIMEOUT_SECONDS"
MARKER = f"[promptmend: OpenRouter timed out after {LIMIT}s: {FLAG_HINT}]"
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


# Far past any deadline here: advancing the fake clock by this much wakes every alarm due.
LATER = 1000.0


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """The fake clock and alarm of every watchdog the test builds: _watchdog(clock)'s, and the
    CLI's, through a trigger.Watchdog that passes them."""
    fake = FakeClock()

    class OnTheFakeClock(Watchdog):
        def __init__(
            self, rec: recorder.Recorder, ran: Rewrite, *, fail_on_marker: bool = False
        ) -> None:
            super().__init__(rec, ran, fail_on_marker=fail_on_marker, clock=fake, alarms=fake.alarm)

    monkeypatch.setattr(trigger, "Watchdog", OnTheFakeClock)
    return fake


def _watchdog(clock: FakeClock | None = None) -> tuple[Watchdog, recorder.Recorder]:
    """An unarmed watchdog on ``clock`` (a fresh fake one by default), its marker the call's:
    no alarm, so nothing wakes until the test arms it."""
    fake = clock if clock is not None else FakeClock()
    rec = recorder.Recorder("improve", "i")
    watch = Watchdog(rec, Rewrite(), clock=fake, alarms=fake.alarm)
    watch._marker = f"[promptmend: {timed_out_message('OpenRouter', LIMIT, FLAG_HINT)}]"
    return watch, rec


def _armed(clock: FakeClock) -> tuple[Watchdog, recorder.Recorder]:
    """A watchdog armed on ``clock`` (from FakeClock.START) with the call's --timeout LIMIT,
    its marker MARKER: it fires once the clock reaches _fire_time()."""
    watch, rec = _watchdog(clock)
    cfg = Settings().for_call("standard", provider="openrouter", timeout=str(LIMIT))
    assert watch.arm(cfg, "openrouter") is None
    assert watch._marker == MARKER
    return watch, rec


def _fire_time() -> float:
    """When an _armed() watchdog fires (_WATCHDOG_HISTORY before its hard deadline)."""
    return _deadline() - trigger._WATCHDOG_HISTORY


def _deadline() -> float:
    """An _armed() watchdog's hard deadline: its limit plus the margin."""
    return FakeClock.START + LIMIT + trigger.WATCHDOG_MARGIN


# --- The lock: exactly one result --------------------------------------------------------


# Once the watchdog has fired, every later step of the main thread ends the run instead.
@pytest.mark.parametrize("step", ["proceed", "claim", "flushed", "delivering"])
def test_steps_after_firing_end_the_run(exits: Exits, clock: FakeClock, step: str) -> None:
    watch, _ = _armed(clock)
    clock.advance(LATER)
    call = (
        (lambda: watch.delivering("", "the rewrite"))
        if step == "delivering"
        else getattr(watch, step)
    )
    with pytest.raises(typer.Exit):
        call()


# The output is out and flushed, only the history is left: the fire time (not the hard
# deadline after it) ends the process silently, without a second result or history row.
def test_flushed_run_ends_silently(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, rec = _armed(clock)
    trigger.emit("the rewrite", watch)
    watch.flushed()
    clock.advance_to(_fire_time() - 0.01)
    assert exits.codes == []
    clock.advance_to(_fire_time())
    assert capfd.readouterr().out == "the rewrite"
    assert exits.codes == [0]
    assert rec.outcome == recorder.OK


# The run printed first: the watchdog firing afterwards writes nothing and never exits.
def test_emit_first_leaves_the_watchdog_nothing(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _armed(clock)
    trigger.emit("the rewrite", watch)
    clock.advance(LATER)  # the fire time and the hard deadline: the run is still printing
    assert capfd.readouterr().out == "the rewrite"
    assert exits.codes == []


# The watchdog fired first: its marker goes straight to fd 1, it exits 0, and the run's own
# output is never printed (the run ends at its emit()).
def test_watchdog_first_wins_alone(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, rec = _armed(clock)
    clock.advance(LATER)
    with pytest.raises(typer.Exit) as raised:
        trigger.emit("the rewrite", watch)
    assert raised.value.exit_code == 0
    assert capfd.readouterr().out == MARKER
    assert exits.codes == [0]
    assert rec.outcome == recorder.ERROR_MARKER


# A race of the two prints exactly one result, every round.
def test_race_prints_exactly_one(exits: Exits, capfd: pytest.CaptureFixture[str]) -> None:
    for _ in range(20):
        clock = FakeClock()
        watch, _ = _armed(clock)
        started = threading.Barrier(2, timeout=10)

        def fire(clock: FakeClock = clock, started: threading.Barrier = started) -> None:
            started.wait()
            clock.advance(LATER)  # the alarm wakes on this thread, as on its own

        thread = threading.Thread(target=fire)
        thread.start()
        started.wait()
        # The loser of the race is emit() when its Exit says so.
        with contextlib.suppress(typer.Exit):
            trigger.emit("the rewrite", watch)
        thread.join(10)
        assert not thread.is_alive()
        assert capfd.readouterr().out in ("the rewrite", MARKER)


# The marker is cleaned like every other output, keeps a sent-despite note, and a newline
# becomes this platform's, as sys.stdout writes it.
def test_marker_is_cleaned_with_note(
    exits: Exits, clock: FakeClock, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    watch, _ = _armed(clock)

    class Built(GatedProvider):
        def __init__(self) -> None:
            self.sent_despite = ("email",)

    watch._ran.built = Built()
    watch._marker = "[promptmend: x\x1b[201~ timed out]"
    clock.advance(LATER)
    # What sys.stdout writes for "\n" here (see test_trigger_contract.EOL), spelled out.
    eol = b"\r\n" if sys.platform == "win32" else b"\n"
    note = b"[promptmend: sent despite: an email address]" + eol + eol
    assert capfdbinary.readouterr().out == note + b"[promptmend: x[201~ timed out]"


# A history write that hangs never holds the exit beyond _WATCHDOG_HISTORY.
def test_slow_history_does_not_delay_exit(
    monkeypatch: pytest.MonkeyPatch,
    exits: Exits,
    clock: FakeClock,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(trigger, "_WATCHDOG_HISTORY", 0.1)
    _, rec = _armed(clock)
    release = threading.Event()
    monkeypatch.setattr(rec, "finish", release.wait)
    started = time.monotonic()
    clock.advance(LATER)
    release.set()
    assert time.monotonic() - started < 2
    assert capfd.readouterr().out == MARKER
    assert exits.codes == [0]


# Exceptions in the marker write or the history never stop the exit.
def test_failures_still_exit(
    monkeypatch: pytest.MonkeyPatch, exits: Exits, clock: FakeClock
) -> None:
    _, rec = _armed(clock)

    def broken(*args: Any) -> None:
        raise OSError("closed")

    monkeypatch.setattr(trigger, "_write_fd", broken)
    monkeypatch.setattr(rec, "timed_out", broken)
    clock.advance(LATER)
    assert exits.codes == [0]


# The provider's own timeout and the watchdog print the same marker for the same call, its
# next step included: the setting of the call's tier, or --timeout when the call passed it.
@pytest.mark.parametrize(
    ("provider", "tier", "timeout", "hint"),
    [
        ("ollama", "standard", None, SETTING_HINT),
        ("openrouter", "standard", None, SETTING_HINT),
        ("openrouter", "pro", None, "try again, or raise PROMPT_PRO_TIMEOUT_SECONDS"),
        ("anthropic", "pro", None, "try again, or raise PROMPT_PRO_TIMEOUT_SECONDS"),
        ("openrouter", "pro", "default", "try again, or raise PROMPT_PRO_TIMEOUT_SECONDS"),
        ("openrouter", "pro", "7", FLAG_HINT),
        ("ollama", "standard", "7", FLAG_HINT),
    ],
)
def test_provider_and_watchdog_word_the_same_timeout(
    monkeypatch: pytest.MonkeyPatch,
    fake_http: FakeHttp,
    provider: str,
    tier: str,
    timeout: str | None,
    hint: str,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg = resolved_settings().for_call(tier, provider=provider, timeout=timeout)
    fake_http.exc = _read_timeout()
    with pytest.raises(base.ProviderError) as caught:
        factory.make_provider(provider, cfg).generate("a draft", "s")
    watch, _ = _watchdog()
    watch.arm(cfg, provider)
    watch.cancel()
    assert watch._marker == f"[promptmend: {caught.value}]"
    assert str(caught.value).endswith(f"s: {hint}")


def _read_timeout() -> Exception:
    import httpx

    return httpx.ReadTimeout("slow")


# arm() words the marker after the provider's label, a name it does not know generically, and
# sets its alarm that far ahead (the real alarm clamps the wait to TIMEOUT_MAX:
# test_alarm_clamps_a_far_time).
@pytest.mark.parametrize(
    ("provider", "label"),
    [("openrouter", "OpenRouter"), ("lmstudio", "LM Studio"), ("nonsense", "The call")],
)
def test_arm_words_marker(clock: FakeClock, provider: str, label: str) -> None:
    watch, _ = _watchdog(clock)
    cfg = Settings(timeout=1e300)
    watch.arm(cfg, provider)
    try:
        assert watch._marker == f"[promptmend: {label} timed out after 1e+300s: {SETTING_HINT}]"
        (alarm,) = clock.alarms
        assert alarm.at == clock() + 1e300 + trigger.WATCHDOG_MARGIN - trigger._WATCHDOG_HISTORY
    finally:
        watch.cancel()


def test_cancel_before_arm_is_harmless() -> None:
    watch, _ = _watchdog()
    watch.cancel()


# --- The recorder keeps the attempt in flight ----------------------------------------------


def test_timed_out_keeps_the_attempt_in_flight() -> None:
    rec = recorder.Recorder("improve", "i")
    rec.track(resolved_settings())
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
    rec.track(resolved_settings())
    observer = rec.observer
    assert observer is not None
    meter = Meter(observer, "ollama", "m", openai_usage, local=True)
    meter.begin(loopback=True, attempt=1)
    meter.record(loopback=True, attempt=1, status=200, error_kind=None, body={}, latency=0.1)
    rec.timed_out()
    assert [a.error_kind for a in rec.attempts] == [None]


# The plain observer (the Try tab's AttemptList) is not told an attempt began.
def test_plain_observer_is_not_told_an_attempt_began() -> None:
    seen = AttemptList()
    Meter(seen, "ollama", "m", openai_usage).begin(loopback=True, attempt=1)
    assert seen == []


# --- In process: a name lookup that never returns ------------------------------------------


def test_blocking_name_lookup_in_process(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    history_rows: HistoryRows,
    clock: FakeClock,
) -> None:
    release = threading.Event()

    def getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        clock.advance(LATER)  # the lookup outlasts the deadline: the watchdog fires
        release.wait(30)
        raise socket.gaierror("released by the test")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    fake = Exits(then=release)
    monkeypatch.setattr(trigger, "_hard_exit", fake)
    # Fires 0.1 s after the limit, with time for history_rows' generous write budget (the
    # history wait is real time). The margin stays above the history wait, as in production,
    # so no wake is due the moment the alarm is set.
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
    assert capfd.readouterr().out == f"[promptmend: OpenRouter timed out after 1.0s: {FLAG_HINT}]"
    assert fake.codes == [0]
    (op,) = history_rows("operations")
    assert (op["outcome"], op["trigger_id"], op["profile_id"]) == ("error_marker", "-i-", "default")
    (attempt,) = history_rows("attempts")
    assert (attempt["error_kind"], attempt["status"], attempt["endpoint"]) == (
        "timeout",
        None,
        "remote",
    )


# A run that ends normally cancels its one alarm: it never wakes, even long after the deadline.
def test_normal_run_cancels_the_alarm(
    stub_provider: StubProvider, exits: Exits, clock: FakeClock
) -> None:
    args = ["improve", "--provider", "ollama", "--source", "argument", "--text", "x"]
    result = runner.invoke(app, args)
    assert (result.exit_code, result.stdout) == (0, "improved")
    (alarm,) = clock.alarms
    assert alarm.cancelled
    clock.advance(LATER)
    assert alarm.wakes == []
    assert exits.codes == []


# A watchdog that fired while the draft was being read stops the run before the provider
# call: nothing is sent after the marker.
def test_no_provider_call_after_the_marker(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    capfd: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    release = threading.Event()

    def stalled(op: str, *args: str) -> str:
        clock.advance(LATER)
        release.wait(30)
        return "a draft"

    monkeypatch.setattr(pipeline, "_clipboard", stalled)
    fake = Exits(then=release)
    monkeypatch.setattr(trigger, "_hard_exit", fake)
    result = runner.invoke(
        app, ["improve", "--provider", "ollama", "--source", "clipboard", "--timeout", "0.05"]
    )
    assert (result.exit_code, result.stdout) == (0, "")
    assert capfd.readouterr().out == f"[promptmend: Ollama timed out after 0.05s: {FLAG_HINT}]"
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
    clock: FakeClock,
    args: list[str],
    env: dict[str, str],
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    release = threading.Event()

    def stalled(op: str, *args: str) -> None:
        assert op == "copy"
        clock.advance(LATER)
        release.wait(30)

    monkeypatch.setattr(pipeline, "_clipboard", stalled)
    fake = Exits(then=release)
    monkeypatch.setattr(trigger, "_hard_exit", fake)
    # Fires 0.1 s after the limit, with time for history_rows' generous write budget (the
    # history wait is real time). The margin stays above the history wait, as in production,
    # so no wake is due the moment the alarm is set.
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


# entry.main() leaves the time it started a trigger run (improve, persona) for that run's
# watchdog, read once; other commands leave none.
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
    assert entry.take_start() is not None
    monkeypatch.setattr(sys, "argv", ["promptmend", "doctor"])
    entry.main()
    assert entry.take_start() is None


# The limit counts from that start: time already spent comes off the wait for the fire time,
# and a run that began past its limit gets only the margin, for its marker and history.
@pytest.mark.parametrize(("ago", "timeout", "wait"), [(10.0, 30.0, 20.0), (100.0, 1.0, 0.0)])
def test_limit_counts_from_entry_start(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock, ago: float, timeout: float, wait: float
) -> None:
    from promptmend import entry

    monkeypatch.setattr(entry, "_started", clock() - ago)
    watch, _ = _watchdog(clock)
    watch.arm(Settings(timeout=timeout), "ollama")
    try:
        (alarm,) = clock.alarms
        assert alarm.at is not None
        expected = wait + trigger.WATCHDOG_MARGIN - trigger._WATCHDOG_HISTORY
        assert alarm.at - clock() == pytest.approx(expected, abs=0.5)
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
    monkeypatch: pytest.MonkeyPatch,
    fake_http: FakeHttp,
    capfd: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "-".join(("test", "key")))
    fake_http.reply({"choices": [{"message": {"content": "rewrite"}}]})
    release = threading.Event()
    make_provider = factory.make_provider

    def slow_build(*args: Any, **kwargs: Any) -> Any:
        clock.advance(LATER)
        release.wait(30)
        return make_provider(*args, **kwargs)

    monkeypatch.setattr(pipeline, "make_provider", slow_build)
    monkeypatch.setattr(trigger, "_hard_exit", Exits(then=release))
    args = ["improve", "--provider", "openrouter", "--source", "argument", "--text", "x"]
    result = runner.invoke(app, [*args, "--timeout", "0.05"])
    assert (result.exit_code, result.stdout) == (0, "")
    assert capfd.readouterr().out == f"[promptmend: OpenRouter timed out after 0.05s: {FLAG_HINT}]"
    assert fake_http.requests == []


# --- --fail-on-marker (#407) -----------------------------------------------------------------


def _flagged(clock: FakeClock) -> tuple[Watchdog, recorder.Recorder]:
    watch, rec = _armed(clock)
    watch._fail_on_marker = True
    return watch, rec


# The watchdog's own timed-out marker (the run still reading or calling) exits 1 with the
# flag, through _hard_exit and through the run's later step alike; without it, 0.
@pytest.mark.parametrize("flag", [True, False])
def test_timed_out_marker_exit_code(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str], flag: bool
) -> None:
    watch, _ = _flagged(clock) if flag else _armed(clock)
    clock.advance(LATER)
    with pytest.raises(typer.Exit) as raised:
        watch.proceed()
    code = 1 if flag else 0
    assert (exits.codes, raised.value.exit_code) == ([code], code)
    assert capfd.readouterr().out == MARKER


# The settings-load marker (the provisional limit) is a failure too.
def test_settings_load_marker_exits_1(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _watchdog(clock)
    watch._fail_on_marker = True
    watch.arm_loading("30")
    clock.advance(LATER)
    assert exits.codes == [1]
    assert capfd.readouterr().out == "[promptmend: the settings did not load within 30.0s]"


# A stalled copy prints its marker before the rewrite: exit 1 with the flag, 0 without.
@pytest.mark.parametrize("flag", [True, False])
def test_stalled_copy_exit_code(
    exits: Exits, clock: FakeClock, capfdbinary: pytest.CaptureFixture[bytes], flag: bool
) -> None:
    watch, _ = _flagged(clock) if flag else _armed(clock)
    watch.delivering("", "the rewrite")
    clock.advance(LATER)
    assert exits.codes == [1 if flag else 0]
    # Written straight to fd 1 with this platform's newlines, as sys.stdout writes them.
    stalled = trigger.CLIPBOARD_STALLED.replace("\n", os.linesep).encode()
    assert capfdbinary.readouterr().out == stalled + b"the rewrite"


# The hard deadline ends a run that was still printing at the fire time and flushed since with
# the code its output had: 1 after a failure marker.
@pytest.mark.parametrize("failed", [True, False])
def test_hard_deadline_keeps_the_code(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str], failed: bool
) -> None:
    watch, _ = _flagged(clock)
    if failed:
        watch.failed()
    trigger.emit("output", watch)
    clock.advance_to(_fire_time())  # still printing: nothing yet
    watch.flushed()
    assert exits.codes == []
    clock.advance_to(_deadline())
    assert exits.codes == [1 if failed else 0]
    assert capfd.readouterr().out == "output"


# --- A stdin that is never closed (#407) ----------------------------------------------------

STDIN_MARKER = (
    "[promptmend: stdin was not closed within {}s, so nothing was sent: "
    "close stdin after the draft, or pass --source clipboard]"
)


class _OpenPipe:
    """A stdin that is a pipe left open: read() outlasts the deadline on ``clock`` and blocks
    until ``release`` is set. A pipe that already holds ``delivered`` text blocks the same way
    (read(n) returns only at the end, or once n characters are in), and returns it only then."""

    def __init__(self, release: threading.Event, clock: FakeClock, delivered: str = "") -> None:
        self._release, self._clock, self._delivered = release, clock, delivered

    def isatty(self) -> bool:
        return False

    def read(self, size: int = -1) -> str:
        self._clock.advance(LATER)
        self._release.wait(30)
        return self._delivered + " too late"


# A run still reading stdin when the watchdog fires gets the stdin marker, not one blaming the
# provider; nothing is built or sent, the history says error_marker, and with
# --fail-on-marker the exit code is 1. Without --source (a pipe) and with --source stdin, and
# for a pipe that delivered a draft and then stayed open, which the marker words truthfully.
@pytest.mark.parametrize(
    ("args", "code", "delivered"),
    [
        ([], 0, ""),
        (["--source", "stdin"], 0, ""),
        (["--fail-on-marker"], 1, ""),
        ([], 0, "a real draft\n"),
    ],
    ids=["default", "explicit", "fail-on-marker", "delivered-then-open"],
)
def test_open_stdin_gets_the_stdin_marker(
    monkeypatch: pytest.MonkeyPatch,
    fake_http: FakeHttp,
    capfd: pytest.CaptureFixture[str],
    history_rows: HistoryRows,
    clock: FakeClock,
    args: list[str],
    code: int,
    delivered: str,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "-".join(("test", "key")))
    release = threading.Event()
    exits = Exits(then=release)
    monkeypatch.setattr(trigger, "_hard_exit", exits)
    built: list[str] = []
    monkeypatch.setattr(pipeline, "make_provider", lambda name, *a, **k: built.append(name))
    # CliRunner puts its own stdin in place for the run, and its own back after it: the open
    # pipe replaces CliRunner's where the draft is read, so CliRunner's restore undoes it.
    choose = pipeline.choose_source

    def with_open_pipe(source: str | None, text: str | None) -> str:
        sys.stdin = _OpenPipe(release, clock, delivered)
        return choose(source, text)

    monkeypatch.setattr(pipeline, "choose_source", with_open_pipe)
    argv = ["improve", "--provider", "openrouter", "--timeout", "0.05", *args]
    result = runner.invoke(app, argv)
    assert (result.exit_code, result.stdout) == (code, "")
    assert capfd.readouterr().out == STDIN_MARKER.format(0.05)
    assert exits.codes == [code]
    assert (built, fake_http.requests) == ([], [])
    (op,) = history_rows("operations")
    assert op["outcome"] == "error_marker"


# A stdin read that returns in time moves on: the watchdog's marker is the provider's again.
def test_stdin_marker_only_while_reading(exits: Exits, capfd: pytest.CaptureFixture[str]) -> None:
    clock = FakeClock()
    watch, _ = _armed(clock)
    watch.reading("stdin")
    watch.proceed()
    clock.advance(LATER)
    assert capfd.readouterr().out == MARKER
    clock = FakeClock()
    watch, _ = _armed(clock)
    watch.reading("clipboard")
    clock.advance(LATER)
    assert capfd.readouterr().out == MARKER


# A flushed run that the deadline ends silently keeps the code its own output had: 1 after a
# failure marker, 0 after a rewrite.
@pytest.mark.parametrize("failed", [True, False])
def test_flushed_run_keeps_its_code(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str], failed: bool
) -> None:
    watch, _ = _flagged(clock)
    if failed:
        watch.failed()
    trigger.emit("output", watch)
    watch.flushed()
    clock.advance(LATER)
    assert exits.codes == [1 if failed else 0]
    assert capfd.readouterr().out == "output"


# --- The hard deadline: a claimed run that flushed late --------------------------------------


# A run claimed at the fire time is left alone then, and the one alarm is set on to the hard
# deadline (limit + margin); cancel() ends it there.
def test_claimed_run_sleeps_on_to_the_hard_deadline(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _watchdog(clock)
    watch.arm(Settings(timeout=30), "ollama")
    (alarm,) = clock.alarms
    deadline = clock() + 30 + trigger.WATCHDOG_MARGIN
    fire_at = deadline - trigger._WATCHDOG_HISTORY
    assert alarm.at == pytest.approx(fire_at)
    watch.claim()
    assert clock.alarms == [alarm]  # claim() starts nothing
    clock.advance_to(fire_at)
    assert alarm.wakes == [pytest.approx(fire_at)]
    assert alarm.at == pytest.approx(deadline)
    assert (exits.codes, capfd.readouterr().out) == ([], "")
    watch.cancel()
    assert alarm.cancelled
    clock.advance(LATER)
    assert len(alarm.wakes) == 1


# Unarmed (the settings did not load), claiming starts nothing.
def test_claim_unarmed_starts_nothing(clock: FakeClock) -> None:
    watch, _ = _watchdog(clock)
    watch.claim()
    assert clock.alarms == []


# At the hard deadline a claimed run that flushed after the fire time ends silently, not a
# moment before; once ended, nothing more wakes.
def test_hard_deadline_ends_a_flushed_run(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _armed(clock)
    watch.claim()
    clock.advance_to(_fire_time())
    assert exits.codes == []
    watch.flushed()
    clock.advance_to(_deadline() - 0.01)
    assert exits.codes == []
    clock.advance_to(_deadline())
    assert exits.codes == [0]
    assert capfd.readouterr().out == ""
    clock.advance(LATER)  # fired already: nothing more
    assert exits.codes == [0]
    (alarm,) = clock.alarms
    assert alarm.wakes == [pytest.approx(_fire_time()), pytest.approx(_deadline())]


# A stand-in exit that raises still releases a main thread waiting on the watchdog.
def test_raising_exit_still_releases(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
    def boom(code: int) -> None:
        raise RuntimeError("stand-in")

    monkeypatch.setattr(trigger, "_hard_exit", boom)
    watch, _ = _armed(clock)
    watch.flushed()
    with pytest.raises(RuntimeError):
        clock.advance(LATER)  # the fake wakes on this thread, so the raise comes back here
    assert watch._done.is_set()


# The hard deadline came while the run was still printing (claimed since before the fire
# time); the run flushes after it: it ends at once, silently, so the history write after it is
# not left unbounded.
def test_flush_after_the_hard_deadline_ends_at_once(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _armed(clock)
    trigger.emit("the rewrite", watch)
    clock.advance_to(_deadline())  # still printing: left alone
    assert exits.codes == []
    watch.flushed()
    assert exits.codes == [0]
    assert capfd.readouterr().out == "the rewrite"
    clock.advance(LATER)  # ended already: nothing more
    assert exits.codes == [0]


# A rewrite holding a lone surrogate still reaches the paste after the clipboard-stalled
# marker, the surrogate replaced as sys.stdout replaces it, instead of the whole text vanishing.
def test_stalled_copy_with_lone_surrogate(
    exits: Exits, clock: FakeClock, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    watch, _ = _armed(clock)
    watch.delivering("", "a\ud800b")
    clock.advance(LATER)
    stalled = trigger.CLIPBOARD_STALLED.replace("\n", os.linesep).encode()
    assert capfdbinary.readouterr().out == stalled + b"a?b"
    assert exits.codes == [0]


# An attempt that began before the watchdog fired and is still blocked before its request (a
# name lookup, say) and is released while the history is written sends it before the marker:
# the marker is written last and the process ends right after it.
def test_attempt_begun_before_the_marker_sends_before_it(
    monkeypatch: pytest.MonkeyPatch,
    fake_http: FakeHttp,
    capfd: pytest.CaptureFixture[str],
    clock: FakeClock,
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

    def fire_once_begun() -> None:
        begun.wait(10)  # however slow the run is to reach its request
        clock.advance(LATER)  # the watchdog fires on this thread, as on its alarm's own

    class Exit(Exits):
        def __call__(self, code: int) -> None:
            events.append("exit")
            super().__call__(code)

    monkeypatch.setattr(base, "_exchange", blocked)
    monkeypatch.setattr(fake_http, "handler", handled)
    monkeypatch.setattr(recorder.Recorder, "finish", history_write)
    monkeypatch.setattr(trigger, "_write_fd", marker_out)
    monkeypatch.setattr(trigger, "_hard_exit", Exit(then=unblock))
    # A history wait (real time) far longer than the test needs: the order comes from the
    # events alone. The margin stays above it, as in production.
    monkeypatch.setattr(trigger, "_WATCHDOG_HISTORY", 10.0)
    monkeypatch.setattr(trigger, "WATCHDOG_MARGIN", 10.1)
    watchdog = threading.Thread(target=fire_once_begun, daemon=True)
    watchdog.start()
    args = ["improve", "--provider", "openrouter", "--source", "argument", "--text", "x"]
    result = runner.invoke(app, [*args, "--timeout", "0.05"])
    watchdog.join(10)
    assert not watchdog.is_alive()
    assert (result.exit_code, result.stdout) == (0, "")
    assert capfd.readouterr().out == f"[promptmend: OpenRouter timed out after 0.05s: {FLAG_HINT}]"
    assert events == ["begun", "request", "marker", "exit"]


# The watchdog ended the run while the draft was read: proceed()'s Exit ends the run as it is,
# never taken for a failure of the run with a marker of its own.
def test_proceed_exit_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, clock: FakeClock
) -> None:
    release = threading.Event()

    def stalled(op: str, *args: str) -> object:
        clock.advance(LATER)
        return release.wait(30) and "a draft"

    monkeypatch.setattr(pipeline, "_clipboard", stalled)
    monkeypatch.setattr(trigger, "_hard_exit", Exits(then=release))
    failures: list[Exception] = []

    def failed(exc: Exception) -> str:
        failures.append(exc)
        return ""

    monkeypatch.setattr(pipeline, "marker", failed)
    result = runner.invoke(
        app, ["improve", "--provider", "ollama", "--source", "clipboard", "--timeout", "0.05"]
    )
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


# --- The settings load (#331) ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeout", "limit"),
    [(None, 30.0), ("default", 30.0), ("5", 5.0), ("0", 30.0), ("nonsense", 30.0)],
)
def test_loading_limit(timeout: str | None, limit: float) -> None:
    assert loading_limit(timeout) == limit


def test_arm_loading_words_its_marker(clock: FakeClock) -> None:
    watch, _ = _watchdog(clock)
    watch.arm_loading("1e300")
    try:
        assert watch._marker == "[promptmend: the settings did not load within 1e+300s]"
        (alarm,) = clock.alarms
        assert alarm.at == clock() + 1e300 + trigger.WATCHDOG_MARGIN - trigger._WATCHDOG_HISTORY
    finally:
        watch.cancel()


# arm() words the load alone (from arm_loading()), apart from the start-up before it. A run
# whose start-up and load used up the limit gets its marker back, to end with at once.
@pytest.mark.parametrize(
    ("startup", "load", "timeout", "expected", "ends"),
    [
        (
            0.0,
            3.0,
            30.0,
            r"Ollama timed out after 30\.0s, 3\.\ds of it loading the settings: try again, or "
            r"raise PROMPT_TIMEOUT_SECONDS",
            False,
        ),
        (
            3.0,
            0.0,
            30.0,
            r"Ollama timed out after 30\.0s: try again, or raise PROMPT_TIMEOUT_SECONDS",
            False,
        ),
        (
            0.0,
            5.0,
            1.0,
            r"the settings took 5\.\ds to load, 5\.\ds in all, past the 1\.0s time limit; "
            r"nothing was sent",
            True,
        ),
        (
            5.0,
            0.0,
            1.0,
            r"starting up took 5\.\ds, past the 1\.0s time limit; nothing was sent",
            True,
        ),
    ],
    ids=["slow-load", "slow-start", "load-used-the-limit", "start-used-the-limit"],
)
def test_arm_words_the_load(
    clock: FakeClock,
    startup: float,
    load: float,
    timeout: float,
    expected: str,
    ends: bool,
) -> None:
    import re

    watch, _ = _watchdog(clock)
    now = clock()
    watch._started, watch._load_started = now - startup - load, now - load
    returned = watch.arm(Settings(timeout=timeout), "ollama")
    watch.cancel()
    assert re.fullmatch(rf"\[promptmend: {expected}\]", watch._marker)
    assert returned == (watch._marker if ends else None)


# arm() moves the deadline on the same alarm: the provisional limit passes and nothing fires
# until the real one.
def test_provisional_limit_passes_after_arm(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _watchdog(clock)
    watch.arm_loading("1")
    (alarm,) = clock.alarms
    provisional = clock() + 1 + trigger.WATCHDOG_MARGIN - trigger._WATCHDOG_HISTORY
    assert alarm.at == pytest.approx(provisional)
    assert watch.arm(Settings(timeout=30), "ollama") is None
    assert clock.alarms == [alarm]  # no second thread
    real = clock() + 30 + trigger.WATCHDOG_MARGIN - trigger._WATCHDOG_HISTORY
    clock.advance_to(provisional + 0.01)
    clock.advance_to(real - 0.01)
    assert (exits.codes, capfd.readouterr().out) == ([], "")
    clock.advance_to(real)
    assert exits.codes == [0]
    assert capfd.readouterr().out == f"[promptmend: Ollama timed out after 30s: {SETTING_HINT}]"


# arm() took the lock first, and a wake for the provisional time comes after it (the alarm's
# thread had already ended its wait when arm() moved the time): it finds the fire time moved
# on, does nothing and sleeps on until the real one.
def test_wake_after_arm_moved_the_deadline_sleeps_on(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _watchdog(clock)
    watch.arm_loading("1")
    (alarm,) = clock.alarms
    provisional = alarm.at
    assert provisional is not None
    watch.arm(Settings(timeout=30), "ollama")
    real = clock() + 30 + trigger.WATCHDOG_MARGIN - trigger._WATCHDOG_HISTORY
    clock.advance_to(provisional + 0.1)
    alarm.on_wake()  # the late wake, past the provisional fire time
    assert (exits.codes, capfd.readouterr().out) == ([], "")
    assert alarm.at == pytest.approx(real)
    clock.advance_to(real)
    assert exits.codes == [0]
    assert capfd.readouterr().out.startswith("[promptmend: Ollama timed out after 30s")


# The provisional watchdog fired already (its wake took the lock first): arm() leaves it be,
# returns None (the marker is out) and sets no later wake.
def test_arm_after_the_provisional_fired(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _watchdog(clock)
    watch.arm_loading("1")
    clock.advance(LATER)
    assert watch.arm(Settings(timeout=30), "ollama") is None
    (alarm,) = clock.alarms
    assert alarm.at is None
    watch.cancel()
    assert capfd.readouterr().out == "[promptmend: the settings did not load within 1.0s]"
    assert exits.codes == [0]


# arm() can also move the deadline earlier on the same alarm (a configured limit shorter than
# the provisional one): the run fires at its own fire time, not at the provisional one.
def test_arm_moves_the_deadline_earlier(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _watchdog(clock)
    watch.arm_loading("30")
    (alarm,) = clock.alarms
    provisional = alarm.at
    assert provisional is not None
    assert watch.arm(Settings(timeout=1), "ollama") is None
    real = clock.START + 1 + trigger.WATCHDOG_MARGIN - trigger._WATCHDOG_HISTORY
    assert real < provisional
    assert clock.alarms == [alarm]  # the same thread, its time moved
    assert alarm.at == pytest.approx(real)
    clock.advance_to(real - 0.01)
    assert (exits.codes, capfd.readouterr().out) == ([], "")
    clock.advance_to(real)
    assert exits.codes == [0]
    assert capfd.readouterr().out.startswith("[promptmend: Ollama timed out after 1")


def _blocking_load(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> threading.Event:
    release = threading.Event()

    def load(*args: Any, **kwargs: Any) -> Settings:
        clock.advance(LATER)
        release.wait(30)
        raise OSError("released by the test")

    monkeypatch.setattr(Settings, "load", load)
    monkeypatch.setattr(trigger, "_hard_exit", Exits(then=release))
    monkeypatch.setattr(config, "DEFAULT_TIMEOUT", "0.05")
    return release


# A load that never returns ends with the settings marker, and no history: whether there is
# one is itself a setting.
@pytest.mark.parametrize("command", ["improve", "persona"])
def test_blocking_load_in_process(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    history_rows: HistoryRows,
    stub_provider: StubProvider,
    clock: FakeClock,
    command: str,
) -> None:
    _blocking_load(monkeypatch, clock)
    started = time.monotonic()
    result = runner.invoke(app, [command, "--trigger-id", "i" if command == "improve" else "p"])
    assert time.monotonic() - started < 10
    assert (result.exit_code, result.stdout) == (0, "")
    assert capfd.readouterr().out == "[promptmend: the settings did not load within 0.05s]"
    assert history_rows("operations") == []
    assert stub_provider.calls == []


# The call's --timeout bounds the load too, so a short one keeps its hard limit.
def test_blocking_load_on_the_call_timeout(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], clock: FakeClock
) -> None:
    _blocking_load(monkeypatch, clock)
    monkeypatch.setattr(config, "DEFAULT_TIMEOUT", "1e300")
    result = runner.invoke(app, ["improve", "--timeout", "0.05"])
    assert (result.exit_code, result.stdout) == (0, "")
    assert capfd.readouterr().out == "[promptmend: the settings did not load within 0.05s]"


# A slow load, then a clipboard that stalls: the marker names the load's share of the limit.
# Event-driven: the stalled read fires the watchdog itself.
def test_slow_load_then_stall_in_process(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    capfd: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    import re

    _slow_load(monkeypatch, clock, 3.0)

    def stalled(op: str, *args: str) -> str:
        clock.advance(LATER)
        return "a draft"

    monkeypatch.setattr(pipeline, "_clipboard", stalled)
    monkeypatch.setattr(trigger, "_hard_exit", Exits())
    result = runner.invoke(
        app, ["improve", "--provider", "ollama", "--source", "clipboard", "--timeout", "30"]
    )
    assert (result.exit_code, result.stdout) == (0, "")
    out = capfd.readouterr().out
    pattern = (
        r"\[promptmend: Ollama timed out after 30\.0s, \d+\.\ds of it loading the settings: "
        r"try again with a longer --timeout\]"
    )
    assert re.fullmatch(pattern, out)
    assert stub_provider.calls == []


def _slow_load(monkeypatch: pytest.MonkeyPatch, clock: FakeClock, seconds: float) -> None:
    """Settings.load() takes ``seconds`` on the fake clock (an alarm that falls due in them
    wakes)."""
    load = Settings.load

    def slow(cls: type[Settings], *args: Any, **kwargs: Any) -> Settings:
        clock.advance(seconds)
        return load(*args, **kwargs)

    monkeypatch.setattr(Settings, "load", classmethod(slow))


# Start-up and load used up the call's limit (start-up back-dated, a fast load or a slow one):
# the run ends at once with a marker that says so, and the draft is never sent; the history
# keeps the run.
@pytest.mark.parametrize("slow_load", [False, True])
def test_used_up_limit_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    capfd: pytest.CaptureFixture[str],
    history_rows: HistoryRows,
    clock: FakeClock,
    slow_load: bool,
) -> None:
    import re

    from promptmend import entry

    monkeypatch.setattr(entry, "_started", clock() - 10)
    # The real deadline stays well ahead, so the run's own marker is the one printed:
    # start-up used up the call's limit, but neither fire time is due yet.
    monkeypatch.setattr(trigger, "WATCHDOG_MARGIN", 60.0)
    if slow_load:
        _slow_load(monkeypatch, clock, 2.0)
    read: list[str] = []

    def clipboard(op: str, *args: str) -> str:
        read.append(op)
        return "x"

    monkeypatch.setattr(pipeline, "_clipboard", clipboard)
    monkeypatch.setattr(trigger, "_hard_exit", Exits())
    args = ["improve", "--provider", "ollama", "--timeout", "5", "--trigger-id", "il"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert capfd.readouterr().out == ""
    cause = (
        r"the settings took \d+\.\ds to load, 1\d\.\ds in all"
        if slow_load
        else (r"starting up took 1\d\.\ds")
    )
    expected = rf"\[promptmend: {cause}, past the 5\.0s time limit; nothing was sent\]"
    assert re.fullmatch(expected, result.stdout)
    assert stub_provider.calls == []
    assert read == []  # not even the draft was read
    (op,) = history_rows("operations")
    assert op["outcome"] == "error_marker"
    assert history_rows("attempts") == []


# Start-up alone ran past the provisional limit: arm_loading()'s marker says so, not that the
# settings did not load (they have not begun to). Its alarm is due at once, and wakes so.
def test_arm_loading_after_a_slow_start(
    clock: FakeClock, exits: Exits, capfd: pytest.CaptureFixture[str]
) -> None:
    import re

    watch, _ = _watchdog(clock)
    watch._started = clock() - 100
    watch.arm_loading("1")
    expected = (
        r"\[promptmend: starting up took 1\d\d\.\ds, past the 1\.0s time limit; nothing was sent\]"
    )
    assert re.fullmatch(expected, watch._marker)
    (alarm,) = clock.alarms
    assert alarm.at is not None
    assert alarm.at <= clock()
    clock.advance(0)  # due at once: the real alarm wakes straight away
    assert alarm.wakes == [clock()]
    assert capfd.readouterr().out == watch._marker
    assert exits.codes == [0]
    watch.cancel()


# A run that reaches arm() 3 s past its hard deadline still gets one bounded window for its
# error_marker row: the watchdog does not end it before the history is written.
def test_used_up_limit_still_records(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    history_rows: HistoryRows,
    clock: FakeClock,
) -> None:
    from promptmend import entry

    monkeypatch.setenv("PROMPT_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(entry, "_started", clock() - (5 + trigger.WATCHDOG_MARGIN + 3))
    exits = Exits()
    monkeypatch.setattr(trigger, "_hard_exit", exits)
    result = runner.invoke(app, ["improve", "--provider", "ollama", "--trigger-id", "il"])
    assert result.exit_code == 0
    assert result.stdout.startswith("[promptmend: starting up took 1")
    assert exits.codes == []
    (op,) = history_rows("operations")
    assert op["outcome"] == "error_marker"
    assert stub_provider.calls == []


# A run whose start-up used up its limit ends in its own marker: exit 1 with
# --fail-on-marker (#407), the same output.
def test_used_up_limit_fails_on_marker(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, clock: FakeClock
) -> None:
    from promptmend import entry

    monkeypatch.setenv("PROMPT_TIMEOUT_SECONDS", "5")
    exits = Exits()
    monkeypatch.setattr(trigger, "_hard_exit", exits)
    args = ["improve", "--provider", "ollama", "--source", "argument", "--text", "d"]
    monkeypatch.setattr(entry, "_started", clock() - 5)
    result = runner.invoke(app, [*args, "--fail-on-marker"])
    assert result.exit_code == 1
    assert result.stdout.startswith("[promptmend: starting up took 5")
    assert (exits.codes, stub_provider.calls) == ([], [])


# A thread that cannot start (in arm_loading(), the only start) costs neither improve nor
# persona its output: improve prints its marker, persona its text.
def test_no_thread_left_still_prints(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, clock: FakeClock
) -> None:
    monkeypatch.setenv("PROMPT_PERSONA", "I am a tester.")
    clock.no_thread = True
    result = runner.invoke(app, ["persona", "--trigger-id", "p"])
    assert (result.exit_code, result.stdout) == (0, "I am a tester.")
    result = runner.invoke(app, ["improve", "--provider", "ollama", "--source", "argument"])
    assert result.exit_code == 0
    assert result.stdout.startswith("[promptmend: ")


# The thread starts once, in arm_loading(): a process left with no thread after it loses
# neither its output nor its limit, since arm() and claim() start none.
def test_no_thread_after_arm_loading_keeps_output_and_limit(
    exits: Exits, capfd: pytest.CaptureFixture[str], clock: FakeClock
) -> None:
    watch, _ = _watchdog(clock)
    watch.arm_loading("1")
    clock.no_thread = True
    assert watch.arm(Settings(timeout=30), "ollama") is None
    trigger.emit("the rewrite", watch)
    watch.flushed()
    assert capfd.readouterr().out == "the rewrite"
    assert len(clock.alarms) == 1
    clock.advance_to(clock.START + 30 + trigger.WATCHDOG_MARGIN - trigger._WATCHDOG_HISTORY)
    assert exits.codes == [0]  # still bounded: the flushed run ends at the fire time
    assert capfd.readouterr().out == ""


# cancel() (the run's end) stops the alarm: no wake, however long after the deadline.
def test_no_wake_after_cancel(
    exits: Exits, clock: FakeClock, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _armed(clock)
    watch.cancel()
    clock.advance(LATER)
    (alarm,) = clock.alarms
    assert alarm.wakes == []
    assert (exits.codes, capfd.readouterr().out) == ([], "")


# --- The real alarm (trigger._ThreadAlarm) ---------------------------------------------------
#
# Tiny real waits on a thread of its own; every wait has a generous upper bound, every alarm
# is cancelled and its thread joined.

_BOUND = 10.0


@contextlib.contextmanager
def _real_alarm(on_wake: Any, clock: Any = time.monotonic) -> Iterator[trigger._ThreadAlarm]:
    alarm = trigger._ThreadAlarm(clock, on_wake)
    try:
        yield alarm
    finally:
        alarm.cancel()
        alarm._thread.join(_BOUND)
        assert not alarm._thread.is_alive()


def test_alarm_wakes_at_the_time_set() -> None:
    woke = threading.Event()
    with _real_alarm(woke.set) as alarm:
        assert alarm._thread.daemon
        started = time.monotonic()
        alarm.set(started + 0.05)
        assert woke.wait(_BOUND)
        assert time.monotonic() - started >= 0.05  # never before the time set


# A time moved earlier while the thread sleeps wakes at the new time, not the old one.
def test_alarm_moved_earlier_wakes_then() -> None:
    woke = threading.Event()
    with _real_alarm(woke.set) as alarm:
        alarm.set(time.monotonic() + 3600)
        alarm.set(time.monotonic() + 0.01)
        assert woke.wait(_BOUND)


# A time moved later while the thread sleeps: the old time passes without a wake. On a clock
# the test moves, so the old time passing is not a matter of real time.
def test_alarm_moved_later_does_not_wake_at_the_old_time() -> None:
    now = [0.0]
    asked = threading.Event()

    def clock() -> float:
        asked.set()
        return now[0]

    woke = threading.Event()
    with _real_alarm(woke.set, clock) as alarm:
        alarm.set(1.0)
        assert asked.wait(_BOUND)  # the thread is waiting on 1.0
        asked.clear()
        alarm.set(2.0)  # moved later; the thread looks again
        assert asked.wait(_BOUND)
        asked.clear()
        # The time was moved before the clock is: a test stalled between the two lines cannot
        # let the thread find the old time reached.
        now[0] = 1.5  # past the old time, before the new one
        alarm.set(2.0)  # only to make the thread read the clock again
        assert asked.wait(_BOUND)
        assert not woke.wait(0.2)
        now[0] = 2.0
        alarm.set(2.0)
        assert woke.wait(_BOUND)


# cancel() ends a sleeping thread, without a wake.
def test_alarm_cancel_while_sleeping() -> None:
    woke = threading.Event()
    alarm = trigger._ThreadAlarm(time.monotonic, woke.set)
    alarm.set(time.monotonic() + 3600)
    alarm.cancel()
    alarm._thread.join(_BOUND)
    assert not alarm._thread.is_alive()
    assert not woke.is_set()


# A cancelled alarm set again does not wake: cancel() is for good.
def test_alarm_set_after_cancel_does_not_wake() -> None:
    woke = threading.Event()
    with _real_alarm(woke.set) as alarm:
        alarm.cancel()
        alarm._thread.join(_BOUND)
        alarm.set(time.monotonic())
        assert not woke.wait(0.1)


# on_wake may set the next time itself (as _wake() does for the hard deadline), and the
# thread wakes again for it.
def test_alarm_set_from_inside_the_wake() -> None:
    wakes: list[float] = []
    twice = threading.Event()
    holder: list[trigger._ThreadAlarm] = []

    def on_wake() -> None:
        wakes.append(time.monotonic())
        if len(wakes) == 1:
            holder[0].set(time.monotonic() + 0.01)
        else:
            twice.set()

    with _real_alarm(on_wake) as alarm:
        holder.append(alarm)
        alarm.set(time.monotonic())
        assert twice.wait(_BOUND)
    assert len(wakes) == 2


# An exception in on_wake is reported (threading.excepthook, as for any thread) and does not
# end the wait: a time set before it still wakes.
def test_alarm_survives_a_failing_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    reported: list[Any] = []
    monkeypatch.setattr(threading, "excepthook", reported.append)
    woke = threading.Event()
    holder: list[trigger._ThreadAlarm] = []

    def on_wake() -> None:
        if not reported:
            holder[0].set(time.monotonic())
            raise RuntimeError("a failing wake")
        woke.set()

    with _real_alarm(on_wake) as alarm:
        holder.append(alarm)
        alarm.set(time.monotonic())
        assert woke.wait(_BOUND)
    (args,) = reported
    assert args.exc_type is RuntimeError
    assert args.thread is alarm._thread


# A time past TIMEOUT_MAX is waited on in TIMEOUT_MAX steps, never handed to the condition
# whole (which would raise OverflowError in the thread).
def test_alarm_clamps_a_far_time(monkeypatch: pytest.MonkeyPatch) -> None:
    reported: list[Any] = []
    monkeypatch.setattr(threading, "excepthook", reported.append)
    asked = threading.Event()

    def clock() -> float:
        asked.set()
        return time.monotonic()

    woke = threading.Event()
    with _real_alarm(woke.set, clock) as alarm:
        alarm.set(time.monotonic() + 1e300)
        assert asked.wait(_BOUND)  # the thread worked out its wait, and waits on
    assert (reported, woke.is_set()) == ([], False)


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


# The limit comes from the settings, not from --timeout: the provisional limit (the default,
# 30 s) is armed first and arm() moves the real alarm's time earlier, on its own thread.
def test_trickling_server_with_the_limit_from_the_settings() -> None:
    argv = [arg for arg in _ARGS if arg not in ("--timeout", str(LIMIT))]
    assert "--timeout" not in argv
    with smoke.stub_server(trickle=0.2) as stub:
        env = {**smoke.stub_env(stub.port, os.environ), "PROMPT_TIMEOUT_SECONDS": str(LIMIT)}
        proc, seconds = _run([*_ENTRY, *argv], env)
    assert (proc.returncode, proc.stderr) == (0, b"")
    expected = timed_out_message("OpenRouter", LIMIT, SETTING_HINT)
    assert proc.stdout.decode("utf-8") == f"[promptmend: {expected}]"
    assert seconds <= LIMIT + trigger.WATCHDOG_MARGIN + SLACK


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


# A settings load that never returns (simulated in the child, with a 1 s built-in default or
# the call's --timeout 1): the settings marker, exit 0 and empty stderr, within limit + margin.
# The child silences stderr (quiet_trigger(), as entry.main() does) before importing config,
# so no import-time warning reaches stderr for a reason of its own.
_BLOCKED_LOAD = """
import sys
import threading

mode = sys.argv[1]
sys.argv = ["promptmend", *sys.argv[2:]]
from promptmend import entry

entry.quiet_trigger(sys.argv[1:])
from promptmend import config

if mode == "default":
    config.DEFAULT_TIMEOUT = "1"
config.Settings.load = classmethod(lambda cls: threading.Event().wait())
entry.main()
"""


@pytest.mark.parametrize(
    "args",
    [
        ["default", "improve", "--trigger-id", "i"],
        ["option", "improve", "--timeout", "1", "--trigger-id", "i"],
        ["default", "persona", "--trigger-id", "p"],
    ],
    ids=["improve-default", "improve-timeout", "persona"],
)
def test_settings_load_that_never_returns(args: list[str]) -> None:
    env = smoke.stub_env(9, os.environ)
    proc, seconds = _run([sys.executable, "-c", _BLOCKED_LOAD, *args], env)
    assert (proc.returncode, proc.stderr) == (0, b"")
    assert proc.stdout.decode("utf-8") == "[promptmend: the settings did not load within 1.0s]"
    assert seconds <= LIMIT + trigger.WATCHDOG_MARGIN + SLACK


# The same stalled load with --fail-on-marker (#407): the same marker, exit code 1.
def test_settings_load_that_never_returns_fails_on_marker() -> None:
    args = ["option", "improve", "--timeout", "1", "--fail-on-marker"]
    proc, _ = _run([sys.executable, "-c", _BLOCKED_LOAD, *args], smoke.stub_env(9, os.environ))
    assert (proc.returncode, proc.stderr) == (1, b"")
    assert proc.stdout.decode("utf-8") == "[promptmend: the settings did not load within 1.0s]"


# With --fail-on-marker the watchdog's os._exit ends the process with exit code 1 and the
# same marker; stderr stays empty.
def test_trickling_server_fails_on_marker() -> None:
    with smoke.stub_server(trickle=0.2) as stub:
        env = smoke.stub_env(stub.port, os.environ)
        proc, seconds = _run([*_ENTRY, *_ARGS, "--fail-on-marker"], env)
    assert (proc.returncode, proc.stderr) == (1, b"")
    assert proc.stdout.decode("utf-8") == MARKER
    assert seconds <= LIMIT + trigger.WATCHDOG_MARGIN + SLACK


# A launcher that leaves stdin an open pipe, never written or closed: improve with no
# --source (or --source stdin) waits for its draft until the watchdog, which prints the stdin
# marker and sends nothing; exit 0, or 1 with --fail-on-marker. A pipe is never a terminal,
# so no OS reads the clipboard here.
@pytest.mark.parametrize(
    ("args", "code"),
    [([], 0), (["--source", "stdin", "--fail-on-marker"], 1)],
    ids=["default", "explicit-fail-on-marker"],
)
def test_open_stdin_pipe_as_a_process(args: list[str], code: int) -> None:
    argv = [*_ENTRY, "improve", "--provider", "openrouter", "--timeout", str(LIMIT), *args]
    with smoke.stub_server() as stub:
        env = {**smoke.stub_env(stub.port, os.environ), **_LOUD}
        started = time.monotonic()
        proc = subprocess.Popen(
            argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            out, err = _wait_open(proc)
        finally:
            proc.kill()
            assert proc.stdin is not None
            proc.stdin.close()
        seconds = time.monotonic() - started
        assert stub.requests == 0
    assert (proc.returncode, err) == (code, b"")
    assert out.decode("utf-8") == STDIN_MARKER.format(LIMIT)
    assert seconds <= LIMIT + trigger.WATCHDOG_MARGIN + SLACK


def _wait_open(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """The child's stdout and stderr once it exits, its stdin left open (communicate() would
    close it)."""
    assert proc.stdout is not None
    assert proc.stderr is not None
    out = proc.stdout.read()
    err = proc.stderr.read()
    proc.wait(60)
    return out, err


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
