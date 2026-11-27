"""What setup does: the decisions and the writes shared by the two setup flows, the
wizard (tui/setup_wizard.py) and `setup --non-interactive`/`--plain` (commands/setup.py). Both
are presentation over this module: they print or show the outcomes, in their own words, and
neither decides what to save, where, or when. setup_guide.py holds what they say; this module
neither imports nor loads Typer, Click or Textual, and nothing from commands/
(tests/test_setup_flow.py checks it): a refusal is a settings_service.ServiceError, which the
command turns into its exit code and the wizard shows as a message. It writes only
through the services the headless commands use (settings_service.py for `config set`/`unset`
and `secrets set`; deploy.py for `espanso deploy`), so each write goes through
config_files.write_atomic() and a key only ever reaches the secrets file.

The two flows still differ where they always have (an interactive step-by-step wizard against a
one-shot printout); this module keeps each one's behaviour rather than unify it, and its
docstrings say which flow uses what."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import assets, config, config_files, config_store, deploy, settings_service, setup_guide
from .config import setting_fields
from .factory import PROVIDER_KEYS
from .prompt_builder import system_prompt

DEFAULTS = {f.metadata["env"]: f.metadata["default"] for f in setting_fields()}


# --- Where settings go ---------------------------------------------------------------------


@dataclass(frozen=True)
class Storage:
    """Where setup may save: ``legacy`` is the .env PROMPTMEND_ENV names (then neither
    config.toml nor the secrets file is read, so nothing is saved), and ``unread`` setup's
    note on a .env this release does not read (config.unread_env_files()), looked for only
    outside legacy mode."""

    legacy: str | None
    unread: str | None

    @property
    def writable(self) -> bool:
        return self.legacy is None


def storage() -> Storage:
    legacy = settings_service.legacy_env()
    if legacy:
        return Storage(legacy, None)
    return Storage(None, setup_guide.unread_env_note(config.unread_env_files()))


# --- The choices: cloud or local, profile, output (and the deprecated --provider) ---------

LOCAL_ONLY = "PROMPT_LOCAL_ONLY"

# The setting each choice is, and how to read its Settings field as config.toml's text.
_CHOICES: dict[str, Callable[[config.Settings], str]] = {
    LOCAL_ONLY: lambda settings: str(settings.local_only).lower(),
    "PROMPT_PROVIDER": lambda settings: settings.provider,
    "PROMPT_PROFILE": lambda settings: settings.profile,
    "PROMPT_OUTPUT": lambda settings: settings.output,
}


def choice_value(name: str, settings: config.Settings) -> str:
    """The value of PROMPT_LOCAL_ONLY, PROMPT_PROVIDER, PROMPT_PROFILE or PROMPT_OUTPUT in
    ``settings``, as config.toml holds it."""
    return _CHOICES[name](settings)


def current_choice(name: str) -> str:
    """The effective value of one of the choices (choice_value() of the settings now)."""
    return choice_value(name, settings_service.load_layers()[1])


def local_only_value(use: str) -> str:
    """PROMPT_LOCAL_ONLY for the answer ``use`` (setup_guide.USES): true for local, else its
    default, false, which save_choice() unsets rather than pins."""
    return "true" if use == setup_guide.LOCAL else "false"


def current_use(settings: config.Settings) -> str:
    """The answer the settings give when none was picked (`setup --non-interactive` without
    --use, or the wizard before its step): local while PROMPT_LOCAL_ONLY is true, else cloud
    (cloud and both are not stored)."""
    return setup_guide.LOCAL if settings.local_only else setup_guide.CLOUD


def choice_command(name: str, value: str) -> list[str]:
    """The `config` command save_choice() runs for ``value``: a default is unset."""
    return ["unset", name] if value == DEFAULTS[name] else ["set", name, value]


def save_choice(name: str, value: str) -> tuple[str | None, list[str]]:
    """Save one of the choices (PROMPT_LOCAL_ONLY from the answer, local_only_value()), for
    both flows: an unchanged value writes
    nothing (None, no command), and a default is not pinned but removed from config.toml, so a
    later change of the default still applies. Else what was done, with the `config` command
    that did it (choice_command(), or `set` when removing the default did not make it apply),
    for the wizard's log."""
    if current_choice(name) == value:
        return None, []
    if value == DEFAULTS[name]:
        removed, path = settings_service.unset_setting(name)
        if removed and current_choice(name) == value:
            return f"{name} removed from {path}; its default, {value}, applies", ["unset", name]
    saved = settings_service.save_setting(name, value)
    notes = settings_service.after_save(name)
    return "; ".join([f"{name}={value} saved in {saved.path}", *notes]), ["set", name, value]


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
    choices: Sequence[tuple[str, str]], said: Callable[[str], None] = lambda _: None
) -> ChoicesSaved:
    """`setup --non-interactive`'s Settings step: each (setting, value) of ``choices`` in
    order, then, when none changed and there is no config.toml yet, an empty one, so a first
    run ends in saved mode even with every default (config.toml is where settings go).
    ``said`` gets each change's line as it is made, before the next write. The wizard saves
    each choice on its own step (save_choice()) and writes no empty config.toml."""
    messages: list[str] = []
    for name, value in choices:
        try:
            message, _ = save_choice(name, value)
        except (ValueError, settings_service.ServiceError, config_store.ConfigStoreError) as exc:
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
KEY_ERRORS = (settings_service.ServiceError, ValueError, config_files.SecretStoreError)


