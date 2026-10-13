from __future__ import annotations

import ipaddress
import json as jsonlib
import math
import re
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from ..redaction import safe_repr, scan
from .usage import Meter

if TYPE_CHECKING:
    import httpx


class ProviderError(RuntimeError):
    """Raised when a provider cannot produce a result.

    ``status`` is the HTTP status, or the code of an error reported inside a 200 response,
    when there is one. ``transient`` marks a failure the same call may not hit again: a
    timeout, a dropped connection, a rate limit or an unavailable provider.
    """

    def __init__(self, message: str, *, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


class Provider(Protocol):
    def generate(self, prompt: str, system_prompt: str) -> str: ...


def chat_messages(system_prompt: str, prompt: str) -> list[dict[str, str]]:
    """System + user message list shared by the Ollama and OpenAI-compatible chat APIs."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]


def is_loopback(url: str) -> bool:
    """Whether the URL's host is this machine: localhost, 127.0.0.0/8 or ::1."""
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Statuses the same call may not hit again; 529 is Anthropic's "overloaded".
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
# What the user can do about a status, shown before the provider's own reason.
_HINTS = {
    400: "bad request (check the model and settings)",
    401: "check the API key",
    402: "out of credits",
    403: "access denied",
    404: "not found (check the model and base URL)",
    429: "rate limited, try again shortly",
}
# The provider's reason is pasted into the user's editor, so it is kept to one short line.
SERVER_MESSAGE_MAX = 160


def error_detail(body: object) -> str:
    """The provider's own reason for an error, cleaned to one short printable line.

    Reads {"error": {"message": ...}} (OpenAI, OpenRouter, Anthropic) or {"error": "..."}
    (Ollama). Returns "" when there is none, or when it matches a sensitive pattern: some
    providers quote part of the rejected API key.
    """
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str):
        return ""
    # Bounded before the scan, which then sees everything that could be shown.
    words = " ".join(message[: SERVER_MESSAGE_MAX * 4].split())
    # Drops control, bidi, zero-width and tag characters (Unicode "Other"/"Separator").
    text = "".join(char for char in words if char.isprintable())
    if scan(text):
        return ""
    if len(text) > SERVER_MESSAGE_MAX:
        return text[: SERVER_MESSAGE_MAX - 1] + "…"
    return text


def http_error(label: str, status: int, body: object) -> ProviderError:
    """A non-2xx response as a ProviderError: the status, a hint and the provider's reason."""
    hint = _HINTS.get(status) or ("provider unavailable, try again" if status >= 500 else "")
    detail = "; ".join(part for part in (hint, error_detail(body)) if part)
    return ProviderError(
        f"{label} returned HTTP {status}" + (f": {detail}" if detail else ""),
        status=status,
        transient=status in TRANSIENT_STATUSES,
    )


def body_error(label: str, body: object) -> ProviderError | None:
    """An error object inside a 200 response, or None. OpenRouter reports an error raised
    after generation started this way, at the top level or inside the choice."""
    error = body.get("error") if isinstance(body, dict) else None
    if not error:
        return None
    code = error.get("code") if isinstance(error, dict) else None
    status = code if isinstance(code, int) and not isinstance(code, bool) else None
    where = f" (code {status})" if status is not None else ""
    return ProviderError(
        f"{label} returned an error{where}: {error_detail(body) or 'no details'}",
        status=status,
        transient=status in TRANSIENT_STATUSES,
    )


# Failures retried once: a rate limit or an unavailable upstream, which a pinned endpoint
# (OPENROUTER_PRO_PROVIDER) cannot route around. 500 is not: it is usually the request itself.
RETRY_STATUSES = frozenset({429, 502, 503, 504, 529})
# Wait before the retry, unless the server's Retry-After says otherwise.
RETRY_DELAY = 1.0
# A server asking for a longer wait is not retried: Espanso is blocked while the CLI waits.
RETRY_AFTER_MAX = 3.0
# The retry needs at least this much of the time limit left.
MIN_RETRY_BUDGET = 1.0
# A host that does not accept the connection within this many seconds is not going to.
CONNECT_TIMEOUT_MAX = 10.0
# Patched by the tests, so a retry does not wait.
_sleep = time.sleep


class _Retry(Exception):
    """A failed attempt that may succeed if repeated after ``delay`` seconds."""

    def __init__(self, error: ProviderError, delay: float = RETRY_DELAY):
        super().__init__(str(error))
        self.error = error
        self.delay = delay


def _retry_delay(retry_after: str | None) -> float | None:
    """Seconds to wait before retrying, or None when the server asks for longer than
    RETRY_AFTER_MAX or gives a date: a sooner retry would only be refused again."""
    if retry_after is None:
        return RETRY_DELAY
    try:
        seconds = float(retry_after)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds > RETRY_AFTER_MAX:
        return None
    return max(seconds, 0.0)


