"""What setup shows (the wizard in tui/setup_wizard.py and the text flow in commands/setup.py),
worked out with no Textual and no writes, so headless `setup` never loads the interface and
`tests/test_setup_guide.py` checks every word. The triggers pick
their own provider (`-i-` OpenRouter, `-il-` Ollama, …); PROMPT_PROVIDER only sets a bare
`promptmend improve` and the Try tab, so setup never asks for it (#390): it asks which
triggers you will use (cloud, local or both; local is PROMPT_LOCAL_ONLY=true), and the keys
asked for and the test run follow the triggers. Every trigger name shown is read from the
match files (assets.triggers()), never a list kept here."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import assets, deploy, factory, settings_checks
from .config import ConfigLayers, Settings, secret_names
from .prompt_builder import ADDED, PROFILES, UserProfile
from .providers.base import is_loopback
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
    "Move with Next and Back (Escape); q, or Escape here, leaves setup. Nothing calls a "
    "paid provider."
)

USE_INTRO = (
    "Which triggers will you use? Your answer sets which key setup asks for and what the "
    "test checks."
)

# `setup --provider` (deprecated, #390): no step of setup sets PROMPT_PROVIDER any more.
_DEPRECATED = "--provider is deprecated and will be removed in a later release"
_NOT_READ = "no trigger reads it (only a bare `promptmend improve` and the Try tab)"
PROVIDER_DEPRECATED = (
    f"{_DEPRECATED}: PROMPT_PROVIDER is saved, but {_NOT_READ}. --use cloud|local|both "
    "chooses your triggers."
)


def provider_not_saved(provider: str) -> str:
    """The wizard's Welcome note for `setup --provider` on a terminal: it has no step for it
    and nothing is written without a press, so the value is not saved."""
    return (
        f"{_DEPRECATED}: this setup has no step for it, so PROMPT_PROVIDER was not saved; "
        f"{_NOT_READ}. `promptmend config set PROMPT_PROVIDER {provider}` saves it."
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
    "clipboard": (
        "Clipboard",
        "you paste it yourself; safe to switch windows (a clipboard history sees it)",
    ),
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

# The test step and the next step when the answer is local but no trigger stays local.
NO_LOCAL_TRIGGER = "no trigger stays on this computer with your settings"

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


# What a fact of setup came to, worked out once (done_rows()) for both the Done checklist
# and the rail (rail()), so the two never disagree (#393): ok, skipped or not run (a step
# `setup --no-deploy`/`--no-smoke-test` leaves out, a test not run), or failed (what leaves a
# trigger not working: an answer PROMPT_LOCAL_ONLY contradicts, a needed key not set, the
# match files not found or not installed, a test that failed). A fact not known yet (the
# match folder still being read, or no State at all) is UNKNOWN: neither ✓ nor ✗.
OK, SKIPPED, FAILED, UNKNOWN = "ok", "skipped", "failed", "unknown"
MARKS = {OK: "✓", SKIPPED: "\u2013", FAILED: "✗", UNKNOWN: "\u2026"}
CURRENT = "▶"


@dataclass(frozen=True)
class DoneRow:
    """One line of the Done checklist. ``step`` is the step it belongs to: the Triggers row
    to "use", each needed key's to "keys", Profile and Output to theirs, Match files to
    "espanso" and Test to "test"; Welcome and Done have none."""

    status: str
    label: str
    text: str
    step: str

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def mark(self) -> str:
        return MARKS[self.status]


@dataclass(frozen=True)
class SmokeRun:
    """One smoke run of the test step: the provider a trigger runs on, and that trigger
    (None when no active trigger takes the path). ``provider`` None: the answer is local but
    no trigger stays on this computer, so there is nothing to run (never a pass)."""

    trigger: str | None
    provider: str | None


def as_answered(settings: Settings, use: str) -> Settings:
    """``settings`` with PROMPT_LOCAL_ONLY as the answer ``use`` sets it: what the deployed
    triggers read from config.toml (Espanso does not start from your shell, so a variable
    exported there never reaches them). The keys asked for follow it."""
    return replace(settings, local_only=use == LOCAL)


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


def _and(words: Sequence[str]) -> str:
    return " and ".join(words) if len(words) < 3 else f"{', '.join(words[:-1])} and {words[-1]}"


def _split(
    triggers: Sequence[assets.Trigger], settings: Settings
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The active rewrite triggers that leave this computer under the settings (what
    PROMPT_LOCAL_ONLY=true refuses, factory.leaves_machine()) and those that stay."""
    paths = _paths(triggers, settings)
    away = [(t, p) for t, p in paths if factory.leaves_machine(p, settings)]
    return away, [(t, p) for t, p in paths if (t, p) not in away]


