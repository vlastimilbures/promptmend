"""The console-script entry point (`promptmend`).

Espanso runs the triggers' calls as `type: script` vars, and such a var fails on any
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
import time
import warnings

# The subcommands the Espanso match files run, always as the first argument.
TRIGGER_COMMANDS = frozenset({"improve", "persona"})

# When main() started a trigger run, improve or persona (time.monotonic()), for its watchdog,
# which counts the time limit from here; None until then, and once take_start() has read it.
_started: float | None = None


def take_start() -> float | None:
    """The time main() started a trigger command, read once (so a later run in the same
    process, such as a test's, counts from its own start), or None when the CLI was started
    another way."""
    global _started
    started, _started = _started, None
    return started


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
    Windows zip) is its own program: its executable runs the CLI, and it has no
    interpreter to take ``-m``. Otherwise this interpreter runs this module, so the child
    starts through main() as the console script does, with ``-P``, so a module planted in the
    working directory never runs."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-P", "-m", "promptmend.entry"]


def main() -> None:
    global _started
    if sys.argv[1:2] and sys.argv[1] in TRIGGER_COMMANDS:
        _started = time.monotonic()
    if sys.argv[1:] == ["--version"]:
        # Answered before the CLI and its dependencies load: the same line Typer's
        # eager --version prints, in a fraction of the start-up on a slow machine.
        from . import installed_version

        sys.stdout.write(installed_version() + "\n")
        return
    quiet_trigger(sys.argv[1:])
    from .cli import app

    app()


if __name__ == "__main__":
    # `python -m promptmend.entry` runs this file as __main__, a module apart from
    # promptmend.entry, which the watchdog imports for take_start(). Run that module's main(),
    # so the start time (and anything a test patches) lives where it is read.
    from promptmend.entry import main as _main

    _main()
