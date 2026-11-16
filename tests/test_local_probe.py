"""local_probe (#220): one short GET to a loopback Ollama or LM Studio, offline through
fake_http; conftest stubs the GET in every other test. The #365 timings below use the real
network stack, on loopback only."""

from __future__ import annotations

import http.server
import socket
import statistics
import sys
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial

import httpx
import pytest
from conftest import FakeHttp

from promptmend import local_probe
from promptmend.providers import base

# Kept at import, before conftest's autouse stub replaces it in each test.
REAL_GET = local_probe._get


@pytest.fixture
def real_get(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_probe, "_get", REAL_GET)


@pytest.mark.usefixtures("real_get")
def test_any_answer_counts(fake_http: FakeHttp) -> None:
    fake_http.reply({"models": []})
    assert local_probe.answers("http://localhost:11434/api/tags") is True
    fake_http.reply(None, status_code=404)
    assert local_probe.answers("http://127.0.0.1:1234/v1/models") is True
    assert [c["url"] for c in fake_http.calls] == [
        "http://localhost:11434/api/tags",
        "http://127.0.0.1:1234/v1/models",
    ]
    assert fake_http.client_kwargs[0]["timeout"] == local_probe.TIMEOUT
    assert isinstance(fake_http.client_kwargs[0]["transport"], httpx.HTTPTransport)


@pytest.mark.usefixtures("real_get")
def test_no_answer(fake_http: FakeHttp) -> None:
    fake_http.exc = httpx.ConnectError("refused")
    assert local_probe.answers("http://localhost:11434/api/tags") is False


@pytest.mark.usefixtures("real_get")
def test_another_host_is_never_asked(fake_http: FakeHttp) -> None:
    assert local_probe.answers("https://ollama.example.com/api/tags") is None
    assert fake_http.calls == []


# --- Timings on the real network stack, loopback only (#365) ---------------------------------
# These use the real httpx.Client (no fake_http) and never leave this machine: every URL is
# 127.0.0.1 or localhost, on a port this test bound itself. A bounded case takes the median of
# TRIES and fails above its bound with every measured time in the message.
#
# 127.0.0.1 with a server listening is fast everywhere. The rest is fast on macOS and Linux
# only: Windows retries a refused loopback connect for about 2 s and tries ``localhost`` as ::1
# first (CI run 43870172511: a closed 127.0.0.1 probe 512-660 ms, a closed localhost one
# 1010-1152 ms, a post_json to localhost 2-4 s). There the probe is pinned loosely to its own
# TIMEOUT, and post_json, which has no short timeout, is checked for its result only.

TRIES = 3
FAST_MS = 300.0
_WINDOWS = sys.platform == "win32"
_TIMEOUT_MS = local_probe.TIMEOUT * 1000
_MARGIN_MS = 1000.0


def _bound(fast_everywhere: bool, windows: float | None) -> float | None:
    """FAST_MS, or on Windows ``windows`` (None: the result only) unless fast everywhere."""
    return windows if _WINDOWS and not fast_everywhere else FAST_MS


def _free_port() -> int:
    """A port nothing listens on: bound on 127.0.0.1 by the OS, then closed."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


class _Timings:
    """Each case's times and bound, checked together so a failure reports them all."""

    def __init__(self) -> None:
        self.cases: dict[str, tuple[list[float], float | None]] = {}

    def run(
        self, name: str, call: Callable[[], object], expected: object, bound: float | None
    ) -> None:
        """Run ``call`` (TRIES times when bounded, else once) and check each result."""
        times = []
        for _ in range(TRIES if bound is not None else 1):
            started = time.perf_counter()
            result = call()
            times.append((time.perf_counter() - started) * 1000)
            assert result == expected, f"{name}: got {result!r}, expected {expected!r}"
        self.cases[name] = (times, bound)

    def check(self) -> None:
        report = "; ".join(
            f"{name}: median {statistics.median(ms):.0f} ms of {[round(t) for t in ms]}"
            f" (bound {'none' if bound is None else f'{bound:.0f} ms'})"
            for name, (ms, bound) in self.cases.items()
        )
        print(f"\n[#365 timings, {sys.platform}] {report}")
        slow = [
            name
            for name, (ms, bound) in self.cases.items()
            if bound is not None and statistics.median(ms) > bound
        ]
        assert not slow, f"over the bound on {sys.platform}: {slow}; {report}"


def _refused_post(url: str) -> str:
    """post_json to ``url``, which nothing answers: 'refused' on a ProviderError."""
    try:
        base.post_json("Ollama", url, 5.0, json={"model": "m"})
    except base.ProviderError:
        return "refused"
    return "answered"


class _Answer(http.server.BaseHTTPRequestHandler):
    """Any GET or POST gets a small JSON answer."""

    def _reply(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b'{"models": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def ipv4_server() -> Iterator[int]:
    """A server on 127.0.0.1 only, as Ollama listens by default; yields its port."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Answer)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.usefixtures("real_get")
def test_closed_port_probe_ends_within_its_timeout() -> None:
    port = _free_port()
    timings = _Timings()
    # On Windows a localhost probe waits out its timeout twice: ::1, then 127.0.0.1.
    for host, attempts in (("localhost", 2), ("127.0.0.1", 1)):
        url = f"http://{host}:{port}"
        probe = partial(local_probe.answers, f"{url}/api/tags")
        bound = _bound(False, attempts * _TIMEOUT_MS + _MARGIN_MS)
        timings.run(f"probe {host} closed", probe, False, bound)
        # A trigger's POST when Ollama is not running (a refused loopback is not retried).
        refused = partial(_refused_post, f"{url}/api/chat")
        timings.run(f"post_json {host} closed", refused, "refused", _bound(False, None))
    timings.check()


@pytest.mark.usefixtures("real_get")
def test_ipv4_only_server_answers_fast_on_127_0_0_1(ipv4_server: int) -> None:
    timings = _Timings()
    for host in ("127.0.0.1", "localhost"):
        url = f"http://{host}:{ipv4_server}"
        ipv4 = host == "127.0.0.1"
        probe = partial(local_probe.answers, f"{url}/api/tags")
        timings.run(f"probe {host}", probe, True, _bound(ipv4, _TIMEOUT_MS + _MARGIN_MS))
        # The trigger path's own POST, as a provider sends a draft to a local Ollama: on
        # Windows ``localhost`` pays for a refused ::1 first, hence the 127.0.0.1 defaults.
        post = partial(base.post_json, "Ollama", f"{url}/api/chat", 5.0, json={"model": "m"})
        timings.run(f"post_json {host}", post, {"models": []}, _bound(ipv4, None))
    timings.check()
