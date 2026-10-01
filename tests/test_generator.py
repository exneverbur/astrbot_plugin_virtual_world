"""批量生成动作 / 地点：解析、校验、摆位与连线。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.generator import (  # noqa: E402
    auto_layout,
    clamp_node_count,
    clamp_per_node,
    link_plan,
    parse_generated_actions,
    parse_generated_nodes,
)

ACTIONS_JSON = """
好的，这是给你的动作：
```json
{
  "actions": [
    {
      "id": "balcony_water_flowers",
      "node_id": "balcony",
      "name": "给花浇水",
      "description": "把阳台上的花浇一遍",
      "category": "continuous",
      "llm_level": "single",
      "visible": true,
      "duration_mode": "llm",
      "duration_min": 300,
      "duration_max": 900,
      "on_complete": {
        "trigger": "llm_followup",
        "prompt_hint": "随口说说花的样子",
        "effects": {"boredom": "-0.05", "affect": "0.06", "mood": "满足"}
      }
    },
    {
      "id": "查天气",
      "node_id": "balcony",
      "name": "查天气",
      "llm_level": "tool",
      "tool_name": "not_registered"
    }
  ]
}
```
"""


class TestParseGeneratedActions(unittest.TestCase):
    def test_valid_action_is_normalised(self):
        actions, problems = parse_generated_actions(
            ACTIONS_JSON, node_ids=["balcony"], tool_names=set()
        )
        self.assertTrue(actions, problems)
        first = actions[0]
        self.assertEqual(first["id"], "balcony_water_flowers")
        self.assertEqual(first["scope"], "node")
        self.assertEqual(first["allowed_nodes"], ["balcony"])
        self.assertTrue(first["enabled"])
        # 效果统一成带符号的写法，心情补 mood: 前缀
        self.assertEqual(first["on_complete"]["effects"]["boredom"], "-0.05")
        self.assertEqual(first["on_complete"]["effects"]["affect"], "+0.06")
        self.assertEqual(first["on_complete"]["effects"]["mood"], "mood:满足")

    def test_intimacy_is_kept_only_when_the_model_means_it(self):
        """生成动作时带上「亲密程度」：写 0 / 不写都当"没提"，交给对照表和分类去判。"""

        text = (
            '[{"id": "hold_hands", "node_id": "balcony", "name": "牵手", '
            '"target_type": "user", "intimacy": 0.6},'
            ' {"id": "water", "node_id": "balcony", "name": "浇水", "intimacy": 0},'
            ' {"id": "lean", "node_id": "balcony", "name": "靠着", "intimacy": "1.4"}]'
        )
        actions, _problems = parse_generated_actions(
            text, node_ids=["balcony"], tool_names=set()
        )
        by_id = {item["id"]: item for item in actions}
        self.assertEqual(by_id["hold_hands"]["intimacy"], 0.6)
        self.assertNotIn("intimacy", by_id["water"])
        # 超出范围的夹回 0~1
        self.assertEqual(by_id["lean"]["intimacy"], 1.0)

    def test_bad_id_is_skipped_with_a_reason(self):
        actions, problems = parse_generated_actions(
            ACTIONS_JSON, node_ids=["balcony"], tool_names=set()
        )
        self.assertEqual([item["id"] for item in actions], ["balcony_water_flowers"])
        self.assertTrue(any("不合法" in item for item in problems), problems)

    def test_unknown_tool_is_downgraded(self):
        text = (
            '[{"id": "check_weather", "node_id": "balcony", "name": "查天气", '
            '"llm_level": "tool", "tool_name": "not_registered"}]'
        )
        actions, problems = parse_generated_actions(
            text, node_ids=["balcony"], tool_names={"web_search"}
        )
        self.assertEqual(actions[0]["llm_level"], "single")
        self.assertNotIn("tool_name", actions[0])
        self.assertTrue(any("没注册" in item for item in problems), problems)

    def test_known_tool_is_kept(self):
        text = (
            '[{"id": "check_weather", "node_id": "balcony", "name": "查天气", '
            '"llm_level": "tool", "tool_name": "web_search"}]'
        )
        actions, _problems = parse_generated_actions(
            text, node_ids=["balcony"], tool_names={"web_search"}
        )
        self.assertEqual(actions[0]["llm_level"], "tool")
        self.assertEqual(actions[0]["tool_name"], "web_search")

    def test_unknown_node_is_rejected(self):
        text = '[{"id": "x_action", "node_id": "nowhere", "name": "x"}]'
        actions, problems = parse_generated_actions(text, node_ids=["balcony"])
        self.assertEqual(actions, [])
        self.assertTrue(any("不存在的地点" in item for item in problems), problems)

    def test_per_node_cap_is_respected(self):
        items = ",".join(
            '{"id": "act_%d", "node_id": "balcony", "name": "动作"}' % index
            for index in range(8)
        )
        actions, problems = parse_generated_actions(
            '{"actions": [' + items + "]}",
            node_ids=["balcony"],
            max_per_node=3,
        )
        self.assertEqual(len(actions), 3)
        self.assertTrue(any("上限" in item for item in problems), problems)

    def test_limits_are_clamped(self):
        self.assertEqual(clamp_per_node(99), 5)
        self.assertEqual(clamp_per_node(0), 1)
        self.assertEqual(clamp_node_count(99), 5)
        self.assertEqual(clamp_node_count("abc"), 3)


class TestParseGeneratedNodes(unittest.TestCase):
    def test_nodes_are_parsed_and_slugged(self):
        text = (
            '[{"id": "Sun Corner", "name": "阳光角", "prompt": "晒得到太阳", '
            '"atmosphere": {"calm": 0.7}, "color": "#F5C542"}]'
        )
        nodes, problems = parse_generated_nodes(text, existing_ids={"balcony"})
        self.assertEqual(problems, [])
        self.assertEqual(nodes[0]["id"], "sun_corner")
        self.assertEqual(nodes[0]["name"], "阳光角")
        self.assertEqual(nodes[0]["atmosphere"]["calm"], 0.7)

    def test_duplicated_existing_id_is_skipped(self):
        text = '[{"id": "balcony", "name": "阳台"}]'
        nodes, problems = parse_generated_nodes(text, existing_ids={"balcony"})
        self.assertEqual(nodes, [])
        self.assertTrue(any("重复" in item for item in problems), problems)

    def test_count_is_capped(self):
        text = "[" + ",".join(
            '{"id": "spot_%d", "name": "地点"}' % index for index in range(9)
        ) + "]"
        nodes, problems = parse_generated_nodes(text, max_nodes=5)
        self.assertEqual(len(nodes), 5)
        self.assertTrue(any("上限" in item for item in problems), problems)

    def test_node_gets_a_nickname_text_even_without_the_model(self):
        text = '[{"id": "balcony", "name": "阳台"}, ' \
               '{"id": "roof", "name": "商场顶楼天台花园"}, ' \
               '{"id": "cellar", "name": "地窖", "nickname_text": "在地窖里"}]'
        nodes, _problems = parse_generated_nodes(text)
        self.assertEqual(nodes[0]["nickname_text"], "在阳台")
        self.assertEqual(nodes[1]["nickname_text"], "在商场顶楼")
        self.assertEqual(nodes[2]["nickname_text"], "在地窖里")


class TestGeneratedTools(unittest.TestCase):
    """工具型动作：多个工具要保留，没注册的工具要剔掉并说明。"""

    def test_multi_tool_action_keeps_known_tools_only(self):
        text = (
            '{"actions":[{"id":"read_news","node_id":"balcony","name":"看新闻",'
            '"llm_level":"tool","tool_names":["web_search","not_registered"]}]}'
        )
        actions, problems = parse_generated_actions(
            text, node_ids=["balcony"], tool_names={"web_search"}
        )
        self.assertEqual(actions[0]["llm_level"], "tool")
        self.assertEqual(actions[0]["tool_names"], ["web_search"])
        self.assertEqual(actions[0]["tool_name"], "web_search")
        self.assertTrue(any("not_registered" in item for item in problems), problems)

    def test_tool_action_without_known_tool_falls_back(self):
        text = (
            '{"actions":[{"id":"read_news","node_id":"balcony","name":"看新闻",'
            '"llm_level":"tool","tool_names":["nope"]}]}'
        )
        actions, problems = parse_generated_actions(
            text, node_ids=["balcony"], tool_names={"web_search"}
        )
        self.assertEqual(actions[0]["llm_level"], "single")
        self.assertNotIn("tool_names", actions[0])
        self.assertNotIn("tool_name", actions[0])
        self.assertTrue(any("让她自己说" in item for item in problems), problems)


class TestLayoutAndLinks(unittest.TestCase):
    def test_new_nodes_are_placed_clear_of_existing_ones(self):
        positions = auto_layout([(60, 60), (220, 60)], 3)
        self.assertEqual(len(positions), 3)
        self.assertTrue(all(x >= 280 for x, _y in positions), positions)
        self.assertEqual(len(set(positions)), 3)

    def test_first_node_links_to_the_anchor_then_chains(self):
        self.assertEqual(
            link_plan(["a", "b", "c"], anchor="base"),
            [("base", "a"), ("a", "b"), ("b", "c")],
        )

    def test_without_anchor_only_the_chain_remains(self):
        self.assertEqual(link_plan(["a", "b"]), [("a", "b")])


if __name__ == "__main__":
    unittest.main()
