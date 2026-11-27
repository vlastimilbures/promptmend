"""What setup shows (the wizard in tui/setup_wizard.py and the text flow in commands/setup.py),
worked out with no Textual and no writes, so headless `setup` never loads the interface and
`tests/test_setup_guide.py` checks every word. The triggers pick
their own provider (`-i-` OpenRouter, `-il-` Ollama, …); PROMPT_PROVIDER only sets a bare
`promptmend improve` and the Try tab, so setup never asks for it (#390): it asks which
triggers you will use (cloud, local or both; local is PROMPT_LOCAL_ONLY=true), and the keys
asked for and the test run follow the triggers. Every trigger name shown is read from the
match files (assets.triggers()), never a list kept here."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import assets, deploy, factory, settings_checks
from .config import ConfigLayers, Settings, secret_names
from .prompt_builder import ADDED, PROFILES, UserProfile
from .providers.registry import PROVIDERS

# Step id -> the name in the left rail, in order.
STEPS = {
    "welcome": "Welcome",
    "use": "Cloud or local",
    "keys": "API keys",
    "profile": "Profile",
    "output": "Output",
    "espanso": "Espanso",
    "test": "Test",
    "done": "Done",
}

# The answers to "which triggers will you use" (`setup --use`). Only LOCAL is stored
# (PROMPT_LOCAL_ONLY=true); cloud and both are the setting's default, false.
CLOUD, LOCAL, BOTH = "cloud", "local", "both"
USES = (CLOUD, LOCAL, BOTH)

WELCOME = (
    "PromptMend rewrites a rough draft into a precise prompt: copy the draft, type a trigger "
    "such as -i- in any text field, and Espanso pastes the rewrite.\n\n"
    "This setup takes a few steps. Each one shows what it changes before it does, and you "
    "can skip any of them and come back later (`promptmend setup`, or Home > Setup… in "
    "`promptmend`).\n\n"
    "Move with Next and Back (Escape). Nothing calls a paid provider."
)

USE_INTRO = (
    "Which triggers will you use? Each names its own provider; your answer picks the key "
    "asked for and the path the test runs."
)

# `setup --provider` (deprecated, #390): no step of setup sets PROMPT_PROVIDER any more.
PROVIDER_DEPRECATED = (
    "--provider is deprecated: no trigger reads PROMPT_PROVIDER (each trigger names its own "
    "provider; it sets only a bare `promptmend improve` and the Try tab). --use replaces it; "
    "`promptmend config set PROMPT_PROVIDER` sets it."
)
PROVIDER_ON_A_TERMINAL = (
    "--provider is deprecated and the setup on a terminal has no step for it (no trigger "
    "reads PROMPT_PROVIDER): pass --use cloud|local|both, or --non-interactive to save it"
)

# What each built-in profile does, for the Profile step.
PROFILE_HELP = {
    "default": "rewrites into the CONTEXT / GOAL / … template, always in English",
    "general": "a short clean-up in the language of your draft",
}
PROFILE_INTRO = (
    "The profile is how a draft is rewritten. It applies to -i-, -iok-, -ip- and -if-; "
    "-il- and -ilm- always use general."
)

OUTPUT_CHOICES = {
    "paste": ("Paste", "replaces the trigger; do not type or switch away"),
    "clipboard": ("Clipboard", "on the clipboard (a clipboard history sees it)"),
}

KEYS_INTRO = (
    "Keys are saved in the secrets file (secrets.toml, a plaintext file readable only by "
    "your account), never in config.toml, and are never shown again. Leave a field empty "
    "to keep what is there."
)


# The Welcome step's note while PROMPTMEND_ENV is set.
LEGACY_NOTE = (
    "PROMPTMEND_ENV is set, so your settings and keys stay in that file; this setup cannot "
    "save them (edit that file instead)."
)


def history_line(on: bool) -> str:
    """The Welcome step's word on the usage history (D-HIST-0; `setup --non-interactive`
    prints usage.disclosure() in full)."""
    if not on:
        return "Usage history is off; nothing is recorded."
    return (
        "Usage history is on: metadata only (trigger, model, outcome, tokens, cost), never "
        "your text or keys, kept on this device. Off: `promptmend config set PROMPT_HISTORY "
        "false`."
    )


def legacy_keys_intro(env_file: str) -> str:
    """KEYS_INTRO while PROMPTMEND_ENV names ``env_file``: that file alone is read, so a key
    goes there and the step has nothing to save."""
    return (
        f"PROMPTMEND_ENV is set, so keys are read only from {env_file}: add each one there "
        "as NAME=value (this setup cannot save them)."
    )


def unread_env_note(env_files: Sequence[Path]) -> str | None:
    """What setup says about ``env_files`` (config.unread_env_files()): .env files earlier
    releases read and this one does not, whose settings this setup saves again; None without
    one."""
    if not env_files:
        return None
    files = ", ".join(map(str, env_files))
    return (
        f"{files} is no longer read: choose its settings again here (they are saved in "
        "config.toml and the secrets file), then remove it."
    )


ESPANSO_INTRO = (
    "Espanso runs PromptMend when you type a trigger. Its match files tell it which trigger "
    "runs what; installing them writes only PromptMend's own files into Espanso's match "
    "folder. A file you edited is kept as it is."
)

NEXT_STEP = (
    "Try it: copy a rough draft, click into any text field, type -i- and wait without "
    "typing or switching windows. The rewrite replaces the trigger."
)

# A match file's deploy state in words.
FILE_STATES = {
    deploy.MISSING: "not installed yet; will be written",
    deploy.IN_SYNC: "up to date",
    deploy.STALE: "from an older version; will be updated",
    deploy.MODIFIED: "you edited it; kept as it is",
    deploy.FOREIGN: "not written by PromptMend; kept as it is",
}


@dataclass(frozen=True)
class Choice:
    value: str
    title: str
    detail: str
    current: bool


@dataclass(frozen=True)
class KeyRow:
    name: str
    set: bool
    source: str | None
    # The active triggers that need it, in file order; empty when only PROMPT_PROVIDER does.
    triggers: tuple[str, ...]
    needed: bool


@dataclass(frozen=True)
class DoneRow:
    ok: bool
    label: str
    text: str


@dataclass(frozen=True)
class SmokeRun:
    """One smoke run of the test step: the provider a trigger runs on, and that trigger
    (None when no active trigger takes the path)."""

    trigger: str | None
    provider: str


def _paths(triggers: Sequence[assets.Trigger], settings: Settings) -> list[tuple[str, str]]:
    """Each active rewrite trigger with the provider it runs on, in file order."""
    on = {
        name: p
        for p, names in settings_checks.rewrites(triggers, settings).items()
        for name in names
    }
    return [(t.trigger, on[t.trigger]) for t in triggers if t.trigger in on]


def _label(provider: str) -> str:
    spec = PROVIDERS.get(provider)
    return spec.label if spec else provider


def local_note(triggers: Sequence[assets.Trigger], settings: Settings) -> str:
    """What "local" does, said before it is saved: the triggers PROMPT_LOCAL_ONLY=true refuses
    (each one whose provider can send the draft off this machine, factory.leaves_machine()),
    the ones left to use, and what the setting cannot see."""
    paths = _paths(triggers, settings)
    away = [(t, p) for t, p in paths if factory.leaves_machine(p, settings)]
    kept = [f"{t} ({_label(p)})" for t, p in paths if not factory.leaves_machine(p, settings)]
    text = "Local saves PROMPT_LOCAL_ONLY=true"
    if away:
        names = ", ".join(t for t, _ in away)
        labels = ", ".join(dict.fromkeys(_label(p) for _, p in away))
        text += f": {names} then answer with a refusal marker instead of calling {labels}"
    use = " or ".join(kept) if kept else "none: no trigger stays on this computer"
    return (
        f"{text}. The triggers to use are {use}. It refuses every provider that would send "
        "the draft off this computer (a server elsewhere, an Ollama cloud model); it cannot "
        "see a local server that passes the draft on. Cloud or both turn it off again."
    )


def use_choices(triggers: Sequence[assets.Trigger], settings: Settings) -> list[Choice]:
    """The Cloud or local step's answers, each with the triggers it means. Only local is
    stored (PROMPT_LOCAL_ONLY=true), so it alone can be marked current."""
    paths = _paths(triggers, settings)
    cloud = " ".join(t for t, p in paths if factory.leaves_machine(p, settings))
    local = " ".join(t for t, p in paths if not factory.leaves_machine(p, settings))
    return [
        Choice(
            CLOUD,
            "Cloud: an OpenRouter key",
            f"{cloud} · the draft goes to a cloud API; asks for its key",
            False,
        ),
        Choice(
            LOCAL,
            "Local: Ollama or LM Studio",
            f"{local} · PROMPT_LOCAL_ONLY=true: the cloud triggers then refuse",
            settings.local_only,
        ),
        Choice(BOTH, "Both", "every trigger above; asks for the cloud key", False),
    ]


def smoke_runs(use: str, triggers: Sequence[assets.Trigger], settings: Settings) -> list[SmokeRun]:
    """The smoke runs of the test step for the answer ``use``: the path of the first cloud
    trigger (-i-, OpenRouter) for cloud, of the first local one (-il-, Ollama) for local,
    both for both. Never PROMPT_PROVIDER, which no trigger reads."""
    paths = _paths(triggers, settings)
    runs = []
    for wanted, cloud, fallback in ((CLOUD, True, "openrouter"), (LOCAL, False, "ollama")):
        if use in (wanted, BOTH):
            found = next(((t, p) for t, p in paths if PROVIDERS[p].cloud == cloud), None)
            runs.append(SmokeRun(*found) if found else SmokeRun(None, fallback))
    return runs


def _path(run: SmokeRun) -> str:
    taken = f"the path {run.trigger} takes" if run.trigger else "no active trigger takes it"
    return f"`promptmend improve --provider {run.provider}` ({taken})"


def smoke_intro(runs: Sequence[SmokeRun]) -> str:
    """The test step's intro: which trigger's path each run takes."""
    return (
        f"Runs {' and '.join(map(_path, runs))} against a stub server on 127.0.0.1 that "
        "answers like the provider. No provider is called and your real key is never sent; "
        "it checks that the CLI starts and reads its settings."
    )


def smoke_result(run: SmokeRun, ok: bool, message: str) -> str:
    """One run's result line, naming the trigger whose path it took."""
    who = f"{run.trigger} ({run.provider})" if run.trigger else run.provider
    if ok:
        return f"{who}: passed: {message}"
    return f"{who}: failed (improve --provider {run.provider}): {message}"


