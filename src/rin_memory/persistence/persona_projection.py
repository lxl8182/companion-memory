"""Host-independent persona projection store (C02).

The projection is the engine-to-host compatibility view: a revision-stamped
snapshot of the current semantic card plus per-consumer adoption state. The
host keeps resources/tools/voice in characters.json; this store only records
what the engine considers authoritative and what each generation entry has
actually adopted.

On-disk contract
----------------
<character_dir>/persona_projection.json:

{
  "schema_version": 1,
  "character": "demo",
  "card_revision": 3,
  "version": "<publish token or card_...>",
  "projection": {"section_id": "text"},
  "text_sha256": "<sha256 of joined text>",
  "updated_at": "<ISO>",
  "consumers": {
    "chat": {"version": "...", "revision": 3, "status": "adopted",
             "receipt_id": "...", "attempts": 0, "updated_at": "...",
             "reason": null}
  },
  "pending": [{"consumer": "active", "version": "...", "revision": 3,
               "reason": "publish", "created_at": "..."}],
  "failed": [{"consumer": "game", "version": "...", "revision": 3,
              "reason": "timeout", "attempts": 1, "updated_at": "..."}],
  "history": [{"action": "sync", "version": "...", "revision": 3,
               "at": "..."}],
  "host_import": {"imported_at": "...", "base_revision": 0,
                  "fingerprint": "...", "section_ids": [...]}
}

A missing file is the empty document. Invalid JSON, a non-object root, an
unsupported schema, a non-object consumer/pending/failed row or a bad status
raises CorruptStoreError/InvalidSchemaError; save refuses to replace a file its
reader rejects. Writes run under one process-wide per-file RLock and follow
WritePermit.assert_writable -> deletion-barrier filter -> AtomicJsonStore.save.
History is bounded (history_limit, default 32); unknown fields round-trip.

All methods are synchronous blocking I/O; call them through asyncio.to_thread
from async code.
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
from .shards import is_blank_file

PROJECTION_FILENAME = "persona_projection.json"
PROJECTION_SCHEMA_VERSION = 1
DEFAULT_HISTORY_LIMIT = 32

CONSUMERS: tuple[str, ...] = ("chat", "active", "game", "realtime")
CONSUMER_STATUSES: frozenset[str] = frozenset({
    "saved", "pending", "published", "adopted", "failed",
})

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


def _text(value: Any, limit: int = 0) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text[:limit] if limit else text


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in out:
            out.append(item)
    return out


def text_sha256(text: Any) -> str:
    body = text if isinstance(text, str) else ""
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()


def empty_document(character: str = "") -> dict[str, Any]:
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "character": str(character or ""),
        "card_revision": 0,
        "version": "",
        "projection": {},
        "text_sha256": "",
        "updated_at": None,
        "consumers": {},
        "pending": [],
        "failed": [],
        "history": [],
        "host_import": {},
    }


def _normalize_consumer(raw: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(f"{where}: expected an object")
    row = copy.deepcopy(dict(raw))
    row["version"] = _text(row.get("version"), 120)
    row["revision"] = _int(row.get("revision"), 0)
    status = _text(row.get("status"), 32) or "saved"
    if status not in CONSUMER_STATUSES:
        raise CorruptStoreError(f"{where}: invalid status {status!r}")
    row["status"] = status
    receipt = row.get("receipt_id")
    row["receipt_id"] = _text(receipt, 120) or None
    row["attempts"] = max(0, _int(row.get("attempts"), 0))
    row["updated_at"] = _text(row.get("updated_at"), 40) or None
    reason = row.get("reason")
    row["reason"] = _text(reason, 400) or None
    return row


def _normalize_queue_row(raw: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(f"{where}: expected an object")
    row = copy.deepcopy(dict(raw))
    consumer = _text(row.get("consumer"), 64)
    if not consumer:
        raise CorruptStoreError(f"{where}: missing consumer")
    row["consumer"] = consumer
    row["version"] = _text(row.get("version"), 120)
    row["revision"] = _int(row.get("revision"), 0)
    row["reason"] = _text(row.get("reason"), 400) or None
    row["attempts"] = max(0, _int(row.get("attempts"), 0))
    row["created_at"] = _text(row.get("created_at"), 40) or None
    row["updated_at"] = _text(row.get("updated_at"), 40) or None
    row["receipt_id"] = _text(row.get("receipt_id"), 120) or None
    if "source_refs" in row:
        row["source_refs"] = _str_list(row.get("source_refs"))
    return row


def _normalize_history_row(raw: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(f"{where}: expected an object")
    row = copy.deepcopy(dict(raw))
    row["action"] = _text(row.get("action"), 64) or "unknown"
    row["version"] = _text(row.get("version"), 120)
    row["revision"] = _int(row.get("revision"), 0)
    row["at"] = _text(row.get("at"), 40) or None
    consumer = _text(row.get("consumer"), 64)
    if consumer:
        row["consumer"] = consumer
    reason = row.get("reason")
    row["reason"] = _text(reason, 400) or None
    return row


def normalize_document(raw: Any, character: str) -> dict[str, Any]:
    if raw is None:
        return empty_document(character)
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"persona_projection: expected a JSON object, found {type(raw).__name__}",
        )
    schema = raw.get("schema_version", PROJECTION_SCHEMA_VERSION)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise InvalidSchemaError(
            f"persona_projection: missing or non-integer schema_version: {schema!r}",
        )
    if schema != PROJECTION_SCHEMA_VERSION:
        raise InvalidSchemaError(
            f"unsupported persona_projection schema {schema}; this build "
            f"supports {PROJECTION_SCHEMA_VERSION}",
        )
    document = copy.deepcopy(dict(raw))
    stored_character = _text(document.get("character"), 200)
    if stored_character and stored_character != character:
        raise CorruptStoreError(
            f"persona_projection: character {stored_character!r} does not "
            f"match {character!r}",
        )
    document["schema_version"] = PROJECTION_SCHEMA_VERSION
    document["character"] = character
    document["card_revision"] = max(0, _int(document.get("card_revision"), 0))
    document["version"] = _text(document.get("version"), 120)
    projection = document.get("projection", {})
    if not isinstance(projection, Mapping):
        raise CorruptStoreError("persona_projection.projection: expected an object")
    document["projection"] = {
        str(key): str(value) for key, value in projection.items()
        if isinstance(key, str) and key
    }
    document["text_sha256"] = _text(document.get("text_sha256"), 64)
    document["updated_at"] = _text(document.get("updated_at"), 40) or None
    consumers = document.get("consumers", {})
    if not isinstance(consumers, Mapping):
        raise CorruptStoreError("persona_projection.consumers: expected an object")
    document["consumers"] = {
        str(key): _normalize_consumer(
            value, where=f"persona_projection.consumers[{key!r}]",
        )
        for key, value in consumers.items()
        if isinstance(key, str) and key
    }
    for key in ("pending", "failed", "history"):
        rows = document.get(key, [])
        if not isinstance(rows, list):
            raise CorruptStoreError(
                f"persona_projection.{key}: expected a list, found "
                f"{type(rows).__name__}",
            )
        if key == "history":
            document[key] = [
                _normalize_history_row(item, where=f"persona_projection.history[{index}]")
                for index, item in enumerate(rows)
            ]
        else:
            document[key] = [
                _normalize_queue_row(item, where=f"persona_projection.{key}[{index}]")
                for index, item in enumerate(rows)
            ]
    host_import = document.get("host_import", {})
    document["host_import"] = (
        copy.deepcopy(dict(host_import)) if isinstance(host_import, Mapping) else {}
    )
    return document


def _validated_document(document: Any, character: str) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise ValueError("persona_projection: document must be a mapping")
    return normalize_document(document, character)


def _refs(row: Mapping[str, Any]) -> list[str]:
    refs = row.get("source_refs")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    return [str(item) for item in refs if isinstance(item, str) and item]


class PersonaProjectionStore:
    """Per-character projection and consumer adoption store."""

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

    def _path(self, character: str, *, create: bool = False) -> Path:
        return self._roots.character_dir(character, create=create) / PROJECTION_FILENAME

    @property
    def history_limit(self) -> int:
        return self._history_limit

    # -- reads ----------------------------------------------------------

    def load(self, character: str) -> dict[str, Any]:
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
        document = self.load(character)
        if not document.get("version") or not document.get("projection"):
            return None
        return {
            "character": character,
            "card_revision": int(document.get("card_revision") or 0),
            "version": str(document.get("version") or ""),
            "projection": copy.deepcopy(document.get("projection") or {}),
            "text_sha256": str(document.get("text_sha256") or ""),
            "updated_at": document.get("updated_at"),
        }

    # -- writes ---------------------------------------------------------

    def save(self, character: str, document: Mapping[str, Any]) -> dict[str, Any]:
        payload = _validated_document(document, character)
        payload = self._filter(character, payload)
        payload = self._trim(payload)
        path = self._path(character)
        with _lock_for(path):
            self._load_document(character)  # refuse to replace a corrupt file
            self._permit.assert_writable("save_persona_projection", str(path))
            self._store.save(path, payload)
        return payload

    def _blocked(self, character: str, ref: str) -> bool:
        if self._barrier is None or not ref:
            return False
        for target_type in ("fact", "reflection", "persona", "growth"):
            try:
                if self._barrier.blocks_target(character, target_type, ref):
                    return True
            except Exception:  # noqa: BLE001 - defensive filter only
                continue
        return False

    def _filter(self, character: str, document: dict[str, Any]) -> dict[str, Any]:
        if self._barrier is None:
            return document
        projection = document.get("projection") or {}
        kept: dict[str, str] = {}
        for section_id, text in projection.items():
            if self._blocked(character, section_id):
                continue
            try:
                if self._barrier.blocks_text(character, text):
                    continue
            except Exception:  # noqa: BLE001
                pass
            kept[str(section_id)] = str(text)
        if kept != projection:
            document["projection"] = kept
            document["text_sha256"] = text_sha256("\n\n".join(kept.values()))
            if not kept:
                document["version"] = ""
                document["card_revision"] = 0
        for key in ("pending", "failed"):
            rows: list[dict[str, Any]] = []
            for row in document.get(key) or []:
                refs = _refs(row)
                if refs and any(self._blocked(character, ref) for ref in refs):
                    continue
                rows.append(row)
            document[key] = rows
        return document

    def _trim(self, document: dict[str, Any]) -> dict[str, Any]:
        history = list(document.get("history") or [])
        if len(history) > self._history_limit:
            document["history"] = history[-self._history_limit:]
        return document

    # -- consumer operations -------------------------------------------

    @staticmethod
    def _consumer_name(consumer: Any) -> str:
        name = _text(consumer, 64)
        if not name:
            raise ValueError("consumer must be nonempty")
        if name not in CONSUMERS:
            raise ValueError(
                f"unknown consumer {name!r}; expected one of {CONSUMERS}",
            )
        return name

    def update_consumer(
        self,
        character: str,
        consumer: str,
        *,
        version: str,
        revision: int,
        status: str,
        receipt_id: str | None = None,
        reason: str | None = None,
        attempts: int | None = None,
    ) -> dict[str, Any]:
        name = self._consumer_name(consumer)
        if status not in CONSUMER_STATUSES:
            raise ValueError(f"invalid consumer status {status!r}")
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            consumers = dict(document.get("consumers") or {})
            existing = consumers.get(name) or {}
            row = {
                "version": _text(version, 120),
                "revision": _int(revision, 0),
                "status": status,
                "receipt_id": _text(receipt_id, 120) or None,
                "attempts": max(
                    0, _int(attempts, _int(existing.get("attempts"), 0)),
                ),
                "updated_at": self._clock.now().isoformat(),
                "reason": _text(reason, 400) or None,
            }
            consumers[name] = row
            document["consumers"] = consumers
            self.save(character, document)
            return self._load_document(character)

    def mark_pending(
        self,
        character: str,
        consumer: str,
        version: str,
        reason: str,
        *,
        revision: int | None = None,
    ) -> dict[str, Any]:
        name = self._consumer_name(consumer)
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            now = self._clock.now().isoformat()
            pending = [
                row for row in document.get("pending") or []
                if not (
                    row.get("consumer") == name
                    and row.get("version") == str(version or "")
                )
            ]
            pending.append({
                "consumer": name,
                "version": _text(version, 120),
                "revision": _int(revision, 0),
                "reason": _text(reason, 400) or None,
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
            })
            document["pending"] = pending
            consumers = dict(document.get("consumers") or {})
            existing = consumers.get(name) or {}
            consumers[name] = {
                "version": _text(version, 120),
                "revision": _int(revision, _int(existing.get("revision"), 0)),
                "status": "pending",
                "receipt_id": existing.get("receipt_id"),
                "attempts": max(0, _int(existing.get("attempts"), 0)),
                "updated_at": now,
                "reason": _text(reason, 400) or None,
            }
            document["consumers"] = consumers
            self.save(character, document)
            return self._load_document(character)

    def mark_failed(
        self,
        character: str,
        consumer: str,
        version: str,
        reason: str,
        *,
        receipt_id: str | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        name = self._consumer_name(consumer)
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            now = self._clock.now().isoformat()
            document["pending"] = [
                row for row in document.get("pending") or []
                if not (
                    row.get("consumer") == name
                    and row.get("version") == str(version or "")
                )
            ]
            failed = list(document.get("failed") or [])
            previous = next(
                (
                    row for row in failed
                    if row.get("consumer") == name
                    and row.get("version") == str(version or "")
                ),
                None,
            )
            attempts = max(0, _int(previous.get("attempts"), 0)) + 1 if previous else 1
            failed = [
                row for row in failed
                if not (
                    row.get("consumer") == name
                    and row.get("version") == str(version or "")
                )
            ]
            failed.append({
                "consumer": name,
                "version": _text(version, 120),
                "revision": _int(revision, 0),
                "reason": _text(reason, 400) or None,
                "attempts": attempts,
                "created_at": (
                    previous.get("created_at") if previous else now
                ),
                "updated_at": now,
                "receipt_id": _text(receipt_id, 120) or None,
            })
            document["failed"] = failed
            consumers = dict(document.get("consumers") or {})
            existing = consumers.get(name) or {}
            consumers[name] = {
                "version": _text(version, 120),
                "revision": _int(revision, _int(existing.get("revision"), 0)),
                "status": "failed",
                "receipt_id": _text(receipt_id, 120) or None,
                "attempts": attempts,
                "updated_at": now,
                "reason": _text(reason, 400) or None,
            }
            document["consumers"] = consumers
            self.save(character, document)
            return self._load_document(character)

    def clear(
        self, character: str, consumer: str | None = None,
    ) -> dict[str, Any]:
        name = self._consumer_name(consumer) if consumer is not None else None
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            for key in ("pending", "failed"):
                rows = [
                    row for row in document.get(key) or []
                    if name is not None and row.get("consumer") != name
                ]
                document[key] = rows
            self.save(character, document)
            return self._load_document(character)

    def append_history(
        self, character: str, entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(entry, Mapping):
            raise ValueError("history entry must be a mapping")
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            history = list(document.get("history") or [])
            history.append(copy.deepcopy(dict(entry)))
            document["history"] = history
            self.save(character, document)
            return self._load_document(character)


__all__ = [
    "CONSUMERS",
    "CONSUMER_STATUSES",
    "DEFAULT_HISTORY_LIMIT",
    "PROJECTION_FILENAME",
    "PROJECTION_SCHEMA_VERSION",
    "PersonaProjectionStore",
    "empty_document",
    "normalize_document",
    "text_sha256",
]
