"""What Home shows: one row per thing that has to work for a trigger to
rewrite, each with a status word (never colour alone) and the tab that fixes it, and a
headline naming the worst one. Worked out from the State alone, with no read of its own, so
it is a pure function a test can call without the interface (and without Textual).

A quick State has no doctor report, plan or stats yet: the rows read from them say
`checking` (status WAIT) until the full State replaces it, or `not checked` when its read
failed.

fix() names the one action that opens the headline's fix (Home's button for it), from the
same row the headline names: the deploy preview, the Set key dialog for a missing key, or
the tab the row points to."""

from __future__ import annotations

from dataclasses import dataclass

from .. import deploy, doctor, installed_version, update_check
from ..commands import common
from ..config import model_setting
from ..factory import routes
from .state import State

OK, WARN, FAIL = doctor.OK, doctor.WARN, doctor.FAIL
# A newer release: highlighted, but no problem, so worst() and headline() count it as
# ok and an update never makes Home "Almost ready".
NOTE = "note"
# A row the full State fills in: no problem yet, so worst() counts it as ok, and the
# headline says the checks are running instead of Ready.
WAIT = "checking"
CHECKING_HEADLINE = "Checking Espanso, the match files and the history…"
# The same with PROMPT_HISTORY=false: the History row says off, it is not checked.
CHECKING_HEADLINE_NO_HISTORY = "Checking Espanso and the match files…"
# A not-checked row's pointer: r reads everything again; no tab fixes it.
CHECK_AGAIN = "r: check again"
# The Version row's pointer to the update command, which About (`a`) shows in full: every
# channel's command is too long for the row at 70 columns.
HOW_TO_UPDATE = "a: how to update"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

# Tab id -> its label; the digit in each label is its key (app.TABS adds each pane's module
# and class, imported when its tab first opens).
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
# PROMPT_OUTPUT value -> what Home says it does.
OUTPUTS = {"paste": "paste into the app", "clipboard": "copy to the clipboard"}
# The Output row's pointer to the tab that switches it.
CHANGE_OUTPUT = f"change: {TAB_LABELS['settings']}"
_NUMBERS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


@dataclass(frozen=True)
class HomeRow:
    """``status`` is ok, warn, fail, note or checking (WAIT); ``detail`` is the short note on
    the right (key set); ``jump`` the tab id that shows more; ``problem`` the headline's
    sentence when not ok; ``key`` the key this row's trigger is missing; ``redeploy``
    whether a deploy fixes this row (match files not deployed, out of date or edited,
    or a stale deployed -ip-, its key missing or not)."""

    status: str
    label: str
    text: str
    detail: str = ""
    jump: str | None = None
    problem: str = ""
    key: str = ""
    redeploy: bool = False


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
    words = {
        update_check.LATEST: " · latest",
        update_check.OFF: " · check off",
        # Filled in when pypi.org answers; the other rows never wait for it.
        update_check.CHECKING: " · checking",
    }
    return HomeRow(OK, "Version", f"{installed_version()}{words.get(update.state, '')}")


def trigger_row(state: State, label: str, name: str) -> HomeRow:
    """The provider and model a trigger runs (its command line names them; PROMPT_PROVIDER,
    or PROMPT_PRO_PROVIDER on the pro tier, only when it does not), and whether its key is
    there. The deployed match file's command line wins over the shipped one, since that is
    what Espanso runs: an older -ip- still names OpenRouter (doctor.stale_pro_pins()). The
    Settings tab's two lines under Triggers show the same ``text`` and ``detail`` (#389)."""
    # Only an active deployed trigger whose command line was read; a commented-out copy or
    # one this release cannot read falls back to the shipped one.
    deployed = [t for t in state.deployed or [] if t.active and t.command is not None]
    trigger = next((t for t in [*deployed, *state.triggers] if t.trigger == name), None)
    cfg = state.settings
    tier = (trigger.tier if trigger else None) or "standard"
    provider = (trigger.provider if trigger else None) or cfg.provider_for(tier)
    route = next((r for r in routes(cfg) if r.name == provider), None)
    if route is None:  # a provider name no route knows: nothing more to say about it
        return HomeRow(FAIL, label, provider, "", "settings", f"{name} names {provider}")
    entries = state.layers.entries
    model = model_setting(provider, tier)
    text = f"{provider} · {common.shown_value(model, entries[model])}"
    stale = {k: v for k, v in doctor.stale_pro_pins(deployed, cfg).items() if k == name}
    # An older deployed -ip-: the Triggers tab redeploys it, whatever else is wrong.
    redeploy = doctor.stale_pro_message(stale, cfg.pro_provider) if stale else ""
    jump = "triggers" if stale else "settings"
    if route.refused:
        # A redeploy does not lift the refusal: Settings (PROMPT_LOCAL_ONLY) does.
        problem = f"PROMPT_LOCAL_ONLY is on, so {name} ({provider}) is refused"
        return HomeRow(
            FAIL, label, text, "refused", "settings", "; ".join(filter(None, (problem, redeploy)))
        )
    if route.key and not entries[route.key].value:
        problem = f"{route.key} is not set, so {name} cannot rewrite"
        # A stale -ip- points to Triggers: once redeployed it runs on PROMPT_PRO_PROVIDER,
        # which may need another key or none, so the redeploy comes first (fix()).
        return HomeRow(
            FAIL,
            label,
            text,
            "key not set",
            jump,
            "; ".join(filter(None, (problem, redeploy))),
            key=route.key,
            redeploy=bool(stale),
        )
    detail = "key set" if route.key else ("no key needed" if route.remote else "stays local")
    if stale:
        return HomeRow(WARN, label, text, "redeploy", "triggers", redeploy, redeploy=True)
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


