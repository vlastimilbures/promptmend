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
from collections.abc import Mapping
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


# Windows security constants (winnt.h, accctrl.h).
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1


def _win32() -> tuple[Any, Any]:
    """advapi32 and kernel32 with the signatures used here (64-bit safe handles)."""
    import ctypes
    from ctypes import POINTER, c_int, c_void_p, wintypes

    win_dll = getattr(ctypes, "WinDLL")  # noqa: B009 - absent from ctypes off Windows
    advapi, kernel = (
        win_dll("advapi32", use_last_error=True),
        win_dll("kernel32", use_last_error=True),
    )
    dword, handle, lpwstr = wintypes.DWORD, wintypes.HANDLE, wintypes.LPWSTR
    # A list, not a dict: ctypes function pointers are unhashable.
    signatures = [
        (kernel.GetCurrentProcess, ([], handle)),
        (kernel.CloseHandle, ([handle], wintypes.BOOL)),
        (kernel.LocalFree, ([c_void_p], c_void_p)),
        (advapi.OpenProcessToken, ([handle, dword, POINTER(handle)], wintypes.BOOL)),
        (
            advapi.GetTokenInformation,
            (
                [handle, c_int, c_void_p, dword, POINTER(dword)],
                wintypes.BOOL,
            ),
        ),
        (advapi.ConvertSidToStringSidW, ([c_void_p, POINTER(lpwstr)], wintypes.BOOL)),
        (
            advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW,
            (
                [wintypes.LPCWSTR, dword, POINTER(c_void_p), POINTER(dword)],
                wintypes.BOOL,
            ),
        ),
        (
            advapi.GetSecurityDescriptorDacl,
            (
                [c_void_p, POINTER(wintypes.BOOL), POINTER(c_void_p), POINTER(wintypes.BOOL)],
                wintypes.BOOL,
            ),
        ),
        (
            advapi.SetNamedSecurityInfoW,
            (
                [lpwstr, c_int, dword, c_void_p, c_void_p, c_void_p, c_void_p],
                dword,
            ),
        ),
        (
            advapi.GetNamedSecurityInfoW,
            (
                [
                    wintypes.LPCWSTR,
                    c_int,
                    dword,
                    c_void_p,
                    c_void_p,
                    c_void_p,
                    c_void_p,
                    POINTER(c_void_p),
                ],
                dword,
            ),
        ),
        (
            advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW,
            (
                [c_void_p, dword, dword, POINTER(lpwstr), POINTER(dword)],
                wintypes.BOOL,
            ),
        ),
    ]
    for function, (argtypes, restype) in signatures:
        function.argtypes, function.restype = argtypes, restype
    return advapi, kernel


def _check(ok: object, what: str) -> None:
    if not ok:
        import ctypes

        raise OSError(getattr(ctypes, "get_last_error")(), what)  # noqa: B009 - Windows only


def current_user_sid() -> str:
    """Windows: the SID of the account this process runs as, e.g. S-1-5-21-…-1001."""
    import ctypes
    from ctypes import wintypes

    advapi, kernel = _win32()
    token = wintypes.HANDLE()
    _check(
        advapi.OpenProcessToken(kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)),
        "OpenProcessToken",
    )
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        _check(
            advapi.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)),
            "GetTokenInformation",
        )
    finally:
        kernel.CloseHandle(token)
    # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the SID pointer.
    sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
    text = wintypes.LPWSTR()
    _check(advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)), "ConvertSidToStringSidW")
    try:
        return str(text.value or "")
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _set_dacl(path: Path, sddl: str) -> None:
    """Windows: replace the DACL of ``path`` with the one in ``sddl``, protected from
    inheritance."""
    import ctypes
    from ctypes import c_void_p, wintypes

    advapi, kernel = _win32()
    descriptor = c_void_p()
    _check(
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    )
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), c_void_p()
        _check(
            advapi.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            ),
            "GetSecurityDescriptorDacl",
        )
        flags = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        error = advapi.SetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, flags, None, None, dacl, None
        )
        if error:
            raise OSError(error, "SetNamedSecurityInfoW")
    finally:
        kernel.LocalFree(descriptor)


