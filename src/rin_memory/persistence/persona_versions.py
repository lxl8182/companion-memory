"""Host-independent persona version/publish store (B11).

Publishing is a persistent act: a card revision becomes an opaque
`persona_version` token plus a per-target safe boundary and consumer receipts.
Writing the card alone is NOT a publish (design §10): the version store records
what was published, when, for which target, and which consumers have adopted
it. `persona_version` replaces the host's in-memory text sha1 with a value
derived from the persisted card revision + projection text.

On-disk contract
----------------
`<character_dir>/persona_versions.json`:

```json
{"schema_version": 1,
 "character": "demo",
 "current": {"version": "<24 hex>", "revision": 3, "target": "next_turn",
             "published_at": "<ISO>", "boundary": {...},
             "text_sha256": "<64 hex>"},
 "published": [<record>, ...],
 "consumers": {"chat": {"version": "<24 hex>", "revision": 3,
                        "seen_at": "<ISO>", "target": "next_turn",
                        "status": "active"}},
 "history": [{"action": "publish|rollback|invalidate", ...}]}
```

Records keep `version/revision/target/published_at/boundary/text_sha256`
plus the served `text`/`projection` and provenance (`request_id/actor/
previous_version/planner_version/source_refs`). Unknown fields round-trip.

A missing file is the empty document. Invalid JSON, a non-object root, an
unsupported schema, a non-object current/consumer, or non-list
published/history raises `CorruptStoreError`/`InvalidSchemaError`; `save`
refuses to replace a file its reader rejects (B02 fail-closed rule).

Writes run under one process-wide per-file RLock and follow
`WritePermit.assert_writable -> barrier filter -> AtomicJsonStore.save`.
History is bounded (`history_limit`, default 32); overflow is appended to
`persona_version_archive/` shards when the optional archive path is used.

All methods are synchronous blocking I/O; call them through
`asyncio.to_thread` from async code.
"""

from __future__ import annotations

import copy
import hashlib
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError, InvalidSchemaError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .shards import ArchiveShardStore, is_blank_file

VERSIONS_FILENAME = "persona_versions.json"
VERSIONS_ARCHIVE_DIRNAME = "persona_version_archive"
VERSIONS_SCHEMA_VERSION = 1
DEFAULT_HISTORY_LIMIT = 32

TARGETS: tuple[str, ...] = ("next_turn", "realtime", "game")

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


def empty_document(character: str = "") -> dict[str, Any]:
    """Fresh empty persona version document."""
    return {
        "schema_version": VERSIONS_SCHEMA_VERSION,
        "character": str(character or ""),
        "current": None,
        "published": [],
        "consumers": {},
        "history": [],
    }


def version_token(revision: Any, text: Any) -> str:
    """Opaque persona version token: sha1(`<revision>:<projection text>`)[:24]."""
    body = f"{int(revision or 0)}:{text if isinstance(text, str) else ''}"
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:24]


