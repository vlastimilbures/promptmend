"""Saving configuration: validated, atomic writes to config.toml, the secret store, and a
consented migration of today's .env to both, which can be rolled back.

These are the services behind the management commands (#92). Nothing here runs on a trigger,
and nothing here prints: errors are ConfigStoreError or ValueError, whose messages name
settings and files but never repeat a secret.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import config, config_files
from .config import ConfigLayers, env_names, secret_names, setting_fields
from .config_files import CONFIG_VERSION, VERSION_KEY, SecretStoreError
from .redaction import safe_repr

MARKER_FILE = "migration.json"
BACKUP_DIR = "backups"
_FIELDS = {f.metadata["env"]: f for f in setting_fields()}


class ConfigStoreError(Exception):
    """A save, migration or rollback that was refused or failed; the message is safe to show."""


class ReadOnlyConfigError(ConfigStoreError):
    """config.toml has a config_version this version does not know, so it is never written."""


class StaleEditError(ConfigStoreError):
    """config.toml changed between the read and the write. ``changed`` names the settings
    whose value differs (never the values); it is empty when only comments or layout did."""

    def __init__(self, path: Path, changed: tuple[str, ...]) -> None:
        self.changed = changed
        what = ", ".join(changed) if changed else "comments or layout only"
        super().__init__(f"{path.name} changed since it was read ({what}); read it again")


class MigrationError(ConfigStoreError):
    """A migration or rollback that was refused, or undone after it failed."""


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def config_dir(environ: Mapping[str, str] | None = None) -> Path:
    return config._user_config_dir(_env(environ))


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


# --- config.toml --------------------------------------------------------------------------


@dataclass(frozen=True)
class SavedSettings:
    """config.toml as read: its table and the sha256 of its bytes (None when it does not
    exist), which save_settings() checks to refuse a write over an edit made since."""

    path: Path
    table: Mapping[str, Any] = field(repr=False)
    digest: str | None

    @property
    def version(self) -> int | None:
        """The config_version (a missing one is the current version); None when invalid."""
        return config_files.config_version(self.table)

    @property
    def read_only(self) -> bool:
        version = self.version
        return version is None or version > CONFIG_VERSION

    @property
    def values(self) -> dict[str, str]:
        """The settings in the file, as strings, keyed by env var name."""
        known = env_names()
        texts = {k: config_files.scalar_text(v) for k, v in self.table.items() if k in known}
        return {k: v for k, v in texts.items() if v is not None}


def read_settings(environ: Mapping[str, str] | None = None) -> SavedSettings:
    """Read config.toml (an empty snapshot when it does not exist). Raises ConfigFileError
    when it exists but cannot be read or parsed."""
    path = config.settings_file(_env(environ))
    return _snapshot(path)


def _snapshot(path: Path) -> SavedSettings:
    loaded = config_files.load_toml(path)
    if loaded is None:
        return SavedSettings(path, {}, None)
    return SavedSettings(path, loaded[0], loaded[1])


def _toml_value(name: str, raw: str) -> Any:
    """``raw`` validated by the setting's own parser, as the TOML value to save: true/false,
    a number or text. Raises the same ValueError a bad value in a .env does. A value that
    parses to None (an empty PROMPT_TEMPERATURE) is saved as the empty text, since None
    means "remove the setting" to save_settings()."""
    value = config.parse_setting(name, _FIELDS[name].metadata["parse"], raw)
    return raw if value is None else value


def _changed_keys(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[str, ...]:
    known = env_names()
    changed = [k for k in {*old, *new} if old.get(k) != new.get(k)]
    return tuple(sorted(k if k in known else safe_repr(k) for k in changed))


def save_settings(snapshot: SavedSettings, changes: Mapping[str, str | None]) -> SavedSettings:
    """Write ``changes`` (env var name to raw value; None removes the setting, so its
    default applies) into the config.toml that ``snapshot`` read, and return it read again.

    Every value is validated first. A secret is refused: it belongs in the secret store, and
    no secret is ever written to config.toml. Refused too: a file with a newer
    config_version (ReadOnlyConfigError), and a file that changed since ``snapshot`` was
    read (StaleEditError, naming the settings that differ). Settings this version does not
    know are kept; comments are not (tomli-w writes plain TOML).
    """
    if snapshot.read_only:
        raise ReadOnlyConfigError(
            f"{snapshot.path.name} has an unknown {VERSION_KEY}; this version "
            f"({CONFIG_VERSION}) only reads it"
        )
    known, secrets = env_names(), secret_names()
    updates: dict[str, Any] = {}
    for name, raw in changes.items():
        if name not in known:
            raise ValueError(f"{safe_repr(name)} is not a setting")
        if name in secrets:
            raise ValueError(f"{name} is a secret; save it in the secret store, not config.toml")
        updates[name] = None if raw is None else _toml_value(name, raw)
    held = [k for k in snapshot.table if k in secrets]
    if held:
        raise ConfigStoreError(
            f"{', '.join(held)} in {snapshot.path.name} is a secret; move it to the secret "
            "store before saving"
        )
    current = _snapshot(snapshot.path)
    if current.digest != snapshot.digest:
        raise StaleEditError(snapshot.path, _changed_keys(snapshot.table, current.table))

    table = {VERSION_KEY: CONFIG_VERSION}
    table |= {k: v for k, v in snapshot.table.items() if k != VERSION_KEY}
    for name, value in updates.items():
        if value is None:
            table.pop(name, None)
        else:
            table[name] = value
    config_files.write_atomic(snapshot.path, config_files.dump_toml(table), private=False)
    return _snapshot(snapshot.path)


# --- secrets ------------------------------------------------------------------------------


def _secret_name(name: str) -> str:
    if name not in secret_names():
        raise ValueError(f"{safe_repr(name)} is not a secret setting")
    return name


def save_secret(name: str, value: str, environ: Mapping[str, str] | None = None) -> None:
    """Save an API key in the secret store (never config.toml). Raises SecretStoreError when
    the store fails; nothing is then written anywhere else."""
    cleaned = value.strip()
    if not cleaned or any(c.isspace() or not c.isprintable() for c in cleaned):
        raise ValueError(f"{_secret_name(name)} must be one line of visible characters")
    config_files.secret_store(config_dir(environ)).set(_secret_name(name), cleaned)


def delete_secret(name: str, environ: Mapping[str, str] | None = None) -> None:
    config_files.secret_store(config_dir(environ)).delete(_secret_name(name))


def saved_secret_names(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Which secrets the store holds; their values are never returned."""
    saved = config_files.secret_store(config_dir(environ)).read()
    return tuple(name for name in secret_names() if saved.get(name))


