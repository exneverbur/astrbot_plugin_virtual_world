"""核心纯逻辑单元测试（seam S7~S12）。"""

from __future__ import annotations

import os
import random
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.decider import Decider  # noqa: E402
from core.defaults import default_world  # noqa: E402
from core.engagement import EngagementTracker  # noqa: E402
from core.json_actions import (  # noqa: E402
    extract_json_object,
    parse_action_payload,
    parse_cancel,
    parse_plan_payload,
    strip_reasoning,
)
from core.memory import emotional_weight  # noqa: E402
from core.mood import cell_for, mood_label, style_block  # noqa: E402
from core.models import (  # noqa: E402
    DEFAULT_ECHO_TYPES,
    ECHO_EVENT_TYPES,
    parse_world,
)
from core.nickname import compute_nickname, should_update  # noqa: E402
from core.pathfinding import find_path, path_ticks, travel_cost  # noqa: E402
from core.planner import create_plan  # noqa: E402
from core.prompt import PromptBuilder  # noqa: E402
from core.state import WorldState  # noqa: E402
from core.state_dynamics import StateDynamics  # noqa: E402
from core.tool_policy import is_self_send_tool  # noqa: E402
from core.tool_policy import allowed_tools, node_tool_names, tool_name_matches  # noqa: E402
from core.timeline import build_timeline, render_event  # noqa: E402


def make_world():
    world, warnings = parse_world(default_world())
    assert warnings == [], warnings
    return world


class TestMoodGrid(unittest.TestCase):
    """两轴表达格：九种组合各有心情词与风格约束，且两样东西同源。"""

    def test_every_combination_has_its_own_cell(self):
        keys = set()
        for arousal in (0.1, 0.5, 0.9):
            for valence in (0.1, 0.5, 0.9):
                cell = cell_for(arousal, valence)
                keys.add(cell.key)
                self.assertTrue(cell.mood)
                self.assertTrue(cell.style)
                self.assertGreaterEqual(cell.say_limit, 1)
        self.assertEqual(len(keys), 9)

    def test_boundaries_keep_the_middle_band_wide(self):
        # 0.36 与 0.64 都算中间档，"正常说话"才是常驻状态
        self.assertEqual(cell_for(0.36, 0.5).key, "stirred+neutral")
        self.assertEqual(cell_for(0.64, 0.5).key, "stirred+neutral")
        self.assertEqual(cell_for(0.2, 0.5).key, "calm+neutral")
        self.assertEqual(cell_for(0.8, 0.5).key, "excited+neutral")

    def test_style_block_carries_the_effective_cap(self):
        cell = cell_for(0.2, 0.2)
        text = style_block(cell, say_limit=1, group=True)
        self.assertIn(cell.style, text)
        self.assertIn("最多说 1 条", text)
        self.assertIn("群聊", text)
        self.assertIn("私聊", style_block(cell, say_limit=2, group=False))

    def test_energy_is_only_a_modifier(self):
        self.assertEqual(mood_label(0.2, 0.9, energy=0.8), "舒坦")
        self.assertEqual(mood_label(0.2, 0.9, energy=0.1), "困倦 · 舒坦")


