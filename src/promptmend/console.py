"""The command line's logic, without a screen (#111, #183): it completes and explains
`promptmend` commands as they are typed, against the CLI's own Click tree, and decides what
Enter does with them (POLICY): run a read-only command (or a --dry-run preview) in a child
process, open a dialog, send it to a terminal, or refuse it (the triggers). A word that looks
like a key is never echoed: not in the help line, the output or a history. Home's command
line in the interface (tui/console.py) and `promptmend shell` (commands/shell.py) share it.
No Textual here (tests/test_shell.py checks), and the triggers never load it."""

from __future__ import annotations

import functools
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import typer
from rich.text import Text
from typer._click import Context
from typer._click.exceptions import UsageError
from typer._types import TyperChoice
from typer.core import TyperArgument, TyperGroup, TyperOption

from . import config, prompt_builder
from .commands import common
from .redaction import redact_words
from .tui import teach

if TYPE_CHECKING:
    from typer._click import Command

# The settings whose value is a profile name: `config set PROMPT_PROFILE <profile>`.
PROFILE_SETTINGS = ("PROMPT_PROFILE", "PROMPT_PRO_PROFILE")
PROFILE_OPTION = "--profile"
# (command path, argument) -> what it takes; tests/test_tui_console.py checks each exists.
SETTING_ARGUMENTS = {("config", "get"), ("config", "set"), ("config", "unset")}
SECRET_ARGUMENTS = {("secrets", "set"), ("secrets", "remove")}
ARGUMENT = "name"
VALUE_ARGUMENT = "value"
# The words a line may start with that name the program itself.
PROGRAMS = (teach.PROGRAM, "prompt-workflow")


@functools.cache
def root() -> TyperGroup:
    """The CLI's Click tree, built once (the lazy commands are imported as they are named)."""
    from . import cli

    return cast("TyperGroup", typer.main.get_command(cli.app))


@dataclass(frozen=True)
class Resolved:
    """Where a command line leads: the commands walked, the deepest one and its context, and
    the values its parser read (by parameter name; a leaf's only)."""

    path: tuple[str, ...]
    command: Command
    context: Context
    values: Mapping[str, Any] = field(default_factory=dict)


def resolve(argv: Sequence[str], *, resilient: bool = False, partial: bool = False) -> Resolved:
    """Walk ``argv`` down the Click tree and parse the deepest command's arguments and
    options, as the CLI would before running it, but with no option's callback run (no
    --help or --version printed). Raises a click UsageError on an unknown command or option,
    a group with no command (unless ``partial``) or a word left over; ``resilient`` skips the
    check a half-typed line fails (a missing argument)."""
    command: Command = root()
    ctx = command.make_context(teach.PROGRAM, [], resilient_parsing=True)
    path: list[str] = []
    words = list(argv)
    while isinstance(command, TyperGroup):
        if not words or words[0].startswith("-"):
            if not words and not partial:
                raise UsageError(f"{' '.join(path) or teach.PROGRAM} needs a command.", ctx)
            # The group's own options (--version, all flags) are checked, never parsed: a
            # callback would print.
            known = {*_options(command), *command.get_help_option_names(ctx)}
            for word in words:
                if word not in known:
                    raise UsageError(f"No such option: {word}", ctx)
            return Resolved(tuple(path), command, ctx)
        sub = command.get_command(ctx, words[0])
        if sub is None or sub.hidden:
            raise UsageError(f"No such command {words[0]!r}.", ctx)
        path.append(words.pop(0))
        command = sub
        if isinstance(command, TyperGroup):
            ctx = command.make_context(path[-1], [], parent=ctx, resilient_parsing=True)
    # The context shows the usage; the words go through the parser alone, which raises the
    # CLI's usage errors (a resilient context would swallow them) but runs no callback.
    leaf = command.make_context(path[-1], [], parent=ctx, resilient_parsing=True)
    options = _options(command)
    if resilient and words and (option := options.get(words[-1])) and not option.is_flag:
        words = words[:-1]  # its value is still to come
    strict = Context(command, parent=ctx, info_name=path[-1])
    values, left, _ = command.make_parser(strict).parse_args(words)
    if left:
        raise UsageError(f"Got unexpected extra argument ({left[0]})", leaf)
    if not resilient:
        for param in command.params:
            if param.required and values.get(param.name or "") is None:
                if isinstance(param, TyperOption):
                    missing = f"option {param.opts[0]}"
                else:
                    missing = f"argument {param.human_readable_name.upper()}"
                raise UsageError(f"Missing {missing}.", leaf)
    return Resolved(tuple(path), command, leaf, values)