def output_intro(settings: Settings) -> str:
    """The Output step's trade-off (docs/usage.md "Output: paste or clipboard"), with the
    timeouts the settings give."""
    return (
        f"Espanso shows nothing while a rewrite runs (up to {settings.timeout:g} s, "
        f"{settings.pro_timeout:g} s on the pro tier) and pastes it wherever the focus is when "
        "it arrives; with clipboard output the rewrite waits on the clipboard for you to "
        "paste instead."
    )


def output_choices(settings: Settings) -> list[Choice]:
    return [
        Choice(value, title, detail, value == settings.output)
        for value, (title, detail) in OUTPUT_CHOICES.items()
    ]


def profile_choices(settings: Settings, profiles: Sequence[UserProfile]) -> list[Choice]:
    own = [p for p in profiles if p.status == ADDED]
    found = [
        Choice(name, name, PROFILE_HELP.get(name, "built in"), name == settings.profile)
        for name in PROFILES
    ]
    found += [
        Choice(p.name, p.name, f"yours: {p.path.name}", p.name == settings.profile)
        for p in own
        if p.name not in PROFILES
    ]
    return found


def key_rows(
    layers: ConfigLayers, settings: Settings, triggers: Sequence[assets.Trigger]
) -> list[KeyRow]:
    """Every key, with the triggers that need it; a key is needed when an active trigger or
    PROMPT_PROVIDER runs on its provider, unless PROMPT_LOCAL_ONLY refuses that provider."""
    used = settings_checks.rewrites(triggers, settings)
    rows = []
    for route in factory.routes(settings):
        if not route.key or route.key not in secret_names():
            continue
        entry = layers.entries[route.key]
        names = tuple(t for t in used.get(route.name, []) if t)
        needed = not route.refused and bool(names or settings.provider == route.name)
        rows.append(
            KeyRow(
                route.key,
                bool(entry.value),
                entry.source if entry.value else None,
                names,
                needed,
            )
        )
    return rows