class TestStateDynamics(unittest.TestCase):
    def setUp(self) -> None:
        self.world = make_world()
        # 固定成正午，免得测试结果跟着跑测试的机器时钟漂
        self.dynamics = StateDynamics(self.world.state_dynamics, hour_provider=lambda: 12)
        self.node = self.world.node_map()["bedroom"]

    def set_valence(self, state, value: float) -> float:
        """按「她看到的心情」设值：落点在偏移上，基线仍由五维决定。"""

        self.dynamics.apply_effects(state, {"valence": f"={value}"})
        return state.valence

    def test_energy_decays_over_an_hour(self):
        state = WorldState(session_id="s1", energy=0.6)
        self.dynamics.tick(state, node=self.node, elapsed_seconds=3600, world=self.world)
        # 卧室 calm=0.9，会减缓衰减；无论如何都必须变低且不超过初始值
        self.assertLess(state.energy, 0.6)
        self.assertGreater(state.energy, 0.5)

    def test_sleeping_recovers_energy_and_freezes_others(self):
        state = WorldState(session_id="s1", energy=0.2, loneliness=0.5, state="sleeping")
        self.dynamics.tick(state, node=self.node, elapsed_seconds=3600, world=self.world)
        self.assertGreater(state.energy, 0.2)
        self.assertAlmostEqual(state.loneliness, 0.5, places=4)

    def test_values_are_clamped(self):
        state = WorldState(session_id="s1", energy=0.99, loneliness=0.99, state="sleeping")
        self.dynamics.tick(state, node=self.node, elapsed_seconds=10_000, world=self.world)
        self.assertLessEqual(state.energy, 1.0)
        self.assertGreaterEqual(state.energy, 0.0)

    def test_mood_derivation_priority(self):
        """心情词由情绪两轴派生：心潮管"有多激动"，效价管"激动成什么样"。"""

        state = WorldState(session_id="s1", energy=0.5, affect=0.9, valence=0.2)
        self.assertEqual(self.dynamics.derive_mood(state), "恼火")
        state.valence = 0.8
        self.assertEqual(self.dynamics.derive_mood(state), "兴奋")
        state.affect = 0.5
        state.valence = 0.5
        self.assertEqual(self.dynamics.derive_mood(state), "平静")
        state.valence = 0.1
        self.assertEqual(self.dynamics.derive_mood(state), "不痛快")
        state.affect = 0.2
        state.valence = 0.2
        self.assertEqual(self.dynamics.derive_mood(state), "低落")
        state.valence = 0.9
        self.assertEqual(self.dynamics.derive_mood(state), "舒坦")
        # 精力低只是一个修饰，不另开一格
        state.energy = 0.2
        self.assertEqual(self.dynamics.derive_mood(state), "困倦 · 舒坦")
        # 动作里写的 mood 覆盖期内优先
        state.mood = "温柔"
        state.mood_override_until = state.world_time + 10
        self.assertEqual(self.dynamics.derive_mood(state), "温柔")

    def test_effects_syntax(self):
        state = WorldState(session_id="s1", energy=0.5)
        self.dynamics.apply_effects(
            state,
            {"energy": "=0.9", "mood": "mood:温柔"},
            world=self.world,
        )
        self.assertAlmostEqual(state.energy, 0.9, places=4)
        self.assertEqual(state.mood, "温柔")
        self.assertGreater(state.mood_override_until, state.world_time)
        self.dynamics.apply_effects(state, {"energy": "-0.2"})
        self.assertAlmostEqual(state.energy, 0.7, places=4)
        self.dynamics.apply_effects(state, {"loneliness": "×2"})
        self.assertAlmostEqual(state.loneliness, 1.0, places=4)

    def test_event_effects(self):
        state = WorldState(session_id="s1", affect=0.5, valence=0.5, loneliness=0.5)
        self.dynamics.apply_event(state, "hug_bot")
        self.assertAlmostEqual(state.loneliness, 0.3, places=4)
        # 抱抱把心潮抬高，但有心潮饱和：0.5 时只加 0.15 × (1-0.75×0.5) = 0.09375
        self.assertAlmostEqual(state.affect, 0.59375, places=4)
        # 好事会把心情推好一点（效价由基线 + 偏移给出）
        self.assertGreater(state.valence, 0.5)

    # ---------------- 效价：基线 / 偏移 / 懒衰减 ----------------

    def test_valence_baseline_follows_the_five_dims(self):
        """效价基线是五维的纯函数：累、孤独、无聊都会把它压低。"""

        good = WorldState(session_id="s1", energy=0.8, loneliness=0.2, boredom=0.2)
        bad = WorldState(session_id="s1", energy=0.2, loneliness=0.9, boredom=0.9)
        self.dynamics.refresh(good)
        self.dynamics.refresh(bad)
        self.assertGreater(good.valence, 0.5)
        self.assertLess(bad.valence, 0.4)

    def test_valence_offset_decays_back_to_baseline(self):
        """事件的偏移会自己归零——效价不会长期挂在一个值上。"""

        state = WorldState(session_id="s1", energy=0.5, loneliness=0.2, boredom=0.2)
        self.dynamics.refresh(state)
        self.set_valence(state, 0.9)
        gap = state.valence_offset
        self.assertGreater(gap, 0.3)
        state.affect_synced_at = 1_000_000.0
        self.dynamics.tick(
            state, node=self.node, elapsed_seconds=3600, world=self.world, now=1_003_600.0
        )
        self.assertLess(state.valence_offset, gap * 0.6, state.valence_offset)
        # 再走四个小时：偏移基本归零（基线本身会随五维变化，所以看偏移）
        self.dynamics.tick(
            state, node=self.node, elapsed_seconds=14400, world=self.world, now=1_017_600.0
        )
        self.assertLess(abs(state.valence_offset), 0.03, state.valence_offset)

    def test_emotion_decay_depends_on_real_time_not_tick_length(self):
        """tick 长度改了，情绪曲线形状不该变（情绪按真实时间结算）。"""

        short = WorldState(session_id="s1", affect=0.9)
        long = WorldState(session_id="s1", affect=0.9)
        short.affect_synced_at = long.affect_synced_at = 1_000_000.0
        for step in range(1, 11):  # 10 个 60 秒的 tick
            self.dynamics.tick(
                short,
                node=self.node,
                elapsed_seconds=60,
                world=self.world,
                now=1_000_000.0 + step * 60,
            )
        self.dynamics.tick(  # 1 个 600 秒的 tick
            long, node=self.node, elapsed_seconds=600, world=self.world, now=1_000_600.0
        )
        # 曲线形状一致（非线性衰减是分步走的，允许一点点路径差）
        self.assertLess(abs(short.affect - long.affect), 0.005)

    def test_old_save_does_not_lose_its_mood(self):
        """老存档没有同步时间戳：第一次结算从当下开始，不能按 epoch 倒算。"""

        state = WorldState(session_id="s1", affect=0.9, affect_synced_at=0.0)
        self.dynamics.tick(
            state, node=self.node, elapsed_seconds=60, world=self.world, now=1_700_000_000.0
        )
        self.assertAlmostEqual(state.affect, 0.9, places=4)

    def test_negative_valence_calms_down_faster(self):
        """负效价 + 高心潮：平复得更快（不留一身火气）。"""

        upset = WorldState(session_id="s1", affect=0.9, valence=0.15)
        neutral = WorldState(session_id="s1", affect=0.9, valence=0.5)
        upset.affect_synced_at = neutral.affect_synced_at = 1_000_000.0
        self.dynamics.tick(
            upset, node=self.node, elapsed_seconds=600, world=self.world, now=1_000_600.0
        )
        self.dynamics.tick(
            neutral, node=self.node, elapsed_seconds=600, world=self.world, now=1_000_600.0
        )
        self.assertLess(upset.affect, neutral.affect)

    def test_positive_events_are_amplified_when_she_is_already_happy(self):
        """心情好更容易被逗乐：同一条好消息在高效价时带来的心潮更高。"""

        happy = WorldState(session_id="s1", affect=0.4)
        grumpy = WorldState(session_id="s1", affect=0.4)
        self.set_valence(happy, 0.9)
        self.set_valence(grumpy, 0.1)
        self.dynamics.apply_event(happy, "positive_words")
        self.dynamics.apply_event(grumpy, "positive_words")
        self.assertGreater(happy.affect, grumpy.affect)

    def test_storm_flag_has_hysteresis(self):
        """`storm` 进得难出得也难：不会在阈值边上反复抖。"""

        state = WorldState(session_id="s1", affect=0.8, storm_since=0.0)
        self.set_valence(state, 0.2)
        self.dynamics.apply_event(state, "topic_engaged")  # 触发一次结算，顺手更新标记
        self.assertTrue(state.storm)
        # 掉到进入线以下、但还在退出线以上：标记要保持
        state.affect = 0.6
        self.set_valence(state, 0.45)
        self.dynamics.apply_event(state, "topic_engaged")
        self.assertTrue(state.storm)
        # 掉到退出线以下才清掉
        state.affect = 0.4
        self.dynamics.apply_event(state, "topic_engaged")
        self.assertFalse(state.storm)

    def test_storm_flag_expires_on_its_own(self):
        state = WorldState(session_id="s1", affect=0.9, storm_since=0.0)
        self.set_valence(state, 0.2)
        self.dynamics.apply_event(state, "topic_engaged")
        self.assertTrue(state.storm)
        state.affect_synced_at = 1_000_000.0
        self.dynamics.tick(
            state,
            node=self.node,
            elapsed_seconds=60,
            world=self.world,
            now=1_000_000.0 + 121 * 60,  # 超过最长持续时间
        )
        self.assertFalse(state.storm)

    def test_safety_valve_halves_the_offset(self):
        """长期低落时把偏移减半（拉回基线），并告诉调用方可以记日志。"""

        state = WorldState(session_id="s1", energy=0.5, loneliness=0.2, boredom=0.2)
        self.dynamics.refresh(state)
        self.set_valence(state, 0.05)
        offset = state.valence_offset
        state.affect_synced_at = 1_000_000.0
        state.low_valence_since = 1_000_000.0
        fired = self.dynamics.tick(
            state,
            node=self.node,
            elapsed_seconds=60,
            world=self.world,
            now=1_000_000.0 + 31 * 60,
        )
        self.assertTrue(fired)
        self.assertLess(abs(state.valence_offset), abs(offset) * 0.6)
        self.assertGreater(state.valence, 0.05)
        # 直接验证那一下"减半"本身
        before = state.valence_offset
        state.valence = 0.1
        state.low_valence_since = 1_000_000.0
        self.assertTrue(self.dynamics._safety_valve(state, now=1_000_000.0 + 1801))
        self.assertAlmostEqual(state.valence_offset, before * 0.5, places=6)

    def test_ignored_streak_softens_then_resets(self):
        """被冷落：第一次最疼，之后递减，隔一阵重新算。"""

        state = WorldState(session_id="s1")
        self.assertEqual(self.dynamics.ignored_magnitude(state, now=1000.0), 1.0)
        self.assertEqual(self.dynamics.ignored_magnitude(state, now=1100.0), 0.5)
        self.assertEqual(self.dynamics.ignored_magnitude(state, now=1200.0), 0.0)
        # 30 分钟后重新开始
        self.assertEqual(self.dynamics.ignored_magnitude(state, now=1200.0 + 1801), 1.0)

    def test_affect_rises_with_diminishing_returns(self):
        """心潮越高，同样的刺激加得越少——否则一两轮聊天就顶满。"""

        calm = WorldState(session_id="s1", affect=0.0)
        excited = WorldState(session_id="s1", affect=0.9)
        self.dynamics.apply_event(calm, "topic_engaged")
        self.dynamics.apply_event(excited, "topic_engaged")
        self.assertGreater(calm.affect, excited.affect - 0.9)
        self.assertLess(excited.affect, 1.0)

    def test_repeated_messages_do_not_max_out_immediately(self):
        """连着聊十来句不该把心潮顶满（以前每条消息 +0.08 就会）。"""

        state = WorldState(session_id="s1", affect=0.3)
        for _ in range(12):
            self.dynamics.apply_event(state, "topic_engaged")
        self.assertLess(state.affect, 0.6, state.affect)

    def test_extreme_protection_flags(self):
        state = WorldState(session_id="s1", energy=0.05, world_time=100, low_energy_since=0)
        flags = self.dynamics.check_extremes(state, low_energy_ticks=10, high_loneliness_ticks=10)
        self.assertEqual(flags, [])
        state.world_time = 200
        flags = self.dynamics.check_extremes(state, low_energy_ticks=10, high_loneliness_ticks=10)
        self.assertIn("force_sleep", flags)


class TestEmotionalWeight(unittest.TestCase):
    """情绪越激烈的记忆，写进库里的权重越高（更容易被想起来）。"""

    def test_legacy_social_keys_are_migrated(self):
        """老的 world.json 里叫 social / social_decay_per_min / social_chat_threshold。"""

        world, _warnings = parse_world(
            {
                # 至少要有一个节点，否则 parse_world 会整体回落到默认世界
                "nodes": [{"id": "study", "name": "书房"}],
                "default_state": {"social": 0.42},
                "state_dynamics": {"social_decay_per_min": 0.003},
                "decider": {"social_chat_threshold": 0.33},
            }
        )
        self.assertAlmostEqual(world.default_state.affect, 0.42, places=3)
        self.assertAlmostEqual(world.state_dynamics.affect_decay_per_min, 0.003, places=4)
        self.assertAlmostEqual(world.decider.interject_threshold, 0.33, places=3)

    def test_legacy_state_social_becomes_affect(self):
        state = WorldState.from_payload({"social": 0.8}, "s1")
        self.assertAlmostEqual(state.affect, 0.8, places=4)

    def test_plain_memory_keeps_base_weight(self):
        self.assertAlmostEqual(
            emotional_weight(0.4, emotion="平静", text="在这里说话"),
            0.4,
            places=4,
        )

    def test_intense_words_raise_weight(self):
        calm = emotional_weight(0.4, text="今天天气不错")
        intense = emotional_weight(0.4, text="他说他很难过，我很心疼")
        self.assertGreater(intense, calm)
        self.assertGreaterEqual(intense, 0.6)

    def test_affect_adds_a_little(self):
        # 心情平平的热闹：加得少；心情偏得远时同样的心潮记得更牢
        low = emotional_weight(0.4, affect=0.0, valence=0.5)
        high = emotional_weight(0.4, affect=1.0, valence=0.5)
        self.assertAlmostEqual(high - low, 0.06, places=4)
        upset = emotional_weight(0.4, affect=1.0, valence=0.0)
        self.assertAlmostEqual(upset - low, 0.20, places=4)

    def test_weight_is_clamped(self):
        self.assertLessEqual(emotional_weight(0.95, text="吵架 生气 讨厌 难过"), 1.0)


