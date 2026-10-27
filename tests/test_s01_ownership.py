"""S01 acceptance: one writable owner per canonical data root.

docs/standalone-service.md §2.2 asks for these scenarios specifically:

* service + service, service + host, service + writable console/CLI/migration;
* simultaneous contention, a crash followed by a restart, and path aliases.

The second writer must be refused *before any data is modified*. The lock is
an OS file lock, so a process that dies releases it automatically and the
recovery path is "restart immediately", not "delete a stale file". The former
pid-based reclamation is exercised as a regression: an unreadable, empty or
foreign-pid lock file no longer makes a live owner's lock stealable.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from companion_memory.cli import main as cli_main
from companion_memory.errors import RootLockConflict
from companion_memory.persistence.ownership import (
    ROOT_LOCK_FILENAME,
    acquire_root_lock,
    canonical_root,
    read_root_lock,
    root_lock_path,
)


def _run_cli(argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(list(argv))
    return code, out.getvalue(), err.getvalue()


class RootLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "memory"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- exclusion -------------------------------------------------------

    def test_second_owner_in_one_process_is_refused(self):
        first = acquire_root_lock(self.root, mode="standalone")
        try:
            with self.assertRaises(RootLockConflict) as caught:
                acquire_root_lock(self.root, mode="host")
            self.assertEqual(caught.exception.code, "root_lock_conflict")
            self.assertIn("already owns", str(caught.exception))
        finally:
            first.release()
        # Releasing makes the root claimable again.
        again = acquire_root_lock(self.root, mode="console")
        again.release()

    def test_second_owner_in_another_process_is_refused(self):
        """A real second process is the case that matters for a service."""
        script = textwrap.dedent(
            """
            import json, sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            from companion_memory.errors import RootLockConflict
            from companion_memory.persistence.ownership import acquire_root_lock
            try:
                lock = acquire_root_lock(Path(sys.argv[2]), mode="standalone")
            except RootLockConflict as exc:
                print(json.dumps({"blocked": True, "code": exc.code}))
            else:
                lock.release()
                print(json.dumps({"blocked": False}))
            """,
        )
        src = str(Path(__file__).resolve().parents[1] / "src")
        holder = acquire_root_lock(self.root, mode="host")
        try:
            completed = subprocess.run(
                [sys.executable, "-c", script, src, str(self.root)],
                capture_output=True, text=True, timeout=90,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["blocked"], completed.stdout)
            self.assertEqual(payload["code"], "root_lock_conflict")
        finally:
            holder.release()

    def test_released_lock_is_taken_by_another_process(self):
        """Simulates a crash: the OS lock is gone, so a restart may proceed."""
        script = textwrap.dedent(
            """
            import json, sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            from companion_memory.persistence.ownership import acquire_root_lock
            lock = acquire_root_lock(Path(sys.argv[2]), mode="cli")
            print(json.dumps({"acquired": True}))
            lock.release()
            """,
        )
        src = str(Path(__file__).resolve().parents[1] / "src")
        completed = subprocess.run(
            [sys.executable, "-c", script, src, str(self.root)],
            capture_output=True, text=True, timeout=90,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout.strip().splitlines()[-1])["acquired"])

    # -- the fixed reclamation bug ---------------------------------------

    def test_unreadable_or_foreign_lock_file_is_not_stealable(self):
        """The old code deleted a lock it could not read; that race is fixed.

        An owner creates the file, writes its payload and only then is seen by
        a second process. A second process that finds the file empty, garbage,
        or carrying someone else's pid must be refused: the payload is
        diagnostics, never the arbiter.
        """
        lock = acquire_root_lock(self.root, mode="standalone")
        try:
            path = root_lock_path(self.root)
            self.assertTrue(path.exists())
            for payload in (b"", b"{not json", b'{"pid": 999999999}'):
                path.write_bytes(payload)
                with self.assertRaises(RootLockConflict):
                    acquire_root_lock(self.root, mode="host")
                self.assertTrue(path.exists(), "a live owner's lock must survive")
        finally:
            lock.release()

    def test_payload_describes_the_holder_and_is_diagnostics_only(self):
        lock = acquire_root_lock(self.root, mode="migration")
        try:
            payload = read_root_lock(self.root)
            self.assertEqual(payload["pid"], os.getpid())
            self.assertEqual(payload["mode"], "migration")
            self.assertEqual(payload["root"], str(canonical_root(self.root)))
        finally:
            lock.release()

    # -- path identity ---------------------------------------------------

    def test_aliases_resolve_to_one_lock(self):
        """Case, '.'/'..' segments and a trailing separator are one root."""
        variants = [
            Path(str(self.root) + os.sep),
            self.root / "." / "nested" / "..",
            Path(str(self.root).upper()) if os.name == "nt" else self.root,
        ]
        base = canonical_root(self.root)
        for variant in variants:
            self.assertEqual(canonical_root(variant), base, str(variant))
        lock = acquire_root_lock(self.root, mode="standalone")
        try:
            for variant in variants:
                with self.assertRaises(RootLockConflict):
                    acquire_root_lock(variant, mode="host")
        finally:
            lock.release()

    # -- entry points ----------------------------------------------------

    def test_cli_run_and_migrate_share_the_root_lock(self):
        """CLI maintenance is refused while another writer owns the root."""
        holder = acquire_root_lock(self.root, mode="standalone")
        try:
            code, _out, err = _run_cli([
                "run", "--memory-root", str(self.root),
                "--character", "demo", "--once",
            ])
            self.assertEqual(code, 3, err)
            self.assertIn("root_lock_conflict", err)
            self.assertFalse(
                (self.root / "demo").exists(),
                "the refused CLI must not create character data",
            )

            code, _out, err = _run_cli([
                "migrate", "--memory-root", str(self.root), "--dry-run",
            ])
            self.assertEqual(code, 2, err)
            self.assertIn("root_lock_conflict", err)
        finally:
            holder.release()

    def test_cli_run_releases_the_lock_when_it_finishes(self):
        code, _out, _err = _run_cli([
            "run", "--memory-root", str(self.root),
            "--character", "demo", "--once",
        ])
        self.assertEqual(code, 0)
        lock = acquire_root_lock(self.root, mode="standalone")
        lock.release()

    def test_lock_never_lands_inside_a_character_directory(self):
        code, _out, _err = _run_cli([
            "run", "--memory-root", str(self.root),
            "--character", "demo", "--once",
        ])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / ROOT_LOCK_FILENAME).exists())
        self.assertFalse(
            (self.root / "demo" / ROOT_LOCK_FILENAME).exists(),
            "the root lock is root-scoped, not per character",
        )


if __name__ == "__main__":
    unittest.main()
