"""B04 recent.json store: host shape, append/replace/compression, deletion cleanup."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from companion_memory.errors import CorruptStoreError, WriteRejected
from companion_memory.persistence import (
    FileSystemAtomicStore, FileSystemRoots, NullWritePermit, RejectingWritePermit,
)
from companion_memory.persistence.recent import (
    RECENT_FILENAME,
    RecentStore,
    is_summary_message,
    message_episode_ids,
    message_source_keys,
    message_text,
)


class FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


def _store(root, *, permit=None, write_guard=None):
    return RecentStore(
        roots=FileSystemRoots(root),
        store=FileSystemAtomicStore(),
        clock=FixedClock(),
        permit=NullWritePermit() if permit is None else permit,
        write_guard=write_guard,
    )


def _recent_path(root):
    return Path(root) / "neko" / RECENT_FILENAME


class RecentHostShapeTests(unittest.TestCase):
    def test_host_shape_round_trip_and_corrupt_files_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            path = _recent_path(tmp)
            messages = [
                {
                    "type": "human",
                    "data": {
                        "content": "第二层遇到精英",
                        "additional_kwargs": {"unknown_kw": [1, {"deep": True}]},
                        "metadata": {
                            "episode_id": "ep:host:msg_episode",
                            "companion_event": {
                                "source_key": "host:neko:private:msg_episode",
                                "nested_unknown": {"keep": [1, 2]},
                            },
                        },
                    },
                },
                {
                    "type": "ai",
                    "data": {
                        "content": [
                            {"type": "text", "text": "先清"},
                            {"type": "image_url", "image_url": {"url": "ignored"}},
                            {"type": "text", "text": "小怪"},
                        ],
                    },
                    "unknown_top_level": "keep me",
                },
                {"type": "system", "data": {"content": "先前对话的备忘录: 用户喜欢猫。"}},
                {"type": "human", "data": {"content": "没有 metadata 的最小形状"}},
            ]
            self.assertEqual(store.append("neko", messages), len(messages))
            self.assertEqual(store.load("neko"), messages)
            self.assertEqual(store.messages("neko"), messages)
            self.assertEqual(store.count("neko"), len(messages))

            # On-disk shape is the host's bare array: indent=2, no ASCII escapes.
            raw_text = path.read_text(encoding="utf-8")
            self.assertTrue(raw_text.lstrip().startswith("["))
            self.assertIn("\n  ", raw_text)
            self.assertIn("用户喜欢猫", raw_text)
            self.assertNotIn("\\u", raw_text)
            self.assertEqual(json.loads(raw_text), messages)

            # No cache: a fresh instance over the same root reads the same bytes.
            self.assertEqual(_store(tmp).load("neko"), messages)

            # Message helpers read the host metadata/content shapes.
            self.assertEqual(message_text(messages[1]), "先清小怪")
            self.assertTrue(is_summary_message(messages[2]))
            self.assertFalse(is_summary_message(messages[0]))
            self.assertEqual(
                message_source_keys(messages[0]),
                {"host:neko:private:msg_episode"},
            )
            self.assertEqual(message_episode_ids(messages[0]), {"ep:host:msg_episode"})

            # A historical empty file and an empty array both read as [].
            path.write_text("", encoding="utf-8")
            self.assertEqual(store.load("neko"), [])
            self.assertEqual(store.count("neko"), 0)
            store.replace("neko", [])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])

            # Corrupt content is refused and left byte-for-byte untouched.
            for corrupt in (
                "{not json",
                json.dumps({"not": "an array"}),
                json.dumps([1, 2]),
            ):
                path.write_text(corrupt, encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(CorruptStoreError):
                    store.load("neko")
                self.assertEqual(path.read_bytes(), before)


class RecentWriteTests(unittest.TestCase):
    def test_append_replace_compression_anchor_and_write_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            path = _recent_path(tmp)
            m1 = {"type": "human", "data": {"content": "第一句"}}
            m2 = {"type": "ai", "data": {"content": "第二句"}}
            m3 = {"type": "human", "data": {"content": "第三句"}}
            m4 = {"type": "ai", "data": {"content": "第四句"}}
            memo = {"type": "system", "data": {"content": "先前对话的备忘录: 测试。"}}

            # Append order is array order; the return value is the new total.
            self.assertEqual(store.append("neko", [m1]), 1)
            self.assertEqual(store.append("neko", [m2, m3, m4]), 4)
            self.assertEqual(store.load("neko"), [m1, m2, m3, m4])
            store.replace("neko", [m1, m2, m3, m4])
            self.assertEqual(store.count("neko"), 4)

            # A matching snapshot is replaced by [memo] + the untouched tail.
            self.assertTrue(
                store.apply_compression("neko", snapshot=[m1, m2], summary=memo),
            )
            self.assertEqual(store.load("neko"), [memo, m3, m4])

            # A moved anchor is moot: nothing is written.
            before = path.read_bytes()
            self.assertFalse(
                store.apply_compression("neko", snapshot=[m3, m4], summary=memo),
            )
            self.assertFalse(
                store.apply_compression(
                    "neko",
                    snapshot=[memo, {"type": "ai", "data": {"content": "改过"}}],
                    summary=memo,
                ),
            )
            self.assertFalse(
                store.apply_compression("neko", snapshot=[], summary=memo),
            )
            self.assertEqual(path.read_bytes(), before)

            # The write guard runs in the same critical section before the
            # replace; raising leaves the file exactly as it was.
            class _RejectingGuard:
                def __init__(self) -> None:
                    self.calls = []

                def __call__(self, character, messages):
                    self.calls.append((character, [dict(m) for m in messages]))
                    raise RuntimeError("guard rejected")

            guard = _RejectingGuard()
            guarded = _store(tmp, write_guard=guard)
            with self.assertRaises(RuntimeError):
                guarded.append("neko", [m4])
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(RuntimeError):
                guarded.apply_compression("neko", snapshot=[memo, m3], summary=memo)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(guard.calls[0][0], "neko")
            self.assertEqual(guard.calls[0][1], [memo, m3, m4, m4])
            self.assertEqual(guard.calls[1][1], [memo, m4])


class RecentDeletionTests(unittest.TestCase):
    def test_episode_deletion_summary_filtering_and_rejecting_permit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            path = _recent_path(tmp)

            summary_episode = {
                "type": "system",
                "data": {
                    "content": "先前对话的备忘录: 精英战斗摘要。",
                    "metadata": {"episode_id": "ep1"},
                },
            }
            summary_text = {
                "type": "system",
                "data": {"content": "备忘录: 用户喜欢猫。秘密文本A在这里。用户住在东京。"},
            }
            summary_whole = {"type": "system", "data": {"content": "秘密文本B"}}
            summary_keep = {"type": "system", "data": {"content": "用户喜欢绿色。"}}
            human_episode = {
                "type": "human",
                "data": {
                    "content": "第二层遇到精英",
                    "metadata": {
                        "episode_id": "ep1",
                        "companion_event": {"source_key": "sk1"},
                    },
                },
            }
            ai_episode = {
                "type": "ai",
                "data": {
                    "content": "先清小怪",
                    "metadata": {
                        "episode_ids": ["ep1"],
                        "companion_event": {"episode_id": "ep1"},
                    },
                },
            }
            human_source_key = {
                "type": "human",
                "data": {"content": "另一段对话"},
                "metadata": {"mirror": {"event": {"source_key": "sk2"}}},
            }
            human_keep = {
                "type": "human",
                "data": {"content": "无关的日常聊天", "unknown": True},
            }
            history = [
                summary_episode,
                summary_text,
                summary_whole,
                summary_keep,
                human_episode,
                ai_episode,
                human_source_key,
                human_keep,
            ]
            store.append("neko", history)

            counts = store.delete_episode(
                "neko",
                episode_ids=["ep1"],
                source_keys=["sk2"],
                match_texts=["秘密文本A", "秘密文本B"],
            )
            self.assertEqual(
                counts,
                {
                    "messages_removed": 3,
                    "summaries_cleaned": 1,
                    "summaries_removed": 2,
                },
            )
            self.assertEqual(
                store.load("neko"),
                [
                    {
                        "type": "system",
                        "data": {"content": "备忘录: 用户喜欢猫。用户住在东京。"},
                    },
                    summary_keep,
                    human_keep,
                ],
            )

            # Entry-scope filtering changes summaries only: originals stay.
            filter_history = [
                {
                    "type": "system",
                    "data": {"content": "先前对话的备忘录: 用户喜欢猫。秘密文本A在这里。"},
                },
                {
                    "type": "system",
                    "data": {
                        "content": "另一段摘要。",
                        "metadata": {
                            "companion_event": {"source_key": "sk9"},
                        },
                    },
                },
                {"type": "system", "data": {"content": "无关摘要。"}},
                {"type": "human", "data": {"content": "秘密文本A在这里。"}},
                {"type": "ai", "data": {"content": "原样保留"}},
            ]
            store.replace("neko", filter_history)
            counts = store.filter_summaries(
                "neko", match_texts=["秘密文本A"], deleted_source_keys=["sk9"],
            )
            self.assertEqual(
                counts, {"summaries_cleaned": 1, "summaries_removed": 1},
            )
            self.assertEqual(
                store.load("neko"),
                [
                    {
                        "type": "system",
                        "data": {"content": "先前对话的备忘录: 用户喜欢猫。"},
                    },
                    {"type": "system", "data": {"content": "无关摘要。"}},
                    {"type": "human", "data": {"content": "秘密文本A在这里。"}},
                    {"type": "ai", "data": {"content": "原样保留"}},
                ],
            )

            # Nothing to clean is a no-op that does not rewrite the file.
            before = path.read_bytes()
            self.assertEqual(
                store.filter_summaries("neko", match_texts=[]),
                {"summaries_cleaned": 0, "summaries_removed": 0},
            )
            self.assertEqual(path.read_bytes(), before)

            # A rejecting permit leaves a fresh character without any file.
            rejecting = _store(tmp, permit=RejectingWritePermit())
            with self.assertRaises(WriteRejected):
                rejecting.append(
                    "rejected", [{"type": "human", "data": {"content": "x"}}],
                )
            self.assertFalse((Path(tmp) / "rejected").exists())


if __name__ == "__main__":
    unittest.main()
