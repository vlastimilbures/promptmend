"""What each action of the interface is in a terminal (#111, stage 1): every button's tooltip
names its headless command, every result shows the command it was (`$ promptmend …`),
and Home keeps this session's commands, or a few recipes before there are any. No Textual
here: `tests/test_tui_teach.py` parses every command below against the CLI, so a renamed
command or option cannot leave them behind."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

PROGRAM = "promptmend"
# A placeholder for what the person picks in the dialog (<NAME>, <FILE>): shown as is.
PLACEHOLDER = re.compile(r"<[A-Z][A-Z_]*>")
WITHHELD = "<value withheld>"

CHECKOUT = "<CHECKOUT>"

# Button id -> the commands it runs, in order.
BUTTONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "home-reload": (("doctor",),),
    "home-setup": (("setup",),),
    "setup-migrate-env": (("config", "migrate"),),
    "setup-save-keys": (("secrets", "set", "<NAME>"),),
    "setup-deploy": (("espanso", "deploy"),),
    "setup-run-test": (("setup",),),
    "migrate-env": (("config", "migrate"),),
    "smoke": (("setup",),),
    "set-profile": (("config", "set", "PROMPT_PROFILE", "<NAME>"),),
    "edit-profile": (("profiles", "list"),),
    "migrate-profiles": (("profiles", "migrate", "--checkout", CHECKOUT),),
    "show-diff": (("espanso", "status", "--diff"),),
    "deploy": (("espanso", "deploy"),),
    "detach": (("espanso", "detach"),),
    "export": (("history", "export", "--format", "json", "-o", "<FILE>"),),
    "prune": (("history", "prune", "--older-than", "<DAYS>"),),
    "reset": (("history", "reset"),),
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
# The Settings tab's list (#199): each action of its keys -> the command it is in a terminal.
SETTING_ACTIONS: dict[str, tuple[str, ...]] = {
    "set": ("config", "set", "<NAME>", "<VALUE>"),  # Space toggles, Enter picks or edits
    "reset": ("config", "unset", "<NAME>"),  # u on a setting
    "set-key": ("secrets", "set", "<NAME>"),  # Enter on a key
    "remove-key": ("secrets", "remove", "<NAME>"),  # u on a key
}
# What a button's command does not cover, said in its tooltip.
NOTES = {
    "smoke": "Its last step, the smoke test against a stub on 127.0.0.1.",
    "setup-run-test": "Its last step, the smoke test against a stub on 127.0.0.1.",
    "home-setup": "On a terminal it opens this same setup; --non-interactive asks nothing.",
    "edit-profile": "Shows where each profile is; open the file in your editor.",
    "export": "Or --format csv.",
    "try-run": "Local stub runs it against a stub on 127.0.0.1 instead; no provider is called.",
}
# Buttons with no command of their own: they move around the interface or measure it.
NO_COMMAND = {
    "home-copy": "Copy the latest command of this session, as Home's log shows it, to the\n"
    "terminal clipboard (OSC 52); the clipboard is never read.",
    "setup-back": "",
    "setup-next-step": "",
    "setup-open": "",
    "setup-quit": "",
    "about-close": "",
}


@dataclass(frozen=True)
class Recipe:
    argv: tuple[str, ...]
    what: str


# Home's empty session log: commands worth knowing, each one the interface also runs.
RECIPES = (
    Recipe(("doctor",), "every check, as on Diagnostics"),
    Recipe(("config", "show"), "each setting and where it comes from"),
    Recipe(("config", "set", "PROMPT_HISTORY", "false"), "switch the usage history off"),
    Recipe(("secrets", "set", "OPENROUTER_API_KEY"), "save a key (typed hidden)"),
    Recipe(("espanso", "status", "--diff"), "what a deploy would change"),
    Recipe(("stats", "--by", "model"), "usage by model"),
)


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
