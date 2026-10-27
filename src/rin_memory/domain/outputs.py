"""M5 assistant-output domain: generate -> persist -> deliver -> receipts.

The assistant's own words are not a turn: a turn is a *pair* the caller hands
over once, while a proactive message or a task result is generated before
anyone has spoken. This service owns that second shape.

The three questions the record has to answer are "what did she say, why did she
say it, and did it arrive". They are answered separately on purpose:

* **why** lives in 'cause' (a task result, an open thread, an agreement) and is
  recorded once at creation, so a later reader can see the original reason
  rather than a reconstruction;
* **what** is the body, persisted in full at creation. Persisting it before
  delivery is what makes a restart able to resume only the parts that failed;
* **whether it arrived** is a list of delivery receipts, one per part index.

The visibility rule that follows (design section 12.3, "generated but not
delivered") is enforced here and nowhere else:

    a part enters the shared conversation (recent.json) only when a delivery
    receipt names that exact part index.

So an undelivered body is durable but invisible: it is not part of what both
parties experienced, it is not recallable, and it does not appear in the recent
window. It is only 'resumable' -- a restart can pick it up and send the missing
parts.

Delivery is idempotent by index: reporting the same index twice is a duplicate
(no second append, no double count), reporting a different character count for
an accepted index is a conflict, and reporting an index outside the body is
invalid. That is the shape a retrying transport needs.

An interruption follows the M2 turn semantics exactly: the body is kept, the
record becomes retryable, and **nothing** is appended to recent -- an
interrupted generation was never delivered, so it never became a shared
experience.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..persistence.outputs import (
    CONTENT_CLASS,
    DEFAULT_KIND,
    KINDS,
    MAX_TEXT_CHARS,
    RESUMABLE_STATUSES,
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_INVALIDATED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    OutputRecord,
    OutputStore,
    deliverable_segments,
    delivered_indices,
    snapshot_payload,
    split_segments,
    status_after_delivery,
)
from ..ports import Clock, WritePermit

#: Journal event name for one output record. Persisted identifier.
EVT_OUTPUT_RECORDED = "core.output_recorded"

#: The delivery outcomes a receipt may name, mirroring ServiceProtocol's M2 set:
#: 'sent' is a transport fact, 'interrupted' keeps the body for a retry.
DELIVERY_KINDS = ("sent", "interrupted", "failed")


@dataclass(frozen=True)
class OutputReceipt:
    """Result of one create/deliver/interrupt/invalidate call."""

    ok: bool = True
    duplicate: bool = False
    conflict: bool = False
    reason: str | None = None
    record: OutputRecord | None = None
    appended_parts: tuple[int, ...] = ()
    recent_written: int = 0

    def to_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "duplicate": self.duplicate,
            "conflict": self.conflict,
            "reason": self.reason,
            "appended_parts": list(self.appended_parts),
            "recent_written": int(self.recent_written),
        }
        if self.record is not None:
            payload.update(self.record.to_public(include_text=include_text))
        return payload


class OutputService:
    """Generate/persist/deliver assistant outputs for one data root.

    Dependencies are duck-typed on purpose: 'recent' is a RecentStore,
    'journal' an EventJournal, 'barrier' a DeletionBarrier. Each is optional so
    the service is still usable (and testable) without the full core, but a
    missing recent store means nothing can ever become a shared experience --
    which is reported, not silently swallowed.
    """

    def __init__(
        self,
        *,
        store: OutputStore,
        recent: Any = None,
        journal: Any = None,
        barrier: Any = None,
        clock: Clock,
        permit: WritePermit,
        max_part_chars: int | None = None,
    ) -> None:
        self._store = store
        self._recent = recent
        self._journal = journal
        self._barrier = barrier
        self._clock = clock
        self._permit = permit
        self._max_part_chars = int(max_part_chars or 0) or None
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        if self._journal is not None:
            # Registered in the constructor, before any write: a journal entry
            # this build writes must be replayable by it.
            self._journal.register_handler(EVT_OUTPUT_RECORDED, self.replay)

    # -- wiring ---------------------------------------------------------

    @staticmethod
    def _character(character: Any) -> str:
        value = str(character or "").strip()
        if not value:
            raise ValueError("character must be nonempty")
        return value

    def _lock(self, character: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.RLock()
                self._locks[character] = lock
            return lock

    def _now(self) -> str:
        return self._clock.now().isoformat()

    # -- public API ------------------------------------------------------

    def create(
        self,
        character: str,
        *,
        text: Any,
        kind: str = DEFAULT_KIND,
        subject: str = "private",
        scene: str = "",
        session_id: str = "",
        client_id: str = "",
        episode_id: str | None = None,
        source_key: str | None = None,
        cause: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        part_sizes: Sequence[int] | None = None,
    ) -> OutputReceipt:
        """Persist a generated body as pending; nothing reaches recent yet."""
        name = self._character(character)
        body = str(text or "").strip()
        if not body:
            return OutputReceipt(ok=False, reason="empty_text")
        if len(body) > MAX_TEXT_CHARS:
            return OutputReceipt(ok=False, reason="text_too_long")
        if str(kind or DEFAULT_KIND) not in KINDS:
            return OutputReceipt(ok=False, reason="invalid_kind")
        try:
            segments = split_segments(
                body, sizes=part_sizes, max_chars=self._max_part_chars or 400,
            )
        except ValueError as exc:
            return OutputReceipt(ok=False, reason=str(exc)[:120])
        with self._lock(name):
            if request_id:
                # Same (character, subject, request) is the same output: a
                # client retry after a lost ack must not create a second body.
                output_id = OutputStore.output_id_for(name, subject, request_id)
                existing = self._store.get(name, output_id)
                if existing is not None:
                    return OutputReceipt(
                        duplicate=True, record=OutputRecord(existing),
                    )
            else:
                output_id = OutputStore.new_output_id()
            now = self._now()
            row = {
                "output_id": output_id,
                "status": STATUS_PENDING,
                "text": body,
                "segments": segments,
                "delivered": [],
                "kind": str(kind),
                "content_class": CONTENT_CLASS,
                "subject": str(subject or "private"),
                "scene": str(scene or ""),
                "session_id": str(session_id or ""),
                "client_id": str(client_id or ""),
                "episode_id": episode_id,
                "source_key": source_key,
                "cause": dict(cause or {}),
                "created_at": now,
                "updated_at": now,
            }
            recorded = self._write(name, row)
        return OutputReceipt(record=OutputRecord(recorded))

    def deliver(
        self,
        character: str,
        output_id: str,
        *,
        parts: Sequence[Mapping[str, Any]] | None = None,
        platform_id: str = "",
        kind: str = "sent",
        reason: str | None = None,
    ) -> OutputReceipt:
        """Record which parts actually arrived; only those enter recent.

        Idempotent by part index: an index already accepted is reported as a
        duplicate (never appended twice, never counted twice). A conflicting
        character count for an accepted index is a conflict, not an overwrite.
        """
        name = self._character(character)
        if str(kind) not in DELIVERY_KINDS:
            return OutputReceipt(ok=False, reason="invalid_delivery_kind")
        if kind != "sent":
            return self._terminal(
                name, output_id,
                status=STATUS_INTERRUPTED if kind == "interrupted" else STATUS_FAILED,
                reason=reason or kind,
            )
        with self._lock(name):
            row = self._store.get(name, str(output_id or "").strip())
            if row is None:
                return OutputReceipt(ok=False, reason="unknown_output")
            segments = deliverable_segments(row)
            if not segments:
                return OutputReceipt(ok=False, reason="empty_body")
            if parts is None:
                # No parts named: the whole body was accepted in one piece.
                parts = [
                    {"index": segment["index"], "chars": segment["chars"]}
                    for segment in segments
                ]
            accepted = delivered_indices(row)
            appended: list[int] = []
            entries = [dict(item) for item in row.get("delivered") or ()]
            for part in parts:
                if not isinstance(part, Mapping):
                    return OutputReceipt(ok=False, reason="invalid_part")
                index = part.get("index")
                if isinstance(index, bool) or not isinstance(index, int):
                    return OutputReceipt(ok=False, reason="invalid_part_index")
                if index < 0 or index >= len(segments):
                    return OutputReceipt(ok=False, reason="part_index_out_of_range")
                chars = part.get("chars", segments[index]["chars"])
                if isinstance(chars, bool) or not isinstance(chars, int) or chars < 0:
                    return OutputReceipt(ok=False, reason="invalid_part_chars")
                if chars != segments[index]["chars"]:
                    return OutputReceipt(
                        ok=False, conflict=True, reason="part_chars_mismatch",
                    )
                if index in accepted:
                    # A retry of an already accepted part: duplicate, no write.
                    continue
                accepted.append(index)
                appended.append(index)
                entries.append({
                    "index": index,
                    "chars": chars,
                    "platform_id": str(part.get("platform_id") or platform_id or "")[:200],
                    "at": self._now(),
                })
            if not appended:
                return OutputReceipt(duplicate=True, record=OutputRecord(row))
            entries.sort(key=lambda item: item["index"])
            updated = dict(row)
            updated["delivered"] = entries
            updated["status"] = status_after_delivery({**updated, "status": STATUS_PENDING})
            delivered_set = set(delivered_indices(updated))
            now = self._now()
            if not updated.get("first_delivered_at"):
                updated["first_delivered_at"] = now
            if len(delivered_set) >= len(segments):
                updated["delivered_at"] = now
            updated["updated_at"] = now
            recorded = self._write(name, updated)
            written = self._ensure_recent(name, recorded)
        return OutputReceipt(
            record=OutputRecord(recorded),
            appended_parts=tuple(sorted(appended)),
            recent_written=written,
        )

    def interrupt(self, character: str, output_id: str, *, reason: str = "interrupted") -> OutputReceipt:
        """Mark a delivery interrupted: body kept for a retry, recent untouched."""
        return self._terminal(
            self._character(character), output_id,
            status=STATUS_INTERRUPTED, reason=reason,
        )

    def fail(self, character: str, output_id: str, *, reason: str = "failed") -> OutputReceipt:
        return self._terminal(
            self._character(character), output_id,
            status=STATUS_FAILED, reason=reason,
        )

    def invalidate(self, character: str, output_id: str, *, reason: str = "source_deleted") -> OutputReceipt:
        """Clear the body after its cause was deleted; ids and receipts stay."""
        name = self._character(character)
        with self._lock(name):
            row = self._store.get(name, str(output_id or "").strip())
            if row is None:
                return OutputReceipt(ok=False, reason="unknown_output")
            if row.get("status") == STATUS_INVALIDATED:
                return OutputReceipt(duplicate=True, record=OutputRecord(row))
            now = self._now()
            updated = dict(row)
            updated.update({
                "status": STATUS_INVALIDATED,
                "text": "",
                "segments": [],
                "invalidated_at": now,
                "updated_at": now,
                "reason": str(reason or "source_deleted")[:200],
            })
            recorded = self._write(name, updated)
        return OutputReceipt(record=OutputRecord(recorded))

    def pending(self, character: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Resumable outputs with the parts still missing (restart resume)."""
        name = self._character(character)
        out: list[dict[str, Any]] = []
        for row in self._store.list(name):
            record = OutputRecord(row)
            if record.status not in RESUMABLE_STATUSES:
                continue
            parts = record.pending_parts
            if not parts:
                continue
            payload = record.to_public(include_text=False)
            payload["pending_parts"] = parts
            out.append(payload)
            if len(out) >= max(0, int(limit)):
                break
        return out

    def get(self, character: str, output_id: str, *, include_text: bool = False) -> dict[str, Any] | None:
        row = self._store.get(self._character(character), str(output_id or "").strip())
        if row is None:
            return None
        return OutputRecord(row).to_public(include_text=include_text)

    def list(self, character: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return [
            OutputRecord(row).to_public()
            for row in self._store.list(self._character(character), limit=limit)
        ]

    def async_create(self, *args: Any, **kwargs: Any) -> Any:
        return asyncio.to_thread(self.create, *args, **kwargs)

    # -- internals -------------------------------------------------------

    def _terminal(self, character: str, output_id: str, *, status: str, reason: str) -> OutputReceipt:
        with self._lock(character):
            row = self._store.get(character, str(output_id or "").strip())
            if row is None:
                return OutputReceipt(ok=False, reason="unknown_output")
            if row.get("status") == STATUS_INVALIDATED:
                return OutputReceipt(ok=False, conflict=True, reason="invalidated")
            now = self._now()
            updated = dict(row)
            updated["status"] = status
            updated["updated_at"] = now
            updated["reason"] = str(reason or status)[:200]
            if status == STATUS_INTERRUPTED:
                updated["interrupted_at"] = now
            recorded = self._write(character, updated)
        return OutputReceipt(record=OutputRecord(recorded))

    def _write(self, character: str, row: Mapping[str, Any]) -> dict[str, Any]:
        """Journal then persist: the entry is the repair source for a crash.

        The order is the one B02 froze -- append and fsync first, then mutate
        the view -- so a crash after the fsync is repaired by replaying the
        entry, and a crash before it leaves nothing behind. The view mutation
        and the save happen inside the same journal critical section, so a
        concurrent writer cannot interleave a full-document overwrite.
        """
        if self._journal is None:
            return self._store.write(character, row)
        self._journal.record_and_save(
            character,
            EVT_OUTPUT_RECORDED,
            snapshot_payload(row),
            load_view=self._store.load,
            mutate_view=lambda view: self._store.mutate(view, row),
            save_view=self._store.save,
        )
        return self._store.get(character, str(row.get("output_id") or "")) or dict(row)

    def _ensure_recent(self, character: str, row: Mapping[str, Any]) -> int:
        """Append the newly delivered parts to recent; idempotent per index.

        Deduplication is by (output_id, index) metadata, not by text: the same
        sentence delivered twice is still one experience, and a replay after a
        crash between the journal and recent must not duplicate it.
        """
        if self._recent is None:
            return 0
        delivered = set(delivered_indices(row))
        if not delivered:
            return 0
        segments = deliverable_segments(row)
        current = list(self._recent.messages(character))
        present: set[tuple[str, int]] = set()
        for message in current:
            metadata = _metadata_of(message)
            if str(metadata.get("output_id") or "") != str(row.get("output_id") or ""):
                continue
            try:
                present.add((str(metadata["output_id"]), int(metadata.get("part_index"))))
            except (TypeError, ValueError):
                continue
        appended: list[dict[str, Any]] = []
        for index in sorted(delivered):
            if (str(row.get("output_id") or ""), index) in present:
                continue
            appended.append(self._recent_message(row, segments[index]))
        if not appended:
            return 0
        self._recent.append(character, appended)
        return len(appended)

    def _recent_message(self, row: Mapping[str, Any], segment: Mapping[str, Any]) -> dict[str, Any]:
        """One delivered part as a shared-conversation message.

        It carries the output id and the part index so a duplicate report is
        recognisable, the cause source keys so a deletion of the cause still
        reaches these lines, and companion_output so it never feeds extraction.
        """
        metadata: dict[str, Any] = {
            "subject": str(row.get("subject") or "private"),
            "scene": str(row.get("scene") or ""),
            "session_id": str(row.get("session_id") or ""),
            "client_id": str(row.get("client_id") or ""),
            "content_class": CONTENT_CLASS,
            "output_id": str(row.get("output_id") or ""),
            "part_index": int(segment["index"]),
            "output_kind": str(row.get("kind") or DEFAULT_KIND),
        }
        if row.get("episode_id"):
            metadata["episode_id"] = str(row["episode_id"])
        source_key = str(row.get("source_key") or "").strip()
        if source_key:
            metadata["source_key"] = source_key
        cause = row.get("cause") if isinstance(row.get("cause"), Mapping) else {}
        cause_keys = [str(item) for item in cause.get("source_keys") or () if item]
        if cause_keys:
            metadata["source_event_keys"] = cause_keys
            metadata["companion_event"] = {
                "source_key": cause_keys[0],
                "content_class": CONTENT_CLASS,
                "event_type": "assistant_output",
            }
        return {"type": "ai", "data": {"content": str(segment["text"]), "metadata": metadata}}

    # -- journal replay --------------------------------------------------

    def replay(self, character: str, payload: Any) -> bool:
        """Rebuild one output row (and its delivered recent lines) from a snapshot."""
        if not isinstance(payload, Mapping):
            return False
        row = payload.get("output")
        if not isinstance(row, Mapping):
            return False
        applied = self._store.apply_replay(character, payload)
        stored = self._store.get(character, str(row.get("output_id") or ""))
        if stored is not None:
            # Delivered parts must exist in recent even when the crash landed
            # between the journal fsync and the recent append.
            self._ensure_recent(character, stored)
        return applied


def _metadata_of(message: Any) -> Mapping[str, Any]:
    if not isinstance(message, Mapping):
        return {}
    data = message.get("data")
    if isinstance(data, Mapping):
        metadata = data.get("metadata")
        if isinstance(metadata, Mapping):
            return metadata
    metadata = message.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


__all__ = [
    "DELIVERY_KINDS",
    "EVT_OUTPUT_RECORDED",
    "OutputReceipt",
    "OutputService",
]
