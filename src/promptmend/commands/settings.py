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
    help="Show, check and change the settings (config.toml); migrate a .env to it.",
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


def _env_file_in_use() -> Path | None:
    """The .env read today when config.toml does not exist yet: writing config.toml would make
    it unread, so its settings would silently stop applying."""
    if config_files.is_file(config.settings_file()):
        return None
    return next((p for p in config._env_file_candidates() if config_files.is_file(p)), None)


def _writable_config(what: str) -> None:
    common.refuse_in_legacy_mode(what)
    env_file = _env_file_in_use()
    if env_file is not None:
        raise common.CommandError(
            f"your settings are in {env_file}; saving {config_files.SETTINGS_FILE} would stop "
            "it being read. Run `promptmend config migrate` first, or edit that file"
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
    value that looks like a key, legacy mode and an unmigrated .env (CommandError), then save
    ``value`` in config.toml after checking it as the CLI would read it."""
    _not_a_secret(name, "set")
    if common.looks_like_a_key(value, user_patterns=name != common.EXTRA_PATTERNS):
        raise common.CommandError(
            "that value looks like a key or password, which never goes in config.toml; use "
            "`promptmend secrets set NAME` for an API key",
            common.USAGE,
        )
    _writable_config(config_files.SETTINGS_FILE)
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
            f"  ({config.env_file_var()} is set: config.toml and the secret store are not read)"
        )
    return lines


@config_app.command("show")
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


def _consent(plan_lines: list[str], token: str, *, dry_run: bool, yes: bool, given: str) -> bool:
    """Show the preview and its token; True when the change was confirmed for this exact
    preview: interactively, or by --yes with the --preview-token it printed."""
    for line in plan_lines:
        typer.echo(f"  {line}")
    typer.echo(f"Preview token: {token}")
    if dry_run:
        typer.echo("Dry run: nothing was changed.")
        return False
    if yes:
        if given != token:
            common.fail(
                "--yes needs --preview-token with the token of this preview, to confirm what you "
                "saw (it changes whenever the files do)",
                common.USAGE,
            )
        return True
    common.confirm("Go ahead?", yes=False, hint=f"re-run with --yes --preview-token {token}")
    return True


_DRY_RUN = typer.Option(False, "--dry-run", help="Only show the preview")
_TOKEN = typer.Option("", "--preview-token", help="The preview's token, required with --yes")


def _checkout(path: str | None, option: str) -> Path | None:
    common.no_key(path, option)
    return Path(path).expanduser() if path else None


@config_app.command("migrate")
@guard
def config_migrate(
    dry_run: bool = _DRY_RUN,
    yes: bool = _YES,
    preview_token: str = _TOKEN,
    source: str | None = typer.Option(
        None,
        "--from",
        help="Also copy the settings of an earlier checkout's .env (it stays in place)",
        metavar="PATH",
    ),
) -> None:
    """Move the .env in use to config.toml and its keys to the secret store, with a backup.
    Shows a preview first; applies only once you confirm it (or --yes --preview-token).
    With --from, an earlier checkout's .env fills the settings still at their default, and
    stays where it is until `config retire --from`."""
    checkout = _checkout(source, "--from")
    plan = config_store.plan_migration(source=checkout)
    if plan.status != "ready":
        for line in plan.describe():
            typer.echo(line)
        return
    typer.echo("Migration preview:")
    if not _consent(plan.describe(), plan.token, dry_run=dry_run, yes=yes, given=preview_token):
        return
    result = config_store.apply_migration(source=checkout, consent=plan.token)
    typer.echo(f"Migrated. Backup: {result.backup}")
    for moved, target in result.moved.items():
        typer.echo(f"  moved {moved} -> {target}")
    if result.copied is not None:
        typer.echo(f"  copied {result.copied} (left in place)")
        typer.echo(
            "Next: `promptmend espanso deploy`, then "
            f"`promptmend config retire --from {result.copied.parent}`."
        )
    for left in result.left:
        common.warn(
            f"{left} was not moved (edited since the preview, or not movable); it is no longer read"
        )
    typer.echo("Undo with `promptmend config rollback`.")


@config_app.command("rollback")
@guard
def config_rollback(
    dry_run: bool = _DRY_RUN, yes: bool = _YES, preview_token: str = _TOKEN
) -> None:
    """Undo `config migrate`: put each .env back and set config.toml and the secret store
    aside in the backup. Shows a preview first, like migrate."""
    plan = config_store.plan_rollback()
    typer.echo("Rollback preview:")
    if not _consent(plan.describe(), plan.token, dry_run=dry_run, yes=yes, given=preview_token):
        return
    config_store.apply_rollback(consent=plan.token)
    typer.echo("Rolled back: the .env is read again." if plan.read_again else "Rolled back.")


@config_app.command("retire")
@guard
def config_retire(
    source: str = typer.Option(
        ..., "--from", help="The earlier checkout whose settings were copied", metavar="PATH"
    ),
    espanso_dir: str | None = typer.Option(
        None, "--espanso-dir", help="Default: `espanso path config`"
    ),
    dry_run: bool = _DRY_RUN,
    yes: bool = _YES,
    preview_token: str = _TOKEN,
) -> None:
    """Move an earlier checkout's .env, whose settings `config migrate --from` copied, into
    the backup. Refused while a match file still runs that checkout's CLI: deploy first.
    Shows a preview first, like migrate; `config rollback` puts it back."""
    common.no_key(source, "--from")
    checkout = Path(source).expanduser()
    folder = _checkout(espanso_dir, "--espanso-dir")
    plan = config_store.plan_retire(checkout, espanso_dir=folder)
    typer.echo("Retire preview:")
    if not _consent(plan.describe(), plan.token, dry_run=dry_run, yes=yes, given=preview_token):
        return
    place = config_store.apply_retire(checkout, espanso_dir=folder, consent=plan.token)
    typer.echo(f"Retired: {plan.env_file} -> {place}")


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
