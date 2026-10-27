"""Per-character forget/erase epochs; the B03 delete barrier builds on it.

`scoped_tombstones.json` keeps, per memory subject, the highest forget epoch
requested and the highest erase epoch that completed. Both are monotonic: a
stale or replayed bump never lowers a fence, because lowering it would reopen
a window a newer forget closed and let deleted content be written back.

On-disk shape is the host's bare map (no schema envelope; the host writes it
unversioned, so the version gate is deferred to B03 when the barrier lands):

    {"<subject>": {"forgotten_at": <float epoch seconds>,
                   "forget_epoch": <int>, "erased_epoch": <int>}, ...}

The host omits `erased_epoch` on a forget-only row; an absent value reads as
0 (no erase completed yet), while an absent or invalid `forget_epoch` is
damage.

Fail closed: a damaged file, a malformed row, or an epoch that is not a
non-negative integer raises CorruptStoreError instead of reading as "nothing
was ever erased" — that reading would resurrect deleted content. A missing
file is a fresh store. Every write passes the injected WritePermit first and
runs under one `threading.Lock` per file; the file is never rewritten by a
read.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..errors import CorruptStoreError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit

TOMBSTONES_FILENAME = "scoped_tombstones.json"

#: One lock per tombstone file; created lazily, never held across an await (none here).
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


@dataclass(frozen=True)
class TombstoneState:
    """Epoch fences of one subject; `forgotten_at` is metadata only."""

    forget_epoch: int = 0
    erased_epoch: int = 0
    forgotten_at: float | None = None


class ScopedTombstoneStore:
    """Monotonic per-subject forget/erase epochs (see the module docstring)."""

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

    # -- paths and rows --------------------------------------------------

    def _path(self, character: str) -> Path:
        return self._roots.character_dir(character, create=False).joinpath(TOMBSTONES_FILENAME)

    def _load(self, path: Path) -> dict[str, dict[str, Any]]:
        """Load and fully validate the subject map; never writes."""
        raw = self._store.load(path)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise CorruptStoreError(
                f"expected a subject map, found {type(raw).__name__}", path=path,
            )
        rows: dict[str, dict[str, Any]] = {}
        for subject, row in raw.items():
            if not isinstance(subject, str) or not subject.strip():
                raise CorruptStoreError(
                    f"invalid tombstone subject {subject!r}", path=path,
                )
            if not isinstance(row, dict):
                raise CorruptStoreError(
                    f"tombstone of {subject!r} is not an object", path=path,
                )
            self._row_to_state(subject, row, path)
            rows[subject] = row
        return rows

    @staticmethod
    def _row_to_state(
        subject: str, row: Mapping[str, Any], path: Path,
    ) -> TombstoneState:
        forget = _epoch(row.get("forget_epoch"), "forget_epoch", subject, path)
        # The host writes a forget-only row without `erased_epoch`; its absence
        # means no erase of that subject has completed yet, not damage.
        erased_raw = row.get("erased_epoch")
        erased = 0 if erased_raw is None else _epoch(
            erased_raw, "erased_epoch", subject, path,
        )
        if erased > forget:
            raise CorruptStoreError(
                f"tombstone of {subject!r}: erased_epoch {erased} exceeds "
                f"forget_epoch {forget}", path=path,
            )
        forgotten_at = row.get("forgotten_at")
        if forgotten_at is not None and (
            isinstance(forgotten_at, bool) or not isinstance(forgotten_at, (int, float))
        ):
            raise CorruptStoreError(
                f"tombstone of {subject!r}: forgotten_at must be a number or null",
                path=path,
            )
        return TombstoneState(
            forget_epoch=forget,
            erased_epoch=erased,
            forgotten_at=None if forgotten_at is None else float(forgotten_at),
        )

    def _now(self) -> float:
        return self._clock.now().timestamp()

    # -- public API ------------------------------------------------------

    def read(self, character: str, subject: str) -> TombstoneState:
        """Epochs of one subject; a missing subject is `(0, 0)`."""
        _require_subject(subject)
        path = self._path(character)
        with _lock_for(path):
            rows = self._load(path)
            row = rows.get(subject)
            if row is None:
                return TombstoneState()
            return self._row_to_state(subject, row, path)

    def bump_forget(self, character: str, subject: str, epoch: int) -> TombstoneState:
        """Raise the subject's forget fence to at least `epoch` (monotonic)."""
        return self._bump(character, subject, forget=epoch)

    def bump_erased(self, character: str, subject: str, epoch: int) -> TombstoneState:
        """Record that the erase of `epoch` completed (monotonic).

        The fence is raised to cover the completed erase, so a later read can
        never see `erased_epoch > forget_epoch`.
        """
        return self._bump(character, subject, erased=epoch)

    def is_erased(self, character: str, subject: str, epoch: int) -> bool:
        """Whether an erase at or above `epoch` already completed."""
        _require_epoch(epoch)
        return epoch <= self.read(character, subject).erased_epoch

    def snapshot(self, character: str) -> Mapping[str, TombstoneState]:
        """Read-only copy of every subject's epochs."""
        path = self._path(character)
        with _lock_for(path):
            rows = self._load(path)
            return {
                subject: self._row_to_state(subject, row, path)
                for subject, row in rows.items()
            }

    # -- internals -------------------------------------------------------

    def _bump(
        self,
        character: str,
        subject: str,
        *,
        forget: int | None = None,
        erased: int | None = None,
    ) -> TombstoneState:
        _require_subject(subject)
        forget_epoch = 0 if forget is None else _require_epoch(forget)
        erased_epoch = 0 if erased is None else _require_epoch(erased)
        path = self._path(character)
        with _lock_for(path):
            rows = self._load(path)
            row = rows.get(subject)
            current = (
                TombstoneState() if row is None
                else self._row_to_state(subject, row, path)
            )
            new_erased = max(current.erased_epoch, erased_epoch)
            new_forget = max(current.forget_epoch, forget_epoch, new_erased)
            if (new_forget, new_erased) == (current.forget_epoch, current.erased_epoch):
                return current
            now = self._now()
            rows[subject] = {
                "forgotten_at": now,
                "forget_epoch": new_forget,
                "erased_epoch": new_erased,
            }
            self._permit.assert_writable("save", str(path))
            self._store.save(path, rows)
            return TombstoneState(new_forget, new_erased, now)


def _epoch(value: Any, field_name: str, subject: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CorruptStoreError(
            f"tombstone of {subject!r}: {field_name} must be a nonnegative "
            f"integer, found {value!r}", path=path,
        )
    return value


def _require_epoch(epoch: int) -> int:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("epoch must be a nonnegative integer")
    return epoch


def _require_subject(subject: str) -> str:
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("subject must be a nonempty string")
    return subject


__all__ = ["TOMBSTONES_FILENAME", "ScopedTombstoneStore", "TombstoneState"]
