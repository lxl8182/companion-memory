"""B07 companion-state domain service: versioned, leased, idempotent commits.

The host keeps a short-term shared state per character/subject (thoughts, moods,
concerns, progress) that ordinary chat, proactive chat and game control all read.
This service owns the commit semantics; \`rin_memory.persistence.state\`
owns the file shape and the pure apply/render rules.

Frozen commit order (host \`memory/companion_state.py:1189-1223\`):

1. external lease check (internal=true skips it);
2. request_id idempotency (\`producer:request_id\` + payload fingerprint);
3. base_version compare-and-swap (required for external commits);
4. apply ops, prune, bump version, write the full-snapshot journal event and the
   view.

A conflict never writes: the receipt carries \`conflict=True\` and the current
snapshot so the caller can re-read and retry against the new version. A no-op
without \`request_id\` writes nothing; a no-op with \`request_id\` records the
request and appends a same-version snapshot event (so a retried "commit
succeeded but the ack was lost" is a duplicate, not a version conflict).

Every state write goes through \`EventJournal.record_and_save\` with the event
\`companion.state_committed\`, whose payload is
\`{character, subject, snapshot}\`; the handler registered in the constructor is
\`StateStore.replay_snapshot\`, so reconcile repairs a crash between event and
view. Mood decay is computed at read/commit time from the injected clock and is
never written back, so a restart cannot refresh an old mood.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from ..persistence.journal import EventJournal
from ..persistence.state import (
    DEFAULT_SUBJECT,
    EVT_COMPANION_STATE_COMMITTED,
    LEASE_DEFAULT_TTL_SECONDS,
    LEASE_MAX_TTL_SECONDS,
    LEASE_RENEW_MIN_REMAINING_SECONDS,
    MAX_PROCESSED_EVENTS,
    REASON_LEASE_STALE,
    REASON_REQUEST_ID_CONFLICT,
    REASON_VERSION_CONFLICT,
    SCHEMA_VERSION,
    STATE_KINDS,
    StateRequestError,
    StateStore,
    apply_ops,
    cap_requests,
    cap_tail,
    document_with_subject,
    normalize_commit_request,
    normalize_subject_key,
    normalize_subject_row,
    op_already_processed,
    prune_state,
    public_state,
    render_state_block,
    STATE_BLOCK_MAX_TOKENS,
)
from ..ports import Clock, StorageRoots, WritePermit

@dataclass(frozen=True)
class StateReadRequest:
    """Parameters for one state read (all optional, host defaults apply)."""

    subject: str = DEFAULT_SUBJECT
    scene: Mapping[str, str] | None = None
    since_version: int | None = None
    language: str | None = None


@dataclass(frozen=True)
class StateCommitRequest:
    """One state commit request (normalized/validated by the service)."""

    subject: str = DEFAULT_SUBJECT
    ops: Sequence[Mapping[str, Any]] = ()
    scene: Mapping[str, str] | None = None
    producer: str = "external"
    instance: str | None = None
    lease_id: str | None = None
    request_id: str | None = None
    base_version: int | None = None
    processed_event_ids: Sequence[str] = ()
    internal: bool = False
    language: str | None = None


@dataclass(frozen=True)
class StateSnapshot:
    """Public read view: host \`GET companion_state\` fields (design §17.5.4)."""

    ok: bool = True
    subject: str = DEFAULT_SUBJECT
    version: int = 0
    updated_at: str | None = None
    state: Mapping[str, Any] = field(default_factory=dict)
    items: tuple[Mapping[str, Any], ...] = ()
    block: str = ""
    unchanged: bool = False
    lease: Mapping[str, Any] | None = None
    processed_events: tuple[str, ...] = ()


@dataclass(frozen=True)
class StateReceipt:
    """One commit/lease result; conflicts carry the current snapshot."""

    ok: bool = True
    version: int = 0
    applied: bool = False
    duplicate: bool = False
    state: Mapping[str, Any] = field(default_factory=dict)
    block: str = ""
    updated_at: str | None = None
    conflict: bool = False
    reason: str | None = None
    current: StateSnapshot | None = None
    event_id: str | None = None


class StateService:
    """Versioned companion-state reads/commits/leases over one data root.

    \`barrier\` is optional and purely defensive: state rows do not participate in
    item deletion (design §17.3), but a tombstoned item is filtered before it is
    served or written back.
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: StateStore,
        journal: EventJournal,
        clock: Clock,
        permit: WritePermit,
        barrier: Any = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._journal = journal
        self._clock = clock
        self._permit = permit
        self._barrier = barrier
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        # Register before any write: the journal refuses unhandled event types.
        self._journal.register_handler(
            EVT_COMPANION_STATE_COMMITTED, self._store.replay_snapshot,
        )

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

    # -- read -----------------------------------------------------------

    def read(
        self,
        character: str,
        request: StateReadRequest | Mapping[str, Any] | None = None,
        *,
        subject: str | None = None,
        scene: Mapping[str, str] | None = None,
        since_version: int | None = None,
        language: str | None = None,
    ) -> StateSnapshot:
        """Read one subject's public state (no writes, no decay write-back)."""
        character = self._character(character)
        params = _read_params(request, subject, scene, since_version, language)
        subject_key = normalize_subject_key(params["subject"])
        document = self._store.load(character)
        document = self._filter_blocked(character, document, subject_key)
        row = normalize_subject_row(document["subjects"].get(subject_key))
        snapshot = self._snapshot(
            character, subject_key, row,
            scene=params["scene"], language=params["language"],
        )
        unchanged = False
        since = params["since_version"]
        if since is not None:
            try:
                unchanged = int(since) == snapshot.version
            except (TypeError, ValueError):
                unchanged = False
        return replace(snapshot, unchanged=unchanged)

    # -- commit ---------------------------------------------------------

    def commit(
        self,
        character: str,
        request: StateCommitRequest | Mapping[str, Any] | None = None,
        *,
        language: str | None = None,
        **overrides: Any,
    ) -> StateReceipt:
        """Commit one increment; conflicts return a receipt, never raise."""
        character = self._character(character)
        raw = _commit_mapping(request, overrides)
        try:
            req = normalize_commit_request(
                raw, language=language or raw.get("language"),
            )
        except StateRequestError as exc:
            return StateReceipt(ok=False, conflict=False, reason=exc.reason)
        subject_key = req["subject"]
        with self._lock(character):
            document = self._store.load(character)
            current = normalize_subject_row(document["subjects"].get(subject_key))
            now = self._clock.now()
            # 1) lease (external commits only)
            if not req["internal"] and not _lease_valid(
                current.get("lease"), req["lease_id"], req["instance"], now,
            ):
                return self._conflict(character, req, current, REASON_LEASE_STALE)
            current_version = int(current.get("version") or 0)
            requests = dict(current.get("requests") or {})
            request_key: str | None = None
            # 2) request_id idempotency
            if req["request_id"]:
                request_key = f"{req['producer']}:{req['request_id']}"
                stored = requests.get(request_key)
                if isinstance(stored, Mapping):
                    if stored.get("fingerprint") == req["fingerprint"]:
                        return self._receipt_from_row(
                            character, req, current,
                            applied=False, duplicate=True,
                        )
                    return self._conflict(
                        character, req, current, REASON_REQUEST_ID_CONFLICT,
                    )
            # 3) base_version CAS (external commits must send it)
            base_version = req["base_version"]
            if base_version is not None:
                if int(base_version) != current_version:
                    return self._conflict(
                        character, req, current, REASON_VERSION_CONFLICT,
                    )
            elif not req["internal"]:
                return self._conflict(
                    character, req, current, REASON_VERSION_CONFLICT,
                )
            # 4) apply
            processed = set(current.get("processed_events") or ())
            effective_ops = [
                op for op in req["ops"]
                if not op_already_processed(op, processed)
            ]
            new_events: list[str] = []
            for event_id in req["processed_event_ids"]:
                if event_id not in processed and event_id not in new_events:
                    new_events.append(event_id)
            state = copy.deepcopy(current.get("state") or {kind: [] for kind in STATE_KINDS})
            applied = apply_ops(state, effective_ops, now)
            for op in effective_ops:
                for source_id in op.get("source_event_ids") or ():
                    if source_id not in processed and source_id not in new_events:
                        new_events.append(source_id)
            if not applied and not new_events:
                if request_key is None:
                    # No semantic change and no idempotency record: no event.
                    return self._receipt_from_row(
                        character, req, current,
                        applied=False, duplicate=False,
                    )
                # Record the request result without bumping the version; the
                # same-version snapshot event makes a retry a duplicate.
                requests[request_key] = {
                    "fingerprint": req["fingerprint"],
                    "at": self._iso(now),
                    "version": current_version,
                    "applied": False,
                }
                snapshot = dict(current)
                snapshot["requests"] = cap_requests(requests)
                snapshot = self._filter_blocked_row(character, snapshot)
                event_id = self._write_snapshot(
                    character, subject_key, snapshot,
                )
                return self._receipt_from_snapshot(
                    character, req, snapshot,
                    applied=False, duplicate=False, event_id=event_id,
                )
            pruned = prune_state(state, now)
            pruned = self._drop_blocked_items(character, pruned)
            new_version = current_version + 1
            processed_events = cap_tail(
                list(current.get("processed_events") or ()) + new_events,
                MAX_PROCESSED_EVENTS,
            )
            if request_key is not None:
                requests[request_key] = {
                    "fingerprint": req["fingerprint"],
                    "at": self._iso(now),
                    "version": new_version,
                    "applied": True,
                }
                requests = cap_requests(requests)
            snapshot = {
                "schema": SCHEMA_VERSION,
                "subject": subject_key,
                "version": new_version,
                "updated_at": self._iso(now),
                "state": pruned,
                "processed_events": processed_events,
                "requests": requests,
                "lease": current.get("lease"),
            }
            event_id = self._write_snapshot(character, subject_key, snapshot)
            return self._receipt_from_snapshot(
                character, req, snapshot,
                applied=True, duplicate=False, event_id=event_id,
            )

    # -- leases ---------------------------------------------------------

    def claim_lease(
        self,
        character: str,
        *,
        subject: str = DEFAULT_SUBJECT,
        instance: str,
        lease_id: str,
        scene: Mapping[str, str] | None = None,
        ttl_seconds: int = LEASE_DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Claim/renew the write lease; another live instance returns conflict."""
        character = self._character(character)
        instance = _require_text(instance, "instance")
        lease_id = _require_text(lease_id, "lease_id")
        subject_key = normalize_subject_key(subject)
        ttl = _normalize_ttl(ttl_seconds)
        with self._lock(character):
            document = self._store.load(character)
            current = normalize_subject_row(document["subjects"].get(subject_key))
            now = self._clock.now()
            lease = current.get("lease")
            version = int(current.get("version") or 0)
            if isinstance(lease, Mapping) and (
                lease.get("lease_id") == lease_id
                and lease.get("instance") == instance
            ):
                expires_at = _parse_aware(lease.get("expires_at"), now)
                remaining = (
                    (expires_at - now).total_seconds() if expires_at else 0.0
                )
                if remaining > LEASE_RENEW_MIN_REMAINING_SECONDS:
                    return {
                        "ok": True,
                        "lease": dict(lease),
                        "version": version,
                        "renewed": False,
                    }
            expires_at = _parse_aware((lease or {}).get("expires_at"), now)
            if (
                isinstance(lease, Mapping)
                and lease.get("instance") != instance
                and expires_at is not None
                and expires_at > now
            ):
                return {
                    "ok": False,
                    "conflict": True,
                    "reason": REASON_LEASE_STALE,
                    "version": version,
                    "current": self._snapshot(
                        character, subject_key, current,
                        scene=scene, language=None,
                    ),
                }
            new_lease = {
                "lease_id": lease_id,
                "instance": instance,
                "scene": dict(scene or {}),
                "acquired_at": self._iso(now),
                "expires_at": self._iso(
                    now + timedelta(seconds=ttl),
                ),
            }
            snapshot = dict(current)
            snapshot["lease"] = new_lease
            self._write_snapshot(character, subject_key, snapshot)
            return {
                "ok": True,
                "lease": new_lease,
                "version": version,
                "renewed": True,
            }

    def release_lease(
        self,
        character: str,
        *,
        subject: str = DEFAULT_SUBJECT,
        lease_id: str,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """Release the lease; a stale/missing lease is an idempotent no-op."""
        character = self._character(character)
        lease_id = _require_text(lease_id, "lease_id")
        subject_key = normalize_subject_key(subject)
        with self._lock(character):
            document = self._store.load(character)
            current = normalize_subject_row(document["subjects"].get(subject_key))
            lease = current.get("lease")
            version = int(current.get("version") or 0)
            matched = (
                isinstance(lease, Mapping)
                and lease.get("lease_id") == lease_id
                and (instance is None or lease.get("instance") == instance)
            )
            if not matched:
                return {
                    "ok": True,
                    "released": False,
                    "reason": REASON_LEASE_STALE,
                    "lease": dict(lease) if isinstance(lease, Mapping) else None,
                    "version": version,
                }
            snapshot = dict(current)
            snapshot["lease"] = None
            self._write_snapshot(character, subject_key, snapshot)
            return {"ok": True, "released": True, "version": version}

    # -- async wrappers -------------------------------------------------

    async def aread(self, character: str, request: Any = None, **kwargs: Any) -> StateSnapshot:
        return await asyncio.to_thread(
            functools.partial(self.read, character, request, **kwargs),
        )

    async def acommit(self, character: str, request: Any = None, **kwargs: Any) -> StateReceipt:
        return await asyncio.to_thread(
            functools.partial(self.commit, character, request, **kwargs),
        )

    async def aclaim_lease(self, character: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(
            functools.partial(self.claim_lease, character, **kwargs),
        )

    async def arelease_lease(self, character: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(
            functools.partial(self.release_lease, character, **kwargs),
        )

    # -- internals ------------------------------------------------------

    def _iso(self, now: datetime) -> str:
        return now.isoformat()

    def _snapshot(
        self,
        character: str,
        subject: str,
        row: Mapping[str, Any],
        *,
        scene: Mapping[str, str] | None,
        language: str | None,
    ) -> StateSnapshot:
        now = self._clock.now()
        document = document_with_subject(subject, row)
        public = public_state(document, subject=subject, scene=scene, now=now)
        block = render_state_block(
            document, subject=subject, scene=scene,
            max_tokens=STATE_BLOCK_MAX_TOKENS, now=now, language=language,
        )
        return StateSnapshot(
            ok=True,
            subject=subject,
            version=int(public["version"]),
            updated_at=public.get("updated_at"),
            state=public["state"],
            items=tuple(public["items"]),
            block=block,
            lease=public.get("lease"),
            processed_events=tuple(public.get("processed_events") or ()),
        )

    def _conflict(
        self,
        character: str,
        req: Mapping[str, Any],
        current: Mapping[str, Any],
        reason: str,
    ) -> StateReceipt:
        snapshot = self._snapshot(
            character, req["subject"], current,
            scene=req.get("scene"), language=req.get("language"),
        )
        return StateReceipt(
            ok=False,
            version=snapshot.version,
            applied=False,
            duplicate=False,
            state=snapshot.state,
            block=snapshot.block,
            updated_at=snapshot.updated_at,
            conflict=True,
            reason=reason,
            current=snapshot,
        )

    def _receipt_from_row(
        self,
        character: str,
        req: Mapping[str, Any],
        row: Mapping[str, Any],
        *,
        applied: bool,
        duplicate: bool,
        event_id: str | None = None,
    ) -> StateReceipt:
        snapshot = self._snapshot(
            character, req["subject"], row,
            scene=req.get("scene"), language=req.get("language"),
        )
        return self._receipt_from_snapshot(
            character, req, row,
            applied=applied, duplicate=duplicate, event_id=event_id,
            snapshot=snapshot,
        )

    def _receipt_from_snapshot(
        self,
        character: str,
        req: Mapping[str, Any],
        row: Mapping[str, Any],
        *,
        applied: bool,
        duplicate: bool,
        event_id: str | None = None,
        snapshot: StateSnapshot | None = None,
    ) -> StateReceipt:
        snap = snapshot or self._snapshot(
            character, req["subject"], row,
            scene=req.get("scene"), language=req.get("language"),
        )
        return StateReceipt(
            ok=True,
            version=snap.version,
            applied=applied,
            duplicate=duplicate,
            state=snap.state,
            block=snap.block,
            updated_at=snap.updated_at,
            event_id=event_id,
        )

    def _write_snapshot(
        self, character: str, subject: str, snapshot: Mapping[str, Any],
    ) -> str | None:
        """Journal the full subject snapshot, then persist the view."""
        payload = {
            "character": character,
            "subject": subject,
            "snapshot": copy.deepcopy(dict(snapshot)),
        }
        view_snapshot = copy.deepcopy(dict(snapshot))

        def _mutate(view: Any) -> None:
            if not isinstance(view, dict):
                raise RuntimeError("[state] view is not a dict")
            subjects = view.setdefault("subjects", {})
            if not isinstance(subjects, dict):
                subjects = {}
                view["subjects"] = subjects
            subjects[subject] = view_snapshot

        entry = self._journal.record_and_save(
            character,
            EVT_COMPANION_STATE_COMMITTED,
            payload,
            load_view=self._store.load,
            mutate_view=_mutate,
            save_view=self._store.save,
        )
        return getattr(entry, "event_id", None)

    def _filter_blocked(
        self, character: str, document: dict[str, Any], subject: str,
    ) -> dict[str, Any]:
        if self._barrier is None:
            return document
        row = document.get("subjects", {}).get(subject)
        if not isinstance(row, Mapping):
            return document
        state = row.get("state")
        if not isinstance(state, Mapping):
            return document
        changed = False
        new_state: dict[str, list[Any]] = {}
        for kind in STATE_KINDS:
            kept: list[Any] = []
            for item in state.get(kind) or ():
                if isinstance(item, Mapping) and self._barrier.blocks_row(
                    character, item,
                ):
                    changed = True
                    continue
                kept.append(item)
            new_state[kind] = kept
        if not changed:
            return document
        filtered = copy.deepcopy(document)
        filtered["subjects"][subject]["state"] = new_state
        return filtered

    def _filter_blocked_row(
        self, character: str, row: dict[str, Any],
    ) -> dict[str, Any]:
        if self._barrier is None:
            return row
        state = row.get("state")
        if not isinstance(state, Mapping):
            return row
        new_state: dict[str, list[Any]] = {}
        changed = False
        for kind in STATE_KINDS:
            kept: list[Any] = []
            for item in state.get(kind) or ():
                if isinstance(item, Mapping) and self._barrier.blocks_row(
                    character, item,
                ):
                    changed = True
                    continue
                kept.append(item)
            new_state[kind] = kept
        if not changed:
            return row
        filtered = copy.deepcopy(row)
        filtered["state"] = new_state
        return filtered

    def _drop_blocked_items(
        self, character: str, state: dict[str, Any],
    ) -> dict[str, Any]:
        if self._barrier is None:
            return state
        out: dict[str, list[Any]] = {}
        for kind in STATE_KINDS:
            out[kind] = [
                item for item in state.get(kind) or ()
                if not (
                    isinstance(item, Mapping)
                    and self._barrier.blocks_row(character, item)
                )
            ]
        return out


# -- helpers -----------------------------------------------------------------


def _parse_aware(value: Any, now: datetime) -> datetime | None:
    from ..persistence.state import _parse_iso  # local import: single source
    return _parse_iso(value, now=now)


def _lease_valid(
    lease: Any,
    lease_id: str | None,
    instance: str | None,
    now: datetime,
) -> bool:
    if not isinstance(lease, Mapping) or not lease_id or not instance:
        return False
    if lease.get("lease_id") != lease_id or lease.get("instance") != instance:
        return False
    expires_at = _parse_aware(lease.get("expires_at"), now)
    return expires_at is not None and expires_at > now


def _normalize_ttl(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return LEASE_DEFAULT_TTL_SECONDS
    return max(1, min(int(value), LEASE_MAX_TTL_SECONDS))


def _require_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise StateRequestError(message=f"{field} is required")
    return text


def _read_params(
    request: StateReadRequest | Mapping[str, Any] | None,
    subject: str | None,
    scene: Mapping[str, str] | None,
    since_version: int | None,
    language: str | None,
) -> dict[str, Any]:
    if isinstance(request, StateReadRequest):
        return {
            "subject": subject if subject is not None else request.subject,
            "scene": scene if scene is not None else request.scene,
            "since_version": (
                since_version if since_version is not None
                else request.since_version
            ),
            "language": language if language is not None else request.language,
        }
    if isinstance(request, Mapping):
        return {
            "subject": subject if subject is not None else request.get("subject"),
            "scene": scene if scene is not None else request.get("scene"),
            "since_version": (
                since_version if since_version is not None
                else request.get("since_version")
            ),
            "language": language if language is not None else request.get("language"),
        }
    if request is not None:
        raise TypeError("request must be StateReadRequest, mapping or None")
    return {
        "subject": subject,
        "scene": scene,
        "since_version": since_version,
        "language": language,
    }


def _commit_mapping(
    request: StateCommitRequest | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(request, StateCommitRequest):
        data: dict[str, Any] = {
            "subject": request.subject,
            "ops": list(request.ops),
            "scene": request.scene,
            "producer": request.producer,
            "instance": request.instance,
            "lease_id": request.lease_id,
            "request_id": request.request_id,
            "base_version": request.base_version,
            "processed_event_ids": list(request.processed_event_ids),
            "internal": request.internal,
            "language": request.language,
        }
    elif isinstance(request, Mapping):
        data = dict(request)
    elif request is None:
        data = {}
    else:
        raise TypeError("request must be StateCommitRequest, mapping or None")
    for key, value in overrides.items():
        if key not in {
            "subject", "ops", "scene", "producer", "instance", "lease_id",
            "request_id", "base_version", "processed_event_ids", "internal",
            "language",
        }:
            raise TypeError(f"unknown commit field {key!r}")
        data[key] = value
    return data


__all__ = [
    "StateCommitRequest",
    "StateReadRequest",
    "StateReceipt",
    "StateService",
    "StateSnapshot",
]
