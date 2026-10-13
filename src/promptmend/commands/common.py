"""What every management command shares: exit codes, the repair-mode settings, prompts that
never run without a terminal, secret input, and output that respects NO_COLOR.

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

from .. import config
from ..config import DEFAULT_SOURCE, ENV_SOURCE, ConfigLayers, Entry, Settings, secret_names
from ..redaction import safe_repr

# Exit codes (docs/commands.md). 2 is Click's own usage error (an unknown option, a bad value).
OK = 0
FAILED = 1
USAGE = 2
NEEDS_TERMINAL = 3  # a question or a confirmation was needed, but stdin is not a terminal
PROBLEMS = 4  # doctor or config validate ran and found problems

# Settings shown only as set or not set: personal text that has no place in a pasted report.
PRIVATE = frozenset({"PROMPT_PERSONA"})
# The user's own credential patterns: its value may match itself, so it is never hidden or
# refused by them.
EXTRA_PATTERNS = "PROMPT_EXTRA_PATTERNS"


class CommandError(Exception):
    """A management command that failed; the message is shown as `error: <message>`."""

    def __init__(self, message: str, code: int = FAILED) -> None:
        super().__init__(message)
        self.code = code


def guard[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Turn any failure into `error: …` on stderr and a non-zero exit, never a traceback: a
    broken config, a full disk or a bug all end the same readable way. Service errors carry a
    message meant for the user (never a secret value); anything else is named as unexpected."""

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
        except CommandError as exc:
            fail(str(exc), exc.code)
        except expected as exc:
            fail(str(exc) or type(exc).__name__)
        except Exception as exc:
            fail(f"unexpected {type(exc).__name__}: {exc}")

    return wrapper


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


def confirm(question: str, *, yes: bool, hint: str = "pass --yes") -> None:
    """Go on when ``yes`` or the person agrees; exit 1 when they decline, NEEDS_TERMINAL
    when there is no terminal to ask."""
    if yes:
        return
    require_terminal(question.rstrip("?"), hint)
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


def load_layers() -> tuple[ConfigLayers, Settings]:
    """The settings in repair mode (#83): a broken .env or TOML is reported, not raised, and a
    rejected value falls back to the next lower layer, so every command still runs."""
    layers = ConfigLayers.resolve(strict=False)
    # Cannot raise: a rejected value fell back to a lower layer, at worst the default.
    return layers, layers.settings()


def show_findings(layers: ConfigLayers) -> None:
    for finding in layers.findings:
        warn(f"{source_label(finding.source)}: {finding.message}")


def legacy_env() -> str | None:
    """The .env PROMPTMEND_ENV (or its alias) names: then it alone is read, never config.toml
    or the secret store, so a command that would write them refuses."""
    return config.env_file_override()


def refuse_in_legacy_mode(what: str) -> None:
    """Raise CommandError when PROMPTMEND_ENV means ``what`` would not be read."""
    path = legacy_env()
    if path:
        name = config.env_file_var()
        raise CommandError(
            f"{name} is set ({path}), so {what} would not be read; edit that file, "
            f"or unset {name} and run `promptmend config migrate`"
        )


def source_label(source: str) -> str:
    if source == DEFAULT_SOURCE:
        return "default"
    if source == ENV_SOURCE:
        return "environment"
    return source.removeprefix("file:")


def short_label(source: str) -> str:
    """``source_label()`` for the interface (#174): a file under the home folder reads
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
    own = name != EXTRA_PATTERNS
    if (
        name in secret_names()
        or name in PRIVATE
        or looks_like_a_key(entry.value, user_patterns=own)
    ):
        return "<set>" if entry.value else "<not set>"
    if not entry.value and entry.source != DEFAULT_SOURCE:
        return "(empty)"
    if entry.value.isprintable() and "\n" not in entry.value:
        return entry.value
    # The user's patterns would hide the pattern setting itself when it matches its own text.
    return safe_repr(entry.value, user_patterns=own)


def looks_like_a_key(value: str, *, user_patterns: bool = True) -> bool:
    """A value the gate would block as a credential (an API key, a token, a password), or
    one the user's PROMPT_EXTRA_PATTERNS match (from the settings loaded last, see
    redaction.set_user_patterns; ``user_patterns`` false skips them, for the value of
    PROMPT_EXTRA_PATTERNS itself, which may match its own text): it belongs in the secret
    store, so it is never saved in config.toml or printed."""
    from .. import redaction

    extra = redaction._user_patterns if user_patterns else ()
    return bool(set(redaction.scan(value, extra)) - redaction.SOFT_FINDINGS)


def no_key(value: str | None, option: str) -> None:
    """Refuse a key given to an option that takes a path or a name, before it is used or
    echoed back (a command prints the paths it works on)."""
    if value and looks_like_a_key(value):
        raise typer.BadParameter(f"{option} looks like a key; a key is never an argument")


def json_dump(data: Any) -> None:
    import json

    typer.echo(json.dumps(data, indent=2, default=str))
