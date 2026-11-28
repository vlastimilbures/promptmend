"""`espanso deploy`, `status` and `detach`: thin Typer wrappers over the deploy service,
mounted through cli._LAZY_COMMANDS like the other management commands, so a trigger never
loads it; each command imports deploy only when it runs. Each command runs under
common.guard: an error is one `error:` line on stderr, never a traceback.
make_plan() and restart() are public: setup's deploy step (commands/setup.py) uses them too."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from .common import guard

if TYPE_CHECKING:
    from ..deploy import Plan

espanso_app = typer.Typer(
    name="espanso",
    no_args_is_help=True,
    add_completion=False,
    help="Deploy the match files into Espanso, check them, or remove them.",
)

_ESPANSO_DIR = typer.Option(None, "--espanso-dir", help="Default: `espanso path config`")
_LAUNCHER = typer.Option(
    None, "--launcher", help="CLI path to write into the matches; default: this install's own"
)
_STATUS_LAUNCHER = typer.Option(
    None,
    "--launcher",
    help="CLI path to compare the matches against; default: this install's own",
)
_YES = typer.Option(False, "--yes", "-y", help="Apply without asking")
_NO_RESTART = typer.Option(False, "--no-restart", help="Do not restart Espanso afterwards")


def _espanso_root(espanso_dir: str | None) -> Path:
    """Espanso's config folder, absolute, so the manifest records one spelling of each path."""
    from .. import deploy
    from .common import no_key

    no_key(espanso_dir, "--espanso-dir")
    if espanso_dir is not None:
        return Path(espanso_dir).expanduser().resolve()
    found = deploy.locate_espanso_dir()
    if found.fallback:
        # Never silently: the default folder may not be the one Espanso reads.
        typer.echo(f"{found.fallback}; using the default Espanso folder {found.path}", err=True)
    return found.path.resolve()


def make_plan(espanso_dir: str | None, launcher: str | None) -> Plan:
    from .. import deploy
    from .common import no_key

    no_key(launcher, "--launcher")

    if launcher is None:
        found = deploy.resolve_launcher()
        path, channel = str(found.path), found.channel
    else:
        path, channel = launcher, "given"
    target = _espanso_root(espanso_dir)
    the_plan = deploy.plan(target, deploy.launcher_text(path), deploy.Manifest.load())
    typer.echo(f"Espanso match folder: {target / 'match'}")
    typer.echo(f"Launcher ({channel}): {path}")
    return the_plan


def restart(no_restart: bool) -> None:
    from .. import deploy

    if no_restart:
        typer.echo("Espanso was not restarted (--no-restart).")
    elif not deploy.restart_espanso():
        typer.echo("Could not restart Espanso; run `espanso restart` yourself.", err=True)


@espanso_app.command("status")
@guard
def espanso_status(
    espanso_dir: str | None = _ESPANSO_DIR,
    launcher: str | None = _STATUS_LAUNCHER,
    diff: bool = typer.Option(False, "--diff", help="Show what deploy would change"),
) -> None:
    """Report each match file: missing, in sync, stale, modified or foreign, then your own."""
    from .. import deploy

    the_plan = make_plan(espanso_dir, launcher)
    for step in the_plan.steps:
        typer.echo(f"  {step.state:<9} {step.name}")
        if diff and step.state != deploy.IN_SYNC:
            typer.echo(step.diff(), nl=False)
    for path in the_plan.yours:
        link = ", a link" if path.is_symlink() else ""
        note = f"not managed{link}; never touched by deploy or detach"
        typer.echo(f"  yours     {path.name} ({note})")


def _ask_choice(name: str, state: str) -> str:
    from .. import deploy
    from .common import require_terminal

    require_terminal(f"{name} is {state}: the choice", "pass --on-conflict keep|ours|side")
    while True:
        answer = str(
            typer.prompt(
                f"{name} is {state}: keep yours, take ours (backed up), or write ours side by "
                f"side? [{'/'.join(deploy.CHOICES)}]",
                default=deploy.KEEP,
                show_default=False,
            )
        ).strip()
        if answer in deploy.CHOICES:
            return answer
        typer.echo(f"Answer one of {', '.join(deploy.CHOICES)}.")


