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
from .providers.registry import PROVIDERS
from .providers.usage import UsageObserver, openai_usage, openrouter_usage
from .redaction import compile_extra, safe_repr

PROVIDER_NAMES = tuple(PROVIDERS)
# How messages name each provider.
PROVIDER_LABELS = {name: spec.label for name, spec in PROVIDERS.items()}
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


def check_base_url(name: str, cfg: Settings) -> None:
    """Refuse to send a cloud provider's API key over plaintext HTTP; a loopback proxy is the
    exception. make_provider() applies it; the Try tab's stub runs check the real URL with it
    before pointing the settings at the stub."""
    spec = PROVIDERS.get(name)
    if spec is None or not spec.cloud:
        return
    url = getattr(cfg, f"{name}_base_url")
    scheme = urlsplit(url).scheme
    if not (scheme == "https" or (scheme == "http" and is_loopback(url))):
        raise GateBlocked(f"{spec.url_setting} must be an https:// URL")


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
    """Whether the named provider can send the draft off this machine: a cloud provider
    always, a local one when its base URL is not loopback or (Ollama) the model is a cloud
    model. The gate and PROMPT_LOCAL_ONLY both follow this one decision."""
    spec = PROVIDERS.get(name)
    if spec is None:
        return False
    if spec.cloud:
        return True
    if not is_loopback(getattr(cfg, f"{name}_base_url")):
        return True
    return spec.protocol == "ollama" and _is_ollama_cloud(getattr(cfg, f"{name}_model"))


# The key setting each provider needs; the local ones need none.
PROVIDER_KEYS = {name: spec.key for name, spec in PROVIDERS.items() if spec.key}


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
    for name, spec in PROVIDERS.items():
        remote = leaves_machine(name, cfg)
        found.append(
            Route(
                name,
                spec.model_setting,
                spec.url_setting,
                spec.key,
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

    ``extra_body``, ``on_response`` and ``title`` only apply to OpenRouter (a ``router``
    spec); they let scripts/bench_models.py request usage/cost data through the same
    construction path.

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
    spec = PROVIDERS.get(name)
    if spec is None:
        raise ProviderError(f"Unknown provider {safe_repr(name)}. Use {', '.join(PROVIDER_NAMES)}.")
    base_url = getattr(cfg, f"{name}_base_url")
    model = getattr(cfg, f"{name}_model")
    api_key = _require(getattr(cfg, f"{name}_api_key"), spec.key) if spec.key else None
    max_tokens = getattr(cfg, spec.cap or "call_max_tokens")
    temperature = getattr(cfg, spec.temperature or "temperature")
    provider: Provider
    if spec.protocol == "ollama":
        provider = OllamaProvider(
            base_url=base_url,
            model=model,
            timeout=cfg.timeout,
            think=cfg.ollama_think,
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=cfg.ollama_num_ctx,
            observer=observer,
            local=not remote,
        )
    elif spec.protocol == "anthropic":
        provider = AnthropicProvider(
            base_url=base_url,
            model=model,
            api_key=api_key or "",  # an anthropic spec sets key: _require() raised
            timeout=cfg.timeout,
            max_tokens=max_tokens,
            temperature=temperature,
            observer=observer,
        )
    else:
        # OpenRouter alone takes the app title, routing and nested reasoning fields, the bench
        # hooks and reports a cost; another spec's effort is a top-level field.
        router = spec.router
        provider = OpenAICompatibleProvider(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=cfg.timeout,
            max_tokens=max_tokens,
            cap_field=spec.cap_field,
            temperature=temperature,
            reasoning_effort=getattr(cfg, spec.effort) if spec.effort and not router else "",
            extra_headers={"X-Title": title} if router else {},
            label=spec.label,
            extra_body={**openrouter_body(cfg), **(extra_body or {})} if router else {},
            on_response=on_response if router else None,
            observer=observer,
            name=name,
            usage=openrouter_usage if router else openai_usage,
            local=not remote,
        )
    if remote or cfg.gate_local:
        return _gate(provider, cfg, allow_flagged, name, remote=remote)
    return provider
