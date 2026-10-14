"""What Home shows (#112, Mockup B): one row per thing that has to work for a trigger to
rewrite, each with a status word (never colour alone) and the tab that fixes it, and a
headline naming the worst one. Worked out from the State alone, with no read of its own, so
it is a pure function a test can call without the interface (and without Textual)."""

from __future__ import annotations

from dataclasses import dataclass

from .. import __version__, deploy, doctor, update_check
from ..commands import common
from ..factory import routes
from .state import State

OK, WARN, FAIL = doctor.OK, doctor.WARN, doctor.FAIL
# A newer release (#197): highlighted, but no problem, so worst() and headline() count it as
# ok and an update never makes Home "Almost ready".
NOTE = "note"
# The Version row's pointer to the update command, which About (`a`) shows in full: every
# channel's command is too long for the row at 70 columns.
HOW_TO_UPDATE = "a: how to update"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

# Tab id -> its label; the digit in each label is its key (app.TABS adds the panes).
TAB_LABELS = {
    "home": "1 Home",
    "settings": "2 Settings",
    "profiles": "3 Profiles",
    "triggers": "4 Triggers",
    "history": "5 History",
    "diagnostics": "6 Diagnostics",
    "try": "7 Try",
}
READY = "Ready: type -i- in any text field."
# PROMPT_OUTPUT (#134) value -> what Home says it does.
OUTPUTS = {"paste": "paste into the app", "clipboard": "copy to the clipboard"}
# The Output row's pointer to the tab that switches it (#198).
CHANGE_OUTPUT = f"change: {TAB_LABELS['settings']}"
_NUMBERS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


@dataclass(frozen=True)
class HomeRow:
    """``status`` is ok, warn, fail or note; ``detail`` is the short note on the right (key set);
    ``jump`` the tab id that shows more; ``problem`` the headline's sentence when not ok."""

    status: str
    label: str
    text: str
    detail: str = ""
    jump: str | None = None
    problem: str = ""


def _status(check_status: str) -> str:
    """A doctor status as a row status: info (version, a skipped check) and note count as
    ok."""
    return check_status if check_status in _RANK else OK


def worst(*statuses: str) -> str:
    return max((_status(s) for s in statuses), key=_RANK.__getitem__, default=OK)


def _count(n: int, noun: str) -> str:
    return f"{_NUMBERS[n] if n < len(_NUMBERS) else n} {noun}{'' if n == 1 else 's'}"


def _checks(state: State) -> dict[str, doctor.Check]:
    return {c.id: c for c in state.report.checks}


def _version_row(state: State) -> HomeRow:
    """The installed version, or the newer release (the header shows the installed one) and
    where to find its update command."""
    update = state.update
    if update.state == update_check.AVAILABLE:
        return HomeRow(NOTE, "Version", f"{update.latest} available", HOW_TO_UPDATE)
    words = {update_check.LATEST: " · latest", update_check.OFF: " · check off"}
    return HomeRow(OK, "Version", f"{__version__}{words.get(update.state, '')}")


def _trigger_row(state: State, label: str, name: str) -> HomeRow:
    """The provider and model a trigger runs (its command line names them; PROMPT_PROVIDER
    only when it does not), and whether its key is there."""
    trigger = next((t for t in state.triggers if t.trigger == name), None)
    cfg = state.settings
    provider = (trigger.provider if trigger else None) or cfg.provider
    route = next((r for r in routes(cfg) if r.name == provider), None)
    if route is None:  # a provider name no route knows: nothing more to say about it
        return HomeRow(FAIL, label, provider, "", "settings", f"{name} names {provider}")
    entries = state.layers.entries
    model_setting = route.model_setting
    if trigger is not None and trigger.tier == "pro" and provider == "openrouter":
        model_setting = "OPENROUTER_PRO_MODEL"
    text = f"{provider} · {common.shown_value(model_setting, entries[model_setting])}"
    if route.refused:
        problem = f"PROMPT_LOCAL_ONLY is on, so {name} ({provider}) is refused"
        return HomeRow(FAIL, label, text, "refused", "settings", problem)
    if route.key and not entries[route.key].value:
        problem = f"{route.key} is not set, so {name} cannot rewrite"
        return HomeRow(FAIL, label, text, "key not set", "settings", problem)
    detail = "key set" if route.key else ("no key needed" if route.remote else "stays local")
    return HomeRow(OK, label, text, detail, "settings")


def _espanso_words(check: doctor.Check | None) -> str:
    data = check.data if check else {}
    if data.get("found") is False:
        return "Espanso not found"
    if data.get("query_failed"):
        return "Espanso folder unknown"
    if data.get("running") is False:
        return "Espanso not running"
    running = data.get("running") or (check is not None and check.status == OK)
    return "Espanso running" if running else "Espanso unknown"


