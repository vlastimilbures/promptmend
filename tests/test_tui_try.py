"""The Try tab (#111), driven headless through Pilot: a typed draft rewritten against the
set-up stub on 127.0.0.1 (no provider, no history) or, after a confirmation, a real provider
answered by fake_http (recorded as a direct call). The clipboard is a fake that fails the
test if touched; no test reaches the network."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pyperclip
import pytest
from test_tui_snapshots import fixed_state, quick_of
from textual.pilot import Pilot
from textual.widgets import Button, Select, Static, TabbedContent, TextArea

from promptmend import clipboard_guard, pipeline, prompt_builder, smoke, try_run
from promptmend.config import ConfigLayers
from promptmend.pipeline import MAX_DRAFT_CHARS
from promptmend.tui import teach
from promptmend.tui.app import ManageApp
from promptmend.tui.modals import ConfirmModal
from promptmend.tui.try_pane import CONFIGURED, REAL, STUB, TryPane

if TYPE_CHECKING:
    from conftest import FakeHttp, HistoryRows

    from promptmend.tui.state import State

# Built at runtime, so no key-shaped literal lands in the repo (gitleaks).
KEY = "sk-or-v1-" + "cd34" * 16
DRAFT = "Write a short plan for the quarterly report review."
REPLY = {
    "id": "gen-abc123",
    "model": "google/gemini-3.5-flash-lite",
    "choices": [{"message": {"content": "A better prompt."}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 30, "cost": 0.0012},
}
COMMAND = (
    "promptmend improve --provider openrouter --tier standard "
    "--source argument --text '<draft withheld>'"
)


@pytest.fixture(autouse=True)
def no_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Try tab never reads or writes the clipboard, nor asks whether it is concealed."""

    def touched(*_: Any) -> Any:
        raise AssertionError("the Try tab touched the clipboard")

    monkeypatch.setattr(pyperclip, "paste", touched)
    monkeypatch.setattr(pyperclip, "copy", touched)
    monkeypatch.setattr(clipboard_guard, "is_concealed", touched)
    monkeypatch.setattr(pipeline, "is_concealed", touched)


@pytest.fixture
def loopback_only(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """For stub runs: any request to a host other than 127.0.0.1 fails the test. Returns the
    URLs of the requests sent (all to the stub)."""
    sent: list[str] = []
    real_send = httpx.Client.send

    def send(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        assert request.url.host == "127.0.0.1", f"a stub run reached {request.url}"
        sent.append(str(request.url))
        return real_send(client, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    return sent


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "-".join(("test", "key")))


async def settle(pilot: Pilot[int]) -> None:
    for _ in range(4):
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
    await pilot.pause()


@dataclass(frozen=True)
class Seen:
    """The tab as the scenario left it, read before the app closes."""

    result: str
    usage: str
    stub_requests: int | None
    running: bool
    disabled: bool
    last_message: str
    session: list[teach.Entry]
    home_session: str


def drive(
    scenario: Callable[[ManageApp, Pilot[int]], Awaitable[None]],
    extra: dict[str, str] | None = None,
    profiles: list[prompt_builder.UserProfile] | None = None,
) -> Seen:
    """Run ``scenario`` on the Try tab. A fixed State (``extra`` added to its environment, read
    again on each reload, so a scenario may change it; ``profiles`` in place of its own): the
    tab reads the (per-test) settings itself when it runs, and the State only for where "Run
    against" starts and for the profile picker's names."""

    def state(group_by: str) -> State:
        fixed = fixed_state(group_by, extra)
        return fixed if profiles is None else replace(fixed, profiles=profiles)

    app = ManageApp(
        loader=lambda g, _first: state(g),
        quick_loader=lambda g: quick_of(state(g)),
    )
    seen: list[Seen] = []

    async def main() -> None:
        async with app.run_test(size=(120, 50)) as pilot:
            await settle(pilot)
            await pilot.press("7")
            await scenario(app, pilot)
            pane = tab(app)
            seen.append(
                Seen(
                    pane.query_one("#try-result", TextArea).text,
                    str(pane.query_one("#try-usage", Static).render()),
                    pane.stub_requests,
                    pane.running,
                    pane.query_one("#try-run", Button).disabled,
                    pane.last_message,
                    list(app.session),
                    str(app.main.query_one("#home-session", Static).render()),
                )
            )

    asyncio.run(main())
    return seen[0]


def tab(app: ManageApp) -> TryPane:
    return app.main.query_one("#try-pane", TryPane)


async def run(app: ManageApp, pilot: Pilot[int], draft: str, **picks: str) -> None:
    """Type ``draft``, pick the options (provider, profile, tier, then the target, as a person
    picks it after the provider) and press Run."""
    pane = tab(app)
    pane.query_one("#try-draft", TextArea).load_text(draft)
    target = picks.pop("target", None)
    for name, value in picks.items():
        pane.query_one(f"#try-{name}", Select).value = value
    await pilot.pause()
    if target is not None:
        pane.query_one("#try-target", Select).value = target
        await pilot.pause()
    await pilot.click("#try-run")
    await settle(pilot)


@pytest.mark.usefixtures("loopback_only")
def test_stub_run_shows_the_reply_and_records_nothing(history_rows: HistoryRows) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=STUB)

    seen = drive(scenario)
    assert seen.result == smoke.REPLY
    line = seen.usage
    assert "1 request" in line
    assert f"tokens in {smoke.INPUT_TOKENS}, out {smoke.OUTPUT_TOKENS}" in line
    assert "cost not applicable (dry run)" in line
    assert seen.stub_requests == 1
    assert history_rows("operations") == []
    # The equivalent command would call the real provider: a stub run is not in the log.
    assert seen.session == []
    assert not seen.disabled


@pytest.mark.usefixtures("loopback_only")
@pytest.mark.parametrize("provider", ["ollama", "anthropic", "lmstudio"])
def test_stub_run_with_each_provider_shape(provider: str) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, provider=provider, profile="general", target=STUB)

    seen = drive(scenario)
    assert seen.result == smoke.REPLY
    assert seen.stub_requests == 1


