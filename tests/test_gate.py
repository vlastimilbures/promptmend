import pytest

from promptmend import redaction
from promptmend.gate import GatedProvider
from promptmend.providers.base import ProviderError
from promptmend.redaction import SOFT_FINDINGS, compile_extra, describe


class _Stub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate(self, prompt: str, system_prompt: str) -> str:
        self.calls.append(prompt)
        return "improved"


# GatedProvider blocks a sensitive draft and never calls through to the inner provider.
def test_blocks_on_findings() -> None:
    inner = _Stub()
    provider = GatedProvider(inner, allow_override=False)
    with pytest.raises(ProviderError, match=r"Blocked cloud call.*a payment card number"):
        provider.generate("card 4111 1111 1111 1111", "system")
    assert inner.calls == []


# ALLOW_CLOUD_OVERRIDE lets a sensitive draft through.
def test_allow_override_passes_through() -> None:
    inner = _Stub()
    provider = GatedProvider(inner, allow_override=True)
    result = provider.generate("card 4111 1111 1111 1111", "system")
    assert result == "improved"
    assert inner.calls == ["card 4111 1111 1111 1111"]


# A clean draft always passes through, override or not.
def test_clean_draft_passes_through() -> None:
    inner = _Stub()
    provider = GatedProvider(inner, allow_override=False)
    result = provider.generate("summarize the quarterly report", "system")
    assert result == "improved"


# User-defined patterns block a draft that the built-in patterns would let through.
def test_blocks_on_extra_pattern() -> None:
    inner = _Stub()
    provider = GatedProvider(inner, allow_override=False, extra_patterns=compile_extra("falcon"))
    with pytest.raises(ProviderError, match="your pattern 1") as exc:
        provider.generate("Project Falcon roadmap", "system")
    assert "custom_1" not in str(exc.value)
    assert inner.calls == []


# A password left alone on the clipboard is blocked, though no pattern names it.
def test_blocks_bare_token_draft() -> None:
    inner = _Stub()
    provider = GatedProvider(inner, allow_override=False)
    with pytest.raises(ProviderError, match="a single password-like word"):
        provider.generate("Winter" + "2026!x", "system")
    assert inner.calls == []


# --allow-flagged sends a draft whose findings are all soft, once, and records them.
def test_allow_flagged_sends_soft_findings() -> None:
    inner = _Stub()
    provider = GatedProvider(inner, allow_override=False, allow_flagged=True)
    assert provider.generate("CONFIDENTIAL: write to jane@example.com", "system") == "improved"
    assert provider.sent_despite == ("email", "confidential_label")


# A hard finding is never sent per call, alone or next to a soft one, and the message says
# that -iok- cannot send it.
@pytest.mark.parametrize(
    "draft",
    [
        "card 4111 1111 1111 1111",
        "CONFIDENTIAL: card 4111 1111 1111 1111",
        "Winter" + "2026!x",
        "password: hunter22",
    ],
    ids=["card", "card-and-label", "bare-token", "password"],
)
def test_allow_flagged_never_sends_hard_findings(draft: str) -> None:
    inner = _Stub()
    provider = GatedProvider(inner, allow_override=False, allow_flagged=True)
    with pytest.raises(ProviderError, match=r"-iok- never sends .*Remove it"):
        provider.generate(draft, "system")
    assert inner.calls == []
    assert provider.sent_despite == ()


# The user's own patterns count as hard: they name what must not leave.
def test_allow_flagged_never_sends_extra_pattern() -> None:
    inner = _Stub()
    provider = GatedProvider(
        inner, allow_override=False, extra_patterns=compile_extra("falcon"), allow_flagged=True
    )
    with pytest.raises(ProviderError, match="never sends your pattern 1"):
        provider.generate("Project Falcon roadmap", "system")
    assert inner.calls == []


# The block message points to the per-call trigger only when every finding is soft, and
# never recommends the global switch.
@pytest.mark.parametrize(
    ("draft", "hint"),
    [
        ("Output is CONFIDENTIAL", "send it once with -iok-"),
        ("card 4111 1111 1111 1111", "Remove it, or use a local trigger (-il-)."),
    ],
    ids=["soft", "hard"],
)
def test_block_message_hint(draft: str, hint: str) -> None:
    provider = GatedProvider(_Stub(), allow_override=False)
    with pytest.raises(ProviderError) as exc:
        provider.generate(draft, "system")
    assert hint in str(exc.value)
    assert "ALLOW_CLOUD_OVERRIDE" not in str(exc.value)
    assert ("-iok-" in str(exc.value)) == (draft == "Output is CONFIDENTIAL")


