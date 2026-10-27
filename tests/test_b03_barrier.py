"""B03 barrier acceptance: one fence for reads, replays and in-flight work.

The barrier must answer four questions from durable state only: was this
source key / text / episode / entry deleted; is a captured snapshot stale;
does a correction entry resurrect deleted content; and do scoped forget epochs
fail closed. The durable event sink is checked through its public path: a
rejected re-ingest and a skipped replay for a deleted source.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import DeletionRequest, MemoryEvent, Scope
from rin_memory.domain.barrier import DeletionBarrier
from rin_memory.errors import BarrierStaleError, CorruptStoreError
from rin_memory.invalidation import text_hash
from rin_memory.persistence.atomic import FileSystemAtomicStore
from rin_memory.persistence.corrections import CorrectionStore
from rin_memory.persistence.deletions import DeletionStore
from rin_memory.persistence.permits import NullWritePermit
from rin_memory.persistence.records import event_snapshot
from rin_memory.persistence.roots import FileSystemRoots
from rin_memory.persistence.tombstones import ScopedTombstoneStore


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


def event(key: str = "host:demo:private:e1") -> MemoryEvent:
    return MemoryEvent(
        scope=Scope("demo"), source_key=key, event_type="user_message",
        text="今天天气不错",
        occurred_at=datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc),
    )


class BarrierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()
        self.core = build_memory_core(memory_root=self.root / "memory", clock=self.clock)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_predicates_cover_source_text_episode_and_target(self):
        digest = text_hash("秘密文本A")
        # Drive the store directly: the public async forget path is covered
        # by the other tests, this one isolates the predicate matrix.
        roots = self.root / "memory"
        store = DeletionStore(
            roots=FileSystemRoots(roots), store=FileSystemAtomicStore(),
            clock=self.clock, permit=NullWritePermit(),
        )
        prepared = store.prepare_registration("demo", {
            "scope": "entry",
            "targets": [
                {"type": "fact", "id": "f1"},
                {"type": "fact", "id": "f2", "text_hash": digest},
                {"type": "episode", "id": "ep:host:x:2026-10-22"},
            ],
            "source_keys": ["host:demo:private:e9"],
            "subject": "private",
        })
        view = store.load_view("demo")
        self.assertTrue(store.apply_snapshot(view, prepared.payload))
        store.save_view("demo", view)
        barrier = DeletionBarrier(
            deletions=store,
            tombstones=ScopedTombstoneStore(
                roots=FileSystemRoots(roots), store=FileSystemAtomicStore(),
                clock=self.clock, permit=NullWritePermit(),
            ),
            corrections=CorrectionStore(
                roots=FileSystemRoots(roots), store=FileSystemAtomicStore(),
                clock=self.clock, permit=NullWritePermit(),
            ),
        )
        self.assertTrue(barrier.blocks_target("demo", "fact", "f1"))
        self.assertTrue(barrier.blocks_text("demo", "秘密文本A"))
        self.assertTrue(barrier.blocks_episode("demo", "ep:host:x:2026-10-22"))
        self.assertTrue(barrier.blocks_source_key("demo", "host:demo:private:e9"))
        self.assertFalse(barrier.blocks_source_key("demo", "host:demo:private:e8"))
        self.assertTrue(barrier.blocks_row("demo", {
            "id": "f9", "text": "秘密文本A",
        }))
        self.assertTrue(barrier.blocks_row("demo", {
            "id": "f9", "text": "无关内容",
            "source_event_keys": ["host:demo:private:e9"],
        }))
        self.assertFalse(barrier.blocks_row("demo", {"id": "f9", "text": "无关内容"}))
        self.assertTrue(barrier.blocks_correction_entry("demo", {
            "correction_id": "corr_1", "kind": "fact_correction",
            "text": "秘密文本A", "replaces_text": None,
            "target_fact_ids": [], "target_source_keys": [],
        }))
        self.assertFalse(barrier.blocks_correction_entry("demo", {
            "correction_id": "corr_2", "kind": "fact_correction",
            "text": "完全无关", "replaces_text": None,
            "target_fact_ids": [], "target_source_keys": [],
        }))

    async def test_scoped_epochs_fail_closed_and_snapshot_goes_stale(self):
        snapshot = self.core.barrier.snapshot("demo")
        self.core.tombstones.bump_forget("demo", "acct:u_1", 2)
        self.core.barrier.refresh("demo")
        self.assertEqual(self.core.barrier.forget_epoch("demo", "acct:u_1"), 2)
        self.assertFalse(self.core.barrier.is_erased("demo", "acct:u_1", 2))
        self.core.tombstones.bump_erased("demo", "acct:u_1", 2)
        self.core.barrier.refresh("demo")
        self.assertTrue(self.core.barrier.is_erased("demo", "acct:u_1", 2))
        self.assertTrue(self.core.barrier.is_stale("demo", snapshot))
        with self.assertRaises(BarrierStaleError):
            self.core.barrier.assert_fresh("demo", snapshot)

        path = self.root / "memory" / "demo" / "scoped_tombstones.json"
        path.write_text("{damaged", encoding="utf-8")
        damaged = path.read_bytes()
        fresh = build_memory_core(memory_root=self.root / "memory", clock=self.clock)
        with self.assertRaises(CorruptStoreError):
            fresh.barrier.state("demo")
        self.assertEqual(path.read_bytes(), damaged)

    async def test_durable_sink_rejects_reingest_and_skips_old_replay(self):
        first = await self.core.sink.append(event())
        self.assertEqual(first.status, "accepted")
        self.assertEqual(self.core.ledger.count("demo"), 1)
        receipt = await self.core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:e1",),
        ))
        self.assertEqual(receipt.status, "completed")
        self.assertEqual(self.core.ledger.count("demo"), 0)
        rejected = await self.core.sink.append(event())
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.reason, "deleted_source")
        # A crash tail that still contains the old source event replays as a
        # no-op: the ledger is not rebuilt for a deleted source key.
        self.core.journal.append(
            "demo", "core.source_recorded", event_snapshot(event()),
        )
        result = self.core.journal.reconcile("demo")
        self.assertFalse(result.paused)
        self.assertEqual(self.core.ledger.count("demo"), 0)


if __name__ == "__main__":
    unittest.main()
