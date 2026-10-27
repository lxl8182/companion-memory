"""C02 persona projection service: host import, sync, consumers, host edits.

This service is the engine-side compatibility layer between the authoritative
card (B10), the published versions (B11) and the host's characters.json
projection. It never writes characters.json: the host adapter reads
resolve_authoritative()/ensure_for_consumer() and writes only the host copy.

Responsibilities:

* import the host card exactly once (system_prompt/persona_override/semantic
  fields -> card sections; resources/tools/voice/model keys stay host-side);
* sync a new card revision (and its published token when available) into a
  projection record, preserving consumer adoption state;
* expose per-consumer status saved/pending/published/adopted/failed plus
  pending/failed queues and retry;
* accept host edits as explicit base_revision revisions; a conflict returns the
  current snapshot and the caller's draft unchanged;
* resolve the authoritative projection for the host, or None so the host can
  fall back to the old card when the engine has no card yet;
* invalidate stale projection/pending/consumer versions on deletion or
  correction and write pending receipts;
* optional idempotent replay of core.persona_projection_synced snapshots.

No host imports, no model calls, temp roots only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..persistence.persona_projection import (
    CONSUMERS,
    CONSUMER_STATUSES,
    PersonaProjectionStore,
    text_sha256,
)
from ..ports import Clock, StorageRoots, WritePermit
from .persona_card import CardSnapshot, PersonaCardService, RevisionRequest

#: Optional journal event for full projection snapshots.
EVT_PERSONA_PROJECTION_SYNCED = "core.persona_projection_synced"

REASON_ALREADY_IMPORTED = "already_imported"
REASON_EMPTY_HOST_CARD = "empty_host_card"
REASON_NO_CARD = "no_card"
REASON_NO_PROJECTION = "no_projection"
REASON_UNKNOWN_VERSION = "unknown_version"
REASON_STALE_VERSION = "stale_version"
REASON_VERSION_MISMATCH = "version_mismatch"
REASON_SOURCE_DELETED = "source_deleted"
REASON_SOURCE_CORRECTED = "source_corrected"
REASON_INVALID_REQUEST = "invalid_request"

MAX_HOST_TEXT = 8000
MAX_HOST_SECTIONS = 32
MAX_REASON = 400

_HOST_FIELDS: tuple[str, ...] = (
    "system_prompt",
    "persona_override",
    "nickname",
    "personality",
    "background",
    "speech_style",
    "interests",
    "habits",
    "relationship",
    "boundaries",
    "identity",
    "appearance",
)

_RESOURCE_HINTS: tuple[str, ...] = (
    "model", "voice", "api", "tool", "live2d", "resource", "path",
    "config", "temperature", "max_token", "secret", "key", "url",
)


@dataclass(frozen=True)
class HostCardImportReceipt:
    status: str
    revision: int = 0
    version: str = ""
    projection: Mapping[str, str] = field(default_factory=dict)
    text: str = ""
    duplicate: bool = False
    reason: str | None = None
    host_import: Mapping[str, Any] = field(default_factory=dict)
    card: CardSnapshot | None = None


@dataclass(frozen=True)
class HostEditRequest:
    base_revision: int
    operations: Sequence[Mapping[str, Any]] = ()
    sections: Sequence[Mapping[str, Any]] = ()
    actor: str = "user"
    reason: str = ""


@dataclass(frozen=True)
class HostEditReceipt:
    status: str
    revision_before: int = 0
    revision_after: int = 0
    version: str = ""
    projection: Mapping[str, str] = field(default_factory=dict)
    text: str = ""
    reason: str | None = None
    conflict: bool = False
    draft: Mapping[str, Any] | None = None
    card: CardSnapshot | None = None


@dataclass(frozen=True)
class AdoptReceipt:
    status: str
    consumer: str = ""
    expected_version: str | None = None
    version: str = ""
    revision: int = 0
    projection: Mapping[str, str] = field(default_factory=dict)
    text: str = ""
    needs_reissue: bool = False
    reason: str | None = None
    consumer_state: Mapping[str, Any] | None = None


class PersonaProjectionService:
    """Projection/consumer coordination for one authoritative card."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: PersonaProjectionStore,
        clock: Clock,
        permit: WritePermit,
        barrier: Any,
        card_service: PersonaCardService,
        publish_service: Any = None,
        journal: Any = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._barrier = barrier
        self._card = card_service
        self._publish = publish_service
        self._journal = journal
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        if journal is not None:
            try:
                journal.register_handler(
                    EVT_PERSONA_PROJECTION_SYNCED, self._replay_projection,
                )
            except Exception:  # noqa: BLE001 - optional journal may be read-only
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
        if types is not None and EVT_PERSONA_PROJECTION_SYNCED not in types:
            try:
                self._journal._event_types = (
                    frozenset(types) | {EVT_PERSONA_PROJECTION_SYNCED}
                )
            except Exception:  # noqa: BLE001 - journal stays read-only for us
                pass

    # -- host import ----------------------------------------------------

    def import_from_host_card(
        self,
        character: str,
        host_card: Mapping[str, Any],
        *,
        base_revision: int = 0,
    ) -> HostCardImportReceipt:
        """Import the host card once; resources/tools/voice stay host-side."""
        character = self._character(character)
        if not isinstance(host_card, Mapping):
            return HostCardImportReceipt(
                status="rejected", reason=REASON_INVALID_REQUEST,
            )
        with self._lock(character):
            card = self._card.get(character)
            if card.revision > 0:
                self.sync_from_card(character)
                document = self._store.load(character)
                projection, text = self._document_view(character, document)
                return HostCardImportReceipt(
                    status="duplicate", duplicate=True,
                    revision=int(card.revision),
                    version=str(document.get("version") or ""),
                    projection=projection, text=text,
                    reason=REASON_ALREADY_IMPORTED,
                    host_import=copy.deepcopy(document.get("host_import") or {}),
                    card=card,
                )
            sections = self._host_card_sections(character, host_card)
            if not sections:
                return HostCardImportReceipt(
                    status="rejected", reason=REASON_EMPTY_HOST_CARD,
                )
            receipt = self._card.import_initial(
                character, sections, source="host_import", actor="host",
            )
            if receipt.status == "duplicate":
                self.sync_from_card(character)
                document = self._store.load(character)
                projection, text = self._document_view(character, document)
                return HostCardImportReceipt(
                    status="duplicate", duplicate=True,
                    revision=int(receipt.revision or card.revision),
                    version=str(document.get("version") or ""),
                    projection=projection, text=text,
                    reason=REASON_ALREADY_IMPORTED,
                    host_import=copy.deepcopy(document.get("host_import") or {}),
                    card=receipt.card,
                )
            if receipt.status != "completed":
                return HostCardImportReceipt(
                    status="rejected",
                    reason=receipt.reason or REASON_INVALID_REQUEST,
                    card=receipt.card,
                )
            changed = self._sync_document(character)
            document = self._store.load(character)
            fingerprint = hashlib.sha256(
                json.dumps(host_card, sort_keys=True, default=str).encode(
                    "utf-8", "replace",
                ),
            ).hexdigest()
            document["host_import"] = {
                "imported_at": self._now_iso(),
                "base_revision": int(base_revision or 0),
                "fingerprint": fingerprint,
                "section_ids": [section["section_id"] for section in sections],
            }
            self._write_document(character, document, action="import")
            document = self._store.load(character)
            projection, text = self._document_view(character, document)
            return HostCardImportReceipt(
                status="completed",
                revision=int(document.get("card_revision") or receipt.revision),
                version=str(document.get("version") or ""),
                projection=projection, text=text,
                host_import=copy.deepcopy(document.get("host_import") or {}),
                card=self._card.get(character),
            )

    # -- sync -----------------------------------------------------------

    def sync_from_card(self, character: str) -> bool:
        """Update the projection when card revision/projection/version change."""
        character = self._character(character)
        with self._lock(character):
            return self._sync_document(character)

    def _sync_document(self, character: str) -> bool:
        card = self._card.get(character)
        if card.revision <= 0:
            return False
        projection = dict(self._card.render_projection(character))
        text = self._card.render_text(character)
        version, _published = self._version_for(card, projection, text)
        document = self._store.load(character)
        if document.get("invalidated") and int(
            document.get("invalidated_revision") or 0,
        ) == int(card.revision):
            # The projection was invalidated by a deletion/correction. Do not
            # resurrect it from the still-unrebuilt card at the same revision.
            return False
        digest = text_sha256(text)
        if (
            int(document.get("card_revision") or 0) == int(card.revision)
            and str(document.get("version") or "") == version
            and dict(document.get("projection") or {}) == projection
            and str(document.get("text_sha256") or "") == digest
        ):
            return False
        document["card_revision"] = int(card.revision)
        document["version"] = version
        document["projection"] = projection
        document["text_sha256"] = digest
        document["updated_at"] = self._now_iso()
        document.pop("invalidated", None)
        document.pop("invalidated_revision", None)
        document["history"] = list(document.get("history") or []) + [{
            "action": "sync",
            "version": version,
            "revision": int(card.revision),
            "at": document["updated_at"],
        }]
        self._write_document(character, document, action="sync")
        return True

    # -- status ---------------------------------------------------------

    def status(self, character: str) -> dict[str, Any]:
        """Current projection/version and per-consumer effective status."""
        character = self._character(character)
        document = self._store.load(character)
        card = self._card.get(character)
        projection, text = self._document_view(character, document)
        version = str(document.get("version") or "")
        published = self._is_published(card, projection, text, version)
        stale = bool(card.revision > 0 and (
            int(document.get("card_revision") or 0) != int(card.revision)
            or not version
        ))
        consumers: dict[str, Any] = {}
        for name in CONSUMERS:
            row = (document.get("consumers") or {}).get(name)
            if not isinstance(row, Mapping):
                consumers[name] = {
                    "version": version, "revision": int(document.get("card_revision") or 0),
                    "status": "published" if published else "saved",
                    "receipt_id": None, "attempts": 0,
                    "updated_at": None, "reason": None,
                }
                continue
            entry = copy.deepcopy(dict(row))
            if version and entry.get("version") != version:
                entry["status"] = "published" if published else "saved"
            consumers[name] = entry
        return {
            "character": character,
            "card_revision": int(document.get("card_revision") or 0),
            "version": version,
            "projection": projection,
            "text": text,
            "text_sha256": str(document.get("text_sha256") or text_sha256(text)),
            "updated_at": document.get("updated_at"),
            "stale": stale,
            "published": published,
            "consumers": consumers,
            "pending": copy.deepcopy(document.get("pending") or []),
            "failed": copy.deepcopy(document.get("failed") or []),
            "host_import": copy.deepcopy(document.get("host_import") or {}),
            "invalidated": bool(document.get("invalidated")),
            "source": "engine",
        }

    # -- consumer adoption ---------------------------------------------

    def adopt(
        self,
        character: str,
        consumer: str,
        version: str,
        *,
        receipt_id: str | None = None,
        status: str = "adopted",
    ) -> AdoptReceipt:
        """Record one consumer adopting a version; mismatch returns current."""
        character = self._character(character)
        consumer = self._store._consumer_name(consumer)
        if status not in CONSUMER_STATUSES:
            raise ValueError(f"invalid consumer status {status!r}")
        with self._lock(character):
            document = self._store.load(character)
            current_version = str(document.get("version") or "")
            projection, text = self._document_view(character, document)
            if not current_version or not projection:
                return AdoptReceipt(
                    status="unknown", consumer=consumer,
                    expected_version=version, reason=REASON_NO_PROJECTION,
                )
            expected = str(version or "")
            if expected == current_version:
                updated = self._store.update_consumer(
                    character, consumer,
                    version=current_version,
                    revision=int(document.get("card_revision") or 0),
                    status=status, receipt_id=receipt_id,
                )
                self._store.clear(character, consumer)
                updated = self._store.load(character)
                state = copy.deepcopy(
                    (updated.get("consumers") or {}).get(consumer) or {},
                )
                return AdoptReceipt(
                    status="match", consumer=consumer,
                    expected_version=expected, version=current_version,
                    revision=int(document.get("card_revision") or 0),
                    projection=projection, text=text,
                    consumer_state=state,
                )
            row = (document.get("consumers") or {}).get(consumer)
            known = self._known_versions(document)
            if isinstance(row, Mapping) and row.get("version") == expected:
                reason = REASON_STALE_VERSION
                status_name = "stale"
            elif expected in known:
                reason = REASON_VERSION_MISMATCH
                status_name = "mismatch"
            else:
                return AdoptReceipt(
                    status="unknown", consumer=consumer,
                    expected_version=expected, version=current_version,
                    revision=int(document.get("card_revision") or 0),
                    projection=projection, text=text,
                    reason=REASON_UNKNOWN_VERSION,
                )
            return AdoptReceipt(
                status=status_name, consumer=consumer,
                expected_version=expected, version=current_version,
                revision=int(document.get("card_revision") or 0),
                projection=projection, text=text,
                needs_reissue=True, reason=reason,
            )

    def mark_pending(
        self, character: str, consumer: str, version: str, reason: str,
    ) -> dict[str, Any]:
        character = self._character(character)
        document = self._store.load(character)
        revision = int(document.get("card_revision") or 0)
        updated = self._store.mark_pending(
            character, consumer, version, reason, revision=revision,
        )
        return self._pending_view(updated, consumer)

    def retry_failed(self, character: str, consumer: str) -> dict[str, Any]:
        """Move one failed consumer back to pending (attempts preserved)."""
        character = self._character(character)
        consumer = self._store._consumer_name(consumer)
        with self._lock(character):
            document = self._store.load(character)
            failed = [
                row for row in document.get("failed") or []
                if row.get("consumer") == consumer
            ]
            if not failed:
                return {
                    "status": "none", "consumer": consumer,
                    "reason": "no_failed_entry",
                }
            row = failed[-1]
            self._store.clear(character, consumer)
            updated = self._store.mark_pending(
                character, consumer, str(row.get("version") or ""),
                "retry", revision=int(row.get("revision") or 0),
            )
            return self._pending_view(updated, consumer)

    # -- host edits -----------------------------------------------------

    def apply_host_edit(
        self,
        character: str,
        request: HostEditRequest | Mapping[str, Any],
    ) -> HostEditReceipt:
        """Apply an explicit host edit through PersonaCardService.revise."""
        character = self._character(character)
        raw = self._request_mapping(request)
        try:
            base_revision = int(raw.get("base_revision"))
        except (TypeError, ValueError):
            return HostEditReceipt(
                status="rejected", reason=REASON_INVALID_REQUEST,
                draft=copy.deepcopy(raw),
            )
        operations = raw.get("operations")
        sections = raw.get("sections")
        if operations is not None and not isinstance(operations, (list, tuple)):
            return HostEditReceipt(
                status="rejected", reason=REASON_INVALID_REQUEST,
                draft=copy.deepcopy(raw),
            )
        if sections is not None and not isinstance(sections, (list, tuple)):
            return HostEditReceipt(
                status="rejected", reason=REASON_INVALID_REQUEST,
                draft=copy.deepcopy(raw),
            )
        actor = str(raw.get("actor") or "user")[:64]
        reason = str(raw.get("reason") or "")[:MAX_REASON]
        with self._lock(character):
            before = self._card.get(character)
            if not operations:
                if not sections:
                    return HostEditReceipt(
                        status="rejected", reason=REASON_INVALID_REQUEST,
                        revision_before=int(before.revision),
                        draft=copy.deepcopy(raw),
                    )
                try:
                    operations = self._sections_to_operations(
                        character, before, sections,
                    )
                except ValueError as exc:
                    return HostEditReceipt(
                        status="rejected", reason=str(exc),
                        revision_before=int(before.revision),
                        draft=copy.deepcopy(raw),
                    )
            if not operations:
                return HostEditReceipt(
                    status="duplicate", reason="no_change",
                    revision_before=int(before.revision),
                    revision_after=int(before.revision),
                    version=self._store.load(character).get("version") or "",
                    draft=copy.deepcopy(raw),
                )
            receipt = self._card.revise(character, RevisionRequest(
                base_revision=base_revision,
                operations=list(operations),
                reason=reason,
                origin="user_edit",
                actor=actor,
            ))
            if receipt.status != "completed":
                current = receipt.card or self._card.get(character)
                document = self._store.load(character)
                projection, text = self._document_view(character, document)
                return HostEditReceipt(
                    status="conflict" if receipt.conflict else (
                        "duplicate" if receipt.status == "duplicate" else "rejected"
                    ),
                    revision_before=int(before.revision),
                    revision_after=int(current.revision),
                    version=str(document.get("version") or ""),
                    projection=projection, text=text,
                    reason=receipt.reason or receipt.status,
                    conflict=bool(receipt.conflict),
                    draft=copy.deepcopy(raw),
                    card=current,
                )
            self._sync_document(character)
            document = self._store.load(character)
            projection, text = self._document_view(character, document)
            return HostEditReceipt(
                status="applied",
                revision_before=int(before.revision),
                revision_after=int(receipt.revision),
                version=str(document.get("version") or ""),
                projection=projection, text=text,
                draft=copy.deepcopy(raw),
                card=receipt.card,
            )

    # -- authoritative resolution --------------------------------------

    def resolve_authoritative(self, character: str) -> dict[str, Any] | None:
        """Engine projection for the host, or None to fall back to old card."""
        character = self._character(character)
        document = self._store.load(character)
        if document.get("invalidated"):
            return None
        card = self._card.get(character)
        if (
            card.revision > 0
            and int(document.get("card_revision") or 0) != int(card.revision)
        ):
            self.sync_from_card(character)
            document = self._store.load(character)
        projection, text = self._document_view(character, document)
        version = str(document.get("version") or "")
        if version and projection:
            return {
                "revision": int(document.get("card_revision") or 0),
                "version": version,
                "projection": projection,
                "text": text,
                "source": "engine",
                "persisted": True,
                "updated_at": document.get("updated_at"),
            }
        card = self._card.get(character)
        if card.revision <= 0:
            return None
        projection = dict(self._card.render_projection(character))
        text = self._card.render_text(character)
        version, _published = self._version_for(card, projection, text)
        return {
            "revision": int(card.revision),
            "version": version,
            "projection": projection,
            "text": text,
            "source": "engine",
            "persisted": False,
            "updated_at": None,
        }

    def ensure_for_consumer(
        self, character: str, consumer: str,
    ) -> dict[str, Any]:
        """Latest version the consumer should adopt, with its current state."""
        character = self._character(character)
        consumer = self._store._consumer_name(consumer)
        self.sync_from_card(character)
        document = self._store.load(character)
        projection, text = self._document_view(character, document)
        version = str(document.get("version") or "")
        if not version or not projection:
            return {
                "consumer": consumer, "status": "unavailable",
                "version": "", "revision": 0,
                "projection": {}, "text": "",
                "needs_update": False,
                "reason": REASON_NO_PROJECTION,
            }
        card = self._card.get(character)
        published = self._is_published(card, projection, text, version)
        row = (document.get("consumers") or {}).get(consumer)
        if not isinstance(row, Mapping):
            state = "published" if published else "saved"
        elif row.get("version") != version:
            state = "published" if published else "saved"
        else:
            state = str(row.get("status") or "saved")
        return {
            "consumer": consumer,
            "status": state,
            "version": version,
            "revision": int(document.get("card_revision") or 0),
            "projection": projection,
            "text": text,
            "needs_update": state not in ("adopted",),
            "receipt_id": (
                row.get("receipt_id") if isinstance(row, Mapping) else None
            ),
            "attempts": int(row.get("attempts") or 0) if isinstance(row, Mapping) else 0,
        }

    # -- invalidation ---------------------------------------------------

    def invalidate_for_deletion(
        self, character: str, resolved: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Invalidate stale projection/consumers after a source deletion."""
        character = self._character(character)
        deleted = self._resolved_refs(resolved)
        hashes = {
            str(item) for item in resolved.get("text_hashes") or []
            if isinstance(item, str) and item
        }
        return self._invalidate(
            character, deleted, hashes, reason=REASON_SOURCE_DELETED,
        )

    def invalidate_for_correction(
        self, character: str, fact_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Invalidate stale projection/consumers after a correction."""
        character = self._character(character)
        deleted = {
            str(item) for item in fact_ids or ()
            if isinstance(item, str) and item
        }
        return self._invalidate(
            character, deleted, set(), reason=REASON_SOURCE_CORRECTED,
        )

    def _invalidate(
        self,
        character: str,
        deleted: set[str],
        text_hashes: set[str],
        *,
        reason: str,
    ) -> dict[str, Any]:
        if not deleted and not text_hashes:
            return {
                "invalidated_versions": [], "pending": [],
                "current_invalidated": False,
            }
        with self._lock(character):
            document = self._store.load(character)
            version = str(document.get("version") or "")
            if not version and not document.get("projection"):
                return {
                    "invalidated_versions": [], "pending": [],
                    "current_invalidated": False,
                }
            affected = bool(text_hashes & {str(document.get("text_sha256") or "")})
            card = self._card.get(character)
            for section in getattr(card, "sections", ()) or ():
                refs = set(getattr(section, "source_refs", ()) or ())
                if refs & deleted:
                    affected = True
                    break
            if not affected:
                return {
                    "invalidated_versions": [], "pending": [],
                    "current_invalidated": False,
                }
            now = self._now_iso()
            old_revision = int(document.get("card_revision") or 0)
            pending = [
                row for row in document.get("pending") or []
                if row.get("version") != version
            ]
            failed = [
                row for row in document.get("failed") or []
                if row.get("version") != version
            ]
            consumers = dict(document.get("consumers") or {})
            for name, row in list(consumers.items()):
                if not isinstance(row, Mapping):
                    continue
                if row.get("version") not in ("", None, version):
                    continue
                pending.append({
                    "consumer": name,
                    "version": str(row.get("version") or version),
                    "revision": int(row.get("revision") or 0),
                    "reason": reason,
                    "attempts": int(row.get("attempts") or 0),
                    "created_at": now,
                    "updated_at": now,
                })
                updated = copy.deepcopy(dict(row))
                updated["status"] = "pending"
                updated["reason"] = reason
                updated["updated_at"] = now
                consumers[name] = updated
            document["consumers"] = consumers
            document["pending"] = pending
            document["failed"] = failed
            document["projection"] = {}
            document["version"] = ""
            document["text_sha256"] = ""
            document["card_revision"] = 0
            document["updated_at"] = now
            document["invalidated"] = True
            document["invalidated_revision"] = old_revision
            document["history"] = list(document.get("history") or []) + [{
                "action": "invalidate",
                "version": version,
                "revision": old_revision,
                "at": now,
                "reason": reason,
            }]
            self._write_document(character, document, action="invalidate")
            return {
                "invalidated_versions": [version] if version else [],
                "pending": copy.deepcopy(pending),
                "current_invalidated": True,
            }

    # -- journal replay -------------------------------------------------

    def _write_document(
        self, character: str, document: Mapping[str, Any], *, action: str,
    ) -> dict[str, Any]:
        payload = {
            "character": character,
            "action": action,
            "projection": copy.deepcopy(dict(document)),
        }
        view_document = copy.deepcopy(dict(document))

        def _mutate(view: Any) -> None:
            if not isinstance(view, dict):
                raise RuntimeError("[persona_projection] view is not a dict")
            view.clear()
            view.update(copy.deepcopy(view_document))

        if self._journal is None:
            self._store.save(character, view_document)
        else:
            self._journal.record_and_save(
                character,
                EVT_PERSONA_PROJECTION_SYNCED,
                payload,
                load_view=self._store.load,
                mutate_view=_mutate,
                save_view=self._store.save,
            )
        return self._store.load(character)

    def _replay_projection(self, character: str, payload: Mapping[str, Any]) -> bool:
        if not isinstance(payload, Mapping):
            raise RuntimeError(
                "core.persona_projection_synced payload is not an object",
            )
        raw = payload.get("projection")
        if not isinstance(raw, Mapping):
            raw = payload
        current = self._store.load(character)
        if raw.get("invalidated"):
            if current.get("invalidated"):
                return False
            self._store.save(character, raw)
            return True
        incoming_revision = int(raw.get("card_revision") or 0)
        if incoming_revision < int(current.get("card_revision") or 0):
            return False
        if (
            str(raw.get("version") or "") == str(current.get("version") or "")
            and dict(raw.get("projection") or {}) == dict(current.get("projection") or {})
        ):
            return False
        self._store.save(character, raw)
        return True

    # -- helpers --------------------------------------------------------

    def _is_published(
        self,
        card: CardSnapshot,
        projection: Mapping[str, str],
        text: str,
        version: str,
    ) -> bool:
        if not version or card.revision <= 0:
            return False
        chosen, published = self._version_for(card, projection, text)
        return bool(published and chosen == version)

    def _version_for(
        self,
        card: CardSnapshot,
        projection: Mapping[str, str],
        text: str,
    ) -> tuple[str, bool]:
        if self._publish is not None:
            try:
                published = self._publish.current(self._character(card.character))
            except Exception:  # noqa: BLE001 - publish is optional
                published = None
            if (
                published is not None
                and getattr(published, "available", False)
                and int(getattr(published, "revision", 0) or 0) == int(card.revision)
                and (
                    getattr(published, "text", "") == text
                    or dict(getattr(published, "projection", {}) or {}) == dict(projection)
                )
            ):
                return str(getattr(published, "version", "") or ""), True
        digest = hashlib.sha1(
            f"{int(card.revision)}:{text}".encode("utf-8", "replace"),
        ).hexdigest()[:16]
        return f"card_{digest}", False

    def _document_view(
        self, character: str, document: Mapping[str, Any],
    ) -> tuple[dict[str, str], str]:
        if document.get("invalidated"):
            return {}, ""
        projection = document.get("projection")
        if isinstance(projection, Mapping) and projection:
            clean = {str(k): str(v) for k, v in projection.items()}
            return clean, "\n\n".join(clean.values())
        card = self._card.get(character)
        if card.revision <= 0:
            return {}, ""
        clean = dict(self._card.render_projection(character))
        return clean, self._card.render_text(character)

    def _known_versions(self, document: Mapping[str, Any]) -> set[str]:
        versions: set[str] = set()
        if document.get("version"):
            versions.add(str(document["version"]))
        for row in (document.get("consumers") or {}).values():
            if isinstance(row, Mapping) and row.get("version"):
                versions.add(str(row["version"]))
        for key in ("pending", "failed", "history"):
            for row in document.get(key) or []:
                if isinstance(row, Mapping) and row.get("version"):
                    versions.add(str(row["version"]))
        return versions

    def _pending_view(
        self, document: Mapping[str, Any], consumer: str,
    ) -> dict[str, Any]:
        pending = [
            copy.deepcopy(row) for row in document.get("pending") or []
            if row.get("consumer") == consumer
        ]
        row = (document.get("consumers") or {}).get(consumer) or {}
        return {
            "status": "pending",
            "consumer": consumer,
            "version": str(row.get("version") or ""),
            "revision": int(row.get("revision") or 0),
            "attempts": int(row.get("attempts") or 0),
            "pending": pending,
        }

    def _request_mapping(
        self, request: HostEditRequest | Mapping[str, Any],
    ) -> dict[str, Any]:
        if isinstance(request, HostEditRequest):
            return {
                "base_revision": request.base_revision,
                "operations": [copy.deepcopy(dict(item)) for item in request.operations],
                "sections": [copy.deepcopy(dict(item)) for item in request.sections],
                "actor": request.actor,
                "reason": request.reason,
            }
        if isinstance(request, Mapping):
            return copy.deepcopy(dict(request))
        return {}

    def _sections_to_operations(
        self,
        character: str,
        card: CardSnapshot,
        sections: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        desired: dict[str, dict[str, Any]] = {}
        for raw in sections:
            if not isinstance(raw, Mapping):
                raise ValueError(REASON_INVALID_REQUEST)
            section_id = str(raw.get("section_id") or "").strip()
            if not section_id:
                raise ValueError(REASON_INVALID_REQUEST)
            desired[section_id] = {
                "section_id": section_id,
                "text": str(raw.get("text") or ""),
                "kind": str(raw.get("kind") or "identity"),
                "scope": copy.deepcopy(raw.get("scope") or {}),
                "editable_by": copy.deepcopy(
                    raw.get("editable_by") or {"user": True, "evolution": False},
                ),
                "frozen": bool(raw.get("frozen")),
                "source_refs": [
                    str(item) for item in raw.get("source_refs") or []
                    if isinstance(item, str)
                ],
            }
        operations: list[dict[str, Any]] = []
        current = {section.section_id for section in card.sections}
        for section_id, row in desired.items():
            operations.append({
                "op": "replace" if section_id in current else "add",
                **row,
            })
        for section_id in current:
            if section_id not in desired:
                operations.append({"op": "delete", "section_id": section_id})
        return operations

    def _host_card_sections(
        self, character: str, host_card: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(section_id: str, text: Any, kind: str) -> None:
            if not isinstance(text, str) or not text.strip():
                return
            body = text.strip()[:MAX_HOST_TEXT]
            candidate = section_id
            index = 2
            while candidate in seen:
                candidate = f"{section_id}_{index}"
                index += 1
            seen.add(candidate)
            sections.append({
                "section_id": candidate,
                "text": body,
                "kind": kind[:64] or "identity",
                "origin": "imported",
                "editable_by": {"user": True, "evolution": False},
                "source_refs": [],
            })

        explicit = host_card.get("sections")
        if isinstance(explicit, (list, tuple)):
            for raw in explicit[:MAX_HOST_SECTIONS]:
                if not isinstance(raw, Mapping):
                    continue
                before = len(sections)
                add(
                    str(raw.get("section_id") or "host_section"),
                    raw.get("text"),
                    str(raw.get("kind") or "identity"),
                )
                if len(sections) > before:
                    section = sections[-1]
                    for key in ("editable_by", "frozen", "source_refs"):
                        if key in raw:
                            section[key] = copy.deepcopy(raw[key])
            return sections

        reserved = host_card.get("_reserved")
        if isinstance(reserved, Mapping):
            add("host_system_prompt", reserved.get("system_prompt"), "system_prompt")
        add("host_system_prompt", host_card.get("system_prompt"), "system_prompt")
        add(
            "host_persona_override",
            host_card.get("persona_override"), "persona_override",
        )
        for container_key in ("persona", "persona_override"):
            container = host_card.get(container_key)
            if not isinstance(container, Mapping):
                continue
            for key, value in container.items():
                if not isinstance(key, str) or not key:
                    continue
                lowered = key.lower()
                if any(hint in lowered for hint in _RESOURCE_HINTS):
                    continue
                add(f"host_{container_key}_{key}", value, key)
        for key in _HOST_FIELDS:
            if key in ("system_prompt", "persona_override"):
                continue
            if key in host_card:
                add(f"host_{key}", host_card.get(key), key)
        return sections

    @staticmethod
    def _resolved_refs(resolved: Mapping[str, Any]) -> set[str]:
        refs: set[str] = set()
        for key in (
            "reflection_ids", "fact_ids", "persona_ids", "source_keys",
            "fact_source_keys", "growth_ids",
        ):
            for item in resolved.get(key) or []:
                if isinstance(item, str) and item:
                    refs.add(item)
        return refs


__all__ = [
    "AdoptReceipt",
    "EVT_PERSONA_PROJECTION_SYNCED",
    "HostCardImportReceipt",
    "HostEditReceipt",
    "HostEditRequest",
    "PersonaProjectionService",
]
