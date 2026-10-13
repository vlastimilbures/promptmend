"""End-to-end checks against the real OpenRouter default model.

Opt-in: the tests marked `live` are excluded by default, run them with
`uv run pytest -m live`. They spend a fraction of a cent per run and need OPENROUTER_API_KEY
in the environment or the repo's .env. An unmarked offline twin runs the same command and
checks on a canned reply, so this file cannot break unseen between live runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from bench_module import GOOD, bench
from conftest import read_env_file
from typer.testing import CliRunner

from promptmend.cli import app
from promptmend.config import ENV_FILE_VARS

if TYPE_CHECKING:
    from conftest import FakeHttp

REPO = Path(__file__).resolve().parents[1]

# The CLI command both the live and the offline test run; the draft arrives on stdin.
IMPROVE = ["improve", "--provider", "openrouter", "--profile", "default", "--source", "stdin"]


def _setting(name: str) -> str:
    return os.environ.get(name) or (read_env_file(REPO / ".env") or {}).get(name) or ""


def _has_key() -> bool:
    # PROMPT_LOCAL_ONLY=true refuses OpenRouter, so there is nothing to test live.
    return bool(_setting("OPENROUTER_API_KEY")) and _setting("PROMPT_LOCAL_ONLY").lower() != "true"


def _improve(draft: str) -> subprocess.CompletedProcess[str]:
    # Run the CLI in a fresh process, the way Espanso does. The autouse fixture stripped
    # provider env vars and pointed PROMPTMEND_ENV at a temp file; drop that (and its alias)
    # so the CLI finds the repo .env, which is what configures this call.
    env = {k: v for k, v in os.environ.items() if k not in ENV_FILE_VARS}
    return subprocess.run(
        [sys.executable, "-m", "promptmend.cli", *IMPROVE],
        input=draft,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=90,
    )


needs_key = pytest.mark.skipif(
    not _has_key(), reason="OPENROUTER_API_KEY not configured, or PROMPT_LOCAL_ONLY=true"
)


def _assert_rewrite(returncode: int, out: str, draft: bench.Draft) -> None:
    assert returncode == 0
    assert not out.startswith("[promptmend:"), out
    assert not out.endswith("\n")
    # Model quality varies run to run; the structural checks must hold regardless.
    structural = [
        f for f in bench.check(out, draft.plan, draft.independent) if not f.startswith("branch:")
    ]
    assert structural == [], out


@pytest.mark.live
@needs_key
def test_live_rewrite_produces_golden_template() -> None:
    draft = bench.DRAFTS["board"]
    proc = _improve(draft.text)
    _assert_rewrite(proc.returncode, proc.stdout, draft)


# The same command and checks on a canned golden rewrite, run in every default test run.
def test_live_checks_run_offline(monkeypatch: pytest.MonkeyPatch, fake_http: FakeHttp) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_http.reply({"choices": [{"message": {"content": GOOD}}]})
    draft = bench.DRAFTS["board"]
    result = CliRunner().invoke(app, IMPROVE, input=draft.text)
    _assert_rewrite(result.exit_code, result.stdout, draft)
    assert [call["url"] for call in fake_http.calls] == [
        "https://openrouter.ai/api/v1/chat/completions"
    ]


# The gate blocks before any network call, so this one needs no credits, only a key.
@pytest.mark.live
@needs_key
def test_live_sensitive_draft_is_blocked() -> None:
    proc = _improve("customer data for card 4111 1111 1111 1111")
    assert proc.returncode == 0
    assert proc.stdout.startswith("[promptmend: Blocked cloud call")
