"""Source-bound interest observations (`interest_signals.json`), RO03.

An interest signal is *evidence about this person's preferences*, not a second
fact store and not a generated profile: every row cites the user utterances it
came from, and it disappears when those sources do. The projection built on top
(domain/profile.py) is the only thing readers see, so a deleted or corrected
source can never leave a preference behind.

On-disk shape is the core versioned envelope, one file per character:

    {"schema_version": 1,
     "version": <int>,          # data version, bumped by every write
     "items": {"<signal id>": {<row>}, ...}}

The *data* version is what makes "same generation turn, same snapshot; a new
turn sees the new version" auditable: a reader reports the version it read and a
writer only ever moves it forward.

Write path: journal first (`core.interest_signals_updated`), then the view, so a
crash between the two is repaired by replay instead of leaving a signal that the
journal claims exists. Replay is a pure merge by id and is therefore idempotent:
a row may arrive twice and the version never moves backwards. Every write passes
the injected WritePermit first.

Read path is strict, like the other core stores: a missing file is a fresh
store; unparsable JSON, a non-object envelope, a missing `items` mapping or a
row that is not an object raises instead of being read as empty. Silently
emptying this file would drop recorded preferences and let the next session
rediscover them as if they had never been withdrawn.

All methods are synchronous blocking IO; callers run them through
`asyncio.to_thread` where the caller is async (extraction already does).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import CorruptStoreError
from ..ports import AtomicJsonStore, EventJournal, StorageRoots, WritePermit
from .schema import load_versioned, wrap

#: File name inside `<character_dir>`.
SIGNAL_FILENAME = "interest_signals.json"
#: Journal event type; the vocabulary entry lives in persistence/journal.py.
SIGNAL_EVENT = "core.interest_signals_updated"

#: Empty document used when the file does not exist yet.
_EMPTY: dict[str, Any] = {"version": 0, "items": {}}


class InterestSignalStore:
    """Per-character signal rows plus their data version (single writer)."""

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        permit: WritePermit,
        journal: EventJournal,
    ) -> None:
        self._roots = roots
        self._store = store
        self._permit = permit
        self._journal = journal
        # Register the replay handler before the first write: an entry this
        # build writes must be one the reconciler can repair (B02 order).
        journal.register_handler(SIGNAL_EVENT, self.replay)

    # -- paths -----------------------------------------------------------

    def path(self, character: str) -> Path:
        """`<character_dir>/interest_signals.json` (created on write only)."""
        return self._roots.character_file(character, SIGNAL_FILENAME)

    # -- reads -----------------------------------------------------------

    def load(self, character: str) -> dict[str, Any]:
        """Return `{"version": int, "items": {id: row}}`; never writes."""
        path = self.path(character)
        document = load_versioned(
            self._store, path, default=dict(_EMPTY),
        )
        items = document.get("items")
        if not isinstance(items, Mapping):
            raise CorruptStoreError(
                "interest_signals: 'items' must be an object of rows", path=path,
            )
        rows: dict[str, dict[str, Any]] = {}
        for key, row in items.items():
            if not isinstance(key, str) or not isinstance(row, dict):
                raise CorruptStoreError(
                    f"interest_signals: row {key!r} must be an object", path=path,
                )
            rows[key] = dict(row)
        version = document.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise CorruptStoreError(
                f"interest_signals: 'version' must be a non-negative integer, "
                f"found {version!r}", path=path,
            )
        return {"version": version, "items": rows}

    # -- writes ----------------------------------------------------------

    def save(self, character: str, document: Mapping[str, Any]) -> None:
        """Atomically replace the file under the write permit."""
        path = self.path(character)
        self._permit.assert_writable("save_interest_signals", str(path))
        self._store.save(path, wrap(document))

    @staticmethod
    def apply(document: dict[str, Any], payload: Mapping[str, Any]) -> None:
        """Merge one payload into a loaded document (replay and live write).

        Idempotent by construction: rows are keyed by id, and the version only
        ever moves forward, so replaying an entry that was already applied is a
        no-op rather than a second generation of the same fact.
        """
        items = document.setdefault("items", {})
        if not isinstance(items, dict):
            items = {}
            document["items"] = items
        for row in payload.get("items") or ():
            if isinstance(row, Mapping) and row.get("id"):
                items[str(row["id"])] = dict(row)
        version = payload.get("version")
        if isinstance(version, int) and not isinstance(version, bool):
            document["version"] = max(int(document.get("version") or 0), version)

    def put(self, character: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Journal then persist the rows that actually changed; returns the count.

        A row identical to the stored one is not rewritten, so a repeated
        extraction of the same utterance costs no version bump and cannot make a
        mid-turn snapshot look stale.
        """
        document = self.load(character)
        stored = document["items"]
        changed: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_id = str(row.get("id") or "")
            if not row_id:
                continue
            candidate = dict(row)
            if stored.get(row_id) != candidate:
                changed.append(candidate)
        if not changed:
            return 0
        payload = {"items": changed, "version": int(document["version"]) + 1}
        self._journal.record_and_save(
            character,
            SIGNAL_EVENT,
            payload,
            load_view=self.load,
            mutate_view=lambda view: self.apply(view, payload),
            save_view=lambda name, view: self.save(name, view),
        )
        return len(changed)

    def replay(self, character: str, payload: Mapping[str, Any]) -> bool:
        """Rebuild the view from one journal entry (crash repair)."""
        document = self.load(character)
        self.apply(document, payload)
        self.save(character, document)
        return True


__all__ = ["SIGNAL_EVENT", "SIGNAL_FILENAME", "InterestSignalStore"]
