"""B12 evolution proposal storage (per-character evolution_proposals.json).

The store keeps model-proposed persona revisions only; it never touches the
persona card. On-disk contract:

    {"schema_version": 1, "character": "demo", "version": 3,
     "proposals": [<proposal>, ...]}

A proposal row (unknown fields round-trip verbatim):

    proposal_id / status / created_at / updated_at / base_revision /
    operations[] / evidence{source_refs,growth_ids,reflection_ids,fact_ids} /
    model{tier,purpose,tokens,elapsed_ms} /
    validation{checks[],ok,reason} / reason / request_id

status is one of proposed|accepted|rejected|withdrawn|expired. The list is
bounded (default 64); append/prune evict the oldest terminal rows first and
refuse to grow past the bound while every row is still active.

Strict reads: invalid JSON, a non-object root, an unsupported schema, a
non-list proposals value or a row without proposal_id/status raises
CorruptStoreError / InvalidSchemaError and the bytes are left as found. A save
refuses to replace a file its own reader rejects (B02 fail-closed rule).

Writes follow WritePermit.assert_writable -> deletion-barrier filter ->
AtomicJsonStore.save. The barrier filter never resurrects a terminal proposal:
a proposed row whose every evidence reference is tombstoned becomes
withdrawn/source_deleted; terminal rows are left exactly as they are.

All methods are synchronous blocking I/O; call them through asyncio.to_thread
from async code.
"""

from __future__ import annotations

import copy
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError, InvalidSchemaError, StoreError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .shards import is_blank_file

EVOLUTION_FILENAME = "evolution_proposals.json"
EVOLUTION_SCHEMA_VERSION = 1
DEFAULT_PROPOSAL_LIMIT = 64

STATUS_PROPOSED = "proposed"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_WITHDRAWN = "withdrawn"
STATUS_EXPIRED = "expired"

STATUSES = frozenset({
    STATUS_PROPOSED, STATUS_ACCEPTED, STATUS_REJECTED,
    STATUS_WITHDRAWN, STATUS_EXPIRED,
})
TERMINAL_STATUSES = frozenset({
    STATUS_ACCEPTED, STATUS_REJECTED, STATUS_WITHDRAWN, STATUS_EXPIRED,
})

#: Allowed forward transitions; terminal rows never return to proposed.
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PROPOSED: frozenset({
        STATUS_ACCEPTED, STATUS_REJECTED, STATUS_WITHDRAWN, STATUS_EXPIRED,
    }),
    STATUS_ACCEPTED: frozenset({STATUS_ACCEPTED}),
    STATUS_REJECTED: frozenset({STATUS_REJECTED}),
    STATUS_WITHDRAWN: frozenset({STATUS_WITHDRAWN}),
    STATUS_EXPIRED: frozenset({STATUS_EXPIRED}),
}

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


def empty_doc(character: str = "") -> dict[str, Any]:
    return {
        "schema_version": EVOLUTION_SCHEMA_VERSION,
        "character": str(character or ""),
        "version": 0,
        "proposals": [],
    }


def _text(value: Any, limit: int = 0) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value[:limit] if limit > 0 else value


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in out:
            out.append(item)
    return out


def _normalize_proposal(raw: Any, *, index: int = 0) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"evolution proposal[{index}]: expected an object, "
            f"found {type(raw).__name__}",
        )
    proposal = copy.deepcopy(dict(raw))
    proposal_id = _text(proposal.get("proposal_id"), 200)
    if not proposal_id:
        raise CorruptStoreError(
            f"evolution proposal[{index}]: missing proposal_id",
        )
    status = _text(proposal.get("status"))
    if status not in STATUSES:
        raise CorruptStoreError(
            f"evolution proposal {proposal_id!r}: invalid status {status!r}",
        )
    proposal["proposal_id"] = proposal_id
    proposal["status"] = status
    proposal["created_at"] = _text(proposal.get("created_at"))
    proposal["updated_at"] = _text(proposal.get("updated_at")) or proposal["created_at"]
    proposal["base_revision"] = _int(proposal.get("base_revision"), 0)
    operations = proposal.get("operations")
    if operations is None:
        operations = []
    if not isinstance(operations, list):
        raise CorruptStoreError(
            f"evolution proposal {proposal_id!r}: operations is not a list",
        )
    proposal["operations"] = [copy.deepcopy(item) for item in operations]
    evidence = proposal.get("evidence")
    if evidence is None:
        evidence = {}
    if not isinstance(evidence, Mapping):
        raise CorruptStoreError(
            f"evolution proposal {proposal_id!r}: evidence is not an object",
        )
    normalized_evidence: dict[str, list[str]] = {}
    for key in ("source_refs", "growth_ids", "reflection_ids", "fact_ids"):
        normalized_evidence[key] = _str_list(evidence.get(key))
    proposal["evidence"] = normalized_evidence
    model = proposal.get("model")
    proposal["model"] = copy.deepcopy(dict(model)) if isinstance(model, Mapping) else {}
    validation = proposal.get("validation")
    proposal["validation"] = (
        copy.deepcopy(dict(validation)) if isinstance(validation, Mapping) else {}
    )
    proposal["reason"] = _text(proposal.get("reason"), 400)
    proposal["request_id"] = _text(proposal.get("request_id"), 200)
    return proposal


