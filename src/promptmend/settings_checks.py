"""What the effective settings mean: the persona the gate's patterns match, the gate packs
PROMPT_GATE_PACKS leaves off, and the providers the rewrite triggers run on.

Doctor (doctor.py), `config validate` (commands/settings.py), setup (setup_guide.py) and the
warnings after a save (settings_service.py) share these without importing each other. They
take a Settings (and the triggers), print nothing and import no Typer; persona_used() reads
the profile templates (prompt_builder.template(), the user's own profile files included).
Never on the trigger path (tests/test_trigger_contract.py)."""

from __future__ import annotations

from collections.abc import Sequence

from . import assets, config


def rewrites(triggers: Sequence[assets.Trigger], settings: config.Settings) -> dict[str, list[str]]:
    """Each provider the active rewrite triggers run on, with those triggers, in file order;
    a trigger naming no provider runs on PROMPT_PROVIDER (PROMPT_PRO_PROVIDER on the pro tier)."""
    found: dict[str, list[str]] = {}
    for t in triggers:
        if t.active and t.command == "improve":
            provider = t.provider or settings.provider_for(t.tier or "standard")
            found.setdefault(provider, []).append(t.trigger)
    return found


# --- the persona ---------------------------------------------------------------------------


def persona_findings(settings: config.Settings) -> list[str]:
    """What the gate's scan finds in PROMPT_PERSONA: finding names only, never the text. The
    gate scans the draft, but every call also sends the persona in the system prompt, so
    `config validate`, doctor and the interface scan it once instead: a match warns and
    never blocks a trigger, which never runs this. No ``bare_token``: that rule is for a
    whole draft that is one secret-like word (a password left on the clipboard); a persona is
    a typed setting, a one-word one (DataOps2Lead) is a role, and a pasted key still matches
    the key patterns, as `config set` checks every value with the same scan()."""
    from .redaction import compile_extra, scan

    return scan(settings.persona, compile_extra(settings.extra_patterns), settings.gate_packs)


def _persona_stays_local(settings: config.Settings) -> bool:
    """PROMPT_LOCAL_ONLY makes make_provider() refuse every provider that leaves the machine,
    unless PROMPT_GATE_LOCAL says a loopback server may relay to a cloud API."""
    return settings.local_only and not settings.gate_local


def persona_used(settings: config.Settings) -> bool:
    """Whether PROMPT_PROFILE or PROMPT_PRO_PROFILE sends the persona: its template (built-in
    or opted-in user file) holds {{PERSONA_RULE}}. A profile that cannot load sends nothing
    (improve stops; the profiles check reports it). The triggers' only explicit --profile is
    `general`, which has no persona."""
    from .prompt_builder import PERSONA_TOKEN, template

    for name in dict.fromkeys(filter(None, (settings.profile, settings.pro_profile))):
        try:
            if PERSONA_TOKEN in template(name, settings.profile_overrides):
                return True
        except ValueError:
            continue
    return False


def persona_problem(settings: config.Settings) -> str | None:
    """`config validate`'s problem for a persona the gate's patterns match, labels only."""
    findings = persona_findings(settings)
    if not findings or _persona_stays_local(settings) or not persona_used(settings):
        return None
    # With PROMPT_LOCAL_ONLY no cloud call is made: only a relaying loopback server sends it on.
    sent = (
        "with every call through a local relay (PROMPT_GATE_LOCAL=true)"
        if settings.local_only
        else "with every cloud call"
    )
    return (
        f"PROMPT_PERSONA matches the data-protection patterns: {', '.join(findings)}; "
        f"it is sent unscanned {sent}"
    )


# --- the gate packs ------------------------------------------------------------------------


def packs_off(settings: config.Settings) -> list[str]:
    """The gate packs PROMPT_GATE_PACKS leaves off, in GATE_PACKS order."""
    from .redaction import GATE_PACKS

    return [pack for pack in GATE_PACKS if pack not in settings.gate_packs]


def gate_packs_note(settings: config.Settings) -> str | None:
    """The note for gate packs PROMPT_GATE_PACKS leaves off (an empty value, or an empty
    ${VAR} it expanded from), or None when every pack is on. Information, not a problem: a
    pack may be off on purpose, but the gate then flags less."""
    from .redaction import GATE_PACK_COVERS

    off = packs_off(settings)
    if not off:
        return None
    return "; ".join(
        f"gate pack {pack} is off (PROMPT_GATE_PACKS): the gate does not flag "
        f"{GATE_PACK_COVERS[pack]}"
        for pack in off
    )
