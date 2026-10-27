"""Composition facade; no host imports, globals, model calls or background tasks."""

import asyncio
from typing import TYPE_CHECKING, Any, Mapping

from .contracts import (
    ContextBundle,
    ContextItem,
    ContextRequest,
    CorrectionReceipt,
    CorrectionRequest,
    DeletionReceipt,
    DeletionRequest,
    EventReceipt,
    MemoryBlock,
    MemoryEvent,
    PersonaProfile,
    PersonaSection,
    Scene,
)
from .errors import NotConfiguredError
from .persona.rendering import render_profile
from .ports import ContextProvider, EventSink, ProfileReader
from .scope import scene_matches

if TYPE_CHECKING:  # runtime stays light; the services are injected, not imported
    from .domain.corrections import CorrectionService
    from .domain.deletions import DeletionService
    from .domain.maintenance import MaintenanceService
    from .domain.persona import PersonaService
    from .domain.evolution import EvolutionService, ProposalRequest
    from .domain.persona_admin import PersonaAdminService, RevisionDiff
    from .domain.persona_card import PersonaCardService, RevisionRequest
    from .domain.persona_projection import PersonaProjectionService
    from .domain.persona_publish import PersonaPublishService, PublishRequest
    from .domain.evolution_scheduler import EvolutionScheduler
    from .domain.outputs import OutputReceipt, OutputService
    from .domain.recall import RecallService
    from .domain.state import StateCommitRequest, StateReadRequest, StateService
    from .domain.working_summary import WorkingSummary, WorkingSummaryService
    from .domain.reflection import (
        GrowthReceipt,
        PromotionOutcome,
        ReflectionService,
        SynthesisOutcome,
    )


