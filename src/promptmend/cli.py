from __future__ import annotations

import contextlib
import io
import re
import sys
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from typer.core import TyperGroup

from . import recorder
from .clipboard_guard import is_concealed
from .config import (
    CLIPBOARD,
    EFFORTS,
    KEEP,
    OUTPUTS,
    TIERS,
    ConfigLayers,
    Settings,
    openrouter_only,
)
from .entry import TRIGGER_COMMANDS
from .factory import PROVIDER_NAMES, make_provider
from .gate import GateBlocked, GatedProvider
from .prompt_builder import (
    TEMPLATE_MARKER,
    repair_template_tags,
    resolve_profile_name,
    strip_outer_fence,
    system_prompt,
)
from .providers.base import ProviderError, split_stop_note
from .redaction import DEFAULT_IGNORABLE, redact_message, redact_words

if TYPE_CHECKING:
    from typer._click import Command, Context

    from .deploy import Plan
    from .providers.usage import UsageObserver

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
    "shell": ("shell", "app"),
}


class _LazyGroup(TyperGroup):
    """Mounts the management commands lazily, and keeps a key typed in the wrong place (a
    stray argument, an unknown command) out of Click's usage errors, which quote it."""

    def make_context(self, *args: Any, **kwargs: Any) -> Context:
        with _redacted_usage_errors():
            return super().make_context(*args, **kwargs)

    def main(self, *args: Any, **kwargs: Any) -> Any:
        # Usage lines name `promptmend` however it was started (the alias, `python -m`).
        if len(args) < 2 and not kwargs.get("prog_name"):
            kwargs["prog_name"] = PROGRAM
        return super().main(*args, **kwargs)

    def invoke(self, ctx: Context) -> Any:
        if _management_command(self, ctx):
            if _run_as_alias():
                typer.echo(DEPRECATED_ALIAS, err=True)
            _relocate_folders()
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
# The command's name before the rename (#169): an alias that works until 1.0.0. Only the
# management commands and the interface say so; the triggers, --version and --help never do.
LEGACY_PROGRAM = "prompt-workflow"
DEPRECATED_ALIAS = "`prompt-workflow` is deprecated; use `promptmend` (removed in 1.0.0)"


def _management_command(group: _LazyGroup, ctx: Context) -> bool:
    """A management command or the interface is about to run: not a trigger, completion or a
    --help (--version is eager and exits before this)."""
    args = [*ctx._protected_args, *ctx.args]
    return not (
        ctx.resilient_parsing
        or not args
        or args[0] in TRIGGER_COMMANDS  # Espanso's: they never move a folder
        or args[0] not in group.list_commands(ctx)
        or {"--help", "-h"} & set(args)
    )


def _run_as_alias() -> bool:
    """Started as the deprecated `prompt-workflow` (or `prompt-workflow.exe`), #169."""
    return bool(sys.argv) and Path(sys.argv[0]).stem.lower() == LEGACY_PROGRAM


def _relocate_folders() -> None:
    """Move the legacy user folders (relocate.py, #169) before any management command or the
    interface runs, one stderr line per step."""
    from .relocate import migrate_folders

    for line in migrate_folders():
        typer.echo(line, err=True)


@contextlib.contextmanager
def _redacted_usage_errors() -> Iterator[None]:
    from typer._click.exceptions import ClickException

    try:
        yield
    except ClickException as exc:
        exc.message = redact_words(exc.message)
        raise


app = typer.Typer(add_completion=False, no_args_is_help=True, cls=_LazyGroup)

# A rough draft is a few hundred words; anything far larger is an accidental copy (a log,
# a whole document) that should neither go to the cloud nor stall the gate's scan.
MAX_DRAFT_CHARS = 50_000

