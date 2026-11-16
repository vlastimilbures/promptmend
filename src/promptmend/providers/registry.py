"""What each provider is: the one table the factory, the gate, the settings, setup, doctor and
the stub read, so a new provider is one entry here plus its settings.

Data only, so it costs nothing on the trigger path. A provider's settings follow its name:
``{name}_base_url`` and ``{name}_model`` always, ``{name}_api_key`` when it has a ``key``
(``url_field``, ``model_field``, ``key_field``: the names ``dataclasses.replace()`` takes).
Each spec also carries typed readers for them, so the code that reads a provider's settings
goes through ``url()``, ``model()``, ``api_key()``, ``max_tokens()``, ``request_temperature()``
and ``reasoning_effort()``, which mypy checks, instead of rebuilding a field name for
``getattr``. tests/test_registry.py ties every reader to the field name next to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..config import Settings


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    # How messages name it, and what the setup wizard says it is.
    label: str
    help: str
    # The wire format: providers/openai_compatible.py, anthropic.py or ollama.py.
    protocol: Literal["openai", "anthropic", "ollama"]
    # A cloud API: it always sends the draft off this machine (so it is always gated), needs
    # its key setting, and its base URL must be https (plain http only to loopback).
    cloud: bool = False
    key: str | None = None
    # The Settings field of its own output cap, which --max-tokens also sets; None: it has no
    # cap setting, and only a --max-tokens (Settings.call_max_tokens) caps it.
    # cap, temperature and effort each have a reader twin below (read_cap, read_temperature,
    # read_effort); test_accessors_read_the_settings_they_name ties each pair.
    cap: str | None = None
    # The Settings field of its own temperature; None: PROMPT_TEMPERATURE (Settings.temperature).
    temperature: str | None = None
    # The request field that carries the output cap (OpenAI's reasoning models reject
    # max_tokens and want max_completion_tokens).
    cap_field: str = "max_tokens"
    # The Settings field of its reasoning effort, which --effort also sets; None: it has none,
    # and --effort is refused for it.
    effort: str | None = None
    # OpenRouter's own API: the pro tier's own settings (OPENROUTER_PRO_*), endpoint routing
    # and the nested `reasoning` field, X-Title, the cost in each reply, the bench hooks and
    # the -iok- trigger. A spec without it sends its effort as a top-level `reasoning_effort`.
    router: bool = False
    # Has the pro tier (--tier pro, PROMPT_PRO_PROVIDER): config.Settings.tier_preset(). On a
    # ``router`` the tier swaps in OPENROUTER_PRO_*; on another provider it keeps the
    # provider's own model, effort and cap, and swaps in only the pro time limit and profile.
    pro: bool = False
    # Where doctor asks a local server whether it answers, below its base URL.
    probe_path: str | None = None
    # The typed readers behind the accessors below, one per setting named above: the base
    # URL and model always, the key, cap, temperature and effort exactly when ``key``,
    # ``cap``, ``temperature`` and ``effort`` are set. Out of repr(): a key reader is code,
    # but nothing here should invite printing what it returns.
    read_url: Callable[[Settings], str] = field(kw_only=True, repr=False, compare=False)
    read_model: Callable[[Settings], str] = field(kw_only=True, repr=False, compare=False)
    read_key: Callable[[Settings], str] | None = field(
        default=None, kw_only=True, repr=False, compare=False
    )
    read_cap: Callable[[Settings], int] | None = field(
        default=None, kw_only=True, repr=False, compare=False
    )
    read_temperature: Callable[[Settings], float | None] | None = field(
        default=None, kw_only=True, repr=False, compare=False
    )
    read_effort: Callable[[Settings], str] | None = field(
        default=None, kw_only=True, repr=False, compare=False
    )

    @property
    def url_field(self) -> str:
        return f"{self.name}_base_url"

    @property
    def model_field(self) -> str:
        return f"{self.name}_model"

    @property
    def key_field(self) -> str | None:
        """The Settings field of its API key; None for a provider without a ``key``."""
        return f"{self.name}_api_key" if self.key else None

    def url(self, cfg: Settings) -> str:
        """Its base URL (``{NAME}_BASE_URL``)."""
        return self.read_url(cfg)

    def model(self, cfg: Settings) -> str:
        """Its model (``{NAME}_MODEL``)."""
        return self.read_model(cfg)

    def api_key(self, cfg: Settings) -> str | None:
        """Its API key (``key``), possibly empty; None for a provider without one."""
        return self.read_key(cfg) if self.read_key else None

    def max_tokens(self, cfg: Settings) -> int | None:
        """The output cap of a call: its own ``cap`` setting, else --max-tokens
        (Settings.call_max_tokens, None when not given)."""
        return self.read_cap(cfg) if self.read_cap else cfg.call_max_tokens

    def request_temperature(self, cfg: Settings) -> float | None:
        """The temperature a call sends: its own ``temperature`` setting, else
        PROMPT_TEMPERATURE (Settings.temperature); None sends none."""
        return self.read_temperature(cfg) if self.read_temperature else cfg.temperature

    def reasoning_effort(self, cfg: Settings) -> str:
        """Its ``effort`` setting; empty for a provider without one."""
        return self.read_effort(cfg) if self.read_effort else ""

    @property
    def model_setting(self) -> str:
        return f"{self.name.upper()}_MODEL"

    @property
    def url_setting(self) -> str:
        return f"{self.name.upper()}_BASE_URL"


# In the order --help, error messages and the interface's lists show them.
PROVIDERS: dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec(
            "ollama",
            "Ollama",
            "local model on this machine, no key",
            "ollama",
            probe_path="/api/tags",
            read_url=lambda c: c.ollama_base_url,
            read_model=lambda c: c.ollama_model,
        ),
        ProviderSpec(
            "lmstudio",
            "LM Studio",
            "local model on this machine, no key",
            "openai",
            probe_path="/models",
            read_url=lambda c: c.lmstudio_base_url,
            read_model=lambda c: c.lmstudio_model,
        ),
        ProviderSpec(
            "openrouter",
            "OpenRouter",
            "cloud, many models through one key",
            "openai",
            cloud=True,
            key="OPENROUTER_API_KEY",
            cap="openrouter_max_tokens",
            effort="openrouter_reasoning_effort",
            router=True,
            pro=True,
            read_url=lambda c: c.openrouter_base_url,
            read_model=lambda c: c.openrouter_model,
            read_key=lambda c: c.openrouter_api_key,
            read_cap=lambda c: c.openrouter_max_tokens,
            read_effort=lambda c: c.openrouter_reasoning_effort,
        ),
        ProviderSpec(
            "anthropic",
            "Anthropic",
            "cloud, Claude models, needs a key",
            "anthropic",
            cloud=True,
            key="ANTHROPIC_API_KEY",
            cap="anthropic_max_tokens",
            temperature="anthropic_temperature",
            pro=True,
            read_url=lambda c: c.anthropic_base_url,
            read_model=lambda c: c.anthropic_model,
            read_key=lambda c: c.anthropic_api_key,
            read_cap=lambda c: c.anthropic_max_tokens,
            read_temperature=lambda c: c.anthropic_temperature,
        ),
        ProviderSpec(
            "openai",
            "OpenAI",
            "cloud, GPT models, needs a key",
            "openai",
            cloud=True,
            key="OPENAI_API_KEY",
            cap="openai_max_tokens",
            temperature="openai_temperature",
            cap_field="max_completion_tokens",
            effort="openai_reasoning_effort",
            pro=True,
            read_url=lambda c: c.openai_base_url,
            read_model=lambda c: c.openai_model,
            read_key=lambda c: c.openai_api_key,
            read_cap=lambda c: c.openai_max_tokens,
            read_temperature=lambda c: c.openai_temperature,
            read_effort=lambda c: c.openai_reasoning_effort,
        ),
    )
}
