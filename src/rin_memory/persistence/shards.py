"""Host-compatible archive shard storage shared by reflections and persona.

Both memory/reflection/ and memory/persona/ archive rows into a directory of
per-day shard files. The sharding rules live here once so the two archives
cannot drift apart (host memory/archive_shards.py).

On-disk contract (B05 spec sections 1.4/1.7, design 17.16.5)
------------------------------------------------------------
* Directory: <character_dir>/<dirname>/ (reflection_archive or
  persona_archive).
* Shard name: <YYYY-MM-DD>_<uuid8>.json, where the date is the archival date
  from the injected clock (local-clock, like the host) and uuid8 is the first
  8 hex chars of a uuid4.
* Shard content: a BARE JSON ARRAY of archived entry objects. Every object
  round-trips verbatim (unknown fields preserved); nothing is sorted, stamped
  or defaulted by this adapter.
* A shard holds at most max_entries rows (host default 500). Same-day
  appends coalesce into the first same-day shard that still has room; when
  every same-day shard is full a fresh shard is minted. One append may spill
  across several shards.
* A missing directory or missing shard is empty, not an error.
* A shard that cannot be parsed, is not an array, or holds a non-object row
  raises CorruptStoreError and is never rewritten, emptied or reused as if it
  were full. read_all therefore aborts instead of returning a partial
  archive: silently skipping a damaged shard would hide data from the caller
  and a later append could then overwrite it.

Writes
------
Every shard write runs inside the process-wide per-archive-directory lock and
follows the frozen order WritePermit.assert_writable -> write_guard ->
AtomicJsonStore.save. The optional guard is called with the entries that
write adds (the new chunk, the healed entry) so a deletion/correction barrier
can reject an archived row before it reaches disk. A raising permit or guard
leaves the shard untouched.

delete_ids reads every shard first, then rewrites only the affected ones (an
emptied shard becomes []; the file is never deleted). That two-pass order
means a corrupt shard aborts the whole operation before any shard is
modified. The guard is not consulted for deletion: removing rows never
reintroduces content.

All methods are synchronous blocking I/O; call them through
asyncio.to_thread from async code, like the other stores in this package.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit

#: Host shard filename pattern (memory/archive_shards.py).
SHARD_NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<uuid8>[0-9a-f]{8})\.json$")

#: Host default (ARCHIVE_FILE_MAX_ENTRIES, B05 spec section 1.4).
DEFAULT_MAX_ENTRIES = 500

#: Called with the character and the rows a write is about to add; raising
#: aborts that write. B05 wires the deletion/correction barrier here.
WriteGuard = Callable[[str, Sequence[Mapping[str, Any]]], None]

#: One lock per physical archive directory, shared process-wide so two store
#: instances in one process cannot interleave shard selection.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(directory: Path) -> threading.RLock:
    key = os.path.normcase(str(directory))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


def is_blank_file(path: Path) -> bool:
    """Whether the file exists and holds only whitespace.

    The host has historically written empty JSON files and read them as an
    empty view. The atomic store reports such a file as "empty file is not
    valid JSON", so bare-array stores special-case exactly this state (and
    nothing else) as empty. A missing file is also blank for this purpose.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return not text.strip()


def is_shard_name(name: Any) -> bool:
    """Whether name is exactly one host shard filename."""
    return isinstance(name, str) and bool(SHARD_NAME_RE.match(name))


def _iter_strings(values: Any) -> Iterable[str]:
    """Yield string items from a scalar or iterable id argument."""
    if values is None:
        return
    if isinstance(values, (str, bytes)):
        values = [values]
    if not isinstance(values, Iterable):
        return
    for value in values:
        if isinstance(value, str):
            yield value


def _clean_ids(values: Any) -> set[str]:
    return {value.strip() for value in _iter_strings(values) if value.strip()}


def _validated_entries(entries: Any, *, what: str) -> list[dict[str, Any]]:
    """Copy a shard payload after a shape check (caller error -> ValueError)."""
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
        raise ValueError(f"{what}: expected a sequence of JSON objects")
    payload: list[dict[str, Any]] = []
    for index, item in enumerate(entries):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"{what}[{index}]: expected a JSON object, "
                f"found {type(item).__name__}"
            )
        payload.append(dict(item))
    return payload


