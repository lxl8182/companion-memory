"""B13 persona administration: review, withdraw, diff, rollback, switch.

This service sits above the card (B10), the evolution proposal store (B12) and
the publish store (B11). It never writes the card itself: accepting a proposal
or rolling back a revision calls PersonaCardService, so base_revision CAS,
frozen/editable_by checks, source validation, history and deletion filtering
all stay in one place.

Responsibilities:

* list proposals (delegated to EvolutionService, status filtered, deleted
  sources hidden);
* accept/reject/withdraw a proposal with a durable decision record;
* current card revision history and section-level diff;
* rollback to an earlier revision by appending a new revision (history is
  never physically deleted);
* the per-character auto-evolution switch (default OFF, persisted separately
  from the card; turning it off does not reset the card);
* deletion/correction fan-out: proposals are withdrawn and decisions/history
  views are filtered, so deleted sources cannot be resurrected;
* idempotent replay of core.persona_admin_decision events.

EvolutionService is optional: without it proposal operations return
unavailable / raise NotConfiguredError instead of pretending success.
"""

from __future__ import annotations

import copy
import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from ..errors import NotConfiguredError
from ..persistence.clock import SystemClock
from ..persistence.evolution import (
    STATUS_ACCEPTED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
)
from ..persistence.persona_admin import (
    AUTO_MODES,
    DEFAULT_AUTO_MODE,
    PersonaAdminStore,
)
from ..persistence.permits import NullWritePermit
from ..ports import Clock, StorageRoots, WritePermit
from .persona_card import (
    CardSnapshot,
    PersonaCardService,
    RevisionRequest,
)

#: Persisted journal event name for one admin decision snapshot.
EVT_PERSONA_ADMIN_DECISION = "core.persona_admin_decision"

DEFAULT_PROPOSAL_TTL_HOURS = 72.0
MAX_DECISIONS_IN_SNAPSHOT = 20
MAX_SOURCE_REFS = 128

REASON_EVOLUTION_UNAVAILABLE = "evolution_unavailable"
REASON_UNKNOWN_PROPOSAL = "unknown_proposal"
REASON_PROPOSAL_NOT_PROPOSED = "proposal_not_proposed"
REASON_PROPOSAL_EXPIRED = "proposal_expired"
REASON_STALE_REVISION = "stale_revision"
REASON_REVISION_FAILED = "revision_failed"
REASON_PROPOSAL_MARK_FAILED = "proposal_mark_failed"
REASON_NO_CHANGE = "no_change"
REASON_UNKNOWN_REVISION = "unknown_revision"
REASON_FUTURE_REVISION = "future_revision"
REASON_SOURCE_DELETED = "source_deleted"
REASON_ALREADY_WITHDRAWN = "already_withdrawn"


