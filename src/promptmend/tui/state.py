"""What the interface shows, read in one go from the services, so every screen shows the same
moment. It is read again after every change, so no screen keeps a stale value.

One read resolves the settings once, looks up the launcher and the manifest once, makes
one deploy plan that doctor reuses, and asks each command once. The update check runs beside it
in a thread of its own: when it has not landed by the time the rest is read, the State says
``checking`` and carries it as ``pending``; settled() waits for it and makes the next State,
which the app gives every screen, as it does a reload.

Two stages: quick() reads only what is local and fast (the settings, the profiles, the
triggers) and the app paints it at once; gather() reads everything and its State replaces the
quick one. A quick State's report, plan and stats are placeholders no screen shows (it says
`checking` there instead), and an action that needs them waits for the full State
(``State.full``)."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import (
    assets,
    config,
    config_files,
    deploy,
    doctor,
    espanso_windows,
    history,
    settings_service,
    update_check,
)
from ..commands.usage import store
from ..config import ConfigLayers, Settings, secret_names
from ..prompt_builder import UserProfile, user_profiles

CHECKING = update_check.UpdateStatus(update_check.CHECKING)
# A State's stage: QUICK holds the settings, profiles and triggers only; FULL all of it.
QUICK, FULL = "quick", "full"
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
    # Whether a newer release exists: Home's Version row and About.
    update: update_check.UpdateStatus = field(default_factory=update_check.UpdateStatus)
    # The update check still running when the rest was read (update is CHECKING then).
    pending: Future[update_check.UpdateStatus] | None = field(
        default=None, compare=False, repr=False
    )
    # QUICK: only layers, settings, triggers, profiles and group_by are read; FULL: all.
    stage: str = FULL
    # A quick State whose full read failed (why, for the rows that wait): see not_checked().
    failed: str | None = None
    # The triggers in the match files Espanso has (doctor.deployed_triggers()): what really
    # runs when the file is older or edited. None before the full read, or with none deployed.
    deployed: list[assets.Trigger] | None = None

    @property
    def full(self) -> bool:
        """Whether the doctor report, the plan and the stats are read: an action that
        reads them, or acts on what they show, waits for this."""
        return self.stage == FULL

    def not_checked(self, error: str) -> State:
        """This quick State once its full read failed with ``error``: the rows that waited
        say they were not checked, never `checking` for ever; still not full."""
        unknown = update_check.UpdateStatus(update_check.UNKNOWN)
        return replace(self, failed=error, update=unknown, pending=None)

    def known_update(self, previous: State | None) -> State:
        """This quick State with the update answer ``previous`` (the full State on screen)
        settled, so a reload's Version row does not go back to `checking`. Not when
        either says off: the settings just read decide that."""
        off = update_check.OFF
        if self.full or self.update.state == off or previous is None or not previous.full:
            return self
        if previous.update.state in (update_check.CHECKING, off):
            return self
        return replace(self, update=previous.update)

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
        except BaseException:  # the check's thread kept anything it raised (_beside)
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
    """``work`` in a daemon thread of its own, so quitting never waits for it and it never
    holds a thread of a shared pool. Running once started: cancelling the future then (as an
    awaiting task's cancellation does) leaves it to finish, its answer unused."""
    future: Future[T] = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return  # cancelled before it started
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
    target = deploy.resolved(deploy.espanso_dir(runner))
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
    and the plan both ask, and a reload blocks the Settings list. Read-only commands only, so
    one answer per reload is the same moment."""
    answers: dict[tuple[str, ...], str | deploy.CommandFailure | None] = {}

    def run(argv: Sequence[str]) -> str | deploy.CommandFailure | None:
        key = tuple(argv)
        if key not in answers:
            answers[key] = runner(argv)
        return answers[key]

    return run


def quick(group_by: str = "trigger") -> State:
    """The first stage: the settings, the profiles and the triggers, local reads only
    (no command, no doctor, no history, no network), so Home paints at once. Everything else
    is a placeholder and the State is not full. The update check says off at once when the
    settings turn it off, else checking (see known_update())."""
    layers, settings = settings_service.load_layers()
    return State(
        layers=layers,
        settings=settings,
        report=doctor.Report(()),
        triggers=assets.triggers(),
        plan=None,
        plan_error=None,
        profiles=user_profiles(settings.profile_overrides),
        group_by=group_by,
        stats=[],
        stats_error=None,
        update=CHECKING if settings.update_check else update_check.UpdateStatus(update_check.OFF),
        stage=QUICK,
    )


def gather(group_by: str = "trigger", first: State | None = None) -> State:
    """Everything the screens show. Read-only: doctor without the clipboard, the deploy plan,
    the profiles, the usage stats and the update check (cached for a day, so a reload asks
    PyPI no more often; never waited for, see the module), each failure kept to show instead
    of raised. ``first`` is this reload's quick State: its settings, profiles and
    triggers are reused, so a reload reads them once; without it they are read here."""
    pending = _beside(update_check.check_configured, "promptmend-reload-update-check")
    if first is None or first.full or first.group_by != group_by:
        first = quick(group_by)
    layers, settings = first.layers, first.settings
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
    # The folder doctor read its deployed triggers from, so Home and doctor never disagree,
    # with or without a plan (a launcher not found, say).
    espanso = next((c for c in report.checks if c.id == "espanso"), None)
    folder = espanso.data.get("config_dir") if espanso else None
    deployed = None
    if isinstance(folder, str) and folder:
        found_triggers, any_deployed = doctor.deployed_triggers(Path(folder))
        deployed = found_triggers if any_deployed else None
    stats: list[history.StatsRow] = []
    stats_error = None
    try:
        stats = store(settings).stats(group_by)
    except (history.HistoryError, OSError, ValueError) as exc:
        stats_error = str(exc) or type(exc).__name__
    state = replace(
        first,
        report=report,
        plan=plan,
        plan_error=plan_error,
        stats=stats,
        stats_error=stats_error,
        update=CHECKING,
        pending=pending,
        stage=FULL,
        deployed=deployed,
    )
    # Landed already (a cached answer, the check turned off): one State, no "checking".
    return state.settled() if pending.done() else state


def needs_setup(state: State) -> bool:
    """Nothing is set up yet: no config.toml, no key, and no match file installed. A bare
    `promptmend` then opens the wizard by itself, once; never of a quick State, which has no
    plan. Here, not in setup_wizard.py, so the app asks without importing the wizard."""
    if settings_service.legacy_env() or not state.full or state.plan is None:
        return False
    if config_files.is_file(config.settings_file()):
        return False
    if any(state.layers.entries[name].value for name in secret_names()):
        return False
    return all(step.state == deploy.MISSING for step in state.plan.steps)
