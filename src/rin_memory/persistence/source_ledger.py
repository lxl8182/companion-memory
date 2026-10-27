"""Host-compatible per-character source event ledger (design §17.3, B02).

The ledger answers "has this source key already been committed for this
subject?" and remembers the stable host/mirror IDs of that commit. It is a
bounded hint table, not a log: one row per source key, at most
`MAX_ENTRIES` rows per character.

Invariants:
  * one row per (subject, source_key); a snapshot that reuses a source key
    with a different digest is a conflict, never an overwrite;
  * the newest `MAX_ENTRIES` rows by ingestion time survive a trim;
  * a missing file is a fresh ledger, while unparsable content or an unknown
    schema raises and leaves the file exactly as found;
  * every save passes the injected WritePermit first;
  * each read-modify-write cycle runs under one `threading.Lock` per ledger
    file, so two threads never lose each other's rows and there is no global
    data lock.

On-disk shape is the host's `source_events.json`
(`{"schema": 1, "entries": {<source_key>: <entry>}}`) so the N.E.K.O build
keeps reading and writing it; `schema_version`/`events` are accepted as read
aliases because the core's other stores use that envelope. Host fields
`ingested`, `pending_since`, `attempts`, `host_ids`, `mirror_ids`,
`scene` and `content_class` keep their host names and types; `subject` and
`fingerprint` are additive fields the host ignores.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import Scope
from ..errors import CorruptStoreError, InvalidSchemaError, SourceConflictError, StoreError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .records import fingerprint

LEDGER_FILENAME = "source_events.json"
LEDGER_SCHEMA = 1
MAX_ENTRIES = 2000
MAX_IDS_PER_ENTRY = 16
_SUPPORTED_SCHEMAS = frozenset({LEDGER_SCHEMA})
#: Snapshot fields that may change without changing the event's meaning (retries).
_VOLATILE_SNAPSHOT_FIELDS = ("event_id", "received_at", "request_id")

#: One lock per ledger file; created lazily, never held across an await (none here).
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
class LedgerEntry:
    """One ledger row: what the ledger remembers about a committed source key.

    `subject` is the scope subject the commit belonged to and `fingerprint`
    is the digest of the snapshot's meaning (volatile receipt fields
    excluded), so a retry is a duplicate while changed content is a conflict.
    Legacy host rows carry neither field (both empty) and match on
    `source_key` alone.
    """

    source_key: str
    subject: str = ""
    fingerprint: str = ""
    ingested: bool = False
    pending_since: float | None = None
    attempts: int = 0
    host_ids: tuple[str, ...] = ()
    mirror_ids: tuple[str, ...] = ()
    scene: Mapping[str, str] | None = None
    content_class: str | None = None


class SourceLedger:
    """Per-character source key ledger (see the module docstring)."""

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

    # -- paths -----------------------------------------------------------

    def _path(self, character: str) -> Path:
        return self._roots.character_dir(character, create=False).joinpath(LEDGER_FILENAME)

    # -- document shape --------------------------------------------------

    def _load_entries(self, path: Path) -> dict[str, dict[str, Any]]:
        """Load and fully validate the ledger; never writes."""
        raw = self._store.load(path)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise CorruptStoreError(
                f"expected a JSON object, found {type(raw).__name__}", path=path,
            )
        version = raw.get("schema_version")
        if version is None:
            version = raw.get("schema")
        if isinstance(version, bool) or not isinstance(version, int):
            raise InvalidSchemaError(
                f"missing or non-integer schema/schema_version: {version!r}", path=path,
            )
        if version not in _SUPPORTED_SCHEMAS:
            raise InvalidSchemaError(
                f"unsupported schema {version}; this build supports "
                f"{sorted(_SUPPORTED_SCHEMAS)}", path=path,
            )
        entries = raw.get("entries")
        if entries is None:
            entries = raw.get("events")
        if not isinstance(entries, dict):
            raise CorruptStoreError("ledger document has no 'entries' object", path=path)
        checked: dict[str, dict[str, Any]] = {}
        for key, entry in entries.items():
            if not isinstance(key, str) or not key.strip():
                raise CorruptStoreError(f"invalid source key {key!r}", path=path)
            if not isinstance(entry, dict):
                raise CorruptStoreError(
                    f"entry {key!r} is not an object, found {type(entry).__name__}",
                    path=path,
                )
            stored_key = entry.get("source_key")
            if not isinstance(stored_key, str) or stored_key != key:
                raise CorruptStoreError(
                    f"entry {key!r} carries source_key {stored_key!r}", path=path,
                )
            self._entry_to_ledger(key, entry, path)
            checked[key] = entry
        return checked

    @staticmethod
    def _entry_to_ledger(
        key: str, raw: Mapping[str, Any], path: Path | None,
    ) -> LedgerEntry:
        """Validate one stored row and return its typed form."""
        subject = raw.get("subject")
        if subject is None:
            subject = ""
        if not isinstance(subject, str):
            raise CorruptStoreError(f"entry {key!r}: subject must be a string", path=path)
        digest = raw.get("fingerprint")
        if digest is None:
            digest = ""
        if not isinstance(digest, str):
            raise CorruptStoreError(f"entry {key!r}: fingerprint must be a string", path=path)
        ingested = raw.get("ingested", False)
        if not isinstance(ingested, bool):
            raise CorruptStoreError(f"entry {key!r}: ingested must be a boolean", path=path)
        pending_since = raw.get("pending_since")
        if pending_since is not None and (
            isinstance(pending_since, bool) or not isinstance(pending_since, (int, float))
        ):
            raise CorruptStoreError(
                f"entry {key!r}: pending_since must be a number or null", path=path,
            )
        attempts = raw.get("attempts", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise CorruptStoreError(
                f"entry {key!r}: attempts must be a nonnegative integer", path=path,
            )
        scene_raw = raw.get("scene")
        scene: dict[str, str] | None = None
        if scene_raw is not None:
            if not isinstance(scene_raw, Mapping) or not all(
                isinstance(name, str) and isinstance(value, str)
                for name, value in scene_raw.items()
            ):
                raise CorruptStoreError(
                    f"entry {key!r}: scene must map strings to strings", path=path,
                )
            scene = dict(scene_raw)
        content_class = raw.get("content_class")
        if content_class is not None and not isinstance(content_class, str):
            raise CorruptStoreError(
                f"entry {key!r}: content_class must be a string or null", path=path,
            )
        return LedgerEntry(
            source_key=key,
            subject=subject,
            fingerprint=digest,
            ingested=ingested,
            pending_since=None if pending_since is None else float(pending_since),
            attempts=attempts,
            host_ids=_id_tuple(raw.get("host_ids"), key, "host_ids", path),
            mirror_ids=_id_tuple(raw.get("mirror_ids"), key, "mirror_ids", path),
            scene=scene,
            content_class=content_class,
        )

    @staticmethod
    def _new_entry(entry: LedgerEntry, now: float) -> dict[str, Any]:
        """Build the stored row of a newly committed snapshot."""
        row: dict[str, Any] = {
            "source_key": entry.source_key,
            "subject": entry.subject,
            "fingerprint": entry.fingerprint,
            "first_seen_at": now,
            "last_seen_at": now,
            "count": 1,
            "ingested": True,
            "pending_since": None,
            "attempts": entry.attempts,
            "host_ids": list(entry.host_ids),
            "mirror_ids": list(entry.mirror_ids),
        }
        if entry.scene:
            row["scene"] = dict(entry.scene)
        if entry.content_class:
            row["content_class"] = entry.content_class
        return row

    @classmethod
    def _apply_entry(
        cls,
        entries: dict[str, dict[str, Any]],
        entry: LedgerEntry,
        *,
        now: float,
        path: Path | None,
    ) -> bool:
        """Merge one entry into a ledger view; return whether the view changed.

        A conflicting row is left untouched (the caller decides whether that
        is an error); an identical retry is a no-op; a legacy row without
        `subject`/`fingerprint` adopts them while keeping its host fields.
        """
        existing = entries.get(entry.source_key)
        if existing is None:
            entries[entry.source_key] = cls._new_entry(entry, now)
            return True
        if not isinstance(existing, Mapping):
            raise CorruptStoreError(
                f"entry {entry.source_key!r} is not an object", path=path,
            )
        stored = cls._entry_to_ledger(entry.source_key, existing, path)
        if _conflicts(stored, entry):
            return False
        if stored.subject == entry.subject and stored.fingerprint == entry.fingerprint:
            return False
        existing["subject"] = entry.subject
        existing["fingerprint"] = entry.fingerprint
        existing["ingested"] = True
        existing["pending_since"] = None
        existing["last_seen_at"] = now
        _merge_ids(existing, "host_ids", entry.host_ids)
        _merge_ids(existing, "mirror_ids", entry.mirror_ids)
        return True

    @staticmethod
    def _trim(entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Keep the newest `MAX_ENTRIES` rows by ingestion time."""
        if len(entries) <= MAX_ENTRIES:
            return entries
        ordered = sorted(
            entries.items(), key=lambda item: _ingested_at(item[1]), reverse=True,
        )
        return dict(ordered[:MAX_ENTRIES])

    @staticmethod
    def _document(entries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        return {"schema": LEDGER_SCHEMA, "entries": dict(entries)}

    def _now(self) -> float:
        return self._clock.now().timestamp()

    # -- public API ------------------------------------------------------

    def lookup(self, scope: Scope, source_key: str) -> LedgerEntry | None:
        """Return the row for this scope's source key, or None.

        A row that records a subject must match it; a legacy host row without
        a subject matches on `source_key` alone.
        """
        if not isinstance(source_key, str) or not source_key.strip():
            raise ValueError("source_key must be a nonempty string")
        path = self._path(scope.character)
        with _lock_for(path):
            entries = self._load_entries(path)
            raw = entries.get(source_key)
            if raw is None:
                return None
            entry = self._entry_to_ledger(source_key, raw, path)
        if entry.subject and entry.subject != scope.subject:
            return None
        return entry

    def record_snapshot(self, character: str, snapshot: Mapping[str, Any]) -> LedgerEntry:
        """Upsert one committed event snapshot under the per-character lock.

        Idempotent by (subject, source_key): an identical snapshot is a no-op
        and returns the stored row. A different digest for the same source key
        raises SourceConflictError without touching the file.
        """
        entry = _entry_from_snapshot(snapshot)
        path = self._path(character)
        with _lock_for(path):
            entries = self._load_entries(path)
            existing = entries.get(entry.source_key)
            if existing is not None and _conflicts(
                self._entry_to_ledger(entry.source_key, existing, path), entry,
            ):
                raise SourceConflictError(
                    f"source key {entry.source_key!r} already belongs to a "
                    f"different event for subject "
                    f"{(existing.get('subject') or entry.subject)!r}"
                )
            if self._apply_entry(entries, entry, now=self._now(), path=path):
                self._permit.assert_writable("save", str(path))
                self._store.save(path, self._document(self._trim(entries)))
            return self._entry_to_ledger(entry.source_key, entries[entry.source_key], path)

    def load_view(self, character: str) -> dict[str, dict[str, Any]]:
        """Load the ledger as a mutable view for the journal replay cycle.

        The journal calls `record_and_save(load_view, mutate_view, save_view)`:
        `mutate_view` calls :meth:`apply_snapshot` on this mapping and
        `save_view` persists it. Validation happens here, before the journal
        appends anything.
        """
        path = self._path(character)
        with _lock_for(path):
            entries = self._load_entries(path)
        return {key: dict(entry) for key, entry in entries.items()}

    def apply_snapshot(
        self, view: dict[str, dict[str, Any]], snapshot: Mapping[str, Any],
    ) -> bool:
        """Idempotently merge one snapshot into a loaded view; never raises.

        Returns whether the document changed. A malformed snapshot or a stored
        row that conflicts with it leaves the view untouched and returns False:
        this runs inside the journal's replay handler, and raising would stall
        recovery for the whole character. Conflict detection for the live path
        is `record_snapshot` / the caller's `lookup` before appending.
        """
        if not isinstance(view, dict) or not isinstance(snapshot, Mapping):
            return False
        try:
            entry = _entry_from_snapshot(snapshot)
            return self._apply_entry(view, entry, now=self._now(), path=None)
        except (StoreError, ValueError, TypeError):
            return False

    def save_view(self, character: str, view: Mapping[str, Mapping[str, Any]]) -> None:
        """Persist a journal view (validated, trimmed, permit-checked)."""
        entries: dict[str, dict[str, Any]] = {}
        for key, entry in view.items():
            if not isinstance(key, str) or not isinstance(entry, Mapping):
                raise ValueError("ledger view must map source keys to objects")
            entries[key] = dict(entry)
        path = self._path(character)
        with _lock_for(path):
            for key, entry in entries.items():
                self._entry_to_ledger(key, entry, path)
            self._permit.assert_writable("save", str(path))
            self._store.save(path, self._document(self._trim(entries)))

    def remove(self, character: str, source_keys: Sequence[str]) -> int:
        """Delete ledger rows for deleted source keys; returns rows removed.

        The ledger is an idempotency hint, not content, but a deleted source's
        hint would let a retried event look like a harmless duplicate instead
        of a rejected re-ingest. Deleting the hint keeps the two answers
        consistent ("this source is deleted", not "already seen").
        """
        keys = [
            key for key in source_keys or ()
            if isinstance(key, str) and key.strip()
        ]
        if not keys:
            return 0
        path = self._path(character)
        with _lock_for(path):
            entries = self._load_entries(path)
            removed = [key for key in keys if key in entries]
            if not removed:
                return 0
            for key in removed:
                entries.pop(key, None)
            self._permit.assert_writable("delete", str(path))
            self._store.save(path, self._document(self._trim(entries)))
            return len(removed)

    def count(self, character: str) -> int:
        """Number of ledger rows currently stored for this character."""
        path = self._path(character)
        with _lock_for(path):
            return len(self._load_entries(path))

    def snapshot(self, character: str) -> Mapping[str, Mapping[str, Any]]:
        """Read-only copy of every stored row, keyed by source key."""
        return self.load_view(character)


def _conflicts(stored: LedgerEntry, incoming: LedgerEntry) -> bool:
    """Whether an existing row contradicts the incoming snapshot."""
    if stored.subject and stored.subject != incoming.subject:
        return True
    return bool(stored.fingerprint and stored.fingerprint != incoming.fingerprint)


def _ingested_at(raw: Mapping[str, Any]) -> float:
    for field_name in ("first_seen_at", "last_seen_at"):
        value = raw.get(field_name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _id_tuple(value: Any, key: str, field_name: str, path: Path | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise CorruptStoreError(f"entry {key!r}: {field_name} must be a list", path=path)
    if not all(isinstance(item, str) for item in value):
        raise CorruptStoreError(
            f"entry {key!r}: {field_name} must contain strings", path=path,
        )
    return tuple(value[-MAX_IDS_PER_ENTRY:])


def _merge_ids(row: dict[str, Any], field_name: str, values: tuple[str, ...]) -> None:
    current = row.get(field_name)
    ids = list(current) if isinstance(current, list) else []
    for value in values:
        if value not in ids:
            ids.append(value)
    row[field_name] = ids[-MAX_IDS_PER_ENTRY:]


def _snapshot_ids(snapshot: Mapping[str, Any], field_name: str) -> tuple[str, ...]:
    value = snapshot.get(field_name)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(
            item for item in value if isinstance(item, str) and item.strip()
        )[-MAX_IDS_PER_ENTRY:]
    raise ValueError(f"event snapshot {field_name} must be a string or a list")


def _snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Digest of the snapshot's meaning, ignoring retry-only fields."""
    semantic = {
        name: value for name, value in snapshot.items()
        if name not in _VOLATILE_SNAPSHOT_FIELDS
    }
    return fingerprint(semantic)


def _entry_from_snapshot(snapshot: Mapping[str, Any]) -> LedgerEntry:
    """Turn a `records.event_snapshot` document into a committed ledger row."""
    if not isinstance(snapshot, Mapping):
        raise ValueError("event snapshot must be a mapping")
    source_key = snapshot.get("source_key")
    if not isinstance(source_key, str) or not source_key.strip():
        raise ValueError("event snapshot source_key must be a nonempty string")
    scope = snapshot.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("event snapshot is missing its scope")
    subject = scope.get("subject", "private")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("event snapshot subject must be a nonempty string")
    scene_raw = snapshot.get("scene")
    scene: dict[str, str] | None = None
    if isinstance(scene_raw, Mapping):
        scene = {
            name: value for name, value in scene_raw.items()
            if isinstance(name, str) and isinstance(value, str) and value.strip()
        } or None
    content_class = snapshot.get("content_class")
    if content_class is not None and not isinstance(content_class, str):
        raise ValueError("event snapshot content_class must be a string or null")
    host_ids = _snapshot_ids(snapshot, "host_ids")
    event_id = snapshot.get("event_id")
    if not host_ids and isinstance(event_id, str) and event_id.strip():
        host_ids = (event_id,)
    try:
        digest = _snapshot_fingerprint(snapshot)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"event snapshot is not JSON-serialisable: {exc}") from exc
    return LedgerEntry(
        source_key=source_key,
        subject=subject,
        fingerprint=digest,
        ingested=True,
        pending_since=None,
        attempts=1,
        host_ids=host_ids,
        mirror_ids=_snapshot_ids(snapshot, "mirror_ids"),
        scene=scene,
        content_class=content_class,
    )


__all__ = [
    "LEDGER_FILENAME",
    "LEDGER_SCHEMA",
    "MAX_ENTRIES",
    "LedgerEntry",
    "SourceLedger",
]
