"""B02 persistence tests: journal durability/recovery, outbox, cursors, permit.

Three scenarios (the B02 acceptance behaviours for this slice):
  (a) append + reopen keeps positions/ids/payloads, read_since filters, an
      unregistered type is refused, and a corrupt mid-file line is reported
      without the file being rewritten;
  (b) a live write never skips an unapplied tail, reconcile drains it, a raising
      handler pauses on the previous entry, and an interrupted commit is repaired
      on a fresh instance;
  (c) outbox pending/complete/fail/dead-letter/compact, cursor round-trip and
      reopen, and RejectingWritePermit leaves every store untouched.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from companion_memory.errors import (
    CorruptStoreError,
    UnregisteredEventTypeError,
    WriteRejected,
)
from companion_memory.persistence.atomic import FileSystemAtomicStore
from companion_memory.persistence.clock import SystemClock
from companion_memory.persistence.cursors import CursorStore
from companion_memory.persistence.journal import EventJournal
from companion_memory.persistence.outbox import OutboxStore
from companion_memory.persistence.permits import (
    NullWritePermit,
    RejectingWritePermit,
)
from companion_memory.persistence.roots import FileSystemRoots

_EVENT_TYPE = "core.source_recorded"


class B02PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = FileSystemAtomicStore()
        self.roots = FileSystemRoots(self.root / "memory")
        self.clock = SystemClock()
        self.permit = NullWritePermit()
        self.character = "demo"

    # -- helpers ---------------------------------------------------------

    def _journal(self, permit=None) -> EventJournal:
        return EventJournal(
            roots=self.roots, store=self.store, clock=self.clock,
            permit=permit or self.permit,
        )

    def _outbox(self, permit=None) -> OutboxStore:
        return OutboxStore(
            roots=self.roots, store=self.store, clock=self.clock,
            permit=permit or self.permit,
        )

    def _cursors(self, permit=None) -> CursorStore:
        return CursorStore(
            roots=self.roots, store=self.store, clock=self.clock,
            permit=permit or self.permit,
        )

    def _character_path(
        self, character: str, name: str, *, create: bool = False,
    ) -> Path:
        return self.roots.character_dir(character, create=create) / name

    # -- (a) append / reopen / corruption --------------------------------

    def test_append_reopen_and_corruption_policy(self) -> None:
        journal = self._journal()
        journal.register_handler(_EVENT_TYPE, lambda character, payload: True)
        first = journal.append(self.character, _EVENT_TYPE, {"source_key": "s0"})
        second = journal.append(self.character, _EVENT_TYPE, {"source_key": "s1"})
        self.assertEqual((first.position, second.position), (0, 1))

        reopened = self._journal()
        entries = reopened.read_since(self.character, -1)
        self.assertEqual([entry.position for entry in entries], [0, 1])
        self.assertEqual(
            [entry.event_id for entry in entries], [first.event_id, second.event_id],
        )
        self.assertEqual(
            [dict(entry.payload) for entry in entries],
            [{"source_key": "s0"}, {"source_key": "s1"}],
        )
        self.assertEqual(reopened.read_since(self.character, 0), (entries[1],))
        self.assertEqual(reopened.count(self.character), 2)
        self.assertEqual(reopened.read_sentinel(self.character).position, -1)

        # Unknown vocabulary and known-but-handler-less types are both refused.
        with self.assertRaises(UnregisteredEventTypeError):
            reopened.append(self.character, "core.not_a_thing", {})
        with self.assertRaises(UnregisteredEventTypeError):
            reopened.append(self.character, _EVENT_TYPE, {"source_key": "s2"})

        # A corrupt mid-file line is reported and the file is never rewritten.
        events_path = self._character_path(self.character, "events.ndjson")
        damaged = events_path.read_bytes() + (
            b'{"event_id": "broken",\n'
            b'{"event_id": "late", "type": "core.source_recorded", "payload": {}}\n'
        )
        events_path.write_bytes(damaged)
        with self.assertRaises(CorruptStoreError) as caught:
            reopened.read_since(self.character, -1)
        self.assertEqual(caught.exception.path, str(events_path))
        self.assertEqual(events_path.read_bytes(), damaged)

        # A torn final fragment is self-healed on the next append: the
        # unacknowledged partial bytes are dropped, the complete records keep
        # their ids/positions, and the new entry is readable after reopening.
        torn_character = "torn"
        torn_path = self._character_path(
            torn_character, "events.ndjson", create=True,
        )
        torn_path.write_text(
            json.dumps({
                "event_id": "torn-0", "type": _EVENT_TYPE, "position": 0,
                "payload": {"source_key": "t0"},
            }) + "\n" + '{"event_id": "frag", "type": "core.sour',
            encoding="utf-8",
        )
        healing = self._journal()
        healing.register_handler(_EVENT_TYPE, lambda character, payload: True)
        appended = healing.append(
            torn_character, _EVENT_TYPE, {"source_key": "t1"},
        )
        self.assertEqual(appended.position, 1)
        healed_bytes = torn_path.read_bytes()
        self.assertTrue(healed_bytes.endswith(b"\n"))
        self.assertNotIn(b'"frag"', healed_bytes)
        healed_entries = self._journal().read_since(torn_character, -1)
        self.assertEqual(
            [entry.event_id for entry in healed_entries],
            ["torn-0", appended.event_id],
        )
        self.assertEqual(
            [entry.position for entry in healed_entries], [0, 1],
        )

    # -- (b) no silent skip / interrupted commit repair ------------------

    def test_no_silent_skip_and_interrupted_commit_repair(self) -> None:
        journal = self._journal()
        applied: list[str] = []

        def handler(character, payload):
            applied.append(payload["source_key"])
            return True

        journal.register_handler(_EVENT_TYPE, handler)
        journal.append(self.character, _EVENT_TYPE, {"source_key": "s0"})
        journal.append(self.character, _EVENT_TYPE, {"source_key": "s1"})

        view: dict[str, list[str]] = {"entries": []}
        third = journal.record_and_save(
            self.character,
            _EVENT_TYPE,
            {"source_key": "s2"},
            load_view=lambda character: view,
            mutate_view=lambda loaded: loaded["entries"].append("s2"),
            save_view=lambda character, loaded: None,
        )
        self.assertEqual(third.position, 2)
        # The unapplied tail must not be skipped by the live write.
        self.assertEqual(journal.read_sentinel(self.character).position, -1)

        recovered = journal.reconcile(self.character)
        self.assertEqual(
            (recovered.applied, recovered.position, recovered.paused),
            (3, 2, False),
        )
        self.assertEqual(applied, ["s0", "s1", "s2"])
        self.assertEqual(journal.read_sentinel(self.character).position, 2)

        # A raising handler pauses the round, leaving the marker on the
        # previous entry so the same entry is retried next time.
        def failing(character, payload):
            raise RuntimeError("boom")

        journal.register_handler(_EVENT_TYPE, failing)
        journal.append(self.character, _EVENT_TYPE, {"source_key": "s3"})
        paused = journal.reconcile(self.character)
        self.assertTrue(paused.paused)
        self.assertTrue((paused.reason or "").startswith("handler_failed:"))
        self.assertEqual(paused.position, 2)
        self.assertEqual(journal.read_sentinel(self.character).position, 2)

        journal.register_handler(_EVENT_TYPE, handler)
        resumed = journal.reconcile(self.character)
        self.assertEqual(
            (resumed.applied, resumed.position, resumed.paused), (1, 3, False),
        )
        self.assertEqual(journal.read_sentinel(self.character).position, 3)

        # Interrupted commit: the journal entry is durable, the view save never
        # ran. A fresh instance (as after a restart) repairs the view.
        journal.append(self.character, _EVENT_TYPE, {"source_key": "s4"})
        view["entries"].append("s4")  # dirty in-memory view, never persisted
        reopened = self._journal()
        repaired: list[str] = []

        def repair_handler(character, payload):
            repaired.append(payload["source_key"])
            return True

        reopened.register_handler(_EVENT_TYPE, repair_handler)
        recovery = reopened.reconcile(self.character)
        self.assertEqual(
            (recovery.applied, recovery.position, recovery.paused), (1, 4, False),
        )
        self.assertEqual(repaired, ["s4"])
        self.assertEqual(reopened.read_sentinel(self.character).position, 4)

    # -- (c) outbox / cursors / permit -----------------------------------

    def test_outbox_cursors_and_write_permit(self) -> None:
        outbox = self._outbox()
        first = outbox.enqueue(self.character, "extract_facts", {"turn": 1})
        second = outbox.enqueue(
            self.character, "persist_prompt_locale", {"locale": "zh"},
        )
        pending = outbox.pending(self.character)
        self.assertEqual([op.op_id for op in pending], [first, second])
        self.assertEqual(
            (pending[0].op_type, dict(pending[0].payload), pending[0].attempts),
            ("extract_facts", {"turn": 1}, 0),
        )

        # Host-compatible record fields survive on disk.
        outbox_path = self._character_path(self.character, "outbox.ndjson")
        first_record = json.loads(
            outbox_path.read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertTrue(
            {"op_id", "type", "payload", "status", "ts"} <= set(first_record)
        )

        outbox.complete(self.character, second)
        self.assertEqual([op.op_id for op in outbox.pending(self.character)], [first])

        counts = [
            outbox.fail(self.character, first, error=f"boom-{index}")
            for index in range(1, 6)
        ]
        self.assertEqual(counts, [1, 2, 3, 4, 5])
        self.assertEqual(outbox.pending(self.character), ())
        self.assertEqual(self._outbox().pending(self.character), ())

        # Compaction only above 1000 lines; keeps pending ops + attempt tally.
        compact_character = "compact"
        timestamp = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc).isoformat()
        lines: list[str] = []
        for index in range(501):
            lines.append(json.dumps({
                "op_id": f"op-{index}", "type": "extract_facts",
                "payload": {"i": index}, "status": "pending", "ts": timestamp,
            }))
            if index < 500:
                lines.append(json.dumps({
                    "op_id": f"op-{index}", "status": "done", "ts": timestamp,
                }))
        for attempt in (1, 2, 3):
            lines.append(json.dumps({
                "op_id": "op-500", "status": "attempt",
                "attempts": attempt, "ts": timestamp,
            }))
        self.assertEqual(len(lines), 1004)
        self._character_path(
            compact_character, "outbox.ndjson", create=True,
        ).write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(outbox.compact(self.character), 0)
        self.assertEqual(outbox.compact(compact_character), 1002)
        kept = outbox.pending(compact_character)
        self.assertEqual([(op.op_id, op.attempts) for op in kept], [("op-500", 3)])
        self.assertEqual(self._outbox().pending(compact_character)[0].attempts, 3)

        cursors = self._cursors()
        self.assertIsNone(cursors.get(self.character, "rebuttal_checked_until"))
        earlier = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        later = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        cursors.set(self.character, "rebuttal_checked_until", earlier)
        cursors.set(self.character, "another", later)
        self.assertEqual(
            cursors.get(self.character, "rebuttal_checked_until"), earlier,
        )
        self.assertEqual(
            cursors.snapshot(self.character),
            {"rebuttal_checked_until": earlier, "another": later},
        )
        self.assertEqual(self._cursors().get(self.character, "another"), later)
        raw = json.loads(
            self._character_path(self.character, "cursors.json").read_text(
                encoding="utf-8",
            )
        )
        self.assertNotIn("schema_version", raw)

        # Corrupt cursor files are reported, never replaced by an empty map.
        corrupt_character = "corrupt"
        corrupt_path = self._character_path(
            corrupt_character, "cursors.json", create=True,
        )
        corrupt_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(CorruptStoreError):
            self._cursors().get(corrupt_character, "any")
        corrupt_path.write_text("[]", encoding="utf-8")
        with self.assertRaises(CorruptStoreError):
            self._cursors().get(corrupt_character, "any")

        # RejectingWritePermit: every store refuses and changes nothing.
        seeded = self._journal()
        seeded.register_handler(_EVENT_TYPE, lambda character, payload: True)
        seeded.append(self.character, _EVENT_TYPE, {"source_key": "seed"})
        rejecting = RejectingWritePermit()
        rejecting_journal = self._journal(permit=rejecting)
        rejecting_journal.register_handler(
            _EVENT_TYPE, lambda character, payload: True,
        )
        events_path = self._character_path(self.character, "events.ndjson")
        events_before = events_path.read_bytes()
        with self.assertRaises(WriteRejected):
            rejecting_journal.append(
                self.character, _EVENT_TYPE, {"source_key": "denied"},
            )
        self.assertEqual(events_path.read_bytes(), events_before)

        rejecting_outbox = self._outbox(permit=rejecting)
        outbox_before = outbox_path.read_bytes()
        with self.assertRaises(WriteRejected):
            rejecting_outbox.enqueue(self.character, "extract_facts", {"turn": 2})
        self.assertEqual(outbox_path.read_bytes(), outbox_before)

        rejecting_cursors = self._cursors(permit=rejecting)
        cursors_path = self._character_path(self.character, "cursors.json")
        cursors_before = cursors_path.read_bytes()
        with self.assertRaises(WriteRejected):
            rejecting_cursors.set(self.character, "another", earlier)
        self.assertEqual(cursors_path.read_bytes(), cursors_before)

        # A rejected write on a fresh character creates no store files.
        fresh = "fresh"
        with self.assertRaises(WriteRejected):
            rejecting_journal.append(fresh, _EVENT_TYPE, {})
        with self.assertRaises(WriteRejected):
            rejecting_outbox.enqueue(fresh, "extract_facts", {})
        with self.assertRaises(WriteRejected):
            rejecting_cursors.set(fresh, "x", earlier)
        for name in ("events.ndjson", "outbox.ndjson", "cursors.json"):
            self.assertFalse(
                self._character_path(fresh, name, create=False).exists()
            )


if __name__ == "__main__":
    unittest.main()
