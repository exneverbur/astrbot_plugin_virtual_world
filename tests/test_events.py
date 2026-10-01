"""事件系统：能力值、掷骰判定、事件包、线索、干涉。

分两层测：

- 纯逻辑（``core/events.py``）：不碰 IO、不调模型，边界情况全在这里；
- 引擎接线：假模型 + 假时钟，把「她抉择 → 判定 → 结算」和「求助 → 等回应 → 自己收尾」跑通。
"""

from __future__ import annotations

import asyncio
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
from core.defaults import default_world  # noqa: E402
from core.models import parse_world  # noqa: E402
from core.engine import (  # noqa: E402
    EVENT_ROLL_MAX_GAP_HOURS,
    MessageContext,
    TickOutcome,
    VirtualWorldEngine,
)
from core.events import (  # noqa: E402
    ABILITY_DAY_LIMIT,
    ABILITY_STEP_LIMIT,
    HINT_OK,
    HINT_SURE,
    TIER_FAIL,
    TIER_GREAT,
    TIER_NARROW,
    TIER_SKIP,
    TIER_SUCCESS,
    append_step,
    clamp_ability_delta,
    default_decision,
    event_from_payload,
    new_thread,
    parse_decision,
    parse_event_package,
    parse_settlement,
    parse_suggestions,
    roll_check,
    situational_modifiers,
    subjective_hint,
    surprise,
    thread_digest,
    thread_exhausted,
    tier_for,
)
from core.ports import LLMReply  # noqa: E402
from core.state import chat_item_is_fresh  # noqa: E402
from tests.stub_ports import (  # noqa: E402
    StubClock,
    StubMessenger,
    StubPersona,
    StubTools,
)

SESSION = "aiocqhttp:GroupMessage:1001"
SAY_REPLY = '{"actions":[{"type":"say","messages":["哦。"]}]}'


class ScriptLLM:
    """按脚本返回内容的模型替身（主模型 / 打杂 / 生成器各一个）。"""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict] = []
        self.default_reply = ""
        self.ok = True
        self.on_call = None
        """可选的钩子：调模型时先跑一下（用来模拟"调用期间又来了新消息"）。"""

    async def generate(self, **kwargs) -> LLMReply:
        self.calls.append(kwargs)
        hook, self.on_call = self.on_call, None
        if callable(hook):
            result = hook(len(self.calls))
            if asyncio.iscoroutine(result):
                await result
        text = self.replies.pop(0) if self.replies else self.default_reply
        return LLMReply(text=text, ok=self.ok)


class FakeRandom:
    """固定序列的骰子：值用完了就返回 0.5。"""

    def __init__(self, values: list[float] | None = None) -> None:
        self.values = list(values or [])
        self.choice_index = 0

    def random(self) -> float:
        return self.values.pop(0) if self.values else 0.5

    def choice(self, items):
        items = list(items)
        if not items:
            raise IndexError("空候选")
        value = items[self.choice_index % len(items)]
        self.choice_index += 1
        return value


# ======================================================================
# 纯逻辑
# ======================================================================


class TestCheckMath(unittest.TestCase):
    """判定：概率夹子、修正相乘、四档分档、主观偏差。"""

    def test_tiers_by_margin(self):
        for margin, want in (
            (0.4, TIER_GREAT),
            (0.25, TIER_GREAT),
            (0.1, TIER_SUCCESS),
            (0.0, TIER_SUCCESS),
            (-0.1, TIER_NARROW),
            (-0.15, TIER_NARROW),
            (-0.2, TIER_FAIL),
        ):
            result = roll_check(
                ability="wits",
                ability_value=0.5,
                difficulty=1.0,  # 会被夹到 0.95
                roll=lambda m=margin: 0.5 - m,
            )
            # 概率是算出来的（0.5 × 0.95 = 0.475），所以按实际 margin 判档
            self.assertEqual(result.tier, tier_for(result.margin), f"margin={margin}")
        # 分档边界本身单独看
        self.assertEqual(tier_for(0.25), TIER_GREAT)
        self.assertEqual(tier_for(0.0), TIER_SUCCESS)
        self.assertEqual(tier_for(-0.15), TIER_NARROW)
        self.assertEqual(tier_for(-0.16), TIER_FAIL)

    def test_probability_is_clamped(self):
        low = roll_check(
            ability="wits", ability_value=0.01, difficulty=0.05, roll=lambda: 0.5
        )
        high = roll_check(
            ability="wits", ability_value=1.0, difficulty=0.95, roll=lambda: 0.5
        )
        self.assertGreaterEqual(low.probability, 0.05)
        self.assertLessEqual(high.probability, 0.95)

    def test_modifiers_multiply(self):
        plain = roll_check(
            ability="wits", ability_value=0.6, difficulty=0.5, roll=lambda: 0.5
        )
        helped = roll_check(
            ability="wits",
            ability_value=0.6,
            difficulty=0.5,
            modifiers=[("有建议", 1.2)],
            roll=lambda: 0.5,
        )
        self.assertAlmostEqual(
            helped.probability, min(0.95, plain.probability * 1.2), places=4
        )

    def test_line_is_readable(self):
        result = roll_check(
            ability="dexterity",
            ability_value=0.62,
            difficulty=0.5,
            modifiers=[("有建议", 1.2), ("疲惫", 0.85)],
            roll=lambda: 0.31,
        )
        line = result.line()
        self.assertIn("灵巧 0.62", line)
        self.assertIn("有建议", line)
        self.assertIn("难度 0.50", line)
        self.assertIn("掷 0.31", line)

    def test_subjective_hint_biases(self):
        calm = subjective_hint(0.5, affect=0.1, energy=0.8, composure=0.8)
        excited = subjective_hint(0.5, affect=0.9, energy=0.8, composure=0.8)
        tired = subjective_hint(0.5, affect=0.1, energy=0.1, composure=0.8)
        self.assertIn(calm, (HINT_OK, HINT_SURE))
        self.assertNotEqual(calm, tired)
        self.assertGreater(surprise(0.5, HINT_SURE), surprise(0.5, HINT_OK))
        self.assertIn(excited, (HINT_OK, HINT_SURE))

    def test_situational_modifiers(self):
        mods = dict(
            situational_modifiers(energy=0.1, affect=0.9, alignment=2, failed_before=True)
        )
        self.assertIn("疲惫", mods)
        self.assertIn("太兴奋", mods)
        self.assertIn("有建议", mods)
        self.assertIn("上次没成", mods)
        self.assertEqual(situational_modifiers(energy=0.8, affect=0.3), [])


class TestAbilityGuardrails(unittest.TestCase):
    """能力值：单次上限、每日上限、跨天清零。"""

    def test_step_limit(self):
        applied, _spent = clamp_ability_delta({"wits": 0.9}, day="d1", today="d1")
        self.assertAlmostEqual(applied["wits"], ABILITY_STEP_LIMIT)

    def test_daily_limit(self):
        spent = {}
        total = 0.0
        for _ in range(5):
            applied, spent = clamp_ability_delta(
                {"wits": 0.05}, spent_today=spent, day="d1", today="d1"
            )
            total += applied.get("wits", 0.0)
        self.assertAlmostEqual(total, ABILITY_DAY_LIMIT, places=3)

    def test_day_rollover_resets(self):
        applied, new_spent = clamp_ability_delta(
            {"wits": 0.05}, spent_today={"wits": 0.1}, day="d1", today="d2"
        )
        self.assertAlmostEqual(applied["wits"], 0.05)
        self.assertAlmostEqual(new_spent["wits"], 0.05)

    def test_daily_limit_also_caps_negative(self):
        applied, _spent = clamp_ability_delta(
            {"wits": -0.05}, spent_today={"wits": -0.1}, day="d1", today="d1"
        )
        self.assertEqual(applied, {})


class TestEventPayloads(unittest.TestCase):
    """模型输出 → 事件包 / 抉择 / 结算 / 建议分拣。"""

    def test_parse_package_tolerates_noise(self):
        raw = (
            "好的，我编一个：\n```json\n"
            '{"title": "锅盖卡死", "hook": "蒸汽往外顶", "tier": "small",'
            ' "options": [{"desc": "硬掰", "abilities": ["stamina"]},'
            ' {"desc": "先关火", "abilities": ["wits"]},'
            ' {"desc": "先撤开", "no_check": true}],'
            ' "followup": {"fail": "手背烫了一下"}}\n```'
        )
        package = parse_event_package(raw)
        self.assertIsNotNone(package)
        self.assertEqual(package.title, "锅盖卡死")
        self.assertEqual(len(package.options), 3)
        self.assertTrue(package.options[-1].no_check)
        self.assertEqual(package.followup.get("fail"), "手背烫了一下")

    def test_package_rejects_junk(self):
        self.assertIsNone(parse_event_package("什么都没给"))

    def test_parse_decision_by_index_and_desc(self):
        package = event_from_payload(
            {
                "title": "t",
                "hook": "h",
                "options": [
                    {"desc": "A", "abilities": ["wits"]},
                    {"desc": "B", "abilities": ["stamina"]},
                ],
            }
        )
        by_index = parse_decision('{"pick": 2}', package)
        self.assertEqual(by_index.desc, "B")
        self.assertEqual(by_index.abilities, ["stamina"])
        by_desc = parse_decision(
            '{"desc": "用毛巾垫着", "abilities": ["dexterity"],'
            ' "say": {"success": [{"text": "成了"}]}}',
            package,
        )
        self.assertEqual(by_desc.desc, "用毛巾垫着")
        self.assertEqual(by_desc.abilities, ["dexterity"])
        self.assertEqual(by_desc.lines("success")[0]["text"], "成了")

    def test_decision_can_carry_the_target_session(self):
        """事件里她也能挑说给哪个会话听（会话组里几个群 / 私聊都是她）。"""

        package = event_from_payload(
            {"title": "t", "hook": "h", "options": [{"desc": "A", "abilities": ["wits"]}]}
        )
        picked = parse_decision(
            '{"pick": 1, "send_to": "私聊 2692047521"}', package
        )
        self.assertEqual(picked.send_to, "私聊 2692047521")
        # 没写就留空：调用方会落回原来的地方
        self.assertEqual(parse_decision('{"pick": 1}', package).send_to, "")

    def test_default_decision_prefers_the_way_out(self):
        package = event_from_payload(
            {
                "title": "t",
                "hook": "h",
                "options": [
                    {"desc": "A", "abilities": ["wits"]},
                    {"desc": "先放着", "no_check": True},
                ],
            }
        )
        decision = default_decision(package)
        self.assertTrue(decision.no_check)
        self.assertEqual(decision.desc, "先放着")

    def test_parse_settlement(self):
        settlement = parse_settlement(
            '{"outcome": "锅盖弹开了", "ability_delta": {"stamina": 0.03, "x": 0.5},'
            ' "state_delta": {"valence": 0.05, "bad": 1}, "followup": "手上还烫着"}'
        )
        self.assertEqual(settlement.outcome, "锅盖弹开了")
        self.assertEqual(settlement.ability_delta, {"stamina": 0.03})
        self.assertEqual(settlement.state_delta, {"valence": 0.05})
        self.assertEqual(settlement.followup, "手上还烫着")

    def test_suggestions_keep_teases_and_drop_noise(self):
        raw = (
            '{"related": [{"from": "老普", "kind": "suggestion", "point": "先关火"},'
            ' {"from": "狗子", "kind": "tease", "point": "让她放壶宝"},'
            ' {"from": "路人", "kind": "cheer", "point": "加油"}],'
            ' "irrelevant": [{"from": "某人", "point": "在问今晚吃啥"}]}'
        )
        result = parse_suggestions(raw)
        self.assertEqual([item["point"] for item in result.suggestions], ["先关火"])
        self.assertEqual([item["point"] for item in result.teasers], ["让她放壶宝"])
        self.assertEqual([item["point"] for item in result.cheers], ["加油"])
        self.assertEqual(len(result.irrelevant), 1)
        self.assertEqual(result.alignment, 1)
        self.assertIn("起哄", result.prompt_block())

    def test_thread_helpers(self):
        package = event_from_payload({"title": "T", "hook": "H"})
        thread = new_thread(package, now=1000.0)
        append_step(
            thread,
            outcome={"status": "open", "followup": "还有下一步", "next_step_gap": 60.0},
            now=1000.0,
            step={"desc": "硬掰", "tier": TIER_FAIL, "result": "烫到手"},
        )
        self.assertIn("硬掰", thread_digest(thread))
        self.assertIn("烫到手", thread_digest(thread))
        self.assertFalse(
            thread_exhausted(thread, now=1000.0, max_steps=6, max_minutes=120)
        )
        self.assertTrue(
            thread_exhausted(thread, now=1000.0 + 121 * 60, max_steps=6, max_minutes=120)
        )


