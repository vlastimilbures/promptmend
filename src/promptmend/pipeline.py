"""The domain steps of one rewrite, shared by the improve trigger (trigger.py) and the
interface's Try tab (tui/try_pane.py): reading the draft (the clipboard, stdin or an
argument, with the concealed-item check), rewrite() through factory.make_provider() and its
gate, the marker that words a failure, its history outcome, the sent-despite note, and the
delivery of a finished rewrite to the clipboard.

It imports neither Typer nor the CLI, so the interface uses it without the command wiring
and a Typer-free trigger entry (#313) can build on it. Printing (trigger.emit()) and
improve's watchdog stay in trigger.py: both need Typer (typer.echo, typer.Exit), and the
watchdog is the one decision of what a trigger run prints."""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import recorder
from .clipboard_guard import is_concealed
from .config import Settings
from .factory import make_provider
from .gate import GateBlocked, GatedProvider
from .prompt_builder import (
    TEMPLATE_MARKER,
    repair_template_tags,
    resolve_profile_name,
    strip_outer_fence,
    system_prompt,
)
from .providers.base import ProviderError, split_stop_note
from .redaction import redact_message
from .sanitize import clean

if TYPE_CHECKING:
    from .providers.usage import UsageObserver

# A rough draft is a few hundred words; anything far larger is an accidental copy (a log,
# a whole document) that should neither go to the cloud nor stall the gate's scan.
MAX_DRAFT_CHARS = 50_000


class ClipboardUnavailable(ProviderError):
    """The clipboard could not be read or written."""


class ConcealedClipboard(ProviderError):
    """The clipboard held a password-manager item, which was cleared and not sent."""


def _clipboard(op: str, *args: str) -> object:
    """pyperclip's ``op`` ("paste" or "copy"). pyperclip is imported only here, once a run
    touches the clipboard: a trigger that gets its draft another way never loads it."""
    import pyperclip

    try:
        return getattr(pyperclip, op)(*args)
    except pyperclip.PyperclipException as exc:
        raise ClipboardUnavailable(f"Clipboard unavailable: {exc}") from exc


def read_input(source: str, text: str | None) -> str:
    if source == "clipboard":
        # Checked before reading, so a vault password is never even loaded, let alone sent or
        # pasted back, whichever provider the trigger uses. None (cannot tell) reads it anyway.
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


def sent_despite_note(built: object) -> str:
    """A draft the gate let through with --allow-flagged says so where it is pasted, even when
    the call failed after the gate, naming only the findings."""
    if isinstance(built, GatedProvider) and built.sent_despite:
        return f"[promptmend: sent despite: {', '.join(built.sent_despite)}]\n\n"
    return ""


def copy_failed(text: str, rec: recorder.Recorder) -> str:
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


def deliver(result: str, note: str, rec: recorder.Recorder) -> str:
    """Clipboard output: copy the rewrite and return what to print, which is only the
    markers (the sent-despite ``note``, a stop note), so a plain success prints nothing and
    Espanso just erases the trigger. If the copy fails, the rewrite is printed after the
    error marker instead (copy_failed()). The stop note (split_stop_note()) is printed, never
    copied."""
    rewrite, stop = split_stop_note(result)
    error = copy_failed(rewrite, rec)
    if error:
        return f"{note}{error}{result}"
    return "\n\n".join(marker for marker in (note.rstrip("\n"), stop) if marker)


# The failures a marker words as expected; anything else is an unexpected error.
_EXPECTED_ERRORS = (ProviderError, ValueError)


def failure_outcome(exc: Exception, rec: recorder.Recorder) -> str:
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
    source: str | None = None,
) -> str:
    """Rewrite the draft ``raw`` with ``provider`` on ``cfg`` (the call's settings), as improve
    does once it has read its input; the interface's Try tab calls it too. Raises on any
    failure (marker() words it), with what was known by then kept in ``ran``.
    ``force_remote`` is make_provider()'s, for the Try tab's stub runs only; improve never
    passes it. ``source`` is where improve read the draft (read_input()'s); an empty draft
    from the clipboard says to copy one, any other (the Try tab passes none) is empty input."""
    # Checked before cleaning, which would otherwise run over a pasted multi-megabyte log.
    if len(raw) > MAX_DRAFT_CHARS:
        raise ProviderError(f"Input is too long ({len(raw)} chars, max {MAX_DRAFT_CHARS})")
    draft = clean(raw)
    if not draft.strip():
        if source == "clipboard":
            raise ProviderError(
                "The clipboard has no text: copy your draft, then type the trigger again"
            )
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
    # wrapped in one code fence would be pasted with the fence (a model can wrap its whole
    # reply in one; seen in benchmark runs). A stop note is split off first, since it would
    # leave the fence short of the reply's end.
    body, stop = split_stop_note(clean(built.generate(draft, system)))
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
    if isinstance(exc, ValueError):
        return f"[promptmend: {redact_message(str(exc))}]"
    return f"[promptmend: unexpected error: {redact_message(str(exc))}]"
