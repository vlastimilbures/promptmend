from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .base import ProviderError, body_error, chat_messages, finalize_content, post_json
from .usage import Meter, UsageObserver, UsageParser, openai_usage


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

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

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
            capped=bool(self.max_tokens),
        )
