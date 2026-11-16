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
# 127.0.0.1 or localhost, on a port this test bound itself. Each case takes the median of
# TRIES and fails above BOUND_MS with every measured time in the message, so a slow platform
# (Windows: a refused loopback connect retried, ``localhost`` tried as ::1 first) reports its
# numbers.

TRIES = 3
BOUND_MS = 0.0  # measurement only: always fail to print the times


def _free_port() -> int:
    """A port nothing listens on: bound on 127.0.0.1 by the OS, then closed."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def _times_ms(call: Callable[[], object], expected: object) -> list[float]:
    """Run ``call`` TRIES times, check each result, and return the milliseconds each took."""
    times = []
    for _ in range(TRIES):
        started = time.perf_counter()
        result = call()
        times.append((time.perf_counter() - started) * 1000)
        assert result == expected, f"got {result!r}, expected {expected!r}"
    return times


def _refused_post(url: str) -> str:
    """post_json to ``url``, which nothing answers: 'refused' on a ProviderError."""
    try:
        base.post_json("Ollama", url, 5.0, json={"model": "m"})
    except base.ProviderError:
        return "refused"
    return "answered"


def _check_bound(measured: dict[str, list[float]]) -> None:
    report = "; ".join(
        f"{name}: median {statistics.median(ms):.0f} ms of {[round(t) for t in ms]}"
        for name, ms in measured.items()
    )
    print(f"\n[#365 timings, {sys.platform}] {report}")
    slow = [name for name, ms in measured.items() if statistics.median(ms) > BOUND_MS]
    assert not slow, f"slower than {BOUND_MS:.0f} ms on {sys.platform}: {slow}; {report}"


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
def test_closed_port_fails_fast() -> None:
    port = _free_port()
    measured: dict[str, list[float]] = {}
    for host in ("localhost", "127.0.0.1"):
        url = f"http://{host}:{port}"
        measured[f"probe {host} closed"] = _times_ms(
            partial(local_probe.answers, f"{url}/api/tags"), False
        )
        # A trigger's POST when Ollama is not running (a refused loopback is not retried).
        measured[f"post_json {host} closed"] = _times_ms(
            partial(_refused_post, f"{url}/api/chat"), "refused"
        )
    _check_bound(measured)


@pytest.mark.usefixtures("real_get")
def test_ipv4_only_server_answers_fast(ipv4_server: int) -> None:
    measured: dict[str, list[float]] = {}
    for host in ("localhost", "127.0.0.1"):
        url = f"http://{host}:{ipv4_server}"
        measured[f"probe {host}"] = _times_ms(partial(local_probe.answers, f"{url}/api/tags"), True)
        # The trigger path's own POST (providers.base.post_json), as a provider sends a draft
        # to a local Ollama: does ``localhost`` pay for a refused ::1 first?
        post = partial(base.post_json, "Ollama", f"{url}/api/chat", 5.0, json={"model": "m"})
        measured[f"post_json {host}"] = _times_ms(post, {"models": []})
    _check_bound(measured)
