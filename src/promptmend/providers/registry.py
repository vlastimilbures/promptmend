"""What each provider is: the one table the factory, the gate, the settings, setup, doctor and
the stub read, so a new provider is one entry here plus its settings.

Data only, so it costs nothing on the trigger path. A provider's settings follow its name:
``{name}_base_url`` and ``{name}_model`` always, ``{name}_api_key`` when it has a ``key``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


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
    cap: str | None = None
    # The Settings field of its own temperature; None: PROMPT_TEMPERATURE (Settings.temperature).
    temperature: str | None = None
    # The request field that carries the output cap (OpenAI's reasoning models reject
    # max_tokens and want max_completion_tokens).
    cap_field: str = "max_tokens"
    # The Settings field of its reasoning effort, which --effort also sets; None: it has none,
    # and --effort is refused for it.
    effort: str | None = None
    # OpenRouter's own API: the pro tier (--tier pro, OPENROUTER_PRO_*), endpoint routing and
    # the nested `reasoning` field, X-Title, the cost in each reply, the bench hooks and the
    # -iok- trigger. A spec without it sends its effort as a top-level `reasoning_effort`.
    router: bool = False
    # Where doctor asks a local server whether it answers, below its base URL.
    probe_path: str | None = None

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
        ),
        ProviderSpec(
            "lmstudio",
            "LM Studio",
            "local model on this machine, no key",
            "openai",
            probe_path="/models",
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
        ),
    )
}
