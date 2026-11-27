from __future__ import annotations

import re
from collections.abc import Collection

from .providers.base import Provider, ProviderError
from .providers.registry import PROVIDERS
from .redaction import GATE_PACKS, SOFT_FINDINGS, describe, scan_draft


def _block_message(
    findings: list[str],
    hard: list[str],
    allow_flagged: bool,
    name: str,
    relay: bool = False,
    gate_local: bool = False,
    tier: str = "standard",
) -> str:
    """The message names the findings in plain words (describe()) and offers only what the
    trigger that made this call can do. A one-off send is -iok-, the OpenRouter trigger on the
    standard tier: so a pro-tier OpenRouter call (-ip-, -if-) is told that -iok- runs the
    standard model, and no deployed trigger passes --allow-flagged to Anthropic, OpenAI or a
    remote Ollama or LM Studio, so their message offers no one-off. Only when --allow-flagged
    was passed and a hard finding stopped it does the message name the flag that was used.
    A remote Ollama or LM Studio has no local trigger to fall back to ("a local model"). A
    loopback server gated only by PROMPT_GATE_LOCAL (``relay``) is no cloud call, and -il-/-ilm-
    have no --allow-flagged, so its message offers neither. With PROMPT_GATE_LOCAL on
    (``gate_local``) a local trigger or model would block the same draft, so a cloud message
    offers none."""
    found = ", ".join(describe(f) for f in findings)
    if relay:
        return (
            "Blocked call to the local server (PROMPT_GATE_LOCAL=true gates it). Sensitive "
            f"content detected: {found}. Remove it, or set PROMPT_GATE_LOCAL=false if the "
            "server runs the model itself."
        )
    spec = PROVIDERS.get(name)
    router = bool(spec and spec.router)
    local = "a local trigger (-il-)" if spec and spec.cloud else "a local model"
    alternative = "" if gate_local else f", or use {local}"
    message = f"Blocked cloud call. Sensitive content detected: {found}. "
    if not hard and router:
        if tier == "pro":
            return (
                message + "If it may leave this machine, -iok- sends it once on the standard "
                f"model. Otherwise remove it{alternative}."
            )
        return message + f"If it may leave this machine, send it once with -iok-{alternative}."
    if hard and allow_flagged:
        flag = "-iok-" if router else "--allow-flagged"
        message += f"{flag} never sends " + ", ".join(describe(f) for f in hard) + ". "
    return message + f"Remove it{alternative}."


class GateBlocked(ProviderError):
    """A data-protection refusal: the gate's scan, PROMPT_LOCAL_ONLY or a cloud base URL that
    is not https (factory.py). The usage history records each as ``gate_blocked``."""


class GatedProvider:
    """Wraps a provider that can send the draft off this machine and refuses to transmit
    sensitive drafts. factory.make_provider() applies it, so no code path can skip it.

    ``allow_flagged`` sends a draft once despite soft findings only (SOFT_FINDINGS); the
    caller shows ``sent_despite`` with the result. ``allow_override`` (ALLOW_CLOUD_OVERRIDE)
    turns the gate off for every finding. ``packs`` (PROMPT_GATE_PACKS) names the
    locale-specific pattern packs scanned besides the built-in patterns. ``tier``
    (Settings.call_tier) only changes the block message's words. ``sent_despite`` keeps the
    stable finding names; a marker shows them through describe()."""

    def __init__(
        self,
        inner: Provider,
        allow_override: bool,
        extra_patterns: tuple[re.Pattern[str], ...] = (),
        allow_flagged: bool = False,
        name: str = "openrouter",
        relay: bool = False,
        gate_local: bool = False,
        packs: Collection[str] = GATE_PACKS,
        tier: str = "standard",
    ) -> None:
        self._inner = inner
        self._name = name
        self._relay = relay
        self._gate_local = gate_local
        self._allow_override = allow_override
        self._extra_patterns = extra_patterns
        self._packs = packs
        self._tier = tier
        self._allow_flagged = allow_flagged
        self.sent_despite: tuple[str, ...] = ()

    def generate(self, prompt: str, system_prompt: str) -> str:
        self.sent_despite = ()
        findings = scan_draft(prompt, self._extra_patterns, self._packs)
        if findings and not self._allow_override:
            hard = [f for f in findings if f not in SOFT_FINDINGS]
            if hard or not self._allow_flagged:
                message = _block_message(
                    findings,
                    hard,
                    self._allow_flagged,
                    self._name,
                    self._relay,
                    self._gate_local,
                    self._tier,
                )
                raise GateBlocked(message)
            self.sent_despite = tuple(findings)
        return self._inner.generate(prompt, system_prompt)
