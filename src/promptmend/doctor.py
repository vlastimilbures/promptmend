"""The `doctor` service: one report of everything a broken trigger can come from.

Read-only: it runs only `espanso path config`, `espanso status` and the launcher lookup
(uv's receipt, else `uv tool dir`; `brew --prefix`) through deploy.run_command, reads the
settings in repair mode, asks PyPI for the newest release at most once a day (update_check,
unless PROMPT_UPDATE_CHECK is false), asks a loopback Ollama and LM Studio at the same time
whether they answer (local_probe), builds (never uses) an https context to report what a
certificate is checked against (tls), reads the deployed match files, and reads the clipboard
only to report its length. The report never holds a key, the persona or clipboard text,
since people paste it into bug reports. Its JSON shape is stable (SCHEMA_VERSION): ids and
keys are only ever added (the one exception: the previous_install check, removed by owner
decision, #246; its PATH warning lives on as path_shadow). verdict() sums the report up in
the one line doctor's text ends with (#402), which the JSON carries as ``verdict``.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import (
    assets,
    config,
    config_files,
    deploy,
    factory,
    installed_version,
    local_probe,
    parsers,
    paths,
    tls,
    update_check,
)
from .config import ConfigLayers, secret_names
from .providers.registry import PROVIDERS
from .settings_checks import (
    gate_packs_note,
    packs_off,
    persona_findings,
    persona_problem,
    persona_used,
    rewrites,
)

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
    "path_shadow",
    "config",
    "keys",
    "pro_provider",
    "persona",
    "gate_packs",
    "espanso",
    "match_files",
    "launcher",
    "history",
    "sqlite",
    "clipboard",
    "profiles",
    "folders",
    "local_servers",
    "tls",
)
# Every key of each check's data, in every report: a check that could not run has them all
# as None, so a consumer never meets a missing key.
DATA_KEYS = {
    "version": ("version", "python", "platform", "latest", "update_available", "checked_at"),
    "cli": ("path", "executable", "frozen"),
    "install": ("channel", "launcher", "editable"),
    "path_shadow": ("command", "found", "launcher"),
    "config": ("mode", "files", "valid", "findings"),
    "keys": ("keys", "provider", "triggers"),
    "pro_provider": ("pro_provider", "deployed", "stale"),
    "persona": ("set", "local_only", "findings"),
    "gate_packs": ("packs", "off"),
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
    "folders": ("config_dir", "data_dir", "legacy", "conflicts"),
    "local_servers": ("servers", "deployed", "slow_localhost"),
    "tls": ("verified_against", "ca_variable"),
}


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
            "version": installed_version(),
            "status": self.status,
            "checks": {
                c.id: {
                    "status": c.status,
                    "message": c.message,
                    "data": {k: c.data.get(k) for k in DATA_KEYS[c.id]},
                }
                for c in self.checks
            },
            # Added in #402, so schema_version stays 1.
            "verdict": verdict(self).to_json(),
        }


SETUP_COMMAND = "promptmend setup"


@dataclass(frozen=True)
class Verdict:
    """The report in one line (#402): how many problems (failed checks) and warnings, and
    what to do first. ``first`` is the check the line points at; ``setup`` is True when it
    points at `promptmend setup` instead. The line quotes only check ids and messages, which
    are safe to paste already."""

    problems: int
    warnings: int
    first: str | None
    setup: bool
    line: str

    def to_json(self) -> dict[str, Any]:
        return {
            "problems": self.problems,
            "warnings": self.warnings,
            "first": self.first,
            "setup": self.setup,
            "line": self.line,
        }


# The checks whose fail or warning `promptmend setup` deals with on a machine with no match
# files deployed: it saves the key and deploys the match files (which the launcher check
# reads). pro_provider is not one: it warns only about a deployed file. match_files counts
# only when it failed (files missing in a folder that exists): its warnings (no Espanso
# folder, cannot compare, files that are not ours) are ones a setup cannot change.
SETUP_CHECKS = ("keys", "match_files", "launcher")


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _one_line(text: str) -> str:
    """``text`` with every run of whitespace (a newline in an exception's text) one space."""
    return " ".join(text.split())


def _not_set_up(report: Report) -> str | None:
    """What is not set up yet, or None. It hinges on the match files: none managed in
    Espanso's folder (deployed_triggers(), as pro_provider reports it). A setup with the
    default answers writes no config.toml, so a missing one alone means nothing; only with no
    config.toml, no PROMPTMEND_ENV (config mode ``defaults``) and no key set either is nothing
    set up. A check that could not run says neither."""
    data = {c.id: c.data for c in report.checks}
    if data.get("pro_provider", {}).get("deployed") is not False:
        return None
    keys = data.get("keys", {}).get("keys") or {}
    any_key = any(isinstance(k, Mapping) and k.get("set") for k in keys.values())
    if data.get("config", {}).get("mode") == "defaults" and not any_key:
        return "nothing is set up yet"
    return "no match files are deployed yet"


def _points_at_setup(check: Check) -> bool:
    """Whether setup fixes this wrong check (SETUP_CHECKS; match_files only when it failed)."""
    return check.id in SETUP_CHECKS and (check.id != "match_files" or check.status == FAIL)


def verdict(report: Report) -> Verdict:
    """What doctor's text output ends with, and doctor --json's ``verdict``: always one line.

    Problems are the failed checks, the ones that make doctor exit 4; warnings are counted
    beside them and point the line only when nothing failed. "First" is the first such check
    in report order (CHECK_IDS), which already runs install, settings, keys, Espanso, match
    files, launcher. On a machine with no match files deployed (_not_set_up()) where a check
    setup deals with is wrong (_points_at_setup()), the line names `promptmend setup` instead;
    unless a check outside setup's reach failed (the settings, the profiles, the history):
    then the first of those comes first, and setup on the next run. Warnings outside its
    reach never hold setup back."""
    fails = [c for c in report.checks if c.status == FAIL]
    warns = [c for c in report.checks if c.status == WARN]
    if not fails and not warns:
        return Verdict(0, 0, None, False, "No problems found.")
    parts = []
    if fails:
        parts.append(_count(len(fails), "problem"))
    if warns:
        parts.append(_count(len(warns), "warning"))
    counts = ", ".join(parts)
    candidates = fails
    missing = _not_set_up(report)
    if missing:
        beyond = [c for c in fails if c.id not in SETUP_CHECKS]
        if beyond:
            candidates = beyond
        elif any(_points_at_setup(c) for c in fails + warns):
            line = f"{counts}: {missing}. Next: `{SETUP_COMMAND}`"
            return Verdict(len(fails), len(warns), None, True, line)
    if candidates:
        first = candidates[0]
        line = f"{counts}. Fix first: {first.id}: {_one_line(first.message)}"
    else:
        first = warns[0]
        line = f"No problems, {counts}. Check first: {first.id}: {_one_line(first.message)}"
    return Verdict(len(fails), len(warns), first.id, False, line)


# uv's `tool install --force` removes the old tool before it deletes its folder, so on
# Windows a promptmend still running from it (the interface itself) leaves none (#356).
CLOSE_FIRST = "close the interface and any running promptmend, then "
_CLOSE_FIRST_CHANNELS = ("uv", "editable")


def upgrade_hint(install: Mapping[str, Any], plat: str = sys.platform) -> str:
    """The update command for the install check's data: a checkout's own wins over the
    launcher's channel, since an editable tool sits in uv's bin folder too. On Windows a
    uv-backed one starts with CLOSE_FIRST, ahead of the command, which stays whole."""
    channel = "editable" if install.get("editable") else str(install.get("channel") or "")
    command = update_check.upgrade_command(channel, plat)
    if plat == "win32" and channel in _CLOSE_FIRST_CHANNELS:
        return CLOSE_FIRST + command
    return command


def _version_check(update: update_check.UpdateStatus, install: Check | None) -> Check:
    """The installed version and whether PyPI has a newer one: information only, never
    a warning, with the update command for doctor's install channel (a checkout's own)."""
    available = update.state == update_check.AVAILABLE
    known = update.state in (update_check.AVAILABLE, update_check.LATEST)
    data = {
        "version": installed_version(),
        "python": platform.python_version(),
        "platform": sys.platform,
        "latest": update.latest,
        "update_available": available if known else None,
        "checked_at": update.checked_at,
    }
    message = f"promptmend {installed_version()}"
    if available:
        message += f", {update.latest} available: {upgrade_hint(install.data if install else {})}"
    return Check("version", INFO, message, data)


def with_update(report: Report, update: update_check.UpdateStatus) -> Report:
    """``report`` with its version check made again for ``update``: the interface's update
    check, when it lands after the rest."""
    install = next((c for c in report.checks if c.id == "install"), None)
    return Report(
        tuple(_version_check(update, install) if c.id == "version" else c for c in report.checks)
    )


def _cli_check() -> Check:
    script = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if sys.platform == "win32" and script and not script.suffix:
        exe = script.with_name(script.name + ".exe")
        # Windows starts the console script's launcher, but argv[0] drops its `.exe`.
        if exe.is_file():
            script = exe
    frozen = bool(getattr(sys, "frozen", False))
    data = {
        "path": str(script) if script else None,
        "executable": sys.executable,
        # The Windows zip's own exe: no interpreter, no import check.
        "frozen": frozen,
    }
    note = " (frozen build)" if frozen else ""
    return Check("cli", INFO, f"running {script or sys.executable}{note}", data)


def _install_check(launcher: deploy.Launcher | None, error: str | None) -> Check:
    editable = (paths._PROJECT_ROOT / "pyproject.toml").is_file()
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


def _path_shadow_check(
    found: deploy.Launcher | None, environ: Mapping[str, str] = os.environ
) -> Check:
    """Whether `promptmend` first on PATH is this install's launcher: another one makes the
    shell run an older CLI with other settings."""
    if found is None:
        return Check("path_shadow", INFO, "not checked: no launcher found")
    # shortcut: on Windows, which() also searches the cwd first (Python 3.12+); doctor runs
    # from a terminal, so fine unless someone reports a false warning from a project folder.
    where = shutil.which("promptmend", path=environ.get("PATH", ""))
    if not where:
        return Check("path_shadow", INFO, "`promptmend` is not on PATH")
    data = {"command": "promptmend", "found": where, "launcher": str(found.path)}
    try:
        own = os.path.samefile(where, found.path)
    except OSError:
        own = False
    if not own:
        hint = _shadow_hint(environ, Path(where), found.path)
        return Check(
            "path_shadow",
            WARN,
            f"`promptmend` on PATH is {where}, not the installed launcher {found.path}: {hint}",
            data,
        )
    return Check("path_shadow", OK, f"`promptmend` on PATH is the installed launcher {where}", data)


def _shadow_hint(environ: Mapping[str, str], found: Path, launcher: Path) -> str:
    venv = environ.get("VIRTUAL_ENV")
    if venv and deploy.inside(found, Path(venv)):
        return "a virtual environment is active: run `deactivate`"
    if ".venv" in found.parts:
        return f"remove {found.parent} from PATH"
    return f"uninstall the old CLI, or put {launcher.parent} earlier on PATH"


def _config_check(layers: ConfigLayers, strict_error: str | None) -> Check:
    legacy = paths.env_file_override()
    files = [
        layer.source.removeprefix("file:")
        for layer in layers.layers
        if layer.source.startswith("file:")
    ]
    saved = paths.settings_file()
    if legacy:
        mode = "legacy"
    elif config_files.is_file(saved):
        mode = "saved"
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


def stale_pro_pins(deployed: list[assets.Trigger], settings: config.Settings) -> dict[str, str]:
    """Each active deployed pro trigger that still names a provider the shipped one leaves to
    PROMPT_PRO_PROVIDER (-ip-'s `--provider openrouter` before #278), with that provider, when
    PROMPT_PRO_PROVIDER is set away from its default to another one: the setting does not
    reach that trigger until a deploy writes the new file. -if- names OpenRouter as shipped,
    so it never counts."""
    if settings.pro_provider == parsers.DEFAULT_PRO_PROVIDER:
        return {}
    shipped = {t.trigger: t for t in assets.triggers()}
    stale = {}
    for t in deployed:
        ours = shipped.get(t.trigger)
        if (
            t.active
            and t.command == "improve"
            and t.tier == "pro"
            and t.provider in PROVIDERS  # a known name only: file text is never echoed
            and t.provider != settings.pro_provider
            and ours is not None
            and ours.provider is None
        ):
            stale[t.trigger] = str(t.provider)
    return stale


def stale_pro_message(stale: dict[str, str], pro_provider: str) -> str:
    """What doctor and Home say about stale_pro_pins(). OpenRouter is what -ip- named before
    #278, so that pin is an older file; any other was written in by hand."""
    named = ", ".join(
        f"the deployed {name} is from an older release and still names {provider}"
        if provider == "openrouter"
        else f"the deployed {name} names {provider} itself"
        for name, provider in stale.items()
    )
    return (
        f"{named}, so PROMPT_PRO_PROVIDER={pro_provider} does not apply to it; redeploy to "
        "apply it (`promptmend espanso deploy`, which asks first for a file you edited, or "
        "`promptmend espanso deploy --on-conflict ours` to replace it with a backup)"
    )


def _pro_provider_check(
    settings: config.Settings, triggers: list[assets.Trigger], deployed: bool
) -> Check:
    """Whether PROMPT_PRO_PROVIDER reaches the deployed pro trigger (stale_pro_pins())."""
    stale = stale_pro_pins(triggers, settings) if deployed else {}
    data = {"pro_provider": settings.pro_provider, "deployed": deployed, "stale": stale}
    if stale:
        return Check("pro_provider", WARN, stale_pro_message(stale, settings.pro_provider), data)
    return Check("pro_provider", OK, f"the pro tier runs on {settings.pro_provider}", data)


def _keys_check(
    layers: ConfigLayers,
    settings: config.Settings,
    triggers: list[assets.Trigger],
    deployed: bool,
) -> Check:
    """Named by the triggers that need each missing key: the deployed ones, else those a
    deploy would write. PROMPT_PROVIDER only counts for a bare `improve` and the Try tab."""
    keys = {}
    for name in secret_names():
        entry = layers.entries[name]
        keys[name] = {"set": bool(entry.value), "source": entry.source if entry.value else None}
    routes = {r.name: r for r in factory.routes(settings)}
    missing: dict[str, list[str]] = {}
    problems = []
    for provider, names in rewrites(triggers, settings).items():
        route = routes.get(provider)
        # A refused provider (PROMPT_LOCAL_ONLY) never needs its key.
        if route is None or route.refused or not route.key or keys[route.key]["set"]:
            continue
        missing[route.key] = names
        label = factory.PROVIDER_LABELS[provider]
        if deployed:
            use = "uses" if len(names) == 1 else "use"
            problems.append(f"{', '.join(names)} {use} {label}, but {route.key} is not set")
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
    layers: ConfigLayers,
    settings: config.Settings,
    triggers: list[assets.Trigger],
    deployed: bool,
    plat: str = sys.platform,
) -> Check:
    """Whether the Ollama and LM Studio the local triggers need answer. The triggers
    stay deployed either way; this says what typing one would need. Never fails or warns:
    a ``localhost`` URL on Windows still works, so its note (#365) is only advice. A base
    URL Settings refused (#368) is named, never quoted, and not asked: repair mode has
    already put the default in its place, which the triggers would not use (they stop)."""
    used = rewrites(triggers, settings)
    servers: dict[str, dict[str, Any]] = {}
    notes = []
    slow: list[str] = []
    wanted = {
        provider: spec
        for provider, spec in PROVIDERS.items()
        if spec.probe_path is not None and used.get(provider)
    }
    asked = {
        provider: spec.url(settings).rstrip("/") + (spec.probe_path or "")
        for provider, spec in wanted.items()
        if parsers.is_valid_url(spec.url(settings)) and not _refused(layers, spec.url_setting)
    }
    replies = _probe_all(asked)
    for provider, spec in wanted.items():
        names = used[provider]
        if provider not in asked:
            servers[provider] = {"answers": None, "triggers": names}
            notes.append(
                f"{', '.join(names)} needs {spec.label}, but {spec.url_setting} is not a "
                "valid URL, so it is not used and not probed"
            )
            continue
        answers = replies[provider]
        servers[provider] = {"answers": answers, "triggers": names}
        if answers is None:
            notes.append(f"{spec.label} ({', '.join(names)}) is not on this machine; not asked")
        elif answers:
            notes.append(f"{spec.label} answers ({', '.join(names)})")
        else:
            notes.append(f"{', '.join(names)} needs {spec.label}: no answer at {spec.url_setting}")
        if plat == "win32" and _on_localhost(spec.url(settings)):
            slow.append(spec.url_setting)
            entry = layers.entries[spec.url_setting]
            notes.append(_localhost_note(spec.url_setting, entry.value, names, entry.source))
    data = {"servers": servers, "deployed": deployed, "slow_localhost": slow}
    if not notes:
        probed = " or ".join(s.label for s in PROVIDERS.values() if s.probe_path)
        return Check("local_servers", INFO, f"no trigger uses {probed}", data)
    return Check("local_servers", INFO, "; ".join(notes), data)


def _refused(layers: ConfigLayers, name: str) -> bool:
    """Whether repair mode refused a value of setting ``name`` (``Entry.rejected``) and
    fell back to a lower layer, which strict mode (the triggers) would not do."""
    entry = layers.entries.get(name)
    return entry is not None and bool(entry.rejected)


def _on_localhost(url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).hostname == "localhost"
    except ValueError:  # a malformed URL (#368), which Settings refuses
        return False


def _localhost_note(setting: str, url: str, names: list[str], source: str) -> str:
    """Windows tries ``localhost`` as ::1 first and retries a refused connect for about 2 s,
    and Ollama and LM Studio listen on 127.0.0.1 only by default (#365).

    The URL is quoted back only in the default's shape with another host and port: no user
    name, password, query or fragment, the default's path, nothing that looks like a key.
    ``source`` (the setting's layer) says where to change it: only a config.toml value gets
    the `config set` command, which legacy mode refuses and the environment overrides."""
    from urllib.parse import urlsplit

    from .redaction import looks_like_a_key

    where = _set_where(source)
    why = f"on Windows each {', '.join(names)} call may wait about 2 s for ::1 at localhost"
    generic = f"{why}: use 127.0.0.1 for localhost in {setting}"
    if where:
        generic += f", set {where}"
    parts = urlsplit(url)
    default_path = urlsplit(_default(setting)).path
    if (
        "@" in parts.netloc
        or parts.query
        or parts.fragment
        or parts.path.rstrip("/") != default_path.rstrip("/")
        or looks_like_a_key(url)
    ):
        return generic
    try:
        port = parts.port
    except ValueError:
        return generic
    fixed = parts._replace(netloc="127.0.0.1" + (f":{port}" if port else "")).geturl()
    if where:
        return f"{why}: set {setting} to {fixed} {where}"
    return f"{why}: set {setting} to {fixed} (`promptmend config set {setting} {fixed}`)"


def _set_where(source: str) -> str | None:
    """Where a setting from ``source`` is changed when `config set` cannot: the real
    environment, or the .env PROMPTMEND_ENV names (legacy mode). None for config.toml."""
    if source == config.ENV_SOURCE:
        return "in the environment"
    path = source.removeprefix("file:")
    if path != source and path != str(paths.settings_file()):
        return f"in {path}"
    return None


def _default(name: str) -> str:
    """A setting's built-in default, as text."""
    return next(
        str(f.metadata["default"]) for f in config.setting_fields() if f.metadata["env"] == name
    )


def _probe_all(urls: Mapping[str, str]) -> dict[str, bool | None]:
    """local_probe.answers() for each URL, at the same time: each one that answers
    nothing waits out its own timeout, so asking in turn would add them up. Every probe runs to
    its end; a probe that raised raises here, in ``urls`` order, as asking in turn would."""
    if len(urls) < 2:
        return {key: local_probe.answers(url) for key, url in urls.items()}
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(urls), thread_name_prefix="promptmend-probe") as pool:
        futures = {key: pool.submit(local_probe.answers, url) for key, url in urls.items()}
    return {key: future.result() for key, future in futures.items()}


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
        if not persona_used(settings):
            why = "not used by the configured profiles"
        return Check("persona", INFO, f"matches {', '.join(findings)}; {why}", data)
    return Check("persona", OK, "PROMPT_PERSONA matches no data-protection pattern", data)


