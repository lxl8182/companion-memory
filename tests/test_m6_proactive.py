"""M6-A acceptance: proactive candidates, the pending-chat intent queue, timing,
the send-save-receipt loop, the server-side plugin gate and the receipt closure.

Design sections 11.3/11.4, 10.4, 8.3 and 12.3 decide the behaviours pinned here.
Each test names the failure it prevents:

* a message with no source must be refused -- an unprompted line without a real
  reason is the exact thing the design forbids (design 11.3);
* the four operations on a live queue (postpone / rewrite / merge / cancel) keep
  the original source, so a steered candidate is still the same reason;
* maturity is routed by source: an agreement or a real task result is its own
  reason, an autonomous inference must clear the M4 eligibility gate;
* the pre-send recheck is mandatory: it reads the record and the conversation
  again, and a user who already opened the topic absorbs the candidate (G25);
* timing: quiet hours, the daily cap and the minimum gap, with an agreed
  reminder exempt (design 11.3), including the cross-midnight arithmetic that
  has no timezone database to lean on in this environment;
* sending produces an M5 output record, so only delivered parts become a shared
  experience and a retry resends only the missing parts (G22);
* "先不聊" is a topic pause, "别再提" is a mention restriction and "忘掉" is a
  real deletion -- three different mechanisms (G26, design 10.4);
* the plugin gate is identity subjects AND trusted plugins AND role, with the
  host-source path unchanged and trusted_plugins empty by default;
* the existing used/sent/delivered receipt values do not move by one byte.
"""

import asyncio
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from companion_memory.adapters.composition import build_memory_core
from companion_memory.domain.proactive import (
    DISPATCH_ABSORBED,
    DISPATCH_DEFER,
    DISPATCH_IDLE,
    DISPATCH_REFUSED,
    DISPATCH_SEND,
    ProactiveSettings,
    REASON_DAILY_CAP,
    REASON_FACT_NOT_ELIGIBLE,
    REASON_MIN_GAP,
    REASON_NO_SOURCE,
    REASON_NOT_MATURE,
    REASON_QUIET_HOURS,
    REASON_TOPIC_CHANGED,
)


class MutableClock:
    def __init__(self, moment=None) -> None:
        self._now = moment or datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)

    def set(self, moment: datetime) -> None:
        self._now = moment


def fact_row(fact_id, text, *, episode_id="", keys=None,
             evidence_kind="user_assertion", importance=8, subject="private"):
    row = {
        "id": fact_id,
        "text": text,
        "importance": importance,
        "source": "user_observation",
        "schema_version": 2,
        "absorbed": True,
        "created_at": "2026-10-27T03:00:00+00:00",
        "last_confirmed_at": "2026-10-27T03:00:00+00:00",
        "event_start_at": "2026-10-27T03:00:00+00:00",
        "source_event_keys": list(keys or ["host:rin:private:" + fact_id]),
        "subject": subject,
        "applies_to": {},
        "evidence_kind": evidence_kind,
    }
    if episode_id:
        row["episode_id"] = episode_id
    return row


def mature_reflection(reflection_id, text, *, episode_id="ep:host:interview",
                      keys=None):
    """A reflection that clears the M4 gate: two independent experiences."""
    support = [
        {"key": "host:rin:private:e1", "fact_id": "f1",
         "kind": "observed_action", "flags": ["actual_result", "real_contact"]},
        {"key": "host:rin:private:e2", "fact_id": "f2",
         "kind": "observed_action", "flags": ["actual_result", "real_contact"]},
    ]
    basis = [
        {"experience": "episode:ep1", "facts": ["f1"], "support": [support[0]]},
        {"experience": "episode:ep2", "facts": ["f2"], "support": [support[1]]},
    ]
    return {
        "id": reflection_id,
        "text": text,
        "status": "confirmed",
        "memory_kind": "",
        "topic": "interview",
        "topic_key": "interview",
        "scope_key": "private",
        "applies_to": {},
        "episode_id": episode_id,
        "source_fact_ids": ["f1", "f2"],
        "source_event_keys": list(keys or ["host:rin:private:e1",
                                          "host:rin:private:e2"]),
        "support_count": 2,
        "support_evidence": {
            "actual_result": ["host:rin:private:e1", "host:rin:private:e2"],
            "user_feedback": [], "both_party": [], "real_contact": [],
        },
        "support_basis": basis,
        "created_at": "2026-10-27T03:00:00+00:00",
        "updated_at": "2026-10-27T03:00:00+00:00",
    }


