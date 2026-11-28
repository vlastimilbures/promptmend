"""Replays every Espanso trigger's real argv (its script var's args) through the CLI and
checks what it would send: provider, profile (via the system prompt) and tier settings.
Testing each option on its own missed that a trigger's explicit --profile overrode the tier's
profile. Then runs each trigger's args as a real process, as Espanso does (#18)."""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest
import yaml
from conftest import FakeClock, resolved_settings
from typer.testing import CliRunner

from promptmend import smoke
from promptmend.cli import app
from promptmend.config import Settings
from promptmend.deploy import launcher_text
from promptmend.prompt_builder import PERSONA_PLACEHOLDER, PROFILES, system_prompt
from promptmend.providers.base import timed_out_message
from promptmend.providers.registry import PROVIDERS
from promptmend.trigger import WATCHDOG_MARGIN, _past

if TYPE_CHECKING:
    from conftest import HistoryRows, StubProvider

MATCH_DIR = Path(__file__).parents[1] / "espanso" / "match"
runner = CliRunner()


def _improve_commands() -> dict[str, tuple[list[str], dict[str, Any]]]:
    """trigger -> (script args, form fields by form name) for every match that runs improve."""
    commands: dict[str, tuple[list[str], dict[str, Any]]] = {}
    for path in sorted(MATCH_DIR.glob("*.yml")):
        for match in yaml.safe_load(path.read_text(encoding="utf-8"))["matches"]:
            vars_ = match.get("vars", [])
            forms = {v["name"]: v["params"]["fields"] for v in vars_ if v.get("type") == "form"}
            for var in vars_:
                args = var.get("params", {}).get("args", [])
                if var.get("type") == "script" and args[1:2] == ["improve"]:
                    commands[match["trigger"]] = (args, forms)
    return commands


COMMANDS = _improve_commands()

# trigger -> (provider, profile, tier) with no profile settings in the environment.
EXPECTED = {
    "-i-": ("openrouter", "default", "standard"),
    "-iok-": ("openrouter", "default", "standard"),
    "-ip-": ("openrouter", "default", "pro"),
    "-if-": ("openrouter", "default", "pro"),
    "-il-": ("ollama", "general", "standard"),
    "-ilm-": ("lmstudio", "general", "standard"),
}


def _argv(trigger: str, **picks: str) -> list[str]:
    """The trigger's arguments after the CLI path, as Espanso runs them: each form field
    filled into its args item (set to ``picks`` or its default), and the draft passed as an
    argument, not the clipboard."""
    args, forms = COMMANDS[trigger]

    def fill(field: re.Match[str]) -> str:
        form, name = field.groups()
        return picks.get(name, str(forms[form][name]["default"]))

    argv = [re.sub(r"\{\{(\w+)\.(\w+)\}\}", fill, arg) for arg in args]
    assert argv[0] == "__PROMPT_WORKFLOW__"
    source = argv.index("--source")
    assert argv[source + 1] == "clipboard"
    argv[source : source + 2] = ["--source", "argument", "--text", "draft"]
    return argv[1:]


def _replay(stub: StubProvider, trigger: str, **picks: str) -> tuple[str, Settings, str]:
    """(provider name, settings, system prompt) the trigger's command hands the provider."""
    stub.built.clear()
    stub.options.clear()
    stub.calls.clear()
    result = runner.invoke(app, _argv(trigger, **picks))
    assert (result.exit_code, result.stdout) == (0, "improved")
    ((name, cfg),) = stub.built
    (call,) = stub.calls
    return name, cfg, call["system_prompt"]


def _profile(prompt: str) -> str:
    names = [name for name in PROFILES if prompt == system_prompt(name)]
    assert len(names) == 1, "the system prompt matches no single profile"
    return names[0]


# Every trigger that runs improve has an expected row, so a new one cannot skip this test.
def test_every_improve_trigger_is_expected() -> None:
    assert set(COMMANDS) == set(EXPECTED)


# Each trigger builds its provider with the expected profile and tier settings.
@pytest.mark.parametrize("trigger", list(EXPECTED))
def test_trigger_request(stub_provider: StubProvider, trigger: str) -> None:
    provider, profile, tier = EXPECTED[trigger]
    name, cfg, prompt = _replay(stub_provider, trigger)
    base = resolved_settings()
    assert name == provider
    assert _profile(prompt) == profile
    if tier == "pro":
        assert cfg.openrouter_model == base.openrouter_pro_model
        assert cfg.openrouter_provider == base.openrouter_pro_provider
        assert cfg.openrouter_reasoning_effort == base.openrouter_pro_reasoning_effort
        assert cfg.timeout == base.pro_timeout
    else:
        assert cfg.openrouter_model == base.openrouter_model
        assert cfg.openrouter_reasoning_effort == base.openrouter_reasoning_effort
        assert cfg.timeout == base.timeout


