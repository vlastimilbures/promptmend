"""Whether a local model server answers: one short GET to a loopback Ollama or LM
Studio, for doctor and the interface only, never on the trigger path
(tests/test_trigger_contract.py). Nothing is sent but the request line; any HTTP answer, an
error status included, means the server is there."""

from __future__ import annotations

from .providers.base import is_loopback

# Seconds per phase: a loopback server answers at once, and nothing listening refuses at once.
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

    # Its own transport, as post_json gives a loopback URL: no env or system proxy.
    with httpx.Client(transport=httpx.HTTPTransport(), timeout=TIMEOUT) as client:
        client.get(url)
