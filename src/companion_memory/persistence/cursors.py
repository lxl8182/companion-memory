"""Per-character named cursors (B02).

cursors.json is a bare JSON object mapping cursor_key to an ISO-8601 string
(host memory/cursors.py). There is deliberately no schema envelope: existing host
files already have this shape and the keys share the object's namespace with
cursor names, so adding a reserved key would collide with a cursor name.
A missing key means "never processed"; the caller owns the fallback window.

Writes are atomic read-modify-write cycles under a per-character
threading.Lock, with no in-memory cache: a second writer (or a restart) never
observes a value that was not persisted, and a failed save leaves the file
exactly as it was.

All methods perform blocking file IO; call them through asyncio.to_thread.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Mapping

from ..errors import CorruptStoreError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit

_CURSORS_FILE = "cursors.json"


class CursorStore:
    """Reads and writes the per-character cursors.json file.

    Implements the ports.CursorStore protocol. Every write goes through the
    injected WritePermit and is preceded by a validating read of the current
    file, so a corrupt file is reported instead of being replaced.
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- paths and locks -------------------------------------------------

    def _lock_for(self, character: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.Lock()
                self._locks[character] = lock
            return lock

    def _path(self, character: str, *, create: bool = True) -> Path:
        return self._roots.character_dir(character, create=create) / _CURSORS_FILE

    # -- reading ---------------------------------------------------------

    def _load_unlocked(self, character: str) -> dict[str, datetime]:
        """Load and validate the whole cursor map; caller holds the lock."""
        path = self._path(character, create=False)
        raw = self._store.load(path)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise CorruptStoreError(
                f"cursors.json must be a JSON object, found {type(raw).__name__}",
                path=path,
            )
        cursors: dict[str, datetime] = {}
        for key, value in raw.items():
            if not isinstance(value, str):
                raise CorruptStoreError(
                    f"cursor {key!r} must be an ISO-8601 string, found "
                    f"{type(value).__name__}", path=path,
                )
            try:
                cursors[key] = datetime.fromisoformat(value)
            except ValueError as exc:
                raise CorruptStoreError(
                    f"cursor {key!r} is not ISO-8601: {value!r}",
                    path=path, cause=exc,
                ) from exc
        return cursors

    # -- public API ------------------------------------------------------

    def get(self, character: str, key: str) -> datetime | None:
        """Return the named cursor, or None when it was never written."""
        with self._lock_for(character):
            return self._load_unlocked(character).get(key)

    def set(self, character: str, key: str, value: datetime) -> None:
        """Persist one cursor atomically, keeping every other key intact."""
        if not isinstance(key, str) or not key.strip():
            raise ValueError("cursor key must be a nonempty string")
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("cursor values must be timezone-aware datetimes")
        path = self._path(character, create=False)
        self._permit.assert_writable("cursor.set", str(path))
        with self._lock_for(character):
            cursors = self._load_unlocked(character)
            cursors[key] = value
            payload = {
                name: stamp.isoformat() for name, stamp in cursors.items()
            }
            self._store.save(self._path(character), payload)

    def snapshot(self, character: str) -> Mapping[str, datetime]:
        """Return a copy of every cursor for the character."""
        with self._lock_for(character):
            return dict(self._load_unlocked(character))