def _gate_packs_check(settings: config.Settings) -> Check:
    data = {"packs": list(settings.gate_packs), "off": packs_off(settings)}
    note = gate_packs_note(settings)
    if note:
        return Check("gate_packs", INFO, note, data)
    return Check("gate_packs", OK, "every gate pack is on", data)


def _espanso_check(runner: deploy.Runner, espanso_dir: Path | None) -> tuple[Check, Path]:
    answer = runner(deploy.PATH_CONFIG)
    out, why = deploy.output(answer), deploy.failure(answer)
    located = Path(out.strip()) if out and out.strip() else None
    # Found means on PATH: an installed Espanso that cannot answer is not a PATH problem.
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


def _match_check(
    target: Path,
    launcher: str | None,
    manifest: deploy.Manifest | None,
    planned: deploy.Plan | None = None,
) -> Check:
    # "legacy" (the pre-0.9 base.yml, not looked for, #246) stays False for schema_version 1.
    data: dict[str, Any] = {"espanso_dir": str(target), "files": [], "legacy": False}
    if launcher is None or manifest is None:
        why = "the deploy manifest cannot be read" if manifest is None else "no launcher found"
        return Check("match_files", WARN, f"cannot compare: {why} (pass --launcher)", data)
    if not (target / "match").is_dir():
        return Check("match_files", WARN, f"no Espanso match folder in {target}", data)
    text = deploy.launcher_text(launcher)
    # Planned in the resolved folder, as a deploy keys its manifest entries (a linked
    # ~/.config/espanso, or on Windows a case or mapped-drive spelling, would else miss them
    # and report deployed files as foreign); the report keeps the folder as printed.
    folder = deploy.resolved(target)
    # The caller's plan when it is this very comparison (the interface's), else our own.
    same = (
        planned is not None
        and planned.espanso_dir == folder
        and planned.launcher == text
        and planned.manifest is manifest
    )
    the_plan = planned if same and planned is not None else deploy.plan(folder, text, manifest)
    data["files"] = [{"name": s.name, "state": s.state} for s in the_plan.steps]
    broken = [s for s in the_plan.steps if s.state in (deploy.STALE, deploy.MISSING)]
    edited = [s for s in the_plan.steps if s.state in (deploy.MODIFIED, deploy.FOREIGN)]
    if broken:
        names = ", ".join(f"{s.name}: {s.state}" for s in broken)
        return Check("match_files", FAIL, f"{names}; run `promptmend espanso deploy`", data)
    if edited:
        return Check("match_files", WARN, "some files are not ours as deployed", data)
    return Check("match_files", OK, "every match file is in sync", data)