def _normalize_doc(raw: Any, character: str) -> dict[str, Any]:
    if raw is None:
        return empty_doc(character)
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"evolution_proposals: expected a JSON object, "
            f"found {type(raw).__name__}",
        )
    schema = raw.get("schema_version", EVOLUTION_SCHEMA_VERSION)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise InvalidSchemaError(
            f"evolution_proposals: missing or non-integer schema_version: {schema!r}",
        )
    if schema != EVOLUTION_SCHEMA_VERSION:
        raise InvalidSchemaError(
            f"unsupported evolution_proposals schema {schema}; this build "
            f"supports {EVOLUTION_SCHEMA_VERSION}",
        )
    doc = copy.deepcopy(dict(raw))
    stored_character = _text(doc.get("character"), 200)
    if stored_character and stored_character != character:
        raise CorruptStoreError(
            f"evolution_proposals: character {stored_character!r} does not "
            f"match {character!r}",
        )
    doc["schema_version"] = EVOLUTION_SCHEMA_VERSION
    doc["character"] = character
    doc["version"] = _int(doc.get("version"), 0)
    proposals = doc.get("proposals")
    if proposals is None:
        proposals = []
    if not isinstance(proposals, list):
        raise CorruptStoreError(
            "evolution_proposals.proposals: expected a list, "
            f"found {type(proposals).__name__}",
        )
    normalized = [_normalize_proposal(item, index=index)
                  for index, item in enumerate(proposals)]
    seen: set[str] = set()
    for proposal in normalized:
        if proposal["proposal_id"] in seen:
            raise CorruptStoreError(
                "evolution_proposals: duplicate proposal_id "
                f"{proposal['proposal_id']!r}",
            )
        seen.add(proposal["proposal_id"])
    doc["proposals"] = normalized
    return doc


def _evidence_refs(proposal: Mapping[str, Any]) -> list[str]:
    evidence = proposal.get("evidence")
    if not isinstance(evidence, Mapping):
        return []
    refs: list[str] = []
    for key in ("source_refs", "growth_ids", "reflection_ids", "fact_ids"):
        for item in evidence.get(key) or []:
            if isinstance(item, str) and item and item not in refs:
                refs.append(item)
    return refs


def _blocked(barrier: Any, character: str, ref: str) -> bool:
    if barrier is None:
        return False
    for target_type in ("fact", "reflection", "growth", "persona"):
        try:
            if barrier.blocks_target(character, target_type, ref):
                return True
        except Exception:  # noqa: BLE001 - a broken barrier must not invent data
            continue
    return False


def _all_refs_blocked(barrier: Any, character: str, refs: Sequence[str]) -> bool:
    if not refs:
        return False
    return all(_blocked(barrier, character, ref) for ref in refs)


