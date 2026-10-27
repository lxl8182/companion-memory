"""S05 first batch: explicit config, same-core initialization, owned processes.

Run with the installed wheel interpreter to also check packaged UI resources.
"""
import asyncio
import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from rin_memory.service.management import ManagementApi
from rin_memory.service.memory_service import MemoryService
from rin_memory.service.server import MemoryApi, build_server
from rin_memory.stack import StackError, start, status, stop
from rin_memory.stack_config import StackConfigError, initialize, load_config


class StackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="companion 新实例 ")
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config" / "stack.toml"
        self.config = initialize(self.config_path)

    def request(self, base, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(base + path, data=data, method=method,
                                        headers={"Content-Type": "application/json", **(headers or {})})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_config_has_stable_paths_and_repeated_init_preserves_data(self):
        text = self.config_path.read_text(encoding="utf-8")
        marker = self.config.roots["memory"] / "keep.txt"
        marker.write_text("保留", encoding="utf-8")
        self.assertEqual(initialize(self.config_path).roots, self.config.roots)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), text)
        self.assertEqual(marker.read_text(encoding="utf-8"), "保留")
        self.assertEqual(self.config.roots["host"], Path(self.tmp.name) / "host")
        self.assertIn("models.service.tiers.summary", self.config.missing())
        for invalid, expected in [
            (text.replace("host = '../host'", "host = '../memory'"), "overlaps"),
            (text.replace("api_key_env = 'COMPANION_MODEL_API_KEY'", "api_key_env = 'invalid key value'"), "api_key_env"),
            (text.replace("port = 48931", "port = 70000"), "service.port"),
        ]:
            self.config_path.write_text(invalid, encoding="utf-8")
            with self.assertRaisesRegex(StackConfigError, expected):
                load_config(self.config_path)
        self.config_path.write_text(text, encoding="utf-8")

    def test_runtime_python_preserves_virtualenv_symlink(self):
        python = Path(self.tmp.name) / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        try:
            python.symlink_to(sys.executable)
        except OSError as exc:
            self.skipTest(f"creating executable symlinks is unavailable: {exc}")
        text = self.config_path.read_text(encoding="utf-8")
        text = text.replace(f"python = '{sys.executable}'", f"python = '{python}'", 1)
        self.config_path.write_text(text, encoding="utf-8")
        config = load_config(self.config_path)
        self.assertEqual(config.python, python)
        self.assertNotEqual(config.python, python.resolve())

    def test_same_core_http_management_initialization_conflict_and_restart(self):
        service = MemoryService.start(self.config.service)
        server = None
        try:
            management = ManagementApi(service, display_name="新伙伴")
            self.assertIs(management.console.core, service.core)
            server = build_server(MemoryApi(service), port=0, management=management)
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, raw = self.request(base, "GET", "/v1/bootstrap")
            bootstrap = json.loads(raw)
            self.assertEqual(code, 200)
            self.assertFalse(bootstrap["initialization"]["initialized"])
            headers = {"X-Companion-Client": "console", "X-Companion-Token": bootstrap["write_token"]}
            body = {"character_id": "companion", "display_name": "小潮",
                    "text": "你是小潮，说话温柔简洁。"}
            self.assertEqual(self.request(base, "POST", "/v1/characters", body)[0], 401)
            self.assertEqual(self.request(base, "POST", "/v1/characters", body,
                                         dict(headers, Origin="http://evil.test"))[0], 403)
            self.assertEqual(self.request(base, "POST", "/v1/characters", dict(body, character_id="other"), headers)[0], 403)
            self.assertEqual(self.request(base, "POST", "/v1/characters", body, headers)[0], 200)
            code, raw = self.request(base, "POST", "/v1/persona/revise", {
                "base_revision": 1, "operations": [{"op": "replace", "section_id": "identity", "text": "你是小潮，喜欢海风。"}],
            }, headers)
            self.assertEqual(code, 200, raw)
            conflict = {"base_revision": 1, "operations": [{"op": "replace", "section_id": "identity", "text": "冲突草稿"}]}
            self.assertEqual(self.request(base, "POST", "/v1/persona/revise", conflict, headers)[0], 409)
            code, raw = self.request(base, "POST", "/v1/persona/publish", {"revision": 2}, headers)
            self.assertEqual(code, 200, raw)
            published = json.loads(raw)["version"]
            self.assertTrue(published)
            self.assertEqual(self.request(base, "POST", "/v1/characters", dict(body, text="不得覆盖"), headers)[0], 200)
            self.assertEqual(service.core.persona_card_service.get("companion").revision, 2)
            for asset in ("index-watercolor.html", "live.js", "motion.js", "motion.css", "assets/rin-logo-refined.svg", "assets/motion/paper-grain.png"):
                code, raw = self.request(base, "GET", "/designs/tidebell/" + asset)
                self.assertEqual(code, 200, asset)
                self.assertTrue(raw)
            self.assertEqual(self.request(base, "GET", "/designs/tidebell/%2e%2e/stack.py")[0], 404)
        finally:
            if server:
                server.shutdown()
                server.server_close()
            asyncio.run(service.stop())
        service = MemoryService.start(self.config.service)
        try:
            management = ManagementApi(service)
            state = management.initialization()
            self.assertEqual(state["display_name"], "小潮")
            self.assertEqual(state["version"], published)
            self.assertEqual(state["revision"], 2)
            self.assertIsNone(service.model)
            self.assertTrue(state["published"])
        finally:
            asyncio.run(service.stop())

    def test_launcher_rejects_port_conflicts_reuses_instance_and_stops(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            port = occupied.getsockname()[1]
            text = self.config_path.read_text(encoding="utf-8").replace("port = 48931", f"port = {port}")
            self.config_path.write_text(text, encoding="utf-8")
            config = load_config(self.config_path)
            with self.assertRaisesRegex(StackError, "occupied"):
                start(config, open_browser=False)
        try:
            first = start(config, open_browser=False)
            self.assertEqual(first["status"], "running")
            self.assertEqual(first["capabilities"]["management"]["status"], "ready")
            again = start(config, open_browser=False)
            self.assertTrue(again["already_running"])
            self.assertEqual(again["pid"], first["pid"])
            self.assertEqual(again["instance_id"], first["instance_id"])
            stopped = stop(config)
            self.assertEqual(stopped["status"], "stopped")
            self.assertTrue(stopped["exit"]["drain"]["drained"])
            second = start(config, open_browser=False)
            self.assertNotEqual(second["instance_id"], first["instance_id"])
        finally:
            stop(config)
        self.assertEqual(status(config)["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
