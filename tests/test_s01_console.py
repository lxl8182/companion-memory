"""S01 acceptance for the writable console: it is an owner, not a second writer.

The console used to assemble its own `MemoryCore` with `NullWritePermit` and no
ownership check at all, which is exactly the "WebUI builds a second core" gap
in docs/standalone-service.md §2.1. A writable live console must now take the
same root lock as the service, the host and the CLI, and must hand it back when
it closes so the root is immediately claimable again.
"""

import sys
import tempfile
import unittest
from pathlib import Path

from rin_memory.errors import RootLockConflict
from rin_memory.persistence.ownership import acquire_root_lock

WEBUI_DIR = Path(__file__).resolve().parents[1] / "webui"
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import seed  # noqa: E402  (webui/seed.py, script-style import)


class WritableConsoleOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "live"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writable_console_holds_the_root_lock(self):
        core = seed.build_live_core(self.root, writable=True, character="demo")
        try:
            self.assertIn(id(core), seed._LIVE_LOCKS)
            with self.assertRaises(RootLockConflict):
                acquire_root_lock(self.root, mode="standalone")
        finally:
            seed.release_live_lock(core)
        lock = acquire_root_lock(self.root, mode="standalone")
        lock.release()

    def test_read_only_console_takes_no_lock(self):
        core = seed.build_live_core(self.root, writable=False, character="demo")
        try:
            self.assertNotIn(id(core), seed._LIVE_LOCKS)
            lock = acquire_root_lock(self.root, mode="standalone")
            lock.release()
        finally:
            seed.release_live_lock(core)

    def test_release_is_idempotent(self):
        core = seed.build_live_core(self.root, writable=True, character="demo")
        seed.release_live_lock(core)
        seed.release_live_lock(core)
        lock = acquire_root_lock(self.root, mode="cli")
        lock.release()


if __name__ == "__main__":
    unittest.main()
