"""Console conversation without a host (S05 C preview).

The console must be able to hold a conversation *before* N.E.K.O is wired: the
same page that edits the persona sends a message, the service's own whole-turn
path commits it, and the page reads the transcript back from `recent.json`.

What these tests pin, and why each one exists:

* a sent turn is durably committed and the reply reaches `recent.json`, so the
  next turn's recall and the timeline agree with what the page showed;
* the same `request_id` replays instead of generating twice (`/v1/chat`
  idempotency), and a different text on the same id is a 409;
* a read-only root serves history but refuses to send, instead of silently
  appearing to work;
* the console talks to the *one* core: in service mode the bridge is built over
  the service's core and refuses nothing the service itself would allow;
* credentials never appear in a response: `status()` reports only whether the
  environment variable is present.
"""

import argparse
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import sys

REPO = Path(__file__).resolve().parents[1]
for extra in (REPO, REPO / "webui", REPO / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import server as webui_server  # noqa: E402

from rin_memory.service.chat import (  # noqa: E402
    ChatBridge,
    ChatError,
    DemoChatModel,
)


class RecordingModel:
    """A real-shaped conversation model that records every prompt."""

    def __init__(self, reply: str = "我记住了。", *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    def available(self, tier: str) -> bool:
        return tier == "conversation"

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({
            "tier": tier,
            "purpose": purpose,
            "messages": [dict(item) for item in messages],
        })
        if self.error is not None:
            raise self.error
        return self.reply


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        memory_root="", demo_root="", allow_write=False, character="",
        model=[], model_base_url="", model_api_key_env="", model_timeout_s=60.0,
        demo=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class ConsoleChatTests(unittest.TestCase):
    """Demo root: the full send -> commit -> history round trip."""

    def test_edit_and_clear_history_preserve_other_sessions(self):
        self.call("POST", "/api/chat/send", {"text": "旧的文字", "request_id": "edit-source"})
        _, history = self.call("GET", "/api/chat/history")
        code, changed = self.call("POST", "/api/chat/edit", {"message_id": history["messages"][0]["id"], "text": "编辑后的文字"})
        self.assertEqual(code, 200, changed)
        model = RecordingModel("收到编辑后的内容")
        self.app.chat._service.model = model
        self.call("POST", "/api/chat/send", {"text": "继续", "request_id": "after-edit"})
        self.assertIn("编辑后的文字", str(model.calls[0]["messages"]))
        self.app.core.recent.append(self.app.character, [{"type": "human", "data": {
            "content": "宿主消息", "metadata": {"session_id": "neko_chat", "subject": "private"},
        }}])
        code, cleared = self.call("POST", "/api/chat/clear", {})
        self.assertEqual(code, 200, cleared)
        self.assertEqual(self.app.chat.history()["messages"], [])
        self.assertEqual(self.app.chat.history()["turns"], [])
        self.assertIn("宿主消息", str(self.app.core.recent.messages(self.app.character)))

    def test_regenerate_branches_once_and_restores_history_after_failure(self):
        self.call("POST", "/api/chat/send", {"text": "第一句", "request_id": "regen-old-1"})
        self.call("POST", "/api/chat/send", {"text": "第二句", "request_id": "regen-old-2"})
        model = RecordingModel("新的回复")
        self.app.chat._service.model = model
        first = self.app.chat.history()["messages"][1]
        body = {"message_id": first["id"], "request_id": "regen-new"}
        code, result = self.call("POST", "/api/chat/regenerate", body)
        self.assertEqual(code, 200, result)
        rows = self.app.chat.history()["messages"]
        self.assertEqual([row["text"] for row in rows], ["第一句", "新的回复"])
        self.call("POST", "/api/chat/regenerate", body)
        self.assertEqual(len(model.calls), 1)
        model.error = RuntimeError("生成失败")
        code, result = self.call("POST", "/api/chat/regenerate", {"message_id": rows[1]["id"], "request_id": "regen-fail"})
        self.assertEqual(code, 503, result)
        self.assertEqual(self.app.chat.history()["messages"], rows)

    def test_provider_reasoning_is_saved_separately_and_cleared_on_manual_edit(self):
        from rin_memory.adapters.model_http import _extract_text
        text = _extract_text(json.dumps({"choices": [{"message": {
            "content": "最终回复", "reasoning_content": "模型返回的原始思考",
        }}]}))
        self.app.chat._service.model = RecordingModel(text)
        code, result = self.call("POST", "/api/chat/send", {"text": "想一想", "request_id": "thought-1"})
        self.assertEqual(code, 200, result)
        row = self.app.chat.history()["messages"][-1]
        self.assertEqual(row["text"], "最终回复")
        self.assertEqual(row["reasoning"], "模型返回的原始思考")
        self.call("POST", "/api/chat/edit", {"message_id": row["id"], "text": "手动改过的回复"})
        self.assertEqual(self.app.chat.history()["messages"][-1]["reasoning"], "")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="companion chat ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "demo"
        self.app = webui_server.build_app(_args())
        self.server = webui_server.RinHTTPServer(("127.0.0.1", 0), webui_server.RinHandler)
        self.server.app = self.app  # type: ignore[attr-defined]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.app.close()

    def call(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_demo_chat_commits_and_the_transcript_reads_back(self):
        code, history = self.call("GET", "/api/chat/history")
        self.assertEqual(code, 200)
        self.assertTrue(history["status"]["ready"])
        self.assertTrue(history["status"]["model"]["demo"])
        self.assertEqual(history["total"], 0)

        code, sent = self.call(
            "POST", "/api/chat/send",
            {"text": "我叫铃芽，喜欢海边", "request_id": "demo-1"},
        )
        self.assertEqual(code, 200, sent)
        self.assertEqual(sent["status"], "committed")
        self.assertTrue(sent["durable"])
        self.assertIn("铃芽", sent["reply"])

        code, history = self.call("GET", "/api/chat/history")
        self.assertEqual(code, 200)
        self.assertEqual(history["total"], 2)
        self.assertEqual(
            [row["role"] for row in history["messages"]], ["user", "assistant"],
        )
        self.assertEqual(history["messages"][0]["text"], "我叫铃芽，喜欢海边")
        self.assertEqual(history["messages"][1]["text"], sent["reply"])
        self.assertEqual([turn["status"] for turn in history["turns"]], ["committed"])

    def test_same_request_id_replays_and_a_changed_text_conflicts(self):
        code, first = self.call(
            "POST", "/api/chat/send", {"text": "我平时喝乌龙茶", "request_id": "same-1"},
        )
        self.assertEqual(code, 200, first)
        code, again = self.call(
            "POST", "/api/chat/send", {"text": "我平时喝乌龙茶", "request_id": "same-1"},
        )
        self.assertEqual(code, 200, again)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["reply"], first["reply"])

        code, conflict = self.call(
            "POST", "/api/chat/send", {"text": "我平时喝咖啡", "request_id": "same-1"},
        )
        self.assertEqual(code, 409, conflict)
        self.assertEqual(conflict["error"]["code"], "request_conflict")

        _code, history = self.call("GET", "/api/chat/history")
        # One business turn, not three: a replay and a refused conflict add none.
        self.assertEqual(history["total"], 2)
        self.assertEqual(len(history["turns"]), 1)

    def test_blank_text_and_wrong_methods_are_refused(self):
        code, payload = self.call("POST", "/api/chat/send", {"text": "   "})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_argument")
        self.assertEqual(self.call("GET", "/api/chat/send")[0], 405)
        self.assertEqual(self.call("POST", "/api/chat/history", {})[0], 405)
        self.assertEqual(self.call("POST", "/api/chat/send", {"text": "x" * 8001})[0], 400)

    def test_reply_sees_the_memory_context_and_the_persona(self):
        model = RecordingModel("好。")
        self.app.chat._service.model = model
        self.app.chat._service.state.chat_ready = True
        self.app.chat._service.state.chat_reason = None
        code, _ = self.call("POST", "/api/chat/send", {"text": "我今天想喝乌龙茶"})
        self.assertEqual(code, 200)
        self.assertEqual(len(model.calls), 1)
        messages = model.calls[0]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        # The persona text is in the system prompt, not only the raw input.
        card_text = self.app.core.persona_card_service.get(self.app.character).sections
        self.assertTrue(any(section.text.strip() in messages[0]["content"] for section in card_text))
        self.assertEqual(messages[-1], {"role": "user", "content": "我今天想喝乌龙茶"})

    def test_recoverable_model_failure_is_reported_and_retryable(self):
        from rin_memory.adapters.model_http import ModelCallError

        model = RecordingModel(error=ModelCallError("slow", kind="timeout", retryable=True))
        self.app.chat._service.model = model
        self.app.chat._service.state.chat_ready = True
        self.app.chat._service.state.chat_reason = None
        code, payload = self.call(
            "POST", "/api/chat/send", {"text": "你好", "request_id": "fail-1"},
        )
        self.assertEqual(code, 504, payload)
        # The capability stays open: a transient failure is not a config error.
        self.assertTrue(self.app.chat._service.state.chat_ready)

        model.error = None
        code, retried = self.call(
            "POST", "/api/chat/send", {"text": "你好", "request_id": "fail-1"},
        )
        self.assertEqual(code, 200, retried)
        self.assertEqual(retried["status"], "committed")


class ReadOnlyConsoleTests(unittest.TestCase):
    def test_read_only_root_serves_history_but_refuses_to_send(self):
        with tempfile.TemporaryDirectory(prefix="companion ro ") as tmp:
            root = Path(tmp) / "memory"
            root.mkdir()
            # Build a real root first (writable), then reopen it read-only.
            seed = webui_server.build_app(_args(demo_root=str(root)))
            seeded = seed.post("/api/chat/send", {"text": "先写一句"})
            self.assertEqual(seeded["status"], "committed")
            seed.close()

            args = _args(demo_root="", memory_root=str(root), allow_write=False)
            app = webui_server.build_app(args)
            try:
                history = app.get("/api/chat/history", {})
                self.assertEqual(history["total"], 2)
                self.assertFalse(history["status"]["ready"])
                self.assertIn("只读", history["status"]["reason"])
                with self.assertRaises(webui_server.ApiError) as caught:
                    app.post("/api/chat/send", {"text": "再写一句"})
                self.assertEqual(caught.exception.code, "read_only")
                self.assertEqual(caught.exception.status, 403)
            finally:
                app.close()


class BridgeUnitTests(unittest.TestCase):
    """The bridge itself, without HTTP: scope, capability and credential hygiene."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="companion bridge ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "memory"
        self.app = webui_server.build_app(_args())
        self.addCleanup(self.app.close)
        self.bridge: ChatBridge = self.app.chat

    def test_status_reports_presence_never_the_credential(self):
        status = self.bridge.status()
        blob = json.dumps(status)
        self.assertNotIn("sk-", blob)
        self.assertNotIn("api_key_stored", blob)
        # `api_key_source` is the field that names *where* a credential came
        # from; it must be a source word, never a value.
        self.assertIn(
            status["model"]["api_key_source"],
            ("none", "console", "environment"),
        )
        self.assertIn("configured", status["model"])
        self.assertIn("demo", status["model"])

    def test_send_reuses_the_console_core_and_writes_recent(self):
        outcome = self.bridge.send_sync("今天下雨了", request_id="unit-1")
        self.assertEqual(outcome["status"], "committed")
        stored = self.app.core.recent.messages(self.app.character)
        texts = [
            (item.get("data") or {}).get("content")
            for item in stored if isinstance(item, dict)
        ]
        self.assertIn("今天下雨了", texts)
        self.assertIn(outcome["reply"], texts)

    def test_empty_text_is_a_contract_error(self):
        with self.assertRaises(ChatError) as caught:
            self.bridge.send_sync("   ")
        self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual(caught.exception.status, 400)

    def test_console_without_a_bridge_reports_rather_than_invents(self):
        bare = webui_server.RinApp(
            core=self.app.core, mode="live", root=self.root,
            read_only=False, demo=False, character=self.app.character,
        )
        state = bare.chat_status()
        self.assertFalse(state["ready"])
        self.assertIn("未配置", state["reason"])
        with self.assertRaises(webui_server.ApiError) as caught:
            bare.post("/api/chat/send", {"text": "你好"})
        self.assertEqual(caught.exception.code, "chat_unavailable")
        # History still reads the committed transcript.
        history = bare.chat_history({})
        self.assertIn("messages", history)


class DemoModelTests(unittest.TestCase):
    def test_demo_model_states_that_it_is_offline(self):
        import asyncio

        model = DemoChatModel()
        reply = asyncio.run(model.invoke(
            "conversation",
            [{"role": "system", "content": "人格"}, {"role": "user", "content": "你好"}],
            max_output_tokens=64, timeout_s=5, purpose="chat_reply",
        ))
        self.assertIn("离线演示模型", reply)
        self.assertIn("你好", reply)


if __name__ == "__main__":
    unittest.main()
