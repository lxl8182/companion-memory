"""Host-compatible reflection storage (B05).

Stores the host's per-character reflection files without any domain policy:
status transitions, evidence scoring, surfacing selection and promotion belong
to the B05 domain services and must not leak in here.

On-disk contract (B05 spec sections 1.1-1.4, design 17.16.5)
------------------------------------------------------------
* reflections.json: BARE JSON ARRAY of reflection entries. Every object
  round-trips verbatim (unknown fields preserved, no defaults filled, no
  terminal status filtered here). A missing or whitespace-only file is [].
* surfaced.json: BARE JSON ARRAY of
  {reflection_id, text, surfaced_at, feedback} rows. At most one row per
  reflection_id: a later row replaces the earlier one while keeping the
  first occurrence position (host surfacing.py:362-377).
* synth_backoff.json: top-level object {key: {n: int, at: ISO|null}}. The
  legacy host format {key: int} is accepted and normalized with at=None. At
  most 64 keys are kept, highest n first (host persistence.py:258-264).
* growth_uses.json: {schema: 1, version: int, uses: [...]}. Missing file is
  {schema: 1, version: 0, uses: []}; uses are capped at 2000 rows keeping the
  newest; a save never lowers version.
* reflection_archive/: delegated to ArchiveShardStore (reflection_archive
  dirname, <YYYY-MM-DD>_<uuid8>.json shards, <=500 rows each).

Strict reads
------------
Invalid JSON, a non-array root, a non-object row, or a missing required id
raise CorruptStoreError and the bytes are left exactly as found: this adapter
never repairs, empties or rewrites a damaged file, and a save also refuses to
replace a file its own reader would reject. Invalid JSON, non-object roots and
non-object rows are corruption rather than "an empty store" because reading
them as empty would silently forget user-visible reflections and let the next
save destroy the original data.

Writes
------
Every write runs inside the process-wide per-file lock and follows the frozen
order WritePermit.assert_writable -> write_guard -> AtomicJsonStore.save.
The guard is part of the frozen ReflectionStore contract for save() only: it
receives (character, reflection rows) so the B05 deletion/correction barrier
can reject a row before it is persisted. Surfaced/backoff/growth writes carry
derived state rather than reflection content, so they only pass the permit.
All methods are synchronous blocking I/O; call them through
asyncio.to_thread from async code.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError, InvalidSchemaError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .shards import ArchiveShardStore, WriteGuard, is_blank_file

#: File names inside <character_dir>; the host uses these exact names.
REFLECTIONS_FILENAME = "reflections.json"
SURFACED_FILENAME = "surfaced.json"
SYNTH_BACKOFF_FILENAME = "synth_backoff.json"
GROWTH_USES_FILENAME = "growth_uses.json"

#: Archive directory under <character_dir>.
ARCHIVE_DIRNAME = "reflection_archive"

#: growth_uses.json envelope version.
GROWTH_USES_SCHEMA = 1

#: Host bounds (B05 spec sections 1.3/1.7).
MAX_BACKOFF_KEYS = 64
MAX_GROWTH_USES = 2000

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
    id_key: str | None,
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
        if id_key is not None:
            value = item.get(id_key)
            if not isinstance(value, str) or not value.strip():
                raise CorruptStoreError(
                    f"{what}[{index}]: missing required {id_key!r}", path=path,
                )
        rows.append(dict(item))
    return rows


def _validated_rows(
    rows: Any,
    *,
    what: str,
    id_key: str | None,
) -> list[dict[str, Any]]:
    """Copy a caller payload after a shape check (caller error -> ValueError).

    The id check mirrors the reader: writing a row the reader would reject
    would create a corrupt file, so a caller bug fails here instead.
    """
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise ValueError(f"{what}: expected a sequence of JSON objects")
    payload: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"{what}[{index}]: expected a JSON object, "
                f"found {type(item).__name__}"
            )
        if id_key is not None:
            value = item.get(id_key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{what}[{index}]: missing required {id_key!r}")
        payload.append(dict(item))
    return payload


def _dedupe_by(rows: Sequence[Mapping[str, Any]], id_key: str) -> list[dict[str, Any]]:
    """Keep one row per id: later wins, first-occurrence order is kept."""
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        merged[str(row[id_key])] = dict(row)
    return list(merged.values())


# -- synth_backoff ----------------------------------------------------------


def _normalize_backoff_value(value: Any, *, key: str, path: Path) -> dict[str, Any]:
    """Normalize one backoff value; legacy {key: int} becomes {n, at: None}."""
    if isinstance(value, Mapping):
        entry = dict(value)
        attempts = entry.get("n", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise CorruptStoreError(
                f"synth backoff {key!r}: n must be a nonnegative integer, "
                f"found {attempts!r}",
                path=path,
            )
        last_failure = entry.get("at")
        if last_failure is not None and not isinstance(last_failure, str):
            raise CorruptStoreError(
                f"synth backoff {key!r}: at must be an ISO string or null, "
                f"found {type(last_failure).__name__}",
                path=path,
            )
        entry["n"] = attempts
        entry["at"] = last_failure
        return entry
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CorruptStoreError(
            f"synth backoff {key!r}: expected an attempt count or an object, "
            f"found {value!r}",
            path=path,
        )
    return {"n": value, "at": None}


def _coerce_backoff_value(value: Any, *, key: str) -> dict[str, Any]:
    """Caller-side normalization for save_backoff (ValueError on bad input)."""
    if isinstance(value, Mapping):
        entry = dict(value)
        attempts = entry.get("n", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ValueError(
                f"synth backoff {key!r}: n must be a nonnegative integer"
            )
        last_failure = entry.get("at")
        if last_failure is not None and not isinstance(last_failure, str):
            raise ValueError(f"synth backoff {key!r}: at must be an ISO string or null")
        entry["n"] = attempts
        entry["at"] = last_failure
        return entry
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"synth backoff {key!r}: expected an attempt count or an object"
        )
    return {"n": value, "at": None}


def _load_backoff(store: AtomicJsonStore, path: Path) -> dict[str, dict[str, Any]]:
    raw = _load_raw(store, path)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"synth backoff: expected a JSON object, found {type(raw).__name__}",
            path=path,
        )
    return {
        str(key): _normalize_backoff_value(value, key=str(key), path=path)
        for key, value in raw.items()
    }


# -- growth_uses ------------------------------------------------------------


def _empty_growth_doc() -> dict[str, Any]:
    return {"schema": GROWTH_USES_SCHEMA, "version": 0, "uses": []}


def _load_growth_doc(store: AtomicJsonStore, path: Path) -> dict[str, Any]:
    raw = _load_raw(store, path)
    if raw is None:
        return _empty_growth_doc()
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"growth_uses: expected a JSON object, found {type(raw).__name__}",
            path=path,
        )
    schema = raw.get("schema", GROWTH_USES_SCHEMA)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise InvalidSchemaError(
            f"growth_uses: missing or non-integer schema: {schema!r}", path=path,
        )
    if schema != GROWTH_USES_SCHEMA:
        raise InvalidSchemaError(
            f"unsupported growth_uses schema {schema}; this build supports "
            f"{GROWTH_USES_SCHEMA}",
            path=path,
        )
    version = raw.get("version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise CorruptStoreError(
            f"growth_uses: version must be a nonnegative integer, "
            f"found {version!r}",
            path=path,
        )
    uses_raw = raw.get("uses", [])
    if not isinstance(uses_raw, list):
        raise CorruptStoreError("growth_uses: uses must be a list", path=path)
    uses: list[dict[str, Any]] = []
    for index, row in enumerate(uses_raw):
        if not isinstance(row, Mapping):
            raise CorruptStoreError(
                f"growth_uses.uses[{index}]: expected a JSON object, "
                f"found {type(row).__name__}",
                path=path,
            )
        uses.append(dict(row))
    doc = dict(raw)
    doc["schema"] = GROWTH_USES_SCHEMA
    doc["version"] = version
    doc["uses"] = uses
    return doc


def _validated_growth_doc(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, Mapping):
        raise ValueError("growth_uses: expected a document object")
    schema = doc.get("schema", GROWTH_USES_SCHEMA)
    if schema is not None and schema != GROWTH_USES_SCHEMA:
        raise ValueError(f"growth_uses: unsupported schema {schema!r}")
    version = doc.get("version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError("growth_uses: version must be a nonnegative integer")
    uses_raw = doc.get("uses", [])
    if isinstance(uses_raw, (str, bytes)) or not isinstance(uses_raw, Sequence):
        raise ValueError("growth_uses: uses must be a sequence")
    uses: list[dict[str, Any]] = []
    for index, row in enumerate(uses_raw):
        if not isinstance(row, Mapping):
            raise ValueError(
                f"growth_uses.uses[{index}]: expected a JSON object, "
                f"found {type(row).__name__}"
            )
        uses.append(dict(row))
    if len(uses) > MAX_GROWTH_USES:
        uses = uses[-MAX_GROWTH_USES:]
    payload = dict(doc)
    payload["schema"] = GROWTH_USES_SCHEMA
    payload["version"] = version
    payload["uses"] = uses
    return payload


# -- store ------------------------------------------------------------------


class ReflectionStore:
    """Per-character reflection files with host shapes and strict reads.

    One instance serves every character; the per-file lock is shared
    process-wide. clock is part of the frozen wiring contract and is passed to
    the archive shard store for its archival date; these files carry their own
    timestamps and are never stamped here. archive lets the caller inject a
    shared ArchiveShardStore; when omitted one is built for
    reflection_archive.
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

    # -- reflections ----------------------------------------------------

    def load(self, character: str) -> list[dict[str, Any]]:
        """Return every stored reflection verbatim (empty when absent)."""
        path = self._path(character, REFLECTIONS_FILENAME)
        with _lock_for(path):
            return _load_array(
                self._store, path, what=REFLECTIONS_FILENAME, id_key="id",
            )

    def save(self, character: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Atomically replace reflections.json with the bare-array payload."""
        payload = _validated_rows(
            rows, what=REFLECTIONS_FILENAME, id_key="id",
        )
        path = self._path(character, REFLECTIONS_FILENAME)
        with _lock_for(path):
            # Refuse to replace a file this reader would reject: a damaged
            # reflections file must be inspected, not silently overwritten.
            _load_array(self._store, path, what=REFLECTIONS_FILENAME, id_key="id")
            self._permit.assert_writable("save_reflections", str(path))
            if self._write_guard is not None:
                self._write_guard(character, payload)
            self._store.save(path, payload)

    # -- surfaced -------------------------------------------------------

    def load_surfaced(self, character: str) -> list[dict[str, Any]]:
        """Return surfaced rows, one per reflection_id, first-seen order."""
        path = self._path(character, SURFACED_FILENAME)
        with _lock_for(path):
            rows = _load_array(
                self._store, path, what=SURFACED_FILENAME, id_key="reflection_id",
            )
        return _dedupe_by(rows, "reflection_id")

    def save_surfaced(
        self, character: str, rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Atomically replace surfaced.json, one row per reflection_id."""
        payload = _dedupe_by(
            _validated_rows(
                rows, what=SURFACED_FILENAME, id_key="reflection_id",
            ),
            "reflection_id",
        )
        path = self._path(character, SURFACED_FILENAME)
        with _lock_for(path):
            _load_array(
                self._store, path, what=SURFACED_FILENAME, id_key="reflection_id",
            )
            self._permit.assert_writable("save_surfaced", str(path))
            self._store.save(path, payload)

    # -- synth_backoff --------------------------------------------------

    def load_backoff(self, character: str) -> dict[str, dict[str, Any]]:
        """Return {key: {n, at}}; legacy {key: int} rows become {n, at: None}."""
        path = self._path(character, SYNTH_BACKOFF_FILENAME)
        with _lock_for(path):
            return _load_backoff(self._store, path)

    def save_backoff(
        self, character: str, mapping: Mapping[str, Any],
    ) -> None:
        """Atomically replace synth_backoff.json, keeping the 64 highest n."""
        if not isinstance(mapping, Mapping):
            raise ValueError("synth backoff: expected a mapping")
        normalized = {
            str(key): _coerce_backoff_value(value, key=str(key))
            for key, value in mapping.items()
        }
        if len(normalized) > MAX_BACKOFF_KEYS:
            normalized = dict(
                sorted(
                    normalized.items(),
                    key=lambda item: item[1]["n"],
                    reverse=True,
                )[:MAX_BACKOFF_KEYS]
            )
        path = self._path(character, SYNTH_BACKOFF_FILENAME)
        with _lock_for(path):
            _load_backoff(self._store, path)
            self._permit.assert_writable("save_synth_backoff", str(path))
            self._store.save(path, normalized)

    # -- growth_uses ----------------------------------------------------

    def load_growth_uses(self, character: str) -> dict[str, Any]:
        """Return the growth usage document (fresh defaults when absent)."""
        path = self._path(character, GROWTH_USES_FILENAME)
        with _lock_for(path):
            return _load_growth_doc(self._store, path)

    def save_growth_uses(
        self, character: str, doc: Mapping[str, Any],
    ) -> None:
        """Atomically replace growth_uses.json.

        uses are capped at 2000 rows keeping the newest, and version never
        moves backwards relative to the stored document (a stale in-flight
        writer must not invalidate newer injection caches).
        """
        payload = _validated_growth_doc(doc)
        path = self._path(character, GROWTH_USES_FILENAME)
        with _lock_for(path):
            current = _load_growth_doc(self._store, path)
            payload["version"] = max(
                int(payload["version"]), int(current["version"]),
            )
            self._permit.assert_writable("save_growth_uses", str(path))
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
    "GROWTH_USES_FILENAME",
    "GROWTH_USES_SCHEMA",
    "MAX_BACKOFF_KEYS",
    "MAX_GROWTH_USES",
    "REFLECTIONS_FILENAME",
    "ReflectionStore",
    "SURFACED_FILENAME",
    "SYNTH_BACKOFF_FILENAME",
]
