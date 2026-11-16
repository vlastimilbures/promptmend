"""The Typer wiring of the promptmend CLI: the app and its version callback, the improve and
persona trigger commands, and the lazy mounting of the management commands (commands/, the
interface) so a trigger never loads them. The trigger run itself is in trigger.py (_emit(),
the watchdog, _improve()); the rewrite's domain steps are in pipeline.py, the sanitiser in
sanitize.py. persona's placeholder and _persona_alone() stay here with the command:
they are persona's whole logic, and doctor and scripts/check_wheel.py read the placeholder.

`python -m promptmend.cli` runs the app (entry.self_command(), the smoke test)."""

from __future__ import annotations

import contextlib
import io
import sys
from collections.abc import Iterator
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
from .trigger import _emit, _flushed, _improve, _Watchdog

if TYPE_CHECKING:
    from typer._click import Command, Context

# The management commands and the interface, each in a module under commands/: command
# name -> (module, its Typer app). Loaded only when that command runs or --help lists it,
# so improve and persona never import them.
# espanso first, so --help lists it right after improve and persona.
_LAZY_COMMANDS = {
    "espanso": ("espanso", "espanso_app"),
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
        # else no_args_is_help prints the help and exits 2.
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
    provider: str | None = typer.Option(
        None,
        help=f"{', '.join(PROVIDER_NAMES)}; defaults to PROMPT_PROVIDER (PROMPT_PRO_PROVIDER with "
        "--tier pro)",
    ),
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
        help=f"{', '.join(TIERS)}; pro: OPENROUTER_PRO_* on OpenRouter, the pro time limit and "
        "profile on Anthropic and OpenAI",
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
    # persona makes no network call and reads no clipboard, but loading the settings can
    # stall on a file system: the same provisional watchdog as improve's load bounds it (#331).
    rec = recorder.Recorder("persona", trigger_id)
    watch = _Watchdog(rec, Rewrite())
    try:
        try:
            # Inside the try: a timer that cannot start still leaves the persona printed.
            watch.arm_loading(None)
            cfg = Settings.load()
            rec.track(cfg)
            text = cfg.persona or PERSONA_PLACEHOLDER
        except Exception:
            # Same contract as improve: never a traceback or blank expansion in Espanso.
            text = _persona_alone() or PERSONA_PLACEHOLDER
        _emit(text, watch)
        rec.emitted()
        _flushed(watch)
        rec.finish()
    finally:
        watch.cancel()


if __name__ == "__main__":
    # `python -m promptmend.cli` (the smoke test): the console
    # script's entry.main() silences a trigger's warnings before this module's imports.
    from .entry import quiet_trigger

    quiet_trigger(sys.argv[1:])  # late here: this module's imports have already run
    app()
