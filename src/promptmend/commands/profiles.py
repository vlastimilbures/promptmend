"""`profiles list`, over prompt_builder's user profiles (#85)."""

from __future__ import annotations

import typer

from ..prompt_builder import ALIASES, PROFILES, user_profiles, user_profiles_dir
from . import common
from .common import guard

app = typer.Typer(
    name="profiles",
    no_args_is_help=True,
    add_completion=False,
    help="List the rewrite profiles: the built-in ones and yours.",
)


@app.callback()
def _group() -> None:
    """Keeps `profiles` a group (`profiles list`): Typer runs a lone command directly."""


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