# -ip- names no provider, so PROMPT_PRO_PROVIDER moves it to Anthropic or OpenAI with their own
# model and the pro time limit and profile; -if- names OpenRouter and stays there (#278).
@pytest.mark.parametrize("pro_provider", ["anthropic", "openai"])
def test_pro_provider_moves_ip_only(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, pro_provider: str
) -> None:
    monkeypatch.setenv("PROMPT_PRO_PROVIDER", pro_provider)
    monkeypatch.setenv("PROMPT_PRO_PROFILE", "general")
    base = resolved_settings()
    name, cfg, prompt = _replay(stub_provider, "-ip-")
    spec = PROVIDERS[pro_provider]
    assert (name, spec.model(cfg), cfg.timeout) == (pro_provider, spec.model(base), 45.0)
    assert _profile(prompt) == "general"
    name, cfg, _ = _replay(stub_provider, "-if-")
    assert (name, cfg.openrouter_model) == ("openrouter", base.openrouter_pro_model)


# With PROMPT_PRO_PROVIDER unset, -ip- runs OpenRouter's pro tier whatever PROMPT_PROVIDER says,
# as when it named OpenRouter itself (#278).
@pytest.mark.parametrize("configured", ["ollama", "anthropic"])
def test_ip_ignores_prompt_provider(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, configured: str
) -> None:
    monkeypatch.setenv("PROMPT_PROVIDER", configured)
    monkeypatch.delenv("PROMPT_PRO_PROVIDER", raising=False)
    base = resolved_settings()
    name, cfg, _ = _replay(stub_provider, "-ip-")
    assert name == "openrouter"
    assert (cfg.openrouter_model, cfg.openrouter_provider) == (
        base.openrouter_pro_model,
        base.openrouter_pro_provider,
    )
    assert (cfg.openrouter_reasoning_effort, cfg.timeout) == ("low", base.pro_timeout)


# OPENROUTER_PRO_MAX_TOKENS reaches the pro triggers only; the -if- popup's max tokens pick
# beats it and its `default` keeps it (#31).
def test_pro_max_tokens_reach_triggers(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider
) -> None:
    monkeypatch.setenv("OPENROUTER_PRO_MAX_TOKENS", "4000")
    for trigger, (_, _, tier) in EXPECTED.items():
        _, cfg, _ = _replay(stub_provider, trigger)
        assert cfg.openrouter_max_tokens == (4000 if tier == "pro" else 2400), trigger
        assert (cfg.anthropic_max_tokens, cfg.call_max_tokens) == (8000, None), trigger
    for pick, cap in (("default", 4000), ("2400", 2400), ("16000", 16000)):
        _, cfg, _ = _replay(stub_provider, "-if-", maxtokens=pick)
        assert cfg.openrouter_max_tokens == cap


# -if- model choice -> profile with the shipped settings: one prompt for every model. A set
# PROMPT_PRO_PROFILE applies to the pro model only (test_profile_settings_reach_triggers).
IF_MODEL_PROFILES = {
    "default": "default",
    "openai/gpt-6-luna@openai": "default",
    "openai/gpt-6-luna@auto": "default",
    "google/gemini-3.8-flash@google-ai-studio": "default",
    "google/gemini-3.5-flash-lite@google-ai-studio/flex": "default",
}


@pytest.mark.parametrize(("model", "profile"), list(IF_MODEL_PROFILES.items()))
def test_if_profile_follows_model(stub_provider: StubProvider, model: str, profile: str) -> None:
    _, cfg, prompt = _replay(stub_provider, "-if-", model=model)
    slug, _, endpoint = model.partition("@")
    assert _profile(prompt) == profile
    if model == "default":
        slug, endpoint = (
            resolved_settings().openrouter_pro_model,
            resolved_settings().openrouter_pro_provider,
        )
    assert cfg.openrouter_model == slug
    assert cfg.openrouter_provider == ("" if endpoint == "auto" else endpoint)


