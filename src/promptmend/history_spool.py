"""The usage history's sidecar files next to the database: the spool, the drop files of the
lost-write count, the sidecar lock that guards them and the sweep of the temp files a killed
write left (moved out of history.py, #412).

A write that misses its budget is kept in a small spool file of the same allowlisted columns,
which a later write stores. A record that is truly dropped is counted: in a small sidecar
marker or, when the marker's lock is not free in time or the marker cannot be written, in a new
empty drop file of its own, which needs no lock.

SpoolFiles holds these as methods of history.HistoryStore, its only subclass, and relies on
the store for ``lost_path`` and ``spool_path`` (set in HistoryStore.__init__) and for what
knows the record's columns and the time format: ``_from_spool()`` (validates a spool entry
as record() validates a new one) and ``_count_lost()`` (writes the marker). This module
never imports history or sqlite3.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

# Sidecar marker of dropped writes, next to the database (JSON: count and last time).
LOST_NAME = "history.lost"
# An empty `history.lost.<random>.<n>.drop` file counts n writes dropped while the marker could
# not be updated (its lock not free in time): created with O_EXCL, so it needs no lock.
_DROP_SUFFIX = ".drop"
_WINDOWS = os.name == "nt"
# Records whose write missed its budget, next to the database, until a later write stores them
# (JSON: {"version": 1, "records": [{"operation": {column: value}, "attempts": [...]}]}).
SPOOL_NAME = "history.spool"
_SPOOL_VERSION = 1
# Most records the spool holds; a record past it is dropped and counted as lost.
_SPOOL_LIMIT = 100
# Largest spool file read or written; a larger one counts as corrupt.
_SPOOL_BYTES = 512 * 1024

# A sidecar lock older than this was left by a killed process (it is held for a few ms).
_STALE_LOCK = 10.0
# The temp file of a marker or spool write (`history.lost.<pid>.tmp`,
# `history.spool.<pid>.tmp`) lives for milliseconds, between its write and its os.replace,
# always under the sidecar lock, which is broken as stale after _STALE_LOCK. One older than
# this was left by a crash or the watchdog's hard exit between the two steps; replay() removes
# it. Six times _STALE_LOCK, so even a writer whose lock was broken under it is long done.
_STALE_TEMP = 6 * _STALE_LOCK

# A drop file of _count_dropped(): a random id and the positive number of writes it counts.
_DROP_NAME = re.compile(re.escape(LOST_NAME) + r"\.[0-9a-f]{32}\.([1-9][0-9]{0,9})\.drop")

# One record as database rows: the operation row and its attempt rows.
_Spooled = tuple[tuple[object, ...], list[tuple[object, ...]]]


def _drop_count(name: str) -> int | None:
    """The count in a drop file's name (``history.lost.<32 hex>.<n>.drop``, n > 0), None
    when the name is not of that shape."""
    match = _DROP_NAME.fullmatch(name)
    return int(match[1]) if match else None


def _break_stale(lock: Path) -> bool:
    """Remove ``lock`` if a killed process left it (older than _STALE_LOCK). Only the process
    that creates the ``<lock>.break`` claim (O_EXCL) may break it, and it checks the age again
    under the claim before it deletes the lock: with no other breaker, the lock it checked is
    the one it deletes. (Renaming the lock aside and putting back a fresh one found there
    could not work: while it was away a third process could take the lock, and the holder of
    the fresh one went on believing it held it too, #413.) A claim left by a process killed
    while breaking is removed once it is older than _STALE_LOCK. True when the lock was
    removed; the caller then tries to take it again.

    What remains: a holder paused for longer than _STALE_LOCK counts as dead, as with any
    timeout lock; and removing an abandoned claim is a stat and then an unlink, so two
    processes paused at exactly those two points, with a claim a killed breaker left, could
    still let two breakers run."""
    claim = lock.with_name(f"{lock.name}.break")
    try:
        if time.time() - lock.stat().st_mtime <= _STALE_LOCK:
            return False
        try:
            os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            # Another process is breaking it (for microseconds), or was killed doing so.
            if time.time() - claim.stat().st_mtime > _STALE_LOCK:
                claim.unlink()
            return False
    except OSError:
        # Windows: PermissionError for a claim whose deletion is pending (busy).
        return False
    try:
        if time.time() - lock.stat().st_mtime <= _STALE_LOCK:
            return False  # another breaker removed it first and a fresh lock was taken
        lock.unlink()
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            claim.unlink()
    return True


class SpoolFiles:
    """The spool, the drop files, the sidecar lock and the temp-file sweep of a
    history.HistoryStore (see the module docstring for what the store provides)."""

    lost_path: Path
    spool_path: Path
    _from_spool: Callable[[object, tuple[re.Pattern[str], ...]], _Spooled]
    _count_lost: Callable[[int], None]

    def _note_lost(self, dropped: int) -> bool:
        """Count ``dropped`` writes in the marker, or in drop files when it cannot be
        written; the caller holds the sidecar lock. False only when neither can be written.
        Never raises."""
        try:
            self._count_lost(dropped)
        except Exception:
            return self._count_dropped(dropped)
        return True

    def _count_dropped(self, dropped: int) -> bool:
        """Count ``dropped`` writes without the sidecar lock: one new empty, owner-only drop
        file, ``history.lost.<random>.<dropped>.drop``, created exclusively under a random
        name, so it never waits for or blocks another writer, and one file whatever the count.
        health() adds them to the marker's count; reset() removes them. False when it cannot
        be created. Never raises."""
        try:
            name = f"{LOST_NAME}.{uuid.uuid4().hex}.{dropped}{_DROP_SUFFIX}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            os.close(os.open(self.lost_path.with_name(name), flags, 0o600))
        except Exception:
            return False
        return True

    def _drop_files(self) -> list[Path]:
        """The files of the writes _count_dropped() counted, [] with no folder; OSError when
        the folder cannot be listed (os.listdir, not Path.glob, which hides that error and
        would hide every drop file with it)."""
        folder = self.lost_path.parent
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            return []
        prefix = f"{LOST_NAME}."
        return [folder / n for n in names if n.startswith(prefix) and n.endswith(_DROP_SUFFIX)]

    def _sweep_temp(self, older_than: float | None = None) -> None:
        """Remove the temp files a marker or spool write left (see _STALE_TEMP), every one or
        only those older than ``older_than`` seconds. A management command's step, never
        record()'s: one listing of the data folder and a stat per temp file. Never raises:
        a temp file holds nothing that counts, and one still open on Windows stays."""
        folder = self.lost_path.parent
        prefixes = (f"{LOST_NAME}.", f"{SPOOL_NAME}.")
        with contextlib.suppress(OSError):
            for name in os.listdir(folder):
                if not (name.startswith(prefixes) and name.endswith(".tmp")):
                    continue
                temp = folder / name
                with contextlib.suppress(OSError):
                    if older_than is None or time.time() - temp.stat().st_mtime > older_than:
                        temp.unlink()

    def _load_spool(self) -> list[object]:
        """The spool's entries as written, [] with no spool; OSError when it cannot be read,
        ValueError when it is not a valid spool (too large, not JSON, unknown version)."""
        try:
            with self.spool_path.open("rb") as handle:
                data = handle.read(_SPOOL_BYTES + 1)
        except FileNotFoundError:
            return []
        if len(data) > _SPOOL_BYTES:
            raise ValueError("history spool is too large")
        spool = json.loads(data)
        if not isinstance(spool, dict) or spool.get("version") != _SPOOL_VERSION:
            raise ValueError("not a history spool")
        records = spool.get("records")
        if not isinstance(records, list):
            raise ValueError("not a history spool")
        return records

    def _save_spool(self, records: list[object]) -> None:
        """Replace the spool with ``records`` (temp file + os.replace), or remove it when
        there are none; the caller holds the sidecar lock. ValueError when it would be larger
        than _SPOOL_BYTES."""
        if not records:
            self.spool_path.unlink(missing_ok=True)
            return
        payload = json.dumps({"version": _SPOOL_VERSION, "records": records})
        if len(payload.encode("utf-8")) > _SPOOL_BYTES:
            raise ValueError("history spool is full")
        # Written like the lost marker, not by write_atomic() (see _count_lost()).
        temp = self.spool_path.with_name(f"{SPOOL_NAME}.{os.getpid()}.tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.spool_path)

    def _replayable(
        self, extra: tuple[re.Pattern[str], ...], deadline: float
    ) -> tuple[list[_Spooled], bool]:
        """The valid spooled records (at most _SPOOL_LIMIT), and whether the spool has
        anything to settle (a record, an invalid entry, or a file that is not a spool). The
        spool is read under the sidecar lock (an unlocked reader would make another
        process's os.replace fail on Windows); with no spool the lock is not taken. An
        unreadable spool, or a lock not had by ``deadline``, is left for later. Never
        raises."""
        if not self.spool_path.exists():
            return [], False
        try:
            with self._sidecar_lock(deadline):
                entries = self._load_spool()
        except ValueError:
            return [], True
        except Exception:
            return [], False
        records = []
        for entry in entries[:_SPOOL_LIMIT]:
            with contextlib.suppress(Exception):
                records.append(self._from_spool(entry, extra))
        return records, bool(entries)

    def _settle_spool(
        self, stored: set[object], extra: tuple[re.Pattern[str], ...], deadline: float
    ) -> None:
        """After a commit: drop the spooled records now in the database (``stored``, their
        ids) and every entry that is not valid, which counts as lost (a file that is not a
        spool counts once). Read again under the lock, so an entry another process added
        since is kept. Gives up at ``deadline`` (the records then replay again later, which
        INSERT OR IGNORE makes harmless). Never raises."""
        with contextlib.suppress(Exception), self._sidecar_lock(deadline):
            try:
                entries = self._load_spool()
            except ValueError:
                entries, dropped = [], 1
            else:
                dropped = 0
            kept = []
            for entry in entries:
                try:
                    op_id = self._from_spool(entry, extra)[0][0]
                except Exception:
                    dropped += 1
                    continue
                if op_id not in stored:
                    kept.append(entry)
            self._save_spool(kept[:_SPOOL_LIMIT])
            dropped += max(0, len(kept) - _SPOOL_LIMIT)
            if dropped:
                self._note_lost(dropped)

    @contextlib.contextmanager
    def _sidecar_lock(self, deadline: float) -> Iterator[None]:
        """Hold the sidecar's lock file, which holds a random nonce: only the holder whose
        nonce is in it removes it. One left by a killed process is broken after _STALE_LOCK.
        Tries at least once, however little time is left."""
        lock = self.lost_path.with_name(f"{LOST_NAME}.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        nonce = uuid.uuid4().hex
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError) as exc:
                # Windows refuses to create a file whose deletion is still pending (another
                # holder just released the lock) with PermissionError: busy, not denied.
                if isinstance(exc, PermissionError) and not _WINDOWS:
                    raise
                if _break_stale(lock):
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("history sidecar is locked") from None
                time.sleep(min(0.002, max(0.0, deadline - time.monotonic())))
                continue
            try:
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    fd = -1  # the handle owns it now and closes it
                    handle.write(nonce)
            except BaseException:
                # Never leave an empty lock behind (it would block every writer until it is
                # stale) or an open descriptor.
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                with contextlib.suppress(OSError):
                    lock.unlink(missing_ok=True)
                raise
            break
        try:
            yield
        finally:
            # Any error (a garbage lock file is not ASCII): the work under the lock is done,
            # and raising here would count it as failed.
            with contextlib.suppress(Exception):
                if lock.read_text(encoding="ascii") == nonce:
                    lock.unlink()
