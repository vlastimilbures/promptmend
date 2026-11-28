"""The saved settings files in the user config dir: ``config.toml`` (non-secret settings) and
``secrets.toml`` (API keys, private to the user), plus the atomic, private file writes the
config services (config_store.py) build on.

This module is on the trigger path (ConfigLayers.resolve reads both files), so it stays
light: tomllib is imported only when a file exists, the writer (tomli_w), tempfile and
subprocess only when something is written, and hashlib (OpenSSL's libcrypto, a native module
on Windows) only when a digest is computed.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SETTINGS_FILE = "config.toml"
SECRETS_FILE = "secrets.toml"
# Format version of config.toml. A file with a newer version is read (best effort) but never
# written, since this version cannot know what a rewrite would lose.
VERSION_KEY = "config_version"
CONFIG_VERSION = 1


def config_version(table: Mapping[str, Any]) -> int | None:
    """config.toml's config_version (a missing one is the current version); None when it is
    not a whole number above 0."""
    version = table.get(VERSION_KEY, CONFIG_VERSION)
    valid = isinstance(version, int) and not isinstance(version, bool) and version >= 1
    return version if valid else None


_WINDOWS = os.name == "nt"


class ConfigFileError(ValueError):
    """A saved settings file that cannot be read or parsed. The message names the file and,
    for a syntax error, the position, never the content (it may hold a key)."""


class SecretStoreError(Exception):
    """The secret store failed. Never answered by falling back to a plaintext file."""


class StaleSecretsError(SecretStoreError):
    """The secrets file changed between a save's read and its write (another save at the same
    moment), so nothing was written: the other save's key would have been lost. The message
    names the file, never a key or its value."""


def _position(exc: Exception) -> str:
    # tomllib reports "Invalid value (at line 3, column 9)"; only the position is kept.
    found = re.search(r"\((at line \d+, column \d+|at end of document)\)", str(exc))
    return f" ({found.group(1)})" if found else ""


def digest(data: bytes) -> str:
    import hashlib  # only the management commands compute a digest

    return hashlib.sha256(data).hexdigest()


def is_file(path: Path) -> bool:
    """``path`` is a regular file; False, never an error, when it is missing, a directory, or
    cannot be checked (its folder is a file, or unreadable)."""
    return os.path.isfile(path)


def lexists(path: Path) -> bool:
    """Something (a file, a folder, a link) is at ``path``; False when it cannot be checked."""
    return os.path.lexists(path)


def load_toml(path: Path) -> tuple[dict[str, Any], str] | None:
    """The parsed table of a TOML file and the sha256 of its bytes, or None when there is no
    such file. A folder at ``path``, or a parent that is a file or cannot be read, counts as
    no file, so an odd config dir never switches saved mode on. Raises ConfigFileError when a
    regular file exists but cannot be read or parsed."""
    try:
        data = path.read_bytes()
    except OSError:
        if not is_file(path):
            return None
        raise ConfigFileError(f"{path.name} cannot be read") from None
    import tomllib  # only once a saved file exists (about 2 ms on top of the CLI imports)

    try:
        # utf-8-sig: Windows Notepad may save the file with a byte-order mark.
        table = tomllib.loads(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise ConfigFileError(f"{path.name} is not UTF-8 text") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError(f"{path.name} is not valid TOML{_position(exc)}") from None
    return table, digest(data)


def scalar_text(value: object) -> str | None:
    """A TOML value as the string a .env or environment variable would hold, or None when it
    is not a scalar setting value (a table, an array, a date)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    return None


def dump_toml(table: Mapping[str, Any]) -> bytes:
    import tomli_w  # never on the trigger path

    return tomli_w.dumps(dict(table)).encode("utf-8")


