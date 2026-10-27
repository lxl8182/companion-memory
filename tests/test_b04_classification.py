"""B04 content classification and gate reads (spec sections 2, 5.2 and 6)."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rin_memory.classification import (
    CONTENT_CLASSES,
    HOST_MEMORY_CLASSES,
    PLUGIN_ONLY_CLASSES,
    UNCLASSIFIED,
    classify_message,
    filter_messages_for_extraction,
    is_extractable,
    message_source_keys,
    metadata_of,
)
from rin_memory.persistence.gates import GateState, SuppressionGate

META_KEYS = {
    "gate_mode",
    "skipped_classes",
    "legacy_skipped",
    "legacy_barrier",
    "plugin_id",
    "kept",
    "deleted_skipped",
}


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class B04ClassificationTests(unittest.TestCase):
    def test_classification_priority_matrix_and_class_routing(self) -> None:
        # -- nine classes, split into four host and five plugin-only ones
        self.assertEqual(len(CONTENT_CLASSES), 9)
        self.assertEqual(len(HOST_MEMORY_CLASSES), 4)
        self.assertEqual(len(PLUGIN_ONLY_CLASSES), 5)
        self.assertEqual(HOST_MEMORY_CLASSES | PLUGIN_ONLY_CLASSES, CONTENT_CLASSES)
        self.assertFalse(HOST_MEMORY_CLASSES & PLUGIN_ONLY_CLASSES)
        self.assertNotIn(UNCLASSIFIED, CONTENT_CLASSES)

        # -- metadata_of accepts the persisted and in-memory shapes
        self.assertEqual(
            metadata_of({"data": {"metadata": {"source": "nested"}}}),
            {"source": "nested"},
        )
        self.assertEqual(
            metadata_of({"metadata": {"source": "direct"}}),
            {"source": "direct"},
        )
        # The direct key wins when both shapes are present (host precedence).
        self.assertEqual(
            metadata_of({
                "metadata": {"source": "direct"},
                "data": {"metadata": {"source": "nested"}},
            }),
            {"source": "direct"},
        )
        self.assertEqual(metadata_of({"data": {"metadata": None}}), {})
        self.assertEqual(metadata_of({}), {})

        # -- every explicit legal class is adopted over mirror/source hints
        for content_class in CONTENT_CLASSES:
            self.assertEqual(
                classify_message({
                    "metadata": {
                        "companion_event": {"content_class": content_class},
                        "mirror": {"kind": "sts2"},
                        "source": "sts2_companion",
                    },
                }),
                content_class,
            )

        # -- an illegal explicit class falls through to the later rules
        self.assertEqual(
            classify_message({
                "metadata": {
                    "companion_event": {"content_class": "bogus"},
                    "mirror": {"kind": "sts2"},
                },
            }),
            "game_stream",
        )
        self.assertEqual(
            classify_message({
                "metadata": {"companion_event": {"content_class": "bogus"}},
            }),
            UNCLASSIFIED,
        )

        # -- mirror.kind: sts2 is a stream, other nonempty kinds are output
        self.assertEqual(
            classify_message({"metadata": {"mirror": {"kind": "sts2"}}}),
            "game_stream",
        )
        self.assertEqual(
            classify_message({"metadata": {"mirror": {"kind": "controller"}}}),
            "companion_output",
        )
        for empty_kind in ("", None, 7):
            self.assertEqual(
                classify_message({"metadata": {"mirror": {"kind": empty_kind}}}),
                UNCLASSIFIED,
            )

        # -- metadata.source rules
        for source in ("sts2_companion", "sts2_autoplay"):
            self.assertEqual(
                classify_message({"metadata": {"source": source}}),
                "game_stream",
            )
        for source in ("proactive", "plugin:nekospire"):
            self.assertEqual(
                classify_message({"metadata": {"source": source}}),
                "plugin_notice",
            )
        self.assertEqual(
            classify_message({"metadata": {"source": "host"}}),
            UNCLASSIFIED,
        )

        # -- proactive turn types and the proactive flag
        for turn_type in ("proactive_instruction", "proactive_reply"):
            self.assertEqual(
                classify_message({"metadata": {"turn_type": turn_type}}),
                "plugin_notice",
            )
        self.assertEqual(
            classify_message({"metadata": {"proactive": True}}),
            "plugin_notice",
        )
        self.assertEqual(
            classify_message({"metadata": {"proactive": False}}),
            UNCLASSIFIED,
        )
        self.assertEqual(
            classify_message({"metadata": {"turn_type": ["not", "a", "string"]}}),
            UNCLASSIFIED,
        )

        # -- priority order: mirror beats source, source beats turn_type
        self.assertEqual(
            classify_message({
                "metadata": {
                    "mirror": {"kind": "controller"},
                    "source": "sts2_companion",
                },
            }),
            "companion_output",
        )
        self.assertEqual(
            classify_message({
                "metadata": {
                    "source": "sts2_companion",
                    "turn_type": "proactive_reply",
                },
            }),
            "game_stream",
        )

        # -- fallback
        self.assertEqual(classify_message({}), UNCLASSIFIED)
        self.assertEqual(classify_message({"data": {"metadata": {}}}), UNCLASSIFIED)

        # -- extraction routing for all nine classes plus the fallback
        for content_class in CONTENT_CLASSES:
            self.assertEqual(
                is_extractable(content_class),
                content_class in HOST_MEMORY_CLASSES,
            )
        self.assertTrue(is_extractable(UNCLASSIFIED))
        self.assertTrue(is_extractable("not-a-class"))
        self.assertTrue(is_extractable(None))
        self.assertFalse(is_extractable("game_stream"))

    def test_filter_keeps_host_skips_plugin_and_honours_barrier_and_deletions(self) -> None:
        deleted_key = "plugin:sts2_companion:neko:private:s1:e1"
        cutoff = 1000.0
        messages = [
            {
                "id": "host-chat",
                "data": {"metadata": {
                    "companion_event": {"content_class": "chat"},
                }},
            },
            {
                "id": "host-life",
                "metadata": {
                    "companion_event": {"content_class": "life_fact"},
                },
            },
            {
                "id": "host-pref",
                "metadata": {"companion_event": {
                    "content_class": "preference",
                    "source_key": deleted_key,
                }},
            },
            {
                "id": "host-shared",
                "metadata": {"companion_event": {
                    "content_class": "shared_experience",
                    "source_key": "host:neko:private:e2",
                }},
            },
            {"id": "plugin-stream",
             "metadata": {"companion_event": {"content_class": "game_stream"}}},
            {"id": "plugin-requirement",
             "metadata": {"companion_event": {"content_class": "game_requirement"}}},
            {"id": "plugin-technique",
             "metadata": {"companion_event": {"content_class": "game_technique"}}},
            {"id": "plugin-notice",
             "metadata": {"companion_event": {"content_class": "plugin_notice"}}},
            {"id": "companion-output",
             "metadata": {"companion_event": {"content_class": "companion_output"}}},
            {"id": "legacy-old", "timestamp": 900.0},
            {"id": "legacy-old-iso", "created_at": "1970-01-01T00:14:00+00:00"},
            {"id": "legacy-at-cutoff", "timestamp": cutoff},
            {"id": "legacy-current", "timestamp": 1100.0},
            {"id": "legacy-no-stamp"},
            {"id": "deleted-mirror",
             "metadata": {"mirror": {"event": {"source_key": deleted_key}}}},
            # Only the two frozen metadata locations count: a bare
            # metadata.source_key is not a deletion source key.
            {"id": "ignored-source-key",
             "metadata": {"source_key": deleted_key}},
            {"id": "kept-unclassified", "metadata": {}},
        ]

        kept, meta = filter_messages_for_extraction(
            messages,
            legacy_barrier_cutoff=cutoff,
            deleted_source_keys={deleted_key},
        )
        self.assertEqual(set(meta), META_KEYS)
        self.assertEqual(meta["gate_mode"], "off")
        self.assertEqual(meta["plugin_id"], "")
        self.assertEqual(meta["legacy_barrier"], cutoff)
        self.assertEqual(meta["skipped_classes"], {
            "game_stream": 1,
            "game_requirement": 1,
            "game_technique": 1,
            "plugin_notice": 1,
            "companion_output": 1,
        })
        self.assertEqual(meta["legacy_skipped"], 2)
        self.assertEqual(meta["deleted_skipped"], 2)
        self.assertEqual(meta["kept"], 8)
        self.assertEqual(meta["kept"], len(kept))
        self.assertEqual([item["id"] for item in kept], [
            "host-chat",
            "host-life",
            "host-shared",
            "legacy-at-cutoff",
            "legacy-current",
            "legacy-no-stamp",
            "ignored-source-key",
            "kept-unclassified",
        ])

        # -- without a barrier or deletions only plugin-only classes skip
        bare, bare_meta = filter_messages_for_extraction(messages)
        self.assertEqual(set(bare_meta), META_KEYS)
        self.assertIsNone(bare_meta["legacy_barrier"])
        self.assertEqual(bare_meta["legacy_skipped"], 0)
        self.assertEqual(bare_meta["deleted_skipped"], 0)
        self.assertEqual(bare_meta["kept"], 12)
        bare_ids = [item["id"] for item in bare]
        for expected in ("legacy-old", "legacy-old-iso", "host-pref",
                         "deleted-mirror"):
            self.assertIn(expected, bare_ids)

        # -- empty input still returns the fixed meta shape
        empty, empty_meta = filter_messages_for_extraction([])
        self.assertEqual(empty, [])
        self.assertEqual(set(empty_meta), META_KEYS)
        self.assertEqual(empty_meta["kept"], 0)
        self.assertEqual(empty_meta["skipped_classes"], {})

        # -- datetime timestamps work for the legacy barrier
        dated, dated_meta = filter_messages_for_extraction(
            [{
                "id": "dt",
                "timestamp": datetime.fromtimestamp(900, tz=timezone.utc),
            }],
            legacy_barrier_cutoff=cutoff,
        )
        self.assertEqual(dated, [])
        self.assertEqual(dated_meta["legacy_skipped"], 1)

        # -- source key collection reads exactly the two frozen locations
        self.assertEqual(
            message_source_keys({
                "metadata": {
                    "companion_event": {"source_key": " a "},
                    "mirror": {"event": {"source_key": "b"}},
                },
            }),
            {"a", "b"},
        )
        self.assertEqual(
            message_source_keys({
                "metadata": {
                    "companion_event": {"source_key": ""},
                    "mirror": {"event": {}},
                },
            }),
            set(),
        )
        self.assertEqual(
            message_source_keys({"metadata": {"source_key": "ignored"}}),
            set(),
        )

        # -- deleted_source_keys accepts any collection and strips entries
        listed, listed_meta = filter_messages_for_extraction(
            [{"id": "x",
              "metadata": {"companion_event": {"source_key": " k "}}}],
            deleted_source_keys=["k"],
        )
        self.assertEqual(listed, [])
        self.assertEqual(listed_meta["deleted_skipped"], 1)

    def test_gates_read_both_schemas_barrier_and_never_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)
            now_epoch = base.timestamp()
            clock = FixedClock(base)
            gate = SuppressionGate(memory_root=root, clock=clock)
            gate_path = root / ".fact_suppression.json"
            state_path = root / ".source_gate_state.json"

            # -- no files at all: off, and the read creates nothing
            self.assertEqual(gate.gate_for("neko"), GateState())
            self.assertEqual(list(root.iterdir()), [])

            # -- schema 1 inside the 90 s TTL suppresses the whole batch
            _write_json(gate_path, {
                "suppress": True,
                "source": "sts2_companion",
                "reason": "plugin alive",
                "pid": 123,
                "updated_at": now_epoch - 30,
            })
            state = gate.gate_for("neko")
            self.assertEqual(state.mode, "legacy")
            self.assertTrue(state.suppressed)
            self.assertEqual(state.suppress_classes, frozenset())
            self.assertIsNone(state.legacy_barrier)
            self.assertIsNone(state.plugin_id)

            # -- a stale schema 1 heartbeat is off
            _write_json(gate_path, {
                "suppress": True,
                "updated_at": now_epoch - 91,
            })
            stale = gate.gate_for("neko")
            self.assertEqual(stale.mode, "off")
            self.assertFalse(stale.suppressed)

            # -- suppress=false is off, and a missing timestamp is off
            _write_json(gate_path, {
                "suppress": False,
                "updated_at": now_epoch - 1,
            })
            self.assertEqual(gate.gate_for("neko").mode, "off")
            _write_json(gate_path, {"suppress": True})
            self.assertEqual(gate.gate_for("neko").mode, "off")

            # -- schema 2 within the TTL is the content gate
            _write_json(gate_path, {
                "schema": 2,
                "plugin_id": "sts2_companion",
                "suppress_classes": [
                    "game_stream", "bogus", " companion_output ", 7,
                ],
                "activated_at": now_epoch - 100,
                "updated_at": now_epoch - 10,
            })
            content = gate.gate_for("neko")
            self.assertEqual(content.mode, "content")
            self.assertFalse(content.suppressed)
            self.assertEqual(
                content.suppress_classes,
                frozenset({"game_stream", "companion_output"}),
            )
            self.assertEqual(content.plugin_id, "sts2_companion")

            # -- a stale schema 2 heartbeat is off
            _write_json(gate_path, {
                "schema": 2,
                "plugin_id": "sts2_companion",
                "suppress_classes": ["game_stream"],
                "updated_at": now_epoch - 91,
            })
            stale = gate.gate_for("neko")
            self.assertEqual(stale.mode, "off")
            self.assertEqual(stale.suppress_classes, frozenset())

            # -- ISO-8601 timestamps are accepted
            _write_json(gate_path, {
                "schema": 2,
                "plugin_id": "sts2_companion",
                "suppress_classes": [],
                "updated_at": (base - timedelta(seconds=20)).isoformat(),
            })
            self.assertEqual(gate.gate_for("neko").mode, "content")

            # -- bad JSON and a bad top-level shape read as off, not raise
            gate_path.write_text("{not json", encoding="utf-8")
            self.assertEqual(gate.gate_for("neko").mode, "off")
            _write_json(gate_path, ["not", "an", "object"])
            self.assertEqual(gate.gate_for("neko").mode, "off")

            # -- barrier entries are read per character, gate or no gate
            gate_path.unlink()
            _write_json(state_path, {
                "schema": 1,
                "characters": {
                    "neko": {
                        "legacy_cutoff": 1234.5,
                        "plugin_id": "sts2_companion",
                        "updated_at": now_epoch,
                    },
                    "other": {
                        "legacy_cutoff": 99,
                        "plugin_id": "other_plugin",
                        "updated_at": now_epoch,
                    },
                    "broken": "not-a-mapping",
                },
            })
            neko = gate.gate_for("neko")
            self.assertEqual(neko.legacy_barrier, 1234.5)
            self.assertEqual(neko.plugin_id, "sts2_companion")
            self.assertEqual(gate.gate_for("other").legacy_barrier, 99.0)
            self.assertEqual(gate.gate_for("other").plugin_id, "other_plugin")
            missing = gate.gate_for("missing")
            self.assertIsNone(missing.legacy_barrier)
            self.assertIsNone(missing.plugin_id)
            self.assertIsNone(gate.gate_for("broken").legacy_barrier)

            # -- a malformed state file is no barrier, not an error
            _write_json(state_path, {"schema": 1, "characters": ["bad"]})
            self.assertIsNone(gate.gate_for("neko").legacy_barrier)
            state_path.write_text("[]", encoding="utf-8")
            self.assertIsNone(gate.gate_for("neko").legacy_barrier)

            # -- the content gate's plugin id wins, barrier still comes along
            _write_json(state_path, {
                "schema": 1,
                "characters": {"neko": {
                    "legacy_cutoff": 555.0,
                    "plugin_id": "old_plugin",
                    "updated_at": now_epoch,
                }},
            })
            _write_json(gate_path, {
                "schema": 2,
                "plugin_id": "current_plugin",
                "suppress_classes": ["chat"],
                "updated_at": now_epoch,
            })
            both = gate.gate_for("neko")
            self.assertEqual(both.mode, "content")
            self.assertEqual(both.plugin_id, "current_plugin")
            self.assertEqual(both.legacy_barrier, 555.0)
            self.assertEqual(both.suppress_classes, frozenset({"chat"}))

            # -- GateState validates its mode
            with self.assertRaises(ValueError):
                GateState(mode = "bogus")

            # -- reading never writes: bytes and file set stay identical
            before = {
                path.name: path.read_bytes()
                for path in root.iterdir() if path.is_file()
            }
            gate.gate_for("neko")
            gate.gate_for("other")
            gate.gate_for("missing")
            after = {
                path.name: path.read_bytes()
                for path in root.iterdir() if path.is_file()
            }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
