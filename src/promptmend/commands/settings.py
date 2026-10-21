"""`config` and `secrets`: the settings and API keys, over config.py (repair mode, #83) and
config_store.py (#84). A key is read only from stdin or a hidden prompt, never from an
argument, and no command prints one."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from .. import config, config_files, config_store
from ..config import ConfigLayers, env_names, secret_names
from ..redaction import safe_repr
from . import common
from .common import guard

config_app = typer.Typer(
    name="config",
    no_args_is_help=True,
    add_completion=False,
    help="Show, check and change the settings (config.toml).",
)
secrets_app = typer.Typer(
    name="secrets",
    no_args_is_help=True,
    add_completion=False,
    help="Save, check or remove API keys in the secret store. Keys are never arguments.",
)

_NAME = typer.Argument(..., help="A setting name, e.g. PROMPT_PROVIDER", show_default=False)
_SECRET = typer.Argument(..., help=", ".join(secret_names()), show_default=False)
_YES = typer.Option(False, "--yes", "-y", help="Do it without asking")


def _setting(name: str) -> str:
    if name not in env_names():
        raise typer.BadParameter(f"{safe_repr(name)} is not a setting; see `config show`")
    return name


def _secret(name: str) -> str:
    if name not in secret_names():
        raise typer.BadParameter(
            f"{safe_repr(name)} is not a secret; choose from {', '.join(secret_names())}"
        )
    return name


def _not_a_secret(name: str, verb: str) -> None:
    if name in secret_names():
        raise common.CommandError(
            f"{name} is a secret: {verb} it with `promptmend secrets {verb} {name}` (a hidden "
            "prompt or --stdin), never as an argument",
            common.USAGE,
        )


def env_override(name: str) -> str | None:
    """The warning to show after saving ``name`` while a real environment variable outranks
    it, or None."""
    if name in os.environ:
        return f"the environment variable {name} is set and overrides the saved value"
    return None


# The settings doctor.persona_problem() reads: saving one can make the persona go out.
_PERSONA_INPUTS = frozenset(
    {
        "PROMPT_PERSONA",
        "PROMPT_PROFILE",
        "PROMPT_PRO_PROFILE",
        "PROMPT_PROFILE_OVERRIDES",
        "PROMPT_EXTRA_PATTERNS",
        "PROMPT_LOCAL_ONLY",
        "PROMPT_GATE_LOCAL",
    }
)


def persona_warning(name: str) -> str | None:
    """The warning to show after saving ``name`` when the effective persona now matches the
    data-protection patterns and is sent (`config validate`'s problem: finding names only,
    never the text), or None. The value stays saved: a persona never blocks a call."""
    if name not in _PERSONA_INPUTS:
        return None
    from ..doctor import persona_problem

    return persona_problem(common.load_layers()[1])


def after_save(name: str) -> list[str]:
    """The warnings to show after saving or removing ``name``; the interface shows them too."""
    return [note for note in (env_override(name), persona_warning(name)) if note]


def _warn_after_save(name: str) -> None:
    for note in after_save(name):
        common.warn(note)


def save_setting(name: str, value: str) -> config_store.SavedSettings:
    """`config set` without the printing, shared with the interface (#93): refuse a secret, a
    value that looks like a key and legacy mode (CommandError), then save
    ``value`` in config.toml after checking it as the CLI would read it."""
    _not_a_secret(name, "set")
    if common.looks_like_a_key(value, user_patterns=name != common.EXTRA_PATTERNS):
        raise common.CommandError(
            "that value looks like a key or password, which never goes in config.toml; use "
            "`promptmend secrets set NAME` for an API key",
            common.USAGE,
        )
    common.refuse_in_legacy_mode(config_files.SETTINGS_FILE)
    return config_store.save_settings(config_store.read_settings(), {name: value})


def store_key(name: str, value: str) -> str:
    """`secrets set` without the printing, shared with setup and the interface: refuse legacy
    mode (CommandError), save ``value`` in the secret store and say so. after_save() gives the
    warnings to show with it."""
    common.refuse_in_legacy_mode("the secret store")
    config_store.save_secret(name, value)
    return f"{name} saved in the secret store ({config_store.config_dir()})"


# --- config -------------------------------------------------------------------------------


def layers_lines(layers: ConfigLayers, raw: bool) -> list[str]:
    lines = ["Read from, lowest precedence first:"]
    for layer in layers.layers:
        label = common.source_label(layer.source)
        if layer.source == config.DEFAULT_SOURCE:
            lines.append(f"  {label}")
            continue
        lines.append(f"  {label} ({len(layer.values)} setting(s))")
        if raw:
            for name in env_names():
                if name in layer.values:
                    entry = config.Entry(layer.values[name], layer.source)
                    lines.append(f"      {name} = {common.shown_value(name, entry)}")
    if common.legacy_env():
        lines.append(
            f"  ({config.ENV_FILE_VAR} is set: config.toml and the secret store are not read)"
        )
    return lines


@config_app.command("show", short_help="Each setting's effective value and where it comes from.")
@guard
def config_show(
    raw: bool = typer.Option(False, "--raw", help="Also list what each settings file sets"),
) -> None:
    """Each setting's effective value and where it comes from. Keys and the persona are shown
    only as set or not set; `overrides` names the files whose value lost to it."""
    layers, _ = common.load_layers()
    for line in layers_lines(layers, raw):
        typer.echo(line)
    typer.echo("")
    width = max(len(n) for n in env_names())
    for name in env_names():
        entry = layers.entries[name]
        notes = [common.source_label(entry.source)]
        if entry.shadows:
            notes.append("overrides " + ", ".join(map(common.source_label, entry.shadows)))
        if entry.rejected:
            bad = ", ".join(map(common.source_label, entry.rejected))
            notes.append(f"invalid value in {bad} ignored")
        typer.echo(f"{name:<{width}}  {common.shown_value(name, entry)}  [{'; '.join(notes)}]")
    common.show_findings(layers)


@config_app.command("get")
@guard
def config_get(name: str = _NAME) -> None:
    """Print one setting's effective value (not for keys: see `secrets status`)."""
    _setting(name)
    if name in secret_names():
        common.fail(f"{name} is a secret and is never printed; `secrets status` says if it is set")
    layers, _ = common.load_layers()
    common.show_findings(layers)
    value = layers.entries[name].value
    if common.looks_like_a_key(value, user_patterns=name != common.EXTRA_PATTERNS):
        common.fail(
            f"{name} holds what looks like a key ({len(value)} chars), so it is not printed; "
            "move the key to `promptmend secrets set`"
        )
    typer.echo(value)


@config_app.command("set")
@guard
def config_set(
    name: str = _NAME,
    value: str = typer.Argument(..., help="The new value", show_default=False),
) -> None:
    """Save a setting in config.toml, after checking it as the CLI would read it."""
    _setting(name)
    saved = save_setting(name, value)
    typer.echo(f"{name} saved in {saved.path}")
    _warn_after_save(name)


@config_app.command("unset")
@guard
def config_unset(name: str = _NAME) -> None:
    """Remove a setting from config.toml, so its default applies again."""
    _setting(name)
    removed, path = unset_setting(name)
    if not removed:
        typer.echo(f"{name} is not saved in {path}; nothing to do.")
        return
    typer.echo(f"{name} removed from {path}; its default applies.")
    _warn_after_save(name)


def unset_setting(name: str) -> tuple[bool, Path]:
    """`config unset` without the printing, shared with the interface's reset (#199): refuse
    a secret and legacy mode, then remove ``name`` from config.toml. Whether it was saved
    there, and the file's path."""
    _not_a_secret(name, "remove")
    common.refuse_in_legacy_mode(config_files.SETTINGS_FILE)
    snapshot = config_store.read_settings()
    if name not in snapshot.table:
        return False, snapshot.path
    config_store.save_settings(snapshot, {name: None})
    return True, snapshot.path


@config_app.command("validate")
@guard
def config_validate() -> None:
    """Check the settings as improve reads them. Exit 4 when there is anything to fix."""
    from ..doctor import persona_problem
    from ..prompt_builder import system_prompt

    problems = []
    try:
        ConfigLayers.resolve().settings()
    except ValueError as exc:
        problems.append(f"improve would stop with: {exc}")
    layers, settings = common.load_layers()
    problems += [f"{common.source_label(f.source)}: {f.message}" for f in layers.findings]
    for profile in dict.fromkeys(filter(None, (settings.profile, settings.pro_profile))):
        try:
            system_prompt(profile, "", settings.profile_overrides)
        except ValueError as exc:
            problems.append(str(exc))
    if flagged := persona_problem(settings):
        problems.append(flagged)
    for problem in dict.fromkeys(problems):
        typer.echo(f"problem: {problem}")
    if problems:
        raise typer.Exit(common.PROBLEMS)
    typer.echo("The settings are valid.")


# --- secrets ------------------------------------------------------------------------------


@secrets_app.command("set")
@guard
def secrets_set(
    name: str = _SECRET,
    stdin: bool = typer.Option(
        False, "--stdin", help="Read the key from the first line of stdin instead of asking"
    ),
) -> None:
    """Save an API key in the secret store (asks with hidden input, or reads --stdin)."""
    _secret(name)
    common.refuse_in_legacy_mode("the secret store")  # before asking for a key it cannot save
    value = common.read_secret(name, from_stdin=stdin)
    typer.echo(store_key(name, value))
    _warn_after_save(name)


@secrets_app.command("status")
@guard
def secrets_status() -> None:
    """Whether each key is set and where it comes from; never the value."""
    layers, _ = common.load_layers()
    width = max(len(n) for n in secret_names())
    for name in secret_names():
        entry = layers.entries[name]
        if not entry.value:
            typer.echo(f"{name:<{width}}  not set")
            continue
        where = common.source_label(entry.source)
        also = f" (also set in {', '.join(map(common.source_label, entry.shadows))})"
        typer.echo(f"{name:<{width}}  set  from {where}{also if entry.shadows else ''}")
    common.show_findings(layers)


@secrets_app.command("remove")
@guard
def secrets_remove(name: str = _SECRET, yes: bool = _YES) -> None:
    """Delete an API key from the secret store."""
    _secret(name)
    common.refuse_in_legacy_mode("the secret store")
    if name not in config_store.saved_secret_names():
        typer.echo(f"{name} is not in the secret store; nothing to do.")
        return
    common.confirm(f"Delete {name} from the secret store?", yes=yes)
    config_store.delete_secret(name)
    typer.echo(f"{name} removed from the secret store.")
    layers, _ = common.load_layers()
    entry = layers.entries[name]
    if entry.value:
        typer.echo(f"{name} is still set, from {common.source_label(entry.source)}.")