def _triggers_row(state: State) -> HomeRow:
    checks = _checks(state)
    espanso, match_files, launcher = (checks.get(i) for i in ("espanso", "match_files", "launcher"))
    words = _espanso_words(espanso)
    if state.plan is None:
        problem = f"cannot compare with Espanso: {state.plan_error}"
        return HomeRow(WARN, "Triggers", f"not compared · {words}", "", "triggers", problem)
    steps = state.plan.steps
    in_sync = sum(s.state == deploy.IN_SYNC for s in steps)
    text = f"{in_sync} of {len(steps)} match files in sync · {words}"
    status = worst(*(c.status for c in (espanso, match_files, launcher) if c is not None))
    edited = sum(s.state in (deploy.MODIFIED, deploy.FOREIGN) for s in steps)
    outdated = sum(s.state in (deploy.STALE, deploy.MISSING) for s in steps)
    if outdated:
        verb = "is" if outdated == 1 else "are"
        problem = f"{_count(outdated, 'match file')} {verb} not deployed or out of date"
    elif edited:
        verb = "was" if edited == 1 else "were"
        problem = f"{_count(edited, 'match file')} {verb} edited since the last deploy"
    else:
        others = [c for c in (espanso, launcher) if c is not None and _status(c.status) != OK]
        problem = others[0].message if others else ""
    if status == OK and (outdated or edited):
        status = WARN  # the plan says so even where doctor could not compare
    return HomeRow(status, "Triggers", text, "", "triggers", problem)


def _history_row(state: State) -> HomeRow:
    check = _checks(state).get("history")
    if not state.settings.history:
        return HomeRow(OK, "History", "off", "", "history")
    if state.stats_error:
        return HomeRow(WARN, "History", "on · cannot read", "", "history", state.stats_error)
    if check is not None and _status(check.status) != OK:
        text = "on · tracking incomplete"
        return HomeRow(_status(check.status), "History", text, "", "history", check.message)
    count = check.data.get("operations") if check else None
    if not isinstance(count, int):
        count = sum(row.operations for row in state.stats)
    text = f"on · {count} call{'' if count == 1 else 's'} recorded"
    return HomeRow(OK, "History", text, "", "history")


def _checks_row(state: State) -> HomeRow:
    checks = state.report.checks
    counts = {s: sum(_status(c.status) == s for c in checks) for s in (OK, WARN, FAIL)}
    text = f"{counts[OK]} ok · {counts[WARN]} warn · {counts[FAIL]} fail"
    status = worst(*(c.status for c in checks))
    first = next((c for c in checks if _status(c.status) == status), None)
    problem = f"{first.id}: {first.message}" if first and status != OK else ""
    return HomeRow(status, "Checks", text, "", "diagnostics", problem)


def home_rows(state: State) -> list[HomeRow]:
    """Home's rows, in the order a person reads them: the version (#197), what rewrites, the
    deployed triggers, the history, where the rewrite goes (PROMPT_OUTPUT, #134) and every
    check."""
    output = state.settings.output
    return [
        _version_row(state),
        _trigger_row(state, "Rewrites", "-i-"),
        _trigger_row(state, "Pro (-ip-)", "-ip-"),
        _triggers_row(state),
        _history_row(state),
        # Always names where to change it (#198), though it is never a problem.
        HomeRow(OK, "Output", OUTPUTS.get(output, output), CHANGE_OUTPUT, "settings"),
        _checks_row(state),
    ]


def headline(rows: list[HomeRow]) -> tuple[str, str | None]:
    """The worst row's problem as one sentence, with the tab that fixes it; ``READY`` when
    every row is ok. The first of equally bad rows wins, so the reading order decides."""
    status = worst(*(r.status for r in rows))
    if status == OK:
        return READY, None
    row = next(r for r in rows if r.status == status)
    lead = "Not ready" if status == FAIL else "Almost ready"
    # A doctor message marks a command with Markdown backticks, shown literally here (#174).
    problem = (row.problem or f"{row.label}: {row.text}").replace("`", "").rstrip(".")
    return f"{lead}: {problem}.", row.jump


def pill(report: doctor.Report) -> str:
    """The header's status: ok, or how many checks fail (problems) and warn (warnings)."""
    fails = sum(c.status == FAIL for c in report.checks)
    warns = sum(c.status == WARN for c in report.checks)
    parts = [f"{fails} problem{'' if fails == 1 else 's'}"] if fails else []
    parts += [f"{warns} warning{'' if warns == 1 else 's'}"] if warns else []
    return ", ".join(parts) or "ok"
