"""B03 mention-restriction persistence: `user_directives.json`."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion_memory.errors import (
    CorruptStoreError, InvalidSchemaError, WriteRejected,
)
from companion_memory.persistence import (
    FileSystemAtomicStore, FileSystemRoots, NullWritePermit,
    RejectingWritePermit,
)
from companion_memory.persistence.directives import (
    DEFAULT_TTL_MAX_SECONDS, DEFAULT_TTL_SECONDS, DIRECTIVES_FILENAME,
    DIRECTIVES_SCHEMA, UserDirectiveStore,
)


class FixedClock:
    """Clock the test moves by hand so every timestamp is exact."""

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def set(self, when: datetime) -> None:
        self._now = when


def _build(tmp, clock, permit, **kwargs) -> UserDirectiveStore:
    return UserDirectiveStore(
        roots=FileSystemRoots(tmp),
        store=FileSystemAtomicStore(),
        clock=clock,
        permit=permit,
        **kwargs,
    )


def _valid_entry(**overrides):
    entry = {
        "term": "工作",
        "kind": "ban_topic",
        "locale": "zh",
        "created_at": 1000.0,
        "last_seen_at": 2000.0,
        "expire_at": 9_999_999_999.0,
        "hit_count": 1,
        "source": "regex",
    }
    entry.update(overrides)
    return entry


class UserDirectiveStoreTests(unittest.TestCase):
    def test_host_shape_round_trip_and_strict_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = FixedClock()
            store = _build(tmp, clock, NullWritePermit())
            path = Path(tmp) / "neko" / DIRECTIVES_FILENAME

            # -- a missing file is a fresh store and creates nothing
            self.assertEqual(store.active("neko"), [])
            self.assertFalse((Path(tmp) / "neko").exists())

            # -- the written document is the host envelope, field by field
            now = clock.now().timestamp()
            written = store.record(
                "neko", term="  前女友  ", kind="ban_topic", locale="zh",
            )
            self.assertEqual(written["term"], "前女友")
            self.assertEqual(written["kind"], "ban_topic")
            self.assertEqual(written["locale"], "zh")
            self.assertEqual(written["source"], "regex")
            self.assertEqual(written["hit_count"], 1)
            self.assertEqual(written["created_at"], now)
            self.assertEqual(written["last_seen_at"], now)
            self.assertEqual(written["expire_at"], now + DEFAULT_TTL_SECONDS)

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(raw), {"version", "directives"})
            self.assertEqual(raw["version"], DIRECTIVES_SCHEMA)
            self.assertIsInstance(raw["directives"], list)
            self.assertEqual(len(raw["directives"]), 1)
            disk_entry = raw["directives"][0]
            self.assertEqual(
                set(disk_entry),
                {
                    "term", "kind", "locale", "created_at", "last_seen_at",
                    "expire_at", "hit_count", "source",
                },
            )
            self.assertIsInstance(disk_entry["created_at"], float)
            self.assertIsInstance(disk_entry["last_seen_at"], float)
            self.assertIsInstance(disk_entry["expire_at"], float)
            self.assertIsInstance(disk_entry["hit_count"], int)

            # -- reopening the same root reads the same row
            reopened = _build(tmp, clock, NullWritePermit())
            self.assertEqual(reopened.active("neko"), [written])

            # -- host rows with unknown top-level / entry fields round-trip
            host_payload = {
                "version": 1,
                "directives": [
                    _valid_entry(host_extra={"keep": [1, 2]}, locale="zh"),
                ],
                "host_note": {"origin": "N.E.K.O"},
            }
            path.write_text(
                json.dumps(host_payload, ensure_ascii=False), encoding="utf-8",
            )
            active = reopened.active("neko")
            self.assertEqual(active[0]["host_extra"], {"keep": [1, 2]})
            refreshed = reopened.record(
                "neko", term="工作", kind="ban_topic", locale="en",
                source="manual",
            )
            # A hit keeps the first spelling, locale and source.
            self.assertEqual(refreshed["term"], "工作")
            self.assertEqual(refreshed["locale"], "zh")
            self.assertEqual(refreshed["source"], "regex")
            self.assertEqual(refreshed["hit_count"], 2)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["host_note"], {"origin": "N.E.K.O"})
            self.assertEqual(
                raw["directives"][0]["host_extra"], {"keep": [1, 2]},
            )

            # -- clear() persists the empty list and keeps unknown keys
            reopened.clear("neko")
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], DIRECTIVES_SCHEMA)
            self.assertEqual(raw["directives"], [])
            self.assertEqual(raw["host_note"], {"origin": "N.E.K.O"})
            self.assertEqual(reopened.active("neko"), [])

            # -- damage fails closed, byte-for-byte, on every entry point
            damaged = [
                ("{not json", CorruptStoreError),
                (json.dumps([1, 2]), CorruptStoreError),
                (json.dumps({"version": 1}), CorruptStoreError),
                (
                    json.dumps({"version": 1, "directives": "nope"}),
                    CorruptStoreError,
                ),
                (
                    json.dumps({
                        "version": 1,
                        "directives": [{"term": "工作"}],
                    }),
                    CorruptStoreError,
                ),
                (
                    json.dumps({
                        "version": 1,
                        "directives": [_valid_entry(hit_count=0)],
                    }),
                    CorruptStoreError,
                ),
                (
                    json.dumps({
                        "version": 1,
                        "directives": [_valid_entry(term="x")],
                    }),
                    CorruptStoreError,
                ),
                (
                    json.dumps({
                        "version": 1,
                        "directives": [_valid_entry(created_at="abc")],
                    }),
                    CorruptStoreError,
                ),
                (
                    json.dumps({
                        "version": 1,
                        "directives": [_valid_entry(kind=1)],
                    }),
                    CorruptStoreError,
                ),
                (
                    json.dumps({
                        "version": 1,
                        "directives": [_valid_entry(), "not an object"],
                    }),
                    CorruptStoreError,
                ),
                (
                    json.dumps({"version": True, "directives": []}),
                    InvalidSchemaError,
                ),
                (
                    json.dumps({"version": "1", "directives": []}),
                    InvalidSchemaError,
                ),
                (
                    json.dumps({"version": 99, "directives": []}),
                    InvalidSchemaError,
                ),
                (json.dumps({"directives": []}), InvalidSchemaError),
            ]
            for text, error in damaged:
                path.write_text(text, encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(error):
                    reopened.active("neko")
                with self.assertRaises(error):
                    reopened.record(
                        "neko", term="新话题", kind="ban_topic", locale="zh",
                    )
                with self.assertRaises(error):
                    reopened.clear("neko")
                with self.assertRaises(error):
                    reopened.purge_expired("neko")
                self.assertEqual(path.read_bytes(), before)

    def test_dedup_renewal_ttl_expiry_and_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = FixedClock()
            store = _build(tmp, clock, NullWritePermit())
            path = Path(tmp) / "neko" / DIRECTIVES_FILENAME
            t0 = clock.now().timestamp()

            # -- first hit: base TTL
            first = store.record(
                "neko", term="Work", kind="ban_topic", locale="zh",
            )
            self.assertEqual(first["hit_count"], 1)
            self.assertEqual(first["expire_at"], t0 + DEFAULT_TTL_SECONDS)

            # -- a case-folded repeat renews the row instead of adding one
            clock.advance(86400)
            t1 = clock.now().timestamp()
            again = store.record(
                "neko", term="work", kind="ban_topic", locale="en",
                source="manual",
            )
            self.assertEqual(len(store.active("neko")), 1)
            self.assertEqual(again["term"], "Work")
            self.assertEqual(again["locale"], "zh")
            self.assertEqual(again["source"], "regex")
            self.assertEqual(again["created_at"], t0)
            self.assertEqual(again["last_seen_at"], t1)
            self.assertEqual(again["hit_count"], 2)
            self.assertEqual(
                again["expire_at"],
                t1 + min(DEFAULT_TTL_SECONDS * 2, DEFAULT_TTL_MAX_SECONDS),
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(raw["directives"]), 1)

            # -- an explicit TTL overrides the repeat window, bounded to the max
            clock.advance(10)
            t2 = clock.now().timestamp()
            manual = store.record(
                "neko", term="手动话题", kind="ban_topic", locale="zh",
                ttl_seconds=3600,
            )
            self.assertEqual(manual["expire_at"], t2 + 3600)
            for bad_ttl in (0, -1, DEFAULT_TTL_MAX_SECONDS + 1, "abc", True):
                with self.assertRaises(ValueError):
                    store.record(
                        "neko", term="坏期限", kind="ban_topic", locale="zh",
                        ttl_seconds=bad_ttl,
                    )
            for bad_term in ("", " x ", "x", "x" * 41, 123):
                with self.assertRaises(ValueError):
                    store.record(
                        "neko", term=bad_term, kind="ban_topic", locale="zh",
                    )

            # -- the TTL ceiling caps the repeat-scaled window
            capped = _build(
                tmp, clock, NullWritePermit(),
                ttl_seconds=10, ttl_max_seconds=25,
            )
            clock.set(datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc))
            entry = capped.record(
                "cap", term="重复话题", kind="ban_topic", locale="zh",
            )
            for _ in range(4):
                entry = capped.record(
                    "cap", term="重复话题", kind="ban_topic", locale="zh",
                )
            self.assertEqual(entry["hit_count"], 5)
            self.assertEqual(
                entry["expire_at"], clock.now().timestamp() + 25,
            )

            # -- expiry filters reads; purge deletes and persists
            clock.advance(3601)
            self.assertEqual(
                [item["term"] for item in store.active("neko")], ["Work"],
            )
            self.assertEqual(store.purge_expired("neko"), 1)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["term"] for item in raw["directives"]], ["Work"],
            )
            self.assertEqual(store.purge_expired("neko"), 0)

            # -- over max_stored, expired rows go first, then the oldest seen
            expiry_rot = _build(
                tmp, clock, NullWritePermit(), max_stored=2,
            )
            clock.set(datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc))
            expiry_rot.record(
                "er", term="过期话题", kind="ban_topic", locale="zh",
                ttl_seconds=60,
            )
            clock.advance(120)
            expiry_rot.record(
                "er", term="活跃一", kind="ban_topic", locale="zh",
            )
            clock.advance(60)
            expiry_rot.record(
                "er", term="活跃二", kind="ban_topic", locale="zh",
            )
            er_raw = json.loads(
                (Path(tmp) / "er" / DIRECTIVES_FILENAME).read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual(
                {item["term"] for item in er_raw["directives"]},
                {"活跃一", "活跃二"},
            )

            # -- rotation keeps the newest last_seen rows
            rotating = _build(tmp, clock, NullWritePermit(), max_stored=3)
            clock.set(datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc))
            for index in range(3):
                rotating.record(
                    "rot", term=f"话题{index}", kind="ban_topic", locale="zh",
                )
                clock.advance(60)
            rotating.record(
                "rot", term="话题3", kind="ban_topic", locale="zh",
            )
            stored = [
                item["term"] for item in rotating.active("rot", limit=10)
            ]
            self.assertEqual(set(stored), {"话题1", "话题2", "话题3"})
            self.assertEqual(len(stored), 3)

            # -- a row evicted by rotation is reported as not recorded
            rollback = _build(tmp, clock, NullWritePermit(), max_stored=2)
            clock.set(datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc))
            rollback.record(
                "rb", term="旧话题", kind="ban_topic", locale="zh",
            )
            clock.advance(60)
            rollback.record(
                "rb", term="新话题", kind="ban_topic", locale="zh",
            )
            clock.set(datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc))
            self.assertEqual(
                rollback.record(
                    "rb", term="回拨话题", kind="ban_topic", locale="zh",
                ),
                {},
            )
            self.assertEqual(
                [item["term"] for item in rollback.active("rb")],
                ["新话题", "旧话题"],
            )

    def test_write_permit_render_block_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = FixedClock()
            roots = FileSystemRoots(tmp)
            rejecting = UserDirectiveStore(
                roots=roots, store=FileSystemAtomicStore(), clock=clock,
                permit=RejectingWritePermit(),
            )

            # -- a rejecting permit refuses every write, before any file exists
            with self.assertRaises(WriteRejected):
                rejecting.record(
                    "denied", term="工作", kind="ban_topic", locale="zh",
                )
            with self.assertRaises(WriteRejected):
                rejecting.record(
                    "denied", term="工作", kind="ban_topic", locale="zh",
                    ttl_seconds=60,
                )
            with self.assertRaises(WriteRejected):
                rejecting.clear("denied")
            self.assertFalse((Path(tmp) / "denied").exists())
            self.assertEqual(rejecting.purge_expired("denied"), 0)

            # -- render_block: empty store renders nothing
            store = _build(tmp, clock, NullWritePermit())
            self.assertEqual(store.render_block("neko"), "")

            store.record("neko", term="前女友", kind="ban_topic", locale="zh")
            clock.advance(1)
            store.record("neko", term="加班", kind="ban_topic", locale="zh")
            zh = store.render_block("neko", "zh")
            self.assertTrue(zh.startswith("["))
            self.assertIn("- 加班", zh)
            self.assertIn("- 前女友", zh)
            # Newest last_seen first.
            self.assertLess(zh.index("- 加班"), zh.index("- 前女友"))

            # -- every supported language renders, unknown tags fall back to en
            for language in ("zh-TW", "en", "ja", "ko", "ru", "es", "pt"):
                block = store.render_block("neko", language)
                self.assertTrue(block, language)
                self.assertIn("- 前女友", block, language)
            self.assertNotEqual(
                store.render_block("neko", "zh-TW"),
                store.render_block("neko", "zh"),
            )
            self.assertEqual(
                store.render_block("neko", "xx-unknown"),
                store.render_block("neko", "en"),
            )
            self.assertEqual(
                store.render_block("neko", "en-US"),
                store.render_block("neko", "en"),
            )
            self.assertEqual(
                store.render_block("neko", "pt_BR"),
                store.render_block("neko", "pt"),
            )

            # -- the estimated character budget truncates whole bullet lines
            self.assertEqual(store.render_block("neko", "zh", 1), "")
            for bad_limit in (0, -5, True):
                with self.assertRaises(ValueError):
                    store.render_block("neko", "zh", bad_limit)

            long_store = _build(tmp, clock, NullWritePermit())
            for index in range(20):
                long_store.record(
                    "long", term=f"话题{index:02d}" + "字" * 35,
                    kind="ban_topic", locale="zh",
                )
            block = long_store.render_block("long", "en", 200)
            self.assertTrue(block)
            self.assertLessEqual(len(block), 200 * 4)
            self.assertLess(block.count("\n- "), 20)
            self.assertGreaterEqual(block.count("\n- "), 1)

            # -- clear empties the file; a rejecting permit cannot clear it
            store.clear("neko")
            self.assertEqual(store.active("neko"), [])
            path = Path(tmp) / "neko" / DIRECTIVES_FILENAME
            cleared = path.read_bytes()
            self.assertEqual(
                json.loads(cleared.decode("utf-8")),
                {"version": DIRECTIVES_SCHEMA, "directives": []},
            )
            rejecting_on_existing = UserDirectiveStore(
                roots=roots, store=FileSystemAtomicStore(), clock=clock,
                permit=RejectingWritePermit(),
            )
            with self.assertRaises(WriteRejected):
                rejecting_on_existing.clear("neko")
            self.assertEqual(path.read_bytes(), cleared)


if __name__ == "__main__":
    unittest.main()
