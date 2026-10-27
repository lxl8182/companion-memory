"""B13 acceptance: evolution review, withdraw, diff, rollback and switch.

The admin layer never writes the card directly: accepting a proposal or rolling
back a revision goes through PersonaCardService, so base_revision CAS, frozen
sections, source validation and history filtering stay in one place. Rejecting
or withdrawing only changes the proposal. The auto-evolution switch is persisted
separately and defaults to OFF.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.api import MemoryEngine
from rin_memory.contracts import DeletionRequest, DeletionTarget, Scope
from rin_memory.domain.evolution import EvolutionSettings
from rin_memory.errors import NotConfiguredError


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class FakeModel:
    def __init__(self, reply="") -> None:
        self.reply = reply
        self.calls = 0

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls += 1
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


class AdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory", *, enabled=True, model=None):
        return build_memory_core(
            memory_root=self.root / name, clock=self.clock, model=model,
            evolution_settings=EvolutionSettings(enabled=enabled),
        )

    async def prepare(self, core):
        """Seed one raw fact (explanation) and one mature understanding.

        M4 (design 9.3): a pool of raw facts is rejected as
        `fact_not_eligible`, so the proposal has to rest on evidence that passed
        the maturity gate - two independent experiences here. The fact stays in
        the pool as the explanation source and is still available to the admin
        layer's source checks.
        """
        core.recover("demo")
        core.persona_card_service.import_initial("demo", INITIAL)
        core.facts.save("demo", [{
            "id": "fact_a", "text": "用户喜欢温柔的语气", "importance": 7,
            "schema_version": 2, "absorbed": False,
            "source_event_keys": ["host:demo:private:a"],
        }])
        core.reflections.save("demo", [{
            "id": "ref_understanding",
            "text": "用户喜欢温柔、具体的语气",
            "status": "confirmed",
            "entity": "relationship",
            "source_fact_ids": ["fact_a"],
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
        return await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["ref_understanding"],
        })

    async def test_accept_proposal_revises_card_and_records_decision(self):
        model = FakeModel(PROPOSAL)
        core = self.build(model=model)
        proposal = await self.prepare(core)
        self.assertEqual(proposal.status, "proposed")
        receipt = core.persona_admin_service.accept_proposal(
            "demo", proposal.proposal_id, reason="review ok",
        )
        self.assertEqual(receipt.status, "accepted")
        self.assertEqual(receipt.revision_before, 1)
        self.assertEqual(receipt.revision_after, 2)
        self.assertTrue(receipt.decision_id)
        self.assertEqual(
            core.persona_card_service.render_projection("demo")["tone"],
            "用户喜欢温柔、具体的语气。",
        )
        stored = core.evolution_service.get("demo", proposal.proposal_id)
        self.assertEqual(stored["status"], "accepted")
        self.assertEqual(
            core.persona_admin_service.get_proposal("demo", proposal.proposal_id)["status"],
            "accepted",
        )
        snapshot = core.persona_admin_service.snapshot("demo")
        self.assertEqual(snapshot.card.revision, 2)
        self.assertEqual(snapshot.pending_proposals, 0)
        self.assertEqual(len(snapshot.decisions), 1)

    async def test_accept_failure_keeps_proposal(self):
        model = FakeModel(PROPOSAL)
        core = self.build(model=model)
        proposal = await self.prepare(core)
        frozen = core.persona_card_service.freeze("demo", "tone", True)
        receipt = core.persona_admin_service.accept_proposal(
            "demo", proposal.proposal_id,
        )
        self.assertEqual(receipt.status, "conflict")
        self.assertEqual(receipt.reason, "base_revision_conflict")
        self.assertEqual(
            core.evolution_service.get("demo", proposal.proposal_id)["status"],
            "proposed",
        )
        self.assertEqual(core.persona_card_service.get("demo").revision,
                         frozen.revision)
        missing = core.persona_admin_service.accept_proposal("demo", "evo_missing")
        self.assertEqual(missing.status, "rejected")

    async def test_reject_withdraw_and_switch(self):
        model = FakeModel(PROPOSAL)
        core = self.build(model=model)
        first = await self.prepare(core)
        second = await core.evolution_service.propose("demo", {
            "base_revision": 1, "source_refs": ["fact_a"],
            "reason": "second",
        })
        rejected = core.persona_admin_service.reject_proposal(
            "demo", first.proposal_id, reason="not now",
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.action, "rejected")
        self.assertEqual(
            core.evolution_service.get("demo", first.proposal_id)["status"],
            "rejected",
        )
        withdrawn = core.persona_admin_service.withdraw_proposal(
            "demo", second.proposal_id, reason="superseded",
        )
        self.assertEqual(withdrawn.status, "withdrawn")
        self.assertEqual(
            core.evolution_service.get("demo", second.proposal_id)["status"],
            "withdrawn",
        )
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)

        self.assertFalse(core.persona_admin_service.auto_evolution("demo").enabled)
        turned_on = core.persona_admin_service.set_auto_evolution("demo", True)
        self.assertTrue(turned_on.enabled)
        self.assertTrue(core.persona_admin_service.auto_evolution("demo").enabled)
        reopened = self.build(name="memory")
        reopened.recover("demo")
        self.assertTrue(
            reopened.persona_admin_service.auto_evolution("demo").enabled,
        )
        self.assertEqual(reopened.persona_card_service.get("demo").revision, 1)

    async def test_revisions_diff_and_rollback(self):
        model = FakeModel(PROPOSAL)
        core = self.build(model=model)
        proposal = await self.prepare(core)
        accepted = core.persona_admin_service.accept_proposal(
            "demo", proposal.proposal_id,
        )
        self.assertEqual(accepted.status, "accepted")
        revisions = core.persona_admin_service.revisions("demo")
        self.assertGreaterEqual(len(revisions), 2)
        diff = core.persona_admin_service.diff("demo", 1, 2)
        self.assertTrue(diff.available)
        changed_ids = {item["section_id"] for item in diff.changed}
        self.assertIn("tone", changed_ids)
        rolled = core.persona_admin_service.rollback_to_revision("demo", 1)
        self.assertEqual(rolled.status, "rolled_back")
        self.assertGreater(rolled.revision_after, accepted.revision_after)
        self.assertEqual(
            core.persona_card_service.render_projection("demo")["tone"],
            "说话温柔。",
        )
        self.assertEqual(core.persona_card_service.get("demo").revision,
                         rolled.revision_after)

    async def test_deletion_withdraws_proposals(self):
        model = FakeModel(PROPOSAL)
        core = self.build(model=model)
        proposal = await self.prepare(core)
        self.assertEqual(proposal.status, "proposed")
        receipt = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            targets=(DeletionTarget("fact", "fact_a"),),
        ))
        self.assertEqual(receipt.status, "completed")
        self.assertEqual(
            core.persona_admin_service.list_proposals("demo"), [],
        )
        self.assertIsNone(
            core.persona_admin_service.get_proposal("demo", proposal.proposal_id),
        )
        # The card section that cited the deleted fact is gone as well.
        self.assertNotIn(
            "tone", core.persona_card_service.render_projection("demo"),
        )

    async def test_engine_admin_api_and_not_configured(self):
        model = FakeModel(PROPOSAL)
        core = self.build(model=model)
        proposal = await self.prepare(core)
        engine = MemoryEngine(
            events=core.sink, profiles=None, context=None,
            persona_admin=core.persona_admin_service,
        )
        snapshot = engine.persona_admin_snapshot("demo")
        self.assertEqual(snapshot["pending_proposals"], 1)
        self.assertFalse(snapshot["auto_evolution"]["enabled"])
        accepted = engine.accept_persona_proposal(
            "demo", proposal.proposal_id, reason="engine ok",
        )
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(
            engine.persona_card_revisions("demo")[0]["revision"],
            accepted["revision_after"],
        )
        self.assertTrue(engine.persona_card_diff("demo", 1, 2)["available"])
        rolled = engine.rollback_persona_card_revision("demo", 1)
        self.assertEqual(rolled["status"], "rolled_back")
        toggled = engine.set_auto_evolution("demo", True)
        self.assertTrue(toggled["enabled"])
        self.assertTrue(engine.auto_evolution("demo")["enabled"])
        bare = MemoryEngine(events=None, profiles=None, context=None)
        with self.assertRaises(NotConfiguredError):
            bare.persona_admin_snapshot("demo")


if __name__ == "__main__":
    unittest.main()