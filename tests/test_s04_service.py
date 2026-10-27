"""S04 acceptance: the `/v1` contract, capability states and ownership.

`docs/standalone-service.md` §3.1/§3.2/§4.2 define what this suite pins:

* the four endpoints, and that `/context` never writes;
* the §3.2 error envelope and the status each code maps to;
* one generation per business turn, replayed on retry;
* capability state reported separately for storage, chat and background;
* the service is the single writable owner (S01 lock) and releases it on stop.

No network: the model is a fake injected on the service, and the transport is a
real loopback server on port 0 so the HTTP path is exercised, not simulated.
"""

import asyncio
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from rin_memory.adapters.composition import build_memory_core
from rin_memory.adapters.model_http import (
    HttpModelInvoker,
    ModelCallError,
    ModelSettings,
    _extract_text,
    _map_http_error,
)
from rin_memory.errors import RootLockConflict
from rin_memory.persistence.ownership import acquire_root_lock
from rin_memory.service.memory_service import (
    MemoryService,
    ServiceSettings,
    ServiceStartupError,
)
from rin_memory.service.server import ApiError, MemoryApi, build_server

CARD = [
    {"section_id": "identity", "text": "你是一只猫娘。",
     "editable_by": {"user": True, "evolution": False}},
    {"section_id": "tone", "text": "说话温柔简洁。",
     "editable_by": {"user": True, "evolution": True}},
]


def _prepare_root(root: Path) -> None:
    core = build_memory_core(memory_root=root)
    core.recover("demo")
    core.persona_card_service.import_initial("demo", CARD)


class FakeModel:
    """Stand-in for the conversation tier; records the prompt it received."""

    def __init__(self, reply: str = "温柔的回答", *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    def available(self, tier: str) -> bool:
        return True

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({
            "tier": tier, "purpose": purpose,
            "roles": [item["role"] for item in messages],
            "system": messages[0]["content"] if messages else "",
        })
        if self.error is not None:
            raise self.error
        return self.reply


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "memory"
        self.root.mkdir(parents=True)
        _prepare_root(self.root)
        self.services: list[MemoryService] = []

    def tearDown(self) -> None:
        for service in self.services:
            asyncio.run(service.stop())
        self._tmp.cleanup()

    def start(self, **overrides) -> MemoryService:
        settings = ServiceSettings(
            memory_root=self.root, character="demo", **overrides,
        )
        service = MemoryService.start(settings)
        self.services.append(service)
        return service

    def api(self, service: MemoryService, model: FakeModel | None = None) -> MemoryApi:
        if model is not None:
            service.model = model  # type: ignore[assignment]
            service.state.chat_ready = True
            service.state.chat_reason = None
        return MemoryApi(service)


class CapabilityTests(ServiceTestCase):
    def test_storage_ready_but_chat_closed_without_a_model(self):
        service = self.start()
        api = self.api(service)
        health = api.health()
        self.assertEqual(health["storage"]["status"], "ready")
        self.assertEqual(health["chat"]["status"], "unavailable")
        self.assertIn("no conversation model", health["chat"]["reason"])

    def test_chat_is_refused_before_reserving_anything(self):
        service = self.start()
        api = self.api(service)
        with self.assertRaises(ApiError) as caught:
            asyncio.run(api.chat({
                "request_id": "c1", "turn_id": "t1",
                "source_key": "host:demo:private:c1",
                "messages": [{"role": "user", "content": "你好"}],
            }))
        self.assertEqual(caught.exception.code, "chat_unavailable")
        self.assertEqual(caught.exception.status, 503)
        self.assertTrue(caught.exception.retryable)
        # Nothing was reserved: a refused request leaves no pending turn.
        self.assertEqual(service.core.turn_requests.count("demo"), 0)

    def test_external_turns_work_without_a_model(self):
        service = self.start()
        api = self.api(service)
        result = asyncio.run(api.turns({
            "request_id": "e1", "turn_id": "et1",
            "source_key": "host:demo:private:e1",
            "messages": [{"role": "user", "content": "我平时喝乌龙茶"}],
            "reply": "记住了",
        }))
        self.assertEqual(result["status"], "committed")
        self.assertTrue(result["durable"])

    def test_background_status_reports_loop_and_outbox(self):
        service = self.start()
        api = self.api(service)
        health = api.health()
        self.assertIn("running", health["background"])
        self.assertIn("outbox_pending", health["background"])


class ChatFlowTests(ServiceTestCase):
    def test_chat_commits_once_and_replays(self):
        service = self.start()
        model = FakeModel("温柔的回答")
        api = self.api(service, model)
        body = {
            "request_id": "c1", "turn_id": "t1",
            "source_key": "host:demo:private:c1",
            "messages": [{"role": "user", "content": "今天好累"}],
        }
        first = asyncio.run(api.chat(dict(body)))
        self.assertEqual(first["status"], "committed")
        self.assertEqual(first["reply"], "温柔的回答")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0]["tier"], "conversation")
        # The persona card reached the prompt as the system message.
        self.assertIn("猫娘", model.calls[0]["system"])
        self.assertEqual(model.calls[0]["roles"], ["system", "user"])

        second = asyncio.run(api.chat(dict(body)))
        self.assertTrue(second["replayed"])
        self.assertEqual(second["reply"], "温柔的回答")
        self.assertEqual(len(model.calls), 1, "a retry must not regenerate")
        # R05: the stored turn now includes the assistant reply.
        self.assertEqual(service.core.recent.count("demo"), 2)

    def test_model_failure_maps_to_a_retryable_error(self):
        service = self.start()
        model = FakeModel(error=ModelCallError("boom", kind="timeout", retryable=True))
        api = self.api(service, model)
        with self.assertRaises(ApiError) as caught:
            asyncio.run(api.chat({
                "request_id": "c2", "turn_id": "t2",
                "source_key": "host:demo:private:c2",
                "messages": [{"role": "user", "content": "在吗"}],
            }))
        self.assertIn(caught.exception.code, ("chat_unavailable", "model_timeout"))
        self.assertTrue(caught.exception.retryable)

    def test_conflicting_request_id_is_409(self):
        service = self.start()
        api = self.api(service, FakeModel())
        asyncio.run(api.chat({
            "request_id": "c3", "turn_id": "t3",
            "source_key": "host:demo:private:c3",
            "messages": [{"role": "user", "content": "第一句"}],
        }))
        with self.assertRaises(ApiError) as caught:
            asyncio.run(api.chat({
                "request_id": "c3", "turn_id": "t3b",
                "source_key": "host:demo:private:c3b",
                "messages": [{"role": "user", "content": "完全不同的一句"}],
            }))
        self.assertEqual(caught.exception.code, "request_conflict")
        self.assertEqual(caught.exception.status, 409)