def thin_reflection(reflection_id, text, *, episode_id="ep:host:thin"):
    """A reflection resting on one statement: not mature (M4 gate)."""
    return {
        "id": reflection_id,
        "text": text,
        "status": "confirmed",
        "memory_kind": "",
        "topic_key": "coffee",
        "scope_key": "private",
        "applies_to": {},
        "episode_id": episode_id,
        "source_fact_ids": ["f_thin"],
        "source_event_keys": ["host:rin:private:e_thin"],
        "support_count": 1,
        "support_evidence": {
            "actual_result": [], "user_feedback": [],
            "both_party": [], "real_contact": [],
        },
        "support_basis": [{
            "experience": "episode:ep_thin",
            "facts": ["f_thin"],
            "support": [{"key": "host:rin:private:e_thin", "fact_id": "f_thin",
                         "kind": "user_assertion", "flags": []}],
        }],
        "created_at": "2026-10-27T03:00:00+00:00",
        "updated_at": "2026-10-27T03:00:00+00:00",
    }


class ProactiveBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # 2026-10-28 12:00 local at UTC+8 = 04:00Z: outside the quiet window.
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory", **kwargs):
        kwargs.setdefault("proactive_settings", ProactiveSettings())
        return build_memory_core(
            memory_root=self.root / name, clock=self.clock, **kwargs,
        )

    def seed_thread(self, core, *, text="帮主人准备面试", next_step="明天问面试结果",
                    thread_id="th_interview", age_hours=24):
        """One open item plus the fact that carries its episode."""
        core.facts.save("rin", [fact_row(
            "fact_interview", "用户明天要去面试",
            episode_id="ep:host:interview:1027",
            keys=["host:rin:private:e_interview"],
        )])
        moment = self.clock.now()
        self.clock.set(moment - timedelta(hours=age_hours))
        receipt = core.state_service.commit("rin", {
            "internal": True, "base_version": 0,
            "request_id": "state-" + thread_id,
            "ops": [{
                "op": "set_progress", "text": text, "next_step": next_step,
                "source_event_ids": ["e_interview"],
            }],
        })
        self.clock.set(moment)
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt


class SourceRequiredTests(ProactiveBase):
    """A candidate with no source is refused: no fabricated reasons."""

    async def test_a_candidate_without_a_source_is_refused(self):
        core = self.build()
        core.recover("rin")
        receipt = core.proactive_service.enqueue(
            "rin", cause="thread_due", text="面试怎么样了？",
            why="明天问面试结果", maturity_source="autonomous_inference",
        )
        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.reason, REASON_NO_SOURCE)
        self.assertEqual(core.proactive_service.candidates("rin"), [])

    async def test_an_unknown_cause_or_maturity_is_refused(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        bad_cause = core.proactive_service.enqueue(
            "rin", cause="vibes", text="想你了",
            maturity_source="explicit_agreement",
            thread_id="th_interview",
        )
        self.assertEqual(bad_cause.reason, "unknown_cause")
        bad_maturity = core.proactive_service.enqueue(
            "rin", cause="thread_due", text="面试怎么样了？",
            maturity_source="because_i_feel_like_it",
            thread_id="th_interview",
        )
        self.assertEqual(bad_maturity.reason, "unknown_maturity")
        self.assertEqual(core.proactive_service.candidates("rin"), [])

    async def test_a_deleted_source_refuses_the_candidate(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        from companion_memory.contracts import DeletionRequest, DeletionTarget, Scope

        receipt = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("rin", "private"), mode="entry",
            targets=(DeletionTarget("fact", "fact_interview"),),
            source_keys=("host:rin:private:e_interview",),
            reason="user_request",
        ))
        self.assertIn(receipt.status, ("completed", "pending"), receipt)
        refused = core.proactive_service.enqueue(
            "rin", cause="thread_due", text="面试怎么样了？",
            maturity_source="explicit_agreement",
            source_keys=["host:rin:private:e_interview"],
        )
        self.assertFalse(refused.ok)
        self.assertEqual(refused.reason, "source_deleted")


