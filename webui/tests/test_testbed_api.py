"""C04 testbed HTTP acceptance: real webui server over a temp demo root.

Runs the stdlib server in a background thread and talks to it with urllib only.
Covers the C04 additions while asserting the old response fields still exist.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from webui.server import RinHandler, RinHTTPServer, build_app


class WebUITestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "demo"
        self.app = build_app(argparse.Namespace(
            demo_root=str(self.root), demo=False,
            memory_root="", allow_write=True, character="",
        ))
        self.server = RinHTTPServer(("127.0.0.1", 0), RinHandler)
        self.server.app = self.app  # type: ignore[attr-defined]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.app.close()
        self._tmp.cleanup()

    def request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                return response.status, response.headers, raw
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def json(self, method: str, path: str, body=None):
        status, _headers, raw = self.request(method, path, body)
        return status, json.loads(raw.decode("utf-8"))

    def test_snapshot_and_engine_status(self):
        status, snapshot = self.json("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertIn("meta", snapshot)
        self.assertIn("persona", snapshot)
        self.assertIn("evolution", snapshot)
        status, engine = self.json("GET", "/api/engine/status")
        self.assertEqual(status, 200)
        for key in (
            "mode", "read_only", "demo", "root", "character",
            "card_revision", "published_version", "evolution_mode",
            "consumers",
        ):
            self.assertIn(key, engine)
        self.assertEqual(engine["character"], "suzume")
        self.assertTrue(engine["demo"])
        self.assertFalse(engine["read_only"])

    def test_persona_revise_publish_adopt(self):
        _status, persona = self.json("GET", "/api/persona")
        revision = persona["card"]["revision"]
        tone = next(
            section for section in persona["card"]["sections"]
            if section["section_id"] == "tone"
        )
        revised = self.json("POST", "/api/persona/revise", {
            "base_revision": revision,
            "operations": [{
                "op": "replace", "section_id": "tone",
                "text": "C04 测试改写的语气：先给结论，再补一句理由。",
            }],
            "reason": "C04 testbed edit",
            "request_id": "c04-revise-1",
        })[1]
        self.assertEqual(revised["status"], "completed")
        new_revision = revised["revision"]
        self.assertGreater(new_revision, revision)
        self.assertEqual(tone["section_id"], "tone")

        published = self.json("POST", "/api/persona/publish", {
            "target": "next_turn", "revision": new_revision,
        })[1]
        self.assertEqual(published["status"], "published")
        self.assertTrue(published["version"])
        self.assertEqual(published["revision"], new_revision)

        synced = self.json("POST", "/api/persona/sync")[1]
        self.assertTrue(synced["synced"])
        adopted = self.json("POST", "/api/persona/adopt", {
            "consumer": "chat", "version": published["version"],
            "receipt_id": "c04-adopt-1",
        })[1]
        self.assertEqual(adopted["status"], "match")
        self.assertEqual(adopted["consumer"], "chat")
        status, persona_after = self.json("GET", "/api/persona")
        self.assertEqual(status, 200)
        self.assertEqual(
            persona_after["consumers"]["chat"]["status"], "adopted",
        )
        self.assertIn("pending", persona_after)
        self.assertIn("failed", persona_after)
        self.assertIn("stale", persona_after)

    def test_evolution_mode_propose_and_accept(self):
        mode = self.json("POST", "/api/evolution/mode", {"mode": "propose"})[1]
        self.assertEqual(mode["mode"], "propose")
        self.assertTrue(mode["enabled"])
        evolution = self.json("GET", "/api/evolution")[1]
        self.assertEqual(evolution["mode"], "propose")
        self.assertEqual(evolution["auto_evolution"]["mode"], "propose")

        # Legacy bool endpoint still maps true -> propose, false -> off.
        legacy = self.json("POST", "/api/evolution/settings", {"enabled": True})[1]
        self.assertEqual(legacy["mode"], "propose")
        legacy_off = self.json("POST", "/api/evolution/settings", {"enabled": False})[1]
        self.assertEqual(legacy_off["mode"], "off")
        self.json("POST", "/api/evolution/mode", {"mode": "propose"})

        persona = self.json("GET", "/api/persona")[1]
        revision = persona["card"]["revision"]
        revised = self.json("POST", "/api/persona/revise", {
            "base_revision": revision,
            "operations": [{
                "op": "replace", "section_id": "tone",
                "text": "C04 演化前的临时语气。",
            }],
            "reason": "prepare C04 proposal", "request_id": "c04-prep-1",
        })[1]
        self.assertEqual(revised["status"], "completed")
        revision = revised["revision"]
        proposed = self.json("POST", "/api/evolution/propose", {
            "base_revision": revision,
            "source_refs": ["fact_tone"],
            "focus_sections": ["tone"],
            "reason": "C04 testbed proposal",
            "request_id": "c04-proposal-1",
        })[1]
        self.assertEqual(proposed["status"], "proposed")
        accepted = self.json(
            "POST", f"/api/evolution/{proposed['proposal_id']}/accept",
            {"reason": "C04 accept"},
        )[1]
        self.assertEqual(accepted["status"], "accepted", accepted)
        self.assertGreater(accepted["revision_after"], revision)

        evaluated = self.json("POST", "/api/evolution/evaluate", {})[1]
        self.assertIn("status", evaluated)
        self.assertIn("pending", evaluated)
        retried = self.json("POST", "/api/evolution/retry", {})[1]
        self.assertIn("status", retried)
        self.assertIn("retried", retried)
        self.assertIn("pending", retried)

    def test_cleanup_delete_search_and_export(self):
        cleanup = self.json("GET", "/api/cleanup")[1]
        self.assertIn("deletable", cleanup)
        self.assertTrue(cleanup["deletable"])
        item = next(
            row for row in cleanup["deletable"]
            if row.get("type") == "fact"
        )
        deleted = self.json("POST", "/api/cleanup/delete", {
            "type": item["type"], "id": item["id"],
            "reason": "C04 testbed delete", "request_id": "c04-delete-1",
        })[1]
        self.assertEqual(deleted["status"], "completed")
        self.assertTrue(deleted["stopped_using"])
        cleanup_after = self.json("GET", "/api/cleanup")[1]
        ids = [row["id"] for row in cleanup_after["deletions"]]
        self.assertIn(deleted["deletion_id"], ids)
        self.assertNotIn(
            item["id"],
            [row["id"] for row in cleanup_after["deletable"]],
        )

        search = self.json("GET", "/api/search?q=%E5%92%96%E5%95%A1")[1]
        self.assertIn("events", search)
        self.assertIn("facts", search)
        status, headers, raw = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        exported = json.loads(raw.decode("utf-8"))
        self.assertIn("snapshot", exported)

    def test_testbed_static_and_error_shapes(self):
        status, headers, raw = self.request("GET", "/testbed/index.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"testbed", raw.lower())
        status, _headers, _raw = self.request("GET", "/testbed")
        self.assertEqual(status, 200)
        status, _headers, _raw = self.request("GET", "/testbed/")
        self.assertEqual(status, 200)
        status, headers, _raw = self.request("GET", "/testbed/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        status, headers, _raw = self.request("GET", "/testbed/style.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))
        status, body = self.json("GET", "/api/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        status, body = self.json("POST", "/api/snapshot", {})
        self.assertEqual(status, 405)
        self.assertEqual(body["error"]["code"], "method_not_allowed")
        status, body = self.json("GET", "/api/persona/revise")
        self.assertEqual(status, 405)
        self.assertEqual(body["error"]["code"], "method_not_allowed")
        status, _body = self.json("POST", "/testbed", {})
        self.assertEqual(status, 405)
        status, _body = self.json("GET", "/testbed/%2e%2e/server.py")
        self.assertEqual(status, 404)

    def test_read_only_write_is_forbidden(self):
        root = Path(self._tmp.name) / "readonly"
        root.mkdir()
        app = build_app(argparse.Namespace(
            demo_root="", demo=False,
            memory_root=str(root), allow_write=False, character="",
        ))
        server = RinHTTPServer(("127.0.0.1", 0), RinHandler)
        server.app = app  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = urllib.request.Request(
                base + "/api/persona/revise",
                data=json.dumps({
                    "base_revision": 0,
                    "operations": [{
                        "op": "replace", "section_id": "tone", "text": "x",
                    }],
                }).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=20)
            self.assertEqual(raised.exception.code, 403)
            payload = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(payload["error"]["code"], "read_only")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.close()


if __name__ == "__main__":
    unittest.main()
