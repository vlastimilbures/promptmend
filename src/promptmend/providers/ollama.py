from __future__ import annotations

from dataclasses import dataclass, field

from .base import ProviderError, chat_messages, finalize_content, post_json
from .usage import Meter, UsageObserver, ollama_usage


@dataclass
class OllamaProvider:
    """Chat provider for Ollama's native /api/chat endpoint."""

    base_url: str
    model: str
    timeout: float = 30
    # Off by default: disables reasoning for thinking-capable models such as qwen3.
    think: bool = False
    temperature: float | None = None
    # options.num_predict, sent only when set (--max-tokens): Ollama's own default otherwise.
    max_tokens: int | None = None
    # options.num_ctx, sent only when set (OLLAMA_NUM_CTX): Ollama's own context otherwise.
    num_ctx: int | None = None
    # Receives one AttemptUsage per HTTP attempt; see providers/usage.py. ``local`` says the
    # model runs on this machine (loopback, not a cloud model), so it has no cost to report.
    observer: UsageObserver | None = field(default=None, repr=False, compare=False)
    local: bool = False
    # The provider in each usage record and how messages name it; make_provider() passes
    # its registry spec's name and label.
    name: str = "ollama"
    label: str = "Ollama"
    # The next steps its failure markers name, decided by make_provider() from the call's
    # settings (factory.timeout_hint(), cap_hint(); ``local_server`` names a local server
    # such as Ollama for a refused loopback connection); empty names none.
    timeout_hint: str = ""
    cap_hint: str = ""
    local_server: str = ""

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def generate(self, prompt: str, system_prompt: str) -> str:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": chat_messages(system_prompt, prompt),
            "stream": False,
            "think": self.think,
        }
        options: dict[str, object] = {}
        if self.temperature is not None:
            options["temperature"] = self.temperature
        if self.max_tokens:
            options["num_predict"] = self.max_tokens
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        if options:
            payload["options"] = options
        meter = (
            Meter(self.observer, self.name, self.model, ollama_usage, self.local)
            if self.observer is not None
            else None
        )
        data = post_json(
            self.label,
            f"{self.base_url}/api/chat",
            self.timeout,
            json=payload,
            meter=meter,
            timeout_hint=self.timeout_hint,
            local_server=self.local_server,
        )

        try:
            content = data["message"]["content"]  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"{self.label} response was malformed") from exc

        return finalize_content(
            content, self.label, stop_reason=data.get("done_reason"), cap_hint=self.cap_hint
        )
