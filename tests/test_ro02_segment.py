"""RO02-CTX: 上下文分段参数与续聊便条。

只测两件本卡负责的事:
- 分段的七个参数在 stack.toml 里能读、能校验,且不写 [segment] 的旧配置照样能用;
- 续聊便条默认不生成,只在真会断线时出现,并且只引用不结论。

换段本身的判定与状态在 DSH 插件侧(segment.test.js);这里不重复实现。
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rin_memory.domain.segment_note import (
    MAX_ITEMS, MIN_GAP_HOURS, NOTE_CHAR_LIMIT,
    build_segment_note, note_is_stale,
)
from rin_memory.stack_config import SEGMENT_DEFAULTS, StackConfigError, initialize, load_config


NOW = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)


def iso(moment):
    return moment.isoformat()


def thread(text, hours_ago, **extra):
    row = {"text": text, "event_at": iso(NOW - timedelta(hours=hours_ago))}
    row.update(extra)
    return row


class SegmentConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ro02-ctx-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config" / "stack.toml"
        self.config = initialize(self.path)
        self.text = self.path.read_text(encoding="utf-8")

    def rewrite(self, old, new):
        self.assertIn(old, self.text)
        self.path.write_text(self.text.replace(old, new, 1), encoding="utf-8")
        return load_config(self.path)

    def test_template_carries_the_agreed_parameters(self):
        # 卡上定下的七个值必须原样出现在新配置模板里,而不是只存在于代码默认值。
        self.assertIn("[segment]", self.text)
        self.assertEqual(self.config.segment, {
            "budget_tokens": 128000,
            "hard_tokens": 160000,
            "carry_turns": 30,
            "carry_tokens": 8000,
            "idle_hours": 24,
            "min_turns": 6,
            "max_age_days": 14,
        })
        self.assertEqual(self.config.segment, SEGMENT_DEFAULTS)

    def test_existing_config_without_the_table_still_loads(self):
        # 已经部署的配置没有 [segment];升级不能因此起不来,只能回落到默认。
        text = self.text.split("[segment]", 1)[0].rstrip() + "\n"
        self.path.write_text(text, encoding="utf-8")
        self.assertNotIn("[segment]", text)
        self.assertEqual(load_config(self.path).segment, SEGMENT_DEFAULTS)

    def test_unknown_key_is_rejected_instead_of_silently_ignored(self):
        # 拼错的参数键如果被静默忽略,用户以为改了上限、实际没生效。
        with self.assertRaisesRegex(StackConfigError, "unknown key"):
            self.rewrite("budget_tokens = 128000", "budget_token = 128000")
        self.restore()

    def restore(self):
        """把模板写回原样:上一个用例可能故意把它写坏过。"""
        self.path.unlink(missing_ok=True)
        self.config = initialize(self.path)
        self.text = self.path.read_text(encoding="utf-8")

    def test_contradictory_limits_are_rejected(self):
        # 硬上限不高于软上限:软上限永远先到,硬上限形同虚设。
        with self.assertRaisesRegex(StackConfigError, "hard_tokens"):
            self.rewrite("hard_tokens = 160000", "hard_tokens = 100000")
        self.restore()
        # 带入上限不小于软上限:新段第一次请求就自带超限。
        with self.assertRaisesRegex(StackConfigError, "carry_tokens"):
            self.rewrite("carry_tokens = 8000", "carry_tokens = 200000")

    def test_non_positive_and_mistyped_values_are_rejected(self):
        for old, new in [
            ("idle_hours = 24", "idle_hours = 0"),
            ("max_age_days = 14", "max_age_days = -1"),
            ("min_turns = 6", "min_turns = 'six'"),
            ("carry_turns = 30", "carry_turns = true"),
        ]:
            self.restore()
            with self.assertRaises(StackConfigError):
                self.rewrite(old, new)


class SegmentNoteTests(unittest.TestCase):
    def test_default_is_no_note(self):
        # 卡片要求:默认不生成,不新增常规摘要调用。
        self.assertIsNone(build_segment_note([], switched_at=iso(NOW), last_user_at=iso(NOW - timedelta(days=3))))
        # 有未完事项,但刚刚才说过话(同一场对话里换段):原文已带过去,不必便条。
        near = NOW - timedelta(hours=MIN_GAP_HOURS - 1)
        self.assertIsNone(build_segment_note(
            [thread("那台服务器", 30)], switched_at=iso(NOW), last_user_at=iso(near),
        ))

    def test_same_conversation_switch_adds_no_note(self):
        # 连续长聊触发换段:时距短,不该多出一张便条。
        for hours in (0, 1, MIN_GAP_HOURS - 0.5):
            note = build_segment_note(
                [thread("写了一半的脚本", 12)], switched_at=iso(NOW),
                last_user_at=iso(NOW - timedelta(hours=hours)),
            )
            self.assertIsNone(note, hours)

    def test_a_long_gap_with_an_open_thread_yields_one_short_note(self):
        note = build_segment_note(
            [thread("服务器那件事还没修好", 72, id="mem_1")],
            switched_at=iso(NOW), last_user_at=iso(NOW - timedelta(days=3)),
        )
        self.assertIsNotNone(note)
        # 第一人称、带时间:能顺着找回原事,而不是重述结论。
        self.assertIn("2026-10-23 10:00 UTC", note.text)
        self.assertIn("服务器那件事还没修好", note.text)
        self.assertLessEqual(len(note.text), NOTE_CHAR_LIMIT)
        self.assertEqual(note.gap_hours, 72.0)
        # 只引用不结论:refs 里是标识,正文不是摘要。
        self.assertEqual(note.refs[0]["id"], "mem_1")

    def test_note_is_deterministic_and_capped(self):
        threads = [thread("第%d件事" % index, 20 + index, id="m%d" % index) for index in range(6)]
        first = build_segment_note(threads, switched_at=iso(NOW), last_user_at=iso(NOW - timedelta(days=2)))
        second = build_segment_note(threads, switched_at=iso(NOW), last_user_at=iso(NOW - timedelta(days=2)))
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertLessEqual(len(first.refs), MAX_ITEMS)
        self.assertLessEqual(len(first.text), NOTE_CHAR_LIMIT)

    def test_threads_created_after_the_switch_are_not_note_material(self):
        # 换段之后才冒出来的话题本来就在新段里,进便条等于重复。
        note = build_segment_note(
            [thread("换段后才出现的想法", -5, id="later")],
            switched_at=iso(NOW), last_user_at=iso(NOW - timedelta(days=2)),
        )
        self.assertIsNone(note)

    def test_a_deleted_source_invalidates_the_whole_note(self):
        note = build_segment_note(
            [thread("服务器", 72, id="mem_1"), thread("脚本", 60, id="mem_2")],
            switched_at=iso(NOW), last_user_at=iso(NOW - timedelta(days=3)),
        )
        payload = note.as_dict()
        self.assertFalse(note_is_stale(payload, []))
        self.assertFalse(note_is_stale(payload, ["unrelated"]))
        # 任一引用被删就整张作废:不重写便条,否则等于旧结论再生一条。
        self.assertTrue(note_is_stale(payload, ["mem_2"]))

    def test_missing_or_broken_timestamps_degrade_to_no_note(self):
        self.assertIsNone(build_segment_note([thread("x", 72)], switched_at=None, last_user_at=iso(NOW)))
        self.assertIsNone(build_segment_note([thread("x", 72)], switched_at="not-a-time", last_user_at=iso(NOW)))
        # last_user_at 坏掉时按「刚说过」处理,不凭坏数据生成便条。
        self.assertIsNone(build_segment_note(
            [thread("x", 72)], switched_at=iso(NOW), last_user_at="garbage",
        ))


if __name__ == "__main__":
    unittest.main()
