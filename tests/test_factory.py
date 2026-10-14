from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from promptmend import factory
from promptmend.config import Settings
from promptmend.factory import PROVIDER_NAMES, make_provider, openrouter_routing
from promptmend.gate import GateBlocked, GatedProvider
from promptmend.providers.anthropic import AnthropicProvider
from promptmend.providers.base import Provider, ProviderError
from promptmend.providers.ollama import OllamaProvider
from promptmend.providers.openai_compatible import OpenAICompatibleProvider

if TYPE_CHECKING:
    from conftest import FakeHttp


def _openrouter_inner(provider: Provider) -> OpenAICompatibleProvider:
    """The OpenAI-compatible provider inside the gate that wraps an OpenRouter one."""
    assert isinstance(provider, GatedProvider)
    assert isinstance(provider._inner, OpenAICompatibleProvider)
    return provider._inner


# make_provider("ollama") passes through the think flag and the temperature.
def test_make_provider_ollama() -> None:
    cfg = Settings()
    provider = make_provider("ollama", cfg)
    assert isinstance(provider, OllamaProvider)
    assert provider.think == cfg.ollama_think
    assert provider.temperature == cfg.temperature
    assert provider.num_ctx is None
    provider = make_provider("ollama", replace(cfg, ollama_num_ctx=8192))
    assert isinstance(provider, OllamaProvider)
    assert provider.num_ctx == 8192


# make_provider("lmstudio") sends no api key and labels itself "LM Studio".
def test_make_provider_lmstudio() -> None:
    provider = make_provider("lmstudio", Settings())
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.api_key is None
    assert provider.label == "LM Studio"


# make_provider("openrouter") is gated and sets X-Title + max_tokens.
def test_make_provider_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    provider = make_provider("openrouter", Settings())
    assert isinstance(provider, GatedProvider)
    assert isinstance(provider._inner, OpenAICompatibleProvider)
    assert provider._inner.extra_headers["X-Title"] == "promptmend"
    assert provider._inner.max_tokens == Settings().openrouter_max_tokens


# The default pin is sent as a preference: no allow_fallbacks key, so a dead
# endpoint degrades to blended routing instead of pasting an error into Espanso.
def test_make_provider_openrouter_pins_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_PROVIDER", raising=False)
    monkeypatch.delenv("OPENROUTER_ALLOW_FALLBACKS", raising=False)
    provider = make_provider("openrouter", Settings())
    assert _openrouter_inner(provider).extra_body == {
        "provider": {"order": ["google-ai-studio/flex"]},
        "reasoning": {"effort": "minimal", "exclude": True},
    }


# An empty OPENROUTER_REASONING_EFFORT omits the reasoning field, for models without one.
def test_make_provider_openrouter_no_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_REASONING_EFFORT", "")
    provider = make_provider("openrouter", Settings())
    assert "reasoning" not in _openrouter_inner(provider).extra_body


# The pro tier builds the gated OpenRouter provider on the OPENROUTER_PRO_* settings.
def test_make_provider_pro_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    provider = make_provider("openrouter", Settings().for_tier("pro"))
    inner = _openrouter_inner(provider)
    assert inner.model == "openai/gpt-6-luna"
    assert inner.timeout == 60.0
    assert inner.extra_body == {
        "provider": {"order": ["openai"]},
        "reasoning": {"effort": "low", "exclude": True},
    }


# An empty OPENROUTER_PROVIDER restores OpenRouter's own blended routing.
def test_make_provider_openrouter_no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_PROVIDER", "")
    provider = make_provider("openrouter", Settings())
    assert isinstance(provider, GatedProvider)
    assert isinstance(provider._inner, OpenAICompatibleProvider)
    assert "provider" not in provider._inner.extra_body


