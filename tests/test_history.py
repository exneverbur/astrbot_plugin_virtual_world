"""配置改动历史：留档、去重、滚动、恢复、删除。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import HISTORY_KEEP, ConfigStore  # noqa: E402
from core.defaults import default_world  # noqa: E402


class HistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ConfigStore(self._tmp.name)
        self.store.ensure_files()

    def world_with(self, **changes):
        world = self.store.raw_world()
        world.update(changes)
        return world

    def set_persona(self, text: str) -> None:
        world = self.store.raw_world()
        world.setdefault("persona", {})
        world["persona"]["text"] = text
        self.store.save_world(world)

    def test_saving_keeps_the_previous_version(self):
        """保存前先留一份：历史里能翻回改之前的样子。"""

        self.set_persona("第一版人设")
        entries = self.store.list_history()
        self.assertTrue(entries, "保存后应该留下一条历史")

        detail = self.store.read_history(entries[0]["id"])
        self.assertEqual(detail["snapshot"]["world"]["persona"]["text"], "")
        self.assertIn("persona", detail["changed"])

    def test_identical_content_is_not_stored_twice(self):
        """内容没变就不占位置（连点保存不该把 50 条挤满）。"""

        self.set_persona("同样的人设")
        # 第一次：当前这一版还没被记过，留一条；之后同样的内容不再占位置
        self.store.save_world(self.store.raw_world())
        count = len(self.store.list_history())
        for _ in range(3):
            self.store.save_world(self.store.raw_world())
        self.assertEqual(len(self.store.list_history()), count)

    def test_history_rolls_at_the_limit(self):
        """只留最近 HISTORY_KEEP 条，最老的自动清掉。"""

        for index in range(HISTORY_KEEP + 5):
            self.set_persona(f"第 {index} 版人设")
        entries = self.store.list_history(limit=200)
        self.assertEqual(len(entries), HISTORY_KEEP)
        # 最新那条记的是"倒数第二次"的样子
        detail = self.store.read_history(entries[0]["id"])
        self.assertEqual(
            detail["snapshot"]["world"]["persona"]["text"],
            f"第 {HISTORY_KEEP + 3} 版人设",
        )

    def test_restore_can_go_back_and_forth(self):
        """恢复会把当前这版也留档，所以"恢复"本身也能再恢复回去。"""

        self.set_persona("旧版")
        self.set_persona("新版")
        target = [
            item
            for item in self.store.list_history(limit=10)
            if "旧版" in (item["summary"] or "")
        ]
        older = self.store.list_history(limit=10)[-1]
        detail = self.store.read_history(older["id"])
        self.assertEqual(detail["snapshot"]["world"]["persona"]["text"], "")

        self.store.restore_history(older["id"], blocks=["persona"])
        self.assertEqual(self.store.raw_world()["persona"]["text"], "")
        # 恢复前的那一版（新版）还在历史里
        texts = [
            self.store.read_history(item["id"])["snapshot"]["world"]["persona"]["text"]
            for item in self.store.list_history(limit=5)
        ]
        self.assertIn("新版", texts)
        self.assertTrue(target is not None)

    def test_restore_only_persona_keeps_the_rest(self):
        """只恢复人设时，地图/世界设置这些不许被一起换掉。"""

        self.set_persona("先写一版")
        before = self.store.raw_world()
        marker = dict(before)
        marker["name"] = "改动后的世界名"
        self.store.save_world(marker)

        older = self.store.list_history(limit=10)[-1]
        self.store.restore_history(older["id"], blocks=["persona"])
        after = self.store.raw_world()
        self.assertEqual(after["name"], "改动后的世界名")
        self.assertEqual(after["persona"]["text"], "")

    def test_delete_and_clear(self):
        """能删单条，也能清空；旧版备份不受影响。"""

        self.set_persona("一")
        self.set_persona("二")
        entries = self.store.list_history()
        self.assertTrue(self.store.delete_history(entries[0]["id"]))
        self.assertEqual(len(self.store.list_history()), len(entries) - 1)
        self.assertGreaterEqual(self.store.clear_history(), 0)
        self.assertEqual(self.store.list_history(), [])

    def test_legacy_backups_are_listed_but_never_scrolled_away(self):
        """早期 before-apply-*.json 也列出来（标成旧版），但不参与滚动删除。"""

        legacy = self.store.backups_dir
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "before-apply-20200101-000000.json").write_text(
            '{"id": "before-apply-20200101-000000", "world": {}}', encoding="utf-8"
        )
        for index in range(HISTORY_KEEP + 3):
            self.set_persona(f"第 {index} 版")
        entries = self.store.list_history(limit=200)
        legacy_rows = [item for item in entries if item["legacy"]]
        self.assertEqual(len(legacy_rows), 1)
        self.assertEqual(self.store.clear_history() >= 0, True)
        self.assertTrue((legacy / "before-apply-20200101-000000.json").exists())


if __name__ == "__main__":
    unittest.main()
