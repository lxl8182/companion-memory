"""Per-character event journal with position-based replay recovery (B02).

events.ndjson is an append-only log: every state transition appends one JSON
object *before* the view it changes is saved, so a crash between the two leaves a
replayable tail instead of a silent lost update. events_applied.json is the
replay marker: the position of the last entry whose handler returned successfully.

Host-compatible line format (one JSON object per line)::

    {"event_id": "<uuid4>", "type": "<event type>", "ts": "<ISO-8601 with offset>",
     "position": <int>, "payload": {...}}

Host logs written before B02 carry no position; on read a record without one gets
its 0-based line index, and the next append uses max(existing position + 1, line
count) so old and new logs agree.

B02 fix for the host's known limitation (design 17.4, 17.13 item 1): the host
record_and_save wrote the id of the line it had just appended as the sentinel
unconditionally, claiming that everything before it had been applied. That claim
is false whenever replay has paused and left an unapplied tail on disk
(unregistered type, raising handler); the next live write then silently orphans
that tail forever. Here the marker is a *position*, and record_and_save advances
it only when it was contiguous with the appended entry
(sentinel.position == entry.position - 1). An unapplied tail therefore stays
behind the marker and the next reconcile replays it.

An interrupted append can leave an unterminated final fragment. The read path
ignores it (see _read_records), and the next append heals the file atomically:
a fragment that still parses as a JSON object is kept (it is a record this build
did write) and only gets its missing newline, while partial bytes are dropped.
No operator edit is needed before the character can write again.

Compaction is implemented but deliberately not scheduled here: B07's maintenance
loop owns the decision to call compact_if_needed (design 17.6.3 requires that
decision to be explicit, because the host never scheduled it and the log grew
without bound).

All methods perform blocking file IO; call them through asyncio.to_thread.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

from ..contracts import JournalEntry, RecoveryResult, SentinelState
from ..errors import (
    CorruptStoreError,
    RecoveryConflictError,
    UnregisteredEventTypeError,
)
from ..ports import (
    AtomicJsonStore,
    Clock,
    JournalApplyHandler,
    StorageRoots,
    WritePermit,
)
from .schema import SCHEMA_VERSION
from .version_cache import file_signature

#: The 17 event names frozen by the host (memory/event_log.py:72-113), verbatim.
#: They are persisted identifiers: renaming one would make existing logs
#: unreplayable. B02 keeps the vocabulary even though only core.* types are
#: written; the unused names stay for compatibility with host-written logs.
HOST_EVENT_TYPES: frozenset[str] = frozenset({
    "fact.added",
    "fact.absorbed",
    "fact.archived",
    "reflection.synthesized",
    "reflection.state_changed",
    "reflection.surfaced",
    "reflection.rebutted",
    "persona.fact_added",
    "persona.fact_mentioned",
    "persona.suppressed",
    "correction.queued",
    "correction.resolved",
    "reflection.evidence_updated",
    "persona.evidence_updated",
    "persona.entry_updated",
    "companion.state_committed",
    "memory.deletion_registered",
})

#: Event types this build can both write and replay. B02 opened
#: `core.source_recorded`; B03 adds `core.correction_recorded` (the correction
#: write is journaled so a crash between the record and its propagation is
#: replayed and repairable); S02 adds `core.turn_committed` (the whole-turn
#: commit event that repairs a request result after a crash).
CORE_EVENT_TYPES: frozenset[str] = frozenset({
    "core.source_recorded",
    "core.correction_recorded",
    "core.turn_committed",
    "service.consumer_receipt",
})

#: Default write vocabulary: host names (read/replay only) plus core names.
KNOWN_EVENT_TYPES: frozenset[str] = HOST_EVENT_TYPES | CORE_EVENT_TYPES

_EVENTS_FILE = "events.ndjson"
_SENTINEL_FILE = "events_applied.json"
_COMPACT_LINES_THRESHOLD = 10_000
_COMPACT_AGE_THRESHOLD = timedelta(days=90)
#: Bounded re-derivations after the marker moves under an in-flight replay round.
_MAX_RESCANS = 8


@dataclass(frozen=True)
class _JournalScan:
    """Cheap summary of one journal body, valid while its file is unchanged.

    `count`/`max_position` are exactly what a full parse would produce, so the
    cached path assigns the same next position and never claims a tail it does
    not have. A cold entry runs the strict parse (and therefore reports a
    damaged line exactly as before).
    """

    signature: tuple[int, int, int, int] | None
    count: int
    max_position: int


class EventJournal:
    """Per-character journal: append, replay-into-views, and compaction.

    Implements the ports.EventJournal protocol. Every mutation goes through the
    injected WritePermit; read-modify-write cycles hold one threading.Lock per
    character (there is no global data lock and no await while holding it).
    """

    def __init__(
        self,
        *,
        roots: StorageRoots,
        store: AtomicJsonStore,
        clock: Clock,
        permit: WritePermit,
        event_types: Collection[str] = KNOWN_EVENT_TYPES,
    ) -> None:
        self._roots = roots
        self._store = store
        self._clock = clock
        self._permit = permit
        self._event_types = frozenset(event_types)
        self._handlers: dict[str, JournalApplyHandler] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        #: Per-character (line count, max position) keyed by file signature.
        #: `append` needs the next position and `reconcile` needs to know whether
        #: an unapplied tail exists at all; re-parsing a long journal for that
        #: cost 180 ms per call on a 15 MB / 1.1k-line log (design §17.6).
        self._scans: dict[str, _JournalScan] = {}
        self._scans_guard = threading.Lock()

    # -- paths and locks -------------------------------------------------

    def _lock_for(self, character: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.Lock()
                self._locks[character] = lock
            return lock

    def _events_path(self, character: str, *, create: bool = True) -> Path:
        return self._roots.character_dir(character, create=create) / _EVENTS_FILE

    def _sentinel_path(self, character: str, *, create: bool = True) -> Path:
        return self._roots.character_dir(character, create=create) / _SENTINEL_FILE

    # -- reading ---------------------------------------------------------

    @staticmethod
    def _read_text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CorruptStoreError(
                f"journal is unreadable: {exc}", path=path, cause=exc,
            ) from exc

    def _read_records(
        self, character: str,
    ) -> tuple[list[tuple[int, dict[str, Any]]], bool]:
        """Parse the journal body; returns ([(line index, record)], torn tail?).

        A missing file is an empty journal. A torn final line (unparsable and
        the file does not end with a newline) is ignored: the append that wrote
        it never returned, so no caller was ever told it landed, and replaying
        the acknowledged prefix is safe. Any other unparsable or malformed line
        raises CorruptStoreError; the file is never rewritten.
        """
        path = self._events_path(character, create=False)
        text = self._read_text(path)
        if not text:
            return [], False
        lines = text.split("\n")
        torn = not text.endswith("\n")
        if not torn:
            lines.pop()
        records: list[tuple[int, dict[str, Any]]] = []
        for index, raw in enumerate(lines):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                if torn and index == len(lines) - 1:
                    continue
                raise CorruptStoreError(
                    f"journal line {index + 1} is not valid JSON: {exc.msg}",
                    path=path, cause=exc,
                ) from exc
            if not isinstance(record, dict):
                raise CorruptStoreError(
                    f"journal line {index + 1} is not a JSON object", path=path,
                )
            records.append((index, record))
        return records, torn

    def _heal_torn_tail(self, character: str) -> None:
        """Atomically terminate or drop an unterminated final fragment.

        Called by the write path only, under the per-character lock and after
        the permit check. The append that produced the fragment never returned,
        so no caller was ever told it landed: if the fragment still parses as a
        JSON object it is a record this build did write, so it is kept and only
        the missing newline is added; otherwise the partial bytes are dropped.
        Complete records keep their exact bytes, positions and event ids. The
        read path already ignores the fragment, so no reader can observe a
        half-healed file.
        """
        path = self._events_path(character, create=False)
        if self._has_torn_tail(path):
            text = self._read_text(path)
            if text and not text.endswith("\n"):
                self._heal_text(path, text)

    @staticmethod
    def _has_torn_tail(path: Path) -> bool:
        """Last-byte check; a 15 MB journal must not be re-read to answer this."""
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if size == 0:
                    return False
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) != b"\n"
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CorruptStoreError(
                f"journal is unreadable: {exc}", path=path, cause=exc,
            ) from exc

    def _heal_text(self, path: Path, text: str) -> None:
        fragment = text.rsplit("\n", 1)[-1]
        try:
            parsed = json.loads(fragment)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            self._store.save_text(path, text + "\n")
        else:
            self._store.save_text(path, text[: len(text) - len(fragment)])

    def _entry(
        self, index: int, record: Mapping[str, Any], path: Path,
    ) -> JournalEntry:
        event_id = record.get("event_id")
        event_type = record.get("type")
        if not isinstance(event_id, str) or not event_id.strip():
            raise CorruptStoreError(
                f"journal line {index + 1} is missing a nonempty event_id",
                path=path,
            )
        if not isinstance(event_type, str) or not event_type.strip():
            raise CorruptStoreError(
                f"journal line {index + 1} is missing a nonempty type", path=path,
            )
        position = record.get("position", index)
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise CorruptStoreError(
                f"journal line {index + 1} has an invalid position {position!r}",
                path=path,
            )
        payload = record.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise CorruptStoreError(
                f"journal line {index + 1} payload is not a JSON object", path=path,
            )
        return JournalEntry(
            position=position,
            event_id=event_id,
            event_type=event_type,
            payload=dict(payload),
            recorded_at=self._parse_ts(record.get("ts")),
        )

    @staticmethod
    def _parse_ts(raw: Any) -> datetime | None:
        """Parse the informational ts; a bad value is metadata, not corruption."""
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _entries(self, character: str) -> tuple[JournalEntry, ...]:
        path = self._events_path(character, create=False)
        records, _ = self._read_records(character)
        return tuple(self._entry(index, record, path) for index, record in records)

    def read_since(self, character: str, position: int) -> tuple[JournalEntry, ...]:
        """Entries with position greater than the argument, in journal order.

        A missing journal is empty. A torn final line is skipped (see
        _read_records); an unparsable line anywhere else raises
        CorruptStoreError and leaves the file untouched.
        """
        with self._lock_for(character):
            return self._read_since_unlocked(character, position)

    def _read_since_unlocked(
        self, character: str, position: int,
    ) -> tuple[JournalEntry, ...]:
        path = self._events_path(character, create=False)
        # An up-to-date marker means there is no tail to replay; the summary is
        # derived from the same strict parse, so this skips the work without
        # hiding a damaged line (a cold/unreadable file still parses and raises).
        if position >= self._scan(character, path).max_position:
            return ()
        selected = [
            entry for entry in self._entries(character) if entry.position > position
        ]
        selected.sort(key=lambda entry: entry.position)
        return tuple(selected)

    def count(self, character: str) -> int:
        """Number of journal records; used by maintenance to size the log."""
        with self._lock_for(character):
            return self._scan(
                character, self._events_path(character, create=False),
            ).count

    # -- body scans ------------------------------------------------------

    def _scan(self, character: str, path: Path) -> _JournalScan:
        """Line count and highest position, cached by file signature.

        A cold entry (or any changed file) runs the full strict parse through
        `_read_records`, so a damaged line still raises exactly as before; only
        repeat lookups of an unchanged file are answered from the summary. The
        summary is what `append` uses to choose the next position, so it must
        match the parse: it is taken from the same entries.
        """
        signature = file_signature(path)
        with self._scans_guard:
            cached = self._scans.get(character)
        if cached is not None and cached.signature == signature:
            return cached
        records, _ = self._read_records(character)
        max_position = -1
        for index, record in records:
            entry = self._entry(index, record, path)
            if entry.position > max_position:
                max_position = entry.position
        scan = _JournalScan(
            signature=signature, count=len(records), max_position=max_position,
        )
        # Do not cache when the file changed while it was being read: that
        # summary would describe neither the old nor the new body.
        if signature is not None and file_signature(path) == signature:
            with self._scans_guard:
                self._scans[character] = scan
        return scan

    def _remember_scan(
        self, character: str, path: Path, *, count: int, max_position: int,
    ) -> None:
        """Record a summary this process just produced by writing the journal."""
        signature = file_signature(path)
        if signature is None:
            return
        with self._scans_guard:
            self._scans[character] = _JournalScan(
                signature=signature, count=count, max_position=max_position,
            )

    # -- writing ---------------------------------------------------------

    def register_handler(
        self, event_type: str, handler: JournalApplyHandler,
    ) -> None:
        """Register (or replace) the sync, idempotent, full-snapshot replay handler.

        Handlers must persist their own view before returning; the sentinel
        advances only after a successful return, so a crash in between simply
        replays the entry again.
        """
        if not callable(handler):
            raise TypeError("journal handler must be callable")
        self._handlers[event_type] = handler

    def append(
        self, character: str, event_type: str, payload: Mapping[str, Any],
    ) -> JournalEntry:
        """Append and fsync one entry. Does not advance the sentinel by itself."""
        self._permit.assert_writable(
            "journal.append", str(self._events_path(character, create=False)),
        )
        with self._lock_for(character):
            return self._append_unlocked(character, event_type, payload)

    def _append_unlocked(
        self, character: str, event_type: str, payload: Mapping[str, Any],
    ) -> JournalEntry:
        if not isinstance(payload, Mapping):
            raise ValueError("journal payload must be a mapping")
        self._require_replayable(event_type)
        path = self._events_path(character, create=False)
        self._heal_torn_tail(character)
        scan = self._scan(character, path)
        # Same rule as before: line count, and one past the highest position
        # (a host log written before B02 carries no positions at all).
        position = max(scan.count, scan.max_position + 1)
        recorded_at = self._clock.now()
        record = {
            "event_id": str(uuid.uuid4()),
            "type": event_type,
            "ts": recorded_at.isoformat(),
            "position": position,
            "payload": dict(payload),
        }
        self._store.append_line(
            self._events_path(character), json.dumps(record, ensure_ascii=False),
        )
        self._remember_scan(
            character, path,
            count=scan.count + 1,
            max_position=max(scan.max_position, position),
        )
        return JournalEntry(
            position=position,
            event_id=record["event_id"],
            event_type=event_type,
            payload=dict(payload),
            recorded_at=recorded_at,
        )

    def _require_replayable(self, event_type: str) -> None:
        """Refuse a write the reconciler could not replay (design 17.4)."""
        if event_type not in self._event_types:
            raise UnregisteredEventTypeError(
                f"event type {event_type!r} is not part of this journal's vocabulary"
            )
        if event_type not in self._handlers:
            raise UnregisteredEventTypeError(
                f"event type {event_type!r} has no registered replay handler; "
                "refusing to write an event the reconciler could not repair"
            )

    def record_and_save(
        self,
        character: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        load_view: Callable[[str], Any],
        mutate_view: Callable[[Any], None],
        save_view: Callable[[str, Any], None],
        validate_view: Callable[[Any], None] | None = None,
    ) -> JournalEntry:
        """load view -> validate -> append (fsync) -> mutate -> save -> advance.

        The sentinel advances only when it was contiguous with the appended
        entry (sentinel.position == entry.position - 1). If an unapplied tail
        exists the marker is left alone so the next reconcile replays it; this
        is the B02 fix for the host's unconditional "park at the end".
        """
        with self._permit.transaction(
            "journal.record_and_save", str(self._events_path(character, create=False)),
        ):
            with self._lock_for(character):
                sentinel = self._read_sentinel(character)
                view = load_view(character)
                if validate_view is not None:
                    validate_view(view)
                # Append before mutating: a failed append must leave the view
                # (often a shared cache) untouched; a failed view save is
                # repaired by replaying the already durable entry.
                entry = self._append_unlocked(character, event_type, payload)
                mutate_view(view)
                save_view(character, view)
                if sentinel.position == entry.position - 1:
                    self._write_sentinel(character, entry.position, entry.event_id)
                return entry

    # -- sentinel --------------------------------------------------------

    def read_sentinel(self, character: str) -> SentinelState:
        """Read the replay marker; unknown/corrupt/legacy all mean "replay all".

        A progress marker must never block recovery, so nothing here raises:
        a missing, unreadable or corrupt sentinel returns position -1 (handlers
        are idempotent, so replaying everything is safe). A legacy host sentinel
        that only carries last_applied_event_id also returns -1 with that id.
        """
        with self._lock_for(character):
            return self._read_sentinel(character)

    def _read_sentinel(self, character: str) -> SentinelState:
        path = self._sentinel_path(character, create=False)
        try:
            raw = self._store.load(path)
        except CorruptStoreError:
            return SentinelState(position=-1)
        if raw is None or not isinstance(raw, dict):
            return SentinelState(position=-1)
        position = raw.get("last_applied_position")
        if isinstance(position, bool) or not isinstance(position, int):
            legacy = raw.get("last_applied_event_id")
            return SentinelState(
                position=-1,
                event_id=legacy if isinstance(legacy, str) and legacy else None,
            )
        if position < -1:
            return SentinelState(position=-1)
        event_id = raw.get("last_applied_event_id")
        if event_id is not None and not isinstance(event_id, str):
            return SentinelState(position=-1)
        return SentinelState(position=position, event_id=event_id)

    def _write_sentinel(
        self, character: str, position: int, event_id: str | None,
    ) -> None:
        path = self._sentinel_path(character, create=False)
        self._permit.assert_writable("journal.sentinel", str(path))
        self._store.save(path, {
            "schema_version": SCHEMA_VERSION,
            "last_applied_position": position,
            "last_applied_event_id": event_id,
            "ts": self._clock.now().isoformat(),
        })

    # -- replay ----------------------------------------------------------

    def reconcile(self, character: str) -> RecoveryResult:
        """Replay the unapplied tail into the registered handlers' views.

        Stops (and leaves the marker on the last successfully applied entry)
        when it meets an unregistered type or a raising handler. Before each
        handler the marker is re-read; if another writer moved it, this round's
        tail is stale, so the tail is re-derived from the journal and replayed
        in journal order (at most _MAX_RESCANS times).

        A round with nothing to replay returns without parsing the body: the
        marker already covers the journal, and re-reading a 15 MB log to prove
        that cost 180 ms per `recover()` call (design §17.6).
        """
        with self._lock_for(character):
            return self._reconcile_unlocked(character)

    def _reconcile_unlocked(self, character: str) -> RecoveryResult:
        position = self._read_sentinel(character).position
        applied = 0
        frozen = False
        rescans = _MAX_RESCANS
        tail = list(self._read_since_unlocked(character, position))
        index = 0
        while True:
            if index >= len(tail):
                if not frozen:
                    return RecoveryResult(applied=applied, position=position)
                if rescans <= 0:
                    return RecoveryResult(
                        applied=applied, position=position, paused=True,
                        reason="rescan_limit",
                    )
                rescans -= 1
                extra = self._read_since_unlocked(character, position)
                if not extra:
                    return RecoveryResult(applied=applied, position=position)
                tail = list(extra)
                index = 0
                continue
            entry = tail[index]
            handler = self._handlers.get(entry.event_type)
            if handler is None:
                return RecoveryResult(
                    applied=applied, position=position, paused=True,
                    reason=f"unregistered_event_type:{entry.event_type}",
                )
            if not frozen:
                try:
                    self._assert_sentinel_unchanged(character, position)
                except RecoveryConflictError:
                    if rescans <= 0:
                        return RecoveryResult(
                            applied=applied, position=position, paused=True,
                            reason="rescan_limit",
                        )
                    rescans -= 1
                    frozen = True
                    tail = list(self._read_since_unlocked(character, position))
                    index = 0
                    continue
            try:
                changed = handler(character, entry.payload)
            except Exception as exc:  # a handler fault pauses the round
                return RecoveryResult(
                    applied=applied, position=position, paused=True,
                    reason=f"handler_failed:{exc}",
                )
            if changed:
                applied += 1
            if not frozen:
                try:
                    self._write_sentinel(character, entry.position, entry.event_id)
                except Exception as exc:
                    # The handler already persisted; only the marker is behind,
                    # so the entry replays idempotently on the next round.
                    return RecoveryResult(
                        applied=applied, position=position, paused=True,
                        reason=f"sentinel_write_failed:{exc}",
                    )
            position = entry.position
            index += 1

    def _assert_sentinel_unchanged(self, character: str, expected: int) -> None:
        current = self._read_sentinel(character)
        if current.position != expected:
            raise RecoveryConflictError(
                f"{character}: replay marker moved to {current.position} while this "
                f"round expected {expected}; re-deriving the tail from the journal"
            )

    # -- compaction ------------------------------------------------------

    def compact_if_needed(
        self,
        character: str,
        seed_events: Callable[[], Sequence[tuple[str, Mapping[str, Any]]]],
    ) -> int:
        """Rewrite the body from snapshot seeds when the thresholds are exceeded.

        Thresholds are 10,000 lines or an oldest record 90 days old. The new
        body gets fresh positions 0..n-1 and fresh event ids, is written
        atomically, and the marker is reset to -1 so the seeds replay (all
        handlers are idempotent). Returns the number of lines dropped (0 when
        nothing was rewritten). Seed types are validated against the journal's
        vocabulary before anything is written.

        Scheduling is NOT in B02: B07's maintenance loop calls this (design
        17.6.3). Caller contract: run it after reconcile has drained the tail;
        the seeds are derived from the views, so an unapplied tail would be
        dropped. A torn final line is dropped as well: it was never
        acknowledged, and the rewrite replaces the whole body by design.
        """
        with self._permit.transaction(
            "journal.compact", str(self._events_path(character, create=False)),
        ):
            with self._lock_for(character):
                path = self._events_path(character, create=False)
                records, _ = self._read_records(character)
                entries = [
                    self._entry(index, record, path) for index, record in records
                ]
                if not self._compact_due(entries):
                    return 0
                seeds = list(seed_events())
                for event_type, payload in seeds:
                    if event_type not in self._event_types:
                        raise UnregisteredEventTypeError(
                            f"compaction seed uses unknown event type {event_type!r}"
                        )
                    if not isinstance(payload, Mapping):
                        raise ValueError("compaction seed payload must be a mapping")
                timestamp = self._clock.now()
                lines: list[str] = []
                for position, (event_type, payload) in enumerate(seeds):
                    record = {
                        "event_id": str(uuid.uuid4()),
                        "type": event_type,
                        "ts": timestamp.isoformat(),
                        "position": position,
                        "payload": dict(payload),
                    }
                    lines.append(json.dumps(record, ensure_ascii=False))
                body = ("\n".join(lines) + "\n") if lines else ""
                self._store.save_text(path, body)
                self._write_sentinel(character, -1, None)
                with self._scans_guard:
                    self._scans.pop(character, None)
                return max(0, len(entries) - len(lines))

    def _compact_due(self, entries: Sequence[JournalEntry]) -> bool:
        if len(entries) > _COMPACT_LINES_THRESHOLD:
            return True
        if not entries:
            return False
        oldest = entries[0].recorded_at
        if oldest is None or oldest.utcoffset() is None:
            return False
        return self._clock.now() - oldest >= _COMPACT_AGE_THRESHOLD
