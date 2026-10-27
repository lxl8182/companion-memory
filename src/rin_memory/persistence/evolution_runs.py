"""C03 background evolution run storage (per-character evolution_runs.json).

One durable record per background trigger attempt. The store never touches the
persona card or the proposal store; it only remembers which evidence set was
already tried, whether it is waiting for a retry/publish, and the bounded
audit of results.

On-disk contract:

    {"schema_version": 1, "character": "demo", "runs": [<run>, ...],
     "pending": ["<run_id>", ...]}

Run row (unknown fields round-trip verbatim):

    run_id / trigger_key / mode / evidence{ids,hashes} / sections[] /
    base_revision / status / reason / attempts / cooldown_until /
    created_at / updated_at / proposal_id / published_version

status is one of triggered|proposed|accepted|published|rejected|failed|skipped.
The list is bounded (default 64); prune evicts the oldest terminal run first and
never drops a run still listed in pending. A save refuses to replace a corrupt
file its reader rejects; unknown stored modes fail closed.

Writes follow WritePermit.assert_writable -> deletion-barrier filter ->
AtomicJsonStore.save. A run whose every evidence id is tombstoned becomes
skipped/source_deleted and leaves pending; terminal runs never resurrect.
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
from .persona_admin import AUTO_MODES
from .shards import is_blank_file

RUNS_FILENAME = "evolution_runs.json"
RUNS_SCHEMA_VERSION = 1
DEFAULT_RUN_LIMIT = 64

RUN_TRIGGERED = "triggered"
RUN_PROPOSED = "proposed"
RUN_ACCEPTED = "accepted"
RUN_PUBLISHED = "published"
RUN_REJECTED = "rejected"
RUN_FAILED = "failed"
RUN_SKIPPED = "skipped"

RUN_STATUSES = frozenset({
    RUN_TRIGGERED, RUN_PROPOSED, RUN_ACCEPTED, RUN_PUBLISHED,
    RUN_REJECTED, RUN_FAILED, RUN_SKIPPED,
})
#: Evictable without losing a retry: published/rejected/skipped only.
TERMINAL_RUN_STATUSES = frozenset({RUN_PUBLISHED, RUN_REJECTED, RUN_SKIPPED})

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
        "schema_version": RUNS_SCHEMA_VERSION,
        "character": str(character or ""),
        "runs": [],
        "pending": [],
    }


def _text(value: Any, limit: int = 0) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value[:limit] if limit else value


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


def _normalize_run(raw: Any, *, index: int = 0) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"evolution_runs.run[{index}]: expected an object",
        )
    run = copy.deepcopy(dict(raw))
    run_id = _text(run.get("run_id"), 200)
    if not run_id:
        raise CorruptStoreError(f"evolution_runs.run[{index}]: missing run_id")
    status = _text(run.get("status"))
    if status not in RUN_STATUSES:
        raise CorruptStoreError(
            f"evolution_runs {run_id!r}: invalid status {status!r}",
        )
    mode = _text(run.get("mode"), 16) or "off"
    if mode not in AUTO_MODES:
        raise CorruptStoreError(
            f"evolution_runs {run_id!r}: unknown mode {mode!r}",
        )
    run["run_id"] = run_id
    run["status"] = status
    run["mode"] = mode
    run["trigger_key"] = _text(run.get("trigger_key"), 128)
    evidence = run.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    run["evidence"] = {
        "ids": _str_list(evidence.get("ids")),
        "hashes": _str_list(evidence.get("hashes")),
    }
    run["sections"] = _str_list(run.get("sections"))
    run["base_revision"] = _int(run.get("base_revision"), 0)
    run["reason"] = _text(run.get("reason"), 400)
    run["attempts"] = max(0, _int(run.get("attempts"), 0))
    run["cooldown_until"] = _text(run.get("cooldown_until"), 40) or None
    run["created_at"] = _text(run.get("created_at"), 40)
    run["updated_at"] = _text(run.get("updated_at"), 40) or run["created_at"]
    run["proposal_id"] = _text(run.get("proposal_id"), 200)
    run["published_version"] = _text(run.get("published_version"), 120)
    return run


def _normalize_document(raw: Any, character: str) -> dict[str, Any]:
    if raw is None:
        return empty_document(character)
    if not isinstance(raw, Mapping):
        raise CorruptStoreError(
            f"evolution_runs: expected a JSON object, found {type(raw).__name__}",
        )
    schema = raw.get("schema_version", RUNS_SCHEMA_VERSION)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise InvalidSchemaError(
            f"evolution_runs: missing or non-integer schema_version: {schema!r}",
        )
    if schema != RUNS_SCHEMA_VERSION:
        raise InvalidSchemaError(
            f"unsupported evolution_runs schema {schema}; this build supports "
            f"{RUNS_SCHEMA_VERSION}",
        )
    document = copy.deepcopy(dict(raw))
    stored_character = _text(document.get("character"), 200)
    if stored_character and stored_character != character:
        raise CorruptStoreError(
            f"evolution_runs: character {stored_character!r} does not match "
            f"{character!r}",
        )
    document["schema_version"] = RUNS_SCHEMA_VERSION
    document["character"] = character
    runs = document.get("runs")
    if runs is None:
        runs = []
    if not isinstance(runs, list):
        raise CorruptStoreError(
            "evolution_runs.runs: expected a list, "
            f"found {type(runs).__name__}",
        )
    normalized = [_normalize_run(item, index=index)
                  for index, item in enumerate(runs)]
    seen: set[str] = set()
    for run in normalized:
        if run["run_id"] in seen:
            raise CorruptStoreError(
                f"evolution_runs: duplicate run_id {run['run_id']!r}",
            )
        seen.add(run["run_id"])
    document["runs"] = normalized
    pending = document.get("pending")
    if pending is None:
        pending = []
    if not isinstance(pending, list):
        raise CorruptStoreError(
            "evolution_runs.pending: expected a list, "
            f"found {type(pending).__name__}",
        )
    document["pending"] = [
        item for item in _str_list(pending) if item in seen
    ]
    return document


def _evidence_ids(run: Mapping[str, Any]) -> list[str]:
    evidence = run.get("evidence")
    if not isinstance(evidence, Mapping):
        return []
    return _str_list(evidence.get("ids"))


def _blocked(barrier: Any, character: str, ref: str) -> bool:
    if barrier is None:
        return False
    for target_type in ("fact", "reflection", "growth", "persona"):
        try:
            if barrier.blocks_target(character, target_type, ref):
                return True
        except Exception:  # noqa: BLE001 - defensive filter only
            continue
    return False


class EvolutionRunStore:
    """Bounded per-character trigger/pending run store."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        barrier: Any = None,
        limit: int = DEFAULT_RUN_LIMIT,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._barrier = barrier
        self._limit = max(1, int(limit or DEFAULT_RUN_LIMIT))

    def _path(self, character: str, *, create: bool = False) -> Path:
        return self._roots.character_dir(character, create=create) / RUNS_FILENAME

    @property
    def limit(self) -> int:
        return self._limit

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
        if self._barrier is None:
            return filtered
        now = self._clock.now().isoformat()
        for run in filtered.get("runs") or []:
            if run.get("status") in TERMINAL_RUN_STATUSES:
                continue
            ids = _evidence_ids(run)
            if not ids or not all(_blocked(self._barrier, character, ref) for ref in ids):
                continue
            run["status"] = RUN_SKIPPED
            run["reason"] = "source_deleted"
            run["updated_at"] = now
            if run.get("run_id") in filtered.get("pending") or []:
                filtered["pending"] = [
                    item for item in filtered["pending"]
                    if item != run.get("run_id")
                ]
        return filtered

    def load(self, character: str) -> dict[str, Any]:
        with _lock_for(self._path(character)):
            return self._filter(character, self._load_document(character))

    def find_trigger(
        self, character: str, trigger_key: str,
    ) -> dict[str, Any] | None:
        wanted = _text(trigger_key, 128)
        if not wanted:
            return None
        for run in self.load(character).get("runs") or []:
            if run.get("trigger_key") == wanted:
                return run
        return None

    def list(
        self,
        character: str,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        runs = list(self.load(character).get("runs") or [])
        if status is not None:
            runs = [run for run in runs if run.get("status") == status]
        runs.sort(key=lambda run: str(run.get("created_at") or ""), reverse=True)
        if limit is not None and limit >= 0:
            runs = runs[:limit]
        return runs

    def pending(self, character: str) -> list[dict[str, Any]]:
        document = self.load(character)
        by_id = {run.get("run_id"): run for run in document.get("runs") or []}
        return [
            copy.deepcopy(by_id[run_id])
            for run_id in document.get("pending") or []
            if run_id in by_id
        ]

    def _write_document(
        self, character: str, document: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = _normalize_document(document, character)
        payload = self._filter(character, payload)
        path = self._path(character)
        with _lock_for(path):
            self._load_document(character)  # refuse to replace a corrupt file
            self._permit.assert_writable("save_evolution_runs", str(path))
            self._store.save(path, payload)
        return payload

    def save(self, character: str, document: Mapping[str, Any]) -> dict[str, Any]:
        payload = _normalize_document(document, character)
        payload = self._prune_document(payload)
        self._enforce_limit(payload)
        return self._write_document(character, payload)

    def append(self, character: str, run: Mapping[str, Any]) -> dict[str, Any]:
        """Append one run; an existing trigger_key is idempotent."""
        row = _normalize_run(run)
        now = self._clock.now().isoformat()
        if not row["created_at"]:
            row["created_at"] = now
        if not row["updated_at"]:
            row["updated_at"] = row["created_at"]
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            if row["trigger_key"]:
                for existing in document.get("runs") or []:
                    if existing.get("trigger_key") == row["trigger_key"]:
                        return existing
            document["runs"] = list(document.get("runs") or []) + [row]
            document = self._prune_document(document)
            self._enforce_limit(document)
            self._write_document(character, document)
            return self.get(character, row["run_id"]) or row

    def get(self, character: str, run_id: str) -> dict[str, Any] | None:
        wanted = _text(run_id, 200)
        if not wanted:
            return None
        for run in self.load(character).get("runs") or []:
            if run.get("run_id") == wanted:
                return run
        return None

    def update(
        self, character: str, run_id: Any,
        fields: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any] | None:
        if isinstance(run_id, Mapping):
            fields = run_id
            run_id = fields.get("run_id")
        if extra:
            merged_fields = dict(fields or {})
            merged_fields.update(extra)
            fields = merged_fields
        wanted = _text(run_id, 200)
        if not wanted or not isinstance(fields, Mapping):
            return None
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            target: dict[str, Any] | None = None
            for run in document.get("runs") or []:
                if run.get("run_id") == wanted:
                    target = run
                    break
            if target is None:
                return None
            merged = copy.deepcopy(target)
            merged.update(copy.deepcopy(dict(fields)))
            merged["run_id"] = wanted
            merged["updated_at"] = self._clock.now().isoformat()
            normalized = _normalize_run(merged)
            document["runs"] = [
                normalized if run.get("run_id") == wanted else run
                for run in document.get("runs") or []
            ]
            if normalized["status"] in TERMINAL_RUN_STATUSES:
                document["pending"] = [
                    item for item in document.get("pending") or []
                    if item != wanted
                ]
            self._write_document(character, document)
            return self.get(character, wanted) or normalized

    def mark_pending(self, character: str, run_id: str) -> bool:
        wanted = _text(run_id, 200)
        if not wanted:
            return False
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            if not any(run.get("run_id") == wanted for run in document.get("runs") or []):
                return False
            if wanted not in (document.get("pending") or []):
                document["pending"] = list(document.get("pending") or []) + [wanted]
                self._write_document(character, document)
            return True

    def clear_pending(self, character: str, run_id: str) -> bool:
        wanted = _text(run_id, 200)
        if not wanted:
            return False
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            pending = [item for item in document.get("pending") or [] if item != wanted]
            if len(pending) == len(document.get("pending") or []):
                return False
            document["pending"] = pending
            self._write_document(character, document)
            return True

    def prune(self, character: str, *, limit: int | None = None) -> int:
        target = self._limit if limit is None else max(1, int(limit))
        with _lock_for(self._path(character)):
            document = self._load_document(character)
            before = len(document.get("runs") or [])
            document = self._prune_document(document, limit=target)
            removed = before - len(document.get("runs") or [])
            if removed:
                self._write_document(character, document)
            else:
                self._enforce_limit(document, limit=target)
            return removed

    def _prune_document(
        self, document: dict[str, Any], *, limit: int | None = None,
    ) -> dict[str, Any]:
        bound = self._limit if limit is None else max(1, int(limit))
        runs = list(document.get("runs") or [])
        if len(runs) <= bound:
            return document
        pending = set(document.get("pending") or [])
        evictable = [
            run for run in runs
            if run.get("status") in TERMINAL_RUN_STATUSES
            and run.get("run_id") not in pending
        ]
        evictable.sort(key=lambda run: str(
            run.get("updated_at") or run.get("created_at") or "",
        ))
        evict_ids = {
            run.get("run_id")
            for run in evictable[: max(0, len(runs) - bound)]
        }
        document["runs"] = [
            run for run in runs if run.get("run_id") not in evict_ids
        ]
        document["pending"] = [
            item for item in document.get("pending") or []
            if item not in evict_ids
        ]
        return document

    def _enforce_limit(
        self, document: Mapping[str, Any], *, limit: int | None = None,
    ) -> None:
        bound = self._limit if limit is None else max(1, int(limit))
        if len(document.get("runs") or []) > bound:
            raise StoreError(
                "evolution run limit reached; no terminal run can be evicted",
                code="run_limit",
            )


__all__ = [
    "DEFAULT_RUN_LIMIT",
    "EvolutionRunStore",
    "RUNS_FILENAME",
    "RUNS_SCHEMA_VERSION",
    "RUN_ACCEPTED",
    "RUN_FAILED",
    "RUN_PROPOSED",
    "RUN_PUBLISHED",
    "RUN_REJECTED",
    "RUN_SKIPPED",
    "RUN_STATUSES",
    "RUN_TRIGGERED",
    "TERMINAL_RUN_STATUSES",
    "empty_document",
]