def _encode_headers(label: str, headers: Mapping[str, str] | None) -> httpx.Headers:
    # httpx is imported only once a request is made: every trigger starts a fresh process,
    # and httpx is the largest import on its path (#223).
    import httpx

    try:
        # httpx encodes header values as ASCII; a key pasted with a smart quote or an accented
        # letter fails here, before anything is sent. The error quotes the key, so it is dropped.
        return httpx.Headers(headers)
    except UnicodeEncodeError:
        raise ProviderError(
            f"{label} request failed: invalid header value (check the API key)"
        ) from None


def post_json(
    label: str,
    url: str,
    timeout: float,
    *,
    json: object = None,
    headers: Mapping[str, str] | None = None,
    meter: Meter | None = None,
) -> dict[str, object]:
    """POST and return the parsed JSON body; every failure is a ProviderError.

    ``timeout`` bounds the whole call, retry included. A rate limit (429), an unavailable
    upstream (502/503/504/529, or such an error inside a 200 reply) or a refused connection
    to another machine is tried once more when at least MIN_RETRY_BUDGET seconds remain. A
    timeout is never retried: the server may still be generating, and billing, the first reply.

    A loopback URL is reached directly, ignoring HTTP(S)_PROXY/ALL_PROXY and the
    macOS/Windows system proxy: a call to this machine has no reason to go anywhere else.
    httpx applies those only to a client without its own transport, so giving it one keeps
    the rest of the environment (SSL_CERT_FILE for a local https server) in effect. Other
    URLs keep using the proxy, which corporate networks need.

    With a ``meter``, every attempt, failed ones included, reports one AttemptUsage to its
    observer before this returns or raises, so usage the API charged for is kept even when
    the caller then fails to finalise the content. The observer's own time is not charged
    to ``timeout``, so a slow observer cannot cost the call its retry.
    """
    encoded = _encode_headers(label, headers)
    deadline = time.monotonic() + timeout
    first = _Outcome()
    try:
        return _attempt(label, url, timeout, timeout, json, encoded, meter, first)
    except _Retry as retry:
        budget = deadline + first.observer_seconds - time.monotonic() - retry.delay
        if budget < MIN_RETRY_BUDGET:
            raise retry.error from retry.error.__cause__
        _sleep(retry.delay)
    try:
        return _attempt(label, url, timeout, budget, json, encoded, meter, _Outcome(2))
    except _Retry as retry:
        raise retry.error from retry.error.__cause__


class _Outcome:
    """What one attempt saw, filled in by _exchange() as it goes, for the usage record, and
    how long the observer then took with it."""

    def __init__(self, number: int = 1) -> None:
        self.number = number
        self.status: int | None = None
        self.error_kind: str | None = None
        self.body: object = None
        self.observer_seconds = 0.0


def _attempt(
    label: str,
    url: str,
    timeout: float,
    budget: float,
    json: object,
    headers: httpx.Headers,
    meter: Meter | None = None,
    outcome: _Outcome | None = None,
) -> dict[str, object]:
    """One request, reported to ``meter`` (if any) however it ends."""
    outcome = outcome or _Outcome()
    started = time.monotonic()
    try:
        return _exchange(label, url, timeout, budget, json, headers, outcome)
    except BaseException:
        outcome.error_kind = outcome.error_kind or "unexpected"
        raise
    finally:
        if meter is not None:
            ended = time.monotonic()
            meter.record(
                loopback=is_loopback(url),
                attempt=outcome.number,
                status=outcome.status,
                error_kind=outcome.error_kind,
                body=outcome.body,
                latency=ended - started,
            )
            outcome.observer_seconds = time.monotonic() - ended


def _exchange(
    label: str,
    url: str,
    timeout: float,
    budget: float,
    json: object,
    headers: httpx.Headers,
    outcome: _Outcome,
) -> dict[str, object]:
    """One request, given ``budget`` seconds in total. httpx's own timeouts apply to each
    network operation, and its read timer restarts with every chunk, so a server that
    trickles bytes is cut off here once the budget is spent."""
    import httpx

    transport = httpx.HTTPTransport() if is_loopback(url) else None
    limits = httpx.Timeout(budget, connect=min(budget, CONNECT_TIMEOUT_MAX))
    stop_at = time.monotonic() + budget
    timed_out = ProviderError(f"{label} timed out after {timeout}s", transient=True)
    content = bytearray()
    try:
        with (
            httpx.Client(timeout=limits, transport=transport) as client,
            client.stream("POST", url, json=json, headers=headers) as response,
        ):
            outcome.status = response.status_code
            for chunk in response.iter_bytes():
                content += chunk
                if time.monotonic() > stop_at:
                    outcome.error_kind = "timeout"
                    raise timed_out
    except httpx.TimeoutException as exc:
        outcome.error_kind = "timeout"
        raise timed_out from exc
    except httpx.InvalidURL as exc:
        outcome.error_kind = "invalid_url"
        raise ProviderError(f"{label} base URL invalid: {safe_repr(str(exc))}") from exc
    except httpx.LocalProtocolError as exc:
        # Raised for a header value with characters HTTP forbids; its message quotes the
        # value, which is the API key, so it is not repeated.
        outcome.error_kind = "invalid_header"
        raise ProviderError(
            f"{label} request failed: invalid header value (check the API key)"
        ) from exc
    except httpx.ConnectError as exc:
        outcome.error_kind = "refused"
        refused = ProviderError(f"{label} request failed: {exc}", transient=True)
        refused.__cause__ = exc  # kept when post_json re-raises it after the retry
        # A local server that refuses the connection is not running; a second try a second
        # later only delays the marker.
        if is_loopback(url):
            raise refused from exc
        raise _Retry(refused) from exc
    except httpx.HTTPError as exc:
        outcome.error_kind = "transport"
        raise ProviderError(f"{label} request failed: {exc}") from exc

    try:
        body: object = jsonlib.loads(content)
    except ValueError:
        body = None
    outcome.body = body
    if not response.is_success:
        outcome.error_kind = "non_2xx"
        error = http_error(label, response.status_code, body)
        delay = _retry_delay(response.headers.get("retry-after"))
        if response.status_code in RETRY_STATUSES and delay is not None:
            raise _Retry(error, delay)
        raise error
    if body is None:
        outcome.error_kind = "invalid_json"
        raise ProviderError(f"{label} returned invalid JSON")
    if failure := body_error(label, body):
        outcome.error_kind = "body_error"
        if failure.status in RETRY_STATUSES:
            raise _Retry(failure)
        raise failure
    return body  # type: ignore[return-value]