class TestPathfinding(unittest.TestCase):
    def setUp(self) -> None:
        self.world = make_world()
        self.graph = self.world.adjacent()

    def test_shortest_path_between_rooms(self):
        path = find_path(self.graph, "bedroom", "window")
        self.assertIsNotNone(path)
        self.assertEqual(path[0], "bedroom")
        self.assertEqual(path[-1], "window")
        self.assertLessEqual(len(path), 5)

    def test_path_ticks(self):
        path = find_path(self.graph, "bedroom", "study")
        self.assertIsNotNone(path)
        self.assertGreaterEqual(path_ticks(self.graph, path), 1)

    def test_travel_cost_same_node(self):
        self.assertEqual(travel_cost(self.graph, "bedroom", "bedroom"), 0)

    def test_unreachable_returns_none(self):
        graph = {"a": [("b", 1)], "b": [("a", 1)], "c": []}
        self.assertIsNone(find_path(graph, "a", "c"))


class TestJsonActions(unittest.TestCase):
    def test_valid_payload(self):
        result = parse_action_payload(
            '{"actions":[{"type":"say","messages":["早","在忙"]},{"type":"think","content":"想他"}]}',
            available_actions={"say", "think"},
        )
        # think 一律被移动到最前面（先想后说）
        self.assertEqual([a.type for a in result.actions], ["think", "say"])
        self.assertFalse(result.fallback_used)
        self.assertEqual(result.actions[1].messages, ["早", "在忙"])

    def test_reasoning_is_parsed(self):
        payload = (
            '{"reasoning":{"env":"书房","state":"刚睡醒","mood":"困倦",'
            '"who":"小明在问周末安排","intent":"敷衍两句再去泡茶"},'
            '"actions":[{"type":"say","messages":["唔……周末啊"]}]}'
        )
        result = parse_action_payload(payload, available_actions={"say"})
        self.assertEqual(result.reasoning["env"], "书房")
        self.assertEqual(result.reasoning["who"], "小明在问周末安排")
        self.assertEqual([a.type for a in result.actions], ["say"])

    def test_reasoning_accepts_plain_string(self):
        result = parse_action_payload(
            '{"reasoning":"先看看情况","actions":[{"type":"say","messages":["嗯"]}]}',
            available_actions={"say"},
        )
        self.assertEqual(result.reasoning.get("intent"), "先看看情况")

    def test_tool_action_has_no_required_params_anymore(self):
        result = parse_action_payload(
            '{"reasoning":{"intent":"查一下"},"actions":[{"type":"search_web"}]}',
            available_actions={"search_web"},
        )
        self.assertEqual([a.type for a in result.actions], ["search_web"])

    def test_code_fence_is_tolerated(self):
        text = '```json\n{"actions":[{"type":"say","messages":["hi"]}]}\n```'
        result = parse_action_payload(text, available_actions={"say"})
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0].messages, ["hi"])

    def test_invalid_json_falls_back_to_say(self):
        result = parse_action_payload("我今天有点困", available_actions={"say"})
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.actions[0].type, "say")
        self.assertEqual(result.actions[0].messages, ["我今天有点困"])

    def test_unavailable_action_is_dropped(self):
        result = parse_action_payload(
            '{"actions":[{"type":"fly","messages":["x"]},{"type":"say","messages":["ok"]}]}',
            available_actions={"say"},
        )
        self.assertEqual([a.type for a in result.actions], ["say"])
        self.assertTrue(any("不可用" in w for w in result.warnings))

    def test_messages_are_truncated(self):
        result = parse_action_payload(
            '{"actions":[{"type":"say","messages":["1","2","3","4","5"]}]}',
            available_actions={"say"},
            max_messages=2,
        )
        self.assertEqual(result.actions[0].messages, ["1", "2"])

    def test_unknown_target_node_is_dropped(self):
        result = parse_action_payload(
            '{"actions":[{"type":"walk_to","target_node":"mars"}]}',
            available_actions={"walk_to"},
            valid_nodes={"bedroom", "lobby"},
        )
        self.assertEqual(result.actions, [])

    def test_tool_action_without_params_is_kept_silently(self):
        """只给 intent、不给 params 是正常的：参数由辅助模型在调用前补全，解析阶段不必报警。"""

        result = parse_action_payload(
            '{"actions":[{"type":"search_web","intent":"今天的新闻"}]}',
            available_actions={"search_web"},
        )
        self.assertEqual([a.type for a in result.actions], ["search_web"])
        self.assertEqual(result.actions[0].intent, "今天的新闻")
        self.assertFalse([w for w in result.warnings if "参数" in w], result.warnings)

    def test_tool_action_duration_is_parsed(self):
        """时长由大模型给的 duration 决定（配合 duration_mode=llm）。"""

        result = parse_action_payload(
            '{"actions":[{"type":"nap","duration":1200}]}',
            available_actions={"nap"},
        )
        self.assertEqual(result.actions[0].duration, 1200)

    def test_plan_payload(self):
        plan, warnings = parse_plan_payload(
            '{"plan":[{"action":"walk_to","target_node":"window"},'
            '{"action":"stare","duration":600}],"valid_until":1800,"reason":"孤独"}',
            available_actions={"walk_to", "stare"},
            valid_nodes={"window", "bedroom"},
        )
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["steps"]), 2)
        self.assertEqual(plan["valid_for"], 1800)
        self.assertEqual(warnings, [])

    def test_plan_payload_rejects_unknown_action(self):
        plan, warnings = parse_plan_payload(
            '{"plan":[{"action":"teleport"}]}',
            available_actions={"walk_to"},
        )
        self.assertIsNone(plan)
        self.assertTrue(warnings)


class TestReasoningStripping(unittest.TestCase):
    """模型偶尔会吐思考段（有时还漏掉开标签）：不能进群，也不能挡着 JSON 解析。"""

    def test_unclosed_close_tag_is_dropped(self):
        raw = (
            "The user said something. Let me think about how to answer. "
            'Let me write {"type":"say"} as an example.</thinking>\n'
            '{"actions":[{"type":"say","messages":["早～"]}]}\n[好感度 持平]'
        )
        cleaned = strip_reasoning(raw)
        self.assertNotIn("Let me think", cleaned)
        self.assertNotIn("好感度", cleaned.split("\n")[0])
        payload = extract_json_object(raw)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["actions"][0]["messages"], ["早～"])

    def test_complete_thinking_block_is_dropped(self):
        raw = '<thinking>想一下</thinking>{"actions":[{"type":"say","messages":["在"]}]}'
        self.assertEqual(
            extract_json_object(raw)["actions"][0]["messages"], ["在"]
        )
        self.assertNotIn("想一下", strip_reasoning(raw))

    def test_fenced_thinking_block_is_dropped(self):
        raw = '```thinking\n想一下\n```\n{"actions":[{"type":"say","messages":["在"]}]}'
        self.assertEqual(
            extract_json_object(raw)["actions"][0]["messages"], ["在"]
        )

    def test_open_tag_without_close_keeps_the_json(self):
        raw = '<thinking>\n还在想…\n{"actions":[{"type":"say","messages":["在"]}]}'
        self.assertEqual(
            extract_json_object(raw)["actions"][0]["messages"], ["在"]
        )

    def test_action_payload_uses_the_json_after_the_reasoning(self):
        raw = (
            "Let me think. Example: {\"type\":\"say\",\"messages\":[\"x\"]}. "
            "</thinking>\n"
            '{"reasoning":{"intent":"打个招呼"},"actions":[{"type":"say","messages":["早～"]}]}'
        )
        result = parse_action_payload(raw, available_actions={"say"})
        self.assertFalse(result.fallback_used)
        self.assertEqual(result.actions[0].messages, ["早～"])
        self.assertEqual(result.reasoning.get("intent"), "打个招呼")

    def test_long_reasoning_without_json_is_not_sent(self):
        """整段思考、没有 JSON：宁可什么都不发，也别把思考发到群里。"""

        raw = ("I need to decide how to answer this. " * 20) + "</thinking>"
        result = parse_action_payload(raw, available_actions={"say"})
        self.assertEqual(result.actions, [])
        self.assertTrue(result.fallback_used)
        self.assertIn("已忽略", result.warnings[0])

    def test_short_plain_reply_still_goes_out(self):
        """模型不写 JSON、直接说了句短话时，老行为要保留。"""

        result = parse_action_payload("在的呀～", available_actions={"say"})
        self.assertEqual(result.actions[0].messages, ["在的呀～"])
        self.assertTrue(result.fallback_used)

    def test_plan_payload_ignores_reasoning_prefix(self):
        raw = "先想想。</thinking>{\"plan\":[{\"action\":\"say\"}]}"
        plan, warnings = parse_plan_payload(raw, available_actions={"say"})
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["steps"]), 1)


