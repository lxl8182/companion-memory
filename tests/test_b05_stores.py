"""B05 storage acceptance: host-shaped reflection/persona stores.

The batch criteria require the migrated files to stay host-readable, to fail
closed on corrupt content (never overwrite it), and to keep the archive shards
and growth ledger bounded. These tests exercise the stores directly; the
domain flow is covered in test_b05_reflection.py / test_b05_persona.py.
"""

import json
import tempfile
import unittest
from pathlib import Path

from rin_memory.errors import CorruptStoreError, ContentBlockedError
from rin_memory.persistence.atomic import FileSystemAtomicStore
from rin_memory.persistence.clock import SystemClock
from rin_memory.persistence.permits import NullWritePermit
from rin_memory.persistence.persona import PersonaStore
from rin_memory.persistence.reflections import ReflectionStore
from rin_memory.persistence.roots import FileSystemRoots
from rin_memory.persistence.shards import ArchiveShardStore


class StoreHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.roots = FileSystemRoots(self.root / "memory", self.root / "local")
        self.store = FileSystemAtomicStore()
        self.clock = SystemClock()
        self.permit = NullWritePermit()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def character_file(self, name: str) -> Path:
        return self.roots.character_dir("demo", create=False) / name


class ReflectionStoreTests(StoreHarness):
    def build(self, **kwargs) -> ReflectionStore:
        return ReflectionStore(
            roots=self.roots, store=self.store, clock=self.clock,
            permit=self.permit, **kwargs,
        )

    def test_round_trip_blank_and_fail_closed(self):
        store = self.build()
        self.assertEqual(store.load("demo"), [])
        self.assertEqual(store.load_surfaced("demo"), [])
        self.assertEqual(store.load_backoff("demo"), {})

        rows = [
            {"id": "ref_a", "text": "主人喜欢被直接问", "status": "pending",
             "source_fact_ids": ["fact_1"], "custom_field": {"kept": True}},
            {"id": "ref_b", "text": "另一条", "status": "confirmed"},
        ]
        store.save("demo", rows)
        loaded = store.load("demo")
        self.assertEqual([row["id"] for row in loaded], ["ref_a", "ref_b"])
        self.assertEqual(loaded[0]["custom_field"], {"kept": True})

        # Blank file reads as empty (host treats an empty file as no rows).
        self.character_file("reflections.json").write_text("  \n", encoding="utf-8")
        self.assertEqual(store.load("demo"), [])

        # Corrupt content is reported and never overwritten.
        corrupt = '{"not": "an array"'
        self.character_file("reflections.json").write_text(corrupt, encoding="utf-8")
        with self.assertRaises(CorruptStoreError):
            store.load("demo")
        self.assertEqual(
            self.character_file("reflections.json").read_text(encoding="utf-8"),
            corrupt,
        )

    def test_surfaced_dedupe_and_backoff_compat(self):
        store = self.build()
        store.save_surfaced("demo", [
            {"reflection_id": "ref_a", "text": "old", "surfaced_at": "t1", "feedback": None},
            {"reflection_id": "ref_b", "text": "b", "surfaced_at": "t2", "feedback": None},
            {"reflection_id": "ref_a", "text": "new", "surfaced_at": "t3", "feedback": "confirmed"},
        ])
        surfaced = store.load_surfaced("demo")
        self.assertEqual([row["reflection_id"] for row in surfaced], ["ref_a", "ref_b"])
        self.assertEqual(surfaced[0]["text"], "new")
        self.assertEqual(surfaced[0]["feedback"], "confirmed")

        # Legacy int-only backoff entries are accepted; saving keeps the object shape.
        self.character_file("synth_backoff.json").write_text(
            json.dumps({"ref_old": 3}), encoding="utf-8",
        )
        backoff = store.load_backoff("demo")
        self.assertEqual(backoff["ref_old"]["n"], 3)
        store.save_backoff("demo", {
            "ref_%02d" % index: {"n": index, "at": None} for index in range(70)
        })
        saved = json.loads(self.character_file("synth_backoff.json").read_text(encoding="utf-8"))
        self.assertLessEqual(len(saved), 64)
        self.assertIn("ref_69", saved)

    def test_archive_shards_append_read_and_self_heal(self):
        store = self.build()
        shard = store.append_archive("demo", [
            {"id": "ref_a", "text": "a"}, {"id": "ref_b", "text": "b"},
        ])
        self.assertTrue(shard.endswith(".json"))
        self.assertEqual({row["id"] for row in store.load_archive("demo")}, {"ref_a", "ref_b"})

        # Missing entry in a named shard is restored by ensure_entry (idempotent).
        self.assertTrue(store._archive.ensure_entry("demo", {"id": "ref_c", "text": "c"}, shard))
        self.assertFalse(store._archive.ensure_entry("demo", {"id": "ref_c", "text": "c"}, shard))
        self.assertEqual(
            {row["id"] for row in store.load_archive("demo")},
            {"ref_a", "ref_b", "ref_c"},
        )

        # A corrupt shard fails closed instead of silently dropping archived rows.
        shard_path = self.roots.character_dir("demo", create=False) / "reflection_archive" / shard
        shard_path.write_text("[{", encoding="utf-8")
        with self.assertRaises(CorruptStoreError):
            store.load_archive("demo")
        self.assertEqual(shard_path.read_text(encoding="utf-8"), "[{")


