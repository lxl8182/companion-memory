"""Host reservations, restart replay, and the native generation boundary."""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from rin_memory.service.memory_service import MemoryService, ServiceSettings
from rin_memory.service.management import ManagementApi
from rin_memory.service.server import ApiError, MemoryApi


class NekoHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = ServiceSettings(memory_root=self.root / "memory", character="test")
        self.settings.memory_root.mkdir()
        self.start()
        self.management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
        self.service.core.persona_publish.publish("test")
        self.scope = dict(character_id="test", subject="private", client_id="neko", session_id="desktop", scene="")

    def start(self):
        self.service = MemoryService.start(self.settings)
        self.api = MemoryApi(self.service)
        self.management = ManagementApi(self.service)

    def tearDown(self):
        asyncio.run(self.service.stop())
        self.tmp.cleanup()

    def context(self):
        return asyncio.run(self.api.context(dict(self.scope, text="你好")))

    def body(self, context, request_id="one"):
        return dict(self.scope, request_id=request_id, turn_id=request_id,
            source_key="host:test:private:" + request_id, messages=[{"role": "user", "content": "你好"}],
            reply="你好呀。", persona_version=context["persona_version"],
            context_receipt_id=context["context_receipt_id"])

    def test_reserved_generation_survives_service_restart_and_replays_once(self):
        context = self.context()
        reservation = dict(self.scope, request_id="one", context_receipt_id=context["context_receipt_id"])
        self.assertTrue(self.service.protocol.reserve(reservation)["durable"])
        asyncio.run(self.service.stop())
        self.start()
        with self.assertRaises(ApiError):
            self.service.protocol.validate(context["context_receipt_id"], self.service.protocol.scope(self.scope))
        result = asyncio.run(self.api.turns(self.body(context)))
        self.assertTrue(result["durable"])
        self.assertTrue(asyncio.run(self.api.turns(self.body(context)))["replayed"])
        self.assertEqual(result["persona_version"], context["persona_version"])
        self.assertEqual(self.service.core.turn_requests.count("test"), 1)

    def test_reservation_cannot_cross_identity_or_reauthorize_invalidated_basis(self):
        context = self.context()
        self.service.protocol.reserve(dict(self.scope, request_id="one", context_receipt_id=context["context_receipt_id"]))
        other = self.body(context)
        other["subject"] = "someone-else"
        with self.assertRaises(ApiError):
            asyncio.run(self.api.turns(other))
        with patch.object(self.service.protocol, "basis", return_value={"deletion_version": 1, "correction_version": 0}):
            fresh = self.context()
            with self.assertRaises(ApiError):
                asyncio.run(self.api.turns(self.body(fresh)))
        self.assertEqual(self.service.core.turn_requests.count("test"), 0)

    def test_native_turn_recovery_never_regenerates_and_context_failure_stops_generation(self):
        path = Path(__file__).resolve().parents[2] / "N.E.K.O/main_logic/remote_memory.py"
        spec = importlib.util.spec_from_file_location("neko_remote_test", path)
        remote = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(remote)
        api = self.api

        class Client(remote.RemoteMemoryClient):
            lose_response = True
            unavailable = False

            async def request(client, path, body):
                try:
                    return await client.dispatch(path, body)
                except ApiError as exc:
                    raise remote.RemoteMemoryError(exc.code) from exc

            async def dispatch(client, path, body):
                body = dict(client.scope, **body)
                if client.unavailable:
                    raise remote.RemoteMemoryError("service_unreachable", retryable=True)
                if path == "/v1/context":
                    return await api.context(body)
                if path == "/v1/context/reserve":
                    return self.service.protocol.reserve(body)
                if path == "/v1/turns":
                    result = await api.turns(body)
                    if client.lose_response:
                        client.lose_response = False
                        raise remote.RemoteMemoryError("service_unreachable", retryable=True)
                    return result
                return self.service.protocol.receipt(body)

        class Message:
            def __init__(self, *, content):
                self.content = content

        class Session:
            _instructions = "host tool protocol"
            statuses = []

            async def on_status_message(session, value):
                session.statuses.append(value)

        module = types.ModuleType("utils.llm_client")
        module.SystemMessage = module.HumanMessage = module.AIMessage = Message
        settings = dict(self.scope, state_root=str(self.root / "host"), url="unused", token="unused")
        client = Client(settings)
        remote._client = client
        generated = []

        @remote.memory_turn
        async def generate(session, text, **kwargs):
            generated.append(text)
            self.assertIn("温柔而直白", session._instructions)
            self.assertIn("host tool protocol", session._instructions)
            await kwargs["_memory_result"]("你好呀。")

        async def scenario():
            await generate(Session(), "你好")
            self.assertEqual(len((await client.load())["items"]), 1)
            recovered = Client(settings)
            recovered.lose_response = False
            self.assertTrue(await recovered.flush())
            self.assertEqual(self.service.core.turn_requests.count("test"), 1)
            self.assertEqual(generated, ["你好"])
            recovered.unavailable = True
            remote._client = recovered
            await generate(Session(), "不要生成")
            self.assertEqual(generated, ["你好"])
            recovered.unavailable = False
            _, stale = await recovered.begin("临时输入", "stale")
            with patch.object(self.service.protocol, "basis", return_value={"deletion_version": 1, "correction_version": 0}):
                self.assertFalse(await recovered.finish(stale, "过期结果"))
            self.assertFalse(recovered._queue["items"])
            recovered._queue["deliveries"] = {"stale": 2}
            self.assertTrue(await recovered.flush())
            self.assertFalse(recovered._queue["deliveries"])

        with patch.dict(os.environ, NEKO_COMPANION_CONFIG="configured"), patch.dict(sys.modules, {"utils.llm_client": module}):
            asyncio.run(scenario())