class MemoryEngine:
    """The operation surface: observe / context / correct / forget.

    B00/B02 call sites only needed the first three collaborators; B03 adds the
    optional correction and deletion services. Calling \`correct\`/\`forget\`
    without wiring them raises \`NotConfiguredError\` instead of returning an
    empty success (design §17.9.1): a receipt that says "done" must mean a
    barrier was actually registered.
    """

    def __init__(
        self,
        *,
        events: EventSink,
        profiles: ProfileReader,
        context: ContextProvider,
        corrections: "CorrectionService | None" = None,
        deletions: "DeletionService | None" = None,
        reflections: "ReflectionService | None" = None,
        persona: "PersonaService | None" = None,
        recall: "RecallService | None" = None,
        state: "StateService | None" = None,
        maintenance: "MaintenanceService | None" = None,
        persona_card: "PersonaCardService | None" = None,
        persona_publish: "PersonaPublishService | None" = None,
        evolution: "EvolutionService | None" = None,
        persona_admin: "PersonaAdminService | None" = None,
        persona_projection: "PersonaProjectionService | None" = None,
        evolution_scheduler: "EvolutionScheduler | None" = None,
        outputs: "OutputService | None" = None,
        working_summary: "WorkingSummaryService | None" = None,
    ) -> None:
        self._events = events
        self._profiles = profiles
        self._context = context
        self._corrections = corrections
        self._deletions = deletions
        self._reflections = reflections
        self._persona = persona
        self._recall = recall
        self._state = state
        self._maintenance = maintenance
        self._persona_card = persona_card
        self._persona_publish = persona_publish
        self._evolution = evolution
        self._persona_admin = persona_admin
        self._persona_projection = persona_projection
        self._evolution_scheduler = evolution_scheduler
        #: M5: assistant-output records and the rebuildable working summaries.
        self._outputs = outputs
        self._working_summary = working_summary

    # -- B00/B02 operations ---------------------------------------------

    async def observe(self, event: MemoryEvent) -> EventReceipt:
        return await self._events.append(event)

    async def get_profile(self, character: str) -> PersonaProfile | None:
        if self._persona_card is not None:
            return self._profile_from_card(character)
        return await self._profiles.get(character)

    def _profile_from_card(self, character: str) -> PersonaProfile | None:
        snapshot = self._persona_card.get(character)  # type: ignore[union-attr]
        if not snapshot.sections:
            return None
        sections = tuple(
            PersonaSection(
                section_id=section.section_id,
                text=section.text,
                subject=(section.scope or {}).get("subject")
                if isinstance(section.scope, Mapping) else None,
                applies_to=_scene_from_payload(
                    (section.scope or {}).get("applies_to")
                    if isinstance(section.scope, Mapping) else None,
                ),
                evolution_enabled=(
                    bool((section.editable_by or {}).get("evolution", False))
                    and not section.frozen
                ),
            )
            for section in snapshot.sections
            if section.text.strip()
        )
        if not sections:
            return None
        return PersonaProfile(
            character=character,
            revision=max(1, snapshot.revision),
            sections=sections,
        )

    async def build_context(self, request: ContextRequest) -> ContextBundle:
        # The optional-recall deadline starts before the required profile read.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.budget_ms / 1000
        profile = (
            self._profile_from_card(request.scope.character)
            if self._persona_card is not None
            else await self._profiles.get(request.scope.character)
        )
        persona = render_profile(profile, request) if profile is not None else ""
        status = "ready"
        candidates = ()
        if loop.time() >= deadline:
            status = "timed_out"
        elif self._recall is not None:
            # B06: the recall service owns its own budget/timeout and never
            # raises; a degraded result maps to the timed_out bundle status.
            remaining_ms = max(1, int((deadline - loop.time()) * 1000))
            result = await self._recall.recall_memory(
                request.scope.character,
                request.current_input,
                subject=request.scope.subject,
                scene=request.scene,
                purpose=request.purpose,
                budget_ms=remaining_ms,
                max_items=request.max_items,
            )
            if result.get("status") in ("unavailable", "error"):
                status = "timed_out"
            candidates = tuple(
                ContextItem(
                    scope=request.scope,
                    source_key=(
                        (item.get("source_keys") or [""])[0]
                        or str(item.get("id") or "")
                    ),
                    text=str(item.get("text") or ""),
                    kind=str(item.get("kind") or ""),
                    applies_to=_scene_from_payload(item.get("applies_to")),
                )
                for item in result.get("items") or ()
                if isinstance(item, dict) and item.get("text")
            )
        else:
            timeout = asyncio.timeout_at(deadline)
            try:
                async with timeout:
                    candidates = await self._context.recall(request)
            except TimeoutError:
                if not timeout.expired():
                    raise
                status = "timed_out"
        items = tuple(
            item for item in candidates
            if item.scope == request.scope
            and scene_matches(item.applies_to, request.scene)
        )[:request.max_items]
        return ContextBundle(
            scope=request.scope,
            persona=persona,
            persona_revision=profile.revision if profile is not None else None,
            items=items,
            recall_status=status,
        )

    # -- B03 operations --------------------------------------------------

    async def correct(self, request: CorrectionRequest) -> CorrectionReceipt:
        """Record an explicit correction/preference and invalidate what it replaces."""
        service = self._require_corrections()
        return await service.correct(request)

    async def retract_correction(
        self, character: str, correction_id: str,
    ) -> CorrectionReceipt:
        """Withdraw an active correction (applied invalidations stay)."""
        service = self._require_corrections()
        return await service.retract(character, correction_id)

    async def forget(self, request: DeletionRequest) -> DeletionReceipt:
        """Register the deletion barrier, clean owned stores, return the receipt."""
        service = self._require_deletions()
        return await service.forget(request)

    async def retry_deletion(
        self, character: str, deletion_id: str,
    ) -> DeletionReceipt:
        """Re-run pending/failed cleanup steps of an existing deletion."""
        service = self._require_deletions()
        return await service.retry(character, deletion_id)

    def correction_block(
        self,
        character: str,
        *,
        subject: str | None = None,
        scene=None,
        language: str = "zh",
        limit_tokens: int = 200,
    ) -> MemoryBlock:
        """Corrections rendered for prompt injection, deleted content filtered."""
        service = self._require_corrections()
        return service.block(
            character, subject=subject, scene=scene,
            language=language, limit_tokens=limit_tokens,
        )

    def correction_version(self, character: str) -> int:
        """Monotonic correction version for in-flight invalidation checks."""
        return self._require_corrections().version(character)

    def deletion_version(self, character: str) -> int:
        """Monotonic deletion version for in-flight invalidation checks."""
        return self._require_deletions().version(character)

    # -- B05 operations --------------------------------------------------

    async def reflect(self, character: str) -> "SynthesisOutcome":
        """Synthesize reflections/growth from unabsorbed facts (one batch)."""
        return await self._require_reflections().synthesize(character)

    async def promote_reflections(self, character: str) -> "PromotionOutcome":
        """Run the score/time-driven promotion pass over eligible reflections."""
        return await self._require_reflections().promote(character)

    async def archive_reflections(self, character: str) -> dict:
        """Move aged/negative-score reflections into the archive shards."""
        return await self._require_reflections().archive(character)

    async def record_surfaced(self, character: str, reflection_id: str) -> dict:
        """Record that a reflection was actually delivered (cooldown starts)."""
        return await self._require_reflections().record_surfaced(character, reflection_id)

    async def record_reflection_feedback(
        self, character: str, reflection_id: str, feedback: str,
    ) -> dict:
        """Record confirmed/denied/ignored feedback for a surfaced reflection."""
        return await self._require_reflections().record_feedback(
            character, reflection_id, feedback,
        )

    def reflection_block(
        self,
        character: str,
        *,
        subject: str | None = None,
        scene=None,
        language: str = "zh",
        limit_tokens: int | None = None,
    ) -> MemoryBlock:
        """Reflections rendered for prompt injection (deleted content filtered)."""
        return self._require_reflections().block(
            character, subject=subject, scene=scene,
            language=language, limit_tokens=limit_tokens,
        )

    def growth_block(
        self,
        character: str,
        *,
        scene=None,
        language: str = "zh",
        limit_tokens: int | None = None,
    ) -> MemoryBlock:
        """Confirmed/promoted growth items rendered as the growth block."""
        return self._require_reflections().growth_block(
            character, scene=scene, language=language, limit_tokens=limit_tokens,
        )

    def growth_items(self, character: str, *, scene=None) -> list[dict]:
        """Structured growth items for callers that render their own block."""
        return self._require_reflections().growth_items(character, scene=scene)

    async def record_growth_usage(
        self,
        character: str,
        item_id: str,
        usage: str,
        *,
        source_key: str = "",
        scene=None,
        outcome: str | None = None,
        feedback: str | None = None,
    ) -> "GrowthReceipt":
        """Record one growth use (read/adopted/result/feedback); idempotent."""
        return await self._require_reflections().record_growth_usage(
            character, item_id, usage, source_key=source_key,
            scene=scene, outcome=outcome, feedback=feedback,
        )

    async def retract_growth(self, character: str, item_id: str) -> dict:
        """Withdraw one growth item and its derived persona entry."""
        return await self._require_reflections().retract_growth(character, item_id)

    def persona_block(
        self,
        character: str,
        *,
        language: str = "zh",
        limit_tokens: int | None = None,
    ) -> str:
        """Persona entries (reflection/growth/manual) rendered for injection."""
        return self._require_persona().render_block(
            character, language=language, limit_tokens=limit_tokens,
        )

    def persona_entries(self, character: str, *, entity: str | None = None) -> list[dict]:
        """Readable persona entries, deleted/invalidated rows filtered out."""
        return self._require_persona().entries(character, entity=entity)

    def persona_corrections(self, character: str) -> list[dict]:
        """Pending persona correction queue rows (host shape)."""
        return self._require_persona().corrections(character)

    async def resolve_persona_corrections(self, character: str, limit: int = 10) -> dict:
        """Resolve a batch of queued persona corrections (merge/keep_* actions)."""
        return await self._require_persona().resolve_corrections_async(
            character, limit=limit,
        )

    # -- B06 operations --------------------------------------------------

    async def build_turn_context(self, character: str, **kwargs) -> dict:
        """Host-shaped turn context from the recall service (never raises)."""
        return await self._require_recall().build_turn_context(character, **kwargs)

    async def recall_memory(
        self, character: str, query: str = "", **kwargs,
    ) -> dict:
        """Explicit hybrid recall for the recall tool / management queries."""
        return await self._require_recall().recall_memory(
            character, query, **kwargs,
        )

    async def recall_by_time(self, character: str, **kwargs) -> dict:
        """Time-window recall (episodes, source background)."""
        return await self._require_recall().recall_by_time(character, **kwargs)

    # -- B07 operations --------------------------------------------------

    async def read_state(
        self, character: str, request: "StateReadRequest | None" = None, **kwargs,
    ) -> dict:
        """Read one subject's companion state (version/lease/blocks included)."""
        snapshot = await self._require_state().aread(character, request, **kwargs)
        return {
            "ok": snapshot.ok,
            "subject": snapshot.subject,
            "version": snapshot.version,
            "updated_at": snapshot.updated_at,
            "state": snapshot.state,
            "items": snapshot.items,
            "block": snapshot.block,
            "unchanged": snapshot.unchanged,
            "lease": snapshot.lease,
            "processed_events": snapshot.processed_events,
        }

    async def commit_state(
        self, character: str, request: "StateCommitRequest | None" = None, **kwargs,
    ) -> dict:
        """Commit state ops with lease + request_id + base_version checks."""
        receipt = await self._require_state().acommit(character, request, **kwargs)
        return {
            "ok": receipt.ok,
            "version": receipt.version,
            "applied": receipt.applied,
            "duplicate": receipt.duplicate,
            "state": receipt.state,
            "block": receipt.block,
            "updated_at": receipt.updated_at,
            "conflict": receipt.conflict,
            "reason": receipt.reason,
            "current": receipt.current,
            "event_id": receipt.event_id,
        }

    # -- M5 operations ---------------------------------------------------

    def record_output(
        self,
        character: str,
        *,
        text: str,
        kind: str = "notice",
        subject: str = "private",
        scene: str = "",
        session_id: str = "",
        client_id: str = "",
        episode_id: str | None = None,
        source_key: str | None = None,
        cause: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        part_sizes: object = None,
    ) -> dict:
        """Persist a generated assistant body without declaring it delivered."""
        receipt = self._require_outputs().create(
            character, text=text, kind=kind, subject=subject, scene=scene,
            session_id=session_id, client_id=client_id, episode_id=episode_id,
            source_key=source_key, cause=cause, request_id=request_id,
            part_sizes=part_sizes,  # type: ignore[arg-type]
        )
        return receipt.to_dict(include_text=True)

    def deliver_output(
        self,
        character: str,
        output_id: str,
        *,
        parts: object = None,
        platform_id: str = "",
        kind: str = "sent",
        reason: str | None = None,
    ) -> dict:
        """Record which parts actually arrived; only those enter recent."""
        receipt = self._require_outputs().deliver(
            character, output_id, parts=parts,  # type: ignore[arg-type]
            platform_id=platform_id, kind=kind, reason=reason,
        )
        return receipt.to_dict(include_text=True)

    def output(self, character: str, output_id: str, *, include_text: bool = True) -> dict:
        payload = self._require_outputs().get(
            character, output_id, include_text=include_text,
        )
        if payload is None:
            raise NotConfiguredError(f"unknown output {output_id!r}")
        return payload

    def pending_outputs(self, character: str, *, limit: int = 20) -> dict:
        """What a restart still has to deliver (resume after a crash)."""
        return {
            "items": self._require_outputs().pending(character, limit=limit),
        }

    def invalidate_output(
        self, character: str, output_id: str, *, reason: str = "source_deleted",
    ) -> dict:
        receipt = self._require_outputs().invalidate(
            character, output_id, reason=reason,
        )
        return receipt.to_dict(include_text=False)

    def working_summary(
        self,
        character: str,
        *,
        subject: str = "private",
        scene: object = None,
        language: str = "zh",
    ) -> dict:
        """The three rebuildable summaries plus the episode view (read-only)."""
        summary = self._require_working_summary().build(
            character, subject=subject, scene=scene, language=language,
        )
        return summary.to_dict()

    def resolve_followup(
        self,
        character: str,
        query: str,
        *,
        subject: str = "private",
        scene: object = None,
        language: str = "zh",
    ) -> dict:
        """Which open thread a continuation question is about (+ clarify basis)."""
        return self._require_working_summary().resolve_followup(
            character, query, subject=subject, scene=scene, language=language,
        )

    async def maintain(self, character: str) -> dict:
        """Run one maintenance round (reconcile -> outbox -> derive -> retry)."""
        result = await self._require_maintenance().run_once(character)
        return {
            "character": result.character,
            "status": result.status,
            "steps": result.steps,
            "processed": result.processed,
            "pending": result.pending,
            "error": result.error,
        }

    # -- B10 operations --------------------------------------------------

    def persona_card(self, character: str) -> dict:
        """Current authoritative card snapshot (revision/sections/revisions)."""
        snapshot = self._require_persona_card().get(character)
        return {
            "character": snapshot.character,
            "revision": snapshot.revision,
            "initial_revision": snapshot.initial_revision,
            "sections": [section.to_mapping() for section in snapshot.sections],
            "revisions": [revision.__dict__ for revision in snapshot.revisions],
        }

    def import_persona_card(
        self, character: str, sections, *, source: str = "initial_import",
    ) -> dict:
        """Import the initial card once (duplicate on the second call)."""
        receipt = self._require_persona_card().import_initial(
            character, sections, source=source,
        )
        return _revision_receipt_dict(receipt)

    def revise_persona_card(self, character: str, request) -> dict:
        """Apply one local revision under base_revision CAS."""
        receipt = self._require_persona_card().revise(character, request)
        return _revision_receipt_dict(receipt)

    def freeze_persona_section(
        self, character: str, section_id: str, frozen: bool = True,
    ) -> dict:
        """Freeze/unfreeze one section (manual control; auto edits blocked)."""
        receipt = self._require_persona_card().freeze(
            character, section_id, frozen,
        )
        return _revision_receipt_dict(receipt)

    def restore_persona_card(self, character: str) -> dict:
        """Restore the imported sections as a new revision (history kept)."""
        receipt = self._require_persona_card().restore_initial(character)
        return _revision_receipt_dict(receipt)

    def persona_projection(self, character: str) -> dict:
        """Read-only section-id -> text projection for characters.json."""
        return self._require_persona_card().render_projection(character)

    def persona_card_text(self, character: str) -> str:
        """Whole-card prompt text from the current revision."""
        return self._require_persona_card().render_text(character)

    # -- B11 operations --------------------------------------------------

    def publish_persona(self, character: str, request=None, **kwargs) -> dict:
        """Publish the current (or requested) card revision for one target."""
        if request is None and not kwargs:
            request = None
        receipt = self._require_persona_publish().publish(
            character, request, **kwargs,
        )
        return _publish_receipt_dict(receipt)

    def current_persona_version(self, character: str) -> dict:
        """Currently published version token/revision/text/boundary."""
        return _publish_snapshot_dict(
            self._require_persona_publish().current(character),
        )

    def consume_persona_version(
        self, character: str, consumer: str, expected_version: str | None = None,
    ) -> dict:
        """A consumer reports the version it rendered/used; mismatch needs reissue."""
        receipt = self._require_persona_publish().consume(
            character, consumer, expected_version=expected_version,
        )
        return {
            "status": receipt.status,
            "consumer": receipt.consumer,
            "expected_version": receipt.expected_version,
            "version": receipt.version,
            "revision": receipt.revision,
            "needs_reissue": receipt.needs_reissue,
            "reason": receipt.reason,
            "seen_at": receipt.seen_at,
            "snapshot": None if receipt.snapshot is None
            else _publish_snapshot_dict(receipt.snapshot),
        }

    def pair_persona_planner(self, character: str, planner_version: str) -> dict:
        """Pair a game planner poll with the published card version."""
        return self._require_persona_publish().pair_planner(
            character, planner_version,
        )

    def rollback_persona_version(
        self, character: str, version: str, *, allow_consumed: bool = False,
    ) -> dict:
        """Make an earlier published version current again (explicit op)."""
        receipt = self._require_persona_publish().rollback(
            character, version, allow_consumed=allow_consumed,
        )
        return _publish_receipt_dict(receipt)

    # -- B12 operations --------------------------------------------------

    async def evolve_persona(self, character: str, request=None) -> dict:
        """Ask the evolution service for a validated proposal (never writes the card)."""
        receipt = await self._require_evolution().propose(character, request)
        return _proposal_receipt_dict(receipt)

    def persona_evolution_proposals(
        self, character: str, *, status: str | None = None, limit: int | None = None,
    ) -> list:
        """List stored proposals (B13 review surface; read-only here)."""
        return self._require_evolution().list(
            character, status=status, limit=limit,
        )

    def get_persona_evolution_proposal(self, character: str, proposal_id: str) -> dict | None:
        """Read one proposal by id."""
        return self._require_evolution().get(character, proposal_id)

    def withdraw_persona_evolution_proposal(
        self, character: str, proposal_id: str,
    ) -> dict | None:
        """Withdraw a proposal that has not been accepted."""
        return self._require_evolution().withdraw(character, proposal_id)

    def expire_persona_evolution_proposals(self, character: str) -> int:
        """Expire proposals past their TTL; returns the number changed."""
        return self._require_evolution().expire_stale(character)

    # -- B13 operations --------------------------------------------------

    def persona_admin_snapshot(self, character: str) -> dict:
        """Management view: switch, current card, revisions, decisions, proposals."""
        snapshot = self._require_persona_admin().snapshot(character)
        return {
            "character": snapshot.character,
            "auto_evolution": snapshot.auto_evolution.to_mapping(),
            "card": None if snapshot.card is None else {
                "character": snapshot.card.character,
                "revision": snapshot.card.revision,
                "initial_revision": snapshot.card.initial_revision,
                "sections": [section.to_mapping() for section in snapshot.card.sections],
            },
            "revisions": [dict(item) for item in snapshot.revisions],
            "decisions": [dict(item) for item in snapshot.decisions],
            "proposals": [dict(item) for item in snapshot.proposals],
            "pending_proposals": snapshot.pending_proposals,
        }

    def persona_admin_proposals(
        self, character: str, *, status: str | None = None,
    ) -> list:
        """Proposals for the management panel (deleted sources hidden)."""
        return self._require_persona_admin().list_proposals(character, status)

    def accept_persona_proposal(
        self, character: str, proposal_id: str, *, actor: str = "user",
        reason: str = "", expected_revision: int | None = None,
    ) -> dict:
        """Accept a proposal: revise the card through B10, never directly."""
        receipt = self._require_persona_admin().accept_proposal(
            character, proposal_id, actor=actor, reason=reason,
            expected_revision=expected_revision,
        )
        return _decision_receipt_dict(receipt)

    def reject_persona_proposal(
        self, character: str, proposal_id: str, *, actor: str = "user",
        reason: str = "",
    ) -> dict:
        """Reject a proposal; the card is untouched."""
        receipt = self._require_persona_admin().reject_proposal(
            character, proposal_id, actor=actor, reason=reason,
        )
        return _decision_receipt_dict(receipt)

    def withdraw_persona_proposal(
        self, character: str, proposal_id: str, *, actor: str = "user",
        reason: str = "",
    ) -> dict:
        """Withdraw a proposal; the card is untouched."""
        receipt = self._require_persona_admin().withdraw_proposal(
            character, proposal_id, actor=actor, reason=reason,
        )
        return _decision_receipt_dict(receipt)

    def persona_card_revisions(self, character: str) -> list:
        """Card revision history summary (newest first)."""
        return self._require_persona_admin().revisions(character)

    def persona_card_diff(
        self, character: str, from_revision: int, to_revision: int,
    ) -> dict:
        """Section-level diff between two card revisions."""
        return self._require_persona_admin().diff(
            character, from_revision, to_revision,
        ).to_dict()

    def rollback_persona_card_revision(
        self, character: str, revision: int, *, actor: str = "user",
        reason: str = "",
    ) -> dict:
        """Restore an earlier revision as a new revision (history kept)."""
        receipt = self._require_persona_admin().rollback_to_revision(
            character, revision, actor=actor,
        )
        return _decision_receipt_dict(receipt)

    def set_auto_evolution(
        self, character: str, enabled: bool, *, actor: str = "user",
    ) -> dict:
        """Persist the auto-evolution switch (default off; card untouched)."""
        settings = self._require_persona_admin().set_auto_evolution(
            character, enabled, actor=actor,
        )
        return settings.to_mapping()

    def auto_evolution(self, character: str) -> dict:
        """Current auto-evolution switch for one character."""
        return self._require_persona_admin().auto_evolution(character).to_mapping()

    # -- C02 projection / host edit / consumer adoption ------------------

    def persona_projection_status(self, character: str) -> dict:
        """Current projection, version and per-consumer effective status."""
        return self._require_persona_projection().status(character)

    def sync_persona_projection(self, character: str) -> bool:
        """Refresh the projection from the current card/published version."""
        return self._require_persona_projection().sync_from_card(character)

    def import_persona_card_from_host(
        self, character: str, host_card, *, base_revision: int = 0,
    ) -> dict:
        """One-time import of the host characters.json card (semantic fields only)."""
        receipt = self._require_persona_projection().import_from_host_card(
            character, host_card, base_revision=base_revision,
        )
        return _host_import_receipt_dict(receipt)

    def apply_host_persona_edit(self, character: str, request) -> dict:
        """Apply an explicit host edit with base_revision CAS; conflicts keep draft."""
        receipt = self._require_persona_projection().apply_host_edit(
            character, request,
        )
        return _host_edit_receipt_dict(receipt)

    def adopt_persona_projection(
        self, character: str, consumer: str, version: str, *,
        receipt_id: str | None = None, status: str = "adopted",
    ) -> dict:
        """Record that one consumer (chat/active/game/realtime) adopted a version."""
        receipt = self._require_persona_projection().adopt(
            character, consumer, version,
            receipt_id=receipt_id, status=status,
        )
        return _adopt_receipt_dict(receipt)

    def retry_persona_projection(self, character: str, consumer: str) -> dict:
        """Move one failed consumer back to pending (retry only unfinished work)."""
        return self._require_persona_projection().retry_failed(character, consumer)

    def resolve_authoritative_persona(self, character: str) -> dict | None:
        """Engine projection when it is authoritative; None tells the host to fall back."""
        return self._require_persona_projection().resolve_authoritative(character)

    def ensure_persona_for_consumer(self, character: str, consumer: str) -> dict | None:
        """Latest projection a consumer should use (None when unavailable)."""
        return self._require_persona_projection().ensure_for_consumer(
            character, consumer,
        )

    # -- C03 evolution mode / background scheduler -----------------------

    def evolution_mode(self, character: str) -> str:
        """Single durable mode: off / propose / auto."""
        return self._require_persona_admin().get_mode(character)

    def set_evolution_mode(
        self, character: str, mode: str, *, actor: str = "user",
    ) -> dict:
        """Set the durable evolution mode (auto must be explicit)."""
        settings = self._require_persona_admin().set_mode(
            character, mode, actor=actor,
        )
        return settings.to_mapping()

    async def evaluate_evolution(self, character: str) -> dict:
        """Run one background trigger evaluation (propose or auto-accept)."""
        result = await self._require_evolution_scheduler().evaluate(character)
        payload = result.to_mapping()
        payload["run"] = None if result.run is None else dict(result.run)
        return payload

    async def retry_evolution(self, character: str) -> int:
        """Retry pending/partially-published runs after a restart."""
        return await self._require_evolution_scheduler().retry_pending(character)

    def pending_evolution(self, character: str) -> list:
        """Runs that still need work (accepted but not published, retryable)."""
        return self._require_evolution_scheduler().pending(character)

    def evolution_runs(self, character: str, *, limit: int | None = None) -> list:
        """Recent background runs for the management panel."""
        return self._require_evolution_scheduler().list_runs(
            character, limit=limit,
        )

    # -- internals -------------------------------------------------------

    def _require_persona_projection(self) -> "PersonaProjectionService":
        if self._persona_projection is None:
            raise NotConfiguredError(
                "this MemoryEngine has no persona projection; wire it with "
                "build_memory_core (C02) instead of calling projection methods",
            )
        return self._persona_projection

    def _require_evolution_scheduler(self) -> "EvolutionScheduler":
        if self._evolution_scheduler is None:
            raise NotConfiguredError(
                "this MemoryEngine has no evolution scheduler; wire it with "
                "build_memory_core (C03) instead of calling scheduler methods",
            )
        return self._evolution_scheduler

    def _require_persona_admin(self) -> "PersonaAdminService":
        if self._persona_admin is None:
            raise NotConfiguredError(
                "this MemoryEngine has no persona admin service; wire it with "
                "build_memory_core (B13) instead of calling admin methods",
            )
        return self._persona_admin

    def _require_evolution(self) -> "EvolutionService":
        if self._evolution is None:
            raise NotConfiguredError(
                "this MemoryEngine has no evolution service; wire it with "
                "build_memory_core (B12) instead of calling evolve methods",
            )
        return self._evolution

    def _require_persona_publish(self) -> "PersonaPublishService":
        if self._persona_publish is None:
            raise NotConfiguredError(
                "this MemoryEngine has no persona publish service; wire it with "
                "build_memory_core (B11) instead of calling publish methods",
            )
        return self._persona_publish

    def _require_persona_card(self) -> "PersonaCardService":
        if self._persona_card is None:
            raise NotConfiguredError(
                "this MemoryEngine has no persona card; wire it with "
                "build_memory_core (B10) instead of calling card methods",
            )
        return self._persona_card

    def _require_outputs(self) -> "OutputService":
        if self._outputs is None:
            raise NotConfiguredError(
                "this MemoryEngine has no output service; wire it with "
                "build_memory_core (M5) instead of calling output methods",
            )
        return self._outputs

    def _require_working_summary(self) -> "WorkingSummaryService":
        if self._working_summary is None:
            raise NotConfiguredError(
                "this MemoryEngine has no working-summary projector; wire it "
                "with build_memory_core (M5)",
            )
        return self._working_summary

    def _require_state(self) -> "StateService":
        if self._state is None:
            raise NotConfiguredError(
                "this MemoryEngine has no state service; wire it with "
                "build_memory_core (B07) instead of calling state methods",
            )
        return self._state

    def _require_maintenance(self) -> "MaintenanceService":
        if self._maintenance is None:
            raise NotConfiguredError(
                "this MemoryEngine has no maintenance service; wire it with "
                "build_memory_core (B07) instead of calling maintain()",
            )
        return self._maintenance

    def _require_recall(self) -> "RecallService":
        if self._recall is None:
            raise NotConfiguredError(
                "this MemoryEngine has no recall service; wire it with "
                "build_memory_core (B06) instead of calling recall methods",
            )
        return self._recall

    def _require_corrections(self) -> "CorrectionService":
        if self._corrections is None:
            raise NotConfiguredError(
                "this MemoryEngine has no correction service; wire it with "
                "build_memory_core (B03) instead of calling correct() on a "
                "B00/B02 engine",
            )
        return self._corrections

    def _require_reflections(self) -> "ReflectionService":
        if self._reflections is None:
            raise NotConfiguredError(
                "this MemoryEngine has no reflection service; wire it with "
                "build_memory_core (B05) instead of calling reflection methods",
            )
        return self._reflections

    def _require_persona(self) -> "PersonaService":
        if self._persona is None:
            raise NotConfiguredError(
                "this MemoryEngine has no persona service; wire it with "
                "build_memory_core (B05) instead of calling persona methods",
            )
        return self._persona

    def _require_deletions(self) -> "DeletionService":
        if self._deletions is None:
            raise NotConfiguredError(
                "this MemoryEngine has no deletion service; wire it with "
                "build_memory_core (B03) instead of calling forget() on a "
                "B00/B02 engine",
            )
        return self._deletions


