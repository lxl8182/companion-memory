"""QP05: deletion reaches the plugin that consumed a web-created fact.

The gap this pins: a fact created on the web/console has a source key without
a plugin segment, so `plugin_ids` never mentions the QQ plugin that recalled
and injected it. Deleting the fact therefore never notified that plugin, and the
injected copy stayed in the DSH history. The fix is a reserve-time consumer
reference (ids only) that the deletion queue also matches on.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import DeletionRequest, DeletionTarget, Scope


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


#: One web-created fact: no plugin segment anywhere in its source key.
WEB_FACT = {
    "id": "f_web_tea",
    "text": "用户只喝乌龙茶，不喝红茶。",
    "subject": "private",
    "source_event_keys": ["host:demo:private:web-1"],
}


class ConsumerRefDeletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "memory"
        self.clock = FixedClock()
        self.core = self.build()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self):
        core = build_memory_core(memory_root=self.root, clock=self.clock)
        core.facts.save("demo", [dict(WEB_FACT)])
        return core

    def consume(self, core, *, plugin_id="dsh_memory", request_id="turn-1",
                memory_ids=("f_web_tea",), session_id="qq-session"):
        """What /v1/context/reserve records when a plugin used these ids."""
        return core.consumer_refs.record("demo", {
            "plugin_id": plugin_id, "session_id": session_id,
            "request_id": request_id, "character": "demo", "subject": "private",
            "memory_ids": list(memory_ids), "source_ids": ["host:demo:private:web-1"],
            "persona_version": "v1", "context_generation": "v1:1",
            "deletion_version": 0,
        })

    async def test_web_fact_deletion_reaches_the_consuming_plugin_and_survives_restart(self):
        self.assertTrue(self.consume(self.core))
        # Same key, same content: reserve is idempotent, no second reference.
        self.assertFalse(self.consume(self.core))
        receipt = await self.core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "f_web_tea"),),
            reason="user_request",
        ))
        pending = self.core.deletion_service.pending_plugin_deletions("demo", "dsh_memory")
        self.assertTrue(pending, "the consuming plugin must be notified")
        self.assertEqual(pending[0]["deletion_id"], receipt.deletion_id)
        self.assertIn("f_web_tea", pending[0]["memory_ids"])
        # The plugin is told which session held the copy, so it need not guess.
        self.assertEqual(pending[0]["consumer_sessions"], ["qq-session"])
        # Ids only: the reference table must never hold the deleted text.
        raw = (self.root / "demo" / "memory_consumer_refs.json").read_text(encoding="utf-8")
        self.assertNotIn(WEB_FACT["text"], raw)
        self.assertNotIn("乌龙茶", raw)

        # A real restart: rebuild the whole core over the same root.
        reopened = self.build()
        again = reopened.deletion_service.pending_plugin_deletions("demo", "dsh_memory")
        self.assertEqual([row["deletion_id"] for row in again], [receipt.deletion_id])

        # Acking the cleanup retires the notification and the reference.
        done = await reopened.deletion_service.record_plugin_receipt(
            "demo", receipt.deletion_id, "dsh_memory", "cleaned",
            cleaned=["f_web_tea"],
        )
        self.assertEqual(done.status, "completed")
        self.assertEqual(
            reopened.deletion_service.pending_plugin_deletions("demo", "dsh_memory"), [],
        )
        self.assertEqual(
            reopened.consumer_refs.memory_ids_for("demo", "dsh_memory"), [],
        )
        # ... and the retirement is durable too.
        third = self.build()
        self.assertEqual(
            third.deletion_service.pending_plugin_deletions("demo", "dsh_memory"), [],
        )

    async def test_existing_plugin_priority_and_order_are_unchanged(self):
        """The source-key plugin stays first and appears exactly once."""
        self.core.facts.save("demo", [dict(WEB_FACT, source_event_keys=[
            "host:demo:private:web-1", "plugin:p_source:demo:private:s1:e1",
        ])])
        self.consume(self.core, plugin_id="dsh_memory", request_id="turn-1")
        receipt = await self.core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "f_web_tea"),),
        ))
        record = (self.core.deletions.snapshot("demo")["deletions"])[0]
        self.assertEqual(record["plugin_ids"], ["p_source", "dsh_memory"])
        rows = self.core.deletion_service.pending_plugin_deletions("demo", "p_source")
        self.assertEqual([row["deletion_id"] for row in rows], [receipt.deletion_id])

    async def test_an_unrelated_plugin_reference_produces_no_notification(self):
        # Both plugins used a *different* memory; neither saw the deleted one.
        self.consume(self.core, plugin_id="other_game", request_id="turn-other",
                     memory_ids=("f_other",))
        self.consume(self.core, plugin_id="dsh_memory", request_id="turn-1",
                     memory_ids=("f_other",))
        await self.core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "f_web_tea"),),
        ))
        self.assertEqual(
            self.core.deletion_service.pending_plugin_deletions("demo", "other_game"), [],
        )
        self.assertEqual(
            self.core.deletion_service.pending_plugin_deletions("demo", "dsh_memory"), [],
        )


if __name__ == "__main__":
    unittest.main()
