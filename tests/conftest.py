from __future__ import annotations

import faulthandler
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
import pytest

from promptmend import config
from promptmend.config import Settings, env_names
from promptmend.providers import base

if TYPE_CHECKING:
    from promptmend.history import HistoryStore

# How long the interpreter may take to exit after the summary (#375).
_EXIT_LIMIT = 60


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    """#375: after the summary, the interpreter's exit joins every thread pool's threads
    (shutdown_default_executor, concurrent.futures' exit hook); one that never ends, a
    Textual thread worker say, would hang the CI job where no per-test limit reaches. A
    minute later every thread's stack is dumped and the process exits 1; a clean exit
    cancels the dump."""
    if sys.__stderr__ is not None:
        faulthandler.dump_traceback_later(_EXIT_LIMIT, exit=True, file=sys.__stderr__)


# history_rows(table) lists that table of the per-test usage history as dicts.
HistoryRows = Callable[[str], list[dict[str, Any]]]
# seed_history(operation, attempts=(), store=None) writes one record and returns the store.
SeedHistory = Callable[..., "HistoryStore"]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent the real .env / shell environment from leaking into tests.

    Settings.load() reads the real environment and the first .env it finds (it never
    writes os.environ), so without this a test's result would depend on the developer's
    shell and real .env contents.
    """
    for key in env_names():
        monkeypatch.delenv(key, raising=False)
    # A developer's CA bundle would switch every https client off the OS trust store (#314,
    # tls.verify()); a test of that branch sets its own.
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    # Point the loader at a per-test file (PROMPTMEND_ENV, legacy mode): unsetting it leaves
    # legacy mode.
    monkeypatch.setenv("PROMPTMEND_ENV", str(tmp_path / ".env"))
    # ...and its real user profile directory is never read either (prompt_builder).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    # The usage history and price table go to per-test dirs, never the developer's real ones.
    for key in ("XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / "data"))
    # The home the config dir would derive from, and the editable-install root, are per-test
    # temp dirs too: a test that unsets PROMPTMEND_ENV can never read or write the developer's
    # real files (config.toml, secrets.toml, the folders), nor have repair mode report the
    # checkout's .env (config.unread_env_files) or doctor call the install editable.
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    from promptmend import config

    monkeypatch.setattr(config, "_PROJECT_ROOT", tmp_path / "project")
    # Never probe the developer's real clipboard: a concealed item there would fail tests.
    import promptmend.pipeline as pipeline

    monkeypatch.setattr(pipeline, "is_concealed", lambda: None)
    # ...nor run the real espanso, uv or brew: a deploy test passes or patches in its own.
    import promptmend.deploy as deploy

    def refuse(argv: list[str]) -> None:
        raise AssertionError(f"a test ran a real command: {argv}")

    monkeypatch.setattr(deploy, "run_command", refuse)
    # The same answers on every OS: a test of the Windows Espanso path turns it on (#210).
    monkeypatch.setattr(deploy, "WINDOWS_ESPANSO", False)
    # ...nor ask pypi.org for the newest release (#197): every check is unknown, unless a test
    # puts the real fetch back (test_update_check.REAL_FETCH) and answers it with fake_http.
    from promptmend import update_check

    def no_pypi() -> str:
        raise httpx.ConnectError("tests never reach pypi.org")

    monkeypatch.setattr(update_check, "_fetch", no_pypi)
    # ...and starts without the previous test's answer kept in memory.
    monkeypatch.setattr(update_check, "_last", None)
    # ...nor ask a local Ollama or LM Studio whether it answers (#220): nothing does, unless a
    # test puts the real GET back (test_local_probe.REAL_GET) and answers it with fake_http.
    from promptmend import local_probe

    def no_server(url: str) -> None:
        raise httpx.ConnectError("tests never reach a local model server")

    monkeypatch.setattr(local_probe, "_get", no_server)
    # The user patterns safe_repr() hides are set by every settings load: start each test
    # without the previous test's.
    from promptmend import redaction

    monkeypatch.setattr(redaction, "_user_patterns", ())
    # ...nor look up the developer's real `promptmend` on PATH (doctor's path_shadow): a test
    # of the check passes its own environ.
    import functools

    from promptmend import doctor

    monkeypatch.setattr(
        doctor, "_path_shadow_check", functools.partial(doctor._path_shadow_check, environ={})
    )


# Every background thread a test can leave behind: the update checks (#315) and the interface's
# reload reads (#317, state._beside()).
BACKGROUND_THREADS = (
    "promptmend-reload",
    "promptmend-reload-update-check",
    "promptmend-update-check",
)


@pytest.fixture(autouse=True)
def background_threads_end_with_the_test(isolated_env: None) -> Iterator[None]:
    """The update check (#315) and the interface's reload reads (#317) run in daemon threads:
    each one a test started must end inside the test, while its fakes (no pypi.org, no
    Espanso, the temp HOME) still hold, so a reload a newer one cancelled cannot run gather()
    after them. Torn down before isolated_env's patches are undone; a thread still running
    after the wait fails the test rather than reach the real network or folders later."""
    import threading

    before = {t for t in threading.enumerate() if t.name in BACKGROUND_THREADS}
    yield
    for thread in threading.enumerate():
        if thread.name in BACKGROUND_THREADS and thread not in before:
            thread.join(10)
            if thread.is_alive():
                pytest.fail(f"{thread.name} outlived the test")


@pytest.fixture(autouse=True)
def no_hard_exit(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """improve's watchdog (#307) ends the process with os._exit: in a test that would end
    pytest itself with exit code 0, skipping the rest and fail_under. Every test gets a
    stand-in that fails it instead; a test of the watchdog patches its own over this one
    (test_watchdog.py's ``exits``). The start time entry.main() leaves for the watchdog is
    dropped too, so no run counts its limit from an earlier test's."""
    import promptmend.trigger as trigger
    from promptmend import entry

    calls: list[int] = []
    monkeypatch.setattr(trigger, "_hard_exit", calls.append)
    monkeypatch.setattr(entry, "_started", None)
    yield
    assert not calls, "improve's watchdog fired and would have ended pytest (trigger._hard_exit)"


# Headers httpx adds to every request on its own; calls[i]["headers"] leaves them out so a
# test sees what the provider set (plus the Content-Type httpx derives from json=).
_HTTPX_OWN_HEADERS = frozenset(
    {"host", "accept", "accept-encoding", "connection", "user-agent", "content-length"}
)


def _response(
    json_data: Any = None,
    *,
    status_code: int = 200,
    bad_json: bool = False,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    if bad_json:
        return httpx.Response(status_code, content=b"<html>not json", headers=headers)
    return httpx.Response(status_code, json={} if json_data is None else json_data, headers=headers)


class FakeHttp:
    """Answers every request through httpx.MockTransport, so httpx still builds, encodes and
    sends each real request (headers, JSON body, timeouts) while nothing reaches the network.

    Replies come from ``queue()`` in order while it lasts, then from ``exc`` if set, then from
    the last ``reply()``. A queued item is ``_response()`` keyword arguments or an exception.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.client_kwargs: list[dict[str, Any]] = []
        # Each wait post_json made before a retry, in seconds; nothing really sleeps.
        self.sleeps: list[float] = []
        self.exc: Exception | None = None
        self._queue: list[dict[str, Any] | Exception] = []
        self._reply: dict[str, Any] = {}

    def reply(self, json_data: Any = None, **kwargs: Any) -> FakeHttp:
        self._reply = {"json_data": json_data, **kwargs}
        return self

    def queue(self, *items: dict[str, Any] | Exception) -> FakeHttp:
        self._queue.extend(items)
        return self

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Each request as {"url", "json", "headers"}; headers keep the case they were set in."""
        return [
            {
                "url": str(request.url),
                "json": json.loads(request.content) if request.content else None,
                "headers": {
                    key.decode(): value.decode()
                    for key, value in request.headers.raw
                    if key.decode().lower() not in _HTTPX_OWN_HEADERS
                },
            }
            for request in self.requests
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return _response(**item)
        if self.exc:
            raise self.exc
        # A fresh Response per request: httpx consumes and closes each one.
        return _response(**self._reply)


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    """Patch httpx.Client so every client uses a MockTransport; no test may reach the network.

    The patched client replaces any transport the code passes (post_json gives loopback URLs
    their own), and refuses a proxy or mount, which would take precedence over the transport.
    ``client_kwargs`` records the arguments as the code passed them.
    """
    fake = FakeHttp()
    real_client = httpx.Client

    def client(*args: Any, **kwargs: Any) -> httpx.Client:
        assert not {"proxy", "mounts"} & kwargs.keys(), "fake_http cannot honour a proxy"
        fake.client_kwargs.append(kwargs)
        return real_client(*args, **{**kwargs, "transport": httpx.MockTransport(fake.handler)})

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(base, "_sleep", fake.sleeps.append)
    return fake


class StubProvider:
    """Stands in for pipeline.make_provider and the provider it builds: records each build
    and generate() call, and returns ``result`` or raises ``exc``."""

    def __init__(self) -> None:
        self.result = "improved"
        self.exc: Exception | None = None
        self.built: list[tuple[str, Settings]] = []
        self.options: list[dict[str, Any]] = []
        self.calls: list[dict[str, str]] = []

    def __call__(self, name: str, cfg: Settings, **options: Any) -> StubProvider:
        self.built.append((name, cfg))
        self.options.append(options)
        return self

    def generate(self, prompt: str, system_prompt: str) -> str:
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        if self.exc:
            raise self.exc
        return self.result


@pytest.fixture
def stub_provider(monkeypatch: pytest.MonkeyPatch) -> StubProvider:
    """Replace the make_provider rewrite() calls, so CLI tests never build a real provider."""
    import promptmend.pipeline as pipeline

    stub = StubProvider()
    monkeypatch.setattr(pipeline, "make_provider", stub)
    return stub


@pytest.fixture
def history_rows(monkeypatch: pytest.MonkeyPatch) -> HistoryRows:
    """Gives history writes a generous time budget (the real ~0.25 s drops writes on a loaded
    CI runner) and returns a reader: history_rows("operations") lists that table of the
    per-test usage history as dicts, [] when nothing was written. A write that still missed
    its budget is in the spool, not lost: the reader replays it first, as stats, export and
    doctor do, so a slow runner cannot leave a database file without its tables behind."""
    import sqlite3
    from contextlib import closing

    from promptmend import history

    monkeypatch.setattr(history, "_BUDGET", 2.25)
    monkeypatch.setattr(history, "_WRITE_BUDGET", 2.0)

    def read(table: str) -> list[dict[str, Any]]:
        path = history.history_path()
        history.HistoryStore(path).replay()
        if not path.is_file():
            return []
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            order = "occurred_at_utc" if table == "operations" else "operation_id, seq"
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]  # noqa: S608

    return read


@pytest.fixture
def seed_history(monkeypatch: pytest.MonkeyPatch) -> SeedHistory:
    """Returns seed(operation, attempts=(), store=None), which writes one record to the
    per-test usage history (or ``store``) and fails the test if it is dropped. Tests that only
    need rows use it instead of asserting record() directly: the write runs with a budget no
    runner exhausts (the real one, and even history_rows' 2 s, have dropped a write on a slow
    Windows runner, #154), and a drop names the exception and the lost-write marker instead of
    a bare ``assert False``. The budget tests in test_history.py assert record() themselves."""
    import sys

    from promptmend import history

    def seed(
        operation: Mapping[str, Any],
        attempts: Sequence[Mapping[str, Any]] = (),
        store: HistoryStore | None = None,
    ) -> HistoryStore:
        store = store or history.HistoryStore(history.history_path())
        errors: list[BaseException | None] = []
        mark_lost, spool_or_mark = store._mark_lost, store._spool_or_mark

        def capture(*args: Any, **kwargs: Any) -> bool:
            errors.append(sys.exception())  # record() calls it inside its except block
            return mark_lost(*args, **kwargs)

        def capture_spool(*args: Any, **kwargs: Any) -> bool:
            errors.append(sys.exception())
            return spool_or_mark(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(history, "_BUDGET", 60.0)
            patch.setattr(history, "_WRITE_BUDGET", 30.0)
            patch.setattr(store, "_mark_lost", capture)
            patch.setattr(store, "_spool_or_mark", capture_spool)
            stored = store.record(operation, attempts)
        if not stored:
            marker = store.lost_path.read_text("utf-8") if store.lost_path.is_file() else None
            pytest.fail(f"history write dropped: {errors!r}; {store.lost_path.name}: {marker}")
        return store

    return seed


def resolved_settings() -> Settings:
    """Settings as the product resolves them (Settings.load(): ConfigLayers.resolve() in
    strict mode, then .settings()), from os.environ as the test has patched it and the
    files of this test's temp folders. Strict mode, so a bad value raises the ValueError
    naming its variable, as a trigger's load does. Bare Settings() is only the built-in
    defaults (#409)."""
    return config.ConfigLayers.resolve(dict(os.environ)).settings()


def read_env_file(path: Path) -> dict[str, str] | None:
    """KEY=VALUE pairs of a .env file, as the settings loader parses them, or None when it is
    missing or unreadable."""
    try:
        raw_text = config._env_text(path.read_bytes())
    except (OSError, UnicodeDecodeError):
        return None
    return config._parse_env_text(raw_text)[0]


@pytest.fixture
def smoke_in_process(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """smoke.run()'s CLI run in this process (Typer's CliRunner) with the environment
    smoke.run() gives it, against the real stub on 127.0.0.1, so the smoke test checks what
    improve really prints. The clipboard is replaced, never reached: returns each clipboard
    operation improve asked for."""
    from typer.testing import CliRunner

    from promptmend import pipeline, smoke
    from promptmend.cli import app

    calls: list[str] = []

    def clipboard(op: str, *args: str) -> object:
        calls.append(op)
        return None

    def run_cli(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, bytes]:
        args = list(argv)
        result = CliRunner().invoke(app, args[args.index("improve") :], env=dict(env))
        return result.exit_code, result.stdout_bytes

    monkeypatch.setattr(pipeline, "_clipboard", clipboard)
    monkeypatch.setattr(smoke, "run_cli", run_cli)
    return calls


class FakeTimer:
    """One of FakeClock's timers: ``action`` runs once the clock reaches ``due``, unless the
    timer was cancelled first. ``interval`` is the wait it was started with, as on a
    threading.Timer."""

    def __init__(
        self, clock: FakeClock, interval: float, due: float, action: Callable[[], None]
    ) -> None:
        self._clock = clock
        self.interval, self.due, self.action = interval, due, action
        self.cancelled = False
        self.fired = False

    def cancel(self) -> None:
        if not (self.fired or self.cancelled) and self.due <= self._clock.now:
            # A threading.Timer would have fired by now (at once, for a wait of 0): a test
            # that cancels it unfired passes only on a race production would lose.
            self._clock.problems.append(f"a timer due at {self.due} was cancelled unfired")
        self.cancelled = True

    def fire_anyway(self) -> None:
        """Run the action although the timer was cancelled, as a threading.Timer whose wait
        had already ended does."""
        self.fired = True
        self.action()


class FakeClock:
    """trigger.Watchdog's clock and timers in a test (``clock=fake, timers=fake.start``):
    time moves only in advance(), which runs each timer that falls due, in the order they fall
    due, on the thread that calls advance(); never inside start(), as a threading.Timer never
    runs on the thread that starts it. With ``no_thread`` set, start() fails as a process
    with no thread left does.

    It keeps the tests honest (verify(), run after every test by fake_clocks_are_honest): a
    wait outside 0..threading.TIMEOUT_MAX, and a timer that fell due (a wait of 0 included)
    but was cancelled or left unfired, fail the test, since a real timer would have fired. A
    test that means a timer to be due at once fires it with advance(0)."""

    made: ClassVar[list[FakeClock]] = []

    def __init__(self, now: float = 1000.0) -> None:
        import threading

        self.now = now
        self.timers: list[FakeTimer] = []
        self.problems: list[str] = []
        self.no_thread = False
        self._lock = threading.Lock()
        FakeClock.made.append(self)

    def __call__(self) -> float:
        return self.now

    def start(self, delay: float, action: Callable[[], None]) -> FakeTimer:
        import threading

        if not 0.0 <= delay <= threading.TIMEOUT_MAX:
            # Recorded as well as raised: the watchdog's caller may swallow the error.
            self.problems.append(f"a timer was started with a wait of {delay}")
            raise AssertionError(f"timer wait out of range: {delay}")
        if self.no_thread:
            raise RuntimeError("can't start new thread")
        with self._lock:
            timer = FakeTimer(self, delay, self.now + delay, action)
            self.timers.append(timer)
        return timer

    def advance(self, seconds: float) -> None:
        with self._lock:
            target = self.now + seconds
        while True:
            with self._lock:
                due = [t for t in self.timers if not (t.cancelled or t.fired) and t.due <= target]
                if not due:
                    self.now = max(self.now, target)
                    return
                timer = min(due, key=lambda t: t.due)  # the first started of equals
                timer.fired = True
                self.now = max(self.now, timer.due)
            timer.action()

    def verify(self) -> list[str]:
        """What this clock saw that a real timer would not allow."""
        left = [
            f"a timer due at {t.due} never fired"
            for t in self.timers
            if not (t.fired or t.cancelled) and t.due <= self.now
        ]
        return [*self.problems, *left]


@pytest.fixture(autouse=True)
def fake_clocks_are_honest() -> Iterator[None]:
    """Fails a test whose FakeClock saw a wait out of range or a due timer left unfired."""
    FakeClock.made.clear()
    yield
    problems = [p for clock in FakeClock.made for p in clock.verify()]
    FakeClock.made.clear()
    assert not problems, f"FakeClock: {problems}"