def _labels(paths: Sequence[tuple[str, str]]) -> list[str]:
    return list(dict.fromkeys(_label(p) for _, p in paths))


def _why_not_local(settings: Settings) -> str:
    """Why each local provider leaves this computer: its address or an Ollama cloud model."""
    reasons = []
    for spec in PROVIDERS.values():
        if spec.cloud or not factory.leaves_machine(spec.name, settings):
            continue
        if is_loopback(spec.url(settings)):
            reasons.append(f"{spec.model_setting} is a cloud model")
        else:
            reasons.append(f"{spec.url_setting} points to another computer")
    return "; ".join(reasons)


def local_note(
    triggers: Sequence[assets.Trigger],
    settings: Settings,
    *,
    legacy: str | None = None,
    already: bool = False,
    headless: bool = False,
) -> str:
    """What "local" does: the triggers PROMPT_LOCAL_ONLY=true refuses (each whose provider
    can send the draft off this machine, factory.leaves_machine()) and the ones left to use.
    Worded by what is true: what the step will save (the default), what is set (``already``:
    config.toml, or in legacy mode the .env, holds it), or what the person must add to the
    .env ``legacy`` names, which setup cannot write (nor remove from: turning it off there is
    the person's edit too). ``headless`` names the options instead of the wizard's answers.
    Which providers it refuses, and a local relay, are docs/privacy.md's."""
    away, kept = _split(triggers, settings)
    refused = ""
    if away:
        names = _and([t for t, _ in away])
        refused = (
            f"{names} {{}}paste a refusal instead of sending your draft to {_and(_labels(away))}."
        )
    if legacy and not already:
        text = (
            f"Local needs PROMPT_LOCAL_ONLY=true, which this setup cannot save: add it to {legacy}."
        )
        if refused:
            text += f" Then {refused.format('')}"
    elif already:
        text = (
            f"PROMPT_LOCAL_ONLY=true is set: {refused.format('')}"
            if refused
            else ("PROMPT_LOCAL_ONLY=true is set.")
        )
    else:
        text = (
            f"Local saves PROMPT_LOCAL_ONLY=true: {refused.format('then ')}"
            if refused
            else ("Local saves PROMPT_LOCAL_ONLY=true.")
        )
    if kept:
        use = f"Use {' or '.join(f'{t} ({_label(p)})' for t, p in kept)}."
    else:
        why = _why_not_local(settings)
        use = f"No trigger stays on this computer{f' ({why})' if why else ''}."
    if legacy and not already:
        return f"{text} {use}"
    if legacy:
        return f"{text} {use} Remove it from {legacy} to turn it off."
    off = "`--use cloud` or `--use both`" if headless else "Cloud or Both"
    return f"{text} {use} {off} turns it off again."


def use_choices(triggers: Sequence[assets.Trigger], settings: Settings) -> list[Choice]:
    """The Cloud or local step's answers, each named by the providers its triggers run on
    (as local_note() splits them) and listing those triggers. Only local is stored
    (PROMPT_LOCAL_ONLY=true), so it alone can be marked current."""
    away, kept = _split(triggers, settings)
    cloud = (
        f"{' '.join(t for t, _ in away)} · the draft leaves; needs a key"
        if away
        else "no trigger leaves this computer"
    )
    local = (
        f"{' '.join(t for t, _ in kept)} · PROMPT_LOCAL_ONLY=true: the rest refuse"
        if kept
        else "no trigger stays on this computer; every one would refuse"
    )
    return [
        Choice(CLOUD, f"Cloud ({', '.join(_labels(away)) or 'none'})", cloud, False),
        Choice(LOCAL, f"Local ({', '.join(_labels(kept)) or 'none'})", local, settings.local_only),
        Choice(BOTH, "Both", "every trigger above; needs the cloud key", False),
    ]


