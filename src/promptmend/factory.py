from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import Settings
from .gate import GateBlocked, GatedProvider
from .providers.anthropic import AnthropicProvider
from .providers.base import Provider, ProviderError, is_loopback
from .providers.ollama import OllamaProvider
from .providers.openai_compatible import OpenAICompatibleProvider
from .providers.usage import UsageObserver
from .redaction import compile_extra, safe_repr

PROVIDER_NAMES = ("ollama", "lmstudio", "openrouter", "anthropic")
# How messages name each provider.
PROVIDER_LABELS = {
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
}
# Sent as OpenRouter's X-Title so calls are attributed to this app in its dashboard.
APP_TITLE = "promptmend"


def openrouter_routing(
    pin: str, allow_fallbacks: bool, data_collection: str = ""
) -> dict[str, object]:
    """OpenRouter `provider` preferences: one pinned endpoint tag (none: blended routing) and
    the `data_collection` policy if set; {} when neither applies."""
    routing: dict[str, object] = {}
    if pin:
        routing["order"] = [pin]
        if not allow_fallbacks:
            routing["allow_fallbacks"] = False
    if data_collection:
        routing["data_collection"] = data_collection
    return routing


def openrouter_body(cfg: Settings) -> dict[str, object]:
    """OpenRouter-only request fields from settings: endpoint routing and reasoning effort.

    `exclude`: the reasoning trace is billed but never returned as text to paste.
    """
    body: dict[str, object] = {}
    routing = openrouter_routing(
        cfg.openrouter_provider, cfg.openrouter_allow_fallbacks, cfg.openrouter_data_collection
    )
    if routing:
        body["provider"] = routing
    if cfg.openrouter_reasoning_effort:
        body["reasoning"] = {"effort": cfg.openrouter_reasoning_effort, "exclude": True}
    return body


def _require(value: str | None, env_name: str) -> str:
    if not value:
        raise ProviderError(f"{env_name} is not configured")
    # A key is printable ASCII; anything else came from a rich-text paste and cannot be sent
    # in a header. The key itself is never shown.
    if not (value.isascii() and value.isprintable()):
        raise ProviderError(
            f"{env_name} contains a non-ASCII or invisible character (a smart quote?); "
            "paste the key again"
        )
    return value


# The providers whose base URL must be https: they send a key.
_HTTPS_ONLY = {"openrouter": "OPENROUTER_BASE_URL", "anthropic": "ANTHROPIC_BASE_URL"}


def check_base_url(name: str, cfg: Settings) -> None:
    """Refuse to send an API key over plaintext HTTP; a loopback proxy is the exception.
    make_provider() applies it; the Try tab's stub runs check the real URL with it before
    pointing the settings at the stub."""
    setting = _HTTPS_ONLY.get(name)
    if setting is None:
        return
    url = getattr(cfg, f"{name}_base_url")
    scheme = urlsplit(url).scheme
    if not (scheme == "https" or (scheme == "http" and is_loopback(url))):
        raise GateBlocked(f"{setting} must be an https:// URL")


def _is_ollama_cloud(model: str) -> bool:
    """Ollama forwards models tagged `cloud` or `*-cloud` (e.g. gpt-oss:120b-cloud) to
    ollama.com instead of running them locally. Ollama's names are case-insensitive and a
    reference may pin a digest (`name:tag@sha256:…`), so the tag is compared in lower case
    without it. Only the tag counts: a name such as `cloudy-llama:7b` stays local, and a
    `host:port/` registry prefix is not a tag."""
    _, colon, tag = model.partition("@")[0].rpartition(":")
    if not colon or "/" in tag:
        return False
    tag = tag.lower()
    return tag == "cloud" or tag.endswith("-cloud")


def leaves_machine(name: str, cfg: Settings) -> bool:
    """Whether the named provider can send the draft off this machine: the cloud providers
    always, Ollama or LM Studio when the base URL is not loopback or (Ollama) the model is a
    cloud model. The gate and PROMPT_LOCAL_ONLY both follow this one decision."""
    if name == "ollama":
        return not is_loopback(cfg.ollama_base_url) or _is_ollama_cloud(cfg.ollama_model)
    if name == "lmstudio":
        return not is_loopback(cfg.lmstudio_base_url)
    return name in ("openrouter", "anthropic")


