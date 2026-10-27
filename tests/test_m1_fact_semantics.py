"""M1 fact semantics: support vs. reference, business time, source merging.

Three defects from the growth design (section 3.2) are pinned here:

* G08 - a fact said in two independent turns must merge both sources, and
  deleting one source must not delete a fact the other still supports.
* G11 - "tomorrow" must resolve against the turn's occurred_at, not against
  the moment the queued extraction happened to run.
* section 6.2 - earlier dialogue only disambiguates a reference; its keys go to
  `context_refs` and never count as evidence support.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import DeletionRequest, Scope


class FixedClock:
    """A clock the test moves by hand: queue time is independent of turn time."""

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)

    def monotonic(self) -> float:
        return 0.0


class FakeModel:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    async def invoke(self, tier, messages, *, max_output_tokens, timeout_s, purpose):
        self.calls.append({"messages": messages, "purpose": purpose})
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return "[]"


def human(text: str, source_key: str | None = None):
    metadata: dict = {}
    if source_key:
        metadata["companion_event"] = {"source_key": source_key}
    message = {"type": "human", "data": {"content": text}}
    if metadata:
        message["data"]["metadata"] = metadata
    return message


def candidates(*items) -> str:
    return json.dumps(list(items), ensure_ascii=False)


class FactSemanticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _core(self, model):
        return build_memory_core(
            memory_root=self.root / "memory", clock=self.clock, model=model,
        )

    # -- G08: two independent mentions are two sources --------------------

    async def test_repeated_fact_merges_sources_and_survives_one_deletion(self):
        mention = {"text": "用户喜欢喝乌龙茶", "importance": 6}
        model = FakeModel([candidates(mention), candidates(mention)])
        core = await self._core(model)

        await core.extraction.ingest_turn(
            "demo", [human("我喜欢喝乌龙茶")],
            source_key="host:demo:private:m1", session_id="s1",
        )
        self.clock.advance(days=2)
        await core.extraction.ingest_turn(
            "demo", [human("还是喜欢乌龙茶")],
            source_key="host:demo:private:m2", session_id="s1",
        )

        facts = core.facts.load("demo")
        self.assertEqual(len(facts), 1, "one fact, not two copies")
        fact = facts[0]
        self.assertEqual(
            sorted(fact["source_event_keys"]),
            ["host:demo:private:m1", "host:demo:private:m2"],
        )
        self.assertEqual(fact["confirm_count"], 2)
        self.assertNotEqual(fact["last_confirmed_at"], fact["created_at"])

        # Deleting the first source leaves the fact standing on the second.
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:m1",),
        ))
        remaining = core.facts.load("demo")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["source_event_keys"], ["host:demo:private:m2"])
        # ...and it stays recallable. The row surviving is not enough: the
        # resolver used to tombstone the survivor id/text, so recall returned
        # zero even though the fact was still on disk.
        recalled = await core.recall.recall_memory("demo", "乌龙茶", subject="private")
        self.assertEqual(len(recalled["items"]), 1)

        # Deleting the last source finally removes it.
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:m2",),
        ))
        self.assertEqual(core.facts.load("demo"), [])

    async def test_retry_of_the_same_turn_adds_no_support(self):
        mention = {"text": "用户喜欢喝乌龙茶", "importance": 6}
        model = FakeModel([candidates(mention), candidates(mention)])
        core = await self._core(model)

        await core.extraction.ingest_turn(
            "demo", [human("我喜欢喝乌龙茶")],
            source_key="host:demo:private:r1", session_id="s1",
        )
        # Same source key and same text: a transport retry, not a new memory.
        await core.extraction.ingest_turn(
            "demo", [human("我喜欢喝乌龙茶")],
            source_key="host:demo:private:r1", session_id="s1",
        )
        facts = core.facts.load("demo")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["source_event_keys"], ["host:demo:private:r1"])
        self.assertEqual(facts[0]["confirm_count"], 1)

    # -- G11: relative time resolves against the turn, not the queue ------

    async def test_tomorrow_resolves_against_occurred_at_not_queue_time(self):
        model = FakeModel([candidates({
            "text": "用户明天有面试",
            "importance": 8,
            "event_when": {"start": {"offset": 1, "unit": "day"}, "end": None},
        })])
        core = await self._core(model)

        spoken_at = datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc)
        result = await core.extraction.ingest_turn(
            "demo", [human("我明天有面试")],
            source_key="host:demo:private:tm1", session_id="s1",
            occurred_at=spoken_at, inline=False,
        )
        self.assertEqual(result.status, "accepted")

        # The queue drains two days later. The fact must still say "the 28th".
        self.clock.advance(days=2)
        outcomes = await core.extraction.process_outbox("demo")
        self.assertEqual(outcomes[0].status, "completed")

        fact = core.facts.load("demo")[0]
        self.assertEqual(fact["text"], "用户明天有面试")
        start = datetime.fromisoformat(fact["event_start_at"])
        self.assertEqual(start.date(), datetime(2026, 10, 28).date())
        self.assertNotEqual(
            start.date(), datetime(2026, 10, 30).date(),
            "a two-day queue delay must not push the target two days out",
        )

    async def test_queued_turn_without_occurred_at_still_extracts(self):
        """Old outbox rows predate the field; they must not fail the window."""
        model = FakeModel([candidates({"text": "用户喜欢热牛奶", "importance": 5})])
        core = await self._core(model)
        await core.extraction.ingest_turn(
            "demo", [human("我睡前喝热牛奶")],
            source_key="host:demo:private:old1", session_id="s1", inline=False,
        )
        # Simulate a pre-M1 payload by rewriting the pending op without the field.
        outbox = core.outbox
        pending = outbox.pending("demo")
        self.assertEqual(len(pending), 1)
        payload = {key: value for key, value in dict(pending[0].payload).items()
                   if key != "occurred_at"}
        outcome = await core.extraction.extract_facts(
            "demo", payload["messages"],
            source_keys=payload["source_keys"],
            episode_id=payload.get("episode_id") or None,
            content_class=payload.get("content_class"),
            language="zh",
        )
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(len(core.facts.load("demo")), 1)

    # -- section 6.2: reference keys are not evidence ---------------------

    async def test_context_refs_are_recorded_but_never_count_as_support(self):
        model = FakeModel([
            '[]',
            candidates({"text": "用户偏好大号马克杯", "importance": 6}),
        ])
        core = await self._core(model)
        await core.extraction.ingest_turn(
            "demo", [human("我在挑马克杯")],
            source_key="host:demo:private:c1", session_id="s1",
        )
        await core.extraction.ingest_turn(
            "demo", [human("我一直喜欢大号的")],
            source_key="host:demo:private:c2", session_id="s1",
        )
        fact = core.facts.load("demo")[0]
        self.assertEqual(fact["source_event_keys"], ["host:demo:private:c2"])
        self.assertEqual(fact["context_refs"], ["host:demo:private:c1"])

        # The reference being deleted does not touch the fact.
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("demo"), source_keys=("host:demo:private:c1",),
        ))
        self.assertEqual(len(core.facts.load("demo")), 1)

    async def test_evidence_kind_defaults_by_source_and_is_not_free_text(self):
        model = FakeModel([
            candidates({"text": "用户喜欢乌龙茶", "importance": 6,
                        "evidence_kind": "explicit_feedback"}),
            candidates({"text": "用户周末去爬山", "importance": 5,
                        "evidence_kind": "made_up_kind"}),
        ])
        core = await self._core(model)
        await core.extraction.ingest_turn(
            "demo", [human("我明确说过喜欢乌龙茶")],
            source_key="host:demo:private:k1", session_id="s1",
        )
        await core.extraction.ingest_turn(
            "demo", [human("我周末去爬山")],
            source_key="host:demo:private:k2", session_id="s1",
        )
        by_text = {row["text"]: row for row in core.facts.load("demo")}
        # A kind the model is allowed to name is kept.
        self.assertEqual(by_text["用户喜欢乌龙茶"]["evidence_kind"], "explicit_feedback")
        # An invented kind falls back to the source default instead of being trusted.
        self.assertEqual(by_text["用户周末去爬山"]["evidence_kind"], "user_assertion")


if __name__ == "__main__":
    unittest.main()
