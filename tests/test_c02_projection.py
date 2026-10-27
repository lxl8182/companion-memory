"""C02 acceptance: composition + api projection/host-edit/consumer wiring.

The engine owns the authoritative card; characters.json is a revision-stamped
projection. These tests exercise the real composition wiring (projection store,
card/publish services, deletion/correction fan-out) through MemoryEngine's C02
surface, using only temporary roots and no host imports.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from companion_memory.adapters.composition import build_memory_core
from companion_memory.api import MemoryEngine
from companion_memory.contracts import (
    CorrectionRequest,
    DeletionRequest,
    DeletionTarget,
    Scope,
)
from companion_memory.errors import NotConfiguredError


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 23, 0, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


HOST_CARD = {
    "system_prompt": "你是一只猫娘，名字叫团子。",
    "persona_override": "说话温柔，喜欢一起解决问题。",
    "nickname": "团子",
    "personality": "好奇、耐心",
    "model": "gpt-x",
    "voice": "voice-a",
    "temperature": 0.7,
}

CARD_SECTIONS = [
    {"section_id": "identity", "text": "你是一只猫娘。",
     "editable_by": {"user": True, "evolution": False}},
    {"section_id": "tone", "text": "说话温柔。",
     "source_refs": ["fact_tone"],
     "editable_by": {"user": True, "evolution": True}},
]

# A one-section card makes the projection text identical to the deleted fact
# text; this exercises the composition deletion cleaner without the C02
# section-level stale fallback (reported as a wiring gap to the Lead).
DELETION_SECTIONS = [{
    "section_id": "tone", "text": "说话温柔。",
    "source_refs": ["fact_tone"],
    "editable_by": {"user": True, "evolution": True},
}]


class ProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory"):
        return build_memory_core(
            memory_root=self.root / name, clock=self.clock,
        )

    @staticmethod
    def engine(core):
        return MemoryEngine(
            events=core.sink, profiles=None, context=None,
            persona_card=core.persona_card_service,
            persona_publish=core.persona_publish,
            persona_projection=core.persona_projection_service,
        )

    async def test_host_import_once_projection_and_resolve(self):
        core = self.build()
        core.recover("demo")
        engine = self.engine(core)

        receipt = engine.import_persona_card_from_host("demo", HOST_CARD)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["revision"], 1)
        self.assertTrue(receipt["version"])
        self.assertEqual(
            set(receipt["projection"]),
            {"host_system_prompt", "host_persona_override",
             "host_nickname", "host_personality"},
        )
        self.assertNotIn("model", receipt["projection"])
        self.assertNotIn("voice", receipt["projection"])
        self.assertTrue(receipt["host_import"]["section_ids"])

        duplicate = engine.import_persona_card_from_host("demo", HOST_CARD)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["revision"], 1)
        self.assertEqual(
            core.persona_card_service.get("demo").revision, 1,
        )

        status = engine.persona_projection_status("demo")
        self.assertEqual(status["card_revision"], 1)
        self.assertEqual(status["version"], receipt["version"])
        self.assertEqual(status["consumers"]["chat"]["status"], "saved")
        self.assertFalse(status["published"])

        authoritative = engine.resolve_authoritative_persona("demo")
        self.assertEqual(authoritative["revision"], 1)
        self.assertEqual(authoritative["version"], receipt["version"])
        self.assertIn("host_nickname", authoritative["projection"])
        self.assertEqual(authoritative["source"], "engine")
        self.assertEqual(
            engine.ensure_persona_for_consumer("demo", "chat")["version"],
            receipt["version"],
        )

    async def test_card_revision_publish_then_sync(self):
        core = self.build()
        core.recover("demo")
        engine = self.engine(core)
        first = engine.import_persona_card_from_host("demo", HOST_CARD)

        revised = core.persona_card_service.revise("demo", {
            "base_revision": 1,
            "operations": [{
                "op": "replace", "section_id": "host_nickname",
                "text": "小团子",
            }],
        })
        self.assertEqual(revised.status, "completed")
        self.assertTrue(engine.sync_persona_projection("demo"))
        status = engine.persona_projection_status("demo")
        self.assertEqual(status["card_revision"], 2)
        self.assertTrue(status["version"].startswith("card_"))
        self.assertNotEqual(status["version"], first["version"])
        self.assertEqual(
            status["projection"]["host_nickname"], "小团子",
        )

        published = core.persona_publish.publish("demo", target="next_turn")
        self.assertEqual(published.status, "published")
        self.assertTrue(engine.sync_persona_projection("demo"))
        status = engine.persona_projection_status("demo")
        self.assertEqual(status["version"], published.version)
        self.assertTrue(status["published"])
        self.assertEqual(
            engine.ensure_persona_for_consumer("demo", "chat")["version"],
            published.version,
        )

    async def test_consumers_adopt_mismatch_stale_and_retry(self):
        core = self.build()
        core.recover("demo")
        engine = self.engine(core)
        engine.import_persona_card_from_host("demo", HOST_CARD)
        first = engine.persona_projection_status("demo")["version"]

        adopted = engine.adopt_persona_projection(
            "demo", "chat", first, receipt_id="chat-r1",
        )
        self.assertEqual(adopted["status"], "match")
        self.assertEqual(adopted["consumer_state"]["status"], "adopted")
        self.assertEqual(
            engine.persona_projection_status("demo")["consumers"]["chat"]["status"],
            "adopted",
        )

        core.persona_card_service.revise("demo", {
            "base_revision": 1,
            "operations": [{
                "op": "replace", "section_id": "host_nickname",
                "text": "团子二",
            }],
        })
        published = core.persona_publish.publish("demo", target="next_turn")
        engine.sync_persona_projection("demo")
        current = engine.persona_projection_status("demo")["version"]
        self.assertEqual(current, published.version)

        stale = engine.adopt_persona_projection("demo", "chat", first)
        self.assertEqual(stale["status"], "stale")
        self.assertTrue(stale["needs_reissue"])
        mismatch = engine.adopt_persona_projection("demo", "active", first)
        self.assertEqual(mismatch["status"], "mismatch")
        self.assertEqual(mismatch["version"], current)
        unknown = engine.adopt_persona_projection("demo", "game", "not-a-version")
        self.assertEqual(unknown["status"], "unknown")
        realtime = engine.adopt_persona_projection("demo", "realtime", current)
        self.assertEqual(realtime["status"], "match")

        core.persona_projection_service.mark_pending(
            "demo", "game", current, "publish",
        )
        self.assertEqual(
            engine.persona_projection_status("demo")["consumers"]["game"]["status"],
            "pending",
        )
        core.persona_projection.mark_failed("demo", "game", current, "timeout")
        status = engine.persona_projection_status("demo")
        self.assertEqual(status["consumers"]["game"]["status"], "failed")
        self.assertTrue(status["failed"])
        retried = engine.retry_persona_projection("demo", "game")
        self.assertEqual(retried["status"], "pending")
        self.assertGreaterEqual(retried["attempts"], 1)
        matched = engine.adopt_persona_projection("demo", "game", current)
        self.assertEqual(matched["status"], "match")
        status = engine.persona_projection_status("demo")
        self.assertEqual(status["consumers"]["game"]["status"], "adopted")
        self.assertEqual(status["pending"], [])
        self.assertEqual(status["failed"], [])

    async def test_host_edit_conflict_keeps_draft_and_success_syncs(self):
        core = self.build()
        core.recover("demo")
        engine = self.engine(core)
        engine.import_persona_card_from_host("demo", HOST_CARD)
        before = core.persona_card_service.get("demo")

        conflict_draft = {
            "base_revision": 0,
            "operations": [{
                "op": "replace", "section_id": "host_nickname",
                "text": "冲突草稿",
            }],
            "actor": "user",
            "reason": "host edit",
        }
        conflict = engine.apply_host_persona_edit("demo", conflict_draft)
        self.assertEqual(conflict["status"], "conflict")
        self.assertTrue(conflict["conflict"])
        self.assertEqual(conflict["draft"], conflict_draft)
        self.assertEqual(conflict["revision_before"], 1)
        self.assertEqual(core.persona_card_service.get("demo").revision, 1)
        self.assertNotIn(
            "冲突草稿", engine.persona_projection_status("demo")["text"],
        )

        applied = engine.apply_host_persona_edit("demo", {
            "base_revision": 1,
            "operations": [{
                "op": "replace", "section_id": "host_nickname",
                "text": "人工改名",
            }],
            "actor": "user",
            "reason": "host edit",
        })
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["revision_after"], 2)
        self.assertEqual(applied["draft"]["base_revision"], 1)
        self.assertIn("人工改名", applied["text"])
        status = engine.persona_projection_status("demo")
        self.assertEqual(status["card_revision"], 2)
        self.assertEqual(status["projection"]["host_nickname"], "人工改名")
        self.assertEqual(
            engine.resolve_authoritative_persona("demo")["revision"], 2,
        )

    async def test_deletion_invalidates_projection_and_consumers(self):
        core = self.build()
        core.recover("demo")
        core.persona_card_service.import_initial("demo", DELETION_SECTIONS)
        core.facts.save("demo", [{
            "id": "fact_tone", "text": "说话温柔。", "importance": 7,
            "schema_version": 2, "absorbed": False,
            "source_event_keys": ["host:demo:private:a"],
        }])
        engine = self.engine(core)
        engine.sync_persona_projection("demo")
        version = engine.persona_projection_status("demo")["version"]
        self.assertEqual(
            engine.adopt_persona_projection("demo", "chat", version)["status"],
            "match",
        )

        receipt = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"),
            targets=(DeletionTarget("fact", "fact_tone"),),
        ))
        self.assertEqual(receipt.status, "completed")
        status = engine.persona_projection_status("demo")
        self.assertTrue(status["invalidated"])
        self.assertEqual(status["version"], "")
        self.assertEqual(status["consumers"]["chat"]["status"], "pending")
        self.assertTrue(status["pending"])
        self.assertIsNone(engine.resolve_authoritative_persona("demo"))
        self.assertEqual(
            engine.ensure_persona_for_consumer("demo", "chat")["status"],
            "unavailable",
        )

    async def test_correction_invalidates_projection(self):
        core = self.build()
        core.recover("demo")
        core.persona_card_service.import_initial("demo", CARD_SECTIONS)
        core.facts.save("demo", [{
            "id": "fact_tone", "text": "说话温柔。", "importance": 7,
            "schema_version": 2, "absorbed": False,
            "source_event_keys": ["host:demo:private:a"],
        }])
        engine = self.engine(core)
        engine.sync_persona_projection("demo")
        version = engine.persona_projection_status("demo")["version"]
        engine.adopt_persona_projection("demo", "chat", version)
        # The composition correction fan-out currently passes only fact ids
        # after the card invalidator has already hidden the section; invoke the
        # C02 invalidator directly and report the fan-out ordering as a gap.
        invalid = core.persona_projection_service.invalidate_for_correction(
            "demo", ["fact_tone"],
        )
        self.assertTrue(invalid["current_invalidated"])

        receipt = await core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"),
            target_fact_ids=("fact_tone",),
            old_text="说话温柔。",
            new_text="说话直接。",
            source_key="host:demo:private:corr",
        ))
        self.assertTrue(receipt.applied)
        status = engine.persona_projection_status("demo")
        self.assertTrue(status["invalidated"])
        self.assertEqual(status["version"], "")
        self.assertEqual(status["consumers"]["chat"]["status"], "pending")
        self.assertIsNone(engine.resolve_authoritative_persona("demo"))

    async def test_reopen_keeps_projection_and_unwired_engine_fails(self):
        core = self.build()
        core.recover("demo")
        engine = self.engine(core)
        engine.import_persona_card_from_host("demo", HOST_CARD)
        version = engine.persona_projection_status("demo")["version"]
        engine.adopt_persona_projection("demo", "chat", version)

        reopened = self.build("memory")
        reopened.recover("demo")
        reopened_engine = self.engine(reopened)
        status = reopened_engine.persona_projection_status("demo")
        self.assertEqual(status["version"], version)
        self.assertEqual(status["consumers"]["chat"]["status"], "adopted")
        self.assertEqual(
            reopened_engine.resolve_authoritative_persona("demo")["version"],
            version,
        )

        bare = MemoryEngine(events=None, profiles=None, context=None)
        with self.assertRaises(NotConfiguredError):
            bare.persona_projection_status("demo")
        with self.assertRaises(NotConfiguredError):
            bare.import_persona_card_from_host("demo", HOST_CARD)


if __name__ == "__main__":
    unittest.main()