@dataclass(frozen=True)
class AdminSettings:
    """Persisted auto-evolution mode (off/propose/auto) plus legacy enabled."""

    enabled: bool = False
    mode: str = DEFAULT_AUTO_MODE
    updated_at: str | None = None
    actor: str = "system"

    @classmethod
    def from_mapping(cls, raw: Any) -> "AdminSettings":
        if not isinstance(raw, Mapping):
            return cls()
        raw_mode = raw.get("mode")
        if raw_mode is None or raw_mode == "":
            mode = "propose" if bool(raw.get("enabled", False)) else DEFAULT_AUTO_MODE
        else:
            mode = str(raw_mode).strip()
            if mode not in AUTO_MODES:
                raise ValueError(f"unknown evolution mode {mode!r}")
        return cls(
            enabled=mode != "off",
            mode=mode,
            updated_at=raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
            actor=str(raw.get("actor") or "system"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "updated_at": self.updated_at,
            "actor": self.actor,
        }


@dataclass(frozen=True)
class DecisionRequest:
    """One review action on a proposal."""

    proposal_id: str = ""
    action: str = "accept"
    actor: str = "user"
    reason: str = ""
    expected_revision: int | None = None


@dataclass(frozen=True)
class DecisionReceipt:
    """Result of accept/reject/withdraw/rollback; failures keep the proposal."""

    status: str
    decision_id: str = ""
    proposal_id: str = ""
    action: str = ""
    revision_before: int = 0
    revision_after: int = 0
    reason: str | None = None
    decision: Mapping[str, Any] | None = None
    card: CardSnapshot | None = None
    conflict: bool = False
    duplicate: bool = False


@dataclass(frozen=True)
class RevisionDiff:
    """Section-level difference between two card revisions."""

    from_revision: int
    to_revision: int
    added: tuple[Mapping[str, Any], ...] = ()
    removed: tuple[Mapping[str, Any], ...] = ()
    changed: tuple[Mapping[str, Any], ...] = ()
    available: bool = True
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_revision": self.from_revision,
            "to_revision": self.to_revision,
            "added": [dict(item) for item in self.added],
            "removed": [dict(item) for item in self.removed],
            "changed": [dict(item) for item in self.changed],
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AdminSnapshot:
    """Management-panel view: switch, current card, history, decisions."""

    character: str
    auto_evolution: AdminSettings = field(default_factory=AdminSettings)
    card: CardSnapshot | None = None
    revisions: tuple[Mapping[str, Any], ...] = ()
    decisions: tuple[Mapping[str, Any], ...] = ()
    proposals: tuple[Mapping[str, Any], ...] = ()
    pending_proposals: int = 0


class PersonaAdminService:
    """Review/withdraw/diff/rollback and auto-evolution switch for one card."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: PersonaAdminStore,
        card_service: PersonaCardService,
        evolution_service: Any = None,
        publish_service: Any = None,
        journal: Any = None,
        clock: Clock | None = None,
        permit: WritePermit | None = None,
        barrier: Any = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._card_service = card_service
        self._evolution = evolution_service
        self._publish = publish_service
        self._journal = journal
        self._clock = clock or getattr(store, "_clock", None) or SystemClock()
        self._permit = permit or getattr(store, "_permit", None) or NullWritePermit()
        self._barrier = barrier
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._decision_seq = 0
        if journal is not None:
            try:
                journal.register_handler(
                    EVT_PERSONA_ADMIN_DECISION, self._replay_decision,
                )
            except Exception:  # noqa: BLE001 - an optional journal may be read-only
                pass
            self._ensure_event_vocabulary()

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

    def _now_iso(self) -> str:
        return self._clock.now().isoformat()

    def _ensure_event_vocabulary(self) -> None:
        types = getattr(self._journal, "_event_types", None)
        if types is not None and EVT_PERSONA_ADMIN_DECISION not in types:
            try:
                self._journal._event_types = (
                    frozenset(types) | {EVT_PERSONA_ADMIN_DECISION}
                )
            except Exception:  # noqa: BLE001 - journal stays read-only for us
                pass

    # -- proposals ------------------------------------------------------

    def list_proposals(
        self, character: str, status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Proposals from EvolutionService, status filtered, deleted hidden."""
        character = self._character(character)
        if self._evolution is None:
            raise NotConfiguredError(
                "evolution_service is not configured; proposal review is unavailable",
            )
        rows = self._proposal_list(character, status)
        out: list[dict[str, Any]] = []
        for row in rows:
            if row.get("invalidated_at"):
                continue
            if self._proposal_blocked(character, row):
                continue
            out.append(row)
        return out

    def get_proposal(self, character: str, proposal_id: str) -> dict[str, Any] | None:
        character = self._character(character)
        if self._evolution is None:
            raise NotConfiguredError(
                "evolution_service is not configured; proposal review is unavailable",
            )
        proposal = self._proposal_get(character, proposal_id)
        if proposal is None or self._proposal_blocked(character, proposal):
            return None
        return proposal

    def accept_proposal(
        self,
        character: str,
        proposal_id: str,
        *,
        actor: str = "user",
        reason: str = "",
        expected_revision: int | None = None,
    ) -> DecisionReceipt:
        """Accept a proposed change by revising the card through B10."""
        character = self._character(character)
        if self._evolution is None:
            return DecisionReceipt(
                status="unavailable", reason=REASON_EVOLUTION_UNAVAILABLE,
            )
        proposal = self._proposal_get(character, proposal_id)
        if proposal is None:
            return DecisionReceipt(
                status="rejected", proposal_id=str(proposal_id or ""),
                reason=REASON_UNKNOWN_PROPOSAL,
            )
        if self._proposal_blocked(character, proposal):
            return DecisionReceipt(
                status="rejected", proposal_id=str(proposal_id),
                reason=REASON_SOURCE_DELETED,
            )
        if proposal.get("status") != STATUS_PROPOSED:
            return DecisionReceipt(
                status="rejected", proposal_id=str(proposal_id),
                reason=REASON_PROPOSAL_NOT_PROPOSED,
                decision={"current_status": proposal.get("status")},
            )
        if self._proposal_expired(proposal):
            return DecisionReceipt(
                status="rejected", proposal_id=str(proposal_id),
                reason=REASON_PROPOSAL_EXPIRED,
            )
        with self._lock(character):
            card = self._card_service.get(character)
            current = int(card.revision)
            if expected_revision is not None:
                try:
                    expected = int(expected_revision)
                except (TypeError, ValueError):
                    return DecisionReceipt(
                        status="rejected", proposal_id=str(proposal_id),
                        action="accepted", reason="invalid_request",
                    )
                if expected != current:
                    return DecisionReceipt(
                        status="conflict", conflict=True,
                        proposal_id=str(proposal_id), action="accepted",
                        revision_before=current, revision_after=current,
                        reason=REASON_STALE_REVISION, card=card,
                    )
            base = int(proposal.get("base_revision") or 0)
            operations = [
                copy.deepcopy(item) for item in proposal.get("operations") or []
                if isinstance(item, Mapping)
            ]
            refs = self._proposal_refs(proposal)
            revise = self._card_service.revise(character, RevisionRequest(
                base_revision=base,
                operations=operations,
                reason=reason or str(proposal.get("reason") or ""),
                origin="evolution",
                actor=str(actor or "user"),
                source_refs=refs,
            ))
            if revise.status != "completed":
                return DecisionReceipt(
                    status="conflict" if revise.conflict else "rejected",
                    proposal_id=str(proposal_id), action="accepted",
                    revision_before=current,
                    revision_after=int(revise.revision or current),
                    reason=revise.reason or REASON_REVISION_FAILED,
                    card=revise.card, conflict=bool(revise.conflict),
                    duplicate=bool(revise.duplicate),
                )
            at = self._now_iso()
            decision = self._make_decision(
                character,
                proposal_id=str(proposal_id),
                action="accepted",
                status="accepted",
                actor=str(actor or "user"),
                reason=reason,
                at=at,
                revision_before=current,
                revision_after=int(revise.revision),
                source_refs=refs,
                proposal=proposal,
            )
            stored = self._write_decision(character, decision)
            marked = self._proposal_update(character, str(proposal_id), {
                "status": STATUS_ACCEPTED,
                "reason": (reason or "accepted")[:400],
                "actor": str(actor or "user"),
                "accepted_at": at,
                "accepted_revision": int(revise.revision),
            })
            mark_reason = None if marked is not None else REASON_PROPOSAL_MARK_FAILED
            return DecisionReceipt(
                status="accepted", decision_id=str(stored.get("decision_id") or ""),
                proposal_id=str(proposal_id), action="accepted",
                revision_before=current, revision_after=int(revise.revision),
                reason=mark_reason, decision=stored, card=revise.card,
            )

    def reject_proposal(
        self,
        character: str,
        proposal_id: str,
        *,
        actor: str = "user",
        reason: str = "",
    ) -> DecisionReceipt:
        """Reject a proposed change; the card is untouched."""
        character = self._character(character)
        if self._evolution is None:
            return DecisionReceipt(
                status="unavailable", reason=REASON_EVOLUTION_UNAVAILABLE,
            )
        proposal = self._proposal_get(character, proposal_id)
        if proposal is None:
            return DecisionReceipt(
                status="rejected", proposal_id=str(proposal_id or ""),
                reason=REASON_UNKNOWN_PROPOSAL,
            )
        if proposal.get("status") != STATUS_PROPOSED:
            return DecisionReceipt(
                status="rejected", proposal_id=str(proposal_id),
                reason=REASON_PROPOSAL_NOT_PROPOSED,
                decision={"current_status": proposal.get("status")},
            )
        with self._lock(character):
            card = self._card_service.get(character)
            at = self._now_iso()
            decision = self._make_decision(
                character, proposal_id=str(proposal_id),
                action="rejected", status="rejected",
                actor=str(actor or "user"), reason=reason, at=at,
                revision_before=int(card.revision),
                revision_after=int(card.revision),
                source_refs=self._proposal_refs(proposal),
                proposal=proposal,
            )
            stored = self._write_decision(character, decision)
            marked = self._proposal_update(character, str(proposal_id), {
                "status": STATUS_REJECTED,
                "reason": (reason or "rejected")[:400],
                "actor": str(actor or "user"),
                "rejected_at": at,
            })
            return DecisionReceipt(
                status="rejected",
                decision_id=str(stored.get("decision_id") or ""),
                proposal_id=str(proposal_id), action="rejected",
                revision_before=int(card.revision),
                revision_after=int(card.revision),
                reason=None if marked is not None else REASON_PROPOSAL_MARK_FAILED,
                decision=stored, card=card,
            )

    def withdraw_proposal(
        self,
        character: str,
        proposal_id: str,
        *,
        actor: str = "user",
        reason: str = "",
    ) -> DecisionReceipt:
        """Withdraw a proposal without changing the card."""
        character = self._character(character)
        if self._evolution is None:
            return DecisionReceipt(
                status="unavailable", reason=REASON_EVOLUTION_UNAVAILABLE,
            )
        proposal = self._proposal_get(character, proposal_id)
        if proposal is None:
            return DecisionReceipt(
                status="rejected", proposal_id=str(proposal_id or ""),
                reason=REASON_UNKNOWN_PROPOSAL,
            )
        current_status = proposal.get("status")
        if current_status == STATUS_WITHDRAWN:
            return DecisionReceipt(
                status="duplicate", duplicate=True,
                proposal_id=str(proposal_id), action="withdrawn",
                reason=REASON_ALREADY_WITHDRAWN,
            )
        if current_status != STATUS_PROPOSED:
            return DecisionReceipt(
                status="rejected", proposal_id=str(proposal_id),
                reason=REASON_PROPOSAL_NOT_PROPOSED,
                decision={"current_status": current_status},
            )
        with self._lock(character):
            card = self._card_service.get(character)
            at = self._now_iso()
            decision = self._make_decision(
                character, proposal_id=str(proposal_id),
                action="withdrawn", status="withdrawn",
                actor=str(actor or "user"), reason=reason, at=at,
                revision_before=int(card.revision),
                revision_after=int(card.revision),
                source_refs=self._proposal_refs(proposal),
                proposal=proposal,
            )
            stored = self._write_decision(character, decision)
            marked = self._proposal_update(character, str(proposal_id), {
                "status": STATUS_WITHDRAWN,
                "reason": (reason or "user_withdraw")[:400],
                "actor": str(actor or "user"),
                "withdrawn_at": at,
            })
            return DecisionReceipt(
                status="withdrawn",
                decision_id=str(stored.get("decision_id") or ""),
                proposal_id=str(proposal_id), action="withdrawn",
                revision_before=int(card.revision),
                revision_after=int(card.revision),
                reason=None if marked is not None else REASON_PROPOSAL_MARK_FAILED,
                decision=stored, card=card,
            )

    # -- revisions / diff / rollback ------------------------------------

    def revisions(self, character: str) -> list[dict[str, Any]]:
        """Bounded card revision summaries (newest first)."""
        character = self._character(character)
        card = self._card_service.get(character)
        summaries: list[dict[str, Any]] = []
        for revision in card.revisions:
            summaries.append({
                "revision": revision.revision,
                "base_revision": revision.base_revision,
                "reason": revision.reason,
                "origin": revision.origin,
                "actor": revision.actor,
                "at": revision.at,
                "result": revision.result,
                "operations_count": len(revision.operations),
                "source_refs": list(revision.source_refs),
            })
        summaries.sort(key=lambda row: int(row.get("revision") or 0), reverse=True)
        return summaries

    def revision_sections(self, character: str, revision: int) -> list[dict[str, Any]] | None:
        """Read the surviving section snapshot of one historical revision."""
        character = self._character(character)
        raw = self._raw_card(character)
        return None if raw is None else self._sections_at(character, raw, int(revision))

    def diff(
        self, character: str, from_revision: int, to_revision: int,
    ) -> RevisionDiff:
        """Section-level diff between two stored card revisions."""
        character = self._character(character)
        try:
            start = int(from_revision)
            end = int(to_revision)
        except (TypeError, ValueError):
            return RevisionDiff(
                from_revision=0, to_revision=0, available=False,
                reason=REASON_UNKNOWN_REVISION,
            )
        raw = self._raw_card(character)
        if raw is None:
            return RevisionDiff(
                from_revision=start, to_revision=end, available=False,
                reason=REASON_UNKNOWN_REVISION,
            )
        before = self._sections_at(character, raw, start)
        after = self._sections_at(character, raw, end)
        if before is None or after is None:
            return RevisionDiff(
                from_revision=start, to_revision=end, available=False,
                reason=REASON_UNKNOWN_REVISION,
            )
        before_map = {str(row.get("section_id")): row for row in before}
        after_map = {str(row.get("section_id")): row for row in after}
        added = [
            self._section_view(after_map[sid])
            for sid in after_map if sid not in before_map
        ]
        removed = [
            self._section_view(before_map[sid])
            for sid in before_map if sid not in after_map
        ]
        changed: list[dict[str, Any]] = []
        for sid in before_map:
            if sid not in after_map:
                continue
            left = self._section_view(before_map[sid])
            right = self._section_view(after_map[sid])
            if self._section_fingerprint(left) != self._section_fingerprint(right):
                changed.append({
                    "section_id": sid,
                    "before": left,
                    "after": right,
                })
        return RevisionDiff(
            from_revision=start, to_revision=end,
            added=tuple(added), removed=tuple(removed), changed=tuple(changed),
        )

    def rollback_to_revision(
        self, character: str, revision: int, *, actor: str = "user",
    ) -> DecisionReceipt:
        """Restore an earlier revision as a new revision (history preserved)."""
        character = self._character(character)
        try:
            target_revision = int(revision)
        except (TypeError, ValueError):
            return DecisionReceipt(
                status="rejected", action="rolled_back",
                reason=REASON_UNKNOWN_REVISION,
            )
        with self._lock(character):
            card = self._card_service.get(character)
            current = int(card.revision)
            if target_revision == current:
                return DecisionReceipt(
                    status="duplicate", duplicate=True,
                    action="rolled_back", revision_before=current,
                    revision_after=current, card=card,
                    reason="already_current",
                )
            if target_revision > current or target_revision < 1:
                return DecisionReceipt(
                    status="rejected", action="rolled_back",
                    revision_before=current, revision_after=current,
                    reason=REASON_FUTURE_REVISION, card=card,
                )
            raw = self._raw_card(character)
            target = (
                self._sections_at(character, raw, target_revision)
                if raw is not None else None
            )
            if target is None:
                return DecisionReceipt(
                    status="rejected", action="rolled_back",
                    revision_before=current, revision_after=current,
                    reason=REASON_UNKNOWN_REVISION, card=card,
                )
            target_map = {str(row.get("section_id")): row for row in target}
            current_map = {section.section_id: section for section in card.sections}
            operations: list[dict[str, Any]] = []
            for sid, row in target_map.items():
                if sid in current_map:
                    operations.append({
                        "op": "replace",
                        "section_id": sid,
                        "text": str(row.get("text") or ""),
                        "kind": str(row.get("kind") or "identity"),
                        "scope": copy.deepcopy(row.get("scope") or {}),
                        "editable_by": copy.deepcopy(row.get("editable_by") or {}),
                        "frozen": bool(row.get("frozen")),
                        "source_refs": list(row.get("source_refs") or []),
                    })
                else:
                    operations.append({
                        "op": "add",
                        "section_id": sid,
                        "text": str(row.get("text") or ""),
                        "kind": str(row.get("kind") or "identity"),
                        "scope": copy.deepcopy(row.get("scope") or {}),
                        "editable_by": copy.deepcopy(row.get("editable_by") or {}),
                        "frozen": bool(row.get("frozen")),
                        "source_refs": list(row.get("source_refs") or []),
                    })
            for sid in current_map:
                if sid not in target_map:
                    operations.append({"op": "delete", "section_id": sid})
            if not operations:
                return DecisionReceipt(
                    status="duplicate", duplicate=True,
                    action="rolled_back", revision_before=current,
                    revision_after=current, card=card, reason=REASON_NO_CHANGE,
                )
            refs = self._dedupe(
                ref for row in target for ref in row.get("source_refs") or []
                if isinstance(ref, str)
            )
            revise = self._card_service.revise(character, RevisionRequest(
                base_revision=current,
                operations=operations,
                reason=f"rollback_to_revision:{target_revision}",
                origin="user_edit",
                actor=str(actor or "user"),
                source_refs=refs,
            ))
            if revise.status != "completed":
                if revise.status == "duplicate":
                    return DecisionReceipt(
                        status="duplicate", duplicate=True,
                        action="rolled_back", revision_before=current,
                        revision_after=current, card=revise.card,
                        reason=REASON_NO_CHANGE,
                    )
                return DecisionReceipt(
                    status="conflict" if revise.conflict else "rejected",
                    action="rolled_back", revision_before=current,
                    revision_after=int(revise.revision or current),
                    reason=revise.reason or REASON_REVISION_FAILED,
                    card=revise.card, conflict=bool(revise.conflict),
                )
            at = self._now_iso()
            decision = self._make_decision(
                character, proposal_id="", action="rolled_back",
                status="rolled_back", actor=str(actor or "user"),
                reason=f"rollback_to_revision:{target_revision}",
                at=at, revision_before=current,
                revision_after=int(revise.revision), source_refs=refs,
                extra={"from_revision": target_revision},
            )
            stored = self._write_decision(character, decision)
            return DecisionReceipt(
                status="rolled_back",
                decision_id=str(stored.get("decision_id") or ""),
                action="rolled_back", revision_before=current,
                revision_after=int(revise.revision),
                decision=stored, card=revise.card,
            )

    # -- auto evolution switch ------------------------------------------

    def auto_evolution(self, character: str) -> AdminSettings:
        character = self._character(character)
        document = self._store.load(character)
        return AdminSettings.from_mapping(document.get("auto_evolution"))

    def get_mode(self, character: str) -> str:
        """Current evolution mode; unknown persisted values fail closed."""
        return self.auto_evolution(self._character(character)).mode

    def set_mode(
        self, character: str, mode: str, *, actor: str = "user",
    ) -> AdminSettings:
        """Set the single durable mode; off/propose/auto are explicit."""
        character = self._character(character)
        mode = str(mode or "").strip()
        if mode not in AUTO_MODES:
            raise ValueError(f"unknown evolution mode {mode!r}")
        with self._lock(character):
            current = self.auto_evolution(character)
            # Always call the store so a legacy bool-only file is persisted with
            # its migrated mode even when the effective mode did not change.
            saved = self._store.set_mode(character, mode, actor=str(actor or "user"))
            if current.mode == mode:
                return AdminSettings.from_mapping(saved)
            at = self._now_iso()
            settings = self.auto_evolution(character)
            action = (
                "auto_evolution_enabled" if mode == "propose"
                else "auto_evolution_disabled" if mode == "off"
                else "auto_evolution_mode_changed"
            )
            decision = self._make_decision(
                character, proposal_id="", action=action,
                status="applied", actor=str(actor or "user"), reason="",
                at=at, revision_before=0, revision_after=0, source_refs=[],
                extra={"mode": mode},
            )
            # Every toggle is its own audit row; do not collapse repeats by the
            # deterministic accept/reject id.
            self._decision_seq += 1
            decision["decision_id"] = "adm_" + hashlib.sha1(
                f"{character}|{action}|{mode}|{at}|{self._decision_seq}".encode("utf-8"),
            ).hexdigest()[:16]
            self._write_decision(character, decision)
            return settings

    def set_auto_evolution(
        self, character: str, enabled: bool, *, actor: str = "user",
    ) -> AdminSettings:
        """Legacy bool compatibility: true means propose, never auto."""
        return self.set_mode(
            character, "propose" if bool(enabled) else "off", actor=actor,
        )

    # -- admin snapshot -------------------------------------------------

    def snapshot(self, character: str) -> AdminSnapshot:
        character = self._character(character)
        card = self._card_service.get(character)
        settings = self.auto_evolution(character)
        decisions = tuple(
            self._store.list_decisions(
                character, limit=MAX_DECISIONS_IN_SNAPSHOT,
            ),
        )
        proposals: list[dict[str, Any]] = []
        if self._evolution is not None:
            try:
                proposals = self.list_proposals(character, status=STATUS_PROPOSED)
            except Exception:  # noqa: BLE001 - snapshot stays readable
                proposals = []
        return AdminSnapshot(
            character=character,
            auto_evolution=settings,
            card=card,
            revisions=tuple(self.revisions(character)),
            decisions=decisions,
            proposals=tuple(proposals),
            pending_proposals=len(proposals),
        )

    # -- deletion / correction fan-out ----------------------------------

    def cleanup(
        self, character: str, resolved: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Withdraw proposals and drop decisions touched by deleted sources."""
        character = self._character(character)
        deleted = self._resolved_refs(resolved)
        withdrawn: list[str] = []
        if self._evolution is not None and deleted:
            try:
                proposals = self._proposal_list(character, STATUS_PROPOSED)
            except Exception:  # noqa: BLE001 - evolution may be unavailable
                proposals = []
            for proposal in proposals:
                refs = set(self._proposal_refs(proposal))
                if not refs & deleted:
                    continue
                pid = str(proposal.get("proposal_id") or "")
                if not pid:
                    continue
                updated = self._proposal_update(character, pid, {
                    "status": STATUS_WITHDRAWN,
                    "reason": REASON_SOURCE_DELETED,
                    "withdrawn_at": self._now_iso(),
                })
                if updated is not None:
                    withdrawn.append(pid)
        card_cleanup = getattr(self._card_service, "cleanup", None)
        if callable(card_cleanup):
            try:
                card_cleanup(character, resolved)
            except Exception:  # noqa: BLE001 - card cleaner may be registered twice
                pass
        removed, narrowed = self._filter_decisions(character, deleted)
        published: Mapping[str, Any] | None = None
        if self._publish is not None:
            try:
                published = self._publish.invalidate_for_deletion(
                    character, resolved,
                )
            except Exception:  # noqa: BLE001 - publish may be unavailable
                published = None
        return {
            "status": "completed",
            "detail": {
                "proposals_withdrawn": len(withdrawn),
                "proposal_ids": withdrawn,
                "decisions_removed": removed,
                "decisions_narrowed": narrowed,
                "published": dict(published) if isinstance(published, Mapping) else None,
            },
        }

    def invalidate(
        self,
        character: str,
        fact_ids: Sequence[str],
        *,
        invalidated_at: str,
        reason: str,
        correction_id: str,
    ) -> Mapping[str, Any]:
        """Mark proposals/decisions derived from corrected facts as invalid."""
        character = self._character(character)
        targets = {
            str(item) for item in fact_ids or ()
            if isinstance(item, str) and item
        }
        if not targets:
            return {"proposal_ids": [], "decision_ids": [], "card_ids": []}
        proposal_ids: list[str] = []
        if self._evolution is not None:
            try:
                proposals = self._proposal_list(character, STATUS_PROPOSED)
            except Exception:  # noqa: BLE001
                proposals = []
            for proposal in proposals:
                refs = set(self._proposal_refs(proposal))
                if not refs & targets:
                    continue
                pid = str(proposal.get("proposal_id") or "")
                if not pid:
                    continue
                updated = self._proposal_update(character, pid, {
                    "status": STATUS_WITHDRAWN,
                    "reason": "source_corrected",
                    "invalidated_at": invalidated_at,
                    "correction_id": correction_id,
                })
                if updated is not None:
                    proposal_ids.append(pid)
        decision_ids: list[str] = []
        document = self._store.load(character)
        decisions: list[dict[str, Any]] = []
        for row in document.get("decisions") or []:
            refs = set(row.get("source_refs") or [])
            hit = refs & targets
            if not hit:
                decisions.append(dict(row))
                continue
            updated = copy.deepcopy(dict(row))
            updated["source_refs"] = [ref for ref in refs if ref not in targets]
            updated["invalidated_at"] = invalidated_at
            updated["invalidated_reason"] = reason
            updated["correction_id"] = correction_id
            updated["needs_rebuild"] = True
            decision_ids.append(str(row.get("decision_id") or ""))
            decisions.append(updated)
        if decision_ids:
            document["decisions"] = decisions
            self._store.save(character, document)
        published: Mapping[str, Any] | None = None
        if self._publish is not None:
            try:
                published = self._publish.invalidate_for_deletion(
                    character, {"fact_ids": sorted(targets)},
                )
            except Exception:  # noqa: BLE001
                published = None
        return {
            "proposal_ids": proposal_ids,
            "decision_ids": decision_ids,
            "card_ids": [],
            "published": dict(published) if isinstance(published, Mapping) else None,
        }

    # -- decision helpers ----------------------------------------------

    def _make_decision(
        self,
        character: str,
        *,
        proposal_id: str,
        action: str,
        status: str,
        actor: str,
        reason: str,
        at: str,
        revision_before: int,
        revision_after: int,
        source_refs: Sequence[str],
        proposal: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        digest = hashlib.sha1(
            f"{character}|{proposal_id}|{action}|{revision_before}|{revision_after}"
            .encode("utf-8"),
        ).hexdigest()[:16]
        decision: dict[str, Any] = {
            "decision_id": f"adm_{digest}",
            "proposal_id": proposal_id,
            "action": action,
            "status": status,
            "actor": actor,
            "reason": str(reason or "")[:400],
            "at": at,
            "revision_before": int(revision_before),
            "revision_after": int(revision_after),
            "source_refs": self._dedupe(source_refs)[:MAX_SOURCE_REFS],
        }
        if proposal is not None:
            decision["base_revision"] = int(proposal.get("base_revision") or 0)
            decision["operations_count"] = len(proposal.get("operations") or [])
        if extra:
            decision.update(copy.deepcopy(dict(extra)))
        return decision

    def _write_decision(
        self, character: str, decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        decision_id = str(decision.get("decision_id") or "")
        if self._journal is None:
            return self._store.append_decision(character, decision)
        document = self._store.load(character)
        document["decisions"] = list(document.get("decisions") or []) + [
            copy.deepcopy(dict(decision)),
        ]
        view_document = copy.deepcopy(document)

        def _mutate(view: Any) -> None:
            if not isinstance(view, dict):
                raise RuntimeError("[persona_admin] view is not a dict")
            view.clear()
            view.update(copy.deepcopy(view_document))

        self._journal.record_and_save(
            character,
            EVT_PERSONA_ADMIN_DECISION,
            {"character": character, "decision": copy.deepcopy(dict(decision))},
            load_view=self._store.load,
            mutate_view=_mutate,
            save_view=self._store.save,
        )
        stored = self._store.get_decision(character, decision_id)
        return stored if stored is not None else dict(decision)

    def _replay_decision(self, character: str, payload: Mapping[str, Any]) -> bool:
        if not isinstance(payload, Mapping):
            raise RuntimeError("core.persona_admin_decision payload is not an object")
        raw = payload.get("decision")
        if not isinstance(raw, Mapping):
            raw = payload
        decision_id = str(raw.get("decision_id") or "").strip()
        if not decision_id:
            raise RuntimeError("core.persona_admin_decision has no decision_id")
        existing = self._store.get_decision(character, decision_id)
        if existing is not None:
            return False
        self._store.append_decision(character, raw)
        return True

    def _filter_decisions(
        self, character: str, deleted: set[str],
    ) -> tuple[int, int]:
        if not deleted:
            return 0, 0
        document = self._store.load(character)
        kept: list[dict[str, Any]] = []
        removed = 0
        narrowed = 0
        for row in document.get("decisions") or []:
            refs = [ref for ref in row.get("source_refs") or []
                    if isinstance(ref, str)]
            hit = set(refs) & deleted
            if not hit:
                kept.append(dict(row))
                continue
            remaining = [ref for ref in refs if ref not in deleted]
            if not remaining:
                removed += 1
                continue
            updated = copy.deepcopy(dict(row))
            updated["source_refs"] = remaining
            updated["invalidated_sources"] = sorted(
                set(updated.get("invalidated_sources") or []) | hit,
            )
            updated["needs_rebuild"] = True
            kept.append(updated)
            narrowed += 1
        if removed or narrowed:
            document["decisions"] = kept
            self._store.save(character, document)
        return removed, narrowed

    # -- proposal adapter ----------------------------------------------

    def _proposal_list(
        self, character: str, status: str | None,
    ) -> list[dict[str, Any]]:
        service = self._evolution
        lister = getattr(service, "list", None)
        if callable(lister):
            rows = lister(character, status=status)
            return [dict(row) for row in rows if isinstance(row, Mapping)]
        store = getattr(service, "_store", None)
        if store is not None and callable(getattr(store, "list", None)):
            return [dict(row) for row in store.list(character, status=status)]
        raise NotConfiguredError(
            "evolution_service has no list API; proposal review is unavailable",
        )

    def _proposal_get(
        self, character: str, proposal_id: str,
    ) -> dict[str, Any] | None:
        service = self._evolution
        if service is None:
            return None
        getter = getattr(service, "get", None)
        if callable(getter):
            row = getter(character, proposal_id)
            return dict(row) if isinstance(row, Mapping) else None
        store = getattr(service, "_store", None)
        if store is not None and callable(getattr(store, "get", None)):
            row = store.get(character, proposal_id)
            return dict(row) if isinstance(row, Mapping) else None
        return None

    def _proposal_update(
        self, character: str, proposal_id: str, fields: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        service = self._evolution
        if service is None:
            return None
        for name in ("update_proposal", "update"):
            method = getattr(service, name, None)
            if callable(method):
                try:
                    result = method(character, proposal_id, fields)
                except Exception:  # noqa: BLE001 - caller reports mark failure
                    return None
                return result if isinstance(result, Mapping) else None
        if fields.get("status") == STATUS_WITHDRAWN:
            method = getattr(service, "withdraw", None)
            if callable(method):
                try:
                    result = method(character, proposal_id)
                except Exception:  # noqa: BLE001
                    return None
                return result if isinstance(result, Mapping) else None
        store = getattr(service, "_store", None)
        if store is not None and callable(getattr(store, "update", None)):
            try:
                result = store.update(character, proposal_id, fields)
            except Exception:  # noqa: BLE001
                return None
            return result if isinstance(result, Mapping) else None
        return None

    def _proposal_refs(self, proposal: Mapping[str, Any]) -> list[str]:
        refs: list[str] = []
        evidence = proposal.get("evidence")
        if isinstance(evidence, Mapping):
            for key in ("source_refs", "growth_ids", "reflection_ids", "fact_ids"):
                for item in evidence.get(key) or []:
                    if isinstance(item, str) and item and item not in refs:
                        refs.append(item)
        for op in proposal.get("operations") or []:
            if not isinstance(op, Mapping):
                continue
            for item in op.get("source_refs") or []:
                if isinstance(item, str) and item and item not in refs:
                    refs.append(item)
        return refs

    def _proposal_blocked(
        self, character: str, proposal: Mapping[str, Any],
    ) -> bool:
        if self._barrier is None:
            return False
        for ref in self._proposal_refs(proposal):
            for target_type in ("fact", "reflection", "persona", "growth"):
                try:
                    if self._barrier.blocks_target(character, target_type, ref):
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def _proposal_expired(self, proposal: Mapping[str, Any]) -> bool:
        created = proposal.get("created_at")
        if not isinstance(created, str) or not created:
            return False
        try:
            created_at = datetime.fromisoformat(created)
        except ValueError:
            return False
        now = self._clock.now()
        if created_at.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        elif created_at.tzinfo is not None and now.tzinfo is None:
            created_at = created_at.replace(tzinfo=None)
        hours = DEFAULT_PROPOSAL_TTL_HOURS
        try:
            hours = float(
                getattr(self._evolution.settings, "proposal_ttl_hours", hours),
            )
        except Exception:  # noqa: BLE001 - fall back to the design default
            hours = DEFAULT_PROPOSAL_TTL_HOURS
        return now > created_at + timedelta(hours=hours)

    # -- card history helpers ------------------------------------------

    def _raw_card(self, character: str) -> dict[str, Any] | None:
        card_store = getattr(self._card_service, "_store", None)
        loader = getattr(card_store, "load", None)
        if not callable(loader):
            return None
        try:
            raw = loader(character)
        except Exception:  # noqa: BLE001 - diff/rollback report unavailable
            return None
        return raw if isinstance(raw, Mapping) else None

    def _sections_at(
        self, character: str, raw: Mapping[str, Any], revision: int,
    ) -> list[dict[str, Any]] | None:
        if revision == int(raw.get("active_revision") or 0):
            sections = raw.get("sections")
            if isinstance(sections, list):
                return self._visible_sections(character, sections)
        if revision == int(raw.get("initial_revision") or 0):
            initial = raw.get("initial_sections")
            if isinstance(initial, list):
                return self._visible_sections(character, initial)
        record = self._revision_record(character, raw, revision)
        if record is None:
            return None
        snapshot = record.get("sections_snapshot")
        if not isinstance(snapshot, list):
            return None
        return self._visible_sections(character, snapshot)

    def _revision_record(
        self, character: str, raw: Mapping[str, Any], revision: int,
    ) -> Mapping[str, Any] | None:
        for record in raw.get("revisions") or []:
            if not isinstance(record, Mapping):
                continue
            if int(record.get("revision") or 0) == revision:
                return record
        card_store = getattr(self._card_service, "_store", None)
        loader = getattr(card_store, "load_archive", None)
        if callable(loader):
            try:
                archived = loader(character)
            except Exception:  # noqa: BLE001
                archived = []
            for record in archived:
                if not isinstance(record, Mapping):
                    continue
                if int(record.get("revision") or 0) == revision:
                    return record
        return None

    def _visible_sections(
        self, character: str, sections: Sequence[Any],
    ) -> list[dict[str, Any]]:
        visible: list[dict[str, Any]] = []
        card_store = getattr(self._card_service, "_store", None)
        section_blocked = getattr(card_store, "section_blocked", None)
        for row in sections:
            if not isinstance(row, Mapping):
                continue
            if row.get("invalidated_at") or row.get("needs_rebuild"):
                continue
            if callable(section_blocked):
                try:
                    if section_blocked(character, row):
                        continue
                except Exception:  # noqa: BLE001
                    pass
            if self._barrier is not None:
                try:
                    if self._barrier.blocks_row(character, dict(row)):
                        continue
                except Exception:  # noqa: BLE001
                    pass
                blocked = False
                for ref in row.get("source_refs") or []:
                    if not isinstance(ref, str) or not ref:
                        continue
                    for target_type in ("fact", "reflection", "persona", "growth"):
                        try:
                            if self._barrier.blocks_target(
                                character, target_type, ref,
                            ):
                                blocked = True
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    if blocked:
                        break
                if blocked:
                    continue
            visible.append(copy.deepcopy(dict(row)))
        return visible

    @staticmethod
    def _section_view(row: Mapping[str, Any]) -> dict[str, Any]:
        scope = row.get("scope")
        editable = row.get("editable_by")
        return {
            "section_id": str(row.get("section_id") or ""),
            "text": str(row.get("text") or ""),
            "kind": str(row.get("kind") or "identity"),
            "scope": copy.deepcopy(dict(scope)) if isinstance(scope, Mapping) else {},
            "editable_by": (
                copy.deepcopy(dict(editable))
                if isinstance(editable, Mapping) else {}
            ),
            "frozen": bool(row.get("frozen")),
            "origin": str(row.get("origin") or "imported"),
            "source_refs": [
                str(item) for item in row.get("source_refs") or []
                if isinstance(item, str)
            ],
        }

    @staticmethod
    def _section_fingerprint(section: Mapping[str, Any]) -> str:
        parts = (
            str(section.get("text") or ""),
            str(section.get("kind") or ""),
            repr(section.get("scope") or {}),
            repr(section.get("editable_by") or {}),
            str(bool(section.get("frozen"))),
            str(section.get("origin") or ""),
        )
        return "|".join(parts)

    @staticmethod
    def _resolved_refs(resolved: Mapping[str, Any]) -> set[str]:
        values: list[str] = []
        for key in (
            "reflection_ids", "fact_ids", "persona_ids", "source_keys",
            "fact_source_keys", "growth_ids",
        ):
            for item in resolved.get(key) or []:
                if isinstance(item, str) and item:
                    values.append(item)
        return set(values)

    @staticmethod
    def _dedupe(values: Any) -> list[str]:
        out: list[str] = []
        for value in values or ():
            if isinstance(value, str) and value and value not in out:
                out.append(value)
        return out


__all__ = [
    "AdminSettings",
    "AUTO_MODES",
    "AdminSnapshot",
    "DecisionReceipt",
    "DecisionRequest",
    "EVT_PERSONA_ADMIN_DECISION",
    "PersonaAdminService",
    "RevisionDiff",
]