def smoke_runs(use: str, triggers: Sequence[assets.Trigger], settings: Settings) -> list[SmokeRun]:
    """The smoke runs of the test step for the answer ``use``: the path of the first trigger
    on a cloud API (-i-, OpenRouter) for cloud, of the first one that stays on this computer
    under the settings (factory.leaves_machine(), as PROMPT_LOCAL_ONLY judges: -il-, Ollama,
    unless Ollama is elsewhere or a cloud model) for local, both for both. Never
    PROMPT_PROVIDER, which no trigger reads. With no local trigger the local run has no
    provider: nothing runs, and it never counts as a pass."""
    paths = _paths(triggers, settings)
    runs = []
    if use in (CLOUD, BOTH):
        found = next(((t, p) for t, p in paths if PROVIDERS[p].cloud), None)
        runs.append(SmokeRun(*found) if found else SmokeRun(None, "openrouter"))
    if use in (LOCAL, BOTH):
        kept = ((t, p) for t, p in paths if not factory.leaves_machine(p, settings))
        found = next(kept, None)
        runs.append(SmokeRun(*found) if found else SmokeRun(None, None))
    return runs


def _path(run: SmokeRun) -> str:
    taken = f"the path {run.trigger} takes" if run.trigger else "no active trigger takes it"
    return f"`promptmend improve --provider {run.provider}` ({taken})"


NOTHING_LOCAL = f"Nothing runs on the local path: {NO_LOCAL_TRIGGER}."


def smoke_intro(runs: Sequence[SmokeRun]) -> str:
    """The test step's intro: which trigger's path each run takes, and that nothing runs on
    the local path when no trigger stays on this computer."""
    real = [run for run in runs if run.provider is not None]
    none = " " + NOTHING_LOCAL if len(real) < len(runs) else ""
    if not real:
        return NOTHING_LOCAL
    return (
        f"Runs {' and '.join(map(_path, real))} against a stub server on 127.0.0.1 that "
        "answers like the provider. No provider is called and your real key is never sent; "
        f"it checks that the CLI starts and reads its settings.{none}"
    )


def _local_urls() -> str:
    return ", ".join(spec.url_setting for spec in PROVIDERS.values() if not spec.cloud)


def smoke_result(run: SmokeRun, ok: bool, message: str) -> str:
    """One run's result line, naming the trigger whose path it took."""
    if run.provider is None:
        return (
            f"local: not run: {NO_LOCAL_TRIGGER}: point a local server's base URL "
            f"({_local_urls()}) back to this computer, with a local model, or answer cloud "
            "(`--use cloud`)"
        )
    who = f"{run.trigger} ({run.provider})" if run.trigger else run.provider
    return f"{who}: {'passed' if ok else 'failed'}: {message}"


def next_step(use: str, triggers: Sequence[assets.Trigger], settings: Settings) -> str:
    """How to try it: the first trigger the answer leaves usable (the local test run's for
    local, the cloud one's, -i-, for cloud and both), and what happens by PROMPT_OUTPUT."""
    run = smoke_runs(LOCAL if use == LOCAL else CLOUD, triggers, settings)[0]
    if run.provider is None:
        return f"Nothing to try yet: {NO_LOCAL_TRIGGER}. See the Cloud or local step."
    trigger = run.trigger or "-i-"
    if settings.output == "clipboard":
        return (
            f"Try it: copy a rough draft, click into any text field, type {trigger} and wait "
            "without typing. The rewrite then waits on the clipboard: paste it where you "
            "want it."
        )
    return (
        f"Try it: copy a rough draft, click into any text field, type {trigger} and wait "
        "without typing or switching windows. The rewrite replaces the trigger."
    )


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


