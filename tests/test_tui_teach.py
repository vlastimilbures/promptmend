"""Every command the interface names (#111, stage 1) parses against the CLI: each button's
tooltip, the recipes on Home and the command a result shows. A renamed command, option or
argument fails here instead of teaching a command that no longer works."""

from __future__ import annotations

import pytest
import typer
from typer._click import Command, Context
from typer.core import TyperGroup

from promptmend import cli
from promptmend.tui import teach

# What a placeholder stands for, so the command can be parsed.
EXAMPLES = {
    "<NAME>": "PROMPT_PROFILE",
    "<VALUE>": "general",
    "<FILE>": "usage.json",
    "<DAYS>": "30",
    "<PROVIDER>": "openrouter",
    "<TIER>": "standard",
    "<DRAFT>": "a rough draft",
}


def parse(argv: tuple[str, ...]) -> None:
    """Walk ``argv`` down the Click tree and parse the leaf's arguments and options, as the
    CLI would before running it, but with no callback run; raises on an unknown command,
    option or argument, a word left over or a missing required one."""
    command: Command = typer.main.get_command(cli.app)
    words = [EXAMPLES.get(word, word) for word in argv]
    while isinstance(command, TyperGroup):
        assert words, f"{argv}: names a group, not a command"
        sub = command.get_command(Context(command), words.pop(0))
        assert sub is not None, f"{argv}: no such command"
        assert not sub.hidden, f"{argv}: a hidden command"
        command = sub
    values, left, _ = command.make_parser(Context(command)).parse_args(words)
    assert not left, f"{argv}: {left} left over"
    for param in command.params:
        assert not param.required or values.get(param.name or "") is not None, param.name


ALL = sorted(
    {argv for commands in teach.BUTTONS.values() for argv in commands}
    | {recipe.argv for recipe in teach.RECIPES}
    | set(teach.SETTING_ACTIONS.values())
)


# Each Settings list action (#199), filled in for every row it applies to, parses too.
def test_every_settings_list_command_parses() -> None:
    from promptmend.tui import settings_model

    for meta in settings_model.SETTINGS:
        if meta.kind == settings_model.KEY:
            commands = [teach.for_setting(a, meta.name) for a in ("set-key", "remove-key")]
        else:
            commands = [
                teach.for_setting("set", meta.name, "x"),
                teach.for_setting("set", meta.name, teach.WITHHELD),
                teach.for_setting("reset", meta.name),
            ]
        for command in commands:
            words = command.split()
            assert words[0] == teach.PROGRAM
            parse(tuple(" ".join(words[1:]).replace(teach.WITHHELD, "v").split()))
    assert teach.for_setting("set", "PROMPT_OUTPUT", "") == (
        "promptmend config set PROMPT_OUTPUT ''"
    )
    assert teach.for_setting("set", "PROMPT_OUTPUT") == (
        "promptmend config set PROMPT_OUTPUT <VALUE>"
    )


@pytest.mark.parametrize("argv", ALL, ids=" ".join)
def test_every_command_parses(argv: tuple[str, ...]) -> None:
    parse(argv)
    assert all(w in EXAMPLES for w in argv if teach.PLACEHOLDER.fullmatch(w))


def test_the_drift_check_catches_a_wrong_command() -> None:
    for wrong in [
        ("espanso", "deplo"),
        ("history", "prune", "--days", "3"),
        ("config",),
        ("secrets", "set"),
        ("doctor", "x"),
    ]:
        with pytest.raises(Exception):  # noqa: B017, PT011  an assert or a Click UsageError
            parse(wrong)


def test_equivalent_quotes_values_and_keeps_placeholders() -> None:
    assert teach.equivalent("config", "set", "<NAME>", "<VALUE>") == (
        "promptmend config set <NAME> <VALUE>"
    )
    assert teach.equivalent("history", "export", "-o", "my file.csv") == (
        "promptmend history export -o 'my file.csv'"
    )
    assert teach.equivalent("config", "set", "X", teach.WITHHELD).endswith("X <value withheld>")


def test_tooltips() -> None:
    assert teach.tooltip("deploy") == "In a terminal:\n$ promptmend espanso deploy"
    smoke = teach.tooltip("smoke") or ""
    assert smoke.startswith("In a terminal:\n$ promptmend setup\n")
    assert "smoke test" in smoke
    assert teach.tooltip("home-copy") == teach.NO_COMMAND["home-copy"]
    assert teach.tooltip("setup-back") is None
    assert teach.tooltip("no-such-button") is None


def test_entry_summary_is_the_first_line() -> None:
    assert teach.Entry("c", "one\ntwo").summary == "one"
    assert teach.Entry("c", "").summary == ""
