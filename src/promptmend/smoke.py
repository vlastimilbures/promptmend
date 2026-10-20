"""The set-up smoke test: run a real `improve` against a stub provider on 127.0.0.1, so the
settings, the provider, the gate, the HTTP call and the output path are all exercised without
a paid call or a key leaving the machine.

A child process runs `python -m promptmend.cli improve --provider <name> ...` (the same
code a trigger runs) with every provider base URL pointed at the stub and a placeholder key
in place of the real one, set as environment variables, which outrank every settings file.
The usage history is switched off the same way: a health check is not usage (#116).

The interface's Try tab (#111) runs its stub rewrites against the same stub, in process:
stub_server() starts it and stub_settings() points a Settings at it as stub_env() does.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .config import Settings, setting_fields
from .entry import self_command
from .providers.registry import PROVIDERS

# What the stub answers, and so what improve must print.
REPLY = "promptmend setup check: ok"
DRAFT = "Check that promptmend can rewrite a draft end to end."
# Sent instead of the real key, which never reaches even the stub.
PLACEHOLDER_KEY = "setup-check-placeholder"
TIMEOUT = 60
# The token counts every stub reply reports, so a stub run shows a usage line.
INPUT_TOKENS = 42
OUTPUT_TOKENS = 7

# Runs argv with env; returns (exit code, stdout). Tests may replace it.
Runner = Callable[[Sequence[str], Mapping[str, str]], tuple[int, bytes]]


def run_cli(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, bytes]:
    proc = subprocess.run(  # noqa: S603 - our own CLI, fixed argv, no shell
        list(argv), env=dict(env), capture_output=True, timeout=TIMEOUT, check=False
    )
    return proc.returncode, proc.stdout


def _reply(path: str) -> dict[str, Any]:
    if path.endswith("/api/chat"):  # Ollama
        return {
            "message": {"content": REPLY},
            "done_reason": "stop",
            "prompt_eval_count": INPUT_TOKENS,
            "eval_count": OUTPUT_TOKENS,
        }
    if path.endswith("/v1/messages"):  # Anthropic
        return {
            "content": [{"type": "text", "text": REPLY}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS},
        }
    # OpenAI-compatible: OpenRouter, LM Studio. No cost: the stub charges nothing.
    return {
        "choices": [{"message": {"content": REPLY}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": INPUT_TOKENS, "completion_tokens": OUTPUT_TOKENS},
    }


class _Handler(BaseHTTPRequestHandler):
    server: _StubServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)  # never kept: only the path is recorded
        self.server.paths.append(self.path)
        body = json.dumps(_reply(self.path)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _StubServer(ThreadingHTTPServer):
    paths: list[str]


@dataclass(frozen=True)
class Stub:
    """A running stub: its port on 127.0.0.1 and the path of each request it answered."""

    port: int
    paths: list[str] = field(default_factory=list)

    @property
    def requests(self) -> int:
        return len(self.paths)


@contextmanager
def stub_server() -> Iterator[Stub]:
    """Run the stub on 127.0.0.1 (a free port) while the block runs, then stop it."""
    server = _StubServer(("127.0.0.1", 0), _Handler)
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Stub(int(server.server_address[1]), server.paths)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@dataclass(frozen=True)
class SmokeResult:
    ok: bool
    output: str
    requests: int
    message: str


def _stub_urls(port: int) -> dict[str, str]:
    """Each provider's base URL on the stub: its built-in default's path on 127.0.0.1, so a
    request reaches the stub at the path the real API would get."""
    defaults = {f.metadata["env"]: f.metadata["default"] for f in setting_fields()}
    return {
        name: f"http://127.0.0.1:{port}{urlsplit(defaults[spec.url_setting]).path}"
        for name, spec in PROVIDERS.items()
    }


def stub_env(port: int, environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` with every provider pointed at the stub, placeholder keys and no history."""
    urls = _stub_urls(port)
    return {
        **environ,
        **{spec.url_setting: urls[name] for name, spec in PROVIDERS.items()},
        **{spec.key: PLACEHOLDER_KEY for spec in PROVIDERS.values() if spec.key},
        "PROMPT_HISTORY": "false",
    }


def stub_settings(cfg: Settings, port: int) -> Settings:
    """``cfg`` pointed at the stub as stub_env() points the environment: every provider's
    base URL, placeholder keys and no history."""
    changes: dict[str, Any] = {f"{name}_base_url": url for name, url in _stub_urls(port).items()}
    changes |= {f"{name}_api_key": PLACEHOLDER_KEY for name, spec in PROVIDERS.items() if spec.key}
    return replace(cfg, **changes, history=False)


def run(provider: str, *, runner: Runner | None = None) -> SmokeResult:
    """Run improve with ``provider`` against the stub; ok when it printed the stub's reply."""
    with stub_server() as stub:
        argv = [
            # -P (or the frozen build's own exe): a module planted in the working directory
            # never runs.
            *self_command(),
            "improve",
            "--provider",
            provider,
            "--source",
            "argument",
            "--text",
            DRAFT,
            "--timeout",
            "20",
        ]
        try:
            code, out = (runner or run_cli)(argv, stub_env(stub.port, os.environ))
        except (OSError, subprocess.SubprocessError) as exc:
            return SmokeResult(False, "", stub.requests, f"could not run the CLI: {exc}")
    output = out.decode("utf-8", "replace")
    if code == 0 and output == REPLY and stub.paths:
        return SmokeResult(
            True, output, stub.requests, "improve reached the stub and printed its reply"
        )
    return SmokeResult(False, output, stub.requests, output or f"exit code {code}, no output")