def use_problem(
    settings: Settings, use: str, *, legacy: str | None = None, from_env: bool = False
) -> str | None:
    """What is wrong when the effective PROMPT_LOCAL_ONLY disagrees with the answer ``use``
    (both setup flows: headless as a to-do line, the wizard on Done), or None. ``from_env``:
    the value comes from an environment variable, which outranks config.toml wherever it is
    set. "Wherever", not "in any shell": Espanso started by launchd on macOS reads
    config.toml, but a Windows user variable (setx) or a Linux session variable reaches
    Espanso and the triggers too. ``legacy`` is the .env PROMPTMEND_ENV names, which setup
    cannot write."""
    local = use == LOCAL
    if local == settings.local_only:
        return None
    if from_env:
        effect = (
            "the cloud triggers still send drafts off this computer wherever it is set"
            if local
            else "the cloud triggers refuse wherever it is set"
        )
        return (
            f"{use}, but the environment variable PROMPT_LOCAL_ONLY overrides the saved value, "
            f"so {effect}: remove it from the environment"
        )
    if local:
        fix = (
            f"add PROMPT_LOCAL_ONLY=true to {legacy}"
            if legacy
            else "`promptmend config set PROMPT_LOCAL_ONLY true`"
        )
        return (
            "local, but PROMPT_LOCAL_ONLY is off, so the cloud triggers still send drafts off "
            f"this computer: {fix}"
        )
    fix = (
        f"remove PROMPT_LOCAL_ONLY from {legacy}"
        if legacy
        else "`promptmend config unset PROMPT_LOCAL_ONLY`"
    )
    return f"{use}, but PROMPT_LOCAL_ONLY=true makes the cloud triggers refuse: {fix}"


def no_key_line(settings: Settings, *, pending: bool = False) -> str:
    """`setup --non-interactive`'s line when no key is needed; ``pending``: the answer is
    local but PROMPT_LOCAL_ONLY=true is not set yet (legacy mode: the person adds it)."""
    if settings.local_only and pending:
        return (
            "No key needed once PROMPT_LOCAL_ONLY=true is set: it refuses every trigger that "
            "would need one."
        )
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


def shown_choice(name: str, value: str, overrides: Mapping[str, str]) -> str:
    """A choice as setup shows it (saved_settings()), and the effective value an environment
    variable gives instead (setup_flow.env_overrides()), said once where the value is shown."""
    if name not in overrides:
        return value
    return (
        f"{value} (the environment variable {name} overrides it wherever it is set: "
        f"{overrides[name]})"
    )


def _use_row(settings: Settings, use: str, legacy: str | None, from_env: bool) -> DoneRow:
    """The answer, checked against PROMPT_LOCAL_ONLY (use_problem(): it may not have been
    saved, in legacy mode, or an environment variable may outrank config.toml)."""
    problem = use_problem(settings, use, legacy=legacy, from_env=from_env)
    if problem:
        return DoneRow(FAILED, "Triggers", problem, "use")
    if use == LOCAL:
        return DoneRow(
            OK, "Triggers", "local (PROMPT_LOCAL_ONLY=true: the cloud triggers refuse)", "use"
        )
    return DoneRow(OK, "Triggers", use, "use")