# Characters no prompt needs that do harm where Espanso pastes the text: C0/C1 controls other
# than tab and newline (an escape sequence can end a terminal's bracketed paste, so the lines
# after it run as commands), and every default-ignorable code point (invisible text that can
# smuggle instructions to the next model, or make text read differently than it is: zero-width
# and bidi characters, Unicode tags, Hangul fillers, variation selectors). The joiners and the
# emoji presentation selectors are judged by _keep_run() instead.
_UNSAFE_CHARS = re.compile(
    f"(?![\u200c\u200d\ufe0e\ufe0f])[\x00-\x08\x0b-\x1f\x7f-\x9f{DEFAULT_IGNORABLE}]"
)
# Other format characters (category Cf) are dropped as a class, so ones Unicode adds later are
# too, except the visible Arabic and Kaithi prepended concatenation marks.
_KEEP_CF = frozenset("\u200c\u200d\u0600\u0601\u0602\u0603\u0604\u0605\u06dd\u070f") | {
    "\u0890",
    "\u0891",
    "\u08e2",
    "\U000110bd",
    "\U000110cd",
}
# Line breaks other than \n would join or split lines differently where the text is pasted.
_LINE_BREAKS = str.maketrans(dict.fromkeys("\r\x0b\x0c\x85\u2028\u2029", "\n"))
# After a visible character, emoji need at most one text/emoji selector and one joiner
# (❤️‍🔥 is U+2764 U+FE0F U+200D U+1F525); a longer run could encode hidden bytes.
_SELECTOR_RUN = re.compile(r"[\u200c\u200d\ufe0e\ufe0f]+")
_ALLOWED_RUN = re.compile(r"[\ufe0e\ufe0f]?[\u200c\u200d]?")
# The only ASCII characters an emoji selector follows: keycaps (#️⃣, 1️⃣).
_KEYCAP_BASES = frozenset("#*0123456789")


def _keep_run(match: re.Match[str]) -> str:
    """Keep a selector/joiner run only in the form real text uses: one selector and one
    joiner, right after a visible character. After an ASCII character only a keycap's
    selector is kept, since no ASCII text needs a joiner, so English prose carries none."""
    run, start = match.group(), match.start()
    prev = match.string[start - 1] if start else " "
    if prev.isspace() or prev == "\u2800" or not _ALLOWED_RUN.fullmatch(run):
        return ""
    if prev.isascii() and (prev not in _KEYCAP_BASES or run not in ("\ufe0e", "\ufe0f")):
        return ""
    return run


def _clean(text: str) -> str:
    text = _UNSAFE_CHARS.sub("", text.replace("\r\n", "\n").translate(_LINE_BREAKS))
    if not text.isascii():  # ASCII holds no format characters
        text = "".join(c for c in text if c in _KEEP_CF or unicodedata.category(c) != "Cf")
    # After every other removal, so a dropped character cannot split a run into allowed pieces.
    return _SELECTOR_RUN.sub(_keep_run, text)


def _emit(text: str) -> None:
    """The only way anything reaches Espanso: unsafe characters stripped, and no trailing
    newline, since Espanso inserts stdout verbatim."""
    typer.echo(_clean(text), nl=False)


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
    """Espanso-invoked prompt rewriter. Keeps ``improve`` as an explicit subcommand
    so the Espanso match files and docs (``promptmend improve ...``) resolve."""
    # A piped stdout uses the ANSI code page on Windows, which lacks many letters (Czech ř,
    # Vietnamese ố): printing such a rewrite would crash into a blank expansion.
    for stream in (sys.stdin, sys.stdout):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


class ClipboardUnavailable(ProviderError):
    """The clipboard could not be read or written."""


class ConcealedClipboard(ProviderError):
    """The clipboard held a password-manager item, which was cleared and not sent."""


def _clipboard(op: str, *args: str) -> object:
    """pyperclip's ``op`` ("paste" or "copy"). pyperclip is imported only here, once a run
    touches the clipboard: a trigger that gets its draft another way never loads it (#223)."""
    import pyperclip

    try:
        return getattr(pyperclip, op)(*args)
    except pyperclip.PyperclipException as exc:
        raise ClipboardUnavailable(f"Clipboard unavailable: {exc}") from exc


