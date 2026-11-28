"""The Windows access-control code of the saved settings files: the user-only DACL a private
file gets (restrict_to_user, from config_files.write_atomic) and the check whether a file is
open to other accounts (open_to_others, from the secrets file's exposure check), through
ctypes. Imported only on Windows, inside the config_files branch that needs it, so it is
never loaded on POSIX at run time; ctypes is loaded only on the first Win32 call
(_load_win32). The SDDL parsers (only_user, opens_to_others) are plain Python, tested on
every platform.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .config_files import SecretStoreError

# Windows security constants (winnt.h, accctrl.h).
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1


# advapi32 and kernel32, loaded once per process (the interface asks on its loop thread).
_DLLS: tuple[Any, Any] | None = None


def _win32() -> tuple[Any, Any]:
    """advapi32 and kernel32 with the signatures used here (64-bit safe handles), loaded on
    first use and then reused."""
    global _DLLS  # a lazy, per-process cache
    if _DLLS is None:
        _DLLS = _load_win32()
    return _DLLS


def _load_win32() -> tuple[Any, Any]:
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
# ACE types that deny access (SDDL): they never open a file to anyone. A conditional
# (callback) entry holds a parenthesised condition, which the parser does not take apart, so
# any such entry makes the whole descriptor count as open (fails closed).
_DENY_ACES = frozenset({"D", "OD"})
# SDDL's OWNER RIGHTS: an entry for whoever owns the file.
_OWNER_RIGHTS = ("OW", "S-1-3-4")


def opens_to_others(sddl: str, sids: tuple[str, ...]) -> bool:
    """The security descriptor in ``sddl`` (``O:<owner>D:<dacl>``, the owner optional) lets an
    account in other than ``sids`` (the user's SID and its SDDL alias), SYSTEM or
    Administrators: an allow entry for anyone else, an owner who is someone else (an owner
    can change the access list at will), a null DACL (``NO_ACCESS_CONTROL``, open to
    everyone) or a descriptor that cannot be parsed (a conditional entry among them). Deny
    and inherit-only entries grant nothing; an empty DACL grants nothing to anyone."""
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
    A symlink is resolved first, as write_atomic writes through it. Raises OSError when the
    descriptor cannot be read."""
    sid, alias = _user_sids()
    target = Path(os.path.realpath(path))
    sddl = _read_security(target, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION)
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
