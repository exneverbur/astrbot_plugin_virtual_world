"""配置与持久层测试（seam S5、S6、S14）。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import ConfigStore  # noqa: E402
from core.db import Database  # noqa: E402
from core.memory import (  # noqa: E402
    SCOPE_GROUP,
    SCOPE_GROUP_PERSONA,
    SCOPE_PERSONA,
    MemoryEngine,
)
from core.models import parse_world  # noqa: E402
from core.defaults import default_world  # noqa: E402


class TempDirMixin:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)


class TestConfigStore(TempDirMixin, unittest.TestCase):
    def test_first_start_creates_default_files(self):
        store = ConfigStore(self.tmp_path)
        created = store.ensure_files()
        self.assertIn("world.json", created)
        self.assertIn("schedules.json", created)
        self.assertIn("sessions.json", created)
        for name in ("world.json", "schedules.json", "sessions.json"):
            self.assertTrue((self.tmp_path / name).exists())

    def test_default_world_loads_without_warnings(self):
        store = ConfigStore(self.tmp_path)
        store.ensure_files()
        world = store.load_world()
        self.assertEqual([], store.warnings)
        self.assertIn("bedroom", world.node_map())
        self.assertTrue(world.actions)

    def test_upgrade_keeps_user_values_and_adds_new_fields(self):
        store = ConfigStore(self.tmp_path)
        store.ensure_files()
        raw = store.raw_world()
        raw["name"] = "我的世界"
        raw["nodes"][0]["prompt"] = "用户改过的提示"
        del raw["engagement"]  # 模拟旧版本没有这个字段
        store.save_world(raw)

        world = store.load_world()
        self.assertEqual(world.name, "我的世界")
        self.assertEqual(world.node_map()["bedroom"].prompt, "用户改过的提示")
        self.assertEqual(world.engagement.unanswered_threshold, 3)

    def test_broken_json_is_backed_up(self):
        store = ConfigStore(self.tmp_path)
        store.ensure_files()
        (self.tmp_path / "world.json").write_text("{ 这不是 JSON", encoding="utf-8")
        world = store.load_world()
        self.assertTrue(world.nodes)
        self.assertTrue(any("不是合法 JSON" in w for w in store.warnings))
        backups = list(self.tmp_path.glob("world.json.broken.*"))
        self.assertEqual(len(backups), 1)

    def test_restore_default(self):
        store = ConfigStore(self.tmp_path)
        store.ensure_files()
        raw = store.raw_world()
        raw["name"] = "改坏了"
        store.save_world(raw)
        restored = store.restore_default("world")
        self.assertIn("world.json", restored)
        self.assertEqual(store.load_world().name, "小世界")

    def test_session_lifecycle(self):
        store = ConfigStore(self.tmp_path)
        store.ensure_files()
        store.load_world()
        self.assertTrue(store.add_session("aiocqhttp:GroupMessage:100", session_type="group"))
        self.assertFalse(store.add_session("aiocqhttp:GroupMessage:100"))
        sessions = store.load_sessions()
        self.assertEqual(len(sessions.sessions), 1)
        self.assertEqual(sessions.sessions[0].platform, "aiocqhttp")
        self.assertTrue(store.set_session_enabled("aiocqhttp:GroupMessage:100", False))
        self.assertFalse(store.load_sessions().sessions[0].enabled)
        self.assertTrue(store.remove_session("aiocqhttp:GroupMessage:100"))
        self.assertEqual(store.load_sessions().sessions, [])

    def test_invalid_node_references_are_repaired(self):
        raw = default_world()
        raw["edges"].append({"id": "bad", "from": "bedroom", "to": "nowhere", "ticks": 1})
        raw["actions"].append(
            {
                "id": "ghost",
                "name": "幽灵动作",
                "category": "instant",
                "llm_level": "template",
                "scope": "node",
                "allowed_nodes": ["nowhere"],
            }
        )
        world, warnings = parse_world(raw)
        self.assertNotIn("nowhere", world.node_map())
        self.assertTrue(all(edge.to != "nowhere" for edge in world.edges))
        self.assertEqual(world.action_map()["ghost"].scope, "global")
        self.assertTrue(warnings)


class TestDatabase(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.tmp_path / "state.db")
        self.addCleanup(self.db.close)

    def test_state_roundtrip(self):
        self.assertIsNone(self.db.get_state("s1"))
        self.db.save_state("s1", {"node_id": "study", "energy": 0.4})
        payload = self.db.get_state("s1")
        self.assertEqual(payload["node_id"], "study")
        self.db.save_state("s1", {"node_id": "lobby"})
        self.assertEqual(self.db.get_state("s1")["node_id"], "lobby")
        self.assertEqual(self.db.list_state_ids(), ["s1"])
        self.db.delete_state("s1")
        self.assertIsNone(self.db.get_state("s1"))

    def test_schedule_fire_is_idempotent_per_day(self):
        self.assertTrue(
            self.db.mark_schedule_fired(
                session_id="s1", schedule_id="night_sleep", day_key="2026-09-10"
            )
        )
        self.assertFalse(
            self.db.mark_schedule_fired(
                session_id="s1", schedule_id="night_sleep", day_key="2026-09-10"
            )
        )
        self.assertTrue(
            self.db.mark_schedule_fired(
                session_id="s1", schedule_id="night_sleep", day_key="2026-09-11"
            )
        )

    def test_schedule_fire_dedupes_per_day_and_time(self):
        """同一天把触发时间改了，新的时间点要能再触发一次。"""

        self.assertTrue(
            self.db.mark_schedule_fired(
                session_id="s1", schedule_id="news", day_key="2026-09-10", slot="08:30"
            )
        )
        self.assertFalse(
            self.db.mark_schedule_fired(
                session_id="s1", schedule_id="news", day_key="2026-09-10", slot="08:30"
            )
        )
        self.assertTrue(
            self.db.mark_schedule_fired(
                session_id="s1", schedule_id="news", day_key="2026-09-10", slot="09:10"
            )
        )

    def test_legacy_schedule_fire_table_is_migrated(self):
        """老库里的 schedule_fire 没有 slot 列，打开时要原地升级而不是报错。"""

        legacy_path = self.tmp_path / "legacy.db"
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE schedule_fire (
                session_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                day_key TEXT NOT NULL,
                fired_at REAL NOT NULL,
                PRIMARY KEY (session_id, schedule_id, day_key)
            );
            INSERT INTO schedule_fire VALUES ('s1', 'news', '2026-09-10', 1.0);
            """
        )
        conn.commit()
        conn.close()

        upgraded = Database(legacy_path)
        self.addCleanup(upgraded.close)
        # 老记录保留：同一个时间点当天不再触发
        self.assertFalse(
            upgraded.mark_schedule_fired(
                session_id="s1", schedule_id="news", day_key="2026-09-10", slot=""
            )
        )
        # 换成新的时间点就能触发
        self.assertTrue(
            upgraded.mark_schedule_fired(
                session_id="s1", schedule_id="news", day_key="2026-09-10", slot="09:10"
            )
        )

    def test_database_recovers_after_being_closed(self):
        """连接被关掉之后（热重载窗口里旧实例收到消息）要能自己接回来。"""

        path = self.tmp_path / "revive.db"
        db = Database(path)
        db.save_state("s1", {"node_id": "study"})
        db.close()
        self.assertEqual(db.get_state("s1")["node_id"], "study")
        db.save_state("s1", {"node_id": "lobby"})
        self.assertEqual(db.get_state("s1")["node_id"], "lobby")
        db.close()

    def test_kv_store(self):
        self.assertEqual(self.db.kv_get("password", "none"), "none")
        self.db.kv_set("password", {"hash": "abc"})
        self.assertEqual(self.db.kv_get("password")["hash"], "abc")
        self.db.kv_delete("password")
        self.assertIsNone(self.db.kv_get("password"))

    def test_events_and_stats(self):
        self.db.add_event(session_id="s1", event_type="test", detail={"a": 1})
        events = self.db.query_events(session_id="s1")
        self.assertEqual(events[0]["event_type"], "test")
        self.assertEqual(events[0]["detail"]["a"], 1)

    def test_event_filters_and_types(self):
        for index in range(5):
            self.db.add_event(
                session_id="s1",
                event_type="plan" if index % 2 == 0 else "action",
                detail={"note": f"第{index}条", "kind": "x"},
            )
        self.db.add_event(session_id="s2", event_type="reply", detail={})
        self.assertEqual(len(self.db.query_events(session_id="s1", limit=10)), 5)
        self.assertEqual(len(self.db.query_events(session_id="s1", event_type="plan")), 3)
        self.assertEqual(len(self.db.query_events(session_id="s1", keyword="第3条")), 1)
        newest = self.db.query_events(session_id="s1", limit=2)
        older = self.db.query_events(session_id="s1", limit=10, before_id=newest[-1]["id"])
        self.assertEqual(len(older), 3)
        self.assertIn("reply", self.db.event_types())
        self.assertEqual(self.db.event_types(session_id="s2"), ["reply"])

    def test_prune_session_events_keeps_newest(self):
        for index in range(60):
            self.db.add_event(session_id="s1", event_type="tick", detail={"i": index})
        removed = self.db.prune_session_events("s1", keep=50)
        self.assertEqual(removed, 10)
        rows = self.db.query_events(session_id="s1", limit=100)
        self.assertEqual(len(rows), 50)
        # 留下的是最新的
        self.assertEqual(rows[0]["detail"]["i"], 59)