# Models such as the qwen3 family open a reply with their reasoning; a later <think> is part
# of the answer (a prompt about reasoning tags), so only a leading block is reasoning. The lazy
# match ends at the first closing tag, or runs to the end of a block the model never closed
# (cut off at the token cap). A leading stray closing tag is matched on its own.
_LEADING_THINK = re.compile(
    r"\A\s*(?:<think>.*?(?:</think>|\Z)|</think>)", re.DOTALL | re.IGNORECASE
)


def strip_thinking(text: str) -> str:
    """Remove the reasoning a model such as the qwen3 family emits before its answer.

    Strips leading <think>...</think> blocks, a leading block left open when a response is
    truncated (dropped to the end), and a leading stray closing tag. A <think> later in the
    text is answer text and is kept. Leading and trailing whitespace is normalized.
    """
    while match := _LEADING_THINK.match(text):
        text = text[match.end() :]
    return text.strip()


# Appended to a rewrite the model stopped at its output cap, so a cut-off prompt is never
# pasted as if it were complete. Provider-neutral: the cap is not always one --max-tokens sets.
TRUNCATED_NOTE = "\n\n[promptmend: the reply hit the model's output limit and is cut off]"
# Appended to a rewrite a content filter or a refusal stopped.
FILTERED_NOTE = (
    "\n\n[promptmend: the model stopped early ({reason}); the rewrite may be incomplete]"
)
_STOP_NOTE = re.compile(
    f"(?:{re.escape(TRUNCATED_NOTE)}"
    f"|{re.escape(FILTERED_NOTE).replace(re.escape('{reason}'), '[a-z_]+')})\\Z"
)


def split_stop_note(result: str) -> tuple[str, str]:
    """(rewrite, note) for a result that may end with a stop note above; note is "" when the
    reply ended normally, else the bare marker. The CLI and the bench both split with it."""
    match = _STOP_NOTE.search(result)
    if match is None:
        return result, ""
    return result[: match.start()], match.group().lstrip("\n")


# Stop reasons (OpenRouter finish_reason, Anthropic stop_reason, Ollama done_reason) for a
# reply that did not end normally.
_TRUNCATING = frozenset({"length", "max_tokens"})
_FAILED = frozenset({"error"})
_FILTERED = frozenset({"content_filter", "refusal"})


def finalize_content(
    content: object, label: str, *, stop_reason: object = None, capped: bool = False
) -> str:
    """Validate a provider's raw response content, strip <think> blocks, require non-empty text.

    ``stop_reason`` is the provider's raw reason for ending the reply:
    - "error": generation failed part way, so no partial text is pasted.
    - "length"/"max_tokens": the token cap. With no text, a reasoning model spent the whole
      budget thinking, which only a bigger cap fixes; otherwise TRUNCATED_NOTE is appended.
      ``capped``: the request sent the cap that --max-tokens sets, so the error suggests it.
    - "content_filter"/"refusal": with no text the request was declined; otherwise
      FILTERED_NOTE is appended.
    """
    reason = stop_reason if isinstance(stop_reason, str) else None
    if reason in _FAILED:
        raise ProviderError(f"{label} stopped with an error before finishing", transient=True)
    result = strip_thinking(content) if isinstance(content, str) else ""
    if reason in _TRUNCATING and not result:
        hint = "; raise --max-tokens" if capped else ""
        raise ProviderError(f"{label} used its whole output limit before writing any text{hint}")
    if reason in _FILTERED and not result:
        raise ProviderError(f"{label} declined the request ({reason})")
    if not isinstance(content, str):
        raise ProviderError(f"{label} returned no text content")
    if not result:
        raise ProviderError(f"{label} returned empty content")
    if reason in _TRUNCATING:
        return result + TRUNCATED_NOTE
    if reason in _FILTERED:
        return result + FILTERED_NOTE.format(reason=reason)
    return result