# OPENROUTER_ALLOW_FALLBACKS=false turns the preference into a hard pin.
def test_make_provider_openrouter_hard_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_PROVIDER", "deepinfra/fp4")
    monkeypatch.setenv("OPENROUTER_ALLOW_FALLBACKS", "false")
    provider = make_provider("openrouter", Settings())
    assert _openrouter_inner(provider).extra_body["provider"] == {
        "order": ["deepinfra/fp4"],
        "allow_fallbacks": False,
    }


# make_provider("openrouter") errors without OPENROUTER_API_KEY.
def test_make_provider_openrouter_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="OPENROUTER_API_KEY"):
        make_provider("openrouter", Settings())


# make_provider("anthropic") is gated.
def test_make_provider_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    provider = make_provider("anthropic", Settings())
    assert isinstance(provider, GatedProvider)
    assert isinstance(provider._inner, AnthropicProvider)


# make_provider("anthropic") errors without ANTHROPIC_API_KEY.
def test_make_provider_anthropic_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        make_provider("anthropic", Settings())


# make_provider() errors on an unknown provider name.
def test_make_provider_unknown() -> None:
    with pytest.raises(ProviderError, match="Unknown provider"):
        make_provider("bogus", Settings())


# Every advertised provider name builds from the default settings and reaches its endpoint
# (keys set so the cloud branches pass).
@pytest.mark.parametrize(
    ("name", "url", "body"),
    [
        ("ollama", "http://localhost:11434/api/chat", {"message": {"content": "ok"}}),
        (
            "lmstudio",
            "http://localhost:1234/v1/chat/completions",
            {"choices": [{"message": {"content": "ok"}}]},
        ),
        (
            "openrouter",
            "https://openrouter.ai/api/v1/chat/completions",
            {"choices": [{"message": {"content": "ok"}}]},
        ),
        (
            "anthropic",
            "https://api.anthropic.com/v1/messages",
            {"content": [{"type": "text", "text": "ok"}]},
        ),
    ],
)
def test_every_provider_name_builds(
    monkeypatch: pytest.MonkeyPatch,
    fake_http: FakeHttp,
    name: str,
    url: str,
    body: dict[str, Any],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    fake_http.reply(body)
    assert make_provider(name, Settings()).generate("d", "s") == "ok"
    assert [call["url"] for call in fake_http.calls] == [url]


# A key pasted from rich text (a smart quote, an accented letter, an invisible character)
# is refused by name before anything is built or sent, and never repeated.
@pytest.mark.parametrize(
    ("name", "env_name"), [("openrouter", "OPENROUTER_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")]
)
@pytest.mark.parametrize("key", ["sk-t\u00ebst-key", "sk-test\u200bkey", "\u201csk-test-key\u201d"])
def test_non_ascii_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp, name: str, env_name: str, key: str
) -> None:
    monkeypatch.setenv(env_name, key)
    with pytest.raises(
        ProviderError, match=f"^{env_name} contains a non-ASCII or invisible"
    ) as caught:
        make_provider(name, Settings())
    assert "test" not in str(caught.value)
    assert fake_http.requests == []


# The table above covers every advertised name.
def test_every_provider_name_is_listed() -> None:
    names = {"ollama", "lmstudio", "openrouter", "anthropic"}
    assert set(PROVIDER_NAMES) == names


# With the default settings only the cloud providers are gated.
@pytest.mark.parametrize(
    ("name", "gated"),
    [("ollama", False), ("lmstudio", False), ("openrouter", True), ("anthropic", True)],
)
def test_cloud_providers_are_gated(monkeypatch: pytest.MonkeyPatch, name: str, gated: bool) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert isinstance(make_provider(name, Settings()), GatedProvider) is gated


