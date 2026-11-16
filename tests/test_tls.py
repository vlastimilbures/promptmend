"""TLS verification against the operating system's trust store (#314): which ``verify=`` each
https client gets, that a CA bundle in SSL_CERT_FILE/SSL_CERT_DIR keeps today's meaning, that a
platform where truststore cannot load falls back to httpx's certifi bundle, and that nothing
ever turns verification off. Offline: building a context reaches no server."""

from __future__ import annotations

import ast
import ssl
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import truststore

from promptmend import tls
from promptmend.providers.base import post_json

if TYPE_CHECKING:
    from conftest import FakeHttp

SRC = Path(__file__).resolve().parents[1] / "src" / "promptmend"
HTTPS = "https://openrouter.ai/api/v1/chat/completions"


def _is_os_store(context: object) -> bool:
    # type(), not isinstance(): truststore.SSLContext passes as an ssl.SSLContext on purpose.
    return type(context) is truststore.SSLContext


# An https URL gets a fresh context verified by the OS trust store: hostname and chain checked.
@pytest.mark.parametrize("url", [HTTPS, "HTTPS://pypi.org/pypi/x/json", "https://localhost:8443"])
def test_https_uses_the_os_trust_store(url: str) -> None:
    context = tls.verify(url)
    assert _is_os_store(context)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert tls.verify(url) is not context  # nothing shared between calls


# Plain http (a local server) never needs a context, so truststore is not even tried.
@pytest.mark.parametrize("url", ["http://localhost:11434/api/chat", "http://192.168.1.20:1234"])
def test_plain_http_keeps_the_httpx_default(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tls, "os_trust_store", pytest.fail)
    assert tls.verify(url) is True


def test_a_malformed_url_keeps_the_httpx_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tls, "os_trust_store", pytest.fail)
    assert tls.verify("https://[::1") is True


# A CA bundle the user named keeps its meaning: verify=True, which httpx 0.28 turns into a
# context loaded from that file or folder (and never from certifi or the OS store).
@pytest.mark.parametrize("name", tls.CA_VARIABLES)
def test_a_named_ca_bundle_keeps_todays_behaviour(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tls, "os_trust_store", pytest.fail)
    monkeypatch.setenv(name, str(tmp_path / "missing"))
    assert tls.verify(HTTPS) is True
    # httpx itself reads the variable for verify=True: a missing file fails, and a folder
    # (read lazily, per handshake) replaces certifi's bundle, so no root is preloaded.
    if name == "SSL_CERT_FILE":
        with pytest.raises(FileNotFoundError):
            httpx.create_ssl_context(verify=True)
    else:
        assert httpx.create_ssl_context(verify=True).cert_store_stats()["x509_ca"] == 0
        monkeypatch.delenv(name)
        assert httpx.create_ssl_context(verify=True).cert_store_stats()["x509_ca"] > 0


# An empty variable is unset for httpx too (it tests truthiness), so the OS store applies.
@pytest.mark.parametrize("name", tls.CA_VARIABLES)
def test_an_empty_ca_variable_is_ignored(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name, "")
    assert _is_os_store(tls.verify(HTTPS))


# Where truststore cannot load (an ImportError, or the OS library missing behind its ctypes
# bindings), the call is verified as before, against certifi: never unverified.
def test_no_truststore_falls_back_to_certifi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "truststore", None)  # makes `import truststore` fail
    assert tls.os_trust_store() is None
    assert tls.verify(HTTPS) is True


def test_a_failing_os_library_falls_back_to_certifi(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(protocol: int) -> None:
        raise OSError("Security framework not found")

    monkeypatch.setattr(truststore, "SSLContext", broken)
    assert tls.verify(HTTPS) is True


# Any other failure is a bug and is not swallowed into a silent downgrade.
def test_an_unexpected_failure_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(protocol: int) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(truststore, "SSLContext", broken)
    with pytest.raises(RuntimeError):
        tls.verify(HTTPS)


# post_json hands the same choice to httpx: the client for a remote https call, the direct
# transport for a loopback one; plain http gets httpx's default.
@pytest.mark.parametrize(
    ("url", "os_store", "direct"),
    [
        (HTTPS, True, False),
        ("https://localhost:8443/v1/chat/completions", True, True),
        ("http://localhost:11434/api/chat", False, True),
        ("http://192.168.1.20:11434/api/chat", False, False),
    ],
)
def test_post_json_passes_the_choice(
    fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch, url: str, os_store: bool, direct: bool
) -> None:
    built: list[object] = []
    real_transport = httpx.HTTPTransport

    def transport(**kwargs: object) -> httpx.HTTPTransport:
        built.append(kwargs["verify"])
        return real_transport(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    post_json("X", url, 5, json={})
    (kwargs,) = fake_http.client_kwargs
    assert _is_os_store(kwargs["verify"]) is os_store
    assert os_store or kwargs["verify"] is True
    assert built == ([kwargs["verify"]] if direct else [])


def test_post_json_with_a_ca_bundle(fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/company-ca.pem")
    post_json("X", HTTPS, 5, json={})
    assert fake_http.client_kwargs[0]["verify"] is True


# --- Source checks: where an HTTP client may be built, and how. ------------------------------


def _client_calls() -> dict[str, list[ast.Call]]:
    """Every httpx.Client(...) / httpx.HTTPTransport(...) call in src/, per file."""
    found: dict[str, list[ast.Call]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "httpx"
                and node.func.attr in {"Client", "HTTPTransport", "AsyncClient"}
            ):
                found.setdefault(path.relative_to(SRC).as_posix(), []).append(node)
    return found


# Only post_json carries a draft; the update check (https, pypi.org) and the local probe (an
# http GET to this machine) are the other clients. A new one is a decision, made here.
def test_http_clients_are_built_only_in_known_places() -> None:
    assert sorted(_client_calls()) == ["local_probe.py", "providers/base.py", "update_check.py"]


# The two clients that can talk https pass tls.verify()'s choice; none ever passes False.
def test_https_clients_pass_the_verify_choice() -> None:
    calls = _client_calls()
    for name in ("providers/base.py", "update_check.py"):
        assert all(any(k.arg == "verify" for k in call.keywords) for call in calls[name]), name
    for call in (call for group in calls.values() for call in group):
        for keyword in call.keywords:
            if keyword.arg == "verify":
                assert not (isinstance(keyword.value, ast.Constant) and not keyword.value.value)