@pytest.mark.usefixtures("loopback_only")
def test_gate_blocks_a_key_before_the_stub_is_called() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, f"Use the key {KEY} to call the API.", target=STUB)

    seen = drive(scenario)
    assert seen.result.startswith("[promptmend: ")
    assert seen.stub_requests == 0
    assert "no request was sent" in seen.usage
    assert seen.last_message == seen.result


@pytest.mark.parametrize(
    ("draft", "marker"),
    [
        ("", "[promptmend: Input is empty]"),
        ("  \n ", "[promptmend: Input is empty]"),
        (
            "x" * (MAX_DRAFT_CHARS + 1),
            f"[promptmend: Input is too long (over {MAX_DRAFT_CHARS} chars)]",
        ),
    ],
    # Short ids: pytest puts the test id in PYTEST_CURRENT_TEST, and Windows caps an
    # environment variable at 32767 characters.
    ids=["empty", "blank", "too-long"],
)
@pytest.mark.usefixtures("loopback_only")
def test_empty_or_too_long_draft(draft: str, marker: str) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, draft, target=STUB)

    seen = drive(scenario)
    assert seen.result == marker
    assert seen.stub_requests == 0


@pytest.mark.usefixtures("loopback_only")
def test_pro_tier_on_another_provider_is_refused() -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, provider="ollama", tier="pro", target=STUB)

    seen = drive(scenario)
    assert seen.result == (
        "[promptmend: --tier pro applies only to OpenRouter, Anthropic and OpenAI, not 'ollama']"
    )
    assert seen.stub_requests is None


def test_real_run_cancelled_sends_nothing(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    notes: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=REAL)
        dialog = app.screen
        assert isinstance(dialog, ConfirmModal)
        notes.append(dialog.preview)
        # Cancel has the focus: Enter changes nothing.
        await pilot.press("enter")
        await settle(pilot)

    seen = drive(scenario)
    assert fake_http.requests == []
    assert history_rows("operations") == []
    assert seen.result == ""
    (preview,) = notes
    assert "Provider: openrouter" in preview
    assert "Model: google/gemini-3.5-flash-lite" in preview
    assert "Base URL: https://openrouter.ai/api/v1" in preview
    assert f"Draft: {len(DRAFT)} characters" in preview
    assert "recorded in the usage history as -try- (a direct call" in preview
    assert DRAFT not in preview


