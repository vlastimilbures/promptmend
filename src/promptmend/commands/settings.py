"""`config` and `secrets`: the settings and API keys, printed over settings_service.py (the
checks and saves setup and the interface share) and config_store.py. A key is read only from
stdin or a hidden prompt, never from an argument, and no command prints one."""

from __future__ import annotations

import typer

from .. import config, config_store, settings_service
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
    help="Save, check or remove API keys in the secrets file. Keys are never arguments.",
)

_NAME = typer.Argument(..., help="A setting name, e.g. PROMPT_PROVIDER", show_default=False)
_SECRET = typer.Argument(..., help=", ".join(secret_names()), show_default=False)
_YES = typer.Option(False, "--yes", "-y", help="Do it without asking")


def _setting(name: str) -> str:
    if name not in env_names():
        raise typer.BadParameter(f"{safe_repr(name)} is not a setting; see `config show`")
    return name


def _secret(name: str) -> str:
    try:
        return config_store.secret_name(name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def _warn_after_save(name: str) -> None:
    for note in settings_service.after_save(name):
        common.warn(note)


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
    if settings_service.legacy_env():
        lines.append(
            f"  ({config.ENV_FILE_VAR} is set: config.toml and the secrets file are not read)"
        )
    return lines


@config_app.command("show", short_help="Each setting's effective value and where it comes from.")
@guard
def config_show(
    raw: bool = typer.Option(False, "--raw", help="Also list what each settings file sets"),
) -> None:
    """Each setting's effective value and where it comes from. Keys and the persona are shown
    only as set or not set; `overrides` names the files whose value lost to it."""
    layers, _ = settings_service.load_layers()
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
    layers, _ = settings_service.load_layers()
    common.show_findings(layers)
    value = layers.entries[name].value
    if settings_service.key_like(name, value):
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
    saved = settings_service.save_setting(name, value)
    typer.echo(f"{name} saved in {saved.path}")
    _warn_after_save(name)


@config_app.command("unset")
@guard
def config_unset(name: str = _NAME) -> None:
    """Remove a setting from config.toml, so its default applies again."""
    _setting(name)
    removed, path = settings_service.unset_setting(name)
    if not removed:
        typer.echo(f"{name} is not saved in {path}; nothing to do.")
        return
    typer.echo(f"{name} removed from {path}; its default applies.")
    _warn_after_save(name)


@config_app.command("validate")
@guard
def config_validate() -> None:
    """Check the settings as improve reads them. Exit 4 when there is anything to fix."""
    from ..prompt_builder import system_prompt
    from ..settings_checks import gate_packs_note, persona_problem

    problems = []
    try:
        ConfigLayers.resolve().settings()
    except ValueError as exc:
        problems.append(f"improve would stop with: {exc}")
    layers, settings = settings_service.load_layers()
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
    if note := gate_packs_note(settings):
        typer.echo(f"note: {note}")
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
    """Save an API key in the secrets file (asks with hidden input, or reads --stdin)."""
    _secret(name)
    # Before asking for a key it cannot save.
    settings_service.refuse_in_legacy_mode("the secrets file")
    value = common.read_secret(name, from_stdin=stdin)
    typer.echo(settings_service.store_key(name, value))
    _warn_after_save(name)


@secrets_app.command("status")
@guard
def secrets_status() -> None:
    """Whether each key is set and where it comes from; never the value."""
    layers, _ = settings_service.load_layers()
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
    """Delete an API key from the secrets file."""
    _secret(name)
    settings_service.refuse_in_legacy_mode("the secrets file")
    if name not in config_store.saved_secret_names():
        typer.echo(f"{name} is not in the secrets file; nothing to do.")
        return
    common.confirm(f"Delete {name} from the secrets file?", yes=yes)
    config_store.delete_secret(name)
    typer.echo(f"{name} removed from the secrets file.")
    layers, _ = settings_service.load_layers()
    entry = layers.entries[name]
    if entry.value:
        typer.echo(f"{name} is still set, from {common.source_label(entry.source)}.")
