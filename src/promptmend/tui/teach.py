"""What each action of the interface is in a terminal: every button's tooltip
names its headless command, every result shows the command it was (`$ promptmend …`),
and Home keeps this session's commands. A button with no command that does the same is in
NO_COMMAND, its tooltip saying what it does instead. No Textual here:
`tests/test_tui_teach.py` parses every command below against the CLI, so a renamed command or
option cannot leave them behind."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

PROGRAM = "promptmend"
# A placeholder for what the person picks in the dialog (<NAME>, <FILE>): shown as is.
PLACEHOLDER = re.compile(r"<[A-Z][A-Z_]*>")
WITHHELD = "<value withheld>"

# Button id -> the commands it runs, in order.
BUTTONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "home-setup": (("setup",),),
    # Home's button for the headline's problem: one of these three shows at a time.
    "home-deploy": (("espanso", "deploy"),),
    "home-set-key": (("secrets", "set", "<NAME>"),),
    "setup-save-keys": (("secrets", "set", "<NAME>"),),
    "setup-deploy": (("espanso", "deploy"),),
    "setup-run-test": (("setup",),),
    "set-profile": (("config", "set", "PROMPT_PROFILE", "<NAME>"),),
    "show-diff": (("espanso", "status", "--diff"),),
    "deploy": (("espanso", "deploy"),),
    "detach": (("espanso", "detach"),),
    "export": (("history", "export", "--format", "json", "-o", "<FILE>"),),
    "prune": (("history", "prune", "--older-than", "<DAYS>"),),
    "reset": (("history", "reset"),),
    "use-default": (("config", "unset", "<NAME>"),),  # the Settings pick and edit dialogs
    "try-run": (
        (
            "improve",
            "--provider",
            "<PROVIDER>",
            "--profile",
            "<NAME>",
            "--tier",
            "<TIER>",
            "--source",
            "argument",
            "--text",
            "<DRAFT>",
        ),
    ),
}
# What a part of a tab that is no button shows the way to (its title names the command).
HINTS: dict[str, tuple[str, ...]] = {
    "last-runs": ("history", "last", "-n", "<N>"),  # the History tab's Last runs table
}
# The Settings tab's list: each action of its keys -> the command it is in a terminal.
SETTING_ACTIONS: dict[str, tuple[str, ...]] = {
    "set": ("config", "set", "<NAME>", "<VALUE>"),  # Space toggles, Enter picks or edits
    "reset": ("config", "unset", "<NAME>"),  # u on a setting
    "set-key": ("secrets", "set", "<NAME>"),  # Enter on a key
    "remove-key": ("secrets", "remove", "<NAME>"),  # u on a key
}
# What a button's command does not cover, said in its tooltip.
NOTES = {
    "setup-run-test": "Its last step, the smoke test against a stub on 127.0.0.1.",
    "home-setup": "On a terminal it opens this same setup; --non-interactive asks nothing.",
    "home-deploy": "Opens the deploy preview first: nothing is written until you press Deploy.",
    "home-set-key": "Opens the Set key dialog for the key the headline names, typed hidden.",
    "export": "Or --format csv.",
    "try-run": "Dry run sends it to a stub on 127.0.0.1 instead: no provider is called and "
    "nothing is rewritten.",
}
# Buttons with no command that does the same: they move around the interface, or do what no
# single command does (each tooltip may name the command closest to it).
NO_COMMAND = {
    "home-reload": "Read the settings and run every check again, as r does;\n"
    "`promptmend doctor` prints the same checks.",
    "smoke": "Run improve against a stub on 127.0.0.1 with a placeholder key: no provider is\n"
    "called and nothing is saved. Setup's Test step runs it on your triggers' path.",
    "edit-profile": "Open the highlighted profile's file in $VISUAL or $EDITOR;\n"
    "`promptmend profiles list` shows where each profile is.",
    "home-copy": "Copy the latest command of this session, as Home's log shows it, to the\n"
    "terminal clipboard (OSC 52); the clipboard is never read.",
    "home-open": "Open the tab the headline names, as its digit does.",
    "setup-back": "",
    "setup-next-step": "",
    "setup-open": "",
    "setup-quit": "",
    "about-close": "",
}


def equivalent(*argv: str) -> str:
    """The command line for ``argv``, quoted for a POSIX shell; placeholders as they are."""
    return " ".join([PROGRAM, *_quoted(argv)])


def for_setting(action: str, name: str, value: str | None = None) -> str:
    """The command of a Settings list action on ``name``: <NAME> filled in, and <VALUE> too
    when ``value`` is given (WITHHELD for a value never shown)."""
    filled = {"<NAME>": name} | ({"<VALUE>": value} if value is not None else {})
    return equivalent(*(filled.get(word, word) for word in SETTING_ACTIONS[action]))


def _quoted(argv: tuple[str, ...]) -> list[str]:
    return [a if PLACEHOLDER.fullmatch(a) or a == WITHHELD else shlex.quote(a) for a in argv]


def tooltip(button_id: str) -> str | None:
    """A button's tooltip: the commands it runs in a terminal, and what they leave out."""
    if button_id in NO_COMMAND:
        return NO_COMMAND[button_id] or None
    commands = BUTTONS.get(button_id)
    if not commands:
        return None
    lines = ["In a terminal:", *(f"$ {equivalent(*argv)}" for argv in commands)]
    if button_id in NOTES:
        lines.append(NOTES[button_id])
    return "\n".join(lines)


@dataclass(frozen=True)
class Entry:
    """One action of this session: the command it was and the first line of its result."""

    command: str
    message: str
    error: bool = False

    @property
    def summary(self) -> str:
        return (self.message.splitlines() or [""])[0]