class TestCancelParsing(unittest.TestCase):
    """模型写的 cancel 字段：只认两档，别的写法当没写。"""

    def test_two_modes_and_aliases(self):
        self.assertEqual(parse_cancel("now"), "now")
        self.assertEqual(parse_cancel("立刻"), "now")
        self.assertEqual(parse_cancel("queue"), "queue")
        self.assertEqual(parse_cancel({"mode": "queue"}), "queue")
        self.assertEqual(parse_cancel(""), "")
        self.assertEqual(parse_cancel("maybe"), "")
        self.assertEqual(parse_cancel(None), "")

    def test_action_payload_carries_cancel(self):
        result = parse_action_payload(
            '{"cancel": "now", "actions": [{"type": "say", "messages": ["行吧"]}]}',
            available_actions={"say"},
        )
        self.assertEqual(result.cancel, "now")
        self.assertEqual([item.type for item in result.actions], ["say"])


class TestNickname(unittest.TestCase):
    def setUp(self) -> None:
        self.world = make_world()
        self.bedroom = self.world.node_map()["bedroom"]
        self.lobby = self.world.node_map()["lobby"]

    def test_action_text_beats_where_she_is(self):
        """「在做什么」优先于「在哪儿」：睡觉时名片该写睡觉而不是在卧室。"""

        state = WorldState(
            session_id="s1",
            state="sleeping",
            current_action={"type": "sleep"},
            bot_base_nickname="小鲸鱼",
        )
        self.assertEqual(compute_nickname(self.world, state, self.bedroom), "小鲸鱼 | 睡觉中")

    def test_node_text_when_she_is_just_there(self):
        """文案写在地点上：人在书房、又没在做带文案的动作时就显示它。"""

        state = WorldState(session_id="s1", state="idle", bot_base_nickname="小鲸鱼")
        self.assertEqual(
            compute_nickname(self.world, state, self.world.node_map()["study"]),
            "小鲸鱼 | 在书房",
        )

    def test_legacy_maps_still_work_as_fallback(self):
        """老配置里的「状态 / 地点 → 文案」两张表还认，只是不再出现在编辑器里。"""

        raw = default_world()
        raw["nickname_sync"]["status_map"] = {"pondering": "发呆中"}
        raw["nickname_sync"]["node_status"] = {"attic": "在阁楼"}
        world, _warnings = parse_world(raw)
        state = WorldState(session_id="s1", state="pondering", bot_base_nickname="小鲸鱼")
        self.assertEqual(compute_nickname(world, state, None), "小鲸鱼 | 发呆中")

    def test_plain_name_when_nothing_matches(self):
        state = WorldState(session_id="s1", state="idle", bot_base_nickname="小鲸鱼")
        self.assertEqual(compute_nickname(self.world, state, self.lobby), "小鲸鱼")

    def test_locked_nickname_wins(self):
        state = WorldState(
            session_id="s1",
            state="sleeping",
            bot_base_nickname="小鲸鱼",
            bot_current_nickname="别改我",
            bot_nickname_locked=True,
        )
        self.assertEqual(compute_nickname(self.world, state, self.bedroom), "别改我")

    def test_cooldown_blocks_update(self):
        state = WorldState(session_id="s1", bot_current_nickname="旧", last_nickname_update_at=100.0)
        self.assertFalse(should_update(state, "新", now=120.0, cooldown_seconds=60))
        self.assertTrue(should_update(state, "新", now=200.0, cooldown_seconds=60))
        self.assertFalse(should_update(state, "旧", now=200.0, cooldown_seconds=60))


class TestEngagement(unittest.TestCase):
    def setUp(self) -> None:
        self.world = make_world()
        self.tracker = EngagementTracker(self.world)

    def test_cooldown_after_threshold(self):
        state = WorldState(session_id="s1", world_time=0)
        self.tracker.on_bot_spoke(state)
        self.assertTrue(self.tracker.can_speak(state))
        # 跨过静默窗口 -> 计数增加
        state.world_time = 61
        verdict = self.tracker.evaluate(state, tick_seconds=60)
        self.assertEqual(verdict.unanswered_count, 1)
        state.world_time = 121
        self.tracker.evaluate(state, tick_seconds=60)
        state.world_time = 181
        verdict = self.tracker.evaluate(state, tick_seconds=60)
        self.assertEqual(verdict.unanswered_count, 3)
        self.assertTrue(verdict.in_cooldown)
        self.assertFalse(self.tracker.can_speak(state))

    def test_user_reply_resets_counter(self):
        state = WorldState(session_id="s1", world_time=10, unanswered_count=2)
        self.tracker.on_user_replied(state)
        self.assertEqual(state.unanswered_count, 0)

    def test_hint_only_after_unanswered(self):
        state = WorldState(session_id="s1", world_time=10, unanswered_count=0)
        self.assertEqual(self.tracker.hint(state), "")
        state.unanswered_count = 2
        self.assertIn("2 次", self.tracker.hint(state))


