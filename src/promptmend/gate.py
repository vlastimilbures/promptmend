from __future__ import annotations

import re

from .providers.base import Provider, ProviderError
from .providers.registry import PROVIDERS
from .redaction import SOFT_FINDINGS, scan_draft


def _block_message(
    findings: list[str],
    hard: list[str],
    allow_flagged: bool,
    name: str,
    relay: bool = False,
    gate_local: bool = False,
) -> str:
    """The hint names what applies to this provider: -iok- is the OpenRouter trigger, and a
    remote Ollama or LM Studio has no local trigger to fall back to. A loopback server gated
    only by PROMPT_GATE_LOCAL (``relay``) is no cloud call, and -il-/-ilm- have no
    --allow-flagged, so its message offers neither. With PROMPT_GATE_LOCAL on (``gate_local``)
    a local trigger or model would block the same draft, so a cloud message offers none."""
    if relay:
        return (
            "Blocked call to the local server (PROMPT_GATE_LOCAL=true gates it). Sensitive "
            "content detected: " + ", ".join(findings) + ". Remove it, or set "
            "PROMPT_GATE_LOCAL=false if the server runs the model itself."
        )
    spec = PROVIDERS.get(name)
    once = "-iok-" if spec and spec.router else "--allow-flagged"
    local = "a local trigger (-il-)" if spec and spec.cloud else "a local model"
    alternative = "" if gate_local else f", or use {local}"
    message = "Blocked cloud call. Sensitive content detected: " + ", ".join(findings) + ". "
    if not hard:
        return message + f"If it may leave this machine, send it once with {once}{alternative}."
    if allow_flagged:
        message += f"{once} never sends " + ", ".join(hard) + ". "
    return message + f"Remove it{alternative}."


class GateBlocked(ProviderError):
    """A data-protection refusal: the gate's scan, PROMPT_LOCAL_ONLY or a cloud base URL that
    is not https (factory.py). The usage history records each as ``gate_blocked``."""


class GatedProvider:
    """Wraps a provider that can send the draft off this machine and refuses to transmit
    sensitive drafts. factory.make_provider() applies it, so no code path can skip it.

    ``allow_flagged`` sends a draft once despite soft findings only (SOFT_FINDINGS); the
    caller shows ``sent_despite`` with the result. ``allow_override`` (ALLOW_CLOUD_OVERRIDE)
    turns the gate off for every finding."""

    def __init__(
        self,
        inner: Provider,
        allow_override: bool,
        extra_patterns: tuple[re.Pattern[str], ...] = (),
        allow_flagged: bool = False,
        name: str = "openrouter",
        relay: bool = False,
        gate_local: bool = False,
    ) -> None:
        self._inner = inner
        self._name = name
        self._relay = relay
        self._gate_local = gate_local
        self._allow_override = allow_override
        self._extra_patterns = extra_patterns
        self._allow_flagged = allow_flagged
        self.sent_despite: tuple[str, ...] = ()

    def generate(self, prompt: str, system_prompt: str) -> str:
        self.sent_despite = ()
        findings = scan_draft(prompt, self._extra_patterns)
        if findings and not self._allow_override:
            hard = [f for f in findings if f not in SOFT_FINDINGS]
            if hard or not self._allow_flagged:
                message = _block_message(
                    findings, hard, self._allow_flagged, self._name, self._relay, self._gate_local
                )
                raise GateBlocked(message)
            self.sent_despite = tuple(findings)
        return self._inner.generate(prompt, system_prompt)