def _same_launcher(deployed: str, current: str | None) -> bool:
    """The same path however it is spelled: the match files hold Windows paths with `/`,
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
        problem = (
            "tracking incomplete: history.lost, history.spool or the history.lost.*.drop "
            "files in the data folder cannot be read"
        )
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
        # Stores the records a write that missed its time budget left in the spool.
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
    from .prompt_builder import (
        ADDED,
        OVERRIDES,
        SHADOWED,
        overrides_hint,
        system_prompt,
        user_profiles,
    )

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
        shadowed = [p.name for p in found if p.status == SHADOWED]
        hint = overrides_hint(shadowed, settings.profile_overrides)
        return Check("profiles", WARN, "; ".join(odd + ([hint] if hint else [])), data)
    return Check("profiles", OK, f"PROMPT_PROFILE {settings.profile} resolves", data)


def _folders_check() -> Check:
    """The user folders. ``legacy`` and ``conflicts`` stay, always empty, for doctor --json
    schema_version 1 (#275): the folders' old names are not looked for."""
    data = {
        "config_dir": str(paths.user_config_dir()),
        "data_dir": str(paths.user_data_dir()),
        "legacy": [],
        "conflicts": [],
    }
    return Check(
        "folders", OK, f"settings in {data['config_dir']}, data in {data['data_dir']}", data
    )


def _tls_check() -> Check:
    """What an https call (a cloud provider, the update check) verifies the server's
    certificate against (#314). The fallback still verifies, so it is information, not a
    warning; it does mean a root installed only in the OS store is not trusted."""
    source, variable = tls.describe()
    data = {"verified_against": source, "ca_variable": variable}
    if variable is not None and tls.missing_bundle(variable):
        # The path is never shown: it can name a user's folders.
        what, other = ("file", "folder") if variable == "SSL_CERT_FILE" else ("folder", "file")
        if tls.bundle_is_other_kind(variable):
            message = f"{variable} names a {other}, not a {what}: https calls will fail"
        else:
            message = f"{variable} names a {what} that does not exist: https calls will fail"
        return Check("tls", WARN, message, data)
    if source == tls.CA_BUNDLE:
        message = f"https certificates checked against the CA bundle {variable} names"
    elif source == tls.OS_STORE:
        message = "https certificates checked against the operating system's trust store"
    else:
        message = (
            "truststore cannot load here: https certificates checked against certifi's "
            "bundle, so a root installed only in the OS trust store is not trusted"
        )
    return Check("tls", OK if source != tls.CERTIFI else INFO, message, data)


@dataclass(frozen=True)
class Lookups:
    """This install's launcher and the deploy manifest, each looked up once, so the interface's
    plan and doctor share them. ``launcher`` is None with ``launcher_error`` saying why;
    ``manifest`` is None when it cannot be read, with ``manifest_error``."""

    launcher: deploy.Launcher | None
    launcher_error: str | None
    manifest: deploy.Manifest | None
    manifest_error: str | None


def lookups(
    runner: deploy.Runner | None = None, durations: dict[str, float] | None = None
) -> Lookups:
    """resolve_launcher() and Manifest.load(), their failures kept. ``durations`` gets the
    first's seconds as install and the second's as launcher (doctor --timing)."""
    spent = durations if durations is not None else {}
    started = time.perf_counter()
    found: deploy.Launcher | None = None
    launcher_error = None
    try:
        found = deploy.resolve_launcher(runner=runner or deploy.run_command)
    except deploy.DeployError as exc:
        launcher_error = str(exc)
    spent["install"] = spent.get("install", 0.0) + time.perf_counter() - started
    started = time.perf_counter()
    manifest: deploy.Manifest | None = None
    manifest_error = None
    try:
        manifest = deploy.Manifest.load()
    except deploy.DeployError as exc:
        manifest_error = str(exc)
    spent["launcher"] = spent.get("launcher", 0.0) + time.perf_counter() - started
    return Lookups(found, launcher_error, manifest, manifest_error)


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
    durations: dict[str, float] | None = None,
    layers: ConfigLayers | None = None,
    found: Lookups | None = None,
    plan: deploy.Plan | None = None,
) -> Report:
    """Every check in CHECK_IDS. ``launcher`` and ``espanso_dir`` override the lookups;
    ``update`` is an update check already made (the interface's). The interface also passes
    what its reload already read: ``layers`` (repair mode, as load_layers() makes them),
    ``found`` (lookups()) and its deploy ``plan``, used for match_files when it compares the
    same folder, launcher and manifest. Without them doctor reads its own, with the same
    report. ``durations``, when given, gets each check's seconds (`doctor --timing`),
    the lookups a check reports counted in it: the update check in version, the launcher
    lookup in install, both settings reads in config, the Espanso queries in espanso; history
    and sqlite share one read (history)."""
    spent = durations if durations is not None else {}
    run_command = runner or deploy.run_command
    # On Windows, `espanso path config` and `status` share one look at the processes.
    from .espanso_windows import one_look

    with one_look():
        return _run(
            espanso_dir=espanso_dir,
            launcher=launcher,
            clipboard=clipboard,
            run_command=run_command,
            update=update,
            spent=spent,
            layers=layers,
            found=found,
            plan=plan,
        )