# A local provider pointed at another machine sends the draft off this one, so it is gated.
@pytest.mark.parametrize(
    ("name", "var"), [("ollama", "OLLAMA_BASE_URL"), ("lmstudio", "LMSTUDIO_BASE_URL")]
)
@pytest.mark.parametrize(
    ("url", "gated"),
    [
        ("http://192.168.1.5:11434", True),
        ("https://ollama.com", True),
        ("http://gpu-box.internal:1234/v1", True),
        ("http://localhost:11434", False),
        ("http://127.0.0.2:1234/v1", False),
        ("http://[::1]:11434", False),
    ],
)
def test_remote_local_provider_is_gated(
    monkeypatch: pytest.MonkeyPatch, name: str, var: str, url: str, gated: bool
) -> None:
    monkeypatch.setenv(var, url)
    assert isinstance(make_provider(name, Settings()), GatedProvider) is gated


# Ollama runs `cloud`-tagged models on ollama.com, even through a local daemon.
@pytest.mark.parametrize(
    ("model", "gated"),
    [
        ("gpt-oss:120b-cloud", True),
        ("glm-4.6:cloud", True),
        ("qwen3:8b", False),
        ("cloud-model", False),
    ],
)
def test_ollama_cloud_model_is_gated(
    monkeypatch: pytest.MonkeyPatch, model: str, gated: bool
) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", model)
    assert isinstance(make_provider("ollama", Settings()), GatedProvider) is gated


# The `cloud` tag is matched case-insensitively and after an `@sha256:` digest is removed;
# only the tag counts, so a model name that contains "cloud" stays local.
CLOUD_SPELLINGS = [
    "GPT-OSS:120B-CLOUD",
    "gpt-oss:Cloud",
    "gpt-oss:120b-cloud@sha256:abc123",
    "glm-4.6:CLOUD@sha256:abc123",
]
LOCAL_SPELLINGS = [
    "cloudy-llama:7b",
    "cloud-model",
    "my-cloud:latest",
    "qwen3:8b@sha256:abc123",
    "registry.local:5000/team/cloud",
]


@pytest.mark.parametrize(
    ("model", "cloud"),
    [(m, True) for m in CLOUD_SPELLINGS] + [(m, False) for m in LOCAL_SPELLINGS],
)
def test_ollama_cloud_tag_spellings(
    monkeypatch: pytest.MonkeyPatch, model: str, cloud: bool
) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", model)
    cfg = Settings()
    assert factory._is_ollama_cloud(model) is cloud
    # the gate wraps it, and its cost is not_applicable only when it truly runs here
    provider = make_provider("ollama", cfg)
    assert isinstance(provider, GatedProvider) is cloud
    inner = provider._inner if isinstance(provider, GatedProvider) else provider
    assert isinstance(inner, OllamaProvider)
    assert inner.local is not cloud
    # routes() (the interface's Providers tab) reports the same verdict
    route = {r.name: r for r in factory.routes(cfg)}["ollama"]
    assert route.remote is cloud