class MaturityRoutingTests(ProactiveBase):
    """Agreed/task results form a candidate; an inference must pass M4."""

    async def test_an_agreement_forms_a_candidate_directly(self):
        core = self.build()
        core.recover("rin")
        receipt = core.proactive_service.enqueue(
            "rin", cause="agreement", text="说好了九点叫你起床。",
            why="昨天答应的", maturity_source="explicit_agreement",
            agreed_at="2026-10-28T01:00:00+00:00",
            due_at="2026-10-28T01:00:00+00:00",
            source_keys=["host:rin:private:e_agreed"],
        )
        self.assertTrue(receipt.ok, receipt.reason)
        self.assertEqual(receipt.status, "pending")
        self.assertTrue(receipt.candidate["honor_agreed_time"])

    async def test_a_real_task_result_forms_a_candidate_directly(self):
        core = self.build()
        core.recover("rin")
        receipt = core.proactive_service.enqueue(
            "rin", cause="task_result", text="整理好了，一共十七条。",
            maturity_source="task_result",
            source_keys=["host:rin:private:e_task"],
        )
        self.assertTrue(receipt.ok, receipt.reason)

    async def test_a_raw_fact_may_not_carry_an_inferred_candidate(self):
        core = self.build()
        core.recover("rin")
        core.facts.save("rin", [fact_row(
            "fact_coffee", "用户喜欢拿铁", keys=["host:rin:private:e_coffee"],
        )])
        receipt = core.proactive_service.enqueue(
            "rin", cause="interest_news", text="今天看到一家拉花很好的店。",
            maturity_source="autonomous_inference",
            fact_ids=["fact_coffee"],
        )
        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.reason, REASON_FACT_NOT_ELIGIBLE)

    async def test_an_immature_reflection_is_not_mature_enough(self):
        core = self.build()
        core.recover("rin")
        core.reflections.save("rin", [thin_reflection("ref_thin", "用户可能喜欢咖啡")])
        receipt = core.proactive_service.enqueue(
            "rin", cause="shared_finding", text="想到你上次说的那杯咖啡。",
            maturity_source="autonomous_inference",
            reflection_ids=["ref_thin"],
        )
        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.reason, REASON_NOT_MATURE)

    async def test_a_mature_reflection_can_carry_a_candidate(self):
        core = self.build()
        core.recover("rin")
        core.reflections.save("rin", [
            mature_reflection("ref_ok", "主人面试前会紧张"),
        ])
        receipt = core.proactive_service.enqueue(
            "rin", cause="shared_finding", text="想到你面试前那次紧张的样子。",
            maturity_source="autonomous_inference",
            reflection_ids=["ref_ok"],
        )
        self.assertTrue(receipt.ok, receipt.reason)