# --- migrating .env -----------------------------------------------------------------------

# ready: there is a .env to migrate. nothing: no .env. saved: config.toml already exists (a
# .env is ignored). migrated: the marker says this ran; rollback_* undoes it.
MigrationStatus = Literal["ready", "nothing", "saved", "migrated"]


@dataclass(frozen=True)
class EnvSource:
    """A .env found where the CLI looks for one; ``active`` is the one in use today."""

    path: Path
    active: bool
    digest: str


@dataclass(frozen=True)
class MigrationPlan:
    """What apply_migration() would do; ``describe()`` is the preview to show before asking
    for consent, and ``token`` the value apply_migration() needs as its consent."""

    status: MigrationStatus
    sources: tuple[EnvSource, ...] = ()
    settings: Mapping[str, str] = field(default_factory=dict, repr=False)
    secret_values: Mapping[str, str] = field(default_factory=dict, repr=False)
    defaults: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    token: str = ""

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(self.secret_values)

    def describe(self) -> list[str]:
        """The preview, one line per point: settings and file names, never a value."""
        if self.status == "nothing":
            return ["No .env was found; there is nothing to migrate."]
        if self.status == "saved":
            name = config_files.SETTINGS_FILE
            return [f"Settings are already saved in {name}; a .env is ignored."]
        if self.status == "migrated":
            return ["The .env was already migrated; roll back to undo it."]
        lines = [
            f"{s.path}: {'in use' if s.active else 'not in use (an earlier .env wins)'}; "
            "it will be moved into the backup"
            for s in self.sources
        ]
        if self.settings:
            lines.append(f"To {config_files.SETTINGS_FILE}: {', '.join(self.settings)}")
        if self.secrets:
            lines.append(f"To the secret store: {', '.join(self.secrets)}")
        if self.defaults:
            lines.append(
                f"Same as the default, not saved (later default changes apply): "
                f"{', '.join(self.defaults)}"
            )
        if self.ignored:
            lines.append(f"Not a setting, left in the backup: {', '.join(self.ignored)}")
        return lines


def _marker(directory: Path) -> Path:
    return directory / MARKER_FILE


def _file_digest(path: Path) -> str | None:
    """The sha256 of a file's bytes; None when there is no such file."""
    try:
        return config_files.digest(path.read_bytes())
    except OSError:
        if not config_files.is_file(path):
            return None
        raise


