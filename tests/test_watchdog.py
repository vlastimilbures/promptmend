"""improve's hard time limit (#307): whatever blocks (name resolution, a proxy lookup, the
clipboard, a server that never answers or trickles its reply), the run ends within its limit
plus cli.WATCHDOG_MARGIN with exactly one result on stdout, exit code 0 and empty stderr.

The in-process tests patch cli._hard_exit, so the watchdog never ends pytest; the real-process
tests run the CLI the way Espanso does, against smoke.stub_server()'s slow modes, with a 1 s
limit to keep the suite fast."""

from __future__ import annotations

import contextlib
import math
import os
import socket
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest
import typer
from typer.testing import CliRunner

from promptmend import cli, recorder, smoke
from promptmend.cli import Rewrite, _Watchdog, app
from promptmend.config import Settings
from promptmend.gate import GatedProvider
from promptmend.providers.base import timed_out_message
from promptmend.providers.usage import AttemptUsage, Meter, openai_usage

if TYPE_CHECKING:
    from conftest import HistoryRows, StubProvider

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
    monkeypatch.setattr(cli, "_hard_exit", fake)
    return fake


def _watchdog(provider: str = "openrouter") -> tuple[_Watchdog, recorder.Recorder]:
    rec = recorder.Recorder("improve", "i")
    watch = _Watchdog(rec, Rewrite())
    watch._marker = f"[promptmend: {timed_out_message('OpenRouter', LIMIT)}]"
    return watch, rec


# --- The lock: exactly one result --------------------------------------------------------


def test_claim_is_taken_once() -> None:
    watch, _ = _watchdog()
    assert watch.claim()
    assert not watch.claim()


# The run printed first: the watchdog firing afterwards writes nothing and never exits.
def test_emit_first_leaves_the_watchdog_nothing(
    exits: Exits, capfd: pytest.CaptureFixture[str]
) -> None:
    watch, _ = _watchdog()
    cli._emit("the rewrite", watch)
    watch._fire()
    assert capfd.readouterr().out == "the rewrite"
    assert exits.codes == []


# The watchdog fired first: its marker goes straight to fd 1, it exits 0, and the run's own
# output is never printed (the run ends at its _emit()).
def test_watchdog_first_wins_alone(exits: Exits, capfd: pytest.CaptureFixture[str]) -> None:
    watch, rec = _watchdog()
    watch._fire()
    with pytest.raises(typer.Exit) as raised:
        cli._emit("the rewrite", watch)
    assert raised.value.exit_code == 0
    assert capfd.readouterr().out == MARKER
    assert exits.codes == [0]
    assert rec.outcome == recorder.ERROR_MARKER


# A race of the two from many threads still prints exactly one result.
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
            cli._emit("the rewrite", watch)
        thread.join()
    out = capfd.readouterr().out
    assert out.count("the rewrite") + out.count(MARKER) == 20


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
    eol = os.linesep.encode()
    note = b"[promptmend: sent despite: email]" + eol + eol
    assert capfdbinary.readouterr().out == note + b"[promptmend: x[201~ timed out]"


# A history write that hangs never holds the exit beyond _WATCHDOG_HISTORY.
def test_slow_history_does_not_delay_exit(
    monkeypatch: pytest.MonkeyPatch, exits: Exits, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_WATCHDOG_HISTORY", 0.1)
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

    monkeypatch.setattr(cli, "_write_fd", broken)
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
    monkeypatch.setattr(cli, "_hard_exit", fake)
    # Fires 0.1 s after the limit, with time for history_rows' generous write budget.
    monkeypatch.setattr(cli, "_WATCHDOG_HISTORY", 5.0)
    monkeypatch.setattr(cli, "WATCHDOG_MARGIN", 5.1)
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
            "0.2",
            "--trigger-id",
            "i",
        ],
    )
    assert time.monotonic() - started < 10
    assert result.exit_code == 0
    assert result.stdout == ""  # the run's own marker was never printed
    assert capfd.readouterr().out == "[promptmend: OpenRouter timed out after 0.2s]"
    assert fake.codes == [0]
    (op,) = history_rows("operations")
    assert (op["outcome"], op["trigger_id"], op["profile_id"]) == ("error_marker", "-i-", "default")
    (attempt,) = history_rows("attempts")
    assert (attempt["error_kind"], attempt["status"], attempt["endpoint"]) == (
        "timeout",
        None,
        "remote",
    )


# A run that ends normally cancels its timer, so no watchdog fires after it.
def test_normal_run_cancels_the_timer(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, exits: Exits
) -> None:
    monkeypatch.setattr(cli, "WATCHDOG_MARGIN", 0.55)
    monkeypatch.setattr(cli, "_WATCHDOG_HISTORY", 0.5)
    args = ["improve", "--provider", "ollama", "--source", "argument", "--text", "x"]
    result = runner.invoke(app, [*args, "--timeout", "0.1"])
    assert (result.exit_code, result.stdout) == (0, "improved")
    assert not exits.called.wait(0.5)


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
    assert seconds <= LIMIT + cli.WATCHDOG_MARGIN + SLACK


_ENTRY = [sys.executable, "-m", "promptmend.entry"]


# Accepts the request and never answers: the provider's own read timeout words the marker.
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
    with smoke.stub_server(LIMIT * 0.6) as stub:
        proc, _ = _run([*_ENTRY, *_ARGS], smoke.stub_env(stub.port, os.environ))
    assert (proc.returncode, proc.stderr) == (0, b"")
    assert proc.stdout.decode("utf-8") == smoke.REPLY


@pytest.mark.parametrize(
    ("delay", "trickle"), [(-1.0, 0.0), (math.nan, 0.0), (0.0, math.inf), (0.0, -1.0)]
)
def test_stub_server_refuses_bad_waits(delay: float, trickle: float) -> None:
    with pytest.raises(ValueError, match="non-negative"), smoke.stub_server(delay, trickle=trickle):
        pass