class TestDecider(unittest.TestCase):
    def setUp(self) -> None:
        self.world = make_world()
        self.decider = Decider(self.world)

    def test_low_energy_goes_to_bedroom_and_sleeps(self):
        state = WorldState(session_id="s1", node_id="study", energy=0.1, world_time=10)
        plan = self.decider.rule_plan(state)
        self.assertIsNotNone(plan)
        actions = [step["action"] for step in plan["steps"]]
        self.assertIn("sleep", actions)
        self.assertEqual(plan["source"], "rule")

    def test_lonely_goes_to_lobby(self):
        state = WorldState(session_id="s1", node_id="bedroom", loneliness=0.9, world_time=10)
        plan = self.decider.rule_plan(state)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["steps"][0]["action"], "walk_to")
        self.assertEqual(plan["steps"][0]["target_node"], "lobby")

    def test_curious_in_study_searches(self):
        state = WorldState(
            session_id="s1", node_id="study", curiosity=0.9, boredom=0.1, world_time=10
        )
        plan = self.decider.rule_plan(state)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["steps"][0]["action"], "search_web")

    def test_bored_wanders(self):
        state = WorldState(
            session_id="s1", node_id="bedroom", boredom=0.95, energy=0.6, loneliness=0.2
        )
        plan = self.decider.rule_plan(state)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["steps"][-1]["action"], "stare")

    def test_forced_plan_for_low_energy(self):
        state = WorldState(session_id="s1", node_id="window", energy=0.05)
        plan = self.decider.forced_plan(state, "force_sleep")
        self.assertIsNotNone(plan)
        self.assertEqual(plan["source"], "extreme")

    def test_needs_plan_false_when_action_running(self):
        state = WorldState(session_id="s1", current_action={"type": "read"})
        self.assertFalse(self.decider.needs_plan(state))

    def test_loneliness_interject_rule(self):
        """孤独感高 + 群里在聊 -> 主动插话；群里安静或冷却中则不插话。"""

        state = WorldState(session_id="s1", node_id="study", loneliness=0.9, world_time=10)
        plan = self.decider.rule_plan(state, group_chatting=True, interject_allowed=True)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["steps"][0]["action"], "say")
        self.assertTrue(plan["steps"][0]["interject"])

        quiet = self.decider.rule_plan(state, group_chatting=False, interject_allowed=True)
        self.assertFalse(quiet and quiet["steps"][0].get("interject"))

        cooling = self.decider.rule_plan(state, group_chatting=True, interject_allowed=False)
        self.assertFalse(cooling and cooling["steps"][0].get("interject"))

    def test_low_loneliness_does_not_interject(self):
        state = WorldState(session_id="s1", node_id="study", loneliness=0.2, world_time=10)
        plan = self.decider.rule_plan(state, group_chatting=True, interject_allowed=True)
        self.assertFalse(plan and plan["steps"][0].get("interject"))

    def test_interject_has_two_motives(self):
        """插话有两条理由：想被注意到（孤独）和想发作（高心潮 + 差心情）。"""

        # 心情不错 + 孤独：动机 A
        cheerful = WorldState(
            session_id="s1", node_id="study", loneliness=0.9, valence=0.6, affect=0.2
        )
        self.assertEqual(self.decider.interject_motive(cheerful), "群里正聊得热闹，想接一句")
        # 心情差 + 情绪上来了：动机 B，即使一点也不孤独
        grumpy = WorldState(
            session_id="s1", node_id="study", loneliness=0.1, valence=0.2, affect=0.8
        )
        self.assertEqual(self.decider.interject_motive(grumpy), "情绪上来了，忍不住想插一句")
        # 心情差但情绪很平：谁都不想理
        flat = WorldState(
            session_id="s1", node_id="study", loneliness=0.9, valence=0.2, affect=0.2
        )
        self.assertEqual(self.decider.interject_motive(flat), "")

    def test_interject_threshold_floats_with_mood(self):
        """心情越差越难开口：阈值浮动，但"开口后什么样"归风格格管。"""

        base = WorldState(session_id="s1", valence=0.5)
        self.assertAlmostEqual(self.decider.interject_threshold(base), 0.6, places=4)
        low = WorldState(session_id="s1", valence=0.2)
        self.assertAlmostEqual(self.decider.interject_threshold(low), 0.7, places=4)
        closed = WorldState(session_id="s1", valence=0.2)
        closed.interject_closed_until = self.decider._now() + 600
        self.assertAlmostEqual(self.decider.interject_threshold(closed), 0.75, places=4)

    def test_low_mood_lowers_blocks_for_self_care(self):
        """低落时更容易选择"缓一缓"：发呆 / 看书的门槛下调。"""

        bored = WorldState(
            session_id="s1", node_id="bedroom", boredom=0.6, valence=0.3, energy=0.6
        )
        plan = self.decider.rule_plan(bored)
        self.assertIsNotNone(plan)
        self.assertIn(plan["steps"][-1]["action"], ("stare", "read"))
        # 心情正常时同样的无聊还不到发呆线（0.8），也不会去看书（0.45）——看书线还是够的
        happy = WorldState(
            session_id="s1", node_id="bedroom", boredom=0.5, valence=0.6, energy=0.6
        )
        plan2 = self.decider.rule_plan(happy)
        self.assertTrue(plan2 is None or plan2["steps"][-1]["action"] in ("stare", "read"))


class TestPlanKeepsIntent(unittest.TestCase):
    """计划不能把工具型动作的 intent 吃掉——丢了它，参数就补不出来。"""

    def test_create_plan_keeps_intent(self):
        plan = create_plan(
            steps=[
                {
                    "action": "search_web",
                    "intent": "今天的新闻",
                    "params": {},
                    "duration": 0,
                }
            ],
            world_time=10,
            reason="测试",
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan["steps"][0]["intent"], "今天的新闻")

    def test_parse_plan_payload_keeps_intent(self):
        plan, _warnings = parse_plan_payload(
            '{"plan":[{"action":"search_web","intent":"今天的新闻"}]}',
            available_actions={"search_web"},
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan["steps"][0]["intent"], "今天的新闻")


class TestDecisionSampleRate(unittest.TestCase):
    """「要不要问大模型」的概率跟着决策意愿走，而不是写死 10%。"""

    def setUp(self) -> None:
        self.world = make_world()
        self.decider = Decider(self.world)

    def _state(self, **fields) -> WorldState:
        base = {"session_id": "s1", "node_id": "study", "energy": 0.8, "loneliness": 0.2,
                "boredom": 0.1, "curiosity": 0.2, "affect": 0.1}
        base.update(fields)
        return WorldState(**base)

    def test_rate_stays_between_configured_bounds(self):
        for state in (
            self._state(),
            self._state(loneliness=1.0, boredom=1.0, curiosity=1.0, affect=1.0, energy=0.0),
        ):
            rate = self.decider.llm_sample_rate(state)
            self.assertGreaterEqual(rate, self.world.decider.llm_rate_min - 1e-9)
            self.assertLessEqual(rate, self.world.decider.llm_rate_max + 1e-9)

    def test_rate_grows_with_willingness(self):
        calm = self.decider.llm_sample_rate(self._state())
        restless = self.decider.llm_sample_rate(
            self._state(loneliness=0.9, boredom=0.9, affect=0.8)
        )
        self.assertGreater(restless, calm)

    def test_tired_and_ignored_lower_the_rate(self):
        base = self._state(loneliness=0.9, boredom=0.9)
        tired = self._state(loneliness=0.9, boredom=0.9, energy=0.05)
        ignored = self._state(loneliness=0.9, boredom=0.9, unanswered_count=3)
        self.assertLess(self.decider.llm_sample_rate(tired), self.decider.llm_sample_rate(base))
        self.assertLess(self.decider.llm_sample_rate(ignored), self.decider.llm_sample_rate(base))

    def test_sleeping_never_asks(self):
        state = self._state(state="sleeping")
        self.assertEqual(self.decider.llm_sample_rate(state), self.world.decider.llm_rate_min)

    def test_should_ask_llm_follows_the_rate(self):
        state = self._state(loneliness=0.0, boredom=0.0, curiosity=0.0, affect=0.0, energy=1.0)
        # 意愿为 0 时概率取下限：用一个刚好高于下限的随机数，就不该被抽中
        low = self.world.decider.llm_rate_min
        decider = Decider(self.world, rng=random.Random())
        decider.rng.random = lambda: low + 0.01
        self.assertFalse(decider.should_ask_llm(state))
        decider.rng.random = lambda: low - 0.01
        self.assertTrue(decider.should_ask_llm(state))


class TestTimeline(unittest.TestCase):
    """日志页的渲染：把事件翻译成"她在做什么、为什么"。"""

    def setUp(self) -> None:
        self.world = make_world()

    def test_tool_event_shows_result_and_failure(self):
        call = render_event(
            {
                "event_type": "tool_call",
                "detail": {
                    "action": "search_web",
                    "tool": "probe_search",
                    "params": {"query": "今天的新闻"},
                },
            },
            self.world,
        )
        self.assertIn("probe_search", call)
        self.assertIn("今天的新闻", call)

        ok = render_event(
            {
                "event_type": "tool_result",
                "detail": {
                    "action": "search_web",
                    "tool": "probe_search",
                    "ok": True,
                    "result": "今天有三条科技新闻",
                },
            },
            self.world,
        )
        self.assertIn("probe_search", ok)
        self.assertIn("今天有三条科技新闻", ok)

        failed = render_event(
            {
                "event_type": "tool_result",
                "detail": {
                    "action": "search_web",
                    "tool": "probe_search",
                    "ok": False,
                    "error": "调用出错：模拟的搜索服务超时",
                },
            },
            self.world,
        )
        self.assertIn("没成功", failed)
        self.assertIn("超时", failed)

        # 精简模式：只留"调了哪个工具"，参数和结果都不发
        compact = render_event(
            {
                "event_type": "tool_call",
                "detail": {
                    "action": "search_web",
                    "tool": "probe_search",
                    "params": {"query": "今天的新闻"},
                },
            },
            self.world,
            compact=True,
        )
        self.assertEqual(compact, "调用「probe_search」")

    def test_reply_shows_reasoning(self):
        event = {
            "event_type": "reply",
            "detail": {
                "user": "小明",
                "messages": ["周末啊……还没想好"],
                "reasoning": {"who": "小明在问我周末", "intent": "敷衍两句"},
            },
        }
        text = render_event(event, self.world)
        self.assertIn("小明", text)
        self.assertIn("周末啊", text)
        self.assertIn("敷衍两句", text)

    def test_plan_shows_steps_reason_and_source(self):
        event = {
            "event_type": "plan",
            "detail": {
                "reason": "孤独感偏高",
                "source": "rule",
                "steps": [{"action": "walk_to", "target_node": "lobby"}, {"action": "say"}],
            },
        }
        text = render_event(event, self.world)
        self.assertIn("孤独感偏高", text)
        self.assertIn("规则", text)
        self.assertIn("移动", text)  # 动作 id 被翻译成中文名
        self.assertIn("说话", text)

    def test_action_shows_content(self):
        event = {
            "event_type": "action",
            "detail": {"type": "think", "content": "今天有点想他", "visible": False},
        }
        text = render_event(event, self.world)
        self.assertIn("今天有点想他", text)
        self.assertIn("想事情", text)

    def test_skip_and_unknown_type(self):
        self.assertIn("跳过", render_event({"event_type": "skip", "detail": {"note": "缺少工具"}}))
        self.assertIn("x=1", render_event({"event_type": "weird", "detail": {"x": 1}}))

    def test_build_timeline_adds_text(self):
        items = build_timeline(
            [{"event_type": "move", "detail": {"to": "kitchen"}}], self.world
        )
        self.assertIn("厨房", items[0]["text"])