def _backup_names(directory: Path) -> list[str]:
    try:
        return sorted(os.listdir(directory / BACKUP_DIR))
    except OSError:
        return []


def _token(*parts: object) -> str:
    return config_files.digest(json.dumps(parts, sort_keys=True, default=str).encode("utf-8"))


def _env_pairs(path: Path, data: bytes, fix: str) -> dict[str, str]:
    """The pairs of a .env's bytes. Refused when it is not UTF-8, or when a free-text
    setting's unquoted value was cut at ` #` (config._HASH_IN_VALUE): config.toml would keep
    only the part before it, and the rest (part of a regex, say) would be lost with the .env.
    Any other setting's ` # note` is a comment, as dotenv reads it."""
    try:
        pairs, _, cut = config._parse_env_text(config._env_text(data))
    except UnicodeDecodeError:
        raise MigrationError(f"{path} is not UTF-8 text; nothing was migrated") from None
    for key in cut:
        raise MigrationError(f"fix {fix} first: {config._cut_value_error(key)}")
    return pairs


def _classify(
    key: str, raw: str, taken: bool
) -> Literal["skip", "kept", "secret", "default", "setting"]:
    """Where a .env's ``key`` (a setting) goes: a secret whose value is empty is skipped,
    one ``taken`` (set here already, so today's value wins) is kept, else a secret goes to
    the secret store, and a setting is left out when it equals its default. A bad value
    raises ValueError, also when it is taken."""
    if key in secret_names():
        if not raw.strip():
            return "skip"
        return "kept" if taken else "secret"
    value = _toml_value(key, raw)
    if taken:
        return "kept"
    return "default" if value == _toml_value(key, _FIELDS[key].metadata["default"]) else "setting"


def plan_migration(environ: Mapping[str, str] | None = None) -> MigrationPlan:
    """Preview migrating the .env in use (the repository's, else the user config dir's) to
    config.toml and the secret store. Reads only; writes nothing.

    Only values that differ from their default are saved, so a copied .env.example stops
    pinning defaults. A secret already in the secret store wins today and is kept. Refused
    (MigrationError) while PROMPTMEND_ENV is set (legacy mode keeps that file as it
    is), or while the current settings are invalid.
    """
    env = _env(environ)
    if config.env_file_override(env):
        raise MigrationError(
            f"{config.env_file_var(env)} is set, so that .env stays in use as it is; "
            "unset it to migrate"
        )
    directory = config_dir(env)
    if config_files.is_file(_marker(directory)):
        return MigrationPlan("migrated")
    if config_files.is_file(config.settings_file(env)):
        return MigrationPlan("saved")

    sources: list[EnvSource] = []
    pairs: dict[str, str] | None = None
    for path in config._env_file_candidates(env):
        try:
            data = path.read_bytes()
        except OSError:
            # Missing, or a parent that is a file (Windows: FileNotFoundError or
            # FileExistsError, POSIX: NotADirectoryError) is no .env; a real one that cannot
            # be read stops the migration.
            if not config_files.is_file(path):
                continue
            raise MigrationError(f"{path} cannot be read; nothing was migrated") from None
        if pairs is None:
            pairs = _env_pairs(path, data, "the .env")
        sources.append(EnvSource(path, len(sources) == 0, config_files.digest(data)))
    if pairs is None:
        return MigrationPlan("nothing")
    try:
        ConfigLayers.resolve(env)
    except ValueError as exc:
        raise MigrationError(f"fix the current settings first: {exc}") from None
    store = config_files.secret_store(directory)
    try:
        stored = store.read()
    except SecretStoreError as exc:
        raise MigrationError(f"the secret store failed: {exc}") from None

    secrets = secret_names()
    settings: dict[str, str] = {}
    secret_values: dict[str, str] = {}
    defaults: list[str] = []
    ignored: list[str] = []
    for key, raw in pairs.items():
        if key not in _FIELDS:
            ignored.append(safe_repr(key))
            continue
        try:
            kind = _classify(key, raw, key in secrets and bool(stored.get(key)))
        except ValueError as exc:
            raise MigrationError(f"fix the .env first: {exc}") from None
        if kind == "secret":
            secret_values[key] = raw.strip()
        elif kind == "default":
            defaults.append(key)
        elif kind == "setting":
            settings[key] = raw

    token = _token(
        [(str(s.path), s.active, s.digest) for s in sources],
        _file_digest(directory / config_files.SECRETS_FILE),
        settings,
        sorted(secret_values),
        # Every migration attempt adds a backup directory, so a preview's token cannot be
        # replayed after a migration and its rollback restored the same files.
        _backup_names(directory),
    )
    return MigrationPlan(
        "ready",
        sources=tuple(sources),
        settings=settings,
        secret_values=secret_values,
        defaults=tuple(defaults),
        ignored=tuple(ignored),
        token=token,
    )