def _card_snapshot_dict(card) -> dict | None:
    if card is None:
        return None
    return {
        "character": card.character,
        "revision": card.revision,
        "initial_revision": card.initial_revision,
        "sections": [section.to_mapping() for section in card.sections],
    }


def _host_import_receipt_dict(receipt) -> dict:
    return {
        "status": receipt.status,
        "revision": receipt.revision,
        "version": receipt.version,
        "projection": dict(receipt.projection or {}),
        "text": receipt.text,
        "duplicate": receipt.duplicate,
        "reason": receipt.reason,
        "host_import": dict(receipt.host_import or {}),
        "card": _card_snapshot_dict(receipt.card),
    }


def _host_edit_receipt_dict(receipt) -> dict:
    return {
        "status": receipt.status,
        "revision_before": receipt.revision_before,
        "revision_after": receipt.revision_after,
        "version": receipt.version,
        "projection": dict(receipt.projection or {}),
        "text": receipt.text,
        "reason": receipt.reason,
        "conflict": receipt.conflict,
        "draft": None if receipt.draft is None else dict(receipt.draft),
        "card": _card_snapshot_dict(receipt.card),
    }


def _adopt_receipt_dict(receipt) -> dict:
    return {
        "status": receipt.status,
        "consumer": receipt.consumer,
        "expected_version": receipt.expected_version,
        "version": receipt.version,
        "revision": receipt.revision,
        "projection": dict(receipt.projection or {}),
        "text": receipt.text,
        "needs_reissue": receipt.needs_reissue,
        "reason": receipt.reason,
        "consumer_state": (None if receipt.consumer_state is None
                           else dict(receipt.consumer_state)),
    }