class ContextTests(ServiceTestCase):
    def test_context_is_read_only(self):
        service = self.start()
        api = self.api(service)
        before = {
            "turns": service.core.turn_requests.count("demo"),
            "recent": service.core.recent.count("demo"),
            "facts": len(service.core.facts.load("demo")),
        }
        payload = asyncio.run(api.context({"text": "我今天喝了乌龙茶"}))
        self.assertEqual(payload["card_revision"], 1)
        self.assertTrue(payload["projection"]["identity"].startswith("你是一只猫娘"))
        self.assertTrue(payload["context_receipt_id"].startswith("ctx_"))
        after = {
            "turns": service.core.turn_requests.count("demo"),
            "recent": service.core.recent.count("demo"),
            "facts": len(service.core.facts.load("demo")),
        }
        self.assertEqual(before, after, "/context must not record anything")

    def test_receipt_changes_with_the_persona_version(self):
        service = self.start()
        api = self.api(service)
        first = asyncio.run(api.context({}))
        service.core.persona_card_service.revise(
            "demo",
            {
                "base_revision": 1,
                "operations": [
                    {"op": "replace", "section_id": "tone",
                     "text": "说话更活泼一点。"},
                ],
            },
        )
        second = asyncio.run(api.context({}))
        self.assertNotEqual(first["card_revision"], second["card_revision"])
        self.assertNotEqual(
            first["context_receipt_id"], second["context_receipt_id"],
        )


class OwnershipTests(ServiceTestCase):
    def test_service_is_the_single_writer_and_releases_on_stop(self):
        service = self.start()
        with self.assertRaises(RootLockConflict):
            acquire_root_lock(self.root, mode="cli")
        with self.assertRaises(ServiceStartupError) as caught:
            MemoryService.start(ServiceSettings(
                memory_root=self.root, character="demo",
            ))
        self.assertEqual(caught.exception.code, "root_lock_conflict")
        asyncio.run(service.stop())
        self.services.clear()
        lock = acquire_root_lock(self.root, mode="cli")
        lock.release()

    def test_startup_refuses_missing_root_and_character(self):
        with self.assertRaises(ServiceStartupError) as caught:
            MemoryService.start(ServiceSettings(
                memory_root=self.root / "absent", character="demo",
            ))
        self.assertEqual(caught.exception.code, "missing_root")
        with self.assertRaises(ServiceStartupError) as caught:
            MemoryService.start(ServiceSettings(
                memory_root=self.root, character="   ",
            ))
        self.assertEqual(caught.exception.code, "missing_character")

    def test_stop_is_idempotent(self):
        service = self.start()
        asyncio.run(service.stop())
        asyncio.run(service.stop())
        self.assertTrue(service.closed)
        lock = acquire_root_lock(self.root, mode="cli")
        lock.release()


