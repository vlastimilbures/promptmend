"""The console-script entry point (`promptmend`).

Espanso runs the triggers' calls as `type: script` vars (#18), and such a var fails on any
stderr output, even with exit code 0. So before the CLI and its dependencies are imported, a
trigger call (`improve`, `persona`) silences Python warnings and points file descriptor 2 at
the null device: a warning, a dependency's stray print or a native library's message would
otherwise turn the expansion into Espanso's generic error. The exit code is left alone, so a
crash still exits nonzero and Espanso still reports it (no `ignore_error: true`, which would
paste empty output instead). Every other command keeps stderr and the default warning filters.
"""

from __future__ import annotations

import os
import sys
import warnings

# The subcommands the Espanso match files run, always as the first argument.
TRIGGER_COMMANDS = frozenset({"improve", "persona"})


def quiet_trigger(argv: list[str]) -> bool:
    """Silence stderr when ``argv`` (without the program) runs a trigger command."""
    if argv[:1] and argv[0] in TRIGGER_COMMANDS:
        warnings.simplefilter("ignore")
        discard_stderr()
        return True
    return False


def discard_stderr() -> None:
    """Point file descriptor 2 (and so ``sys.stderr``) at the null device.

    Catches what a warning filter cannot: ``print(file=sys.stderr)``, ``logging``'s last
    resort handler and writes from C code. Without a usable stderr (none at all, or the null
    device cannot be opened) it changes nothing; the warning filter still applies.
    """
    if sys.stderr is None:
        return
    try:
        sys.stderr.flush()
        null = os.open(os.devnull, os.O_WRONLY)
    except (OSError, ValueError):
        return
    try:
        os.dup2(null, 2)
    except OSError:
        pass
    finally:
        os.close(null)


def self_command() -> list[str]:
    """The argv prefix that runs this CLI again in a child process. A frozen build (the
    Windows zip, #185) is its own program: its executable runs the CLI, and it has no
    interpreter to take ``-m``. Otherwise this interpreter runs the module with ``-P``, so a
    module planted in the working directory never runs."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-P", "-m", "promptmend.cli"]


def main() -> None:
    if sys.argv[1:] == ["--version"]:
        # Answered before the CLI and its dependencies load (#223): the same line Typer's
        # eager --version prints, in a fraction of the start-up on a slow machine.
        from . import installed_version

        sys.stdout.write(installed_version() + "\n")
        return
    quiet_trigger(sys.argv[1:])
    from .cli import app

    app()


if __name__ == "__main__":
    main()