def key_line(row: KeyRow) -> str:
    where = f" ({_source(row.source)})" if row.source else ""
    status = f"set{where}" if row.set else "not set"
    if row.triggers and row.needed:
        use = f"needed by {', '.join(row.triggers)}"
    elif row.triggers:  # their provider is refused (key_rows())
        use = f"not needed: PROMPT_LOCAL_ONLY=true refuses {', '.join(row.triggers)}"
    elif row.needed:
        use = "needed by a bare `promptmend improve` (your provider)"
    else:
        use = "not needed by any active trigger"
    return f"{row.name}: {status}; {use}"


def no_key_line(settings: Settings) -> str:
    """`setup --non-interactive`'s line when no key is needed."""
    if settings.local_only:
        return "No key needed: PROMPT_LOCAL_ONLY=true refuses every trigger that would need one."
    return "No key needed: every active trigger and your provider run locally."


def _source(source: str) -> str:
    if source.startswith("file:"):
        return Path(source.removeprefix("file:")).name
    return "environment variable" if source == "env" else source


def file_rows(plan: deploy.Plan) -> list[tuple[str, str]]:
    return [(s.name, FILE_STATES.get(s.state, s.state)) for s in plan.steps]


def _writes(plan: deploy.Plan) -> list[deploy.FileStep]:
    return [s for s in plan.steps if s.state in (deploy.MISSING, deploy.STALE)]