def make_private_dir(directory: Path) -> None:
    """Create ``directory`` (and parents) if missing; a new one is private to the user on
    POSIX (an existing one keeps its mode)."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def write_atomic(
    path: Path,
    data: bytes,
    *,
    private: bool,
    before_replace: Callable[[], None] | None = None,
) -> None:
    """Replace ``path`` with ``data`` all at once: a reader sees the old file or the new one,
    never a torn write. The temporary file is created in the same directory (os.replace is
    atomic only within one file system; on Windows it is MoveFileEx with
    MOVEFILE_REPLACE_EXISTING, which fails rather than half-replaces when another process
    holds the file open).

    The temporary file is created with mode 600 on POSIX (mkstemp), so a ``private`` file is
    never readable by others, not even for a moment. On Windows a ``private`` file gets a
    user-only ACL while still empty, before any secret is written into it.

    A symlinked ``path`` (a config.toml kept in a dotfiles repo) is written through to the
    file it points to, so the link stays a link.

    ``before_replace`` runs last before the os.replace; what it raises removes the temporary
    file and leaves ``path`` as it was (the secrets file's check for a concurrent save).
    """
    make_private_dir(path.parent)
    # Only the folder the caller named is created: a dangling link to a missing folder fails.
    path = Path(os.path.realpath(path))
    if path.is_dir():
        raise IsADirectoryError(errno.EISDIR, "it is a folder, not a file", str(path))
    import tempfile  # only when something is written

    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(name)
    try:
        os.close(fd)
        if private and _WINDOWS:
            from . import winacl  # Windows only, and only for a private file

            winacl.restrict_to_user(temp)
        with temp.open("r+b") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    if not _WINDOWS:
        # Persist the rename itself; best effort, since some file systems refuse it.
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)


class FileSecretStore:
    """Secrets in a TOML file (``NAME = "value"``, plaintext, not encrypted) private to the
    user: mode 600 on POSIX, a user-only ACL on Windows. Never part of config.toml. Reads and
    writes raise SecretStoreError, never fall back to another store. Users see it as "the
    secrets file"."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def source(self) -> str:
        return f"file:{self.path}"

    def read(self) -> dict[str, str]:
        return self._load()[0]

    def _load(self) -> tuple[dict[str, str], str | None]:
        """The keys and the sha256 of the file's bytes (None with no file)."""
        try:
            loaded = load_toml(self.path)
        except ConfigFileError as exc:
            raise SecretStoreError(str(exc)) from None
        if loaded is None:
            return {}, None
        return {k: v for k, v in loaded[0].items() if isinstance(v, str)}, loaded[1]

    def _open(self) -> bool | None:
        """The file is open to other accounts: on POSIX readable or writable by the group or
        others; on Windows an owner or access-list entry for anyone but the user, SYSTEM and
        Administrators (open_to_others). False when there is no file; None when it exists but
        could not be checked (any failure: this check must never break the settings)."""
        if not is_file(self.path):
            return False
        try:
            if _WINDOWS:
                from . import winacl  # Windows only, never asked by a trigger

                return winacl.open_to_others(self.path)
            return bool(self.path.stat().st_mode & 0o077)
        except Exception:  # an unknown answer, reported as such
            return None

    def exposed(self) -> bool:
        """The file is known to be open to other accounts (see _open)."""
        return self._open() is True

    def exposure(self) -> str | None:
        """The repair-mode finding about the file's permissions, or None when it is private
        or missing. Names no value. Only the management commands ask, never a trigger."""
        found = self._open()
        if found is False:
            return None
        if not _WINDOWS:
            if found is None:
                return "could not check the secrets file's permissions"
            return "the secrets file can be read by other users; make it private (chmod 600)"
        fix = (
            "save a key again with `promptmend secrets set <NAME>`, which rewrites it "
            "private to your account"
        )
        if found is None:
            return f"could not check the secrets file's access list; {fix}"
        return f"the secrets file's access list or owner is not private to your account; {fix}"

    def _write(self, values: Mapping[str, str], read: str | None, what: str) -> None:
        """Replace the file with ``values``, unless it changed since the read whose digest is
        ``read`` (StaleSecretsError): two saves at the same moment would otherwise each write
        the file they read, and the first one's key would be lost without a word. Checked
        again right before the os.replace, which is the only window left."""

        def unchanged() -> None:
            if self._load()[1] != read:
                raise StaleSecretsError(
                    f"{self.path.name} changed while {what} (another save at the same "
                    "moment?); nothing was written: run it again"
                )

        try:
            write_atomic(
                self.path,
                dump_toml(dict(sorted(values.items()))),
                private=True,
                before_replace=unchanged,
            )
        except OSError as exc:
            raise SecretStoreError(f"could not write {self.path.name}: {exc.strerror}") from None

    def set(self, name: str, value: str) -> None:
        values, read = self._load()
        self._write({**values, name: value}, read, f"{name} was being saved")

    def delete(self, name: str) -> None:
        values, read = self._load()
        if name in values:
            del values[name]
            self._write(values, read, f"{name} was being removed")


def secret_store(directory: Path) -> FileSecretStore:
    """The secret store for the config dir ``directory``.

    TODO(D-CFG-3): an opt-in KeyringSecretStore, once it has been tested from an
    Espanso-launched (non-TTY) process on macOS, including after the interpreter path changes,
    and on Windows. It must raise SecretStoreError on any keyring failure; it must never fall
    back to this file. Import keyring lazily (it is forbidden on the trigger path). Bring
    back a SecretStore Protocol then, for the two stores. The store is also read on the
    interface's loop thread by tui.state.quick() (#348), so a keyring store must stay
    off the loop there (quick() needs only whether each key is set).
    """
    return FileSecretStore(directory / SECRETS_FILE)
