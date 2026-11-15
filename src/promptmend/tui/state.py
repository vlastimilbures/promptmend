"""What the interface shows, read in one go from the services, so every screen shows the same
moment. It is read again after every change, so no screen keeps a stale value (#83).

One read (#315) resolves the settings once, looks up the launcher and the manifest once, makes
one deploy plan that doctor reuses, and asks each command once. The update check runs beside it
in a thread of its own: when it has not landed by the time the rest is read, the State says
``checking`` and carries it as ``pending``; settled() waits for it and makes the next State,
which the app gives every screen, as it does a reload."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field, replace

from .. import assets, deploy, doctor, espanso_windows, history, update_check
from ..commands import common
from ..commands.usage import store
from ..config import ConfigLayers, Settings
from ..prompt_builder import UserProfile, user_profiles

CHECKING = update_check.UpdateStatus(update_check.CHECKING)
# How long settled() waits for the update check: its own request gives up after
# update_check.TIMEOUT; the rest is reading and writing its small cache file.
SETTLE_TIMEOUT = update_check.TIMEOUT + 2.0


@dataclass(frozen=True)
class State:
    layers: ConfigLayers
    settings: Settings
    report: doctor.Report
    triggers: list[assets.Trigger]
    # None, with plan_error saying why, when the launcher or Espanso's folder is not found.
    plan: deploy.Plan | None
    plan_error: str | None
    profiles: list[UserProfile]
    group_by: str
    stats: list[history.StatsRow]
    stats_error: str | None
    # Whether a newer release exists (#197): Home's Version row and About.
    update: update_check.UpdateStatus = field(default_factory=update_check.UpdateStatus)
    # The update check still running when the rest was read (update is CHECKING then).
    pending: Future[update_check.UpdateStatus] | None = field(
        default=None, compare=False, repr=False
    )

    def with_update(self, update: update_check.UpdateStatus) -> State:
        """This moment with ``update`` in Home's Version row, About and doctor's version
        check; nothing else is read again."""
        report = doctor.with_update(self.report, update)
        return replace(self, update=update, report=report, pending=None)

    def landed(self, done: Future[update_check.UpdateStatus]) -> State:
        """The next State for the finished update check ``done``. Never raises and never
        stays `checking`: a failed check, or one whose answer cannot be shown, is unknown."""
        unknown = update_check.UpdateStatus(update_check.UNKNOWN)
        try:
            update = done.result(timeout=0)
        except Exception:
            update = unknown
        for answer in (update, unknown):
            try:
                return self.with_update(answer)
            except Exception:  # noqa: S112 - fall back to unknown, then to no report change
                continue
        return replace(self, update=unknown, pending=None)

    def settled(self) -> State:
        """This State once its update check has landed (unknown if it never does within
        SETTLE_TIMEOUT); itself when nothing is pending. Blocks, so the app never calls it
        (it waits with a callback); for tests and scripts."""
        if self.pending is None:
            return self
        # landed() makes a failed or late check unknown.
        with contextlib.suppress(Exception):
            self.pending.result(timeout=SETTLE_TIMEOUT)
        return self.landed(self.pending)


def _beside[T](work: Callable[[], T], name: str) -> Future[T]:
    """``work`` in a daemon thread, so quitting never waits for it."""
    future: Future[T] = Future()

    def run() -> None:
        try:
            future.set_result(work())
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=run, name=name, daemon=True).start()
    return future


def _plan(found: doctor.Lookups, runner: deploy.Runner | None) -> deploy.Plan:
    """The deploy plan for ``found``'s launcher and manifest and `espanso path config`;
    DeployError (or OSError, ValueError) when it cannot be made."""
    if found.launcher is None:
        raise deploy.DeployError(found.launcher_error or "no launcher found")
    target = deploy.espanso_dir(runner).resolve()
    text = deploy.launcher_text(found.launcher.path)
    if found.manifest is None:
        raise deploy.DeployError(found.manifest_error or "the deploy manifest cannot be read")
    return deploy.plan(target, text, found.manifest)


def current_plan(runner: deploy.Runner | None = None) -> deploy.Plan:
    """The deploy plan as `espanso status` and `espanso deploy` make it, with no --launcher
    or --espanso-dir: this install's launcher and `espanso path config`."""
    return _plan(doctor.lookups(runner), runner)


def _once(runner: deploy.Runner) -> deploy.Runner:
    """``runner``, asking each command (`espanso path config`, the launcher lookup) once: doctor
    and the plan both ask, and a reload blocks the Settings list
    (#242). Read-only commands only, so one answer per reload is the same moment."""
    answers: dict[tuple[str, ...], str | deploy.CommandFailure | None] = {}

    def run(argv: Sequence[str]) -> str | deploy.CommandFailure | None:
        key = tuple(argv)
        if key not in answers:
            answers[key] = runner(argv)
        return answers[key]

    return run


def gather(group_by: str = "trigger") -> State:
    """Everything the screens show. Read-only: doctor without the clipboard, the deploy plan,
    the profiles, the usage stats and the update check (cached for a day, so a reload asks
    PyPI no more often; never waited for, see the module), each failure kept to show instead
    of raised."""
    pending = _beside(update_check.check_configured, "promptmend-reload-update-check")
    layers, settings = common.load_layers()
    # Only read-only commands may go through it: a cached `espanso restart` would be skipped.
    run = _once(deploy.run_command)
    plan: deploy.Plan | None = None
    plan_error = None
    with espanso_windows.one_look():
        found = doctor.lookups(run)
        try:
            plan = _plan(found, run)
        except (deploy.DeployError, OSError, ValueError) as exc:
            plan_error = str(exc)
        report = doctor.run(
            clipboard=False,
            update=CHECKING,
            runner=run,
            layers=layers,
            found=found,
            plan=plan,
        )
    stats: list[history.StatsRow] = []
    stats_error = None
    try:
        stats = store(settings).stats(group_by)
    except (history.HistoryError, OSError, ValueError) as exc:
        stats_error = str(exc) or type(exc).__name__
    state = State(
        layers=layers,
        settings=settings,
        report=report,
        triggers=assets.triggers(),
        plan=plan,
        plan_error=plan_error,
        profiles=user_profiles(settings.profile_overrides),
        group_by=group_by,
        stats=stats,
        stats_error=stats_error,
        update=CHECKING,
        pending=pending,
    )
    # Landed already (a cached answer, the check turned off): one State, no "checking".
    return state.settled() if pending.done() else state
