"""Durable assistant-output records and their replay view (M5).

The assistant's own speech used to exist only as the 'reply' field of a
whole-turn commit: there was no way to tell "what she said, why she said it,
and whether it arrived". This store owns that record.

Visibility contract (the part that matters):

* the full body is persisted as soon as it is *generated*, so a process restart
  can resume delivery of the parts that never arrived;
* a part becomes part of the shared conversation (recent.json) **only** when a
  delivery receipt says the platform accepted that exact part;
* undelivered text therefore never counts as something both parties
  experienced, and never reaches a default recall path.

Delivery receipts are deduplicated by **part index**, not by content: a client
retry that reports the same part twice is a duplicate, and a different length
for an already accepted index is a conflict. That is what makes "retry only the
parts that failed" idempotent.

The document is a versioned envelope so a future migration can move it forward
without a second reader; every row is a plain object and unknown fields survive
a round trip.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .schema import load_versioned, wrap

OUTPUTS_FILENAME = "outputs.json"
OUTPUTS_SCHEMA = 1

#: One delivery: platform acceptance of one part. 'interrupted' keeps the body
#: for a retry (M2 turn semantics); 'failed' is terminal for scheduling but the
#: body stays until retention drops it; 'invalidated' clears the body.
STATUS_PENDING = "pending"
STATUS_PARTIAL = "partial"
STATUS_DELIVERED = "delivered"
STATUS_INTERRUPTED = "interrupted"
STATUS_FAILED = "failed"
STATUS_INVALIDATED = "invalidated"

STATUSES = frozenset({
    STATUS_PENDING, STATUS_PARTIAL, STATUS_DELIVERED,
    STATUS_INTERRUPTED, STATUS_FAILED, STATUS_INVALIDATED,
})
#: Statuses whose undelivered parts may still be sent (restart resume).
RESUMABLE_STATUSES = frozenset({
    STATUS_PENDING, STATUS_PARTIAL, STATUS_INTERRUPTED, STATUS_FAILED,
})
TERMINAL_STATUSES = frozenset({STATUS_INVALIDATED})

#: Output kinds: an unprompted message, feedback about a real task, or the
#: assistant side of an ordinary turn (kept for symmetry with /v1/turns).
KINDS = frozenset({"proactive", "task_feedback", "reply", "notice"})
DEFAULT_KIND = "notice"

#: companion_output never feeds fact extraction and never adds user preference
#: support (classification.PLUGIN_ONLY_CLASSES).
CONTENT_CLASS = "companion_output"

MAX_RECORDS = 512
MAX_TEXT_CHARS = 20_000
MAX_PART_CHARS = 400
MAX_PARTS = 64
MAX_CAUSE_KEYS = 32
MAX_REASON_CHARS = 200
MAX_ID_CHARS = 120
MAX_PLATFORM_ID_CHARS = 200

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def split_segments(
    text: Any, *, sizes: Sequence[int] | None = None, max_chars: int = MAX_PART_CHARS,
) -> list[dict[str, Any]]:
    """Split a body into deterministic delivery parts.

    The service owns the split so a delivery receipt can be checked against a
    fixed contract: 'sizes' lets a caller that has its own transport limit
    state it up front (it must sum to the body length), otherwise the split is
    'max_chars' wide, snapped back to a sentence terminator when one lies in
    the second half of the window so a part rarely ends mid-sentence.
    """
    body = str(text or "")
    if not body:
        return []
    if sizes is not None:
        cleaned = [_as_int(value, 0) for value in sizes]
        if not cleaned or any(value <= 0 for value in cleaned):
            raise ValueError("part sizes must be positive integers")
        if sum(cleaned) != len(body):
            raise ValueError("part sizes must sum to the body length")
        spans = cleaned
    else:
        limit = max(1, min(int(max_chars or MAX_PART_CHARS), len(body)))
        spans = []
        cursor = 0
        while cursor < len(body):
            end = min(cursor + limit, len(body))
            if end < len(body):
                window = body[cursor:end]
                cut = max(
                    (window.rfind(mark) for mark in "。！？!?；;\n"),
                    default=-1,
                )
                if cut >= len(window) // 2:
                    end = cursor + cut + 1
            spans.append(end - cursor)
            cursor = end
        if len(spans) > MAX_PARTS:
            raise ValueError(f"body needs more than {MAX_PARTS} parts")
    segments: list[dict[str, Any]] = []
    offset = 0
    for index, chars in enumerate(spans):
        segments.append({
            "index": index,
            "chars": int(chars),
            "offset": offset,
            "text": body[offset:offset + chars],
        })
        offset += int(chars)
    return segments


def deliverable_segments(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Recompute the segment list of a stored row (the body is authoritative)."""
    return split_segments(
        row.get("text"),
        sizes=[_as_int(item.get("chars"), 0) for item in row.get("segments") or ()],
    )