def done_rows(
    settings: Settings,
    use: str,
    keys: Sequence[KeyRow],
    plan: deploy.Plan | None,
    test: bool | None,
    legacy: str | None = None,
    skipped: Sequence[str] = (),
    *,
    from_env: bool = False,
    saved: Settings | None = None,
    overrides: Mapping[str, str] | None = None,
    reading: bool = False,
) -> list[DoneRow]:
    """What is set and what is left; ``use`` is the Cloud or local step's answer and
    ``legacy`` the .env PROMPTMEND_ENV names, where a missing setting or key then goes. A step
    in ``skipped`` ("espanso", "test": left out by `setup --no-deploy`/`--no-smoke-test`) says
    so, SKIPPED, not a problem; so does a test not run. ``from_env``: PROMPT_LOCAL_ONLY comes
    from an environment variable (use_problem()). The answer is checked against the effective
    ``settings``; the profile and output shown are ``saved`` (setup_flow.saved_settings()),
    with ``overrides`` naming an environment variable that changes them. Each row names its
    step (DoneRow), so the rail's marks come from these rows (rail()). ``reading``: the match
    folder is still being read (no ``plan`` yet, and no error), so Match files is UNKNOWN."""
    shown = saved or settings
    over = overrides or {}
    rows = [
        _use_row(settings, use, legacy, from_env),
        DoneRow(OK, "Profile", shown_choice("PROMPT_PROFILE", shown.profile, over), "profile"),
        DoneRow(OK, "Output", shown_choice("PROMPT_OUTPUT", shown.output, over), "output"),
    ]
    for key in keys:
        if not key.needed:
            continue
        if key.set:
            rows.append(DoneRow(OK, key.name, "set", "keys"))
        else:
            fix = (
                f"add {key.name}= to {legacy}" if legacy else f"`promptmend secrets set {key.name}`"
            )
            rows.append(DoneRow(FAILED, key.name, f"not set: {fix}", "keys"))
    rows.append(_match_row(plan, skipped, reading))
    if "test" in skipped:
        rows.append(DoneRow(SKIPPED, "Test", "skipped (--no-smoke-test)", "test"))
    elif test is None:
        rows.append(DoneRow(SKIPPED, "Test", "not run", "test"))
    elif test:
        rows.append(DoneRow(OK, "Test", "passed", "test"))
    else:
        rows.append(DoneRow(FAILED, "Test", "failed (see the Test step)", "test"))
    return rows


def _match_row(plan: deploy.Plan | None, skipped: Sequence[str], reading: bool) -> DoneRow:
    """The Match files row: skipped by --no-deploy, unknown before the first read of the
    match folder lands (a later re-read keeps the last result on screen; with no State at
    all the rail shows every passed step unknown, rail()), failed while Espanso's folder
    cannot be read or a file is left to write (the triggers do not run until it is), else
    ok."""
    label = "Match files"
    if "espanso" in skipped:
        return DoneRow(SKIPPED, label, "skipped (--no-deploy)", "espanso")
    if plan is None and reading:
        return DoneRow(UNKNOWN, label, "the match folder is being read", "espanso")
    if plan is None:
        return DoneRow(FAILED, label, "Espanso's folder was not found", "espanso")
    if _writes(plan):
        return DoneRow(FAILED, label, "not installed: `promptmend espanso deploy`", "espanso")
    text = f"installed in {plan.espanso_dir / 'match'}"
    if plan.conflicts:
        text += f"; {len(plan.conflicts)} file(s) you changed were kept"
    return DoneRow(OK, label, text, "espanso")


def step_status(step: str, rows: Sequence[DoneRow]) -> str:
    """A step's status from its rows (DoneRow.step): FAILED when any failed, else UNKNOWN
    when any is not known yet, else SKIPPED when any was skipped or not run, else OK.
    Welcome has no row, nor the keys step when no key is needed: there is nothing to fail,
    so passed it is OK."""
    statuses = {row.status for row in rows if row.step == step}
    for status in (FAILED, UNKNOWN, SKIPPED):
        if status in statuses:
            return status
    return OK


def rail(at: str, order: Sequence[str], rows: Sequence[DoneRow] | None) -> list[tuple[str, str]]:
    """The left rail: every step in STEPS with its mark. ``at`` is the current step (▶),
    ``order`` the steps the wizard visits (setup --no-deploy/--no-smoke-test leave one out:
    it is listed still, marked SKIPPED, as Done lists it); a step before ``at`` is marked by
    step_status(), so on Done the rail and the checklist say the same; a step not reached yet
    has no mark (""). ``rows`` None: nothing is known yet (no State), so a passed step other
    than Welcome is UNKNOWN, never ✓."""
    passed = order[: order.index(at)] if at in order else []
    marks = []
    for step in STEPS:
        if step == at:
            mark = CURRENT
        elif step not in order:
            mark = MARKS[SKIPPED]
        elif step in passed:
            known = rows is not None or step == "welcome"
            mark = MARKS[step_status(step, rows or []) if known else UNKNOWN]
        else:
            mark = ""
        marks.append((step, mark))
    return marks