def _decision_receipt_dict(receipt) -> dict:
    card = receipt.card
    return {
        "status": receipt.status,
        "decision_id": receipt.decision_id,
        "proposal_id": receipt.proposal_id,
        "action": receipt.action,
        "revision_before": receipt.revision_before,
        "revision_after": receipt.revision_after,
        "reason": receipt.reason,
        "conflict": receipt.conflict,
        "duplicate": receipt.duplicate,
        "decision": None if receipt.decision is None else dict(receipt.decision),
        "card": None if card is None else {
            "character": card.character,
            "revision": card.revision,
            "initial_revision": card.initial_revision,
            "sections": [section.to_mapping() for section in card.sections],
        },
    }


def _proposal_receipt_dict(receipt) -> dict:
    return {
        "status": receipt.status,
        "proposal_id": receipt.proposal_id,
        "validation": dict(receipt.validation or {}),
        "reason": receipt.reason,
        "proposal": receipt.proposal,
    }


def _publish_snapshot_dict(snapshot) -> dict:
    return {
        "available": snapshot.available,
        "version": snapshot.version,
        "revision": snapshot.revision,
        "text": snapshot.text,
        "projection": dict(snapshot.projection or {}),
        "published_at": snapshot.published_at,
        "target": snapshot.target,
        "boundary": dict(snapshot.boundary or {}),
        "text_sha256": snapshot.text_sha256,
        "planner_version": snapshot.planner_version,
        "request_id": snapshot.request_id,
    }