def _visible_commands(group: TyperGroup, ctx: Context) -> list[str]:
    return [
        name
        for name in group.list_commands(ctx)
        if (sub := group.get_command(ctx, name)) is not None and not sub.hidden
    ]


def _options(command: Command) -> dict[str, TyperOption]:
    """Every option string of ``command`` (--yes, -y, --no-copy) -> its option, hidden ones
    included: a hidden option is never offered, but its value is still skipped."""
    return {
        opt: param
        for param in command.params
        if isinstance(param, TyperOption)
        for opt in (*param.opts, *param.secondary_opts)
    }


def settings() -> list[str]:
    """The settings `config get|set|unset` take: every one but the keys."""
    secrets = set(config.secret_names())
    return sorted(name for name in config.env_names() if name not in secrets)


def secrets() -> list[str]:
    return sorted(config.secret_names())


def profiles() -> list[str]:
    """The built-in profiles and the user's own with a valid name."""
    names = set(prompt_builder.PROFILES)
    names.update(
        p.name
        for p in prompt_builder.user_profiles()
        if p.status not in (prompt_builder.INVALID_NAME, prompt_builder.MISSING)
    )
    return sorted(names)


def _option_values(option: TyperOption, opt: str) -> list[str]:
    if isinstance(option.type, TyperChoice):
        return sorted(str(c) for c in option.type.choices)
    if opt == PROFILE_OPTION:
        return profiles()
    return []


def _argument_values(path: tuple[str, ...], argument: TyperArgument, given: list[str]) -> list[str]:
    if isinstance(argument.type, TyperChoice):
        return sorted(str(c) for c in argument.type.choices)
    if argument.name == ARGUMENT and path in SETTING_ARGUMENTS:
        return settings()
    if argument.name == ARGUMENT and path in SECRET_ARGUMENTS:
        return secrets()
    if path == ("config", "set") and argument.name == VALUE_ARGUMENT:
        return profiles() if given[:1] and given[0] in PROFILE_SETTINGS else []
    return []


def words_for(prefix_words: Sequence[str]) -> list[str]:
    """Every word that may come after ``prefix_words``, sorted: a command of the group they
    name, or the command's options (hidden ones never) and the values of its next argument
    or of the option just typed. Empty when they do not resolve."""
    try:
        found = resolve(prefix_words, resilient=True, partial=True)
    except UsageError:
        return []
    command, ctx = found.command, found.context
    if isinstance(command, TyperGroup):
        if prefix_words[len(found.path) :]:
            return []  # past the group's own options: nothing else goes there
        names = _visible_commands(command, ctx)
        return sorted({*names, *_long_options(command)})
    options = _options(command)
    rest = list(prefix_words[len(found.path) :])
    given: list[str] = []
    used: set[str] = set()
    expecting: tuple[TyperOption, str] | None = None
    for word in rest:
        if expecting is not None:
            expecting = None
        elif word.startswith("-") and word.split("=", 1)[0] in options:
            option = options[word.split("=", 1)[0]]
            used.update((*option.opts, *option.secondary_opts))
            if not option.is_flag and "=" not in word:
                expecting = (option, word)
        else:
            given.append(word)
    if expecting is not None:
        return _option_values(*expecting)
    arguments = [p for p in command.params if isinstance(p, TyperArgument)]
    values: list[str] = []
    if len(given) < len(arguments):
        values = _argument_values(found.path, arguments[len(given)], given)
    return sorted({*values, *(o for o in _long_options(command) if o not in used)})


def _long_options(command: Command) -> list[str]:
    return [
        opt
        for param in command.params
        if isinstance(param, TyperOption) and not param.hidden
        for opt in (*param.opts, *param.secondary_opts)
        if opt.startswith("--")
    ]


def split(line: str) -> list[str]:
    """The line's words as a POSIX shell splits them, without a leading `promptmend`;
    raises ValueError on an open quote."""
    words = shlex.split(line)
    if words and words[0] in PROGRAMS:
        words = words[1:]
    return words