class TestWorldDefaults(unittest.TestCase):
    """事件现在是临场生成的：默认世界里不再有内置事件池。"""

    def test_default_world_parses_without_an_event_pool(self):
        world, warnings = parse_world(default_world())
        self.assertEqual(warnings, [])
        self.assertFalse(hasattr(world, "events_pool"))
        self.assertTrue(world.node_map())
        self.assertTrue(world.action_map())

    def test_default_red_lines_and_fail_menu_are_filled(self):
        """默认预设就带上红线与「没成」的写法参考，否则失败清一色写成受伤。"""

        world, _warnings = parse_world(default_world())
        self.assertTrue(world.events.red_lines)
        self.assertTrue(world.events.fail_result_menu)

    def test_test_event_payload_parses(self):
        """测试自己用的那条事件包本身要能被解析（后面全靠它驱动流程）。"""

        package = event_from_payload(TEST_POOL_EVENT)
        self.assertIsNotNone(package)
        self.assertTrue(package.title)
        self.assertTrue(package.hook)
        self.assertTrue(any(item.no_check for item in package.options))


# ======================================================================
# 引擎接线
# ======================================================================

TEST_POOL_EVENT = {
    "id": "test_box",
    "tier": "small",
    "kind": "solo",
    "title": "打不开的盒子",
    "hook": "桌上多了个盒子，盖子卡得很死",
    "scene": "书房，桌上",
    "difficulty": 0.5,
    "options": [
        {"desc": "硬掰", "abilities": ["stamina"]},
        {"desc": "找把工具撬", "abilities": ["wits"]},
        {"desc": "先放着", "no_check": True},
    ],
    "followup": {"success": "盒子开了", "fail": "手弄疼了"},
}