class TestPronoun(unittest.TestCase):
    def test_pronoun_mapping(self):
        from core.models import pronoun_for

        self.assertEqual(pronoun_for("female"), "她")
        self.assertEqual(pronoun_for("male"), "他")
        self.assertEqual(pronoun_for("other"), "ta")
        self.assertEqual(pronoun_for(None), "ta")
        self.assertEqual(pronoun_for("unknown-value"), "ta")


class TestEdgeSerialization(unittest.TestCase):
    """连线的起点字段必须一直是 `from`：
    模型字段名是 from_（from 是关键字），前端和文件里读的都是 from。"""

    def test_dump_with_alias_keeps_from(self):
        world, _warnings = parse_world(default_world())
        dumped = world.model_dump(mode="json", by_alias=True)
        first = dumped["edges"][0]
        self.assertIn("from", first)
        self.assertNotIn("from_", first)

    def test_parse_accepts_both_spellings(self):
        data = default_world()
        for edge in data["edges"]:
            edge["from_"] = edge.pop("from")
        world, _warnings = parse_world(data)
        self.assertTrue(world.edges)
        self.assertEqual(world.edges[0].from_, "bedroom")
        # 多余字段不该被留在配置里（否则每次保存都会把它写回文件）
        self.assertNotIn("from_", world.model_dump(mode="json", by_alias=True)["edges"][0])

    def test_round_trip_keeps_map_connected(self):
        world, _warnings = parse_world(default_world())
        dumped = world.model_dump(mode="json", by_alias=True)
        again, _warnings2 = parse_world(dumped)
        self.assertEqual(len(again.edges), len(world.edges))
        self.assertEqual(
            [(edge.from_, edge.to) for edge in again.edges],
            [(edge.from_, edge.to) for edge in world.edges],
        )
        graph = again.adjacent()
        self.assertTrue(graph["study"], "书房应该还连着别的地方")


class TestToolScopeFromActions(unittest.TestCase):
    """工具只挂在动作上：节点不再有工具白名单，前置条件也不再要求工具。"""

    def test_tools_are_derived_from_node_actions(self):
        world, _warnings = parse_world(default_world())
        # 内置搜索 / 查天气还带一串备选工具（本机装了哪个就用哪个），都算这个地点能用的
        self.assertEqual(
            node_tool_names(world, "study"),
            {
                "web_search",
                "anysearch_search",
                "search",
                "tavily_search",
                "get_weather",
                "get_current_weather",
                "weather",
            },
        )
        self.assertEqual(node_tool_names(world, "bedroom"), set())
        bedroom = world.node_map()["bedroom"]
        self.assertEqual(allowed_tools(world, bedroom), set())

    def test_global_tools_apply_everywhere(self):
        data = default_world()
        data["global_allowed_tools"] = ["always_on"]
        world, _warnings = parse_world(data)
        self.assertIn("always_on", allowed_tools(world, world.node_map()["bedroom"]))

    def test_deprecated_keys_are_dropped(self):
        data = default_world()
        data["nodes"][0]["allowed_tools"] = ["web_search"]
        target = [item for item in data["actions"] if item["id"] == "sing"][0]
        target["tool_name"] = ""
        target.setdefault("preconditions", {})["tool_available"] = ["web_search"]
        world, _warnings = parse_world(data)
        dumped = world.model_dump(mode="json", by_alias=True)
        self.assertNotIn("allowed_tools", dumped["nodes"][0])
        kept = [item for item in dumped["actions"] if item["id"] == target["id"]][0]
        self.assertNotIn("tool_available", kept.get("preconditions") or {})
        # 老写法表达的"需要哪个工具"要迁移成 tool_name，不能就这么丢掉
        self.assertEqual(kept["tool_name"], "web_search")

    def test_tool_action_defaults_name_their_tool(self):
        world, _warnings = parse_world(default_world())
        search = world.action_map()["search_web"]
        weather = world.action_map()["check_weather"]
        # 主选工具没装时才会轮到备选，用户自己挑的名字始终排在最前
        self.assertEqual(search.tool_name, "web_search")
        self.assertIn("anysearch_search", search.tool_fallbacks)
        self.assertEqual(weather.tool_name, "get_weather")
        self.assertIn("get_current_weather", weather.tool_fallbacks)


class TestToolNameMatching(unittest.TestCase):
    """工具名能不能用上：同名算，只配了一个工具时按前缀算。"""

    def test_exact_match(self):
        self.assertTrue(tool_name_matches({"web_search"}, ["web_search"]))

    def test_prefix_matches_official_naming(self):
        # 官方搜索工具叫 web_search_tavily / web_search_anysearch 这类
        self.assertTrue(tool_name_matches({"web_search_tavily"}, ["web_search"]))

    def test_multi_tool_actions_stay_exact(self):
        self.assertFalse(
            tool_name_matches({"web_search_tavily"}, ["web_search", "web_fetch"])
        )

    def test_short_names_are_not_treated_as_patterns(self):
        self.assertFalse(tool_name_matches({"search_plus"}, ["sea"]))

    def test_no_tool_configured(self):
        self.assertFalse(tool_name_matches({"web_search"}, []))


class TestSelfSendToolPolicy(unittest.TestCase):
    """直发消息的工具不该被插件拿去用（会绕过回复管线）。"""

    def test_known_names_are_recognized(self):
        for name in ("send_message_to_user", "Send_Message", "reply_message", "send_msg"):
            self.assertTrue(is_self_send_tool(name), name)
        self.assertFalse(is_self_send_tool("web_search"))

    def test_blank_name_is_not_a_self_send_tool(self):
        self.assertFalse(is_self_send_tool(""))
        self.assertFalse(is_self_send_tool(None))


class TestToolSourceLabel(unittest.TestCase):
    """官方内置工具在编辑器里要带「官方」前缀，别的工具不加。"""

    def test_editor_labels_official_tools(self):
        page = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pages",
            "world_editor",
            "app.js",
        )
        with open(page, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('tool.source === "official" ? "官方 · "', source)

    def test_tool_info_defaults_to_plugin_source(self):
        from core.ports import ToolInfo

        self.assertEqual(ToolInfo(name="x").source, "plugin")


class TestEchoTypeCatalog(unittest.TestCase):
    """调试输出的可勾选类型：前端清单和后端目录必须一致。"""

    def test_editor_lists_every_echoable_type(self):
        import re

        page = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pages",
            "world_editor",
            "app.js",
        )
        with open(page, encoding="utf-8") as handle:
            source = handle.read()
        block = source.split("const ECHO_TYPE_CHOICES = [", 1)[1].split("\n];", 1)[0]
        keys = re.findall(r'key:\s*"([a-z_]+)"', block)
        self.assertEqual(keys, list(ECHO_EVENT_TYPES))

    def test_default_echo_set_is_off_until_someone_picks(self):
        world = make_world()
        self.assertEqual(world.echo_types, [])

    def test_legacy_switch_migrates_to_the_common_set(self):
        raw = default_world()
        raw["echo_actions"] = True
        world, _warnings = parse_world(raw)
        self.assertEqual(world.echo_types, list(DEFAULT_ECHO_TYPES))
        self.assertNotIn("echo_actions", world.model_dump())

    def test_legacy_switch_off_means_nothing_selected(self):
        raw = default_world()
        raw["echo_actions"] = False
        world, _warnings = parse_world(raw)
        self.assertEqual(world.echo_types, [])