def delivered_indices(row: Mapping[str, Any]) -> list[int]:
    out: list[int] = []
    for item in row.get("delivered") or ():
        if not isinstance(item, Mapping):
            continue
        index = _as_int(item.get("index"), -1)
        if index >= 0 and index not in out:
            out.append(index)
    return sorted(out)


def status_after_delivery(row: Mapping[str, Any]) -> str:
    """The status implied by how many parts actually arrived."""
    if row.get("status") == STATUS_INVALIDATED:
        return STATUS_INVALIDATED
    total = len(row.get("segments") or ())
    delivered = len(delivered_indices(row))
    if total and delivered >= total:
        return STATUS_DELIVERED
    if delivered > 0:
        return STATUS_PARTIAL
    return STATUS_PENDING


class OutputRecord:
    """One assistant output as a caller sees it (ids and state, no secrets)."""

    __slots__ = ("row",)

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.row = dict(row)

    @property
    def output_id(self) -> str:
        return str(self.row.get("output_id") or "")

    @property
    def status(self) -> str:
        return str(self.row.get("status") or STATUS_PENDING)

    @property
    def body_available(self) -> bool:
        """Whether the stored body may still be served or delivered."""
        return bool(str(self.row.get("text") or "").strip()) and (
            self.status not in TERMINAL_STATUSES
        )

    @property
    def pending_parts(self) -> list[dict[str, Any]]:
        have = set(delivered_indices(self.row))
        return [
            dict(segment) for segment in deliverable_segments(self.row)
            if segment["index"] not in have
        ]

    def to_public(self, *, include_text: bool = False) -> dict[str, Any]:
        row = self.row
        pending = self.pending_parts
        payload: dict[str, Any] = {
            "output_id": self.output_id,
            "status": self.status,
            "kind": str(row.get("kind") or DEFAULT_KIND),
            "content_class": str(row.get("content_class") or CONTENT_CLASS),
            "subject": str(row.get("subject") or "private"),
            "scene": str(row.get("scene") or ""),
            "session_id": str(row.get("session_id") or ""),
            "chars": _as_int(row.get("chars"), 0),
            "parts_total": len(row.get("segments") or ()),
            "parts_delivered": [dict(item) for item in row.get("delivered") or ()],
            "delivered_chars": _as_int(row.get("delivered_chars"), 0),
            "pending_indices": [item["index"] for item in pending],
            "resumable": self.status in RESUMABLE_STATUSES,
            "cause": dict(row.get("cause") or {}),
            "reason": row.get("reason"),
            "episode_id": row.get("episode_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "first_delivered_at": row.get("first_delivered_at"),
            "delivered_at": row.get("delivered_at"),
            "interrupted_at": row.get("interrupted_at"),
            "invalidated_at": row.get("invalidated_at"),
        }
        if include_text:
            payload["text"] = str(row.get("text") or "")
            payload["segments"] = [
                {key: segment[key] for key in ("index", "chars", "offset", "text")}
                for segment in deliverable_segments(row)
            ]
            payload["pending_parts"] = pending
        return payload


def _row_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    """The journal payload for one row: everything needed to rebuild it."""
    snapshot = dict(row)
    snapshot["segments"] = [
        {"index": item["index"], "chars": item["chars"],
         "offset": item["offset"], "text": item["text"]}
        for item in deliverable_segments(row)
    ]
    return snapshot


class OutputStore:
    """Versioned assistant-output table for one character directory."""

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
        return self._roots.character_file(character, OUTPUTS_FILENAME)

    def load(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        document = load_versioned(
            self._store, path,
            supported=frozenset({OUTPUTS_SCHEMA}),
            default={"version": 0, "outputs": {}},
        )
        version = _as_int(document.get("version"), 0)
        raw = document.get("outputs")
        outputs: dict[str, dict[str, Any]] = {}
        if isinstance(raw, Mapping):
            for key, row in raw.items():
                if not isinstance(row, Mapping):
                    continue
                normalized = self._normalize(dict(row), str(key))
                if normalized:
                    outputs[normalized["output_id"]] = normalized
        return {"schema_version": OUTPUTS_SCHEMA, "version": max(0, version),
                "outputs": outputs}

    def save(self, character: str, document: Mapping[str, Any]) -> None:
        path = self._path(character)
        payload = wrap({
            "version": max(0, _as_int(document.get("version"), 0)),
            "outputs": {
                str(key): dict(row)
                for key, row in (document.get("outputs") or {}).items()
                if isinstance(row, Mapping)
            },
        }, version=OUTPUTS_SCHEMA)
        self._permit.assert_writable("save_outputs", str(path))
        self._store.save(path, payload)

    # -- reads -----------------------------------------------------------

    def get(self, character: str, output_id: str) -> dict[str, Any] | None:
        return self.load(character)["outputs"].get(str(output_id or "").strip())

    def list(
        self, character: str, *, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self.load(character)["outputs"].values())
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        cap = MAX_RECORDS if limit is None else max(0, int(limit))
        return rows[:cap]

    # -- writes ----------------------------------------------------------

    @staticmethod
    def new_output_id() -> str:
        return "out_" + uuid.uuid4().hex

    @staticmethod
    def output_id_for(character: str, subject: str, request_id: str) -> str:
        """Stable id for an idempotent create (same request -> same output)."""
        blob = f"{character}|{subject}|{request_id}".encode("utf-8")
        return "out_" + hashlib.sha256(blob).hexdigest()[:20]

    def write(self, character: str, row: Mapping[str, Any]) -> dict[str, Any]:
        """Upsert one row and bump the table version (not journaled here)."""
        path = self._path(character)
        with _lock_for(path):
            document = self.load(character)
            normalized = merge_row(document, row)
            self.save(character, document)
            return normalized

    def mutate(self, document: dict[str, Any], row: Mapping[str, Any]) -> None:
        """In-place upsert for journal.record_and_save's mutate_view."""
        merge_row(document, row)

    def apply_replay(self, character: str, payload: Any) -> bool:
        """Journal replay: rebuild one row from its own snapshot."""
        if not isinstance(payload, Mapping):
            return False
        row = payload.get("output")
        if not isinstance(row, Mapping):
            return False
        path = self._path(character)
        with _lock_for(path):
            document = self.load(character)
            if not merge_row(document, row, guard_older=True):
                return False
            self.save(character, document)
            return True

    # -- normalization ---------------------------------------------------

    @staticmethod
    def _normalize(raw: dict[str, Any], fallback_id: str) -> dict[str, Any]:
        output_id = _text(raw.get("output_id"), MAX_ID_CHARS) or fallback_id
        if not output_id:
            return {}
        status = str(raw.get("status") or STATUS_PENDING)
        if status not in STATUSES:
            status = STATUS_PENDING
        text = str(raw.get("text") or "")[:MAX_TEXT_CHARS]
        segments_raw = raw.get("segments")
        segments: list[dict[str, Any]] = []
        if isinstance(segments_raw, (list, tuple)):
            offset = 0
            for index, item in enumerate(segments_raw):
                if not isinstance(item, Mapping):
                    continue
                chars = _as_int(item.get("chars"), 0)
                if chars <= 0:
                    continue
                segments.append({
                    "index": index, "chars": chars, "offset": offset,
                    "text": str(item.get("text") or "")[:chars],
                })
                offset += chars
        if not segments and text:
            try:
                segments = split_segments(text)
            except ValueError:
                segments = []
        delivered: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in raw.get("delivered") or ():
            if not isinstance(item, Mapping):
                continue
            index = _as_int(item.get("index"), -1)
            if index < 0 or index in seen or index >= len(segments):
                continue
            seen.add(index)
            delivered.append({
                "index": index,
                "chars": _as_int(item.get("chars"), segments[index]["chars"]),
                "platform_id": _text(item.get("platform_id"), MAX_PLATFORM_ID_CHARS),
                "at": _text(item.get("at"), 40) or None,
            })
        delivered.sort(key=lambda item: item["index"])
        delivered_chars = sum(
            segments[item["index"]]["chars"] for item in delivered
        )
        cause_raw = raw.get("cause")
        cause: dict[str, Any] = {}
        if isinstance(cause_raw, Mapping):
            keys = [
                _text(value, MAX_ID_CHARS * 2)
                for value in cause_raw.get("source_keys") or ()
            ]
            cause = {
                "kind": _text(cause_raw.get("kind"), 40),
                "source_keys": [value for value in keys if value][:MAX_CAUSE_KEYS],
                "turn_id": _text(cause_raw.get("turn_id"), MAX_ID_CHARS) or None,
                "request_id": _text(cause_raw.get("request_id"), MAX_ID_CHARS) or None,
                "note": _text(cause_raw.get("note"), MAX_REASON_CHARS) or None,
                # M6-A additive fields: which proactive candidate produced this
                # body, and which open thread it belongs to. Existing keys keep
                # their exact shape and meaning.
                "candidate_id": _text(cause_raw.get("candidate_id"), MAX_ID_CHARS) or None,
                "thread_id": _text(cause_raw.get("thread_id"), MAX_ID_CHARS) or None,
            }
        row = {
            "output_id": output_id,
            "status": status,
            "text": text,
            "chars": len(text),
            "segments": segments,
            "delivered": delivered,
            "delivered_chars": delivered_chars,
            "kind": str(raw.get("kind") or DEFAULT_KIND),
            "content_class": CONTENT_CLASS,
            "subject": _text(raw.get("subject"), 256) or "private",
            "scene": _text(raw.get("scene"), 128),
            "session_id": _text(raw.get("session_id"), 128),
            "client_id": _text(raw.get("client_id"), 128),
            "episode_id": _text(raw.get("episode_id"), 200) or None,
            "source_key": _text(raw.get("source_key"), 400) or None,
            "cause": cause,
            "reason": _text(raw.get("reason"), MAX_REASON_CHARS) or None,
            "created_at": _text(raw.get("created_at"), 40) or None,
            "updated_at": _text(raw.get("updated_at"), 40) or None,
            "first_delivered_at": _text(raw.get("first_delivered_at"), 40) or None,
            "delivered_at": _text(raw.get("delivered_at"), 40) or None,
            "interrupted_at": _text(raw.get("interrupted_at"), 40) or None,
            "invalidated_at": _text(raw.get("invalidated_at"), 40) or None,
        }
        if row["kind"] not in KINDS:
            row["kind"] = DEFAULT_KIND
        return row


def merge_row(
    document: dict[str, Any], row: Mapping[str, Any], *, guard_older: bool = False,
) -> dict[str, Any] | None:
    """Normalize and upsert one row into a loaded document; bump its version.

    Returns the normalized row, or None when the incoming snapshot is older
    than what the document already holds (replay of a stale entry).
    """
    outputs = document.get("outputs")
    if not isinstance(outputs, dict):
        outputs = {}
        document["outputs"] = outputs
    normalized = OutputStore._normalize(dict(row), str(row.get("output_id") or ""))
    if not normalized:
        return None
    current = outputs.get(normalized["output_id"])
    if guard_older and isinstance(current, Mapping):
        if str(current.get("updated_at") or "") > str(normalized.get("updated_at") or ""):
            return None
    outputs[normalized["output_id"]] = normalized
    while len(outputs) > MAX_RECORDS:
        victim = min(outputs.values(), key=lambda item: str(item.get("created_at") or ""))
        outputs.pop(str(victim.get("output_id") or ""), None)
    document["version"] = _as_int(document.get("version"), 0) + 1
    return normalized


def snapshot_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Journal payload shape for core.output_recorded."""
    return {"output": _row_snapshot(row)}


__all__ = [
    "CONTENT_CLASS",
    "DEFAULT_KIND",
    "KINDS",
    "MAX_PART_CHARS",
    "MAX_RECORDS",
    "OUTPUTS_FILENAME",
    "OUTPUTS_SCHEMA",
    "OutputRecord",
    "OutputStore",
    "RESUMABLE_STATUSES",
    "STATUSES",
    "STATUS_DELIVERED",
    "STATUS_FAILED",
    "STATUS_INTERRUPTED",
    "STATUS_INVALIDATED",
    "STATUS_PARTIAL",
    "STATUS_PENDING",
    "deliverable_segments",
    "delivered_indices",
    "snapshot_payload",
    "split_segments",
    "status_after_delivery",
]