# The popup's `default` model keeps the OPENROUTER_PRO_* model and endpoint, whatever they are
# set to, and with them PROMPT_PRO_PROFILE.
def test_if_default_model_keeps_pro_settings(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider
) -> None:
    monkeypatch.setenv("OPENROUTER_PRO_MODEL", "vendor/custom-model")
    monkeypatch.setenv("OPENROUTER_PRO_PROVIDER", "custom-endpoint")
    monkeypatch.setenv("PROMPT_PRO_PROFILE", "general")
    _, cfg, prompt = _replay(stub_provider, "-if-", model="default")
    assert (cfg.openrouter_model, cfg.openrouter_provider) == (
        "vendor/custom-model",
        "custom-endpoint",
    )
    assert _profile(prompt) == "general"


# The table above covers every model the -if- popup offers.
def test_if_model_choices_are_covered() -> None:
    _, forms = COMMANDS["-if-"]
    assert set(forms["form1"]["model"]["values"]) == set(IF_MODEL_PROFILES)


# The profile settings reach the triggers: PROMPT_PROFILE for -i- (and a non-pro -if- model),
# PROMPT_PRO_PROFILE for the pro tier, which falls back to PROMPT_PROFILE when empty.
@pytest.mark.parametrize(
    ("env", "trigger", "picks", "profile"),
    [
        ({"PROMPT_PROFILE": "general"}, "-i-", {}, "general"),
        ({"PROMPT_PRO_PROFILE": "general"}, "-ip-", {}, "general"),
        ({"PROMPT_PRO_PROFILE": ""}, "-ip-", {}, "default"),
        ({"PROMPT_PRO_PROFILE": "", "PROMPT_PROFILE": "general"}, "-ip-", {}, "general"),
        ({"PROMPT_PRO_PROFILE": "general"}, "-if-", {}, "general"),
        (
            {"PROMPT_PROFILE": "general"},
            "-if-",
            {"model": "google/gemini-3.5-flash-lite@google-ai-studio/flex"},
            "general",
        ),
        (
            {"PROMPT_PRO_PROFILE": "general"},
            "-if-",
            {"model": "google/gemini-3.8-flash@google-ai-studio"},
            "default",
        ),
        ({"PROMPT_PROFILE": "default-pro"}, "-il-", {}, "general"),
    ],
    ids=[
        "i-profile",
        "ip-pro-profile",
        "ip-empty-pro-profile",
        "ip-empty-pro-profile-falls-back",
        "if-pro-profile",
        "if-other-model-profile",
        "if-other-model-loses-pro-profile",
        "il-keeps-general",
    ],
)
def test_profile_settings_reach_triggers(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: StubProvider,
    env: dict[str, str],
    trigger: str,
    picks: dict[str, str],
    profile: str,
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _, _, prompt = _replay(stub_provider, trigger, **picks)
    assert _profile(prompt) == profile


# The retired `default-pro` (an alias of default from 0.14.0 until #414) fails as any unknown
# profile does: a marker naming it and the valid ones, exit 0, nothing on stderr, no call.
@pytest.mark.parametrize(
    ("name", "trigger"),
    [("PROMPT_PRO_PROFILE", "-ip-"), ("PROMPT_PROFILE", "-i-"), ("--profile", "-i-")],
)
def test_retired_default_pro_is_an_unknown_profile(
    monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider, name: str, trigger: str
) -> None:
    argv = _argv(trigger)
    if name == "--profile":
        argv += ["--profile", "default-pro"]
    else:
        monkeypatch.setenv(name, "default-pro")
    result = runner.invoke(app, argv)
    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        "[promptmend: Unknown profile: 'default-pro'. Choose from: default, general]",
        "",
    )
    assert stub_provider.calls == []


# An explicit --profile beats the tier's profile, so no pro-tier trigger may pass one.
def test_pro_triggers_pass_no_profile() -> None:
    for trigger, (args, _) in COMMANDS.items():
        if "pro" in [b for a, b in itertools.pairwise(args) if a == "--tier"]:
            assert "--profile" not in args, trigger


# Only -iok- sends a flagged draft per call; every other trigger keeps the gate's block.
# (Each build also gets the usage-history observer, so only allow_flagged is compared.)
@pytest.mark.parametrize("trigger", list(EXPECTED))
def test_only_iok_allows_flagged(stub_provider: StubProvider, trigger: str) -> None:
    _replay(stub_provider, trigger)
    assert [o["allow_flagged"] for o in stub_provider.options] == [trigger == "-iok-"]
    assert set(stub_provider.options[0]) == {"allow_flagged", "observer", "force_remote"}
    assert stub_provider.options[0]["force_remote"] is False


