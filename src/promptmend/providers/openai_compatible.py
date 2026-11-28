from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .base import ProviderError, body_error, chat_messages, finalize_content, post_json
from .usage import Meter, UsageObserver, UsageParser, openai_usage

# Refused in extra_body: keys that would send different text, or send it to a different model
# or service. extra_body is merged over the payload, past what the data-protection gate checked.
# OpenRouter's: ``models`` (fallback models), ``prompt`` (instead of messages), ``preset`` (can
# set the model and a system prompt), ``plugins`` (web search sends the draft to a provider).
_RESERVED_BODY_KEYS = frozenset({"model", "messages", "models", "prompt", "preset", "plugins"})


@dataclass
class OpenAICompatibleProvider:
    """Chat provider for any OpenAI-compatible /chat/completions endpoint.

    Covers OpenAI, OpenRouter, LM Studio, and other compatible servers. The API key is
    optional so local servers (LM Studio) work without one.
    """

    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    timeout: float = 30
    max_tokens: int | None = None
    # The field that carries max_tokens: OpenAI's reasoning models want max_completion_tokens.
    cap_field: str = "max_tokens"
    temperature: float | None = None
    # A top-level `reasoning_effort` (OpenAI), omitted when empty. OpenRouter's nested
    # `reasoning` arrives in extra_body instead.
    reasoning_effort: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    label: str = "OpenAI-compatible endpoint"
    # Both default to inert; used by scripts/bench_models.py to add request fields and read
    # the raw body of a successful reply.
    extra_body: dict[str, object] = field(default_factory=dict)
    on_response: Callable[[dict[str, object]], None] | None = None
    # Receives one AttemptUsage per HTTP attempt; see providers/usage.py. ``name`` is the
    # provider in each record and ``usage`` reads the reply (openrouter_usage also reads
    # OpenRouter's cost and serving provider). ``local`` says the model runs on this machine,
    # so it has no cost to report.
    observer: UsageObserver | None = field(default=None, repr=False, compare=False)
    name: str = "openai-compatible"
    usage: UsageParser = field(default=openai_usage, repr=False, compare=False)
    local: bool = False
    # The next steps its failure markers name, decided by make_provider() from the call's
    # settings (factory.timeout_hint(), cap_hint(); ``local_server`` names a local server
    # such as Ollama for a refused loopback connection); empty names none.
    timeout_hint: str = ""
    cap_hint: str = ""
    local_server: str = ""

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        # A programming error (extra_body is built in code, never from a setting's name):
        # refused here, so no provider is ever built that would send another model or text.
        # Names the key only, never its value.
        if reserved := sorted(_RESERVED_BODY_KEYS.intersection(self.extra_body)):
            raise ValueError(f"extra_body cannot set {', '.join(reserved)}")

    def generate(self, prompt: str, system_prompt: str) -> str:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, object] = {
            "model": self.model,
            "messages": chat_messages(system_prompt, prompt),
        }
        if self.max_tokens:
            payload[self.cap_field] = self.max_tokens
        # Omitted when None: some models (e.g. openai/gpt-5-nano) reject the field.
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        payload.update(self.extra_body)

        meter = None
        if self.observer is not None:
            meter = Meter(self.observer, self.name, self.model, self.usage, self.local)
        data = post_json(
            self.label,
            f"{self.base_url}/chat/completions",
            self.timeout,
            headers=headers,
            json=payload,
            meter=meter,
            timeout_hint=self.timeout_hint,
            local_server=self.local_server,
        )
        if self.on_response is not None:
            self.on_response(data)

        try:
            choice = data["choices"][0]  # type: ignore[index]
            # OpenRouter puts an error raised mid-generation in the choice it cut short.
            if error := body_error(self.label, choice):
                raise error
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.label} response was malformed") from exc

        return finalize_content(
            content,
            self.label,
            stop_reason=choice.get("finish_reason"),
            cap_hint=self.cap_hint,
        )
