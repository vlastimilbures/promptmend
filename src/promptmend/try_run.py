"""One run of the interface's Try tab (tui/try_pane.py), without the interface: a typed draft
rewritten through pipeline.rewrite() (and with it factory.make_provider() and its gate), against
the set-up smoke test's stub on 127.0.0.1 or against the real provider, and the outcome the tab
shows (the cleaned text, whether it failed, a usage line, the stub's request count).

A stub run gets smoke.stub_settings() (so smoke.PLACEHOLDER_KEY, never the real key), is gated
and refused as the real settings would be, and records nothing. A real run is recorded in the
usage history as `improve` from recorder.TRY_TAB, after its outcome was handed to the screen and
even when showing it failed. The clipboard is never touched.

usable() decides which run the tab starts at: the real provider when it could rewrite (its
key set, or none needed, and PROMPT_LOCAL_ONLY not refusing it), else the dry run against the
stub. Choosing the default calls nothing: the tab still asks before every real call.

It imports neither Typer nor Textual, and the trigger path never imports it (smoke brings
http.server), so it is tested without an app."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from . import pipeline, recorder, sanitize, smoke
from .factory import check_base_url, leaves_machine
from .providers.registry import PROVIDERS

if TYPE_CHECKING:
    from .config import Settings
    from .providers.usage import AttemptUsage


# What a stub run is called wherever the tab names it: it checks the wiring, it never rewrites.
DRY_RUN = "Dry run"
# Above a stub run's result, so its fixed reply is never taken for a rewrite.
DRY_RUN_NOTE = f"{DRY_RUN} (checks the wiring, no rewrite)"


@dataclass(frozen=True)
class Outcome:
    """A finished run: what to show, whether it failed, the usage line, the requests sent
    (the stub's count on a stub run, 0 when the gate stopped it first), and ``recorded``: true
    whenever a real run was meant to be recorded in the usage history (PROMPT_HISTORY on),
    also when the history write itself was lost (Recorder.finish() never raises)."""

    text: str
    error: bool
    usage: str
    requests: int | None
    recorded: bool = False


def _tokens(values: Sequence[int | None]) -> str:
    known = [v for v in values if v is not None]
    return str(sum(known)) if known else "unknown"


def _cost(attempts: Sequence[AttemptUsage], *, stub: bool) -> str:
    """Reported, unknown or not applicable: a cost nobody reported is never shown as 0."""
    if stub:
        return "not applicable (dry run)"
    if all(a.cost_state == "not_applicable" for a in attempts):
        return "not applicable (runs on this machine)"
    totals: dict[str, Decimal] = {}
    for usage in attempts:
        if usage.charged_amount is not None and usage.charged_unit is not None:
            totals[usage.charged_unit] = totals.get(usage.charged_unit, Decimal(0)) + (
                usage.charged_amount
            )
    if not totals:
        return "unknown"
    shown = " + ".join(f"{amount} {unit}" for unit, amount in totals.items())
    unreported = any(a.charged_amount is None and a.cost_state == "unknown" for a in attempts)
    return f"{shown} reported{' (some unknown)' if unreported else ''}"


def usage_line(attempts: Sequence[AttemptUsage], latency_ms: float, *, stub: bool) -> str:
    """One line on what a run took: time, requests, tokens in and out, and cost; a stub run's
    starts with DRY_RUN_NOTE."""
    lead = f"{DRY_RUN_NOTE} · " if stub else ""
    if not attempts:
        return f"{lead}{latency_ms:.0f} ms · no request was sent"
    inputs = [
        None
        if a.input_uncached is None and a.cache_read is None and a.cache_write is None
        else (a.input_uncached or 0) + (a.cache_read or 0) + (a.cache_write or 0)
        for a in attempts
    ]
    requests = f"{len(attempts)} request{'' if len(attempts) == 1 else 's'}"
    return (
        f"{lead}{latency_ms:.0f} ms · {requests} · tokens in {_tokens(inputs)}, "
        f"out {_tokens([a.output for a in attempts])} · cost {_cost(attempts, stub=stub)}"
    )


def usable(provider: str, cfg: Settings) -> bool:
    """Whether ``provider`` could rewrite a draft on ``cfg``: PROMPT_LOCAL_ONLY does not refuse
    it (leaves_machine(), as make_provider() decides), and its key is set as the call reads it
    (parsed, so a blank one is not; only whether it is, never its value), or it takes none
    (Ollama, LM Studio). An unknown name is not usable. Builds no provider, makes no call."""
    spec = PROVIDERS.get(provider)
    if spec is None or (cfg.local_only and leaves_machine(provider, cfg)):
        return False
    key = spec.api_key(cfg)
    return key is None or bool(key)


def run(
    draft: str,
    provider: str,
    profile: str | None,
    cfg: Settings,
    *,
    stub: bool,
    show: Callable[[Outcome], object] = lambda _: None,
) -> Outcome:
    """Rewrite ``draft`` with ``provider`` on ``cfg`` (the call's strict settings) and
    ``profile`` (None: as configured), against the stub (a dry run) when ``stub``. ``show`` gets the
    outcome before a real run is recorded, as improve records after printing; the run is
    recorded even when ``show`` raises (which then propagates). Returns the outcome."""
    attempts: list[AttemptUsage] = []
    ran = pipeline.Rewrite()
    rec: recorder.Recorder | None = None
    if not stub:
        rec = recorder.Recorder("improve")
        rec.trigger = recorder.TRY_TAB
        rec.track(cfg)
        # Collected whether or not the history is on: the usage line shows them.
        rec.attempts = attempts
    try:
        outcome = _rewrite(draft, provider, profile, cfg, attempts, ran, rec)
        show(outcome)
    finally:
        if rec is not None:
            # After the result is on screen, and even when showing it failed (the app quit
            # meanwhile): a charged call is recorded.
            rec.emitted()
            rec.profile_id = ran.profile_id
            rec.finish()
    return outcome


def _rewrite(
    draft: str,
    provider: str,
    profile: str | None,
    cfg: Settings,
    attempts: list[AttemptUsage],
    ran: pipeline.Rewrite,
    rec: recorder.Recorder | None,
) -> Outcome:
    """The call itself (timed alone) and its outcome. ``rec`` is None on a stub run."""
    requests = 0
    error = False
    latency = 0.0
    try:
        if rec is None:
            # Gated (and refused under PROMPT_LOCAL_ONLY) as the real settings would be,
            # though the stub settings point every provider at 127.0.0.1, and the real
            # base URL checked first (an http:// cloud URL is refused for real too).
            remote = leaves_machine(provider, cfg)
            check_base_url(provider, cfg)
            with smoke.stub_server() as server:
                stub_cfg = smoke.stub_settings(cfg, server.port)
                started = time.monotonic()
                try:
                    text = pipeline.rewrite(
                        draft,
                        provider,
                        stub_cfg,
                        profile,
                        ran,
                        observer=attempts.append,
                        force_remote=remote,
                    )
                finally:
                    # The call alone, not the stub's start or shutdown.
                    latency = (time.monotonic() - started) * 1000
                    requests = server.requests
        else:
            started = time.monotonic()
            try:
                text = pipeline.rewrite(
                    draft, provider, cfg, profile, ran, observer=attempts.append
                )
            finally:
                latency = (time.monotonic() - started) * 1000
            requests = len(attempts)
    except Exception as exc:
        error = True
        text = pipeline.marker(exc)
        if rec is not None:
            rec.outcome = pipeline.failure_outcome(exc, rec)
    # Cleaned as improve's output is (trigger.emit()), the sent-despite note included.
    shown = sanitize.clean(pipeline.sent_despite_note(ran.built) + text)
    usage = usage_line(attempts, latency, stub=rec is None)
    return Outcome(shown, error, usage, requests, recorded=rec is not None and cfg.history)
