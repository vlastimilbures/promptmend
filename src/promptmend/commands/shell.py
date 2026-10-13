"""`shell` (#183): a line-based REPL for a terminal, with the completion and live help of
Home's command line in the interface (promptmend/console.py), for people who would rather not
use the full-screen interface. Enter runs the line as `promptmend …` in this terminal: a
command that asks (deploy, migrate, `secrets set NAME`) asks here, as on any terminal. Only
the triggers (`improve`, `persona`), a nested shell and a key typed on the line are refused;
a line that looks like it holds a key is never echoed or kept in the history (memory only,
never a file). prompt_toolkit is imported only once the shell opens, so --help, the triggers
and every other command never load it."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import typer

from . import common
from .ui import on_a_terminal

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession

    from ..console import Resolved

app = typer.Typer(name="shell", add_completion=False)

# What a line can be, besides a command to run.
RUN = "run"
REFUSE = "refuse"  # dropped with a note, nothing runs
ERROR = "error"  # a usage error, nothing runs
HELP = "help"  # an empty line or `help`
EXIT = "exit"

EXITS = ("exit", "quit")
HELPS = ("help", "?")
PROMPT = "promptmend> "
# The interface's refusals (console.POLICY) apply, except that the shell runs `ui` (a real
# terminal) and never itself.
SHELL = ("shell",)
UI = ("ui",)
WITHHELD_NOTE = (
    "That line looked like it holds a key: nothing ran, and it was not kept. "
    "Keys are typed hidden: secrets set OPENROUTER_API_KEY"
)
LEAVE = "Type exit or quit (or press Ctrl-D) to leave."


@dataclass(frozen=True)
class Plan:
    """What Enter does with a line: ``argv`` runs (RUN), else ``message`` is printed."""

    kind: str
    argv: tuple[str, ...] = ()
    message: str = ""


def plan(line: str) -> Plan:
    """What Enter does with ``line``: run it, refuse it, show a usage error or the help, or
    leave. A leaf's --help counts only as the parser read it, never as another option's value
    (`improve --profile --help` is `improve`, refused)."""
    from typer._click.exceptions import UsageError
    from typer.core import TyperGroup

    from .. import console

    if line.strip() in EXITS:
        return Plan(EXIT)
    if line.strip() in HELPS:
        return Plan(HELP)
    if console.holds_a_key(line):
        return Plan(REFUSE, message=WITHHELD_NOTE)
    if console.refused_secret(_words(line)):
        return Plan(REFUSE, message=secret_note())
    try:
        words = console.split(line)
    except ValueError as exc:
        return Plan(ERROR, message=console.error_text(str(exc)).plain)
    if not words:
        return Plan(HELP)
    argv = tuple(words)
    found: Resolved | None
    strict = True
    try:
        found = console.resolve(words)
    except UsageError as exc:
        # `config set --help`: the CLI prints the help before it misses the arguments, but
        # only for a --help its parser reads as one.
        found = _help_only(words)
        if found is None:
            return Plan(ERROR, message=console.error_text(exc.format_message()).plain)
        strict = False
    decision = console.decide(found, words)
    if isinstance(found.command, TyperGroup):
        if decision.kind == console.RUN:
            return Plan(RUN, argv)
        return Plan(ERROR, message=console.error_text(decision.message).plain)
    why = _refused(found.path)
    # A refused command's --help only when its whole line parses (`improve --help --profile`
    # would start a trigger that prints nothing and exits 2).
    if why and (decision.kind != console.RUN or not strict):
        return Plan(REFUSE, argv, f"Not from the shell: {why}.")
    # A dialog or a terminal-only command runs as typed: this is a terminal, it asks here.
    return Plan(RUN, argv)


def _refused(path: tuple[str, ...]) -> str:
    """Why the shell never runs ``path``, or "" when it does."""
    from .. import console

    if path == SHELL:
        return "this is the shell"
    rule = console.POLICY[path]
    return rule.why if rule.kind == console.REFUSE and path != UI else ""


def _words(line: str) -> list[str]:
    """The line's words, also when it does not parse (an open quote): split on whitespace
    then, so `secrets set NAME "value` is still seen for what it is."""
    from .. import console

    try:
        return console.split(line)
    except ValueError:
        words = line.split()
        return words[1:] if words[:1] and words[0] in console.PROGRAMS else words


def secret_line(line: str) -> bool:
    """A line that holds a key, or gives a key's value (`secrets set NAME VALUE`, `config
    set NAME VALUE` for a key's name): never run, shown back or kept."""
    from .. import console

    return console.holds_a_key(line) or console.refused_secret(_words(line))


def _help_only(words: Sequence[str]) -> Resolved | None:
    from typer._click.exceptions import UsageError

    from .. import console

    try:
        found = console.resolve(words, resilient=True)
    except UsageError:
        return None
    return found if found.values.get("help") is True else None


def secret_note() -> str:
    from ..tui import teach

    return (
        f"$ promptmend … {teach.WITHHELD}: a key's value on the line, refused, and the line "
        "was not kept. Type only the key's name (secrets set OPENROUTER_API_KEY); it then "
        "asks for the value, hidden."
    )


def runs_here(argv: Sequence[str]) -> bool:
    """The last check before a child starts: a fresh parse of exactly ``argv`` (as one line)
    plans to run exactly ``argv``."""
    import shlex

    decided = plan(shlex.join(argv))
    return decided.kind == RUN and decided.argv == tuple(argv)


def kept(line: str) -> bool:
    """Whether the history may keep ``line``: only a line that parses and runs. Never one
    that holds a key or gives a key's value, nor one that does not parse (an open quote, a
    mistyped command), where a value may hide that no check recognises."""
    return plan(line).kind == RUN


def run_here(argv: Sequence[str]) -> int:
    """``promptmend argv`` in a child process on this terminal: its stdin, stdout and stderr
    are the shell's, no capture, no timeout, never a shell; ``-P``, so a module in the
    working directory never runs. Ctrl-C reaches the child (same terminal) and the shell
    waits for it to end instead of ending itself."""
    import subprocess
    import sys

    from ..entry import self_command

    if not runs_here(argv):
        raise ValueError("refused to run a line the shell does not run")
    child = subprocess.Popen(  # noqa: S603 - our own interpreter and module, an argv list
        [*self_command(), *argv]
    )
    interrupted = False
    while True:
        try:
            code = child.wait()
        except KeyboardInterrupt:
            interrupted = True
            continue
        if interrupted:
            # Ctrl-C at the child's prompt (a hidden key) left the cursor on that line.
            sys.stdout.write("\n")
            sys.stdout.flush()
        return code


def load_settings() -> None:
    """Read the settings in repair mode, as the interface does: that sets the user's
    PROMPT_EXTRA_PATTERNS, so a word only they match is withheld too. A broken settings file
    never stops the shell: it says so, and the built-in patterns still apply."""
    try:
        common.load_layers()
    except Exception as exc:
        common.warn(f"the settings could not be read ({exc}); only the built-in key patterns apply")


Runner = Callable[[Sequence[str]], int]


def recipes() -> str:
    from ..tui import teach

    width = max(len(teach.equivalent(*r.argv)) for r in teach.RECIPES)
    lines = ["Tab completes; the bar below explains the line as you type; Enter runs it."]
    lines += [f"  $ {teach.equivalent(*r.argv):<{width}}  {r.what}" for r in teach.RECIPES]
    lines.append(LEAVE)
    return "\n".join(lines)


def repl(
    read: Callable[[], str],
    runner: Runner | None = None,
    echo: Callable[[str], None] = typer.echo,
    refresh: Callable[[], None] = lambda: None,
) -> int:
    """Read lines until exit, quit or Ctrl-D (EOFError), and do what plan() says with each.
    Ctrl-C at the prompt (KeyboardInterrupt) drops the line. After each run, ``refresh``
    reads the settings again (the command may have changed PROMPT_EXTRA_PATTERNS). Returns
    the shell's exit code."""
    echo(recipes())
    while True:
        try:
            line = read()
        except KeyboardInterrupt:
            continue
        except EOFError:
            return common.OK
        decided = plan(line)
        if decided.kind == EXIT:
            return common.OK
        if decided.kind == HELP:
            echo(recipes())
        elif decided.kind == RUN:
            code = (runner or run_here)(decided.argv)
            refresh()
            if code != 0:
                echo(f"exit {code}")
        else:
            echo(decided.message)


# --- The line editor ---------------------------------------------------------------------------


def completions(text: str) -> list[tuple[str, int]]:
    """The candidates for the word before the cursor in ``text``, each with how far back
    (as a negative start position) it replaces: none inside an open quote or on a line that
    looks like it holds a key."""
    from .. import console

    if console.holds_a_key(text):
        return []
    try:
        words = console.split(text)
    except ValueError:
        return []
    if not text.strip() or text[-1].isspace():
        partial, prefix = "", words
    elif not words:
        return []  # `promptmend` itself, still without its space
    else:
        partial, prefix = words[-1], words[:-1]
    return [(w, -len(partial)) for w in console.words_for(prefix) if w.startswith(partial)]


def toolbar(text: str) -> str:
    """The live help under the prompt, as plain text."""
    from .. import console

    return console.describe(text, typing=True).plain


class Editor:
    """The prompt: completion from the Click tree, the console's suggestion (Right arrow takes
    it), live help in the bottom bar and a history in memory that keeps only lines that ran.
    Accepting a line that gives a key replaces it with <value withheld> first,
    so the line left on screen (and in the scrollback) no longer holds it; read() still
    returns what was typed, for plan() to refuse. ``kwargs`` go to PromptSession (tests pass
    a pipe input and a dummy output)."""

    def __init__(self, **kwargs: Any) -> None:
        self.held: str | None = None
        self.prompt = _session(self, **kwargs)

    def read(self) -> str:
        line = self.prompt.prompt()
        held, self.held = self.held, None
        return line if held is None else held


def _session(editor: Editor, **kwargs: Any) -> PromptSession[str]:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.completion import CompleteEvent, Completer, Completion
    from prompt_toolkit.document import Document
    from prompt_toolkit.history import InMemoryHistory

    from .. import console
    from ..tui import teach

    class Words(Completer):
        def get_completions(
            self, document: Document, complete_event: CompleteEvent
        ) -> Iterable[Completion]:
            for word, start in completions(document.text_before_cursor):
                yield Completion(word, start_position=start)

    class Suggest(AutoSuggest):
        def get_suggestion(self, buffer: Any, document: Document) -> Suggestion | None:
            text = document.text
            if console.holds_a_key(text):
                return None
            completed = console.suggest(text)
            return Suggestion(completed[len(text) :]) if completed else None

    class History(InMemoryHistory):
        def append_string(self, string: str) -> None:
            strings = list(self.get_strings())
            if kept(string) and string.strip() and (not strings or strings[-1] != string):
                super().append_string(string)

    prompt: PromptSession[str] = PromptSession(
        PROMPT,
        completer=Words(),
        auto_suggest=Suggest(),
        history=History(),
        complete_while_typing=False,
        **kwargs,
    )
    prompt.bottom_toolbar = lambda: toolbar(prompt.default_buffer.text)
    # Every way to accept a line ends in the buffer's accept handler: Enter, Meta-Enter,
    # Ctrl-O, Enter from a search. Wrapped, a key-giving line is replaced before the prompt
    # exits with it, so the line left on screen never holds the value, whichever key it was.
    accept = prompt.default_buffer.accept_handler

    def withhold(buffer: Buffer) -> bool:
        if secret_line(buffer.text):
            editor.held = buffer.text
            buffer.document = Document(teach.WITHHELD)
        return bool(accept and accept(buffer))

    prompt.default_buffer.accept_handler = withhold
    return prompt


@app.command("shell", short_help="A command line with completion and live help (a terminal).")
@common.guard
def shell() -> None:
    """Type promptmend commands with completion (Tab) and live help in the bar below; Enter
    runs each in this terminal, so a command that asks (deploy, migrate, secrets set NAME)
    asks here. The triggers (improve, persona) are refused, and a line that holds a key is
    never shown or kept. exit, quit or Ctrl-D leaves. It needs a terminal."""
    if not on_a_terminal():
        common.fail(
            "the shell needs a terminal (stdin and stdout); run the commands directly "
            "instead (see --help)",
            common.NEEDS_TERMINAL,
        )
    load_settings()
    raise typer.Exit(repl(Editor().read, refresh=load_settings))
