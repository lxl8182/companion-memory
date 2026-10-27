"""B05 persona acceptance: host-shaped entries, merge audit, corrections, replay.

Covers the persona half of the batch: entries keep the host fields and source
links, a merge rewrites the target with an audit list/history, the four queued
correction actions behave as the host's review step, an entry_updated replay
with a drifted sha256 pauses the reconciler instead of losing the mutation, and
retracting a source stops serving its derived entries.
"""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from companion_memory.adapters.composition import build_memory_core
from companion_memory.domain.persona import PERSONA_ENTRY_EVENT


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class FakeModel:
    """Answers the correction-review purpose with queued JSON actions."""

    def __init__(self, actions=None) -> None:
        self.actions = list(actions or [])
        self.calls = []

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({"purpose": purpose, "tier": tier})
        if purpose == "persona_correction_resolution":
            if not self.actions:
                return json.dumps({"action": "keep_new"})
            item = self.actions.pop(0)
            if isinstance(item, Exception):
                raise item
            return json.dumps(item, ensure_ascii=False)
        return "{}"


def reflection_row(reflection_id="ref_a", text="主人喜欢被直接问"):
    return {
        "id": reflection_id,
        "text": text,
        "status": "confirmed",
        "entity": "relationship",
        "source_fact_ids": ["fact_0"],
        "reinforcement": 1.2,
        "rein_last_signal_at": "2026-10-22T08:00:00+00:00",
        "created_at": "2026-10-22T08:00:00+00:00",
    }


class PersonaFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model=None):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=model,
        )

    async def test_entries_merge_evidence_and_render(self):
        core = self.build()
        core.recover("demo")
        manual_id = core.persona_service.add_manual(
            "demo", "主人喜欢被直接问", entity="relationship",
        )
        self.assertTrue(manual_id.startswith("manual_"))
        reflection = reflection_row()
        core.reflections.save("demo", [reflection])
        promoted_id = core.persona_service.add_reflection("demo", reflection)
        self.assertEqual(promoted_id, "prom_ref_a")
        # Appending the same source again is idempotent.
        self.assertEqual(core.persona_service.add_reflection("demo", reflection), promoted_id)

        entries = core.persona_service.entries("demo")
        self.assertEqual([entry["id"] for entry in entries], [manual_id, promoted_id])
        rendered = core.persona_service.render_block("demo")
        self.assertIn("主人喜欢被直接问", rendered)

        merged = core.persona_service.merge(
            "demo", "ref_a", manual_id, "主人更喜欢被直接问",
        )
        self.assertEqual(merged["status"], "merged")
        self.assertEqual(merged["merged_from_ids"], ["ref_a"])
        target = next(
            entry for entry in core.persona_service.entries("demo")
            if entry["id"] == manual_id
        )
        self.assertEqual(target["text"], "主人更喜欢被直接问")
        self.assertTrue(target["version_history"])
        # The reflection's evidence is not double counted: max() wins.
        self.assertEqual(target["reinforcement"], 1.2)

        self.assertTrue(core.persona_service.apply_evidence(
            "demo", manual_id, {"reinforcement": 1.7},
        ))
        self.assertFalse(core.persona_service.apply_evidence(
            "demo", manual_id, {"reinforcement": 1.7},
        ))
        updated = next(
            entry for entry in core.persona_service.entries("demo")
            if entry["id"] == manual_id
        )
        self.assertEqual(updated["reinforcement"], 1.7)

        # Reopen + full replay: the evidence event is replayed idempotently.
        reopened = build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=None,
        )
        reopened.recover("demo")
        replayed = next(
            entry for entry in reopened.persona_service.entries("demo")
            if entry["id"] == manual_id
        )
        self.assertEqual(replayed["reinforcement"], 1.7)
        self.assertEqual(replayed["text"], "主人更喜欢被直接问")

    async def test_correction_queue_four_actions(self):
        model = FakeModel([
            {"action": "merge", "text": "合并后 A"},
            {"action": "keep_old"},
            {"action": "keep_both"},
        ])
        core = self.build(model=model)
        core.recover("demo")
        for text in ("旧文本A", "旧文本B", "旧文本C"):
            core.persona_service.add_manual("demo", text, entity="relationship")
        for old, new in (
            ("旧文本A", "新文本A"), ("旧文本B", "新文本B"), ("旧文本C", "新文本C"),
        ):
            queued = core.persona_service.enqueue_correction(
                "demo", {"old_text": old, "new_text": new, "entity": "relationship"},
            )
            self.assertEqual(queued["status"], "queued")
        duplicate = core.persona_service.enqueue_correction(
            "demo", {"old_text": "旧文本A", "new_text": "新文本A", "entity": "relationship"},
        )
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(len(core.persona_service.corrections("demo")), 3)

        result = await core.persona_service.resolve_corrections_async("demo")
        self.assertEqual(result["resolved"], 3)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(result["actions"], {"merge": 1, "keep_old": 1, "keep_both": 1})
        self.assertEqual(
            [call["purpose"] for call in model.calls],
            ["persona_correction_resolution"] * 3,
        )
        texts = {entry["text"] for entry in core.persona_service.entries("demo")}
        self.assertIn("合并后 A", texts)
        self.assertIn("旧文本B", texts)
        self.assertIn("旧文本C", texts)
        self.assertIn("新文本C", texts)
        self.assertNotIn("旧文本A", texts)

        # Deterministic fallback (no model): keep_new replaces the old row.
        plain = build_memory_core(
            memory_root=self.root / "plain", clock=self.clock, model=None,
        )
        plain.recover("demo")
        plain.persona_service.add_manual("demo", "旧文本D", entity="relationship")
        plain.persona_service.enqueue_correction(
            "demo", {"old_text": "旧文本D", "new_text": "新文本D", "entity": "relationship"},
        )
        fallback = await plain.persona_service.resolve_corrections_async("demo")
        self.assertEqual(fallback["resolved"], 1)
        self.assertEqual(
            [entry["text"] for entry in plain.persona_service.entries("demo")],
            ["新文本D"],
        )

    async def test_entry_updated_replay_sha_mismatch_pauses(self):
        core = self.build()
        core.recover("demo")
        persona_id = core.persona_service.add_manual(
            "demo", "原始文本", entity="relationship",
        )
        body = "被事件改写后的文本"
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        core.journal.append("demo", PERSONA_ENTRY_EVENT, {
            "character": "demo",
            "persona_id": persona_id,
            "entity": "relationship",
            "rewrite_text_sha256": "0" * 64,
            "text": body,
        })
        result = core.recover("demo")
        self.assertTrue(result.paused)
        self.assertIn("sha256", result.reason or "")
        entries = core.persona_service.entries("demo")
        self.assertEqual(entries[0]["text"], "原始文本")

        # A matching sha applies the rewrite on a clean replay (the paused bad
        # event above is still in that journal, so a fresh root is used).
        clean_root = self.root / "clean"
        clean = build_memory_core(memory_root=clean_root, clock=self.clock, model=None)
        clean.recover("demo")
        clean_id = clean.persona_service.add_manual(
            "demo", "原始文本", entity="relationship",
        )
        clean.journal.append("demo", PERSONA_ENTRY_EVENT, {
            "character": "demo",
            "persona_id": clean_id,
            "entity": "relationship",
            "rewrite_text_sha256": digest,
            "text": body,
        })
        applied = clean.recover("demo")
        self.assertFalse(applied.paused)
        self.assertEqual(clean.persona_service.entries("demo")[0]["text"], body)

    async def test_retract_source_stops_serving_derived_entries(self):
        core = self.build()
        core.recover("demo")
        reflection = reflection_row()
        core.reflections.save("demo", [reflection])
        persona_id = core.persona_service.add_reflection("demo", reflection)
        self.assertIn("主人喜欢被直接问", core.persona_service.render_block("demo"))

        marked = core.persona_service.retract_source(
            "demo", source="reflection", source_id="ref_a",
        )
        self.assertEqual(marked, [persona_id])
        self.assertEqual(core.persona_service.entries("demo"), [])
        self.assertNotIn("主人喜欢被直接问", core.persona_service.render_block("demo"))
        self.assertEqual(
            core.persona_service.retract_source(
                "demo", source="reflection", source_id="ref_a",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
