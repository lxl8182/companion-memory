"""B02 acceptance: durable receipts, recovery, and no-overwrite corruption policy.

These tests exercise the public path only (DurableEventStore -> journal ->
source ledger on a real temporary data root), because the acceptance criteria
are about the closed loop rather than any single store: a receipt is durable
only after the commit, a retried commit lands once, an interrupted commit is
repaired on reopen, and a damaged or newer store is never replaced by an
empty one.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from companion_memory.adapters.durable import DurableEventStore, build_durable_event_store
from companion_memory.contracts import MemoryEvent, Scope
from companion_memory.errors import CorruptStoreError, InvalidSchemaError
from companion_memory.persistence.atomic import FileSystemAtomicStore
from companion_memory.persistence.clock import SystemClock
from companion_memory.persistence.journal import EventJournal
from companion_memory.persistence.permits import NullWritePermit, RejectingWritePermit
from companion_memory.persistence.records import event_snapshot
from companion_memory.persistence.roots import FileSystemRoots
from companion_memory.persistence.source_ledger import SourceLedger


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class DurableReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()
        self.stack = self.build(self.root, self.clock, NullWritePermit())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, root: Path, clock, permit) -> dict:
        store = FileSystemAtomicStore()
        roots = FileSystemRoots(root / "memory")
        ledger = SourceLedger(roots=roots, store=store, clock=clock, permit=permit)
        journal = EventJournal(roots=roots, store=store, clock=clock, permit=permit)
        sink = DurableEventStore(
            roots=roots, journal=journal, ledger=ledger, permit=permit, clock=clock,
        )
        return {"store": store, "roots": roots, "ledger": ledger, "journal": journal, "sink": sink}

    def event(self, key: str = "host:demo:private:one", *, text: str = "今天把框架搭好了") -> MemoryEvent:
        return MemoryEvent(
            scope=Scope("demo"), source_key=key, event_type="user_message", text=text,
            occurred_at=datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc),
            content_class="chat",
        )

    async def test_receipt_is_durable_and_retry_applies_once(self):
        sink = self.stack["sink"]
        first = await sink.append(self.event())
        self.assertEqual(first.status, "accepted")
        self.assertTrue(first.durable)
        self.assertIsNotNone(first.event_id)
        self.assertEqual(first.processing_position, 0)

        retry = await sink.append(self.event())
        self.assertEqual(retry.status, "duplicate")
        self.assertTrue(retry.durable)

        conflict = await sink.append(self.event(text="完全不同的内容"))
        self.assertEqual(conflict.status, "conflict")
        self.assertTrue(conflict.durable)

        second = await sink.append(self.event("host:demo:private:two"))
        self.assertEqual(second.status, "accepted")
        self.assertEqual(second.processing_position, 1)

        # Reopen the whole stack on the same data root: ids, scope, time and
        # position survive, and the duplicate is recognised from disk.
        reopened = self.build(self.root, self.clock, NullWritePermit())
        entries = reopened["journal"].read_since("demo", -1)
        self.assertEqual([entry.position for entry in entries], [0, 1])
        self.assertEqual(entries[0].event_id, first.event_id)
        self.assertEqual(entries[0].payload["source_key"], "host:demo:private:one")
        self.assertEqual(entries[0].payload["scope"], {"character": "demo", "subject": "private"})
        self.assertEqual(entries[0].payload["occurred_at"], "2026-10-22T09:00:00+00:00")
        self.assertEqual(entries[0].payload["received_at"], "2026-10-22T09:30:00+00:00")
        self.assertEqual(reopened["journal"].read_sentinel("demo").position, 1)
        self.assertEqual(reopened["ledger"].count("demo"), 2)
        duplicate_after_reopen = await reopened["sink"].append(self.event())
        self.assertEqual(duplicate_after_reopen.status, "duplicate")
        self.assertEqual(reopened["ledger"].count("demo"), 2)

    async def test_interrupted_commit_is_repaired_on_reopen(self):
        sink = self.stack["sink"]
        character = "demo"
        # Simulate the crash window: the event is fsynced into the journal but
        # the ledger view was never saved, so the receipt was never returned.
        prepared = self.event()
        snapshot = event_snapshot(prepared)
        entry = self.stack["journal"].append(character, DurableEventStore.EVENT_TYPE, snapshot)
        self.assertEqual(entry.position, 0)
        self.assertEqual(self.stack["ledger"].count(character), 0)
        self.assertEqual(self.stack["journal"].read_sentinel(character).position, -1)

        reopened = self.build(self.root, self.clock, NullWritePermit())
        recovery = reopened["sink"].recover(character)
        self.assertFalse(recovery.paused)
        self.assertEqual(recovery.applied, 1)
        self.assertEqual(reopened["ledger"].count(character), 1)
        self.assertEqual(reopened["journal"].read_sentinel(character).position, 0)

        # The caller retries the lost acknowledgement: exactly one effect.
        retry = await reopened["sink"].append(self.event())
        self.assertEqual(retry.status, "duplicate")
        self.assertTrue(retry.durable)
        self.assertEqual(reopened["ledger"].count(character), 1)
        self.assertEqual(len(reopened["journal"].read_since(character, -1)), 1)

    async def test_corrupt_or_unknown_store_is_never_overwritten(self):
        ledger_path = self.stack["roots"].character_file("demo", "source_events.json")
        await self.stack["sink"].append(self.event())

        # A damaged store is reported, not repaired or emptied.
        ledger_path.write_text("{not json", encoding="utf-8")
        damaged = ledger_path.read_bytes()
        with self.assertRaises(CorruptStoreError) as corrupt:
            await self.stack["sink"].append(self.event("host:demo:private:two"))
        self.assertEqual(corrupt.exception.path, str(ledger_path))
        self.assertEqual(ledger_path.read_bytes(), damaged)

        # A newer schema is rejected before any write, and left untouched.
        ledger_path.write_text(
            json.dumps({"schema_version": 99, "events": {}}), encoding="utf-8",
        )
        newer = ledger_path.read_bytes()
        with self.assertRaises(InvalidSchemaError):
            await self.stack["sink"].append(self.event("host:demo:private:three"))
        self.assertEqual(ledger_path.read_bytes(), newer)

        # A write permit is the last fence: a rejected write reports a
        # non-durable receipt and changes nothing on disk.
        fresh_root = self.root / "fenced"
        fenced = self.build(fresh_root, self.clock, RejectingWritePermit())
        rejected = await fenced["sink"].append(self.event())
        self.assertEqual(rejected.status, "rejected")
        self.assertFalse(rejected.durable)
        self.assertEqual(rejected.reason, "write_rejected")
        self.assertFalse(
            fenced["roots"].character_file("demo", "events.ndjson").exists()
        )
        self.assertEqual(fenced["ledger"].count("demo"), 0)


class DurableWiringTests(unittest.TestCase):
    def test_build_helper_wires_an_isolated_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sink = build_durable_event_store(memory_root=root / "memory")
            self.assertIsInstance(sink, DurableEventStore)
            self.assertEqual(
                sink.recovery_state("demo"), None,
            )
            self.assertFalse((root / "memory").exists())


if __name__ == "__main__":
    unittest.main()
