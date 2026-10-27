"""Whole-turn commit records: one durable row per business request (S02).

`docs/standalone-service.md` §3.4 requires that a turn's messages, source, the
persona version used and its extraction intent are persisted as **one** event,
so a crash anywhere between "request registered" and "response returned" can be
finished forward instead of re-generating or losing work. This store is that
ledger.

Design boundaries, deliberately narrow:

* one file per character, `turn_requests.json`, versioned and fail-closed like
  every other store (a damaged file is never read as empty; a newer schema is
  refused before any write);
* the record is keyed by `request_id`, which is the *business* idempotency key
  (client-supplied, stable across retries). `source_key`/`turn_id` dedupe the
  business source underneath it, which is what lets two different clients
  mirror one turn without creating a second experience;
* the record holds the reply text only while the request is live. A deletion
  or correction that invalidates it clears the text and leaves a tombstone, so
  a later replay returns `deleted` rather than the old answer. This mirrors the
  rule that audit records keep the minimum identifier, never the body;
* the fingerprint covers the business input (messages, class, scene, scope),
  not transport fields. A retry with the same fingerprint replays; a different
  fingerprint under the same key is a conflict, never an overwrite.

This module is pure storage plus its replay handler. Deciding *when* to
generate, and generating, belongs to `domain/turns.py`.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..errors import CorruptStoreError, InvalidSchemaError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .schema import SCHEMA_VERSION, load_versioned, wrap

TURN_REQUESTS_FILENAME = "turn_requests.json"
TURN_REQUESTS_SCHEMA = SCHEMA_VERSION

#: Journal event type for a committed turn. Persisted identifier: never rename.
TURN_EVENT_TYPE = "core.turn_committed"

STATUS_PENDING = "pending"
STATUS_COMMITTED = "committed"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_DELETED = "deleted"
STATUSES = frozenset({
    STATUS_PENDING, STATUS_COMMITTED, STATUS_FAILED, STATUS_INTERRUPTED,
    STATUS_DELETED,
})
#: States a retry may move forward from (nothing has been committed yet).
RETRYABLE_STATUSES = frozenset({STATUS_PENDING, STATUS_FAILED, STATUS_INTERRUPTED})
#: States whose reply text must not be served again.
TERMINAL_STATUSES = frozenset({STATUS_DELETED})

#: Bounded history: the newest records win, older ones are dropped on write.
MAX_RECORDS = 512
#: Longest reply kept inline; anything longer is refused rather than truncated.
MAX_REPLY_CHARS = 20_000
#: Longest message body kept in the fingerprint snapshot (per message).
_MAX_FINGERPRINT_CHARS = 4_000

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
class TurnRecord:
    """One whole-turn commit record; `reply` is None once invalidated."""

    request_id: str
    status: str
    fingerprint: str
    source_key: str
    turn_id: str
    content_class: str
    #: Identity boundary the turn was committed for (R03). Stored so a result
    #: lookup can refuse a different subject instead of leaking the reply.
    subject: str = "private"
    client_id: str = ""
    scene: str = ""
    context_receipt_id: str = ""
    session_id: str = ""
    episode_id: str = ""
    parent_turn_id: str = ""
    persona_version: str = ""
    card_revision: int = 0
    reply: str | None = None
    extraction_op_id: str | None = None
    reason: str | None = None
    processing_position: int | None = None
    created_at: str = ""
    updated_at: str = ""
    #: Business time fixed on the first attempt; reused on retries (R04).
    occurred_at: str = ""

    @property
    def is_committed(self) -> bool:
        return self.status == STATUS_COMMITTED

    @property
    def body_available(self) -> bool:
        """Whether the stored reply may still be returned to a caller."""
        return self.reply is not None and self.status not in TERMINAL_STATUSES

    def to_public(self) -> dict[str, Any]:
        """The wire shape of §3.2's write response (ids and state, no secrets)."""
        return {
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "status": self.status,
            "durable": self.is_committed and self.status != STATUS_DELETED,
            "replayed": False,
            "processing_position": self.processing_position,
            "extraction_status": (
                "queued" if self.extraction_op_id else
                ("committed" if self.is_committed else "not_queued")
            ),
            "persona_version": self.persona_version or None,
            "card_revision": self.card_revision,
            "context_receipt_id": self.context_receipt_id,
            "subject": self.subject, "scene": self.scene,
            "client_id": self.client_id, "session_id": self.session_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TurnReservation:
    """Outcome of reserving a request id.

    `kind` is one of:
      * new         - reserved now, caller owns the work;
      * replay      - already committed, `record.reply` is the answer to return;
      * in_progress - another attempt holds the key;
      * conflict    - same key, different business input;
      * deleted     - the result was invalidated; never serve the old text.
    """

    kind: str
    record: TurnRecord | None = None


def turn_fingerprint(
    *,
    messages: Any,
    content_class: str,
    source_key: str,
    episode_id: str = "",
    session_id: str = "",
    applies_to: str = "",
    scene: str = "",
) -> str:
    """Digest of the business input; transport-only fields are excluded.

    `received_at`, retry timestamps and the caller's connection are deliberately
    absent: a retry must produce the same digest or it would look like a
    different turn and run twice.
    """
    import hashlib

    snapshot = {
        "messages": _fingerprint_messages(messages),
        "content_class": str(content_class or ""),
        "source_key": str(source_key or ""),
        "episode_id": str(episode_id or ""),
        "session_id": str(session_id or ""),
        "applies_to": str(applies_to or ""),
        "scene": str(scene or ""),
    }
    blob = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _fingerprint_messages(messages: Any) -> list[dict[str, str]]:
    """Reduce messages to (role, text) pairs, bounded, order preserved."""
    if isinstance(messages, (str, bytes)) or not isinstance(messages, (list, tuple)):
        return []
    reduced: list[dict[str, str]] = []
    for item in messages:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("type") or item.get("role") or "")
        data = item.get("data")
        text = ""
        if isinstance(data, Mapping):
            text = str(data.get("content") or "")
        elif item.get("content") is not None:
            text = str(item.get("content") or "")
        reduced.append({
            "role": role,
            "text": text,
        })
    return reduced