class TestSleepConfigDefaults(unittest.TestCase):
    """睡觉时的门禁默认值：回一句固定文案、唤醒词要配 @。"""

    def test_defaults_are_filled_in(self):
        world = make_world()
        self.assertEqual(world.sleep.reply_mode, "template")
        self.assertIn("{bot}", world.sleep.reply_text)
        self.assertTrue(world.sleep.wake_requires_mention)
        self.assertTrue(world.sleep.clear_plan_on_wake)
        self.assertIn("醒醒", world.sleep.wake_words)

    def test_timeline_explains_the_sleep_gate(self):
        said = render_event(
            {
                "event_type": "sleep_reply",
                "detail": {"user": "小明", "text": "zzz…（她在睡觉）"},
            }
        )
        self.assertIn("睡觉", said)
        self.assertIn("固定文案", said)
        skipped = render_event(
            {
                "event_type": "sleep_skip",
                "detail": {"user": "小明", "reason": "她在睡觉，而且这条消息没 @ 她"},
            }
        )
        self.assertIn("没有回复", skipped)


class TestDefaultTextUpgrade(unittest.TestCase):
    """内置动作的默认提示语改过措辞：老配置里还是旧默认值就顺手换掉。"""

    OLD_COOK_HINT = (
        "用第一人称说说刚做好的这道菜：做了什么、闻起来怎么样、想不想分给大家"
    )

    def _world_with_cook_hint(self, hint: str):
        raw = default_world()
        for action in raw["actions"]:
            if action.get("id") == "cook":
                action["on_complete"]["prompt_hint"] = hint
        world, _warnings = parse_world(raw)
        return world.action_map()["cook"].on_complete.prompt_hint

    def test_old_default_hint_is_upgraded(self):
        hint = self._world_with_cook_hint(self.OLD_COOK_HINT)
        self.assertIn("不要招呼或招揽别人来吃", hint)
        self.assertNotEqual(hint, self.OLD_COOK_HINT)

    def test_custom_hint_is_left_alone(self):
        self.assertEqual(self._world_with_cook_hint("照我自己写的说"), "照我自己写的说")

    def test_shipped_default_is_already_the_new_one(self):
        world = make_world()
        self.assertIn("不要招呼", world.action_map()["cook"].on_complete.prompt_hint)

    def test_old_sleep_and_nap_descriptions_are_upgraded(self):
        raw = default_world()
        for action in raw["actions"]:
            if action.get("id") == "sleep":
                action["description"] = "睡一觉恢复精力，需要先回到卧室。"
            if action.get("id") == "nap":
                action["description"] = (
                    "打个盹，睡多久由你自己决定（10 分钟到 1 小时），睡得越久精力恢复越多。"
                )
        world, _warnings = parse_world(raw)
        self.assertIn("夜里或精力见底时用", world.action_map()["sleep"].description)
        self.assertIn("白天犯困时", world.action_map()["nap"].description)

    def test_shipped_sleep_defaults_say_when_to_use_them(self):
        world = make_world()
        self.assertIn("夜里或精力见底时用", world.action_map()["sleep"].description)
        self.assertIn("白天犯困时", world.action_map()["nap"].description)