def _publish_receipt_dict(receipt) -> dict:
    return {
        "status": receipt.status,
        "version": receipt.version,
        "revision": receipt.revision,
        "target": receipt.target,
        "published_at": receipt.published_at,
        "boundary": dict(receipt.boundary or {}),
        "conflict": receipt.conflict,
        "duplicate": receipt.duplicate,
        "reason": receipt.reason,
        "snapshot": None if receipt.snapshot is None
        else _publish_snapshot_dict(receipt.snapshot),
    }


def _revision_receipt_dict(receipt) -> dict:
    """Serialize one RevisionReceipt for the wire/adapters."""
    card = receipt.card
    return {
        "status": receipt.status,
        "revision": receipt.revision,
        "base_revision": receipt.base_revision,
        "applied_ops": receipt.applied_ops,
        "reason": receipt.reason,
        "conflict": receipt.conflict,
        "duplicate": receipt.duplicate,
        "archived": receipt.archived,
        "changed_sections": list(receipt.changed_sections),
        "card": None if card is None else {
            "character": card.character,
            "revision": card.revision,
            "initial_revision": card.initial_revision,
            "sections": [section.to_mapping() for section in card.sections],
        },
    }


def _scene_from_payload(payload) -> Scene:
    """Map a stored applies_to mapping onto the frozen Scene contract."""
    if not isinstance(payload, Mapping):
        return Scene()
    return Scene(
        plugin_id=str(payload.get("plugin_id") or ""),
        game_id=str(payload.get("game_id") or ""),
        session_id=str(payload.get("session_id") or ""),
    )


__all__ = ["MemoryEngine"]
