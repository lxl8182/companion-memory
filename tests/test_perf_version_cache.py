"""Version caching and the roots fast path must not weaken their guarantees.

The deletion barrier asks `DeletionStore.version()`/`CorrectionStore.version()`
on every predicate, so those reads are served from a stat-validated cache. These
tests pin the contracts the cache could otherwise break:

* a damaged or unknown-schema store still raises (`CorruptStoreError` /
  `InvalidSchemaError`) instead of reporting a cached version, so the barrier
  never reopens on a file it cannot read;
* a version written elsewhere (another process, a host writer) is still
  observed, because `os.replace` changes the file signature.

The last test keeps the roots containment check in place now that
`character_dir` caches resolved directories per character name.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from companion_memory.adapters.composition import build_memory_core
from companion_memory.errors import CorruptStoreError, InvalidSchemaError
from companion_memory.persistence import roots as roots_module
from companion_memory.persistence.roots import FileSystemRoots


class VersionCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "memory"
        self.core = build_memory_core(memory_root=self.root)
        self.deletion_path = self.root / "demo" / "memory_deletions.json"
        self.correction_path = self.root / "demo" / "memory_corrections.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cached_version_never_hides_a_damaged_store(self):
        self.assertEqual(self.core.deletions.version("demo"), 0)  # cache is warm
        self.deletion_path.parent.mkdir(parents=True, exist_ok=True)

        self.deletion_path.write_text("{not json", encoding="utf-8")
        damaged = self.deletion_path.read_bytes()
        with self.assertRaises(CorruptStoreError):
            self.core.deletions.version("demo")
        with self.assertRaises(CorruptStoreError):
            self.core.barrier.state("demo")
        self.assertEqual(self.deletion_path.read_bytes(), damaged)

        self.deletion_path.write_text(
            json.dumps({"schema": 99, "version": 1, "deletions": [], "tombstones": []}),
            encoding="utf-8",
        )
        with self.assertRaises(InvalidSchemaError):
            self.core.deletions.version("demo")

        # A dirty row the envelope alone would miss still fails the strict parse.
        self.deletion_path.write_text(json.dumps({
            "schema": 1, "version": 5, "deletions": [],
            "tombstones": [{"type": "text", "text_hash": "short"}],
        }), encoding="utf-8")
        with self.assertRaises(CorruptStoreError):
            self.core.deletions.version("demo")

    def test_version_written_elsewhere_is_observed(self):
        self.assertEqual(self.core.corrections.version("demo"), 0)

        self.correction_path.parent.mkdir(parents=True, exist_ok=True)
        self.correction_path.write_text(
            json.dumps({"schema": 1, "version": 7, "corrections": []}),
            encoding="utf-8",
        )
        self.assertEqual(self.core.corrections.version("demo"), 7)
        self.assertEqual(self.core.barrier.state("demo").correction_version, 7)

        # A second writer bumping the version is seen on the next predicate.
        self.correction_path.write_text(
            json.dumps({"schema": 1, "version": 9, "corrections": []}),
            encoding="utf-8",
        )
        self.assertEqual(self.core.corrections.version("demo"), 9)

    def test_own_write_refreshes_the_cached_version(self):
        before = self.core.deletions.version("demo")
        self.core.deletions.save_view("demo", {
            "schema": 1, "version": before + 1, "deletions": [], "tombstones": [],
        })
        self.assertEqual(self.core.deletions.version("demo"), before + 1)


class RootsFastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "memory"
        self.roots = FileSystemRoots(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_containment_check_still_runs_for_a_new_name(self):
        with mock.patch.object(roots_module, "_is_within", return_value=False) as guard:
            with self.assertRaises(ValueError):
                self.roots.character_dir("demo", create=False)
        self.assertTrue(guard.called)

        # The directory is cached only after the check passed, so a later call
        # returns the same path without another resolve.
        first = self.roots.character_dir("demo", create=False)
        with mock.patch.object(roots_module, "_is_within", return_value=False):
            self.assertEqual(self.roots.character_dir("demo", create=False), first)
        self.assertEqual(first.parent, self.roots.memory_root)

        # The name validator is still the first gate for every call.
        for bad in ("../escape", "a/b", "a\\b", "..", ".", "CON", "name "):
            with self.assertRaises(ValueError):
                self.roots.character_dir(bad, create=False)


if __name__ == "__main__":
    unittest.main()