@espanso_app.command("deploy")
@guard
def espanso_deploy(
    espanso_dir: str | None = _ESPANSO_DIR,
    launcher: str | None = _LAUNCHER,
    yes: bool = _YES,
    on_conflict: str | None = typer.Option(
        None,
        "--on-conflict",
        help="For a file you edited: keep (yours, the default with --yes), ours (replace it, "
        "with a backup) or side (write ours next to it)",
    ),
    no_restart: bool = _NO_RESTART,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the plan and the diff, then stop without writing"
    ),
) -> None:
    """Show the plan and a diff, then write the match files (asks first unless --yes)."""
    from .. import deploy
    from .common import confirm

    if on_conflict is not None and on_conflict not in deploy.CHOICES:
        # A usage error (exit 2), raised before make_plan() reads or prints anything.
        raise typer.BadParameter(
            f"must be one of {', '.join(deploy.CHOICES)}", param_hint="'--on-conflict'"
        )
    the_plan = make_plan(espanso_dir, launcher)
    if the_plan.is_noop:
        typer.echo("Nothing to do: every match file is in sync.")
        return
    for step in the_plan.steps:
        typer.echo(f"  {step.state:<9} {step.name}")
        if step.state not in (deploy.IN_SYNC, deploy.MISSING):
            typer.echo(step.diff(), nl=False)
    for key in the_plan.orphans:
        typer.echo(f"  forget    {key} (already gone)")
    if dry_run:
        typer.echo("Dry run: nothing was written.")
        return
    choices = {}
    for step in the_plan.conflicts:
        if on_conflict is not None or yes:
            choices[step.name] = on_conflict or deploy.KEEP
        else:
            choices[step.name] = _ask_choice(step.name, step.state)
    confirm("Apply this plan?", yes=yes)
    outcome = deploy.apply(the_plan, choices)
    for line in outcome.lines:
        typer.echo(line)
    if outcome.changed:
        restart(no_restart)
    # A kept file is a safe outcome, not a failure (exit 0), but it is not up to date: say so
    # last and loudly, so an install script's run cannot read as a full success.
    if outcome.kept:
        names = ", ".join(p.name for p in outcome.kept)
        typer.echo(
            f"WARNING: {len(outcome.kept)} match file(s) kept as you have them and NOT updated: "
            f"{names}. Run `promptmend espanso deploy` to choose for each, or add "
            "`--on-conflict ours` to replace them (yours are backed up).",
            err=True,
        )
    else:
        typer.echo("The match files are up to date.")


@espanso_app.command("detach")
@guard
def espanso_detach(
    keep_static: bool = typer.Option(
        True,
        "--keep-static/--remove-all",
        help="Remove only the matches that call the CLI (default), or every file we deployed",
    ),
    espanso_dir: str | None = _ESPANSO_DIR,
    yes: bool = _YES,
    no_restart: bool = _NO_RESTART,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be removed, then stop without removing"
    ),
) -> None:
    """Remove the match files promptmend deployed. Files you edited are kept."""
    from .. import deploy
    from .common import confirm

    manifest = deploy.Manifest.load()
    if not manifest.entries:
        typer.echo("Nothing to do: promptmend has no deployed match files on record.")
        return
    mode = "the CLI-calling match files" if keep_static else "every deployed match file"
    typer.echo(f"Detach removes {mode} that you have not edited:")
    if dry_run:
        # The same decisions as a real detach (deploy.detach), reported, nothing removed.
        planned = deploy.detach(
            manifest, _espanso_root(espanso_dir), remove_all=not keep_static, dry_run=True
        )
        for line in planned.lines:
            typer.echo(f"  {line}")
        typer.echo("Dry run: nothing was removed.")
        return
    for target in sorted(manifest.entries):
        typer.echo(f"  {target}")
    confirm("Detach?", yes=yes)
    outcome = deploy.detach(manifest, _espanso_root(espanso_dir), remove_all=not keep_static)
    for line in outcome.lines:
        typer.echo(line)
    if outcome.changed:
        restart(no_restart)