class TurnRequestStore:
    """Durable whole-turn request ledger for one character directory."""

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

    # -- paths / io ------------------------------------------------------

    def _path(self, character: str) -> Path:
        return self._roots.character_file(character, TURN_REQUESTS_FILENAME)

    def _load(self, character: str) -> dict[str, dict[str, Any]]:
        path = self._path(character)
        document = load_versioned(
            self._store, path,
            supported=frozenset({TURN_REQUESTS_SCHEMA}),
            default={"records": {}},
        )
        records = document.get("records")
        if records is None:
            return {}
        if not isinstance(records, dict):
            raise CorruptStoreError(
                f"turn records must be an object, found {type(records).__name__}",
                path=path,
            )
        for request_id, record in records.items():
            _validate_record(record, request_id=request_id, path=path)
        return {str(key): dict(value) for key, value in records.items()}

    def _save(
        self, character: str, records: Mapping[str, Mapping[str, Any]],
    ) -> None:
        path = self._path(character)
        self._permit.assert_writable("save", str(path))
        self._store.save(path, wrap({"records": dict(records)}))

    def _now_iso(self) -> str:
        return self._clock.now().astimezone(timezone.utc).isoformat()

    # -- queries ---------------------------------------------------------

    def get(self, character: str, request_id: str) -> TurnRecord | None:
        """One record by request id, or None when the key was never seen."""
        request_id = _require_id(request_id, "request_id")
        with _lock_for(self._path(character)):
            record = self._load(character).get(request_id)
        return None if record is None else _to_record(record)

    def find_by_turn(self, character: str, turn_id: str) -> TurnRecord | None:
        """The record of one business turn (mirror dedupe across clients)."""
        turn_id = _require_id(turn_id, "turn_id")
        with _lock_for(self._path(character)):
            records = self._load(character)
        for record in records.values():
            if record.get("turn_id") == turn_id:
                return _to_record(record)
        return None

    def count(self, character: str) -> int:
        with _lock_for(self._path(character)):
            return len(self._load(character))

    # -- view API for journal.record_and_save ---------------------------

    def load_view(self, character: str) -> dict[str, dict[str, Any]]:
        """The raw record mapping (journal.record_and_save load_view)."""
        with _lock_for(self._path(character)):
            return self._load(character)

    def save_view(
        self, character: str, view: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Persist the raw mapping (journal.record_and_save save_view).

        Unlike `reserve`, this does not trim: the caller is mid-transaction and
        a trim here could drop the record the same journal entry is committing.
        Trimming happens on the reserve path, which only adds new keys.
        """
        path = self._path(character)
        self._permit.assert_writable("save", str(path))
        self._store.save(path, wrap({"records": dict(view)}))

    def apply_snapshot(
        self, view: dict[str, dict[str, Any]], payload: Mapping[str, Any],
    ) -> bool:
        """Merge one committed snapshot into a loaded view; True when it changed.

        Never downgrades: a deleted record stays deleted and an already
        committed record keeps its own reply. A malformed snapshot raises
        rather than returning False -- a silent skip here would advance the
        journal sentinel over an entry that was never applied, which is exactly
        the lost-update failure B02 removed.
        """
        restored = _record_from_snapshot(payload)
        request_id = restored["request_id"]
        existing = view.get(request_id)
        if existing is not None:
            if existing.get("status") == STATUS_DELETED:
                return False
            if existing.get("status") == STATUS_COMMITTED:
                return False
        view[request_id] = restored
        return True

    # -- mutation --------------------------------------------------------

    def reserve(
        self,
        character: str,
        *,
        request_id: str,
        fingerprint: str,
        source_key: str,
        turn_id: str,
        content_class: str,
        subject: str = "private",
        client_id: str = "",
        scene: str = "",
        context_receipt_id: str = "",
        session_id: str = "",
        episode_id: str = "",
        parent_turn_id: str = "",
        persona_version: str = "",
        card_revision: int = 0,
        occurred_at: str = "",
    ) -> TurnReservation:
        """Reserve one request id before doing any work.

        Same key + same fingerprint replays or reports in-flight; a different
        fingerprint is a conflict and never overwrites; an invalidated result
        reports `deleted` so the caller cannot resurrect the old answer.
        """
        request_id = _require_id(request_id, "request_id")
        fingerprint = _require_id(fingerprint, "fingerprint")
        source_key = _require_id(source_key, "source_key")
        turn_id = _require_id(turn_id, "turn_id")
        path = self._path(character)
        with _lock_for(path):
            records = self._load(character)
            existing = records.get(request_id)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    return TurnReservation("conflict", _to_record(existing))
                status = existing["status"]
                if status == STATUS_DELETED:
                    return TurnReservation("deleted", _to_record(existing))
                if status == STATUS_COMMITTED:
                    return TurnReservation("replay", _to_record(existing))
                if status == STATUS_PENDING:
                    return TurnReservation("in_progress", _to_record(existing))
                # failed / interrupted: the caller may retry the same request.
            now = self._now_iso()
            record = dict(existing or {})
            record.update({
                "request_id": request_id,
                "fingerprint": fingerprint,
                "source_key": source_key,
                "turn_id": turn_id,
                "content_class": content_class,
                "subject": subject,
                "client_id": client_id,
                "scene": scene,
                "context_receipt_id": context_receipt_id,
                "session_id": session_id,
                "episode_id": episode_id,
                "parent_turn_id": parent_turn_id,
                "persona_version": persona_version,
                "card_revision": int(card_revision or 0),
                "status": STATUS_PENDING,
                "reply": None,
                "extraction_op_id": None,
                "reason": None,
                "processing_position": None,
                "created_at": record.get("created_at") or now,
                "updated_at": now,
                # Fixed on the first attempt and kept on every retry: the
                # source fingerprint includes it (R04).
                "occurred_at": (record.get("occurred_at") or occurred_at or now),
            })
            records[request_id] = record
            records = _trim(records)
            self._save(character, records)
        return TurnReservation("new", _to_record(record))

    def mark_committed(
        self,
        character: str,
        request_id: str,
        *,
        reply: str | None = None,
        extraction_op_id: str | None = None,
        processing_position: int | None = None,
        persona_version: str | None = None,
    ) -> TurnRecord:
        """Record the reply and extraction intent; terminal until invalidated."""
        if reply is not None and len(reply) > MAX_REPLY_CHARS:
            raise ValueError(
                f"reply longer than {MAX_REPLY_CHARS} characters is refused",
            )
        path = self._path(character)
        with _lock_for(path):
            records = self._load(character)
            record = _require_record(records, request_id, path)
            if record["status"] == STATUS_DELETED:
                raise CorruptStoreError(
                    f"turn request {request_id!r} was invalidated and cannot "
                    f"be committed",
                    path=path,
                )
            now = self._now_iso()
            updated = dict(record)
            updated.update({
                "status": STATUS_COMMITTED,
                "reply": reply,
                "extraction_op_id": extraction_op_id,
                "processing_position": processing_position,
                "reason": None,
                "updated_at": now,
            })
            if persona_version:
                updated["persona_version"] = persona_version
            records[request_id] = updated
            self._save(character, records)
        return _to_record(updated)

    def mark_failed(
        self, character: str, request_id: str, *, status: str, reason: str,
    ) -> TurnRecord:
        """Record a retryable failure (`failed`) or an interruption."""
        if status not in (STATUS_FAILED, STATUS_INTERRUPTED):
            raise ValueError(f"unsupported failure status {status!r}")
        path = self._path(character)
        with _lock_for(path):
            records = self._load(character)
            record = _require_record(records, request_id, path)
            if record["status"] in (STATUS_COMMITTED, STATUS_DELETED):
                return _to_record(record)
            now = self._now_iso()
            updated = dict(record)
            updated.update({
                "status": status,
                "reply": None,
                "reason": str(reason or "")[:500] or None,
                "updated_at": now,
            })
            records[request_id] = updated
            self._save(character, records)
        return _to_record(updated)

    def attach_processing_position(
        self, character: str, request_id: str, position: int,
    ) -> TurnRecord:
        """Record the journal position assigned to a committed turn.

        Separate from `mark_committed` because the position is only known after
        the journal append, and the commit must not be repeated to learn it.
        """
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValueError("position must be an integer")
        path = self._path(character)
        with _lock_for(path):
            records = self._load(character)
            record = _require_record(records, request_id, path)
            if record.get("processing_position") == position:
                return _to_record(record)
            updated = dict(record)
            updated["processing_position"] = position
            updated["updated_at"] = self._now_iso()
            records[request_id] = updated
            self._save(character, records)
        return _to_record(updated)

    def mark_interrupted_pending(self, character: str) -> list[str]:
        """Mark every still-pending request `interrupted` (retryable).

        Call after the journal replay at startup: any record still `pending`
        belongs to a process that died between reserve and commit, so it must
        not be reported as in-flight forever. The request id stays retryable,
        which is what §3.4 requires ("restart distinguishes a resumable commit
        from an interrupted generation").
        """
        path = self._path(character)
        changed: list[str] = []
        with _lock_for(path):
            records = self._load(character)
            if not records:
                return []
            now = self._now_iso()
            for request_id, record in list(records.items()):
                if record.get("status") != STATUS_PENDING:
                    continue
                updated = dict(record)
                updated.update({
                    "status": STATUS_INTERRUPTED,
                    "reply": None,
                    "reason": "interrupted_restart",
                    "updated_at": now,
                })
                records[request_id] = updated
                changed.append(request_id)
            if changed:
                self._save(character, records)
        return sorted(changed)

    def invalidate(
        self,
        character: str,
        request_ids: list[str],
        *,
        reason: str = "deleted",
    ) -> list[str]:
        """Clear reply bodies of these requests and mark them deleted.

        Returns the ids actually changed. The record itself stays so a retry is
        answered `deleted` instead of being re-run into fresh content.
        """
        if not request_ids:
            return []
        path = self._path(character)
        changed: list[str] = []
        with _lock_for(path):
            records = self._load(character)
            now = self._now_iso()
            for request_id in request_ids:
                record = records.get(request_id)
                if record is None or record["status"] == STATUS_DELETED:
                    continue
                record = dict(record)
                record.update({
                    "status": STATUS_DELETED,
                    "reply": None,
                    "reason": reason,
                    "updated_at": now,
                })
                records[request_id] = record
                changed.append(request_id)
            if changed:
                self._save(character, records)
        return changed

    def invalidate_by_source(
        self, character: str, source_keys: set[str], *,
        reason: str = "deleted_source",
    ) -> list[str]:
        """Invalidate every request whose source key is in `source_keys`."""
        if not source_keys:
            return []
        with _lock_for(self._path(character)):
            records = self._load(character)
        targets = [
            request_id for request_id, record in records.items()
            if record.get("source_key") in source_keys
        ]
        return self.invalidate(character, targets, reason=reason)

    # -- replay ----------------------------------------------------------

    def replay_payload(
        self, character: str, request_id: str,
    ) -> Mapping[str, Any] | None:
        """The journal snapshot of one record (for crash repair)."""
        record = self.get(character, request_id)
        return None if record is None else _record_snapshot(record)

    def apply_replay(
        self, character: str, payload: Mapping[str, Any],
    ) -> bool:
        """Idempotently restore one committed record from a journal payload.

        Never downgrades a newer state: an existing `deleted` record is left
        alone (the deletion is the later fact), and a record already committed
        keeps its own reply. Returns whether the view changed.
        """
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return False
        path = self._path(character)
        with _lock_for(path):
            records = self._load(character)
            existing = records.get(request_id)
            if existing is not None:
                if existing["status"] == STATUS_DELETED:
                    return False
                if existing["status"] == STATUS_COMMITTED:
                    return False
            try:
                restored = _record_from_snapshot(payload)
            except (TypeError, ValueError):
                return False
            restored["updated_at"] = self._now_iso()
            records[request_id] = restored
            self._save(character, _trim(records))
        return True


# -- record helpers ----------------------------------------------------------


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def _require_record(
    records: Mapping[str, dict[str, Any]], request_id: str, path: Path,
) -> dict[str, Any]:
    request_id = _require_id(request_id, "request_id")
    record = records.get(request_id)
    if record is None:
        raise CorruptStoreError(
            f"no turn request {request_id!r} was reserved", path=path,
        )
    return record


def _trim(records: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep the newest MAX_RECORDS by created_at (ties broken by request id)."""
    if len(records) <= MAX_RECORDS:
        return {str(key): dict(value) for key, value in records.items()}
    ordered = sorted(
        records.items(),
        key=lambda item: (str(item[1].get("created_at") or ""), item[0]),
        reverse=True,
    )
    return {str(key): dict(value) for key, value in ordered[:MAX_RECORDS]}


_REQUIRED_FIELDS = (
    "request_id", "fingerprint", "source_key", "turn_id", "content_class",
    "status",
)


def _validate_record(record: Any, *, request_id: str, path: Path) -> None:
    if not isinstance(record, Mapping):
        raise CorruptStoreError(
            f"turn record {request_id!r} is not an object", path=path,
        )
    for field_name in _REQUIRED_FIELDS:
        value = record.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise CorruptStoreError(
                f"turn record {request_id!r} has no usable {field_name!r}",
                path=path,
            )
    if record.get("request_id") != request_id:
        raise CorruptStoreError(
            f"turn record key {request_id!r} disagrees with its request_id",
            path=path,
        )
    if record["status"] not in STATUSES:
        raise CorruptStoreError(
            f"turn record {request_id!r} has unknown status "
            f"{record['status']!r}", path=path,
        )
    revision = record.get("card_revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise CorruptStoreError(
            f"turn record {request_id!r} has non-integer card_revision",
            path=path,
        )


def _to_record(raw: Mapping[str, Any]) -> TurnRecord:
    reply = raw.get("reply")
    return TurnRecord(
        request_id=str(raw["request_id"]),
        status=str(raw["status"]),
        fingerprint=str(raw["fingerprint"]),
        source_key=str(raw["source_key"]),
        turn_id=str(raw["turn_id"]),
        content_class=str(raw["content_class"]),
        subject=str(raw.get("subject") or "private"),
        client_id=str(raw.get("client_id") or ""),
        scene=str(raw.get("scene") or ""),
        context_receipt_id=str(raw.get("context_receipt_id") or ""),
        session_id=str(raw.get("session_id") or ""),
        episode_id=str(raw.get("episode_id") or ""),
        parent_turn_id=str(raw.get("parent_turn_id") or ""),
        persona_version=str(raw.get("persona_version") or ""),
        card_revision=int(raw.get("card_revision") or 0),
        reply=reply if isinstance(reply, str) else None,
        extraction_op_id=(
            str(raw["extraction_op_id"])
            if raw.get("extraction_op_id") else None
        ),
        reason=str(raw["reason"]) if raw.get("reason") else None,
        processing_position=(
            int(raw["processing_position"])
            if isinstance(raw.get("processing_position"), int)
            and not isinstance(raw.get("processing_position"), bool)
            else None
        ),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        occurred_at=str(raw.get("occurred_at") or ""),
    )


_SNAPSHOT_FIELDS = (
    "request_id", "fingerprint", "source_key", "turn_id", "content_class",
    "subject", "session_id", "episode_id", "parent_turn_id", "persona_version",
    "client_id", "scene", "context_receipt_id",
    "card_revision", "reply", "extraction_op_id", "reason",
    "processing_position", "created_at", "occurred_at",
)


def _record_snapshot(record: TurnRecord) -> dict[str, Any]:
    """The journal payload: everything needed to rebuild a committed record."""
    payload = {name: getattr(record, name) for name in _SNAPSHOT_FIELDS}
    payload["status"] = STATUS_COMMITTED
    return payload


def _record_from_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("snapshot has no request_id")
    record = {name: payload.get(name) for name in _SNAPSHOT_FIELDS}
    # `status` is not among the snapshot fields: a committed snapshot always
    # replays as committed, and validating it as incoming input would reject
    # every one of them.
    record["status"] = STATUS_COMMITTED
    for field_name in _REQUIRED_FIELDS:
        if field_name == "status":
            continue
        value = record.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"snapshot has no usable {field_name}")
    record["request_id"] = request_id.strip()
    revision = record.get("card_revision")
    record["card_revision"] = int(revision) if isinstance(revision, int) else 0
    if record.get("reply") is not None and not isinstance(record["reply"], str):
        record["reply"] = None
    return record
