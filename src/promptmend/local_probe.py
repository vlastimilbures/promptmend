"""Whether a local model server answers: one short GET to a loopback Ollama or LM
Studio, for doctor and the interface only, never on the trigger path
(tests/test_trigger_contract.py). Nothing is sent but the request line; any HTTP answer, an
error status included, means the server is there."""

from __future__ import annotations

from .providers.base import is_loopback

# Seconds per phase. A loopback server answers at once. Where nothing listens, macOS and Linux
# refuse at once, but Windows retries a refused connect for about 2 s, so there a probe of
# 127.0.0.1 waits out this timeout and one of localhost (::1 first) twice (#365).
TIMEOUT = 0.5


def answers(url: str) -> bool | None:
    """True when something answers at ``url``, False when nothing does, None when the URL is
    not on this machine (never asked: doctor does not reach out to another host)."""
    if not is_loopback(url):
        return None
    try:
        _get(url)
    except Exception:
        return False
    return True


def _get(url: str) -> None:
    import httpx

    from .tls import verify

    # Its own transport, as post_json gives a loopback URL: no env or system proxy. An https
    # URL is checked against the OS trust store, as post_json checks it (#314).
    transport = httpx.HTTPTransport(verify=verify(url))
    with httpx.Client(transport=transport, timeout=TIMEOUT) as client:
        client.get(url)