# The global override sends everything silently, as before; sent_despite is per-call only.
def test_allow_override_records_nothing() -> None:
    provider = GatedProvider(_Stub(), allow_override=True, allow_flagged=True)
    provider.generate("CONFIDENTIAL card 4111 1111 1111 1111", "system")
    assert provider.sent_despite == ()


# Each soft finding can be sent once; the hint names what applies to the provider.
@pytest.mark.parametrize(
    ("draft", "finding"),
    [
        ("Pay CZ65 0800 0000 1920 0014 5399 today", "iban"),
        ("CCCD 001099012345", "vietnam_id_12"),
        ("CMND 123456789", "vietnam_id_9"),
    ],
)
def test_allow_flagged_sends_each_soft_finding(draft: str, finding: str) -> None:
    provider = GatedProvider(_Stub(), allow_override=False, allow_flagged=True)
    provider.generate(draft, "system")
    assert finding in provider.sent_despite


# Only OpenRouter (the registry's router) is offered a one-off, -iok-, its trigger: no deployed
# trigger passes --allow-flagged to the others, so a soft finding reads like a hard one. A
# remote Ollama or LM Studio is not told to use -il- either, while a cloud API is.
@pytest.mark.parametrize("name", ["ollama", "lmstudio", "anthropic", "openai"])
def test_block_message_hint_per_provider(name: str) -> None:
    provider = GatedProvider(_Stub(), allow_override=False, name=name)
    with pytest.raises(ProviderError) as exc:
        provider.generate("Output is CONFIDENTIAL", "system")
    local = "a local trigger (-il-)" if name in ("anthropic", "openai") else "a local model"
    assert str(exc.value) == (
        "Blocked cloud call. Sensitive content detected: a confidentiality label. "
        f"Remove it, or use {local}."
    )


# sent_despite describes the last call only.
def test_sent_despite_resets_per_call() -> None:
    provider = GatedProvider(_Stub(), allow_override=False, allow_flagged=True)
    provider.generate("Output is CONFIDENTIAL", "system")
    provider.generate("plain text", "system")
    assert provider.sent_despite == ()


# The default cloud block message, byte for byte (docs/privacy.md quotes it).
def test_block_message_default_wording() -> None:
    with pytest.raises(ProviderError) as exc:
        GatedProvider(_Stub(), allow_override=False).generate("card 4111 1111 1111 1111", "s")
    assert str(exc.value) == (
        "Blocked cloud call. Sensitive content detected: a payment card number. Remove it, or use "
        "a local trigger (-il-)."
    )


# With PROMPT_GATE_LOCAL=true the local triggers are gated too, so the cloud message does not
# send the user to them (or to a local model).
@pytest.mark.parametrize("name", ["openrouter", "anthropic", "openai", "ollama"])
@pytest.mark.parametrize(
    ("draft", "tail"),
    [
        ("Output is CONFIDENTIAL", "If it may leave this machine, send it once with -iok-."),
        ("card 4111 1111 1111 1111", "Remove it."),
    ],
    ids=["soft", "hard"],
)
def test_block_message_with_gate_local(name: str, draft: str, tail: str) -> None:
    provider = GatedProvider(_Stub(), allow_override=False, name=name, gate_local=True)
    with pytest.raises(ProviderError) as exc:
        provider.generate(draft, "system")
    if name != "openrouter":
        tail = "Remove it."  # no one-off where -iok- does not run
    assert str(exc.value).endswith(tail)
    assert "-il-" not in str(exc.value)
    assert "local model" not in str(exc.value)


# Every finding a marker can name has plain words, one pasteable phrase: each rule's name,
# bare_token (scan_draft) and the user's custom_N; an unknown name falls back to itself.
@pytest.mark.parametrize(
    "name", sorted({rule.name for rule in redaction._RULES} | {"bare_token", "custom_3"})
)
def test_every_finding_has_a_label(name: str) -> None:
    label = describe(name)
    assert label
    assert label != name
    assert not set(label) & set("[]\r\n")
    assert "_" not in label


def test_describe_custom_and_unknown() -> None:
    assert describe("custom_3") == "your pattern 3"
    assert describe("custom_12") == "your pattern 12"
    assert describe("no_such_finding") == "no_such_finding"
    assert describe("custom_x") == "custom_x"
    assert describe("") == ""


# Two rules share the name confidential_label (built-in and vn pack); they share the label.
def test_shared_name_shares_the_label() -> None:
    labels = {r.label for r in redaction._RULES if r.name == "confidential_label"}
    assert labels == {"a confidentiality label"}


