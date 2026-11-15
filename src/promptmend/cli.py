"""The Typer wiring of the promptmend CLI: the app and its version callback, the improve and
persona trigger commands, and the lazy mounting of the management commands (commands/, the
interface) so a trigger never loads them. The trigger run itself is in trigger.py (_emit(),
the watchdog, _improve()); the rewrite's domain steps are in pipeline.py, the sanitiser in
sanitize.py (#281). persona's placeholder and _persona_alone() stay here with the command:
they are persona's whole logic, and doctor and scripts/check_wheel.py read the placeholder.

`python -m promptmend.cli` runs the app (entry.self_command(), the smoke test)."""

from __future__ import annotations

import contextlib
import io
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from typer.core import TyperGroup

from . import recorder
from .config import (
    EFFORTS,
    KEEP,
    OUTPUTS,
    TIERS,
    ConfigLayers,
    Settings,
)
from .factory import PROVIDER_NAMES
from .pipeline import Rewrite
from .redaction import redact_words
from .trigger import _emit, _improve, _Watchdog

if TYPE_CHECKING:
    from typer._click import Command, Context

    from .deploy import Plan

# The management commands (#92) and the interface (#93), each in a module under commands/:
# command name -> (module, its Typer app). Loaded only when that command runs or --help lists
# it, so improve and persona never import them.
_LAZY_COMMANDS = {
    "setup": ("setup", "app"),
    "config": ("settings", "config_app"),
    "secrets": ("settings", "secrets_app"),
    "profiles": ("profiles", "app"),
    "stats": ("usage", "stats_app"),
    "history": ("usage", "history_app"),
    "doctor": ("doctor", "app"),
    "ui": ("ui", "app"),
}


class _LazyGroup(TyperGroup):
    """Mounts the management commands lazily, and keeps a key typed in the wrong place (a
    stray argument, an unknown command) out of Click's usage errors, which quote it."""

    def make_context(self, *args: Any, **kwargs: Any) -> Context:
        with _redacted_usage_errors():
            return super().make_context(*args, **kwargs)

    def main(self, *args: Any, **kwargs: Any) -> Any:
        # Usage lines name `promptmend` however it was started (`python -m`).
        if len(args) < 2 and not kwargs.get("prog_name"):
            kwargs["prog_name"] = PROGRAM
        return super().main(*args, **kwargs)

    def invoke(self, ctx: Context) -> Any:
        with _redacted_usage_errors():
            return super().invoke(ctx)

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        # D-UI-1: a bare `promptmend` on a terminal opens the interface (`ui`). Anywhere
        # else no_args_is_help prints the help and exits 2, exactly as before.
        if not args and not ctx.resilient_parsing:
            from .commands.ui import on_a_terminal

            if on_a_terminal():
                args = ["ui"]
        return super().parse_args(ctx, args)

    def list_commands(self, ctx: Context) -> list[str]:
        return [*super().list_commands(ctx), *_LAZY_COMMANDS]

    def get_command(self, ctx: Context, cmd_name: str) -> Command | None:
        if cmd_name not in _LAZY_COMMANDS:
            return super().get_command(ctx, cmd_name)
        import importlib

        module_name, attr = _LAZY_COMMANDS[cmd_name]
        module = importlib.import_module(f".commands.{module_name}", __package__)
        return typer.main.get_command(getattr(module, attr))


PROGRAM = "promptmend"


@contextlib.contextmanager
def _redacted_usage_errors() -> Iterator[None]:
    from typer._click.exceptions import ClickException

    try:
        yield
    except ClickException as exc:
        exc.message = redact_words(exc.message)
        raise


app = typer.Typer(add_completion=False, no_args_is_help=True, cls=_LazyGroup)


def _show_version(value: bool) -> None:
    if value:
        from . import __version__

        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        is_eager=True,
        callback=_show_version,
        help="Print the installed version and exit",
    ),
) -> None:
    """Rewrite a draft into a clearer prompt, and set up and manage PromptMend."""
    # This callback keeps ``improve`` an explicit subcommand, so the Espanso match files
    # and the docs (``promptmend improve ...``) resolve.
    # A piped stdout uses the ANSI code page on Windows, which lacks many letters (Czech ř,
    # Vietnamese ố): printing such a rewrite would crash into a blank expansion.
    for stream in (sys.stdin, sys.stdout):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


