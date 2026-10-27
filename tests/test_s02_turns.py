"""S02 acceptance: whole-turn commit, replay and the four failure windows.

docs/standalone-service.md §3.4 lists the cases that must not produce a
duplicate turn or lose follow-up work:

* registered the source but failed before saving;
* saved the raw text but failed before enqueueing;
* committed but the HTTP response was lost;
* a same-key concurrent retry;
* a deletion during generation, and a restart replay after a deletion.

The turn-record layer is what makes all six answerable, so these tests drive it
through the real journal (record_and_save) rather than poking the file.
"""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.domain.extraction import ExtractionSettings
from rin_memory.domain.turns import TurnCommitService, TurnRequest
from rin_memory.persistence.turns import (
    STATUS_COMMITTED,
    STATUS_DELETED,
    STATUS_FAILED,
    TurnRequestStore,
)


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class FakeModel:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({"messages": messages, "purpose": purpose})
        if self.responses:
            return self.responses.pop(0)
        return "[]"


def human(text: str) -> dict:
    return {"type": "human", "data": {"content": text}}


def request(**overrides) -> TurnRequest:
    defaults = dict(
        character="demo",
        request_id="req-1",
        turn_id="turn-1",
        source_key="host:demo:private:s1",
        content_class="chat",
        messages=[human("我今天喝了乌龙茶")],
        session_id="s1",
    )
    defaults.update(overrides)
    return TurnRequest(**defaults)


class TurnCommitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model=None, **kwargs):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=model,
            extraction_settings=ExtractionSettings(inline=False), **kwargs,
        )

    def service(self, core, *, generator=None, basis_check=None):
        return TurnCommitService(
            turns=core.turn_requests,
            journal=core.journal,
            permit=core.permit,
            clock=core.clock,
            character="demo",
            ingest=core.extraction.ingest_turn,
            generator=generator,
            basis_check=basis_check,
        )

    # -- happy path ------------------------------------------------------

    async def test_external_turn_commit_is_durable_and_queues_extraction(self):
        model = FakeModel([json.dumps([{"text": "用户喜欢乌龙茶"}], ensure_ascii=False)])
        core = self.build(model)
        service = self.service(core)
        outcome = await service.commit_external(request(), reply="好呀，乌龙茶很温和")
        self.assertEqual(outcome.status, STATUS_COMMITTED)
        self.assertTrue(outcome.durable)
        self.assertFalse(outcome.replayed)
        self.assertEqual(outcome.reply, "好呀，乌龙茶很温和")
        self.assertIsNotNone(outcome.extraction_op_id)
        # R05: human + the generated assistant reply are both stored.
        self.assertEqual(core.recent.count("demo"), 2)
        self.assertEqual(len(core.outbox.pending("demo")), 1)

        record = core.turn_requests.get("demo", "req-1")
        self.assertEqual(record.status, STATUS_COMMITTED)
        self.assertIsNotNone(record.processing_position)

    async def test_generated_turn_uses_the_injected_generator(self):
        core = self.build(FakeModel())

        async def generator(messages):
            return "生成好的回复"

        service = self.service(core, generator=generator)
        outcome = await service.commit_generated(request())
        self.assertEqual(outcome.status, STATUS_COMMITTED)
        self.assertEqual(outcome.reply, "生成好的回复")

    async def test_generated_turn_without_a_generator_fails_loudly(self):
        core = self.build(FakeModel())
        service = self.service(core)
        with self.assertRaises(RuntimeError):
            await service.commit_generated(request())

    # -- §3.4 window 3: response lost ------------------------------------

    async def test_same_request_replays_instead_of_regenerating(self):
        core = self.build(FakeModel())
        calls = {"n": 0}

        async def generator(messages):
            calls["n"] += 1
            return f"回复{calls['n']}"

        service = self.service(core, generator=generator)
        first = await service.commit_generated(request())
        self.assertEqual(first.reply, "回复1")

        # The client never saw the response and retries with the same key.
        second = await service.commit_generated(request())
        self.assertEqual(second.status, STATUS_COMMITTED)
        self.assertTrue(second.replayed)
        self.assertEqual(second.reply, "回复1", "the committed reply must replay")
        self.assertEqual(calls["n"], 1, "a retry must not regenerate")
        self.assertEqual(core.recent.count("demo"), 2, "no second turn was stored")

    async def test_same_key_with_different_input_is_a_conflict(self):
        core = self.build(FakeModel())
        service = self.service(core, generator=None)
        await service.commit_external(request(), reply="第一版")
        changed = request(messages=[human("我改主意了，喝咖啡")])
        conflict = await service.commit_external(changed, reply="第二版")
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(conflict.reason, "request_id_reused_with_different_input")
        # The original reply is untouched.
        record = core.turn_requests.get("demo", "req-1")
        self.assertEqual(record.reply, "第一版")

    # -- §3.4 window 4: concurrent retry ---------------------------------

    async def test_concurrent_same_key_reports_in_progress(self):
        core = self.build(FakeModel())
        service = self.service(core)
        core.turn_requests.reserve(
            "demo",
            request_id="req-1",
            fingerprint=request().fingerprint(),
            source_key="host:demo:private:s1",
            turn_id="turn-1",
            content_class="chat",
            session_id="s1",
        )
        outcome = await service.commit_external(request(), reply="迟到的回复")
        self.assertEqual(outcome.status, "in_progress")
        self.assertTrue(outcome.retryable)
        self.assertIsNone(outcome.reply)

    # -- §3.4 window 1/2: failure before and after the raw save ----------

    async def test_generation_failure_is_retryable_and_commits_once(self):
        core = self.build(FakeModel())
        state = {"fail": True}

        async def generator(messages):
            if state["fail"]:
                raise RuntimeError("provider down")
            return "终于成功了"

        service = self.service(core, generator=generator)
        failed = await service.commit_generated(request())
        self.assertEqual(failed.status, STATUS_FAILED)
        self.assertFalse(failed.durable)
        self.assertIsNone(failed.reply)

        state["fail"] = False
        recovered = await service.commit_generated(request())
        self.assertEqual(recovered.status, STATUS_COMMITTED)
        self.assertEqual(recovered.reply, "终于成功了")
        self.assertEqual(core.recent.count("demo"), 2, "only one turn was stored")

    async def test_ingest_rejection_fails_the_commit_without_a_record_body(self):
        core = self.build(FakeModel())
        service = self.service(core)
        # A source key the barrier already deleted is rejected by ingest.
        await core.deletion_service.forget(
            __import__("rin_memory.contracts", fromlist=["DeletionRequest"])
            .DeletionRequest(
                scope=__import__("rin_memory.contracts", fromlist=["Scope"]).Scope("demo"),
                source_keys=("host:demo:private:s1",),
            ),
        )
        outcome = await service.commit_external(request(), reply="不该被留下")
        self.assertEqual(outcome.status, STATUS_FAILED)
        self.assertIsNone(outcome.reply, "a rejected turn must not keep its reply")

    # -- §3.3: basis invalidated during generation -----------------------

    async def test_stale_basis_commits_nothing(self):
        core = self.build(FakeModel())

        async def generator(messages):
            return "基于旧状态生成的回复"

        service = self.service(core, generator=generator, basis_check=lambda _c: False)
        outcome = await service.commit_generated(request())
        self.assertEqual(outcome.status, "context_stale")
        self.assertFalse(outcome.durable)
        self.assertIsNone(outcome.reply)
        self.assertTrue(outcome.retryable)
        self.assertEqual(core.recent.count("demo"), 0, "nothing was saved")

    # -- §3.4 windows 5/6: deletion and restart --------------------------

    async def test_deletion_invalidates_the_stored_reply(self):
        core = self.build(FakeModel())
        service = self.service(core)
        await service.commit_external(request(), reply="要被删除的回复")
        changed = service.invalidate_sources({"host:demo:private:s1"})
        self.assertEqual(changed, ["req-1"])

        record = core.turn_requests.get("demo", "req-1")
        self.assertEqual(record.status, STATUS_DELETED)
        self.assertIsNone(record.reply, "the body must not survive a deletion")

        replay = service.status("req-1")
        self.assertEqual(replay.status, STATUS_DELETED)
        self.assertIsNone(replay.reply, "a replay after deletion returns no text")
        self.assertFalse(replay.durable)

        # A retry of the same request is answered deleted, not re-run.
        retried = await service.commit_external(request(), reply="新生成的")
        self.assertEqual(retried.status, "deleted")
        self.assertIsNone(retried.reply)

    async def test_reopen_replays_a_commit_that_never_reached_the_view(self):
        """Window: journal fsynced, crash before the record view was saved.

        The repair only works while the journal marker is *behind* the entry;
        once the view and the marker are both saved there is nothing to replay
        (and nothing to repair). So this test rolls the marker back, which is
        exactly the on-disk state a crash inside `record_and_save` leaves.
        """
        core = self.build(FakeModel())
        service = self.service(core)
        await service.commit_external(request(), reply="提交过的回复")

        # Simulate the crash: the record was never saved and the marker never
        # advanced past the commit entry.
        path = self.root / "memory" / "demo" / "turn_requests.json"
        marker = self.root / "memory" / "demo" / "events_applied.json"
        self.assertTrue(path.exists())
        path.unlink()
        sentinel = json.loads(marker.read_text(encoding="utf-8"))
        sentinel["last_applied_position"] = (
            sentinel.get("last_applied_position", 0) - 1
        )
        sentinel.pop("last_applied_event_id", None)
        marker.write_text(json.dumps(sentinel), encoding="utf-8")

        reopened = self.build(FakeModel())
        self.assertIsNone(reopened.turn_requests.get("demo", "req-1"))
        recovery = reopened.recover("demo")
        self.assertFalse(recovery.paused, recovery.reason)

        record = reopened.turn_requests.get("demo", "req-1")
        self.assertIsNotNone(record, "the journal replay must rebuild the record")
        self.assertEqual(record.status, STATUS_COMMITTED)
        self.assertEqual(record.reply, "提交过的回复")
        self.assertEqual(record.source_key, "host:demo:private:s1")

    async def test_replay_never_resurrects_a_deleted_reply(self):
        core = self.build(FakeModel())
        service = self.service(core)
        await service.commit_external(request(), reply="提交后又删除")
        core.turn_requests.invalidate("demo", ["req-1"])

        # A replay round must keep the deletion, not restore the old body.
        reopened = self.build(FakeModel())
        reopened.recover("demo")
        record = reopened.turn_requests.get("demo", "req-1")
        self.assertEqual(record.status, STATUS_DELETED)
        self.assertIsNone(record.reply)

    # -- mirror dedupe ---------------------------------------------------

    async def test_mirrored_turn_does_not_create_a_second_experience(self):
        core = self.build(FakeModel())
        service = self.service(core)
        first = await service.commit_external(request(), reply="原回复")
        self.assertEqual(first.status, STATUS_COMMITTED)

        # Another client mirrors the same business turn under its own request id.
        mirror = await service.commit_external(
            request(request_id="req-2"), reply="重复生成的回复",
        )
        self.assertEqual(mirror.status, STATUS_COMMITTED)
        self.assertTrue(mirror.replayed)
        self.assertEqual(mirror.reply, "原回复")
        self.assertEqual(core.recent.count("demo"), 2, "the mirror is not a new turn")


