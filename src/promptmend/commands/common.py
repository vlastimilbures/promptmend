"""What every management command shares: exit codes, the error guard, prompts that never run
without a terminal, secret input, and output that respects NO_COLOR. The settings services
they print over (saving, the repair-mode settings, the legacy-mode refusal) are
settings_service.py, Typer-free and shared with setup and the interface.

Unlike improve and persona, these commands use ordinary exit codes (documented in
docs/commands.md, "Exit codes") and print errors to stderr.
"""

from __future__ import annotations

import functools
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import typer

from .. import settings_service
from ..config import DEFAULT_SOURCE, ENV_SOURCE, ConfigLayers, Entry, secret_names
from ..redaction import looks_like_a_key as _looks_like_a_key
from ..redaction import redact_message, safe_repr

# Exit codes (docs/commands.md). 2 is Click's own usage error (an unknown option, a bad value).
OK = 0
FAILED = 1
USAGE = 2
NEEDS_TERMINAL = 3  # a question or a confirmation was needed, but stdin is not a terminal
PROBLEMS = 4  # doctor or config validate ran and found problems

# Settings shown only as set or not set: personal text that has no place in a pasted report.
PRIVATE = frozenset({"PROMPT_PERSONA"})


def guard[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Turn any failure into `error: …` on stderr and a non-zero exit, never a traceback: a
    broken config, a full disk or a bug all end the same readable way. Service errors carry a
    message meant for the user (never a secret value); anything else is named as unexpected.
    Both pass through redacted(), since people paste this text into bug reports; a
    ServiceError's text is this project's own and is shown as it is, exiting USAGE when it
    refuses what was given (``usage``), else FAILED."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        from ..config_files import SecretStoreError
        from ..config_store import ConfigStoreError
        from ..deploy import DeployError
        from ..history import HistoryError

        expected = (
            ValueError,
            OSError,
            ConfigStoreError,
            SecretStoreError,
            DeployError,
            HistoryError,
        )
        try:
            return func(*args, **kwargs)
        except (typer.Exit, typer.Abort, typer.BadParameter, KeyboardInterrupt):
            raise
        except settings_service.ServiceError as exc:
            fail(str(exc), USAGE if exc.usage else FAILED)
        except expected as exc:
            fail(redacted(str(exc) or type(exc).__name__))
        except Exception as exc:
            fail(redacted(f"unexpected {type(exc).__name__}: {exc}"))

    return wrapper


def redacted(message: str) -> str:
    """An error's text as a management command or the interface shows it: a key, token,
    password or card in it (and what the user's PROMPT_EXTRA_PATTERNS match) is described,
    not repeated. Unlike a trigger marker it keeps an email address, a label or an ID number:
    this is the user's own terminal, and a path such as ``…/GoogleDrive-jane@example.com/…``
    is what tells them which file failed."""
    return redact_message(message, secrets_only=True)


def fail(message: str, code: int = FAILED) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def warn(message: str) -> None:
    typer.echo(f"warning: {message}", err=True)


# --- terminal -----------------------------------------------------------------------------


def stdin_is_tty() -> bool:
    """Whether a person can answer a question; tests replace it."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def require_terminal(what: str, hint: str) -> None:
    """Fail with NEEDS_TERMINAL instead of prompting when nobody can answer."""
    if not stdin_is_tty():
        fail(f"{what} needs an answer, but this is not a terminal; {hint}", NEEDS_TERMINAL)


def confirm(question: str, *, yes: bool) -> None:
    """Go on when ``yes`` or the person agrees; exit 1 when they decline, NEEDS_TERMINAL
    when there is no terminal to ask."""
    if yes:
        return
    require_terminal(question.rstrip("?"), "pass --yes")
    if not typer.confirm(question, default=False):
        fail("not confirmed; nothing was changed")


def color_enabled() -> bool:
    """Colour only on a terminal, and never with NO_COLOR set (https://no-color.org)."""
    return not os.environ.get("NO_COLOR") and stdout_is_tty()


def paint(text: str, color: str) -> str:
    return typer.style(text, fg=color) if color_enabled() else text


# --- secrets ------------------------------------------------------------------------------


def _getpass(prompt: str) -> str:
    import getpass

    return getpass.getpass(prompt)


def read_secret(name: str, *, from_stdin: bool) -> str:
    """A key from stdin (the first line, a byte-order mark dropped) or a hidden prompt; never a
    command-line argument. --stdin at a terminal asks with hidden input instead, since reading
    the terminal would show the key as it is typed."""
    if from_stdin and not stdin_is_tty():
        lines = sys.stdin.read().splitlines()
        value = lines[0].lstrip("\ufeff").strip() if lines else ""
        if not value:
            fail(f"nothing was read from stdin for {name}")
        return value
    require_terminal(f"Typing {name}", "pipe it in with --stdin")
    value = _getpass(f"{name} (input hidden): ").strip()
    if not value:
        fail(f"no value entered; {name} was not changed")
    return value


# --- settings -----------------------------------------------------------------------------


def show_findings(layers: ConfigLayers) -> None:
    for finding in layers.findings:
        warn(f"{source_label(finding.source)}: {finding.message}")


def source_label(source: str) -> str:
    if source == DEFAULT_SOURCE:
        return "default"
    if source == ENV_SOURCE:
        return "environment"
    return source.removeprefix("file:")


def short_label(source: str) -> str:
    """``source_label()`` for the interface: a file under the home folder reads
    ``~/…``, so a narrow column keeps its name. Display only; ``config show`` prints the
    full path."""
    label = source_label(source)
    if not source.startswith("file:"):
        return label
    try:
        rest = Path(label).relative_to(Path.home())
    except ValueError:
        return label
    return f"~/{rest.as_posix()}" if rest.parts else "~"


def shown_value(name: str, entry: Entry) -> str:
    """A setting's value as a command may print it: never a secret, never the persona. An
    empty value set on purpose (PROMPT_TEMPERATURE= sends no temperature) reads "(empty)"
    rather than a blank; one left at an empty default stays blank."""
    own = name != settings_service.EXTRA_PATTERNS
    if (
        name in secret_names()
        or name in PRIVATE
        or _looks_like_a_key(entry.value, user_patterns=own)
    ):
        return "<set>" if entry.value else "<not set>"
    if not entry.value and entry.source != DEFAULT_SOURCE:
        return "(empty)"
    if entry.value.isprintable() and "\n" not in entry.value:
        return entry.value
    # The user's patterns would hide the pattern setting itself when it matches its own text.
    return safe_repr(entry.value, user_patterns=own)


def no_key(value: str | None, option: str) -> None:
    """settings_service.refuse_key() as Click's usage error (exit 2): refuse a key given to an
    option that takes a path or a name, before it is used or echoed back."""
    try:
        settings_service.refuse_key(value, option)
    except settings_service.ServiceError as exc:
        raise typer.BadParameter(str(exc)) from None


def json_dump(data: Any) -> None:
    import json

    typer.echo(json.dumps(data, indent=2, default=str))
