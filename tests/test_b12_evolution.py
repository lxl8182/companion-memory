"""B12 acceptance: evidence-driven persona revision proposals.

The model may only propose; deterministic validation decides whether a proposal
is stored. The card is never written by this path, the feature is off by
default, evidence must exist and survive the deletion barrier, frozen or
non-evolvable sections are rejected, and request ids are idempotent.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion_memory.adapters.composition import build_memory_core
from companion_memory.api import MemoryEngine
from companion_memory.contracts import DeletionRequest, DeletionTarget, Scope
from companion_memory.domain.evolution import EvolutionSettings
from companion_memory.errors import NotConfiguredError


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


class FakeModel:
    def __init__(self, reply="", error=None) -> None:
        self.reply = reply
        self.error = error
        self.calls = []

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({
            "tier": tier, "purpose": purpose,
            "max_output_tokens": max_output_tokens, "timeout_s": timeout_s,
        })
        if self.error is not None:
            raise self.error
        return self.reply


INITIAL = [
    {"section_id": "identity", "text": "你是一只猫娘。",
     "editable_by": {"user": True, "evolution": False}},
    {"section_id": "tone", "text": "说话温柔。",
     "source_refs": ["fact_a"],
     "editable_by": {"user": True, "evolution": True}},
]

PROPOSAL = json.dumps({
    "operations": [{"op": "replace", "section_id": "tone",
                    "text": "用户喜欢温柔、具体的语气。"}],
    "reason": "证据显示用户偏好具体的温柔表达",
}, ensure_ascii=False)


class EvolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory", *, enabled=True, model=None):
        return build_memory_core(
            memory_root=self.root / name, clock=self.clock, model=model,
            evolution_settings=EvolutionSettings(
                enabled=enabled, tier="summary", timeout_s=5,
                max_output_tokens=300,
            ),
        )

    async def prepare(self, core, *, sections=None, fact=True, understanding=True):
        """Seed evidence: a raw fact plus one *mature* understanding.

        M4 (design 9.3 / G15): a raw fact may explain a proposal but must never
        qualify for one, so a pool of facts only is rejected as
        `fact_not_eligible`. The understanding below is what a real conversation
        would have produced through the growth gate; the fact stays in the pool
        as explanation, which is also what G16 exercises.
        """
        core.recover("demo")
        core.persona_card_service.import_initial("demo", sections or INITIAL)
        if fact:
            core.facts.save("demo", [{
                "id": "fact_a", "text": "用户喜欢温柔的语气", "importance": 7,
                "schema_version": 2, "absorbed": False,
                "source_event_keys": ["host:demo:private:a"],
            }])
        if understanding:
            # Two *independent* experiences (two episodes), so the maturity gate
            # accepts it. A single-source or basis-less "confirmed" row is
            # deliberately not eligible (design 9.3): status alone is not proof,
            # because the growth path and the evidence-score path confirm rows by
            # different routes.
            core.reflections.save("demo", [{
                "id": "ref_understanding",
                "text": "用户喜欢温柔、具体的语气",
                "status": "confirmed",
                "entity": "relationship",
                "source_fact_ids": ["fact_a", "fact_b"],
                "source_event_keys": [
                    "host:demo:private:a", "host:demo:private:b",
                ],
                "support_basis": [
                    {"experience": "episode:e1", "facts": ["fact_a"],
                     "support": [{"key": "host:demo:private:a",
                                  "fact_id": "fact_a",
                                  "kind": "explicit_feedback",
                                  "flags": ["user_feedback"]}]},
                    {"experience": "episode:e2", "facts": ["fact_b"],
                     "support": [{"key": "host:demo:private:b",
                                  "fact_id": "fact_b",
                                  "kind": "observed_action",
                                  "flags": ["actual_result", "real_contact"]}]},
                ],
                "created_at": "2026-10-20T00:00:00+00:00",
            }])
        return 1

    async def test_disabled_by_default(self):
        model = FakeModel(PROPOSAL)
        core = self.build(enabled=False, model=model)
        await self.prepare(core)
        receipt = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
        })
        self.assertEqual(receipt.status, "disabled")
        self.assertEqual(receipt.reason, "disabled")
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)
        self.assertEqual(model.calls, [])
        self.assertEqual(core.evolution_service.list("demo"), [])

    async def test_valid_proposal_stored_without_card_write(self):
        model = FakeModel(PROPOSAL)
        core = self.build(model=model)
        await self.prepare(core)
        receipt = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
            "reason": "review",
        })
        self.assertEqual(receipt.status, "proposed")
        self.assertTrue(receipt.proposal_id.startswith("evo_"))
        stored = core.evolution_service.get("demo", receipt.proposal_id)
        self.assertEqual(stored["status"], "proposed")
        self.assertEqual(stored["base_revision"], 1)
        # G16: the understanding reached the proposal with its origin intact,
        # and the raw fact is still carried as an explanation source.
        self.assertIn("fact_a", stored["evidence"]["source_refs"])
        self.assertIn("ref_understanding", stored["evidence"]["reflection_ids"])
        self.assertIn("fact_a", stored["evidence"]["fact_ids"])
        self.assertIn("fact_b", stored["evidence"]["fact_ids"])
        self.assertEqual(model.calls[0]["purpose"], "persona_evolution_proposal")
        self.assertEqual(model.calls[0]["tier"], "summary")
        # The card itself is untouched until a human accepts the proposal.
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)
        self.assertEqual(
            core.persona_card_service.render_projection("demo")["tone"],
            "说话温柔。",
        )
        duplicate = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
            "request_id": "req-1",
        })
        self.assertEqual(duplicate.status, "proposed")
        again = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
            "request_id": "req-1",
        })
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(again.proposal_id, duplicate.proposal_id)
        self.assertEqual(len(core.evolution_service.list("demo")), 2)

    async def test_evidence_and_section_gates(self):
        model = FakeModel(PROPOSAL)
        core = self.build(model=model)
        await self.prepare(core)
        missing = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_missing"],
        })
        self.assertEqual(missing.status, "rejected")
        self.assertEqual(missing.reason, "insufficient_evidence")

        frozen = core.persona_card_service.freeze("demo", "tone", True)
        blocked = await core.evolution_service.propose("demo", {
            "base_revision": frozen.revision, "source_refs": ["fact_a"],
        })
        self.assertEqual(blocked.status, "rejected")
        self.assertEqual(blocked.reason, "section_frozen")

        identity_only = await core.evolution_service.propose("demo", {
            "base_revision": frozen.revision, "source_refs": ["fact_a"],
            "focus_sections": ["identity"],
        })
        self.assertEqual(identity_only.status, "rejected")
        self.assertEqual(identity_only.reason, "section_not_editable")

        stale = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
        })
        self.assertEqual(stale.status, "rejected")
        self.assertEqual(stale.reason, "base_revision_conflict")

    async def test_deleted_source_rejects_and_cleans_proposals(self):
        model = FakeModel(PROPOSAL)
        core = self.build(model=model)
        await self.prepare(core)
        receipt = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
        })
        self.assertEqual(receipt.status, "proposed")
        deleted = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            targets=(DeletionTarget("fact", "fact_a"),),
        ))
        self.assertEqual(deleted.status, "completed")
        rejected = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
        })
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.reason, "insufficient_evidence")
        # Stored proposal can no longer be accepted by B13; its sources are gone.
        after = core.evolution_service.list("demo")
        self.assertTrue(
            all(item["status"] != "proposed" for item in after)
            or after == [],
        )

    async def test_invalid_model_output_timeout_and_expire(self):
        bad = FakeModel("not-json")
        core = self.build(model=bad)
        await self.prepare(core)
        invalid = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
        })
        self.assertEqual(invalid.status, "rejected")
        self.assertEqual(invalid.reason, "invalid_model_output")

        timeout = FakeModel(error=TimeoutError("slow"))
        core2 = self.build(name="memory2", model=timeout)
        await self.prepare(core2)
        timed = await core2.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
        })
        self.assertEqual(timed.status, "rejected")
        self.assertEqual(timed.reason, "timeout")

        good = FakeModel(PROPOSAL)
        core3 = self.build(name="memory3", model=good)
        await self.prepare(core3)
        proposal = await core3.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
        })
        self.assertEqual(proposal.status, "proposed")
        self.clock.advance(hours=100)
        expired = core3.evolution_service.expire_stale("demo")
        self.assertEqual(expired, 1)
        self.assertEqual(
            core3.evolution_service.get("demo", proposal.proposal_id)["status"],
            "expired",
        )
        withdrawn = core3.evolution_service.withdraw("demo", proposal.proposal_id)
        self.assertIsNotNone(withdrawn)

    async def test_engine_evolution_api_and_not_configured(self):
        model = FakeModel(PROPOSAL)
        core = self.build(model=model)
        await self.prepare(core)
        engine = MemoryEngine(
            events=core.sink, profiles=None, context=None,
            evolution=core.evolution_service,
        )
        receipt = await engine.evolve_persona("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
        })
        self.assertEqual(receipt["status"], "proposed")
        self.assertEqual(len(engine.persona_evolution_proposals("demo")), 1)
        self.assertEqual(
            engine.get_persona_evolution_proposal(
                "demo", receipt["proposal_id"],
            )["status"],
            "proposed",
        )
        bare = MemoryEngine(events=None, profiles=None, context=None)
        with self.assertRaises(NotConfiguredError):
            await bare.evolve_persona("demo")


if __name__ == "__main__":
    unittest.main()