class TurnStartupTests(unittest.IsolatedAsyncioTestCase):
    """§3.4: a restart must distinguish a resumable commit from an interrupted one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, model=None):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=model,
            extraction_settings=ExtractionSettings(inline=False),
        )

    def service(self, core):
        return TurnCommitService(
            turns=core.turn_requests, journal=core.journal, permit=core.permit,
            clock=core.clock, character="demo",
            ingest=core.extraction.ingest_turn,
        )

    async def test_interrupted_generation_is_retryable_after_restart(self):
        core = self.build(FakeModel())
        service = self.service(core)
        # Reserve without committing: a process that died mid-generation.
        core.turn_requests.reserve(
            "demo",
            request_id="req-1",
            fingerprint=request().fingerprint(),
            source_key="host:demo:private:s1",
            turn_id="turn-1",
            content_class="chat",
            session_id="s1",
        )
        reopened = self.build(FakeModel())
        reopened.recover("demo")
        record = reopened.turn_requests.get("demo", "req-1")
        self.assertEqual(record.status, "interrupted")
        self.assertEqual(record.reason, "interrupted_restart")
        self.assertIsNone(record.reply)

        # The same request id may now complete normally.
        outcome = await self.service(reopened).commit_external(
            request(), reply="重启后补上的回复",
        )
        self.assertEqual(outcome.status, STATUS_COMMITTED)
        self.assertEqual(outcome.reply, "重启后补上的回复")
        self.assertEqual(reopened.recent.count("demo"), 2)

    async def test_committed_record_survives_restart_marking(self):
        core = self.build(FakeModel())
        await self.service(core).commit_external(request(), reply="已完成")
        reopened = self.build(FakeModel())
        reopened.recover("demo")
        record = reopened.turn_requests.get("demo", "req-1")
        self.assertEqual(record.status, STATUS_COMMITTED)
        self.assertEqual(record.reply, "已完成")


class TurnStoreTests(unittest.TestCase):
    """Storage-level guarantees: fail closed, bound the log, keep the tombstone."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()
        self.core = build_memory_core(
            memory_root=self.root / "memory", clock=self.clock,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_corrupt_store_is_never_read_as_empty(self):
        path = self.root / "memory" / "demo" / "turn_requests.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(Exception):
            self.core.turn_requests.count("demo")
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json",
                         "a corrupt store must be left exactly as found")

    def test_unknown_schema_is_refused_before_any_write(self):
        path = self.root / "memory" / "demo" / "turn_requests.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 99, "records": {}}), encoding="utf-8")
        with self.assertRaises(Exception):
            self.core.turn_requests.count("demo")
        self.assertIn("99", path.read_text(encoding="utf-8"),
                      "the newer store must not be downgraded")

    def test_records_are_bounded(self):
        from rin_memory.persistence import turns as turns_module

        store = self.core.turn_requests
        original = turns_module.MAX_RECORDS
        turns_module.MAX_RECORDS = 5
        try:
            for index in range(8):
                store.reserve(
                    "demo",
                    request_id=f"req-{index}",
                    fingerprint="fp",
                    source_key=f"host:demo:private:x{index}",
                    turn_id=f"turn-{index}",
                    content_class="chat",
                )
            self.assertEqual(store.count("demo"), 5)
        finally:
            turns_module.MAX_RECORDS = original


if __name__ == "__main__":
    unittest.main()