# Set only by the managed Espanso matches, each to its own literal value; hidden from --help.
_TRIGGER_ID = typer.Option(
    None,
    "--trigger-id",
    hidden=True,
    help=f"Usage-history trigger: {', '.join(recorder.TRIGGER_IDS)}",
)


# Options are plain strings, not Enum choices: Typer would reject a bad value with a usage
# error on stderr and exit 2, which Espanso cannot show. make_provider() reports it inline.
@app.command()
def improve(
    provider: str | None = typer.Option(None, help=", ".join(PROVIDER_NAMES)),
    profile: str | None = typer.Option(
        None,
        help=(
            "Defaults to PROMPT_PROFILE (PROMPT_PRO_PROFILE, if set, with --tier pro on the pro "
            "model)"
        ),
    ),
    model: str | None = typer.Option(
        None, help="Override the model for this call; slug@endpoint also pins the endpoint"
    ),
    tier: str = typer.Option(
        "standard",
        help=f"{', '.join(TIERS)}; pro uses the OPENROUTER_PRO_* reasoning model (OpenRouter only)",
    ),
    effort: str | None = typer.Option(
        None,
        help=f"Reasoning effort for this call (OpenRouter, OpenAI): {', '.join((KEEP, *EFFORTS))}",
    ),
    max_tokens: str | None = typer.Option(None, help=f"Output cap for this call, or {KEEP}"),
    timeout: str | None = typer.Option(None, help=f"Request timeout in seconds, or {KEEP}"),
    source: str = typer.Option("clipboard", help="clipboard, stdin, or argument"),
    text: str | None = typer.Option(None, help="Input when source is argument"),
    output: str | None = typer.Option(
        None,
        "--output",
        help=f"{' or '.join(OUTPUTS)}: paste the rewrite, or copy it and print only markers; "
        "default: PROMPT_OUTPUT",
    ),
    copy: bool = typer.Option(
        False,
        help="With paste output, also copy it to the clipboard (overwrites --source "
        "clipboard's draft)",
    ),
    allow_flagged: bool = typer.Option(
        False,
        "--allow-flagged",
        help="Send this draft once despite labels, IDs, emails or IBANs the gate flagged "
        "(never keys, cards or passwords); the output says so",
    ),
    trigger_id: str | None = _TRIGGER_ID,
) -> None:
    """Improve a draft prompt. Errors are printed inline so Espanso shows them."""
    rec = recorder.Recorder("improve", trigger_id)
    ran = Rewrite()
    watch = _Watchdog(rec, ran)
    try:
        _improve(
            watch,
            rec,
            ran,
            provider=provider,
            profile=profile,
            model=model,
            tier=tier,
            effort=effort,
            max_tokens=max_tokens,
            timeout=timeout,
            source=source,
            text=text,
            output=output,
            copy=copy,
            allow_flagged=allow_flagged,
        )
    finally:
        watch.cancel()


PERSONA_PLACEHOLDER = "I am working as [role] in [company]."


def _persona_alone() -> str:
    """PROMPT_PERSONA when the full settings fail to load: another setting's bad value or a
    failing secret store must not hide it from the -p- snippet. Empty when the persona itself
    cannot be read (a broken config.toml or .env, a persona line run into the next one)."""
    try:
        return ConfigLayers.resolve(only="PROMPT_PERSONA").entries["PROMPT_PERSONA"].value
    except Exception:
        return ""


@app.command()
def persona(trigger_id: str | None = _TRIGGER_ID) -> None:
    """Print PROMPT_PERSONA for the -p- snippet, or a fill-in placeholder when unset."""
    # No watchdog (#307): persona makes no network call and reads no clipboard; it only
    # loads the settings, as improve does before its watchdog is armed.
    rec = recorder.Recorder("persona", trigger_id)
    try:
        cfg = Settings.load()
        rec.track(cfg)
        text = cfg.persona or PERSONA_PLACEHOLDER
    except Exception:
        # Same contract as improve: never a traceback or blank expansion in Espanso.
        text = _persona_alone() or PERSONA_PLACEHOLDER
    _emit(text)
    rec.emitted()
    rec.finish()


# Deployment commands. Not on the trigger path: each imports the deploy module only when run,
# so improve and persona never load it.
espanso_app = typer.Typer(
    no_args_is_help=True, help="Deploy the match files into Espanso, check them, or remove them."
)
app.add_typer(espanso_app, name="espanso")

