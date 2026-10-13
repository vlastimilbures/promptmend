"""`ui`: the full-screen interface (#93), also what a bare `promptmend` opens on a
terminal (D-UI-1). Textual is imported here only once the interface opens, so --help, the
triggers and every other command never load it."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import typer

from . import common

if TYPE_CHECKING:
    from ..tui.setup_wizard import SetupOptions

app = typer.Typer(name="ui", add_completion=False)


def on_a_terminal() -> bool:
    """Both stdin and stdout are a terminal: a person is there to use the interface."""
    return common.stdin_is_tty() and common.stdout_is_tty()


def wants_intro() -> bool:
    """PROMPT_UI_INTRO (#112), read in repair mode: a broken settings file shows the intro,
    and the interface then reports the file."""
    return common.load_layers()[1].ui_intro


@app.command("ui", short_help="Open the full-screen interface to set up and manage (a terminal).")
def ui(
    no_intro: bool = typer.Option(
        False, "--no-intro", help="Open without the intro (or set PROMPT_UI_INTRO=false)."
    ),
) -> None:
    """Open the full-screen interface: status, settings and keys, profiles, triggers, usage
    history and diagnostics. It needs a terminal; every screen has a headless command (see
    --help), which scripts and screen readers can use instead."""
    if not on_a_terminal():
        common.fail(
            "the interface needs a terminal (stdin and stdout); use the headless commands "
            "instead (see --help)",
            common.NEEDS_TERMINAL,
        )
    from ..tui.app import ManageApp

    interface = ManageApp(intro=not no_intro and wants_intro(), first_run=True)
    interface.run()
    sys.exit(interface.return_code or 0)


def open_setup(options: SetupOptions) -> None:
    """`setup` on a terminal: the interface opened on its setup wizard, which quits when done
    (Open PromptMend stays in the interface)."""
    from ..tui.app import ManageApp

    interface = ManageApp(intro=False, setup=options)
    interface.run()
    sys.exit(interface.return_code or 0)