class TestMemoryEngine(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.tmp_path / "state.db")
        self.addCleanup(self.db.close)
        world, _ = parse_world(default_world())
        self.world = world
        self.engine = MemoryEngine(self.db, world)

    def test_group_persona_recall_covers_same_group_and_persona(self):
        session = "aiocqhttp:GroupMessage:100"
        other = "aiocqhttp:GroupMessage:200"
        self.engine.remember(
            session_id=session, persona_id="p1", node_id="window",
            content="在这个房间说过想他", scope=SCOPE_GROUP_PERSONA, weight=0.9,
        )
        self.engine.remember(
            session_id=session, persona_id="p1", node_id="window",
            content="这个群里的旧事", scope=SCOPE_GROUP, weight=0.9,
        )
        self.engine.remember(
            session_id=other, persona_id="p1", node_id="window",
            content="人格在别的群的记忆", scope=SCOPE_PERSONA, weight=0.9,
        )
        self.engine.remember(
            session_id=other, persona_id="p2", node_id="window",
            content="别人的隐私", scope=SCOPE_GROUP_PERSONA, weight=0.9,
        )
        contents = [
            item.content
            for item in self.engine.recall(
                session_id=session, persona_id="p1", node_id="window", limit=10
            )
        ]
        self.assertIn("在这个房间说过想他", contents)
        self.assertIn("这个群里的旧事", contents)
        self.assertIn("人格在别的群的记忆", contents)
        self.assertNotIn("别人的隐私", contents)

    def test_group_mode_does_not_leak_other_groups(self):
        session = "aiocqhttp:GroupMessage:100"
        other = "aiocqhttp:GroupMessage:200"
        self.engine.remember(
            session_id=other, persona_id="p1", node_id="window",
            content="别的群的事", scope=SCOPE_GROUP_PERSONA, weight=0.9,
        )
        results = self.engine.recall(
            session_id=session, persona_id="p1", mode=SCOPE_GROUP, limit=10
        )
        self.assertEqual([item.content for item in results], [])

    def test_node_memory_does_not_leak_across_sessions(self):
        """真机实测到的 bug：节点专属记忆没按会话过滤，A 群的房间记忆会出现在 B 群。"""

        a, b = "aiocqhttp:GroupMessage:100", "aiocqhttp:GroupMessage:200"
        self.engine.remember(
            session_id=a, persona_id="", node_id="study",
            content="在这个书房里放过一首歌", scope="node", weight=0.8,
        )
        in_a = self.engine.recall(session_id=a, persona_id="", node_id="study", limit=5)
        in_b = self.engine.recall(session_id=b, persona_id="", node_id="study", limit=5)
        self.assertEqual([item.content for item in in_a], ["在这个书房里放过一首歌"])
        self.assertEqual([item.content for item in in_b], [])

    def test_focus_user_boosts_score(self):
        session = "aiocqhttp:GroupMessage:100"
        self.engine.remember(
            session_id=session, persona_id="p1", node_id="window",
            content="和某人聊过天", related_users=["42"], weight=0.5,
        )
        self.engine.remember(
            session_id=session, persona_id="p1", node_id="window",
            content="无关的事", weight=0.5,
        )
        results = self.engine.recall(
            session_id=session, persona_id="p1", node_id="window", focus_user="42", limit=1
        )
        self.assertEqual(results[0].content, "和某人聊过天")

    def test_conflict_policy_replaces_opposite_memory(self):
        session = "aiocqhttp:GroupMessage:100"
        self.engine.remember(
            session_id=session, persona_id="p1", node_id="window",
            content="他喜欢我", weight=0.5,
        )
        self.engine.remember(
            session_id=session, persona_id="p1", node_id="window",
            content="他不喜欢我", weight=0.6,
        )
        rows = self.db.query_memories(session_id=session, limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "他不喜欢我")

    def test_forget_and_export_user(self):
        session = "aiocqhttp:GroupMessage:100"
        self.engine.remember(
            session_id=session, persona_id="p1", node_id="window",
            content="和 42 聊过天", related_users=["42"],
        )
        self.engine.remember(
            session_id=session, persona_id="p1", node_id="window",
            content="无关的事", related_users=["7"],
        )
        exported = self.engine.export_user(session_id=session, persona_id="p1", user_id="42")
        self.assertEqual(len(exported), 1)
        removed = self.engine.forget_user(session_id=session, persona_id="p1", user_id="42")
        self.assertEqual(removed, 1)
        self.assertEqual(self.engine.export_user(session_id=session, persona_id="p1", user_id="42"), [])

    def test_correct_memory(self):
        session = "aiocqhttp:GroupMessage:100"
        self.engine.remember(
            session_id=session, persona_id="p1", node_id="window", content="他喜欢猫"
        )
        changed = self.engine.correct(
            session_id=session, persona_id="p1", old_text="猫", new_text="狗"
        )
        self.assertEqual(changed, 1)
        rows = self.db.query_memories(session_id=session, limit=10)
        self.assertEqual(rows[0]["content"], "他喜欢狗")

    def test_decay_removes_negligible_memories(self):
        session = "aiocqhttp:GroupMessage:100"
        memory_id = self.engine.remember(
            session_id=session, persona_id="p1", node_id="window",
            content="很旧的事", weight=0.4,
        )
        self.assertIsNotNone(memory_id)
        self.db.update_memory(memory_id, created_at=time.time() - 86400 * 365)
        self.engine.decay(session_id=session, half_life_days=30)
        self.assertEqual(self.db.query_memories(session_id=session), [])

    def test_stats(self):
        session = "aiocqhttp:GroupMessage:100"
        self.engine.remember(
            session_id=session, persona_id="p1", node_id="window", content="一"
        )
        stats = self.engine.stats(session)
        self.assertEqual(stats["total"], 1)
        self.assertIn("by_scope", stats)

    def test_bulk_delete_and_clear_memories(self):
        session = "aiocqhttp:GroupMessage:100"
        other = "aiocqhttp:GroupMessage:200"
        ids = [
            self.engine.remember(
                session_id=session, persona_id="p1", node_id="window", content=f"第{i}条"
            )
            for i in range(3)
        ]
        self.engine.remember(
            session_id=other, persona_id="p1", node_id="window", content="别的会话"
        )

        removed = self.db.delete_memories_by_ids([ids[0], ids[1]])
        self.assertEqual(removed, 2)
        self.assertEqual(len(self.db.query_memories(session_id=session)), 1)

        cleared = self.db.clear_memories(session)
        self.assertEqual(cleared, 1)
        self.assertEqual(self.db.query_memories(session_id=session), [])
        # 只清指定会话，别的会话不受影响
        self.assertEqual(len(self.db.query_memories(session_id=other)), 1)

    def test_bulk_delete_and_clear_events(self):
        session = "aiocqhttp:GroupMessage:100"
        other = "aiocqhttp:GroupMessage:200"
        first = self.db.add_event(session_id=session, event_type="action", detail={"a": 1})
        self.db.add_event(session_id=session, event_type="action", detail={"a": 2})
        self.db.add_event(session_id=other, event_type="action", detail={"a": 3})

        self.assertEqual(self.db.delete_events_by_ids([first]), 1)
        self.assertEqual(len(self.db.query_events(session_id=session)), 1)
        self.assertEqual(self.db.clear_events(session), 1)
        self.assertEqual(self.db.query_events(session_id=session), [])
        self.assertEqual(len(self.db.query_events(session_id=other)), 1)


if __name__ == "__main__":
    unittest.main()
