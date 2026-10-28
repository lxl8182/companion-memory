"""Closeout risks: retryable citations, source scope, notes and output consumers."""
import asyncio
from datetime import timedelta

from rin_memory.contracts import DeletionRequest, Scope
from tests.test_ro02_batch import BatchHarness, BatchModel, human
from tests.test_m6_server_gate import ServiceHarness


class CitationSafetyTests(BatchHarness):
    async def test_bad_batch_citations_stay_pending_until_a_valid_retry(self):
        for case, refs in enumerate((None, [], [999], [True], [1, 999])):
            with self.subTest(refs=refs):
                model = BatchModel([
                    {"facts": [{"text": "用户喜欢晨跑", "sources": refs}]},
                    {"facts": [{"text": "用户喜欢晨跑", "sources": [1]}]},
                ])
                core = self.build(model)
                core.recover("demo")
                keys = []
                for index in range(2):
                    self.clock.advance(seconds=1)
                    key = f"host:demo:private:retry-{case}-{index}"
                    keys.append(key)
                    await core.extraction.ingest_turn("demo", [human("我喜欢晨跑")],
                        source_key=key, session_id="retry-session")
                first = await core.extraction.process_outbox("demo", limit=50)
                self.assertEqual(first[0].reason, "unresolved_sources")
                self.assertEqual(len(core.outbox.pending("demo")), 2)
                self.assertEqual(core.facts.load("demo"), [])
                retried = await core.extraction.process_outbox("demo", limit=50)
                self.assertEqual(retried[0].status, "completed")
                self.assertEqual(core.facts.load("demo")[0]["source_event_keys"], [keys[0]])
                # Clear only this fixture between the independent retry cases.
                core.facts.save("demo", [])

    async def test_model_cannot_choose_a_batched_fact_subject_or_scene(self):
        core = self.build(BatchModel([{"facts": [{"text": "用户喜欢晨跑", "sources": [1],
            "subject": "private", "applies_to": {}}]}]))
        core.recover("demo")
        for index in range(2):
            self.clock.advance(seconds=1)
            message = human("我喜欢晨跑")
            message["data"]["metadata"] = {"subject": "friend", "applies_to": {"game_id": "arena"}}
            await core.extraction.ingest_turn("demo", [message],
                source_key=f"host:demo:friend:scoped{index}", session_id="scoped")
        await core.extraction.process_outbox("demo", limit=50)
        row = core.facts.load("demo")[0]
        self.assertEqual(row["subject"], "friend")
        self.assertEqual(row["applies_to"], {"game_id": "arena"})


class ContextProtocolTests(ServiceHarness):
    CLIENTS = {**ServiceHarness.CLIENTS,
               "qq": {**ServiceHarness.CLIENTS["qq"], "memory_outputs": True}}

    def test_segment_note_is_scoped_current_and_does_not_call_a_model(self):
        core = self.service.core
        now = core.clock.now()
        receipt = core.state_service.commit("test", {"internal": True, "base_version": 0,
            "request_id": "note-state", "ops": [{"op": "set_progress", "id": "interview",
                "text": "主人准备面试", "source_event_ids": ["host:test:private:interview"]}]})
        self.assertTrue(receipt.ok, receipt.reason)
        body = {"subject": "private", "session_id": "qq-note", "switched_at": (now + timedelta(hours=7)).isoformat(),
                "last_user_at": now.isoformat()}
        status, note = self.call("POST", "/v1/segment/note", body)
        self.assertEqual(status, 200, note)
        self.assertIn("面试", note["text"])
        self.assertIn("host:test:private:interview", str(note["refs"]))
        status, other = self.call("POST", "/v1/segment/note", {**body, "subject": "friend"}, client="other-subject")
        self.assertEqual(status, 200, other)
        self.assertEqual(other["text"], "")
        core.directives.record("test", term="面试", kind="ban_topic", locale="zh")
        status, suppressed = self.call("POST", "/v1/segment/note", body)
        self.assertEqual(status, 200, suppressed)
        self.assertEqual(suppressed["text"], "")

    def test_output_context_consumption_tracks_summary_without_a_turn_reservation(self):
        core = self.service.core
        source = "host:test:private:summary"
        core.facts.save("test", [{"id": "f_summary", "text": "主人有一台服务器", "subject": "private",
                                  "source_event_keys": [source]}])
        summary = core.working_summary_service.build("test", query="服务器").to_dict()
        scope = {"subject": "private", "scene": "", "session_id": "qq-output", "client_id": "qq"}
        context = self.service.protocol.issue(scope, {"memory_ids": [], "source_ids": [], "working_summary": summary})
        payload = {**scope, "request_id": "contact-one", "context_receipt_id": context["context_receipt_id"],
            "consumer_refs": {"plugin_id": "dsh-rin-memory", "session_id": "qq-output",
                              "memory_ids": ["f_summary", "foreign"], "source_ids": [source, "foreign"]}}
        status, result = self.call("POST", "/v1/context/consume", payload)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["consumer_refs"], "recorded")
        self.assertFalse(self.service.protocol._reservations_path.exists())
        refs = core.consumer_refs.refs("test")
        self.assertEqual(refs[0]["memory_ids"], ["f_summary"])
        self.assertEqual(refs[0]["source_ids"], [source])
        again, _ = self.call("POST", "/v1/context/consume", payload)
        self.assertEqual(again, 200)
        self.assertEqual(len(core.consumer_refs.refs("test")), 1)
        forgotten = asyncio.run(core.deletion_service.forget(DeletionRequest(scope=Scope("test"), source_keys=(source,))))
        self.assertIn(forgotten.status, ("completed", "pending"))
        status, cleanup = self.call("GET", "/v1/cleanup/pending?plugin_id=dsh-rin-memory")
        self.assertEqual(status, 200, cleanup)
        self.assertIn(forgotten.deletion_id, str(cleanup))