_ESPANSO_DIR = typer.Option(None, "--espanso-dir", help="Default: `espanso path config`")
_LAUNCHER = typer.Option(
    None, "--launcher", help="CLI path to write into the matches; default: this install's own"
)
_YES = typer.Option(False, "--yes", "-y", help="Apply without asking")
_NO_RESTART = typer.Option(False, "--no-restart", help="Do not restart Espanso afterwards")


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"error: {exc}", err=True)
    return typer.Exit(1)


def _espanso_root(espanso_dir: str | None) -> Path:
    """Espanso's config folder, absolute, so the manifest records one spelling of each path."""
    from . import deploy
    from .commands.common import no_key

    no_key(espanso_dir, "--espanso-dir")
    if espanso_dir is not None:
        return Path(espanso_dir).expanduser().resolve()
    found = deploy.locate_espanso_dir()
    if found.fallback:
        # Never silently: the default folder may not be the one Espanso reads (#115).
        typer.echo(f"{found.fallback}; using the default Espanso folder {found.path}", err=True)
    return found.path.resolve()


def _make_plan(espanso_dir: str | None, launcher: str | None) -> Plan:
    from . import deploy
    from .commands.common import no_key

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


def _restart(no_restart: bool) -> None:
    from . import deploy

    if no_restart:
        typer.echo("Espanso was not restarted (--no-restart).")
    elif not deploy.restart_espanso():
        typer.echo("Could not restart Espanso; run `espanso restart` yourself.", err=True)


@espanso_app.command("status")
def espanso_status(
    espanso_dir: str | None = _ESPANSO_DIR,
    launcher: str | None = _LAUNCHER,
    diff: bool = typer.Option(False, "--diff", help="Show what deploy would change"),
) -> None:
    """Report each match file: missing, in sync, stale, modified or foreign, then your own."""
    from . import deploy

    try:
        the_plan = _make_plan(espanso_dir, launcher)
    except (deploy.DeployError, ValueError, OSError) as exc:
        raise _fail(exc) from None
    for step in the_plan.steps:
        typer.echo(f"  {step.state:<9} {step.name}")
        if diff and step.state != deploy.IN_SYNC:
            typer.echo(step.diff(), nl=False)
    for path in the_plan.yours:
        link = ", a link" if path.is_symlink() else ""
        note = f"not managed{link}; never touched by deploy or detach"
        typer.echo(f"  yours     {path.name} ({note})")


def _ask_choice(name: str, state: str) -> str:
    from . import deploy
    from .commands.common import require_terminal

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
    from . import deploy
    from .commands.common import confirm

    try:
        if on_conflict is not None and on_conflict not in deploy.CHOICES:
            raise deploy.DeployError(f"--on-conflict must be one of {', '.join(deploy.CHOICES)}")
        the_plan = _make_plan(espanso_dir, launcher)
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
    except (deploy.DeployError, ValueError, OSError) as exc:
        raise _fail(exc) from None
    for line in outcome.lines:
        typer.echo(line)
    if outcome.changed:
        _restart(no_restart)
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
def espanso_detach(
    keep_static: bool = typer.Option(
        True,
        "--keep-static/--remove-all",
        help="Remove only the matches that call the CLI (default), or every file we deployed",
    ),
    espanso_dir: str | None = _ESPANSO_DIR,
    yes: bool = _YES,
    no_restart: bool = _NO_RESTART,
) -> None:
    """Remove the match files promptmend deployed. Files you edited are kept."""
    from . import deploy
    from .commands.common import confirm

    try:
        manifest = deploy.Manifest.load()
        if not manifest.entries:
            typer.echo("Nothing to do: promptmend has no deployed match files on record.")
            return
        mode = "the CLI-calling match files" if keep_static else "every deployed match file"
        typer.echo(f"Detach removes {mode} that you have not edited:")
        for target in sorted(manifest.entries):
            typer.echo(f"  {target}")
        confirm("Detach?", yes=yes)
        outcome = deploy.detach(manifest, _espanso_root(espanso_dir), remove_all=not keep_static)
    except (deploy.DeployError, OSError) as exc:
        raise _fail(exc) from None
    for line in outcome.lines:
        typer.echo(line)
    if outcome.changed:
        _restart(no_restart)


if __name__ == "__main__":
    # `python -m promptmend.cli` (the smoke test): the console
    # script's entry.main() silences a trigger's warnings before this module's imports.
    from .entry import quiet_trigger

    quiet_trigger(sys.argv[1:])  # late here: this module's imports have already run
    app()