def _read_input(source: str, text: str | None) -> str:
    if source == "clipboard":
        # Checked before reading, so a vault password is never even loaded, let alone sent or
        # pasted back, whichever provider the trigger uses. None (cannot tell) reads as before.
        if is_concealed():
            # Cleared, not left in place: Espanso pastes this marker through the clipboard and
            # then restores what it held as plain text, without the concealed marker, so the
            # next trigger would send the password. Espanso restores the empty clipboard instead.
            with contextlib.suppress(ClipboardUnavailable):
                _clipboard("copy", "")
            raise ConcealedClipboard(
                "The clipboard held a password-manager item (marked concealed); it was cleared "
                "and not sent. Copy the draft first"
            )
        return str(_clipboard("paste"))
    if source == "stdin":
        # Bounded, so a piped multi-gigabyte file is never loaded whole just to be refused.
        draft = sys.stdin.read(MAX_DRAFT_CHARS + 1)
        if len(draft) > MAX_DRAFT_CHARS:
            raise ProviderError(f"Input is too long (over {MAX_DRAFT_CHARS} chars)")
        return draft
    if source == "argument":
        return text or ""
    raise ProviderError("source must be clipboard, stdin, or argument")


def _sent_despite_note(built: object) -> str:
    """A draft the gate let through with --allow-flagged says so where it is pasted, even when
    the call failed after the gate, naming only the findings."""
    if isinstance(built, GatedProvider) and built.sent_despite:
        return f"[promptmend: sent despite: {', '.join(built.sent_despite)}]\n\n"
    return ""


def _copy_failed(text: str, rec: recorder.Recorder) -> str:
    """Copy ``text`` (the rewrite) for clipboard output and --copy: "" on success, else the
    marker to print before the rewrite, which is then pasted rather than lost (outcome
    clipboard_failed)."""
    try:
        _clipboard("copy", text)
    except Exception as exc:  # any failure: this runs after improve's guard, and must not lose it
        rec.outcome = recorder.CLIPBOARD_FAILED
        reason = (
            str(exc)
            if isinstance(exc, ClipboardUnavailable)
            else f"Clipboard unavailable: {redact_message(str(exc))}"
        )
        return f"[promptmend: {reason}; the rewrite is pasted instead]\n\n"
    return ""


def _deliver(result: str, note: str, rec: recorder.Recorder) -> str:
    """Clipboard output (#134): copy the rewrite and return what to print, which is only the
    markers (the sent-despite ``note``, a stop note), so a plain success prints nothing and
    Espanso just erases the trigger. If the copy fails, the rewrite is printed after the
    error marker instead (_copy_failed()). The stop note (split_stop_note()) is printed, never
    copied."""
    rewrite, stop = split_stop_note(result)
    error = _copy_failed(rewrite, rec)
    if error:
        return f"{note}{error}{result}"
    return "\n\n".join(marker for marker in (note.rstrip("\n"), stop) if marker)


# The failures a marker words as expected; anything else is an unexpected error.
_EXPECTED_ERRORS = (ProviderError, ValueError)


def _failure(exc: Exception, rec: recorder.Recorder) -> str:
    """The history outcome of a run that printed a marker for ``exc``."""
    if isinstance(exc, ConcealedClipboard):
        return recorder.CONCEALED_REFUSED
    if isinstance(exc, ClipboardUnavailable):
        return recorder.CLIPBOARD_FAILED
    if isinstance(exc, GateBlocked):
        return recorder.GATE_BLOCKED
    if not isinstance(exc, _EXPECTED_ERRORS):
        return recorder.UNEXPECTED_ERROR
    # Providers raise after their HTTP call returned only when the reply's content is bad.
    if isinstance(exc, ProviderError) and rec.answered:
        return recorder.VALIDATION_FAILED
    return recorder.ERROR_MARKER


@dataclass
class Rewrite:
    """How far a rewrite() got, kept when it raises: the profile it resolved (the history's
    profile_id) and the provider it built (for the sent-despite note of a failed call)."""

    profile_id: str | None = None
    built: object = None