def _to_sddl(
    advapi: Any, kernel: Any, descriptor: Any, info: int = _DACL_SECURITY_INFORMATION
) -> str:
    """Windows: the ``info`` parts (the DACL by default) of a security descriptor in SDDL."""
    import ctypes
    from ctypes import c_void_p, wintypes

    text = wintypes.LPWSTR()
    _check(
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, _SDDL_REVISION_1, info, ctypes.byref(text), None
        ),
        "ConvertSecurityDescriptorToStringSecurityDescriptorW",
    )
    try:
        return str(text.value or "")
    finally:
        kernel.LocalFree(ctypes.cast(text, c_void_p))


def _read_dacl(path: Path) -> str:
    """Windows: the DACL of ``path`` in SDDL, e.g. ``D:P(A;;FA;;;S-1-5-21-…)``."""
    return _read_security(path, _DACL_SECURITY_INFORMATION)


def _read_security(path: Path, info: int) -> str:
    """Windows: the ``info`` parts of the security descriptor of ``path`` in SDDL."""
    import ctypes
    from ctypes import c_void_p

    advapi, kernel = _win32()
    descriptor = c_void_p()
    error = advapi.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        info,
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if error:
        raise OSError(error, "GetNamedSecurityInfoW")
    try:
        return _to_sddl(advapi, kernel, descriptor, info)
    finally:
        kernel.LocalFree(descriptor)


def _canonical_dacl(sddl: str) -> str:
    """Windows: ``sddl`` as Windows writes it back. SDDL output names some SIDs by alias
    (LA for the built-in Administrator, which a CI runner account may be), so the read-back
    DACL is compared with this rather than with the SID string."""
    import ctypes
    from ctypes import c_void_p

    advapi, kernel = _win32()
    descriptor = c_void_p()
    _check(
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    )
    try:
        return _to_sddl(advapi, kernel, descriptor)
    finally:
        kernel.LocalFree(descriptor)


def only_user(sddl: str, sids: str | tuple[str, ...]) -> bool:
    """The SDDL DACL is protected (inherits nothing), has at least one entry, and every entry
    allows one of ``sids`` (the user's SID string, or its SDDL alias)."""
    allowed = (sids,) if isinstance(sids, str) else sids
    found = re.fullmatch(r"D:([A-Z]*)((?:\([^()]*\))+)", sddl)
    if not found or "P" not in found.group(1):
        return False
    aces = [ace.split(";") for ace in re.findall(r"\(([^()]*)\)", found.group(2))]
    return all(len(ace) == 6 and ace[0] == "A" and ace[5] in allowed for ace in aces)


def _user_sids() -> tuple[str, str]:
    """Windows: the current user's SID string and the name SDDL output gives it (an alias
    such as LA, or the SID itself). Raises OSError."""
    sid = current_user_sid()
    if not sid.startswith("S-1-"):
        raise OSError("no SID")
    alias = _canonical_dacl(f"D:P(A;;FA;;;{sid})").rpartition(";")[2].rstrip(")")
    return sid, alias


# Besides the user, the accounts a file in the user's profile is normally open to (Windows
# gives every profile folder these entries): SYSTEM and the Administrators group, by SDDL
# alias and SID. Either can take any file anyway, so an entry for them is not an exposure.
_TRUSTED_SIDS = ("SY", "S-1-5-18", "BA", "S-1-5-32-544")
# ACE types that deny access (SDDL): they never open a file to anyone.
_DENY_ACES = frozenset({"D", "OD", "XD"})
# SDDL's OWNER RIGHTS: an entry for whoever owns the file.
_OWNER_RIGHTS = ("OW", "S-1-3-4")


