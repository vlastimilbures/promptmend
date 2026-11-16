"""How an https call verifies the server's certificate (#314): against the operating system's
trust store, through truststore, so a root that IT installed there (a corporate TLS-inspection
proxy, a company CA) is trusted without pointing SSL_CERT_FILE at a bundle.

``verify(url)`` is the ``verify=`` every https client passes to httpx:

- a plain-http URL: True, httpx's default, which no connection then uses;
- SSL_CERT_FILE or SSL_CERT_DIR set (non-empty) in the real environment: True, so httpx
  builds its context from that file or folder exactly as before (httpx 0.28 reads them only
  for ``verify=True``; config.ConfigLayers never takes them from a settings file);
- otherwise a ``truststore.SSLContext``: the macOS Security framework, the Windows CryptoAPI,
  or OpenSSL's system paths on Linux;
- if truststore cannot load on this platform (ImportError, OSError from its ctypes
  bindings): True again, httpx's certifi bundle, today's behaviour.

Every branch verifies: the result is never False, nor a context with verification off. The
caller never learns which branch it got, so nothing is printed on the trigger path; doctor's
``tls`` check reports it (``describe()``). truststore is imported only here, only for an
https URL.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import ssl

# The variables httpx (and OpenSSL) read for a CA bundle; set, they keep their meaning.
CA_VARIABLES = ("SSL_CERT_FILE", "SSL_CERT_DIR")


def _https(url: str) -> bool:
    try:
        return urlsplit(url).scheme.lower() == "https"
    except ValueError:  # a malformed URL: httpx reports it, no context is needed
        return False


def os_trust_store() -> ssl.SSLContext | None:
    """A client context verified by the OS trust store, or None when truststore cannot load."""
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except (ImportError, OSError):
        return None


def ca_variable(environ: Mapping[str, str] | None = None) -> str | None:
    """The first of CA_VARIABLES set (non-empty) in ``environ`` (os.environ by default)."""
    environ = os.environ if environ is None else environ
    return next((name for name in CA_VARIABLES if environ.get(name)), None)


def verify(url: str) -> ssl.SSLContext | Literal[True]:
    """The ``verify=`` for an httpx client that talks to ``url`` (see the module docstring)."""
    if not _https(url) or ca_variable() is not None:
        return True
    context = os_trust_store()
    return True if context is None else context


# What describe() reports an https call is verified against.
OS_STORE, CA_BUNDLE, CERTIFI = "os_trust_store", "ca_bundle", "certifi"


def describe() -> tuple[str, str | None]:
    """What an https call made now is verified against, for doctor: OS_STORE, CA_BUNDLE (with
    the variable that names it) or CERTIFI (truststore could not load here)."""
    variable = ca_variable()
    if variable is not None:
        return CA_BUNDLE, variable
    return (OS_STORE if os_trust_store() is not None else CERTIFI), None