class ArchiveShardStore:
    """Per-character sharded archive with host filenames and capacity rules.

    One instance serves every character; the per-directory lock is shared
    process-wide. clock supplies the local archival date stamped into new
    shard names. dirname is the archive directory name under the character
    directory (reflection_archive / persona_archive).
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        dirname: str,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        write_guard: WriteGuard | None = None,
    ) -> None:
        if not isinstance(dirname, str) or not dirname.strip():
            raise ValueError("archive dirname must be a nonempty string")
        if dirname in {".", ".."} or "/" in dirname or "\\" in dirname:
            raise ValueError(f"invalid archive dirname {dirname!r}")
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise ValueError("max_entries must be an integer")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._dirname = dirname
        self._max_entries = max_entries
        self._write_guard = write_guard

    # -- paths ----------------------------------------------------------

    def directory(self, character: str) -> Path:
        """Resolve <character_dir>/<dirname> without creating anything."""
        return self._roots.character_dir(character, create=False) / self._dirname

    def _path(self, character: str, name: str) -> Path:
        return self.directory(character) / name

    def _today(self) -> str:
        """Local archival date, mirroring the host's datetime.now()."""
        now = self._clock.now()
        if now.tzinfo is not None:
            now = now.astimezone()
        return now.date().isoformat()

    def _shard_names(self, directory: Path) -> list[str]:
        """Valid shard filenames in lexicographic order; other files ignored."""
        try:
            names = [entry.name for entry in directory.iterdir() if entry.is_file()]
        except FileNotFoundError:
            return []
        return sorted(name for name in names if SHARD_NAME_RE.match(name))

    # -- reads ----------------------------------------------------------

    def read_all(self, character: str) -> list[dict[str, Any]]:
        """Concatenate every shard in filename order (empty when absent).

        A damaged shard raises CorruptStoreError; the caller gets no partial
        archive and the damaged bytes stay untouched.
        """
        directory = self.directory(character)
        with _lock_for(directory):
            rows: list[dict[str, Any]] = []
            for name in self._shard_names(directory):
                rows.extend(self._read(character, name))
            return rows

    def read_shard(self, character: str, name: str) -> list[dict[str, Any]]:
        """Read one named shard; missing file is [], damage raises."""
        shard = self._checked_name(name)
        directory = self.directory(character)
        with _lock_for(directory):
            return self._read(character, shard)

    def _read(self, character: str, name: str) -> list[dict[str, Any]]:
        path = self._path(character, name)
        raw = self._store.load(path)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise CorruptStoreError(
                f"archive shard {name!r}: expected a JSON array, "
                f"found {type(raw).__name__}",
                path=path,
            )
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise CorruptStoreError(
                    f"archive shard {name!r}[{index}]: expected a JSON object, "
                    f"found {type(item).__name__}",
                    path=path,
                )
            rows.append(dict(item))
        return rows

    # -- writes ---------------------------------------------------------

    def append(self, character: str, entries: Sequence[Mapping[str, Any]]) -> str:
        """Append entries into today's shard(s) and return the last shard name.

        Entries spill into fresh shards when the open shard reaches
        max_entries. Returns "" for an empty payload (host behaviour). Each
        shard write is atomic; the multi-shard sequence is not one
        transaction, but a crash can only leave rows in the archive, never
        silently drop them from an active view (the caller removes rows from
        the active view after this returns).
        """
        payload = _validated_entries(entries, what="archive entries")
        if not payload:
            return ""
        directory = self.directory(character)
        with _lock_for(directory):
            today = self._today()
            sizes = self._sizes_for_day(directory, today)
            remaining = list(payload)
            last_written = ""
            while remaining:
                name = self._pick(directory, today, sizes)
                existing = self._read(character, name)
                capacity = self._max_entries - len(existing)
                if capacity <= 0:
                    sizes[name] = self._max_entries
                    continue
                chunk = remaining[:capacity]
                remaining = remaining[capacity:]
                merged = existing + chunk
                self._write(character, name, merged, chunk, operation="append_archive")
                sizes[name] = len(merged)
                last_written = name
            return last_written

    def ensure_entry(
        self, character: str, entry: Mapping[str, Any], shard_path: str,
    ) -> bool:
        """Self-heal one archived row into its recorded shard.

        Returns True when the row had to be written, False when the shard
        already holds it (or the entry carries no usable id, host behaviour).
        A missing shard is created. A damaged shard raises CorruptStoreError
        and is never overwritten: the caller must keep the active copy until
        an operator repairs the shard.
        """
        if not isinstance(entry, Mapping):
            return False
        entry_copy = dict(entry)
        entry_id = entry_copy.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            return False
        name = self._checked_name(shard_path)
        directory = self.directory(character)
        with _lock_for(directory):
            existing = self._read(character, name)
            for item in existing:
                if item.get("id") == entry_id:
                    return False
            merged = existing + [entry_copy]
            self._write(
                character, name, merged, [entry_copy], operation="heal_archive_shard",
            )
            return True

    def delete_ids(self, character: str, ids: Iterable[str]) -> int:
        """Remove every archived row whose id is in ids; return the count.

        All shards are read before the first rewrite, so a damaged shard
        aborts the operation with no shard modified. A shard that loses rows
        is rewritten atomically (possibly as []); no shard file is deleted.
        """
        targets = _clean_ids(ids)
        if not targets:
            return 0
        directory = self.directory(character)
        with _lock_for(directory):
            plans: list[tuple[str, list[dict[str, Any]]]] = []
            removed = 0
            for name in self._shard_names(directory):
                existing = self._read(character, name)
                kept = [item for item in existing if item.get("id") not in targets]
                if len(kept) != len(existing):
                    removed += len(existing) - len(kept)
                    plans.append((name, kept))
            for name, kept in plans:
                self._write(
                    character, name, kept, (), operation="delete_archive_entries",
                )
            return removed

    # -- internals ------------------------------------------------------

    def _checked_name(self, name: Any) -> str:
        """Accept a shard basename (or <dirname>/<basename>) and validate it."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("shard name must be a nonempty string")
        raw = name.replace("\\", "/")
        if raw != raw.rsplit("/", 1)[-1]:
            parent, _, base = raw.rpartition("/")
            if parent != self._dirname:
                raise ValueError(f"shard path is outside {self._dirname!r}: {name!r}")
            raw = base
        if not SHARD_NAME_RE.match(raw):
            raise ValueError(f"invalid shard name {name!r}")
        return raw

    def _sizes_for_day(self, directory: Path, today: str) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for name in self._shard_names(directory):
            if not name.startswith(today + "_"):
                continue
            # A damaged same-day shard raises here: it is never treated as
            # "full but reusable" and never overwritten by a new append.
            sizes[name] = len(self._read_path(directory / name))
        return sizes

    def _read_path(self, path: Path) -> list[dict[str, Any]]:
        raw = self._store.load(path)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise CorruptStoreError(
                f"archive shard: expected a JSON array, found {type(raw).__name__}",
                path=path,
            )
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise CorruptStoreError(
                    f"archive shard[{index}]: expected a JSON object, "
                    f"found {type(item).__name__}",
                    path=path,
                )
        return [dict(item) for item in raw]

    def _pick(self, directory: Path, today: str, sizes: Mapping[str, int]) -> str:
        """First same-day shard with room, else a fresh collision-free name."""
        todays = [
            name for name in self._shard_names(directory)
            if name.startswith(today + "_")
        ]
        for name in todays:
            if sizes.get(name, 0) < self._max_entries:
                return name
        existing = set(self._shard_names(directory))
        while True:
            candidate = f"{today}_{uuid.uuid4().hex[:8]}.json"
            if candidate not in existing:
                return candidate

    def _write(
        self,
        character: str,
        name: str,
        merged: list[dict[str, Any]],
        guard_entries: Sequence[Mapping[str, Any]],
        *,
        operation: str,
    ) -> None:
        path = self._path(character, name)
        self._permit.assert_writable(operation, str(path))
        if self._write_guard is not None:
            self._write_guard(character, list(guard_entries))
        self._store.save(path, merged)


__all__ = [
    "ArchiveShardStore",
    "DEFAULT_MAX_ENTRIES",
    "SHARD_NAME_RE",
    "WriteGuard",
    "is_blank_file",
    "is_shard_name",
]
