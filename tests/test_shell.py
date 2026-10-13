"""`promptmend shell` (#183): the REPL over the console's completion, live help and refusals.
No test starts a real child (conftest refuses shell.run_here; the runner tests fake Popen) or
reads the real terminal (prompt_toolkit gets a pipe input and a dummy output)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

from promptmend import console
from promptmend.cli import app
from promptmend.commands import common, shell
from promptmend.tui import teach

runner = CliRunner()
# Built at runtime, so no key-shaped literal lands in the repo (gitleaks).
KEY = "sk-or-v1-" + "ab12" * 16
# The real runner, kept before conftest replaces shell.run_here with a refusal in every test.
REAL_RUN_HERE = shell.run_here


class FakeRunner:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(tuple(argv))
        return self.code


def _lines(*lines: str | type[BaseException]) -> Any:
    """A read() that returns each line in turn, or raises each exception class given."""
    feed: Iterator[str | type[BaseException]] = iter(lines)

    def read() -> str:
        item = next(feed)
        if isinstance(item, str):
            return item
        raise item

    return read


def _repl(*lines: str | type[BaseException], code: int = 0) -> tuple[int, FakeRunner, list[str]]:
    fake = FakeRunner(code)
    out: list[str] = []
    result = shell.repl(_lines(*lines), runner=fake, echo=out.append)
    return result, fake, out


# --- The command ----------------------------------------------------------------------------


def test_shell_needs_a_terminal() -> None:
    result = runner.invoke(app, ["shell"])
    assert result.exit_code == common.NEEDS_TERMINAL
    assert "needs a terminal" in result.output


def test_shell_runs_a_session_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    fake = FakeRunner()
    monkeypatch.setattr(shell, "run_here", fake)
    with create_pipe_input() as pipe:
        pipe.send_text("promptmend config show\rimprove\rquit\r")
        made = shell.Editor(input=pipe, output=DummyOutput())
        monkeypatch.setattr(shell, "Editor", lambda: made)
        result = runner.invoke(app, ["shell"])
    assert result.exit_code == 0, result.output
    assert fake.calls == [("config", "show")]
    assert "Not from the shell: the Espanso triggers run it" in result.output


def test_shell_is_listed_and_documented() -> None:
    result = runner.invoke(app, ["--help"])
    assert "shell" in result.output
    assert console.POLICY[("shell",)].kind == console.TERMINAL


# --- What Enter does --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "argv"),
    [
        ("doctor", ("doctor",)),  # a real terminal: never forced to --no-clipboard
        ("promptmend config show", ("config", "show")),
        ("prompt-workflow stats --by model", ("stats", "--by", "model")),
        ("espanso deploy", ("espanso", "deploy")),  # asks here, as on any terminal
        ("secrets set OPENROUTER_API_KEY", ("secrets", "set", "OPENROUTER_API_KEY")),
        ("config set --help", ("config", "set", "--help")),
        ("improve --help", ("improve", "--help")),
        ("config --help", ("config", "--help")),
        ("--version", ("--version",)),
        ("ui", ("ui",)),
        ("setup", ("setup",)),
        ("espanso deploy --espanso-dir e", ("espanso", "deploy", "--espanso-dir", "e")),
        ("history reset", ("history", "reset")),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_lines_that_run(line: str, argv: tuple[str, ...]) -> None:
    assert shell.plan(line) == shell.Plan(shell.RUN, argv)
    assert shell.runs_here(argv)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("improve", "Not from the shell: the Espanso triggers run it"),
        ("improve --profile --help", "Not from the shell: the Espanso triggers run it"),
        ("improve --source argument --text hi", "use the Try tab (7) of `promptmend ui`"),
        ("persona", "Not from the shell: the -p- trigger runs it"),
        ("shell", "Not from the shell: this is the shell"),
        (f"secrets set OPENROUTER_API_KEY {KEY}", "holds a key"),
        (f"config set PROMPT_PROFILE {KEY}", "holds a key"),
        ("secrets set OPENROUTER_API_KEY value", "Type only the key's name"),
        ("secrets set OPENROUTER_API_KEY --stdin", "Type only the key's name"),
        ("secrets set NOT_A_KEY_NAME", "Type only the key's name"),
        ('secrets set OPENROUTER_API_KEY "hunter2', "Type only the key's name"),
        ("config set OPENROUTER_API_KEY hunter2", "Type only the key's name"),
        ("improve --help --profile", "Not from the shell: the Espanso triggers run it"),
    ],
    ids=["improve", "help-as-value", "argument", "persona", "shell", "key", "key-setting",
         "value", "stdin", "not-a-name", "open-quote", "config-key", "help-missing-value"],
)  # fmt: skip
def test_lines_that_are_refused(line: str, message: str) -> None:
    decided = shell.plan(line)
    assert decided.kind == shell.REFUSE
    assert message in decided.message
    assert KEY not in decided.message
    assert not shell.runs_here(line.split())


# Leaves whose bare line misses a required argument: a usage error, nothing runs.
NEEDS_ARGUMENTS = {
    ("config", "get"),
    ("config", "set"),
    ("config", "unset"),
    ("config", "retire"),
    ("secrets", "set"),
    ("secrets", "remove"),
}


@pytest.mark.parametrize("path", list(console.POLICY), ids=" ".join)
def test_the_shell_follows_the_interface_policy(path: tuple[str, ...]) -> None:
    # One table (#239): what the interface refuses the shell refuses, except `ui`, and the
    # shell itself; a dialog or terminal command runs here as typed.
    line = " ".join(path)
    refuses = console.POLICY[path].kind == console.REFUSE
    if path == ("shell",) or (path != ("ui",) and refuses):
        assert shell.plan(line).kind == shell.REFUSE
        assert shell.plan(f"{line} --help") == shell.Plan(shell.RUN, (*path, "--help"))
    elif path in NEEDS_ARGUMENTS:
        assert shell.plan(line).kind == shell.ERROR
    else:
        assert shell.plan(line) == shell.Plan(shell.RUN, path)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("espanso deplo", "error: No such command 'deplo'."),
        ("config", "error: config needs a command."),
        ("config set PROMPT_HISTORY", "error: Missing argument VALUE."),
        ('config set "x', "error: No closing quotation"),
        ("doctor --no-such", "error: No such option: --no-such"),
        ("config --no-such", "error: No such option: --no-such"),
        ("persona --help --no-such", "error: No such option: --no-such"),
    ],
    ids=["command", "group", "argument", "quote", "option", "group-option", "trigger-help"],
)
def test_usage_errors_run_nothing(line: str, message: str) -> None:
    decided = shell.plan(line)
    assert decided.kind == shell.ERROR
    assert decided.message == message


def test_a_group_flag_that_is_not_help_is_a_usage_error() -> None:
    # resolve() takes a group's own flags; decide() runs only --help or a bare --version.
    decided = shell.plan("--version --version")
    assert decided == shell.Plan(shell.ERROR, message="error: promptmend needs a command.")


@pytest.mark.parametrize("line", ["", "   ", "help", "?", "promptmend"])
def test_help_lines(line: str) -> None:
    assert shell.plan(line).kind == shell.HELP


@pytest.mark.parametrize("line", ["exit", "quit", " exit "])
def test_exit_lines(line: str) -> None:
    assert shell.plan(line).kind == shell.EXIT


def test_runs_here_reparses_exactly() -> None:
    assert not shell.runs_here(("improve",))
    assert not shell.runs_here(("improve", "--profile", "--help"))
    assert not shell.runs_here(("no-such",))
    assert not shell.runs_here(("exit",))
    assert shell.runs_here(("config", "set", "PROMPT_PERSONA", "it's me"))


# --- The loop -------------------------------------------------------------------------------


def test_repl_runs_lines_and_reports_a_failed_exit() -> None:
    code, fake, out = _repl("doctor", "stats", "exit", code=4)
    assert code == 0
    assert fake.calls == [("doctor",), ("stats",)]
    assert out[1:] == ["exit 4", "exit 4"]


def test_repl_prints_nothing_after_a_clean_exit() -> None:
    _, fake, out = _repl("doctor", "quit")
    assert fake.calls == [("doctor",)]
    assert out == [shell.recipes()]


def test_repl_ends_on_eof_and_survives_ctrl_c() -> None:
    code, fake, out = _repl(KeyboardInterrupt, "", "help", EOFError)
    assert code == 0
    assert fake.calls == []
    assert out == [shell.recipes()] * 3


def test_repl_refuses_and_never_echoes_a_key() -> None:
    _, fake, out = _repl(f"secrets set OPENROUTER_API_KEY {KEY}", "improve", "config", "exit")
    assert fake.calls == []
    assert not [line for line in out if KEY in line]
    assert out[1] == shell.WITHHELD_NOTE
    assert out[2].startswith("Not from the shell")
    assert out[3] == "error: config needs a command."


def test_recipes_name_every_recipe_and_how_to_leave() -> None:
    text = shell.recipes()
    for recipe in teach.RECIPES:
        assert teach.equivalent(*recipe.argv) in text
    assert shell.LEAVE in text


# --- The runner -----------------------------------------------------------------------------


class FakePopen:
    instances: ClassVar[list[FakePopen]] = []

    def __init__(self, args: Sequence[str], **kwargs: Any) -> None:
        self.args = list(args)
        self.kwargs = kwargs
        self.waits = 0
        FakePopen.instances.append(self)

    def wait(self) -> int:
        self.waits += 1
        if self.waits == 1:
            raise KeyboardInterrupt  # Ctrl-C reached the child; the shell keeps waiting
        return 130


def test_run_here_starts_promptmend_on_this_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    FakePopen.instances = []
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    assert REAL_RUN_HERE(("doctor",)) == 130
    (child,) = FakePopen.instances
    assert child.args == [sys.executable, "-P", "-m", "promptmend.cli", "doctor"]
    assert child.kwargs == {}  # inherited stdin, stdout and stderr; no shell, no timeout
    assert child.waits == 2


def test_run_here_refuses_what_the_shell_does_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    FakePopen.instances = []
    for argv in (("improve",), ("improve", "--profile", "--help"), ("secrets", "set", KEY)):
        with pytest.raises(ValueError, match="refused"):
            REAL_RUN_HERE(argv)
    assert FakePopen.instances == []


def test_conftest_refuses_a_real_child() -> None:
    with pytest.raises(AssertionError, match="started a real promptmend"):
        shell.run_here(("--version",))


# --- The line editor ----------------------------------------------------------------------------


def test_completions() -> None:
    assert ("espanso", -3) in shell.completions("esp")
    assert shell.completions("espanso st") == [("status", -2)]
    assert ("status", 0) in shell.completions("espanso ")
    assert ("doctor", 0) in shell.completions("promptmend ")
    assert ("doctor", 0) in shell.completions("")
    assert shell.completions("promptmend") == []
    assert shell.completions('config set "PROMPT_') == []
    assert shell.completions(f"secrets set OPENROUTER_API_KEY {KEY[:-2]}") == []
    names = {w for w, _ in shell.completions("secrets set ")}
    assert "OPENROUTER_API_KEY" in names


def test_toolbar() -> None:
    assert shell.toolbar("espanso status").startswith("Usage: promptmend espanso status")
    assert "Matches: status" in shell.toolbar("espanso st")
    assert shell.toolbar(f"secrets set OPENROUTER_API_KEY {KEY}").startswith(teach.WITHHELD)
    assert KEY not in shell.toolbar(f"config set X {KEY}")
    assert shell.toolbar("espanso deplo ").startswith("error: No such command")


def test_session_wires_completion_suggestion_toolbar_and_history() -> None:
    with create_pipe_input() as pipe:
        made = shell.Editor(input=pipe, output=DummyOutput()).prompt
    assert made.completer is not None
    words = made.completer.get_completions(Document("espanso st"), CompleteEvent())
    assert [c.text for c in words] == ["status"]
    assert made.auto_suggest is not None
    found = made.auto_suggest.get_suggestion(made.default_buffer, Document("espanso st"))
    assert found is not None
    assert found.text == "atus"
    assert made.auto_suggest.get_suggestion(made.default_buffer, Document("doctor ")) is None
    key_line = Document(f"secrets set OPENROUTER_API_KEY {KEY[:-1]}")
    assert made.auto_suggest.get_suggestion(made.default_buffer, key_line) is None
    toolbar = made.bottom_toolbar
    assert callable(toolbar)
    assert toolbar() == console.describe("", typing=True).plain  # the empty line's recipes


def test_history_keeps_no_key_like_line() -> None:
    lines = [
        "doctor",
        "doctor",
        f"secrets set OPENROUTER_API_KEY {KEY}",
        "secrets set OPENROUTER_API_KEY value",
        'config set "x',
        "improve",
        "   ",
        "exit",
    ]
    with create_pipe_input() as pipe:
        pipe.send_text("".join(f"{line}\r" for line in lines))
        editor = shell.Editor(input=pipe, output=DummyOutput())
        shell.repl(editor.read, runner=FakeRunner(), echo=lambda _: None)
    assert list(editor.prompt.history.get_strings()) == ["doctor"]
    assert shell.kept("doctor")
    for line in (
        f"x {KEY}",
        "secrets set OPENROUTER_API_KEY value",
        'secrets set OPENROUTER_API_KEY "hunter2',  # does not parse
        "secret set OPENROUTER_API_KEY hunter2",  # a typo: an unknown command
        "config set OPENROUTER_API_KEY hunter2",
        "improve",
        "exit",
    ):
        assert not shell.kept(line), line


def _accepted(text: str) -> tuple[list[str], list[str], FakeRunner, shell.Editor]:
    """Feed ``text`` to an editor and return what each accepted line showed on screen, what
    the shell printed, the runner and the editor."""
    with create_pipe_input() as pipe:
        pipe.send_text(text)
        pipe.close()  # then EOF ends the loop, whatever the keys did
        editor = shell.Editor(input=pipe, output=DummyOutput())
        shown: list[str] = []
        original = editor.prompt.prompt

        def read() -> str:
            line = original()
            shown.append(editor.prompt.default_buffer.text or line)
            return line

        editor.prompt.prompt = read  # type: ignore[method-assign, assignment]
        out: list[str] = []
        fake = FakeRunner()
        shell.repl(editor.read, runner=fake, echo=out.append)
    return shown, out, fake, editor


def test_a_refused_key_line_is_replaced_on_screen_before_it_is_accepted() -> None:
    lines = [f"secrets set OPENROUTER_API_KEY {KEY}", "secrets set OPENROUTER_API_KEY hunter2"]
    shown, out, fake, editor = _accepted("".join(f"{x}\r" for x in lines) + "doctor\rexit\r")
    # The accepted (rendered) line no longer holds the value...
    assert shown[:2] == [teach.WITHHELD, teach.WITHHELD]
    # ...the shell still knew what it refused, and ran only doctor.
    assert out[1] == shell.WITHHELD_NOTE
    assert "Type only the key's name" in out[2]
    assert fake.calls == [("doctor",)]
    assert list(editor.prompt.history.get_strings()) == ["doctor"]


# Every way to accept a line: Enter, Meta-Enter (Esc then Enter), Ctrl-O (accept and get the
# next history line), and Enter from a Ctrl-R search. (No vi mode: the shell is emacs only.)
ACCEPTS = {"enter": "\r", "meta-enter": "\x1b\r", "ctrl-o": "\x0f", "search": "\x12\r"}


@pytest.mark.parametrize("accept", list(ACCEPTS), ids=list(ACCEPTS))
def test_every_accept_key_withholds_a_key_line(accept: str) -> None:
    # A key after it, so the input parser hands over the accept key too.
    shown, out, fake, _ = _accepted(f"config get {KEY}{ACCEPTS[accept]}exit\r")
    assert shown[0] == teach.WITHHELD
    assert out[1] == shell.WITHHELD_NOTE
    assert fake.calls == []


# --- Off the trigger path ---------------------------------------------------------------------


def test_console_and_shell_load_neither_textual_nor_prompt_toolkit() -> None:
    probe = (
        "import json, sys\n"
        "import promptmend.console, promptmend.commands.shell\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-P", "-c", probe], capture_output=True, check=True, timeout=60
    )
    loaded = json.loads(proc.stdout)
    for module in ("textual", "prompt_toolkit"):
        assert not [m for m in loaded if m == module or m.startswith(module + ".")], module


# --- The user's PROMPT_EXTRA_PATTERNS -------------------------------------------------------

# A word only the user's pattern flags, no built-in one.
CUSTOM = "zz-falcon-1234"


def test_looks_like_a_key_uses_the_users_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    import re

    from promptmend import redaction

    assert not common.looks_like_a_key(CUSTOM)
    monkeypatch.setattr(redaction, "_user_patterns", (re.compile("zz-falcon"),))
    assert common.looks_like_a_key(CUSTOM)
    assert console.holds_a_key(f"config get {CUSTOM}")
    # Showing PROMPT_EXTRA_PATTERNS itself never hides it behind its own pattern.
    assert not common.looks_like_a_key("zz-falcon", user_patterns=False)


def test_shell_loads_the_users_patterns_and_withholds_their_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPT_EXTRA_PATTERNS", "zz-falcon")
    monkeypatch.setattr(common, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    fake = FakeRunner()
    monkeypatch.setattr(shell, "run_here", fake)
    with create_pipe_input() as pipe:
        pipe.send_text(f"config get {CUSTOM}\rdoctor\rexit\r")
        made = shell.Editor(input=pipe, output=DummyOutput())
        monkeypatch.setattr(shell, "Editor", lambda: made)
        result = runner.invoke(app, ["shell"])
    assert result.exit_code == 0, result.output
    assert fake.calls == [("doctor",)]
    assert shell.WITHHELD_NOTE in result.output
    assert CUSTOM not in result.output
    assert list(made.prompt.history.get_strings()) == ["doctor"]


def test_a_broken_config_does_not_stop_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)

    def broken() -> Any:
        raise OSError("unreadable")

    monkeypatch.setattr(common, "load_layers", broken)
    monkeypatch.setattr(shell, "run_here", FakeRunner())
    with create_pipe_input() as pipe:
        pipe.send_text("exit\r")
        made = shell.Editor(input=pipe, output=DummyOutput())
        monkeypatch.setattr(shell, "Editor", lambda: made)
        result = runner.invoke(app, ["shell"])
    assert result.exit_code == 0, result.output
    assert "unreadable" in result.output


def test_patterns_are_read_again_after_each_run() -> None:
    reloads: list[int] = []
    shell.repl(
        _lines("doctor", "stats", "improve", "exit"),
        runner=FakeRunner(),
        echo=lambda _: None,
        refresh=lambda: reloads.append(1),
    )
    assert len(reloads) == 2


def test_an_interrupted_child_leaves_the_exit_note_on_its_own_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subprocess, "Popen", FakePopen)  # Ctrl-C once, then exit 130
    assert REAL_RUN_HERE(("doctor",)) == 130
    assert capsys.readouterr().out == "\n"


def test_a_child_that_ends_by_itself_adds_no_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Quiet(FakePopen):
        def wait(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", Quiet)
    assert REAL_RUN_HERE(("doctor",)) == 0
    assert capsys.readouterr().out == ""
