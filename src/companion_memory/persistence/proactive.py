"""Durable proactive candidates, the pending-chat intent queue and topic pauses (M6-A).

Design section 11.3 fixes the rule this store exists to make enforceable: a
proactive message is not generated out of nowhere, it is *carried by a real
reason* -- an open item that reached a good time to follow up, an agreed
reminder/check-in/share, a real task result, new information about a sustained
interest, or a finding that came out of a shared experience. Section 11.4 adds
the other half: the pending-chat intent queue (what she wants to continue, share,
or has just made) is what makes an unprompted message feel like a person rather
than a reminder service.

So a candidate row always carries its cause AND the records that cause rests on
(source_keys / fact_ids / reflection_ids / thread_id / output_id / episode_id).
A candidate with no source is refused at enqueue time (see domain.proactive);
this store therefore never has to answer "why".

Candidate states
----------------
    pending     waiting for its moment
    postponed   delayed by the user or by scheduling pressure (same source)
    merged      folded into another candidate or into the user own turn
    cancelled   the user said "先不聊" / "别再提"
    sent        an output record was created for it (the body lives in outputs)
    stale       a pre-send context check found the moment has passed
    invalidated its source was deleted (text cleared, id kept)

Topic pauses ("先不聊") are stored here too, next to the candidates they silence:
a pause is a temporary "not now" that expires on its own, and it is neither a
mention restriction (that is the existing user-directive store) nor a deletion
(that is the barrier). Keeping the three apart is design section 10.4.

On-disk shape is a versioned envelope; unknown fields survive a round trip so a
future build can add one without a second reader.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .schema import load_versioned, wrap

PROACTIVE_FILENAME = "proactive_candidates.json"
PROACTIVE_SCHEMA = 1

#: The five real causes (design section 11.3). Every candidate names one.
CAUSES: tuple[str, ...] = (
    "thread_due",       # an open item reached its follow-up time
    "agreement",        # an agreed reminder / check-in / share
    "task_result",      # a real task produced a new result
    "interest_news",    # new information about a sustained interest
    "shared_finding",   # a shared experience produced a finding
)
#: Why she wants to talk, in her own terms (the pending-chat intent queue).
INTENT_KINDS: tuple[str, ...] = (
    "continue_topic",   # pick up something left unfinished
    "interest_share",   # share something about a shared interest
    "new_idea",         # a thought of her own
    "creation",         # something she actually made
)
#: How the cause earned its right to be a candidate.
MATURITY_SOURCES: tuple[str, ...] = (
    "explicit_agreement",
    "task_result",
    "autonomous_inference",
)
#: Maturity sources that may form a candidate directly (no eligibility gate).
DIRECT_SOURCES = frozenset({"explicit_agreement", "task_result"})
#: Maturity source that must pass the M4 eligibility gate.
INFERRED_SOURCE = "autonomous_inference"

STATUS_PENDING = "pending"
STATUS_POSTPONED = "postponed"
STATUS_MERGED = "merged"
STATUS_CANCELLED = "cancelled"
STATUS_SENT = "sent"
STATUS_STALE = "stale"
STATUS_INVALIDATED = "invalidated"
STATUSES = frozenset({
    STATUS_PENDING, STATUS_POSTPONED, STATUS_MERGED, STATUS_CANCELLED,
    STATUS_SENT, STATUS_STALE, STATUS_INVALIDATED,
})
#: Statuses that may still be sent.
LIVE_STATUSES = frozenset({STATUS_PENDING, STATUS_POSTPONED})

MAX_CANDIDATES = 256
MAX_TEXT_CHARS = 600
MAX_WHY_CHARS = 400
MAX_ID_CHARS = 120
MAX_SOURCE_KEYS = 16
MAX_REASON_CHARS = 200
MAX_REWRITES = 20
MAX_PAUSES = 32
#: Recent user utterances kept per character (topic cooldown lookback).
MAX_UTTERANCES = 32
#: One utterance is capped like a candidate body: it is used for topic matching,
#: never replayed as content.
MAX_UTTERANCE_CHARS = 400

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


def candidate_id_for(character: str, subject: str, cause: str, key: str) -> str:
    """Stable id so re-deriving the same reason is a duplicate, not a second row.

    This is what keeps the maintenance pass idempotent: the same open item
    derived twice must update one candidate, not queue two messages.
    """
    blob = f"{character}|{subject}|{cause}|{key}".encode("utf-8")
    return "pc_" + hashlib.sha256(blob).hexdigest()[:20]


def new_candidate_id() -> str:
    return "pc_" + uuid.uuid4().hex


class CandidateRecord:
    """One candidate as a caller sees it (cause, source and state)."""

    __slots__ = ("row",)

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.row = dict(row)

    @property
    def candidate_id(self) -> str:
        return str(self.row.get("candidate_id") or "")

    @property
    def status(self) -> str:
        return str(self.row.get("status") or STATUS_PENDING)

    @property
    def cause(self) -> str:
        return str(self.row.get("cause") or "")

    @property
    def live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def has_source(self) -> bool:
        return bool(
            self.row.get("source_keys")
            or self.row.get("fact_ids")
            or self.row.get("reflection_ids")
            or self.row.get("thread_id")
            or self.row.get("episode_id")
        )

    def to_public(self) -> dict[str, Any]:
        row = self.row
        return {
            "candidate_id": self.candidate_id,
            "cause": self.cause,
            "intent": row.get("intent") or None,
            "maturity_source": str(row.get("maturity_source") or ""),
            "text": str(row.get("text") or ""),
            "why": str(row.get("why") or ""),
            "subject": str(row.get("subject") or "private"),
            "scene": str(row.get("scene") or ""),
            "status": self.status,
            "due_at": row.get("due_at"),
            "source_keys": [str(item) for item in row.get("source_keys") or ()],
            "fact_ids": [str(item) for item in row.get("fact_ids") or ()],
            "reflection_ids": [str(item) for item in row.get("reflection_ids") or ()],
            "thread_id": row.get("thread_id") or None,
            "episode_id": row.get("episode_id") or None,
            "output_id": row.get("output_id") or None,
            "agreed_at": row.get("agreed_at"),
            "honor_agreed_time": bool(row.get("honor_agreed_time")),
            "eligibility": dict(row.get("eligibility") or {}),
            "merged_into": row.get("merged_into") or None,
            "absorbed_into": row.get("absorbed_into") or None,
            "cancelled_reason": row.get("cancelled_reason") or None,
            "stale_reason": row.get("stale_reason") or None,
            "rewrite_history": [dict(item) for item in row.get("rewrite_history") or ()],
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "sent_at": row.get("sent_at"),
        }


class ProactiveStore:
    """Versioned candidate table plus topic pauses for one character directory."""

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

    def _path(self, character: str) -> Path:
        return self._roots.character_file(character, PROACTIVE_FILENAME)

    # -- io --------------------------------------------------------------

    def load(self, character: str) -> dict[str, Any]:
        document = load_versioned(
            self._store, self._path(character),
            supported=frozenset({PROACTIVE_SCHEMA}),
            default={"version": 0, "candidates": {}, "pauses": []},
        )
        raw = document.get("candidates")
        candidates: dict[str, dict[str, Any]] = {}
        if isinstance(raw, Mapping):
            for key, row in raw.items():
                if not isinstance(row, Mapping):
                    continue
                normalized = self._normalize(dict(row), str(key))
                if normalized:
                    candidates[normalized["candidate_id"]] = normalized
        pauses: list[dict[str, Any]] = []
        for item in document.get("pauses") or ():
            if not isinstance(item, Mapping):
                continue
            term = _text(item.get("term"), 40)
            if not term:
                continue
            pauses.append({
                "term": term,
                "subject": _text(item.get("subject"), 256) or "private",
                "created_at": _text(item.get("created_at"), 40) or None,
                "expires_at": _text(item.get("expires_at"), 40) or None,
                "reason": _text(item.get("reason"), MAX_REASON_CHARS) or None,
            })
        utterances: list[dict[str, Any]] = []
        for item in document.get("utterances") or ():
            if not isinstance(item, Mapping):
                continue
            text = _text(item.get("text"), MAX_UTTERANCE_CHARS)
            at = _text(item.get("at"), 40)
            if not text or not at:
                continue
            utterances.append({
                "text": text,
                "at": at,
                "subject": _text(item.get("subject"), 256) or "private",
                "request_id": _text(item.get("request_id"), MAX_ID_CHARS) or None,
            })
        return {
            "schema_version": PROACTIVE_SCHEMA,
            "version": max(0, _as_int(document.get("version"), 0)),
            "candidates": candidates,
            "pauses": pauses[-MAX_PAUSES:],
            "utterances": utterances[-MAX_UTTERANCES:],
        }

    def save(self, character: str, document: Mapping[str, Any]) -> None:
        path = self._path(character)
        pauses = [dict(item) for item in (document.get("pauses") or ()) if isinstance(item, Mapping)]
        utterances = [
            dict(item) for item in (document.get("utterances") or ())
            if isinstance(item, Mapping)
        ]
        payload = wrap({
            "version": max(0, _as_int(document.get("version"), 0)),
            "candidates": {
                str(key): dict(row)
                for key, row in (document.get("candidates") or {}).items()
                if isinstance(row, Mapping)
            },
            "pauses": pauses[-MAX_PAUSES:],
            "utterances": utterances[-MAX_UTTERANCES:],
        }, version=PROACTIVE_SCHEMA)
        self._permit.assert_writable("save_proactive_candidates", str(path))
        self._store.save(path, payload)

    # -- reads -----------------------------------------------------------

    def get(self, character: str, candidate_id: str) -> dict[str, Any] | None:
        return self.load(character)["candidates"].get(str(candidate_id or "").strip())

    def list(
        self,
        character: str,
        *,
        subject: str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self.load(character)["candidates"].values())
        if subject is not None:
            rows = [row for row in rows if str(row.get("subject")) == str(subject)]
        if statuses:
            wanted = {str(item) for item in statuses}
            rows = [row for row in rows if str(row.get("status")) in wanted]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        cap = MAX_CANDIDATES if limit is None else max(0, int(limit))
        return rows[:cap]

    def active_pauses(self, character: str, *, subject: str | None = None) -> list[dict[str, Any]]:
        """Unexpired topic pauses; expiry is filtered on read, never written back."""
        now = self._clock.now()
        out: list[dict[str, Any]] = []
        for item in self.load(character)["pauses"]:
            expires = _parse_stamp(item.get("expires_at"))
            if expires is not None and expires <= now:
                continue
            if subject is not None and str(item.get("subject")) != str(subject):
                continue
            out.append(dict(item))
        return out

    def recent_utterances(
        self, character: str, *, subject: str | None = None, since=None,
    ) -> list[dict[str, Any]]:
        """What the user said recently, newest last (topic-cooldown lookback).

        Stored as text plus a timestamp because the conversation window carries
        no timestamps of its own: without this record the service cannot tell
        "she just asked about the interview" from "the user mentioned it an hour
        ago", and would repeat a topic the user already opened.
        """
        out: list[dict[str, Any]] = []
        for item in self.load(character)["utterances"]:
            if subject is not None and str(item.get("subject")) != str(subject):
                continue
            if since is not None:
                moment = _parse_stamp(item.get("at"))
                if moment is None or moment < since:
                    continue
            out.append(dict(item))
        return out

    # -- writes ----------------------------------------------------------

    def record_utterance(
        self,
        character: str,
        *,
        text: str,
        at: str,
        subject: str = "private",
        request_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Append one user utterance to the bounded topic-cooldown log."""
        value = _text(text, MAX_UTTERANCE_CHARS)
        stamp = _text(at, 40)
        if not value or not stamp:
            return None
        path = self._path(character)
        with _lock_for(path):
            document = self.load(character)
            rows = [dict(item) for item in document.get("utterances") or ()]
            rows.append({
                "text": value,
                "at": stamp,
                "subject": str(subject or "private"),
                "request_id": _text(request_id, MAX_ID_CHARS) or None,
            })
            document["utterances"] = rows[-MAX_UTTERANCES:]
            document["version"] = _as_int(document.get("version"), 0) + 1
            self.save(character, document)
            return dict(rows[-1])

    def mutate(self, document: dict[str, Any], row: Mapping[str, Any]) -> None:
        merge_row(document, row)

    def write(self, character: str, row: Mapping[str, Any]) -> dict[str, Any]:
        path = self._path(character)
        with _lock_for(path):
            document = self.load(character)
            normalized = merge_row(document, row)
            self.save(character, document)
            return normalized or dict(row)

    def record_pause(
        self,
        character: str,
        *,
        term: str,
        subject: str = "private",
        expires_at: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Add or renew one topic pause; a repeated term keeps one row."""
        value = _text(term, 40)
        if not value:
            raise ValueError("pause term must be nonempty")
        path = self._path(character)
        with _lock_for(path):
            document = self.load(character)
            pauses = [dict(item) for item in document.get("pauses") or ()]
            kept: list[dict[str, Any]] = []
            written: dict[str, Any] | None = None
            for item in pauses:
                same = (
                    str(item.get("term")) == value
                    and str(item.get("subject")) == str(subject or "private")
                )
                if not same:
                    kept.append(item)
                    continue
                renewed = dict(item)
                renewed["expires_at"] = str(expires_at)
                renewed["reason"] = _text(reason, MAX_REASON_CHARS) or None
                written = renewed
                kept.append(renewed)
            if written is None:
                written = {
                    "term": value,
                    "subject": str(subject or "private"),
                    "created_at": self._clock.now().isoformat(),
                    "expires_at": str(expires_at),
                    "reason": _text(reason, MAX_REASON_CHARS) or None,
                }
                kept.append(written)
            document["pauses"] = kept[-MAX_PAUSES:]
            document["version"] = _as_int(document.get("version"), 0) + 1
            self.save(character, document)
            return dict(written)

    def apply_replay(self, character: str, payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False
        row = payload.get("candidate")
        if not isinstance(row, Mapping):
            return False
        path = self._path(character)
        with _lock_for(path):
            document = self.load(character)
            if merge_row(document, row, guard_older=True) is None:
                return False
            self.save(character, document)
            return True

    # -- normalization ---------------------------------------------------

    @staticmethod
    def _normalize(raw: dict[str, Any], fallback_id: str) -> dict[str, Any]:
        candidate_id = _text(raw.get("candidate_id"), MAX_ID_CHARS) or fallback_id
        if not candidate_id:
            return {}
        status = str(raw.get("status") or STATUS_PENDING)
        if status not in STATUSES:
            status = STATUS_PENDING
        cause = str(raw.get("cause") or "")
        if cause not in CAUSES:
            cause = ""
        intent = str(raw.get("intent") or "")
        if intent not in INTENT_KINDS:
            intent = ""
        maturity = str(raw.get("maturity_source") or "")
        if maturity not in MATURITY_SOURCES:
            maturity = ""

        def _ids(key: str, cap: int) -> list[str]:
            out: list[str] = []
            for item in raw.get(key) or ():
                value = _text(item, 400 if key == "source_keys" else MAX_ID_CHARS)
                if value and value not in out:
                    out.append(value)
                if len(out) >= cap:
                    break
            return out

        rewrites: list[dict[str, Any]] = []
        for item in raw.get("rewrite_history") or ():
            if not isinstance(item, Mapping):
                continue
            text = _text(item.get("text"), MAX_TEXT_CHARS)
            if not text:
                continue
            rewrites.append({"text": text, "at": _text(item.get("at"), 40) or None})
        rewrites = rewrites[-MAX_REWRITES:]
        eligibility = raw.get("eligibility")
        return {
            "candidate_id": candidate_id,
            "cause": cause,
            "intent": intent or None,
            "maturity_source": maturity,
            "text": _text(raw.get("text"), MAX_TEXT_CHARS),
            "why": _text(raw.get("why"), MAX_WHY_CHARS),
            "subject": _text(raw.get("subject"), 256) or "private",
            "scene": _text(raw.get("scene"), 128),
            "status": status,
            "due_at": _text(raw.get("due_at"), 40) or None,
            "agreed_at": _text(raw.get("agreed_at"), 40) or None,
            "honor_agreed_time": bool(raw.get("honor_agreed_time")),
            "source_keys": _ids("source_keys", MAX_SOURCE_KEYS),
            "fact_ids": _ids("fact_ids", 16),
            "reflection_ids": _ids("reflection_ids", 16),
            "thread_id": _text(raw.get("thread_id"), MAX_ID_CHARS) or None,
            "thread_revision": _text(raw.get("thread_revision"), 40) or None,
            "episode_id": _text(raw.get("episode_id"), 200) or None,
            "output_id": _text(raw.get("output_id"), MAX_ID_CHARS) or None,
            "eligibility": dict(eligibility) if isinstance(eligibility, Mapping) else {},
            "merged_into": _text(raw.get("merged_into"), MAX_ID_CHARS) or None,
            "absorbed_into": _text(raw.get("absorbed_into"), 400) or None,
            "cancelled_reason": _text(raw.get("cancelled_reason"), MAX_REASON_CHARS) or None,
            "stale_reason": _text(raw.get("stale_reason"), MAX_REASON_CHARS) or None,
            "rewrite_history": rewrites,
            "created_at": _text(raw.get("created_at"), 40) or None,
            "updated_at": _text(raw.get("updated_at"), 40) or None,
            "sent_at": _text(raw.get("sent_at"), 40) or None,
            "lease_id": _text(raw.get("lease_id"), 128) or None,
            "lease_owner": _text(raw.get("lease_owner"), 256) or None,
            "lease_session": _text(raw.get("lease_session"), 256) or None,
            "lease_scene": _text(raw.get("lease_scene"), 128),
            "lease_request": _text(raw.get("lease_request"), 256) or None,
            "lease_until": _text(raw.get("lease_until"), 40) or None,
            "lease_user_event": _text(raw.get("lease_user_event"), 400) or None,
        }


def _parse_stamp(value: Any):
    from datetime import datetime, timezone

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def merge_row(
    document: dict[str, Any], row: Mapping[str, Any], *, guard_older: bool = False,
) -> dict[str, Any] | None:
    """Normalize and upsert one candidate; returns the row or None (stale)."""
    candidates = document.get("candidates")
    if not isinstance(candidates, dict):
        candidates = {}
        document["candidates"] = candidates
    normalized = ProactiveStore._normalize(
        dict(row), str(row.get("candidate_id") or ""),
    )
    if not normalized:
        return None
    current = candidates.get(normalized["candidate_id"])
    if guard_older and isinstance(current, Mapping):
        if str(current.get("updated_at") or "") > str(normalized.get("updated_at") or ""):
            return None
    candidates[normalized["candidate_id"]] = normalized
    while len(candidates) > MAX_CANDIDATES:
        # Only finished rows are rotation victims: a live candidate must never
        # be dropped just because the table is full.
        finished = [
            item for item in candidates.values()
            if str(item.get("status")) not in LIVE_STATUSES
        ]
        victim = min(
            finished or list(candidates.values()),
            key=lambda item: str(item.get("created_at") or ""),
        )
        candidates.pop(str(victim.get("candidate_id") or ""), None)
    document["version"] = _as_int(document.get("version"), 0) + 1
    return normalized


def snapshot_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Journal payload shape for core.proactive_candidate."""
    return {"candidate": dict(row)}


__all__ = [
    "CAUSES",
    "CandidateRecord",
    "DIRECT_SOURCES",
    "INFERRED_SOURCE",
    "INTENT_KINDS",
    "LIVE_STATUSES",
    "MATURITY_SOURCES",
    "MAX_CANDIDATES",
    "MAX_UTTERANCES",
    "PROACTIVE_FILENAME",
    "PROACTIVE_SCHEMA",
    "ProactiveStore",
    "STATUSES",
    "STATUS_CANCELLED",
    "STATUS_INVALIDATED",
    "STATUS_MERGED",
    "STATUS_PENDING",
    "STATUS_POSTPONED",
    "STATUS_SENT",
    "STATUS_STALE",
    "candidate_id_for",
    "merge_row",
    "new_candidate_id",
    "snapshot_payload",
]
