"""C03 integration: background evolution modes through composition + API.

Covers the real build_memory_core wiring (evolution service, admin mode store,
run store, scheduler, maintenance) and the MemoryEngine C03 methods. Evidence
comes from the default composition provider (facts), not an injected fake.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion_memory.adapters.composition import build_memory_core
from companion_memory.api import MemoryEngine
from companion_memory.domain.evolution import EvolutionSettings
from companion_memory.domain.evolution_scheduler import SchedulerSettings


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)
        self._mono += 3600.0


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


class SchedulerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory", *, model=None, reply=PROPOSAL,
              cooldown=30.0):
        model = model or FakeModel(reply)
        core = build_memory_core(
            memory_root=self.root / name,
            clock=self.clock,
            model=model,
            evolution_settings=EvolutionSettings(
                enabled=True, tier="summary", timeout_s=5,
                max_output_tokens=300,
            ),
            scheduler_settings=SchedulerSettings(
                cooldown_minutes=cooldown, daily_budget=8,
                publish_target="next_turn",
            ),
        )
        return core, model

    def prepare(self, core, *, facts=None):
        core.recover("demo")
        core.persona_card_service.import_initial("demo", INITIAL)
        rows = facts if facts is not None else [{
            "id": "fact_a", "text": "用户喜欢温柔的语气", "importance": 7,
            "schema_version": 2, "absorbed": False,
            "source_event_keys": ["host:demo:private:a"],
        }]
        core.facts.save("demo", rows)
        return core

    def engine(self, core):
        return MemoryEngine(
            events=core.sink, profiles=None, context=None,
            persona_card=core.persona_card_service,
            persona_publish=core.persona_publish,
            evolution=core.evolution_service,
            persona_admin=core.persona_admin_service,
            evolution_scheduler=core.evolution_scheduler,
            maintenance=core.maintenance,
        )

    async def test_default_off_skips_without_model_or_card_write(self):
        core, model = self.build()
        self.prepare(core)
        engine = self.engine(core)
        self.assertEqual(engine.evolution_mode("demo"), "off")
        result = await engine.evaluate_evolution("demo")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "off")
        self.assertEqual(model.calls, [])
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)
        self.assertEqual(engine.evolution_runs("demo"), [])
        self.assertEqual(engine.pending_evolution("demo"), [])

    async def test_propose_creates_proposal_and_dedupes_trigger(self):
        core, model = self.build()
        self.prepare(core)
        engine = self.engine(core)
        engine.set_evolution_mode("demo", "propose")
        result = await engine.evaluate_evolution("demo")
        self.assertEqual(result["status"], "proposed")
        self.assertTrue(result["proposal_id"])
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)
        self.assertEqual(len(model.calls), 1)
        proposal = core.evolution_service.get("demo", result["proposal_id"])
        self.assertEqual(proposal["status"], "proposed")
        runs = engine.evolution_runs("demo")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "proposed")
        self.assertEqual(engine.pending_evolution("demo"), [])

        duplicate = await engine.evaluate_evolution("demo")
        self.assertEqual(duplicate["status"], "skipped")
        self.assertEqual(duplicate["reason"], "duplicate")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(engine.evolution_runs("demo")), 1)

    async def test_auto_accepts_and_publishes_new_card_revision(self):
        core, model = self.build()
        self.prepare(core)
        engine = self.engine(core)
        engine.set_evolution_mode("demo", "auto")
        result = await engine.evaluate_evolution("demo")
        self.assertEqual(result["status"], "published")
        self.assertEqual(core.persona_card_service.get("demo").revision, 2)
        current = engine.current_persona_version("demo")
        self.assertEqual(current["revision"], 2)
        self.assertEqual(len(current["version"]), 24)
        proposal = core.evolution_service.get("demo", result["proposal_id"])
        self.assertEqual(proposal["status"], "accepted")
        run = engine.evolution_runs("demo")[0]
        self.assertEqual(run["status"], "published")
        self.assertEqual(run["published_version"], current["version"])
        self.assertEqual(engine.pending_evolution("demo"), [])
        self.assertEqual(len(model.calls), 1)

    async def test_cooldown_blocks_then_allows_new_evidence(self):
        core, model = self.build()
        self.prepare(core)
        engine = self.engine(core)
        engine.set_evolution_mode("demo", "propose")
        first = await engine.evaluate_evolution("demo")
        self.assertEqual(first["status"], "proposed")

        core.facts.save("demo", [
            {"id": "fact_a", "text": "用户喜欢温柔的语气", "importance": 7,
             "schema_version": 2, "absorbed": False,
             "source_event_keys": ["host:demo:private:a"]},
            {"id": "fact_b", "text": "用户周末喜欢跑步", "importance": 6,
             "schema_version": 2, "absorbed": False,
             "source_event_keys": ["host:demo:private:b"]},
        ])
        blocked = await engine.evaluate_evolution("demo")
        self.assertEqual(blocked["status"], "skipped")
        self.assertEqual(blocked["reason"], "cooldown")
        self.assertEqual(len(model.calls), 1)

        self.clock.advance(minutes=31)
        allowed = await engine.evaluate_evolution("demo")
        self.assertEqual(allowed["status"], "proposed")
        self.assertEqual(len(model.calls), 2)

    async def test_frozen_section_and_off_mode_intercept(self):
        core, model = self.build()
        self.prepare(core)
        engine = self.engine(core)
        engine.set_evolution_mode("demo", "auto")
        core.persona_card_service.freeze("demo", "tone", True)
        frozen = await engine.evaluate_evolution("demo")
        self.assertEqual(frozen["status"], "skipped")
        self.assertEqual(frozen["reason"], "section_frozen")
        self.assertEqual(model.calls, [])
        self.assertEqual(core.persona_card_service.get("demo").revision, 2)

        engine.set_evolution_mode("demo", "off")
        off = await engine.evaluate_evolution("demo")
        self.assertEqual(off["status"], "skipped")
        self.assertEqual(off["reason"], "off")

    async def test_invalid_model_output_fails_then_retry_publishes(self):
        core, model = self.build(reply="not json")
        self.prepare(core)
        engine = self.engine(core)
        engine.set_evolution_mode("demo", "auto")
        failed = await engine.evaluate_evolution("demo")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["reason"], "invalid_model_output")
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)
        self.assertEqual(len(engine.pending_evolution("demo")), 1)

        self.clock.advance(minutes=31)
        model.reply = PROPOSAL
        retried = await engine.retry_evolution("demo")
        self.assertEqual(retried, 1)
        self.assertEqual(core.persona_card_service.get("demo").revision, 2)
        self.assertEqual(engine.pending_evolution("demo"), [])
        self.assertEqual(engine.evolution_runs("demo")[0]["status"], "published")

    async def test_restart_preserves_pending_and_published_state(self):
        core, model = self.build(reply="not json")
        self.prepare(core)
        engine = self.engine(core)
        engine.set_evolution_mode("demo", "auto")
        failed = await engine.evaluate_evolution("demo")
        self.assertEqual(failed["status"], "failed")

        self.clock.advance(minutes=31)
        reopened, model2 = self.build(name="memory")
        reopened.recover("demo")
        engine2 = self.engine(reopened)
        self.assertEqual(engine2.evolution_mode("demo"), "auto")
        self.assertEqual(len(engine2.pending_evolution("demo")), 1)
        retried = await engine2.retry_evolution("demo")
        self.assertEqual(retried, 1)
        self.assertEqual(reopened.persona_card_service.get("demo").revision, 2)

        again, _ = self.build(name="memory")
        again.recover("demo")
        engine3 = self.engine(again)
        self.assertEqual(engine3.pending_evolution("demo"), [])
        self.assertEqual(engine3.evolution_runs("demo")[0]["status"], "published")
        self.assertEqual(
            again.persona_card_service.get("demo").revision, 2,
        )
        self.assertEqual(engine3.current_persona_version("demo")["revision"], 2)

    async def test_legacy_switch_migrates_and_maintenance_has_step(self):
        core, model = self.build()
        self.prepare(core)
        engine = self.engine(core)
        self.assertEqual(engine.evolution_mode("demo"), "off")
        migrated = engine.set_auto_evolution("demo", True)
        self.assertEqual(migrated["mode"], "propose")
        self.assertEqual(engine.evolution_mode("demo"), "propose")
        disabled = engine.set_auto_evolution("demo", False)
        self.assertEqual(disabled["mode"], "off")
        self.assertEqual(engine.evolution_mode("demo"), "off")

        maintained = await core.maintenance.run_once("demo")
        self.assertIn("evolution_trigger", maintained.steps)
        self.assertEqual(
            maintained.steps["evolution_trigger"]["status"], "skipped",
        )
        self.assertIn("off", maintained.steps["evolution_trigger"]["detail"])

    async def test_hand_edit_same_evidence_does_not_repropose(self):
        core, model = self.build()
        self.prepare(core)
        engine = self.engine(core)
        engine.set_evolution_mode("demo", "auto")
        first = await engine.evaluate_evolution("demo")
        self.assertEqual(first["status"], "published")
        self.assertEqual(core.persona_card_service.get("demo").revision, 2)

        core.persona_card_service.revise("demo", {
            "base_revision": 2,
            "operations": [{
                "op": "replace", "section_id": "tone",
                "text": "用户手动改写的语气。",
            }],
        })
        self.assertEqual(core.persona_card_service.get("demo").revision, 3)

        duplicate = await engine.evaluate_evolution("demo")
        self.assertEqual(duplicate["status"], "skipped")
        self.assertEqual(duplicate["reason"], "duplicate_evidence")
        self.assertEqual(core.persona_card_service.get("demo").revision, 3)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(engine.evolution_runs("demo")), 1)

    async def test_accepted_publish_failure_retries_even_when_mode_off(self):
        core, model = self.build(cooldown=0)
        self.prepare(core)
        engine = self.engine(core)
        engine.set_evolution_mode("demo", "auto")

        class FailedPublish:
            status = "failed"
            reason = "publish_timeout"

        real_publish = core.persona_publish.publish
        core.persona_publish.publish = lambda *args, **kwargs: FailedPublish()
        failed = await engine.evaluate_evolution("demo")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["reason"], "publish_timeout")
        self.assertEqual(core.persona_card_service.get("demo").revision, 2)
        self.assertEqual(len(engine.pending_evolution("demo")), 1)

        core.persona_publish.publish = real_publish
        engine.set_evolution_mode("demo", "off")
        retried = await engine.retry_evolution("demo")
        self.assertEqual(retried, 1)
        self.assertEqual(core.persona_card_service.get("demo").revision, 2)
        self.assertEqual(engine.pending_evolution("demo"), [])
        self.assertEqual(engine.evolution_runs("demo")[0]["status"], "published")

    async def test_evidence_provider_error_is_failed_and_retryable(self):
        core, model = self.build(cooldown=0)
        self.prepare(core)
        engine = self.engine(core)
        engine.set_evolution_mode("demo", "auto")
        provider = core.evolution_scheduler._evidence_provider

        def unavailable(*args, **kwargs):
            raise RuntimeError("facts unavailable")

        core.evolution_scheduler._evidence_provider = unavailable
        failed = await engine.evaluate_evolution("demo")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["reason"], "evidence_error")
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)
        self.assertEqual(len(engine.pending_evolution("demo")), 1)
        self.assertEqual(model.calls, [])

        core.evolution_scheduler._evidence_provider = provider
        retried = await engine.retry_evolution("demo")
        self.assertEqual(retried, 1)
        self.assertEqual(core.persona_card_service.get("demo").revision, 2)
        self.assertEqual(engine.pending_evolution("demo"), [])

    async def test_mode_off_cancels_failed_pending(self):
        core, model = self.build(cooldown=0, reply="not json")
        self.prepare(core)
        engine = self.engine(core)
        engine.set_evolution_mode("demo", "auto")
        failed = await engine.evaluate_evolution("demo")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(engine.pending_evolution("demo")), 1)

        engine.set_evolution_mode("demo", "off")
        retried = await engine.retry_evolution("demo")
        self.assertEqual(retried, 0)
        self.assertEqual(engine.pending_evolution("demo"), [])
        self.assertEqual(engine.evolution_runs("demo")[0]["status"], "skipped")
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)


if __name__ == "__main__":
    unittest.main()
