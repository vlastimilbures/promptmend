"""What setup shows (the wizard in tui/setup_wizard.py and the text flow in commands/setup.py),
worked out with no Textual and no writes, so headless `setup` never loads the interface and
`tests/test_setup_guide.py` checks every word. The triggers pick
their own provider (`-i-` OpenRouter, `-il-` Ollama, …); PROMPT_PROVIDER only sets a bare
`promptmend improve` and the Try tab, so the keys asked for are the ones the triggers need."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import assets, deploy, doctor, factory
from .config import ConfigLayers, Settings, secret_names
from .factory import PROVIDER_LABELS, PROVIDER_NAMES
from .prompt_builder import ADDED, PROFILES, UserProfile

# Step id -> the name in the left rail, in order. "earlier" is shown only when there is a
# .env or an earlier checkout install to take settings from.
STEPS = {
    "welcome": "Welcome",
    "earlier": "Earlier settings",
    "provider": "Provider",
    "profile": "Profile",
    "keys": "API keys",
    "espanso": "Espanso",
    "test": "Test",
    "done": "Done",
}

WELCOME = (
    "PromptMend rewrites a rough draft into a precise prompt: copy the draft, type a trigger "
    "such as -i- in any text field, and Espanso pastes the rewrite.\n\n"
    "This setup takes a few steps. Each one shows what it changes before it does, and you "
    "can skip any of them and come back later (`promptmend setup`, or Home > Setup… in "
    "`promptmend`).\n\n"
    "Move with Next and Back (Escape). Nothing calls a paid provider."
)

# What each provider is, for the Provider step.
PROVIDER_HELP = {
    "openrouter": "cloud, many models through one key",
    "anthropic": "cloud, Claude models, needs a key",
    "ollama": "local model on this machine, no key",
    "lmstudio": "local model on this machine, no key",
}
# The order the Provider step lists them: the cloud default first.
PROVIDER_ORDER = ("openrouter", "anthropic", "ollama", "lmstudio")
PROVIDER_INTRO = (
    "Each trigger picks its own provider: the ones shown next to each choice below. This "
    "choice is the default for a bare `promptmend improve` and the Try tab."
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

KEYS_INTRO = (
    "Keys are saved in the secret store (secrets.toml, readable only by you), never in "
    "config.toml, and are never shown again. Leave a field empty to keep what is there."
)


def legacy_keys_intro(env_file: str) -> str:
    """KEYS_INTRO while PROMPTMEND_ENV names ``env_file``: that file alone is read, so a key
    goes there and the step has nothing to save."""
    return (
        f"PROMPTMEND_ENV is set, so keys are read only from {env_file}: add each one there "
        "as NAME=value (this setup cannot save them)."
    )


ESPANSO_INTRO = (
    "Espanso runs PromptMend when you type a trigger. Its match files tell it which trigger "
    "runs what; installing them writes only PromptMend's own files into Espanso's match "
    "folder. A file you edited is kept as it is."
)

TEST_INTRO = (
    "Runs `promptmend improve` against a stub server on 127.0.0.1 that answers like the "
    "provider. No provider is called and your real key is never sent; it checks that the "
    "CLI starts and reads its settings."
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


def steps(earlier: bool) -> list[str]:
    return [s for s in STEPS if earlier or s != "earlier"]


def rewrites(triggers: Sequence[assets.Trigger], settings: Settings) -> dict[str, list[str]]:
    """Each provider the active rewrite triggers run on, with those triggers (doctor's)."""
    return doctor._rewrites(list(triggers), settings)


def provider_choices(settings: Settings, triggers: Sequence[assets.Trigger]) -> list[Choice]:
    used = rewrites(triggers, settings)
    refused = {r.name for r in factory.routes(settings) if r.refused}
    found = []
    order = [*PROVIDER_ORDER, *(n for n in PROVIDER_NAMES if n not in PROVIDER_ORDER)]
    for name in order:
        detail = PROVIDER_HELP.get(name, "")
        names = [t for t in used.get(name, []) if t]
        if names:
            detail += f" · {' '.join(names)}"
        if name in refused:
            detail += "; refused while PROMPT_LOCAL_ONLY=true"
        found.append(Choice(name, PROVIDER_LABELS[name], detail, name == settings.provider))
    return found


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
    used = rewrites(triggers, settings)
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
    if row.triggers:
        use = f"needed by {', '.join(row.triggers)}"
    elif row.needed:
        use = "needed by a bare `promptmend improve` (your provider)"
    else:
        use = "not needed by any active trigger"
    return f"{row.name}: {status}; {use}"


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


def done_rows(
    settings: Settings,
    keys: Sequence[KeyRow],
    plan: deploy.Plan | None,
    test: bool | None,
    legacy: str | None = None,
    skipped: Sequence[str] = (),
) -> list[DoneRow]:
    """What is set and what is left; ``legacy`` is the .env PROMPTMEND_ENV names, where a
    missing key then goes. A step in ``skipped`` ("espanso", "test": left out by `setup
    --no-deploy`/`--no-smoke-test`) says so and is not a problem."""
    rows = [DoneRow(True, "Provider", f"{settings.provider}; profile {settings.profile}")]
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
