"""RO01 acceptance: arbitrary persona sections, expression samples, permissions.

The card already accepted any section_id/kind/scope/editable_by/frozen; what RO01
adds is the *semantics* around that surface, pinned here one behaviour at a time:

* a custom section id (including a Chinese label turned into an id) goes through
  the ordinary add/replace/delete revisions and reaches the published projection;
* kind=examples renders into the fixed persona text marked as expression samples,
  and reaches no evidence consumer (facts, reflection, evolution);
* an examples section is created closed: editable_by.evolution=false and
  frozen=true, so automatic revision is refused while a hand edit still applies;
* an existing section may have its permissions changed (frozen/editable_by/scope)
  without a text change, and frozen wins over editable_by.evolution;
* the about_owner preset is a default field template, not a new store: the same
  revisions, publish and rollback apply;
* an unusable section id is refused with a reason the caller can act on, and a
  repeated add never overwrites the existing section.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.domain.evolution import EvolutionSettings
from rin_memory.domain.persona_card import (
    EXAMPLES_BANNER,
    EXAMPLES_KIND,
    SECTION_PRESETS,
)


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class FakeModel:
    """Records every prompt; returns a canned reply per purpose."""

    def __init__(self, replies=None) -> None:
        self.replies = dict(replies or {})
        self.calls = []

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({"purpose": purpose, "messages": messages})
        return self.replies.get(purpose, "[]")

    def prompts(self, purpose):
        return [call["messages"][0]["content"]
                for call in self.calls if call["purpose"] == purpose]


INITIAL = [
    {
        "section_id": "identity", "kind": "identity", "text": "你是一只猫娘。",
        "editable_by": {"user": True, "evolution": False}, "frozen": True,
    },
    {
        "section_id": "tone", "kind": "style", "text": "说话温柔。",
        "editable_by": {"user": True, "evolution": True},
    },
]

EXAMPLES_TEXT = (
    "场景：主人分享了一张可爱的图片\n"
    "主人：你看这个\n"
    "我：等下，这个小表情也太得意了"
)


def preset(section_id):
    return next(row for row in SECTION_PRESETS if row["section_id"] == section_id)


class SectionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Custom sections, samples, permissions and presets on one real core."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory", *, model=None, evolution=False):
        return build_memory_core(
            memory_root=self.root / name, clock=self.clock, model=model,
            evolution_settings=EvolutionSettings(
                enabled=evolution, tier="summary", timeout_s=5,
                max_output_tokens=300,
            ),
        )

    def card(self, core):
        core.recover("demo")
        core.persona_card_service.import_initial("demo", INITIAL)
        return core

    def revise(self, core, operations, **kwargs):
        return core.persona_card_service.revise("demo", {
            "base_revision": core.persona_card_service.get("demo").revision,
            "operations": list(operations),
            **kwargs,
        })

    # -- 1. arbitrary section id ---------------------------------------

    async def test_custom_section_id_lifecycle_publish_and_restart(self):
        core = self.card(self.build())
        added = self.revise(core, [{
            "op": "add", "section_id": "审美与创作", "kind": "aesthetic",
            "text": "偏爱留白和旧纸张的质感，喜欢把没说完的话留在句尾。",
            "editable_by": {"user": True, "evolution": True},
        }], reason="add custom section")
        self.assertEqual(added.status, "completed")
        self.assertIn("审美与创作", added.changed_sections)

        replaced = self.revise(core, [{
            "op": "replace", "section_id": "审美与创作",
            "text": "偏爱留白与旧纸张，也喜欢水彩洇开的边缘。",
        }])
        self.assertEqual(replaced.status, "completed")

        # Publishing carries the custom section into the fixed persona text.
        published = core.persona_publish.publish(
            "demo", target="next_turn",
            revision=core.persona_card_service.get("demo").revision,
        )
        self.assertEqual(published.status, "published")
        self.assertIn("审美与创作", published.snapshot.projection)
        self.assertIn("水彩洇开的边缘", published.snapshot.text)

        # A restart re-reads the same card and the same published version.
        reopened = self.build(name="memory")
        reopened.recover("demo")
        self.assertIn("审美与创作",
                      reopened.persona_card_service.render_projection("demo"))
        self.assertEqual(reopened.persona_publish.current("demo").version,
                         published.version)

        deleted = self.revise(reopened, [{
            "op": "delete", "section_id": "审美与创作",
        }])
        self.assertEqual(deleted.status, "completed")
        self.assertNotIn("审美与创作",
                         reopened.persona_card_service.render_projection("demo"))

    async def test_unusable_section_id_is_refused_with_a_readable_reason(self):
        core = self.card(self.build())
        cases = {
            "带 空格": "invalid_section_id",
            "斜杠/份": "invalid_section_id",
            "x" * 200: "section_id_too_long",
        }
        for section_id, expected in cases.items():
            receipt = self.revise(core, [{
                "op": "add", "section_id": section_id, "text": "不该写进去。",
            }])
            self.assertEqual(receipt.status, "rejected", section_id[:20])
            self.assertEqual(receipt.reason, expected, section_id[:20])
        # Nothing was written for a refused id.
        self.assertEqual(
            [section.section_id for section in
             core.persona_card_service.get("demo").sections],
            ["identity", "tone"],
        )
        # A repeated add of an existing id is refused, never an overwrite.
        duplicate = self.revise(core, [{
            "op": "add", "section_id": "tone", "text": "覆盖原语气。",
        }])
        self.assertEqual(duplicate.status, "rejected")
        self.assertEqual(duplicate.reason, "duplicate_section")
        self.assertEqual(
            core.persona_card_service.render_projection("demo")["tone"],
            "说话温柔。",
        )

    # -- 2. expression samples -----------------------------------------

    async def test_examples_section_renders_marked_and_never_becomes_evidence(self):
        model = FakeModel(replies={
            "memory_fact_extraction": json.dumps(
                [{"text": "主人今天在整理照片", "importance": 6,
                  "entity": "master", "source": "user_observation"}],
                ensure_ascii=False,
            ),
            "reflection_synthesis": json.dumps(
                {"operation": "no_change", "reflection": None},
                ensure_ascii=False,
            ),
            # A valid proposal: the point of this case is what the model was
            # *shown*, not whether it answered.
            "persona_evolution_proposal": json.dumps({
                "operations": [{"op": "replace", "section_id": "tone",
                                "text": "说话温柔，也接得住玩笑。"}],
                "reason": "证据显示主人偏好具体的温柔表达",
            }, ensure_ascii=False),
        })
        core = self.build(model=model, evolution=True)
        self.card(core)
        added = self.revise(core, [{
            "op": "add", "section_id": "dialogue_examples",
            "kind": EXAMPLES_KIND, "text": EXAMPLES_TEXT,
        }])
        self.assertEqual(added.status, "completed")

        stored = {section.section_id: section
                  for section in core.persona_card_service.get("demo").sections}
        samples = stored["dialogue_examples"]
        # Created closed by default: nothing automatic may rewrite hand-picked
        # expression, and the two defaults are exactly what the preset says.
        self.assertFalse(samples.editable_by.get("evolution"))
        self.assertTrue(samples.frozen)
        self.assertEqual(samples.editable_by, preset("dialogue_examples")["editable_by"])
        self.assertEqual(samples.frozen, preset("dialogue_examples")["frozen"])

        revision = core.persona_card_service.get("demo").revision
        published = core.persona_publish.publish(
            "demo", target="next_turn", revision=revision,
        )
        self.assertEqual(published.status, "published")
        # The mark is prose in the fixed persona text, and the samples follow it.
        self.assertIn(EXAMPLES_BANNER, published.snapshot.text)
        self.assertIn("这个小表情也太得意了", published.snapshot.text)
        self.assertIn(
            EXAMPLES_BANNER, published.snapshot.projection["dialogue_examples"],
        )
        # The card itself keeps the authored text; the mark is a render concern.
        self.assertNotIn(EXAMPLES_BANNER, samples.text)

        # Consumer 1: extraction reads the conversation, never the card text.
        await core.extraction.ingest_turn(
            "demo",
            [{"type": "human", "data": {"content": "我今天在整理照片"}},
             {"type": "ai", "data": {"content": "整理到哪一步了"}}],
            source_key="host:demo:private:r1",
        )
        for prompt in model.prompts("memory_fact_extraction"):
            self.assertNotIn("这个小表情也太得意了", prompt)
            self.assertNotIn(EXAMPLES_BANNER, prompt)
        facts_text = " ".join(str(row.get("text") or "")
                             for row in core.facts.load("demo"))
        self.assertNotIn("这个小表情也太得意了", facts_text)

        # Consumer 2: reflection synthesises from facts, not from samples.
        core.facts.save("demo", [
            {"id": "fact_a", "text": "主人喜欢胶片摄影的颗粒感", "importance": 7,
             "schema_version": 2, "absorbed": False,
             "source_event_keys": ["host:demo:private:a"]},
            {"id": "fact_b", "text": "主人喜欢把照片打印出来", "importance": 6,
             "schema_version": 2, "absorbed": False,
             "source_event_keys": ["host:demo:private:b"]},
        ])
        await core.reflection_service.synthesize("demo")
        for prompt in model.prompts("reflection_synthesis"):
            self.assertNotIn("这个小表情也太得意了", prompt)

        # Consumer 3: evolution never offers a closed section as an edit surface.
        core.facts.save("demo", [
            {"id": "fact_a", "text": "主人喜欢胶片摄影的颗粒感", "importance": 7,
             "schema_version": 2, "absorbed": False,
             "source_event_keys": ["host:demo:private:a"]},
        ])
        core.reflections.save("demo", [{
            "id": "ref_understanding", "text": "主人喜欢温柔、具体的语气",
            "status": "confirmed", "entity": "relationship",
            "source_fact_ids": ["fact_a", "fact_b"],
            "source_event_keys": ["host:demo:private:a", "host:demo:private:b"],
            "support_basis": [
                {"experience": "episode:e1", "facts": ["fact_a"],
                 "support": [{"key": "host:demo:private:a", "fact_id": "fact_a",
                              "kind": "explicit_feedback", "flags": ["user_feedback"]}]},
                {"experience": "episode:e2", "facts": ["fact_b"],
                 "support": [{"key": "host:demo:private:b", "fact_id": "fact_b",
                              "kind": "observed_action",
                              "flags": ["actual_result", "real_contact"]}]},
            ],
            "created_at": "2026-10-20T00:00:00+00:00",
        }])
        base = core.persona_card_service.get("demo").revision
        proposal = await core.evolution_service.propose("demo", {
            "base_revision": base, "source_refs": ["fact_a"],
        })
        self.assertEqual(proposal.status, "proposed")
        evolution_prompt = model.prompts("persona_evolution_proposal")[-1]
        self.assertNotIn("这个小表情也太得意了", evolution_prompt)

        focused = await core.evolution_service.propose("demo", {
            "base_revision": base, "source_refs": ["fact_a"],
            "focus_sections": ["dialogue_examples"],
        })
        self.assertEqual(focused.status, "rejected")
        self.assertEqual(focused.reason, "section_frozen")

    async def test_evolution_refuses_an_unusable_new_section_id(self):
        model = FakeModel()
        core = self.build(model=model, evolution=True)
        self.card(core)
        # The model proposes an id that cannot be a stable key. The proposal is
        # refused while it is being validated, so nothing bad reaches the card.
        core.facts.save("demo", [
            {"id": "fact_a", "text": "主人喜欢胶片摄影的颗粒感", "importance": 7,
             "schema_version": 2, "absorbed": False,
             "source_event_keys": ["host:demo:private:a"]},
        ])
        core.reflections.save("demo", [{
            "id": "ref_understanding", "text": "主人喜欢温柔、具体的语气",
            "status": "confirmed", "entity": "relationship",
            "source_fact_ids": ["fact_a", "fact_b"],
            "source_event_keys": ["host:demo:private:a", "host:demo:private:b"],
            "support_basis": [
                {"experience": "episode:e1", "facts": ["fact_a"],
                 "support": [{"key": "host:demo:private:a", "fact_id": "fact_a",
                              "kind": "explicit_feedback", "flags": ["user_feedback"]}]},
                {"experience": "episode:e2", "facts": ["fact_b"],
                 "support": [{"key": "host:demo:private:b", "fact_id": "fact_b",
                              "kind": "observed_action",
                              "flags": ["actual_result", "real_contact"]}]},
            ],
            "created_at": "2026-10-20T00:00:00+00:00",
        }])
        model.replies["persona_evolution_proposal"] = json.dumps({
            "operations": [{"op": "add", "section_id": "新的 段落",
                            "text": "不该被接受的段落。"}],
            "reason": "模型自己挑了一个带空格的 id",
        }, ensure_ascii=False)
        receipt = await core.evolution_service.propose("demo", {
            "base_revision": core.persona_card_service.get("demo").revision,
            "source_refs": ["fact_a"],
        })
        self.assertEqual(receipt.status, "rejected")
        self.assertIn(
            "invalid_section_id",
            [check.get("reason") for check in receipt.validation["checks"]],
        )
        self.assertEqual(core.evolution_service.list("demo"), [])

    async def test_examples_can_be_opened_by_an_explicit_choice(self):
        model = FakeModel()
        core = self.build(model=model, evolution=True)
        self.card(core)
        self.revise(core, [{
            "op": "add", "section_id": "dialogue_examples",
            "kind": EXAMPLES_KIND, "text": EXAMPLES_TEXT,
            "editable_by": {"user": True, "evolution": True}, "frozen": False,
        }])
        stored = {section.section_id: section
                  for section in core.persona_card_service.get("demo").sections}
        # An explicit field in the same operation wins over the closed default.
        self.assertTrue(stored["dialogue_examples"].editable_by.get("evolution"))
        self.assertFalse(stored["dialogue_examples"].frozen)

    # -- 3. permissions on existing sections ---------------------------

    async def test_existing_section_permissions_change_without_a_text_edit(self):
        core = self.card(self.build())
        frozen = self.revise(core, [{
            "op": "replace", "section_id": "tone", "text": "说话温柔。",
            "frozen": True,
        }], reason="freeze this section")
        # The text did not change, but the permission did: still a revision.
        self.assertEqual(frozen.status, "completed")
        tone = next(section for section in
                    core.persona_card_service.get("demo").sections
                    if section.section_id == "tone")
        self.assertTrue(tone.frozen)

        scoped = self.revise(core, [{
            "op": "replace", "section_id": "tone", "text": "说话温柔。",
            "scope": {"subject": "private", "applies_to": {"game_id": "sts2"}},
            "editable_by": {"user": True, "evolution": False},
        }])
        self.assertEqual(scoped.status, "completed")
        tone = next(section for section in
                    core.persona_card_service.get("demo").sections
                    if section.section_id == "tone")
        self.assertEqual(tone.scope.get("applies_to"), {"game_id": "sts2"})
        self.assertFalse(tone.editable_by.get("evolution"))

        # A revision that changes nothing at all is the duplicate, not a rewrite.
        noop = self.revise(core, [{
            "op": "replace", "section_id": "tone", "text": "说话温柔。",
        }])
        self.assertEqual(noop.status, "duplicate")
        self.assertEqual(noop.reason, "no_change")

    async def test_frozen_wins_over_evolution_permission(self):
        core = self.card(self.build())
        revision = core.persona_card_service.get("demo").revision
        # tone is open to evolution; freezing it must win over that permission.
        self.revise(core, [{
            "op": "replace", "section_id": "tone", "text": "说话温柔。",
            "frozen": True,
        }])
        auto = core.persona_card_service.revise("demo", {
            "base_revision": core.persona_card_service.get("demo").revision,
            "origin": "evolution",
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "自动改写的语气。"}],
        })
        self.assertEqual(auto.status, "rejected")
        self.assertEqual(auto.reason, "section_frozen")

        manual = core.persona_card_service.revise("demo", {
            "base_revision": core.persona_card_service.get("demo").revision,
            "origin": "user_edit",
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "用户手改的语气。"}],
        })
        self.assertEqual(manual.status, "completed")
        self.assertEqual(
            core.persona_card_service.render_projection("demo")["tone"],
            "用户手改的语气。",
        )
        self.assertGreater(revision, 0)

        # Unfrozen and open again: automatic revision is accepted.
        self.revise(core, [{
            "op": "replace", "section_id": "tone", "text": "用户手改的语气。",
            "frozen": False, "editable_by": {"user": True, "evolution": True},
        }])
        reopened = core.persona_card_service.revise("demo", {
            "base_revision": core.persona_card_service.get("demo").revision,
            "origin": "evolution",
            "operations": [{"op": "replace", "section_id": "tone",
                            "text": "演化后的语气。"}],
        })
        self.assertEqual(reopened.status, "completed")

    # -- 4. presets are templates, not a new store ---------------------

    async def test_about_owner_preset_uses_the_ordinary_publish_and_rollback(self):
        core = self.card(self.build())
        owner_preset = preset("about_owner")
        self.assertEqual(owner_preset["kind"], "profile")
        added = self.revise(core, [{
            "op": "add",
            "section_id": owner_preset["section_id"],
            "kind": owner_preset["kind"],
            "text": "主人喜欢把事情想清楚再动手；说晚安时不希望被多问。",
            "editable_by": dict(owner_preset["editable_by"]),
            "frozen": owner_preset["frozen"],
            "scope": {"subject": "private"},
        }])
        self.assertEqual(added.status, "completed")

        first = core.persona_publish.publish(
            "demo", target="next_turn",
            revision=core.persona_card_service.get("demo").revision,
        )
        self.assertEqual(first.status, "published")
        self.assertIn("about_owner", first.snapshot.projection)

        revised = self.revise(core, [{
            "op": "replace", "section_id": "about_owner",
            "text": "主人喜欢把事情想清楚再动手。",
        }])
        self.assertEqual(revised.status, "completed")
        second = core.persona_publish.publish(
            "demo", target="next_turn", revision=revised.revision,
        )
        self.assertEqual(second.status, "published")
        self.assertNotEqual(second.version, first.version)

        rolled = core.persona_publish.rollback(
            "demo", first.version, allow_consumed=True,
        )
        self.assertEqual(rolled.status, "rolled_back")
        self.assertEqual(core.persona_publish.current("demo").version, first.version)
        # Rollback changes what is served; the card keeps the newer text as a
        # draft the user can publish again, so nothing is lost in the round trip.
        self.assertIn("说晚安时不希望被多问",
                      core.persona_publish.current("demo").text)
        self.assertNotIn("说晚安时不希望被多问",
                         core.persona_card_service.render_projection(
                             "demo")["about_owner"])

    async def test_base_revision_conflict_keeps_the_draft_and_card(self):
        core = self.card(self.build())
        base = core.persona_card_service.get("demo").revision
        self.revise(core, [{
            "op": "add", "section_id": "about_owner", "kind": "profile",
            "text": "第一版。",
        }])
        # A second editor that still holds the old base revision: the CAS fails
        # and the caller's draft is never applied on top of the newer card.
        stale = core.persona_card_service.revise("demo", {
            "base_revision": base,
            "operations": [{
                "op": "add", "section_id": "about_owner", "kind": "profile",
                "text": "基于旧版本的另一份草稿。",
            }],
        })
        self.assertEqual(stale.status, "conflict")
        self.assertTrue(stale.conflict)
        self.assertEqual(stale.reason, "base_revision_conflict")
        self.assertEqual(
            core.persona_card_service.render_projection("demo")["about_owner"],
            "第一版。",
        )


if __name__ == "__main__":
    unittest.main()