def text_sha256(text: Any) -> str:
    body = text if isinstance(text, str) else ""
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _bounded_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_record(raw: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(f"{where}: expected an object")
    record = copy.deepcopy(dict(raw))
    version = _bounded_text(record.get("version"), 64)
    if not version:
        raise CorruptStoreError(f"{where}: missing version")
    record["version"] = version
    record["revision"] = _bounded_int(record.get("revision"), 0)
    target = _bounded_text(record.get("target"), 32) or "next_turn"
    record["target"] = target
    record["published_at"] = _bounded_text(record.get("published_at"), 40) or None
    boundary = record.get("boundary")
    record["boundary"] = dict(boundary) if isinstance(boundary, Mapping) else {}
    record["text_sha256"] = _bounded_text(record.get("text_sha256"), 64)
    if "text" in record and not isinstance(record.get("text"), str):
        record["text"] = str(record.get("text") or "")
    if "projection" in record and not isinstance(record.get("projection"), Mapping):
        record["projection"] = {}
    if "source_refs" in record and not isinstance(record.get("source_refs"), list):
        record["source_refs"] = []
    return record


def _normalize_consumer(raw: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(f"{where}: expected an object")
    entry = copy.deepcopy(dict(raw))
    entry["version"] = _bounded_text(entry.get("version"), 64)
    entry["revision"] = _bounded_int(entry.get("revision"), 0)
    entry["seen_at"] = _bounded_text(entry.get("seen_at"), 40) or None
    entry["status"] = _bounded_text(entry.get("status"), 32) or "active"
    target = _bounded_text(entry.get("target"), 32)
    if target:
        entry["target"] = target
    return entry


def normalize_document(raw: Any, character: str = "") -> dict[str, Any]:
    """Validate and copy a version document; unknown fields round-trip."""
    if raw is None:
        return empty_document(character)
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"persona_versions: expected a JSON object, found {type(raw).__name__}",
        )
    schema = raw.get("schema_version", VERSIONS_SCHEMA_VERSION)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise InvalidSchemaError(
            f"persona_versions: missing or non-integer schema_version: {schema!r}",
        )
    if schema != VERSIONS_SCHEMA_VERSION:
        raise InvalidSchemaError(
            f"unsupported persona_versions schema {schema}; this build supports "
            f"{VERSIONS_SCHEMA_VERSION}",
        )
    document = copy.deepcopy(dict(raw))
    document["schema_version"] = VERSIONS_SCHEMA_VERSION
    if character:
        document.setdefault("character", character)
    current = document.get("current")
    if current is not None:
        document["current"] = _normalize_record(current, where="persona_versions.current")
    published = document.get("published", [])
    if not isinstance(published, list):
        raise CorruptStoreError(
            f"persona_versions.published: expected a list, found "
            f"{type(published).__name__}",
        )
    document["published"] = [
        _normalize_record(item, where=f"persona_versions.published[{index}]")
        for index, item in enumerate(published)
    ]
    consumers = document.get("consumers", {})
    if not isinstance(consumers, Mapping):
        raise CorruptStoreError(
            f"persona_versions.consumers: expected an object, found "
            f"{type(consumers).__name__}",
        )
    document["consumers"] = {
        str(key): _normalize_consumer(
            value, where=f"persona_versions.consumers[{key!r}]",
        )
        for key, value in consumers.items()
        if isinstance(key, str) and key
    }
    history = document.get("history", [])
    if not isinstance(history, list):
        raise CorruptStoreError(
            f"persona_versions.history: expected a list, found "
            f"{type(history).__name__}",
        )
    document["history"] = [
        copy.deepcopy(dict(item)) for item in history if isinstance(item, Mapping)
    ]
    return document


def _validated_document(document: Any, character: str) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise ValueError("persona_versions: document must be a mapping")
    return normalize_document(document, character)


def _refs(row: Mapping[str, Any]) -> list[str]:
    refs = row.get("source_refs")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    return [str(item) for item in refs if isinstance(item, str) and item]


class PersonaVersionStore:
    """Per-character persona version/publish store with bounded history."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        barrier: Any = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._barrier = barrier
        self._history_limit = max(1, int(history_limit or DEFAULT_HISTORY_LIMIT))
        self._archive: ArchiveShardStore | None = None

    # -- paths ----------------------------------------------------------

    def _path(self, character: str, *, create: bool = False) -> Path:
        return self._roots.character_dir(character, create=create) / VERSIONS_FILENAME

    @property
    def history_limit(self) -> int:
        return self._history_limit

    def _archive_store(self) -> ArchiveShardStore:
        if self._archive is None:
            self._archive = ArchiveShardStore(
                roots=self._roots, store=self._store, clock=self._clock,
                permit=self._permit, dirname=VERSIONS_ARCHIVE_DIRNAME,
            )
        return self._archive

    # -- reads ----------------------------------------------------------

    def load(self, character: str) -> dict[str, Any]:
        """Return the normalized document; corruption raises, never empty."""
        path = self._path(character)
        with _lock_for(path):
            return self._load_document(character)

    def _load_document(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        try:
            raw = self._store.load(path)
        except CorruptStoreError:
            if is_blank_file(path):
                return empty_document(character)
            raise
        return normalize_document(raw, character)

    def current(self, character: str) -> dict[str, Any] | None:
        """Return a copy of the current published record, or None."""
        document = self.load(character)
        current = document.get("current")
        return copy.deepcopy(current) if isinstance(current, Mapping) else None

    # -- writes ---------------------------------------------------------

    def save(self, character: str, document: Mapping[str, Any]) -> None:
        """Atomically replace the document after permit + barrier filtering."""
        payload = _validated_document(document, character)
        payload = self._filter_document(character, payload)
        payload = self._trim_document(character, payload)
        path = self._path(character)
        with _lock_for(path):
            self._load_document(character)  # refuse to replace a corrupt file
            self._permit.assert_writable("save_persona_versions", str(path))
            self._store.save(path, payload)

    def _filter_document(
        self, character: str, document: Mapping[str, Any],
    ) -> dict[str, Any]:
        filtered = copy.deepcopy(dict(document))
        if self._barrier is None:
            return filtered

        def blocked(row: Mapping[str, Any]) -> bool:
            if row.get("invalidated_at"):
                return True
            try:
                if self._barrier.blocks_row(character, row):
                    return True
            except Exception:  # noqa: BLE001 - defensive only
                pass
            refs = _refs(row)
            if not refs:
                return False
            # Any deleted source invalidates the published version: old text
            # must not remain consumable after a correction/deletion.
            for ref in refs:
                for target_type in ("fact", "reflection", "persona", "growth"):
                    try:
                        if self._barrier.blocks_target(character, target_type, ref):
                            return True
                    except Exception:  # noqa: BLE001
                        continue
            return False

        filtered["published"] = [
            row for row in filtered.get("published") or []
            if not blocked(row)
        ]
        filtered["history"] = [
            row for row in filtered.get("history") or []
            if not blocked(row)
        ]
        current = filtered.get("current")
        if isinstance(current, Mapping) and blocked(current):
            filtered["current"] = None
        return filtered

    def _trim_document(
        self, character: str, document: dict[str, Any],
    ) -> dict[str, Any]:
        overflow: list[dict[str, Any]] = []
        published = list(document.get("published") or [])
        if len(published) > self._history_limit:
            overflow.extend(dict(item) for item in published[:-self._history_limit])
            document["published"] = published[-self._history_limit:]
        history = list(document.get("history") or [])
        if len(history) > self._history_limit:
            overflow.extend(dict(item) for item in history[:-self._history_limit])
            document["history"] = history[-self._history_limit:]
        if overflow:
            try:
                self._archive_store().append(character, overflow)
            except Exception:  # noqa: BLE001 - archive is best effort
                pass
        return document

    # -- mutations ------------------------------------------------------

    def append_publish(
        self,
        character: str,
        record: Mapping[str, Any],
        *,
        make_current: bool = True,
    ) -> dict[str, Any]:
        """Append one publish record (and make it current); returns the document."""
        normalized = _normalize_record(record, where="publish record")
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            document["published"] = list(document.get("published") or []) + [normalized]
            if make_current:
                document["current"] = copy.deepcopy(normalized)
            document["history"] = list(document.get("history") or []) + [{
                "action": "publish",
                "version": normalized["version"],
                "revision": normalized["revision"],
                "target": normalized["target"],
                "at": self._clock.now().isoformat(),
            }]
            self.save(character, document)
            return self._load_document(character)

    def set_current(
        self,
        character: str,
        record: Mapping[str, Any],
        *,
        action: str = "rollback",
    ) -> dict[str, Any]:
        """Make an existing record current again (rollback); append history."""
        normalized = _normalize_record(record, where="rollback record")
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            document["current"] = copy.deepcopy(normalized)
            document["history"] = list(document.get("history") or []) + [{
                "action": action,
                "version": normalized["version"],
                "revision": normalized["revision"],
                "target": normalized["target"],
                "at": self._clock.now().isoformat(),
            }]
            self.save(character, document)
            return self._load_document(character)

    def mark_consumer(
        self,
        character: str,
        consumer: str,
        *,
        version: str,
        revision: int,
        seen_at: str | None = None,
        status: str = "active",
        target: str | None = None,
    ) -> dict[str, Any]:
        """Record which version one consumer has adopted (or superseded)."""
        name = _bounded_text(consumer, 120)
        if not name:
            raise ValueError("consumer must be nonempty")
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            consumers = dict(document.get("consumers") or {})
            entry: dict[str, Any] = {
                "version": _bounded_text(version, 64),
                "revision": _bounded_int(revision, 0),
                "seen_at": seen_at or self._clock.now().isoformat(),
                "status": _bounded_text(status, 32) or "active",
            }
            if target:
                entry["target"] = _bounded_text(target, 32)
            consumers[name] = entry
            document["consumers"] = consumers
            self.save(character, document)
            return self._load_document(character)

    def purge_invalidated(
        self, character: str, versions: Sequence[str],
    ) -> dict[str, Any]:
        """Remove invalidated versions from current/published/history."""
        wanted = {str(item) for item in versions or () if isinstance(item, str) and item}
        if not wanted:
            return self.load(character)
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            current = document.get("current")
            if isinstance(current, Mapping) and current.get("version") in wanted:
                document["current"] = None
            document["published"] = [
                row for row in document.get("published") or []
                if row.get("version") not in wanted
            ]
            document["history"] = [
                row for row in document.get("history") or []
                if row.get("version") not in wanted
            ]
            consumers = dict(document.get("consumers") or {})
            for name, entry in list(consumers.items()):
                if not isinstance(entry, Mapping):
                    continue
                if entry.get("version") in wanted:
                    updated = dict(entry)
                    updated["status"] = "superseded"
                    updated["seen_at"] = self._clock.now().isoformat()
                    consumers[name] = updated
            document["consumers"] = consumers
            document["history"] = list(document.get("history") or []) + [{
                "action": "invalidate",
                "versions": sorted(wanted),
                "at": self._clock.now().isoformat(),
            }]
            self.save(character, document)
            return self._load_document(character)

    # -- archive --------------------------------------------------------

    def archive_history(
        self, character: str, rows: Sequence[Mapping[str, Any]],
    ) -> str:
        entries = [dict(item) for item in rows]
        if not entries:
            return ""
        return self._archive_store().append(character, entries)

    def load_archive(self, character: str) -> list[dict[str, Any]]:
        return self._archive_store().read_all(character)


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "TARGETS",
    "VERSIONS_ARCHIVE_DIRNAME",
    "VERSIONS_FILENAME",
    "VERSIONS_SCHEMA_VERSION",
    "PersonaVersionStore",
    "empty_document",
    "normalize_document",
    "text_sha256",
    "version_token",
]
