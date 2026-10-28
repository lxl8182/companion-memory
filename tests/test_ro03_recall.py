"""RO03 acceptance: turn context selects memory by relevance, not importance.

The bug this fixes: `working_summary._facts_section()` had no access to the
turn's own words, so it ranked facts by importance alone and a caller had to
take the top three. Every turn therefore inherited the same high-importance
facts -- "主人给了我服务器" showed up in a plain picture turn, and the plugin
renders any fact it has not seen as "[重要事实]".

Contract under test:

* `build_turn_context` receives the turn's text (already forwarded by
  `/v1/context` and `/v1/chat`) and passes it down to the projector;
* ordinary facts are selected by scope + validity + relevance, at most three,
  and **zero** is a legal answer when the turn shares no terms with them;
* a turn with no words (an image, a bare greeting) gets no facts at all --
  never the highest-importance rows;
* asking about the topic does recall it;
* a correction, a retraction and a mention restriction are not subject to the
  three-item limit (dropping one of those causes real harm);
* the administrative summary (`/v1/working-summary`, the audit cache) is
  unchanged: it still reports everything it knows, importance ordered;
* one generation turn reuses one snapshot; the next turn sees the next version.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rin_memory.adapters.composition import build_memory_core
from rin_memory.contracts import CorrectionRequest, DeletionRequest, DeletionTarget, Scope
from rin_memory.domain.extraction import ExtractionSettings

CHARACTER = "demo"


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


def fact(fact_id, text, *, importance=5, keys=None, subject="private"):
    return {
        "id": fact_id,
        "text": text,
        "importance": importance,
        "source": "user_observation",
        "schema_version": 2,
        "absorbed": True,
        "created_at": "2026-10-20T08:00:00+00:00",
        "last_confirmed_at": "2026-10-20T08:00:00+00:00",
        "subject": subject,
        "source_event_keys": keys or [f"host:{CHARACTER}:private:{fact_id}"],
    }


class RecallHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, name="memory"):
        core = build_memory_core(
            memory_root=self.root / name, clock=self.clock,
            extraction_settings=ExtractionSettings(inline=False),
        )
        core.recover(CHARACTER)
        return core

    def seed(self, core):
        """One high-importance operational fact plus a couple of ordinary ones."""
        core.facts.save(CHARACTER, [
            fact("fact_server", "主人把一台服务器交给助手使用", importance=10,
                 keys=[f"host:{CHARACTER}:private:e_server"]),
            fact("fact_cat", "用户的猫叫团子", importance=4,
                 keys=[f"host:{CHARACTER}:private:e_cat"]),
            fact("fact_tea", "用户喜欢乌龙茶", importance=3,
                 keys=[f"host:{CHARACTER}:private:e_tea"]),
        ])

    @staticmethod
    def sections(context):
        return context["working_summary"]["sections"]["important_facts"]["items"]


class ImageAndSmallTalkTurn(RecallHarness):
    """A turn with no usable words must not inherit the important facts."""

    async def test_a_plain_picture_turn_carries_no_facts(self):
        core = self.build()
        self.seed(core)
        for text in ("", "[图片1]", "看看这张图", "在吗"):
            context = await core.recall.build_turn_context(
                CHARACTER, current_input=text,
            )
            self.assertEqual(
                self.sections(context), [],
                f"图片/寒暄轮不得注入与问题无关的事实: {text!r}",
            )
            self.assertEqual(
                context["working_summary"]["sections"]["important_facts"]["block"],
                "",
            )

    async def test_the_server_fact_appears_only_when_asked_about(self):
        core = self.build()
        self.seed(core)
        missed = await core.recall.build_turn_context(
            CHARACTER, current_input="在吗",
        )
        self.assertNotIn("服务器", str(missed["working_summary"]))

        asked = await core.recall.build_turn_context(
            CHARACTER, current_input="服务器怎么部署比较好？",
        )
        items = self.sections(asked)
        self.assertEqual([item["id"] for item in items], ["fact_server"])
        self.assertIn("服务器", asked["working_summary"]["sections"]
                      ["important_facts"]["block"])
        # The reason a row was offered is reported, so a reader can audit it.
        self.assertTrue(items[0]["matched_terms"])

    async def test_an_unrelated_topic_gets_no_facts_even_with_many_stored(self):
        core = self.build()
        core.facts.save(CHARACTER, [
            fact(f"fact_{index}", f"用户提到过第{index}件事", importance=10 - index)
            for index in range(6)
        ])
        context = await core.recall.build_turn_context(
            CHARACTER, current_input="今天天气怎么样",
        )
        self.assertEqual(self.sections(context), [],
                         "相关性不足允许 0 条，不能用重要度填补")


class SelectionLimits(RecallHarness):
    """At most three ordinary facts, relevance first, importance second."""

    async def test_at_most_three_and_relevant_ones_win(self):
        core = self.build()
        core.facts.save(CHARACTER, [
            fact("fact_a", "用户周末在公园跑步", importance=3),
            fact("fact_b", "用户的跑鞋是蓝色", importance=2),
            fact("fact_c", "用户跑步时听播客", importance=1),
            fact("fact_d", "用户跑步会带水", importance=9),
            fact("fact_e", "用户养了一只猫", importance=10),
        ])
        context = await core.recall.build_turn_context(
            CHARACTER, current_input="周末去公园跑步要带什么",
        )
        items = self.sections(context)
        self.assertLessEqual(len(items), 3)
        ids = {item["id"] for item in items}
        self.assertNotIn("fact_e", ids, "无关的高重要度事实不得挤进上下文")
        self.assertTrue(ids & {"fact_a", "fact_b", "fact_c", "fact_d"})

    async def test_the_seed_closes_over_scope_and_validity(self):
        core = self.build()
        self.seed(core)
        core.facts.save(CHARACTER, core.facts.load(CHARACTER) + [
            fact("fact_group", "群里有人提到服务器", subject="group"),
        ])
        context = await core.recall.build_turn_context(
            CHARACTER, current_input="服务器怎么样", subject="private",
        )
        ids = {item["id"] for item in self.sections(context)}
        self.assertIn("fact_server", ids)
        self.assertNotIn("fact_group", ids, "别的范围的事实不得进入本轮上下文")


class ConstraintsOutrankTheLimit(RecallHarness):
    """A correction, retraction or mention restriction is never budgeted out."""

    async def test_corrections_and_restrictions_survive_the_limit(self):
        core = self.build()
        self.seed(core)
        await core.correction_service.correct(CorrectionRequest(
            scope=Scope(CHARACTER), old_text="乌龙茶", new_text="用户现在喜欢绿茶",
        ))
        context = await core.recall.build_turn_context(
            CHARACTER, current_input="今天随便聊聊",
        )
        # Two facts share nothing with the message, so the fact list is empty --
        # but the correction is still reported, because it is a constraint.
        self.assertEqual(self.sections(context), [])
        self.assertIn("绿茶", context["corrections"]["block"])

    async def test_a_deleted_fact_never_comes_back_for_a_matching_turn(self):
        core = self.build()
        self.seed(core)
        await core.deletion_service.forget(DeletionRequest(
            scope=Scope(CHARACTER),
            targets=(DeletionTarget("fact", "fact_server"),),
        ))
        context = await core.recall.build_turn_context(
            CHARACTER, current_input="服务器还在用吗",
        )
        self.assertEqual(self.sections(context), [],
                         "删除后即使用户问到也不能召回")


class AdministrativeViewIsUnchanged(RecallHarness):
    """The audit/admin projection still reports everything, importance ordered."""

    async def test_admin_summary_keeps_every_fact(self):
        core = self.build()
        self.seed(core)
        summary = core.working_summary_service.build(CHARACTER)
        items = summary.section("important_facts").items
        self.assertEqual(len(items), 3, "管理视图不受本轮 3 条上限影响")
        self.assertEqual(items[0]["text"], "主人把一台服务器交给助手使用")
        # And the HTTP view keeps its old shape.
        self.assertEqual(
            summary.section("important_facts").block.splitlines()[0], "重要事实",
        )


class SnapshotPerTurn(RecallHarness):
    """One generation turn sees one version; a new turn sees the new one."""

    async def test_two_reads_in_one_turn_agree_and_a_new_turn_sees_more(self):
        core = self.build()
        self.seed(core)
        first = await core.recall.build_turn_context(
            CHARACTER, current_input="服务器怎么样",
        )
        second = await core.recall.build_turn_context(
            CHARACTER, current_input="服务器怎么样",
        )
        self.assertEqual(self.sections(first), self.sections(second),
                         "同一生成轮次的两次读取必须一致")

        core.facts.save(CHARACTER, core.facts.load(CHARACTER) + [
            fact("fact_new", "服务器换成了新的机架", importance=6),
        ])
        third = await core.recall.build_turn_context(
            CHARACTER, current_input="服务器怎么样",
        )
        self.assertNotEqual(
            [item["id"] for item in self.sections(first)],
            [item["id"] for item in self.sections(third)],
            "新一轮才看到新版本",
        )
        self.assertIn("fact_new", {item["id"] for item in self.sections(third)})

    async def test_the_summary_is_not_cached_across_turns(self):
        core = self.build()
        self.seed(core)
        empty = await core.recall.build_turn_context(CHARACTER, current_input="在吗")
        self.assertEqual(self.sections(empty), [])
        # The same core, a different question: nothing from the previous turn
        # (and no process-level cache) may be reused.
        asked = await core.recall.build_turn_context(
            CHARACTER, current_input="团子今天怎么样",
        )
        self.assertEqual([item["id"] for item in self.sections(asked)], ["fact_cat"])


class InterestLayerIsReported(RecallHarness):
    """Interest evidence rides along as data, without becoming page prose."""

    async def test_context_reports_interest_material_and_version(self):
        core = self.build()
        self.seed(core)
        context = await core.recall.build_turn_context(
            CHARACTER, current_input="在吗",
        )
        signals = context["interest_signals"]
        self.assertEqual(
            set(signals),
            {"version", "items", "stable_interests", "recent_interests",
             "avoid_topics"},
        )
        self.assertEqual(signals["version"], 0)
        self.assertEqual(signals["items"], [])


if __name__ == "__main__":
    unittest.main()