class EventEngineTest(unittest.IsolatedAsyncioTestCase):
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
        raw = self.store.raw_world()
        raw["events"] = {
            "enabled": True,
            "micro_per_hour": 0.0,
            "small_per_hour": 0.0,
            "big_per_hour": 0.0,
            "dwell_minutes": 0,
            "active_seconds": 180,
            "idle_minutes": 60,
            "step_gap_seconds": 60,
        }
        self.store.save_world(raw)

        self.db = AsyncDatabase(self.store.db_path)
        self.addAsyncCleanup(self.db.close)

        self.main = ScriptLLM()
        self.helper = ScriptLLM()
        self.creator = ScriptLLM()
        self.messenger = StubMessenger()
        self.tools = StubTools({"web_search": "搜索网页"})
        self.persona = StubPersona("你是一只又懒又爱面子的鲸鱼少女，自称本小姐。")
        self.clock = StubClock(now=1_700_000_000.0)
        self.engine = VirtualWorldEngine(
            store=self.store,
            db=self.db,
            llm=self.main,
            helper_llm=self.helper,
            creator_llm=self.creator,
            messenger=self.messenger,
            tools=self.tools,
            persona=self.persona,
            clock=self.clock,
            tick_seconds=60.0,
            decider_interval=0.0,
        )
        self.engine.rng = FakeRandom([0.0, 0.0, 0.4, 0.9, 0.3, 0.7])
        # 让"要不要问大模型安排计划"永远不中，tick 里就不会冒出计划调用
        self.engine.decider.rng = FakeRandom([1.0])
        self.engine.reload_config()

    def use_pool_only(self, *events: dict) -> None:
        """这一轮"临场生成"的就是测试给的那一条。

        事件现在是每次都由模型现编的，所以这里换成给定生成模型的返回值——
        名字保留着，是为了让测试读起来还是"这件事发生了"。
        """

        if len(events) == 1:
            self.creator.default_reply = json.dumps(events[0], ensure_ascii=False)
            return
        self.creator.replies = [json.dumps(item, ensure_ascii=False) for item in events]

    def add_schedule(
        self, *, schedule_id: str, time: str, actions: list[dict], priority: int = 5
    ) -> None:
        """加一条日程（闸门测试用）。"""

        raw = self.store.raw_schedules()
        raw.setdefault("schedules", []).append(
            {
                "id": schedule_id,
                "enabled": True,
                "time": time,
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "action_chain": [dict(item) for item in actions],
                "conditions": {"not_state": []},
                "priority": priority,
            }
        )
        self.store.save_schedules(raw)
        self.engine.reload_config()

    def schedule_by_id(self, schedule_id: str):
        for item in list(self.engine.schedules.schedules):
            if item.id == schedule_id:
                return item
        raise AssertionError(f"没有这条日程：{schedule_id}")

    async def get_state(self):
        return await self.engine.load_state(SESSION, cold_start=False)

    async def set_state(self, *, node_id: str) -> None:
        """把她挪到某个地点（断言记忆与日记按地点落点时用）。"""

        async with self.engine.session_state(SESSION) as state:
            state.node_id = node_id
            state.node_since = self.clock.now() - 3600

    async def group_says(
        self, text: str = "在聊今晚吃什么", *, who: str = "老普", user_id: str = "7"
    ) -> None:
        """往群聊留档里塞一条"别人说的话"：用来模拟群里有人在。"""

        async with self.engine.session_state(SESSION) as state:
            state.note_chat(
                user_id=user_id, name=who, text=text, now=self.clock.now(), keep=20
            )

    def add_lookup_action(self, **overrides) -> None:
        """加一个「查资料」工具型动作：测「事件里先做点什么」用。"""

        payload = {
            "id": "lookup",
            "name": "查资料",
            "category": "instant",
            "llm_level": "tool",
            "tool_names": ["web_search"],
            "scope": "global",
            "visible": False,
            "description": "上网查一件事。",
        }
        payload.update(overrides)
        raw = self.store.raw_world()
        raw["actions"] = [
            item for item in raw["actions"] if str(item.get("id")) != payload["id"]
        ]
        raw["actions"].append(payload)
        self.store.save_world(raw)
        self.engine.reload_config()

    async def start_event(self, *, tier: str = "small", seed: str = "") -> TickOutcome:
        """直接开一件事（跳过掷骰与环境判断，测试里更好控制）。"""

        outcome = TickOutcome(session_id=SESSION)
        async with self.engine.session_state(SESSION) as state:
            state.node_since = self.clock.now() - 3600
            node = self.engine.node(state.node_id)
            await self.engine._start_event(
                state, node, outcome, tier=tier, seed=seed, source="roll"
            )
        return outcome

    async def events(self, limit: int = 40) -> list[dict]:
        return await self.db.call("query_events", session_id=SESSION, limit=limit)

    async def event_types(self, limit: int = 40) -> list[str]:
        return [str(item["event_type"]) for item in await self.events(limit)]

    async def test_pool_event_runs_decision_check_and_settlement(self):
        """一条小事件：她抉择 → 掷骰 → 结算 → 写记忆 → 进「这段时间」。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.replies = [
            '{"pick": 1, "reason": "本小姐懒得找工具",'
            ' "say": {"success": [{"text": "开了，看本小姐的"}],'
            '         "fail": [{"text": "……手疼"}]}}'
        ]
        self.helper.replies = [
            '{"outcome": "盖子被掰开了，里面是空的", "ability_delta": {"stamina": 0.03},'
            ' "state_delta": {"valence": 0.05}}'
        ]
        outcome = await self.start_event()

        state = await self.get_state()
        self.assertGreater(state.abilities["stamina"], 0.6)
        self.assertTrue(state.event_digest)
        self.assertEqual(len(state.event_threads), 1)
        self.assertEqual(state.event_threads[0]["status"], "closed")
        self.assertEqual(state.event_threads[0]["steps"][0]["desc"], "硬掰")
        kinds = await self.event_types()
        for want in ("event", "event_choice", "event_check", "event_result"):
            self.assertIn(want, kinds)
        self.assertIn("开了", " ".join(outcome.messages))
        check = [item for item in await self.events() if item["event_type"] == "event_check"][0]
        self.assertEqual(check["detail"]["ability"], "体力")
        self.assertIn("modifiers", check["detail"])

    async def test_micro_event_costs_no_model_call(self):
        """微事件：结果与效果都写在池子里，一次模型都不调。"""

        self.use_pool_only(
            {
                "id": "micro_test",
                "tier": "micro",
                "kind": "solo",
                "title": "水开了",
                "hook": "壶盖被顶得咕嘟响",
                "outcome": "她关火的时候被热气扑了一下",
                "effects": {"affect": 0.04},
                "abilities": {"dexterity": -0.01},
            }
        )
        # 默认就是"微事件只走池子"，这里把概率也设成 0，验证它真的不调生成器
        raw = self.store.raw_world()
        raw["events"]["generate_micro"] = 0.0
        self.store.save_world(raw)
        self.engine.reload_config()

        await self.start_event(tier="micro")

        self.assertEqual(self.main.calls, [])
        self.assertEqual(self.helper.calls, [])
        state = await self.get_state()
        self.assertTrue(state.event_digest)

    async def test_every_tier_is_generated_on_the_spot(self):
        """没有"走池子"这一档了：微事件也一样由生成模型按当前场景现编。"""

        from core.models import EventConfig

        defaults = EventConfig()
        for gone in ("generate", "generate_micro", "generate_small", "generate_big"):
            self.assertFalse(hasattr(defaults, gone), gone)

        self.creator.replies = [
            '{"title": "临场生成的事", "hook": "桌上多了个盒子", "tier": "small",'
            ' "options": [{"desc": "打开", "abilities": ["wits"]},'
            ' {"desc": "先放着", "no_check": true}]}'
        ]
        self.main.replies = ['{"pick": 2, "reason": "懒"}']
        self.helper.replies = ['{"outcome": "就没打开"}']
        await self.start_event()

        self.assertTrue(self.creator.calls)

        # 微事件也要过生成模型（以前默认 0 概率，一次模型都不调）
        self.creator.replies = [
            '{"title": "水开了", "hook": "壶盖被顶得咕嘟响", "tier": "micro",'
            ' "outcome": "她关火的时候被热气扑了一下"}'
        ]
        self.creator.calls.clear()
        await self.start_event(tier="micro")
        self.assertTrue(self.creator.calls)
        state = await self.get_state()
        self.assertEqual(state.event_threads[-1]["title"], "水开了")

    async def test_event_source_is_labelled_in_the_log(self):
        """日志里标出来源：临场生成 / 事件池 / 用户投递 / 线索续演。"""

        from core.timeline import EVENT_SOURCE_LABELS, build_timeline

        self.assertEqual(EVENT_SOURCE_LABELS["llm"], "临场生成")
        self.assertNotIn("pool", EVENT_SOURCE_LABELS)
        self.assertEqual(EVENT_SOURCE_LABELS["user"], "用户投递")
        self.assertEqual(EVENT_SOURCE_LABELS["follow"], "线索续演")

        self.use_pool_only(
            {
                "id": "micro_pool2",
                "tier": "micro",
                "kind": "solo",
                "title": "水开了",
                "hook": "壶盖被顶得咕嘟响",
                "outcome": "她关火的时候被热气扑了一下",
            }
        )
        await self.start_event(tier="micro")
        rows = await self.db.call("query_events", session_id=SESSION, limit=40)
        texts = [item["text"] for item in build_timeline(rows, self.engine.world)]
        hit = [text for text in texts if text.startswith("遇到「水开了」")]
        self.assertTrue(hit, texts)
        # 临场生成的那一条：来源标成「临场生成」
        self.assertIn("临场生成", hit[0])

    async def test_ability_delta_respects_the_daily_cap(self):
        """同一天连着好几件事，能力值最多长到每日上限。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = '{"pick": 1, "reason": "上"}'
        self.helper.default_reply = (
            '{"outcome": "折腾半天", "ability_delta": {"stamina": 0.05}}'
        )
        for _ in range(4):
            await self.start_event()
        state = await self.get_state()
        self.assertLessEqual(
            state.abilities["stamina"], 0.6 + ABILITY_DAY_LIMIT + 1e-6
        )

    async def test_failed_check_still_grows(self):
        """失败也长能力值：这是「还能再试」的依据。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.replies = ['{"pick": 1, "reason": "上"}']
        self.helper.replies = ['{"outcome": "没掰开，手倒是疼了"}']
        # 掷骰给 0.99：必失败
        self.engine.rng = FakeRandom([0.99, 0.99, 0.99, 0.99])
        await self.start_event()
        state = await self.get_state()
        self.assertGreater(state.abilities["stamina"], 0.6)
        self.assertEqual(state.event_threads[0]["steps"][0]["tier"], TIER_FAIL)

    async def test_help_flow_consumes_group_reply(self):
        """求助：进入活跃等待 → 有人回 → 分拣建议 → 她重新拿主意并收尾。"""

        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        # 群里得有人在，她才会开口问（没人的时候问了也没人接）
        await self.group_says()

        self.main.replies = [
            '{"pick": 1, "reason": "先自己试试", "ask_help": true,'
            ' "say": {"plan": [{"text": "有人吗"}, {"text": "桌上这盒子打不开"}]}}',
            '{"pick": 2, "reason": "听老普的", "say": {"success": [{"text": "撬开了"}]}}',
        ]
        self.helper.replies = [
            '{"related": [{"from": "老普", "kind": "suggestion", "point": "用卡片撬一下"}],'
            ' "irrelevant": []}',
            '{"outcome": "用卡片一撬就开了", "ability_delta": {"wits": 0.02}}',
        ]
        outcome = await self.start_event()
        self.assertTrue(outcome.messages)
        state = await self.get_state()
        self.assertEqual(state.pending_help.get("state"), "active")
        self.assertGreater(await self.engine.pending_intervention(SESSION), 0.0)

        reply = await self.engine.handle_reply(
            MessageContext(
                session_id=SESSION,
                user_id="7",
                user_name="老普",
                text="用卡片撬一下就开了",
                is_wake=True,
                is_mentioned=True,
            )
        )

        self.assertTrue(reply.ok, reply.error)
        self.assertIn("撬开了", " ".join(reply.messages))
        state = await self.get_state()
        self.assertEqual(state.pending_help, {})
        self.assertEqual(await self.engine.pending_intervention(SESSION), 0.0)
        self.assertEqual(state.event_threads[0]["status"], "closed")
        self.assertIn("help", await self.event_types())
        # 日志里要留着群友的原话，不能只写一条空摘要
        rows = [
            item
            for item in await self.events(limit=30)
            if item["event_type"] == "help" and item["detail"].get("stage") == "reply"
        ]
        self.assertTrue(rows)
        self.assertIn("用卡片撬一下", str(rows[0]["detail"].get("text") or ""))
        self.assertEqual(rows[0]["detail"].get("kept"), 1)

    async def test_unrelated_reply_does_not_eat_the_message(self):
        """完全无关的回应不会被她的事件线吃掉：照常走普通回复。"""

        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        await self.group_says()
        self.main.replies = [
            '{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}',
            SAY_REPLY,
        ]
        self.helper.replies = [
            '{"related": [], "irrelevant": [{"from": "路人", "point": "在聊吃的"}]}'
        ]
        await self.start_event()

        reply = await self.engine.handle_reply(
            MessageContext(
                session_id=SESSION,
                user_id="9",
                user_name="路人",
                text="今晚吃啥",
                is_wake=True,
                is_mentioned=True,
            )
        )
        self.assertTrue(reply.ok, reply.error)
        state = await self.get_state()
        self.assertEqual(state.pending_help.get("state"), "active")

    async def test_help_times_out_and_she_finishes_alone(self):
        """等不到人：先轻提醒降到轻等待，超时后按她自己的判断收尾。"""

        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        raw = self.store.raw_world()
        raw["events"]["active_seconds"] = 30
        raw["events"]["idle_minutes"] = 1
        self.store.save_world(raw)
        self.engine.reload_config()
        self.main.replies = [
            '{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}'
        ]
        # 打杂模型的调用顺序：先写"没人接我自己来"那一句，再写收尾的结果
        self.helper.replies = [
            '{"line": "算了，我自己来。"}',
            '{"outcome": "最后她自己把盒子塞回柜子里了"}',
        ]
        await self.group_says()
        await self.start_event()

        self.clock.advance(40)
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.pending_help.get("state"), "idle")
        self.assertTrue(
            [text for text in self.messenger.flat_messages if text.strip()]
        )

        self.clock.advance(90)
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.pending_help, {})
        self.assertEqual(state.event_threads[0]["status"], "closed")
        self.assertIn("event_idle", await self.event_types())

    async def test_ignored_help_reminder_is_written_by_the_model(self):
        """求助没人接时那句圆场由模型按人设现写，不再是代码里那三句固定台词。"""

        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        raw = self.store.raw_world()
        raw["events"]["active_seconds"] = 30
        self.store.save_world(raw)
        self.engine.reload_config()
        self.main.replies = [
            '{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}'
        ]
        self.helper.default_reply = '{"line": "行吧，我自己弄。"}'
        await self.group_says()
        await self.start_event()
        self.helper.calls.clear()

        self.clock.advance(40)
        await self.engine.tick()

        self.assertTrue(self.helper.calls, "这一句该由模型写")
        self.assertIn("一直没人接话", self.helper.calls[0]["system_prompt"])
        self.assertIn("行吧，我自己弄。", self.messenger.flat_messages)

    async def test_ignored_help_reminder_can_be_turned_off(self):
        """关掉「补一句自我圆场」之后，她求助完就安静等着，不追这一句。"""

        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        raw = self.store.raw_world()
        raw["events"]["active_seconds"] = 30
        raw["events"]["remind_when_ignored"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        self.main.replies = [
            '{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}'
        ]
        self.helper.default_reply = '{"line": "行吧，我自己弄。"}'
        await self.group_says()
        await self.start_event()
        before = len(self.messenger.flat_messages)
        self.helper.calls.clear()

        self.clock.advance(40)
        await self.engine.tick()

        self.assertEqual(self.helper.calls, [])
        self.assertEqual(len(self.messenger.flat_messages), before)

    async def test_event_followups_keep_the_session_she_picked(self):
        """她当初挑了别的会话说这件事：后面的轻提醒和收尾也要回到那儿。"""

        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        raw = self.store.raw_world()
        raw["events"]["active_seconds"] = 30
        raw["events"]["idle_minutes"] = 1
        self.store.save_world(raw)
        self.engine.reload_config()
        self.main.replies = [
            '{"pick": 1, "ask_help": true, "send_to": "别处",'
            ' "say": {"plan": [{"text": "有人吗"}]}}'
        ]
        self.helper.replies = ['{"line": "算了，我自己来"}', '{"outcome": "她自己搞定了"}']
        await self.group_says()
        # 会话组里的另一个落点：把解析结果固定住，测的是"有没有用它"
        self.engine.resolve_send_to = (
            lambda state, value, fallback="": "other:session"
            if str(value or "").strip()
            else fallback
        )
        first = await self.start_event()
        self.assertEqual(first.session_id, "other:session")

        async with self.engine.session_state(SESSION) as state:
            node = self.engine.node(state.node_id)
            # 活跃等待到点 → 那一句"没人接，我自己来"
            reminder = TickOutcome(session_id=SESSION)
            await self.engine._advance_pending_help(
                state, node, reminder, self.clock.now() + 40
            )
            self.assertEqual(reminder.session_id, "other:session")
            # 总超时 → 她自己收尾那一幕
            timeout = TickOutcome(session_id=SESSION)
            await self.engine._advance_pending_help(
                state, node, timeout, self.clock.now() + 200
            )
            self.assertEqual(timeout.session_id, "other:session")

    async def test_help_ask_only_when_someone_is_around(self):
        """群里没人时她不喊「有人吗」：自己拿主意，群里只看到结果。"""

        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        self.main.default_reply = (
            '{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}'
        )
        self.helper.default_reply = '{"outcome": "她自己找了把改锥把盒子撬开了"}'

        outcome = await self.start_event()

        self.assertNotIn("有人吗", " ".join(outcome.messages))
        self.assertEqual(outcome.messages, [], "群里没人时她不该在群里说话")
        state = await self.get_state()
        self.assertEqual(state.pending_help, {}, "没人可问就不该进入等待")
        # 她自己把事办了：结果照样进记忆
        rows = self.engine.memory.db.query_memories(session_id=SESSION, limit=10)
        self.assertTrue([row for row in rows if "改锥" in str(row.get("content") or "")])

    async def test_help_fallback_repeats_the_hook_not_the_title(self):
        """模型没写求助文案时，兜底那句要说"发生了什么"，不能只念事件标题。"""

        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        await self.group_says()
        self.main.default_reply = '{"pick": 1, "ask_help": true}'
        self.helper.default_reply = '{"outcome": "她自己搞定了"}'

        outcome = await self.start_event()

        text = " ".join(outcome.messages)
        self.assertIn("盖子卡得很死", text)
        self.assertNotIn("打不开的盒子……", text)

    async def test_help_lines_come_from_the_model(self):
        """求助那两句让她自己说（打杂模型写），不再是代码里固定的「有人吗」。"""

        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        await self.group_says()
        self.main.default_reply = '{"pick": 1, "ask_help": true}'
        self.helper.default_reply = (
            '{"lines": ["桌上这个盒子卡死了", "谁来帮我看看怎么开"]}'
        )

        outcome = await self.start_event()

        self.assertEqual(
            outcome.messages, ["桌上这个盒子卡死了", "谁来帮我看看怎么开"]
        )

    async def test_sleeping_events_stay_quiet_in_the_group(self):
        """她睡着时事件照演，但不在群里出声（被 @ 才是那句睡觉的固定文案）。"""

        raw = self.store.raw_world()
        raw["events"]["in_sleep"] = True
        self.store.save_world(raw)
        self.engine.reload_config()
        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = (
            '{"pick": 1, "say": {"success": ["撬开了"], "fail": ["没撬开"]}}'
        )
        self.helper.default_reply = '{"outcome": "盒子自己弹开了"}'
        async with self.engine.session_state(SESSION) as state:
            state.state = "sleeping"
            state.node_since = self.clock.now() - 3600

        outcome = await self.start_event()
        await self.engine._deliver(outcome)

        self.assertEqual(outcome.messages, [])
        self.assertEqual(self.messenger.sent, [], "睡着时不该往群里发话")
        rows = self.engine.memory.db.query_memories(session_id=SESSION, limit=10)
        self.assertTrue([row for row in rows if "弹开" in str(row.get("content") or "")])

    async def test_thread_continues_only_when_followup_says_so(self):
        """伏笔决定还有没有下一步；续线时把整条线索喂回去。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = '{"pick": 1, "reason": "继续"}'
        self.helper.default_reply = (
            '{"outcome": "手被划了一下", "followup": "得去找个创可贴"}'
        )
        await self.start_event()
        state = await self.get_state()
        self.assertEqual(state.event_threads[0]["status"], "open")
        self.assertTrue(state.event_threads[0]["pending_followup"])

        self.clock.advance(120)
        await self.engine.tick()
        state = await self.get_state()
        self.assertGreaterEqual(len(state.event_threads[0]["steps"]), 2)

    async def test_submit_event_ignores_the_dwell_gate(self):
        """主动投递不受「在同一个地方待够几分钟」的限制：刚到了也能立刻发生。"""

        self.main.default_reply = '{"pick": 1, "reason": "上"}'
        self.helper.default_reply = '{"outcome": "事情办完了"}'
        # 打开「待够 5 分钟」这条门槛
        raw = self.store.raw_world()
        raw["events"]["dwell_minutes"] = 5
        self.store.save_world(raw)
        self.engine.reload_config()
        # 刚落地：node_since 就是现在，自动掷骰会被 dwell 门槛挡住
        async with self.engine.session_state(SESSION) as state:
            state.node_since = self.clock.now()
            blocked = self.engine._event_block_reason(state, self.clock.now())
            self.assertTrue(blocked, "自动掷骰应该被「待够时间」挡住")

        note = await self.engine.submit_event(SESSION, "出门忘了带伞")

        self.assertEqual(note, "")
        state = await self.get_state()
        self.assertEqual(len(state.event_threads), 1)
        self.assertEqual(state.event_threads[0]["title"], "出门忘了带伞")

    async def test_submit_event_rejects_when_busy_or_asleep(self):
        """忙 / 在走路 / 在睡觉时**直接拒绝**，不排队。"""

        self.main.default_reply = '{"pick": 1, "reason": "上"}'
        self.helper.default_reply = '{"outcome": "事情办完了"}'

        async with self.engine.session_state(SESSION) as state:
            state.state = "sleeping"
        note = await self.engine.submit_event(SESSION, "窗外有只鸟撞进来了")
        self.assertIn("睡觉", note)
        state = await self.get_state()
        self.assertEqual(state.event_threads, [])

        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            state.current_action = {
                "type": "cook",
                "desc": "做饭",
                "elapsed_ticks": 1,
                "duration_ticks": 10,
            }
        note = await self.engine.submit_event(SESSION, "锅盖卡死了")
        self.assertIn("做饭", note)
        state = await self.get_state()
        self.assertEqual(state.event_threads, [])

        # 忙完之后立刻能投
        async with self.engine.session_state(SESSION) as state:
            state.current_action = None
        note = await self.engine.submit_event(SESSION, "锅盖卡死了")
        self.assertEqual(note, "")
        state = await self.get_state()
        self.assertEqual(len(state.event_threads), 1)

    # ---------------- 编辑器上的手动推进 / 手动完结 ----------------

    async def test_manual_advance_ignores_the_step_gap(self):
        """「立即推进一幕」不用等那个间隔：把没完的那件事往前推一步。"""

        self.use_pool_only(TEST_POOL_EVENT)
        # 推进出来的这一幕她要真的说一句，才能验证"群里看得到"
        self.main.default_reply = (
            '{"pick": 1, "reason": "继续",'
            ' "say": {"success": ["找着了"], "fail": ["还没找着"]}}'
        )
        self.helper.default_reply = (
            '{"outcome": "手被划了一下", "followup": "得去找个创可贴"}'
        )
        await self.start_event()
        async with self.engine.session_state(SESSION) as state:
            thread = state.event_threads[0]
            before = len(thread["steps"])
            thread_id = str(thread["id"])
            # 下一步还得等 10 分钟：正常 tick 这会儿推不动
            thread["next_step_at"] = self.clock.now() + 600

        self.helper.default_reply = '{"outcome": "创可贴找着了"}'
        note = await self.engine.advance_thread(SESSION, thread_id)

        self.assertIn("推进了一幕", note)
        state = await self.get_state()
        thread = state.event_threads[0]
        self.assertGreater(len(thread["steps"]), before)
        self.assertTrue(self.messenger.sent, "推进出来的话要发给群里")

    async def test_manual_advance_waits_for_the_group_instead_of_guessing(self):
        """她在等群友拿主意时不手动推进：那时该等的是人，不是时钟。"""

        await self.group_says()
        self.use_pool_only(
            {
                "id": "ask_around",
                "tier": "small",
                "kind": "need_intervene",
                "title": "拿不准",
                "hook": "有两件事撞一起了",
                "difficulty": 0.5,
                "options": [
                    {"desc": "先做这件", "abilities": ["wits"]},
                    {"desc": "先放着", "no_check": True},
                ],
            }
        )
        self.main.default_reply = '{"pick": 1, "reason": "问问大家", "ask_help": true}'
        self.helper.default_reply = '{"outcome": "她拿不定主意"}'
        await self.start_event()
        state = await self.get_state()
        self.assertIn(state.pending_help.get("state"), ("active", "idle"))

        note = await self.engine.advance_thread(SESSION)

        self.assertIn("等群友", note)

    async def test_manual_close_ends_the_thread_and_lets_the_help_wait_go(self):
        """「立刻完结」：这条线索收尾，等着的那条也一起放掉。"""

        await self.group_says()
        self.use_pool_only(
            {
                "id": "ask_around",
                "tier": "small",
                "kind": "need_intervene",
                "title": "拿不准",
                "hook": "有两件事撞一起了",
                "difficulty": 0.5,
                "options": [
                    {"desc": "先做这件", "abilities": ["wits"]},
                    {"desc": "先放着", "no_check": True},
                ],
            }
        )
        self.main.default_reply = '{"pick": 1, "reason": "问问大家", "ask_help": true}'
        self.helper.default_reply = '{"outcome": "她拿不定主意"}'
        await self.start_event()

        note = await self.engine.close_thread(SESSION)

        self.assertIn("已完结", note)
        state = await self.get_state()
        thread = state.event_threads[0]
        self.assertEqual(thread["status"], "closed")
        self.assertEqual(thread["pending_followup"], "")
        self.assertEqual(thread["next_step_at"], 0.0)
        self.assertEqual(state.pending_help, {})
        self.assertIn("event_idle", await self.event_types())

    async def test_manual_close_says_so_when_there_is_nothing_to_close(self):
        note = await self.engine.close_thread(SESSION)

        self.assertIn("没有正在进行", note)

    # ---------------- 事件里先用动作查清楚再拿主意 ----------------

    async def test_event_can_use_an_action_before_deciding(self):
        """phase=act：先调动作拿信息 → 结果交回给她 → 下一轮才拿主意。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.add_lookup_action()
        self.main.replies = [
            '{"phase": "act", "reason": "先查一下盒子怎么开",'
            ' "actions": [{"type": "lookup", "intent": "查一下这种盒子怎么开"}]}',
            '{"pick": 2, "reason": "有资料了",'
            ' "say": {"success": ["照着查到的办法撬开了"]}}',
        ]
        self.helper.default_reply = '{"outcome": "盒子开了"}'

        outcome = await self.start_event()

        self.assertEqual([name for name, _params in self.tools.calls], ["web_search"])
        state = await self.get_state()
        thread = state.event_threads[0]
        self.assertEqual(thread["action_calls"], 1)
        self.assertTrue(thread["observations"])
        # 查到的内容只用来判断，不往群里念
        self.assertNotIn("web_search 的结果", " ".join(outcome.messages))
        self.assertIn("照着查到的办法撬开了", " ".join(outcome.messages))
        # 第二轮抉择的提示词里带着上一轮的结果
        second_prompt = self.main.calls[-1]["prompt"]
        self.assertIn("你刚才做的事", second_prompt)
        self.assertIn("web_search 的结果", second_prompt)

    async def test_event_action_prompt_lists_the_available_actions(self):
        """提示词里只列可用动作（id + 一句说明），并说明结果用来帮她判断。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.add_lookup_action()
        self.main.replies = ['{"pick": 1}']
        self.helper.default_reply = '{"outcome": "盒子开了"}'

        await self.start_event()

        first_prompt = self.main.calls[-1]["prompt"]
        self.assertIn("可以先用它做点什么", first_prompt)
        self.assertIn("lookup", first_prompt)
        self.assertIn("phase", self.main.calls[-1]["system_prompt"])

    async def test_event_action_budget_stops_the_extra_rounds(self):
        """额度用完就不再给动作：她得直接拿主意。"""

        raw = self.store.raw_world()
        raw["events"]["event_action_calls"] = 1
        self.store.save_world(raw)
        self.engine.reload_config()
        self.use_pool_only(TEST_POOL_EVENT)
        self.add_lookup_action()
        self.main.replies = [
            '{"phase": "act", "actions": [{"type": "lookup", "intent": "查一下"}]}',
            '{"phase": "act", "actions": [{"type": "lookup", "intent": "再查一下"}]}',
            '{"pick": 2, "reason": "差不多了"}',
        ]
        self.helper.default_reply = '{"outcome": "盒子开了"}'

        await self.start_event()

        state = await self.get_state()
        thread = state.event_threads[0]
        self.assertEqual(thread["action_calls"], 1)
        self.assertEqual(len(self.tools.calls), 1, "第二次 act 不该真的执行")

    async def test_event_action_whitelist_follows_the_switch(self):
        """默认只放工具型/指令型；group 目标要显式 allow，deny 直接排除。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.add_lookup_action()
        self.add_lookup_action(id="say_thing", llm_level="tool", target_type="group")
        self.add_lookup_action(id="denied", event_usable="deny")
        self.add_lookup_action(id="forced", llm_level="single", event_usable="allow")
        async with self.engine.session_state(SESSION) as state:
            ids = [item.id for item in self.engine._event_action_defs(state, {})]

        self.assertIn("lookup", ids)
        self.assertNotIn("say_thing", ids)
        self.assertNotIn("denied", ids)
        self.assertIn("forced", ids)

    # ---------------- 掷骰节奏 ----------------

    async def test_check_result_moves_her_mood(self):
        """判定档位也带一点情绪：失败往下压，成功往上抬（可关）。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = '{"pick": 1}'
        self.helper.default_reply = '{"outcome": "没弄成"}'
        # 掷骰给 0.99：必失败
        self.engine.rng = FakeRandom([0.99, 0.99, 0.99, 0.99])
        async with self.engine.session_state(SESSION) as state:
            before = (state.valence, state.affect)
        await self.start_event()
        after = await self.get_state()
        self.assertLess(after.valence, before[0])
        self.assertGreater(after.affect, before[1])

        raw = self.store.raw_world()
        raw["events"]["result_emotion"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        self.engine.rng = FakeRandom([0.99, 0.99, 0.99, 0.99])
        self.use_pool_only(TEST_POOL_EVENT)
        async with self.engine.session_state(SESSION) as state:
            state.event_threads = []
            state.event_recent_titles = []
            before2 = (state.valence, state.affect)
        await self.start_event()
        after2 = await self.get_state()
        self.assertAlmostEqual(after2.valence, before2[0], places=3)
        self.assertAlmostEqual(after2.affect, before2[1], places=3)

    async def test_settlement_can_choose_the_next_gap(self):
        """下一幕隔多久交给模型（next_gap 分钟），配置值是它的下限。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = '{"pick": 1}'
        self.helper.default_reply = (
            '{"outcome": "手被划了一下", "followup": "得去找个创可贴", "next_gap": 20}'
        )

        await self.start_event()

        state = await self.get_state()
        thread = state.event_threads[0]
        self.assertAlmostEqual(
            thread["next_step_at"] - self.clock.now(), 20 * 60, delta=2
        )

    async def test_event_step_can_take_a_photo(self):
        """每一幕按概率出图：图要跟着这件事的内容走，图本身发到群里。"""

        from core.ports import ToolCallResult

        captured: dict[str, Any] = {}

        class StubCommands:
            async def trigger(self, session_id, command, *, event=None):
                captured["command"] = command
                return ToolCallResult(
                    ok=True,
                    text="画好了",
                    tool="画图",
                    attachments=["https://img/room.png"],
                )

        self.engine.commands = StubCommands()
        self.add_lookup_action(
            id="photo_cmd",
            name="自拍",
            llm_level="command",
            trigger_command="自拍",
            trigger_hint="画面描述",
            description="拍一张自己的照片发出来。",
        )
        raw = self.store.raw_world()
        raw["events"]["photo_chance"] = 1.0
        raw["events"]["photo_actions"] = ["photo_cmd"]
        self.store.save_world(raw)
        self.engine.reload_config()
        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = '{"pick": 1}'
        self.helper.default_reply = '{"outcome": "盒子开了"}'

        outcome = await self.start_event()

        self.assertEqual(outcome.images, ["https://img/room.png"])
        rows = [
            item
            for item in await self.events(limit=40)
            if item["event_type"] == "command_call"
        ]
        self.assertTrue(rows, "出图动作应该真的被执行")
        self.assertIn("打不开的盒子", str(rows[0]["detail"].get("intent") or ""))
        self.assertIn("桌上多了个盒子", str(rows[0]["detail"].get("intent") or ""))
        # 她发出去的图也要进自己的聊天记录：下一轮有人引用那张图，她才认得出
        state = await self.get_state()
        own = [
            item["text"]
            for item in state.recent_chat
            if item.get("is_self") and "我发了一张图片" in str(item.get("text") or "")
        ]
        self.assertTrue(own, state.recent_chat)
        self.assertIn("打不开的盒子", own[0])

    async def test_roll_clock_advances_every_tick_and_caps_the_gap(self):
        """掷骰时钟每 tick 都推进；一次最多按 15 分钟折算概率（不攒"欠账"）。"""

        raw = self.store.raw_world()
        raw["events"]["micro_per_hour"] = 1.5
        self.store.save_world(raw)
        self.engine.reload_config()
        async with self.engine.session_state(SESSION) as state:
            state.event_last_roll_at = self.clock.now() - 8 * 3600
        async with self.engine.session_state(SESSION) as state:
            gap = self.engine._event_roll_gap_hours(state, self.clock.now())
            self.assertEqual(gap, EVENT_ROLL_MAX_GAP_HOURS)
            self.assertEqual(state.event_last_roll_at, self.clock.now())
            _tier, info = self.engine._roll_event_tier(
                state, self.clock.now(), gap
            )
        # 8 小时不折算成"必中"：概率只按 15 分钟算
        self.assertAlmostEqual(
            info["chance"], 1.5 * EVENT_ROLL_MAX_GAP_HOURS, places=3
        )

    async def test_two_events_keep_the_minimum_gap(self):
        """两件事之间至少隔 min_gap_minutes：刚完结也不会立刻再来一件。"""

        self.use_pool_only(TEST_POOL_EVENT)
        raw = self.store.raw_world()
        # 概率上必中（生成出来的是 small 档）
        raw["events"]["small_per_hour"] = 600.0
        raw["events"]["min_gap_minutes"] = 30
        self.store.save_world(raw)
        self.engine.reload_config()
        await self.start_event()

        async with self.engine.session_state(SESSION) as state:
            state.event_threads = []  # 假装那件事已经收尾
            state.event_recent_titles = []  # 池子里那条也别被"最近用过"挡掉
            state.event_last_roll_at = self.clock.now() - 600
            node = self.engine.node(state.node_id)
            first = TickOutcome(session_id=SESSION)
            await self.engine._maybe_run_event(state, node, first)
            self.assertEqual(state.event_threads, [], "还没到最小间隔，不该再出事")
            self.assertTrue(self.engine._event_gap_reason(state, self.clock.now()))

            state.last_event_started_at = self.clock.now() - 3600
            state.event_last_roll_at = self.clock.now() - 600
            second = TickOutcome(session_id=SESSION)
            await self.engine._maybe_run_event(state, node, second)
            self.assertEqual(
                len(state.event_threads), 1, second.notes or "过了间隔就该照常掷骰"
            )

    async def test_event_overview_carries_steps_and_buttons(self):
        """状态页要的数据：每一幕的细节 + 这一条能不能推进 / 完结。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = '{"pick": 1, "reason": "继续"}'
        self.helper.default_reply = (
            '{"outcome": "手被划了一下", "followup": "得去找个创可贴",'
            ' "ability_delta": {"dexterity": 0.02}}'
        )
        await self.start_event()
        async with self.engine.session_state(SESSION) as state:
            state.event_threads[0]["next_step_at"] = self.clock.now() + 3600

        data = await self.engine.event_overview(SESSION)

        thread = data["threads"][0]
        self.assertEqual(thread["status"], "open")
        self.assertTrue(thread["suspended"], "下一步要等一小时，算挂起")
        self.assertTrue(thread["can_advance"])
        self.assertTrue(thread["can_close"])
        self.assertTrue(thread["steps"])
        step = thread["steps"][0]
        self.assertTrue(step["desc"])
        self.assertTrue(step["result"])
        self.assertIn("tier_label", step)
        self.assertIn("灵巧", step["ability_delta"])

    async def test_persona_brief_is_cached_by_persona(self):
        """简易人设按人格缓存：生成一次就存下来，读的时候直接拿到。"""

        self.assertNotEqual(
            self.engine._persona_brief_key("人设 A"),
            self.engine._persona_brief_key("人设 B"),
        )
        self.creator.replies = ["一只又懒又爱面子的鲸鱼，自称本小姐，说话短促爱吐槽。"]
        brief = await self.engine.generate_persona_brief(SESSION)
        self.assertIn("本小姐", brief)
        state = await self.engine.persona_brief_state(SESSION)
        self.assertEqual(state["brief"], brief)
        self.assertEqual(state["source"], "generated")
        self.assertEqual(await self.engine.event_persona_brief(SESSION), brief)

    async def test_persona_brief_can_be_generated_from_an_unsaved_draft(self):
        """向导里刚写完、还没保存的角色卡，生成时要优先用它。

        不传草稿的话后端读的是配置里那份旧的，生成的摘要跟眼前这份人设对不上；
        而且这份摘要要按**当时用的那份人设**存，等草稿保存成正式人设之后才对得上指纹。
        """

        draft = "这是一份刚在向导里写的人设：一只爱睡觉的猫，白天爱晒太阳。"
        self.creator.replies = ["爱睡觉的猫，白天晒太阳，说话慢吞吞。"]
        brief = await self.engine.generate_persona_brief(SESSION, persona_text=draft)
        self.assertTrue(brief)
        sent = " ".join(str(value) for value in self.creator.calls[-1].values())
        self.assertIn("爱睡觉的猫", sent)
        # 用草稿生成 → 按草稿的指纹存；把草稿存成正式人设之后能直接读到
        key = self.engine._persona_brief_key(draft)
        stored = await self.db.call("kv_get", key)
        self.assertEqual(str((stored or {}).get("brief") or ""), brief)
        # 不传草稿时仍然读配置里那份人设
        self.creator.replies = ["按配置里那份生成。"]
        await self.engine.generate_persona_brief(SESSION)
        fallback = " ".join(str(value) for value in self.creator.calls[-1].values())
        self.assertIn("本小姐", fallback)

    async def test_abilities_line_uses_human_words(self):
        state = await self.get_state()
        line = self.engine.abilities_line(state)
        self.assertIn("体力", line)
        self.assertNotIn("0.6", line)

    async def test_pending_event_line_depends_on_stage(self):
        state = await self.get_state()
        self.assertEqual(self.engine.pending_event_line(state), "")
        state.pending_help = {"state": "active", "title": "打不开的盒子"}
        self.assertIn("等", self.engine.pending_event_line(state))
        state.pending_help = {"state": "idle", "title": "打不开的盒子"}
        self.assertIn("不催", self.engine.pending_event_line(state))

    async def test_event_overview_reports_abilities_and_threads(self):
        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = '{"pick": 1, "reason": "上"}'
        self.helper.default_reply = '{"outcome": "盒子开了"}'
        await self.start_event()
        overview = await self.engine.event_overview(SESSION)
        self.assertEqual(len(overview["abilities"]), 4)
        self.assertTrue(overview["threads"])
        self.assertIn("steps", overview["threads"][0])

    async def test_generation_failure_starts_no_event(self):
        """生成模型给不出可用的事件包时，这一轮就当没出事——不塞一条假事件顶上。"""

        self.creator.default_reply = "我觉得今天挺平静的。"
        before = await self.get_state()
        self.assertEqual(len(before.event_threads), 0)
        await self.start_event()
        state = await self.get_state()
        self.assertEqual(state.event_threads, [])
        self.assertIn("event_skip", await self.event_types())

    async def test_every_event_comes_from_the_generation_model(self):
        """没有任何内置池：事件一律由生成模型现编，题材由代码按权重掷。"""

        self.use_pool_only(TEST_POOL_EVENT)
        await self.start_event()
        self.assertTrue(self.creator.calls)
        system = self.creator.calls[0]["system_prompt"]
        self.assertIn("题材", system)

    # ---------------- 同一时间只能有一件事 ----------------

    async def test_only_one_event_at_a_time(self):
        """有没完的事就不再开新的；用户投递也会被明确拒绝。"""

        self.use_pool_only(TEST_POOL_EVENT)
        # 让这件事留个伏笔：演完还是 open 的
        self.main.default_reply = '{"pick": 1, "reason": "继续"}'
        self.helper.default_reply = '{"outcome": "事情还没完", "followup": "得再来一次"}'
        await self.start_event()
        state = await self.get_state()
        self.assertIsNotNone(self.engine._active_thread(state))

        # 自动触发：即使掷中了也不再开新的
        outcome = TickOutcome(session_id=SESSION)
        before = len(state.event_threads)
        async with self.engine.session_state(SESSION) as live:
            await self.engine._start_event(live, self.engine.node("study"), outcome, tier="small")
        state = await self.get_state()
        self.assertEqual(len(state.event_threads), before)

        # 用户投递：直接拒绝，并说明手上那件事
        note = await self.engine.submit_event(SESSION, "出门忘了带伞")
        self.assertIn("没完", note)
        state = await self.get_state()
        self.assertEqual(len(state.event_threads), before)

    # ---------------- 题材由代码掷 ----------------

    async def test_genre_is_rolled_by_weight_and_avoids_recent(self):
        """题材由代码按权重抽，最近用过的先排除——不是让模型自己分配比例。"""

        raw = self.store.raw_world()
        raw["events"]["genres"] = [
            {"name": "日常小事", "weight": 1, "examples": "做饭"},
            {"name": "人际", "weight": 1, "examples": "被人跟着"},
        ]
        raw["events"]["genre_recency"] = 1
        self.store.save_world(raw)
        self.engine.reload_config()
        state = await self.get_state()

        first = self.engine._pick_genre(state)
        self.assertIn(first["name"], ("日常小事", "人际"))
        state.event_recent_genres = [first["name"]]
        second = self.engine._pick_genre(state)
        self.assertNotEqual(second["name"], first["name"])

        # 权重 0 的题材不会被抽到
        raw = self.store.raw_world()
        raw["events"]["genres"] = [
            {"name": "零权重", "weight": 0, "examples": ""},
            {"name": "唯一的", "weight": 1, "examples": ""},
        ]
        raw["events"]["genre_recency"] = 0
        self.store.save_world(raw)
        self.engine.reload_config()
        state = await self.get_state()
        self.assertEqual(self.engine._pick_genre(state)["name"], "唯一的")

    async def test_picked_genre_goes_into_the_prompt_and_the_log(self):
        """抽中的题材会写进生成提示词，也会记进线索和日志。"""

        raw = self.store.raw_world()
        raw["events"]["generate"] = True
        raw["events"]["generate_small"] = 1.0
        raw["events"]["genres"] = [
            {"name": "人际", "weight": 1, "examples": "被人跟着、被搭讪"}
        ]
        raw["events"]["genre_recency"] = 0
        self.store.save_world(raw)
        self.engine.reload_config()
        self.creator.replies = [
            '{"title": "有人一直跟着", "hook": "她发现有人跟了她三条街",'
            ' "critical": true, "options": [{"desc": "往人多的地方走", "abilities": ["wits"]},'
            ' {"desc": "先回家", "no_check": true}]}'
        ]
        self.main.replies = ['{"pick": 2, "reason": "怂"}']
        self.helper.replies = ['{"outcome": "她回家了"}']

        await self.start_event()

        self.assertTrue(self.creator.calls, "应该走临场生成")
        prompt = self.creator.calls[0]["system_prompt"]
        self.assertIn("人际", prompt)
        self.assertIn("被人跟着", prompt)
        state = await self.get_state()
        self.assertEqual(state.event_recent_genres[-1], "人际")
        self.assertEqual(state.event_threads[0]["genre"], "人际")
        self.assertTrue(self.engine._thread_critical(state.event_threads[0]))
        events = await self.events()
        hit = [item for item in events if item["event_type"] == "event"][0]
        self.assertEqual(hit["detail"]["genre"], "人际")
        self.assertTrue(hit["detail"]["critical"])

    # ---------------- 日程闸门 ----------------

    async def test_schedule_gate_ignores_light_schedules(self):
        """伸懒腰、看书这类日程不查闸门，照常执行（也不调模型）。"""

        self.add_schedule(
            schedule_id="light_one",
            time="12:00",
            actions=[{"type": "say", "content": "伸个懒腰"}],
        )
        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        self.main.replies = ['{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}']
        await self.start_event()
        schedule = self.schedule_by_id("light_one")

        async with self.engine.session_state(SESSION) as state:
            before = len(self.main.calls)
            choice, _note = await self.engine._schedule_gate(state, schedule, label="light_one")

        self.assertEqual(choice, "do")
        self.assertEqual(len(self.main.calls), before)

    async def test_schedule_gate_delays_sleep_for_a_dangerous_event(self):
        """危险事件撞上睡觉：直接推迟，不问模型（"被跟踪时不能回卧室睡"）。"""

        await self.group_says()
        self.add_schedule(
            schedule_id="night_sleep",
            time="23:30",
            actions=[{"type": "walk_to", "target_node": "bedroom"}, {"type": "sleep", "duration": 60}],
        )
        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene", "critical": True})
        self.main.replies = ['{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}']
        await self.start_event()
        schedule = self.schedule_by_id("night_sleep")

        async with self.engine.session_state(SESSION) as state:
            before = len(self.main.calls)
            choice, note = await self.engine._schedule_gate(state, schedule, label="night_sleep")

        self.assertEqual(choice, "delay")
        self.assertIn("危险", note)
        self.assertEqual(len(self.main.calls), before, "危险事件不该去问模型")

    async def test_schedule_gate_asks_the_model_then_delays(self):
        """普通事件撞上睡觉：问模型，它说推迟就推迟，并记下熬夜时间。"""

        await self.group_says()
        self.add_schedule(
            schedule_id="night_sleep",
            time="23:30",
            actions=[{"type": "sleep", "duration": 60}],
        )
        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        self.main.replies = ['{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}']
        await self.start_event()
        self.main.replies = ['{"choice": "delay", "reason": "先把这件事弄完"}']
        schedule = self.schedule_by_id("night_sleep")

        async with self.engine.session_state(SESSION) as state:
            choice, note = await self.engine._schedule_gate(state, schedule, label="night_sleep")

        self.assertEqual(choice, "delay")
        self.assertIn("先处理", note)
        # 睡觉被推迟 = 熬夜：这段时间精力掉得更快
        self._delay_schedule_via_engine(state, schedule)
        self.assertGreater(int(state.stay_up_until), int(state.world_time))

    def _delay_schedule_via_engine(self, state, schedule) -> None:
        day_key = self.engine.local_now().strftime("%Y-%m-%d")
        self.engine._delay_schedule(state, schedule, day_key, "23:30")

    async def test_schedule_delay_hits_the_limit(self):
        """推迟到达上限后不再让路，按日程执行。"""

        await self.group_says()
        self.add_schedule(
            schedule_id="night_sleep", time="23:30", actions=[{"type": "sleep", "duration": 60}]
        )
        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene", "critical": True})
        self.main.replies = ['{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}']
        await self.start_event()
        schedule = self.schedule_by_id("night_sleep")
        day_key = self.engine.local_now().strftime("%Y-%m-%d")

        async with self.engine.session_state(SESSION) as state:
            state.schedule_delays = {
                "night_sleep": {
                    "day": day_key,
                    "count": int(self.engine.world.events.schedule_delay_limit_times),
                    "minutes": 0.0,
                    "until": 0.0,
                    "slot": "23:30",
                }
            }
            choice, note = await self.engine._schedule_gate(state, schedule, label="night_sleep")

        self.assertEqual(choice, "do")
        self.assertIn("上限", note)

    async def test_schedule_gate_falls_back_without_a_model(self):
        """没模型可用：睡觉类兜底推迟，换地方照做。"""

        await self.group_says()
        self.add_schedule(
            schedule_id="night_sleep", time="23:30", actions=[{"type": "sleep", "duration": 60}]
        )
        self.add_schedule(
            schedule_id="go_study", time="12:00", actions=[{"type": "walk_to", "target_node": "study"}]
        )
        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        self.main.replies = ['{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}']
        await self.start_event()
        self.engine.llm = None

        async with self.engine.session_state(SESSION) as state:
            sleep_choice, _ = await self.engine._schedule_gate(
                state, self.schedule_by_id("night_sleep"), label="night_sleep"
            )
            move_choice, _ = await self.engine._schedule_gate(
                state, self.schedule_by_id("go_study"), label="go_study"
            )

        self.assertEqual(sleep_choice, "delay")
        self.assertEqual(move_choice, "do")

    async def test_delayed_schedule_is_retried_later(self):
        """推迟之后到点会再检查一次：不执行、也不写"已跑过"。"""

        await self.group_says()
        self.add_schedule(
            schedule_id="night_sleep", time="23:30", actions=[{"type": "sleep", "duration": 60}]
        )
        self.clock.set_struct(datetime(2026, 9, 10, 23, 30))
        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene", "critical": True})
        self.main.replies = ['{"pick": 1, "ask_help": true, "say": {"plan": [{"text": "有人吗"}]}}']
        await self.start_event()

        outcomes = await self.engine.run_schedules()
        state = await self.get_state()
        self.assertTrue(state.schedule_delays.get("night_sleep"))
        self.assertIsNone(state.current_action)
        kinds = [item["event_type"] for item in await self.events()]
        self.assertIn("skip", kinds)
        self.assertEqual(outcomes, [])

    # ---------------- 事件里的发言条数 ----------------

    async def test_event_ask_uses_the_event_say_limit(self):
        """求助可以分几条发（事件上限，默认 4），不再被普通聊天的 2 条卡住。"""

        await self.group_says()
        self.use_pool_only({**TEST_POOL_EVENT, "kind": "need_intervene"})
        self.main.replies = [
            '{"pick": 1, "ask_help": true, "say": {"plan": ['
            '{"text": "有人吗"}, {"text": "本小姐遇到点事"},'
            ' {"text": "就是桌上这个盒子"}, {"text": "你们说我该怎么办"},'
            ' {"text": "第五句不该发出去"}]}}'
        ]
        outcome = await self.start_event()
        self.assertEqual(len(outcome.messages), 4, outcome.messages)
        self.assertNotIn("第五句", " ".join(outcome.messages))

    async def test_event_result_keeps_at_most_two_lines(self):
        """结果那几句收紧到 2 条（事件上限只管求助那种要分条发的场合）。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.replies = [
            '{"pick": 1, "say": {"success": [{"text": "第一句"}, {"text": "第二句"},'
            ' {"text": "第三句"}]}}'
        ]
        self.helper.replies = ['{"outcome": "盒子开了"}']
        outcome = await self.start_event()
        self.assertEqual(len(outcome.messages), 2, outcome.messages)

    async def test_event_say_limit_hard_cap(self):
        """硬顶拦住刷屏：把上限配大也不会超过 max_say_lines_hard。"""

        raw = self.store.raw_world()
        raw["events"]["max_say_lines"] = 99
        raw["events"]["max_say_lines_hard"] = 6
        self.store.save_world(raw)
        self.engine.reload_config()
        self.assertEqual(self.engine._event_say_limit("ask"), 6)
        self.assertEqual(self.engine._event_say_limit("result"), 2)
        self.assertEqual(self.engine._event_say_limit("beat"), 1)

    # ---------------- 合并重启：连发两条只回一次 ----------------

    async def test_two_messages_merge_into_one_reply(self):
        """她还在调模型时又来一条：并成一次请求重新发起，第二条不再单独回。"""

        raw = self.store.raw_world()
        raw["reply_style"]["interrupt_pending"] = False  # 这一档要的是"合并"
        self.store.save_world(raw)
        self.engine.reload_config()
        self.main.replies = [SAY_REPLY, SAY_REPLY]
        holder: dict[str, Any] = {}

        def hook(_index: int) -> None:
            holder["second"] = self.engine.register_incoming(SESSION, "还有一句话")

        self.main.on_call = hook
        reply = await self.engine.handle_reply(
            MessageContext(
                session_id=SESSION,
                user_id="1",
                user_name="小明",
                text="第一句",
                is_wake=True,
                is_mentioned=True,
            )
        )

        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(len(self.main.calls), 2, "应该合并后重新发起一次")
        merged_prompt = self.main.calls[-1]["prompt"]
        self.assertIn("第一句", merged_prompt)
        self.assertIn("还有一句话", merged_prompt)
        self.assertTrue(self.engine.was_absorbed(holder["second"]))

    async def test_late_message_is_not_merged(self):
        """窗口之外来的新消息不合并：她照常各回各的（宁可多说一句）。"""

        from core import engine as engine_module

        raw = self.store.raw_world()
        raw["reply_style"]["interrupt_pending"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        original = engine_module.MERGE_WINDOW_SECONDS
        engine_module.MERGE_WINDOW_SECONDS = -1.0  # 任何新消息都算"来晚了"
        self.addCleanup(setattr, engine_module, "MERGE_WINDOW_SECONDS", original)
        self.main.replies = [SAY_REPLY]
        holder: dict[str, Any] = {}
        self.main.on_call = lambda _index: holder.update(
            {"second": self.engine.register_incoming(SESSION, "晚来的那句")}
        )

        reply = await self.engine.handle_reply(
            MessageContext(
                session_id=SESSION,
                user_id="1",
                user_name="小明",
                text="第一句",
                is_wake=True,
                is_mentioned=True,
            )
        )

        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(len(self.main.calls), 1, "窗口外不该重新发起")
        self.assertFalse(self.engine.was_absorbed(holder["second"]))

    async def test_new_message_interrupts_the_pending_reply(self):
        """默认（打断）：正在生成时又来一条，这一次生成作废，交给新那条重新触发。"""

        self.main.replies = [SAY_REPLY, SAY_REPLY]
        holder: dict[str, Any] = {}

        def hook(_index: int) -> None:
            holder["second"] = self.engine.register_incoming(SESSION, "还有一句话")

        self.main.on_call = hook
        reply = await self.engine.handle_reply(
            MessageContext(
                session_id=SESSION,
                user_id="1",
                user_name="小明",
                text="第一句",
                is_wake=True,
                is_mentioned=True,
            )
        )

        self.assertFalse(reply.ok)
        self.assertIn("打断", reply.error)
        self.assertEqual(len(self.main.calls), 1, "被打断的那次不再重新发起")
        # 新那条没有被吃掉：它自己的那一轮照常回
        self.assertFalse(self.engine.was_absorbed(holder["second"]))

    # ---------------- 指令不进聊天记录 ----------------

    async def test_plugin_commands_stay_out_of_the_chat_log(self):
        """`/vw …` 是管理入口，不是"有人在跟她讲话"：不能进她的聊天记录。"""

        self.assertTrue(self.engine.is_plugin_command("/vw event 逛街被跟了"))
        self.assertTrue(self.engine.is_plugin_command("／VW ability"))
        # AstrBot 派发指令时会把开头的斜杠吃掉：她那边看到的是「vw event h」，
        # 这一条也得认出来，不然指令会被记成"他刚说的话"
        self.assertTrue(self.engine.is_plugin_command("vw event h"))
        self.assertTrue(self.engine.is_plugin_command("VW 状态"))
        self.assertFalse(self.engine.is_plugin_command("/签到"))
        self.assertFalse(self.engine.is_plugin_command("在吗"))
        # 「vw」后面不是子命令的，还是当成普通聊天
        self.assertFalse(self.engine.is_plugin_command("vw 是什么意思"))
        # 起事件的那条指令单独认（回复时换成一句世界内的话，别让她复述指令）
        self.assertTrue(self.engine.is_event_command("/vw event h"))
        self.assertTrue(self.engine.is_event_command("vw event 逛街被跟了"))
        self.assertFalse(self.engine.is_event_command("/vw debug state"))
        self.assertFalse(self.engine.is_event_command("在吗"))

        await self.engine.handle_incoming(
            MessageContext(
                session_id=SESSION,
                user_id="1",
                user_name="主人",
                text="/vw event 逛街被跟了",
                is_wake=True,
                is_mentioned=True,
            )
        )
        state = await self.get_state()
        self.assertEqual(state.recent_chat, [], "指令不该出现在她的聊天记录里")

        await self.engine.handle_incoming(
            MessageContext(
                session_id=SESSION,
                user_id="1",
                user_name="主人",
                text="在吗",
                is_wake=True,
                is_mentioned=True,
            )
        )
        state = await self.get_state()
        self.assertEqual([item["text"] for item in state.recent_chat], ["在吗"])

    # ---------------- 记忆记在事件发生的地点 ----------------

    async def test_event_memory_uses_the_event_place_not_her_position(self):
        """逛街被跟这件事发生在"外面"，不能记进她物理所在的窗边。"""

        raw = self.store.raw_world()
        raw["events"]["generate"] = True
        raw["events"]["generate_small"] = 1.0
        raw["events"]["genres"] = []
        self.store.save_world(raw)
        self.engine.reload_config()
        self.creator.replies = [
            '{"title": "有人跟着", "hook": "有人跟了她三条街", "place_name": "商业街",'
            ' "options": [{"desc": "往人多的地方走", "abilities": ["wits"]},'
            ' {"desc": "先回家", "no_check": true}]}'
        ]
        self.main.replies = ['{"pick": 1}']
        self.helper.replies = [
            '{"outcome": "她进了商场", "memory": "我逛街时被人跟了三条街，后来躲进商场"}'
        ]
        await self.set_state(node_id="window")

        await self.start_event()

        rows = self.engine.memory.db.query_memories(session_id=SESSION, limit=10)
        event_rows = [item for item in rows if "商场" in str(item.get("content") or "")]
        self.assertTrue(event_rows, rows)
        self.assertEqual(event_rows[0]["node_id"], "outside:商业街")
        self.assertIn("商业街", str(event_rows[0]["content"]))
        # 她本人还在窗边——事件没有把她搬走
        state = await self.get_state()
        self.assertEqual(state.node_id, "window")

    async def test_pool_event_memory_uses_the_events_place(self):
        """池子里写「在商业街发生」的事件：记忆记在商业街，不是她此刻站的地方。"""

        self.use_pool_only(
            {
                "id": "street_only",
                "tier": "small",
                "kind": "solo",
                "title": "有人跟着",
                "hook": "有人跟了她三条街",
                "place_name": "商业街",
                "difficulty": 0.5,
                "options": [
                    {"desc": "往人多的地方走", "abilities": ["wits"]},
                    {"desc": "先回家", "no_check": True},
                ],
            }
        )
        self.main.replies = ['{"pick": 1}']
        self.helper.replies = [
            '{"outcome": "她进了商场", "memory": "我在商业街被人跟了三条街，后来躲进商场"}'
        ]
        await self.set_state(node_id="study")

        await self.start_event()

        rows = self.engine.memory.db.query_memories(session_id=SESSION, limit=10)
        event_rows = [item for item in rows if "商场" in str(item.get("content") or "")]
        self.assertTrue(event_rows, rows)
        self.assertEqual(event_rows[0]["node_id"], "outside:商业街")
        self.assertIn("商业街", str(event_rows[0]["content"]))
        # 她本人还在书房——事件没有把她搬走
        state = await self.get_state()
        self.assertEqual(state.node_id, "study")

    async def test_event_generation_is_anchored_to_where_she_is(self):
        """事件是临场编的：生成提示词里必须带着她此刻所在的地点，不能凭空挑一条。"""

        self.use_pool_only(
            {
                "id": "kitchen_only",
                "tier": "small",
                "kind": "solo",
                "title": "锅盖卡死",
                "hook": "锅里蒸汽往外顶",
                "place": "kitchen",
                "place_name": "厨房",
                "difficulty": 0.5,
                "options": [
                    {"desc": "硬掰", "abilities": ["stamina"]},
                    {"desc": "先放着", "no_check": True},
                ],
            }
        )
        self.main.replies = ['{"pick": 1}']
        self.helper.replies = [
            '{"outcome": "锅盖弹开了", "memory": "我在厨房把卡死的锅盖掰开了"}'
        ]
        await self.set_state(node_id="kitchen")

        await self.start_event()

        scene = self.creator.calls[0]["prompt"]
        self.assertIn("厨房", scene)
        rows = self.engine.memory.db.query_memories(session_id=SESSION, limit=10)
        event_rows = [item for item in rows if "锅盖" in str(item.get("content") or "")]
        self.assertTrue(event_rows, rows)
        self.assertEqual(event_rows[0]["node_id"], "kitchen")
        self.assertIn("厨房", str(event_rows[0]["content"]))

    # ---------------- 她自己的账（三段） ----------------

    async def advance_thread(self) -> TickOutcome:
        """手工把这条线索往前推一幕（跳过"到点没到点"的等待）。"""

        outcome = TickOutcome(session_id=SESSION)
        async with self.engine.session_state(SESSION) as state:
            thread = self.engine._active_thread(state)
            if thread is not None:
                # 续演那条路只认"到点没到点"：把它挪到此刻
                thread["next_step_at"] = float(self.clock.now())
            node = self.engine.node(state.node_id)
            await self.engine._continue_thread(
                state, node, outcome, self.clock.now()
            )
        self.engine.messenger.sent.extend(outcome.messages)
        return outcome

    async def test_small_event_only_speaks_at_opening_and_closing(self):
        """小事件默认「只在开场和收尾说」：中间那些幕静默推演，不刷屏。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = (
            '{"pick": 1, "reason": "上",'
            ' "say": {"success": [{"text": "本小姐先试一下"}],'
            '         "fail": [{"text": "……手疼"}]}}'
        )
        # 前两幕都留伏笔（第二幕是"中间"，该闭嘴），最后一幕收尾
        self.helper.replies = [
            '{"outcome": "先掰了一下", "followup": "还没开，得换个办法",'
            ' "ability_delta": {"wits": 0.01}}',
            '{"outcome": "换了个办法", "followup": "还差最后一下"}',
            '{"outcome": "终于开了"}',
        ]
        first = await self.start_event()

        # 台词在第 1 幕就说过；后面两幕的 say 由 assistant 自己写，这里只关心发不发
        self.assertTrue(first.messages)
        second = await self.advance_thread()
        self.assertEqual(second.messages, [], "中间那一幕不该说话")
        third = await self.advance_thread()
        self.assertTrue(third.messages, "收尾那一幕要说话")

    async def test_big_event_cannot_finish_in_one_step(self):
        """大事件最少演两幕：模型忘了留伏笔，代码也会补上下一步。"""

        self.use_pool_only(TEST_POOL_EVENT)
        raw = self.store.raw_world()
        raw["events"]["big_min_steps"] = 2
        self.store.save_world(raw)
        self.engine.reload_config()
        self.main.default_reply = '{"pick": 1, "reason": "上"}'
        self.helper.default_reply = '{"outcome": "事情办完了"}'
        await self.start_event(tier="big")

        state = await self.get_state()
        thread = state.event_threads[-1]
        self.assertEqual(thread["status"], "open")
        self.assertTrue(thread["pending_followup"])

    async def test_micro_event_never_speaks_but_still_lands_in_her_journal(self):
        """微事件只记账、不出声：群里的观感靠别的动作，不靠一句没头没尾的话。"""

        self.use_pool_only(
            {
                "id": "micro_one",
                "tier": "micro",
                "kind": "solo",
                "title": "水开了",
                "hook": "壶盖被顶得咕嘟响",
                "outcome": "她关火的时候被热气扑了一下",
            }
        )
        outcome = await self.start_event(tier="micro")
        self.assertEqual(outcome.messages, [])
        state = await self.get_state()
        self.assertTrue(any("水开了" in item for item in state.event_digest))

    async def test_event_result_is_written_into_the_chat_log(self):
        """收尾那一幕的结果写进留档（标成"她自己身上发生的事"），且不算未回消息。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = '{"pick": 1, "reason": "上"}'
        self.helper.default_reply = '{"outcome": "盖子被掰开了"}'
        await self.start_event()

        state = await self.get_state()
        rows = [item for item in state.recent_chat if item.get("internal")]
        self.assertTrue(rows, state.recent_chat)
        self.assertIn("打不开的盒子", rows[-1]["text"])
        # 不是"等着被回"的消息
        self.assertFalse(
            chat_item_is_fresh(
                rows[-1],
                replied_until=0.0,
                replied_seq=int(state.chat_replied_seq or 0),
            )
        )

    async def test_genre_cools_down_after_use(self):
        """同一个题材用完之后要过冷却期，不能隔一件就又抽中。"""

        self.use_pool_only(TEST_POOL_EVENT)
        self.main.default_reply = '{"pick": 1, "reason": "上"}'
        self.helper.default_reply = '{"outcome": "办完了"}'
        raw = self.store.raw_world()
        raw["events"]["genres"] = [
            {"name": "只有这一类", "weight": 1, "examples": "随便"},
        ]
        raw["events"]["genre_cooldown_minutes"] = 180
        raw["events"]["genre_recency"] = 0
        self.store.save_world(raw)
        self.engine.reload_config()

        await self.start_event()
        state = await self.get_state()
        self.assertEqual(state.event_genre_at.get("只有这一类"), self.clock.now())

        # 冷却期内：没有别的题材可挑，只好退回全量池（不会崩）
        self.assertIsNotNone(self.engine._pick_genre(state))

    def _fake_thread(self, **overrides) -> dict:
        thread = {
            "id": "t1",
            "title": "一件事",
            "tier": "small",
            "status": "closed",
            "opened_at": 1_700_000_000.0,
            "updated_at": 1_700_000_100.0,
            "closed_at": 1_700_000_100.0,
            "place": "kitchen",
            "place_name": "厨房",
            "line": "锅盖卡死，我硬掰被蒸汽烫了手背",
            "root": {"title": "锅盖卡死", "hook": "锅里蒸汽往外顶"},
            "steps": [],
            "pending_followup": "",
            "next_step_at": 0.0,
        }
        thread.update(overrides)
        return thread

    async def test_event_journal_has_three_sections(self):
        """她自己的账：正在经历 / 最近发生在我身上的事 / 还挂着的。"""

        now = self.clock.now()
        await self.set_state(node_id="study")
        async with self.engine.session_state(SESSION) as state:
            state.event_threads = [
                self._fake_thread(
                    id="done1",
                    title="锅盖卡死",
                    updated_at=now - 900,
                    closed_at=now - 900,
                    opened_at=now - 1200,
                    line="锅盖卡死，我硬掰被蒸汽烫了手背",
                ),
                self._fake_thread(
                    id="done2",
                    title="窗台来了只猫",
                    tier="micro",
                    place="window",
                    place_name="窗边",
                    updated_at=now - 300,
                    closed_at=now - 300,
                    opened_at=now - 400,
                    line="我拿小鱼干把猫引到门口",
                ),
                self._fake_thread(
                    id="open1",
                    title="逛街被跟",
                    tier="big",
                    status="open",
                    place="outside:商业街",
                    place_name="商业街",
                    opened_at=now - 600,
                    updated_at=now - 60,
                    closed_at=0.0,
                    line="我往商场走，他停在门口",
                    pending_followup="还没弄明白他想干什么",
                    next_step_at=now + 40,
                    steps=[{"desc": "往人多的地方走", "tier": "success", "result": "进了商场"}],
                ),
            ]
            text = self.engine.event_journal(state, now)

        self.assertIn("# 我正在经历", text)
        self.assertIn("逛街被跟", text)
        self.assertIn("# 最近发生在我身上的事", text)
        self.assertIn("锅盖卡死", text)
        self.assertIn("窗台来了只猫", text)
        self.assertIn("今天", text)  # 每条带时间

    async def test_suspended_thread_leaves_the_in_progress_section(self):
        """下一步要等很久的算挂起：不占「我正在经历」，只留一行轻提示。"""

        now = self.clock.now()
        await self.set_state(node_id="study")
        async with self.engine.session_state(SESSION) as state:
            state.event_threads = [
                self._fake_thread(
                    id="open1",
                    title="逛街被跟",
                    status="open",
                    place="outside:商业街",
                    place_name="商业街",
                    opened_at=now - 600,
                    updated_at=now - 300,
                    closed_at=0.0,
                    pending_followup="还得再去看看",
                    next_step_at=now + 3 * 3600,  # 三小时后才续
                    steps=[],
                )
            ]
            text = self.engine.event_journal(state, now)

        self.assertNotIn("# 我正在经历", text)
        self.assertIn("# 有件事我心里还挂着", text)
        self.assertIn("商业街", text)

    async def test_recent_lines_drop_micro_events_first(self):
        """超过条数上限时：先顶掉微事件，再顶最旧的。"""

        now = self.clock.now()
        await self.set_state(node_id="study")
        raw = self.store.raw_world()
        raw["events"]["recent_max_lines"] = 2
        self.store.save_world(raw)
        self.engine.reload_config()
        threads = [
            self._fake_thread(
                id="micro_old",
                title="微事件甲",
                tier="micro",
                updated_at=now - 900,
                closed_at=now - 900,
                opened_at=now - 900,
                line="甲",
            ),
            self._fake_thread(
                id="big_old",
                title="大事乙",
                tier="big",
                updated_at=now - 800,
                closed_at=now - 800,
                opened_at=now - 800,
                line="乙",
            ),
            self._fake_thread(
                id="small_new",
                title="小事丙",
                updated_at=now - 100,
                closed_at=now - 100,
                opened_at=now - 100,
                line="丙",
            ),
        ]
        async with self.engine.session_state(SESSION) as state:
            state.event_threads = threads
            text = self.engine.event_journal(state, now)

        self.assertNotIn("甲", text, "先被顶掉的应该是微事件")
        self.assertIn("乙", text)
        self.assertIn("丙", text)

    async def test_stale_thread_is_closed_instead_of_resumed(self):
        """挂太久的事件不再续演：直接收尾并写一条"那件事后来没再提"。"""

        now = self.clock.now()
        await self.set_state(node_id="study")
        self.use_pool_only(TEST_POOL_EVENT)
        async with self.engine.session_state(SESSION) as state:
            state.event_threads = [
                self._fake_thread(
                    id="stale",
                    title="逛街被跟",
                    tier="big",
                    status="open",
                    place="outside:商业街",
                    place_name="商业街",
                    opened_at=now - 48 * 3600,
                    updated_at=now - 48 * 3600,
                    closed_at=0.0,
                    line="我往商场走",
                    pending_followup="还得再去看看",
                    next_step_at=now - 60,  # 早就该续了
                    steps=[],
                )
            ]
        outcome = TickOutcome(session_id=SESSION)
        async with self.engine.session_state(SESSION) as state:
            resumed = await self.engine._continue_thread(
                state, self.engine.node("study"), outcome, now
            )
            thread = state.event_threads[0]
            self.assertFalse(resumed)
            self.assertEqual(thread["status"], "closed")
            self.assertEqual(thread["pending_followup"], "")
        rows = self.engine.memory.db.query_memories(session_id=SESSION, limit=10)
        self.assertTrue(
            [item for item in rows if "没再提" in str(item.get("content") or "")], rows
        )
        self.assertIn("event_idle", await self.event_types())

    async def test_resume_from_another_place_is_marked_as_recap(self):
        """续演时她人不在事件发生的地方 → 这一幕按"想起来"演，不搬场景。"""

        thread = self._fake_thread(id="x", status="open", place="outside:商业街", place_name="商业街")
        async with self.engine.session_state(SESSION) as state:
            state.node_id = "study"
            self.assertTrue(self.engine._needs_recap(state, thread))
        thread2 = self._fake_thread(id="y", status="open", place="kitchen", place_name="厨房")
        async with self.engine.session_state(SESSION) as state:
            state.node_id = "study"
            self.assertTrue(self.engine._needs_recap(state, thread2))
        async with self.engine.session_state(SESSION) as state:
            state.node_id = "kitchen"
            self.assertFalse(self.engine._needs_recap(state, thread2))


if __name__ == "__main__":
    unittest.main()
