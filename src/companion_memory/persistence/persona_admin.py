"""Host-independent persona administration store (B13).

B13 owns the small amount of durable administration state around the card and
the evolution proposals:

* the per-character automatic-evolution switch (default OFF, independent of
  the card and of the process-level \`EvolutionSettings\`);
* a bounded audit log of review decisions (accept/reject/withdraw/rollback/
  auto-evolution toggle).

\`<character_dir>/persona_admin.json\`:

\`\`\`json
{"schema_version": 1,
 "character": "demo",
 "auto_evolution": {"enabled": false, "updated_at": null, "actor": "system"},
 "decisions": [{"decision_id": "adm_...", "proposal_id": "evo_...",
                "action": "accepted", "status": "accepted",
                "actor": "user", "reason": "", "at": "<ISO>",
                "revision_before": 3, "revision_after": 4,
                "source_refs": []}]}
\`\`\`

A missing file is the empty document. Invalid JSON, a non-object root, an
unsupported schema, a non-object auto_evolution, a non-list decisions or a
decision without \`decision_id\` raises \`CorruptStoreError\`/
\`InvalidSchemaError\`; \`save\` refuses to replace a file its reader rejects.

Writes run under one process-wide per-file RLock and follow
\`WritePermit.assert_writable -> barrier filter -> AtomicJsonStore.save\`.
Decisions are bounded (\`DEFAULT_DECISION_LIMIT\`, 64); oldest rows are pruned,
never the current card or the user's revision history. Unknown fields
round-trip verbatim.

All methods are synchronous blocking I/O; call them through
\`asyncio.to_thread\` from async code.
"""

from __future__ import annotations

import copy
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import CorruptStoreError, InvalidSchemaError
from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .shards import is_blank_file

ADMIN_FILENAME = "persona_admin.json"
ADMIN_SCHEMA_VERSION = 1
DEFAULT_DECISION_LIMIT = 64

#: One durable source for the background-evolution mode.
AUTO_MODES = ("off", "propose", "auto")
DEFAULT_AUTO_MODE = "off"

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


def empty_document(character: str = "") -> dict[str, Any]:
    return {
        "schema_version": ADMIN_SCHEMA_VERSION,
        "character": str(character or ""),
        "auto_evolution": {
            "enabled": False,
            "mode": DEFAULT_AUTO_MODE,
            "updated_at": None,
            "actor": "system",
        },
        "decisions": [],
    }


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


def _normalize_auto(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError("persona_admin.auto_evolution: expected an object")
    auto = copy.deepcopy(dict(raw))
    raw_mode = auto.get("mode")
    if raw_mode is None or raw_mode == "":
        # Read-time migration: legacy bool true kept its "propose" meaning.
        mode = "propose" if bool(auto.get("enabled", False)) else "off"
    else:
        mode = _text(raw_mode, 16)
        if mode not in AUTO_MODES:
            raise CorruptStoreError(
                f"persona_admin.auto_evolution.mode: unknown mode {mode!r}",
            )
    auto["mode"] = mode
    auto["enabled"] = mode != "off"
    updated = auto.get("updated_at")
    auto["updated_at"] = _text(updated, 40) or None
    auto["actor"] = _text(auto.get("actor"), 64) or "system"
    return auto


def _normalize_decision(raw: Any, *, index: int = 0) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"persona_admin decision[{index}]: expected an object",
        )
    decision = copy.deepcopy(dict(raw))
    decision_id = _text(decision.get("decision_id"), 120)
    if not decision_id:
        raise CorruptStoreError(
            f"persona_admin decision[{index}]: missing decision_id",
        )
    decision["decision_id"] = decision_id
    decision["proposal_id"] = _text(decision.get("proposal_id"), 200)
    decision["action"] = _text(decision.get("action"), 64) or "unknown"
    decision["status"] = _text(decision.get("status"), 64) or decision["action"]
    decision["actor"] = _text(decision.get("actor"), 64) or "user"
    decision["reason"] = _text(decision.get("reason"), 400)
    decision["at"] = _text(decision.get("at"), 40) or None
    decision["revision_before"] = _int(decision.get("revision_before"), 0)
    decision["revision_after"] = _int(decision.get("revision_after"), 0)
    decision["source_refs"] = _str_list(decision.get("source_refs"))
    return decision


def _normalize_document(raw: Any, character: str) -> dict[str, Any]:
    if raw is None:
        return empty_document(character)
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"persona_admin: expected a JSON object, found {type(raw).__name__}",
        )
    schema = raw.get("schema_version", ADMIN_SCHEMA_VERSION)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise InvalidSchemaError(
            f"persona_admin: missing or non-integer schema_version: {schema!r}",
        )
    if schema != ADMIN_SCHEMA_VERSION:
        raise InvalidSchemaError(
            f"unsupported persona_admin schema {schema}; this build supports "
            f"{ADMIN_SCHEMA_VERSION}",
        )
    document = copy.deepcopy(dict(raw))
    stored_character = _text(document.get("character"), 200)
    if stored_character and stored_character != character:
        raise CorruptStoreError(
            f"persona_admin: character {stored_character!r} does not match "
            f"{character!r}",
        )
    document["schema_version"] = ADMIN_SCHEMA_VERSION
    document["character"] = character
    document["auto_evolution"] = _normalize_auto(
        document.get("auto_evolution", empty_document()["auto_evolution"]),
    )
    decisions = document.get("decisions", [])
    if not isinstance(decisions, list):
        raise CorruptStoreError(
            f"persona_admin.decisions: expected a list, found "
            f"{type(decisions).__name__}",
        )
    normalized = [
        _normalize_decision(item, index=index)
        for index, item in enumerate(decisions)
    ]
    seen: set[str] = set()
    for decision in normalized:
        if decision["decision_id"] in seen:
            raise CorruptStoreError(
                "persona_admin: duplicate decision_id "
                f"{decision['decision_id']!r}",
            )
        seen.add(decision["decision_id"])
    document["decisions"] = normalized
    return document