@dataclass(frozen=True)
class MigrationResult:
    """Where the backup is, and where each .env went: ``moved`` maps each original to its
    inactive copy; ``left`` lists any that could not be moved (ignored from now on)."""

    backup: Path
    moved: Mapping[Path, Path]
    left: tuple[Path, ...]


def _effective(environ: Mapping[str, str]) -> dict[str, tuple[Any, str]]:
    """Each setting's parsed value (so `30.0` equals the default `30`) and its source."""
    entries = ConfigLayers.resolve(environ).entries
    return {k: (_toml_value(k, e.value), e.source) for k, e in entries.items()}


def _new_backup_dir(directory: Path) -> tuple[Path, str]:
    root = directory / BACKUP_DIR
    config_files.make_private_dir(root)
    while True:
        stamp = _stamp()
        backup = root / f"migration-{stamp}"
        try:
            backup.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return backup, stamp


def _write_marker(directory: Path, record: Mapping[str, Any]) -> None:
    data = json.dumps(record, indent=2).encode("utf-8")
    config_files.write_atomic(_marker(directory), data, private=False)


def _move(source: Path, target: Path) -> bool:
    """Rename ``source`` to ``target`` (never over an existing file); False when it fails,
    for example across file systems."""
    if config_files.lexists(target):
        return False
    try:
        source.rename(target)
    except OSError:
        return False
    return True


def _unlink_real(path: Path) -> None:
    """Remove the file at ``path``; a symlink stays and the file it points to goes (#236)."""
    Path(os.path.realpath(path)).unlink()


def _label(path: Path) -> str:
    return "repository" if path.parent == config._PROJECT_ROOT else "user-config"


def apply_migration(environ: Mapping[str, str] | None = None, *, consent: str) -> MigrationResult:
    """Migrate the .env as plan_migration() previewed it. ``consent`` must be that plan's
    ``token``: it proves the preview was shown and confirmed, and refuses the migration when
    anything changed since (MigrationError).

    Order: backup, write the secret store and config.toml, verify by reloading that every
    effective setting is unchanged and none comes from a .env any more (else the writes are
    undone), and only then move each .env into the backup directory (a rename, never a
    delete; across file systems, a rename in place to `.env.inactive-<time>`). A marker
    records it all, so a second run does nothing and apply_rollback() can undo it.
    """
    env = _env(environ)
    plan = plan_migration(env)
    if plan.status != "ready":
        raise MigrationError(plan.describe()[0])
    if not consent or consent != plan.token:
        raise MigrationError("not confirmed, or the settings changed since the preview")
    directory = config_dir(env)
    settings_path = config.settings_file(env)
    secrets_path = directory / config_files.SECRETS_FILE
    before = _effective(env)
    try:
        backup, stamp = _new_backup_dir(directory)
    except OSError as exc:
        raise MigrationError(
            f"nothing was migrated: cannot create the backup folder ({exc.strerror or exc})"
        ) from None
    secrets_copy = None
    targets = {
        s.path: backup / f"{index}-{_label(s.path)}.env.inactive"
        for index, s in enumerate(plan.sources, start=1)
    }
    record: dict[str, Any] = {
        "migrated_at": stamp,
        "backup": str(backup),
        "secrets_backup": None,
        "sources": [
            {
                "from": str(s.path),
                "to": str(targets[s.path]),
                "in_place": str(s.path.with_name(f".env.inactive-{stamp}")),
                "sha256": s.digest,
            }
            for s in plan.sources
        ],
    }
    try:
        if config_files.is_file(secrets_path):
            secrets_copy = backup / config_files.SECRETS_FILE
            config_files.write_atomic(secrets_copy, secrets_path.read_bytes(), private=True)
            record["secrets_backup"] = secrets_copy.name
        # Written before anything changes, and naming every place a .env may move to, so a
        # rollback can undo whatever happened after it, even a run cut short.
        _write_marker(directory, record)
    except (OSError, SecretStoreError) as exc:
        with contextlib.suppress(OSError):  # never hides ``exc``
            _marker(directory).unlink(missing_ok=True)
        raise MigrationError(f"nothing was migrated: {exc}") from None

    try:
        store = config_files.secret_store(directory)
        for name, value in plan.secret_values.items():
            store.set(name, value)
        table: dict[str, Any] = {VERSION_KEY: CONFIG_VERSION}
        table |= {k: _toml_value(k, v) for k, v in plan.settings.items()}
        config_files.write_atomic(settings_path, config_files.dump_toml(table), private=False)
        after = _effective(env)
    except (ValueError, OSError, SecretStoreError) as exc:
        raise _undo(directory, settings_path, secrets_path, secrets_copy, str(exc)) from None
    wrong = _verify(plan, before, after)
    if wrong:
        reason = f"reloading changed {', '.join(wrong)}"
        raise _undo(directory, settings_path, secrets_path, secrets_copy, reason)
    moved, left = _move_sources(record, targets)
    return MigrationResult(backup, moved, tuple(left))


