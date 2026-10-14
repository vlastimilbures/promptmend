"""The `doctor` service: one report of everything a broken trigger can come from (#25, #92).

Read-only: it runs only `espanso path config`, `espanso status` and the launcher lookup
(`uv tool dir`, `brew --prefix`) through deploy.run_command, reads the settings in repair
mode, asks PyPI for the newest release at most once a day (update_check, unless
PROMPT_UPDATE_CHECK is false), asks a loopback Ollama or LM Studio whether it answers
(local_probe), reads the deployed match files, and reads the clipboard only to report its
length. The report never holds a key, the persona or clipboard text, since people paste it
into bug reports. Its JSON shape is stable (SCHEMA_VERSION): ids and keys are only ever added.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__, assets, config, config_files, deploy, factory, local_probe, update_check
from .config import ConfigLayers, secret_names

if TYPE_CHECKING:
    from .history import Health

SCHEMA_VERSION = 1
OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
_RANK = {INFO: 0, OK: 0, WARN: 1, FAIL: 2}
# Every check, in report order; each appears in every report.
CHECK_IDS = (
    "version",
    "cli",
    "install",
    "config",
    "keys",
    "persona",
    "espanso",
    "match_files",
    "launcher",
    "history",
    "sqlite",
    "clipboard",
    "profiles",
    "previous_install",
    "folders",
    "local_servers",
)
# Every key of each check's data, in every report: a check that could not run has them all
# as None, so a consumer never meets a missing key.
DATA_KEYS = {
    "version": ("version", "python", "platform", "latest", "update_available", "checked_at"),
    "cli": ("path", "executable", "frozen"),
    "install": ("channel", "launcher", "editable"),
    "config": ("mode", "files", "valid", "findings"),
    "keys": ("keys", "provider", "triggers"),
    "persona": ("set", "local_only", "findings"),
    "espanso": (
        "found",
        "config_dir",
        "running",
        "query_failed",
        "exit_code",
        "timed_out",
        "error",
    ),
    "match_files": ("espanso_dir", "files", "legacy"),
    "launcher": ("current", "deployed", "drift", "missing", "orphans"),
    "history": (
        "enabled",
        "path",
        "exists",
        "schema_version",
        "operations",
        "attempts",
        "lost_writes",
        "last_lost_utc",
        "tracking_incomplete",
        "writable",
        "error",
        "spooled",
    ),
    "sqlite": ("version", "wal_reset_bug", "journal_mode"),
    "clipboard": ("read", "length", "concealed", "error"),
    "profiles": ("profile", "pro_profile", "user"),
    "previous_install": ("gated", "roots", "signals", "env_file", "retire_pending", "shadow"),
    "folders": ("config_dir", "data_dir", "legacy", "conflicts"),
    "local_servers": ("servers", "deployed"),
}
# Where local_probe asks each local server, below its base URL.
_PROBE_PATHS = {"ollama": "/api/tags", "lmstudio": "/models"}


@dataclass(frozen=True)
class Check:
    id: str
    status: str
    message: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Report:
    checks: tuple[Check, ...]

    @property
    def status(self) -> str:
        return max((c.status for c in self.checks), key=_RANK.__getitem__, default=OK)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "version": __version__,
            "status": self.status,
            "checks": {
                c.id: {
                    "status": c.status,
                    "message": c.message,
                    "data": {k: c.data.get(k) for k in DATA_KEYS[c.id]},
                }
                for c in self.checks
            },
        }


def upgrade_hint(install: Mapping[str, Any]) -> str:
    """The update command for the install check's data: a checkout's own wins over the
    launcher's channel, since an editable tool sits in uv's bin folder too."""
    channel = "editable" if install.get("editable") else str(install.get("channel") or "")
    return update_check.upgrade_command(channel)


def _version_check(update: update_check.UpdateStatus, install: Check | None) -> Check:
    """The installed version and whether PyPI has a newer one (#197): information only, never
    a warning, with the update command for doctor's install channel (a checkout's own)."""
    available = update.state == update_check.AVAILABLE
    known = update.state in (update_check.AVAILABLE, update_check.LATEST)
    data = {
        "version": __version__,
        "python": platform.python_version(),
        "platform": sys.platform,
        "latest": update.latest,
        "update_available": available if known else None,
        "checked_at": update.checked_at,
    }
    message = f"promptmend {__version__}"
    if available:
        message += f", {update.latest} available: {upgrade_hint(install.data if install else {})}"
    return Check("version", INFO, message, data)


def _cli_check() -> Check:
    script = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if sys.platform == "win32" and script and not script.suffix:
        exe = script.with_name(script.name + ".exe")
        # Windows starts the console script's launcher, but argv[0] drops its `.exe` (#222).
        if exe.is_file():
            script = exe
    frozen = bool(getattr(sys, "frozen", False))
    data = {
        "path": str(script) if script else None,
        "executable": sys.executable,
        # The Windows zip's own exe (#185): no interpreter, no import check.
        "frozen": frozen,
    }
    note = " (frozen build)" if frozen else ""
    return Check("cli", INFO, f"running {script or sys.executable}{note}", data)


def _install_check(launcher: deploy.Launcher | None, error: str | None) -> Check:
    editable = (config._PROJECT_ROOT / "pyproject.toml").is_file()
    channel = launcher.channel if launcher else ("editable" if editable else "unknown")
    data = {
        "channel": channel,
        "launcher": str(launcher.path) if launcher else None,
        "editable": editable,
    }
    if launcher is None:
        return Check(
            "install", WARN, f"no stable launcher found ({error}); best guess: {channel}", data
        )
    note = ", editable checkout" if editable else ""
    return Check("install", OK, f"{channel}{note}: {launcher.path}", data)


def _config_check(layers: ConfigLayers, strict_error: str | None) -> Check:
    legacy = config.env_file_override()
    files = [
        layer.source.removeprefix("file:")
        for layer in layers.layers
        if layer.source.startswith("file:")
    ]
    saved = config.settings_file()
    if legacy:
        mode = "legacy"
    elif config_files.is_file(saved):
        mode = "saved"
    elif any(Path(f).name == ".env" for f in files):
        mode = "env"
    else:
        mode = "defaults"
    findings = [{"source": f.source, "message": f.message} for f in layers.findings]
    data = {"mode": mode, "files": files, "valid": strict_error is None, "findings": findings}
    if strict_error is not None:
        return Check("config", FAIL, f"improve would stop with: {strict_error}", data)
    if findings:
        return Check("config", WARN, f"{len(findings)} finding(s); run `config validate`", data)
    where = ", ".join(files) or "built-in defaults"
    return Check("config", OK, f"valid ({mode}: {where})", data)


def deployed_triggers(espanso_dir: Path) -> tuple[list[assets.Trigger], bool]:
    """The triggers in the managed match files Espanso has, and True; when none is deployed,
    the ones a deploy would write, and False. Each a regular file, never a link followed."""
    found: list[assets.Trigger] = []
    deployed = False
    for name in assets.match_names():
        path = espanso_dir / "match" / name
        try:
            if path.is_symlink() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        deployed = True
        found.extend(assets.triggers_in(text, name))
    return (found, True) if deployed else (assets.triggers(), False)


def _rewrites(triggers: list[assets.Trigger], settings: config.Settings) -> dict[str, list[str]]:
    """Each provider the active rewrite triggers run on, with those triggers, in file order;
    a trigger naming no provider runs on PROMPT_PROVIDER."""
    found: dict[str, list[str]] = {}
    for t in triggers:
        if t.active and t.command == "improve":
            found.setdefault(t.provider or settings.provider, []).append(t.trigger)
    return found


def _keys_check(
    layers: ConfigLayers,
    settings: config.Settings,
    triggers: list[assets.Trigger],
    deployed: bool,
) -> Check:
    """Named by the triggers that need each missing key (#217): the deployed ones, else those a
    deploy would write. PROMPT_PROVIDER only counts for a bare `improve` and the Try tab."""
    keys = {}
    for name in secret_names():
        entry = layers.entries[name]
        keys[name] = {"set": bool(entry.value), "source": entry.source if entry.value else None}
    routes = {r.name: r for r in factory.routes(settings)}
    missing: dict[str, list[str]] = {}
    problems = []
    for provider, names in _rewrites(triggers, settings).items():
        route = routes.get(provider)
        # A refused provider (PROMPT_LOCAL_ONLY) never needs its key.
        if route is None or route.refused or not route.key or keys[route.key]["set"]:
            continue
        missing[route.key] = names
        label = factory.PROVIDER_LABELS[provider]
        if deployed:
            problems.append(f"{', '.join(names)} use {label}, but {route.key} is not set")
        else:
            problems.append(
                f"{', '.join(names)} will use {label} once deployed, but {route.key} is not set"
            )
    data = {"keys": keys, "provider": settings.provider, "triggers": missing}
    if problems:
        return Check("keys", FAIL if deployed else WARN, "; ".join(problems), data)
    bare = routes.get(settings.provider)
    if bare and not bare.refused and bare.key and not keys[bare.key]["set"]:
        return Check(
            "keys",
            WARN,
            f"PROMPT_PROVIDER (a bare `promptmend improve`, the Try tab) is {settings.provider}, "
            f"but {bare.key} is not set",
            data,
        )
    shown = ", ".join(f"{n}: {'set' if k['set'] else 'not set'}" for n, k in keys.items())
    return Check("keys", OK, shown, data)


def _local_servers_check(
    settings: config.Settings, triggers: list[assets.Trigger], deployed: bool
) -> Check:
    """Whether the Ollama and LM Studio the local triggers need answer (#220). The triggers
    stay deployed either way; this says what typing one would need. Never fails or warns."""
    used = _rewrites(triggers, settings)
    servers: dict[str, dict[str, Any]] = {}
    notes = []
    for provider, base_url in (
        ("ollama", settings.ollama_base_url),
        ("lmstudio", settings.lmstudio_base_url),
    ):
        names = used.get(provider)
        if not names:
            continue
        answers = local_probe.answers(base_url.rstrip("/") + _PROBE_PATHS[provider])
        servers[provider] = {"answers": answers, "triggers": names}
        label = factory.PROVIDER_LABELS[provider]
        setting = f"{provider.upper()}_BASE_URL"
        if answers is None:
            notes.append(f"{label} ({', '.join(names)}) is not on this machine; not asked")
        elif answers:
            notes.append(f"{label} answers ({', '.join(names)})")
        else:
            notes.append(f"{', '.join(names)} needs {label}: no answer at {setting}")
    data = {"servers": servers, "deployed": deployed}
    if not notes:
        return Check("local_servers", INFO, "no trigger uses Ollama or LM Studio", data)
    return Check("local_servers", INFO, "; ".join(notes), data)


def persona_findings(settings: config.Settings) -> list[str]:
    """What the gate's scan finds in PROMPT_PERSONA: finding names only, never the text. The
    gate scans the draft, but every call also sends the persona in the system prompt (#29), so
    `config validate`, this check and the interface scan it once instead: a match warns and
    never blocks a trigger, which never runs this. No ``bare_token``: that rule is for a
    whole draft that is one secret-like word (a password left on the clipboard); a persona is
    a typed setting, a one-word one (DataOps2Lead) is a role, and a pasted key still matches
    the key patterns, as `config set` checks every value with the same scan()."""
    from .redaction import compile_extra, scan

    return scan(settings.persona, compile_extra(settings.extra_patterns))


def _persona_stays_local(settings: config.Settings) -> bool:
    """PROMPT_LOCAL_ONLY makes make_provider() refuse every provider that leaves the machine,
    unless PROMPT_GATE_LOCAL says a loopback server may relay to a cloud API."""
    return settings.local_only and not settings.gate_local


def _persona_used(settings: config.Settings) -> bool:
    """Whether PROMPT_PROFILE or PROMPT_PRO_PROFILE sends the persona: its template (built-in
    or opted-in user file) holds {{PERSONA_RULE}}. A profile that cannot load sends nothing
    (improve stops; the profiles check reports it). The triggers' only explicit --profile is
    `general`, which has no persona."""
    from .prompt_builder import PERSONA_TOKEN, template

    for name in dict.fromkeys(filter(None, (settings.profile, settings.pro_profile))):
        try:
            if PERSONA_TOKEN in template(name, settings.profile_overrides):
                return True
        except ValueError:
            continue
    return False


def persona_problem(settings: config.Settings) -> str | None:
    """`config validate`'s problem for a persona the gate's patterns match, labels only."""
    findings = persona_findings(settings)
    if not findings or _persona_stays_local(settings) or not _persona_used(settings):
        return None
    # With PROMPT_LOCAL_ONLY no cloud call is made: only a relaying loopback server sends it on.
    sent = (
        "with every call through a local relay (PROMPT_GATE_LOCAL=true)"
        if settings.local_only
        else "with every cloud call"
    )
    return (
        f"PROMPT_PERSONA matches the data-protection patterns: {', '.join(findings)}; "
        f"it is sent unscanned {sent}"
    )


def _persona_readable() -> bool:
    """False when a problem can change PROMPT_PERSONA (its own rejected value, a merged line,
    an unreadable settings file): then the persona subcommand prints its placeholder, and
    repair mode's fallback value says nothing about the one the user wrote."""
    try:
        ConfigLayers.resolve(only="PROMPT_PERSONA")
    except ValueError:
        return False
    return True


def _persona_check(settings: config.Settings, readable: bool = True) -> Check:
    findings = persona_findings(settings)
    data = {"set": bool(settings.persona), "local_only": settings.local_only, "findings": findings}
    if not readable:
        return Check("persona", INFO, "PROMPT_PERSONA could not be read; see config", data)
    if not settings.persona:
        return Check("persona", OK, "PROMPT_PERSONA is not set", data)
    problem = persona_problem(settings)
    if problem:
        return Check("persona", WARN, problem, data)
    if findings:
        why = "not sent (PROMPT_LOCAL_ONLY=true)"
        if not _persona_used(settings):
            why = "not used by the configured profiles"
        return Check("persona", INFO, f"matches {', '.join(findings)}; {why}", data)
    return Check("persona", OK, "PROMPT_PERSONA matches no data-protection pattern", data)


def _espanso_check(runner: deploy.Runner, espanso_dir: Path | None) -> tuple[Check, Path]:
    answer = runner(deploy.PATH_CONFIG)
    out, why = deploy.output(answer), deploy.failure(answer)
    located = Path(out.strip()) if out and out.strip() else None
    # Found means on PATH: an installed Espanso that cannot answer is not a PATH problem (#115).
    found = why is None or why.found
    running = None
    if found:
        # `espanso status` exits non-zero when Espanso is not running (no output then).
        status = deploy.output(runner(["espanso", "status"]))
        running = status is not None and "not running" not in status.lower()
    target = espanso_dir or located or deploy.default_espanso_dir()
    data = {
        "found": found,
        "config_dir": str(target),
        "running": running,
        "query_failed": why is not None and why.found,
        "exit_code": why.returncode if why else None,
        "timed_out": why is not None and why.timed_out,
        "error": why.error if why else None,
    }
    if why is not None and not why.found:
        return Check("espanso", WARN, why.describe(deploy.PATH_CONFIG), data), target
    if why is not None:
        where = f" at {why.path}" if why.path else ""
        hint = "" if why.timed_out else "; start Espanso once (`espanso start`) or check its config"
        message = f"espanso found{where}, but {why.describe(deploy.PATH_CONFIG)}{hint}"
        if espanso_dir is None:
            message += f" (using the default folder {target})"
        return Check("espanso", WARN, message, data), target
    if not running:
        return Check("espanso", WARN, f"Espanso is not running (config: {target})", data), target
    return Check("espanso", OK, f"running (config: {target})", data), target


def _match_check(target: Path, launcher: str | None, manifest: deploy.Manifest | None) -> Check:
    data: dict[str, Any] = {"espanso_dir": str(target), "files": [], "legacy": False}
    if launcher is None or manifest is None:
        why = "the deploy manifest cannot be read" if manifest is None else "no launcher found"
        return Check("match_files", WARN, f"cannot compare: {why} (pass --launcher)", data)
    if not (target / "match").is_dir():
        return Check("match_files", WARN, f"no Espanso match folder in {target}", data)
    the_plan = deploy.plan(target, deploy.launcher_text(launcher), manifest)
    data["files"] = [{"name": s.name, "state": s.state} for s in the_plan.steps]
    data["legacy"] = the_plan.legacy is not None
    broken = [s for s in the_plan.steps if s.state in (deploy.STALE, deploy.MISSING)]
    edited = [s for s in the_plan.steps if s.state in (deploy.MODIFIED, deploy.FOREIGN)]
    if broken:
        names = ", ".join(f"{s.name}: {s.state}" for s in broken)
        return Check("match_files", FAIL, f"{names}; run `promptmend espanso deploy`", data)
    if edited or the_plan.legacy is not None:
        return Check("match_files", WARN, "some files are not ours as deployed", data)
    return Check("match_files", OK, "every match file is in sync", data)


def _same_launcher(deployed: str, current: str | None) -> bool:
    """The same path however it is spelled: the match files hold Windows paths with `/` (#212),
    and Windows paths ignore case. A launcher never holds a backslash elsewhere."""
    if current is None:
        return False

    def norm(path: str) -> str:
        return os.path.normcase(os.path.normpath(path.replace("\\", "/")))

    return norm(deployed) == norm(current)


def _launcher_check(current: str | None, manifest: deploy.Manifest | None) -> Check:
    # An entry whose file is gone (a deleted folder, another Espanso config folder) calls
    # nothing, so only live entries are judged; the next deploy forgets the others.
    entries = list(manifest.entries.values()) if manifest else []
    orphans = sorted(e.target for e in entries if deploy.is_gone(e))
    deployed = sorted({e.launcher for e in entries if not deploy.is_gone(e)})
    missing = [p for p in deployed if not Path(p).is_file()]
    drift = bool(current) and any(not _same_launcher(p, current) for p in deployed)
    data = {
        "current": current,
        "deployed": deployed,
        "drift": drift,
        "missing": missing,
        "orphans": orphans,
    }
    note = (
        f"; {len(orphans)} manifest entries for files that no longer exist "
        "(`promptmend espanso deploy` forgets them)"
        if orphans
        else ""
    )
    if manifest is None:
        return Check("launcher", WARN, "the deploy manifest cannot be read", data)
    if not deployed:
        return Check("launcher", INFO, f"nothing deployed by promptmend on record{note}", data)
    if missing:
        return Check(
            "launcher",
            FAIL,
            f"the deployed matches call {', '.join(missing)}, which is gone{note}",
            data,
        )
    legacy = [p for p in deployed if deploy.is_legacy_launcher(p)]
    if legacy:
        return Check(
            "launcher",
            WARN,
            f"the deployed matches call {', '.join(legacy)}, the deprecated `prompt-workflow` "
            f"command (removed in 1.0.0): redeploy with `promptmend espanso deploy`{note}",
            data,
        )
    if drift:
        return Check(
            "launcher",
            WARN,
            f"the deployed matches call {', '.join(deployed)}, this install is {current}{note}",
            data,
        )
    return Check("launcher", OK, f"the deployed matches call {', '.join(deployed)}{note}", data)


def _history_problem(health: Health) -> str:
    """What is wrong with the history, why, and what to do about it."""
    if health.lost_writes:
        problem = (
            f"tracking incomplete: {health.lost_writes} write(s) lost, the last at "
            f"{health.last_lost_utc or 'an unknown time'}. A write that takes longer than its "
            "time budget (a slow first write, or antivirus or endpoint scanning of the data "
            "folder) now waits in history.spool for the next call or `promptmend stats`; "
            "`promptmend history reset` clears the count"
        )
    elif not health.writable:
        problem = "tracking incomplete: not writable; check the data folder's permissions"
    elif health.tracking_incomplete:
        problem = "tracking incomplete: history.lost or history.spool cannot be read"
    else:
        problem = ""
    if health.error:
        problem = f"{health.error}{'; ' + problem if problem else ''}"
    if health.spooled:
        waiting = (
            f"{health.spooled} record(s) waiting in history.spool: run `promptmend stats` "
            "to store them, or `promptmend history reset` if the database is corrupt"
        )
        problem = f"{problem}; {waiting}" if problem else waiting
    return problem


def _history_checks(settings: config.Settings) -> tuple[Check, Check]:
    from .history import HistoryStore, wal_is_safe

    store = HistoryStore.from_settings(settings)
    if settings.history:
        # Stores the records a write that missed its time budget left in the spool (#213).
        store.replay()
    health = store.health()
    data = {"enabled": settings.history, **vars(health)}
    data.pop("sqlite_version")
    journal = data.pop("journal_mode")
    if not settings.history:
        history = Check("history", INFO, "off (PROMPT_HISTORY=false)", data)
    elif health.error or health.tracking_incomplete or health.spooled:
        history = Check("history", WARN, f"{_history_problem(health)} ({health.path})", data)
    elif not health.exists:
        history = Check(
            "history", OK, f"no calls yet ({health.path} is created by the first)", data
        )
    else:
        count = health.operations if health.operations is not None else 0
        history = Check("history", OK, f"{count} call(s) recorded in {health.path}", data)
    version = health.sqlite_version
    safe = wal_is_safe(version)
    # wal_reset_bug: the library is older than history.WAL_FIXED (3.51.3).
    old = safe is False
    sqlite_data = {"version": version, "wal_reset_bug": old, "journal_mode": journal}
    if safe is None:
        sqlite = Check("sqlite", INFO, f"SQLite version {version}", sqlite_data)
    elif not old:
        sqlite = Check("sqlite", OK, f"SQLite {version}", sqlite_data)
    elif journal == "wal":
        sqlite = Check(
            "sqlite",
            WARN,
            f"SQLite {version}: history still in WAL, which can rarely reset a database "
            "before 3.51.3; the next recorded run switches it back",
            sqlite_data,
        )
    else:
        sqlite = Check("sqlite", OK, f"SQLite {version}, rollback journal", sqlite_data)
    return history, sqlite


def _clipboard_check(read: bool) -> Check:
    data: dict[str, Any] = {"read": False, "length": None, "concealed": None, "error": None}
    if not read:
        return Check("clipboard", INFO, "skipped (--no-clipboard)", data)
    import pyperclip

    from .clipboard_guard import is_concealed

    concealed = is_concealed()
    data["concealed"] = concealed
    if concealed:
        return Check("clipboard", OK, "holds a password-manager item; not read", data)
    try:
        length = len(str(pyperclip.paste()))
    except pyperclip.PyperclipException as exc:
        data["error"] = type(exc).__name__
        return Check("clipboard", WARN, f"cannot be read ({type(exc).__name__})", data)
    data.update(read=True, length=length)
    return Check("clipboard", OK, f"readable: {length} character(s) (content not shown)", data)


def _profiles_check(settings: config.Settings) -> Check:
    from .prompt_builder import ADDED, OVERRIDES, SHADOWED, system_prompt, user_profiles

    found = user_profiles(settings.profile_overrides)
    data: dict[str, Any] = {
        "profile": settings.profile,
        "pro_profile": settings.pro_profile,
        "user": [{"name": p.name, "status": p.status} for p in found],
    }
    for name in dict.fromkeys(filter(None, (settings.profile, settings.pro_profile))):
        try:
            system_prompt(name, "", settings.profile_overrides)
        except ValueError as exc:
            return Check("profiles", FAIL, str(exc), data)
    odd = [f"{p.name}: {p.status}" for p in found if p.status not in (ADDED, OVERRIDES)]
    if odd:
        from .profiles import overrides_hint

        shadowed = [p.name for p in found if p.status == SHADOWED]
        hint = overrides_hint(shadowed, settings.profile_overrides)
        return Check("profiles", WARN, "; ".join(odd + ([hint] if hint else [])), data)
    return Check("profiles", OK, f"PROMPT_PROFILE {settings.profile} resolves", data)


def _previous_install_check(
    runner: deploy.Runner, espanso_dir: Path, launcher: str | None
) -> Check:
    from . import previous_install

    try:
        found = previous_install.detect(
            runner=runner,
            espanso_dir=espanso_dir,
            launcher=Path(launcher) if launcher else None,
            look_up_launcher=False,  # run() already looked: ``launcher`` is its answer
        )
    except Exception as exc:
        # Information only: a folder it cannot look at never fails the report.
        message = f"could not look for a previous install: {type(exc).__name__}"
        return Check("previous_install", WARN, message)
    first = found.candidates[0] if found.candidates else None
    pending = found.pending
    retire = pending is not None and not pending.launchers_in_root
    data = {
        "gated": found.gated,
        "roots": [str(c.root) for c in found.candidates],
        "signals": sorted(first.signals) if first else [],
        "env_file": str(first.env_file) if first and first.env_file else None,
        "retire_pending": str(pending.env_file) if pending and retire else None,
        "shadow": found.shadow.path if found.shadow else None,
    }
    found_at = (
        f"a previous install was found at {', '.join(str(c.root) for c in found.candidates)} "
        "and is not migrated"
    )
    notes = [found_at] if first else []
    if pending and retire:
        notes.append(
            f"the old .env at {pending.env_file} is still in place "
            f"(`promptmend config retire --from {pending.root}`)"
        )
    elif pending:
        notes.append(
            f"the settings of {pending.root} were copied, but the match files still run "
            f"{', '.join(pending.launchers_in_root)} (`promptmend espanso deploy`)"
        )
    if found.shadow:
        notes.append(
            f"`{found.shadow.command}` on PATH is {found.shadow.path}, not the installed launcher "
            f"{found.shadow.launcher}: {found.shadow.hint}"
        )
    if found.shadow or (pending and retire):
        return Check("previous_install", WARN, "; ".join(notes), data)
    if notes:
        return Check("previous_install", INFO, "; ".join(notes), data)
    if found.gated:
        return Check("previous_install", OK, "settings are in place; nothing to look for", data)
    return Check("previous_install", OK, "no previous install found", data)


def _folders_check() -> Check:
    """The user folders in use (#169). Doctor runs after the move (cli._LazyGroup), so a legacy
    folder still in use means the move failed, and entries left in one are conflicts: the new
    folder has its own, and nothing was overwritten."""
    legacy: list[str] = []
    conflicts: list[str] = []
    notes: list[str] = []
    for new, old in (config.config_folders(), config.data_folders()):
        in_use = config.folder_in_use((new, old)) == old
        if in_use:
            legacy.append(str(old))
        variable = config.env_file_inside(old) if old.is_dir() else None
        if old.is_symlink():
            notes.append(f"{old} is a symlink, which is never moved: move it to {new} by hand")
        elif variable:
            notes.append(
                f"{variable} names a file inside {old}, so it is not moved: point {variable} "
                f"at the same file under {new}, then run any command"
            )
        elif in_use and os.path.lexists(new):
            notes.append(f"{new} is not a folder, so {old} stays in use: move {new} away")
        elif in_use:
            notes.append(f"{old} is still in use under the old name (the move failed)")
        elif old.is_dir():
            conflicts.extend(str(entry) for entry in sorted(old.iterdir()))
    data = {
        "config_dir": str(config._user_config_dir()),
        "data_dir": str(config.user_data_dir()),
        "legacy": legacy,
        "conflicts": conflicts,
    }
    if conflicts:
        notes.append(
            f"left in the old folder, since the new one has its own: {', '.join(conflicts)}; "
            "merge or remove them by hand"
        )
    if notes:
        return Check("folders", WARN, "; ".join(notes), data)
    return Check(
        "folders", OK, f"settings in {data['config_dir']}, data in {data['data_dir']}", data
    )


def _safely(check_id: str, build: Callable[[], Check]) -> Check:
    """One check that fails on its own never stops the report."""
    try:
        return build()
    except Exception as exc:
        return Check(check_id, FAIL, f"check failed: {type(exc).__name__}: {exc}")


def run(
    *,
    espanso_dir: Path | None = None,
    launcher: str | None = None,
    clipboard: bool = True,
    runner: deploy.Runner | None = None,
    update: update_check.UpdateStatus | None = None,
) -> Report:
    """Every check in CHECK_IDS. ``launcher`` and ``espanso_dir`` override the lookups;
    ``update`` is an update check already made (the interface's)."""
    run_command = runner or deploy.run_command
    layers = ConfigLayers.resolve(strict=False)
    settings = layers.settings()
    if update is None:
        update = update_check.check_configured()
    try:
        ConfigLayers.resolve().settings()
        strict_error = None
    except ValueError as exc:
        strict_error = str(exc)

    found: deploy.Launcher | None = None
    launcher_error = None
    try:
        found = deploy.resolve_launcher(runner=run_command)
    except deploy.DeployError as exc:
        launcher_error = str(exc)
    current = launcher or (str(found.path) if found else None)
    try:
        manifest: deploy.Manifest | None = deploy.Manifest.load()
    except deploy.DeployError:
        manifest = None
    compare = current
    if compare is None and manifest and manifest.entries:
        # Compare with what was deployed, so a missing launcher still shows each file's state;
        # an entry whose file is gone says nothing about the files there now.
        live = [e for e in manifest.entries.values() if not deploy.is_gone(e)]
        compare = min(e.launcher for e in live or manifest.entries.values())

    # The version check names the install channel's update command, so it is built after the
    # install check; the report keeps CHECK_IDS order.
    install = _safely("install", lambda: _install_check(found, launcher_error))
    checks: list[Check] = [
        _safely("version", lambda: _version_check(update, install)),
        _safely("cli", _cli_check),
        install,
        _safely("config", lambda: _config_check(layers, strict_error)),
    ]
    try:
        espanso, target = _espanso_check(run_command, espanso_dir)
    except Exception as exc:
        espanso = Check("espanso", FAIL, f"check failed: {type(exc).__name__}")
        target = espanso_dir or deploy.default_espanso_dir()
    try:
        triggers, deployed = deployed_triggers(target)
    except Exception:
        triggers, deployed = [], False
    checks += [
        _safely("keys", lambda: _keys_check(layers, settings, triggers, deployed)),
        _safely("persona", lambda: _persona_check(settings, _persona_readable())),
        espanso,
    ]
    checks.append(_safely("match_files", lambda: _match_check(target, compare, manifest)))
    checks.append(_safely("launcher", lambda: _launcher_check(current, manifest)))
    try:
        checks.extend(_history_checks(settings))
    except Exception as exc:
        checks.append(Check("history", FAIL, f"check failed: {type(exc).__name__}"))
        checks.append(Check("sqlite", WARN, "unknown"))
    checks.append(_safely("clipboard", lambda: _clipboard_check(clipboard)))
    checks.append(_safely("profiles", lambda: _profiles_check(settings)))
    checks.append(
        _safely("previous_install", lambda: _previous_install_check(run_command, target, current))
    )
    checks.append(_safely("folders", _folders_check))
    checks.append(
        _safely("local_servers", lambda: _local_servers_check(settings, triggers, deployed))
    )
    return Report(tuple(checks))