class TestPromptBuilder(unittest.TestCase):
    def setUp(self) -> None:
        self.world = make_world()
        self.builder = PromptBuilder(self.world)

    def test_injection_contains_scene_and_state(self):
        state = WorldState(session_id="s1", node_id="study", energy=0.42, mood="好奇")
        text = self.builder.build_injection(state, node=self.world.node_map()["study"])
        self.assertIn("书房", text)
        self.assertIn("精力 0.42", text)
        self.assertIn("0~1", text)
        self.assertIn("虚拟世界状态", text)
        self.assertNotIn("actions", text)

    def test_autonomous_prompt_has_five_layers_and_json_contract(self):
        state = WorldState(session_id="s1", node_id="study")
        text = self.builder.build_autonomous_system_prompt(
            persona_text="你是温柔少女",
            state=state,
            node=self.world.node_map()["study"],
            available_tools={"web_search": "搜索网页"},
        )
        for marker in ("第 1 层", "第 2 层", "第 3 层", "第 4 层", "第 5 层"):
            self.assertIn(marker, text)
        self.assertIn("你是温柔少女", text)
        self.assertIn('"actions"', text)

    def test_reach_table_lists_every_node_with_distance(self):
        """只能看到相邻地点时，模型不知道远处有什么、要走多久、id 叫什么。"""

        text = self.builder.reach_table("study")
        for node in self.world.nodes:
            self.assertIn(node.id, text, node.id)
        self.assertIn("你现在在这里", text)
        self.assertIn("tick", text)
        self.assertIn("walk_to", text)
        # 远处地点也要给出耗时，不能只列邻居
        self.assertIn("厨房 kitchen", text)

    def test_stable_layers_come_before_volatile_ones(self):
        """固定内容在前、易变内容在后：前面那截才能被前缀缓存命中。"""

        state = WorldState(session_id="s1", node_id="study")
        text = self.builder.build_autonomous_system_prompt(
            persona_text="你是温柔少女",
            state=state,
            node=self.world.node_map()["study"],
            available_tools={"web_search": "搜索网页"},
        )
        order = [
            text.index("第 1 层：你是谁"),
            text.index("第 2 层：世界规则"),
            text.index("第 3 层：输出格式"),
            text.index("第 4 层：当前场景"),
            text.index("第 5 层：运行时状态"),
        ]
        self.assertEqual(order, sorted(order))
        # 尾巴上留一段近因提醒
        self.assertGreater(text.index("# 最后确认"), order[-1])

    def test_fixed_prefix_survives_state_and_location_changes(self):
        """换了状态、换了地点、换了最近聊天，前三层依然一字不差。"""

        head = "第 1 层：你是谁"
        cut = "第 4 层：当前场景"

        def build(node_id: str, **state_fields) -> str:
            state = WorldState(session_id="s1", node_id=node_id, **state_fields)
            return self.builder.build_autonomous_system_prompt(
                persona_text="你是温柔少女",
                state=state,
                node=self.world.node_map()[node_id],
                available_tools={"web_search": "搜索网页"},
                recent_chat=[
                    {"user_id": "42", "name": "小明", "text": "在吗", "is_self": False}
                ],
            )

        first = build("study", energy=0.9, mood="开心")
        second = build("kitchen", energy=0.2, mood="困")
        prefix_first = first[first.index(head) : first.index(cut)]
        prefix_second = second[second.index(head) : second.index(cut)]
        self.assertEqual(prefix_first, prefix_second)

    def test_scene_layer_lists_usable_action_types(self):
        """能写哪些 type 随地点变化，所以放在场景层而不是固定格式层。"""

        scene = self.builder.scene_layer(
            self.world.node_map()["study"], {"web_search": "搜索网页"}
        )
        self.assertIn("这一轮你能写的 type 只有：", scene)
        self.assertIn("say", scene)
        self.assertIn("walk_to", scene)
        # 固定格式层不依赖这张地图，所以任何节点名都不该出现在里面
        for node in self.world.nodes:
            self.assertNotIn(node.name, self.builder.format_layer(), node.name)

    def test_prompt_carries_the_clock(self):
        """提示词要告诉她"现在几点、什么时候段"，否则深夜也会挑小睡。"""

        from datetime import datetime

        builder = PromptBuilder(
            self.world, tick_seconds=60.0, now_provider=lambda: datetime(2026, 9, 12, 23, 40)
        )
        state = WorldState(session_id="s1", node_id="bedroom")
        text = builder.build_autonomous_system_prompt(
            persona_text="p",
            state=state,
            node=self.world.node_map()["bedroom"],
            available_tools={},
        )
        self.assertIn("现在是：2026-09-12", text)
        self.assertIn("23:40", text)
        self.assertIn("深夜", text)

    def test_memories_carry_dates_and_run_oldest_to_newest(self):
        import time
        from datetime import datetime

        from core.memory import RecalledMemory

        now = datetime(2026, 9, 12, 12, 0)
        stamps = {
            "上个月想去海边": now.timestamp() - 30 * 86400,
            "三天前聊到换工作": now.timestamp() - 3 * 86400,
            "今天凌晨睡不好": now.timestamp() - 3600,
        }
        memories = [
            RecalledMemory(
                id=index,
                content=text,
                node_id="study",
                memory_type="interaction",
                emotion="",
                weight=0.5,
                score=0.5,
                related_users=[],
                created_at=created,
            )
            for index, (text, created) in enumerate(stamps.items())
        ]
        builder = PromptBuilder(self.world, now_provider=lambda: now)
        text = builder.runtime_layer(
            WorldState(session_id="s1", node_id="study"),
            node=self.world.node_map()["study"],
            memories=memories,
        )
        positions = [text.index(item) for item in stamps]
        self.assertEqual(positions, sorted(positions), text)
        # 记忆的时间标签要带时段：只有日期的话，"早上聊的"和"半夜聊的"长得一样
        self.assertIn("[08-13 中午]", text)
        self.assertIn("[09-12 中午]", text)
        self.assertIn("越靠下的事情发生得越近", text)

    def test_section_index_lists_the_chat_blocks(self):
        """分段索引要能看出群聊那一段在不在——排查"某段没了"用。"""

        from core.prompt import prompt_section_index

        state = WorldState(session_id="s1", node_id="study")
        text = self.builder.build_autonomous_system_prompt(
            persona_text="你是温柔少女",
            state=state,
            node=self.world.node_map()["study"],
            available_tools={},
            recent_chat=[
                {"user_id": "42", "name": "小明", "text": "今天加班好累", "is_self": False},
                {"user_id": "__self__", "name": "她", "text": "辛苦啦", "is_self": True},
            ],
        )
        titles = [item["title"] for item in prompt_section_index(text)]
        self.assertTrue(any("最近在聊什么" in title for title in titles), titles)
        self.assertTrue(any("你的内心活动" in title for title in titles), titles)
        self.assertTrue(all(len(title) <= 20 for title in titles), titles)

    def test_prompt_keeps_the_last_generated_plan(self):
        state = WorldState(session_id="s1", node_id="bedroom")
        state.last_plan = {
            "steps": [{"action": "walk_to", "target_node": "study"}, {"action": "read"}],
            "reason": "有点闲，去看会儿书",
            "at": 123,
        }
        text = self.builder.runtime_layer(
            state, node=self.world.node_map()["bedroom"]
        )
        self.assertIn("你上一次安排好的是：移动 → 看书", text)
        self.assertIn("有点闲，去看会儿书", text)

    def test_remote_action_asks_for_both_steps(self):
        """去别处做事必须把「移动 + 那件事」一起写出来，不能只写移动。"""

        contract = self.builder.format_layer()
        self.assertIn("只写移动不算安排", contract)
        self.assertIn('"type":"walk_to"', contract)
        scene = self.builder.scene_layer(
            self.world.node_map()["bedroom"], {"web_search": "搜索网页"}
        )
        self.assertIn("紧接着把要做的那个动作也写进", scene)

    def test_plan_mode_switches_the_format_contract(self):
        state = WorldState(session_id="s1", node_id="study")
        plan_prompt = self.builder.build_autonomous_system_prompt(
            persona_text="你是温柔少女",
            state=state,
            node=self.world.node_map()["study"],
            available_tools={},
            mode="plan",
        )
        self.assertIn('"plan"', plan_prompt)
        self.assertNotIn('"actions"', plan_prompt)
        self.assertNotIn('"reasoning"', plan_prompt)

    def test_followup_prompt_discourages_hawking(self):
        """做完饭这类续说不能变成在群里吆喝推销。"""

        text = self.builder.build_reply_followup_prompt("说说刚做好的菜", "你刚做完了「做饭」。")
        self.assertIn("不要吆喝", text)
        self.assertIn("自言自语", text)
        self.assertIn("不要和最近说过的话重复", text)

    def test_chat_blocks_keep_one_chronological_stream(self):
        """群聊要按原样一条时间线呈现：谁在接谁的话，模型得看得出来。"""

        blocks = self.builder.chat_blocks(
            [
                {"user_id": "42", "name": "小明", "text": "今天吃什么", "is_self": False},
                {"user_id": "__self__", "name": "她", "text": "随便呀", "is_self": True},
            ]
        )
        joined = "\n".join(blocks)
        self.assertIn("最近在聊什么", joined)
        self.assertIn("小明(42)", joined)
        self.assertIn("你: 随便呀", joined)
        # 顺序不能乱：别人的话在她那句前面
        self.assertLess(joined.index("小明(42)"), joined.index("你: 随便呀"))

    def test_chat_blocks_keep_everything_they_are_given(self):
        """条数上限由调用方决定，渲染层不再二次截断。"""

        chat = [
            {"user_id": str(i), "name": f"用户{i}", "text": f"消息{i}", "is_self": False}
            for i in range(14)
        ]
        chat.append({"user_id": "__self__", "name": "她", "text": "我说的", "is_self": True})
        joined = "\n".join(self.builder.chat_blocks(chat))
        for i in range(14):
            self.assertIn(f"消息{i}", joined)
        self.assertIn("我说的", joined)

    def test_chat_blocks_include_summary_first(self):
        blocks = self.builder.chat_blocks(
            [{"user_id": "42", "name": "小明", "text": "在吗", "is_self": False}],
            "更早的时候大家在聊搬家。",
        )
        self.assertIn("之前的群聊", blocks[0])
        self.assertIn("搬家", blocks[0])

    def test_replied_batch_becomes_history_not_fresh(self):
        """水位线以内的是"已经回应过"：压成概览进历史块，不原样重复。"""

        chat = [
            {"user_id": "42", "name": "小明", "text": "晚饭吃什么", "at": 100.0},
            {"user_id": "7", "name": "阿May", "text": "吃鱼吧", "at": 110.0},
            {"user_id": "__self__", "name": "她", "text": "好呀", "at": 115.0, "is_self": True},
            {"user_id": "42", "name": "小明", "text": "那就这么定了", "at": 200.0},
        ]
        blocks = self.builder.chat_blocks(
            chat,
            preview="小明：晚饭吃什么；阿May：吃鱼吧；你：好呀",
            replied_until=115.0,
        )
        history, fresh = blocks[0], blocks[1]
        self.assertIn("之前的群聊", history)
        self.assertIn("晚饭吃什么", history)
        # 还没回应过的那条原样出现在「最近在聊什么」里，并带上时间
        self.assertIn("最近在聊什么", fresh)
        self.assertIn("那就这么定了", fresh)
        self.assertIn("小明(42)", fresh)
        self.assertNotIn("晚饭吃什么", fresh)
        self.assertIn(":", fresh)

    def test_consecutive_lines_from_the_same_person_merge(self):
        """同一个人连着说的几句合并成一行，读起来才像对话。"""

        blocks = self.builder.chat_blocks(
            [
                {"user_id": "42", "name": "小明", "text": "在吗", "at": 100.0},
                {"user_id": "42", "name": "小明", "text": "帮我看个东西", "at": 130.0},
            ]
        )
        joined = "\n".join(blocks)
        self.assertIn("在吗 / 帮我看个东西", joined)
        self.assertEqual(joined.count("小明(42)"), 1)

    def test_her_recent_replies_are_listed_for_style_avoidance(self):
        blocks = self.builder.chat_blocks(
            [],
            recent_replies=["早～", "……等等", "都下午三点了呀主人！"],
        )
        joined = "\n".join(blocks)
        self.assertIn("你最近说过的话", joined)
        self.assertIn("都下午三点了呀主人！", joined)

    def test_inner_voice_prefers_reasoning(self):
        state = WorldState(
            session_id="s1",
            node_id="study",
            last_reasoning={"env": "书房", "intent": "继续看书"},
        )
        text = self.builder.build_injection(state, node=self.world.node_map()["study"])
        self.assertIn("继续看书", text)

    def test_runtime_layer_shows_what_she_is_busy_with(self):
        """跨轮次不能失忆：手头的动作和还没做的计划要出现在提示词里。"""

        state = WorldState(
            session_id="s1",
            node_id="study",
            current_action={
                "type": "walk_to",
                "desc": "正在走去书房",
                "duration_ticks": 2,
                "elapsed_ticks": 1,
            },
            current_plan={
                "steps": [{"action": "search_web"}, {"action": "say"}],
                "current_step": 1,
                "reason": "去查新闻",
                "source": "llm",
            },
            recent_events=[{"kind": "move", "detail": {"to": "study"}}],
        )
        text = self.builder.build_injection(state, node=self.world.node_map()["study"])
        self.assertIn("你手头的事", text)
        self.assertIn("正在走去书房", text)
        self.assertIn("说话", text)
        # 事件也渲染成人话，而不是原始 dict
        self.assertIn("走到了：书房", text)

    def test_ability_map_lists_nodes(self):
        text = self.builder.ability_map()
        self.assertIn("书房", text)
        self.assertIn("卧室", text)

    def test_engagement_hint_is_rendered(self):
        state = WorldState(session_id="s1", node_id="window")
        text = self.builder.build_injection(
            state,
            node=self.world.node_map()["window"],
            engagement_hint="你最近连续 2 次主动说话都没人回应。",
        )
        self.assertIn("没人回应", text)


if __name__ == "__main__":
    unittest.main()
