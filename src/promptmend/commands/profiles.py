"""`profiles list / migrate`, over prompt_builder's user profiles and profiles.py (#85)."""

from __future__ import annotations

from pathlib import Path

import typer

from .. import config
from .. import profiles as service
from ..prompt_builder import ALIASES, PROFILES, user_profiles, user_profiles_dir
from . import common
from .common import guard

app = typer.Typer(
    name="profiles",
    no_args_is_help=True,
    add_completion=False,
    help="List the rewrite profiles, or copy a checkout's edited ones to your profile folder.",
)


@app.command("list")
@guard
def profiles_list() -> None:
    """The built-in profiles and the ones in your profile folder, and which the triggers use."""
    layers, settings = common.load_layers()
    common.show_findings(layers)
    typer.echo(f"PROMPT_PROFILE: {settings.profile}")
    typer.echo(f"PROMPT_PRO_PROFILE: {settings.pro_profile or '(empty: PROMPT_PROFILE)'}")
    typer.echo("Built in:")
    for name in PROFILES:
        typer.echo(f"  {name}")
    for alias, target in ALIASES.items():
        typer.echo(f"  {alias} (retired; means {target})")
    typer.echo(f"Yours, in {user_profiles_dir()}:")
    found = user_profiles(settings.profile_overrides)
    if not found:
        typer.echo("  (none)")
    for profile in found:
        typer.echo(f"  {profile.name}: {profile.status}")


def find_checkout(path: str | None) -> Path:
    root = config._PROJECT_ROOT if path is None else Path(path).expanduser()
    source = root / service.prompts_path(root)
    if not source.is_dir():
        hint = "pass --checkout PATH" if path is None else "is it a checkout of this project?"
        raise common.CommandError(f"no {service.PROMPTS_PATH} under {root}; {hint}")
    return root


@app.command("migrate")
@guard
def profiles_migrate(
    checkout: str | None = typer.Option(
        None, "--checkout", help="The repository checkout; default: this editable install's"
    ),
    rev: str | None = typer.Option(
        None, "--rev", help="Compare with this git revision; default: the upstream merge base"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only list what would be copied"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Copy without asking"),
) -> None:
    """Copy the profiles you added or edited in a checkout's src/promptmend/prompts to your
    profile folder, where an upgrade cannot replace them. Copies only, never overwrites."""
    common.no_key(checkout, "--checkout")
    common.no_key(rev, "--rev")
    root = find_checkout(checkout)
    source = root / service.prompts_path(root)
    pristine = service.git_pristine_profiles(root, rev)
    changed = service.changed_profiles(source, pristine)
    if not changed:
        typer.echo(
            service.NOTHING_CHANGED
            if rev is None
            else f"No added or edited profiles since {rev}; nothing to copy."
        )
        return
    dest = user_profiles_dir()
    for name, change in changed.items():
        typer.echo(f"  {name}.md ({change}) -> {dest / f'{name}.md'}")
    if dry_run:
        typer.echo("Dry run: nothing was copied.")
        return
    common.confirm("Copy these profiles?", yes=yes)
    for item in service.migrate_profiles(source, pristine, dest):
        typer.echo(f"  {item.name}: {item.status}")
    hint = service.overrides_hint(changed, common.load_layers()[1].profile_overrides)
    if hint:
        typer.echo(hint)