def holds_a_key(line: str) -> bool:
    """A line with a word (or as a whole) the gate would block as a credential."""
    try:
        words = shlex.split(line)
    except ValueError:
        words = line.split()
    return any(common.looks_like_a_key(w) for w in [*words, line] if w.strip())


def shown_arg(value: str) -> str:
    """A value as a command line may show it: never one that looks like a credential."""
    return teach.WITHHELD if common.looks_like_a_key(value) else value


def shown(argv: Sequence[str]) -> str:
    """``argv`` as `promptmend …` for the screen: a key-like word withheld, and the value of
    a private setting (`config set PROMPT_PERSONA …`) too."""
    words = [shown_arg(word) for word in argv]
    if tuple(words[:2]) == ("config", "set") and len(words) > 3 and words[2] in common.PRIVATE:
        words[3] = teach.WITHHELD
    return teach.equivalent(*words)


# --- What Enter does --------------------------------------------------------------------------

RUN = "run"  # in a child process, its output on Home
DIALOG = "dialog"  # the interface's own dialog for it, which asks before any change
TERMINAL = "terminal"  # it prompts as it goes: quit and run it in a terminal
REFUSE = "refuse"  # never from here


@dataclass(frozen=True)
class Rule:
    kind: str
    # --dry-run makes it a preview that changes nothing: then it runs.
    dry_run: bool = False
    why: str = ""


# Every visible command of the CLI (tests/test_tui_console.py walks the tree), by its path.
POLICY: dict[tuple[str, ...], Rule] = {
    ("improve",): Rule(
        REFUSE,
        why="the Espanso triggers run it, and it reads the clipboard; "
        "to try a rewrite, use the Try tab (7)",
    ),
    ("persona",): Rule(REFUSE, why="the -p- trigger runs it"),
    ("ui",): Rule(REFUSE, why="this is the interface"),
    ("setup",): Rule(TERMINAL),
    ("shell",): Rule(TERMINAL),
    ("doctor",): Rule(RUN),
    ("stats",): Rule(RUN),
    ("espanso", "status"): Rule(RUN),
    ("espanso", "deploy"): Rule(DIALOG, dry_run=True),
    ("espanso", "detach"): Rule(DIALOG),
    ("config", "show"): Rule(RUN),
    ("config", "get"): Rule(RUN),
    ("config", "set"): Rule(RUN),
    ("config", "unset"): Rule(RUN),
    ("config", "validate"): Rule(RUN),
    ("config", "migrate"): Rule(DIALOG, dry_run=True),
    ("config", "rollback"): Rule(TERMINAL, dry_run=True),
    ("config", "retire"): Rule(TERMINAL, dry_run=True),
    ("secrets", "set"): Rule(DIALOG),
    ("secrets", "status"): Rule(RUN),
    ("secrets", "remove"): Rule(DIALOG),
    ("profiles", "list"): Rule(RUN),
    ("profiles", "migrate"): Rule(TERMINAL, dry_run=True),
    ("history", "export"): Rule(RUN),
    ("history", "prune"): Rule(DIALOG),
    ("history", "reset"): Rule(DIALOG),
}
DRY_RUN = "--dry-run"
VERSION = "--version"
# doctor in a child never reads the clipboard unless asked to.
CLIPBOARD = ("--clipboard", "--no-clipboard")
# What a dialog decides itself (it always asks): ignored when typed.
DIALOG_DECIDES = ("--yes", "-y", "--on-conflict", "--no-restart", "--preview-token")
# What a dialog cannot do (another folder or launcher, a copy from a checkout): a terminal can.
TERMINAL_ONLY = ("--espanso-dir", "--launcher", "--from")
SECRET_SET = ("secrets", "set")
CONFIG_SET = ("config", "set")
CONFIG_NAMED = (("config", "get"), CONFIG_SET, ("config", "unset"))


@dataclass(frozen=True)
class Decision:
    """What Enter does with a line: ``argv`` is what runs (RUN) or what was typed."""

    kind: str
    argv: tuple[str, ...]
    path: tuple[str, ...] = ()
    values: Mapping[str, Any] = field(default_factory=dict)
    message: str = ""
    ignored: tuple[str, ...] = ()


def _option(word: str) -> str:
    return word.split("=", 1)[0]


