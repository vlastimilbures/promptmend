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
- if truststore cannot load or build a context here (any exception: an ImportError, an
  OSError from its ctypes bindings, anything else): True again, httpx's certifi bundle, the
  behaviour before #314. The owner's choice: the call still works, and is still verified.

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
    """A client context verified by the OS trust store, or None when truststore cannot load
    or build one (whatever it raises: the caller then verifies against certifi)."""
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
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


def missing_bundle(variable: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``variable`` (one of CA_VARIABLES) names a path that is not there: a file for
    SSL_CERT_FILE, a folder for SSL_CERT_DIR."""
    environ = os.environ if environ is None else environ
    path = environ.get(variable, "")
    return not (os.path.isfile(path) if variable == "SSL_CERT_FILE" else os.path.isdir(path))


def bundle_is_other_kind(variable: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``variable`` names a folder where a file is wanted, or a file for SSL_CERT_DIR."""
    environ = os.environ if environ is None else environ
    path = environ.get(variable, "")
    return os.path.isdir(path) if variable == "SSL_CERT_FILE" else os.path.isfile(path)


def describe() -> tuple[str, str | None]:
    """What an https call made now is verified against, for doctor: OS_STORE, CA_BUNDLE (with
    the variable that names it) or CERTIFI (truststore could not load here)."""
    variable = ca_variable()
    if variable is not None:
        return CA_BUNDLE, variable
    return (OS_STORE if os_trust_store() is not None else CERTIFI), None