# Each trigger's real command is recorded once in the usage history, as that trigger.
@pytest.mark.parametrize("trigger", list(EXPECTED))
def test_trigger_is_recorded_as_itself(
    stub_provider: StubProvider, history_rows: HistoryRows, trigger: str
) -> None:
    _replay(stub_provider, trigger)
    (op,) = history_rows("operations")
    assert (op["origin"], op["trigger_id"], op["kind"], op["outcome"]) == (
        "espanso_managed",
        trigger,
        "improve",
        "ok",
    )
    assert op["profile_id"] == EXPECTED[trigger][1]


def _persona_argv() -> list[str]:
    match = next(
        m
        for m in yaml.safe_load((MATCH_DIR / "prompts-template.yml").read_text("utf-8"))["matches"]
        if m["trigger"] == "-p-"
    )
    (argv,) = [v["params"]["args"] for v in match["vars"] if v.get("type") == "script"]
    assert argv[0] == "__PROMPT_WORKFLOW__"
    return [str(arg) for arg in argv[1:]]


# -p- is counted once, through persona: its one CLI call, recorded as -p-.
def test_persona_trigger_is_recorded_once(history_rows: HistoryRows) -> None:
    result = runner.invoke(app, _persona_argv())
    assert result.exit_code == 0
    (op,) = history_rows("operations")
    assert (op["origin"], op["trigger_id"], op["kind"], op["outcome"]) == (
        "espanso_managed",
        "-p-",
        "persona",
        "ok",
    )
    assert history_rows("attempts") == []


# --- Each trigger's args as a real process (#18) ----------------------------------------------


