"""B02 unit tests: source ledger, request idempotency, scoped tombstones.

Each test drives one store on a real temporary data root and checks the
contracts the B02 acceptance depends on: idempotent upserts, bounded growth,
host-format compatibility, restart survival, monotonic fences, permit
enforcement, and the no-overwrite corruption policy.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion_memory.contracts import MemoryEvent, Scope
from companion_memory.errors import (
    CorruptStoreError, InvalidSchemaError, SourceConflictError, WriteRejected,
)
from companion_memory.persistence.atomic import FileSystemAtomicStore
from companion_memory.persistence.clock import SystemClock
from companion_memory.persistence.idempotency import IdempotencyStore
from companion_memory.persistence.permits import NullWritePermit, RejectingWritePermit
from companion_memory.persistence.records import event_fingerprint, event_snapshot
from companion_memory.persistence.roots import FileSystemRoots
from companion_memory.persistence.source_ledger import SourceLedger
from companion_memory.persistence.tombstones import ScopedTombstoneStore, TombstoneState


def make_event(
    source_key: str = "host:demo:private:one", *, text: str = "hello",
    subject: str = "private",
) -> MemoryEvent:
    return MemoryEvent(
        scope=Scope("demo", subject),
        source_key=source_key,
        event_type="user_message",
        text=text,
        occurred_at=datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc),
        content_class="chat",
    )


class SourceLedgerTests(unittest.TestCase):
    def test_ledger_upserts_caps_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = FileSystemRoots(tmp)
            store = FileSystemAtomicStore()
            ledger = SourceLedger(
                roots=roots, store=store, clock=SystemClock(), permit=NullWritePermit(),
            )
            event = make_event()
            snapshot = event_snapshot(event)

            first = ledger.record_snapshot("demo", snapshot)
            self.assertEqual(first.subject, "private")
            self.assertEqual(first.fingerprint, event_fingerprint(event))
            self.assertTrue(first.ingested)

            # An identical retry is a no-op: one row, still the same entry.
            self.assertEqual(ledger.record_snapshot("demo", snapshot), first)
            self.assertEqual(ledger.count("demo"), 1)
            ledger_path = roots.character_dir("demo") / "source_events.json"
            document = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(document["schema"], 1)  # host envelope, not schema_version
            self.assertEqual(list(document["entries"]), [event.source_key])

            entry = ledger.lookup(Scope("demo", "private"), event.source_key)
            self.assertIsNotNone(entry)
            self.assertEqual((entry.subject, entry.content_class), ("private", "chat"))
            self.assertEqual(entry.fingerprint, event_fingerprint(event))
            self.assertIsNone(ledger.lookup(Scope("demo", "other"), event.source_key))

            # Same source key, different meaning: conflict, stored row untouched.
            changed = event_snapshot(make_event(text="different"))
            with self.assertRaises(SourceConflictError):
                ledger.record_snapshot("demo", changed)
            self.assertEqual(ledger.count("demo"), 1)
            self.assertEqual(
                ledger.lookup(Scope("demo", "private"), event.source_key).fingerprint,
                event_fingerprint(event),
            )

            # The replay helper never raises: a conflict is a silent no-change.
            view = ledger.load_view("demo")
            self.assertFalse(ledger.apply_snapshot(view, changed))
            self.assertFalse(ledger.apply_snapshot(view, snapshot))
            self.assertTrue(ledger.apply_snapshot(
                view, event_snapshot(make_event("host:demo:private:two")),
            ))

            # 2001 rows over the cap keep the newest 2000 by ingestion time.
            for index in range(2001):
                key = f"host:demo:private:bulk-{index}"
                view[key] = {
                    "source_key": key, "subject": "private",
                    "fingerprint": f"fp-{index}",
                    "first_seen_at": float(index), "last_seen_at": float(index),
                    "count": 1, "ingested": True, "pending_since": None,
                    "attempts": 1, "host_ids": [], "mirror_ids": [],
                }
            ledger.save_view("demo", view)
            self.assertEqual(ledger.count("demo"), 2000)
            stored = ledger.snapshot("demo")
            self.assertNotIn("host:demo:private:bulk-0", stored)
            self.assertNotIn("host:demo:private:bulk-1", stored)
            self.assertIn("host:demo:private:bulk-2000", stored)
            self.assertIn(event.source_key, stored)

            # A hand-written legacy host row has no subject/fingerprint and
            # matches on source_key alone.
            ledger_path.write_text(json.dumps({
                "schema": 1,
                "entries": {
                    "host:demo:private:legacy": {
                        "source_key": "host:demo:private:legacy",
                        "first_seen_at": 1.0, "last_seen_at": 2.0, "count": 2,
                        "ingested": True, "pending_since": None, "attempts": 1,
                        "host_ids": ["msg-1"], "mirror_ids": [], "content_class": "chat",
                    },
                },
            }), encoding="utf-8")
            self.assertEqual(ledger.count("demo"), 1)
            legacy = ledger.lookup(Scope("demo", "alice"), "host:demo:private:legacy")
            self.assertIsNotNone(legacy)
            self.assertEqual((legacy.subject, legacy.fingerprint), ("", ""))
            self.assertEqual(legacy.host_ids, ("msg-1",))

            # A damaged store is reported, never repaired or emptied.
            damaged = b"{not json"
            ledger_path.write_bytes(damaged)
            with self.assertRaises(CorruptStoreError) as corrupt:
                ledger.count("demo")
            self.assertEqual(corrupt.exception.path, str(ledger_path))
            self.assertEqual(ledger_path.read_bytes(), damaged)

            # A newer schema is rejected before any write and left untouched.
            ledger_path.write_text(
                json.dumps({"schema_version": 99, "events": {}}), encoding="utf-8",
            )
            newer = ledger_path.read_bytes()
            with self.assertRaises(InvalidSchemaError):
                ledger.count("demo")
            self.assertEqual(ledger_path.read_bytes(), newer)

            # A rejected write permit changes nothing on disk.
            fenced = SourceLedger(
                roots=FileSystemRoots(tmp), store=store, clock=SystemClock(),
                permit=RejectingWritePermit(),
            )
            with self.assertRaises(WriteRejected):
                fenced.record_snapshot(
                    "fenced", event_snapshot(make_event("host:fenced:private:one")),
                )
            self.assertFalse((Path(tmp) / "fenced" / "source_events.json").exists())


class IdempotencyStoreTests(unittest.TestCase):
    def test_idempotency_reserves_completes_and_purges(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = FileSystemRoots(tmp)
            store = FileSystemAtomicStore()
            clock = SystemClock()
            idempotency = IdempotencyStore(
                roots=roots, store=store, clock=clock, permit=NullWritePermit(),
            )

            reserved = idempotency.reserve("demo", "key-one", "fingerprint-a")
            self.assertEqual(reserved.status, "new")
            self.assertEqual(reserved.record["state"], "pending")

            # A concurrent retry sees the in-flight reservation, a changed
            # fingerprint is a conflict and never overwrites the record.
            self.assertEqual(
                idempotency.reserve("demo", "key-one", "fingerprint-a").status,
                "in_progress",
            )
            conflict = idempotency.reserve("demo", "key-one", "fingerprint-b")
            self.assertEqual(conflict.status, "conflict")
            self.assertEqual(conflict.record["fingerprint"], "fingerprint-a")

            idempotency.complete("demo", "key-one", {"ok": True, "value": 7})
            done = idempotency.reserve("demo", "key-one", "fingerprint-a")
            self.assertEqual(done.status, "done")
            self.assertEqual(done.record["response"], {"ok": True, "value": 7})

            # The response survives a restart: a new store on the same root
            # replays it from the staging copy.
            staging = roots.character_dir("demo") / "idempotency_staging"
            self.assertEqual(len(list(staging.glob("*.json"))), 1)
            reopened = IdempotencyStore(
                roots=FileSystemRoots(tmp), store=store, clock=clock,
                permit=NullWritePermit(),
            )
            replayed = reopened.reserve("demo", "key-one", "fingerprint-a")
            self.assertEqual(replayed.status, "done")
            self.assertEqual(replayed.record["response"], {"ok": True, "value": 7})

            # A cancelled reservation may be retried with any fingerprint.
            self.assertEqual(reopened.reserve("demo", "key-two", "fp-two").status, "new")
            reopened.cancel("demo", "key-two")
            self.assertEqual(reopened.lookup("demo", "key-two")["state"], "cancelled")
            retried = reopened.reserve("demo", "key-two", "fp-other")
            self.assertEqual(retried.status, "new")
            self.assertEqual(retried.record["state"], "pending")

            # Purge drops old terminal records and their staging, keeps pending.
            self.assertEqual(reopened.reserve("demo", "key-three", "fp-three").status, "new")
            reopened.complete("demo", "key-three", {"ok": True})
            self.assertEqual(len(list(staging.glob("*.json"))), 2)
            self.assertEqual(
                reopened.purge("demo", clock.now() + timedelta(minutes=1)), 2,
            )
            self.assertIsNone(reopened.lookup("demo", "key-one"))
            self.assertIsNotNone(reopened.lookup("demo", "key-two"))
            self.assertEqual(list(staging.glob("*.json")), [])

            # A rejected write permit changes nothing on disk.
            fenced = IdempotencyStore(
                roots=FileSystemRoots(tmp), store=store, clock=clock,
                permit=RejectingWritePermit(),
            )
            with self.assertRaises(WriteRejected):
                fenced.reserve("fenced", "key-one", "fp")
            self.assertFalse((Path(tmp) / "fenced" / "idempotency_keys.json").exists())

            # A damaged key file is reported, never treated as empty.
            keys_path = roots.character_dir("demo") / "idempotency_keys.json"
            damaged = b"{not json"
            keys_path.write_bytes(damaged)
            with self.assertRaises(CorruptStoreError) as corrupt:
                reopened.reserve("demo", "key-four", "fp-four")
            self.assertEqual(corrupt.exception.path, str(keys_path))
            self.assertEqual(keys_path.read_bytes(), damaged)

            keys_path.write_text(
                json.dumps({"schema_version": 99, "keys": {}}), encoding="utf-8",
            )
            newer = keys_path.read_bytes()
            with self.assertRaises(InvalidSchemaError):
                reopened.reserve("demo", "key-four", "fp-four")
            self.assertEqual(keys_path.read_bytes(), newer)


class ScopedTombstoneStoreTests(unittest.TestCase):
    def test_tombstones_are_monotonic_per_subject_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = FileSystemRoots(tmp)
            store = FileSystemAtomicStore()
            tombstones = ScopedTombstoneStore(
                roots=roots, store=store, clock=SystemClock(), permit=NullWritePermit(),
            )

            self.assertEqual(tombstones.read("demo", "alice"), TombstoneState(0, 0))
            self.assertFalse(tombstones.is_erased("demo", "alice", 1))

            # Monotonic: a stale bump never lowers the fence.
            self.assertEqual(tombstones.bump_forget("demo", "alice", 3).forget_epoch, 3)
            self.assertEqual(tombstones.bump_forget("demo", "alice", 1).forget_epoch, 3)
            self.assertEqual(tombstones.read("demo", "alice").forget_epoch, 3)

            # Subjects keep independent epoch domains.
            self.assertEqual(tombstones.read("demo", "bob").forget_epoch, 0)
            tombstones.bump_forget("demo", "bob", 1)
            self.assertEqual(tombstones.read("demo", "bob").forget_epoch, 1)
            self.assertEqual(tombstones.read("demo", "alice").forget_epoch, 3)

            state = tombstones.bump_erased("demo", "alice", 2)
            self.assertEqual((state.forget_epoch, state.erased_epoch), (3, 2))
            self.assertTrue(tombstones.is_erased("demo", "alice", 2))
            self.assertFalse(tombstones.is_erased("demo", "alice", 3))
            self.assertTrue(tombstones.is_erased("demo", "alice", 0))

            # A completed erase raises the fence to cover it.
            state = tombstones.bump_erased("demo", "alice", 5)
            self.assertEqual((state.forget_epoch, state.erased_epoch), (5, 5))
            self.assertTrue(tombstones.is_erased("demo", "alice", 5))
            self.assertEqual(sorted(tombstones.snapshot("demo")), ["alice", "bob"])

            # Fail closed: a damaged barrier is never read as "nothing erased".
            path = roots.character_dir("demo") / "scoped_tombstones.json"
            damaged = b'{"alice": {"forget_epoch": 3'
            path.write_bytes(damaged)
            with self.assertRaises(CorruptStoreError) as corrupt:
                tombstones.read("demo", "alice")
            self.assertEqual(corrupt.exception.path, str(path))
            self.assertEqual(path.read_bytes(), damaged)

            path.write_text(
                json.dumps({"alice": {"forget_epoch": -1, "erased_epoch": 0}}),
                encoding="utf-8",
            )
            with self.assertRaises(CorruptStoreError):
                tombstones.read("demo", "alice")

            # The host writes a forget-only row without erased_epoch; that
            # means no erase completed yet, not damage.
            path.write_text(json.dumps({
                "alice": {"forgotten_at": 5.0, "forget_epoch": 2},
            }), encoding="utf-8")
            host_row = tombstones.read("demo", "alice")
            self.assertEqual((host_row.forget_epoch, host_row.erased_epoch), (2, 0))

            # A rejected permit blocks the write and creates no file.
            fenced = ScopedTombstoneStore(
                roots=FileSystemRoots(tmp), store=store, clock=SystemClock(),
                permit=RejectingWritePermit(),
            )
            with self.assertRaises(WriteRejected):
                fenced.bump_forget("fenced", "alice", 1)
            fenced_path = Path(tmp) / "fenced" / "scoped_tombstones.json"
            self.assertFalse(fenced_path.exists())

            # A stale bump is a no-op and needs no write permit.
            fenced_path.parent.mkdir(parents=True)
            fenced_path.write_text(json.dumps({
                "alice": {"forgotten_at": 1.0, "forget_epoch": 4, "erased_epoch": 0},
            }), encoding="utf-8")
            self.assertEqual(fenced.bump_forget("fenced", "alice", 2).forget_epoch, 4)


if __name__ == "__main__":
    unittest.main()
