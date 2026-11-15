"""What setup does (#282): the decisions and the writes shared by the two setup flows, the
wizard (tui/setup_wizard.py) and `setup --non-interactive`/`--plain` (commands/setup.py). Both
are presentation over this module: they print or show the outcomes, in their own words, and
neither decides what to save, where, or when. setup_guide.py holds what they say; this module
imports neither Typer nor Textual itself and never loads Textual (tests/test_setup_flow.py
checks both); Typer still arrives through commands.common and commands.settings, where the
setting services and no_key() live. It writes only
through the services the headless commands use (`config set`/`unset`, `secrets set`,
`espanso deploy`), so each write goes through config_files.write_atomic() and a key only ever
reaches the secret store.

The two flows still differ where they always have (an interactive step-by-step wizard against a
one-shot printout); this module keeps each one's behaviour rather than unify it, and its
docstrings say which flow uses what."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import assets, config, config_files, config_store, deploy, setup_guide
from .commands import common
from .commands import settings as settings_cmd
from .config import setting_fields
from .factory import PROVIDER_KEYS, PROVIDER_NAMES
from .prompt_builder import system_prompt
from .redaction import safe_repr

DEFAULTS = {f.metadata["env"]: f.metadata["default"] for f in setting_fields()}


# --- Where settings go ---------------------------------------------------------------------


@dataclass(frozen=True)
class Storage:
    """Where setup may save: ``legacy`` is the .env PROMPTMEND_ENV names (then neither
    config.toml nor the secret store is read, so nothing is saved), and ``unread`` setup's
    note on a .env no release reads any more (#275), looked for only outside legacy mode."""

    legacy: str | None
    unread: str | None

    @property
    def writable(self) -> bool:
        return self.legacy is None


def storage() -> Storage:
    legacy = common.legacy_env()
    if legacy:
        return Storage(legacy, None)
    return Storage(None, setup_guide.unread_env_note(config.unread_env_files()))


# --- Provider and profile ------------------------------------------------------------------

# The setting each choice is, and how to read its Settings field.
_CHOICES: dict[str, Callable[[config.Settings], str]] = {
    "PROMPT_PROVIDER": lambda settings: settings.provider,
    "PROMPT_PROFILE": lambda settings: settings.profile,
}


def current_choice(name: str) -> str:
    """The effective value of PROMPT_PROVIDER or PROMPT_PROFILE."""
    return _CHOICES[name](common.load_layers()[1])


def choice_command(name: str, value: str) -> list[str]:
    """The `config` command save_choice() runs for ``value``: a default is unset."""
    return ["unset", name] if value == DEFAULTS[name] else ["set", name, value]


def save_choice(name: str, value: str) -> tuple[str | None, list[str]]:
    """Save PROMPT_PROVIDER or PROMPT_PROFILE, for both flows: an unchanged value writes
    nothing (None, no command), and a default is not pinned but removed from config.toml, so a
    later change of the default still applies. Else what was done, with the `config` command
    that did it (choice_command(), or `set` when removing the default did not make it apply),
    for the wizard's log."""
    if current_choice(name) == value:
        return None, []
    if value == DEFAULTS[name]:
        removed, path = settings_cmd.unset_setting(name)
        if removed and current_choice(name) == value:
            return f"{name} removed from {path}; its default, {value}, applies", ["unset", name]
    saved = settings_cmd.save_setting(name, value)
    notes = settings_cmd.after_save(name)
    return "; ".join([f"{name}={value} saved in {saved.path}", *notes]), ["set", name, value]


def chosen_provider(option: str | None, settings: config.Settings) -> str:
    """`setup --non-interactive`'s provider: ``option`` (--provider), else the current one; a
    current value that is no provider raises ValueError (the command's usage error)."""
    chosen = option if option is not None else settings.provider
    if chosen not in PROVIDER_NAMES:
        raise ValueError(f"PROMPT_PROVIDER {safe_repr(chosen)} is not a provider; pass --provider")
    return chosen


def check_profile(name: str, overrides: tuple[str, ...]) -> None:
    """Raise ValueError when ``name`` is no profile (`setup --profile`, either flow)."""
    system_prompt(name, "", overrides)


@dataclass(frozen=True)
class ChoicesSaved:
    """save_choices()'s outcome: a line per choice it changed; else ``created``, config.toml
    written with no setting in it, or neither (nothing changed). ``error`` ends the step."""

    messages: tuple[str, ...] = ()
    created: Path | None = None
    error: str | None = None


def save_choices(
    provider: str, profile: str, said: Callable[[str], None] = lambda _: None
) -> ChoicesSaved:
    """`setup --non-interactive`'s Settings step: both choices, then, when neither changed and
    there is no config.toml yet, an empty one, so a first run ends in saved mode even with
    every default (config.toml is where settings go). ``said`` gets each change's line as it
    is made, before the next write. The wizard saves each choice on its own step
    (save_choice()) and writes no empty config.toml."""
    messages: list[str] = []
    for name, value in (("PROMPT_PROVIDER", provider), ("PROMPT_PROFILE", profile)):
        try:
            message, _ = save_choice(name, value)
        except (ValueError, common.CommandError, config_store.ConfigStoreError) as exc:
            return ChoicesSaved(tuple(messages), error=str(exc))
        if message:
            said(message)
            messages.append(message)
    if messages:
        return ChoicesSaved(tuple(messages))
    snapshot = config_store.read_settings()
    if config_files.is_file(snapshot.path):
        return ChoicesSaved()
    try:
        saved = config_store.save_settings(snapshot, {})
    except (ValueError, config_store.ConfigStoreError) as exc:
        return ChoicesSaved(error=str(exc))
    return ChoicesSaved(created=saved.path)


# --- API keys ------------------------------------------------------------------------------

# What saving a key can raise; the step reports it and setup goes on.
KEY_ERRORS = (common.CommandError, ValueError, config_files.SecretStoreError)


def stdin_key(provider: str) -> str | None:
    """The key `setup --api-key-stdin` reads for ``provider``, or None when it takes none."""
    return PROVIDER_KEYS.get(provider)


def needed_keys(layers: config.ConfigLayers, settings: config.Settings) -> list[setup_guide.KeyRow]:
    """The keys `setup --non-interactive` reports: those an active trigger or the provider
    needs (an Ollama default still leaves -i- on OpenRouter). The wizard lists every key."""
    rows = setup_guide.key_rows(layers, settings, assets.triggers())
    return [row for row in rows if row.needed]


def save_key(name: str, value: str) -> None:
    """Save one key in the secret store (`secrets set`'s service; never config.toml), raising
    one of KEY_ERRORS. ``value`` comes from stdin, getpass or the wizard's masked field."""
    settings_cmd.store_key(name, value)


def save_keys(typed: Sequence[tuple[str, str]]) -> str:
    """The wizard's Save keys: each typed key in turn, with `secrets set`'s warnings; what was
    saved, naming the keys, never their values. The first failure raises (KEY_ERRORS)."""
    saved = []
    for name, value in typed:
        save_key(name, value)
        saved.append("; ".join([f"{name} saved", *settings_cmd.after_save(name)]))
    return "; ".join(saved)


# --- Espanso's match files -----------------------------------------------------------------


def read_plan(espanso_dir: str | None, launcher: str | None) -> tuple[deploy.Plan, str | None]:
    """The wizard's deploy plan (as `setup --espanso-dir --launcher` makes it), and why the
    default Espanso folder was used when `espanso path config` gave none. It prints nothing;
    `setup --non-interactive` reads its plan through `espanso deploy`'s make_plan(), which
    prints the folder and the launcher."""
    fallback = None
    if espanso_dir is not None:
        common.no_key(espanso_dir, "--espanso-dir")
        target = Path(espanso_dir).expanduser().resolve()
    else:
        found = deploy.locate_espanso_dir()
        target, fallback = found.path.resolve(), found.fallback
    if launcher is not None:
        common.no_key(launcher, "--launcher")
        path = launcher
    else:
        path = str(deploy.resolve_launcher().path)
    return deploy.plan(target, deploy.launcher_text(path), deploy.Manifest.load()), fallback


def in_sync(plan: deploy.Plan) -> bool:
    """Whether there is nothing to install: every file is up to date, or only gone manifest
    entries are left (forget() drops those without writing a file)."""
    return plan.is_noop or plan.only_forgets


def forget(plan: deploy.Plan) -> deploy.Outcome:
    """Drop the manifest's gone entries (`setup --non-interactive`, with no question: no match
    file is written). Only for a plan that does nothing else (only_forgets)."""
    if not plan.only_forgets:
        raise ValueError("forget() takes a plan that only drops gone manifest entries")
    return deploy.apply(plan, {})


def install(plan: deploy.Plan) -> deploy.Outcome:
    """Write the match files: a file the person edited is kept, never overwritten (no choice
    is passed; `espanso deploy` is where one is made)."""
    return deploy.apply(plan, {})
