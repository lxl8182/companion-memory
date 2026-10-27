"""The display name is the identity section's first line (S05 follow-up).

Pinned failures:

* a name kept in its own file could not be edited by any card editor;
* a second name section can disagree with the identity text the model reads;
* a leftover legacy file must be folded in once and then removed;
* a name the card already holds always wins over the file;
* the console reads the name live, so a rename shows up without a restart.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from rin_memory.persistence.persona_card import parse_name_line, with_name_line
from rin_memory.service.management import LEGACY_NAME_FILE, ManagementApi
from rin_memory.service.memory_service import MemoryService, ServiceSettings

IDENTITY = "性别: 女\n年龄: 16岁\n身份: 主人身边的伙伴。"


class NameLineTests(unittest.TestCase):
    def test_the_first_line_carries_the_name(self):
        self.assertEqual(parse_name_line("名字: 小铃\n性别: 女"), "小铃")
        self.assertEqual(parse_name_line("姓名：小铃"), "小铃")
        self.assertEqual(parse_name_line("\n名字: 小铃\n"), "小铃")

    def test_free_prose_has_no_name_instead_of_a_guess(self):
        # Guessing from prose would silently rename her to whatever came first.
        self.assertEqual(parse_name_line("昵称: 小铃、小潮"), "")
        self.assertEqual(parse_name_line(""), "")
        self.assertEqual(parse_name_line(None), "")

    def test_rewriting_moves_the_name_and_keeps_the_body(self):
        self.assertEqual(
            with_name_line("名字: 旧\n性别: 女", "新"), "名字: 新\n性别: 女",
        )
        self.assertEqual(
            with_name_line(IDENTITY, "小铃"), "名字: 小铃\n" + IDENTITY,
        )


class DisplayNameSectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory_root = self.root / "memory"
        self.memory_root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def start(self):
        self.service = MemoryService.start(ServiceSettings(
            memory_root=self.memory_root, character="demo",
        ))
        self.management = ManagementApi(self.service)
        return self.management

    def stop(self):
        asyncio.run(self.service.stop())

    def test_initialization_puts_the_name_on_the_identity_line(self):
        management = self.start()
        try:
            management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
            card = self.service.core.persona_card_service.get("demo")
            self.assertEqual([section.section_id for section in card.sections],
                             ["identity"])
            identity = card.sections[0]
            self.assertTrue(identity.text.startswith("名字: 小潮\n"))
            self.assertIn("温柔而直白。", identity.text)
            self.assertTrue(identity.frozen)
            self.assertEqual(management.initialization()["display_name"], "小潮")
            # A rename is one ordinary card revision of the same section.
            self.assertTrue(self.service.core.persona_card_service.rename("demo", "小铃"))
            self.assertEqual(management.initialization()["display_name"], "小铃")
            card = self.service.core.persona_card_service.get("demo")
            self.assertEqual(
                sum(1 for section in card.sections if section.section_id == "identity"),
                1,
            )
            self.assertEqual(card.sections[0].text,
                             "名字: 小铃\n温柔而直白。")
        finally:
            self.stop()

    def test_the_name_is_part_of_the_prompt_not_a_second_label(self):
        management = self.start()
        try:
            management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
            card_service = self.service.core.persona_card_service
            # The name is authored text now, so the model may see it; what must
            # not exist is a second key carrying the same value.
            self.assertIn("名字: 小潮", card_service.render_text("demo"))
            self.assertEqual(
                set(card_service.render_projection("demo")), {"identity"},
            )
        finally:
            self.stop()

    def test_a_repeated_initialization_never_changes_the_name(self):
        management = self.start()
        try:
            management.initialize({"display_name": "小潮", "text": "温柔而直白。"})
            management.initialize({"display_name": "别人", "text": "别的正文。"})
            self.assertEqual(management.initialization()["display_name"], "小潮")
            # The duplicate import changes neither the name nor the body.
            text = self.service.core.persona_card_service.get("demo").sections[0].text
            self.assertIn("温柔而直白。", text)
            self.assertNotIn("别的正文。", text)
        finally:
            self.stop()

    def test_a_legacy_name_file_is_folded_in_once_and_then_removed(self):
        self.service = MemoryService.start(ServiceSettings(
            memory_root=self.memory_root, character="demo",
        ))
        self.service.core.persona_card_service.import_initial("demo", [{
            "section_id": "identity", "kind": "identity", "text": IDENTITY,
            "editable_by": {"user": True, "evolution": False}, "frozen": True,
        }])
        legacy = self.memory_root / LEGACY_NAME_FILE
        legacy.write_text(json.dumps({
            "schema_version": 1, "character_id": "demo", "display_name": "小潮",
        }, ensure_ascii=False), encoding="utf-8")
        self.stop()

        management = self.start()
        try:
            self.assertEqual(management.initialization()["display_name"], "小潮")
            self.assertFalse(legacy.exists())
            identity = self.service.core.persona_card_service.get("demo").sections[0]
            self.assertTrue(identity.text.startswith("名字: 小潮\n"))
            self.assertIn("性别: 女", identity.text)
        finally:
            self.stop()

        management = self.start()
        try:
            self.assertEqual(management.initialization()["display_name"], "小潮")
        finally:
            self.stop()

    def test_a_name_already_on_the_card_beats_the_legacy_file(self):
        self.service = MemoryService.start(ServiceSettings(
            memory_root=self.memory_root, character="demo",
        ))
        self.service.core.persona_card_service.import_initial("demo", [{
            "section_id": "identity", "text": "名字: 小铃\n" + IDENTITY,
        }])
        legacy = self.memory_root / LEGACY_NAME_FILE
        legacy.write_text(json.dumps({
            "schema_version": 1, "character_id": "demo", "display_name": "旧名字",
        }, ensure_ascii=False), encoding="utf-8")
        self.stop()

        management = self.start()
        try:
            self.assertEqual(management.initialization()["display_name"], "小铃")
            self.assertFalse(legacy.exists())
        finally:
            self.stop()

    def test_a_legacy_name_for_another_character_is_refused(self):
        self.service = MemoryService.start(ServiceSettings(
            memory_root=self.memory_root, character="demo",
        ))
        self.service.core.persona_card_service.import_initial("demo", [{
            "section_id": "identity", "text": IDENTITY,
        }])
        self.service.core.persona_publish.publish("demo")
        (self.memory_root / LEGACY_NAME_FILE).write_text(json.dumps({
            "schema_version": 1, "character_id": "other", "display_name": "别人的名字",
        }, ensure_ascii=False), encoding="utf-8")
        self.stop()

        service = MemoryService.start(ServiceSettings(
            memory_root=self.memory_root, character="demo",
        ))
        try:
            with self.assertRaises(ValueError):
                ManagementApi(service)
        finally:
            asyncio.run(service.stop())


if __name__ == "__main__":
    unittest.main()
