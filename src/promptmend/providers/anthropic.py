from __future__ import annotations

from dataclasses import dataclass, field

from .base import ProviderError, finalize_content, post_json
from .usage import Meter, UsageObserver, anthropic_usage

# The Messages API pins its schema via a required version header.
ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class AnthropicProvider:
    """Chat provider for Anthropic's native Messages API.

    This is not OpenAI-compatible: the system prompt is a top-level field,
    max_tokens is required, and the response is a list of content blocks.
    """

    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout: float = 30
    max_tokens: int = 1200
    temperature: float | None = None
    # `output_config.effort` (config.ANTHROPIC_EFFORTS); empty sends no output_config.
    effort: str = ""
    # Receives one AttemptUsage per HTTP attempt; see providers/usage.py.
    observer: UsageObserver | None = field(default=None, repr=False, compare=False)
    # The provider in each usage record and how messages name it; make_provider() passes
    # its registry spec's name and label.
    name: str = "anthropic"
    label: str = "Anthropic"

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def generate(self, prompt: str, system_prompt: str) -> str:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        # No `thinking` field: the default (adaptive) is the one setting every current Claude
        # model accepts (Sonnet 5.5 and Opus 5.5 reject `disabled`); effort bounds it.
        if self.effort:
            payload["output_config"] = {"effort": self.effort}
        meter = (
            Meter(self.observer, self.name, self.model, anthropic_usage)
            if self.observer is not None
            else None
        )
        data = post_json(
            self.label,
            f"{self.base_url}/v1/messages",
            self.timeout,
            headers=headers,
            json=payload,
            meter=meter,
        )

        try:
            # Every text block, in order (a reply may be split into several); thinking and
            # other blocks are skipped. None when there is no text block, e.g. only thinking
            # before the token cap.
            texts = [
                block["text"]
                for block in data["content"]  # type: ignore[attr-defined]
                if block.get("type") == "text"
            ]
            ok = texts and all(isinstance(text, str) for text in texts)
            content = "".join(texts) if ok else None
        except (KeyError, TypeError, AttributeError) as exc:
            raise ProviderError(f"{self.label} response was malformed") from exc

        return finalize_content(
            content, self.label, stop_reason=data.get("stop_reason"), capped=True
        )
