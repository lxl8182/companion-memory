"""Journal fast paths must stay fail-closed and position-correct.

`append`/`reconcile` no longer re-parse the whole journal when the file
signature and the replay marker prove there is nothing new (a 15 MB / 1.1 k-line
`events.ndjson` cost 180 ms per `recover()` and ~300 ms per append). These tests
pin the three invariants that optimisation could break:

* a damaged line still raises `CorruptStoreError` instead of being skipped, and
  is never rewritten;
* the next position is still line-count / last-position derived, including when
  another writer appended behind this process's back;
* a real unapplied tail is still replayed (the fast path only skips an empty
  tail, never a pending one).
"""

import json
import tempfile
import unittest
from pathlib import Path

from companion_memory.adapters.composition import build_memory_core
from companion_memory.errors import CorruptStoreError
from companion_memory.persistence.journal import EventJournal
from companion_memory.persistence.atomic import FileSystemAtomicStore
from companion_memory.persistence.clock import SystemClock
from companion_memory.persistence.permits import NullWritePermit
from companion_memory.persistence.roots import FileSystemRoots

_EVENT_TYPE = "core.source_recorded"


class JournalFastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "memory"
        self.character = "demo"
        self.core = build_memory_core(memory_root=self.root)
        self.journal_path = self.root / self.character / "events.ndjson"
        self.core.journal.register_handler(
            _EVENT_TYPE, lambda character, payload: True,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _new_journal(self) -> EventJournal:
        journal = EventJournal(
            roots=FileSystemRoots(self.root),
            store=FileSystemAtomicStore(),
            clock=SystemClock(),
            permit=NullWritePermit(),
        )
        journal.register_handler(_EVENT_TYPE, lambda character, payload: True)
        return journal

    def test_damaged_journal_still_raises_and_is_not_rewritten(self):
        journal = self.core.journal
        journal.append(self.character, _EVENT_TYPE, {"source_key": "s0"})
        self.assertEqual(self.core.recover(self.character).applied, 1)  # warm scan
        self.assertEqual(journal.count(self.character), 1)

        damaged = self.journal_path.read_bytes() + b'{"event_id": "broken",\n'
        self.journal_path.write_bytes(damaged)
        with self.assertRaises(CorruptStoreError):
            journal.count(self.character)
        with self.assertRaises(CorruptStoreError):
            journal.reconcile(self.character)
        with self.assertRaises(CorruptStoreError):
            journal.append(self.character, _EVENT_TYPE, {"source_key": "s1"})
        self.assertEqual(self.journal_path.read_bytes(), damaged)

    def test_position_tracks_an_append_from_another_writer(self):
        journal = self.core.journal
        first = journal.append(self.character, _EVENT_TYPE, {"source_key": "s0"})
        second = self._new_journal().append(
            self.character, _EVENT_TYPE, {"source_key": "s1"},
        )
        self.assertEqual((first.position, second.position), (0, 1))

        # The first journal's cached scan is stale; the new position must not
        # reuse 1 and silently duplicate it inside the journal file.
        third = journal.append(self.character, _EVENT_TYPE, {"source_key": "s2"})
        self.assertEqual(third.position, 2)
        positions = [
            entry.position for entry in journal.read_since(self.character, -1)
        ]
        self.assertEqual(positions, [0, 1, 2])

    def test_unapplied_tail_is_still_replayed(self):
        journal = self.core.journal
        journal.append(self.character, _EVENT_TYPE, {"source_key": "s0"})
        self.assertEqual(self.core.recover(self.character).applied, 1)
        self.assertEqual(self.core.recover(self.character).applied, 0)  # fast path

        # An append does not advance the marker, so this entry is a real tail.
        journal.append(self.character, _EVENT_TYPE, {"source_key": "s1"})
        replayed = self.core.recover(self.character)
        self.assertEqual((replayed.applied, replayed.paused), (1, False))
        self.assertEqual(self.core.recover(self.character).applied, 0)


if __name__ == "__main__":
    unittest.main()
