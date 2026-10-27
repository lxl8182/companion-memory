"""B10 acceptance: authoritative persona card, revisions and manual editing.

The batch makes the card the single source of truth for persona text: one-time
import, monotonic revisions with base_revision CAS, section-level add/replace/
merge/delete, freeze (auto revisions only), history bounds plus archive,
deletion/correction filtering of sections and history, and a read-only
characters.json projection. No model call and no host write happens here.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from companion_memory.adapters.composition import build_memory_core
from companion_memory.api import MemoryEngine
from companion_memory.contracts import (
    ContextRequest,
    CorrectionRequest,
    DeletionRequest,
    DeletionTarget,
    Scope,
)


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


INITIAL = [
    {
        "section_id": "identity", "kind": "identity", "text": "你是一只猫娘。",
        "editable_by": {"user": True, "evolution": False},
    },
    {
        "section_id": "tone", "kind": "style", "text": "说话温柔。",
        "editable_by": {"user": True, "evolution": True},
    },
]


class PersonaCardTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_import_once_revision_projection_and_reopen(self):
        core = self.build()
        core.recover("demo")
        imported = core.persona_card_service.import_initial("demo", INITIAL)
        self.assertEqual(imported.status, "completed")
        self.assertEqual(imported.revision, 1)
        self.assertEqual(imported.applied_ops, 2)
        duplicate = core.persona_card_service.import_initial("demo", INITIAL)
        self.assertEqual(duplicate.status, "duplicate")
        self.assertTrue(duplicate.duplicate)

        snapshot = core.persona_card_service.get("demo")
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual(snapshot.initial_revision, 1)
        self.assertEqual([section.section_id for section in snapshot.sections],
                         ["identity", "tone"])
        self.assertEqual(
            core.persona_card_service.render_projection("demo"),
            {"identity": "你是一只猫娘。", "tone": "说话温柔。"},
        )
        self.assertIn("你是一只猫娘。", core.persona_card_service.render_text("demo"))

        # Reopen on the same root: the card and its replay handler survive.
        reopened = self.build()
        reopened.recover("demo")
        self.assertEqual(reopened.persona_card_service.get("demo").revision, 1)
        self.assertEqual(
            reopened.persona_card_service.render_projection("demo")["tone"],
            "说话温柔。",
        )

    async def test_revise_operations_and_base_conflict(self):
        core = self.build()
        core.recover("demo")
        core.persona_card_service.import_initial("demo", INITIAL)
        revised = core.persona_card_service.revise("demo", {
            "base_revision": 1,
            "operations": [
                {"op": "add", "section_id": "hobby", "text": "喜欢跑步。"},
                {"op": "replace", "section_id": "identity",
                 "text": "你是一只喜欢被直接问的猫娘。"},
                {"op": "merge", "section_id": "tone", "text": "也会开玩笑。"},
            ],
            "reason": "user edit",
        })
        self.assertEqual(revised.status, "completed")
        self.assertEqual(revised.revision, 2)
        self.assertEqual(set(revised.changed_sections),
                         {"hobby", "identity", "tone"})
        snapshot = core.persona_card_service.get("demo")
        by_id = {section.section_id: section for section in snapshot.sections}
        self.assertEqual(by_id["identity"].text, "你是一只喜欢被直接问的猫娘。")
        self.assertEqual(by_id["tone"].text, "说话温柔。\n也会开玩笑。")
        self.assertEqual(len(by_id["identity"].history), 1)

        stale = core.persona_card_service.revise("demo", {
            "base_revision": 1,
            "operations": [{"op": "replace", "section_id": "tone", "text": "x"}],
        })
        self.assertEqual(stale.status, "conflict")
        self.assertTrue(stale.conflict)
        self.assertEqual(stale.reason, "base_revision_conflict")
        self.assertEqual(core.persona_card_service.get("demo").revision, 2)

        deleted = core.persona_card_service.revise("demo", {
            "base_revision": 2,
            "operations": [{"op": "delete", "section_id": "hobby"}],
        })
        self.assertEqual(deleted.status, "completed")
        self.assertNotIn("hobby", core.persona_card_service.render_projection("demo"))

    async def test_freeze_blocks_auto_but_not_manual_edits(self):
        core = self.build()
        core.recover("demo")
        core.persona_card_service.import_initial("demo", INITIAL)
        frozen = core.persona_card_service.freeze("demo", "tone", True)
        self.assertEqual(frozen.status, "completed")
        revision = frozen.revision

        auto = core.persona_card_service.revise("demo", {
            "base_revision": revision, "origin": "evolution",
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "自动改写"}],
        })
        self.assertEqual(auto.status, "rejected")
        self.assertEqual(auto.reason, "section_frozen")

        manual = core.persona_card_service.revise("demo", {
            "base_revision": revision,
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "用户手改的说话方式。"}],
        })
        self.assertEqual(manual.status, "completed")

        # evolution-disabled section rejects auto edits even when not frozen.
        current = core.persona_card_service.get("demo").revision
        not_editable = core.persona_card_service.revise("demo", {
            "base_revision": current, "origin": "evolution",
            "operations": [{"op": "replace", "section_id": "identity",
                            "text": "自动改写身份"}],
        })
        self.assertEqual(not_editable.status, "rejected")
        self.assertEqual(not_editable.reason, "section_not_editable")

    async def test_history_bound_archive_and_restore_initial(self):
        core = self.build()
        core.recover("demo")
        core.persona_card_service.import_initial("demo", INITIAL)
        revision = 1
        for index in range(24):
            receipt = core.persona_card_service.revise("demo", {
                "base_revision": revision,
                "operations": [{"op": "replace", "section_id": "tone",
                                "text": f"版本 {index} 的说话方式。"}],
            })
            self.assertEqual(receipt.status, "completed")
            revision = receipt.revision
        card = core.persona_card_service.get("demo")
        self.assertLessEqual(len(card.revisions), 20)
        archived = core.persona_card.load_archive("demo")
        self.assertGreaterEqual(len(archived), 4)

        restored = core.persona_card_service.restore_initial("demo")
        self.assertEqual(restored.status, "completed")
        projection = core.persona_card_service.render_projection("demo")
        self.assertEqual(projection["tone"], "说话温柔。")
        self.assertGreater(core.persona_card_service.get("demo").revision, revision)

    async def test_deletion_and_correction_filter_sections_and_history(self):
        core = self.build()
        core.recover("demo")
        core.persona_card_service.import_initial("demo", [
            {"section_id": "tone", "text": "说话温柔。",
             "source_refs": ["fact_a", "fact_b"],
             "editable_by": {"user": True, "evolution": True}},
            {"section_id": "identity", "text": "你是一只猫娘。"},
        ])
        core.facts.save("demo", [
            {"id": "fact_a", "text": "用户喜欢温柔的语气", "importance": 7,
             "schema_version": 2, "absorbed": False,
             "source_event_keys": ["host:demo:private:a"]},
            {"id": "fact_b", "text": "用户喜欢被鼓励", "importance": 6,
             "schema_version": 2, "absorbed": False,
             "source_event_keys": ["host:demo:private:b"]},
        ])
        # Deleting a source the section depends on removes it from the card and
        # from the projection; the deleted ref never comes back on replay.
        receipt = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "fact_a"),),
        ))
        self.assertEqual(receipt.status, "completed")
        projection = core.persona_card_service.render_projection("demo")
        self.assertNotIn("tone", projection)
        self.assertIn("identity", projection)
        stored = {section["section_id"]: section
                  for section in core.persona_card.load("demo")["sections"]}
        self.assertNotIn("tone", stored)

        # A correction invalidates the sections that cite the replaced fact.
        added = core.persona_card_service.revise("demo", {
            "base_revision": core.persona_card_service.get("demo").revision,
            "operations": [{"op": "add", "section_id": "pref",
                            "text": "用户喜欢被鼓励。",
                            "source_refs": ["fact_b"],
                            "editable_by": {"user": True, "evolution": True}}],
        })
        self.assertEqual(added.status, "completed")
        self.assertIn("pref", core.persona_card_service.render_projection("demo"))
        correction = await core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), old_text="喜欢被鼓励", new_text="喜欢被具体表扬",
        ))
        self.assertIn("fact_b", correction.invalidated_fact_ids)
        self.assertNotIn("pref", core.persona_card_service.render_projection("demo"))
        invalidated = {section["section_id"]: section
                       for section in core.persona_card.load("demo")["sections"]}
        self.assertTrue(
            invalidated["pref"].get("invalidated_at")
            or invalidated["pref"].get("needs_rebuild"),
        )
    async def test_engine_profile_and_card_operations(self):
        core = self.build()
        core.recover("demo")
        core.persona_card_service.import_initial("demo", INITIAL)

        class UnusedProfiles:
            async def get(self, character):
                raise AssertionError("the card must be the profile source")

        class EmptyContext:
            async def recall(self, request):
                return ()

        engine = MemoryEngine(
            events=core.sink, profiles=UnusedProfiles(), context=EmptyContext(),
            persona_card=core.persona_card_service,
        )
        profile = await engine.get_profile("demo")
        self.assertEqual(profile.revision, 1)
        self.assertEqual([section.section_id for section in profile.sections],
                         ["identity", "tone"])
        self.assertTrue(profile.sections[1].evolution_enabled)
        self.assertFalse(profile.sections[0].evolution_enabled)

        bundle = await engine.build_context(ContextRequest(
            scope=Scope("demo"), current_input="你好",
        ))
        self.assertIn("你是一只猫娘。", bundle.persona)

        revised = engine.revise_persona_card("demo", {
            "base_revision": 1,
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "说话更温柔。"}],
        })
        self.assertEqual(revised["status"], "completed")
        self.assertEqual(engine.persona_projection("demo")["tone"], "说话更温柔。")
        frozen = engine.freeze_persona_section("demo", "tone", True)
        self.assertEqual(frozen["status"], "completed")
        restored = engine.restore_persona_card("demo")
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(engine.persona_projection("demo")["tone"], "说话温柔。")

        bare = MemoryEngine(events=None, profiles=None, context=None)
        from companion_memory.errors import NotConfiguredError
        with self.assertRaises(NotConfiguredError):
            bare.persona_card("demo")


if __name__ == "__main__":
    unittest.main()