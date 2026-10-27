"""Host-compatible persona storage (B05).

Stores the host's per-character persona files without any domain policy: fact
merging, correction resolution, card sync and rendering belong to the B05
domain services and must not leak in here.

On-disk contract (B05 spec sections 1.5-1.7, design 17.16.5)
------------------------------------------------------------
* persona.json: top-level object {<entity>: {"facts": [entry, ...]}}. entity
  is master/neko/relationship or a scoped key such as @subject/<kind>:<id>.
  Every entity object and every fact entry round-trips verbatim (unknown
  fields preserved, no defaults filled). An empty persona is {}. A missing or
  whitespace-only file is {}.
* persona_corrections.json: BARE JSON ARRAY of correction queue rows
  {old_text, new_text, entity, created_at, correction_id, ...}. Rows are
  returned verbatim; the queue identity rules belong to the domain service.
* persona_archive/: delegated to ArchiveShardStore (persona_archive dirname,
  <YYYY-MM-DD>_<uuid8>.json shards, <=500 rows each).

Strict reads
------------
Invalid JSON, a non-object persona root, a non-object entity value, a missing
or non-list facts field, a non-object entry, or an entry without a nonempty
id raise CorruptStoreError. The file is never repaired, emptied or rewritten,
and a save refuses to replace a file this reader would reject. Reading a
damaged persona as empty would silently drop the character's current
understanding and let the next save overwrite the only copy.

Writes
------
Every write runs inside the process-wide per-file lock and follows the frozen
order WritePermit.assert_writable -> write_guard -> AtomicJsonStore.save.
save() passes the flattened list of every fact entry across entities to the
guard (character, entries) so the B05 deletion/correction barrier can reject
a row before it is persisted. The correction queue only passes the permit:
queue rows are user intent and are not filtered by the memory barrier (B03
decision D1). All methods are synchronous blocking I/O; call them through
asyncio.to_thread from async code.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .shards import ArchiveShardStore, WriteGuard, is_blank_file

#: File names inside <character_dir>; the host uses these exact names.
PERSONA_FILENAME = "persona.json"
PERSONA_CORRECTIONS_FILENAME = "persona_corrections.json"

#: Archive directory under <character_dir>.
ARCHIVE_DIRNAME = "persona_archive"

#: One lock per physical file, shared process-wide.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


# -- raw file helpers -------------------------------------------------------


def _load_raw(store: AtomicJsonStore, path: Path) -> Any | None:
    """Load a JSON file, treating a whitespace-only file as absent."""
    try:
        return store.load(path)
    except CorruptStoreError:
        if is_blank_file(path):
            return None
        raise


def _load_array(
    store: AtomicJsonStore,
    path: Path,
    *,
    what: str,
) -> list[dict[str, Any]]:
    """Strict bare-array read; never writes."""
    raw = _load_raw(store, path)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CorruptStoreError(
            f"{what}: expected a JSON array, found {type(raw).__name__}", path=path,
        )
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise CorruptStoreError(
                f"{what}[{index}]: expected a JSON object, "
                f"found {type(item).__name__}",
                path=path,
            )
        rows.append(dict(item))
    return rows


def _validated_rows(rows: Any, *, what: str) -> list[dict[str, Any]]:
    """Copy a caller payload after a shape check (caller error -> ValueError)."""
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise ValueError(f"{what}: expected a sequence of JSON objects")
    payload: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"{what}[{index}]: expected a JSON object, "
                f"found {type(item).__name__}"
            )
        payload.append(dict(item))
    return payload


def _load_persona(store: AtomicJsonStore, path: Path) -> dict[str, dict[str, Any]]:
    """Strict {entity: {facts: [...]}} read; never writes."""
    raw = _load_raw(store, path)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"persona: expected a JSON object, found {type(raw).__name__}", path=path,
        )
    doc: dict[str, dict[str, Any]] = {}
    for entity, value in raw.items():
        if not isinstance(value, Mapping):
            raise CorruptStoreError(
                f"persona[{entity!r}]: expected an object, "
                f"found {type(value).__name__}",
                path=path,
            )
        facts = value.get("facts")
        if not isinstance(facts, list):
            raise CorruptStoreError(
                f"persona[{entity!r}].facts: expected a list, "
                f"found {type(facts).__name__}",
                path=path,
            )
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(facts):
            if not isinstance(item, Mapping):
                raise CorruptStoreError(
                    f"persona[{entity!r}].facts[{index}]: expected a JSON object, "
                    f"found {type(item).__name__}",
                    path=path,
                )
            entry_id = item.get("id")
            if not isinstance(entry_id, str) or not entry_id.strip():
                raise CorruptStoreError(
                    f"persona[{entity!r}].facts[{index}]: missing required 'id'",
                    path=path,
                )
            entries.append(dict(item))
        entity_doc = dict(value)
        entity_doc["facts"] = entries
        doc[str(entity)] = entity_doc
    return doc


def _validated_persona(doc: Any) -> dict[str, dict[str, Any]]:
    """Copy a caller document after a shape check (caller error -> ValueError)."""
    if not isinstance(doc, Mapping):
        raise ValueError("persona: expected a document object")
    payload: dict[str, dict[str, Any]] = {}
    for entity, value in doc.items():
        if not isinstance(value, Mapping):
            raise ValueError(
                f"persona[{entity!r}]: expected an object, "
                f"found {type(value).__name__}"
            )
        facts = value.get("facts")
        if isinstance(facts, (str, bytes)) or not isinstance(facts, Sequence):
            raise ValueError(f"persona[{entity!r}].facts: expected a sequence")
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(facts):
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"persona[{entity!r}].facts[{index}]: expected a JSON object, "
                    f"found {type(item).__name__}"
                )
            entry_id = item.get("id")
            if not isinstance(entry_id, str) or not entry_id.strip():
                raise ValueError(
                    f"persona[{entity!r}].facts[{index}]: missing required 'id'"
                )
            entries.append(dict(item))
        entity_doc = dict(value)
        entity_doc["facts"] = entries
        payload[str(entity)] = entity_doc
    return payload


def _all_entries(doc: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flatten every fact entry across entities, in document order."""
    return [
        entry
        for entity_doc in doc.values()
        for entry in entity_doc.get("facts") or ()
    ]