def _validated_document(document: Any, character: str) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise ValueError("persona_admin: document must be a mapping")
    return _normalize_document(document, character)


def _refs(row: Mapping[str, Any]) -> list[str]:
    refs = row.get("source_refs")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    return [str(item) for item in refs if isinstance(item, str) and item]


class PersonaAdminStore:
    """Per-character admin switch + bounded decision audit store."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        barrier: Any = None,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._barrier = barrier
        self._limit = DEFAULT_DECISION_LIMIT

    # -- paths ----------------------------------------------------------

    def _path(self, character: str, *, create: bool = False) -> Path:
        return self._roots.character_dir(character, create=create) / ADMIN_FILENAME

    @property
    def decision_limit(self) -> int:
        return self._limit

    # -- reads ----------------------------------------------------------

    def load(self, character: str) -> dict[str, Any]:
        """Return the filtered document; corruption raises, never empty."""
        path = self._path(character)
        with _lock_for(path):
            return self._filter(character, self._load_document(character))

    def _load_document(self, character: str) -> dict[str, Any]:
        path = self._path(character)
        try:
            raw = self._store.load(path)
        except CorruptStoreError:
            if is_blank_file(path):
                return empty_document(character)
            raise
        return _normalize_document(raw, character)

    def _filter(self, character: str, document: Mapping[str, Any]) -> dict[str, Any]:
        filtered = copy.deepcopy(dict(document))
        kept: list[dict[str, Any]] = []
        for row in filtered.get("decisions") or []:
            # Invalidated rows are hidden regardless of whether a barrier was
            # injected; the barrier additionally hides deleted sources.
            if row.get("invalidated_at"):
                continue
            if self._barrier is not None:
                refs = _refs(row)
                if refs and any(self._blocked(character, ref) for ref in refs):
                    continue
            kept.append(row)
        filtered["decisions"] = kept
        return filtered

    def _blocked(self, character: str, ref: str) -> bool:
        barrier = self._barrier
        if barrier is None or not ref:
            return False
        for target_type in ("fact", "reflection", "persona", "growth"):
            try:
                if barrier.blocks_target(character, target_type, ref):
                    return True
            except Exception:  # noqa: BLE001 - defensive filter only
                continue
        return False

    # -- writes ---------------------------------------------------------

    def save(self, character: str, document: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically replace the document after permit + barrier filtering."""
        payload = _validated_document(document, character)
        payload = self._filter(character, payload)
        payload = self._trim(payload)
        path = self._path(character)
        with _lock_for(path):
            self._load_document(character)  # refuse to replace a corrupt file
            self._permit.assert_writable("save_persona_admin", str(path))
            self._store.save(path, payload)
        return payload

    def get_mode(self, character: str) -> str:
        """Current mode (off/propose/auto); unknown stored values fail closed."""
        return self.load(character)["auto_evolution"]["mode"]

    def set_mode(
        self, character: str, mode: str, *, actor: str = "user",
    ) -> dict[str, Any]:
        """Persist the single evolution mode and return the auto_evolution row."""
        mode = _text(mode, 16)
        if mode not in AUTO_MODES:
            raise ValueError(f"unknown evolution mode {mode!r}")
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            document["auto_evolution"] = {
                "enabled": mode != "off",
                "mode": mode,
                "updated_at": self._clock.now().isoformat(),
                "actor": _text(actor, 64) or "user",
            }
            saved = self.save(character, document)
            return copy.deepcopy(saved["auto_evolution"])

    def append_decision(
        self, character: str, decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append one decision; a duplicate decision_id is idempotent."""
        row = _normalize_decision(decision)
        if not row.get("at"):
            row["at"] = self._clock.now().isoformat()
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            for existing in document.get("decisions") or []:
                if existing.get("decision_id") == row["decision_id"]:
                    return copy.deepcopy(existing)
            document["decisions"] = list(document.get("decisions") or []) + [row]
            self.save(character, document)
            return row

    # -- decision queries ----------------------------------------------

    def list_decisions(
        self,
        character: str,
        *,
        limit: int | None = None,
        action: str | None = None,
        proposal_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self.load(character).get("decisions") or [])
        if action is not None:
            rows = [row for row in rows if row.get("action") == action]
        if proposal_id is not None:
            rows = [row for row in rows if row.get("proposal_id") == proposal_id]
        rows.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
        if limit is not None and limit >= 0:
            rows = rows[:limit]
        return rows

    def get_decision(
        self, character: str, decision_id: str,
    ) -> dict[str, Any] | None:
        wanted = _text(decision_id, 120)
        if not wanted:
            return None
        for row in self.load(character).get("decisions") or []:
            if row.get("decision_id") == wanted:
                return row
        return None

    def prune(self, character: str) -> int:
        """Trim decisions to the configured bound; returns the removed count."""
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            decisions = list(document.get("decisions") or [])
            if len(decisions) <= self._limit:
                return 0
            removed = len(decisions) - self._limit
            document["decisions"] = decisions[-self._limit:]
            self.save(character, document)
            return removed

    def _trim(self, document: dict[str, Any]) -> dict[str, Any]:
        decisions = list(document.get("decisions") or [])
        if len(decisions) > self._limit:
            document["decisions"] = decisions[-self._limit:]
        return document


__all__ = [
    "ADMIN_FILENAME",
    "ADMIN_SCHEMA_VERSION",
    "AUTO_MODES",
    "DEFAULT_AUTO_MODE",
    "DEFAULT_DECISION_LIMIT",
    "PersonaAdminStore",
    "empty_document",
]
