"""Whether the clipboard holds an item a password manager marked as not for other apps.

A trigger sends whatever is on the clipboard, so a vault password copied a minute ago would
go to the model if the user forgot to copy the draft. Password managers mark such items: on
macOS with the nspasteboard.org types, on Windows with the clipboard formats Microsoft defines
for clipboard history and monitors. Each probe asks only which formats are present, never for
the content, through ctypes, so nothing is added to the package; ctypes (a native module on
Windows) is imported only when a probe runs, so a trigger that reads no clipboard never loads
it (#312).

is_concealed() returns None when it cannot tell (another OS, or a probe error), and the caller
then reads the clipboard as before: the check adds protection, never a new way to fail. Browser
extensions copy through the web clipboard API and set no marker, so they are not covered.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any, Protocol

# ConcealedType (nspasteboard.org) is set by 1Password, Bitwarden, KeePassXC and Enpass; 1Password
# also adds its own type. TransientType is left out: text expanders set it for their own
# momentary pastes, which are not secrets.
MAC_MARKERS = frozenset({"org.nspasteboard.ConcealedType", "com.agilebits.onepassword"})
# Present as a format means "do not record": the first is Microsoft's, the second the older
# convention clipboard viewers honour (KeePass sets both).
WIN_MARKERS = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")
# A DWORD format; 0 asks Windows to keep the item out of clipboard history. Bitwarden sets only
# this one, so an item that has it but cannot be read counts as concealed.
WIN_HISTORY_FORMAT = "CanIncludeInClipboardHistory"
# OpenClipboard fails while another app (a clipboard manager, the history service) holds it.
OPEN_ATTEMPTS = 5
OPEN_RETRY_SECONDS = 0.01


class ObjCRuntime(Protocol):
    def cls(self, name: str) -> object: ...
    def call(self, obj: object, selector: str, *args: int) -> Any: ...
    def string(self, obj: object) -> str: ...


class ClipboardApi(Protocol):
    def has_format(self, name: str) -> bool: ...
    def read_dword(self, name: str) -> int | None: ...


class _MacRuntime:
    """The Objective-C runtime calls the macOS probe needs. objc_msgSend is cast to a
    function type per call, as arm64 requires exact argument types."""

    def __init__(self) -> None:
        import ctypes

        self._objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")  # defines NSPasteboard
        self._objc.objc_getClass.restype = ctypes.c_void_p
        self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self._objc.sel_registerName.restype = ctypes.c_void_p
        self._objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def cls(self, name: str) -> object:
        return self._objc.objc_getClass(name.encode())

    def _send(self, restype: Any, obj: object, selector: str, *args: int) -> Any:
        import ctypes

        argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_ulong] * len(args)
        send = ctypes.CFUNCTYPE(restype, *argtypes)(("objc_msgSend", self._objc))
        return send(obj, self._objc.sel_registerName(selector.encode()), *args)

    def call(self, obj: object, selector: str, *args: int) -> Any:
        import ctypes

        return self._send(ctypes.c_void_p, obj, selector, *args)

    def string(self, obj: object) -> str:
        import ctypes

        return bytes(self._send(ctypes.c_char_p, obj, "UTF8String") or b"").decode()


class _WinClipboard:
    """The user32/kernel32 clipboard calls the Windows probe needs."""

    def __init__(self) -> None:
        import ctypes

        win_dll = getattr(ctypes, "WinDLL")  # noqa: B009 - absent from ctypes off Windows
        self._user32 = win_dll("user32")
        self._kernel32 = win_dll("kernel32")
        u, k = self._user32, self._kernel32
        u.RegisterClipboardFormatW.restype = ctypes.c_uint
        u.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
        u.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        u.OpenClipboard.argtypes = [ctypes.c_void_p]
        u.GetClipboardData.restype = ctypes.c_void_p
        u.GetClipboardData.argtypes = [ctypes.c_uint]
        k.GlobalSize.restype = ctypes.c_size_t
        k.GlobalSize.argtypes = [ctypes.c_void_p]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]

    def _format(self, name: str) -> int:
        return int(self._user32.RegisterClipboardFormatW(name))

    def has_format(self, name: str) -> bool:
        fmt = self._format(name)
        return bool(fmt and self._user32.IsClipboardFormatAvailable(fmt))

    def _open(self) -> bool:
        for attempt in range(OPEN_ATTEMPTS):
            if self._user32.OpenClipboard(None):
                return True
            if attempt + 1 < OPEN_ATTEMPTS:
                time.sleep(OPEN_RETRY_SECONDS)
        return False

    def read_dword(self, name: str) -> int | None:
        import ctypes

        if not self._open():
            return None
        try:
            handle = self._user32.GetClipboardData(self._format(name))
            big_enough = handle and self._kernel32.GlobalSize(handle) >= 4
            pointer = self._kernel32.GlobalLock(handle) if big_enough else None
            if not pointer:
                return None
            try:
                return int(ctypes.c_uint32.from_address(pointer).value)
            finally:
                self._kernel32.GlobalUnlock(handle)
        finally:
            self._user32.CloseClipboard()


def mac_concealed(runtime: ObjCRuntime, pasteboard: object = None) -> bool:
    """Whether the pasteboard's current item (the general one unless given) carries a
    password-manager type."""
    if pasteboard is None:
        pasteboard = runtime.call(runtime.cls("NSPasteboard"), "generalPasteboard")
    types = runtime.call(pasteboard, "types")
    count = int(runtime.call(types, "count") or 0) if types else 0
    names = {runtime.string(runtime.call(types, "objectAtIndex:", i)) for i in range(count)}
    return bool(MAC_MARKERS & names)


def win_concealed(clipboard: ClipboardApi) -> bool:
    """Whether the clipboard carries a do-not-record format or opts out of clipboard history
    (or has that format but it cannot be read)."""
    if any(clipboard.has_format(name) for name in WIN_MARKERS):
        return True
    if not clipboard.has_format(WIN_HISTORY_FORMAT):
        return False
    return clipboard.read_dword(WIN_HISTORY_FORMAT) != 1


_PROBES: dict[str, Callable[[], bool]] = {
    "darwin": lambda: mac_concealed(_MacRuntime()),
    "win32": lambda: win_concealed(_WinClipboard()),
}


def is_concealed(platform: str | None = None) -> bool | None:
    """True if a password manager marked the clipboard item, False if not, None if this OS
    has no probe (Linux) or the probe failed."""
    probe = _PROBES.get(platform or sys.platform)
    if probe is None:
        return None
    try:
        return probe()
    except Exception:  # any ctypes or OS failure (ArgumentError is not a TypeError)
        return None