def opens_to_others(sddl: str, sids: tuple[str, ...]) -> bool:
    """The security descriptor in ``sddl`` (``O:<owner>D:<dacl>``, the owner optional) lets an
    account in other than ``sids`` (the user's SID and its SDDL alias), SYSTEM or
    Administrators: an allow entry for anyone else, an owner who is someone else (an owner
    can change the access list at will), a null DACL (``NO_ACCESS_CONTROL``, open to
    everyone) or a descriptor that cannot be parsed. Deny and inherit-only entries grant
    nothing; an empty DACL grants nothing to anyone."""
    trusted = {*sids, *_TRUSTED_SIDS}
    found = re.fullmatch(r"(?:O:([^:()]+?))?D:([A-Z]*)((?:\([^()]*\))*)", sddl)
    if not found:
        return True
    owner = found.group(1)
    if owner is not None and owner not in trusted:
        return True
    for ace in re.findall(r"\(([^()]*)\)", found.group(3)):
        fields = ace.split(";")
        if len(fields) < 6:
            return True
        kind, flags, sid = fields[0], fields[1], fields[5]
        inherit_only = "IO" in [flags[i : i + 2] for i in range(0, len(flags), 2)]
        if kind in _DENY_ACES or inherit_only:
            continue
        if sid in _OWNER_RIGHTS and owner is not None:
            continue  # the owner, already checked above
        if sid not in trusted:
            return True
    return False


def open_to_others(path: Path) -> bool:
    """Windows: ``path``'s owner or access list lets another account in (opens_to_others).
    Raises OSError when the descriptor cannot be read."""
    sid, alias = _user_sids()
    sddl = _read_security(path, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION)
    return opens_to_others(sddl, (sid, alias))


def restrict_to_user(path: Path) -> None:
    """Windows: give ``path`` a protected DACL with one entry, full access for the current
    user, so no other account or group (not SYSTEM, not Administrators, nothing inherited)
    is granted access. Then reads the DACL back and refuses anything else, naming the DACL
    it found (SIDs, no secret). Called while the file is still empty, with no handle open.
    Raises SecretStoreError; there is no fallback."""
    try:
        sid, alias = _user_sids()
        _set_dacl(path, f"D:P(A;;FA;;;{sid})")
        actual = _read_dacl(path)
    except OSError as exc:
        raise SecretStoreError(
            f"could not make {path.name} private to your user ({exc.strerror or exc})"
        ) from None
    if not only_user(actual, (sid, alias)):
        raise SecretStoreError(
            f"{path.name} is still open to other accounts ({actual}, expected {sid}); "
            "nothing was written"
        )


def make_private_dir(directory: Path) -> None:
    """Create ``directory`` (and parents) if missing; a new one is private to the user on
    POSIX (an existing one keeps its mode)."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def write_atomic(path: Path, data: bytes, *, private: bool) -> None:
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
            restrict_to_user(temp)
        with temp.open("r+b") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
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
        try:
            loaded = load_toml(self.path)
        except ConfigFileError as exc:
            raise SecretStoreError(str(exc)) from None
        if loaded is None:
            return {}
        return {k: v for k, v in loaded[0].items() if isinstance(v, str)}

    def exposed(self) -> bool:
        """The file is open to other accounts: on POSIX readable or writable by the group or
        others; on Windows an owner or access-list entry for anyone but the user, SYSTEM and
        Administrators (open_to_others). False when it cannot be checked. Only the
        management commands ask (repair mode), never a trigger."""
        try:
            if _WINDOWS:
                return open_to_others(self.path)
            return bool(self.path.stat().st_mode & 0o077)
        except OSError:
            return False

    @staticmethod
    def exposed_hint() -> str:
        """What to do about a secrets file that exposed() reports."""
        if _WINDOWS:
            return (
                "the secrets file's access list lets other accounts in; save a key again with "
                "`promptmend secrets set <NAME>`, which rewrites it private to your account"
            )
        return "the secrets file can be read by other users; make it private (chmod 600)"

    def _write(self, values: Mapping[str, str]) -> None:
        try:
            write_atomic(self.path, dump_toml(dict(sorted(values.items()))), private=True)
        except OSError as exc:
            raise SecretStoreError(f"could not write {self.path.name}: {exc.strerror}") from None

    def set(self, name: str, value: str) -> None:
        self._write({**self.read(), name: value})

    def delete(self, name: str) -> None:
        values = self.read()
        if name in values:
            del values[name]
            self._write(values)


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
