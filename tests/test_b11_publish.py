"""B11 acceptance: persona publish, version tokens and consumer boundaries.

A revision is not "in effect" until it is published as a persisted version; each
target records its safe boundary (next request / next generation / next planner
round), consumers must report the version they actually rendered, game planner
polls pair against the published version, and deletion/correction invalidates
versions whose sources are gone.
"""

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.api import MemoryEngine
from rin_memory.contracts import DeletionRequest, DeletionTarget, Scope
from rin_memory.errors import NotConfiguredError


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


INITIAL = [
    {"section_id": "identity", "text": "你是一只猫娘。",
     "editable_by": {"user": True, "evolution": False}},
    {"section_id": "tone", "text": "说话温柔。",
     "editable_by": {"user": True, "evolution": True}},
]


class PersonaPublishTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory"):
        return build_memory_core(memory_root=self.root / name, clock=self.clock)

    def prepare(self, core):
        core.recover("demo")
        core.persona_card_service.import_initial("demo", INITIAL)
        receipt = core.persona_card_service.revise("demo", {
            "base_revision": 1,
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "说话温柔，偶尔开玩笑。"}],
        })
        self.assertEqual(receipt.status, "completed")
        return receipt.revision

    async def test_publish_next_turn_token_and_duplicate(self):
        core = self.build()
        revision = self.prepare(core)
        published = core.persona_publish.publish(
            "demo", target="next_turn", revision=revision,
        )
        self.assertEqual(published.status, "published")
        self.assertEqual(published.revision, revision)
        self.assertEqual(published.target, "next_turn")
        self.assertEqual(published.boundary.get("safe_boundary"), "next_request")
        text = core.persona_card_service.render_text("demo")
        expected = hashlib.sha1(
            f"{revision}:{text}".encode("utf-8"),
        ).hexdigest()[:24]
        self.assertEqual(published.version, expected)

        current = core.persona_publish.current("demo")
        self.assertTrue(current.available)
        self.assertEqual(current.version, published.version)
        self.assertEqual(current.text, text)
        self.assertIn("tone", current.projection)

        duplicate = core.persona_publish.publish(
            "demo", target="next_turn", revision=revision,
        )
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(duplicate.version, published.version)

    async def test_realtime_boundary_and_consumer_superseded(self):
        core = self.build()
        revision = self.prepare(core)
        first = core.persona_publish.publish(
            "demo", target="realtime", revision=revision,
        )
        self.assertEqual(first.boundary.get("safe_boundary"), "next_generation")
        matched = core.persona_publish.consume(
            "demo", "realtime:session-a", expected_version=first.version,
        )
        self.assertEqual(matched.status, "match")
        self.assertEqual(matched.version, first.version)

        newer = core.persona_card_service.revise("demo", {
            "base_revision": revision,
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "说话非常温柔。"}],
        })
        republished = core.persona_publish.publish(
            "demo", target="realtime", revision=newer.revision,
        )
        superseded = core.persona_publish.consume(
            "demo", "realtime:session-a", expected_version=first.version,
        )
        self.assertEqual(superseded.status, "superseded")
        self.assertTrue(superseded.needs_reissue)
        self.assertEqual(superseded.snapshot.version, republished.version)

    async def test_game_planner_pairing(self):
        core = self.build()
        revision = self.prepare(core)
        published = core.persona_publish.publish(
            "demo", target="game", revision=revision,
        )
        self.assertEqual(published.boundary.get("safe_boundary"), "next_plan")
        self.assertTrue(published.snapshot.planner_version)

        matched = core.persona_publish.pair_planner(
            "demo", published.snapshot.planner_version,
        )
        self.assertEqual(matched["status"], "matched")
        self.assertEqual(matched["version"], published.version)
        stale = core.persona_publish.pair_planner("demo", "planner:stale")
        self.assertEqual(stale["status"], "reissue")
        self.assertEqual(stale["snapshot"].version, published.version)

    async def test_rollback_conflict_and_allow_consumed(self):
        core = self.build()
        revision = self.prepare(core)
        first = core.persona_publish.publish(
            "demo", target="next_turn", revision=revision,
        )
        second_revision = core.persona_card_service.revise("demo", {
            "base_revision": revision,
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "更活泼的说话方式。"}],
        }).revision
        second = core.persona_publish.publish(
            "demo", target="next_turn", revision=second_revision,
        )
        core.persona_publish.consume("demo", "chat", expected_version=second.version)

        blocked = core.persona_publish.rollback("demo", first.version)
        self.assertEqual(blocked.status, "conflict")
        self.assertTrue(blocked.conflict)
        allowed = core.persona_publish.rollback(
            "demo", first.version, allow_consumed=True,
        )
        self.assertEqual(allowed.status, "rolled_back")
        self.assertEqual(allowed.version, first.version)
        duplicate = core.persona_publish.rollback(
            "demo", first.version, allow_consumed=True,
        )
        self.assertEqual(duplicate.status, "duplicate")

    async def test_deletion_invalidates_published_versions(self):
        core = self.build()
        core.recover("demo")
        core.persona_card_service.import_initial("demo", [
            {"section_id": "tone", "text": "用户喜欢温柔的语气。",
             "source_refs": ["fact_tone"],
             "editable_by": {"user": True, "evolution": True}},
        ])
        core.facts.save("demo", [{
            "id": "fact_tone", "text": "用户喜欢温柔的语气", "importance": 7,
            "schema_version": 2, "absorbed": False,
            "source_event_keys": ["host:demo:private:tone"],
        }])
        published = core.persona_publish.publish(
            "demo", target="next_turn", revision=1,
        )
        self.assertEqual(published.status, "published")
        receipt = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            targets=(DeletionTarget("fact", "fact_tone"),),
        ))
        self.assertEqual(receipt.status, "completed")
        current = core.persona_publish.current("demo")
        self.assertFalse(current.available)
        self.assertEqual(
            core.persona_card_service.render_projection("demo"), {},
        )

    async def test_engine_publish_api_reopen_and_not_configured(self):
        core = self.build()
        revision = self.prepare(core)
        engine = MemoryEngine(
            events=core.sink, profiles=None, context=None,
            persona_publish=core.persona_publish,
        )
        published = engine.publish_persona("demo", {"target": "next_turn"})
        self.assertEqual(published["status"], "published")
        self.assertEqual(len(published["version"]), 24)
        current = engine.current_persona_version("demo")
        self.assertEqual(current["version"], published["version"])
        matched = engine.consume_persona_version(
            "demo", "chat", expected_version=published["version"],
        )
        self.assertEqual(matched["status"], "match")
        planner = engine.pair_persona_planner(
            "demo", published["snapshot"]["planner_version"],
        )
        self.assertIn(planner["status"], ("matched", "reissue"))

        reopened = self.build()
        reopened.recover("demo")
        self.assertEqual(
            reopened.persona_publish.current("demo").version,
            published["version"],
        )
        bare = MemoryEngine(events=None, profiles=None, context=None)
        with self.assertRaises(NotConfiguredError):
            bare.publish_persona("demo")


if __name__ == "__main__":
    unittest.main()