def _terminal_only(found: Resolved, argv: Sequence[str]) -> bool:
    """A TERMINAL_ONLY option given: parsed with a value, or typed at all (as another
    option's value too: never less strict than the parser)."""
    names = {
        param.name
        for param in found.command.params
        if isinstance(param, TyperOption) and set(param.opts) & set(TERMINAL_ONLY)
    }
    parsed = any(found.values.get(name or "") is not None for name in names)
    return parsed or any(_option(w) in TERMINAL_ONLY for w in argv)


def decide(found: Resolved, words: Sequence[str]) -> Decision:
    """What Enter does with ``words``, which ``found`` resolved: --help (and a bare
    --version) always runs; else the command's POLICY rule. A leaf's --help and --dry-run
    count only as the parser read them, never a word another option took as its value
    (`improve --profile --help`)."""
    argv = tuple(words)
    if isinstance(found.command, TyperGroup):
        # A group's words are its own flags only (resolve() refuses any other): no option
        # there takes a value, so a word is what it says.
        helps = set(found.command.get_help_option_names(found.context))
        if helps & set(argv) or argv == (VERSION,):
            return Decision(RUN, argv, found.path)
        name = " ".join(found.path) or teach.PROGRAM
        return Decision(REFUSE, argv, found.path, message=f"{name} needs a command.")
    if found.values.get("help") is True:
        return Decision(RUN, argv, found.path)
    rule = POLICY[found.path]
    terminal = f"Quit and run it in a terminal: $ {shown(argv)}"
    if rule.kind != RUN and _terminal_only(found, argv):
        return Decision(TERMINAL, argv, found.path, message=terminal)
    if rule.dry_run and found.values.get("dry_run") is True:
        return Decision(RUN, argv, found.path)
    if rule.kind == RUN:
        if found.path == ("doctor",) and not set(CLIPBOARD) & set(argv):
            argv = (*argv, "--no-clipboard")
        return Decision(RUN, argv, found.path)
    if rule.kind == DIALOG:
        ignored = tuple(w for w in argv if _option(w) in DIALOG_DECIDES)
        return Decision(DIALOG, argv, found.path, found.values, ignored=ignored)
    if rule.kind == TERMINAL:
        return Decision(TERMINAL, argv, found.path, message=terminal)
    return Decision(REFUSE, argv, found.path, message=f"Not from here: {rule.why}.")


def refused_secret(words: Sequence[str]) -> bool:
    """`secrets set` with more than a key's name: a value typed after it, --stdin (there is
    no stdin here) or a name that is no key's (perhaps the key itself). Also `config
    set|get|unset` with a key's name and a word after it, or a value that looks like a key:
    the command refuses it, but only once the value is in its argv. Never shown or kept;
    --help adds no exception (`secrets set NAME --help VALUE` is refused too)."""
    if tuple(words[:2]) in CONFIG_NAMED:
        rest = words[2:]
        names = [w for w in rest if not w.startswith("-")]
        return bool(
            (len(names) > 1 and names[0] in config.secret_names())
            or (tuple(words[:2]) == CONFIG_SET and any(common.looks_like_a_key(w) for w in rest))
        )
    if tuple(words[:2]) != SECRET_SET:
        return False
    rest = words[2:]
    names = [w for w in rest if not w.startswith("-")]
    return (
        len(names) > 1
        or "--stdin" in rest
        or any(common.looks_like_a_key(w) for w in rest)
        or bool(names and names[0] not in config.secret_names())
    )


# --- Running a command ------------------------------------------------------------------------

TIMEOUT = 120
OUTPUT_CAP = 200_000
CUT = f"[output cut at {OUTPUT_CAP // 1000} KB]"


@dataclass(frozen=True)
class Ran:
    """A child's exit code (None: it timed out) and what it printed, stdout and stderr."""

    exit_code: int | None
    output: str


Runner = Callable[[Sequence[str]], Ran]


