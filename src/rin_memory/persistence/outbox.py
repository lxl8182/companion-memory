"""Per-character persistent outbox (B02).

outbox.ndjson is an append-only queue of background operations that must survive
a restart (host memory/outbox.py). Each operation is one pending record plus, as
it progresses, attempt / done / dead-letter records paired by op_id. The store
makes no delivery guarantee: it is at-least-once, so every op handler must be
naturally idempotent. Dispatch and the async replay loop are B07's job; this
module only persists, scans and compacts.

Record fields follow the host wire format: op_id, type, payload, status, ts (the
Python attribute is called op_type, the persisted key stays "type" so host-written
outbox.ndjson files keep working). Attempt records additionally carry a cumulative
"attempts" count; the host counts attempt lines instead, and this reader accepts
both. Dead letters are persisted as status "dead_letter" (the host appended a
plain "done" record for the same purpose); both are terminal for pending().

All methods perform blocking file IO; call them through asyncio.to_thread.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import CorruptStoreError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit

_OUTBOX_FILE = "outbox.ndjson"
_COMPACT_LINES_THRESHOLD = 1000

_STATUS_PENDING = "pending"
_STATUS_ATTEMPT = "attempt"
_STATUS_DONE = "done"
_STATUS_DEAD_LETTER = "dead_letter"
_TERMINAL_STATUSES = frozenset({_STATUS_DONE, _STATUS_DEAD_LETTER})


@dataclass(frozen=True)
class OutboxOp:
    """One unfinished outbox operation, as returned by pending()."""

    op_id: str
    op_type: str
    payload: Mapping[str, Any]
    attempts: int
    created_at: datetime | None


class OutboxStore:
    """Append-only per-character op queue with attempt counting and compaction.

    Implements the ports.OutboxStore protocol. Every mutation goes through the
    injected WritePermit; a per-character threading.Lock serialises appends,
    scans and compaction (there is no global data lock).
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        max_attempts: int = 5,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._max_attempts = max_attempts
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
        return self._roots.character_dir(character, create=create) / _OUTBOX_FILE

    # -- reading ---------------------------------------------------------

    def _read_records(self, character: str) -> list[dict[str, Any]]:
        """Parse the queue; a torn final line is a never-acknowledged append.

        Any other unparsable or malformed line raises CorruptStoreError and the
        file is left untouched (a skipped line could be an op that never runs).
        """
        path = self._path(character, create=False)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise CorruptStoreError(
                f"outbox is unreadable: {exc}", path=path, cause=exc,
            ) from exc
        if not text:
            return []
        lines = text.split("\n")
        torn = not text.endswith("\n")
        if not torn:
            lines.pop()
        records: list[dict[str, Any]] = []
        for index, raw in enumerate(lines):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                if torn and index == len(lines) - 1:
                    continue
                raise CorruptStoreError(
                    f"outbox line {index + 1} is not valid JSON: {exc.msg}",
                    path=path, cause=exc,
                ) from exc
            if not isinstance(record, dict):
                raise CorruptStoreError(
                    f"outbox line {index + 1} is not a JSON object", path=path,
                )
            records.append(record)
        return records

    def _scan(
        self, character: str, records: Sequence[Mapping[str, Any]],
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, int],
        dict[str, dict[str, Any]],
    ]:
        """Reduce records to (pending by op, attempts by op, latest attempt record)."""
        path = self._path(character, create=False)
        pending: dict[str, dict[str, Any]] = {}
        attempts: dict[str, int] = {}
        latest_attempt: dict[str, dict[str, Any]] = {}
        for index, record in enumerate(records):
            op_id = record.get("op_id")
            status = record.get("status")
            if not isinstance(op_id, str) or not op_id.strip():
                raise CorruptStoreError(
                    f"outbox record {index + 1} has no op_id", path=path,
                )
            if status == _STATUS_PENDING:
                pending[op_id] = dict(record)
            elif status == _STATUS_ATTEMPT:
                declared = record.get("attempts")
                count = attempts.get(op_id, 0) + 1
                if isinstance(declared, int) and not isinstance(declared, bool):
                    count = max(count, declared)
                attempts[op_id] = count
                latest_attempt[op_id] = dict(record)
            elif status in _TERMINAL_STATUSES:
                pending.pop(op_id, None)
            else:
                raise CorruptStoreError(
                    f"outbox record {index + 1} has unknown status {status!r}",
                    path=path,
                )
        return pending, attempts, latest_attempt

    @staticmethod
    def _parse_ts(raw: Any) -> datetime | None:
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    # -- writing ---------------------------------------------------------

    def enqueue(
        self, character: str, op_type: str, payload: Mapping[str, Any],
    ) -> str:
        """Append a pending op (fsync) and return its new op_id."""
        if not isinstance(op_type, str) or not op_type.strip():
            raise ValueError("outbox op_type must be a nonempty string")
        if not isinstance(payload, Mapping):
            raise ValueError("outbox payload must be a mapping")
        path = self._path(character, create=False)
        self._permit.assert_writable("outbox.enqueue", str(path))
        op_id = str(uuid.uuid4())
        record = {
            "op_id": op_id,
            "type": op_type,
            "payload": dict(payload),
            "status": _STATUS_PENDING,
            "ts": self._clock.now().isoformat(),
        }
        with self._lock_for(character):
            self._store.append_line(
                self._path(character), json.dumps(record, ensure_ascii=False),
            )
        return op_id

    def complete(self, character: str, op_id: str) -> None:
        """Mark an op done; it never replays again."""
        self.complete_many(character, (op_id,))

    def complete_many(self, character: str, op_ids: Sequence[str]) -> int:
        """Mark several ops done in one critical section; returns the count.

        RO02 batch extraction finishes a whole batch at once: one model call
        covers N queued turns, so N ops become done together. The ledger format
        does not change -- a batch completion is still one `done` record per
        op_id, written under the same per-character lock, so a reader sees
        either "all still pending" or "all done" per append and a crash in the
        middle leaves the rest pending (they replay, and the batch is deduped by
        its deterministic id). Rewriting the file would have to coordinate with
        compaction and the host's append-only readers for no gain.
        """
        ids: list[str] = []
        for op_id in op_ids or ():
            self._require_op_id(op_id)
            if op_id not in ids:
                ids.append(op_id)
        if not ids:
            return 0
        self._permit.assert_writable(
            "outbox.complete", str(self._path(character, create=False)),
        )
        timestamp = self._clock.now().isoformat()
        path = self._path(character)
        with self._lock_for(character):
            for op_id in ids:
                record = {
                    "op_id": op_id,
                    "status": _STATUS_DONE,
                    "ts": timestamp,
                }
                self._store.append_line(
                    path, json.dumps(record, ensure_ascii=False),
                )
        return len(ids)

    def fail(self, character: str, op_id: str, *, error: str) -> int:
        """Record a failed attempt, return the new count, dead-letter at the limit.

        Once the count reaches max_attempts a terminal dead_letter record is
        appended in the same critical section, so a poison op stops replaying
        instead of growing the queue forever.
        """
        self._require_op_id(op_id)
        path = self._path(character, create=False)
        self._permit.assert_writable("outbox.fail", str(path))
        with self._lock_for(character):
            records = self._read_records(character)
            _, attempts, _ = self._scan(character, records)
            count = attempts.get(op_id, 0) + 1
            timestamp = self._clock.now().isoformat()
            attempt = {
                "op_id": op_id,
                "status": _STATUS_ATTEMPT,
                "attempts": count,
                "error": error,
                "ts": timestamp,
            }
            self._store.append_line(
                self._path(character), json.dumps(attempt, ensure_ascii=False),
            )
            if count >= self._max_attempts:
                dead = {
                    "op_id": op_id,
                    "status": _STATUS_DEAD_LETTER,
                    "attempts": count,
                    "error": error,
                    "ts": timestamp,
                }
                self._store.append_line(
                    self._path(character), json.dumps(dead, ensure_ascii=False),
                )
        return count

    # -- scan ------------------------------------------------------------

    def pending(self, character: str, *, limit: int = 20) -> tuple[OutboxOp, ...]:
        """Pending, non-dead-lettered ops in append order, oldest first."""
        if limit < 0:
            raise ValueError("limit must be nonnegative")
        with self._lock_for(character):
            records = self._read_records(character)
            pending, attempts, _ = self._scan(character, records)
        path = self._path(character, create=False)
        ops: list[OutboxOp] = []
        for op_id, record in pending.items():
            op_type = record.get("type", record.get("op_type"))
            payload = record.get("payload")
            if payload is None:
                payload = {}
            if not isinstance(op_type, str) or not op_type.strip():
                raise CorruptStoreError(
                    f"outbox pending op {op_id} has no op type", path=path,
                )
            if not isinstance(payload, Mapping):
                raise CorruptStoreError(
                    f"outbox pending op {op_id} payload is not an object", path=path,
                )
            ops.append(OutboxOp(
                op_id=op_id,
                op_type=op_type,
                payload=dict(payload),
                attempts=attempts.get(op_id, 0),
                created_at=self._parse_ts(record.get("ts")),
            ))
            if len(ops) >= limit:
                break
        return tuple(ops)

    # -- lookup ----------------------------------------------------------

    def find_by_source_key(
        self, character: str, source_key: str, *, op_type: str | None = None,
    ) -> tuple[OutboxOp, ...]:
        """Every still-pending op whose payload lists `source_key`, oldest first.

        Recovery uses this to answer "was the extraction op for this turn ever
        written?". `ingest_turn` registers the source, saves the raw turn and
        *then* enqueues, so a crash between saving and enqueueing left a turn
        that a retry reported as `duplicate` with no op queued anywhere: the
        conversation existed but could never be extracted.
        """
        if not isinstance(source_key, str) or not source_key.strip():
            return ()
        with self._lock_for(character):
            records = self._read_records(character)
            pending, attempts, _ = self._scan(character, records)
        found: list[OutboxOp] = []
        for op_id, record in pending.items():
            record_type = record.get("type", record.get("op_type"))
            if op_type is not None and record_type != op_type:
                continue
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                continue
            keys = payload.get("source_keys")
            if isinstance(keys, (str, bytes)):
                keys = (keys,)
            if not isinstance(keys, (list, tuple)) or source_key not in keys:
                continue
            found.append(OutboxOp(
                op_id=op_id,
                op_type=str(record_type),
                payload=dict(payload),
                attempts=attempts.get(op_id, 0),
                created_at=self._parse_ts(record.get("ts")),
            ))
        return tuple(found)

    # -- compaction ------------------------------------------------------

    def compact(self, character: str) -> int:
        """Rewrite the queue when it exceeds 1000 lines; returns lines dropped.

        Keeps the pending records and, per still-pending op, the latest attempt
        record (with its cumulative attempts count) so the dead-letter tally
        survives a restart. Opportunistic and safe at startup; 0 means the
        threshold was not reached or nothing was discardable.
        """
        path = self._path(character, create=False)
        self._permit.assert_writable("outbox.compact", str(path))
        with self._lock_for(character):
            records = self._read_records(character)
            if len(records) <= _COMPACT_LINES_THRESHOLD:
                return 0
            pending, attempts, latest_attempt = self._scan(character, records)
            kept: list[dict[str, Any]] = []
            for op_id, record in pending.items():
                kept.append(record)
                if attempts.get(op_id, 0) > 0:
                    attempt = dict(latest_attempt.get(op_id) or {})
                    attempt["status"] = _STATUS_ATTEMPT
                    attempt["attempts"] = attempts[op_id]
                    attempt.setdefault("ts", record.get("ts"))
                    kept.append(attempt)
            body = "".join(
                json.dumps(record, ensure_ascii=False) + "\n" for record in kept
            )
            self._store.save_text(self._path(character), body)
            return max(0, len(records) - len(kept))

    @staticmethod
    def _require_op_id(op_id: str) -> None:
        if not isinstance(op_id, str) or not op_id.strip():
            raise ValueError("outbox op_id must be a nonempty string")
