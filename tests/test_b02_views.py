"""B02 persistence views: facts.json/facts_archive.json and the time index."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.errors import (
    CorruptStoreError, InvalidSchemaError, WriteRejected,
)
from rin_memory.persistence import (
    FileSystemAtomicStore, FileSystemRoots, NullWritePermit, RejectingWritePermit,
    SystemClock,
)
from rin_memory.persistence.facts_view import FactsView
from rin_memory.persistence.timeindex import TimeIndexStore


class _RecordingStore:
    """AtomicJsonStore wrapper that records the order of save() calls."""

    def __init__(self, inner: FileSystemAtomicStore) -> None:
        self._inner = inner
        self.saved: list[str] = []

    def load(self, path: Path) -> object:
        return self._inner.load(path)

    def save(self, path: Path, value: object) -> None:
        self.saved.append(Path(path).name)
        self._inner.save(path, value)

    def save_text(self, path: Path, text: str) -> None:
        self._inner.save_text(path, text)

    def append_line(self, path: Path, line: str) -> None:
        self._inner.append_line(path, line)


class FactsViewTests(unittest.TestCase):
    def test_facts_view_round_trip_archive_first_commit_and_schema_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            roots = FileSystemRoots(tmp)
            store = FileSystemAtomicStore()
            view = FactsView(
                roots=roots, store=store, clock=SystemClock(),
                permit=NullWritePermit(),
            )
            facts_path = Path(tmp) / "neko" / "facts.json"
            archive_path = Path(tmp) / "neko" / "facts_archive.json"

            # -- verbatim round-trip, including unknown fields and Chinese text
            facts = [
                {
                    "id": "fact_1", "text": "用户最近养了一只猫",
                    "subject": "private", "importance": 7,
                    "unknown_field": {"nested": [1, 2, 3], "flag": True},
                },
                {"id": "fact_2", "text": "猫猫喜欢晒太阳", "extra": "keep me"},
            ]
            view.save("neko", facts)
            self.assertEqual(view.load("neko"), facts)
            self.assertEqual(view.count("neko"), 2)
            # On-disk shape stays the host's bare array: no schema envelope.
            raw = json.loads(facts_path.read_text(encoding="utf-8"))
            self.assertIsInstance(raw, list)
            self.assertEqual(raw, facts)

            # -- a bare host-shaped array on disk loads
            bare = [{"id": "host_row", "text": "宿主写入的裸数组", "x": {"y": 1}}]
            store.save(facts_path, bare)
            self.assertEqual(view.load("neko"), bare)

            # -- a versioned envelope loads on both files
            envelope = {"schema_version": 1, "facts": [{"id": "envelope_row"}]}
            store.save(facts_path, envelope)
            self.assertEqual(view.load("neko"), envelope["facts"])
            archive_envelope = {
                "schema_version": 1, "archive": [{"id": "archived_row"}],
            }
            store.save(archive_path, archive_envelope)
            self.assertEqual(view.load_archive("neko"), archive_envelope["archive"])

            # -- commit() writes the archive file BEFORE the active file
            recording = _RecordingStore(store)
            recording_view = FactsView(
                roots=roots, store=recording, clock=SystemClock(),
                permit=NullWritePermit(),
            )
            active = [{"id": "fact_active", "text": "活跃事实"}]
            archive = [{"id": "fact_archived", "text": "归档事实"}]
            recording_view.commit("neko", active=active, archive=archive)
            self.assertEqual(recording.saved, ["facts_archive.json", "facts.json"])
            self.assertEqual(recording_view.load_archive("neko"), archive)
            self.assertEqual(recording_view.load("neko"), active)

            # -- corrupt content is refused and left byte-for-byte untouched
            for corrupt in (
                "{not json",
                json.dumps([1, 2]),
                json.dumps("a bare string is not a list"),
            ):
                facts_path.write_text(corrupt, encoding="utf-8")
                before = facts_path.read_bytes()
                with self.assertRaises(CorruptStoreError):
                    view.load("neko")
                self.assertEqual(facts_path.read_bytes(), before)

            # -- unknown schema version is refused before any write
            unsupported = json.dumps({"schema_version": 99, "facts": []})
            facts_path.write_text(unsupported, encoding="utf-8")
            archive_path.write_text(unsupported, encoding="utf-8")
            before = facts_path.read_bytes()
            before_archive = archive_path.read_bytes()
            with self.assertRaises(InvalidSchemaError):
                view.load("neko")
            with self.assertRaises(InvalidSchemaError):
                view.load_archive("neko")
            self.assertEqual(facts_path.read_bytes(), before)
            self.assertEqual(archive_path.read_bytes(), before_archive)

            # -- a rejecting permit leaves a fresh character without any file
            rejecting = FactsView(
                roots=roots, store=store, clock=SystemClock(),
                permit=RejectingWritePermit(),
            )
            with self.assertRaises(WriteRejected):
                rejecting.save("rejected", [{"id": "fact_x"}])
            with self.assertRaises(WriteRejected):
                rejecting.commit(
                    "rejected", active=[{"id": "a"}], archive=[{"id": "b"}],
                )
            self.assertFalse((Path(tmp) / "rejected").exists())


class TimeIndexStoreTests(unittest.TestCase):
    def test_timeindex_schema_gate_and_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            roots = FileSystemRoots(tmp)
            permit = NullWritePermit()
            db_path = Path(tmp) / "neko" / "time_indexed.db"

            # -- fresh database: host tables plus schema_meta version 1
            store = TimeIndexStore(
                roots=roots, character="neko", permit=permit,
                clock=SystemClock(),
            )
            with store:
                self.assertTrue(db_path.exists())
                connection = sqlite3.connect(str(db_path))
                try:
                    tables = {
                        row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    version = connection.execute(
                        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                    ).fetchone()
                finally:
                    connection.close()
                self.assertLessEqual(
                    {
                        "time_indexed_original", "time_indexed_compressed",
                        "facts_fts_v2", "facts_fts_meta", "schema_meta",
                    },
                    tables,
                )
                self.assertEqual(version, ("1",))

                row_id = store.insert_original({
                    "session_id": "session-1",
                    "message": "the user likes green tea",
                    "timestamp": datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc),
                })
                self.assertIsInstance(row_id, int)
                self.assertEqual(store.count_original(), 1)
                record = store.get_original(row_id)
                self.assertEqual(record["session_id"], "session-1")
                self.assertEqual(record["message"], "the user likes green tea")
                self.assertIn("2026-10-22", record["timestamp"])

                hits = store.search("tea")
                self.assertTrue(hits)
                self.assertEqual(hits[0]["fact_id"], str(row_id))
                self.assertIn("tea", hits[0]["content"])
                self.assertEqual(store.search("   "), [])
                with self.assertRaises(ValueError):
                    store.search('"unterminated')

            # -- data survives close/reopen; the compressed table round-trips
            store.close()  # idempotent
            with TimeIndexStore(
                roots=roots, character="neko", permit=permit,
            ) as reopened:
                self.assertEqual(reopened.count_original(), 1)
                self.assertEqual(
                    reopened.get_original(row_id)["message"],
                    "the user likes green tea",
                )
                reopened.insert_compressed({
                    "session_id": "session-1", "message": "summary of the turn",
                })
                self.assertEqual(reopened.count_compressed(), 1)

            # -- a host database without schema_meta migrates additively
            legacy_dir = Path(tmp) / "legacy"
            legacy_dir.mkdir(parents=True)
            legacy_db = legacy_dir / "time_indexed.db"
            connection = sqlite3.connect(str(legacy_db))
            try:
                connection.execute(
                    "CREATE TABLE time_indexed_original (id INTEGER NOT NULL, "
                    "session_id VARCHAR, message TEXT, PRIMARY KEY (id))"
                )
                connection.execute(
                    "CREATE TABLE time_indexed_compressed (id INTEGER NOT NULL, "
                    "session_id VARCHAR, message TEXT, PRIMARY KEY (id))"
                )
                connection.execute(
                    "INSERT INTO time_indexed_original (session_id, message) "
                    "VALUES ('host', 'host row kept')"
                )
                connection.commit()
            finally:
                connection.close()

            with TimeIndexStore(
                roots=roots, character="legacy", permit=permit,
            ) as legacy:
                self.assertEqual(legacy.count_original(), 1)
                self.assertEqual(legacy.get_original(1)["message"], "host row kept")

            connection = sqlite3.connect(str(legacy_db))
            try:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(time_indexed_original)"
                    )
                }
                indexes = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                connection.close()
            self.assertEqual(version, ("1",))
            self.assertIn("timestamp", columns)
            self.assertIn("idx_time_indexed_original_timestamp", indexes)
            self.assertIn("facts_fts_v2", tables)
            self.assertIn("facts_fts_meta", tables)

            # -- an unsupported schema version is refused before any write
            future_dir = Path(tmp) / "future"
            future_dir.mkdir(parents=True)
            future_db = future_dir / "time_indexed.db"
            connection = sqlite3.connect(str(future_db))
            try:
                connection.execute(
                    "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)"
                )
                connection.execute(
                    "INSERT INTO schema_meta (key, value) "
                    "VALUES ('schema_version', '99')"
                )
                connection.execute(
                    "CREATE TABLE time_indexed_original (id INTEGER NOT NULL, "
                    "session_id VARCHAR, message TEXT, PRIMARY KEY (id))"
                )
                connection.execute(
                    "INSERT INTO time_indexed_original (session_id, message) "
                    "VALUES ('future', 'must stay')"
                )
                connection.commit()
            finally:
                connection.close()
            before = future_db.read_bytes()
            with self.assertRaises(InvalidSchemaError):
                TimeIndexStore(
                    roots=roots, character="future", permit=permit,
                ).open()
            self.assertEqual(future_db.read_bytes(), before)

            # -- a non-SQLite file is corruption and is left untouched
            garbage_dir = Path(tmp) / "garbage"
            garbage_dir.mkdir(parents=True)
            garbage_db = garbage_dir / "time_indexed.db"
            garbage = b"this is not a sqlite database"
            garbage_db.write_bytes(garbage)
            with self.assertRaises(CorruptStoreError):
                TimeIndexStore(
                    roots=roots, character="garbage", permit=permit,
                ).open()
            self.assertEqual(garbage_db.read_bytes(), garbage)


if __name__ == "__main__":
    unittest.main()