def rewrite(
    raw: str,
    provider: str,
    cfg: Settings,
    profile: str | None,
    ran: Rewrite,
    *,
    allow_flagged: bool = False,
    observer: UsageObserver | None = None,
    force_remote: bool = False,
) -> str:
    """Rewrite the draft ``raw`` with ``provider`` on ``cfg`` (the call's settings), as improve
    does once it has read its input; the interface's Try tab calls it too. Raises on any
    failure (marker() words it), with what was known by then kept in ``ran``.
    ``force_remote`` is make_provider()'s, for the Try tab's stub runs only; improve never
    passes it."""
    # Checked before cleaning, which would otherwise run over a pasted multi-megabyte log.
    if len(raw) > MAX_DRAFT_CHARS:
        raise ProviderError(f"Input is too long ({len(raw)} chars, max {MAX_DRAFT_CHARS})")
    draft = _clean(raw)
    if not draft.strip():
        raise ProviderError("Input is empty")

    # Data-protection gate: make_provider wraps anything that can send the draft off this
    # machine in GatedProvider, so it cannot be bypassed.
    name = profile or cfg.profile
    system = system_prompt(name, cfg.persona, cfg.profile_overrides)
    ran.profile_id = resolve_profile_name(name)
    built = make_provider(
        provider, cfg, allow_flagged=allow_flagged, observer=observer, force_remote=force_remote
    )
    ran.built = built
    # Cleaned first, so an invisible character cannot hide a fence from the strip. A reply
    # wrapped in one code fence would be pasted with the fence (flash-lite wrapped 16 of 36
    # with the old general profile). A stop note is split off first, since it would leave the
    # fence short of the reply's end.
    body, stop = split_stop_note(_clean(built.generate(draft, system)))
    body = strip_outer_fence(body)
    if TEMPLATE_MARKER in system:
        body = repair_template_tags(body)
    return f"{body}\n\n{stop}" if stop else body


def marker(exc: Exception) -> str:
    """The bracketed marker printed in place of a rewrite that failed with ``exc``."""
    # Pasted into the focused app: a Python message can quote the value that caused it. Our
    # ProviderErrors word their own text; a ValueError may come from a library.
    if isinstance(exc, ProviderError):
        return f"[promptmend: {exc}]"
    if isinstance(exc, _EXPECTED_ERRORS):
        return f"[promptmend: {redact_message(str(exc))}]"
    return f"[promptmend: unexpected error: {redact_message(str(exc))}]"


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
        None, help=f"OpenRouter reasoning effort for this call: {', '.join((KEEP, *EFFORTS))}"
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
    try:
        loaded = Settings.load()
        rec.track(loaded)
        provider = provider or loaded.provider
        cfg = loaded.for_call(
            tier,
            provider=provider,
            model=model,
            effort=effort,
            max_tokens=max_tokens,
            timeout=timeout,
            output=output,
        )
        # An unknown name is left to make_provider(), which reports it as such.
        if provider in PROVIDER_NAMES:
            openrouter_only(provider, tier, effort)
        raw = _read_input(source, text)
        # The rewrite comes back cleaned, which matters here too: --copy and clipboard output
        # put it on the clipboard.
        result = rewrite(
            raw, provider, cfg, profile, ran, allow_flagged=allow_flagged, observer=rec.observer
        )
    except Exception as exc:
        # Nothing may traceback or exit nonzero: Espanso cannot surface stderr, so emit a
        # visible bracketed marker instead of a blank expansion.
        _emit(_sent_despite_note(ran.built) + marker(exc))
        rec.emitted()
        rec.profile_id = ran.profile_id
        rec.outcome = _failure(exc, rec)
        # After the paste text is out, so history can never change it (see recorder.py).
        rec.finish()
        raise typer.Exit(0) from None

    rec.profile_id = ran.profile_id
    note = _sent_despite_note(ran.built)
    if cfg.output == CLIPBOARD:
        _emit(_deliver(result, note, rec))
    else:
        # Like clipboard output, the stop note is printed but not copied.
        _emit(note + (_copy_failed(split_stop_note(result)[0], rec) if copy else "") + result)
    rec.emitted()
    rec.finish()


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
    if the_plan.legacy is not None:
        typer.echo(f"  legacy    {the_plan.legacy.name} (deploy retires it, with a backup)")
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
        if the_plan.legacy is not None:
            typer.echo(f"  legacy    {the_plan.legacy.name} will be retired, with a backup")
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
    # `python -m promptmend.cli` (the smoke test, the interface's command line): the console
    # script's entry.main() silences a trigger's warnings before this module's imports.
    from .entry import quiet_trigger

    quiet_trigger(sys.argv[1:])  # late here: this module's imports have already run
    app()
