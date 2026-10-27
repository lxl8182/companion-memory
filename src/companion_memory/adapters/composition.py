"""Explicit composition of the B03 memory core (no host imports, no globals).

\`build_memory_core\` wires one data root and returns the independent entry
points: durable event ingest (with the deletion barrier in front), correction
and deletion services, and the shared barrier/facts/journal stores. This is
what an embedder (B08) or the standalone entry (B07) constructs; the core
never discovers Documents, host config or the current character on its own.

Recovery order matches B02/B03: the journal replay handlers are registered in
the constructors (source ledger, deletion store, correction store), then
\`MemoryCore.recover\` reconciles the journal before any new write. Barrier
state is read from disk, so replayed deletion events restore the fence even
when the tail contains older source events.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..contracts import RecoveryResult
from ..domain.barrier import DeletionBarrier
from ..domain.compression import CompressionService, CompressionSettings
from ..domain.corrections import CorrectionService
from ..domain.deletions import DeletionService, DeletionServices
from ..domain.extraction import ExtractionService, ExtractionSettings
from ..domain.maintenance import MaintenanceService, MaintenanceSettings
from ..domain.outbox_dispatch import OutboxDispatcher
from ..domain.persona import PersonaService
from ..domain.evolution import EvolutionService, EvolutionSettings
from ..domain.evolution_scheduler import EvolutionScheduler, SchedulerSettings
from ..domain.persona_admin import PersonaAdminService
from ..domain.persona_card import PersonaCardService
from ..domain.persona_projection import PersonaProjectionService
from ..domain.persona_publish import PersonaPublishService
from ..domain.recall import RecallService, RecallSettings
from ..domain.recent import RecentDeletionCleaner
from ..domain.turn_results import CorrectionTurnInvalidator, TurnResultCleaner
from ..domain.reflection import ReflectionService, ReflectionSettings
from ..domain.state import StateService
from ..persistence.atomic import FileSystemAtomicStore
from ..persistence.clock import SystemClock
from ..persistence.consumer_refs import ConsumerRefStore
from ..persistence.corrections import CorrectionStore
from ..persistence.deletions import DeletionStore
from ..persistence.directives import UserDirectiveStore
from ..persistence.facts_view import FactsView
from ..persistence.gates import SuppressionGate
from ..persistence.journal import EventJournal
from ..persistence.outbox import OutboxStore
from ..persistence.recent import RecentStore
from ..persistence.persona import PersonaStore
from ..persistence.persona_card import PersonaCardStore
from ..persistence.evolution import EvolutionStore
from ..persistence.evolution_runs import EvolutionRunStore
from ..persistence.persona_admin import PersonaAdminStore
from ..persistence.persona_projection import PersonaProjectionStore
from ..persistence.persona_versions import PersonaVersionStore
from ..persistence.permits import NullWritePermit
from ..persistence.reflections import ReflectionStore
from ..persistence.roots import FileSystemRoots
from ..persistence.shards import ArchiveShardStore
from ..persistence.source_ledger import SourceLedger
from ..persistence.state import StateStore
from ..persistence.timeindex import TimeIndexStore
from ..persistence.tombstones import ScopedTombstoneStore
from ..persistence.turns import TURN_EVENT_TYPE, TurnRequestStore
from ..ports import (
    AtomicJsonStore,
    Clock,
    EmbeddingProvider,
    ModelInvoker,
    StorageRoots,
    TokenCounter,
    WritePermit,
)
from .durable import DurableEventStore


def _evolution_evidence(facts: FactsView, reflections: ReflectionStore):
    """Default B12 evidence rows: facts and reflections with source refs."""

    def collect(character: str, request: Any = None) -> list[dict]:
        wanted = set(getattr(request, "source_refs", ()) or ())
        rows: list[dict] = []
        for row in facts.load(character):
            refs = [str(item) for item in row.get("source_event_keys") or ()]
            rows.append({
                "id": str(row.get("id") or ""),
                "kind": "fact",
                "text": str(row.get("text") or ""),
                "source_keys": refs,
                "status": str(row.get("status") or ""),
            })
        for row in reflections.load(character):
            refs = [str(item) for item in (
                row.get("source_refs") or row.get("source_keys") or ()
            )]
            rows.append({
                "id": str(row.get("id") or ""),
                "kind": "reflection",
                "text": str(row.get("text") or row.get("summary") or ""),
                "source_keys": refs,
                "status": str(row.get("status") or ""),
                "invalidated_at": row.get("invalidated_at"),
            })
        if wanted:
            rows = [
                row for row in rows
                if wanted & ({row["id"]} | set(row["source_keys"]))
            ]
        return rows

    return collect


class _PublishInvalidator:
    """Correction fan-out: invalidate published versions by fact ids."""

    def __init__(self, publish: PersonaPublishService) -> None:
        self._publish = publish

    def invalidate(self, character, fact_ids, **kwargs) -> dict:
        resolved = {"fact_ids": list(fact_ids or ())}
        return self._publish.invalidate_for_deletion(character, resolved)


class _ProjectionInvalidator:
    """Correction fan-out: invalidate the projection/consumer versions."""

    def __init__(self, projection: PersonaProjectionService) -> None:
        self._projection = projection

    def invalidate(self, character, fact_ids, **kwargs) -> dict:
        return self._projection.invalidate_for_correction(
            character, list(fact_ids or ()),
        )


class _ProjectionCleaner:
    """Deletion cleaner step: invalidate projection and consumer versions."""

    def __init__(self, projection: PersonaProjectionService) -> None:
        self._projection = projection

    def cleanup(self, character: str, resolved: dict) -> dict:
        outcome = self._projection.invalidate_for_deletion(character, resolved)
        return {"status": "completed", "detail": dict(outcome or {})}


class _AdminCleaner:
    """Deletion cleaner step: withdraw proposals / filter admin decisions."""

    def __init__(self, admin: PersonaAdminService) -> None:
        self._admin = admin

    def cleanup(self, character: str, resolved: dict) -> dict:
        outcome = self._admin.cleanup(character, resolved)
        return {"status": "completed", "detail": dict(outcome or {})}


class _PublishCleaner:
    """Deletion cleaner step: invalidate published versions for the scope."""

    def __init__(self, publish: PersonaPublishService) -> None:
        self._publish = publish

    def cleanup(self, character: str, resolved: dict) -> dict:
        outcome = self._publish.invalidate_for_deletion(character, resolved)
        return {"status": "completed", "detail": dict(outcome)}


class _CombinedCleaner:
    """Run several deletion cleaners behind the one persona cleanup step."""

    def __init__(self, *cleaners: Any) -> None:
        self._cleaners = tuple(cleaners)

    def cleanup(self, character: str, resolved: dict) -> dict:
        detail: dict = {}
        statuses: list[str] = []
        for cleaner in self._cleaners:
            outcome = cleaner.cleanup(character, dict(resolved))
            if not isinstance(outcome, Mapping):
                statuses.append("completed")
                continue
            statuses.append(str(outcome.get("status") or "completed"))
            nested = outcome.get("detail")
            if isinstance(nested, Mapping):
                detail.update(dict(nested))
        if any(status == "failed" for status in statuses):
            status = "failed"
        elif any(status == "pending" for status in statuses):
            status = "pending"
        else:
            status = "completed"
        return {"status": status, "detail": detail}


@dataclass(frozen=True)
class MemoryCore:
    """One fully wired memory core over an explicit data root."""

    roots: StorageRoots
    store: AtomicJsonStore
    clock: Clock
    permit: WritePermit
    journal: EventJournal
    ledger: SourceLedger
    outbox: OutboxStore
    sink: DurableEventStore
    turn_requests: TurnRequestStore
    consumer_receipts: Any
    #: QP05: which plugin session consumed which memory ids (ids only).
    consumer_refs: Any
    deletions: DeletionStore
    corrections: CorrectionStore
    directives: UserDirectiveStore
    tombstones: ScopedTombstoneStore
    barrier: DeletionBarrier
    facts: FactsView
    recent: RecentStore
    gates: SuppressionGate
    reflections: ReflectionStore
    persona: PersonaStore
    deletion_service: DeletionService
    correction_service: CorrectionService
    extraction: ExtractionService
    compression: CompressionService
    reflection_service: ReflectionService
    persona_service: PersonaService
    persona_card: PersonaCardStore
    persona_card_service: PersonaCardService
    persona_versions: PersonaVersionStore
    persona_publish: PersonaPublishService
    persona_projection: PersonaProjectionStore
    persona_projection_service: PersonaProjectionService
    evolution: EvolutionStore
    evolution_service: EvolutionService
    persona_admin: PersonaAdminStore
    persona_admin_service: PersonaAdminService
    evolution_runs: EvolutionRunStore
    evolution_scheduler: EvolutionScheduler
    recall: RecallService
    state: StateStore
    state_service: StateService
    outbox_dispatcher: OutboxDispatcher
    maintenance: MaintenanceService
    timeindex_factory: Callable[[str], TimeIndexStore]

    def recover(self, character: str) -> RecoveryResult:
        """Replay the unapplied journal tail; idempotent, call before writes.

        After the replay, S02 marks turn requests that are still `pending` as
        `interrupted`: they belong to a process that died between reserve and
        commit, and leaving them pending would make a retry look like a
        concurrent attempt forever.
        """
        self.barrier.refresh(character)
        result = self.sink.recover(character)
        try:
            self.turn_requests.mark_interrupted_pending(character)
        except Exception:  # noqa: BLE001 - recovery must not fail on bookkeeping
            pass
        return result

    def timeindex(self, character: str) -> TimeIndexStore:
        """An unopened time-index handle for \`character\` (callers open it)."""
        return self.timeindex_factory(character)


def build_memory_core(
    *,
    memory_root: Path | str,
    local_state_root: Path | str | None = None,
    permit: WritePermit | None = None,
    clock: Clock | None = None,
    atomic_store: AtomicJsonStore | None = None,
    ttl_seconds: float | None = None,
    ttl_max_seconds: float | None = None,
    deletion_services: DeletionServices | None = None,
    derived_invalidators=(),
    cache_invalidators=(),
    model: ModelInvoker | None = None,
    extraction_settings: ExtractionSettings | None = None,
    compression_settings: CompressionSettings | None = None,
    reflection_settings: ReflectionSettings | None = None,
    persona_render_max_tokens: int = 2000,
    recall_settings: RecallSettings | None = None,
    token_counter: TokenCounter | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    maintenance_settings: MaintenanceSettings | None = None,
    evolution_settings: EvolutionSettings | None = None,
    scheduler_settings: SchedulerSettings | None = None,
    migration_hooks: Sequence[Callable[..., Any]] = (),
) -> MemoryCore:
    """Wire the B03 stack for an explicit data root (test/demo/embedder)."""
    roots = FileSystemRoots(memory_root, local_state_root)
    store = atomic_store if atomic_store is not None else FileSystemAtomicStore()
    clock = clock if clock is not None else SystemClock()
    permit = permit if permit is not None else NullWritePermit()
    journal = EventJournal(roots=roots, store=store, clock=clock, permit=permit)
    ledger = SourceLedger(roots=roots, store=store, clock=clock, permit=permit)
    outbox = OutboxStore(roots=roots, store=store, clock=clock, permit=permit)
    deletions = DeletionStore(roots=roots, store=store, clock=clock, permit=permit)
    # QP05: which plugin session consumed which memory ids. Ids/versions only,
    # so the table survives a restart but can never carry content. Built here
    # because the deletion service needs it to find the plugins that hold a
    # copy of a fact whose source key has no plugin segment.
    consumer_refs = ConsumerRefStore(roots=roots, store=store, clock=clock, permit=permit)
    corrections = CorrectionStore(roots=roots, store=store, clock=clock, permit=permit)
    directive_kwargs = {}
    if ttl_seconds is not None:
        directive_kwargs["ttl_seconds"] = ttl_seconds
    if ttl_max_seconds is not None:
        directive_kwargs["ttl_max_seconds"] = ttl_max_seconds
    directives = UserDirectiveStore(
        roots=roots, store=store, clock=clock, permit=permit, **directive_kwargs,
    )
    tombstones = ScopedTombstoneStore(
        roots=roots, store=store, clock=clock, permit=permit,
    )
    barrier = DeletionBarrier(
        deletions=deletions, tombstones=tombstones, corrections=corrections,
    )
    facts = FactsView(roots=roots, store=store, clock=clock, permit=permit)
    # The recent store checks the deletion barrier inside its write lock (W1).
    recent = RecentStore(
        roots=roots, store=store, clock=clock, permit=permit,
        write_guard=barrier.assert_messages_allowed,
    )
    gates = SuppressionGate(memory_root=roots.memory_root, clock=clock)
    # B07: host-shaped companion_state view plus its replay handler; the service
    # serializes commits per character (lease -> request_id -> base_version).
    state_store = StateStore(
        roots=roots, store=store, clock=clock, permit=permit,
    )
    state_service = StateService(
        roots=roots, store=state_store, journal=journal, clock=clock,
        permit=permit, barrier=barrier,
    )
    timeindex_factory = lambda character: TimeIndexStore(  # noqa: E731 - simple closure
        roots=roots, character=character, permit=permit, clock=clock,
    )
    # B05: host-shaped reflection/persona stores, guarded by the deletion barrier
    # at write time; the domain services also re-check before every derived write.
    reflection_archive = ArchiveShardStore(
        roots=roots, store=store, clock=clock, permit=permit,
        dirname="reflection_archive",
    )
    persona_archive = ArchiveShardStore(
        roots=roots, store=store, clock=clock, permit=permit,
        dirname="persona_archive",
    )
    def _guard_rows(character, rows):
        for row in rows:
            barrier.assert_row_allowed(character, row)

    reflections = ReflectionStore(
        roots=roots, store=store, clock=clock, permit=permit,
        write_guard=_guard_rows, archive=reflection_archive,
    )
    persona = PersonaStore(
        roots=roots, store=store, clock=clock, permit=permit,
        write_guard=_guard_rows, archive=persona_archive,
    )
    persona_service = PersonaService(
        roots=roots, store=persona, reflections=reflections, barrier=barrier,
        journal=journal, clock=clock, permit=permit, model=model,
        render_max_tokens=persona_render_max_tokens,
    )
    reflection_service = ReflectionService(
        roots=roots, store=reflections, facts=facts, barrier=barrier,
        journal=journal, clock=clock, permit=permit, model=model,
        settings=reflection_settings, persona=persona_service,
    )
    # B10: authoritative persona card (revisions/freeze/projection); the card
    # joins the deletion and correction fan-out alongside the persona entries.
    persona_card = PersonaCardStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    persona_card_service = PersonaCardService(
        roots=roots, store=persona_card, journal=journal, clock=clock,
        permit=permit, barrier=barrier,
    )
    persona_versions = PersonaVersionStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    persona_publish = PersonaPublishService(
        roots=roots, store=persona_versions, card_service=persona_card_service,
        journal=journal, clock=clock, permit=permit, barrier=barrier,
    )
    persona_projection = PersonaProjectionStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    persona_projection_service = PersonaProjectionService(
        roots=roots, store=persona_projection, clock=clock, permit=permit,
        barrier=barrier, card_service=persona_card_service,
        publish_service=persona_publish, journal=journal,
    )
    evolution = EvolutionStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    evidence_provider = _evolution_evidence(facts, reflections)
    evolution_service = EvolutionService(
        roots=roots, store=evolution, card_service=persona_card_service,
        evidence_provider=evidence_provider,
        barrier=barrier, journal=journal, clock=clock, permit=permit,
        model=model, settings=evolution_settings,
    )
    persona_admin = PersonaAdminStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    persona_admin_service = PersonaAdminService(
        roots=roots, store=persona_admin, card_service=persona_card_service,
        evolution_service=evolution_service, publish_service=persona_publish,
        journal=journal, clock=clock, permit=permit, barrier=barrier,
    )
    evolution_runs = EvolutionRunStore(
        roots=roots, store=store, clock=clock, permit=permit, barrier=barrier,
    )
    evolution_scheduler = EvolutionScheduler(
        roots=roots, runs_store=evolution_runs,
        evolution_service=evolution_service, admin_service=persona_admin_service,
        publish_service=persona_publish, card_service=persona_card_service,
        evidence_provider=evidence_provider, barrier=barrier, clock=clock,
        permit=permit, settings=scheduler_settings,
    )
    turn_requests = TurnRequestStore(
        roots=roots, store=store, clock=clock, permit=permit,
    )
    # S02: the replay handler for the whole-turn commit event is registered
    # here, before any write, so a crash after the journal fsync is repairable.
    journal.register_handler(TURN_EVENT_TYPE, turn_requests.apply_replay)
    derived_invalidators = tuple(derived_invalidators) + (
        _ProjectionInvalidator(persona_projection_service),
        reflection_service, persona_service, persona_card_service,
        _PublishInvalidator(persona_publish),
        persona_admin_service,
        # S02/R01: a committed reply must stop being served once the fact it
        # was generated from is corrected.
        CorrectionTurnInvalidator(turn_requests, facts),
    )
    services = deletion_services or DeletionServices()
    if services.recent is None:
        services = DeletionServices(
            reflections=reflection_service
            if services.reflections is None else services.reflections,
            persona=_CombinedCleaner(
                _ProjectionCleaner(persona_projection_service),
                persona_service, persona_card_service,
                _PublishCleaner(persona_publish),
                _AdminCleaner(persona_admin_service),
            ) if services.persona is None else services.persona,
            recent=RecentDeletionCleaner(recent, barrier),
            # S02/R01: invalidate committed turn results for deleted sources.
            turn_results=TurnResultCleaner(turn_requests),
            consumer_refs=services.consumer_refs,
        )
    if services.consumer_refs is None:
        # A caller that supplied its own services still gets consumer
        # tracking: without it a web-created fact's plugin copy is invisible.
        services = DeletionServices(
            reflections=services.reflections,
            persona=services.persona,
            recent=services.recent,
            turn_results=services.turn_results,
            consumer_refs=consumer_refs,
        )
    deletion_service = DeletionService(
        roots=roots,
        journal=journal,
        deletions=deletions,
        barrier=barrier,
        facts=facts,
        clock=clock,
        permit=permit,
        timeindex_factory=timeindex_factory,
        services=services,
        cache_invalidators=cache_invalidators,
        ledger=ledger,
        outbox=outbox,
    )
    correction_service = CorrectionService(
        roots=roots,
        journal=journal,
        corrections=corrections,
        barrier=barrier,
        facts=facts,
        clock=clock,
        permit=permit,
        derived_invalidators=derived_invalidators,
        token_counter=token_counter,
    )
    sink = DurableEventStore(
        roots=roots, journal=journal, ledger=ledger, permit=permit,
        clock=clock, barrier=barrier,
    )
    extraction = ExtractionService(
        roots=roots,
        sink=sink,
        outbox=outbox,
        facts=facts,
        recent=recent,
        barrier=barrier,
        clock=clock,
        permit=permit,
        model=model,
        settings=extraction_settings,
        gate=gates,
        timeindex_factory=timeindex_factory,
        corrections=correction_service,
    )
    compression = CompressionService(
        recent=recent, barrier=barrier, clock=clock, model=model,
        settings=compression_settings,
    )
    # B06: read-only recall over the B05 stores; token/vector providers are
    # optional ports (BM25-only fallback when no embedding is injected).
    recall = RecallService(
        clock=clock,
        facts=facts,
        reflections=reflection_service,
        persona=persona_service,
        corrections=correction_service,
        barrier=barrier,
        token_counter=token_counter,
        embedding=embedding_provider,
        settings=recall_settings,
        roots=roots,
    )
    # B07: outbox dispatch + one maintenance round; no background task is
    # started here (the standalone entry / host supervisor owns scheduling).
    outbox_dispatcher = OutboxDispatcher(outbox=outbox)
    outbox_dispatcher.register_bulk(
        "extract_facts",
        lambda character: extraction.process_outbox(character),
    )
    maintenance = MaintenanceService(
        dispatcher=outbox_dispatcher,
        deletion_service=deletion_service,
        reflection_service=reflection_service,
        persona_service=persona_service,
        journal=journal,
        outbox=outbox,
        clock=clock,
        settings=maintenance_settings,
        recover=sink.recover,
        scheduler=evolution_scheduler,
        migrations=tuple(migration_hooks or ()),
    )
    from ..persistence.consumer_receipts import ConsumerReceiptStore, RECEIPT_EVENT
    consumer_receipts = ConsumerReceiptStore(roots=roots, store=store, permit=permit)
    journal.register_handler(RECEIPT_EVENT, consumer_receipts.replay)
    return MemoryCore(
        roots=roots,
        store=store,
        clock=clock,
        permit=permit,
        journal=journal,
        ledger=ledger,
        outbox=outbox,
        sink=sink,
        turn_requests=turn_requests,
        consumer_receipts=consumer_receipts,
        consumer_refs=consumer_refs,
        deletions=deletions,
        corrections=corrections,
        directives=directives,
        tombstones=tombstones,
        barrier=barrier,
        facts=facts,
        recent=recent,
        gates=gates,
        reflections=reflections,
        persona=persona,
        deletion_service=deletion_service,
        correction_service=correction_service,
        extraction=extraction,
        compression=compression,
        reflection_service=reflection_service,
        persona_service=persona_service,
        persona_card=persona_card,
        persona_card_service=persona_card_service,
        persona_versions=persona_versions,
        persona_publish=persona_publish,
        persona_projection=persona_projection,
        persona_projection_service=persona_projection_service,
        evolution=evolution,
        evolution_service=evolution_service,
        persona_admin=persona_admin,
        persona_admin_service=persona_admin_service,
        evolution_runs=evolution_runs,
        evolution_scheduler=evolution_scheduler,
        recall=recall,
        state=state_store,
        state_service=state_service,
        outbox_dispatcher=outbox_dispatcher,
        maintenance=maintenance,
        timeindex_factory=timeindex_factory,
    )


__all__ = ["MemoryCore", "build_memory_core"]
