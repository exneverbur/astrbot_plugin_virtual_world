"""引擎集成测试（seam S1~S4）：不依赖 AstrBot，用测试替身驱动整个状态机。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import ConfigStore  # noqa: E402
from core.db import AsyncDatabase  # noqa: E402
from core.engine import MessageContext, TickOutcome, VirtualWorldEngine  # noqa: E402
from core.json_actions import PlannedAction  # noqa: E402
from core.models import parse_world  # noqa: E402
from tests.stub_ports import (  # noqa: E402
    StubClock,
    StubLLM,
    StubMessenger,
    StubPersona,
    StubTools,
)

SESSION = "aiocqhttp:GroupMessage:1001"
OTHER_SESSION = "aiocqhttp:GroupMessage:2002"
SAY_REPLY = '{"actions":[{"type":"say","messages":["有人在吗？"]}]}'


class EngineTestCase(unittest.IsolatedAsyncioTestCase):
    node_id = "study"

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

        self.store = ConfigStore(self.data_dir)
        self.store.ensure_files()
        self.store.load_world()
        self.store.add_session(
            SESSION,
            session_type="group",
            platform="aiocqhttp",
            cold_start_node=self.node_id,
        )
        self.db = AsyncDatabase(self.store.db_path)
        self.addAsyncCleanup(self.db.close)

        self.llm = StubLLM([SAY_REPLY, SAY_REPLY, SAY_REPLY, SAY_REPLY])
        self.messenger = StubMessenger()
        self.tools = StubTools({"web_search": "搜索网页"})
        self.persona = StubPersona("你是一个温柔黏人的少女。")
        self.clock = StubClock(now=1_700_000_000.0)

        self.engine = VirtualWorldEngine(
            store=self.store,
            db=self.db,
            llm=self.llm,
            messenger=self.messenger,
            tools=self.tools,
            persona=self.persona,
            clock=self.clock,
            tick_seconds=60.0,
            decider_interval=0.0,
        )
        self.assertEqual(self.engine.load_warnings, [])

    # ---------------- 工具 ----------------

    def ctx(self, **overrides) -> MessageContext:
        data = {
            "session_id": SESSION,
            "user_id": "42",
            "user_name": "小明",
            "text": "你在干嘛呀",
            "is_wake": True,
            # 「真的 @ 了她」和「消息要交给大模型」是两件事，测试里默认按"在跟她说话"构造
            "is_mentioned": True,
        }
        data.update(overrides)
        return MessageContext(**data)

    async def set_state(self, **fields):
        async with self.engine.session_state(SESSION) as state:
            for key, value in fields.items():
                setattr(state, key, value)
        return await self.engine.load_state(SESSION, cold_start=False)

    async def get_state(self):
        return await self.engine.load_state(SESSION, cold_start=False)

    def interaction_rows(self):
        rows = self.engine.memory.db.query_memories(session_id=SESSION, limit=50)
        return [row for row in rows if row["type"] == "interaction"]

    # ---------------- S1：注入模式 ----------------

    async def test_non_whitelisted_session_is_ignored(self):
        injection = await self.engine.handle_incoming(
            self.ctx(session_id=OTHER_SESSION)
        )
        self.assertIsNone(injection)

    async def test_whitelisted_session_gets_world_context(self):
        injection = await self.engine.handle_incoming(self.ctx())
        self.assertIsNotNone(injection)
        assert injection is not None
        self.assertIn("书房", injection)
        self.assertIn("虚拟世界状态", injection)
        # 注入模式不接管回复：不带 JSON 输出协议
        self.assertNotIn('"actions"', injection)

    async def test_cold_start_note_appears_only_once(self):
        first = await self.engine.handle_incoming(self.ctx())
        second = await self.engine.handle_incoming(self.ctx())
        assert first is not None and second is not None
        self.assertIn("刚从沉睡中苏醒", first)
        self.assertNotIn("刚从沉睡中苏醒", second)

    async def test_user_message_updates_state_and_records_memory(self):
        await self.engine.handle_incoming(self.ctx(text="我今天有点难过", mood_signal="negative"))
        state = await self.get_state()
        self.assertIn("42", state.user_presence)
        self.assertEqual(state.user_presence["42"]["name"], "小明")
        # 逐条不落记忆：先攒着，等这段聊完再总结
        self.assertTrue(state.pending_memory)
        self.assertEqual(self.interaction_rows(), [])

        self.llm.replies = ["小明说他今天有点难过，我记下了"]
        self.clock.advance(31 * 60)
        await self.engine.flush_pending_memory(SESSION)
        memories = self.engine.memory.recall(
            session_id=SESSION, persona_id="", node_id=self.node_id, focus_user="42", limit=5
        )
        self.assertTrue(
            any("小明" in item.content for item in memories),
            [item.content for item in memories],
        )

    async def test_blocked_words_are_ignored(self):
        async with self.engine.session_state(SESSION):
            pass
        raw = self.store.raw_world()
        raw["content_safety"]["blocked_words"] = ["违禁词"]
        self.store.save_world(raw)
        self.engine.reload_config()
        injection = await self.engine.handle_incoming(self.ctx(text="这里面有违禁词"))
        self.assertIsNone(injection)

    # ---------------- S2：tick ----------------

    async def test_tick_advances_world_time_and_decays_energy(self):
        before = await self.get_state()
        outcomes = await self.engine.tick()
        after = await self.get_state()
        self.assertEqual(after.world_time, before.world_time + 1)
        self.assertLess(after.energy, before.energy)
        self.assertEqual(len(outcomes), 1)

    async def test_continuous_action_advances_and_finishes(self):
        await self.set_state(loneliness=0.9, boredom=0.1, energy=0.6)
        await self.engine.maybe_decide(SESSION)
        state = await self.get_state()
        self.assertEqual((state.current_action or {}).get("type"), "walk_to")
        # 书房 -> 大厅需要 1 tick
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.node_id, "lobby")
        self.assertIsNone(state.current_action)

    async def test_sleep_schedule_moves_bot_and_puts_it_to_sleep(self):
        self.clock.set_struct(datetime(2026, 9, 10, 23, 30))
        outcomes = await self.engine.run_schedules()
        self.assertTrue(any("night_sleep" in note for note in outcomes[0].notes))
        state = await self.get_state()
        self.assertEqual((state.current_action or {}).get("type"), "walk_to")

        await self.engine.run_schedules()  # 同一天同一时间不应重复触发
        self.assertEqual(len(await self.engine.run_schedules()), 0)

        # 书房 -> 大厅 -> 卧室 = 2 tick
        await self.engine.tick()
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.node_id, "bedroom")
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.state, "sleeping")
        self.assertEqual((state.current_action or {}).get("type"), "sleep")

    async def test_schedule_conditions_block_trigger(self):
        self.clock.set_struct(datetime(2026, 9, 10, 8, 30))
        await self.set_state(energy=0.1)  # morning_greet 需要 min_energy 0.35
        outcomes = await self.engine.run_schedules()
        self.assertEqual(outcomes, [])

    # ---------------- S3：自主行为 ----------------

    async def test_autonomous_loneliness_makes_her_find_people_and_speak(self):
        await self.set_state(loneliness=0.95, node_id="bedroom")
        await self.engine.maybe_decide(SESSION)
        state = await self.get_state()
        self.assertEqual((state.current_action or {}).get("type"), "walk_to")

        await self.engine.tick()  # 抵达大厅
        state = await self.get_state()
        self.assertEqual(state.node_id, "lobby")

        await self.engine.tick()  # 执行 say（由 LLM 生成文案）
        self.assertIn("有人在吗？", self.messenger.flat_messages)
        state = await self.get_state()
        self.assertTrue(state.awaiting_reply)

    async def test_hourly_autonomous_limit(self):
        await self.set_state(loneliness=0.95, autonomous_hour_marker=0, autonomous_count_hour=5)
        outcome = await self.engine.maybe_decide(SESSION)
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertTrue(any("上限" in note for note in outcome.notes))
        state = await self.get_state()
        self.assertIsNone(state.current_action)

    async def test_cooldown_blocks_outgoing_messages(self):
        self.clock.set_struct(datetime(2026, 9, 10, 8, 30))
        await self.set_state(unanswered_count=3, cooldown_until=0)
        await self.engine.run_schedules()
        await self.engine.tick()  # 走到大厅，同时评估进入冷却
        await self.engine.tick()  # 执行 stretch（有可见文案）
        state = await self.get_state()
        self.assertGreater(state.cooldown_until, 0)
        self.assertNotIn("（伸了个懒腰）", self.messenger.flat_messages)

    async def test_extreme_reach_out_is_rate_limited(self):
        """极端保护必须受限：不能每个 tick 都触发一次主动找人。"""

        await self.set_state(
            loneliness=0.95,
            node_id="lobby",
            world_time=5000,
            high_loneliness_since=1,
        )
        for _ in range(10):
            await self.engine.tick()
        self.assertEqual(len(self.messenger.sent), 1)

        # 隔了足够久之后才允许再来一次
        self.clock.advance(4000)
        await self.engine.tick()  # 这一 tick 生成计划
        await self.engine.tick()  # 这一 tick 执行计划
        self.assertEqual(len(self.messenger.sent), 2)

    # ---------------- 工具型动作 ----------------

    def add_schedule(self, **overrides):
        """往日程配置里追加一条日程并热重载，返回最终写入的字典。"""

        payload = {
            "id": "custom_schedule",
            "enabled": True,
            "time": "12:00",
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "conditions": {"not_state": ["sleeping"]},
            "priority": 5,
        }
        payload.update(overrides)
        raw = self.store.raw_schedules()
        raw["schedules"].append(payload)
        self.store.save_schedules(raw)
        self.engine.reload_config()
        return payload

    async def test_tool_result_is_logged(self):
        """工具返回的值必须进事件日志，方便核对她说的是不是编的。"""

        self.add_schedule(
            id="search_log",
            time="12:00",
            action_chain=[{"type": "search_web", "intent": "今天有什么新闻"}],
        )
        await self.set_state(node_id="study")
        self.tools.results["web_search"] = "今天有 3 条科技新闻，其中一条是模型开源。"
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.run_schedules()
        events = await self.db.call("query_events", session_id=SESSION, limit=30)
        # 调用和返回各记一条：日志里能看出"什么时候调的、拿回来了什么"
        calls = [item for item in events if item["event_type"] == "tool_call"]
        results = [item for item in events if item["event_type"] == "tool_result"]
        self.assertTrue(calls, events)
        self.assertTrue(results, events)
        self.assertEqual(calls[0]["detail"]["tool"], "web_search")
        self.assertTrue(results[0]["detail"]["ok"])
        self.assertIn("科技新闻", results[0]["detail"]["result"])
        self.assertEqual(results[0]["detail"]["tool"], "web_search")

    async def test_failed_tool_is_logged_and_does_not_make_her_talk(self):
        """工具失败时：日志里记下原因，并且不要让她就着空气编一段。"""

        self.add_schedule(
            id="search_fail",
            time="12:00",
            action_chain=[{"type": "search_web", "intent": "今天有什么新闻"}],
        )
        await self.set_state(node_id="study")
        self.tools.failures["web_search"] = "调用出错：连接超时"
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.run_schedules()
        events = await self.db.call("query_events", session_id=SESSION, limit=30)
        results = [item for item in events if item["event_type"] == "tool_result"]
        self.assertTrue(results, events)
        self.assertFalse(results[0]["detail"]["ok"])
        self.assertIn("连接超时", results[0]["detail"]["error"])
        # 失败时不该产生"把结果讲给大家听"的发言
        self.assertFalse(
            [message for message in self.messenger.flat_messages if message],
            self.messenger.flat_messages,
        )

    async def test_generate_actions_for_zone_returns_drafts(self):
        """批量生成只出草稿：绑到具体地点、不直接落库。"""

        self.llm.replies = [
            '{"actions":[{"id":"lobby_watch_rain","node_id":"lobby",'
            '"name":"在大厅看雨","description":"趴在窗边看外面的雨",'
            '"on_complete":{"effects":{"affect":"0.06"}}}]}'
        ]
        before = len(self.engine.world.actions)
        result = await self.engine.generate_actions_for_zone("home", 3)
        self.assertTrue(result["actions"], result)
        draft = result["actions"][0]
        self.assertEqual(draft["allowed_nodes"], ["lobby"])
        self.assertEqual(draft["scope"], "node")
        self.assertEqual(draft["on_complete"]["effects"]["affect"], "+0.06")
        # 生成不会直接写进配置
        self.assertEqual(len(self.engine.world.actions), before)

    async def test_generate_nodes_for_zone_places_and_links(self):
        self.llm.replies = [
            '{"nodes":[{"id":"balcony","name":"阳台","prompt":"有花有太阳",'
            '"atmosphere":{"calm":0.7}}]}'
        ]
        result = await self.engine.generate_nodes_for_zone("home", 2)
        self.assertEqual([item["id"] for item in result["nodes"]], ["balcony"])
        self.assertEqual(result["nodes"][0]["zone_id"], "home")
        # 方案 A：新地点连到区域内已有的一个地点
        self.assertTrue(result["edges"], result)
        self.assertIn(result["edges"][0]["to"], {"balcony"})
        self.assertGreater(result["nodes"][0]["x"], 0)

    async def test_set_values_clamps_and_logs(self):
        applied = await self.engine.set_values(
            SESSION, {"energy": 5, "affect": 0.8, "unknown_field": 1}
        )
        self.assertEqual(applied, {"energy": 1.0, "affect": 0.8})
        state = await self.get_state()
        self.assertEqual(state.energy, 1.0)
        self.assertEqual(state.affect, 0.8)
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        self.assertTrue([item for item in events if item["event_type"] == "manual"])

    async def test_force_decide_skips_the_interval(self):
        await self.set_state(loneliness=0.95, node_id="study")
        await self.engine.maybe_decide(SESSION)
        await self.set_state(current_action=None, current_plan=None)
        # 间隔没到：普通的评估会被直接跳过
        self.assertIsNone(await self.engine.maybe_decide(SESSION))
        # 编辑器手动触发：绕过间隔
        forced = await self.engine.maybe_decide(SESSION, force=True)
        self.assertIsNotNone(forced)

    async def test_disabled_action_is_skipped_and_interrupted(self):
        """停用的动作：新的一轮不会执行它，正在做的这一件也会立刻停下。"""

        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "read":
                action["enabled"] = False
        self.store.save_world(raw)
        self.engine.reload_config()

        self.add_schedule(
            id="disabled_read", time="12:00", action_chain=[{"type": "read"}]
        )
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.run_schedules()
        state = await self.get_state()
        self.assertIsNone(state.current_action)
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        skipped = [item for item in events if item["event_type"] == "skip"]
        self.assertTrue(skipped, events)
        self.assertIn("停用", skipped[0]["detail"]["note"])

        # 正在做的动作被停用：下一个 tick 立刻中止
        await self.set_state(
            current_action={"type": "read", "duration_ticks": 999, "elapsed_ticks": 0}
        )
        await self.engine.tick()
        state = await self.get_state()
        self.assertIsNone(state.current_action)

    async def test_tool_action_calls_tool_then_speaks(self):
        raw = self.store.raw_schedules()
        raw["schedules"].append(
            {
                "id": "search_now",
                "enabled": True,
                "time": "12:00",
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "action_chain": [
                    {"type": "search_web", "params": {"query": "今天的新闻"}}
                ],
                "conditions": {"not_state": ["sleeping"]},
                "priority": 5,
            }
        )
        self.store.save_schedules(raw)
        self.engine.reload_config()
        self.engine.tools.results["web_search"] = "今天天气不错，适合出门。"
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))

        outcomes = await self.engine.run_schedules()
        # 瞬时工具动作：到点就当场查、当场说完，不用再等一个 tick
        self.assertEqual(len(self.tools.calls), 1)
        self.assertEqual(self.tools.calls[0][1]["query"], "今天的新闻")
        self.assertIn("有人在吗？", outcomes[0].messages)
        self.assertEqual((await self.get_state()).current_action, None)

    async def test_builtin_search_takes_the_installed_official_tool(self):
        """内置搜索写的是 web_search，官方工具却叫 web_search_tavily：也要能用上。"""

        self.tools._tools = {"web_search_tavily": "官方联网搜索"}
        self.tools.schemas["web_search_tavily"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.results = {"web_search_tavily": "今天有三条科技新闻。"}
        self.add_schedule(
            id="news",
            time="12:00",
            action_chain=[{"type": "search_web", "params": {"query": "今天的新闻"}}],
        )
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))

        await self.engine.run_schedules()
        self.assertEqual([name for name, _params in self.tools.calls], ["web_search_tavily"])

    async def test_schedule_auto_travel_walks_to_required_node_first(self):
        """日程只写「上网搜索」，开启 auto_travel 后她会自己先走到书房。"""

        self.add_schedule(
            id="morning_news",
            time="08:00",
            action_chain=[{"type": "search_web", "params": {"query": "今天的新闻"}}],
            auto_travel=True,
        )
        await self.set_state(node_id="bedroom")
        self.clock.set_struct(datetime(2026, 9, 10, 8, 0))

        await self.engine.run_schedules()
        state = await self.get_state()
        current = state.current_action or {}
        self.assertEqual(current.get("type"), "walk_to")
        self.assertEqual(current.get("target_node"), "study")

        # 移动是「走完整条最短路才落地」：卧室 -> 大厅 -> 书房 = 2 tick
        await self.engine.tick()
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.node_id, "study")

        for _ in range(8):
            if self.tools.calls:
                break
            await self.engine.tick()
        self.assertEqual(len(self.tools.calls), 1)
        self.assertEqual(self.tools.calls[0][1]["query"], "今天的新闻")

    async def test_schedule_without_auto_travel_skips_when_at_wrong_node(self):
        """关掉 auto_travel 时地点是硬条件：不在书房就直接跳过这一步。"""

        self.add_schedule(
            id="morning_news",
            time="08:00",
            action_chain=[{"type": "search_web", "params": {"query": "今天的新闻"}}],
            auto_travel=False,
        )
        await self.set_state(node_id="bedroom")
        self.clock.set_struct(datetime(2026, 9, 10, 8, 0))

        outcomes = await self.engine.run_schedules()
        self.assertTrue(outcomes)
        # 地点限制现在由「限定地点」表达，所以跳过原因是「在当前地点不可用」
        self.assertTrue(
            any("不可用" in note for note in outcomes[0].notes),
            outcomes[0].notes,
        )
        state = await self.get_state()
        self.assertEqual(state.node_id, "bedroom")
        self.assertIsNone(state.current_action)
        self.assertEqual(self.tools.calls, [])

        # 跳过原因必须进事件日志（日志页要能解释"她为什么没做这件事"）
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        skips = [item for item in events if item["event_type"] == "skip"]
        self.assertTrue(skips, events)
        self.assertIn("不可用", skips[0]["detail"]["note"])

    # ---------------- 群聊上下文：留档 / 携带 / 压缩 ----------------

    def set_context(self, **fields):
        raw = self.store.raw_world()
        raw.setdefault("context", {}).update(fields)
        self.store.save_world(raw)
        self.engine.reload_config()

    async def test_chat_history_survives_model_reload(self):
        """群聊留档持久化：重启（重新加载配置与状态）后还在。"""

        for index in range(3):
            await self.engine.note_presence(
                self.ctx(text=f"第{index}条消息", user_id=str(index), user_name=f"用户{index}")
            )
        state = await self.get_state()
        self.assertEqual(len(state.recent_chat), 3)

        # 换一个引擎实例读同一份数据，模拟重启
        again = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual([item["text"] for item in again.recent_chat], [f"第{i}条消息" for i in range(3)])

    async def test_chat_context_carry_is_limited_but_history_is_kept(self):
        """带进提示词的条数有限，但原始留档不受影响。"""

        self.set_context(chat_history_max=50, chat_overflow="discard")
        for index in range(20):
            await self.engine.note_presence(
                self.ctx(text=f"消息{index}", user_id="1", user_name="小明")
            )
        state = await self.get_state()
        self.assertEqual(len(state.recent_chat), 20)
        carried = self.engine.chat_context(state)
        self.assertEqual(len(carried), 12)  # decider.chat_max_messages 默认值
        self.assertEqual(carried[-1]["text"], "消息19")

    async def test_chat_overflow_discard_keeps_only_limit(self):
        self.set_context(chat_history_max=20, chat_overflow="discard")
        for index in range(35):
            await self.engine.note_presence(
                self.ctx(text=f"消息{index}", user_id="1", user_name="小明")
            )
        state = await self.get_state()
        self.assertEqual(len(state.recent_chat), 20)
        self.assertEqual(state.recent_chat[-1]["text"], "消息34")

    async def test_chat_overflow_compress_makes_summary(self):
        """留档超阈值时用压缩模型压成摘要，只保留最近的原文。"""

        self.set_context(
            chat_history_max=200,
            chat_overflow="compress",
            chat_compress_threshold=10,
            chat_keep_after_compress=4,
            summary_refresh_minutes=1,
        )
        for index in range(12):
            await self.engine.note_presence(
                self.ctx(text=f"消息{index}", user_id="1", user_name="小明")
            )
        self.llm.replies = ["大家主要在聊周末去哪玩，顺便问了她在不在。"]
        await self.engine.tick()

        state = await self.get_state()
        self.assertEqual(state.chat_summary, "大家主要在聊周末去哪玩，顺便问了她在不在。")
        self.assertEqual(len(state.recent_chat), 4)

        # 摘要会出现在提示词里
        injection = await self.engine.preview_injection(SESSION)
        self.assertIn("更早的群聊", injection)

    async def test_clear_chat_context(self):
        """清空上下文：留档与摘要一起清掉，其它状态不受影响。"""

        # 压缩阈值有下限（10），所以这里攒够 14 条
        self.set_context(chat_history_max=200, chat_overflow="compress",
                         chat_compress_threshold=10, chat_keep_after_compress=2,
                         summary_refresh_minutes=1)
        for index in range(14):
            await self.engine.note_presence(
                self.ctx(text=f"消息{index}", user_id="1", user_name="小明")
            )
        self.llm.replies = ["摘要是她正在和人聊周末安排。"]
        await self.engine.tick()
        state = await self.get_state()
        self.assertTrue(state.chat_summary)

        result = await self.engine.clear_chat_context(SESSION)
        self.assertEqual(result["removed"], 2)
        self.assertTrue(result["had_summary"])
        state = await self.get_state()
        self.assertEqual(state.recent_chat, [])
        self.assertEqual(state.chat_summary, "")
        # 世界状态不受影响
        self.assertEqual(state.node_id, self.node_id)

        snapshot = await self.engine.snapshot(SESSION)
        self.assertEqual(snapshot["chat_history_count"], 0)
        self.assertEqual(snapshot["chat_summary"], "")

    async def test_takeover_reply_is_remembered_as_her_own_words(self):
        """她说出去的话要进聊天上下文，下一轮提示词才有「你最近说过的话」。"""

        ctx = self.ctx(text="在干嘛呀")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)

        state = await self.get_state()
        mine = [item for item in state.recent_chat if item.get("is_self")]
        self.assertTrue(mine, state.recent_chat)
        self.assertEqual(mine[-1]["text"], "有人在吗？")

        prompt = await self.engine.preview_autonomous_prompt(SESSION)
        self.assertIn("你最近说过的话", prompt)
        self.assertIn("有人在吗？", prompt)

    # ---------------- 换工具 / 缺工具 ----------------

    async def test_tool_params_filled_by_helper_model(self):
        """主模型只给「想干什么」，参数由辅助模型按工具定义补全。"""

        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        }
        self.llm.replies = [
            '{"reasoning":{"intent":"查今天的新闻"},'
            '"actions":[{"type":"search_web","intent":"查一下今天的新闻"},'
            '{"type":"say","messages":["我去查一下"]}]}',
            '{"query": "今天的新闻"}',
            '{"actions":[{"type":"say","messages":["查到了，今天还挺热闹的"]}]}',
        ]
        ctx = self.ctx(text="帮我查一下今天有什么新闻")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)

        for _ in range(8):
            if self.tools.calls:
                break
            await self.engine.tick()

        self.assertEqual(len(self.tools.calls), 1)
        self.assertEqual(self.tools.calls[0][1]["query"], "今天的新闻")

    async def test_tool_param_helper_result_is_cached(self):
        """同一个工具 + 同一个意图只问一次辅助模型。"""

        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        state = await self.set_state(node_id="study")
        definition = self.engine.world.action_map()["search_web"]
        action = PlannedAction(type="search_web", intent="查今天的新闻")
        self.llm.replies = ['{"query": "今天的新闻"}']
        first, note = await self.engine.fill_tool_params(state, definition, action)
        self.assertEqual(note, "")
        self.assertEqual(first["query"], "今天的新闻")

        self.llm.replies = []  # 第二次不该再调用（调用会拿到空回复）
        second, note2 = await self.engine.fill_tool_params(
            state, definition, PlannedAction(type="search_web", intent="查今天的新闻")
        )
        self.assertEqual(note2, "")
        self.assertEqual(second["query"], "今天的新闻")
        self.assertEqual(len(self.llm.calls), 1)

    async def test_tool_action_without_tool_name_is_skipped(self):
        """工具型动作必须选一个工具，没选就跳过并写明原因。"""

        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "search_web":
                action["tool_name"] = ""
                action["tool_names"] = []
                action["tool_fallbacks"] = []
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_schedule(
            id="no_tool_bound",
            time="12:00",
            action_chain=[{"type": "search_web", "intent": "查新闻"}],
        )
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        outcomes = await self.engine.run_schedules()
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        skips = [item for item in events if item["event_type"] == "skip"]
        self.assertTrue(skips, outcomes[0].notes)
        self.assertIn("必须选一个工具", skips[0]["detail"]["note"])

    async def test_tool_action_with_self_send_tool_is_skipped(self):
        """选了「直发消息」类工具时不调用，并写明原因。"""

        self.tools._tools = {"send_message_to_user": "直发消息"}
        self.tools.schemas["send_message_to_user"] = {
            "type": "object",
            "properties": {"messages": {"type": "array"}},
        }
        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "search_web":
                action["tool_name"] = "send_message_to_user"
                action["tool_names"] = ["send_message_to_user"]
                action["tool_fallbacks"] = []
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_schedule(
            id="self_send",
            time="12:00",
            action_chain=[{"type": "search_web", "intent": "说句话"}],
        )
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.run_schedules()
        self.assertEqual(self.tools.calls, [])
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        skips = [item for item in events if item["event_type"] == "skip"]
        self.assertTrue(skips)
        self.assertIn("直发消息", skips[0]["detail"]["note"])

    async def test_tool_action_without_intent_falls_back_to_the_description(self):
        """没有意图也不再直接跳过：用动作自己的说明兜一句，照样把参数补出来。

        日程动作链没有"想干什么"这一栏，缺了它工具型动作到点只会被跳过。
        """

        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.llm.replies = ['{"query": "今天的新闻"}']
        state = await self.set_state(node_id="study")
        definition = self.engine.world.action_map()["search_web"]
        params, _note = await self.engine.fill_tool_params(
            state, definition, PlannedAction(type="search_web")
        )
        self.assertEqual(params.get("query"), "今天的新闻")
        # 兜底意图里带上了动作自己的说明，补全模型才有依据
        self.assertIn("上网搜索", self.llm.calls[-1]["prompt"])

    async def test_optional_only_tool_schema_still_fills_params(self):
        """工具把参数写成「可选」、实现却必须要：也要让辅助模型补一次。

        这是真实踩到的坑：天气工具的 schema 里没有 required，我们直接拿空参数调用，
        工具内部就报 missing positional argument。
        """

        self.tools._tools = {"get_current_weather": "查天气"}
        self.tools.schemas["get_current_weather"] = {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "地点，例如：杭州"}},
        }
        self.tools.results = {"get_current_weather": "武汉 晴 26℃"}
        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "check_weather":
                action["tool_names"] = ["get_current_weather"]
                action["tool_name"] = "get_current_weather"
        self.store.save_world(raw)
        self.engine.reload_config()

        definition = self.engine.world.action_map()["check_weather"]
        state = await self.set_state(node_id="study")
        self.llm.replies = ['{"city": "武汉"}']
        action = PlannedAction(type="check_weather", intent="看一眼武汉今晚的天气")
        outcome = TickOutcome(session_id=SESSION)
        ok = await self.engine._prepare_tool_action(state, definition, action, outcome)

        self.assertTrue(ok, outcome.notes)
        filled = dict(getattr(action, "tool_params", {}) or {})
        self.assertEqual(filled.get("get_current_weather", {}).get("city"), "武汉")

    async def test_tool_call_retries_once_after_argument_error(self):
        """参数没给全导致调用报错时，带着报错再补一次并重试一次。"""

        from core.ports import ToolCallResult

        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "check_weather":
                action["tool_names"] = ["get_current_weather"]
                action["tool_name"] = "get_current_weather"
        self.store.save_world(raw)
        self.engine.reload_config()
        self.tools._tools = {"get_current_weather": "查天气"}
        self.tools.schemas["get_current_weather"] = {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "地点"}},
        }

        seen: list[dict] = []

        async def flaky(name: str, params: dict, session_id: str = "") -> ToolCallResult:
            seen.append(dict(params))
            if len(seen) == 1:
                return ToolCallResult(
                    ok=False,
                    error=(
                        "WeatherPlugin.get_current_weather_tool() missing 1 required "
                        "positional argument: 'city'"
                    ),
                    tool=name,
                )
            return ToolCallResult(ok=True, text=f"{params.get('city')} 晴", tool=name)

        self.tools.call_tool = flaky
        definition = self.engine.world.action_map()["check_weather"]
        state = await self.set_state(node_id="study")
        action = PlannedAction(type="check_weather", intent="看一眼武汉今晚的天气")
        self.llm.replies = ['{"city": "武汉"}', '{"city": "武汉"}']
        outcome = TickOutcome(session_id=SESSION)
        await self.engine._prepare_tool_action(state, definition, action, outcome)

        payload = {
            "type": "check_weather",
            "intent": "看一眼武汉今晚的天气",
            "params": {},
            "tool_params": {},  # 故意留空，模拟第一次调用参数不全
        }
        await self.engine._run_tool_calls(state, definition, payload)

        self.assertEqual(len(seen), 2, seen)
        self.assertEqual(seen[-1].get("city"), "武汉")
        self.assertTrue(payload.get("tool_ok"))
        self.assertIn("晴", str(payload.get("tool_result")))

    async def test_action_fixed_params_win_over_model_values(self):
        """动作里配好的固定参数（例如 city=武汉）会直接带给工具。"""

        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "check_weather":
                action["tool_names"] = ["get_current_weather"]
                action["tool_name"] = "get_current_weather"
                action["params"] = {"city": {"type": "string", "value": "武汉"}}
        self.store.save_world(raw)
        self.engine.reload_config()
        self.tools._tools = {"get_current_weather": "查天气"}
        self.tools.schemas["get_current_weather"] = {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "地点"}},
        }

        definition = self.engine.world.action_map()["check_weather"]
        state = await self.set_state(node_id="study")
        action = PlannedAction(type="check_weather", intent="看看天气")
        outcome = TickOutcome(session_id=SESSION)
        ok = await self.engine._prepare_tool_action(state, definition, action, outcome)

        self.assertTrue(ok, outcome.notes)
        filled = dict(getattr(action, "tool_params", {}) or {})
        self.assertEqual(filled.get("get_current_weather", {}).get("city"), "武汉")
        self.assertEqual(self.llm.calls, [])  # 固定参数齐了，不需要再问辅助模型

    async def test_action_with_two_tools_calls_both_in_order(self):
        """一个动作挂两个工具时，按配置顺序都调用，并把结果合并起来。"""

        self.tools._tools = {"web_search": "搜索网页", "web_fetch": "抓网页"}
        self.tools.results = {"web_search": "搜到了三条", "web_fetch": "正文内容"}
        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.schemas["web_fetch"] = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "search_web":
                action["tool_names"] = ["web_search", "web_fetch"]
        self.store.save_world(raw)
        self.engine.reload_config()

        definition = self.engine.world.action_map()["search_web"]
        self.assertEqual(definition.tool_list(), ["web_search", "web_fetch"])
        self.assertEqual(definition.tool_name, "web_search")

        state = await self.set_state(node_id="study")
        action = PlannedAction(
            type="search_web",
            intent="查今天的新闻",
            params={"query": "今天的新闻"},
        )
        self.llm.replies = ['{"url": "https://example.com/news"}']
        outcome = TickOutcome(session_id=SESSION)
        ok = await self.engine._prepare_tool_action(state, definition, action, outcome)
        self.assertTrue(ok, outcome.notes)

        payload = {
            "type": "search_web",
            "params": dict(action.params),
            "tool_params": dict(action.tool_params),
        }
        await self.engine._run_tool_calls(state, definition, payload)

        self.assertEqual(
            [name for name, _params in self.tools.calls], ["web_search", "web_fetch"]
        )
        self.assertEqual(self.tools.calls[0][1]["query"], "今天的新闻")
        self.assertEqual(self.tools.calls[1][1]["url"], "https://example.com/news")
        self.assertIn("搜到了三条", payload["tool_result"])
        self.assertIn("正文内容", payload["tool_result"])
        self.assertTrue(payload["tool_ok"])

    async def test_queued_step_keeps_intent(self):
        """被排队的工具动作必须留住 intent，否则到点只会说"没有给出想做什么"。"""

        await self.set_state(node_id="bedroom")
        actions = [
            PlannedAction(type="say", messages=["这就去"]),
            PlannedAction(type="sleep", duration=600),
            PlannedAction(type="search_web", intent="搜今天的新闻"),
        ]
        outcome = TickOutcome(session_id=SESSION)
        async with self.engine.session_state(SESSION) as state:
            await self.engine._execute_actions(
                state, self.engine.node("bedroom"), outcome, actions, depth=0, autonomous=False
            )
        state = await self.engine.load_state(SESSION, cold_start=False)

        plan = state.current_plan or {}
        queued = [step for step in plan.get("steps", []) if step.get("action") == "search_web"]
        self.assertTrue(queued, plan)
        self.assertEqual(queued[0]["intent"], "搜今天的新闻")

    async def test_new_actions_are_appended_after_existing_plan(self):
        """这一轮剩下的动作排在她原来的安排前面，原计划不丢。"""

        from core import planner as planner_module

        await self.set_state(node_id="bedroom")
        async with self.engine.session_state(SESSION) as live:
            live.current_plan = planner_module.create_plan(
                steps=[{"action": "walk_to", "target_node": "study"}, {"action": "read"}],
                world_time=live.world_time,
                reason="她自己的安排",
                source="rule",
            )

        actions = [
            PlannedAction(type="sleep", duration=600),
            PlannedAction(type="cook"),
        ]
        outcome = TickOutcome(session_id=SESSION)
        async with self.engine.session_state(SESSION) as live:
            await self.engine._execute_actions(
                live, self.engine.node("bedroom"), outcome, actions, depth=0, autonomous=False
            )
        state = await self.engine.load_state(SESSION, cold_start=False)

        steps = [step.get("action") for step in (state.current_plan or {}).get("steps", [])]
        self.assertEqual(steps, ["cook", "walk_to", "read"], steps)
        self.assertEqual((state.current_plan or {}).get("reason"), "她自己的安排")

    async def test_sleeping_chatter_only_logs_once_per_window(self):
        """睡着时群里刷屏：消息照样挡下、照样记进上下文，但日志不能一条一条刷。"""

        async with self.engine.session_state(SESSION) as state:
            state.state = "sleeping"
            state.current_action = {
                "type": "sleep",
                "elapsed_ticks": 1,
                "duration_ticks": 100,
                "interruptible": False,
            }
            state.pending_memory = []
            state.recent_chat = []

        for index in range(5):
            ctx = self.ctx(
                user_id=f"u{index}",
                user_name=f"路人{index}",
                text=f"群里说点啥 {index}",
                is_mentioned=False,
            )
            self.assertTrue(await self.engine.should_block_sleep(ctx))

        events = await self.db.call("query_events", session_id=SESSION, limit=50)
        skips = [item for item in events if item["event_type"] == "sleep_skip"]
        self.assertEqual(len(skips), 1, skips)
        self.assertEqual(skips[0]["detail"]["count"], 1)
        # 后面 4 条先攒着，下次记日志时一并报出来，不再一条一条刷
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.sleep_skip_count, 4)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(len(state.recent_chat), 5)  # 挡下但留档，醒来还知道群里说了什么

    async def test_refresh_nickname_reads_the_card_from_the_platform(self):
        """「重新获取」按钮：从平台读一次当前的群名片。"""

        self.messenger.card = "凶猛蓝色虎鲸💢"
        result = await self.engine.refresh_nickname(SESSION)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["card"], "凶猛蓝色虎鲸💢")

        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.bot_current_nickname, "凶猛蓝色虎鲸💢")
        self.assertEqual(state.bot_base_nickname, "凶猛蓝色虎鲸💢")
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        self.assertTrue(any(item["event_type"] == "nickname" for item in events))

    async def test_command_action_triggers_another_plugin_command(self):
        """指令触发：把意图拼成一条指令交给 AstrBot，再把结果交回给她。"""

        from core.ports import ToolCallResult

        captured: dict[str, str] = {}

        class StubCommands:
            async def trigger(self, session_id, command, *, event=None):
                captured["command"] = command
                return ToolCallResult(ok=True, text="北京 晴 26℃", tool="天气")

        self.engine.commands = StubCommands()
        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "ask_weather",
                "name": "查天气（指令）",
                "category": "instant",
                "llm_level": "command",
                "scope": "global",
                "trigger_command": "天气",
                "trigger_hint": "城市名",
                "visible": False,
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        definition = self.engine.world.action_map()["ask_weather"]

        self.llm.replies = ["/天气 北京", SAY_REPLY]
        action = PlannedAction(type="ask_weather", intent="查一下北京的天气")
        async with self.engine.session_state(SESSION) as live:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._run_command_action(
                live, self.engine.node("study"), outcome, definition, action
            )

        self.assertEqual(captured["command"], "/天气 北京")
        self.assertIn("26℃", self.llm.calls[-1]["prompt"])
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        kinds = [item["event_type"] for item in events]
        self.assertIn("command_call", kinds)
        self.assertIn("command_result", kinds)

    # ---------------- 指令动作：结果怎么交回给她 ----------------

    def add_command_action(self, **overrides) -> None:
        payload = {
            "id": "ask_weather",
            "name": "查天气（指令）",
            "category": "instant",
            "llm_level": "command",
            "scope": "global",
            "trigger_command": "天气",
            "trigger_hint": "城市名",
            "visible": False,
        }
        payload.update(overrides)
        raw = self.store.raw_world()
        raw["actions"] = [item for item in raw["actions"] if item.get("id") != payload["id"]]
        raw["actions"].append(payload)
        self.store.save_world(raw)
        self.engine.reload_config()

    def stub_commands(self, result):
        class StubCommands:
            async def trigger(self, session_id, command, *, event=None):
                return result

        self.engine.commands = StubCommands()

    async def run_command_action(self, intent: str = "查一下北京的天气") -> None:
        definition = self.engine.world.action_map()["ask_weather"]
        self.llm.replies = ["/天气 北京", SAY_REPLY, SAY_REPLY]
        async with self.engine.session_state(SESSION) as live:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._run_command_action(
                live,
                self.engine.node("study"),
                outcome,
                definition,
                PlannedAction(type="ask_weather", intent=intent),
            )

    async def test_continuous_command_action_runs_at_completion(self):
        """配成「持续动作」的指令动作，到点也要真的把指令发出去并写日志。

        以前 `_finish_action` 里只有工具型的分支，这种动作从头到尾一条日志都没有。
        """

        from core.ports import ToolCallResult

        captured: dict[str, str] = {}

        class StubCommands:
            async def trigger(self, session_id, command, *, event=None):
                captured["command"] = command
                return ToolCallResult(ok=True, text="武汉 晴 26℃", tool="天气")

        self.engine.commands = StubCommands()
        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "ask_weather",
                "name": "查天气（指令）",
                "category": "continuous",
                "llm_level": "command",
                "scope": "global",
                "trigger_command": "天气",
                "duration": 30,
                "visible": False,
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_schedule(
            id="weather",
            time="12:00",
            action_chain=[{"type": "ask_weather", "intent": "查武汉天气"}],
        )
        self.llm.replies = ["/天气 武汉", SAY_REPLY]
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.tick_until(lambda: bool(captured))
        self.assertEqual(captured["command"], "/天气 武汉")
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        self.assertIn("command_result", [item["event_type"] for item in events])

    async def test_command_composer_rejects_anything_that_is_not_a_command(self):
        """补参模型回一坨 JSON 时，别把 `/{"actions":...}` 发出去。"""

        self.add_command_action()
        self.llm.replies = ['{"actions":[{"type":"say","messages":["有人在吗？"]}]}']
        definition = self.engine.world.action_map()["ask_weather"]
        async with self.engine.session_state(SESSION) as state:
            line = await self.engine._compose_command(state, definition, "天气", "查天气")
        self.assertEqual(line, "/天气")

    async def test_command_composer_keeps_normal_replies(self):
        self.add_command_action()
        self.llm.replies = ["/天气 武汉"]
        definition = self.engine.world.action_map()["ask_weather"]
        async with self.engine.session_state(SESSION) as state:
            line = await self.engine._compose_command(state, definition, "天气", "查天气")
        self.assertEqual(line, "/天气 武汉")

    async def test_command_result_images_reach_the_main_model(self):
        """指令返回图片时，图片要跟着续说那次调用一起发给多模态主模型。"""

        from core.ports import ToolCallResult

        self.stub_commands(
            ToolCallResult(
                ok=True,
                text="给你看看今天的图",
                tool="天气",
                image_urls=["https://example.com/today.png"],
            )
        )
        self.add_command_action()
        await self.run_command_action()
        self.assertEqual(
            self.llm.calls[-1]["image_urls"], ["https://example.com/today.png"]
        )

    async def test_command_without_images_sends_text_only(self):
        from core.ports import ToolCallResult

        self.stub_commands(ToolCallResult(ok=True, text="北京 晴 26℃", tool="天气"))
        self.add_command_action()
        await self.run_command_action()
        self.assertEqual(self.llm.calls[-1]["image_urls"], [])

    async def test_command_followup_can_be_turned_off_globally(self):
        """全局「工具结果回话」关掉、动作也没要求续说时：执行完就闭嘴。"""

        from core.ports import ToolCallResult

        raw = self.store.raw_world()
        raw["tool_result_reply"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        self.stub_commands(ToolCallResult(ok=True, text="北京 晴 26℃", tool="天气"))
        self.add_command_action()
        await self.run_command_action()
        # 补指令参数那次调用是允许的；不许出现"带着结果再问她一次"的续说调用
        self.assertFalse(
            any("这是结果" in call["prompt"] for call in self.llm.calls),
            [call["prompt"][:60] for call in self.llm.calls],
        )
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        self.assertIn("command_result", [item["event_type"] for item in events])

    async def test_command_followup_honours_prompt_hint(self):
        from core.ports import ToolCallResult

        self.stub_commands(ToolCallResult(ok=True, text="北京 晴 26℃", tool="天气"))
        self.add_command_action(
            on_complete={"trigger": "llm_followup", "prompt_hint": "只报温度，别报湿度"}
        )
        await self.run_command_action()
        self.assertIn("只报温度，别报湿度", self.llm.calls[-1]["prompt"])

    async def test_command_result_is_truncated_before_the_followup(self):
        """一坨 base64 不能整条塞进提示词。"""

        from core.ports import ToolCallResult

        self.stub_commands(
            ToolCallResult(ok=True, text="X" * 5000, tool="天气")
        )
        self.add_command_action()
        await self.run_command_action()
        self.assertLess(len(self.llm.calls[-1]["prompt"]), 3000)
        self.assertIn("已截断", self.llm.calls[-1]["prompt"])

    async def test_preset_round_trip_and_state_clear(self):
        """预设：存下来、改坏再应用能复原；切预设只清状态，不动记忆与日志。"""

        await self.set_state(node_id="kitchen", energy=0.2)
        path = self.store.save_preset("p1", name="测试预设")
        self.assertTrue(path.is_file())
        self.assertEqual(self.store.active_preset(), "p1")

        raw = self.store.raw_world()
        raw["nodes"][0]["name"] = "改过的名字"
        self.store.save_world(raw)
        self.store.apply_preset("p1")
        self.assertNotEqual(self.store.raw_world()["nodes"][0]["name"], "改过的名字")

        cleared = await self.engine.clear_all_states()
        self.assertGreaterEqual(cleared, 1)
        self.assertIsNone(await self.db.call("get_state", SESSION))
        self.assertTrue(self.store.list_presets())

    async def test_schedule_actions_add_list_and_remove(self):
        """日程三件套：她自己加的日程能加、能看、能删；用户配置的删不掉。"""

        state = await self.engine.load_state(SESSION, cold_start=False)

        ok, note = await self.engine.schedule_add(
            {
                "time": "7:5",
                "days": ["mon", "tue"],
                "action_chain": [{"type": "say", "content": "早上好"}],
            }
        )
        self.assertTrue(ok, note)
        self.assertIn("07:05", note)

        text = self.engine.schedule_text()
        self.assertIn("07:05", text)
        self.assertIn("她自己加的", text)

        # 用户配置的日程（没有 created_by）不能删
        raw = self.store.raw_schedules()
        raw["schedules"].append(
            {
                "id": "user_one",
                "time": "12:00",
                "days": ["mon"],
                "action_chain": [{"type": "say"}],
            }
        )
        self.store.save_schedules(raw)
        self.engine.reload_config()
        ok, note = await self.engine.schedule_remove({"id": "user_one"})
        self.assertFalse(ok, note)
        self.assertIn("用户", note)

        ok, note = await self.engine.schedule_remove({"time": "07:05"})
        self.assertTrue(ok, note)
        self.assertNotIn("07:05", self.engine.schedule_text())

    async def test_schedule_add_rejects_unknown_action(self):
        ok, note = await self.engine.schedule_add(
            {"time": "07:00", "action_chain": [{"type": "不存在的动作"}]}
        )
        self.assertFalse(ok, note)
        self.assertIn("不存在", note)

    async def test_replied_chat_is_not_replayed_but_her_words_stay(self):
        """回应过的群聊不再回放；她自己说过的话要留着，才能要求她别重复。"""

        await self.engine.handle_incoming(self.ctx(text="第一句：晚饭吃什么"))
        self.llm.replies = [
            '{"chat_note": "在聊晚饭吃什么",'
            ' "actions": [{"type": "say", "messages": ["吃鱼吧"]}]}'
        ]
        outcome = await self.engine.handle_reply(self.ctx(text="第二句：我想吃鱼"))
        self.assertTrue(outcome.ok, outcome.error)

        state = await self.engine.load_state(SESSION, cold_start=False)
        texts = " ".join(str(item.get("text")) for item in self.engine.chat_context(state))
        self.assertNotIn("第一句", texts)
        self.assertNotIn("第二句", texts)
        self.assertIn("吃鱼吧", texts)
        self.assertEqual(state.chat_note, "在聊晚饭吃什么")

        prompt = self.prompts_prompt(state)
        self.assertIn("刚才你们聊过", prompt)
        self.assertIn("在聊晚饭吃什么", prompt)

    def prompts_prompt(self, state) -> str:
        return self.engine.prompts.build_autonomous_system_prompt(
            persona_text="测试人格",
            state=state,
            node=self.engine.node(state.node_id),
            available_tools=self.engine.available_tools(),
            recent_chat=self.engine.chat_context(state),
        )

    async def test_recall_reads_memories_and_asks_again(self):
        """回想：翻记忆 → 记两条日志 → 带着结果再问她一次。"""

        from core.memory import MemoryEngine, SCENE

        memory = MemoryEngine(self.engine.memory.db, self.engine.world)
        memory.remember(
            session_id=SESSION,
            persona_id="",
            node_id="kitchen",
            content="我在厨房煮了一大锅鱼汤，香味飘满了整个屋子",
            memory_type=SCENE,
            weight=0.8,
        )
        memory.remember(
            session_id=SESSION,
            persona_id="",
            node_id="study",
            content="在书房翻到一本很旧的漫画",
            memory_type=SCENE,
            weight=0.8,
        )

        self.llm.replies = [SAY_REPLY]  # 续说那一次
        action = PlannedAction(type="recall", intent="想想上次在厨房做饭的事")
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._run_recall(state, self.engine.node("study"), outcome, action)

        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        kinds = [item["event_type"] for item in events]
        self.assertIn("recall_start", kinds)
        self.assertIn("recall_done", kinds)
        done = [item for item in events if item["event_type"] == "recall_done"][0]
        self.assertEqual(done["detail"]["count"], 1)
        self.assertIn("厨房", done["detail"]["detail"])
        # 续说的那次调用里要带上看回忆到的内容
        self.assertIn("厨房", self.llm.calls[-1]["prompt"])

    async def test_recall_can_target_a_zone(self):
        """只给区域时，翻的是这个区域里所有地点。"""

        state = await self.engine.load_state(SESSION, cold_start=False)
        async with self.engine.session_state(SESSION) as live:
            query = await self.engine._parse_recall_query(live, "回忆一下公园里的事")
        zone_id = query.get("zone")
        self.assertEqual(zone_id, "", "测试世界只有一个区域，名字对不上就不该命中")

        # 区域名出现时必须展开成该区域的全部地点
        self.engine.world.zones[0].name = "公园"
        async with self.engine.session_state(SESSION) as live:
            query = await self.engine._parse_recall_query(live, "回忆一下公园里的事")
        expected = sorted(node.id for node in self.engine.world.nodes_in_zone("home"))
        self.assertEqual(sorted(query["nodes"]), expected)

    async def test_images_are_passed_to_the_model_and_cleared(self):
        """没配转述模型时，图片直接交给多模态主模型，回复完就清空。"""

        self.llm.replies = [SAY_REPLY]
        ctx = self.ctx(text="看看这张图", image_urls=["https://img/1.jpg"])
        outcome = await self.engine.handle_reply(ctx)

        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(self.llm.calls[-1]["image_urls"], ["https://img/1.jpg"])

        async with self.engine.session_state(SESSION) as state:
            self.engine._note_images(state, ["https://img/1.jpg", "https://img/2.jpg"])
            self.assertEqual(len(state.pending_images), 2)
        taken = await self.engine.take_pending_images(SESSION)
        self.assertEqual(taken, ["https://img/1.jpg", "https://img/2.jpg"])
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.pending_images, [])

    async def test_pending_images_respect_the_configured_limit(self):
        """「自上次回复以来最多带几张图」是配置项。"""

        self.engine.world.context.image_max = 2
        async with self.engine.session_state(SESSION) as state:
            self.engine._note_images(
                state, ["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"]
            )
            self.assertEqual(
                [item["url"] for item in state.pending_images],
                ["https://img/2.jpg", "https://img/3.jpg"],
            )

    async def test_vision_result_is_written_to_the_log(self):
        """图片转述成功/失败都要能在日志里查到。"""

        await self.engine.note_vision(
            SESSION, ok=True, images=2, detail="图片1：一只猫"
        )
        await self.engine.note_vision(
            SESSION, ok=False, images=1, detail="没配图片转述模型"
        )
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        kinds = [item["event_type"] for item in events]
        self.assertIn("vision", kinds)
        vision = [item for item in events if item["event_type"] == "vision"]
        self.assertEqual(len(vision), 2)

    async def test_proactive_speech_is_blocked_after_a_reply(self):
        """刚被搭话、她回完话之后，这段时间不再因为孤独感主动开口。"""

        self.engine.engagement.set_world(self.engine.world)
        async with self.engine.session_state(SESSION) as state:
            state.loneliness = 0.95  # 高到必然想找人
            state.current_plan = None
            state.current_action = None
            self.engine.engagement.note_passive_reply(state, tick_seconds=60.0)
            blocked_until = state.proactive_block_until

        self.assertGreater(blocked_until, 0)
        outcome = await self.engine.maybe_decide(SESSION, force=True)
        state = await self.engine.load_state(SESSION, cold_start=False)

        self.assertIsNone(state.current_plan, "冷却期内不该排「找人说话」的计划")
        self.assertTrue(
            any("不主动搭话" in note for note in (outcome.notes if outcome else [])),
            outcome.notes if outcome else None,
        )

    async def test_proactive_speech_resumes_after_cooldown(self):
        """冷却过去之后，孤独感又能让她找人说话。"""

        async with self.engine.session_state(SESSION) as state:
            state.loneliness = 0.95
            state.proactive_block_until = int(state.world_time)  # 已经过期

        outcome = await self.engine.maybe_decide(SESSION, force=True)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertIsNotNone(state.current_plan)
        self.assertIsNotNone(outcome)

    async def test_snapshot_exposes_nickname_lock(self):
        """编辑器靠这个字段决定名片那个按钮显示「锁定」还是「解锁」。"""

        async with self.engine.session_state(SESSION) as state:
            state.bot_base_nickname = "小鲸鱼"
            state.bot_nickname_locked = True

        snapshot = await self.engine.snapshot(SESSION)
        self.assertTrue(snapshot["nickname_locked"])
        self.assertEqual(snapshot["nickname_base"], "小鲸鱼")

    async def test_cancel_now_stops_action_and_plan(self):
        """对方明确说"别查了"：立刻停手 + 放弃剩下的安排。"""

        async with self.engine.session_state(SESSION) as state:
            state.current_action = {
                "type": "search_web",
                "elapsed_ticks": 1,
                "duration_ticks": 5,
                "interruptible": True,
            }
            state.state = "searching"
            from core import planner as planner_module

            state.current_plan = planner_module.create_plan(
                steps=[{"action": "read"}], world_time=state.world_time
            )

        async with self.engine.session_state(SESSION) as live:
            result = await self.engine.apply_cancel(
                live, "now", "别查资料了，先过来", TickOutcome(session_id=SESSION)
            )

        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertIn("停掉了", result)
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)

    async def test_model_cancel_always_applies(self):
        """模型既然判断要终止就照做：不再拿消息里有没有"别"字当门槛。

        否则就会出现「她嘴上说不干了，身体还在接着干」。
        """

        async with self.engine.session_state(SESSION) as state:
            from core import planner as planner_module

            state.current_plan = planner_module.create_plan(
                steps=[{"action": "read"}], world_time=state.world_time
            )

        outcome = TickOutcome(session_id=SESSION)
        async with self.engine.session_state(SESSION) as live:
            result = await self.engine.apply_cancel(
                live, "queue", "（这句话里没有停止词）", outcome
            )

        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertIn("放弃", result)
        self.assertIsNone(state.current_plan)

    async def test_cancel_respects_uninterruptible_action(self):
        """不可打断的动作（例如睡觉）不做一半就扔，但剩下的安排照样放弃。"""

        async with self.engine.session_state(SESSION) as state:
            from core import planner as planner_module

            state.current_action = {
                "type": "sleep",
                "elapsed_ticks": 1,
                "duration_ticks": 30,
                "interruptible": False,
            }
            state.current_plan = planner_module.create_plan(
                steps=[{"action": "read"}], world_time=state.world_time
            )

        outcome = TickOutcome(session_id=SESSION)
        async with self.engine.session_state(SESSION) as live:
            result = await self.engine.apply_cancel(live, "now", "别睡了，起来", outcome)

        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertIn("不可打断", result)
        self.assertIsNotNone(state.current_action)
        self.assertIsNone(state.current_plan)

    async def test_cancel_queue_keeps_current_action(self):
        """cancel=queue：手上这件做完，但不要再按原计划走。"""

        async with self.engine.session_state(SESSION) as state:
            from core import planner as planner_module

            state.current_action = {
                "type": "search_web",
                "elapsed_ticks": 1,
                "duration_ticks": 5,
                "interruptible": True,
            }
            state.current_plan = planner_module.create_plan(
                steps=[{"action": "read"}], world_time=state.world_time
            )

        async with self.engine.session_state(SESSION) as live:
            await self.engine.apply_cancel(
                live, "queue", "不用按原来的来了", TickOutcome(session_id=SESSION)
            )

        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertIsNotNone(state.current_action)
        self.assertIsNone(state.current_plan)

    async def test_second_tool_gets_previous_result(self):
        """前一个工具查到的网址要传给后一个工具，而不是让补全模型自己猜。"""

        self.tools._tools = {"web_search": "搜索网页", "web_fetch": "抓网页"}
        self.tools.results = {
            "web_search": "结果：https://real.example/news",
            "web_fetch": "正文内容",
        }
        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.schemas["web_fetch"] = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "search_web":
                action["tool_names"] = ["web_search", "web_fetch"]
        self.store.save_world(raw)
        self.engine.reload_config()

        definition = self.engine.world.action_map()["search_web"]
        state = await self.set_state(node_id="study")
        action = PlannedAction(
            type="search_web",
            intent="查今天的新闻",
            params={"query": "今天的新闻"},
        )
        # 准备阶段还不知道搜出来的网址，模型只能先猜一个
        self.llm.replies = ['{"url": "https://guess.example"}']
        outcome = TickOutcome(session_id=SESSION)
        self.assertTrue(
            await self.engine._prepare_tool_action(state, definition, action, outcome),
            outcome.notes,
        )

        payload = {
            "type": "search_web",
            "intent": "查今天的新闻",
            "params": dict(action.params),
            "tool_params": dict(action.tool_params),
        }
        self.llm.replies = ['{"url": "https://real.example/news"}']
        await self.engine._run_tool_calls(state, definition, payload)

        self.assertEqual(
            [name for name, _params in self.tools.calls], ["web_search", "web_fetch"]
        )
        self.assertEqual(self.tools.calls[1][1]["url"], "https://real.example/news")
        self.assertIn("https://real.example/news", self.llm.calls[-1]["prompt"])

    async def test_two_tool_action_still_skips_without_tools(self):
        """多个工具都不存在时，一样要跳过并说明原因。"""

        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "search_web":
                action["tool_names"] = ["nope_one", "nope_two"]
        self.store.save_world(raw)
        self.engine.reload_config()

        definition = self.engine.world.action_map()["search_web"]
        state = await self.set_state(node_id="study")
        outcome = TickOutcome(session_id=SESSION)
        ok = await self.engine._prepare_tool_action(
            state, definition, PlannedAction(type="search_web", intent="查新闻"), outcome
        )
        self.assertFalse(ok)
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        skips = [item for item in events if item["event_type"] == "skip"]
        self.assertTrue(skips)
        self.assertIn("找不到可用工具", skips[0]["detail"]["note"])

    async def test_instant_tool_action_calls_tool(self):
        """瞬时工具型动作也要真的调用工具，而不是只说一句就完了。"""

        self.tools._tools = {"hsearch": "搜索网页"}
        self.tools.schemas["hsearch"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.results = {"hsearch": "搜到一条新闻"}
        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "lookup_news",
                "name": "查新闻",
                "category": "instant",
                "llm_level": "tool",
                "tool_names": ["hsearch"],
                "scope": "global",
                "visible": False,
                "on_complete": {
                    "trigger": "llm_followup",
                    "prompt_hint": "讲讲查到了什么",
                },
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_schedule(
            id="instant_lookup",
            time="12:00",
            action_chain=[
                {"type": "lookup_news", "intent": "查今天新闻", "params": {"query": "今天新闻"}}
            ],
        )
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.run_schedules()

        self.assertEqual([name for name, _params in self.tools.calls], ["hsearch"])
        self.assertEqual(self.tools.calls[0][1]["query"], "今天新闻")

    def test_memory_summary_refusal_is_dropped(self):
        """模型反过来"要人名"的那句不是记忆，直接丢掉，让兜底文案接手。"""

        refusal = "这段记忆里没有出现对方的名字，两条都是「我」在说，我无法凭空捏造一个人名。"
        self.assertEqual(self.engine._clean_memory_summary(refusal), "")
        self.assertEqual(
            self.engine._clean_memory_summary("我在厨房做好鱼，招呼人来蹭一口。"),
            "我在厨房做好鱼，招呼人来蹭一口",
        )

    def test_memory_fallback_keeps_her_own_words(self):
        """只有她自己说话时，兜底记忆直接用她自己的话，不套「我说」那层壳。"""

        entries = [
            {"name": "我", "text": "我在厨房做好鱼", "is_self": True},
            {"name": "我", "text": "招呼人来蹭一口", "is_self": True},
        ]
        text = self.engine._fallback_memory_text(entries)
        self.assertIn("厨房", text)
        self.assertNotIn("我说", text)

        mixed = entries + [{"name": "小明", "text": "好香", "is_self": False}]
        self.assertIn("小明", self.engine._fallback_memory_text(mixed))

    def switch_to_other_search_tool(self, action_id: str = "search_web", node_id: str = "study"):
        """模拟用户把 web_search 换成另一个插件提供的搜索工具。"""

        self.tools._tools = {"hsearch": "另一个插件提供的搜索"}
        raw = self.store.raw_world()
        for node in raw["nodes"]:
            if node["id"] == node_id:
                node["allowed_tools"] = ["hsearch"]
        for action in raw["actions"]:
            if action["id"] == action_id:
                action["tool_name"] = "hsearch"
                action["tool_names"] = ["hsearch"]
                action["tool_fallbacks"] = []
                # 故意留下过期的前置条件，还原用户踩到的那个坑
                action.setdefault("preconditions", {})["tool_available"] = ["web_search"]
        self.store.save_world(raw)
        self.engine.reload_config()

    async def test_swapped_tool_ignores_stale_precondition(self):
        """换了工具之后，旧前置条件里的 web_search 不该再拦住动作。"""

        self.switch_to_other_search_tool()
        self.add_schedule(
            id="news_with_new_tool",
            time="12:00",
            action_chain=[{"type": "search_web", "params": {"query": "今天的新闻"}}],
        )
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))

        outcomes = await self.engine.run_schedules()
        self.assertFalse(
            any("缺少工具" in note for note in outcomes[0].notes), outcomes[0].notes
        )
        for _ in range(8):
            if self.tools.calls:
                break
            await self.engine.tick()
        self.assertEqual(len(self.tools.calls), 1)
        self.assertEqual(self.tools.calls[0][0], "hsearch")
        self.assertEqual(self.tools.calls[0][1]["query"], "今天的新闻")

    async def test_tool_action_without_any_tool_is_skipped_with_reason(self):
        """一个工具都没注册时，跳过原因要写清楚，而不是静默什么都不做。"""

        self.tools._tools = {}
        self.add_schedule(
            id="no_tool_news",
            time="12:00",
            action_chain=[{"type": "search_web", "params": {"query": "今天的新闻"}}],
        )
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))

        outcomes = await self.engine.run_schedules()
        self.assertIsNone((await self.get_state()).current_action)
        self.assertFalse(self.tools.calls)
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        skips = [item for item in events if item["event_type"] == "skip"]
        self.assertTrue(skips, events)
        # 报错里要带上"现在有哪些工具"，才知道该怎么改
        self.assertIn("web_search", skips[0]["detail"]["note"])
        self.assertIn("没有注册任何工具", skips[0]["detail"]["note"])

    # ---------------- 打断与状态 ----------------

    async def test_later_actions_wait_for_the_current_one(self):
        """同一轮里「先移动再说话」要排队：不能边走边说，更不能把移动顶掉。"""

        self.llm.replies = [
            '{"reasoning":{"intent":"去大厅看看"},'
            '"actions":[{"type":"walk_to","target_node":"lobby"},'
            '{"type":"say","messages":["到大厅啦"]}]}'
        ]
        ctx = self.ctx(text="去大厅转转")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)

        state = await self.get_state()
        self.assertEqual((state.current_action or {}).get("type"), "walk_to")
        self.assertEqual((state.current_plan or {}).get("reason"), "同一轮里还没做完的动作")
        self.assertNotIn("到大厅啦", self.messenger.flat_messages)

        await self.engine.tick()  # 走到大厅后才说
        state = await self.get_state()
        self.assertEqual(state.node_id, "lobby")
        self.assertIn("到大厅啦", self.messenger.flat_messages)

    async def test_plan_prompt_mentions_the_time_and_allows_long_plans(self):
        """计划提示词别写死"接下来 15~30 分钟"，否则深夜也只会挑小睡。"""

        await self.set_state(energy=0.2, node_id="bedroom")
        self.llm.replies = [
            '{"plan":[{"action":"sleep","duration":28800}],"reason":"太晚了，去睡"}'
        ]
        outcome = TickOutcome(session_id=SESSION)
        state = await self.get_state()
        plan = await self.engine._ask_llm_for_plan(
            state, self.engine.node("bedroom"), outcome, force=True
        )
        prompt = self.llm.calls[-1]["prompt"]
        self.assertIn("计划的长度由你决定", prompt)
        self.assertIn("深夜或精力见底就该去睡觉", prompt)
        self.assertIsNotNone(plan)
        system_prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("现在是：", system_prompt)

    async def test_last_generated_plan_is_remembered(self):
        from core.planner import create_plan

        await self.set_state(node_id="bedroom")
        plan = create_plan(
            steps=[{"action": "sleep", "duration": 100}],
            world_time=0,
            reason="精力过低，该休息了",
            source="rule",
        )
        async with self.engine.session_state(SESSION) as state:
            await self.engine._apply_plan(
                state,
                self.engine.node("bedroom"),
                TickOutcome(session_id=SESSION),
                plan,
            )
            state.current_plan = None  # 做完了：只剩"最近一次生成的计划"

        state = await self.get_state()
        self.assertEqual(
            [item["action"] for item in state.last_plan["steps"]], ["sleep"]
        )
        self.assertEqual(state.last_plan["reason"], "精力过低，该休息了")

        prompt = await self.engine.preview_autonomous_prompt(SESSION)
        self.assertIn("你上一次安排好的是：睡觉", prompt)
        self.assertIn("精力过低，该休息了", prompt)

    async def test_arrival_at_new_node_triggers_a_decision(self):
        """走到新地方后立刻做一次决策，而不是愣在那儿等下个评估周期。"""

        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.llm.replies = [
            '{"reasoning":{"intent":"去书房"},'
            '"actions":[{"type":"walk_to","target_node":"study"}]}',
            '{"plan":[{"action":"search_web","params":{"query":"今天的新闻"}}],'
            '"reason":"刚进书房，顺手查点东西","valid_until":1800}',
        ]
        await self.set_state(node_id="bedroom")  # 从卧室走过去才有"抵达"这件事
        ctx = self.ctx(text="去书房")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        await self.engine.tick()  # 卧室 -> 大厅
        await self.engine.tick()  # 大厅 -> 书房：落地并就地决策

        state = await self.get_state()
        self.assertEqual(state.node_id, "study")
        # 计划里的「上网搜索」是瞬时动作：落地决策这一下就查完了，
        # 所以留痕在工具调用记录里，而不是挂在 current_plan / current_action 上
        self.assertEqual([name for name, _params in self.tools.calls], ["web_search"])
        self.assertEqual(self.tools.calls[0][1]["query"], "今天的新闻")
        # 这次决策要明确告诉她「为什么走到这儿」，而不是让她重新规划一整段时间
        prompts = [call["prompt"] for call in self.llm.calls]
        self.assertTrue(
            any("你刚刚特地走到了「书房」" in text for text in prompts), prompts
        )
        self.assertTrue(any("去书房" in text for text in prompts), prompts)


    async def test_interrupt_stops_continuous_action(self):
        await self.set_state(loneliness=0.9, node_id="study")
        await self.engine.maybe_decide(SESSION)
        self.assertIsNotNone((await self.get_state()).current_action)
        interrupted = await self.engine.interrupt(SESSION)
        self.assertTrue(interrupted)
        self.assertIsNone((await self.get_state()).current_action)

    async def test_wake_up_clears_sleep(self):
        await self.set_state(state="sleeping", current_action={"type": "sleep", "duration_ticks": 10})
        was_sleeping = await self.engine.wake_up(SESSION)
        self.assertTrue(was_sleeping)
        state = await self.get_state()
        self.assertEqual(state.state, "idle")
        self.assertIsNone(state.current_action)

    async def test_being_called_awake_interrupts_sleep(self):
        await self.set_state(state="sleeping", current_action={"type": "sleep", "duration_ticks": 10})
        injection = await self.engine.handle_incoming(self.ctx(text="醒醒，别睡了"))
        state = await self.get_state()
        self.assertEqual(state.state, "idle")
        self.assertIsNone(state.current_action)
        assert injection is not None
        self.assertIn("被叫醒", injection)

    # ---------------- 睡觉时的门禁 ----------------

    async def test_sleep_guard_blocks_unmentioned_messages(self):
        """睡着时没 @ 她的消息会被整个挡下（方案 A）。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        blocked = await self.engine.should_block_sleep(
            self.ctx(text="今天好热啊", is_wake=False, is_mentioned=False)
        )
        self.assertTrue(blocked)
        # 被挡下的消息也要进聊天留档，醒来才知道发生过什么
        state = await self.get_state()
        self.assertTrue(any(item["text"] == "今天好热啊" for item in state.recent_chat))

    async def test_sleep_guard_leaves_mentions_and_commands_alone(self):
        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        # @ 她但没说唤醒词：交给 sleep_gate 回固定文案
        self.assertFalse(
            await self.engine.should_block_sleep(self.ctx(text="在吗", is_mentioned=True))
        )
        # 明确叫醒：放行
        self.assertFalse(
            await self.engine.should_block_sleep(self.ctx(text="醒醒", is_mentioned=True))
        )
        # 私聊等同被叫
        self.assertFalse(
            await self.engine.should_block_sleep(
                self.ctx(text="在吗", is_wake=False, is_mentioned=False, is_private=True)
            )
        )

    async def test_sleep_guard_scope_all_blocks_mentions_too(self):
        raw = self.store.raw_world()
        raw["sleep"] = {"block_scope": "all"}
        self.store.save_world(raw)
        self.engine.reload_config()
        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        self.assertTrue(
            await self.engine.should_block_sleep(self.ctx(text="在吗", is_mentioned=True))
        )

    async def test_sleep_guard_can_be_switched_to_plan_b(self):
        """关掉「连其他插件一起挡」后不再拦截，但仍不会出声。"""

        raw = self.store.raw_world()
        raw["sleep"] = {"block_plugins": False}
        self.store.save_world(raw)
        self.engine.reload_config()
        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        self.assertFalse(
            await self.engine.should_block_sleep(
                self.ctx(text="今天好热啊", is_wake=False, is_mentioned=False)
            )
        )
        gate = await self.engine.sleep_gate(
            self.ctx(text="今天好热啊", is_wake=False, is_mentioned=False)
        )
        self.assertEqual(gate.mode, "silent")

    async def test_sleep_guard_ignores_a_fake_wake_flag(self):
        """意图路由会把 is_wake 置 True，但那不算"有人在跟她说话"。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        gate = await self.engine.sleep_gate(
            self.ctx(text="今天好热啊", is_wake=True, is_mentioned=False)
        )
        self.assertEqual(gate.mode, "silent")

    async def test_awake_sessions_are_not_blocked(self):
        await self.set_state(state="idle")
        self.assertFalse(
            await self.engine.should_block_sleep(
                self.ctx(text="今天好热啊", is_wake=False, is_mentioned=False)
            )
        )

    async def test_sleeping_bot_answers_with_the_configured_template(self):
        """睡着时被 @：只回一句固定文案，不调大模型、不执行动作。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        gate = await self.engine.sleep_gate(self.ctx(text="在吗"))
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate.mode, "template")
        self.assertEqual(len(gate.messages), 1)
        self.assertIn("睡觉中", gate.messages[0])
        self.assertEqual(self.llm.calls, [])

    async def test_sleeping_bot_stays_quiet_when_not_addressed(self):
        """睡觉时没 @ 她的消息：完全不回，也不要交给主人格。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        gate = await self.engine.sleep_gate(
            self.ctx(text="今天好热啊", is_wake=False, is_mentioned=False)
        )
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate.mode, "silent")
        self.assertEqual(gate.messages, [])

    async def test_wake_words_need_a_mention(self):
        """唤醒词必须配上 @ 才算叫醒：没 @ 她的话叫不醒，也不会打扰群里。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        plain = await self.engine.sleep_gate(
            self.ctx(text="起床了", is_wake=False, is_mentioned=False)
        )
        self.assertIsNotNone(plain)
        assert plain is not None
        self.assertEqual(plain.mode, "silent")

        called = await self.engine.sleep_gate(self.ctx(text="起床了", is_wake=True))
        self.assertIsNone(called)  # 交给正常路径叫醒她
        state = await self.get_state()
        self.assertEqual(state.state, "sleeping")  # 门禁本身不改状态

    async def test_sleep_template_has_a_cooldown(self):
        """连着被 @ 不会刷屏：冷却期内保持安静。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        first = await self.engine.sleep_gate(self.ctx(text="在吗"))
        second = await self.engine.sleep_gate(self.ctx(text="在吗"))
        assert first is not None and second is not None
        self.assertEqual(first.mode, "template")
        self.assertEqual(second.mode, "silent")

        self.clock.advance(10 * 60)
        third = await self.engine.sleep_gate(self.ctx(text="在吗"))
        self.assertEqual(third.mode, "template")

    async def test_sleep_reply_mode_normal_disables_the_gate(self):
        raw = self.store.raw_world()
        raw["sleep"] = {"reply_mode": "normal"}
        self.store.save_world(raw)
        self.engine.reload_config()
        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        self.assertIsNone(await self.engine.sleep_gate(self.ctx(text="在吗")))

    async def test_wake_up_note_reaches_the_takeover_prompt(self):
        """「刚被叫醒」这句提示接管模式也要带上，并且要提醒她把交代的事做了。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        self.llm.replies = [
            '{"reasoning":{"intent":"去书房查新闻"},'
            '"actions":[{"type":"walk_to","target_node":"study"},'
            '{"type":"search_web","intent":"查今天的新闻"}]}'
        ]
        ctx = self.ctx(text="醒醒，去书房帮我查今天的新闻")
        await self.engine.handle_incoming(ctx)
        outcome = await self.engine.handle_reply(ctx)

        system_prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("刚被叫醒", system_prompt)
        self.assertIn("不只是叫你起来", system_prompt)
        self.assertIn("去书房帮我查今天的新闻", self.llm.calls[-1]["prompt"])
        # 提示归提示，动作照做
        state = await self.get_state()
        steps = [item.get("action") for item in (state.current_plan or {}).get("steps", [])]
        types = steps + [str((state.current_action or {}).get("type") or "")]
        self.assertIn("search_web", types, (state.current_plan, state.current_action))
        self.assertTrue(outcome.ok or steps, (outcome.error, steps))

    async def test_wake_up_note_is_consumed_once(self):
        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        self.llm.replies = ['{"actions":[{"type":"say","messages":["唔…醒了"]}]}']
        ctx = self.ctx(text="醒醒")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        first = self.llm.calls[-1]["system_prompt"]
        self.assertIn("刚被叫醒", first)

        self.llm.replies = ['{"actions":[{"type":"say","messages":["嗯"]}]}']
        second_ctx = self.ctx(text="你在干嘛")
        await self.engine.handle_incoming(second_ctx)
        await self.engine.handle_reply(second_ctx)
        self.assertNotIn("刚被叫醒", self.llm.calls[-1]["system_prompt"])

    async def test_wake_up_clears_the_plan_and_keeps_her_up(self):
        """叫醒会清掉排队的计划，并给一段「别再睡」的保护期。"""

        await self.set_state(
            state="sleeping",
            energy=0.05,
            current_action={"type": "sleep", "duration_ticks": 480},
            current_plan={"steps": [{"action": "say", "status": "pending"}], "current_step": 0},
        )
        await self.engine.handle_incoming(self.ctx(text="醒醒，起来啦"))
        state = await self.get_state()
        self.assertEqual(state.state, "idle")
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)
        self.assertGreater(state.no_sleep_until, state.world_time)
        # 保护期内规则不会再安排她回去睡（精力只有 0.05，本来一定会触发）
        self.assertIsNone(self.engine.decider.rule_plan(state))

    async def test_share_limit_blocks_extra_shares(self):
        raw = self.store.raw_schedules()
        raw["schedules"].append(
            {
                "id": "share_thing",
                "enabled": True,
                "time": "12:00",
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "action_chain": [{"type": "share", "messages": ["今天天气真好"]}],
                "conditions": {},
                "priority": 5,
            }
        )
        self.store.save_schedules(raw)
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))

        # 第一次分享应当发出
        await self.engine.tick()
        self.assertIn("今天天气真好", self.messenger.flat_messages)
        state = await self.get_state()
        self.assertEqual(state.share_count_hour, 1)

        # 把这个小时的额度改成 1 并且已经用完 -> 不再发送
        raw_world = self.store.raw_world()
        raw_world["limits"]["max_share_per_hour"] = 1
        self.store.save_world(raw_world)
        self.engine.reload_config()
        async with self.engine.session_state(SESSION) as state:
            state.share_count_hour = 1
            state.share_hour_marker = self.engine._hour_index(state)

        raw = self.store.raw_schedules()
        raw["schedules"] = [item for item in raw["schedules"] if item["id"] != "share_thing"]
        raw["schedules"].append(
            {
                "id": "share_again",
                "enabled": True,
                "time": "12:30",
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "action_chain": [{"type": "share", "messages": ["又想说一句"]}],
                "conditions": {},
                "priority": 5,
            }
        )
        self.store.save_schedules(raw)
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 12, 30))
        await self.engine.tick()
        self.assertNotIn("又想说一句", self.messenger.flat_messages)

    async def test_snapshot_contains_expected_fields(self):
        snapshot = await self.engine.snapshot(SESSION)
        for key in ("session_id", "world_time", "node_id", "mood", "values", "tick_seconds"):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["node_id"], self.node_id)

    # ---------------- 群名片 ----------------

    async def test_nickname_uses_bot_name_when_base_is_empty(self):
        """全新安装时状态里没有"原名"，要回落到全局设置里的 Bot 名称，否则永远算不出名片。"""

        raw = self.store.raw_world()
        raw["bot_name"] = "小鲸鱼"
        self.store.save_world(raw)
        self.engine.reload_config()
        await self.set_state(state="sleeping")
        # 她当前名片是空的，但 messenger 能"读到"原名片
        self.messenger.card = ""
        await self.engine.tick()
        state = await self.get_state()
        self.assertIn("小鲸鱼", state.bot_current_nickname)
        self.assertEqual(self.messenger.cards[-1][1], state.bot_current_nickname)

    async def test_nickname_captures_original_card_once(self):
        """第一次遇到这个群时，先把她当前的名片读回来当原名。"""

        await self.set_state(state="idle")
        self.messenger.card = "蓝色大肥鱼"
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.bot_base_nickname, "蓝色大肥鱼")

    async def test_nickname_failure_is_logged(self):
        """改名片失败要能在日志里看到原因，而不是静默什么都不发生。"""

        self.messenger.card_result = False
        await self.set_state(state="sleeping", bot_base_nickname="小鲸鱼")
        await self.engine.tick()
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        nicknames = [item for item in events if item["event_type"] == "nickname"]
        self.assertTrue(nicknames, events)
        self.assertFalse(nicknames[0]["detail"]["ok"])
        self.assertIn("测试里指定失败", nicknames[0]["detail"]["note"])

    async def test_nickname_failures_back_off(self):
        """协议端挂掉时，群名片不能每 tick 都重试（否则日志一直刷）。"""

        self.messenger.card_result = False
        await self.set_state(state="sleeping", bot_base_nickname="小鲸鱼")
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.nickname_fail_count, 1)
        attempts = len(self.messenger.cards)

        for _ in range(3):
            await self.engine.tick()
        self.assertEqual(len(self.messenger.cards), attempts)

    async def test_nickname_backoff_resets_after_success(self):
        self.messenger.card_result = False
        await self.set_state(state="sleeping", bot_base_nickname="小鲸鱼")
        await self.engine.tick()
        self.assertEqual((await self.get_state()).nickname_fail_count, 1)

        self.messenger.card_result = True
        await self.set_state(last_nickname_update_at=0.0, nickname_fail_count=0)
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.nickname_fail_count, 0)
        self.assertIn("睡觉中", state.bot_current_nickname)

    async def test_send_failure_is_logged_without_retrying(self):
        """发送失败：不重试，但日志里要能看到"这条没发出去"。"""

        class _FailingMessenger(StubMessenger):
            def __init__(self) -> None:
                super().__init__()
                self.fail_note = ""
                self.blocked_flag = False

            def blocked(self, session_id: str) -> bool:
                return self.blocked_flag

            def take_fail_note(self, session_id: str) -> str:
                note = self.fail_note
                self.fail_note = ""
                return note

            async def send_text(self, session_id: str, messages: list[str]) -> bool:
                self.sent.append((session_id, list(messages)))
                self.fail_note = "ActionFailed: Timeout"
                self.blocked_flag = True
                return False

        self.engine.messenger = _FailingMessenger()
        outcome = TickOutcome(session_id=SESSION)
        outcome.messages = ["我在这儿呢"]
        await self.engine._deliver(outcome)
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        failed = [item for item in events if item["event_type"] == "send_failed"]
        self.assertTrue(failed, events)
        self.assertIn("我在这儿呢", failed[0]["detail"]["messages"])
        self.assertIn("Timeout", failed[0]["detail"]["note"])

    async def test_nickname_success_is_logged(self):
        await self.set_state(state="sleeping", bot_base_nickname="小鲸鱼")
        await self.engine.tick()
        state = await self.get_state()
        self.assertIn("睡觉中", state.bot_current_nickname)
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        nicknames = [item for item in events if item["event_type"] == "nickname"]
        self.assertTrue(nicknames, events)
        self.assertTrue(nicknames[0]["detail"]["ok"])

    async def test_nickname_sync_sets_group_card(self):
        await self.set_state(state="sleeping", bot_base_nickname="小鲸鱼")
        await self.engine.tick()
        cards = [card for _session, card in self.messenger.cards]
        self.assertIn("小鲸鱼 | 睡觉中", cards)

    async def test_nickname_sync_skipped_when_locked(self):
        await self.set_state(
            state="sleeping", bot_base_nickname="小鲸鱼", bot_nickname_locked=True
        )
        await self.engine.tick()
        self.assertEqual(self.messenger.cards, [])

    # ---------------- 多会话隔离 ----------------

    async def test_sessions_are_isolated(self):
        self.store.add_session(OTHER_SESSION, cold_start_node="bedroom")
        self.engine.reload_config()
        await self.set_state(node_id="bar", world_time=99)
        other = await self.engine.load_state(OTHER_SESSION)
        self.assertEqual(other.node_id, "bedroom")
        self.assertEqual(other.world_time, 0)
        await self.engine.tick()
        self.assertEqual((await self.engine.load_state(OTHER_SESSION)).world_time, 1)
        self.assertEqual((await self.get_state()).world_time, 100)

    # ---------------- 热加载 ----------------

    async def test_hot_reload_picks_up_new_node(self):
        raw = self.store.raw_world()
        raw["nodes"].append(
            {
                "id": "garden",
                "name": "花园",
                "prompt": "有花有草",
            }
        )
        raw["edges"].append({"id": "e_garden", "from": "lobby", "to": "garden", "ticks": 1})
        self.store.save_world(raw)
        self.engine.reload_config()
        self.assertIn("garden", self.engine.world.node_map())
        injection = await self.engine.handle_incoming(self.ctx())
        self.assertIsNotNone(injection)


    async def test_llm_duration_is_clamped_and_scales_effects(self):
        """时长交给大模型决定：超范围会被夹住，完成效果按实际分钟数缩放。"""

        raw = self.store.raw_world()
        for key in (
            "energy_decay_per_min",
            "loneliness_growth_per_min",
            "curiosity_growth_per_min",
            "affect_decay_per_min",
            "boredom_growth_per_min",
            "nap_energy_recovery_per_min",
        ):
            raw["state_dynamics"][key] = 0.0
        raw["actions"].append(
            {
                "id": "nap_llm",
                "name": "弹性小睡",
                "category": "continuous",
                "llm_level": "template",
                "scope": "global",
                "target_type": "none",
                "duration_mode": "llm",
                "duration_min": 600,
                "duration_max": 1800,
                "interruptible": True,
                "during": {"state": "napping"},
                "on_complete": {
                    "trigger": "none",
                    "effects_per_minute": {"energy": "+0.002"},
                },
                "visible": False,
            }
        )
        self.store.save_world(raw)
        self.set_schedules(
            [
                {
                    "id": "nap_now",
                    "enabled": True,
                    "time": "15:00",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": [{"type": "nap_llm", "duration": 99999}],
                    "conditions": {},
                    "priority": 9,
                }
            ]
        )
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 15, 0))

        before = await self.get_state()
        await self.engine.tick()
        state = await self.get_state()
        action = state.current_action or {}
        self.assertEqual(action.get("type"), "nap_llm")
        # 大模型给的 99999 秒被上限 1800 秒夹住 => 30 tick
        self.assertEqual(action.get("duration_ticks"), 30)

        for _ in range(30):
            await self.engine.tick()
        after = await self.get_state()
        self.assertIsNone(after.current_action)
        # 每持续 1 分钟 +0.002，实际 30 分钟 => +0.06（自然变化已在配置里清零）
        self.assertAlmostEqual(after.energy - before.energy, 0.06, places=3)

    async def test_tool_action_needs_its_own_required_params(self):
        """工具参数由工具 schema 决定：缺必填就跳过，给全了就调用。"""

        self.engine.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        }
        self.set_schedules(
            [
                {
                    "id": "search_missing",
                    "enabled": True,
                    "time": "16:00",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": [{"type": "search_web"}],
                    "conditions": {},
                    "priority": 9,
                }
            ]
        )
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 16, 0))
        await self.engine.tick()
        self.assertEqual(self.tools.calls, [])

        self.set_schedules(
            [
                {
                    "id": "search_ok",
                    "enabled": True,
                    "time": "16:00",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": [
                        {"type": "search_web", "params": {"query": "今天的新闻"}}
                    ],
                    "conditions": {},
                    "priority": 9,
                }
            ]
        )
        self.engine.reload_config()
        await self.engine.tick()  # 启动动作
        await self.engine.tick()  # 动作完成 -> 调工具
        self.assertEqual(len(self.tools.calls), 1)
        self.assertEqual(self.tools.calls[0][1]["query"], "今天的新闻")

    def set_schedules(self, schedules: list[dict]) -> None:
        self.store.save_schedules({"schedules": schedules})

    async def test_template_action_uses_bot_name_and_applies_effects(self):
        """互动类动作走模板：{bot} 换成 bot 名称，并带上数值联动。"""

        raw = self.store.raw_world()
        raw["bot_name"] = "小鲸鱼"
        self.store.save_world(raw)
        self.set_schedules(
            [
                {
                    "id": "hug_now",
                    "enabled": True,
                    "time": "17:00",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": [{"type": "hug", "target": "42"}],
                    "conditions": {},
                    "priority": 9,
                }
            ]
        )
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 17, 0))
        before = await self.get_state()
        await self.engine.tick()
        self.assertIn("（小鲸鱼抱了你一下）", self.messenger.flat_messages)
        after = await self.get_state()
        self.assertLess(after.loneliness, before.loneliness)
        self.assertGreater(after.affect, before.affect)

    async def test_template_falls_back_to_nickname_when_bot_name_empty(self):
        await self.set_state(bot_base_nickname="小蓝鱼")
        self.set_schedules(
            [
                {
                    "id": "hug_now2",
                    "enabled": True,
                    "time": "17:10",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": [{"type": "hug", "target": "42"}],
                    "conditions": {},
                    "priority": 9,
                }
            ]
        )
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 17, 10))
        await self.engine.tick()
        self.assertIn("（小蓝鱼抱了你一下）", self.messenger.flat_messages)

    async def test_cook_action_uses_llm_duration_and_shares(self):
        """做饭：时长由大模型定，做完把经历讲成人话。"""

        self.set_schedules(
            [
                {
                    "id": "cook_now",
                    "enabled": True,
                    "time": "18:00",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": [
                        {"type": "walk_to", "target_node": "kitchen"},
                        {"type": "cook", "duration": 1800},
                    ],
                    "conditions": {},
                    "priority": 9,
                }
            ]
        )
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 18, 0))
        await self.engine.tick()  # 触发日程：开始走向厨房
        for _ in range(3):
            await self.engine.tick()  # 书房 -> 大厅 -> 吧台 -> 厨房
        state = await self.get_state()
        self.assertEqual(state.node_id, "kitchen")
        self.assertEqual((state.current_action or {}).get("type"), "cook")
        # 1800 秒 = 30 tick（在 llm 模式的 900~3600 区间内）
        self.assertEqual((state.current_action or {}).get("duration_ticks"), 30)
        self.assertEqual(state.state, "cooking")
        for _ in range(30):
            await self.engine.tick()
        self.assertIn("有人在吗？", self.messenger.flat_messages)

    async def test_overview_lists_every_session_position(self):
        """地图页要能一次看到所有会话分别在哪。"""

        self.store.add_session(OTHER_SESSION, cold_start_node="bedroom")
        self.engine.reload_config()
        await self.set_state(node_id="bar")
        overview = await self.engine.overview()
        by_id = {item["session_id"]: item for item in overview}
        self.assertIn(SESSION, by_id)
        self.assertIn(OTHER_SESSION, by_id)
        self.assertEqual(by_id[SESSION]["node_id"], "bar")
        self.assertEqual(by_id[SESSION]["node_name"], "吧台")
        # 另一个会话还没被 tick 过，用冷启动节点
        self.assertEqual(by_id[OTHER_SESSION]["node_id"], "bedroom")

    async def test_snapshot_reports_travel_time(self):
        snapshot = await self.engine.snapshot(SESSION)
        travel = {item["node_id"]: item["ticks"] for item in snapshot["travel"]}
        # 冷启动在书房，可直达大厅与窗边，各 1 tick
        self.assertEqual(travel.get("lobby"), 1)
        self.assertEqual(travel.get("window"), 1)

    async def test_group_chat_is_recorded_for_interjection(self):
        """群聊内容会被记录，作为她判断"大家在聊什么"的上下文。"""

        await self.engine.note_presence(self.ctx(text="今天中午吃什么好呢", user_id="1", user_name="小明"))
        await self.engine.note_presence(self.ctx(text="我想吃面", user_id="2", user_name="小红"))
        state = await self.get_state()
        self.assertEqual(len(state.recent_chat), 2)
        self.assertTrue(self.engine.group_is_chatting(state))
        context = self.engine.chat_context(state)
        self.assertIn("今天中午吃什么好呢", [item["text"] for item in context])

    async def test_interjection_sends_a_message(self):
        """孤独感高 + 群里在聊 -> 她主动插一句（文案由大模型给）。"""

        await self.engine.note_presence(self.ctx(text="你们觉得这个周末去哪玩好", user_id="1", user_name="小明"))
        await self.engine.note_presence(self.ctx(text="我觉得去爬山不错", user_id="2", user_name="小红"))
        await self.set_state(loneliness=0.9, node_id="lobby")
        outcome = await self.engine.maybe_decide(SESSION)
        self.assertIsNotNone(outcome)
        self.assertIn("有人在吗？", self.messenger.flat_messages)
        state = await self.get_state()
        self.assertGreater(state.last_interject_at, 0)
        # 插话冷却内不会连着插
        self.assertFalse(self.engine.interject_allowed(state))

    async def test_quiet_group_does_not_interject(self):
        # 0.65 高于插话阈值(0.6)但低于"去大厅找人"阈值(0.7)，
        # 这样测的才是"群里没人聊 -> 不插话"，而不是被别的规则接走
        await self.set_state(loneliness=0.65, node_id="lobby")
        outcome = await self.engine.maybe_decide(SESSION)
        self.assertIsNotNone(outcome)
        self.assertNotIn("有人在吗？", self.messenger.flat_messages)

    async def test_same_message_is_not_recorded_twice(self):
        """同一条消息会同时经过两个钩子，聊天上下文里不能重复出现。"""

        ctx = self.ctx(text="今天晚上吃什么", user_id="1", user_name="小明")
        await self.engine.note_presence(ctx)
        await self.engine.handle_incoming(ctx)
        state = await self.get_state()
        texts = [item["text"] for item in state.recent_chat]
        self.assertEqual(texts.count("今天晚上吃什么"), 1)

    # ---------------- 接管回复 ----------------

    async def test_takeover_reply_sends_messages(self):
        """接管模式：自己拿 JSON、执行动作、把 say 内容交回去。"""

        ctx = self.ctx(text="你在干嘛呀")
        await self.engine.handle_incoming(ctx)  # 先记录消息影响
        outcome = await self.engine.handle_reply(ctx)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.messages, ["有人在吗？"])
        # 提示词里带上了 reasoning 契约
        system_prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("reasoning", system_prompt)
        self.assertIn('"who"', system_prompt)

    async def test_takeover_returns_reasoning_and_logs_it(self):
        self.llm.replies = [
            '{"reasoning":{"env":"书房","state":"刚醒","mood":"困倦",'
            '"who":"小明问我周末安排","intent":"敷衍两句"},'
            '"actions":[{"type":"say","messages":["周末啊……还没想好"]}]}'
        ]
        ctx = self.ctx(text="周末干嘛去")
        await self.engine.handle_incoming(ctx)
        outcome = await self.engine.handle_reply(ctx)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.reasoning["who"], "小明问我周末安排")
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        reply_events = [item for item in events if item["event_type"] == "reply"]
        self.assertTrue(reply_events)
        self.assertEqual(
            reply_events[0]["detail"]["reasoning"]["env"], "书房"
        )
        # reasoning 不应该被写进记忆
        memories = self.engine.memory.db.query_memories(session_id=SESSION, limit=50)
        self.assertFalse(any("小明问我周末安排" in row["content"] for row in memories))

    async def test_reasoning_is_kept_for_the_editor(self):
        """推理草稿除了进日志，还要留在 state 里，编辑器「实时状态」才有东西可看。"""

        self.llm.replies = [
            '{"reasoning":{"env":"书房","state":"刚醒","mood":"困倦",'
            '"who":"小明","intent":"敷衍两句"},'
            '"actions":[{"type":"say","messages":["嗯……"]}]}'
        ]
        ctx = self.ctx(text="在吗")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)

        snapshot = await self.engine.snapshot(SESSION)
        reasoning = snapshot["last_reasoning"]
        self.assertEqual(reasoning["env"], "书房")
        self.assertEqual(reasoning["intent"], "敷衍两句")
        self.assertEqual(reasoning["_source"], "reply")

    # ---------------- 对话记忆：攒片段再总结 ----------------

    async def test_dialogue_is_not_stored_message_by_message(self):
        """连着聊几句只攒着，一条记忆都不落。"""

        for text in ("在吗", "我今天加班到现在才下班", "好累啊"):
            await self.engine.handle_incoming(self.ctx(text=text))
        state = await self.get_state()
        self.assertEqual(len(state.pending_memory), 3)
        self.assertEqual(self.interaction_rows(), [])

    async def test_summary_trigger_messages_writes_one_memory(self):
        """攒够条数触发一次总结：一段对话只留一条。"""

        raw = self.store.raw_world()
        raw["memory"] = {"summary_trigger_messages": 3}
        self.store.save_world(raw)
        self.engine.reload_config()
        self.llm.replies = ["小明说他加班到很晚，我有点心疼"]

        for text in ("在吗", "我今天加班到现在才下班", "好累啊"):
            await self.engine.handle_incoming(self.ctx(text=text))
        await self.engine.flush_pending_memory(SESSION)

        rows = self.interaction_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["content"], "小明说他加班到很晚，我有点心疼")
        self.assertIn("42", rows[0]["related_users"])
        self.assertEqual(rows[0]["node_id"], self.node_id)
        state = await self.get_state()
        self.assertEqual(state.pending_memory, [])

    async def test_model_memory_field_is_only_a_hint(self):
        """模型顺手给的 memory 只当写记忆时的提示，不单独落成一条。"""

        self.llm.replies = [
            '{"reasoning":{"intent":"安慰一下"},'
            '"memory":"小明说他加班很累，我有点心疼",'
            '"actions":[{"type":"say","messages":["辛苦啦"]}]}',
            "小明说他加班到很晚，我有点心疼",
        ]
        ctx = self.ctx(text="我今天加班到现在才下班，好累")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        self.assertEqual(self.interaction_rows(), [])
        self.assertEqual((await self.get_state()).memory_hints, ["小明说他加班很累，我有点心疼"])

        self.clock.advance(31 * 60)
        await self.engine.flush_pending_memory(SESSION)
        rows = self.interaction_rows()
        self.assertEqual(len(rows), 1, [row["content"] for row in rows])
        self.assertEqual(rows[0]["content"], "小明说他加班到很晚，我有点心疼")
        # 她自己说过的话也在同一段材料里（总结时能看到）
        self.assertIn("辛苦啦", self.llm.calls[-1]["prompt"])

    async def test_moving_summarizes_the_conversation_where_it_happened(self):
        """换地点时把刚才那段总结掉，并挂在原来那个地点上。"""

        await self.set_state(node_id="bedroom")
        await self.engine.handle_incoming(self.ctx(text="我先去睡了，你也早点休息"))
        self.assertEqual(self.interaction_rows(), [])
        self.assertEqual((await self.get_state()).pending_memory_node, "bedroom")

        # 走开：这一步走完就顺手把刚才那段总结掉
        await self.set_state(
            current_action={
                "type": "walk_to",
                "target_node": "study",
                "duration_ticks": 1,
                "elapsed_ticks": 0,
                "interruptible": True,
                "arrival_decide": 0,
            }
        )
        await self.engine.tick()

        state = await self.get_state()
        self.assertEqual(state.node_id, "study")
        self.assertEqual(state.pending_memory, [])
        rows = self.interaction_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["node_id"], "bedroom")

    async def test_memory_flush_runs_on_tick(self):
        """tick 会顺手把攒着的对话总结掉（不需要额外调用）。"""

        await self.engine.handle_incoming(self.ctx(text="今天天气不错啊"))
        self.clock.advance(31 * 60)
        await self.engine.tick()
        self.assertEqual(len(self.interaction_rows()), 1)

    async def test_wake_up_and_memory_are_logged(self):
        """叫醒和写记忆都要进事件日志（日志页与调试回显都用它）。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        await self.engine.handle_incoming(self.ctx(text="醒醒，别睡了"))
        self.clock.advance(31 * 60)
        self.llm.replies = []  # 没有总结可用 -> 退化成一行摘录
        await self.engine.flush_pending_memory(SESSION)

        events = await self.db.call("query_events", session_id=SESSION, limit=50)
        kinds = [item["event_type"] for item in events]
        self.assertIn("wake_up", kinds)
        self.assertIn("memory", kinds)
        memory_event = next(item for item in events if item["event_type"] == "memory")
        self.assertIn("醒醒", memory_event["detail"]["content"])

    async def test_summary_falls_back_to_an_excerpt_without_a_model(self):
        """模型没给出总结时，退化成一行摘录，而不是什么都不记。"""

        await self.engine.handle_incoming(self.ctx(text="我今天加班到现在才下班"))
        self.engine.llm = None
        self.clock.advance(31 * 60)
        await self.engine.flush_pending_memory(SESSION)
        rows = self.interaction_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("我今天加班到现在才下班", rows[0]["content"])
        self.assertIn("小明", rows[0]["content"])

    async def test_takeover_falls_back_when_llm_unavailable(self):
        """没有可用模型/调用失败时返回 ok=False，交给调用方降级为注入。"""

        self.engine.llm = None
        outcome = await self.engine.handle_reply(self.ctx(text="在吗"))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.messages, [])

    async def test_takeover_skips_when_model_returns_no_action(self):
        self.llm.replies = ['{"reasoning":{"intent":"不想说话"},"actions":[]}']
        outcome = await self.engine.handle_reply(self.ctx(text="在吗"))
        self.assertFalse(outcome.ok)

    # ---------------- 事件日志（日志页的数据来源） ----------------

    async def test_echo_actions_sends_them_as_messages(self):
        """开「把动作发到群里」后，动作与工具调用会作为普通消息发出来。"""

        raw = self.store.raw_world()
        raw["echo_actions"] = True
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_schedule(
            id="echo_search",
            time="12:00",
            action_chain=[{"type": "search_web", "params": {"query": "今天的新闻"}}],
        )
        await self.set_state(node_id="study")
        self.tools.results["web_search"] = "今天有三条科技新闻。"
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        outcomes = await self.engine.run_schedules()
        echoed = [
            text
            for text in outcomes[0].debug_messages
            if text.startswith(("🔧", "📥", "▶️", "✅", "🎬", "⏭️", "🧠"))
        ]
        self.assertTrue(echoed, outcomes[0].debug_messages)
        self.assertTrue(any("🔧" in text and "今天的新闻" in text for text in echoed), echoed)
        self.assertTrue(any("📥" in text and "科技新闻" in text for text in echoed), echoed)
        # 回显不算"她说过的话"：不进聊天上下文（她自己真正说的那句才算）
        state = await self.get_state()
        self.assertFalse(
            [item for item in state.recent_chat if "🔧" in str(item.get("text", ""))],
            state.recent_chat,
        )

    async def test_echo_actions_off_by_default(self):
        self.add_schedule(
            id="quiet_search",
            time="12:00",
            action_chain=[{"type": "search_web", "params": {"query": "x"}}],
        )
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.run_schedules()
        for _ in range(3):
            await self.engine.tick()
        prefixes = ("🔧", "📥", "▶️", "✅", "🎬", "⏭️", "🧠")
        self.assertFalse(
            [text for text in self.messenger.flat_messages if text.startswith(prefixes)],
            self.messenger.flat_messages,
        )

    async def test_echo_only_lists_the_checked_types(self):
        """只勾了「工具调用」，群里就只出现工具那一行。"""

        raw = self.store.raw_world()
        raw["echo_types"] = ["tool_call"]
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_schedule(
            id="only_tool",
            time="12:00",
            action_chain=[{"type": "search_web", "params": {"query": "今天的新闻"}}],
        )
        await self.set_state(node_id="study")
        self.tools.results["web_search"] = "今天有三条科技新闻。"
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        outcomes = await self.engine.run_schedules()

        echoed = [
            text
            for text in outcomes[0].debug_messages
            if text.startswith(("🔧", "📥", "▶️", "✅", "🎬", "⏭️", "🧠"))
        ]
        self.assertTrue(echoed, outcomes[0].debug_messages)
        self.assertTrue(all(text.startswith("🔧") for text in echoed), echoed)

    async def test_legacy_echo_switch_migrates_to_the_common_set(self):
        """老配置里的 echo_actions: true 会变成「常用」那几类。"""

        raw = self.store.raw_world()
        raw["echo_actions"] = True
        self.store.save_world(raw)
        self.engine.reload_config()
        self.assertEqual(
            self.engine.world.echo_types,
            [
                "plan",
                "action_start",
                "action_done",
                "action",
                "tool_call",
                "tool_result",
                "skip",
            ],
        )
        self.assertEqual(self.engine.echo_types(), set(self.engine.world.echo_types))

    async def test_unknown_echo_types_are_ignored(self):
        raw = self.store.raw_world()
        raw["echo_types"] = ["tool", "不存在的类型"]
        self.store.save_world(raw)
        self.engine.reload_config()
        # 老写法「工具」拆成了调用 / 返回两条，两个都留着
        self.assertEqual(self.engine.echo_types(), {"tool_call", "tool_result"})

    async def test_echo_covers_events_that_happen_before_the_reply(self):
        """睡觉门禁、被叫醒发生在"要不要回复"之前，回显也要能带出去。"""

        raw = self.store.raw_world()
        raw["echo_types"] = ["sleep_reply", "wake_up", "memory"]
        self.store.save_world(raw)
        self.engine.reload_config()
        await self.set_state(state="sleeping", current_action={"type": "sleep"})

        gate = await self.engine.sleep_gate(self.ctx(text="在吗"))
        self.assertEqual(gate.mode, "template")
        first = self.engine.take_pending_echo(SESSION)
        self.assertTrue(any("😴" in line for line in first), first)
        # 取走之后不会重复发
        self.assertEqual(self.engine.take_pending_echo(SESSION), [])

        await self.engine.handle_incoming(self.ctx(text="醒醒，别睡了"))
        second = self.engine.take_pending_echo(SESSION)
        self.assertTrue(any("🌅" in line for line in second), second)

    async def test_echo_does_not_repeat_what_was_already_sent(self):
        """她自己说过的话本来就会发出去，调试回显不再重复一遍。"""

        raw = self.store.raw_world()
        raw["echo_actions"] = True
        self.store.save_world(raw)
        self.engine.reload_config()
        self.llm.replies = ['{"actions":[{"type":"say","messages":["我在这儿呢"]}]}']

        ctx = self.ctx(text="在吗")
        await self.engine.handle_incoming(ctx)
        outcome = await self.engine.handle_reply(ctx)

        self.assertEqual(outcome.messages, ["我在这儿呢"])
        self.assertEqual(outcome.debug_messages, [])

    async def test_echo_still_shows_what_the_group_cannot_see(self):
        """内心活动、动作开始/完成这些本来不发出去的东西照旧回显。"""

        raw = self.store.raw_world()
        raw["echo_actions"] = True
        self.store.save_world(raw)
        self.engine.reload_config()
        self.llm.replies = [
            '{"actions":[{"type":"think","content":"他今天好像很累"}]}',
            '{"actions":[{"type":"think","content":"算了，让他歇会儿"}]}',
        ]

        ctx = self.ctx(text="我好累")
        await self.engine.handle_incoming(ctx)
        outcome = await self.engine.handle_reply(ctx)

        echoed = " ".join(outcome.debug_messages)
        self.assertIn("🎬", echoed)
        self.assertIn("他今天好像很累", echoed)

    async def test_remote_action_auto_travels(self):
        """她想去别处做某件事、自己没写移动时，插件带她过去再执行（A2 兜底）。"""

        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        await self.set_state(node_id="kitchen")  # 厨房做不了 search_web（它属于书房）
        self.llm.replies = [
            '{"reasoning":{"intent":"去查今天的新闻"},'
            '"actions":[{"type":"search_web","intent":"今天有什么新闻"},'
            '{"type":"say","messages":["我这就去查"]}]}'
        ]
        ctx = self.ctx(text="帮我搜今天的新闻")
        await self.engine.handle_incoming(ctx)
        result = await self.engine.handle_reply(ctx)

        state = await self.get_state()
        self.assertEqual((state.current_action or {}).get("type"), "walk_to")
        self.assertEqual((state.current_action or {}).get("target_node"), "study")
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        replies = [item for item in events if item["event_type"] == "reply"]
        self.assertTrue(replies)
        self.assertEqual(replies[0]["detail"]["auto_travel"], ["study"])
        self.assertIn("search_web", replies[0]["detail"]["actions"])
        self.assertIn("raw", replies[0]["detail"])
        self.assertEqual(result.ok, True)

    async def test_reply_event_keeps_model_raw_and_warnings(self):
        """日志要能看出模型原话、以及哪些动作被丢掉了。"""

        self.llm.replies = [
            '{"reasoning":{"intent":"随便说点什么"},'
            '"actions":[{"type":"fly"},{"type":"say","messages":["好困"]}]}'
        ]
        ctx = self.ctx(text="你在干嘛")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        replies = [item for item in events if item["event_type"] == "reply"]
        self.assertTrue(replies)
        detail = replies[0]["detail"]
        self.assertIn("fly", detail["raw"])
        self.assertTrue(any("fly" in item for item in detail["warnings"]), detail["warnings"])

    async def test_plan_event_contains_steps_and_reason(self):
        await self.set_state(loneliness=0.95, node_id="bedroom")
        await self.engine.maybe_decide(SESSION)
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        plans = [item for item in events if item["event_type"] == "plan"]
        self.assertTrue(plans)
        detail = plans[0]["detail"]
        self.assertEqual(detail["source"], "rule")
        self.assertTrue(detail["steps"])
        self.assertEqual(detail["steps"][0]["action"], "walk_to")

    async def test_action_event_contains_what_she_said(self):
        """互动类动作（模板）也要在日志里留下"她发了什么"。"""

        self.set_schedules(
            [
                {
                    "id": "hug_log",
                    "enabled": True,
                    "time": "17:30",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": [{"type": "hug", "target": "42"}],
                    "conditions": {},
                    "priority": 9,
                }
            ]
        )
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 17, 30))
        await self.engine.tick()
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        actions = [item for item in events if item["event_type"] == "action"]
        self.assertTrue(actions)
        self.assertEqual(actions[0]["detail"]["type"], "hug")
        self.assertIn("抱了你一下", " ".join(actions[0]["detail"]["messages"]))

    async def test_skipped_action_is_logged(self):
        self.engine.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.set_schedules(
            [
                {
                    "id": "search_log",
                    "enabled": True,
                    "time": "19:00",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": [{"type": "search_web"}],
                    "conditions": {},
                    "priority": 9,
                }
            ]
        )
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 19, 0))
        await self.engine.tick()
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        skips = [item for item in events if item["event_type"] == "skip"]
        self.assertTrue(skips)
        self.assertIn("缺少必填参数", skips[0]["detail"]["note"])

    async def test_takeover_uses_history_contexts(self):
        """历史对话会作为 contexts 传给大模型，不会让她失忆。"""

        history = [
            {"role": "user", "content": "我们刚才说到哪了"},
            {"role": "assistant", "content": "说到周末计划"},
        ]
        await self.engine.handle_reply(self.ctx(text="继续"), history=history)
        self.assertEqual(self.llm.calls[-1]["contexts"], history)

    # ---------------- 编辑器左栏（时间与日程 / 正在进行 / 互动与运行） ----------------

    async def tick_until(self, done, limit: int = 6) -> None:
        """连着推几个 tick，直到条件满足（持续动作要跑完才会触发后续步骤）。"""

        for _ in range(limit):
            await self.engine.tick()
            if done():
                return

    def add_schedule(self, **overrides) -> None:
        item = {
            "id": "morning_news",
            "enabled": True,
            "time": "07:30",
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "action_chain": [{"type": "say", "messages": ["看新闻了"]}],
            "conditions": {},
            "priority": 5,
        }
        item.update(overrides)
        raw = self.store.raw_schedules()
        raw["schedules"] = [item]
        self.store.save_schedules(raw)
        self.engine.reload_config()

    async def test_clock_text_matches_prompt_wording(self):
        self.clock.set_struct(datetime(2026, 9, 15, 14, 3))
        snapshot = await self.engine.snapshot(SESSION)
        self.assertEqual(snapshot["clock_text"], "2026-09-15（周二）14:03 —— 下午")
        await self.set_state(node_id="study")
        injection = await self.engine.preview_injection(SESSION)
        self.assertIn(f"现在是：{snapshot['clock_text']}", injection)

    async def test_world_time_is_tick_count_times_tick_length(self):
        await self.set_state(world_time=2455)
        snapshot = await self.engine.snapshot(SESSION)
        self.assertEqual(snapshot["world_elapsed_seconds"], 2455 * 60)
        self.assertEqual(snapshot["world_elapsed_text"], "1 天 16 小时")

    async def test_next_schedule_counts_down_to_the_coming_one(self):
        self.add_schedule(time="07:30")
        self.clock.set_struct(datetime(2026, 9, 15, 7, 5))
        snapshot = await self.engine.snapshot(SESSION)
        next_item = snapshot["next_schedule"]
        self.assertIsNotNone(next_item)
        assert next_item is not None
        self.assertEqual(next_item["in_minutes"], 25)
        self.assertEqual(next_item["weekday"], "周二")
        self.assertEqual(next_item["actions"], "说话")

    async def test_next_schedule_skips_disabled_and_next_week(self):
        self.add_schedule(time="07:30", days=["mon"])
        self.clock.set_struct(datetime(2026, 9, 15, 9, 0))  # 周二，本周一已经过了
        snapshot = await self.engine.snapshot(SESSION)
        next_item = snapshot["next_schedule"]
        assert next_item is not None
        # 下周一 07:30
        self.assertEqual(next_item["weekday"], "周一")
        self.assertEqual(next_item["in_minutes"], 6 * 24 * 60 - 90)

        self.add_schedule(time="07:30", enabled=False)
        snapshot = await self.engine.snapshot(SESSION)
        self.assertIsNone(snapshot["next_schedule"])

    async def test_budget_reports_remaining_quotas(self):
        await self.set_state(world_time=120)
        hour = self.engine._hour_index(await self.get_state())
        await self.set_state(llm_plan_hour_marker=hour, llm_plan_count_hour=2)
        snapshot = await self.engine.snapshot(SESSION)
        plan = snapshot["budget"]["plan"]
        limit = self.engine.world.limits.max_llm_plan_per_hour
        self.assertEqual(plan["limit"], limit)
        self.assertEqual(plan["used"], 2)
        self.assertEqual(plan["left"], max(0, limit - 2))

    async def test_budget_resets_when_the_hour_changes(self):
        await self.set_state(world_time=120)
        hour = self.engine._hour_index(await self.get_state())
        await self.set_state(llm_plan_hour_marker=hour, llm_plan_count_hour=99)
        snapshot = await self.engine.snapshot(SESSION)
        self.assertEqual(snapshot["budget"]["plan"]["left"], 0)

        # 世界时间跨到下一个小时：计数自动归零，额度回到满格
        await self.set_state(world_time=180)
        snapshot = await self.engine.snapshot(SESSION)
        self.assertEqual(
            snapshot["budget"]["plan"]["left"],
            self.engine.world.limits.max_llm_plan_per_hour,
        )

    async def test_channel_status_tracks_last_llm_call(self):
        snapshot = await self.engine.snapshot(SESSION)
        self.assertEqual(snapshot["channel"]["send_blocked_seconds"], 0)
        self.assertEqual(snapshot["channel"]["last_llm"], {})

        await self.engine.handle_reply(self.ctx())
        self.assertTrue(self.llm.calls)
        snapshot = await self.engine.snapshot(SESSION)
        last = snapshot["channel"]["last_llm"]
        self.assertTrue(last["ok"])
        self.assertEqual(last["error"], "")
        self.assertGreaterEqual(last["ago_seconds"], 0)

    async def test_channel_status_shows_llm_failure(self):
        self.llm.default_reply = ""
        self.llm.replies = []

        async def boom(**_kwargs):
            raise RuntimeError("连接超时")

        self.llm.generate = boom
        await self.engine.handle_reply(self.ctx())
        snapshot = await self.engine.snapshot(SESSION)
        last = snapshot["channel"]["last_llm"]
        self.assertFalse(last["ok"])
        self.assertIn("RuntimeError", last["error"])

    async def test_channel_status_reports_send_cooldown(self):
        self.messenger.blocked_seconds = lambda session_id: 42
        snapshot = await self.engine.snapshot(SESSION)
        self.assertEqual(snapshot["channel"]["send_blocked_seconds"], 42)

    # ---------------- 日程触发 ----------------

    async def test_schedule_fires_when_ticks_skipped_a_minute(self):
        """tick 被拖慢、整分钟跳过去时，下一次检查要把漏掉的那一分钟补上。"""

        self.add_schedule(
            id="news",
            time="12:00",
            action_chain=[{"type": "say", "messages": ["该看新闻了"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 10, 11, 58))
        await self.engine.tick()
        self.assertEqual(self.messenger.flat_messages, [])

        # 中间的 11:59 / 12:00 两分钟没有被检查过
        self.clock.set_struct(datetime(2026, 9, 10, 12, 2))
        await self.engine.tick()
        self.assertIn("该看新闻了", self.messenger.flat_messages)

    async def test_schedule_does_not_fire_twice_a_day(self):
        self.add_schedule(
            id="news",
            time="12:00",
            action_chain=[{"type": "say", "messages": ["该看新闻了"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.tick()
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1))
        await self.engine.tick()
        self.assertEqual(self.messenger.flat_messages.count("该看新闻了"), 1)

    async def test_schedule_with_edited_time_fires_the_same_day(self):
        """当天把触发时间改掉之后，新时间点要能正常触发（原来会被日期去重挡掉）。"""

        self.add_schedule(
            id="news",
            time="12:00",
            action_chain=[{"type": "say", "messages": ["第一次"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.tick()
        self.assertIn("第一次", self.messenger.flat_messages)

        raw = self.store.raw_schedules()
        raw["schedules"] = [
            {
                **item,
                "time": "12:30",
                "action_chain": [{"type": "say", "messages": ["改完时间之后"]}],
            }
            for item in raw["schedules"]
            if item["id"] == "news"
        ]
        self.store.save_schedules(raw)
        self.engine.reload_config()

        self.clock.set_struct(datetime(2026, 9, 10, 12, 30))
        await self.engine.tick()
        self.assertIn("改完时间之后", self.messenger.flat_messages)

    async def test_blocked_schedule_is_logged_with_reason(self):
        """日程到点却没跑时必须留下原因，否则看起来就像"改了没生效"。"""

        self.add_schedule(
            id="news",
            time="12:00",
            action_chain=[{"type": "say", "messages": ["该看新闻了"]}],
            conditions={"not_state": ["sleeping"]},
        )
        await self.set_state(state="sleeping")
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.tick()
        self.assertEqual(self.messenger.flat_messages, [])
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        skips = [item for item in events if item["event_type"] == "skip"]
        self.assertTrue(skips)
        self.assertIn("到点了却没跑", skips[0]["detail"]["note"])
        self.assertIn("sleeping", skips[0]["detail"]["note"])

    async def test_schedule_days_filter_matches_weekday(self):
        """只勾了周一：周二不触发，下周一才触发。"""

        self.add_schedule(
            id="monday_only",
            time="12:00",
            days=["mon"],
            action_chain=[{"type": "say", "messages": ["周一好"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))  # 周二
        await self.engine.tick()
        self.assertEqual(self.messenger.flat_messages, [])

        self.clock.set_struct(datetime(2026, 9, 21, 12, 0))  # 下周一
        await self.engine.tick()
        self.assertIn("周一好", self.messenger.flat_messages)

    async def test_run_schedule_now_ignores_time_and_conditions(self):
        """「立即执行」：不管几点、条件满不满足，都立刻跑一遍。"""

        self.add_schedule(
            id="manual",
            time="23:30",
            action_chain=[{"type": "say", "messages": ["手动跑的"]}],
            conditions={"not_state": ["sleeping"]},
        )
        await self.set_state(state="sleeping")
        self.clock.set_struct(datetime(2026, 9, 15, 9, 0))  # 离 23:30 还早
        result = await self.engine.run_schedule_now(SESSION, "manual")
        self.assertTrue(result["ok"], result)
        self.assertIn("手动跑的", self.messenger.flat_messages)

    async def test_run_schedule_now_does_not_use_up_the_real_trigger(self):
        """手动跑一次之后，到点还得照常触发一次。"""

        self.add_schedule(
            id="manual",
            time="12:00",
            action_chain=[{"type": "say", "messages": ["到点这句"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 9, 0))
        await self.engine.run_schedule_now(SESSION, "manual")
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        # 手动那次 1 条 + 到点正常触发 1 条
        self.assertEqual(self.messenger.flat_messages.count("到点这句"), 2)

    async def test_run_schedule_now_reports_unknown_or_blocked(self):
        self.add_schedule(
            id="manual",
            time="12:00",
            action_chain=[{"type": "say", "messages": ["x"]}],
            conditions={"not_state": ["sleeping"]},
        )
        missing = await self.engine.run_schedule_now(SESSION, "nope")
        self.assertFalse(missing["ok"])
        self.assertIn("没有找到", missing["reason"])

        await self.set_state(state="sleeping")
        blocked = await self.engine.run_schedule_now(SESSION, "manual", force=False)
        self.assertFalse(blocked["ok"])
        self.assertIn("条件不满足", blocked["reason"])

    async def test_run_schedule_now_expands_auto_travel(self):
        """开了「自动先走过去」的日程，手动执行也会先补一步移动。"""

        self.add_schedule(
            id="news",
            time="12:00",
            auto_travel=True,
            action_chain=[{"type": "search_web", "intent": "查新闻"}],
        )
        self.tools.results["web_search"] = "今天没什么新闻"
        await self.set_state(node_id="bedroom")
        await self.engine.run_schedule_now(SESSION, "news")
        state = await self.get_state()
        self.assertEqual(state.current_action["type"], "walk_to")
        self.assertEqual(state.current_action["target_node"], "study")

    async def test_schedule_start_is_logged(self):
        """日程开始执行要写进事件日志（含时间、要跑的动作、是不是手动跑的）。"""

        self.add_schedule(
            id="news",
            time="12:00",
            action_chain=[{"type": "say", "messages": ["看新闻了"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        item = next((row for row in events if row["event_type"] == "schedule"), None)
        self.assertIsNotNone(events and item, events)
        assert item is not None
        self.assertEqual(item["detail"]["id"], "news")
        self.assertEqual(item["detail"]["time"], "12:00")
        self.assertIn("说话", item["detail"]["actions"])
        self.assertFalse(item["detail"]["manual"])

    async def test_manual_run_is_logged_as_manual(self):
        self.add_schedule(
            id="news",
            time="12:00",
            action_chain=[{"type": "say", "messages": ["手动跑的"]}],
        )
        await self.engine.run_schedule_now(SESSION, "news")
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        item = next((row for row in events if row["event_type"] == "schedule"), None)
        self.assertIsNotNone(item, events)
        assert item is not None
        self.assertTrue(item["detail"]["manual"])

    async def test_schedule_start_can_be_echoed_to_the_group(self):
        """勾了「日程开始执行」这一档，就会把这一行发到群里。"""

        raw = self.store.raw_world()
        raw["echo_types"] = ["schedule"]
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_schedule(
            id="news",
            time="12:00",
            action_chain=[{"type": "say", "messages": ["看新闻了"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertTrue(
            any("news" in line and "📅" in line for line in self.messenger.flat_messages),
            self.messenger.flat_messages,
        )

    async def test_single_action_without_lines_asks_the_model(self):
        """日程里的「单轮」动作没有现成台词时，让大模型按动作语义现写一句。"""

        self.add_schedule(id="sing_now", time="12:00", action_chain=[{"type": "sing"}])
        self.llm.replies = ['{"actions":[{"type":"say","messages":["哼两句~"]}]}']
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertIn("哼两句~", self.messenger.flat_messages)

    async def test_single_action_falls_back_when_no_model_is_left(self):
        """没配模型（或发言额度用完）时，退化成一句动作文案，不至于什么都不发。"""

        raw = self.store.raw_world()
        raw["bot_name"] = "小鲸鱼"
        self.store.save_world(raw)
        self.engine.reload_config()
        self.engine.llm = None
        self.addCleanup(setattr, self.engine, "llm", self.llm)
        self.add_schedule(id="sing_now", time="12:00", action_chain=[{"type": "sing"}])
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertTrue(
            any("哼歌" in line for line in self.messenger.flat_messages),
            self.messenger.flat_messages,
        )

    async def test_group_single_action_speaks_even_when_flagged_quiet(self):
        """「单轮」动作就算「会发到群里」没开，它生成的话也要发出去。

        这个开关在编辑器里藏在「高级」里，新建动作时默认是关的；用户建一个
        「问候早安」指望她说句话，结果只进了日志、群里什么都没有。
        """

        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "good_morning",
                "name": "问候早安",
                "category": "instant",
                "llm_level": "single",
                "scope": "global",
                "target_type": "group",
                "visible": False,
                "description": "跟大家打个招呼",
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_schedule(
            id="greet", time="12:00", action_chain=[{"type": "good_morning"}]
        )
        self.llm.replies = ['{"actions":[{"type":"say","messages":["早～"]}]}']
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertIn("早～", self.messenger.flat_messages)

    async def test_inner_action_still_stays_quiet(self):
        """「想事情」这类内心活动不受上一条影响，仍然不发到群里。"""

        self.add_schedule(
            id="think_now",
            time="12:00",
            action_chain=[{"type": "think", "content": "有点困了"}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertNotIn("有点困了", self.messenger.flat_messages)
        state = await self.get_state()
        self.assertTrue(any(item.get("content") == "有点困了" for item in state.thoughts))

    async def test_template_action_speaks_even_without_the_visible_flag(self):
        """模板写了文案就要发——「动作会发到群里」那个开关藏得深，别再让它吞掉文案。"""

        raw = self.store.raw_world()
        raw["bot_name"] = "小鲸鱼"
        raw["actions"].append(
            {
                "id": "wave_hi",
                "name": "招手",
                "category": "instant",
                "llm_level": "template",
                "scope": "global",
                "target_type": "group",
                "visible": False,
                "template": "（{bot}朝群里招了招手）",
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_schedule(id="hi", time="12:00", action_chain=[{"type": "wave_hi"}])
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertIn("（小鲸鱼朝群里招了招手）", self.messenger.flat_messages)

    async def test_empty_template_action_stays_silent(self):
        """模板是空的、又没别的话可说（移动、发呆这类），依旧什么都不发。"""

        self.add_schedule(
            id="move",
            time="12:00",
            action_chain=[{"type": "walk_to", "target_node": "lobby"}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertEqual(self.messenger.flat_messages, [])

    # ---------------- 日程里的工具 / 指令步骤：意图从哪来 ----------------

    def prepare_tool_schedule(self, **step_overrides) -> None:
        """搭一条「上网搜索」的日程，并把搜索工具的 schema / 返回准备好。"""

        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.results["web_search"] = "今天有三条科技新闻"
        step = {"type": "search_web"}
        step.update(step_overrides)
        self.add_schedule(id="news", time="12:00", action_chain=[step])

    async def test_schedule_step_intent_drives_the_tool_params(self):
        """动作链里写了「意图」，补参模型就按它去填参数。"""

        self.prepare_tool_schedule(intent="查今天的科技新闻")
        self.llm.replies = ['{"query": "今天的科技新闻"}', SAY_REPLY]
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.tick_until(lambda: bool(self.tools.calls))
        self.assertIn(("web_search", {"query": "今天的科技新闻"}), self.tools.calls)

    async def test_schedule_step_without_intent_still_runs(self):
        """没写意图不再直接跳过：用动作自己的说明兜底，照样把参数补出来。"""

        self.prepare_tool_schedule()
        self.llm.replies = ['{"query": "今天的新闻"}', SAY_REPLY]
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.tick_until(lambda: bool(self.tools.calls))
        self.assertTrue(
            any(name == "web_search" for name, _params in self.tools.calls),
            self.tools.calls,
        )
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        self.assertFalse(
            any(
                item["event_type"] == "skip"
                and "没有给出想做什么" in str(item["detail"])
                for item in events
            ),
            events,
        )

    async def test_saving_schedules_fills_missing_intents(self):
        """保存日程时，缺意图的工具 / 指令步骤由内容生成模型补一句写进配置。"""

        self.tools.schemas["web_search"] = {"type": "object", "properties": {}}
        self.llm.replies = ["看看今天有什么科技新闻"]
        payload = {
            "schedules": [
                {
                    "id": "news",
                    "time": "07:30",
                    "action_chain": [{"type": "search_web"}],
                }
            ]
        }
        data, filled = await self.engine.fill_schedule_intents(payload)
        self.assertEqual(
            data["schedules"][0]["action_chain"][0]["intent"], "看看今天有什么科技新闻"
        )
        self.assertTrue(filled)
        self.assertIn("news", filled[0])

        # 已经有意图的步骤不会被动、智能日程也不需要意图
        self.llm.replies = ["不该被用到"]
        payload["schedules"][0]["action_chain"][0]["intent"] = "已经写好了"
        payload["schedules"].append(
            {
                "id": "smart_one",
                "smart": True,
                "action_chain": [{"type": "search_web"}],
            }
        )
        data, filled = await self.engine.fill_schedule_intents(payload)
        self.assertEqual(data["schedules"][0]["action_chain"][0]["intent"], "已经写好了")
        self.assertEqual(data["schedules"][1]["action_chain"][0].get("intent", ""), "")
        self.assertEqual(filled, [])

    # ---------------- 智能日程 ----------------

    async def test_smart_schedule_asks_the_model_for_a_plan(self):
        """智能日程：只让大模型补意图，动作链本身一个字都不改。"""

        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.schemas["search_news"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools._tools = {"web_search": "搜索网页", "search_news": "热搜新闻"}
        self.tools.results["search_news"] = "热搜第一：某某某"
        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "search_news",
                "name": "上网查热搜新闻",
                "category": "continuous",
                "llm_level": "tool",
                "scope": "global",
                "tool_names": ["search_news"],
                "duration": 30,
                "description": "查一下当前的热搜新闻。",
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_schedule(
            id="news",
            time="12:00",
            smart=True,
            action_chain=[{"type": "search_news"}],
        )
        self.llm.replies = [
            '{"intents":[{"step":1,"intent":"看看今天有什么热搜新闻"}]}',
            '{"query": "今天的热搜"}',
            SAY_REPLY,
        ]
        await self.set_state(node_id="study", chat_note="大家在聊周末去哪玩")
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.tick_until(lambda: bool(self.tools.calls))
        # 只补意图：跑的必须还是日程里配的那个动作
        self.assertEqual(self.tools.calls[0][0], "search_news")
        self.assertNotIn("web_search", [name for name, _p in self.tools.calls])
        # 补意图那次调用：给了这一步的说明和一句话题背景
        intent_call = next(
            call for call in self.llm.calls if "要补的意图" in call["prompt"]
        )
        intent_prompt = intent_call["prompt"]
        self.assertIn("上网查热搜新闻", intent_prompt)
        self.assertIn("大家在聊周末去哪玩", intent_prompt)
        # 带着人设：这句意图要符合她的身份
        self.assertIn("温柔黏人的少女", intent_call["system_prompt"])
        # 不给她平时那一整套提示词
        for noise in ("你可以去的地方", "通用工具", "这里让你想起", "最近发生的事"):
            self.assertNotIn(noise, intent_prompt)
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        schedule_events = [
            item for item in events if item["event_type"] == "schedule"
        ]
        self.assertTrue(schedule_events, events)
        self.assertTrue(schedule_events[0]["detail"]["smart"])
        self.assertIn("热搜新闻", schedule_events[0]["detail"]["intents"])

    async def test_smart_schedule_falls_back_when_the_model_gives_nothing(self):
        """智能日程补不出意图时，照样按动作链执行，不能什么都不做。"""

        self.add_schedule(
            id="news",
            time="12:00",
            smart=True,
            action_chain=[{"type": "say", "messages": ["还是按原计划说一句"]}],
        )
        self.llm.replies = []
        self.llm.default_reply = ""
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertIn("还是按原计划说一句", self.messenger.flat_messages)

    async def test_smart_schedule_without_tool_steps_skips_the_call(self):
        """链里没有工具 / 指令型步骤时，没必要为了补意图多调一次模型。"""

        self.add_schedule(
            id="greet",
            time="12:00",
            smart=True,
            action_chain=[{"type": "say", "messages": ["早呀"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertIn("早呀", self.messenger.flat_messages)
        self.assertEqual(self.llm.calls, [])

    async def test_smart_intent_parser_drops_bad_answers(self):
        """模型乱写（下标越界 / 空话 / 不是 JSON）时一律丢掉。"""

        parse = self.engine._parse_intents
        self.assertEqual(
            parse('{"intents":[{"step":1,"intent":"看看新闻"}]}', {1, 2}, 2),
            {1: "看看新闻"},
        )
        self.assertEqual(
            parse('{"intents":[{"step":9,"intent":"越界"},{"step":2,"intent":"  "}]}', {1, 2}, 2),
            {},
        )
        self.assertEqual(parse("这不是 JSON", {1}, 1), {})
        self.assertEqual(parse('{"intents":["按顺序的第一句"]}', {1}, 1), {1: "按顺序的第一句"})

    async def test_snapshot_reports_zone_and_progress(self):
        self.add_schedule(time="07:30")
        self.clock.set_struct(datetime(2026, 9, 15, 7, 0))
        async with self.engine.session_state(SESSION) as state:
            state.current_action = {
                "type": "sleep",
                "desc": "睡觉",
                "duration_ticks": 480,
                "elapsed_ticks": 120,
                "interruptible": False,
            }
        snapshot = await self.engine.snapshot(SESSION)
        self.assertEqual(snapshot["node_id"], "study")
        self.assertEqual(snapshot["zone_name"], self.engine.world.zone_map()["home"].name)
        self.assertEqual(snapshot["current_action"]["elapsed_ticks"], 120)

    # ---------------- 调试回显不能变成"刷屏" ----------------

    async def test_restart_does_not_replay_old_echo_events(self):
        """插件重载后，不能把历史日志当成新事件往群里重放。"""

        raw = self.store.raw_world()
        raw["echo_types"] = ["tool"]
        self.store.save_world(raw)
        self.engine.reload_config()
        # 模拟"上一次运行"留下的一条工具失败日志
        await self.db.call(
            "add_event",
            session_id=SESSION,
            event_type="tool",
            detail={"action": "check_weather", "tool": "get_current_weather", "ok": False, "error": "旧报错"},
        )
        # 重载 = 内存里的游标清空（_event_ids 是进程内的）
        self.engine._event_ids.clear()
        await self.engine.tick()
        self.assertEqual(self.messenger.flat_messages, [])

    async def test_new_events_are_still_echoed_after_restart(self):
        """但重载之后新发生的事，该回显还是要回显。"""

        raw = self.store.raw_world()
        raw["echo_types"] = ["tool_result"]
        self.store.save_world(raw)
        self.engine.reload_config()
        self.engine._event_ids.clear()
        await self.engine.tick()  # 第一次 tick 用来对齐游标
        await self.db.call(
            "add_event",
            session_id=SESSION,
            event_type="tool_result",
            detail={"action": "check_weather", "tool": "get_current_weather", "ok": True, "result": "武汉 晴"},
        )
        await self.engine.tick()
        self.assertTrue(
            any("武汉 晴" in line for line in self.messenger.flat_messages),
            self.messenger.flat_messages,
        )
        # 同一条只发一次：没被处理掉的话，下一个 tick 还会再发一遍
        before = len(self.messenger.flat_messages)
        await self.engine.tick()
        self.assertEqual(len(self.messenger.flat_messages), before)

    # ---------------- 戳一戳 ----------------

    def use_action_chain(self, chain: list[dict]) -> None:
        """把一条日程固定到 17:00，方便直接驱动某个动作。"""

        self.set_schedules(
            [
                {
                    "id": "drive_now",
                    "enabled": True,
                    "time": "17:00",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": chain,
                    "conditions": {},
                    "priority": 9,
                }
            ]
        )
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 17, 0))

    async def test_poke_action_pokes_the_target_without_talking(self):
        raw = self.store.raw_world()
        raw["bot_name"] = "小鲸鱼"
        self.store.save_world(raw)
        self.use_action_chain([{"type": "poke", "target": "42"}])
        await self.engine.tick()
        self.assertEqual(self.messenger.pokes, [(SESSION, "42")])
        self.assertEqual(self.messenger.flat_messages, [])

    async def test_poke_falls_back_to_text_when_platform_refuses(self):
        """协议端不给戳（非 QQ 系）时不能凭空消失，要退化成一句动作文案。"""

        raw = self.store.raw_world()
        raw["bot_name"] = "小鲸鱼"
        self.store.save_world(raw)
        self.messenger.poke_result = False
        self.use_action_chain([{"type": "poke", "target": "42"}])
        await self.engine.tick()
        self.assertIn("（小鲸鱼戳了戳你）", self.messenger.flat_messages)

    async def test_poke_without_target_uses_recent_speaker(self):
        await self.engine.note_presence(
            self.ctx(text="在吗在吗", user_id="77", user_name="小红")
        )
        self.use_action_chain([{"type": "poke"}])
        await self.engine.tick()
        self.assertEqual(self.messenger.pokes[-1], (SESSION, "77"))

    async def test_poke_is_a_builtin_action(self):
        world, _warnings = parse_world(self.store.raw_world())
        self.assertIn("poke", world.action_map())
        self.assertTrue(world.action_map()["poke"].builtin)

    # ---------------- 是不是在对她说 ----------------

    async def test_group_chatter_is_treated_as_interjection(self):
        """群里随便一句话（没 @ 她、也不是接她的话）不能当成对她的请求。"""

        await self.engine.handle_incoming(self.ctx(text="你们中午吃什么"))
        state = await self.get_state()
        mode = self.engine.reply_addressing(
            state,
            self.ctx(
                text="你们中午吃什么",
                user_id="9",
                user_name="路人",
                is_mentioned=False,
                is_wake=False,
            ),
        )
        self.assertEqual(mode, "interject")

    async def test_mention_or_name_is_direct(self):
        state = await self.get_state()
        self.assertEqual(
            self.engine.reply_addressing(state, self.ctx(text="在吗")), "direct"
        )
        raw = self.store.raw_world()
        raw["bot_name"] = "小鲸鱼"
        self.store.save_world(raw)
        self.engine.reload_config()
        state = await self.get_state()
        mode = self.engine.reply_addressing(
            state,
            self.ctx(
                text="小鲸鱼今天心情怎么样",
                is_mentioned=False,
                is_wake=False,
            ),
        )
        self.assertEqual(mode, "direct")

    async def test_reply_to_her_own_last_line_is_direct(self):
        """她刚说完话，有人顺着接一句 —— 这算在跟她对话。"""

        await self.engine.handle_incoming(self.ctx(text="你在干嘛"))
        async with self.engine.session_state(SESSION) as state:
            state.note_chat(
                user_id="__self__",
                name="小鲸鱼",
                text="在看书呢",
                now=self.engine._now(),
                is_self=True,
            )
        state = await self.get_state()
        mode = self.engine.reply_addressing(
            state,
            self.ctx(text="看什么书", is_mentioned=False, user_id="42", user_name="小明"),
        )
        self.assertEqual(mode, "direct")

    async def test_interjection_prompt_does_not_claim_it_is_for_her(self):
        await self.engine.handle_incoming(self.ctx(text="你们中午吃什么"))
        state = await self.get_state()
        system = self.engine.prompts.build_autonomous_system_prompt(
            persona_text="你是温柔少女",
            state=state,
            node=self.engine.node(state.node_id),
            available_tools={},
            recent_chat=self.engine.chat_context(state),
        )
        prompt = self.engine.prompts.build_reply_user_prompt(
            user_name="路人",
            text="你们中午吃什么",
            addressing="interject",
        )
        self.assertIn("没有人点名找你", prompt)
        self.assertNotIn("对你说", prompt)
        self.assertIn("群里刚才的对话", system)

    async def test_speech_density_hint_appears_after_talking_a_lot(self):
        state = await self.get_state()
        async with self.engine.session_state(SESSION) as state:
            for index in range(5):
                state.note_chat(
                    user_id="__self__",
                    name="小鲸鱼",
                    text=f"第{index}句",
                    now=self.engine._now(),
                    is_self=True,
                )
        state = await self.get_state()
        hint = self.engine.speech_density_hint(state)
        self.assertIn("说话密度", hint)
        self.assertIn("少说话", hint)

    async def test_speech_density_hint_is_quiet_when_she_barely_talked(self):
        state = await self.get_state()
        self.assertEqual(self.engine.speech_density_hint(state), "")


if __name__ == "__main__":
    unittest.main()
