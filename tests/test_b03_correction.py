"""B03 correction acceptance: record first, invalidate, filter reads.

The public path only: a correction must stop the replaced facts from entering
reads, a duplicate submission must not bump the version or re-propagate, a
retraction must keep already-applied invalidations (host semantics), and the
deleted-content filter must keep a correction block from resurrecting text the
user deleted.
"""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from companion_memory.adapters.composition import build_memory_core
from companion_memory.contracts import (
    CorrectionRequest, DeletionRequest, DeletionTarget, Scope,
)
from companion_memory.errors import (
    CorrectionNotFoundError, CorruptStoreError, InvalidSchemaError,
)
from companion_memory.persistence.corrections import (
    CorrectionStore, fact_correction_id, preference_correction_id,
)
from companion_memory.persistence.atomic import FileSystemAtomicStore
from companion_memory.persistence.permits import NullWritePermit
from companion_memory.persistence.roots import FileSystemRoots


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class CorrectionFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()
        self.core = build_memory_core(memory_root=self.root / "memory", clock=self.clock)
        self.core.facts.save("demo", [
            {"id": "f1", "text": "用户以前住在东京", "source_event_keys": ["host:demo:private:e1"]},
            {"id": "f2", "text": "用户喜欢猫"},
        ])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_correction_invalidates_matching_facts_and_is_idempotent(self):
        receipt = await self.core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), action="replace",
            old_text="以前住在东京", new_text="现在住在大阪",
            source_key="host:demo:private:corr1",
        ))
        self.assertTrue(receipt.applied)
        self.assertFalse(receipt.duplicate)
        self.assertEqual(receipt.invalidated_fact_ids, ("f1",))
        self.assertGreaterEqual(receipt.version, 1)
        rows = {row["id"]: row for row in self.core.facts.load("demo")}
        self.assertTrue(rows["f1"]["invalidated_at"])
        self.assertEqual(rows["f1"]["invalidated_by"], receipt.correction_id)
        self.assertNotIn("invalidated_at", rows["f2"])

        block = self.core.correction_service.block("demo", language="zh")
        self.assertIn("现在住在大阪", block.block)
        self.assertIn("以前住在东京", block.block)
        self.assertEqual(block.version, receipt.version)

        duplicate = await self.core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), action="replace",
            old_text="以前住在东京", new_text="现在住在大阪",
            source_key="host:demo:private:corr1",
        ))
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.correction_id, receipt.correction_id)
        self.assertEqual(duplicate.version, receipt.version)
        self.assertEqual(duplicate.invalidated_fact_ids, ("f1",))

        reopened = build_memory_core(memory_root=self.root / "memory", clock=self.clock)
        self.assertEqual(
            reopened.correction_service.version("demo"), receipt.version,
        )
        self.assertEqual(
            reopened.correction_service.get("demo", receipt.correction_id)["status"],
            "active",
        )

    async def test_retract_keeps_applied_invalidations_and_hides_block(self):
        receipt = await self.core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), action="replace",
            old_text="以前住在东京", new_text="现在住在大阪",
        ))
        retracted = await self.core.correction_service.retract(
            "demo", receipt.correction_id,
        )
        self.assertTrue(retracted.applied is False)
        self.assertEqual(retracted.status, "retracted")
        self.assertGreater(retracted.version, receipt.version)
        # Host semantics: retracting the correction does not restore the fact.
        rows = {row["id"]: row for row in self.core.facts.load("demo")}
        self.assertTrue(rows["f1"]["invalidated_at"])
        self.assertEqual(self.core.correction_service.block("demo").block, "")
        again = await self.core.correction_service.retract("demo", receipt.correction_id)
        self.assertFalse(again.applied)
        with self.assertRaises(CorrectionNotFoundError):
            await self.core.correction_service.retract("demo", "corr_missing")

        preference = await self.core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), action="replace", new_text="喜欢安静的咖啡馆",
        ))
        self.assertIn("喜欢安静的咖啡馆", self.core.correction_service.block("demo").block)
        self.assertTrue(preference.applied)

    async def test_deleting_the_corrected_fact_hides_the_correction_block(self):
        receipt = await self.core.correction_service.correct(CorrectionRequest(
            scope=Scope("demo"), action="replace",
            old_text="以前住在东京", new_text="现在住在大阪",
        ))
        self.assertEqual(receipt.invalidated_fact_ids, ("f1",))
        await self.core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), targets=(DeletionTarget("fact", "f1"),),
        ))
        block = self.core.correction_service.block("demo", language="zh")
        self.assertNotIn("现在住在大阪", block.block)
        self.assertNotIn("以前住在东京", block.block)

    async def test_store_format_formulas_and_strict_read(self):
        # Deterministic ids must match the host formulas exactly.
        expected_pref = "corr_" + hashlib.sha1(
            "preference|host:demo:private:e1|喜欢猫".encode("utf-8"),
        ).hexdigest()[:16]
        self.assertEqual(
            preference_correction_id("host:demo:private:e1", "喜欢猫"),
            expected_pref,
        )
        expected_fact = "corr_" + hashlib.sha1(
            "correction|host:demo:private:e1|f1,f2|k1,k2|旧说法|新说法|replace".encode("utf-8"),
        ).hexdigest()[:16]
        self.assertEqual(
            fact_correction_id(
                "host:demo:private:e1", ["f2", "f1"], ["k2", "k1"],
                "旧说法", "新说法", "replace",
            ),
            expected_fact,
        )

        roots = FileSystemRoots(self.root / "memory")
        store = CorrectionStore(
            roots=roots, store=FileSystemAtomicStore(),
            clock=self.clock, permit=NullWritePermit(),
        )
        path = self.root / "memory" / "demo" / "memory_corrections.json"
        path.write_text(json.dumps({
            "schema": 1, "version": 3,
            "corrections": [{
                "correction_id": "corr_hostwritten", "kind": "preference",
                "text": "宿主写入的偏好", "status": "active",
                "created_at": "2026-10-21T09:30:00",
                "unknown_future_field": {"kept": True},
            }],
        }, ensure_ascii=False), encoding="utf-8")
        loaded = store.load_view("demo")
        self.assertEqual(loaded["version"], 3)
        self.assertEqual(
            loaded["corrections"][0]["unknown_future_field"], {"kept": True},
        )

        path.write_text("{broken", encoding="utf-8")
        damaged = path.read_bytes()
        with self.assertRaises(CorruptStoreError):
            store.load_view("demo")
        self.assertEqual(path.read_bytes(), damaged)
        path.write_text(json.dumps({"schema": 7, "version": 1, "corrections": []}), encoding="utf-8")
        with self.assertRaises(InvalidSchemaError):
            store.load_view("demo")


if __name__ == "__main__":
    unittest.main()
