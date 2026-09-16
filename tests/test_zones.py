"""区域（世界地图）与跨区门户：数据模型、迁移、寻路与提示词。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.defaults import default_world  # noqa: E402
from core.models import parse_world  # noqa: E402
from core.pathfinding import travel_cost  # noqa: E402
from core.prompt import PromptBuilder  # noqa: E402


def world_with_park():
    """默认世界 + 一个室外区域：北门 / 鸽子广场，大厅 ↔ 北门 是跨区门户。"""

    raw = default_world()
    raw["zones"].append(
        {
            "id": "park",
            "name": "街心公园",
            "note": "外面人来人往，风里有树叶味",
            "x": 320,
            "y": 80,
        }
    )
    raw["nodes"].append(
        {
            "id": "park_gate",
            "name": "北门",
            "zone_id": "park",
            "x": 80,
            "y": 60,
            "prompt": "公园的北门，能看见马路对面。",
        }
    )
    raw["nodes"].append(
        {
            "id": "park_square",
            "name": "鸽子广场",
            "zone_id": "park",
            "x": 220,
            "y": 60,
            "prompt": "一群鸽子，长椅上有晒太阳的人。",
        }
    )
    raw["edges"].append(
        {"id": "e_gate_square", "from": "park_gate", "to": "park_square", "ticks": 1}
    )
    raw["zone_edges"].append(
        {
            "id": "z_lobby_gate",
            "from_zone": "home",
            "to_zone": "park",
            "from_node": "lobby",
            "to_node": "park_gate",
            "ticks": 2,
        }
    )
    return raw


class TestZoneModel(unittest.TestCase):
    def test_legacy_world_without_zones_is_migrated(self):
        """老配置没有 zones：自动建「家中」，所有节点归进去。"""

        raw = default_world()
        raw.pop("zones")
        raw.pop("zone_edges")
        for node in raw["nodes"]:
            node.pop("zone_id", None)

        world, warnings = parse_world(raw)
        self.assertEqual(warnings, [])
        self.assertEqual([zone.id for zone in world.zones], ["home"])
        self.assertTrue(all(node.zone_id == "home" for node in world.nodes))
        self.assertEqual(len(world.nodes_in_zone("home")), len(world.nodes))

    def test_unknown_zone_falls_back_to_the_first_one(self):
        raw = default_world()
        raw["nodes"][0]["zone_id"] = "不存在的地方"
        world, warnings = parse_world(raw)
        self.assertTrue(any("所属区域" in item for item in warnings), warnings)
        self.assertEqual(world.nodes[0].zone_id, "home")

    def test_zone_nodes_are_kept_separate(self):
        world, warnings = parse_world(world_with_park())
        self.assertEqual(warnings, [])
        self.assertEqual(
            [node.id for node in world.nodes_in_zone("home")],
            ["bedroom", "study", "window", "bar", "kitchen", "lobby"],
        )
        self.assertEqual(
            [node.id for node in world.nodes_in_zone("park")],
            ["park_gate", "park_square"],
        )
        self.assertEqual(world.zone_of("park_gate"), "park")
        self.assertEqual(world.default_zone_id(), "home")

    def test_broken_portals_are_dropped(self):
        raw = world_with_park()
        raw["zone_edges"].append(
            {"from_zone": "home", "to_zone": "没有这个区", "from_node": "lobby", "to_node": "park_gate"}
        )
        raw["zone_edges"].append(
            {"from_zone": "home", "to_zone": "park", "from_node": "lobby", "to_node": "没有这个房间"}
        )
        raw["zone_edges"].append(
            # 房间和区域对不上：park_gate 属于 park，不是 home
            {"from_zone": "home", "to_zone": "park", "from_node": "park_gate", "to_node": "park_square"}
        )
        world, warnings = parse_world(raw)
        self.assertEqual(len(world.zone_edges), 1)  # 只剩本来就对的那条
        self.assertEqual(len(warnings), 3)


class TestActionBindingSingleSource(unittest.TestCase):
    """动作归属只有一份数据：动作的 allowed_nodes（节点上那份已经废弃）。"""

    def test_node_side_list_migrates_into_the_action(self):
        raw = default_world()
        raw["nodes"].append(
            {
                "id": "garden",
                "name": "花园",
                "zone_id": "home",
                "prompt": "有花有草",
                "allowed_actions": ["read"],  # 老写法：节点说自己能做「看书」
            }
        )
        raw["edges"].append({"id": "e_garden", "from": "lobby", "to": "garden", "ticks": 1})
        world, warnings = parse_world(raw)
        self.assertEqual(warnings, [])
        self.assertIn("garden", world.action_map()["read"].allowed_nodes)
        self.assertTrue(world.action_map()["read"].available_in("garden"))
        self.assertFalse(hasattr(world.node_map()["garden"], "allowed_actions"))

    def test_global_actions_ignore_the_node_side_list(self):
        raw = default_world()
        raw["nodes"][0]["allowed_actions"] = ["say", "think"]  # 全是全局动作
        world, _warnings = parse_world(raw)
        say = world.action_map()["say"]
        self.assertEqual(say.scope, "global")
        self.assertEqual(say.allowed_nodes, [])

    def test_disabled_action_disappears_everywhere(self):
        raw = default_world()
        for action in raw["actions"]:
            if action["id"] == "read":
                action["enabled"] = False
        world, _warnings = parse_world(raw)
        read = world.action_map()["read"]
        self.assertFalse(read.enabled)
        self.assertFalse(read.available_in("study"))
        self.assertNotIn("read", [action.id for action in world.actions_in("study")])


class TestCrossZoneTravel(unittest.TestCase):
    def setUp(self) -> None:
        self.world, _warnings = parse_world(world_with_park())
        self.graph = self.world.adjacent()

    def test_portal_counts_as_a_normal_edge(self):
        """跨区走的是"区域内 → 门户 → 目标区域"，总耗时就是各段相加。"""

        cost = travel_cost(self.graph, "bedroom", "park_square")
        self.assertIsNotNone(cost)
        # 卧室 → 大厅（1）+ 门户（2）+ 北门 → 鸽子广场（1）
        self.assertEqual(cost, 4)

    def test_world_map_graph_is_zone_level(self):
        zone_graph = self.world.zone_adjacent()
        self.assertIn(("park", 2), zone_graph["home"])
        self.assertIn(("home", 2), zone_graph["park"])

    def test_reach_table_groups_by_zone(self):
        builder = PromptBuilder(self.world, tick_seconds=60.0)
        text = builder.reach_table("bedroom")
        self.assertIn("【家中】", text)
        self.assertIn("【街心公园】", text)
        # 户外区域排在家中之后，并且带上区域说明
        self.assertLess(text.index("【家中】"), text.index("【街心公园】"))
        self.assertIn("外面人来人往", text)
        self.assertIn("鸽子广场 park_square：4 tick", text)

    def test_scene_layer_shows_the_zone(self):
        builder = PromptBuilder(self.world, tick_seconds=60.0)
        text = builder.scene_layer(self.world.node_map()["park_gate"], {})
        self.assertIn("街心公园 · 北门", text)
        self.assertIn("id：park_gate", text)


if __name__ == "__main__":
    unittest.main()