def _capped(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    output = output or ""
    if len(output) > OUTPUT_CAP:
        return f"{output[:OUTPUT_CAP]}\n{CUT}"
    return output


def runnable(argv: Sequence[str]) -> bool:
    """The last check before a child starts: a fresh parse of exactly ``argv`` decides RUN
    for exactly ``argv``. A line that fails to parse, or that decide() would send to a
    dialog, a terminal or a refusal, never runs."""
    words = list(argv)
    try:
        found = resolve(words)
    except UsageError:
        lenient = loose(words)
        if lenient is None:
            return False
        found = lenient
    decision = decide(found, words)
    return decision.kind == RUN and decision.argv == tuple(words)


def run(argv: Sequence[str]) -> Ran:
    """``promptmend argv`` in a child process, as in a terminal but without one: no stdin,
    no colour, stderr with stdout, at most TIMEOUT seconds. Never a shell; ``-P``, so a
    module in the working directory never runs."""
    import os
    import subprocess

    from .entry import self_command

    if not runnable(argv):
        raise ValueError(f"refused to run {shown(argv)}: the command line does not run it")
    try:
        done = subprocess.run(  # noqa: S603 - our own interpreter and module, an argv list
            [*self_command(), *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"},
            timeout=TIMEOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return Ran(None, _capped(exc.output))
    return Ran(done.returncode, _capped(done.stdout))


@dataclass(frozen=True)
class Transcript:
    """A run as Home shows it: the command, each output line (redacted), the exit line."""

    command: str
    lines: tuple[str, ...]
    status: str
    error: bool

    @property
    def summary(self) -> str:
        """The session log's line: the first line printed, else the exit line."""
        return next((line.strip() for line in self.lines if line.strip()), self.status)


def transcript(argv: Sequence[str], ran: Ran) -> Transcript:
    lines = tuple(redact_words(line) for line in ran.output.splitlines())
    timeout = f"timeout after {TIMEOUT} s"
    status = timeout if ran.exit_code is None else f"exit {ran.exit_code}"
    return Transcript(shown(argv), lines, status, ran.exit_code != 0)


def suggest(value: str) -> str | None:
    """``value`` with its last word completed to the first candidate, in order; None after
    a space, inside an open quote or with nothing to add."""
    if not value.strip() or value[-1].isspace():
        return None
    try:
        words = split(value)
    except ValueError:
        return None
    if not words:
        return None
    partial = words[-1]
    for word in words_for(words[:-1]):
        if word.startswith(partial) and word != partial:
            return value + word[len(partial) :]
    return None


# How many recipes the empty line's help names (Home's session log lists them all).


HELP_RECIPES = 2


def _recipes() -> Text:
    text = Text("Tab or Right arrow completes; Enter runs it. Try ")
    shown = (f"$ {teach.equivalent(*r.argv)}" for r in teach.RECIPES[:HELP_RECIPES])
    text.append(" · ".join(shown), style="dim")
    return text


def _withheld() -> Text:
    return Text(f"{teach.WITHHELD}: this line looks like it holds a key; nothing of it is shown.")


def _command_help(found: Resolved) -> Text:
    text = Text(no_wrap=False)
    text.append(found.command.get_usage(found.context), style="bold")
    short = found.command.get_short_help_str(limit=120)
    if short:
        text.append(f"\n{short}")
    if isinstance(found.command, TyperGroup):
        names = _visible_commands(found.command, found.context)
        text.append("\nCommands: " + ", ".join(names), style="dim")
    return text


def error_text(message: str) -> Text:
    return Text(f"error: {redact_words(message)}")


def describe(line: str, *, typing: bool = False) -> Text:
    """The help line for ``line``: recipes when empty, else the usage and short help of the
    command it names, or the usage error the CLI would print (redacted). ``typing`` (the line
    is still being typed) explains a half-typed last word by the command before it and the
    words it may become, instead of an error."""
    if holds_a_key(line):
        return _withheld()
    if not line.strip():
        return _recipes()
    try:
        words = split(line)
    except ValueError as exc:
        return error_text(str(exc))
    try:
        return _command_help(resolve(words, resilient=True, partial=True))
    except UsageError as exc:
        error = exc
    if typing and words and not line[-1].isspace():
        matches = [w for w in words_for(words[:-1]) if w.startswith(words[-1])]
        if matches:
            try:
                text = _command_help(resolve(words[:-1], resilient=True, partial=True))
            except UsageError:  # pragma: no cover - words_for() resolved it already
                return error_text(error.format_message())
            text.append("\nMatches: " + ", ".join(matches), style="dim")
            return text
    return error_text(error.format_message())


def loose(words: Sequence[str]) -> Resolved | None:
    """A line the CLI would refuse that still goes somewhere: --help (`config set --help`),
    or a dialog that asks for what is missing (`secrets remove`, `history prune
    --older-than`)."""
    try:
        found = resolve(words, resilient=True)
    except UsageError:
        return None
    if "--help" in words:
        return found
    rule = POLICY.get(found.path)
    return found if rule is not None and rule.kind == DIALOG else None