@pytest.mark.parametrize("model", CLOUD_SPELLINGS)
def test_local_only_refuses_every_cloud_tag_spelling(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")
    monkeypatch.setenv("OLLAMA_MODEL", model)
    assert {r.name: r for r in factory.routes(Settings())}["ollama"].refused
    with pytest.raises(ProviderError, match=r"^PROMPT_LOCAL_ONLY=true: ollama would send"):
        make_provider("ollama", Settings())


@pytest.mark.parametrize("model", LOCAL_SPELLINGS)
def test_local_only_allows_a_name_containing_cloud(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")
    monkeypatch.setenv("OLLAMA_MODEL", model)
    assert not isinstance(make_provider("ollama", Settings()), GatedProvider)


# PROMPT_LOCAL_ONLY=true refuses every provider that can leave this machine before building
# it (so even without an API key the refusal is what the user sees).
@pytest.mark.parametrize(
    ("name", "env"),
    [
        ("openrouter", {}),
        ("anthropic", {}),
        ("ollama", {"OLLAMA_BASE_URL": "http://192.168.1.5:11434"}),
        ("ollama", {"OLLAMA_MODEL": "gpt-oss:120b-cloud"}),
        ("lmstudio", {"LMSTUDIO_BASE_URL": "https://lms.example.com/v1"}),
    ],
    ids=["openrouter", "anthropic", "remote-ollama", "ollama-cloud-model", "remote-lmstudio"],
)
def test_local_only_refuses_providers_that_leave_the_machine(
    monkeypatch: pytest.MonkeyPatch, name: str, env: dict[str, str]
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ProviderError, match=f"^PROMPT_LOCAL_ONLY=true: {name} would send"):
        make_provider(name, Settings())


# Local providers on this machine still build, ungated, under PROMPT_LOCAL_ONLY=true.
@pytest.mark.parametrize("name", ["ollama", "lmstudio"])
def test_local_only_allows_loopback_providers(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")
    assert not isinstance(make_provider(name, Settings()), GatedProvider)


# make_provider gates exactly the providers leaves_machine reports, so PROMPT_LOCAL_ONLY
# (which follows leaves_machine) refuses everything the gate would scan.
@pytest.mark.parametrize("name", PROVIDER_NAMES)
@pytest.mark.parametrize(
    "env",
    [
        {},
        {"OLLAMA_BASE_URL": "http://192.168.1.5:11434", "LMSTUDIO_BASE_URL": "http://10.0.0.2/v1"},
        {"OLLAMA_MODEL": "gpt-oss:120b-cloud"},
    ],
    ids=["defaults", "remote-local", "ollama-cloud-model"],
)
def test_gated_exactly_when_leaving_the_machine(
    monkeypatch: pytest.MonkeyPatch, name: str, env: dict[str, str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cfg = Settings()
    assert isinstance(make_provider(name, cfg), GatedProvider) is factory.leaves_machine(name, cfg)


# The gate on a remote local provider blocks a sensitive draft before any request.
def test_remote_ollama_blocks_sensitive_draft(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp
) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://192.168.1.5:11434")
    provider = make_provider("ollama", Settings())
    with pytest.raises(ProviderError, match="Blocked cloud call"):
        provider.generate("card 4111 1111 1111 1111", "sys")
    assert fake_http.calls == []


# extra_body is merged over the routing preferences, on_response and title are forwarded
# (the path scripts/bench_models.py uses for usage/cost reporting).
def test_openrouter_extra_body_on_response_and_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    hook = print
    provider = make_provider(
        "openrouter",
        Settings(),
        extra_body={"usage": {"include": True}},
        on_response=hook,
        title="bench",
    )
    inner = _openrouter_inner(provider)
    assert inner.extra_body == {
        "provider": {"order": ["google-ai-studio/flex"]},
        "reasoning": {"effort": "minimal", "exclude": True},
        "usage": {"include": True},
    }
    assert inner.on_response is hook
    assert inner.extra_headers == {"X-Title": "bench"}


@pytest.mark.parametrize(
    ("pin", "allow", "expected"),
    [
        ("", True, {}),
        ("", False, {}),
        ("a/b", True, {"order": ["a/b"]}),
        ("a/b", False, {"order": ["a/b"], "allow_fallbacks": False}),
    ],
)
def test_openrouter_routing(pin: str, allow: bool, expected: dict[str, object]) -> None:
    assert openrouter_routing(pin, allow) == expected
    assert openrouter_routing(pin, allow, "") == expected


# data_collection joins the routing object, also without a pin (blended routing).
@pytest.mark.parametrize(
    ("pin", "allow", "policy", "expected"),
    [
        ("", True, "deny", {"data_collection": "deny"}),
        ("a/b", True, "allow", {"order": ["a/b"], "data_collection": "allow"}),
        (
            "a/b",
            False,
            "deny",
            {"order": ["a/b"], "allow_fallbacks": False, "data_collection": "deny"},
        ),
    ],
)
def test_openrouter_routing_data_collection(
    pin: str,
    allow: bool,
    policy: str,
    expected: dict[str, object],
) -> None:
    assert openrouter_routing(pin, allow, policy) == expected


# OPENROUTER_DATA_COLLECTION reaches the request body of both tiers, and an @auto pick
# (no pin) still carries it; unset leaves the body exactly as before.
@pytest.mark.parametrize("policy", ["deny", "allow"])
def test_make_provider_openrouter_data_collection(
    monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_PROVIDER", raising=False)
    monkeypatch.setenv("OPENROUTER_DATA_COLLECTION", policy)
    standard = _openrouter_inner(make_provider("openrouter", Settings())).extra_body["provider"]
    assert standard == {"order": ["google-ai-studio/flex"], "data_collection": policy}
    pro = _openrouter_inner(make_provider("openrouter", Settings().for_call("pro"))).extra_body[
        "provider"
    ]
    assert pro == {"order": ["openai"], "data_collection": policy}
    auto = Settings().for_call("pro", model="openai/gpt-6-luna@auto")
    assert _openrouter_inner(make_provider("openrouter", auto)).extra_body["provider"] == {
        "data_collection": policy
    }


def test_make_provider_openrouter_data_collection_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_DATA_COLLECTION", raising=False)
    for cfg in (Settings(), Settings().for_tier("pro")):
        routing = _openrouter_inner(make_provider("openrouter", cfg)).extra_body.get("provider", {})
        assert isinstance(routing, dict)
        assert "data_collection" not in routing
    monkeypatch.setenv("OPENROUTER_PROVIDER", "")
    assert "provider" not in _openrouter_inner(make_provider("openrouter", Settings())).extra_body


# Cloud base URLs must be https, so the API key never travels in plaintext.
@pytest.mark.parametrize(
    ("name", "var"), [("openrouter", "OPENROUTER_BASE_URL"), ("anthropic", "ANTHROPIC_BASE_URL")]
)
@pytest.mark.parametrize("url", ["http://example.com/api", "ftp://example.com", "example.com"])
def test_cloud_base_url_requires_https(
    monkeypatch: pytest.MonkeyPatch, name: str, var: str, url: str
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv(var, url)
    with pytest.raises(ProviderError, match=f"{var} must be an https:// URL"):
        make_provider(name, Settings())


# Plain http is allowed to a loopback host, e.g. a local debugging proxy.
@pytest.mark.parametrize(
    "url", ["http://localhost:8080/v1", "http://127.0.0.1/v1", "http://[::1]/v1"]
)
def test_cloud_base_url_allows_loopback_http(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_BASE_URL", url)
    assert isinstance(make_provider("openrouter", Settings()), GatedProvider)


# Settings rejects an invalid PROMPT_EXTRA_PATTERNS regex (test_config.py); one that reaches
# the factory anyway (a Settings built without its parsers) is still reported, never skipped.
def test_invalid_extra_pattern_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    cfg = replace(Settings(), extra_patterns="a[")
    with pytest.raises(ValueError, match="PROMPT_EXTRA_PATTERNS entry 1"):
        make_provider("openrouter", cfg)


# allow_flagged reaches the gate of every provider that leaves the machine, and nothing else.
@pytest.mark.parametrize("name", ["openrouter", "anthropic"])
def test_make_provider_passes_allow_flagged_to_gate(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    provider = make_provider(name, Settings.load(), allow_flagged=True)
    assert isinstance(provider, GatedProvider)
    assert provider._allow_flagged is True
    unflagged = make_provider(name, Settings.load())
    assert isinstance(unflagged, GatedProvider)
    assert unflagged._allow_flagged is False


# A remote Ollama or LM Studio gets the flag and its name through its gate too; a local one
# is not gated at all.
@pytest.mark.parametrize(
    ("name", "remote_cfg", "local_type"),
    [
        ("ollama", Settings(ollama_base_url="https://ollama.example.com"), OllamaProvider),
        (
            "lmstudio",
            Settings(lmstudio_base_url="https://lm.example.com"),
            OpenAICompatibleProvider,
        ),
    ],
)
def test_allow_flagged_on_remote_and_local_models(
    name: str, remote_cfg: Settings, local_type: type[Provider]
) -> None:
    remote = make_provider(name, remote_cfg, allow_flagged=True)
    assert isinstance(remote, GatedProvider)
    assert (remote._allow_flagged, remote._name) == (True, name)
    assert isinstance(make_provider(name, Settings(), allow_flagged=True), local_type)


# PROMPT_LOCAL_ONLY still wins over --allow-flagged.
def test_local_only_beats_allow_flagged() -> None:
    with pytest.raises(ProviderError, match="PROMPT_LOCAL_ONLY"):
        make_provider("openrouter", Settings(local_only=True), allow_flagged=True)


# PROMPT_GATE_LOCAL=true also wraps Ollama and LM Studio on loopback in the gate (for a
# localhost relay to a cloud API), without changing what counts as leaving this machine.
@pytest.mark.parametrize("name", ["ollama", "lmstudio"])
def test_gate_local_wraps_loopback_providers(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("PROMPT_GATE_LOCAL", "true")
    cfg = Settings()
    provider = make_provider(name, cfg)
    assert isinstance(provider, GatedProvider)
    # still local: cost not_applicable, routes() says it stays here
    assert isinstance(provider._inner, (OllamaProvider, OpenAICompatibleProvider))
    assert provider._inner.local is True
    assert not factory.leaves_machine(name, cfg)
    assert not {r.name: r for r in factory.routes(cfg)}[name].remote


@pytest.mark.parametrize("name", ["ollama", "lmstudio"])
def test_gate_local_blocks_a_sensitive_draft(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp, name: str
) -> None:
    monkeypatch.setenv("PROMPT_GATE_LOCAL", "true")
    provider = make_provider(name, Settings())
    with pytest.raises(ProviderError, match="Sensitive content detected: payment_card"):
        provider.generate("card 4111 1111 1111 1111", "sys")
    assert fake_http.calls == []


# The same override rules apply: ALLOW_CLOUD_OVERRIDE and --allow-flagged for soft findings.
def test_gate_local_follows_the_override_rules(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp
) -> None:
    monkeypatch.setenv("PROMPT_GATE_LOCAL", "true")
    flagged = make_provider("ollama", Settings(), allow_flagged=True)
    with pytest.raises(ProviderError, match="payment_card"):
        flagged.generate("card 4111 1111 1111 1111", "sys")
    monkeypatch.setenv("ALLOW_CLOUD_OVERRIDE", "true")
    overridden = make_provider("ollama", Settings())
    assert isinstance(overridden, GatedProvider)
    assert overridden._allow_override is True


# PROMPT_LOCAL_ONLY still allows a loopback provider when PROMPT_GATE_LOCAL gates it.
@pytest.mark.parametrize("name", ["ollama", "lmstudio"])
def test_gate_local_with_local_only_keeps_loopback(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("PROMPT_GATE_LOCAL", "true")
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")
    assert isinstance(make_provider(name, Settings()), GatedProvider)
    assert not {r.name: r for r in factory.routes(Settings())}[name].refused


# Off (the default), a loopback provider stays unwrapped.
def test_gate_local_defaults_to_false() -> None:
    assert Settings().gate_local is False


# A loopback server gated by PROMPT_GATE_LOCAL is not a cloud call, and -il-/-ilm- cannot
# pass --allow-flagged, so the block message says what applies instead.
@pytest.mark.parametrize("name", ["ollama", "lmstudio"])
def test_gate_local_block_message(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp, name: str
) -> None:
    monkeypatch.setenv("PROMPT_GATE_LOCAL", "true")
    with pytest.raises(ProviderError) as exc:
        make_provider(name, Settings()).generate("write to jane@example.com", "sys")
    assert str(exc.value) == (
        "Blocked call to the local server (PROMPT_GATE_LOCAL=true gates it). Sensitive content "
        "detected: email. Remove it, or set PROMPT_GATE_LOCAL=false if the server runs the "
        "model itself."
    )


# An empty PROMPT_TEMPERATURE reaches every provider as None (omitted from the body), and
# --max-tokens reaches the local providers, which otherwise get no cap (#31).
def test_make_provider_empty_temperature_and_call_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "-".join(("test", "key")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "-".join(("test", "key")))
    monkeypatch.setenv("PROMPT_TEMPERATURE", "")
    cfg = Settings()
    for name in PROVIDER_NAMES:
        provider = make_provider(name, cfg)
        inner = provider._inner if isinstance(provider, GatedProvider) else provider
        assert isinstance(inner, (OllamaProvider, OpenAICompatibleProvider, AnthropicProvider))
        assert inner.temperature is None
    uncapped = make_provider("ollama", cfg)
    assert isinstance(uncapped, OllamaProvider)
    assert uncapped.max_tokens is None
    capped = cfg.with_overrides(max_tokens="300")
    for name in ("ollama", "lmstudio"):
        local = make_provider(name, capped)
        assert isinstance(local, (OllamaProvider, OpenAICompatibleProvider))
        assert local.max_tokens == 300


# make_provider passes PROMPT_GATE_LOCAL to every gate: a cloud block then offers no local
# trigger, which would be blocked as well.
def test_gate_local_cloud_block_message(
    monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp
) -> None:
    monkeypatch.setenv("PROMPT_GATE_LOCAL", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    with pytest.raises(ProviderError) as exc:
        make_provider("openrouter", Settings()).generate("card 4111 1111 1111 1111", "sys")
    assert str(exc.value) == (
        "Blocked cloud call. Sensitive content detected: payment_card. Remove it."
    )
    assert fake_http.requests == []


# force_remote (the Try tab's stub runs) only ever adds the gate: False never un-gates a
# provider that leaves the machine, nor lifts PROMPT_LOCAL_ONLY.
@pytest.mark.parametrize("name", ["ollama", "lmstudio"])
def test_force_remote_false_never_ungates(name: str) -> None:
    remote = replace(
        Settings(),
        ollama_base_url="https://ollama.example.com",
        lmstudio_base_url="https://lmstudio.example.com/v1",
    )
    assert isinstance(make_provider(name, remote, force_remote=False), GatedProvider)
    with pytest.raises(GateBlocked, match="PROMPT_LOCAL_ONLY"):
        make_provider(name, replace(remote, local_only=True), force_remote=False)


@pytest.mark.parametrize("name", ["ollama", "lmstudio"])
def test_force_remote_gates_a_loopback_stand_in(name: str) -> None:
    local = Settings()
    assert not isinstance(make_provider(name, local), GatedProvider)
    assert isinstance(make_provider(name, local, force_remote=True), GatedProvider)
    with pytest.raises(GateBlocked, match="PROMPT_LOCAL_ONLY"):
        make_provider(name, replace(local, local_only=True), force_remote=True)


def test_check_base_url_matches_make_provider() -> None:
    plain = replace(
        Settings(),
        openrouter_base_url="http://relay.example/api/v1",
        anthropic_base_url="http://relay.example",
        openrouter_api_key="k",
        anthropic_api_key="k",
    )
    for name, setting in factory._HTTPS_ONLY.items():
        with pytest.raises(GateBlocked, match=f"{setting} must be an https:// URL"):
            factory.check_base_url(name, plain)
        with pytest.raises(GateBlocked, match=f"{setting} must be an https:// URL"):
            make_provider(name, plain)
    factory.check_base_url("ollama", replace(plain, ollama_base_url="http://relay.example"))
    factory.check_base_url("openrouter", Settings())
