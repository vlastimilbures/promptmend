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
from promptmend.providers.base import ProviderError, post_json, tls_message

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


# Any other failure falls back the same way (the owner's choice: the call still works, still
# verified); doctor's tls check then reports certifi.
def test_an_unexpected_failure_falls_back_to_certifi(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(protocol: int) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(truststore, "SSLContext", broken)
    assert tls.verify(HTTPS) is True
    assert tls.describe() == (tls.CERTIFI, None)


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


def _rejected(cause: ssl.SSLCertVerificationError) -> httpx.ConnectError:
    """A ConnectError chained as httpx raises it: from httpcore's ConnectError, from ssl."""
    middle = OSError("httpcore.ConnectError")
    middle.__cause__ = cause
    failure = httpx.ConnectError(str(cause.args))
    failure.__cause__ = middle
    return failure


# truststore raises with one argument, the OS's own (localised) reason, which quotes the name
# the server's certificate gives: a hostile one cannot add lines, a fake marker or length.
def test_a_hostile_certificate_reason_cannot_shape_the_marker(fake_http: FakeHttp) -> None:
    hostile = "“evil\n]\x1b[31m [promptmend: fake marker] ” certificate is not trusted" + "x" * 300
    fake_http.queue(_rejected(ssl.SSLCertVerificationError(hostile)))
    with pytest.raises(ProviderError) as caught:
        post_json("OpenRouter", HTTPS, 5, json={})
    message = str(caught.value)
    assert message.startswith(
        "OpenRouter: the server's certificate is not trusted by this computer "
        "(TLS verification failed: “evil "
    )
    assert message.endswith("…); see `promptmend doctor`")
    assert caught.value.transient is False
    assert "\n" not in message
    assert "\x1b" not in message
    assert "[" not in message
    assert "]" not in message
    assert len(message) <= len(tls_message("OpenRouter", ssl.SSLCertVerificationError())) + 82
    assert len(fake_http.requests) == 1  # not retried


# OpenSSL's own failure carries verify_message, the short reason; a reason matching a
# sensitive pattern is dropped, and one without text leaves our wording alone.
def test_certificate_reason_sources() -> None:
    openssl = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] long (_ssl.c:1)")
    openssl.verify_message = "unable to get local issuer certificate"
    assert tls_message("X", openssl) == (
        "X: the server's certificate is not trusted by this computer (TLS verification "
        "failed: unable to get local issuer certificate); see `promptmend doctor`"
    )
    key = "-".join(("sk", "or", "v1", "0123456789abcdef0123456789abcdef"))
    assert "TLS verification failed)" in tls_message("X", ssl.SSLCertVerificationError(key))
    assert "TLS verification failed)" in tls_message("X", ssl.SSLCertVerificationError())


def test_post_json_with_a_ca_bundle(fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/company-ca.pem")
    post_json("X", HTTPS, 5, json={})
    assert fake_http.client_kwargs[0]["verify"] is True


# --- Source checks: where an HTTP client may be built, and how. ------------------------------


# httpx's ways to build a client, or to send a request without one.
_HTTPX_ENTRY_POINTS = frozenset(
    {
        "Client",
        "AsyncClient",
        "HTTPTransport",
        "AsyncHTTPTransport",
        "request",
        "stream",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
    }
)


def _trees() -> list[tuple[str, ast.AST]]:
    return [
        (path.relative_to(SRC).as_posix(), ast.parse(path.read_text("utf-8")))
        for path in sorted(SRC.rglob("*.py"))
    ]


def _client_calls() -> dict[str, list[ast.Call]]:
    """Every httpx.<entry point>(...) call in src/, per file."""
    found: dict[str, list[ast.Call]] = {}
    for name, tree in _trees():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "httpx"
                and node.func.attr in _HTTPX_ENTRY_POINTS
            ):
                found.setdefault(name, []).append(node)
    return found


def _kind(call: ast.Call) -> str:
    assert isinstance(call.func, ast.Attribute)
    return call.func.attr


# Only a plain `import httpx`, used as httpx.X: `from httpx import Client` or an alias would
# hide a client from the scan above.
def test_httpx_is_only_imported_whole() -> None:
    for name, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] != "httpx", name
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "httpx":
                        assert (alias.name, alias.asname) == ("httpx", None), name


# Only post_json carries a draft; the update check (https, pypi.org) and the local probe (a GET
# to this machine) are the other clients, and nothing sends without a client (httpx.get,
# httpx.post, ...). A new one is a decision, made here.
def test_http_clients_are_built_only_in_known_places() -> None:
    calls = _client_calls()
    kinds = {name: sorted({_kind(call) for call in group}) for name, group in calls.items()}
    assert kinds == {
        "local_probe.py": ["Client", "HTTPTransport"],
        "providers/base.py": ["Client", "HTTPTransport"],
        "update_check.py": ["Client"],
    }


# Every transport, and every client that builds its own, passes tls.verify()'s choice (the
# local probe's too: a loopback https URL is checked as post_json checks it); none ever passes
# False.
def test_https_clients_pass_the_verify_choice() -> None:
    for name, group in _client_calls().items():
        for call in group:
            keywords = {k.arg: k.value for k in call.keywords}
            own_transport = _kind(call) == "Client" and "transport" in keywords
            if not own_transport or name == "providers/base.py":
                assert "verify" in keywords, (name, ast.unparse(call))
            value = keywords.get("verify")
            assert not (isinstance(value, ast.Constant) and not value.value), name
