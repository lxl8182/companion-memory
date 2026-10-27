"""Audit cache for the three working summaries (M5).

A working summary is a *projection*, never a source: the read path always
re-derives it from the authoritative records (published rules, growth and
reflection rows, active facts, companion state). This file exists only so an
operator can see what the last maintenance round computed and whether it is
stale.

Two invariants make that safe:

* nothing in the recall or context path opens this file -- a materially
  different summary can never be served just because a cache was warm;
* the cache records the 'rebuild_key' of the inputs it was built from and the
  deletion/correction versions it observed. A mismatch, or an invalidation
  written by the deletion fan-out, is reported as 'stale' rather than silently
  trusted, and deleting the file costs nothing but the audit trail.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping

from ..ports import AtomicJsonStore, Clock, StorageRoots, WritePermit
from .schema import load_versioned, wrap

WORKING_SUMMARY_FILENAME = "working_summary.json"
WORKING_SUMMARY_SCHEMA = 1

#: One row per section name. Kept as a tuple so a truncated file is detectable.
SECTION_NAMES = ("growth", "important_facts", "open_threads", "episode")

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


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class WorkingSummaryStore:
    """One audit row per (character, subject), rewritten by maintenance."""

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
        return self._roots.character_file(character, WORKING_SUMMARY_FILENAME)

    def load(self, character: str) -> dict[str, Any]:
        document = load_versioned(
            self._store, self._path(character),
            supported=frozenset({WORKING_SUMMARY_SCHEMA}),
            default={"version": 0, "subjects": {}},
        )
        rows = document.get("subjects")
        subjects: dict[str, dict[str, Any]] = {}
        if isinstance(rows, Mapping):
            for key, row in rows.items():
                if not isinstance(row, Mapping):
                    continue
                subjects[str(key)] = self._normalize(dict(row))
        return {
            "schema_version": WORKING_SUMMARY_SCHEMA,
            "version": max(0, _as_int(document.get("version"), 0)),
            "subjects": subjects,
        }

    def save(self, character: str, document: Mapping[str, Any]) -> None:
        path = self._path(character)
        payload = wrap({
            "version": max(0, _as_int(document.get("version"), 0)),
            "subjects": {
                str(key): dict(row)
                for key, row in (document.get("subjects") or {}).items()
                if isinstance(row, Mapping)
            },
        }, version=WORKING_SUMMARY_SCHEMA)
        self._permit.assert_writable("save_working_summary", str(path))
        self._store.save(path, payload)

    def get(self, character: str, subject: str = "private") -> dict[str, Any] | None:
        return self.load(character)["subjects"].get(str(subject or "private"))

    def record(
        self, character: str, subject: str, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Write the audit row for one subject (the only write path)."""
        path = self._path(character)
        subject_key = str(subject or "private")
        with _lock_for(path):
            document = self.load(character)
            row = self._normalize({**dict(payload), "subject": subject_key})
            document["subjects"][subject_key] = row
            document["version"] = _as_int(document.get("version"), 0) + 1
            self.save(character, document)
            return row

    def invalidate(self, character: str, subject: str | None = None, *, reason: str = "") -> int:
        """Mark cached rows stale after a deletion/correction; returns the count.

        The row keeps its content (an operator may want to see what the last
        round produced) but is never reported as current again.
        """
        path = self._path(character)
        now = self._clock.now().isoformat()
        changed = 0
        with _lock_for(path):
            document = self.load(character)
            targets = (
                [str(subject)] if subject else list(document["subjects"])
            )
            for key in targets:
                row = document["subjects"].get(key)
                if row is None or row.get("invalidated_at"):
                    continue
                updated = dict(row)
                updated["invalidated_at"] = now
                updated["invalidated_reason"] = str(reason or "derived_stale")[:200]
                document["subjects"][key] = updated
                changed += 1
            if changed:
                document["version"] = _as_int(document.get("version"), 0) + 1
                self.save(character, document)
        return changed

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        sections: dict[str, dict[str, Any]] = {}
        raw_sections = raw.get("sections")
        if isinstance(raw_sections, Mapping):
            for name in SECTION_NAMES:
                item = raw_sections.get(name)
                if isinstance(item, Mapping):
                    sections[name] = dict(item)
        versions = raw.get("source_versions")
        return {
            "subject": str(raw.get("subject") or "private"),
            "rebuild_key": str(raw.get("rebuild_key") or ""),
            "built_at": raw.get("built_at"),
            "sections": sections,
            "source_versions": dict(versions) if isinstance(versions, Mapping) else {},
            "invalidated_at": raw.get("invalidated_at"),
            "invalidated_reason": raw.get("invalidated_reason"),
        }


__all__ = [
    "SECTION_NAMES",
    "WORKING_SUMMARY_FILENAME",
    "WORKING_SUMMARY_SCHEMA",
    "WorkingSummaryStore",
]