class PersonaStoreTests(StoreHarness):
    def build(self, **kwargs) -> PersonaStore:
        return PersonaStore(
            roots=self.roots, store=self.store, clock=self.clock,
            permit=self.permit, **kwargs,
        )

    def test_persona_shape_and_fail_closed(self):
        store = self.build()
        self.assertEqual(store.load("demo"), {})
        doc = {
            "relationship": {"facts": [
                {"id": "prom_ref_a", "text": "主人喜欢被直接问",
                 "source": "reflection", "source_id": "ref_a",
                 "merged_from_ids": [], "unknown_key": 1},
            ]},
            "master": {"facts": []},
        }
        store.save("demo", doc)
        loaded = store.load("demo")
        self.assertEqual(set(loaded), {"relationship", "master"})
        self.assertEqual(loaded["relationship"]["facts"][0]["unknown_key"], 1)

        self.character_file("persona.json").write_text('{"relationship": {"facts": {}}}', encoding="utf-8")
        with self.assertRaises(CorruptStoreError):
            store.load("demo")
        self.assertEqual(
            self.character_file("persona.json").read_text(encoding="utf-8"),
            '{"relationship": {"facts": {}}}',
        )

    def test_growth_uses_defaults_and_cap(self):
        store = ReflectionStore(
            roots=self.roots, store=self.store, clock=self.clock,
            permit=self.permit,
        )
        self.assertEqual(
            store.load_growth_uses("demo"),
            {"schema": 1, "version": 0, "uses": []},
        )
        uses = [
            {"item_id": "grow_%02d" % index, "version": index, "usage": "read"}
            for index in range(2005)
        ]
        store.save_growth_uses("demo", {"schema": 1, "version": 7, "uses": uses})
        saved = store.load_growth_uses("demo")
        self.assertEqual(saved["version"], 7)
        self.assertLessEqual(len(saved["uses"]), 2000)
        self.assertEqual(saved["uses"][-1]["item_id"], "grow_2004")


class WriteGuardTests(StoreHarness):
    def test_guard_blocks_reflection_and_persona_writes(self):
        def guard(character, rows):
            if any(row.get("id") == "ref_deleted" for row in rows):
                raise ContentBlockedError("row is covered by a tombstone")

        reflections = ReflectionStore(
            roots=self.roots, store=self.store, clock=self.clock,
            permit=self.permit, write_guard=guard,
        )
        reflections.save("demo", [{"id": "ref_ok", "text": "ok"}])
        with self.assertRaises(ContentBlockedError):
            reflections.save("demo", [{"id": "ref_deleted", "text": "gone"}])
        self.assertEqual([row["id"] for row in reflections.load("demo")], ["ref_ok"])

        persona = PersonaStore(
            roots=self.roots, store=self.store, clock=self.clock,
            permit=self.permit, write_guard=guard,
        )
        with self.assertRaises(ContentBlockedError):
            persona.save(
                "demo",
                {"relationship": {"facts": [{"id": "ref_deleted", "text": "x"}]}},
            )
        # Rejected write must leave the previous view (absent) untouched.
        self.assertEqual(persona.load("demo"), {})


if __name__ == "__main__":
    unittest.main()