def _verify(
    plan: MigrationPlan,
    before: Mapping[str, tuple[Any, str]],
    after: Mapping[str, tuple[Any, str]],
) -> list[str]:
    """The settings whose reloaded value changed, else those still read from a .env; empty
    when the migration's writes check out."""
    env_sources = {f"file:{s.path}" for s in plan.sources}
    changed = sorted(k for k in before if before[k][0] != after[k][0])
    still = sorted(k for k, (_, source) in after.items() if source in env_sources)
    return changed or still


def _move_sources(
    record: Mapping[str, Any], targets: Mapping[Path, Path]
) -> tuple[dict[Path, Path], list[Path]]:
    """Move each migrated .env into the backup (else rename it in place). Returns what moved
    where and what was left."""
    moved: dict[Path, Path] = {}
    left: list[Path] = []
    for entry in record["sources"]:
        env_file, in_place = Path(entry["from"]), Path(entry["in_place"])
        if _file_digest(env_file) != entry["sha256"]:
            # Edited since the preview: left where it is (ignored from now on), unmoved.
            left.append(env_file)
        elif _move(env_file, targets[env_file]):
            moved[env_file] = targets[env_file]
        elif _move(env_file, in_place):
            moved[env_file] = in_place
        else:
            left.append(env_file)
    return moved, left


def _undo(
    directory: Path,
    settings_path: Path,
    secrets_path: Path,
    secrets_copy: Path | None,
    reason: str,
) -> MigrationError:
    """Undo a migration's own writes after it failed for ``reason`` and return the error to
    raise: config.toml was not a file before (plan_migration requires that), secrets.toml gets
    its old bytes back (or is removed when there was none), and the marker goes. Each step
    runs even when an earlier one failed, and a failure never hides ``reason``. When a file
    cannot be undone the marker stays, so `promptmend config rollback` can finish the job."""

    def settings() -> None:
        if config_files.is_file(settings_path):
            _unlink_real(settings_path)

    def secrets() -> None:
        if secrets_copy is not None:
            config_files.write_atomic(secrets_path, secrets_copy.read_bytes(), private=True)
        elif config_files.is_file(secrets_path):
            _unlink_real(secrets_path)

    failed: list[str] = []
    for name, step in (
        (config_files.SETTINGS_FILE, settings),
        (config_files.SECRETS_FILE, secrets),
    ):
        try:
            step()
        except (OSError, SecretStoreError):
            failed.append(name)
    if not failed:  # else the marker stays for the rollback
        try:
            _marker(directory).unlink(missing_ok=True)
        except OSError:
            failed.append(MARKER_FILE)
    if failed:
        return MigrationError(
            f"migration failed: {reason}; could not undo: {', '.join(failed)}; "
            "run `promptmend config rollback`"
        )
    return MigrationError(f"nothing was migrated: {reason}")


@dataclass(frozen=True)
class RollbackPlan:
    """What apply_rollback() would do; ``token`` is the consent it needs."""

    backup: Path
    restores: tuple[tuple[Path, Path], ...]
    token: str
    # Checkouts whose .env the removed copy mode (`config migrate --from`, before #246) copied:
    # this install never reads it, so the settings that came only from there are gone.
    unread: tuple[Path, ...] = ()

    @property
    def read_again(self) -> bool:
        """A restored .env is one this install reads (not only a copied checkout's)."""
        return any(original.parent not in self.unread for _, original in self.restores)

    def describe(self) -> list[str]:
        lines = [f"{original}: restored from {stored}" for stored, original in self.restores]
        lines.append(
            f"{config_files.SETTINGS_FILE} and {config_files.SECRETS_FILE} as they are now: "
            f"moved into {self.backup}"
        )
        lines.extend(
            f"This install does not read {root / '.env'}: the settings copied from it are "
            "gone, and the match files still call this install. To use that checkout again, "
            "run its install script."
            for root in self.unread
        )
        return lines


