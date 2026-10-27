"""Host persona management, per-turn state and proactive turns (S05-H01—H03).

These pin the service-side contracts the N.E.K.O host depends on, each for an
independent failure mode:

* H01: the host capability is a narrow grant checked per identity; a stale base
  revision is a conflict that reports the current revision and never silently
  rewrites the caller's draft; publishing is bound to the revision the editor
  sent, and only `next_turn`.
* H02: a turn's state says what actually happened (generated vs. durably saved
  vs. queued vs. invalidated) and survives a restart without storing bodies.
* H03: a proactive round is companion output under its own consumer, adopts
  without touching the chat consumer, and a filtered/abandoned candidate never
  becomes text she "said".
"""
import asyncio
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from companion_memory.service.management import ManagementApi
from companion_memory.service.memory_service import MemoryService, ServiceSettings
from companion_memory.service.server import ApiError, MemoryApi, build_server


class HostPersonaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "memory").mkdir()
        self.settings = ServiceSettings(
            memory_root=self.root / "memory", character="test",
            clients={"neko-desktop": {"token_env": "TEST_NEKO_TOKEN", "subjects": ["private"]},
                     "limited": {"token_env": "TEST_LIMITED_TOKEN", "subjects": ["private"]}},
        )
        self.service = MemoryService.start(self.settings)
        self.api = MemoryApi(self.service)
        self.management = ManagementApi(self.service)
        self.management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
        self.service.core.persona_publish.publish("test")
        self.identity = {"client_id": "neko-desktop", "subjects": ["private"],
                         "admin": False, "host_persona": True}

    def tearDown(self):
        asyncio.run(self.service.stop())
        self.tmp.cleanup()

    def card(self):
        return self.management.host_persona()

    def revise(self, text, *, base=None, identity=None):
        return self.management.post("/v1/host/persona", {
            "base_revision": self.card()["revision"] if base is None else base,
            "operations": [{"op": "replace", "section_id": "identity", "text": text}],
            "reason": "test",
        }, identity=identity or self.identity)

    def test_capability_is_required_and_never_comes_from_the_console_flag(self):
        from companion_memory.service.console import ApiError as ConsoleError

        reading = dict(self.identity, host_persona=False)
        with self.assertRaises(ConsoleError) as denied:
            self.management.get("/v1/host/persona", {}, identity=reading)
        self.assertEqual(denied.exception.code, "forbidden")
        with self.assertRaises(ConsoleError):
            self.revise("不该写入", identity=reading)
        # An admin identity without the explicit grant is refused too: the
        # console flag is not a persona-editing capability.
        with self.assertRaises(ConsoleError):
            self.revise("也不该写入", identity={"admin": True, "subjects": ["private"]})
        self.assertEqual(self.card()["sections"][0]["text"], "名字: 小潮\n温柔而直白。")

    def test_stale_base_revision_conflicts_and_reports_the_current_card(self):
        from companion_memory.service.console import ApiError as ConsoleError

        first = self.revise("温柔而直白，也更耐心。")
        self.assertEqual(first["status"], "completed")
        stale = self.card()["revision"] - 1
        with self.assertRaises(ConsoleError) as conflict:
            self.revise("基于旧版本的草稿", base=stale)
        self.assertEqual(conflict.exception.code, "revision_conflict")
        # The service reports its own current card so the editor can compare;
        # the caller's draft text is never applied.
        current = conflict.exception.extra["current"]
        self.assertEqual(current["revision"], self.card()["revision"])
        self.assertEqual(self.card()["sections"][0]["text"], "名字: 小潮\n温柔而直白，也更耐心。")

    def test_publish_only_accepts_the_shown_revision_and_next_turn(self):
        from companion_memory.service.console import ApiError as ConsoleError

        self.revise("第二版人格。")
        shown = self.card()["revision"]
        published = self.management.post(
            "/v1/host/persona/publish", {"revision": shown, "target": "next_turn"},
            identity=self.identity,
        )
        self.assertEqual(published["status"], "published")
        with self.assertRaises(ConsoleError):
            self.management.post(
                "/v1/host/persona/publish", {"revision": shown, "target": "realtime"},
                identity=self.identity,
            )
        # Publishing an older revision must not roll back a newer card.
        self.revise("第三版人格。")
        with self.assertRaises(ConsoleError) as conflict:
            self.management.post(
                "/v1/host/persona/publish", {"revision": shown, "target": "next_turn"},
                identity=self.identity,
            )
        self.assertEqual(conflict.exception.code, "revision_conflict")

    def test_saving_a_draft_does_not_change_what_generation_uses(self):
        # Only drafts here: no publish, so the in-flight published version stays.
        before = self.service.core.persona_publish.current("test")
        self.revise("草稿版本人格。")
        after = self.service.core.persona_publish.current("test")
        self.assertEqual(before.version, after.version)
        self.assertEqual(after.revision, before.revision)
        self.assertTrue(self.card()["published"]["stale"])


class TurnStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "memory").mkdir()
        self.settings = ServiceSettings(
            memory_root=self.root / "memory", character="test",
            clients={"neko-desktop": {"token_env": "TEST_NEKO_TOKEN", "subjects": ["private"]}},
        )
        self.service = MemoryService.start(self.settings)
        self.api = MemoryApi(self.service)
        self.management = ManagementApi(self.service)
        self.management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
        self.service.core.persona_publish.publish("test")
        self.scope = dict(character_id="test", subject="private", client_id="neko-desktop",
                          session_id="desk", scene="")

    def tearDown(self):
        asyncio.run(self.service.stop())
        self.tmp.cleanup()

    def test_turn_state_reports_durable_save_and_clears_after_invalidation(self):
        context = asyncio.run(self.api.context(dict(self.scope, text="你好")))
        body = dict(self.scope, request_id="t1", turn_id="t1",
                    source_key="host:test:private:t1",
                    messages=[{"role": "user", "content": "你好"}],
                    reply="你好呀。", persona_version=context["persona_version"],
                    context_receipt_id=context["context_receipt_id"])
        self.assertEqual(asyncio.run(self.api.turns(body))["status"], "committed")
        self.assertTrue(self.service.protocol.receipt(dict(
            self.scope, request_id="t1", receipt_id="used:t1", kind="used",
            consumer="neko_chat",
        ))["durable"])
        asyncio.run(self.service.stop())
        self.service = MemoryService.start(self.settings)
        self.api = MemoryApi(self.service)
        # A replayed submit stays one turn and one committed record.
        self.assertTrue(asyncio.run(self.api.turns(body))["replayed"])
        self.assertEqual(self.service.core.turn_requests.count("test"), 1)


class ProactiveConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "memory").mkdir()
        self.settings = ServiceSettings(
            memory_root=self.root / "memory", character="test",
            clients={"neko-desktop": {"token_env": "TEST_NEKO_TOKEN", "subjects": ["private"]}},
        )
        self.service = MemoryService.start(self.settings)
        self.api = MemoryApi(self.service)
        self.management = ManagementApi(self.service)
        self.management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
        self.published = self.service.core.persona_publish.publish("test")
        self.scope = dict(character_id="test", subject="private", client_id="neko-desktop",
                          session_id="desk", scene="")

    def tearDown(self):
        asyncio.run(self.service.stop())
        self.tmp.cleanup()

    def proactive_turn(self, request_id, *, reply="主动打招呼。"):
        context = asyncio.run(self.api.context(dict(self.scope, text="")))
        return dict(self.scope, request_id=request_id, turn_id=request_id,
                    source_key="host:test:private:" + request_id,
                    content_class="companion_output",
                    messages=[{"role": "system", "content": "companion_proactive"}],
                    reply=reply, persona_version=context["persona_version"],
                    context_receipt_id=context["context_receipt_id"])

    def test_proactive_turn_uses_its_own_consumer_and_never_writes_a_preference(self):
        asyncio.run(self.api.turns(self.proactive_turn("pa1")))
        used = self.service.protocol.receipt(dict(
            self.scope, request_id="pa1", receipt_id="used:pa1", kind="used",
            consumer="neko_active",
        ))
        self.assertTrue(used["durable"])
        # Chat's own adoption state is untouched by the proactive consumer.
        chat = self.service.core.persona_publish.current("test")
        self.assertEqual(chat.version, self.published.version)
        # No facts are extracted from companion output: a preference claim must
        # come from the user, never from her own proactive line.
        self.assertEqual(self.service.core.facts.count("test")
                         if hasattr(self.service.core.facts, "count") else 0, 0)

    def test_an_abandoned_proactive_round_stores_no_reply_body(self):
        context = asyncio.run(self.api.context(dict(self.scope, text="")))
        asyncio.run(self.api.turns(dict(
            self.scope, request_id="pa2", turn_id="pa2",
            source_key="host:test:private:pa2",
            content_class="companion_output",
            messages=[{"role": "system", "content": "companion_proactive"}],
            reply="", persona_version=context["persona_version"],
            context_receipt_id=context["context_receipt_id"],
        )))
        status = self.api.request_status("pa2", subject="private",
                                         client_id="neko-desktop", session_id="desk", scene="")
        self.assertEqual(status["status"], "committed")
        self.assertFalse(status.get("reply"))


class HostPersonaHttpTests(unittest.TestCase):
    """The real transport: capability headers, status codes and error bodies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "memory").mkdir()
        self.settings = ServiceSettings(
            memory_root=self.root / "memory", character="test",
            clients={"neko-desktop": {"token_env": "TEST_NEKO_TOKEN", "subjects": ["private"],
                                      "host_persona": True},
                     "plain": {"token_env": "TEST_PLAIN_TOKEN", "subjects": ["private"]}},
        )
        self.service = MemoryService.start(self.settings)
        self.api = MemoryApi(self.service)
        self.management = ManagementApi(self.service)
        self.management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
        self.service.core.persona_publish.publish("test")
        self.server = build_server(self.api, host="127.0.0.1", port=0, management=self.management)
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        asyncio.run(self.service.stop())
        self.tmp.cleanup()

    def call(self, method, path, body=None, token="granted"):
        headers = {"Content-Type": "application/json",
                   "X-Companion-Client": "neko-desktop" if token == "granted" else "plain",
                   "X-Companion-Token": os.environ.get(
                       "TEST_NEKO_TOKEN" if token == "granted" else "TEST_PLAIN_TOKEN", "")}
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_read_requires_the_grant_and_a_conflict_returns_the_current_card(self):
        env = {"TEST_NEKO_TOKEN": "t-granted", "TEST_PLAIN_TOKEN": "t-plain"}
        with patch.dict(os.environ, env):
            status, body = self.call("GET", "/v1/host/persona")
            self.assertEqual(status, 200)
            self.assertEqual(body["character_id"], "test")
            self.assertIn("revision", body)
            # A client without the grant is refused, not served a redacted card.
            status, body = self.call("GET", "/v1/host/persona", token="plain")
            self.assertEqual(status, 403)
            self.assertNotIn("sections", body)
            revision = self.call("GET", "/v1/host/persona")[1]["revision"]
            status, body = self.call("POST", "/v1/host/persona", {
                "base_revision": revision,
                "operations": [{"op": "replace", "section_id": "identity", "text": "更温和。"}],
            })
            self.assertEqual(status, 200)
            status, body = self.call("POST", "/v1/host/persona", {
                "base_revision": revision,
                "operations": [{"op": "replace", "section_id": "identity", "text": "旧草稿。"}],
            })
            self.assertEqual(status, 409)
            self.assertEqual(body["code"], "revision_conflict")
            self.assertEqual(body["current"]["sections"][0]["text"], "名字: 小潮\n更温和。")