def test_real_run_confirmed_is_one_call_recorded_as_direct(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    fake_http.reply(REPLY)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=REAL)
        await pilot.press("y")
        await settle(pilot)

    seen = drive(scenario)
    assert len(fake_http.requests) == 1
    assert fake_http.calls[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen.result == "A better prompt."
    line = seen.usage
    assert "tokens in 120, out 30" in line
    assert "cost 0.0012 credits reported" in line
    (op,) = history_rows("operations")
    assert (op["origin"], op["trigger_id"], op["kind"], op["outcome"], op["profile_id"]) == (
        "direct",
        "-try-",
        "improve",
        "ok",
        "default",
    )
    (attempt,) = history_rows("attempts")
    assert attempt["charged_amount"] == "0.0012"
    # The session log names the command, never the draft.
    assert seen.session[-1].command == COMMAND
    assert COMMAND in seen.home_session
    assert DRAFT not in seen.home_session


def test_real_run_failure_is_a_marker_and_its_outcome(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    fake_http.reply({"error": {"message": "no credit"}}, status_code=402)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=REAL)
        await pilot.press("y")
        await settle(pilot)

    seen = drive(scenario)
    assert seen.result.startswith("[promptmend: ")
    (op,) = history_rows("operations")
    assert (op["origin"], op["outcome"]) == ("direct", "error_marker")
    assert seen.session[-1].error
    assert "cost unknown" in seen.usage


def test_real_run_with_history_off_records_nothing(
    cloud: None,
    fake_http: FakeHttp,
    history_rows: HistoryRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", "false")
    fake_http.reply(REPLY)
    notes: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=REAL)
        assert isinstance(app.screen, ConfirmModal)
        notes.append(app.screen.preview)
        await pilot.press("y")
        await settle(pilot)

    seen = drive(scenario)
    assert "PROMPT_HISTORY is off" in notes[0]
    assert len(fake_http.requests) == 1
    assert history_rows("operations") == []
    # The usage line comes from the attempts even with the history off.
    assert "tokens in 120, out 30" in seen.usage


def counting_reloads(app: ManageApp) -> list[int]:
    """Counts the app's reloads from here on (the History tab reloads with it)."""
    reloads: list[int] = []
    real_reload = app.reload

    def reload() -> None:
        reloads.append(1)
        real_reload()

    app.reload = reload  # type: ignore[method-assign]
    return reloads


@pytest.mark.parametrize(("history", "expected"), [("true", 1), ("false", 0)], ids=["on", "off"])
def test_a_real_run_reloads_history_only_when_recorded(
    history: str,
    expected: int,
    cloud: None,
    fake_http: FakeHttp,
    history_rows: HistoryRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPT_HISTORY", history)
    fake_http.reply(REPLY)
    counts: list[int] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        reloads = counting_reloads(app)
        await run(app, pilot, DRAFT, target=REAL)
        await pilot.press("y")
        await settle(pilot)
        counts.append(len(reloads))

    seen = drive(scenario)
    assert seen.result == "A better prompt."
    assert counts == [expected]
    assert len(history_rows("operations")) == expected


@pytest.mark.usefixtures("loopback_only")
def test_a_stub_run_does_not_reload_history() -> None:
    counts: list[int] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        reloads = counting_reloads(app)
        await run(app, pilot, DRAFT, target=STUB)
        counts.append(len(reloads))

    seen = drive(scenario)
    assert seen.result == smoke.REPLY
    assert counts == [0]


def test_one_run_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    calls: list[str] = []

    def slow(raw: str, *args: Any, **kwargs: Any) -> str:
        calls.append(raw)
        release.wait(10)
        return "done"

    monkeypatch.setattr(pipeline, "rewrite", slow)
    during: list[bool] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        pane = tab(app)
        pane.query_one("#try-draft", TextArea).load_text(DRAFT)
        pane.query_one("#try-target", Select).value = STUB
        await pilot.pause()
        await pilot.click("#try-run")
        await pilot.pause()
        during.append(pane.running)
        during.append(pane.query_one("#try-run", Button).disabled)
        pane._run()  # a second Run while the first is under way
        await pilot.pause()
        release.set()
        await settle(pilot)

    seen = drive(scenario)
    assert calls == [DRAFT]
    assert during == [True, True]
    assert (seen.running, seen.disabled, seen.result) == (False, False, "done")


def test_a_crashed_worker_frees_the_run_button(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(try_run, "usage_line", broken)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=STUB)

    seen = drive(scenario)
    assert not seen.running
    assert seen.last_message == "error: unexpected RuntimeError: boom"


def test_pickers_start_at_the_settings_and_keep_a_pick() -> None:
    seen: list[tuple[str, str]] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        pane = tab(app)
        options = pane.query_one("#try-profile", Select)
        seen.append((str(pane.query_one("#try-provider", Select).value), str(options.value)))
        # fixed_state has a profile of the user's own.
        assert "mine" in [value for _, value in options._options]
        options.value = "mine"
        pane.query_one("#try-provider", Select).value = "ollama"
        app.reload()
        await settle(pilot)
        seen.append((str(pane.query_one("#try-provider", Select).value), str(options.value)))

    drive(scenario)
    assert seen == [("openrouter", CONFIGURED), ("ollama", "mine")]


def test_pro_tier_preview_names_the_pro_model(cloud: None, fake_http: FakeHttp) -> None:
    notes: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=REAL, tier="pro")
        assert isinstance(app.screen, ConfirmModal)
        notes.append(app.screen.preview)
        await pilot.press("escape")
        await settle(pilot)

    drive(scenario)
    assert "Model: openai/gpt-6-luna" in notes[0]
    # PROMPT_PRO_PROFILE is empty: the pro tier falls back to PROMPT_PROFILE.
    assert "Profile: default (as configured)" in notes[0]
    assert fake_http.requests == []


def test_the_send_dialog_names_the_pro_profile_as_configured(
    cloud: None, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPT_PRO_PROFILE", "general")
    notes: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        for tier in ("pro", "standard"):
            await run(app, pilot, DRAFT, target=REAL, tier=tier)
            assert isinstance(app.screen, ConfirmModal)
            notes.append(app.screen.preview)
            await pilot.press("escape")
            await settle(pilot)

    drive(scenario)
    assert "Profile: general (as configured)" in notes[0]
    assert "Profile: default (as configured)" in notes[1]
    assert fake_http.requests == []


# A profile's name may be 64 characters (#394): the closed picker cuts a long one, so the Send
# dialog names the picked profile whole, and two names alike up to the cut stay apart there.
LONG_PROFILES = ("customer-support-german-formal", "customer-support-german-casual")


def test_the_send_dialog_names_a_long_profile_whole(cloud: None, fake_http: FakeHttp) -> None:
    notes: list[str] = []
    profiles = [
        prompt_builder.UserProfile(name, Path(f"/home/me/profiles/{name}.md"), prompt_builder.ADDED)
        for name in LONG_PROFILES
    ]

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        options = tab(app).query_one("#try-profile", Select)
        assert [value for _, value in options._options][-2:] == list(LONG_PROFILES)
        for name in LONG_PROFILES:
            await run(app, pilot, DRAFT, target=REAL, profile=name)
            assert isinstance(app.screen, ConfirmModal)
            notes.append(app.screen.preview)
            await pilot.press("escape")
            await settle(pilot)

    drive(scenario, profiles=profiles)
    assert [line for n in notes for line in n.splitlines() if line.startswith("Profile:")] == [
        f"Profile: {name}" for name in LONG_PROFILES
    ]
    assert fake_http.requests == []


def test_escape_leaves_the_draft_so_the_tab_keys_work() -> None:
    seen: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        tab(app).query_one("#try-draft", TextArea).focus()
        await pilot.press("1")  # typed into the draft
        await pilot.press("escape", "1")
        seen.append(tab(app).query_one("#try-draft", TextArea).text)
        seen.append(str(app.main.query_one(TabbedContent).active))

    drive(scenario)
    assert seen == ["1", "home"]


# --- Review fixes: strict settings, recording, the pro profile, stub gating ----------------


@pytest.mark.parametrize(
    ("name", "value"),
    [("PROMPT_LOCAL_ONLY", "maybe"), ("PROMPT_EXTRA_PATTERNS", "(unclosed")],
)
@pytest.mark.parametrize("target", ["stub", REAL])
def test_an_invalid_setting_fails_closed(
    name: str,
    value: str,
    target: str,
    cloud: None,
    fake_http: FakeHttp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict settings, as a trigger loads them: repair mode would fall back to the default
    (no local-only refusal, no extra patterns) and send the draft."""
    monkeypatch.setenv(name, value)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=target)
        # No dialog: nothing to confirm when the settings do not load.
        assert not isinstance(app.screen, ConfirmModal)

    seen = drive(scenario)
    assert seen.result.startswith("[promptmend: ")
    assert name in seen.result
    assert fake_http.requests == []
    assert seen.stub_requests is None


def test_a_real_call_is_recorded_even_when_showing_it_fails(
    cloud: None,
    fake_http: FakeHttp,
    history_rows: HistoryRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_http.reply(REPLY)

    def lost(*_: Any) -> None:
        raise RuntimeError("the app went away")

    monkeypatch.setattr(TryPane, "_finish", lost)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=REAL)
        await pilot.press("y")
        await settle(pilot)

    seen = drive(scenario)
    assert len(fake_http.requests) == 1
    (op,) = history_rows("operations")
    assert (op["origin"], op["outcome"], op["profile_id"]) == ("direct", "ok", "default")
    assert op["latency_ms"] is not None
    assert history_rows("attempts")[0]["charged_amount"] == "0.0012"
    assert seen.last_message == "error: unexpected RuntimeError: the app went away"
    assert not seen.running


def test_pro_tier_uses_the_pro_profile_as_configured(
    cloud: None,
    fake_http: FakeHttp,
    history_rows: HistoryRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As -ip-: no --profile, so PROMPT_PRO_PROFILE applies on the pro model."""
    monkeypatch.setenv("PROMPT_PRO_PROFILE", "general")
    fake_http.reply(REPLY)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=REAL, tier="pro")
        await pilot.press("y")
        await settle(pilot)

    seen = drive(scenario)
    (call,) = fake_http.calls
    assert call["json"]["model"] == "openai/gpt-6-luna"
    assert call["json"]["messages"][0]["content"] == prompt_builder.system_prompt("general")
    (op,) = history_rows("operations")
    assert op["profile_id"] == "general"
    assert seen.session[-1].command == (
        "promptmend improve --provider openrouter --tier pro --source argument "
        "--text '<draft withheld>'"
    )


def test_a_picked_profile_is_passed_and_named(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    fake_http.reply(REPLY)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=REAL, profile="general")
        await pilot.press("y")
        await settle(pilot)

    seen = drive(scenario)
    (op,) = history_rows("operations")
    assert op["profile_id"] == "general"
    assert "--profile general --tier standard" in seen.session[-1].command


def test_pro_tier_stub_run(loopback_only: list[str]) -> None:
    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, tier="pro", target=STUB)

    seen = drive(scenario)
    assert seen.result == smoke.REPLY
    assert seen.stub_requests == 1
    (url,) = loopback_only
    assert url.endswith("/api/v1/chat/completions")


@pytest.mark.parametrize(
    ("name", "value", "model"),
    [
        ("OLLAMA_BASE_URL", "https://ollama.example.com", "qwen3:8b"),
        ("OLLAMA_BASE_URL", "http://localhost:11434", "gpt-oss:120b-cloud"),
    ],
)
def test_a_stub_run_is_gated_as_the_real_call_would_be(
    name: str,
    value: str,
    model: str,
    loopback_only: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote Ollama (or a cloud model) is gated for real; the stub run is too, though the
    stub settings point it at 127.0.0.1."""
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("OLLAMA_MODEL", model)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, f"Use the key {KEY} to call the API.", provider="ollama", target=STUB)

    seen = drive(scenario)
    assert seen.result.startswith("[promptmend: ")
    assert seen.stub_requests == 0
    assert loopback_only == []


def test_a_stub_run_is_refused_under_local_only_as_the_real_call_would_be(
    loopback_only: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPT_LOCAL_ONLY", "true")

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=STUB)

    seen = drive(scenario)
    assert seen.result.startswith("[promptmend: PROMPT_LOCAL_ONLY=true")
    assert seen.stub_requests == 0
    assert loopback_only == []


def test_a_local_stub_run_stays_ungated(
    loopback_only: list[str],
) -> None:
    """A loopback Ollama is not gated for real (without PROMPT_GATE_LOCAL), nor for the stub."""

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(
            app,
            pilot,
            "Rename the helper and keep the tests green.",
            provider="ollama",
            target=STUB,
        )

    seen = drive(scenario)
    assert seen.result == smoke.REPLY
    assert len(loopback_only) == 1


def test_stub_latency_times_the_call_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub's shutdown is not in the usage line's time.

    Timed against the stub's own block, not a fixed bound: the call runs inside it, so its
    time is at most the block's (a slow runner makes both longer alike, #355). Counting the
    0.6 s stop would put it at least 600 ms over."""
    import contextlib
    import time

    real_server = smoke.stub_server
    block: list[float] = []

    @contextlib.contextmanager
    def slow_stop() -> Any:
        with real_server() as stub:
            block.append(time.monotonic())  # before the call starts
            try:
                yield stub
            finally:
                block.append(time.monotonic())  # the stop starts
            time.sleep(0.6)

    monkeypatch.setattr(smoke, "stub_server", slow_stop)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, target=STUB)

    seen = drive(scenario)
    entered, stopping = block
    # Both clocks are monotonic, so the reading order holds; +1 for the usage line's rounding.
    assert seen.usage.startswith(f"{try_run.DRY_RUN_NOTE} · ")
    latency = seen.usage.removeprefix(f"{try_run.DRY_RUN_NOTE} · ").split(" ms")[0]
    assert int(latency) <= (stopping - entered) * 1000 + 1


@pytest.mark.parametrize(
    ("provider", "name", "value"),
    [
        ("openrouter", "OPENROUTER_BASE_URL", "http://relay.example/api/v1"),
        ("anthropic", "ANTHROPIC_BASE_URL", "http://relay.example"),
    ],
)
def test_a_stub_run_checks_the_real_base_url(
    provider: str,
    name: str,
    value: str,
    loopback_only: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real call would be refused (plain http to a cloud URL), so the stub run is too."""
    monkeypatch.setenv(name, value)

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        await run(app, pilot, DRAFT, provider=provider, target=STUB)

    seen = drive(scenario)
    assert seen.result == f"[promptmend: {name} must be an https:// URL]"
    assert seen.stub_requests == 0
    assert loopback_only == []


# --- Where "Run against" starts (#391) ------------------------------------------------------

# fixed_state has OPENROUTER_API_KEY set; this takes it away.
NO_KEY = {"OPENROUTER_API_KEY": ""}


def target(app: ManageApp) -> str:
    return str(tab(app).query_one("#try-target", Select).value)


async def pick(app: ManageApp, pilot: Pilot[int], name: str, value: str) -> None:
    tab(app).query_one(f"#try-{name}", Select).value = value
    await pilot.pause()


def test_a_usable_provider_starts_at_the_real_provider_and_rewrites(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows
) -> None:
    """The key is set: Run asks first (nothing sent before the confirm), then rewrites."""
    fake_http.reply(REPLY)
    seen_target: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        seen_target.append(target(app))
        await run(app, pilot, DRAFT)
        assert isinstance(app.screen, ConfirmModal)
        assert fake_http.requests == []
        await pilot.press("y")
        await settle(pilot)

    seen = drive(scenario)
    assert seen_target == [REAL]
    assert len(fake_http.requests) == 1
    assert seen.result == "A better prompt."
    assert try_run.DRY_RUN not in seen.usage
    assert seen.last_message == "Done: rewritten."
    assert len(history_rows("operations")) == 1


@pytest.mark.usefixtures("loopback_only")
def test_a_cloud_provider_without_a_key_starts_at_the_dry_run(history_rows: HistoryRows) -> None:
    """No key: the dry run, which says above its result and in its report that it is none."""
    seen_target: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        seen_target.append(target(app))
        await run(app, pilot, DRAFT)
        # No dialog: a dry run calls no provider.
        assert not isinstance(app.screen, ConfirmModal)

    seen = drive(scenario, NO_KEY)
    assert seen_target == [STUB]
    assert seen.result == smoke.REPLY
    assert seen.usage.startswith("Dry run (checks the wiring, no rewrite) · ")
    assert seen.last_message == "Done: dry run, the stub answered (no rewrite)."
    assert history_rows("operations") == []


def test_the_dry_run_is_named_for_what_it_is() -> None:
    seen: list[list[str]] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        options = tab(app).query_one("#try-target", Select)
        seen.append([str(prompt) for prompt, _ in options._options])

    drive(scenario)
    assert seen == [["Dry run", "Real provider"]]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        # No key for the cloud providers: only a local one is usable.
        (NO_KEY, {"openrouter": STUB, "anthropic": STUB, "ollama": REAL, "lmstudio": REAL}),
        # PROMPT_LOCAL_ONLY refuses a cloud provider though its key is set.
        (
            {"PROMPT_LOCAL_ONLY": "true"},
            {"openrouter": STUB, "anthropic": STUB, "ollama": REAL, "lmstudio": REAL},
        ),
        ({"ANTHROPIC_API_KEY": "-".join(("test", "key"))}, {"anthropic": REAL, "openai": STUB}),
    ],
    ids=["no-key", "local-only", "anthropic-key"],
)
def test_changing_the_provider_works_the_default_out_again(
    extra: dict[str, str], expected: dict[str, str]
) -> None:
    seen: dict[str, str] = {}

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        for provider in expected:
            await pick(app, pilot, "provider", provider)
            seen[provider] = target(app)

    drive(scenario, extra)
    assert seen == expected


def test_a_target_picked_by_hand_survives_a_provider_change_and_a_reload() -> None:
    seen: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        seen.append(target(app))  # the key is set
        await pick(app, pilot, "target", STUB)
        await pick(app, pilot, "provider", "ollama")  # usable, yet the pick stays
        seen.append(target(app))
        app.reload()
        await settle(pilot)
        seen.append(target(app))

    drive(scenario)
    assert seen == [REAL, STUB, STUB]


def test_a_real_target_picked_by_hand_stays_for_a_provider_without_a_key(
    cloud: None, fake_http: FakeHttp
) -> None:
    """Kept as picked, and still asks before the call."""
    seen: list[str] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        seen.append(target(app))
        await pick(app, pilot, "target", REAL)
        await pick(app, pilot, "provider", "anthropic")
        seen.append(target(app))
        await run(app, pilot, DRAFT)
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("escape")
        await settle(pilot)

    drive(scenario, NO_KEY)
    assert seen == [STUB, REAL]
    assert fake_http.requests == []


def test_a_reload_follows_a_key_set_elsewhere_until_a_pick() -> None:
    """A key saved on the Settings tab: the reload moves the default to the real provider.
    Once the dry run is picked by hand, a key changed again and a reload keep the pick."""
    extra = dict(NO_KEY)
    seen: list[str] = []

    async def reload_with(app: ManageApp, pilot: Pilot[int], key: str) -> None:
        extra["OPENROUTER_API_KEY"] = key
        app.reload()
        await settle(pilot)
        seen.append(target(app))

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        seen.append(target(app))
        await reload_with(app, pilot, "-".join(("test", "key")))
        await pick(app, pilot, "target", STUB)
        await reload_with(app, pilot, "")
        await reload_with(app, pilot, "-".join(("other", "key")))

    drive(scenario, extra)
    assert seen == [STUB, REAL, STUB, STUB]


def test_two_presses_open_one_dialog_and_one_send_is_one_call(
    cloud: None, fake_http: FakeHttp, history_rows: HistoryRows, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run pressed twice before the dialog is up: one dialog, so one Send, one request; and
    no second start while that run is under way, whoever asks."""
    fake_http.reply(REPLY)
    release = threading.Event()
    real_run = try_run.run

    def held(*args: Any, **kwargs: Any) -> try_run.Outcome:
        release.wait(10)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(try_run, "run", held)
    dialogs: list[int] = []

    async def scenario(app: ManageApp, pilot: Pilot[int]) -> None:
        pane = tab(app)
        pane.query_one("#try-draft", TextArea).load_text(DRAFT)
        assert target(app) == REAL
        button = pane.query_one("#try-run", Button)
        button.press()
        button.press()
        await pilot.pause()
        pane._run()  # a third ask while the dialog is open
        await pilot.pause()
        dialogs.append(sum(isinstance(s, ConfirmModal) for s in app.screen_stack))
        await pilot.press("y")
        await pilot.pause()
        assert pane.running
        # The run is held: another y, Run or a direct start sends nothing more.
        await pilot.press("y")
        pane._run()
        cfg = ConfigLayers.resolve().settings()
        pane._start(DRAFT, "openrouter", None, cfg, COMMAND)
        await pilot.pause()
        dialogs.append(sum(isinstance(s, ConfirmModal) for s in app.screen_stack))
        release.set()
        await settle(pilot)

    seen = drive(scenario)
    assert dialogs == [1, 0]
    assert len(fake_http.requests) == 1
    assert seen.result == "A better prompt."
    assert len(history_rows("operations")) == 1
