"""Deletion/correction cleaners for whole-turn request results (S02/R01).

R01 found that a real `forget()` left the committed turn result intact: the
request ledger had an `invalidate_sources()` method, but nothing called it, so a
`GET /v1/requests/{id}` kept returning `committed` with the deleted reply body.
The review also noted that the tests which "passed" invoked the invalidator
directly, bypassing the real path.

This module is the missing wiring. It is a normal deletion cleaner (the same
shape as `RecentDeletionCleaner`), so it runs inside the engine's own step
sequence and reports per-step status like every other cleaner.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..persistence.turns import TurnRequestStore


class TurnResultCleaner:
    """Invalidate committed turn results whose source a deletion removed."""

    def __init__(self, turns: TurnRequestStore) -> None:
        self._turns = turns

    def cleanup(
        self, character: str, resolved: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Clear reply bodies for every affected source; keep identifiers.

        Reads both key spellings the engine uses (`fact_source_keys` for the
        facts a deletion resolved to, and `source_keys` from the request), so a
        deletion that names only one of them still invalidates the result.
        """
        source_keys: set[str] = set()
        for name in ("fact_source_keys", "source_keys"):
            for value in resolved.get(name) or ():
                if isinstance(value, str) and value.strip():
                    source_keys.add(value.strip())
        if not source_keys:
            return {
                "status": "completed",
                "detail": {"invalidated": 0, "reason": "no_source_keys"},
            }
        changed = self._turns.invalidate_by_source(
            character, source_keys, reason="deleted_source",
        )
        return {
            "status": "completed",
            "detail": {
                "invalidated": len(changed),
                "request_ids": list(changed)[:20],
            },
        }


class CorrectionTurnInvalidator:
    """Invalidate turn results whose source a correction replaced (§3.3).

    A correction is not a deletion: the messages stay, but a reply generated
    from the superseded understanding must not be served as if it were still
    current. The body is cleared and the request marked deleted -- the same
    contract a deletion uses (410, no stale text).

    Corrections fan out by **fact id**, so the affected request records are
    found through the fact rows they were generated from; a request whose
    source key matches also counts. Matching only on source keys would miss
    the common case where the correction names a fact the turn produced.
    """

    def __init__(self, turns: TurnRequestStore, facts: Any = None) -> None:
        self._turns = turns
        self._facts = facts

    def invalidate(self, character: str, fact_ids, **_: Any) -> dict[str, Any]:
        wanted = {str(item) for item in fact_ids or () if str(item).strip()}
        if not wanted:
            return {"invalidated": 0}
        source_keys = self._source_keys_for(character, wanted)
        if not source_keys:
            return {"invalidated": 0, "reason": "no_source_keys_for_facts"}
        changed = self._turns.invalidate_by_source(
            character, source_keys, reason="corrected_source",
        )
        return {"invalidated": len(changed), "request_ids": list(changed)[:20]}

    def _source_keys_for(self, character: str, fact_ids: set[str]) -> set[str]:
        """Source keys of the given facts (and their mirrors)."""
        if self._facts is None:
            return set()
        try:
            rows = self._facts.load(character) or ()
        except Exception:  # noqa: BLE001 - a broken store must not block the step
            return set()
        keys: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("id") or "") not in fact_ids:
                continue
            for value in row.get("source_event_keys") or ():
                if isinstance(value, str) and value.strip():
                    keys.add(value.strip())
        return keys


__all__ = ["CorrectionTurnInvalidator", "TurnResultCleaner"]