def deploy_summary(plan: deploy.Plan) -> str:
    if plan.is_noop or plan.only_forgets:
        return "Every match file is up to date; nothing to install."
    writes = _writes(plan)
    if not writes:
        return "Nothing to write: the files that differ are yours and stay as they are."
    return f"Install writes {len(writes)} file(s) and then restarts Espanso."


def _use_row(settings: Settings, use: str, legacy: str | None) -> DoneRow:
    """The answer, checked against PROMPT_LOCAL_ONLY (which may not have been saved: legacy
    mode, or an environment variable that outranks config.toml)."""
    if use == LOCAL and settings.local_only:
        return DoneRow(True, "Triggers", "local (PROMPT_LOCAL_ONLY=true refuses the cloud ones)")
    if use == LOCAL:
        fix = (
            f"add PROMPT_LOCAL_ONLY=true to {legacy}"
            if legacy
            else "`promptmend config set PROMPT_LOCAL_ONLY true`"
        )
        text = f"local, but PROMPT_LOCAL_ONLY is off, so the cloud ones still call out: {fix}"
        return DoneRow(False, "Triggers", text)
    if settings.local_only:
        fix = (
            f"remove PROMPT_LOCAL_ONLY from {legacy}"
            if legacy
            else "`promptmend config unset PROMPT_LOCAL_ONLY`"
        )
        text = f"{use}, but PROMPT_LOCAL_ONLY=true refuses the cloud ones: {fix}"
        return DoneRow(False, "Triggers", text)
    return DoneRow(True, "Triggers", use)


def done_rows(
    settings: Settings,
    use: str,
    keys: Sequence[KeyRow],
    plan: deploy.Plan | None,
    test: bool | None,
    legacy: str | None = None,
    skipped: Sequence[str] = (),
) -> list[DoneRow]:
    """What is set and what is left; ``use`` is the Cloud or local step's answer and
    ``legacy`` the .env PROMPTMEND_ENV names, where a missing setting or key then goes. A step
    in ``skipped`` ("espanso", "test": left out by `setup --no-deploy`/`--no-smoke-test`) says
    so and is not a problem."""
    rows = [
        _use_row(settings, use, legacy),
        DoneRow(True, "Profile", settings.profile),
        DoneRow(True, "Output", settings.output),
    ]
    for key in keys:
        if not key.needed:
            continue
        if key.set:
            rows.append(DoneRow(True, key.name, "set"))
        else:
            fix = (
                f"add {key.name}= to {legacy}" if legacy else f"`promptmend secrets set {key.name}`"
            )
            rows.append(DoneRow(False, key.name, f"not set: {fix}"))
    if "espanso" in skipped:
        rows.append(DoneRow(True, "Match files", "skipped (--no-deploy)"))
    elif plan is None:
        rows.append(DoneRow(False, "Match files", "Espanso's folder was not found"))
    elif not _writes(plan):
        text = f"installed in {plan.espanso_dir / 'match'}"
        if plan.conflicts:
            text += f"; {len(plan.conflicts)} file(s) you changed were kept"
        rows.append(DoneRow(True, "Match files", text))
    else:
        rows.append(DoneRow(False, "Match files", "not installed: `promptmend espanso deploy`"))
    if "test" in skipped:
        rows.append(DoneRow(True, "Test", "skipped (--no-smoke-test)"))
    elif test is None:
        rows.append(DoneRow(False, "Test", "not run"))
    else:
        rows.append(DoneRow(test, "Test", "passed" if test else "failed (see the Test step)"))
    return rows