_SOFT = "Output is CONFIDENTIAL, write to jane@example.com"
_HARD = "CONFIDENTIAL: card 4111 1111 1111 1111"
# The names with an underscore; "email" is also a word in its label.
_CODE_NAMES = sorted(
    n for n in {r.name for r in redaction._RULES} | {"bare_token", "custom_1"} if "_" in n
)


# Every block message, whatever the provider, tier, findings, flag and PROMPT_GATE_LOCAL: no
# code name, no --allow-flagged unless the call passed it, a one-off only on OpenRouter (and
# saying the standard model on the pro tier), nothing local with gate_local, and never the
# global switch.
@pytest.mark.parametrize("gate_local", [False, True], ids=["", "gate-local"])
@pytest.mark.parametrize("allow_flagged", [False, True], ids=["", "flagged"])
@pytest.mark.parametrize("draft", [_SOFT, _HARD], ids=["soft", "hard"])
@pytest.mark.parametrize("tier", ["standard", "pro"])
@pytest.mark.parametrize("name", ["openrouter", "anthropic", "openai", "ollama", "lmstudio"])
def test_block_message_matrix(
    name: str, tier: str, draft: str, allow_flagged: bool, gate_local: bool
) -> None:
    provider = GatedProvider(
        _Stub(),
        allow_override=False,
        allow_flagged=allow_flagged,
        name=name,
        gate_local=gate_local,
        tier=tier,
    )
    soft = draft == _SOFT
    if soft and allow_flagged:
        assert provider.generate(draft, "s") == "improved"
        assert set(provider.sent_despite) <= SOFT_FINDINGS
        return
    with pytest.raises(ProviderError) as exc:
        provider.generate(draft, "s")
    message = str(exc.value)
    assert message.startswith("Blocked cloud call. Sensitive content detected: ")
    assert "a confidentiality label" in message
    assert not set(message) & set("[]\r\n")
    assert [code for code in _CODE_NAMES if code in message] == []
    assert "ALLOW_CLOUD_OVERRIDE" not in message
    assert ("--allow-flagged" in message) == (allow_flagged and name != "openrouter")
    assert ("-iok-" in message) == (name == "openrouter" and (soft or allow_flagged))
    assert ("standard model" in message) == (name == "openrouter" and soft and tier == "pro")
    assert ("never sends a payment card number" in message) == allow_flagged
    if gate_local:
        assert "-il-" not in message
        assert "local model" not in message
    elif name in ("anthropic", "openai", "openrouter"):
        assert message.endswith("or use a local trigger (-il-).")
    else:
        assert message.endswith("Remove it, or use a local model.")


# The pro-tier OpenRouter wording, byte for byte (-ip-, -if-: -iok- runs the standard model).
@pytest.mark.parametrize(
    ("gate_local", "tail"),
    [
        (False, " Otherwise remove it, or use a local trigger (-il-)."),
        (True, " Otherwise remove it."),
    ],
)
def test_block_message_pro_tier_wording(gate_local: bool, tail: str) -> None:
    provider = GatedProvider(_Stub(), allow_override=False, tier="pro", gate_local=gate_local)
    with pytest.raises(ProviderError) as exc:
        provider.generate("write to jane@example.com", "s")
    assert str(exc.value) == (
        "Blocked cloud call. Sensitive content detected: an email address. If it may leave "
        "this machine, -iok- sends it once on the standard model." + tail
    )


# The hard-finding sentence names the hard findings only, by their labels, and the flag the
# call passed.
def test_never_sends_lists_hard_findings_only() -> None:
    provider = GatedProvider(_Stub(), allow_override=False, allow_flagged=True, name="anthropic")
    with pytest.raises(ProviderError) as exc:
        provider.generate(_HARD, "s")
    assert str(exc.value) == (
        "Blocked cloud call. Sensitive content detected: a payment card number, a "
        "confidentiality label. --allow-flagged never sends a payment card number. Remove it, "
        "or use a local trigger (-il-)."
    )


# The relay message (a loopback server gated by PROMPT_GATE_LOCAL) uses the labels too.
def test_relay_message_uses_labels() -> None:
    provider = GatedProvider(
        _Stub(),
        allow_override=False,
        name="ollama",
        relay=True,
        extra_patterns=compile_extra("falcon"),
    )
    with pytest.raises(ProviderError) as exc:
        provider.generate("Project Falcon roadmap, write to jane@example.com", "s")
    assert str(exc.value) == (
        "Blocked call to the local server (PROMPT_GATE_LOCAL=true gates it). Sensitive content "
        "detected: an email address, your pattern 1. Remove it, or set "
        "PROMPT_GATE_LOCAL=false if the server runs the model itself."
    )