def stdin_key(provider: str) -> str | None:
    """The key `setup --api-key-stdin` reads for ``provider``, or None when it takes none."""
    return PROVIDER_KEYS.get(provider)


def key_provider(use: str, settings: config.Settings) -> str | None:
    """The provider whose key `setup --api-key-stdin` reads for the answer ``use``: the one
    the cloud trigger the test runs takes (OpenRouter, -i-); None for local, which needs none.
    The deprecated --provider still names its own instead."""
    if use == setup_guide.LOCAL:
        return None
    return setup_guide.smoke_runs(setup_guide.CLOUD, assets.triggers(), settings)[0].provider


def needed_keys(layers: config.ConfigLayers, settings: config.Settings) -> list[setup_guide.KeyRow]:
    """The keys `setup --non-interactive` reports: those an active trigger or the provider
    needs, unless PROMPT_LOCAL_ONLY refuses it. The wizard lists every key."""
    rows = setup_guide.key_rows(layers, settings, assets.triggers())
    return [row for row in rows if row.needed]


def save_key(name: str, value: str) -> None:
    """Save one key in the secrets file (`secrets set`'s service; never config.toml), raising
    one of KEY_ERRORS. ``value`` comes from stdin, getpass or the wizard's masked field."""
    settings_service.store_key(name, value)


def save_keys(typed: Sequence[tuple[str, str]]) -> str:
    """The wizard's Save keys: each typed key in turn, with `secrets set`'s warnings; what was
    saved, naming the keys, never their values. The first failure raises (KEY_ERRORS)."""
    saved = []
    for name, value in typed:
        save_key(name, value)
        saved.append("; ".join([f"{name} saved", *settings_service.after_save(name)]))
    return "; ".join(saved)


# --- Espanso's match files -----------------------------------------------------------------


def read_plan(espanso_dir: str | None, launcher: str | None) -> tuple[deploy.Plan, str | None]:
    """The wizard's deploy plan (as `setup --espanso-dir --launcher` makes it), and why the
    default Espanso folder was used when `espanso path config` gave none. It prints nothing;
    `setup --non-interactive` reads its plan through `espanso deploy`'s make_plan(), which
    prints the folder and the launcher."""
    fallback = None
    if espanso_dir is not None:
        settings_service.refuse_key(espanso_dir, "--espanso-dir")
        target = Path(espanso_dir).expanduser().resolve()
    else:
        found = deploy.locate_espanso_dir()
        target, fallback = found.path.resolve(), found.fallback
    if launcher is not None:
        settings_service.refuse_key(launcher, "--launcher")
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