class HttpTransportTests(ServiceTestCase):
    """The real loopback server, so headers, status codes and JSON are checked."""

    def setUp(self) -> None:
        super().setUp()
        service = self.start()
        self.model = FakeModel()
        api = self.api(service, self.model)
        self.server = build_server(api, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        super().tearDown()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self._url(path), timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self._url(path), data=json.dumps(body).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health_and_context_over_http(self):
        status, health = self._get("/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["storage"]["status"], "ready")
        status, context = self._get("/v1/context")
        self.assertEqual(status, 200)
        self.assertEqual(context["card_revision"], 1)

    def test_turns_and_request_lookup_over_http(self):
        status, body = self._post("/v1/turns", {
            "request_id": "h1", "turn_id": "ht1",
            "source_key": "host:demo:private:h1",
            "messages": [{"role": "user", "content": "我平时喝乌龙茶"}],
            "reply": "记住啦",
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["durable"])
        status, lookup = self._get("/v1/requests/h1")
        self.assertEqual(status, 200)
        self.assertEqual(lookup["status"], "committed")
        self.assertEqual(lookup["reply"], "记住啦")

    def test_error_envelope_shape(self):
        status, body = self._post("/v1/turns", {"request_id": "only-id"})
        self.assertEqual(status, 400)
        self.assertEqual(
            set(body), {"code", "message", "retryable", "request_id"},
        )
        self.assertEqual(body["code"], "invalid_argument")

    def test_unknown_path_and_host_header(self):
        status, body = self._get("/v1/nope")
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_argument")
        request = urllib.request.Request(
            self._url("/v1/health"), headers={"Host": "evil.example"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 403, "a non-loopback Host must be refused")


class ModelAdapterTests(unittest.TestCase):
    """The adapter is dependency-free; its parsing and error mapping are pure."""

    def test_extract_text_shapes(self):
        self.assertEqual(
            _extract_text(json.dumps({"choices": [{"message": {"content": "你好"}}]})),
            "你好",
        )
        self.assertEqual(
            _extract_text(json.dumps(
                {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]},
            )),
            "ab",
        )
        with self.assertRaises(ModelCallError) as caught:
            _extract_text("not json")
        self.assertEqual(caught.exception.kind, "format")

    def test_http_error_mapping(self):
        import urllib.error

        def error(code: int) -> urllib.error.HTTPError:
            return urllib.error.HTTPError("u", code, "x", {}, None)  # type: ignore[arg-type]

        self.assertEqual(_map_http_error(error(401)).kind, "auth")
        self.assertFalse(_map_http_error(error(401)).retryable)
        self.assertEqual(_map_http_error(error(429)).kind, "rate_limit")
        self.assertTrue(_map_http_error(error(429)).retryable)
        self.assertEqual(_map_http_error(error(503)).kind, "transport")
        self.assertEqual(_map_http_error(error(404)).kind, "config")

    def test_unconfigured_adapter_reports_unavailable(self):
        invoker = HttpModelInvoker(ModelSettings())
        self.assertFalse(invoker.available("conversation"))
        with self.assertRaises(ModelCallError) as caught:
            asyncio.run(invoker.invoke(
                "conversation", [{"role": "user", "content": "hi"}],
                max_output_tokens=16, timeout_s=1, purpose="test",
            ))
        self.assertEqual(caught.exception.kind, "config")

    def test_missing_credential_is_an_auth_error(self):
        invoker = HttpModelInvoker(ModelSettings(
            base_url="http://127.0.0.1:1/v1",
            api_key_env="COMPANION_MEMORY_TEST_KEY_NOT_SET",
            models={"conversation": "m"},
        ))
        with self.assertRaises(ModelCallError) as caught:
            asyncio.run(invoker.invoke(
                "conversation", [{"role": "user", "content": "hi"}],
                max_output_tokens=16, timeout_s=1, purpose="test",
            ))
        self.assertEqual(caught.exception.kind, "auth")
        self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