# The key setting each provider needs; the local ones need none.
PROVIDER_KEYS = {"openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


@dataclass(frozen=True)
class Route:
    """Where a provider would send a draft, as the settings say: for a screen to show, not
    to call. ``remote`` is leaves_machine(); ``refused`` is PROMPT_LOCAL_ONLY refusing it."""

    name: str
    # The settings that hold the model and the base URL, so a screen can show their values
    # the way `config show` does (a URL with a password in it only as set).
    model_setting: str
    url_setting: str
    key: str | None
    remote: bool
    refused: bool


def routes(cfg: Settings) -> list[Route]:
    """Every provider in PROVIDER_NAMES with its model and base URL settings, key setting and
    whether it can send the draft off this machine. Builds no provider and makes no call."""
    found = []
    for name in PROVIDER_NAMES:
        prefix = name.upper()
        remote = leaves_machine(name, cfg)
        found.append(
            Route(
                name,
                f"{prefix}_MODEL",
                f"{prefix}_BASE_URL",
                PROVIDER_KEYS.get(name),
                remote,
                remote and cfg.local_only,
            )
        )
    return found


def _gate(
    inner: Provider, cfg: Settings, allow_flagged: bool, name: str, *, remote: bool
) -> GatedProvider:
    # ``remote=False`` is only for a loopback provider gated by PROMPT_GATE_LOCAL.
    return GatedProvider(
        inner,
        allow_override=cfg.allow_cloud_override,
        extra_patterns=compile_extra(cfg.extra_patterns),
        allow_flagged=allow_flagged,
        name=name,
        relay=not remote,
        gate_local=cfg.gate_local,
    )


def make_provider(
    name: str,
    cfg: Settings,
    *,
    allow_flagged: bool = False,
    extra_body: dict[str, object] | None = None,
    on_response: Callable[[dict[str, object]], None] | None = None,
    title: str = APP_TITLE,
    observer: UsageObserver | None = None,
    force_remote: bool = False,
) -> Provider:
    """Build the named provider from settings. Anything that can send the draft off this
    machine (see leaves_machine) comes wrapped in the data-protection gate, and with
    PROMPT_LOCAL_ONLY=true it is refused before anything is built. Nothing built here can
    skip either.

    ``allow_flagged`` (--allow-flagged) lets the gate send a draft whose findings are all
    soft, once; it never touches PROMPT_LOCAL_ONLY or a provider that stays on this machine.

    ``PROMPT_GATE_LOCAL=true`` gates a loopback Ollama or LM Studio too (a localhost relay to a
    cloud API), with the same override rules; it is still local for PROMPT_LOCAL_ONLY, routes()
    and its ``not_applicable`` cost.

    ``extra_body``, ``on_response`` and ``title`` only apply to OpenRouter; they let
    scripts/bench_models.py request usage/cost data through the same construction path.

    ``observer`` goes to every provider, inside the gate, and receives one AttemptUsage per
    HTTP attempt (providers/usage.py); a draft the gate blocks makes no attempt, so no record.
    A loopback Ollama or LM Studio that is not a cloud model reports cost ``not_applicable``.

    ``force_remote`` treats the provider as leaving this machine even when ``cfg`` says it
    stays: the interface's Try tab builds against the stub on 127.0.0.1 (``cfg`` points
    there) but gates, and refuses under PROMPT_LOCAL_ONLY, as the real settings would (it
    passes leaves_machine() of those). It only ever adds the gate, never removes it; every
    other caller leaves it False.

    The ``-> Provider`` return type is also what makes mypy check that every provider class
    conforms to the Provider protocol.
    """
    remote = leaves_machine(name, cfg) or force_remote
    if remote and cfg.local_only:
        raise GateBlocked(f"PROMPT_LOCAL_ONLY=true: {name} would send the draft off this machine")
    check_base_url(name, cfg)
    provider: Provider
    if name == "ollama":
        provider = OllamaProvider(
            base_url=cfg.ollama_base_url,
            model=cfg.ollama_model,
            timeout=cfg.timeout,
            think=cfg.ollama_think,
            temperature=cfg.temperature,
            max_tokens=cfg.call_max_tokens,
            num_ctx=cfg.ollama_num_ctx,
            observer=observer,
            local=not remote,
        )
    elif name == "lmstudio":
        provider = OpenAICompatibleProvider(
            base_url=cfg.lmstudio_base_url,
            model=cfg.lmstudio_model,
            timeout=cfg.timeout,
            max_tokens=cfg.call_max_tokens,
            temperature=cfg.temperature,
            label="LM Studio",
            observer=observer,
            name=name,
            local=not remote,
        )
    elif name == "openrouter":
        provider = OpenAICompatibleProvider(
            base_url=cfg.openrouter_base_url,
            model=cfg.openrouter_model,
            api_key=_require(cfg.openrouter_api_key, "OPENROUTER_API_KEY"),
            timeout=cfg.timeout,
            max_tokens=cfg.openrouter_max_tokens,
            temperature=cfg.temperature,
            extra_headers={"X-Title": title},
            label="OpenRouter",
            extra_body={**openrouter_body(cfg), **(extra_body or {})},
            on_response=on_response,
            observer=observer,
            name=name,
        )
    elif name == "anthropic":
        provider = AnthropicProvider(
            base_url=cfg.anthropic_base_url,
            model=cfg.anthropic_model,
            api_key=_require(cfg.anthropic_api_key, "ANTHROPIC_API_KEY"),
            timeout=cfg.timeout,
            max_tokens=cfg.anthropic_max_tokens,
            temperature=cfg.temperature,
            observer=observer,
        )
    else:
        raise ProviderError(f"Unknown provider {safe_repr(name)}. Use {', '.join(PROVIDER_NAMES)}.")
    if remote or cfg.gate_local:
        return _gate(provider, cfg, allow_flagged, name, remote=remote)
    return provider