# -- store ------------------------------------------------------------------


class PersonaStore:
    """Per-character persona files with host shapes and strict reads.

    One instance serves every character; the per-file lock is shared
    process-wide. clock is part of the frozen wiring contract and is passed to
    the archive shard store for its archival date; these files carry their own
    timestamps and are never stamped here. archive lets the caller inject a
    shared ArchiveShardStore; when omitted one is built for persona_archive.
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        write_guard: WriteGuard | None = None,
        archive: ArchiveShardStore | None = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._write_guard = write_guard
        self._archive = archive if archive is not None else ArchiveShardStore(
            roots=roots,
            store=store,
            clock=clock,
            permit=permit,
            dirname=ARCHIVE_DIRNAME,
            write_guard=write_guard,
        )

    # -- paths ----------------------------------------------------------

    def _path(self, character: str, filename: str) -> Path:
        """Resolve <character_dir>/<filename> without creating anything."""
        return self._roots.character_dir(character, create=False) / filename

    # -- persona --------------------------------------------------------

    def load(self, character: str) -> dict[str, dict[str, Any]]:
        """Return the persona document verbatim (empty when absent)."""
        path = self._path(character, PERSONA_FILENAME)
        with _lock_for(path):
            return _load_persona(self._store, path)

    def save(self, character: str, doc: Mapping[str, Any]) -> None:
        """Atomically replace persona.json with the bare entity object."""
        payload = _validated_persona(doc)
        path = self._path(character, PERSONA_FILENAME)
        with _lock_for(path):
            # Refuse to replace a file this reader would reject: a damaged
            # persona must be inspected, not silently overwritten.
            _load_persona(self._store, path)
            self._permit.assert_writable("save_persona", str(path))
            if self._write_guard is not None:
                self._write_guard(character, _all_entries(payload))
            self._store.save(path, payload)

    # -- corrections ----------------------------------------------------

    def load_corrections(self, character: str) -> list[dict[str, Any]]:
        """Return the correction queue verbatim (empty when absent)."""
        path = self._path(character, PERSONA_CORRECTIONS_FILENAME)
        with _lock_for(path):
            return _load_array(
                self._store, path, what=PERSONA_CORRECTIONS_FILENAME,
            )

    def save_corrections(
        self, character: str, rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Atomically replace persona_corrections.json with the bare array."""
        payload = _validated_rows(rows, what=PERSONA_CORRECTIONS_FILENAME)
        path = self._path(character, PERSONA_CORRECTIONS_FILENAME)
        with _lock_for(path):
            _load_array(self._store, path, what=PERSONA_CORRECTIONS_FILENAME)
            self._permit.assert_writable(
                "save_persona_corrections", str(path),
            )
            self._store.save(path, payload)

    # -- archive --------------------------------------------------------

    def append_archive(
        self, character: str, entries: Sequence[Mapping[str, Any]],
    ) -> str:
        """Append archived rows and return the last shard filename written."""
        return self._archive.append(character, entries)

    def load_archive(self, character: str) -> list[dict[str, Any]]:
        """Return every archived row across shards (empty when absent)."""
        return self._archive.read_all(character)

    def ensure_archive_entry(
        self, character: str, entry: Mapping[str, Any], shard_path: str,
    ) -> bool:
        """Self-heal one archived row into its recorded shard.

        Returns True when the row had to be written, False when already
        present. A damaged shard raises CorruptStoreError and is left as
        found, so replay can retry instead of losing the active copy.
        """
        return self._archive.ensure_entry(character, entry, shard_path)

    def delete_from_archive(self, character: str, ids: Sequence[str]) -> int:
        """Remove archived rows whose id is in ids; return the count."""
        return self._archive.delete_ids(character, ids)


__all__ = [
    "ARCHIVE_DIRNAME",
    "PERSONA_CORRECTIONS_FILENAME",
    "PERSONA_FILENAME",
    "PersonaStore",
]
