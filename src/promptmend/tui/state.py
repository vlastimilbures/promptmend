"""What the interface shows, read in one go from the services, so every screen shows the same
moment. It is read again after every change, so no screen keeps a stale value (#83)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .. import assets, deploy, doctor, history, previous_install, update_check
from ..commands import common
from ..commands.usage import store
from ..config import ConfigLayers, Settings
from ..prompt_builder import UserProfile, user_profiles


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
    # An earlier checkout install to switch from (#110), with previous_error saying why
    # nothing could be looked for.
    previous: previous_install.Detection = field(default_factory=previous_install.Detection)
    previous_error: str | None = None
    # Whether a newer release exists (#197): Home's Version row and About.
    update: update_check.UpdateStatus = field(default_factory=update_check.UpdateStatus)


def current_plan(runner: deploy.Runner | None = None) -> deploy.Plan:
    """The deploy plan as `espanso status` and `espanso deploy` make it, with no --launcher
    or --espanso-dir: this install's launcher and `espanso path config`."""
    launcher = deploy.resolve_launcher(runner=runner)
    target = deploy.espanso_dir(runner).resolve()
    return deploy.plan(target, deploy.launcher_text(launcher.path), deploy.Manifest.load())


def _once(runner: deploy.Runner) -> deploy.Runner:
    """``runner``, asking each command (`espanso path config`, the launcher lookup) once: doctor,
    the plan and the previous-install lookup all ask, and a reload blocks the Settings list
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
    the profiles, the usage stats, an earlier checkout install and the update check (cached
    for a day, so a reload asks PyPI no more often), each failure kept to show instead of
    raised."""
    layers, settings = common.load_layers()
    update = update_check.check_configured()
    run = _once(deploy.run_command)
    report = doctor.run(clipboard=False, update=update, runner=run)
    plan: deploy.Plan | None = None
    plan_error = None
    try:
        plan = current_plan(run)
    except (deploy.DeployError, OSError, ValueError) as exc:
        plan_error = str(exc)
    stats: list[history.StatsRow] = []
    stats_error = None
    try:
        stats = store(settings).stats(group_by)
    except (history.HistoryError, OSError, ValueError) as exc:
        stats_error = str(exc) or type(exc).__name__
    previous = previous_install.Detection()
    previous_error = None
    try:
        previous = previous_install.detect(runner=run)
    except (deploy.DeployError, OSError, ValueError) as exc:
        previous_error = str(exc) or type(exc).__name__
    return State(
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
        previous=previous,
        previous_error=previous_error,
        update=update,
    )