def _script_vars() -> dict[str, list[str]]:
    """trigger -> the args of its script var that runs the CLI, for every match that has one,
    the commented-out -ic- and -io- included (each `# - trigger:` block, uncommented)."""
    found: dict[str, list[str]] = {}
    for path in sorted(MATCH_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        matches = yaml.safe_load(text)["matches"]
        for block in re.findall(r"^ *# - trigger:.*\n(?: *#.*\n?)*", text, flags=re.MULTILINE):
            matches += yaml.safe_load(re.sub(r"^( *)# ", r"\1", block, flags=re.MULTILINE))
        for match in matches:
            for var in match.get("vars", []):
                args = var.get("params", {}).get("args", [])
                if var.get("type") == "script" and args[:1] == ["__PROMPT_WORKFLOW__"]:
                    found[match["trigger"]] = args
    return found


SCRIPT_VARS = _script_vars()


def _launcher() -> list[str]:
    """What deploy puts in args[0]: the console script next to this interpreter (an editable
    or CI install has one; on Windows a .exe launcher, given with forward slashes as deploy
    writes it), else the same entry point through the interpreter. With PROMPTMEND_EXE set
    (the CI job that builds the frozen Windows zip, #185), that exe instead."""
    # A frozen exe likely ignores PYTHONWARNINGS/PYTHONDEVMODE (the bootloader's own config),
    # so _LOUD may not apply there; the exit code and empty stderr checks still hold.
    if frozen := os.environ.get("PROMPTMEND_EXE"):
        return [launcher_text(Path(frozen).resolve())]
    name = "promptmend.exe" if os.name == "nt" else "promptmend"
    script = Path(sys.executable).parent / name
    if script.is_file():
        return [launcher_text(script)]
    return [sys.executable, "-m", "promptmend.entry"]


def test_every_cli_trigger_has_a_script_var() -> None:
    assert set(SCRIPT_VARS) == {*EXPECTED, "-ic-", "-io-", "-p-"}


# Espanso starts args[0] with the other items as its arguments, no shell, and treats a nonzero
# exit or any stderr output as a failed expansion (espanso-render's script.rs). So each
# trigger's args, run as a process exactly as listed (form fields at their defaults, the draft
# as an argument instead of the clipboard, every provider pointed at the local stub, the usage
# history on), exit 0, print the stub's reply on stdout and nothing on stderr. On
# windows-latest this exercises the real CreateProcess path.
# Every warning shown, as a user's PYTHONWARNINGS or a dev build might: none may reach stderr.
_LOUD = {"PYTHONWARNINGS": "always", "PYTHONDEVMODE": "1"}


def _process_argv(trigger: str) -> list[str]:
    """The trigger's process argv as Espanso starts it: the launcher, then its args (form
    fields at their defaults, the draft as an argument instead of the clipboard)."""
    args = list(SCRIPT_VARS[trigger])
    if trigger in COMMANDS:
        args = ["__PROMPT_WORKFLOW__", *_argv(trigger)]
    elif args[1] == "improve":  # -ic- and -io-, commented out: no form, the same draft swap
        source = args.index("--source")
        args[source : source + 2] = ["--source", "argument", "--text", "draft"]
    return [*_launcher(), *args[1:]]


def _process_env(port: int, **extra: str) -> dict[str, str]:
    return {**smoke.stub_env(port, os.environ), "PROMPT_HISTORY": "true", **_LOUD, **extra}


@pytest.mark.parametrize("trigger", sorted(SCRIPT_VARS))
def test_trigger_args_run_as_a_process(trigger: str) -> None:
    improve = SCRIPT_VARS[trigger][1] == "improve"
    with smoke.stub_server() as stub:
        proc = subprocess.run(
            _process_argv(trigger),
            env=_process_env(stub.port),
            capture_output=True,
            timeout=60,
            check=False,
        )
    assert (proc.returncode, proc.stderr) == (0, b"")
    if improve:
        assert proc.stdout.decode("utf-8") == smoke.REPLY
        assert len(stub.paths) == 1
    else:
        assert proc.stdout.decode("utf-8") == PERSONA_PLACEHOLDER
        assert stub.paths == []


# --- Each trigger as a process against a slow stub (#309) -------------------------------------

# Every trigger that calls a provider (-p- only prints its placeholder and calls none).
SLOW_TRIGGERS = sorted(t for t, args in SCRIPT_VARS.items() if args[1] == "improve")
# A 1 s limit through the environment for both tiers. Only -if- passes --timeout, and its
# form default `default` keeps the setting (test_slow_triggers_keep_the_env_limit).
LIMIT = 1.0
LIMIT_ENV = {"PROMPT_TIMEOUT_SECONDS": str(LIMIT), "PROMPT_PRO_TIMEOUT_SECONDS": str(LIMIT)}
# Process start-up on a loaded CI runner (windows-latest above all), with every slow run at
# once, on top of the limit plus trigger.WATCHDOG_MARGIN.
SLACK = 20.0


def _provider(trigger: str) -> str:
    args = SCRIPT_VARS[trigger]
    if "--provider" in args:
        return args[args.index("--provider") + 1]
    # -ip- names none, so it runs on PROMPT_PRO_PROVIDER (OpenRouter as shipped).
    return resolved_settings().provider_for(
        args[args.index("--tier") + 1] if "--tier" in args else ""
    )


def _timed_run(
    argv: list[str], env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[bytes], float]:
    started = time.monotonic()
    proc = subprocess.run(
        argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=90, check=False
    )
    return proc, time.monotonic() - started


def _used_the_limit(limit: float) -> str:
    """A regex for the markers of a run whose start-up (and settings load) already used up
    ``limit`` before its call (trigger.Watchdog.arm_loading() and arm()): it sent nothing."""
    past = re.escape(_past(limit))
    return (
        rf"\[promptmend: (?:starting up took \d+\.\ds, {past}"
        rf"|the settings took \d+\.\ds to load, \d+\.\ds in all, {past})\]"
    )


# The regex follows the watchdog's real wording: each used-the-limit marker it makes matches,
# the timed-out marker does not.
@pytest.mark.parametrize(("startup", "load"), [(5.0, 0.0), (0.0, 5.0)], ids=["start", "load"])
def test_used_the_limit_matches_the_watchdog(startup: float, load: float) -> None:
    from promptmend import recorder
    from promptmend.pipeline import Rewrite
    from promptmend.trigger import Watchdog

    clock = FakeClock()  # its alarm never wakes on its own
    watch = Watchdog(recorder.Recorder("improve", "i"), Rewrite(), clock=clock, alarms=clock.alarm)
    now = clock()
    watch._started, watch._load_started = now - startup - load, now - load
    marker = watch.arm(Settings(timeout=LIMIT), "openrouter")
    watch.cancel()
    assert marker is not None
    assert re.fullmatch(_used_the_limit(LIMIT), marker)
    timed_out = f"[promptmend: {timed_out_message('OpenRouter', LIMIT)}]"
    assert not re.fullmatch(_used_the_limit(LIMIT), timed_out)


def test_slow_triggers_keep_the_env_limit() -> None:
    for trigger in SLOW_TRIGGERS:
        argv = _process_argv(trigger)
        if "--timeout" in argv:
            assert argv[argv.index("--timeout") + 1] == "default", trigger


# What Espanso sees from every trigger's real argv, all run at once to keep the suite fast,
# each against its own stub: a reply after about 3 s within the default limits is pasted as
# is and is that trigger's one request, to its provider's path; a stub that never answers,
# under the 1 s limit, gives the timed-out marker, or, when a loaded runner's start-up alone
# took that second, the marker saying so, with no request sent (#331). Either way exit 0 and
# empty stderr, within the limit plus the margin. The name matches the frozen Windows job's
# `-k process`, so it also runs through the exe.
def test_trigger_processes_against_a_slow_stub() -> None:
    with contextlib.ExitStack() as stack:
        slow = {t: stack.enter_context(smoke.stub_server(3.0)) for t in SLOW_TRIGGERS}
        late = {t: stack.enter_context(smoke.stub_server(math.inf)) for t in SLOW_TRIGGERS}
        pool = stack.enter_context(ThreadPoolExecutor(max_workers=2 * len(SLOW_TRIGGERS)))
        envs = {t: _process_env(slow[t].port) for t in SLOW_TRIGGERS}
        replies = {t: pool.submit(_timed_run, _process_argv(t), envs[t]) for t in SLOW_TRIGGERS}
        markers = {
            t: pool.submit(_timed_run, _process_argv(t), _process_env(late[t].port, **LIMIT_ENV))
            for t in SLOW_TRIGGERS
        }
        for trigger, future in replies.items():
            proc, _ = future.result()
            assert (proc.returncode, proc.stderr) == (0, b""), trigger
            assert proc.stdout.decode("utf-8") == smoke.REPLY, trigger
            base = urlsplit(envs[trigger][PROVIDERS[_provider(trigger)].url_setting]).path
            (path,) = slow[trigger].paths
            assert path.startswith(f"{base}/"), trigger
        for trigger, future in markers.items():
            proc, seconds = future.result()
            label = PROVIDERS[_provider(trigger)].label
            assert (proc.returncode, proc.stderr) == (0, b""), trigger
            out = proc.stdout.decode("utf-8")
            assert seconds <= LIMIT + WATCHDOG_MARGIN + SLACK, trigger
            args = SCRIPT_VARS[trigger]
            tier = args[args.index("--tier") + 1] if "--tier" in args else "standard"
            # The pro tier names its own limit setting (-ip-, -if- with its timeout at default).
            setting = "PROMPT_PRO_TIMEOUT_SECONDS" if tier == "pro" else "PROMPT_TIMEOUT_SECONDS"
            hint = f"try again, or raise {setting}"
            if out == f"[promptmend: {timed_out_message(label, LIMIT, hint)}]":
                # The watchdog may end a run on a slow runner before its request goes out.
                assert late[trigger].requests <= 1, trigger
            else:
                assert re.fullmatch(_used_the_limit(LIMIT), out), (trigger, out)
                assert late[trigger].requests == 0, trigger


# --- The default source as a process (#407) ----------------------------------------------------

# The child refuses the clipboard (and skips the concealed-item probe) before the CLI loads,
# so a run that read it would print a clipboard marker instead of the reply.
_NO_CLIPBOARD = """
import sys

from promptmend import entry

entry.quiet_trigger(sys.argv[1:])
import pyperclip

import promptmend.pipeline as pipeline


def refuse(*args):
    raise pyperclip.PyperclipException("this test never touches the clipboard")


pyperclip.paste = pyperclip.copy = refuse
pipeline.is_concealed = lambda: None
sys.argv = ["promptmend", *sys.argv[1:]]
entry.main()
"""


# A draft piped to `improve` with no --source is what reaches the provider, on every OS (a
# pipe is never a terminal): the stub's reply comes back, exit 0, empty stderr.
def test_piped_draft_without_source_as_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[bytes] = []
    do_post = smoke._Handler.do_POST

    def recording(handler: Any) -> None:
        data = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
        bodies.append(data)
        handler.rfile = io.BytesIO(data)
        do_post(handler)

    monkeypatch.setattr(smoke._Handler, "do_POST", recording)
    argv = [sys.executable, "-c", _NO_CLIPBOARD, "improve", "--provider", "ollama"]
    with smoke.stub_server() as stub:
        proc = subprocess.run(
            [*argv, "--trigger-id", "il"],
            input=b"the piped draft",
            env=_process_env(stub.port),
            capture_output=True,
            timeout=60,
            check=False,
        )
    assert (proc.returncode, proc.stderr) == (0, b"")
    assert proc.stdout.decode("utf-8") == smoke.REPLY
    (body,) = bodies
    assert "the piped draft" in json.dumps(json.loads(body))