def _run(
    *,
    espanso_dir: Path | None,
    launcher: str | None,
    clipboard: bool,
    run_command: deploy.Runner,
    update: update_check.UpdateStatus | None,
    spent: dict[str, float],
    layers: ConfigLayers | None,
    found: Lookups | None,
    plan: deploy.Plan | None,
) -> Report:
    def clock(check_id: str, started: float) -> None:
        spent[check_id] = spent.get(check_id, 0.0) + time.perf_counter() - started

    def timed(check_id: str, build: Callable[[], Check]) -> Check:
        started = time.perf_counter()
        check = _safely(check_id, build)
        clock(check_id, started)
        return check

    started = time.perf_counter()
    if layers is None:
        layers = ConfigLayers.resolve(strict=False)
    settings = layers.settings()
    clock("config", started)
    if update is None:
        started = time.perf_counter()
        update = update_check.check_configured()
        clock("version", started)
    started = time.perf_counter()
    try:
        ConfigLayers.resolve().settings()
        strict_error = None
    except ValueError as exc:
        strict_error = str(exc)
    clock("config", started)

    looked = found or lookups(run_command, spent)
    current = launcher or (str(looked.launcher.path) if looked.launcher else None)
    manifest = looked.manifest
    compare = current
    if compare is None and manifest and manifest.entries:
        # Compare with what was deployed, so a missing launcher still shows each file's state;
        # an entry whose file is gone says nothing about the files there now.
        live = [e for e in manifest.entries.values() if not deploy.is_gone(e)]
        compare = min(e.launcher for e in live or manifest.entries.values())

    # The version check names the install channel's update command, so it is built after the
    # install check; the report keeps CHECK_IDS order.
    install = timed("install", lambda: _install_check(looked.launcher, looked.launcher_error))
    checks: list[Check] = [
        timed("version", lambda: _version_check(update, install)),
        timed("cli", _cli_check),
        install,
        timed("path_shadow", lambda: _path_shadow_check(looked.launcher)),
        timed("config", lambda: _config_check(layers, strict_error)),
    ]
    started = time.perf_counter()
    try:
        espanso, target = _espanso_check(run_command, espanso_dir)
    except Exception as exc:
        target = espanso_dir or deploy.default_espanso_dir()
        # The folder the deployed triggers are read from, which the interface reads too.
        data = {"config_dir": str(target)}
        espanso = Check("espanso", FAIL, f"check failed: {type(exc).__name__}", data)
    clock("espanso", started)
    started = time.perf_counter()
    try:
        triggers, deployed = deployed_triggers(target)
    except Exception:
        triggers, deployed = [], False
    clock("keys", started)
    checks += [
        timed("keys", lambda: _keys_check(layers, settings, triggers, deployed)),
        timed("pro_provider", lambda: _pro_provider_check(settings, triggers, deployed)),
        timed("persona", lambda: _persona_check(settings, _persona_readable())),
        timed("gate_packs", lambda: _gate_packs_check(settings)),
        espanso,
    ]
    checks.append(timed("match_files", lambda: _match_check(target, compare, manifest, plan)))
    checks.append(timed("launcher", lambda: _launcher_check(current, manifest)))
    started = time.perf_counter()
    try:
        checks.extend(_history_checks(settings))
    except Exception as exc:
        checks.append(Check("history", FAIL, f"check failed: {type(exc).__name__}"))
        checks.append(Check("sqlite", WARN, "unknown"))
    clock("history", started)
    checks.append(timed("clipboard", lambda: _clipboard_check(clipboard)))
    checks.append(timed("profiles", lambda: _profiles_check(settings)))
    checks.append(timed("folders", _folders_check))
    checks.append(
        timed("local_servers", lambda: _local_servers_check(layers, settings, triggers, deployed))
    )
    checks.append(timed("tls", _tls_check))
    return Report(tuple(checks))