class EvolutionStore:
    """Bounded per-character proposal store with barrier filtering."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        barrier: Any = None,
        limit: int = DEFAULT_PROPOSAL_LIMIT,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._barrier = barrier
        self._limit = max(1, int(limit or DEFAULT_PROPOSAL_LIMIT))

    # -- paths and reads -------------------------------------------------

    def _path(self, character: str, *, create: bool = False) -> Path:
        return self._roots.character_dir(character, create=create) / EVOLUTION_FILENAME

    @property
    def limit(self) -> int:
        return self._limit

    def _load_doc(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        try:
            raw = self._store.load(path)
        except CorruptStoreError:
            if is_blank_file(path):
                return empty_doc(character)
            raise
        return _normalize_doc(raw, character)

    def _filter(self, character: str, doc: Mapping[str, Any]) -> dict[str, Any]:
        filtered = copy.deepcopy(dict(doc))
        if self._barrier is None:
            return filtered
        now = self._clock.now().isoformat()
        for proposal in filtered.get("proposals") or []:
            if proposal.get("status") in TERMINAL_STATUSES:
                continue
            refs = _evidence_refs(proposal)
            if _all_refs_blocked(self._barrier, character, refs):
                proposal["status"] = STATUS_WITHDRAWN
                proposal["reason"] = "source_deleted"
                proposal["updated_at"] = now
        return filtered

    def load(self, character: str) -> dict[str, Any]:
        """Return the filtered document; corruption raises, never empty."""
        with _lock_for(self._path(character)):
            return self._filter(character, self._load_doc(character))

    def list(
        self,
        character: str,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        proposals = list(self.load(character).get("proposals") or [])
        if status is not None:
            proposals = [p for p in proposals if p.get("status") == status]
        proposals.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
        if limit is not None and limit >= 0:
            proposals = proposals[:limit]
        return proposals

    def get(self, character: str, proposal_id: str) -> dict[str, Any] | None:
        wanted = _text(proposal_id, 200)
        if not wanted:
            return None
        for proposal in self.load(character).get("proposals") or []:
            if proposal.get("proposal_id") == wanted:
                return proposal
        return None

    def find_by_request_id(
        self, character: str, request_id: str,
    ) -> dict[str, Any] | None:
        wanted = _text(request_id, 200)
        if not wanted:
            return None
        for proposal in self.load(character).get("proposals") or []:
            if proposal.get("request_id") == wanted:
                return proposal
        return None

    # -- writes ----------------------------------------------------------

    def _write_doc(self, character: str, doc: Mapping[str, Any]) -> dict[str, Any]:
        payload = _normalize_doc(doc, character)
        payload = self._filter(character, payload)
        path = self._path(character)
        with _lock_for(path):
            self._load_doc(character)  # refuse to replace a corrupt file
            self._permit.assert_writable("save_evolution_proposals", str(path))
            self._store.save(path, payload)
        return payload

    def save(self, character: str, doc: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically replace the document after permit + barrier filtering."""
        payload = _normalize_doc(doc, character)
        payload["version"] = _int(payload.get("version"), 0) + 1
        payload = self._prune_doc(payload)
        self._enforce_limit(payload)
        return self._write_doc(character, payload)

    def append(
        self, character: str, proposal: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append one proposal; a duplicate request_id returns the existing row."""
        if isinstance(proposal, Mapping) and not proposal.get("status"):
            proposal = {**dict(proposal), "status": STATUS_PROPOSED}
        row = _normalize_proposal(proposal)
        if not row["created_at"]:
            row["created_at"] = self._clock.now().isoformat()
        if not row["updated_at"]:
            row["updated_at"] = row["created_at"]
        with _lock_for(self._path(character)):
            doc = self._load_doc(character)
            if row["request_id"]:
                for existing in doc.get("proposals") or []:
                    if existing.get("request_id") == row["request_id"]:
                        return existing
            doc["proposals"].append(row)
            doc["version"] = _int(doc.get("version"), 0) + 1
            doc = self._prune_doc(doc)
            if len(doc.get("proposals") or []) > self._limit:
                raise StoreError(
                    "evolution proposal limit reached; no terminal proposal "
                    "can be evicted",
                    code="proposal_limit",
                )
            self._write_doc(character, doc)
            return self.get(character, row["proposal_id"]) or row

    def update(
        self, character: str, proposal_id: Any,
        fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if isinstance(proposal_id, Mapping):
            fields = proposal_id
            proposal_id = fields.get("proposal_id")
        wanted = _text(proposal_id, 200)
        if not wanted or not isinstance(fields, Mapping):
            return None
        with _lock_for(self._path(character)):
            doc = self._load_doc(character)
            target: dict[str, Any] | None = None
            for proposal in doc.get("proposals") or []:
                if proposal.get("proposal_id") == wanted:
                    target = proposal
                    break
            if target is None:
                return None
            new_status = fields.get("status")
            if new_status is not None:
                new_status = _text(new_status)
                if new_status not in STATUSES:
                    raise ValueError(f"invalid evolution status {new_status!r}")
                allowed = _TRANSITIONS.get(str(target.get("status")), frozenset())
                if new_status not in allowed:
                    raise ValueError(
                        f"illegal evolution transition {target.get('status')!r} "
                        f"-> {new_status!r}",
                    )
            merged = copy.deepcopy(target)
            for key, value in fields.items():
                merged[key] = copy.deepcopy(value)
            merged["proposal_id"] = wanted
            merged["updated_at"] = self._clock.now().isoformat()
            normalized = _normalize_proposal(merged)
            proposals = [
                normalized if p.get("proposal_id") == wanted else p
                for p in doc.get("proposals") or []
            ]
            doc["proposals"] = proposals
            doc["version"] = _int(doc.get("version"), 0) + 1
            self._write_doc(character, doc)
            return self.get(character, wanted) or normalized

    def upsert(self, character: str, proposal: Mapping[str, Any]) -> dict[str, Any]:
        """Replay one full proposal snapshot (idempotent; never rolls back)."""
        row = _normalize_proposal(proposal)
        with _lock_for(self._path(character)):
            doc = self._load_doc(character)
            proposals = list(doc.get("proposals") or [])
            for index, existing in enumerate(proposals):
                if existing.get("proposal_id") != row["proposal_id"]:
                    continue
                if str(existing.get("updated_at") or "") > str(row.get("updated_at") or ""):
                    return existing
                if (existing.get("status") in TERMINAL_STATUSES
                        and row.get("status") == STATUS_PROPOSED):
                    return existing
                proposals[index] = row
                break
            else:
                proposals.append(row)
            doc["proposals"] = proposals
            doc["version"] = _int(doc.get("version"), 0) + 1
            doc = self._prune_doc(doc)
            self._enforce_limit(doc)
            self._write_doc(character, doc)
            return self.get(character, row["proposal_id"]) or row

    def expire_proposed(self, character: str, cutoff_iso: str) -> int:
        """Mark proposed rows older than cutoff as expired; return count."""
        cutoff = _text(cutoff_iso)
        if not cutoff:
            return 0
        with _lock_for(self._path(character)):
            doc = self._load_doc(character)
            changed = 0
            now = self._clock.now().isoformat()
            for proposal in doc.get("proposals") or []:
                if proposal.get("status") != STATUS_PROPOSED:
                    continue
                if str(proposal.get("created_at") or "") >= cutoff:
                    continue
                proposal["status"] = STATUS_EXPIRED
                proposal["reason"] = "proposal_expired"
                proposal["updated_at"] = now
                changed += 1
            if changed:
                doc["version"] = _int(doc.get("version"), 0) + 1
                self._write_doc(character, doc)
            return changed

    def prune(self, character: str, *, limit: int | None = None) -> int:
        """Evict oldest terminal rows until the bound holds; return count."""
        target_limit = self._limit if limit is None else max(1, int(limit))
        with _lock_for(self._path(character)):
            doc = self._load_doc(character)
            before = len(doc.get("proposals") or [])
            doc = self._prune_doc(doc, limit=target_limit)
            removed = before - len(doc.get("proposals") or [])
            if removed:
                doc["version"] = _int(doc.get("version"), 0) + 1
                self._write_doc(character, doc)
            else:
                self._enforce_limit(doc, limit=target_limit)
            return removed

    def _enforce_limit(
        self, doc: Mapping[str, Any], *, limit: int | None = None,
    ) -> None:
        bound = self._limit if limit is None else max(1, int(limit))
        if len(doc.get("proposals") or []) > bound:
            raise StoreError(
                "evolution proposal limit reached; no terminal proposal "
                "can be evicted",
                code="proposal_limit",
            )

    def _prune_doc(
        self, doc: dict[str, Any], *, limit: int | None = None,
    ) -> dict[str, Any]:
        bound = self._limit if limit is None else max(1, int(limit))
        proposals = list(doc.get("proposals") or [])
        if len(proposals) <= bound:
            return doc
        terminal = [
            p for p in proposals if p.get("status") in TERMINAL_STATUSES
        ]
        terminal.sort(key=lambda p: str(p.get("updated_at") or p.get("created_at") or ""))
        evict_ids = {
            p.get("proposal_id") for p in terminal[: max(0, len(proposals) - bound)]
        }
        doc["proposals"] = [
            p for p in proposals if p.get("proposal_id") not in evict_ids
        ]
        return doc