def _waiting(state: State, label: str, text: str, jump: str) -> HomeRow:
    """A row the full State fills in: checking while it is read, not checked when it failed
    (r reads it again)."""
    if state.failed is None:
        return HomeRow(WAIT, label, f"checking {text}", "", jump)
    problem = f"the checks could not run ({state.failed}); r checks again"
    return HomeRow(WARN, label, "not checked", CHECK_AGAIN, None, problem)


def _triggers_row(state: State) -> HomeRow:
    if not state.full:
        return _waiting(state, "Triggers", "the match files and Espanso", "triggers")
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
    fixed = bool(outdated or edited)
    return HomeRow(status, "Triggers", text, "", "triggers", problem, redeploy=fixed)


def _history_row(state: State) -> HomeRow:
    check = _checks(state).get("history")
    if not state.settings.history:
        return HomeRow(OK, "History", "off", "", "history")
    if not state.full:
        return _waiting(state, "History", "the usage history", "history")
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
    if not state.full:
        return _waiting(state, "Checks", "every part", "diagnostics")
    checks = state.report.checks
    counts = {s: sum(_status(c.status) == s for c in checks) for s in (OK, WARN, FAIL)}
    text = f"{counts[OK]} ok · {counts[WARN]} warn · {counts[FAIL]} fail"
    status = worst(*(c.status for c in checks))
    first = next((c for c in checks if _status(c.status) == status), None)
    problem = f"{first.id}: {first.message}" if first and status != OK else ""
    return HomeRow(status, "Checks", text, "", "diagnostics", problem)


def home_rows(state: State) -> list[HomeRow]:
    """Home's rows, in the order a person reads them: the version, what rewrites, the
    deployed triggers, the history, where the rewrite goes (PROMPT_OUTPUT) and every
    check."""
    output = state.settings.output
    return [
        _version_row(state),
        trigger_row(state, "Rewrites", "-i-"),
        trigger_row(state, "Pro (-ip-)", "-ip-"),
        _triggers_row(state),
        _history_row(state),
        # Always names where to change it, though it is never a problem.
        HomeRow(OK, "Output", OUTPUTS.get(output, output), CHANGE_OUTPUT, "settings"),
        _checks_row(state),
    ]


def headline_row(rows: list[HomeRow]) -> HomeRow | None:
    """The row the headline names: the first of the worst ones, so the reading order
    decides; None when every row is ok (a row still checking counts as ok)."""
    status = worst(*(r.status for r in rows))
    if status == OK:
        return None
    return next(r for r in rows if r.status == status)


def headline(rows: list[HomeRow]) -> tuple[str, str | None]:
    """The worst row's problem as one sentence, with the tab that fixes it; ``READY`` when
    every row is ok, CHECKING_HEADLINE while one is still checking. The first of
    equally bad rows wins, so the reading order decides."""
    row = headline_row(rows)
    if row is None:
        waiting = {r.label for r in rows if r.status == WAIT}
        if not waiting:
            return READY, None
        if "History" in waiting:
            return CHECKING_HEADLINE, None
        return CHECKING_HEADLINE_NO_HISTORY, None
    lead = "Not ready" if row.status == FAIL else "Almost ready"
    # A doctor message marks a command with Markdown backticks; stripped, since plain text
    # would show them literally.
    problem = (row.problem or f"{row.label}: {row.text}").replace("`", "").rstrip(".")
    return f"{lead}: {problem}.", row.jump


# What Home's fix button opens (Fix.action).
DEPLOY = "deploy"  # the Triggers tab's deploy preview, as its Deploy or repair button
SET_KEY = "set-key"  # the Settings tab's Set key dialog for Fix.key
OPEN = "open"  # the tab Fix.tab


@dataclass(frozen=True)
class Fix:
    """The action that opens the headline's fix: ``action`` (DEPLOY, SET_KEY or OPEN) on
    ``tab``; ``key`` the key SET_KEY opens the dialog for."""

    action: str
    tab: str
    key: str = ""

    @property
    def label(self) -> str:
        """The button's label: an ellipsis when it opens a dialog."""
        if self.action == DEPLOY:
            return "Deploy or repair…"
        if self.action == SET_KEY:
            return f"Set {self.key}…"
        return f"Open {TAB_LABELS[self.tab]}"


def fix(rows: list[HomeRow]) -> Fix | None:
    """What opens the fix of the row the headline names (headline_row()), always on the tab
    its `->` pointer names: the deploy preview when the row points to Triggers and a deploy
    fixes it, Set key when it points to Settings for a missing key, else that tab. None when
    no row is a problem (ready, or only checking rows, which the full State fills in) and
    for a row no tab fixes (`not checked`, which Check again reads again). On the quick
    State a problem it already knows (a missing key) has its fix at once."""
    row = headline_row(rows)
    if row is None:
        return None
    if row.redeploy and row.jump == "triggers":
        return Fix(DEPLOY, "triggers")
    if row.key and row.jump == "settings":
        return Fix(SET_KEY, "settings", row.key)
    if row.jump:
        return Fix(OPEN, row.jump)
    return None


def pill(report: doctor.Report) -> str:
    """The header's status: ok, or how many checks fail (problems) and warn (warnings)."""
    fails = sum(c.status == FAIL for c in report.checks)
    warns = sum(c.status == WARN for c in report.checks)
    parts = [f"{fails} problem{'' if fails == 1 else 's'}"] if fails else []
    parts += [f"{warns} warning{'' if warns == 1 else 's'}"] if warns else []
    return ", ".join(parts) or "ok"


def status_pill(state: State) -> str:
    """The header's status for ``state``: pill() of its report, or `checking` (`not checked`
    when its full read failed) for a quick State, whose report is not read yet."""
    if not state.full:
        return "checking" if state.failed is None else "not checked"
    return pill(state.report)
