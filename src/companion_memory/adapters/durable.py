"""Durable event sink: per-character journal + source ledger + write permit.

This is the first persistent `EventSink` (B02). `append` returns
`durable=True` only after the event is fsynced into the journal AND the
source ledger has been committed, so a caller may treat the receipt as a
durable acknowledgement. A crash between the journal append and the ledger
save leaves an unapplied journal entry that `recover()` replays into the
ledger on the next open; the handler is idempotent, so re-applying is safe.

Scope boundaries kept deliberately: this module owns the *durable receipt*
path only. It does not extract facts, rank recall, or touch the host; those
arrive in B04/B05/B06. Every persistent write goes through the injected
`WritePermit` (host maintenance mode / save fence is wired in B08).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Mapping

from ..contracts import EventReceipt, MemoryEvent, RecoveryResult
from ..errors import WriteRejected
from ..persistence.records import event_fingerprint, event_snapshot
from ..persistence.source_ledger import SourceLedger
from ..ports import Clock, EventJournal, StorageRoots, WritePermit

if TYPE_CHECKING:  # typing-only; keeps the runtime import graph acyclic
    from ..domain.barrier import DeletionBarrier


class DurableEventStore:
    """Persistent `EventSink`: journal entry first, source ledger second.

    Wiring requirements (enforced by construction, not by convention):
      * the journal handler for `EVENT_TYPE` is registered here, before any
        write can happen, because a journal entry without a replay handler
        could never be repaired after a crash;
      * `recover()` runs the journal replay before the first write for a
        character (startup order: handlers -> reconcile -> writes, design
        §17.4/§17.6.3);
      * per-character locks serialise the lookup -> append -> ledger commit
        sequence inside one process (single-writer rule, design §17.2).
    """

    #: Core-owned journal event type (B02 opens exactly this one).
    EVENT_TYPE = "core.source_recorded"

    def __init__(
        self,
        *,
        roots: StorageRoots,
        journal: EventJournal,
        ledger: SourceLedger,
        permit: WritePermit,
        clock: Clock,
        barrier: "DeletionBarrier | None" = None,
    ) -> None:
        self._roots = roots
        self._journal = journal
        self._ledger = ledger
        self._permit = permit
        self._clock = clock
        # B03: the deletion barrier is checked before a live append and before
        # a replay repairs the ledger, so deleted content cannot be written
        # back by an old task or an old journal entry.
        self._barrier = barrier
        self._journal.register_handler(self.EVENT_TYPE, self._replay_source_recorded)
        self._recovered: dict[str, RecoveryResult] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- EventSink ------------------------------------------------------

    async def append(self, event: MemoryEvent) -> EventReceipt:
        return await asyncio.to_thread(self._append_sync, event)

    # -- recovery -------------------------------------------------------

    def recover(self, character: str) -> RecoveryResult:
        """Replay the unapplied journal tail for `character`; idempotent.

        Runs once per character per process, before the first write. A paused
        result is reported, not raised: the caller decides whether an
        incomplete replay blocks its use (B07 maintenance owns retry policy).
        """
        result = self._journal.reconcile(character)
        self._recovered[character] = result
        return result

    def recovery_state(self, character: str) -> RecoveryResult | None:
        return self._recovered.get(character)

    # -- internals ------------------------------------------------------

    def _append_sync(self, event: MemoryEvent) -> EventReceipt:
        character = event.scope.character
        if character not in self._recovered:
            self.recover(character)
        prepared = event if event.received_at is not None else replace(
            event, received_at=self._clock.now(),
        )
        if self._barrier is not None and self._barrier.blocks_event(prepared):
            # The barrier is durable on disk, so the rejection is a durable
            # decision: this source was deleted and must not be re-ingested.
            return EventReceipt(
                source_key=prepared.source_key, status="rejected", durable=True,
                reason="deleted_source",
            )
        snapshot = event_snapshot(prepared)
        digest = event_fingerprint(prepared)
        with self._character_lock(character):
            existing = self._ledger.lookup(prepared.scope, prepared.source_key)
            if existing is not None:
                if existing.fingerprint and existing.fingerprint != digest:
                    return EventReceipt(
                        source_key=prepared.source_key, status="conflict",
                        durable=True, reason="source_key_reused_with_different_payload",
                    )
                return EventReceipt(
                    source_key=prepared.source_key, status="duplicate", durable=True,
                )
            try:
                entry = self._journal.record_and_save(
                    character,
                    self.EVENT_TYPE,
                    snapshot,
                    load_view=self._ledger.load_view,
                    mutate_view=lambda view: self._ledger.apply_snapshot(view, snapshot),
                    save_view=self._ledger.save_view,
                )
            except WriteRejected as exc:
                # The permit is the last fence before persistence; a rejected
                # write is an expected outcome (maintenance mode), not a fault.
                return EventReceipt(
                    source_key=prepared.source_key, status="rejected",
                    durable=False, reason=exc.code,
                )
        return EventReceipt(
            source_key=prepared.source_key, status="accepted", durable=True,
            event_id=entry.event_id, processing_position=entry.position,
        )

    def _replay_source_recorded(self, character: str, payload: Mapping[str, object]) -> bool:
        """Journal replay handler: idempotently repair the ledger from a snapshot.

        Must stay tolerant: a conflicting entry already on disk is left alone
        (returning False) instead of raising, because a raising handler pauses
        replay for the whole character. Conflict detection happens on the live
        path, before a conflicting event is ever written.
        """
        if self._barrier is not None:
            # Replaying an event that predates a deletion must not rebuild the
            # ledger hint for deleted content. A malformed snapshot is left to
            # the ledger's own tolerant path (it returns False).
            source_key = payload.get("source_key")
            episode_id = payload.get("episode_id")
            if (
                isinstance(source_key, str)
                and self._barrier.blocks_source_key(character, source_key)
            ) or (
                isinstance(episode_id, str)
                and self._barrier.blocks_episode(character, episode_id)
            ):
                return False
        view = self._ledger.load_view(character)
        if not self._ledger.apply_snapshot(view, payload):
            return False
        self._ledger.save_view(character, view)
        return True

    def _character_lock(self, character: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(character)
            if lock is None:
                lock = threading.Lock()
                self._locks[character] = lock
            return lock

def build_durable_event_store(
    *,
    memory_root,
    local_state_root=None,
    permit=None,
    clock=None,
    atomic_store=None,
    journal=None,
    barrier=None,
) -> DurableEventStore:
    """Wire the B02 persistent stack for an explicit data root.

    Deliberately takes the root as an argument: the core never looks up
    Documents, host config or the current character (design §17.2). Production
    wiring (B08) passes the host-resolved `memory_dir`, the real
    `local_state_dir` and the host-backed write permit.
    """
    from ..persistence.atomic import FileSystemAtomicStore
    from ..persistence.clock import SystemClock
    from ..persistence.journal import EventJournal
    from ..persistence.permits import NullWritePermit
    from ..persistence.roots import FileSystemRoots

    roots = FileSystemRoots(memory_root, local_state_root)
    store = atomic_store if atomic_store is not None else FileSystemAtomicStore()
    clock = clock if clock is not None else SystemClock()
    permit = permit if permit is not None else NullWritePermit()
    ledger = SourceLedger(roots=roots, store=store, clock=clock, permit=permit)
    event_journal = journal if journal is not None else EventJournal(
        roots=roots, store=store, clock=clock, permit=permit,
    )
    return DurableEventStore(
        roots=roots, journal=event_journal, ledger=ledger, permit=permit,
        clock=clock, barrier=barrier,
    )