class QueueOperationTests(ProactiveBase):
    """Postpone / rewrite / merge / cancel on a live candidate."""

    def _one(self, core, **kwargs):
        receipt = core.proactive_service.enqueue(
            "rin", cause="thread_due", text="面试怎么样了？",
            why="明天问面试结果", maturity_source="explicit_agreement",
            thread_id="th_interview",
            source_keys=["host:rin:private:e_interview"],
            **kwargs,
        )
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt.candidate_id

    async def test_postpone_moves_the_time_and_keeps_the_source(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._one(core)
        receipt = core.proactive_service.postpone(
            "rin", candidate_id, minutes=180, reason="她还在忙",
        )
        self.assertEqual(receipt.status, "postponed")
        candidate = receipt.candidate
        self.assertEqual(candidate["status"], "postponed")
        self.assertEqual(candidate["source_keys"], ["host:rin:private:e_interview"])
        self.assertEqual(candidate["thread_id"], "th_interview")
        self.assertIsNotNone(candidate["due_at"])

    async def test_rewrite_keeps_the_previous_wording(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._one(core)
        receipt = core.proactive_service.rewrite(
            "rin", candidate_id, text="面试顺利吗？",
        )
        candidate = receipt.candidate
        self.assertEqual(candidate["text"], "面试顺利吗？")
        self.assertEqual([item["text"] for item in candidate["rewrite_history"]],
                         ["面试怎么样了？"])
        self.assertEqual(candidate["status"], "pending")

    async def test_merge_folds_one_candidate_into_another(self):
        core = self.build()
        core.recover("rin")
        first = self._one(core)
        second = core.proactive_service.enqueue(
            "rin", cause="interest_news", text="看到一条面试相关的新闻。",
            maturity_source="task_result",
            source_keys=["host:rin:private:e_news"],
        )
        self.assertTrue(second.ok, second.reason)
        receipt = core.proactive_service.merge(
            "rin", second.candidate_id, into=first, reason="同一件事",
        )
        self.assertEqual(receipt.status, "merged")
        self.assertEqual(receipt.candidate["merged_into"], first)
        survivor = core.proactive_service.get("rin", first)
        # The survivor inherits the merged source, so a deletion of either one
        # still reaches the message that will actually be sent.
        self.assertIn("host:rin:private:e_news", survivor["source_keys"])
        self.assertIn("host:rin:private:e_interview", survivor["source_keys"])

    async def test_cancel_removes_it_from_the_live_queue(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._one(core)
        receipt = core.proactive_service.cancel(
            "rin", candidate_id, reason="user_declined",
        )
        self.assertEqual(receipt.status, "cancelled")
        live = core.proactive_service.candidates(
            "rin", statuses=["pending", "postponed"],
        )
        self.assertEqual(live, [])
        again = core.proactive_service.cancel("rin", candidate_id)
        self.assertTrue(again.duplicate)

    async def test_a_non_live_candidate_cannot_be_edited(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._one(core)
        core.proactive_service.cancel("rin", candidate_id)
        for call in (
            lambda: core.proactive_service.postpone("rin", candidate_id, minutes=10),
            lambda: core.proactive_service.rewrite("rin", candidate_id, text="x"),
        ):
            receipt = call()
            self.assertFalse(receipt.ok)
            self.assertEqual(receipt.reason, "not_live")


class DispatchAndRecheckTests(ProactiveBase):
    """Timing limits, the pre-send recheck, and the user taking the topic first."""

    def _agreed(self, core, **kwargs):
        receipt = core.proactive_service.enqueue(
            "rin", cause="agreement", text="说好九点叫你。",
            maturity_source="explicit_agreement",
            source_keys=["host:rin:private:e_agreed"], **kwargs,
        )
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt.candidate_id

    def _ordinary(self, core, candidate_id="pc_fixed", text="面试怎么样了？", **kwargs):
        # A real task result is a direct reason but is NOT an agreed reminder, so
        # the trial caps and quiet hours apply to it.
        receipt = core.proactive_service.enqueue(
            "rin", cause="task_result", text=text,
            maturity_source="task_result", candidate_id=candidate_id,
            source_keys=["host:rin:private:e_interview"], **kwargs,
        )
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt.candidate_id

    async def test_quiet_hours_choose_the_right_boundary(self):
        # 2026-10-28 07:59 at UTC+8 -> quiet; 09:00 -> not quiet.
        core = self.build()
        core.recover("rin")
        self._ordinary(core)
        self.clock.set(datetime(2026, 10, 27, 23, 59, tzinfo=timezone.utc))
        defers = core.proactive_service.dispatch("rin")
        self.assertEqual(defers["status"], DISPATCH_DEFER)
        self.assertEqual(defers["reason"], REASON_QUIET_HOURS)
        self.clock.set(datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc))
        allowed = core.proactive_service.dispatch("rin")
        self.assertEqual(allowed["status"], DISPATCH_SEND, allowed)

    async def test_the_quiet_window_wraps_midnight(self):
        settings = ProactiveSettings(
            quiet_start_hour=23, quiet_end_hour=7,
            user_utc_offset_minutes=0,
        )
        core = self.build(proactive_settings=settings, name="wrap")
        core.recover("rin")
        candidate_id = self._ordinary(core)
        # The recheck is the same decision without sending, so the boundary can
        # be walked hour by hour without consuming the candidate.
        for moment, quiet in (
            (datetime(2026, 10, 28, 22, 59, tzinfo=timezone.utc), False),
            (datetime(2026, 10, 28, 23, 0, tzinfo=timezone.utc), True),
            (datetime(2026, 10, 28, 23, 59, tzinfo=timezone.utc), True),
            (datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc), True),
            (datetime(2026, 10, 29, 6, 59, tzinfo=timezone.utc), True),
            (datetime(2026, 10, 29, 7, 0, tzinfo=timezone.utc), False),
        ):
            verdict = core.proactive_service.recheck(
                "rin", candidate_id, now=moment,
            )
            expected = DISPATCH_DEFER if quiet else DISPATCH_SEND
            self.assertEqual(verdict["status"], expected, (moment, verdict))
            if quiet:
                self.assertEqual(verdict["reason"], REASON_QUIET_HOURS)

    async def test_the_daily_cap_and_the_minimum_gap(self):
        core = self.build()
        core.recover("rin")
        # Three separate sent rows today would already break the cap; send the
        # first one for real and check that a second is refused for the gap.
        first = self._ordinary(core, candidate_id="pc_one")
        sent = core.proactive_service.dispatch("rin")
        self.assertEqual(sent["status"], DISPATCH_SEND, sent)
        self.assertTrue(sent["output_id"])

        self._ordinary(core, candidate_id="pc_two", text="面试还好吗？")
        self.clock.advance(minutes=30)
        gapped = core.proactive_service.dispatch("rin")
        self.assertEqual(gapped["status"], DISPATCH_DEFER)
        self.assertEqual(gapped["reason"], REASON_MIN_GAP)

        self.clock.advance(minutes=61)
        second = core.proactive_service.dispatch("rin")
        self.assertEqual(second["status"], DISPATCH_SEND, second)
        self._ordinary(core, candidate_id="pc_three", text="还在等结果吗？")
        self.clock.advance(minutes=120)
        capped = core.proactive_service.dispatch("rin")
        self.assertEqual(capped["status"], DISPATCH_DEFER)
        self.assertEqual(capped["reason"], REASON_DAILY_CAP)

    async def test_an_agreed_reminder_ignores_quiet_hours_and_the_cap(self):
        core = self.build()
        core.recover("rin")
        self._agreed(core)
        # 03:00 local at UTC+8 is deep inside the quiet window.
        self.clock.set(datetime(2026, 10, 27, 19, 0, tzinfo=timezone.utc))
        verdict = core.proactive_service.dispatch("rin")
        self.assertEqual(verdict["status"], DISPATCH_SEND, verdict)

    async def test_a_user_who_opens_the_topic_absorbs_the_candidate(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._ordinary(core)
        self.clock.advance(minutes=5)
        verdict = core.proactive_service.dispatch(
            "rin", user_recent_text="面试结果出来了，过了！",
            user_spoke_at=(self.clock.now() - timedelta(minutes=2)).isoformat(),
        )
        self.assertEqual(verdict["status"], DISPATCH_ABSORBED, verdict)
        row = core.proactive_service.get("rin", candidate_id)
        self.assertEqual(row["status"], "merged")
        self.assertEqual(row["absorbed_into"], "dispatch")
        # Nothing was said, so nothing became a shared experience.
        self.assertEqual(core.recent.count("rin"), 0)

    async def test_a_user_changing_topic_makes_the_candidate_wait(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._ordinary(core)
        self.clock.advance(minutes=5)
        verdict = core.proactive_service.dispatch(
            "rin", user_recent_text="今天想吃火锅",
            user_spoke_at=(self.clock.now() - timedelta(minutes=2)).isoformat(),
        )
        self.assertEqual(verdict["status"], DISPATCH_DEFER, verdict)
        self.assertEqual(verdict["reason"], REASON_TOPIC_CHANGED)
        row = core.proactive_service.get("rin", candidate_id)
        self.assertEqual(row["status"], "pending")

    async def test_a_topic_the_user_raised_last_turn_is_not_repeated(self):
        """Lead review: "just spoke" alone would repeat last turn's topic.

        The user raised the interview one turn ago and is now silent. The
        candidate must defer on the topic cooldown, not send -- otherwise the
        scheduler asks about something that was settled two minutes ago.
        """
        core = self.build()
        core.recover("rin")
        # One turn ago the user opened the topic themselves (no candidate existed
        # yet, so nothing was absorbed -- this is the turn *after* that one).
        core.proactive_service.absorb_user_turn(
            "rin", subject="private", text="面试结果出来了，是下周二。",
            request_id="turn-1",
        )
        self.clock.advance(minutes=10)
        # A later derivation queues a follow-up for the same topic.
        candidate_id = self._ordinary(core)
        # Now they are silent, and this is a *different* turn.
        self.clock.advance(minutes=10)
        verdict = core.proactive_service.dispatch("rin")
        self.assertEqual(verdict["status"], DISPATCH_DEFER, verdict)
        self.assertEqual(verdict["reason"], "recently_discussed")
        row = core.proactive_service.get("rin", candidate_id)
        # Deferred, not silently cancelled: it is still queued for later.
        self.assertIn(row["status"], ("pending", "postponed"))
        self.assertEqual(core.recent.count("rin"), 0)

    async def test_the_topic_cooldown_expires(self):
        core = self.build()
        core.recover("rin")
        core.proactive_service.absorb_user_turn(
            "rin", subject="private", text="面试结果出来了。", request_id="turn-1",
        )
        candidate_id = self._ordinary(core)
        # The default window is six hours; past it the follow-up is allowed.
        self.clock.advance(minutes=361)
        verdict = core.proactive_service.dispatch("rin")
        self.assertEqual(verdict["status"], DISPATCH_SEND, verdict)
        row = core.proactive_service.get("rin", candidate_id)
        self.assertEqual(row["status"], "sent")

    async def test_an_agreed_reminder_is_exempt_from_the_topic_cooldown(self):
        core = self.build()
        core.recover("rin")
        core.proactive_service.absorb_user_turn(
            "rin", subject="private", text="九点叫我起来", request_id="turn-1",
        )
        receipt = core.proactive_service.enqueue(
            "rin", cause="agreement", text="说好九点叫你。",
            maturity_source="explicit_agreement",
            source_keys=["host:rin:private:e_agreed"],
        )
        self.assertTrue(receipt.ok, receipt.reason)
        # The user asking for it is the reason for the message, not a reason to
        # suppress it (design 11.3: an agreement follows the agreed time).
        verdict = core.proactive_service.dispatch("rin")
        self.assertEqual(verdict["status"], DISPATCH_SEND, verdict)

    async def test_the_pre_send_recheck_refuses_a_deleted_source(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._ordinary(core)
        from companion_memory.contracts import DeletionRequest, Scope

        await core.deletion_service.forget(DeletionRequest(
            scope=Scope("rin", "private"), mode="entry",
            source_keys=("host:rin:private:e_interview",),
            reason="user_request",
        ))
        # The deletion cleaner reaches the candidate itself: the queued message
        # about deleted content is invalidated (id kept, text cleared).
        row = core.proactive_service.get("rin", candidate_id)
        self.assertEqual(row["status"], "invalidated")
        self.assertEqual(row["text"], "")
        verdict = core.proactive_service.recheck("rin", candidate_id)
        self.assertEqual(verdict["status"], DISPATCH_REFUSED)
        refused = core.proactive_service.send("rin", candidate_id)
        self.assertFalse(refused.ok)
        self.assertEqual(core.recent.count("rin"), 0)

    async def test_the_recheck_refuses_a_closed_thread(self):
        core = self.build()
        core.recover("rin")
        receipt = core.proactive_service.enqueue(
            "rin", cause="thread_due", text="面试怎么样了？",
            maturity_source="explicit_agreement",
            thread_id="th_gone",
            source_keys=["host:rin:private:e_interview"],
        )
        verdict = core.proactive_service.recheck("rin", receipt.candidate_id)
        self.assertEqual(verdict["status"], DISPATCH_REFUSED)
        self.assertEqual(verdict["reason"], "thread_closed")


class SendReceiptLoopTests(ProactiveBase):
    """Sending writes an output record; only delivered parts become shared."""

    def _candidate(self, core, **kwargs):
        receipt = core.proactive_service.enqueue(
            "rin", cause="agreement", text="说好了九点叫你起床，别赖床。",
            maturity_source="explicit_agreement",
            source_keys=["host:rin:private:e_agreed"], **kwargs,
        )
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt.candidate_id

    async def test_sending_persists_a_body_but_writes_no_shared_line(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._candidate(core)
        receipt = core.proactive_service.send("rin", candidate_id)
        self.assertTrue(receipt.ok, receipt.reason)
        self.assertEqual(receipt.status, "sent")
        output_id = receipt.output_id
        self.assertTrue(output_id)
        record = core.output_service.get("rin", output_id, include_text=True)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["kind"], "proactive")
        self.assertEqual(core.recent.count("rin"), 0)
        # Cause and candidate id travel with the record: the "why" is stored once.
        self.assertEqual(record["cause"]["candidate_id"], candidate_id)
        self.assertEqual(record["cause"]["source_keys"],
                         ["host:rin:private:e_agreed"])

    async def test_delivery_moves_only_the_delivered_parts_into_the_record(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._candidate(core)
        sent = core.proactive_service.send("rin", candidate_id)
        delivered = core.proactive_service.deliver(
            "rin", candidate_id, parts=[{"index": 0}], platform_id="m1",
        )
        self.assertTrue(delivered.ok, delivered.reason)
        self.assertEqual(delivered.detail["recent_written"], 1)
        self.assertEqual(core.recent.count("rin"), 1)
        again = core.proactive_service.deliver(
            "rin", candidate_id, parts=[{"index": 0}],
        )
        self.assertTrue(again.duplicate)
        self.assertEqual(core.recent.count("rin"), 1)

    async def test_a_failed_segment_resumes_only_the_missing_parts(self):
        """G22: a partially delivered body resumes exactly what is missing."""
        core = self.build()
        core.recover("rin")
        long_text = "第一段说完了。" * 60
        receipt = core.proactive_service.enqueue(
            "rin", cause="task_result", text=long_text,
            maturity_source="task_result",
            source_keys=["host:rin:private:e_task"],
        )
        self.assertTrue(receipt.ok, receipt.reason)
        candidate_id = receipt.candidate_id
        sent = core.proactive_service.send("rin", candidate_id)
        self.assertTrue(sent.ok, sent.reason)
        record = core.output_service.get("rin", sent.output_id, include_text=True)
        self.assertGreater(record["parts_total"], 1)

        first = core.proactive_service.deliver(
            "rin", candidate_id, parts=[{"index": 0}], platform_id="m1",
        )
        self.assertTrue(first.ok, first.reason)
        self.assertEqual(first.status, "partial")
        pending = core.proactive_service.resumable("rin")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["output_id"], sent.output_id)
        self.assertNotIn(0, pending[0]["pending_indices"])
        self.assertEqual(
            len(pending[0]["pending_indices"]), record["parts_total"] - 1,
        )

        # The retry resends only the indices the receipt did not name.
        missing = [{"index": index} for index in pending[0]["pending_indices"]]
        rest = core.proactive_service.deliver(
            "rin", candidate_id, parts=missing, platform_id="m2",
        )
        self.assertTrue(rest.ok, rest.reason)
        self.assertEqual(rest.status, "delivered")
        self.assertEqual(rest.detail["appended_parts"],
                         pending[0]["pending_indices"])
        self.assertEqual(core.recent.count("rin"), record["parts_total"])
        # Nothing is left to resume, and a repeated report is a duplicate.
        self.assertEqual(core.proactive_service.resumable("rin"), [])
        replay = core.proactive_service.deliver(
            "rin", candidate_id, parts=missing,
        )
        self.assertTrue(replay.duplicate)
        self.assertEqual(core.recent.count("rin"), record["parts_total"])

    async def test_sending_twice_keeps_one_body(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._candidate(core)
        first = core.proactive_service.send("rin", candidate_id)
        second = core.proactive_service.send("rin", candidate_id)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.output_id, first.output_id)
        self.assertEqual(len(core.output_service.list("rin")), 1)

    async def test_sending_survives_a_restart_with_the_same_output(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._candidate(core)
        sent = core.proactive_service.send("rin", candidate_id)
        reopened = self.build(name="memory")
        reopened.recover("rin")
        resumed = reopened.proactive_service.resumable("rin")
        self.assertEqual([item["candidate_id"] for item in resumed], [candidate_id])
        record = reopened.output_service.get(
            "rin", sent.output_id, include_text=True,
        )
        self.assertIn("别赖床", record["text"])
        again = reopened.proactive_service.send("rin", candidate_id)
        self.assertTrue(again.duplicate)
        self.assertEqual(again.output_id, sent.output_id)


class DeriveTests(ProactiveBase):
    """Derivation reads existing records and never invents a reason."""

    async def test_a_due_open_thread_becomes_a_candidate(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core, age_hours=24)
        result = core.proactive_service.derive("rin")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["created"]), 1, result)
        rows = core.proactive_service.candidates("rin")
        self.assertEqual(rows[0]["cause"], "thread_due")
        self.assertEqual(rows[0]["intent"], "continue_topic")
        self.assertIn("面试", rows[0]["text"])

    async def test_a_fresh_thread_is_not_due_yet(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core, age_hours=1)
        result = core.proactive_service.derive("rin")
        self.assertEqual(result["created"], [])

    async def test_deriving_twice_is_idempotent(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core, age_hours=24)
        first = core.proactive_service.derive("rin")
        second = core.proactive_service.derive("rin")
        self.assertEqual(len(first["created"]), 1)
        self.assertEqual(second["created"], [])
        self.assertEqual(len(second["duplicates"]), 1)
        self.assertEqual(len(core.proactive_service.candidates("rin")), 1)

    async def test_an_ineligible_reflection_is_refused_when_deriving(self):
        core = self.build()
        core.recover("rin")
        core.reflections.save("rin", [
            thin_reflection("ref_thin", "用户可能喜欢咖啡"),
            mature_reflection("ref_ok", "主人面试前会紧张"),
        ])
        result = core.proactive_service.derive("rin")
        origins = [item["origin"] for item in result["refused"]]
        self.assertIn("shared_finding", origins)
        reasons = [item["reason"] for item in result["refused"]]
        self.assertIn(REASON_NOT_MATURE, reasons)
        # The eligible one still became a candidate.
        causes = {row["cause"] for row in core.proactive_service.candidates("rin")}
        self.assertIn("shared_finding", causes)


class UserBoundaryTests(ProactiveBase):
    """G26: pause the topic, restrict the mention, delete for real."""

    def _candidate(self, core, text="说好九点叫你起床。"):
        receipt = core.proactive_service.enqueue(
            "rin", cause="agreement", text=text,
            maturity_source="explicit_agreement",
            source_keys=["host:rin:private:e_agreed"],
        )
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt.candidate_id

    async def test_pausing_a_topic_cancels_its_candidates_and_expires(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._candidate(core)
        paused = core.proactive_service.pause_topic(
            "rin", subject="private", text="起床", minutes=60,
        )
        self.assertEqual(paused["status"], "paused")
        self.assertIn(candidate_id, paused["cancelled"])
        row = core.proactive_service.get("rin", candidate_id)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["cancelled_reason"], "topic_paused")
        # A pause blocks a new candidate while it lasts...
        again = core.proactive_service.enqueue(
            "rin", cause="agreement", text="起床啦。",
            maturity_source="explicit_agreement",
            source_keys=["host:rin:private:e_agreed2"],
        )
        self.assertEqual(again.reason, "topic_paused")
        # ...and expires on its own, without anything being reset by hand.
        self.clock.advance(minutes=61)
        later = core.proactive_service.enqueue(
            "rin", cause="agreement", text="起床啦。",
            maturity_source="explicit_agreement",
            source_keys=["host:rin:private:e_agreed2"],
        )
        self.assertTrue(later.ok, later.reason)
        # A pause is not a deletion: no deletion record was registered.
        self.assertEqual(core.deletion_service.list("rin")["items"], [])

    async def test_a_mention_restriction_uses_the_directive_store(self):
        core = self.build()
        core.recover("rin")
        candidate_id = self._candidate(core)
        result = core.proactive_service.mention_restriction(
            "rin", subject="private", text="起床",
        )
        self.assertTrue(result["ok"], result)
        self.assertIn(candidate_id, result["cancelled"])
        active = core.directives.active("rin")
        self.assertEqual([row["term"] for row in active], ["起床"])
        # The restriction is consulted by the proactive gate itself.
        refused = core.proactive_service.enqueue(
            "rin", cause="agreement", text="起床啦。",
            maturity_source="explicit_agreement",
            source_keys=["host:rin:private:e_agreed2"],
        )
        self.assertEqual(refused.reason, "mention_restricted")

    async def test_forgetting_is_a_real_deletion(self):
        from companion_memory.contracts import DeletionRequest, DeletionTarget, Scope

        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        derive = core.proactive_service.derive("rin")
        self.assertEqual(len(derive["created"]), 1)
        candidate_id = derive["created"][0]
        receipt = await core.deletion_service.forget(DeletionRequest(
            scope=Scope("rin", "private"), mode="entry",
            targets=(DeletionTarget("fact", "fact_interview"),),
            source_keys=("host:rin:private:e_interview",),
            reason="user_request",
        ))
        self.assertIn(receipt.status, ("completed", "pending"), receipt)
        row = core.proactive_service.get("rin", candidate_id)
        self.assertEqual(row["status"], "invalidated")
        self.assertEqual(row["text"], "")
        # ...and the fact is really gone. Silence is not what made this happen.
        self.assertEqual(core.facts.count("rin"), 0)


class MaintenanceWiringTests(ProactiveBase):
    """Deriving runs in the background pass, and only registers reasons."""

    async def test_the_maintenance_pass_derives_candidates(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core, age_hours=24)
        result = await core.maintenance.run_once("rin")
        step = result.steps["proactive_candidates"]
        self.assertEqual(step["status"], "ok", step)
        self.assertIn("created=1", step["detail"])
        self.assertEqual(len(core.proactive_service.candidates("rin")), 1)
        # Deriving never generates a body: nothing was said, so nothing exists
        # to deliver, and the shared conversation is untouched.
        self.assertEqual(core.recent.count("rin"), 0)
        self.assertEqual(core.output_service.list("rin"), [])

    async def test_a_second_pass_derives_nothing_new(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core, age_hours=24)
        await core.maintenance.run_once("rin")
        second = await core.maintenance.run_once("rin")
        step = second.steps["proactive_candidates"]
        self.assertIn("created=0", step["detail"])
        self.assertIn("duplicates=1", step["detail"])
        self.assertEqual(len(core.proactive_service.candidates("rin")), 1)

    async def test_the_step_can_be_disabled(self):
        from companion_memory.adapters.composition import build_memory_core
        from companion_memory.domain.maintenance import MaintenanceSettings

        core = build_memory_core(
            memory_root=self.root / "off",
            clock=self.clock,
            maintenance_settings=MaintenanceSettings(enable_proactive=False),
        )
        core.recover("rin")
        self.seed_thread(core, age_hours=24)
        result = await core.maintenance.run_once("rin")
        self.assertEqual(result.steps["proactive_candidates"]["status"], "skipped")
        self.assertEqual(core.proactive_service.candidates("rin"), [])


class NoSilenceProgressionTests(ProactiveBase):
    """Design 11.2: time passing and no reply never advance anything."""

    async def test_time_and_silence_change_nothing_about_the_thread(self):
        core = self.build()
        core.recover("rin")
        self.seed_thread(core)
        before = core.state_service.read("rin", subject="private")
        thread_id = str(before.state["progress"][0]["id"])
        receipt = core.proactive_service.enqueue(
            "rin", cause="thread_due", text="面试怎么样了？",
            maturity_source="task_result", thread_id=thread_id,
            source_keys=["host:rin:private:e_interview"],
        )
        self.assertTrue(receipt.ok, receipt.reason)
        sent = core.proactive_service.send("rin", receipt.candidate_id)
        self.assertTrue(sent.ok, sent.reason)
        core.proactive_service.deliver("rin", receipt.candidate_id)
        state_after = core.state_service.read("rin", subject="private")
        self.assertEqual(state_after.version, before.version)
        self.assertEqual(state_after.state, before.state)
        # A week of silence changes nothing either.
        self.clock.advance(days=7)
        later = core.state_service.read("rin", subject="private")
        self.assertEqual(later.version, before.version)
        self.assertEqual(
            [item.get("text") for item in later.items],
            [item.get("text") for item in before.items],
        )


if __name__ == "__main__":
    unittest.main()
