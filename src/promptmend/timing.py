"""`doctor --timing`: each command through the launcher and the interpreter, the interface's
import, and each check's duration. It times `--version`, `persona` and the -i- trigger as
child processes against smoke's stub, a second harness next to smoke.time_runs: no provider
is called and only the placeholder key is set.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import installed_version, parsers
from .doctor import CHECK_IDS, Report
from .providers.registry import PROVIDERS

# `doctor --timing --runs N`: the warm runs after each cold one; WARM_RUNS is the default,
# and `scripts/build_windows.py time`'s. It is defined here and not in smoke: the interface's
# Home loads commands/doctor.py, and so this module, before its first paint, which must not
# load smoke or http.server (tests/test_ui_startup.py).
RUNS_RANGE = (1, 50)
WARM_RUNS = 5
TIMED_OK, TIMED_FAILED, TIMED_SKIPPED = "ok", "failed", "skipped"
LAUNCHER, INTERPRETER = "launcher", "interpreter"
INTERFACE_IMPORT = "import promptmend.tui.app"


@dataclass(frozen=True)
class Timed:
    """One command timed through one way of starting it: the cold run first, then the warm."""

    command: str
    via: str
    status: str
    seconds: tuple[float, ...] = ()
    message: str | None = None

    @property
    def cold(self) -> float | None:
        return self.seconds[0] if self.seconds else None

    @property
    def warm(self) -> float | None:
        """The median of the warm runs; None without one (a failed or a single run)."""
        import statistics

        return statistics.median(self.seconds[1:]) if len(self.seconds) > 1 else None

    def to_json(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "via": self.via,
            "status": self.status,
            "cold_ms": _ms(self.cold),
            "warm_median_ms": _ms(self.warm),
            "warm_runs": max(len(self.seconds) - 1, 0),
            "message": self.message,
        }


@dataclass(frozen=True)
class Timing:
    """`doctor --timing`: each command through the launcher and the interpreter, the
    interface's import, and each check's duration. ``launcher`` is None when it is not timed
    (``launcher_note`` says why); ``drift_note`` says when the deployed matches call another
    launcher than the one timed."""

    warm_runs: int
    launcher: str | None
    launcher_note: str | None
    interpreter: tuple[str, ...]
    runs: tuple[Timed, ...]
    checks: Mapping[str, float]
    drift_note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "warm_runs": self.warm_runs,
            "launcher": self.launcher,
            "launcher_note": self.launcher_note,
            "drift_note": self.drift_note,
            "interpreter": list(self.interpreter),
            "runs": [r.to_json() for r in self.runs],
            "checks_ms": {k: _ms(v) for k, v in self.checks.items()},
        }


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 1)


def _timed_launcher(current: str | None, interpreter: list[str]) -> tuple[str | None, str | None]:
    """The launcher to time, as an absolute path (this install's, or --launcher), or None and
    why not."""
    if not current:
        return None, "no launcher found; timed through the interpreter only"
    # Absolute (links kept, as Espanso runs the path it was given), so a relative --launcher
    # names the same file in every child.
    path = Path(os.path.abspath(Path(current).expanduser()))
    if not path.is_file():
        return None, f"{path} is not a file; timed through the interpreter only"
    if len(interpreter) == 1:
        try:
            same = os.path.samefile(interpreter[0], path)
        except OSError:
            same = False
        if same:
            return None, f"the launcher is this executable ({path}); timed once"
    return str(path), None


def _drift_note(report: Report, launcher: str | None) -> str | None:
    """When Espanso's deployed matches call another launcher than the one timed."""
    data = _status_data(report, "launcher")
    if not data.get("drift"):
        return None
    deployed = ", ".join(data.get("deployed") or [])
    timed = launcher or "the interpreter"
    return f"Espanso's deployed matches call {deployed}, not {timed}, which was timed"


def timing_env(port: int, environ: Mapping[str, str]) -> dict[str, str]:
    """The children's environment: smoke's stub env, with no persona (persona prints its
    placeholder, never yours), paste output (the clipboard is never written), and neither
    PROMPT_LOCAL_ONLY nor PROMPT_EXTRA_PATTERNS, which would refuse or block the -i- trigger's
    OpenRouter call. Turning them off is safe here only because every provider's base URL is
    the 127.0.0.1 stub and every key is smoke.PLACEHOLDER_KEY: nothing can leave the machine.
    Checked, not assumed: RuntimeError if a base URL is not loopback or a key is not the
    placeholder."""
    from . import smoke
    from .providers.base import is_loopback

    env = {
        **smoke.stub_env(port, environ),
        "PROMPT_PERSONA": "",
        "PROMPT_OUTPUT": parsers.PASTE,
        "PROMPT_LOCAL_ONLY": "false",
        "PROMPT_EXTRA_PATTERNS": "",
    }
    for spec in PROVIDERS.values():
        if not is_loopback(env.get(spec.url_setting, "")):
            raise RuntimeError(f"timing: {spec.url_setting} is not the loopback stub")
        if spec.key and env.get(spec.key) != smoke.PLACEHOLDER_KEY:
            raise RuntimeError(f"timing: {spec.key} is not the placeholder key")
    return env


def timing(
    report: Report,
    durations: Mapping[str, float],
    runs: int = WARM_RUNS,
    *,
    timer: Callable[..., tuple[list[float], str | None]] | None = None,
) -> Timing:
    """Time `--version`, `persona` and the -i- trigger through the launcher and through
    entry.self_command(), and the interface's import, as child processes against smoke's stub
    in timing_env(): no provider is called and only the placeholder key is set. A run that
    fails is reported, never raised. When the launcher's --version fails or prints another
    version, it is another CLI (or none), so its persona and improve are not run."""
    from . import smoke
    from .entry import self_command
    from .prompt_builder import PERSONA_PLACEHOLDER

    time_runs = timer or smoke.time_runs
    interpreter = self_command()
    current = _status_data(report, "launcher").get("current")
    launcher, note = _timed_launcher(current, interpreter)
    starts = ([(LAUNCHER, [launcher])] if launcher else []) + [(INTERPRETER, interpreter)]
    try:
        trigger: list[str] | None = smoke.trigger_args()
        trigger_problem = None
    except (LookupError, OSError, ValueError) as exc:
        trigger, trigger_problem = None, f"the {smoke.TRIGGER} args cannot be read: {exc}"
    version = smoke.expect_output(installed_version(), strip=True, show=True)
    commands: list[tuple[str, list[str] | None, smoke.Expect, str | None]] = [
        ("--version", ["--version"], version, None),
        ("persona", ["persona"], smoke.expect_output(PERSONA_PLACEHOLDER), None),
        (f"improve ({smoke.TRIGGER})", trigger, smoke.expect_output(smoke.REPLY), trigger_problem),
    ]
    results: list[Timed] = []
    with smoke.stub_server() as stub:
        env = timing_env(stub.port, os.environ)

        def one(command: str, via: str, argv: list[str], expect: smoke.Expect) -> Timed:
            seconds, problem = time_runs(argv, env, runs, expect)
            status = TIMED_FAILED if problem else TIMED_OK
            return Timed(command, via, status, tuple(seconds), problem)

        broken: set[str] = set()
        for command, args, expect, problem in commands:
            for via, prefix in starts:
                if via in broken:
                    why = "not run: --version through the launcher failed"
                    results.append(Timed(command, via, TIMED_SKIPPED, message=why))
                elif args is None:
                    results.append(Timed(command, via, TIMED_FAILED, message=problem))
                else:
                    timed = one(command, via, [*prefix, *args], expect)
                    results.append(timed)
                    if via == LAUNCHER and expect is version and timed.status != TIMED_OK:
                        broken.add(via)
        if getattr(sys, "frozen", False):
            results.append(
                Timed(
                    INTERFACE_IMPORT,
                    INTERPRETER,
                    TIMED_SKIPPED,
                    message="a frozen build has no interpreter to import it with",
                )
            )
        else:
            importer = [sys.executable, "-P", "-c", INTERFACE_IMPORT]
            results.append(one(INTERFACE_IMPORT, INTERPRETER, importer, _exits_zero))
    checks = {i: durations[i] for i in CHECK_IDS if i in durations}
    return Timing(
        runs,
        launcher,
        note,
        tuple(interpreter),
        tuple(results),
        checks,
        _drift_note(report, launcher),
    )


def _exits_zero(code: int, out: bytes, err: bytes) -> str | None:
    """The interface's import: only the exit code counts (no trigger, so stderr may warn)."""
    return None if code == 0 else f"exit code {code}"


def _status_data(report: Report, check_id: str) -> Mapping[str, Any]:
    for check in report.checks:
        if check.id == check_id:
            return check.data
    return {}
