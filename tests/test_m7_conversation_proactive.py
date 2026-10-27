"""Conversation threads and leased host generation, on disposable data."""
import json

if __package__:
    from .test_b04_extraction import FakeModel, human
    from .test_m6_proactive import ProactiveBase
    from .test_m6_server_gate import ServiceHarness
else:
    from test_b04_extraction import FakeModel, human
    from test_m6_proactive import ProactiveBase
    from test_m6_server_gate import ServiceHarness


class ConversationTests(ProactiveBase):
    async def test_one_extraction_call_updates_and_closes_a_source_bound_thread(self):
        model = FakeModel([json.dumps({"facts": [], "state_updates": [{
            "op": "set_progress", "text": "主人准备面试", "next_step": "问面试结果",
            "followup_when": {"offset": 1, "unit": "day"}, "agreed_followup": True,
        }]})])
        core = self.build(model=model)
        scope = {"plugin_id": "dsh-rin-memory"}
        scene = "plugin:dsh-rin-memory"
        message = human("我明天去面试，明天这个点记得问我结果")
        message["data"]["metadata"] = {"applies_to": scope, "scene": scene}
        await core.extraction.ingest_turn("rin", [message],
            source_key="host:rin:private:m1", session_id="qq-test")
        items = core.state_service.read("rin", scene=scope).items
        self.assertEqual(core.state_service.read("rin").items, ())
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["source_event_ids"], ["host:rin:private:m1"])
        self.assertTrue(item["agreed_followup"])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(core.proactive_service.derive("rin", scene=scene)["created"]), 1)
        self.assertEqual(core.proactive_service.claim("rin", subject="private", scene=scene, owner="qq",
            session_id="qq-test", request_id="early")["status"], "idle")
        model.responses.append(json.dumps({"facts": [], "state_updates": [{
            "op": "close_progress", "id": item["id"], "text": "面试结束，已经拿到 offer",
        }]}))
        message = human("面试结束啦，已经拿到 offer，不用再跟进")
        message["data"]["metadata"] = {"applies_to": scope, "scene": scene}
        await core.extraction.ingest_turn("rin", [message],
            source_key="host:rin:private:m2", session_id="qq-test")
        self.assertEqual(core.state_service.read("rin", scene=scope).items, ())
        self.clock.advance(days=2)
        self.assertEqual(core.proactive_service.claim("rin", subject="private", scene=scene, owner="qq",
            session_id="qq-test", request_id="closed")["status"], "idle")

    async def test_lease_preemption_and_generated_body_survive_replay(self):
        core = self.build()
        self.seed_thread(core)
        core.proactive_service.derive("rin")
        p = core.proactive_service
        lease = p.claim("rin", subject="private", scene="", owner="qq", session_id="qq-test", request_id="lease")
        row = lease["candidate"]
        self.assertEqual(p.claim("rin", subject="private", scene="", owner="qq", session_id="other", request_id="other")["status"], "idle")
        kwargs = dict(lease_id=row["lease_id"], owner="qq", session_id="qq-test")
        p.release_claim("rin", row["candidate_id"], postpone=True, **kwargs)
        self.assertEqual(p.claim("rin", subject="private", scene="", owner="qq", session_id="qq-test", request_id="pass-retry")["status"], "idle")
        self.clock.advance(hours=3)
        row = p.claim("rin", subject="private", scene="", owner="qq", session_id="qq-test", request_id="after-pass")["candidate"]
        kwargs["lease_id"] = row["lease_id"]
        result = p.complete_claim("rin", row["candidate_id"], text="面试回来了吗，想听听你怎么说。", **kwargs)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(core.output_service.get("rin", result.output_id, include_text=True)["text"], "面试回来了吗，想听听你怎么说。")
        self.assertEqual(p.complete_claim("rin", row["candidate_id"], text="重试不能换正文", **kwargs).output_id, result.output_id)
        p.absorb_user_turn("rin", text="先说点别的，我现在很忙", subject="private", request_id="new-human")
        self.assertFalse(p.check_delivery("rin", row["candidate_id"], **kwargs)["ok"])
        self.assertEqual(core.output_service.get("rin", result.output_id)["status"], "interrupted")


class ProactiveHttpTests(ServiceHarness):
    def test_capability_and_session_binding_are_enforced(self):
        scope = dict(subject="private", session_id="qq-test", scene="plugin:dsh-rin-memory", request_id="poll")
        status, _ = self.call("POST", "/v1/proactive/poll", scope)
        self.assertEqual(status, 403)
        status, result = self.call("POST", "/v1/proactive/poll", scope, client="desk")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "idle")
        status, result = self.call("POST", "/v1/proactive/complete", {
            **scope, "candidate_id": "foreign", "lease_id": "foreign", "text": "hi",
        }, client="desk")
        self.assertEqual(status, 403, result)
        p = self.service.core.proactive_service
        now = self.service.core.clock.now().isoformat()
        queued = p.enqueue("test", cause="agreement", maturity_source="explicit_agreement",
            text="面试结果怎样了", source_keys=["host:test:private:interview"], agreed_at=now, due_at=now)
        self.assertTrue(queued.ok, queued.reason)
        status, leased = self.call("POST", "/v1/proactive/poll", scope, client="desk")
        self.assertEqual(status, 200, leased)
        self.assertEqual(leased["status"], "ready")
        body = {**scope, "candidate_id": leased["candidate"]["candidate_id"],
                "lease_id": leased["candidate"]["lease_id"],
                "context_receipt_id": leased["context"]["context_receipt_id"], "text": "面试顺利吗？"}
        status, completed = self.call("POST", "/v1/proactive/complete", body, client="desk")
        self.assertEqual(status, 200, completed)
        self.assertTrue(completed["ok"], completed)
        self.api.protocol._contexts.clear()
        status, replayed = self.call("POST", "/v1/proactive/complete", body, client="desk")
        self.assertEqual(status, 200, replayed)
        self.assertEqual(replayed["output_id"], completed["output_id"])
        status, _ = self.call("POST", "/v1/proactive/observe", {**scope, "request_id": "new-user", "text": "我现在想聊别的"}, client="desk")
        self.assertEqual(status, 200)
        status, checked = self.call("POST", "/v1/proactive/check", body, client="desk")
        self.assertEqual(status, 200, checked)
        self.assertFalse(checked["ok"])
