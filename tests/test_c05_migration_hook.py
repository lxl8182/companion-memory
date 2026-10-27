# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""C05: migration_hooks wiring through build_memory_core -> maintenance."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core


class MigrationHookTests(unittest.TestCase):
    def _steps(self, core):
        result = asyncio.run(core.maintenance.run_once("demo"))
        steps = getattr(result, "steps", {})
        self.assertIsInstance(steps, dict)
        return steps

    def test_no_hooks_means_migrations_skipped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cm-c05-") as tmp:
            core = build_memory_core(memory_root=Path(tmp) / "memory")
            steps = self._steps(core)
            self.assertEqual(steps["migrations"]["status"], "skipped")

    def test_hook_runs_inside_maintenance(self) -> None:
        calls: list[str] = []

        def hook(character=None):
            calls.append(str(character))
            return {"status": "ok"}

        with tempfile.TemporaryDirectory(prefix="cm-c05-") as tmp:
            core = build_memory_core(
                memory_root=Path(tmp) / "memory",
                migration_hooks=(hook,),
            )
            steps = self._steps(core)
            self.assertEqual(steps["migrations"]["status"], "ok")
            self.assertEqual(calls, ["demo"])


if __name__ == "__main__":
    unittest.main()