def _read_marker(directory: Path) -> tuple[dict[str, Any], bytes]:
    try:
        data = _marker(directory).read_bytes()
    except FileNotFoundError:
        raise MigrationError("no migration to roll back") from None
    try:
        record = json.loads(data)
    except ValueError:
        record = None
    if not isinstance(record, dict) or not {"backup", "sources"} <= record.keys():
        raise MigrationError(f"{MARKER_FILE} is damaged; restore the backup by hand")
    return record, data


def plan_rollback(environ: Mapping[str, str] | None = None) -> RollbackPlan:
    """Preview undoing the migration recorded in the marker. Refused when a moved .env is
    missing or changed, or a new .env took its place: nothing is overwritten. A .env that copy
    mode only copied is left alone, unless it was retired since: then it is moved back."""
    # The copied/retired_to/retired_in_place entries come only from markers that older
    # versions (copy mode and `config retire`, removed in #246) wrote.
    env = _env(environ)
    directory = config_dir(env)
    record, data = _read_marker(directory)
    restores: list[tuple[Path, Path]] = []
    unread: list[Path] = []
    for entry in record["sources"]:
        original = Path(entry["from"])
        if entry.get("copied"):  # never the running checkout: copy mode refused it
            unread.append(original.parent)
        keys = ("retired_to", "retired_in_place") if entry.get("copied") else ("to", "in_place")
        places = [Path(entry[key]) for key in keys if entry.get(key)]
        if entry.get("copied") and not places:
            continue
        stored = next((place for place in places if config_files.is_file(place)), None)
        if stored is not None:
            if config_files.lexists(original):
                raise MigrationError(f"{original} exists again; move it away to roll back")
            if _file_digest(stored) != entry["sha256"]:
                raise MigrationError(f"{stored} changed since the migration; restore it by hand")
            restores.append((stored, original))
        elif not config_files.is_file(original):
            raise MigrationError(f"the backup of {original} is missing; restore it by hand")
    token = _token(
        config_files.digest(data),
        _file_digest(config.settings_file(env)),
        _file_digest(directory / config_files.SECRETS_FILE),
        [(str(stored), str(original)) for stored, original in restores],
    )
    return RollbackPlan(Path(record["backup"]), tuple(restores), token, tuple(unread))


def apply_rollback(environ: Mapping[str, str] | None = None, *, consent: str) -> None:
    """Restore the state before the migration: each .env is moved back to where it was
    first (ignored while config.toml exists, so a trigger in between still runs on the saved
    settings, and a failed restore stops before anything else changed), then config.toml and
    secrets.toml are moved into the backup directory (never deleted; a symlinked one is
    copied there and stays a link), a secrets.toml that existed before gets its exact old
    bytes back, and the marker is removed. ``consent`` must be the token of the
    plan_rollback() preview."""
    env = _env(environ)
    plan = plan_rollback(env)
    if not consent or consent != plan.token:
        raise MigrationError("not confirmed, or the files changed since the preview")
    directory = config_dir(env)
    record, _ = _read_marker(directory)
    stamp = _stamp()
    for stored, original in plan.restores:
        if not _move(stored, original):
            raise MigrationError(f"could not move {stored} back to {original}; rollback stopped")
    for path in (config.settings_file(env), directory / config_files.SECRETS_FILE):
        if not config_files.is_file(path):
            continue
        stored = plan.backup / f"rolled-back-{stamp}-{path.name}"
        if path.is_symlink():  # keep the link (#236): copy its bytes, then remove its target
            try:
                config_files.write_atomic(stored, path.read_bytes(), private=True)
                _unlink_real(path)
            except OSError as exc:
                raise MigrationError(
                    f"could not copy {path} into the backup ({exc.strerror or exc}); "
                    "rollback stopped"
                ) from None
        elif not _move(path, stored):
            raise MigrationError(f"could not move {path} into the backup; rollback stopped")
    if record.get("secrets_backup"):
        old = (plan.backup / record["secrets_backup"]).read_bytes()
        config_files.write_atomic(directory / config_files.SECRETS_FILE, old, private=True)
    _marker(directory).unlink()
