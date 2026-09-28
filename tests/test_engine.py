"""引擎集成测试（seam S1~S4）：不依赖 AstrBot，用测试替身驱动整个状态机。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import ConfigStore  # noqa: E402
from core.db import AsyncDatabase  # noqa: E402
from core.engine import (  # noqa: E402
    MessageContext,
    TickOutcome,
    VirtualWorldEngine,
    _guess_duration_seconds,
)
from core.json_actions import PlannedAction  # noqa: E402
from core.models import parse_world  # noqa: E402
from core.state import WorldState, chat_item_is_fresh, group_chat_items  # noqa: E402
from tests.stub_ports import (  # noqa: E402
    StubClock,
    StubLLM,
    StubMessenger,
    StubPersona,
    StubTools,
)

SESSION = "aiocqhttp:GroupMessage:1001"
OTHER_SESSION = "aiocqhttp:GroupMessage:2002"
PRIVATE_SESSION = "aiocqhttp:PrivateMessage:2692047521"
SAY_REPLY = '{"actions":[{"type":"say","messages":["有人在吗？"]}]}'


class _NoLlmDraw:
    """骰子固定成"不抽中"：这一轮一定走规则决策，不额外问大模型。"""

    def random(self) -> float:
        return 1.0


class _RecordingCommands:
    """会记账的指令通道：用来确认同一条指令没有被执行两遍。"""

    def __init__(self, calls: list[str], result) -> None:
        self.calls = calls
        self.result = result

    async def trigger(self, session_id, command, *, event=None):
        self.calls.append(command)
        return self.result


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

        # 内置的搜索 / 查天气动作不再自带默认工具（现在由用户自己挑）。
        # 测试里显式配一个，等同于"真实用户已经配好"的状态；
        # 专门测"没配工具会被跳过"的用例会自己把它清掉。
        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "search_web":
                action["tool_names"] = ["web_search"]
            elif action["id"] == "check_weather":
                action["tool_names"] = ["get_current_weather", "get_weather", "weather"]
        self.store.save_world(raw)
        self.engine.reload_config()

    # ---------------- 工具 ----------------

    # ---------------- 会话组 ----------------

    def set_session_note(self, session_id: str, note: str) -> None:
        """给会话改备注（编辑器里那个字段）。"""

        raw = self.store.raw_sessions()
        for item in raw.get("sessions") or []:
            if str(item.get("session_id")) == str(session_id):
                item["note"] = note
        self.store.save_sessions(raw)
        self.engine.reload_config()

    def add_group(self, *, group_id="team", sessions=None, main="", name=""):
        """把两个会话编成一组（另一个会话顺手加进白名单）。"""

        members = list(sessions or [SESSION, OTHER_SESSION])
        for item in members:
            if item == SESSION:
                continue
            self.store.add_session(
                item,
                session_type="private" if "Private" in item else "group",
                platform=item.split(":", 1)[0],
                note="测试私聊" if "Private" in item else "",
            )
        raw = self.store.raw_sessions()
        raw["groups"] = [
            {
                "id": group_id,
                "name": name or group_id,
                "sessions": members,
                "main_session": main or members[0],
            }
        ]
        self.store.save_sessions(raw)
        self.engine.reload_config()
        return members

    async def test_group_shares_the_chat_context(self):
        """一个组里的两个群共享上下文：那边说的话这边也听得到。"""

        self.add_group()
        await self.engine.note_presence(
            self.ctx(session_id=OTHER_SESSION, text="晚上吃火锅吗", user_name="小红")
        )
        async with self.engine.session_state(SESSION) as state:
            window = self.engine.chat_window(state)
        texts = [str(item.get("text") or "") for item in window]
        self.assertIn("晚上吃火锅吗", texts)
        # 留档只有一份，但要记下"这句是哪个会话里说的"
        mirrored = [item for item in window if item.get("text") == "晚上吃火锅吗"][0]
        self.assertEqual(mirrored.get("origin"), OTHER_SESSION)

    async def test_group_marks_the_other_chat_in_the_prompt(self):
        """别处的话只当背景：标出来源，而且不混进"这里在聊什么"。"""

        self.add_group()
        self.engine.note_session_name(OTHER_SESSION, "夜班吐槽群")
        await self.engine.note_presence(
            self.ctx(session_id=OTHER_SESSION, text="晚上吃火锅吗", user_name="小红")
        )
        async with self.engine.session_state(SESSION) as state:
            window = self.engine.chat_window(state)
            blocks = self.engine.prompts.chat_blocks(
                window,
                replied_until=state.chat_replied_until,
                replied_seq=state.chat_replied_seq,
                current_session=SESSION,
                session_labels=self.engine.session_labels(state),
            )
        blob = "\n".join(blocks)
        self.assertIn("你在别处同时听到的", blob)
        self.assertIn("夜班吐槽群", blob)
        # 这句不属于"这里"，所以不该出现在"这里最近在聊什么"那一段里
        here_block = blob.split("你在别处同时听到的")[0]
        self.assertNotIn("晚上吃火锅吗", here_block)

    async def test_group_shares_memories(self):
        """记忆按组存：她在别的群里记下的事，这个群里也回想得起来。"""

        self.add_group()
        self.engine.memory.remember(
            session_id=SESSION,
            persona_id="",
            node_id=self.node_id,
            content="主人在私聊里说周末带我去海边",
            memory_type="event",
            weight=0.9,
        )
        recalled = self.engine.memory.recall(
            session_id=OTHER_SESSION, persona_id="", node_id=self.node_id, limit=5
        )
        self.assertTrue(
            any("海边" in item.content for item in recalled), [item.content for item in recalled]
        )

    async def test_group_shares_the_passive_reply_quota(self):
        """被动回复上限按整组算：两个群加起来超了就静默。"""

        raw = self.store.raw_world()
        raw.setdefault("limits", {})["max_replies_per_hour"] = 2
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_group()
        for item in (SESSION, OTHER_SESSION, SESSION):
            async with self.engine.session_state(item) as state:
                self.engine._count_passive_reply(state)
        async with self.engine.session_state(OTHER_SESSION) as state:
            used, limit = self.engine.reply_quota(state)
            allowed = self.engine._passive_reply_allowed(state)
        self.assertEqual((used, limit), (3, 2))
        self.assertFalse(allowed)

    async def test_sessions_outside_a_group_keep_their_own_chat(self):
        """没进组的会话不受影响：别人群里的话不会跑进来。"""

        self.add_group()
        await self.engine.note_presence(
            self.ctx(session_id=OTHER_SESSION, text="晚上吃火锅吗", user_name="小红")
        )
        raw = self.store.raw_sessions()
        raw["groups"] = []
        self.store.save_sessions(raw)
        self.engine.reload_config()
        async with self.engine.session_state(SESSION) as state:
            state.recent_chat = []
        self.assertEqual(self.engine.member_sessions(SESSION), [])
        self.assertEqual(self.engine.scope_id(SESSION), SESSION)

    async def test_a_group_is_one_person_with_one_clock(self):
        """同一个组只推一次时钟：不然她在两个群里会各老一分钟。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.assertEqual(len(self.engine.tick_session_ids()), 1)
        before = (await self.engine.load_state(SESSION, cold_start=False)).world_time
        await self.engine.tick()
        after = (await self.engine.load_state(SESSION, cold_start=False)).world_time
        self.assertEqual(after - before, 1)
        # 私聊里看到的是同一份状态（位置、数值、计划都共用）
        other = await self.engine.load_state(PRIVATE_SESSION, cold_start=False)
        self.assertEqual(other.world_time, after)
        self.assertEqual(other.node_id, (await self.engine.load_state(SESSION, cold_start=False)).node_id)

    async def test_her_words_can_land_in_another_session_of_the_group(self):
        """主动开口：她自己挑了说给谁，就发到那个会话里去。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.engine.decider.should_ask_llm = lambda state: True
        self.llm.replies = [
            '{"plan":[{"action":"say","messages":["主人，我有点想你了"]}],'
            '"reason":"想跟主人说说话","send_to":"私聊 2692047521"}'
        ]
        outcome = await self.engine.maybe_decide(SESSION, force=True)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.session_id, PRIVATE_SESSION)
        self.assertTrue(any(item == "主人，我有点想你了" for item in outcome.messages))
        sent = [session for session, _msgs in self.messenger.sent]
        self.assertIn(PRIVATE_SESSION, sent)
        self.assertNotIn(SESSION, sent)

    async def test_unknown_destination_falls_back_to_the_representative(self):
        """落点写错 / 没写：不许发到不存在的会话，回落到组代表。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        async with self.engine.session_state(SESSION) as state:
            self.assertEqual(
                self.engine.resolve_send_to(state, "某个不存在的群", fallback=SESSION),
                SESSION,
            )
            self.assertEqual(self.engine.resolve_send_to(state, "", fallback=SESSION), SESSION)
            # 群号、备注、群名都能对上
            self.assertEqual(
                self.engine.resolve_send_to(state, "发给 2692047521", fallback=SESSION),
                PRIVATE_SESSION,
            )

    async def test_promising_to_say_it_elsewhere_actually_goes_there(self):
        """私聊里答应"去群里说晚安"：那条 say 必须真的发到群里，不能只在私聊口头答应。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        self.llm.replies = [
            '{"actions":['
            '{"type":"say","messages":["行，去群里说晚安"]},'
            '{"type":"say","messages":["大家晚安"],"send_to":"群 1001"}'
            "]}"
        ]
        ctx = self.ctx(
            session_id=PRIVATE_SESSION,
            user_id="2692047521",
            user_name="主人",
            text="你回完我，去群里道个晚安",
        )
        await self.engine.handle_incoming(ctx)
        outcome = await self.engine.handle_reply(ctx)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.messages, ["行，去群里说晚安"])
        self.assertEqual(outcome.routed.get(SESSION), ["大家晚安"])

        # 提示词里也得说清她能在两个地方说话（不然模型不敢写 send_to）
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("你能说话的地方", prompt)
        self.assertIn("换地方说就写 send_to", prompt)

    async def test_the_prompt_never_says_she_may_only_speak_here(self):
        """多会话时不能说"只能在你现在说话的地方说话"——那会把跨会话发言整条否掉。"""

        # 单会话（没有会话组）：还是"只能在这儿说"这条老提醒
        self.llm.replies = [SAY_REPLY]
        single = self.prompts_prompt(await self.engine.load_state(SESSION, cold_start=False))
        self.assertIn("只能在你现在说话的地方说话", single)
        self.assertNotIn("换地方说就写 send_to", single)

        # 会了组：改成"名单里的地方都能去"，并且提醒答应的事必须写进 actions
        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        state = await self.engine.load_state(SESSION, cold_start=False)
        multi = self.prompts_prompt(state)
        self.assertIn("换地方说就写 send_to", multi)
        self.assertNotIn("只能在你现在说话的地方说话", multi)
        self.assertIn("就必须把那条 say 一起写进 actions 并写上 send_to", multi)

    async def test_she_can_say_one_thing_here_and_another_there(self):
        """一句话发给群里、另一句发到私聊：两个地方各收到自己那句。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        async with self.engine.session_state(SESSION) as state:
            node = self.engine.node(state.node_id)
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._execute_actions(
                state,
                node,
                outcome,
                [
                    PlannedAction(
                        type="say",
                        messages=["行行行，你们说得都对"],
                        send_to="群 1001",
                    ),
                    PlannedAction(
                        type="say",
                        messages=["烦死了，他们根本不懂"],
                        send_to="私聊 2692047521",
                    ),
                ],
                depth=0,
                autonomous=True,
            )
        self.assertEqual(outcome.messages, ["行行行，你们说得都对"])
        self.assertEqual(
            outcome.routed.get(PRIVATE_SESSION), ["烦死了，他们根本不懂"]
        )
        await self.engine._deliver(outcome)
        sent = {session: list(msgs) for session, msgs in self.messenger.sent}
        self.assertEqual(sent.get(SESSION), ["行行行，你们说得都对"])
        self.assertEqual(sent.get(PRIVATE_SESSION), ["烦死了，他们根本不懂"])

    async def test_a_round_remembers_which_place_it_happened_in(self):
        """私聊里被搭话的那一轮：日志里要写明是在私聊，回显也发到私聊。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        raw = self.store.raw_world()
        raw["echo_types"] = ["reply", "action"]
        self.store.save_world(raw)
        self.engine.reload_config()
        echoed: list[str] = []

        async def sink(session_id, message):
            echoed.append(str(session_id))
            return True

        self.engine.debug_sink = sink
        self.llm.replies = [
            '{"actions":[{"type":"think","content":"心里记一笔"},'
            '{"type":"say","messages":["这就去"]}]}'
        ]
        ctx = self.ctx(session_id=PRIVATE_SESSION, text="你去群里骂他一下")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)

        events = await self.db.call("query_events", session_id=SESSION, limit=30)
        # 她的存档与日志挂在组代表（群）名下，但每条都记着"发生在哪儿"
        where = {
            item["event_type"]: str((item.get("detail") or {}).get("session") or "")
            for item in events
            if item["event_type"] in ("user_message", "reply")
        }
        self.assertIn("私聊 2692047521「主人」", where.get("user_message", ""))
        self.assertIn("私聊 2692047521「主人」", where.get("reply", ""))
        # 回显跟着这一轮走：发到私聊，不跑到她存档的那个群里
        self.assertTrue(echoed, "这一轮应该有回显")
        self.assertEqual(set(echoed), {PRIVATE_SESSION})

    async def test_a_command_echo_follows_the_place_it_happened_in(self):
        """私聊里让她拍腿照：指令那两行回显（🧩/📤）也得发私聊，不能跑到群里。"""

        from core.ports import ToolCallResult

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        raw = self.store.raw_world()
        raw["echo_types"] = ["command_call", "command_result"]
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_command_action(
            id="leg_shot", name="拍腿照", trigger_command="/看看腿"
        )

        echoed: list[str] = []

        async def sink(session_id, message):
            echoed.append(f"{session_id}|{message}")
            return True

        self.engine.debug_sink = sink
        self.engine.commands = _RecordingCommands(
            [], ToolCallResult(ok=True, text="拍好了", tool="/看看腿")
        )
        self.llm.replies = [
            '{"actions":[{"type":"leg_shot","intent":"在沙发上拍一张露腿的照片"}]}',
            "/看看腿",
            SAY_REPLY,
        ]
        ctx = self.ctx(
            session_id=PRIVATE_SESSION, user_id="2692047521", text="拍张腿照给我看"
        )
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)

        self.assertTrue(echoed, "这一轮应该有回显")
        self.assertTrue(
            all(item.startswith(PRIVATE_SESSION) for item in echoed),
            f"回显发错地方了：{echoed}",
        )
        self.assertTrue(any("看看腿" in item for item in echoed), echoed)

    async def test_elsewhere_chat_lines_are_merged_too(self):
        """「你在别处同时听到的」也得合并：同一个人连着说的并成一行，会话标签只写一次。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        now = self.engine._now()
        async with self.engine.session_state(SESSION) as state:
            state.recent_chat = [
                {
                    "user_id": "2692047521",
                    "name": "不相疑",
                    "text": "谁让你帮这种忙啦",
                    "at": now - 4,
                    "is_self": True,
                    "origin": PRIVATE_SESSION,
                    "seq": 1,
                    "world_time": state.world_time,
                },
                {
                    "user_id": "2692047521",
                    "name": "never",
                    "text": "给我老实待着",
                    "at": now - 3,
                    "is_self": True,
                    "origin": PRIVATE_SESSION,
                    "seq": 2,
                    "world_time": state.world_time,
                },
                {
                    "user_id": "2692047521",
                    "name": "不相疑",
                    "text": "你刚刚都答应了",
                    "at": now - 2,
                    "is_self": False,
                    "origin": PRIVATE_SESSION,
                    "seq": 3,
                    "world_time": state.world_time,
                },
            ]
            state.chat_seq = 3

        prompt = await self.engine.preview_autonomous_prompt(SESSION)
        lines = [line for line in prompt.splitlines() if "谁让你帮这种忙啦" in line]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(" / 给我老实待着", lines[0])
        # 会话标签只写一次，不是每行都重复
        self.assertEqual(lines[0].count("〔私聊"), 1, lines[0])

    async def test_a_schedule_that_lands_in_private_echoes_there(self):
        """日程落点勾了私聊：它的回显（🧩 指令那行）也发私聊，别跑到她存档的群里。"""

        from core.ports import ToolCallResult

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        raw = self.store.raw_world()
        raw["echo_types"] = ["command_call", "command_result"]
        self.store.save_world(raw)
        self.engine.reload_config()
        self.add_command_action(
            id="leg_shot", name="拍腿照", trigger_command="/看看腿"
        )
        self.add_schedule(
            id="private_shot",
            time="12:00",
            action_chain=[{"type": "leg_shot", "intent": "在沙发上拍一张露腿的照片"}],
            sessions=[PRIVATE_SESSION],
        )
        echoed: list[str] = []

        async def sink(session_id, message):
            echoed.append(str(session_id))
            return True

        self.engine.debug_sink = sink
        self.engine.commands = _RecordingCommands(
            [], ToolCallResult(ok=True, text="拍好了", tool="/看看腿")
        )
        self.llm.replies = ["/看看腿", SAY_REPLY]
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))

        await self.engine.run_schedules()

        self.assertTrue(echoed, "这条日程应该产生回显")
        self.assertEqual(set(echoed), {PRIVATE_SESSION}, echoed)

    # ---------------- 用户画像（提示词里"这个人是谁"）----------------

    async def test_profile_block_reaches_the_reply_prompt(self):
        """被搭话时提示词里要有"你在跟谁说话"：关系、称呼、喜好、当前能做什么。"""

        self.llm.replies = [SAY_REPLY]
        ctx = self.ctx(text="在干嘛呀", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx)
        self.engine.profiles.set_call_names(
            ctx.session_id, ctx.user_id, call_me="主人"
        )
        self.engine.profiles.note_bond(
            ctx.session_id, ctx.user_id, type="主人", asserted_by="她的判断"
        )
        self.engine.profiles.note_fact(
            ctx.session_id, ctx.user_id, text="喜欢猫", kind="喜好", evidence="我喜欢猫"
        )
        self.engine.profiles.adjust_affinity(
            ctx.session_id, ctx.user_id, 100, reason="测试"
        )

        await self.engine.handle_reply(ctx)
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("# 你在跟谁说话", prompt)
        self.assertIn("主人", prompt)

    async def test_voice_samples_reach_the_prompt_without_becoming_lines(self):
        """挑中的声音样例进第 1 层，并且明确写着"不是台词"（不然她会照抄）。"""

        self.engine.world.persona.samples = [
            {"id": "s1", "scene": "tease", "text": "别逗你蓝姐笑了", "move": "拆台"},
            {"id": "s2", "scene": "snap", "text": "库洛是这样的", "move": "接梗"},
            {"id": "s3", "scene": "night", "text": "本小姐的人生也过期了", "move": "自嘲"},
            {"id": "s4", "scene": "busy", "text": "等下，锅要糊了", "move": "先说自己"},
        ]
        self.llm.replies = [SAY_REPLY]
        ctx = self.ctx(text="在干嘛呀", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("# 她平时怎么说话（只学口吻，不是台词）", prompt)
        self.assertIn("别逗你蓝姐笑了", prompt)
        self.assertIn("不是这一轮要说的台词", prompt)
        # 一轮里不该把整库都塞进去（默认每轮 3 条，库里 4 条）
        inlined = [
            text
            for text in ("别逗你蓝姐笑了", "库洛是这样的", "本小姐的人生也过期了", "等下，锅要糊了")
            if text in prompt
        ]
        self.assertLessEqual(len(inlined), 3, prompt)

    async def test_voice_samples_rotate_between_turns(self):
        """连着两轮不抽同一组：不然她会变成复读机。"""

        self.engine.world.persona.samples = [
            {"id": f"s{index}", "scene": "tease", "text": f"第 {index} 句"} for index in range(5)
        ]
        async with self.engine.session_state(SESSION) as state:
            first = self.engine.voice_sample_lines(state, SESSION)
            second = self.engine.voice_sample_lines(state, SESSION)
        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        self.assertFalse(
            {item["id"] for item in first} == {item["id"] for item in second},
            "连着两轮抽到了同一组",
        )

    async def test_eval_runs_the_script_with_full_prompt_but_no_state_change(self):
        """测评：带着完整提示词跑真实调用，但**不写任何状态**（所以能反复跑）。"""

        self.engine.world.persona.samples = [
            {"id": "s1", "scene": "tease", "text": "别逗你蓝姐笑了"}
        ]
        self.llm.default_reply = (
            '{"reasoning":{"env":"书房","intent":"短回"},'
            '"actions":[{"type":"say","messages":["嗯。"]}]}'
        )
        self.llm.replies = []
        # 画像那一段要"有人在说话"才渲染：先走一条真实消息，把在场的人记下来
        warm = self.ctx(text="我先说一句", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(warm)
        before = await self.engine.db.call("query_events", session_id=SESSION, limit=50)
        result = await self.engine.run_eval(
            SESSION,
            [
                {"scene": "闲聊", "user_text": "在吗", "watch": "短"},
                {"scene": "被撩", "user_text": "想你了", "watch": "接梗", "taboo": "客服腔"},
            ],
            concurrency=2,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["results"][0]["reply"], ["嗯。"])
        # 用上的是完整提示词：人设层、声音样例、画像都要在
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("别逗你蓝姐笑了", prompt)
        self.assertIn("# 你在跟谁说话", prompt)
        # 不写状态：日志里不该多出 reply / action 事件
        after = await self.engine.db.call("query_events", session_id=SESSION, limit=50)
        kinds = [item["event_type"] for item in after]
        self.assertNotIn("reply", kinds)
        self.assertEqual(len(before), len(after))

    async def test_judge_channel_falls_back_to_the_helper_model(self):
        """判断模型没配 → 跟随打杂；判断模型报错/空回 → 这一轮退回打杂，不能挂。"""

        # 没配判断模型时它就是打杂模型本身
        self.assertIs(self.engine.judge_llm, self.engine.helper_llm)

        class _Boom:
            async def generate(self, **kwargs):
                raise RuntimeError("provider 500")

        class _Empty:
            async def generate(self, **kwargs):
                from core.ports import LLMReply

                return LLMReply(text="", ok=True)

        self.llm.default_reply = '{"ok": true}'
        self.llm.replies = []
        for fake in (_Boom(), _Empty()):
            self.engine.judge_llm = fake
            out = await self.engine._ask_judge("s", "sys", "prompt")
            self.assertEqual(out, '{"ok": true}', type(fake).__name__)
        self.engine.judge_llm = self.engine.helper_llm

    async def test_lean_prompt_drops_pep_talk_but_keeps_the_spec(self):
        """精简版：只删劝导语，功能性的格式规范一个字都不能少。"""

        from core.prompt import lean_prompt

        sample = (
            "# ========== 第 1 层：你是谁 ==========\n她话不多。\n\n"
            "# 你最近说过的话（同一个地方别再重复这些句式和开头）\n- 「嗯。」\n\n"
            "# ========== 第 3 层：输出格式 ==========\n"
            '{\n  "reasoning": { ... },\n  "actions": [ ... ]\n}\n\n'
            "关于 reasoning：\n- 它是草稿\n- 先写、写短\n\n"
            "关于 valence_delta：\n- 取 -1 ~ 1\n\n"
            "actions 是你要执行的动作列表，格式如下：\n"
            '- { "type": "say", "messages": ["消息1"] }\n\n'
            "# 你在跟谁说话\n- 某人\n\n"
            "# 最后确认\n1. 只输出 JSON\n"
        )
        lean = lean_prompt(sample)
        for gone in ("# 你最近说过的话", "# 最后确认", "关于 reasoning：", "关于 valence_delta："):
            self.assertNotIn(gone, lean, gone)
        for keep in (
            "# ========== 第 1 层：你是谁",
            "她话不多。",
            "# ========== 第 3 层：输出格式",
            '"actions": [ ... ]',
            "actions 是你要执行的动作列表",
            "# 你在跟谁说话",
        ):
            self.assertIn(keep, lean, keep)
        self.assertLess(len(lean), len(sample))

    async def test_miss_overview_shows_up_in_the_state_snapshot(self):
        """「想找谁」要能在编辑器里看到：想念值、冷却里的人、今天还剩几次额度。"""

        # 先让他来说一句（这时候她会把想念清零、进冷却），再设想念值——
        # 顺序反了会被"刚聊过就清零"的正确行为覆盖掉
        await self.engine.handle_incoming(self.ctx(text="在吗", user_id="2692047521"))
        async with self.engine.session_state(SESSION) as state:
            state.miss = {"2692047521": 0.62}
            state.miss_ready_at = {"999": self.engine._now() + 45 * 60}
            state.user_presence["999"] = {"user_id": "999", "name": "小明"}
        snap = await self.engine.snapshot(SESSION)
        miss = snap.get("miss") or {}
        self.assertTrue(miss.get("people"), snap.get("miss"))
        top = miss["people"][0]
        self.assertEqual(top["user_id"], "2692047521")
        self.assertAlmostEqual(float(top["value"]), 0.62, places=2)
        self.assertIn("threshold", miss)
        self.assertIn("push_cap", miss)
        waiting = [item for item in miss["people"] if item.get("waiting")]
        self.assertTrue(waiting, "刚聊过还在冷却里的人也要列出来")
        self.assertGreater(int(waiting[0]["ready_in_minutes"]), 0)

    async def test_followup_of_a_consumption_action_remembers_what_she_watched(self):
        """追剧 / 看书这类动作：她说的那句要进状态槽，下一次提示词里带着——
        不然每次追剧都像第一次看，只会凭空来一句"这剧情真离谱"。"""

        self.llm.replies = [
            '{"actions":[{"type":"say","messages":["在重看《海兽之子》，'
            '看到鲸鱼那段还是起鸡皮疙瘩"]}]}'
        ]
        definition = self.engine.world.action_map().get("watch_tv")
        self.assertIsNotNone(definition, "默认世界里应该有追剧这个动作")
        self.assertEqual(str(getattr(definition, "state_slot", "")), "watching")

        async with self.engine.session_state(SESSION) as state:
            state.node_id = "lobby"
            outcome = TickOutcome(session_id=SESSION)
            node = self.engine.node("lobby")
            await self.engine._llm_followup(
                state,
                node,
                outcome,
                str(definition.on_complete.prompt_hint),
                "你刚刚做完了「追剧」。",
                definition=definition,
            )
            slot = (state.external_state or {}).get("watching") or {}
            self.assertIn("海兽之子", str(slot.get("text") or ""))
            self.assertEqual(str(slot.get("label") or ""), "在看的剧")
            # 提示词里要能念出来
            block = self.engine.prompts.external_state_block(state)
            self.assertIn("在看的剧", block)
            self.assertIn("海兽之子", block)

    async def test_eval_reports_action_usage_and_drops_invalid_ones(self):
        """测评要看"会不会用动作系统"：用真实白名单解析，做不到的动作会被丢掉并记下来。"""

        async with self.engine.session_state(SESSION) as state:
            state.node_id = "kitchen"  # 厨房才做得了饭
        self.llm.default_reply = json.dumps(
            {
                "actions": [
                    {"type": "say", "messages": ["等下，锅要糊了"]},
                    {"type": "cook", "duration": 600, "intent": "把饭做上"},
                    {"type": "fly_to_moon", "intent": "这个动作根本不存在"},
                ]
            },
            ensure_ascii=False,
        )
        self.llm.replies = []
        warm = self.ctx(text="我先说一句", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(warm)
        result = await self.engine.run_eval(
            SESSION, [{"scene": "动作", "user_text": "拍张照"}], concurrency=1
        )
        row = result["results"][0]
        self.assertIn("say", row["actions"])
        self.assertIn("cook", row["actions"])
        # 不存在的动作不该出现在结果里（真机上会被丢掉，测评也要照实反映）
        self.assertNotIn("fly_to_moon", row["actions"])
        self.assertTrue(row["tools"], "工具/动作的意图也要记下来")
        self.assertTrue(row["actions_available"], "要记下这一轮她手边能用哪些动作")
        self.assertIn("cook", row["actions_available"])

    async def test_tone_from_the_model_overrides_the_keyword_table(self):
        """反话由模型判：模型说是 attack，就不能按关键词表当成被夸。"""

        self.llm.replies = [
            '{"tone":"attack","actions":[{"type":"say","messages":["少阴阳怪气啦"]}]}'
        ]
        ctx = self.ctx(text="你可真行啊，全群就你最厉害", user_id="2692047521")
        await self.engine.handle_incoming(ctx)
        before = (await self.get_state()).loneliness
        await self.engine.handle_reply(ctx)
        after = await self.get_state()
        # attack → negative_words：孤独感上升（而不是被当作夸奖掉下去）
        self.assertGreaterEqual(after.loneliness, before)
        events = await self.engine.db.call("query_events", session_id=SESSION, limit=10)
        reply = next(item for item in events if item["event_type"] == "reply")
        self.assertEqual(reply["detail"]["tone"], "attack")

    async def test_analyst_variant_appends_instead_of_replacing(self):
        """分析层是**补充**：原始信息一个不少，分析贴在最后，并写明"冲突以事实为准"。"""

        self.llm.default_reply = '{"actions":[{"type":"say","messages":["嗯。"]}]}'
        self.llm.replies = []
        analyst = StubLLM(
            [
                "这一轮是试探，不是随口问。\n他想要照片。\n你们现在到客气档，不能给。",
                "这轮变了：他开始装可怜。\n别的照旧。",
            ]
        )
        warm = self.ctx(text="我先说一句", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(warm)
        result = await self.engine.run_eval(
            SESSION,
            [
                {"scene": "a", "user_text": "发张照片"},
                {"scene": "b", "user_text": "求你了"},
            ],
            concurrency=1,
            variant="analyst",
            director_llm=analyst,
        )
        rows = result["results"]
        self.assertEqual(len(rows), 2)
        first_prompt = self.llm.calls[0]["system_prompt"]
        second_prompt = self.llm.calls[1]["system_prompt"]
        # 原始信息仍在（不是替换）
        for keep in ("# 你在跟谁说话", "# ========== 第 5 层：运行时状态"):
            self.assertIn(keep, first_prompt)
        self.assertIn("# 这一轮的分析", first_prompt)
        self.assertIn("以事实为准", first_prompt)
        self.assertIn("他想要照片", first_prompt)
        # 第二轮把上一轮的分析带进分析师的输入里，并且主人格拿到的是新的那份
        self.assertIn("你上一轮的分析", analyst.calls[1]["prompt"])
        # v2 是"选择题"：意图只能从固定选项里挑，记忆只准按编号引用
        self.assertIn("意图：<从这些里挑一个", analyst.calls[0]["system_prompt"])
        self.assertIn("只准按编号引用", analyst.calls[0]["prompt"])
        self.assertIn("他开始装可怜", second_prompt)
        self.assertNotIn("他想要照片", second_prompt)

    async def test_director_variant_shrinks_the_main_prompt(self):
        """决策层：先用便宜模型写简报，主人格拿到的提示词应该明显变短，且照样出话。"""

        self.engine.world.persona.samples = [
            {"id": "s1", "scene": "tease", "text": "别逗你蓝姐笑了"}
        ]
        self.llm.default_reply = '{"actions":[{"type":"say","messages":["嗯。"]}]}'
        self.llm.replies = []
        # 导演通道单独用一个桩，方便断言"它确实被调过"
        director = StubLLM(["她要先把话接住。\n口吻照旧，短。\n不用提旧事。"])
        warm = self.ctx(text="我先说一句", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(warm)
        full = await self.engine.run_eval(
            SESSION, [{"scene": "a", "user_text": "在吗"}], concurrency=1
        )
        full_prompt = self.llm.calls[-1]["system_prompt"]
        result = await self.engine.run_eval(
            SESSION,
            [{"scene": "a", "user_text": "在吗"}],
            concurrency=1,
            variant="director",
            director_llm=director,
        )
        main_prompt = self.llm.calls[-1]["system_prompt"]
        # 导演写简报时看的是完整语境，主人格只看简报
        self.assertTrue(director.calls, "导演通道没被调用")
        self.assertIn("# 你在跟谁说话", director.calls[-1]["prompt"])
        self.assertLess(len(main_prompt), len(full_prompt) * 0.6)
        self.assertIn("# 这一轮怎么办", main_prompt)
        self.assertIn("别逗你蓝姐笑了", main_prompt)
        self.assertNotIn("# 你在跟谁说话", main_prompt)
        self.assertEqual(result["results"][0]["reply"], ["嗯。"])
        self.assertIn("她要先把话接住", result["results"][0]["brief"])

    async def test_eval_lean_variant_is_shorter_and_still_answers(self):
        """真跑一遍精简版：提示词更短、照样能出话。"""

        self.llm.default_reply = '{"actions":[{"type":"say","messages":["嗯。"]}]}'
        self.llm.replies = []
        warm = self.ctx(text="我先说一句", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(warm)
        full = await self.engine.run_eval(
            SESSION, [{"scene": "a", "user_text": "在吗"}], concurrency=1
        )
        full_prompt = self.llm.calls[-1]["system_prompt"]
        lean = await self.engine.run_eval(
            SESSION, [{"scene": "a", "user_text": "在吗"}], concurrency=1, variant="lean"
        )
        lean_prompt = self.llm.calls[-1]["system_prompt"]
        self.assertLess(len(lean_prompt), len(full_prompt))
        self.assertEqual(lean["results"][0]["reply"], ["嗯。"])
        self.assertEqual(full["results"][0]["reply"], ["嗯。"])

    async def test_eval_keeps_going_when_one_round_fails(self):
        """某一轮模型挂了，不该让整份考卷作废。"""

        class _Boom:
            def __init__(self, inner):
                self.inner = inner
                self.count = 0

            async def generate(self, **kwargs):
                self.count += 1
                if self.count == 1:
                    raise RuntimeError("provider 502")
                return await self.inner.generate(**kwargs)

        self.llm.default_reply = '{"actions":[{"type":"say","messages":["嗯。"]}]}'
        self.llm.replies = []
        boom = _Boom(self.llm)
        result = await self.engine.run_eval(
            SESSION,
            [{"scene": "a", "user_text": "第一句"}, {"scene": "b", "user_text": "第二句"}],
            llm=boom,
            concurrency=1,
        )
        rows = result["results"]
        self.assertTrue(any(item["error"] for item in rows))
        self.assertTrue(any(item["reply"] for item in rows))

    async def test_persona_review_only_applies_what_you_picked(self):
        """优化人设：只写你点过的改动；找不到原文的条目跳过并报出来。"""

        self.engine.world.persona.text = "她很温柔。\n她喜欢米饭。"
        self.llm.replies = [
            json.dumps(
                {
                    "ok": ["怪癖写得好"],
                    "issues": [
                        {
                            "level": "建议改",
                            "kind": "形容词",
                            "detail": "温柔没有落地",
                            "quote": "她很温柔。",
                        }
                    ],
                    "rewrite": [
                        {"before": "她很温柔。", "after": "她嘴硬心软，被哄会别过脸。", "why": "落地"},
                        {"before": "原文里没有这句", "after": "乱改", "why": "找不到"},
                    ],
                    "add": [{"field": "口头习惯", "text": "爱说「诶嘿」", "why": "补口癖"}],
                    "questions": ["她怕不怕冷"],
                },
                ensure_ascii=False,
            )
        ]
        report = await self.engine.review_persona(SESSION)
        self.assertTrue(report["ok"], report)
        self.assertEqual(len(report["issues"]), 1)
        # 找不到原文的那条被挡在门外，不该出现在可选项里
        self.assertEqual(len(report["rewrite"]), 1)
        self.assertEqual(report["add"][0]["field"], "口头习惯")

        # 只应用"改写"这一条：新增那条不点，就不该进角色卡
        result = self.engine.apply_persona_changes(report["rewrite"], [])
        self.assertTrue(result["ok"], result)
        text = self.engine.world.persona.text
        self.assertIn("嘴硬心软", text)
        self.assertIn("她喜欢米饭。", text)
        self.assertNotIn("诶嘿", text)

    async def test_persona_apply_reports_missing_original(self):
        """原文对不上（用户自己改过了）时跳过并说明，不硬写。"""

        self.engine.world.persona.text = "只有这一句。"
        result = self.engine.apply_persona_changes(
            [{"before": "不存在的一段", "after": "改后"}], []
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(self.engine.world.persona.text, "只有这一句。")

    async def test_open_topic_is_recorded_but_not_asked_right_away(self):
        """他还有件没说完的事：先记下来，不到点不提（不然像查户口）。"""

        self.llm.replies = [
            '{"open_topic":"主人明天要去体检，还没出结果",'
            '"actions":[{"type":"say","messages":["嗯，去吧"]}]}'
        ]
        ctx = self.ctx(text="我明天要去体检", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        state = await self.get_state()
        self.assertEqual(len(state.open_topics), 1)
        self.assertIn("体检", state.open_topics[0]["text"])

        # 没过时间：提示词里不该出现
        self.llm.replies = [SAY_REPLY]
        ctx2 = self.ctx(text="在吗", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx2)
        await self.engine.handle_reply(ctx2)
        self.assertNotIn("# 还没聊完的", self.llm.calls[-1]["system_prompt"])

        # 过了默认的一小时：可以接一句了
        self.clock.advance(70 * 60)
        self.llm.replies = [SAY_REPLY]
        ctx3 = self.ctx(text="在干嘛", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx3)
        await self.engine.handle_reply(ctx3)
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("# 还没聊完的", prompt)
        self.assertIn("体检", prompt)
        state = await self.get_state()
        self.assertEqual(int(state.open_topics[0]["asked"]), 1)

    async def test_open_topic_carries_the_date_it_was_said(self):
        """「他明天要去体检」要带上说的日期。

        不带日期的话，挂三天之后那句里的「明天」还是明天，
        她会去问一件早就过完的事。
        """

        async with self.engine.session_state(SESSION) as state:
            state.open_topics = [
                {
                    "text": "他明天要去体检",
                    "who": "2692047521",
                    "who_name": "不相疑",
                    "session": SESSION,
                    "at": self.clock.now() - 3 * 86400,
                    "next_ask_at": self.clock.now() - 60,
                    "asked": 0,
                }
            ]
            layer = self.engine.prompts.open_topics_layer(
                self.engine.prompts._due_open_topics(state, SESSION)
            )
        self.assertIn("体检", layer)
        self.assertIn("不相疑", layer)
        self.assertIn("3 天前", layer)
        # 规则也要写清：里面的"明天"按那天算，过期了改问结果
        self.assertIn("按那一天算", layer)
        self.assertIn("改问结果", layer)

        # 而且抽取那一刻就要求写具体日期，别写"明天"
        spec = self.engine.prompts.format_layer(mode="actions", max_messages=3)
        self.assertIn("时间写成具体日期", spec)

    async def test_open_topic_gives_up_after_the_ask_limit(self):
        """催两次还没结果就先放下，别变成催命。"""

        async with self.engine.session_state(SESSION) as state:
            state.open_topics = [
                {
                    "text": "他在纠结要不要换工作",
                    "who": "2692047521",
                    "who_name": "不相疑",
                    "session": SESSION,
                    "at": self.clock.now() - 3600,
                    "next_ask_at": self.clock.now() - 60,
                    "asked": 0,
                }
            ]
            ctx = self.ctx(text="在吗", user_id="2692047521", user_name="不相疑")
            self.engine.mark_open_topics_asked(state, ctx)
            self.assertEqual(int(state.open_topics[0]["asked"]), 1)
            self.clock.advance(3 * 3600)
            self.engine.mark_open_topics_asked(state, ctx)
        # 第二次问过之后就放下，不再挂在提示词里
        self.assertEqual(state.open_topics, [])

    async def test_open_topic_is_dropped_when_it_gets_old(self):
        """挂了一周还没结果的事，不再当成"待续"。"""

        async with self.engine.session_state(SESSION) as state:
            state.open_topics = [
                {
                    "text": "一件很久以前的事",
                    "who": "2692047521",
                    "session": SESSION,
                    "at": self.clock.now() - 30 * 86400,
                    "next_ask_at": self.clock.now() - 60,
                    "asked": 0,
                }
            ]
            self.engine.mark_open_topics_asked(
                state,
                self.ctx(text="在吗", user_id="2692047521", user_name="不相疑"),
            )
        self.assertEqual(state.open_topics, [])

    async def test_voice_samples_prefer_the_matching_scene(self):
        """被哄的那一轮优先抽"被撩"那组样例。"""

        self.engine.world.persona.samples = [
            {"id": "warm", "scene": "tease", "text": "诶嘿～"},
            {"id": "cold", "scene": "snap", "text": "别说了"},
        ]
        async with self.engine.session_state(SESSION) as state:
            picked = self.engine.voice_sample_lines(state, SESSION, signal="hug")
        self.assertEqual(picked[0]["id"], "warm")

    async def test_reply_prompt_tells_her_how_long_since_they_talked(self):
        """提示词里要知道"上次跟他说话是多久前"——不然"你昨天说的那个"落不了地。"""

        self.llm.replies = [SAY_REPLY]
        ctx = self.ctx(text="在干嘛呀", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx)
        # 上一次直接说话挪到两小时前（模拟"他好久没找我"）
        store = self.engine.profiles
        key = store.group_key(ctx.session_id)
        row = await self.engine.db.call(
            "get_user_profile", group_id=key, user_id=ctx.user_id
        )
        payload = dict((row or {}).get("payload") or {})
        payload["last_talked_at"] = self.clock.now() - 2 * 3600
        store._save(key, ctx.user_id, payload)

        await self.engine.handle_reply(ctx)
        prompt = self.llm.calls[-1]["system_prompt"]
        section = prompt.split("# 你在跟谁说话", 1)[-1].split("\n# ", 1)[0]
        self.assertIn("他上次跟你说话是 2 小时前", section, f"实际这一段是：\n{section}")

    async def test_plain_bond_keeps_intimacy_limits_in_the_prompt(self):
        """普通关系（群友）就算好感拉满，提示词里也写着"这一档还不能做：亲亲"。"""

        self.llm.replies = [SAY_REPLY]
        ctx = self.ctx(text="在吗", user_id="7", user_name="小明")
        await self.engine.handle_incoming(ctx)
        self.engine.profiles.note_bond(
            ctx.session_id, ctx.user_id, type="群友", asserted_by="她的判断"
        )
        self.engine.profiles.adjust_affinity(ctx.session_id, ctx.user_id, 100, reason="测试")

        await self.engine.handle_reply(ctx)
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("这一档还不能做：", prompt)
        self.assertIn("亲亲", prompt)
        self.assertIn("熟人", prompt)  # 群友的上限卡在"熟人"，不是"特别的人"
        self.assertNotIn("特别的人", prompt)

    async def test_romance_bond_does_not_get_denied_hugs_when_affinity_is_low(self):
        """绑了男友但好感还低：提示词里不能出现"他是你男友"和"还不熟别贴贴"打架。"""

        self.llm.replies = [SAY_REPLY]
        ctx = self.ctx(text="在吗", user_id="7", user_name="小明")
        await self.engine.handle_incoming(ctx)
        self.engine.profiles.note_bond(
            ctx.session_id, ctx.user_id, type="男友", asserted_by="她的判断"
        )
        self.engine.profiles.adjust_affinity(ctx.session_id, ctx.user_id, 20, reason="刚在一起")

        await self.engine.handle_reply(ctx)
        prompt = self.llm.calls[-1]["system_prompt"]
        section = prompt.split("# 你在跟谁说话", 1)[-1].split("\n# ", 1)[0]
        self.assertIn("男友", section)
        self.assertIn("亲近", section)  # 关系下限把档位抬到"亲近"
        # 这一档的 deny 只剩腿照，抱抱 / 亲亲不在禁止清单里
        denied = section.split("这一档还不能做：", 1)[-1].split("\n", 1)[0]
        self.assertNotIn("抱抱", denied)
        self.assertNotIn("亲亲", denied)
        self.assertNotIn("客气", section)

    async def test_other_people_get_their_digest_line(self):
        """群里还有谁：其他人一人一行（带缩略版画像），最多按配置给几个。"""

        self.llm.replies = [SAY_REPLY]
        await self.engine.note_presence(
            self.ctx(text="群里的闲聊", user_id="7", user_name="小明")
        )
        self.engine.profiles.set_digest(SESSION, "7", "爱开玩笑、常聊工作")
        self.engine.profiles.note_bond(
            SESSION, "7", type="朋友", asserted_by="她的判断"
        )
        ctx = self.ctx(text="在干嘛呀", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx)

        await self.engine.handle_reply(ctx)
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("# 群里还有谁", prompt)
        self.assertIn("小明", prompt)
        self.assertIn("朋友", prompt)
        self.assertIn("爱开玩笑", prompt)

    # ---------------- 回想与回访 ----------------

    async def test_a_sleep_segment_consolidates_at_most_twice(self):
        """一段睡眠最多整理两次：睡下 20 分钟一次、睡到 5 小时补一次，之后不再跑。"""

        calls: list[str] = []

        async def fake_consolidate(state, *, mode="full", dry_run=False):
            calls.append(mode)
            return {"mode": mode, "ok": True, "note": "测试"}

        self.engine.consolidate_now = fake_consolidate

        async with self.engine.session_state(SESSION) as state:
            state.state = "sleeping"
        # 刚睡下：不整理
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(calls, [])
        # 20 分钟：第一次
        self.clock.advance(20 * 60)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(calls, ["full"])
        # 又过两小时：不该再跑（不是"每半小时一次"）
        self.clock.advance(120 * 60)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(calls, ["full"])
        # 睡到 5 小时：补第二次
        self.clock.advance(180 * 60)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(calls, ["full", "full"])
        # 睡到 8 小时：还是两次
        self.clock.advance(180 * 60)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(calls, ["full", "full"])
        # 醒了：这一段结束；再睡下重新算，还能整理
        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            await self.engine.maybe_consolidate(state)
        async with self.engine.session_state(SESSION) as state:
            state.state = "sleeping"
            await self.engine.maybe_consolidate(state)
        self.clock.advance(25 * 60)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(calls, ["full", "full", "full"])

    async def test_a_nap_consolidates_once_with_the_light_mode(self):
        calls: list[str] = []

        async def fake_consolidate(state, *, mode="full", dry_run=False):
            calls.append(mode)
            return {"mode": mode, "ok": True}

        self.engine.consolidate_now = fake_consolidate
        async with self.engine.session_state(SESSION) as state:
            state.state = "napping"
            await self.engine.maybe_consolidate(state)
        self.clock.advance(6 * 60)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(calls, ["nap"])
        self.clock.advance(60 * 60)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(calls, ["nap"], "一段小睡只做一次轻整理")

    async def test_recall_can_focus_on_one_person(self):
        """「回想一下关于小明的」：按名字对上人，只挑跟他有关的记忆。"""

        await self.engine.note_presence(
            self.ctx(text="我是小明", user_id="42", user_name="小明")
        )
        await self.engine.note_presence(
            self.ctx(text="我是阿May", user_id="7", user_name="阿May")
        )
        async with self.engine.session_state(SESSION) as state:
            focus = self.engine._recall_focus_user(state, "回忆一下关于小明的事")
        self.assertEqual(focus, "42")

    async def test_due_review_is_remembered_once_then_rescheduled(self):
        """到点的回访：想起来一次、写进提示词一次，接着把下次回访往后排。"""

        self.engine.world.profile.recall_daily_max = 5
        await self.engine.handle_incoming(
            self.ctx(text="我喜欢猫", user_id="42", user_name="小明")
        )
        memory_id = self.engine.memory.remember(
            session_id=SESSION,
            persona_id="",
            node_id=self.node_id,
            content="他以前养过一只橘猫",
            memory_type="interaction",
            related_users=["42"],
            participants=["42"],
            tier="gist",
            next_review_at=1.0,  # 早就该回访了
            weight=0.6,
        )
        async with self.engine.session_state(SESSION) as state:
            block = self.engine.review_block(state)
            # 实跑里是 tick 把它挂上去的；这里手动挂，验证"读过一次就不再提"
            state.pending_review = block
            pending = self.engine.extra_reminders(state)
            again = self.engine.extra_reminders(state)
        self.assertIn("你忽然想起", block)
        self.assertIn("橘猫", block)
        self.assertIn("小明", block)
        self.assertIn("你忽然想起", pending)
        self.assertEqual(again, "", "同一件事不该连着提两遍")
        row = [item for item in self.engine.memory.db.query_memories(session_id=SESSION) if item["id"] == memory_id][0]
        self.assertGreater(row["next_review_at"], time.time())

    async def test_review_respects_the_daily_quota(self):
        """每天的回想回访有上限：额度用完就不再冒出新的"忽然想起"。"""

        self.engine.world.profile.recall_daily_max = 1
        self.engine.world.profile.miss_limit = 1
        await self.engine.handle_incoming(
            self.ctx(text="我喜欢猫", user_id="42", user_name="小明")
        )
        for index in range(2):
            self.engine.memory.remember(
                session_id=SESSION,
                persona_id="",
                node_id=self.node_id,
                content=f"值得回想的事 {index}",
                memory_type="interaction",
                related_users=["42"],
                participants=["42"],
                tier="gist",
                next_review_at=1.0,
            )
        async with self.engine.session_state(SESSION) as state:
            first = self.engine.review_block(state)
            second = self.engine.review_block(state)
        self.assertIn("你忽然想起", first)
        self.assertEqual(second, "", "当天额度用完就不该再提")

    # ---------------- 孤独与想念：群里热闹 ≠ 有人陪她 ----------------

    async def test_a_busy_group_does_not_drain_her_loneliness(self):
        """群里一直有人说话只说明"不闷"：孤独不该被刷屏一路扣光。"""

        async with self.engine.session_state(SESSION) as state:
            state.loneliness = 0.5
        async with self.engine.session_state(SESSION) as state:
            before = state.loneliness
            self.engine.dynamics.apply_event(state, "group_lively", now=self.engine._now())
            group_after = state.loneliness
            self.engine.dynamics.apply_event(state, "mention_bot", now=self.engine._now())
            talked_to = state.loneliness

        self.assertGreater(group_after, before - 0.03)
        self.assertLess(talked_to, group_after - 0.02)

    async def test_missing_someone_builds_up_when_nobody_talks_to_her(self):
        """没跟主人说过话：想念会攒起来，并写进提示词让她主动去找他。"""

        self.engine.world.profile.miss_growth_per_min = 0.05
        ctx = self.ctx(text="在吗", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx)
        self.engine.profiles.note_bond(
            ctx.session_id, ctx.user_id, type="主人", asserted_by="她的判断"
        )
        self.engine.profiles.adjust_affinity(ctx.session_id, ctx.user_id, 80, reason="测试")
        # 群里别人在刷屏（不是对她说的）
        await self.engine.note_presence(
            self.ctx(text="群友闲聊", user_id="7", user_name="小明")
        )
        # 刚聊过之后要先随机等一段才会开始想他：把表直接拨过最长冷却
        self.clock.advance((self.engine.world.profile.miss_cooldown_max_minutes + 1) * 60)

        async with self.engine.session_state(SESSION) as state:
            for _ in range(12):
                self.engine._update_miss(state)
            score = float(state.miss.get("2692047521") or 0.0)
            block = self.engine.miss_block(state)

        self.assertGreater(score, 0.0)
        self.assertIn("你有点想他们了", block)
        self.assertIn("不相疑", block)

    async def test_missing_waits_a_random_cooldown_before_it_starts(self):
        """想念不是固定节拍：刚聊完要随机等一段，等够了才涨。"""

        boss = "2692047521"
        self.engine.world.profile.miss_growth_per_min = 0.5
        await self.engine.handle_incoming(
            self.ctx(text="在吗", user_id=boss, user_name="不相疑")
        )

        # 冷却时长是在 [min, max] 里随机取的：同一时刻连取两次不会一样
        self.assertNotEqual(
            self.engine._next_miss_open_at(self.engine._now()),
            self.engine._next_miss_open_at(self.engine._now()),
        )

        async with self.engine.session_state(SESSION) as state:
            self.clock.advance(10 * 60)  # 还没到最短冷却
            for _ in range(5):
                self.engine._update_miss(state)
            self.assertEqual(
                float(state.miss.get(boss) or 0.0), 0.0, "冷却期里不该开始想他"
            )

            self.clock.advance(
                (self.engine.world.profile.miss_cooldown_max_minutes + 1) * 60
            )
            for _ in range(3):
                self.engine._update_miss(state)
            self.assertGreater(float(state.miss.get(boss) or 0.0), 0.0)

    async def test_missing_tells_talked_apart_from_seen(self):
        """想念里的"多久"看的是他多久没跟她说话，不是他在群里露没露面。"""

        boss = "2692047521"
        await self.engine.handle_incoming(
            self.ctx(text="在吗", user_id=boss, user_name="不相疑")
        )
        # 之后他一直在群里露脸，但再没跟她说过话
        self.clock.advance(5 * 3600 + 10 * 60)
        await self.engine.note_presence(
            self.ctx(
                text="群友闲聊",
                user_id=boss,
                user_name="不相疑",
                is_wake=False,
                is_mentioned=False,
            )
        )
        self.clock.advance(10 * 60)

        async with self.engine.session_state(SESSION) as state:
            state.miss[boss] = 0.9
            block = self.engine.miss_block(state)

        self.assertIn("上次跟他说话是 5 小时前", block)
        self.assertIn("10 分钟前在群里露过面", block)

    # ---------------- 好奇心：睡眠回落 / 增长饱和 / 查完就满足 ----------------

    async def test_a_night_of_sleep_cools_her_curiosity(self):
        """睡一觉好奇心会落下来：不会带着满格好奇醒来，也不会一路掉到 0。"""

        async with self.engine.session_state(SESSION) as state:
            state.state = "sleeping"
            state.curiosity = 0.95
            for _ in range(8 * 60):
                self.engine.dynamics.tick(
                    state, node=None, elapsed_seconds=60.0, world=self.engine.world
                )
            overnight = float(state.curiosity)
            for _ in range(12 * 60):
                self.engine.dynamics.tick(
                    state, node=None, elapsed_seconds=60.0, world=self.engine.world
                )
            after_a_day = float(state.curiosity)

        floor = float(self.engine.world.state_dynamics.sleep_curiosity_floor)
        self.assertLess(overnight, 0.45, "睡一整觉该把昨天攒的好奇放下")
        self.assertGreater(overnight, floor, "但醒来还是要对新鲜事有点兴趣")
        self.assertAlmostEqual(after_a_day, floor, places=3)

    async def test_curiosity_growth_slows_down_near_the_top(self):
        """好奇心涨到快满时增长会慢下来，不会一天就钉死在 1.0。"""

        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            state.curiosity = 0.5
            for _ in range(60):
                self.engine.dynamics.tick(
                    state, node=None, elapsed_seconds=60.0, world=self.engine.world
                )
            low_gain = float(state.curiosity) - 0.5

            state.curiosity = 0.95
            for _ in range(60):
                self.engine.dynamics.tick(
                    state, node=None, elapsed_seconds=60.0, world=self.engine.world
                )
            high_gain = float(state.curiosity) - 0.95

        self.assertAlmostEqual(low_gain, 0.06, places=3)
        self.assertLess(high_gain, low_gain * 0.3)

    async def test_searching_something_settles_her_curiosity(self):
        """查完一次真拿到东西：好奇心落到阈值以下，不会冷却一到又去查同一件事。"""

        self.add_schedule(
            id="news",
            time="12:00",
            action_chain=[{"type": "search_web", "intent": "今天有什么新闻"}],
        )
        self.tools.results["web_search"] = "今天有 3 条科技新闻，其中一条是模型开源。"
        await self.set_state(node_id="study", curiosity=0.92)
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))

        await self.engine.run_schedules()

        state = await self.get_state()
        self.assertLess(float(state.curiosity), 0.7, "查完该落到「想查点东西」这条线以下")
        self.assertAlmostEqual(float(state.curiosity), 0.92 - 0.30, places=3)

    async def test_a_search_that_found_nothing_does_not_settle_anything(self):
        """什么都没查到（工具报错 / 没配检索工具）：这次不算「满足」，好奇心不动。"""

        self.add_schedule(
            id="news",
            time="12:00",
            action_chain=[{"type": "search_web", "intent": "今天有什么新闻"}],
        )
        self.tools.results["web_search"] = ""
        await self.set_state(node_id="study", curiosity=0.92)
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))

        await self.engine.run_schedules()

        state = await self.get_state()
        self.assertAlmostEqual(float(state.curiosity), 0.92, places=3)

    async def test_the_lonelier_she_is_the_faster_she_misses_someone(self):
        """越孤独越容易想起某个人：同样没人找她，孤独满的时候涨得明显更快。"""

        boss = "2692047521"
        self.engine.world.profile.miss_growth_per_min = 0.01
        await self.engine.handle_incoming(
            self.ctx(text="在吗", user_id=boss, user_name="不相疑")
        )
        self.clock.advance((self.engine.world.profile.miss_cooldown_max_minutes + 1) * 60)

        async with self.engine.session_state(SESSION) as state:
            state.loneliness = 0.0
            for _ in range(10):
                self.engine._update_miss(state)
            calm = float(state.miss.get(boss) or 0.0)

            state.miss[boss] = 0.0
            state.loneliness = 1.0
            for _ in range(10):
                self.engine._update_miss(state)
            lonely = float(state.miss.get(boss) or 0.0)

        self.assertGreater(calm, 0.0)
        self.assertGreater(lonely, calm * 1.5)

    async def test_a_message_stored_twice_shows_up_once_in_the_prompt(self):
        """老存档里同一条消息存了「带注释」和「不带注释」两份：提示词里只留一行。"""

        now = self.engine._now()
        async with self.engine.session_state(SESSION) as state:
            state.recent_chat = [
                {
                    "user_id": "42",
                    "name": "小明",
                    "text": "亲亲",
                    "at": now - 5,
                    "world_time": state.world_time,
                    "seq": 1,
                    "is_self": False,
                },
                {
                    "user_id": "42",
                    "name": "小明",
                    "text": "亲亲\n［这条消息 @ 了：你（小鲸鱼(10001)）］",
                    "at": now,
                    "world_time": state.world_time,
                    "seq": 2,
                    "is_self": False,
                },
            ]
            state.chat_seq = 2

        prompt = await self.engine.preview_autonomous_prompt(SESSION)
        history = [
            line
            for line in prompt.splitlines()
            if line.startswith("- [") and "亲亲" in line
        ]
        self.assertEqual(len(history), 1, history)

    async def test_he_talking_to_her_clears_the_missing(self):
        ctx = self.ctx(text="在吗", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx)
        async with self.engine.session_state(SESSION) as state:
            state.miss["2692047521"] = 0.9
        await self.engine.handle_incoming(self.ctx(
            text="蓝蓝在吗", user_id="2692047521", user_name="不相疑"
        ))
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(float(state.miss.get("2692047521") or 0.0), 0.0)

    async def test_profile_can_be_switched_off(self):
        self.llm.replies = [SAY_REPLY]
        self.engine.world.profile.enabled = False
        ctx = self.ctx(text="在吗", user_id="7", user_name="小明")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertNotIn("# 你在跟谁说话", prompt)
        self.assertNotIn("# 群里还有谁", prompt)

    async def test_replying_here_does_not_mark_the_other_place_as_answered(self):
        """在群里回过话，不该把私聊里还没答的留言也标成答过了。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        await self.engine.note_presence(
            self.ctx(session_id=PRIVATE_SESSION, text="在忙吗", user_name="主人")
        )
        await self.engine.note_presence(self.ctx(text="群里随便聊聊", user_name="小明"))
        async with self.engine.session_state(SESSION) as state:
            self.engine.mark_chat_replied(state, SESSION)
            # 群里的这句算回应过了
            here = [str(item.get("text") or "") for item in self.engine.chat_context(state, SESSION)]
            self.assertNotIn("群里随便聊聊", here)
            # 私聊那句还原样等着
            private = [
                str(item.get("text") or "")
                for item in self.engine.chat_context(state, PRIVATE_SESSION)
            ]
            self.assertIn("在忙吗", private)

    async def test_prompt_blocks_are_scoped_to_the_current_session(self):
        """提示词里那几块都按"当前会话"分好：这里的、别处的、她能说话的地方。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        self.engine.note_session_name(SESSION, "小米粥群")
        await self.engine.note_presence(self.ctx(text="群里聊两句", user_name="小明"))
        await self.engine.note_presence(
            self.ctx(session_id=PRIVATE_SESSION, text="私聊里说的事", user_name="主人")
        )
        async with self.engine.session_state(PRIVATE_SESSION) as state:
            injection = self.engine.prompts.build_injection(
                state,
                node=self.engine.node(state.node_id) or self.engine.node(self.node_id),
                recent_chat=self.engine.chat_window(state),
                current_session=PRIVATE_SESSION,
                session_labels=self.engine.session_labels(state),
            )
        # 私聊里说的话属于「这里」，群里那句只当「别处」的背景
        here = injection.split("你在别处同时听到的")[0]
        self.assertIn("私聊里说的事", here)
        self.assertNotIn("群里聊两句", here)
        self.assertIn("你在别处同时听到的", injection)
        self.assertIn("小米粥群", injection.split("你在别处同时听到的")[1])

    async def test_her_recent_lines_carry_where_she_said_them(self):
        """「你最近说过的话」也要标出是在哪个会话说的。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        self.engine.note_session_name(SESSION, "小米粥群")
        async with self.engine.session_state(SESSION) as state:
            state.note_reply("群里那句", session_id=SESSION)
            state.note_reply("私聊那句", session_id=PRIVATE_SESSION)
            blocks = self.engine.prompts.chat_blocks(
                [],
                current_session=PRIVATE_SESSION,
                session_labels=self.engine.session_labels(state),
                recent_replies=list(state.recent_replies),
            )
        blob = "\n".join(blocks)
        self.assertIn("- 私聊那句", blob)          # 这里说的，不标
        self.assertIn("〔群 1001「小米粥群」〕群里那句", blob)  # 别处说的，标出来源

    async def test_line_for_a_place_she_cannot_reach_is_not_posted_here(self):
        """有人让她私聊他、而那个人不在她能说话的地方里：这句不说出口，也别落到群里。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        async with self.engine.session_state(SESSION) as state:
            node = self.engine.node(state.node_id)
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._execute_actions(
                state,
                node,
                outcome,
                [
                    PlannedAction(
                        type="say",
                        messages=["这事我只跟你一个人说"],
                        send_to="私聊 88888888",
                    )
                ],
                depth=0,
                autonomous=True,
            )
        self.assertEqual(outcome.messages, [])
        self.assertEqual(outcome.routed, {})
        self.assertTrue(any("不在她能说话的地方" in note for note in outcome.notes))
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, [])

    async def test_the_directory_lists_every_place_she_can_talk(self):
        """通讯录：群写群号 + 名，私聊写 QQ 号 + 昵称，备注优先于群名。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        self.engine.note_session_name(SESSION, "小米粥群")
        await self.engine.note_presence(
            self.ctx(session_id=PRIVATE_SESSION, user_id="2692047521",
                     user_name="主人", text="在干嘛")
        )
        async with self.engine.session_state(SESSION) as state:
            text = self.engine.session_directory(state)
        self.assertIn("群 1001「小米粥群」", text)
        self.assertIn("私聊 2692047521「主人」", text)
        self.assertIn("主人最近跟你说话是", text)
        # 备注优先：私聊有备注就写备注，不写平台名
        self.engine.note_session_name(PRIVATE_SESSION, "某个平台昵称")
        async with self.engine.session_state(SESSION) as state:
            again = self.engine.session_directory(state)
        self.assertIn("私聊 2692047521「主人」", again)
        self.assertNotIn("某个平台昵称", again)

    async def test_private_session_borrows_the_speaker_name(self):
        """私聊没有群名时，用最近跟她说话的那个人当名字（不然只有一串数字）。"""

        self.add_group(sessions=[SESSION, OTHER_SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "")
        await self.engine.note_presence(
            self.ctx(
                session_id=PRIVATE_SESSION,
                user_id="2692047521",
                user_name="主人",
                text="在干嘛",
            )
        )
        async with self.engine.session_state(SESSION) as state:
            text = self.engine.session_directory(state)
        self.assertIn("私聊 2692047521「主人」", text)

    async def test_plan_prompt_carries_the_directory(self):
        """计划提示词里要有「你能说话的地方」，她才挑得出来。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        async with self.engine.session_state(SESSION) as state:
            prompt = self.engine.prompts.build_autonomous_system_prompt(
                persona_text="她是一个温柔的人",
                state=state,
                node=self.engine.node(state.node_id),
                available_tools={},
                mode="plan",
                session_directory=self.engine.session_directory(state),
            )
        self.assertIn("你能说话的地方", prompt)
        self.assertIn("私聊 2692047521「主人」", prompt)
        self.assertIn("send_to", prompt)

    async def test_schedule_lands_in_the_session_it_is_pinned_to(self):
        """日程的会话多选是「落点」：勾了私聊就发私聊，而且只跑一遍。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.add_schedule(
            id="private_reminder",
            time="12:00",
            sessions=[PRIVATE_SESSION],
            action_chain=[{"type": "say", "messages": ["记得吃饭"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        sent = [session for session, _msgs in self.messenger.sent]
        self.assertEqual(self.messenger.flat_messages.count("记得吃饭"), 1)
        self.assertIn(PRIVATE_SESSION, sent)
        self.assertNotIn(SESSION, sent)

    async def test_old_per_session_state_is_adopted_when_grouping(self):
        """刚编成一组时接过她原来那份状态，别把她打回刚出生。"""

        # 先让私聊里那份"活"起来（她原来只在这个会话里过日子）
        self.store.add_session(PRIVATE_SESSION, session_type="private", platform="aiocqhttp")
        self.engine.reload_config()
        async with self.engine.session_state(PRIVATE_SESSION) as state:
            state.world_time = 900
            state.node_id = "window"
            state.energy = 0.9
        # 再把它和群编成一组：组代表是群（还没有状态）
        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        adopted = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(adopted.world_time, 900)
        self.assertEqual(adopted.node_id, "window")
        self.assertAlmostEqual(float(adopted.energy), 0.9, places=3)

    async def test_schedule_can_pin_a_session_group(self):
        """日程的落点可以是会话组：勾了组，她的话就落在组代表会话里。"""

        self.add_group(group_id="team", sessions=[SESSION, PRIVATE_SESSION])
        self.add_schedule(
            id="group_only",
            time="12:00",
            sessions=["team"],
            action_chain=[{"type": "say", "messages": ["给这一组的"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertEqual(self.messenger.flat_messages.count("给这一组的"), 1)
        self.assertEqual([item for item, _msgs in self.messenger.sent], [SESSION])

    async def test_schedule_can_pin_one_session_inside_a_group(self):
        """落点也能勾组里的某个会话：话就落在那个群 / 私聊，而不是组代表。"""

        self.add_group(group_id="team", sessions=[SESSION, PRIVATE_SESSION])
        self.add_schedule(
            id="private_only",
            time="12:00",
            sessions=[PRIVATE_SESSION],
            action_chain=[{"type": "say", "messages": ["只给私聊的"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertEqual(self.messenger.flat_messages.count("只给私聊的"), 1)
        self.assertEqual(
            [item for item, _msgs in self.messenger.sent], [PRIVATE_SESSION]
        )

    async def test_schedule_pinned_elsewhere_skips_this_group(self):
        """勾了别的会话：这一组不跑（只跑勾中的那个）。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.store.add_session(OTHER_SESSION, session_type="group", platform="aiocqhttp")
        self.engine.reload_config()
        self.add_schedule(
            id="other_only",
            time="12:00",
            sessions=[OTHER_SESSION],
            action_chain=[{"type": "say", "messages": ["只给那边的"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertEqual(self.messenger.flat_messages.count("只给那边的"), 1)
        self.assertEqual([item for item, _msgs in self.messenger.sent], [OTHER_SESSION])

    async def test_memories_stay_per_session_and_are_queried_group_wide(self):
        """记忆按会话各存一份；查的时候把组里的会话一起当条件——改组才不会乱。"""

        self.add_group(group_id="team", sessions=[SESSION, PRIVATE_SESSION])
        self.engine.memory.remember(
            session_id=SESSION,
            persona_id="",
            node_id=self.node_id,
            content="群里聊过的一件大事",
            memory_type="event",
            weight=0.9,
        )
        # 存在会话名下（不是组 id）
        rows = self.engine.memory.db.query_memories(session_id=SESSION, limit=5)
        self.assertTrue(any("一件大事" in row["content"] for row in rows))
        self.assertEqual(self.engine.memory.db.query_memories(session_id="team", limit=5), [])
        # 组里另一个会话回想得到
        recalled = self.engine.memory.recall(
            session_id=PRIVATE_SESSION, persona_id="", node_id=self.node_id, limit=5
        )
        self.assertTrue(any("一件大事" in item.content for item in recalled))

        # 改组：SESSION 退出后，私聊不再跨会话想起那段；记忆还在 SESSION 名下
        raw = self.store.raw_sessions()
        raw["groups"] = [
            {
                "id": "team",
                "name": "team",
                "sessions": [PRIVATE_SESSION],
                "main_session": PRIVATE_SESSION,
            }
        ]
        self.store.save_sessions(raw)
        self.engine.reload_config()
        recalled = self.engine.memory.recall(
            session_id=PRIVATE_SESSION, persona_id="", node_id=self.node_id, limit=5
        )
        self.assertFalse(any("一件大事" in item.content for item in recalled))
        self.assertTrue(
            any("一件大事" in row["content"] for row in self.engine.memory.db.query_memories(session_id=SESSION, limit=5))
        )

        # 再加回来，那段经历又跟着回来
        raw["groups"] = [
            {
                "id": "team",
                "name": "team",
                "sessions": [PRIVATE_SESSION, SESSION],
                "main_session": PRIVATE_SESSION,
            }
        ]
        self.store.save_sessions(raw)
        self.engine.reload_config()
        recalled = self.engine.memory.recall(
            session_id=PRIVATE_SESSION, persona_id="", node_id=self.node_id, limit=5
        )
        self.assertTrue(any("一件大事" in item.content for item in recalled))

    async def test_she_can_pin_a_new_schedule_to_one_place(self):
        """她自己排日程时也能指定落点：写「私聊主人」，就只发私聊。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        async with self.engine.session_state(SESSION) as state:
            ok, note = await self.engine.schedule_add(
                {
                    "time": "12:30",
                    "sessions": ["私聊 2692047521"],
                    "note": "主人说下午可能下雨",
                    "action_chain": [{"type": "say", "messages": ["记得收衣服"]}],
                },
                session_id=state.session_id,
            )
        self.assertTrue(ok, note)
        saved = [
            item
            for item in self.store.raw_schedules()["schedules"]
            if item.get("created_by") == "bot"
        ][0]
        self.assertEqual(saved["sessions"], [PRIVATE_SESSION])
        # 日程表里也写清落点在哪儿
        self.assertIn("落点", self.engine.schedule_text())
        self.assertIn("主人", self.engine.schedule_text())

    async def test_reply_can_carry_a_line_for_another_session(self):
        """回复里带 send_to 的那句要被交出去（以前会静默丢掉）。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        await self.engine.handle_incoming(self.ctx(text="群里说一句，再私聊说一句"))
        self.llm.replies = [
            '{"actions":[{"type":"say","messages":["群里这句"]},'
            '{"type":"say","messages":["私聊这句"],"send_to":"私聊 2692047521"}]}'
        ]
        outcome = await self.engine.handle_reply(
            self.ctx(text="群里说一句，再私聊说一句")
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.messages, ["群里这句"])
        self.assertEqual(outcome.routed.get(PRIVATE_SESSION), ["私聊这句"])

    async def test_a_reply_that_only_answers_elsewhere_still_counts(self):
        """只写了"私下补一句"、群里不吭声：这一轮也算成立，不该交回主人格。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        await self.engine.handle_incoming(self.ctx(text="别在群里说，私下告诉我"))
        self.llm.replies = [
            '{"actions":[{"type":"say","messages":["好，我单独跟你说"],'
            '"send_to":"私聊 2692047521"}]}'
        ]
        outcome = await self.engine.handle_reply(
            self.ctx(text="别在群里说，私下告诉我")
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.messages, [])
        self.assertEqual(outcome.routed.get(PRIVATE_SESSION), ["好，我单独跟你说"])

    async def test_action_followup_goes_back_where_it_started(self):
        """私聊里让她做事：做完了那句续说回私聊，不会跑到群里去。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        # 一个"1 tick 就做完、做完会补一句话"的动作（默认动作要么太慢要么没续说）
        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "test_rest",
                "name": "歇一会儿",
                "category": "continuous",
                "llm_level": "template",
                "scope": "global",
                "duration": 60,
                "duration_mode": "fixed",
                "visible": False,
                "on_complete": {"trigger": "llm_followup", "prompt_hint": "说说感觉"},
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        ctx = self.ctx(session_id=PRIVATE_SESSION, text="去给我读会儿书")
        await self.engine.handle_incoming(ctx)
        self.llm.replies = [
            '{"actions":[{"type":"say","messages":["好，本小姐这就去"]},'
            '{"type":"test_rest","duration":60}]}',
            '{"actions":[{"type":"say","messages":["读完了，眼睛有点酸"]}]}',
        ]
        outcome = await self.engine.handle_reply(ctx)
        self.assertTrue(outcome.ok, outcome.error)
        state = await self.engine.load_state(PRIVATE_SESSION, cold_start=False)
        self.assertEqual(
            str((state.current_action or {}).get("session") or ""), PRIVATE_SESSION
        )

        await self.tick_until(lambda: "读完了，眼睛有点酸" in self.messenger.flat_messages)
        sent = [item for item, _msgs in self.messenger.sent]
        self.assertIn(PRIVATE_SESSION, sent)
        self.assertNotIn(SESSION, sent)

    async def test_two_messages_in_a_row_get_one_reply(self):
        """同一个会话连发两句：先等安静期，听完了再一起回一次。"""

        self.engine.world.reply_style.merge_wait_seconds = 0.2
        await self.engine.handle_incoming(self.ctx(text="第一句：在吗"))
        second = self.engine.register_incoming(SESSION, "第二句：顺便问你件事")
        self.llm.replies = ['{"actions":[{"type":"say","messages":["都在，你说"]}]}']
        outcome = await self.engine.handle_reply(self.ctx(text="第一句：在吗"))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(second["absorbed"], "第二条应当并进这一次回复")
        prompt = str(self.llm.calls[-1]["prompt"])
        self.assertIn("第一句：在吗", prompt)
        self.assertIn("第二句：顺便问你件事", prompt)

    async def test_a_message_from_another_session_is_not_merged(self):
        """别的会话来的消息不并进这一条：跨会话并成一条会在群里答私聊的话。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.engine.world.reply_style.merge_wait_seconds = 0.2
        await self.engine.handle_incoming(self.ctx(text="群里这句"))
        record = self.engine.register_incoming(PRIVATE_SESSION, "私聊那句")
        self.llm.replies = ['{"actions":[{"type":"say","messages":["群里的回话"]}]}']
        outcome = await self.engine.handle_reply(self.ctx(text="群里这句"))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertFalse(record["absorbed"])
        self.assertNotIn("私聊那句", str(self.llm.calls[-1]["prompt"]))

    async def test_debug_previews_show_what_she_actually_sees(self):
        """调试预览要和实跑一致：通讯录、"来自哪个会话"、这里/别处的划分都要在。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        self.engine.note_session_name(SESSION, "天马行空")
        await self.engine.note_presence(
            self.ctx(session_id=PRIVATE_SESSION, text="私聊里的那句", user_name="主人")
        )
        await self.engine.note_presence(self.ctx(text="群里的那句", user_name="小明"))

        auto = await self.engine.preview_autonomous_prompt(SESSION)
        self.assertIn("你能说话的地方", auto)
        self.assertIn("私聊 2692047521「主人」", auto)
        # 私聊那句算"别处"，不混进"这里"
        self.assertIn("你在别处同时听到的", auto)
        self.assertIn("私聊里的那句", auto)

        inject = await self.engine.preview_injection(SESSION)
        self.assertIn("你在别处同时听到的", inject)
        self.assertNotIn("私聊里的那句", inject.split("你在别处同时听到的")[0])

    async def test_this_message_carries_where_it_came_from(self):
        """多会话时，当前这条消息要写明来自哪个会话。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        await self.engine.handle_incoming(
            self.ctx(session_id=PRIVATE_SESSION, text="在吗", user_name="主人")
        )
        self.llm.replies = ['{"actions":[{"type":"say","messages":["在的"]}]}']
        await self.engine.handle_reply(
            self.ctx(session_id=PRIVATE_SESSION, text="在吗", user_name="主人")
        )
        prompt = str(self.llm.calls[-1]["prompt"])
        self.assertIn("这条来自 私聊 2692047521「主人」", prompt)

    async def test_a_plugin_result_can_be_kept_as_state(self):
        """别的插件的结果（今日穿搭）存进状态槽，之后每一轮提示词里都还在。"""

        from core.ports import ToolCallResult

        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "refresh_outfit",
                "name": "刷新穿搭",
                "category": "instant",
                "llm_level": "command",
                "scope": "global",
                "trigger_command": "穿搭",
                "visible": False,
                "state_slot": "outfit",
                "state_label": "今日穿搭",
                "state_ttl_minutes": 720,
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        self.stub_commands(
            ToolCallResult(ok=True, text="白色卫衣 + 牛仔裤，配蓝色发带", tool="穿搭")
        )
        definition = self.engine.world.action_map()["refresh_outfit"]
        self.llm.replies = ["/穿搭"]
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._start_action(
                state,
                self.engine.node("study"),
                outcome,
                definition,
                PlannedAction(type="refresh_outfit", intent="换一套今天的穿搭"),
                0,
                False,
            )
        live = await self.get_state()
        self.assertIn("outfit", live.external_state)
        self.assertEqual(live.external_state["outfit"]["label"], "今日穿搭")
        self.assertIn("白色卫衣", live.external_state["outfit"]["text"])
        # 下一轮提示词里带着它（她不会说完就忘）
        prompt = self.prompts_prompt(live)
        self.assertIn("今日穿搭", prompt)
        self.assertIn("白色卫衣", prompt)

    async def test_someone_who_was_gone_gets_a_greeting_hint(self):
        """群里有人隔了大半天又冒头：提示词里提一句"这人好久没来了"，说一次就销账。"""

        ctx = self.ctx(user_id="7", user_name="小红")
        async with self.engine.session_state(SESSION) as state:
            state.user_presence["7"] = {
                "user_id": "7",
                "name": "小红",
                "last_seen": self.clock.now() - 20 * 3600,
            }
        # note_presence 自己会拿会话锁，不能在上面的 with 里调（会自己等自己）
        await self.engine.note_presence(ctx)
        async with self.engine.session_state(SESSION) as state:
            self.assertTrue(state.greet_pending.get("7"), state.greet_pending)
            hint = self.engine.extra_reminders(state)
        self.assertIn("好久没来", hint)
        self.assertIn("小红", hint)
        # 提过一次就销账，别每轮都说
        async with self.engine.session_state(SESSION) as state:
            self.assertEqual(state.greet_pending, {})
            self.assertNotIn("好久没来", self.engine.extra_reminders(state))

    async def test_greeting_is_capped_per_day(self):
        """打招呼一天最多几次：全群第 3 个人不再提醒。"""

        raw = self.store.raw_world()
        raw.setdefault("decider", {})["greet_gap_hours"] = 12
        raw["decider"]["greet_daily_max"] = 2
        self.store.save_world(raw)
        self.engine.reload_config()
        fired = 0
        for index in range(4):
            uid = f"g{index}"
            ctx = self.ctx(user_id=uid, user_name=f"路人{index}")
            async with self.engine.session_state(SESSION) as state:
                state.user_presence[uid] = {
                    "user_id": uid,
                    "name": f"路人{index}",
                    "last_seen": self.clock.now() - 30 * 3600,
                }
            await self.engine.note_presence(ctx)
            async with self.engine.session_state(SESSION) as state:
                if "好久没来" in self.engine.extra_reminders(state):
                    fired += 1
        self.assertEqual(fired, 2)

    async def test_heart_knot_is_only_about_the_person_it_was_written_for(self):
        """心事是分了人的：跟 B 说话时不该看到关于 A 的那件。"""

        ctx_a = self.ctx(user_id="1", user_name="甲")
        ctx_b = self.ctx(user_id="2", user_name="乙")
        async with self.engine.session_state(SESSION) as state:
            self.engine.note_heart_knot(state, "他上次那句话到现在还别扭", about="1")
            self.engine.note_heart_knot(state, "我自己有点在意白天那件事", about="")
            block_a = self.engine.extra_reminders(state, ctx=ctx_a)
            block_b = self.engine.extra_reminders(state, ctx=ctx_b)
            # 自主决策那一路：带上是谁的事，免得她拿着 A 的账去对 B 说
            block_auto = self.engine.extra_reminders(state, autonomy=True)
        self.assertIn("他上次那句话", block_a)
        self.assertNotIn("他上次那句话", block_b)
        # 没写对谁的心事（她自己那点事）两边都在
        self.assertIn("我自己有点在意", block_a)
        self.assertIn("我自己有点在意", block_b)
        self.assertIn("关于", block_auto)

    async def test_grudge_shows_up_only_for_that_person(self):
        """记了账就直接生效，但只对当事人：别人面前一个字不提。"""

        ctx = self.ctx(user_id="9", user_name="小刚")
        async with self.engine.session_state(SESSION) as state:
            self.engine.profiles.touch(state.session_id, "9", "小刚")
            self.engine.note_grudge(
                state, "他答应的事又没做", user_id="9", user_name="小刚"
            )
            self.assertIsNotNone(self.engine.grudge_for(state, "9"))
            prompt = self.engine.extra_reminders(state, ctx=ctx)
            self.assertIn("记着他一笔账", prompt)
            self.assertIn("他答应的事又没做", prompt)
            # 只对当事人说
            other = self.ctx(user_id="7", user_name="小红")
            self.assertNotIn("他答应的事又没做", self.engine.extra_reminders(state, ctx=other))

    async def test_grudge_cools_only_the_person_it_is_about(self):
        """记了账 → 对他这一档被压下来；同样亲密的别人一点都不变。"""

        ctx_a = self.ctx(user_id="9", user_name="小刚")
        ctx_b = self.ctx(user_id="7", user_name="小红")
        async with self.engine.session_state(SESSION) as state:
            self.engine.profiles.touch(state.session_id, "9", "小刚")
            self.engine.profiles.note_bond(
                state.session_id, "9", type="男友", asserted_by="测试"
            )
            self.engine.profiles.adjust_affinity(state.session_id, "9", 40, reason="测试")
            self.engine.profiles.touch(state.session_id, "7", "小红")
            # 小红只是群友（好感拉满也只能到"熟人"），用来对照"没账的那个人不变"
            self.engine.profiles.note_bond(
                state.session_id, "7", type="群友", asserted_by="测试"
            )
            self.engine.profiles.adjust_affinity(state.session_id, "7", 95, reason="测试")
            self.assertIn("你对他：特别的人", self.engine.profile_block(state, ctx_a))
            self.assertIn("你对他：熟人", self.engine.profile_block(state, ctx_b))
            self.engine.note_grudge(state, "当众让我下不来台", user_id="9", user_name="小刚")
            cold = self.engine.profile_block(state, ctx_a)
            warm_other = self.engine.profile_block(state, ctx_b)
        # 好感还是 95（数值没动），但"生效档位"被他这笔账压下来一档
        self.assertIn("你对他：亲近", cold)
        self.assertNotIn("你对他：特别的人", cold)
        self.assertIn("你对他：熟人", warm_other)

    async def test_forgive_clears_the_grudge_and_writes_it_down(self):
        """他道歉了 → 这笔账划掉，而且留下一条她自己的记忆。"""

        async with self.engine.session_state(SESSION) as state:
            self.engine.profiles.touch(state.session_id, "9", "小刚")
            self.engine.note_grudge(state, "他放了我鸽子", user_id="9", user_name="小刚")
            self.assertTrue(self.engine.resolve_grudge(state, user_id="9"))
            self.assertIsNone(self.engine.grudge_for(state, "9"))
            # 再算一次：已经没有账了
            self.assertFalse(self.engine.resolve_grudge(state, user_id="9"))
        rows = self.engine.memory.db.query_memories(session_id=SESSION, limit=20)
        self.assertTrue(
            any("算清了一笔账" in str(item.get("content") or "") for item in rows), rows
        )

    async def test_grudge_quota_is_one_new_grudge_a_day(self):
        """一天最多记一笔新的：不然她会变成天天记仇的人。"""

        async with self.engine.session_state(SESSION) as state:
            self.engine.profiles.touch(state.session_id, "9", "小刚")
            self.assertTrue(
                self.engine.note_grudge(state, "第一笔", user_id="9", user_name="小刚")
            )
            self.assertFalse(
                self.engine.note_grudge(state, "第二笔", user_id="7", user_name="小红")
            )
            self.assertEqual(len(state.grudges), 1)

    async def test_own_topic_is_recorded_then_crossed_off(self):
        """她自己的事：答应过的记着，做完了划掉。"""

        ctx = self.ctx(user_id="9", user_name="小刚")
        async with self.engine.session_state(SESSION) as state:
            self.engine.note_own_topic(
                state, "答应给他看照片", user_id="9", user_name="小刚"
            )
            prompt = self.engine.extra_reminders(state, ctx=ctx)
            self.assertIn("你自己还记着的事", prompt)
            self.assertIn("答应给他看照片", prompt)
            # 对别人不说这件事
            other = self.ctx(user_id="7", user_name="小红")
            self.assertNotIn("答应给他看照片", self.engine.extra_reminders(state, ctx=other))
            # 做完划掉
            self.assertEqual(self.engine.mark_own_topic_done(state, user_id="9"), 1)
            self.assertEqual(state.own_topics, [])
            self.assertEqual(self.engine.mark_own_topic_done(state, user_id="9"), 0)

    async def test_own_topic_is_dropped_after_a_few_days(self):
        """放太久的自己那点事：丢掉，别当成陈年待办。"""

        async with self.engine.session_state(SESSION) as state:
            self.engine.note_own_topic(state, "想做顿饭", user_id="9")
            state.own_topics[0]["at"] = self.clock.now() - 5 * 86400
            self.engine._decay_own_topics(state, now=self.clock.now())
            self.assertEqual(state.own_topics, [])

    async def test_prompt_asks_for_grudge_and_own_topic_fields(self):
        """输出格式里得有这几个字段，模型才知道能写。"""

        spec = self.engine.prompts.format_layer(mode="actions", max_messages=3)
        for field in ("grudge", "forgive", "own_topic", "own_topic_done"):
            self.assertIn(f'"{field}"', spec)
        self.assertIn("关于 grudge", spec)
        self.assertIn("关于 own_topic", spec)

    async def test_reply_can_record_a_grudge_and_her_own_promise(self):
        """模型给的 grudge / own_topic / forgive 要真的落地，不只是能解析。"""

        self.llm.replies = [
            '{"grudge":"他答应的事又没做","own_topic":"答应给他看照片",'
            '"actions":[{"type":"say","messages":["哼"]}]}'
        ]
        ctx = self.ctx(text="抱歉我又忘了", user_id="9", user_name="小刚")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        state = await self.get_state()
        self.assertIsNotNone(self.engine.grudge_for(state, "9"))
        self.assertEqual(len(state.own_topics), 1)
        self.assertIn("看照片", state.own_topics[0]["text"])

        # 他补上了 → forgive 把那笔账划掉
        self.llm.replies = ['{"forgive":true,"actions":[{"type":"say","messages":["算了"]}]}']
        ctx2 = self.ctx(text="对不起，我这就补上", user_id="9", user_name="小刚")
        await self.engine.handle_incoming(ctx2)
        await self.engine.handle_reply(ctx2)
        state = await self.get_state()
        self.assertIsNone(self.engine.grudge_for(state, "9"))

    async def test_heart_knot_is_kept_then_fades(self):
        """心事挂着、会淡；淡没了就写进记忆放下。"""

        async with self.engine.session_state(SESSION) as state:
            self.engine.note_heart_knot(state, "他上次那句话到现在还别扭")
            block = self.engine._heart_knot_block(state)
            self.assertIn("还别扭", block)
            # 淡到没：把强度直接压到快没，再过一小时就该退出
            state.heart_knots[0]["strength"] = 0.03
            state.heart_knots[0]["at"] = self.clock.now() - 3600
            self.engine._decay_heart_knots(state, now=self.clock.now())
            self.assertEqual(state.heart_knots, [])
            self.assertEqual(self.engine._heart_knot_block(state), "")
        rows = self.engine.memory.db.query_memories(session_id=SESSION, limit=20)
        self.assertTrue(
            any("心里搁过的事" in str(item.get("content") or "") for item in rows),
            rows,
        )

    async def test_ask_about_only_when_the_relationship_is_close(self):
        """主动问只在关系熟了之后：刚认识就问生日很像查户口。"""

        ctx = self.ctx(user_id="9", user_name="小刚")
        async with self.engine.session_state(SESSION) as state:
            self.engine.profiles.touch(state.session_id, "9", "小刚")
            # 陌生人这一档：不问
            self.assertEqual(self.engine._ask_about_hint(state, ctx), "")
            # 拉到"朋友"档：开始问，而且一次只提一件
            self.engine.profiles.note_bond(state.session_id, "9", type="朋友", asserted_by="测试")
            self.engine.profiles.adjust_affinity(state.session_id, "9", 40, reason="测试")
            first = self.engine._ask_about_hint(state, ctx)
            self.assertIn("还不知道他的", first)
            # 同一件东西有冷却：紧接着再算一次不会重复追问
            self.assertEqual(self.engine._ask_about_hint(state, ctx), "")

    async def test_curiosity_can_send_her_to_ask_instead_of_only_searching(self):
        """好奇心高的时候，除了上网查，也能推动她"去问问人"。

        两件事一起钉住：好奇心低就不提；提也只走自主决策那一路——
        回话时把这段掺进去，她会突然打听第三个人的生日。
        """

        async with self.engine.session_state(SESSION) as state:
            self.engine.profiles.touch(state.session_id, "9", "小刚")
            self.engine.profiles.note_bond(
                state.session_id, "9", type="朋友", asserted_by="测试"
            )
            self.engine.profiles.adjust_affinity(state.session_id, "9", 40, reason="测试")
            # 好奇心低：这事想不起来
            state.curiosity = 0.4
            self.assertEqual(self.engine._ask_about_block(state), "")
            # 好奇心上来了：给出"想问谁 + 问什么"
            state.curiosity = 0.8
            self.assertNotIn("好奇心上来了", self.engine.extra_reminders(state))
            block = self.engine.extra_reminders(state, autonomy=True)
            self.assertIn("好奇心上来了", block)
            self.assertIn("小刚", block)
            # 对同一个人一天只提一件：紧接着再算不会重复问
            self.assertEqual(self.engine._ask_about_block(state), "")

    async def test_ask_about_stays_silent_when_it_is_switched_off(self):
        """关掉「主动问」之后，好奇心再高也不会冒出这段提示。"""

        async with self.engine.session_state(SESSION) as state:
            self.engine.profiles.touch(state.session_id, "9", "小刚")
            self.engine.profiles.note_bond(
                state.session_id, "9", type="朋友", asserted_by="测试"
            )
            self.engine.profiles.adjust_affinity(state.session_id, "9", 40, reason="测试")
            state.curiosity = 0.9
            self.engine.world.profile.ask_about_enabled = False
            try:
                self.assertEqual(self.engine._ask_about_block(state), "")
            finally:
                self.engine.world.profile.ask_about_enabled = True

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

    async def test_daytime_low_energy_naps_instead_of_sleeping_it_off(self):
        """白天精力低就小睡一会儿：一睡八小时的话，醒来正好是半夜。"""

        self.engine.decider.rng = _NoLlmDraw()
        self.clock.set_struct(datetime(2026, 9, 10, 15, 0))
        await self.set_state(node_id="bedroom", energy=0.2, loneliness=0.2, boredom=0.2)

        await self.engine.maybe_decide(SESSION)

        state = await self.get_state()
        action = state.current_action or {}
        self.assertEqual(action.get("type"), "nap")
        # 睡多久不是拍脑袋：按小睡动作自己配的恢复速度算出来
        self.assertGreater(int(action.get("duration_ticks") or 0), 0)

    async def test_night_low_energy_sleeps_through_the_night(self):
        self.engine.decider.rng = _NoLlmDraw()
        self.clock.set_struct(datetime(2026, 9, 10, 2, 0))
        await self.set_state(node_id="bedroom", energy=0.2, loneliness=0.2, boredom=0.2)

        await self.engine.maybe_decide(SESSION)

        state = await self.get_state()
        self.assertEqual((state.current_action or {}).get("type"), "sleep")

    async def test_sleep_is_not_offered_in_the_daytime_prompt(self):
        """白天提示词里不带「睡觉」，解析白名单也挡掉；夜里照常给。"""

        self.clock.set_struct(datetime(2026, 9, 10, 15, 0))
        await self.set_state(node_id="bedroom")
        day_prompt = await self.engine.preview_autonomous_prompt(SESSION)
        self.assertNotIn("sleep：", day_prompt)
        self.assertNotIn("sleep:", day_prompt)
        self.assertNotIn("sleep", self.engine._parseable_action_ids("bedroom"))

        self.clock.set_struct(datetime(2026, 9, 10, 23, 40))
        night_prompt = await self.engine.preview_autonomous_prompt(SESSION)
        self.assertIn("sleep", self.engine._parseable_action_ids("bedroom"))
        self.assertIn("sleep", night_prompt)

    async def test_sleep_can_be_allowed_around_the_clock(self):
        """关掉「睡觉只在夜里」之后，白天也能写。"""

        raw = self.store.raw_world()
        raw["night"]["sleep_only_at_night"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 9, 10, 15, 0))
        await self.set_state(node_id="bedroom")

        self.assertIn("sleep", self.engine._parseable_action_ids("bedroom"))

    async def test_action_quota_hides_the_action_once_exhausted(self):
        """动作配额用尽后：提示词里不列、解析也拒、计划排到就跳过；换天恢复。"""

        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "greet_once",
                "name": "打个招呼",
                "category": "instant",
                "llm_level": "template",
                "template": "（{bot}挥了挥手）",
                "scope": "global",
                "visible": True,
                "quota": {"day": 1},
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        definition = self.engine.world.action_map()["greet_once"]
        day_key = self.engine._usage_keys()["day"]
        node = self.engine.node("study")

        # 没用过时：照常出现在提示词里
        self.assertIn("greet_once", self.engine.prompts.scene_layer(node, {}))

        async with self.engine.session_state(SESSION) as state:
            state.action_usage = {"greet_once": {day_key: 1}}
            self.assertEqual(self.engine.action_quota_left(state, definition), 0)
            self.assertIn("greet_once", self.engine.exhausted_actions(state))
            blocked = self.engine.exhausted_actions(state)

        # 提示词里不列、解析也不接受
        self.assertNotIn(
            "greet_once", self.engine.prompts.scene_layer(node, {}, blocked)
        )
        self.assertNotIn(
            "greet_once", self.engine._parseable_action_ids("study", blocked=blocked)
        )

        # 计划里排到它也会被跳过
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._execute_actions(
                state,
                node,
                outcome,
                [PlannedAction(type="greet_once")],
                depth=0,
                autonomous=True,
            )
        self.assertEqual(outcome.messages, [])
        self.assertTrue(
            any("次数" in str(note) for note in outcome.notes), outcome.notes
        )

        # 换到第二天：重新可用了
        self.clock.set_struct(datetime(2026, 9, 11, 12, 0))
        async with self.engine.session_state(SESSION) as state:
            self.assertGreater(self.engine.action_quota_left(state, definition), 0)

    async def test_staying_up_at_night_costs_more_energy(self):
        """熬夜代价：夜里醒着按倍数掉精力；睡着不算熬，白天也不算。"""

        from core.engine import STATE_NAPPING, STATE_SLEEPING

        night = WorldState(session_id=SESSION, world_time=10, state="idle")
        day = WorldState(session_id=SESSION, world_time=10, state="idle")
        asleep = WorldState(session_id=SESSION, world_time=10, state=STATE_SLEEPING)
        napping = WorldState(session_id=SESSION, world_time=10, state=STATE_NAPPING)
        delayed = WorldState(session_id=SESSION, world_time=10, state="idle")
        delayed.stay_up_until = 30

        self.clock.set_struct(datetime(2026, 9, 10, 3, 0))
        self.assertGreater(self.engine._stay_up_penalty(night), 1.0)
        self.assertGreater(self.engine._stay_up_penalty(asleep), 0.0)
        self.assertEqual(self.engine._stay_up_penalty(asleep), 1.0)
        self.assertEqual(self.engine._stay_up_penalty(napping), 1.0)
        # 夜里本来就该付熬夜代价，取较大值不会叠加
        self.assertGreater(self.engine._stay_up_penalty(delayed), 1.0)

        self.clock.set_struct(datetime(2026, 9, 10, 15, 0))
        self.assertEqual(self.engine._stay_up_penalty(day), 1.0)
        # 为事件推迟睡觉时，白天也算熬夜代价（原来那条规则保留）
        self.assertGreater(self.engine._stay_up_penalty(delayed), 1.0)

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
        # 自己把额度定死：这条测的是"触顶就不动"，不该跟着默认值一起漂
        raw = self.store.raw_world()
        raw.setdefault("limits", {})["max_autonomous_per_hour"] = 5
        self.store.save_world(raw)
        self.engine.reload_config()
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

    async def test_long_tool_result_is_kept_long_in_the_log(self):
        """工具返回一长段时，日志里要留得下大半，别只剩开头一小截。"""

        self.add_schedule(
            id="search_log",
            time="12:00",
            action_chain=[{"type": "search_web", "intent": "今天有什么新闻"}],
        )
        await self.set_state(node_id="study")
        body = "".join(f"第{index:03d}条结果。" for index in range(100))
        self.tools.results["web_search"] = body
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.run_schedules()
        events = await self.db.call("query_events", session_id=SESSION, limit=30)
        results = [item for item in events if item["event_type"] == "tool_result"]
        self.assertTrue(results, events)
        logged = results[0]["detail"]["result"]
        self.assertEqual(logged, body)
        self.assertIn("第099条结果", logged)

    async def test_over_long_tool_result_says_how_much_was_cut(self):
        """真的超长时也要标明截断，免得以为工具只返回了这么点。"""

        self.add_schedule(
            id="search_log",
            time="12:00",
            action_chain=[{"type": "search_web", "intent": "今天有什么新闻"}],
        )
        await self.set_state(node_id="study")
        self.tools.results["web_search"] = "长" * 3000
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        await self.engine.run_schedules()
        events = await self.db.call("query_events", session_id=SESSION, limit=30)
        results = [item for item in events if item["event_type"] == "tool_result"]
        self.assertTrue(results, events)
        logged = results[0]["detail"]["result"]
        self.assertIn("完整 3000 字", logged)
        self.assertLess(len(logged), 1300)

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

    # ---------------- 情绪两轴 → 表达方式 ----------------

    async def test_style_cell_follows_her_mood(self):
        """心情差 + 情绪高 → 「憋着火」格：只允许 1 条，而且要她少说多做。"""

        await self.set_state(affect=0.8, valence=0.15)
        state = await self.get_state()
        cell, limit, text = self.engine.style_for(state, SESSION)
        self.assertIsNotNone(cell)
        self.assertEqual(cell.key, "a4v0")
        self.assertEqual(limit, 1)
        self.assertIn("这一轮的表达方式", text)
        self.assertIn("最多说 1 条", text)
        self.assertEqual(state.last_style_cell, "a4v0")

    async def test_mood_cause_is_recorded_and_expires(self):
        """心情标签会带上"为什么"：被哄了一下 → 记下来；放太久就不再挂着了。

        口吻现在由主模型的 ``tone`` 判（关键词表只兜底），所以这条路径要过一遍回复。
        """

        self.llm.replies = [
            '{"tone":"hug","actions":[{"type":"say","messages":["诶嘿～"]}]}'
        ]
        await self.set_state(affect=0.5, valence=0.5)
        ctx = self.ctx(text="抱抱你")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        state = await self.get_state()
        self.assertEqual(state.mood_cause, "被哄了一下")
        self.assertIn("因为：被哄了一下", state.mood)

        # 半小时后：来源不再算数，心情标签回到纯状态
        async with self.engine.session_state(SESSION) as live:
            live.mood_cause_at = self.engine._now() - 3600
            live.mood = self.engine.dynamics.derive_mood(live)
        self.assertNotIn("因为：", live.mood)

    async def test_group_chat_caps_replies_at_two(self):
        """群聊里最多 2 条：连发 3 条在群里已经很显眼，还容易撞上发送冷却。"""

        await self.set_state(affect=0.9, valence=0.9)
        state = await self.get_state()
        cell, limit, _text = self.engine.style_for(state, SESSION)
        self.assertEqual(cell.key, "a4v4")
        self.assertEqual(limit, 2)

    async def test_style_can_be_turned_off(self):
        raw = self.store.raw_world()
        raw["style_injection"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        state = await self.get_state()
        cell, limit, text = self.engine.style_for(state, SESSION)
        self.assertIsNone(cell)
        self.assertEqual(text, "")
        self.assertEqual(limit, 3)
        self.assertEqual(state.last_style_cell, "")

    async def test_say_messages_are_not_cut_by_the_style_cell(self):
        """模型一口气写三条就发三条：条数只提醒，不截断（提示词负责让她收敛）。"""

        self.llm.replies = [
            '{"actions":[{"type":"say","messages":["第一句","第二句","第三句"]}]}'
        ]
        ctx = self.ctx(text="在吗")
        await self.engine.handle_incoming(ctx)
        outcome = await self.engine.handle_reply(ctx)
        self.assertEqual(outcome.messages, ["第一句", "第二句", "第三句"])
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        replies = [item for item in events if item["event_type"] == "reply"]
        self.assertTrue(replies)
        self.assertEqual(replies[0]["detail"]["say_limit"], 2)
        self.assertTrue(replies[0]["detail"]["style_cell"])
        self.assertTrue(
            any("比这一轮建议的" in str(item) for item in replies[0]["detail"]["warnings"]),
            replies[0]["detail"]["warnings"],
        )

    # ---------------- 数值历史与插话闸门统计 ----------------

    async def test_passive_replies_are_capped_per_hour(self):
        """本小时被动回复触顶后：接管但静默（静默就是什么都不发，也不落回主人格）。"""

        raw = self.store.raw_world()
        raw.setdefault("limits", {})["max_replies_per_hour"] = 2
        self.store.save_world(raw)
        self.engine.reload_config()

        for index in range(2):
            ctx = self.ctx(text=f"第{index}句")
            await self.engine.handle_incoming(ctx)
            outcome = await self.engine.handle_reply(ctx)
            self.assertTrue(outcome.ok, outcome.error)
        async with self.engine.session_state(SESSION) as state:
            self.assertEqual(self.engine.reply_quota(state), (2, 2))

        ctx = self.ctx(text="第三句")
        await self.engine.handle_incoming(ctx)
        outcome = await self.engine.handle_reply(ctx)
        self.assertFalse(outcome.ok)
        self.assertIn("上限", outcome.error)
        self.assertEqual(outcome.messages, [])

        # 跨到下一个小时就恢复
        await self.set_state(world_time=61)
        ctx = self.ctx(text="下个小时")
        await self.engine.handle_incoming(ctx)
        outcome = await self.engine.handle_reply(ctx)
        self.assertTrue(outcome.ok, outcome.error)

    async def test_every_tick_records_a_value_snapshot(self):
        await self.engine.tick()
        rows = await self.db.call(
            "query_state_history", session_id=SESSION, since=0.0, limit=50
        )
        self.assertTrue(rows, rows)
        self.assertIn("affect", rows[-1])
        self.assertIn("valence", rows[-1])
        self.assertGreater(rows[-1]["at"], 0)

    async def test_history_summarises_the_window(self):
        base = self.clock.now()
        for index in range(6):
            await self.db.call(
                "add_state_history",
                session_id=SESSION,
                world_time=index,
                at=base + index * 600,
                values={
                    "affect": 0.2 + index * 0.1,
                    "valence": 0.6 - index * 0.1,
                    "energy": 0.5,
                    "loneliness": 0.5,
                    "curiosity": 0.5,
                    "boredom": 0.3,
                },
            )
        self.clock.advance(3600)
        data = await self.engine.state_history(SESSION, hours=1)
        self.assertEqual(len(data["points"]), 6)
        metrics = data["metrics"]
        self.assertGreater(metrics["swing_per_hour"], 0)
        self.assertAlmostEqual(metrics["peak_arousal"], 0.7, places=4)
        # 后两帧效价低于 0.35，各占 10 分钟
        self.assertGreater(metrics["low_minutes"], 0)

    async def test_interject_gate_reports_which_one_blocks(self):
        """插话被拦住时要能说清是哪一道闸。"""

        state = await self.get_state()
        self.assertEqual(self.engine.interject_gate(state), "")
        await self.set_state(last_interject_at=self.clock.now())
        state = await self.get_state()
        self.assertEqual(self.engine.interject_gate(state), "cooldown")
        await self.set_state(last_interject_at=0.0, cooldown_until=999999)
        state = await self.get_state()
        self.assertEqual(self.engine.interject_gate(state), "engage")
        await self.set_state(cooldown_until=0, autonomous_count_hour=99)
        state = await self.get_state()
        self.assertEqual(self.engine.interject_gate(state), "hourly")

    async def test_blocked_interjections_are_counted(self):
        """她想插话却被拦住时按闸门计数：光看频率看不出是谁在限流。"""

        async with self.engine.session_state(SESSION) as live:
            for index in range(3):
                live.note_chat(
                    user_id=f"u{index}",
                    name="小明",
                    text=f"第 {index} 句",
                    now=self.clock.now(),
                    keep=12,
                )
        await self.set_state(loneliness=0.95, last_interject_at=self.clock.now())
        await self.engine.maybe_decide(SESSION, force=True)
        state = await self.get_state()
        self.assertEqual(state.interject_stats.get("cooldown"), 1)
        self.assertTrue(state.interject_stats)

    # ---------------- 生活事件与心情 ----------------

    async def test_ignored_only_sours_the_mood(self):
        """被冷落只压心情，不抬心潮：否则她会从"退缩"直接跳成"发作"。"""

        async with self.engine.session_state(SESSION) as live:
            before_affect = live.affect
            before_valence = live.valence
            self.engine.dynamics.apply_event(live, "ignored", now=self.clock.now())
            after_affect = live.affect
            after_valence = live.valence
        self.assertLess(after_valence, before_valence)
        self.assertLessEqual(after_affect, before_affect + 1e-6)

    async def test_ignored_penalty_softens_then_resets(self):
        """第一次被冷落最疼，之后递减，隔一阵重新算。"""

        state = await self.get_state()
        now = self.clock.now()
        magnitudes = [
            self.engine.dynamics.ignored_magnitude(state, now=now + index * 60)
            for index in range(3)
        ]
        self.assertEqual(magnitudes, [1.0, 0.5, 0.0])
        self.assertEqual(
            self.engine.dynamics.ignored_magnitude(state, now=now + 3600), 1.0
        )

    async def test_tool_failure_sours_her_mood(self):
        """工具调用失败 = 挫败感：线上一眼看不见，但确实影响心情。"""

        self.tools.failures["web_search"] = "调用出错：连接超时"
        self.add_schedule(
            id="fail_news",
            time="12:00",
            action_chain=[{"type": "search_web", "params": {"query": "今天的新闻"}}],
        )
        await self.set_state(node_id="study")
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        before = (await self.get_state()).valence
        await self.engine.run_schedules()
        after = (await self.get_state()).valence
        self.assertLess(after, before)

    async def test_silent_interruption_leaves_a_trace(self):
        """正在做的事被顶掉时，日志里要留痕，心情上也要有反应。"""

        await self.set_state(
            node_id="kitchen",
            current_action={
                "type": "cook",
                "duration_ticks": 30,
                "elapsed_ticks": 5,
                "desc": "做饭",
            },
        )
        before = (await self.get_state()).valence
        self.llm.replies = [
            '{"actions":[{"type":"walk_to","target_node":"window"}]}'
        ]
        ctx = self.ctx(text="去窗边")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        state = await self.get_state()
        self.assertEqual((state.current_action or {}).get("type"), "walk_to")
        self.assertLess(state.valence, before)
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        interrupts = [item for item in events if item["event_type"] == "interrupt"]
        self.assertTrue(interrupts, events)
        self.assertEqual(interrupts[0]["detail"]["action"], "cook")

    async def test_model_valence_delta_moves_her_mood(self):
        """主模型按提示词给的 valence_delta 会真的改变她的心情。"""

        self.llm.replies = [
            '{"valence_delta":-0.9,"actions":[{"type":"say","messages":["……"]}]}'
        ]
        before = (await self.get_state()).valence
        ctx = self.ctx(text="你好烦")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        state = await self.get_state()
        self.assertLess(state.valence, before)
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        replies = [item for item in events if item["event_type"] == "reply"]
        self.assertEqual(replies[0]["detail"]["valence_delta"], -0.9)

    async def test_chat_cannot_fill_her_mood_by_itself(self):
        """聊一整天也推不满效价：单轮有上限、当天有总额度。"""

        self.llm.replies = [
            '{"valence_delta":1.0,"actions":[{"type":"say","messages":["好开心呀"]}]}'
        ]
        before = (await self.get_state()).valence
        for index in range(12):
            ctx = self.ctx(text=f"夸她第 {index} 遍", user_id=f"u{index}")
            await self.engine.handle_incoming(ctx)
            await self.engine.handle_reply(ctx)
        state = await self.get_state()
        self.assertLess(state.valence, 0.9, state.valence)
        # 一天里聊天推动的效价不超过配置的额度
        cap = float(self.engine.world.state_dynamics.chat_valence_daily_cap)
        self.assertLessEqual(state.chat_valence_spent, cap + 1e-6)
        self.assertLessEqual(state.valence - before, cap + 0.12)

    async def test_lively_group_stirs_her_up(self):
        """群里突然热闹起来：轻微带动一下心潮。"""

        async with self.engine.session_state(SESSION) as live:
            self.assertFalse(self.engine._group_is_lively(live, now=self.clock.now()))
            for index in range(5):
                live.note_chat(
                    user_id=f"u{index}",
                    name="小明",
                    text=f"第 {index} 句",
                    now=self.clock.now(),
                    keep=12,
                )
            self.assertTrue(self.engine._group_is_lively(live, now=self.clock.now()))

    async def test_poke_logs_which_route_worked(self):
        """群聊和私聊走的不是同一条路：日志里要能看出这次是哪条通的。"""

        await self.set_state(node_id="study")
        self.use_action_chain([{"type": "poke", "target": "3397734465"}])
        await self.engine.run_schedules()

        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        pokes = [item for item in events if item["event_type"] == "poke"]
        self.assertTrue(pokes, events)
        self.assertTrue(pokes[0]["detail"]["ok"])
        self.assertEqual(pokes[0]["detail"]["route"], "测试通道")

    async def test_poke_failure_is_logged_with_the_reason(self):
        """戳不动时要留下原因，而不是只看到她变成固定文案。"""

        self.messenger.poke_result = False
        await self.set_state(node_id="study")
        self.use_action_chain([{"type": "poke", "target": "3397734465"}])
        outcomes = await self.engine.run_schedules()

        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        pokes = [item for item in events if item["event_type"] == "poke"]
        self.assertTrue(pokes, events)
        self.assertFalse(pokes[0]["detail"]["ok"])
        self.assertIn("戳不动", pokes[0]["detail"]["note"])
        # 失败时退化成动作自己的文案（这一轮要说的话）
        self.assertTrue(outcomes[0].messages, outcomes[0].notes)

    async def test_same_second_messages_are_still_fresh(self):
        """时钟精度只有秒：她刚说完的同一秒里进来的消息不能被当成"已回应过"。"""

        ctx = self.ctx(text="第一句")
        await self.engine.handle_incoming(ctx)
        self.llm.replies = ['{"actions":[{"type":"say","messages":["嗯"]}]}']
        await self.engine.handle_reply(ctx)
        # 水位线由"确实发出去"那一步推进
        await self.engine.mark_chat_replied_by_session(SESSION)
        # 同一个时间戳（测试里时钟是冻结的）再来一条
        await self.engine.handle_incoming(self.ctx(text="第二句"))
        state = await self.get_state()
        fresh = " ".join(str(item.get("text")) for item in self.engine.chat_context(state))
        self.assertIn("第二句", fresh)
        self.assertNotIn("第一句", fresh)

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
        self.assertEqual(
            len(carried), min(20, int(self.engine.world.decider.chat_max_messages))
        )
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
        self.assertEqual(
            self.engine.chat_summary_for(state, SESSION),
            "大家主要在聊周末去哪玩，顺便问了她在不在。",
        )
        self.assertEqual(len(state.recent_chat), 4)

        # 摘要会出现在提示词里
        injection = await self.engine.preview_injection(SESSION)
        self.assertIn("更早聊过的", injection)

    async def test_summaries_are_kept_per_session(self):
        """群聊和私聊的「更早聊过的」各自成段：压缩只喂本会话的内容。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        self.set_context(
            chat_history_max=200,
            chat_overflow="compress",
            chat_compress_threshold=10,
            chat_keep_after_compress=2,
            summary_refresh_minutes=1,
        )
        for index in range(12):
            await self.engine.note_presence(
                self.ctx(text=f"群里第{index}句", user_id="1", user_name="小明")
            )
        for index in range(12):
            await self.engine.note_presence(
                self.ctx(
                    session_id=PRIVATE_SESSION,
                    text=f"私聊第{index}句",
                    user_id="2",
                    user_name="主人",
                )
            )
        self.llm.replies = ["群里在聊周末去哪玩。", "私聊里在闹别扭。"]
        await self.engine.tick()

        state = await self.get_state()
        self.assertEqual(self.engine.chat_summary_for(state, SESSION), "群里在聊周末去哪玩。")
        self.assertEqual(
            self.engine.chat_summary_for(state, PRIVATE_SESSION), "私聊里在闹别扭。"
        )
        # 压群里那一份时，提示词里不该混进私聊的内容（反过来也一样）
        prompts = [str(call.get("prompt") or "") for call in self.llm.calls]
        group_prompt = next((item for item in prompts if "群里第" in item), "")
        self.assertTrue(group_prompt)
        self.assertNotIn("私聊第", group_prompt)

    async def test_the_two_chat_views_use_the_same_unit(self):
        """"插话判定 / 工具选型"的视图和进提示词的视图必须同一套口径（都按行）。"""

        now = self.engine._now()
        async with self.engine.session_state(SESSION) as state:
            for index in range(12):
                state.note_chat(
                    user_id="7",
                    name="小明",
                    text=f"第{index}句",
                    now=now - 60 + index,
                    keep=200,
                    origin=SESSION,
                )
            context_rows = self.engine.chat_context(state, SESSION)
            window_rows = self.engine.chat_window(state, SESSION)

        # 12 条同一个人连着说的 = 1 行：两个视图都只该看到 1 行（不是 12 条）
        self.assertEqual(len(group_chat_items(context_rows)), 1)
        self.assertEqual(len(group_chat_items(window_rows)), 1)
        self.assertEqual(len(context_rows), 12)

    async def test_the_snapshot_counts_match_what_she_sees(self):
        """编辑器那两个数要跟"她这一轮实际看到的行"对得上（按本会话 + 水位线算）。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        await self.engine.handle_incoming(self.ctx(text="第一句", user_id="7", user_name="小明"))
        await self.engine.handle_incoming(
            self.ctx(
                session_id=PRIVATE_SESSION, text="私聊里的一句", user_id="9", user_name="主人"
            )
        )
        await self.engine.handle_incoming(self.ctx(text="第二句", user_id="7", user_name="小明"))

        snapshot = await self.engine.snapshot(SESSION)
        async with self.engine.session_state(SESSION) as state:
            view = self.engine.chat_window(state, SESSION)
            here = [item for item in view if self.engine._chat_origin(item, state) == SESSION]
            until, seq = self.engine._watermark(state, SESSION)
            fresh = [
                item
                for item in here
                if chat_item_is_fresh(item, replied_until=until, replied_seq=seq)
            ]

        self.assertEqual(snapshot["chat_unreplied_count"], len(group_chat_items(fresh)))
        # 私聊那句不算进"本会话留档"
        self.assertEqual(snapshot["chat_history_count"], len(here))

    async def test_the_prompt_respects_the_three_line_budgets(self):
        """三块各自的上限都来自配置，提示词里的行数不会超。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        async with self.engine.session_state(SESSION) as state:
            self.engine.world.context.chat_lines = 3
            self.engine.world.context.chat_elsewhere_lines = 2
            now = self.engine._now()
            for index in range(10):
                state.note_chat(
                    user_id="7",
                    name="小明",
                    text=f"群里第{index}句",
                    # 每条间隔 350 秒（超过 5 分钟的合并窗口）→ 各算一行
                    now=now - 3300 + index * 350,
                    keep=200,
                    origin=SESSION,
                )
            for index in range(10):
                state.note_chat(
                    user_id="9",
                    name="主人",
                    text=f"私聊第{index}句",
                    now=now - 3300 + index * 350,
                    keep=200,
                    origin=PRIVATE_SESSION,
                )
            rows = self.engine.chat_window(state, SESSION)

        here = [item for item in rows if item.get("origin") == SESSION]
        elsewhere = [item for item in rows if item.get("origin") == PRIVATE_SESSION]
        self.assertEqual(len(group_chat_items(here)), 3)
        self.assertEqual(len(group_chat_items(elsewhere)), 2)

    async def test_recent_events_skip_pure_chatter(self):
        """「最近发生的事」只列真的做了什么：说话 / 想事情 / 分享 不占位置。"""

        async with self.engine.session_state(SESSION) as state:
            state.add_event("action", {"type": "say", "visible": True})
            state.add_event("action_start", {"type": "say"})
            state.add_event("action", {"type": "think", "visible": False})
            state.add_event("action", {"type": "share", "visible": True})
            state.add_event("move", {"to": "study"})
            state.add_event("action", {"type": "stretch", "visible": True})

        prompt = await self.engine.preview_autonomous_prompt(SESSION)
        lines = prompt.splitlines()
        start = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("# 最近发生的事")
        )
        block: list[str] = []
        for line in lines[start + 1 :]:
            if line.startswith("#"):
                break
            block.append(line)
        body = "\n".join(block)
        self.assertIn("伸懒腰", body)
        self.assertIn("走到了", body)
        self.assertNotIn("说话", body)
        self.assertNotIn("想事情", body)
        self.assertNotIn("分享", body)

        # 编辑器看的原始记录照旧完整：她做过什么都要能查到
        state = await self.engine.load_state(SESSION, cold_start=False)
        kinds = [
            str((item.get("detail") or {}).get("type") or "")
            for item in state.recent_events
        ]
        self.assertIn("say", kinds)

    async def test_recent_events_skip_raw_records(self):
        """碎片记录（发言等待回应之类）也不该进「最近发生的事」，更不该糊原始数据。"""

        async with self.engine.session_state(SESSION) as state:
            state.add_event("bot_spoke", {"world_time": 31406})
            state.add_event("schedule", {"id": "dinner_cook", "note": "到点把晚饭做上"})
            state.add_event("move", {"to": "study"})
            state.add_event("mystery", {"foo": "bar"})

        prompt = await self.engine.preview_autonomous_prompt(SESSION)
        lines = prompt.splitlines()
        start = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("# 最近发生的事")
        )
        block: list[str] = []
        for line in lines[start + 1 :]:
            if line.startswith("#"):
                break
            block.append(line)
        body = "\n".join(block)
        self.assertIn("走到了", body)
        self.assertIn("把晚饭做上", body)
        self.assertNotIn("dinner_cook", body)
        self.assertNotIn("bot_spoke", body)
        self.assertNotIn("world_time", body)
        self.assertNotIn("mystery", body)
        self.assertNotIn("{", body)

    async def test_chat_note_is_kept_per_session_and_expires(self):
        """「刚才在聊什么」按会话存（私聊写的不该出现在群里），而且过期就不再带。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        async with self.engine.session_state(SESSION) as state:
            self.engine.note_chat_note(state, "私聊里在闹别扭", PRIVATE_SESSION)
            self.engine.note_chat_note(state, "群里在聊周末去哪", SESSION)

            self.assertEqual(
                self.engine.chat_note_for(state, SESSION), "群里在聊周末去哪"
            )
            self.assertEqual(
                self.engine.chat_note_for(state, PRIVATE_SESSION), "私聊里在闹别扭"
            )
            # 默认 30 分钟之后就不算"刚才"了
            state.chat_notes[SESSION]["at"] = self.engine._now() - 3600
            self.assertEqual(self.engine.chat_note_for(state, SESSION), "")

    async def test_the_topic_note_reaches_the_prompt_even_with_answered_lines(self):
        """「刚才在聊什么」以前只在没原文可列时才出现（等于白写）：现在有原文也给。"""

        await self.engine.handle_incoming(self.ctx(text="第一句：晚饭吃什么"))
        self.llm.replies = [
            '{"chat_note": "在聊晚饭吃什么",'
            ' "actions": [{"type": "say", "messages": ["吃鱼吧"]}]}'
        ]
        await self.engine.handle_reply(self.ctx(text="第二句：我想吃鱼"))
        await self.engine.mark_chat_replied_by_session(SESSION)

        state = await self.engine.load_state(SESSION, cold_start=False)
        prompt = self.prompts_prompt(state)
        self.assertIn("上一轮你们在聊：在聊晚饭吃什么", prompt)
        self.assertIn("你已经回过话", prompt)

    async def test_each_session_gets_its_own_chat_budget(self):
        """每个会话各算各的额度：私聊聊得多，也不会把群里刚说的话挤出去。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        now = self.engine._now()
        async with self.engine.session_state(SESSION) as state:
            # 群里 3 条（还没回过）
            for index in range(3):
                state.note_chat(
                    user_id="7",
                    name="小明",
                    text=f"群里第{index}句",
                    now=now - 900 + index,
                    keep=200,
                )
                state.recent_chat[-1]["origin"] = SESSION
            # 私聊 20 组一问一答，全都排在群里那几句之后
            for index in range(20):
                state.note_chat(
                    user_id="9",
                    name="主人",
                    text=f"私聊第{index}问",
                    now=now - 600 + index * 20,
                    keep=200,
                )
                state.recent_chat[-1]["origin"] = PRIVATE_SESSION
                state.note_chat(
                    user_id="",
                    name="我",
                    text=f"私聊第{index}答",
                    now=now - 600 + index * 20 + 10,
                    is_self=True,
                    keep=200,
                )
                state.recent_chat[-1]["origin"] = PRIVATE_SESSION

        async with self.engine.session_state(SESSION) as state:
            rows = self.engine.chat_window(state, SESSION)

        here = [item for item in rows if item.get("origin") == SESSION]
        elsewhere = [item for item in rows if item.get("origin") == PRIVATE_SESSION]
        self.assertEqual(len(here), 3, "群里那几条必须还在（以前会被私聊挤掉）")
        self.assertLessEqual(len(group_chat_items(elsewhere)), 12)
        self.assertGreaterEqual(len(elsewhere), 1)

    async def test_answered_chat_survives_the_time_window(self):
        """已经回过话的那批不受时间窗限制：聊过一个小时，她也接得上刚说过的话。"""

        self.set_context(chat_window_minutes=30)
        async with self.engine.session_state(SESSION) as state:
            state.note_chat(
                user_id="1",
                name="小明",
                text="一小时前说的这句",
                now=self.engine._now() - 3600,
                keep=50,
            )
            # 水位线推到现在：这条算"已经回过话"
            state.chat_replied_until = self.engine._now()
            state.chat_replied_seq = int(state.chat_seq)
            carried = self.engine.chat_window(state, SESSION)

        self.assertTrue(
            any("一小时前说的这句" in str(item.get("text")) for item in carried),
            "已经回过话的留档不该被时间窗裁掉",
        )
        prompt = await self.engine.preview_autonomous_prompt(SESSION)
        self.assertIn("一小时前说的这句", prompt)
        self.assertIn("你已经回过话", prompt)

    async def test_chat_dropped_by_the_cap_still_reaches_the_summary(self):
        """被上限顶掉、还没轮到压缩的那几条，下次压缩要一起并进摘要。"""

        self.set_context(
            chat_history_max=20,
            chat_overflow="compress",
            chat_compress_threshold=80,
            chat_keep_after_compress=10,
            summary_refresh_minutes=1,
        )
        for index in range(25):
            await self.engine.note_presence(
                self.ctx(text=f"消息{index}", user_id="1", user_name="小明")
            )
        state = await self.get_state()
        self.assertEqual(len(state.recent_chat), 20)
        self.assertEqual(len(state.chat_dropped), 5, "顶掉的几条要先攒着")

        self.llm.replies = ["本周大家在聊搬家，顺便问了她周末有没有空。"]
        await self.engine.tick()

        state = await self.get_state()
        self.assertEqual(
            self.engine.chat_summary_for(state, SESSION),
            "本周大家在聊搬家，顺便问了她周末有没有空。",
        )
        self.assertEqual(state.chat_dropped, [], "并进摘要之后就该清掉")
        sent = str(self.llm.calls[-1].get("prompt") or "")
        self.assertIn("消息0", sent, "顶掉的那几条要出现在压缩请求里")

    async def test_discard_mode_tells_the_user_chat_is_being_dropped(self):
        """配置成"直接丢弃"时，留档满了要在日志里说清怎么改成压缩（一小时一次）。"""

        self.set_context(chat_history_max=20, chat_overflow="discard")
        for index in range(25):
            await self.engine.note_presence(
                self.ctx(text=f"消息{index}", user_id="1", user_name="小明")
            )
        state = await self.get_state()
        self.assertEqual(state.chat_dropped, [], "直接丢弃模式不攒被顶掉的内容")

        outcome = TickOutcome(session_id=SESSION)
        async with self.engine.session_state(SESSION) as live:
            await self.engine._maybe_compress_chat(live, outcome)
            await self.engine._maybe_compress_chat(live, outcome)
        events = await self.db.call("query_events", session_id=SESSION, limit=50)
        warned = [
            item
            for item in events
            if "留档已满"
            in json.dumps(item.get("detail") or {}, ensure_ascii=False)
        ]
        self.assertEqual(len(warned), 1)

    def test_when_text_reads_relative_and_absolute_times(self):
        """日程的「什么时候」要能读懂「十分钟后」和「晚上八点」。"""

        from datetime import datetime

        base = datetime(2026, 9, 23, 9, 44)
        read = lambda text: self.engine._parse_when_text(text, now=base)  # noqa: E731

        soon = read("十分钟后提醒主人喝水")
        self.assertEqual(soon["time"], "09:54")
        self.assertTrue(soon["once"])
        self.assertEqual(soon["date"], "2026-09-23")

        later = read("两个小时后")
        self.assertEqual(later["time"], "11:44")

        clock = read("21:30 提醒我收衣服")
        self.assertEqual(clock["time"], "21:30")

        evening = read("晚上八点半提醒我吃药")
        self.assertEqual(evening["time"], "20:30")

        daily = read("每天早上七点提醒我吃药")
        self.assertEqual(daily["time"], "07:00")
        self.assertFalse(daily["once"], "写了「每天」就是循环日程")

        self.assertIsNone(read("随便排一条吧"))

    async def test_reminder_schedule_keeps_her_own_words(self):
        """「提醒类」日程直接照她说的存：不经过辅助模型，也不改写她的原话。"""

        self.llm.replies = [SAY_REPLY]
        action = PlannedAction(
            type="schedule_add",
            intent="十分钟后提醒主人喝水",
            params={"at": "10 分钟后", "say": ["该喝水啦 笨蛋"]},
        )
        definition = self.engine.world.action_map()["schedule_add"]
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._run_schedule_action(
                state, self.engine.node(state.node_id), outcome, definition, action
            )

        schedules = [
            item
            for item in self.engine.schedules.schedules
            if str(item.id).startswith("bot_")
        ]
        self.assertEqual(len(schedules), 1, [item.id for item in self.engine.schedules.schedules])
        created = schedules[0]
        self.assertEqual(created.note, "十分钟后提醒主人喝水")
        self.assertEqual([step.type for step in created.action_chain], ["say"])
        self.assertEqual(
            [message for message in created.action_chain[0].messages],
            ["该喝水啦 笨蛋"],
        )
        self.assertTrue(created.once)
        # 没走辅助模型那条路（它会重写她的原话）
        prompts = [str(item.get("prompt") or "") for item in self.llm.calls]
        self.assertFalse(
            any("她想加一条日程" in text for text in prompts), prompts
        )

    async def test_schedule_chain_cannot_contain_schedule_actions(self):
        """日程里再排日程只会到点报错，直接拒绝。"""

        ok, note = await self.engine.schedule_add(
            {
                "time": "07:00",
                "action_chain": [{"type": "schedule_add"}],
                "note": "测试",
            }
        )
        self.assertFalse(ok)
        self.assertIn("不能再排日程", note)

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
        self.assertTrue(self.engine.chat_summary_for(state, SESSION))

        result = await self.engine.clear_chat_context(SESSION)
        self.assertEqual(result["removed"], 2)
        self.assertTrue(result["had_summary"])
        state = await self.get_state()
        self.assertEqual(state.recent_chat, [])
        self.assertEqual(self.engine.chat_summary_for(state, SESSION), "")
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

    # ---------------- 工具用法：按顺序都调 / 依次尝试 / 智能选择 ----------------

    def bind_tool_action(
        self,
        tool_names: list[str],
        *,
        mode: str = "sequence",
        flow: str = "simple",
        action_id: str = "search_web",
        offset: int = 0,
    ) -> ActionDef:
        """给内置搜索动作换一组工具和用法（真实用户就是这么配的）。"""

        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == action_id:
                action["tool_names"] = list(tool_names)
                action["tool_name"] = tool_names[0] if tool_names else ""
                action["tool_fallbacks"] = []
                action["tool_mode"] = mode
                # 这里测的是「多个工具怎么用」，与检索流水线无关，走最朴素的调用形态
                action["tool_flow"] = flow
        self.store.save_world(raw)
        self.engine.reload_config()
        return self.engine.world.action_map()[action_id]

    async def test_fallback_mode_uses_the_next_tool_when_one_fails(self):
        """依次尝试：第一个工具坏了就用第二个，不再往下试。"""

        self.tools._tools = {"first_tool": "先试这个", "second_tool": "坏了再试这个"}
        self.tools.failures["first_tool"] = "工具「first_tool」没有可调用的 handler"
        self.tools.results = {"second_tool": "第二个工具的结果"}
        definition = self.bind_tool_action(["first_tool", "second_tool"], mode="fallback")
        await self.set_state(node_id="study")

        payload = {"type": "search_web", "intent": "查今天的新闻", "params": {}}
        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        self.assertEqual([name for name, _params in self.tools.calls], ["first_tool", "second_tool"])
        self.assertIn("第二个工具的结果", payload["tool_result"])
        self.assertTrue(payload["tool_ok"])

    # ---------------- 工具熔断 ----------------

    async def test_broken_tool_is_skipped_after_two_failures(self):
        """连续两次"工具坏了"就临时不用它；退避期过后自动放行试探。"""

        self.tools.failures["web_search"] = "工具「web_search」没有可调用的 handler"
        state = await self.set_state(node_id="study")
        for _ in range(2):
            call = await self.engine._call_tool("search_web", {"query": "x"}, state, tool_name="web_search")
            self.assertFalse(call.ok)
        # 第三次：解析阶段就跳过它了
        call = await self.engine._call_tool("search_web", {"query": "x"}, state, tool_name="web_search")
        self.assertFalse(call.ok)
        self.assertEqual(self.engine.resolve_tool("search_web", "study", "web_search"), "")
        breakers = self.engine.tool_breaker_state()
        self.assertEqual(len(breakers), 1)
        self.assertEqual(breakers[0]["tool"], "web_search")
        self.assertGreater(breakers[0]["seconds_left"], 0)

        # 成功一次就清零
        self.tools.failures.clear()
        self.engine.reset_tool_breakers("web_search")
        ok_call = await self.engine._call_tool("search_web", {"query": "x"}, state, tool_name="web_search")
        self.assertTrue(ok_call.ok)
        self.assertEqual(self.engine.tool_breaker_state(), [])

    async def test_argument_errors_do_not_break_the_tool(self):
        """参数写错是补参的问题，不该把工具拉黑。"""

        self.tools.failures["web_search"] = "missing 1 required positional argument: 'query'"
        state = await self.set_state(node_id="study")
        for _ in range(3):
            await self.engine._call_tool("search_web", {}, state, tool_name="web_search")
        self.assertEqual(self.engine.tool_breaker_state(), [])
        self.assertEqual(self.engine.resolve_tool("search_web", "study", "web_search"), "web_search")

    async def test_breaker_clears_when_config_changes(self):
        """配置一改就全清：最常见的"工具坏了"其实是刚装好、刚补了 key。"""

        self.tools.failures["web_search"] = "工具「web_search」没有可调用的 handler"
        state = await self.set_state(node_id="study")
        for _ in range(2):
            await self.engine._call_tool("search_web", {"query": "x"}, state, tool_name="web_search")
        self.assertTrue(self.engine.tool_breaker_state())

        self.engine.reload_config()
        self.assertEqual(self.engine.tool_breaker_state(), [])

    async def test_smart_mode_lets_the_helper_pick_one_tool(self):
        """智能选择：辅助模型按意图挑一个，挑工具和补参数一次完成。"""

        self.tools._tools = {"news_search": "搜新闻", "code_search": "搜代码"}
        self.tools.results = {"news_search": "今天的新闻", "code_search": "代码片段"}
        self.tools.schemas["news_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.schemas["code_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        definition = self.bind_tool_action(["news_search", "code_search"], mode="smart")
        await self.set_state(node_id="study")
        self.llm.replies = ['{"tool": "news_search", "params": {"query": "今天的新闻"}}']

        payload = {"type": "search_web", "intent": "查今天的新闻", "params": {}}
        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        # 只调了模型挑中的那个，而且参数也是它一起给出来的
        self.assertEqual([name for name, _params in self.tools.calls], ["news_search"])
        self.assertEqual(self.tools.calls[0][1]["query"], "今天的新闻")
        self.assertTrue(payload["tool_ok"])

    async def test_smart_mode_switches_tool_after_a_broken_one(self):
        """智能选择：挑中的工具坏了，就把它摘掉再问一次。"""

        self.tools._tools = {"broken_one": "坏的", "good_one": "好的"}
        self.tools.failures["broken_one"] = "工具「broken_one」没有可调用的 handler"
        self.tools.results = {"good_one": "好的结果"}
        for name in ("broken_one", "good_one"):
            self.tools.schemas[name] = {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            }
        definition = self.bind_tool_action(["broken_one", "good_one"], mode="smart")
        await self.set_state(node_id="study")
        self.llm.replies = [
            '{"tool": "broken_one", "params": {"query": "新闻"}}',
            '{"tool": "good_one", "params": {"query": "新闻"}}',
        ]

        payload = {"type": "search_web", "intent": "查今天的新闻", "params": {}}
        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        self.assertEqual(
            [name for name, _params in self.tools.calls], ["broken_one", "good_one"]
        )
        self.assertIn("好的结果", payload["tool_result"])
        # 第二次问的时候，坏工具已经从候选里摘掉了
        self.assertNotIn("broken_one", self.llm.calls[-1]["prompt"])

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
                action["tool_flow"] = "simple"
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

    # ---------------- 联网检索：多查询 / 读正文 / 补查 ----------------

    def bind_search_flow(
        self,
        search_tools: list[str],
        *,
        readers: list[str] | None = None,
        depth: str = "standard",
        reads: int = 2,
        rounds: int = 1,
        max_queries: int = 3,
    ) -> ActionDef:
        """把内置搜索动作配成检索流水线形态。"""

        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "search_web":
                action["tool_names"] = list(search_tools)
                action["tool_name"] = search_tools[0] if search_tools else ""
                action["tool_flow"] = "search"
                action["reader_tool_names"] = list(readers or [])
                action["search_depth"] = depth
                action["search_max_reads"] = reads
                action["search_rounds"] = rounds
                action["search_max_queries"] = max_queries
        self.store.save_world(raw)
        self.engine.reload_config()
        return self.engine.world.action_map()["search_web"]

    def use_search_tool(self, name: str = "news_search", result: str = "") -> None:
        self.tools._tools = {name: "搜新闻"}
        self.tools.schemas[name] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.results = {name: result or "今日热点\nhttps://news.example/a 第一条"}

    async def test_search_flow_runs_every_query_and_keeps_sources(self):
        """她写了几条 queries 就查几次，来源进日志。"""

        self.use_search_tool(
            result=(
                "今日热点\nhttps://news.example/a 第一条\n"
                "行业动态\nhttps://news.example/b 第二条"
            )
        )
        definition = self.bind_search_flow(["news_search"], depth="quick")
        state = await self.set_state(node_id="study")
        payload = {
            "type": "search_web",
            "intent": "查今天的新闻",
            "queries": ["今日科技新闻", "AI 行业 最新进展"],
        }

        await self.engine._run_tool_calls(state, definition, payload)

        self.assertEqual(
            [params.get("query") for _name, params in self.tools.calls],
            ["今日科技新闻", "AI 行业 最新进展"],
        )
        self.assertIn("1. ", payload["tool_evidence_text"])
        events = await self.db.call("query_events", session_id=SESSION, limit=30)
        sources = [item for item in events if item["event_type"] == "search_sources"]
        self.assertTrue(sources)
        self.assertEqual(
            sources[0]["detail"]["queries"], ["今日科技新闻", "AI 行业 最新进展"]
        )
        self.assertEqual(len(sources[0]["detail"]["sources"]), 2)

    async def test_search_flow_asks_the_helper_when_no_queries_given(self):
        """她只写了意图：让辅助模型翻成一条查询词，而不是把意图原样丢给搜索。"""

        self.use_search_tool()
        definition = self.bind_search_flow(["news_search"], depth="quick")
        await self.set_state(node_id="study")
        self.llm.replies = ['{"query": "今天的新闻"}']
        payload = {"type": "search_web", "intent": "查一下今天的新闻"}

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        self.assertEqual(self.tools.calls[0][1]["query"], "今天的新闻")

    async def test_rule_triggered_search_gets_a_real_topic(self):
        """规则触发的检索（没有大模型给的意图）：让她自己说想查什么，别再拿动作说明顶。"""

        self.tools._tools = {"web_search": "搜索网页"}
        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.results = {"web_search": "今天有三条科技新闻。"}
        definition = self.bind_search_flow(["web_search"], depth="quick")
        await self.set_state(node_id="study")
        self.llm.replies = ["今天有什么有意思的科技新闻", '{"query": "今日科技新闻"}']
        # 好奇心规则就是这么建动作的：只有动作名，没有意图
        payload = {"type": "search_web", "intent": "", "params": {}}

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        # 第一次调用是"你自己想查什么"，第二次才是翻成查询词
        self.assertIn("想上网查什么", self.llm.calls[0]["system_prompt"])
        self.assertIn("只输出这句话", self.llm.calls[0]["prompt"])
        asked = self.llm.calls[1]["prompt"]
        self.assertNotIn("书房用电脑", asked)
        self.assertIn("今天有什么有意思的科技新闻", asked)
        self.assertEqual(self.tools.calls[0][1]["query"], "今日科技新闻")

    async def test_rule_triggered_search_uses_the_configured_topic_first(self):
        """动作里配过「搜索主题」：直接用，不再多问一次大模型。"""

        self.tools._tools = {"web_search": "搜索网页"}
        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.results = {"web_search": "今天有三条科技新闻。"}
        self.bind_search_flow(["web_search"], depth="quick")
        self.set_search_topic("今日新闻热点")
        definition = self.engine.world.action_map()["search_web"]
        await self.set_state(node_id="study")
        self.llm.replies = ['{"query": "今日热点新闻"}']
        payload = {"type": "search_web", "intent": "", "params": {}}

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        self.assertEqual(len(self.llm.calls), 1)  # 只调了补参那一次
        self.assertIn("今日新闻热点", self.llm.calls[0]["prompt"])
        self.assertEqual(self.tools.calls[0][1]["query"], "今日热点新闻")

    async def test_rule_triggered_search_falls_back_without_a_model(self):
        """没有可用的主模型时退回中性主题，而不是动作说明。"""

        self.tools._tools = {"web_search": "搜索网页"}
        self.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.results = {"web_search": "今天有三条科技新闻。"}
        definition = self.bind_search_flow(["web_search"], depth="quick")
        await self.set_state(node_id="study")
        self.engine.llm = None  # 主模型不可用；补参还有 helper
        self.llm.replies = ['{"query": "今日热点新闻"}']
        payload = {"type": "search_web", "intent": "", "params": {}}

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        asked = self.llm.calls[0]["prompt"]
        self.assertNotIn("书房用电脑", asked)
        self.assertIn("今天有什么新鲜事", asked)

    async def test_search_flow_reads_the_top_pages(self):
        """配了阅读工具就去读正文，正文进证据块。"""

        self.use_search_tool(result="标题\nhttps://news.example/a 摘要")
        self.tools._tools["page_reader"] = "读网页"
        self.tools.schemas["page_reader"] = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        self.tools.results["page_reader"] = "正文片段" * 60
        definition = self.bind_search_flow(["news_search"], readers=["page_reader"])
        await self.set_state(node_id="study")
        payload = {"type": "search_web", "intent": "查新闻", "queries": ["今天的新闻"]}

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        self.assertEqual(
            [name for name, _params in self.tools.calls], ["news_search", "page_reader"]
        )
        self.assertEqual(self.tools.calls[1][1]["url"], "https://news.example/a")
        self.assertIn("正文片段", payload["tool_evidence"][0]["passage"])
        self.assertIn("正文片段", payload["tool_evidence_text"])

    async def test_search_flow_asks_for_more_angles_when_evidence_is_thin(self):
        """只搜到一条时，让辅助模型补一个角度再查一次。"""

        self.use_search_tool(result="只有一条\nhttps://news.example/only 很短")
        definition = self.bind_search_flow(["news_search"])
        await self.set_state(node_id="study")
        self.llm.replies = ['{"queries": ["换个角度"]}']
        payload = {"type": "search_web", "intent": "查新闻", "queries": ["今天的新闻"]}

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        self.assertEqual(
            [params.get("query") for _name, params in self.tools.calls],
            ["今天的新闻", "换个角度"],
        )

    async def test_quick_depth_stops_after_one_search(self):
        """quick 档位：只查一轮，不读正文也不补查。"""

        self.use_search_tool(result="只有一条\nhttps://news.example/only 很短")
        self.tools._tools["page_reader"] = "读网页"
        self.tools.schemas["page_reader"] = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
        }
        definition = self.bind_search_flow(
            ["news_search"], readers=["page_reader"], depth="quick"
        )
        await self.set_state(node_id="study")
        self.llm.replies = ['{"queries": ["不该被问到"]}']
        payload = {"type": "search_web", "intent": "查新闻", "queries": ["今天的新闻"]}

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        self.assertEqual([name for name, _params in self.tools.calls], ["news_search"])
        self.assertEqual(self.llm.calls, [])

    async def test_search_flow_reports_when_nothing_is_usable(self):
        """什么都没搜到：结果为空，不写证据块，也不编。"""

        self.use_search_tool()
        self.tools.failures["news_search"] = "工具「news_search」没有可调用的 handler"
        definition = self.bind_search_flow(["news_search"], depth="quick")
        await self.set_state(node_id="study")
        payload = {"type": "search_web", "intent": "查新闻", "queries": ["今天的新闻"]}

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        self.assertFalse(payload["tool_ok"])
        self.assertNotIn("tool_evidence_text", payload)
        self.assertTrue(payload["tool_error"])

    async def test_search_hides_tool_calls_and_shows_one_line(self):
        """检索期间的工具调用不进群（只在日志里），群里只出现一条「联网搜索」。"""

        raw = self.store.raw_world()
        raw["echo_types"] = ["tool_call", "tool_result", "search"]
        self.store.save_world(raw)
        self.engine.reload_config()
        self.use_search_tool()
        definition = self.bind_search_flow(["news_search"], depth="quick")
        await self.set_state(node_id="study")
        payload = {
            "type": "search_web",
            "intent": "看看今天的科技新闻",
            "queries": ["今日科技新闻", "AI 行业 最新进展", "芯片 动态"],
        }
        outcome = TickOutcome(session_id=SESSION)

        await self.engine._run_tool_calls(
            await self.get_state(), definition, payload, outcome=outcome
        )

        lines = list(outcome.debug_messages)
        self.assertFalse([line for line in lines if line.startswith("🔧")], lines)
        self.assertFalse([line for line in lines if line.startswith("📥")], lines)
        search_lines = [line for line in lines if line.startswith("🌐")]
        self.assertEqual(len(search_lines), 1, lines)
        self.assertIn("正在联网搜索", search_lines[0])
        # 查询词是她正在查什么，精简模式下也不能省
        self.assertIn("今日科技新闻", search_lines[0])
        # 逐条的工具调用仍然完整写进日志页
        events = await self.db.call("query_events", session_id=SESSION, limit=40)
        kinds = [item["event_type"] for item in events]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        self.assertIn("search", kinds)

    async def test_search_line_keeps_queries_in_compact_mode(self):
        """精简模式省的是参数和结果，不是「正在查什么」。"""

        raw = self.store.raw_world()
        raw["echo_types"] = ["search"]
        raw["echo_compact"] = True
        self.store.save_world(raw)
        self.engine.reload_config()
        self.use_search_tool()
        definition = self.bind_search_flow(["news_search"], depth="quick")
        await self.set_state(node_id="study")
        payload = {
            "type": "search_web",
            "intent": "看看今天的科技新闻",
            "queries": ["今日科技新闻", "芯片 动态"],
        }
        outcome = TickOutcome(session_id=SESSION)

        await self.engine._run_tool_calls(
            await self.get_state(), definition, payload, outcome=outcome
        )

        search_lines = [line for line in outcome.debug_messages if line.startswith("🌐")]
        self.assertEqual(len(search_lines), 1, outcome.debug_messages)
        self.assertIn("今日科技新闻", search_lines[0])
        self.assertIn("芯片 动态", search_lines[0])

    async def test_search_line_is_sent_live_and_not_duplicated(self):
        """有实时通道时：「正在联网搜索」在检索开始时立刻发出，收尾不再重复发。"""

        raw = self.store.raw_world()
        raw["echo_types"] = ["tool_call", "tool_result", "search"]
        self.store.save_world(raw)
        self.engine.reload_config()
        self.use_search_tool()
        definition = self.bind_search_flow(["news_search"], depth="quick")
        await self.set_state(node_id="study")

        async def sink(session_id, message):
            return await self.messenger.send_text(session_id, [message])

        self.engine.debug_sink = sink
        payload = {
            "type": "search_web",
            "intent": "看看今天的科技新闻",
            "queries": ["今日科技新闻", "AI 行业 最新进展", "芯片 动态"],
        }
        outcome = TickOutcome(session_id=SESSION)

        await self.engine._run_tool_calls(
            await self.get_state(), definition, payload, outcome=outcome
        )

        live = [text for text in self.messenger.flat_messages if text.startswith("🌐")]
        # 一次检索只发一条；工具调用一条都不进群
        self.assertEqual(len(live), 1, self.messenger.flat_messages)
        self.assertIn("正在联网搜索", live[0])
        self.assertFalse(
            [text for text in self.messenger.flat_messages if text.startswith("🔧")]
        )
        # 已经实时发过的，不再挂进回合末的批量队列
        self.assertFalse(
            [line for line in outcome.debug_messages if line.startswith("🌐")]
        )
        # 收尾那次批量回显也不能再补一遍
        before = len(self.messenger.flat_messages)
        state = await self.get_state()
        await self.engine._echo_events_since(state, outcome, 0)
        self.assertEqual(len(self.messenger.flat_messages), before)

    async def test_search_loop_lets_the_main_model_decide(self):
        """够不够、要不要再来一轮：由主模型判断（它说够了就停，并给下一轮的查询词）。"""

        await self.set_state(node_id="study")
        state = await self.get_state()

        self.llm.replies = ['{"done": false, "queries": ["换个更具体的角度"]}']
        done, queries = await self.engine._search_continue(
            state, None, topic="查新闻", asked=["今日新闻"], items=[]
        )
        self.assertFalse(done)
        self.assertEqual(queries, ["换个更具体的角度"])
        asked_prompt = self.llm.calls[-1]["prompt"]
        self.assertIn("查新闻", asked_prompt)
        self.assertIn("今日新闻", asked_prompt)

        # 说够了
        self.llm.replies = ['{"done": true}']
        done, queries = await self.engine._search_continue(
            state, None, topic="查新闻", asked=["今日新闻"], items=[]
        )
        self.assertTrue(done)
        self.assertEqual(queries, [])

        # 返回不可解析 / 给不出新词：都当"够了"，不要无限查
        self.llm.replies = ["我觉得差不多了"]
        done, queries = await self.engine._search_continue(
            state, None, topic="查新闻", asked=["今日新闻"], items=[]
        )
        self.assertTrue(done)
        self.llm.replies = ['{"done": false, "queries": ["今日新闻"]}']
        done, queries = await self.engine._search_continue(
            state, None, topic="查新闻", asked=["今日新闻"], items=[]
        )
        self.assertTrue(done)  # 只给已经查过的词 = 没有新东西

    async def test_reader_json_payload_becomes_plain_passage(self):
        """阅读工具返回 JSON 外壳：材料里要放正文，不是 {"url":…} 这种字段。"""

        self.use_search_tool(result="标题\nhttps://news.example/a 摘要")
        self.tools._tools["page_reader"] = "读网页"
        self.tools.schemas["page_reader"] = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        self.tools.results["page_reader"] = json.dumps(
            {
                "url": "https://news.example/a",
                "title": "一条新闻",
                "content": (
                    "[](https://news.example/nav)\n\n# 一条新闻\n\n"
                    "正文第一句，把这件事的来龙去脉讲清楚了。" * 6
                ),
            },
            ensure_ascii=False,
        )
        definition = self.bind_search_flow(["news_search"], readers=["page_reader"])
        await self.set_state(node_id="study")
        payload = {"type": "search_web", "intent": "查新闻", "queries": ["今天的新闻"]}

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        passage = payload["tool_evidence"][0]["passage"]
        self.assertNotIn('"url"', passage)
        self.assertNotIn("news.example/nav", passage)
        self.assertIn("正文第一句", passage)
        self.assertIn("正文第一句", payload["tool_evidence_text"])

    async def test_repeated_phrasings_of_the_same_query_are_dropped(self):
        """补查给的"同一个意思换个说法"不再查第二遍，剩下的收工。"""

        await self.set_state(node_id="study")
        state = await self.get_state()
        self.llm.replies = [
            '{"done": false, "queries": ['
            '"异环 1.4版本 祷歌为谁而诵 更新内容",'
            '"异环 1.4版本 祷歌为谁而诵 前瞻特别节目回顾"]}'
        ]

        done, queries = await self.engine._search_continue(
            state,
            None,
            topic="异环新版本内容",
            asked=["异环 1.4版本 祷歌为谁而诵 更新公告"],
            items=[],
        )

        self.assertTrue(done)
        self.assertEqual(queries, [])

    async def test_a_genuinely_new_angle_is_still_asked(self):
        """换了角度的问题照查：只掐掉同义的重复，不误伤。"""

        await self.set_state(node_id="study")
        state = await self.get_state()
        self.llm.replies = ['{"done": false, "queries": ["异环 1.4 新角色 技能"]}']

        done, queries = await self.engine._search_continue(
            state,
            None,
            topic="异环新版本内容",
            asked=["异环 1.4版本 祷歌为谁而诵 更新公告"],
            items=[],
        )

        self.assertFalse(done)
        self.assertEqual(queries, ["异环 1.4 新角色 技能"])

    # ---------------- 即时发言：慢动作之前先说的话 ----------------

    async def test_say_before_a_slow_action_goes_out_immediately(self):
        """「坐好等我两分钟～」当场发出去，不等检索跑完再和结果一起冒出来。"""

        self.use_search_tool(result="标题\nhttps://news.example/a 摘要")
        self.bind_search_flow(["news_search"], depth="quick")
        self.llm.replies = [
            '{"actions":['
            '{"type":"say","messages":["坐好等我两分钟～"]},'
            '{"type":"search_web","intent":"查今天的新闻"}]}',
            '{"query": "今天的新闻"}',
            '{"actions":[{"type":"say","messages":["查到啦"]}]}',
        ]
        sent: list[tuple[str, str]] = []

        async def sink(session_id, message):
            sent.append((session_id, str(message)))
            return await self.messenger.send_text(session_id, [message])

        self.engine.say_sink = sink
        ctx = self.ctx(text="帮我查一下今天有什么新闻")
        await self.engine.handle_incoming(ctx)
        outcome = await self.engine.handle_reply(ctx)

        # 她先说的那句走的是实时通道；收尾只剩"检索之后要说的话"
        self.assertEqual(sent, [(SESSION, "坐好等我两分钟～")])
        self.assertEqual(outcome.live_messages, ["坐好等我两分钟～"])
        self.assertTrue(outcome.live_sent)
        self.assertNotIn("坐好等我两分钟～", outcome.messages)
        self.assertIn("查到啦", outcome.messages)
        self.assertTrue(outcome.ok)

    async def test_live_say_is_recorded_as_her_words(self):
        """即时发出去的那句也算她说过：聊天上下文里有它，下一轮提示词才看得到。"""

        self.use_search_tool(result="标题\nhttps://news.example/a 摘要")
        self.bind_search_flow(["news_search"], depth="quick")
        self.llm.replies = [
            '{"actions":['
            '{"type":"say","messages":["坐好等我两分钟～"]},'
            '{"type":"search_web","intent":"查今天的新闻"}]}',
            '{"query": "今天的新闻"}',
            '{"actions":[{"type":"say","messages":["查到啦"]}]}',
        ]

        async def sink(session_id, message):
            return await self.messenger.send_text(session_id, [message])

        self.engine.say_sink = sink
        ctx = self.ctx(text="帮我查一下今天有什么新闻")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)

        state = await self.get_state()
        mine = [item["text"] for item in state.recent_chat if item.get("is_self")]
        self.assertIn("坐好等我两分钟～", mine)
        self.assertIn("查到啦", mine)

    async def test_flush_say_without_a_channel_keeps_everything_pending(self):
        """没接实时通道（测试 / 老路径）：句子原样留着，收尾一起发，不丢话。"""

        outcome = TickOutcome(session_id=SESSION)
        outcome.messages = ["等我两分钟～"]
        await self.engine._flush_say(outcome)
        self.assertEqual(outcome.messages, ["等我两分钟～"])
        self.assertEqual(outcome.live_messages, [])

    async def test_flush_say_stops_at_the_first_failure(self):
        """发不出去就从那一句起全部留到收尾：顺序不乱，也不丢内容。"""

        outcome = TickOutcome(session_id=SESSION)
        outcome.messages = ["第一句", "第二句"]
        outcome.debug_positions = [2]
        seen: list[str] = []

        async def sink(session_id, message):
            seen.append(str(message))
            return len(seen) == 1  # 第一句发出去，第二句失败

        self.engine.say_sink = sink
        await self.engine._flush_say(outcome)

        self.assertEqual(seen, ["第一句", "第二句"])
        self.assertEqual(outcome.live_messages, ["第一句"])
        self.assertEqual(outcome.messages, ["第二句"])
        self.assertEqual(outcome.debug_positions, [1])

    async def test_deliver_does_not_repeat_the_lines_already_sent(self):
        """已经即时发过的那几句：日志照记，但不再发第二遍。"""

        await self.set_state(node_id="study")
        outcome = TickOutcome(session_id=SESSION)
        outcome.live_messages = ["坐好等我两分钟～"]
        outcome.messages = ["查到了，今天还挺热闹"]

        await self.engine._deliver(outcome)

        self.assertEqual(
            self.messenger.flat_messages, ["查到了，今天还挺热闹"]
        )

    async def test_a_planned_say_before_a_slow_step_also_goes_out_first(self):
        """自主计划里排在慢动作前的那句同样当场发出去（不是等整轮做完）。"""

        self.use_search_tool(result="标题\nhttps://news.example/a 摘要")
        self.bind_search_flow(["news_search"], depth="quick")
        state = await self.set_state(node_id="study")
        self.llm.replies = ["", ""]
        caught: list[str] = []

        async def sink(session_id, message):
            caught.append(str(message))
            return True

        self.engine.say_sink = sink
        outcome = TickOutcome(session_id=SESSION)
        await self.engine._execute_actions(
            state,
            self.engine.node(state.node_id),
            outcome,
            [
                PlannedAction(type="say", messages=["本小姐去查查今天的新闻"]),
                PlannedAction(
                    type="search_web",
                    intent="查今天的新闻",
                    params={"query": "今天的新闻"},
                ),
            ],
            depth=0,
            autonomous=True,
        )

        self.assertEqual(caught, ["本小姐去查查今天的新闻"])
        self.assertEqual(outcome.live_messages, ["本小姐去查查今天的新闻"])
        self.assertEqual(self.tools.calls[0][1]["query"], "今天的新闻")

    async def test_busy_session_is_detected_without_waiting(self):
        """她正忙时要能被"不排队"地发现：编辑器那次推进直接跳过，而不是等它做完。"""

        import asyncio as _asyncio

        self.assertFalse(self.engine.is_busy(SESSION))

        async def hold():
            async with self.engine.session_state(SESSION):
                await _asyncio.sleep(0.3)

        task = _asyncio.ensure_future(hold())
        await _asyncio.sleep(0.05)
        self.assertTrue(self.engine.is_busy(SESSION))  # 忙：这次的 tick 会被跳过
        await task
        self.assertFalse(self.engine.is_busy(SESSION))

    async def test_multiple_queries_are_issued_in_parallel(self):
        """一次检索的多条查询要并发发出去：串行时每条都要等上一条搜完（又慢、回显还散）。"""

        import asyncio

        self.tools._tools = {"news_search": "搜新闻"}
        self.tools.schemas["news_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.tools.results = {"news_search": "标题\nhttps://news.example/a 内容"}
        inflight = {"now": 0, "peak": 0}
        original = self.tools.call_tool

        async def slow_call(name, params, session_id=""):
            inflight["now"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["now"])
            try:
                await asyncio.sleep(0.15)
                return await original(name, params, session_id)
            finally:
                inflight["now"] -= 1

        self.tools.call_tool = slow_call
        definition = self.bind_search_flow(["news_search"], depth="quick")
        await self.set_state(node_id="study")
        payload = {
            "type": "search_web",
            "intent": "查新闻",
            "queries": ["今日新闻", "国际要闻", "科技动态"],
        }

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        self.assertEqual(inflight["peak"], 3, (self.tools.calls, payload))
        self.assertEqual(len(self.tools.calls), 3)

    async def test_followup_round_cannot_search_again(self):
        """刚查完那一轮：她想再写一次检索会被拦下，只让她说话。"""

        self.use_search_tool()
        self.bind_search_flow(["news_search"], depth="quick")
        await self.set_state(node_id="study")
        self.tools.results["news_search"] = (
            "新闻一\nhttps://news.example/a " + "有一条芯片行业的消息。" * 8 + "\n"
            "新闻二\nhttps://news.example/b " + "另外还有一条 AI 模型的进展。" * 8
        )
        self.llm.replies = [
            '{"actions":[{"type":"search_web","intent":"查新闻",'
            '"queries":["今天的新闻"]}]}',
            "结论：查到了三条\n1. 有一条值得讲（1）",
            '{"actions":[{"type":"search_web","intent":"再查一次",'
            '"queries":["别的话题"]},{"type":"say","messages":["查到了三条"]}]}',
        ]

        outcome = await self.engine.handle_reply(self.ctx())

        # 只搜了一次（第二轮那次被拦）
        self.assertEqual(len(self.tools.calls), 1)
        self.assertIn("查到了三条", " ".join(outcome.messages))
        # 拦下来这件事要能在日志里看到，提示词里也明确不让她再查
        events = await self.db.call("query_events", session_id=SESSION, limit=40)
        notes = [item["detail"].get("note", "") for item in events if item["event_type"] == "skip"]
        self.assertTrue(any("续说这一轮不再接" in note for note in notes), notes)
        self.assertIn("不要再调检索", self.llm.calls[-1]["prompt"])

    async def test_she_can_ask_for_a_shallower_search(self):
        """她自己写 search_depth=quick：就不读正文了（配置是上限）。"""

        self.use_search_tool(result="标题\nhttps://news.example/a 摘要内容够长" * 3)
        self.tools._tools["page_reader"] = "读网页"
        self.tools.schemas["page_reader"] = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        definition = self.bind_search_flow(["news_search"], readers=["page_reader"])
        await self.set_state(node_id="study")
        payload = {
            "type": "search_web",
            "intent": "查新闻",
            "queries": ["今天的新闻"],
            "search_depth": "quick",
        }

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        # 快查档：读了 0 篇
        self.assertEqual([name for name, _params in self.tools.calls], ["news_search"])

    async def test_second_search_tool_takes_over_when_the_first_is_broken(self):
        """动作上挂了两个搜索工具：第一个用不了就换第二个（不是白挂）。"""

        self.tools._tools = {"first_search": "第一个", "second_search": "第二个"}
        for name in ("first_search", "second_search"):
            self.tools.schemas[name] = {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }
        self.tools.results = {"second_search": "第二条搜索返回的内容"}
        definition = self.bind_search_flow(["first_search", "second_search"], depth="quick")
        await self.set_state(node_id="study")
        self.tools.failures["first_search"] = "工具「first_search」没有可调用的 handler"
        payload = {"type": "search_web", "intent": "查新闻", "queries": ["今天的新闻"]}

        await self.engine._run_tool_calls(await self.get_state(), definition, payload)

        self.assertIn(
            ("second_search", {"query": "今天的新闻"}),
            self.tools.calls,
        )

    async def test_recent_search_is_written_into_the_prompt(self):
        """上一轮查过什么要带进下一轮提示词，免得她拿同样的词再查一遍。"""

        await self.engine._remember_search(SESSION, ["今日科技新闻"], 3)
        self.llm.replies = [SAY_REPLY]

        await self.engine.handle_reply(self.ctx())

        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("# 最近查过", prompt)
        self.assertIn("今日科技新闻", prompt)

    async def test_waking_from_the_editor_drops_the_sleep_plan(self):
        """网页上叫醒之后，计划里那一步「睡觉」不能再把她放倒。"""

        from core import planner as planner_module

        await self.set_state(node_id="bedroom")
        async with self.engine.session_state(SESSION) as state:
            state.state = "sleeping"
            state.current_action = {
                "type": "sleep",
                "elapsed_ticks": 1,
                "duration_ticks": 480,
            }
            state.current_plan = planner_module.create_plan(
                steps=[{"action": "sleep", "duration": 480}],
                world_time=state.world_time,
            )

        woken = await self.engine.wake_up(SESSION)

        self.assertTrue(woken)
        state = await self.get_state()
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)
        self.assertFalse(state.is_sleeping)
        # 保护期：刚被叫醒这段时间里规则不再安排她回去睡
        self.assertGreater(int(state.no_sleep_until), int(state.world_time))

    async def test_weather_refresh_explains_why_it_did_nothing(self):
        """点「刷新」没反应时，要能告诉她为什么（这里没给动作配天气工具）。"""

        note = await self.engine.maybe_refresh_weather(force=True)

        self.assertTrue(note)
        self.assertIn("天气工具", note)

    async def test_interrupting_her_sleep_counts_as_waking_up(self):
        """网页上「打断」睡觉 = 叫醒：计划要一起清掉，不然下一个 tick 又睡回去。"""

        from core import planner as planner_module

        await self.set_state(node_id="bedroom")
        async with self.engine.session_state(SESSION) as state:
            state.state = "sleeping"
            state.current_action = {
                "type": "sleep",
                "elapsed_ticks": 1,
                "duration_ticks": 480,
            }
            state.current_plan = planner_module.create_plan(
                steps=[{"action": "sleep", "duration": 480}],
                world_time=state.world_time,
            )

        done = await self.engine.interrupt(SESSION, force=True)

        self.assertTrue(done)
        state = await self.get_state()
        self.assertFalse(state.is_sleeping)
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)
        self.assertGreater(int(state.no_sleep_until), int(state.world_time))

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

    async def test_queued_step_keeps_queries(self):
        """排队时也要留住她自己写的查询词，否则到点会退化成按主题兜一条。"""

        await self.set_state(node_id="bedroom")
        actions = [
            PlannedAction(type="sleep", duration=600),
            PlannedAction(
                type="search_web",
                intent="查今天的新闻",
                queries=["今日热点", "行业动态"],
            ),
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
        self.assertEqual(queued[0]["queries"], ["今日热点", "行业动态"])

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

    async def test_command_images_are_posted_to_the_group(self):
        """指令型动作生出来的图（可能多张）要贴到群里，不是只给模型看一眼。"""

        from core.ports import ToolCallResult

        self.stub_commands(
            ToolCallResult(
                ok=True,
                text="画好了",
                tool="画图",
                attachments=["https://img/a.png", "https://img/b.png"],
            )
        )
        self.add_command_action()
        definition = self.engine.world.action_map()["ask_weather"]
        self.llm.replies = ["/天气 北京", SAY_REPLY]
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._run_command_action(
                state,
                self.engine.node("study"),
                outcome,
                definition,
                PlannedAction(type="ask_weather", intent="给我画张图"),
            )

        self.assertEqual(outcome.images, ["https://img/a.png", "https://img/b.png"])
        await self.engine._deliver(outcome)
        self.assertEqual(
            self.messenger.flat_images, ["https://img/a.png", "https://img/b.png"]
        )

    async def test_tool_images_are_posted_to_the_group(self):
        """工具型动作：生图工具把图交回来，这一轮就要贴到群里。"""

        self.tools._tools = {"text2img": "画图"}
        self.tools.results = {"text2img": "画好了"}
        self.tools.images = {"text2img": ["https://img/1.png", "https://img/2.png"]}
        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "draw_pic",
                "name": "画图",
                "category": "instant",
                "llm_level": "tool",
                "tool_names": ["text2img"],
                "scope": "global",
                "visible": False,
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        definition = self.engine.world.action_map()["draw_pic"]
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._run_tool_calls(
                state,
                definition,
                {"type": "draw_pic", "intent": "画只猫"},
                outcome=outcome,
            )

        self.assertEqual(outcome.images, ["https://img/1.png", "https://img/2.png"])
        await self.engine._deliver(outcome)
        self.assertEqual(
            self.messenger.flat_images, ["https://img/1.png", "https://img/2.png"]
        )

    async def test_continuous_image_action_posts_picture_before_it_finishes(self):
        """持续型生图动作：指令一开跑就把图发出去，剩下的时间只是占位。"""

        from core.ports import ToolCallResult

        self.stub_commands(
            ToolCallResult(
                ok=True,
                text="拍好了",
                tool="拍照",
                attachments=["https://img/leg.png"],
            )
        )
        self.add_command_action(category="continuous", duration=60)
        definition = self.engine.world.action_map()["ask_weather"]
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._start_action(
                state,
                self.engine.node("study"),
                outcome,
                definition,
                PlannedAction(type="ask_weather", intent="拍张照片"),
                0,
                False,
            )
            live = dict(state.current_action or {})

        # 动作还在跑，图已经在这一轮发出去了
        self.assertEqual(outcome.images, ["https://img/leg.png"])
        self.assertTrue(live.get("tool_live"))
        self.assertEqual(live.get("duration_ticks"), 1)  # 60 秒 = 1 tick
        self.assertIn("cmd_cache", live)

    async def test_continuous_image_action_speaks_once_at_the_end(self):
        """收尾时用开始那次拿到的结果补一句，不会再执行一遍指令。"""

        from core.ports import ToolCallResult

        calls: list[str] = []
        self.engine.commands = _RecordingCommands(calls, ToolCallResult(ok=True, text="拍好了"))
        self.add_command_action(category="continuous", duration=60)
        definition = self.engine.world.action_map()["ask_weather"]
        # 第一条给补参模型（拼指令），第二条才是她收尾时说的那句
        self.llm.replies = ["/天气 拍照", SAY_REPLY]
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._start_action(
                state,
                self.engine.node("study"),
                outcome,
                definition,
                PlannedAction(type="ask_weather", intent="拍张照片"),
                0,
                False,
            )
            await self.engine._finish_action(
                state, self.engine.node("study"), outcome, dict(state.current_action)
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(outcome.messages, ["有人在吗？"])
        self.assertIsNone(state.current_action)

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

    async def test_long_command_result_is_kept_long_in_the_log(self):
        """指令返回一长段时，提示词可以压，但日志里要留全一点。"""

        from core.ports import ToolCallResult

        body = "".join(f"第{index:03d}行。" for index in range(120))
        self.stub_commands(ToolCallResult(ok=True, text=body, tool="天气"))
        self.add_command_action()
        await self.run_command_action()
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        results = [item for item in events if item["event_type"] == "command_result"]
        self.assertTrue(results, events)
        self.assertEqual(results[0]["detail"]["result"], body)

    async def test_preset_round_trip_and_state_clear(self):
        """预设：存下来、改坏再应用能复原；切预设只清状态，不动记忆与日志。"""

        await self.set_state(node_id="kitchen", energy=0.2)
        path, warnings = self.store.save_preset("p1", name="测试预设")
        self.assertEqual(warnings, [])
        self.assertTrue(path.is_file())
        self.assertEqual(self.store.active_preset(), "p1")

        raw = self.store.raw_world()
        raw["nodes"][0]["name"] = "改过的名字"
        self.store.save_world(raw)
        self.store.apply_preset("p1", blocks=["map"])
        self.assertNotEqual(self.store.raw_world()["nodes"][0]["name"], "改过的名字")

        cleared = await self.engine.clear_all_states()
        self.assertGreaterEqual(cleared, 1)
        self.assertIsNone(await self.db.call("get_state", SESSION))
        self.assertTrue(self.store.list_presets())

    async def test_preset_blocks_leave_everything_else_alone(self):
        """分块应用：只勾地图时，动作、人设、日程、会话白名单都不动。"""

        raw_sessions = self.store.raw_sessions()
        raw_sessions.setdefault("sessions", []).append(
            {
                "session_id": OTHER_SESSION,
                "type": "group",
                "platform": "aiocqhttp",
                "enabled": True,
            }
        )
        self.store.save_sessions(raw_sessions)
        raw_world = self.store.raw_world()
        raw_world["persona"] = {"mode": "plugin", "text": "我自己那份人设"}
        raw_world["actions"][0]["description"] = "我改过的说明"
        self.store.save_world(raw_world)

        self.store.save_preset("blocks_src", name="分块源")
        # 把当前配置改坏：名字、动作说明、人设、会话全部动过
        broken = self.store.raw_world()
        broken["nodes"][0]["name"] = "改名了"
        broken["actions"][0]["description"] = "被改坏的说明"
        broken["persona"] = {"mode": "astrbot", "text": ""}
        self.store.save_world(broken)
        sessions = self.store.raw_sessions()
        sessions["sessions"] = [item for item in sessions["sessions"] if item["session_id"] != OTHER_SESSION]
        self.store.save_sessions(sessions)

        result = self.store.apply_preset("blocks_src", blocks=["map", "persona"])
        self.assertEqual(sorted(result["blocks"]), ["map", "persona"])
        world = self.store.raw_world()
        self.assertNotEqual(world["nodes"][0]["name"], "改名了")  # 地图换了回来
        self.assertEqual(world["actions"][0]["description"], "被改坏的说明")  # 动作没被换
        self.assertEqual(world["persona"]["text"], "我自己那份人设")  # 人设跟着一起换
        kept = [item["session_id"] for item in self.store.raw_sessions()["sessions"]]
        self.assertNotIn(OTHER_SESSION, kept)  # 会话没被预设覆盖

    async def test_apply_preset_keeps_sessions_by_default(self):
        """不勾会话：白名单与会话组保持原样（切预设不再把会话清掉）。"""

        raw = self.store.raw_sessions()
        raw["sessions"] = [
            {
                "session_id": SESSION,
                "type": "group",
                "platform": "aiocqhttp",
                "enabled": True,
            },
            {
                "session_id": OTHER_SESSION,
                "type": "group",
                "platform": "aiocqhttp",
                "enabled": True,
            },
        ]
        raw["groups"] = [
            {
                "id": "g1",
                "name": "一组",
                "sessions": [OTHER_SESSION],
                "main_session": OTHER_SESSION,
            }
        ]
        self.store.save_sessions(raw)

        # 预设里写一份完全不同的会话白名单（模拟"另一套会话"的预设）
        self.store.save_preset("other_sessions", name="另一套会话")
        preset = self.store.read_preset("other_sessions")
        preset["sessions"] = {"sessions": [], "groups": []}
        self.store.write_preset("other_sessions", preset)

        self.store.apply_preset("other_sessions", blocks=["map", "actions"])
        after = self.store.raw_sessions()
        self.assertEqual(
            sorted(item["session_id"] for item in after["sessions"]),
            sorted([SESSION, OTHER_SESSION]),
        )
        self.assertEqual([item["id"] for item in after["groups"]], ["g1"])

    async def test_states_are_repaired_after_world_change(self):
        """换了地图不清状态：旧地点 / 旧动作要就地修掉，别让她卡在虚空里。"""

        await self.set_state(
            node_id="不存在的房间",
            current_action={"type": "不存在的动作"},
            current_plan={
                "steps": [
                    {"action": "say"},
                    {"action": "不存在的动作"},
                    {"action": "walk_to", "target_node": "不存在的地方"},
                ]
            },
        )
        notes = await self.engine.repair_states_after_config_change()
        self.assertTrue(notes, notes)
        state = await self.get_state()
        self.assertEqual(state.node_id, self.engine.default_node_id())
        self.assertIsNone(state.current_action)
        self.assertEqual(
            [step["action"] for step in (state.current_plan or {}).get("steps") or []],
            ["say"],
        )

    async def test_remember_writes_facts_bonds_and_affinity(self):
        """内置动作「记住」：当场把事实 / 关系 / 好感写进通讯录（带原话）。"""

        ctx = self.ctx(text="我最喜欢喝冰美式了", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx)
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._run_remember(
                state,
                outcome,
                PlannedAction(
                    type="remember",
                    params={
                        "user": "2692047521",
                        "text": "爱喝冰美式，不加糖",
                        "evidence": "我最喜欢喝冰美式了",
                        "kind": "喜好",
                        "bond": "朋友",
                        "affinity_delta": 5,
                    },
                ),
            )
        facts = self.engine.profiles.facts(SESSION, "2692047521")
        self.assertEqual([item["text"] for item in facts], ["爱喝冰美式，不加糖"])
        bonds = [
            item["type"]
            for item in self.engine.profiles.bonds(SESSION, "2692047521", statuses=["current"])
        ]
        self.assertIn("朋友", bonds)
        self.assertGreater(
            float(self.engine.profiles.profile(SESSION, "2692047521")["affinity"]), 0
        )

    async def test_remember_refuses_to_guess(self):
        """没带原话、或者记的是她没见过的人：一律不写进通讯录。"""

        ctx = self.ctx(text="你好", user_id="2692047521", user_name="不相疑")
        await self.engine.handle_incoming(ctx)
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._run_remember(
                state,
                outcome,
                PlannedAction(
                    type="remember",
                    params={"user": "2692047521", "text": "养过一只橘猫"},  # 没带原话
                ),
            )
            await self.engine._run_remember(
                state,
                outcome,
                PlannedAction(
                    type="remember",
                    params={
                        "user": "10001",  # 从没说过话的人
                        "text": "爱喝冰美式",
                        "evidence": "今天又灌了一杯冰美式",
                    },
                ),
            )
        self.assertEqual(self.engine.profiles.facts(SESSION, "2692047521"), [])
        people = [str(row.get("user_id")) for row in self.engine.profiles.list_people(SESSION)]
        self.assertNotIn("10001", people)
        self.assertTrue(any("原话" in item for item in outcome.notes), outcome.notes)

    async def test_arrival_does_not_ask_again_when_something_is_queued(self):
        """走到新地方之后本来就有安排：不要再问一次大模型。"""

        from core import planner as planner_module

        self.llm.replies = []
        self.llm.default_reply = ""
        async with self.engine.session_state(SESSION) as state:
            state.node_id = "study"
            state.pending_arrival = True
            state.current_action = None
            state.current_plan = planner_module.create_plan(
                steps=[{"action": "say", "messages": ["到了"]}],
                world_time=state.world_time,
                reason="落地后还有话说",
            )
        # 这一 tick 会先把计划里的 say 推下去，不该再为"落地"多问一次
        await self.engine.tick()
        arrival_calls = [
            item
            for item in self.llm.calls
            if "你刚刚特地走到了" in str(item.get("prompt") or "")
        ]
        self.assertEqual(arrival_calls, [], "计划里还有下一步时不该为落地再问一次")

    async def test_arrival_still_asks_when_nothing_is_queued(self):
        """只移动、没说到了做什么：还是照旧问一次（这条是原来就有的兜底）。"""

        async with self.engine.session_state(SESSION) as state:
            state.node_id = "study"
            state.pending_arrival = True
            state.current_action = None
            state.current_plan = None
        await self.engine.tick()
        arrival_calls = [
            item
            for item in self.llm.calls
            if "你刚刚特地走到了" in str(item.get("prompt") or "")
        ]
        self.assertTrue(arrival_calls, "空手落地时该问一次接下来做什么")

    # ---------------- 想念：负关系不惦记 / 主动找人额度 / 软推 ----------------

    def _make_close_friend(self, user_id: str = "42", affinity: float = 40.0) -> None:
        """建档 + 定为朋友 + 好感 40：这一级的「每天最多主动找他几次」是 2。"""

        self.engine.profiles.touch(SESSION, user_id, "小明")
        self.engine.profiles.note_bond(
            SESSION, user_id, type="朋友", asserted_by="她的判断"
        )
        self.engine.profiles.adjust_affinity(
            SESSION, user_id, affinity, reason="测试", now=self.engine._now()
        )

    async def test_negative_people_are_not_missed(self):
        """不喜欢的人不惦记：挂过"讨厌的人"，或者好感是负的，想念直接清零。"""

        self.engine.profiles.touch(SESSION, "7", "讨厌鬼")
        self.engine.profiles.note_bond(
            SESSION, "7", type="讨厌的人", asserted_by="她的判断"
        )
        self.engine.profiles.touch(SESSION, "8", "冷淡的人")
        self.engine.profiles.adjust_affinity(SESSION, "8", -30, reason="吵过架")
        async with self.engine.session_state(SESSION) as state:
            state.user_presence = {"7": {"at": 1.0}, "8": {"at": 1.0}}
            state.miss = {"7": 0.9, "8": 0.9}
            self.engine._update_miss(state)
        state = await self.get_state()
        self.assertNotIn("7", state.miss)
        self.assertNotIn("8", state.miss)

    async def test_proactive_contact_quota_follows_the_level(self):
        """「每天最多主动找他几次」按级别算：陌生人 0 次（不主动动手），朋友 2 次。"""

        self.engine.profiles.touch(SESSION, "42", "小明")
        self.assertEqual(self.engine.profiles.proactive_quota_left(SESSION, "42"), 0)
        self._make_close_friend("42")
        self.assertEqual(self.engine.profiles.proactive_quota_left(SESSION, "42"), 2)
        self.engine.profiles.note_proactive(SESSION, "42")
        self.assertEqual(self.engine.profiles.proactive_quota_left(SESSION, "42"), 1)

        # 她主动对某人做事（不是用户日程）：额度是 0 时直接跳过
        await self.set_state(state="idle", current_action=None, current_plan=None)
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._execute_actions(
                state,
                self.engine.node(state.node_id),
                outcome,
                [PlannedAction(type="poke", target="9")],
                depth=0,
                autonomous=True,
            )
        self.assertTrue(
            any("主动找 9 的次数今天用完了" in item for item in outcome.notes),
            outcome.notes,
        )

    async def test_miss_push_sends_her_to_find_him(self):
        """想他想到不行了：软推一次，她自己决定在哪说（这里她选了群）。"""

        self._make_close_friend("42", affinity=40.0)
        await self.set_state(state="idle", current_action=None, current_plan=None)
        async with self.engine.session_state(SESSION) as state:
            state.miss = {"42": 0.95}
        self.llm.replies = [
            '{"plan":[{"action":"say","messages":["突然有点想你了"],"send_to":"群 1001"}],'
            '"valid_until":1800,"reason":"想他了就去找他说一句"}'
        ]
        outcome = await self.engine.maybe_decide(SESSION, force=True)
        self.assertIsNotNone(outcome)
        self.assertTrue(
            any("特别想" in str(item.get("prompt") or "") for item in self.llm.calls),
            "软推那一轮应该把「想他」交代清楚",
        )
        state = await self.get_state()
        self.assertEqual(state.miss.get("42", 0.0), 0.0)
        self.assertEqual(state.miss_push_count, 1)
        self.assertEqual(self.engine.profiles.proactive_quota_left(SESSION, "42"), 1)
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        self.assertTrue(any(item["event_type"] == "miss_push" for item in events))

    async def test_miss_push_can_be_switched_off(self):
        raw = self.store.raw_world()
        raw["profile"]["miss_push_enabled"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        self._make_close_friend("42", affinity=40.0)
        await self.set_state(state="idle", current_action=None, current_plan=None)
        async with self.engine.session_state(SESSION) as state:
            state.miss = {"42": 0.95}
        self.llm.replies = [
            '{"plan":[{"action":"say","messages":["在吗"],"send_to":"群 1001"}],'
            '"valid_until":1800,"reason":"随口一句"}'
        ]
        await self.engine.maybe_decide(SESSION, force=True)
        state = await self.get_state()
        self.assertEqual(state.miss_push_count, 0)

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

    async def test_replied_chat_becomes_a_summary_next_round(self):
        """回应过的那批压成一条概览；她说过的话留着用来提醒别重复句式。"""

        await self.engine.handle_incoming(self.ctx(text="第一句：晚饭吃什么"))
        self.llm.replies = [
            '{"chat_note": "在聊晚饭吃什么",'
            ' "actions": [{"type": "say", "messages": ["吃鱼吧"]}]}'
        ]
        outcome = await self.engine.handle_reply(self.ctx(text="第二句：我想吃鱼"))
        self.assertTrue(outcome.ok, outcome.error)
        # 水位线由"确实发出去"这一步推进（引擎不再自己推）
        await self.engine.mark_chat_replied_by_session(SESSION)

        state = await self.engine.load_state(SESSION, cold_start=False)
        # 水位线之内：不再原样回放（规则判断"有没有新话"用这份）
        fresh = " ".join(str(item.get("text")) for item in self.engine.chat_context(state))
        self.assertNotIn("第一句", fresh)
        self.assertNotIn("第二句", fresh)
        # 但提示词用的是完整窗口：老的压成概览，她自己的话单独列出来
        window = " ".join(str(item.get("text")) for item in self.engine.chat_window(state))
        self.assertIn("第一句", window)
        self.assertIn("吃鱼吧", window)
        preview = self.engine.chat_preview_for(state, SESSION)
        self.assertIn("第一句：晚饭吃什么", preview)
        self.assertIn("你：吃鱼吧", preview)
        # 她说过的话带着"在哪儿说的"
        self.assertIn("吃鱼吧", [str(item.get("text")) for item in state.recent_replies])
        self.assertTrue(
            all(str(item.get("session") or "") for item in state.recent_replies)
        )

        prompt = self.prompts_prompt(state)
        # 回过的那批也原样给她看，但要标着"已经回过了"
        self.assertIn("你已经回过话", prompt)
        self.assertIn("第一句：晚饭吃什么", prompt)
        self.assertIn("吃鱼吧", prompt)
        self.assertIn("你最近说过的话", prompt)

        # 有人又说了一句：新的那句原样出现在「最近在聊什么」里
        await self.engine.handle_incoming(self.ctx(text="第三句：那我也吃鱼"))
        state = await self.engine.load_state(SESSION, cold_start=False)
        prompt = self.prompts_prompt(state)
        self.assertIn("最近在聊什么", prompt)
        self.assertIn("第三句：那我也吃鱼", prompt)

    def prompts_prompt(self, state) -> str:
        """按实跑的方式组装提示词：会话目录、当前会话、会话标签都要带上。"""

        return self.engine.prompts.build_autonomous_system_prompt(
            persona_text="测试人格",
            state=state,
            node=self.engine.node(state.node_id),
            available_tools=self.engine.available_tools(),
            recent_chat=self.engine.chat_window(state),
            session_directory=self.engine.session_directory(state),
            current_session=state.session_id,
            session_labels=self.engine.session_labels(state),
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
            query = await self.engine._parse_recall_query(live, "回忆一下游乐园里的事")
        zone_id = query.get("zone")
        self.assertEqual(zone_id, "", "名字对不上任何区域时不该命中")

        # 区域名出现时必须展开成该区域的全部地点
        async with self.engine.session_state(SESSION) as live:
            query = await self.engine._parse_recall_query(live, "回忆一下公园里的事")
        expected = sorted(node.id for node in self.engine.world.nodes_in_zone("park"))
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

    async def test_pending_images_keep_more_than_the_inline_limit(self):
        """「交几张给主模型看」是 image_max，但队列要多留几张：超出的旧图还要转述。"""

        self.engine.world.context.image_max = 2
        async with self.engine.session_state(SESSION) as state:
            self.engine._note_images(
                state, ["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"]
            )
            self.assertEqual(
                [item["url"] for item in state.pending_images],
                ["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"],
            )

    async def test_pending_images_still_have_a_hard_ceiling(self):
        """队列本身也有上限：群里一直刷图不会把它撑成无限长。"""

        from core.engine import PENDING_IMAGE_KEEP

        urls = [f"https://img/{index}.jpg" for index in range(1, PENDING_IMAGE_KEEP + 6)]
        async with self.engine.session_state(SESSION) as state:
            self.engine._note_images(state, urls)
            kept = [item["url"] for item in state.pending_images]
        self.assertEqual(len(kept), PENDING_IMAGE_KEEP)
        self.assertEqual(kept[-1], urls[-1])

    async def test_image_captions_are_attached_to_their_own_message(self):
        """转述挂回当初发这张图的那条记录上，不会蹭到最新那条消息的注解里。"""

        await self.engine.handle_incoming(
            self.ctx(text="看这张", chat_images=["https://img/1.jpg"])
        )
        await self.engine.handle_incoming(self.ctx(text="还有这张"))
        attached = await self.engine.attach_image_captions(
            SESSION, {"https://img/1.jpg": "一只橘猫趴在键盘上"}
        )
        self.assertEqual(attached, 1)
        state = await self.engine.load_state(SESSION, cold_start=False)
        texts = [str(item.get("text") or "") for item in state.recent_chat]
        self.assertTrue(any("看这张" in text and "橘猫" in text for text in texts), texts)

    async def test_chat_images_come_from_the_record_in_time_order(self):
        """聊天记录里的图按时间取最近的几张，编号从小到大 = 从早到晚。"""

        await self.engine.handle_incoming(
            self.ctx(text="看这张", chat_images=["https://img/1.jpg"])
        )
        await self.engine.handle_incoming(
            self.ctx(
                user_id="7",
                user_name="阿May",
                text="还有这张",
                chat_images=["https://img/2.jpg"],
            )
        )
        picks = await self.engine.chat_images_for_reply(SESSION, 2)
        self.assertEqual(
            [item["url"] for item in picks],
            ["https://img/1.jpg", "https://img/2.jpg"],
        )
        self.assertEqual([item["label"] for item in picks], ["图1", "图2"])
        # 只带一张时取的是最近的那张
        latest = await self.engine.chat_images_for_reply(SESSION, 1)
        self.assertEqual([item["url"] for item in latest], ["https://img/2.jpg"])

    async def test_chat_images_only_use_the_same_session(self):
        """别处的图不进这个会话的记录：不然她会在群里答私聊的图。"""

        self.store.add_session(
            OTHER_SESSION, session_type="group", platform="aiocqhttp"
        )
        self.engine.reload_config()
        await self.engine.handle_incoming(
            self.ctx(session_id=OTHER_SESSION, text="那边的图", chat_images=["https://img/x.jpg"])
        )
        picks = await self.engine.chat_images_for_reply(SESSION, 3)
        self.assertEqual(picks, [])

    async def test_chat_images_are_ignored_when_limit_is_zero(self):
        """配置成 0 / 负数时一张都不带。"""

        await self.engine.handle_incoming(
            self.ctx(text="看这张", chat_images=["https://img/1.jpg"])
        )
        self.assertEqual(await self.engine.chat_images_for_reply(SESSION, 0), [])

    def test_the_same_message_registered_twice_becomes_one_record(self):
        """同一条消息被两个钩子（或重注入的副本）各记一次：只留一条。"""

        first = self.engine.register_incoming(SESSION, "在吗")
        second = self.engine.register_incoming(SESSION, "在吗")
        self.assertIs(first, second)
        # 隔得久的那条是另一次发言，照常记一条新的
        first["at"] -= 60.0
        third = self.engine.register_incoming(SESSION, "在吗")
        self.assertIsNot(first, third)

    async def test_forward_summary_cache_round_trip(self):
        """转发摘要的缓存：写进去、命中计数、按最近使用淘汰。"""

        await self.db.call(
            "put_forward_summary", fingerprint="fp1", summary="张三说团建改到三点"
        )
        entry = await self.db.call("get_forward_summary", fingerprint="fp1")
        self.assertEqual(entry["summary"], "张三说团建改到三点")
        self.assertIsNone(await self.db.call("get_forward_summary", fingerprint="nope"))

        await self.db.call("touch_forward_summary", fingerprint="fp1")
        entry = await self.db.call("get_forward_summary", fingerprint="fp1")
        self.assertEqual(int(entry["hits"]), 1)

        await self.db.call("put_forward_summary", fingerprint="fp2", summary="另一条")
        await self.db.call("trim_forward_cache", keep=2)
        self.assertIsNotNone(await self.db.call("get_forward_summary", fingerprint="fp1"))
        await self.db.call("touch_forward_summary", fingerprint="fp2")
        await self.db.call("trim_forward_cache", keep=1)
        self.assertIsNotNone(await self.db.call("get_forward_summary", fingerprint="fp2"))
        self.assertIsNone(await self.db.call("get_forward_summary", fingerprint="fp1"))

    async def test_persona_brief_survives_a_persona_change(self):
        """人设指纹变了（换人格 / 换会话）时，编辑器还能拿回上一次那份。"""

        await self.engine.save_persona_brief(
            SESSION, "说话软一点，爱用「呀」结尾", source="generated"
        )
        state = await self.engine.persona_brief_state(SESSION)
        self.assertEqual(state["brief"], "说话软一点，爱用「呀」结尾")
        self.assertEqual(state["latest"]["brief"], "说话软一点，爱用「呀」结尾")

        # 换了一份完全不同的人设：指纹对不上，但"最近一份"还在
        self.persona.text = "换了一个完全不同的人格"
        stale = await self.engine.persona_brief_state(SESSION)
        self.assertEqual(stale["brief"], "")
        self.assertEqual(stale["latest"]["brief"], "说话软一点，爱用「呀」结尾")
        # 运行时的兜底不变：指纹对不上就退回主人设前 200 字
        brief = await self.engine.event_persona_brief(SESSION)
        self.assertIn("换了一个完全不同的人格", brief)

    def test_duration_is_read_from_what_she_said(self):
        """「小睡三小时」这类话要能读成秒数，读不出就不能瞎猜。"""

        self.assertEqual(_guess_duration_seconds("小睡三小时回复精力"), 3 * 3600)
        self.assertEqual(_guess_duration_seconds("睡两个小时养养精神"), 2 * 3600)
        self.assertEqual(_guess_duration_seconds("睡个半小时"), 30 * 60)
        self.assertEqual(_guess_duration_seconds("一个半小时差不多了"), 90 * 60)
        self.assertEqual(_guess_duration_seconds("眯 20 分钟"), 20 * 60)
        self.assertEqual(_guess_duration_seconds("躺个十分钟就走"), 10 * 60)
        self.assertEqual(_guess_duration_seconds(""), 0)
        self.assertEqual(_guess_duration_seconds("随便歇会儿"), 0)

    async def test_prompt_previews_show_the_state_slot(self):
        """调试页那两个预览要和实跑一致：状态槽（今日穿搭这类）必须看得到。"""

        now = self.engine._now()
        async with self.engine.session_state(SESSION) as state:
            state.external_state = {
                "outfit": {
                    "text": "白色卫衣配灰裤子",
                    "label": "今日穿搭",
                    "at": now,
                    "expires_at": 0.0,
                }
            }
        self.engine.reload_config()

        for mode in ("inject", "autonomous"):
            text = (
                await self.engine.preview_injection(SESSION)
                if mode == "inject"
                else await self.engine.preview_autonomous_prompt(SESSION)
            )
            self.assertIn("今日穿搭", text, mode)
            self.assertIn("白色卫衣配灰裤子", text, mode)

    async def test_state_slot_recorded_by_a_command_action_reaches_the_prompt(self):
        """指令型动作配了状态槽：结果留在状态里，下一轮提示词和预览都看得到。"""

        from core.ports import ToolCallResult

        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "refresh_outfit",
                "name": "刷新穿搭",
                "category": "instant",
                "llm_level": "command",
                "scope": "global",
                "trigger_command": "刷新穿搭",
                "target_type": "none",
                "duration": 60,
                "enabled": True,
                "state_slot": "outfit",
                "state_label": "今日穿搭",
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        self.engine.commands = _RecordingCommands(
            [], ToolCallResult(ok=True, text="白色卫衣配灰裤子", tool="刷新穿搭")
        )

        outcome = TickOutcome(session_id=SESSION)
        async with self.engine.session_state(SESSION) as state:
            node = self.engine.node(state.node_id)
            await self.engine._execute_actions(
                state,
                node,
                outcome,
                [PlannedAction(type="refresh_outfit", intent="换一身适合今天的")],
                depth=0,
                autonomous=False,
            )
            self.assertIn("outfit", state.external_state)
            self.assertEqual(state.external_state["outfit"]["text"], "白色卫衣配灰裤子")

        for text in (
            await self.engine.preview_injection(SESSION),
            await self.engine.preview_autonomous_prompt(SESSION),
        ):
            self.assertIn("今日穿搭", text)
            self.assertIn("白色卫衣配灰裤子", text)

    async def test_continuous_action_records_its_state_slot_right_away(self):
        """持续型指令动作：指令一跑完就把结果记进状态槽，不等占位结束。"""

        from core.ports import ToolCallResult

        self.stub_commands(ToolCallResult(ok=True, text="今天穿的是白色卫衣", tool="穿搭"))
        self.add_command_action(
            category="continuous",
            duration=600,
            state_slot="outfit",
            state_label="今日穿搭",
        )
        definition = self.engine.world.action_map()["ask_weather"]
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._start_action(
                state,
                self.engine.node("study"),
                outcome,
                definition,
                PlannedAction(type="ask_weather", intent="刷新今天的穿搭"),
                0,
                False,
            )
            # 动作还在占位，状态槽已经写好了
            self.assertIn("outfit", state.external_state)
            self.assertEqual(
                state.external_state["outfit"]["text"], "今天穿的是白色卫衣"
            )
            self.assertTrue((state.current_action or {}).get("slot_recorded"))

        slots = await self.engine.preview_state_slots(SESSION)
        self.assertEqual([item["label"] for item in slots], ["今日穿搭"])
        self.assertFalse(slots[0]["expired"])

    async def test_state_slot_report_flags_expired_ones(self):
        """过期的那条不会进提示词，调试页要能说出原因。"""

        now = self.engine._now()
        async with self.engine.session_state(SESSION) as state:
            state.external_state = {
                "outfit": {
                    "text": "白色卫衣",
                    "label": "今日穿搭",
                    "at": now - 7200,
                    "expires_at": now - 60,
                },
                "bag": {
                    "text": "背包里有伞",
                    "label": "背包",
                    "at": now,
                    "expires_at": 0.0,
                },
            }

        slots = {item["slot"]: item for item in await self.engine.preview_state_slots(SESSION)}
        self.assertTrue(slots["outfit"]["expired"])
        self.assertFalse(slots["bag"]["expired"])
        text = await self.engine.preview_autonomous_prompt(SESSION)
        self.assertNotIn("白色卫衣", text)
        self.assertIn("背包里有伞", text)

    async def test_preview_hides_actions_that_ran_out_of_quota(self):
        """配额用完的动作实跑不出现，预览也不该出现（预览=实跑）。"""

        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "search_web":
                action["quota"] = {"day": 1}
        self.store.save_world(raw)
        self.engine.reload_config()

        before = await self.engine.preview_autonomous_prompt(SESSION)
        self.assertIn("- search_web：", before)

        async with self.engine.session_state(SESSION) as state:
            definition = self.engine.world.action_map()["search_web"]
            self.engine._count_action_use(state, definition)

        after = await self.engine.preview_autonomous_prompt(SESSION)
        # 动作清单里不再列出它（"你能去的地方"那张表只列动作名字，不受影响）
        self.assertNotIn("- search_web：", after)

    async def test_nap_length_follows_what_she_said(self):
        """大模型把时长写在 intent 里（duration 忘了填）时，按她说的算，不是一律最短。"""

        nap = self.engine.world.action_map()["nap"]
        nap.duration_min = 600
        nap.duration_max = 10800
        state = await self.get_state()

        def ticks(intent: str, duration: int = 0) -> int:
            action = PlannedAction(type="nap", intent=intent, duration=duration)
            return self.engine._duration_ticks(nap, action, state)

        self.assertEqual(ticks("小睡三小时回复精力"), 180)  # 3 小时
        self.assertEqual(ticks("小睡一会儿", 7200), 120)  # 自己填了 duration 就用它
        self.assertEqual(ticks("眯十个小时"), 180)  # 超出上限 → 夹住
        self.assertEqual(ticks("随便眯一下"), 10)  # 什么都没说 → 下限

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

    async def test_generated_text_also_gets_the_session_directory(self):
        """"现写一句"这条路（say 没给文案 / 分享 / 单轮动作）也要带「你能说话的地方」。"""

        self.add_group(sessions=[SESSION, PRIVATE_SESSION])
        self.set_session_note(PRIVATE_SESSION, "主人")
        self.llm.replies = ["晚安"]
        state = await self.get_state()
        await self.engine._generate_text_actions(
            state, None, "说一句晚安", session_id=PRIVATE_SESSION
        )
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("你能说话的地方", prompt)
        self.assertIn("换地方说就写 send_to", prompt)

    async def test_autonomous_say_never_echoes_a_json_payload(self):
        """模型只给了推理、没给要说的话：保持安静，不能把整段 JSON 当台词发出去。"""

        self.llm.replies = [
            '{"reasoning": {"env": "客厅", "state": "发呆"}, "actions": []}'
        ]
        state = await self.get_state()
        spoken = await self.engine._generate_text_actions(
            state, None, "你现在想和群里的人说点什么。"
        )
        self.assertEqual(spoken, [])

    async def test_autonomous_say_still_sends_a_plain_sentence(self):
        """模型没按 JSON 回、但确实写了一句话：这句照发。"""

        self.llm.replies = ["今天天气真好呀"]
        state = await self.get_state()
        spoken = await self.engine._generate_text_actions(
            state, None, "你现在想和群里的人说点什么。"
        )
        self.assertEqual(spoken, ["今天天气真好呀"])

    async def test_autonomous_say_stays_silent_when_the_model_says_nothing(self):
        """模型给不出话时**保持安静**：不再拿一句跟人设无关的通用短句顶上。"""

        self.llm.replies = ['{"reasoning": {"env": "客厅"}, "actions": []}']
        state = await self.get_state()
        spoken = await self.engine._generate_text_actions(
            state,
            None,
            "你现在想和群里的人说点什么。",
        )
        self.assertEqual(spoken, [])

    async def test_autonomous_say_exhausted_budget_goes_silent(self):
        """自主发言的预算用完之后不再说话（以前会退到内置短句池）。"""

        state = await self.get_state()
        state.llm_text_hour_marker = self.engine._hour_index(state)
        state.llm_text_count_hour = int(
            self.engine.world.limits.max_llm_text_per_hour
        )
        self.llm.replies = ['{"actions":[{"type":"say","messages":["在的在的"]}]}']
        spoken = await self.engine._generate_text_actions(
            state, None, "你现在想和群里的人说点什么。"
        )
        self.assertEqual(spoken, [])
        self.assertEqual(self.llm.calls, [])

    async def test_autonomous_prompt_carries_the_ignored_warning(self):
        """自主开口时提示词里要写明"刚才没人接"，并叮嘱别追着说。"""

        self.llm.replies = ['{"actions":[{"type":"say","messages":["哦。"]}]}']
        state = await self.get_state()
        state.unanswered_count = 2
        state.awaiting_reply = False

        await self.engine._generate_text_actions(
            state, None, "你现在想和群里的人说点什么。"
        )

        prompt = self.llm.calls[-1]["prompt"]
        self.assertIn("刚才的情况", prompt)
        self.assertIn("没人接", prompt)
        self.assertIn("保持安静", prompt)

    async def test_voice_signal_follows_the_last_judged_tone(self):
        """声音样例按上一轮模型判的口吻挑场景：被夸挑"哄人"那组，而不是随机。"""

        persona = self.engine.world.persona
        persona.samples = [
            {"id": "a", "scene": "snap", "text": "你说话注意点"},
            {"id": "b", "scene": "comfort", "text": "怎么啦，跟我说说"},
        ]
        state = await self.get_state()
        state.last_voice_samples = []
        state.last_user_tone = "praise"
        picked = self.engine.voice_sample_lines(state, SESSION, signal=self.engine.tone_signal(state, ""))
        self.assertEqual([item["id"] for item in picked][:1], ["b"])

        state.last_voice_samples = []
        state.last_user_tone = "attack"
        picked = self.engine.voice_sample_lines(state, SESSION, signal=self.engine.tone_signal(state, ""))
        self.assertEqual([item["id"] for item in picked][:1], ["a"])

    async def test_chat_samples_only_pick_answered_lines(self):
        """从聊天里挑样例：只挑她说过、而且后面有人接话的那几句。"""

        state = await self.get_state()
        state.recent_chat = [
            {"user_id": "9", "name": "老普", "text": "今晚吃啥", "at": 1.0, "seq": 1, "origin": SESSION},
            {"user_id": "", "name": "小鲸鱼", "text": "本小姐也不知道", "at": 2.0, "seq": 2, "is_self": True, "origin": SESSION},
            {"user_id": "9", "name": "老普", "text": "那随便吃点吧", "at": 3.0, "seq": 3, "origin": SESSION},
            {"user_id": "", "name": "小鲸鱼", "text": "有人吗", "at": 4.0, "seq": 4, "is_self": True, "origin": SESSION},
        ]
        found = self.engine.voice_sample_candidates_from_chat(state, limit=5)
        texts = [item["text"] for item in found]
        self.assertIn("本小姐也不知道", texts)
        self.assertNotIn("有人吗", texts, "没人接的那句不该被学走")
        self.assertTrue(found[0]["context"])

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
                action["tool_flow"] = "simple"
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
        # 白天只小睡、整觉留给夜里：否则下午躺下、醒来正好是半夜
        self.assertIn("白天只是犯困就先小睡一会儿", prompt)
        self.assertIn("别在白天一睡八小时", prompt)
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

        raw = self.store.raw_world()
        raw["sleep"] = {
            "startled": {"enabled": False}  # 这条专门测固定文案，先把"迷糊惊醒"关掉
        }
        self.store.save_world(raw)
        self.engine.reload_config()
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

    async def test_a_noisy_group_wakes_her_groggily_once(self):
        """睡着时群里吵得厉害：迷糊醒一次（一段睡眠只一次），还会掉一点精力。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        async with self.engine.session_state(SESSION) as state:
            state.sleep_started_at = int(state.world_time) - 120  # 已经睡了两小时
            before = float(state.energy)

        blocked = [
            await self.engine.should_block_sleep(
                self.ctx(text="随便聊聊", is_mentioned=False, is_wake=False)
            )
            for _ in range(8)
        ]
        self.assertEqual(blocked[:-1], [True] * 7)  # 前面照旧挡着
        self.assertFalse(blocked[-1], "吵到第 8 条就该放行让她回一句")

        state = await self.get_state()
        self.assertEqual(state.state, "sleeping", "惊醒不是真醒：她还在这段睡眠里")
        self.assertEqual(state.startled_count, 1)
        self.assertGreater(state.startled_until, int(state.world_time))
        self.assertLess(state.energy, before, "被吵醒要掉一点精力")
        self.assertGreater(state.grumpy_until, int(state.world_time))

        # 一段睡眠只惊醒一次：再来一轮刷屏也只是挡下
        self.clock.advance(10 * 60)
        more = [
            await self.engine.should_block_sleep(
                self.ctx(text="继续聊", is_mentioned=False, is_wake=False)
            )
            for _ in range(10)
        ]
        self.assertTrue(all(more), "一段睡眠最多惊醒一次")
        state = await self.get_state()
        self.assertEqual(state.startled_count, 1)

    async def test_startled_window_lets_her_reply_without_resetting_the_sleep(self):
        """迷糊窗口里她可以回话，但睡眠还是同一段：不重新计时、不用固定文案。"""

        await self.set_state(
            state="sleeping",
            current_action={"type": "sleep"},
            world_time=600,
        )
        async with self.engine.session_state(SESSION) as state:
            state.sleep_started_at = int(state.world_time) - 240
            started = int(state.sleep_started_at)
            state.startled_until = int(state.world_time) + 3
            state.startled_note = "你刚才被吵醒了"

        reply = await self.engine.sleep_gate(self.ctx(text="在吗", is_mentioned=True))
        self.assertIsNone(reply, "迷糊窗口里该走正常回复，而不是那句固定文案")
        state = await self.get_state()
        self.assertEqual(state.state, "sleeping")
        self.assertEqual(state.sleep_started_at, started, "接着睡同一段，不重新计时")

    async def test_startled_window_only_allows_talking(self):
        """半睡半醒的那一轮只能说话 / 想事情：不许出门、不许接着做别的。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        async with self.engine.session_state(SESSION) as state:
            state.startled_until = int(state.world_time) + 3
            before_node = str(state.node_id)
        self.llm.replies = [
            '{"actions":[{"type":"walk_to","target_node":"kitchen"},'
            '{"type":"say","messages":["唔…"]}]}'
        ]
        outcome = await self.engine.handle_reply(self.ctx(text="在吗"))
        self.assertTrue(outcome.ok, outcome.error)
        state = await self.get_state()
        self.assertEqual(state.node_id, before_node, "被吵醒的这一轮不该走动")
        self.assertTrue(
            any("只让她说话" in item for item in outcome.warnings), outcome.warnings
        )

    async def test_short_sleep_leaves_her_grumpy(self):
        """没睡够就醒了：有起床气（效价掉、心潮涨），并且写进提示词。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        async with self.engine.session_state(SESSION) as state:
            state.sleep_started_at = int(state.world_time) - 60
            before_valence = float(state.valence)
            self.engine._apply_wake_quality(state, slept_minutes=45.0)
            self.assertLess(state.valence, before_valence)
            self.assertGreater(state.grumpy_until, int(state.world_time))
            notes = self.engine.sleep_notes(state)
        self.assertTrue(any("起床气" in item for item in notes), notes)

    async def test_full_sleep_leaves_her_rested(self):
        """睡够了：清爽，没有起床气。"""

        await self.set_state(state="sleeping", current_action={"type": "sleep"})
        async with self.engine.session_state(SESSION) as state:
            state.sleep_started_at = int(state.world_time) - 480
            before_valence = float(state.valence)
            self.engine._apply_wake_quality(state, slept_minutes=480.0)
            self.assertGreater(state.valence, before_valence)
            self.assertEqual(state.grumpy_until, 0)
            notes = self.engine.sleep_notes(state)
        self.assertFalse(any("起床气" in item for item in notes), notes)

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
        raw_world.setdefault("limits", {})["max_share_per_hour"] = 1
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
        await self.set_state(
            state="sleeping",
            current_action={"type": "sleep", "duration_ticks": 900, "elapsed_ticks": 0},
            bot_base_nickname="小鲸鱼",
        )
        await self.engine.tick()
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        nicknames = [item for item in events if item["event_type"] == "nickname"]
        self.assertTrue(nicknames, events)
        self.assertFalse(nicknames[0]["detail"]["ok"])
        self.assertIn("测试里指定失败", nicknames[0]["detail"]["note"])

    async def test_nickname_failures_back_off(self):
        """协议端挂掉时，群名片不能每 tick 都重试（否则日志一直刷）。"""

        self.messenger.card_result = False
        await self.set_state(
            state="sleeping",
            current_action={"type": "sleep", "duration_ticks": 900, "elapsed_ticks": 0},
            bot_base_nickname="小鲸鱼",
        )
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.nickname_fail_count, 1)
        attempts = len(self.messenger.cards)

        for _ in range(3):
            await self.engine.tick()
        self.assertEqual(len(self.messenger.cards), attempts)

    async def test_nickname_backoff_resets_after_success(self):
        self.messenger.card_result = False
        await self.set_state(
            state="sleeping",
            current_action={"type": "sleep", "duration_ticks": 900, "elapsed_ticks": 0},
            bot_base_nickname="小鲸鱼",
        )
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
        await self.set_state(
            state="sleeping",
            current_action={"type": "sleep", "duration_ticks": 900, "elapsed_ticks": 0},
            bot_base_nickname="小鲸鱼",
        )
        await self.engine.tick()
        state = await self.get_state()
        self.assertIn("睡觉中", state.bot_current_nickname)
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        nicknames = [item for item in events if item["event_type"] == "nickname"]
        self.assertTrue(nicknames, events)
        self.assertTrue(nicknames[0]["detail"]["ok"])

    async def test_nickname_sync_sets_group_card(self):
        await self.set_state(
            state="sleeping",
            current_action={"type": "sleep", "duration_ticks": 900, "elapsed_ticks": 0},
            bot_base_nickname="小鲸鱼",
        )
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

        self.tools._tools = {"get_current_weather": "查天气"}
        self.engine.tools.schemas["get_current_weather"] = {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市"}},
            "required": ["city"],
        }
        self.set_schedules(
            [
                {
                    "id": "search_missing",
                    "enabled": True,
                    "time": "16:00",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": [{"type": "check_weather"}],
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
                        {"type": "check_weather", "params": {"city": "武汉"}}
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
        self.assertEqual(self.tools.calls[0][1]["city"], "武汉")

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

    async def test_takeover_reply_carries_generated_images(self):
        """回复路径里生图动作出的图，要跟着 ReplyOutcome 一起交回给发送方。"""

        self.tools._tools = {"text2img": "画图"}
        self.tools.results = {"text2img": "画好了"}
        self.tools.images = {"text2img": ["https://img/selfie.png"]}
        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "selfie_shot",
                "name": "自拍",
                "category": "instant",
                "llm_level": "tool",
                "tool_names": ["text2img"],
                "scope": "global",
                "visible": True,
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        self.llm.replies = [
            '{"reasoning":{"env":"书房","state":"刚醒","mood":"开心",'
            '"who":"小明要我拍一张","intent":"拍一张自拍给他"},'
            '"actions":[{"type":"selfie_shot","intent":"拍一张今天的自拍"}]}',
            SAY_REPLY,
            SAY_REPLY,
        ]
        ctx = self.ctx(text="来张自拍")
        await self.engine.handle_incoming(ctx)
        outcome = await self.engine.handle_reply(ctx)

        self.assertEqual([name for name, _params in self.tools.calls], ["text2img"])
        self.assertEqual(outcome.images, ["https://img/selfie.png"])
        events = await self.db.call("query_events", session_id=SESSION, limit=10)
        reply_events = [item for item in events if item["event_type"] == "reply"]
        self.assertEqual(reply_events[0]["detail"]["images"], 1)

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

    # ---------------- 天气 ----------------

    def use_weather_tool(self, result: str = "武汉 26℃ 多云") -> None:
        self.tools._tools = {"get_current_weather": "查天气"}
        self.tools.schemas["get_current_weather"] = {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        }
        self.tools.results = {"get_current_weather": result}

    def set_weather_city(self, city: str = "武汉") -> None:
        raw = self.store.raw_world()
        raw["weather"] = {**(raw.get("weather") or {}), "city": city}
        self.store.save_world(raw)
        self.engine.reload_config()

    def set_weather_command(self, command: str = "天气") -> None:
        """把内置「查天气」改成指令型：不选工具，改成触发一条指令。"""

        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "check_weather":
                action["llm_level"] = "command"
                action["trigger_command"] = command
                action["tool_names"] = []
        self.store.save_world(raw)
        self.engine.reload_config()

    def stub_command_channel(self, result) -> list[str]:
        """记录触发过的指令；返回那个列表，方便断言她到底发了什么。"""

        lines: list[str] = []

        class _Commands:
            async def trigger(self, session_id, command, *, event=None):
                lines.append(command)
                return result

        self.engine.commands = _Commands()
        return lines

    def set_search_topic(self, topic: str, action_id: str = "search_web") -> None:
        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == action_id:
                action["search_topic"] = topic
        self.store.save_world(raw)
        self.engine.reload_config()

    async def test_manual_weather_query_is_kept_as_context(self):
        """她主动查了天气：结果进全局记录，下一轮提示词里就有这一段。"""

        self.use_weather_tool()
        self.set_weather_city()
        await self.set_state(node_id="study")
        self.llm.replies = ["武汉｜26℃｜多云｜湿度68%｜东南风3级｜夜里转小雨"]

        await self.engine.refresh_weather(SESSION)

        record = await self.engine.weather_record()
        self.assertEqual(record.text, "武汉｜26℃｜多云｜湿度68%｜东南风3级｜夜里转小雨")
        self.assertEqual(record.parts.get("city"), "武汉")

        self.llm.replies = [SAY_REPLY]
        await self.engine.handle_reply(self.ctx())
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("# 外面的天气", prompt)
        self.assertIn("武汉｜26℃", prompt)

    async def test_weather_refresh_follows_the_interval(self):
        """后台静默刷新按间隔来：没到点不查，到点查一次。"""

        self.use_weather_tool()
        self.set_weather_city()
        await self.set_state(node_id="study")

        await self.engine.maybe_refresh_weather()
        self.assertEqual(len(self.tools.calls), 1)
        # 间隔没到：再调也不查
        await self.engine.maybe_refresh_weather()
        self.assertEqual(len(self.tools.calls), 1)

        self.clock.advance(3 * 3600)
        await self.engine.maybe_refresh_weather()
        self.assertEqual(len(self.tools.calls), 2)

    async def test_manual_query_pushes_the_refresh_back(self):
        """她主动查过之后，后台刷新的倒计时从头算。"""

        self.use_weather_tool()
        self.set_weather_city()
        await self.set_state(node_id="study")

        await self.engine.refresh_weather(SESSION)
        self.assertEqual(len(self.tools.calls), 1)
        await self.engine.maybe_refresh_weather()
        self.assertEqual(len(self.tools.calls), 1)

        self.clock.advance(3 * 3600)
        await self.engine.maybe_refresh_weather()
        self.assertEqual(len(self.tools.calls), 2)

    async def test_weather_refresh_works_when_the_action_is_a_command(self):
        """「查天气」配成指令型时，地图上的刷新也要走得通（不用再选天气工具）。"""

        from core.ports import ToolCallResult

        self.set_weather_command("天气")
        self.set_weather_city("武汉")
        await self.set_state(node_id="study")
        lines = self.stub_command_channel(
            ToolCallResult(ok=True, text="武汉｜26℃｜多云｜湿度68%", tool="天气")
        )
        self.llm.replies = ["/天气 武汉"]

        note = await self.engine.maybe_refresh_weather(force=True, session_id=SESSION)

        self.assertEqual(note, "")
        self.assertEqual(lines, ["/天气 武汉"])
        record = await self.engine.weather_record()
        self.assertEqual(record.text, "武汉｜26℃｜多云｜湿度68%")

    async def test_command_weather_failure_is_explained_honestly(self):
        """指令型没跑成时要说清是指令的事，别再让人去配工具。"""

        from core.ports import ToolCallResult

        self.set_weather_command("天气")
        self.set_weather_city("武汉")
        await self.set_state(node_id="study")
        self.stub_command_channel(
            ToolCallResult(ok=False, error="没找到指令「天气」", tool="天气")
        )

        note = await self.engine.maybe_refresh_weather(force=True, session_id=SESSION)

        self.assertIn("没找到指令", note)
        self.assertNotIn("天气工具", note)

    async def test_manual_weather_query_ignores_the_background_interval(self):
        """后台刷新间隔填 0（只在手动点的时候查）时，点「刷新」仍然要真的去查。"""

        self.use_weather_tool()
        self.set_weather_city()
        raw = self.store.raw_world()
        raw["weather"] = {**(raw.get("weather") or {}), "refresh_hours": 0}
        self.store.save_world(raw)
        self.engine.reload_config()
        await self.set_state(node_id="study")

        note = await self.engine.maybe_refresh_weather(force=True, session_id=SESSION)

        self.assertEqual(note, "")
        self.assertEqual(len(self.tools.calls), 1)

    async def test_weather_image_is_read_by_the_describer(self):
        """天气工具返回图片时，先用看图模型读成文字再存。"""

        class _Describer:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            async def describe_to_text(self, images, prompt=""):
                self.calls.append(list(images))
                return "武汉｜26℃｜多云｜湿度68%｜东南风3级"

        describer = _Describer()
        self.engine.describer = describer
        state = await self.set_state(node_id="study")
        definition = self.engine.world.action_map()["check_weather"]

        record = await self.engine._store_weather(
            state,
            definition,
            {"tool_images": ["http://example.com/weather.png"]},
            source="auto",
        )

        self.assertEqual(describer.calls, [["http://example.com/weather.png"]])
        self.assertEqual(record.parts.get("city"), "武汉")
        self.assertEqual((await self.engine.weather_record()).text, record.text)

    async def test_stale_weather_is_not_written_into_the_prompt(self):
        """超过「多旧就不再提」的时间：提示词里不再带这份天气。"""

        await self.engine.db.call(
            "kv_set",
            "weather",
            {
                "text": "武汉｜26℃",
                "parts": {"city": "武汉", "temp": "26℃"},
                "at": self.clock.now() - 5 * 86400,
            },
        )
        self.assertEqual(await self.engine.weather_line(), "")

        # 横幅仍然看得到，只是标着"多久之前"
        payload = await self.engine.weather_payload()
        self.assertEqual(payload["temp"], "26℃")
        self.assertIn("天前", payload["age"])

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
            if text.startswith(("🔧", "📥", "▶️", "✅", "🎬", "⏭️", "🧠", "🌐"))
        ]
        self.assertTrue(echoed, outcomes[0].debug_messages)
        # 联网检索期间的工具调用只在日志里；群里看到的是一条「正在联网搜索」
        self.assertTrue(any("🌐" in text for text in echoed), echoed)
        self.assertFalse([text for text in echoed if text.startswith("🔧")], echoed)
        # 回显不算"她说过的话"：不进聊天上下文（她自己真正说的那句才算）
        state = await self.get_state()
        self.assertFalse(
            [item for item in state.recent_chat if "🔧" in str(item.get("text", ""))],
            state.recent_chat,
        )

    async def test_tool_echo_is_sent_before_the_followup_say(self):
        """先调工具、结果回来才说话：回显也要按这个顺序排（工具在前）。"""

        raw = self.store.raw_world()
        raw["echo_types"] = ["tool_call", "tool_result", "search"]
        self.store.save_world(raw)
        self.engine.reload_config()
        self.bind_search_flow(["web_search"], depth="quick")
        await self.set_state(node_id="study")
        # 材料够多才值得压成要点：一两条短摘要直接用证据块，不会多花一次调用
        self.tools.results["web_search"] = (
            "科技新闻一\nhttps://news.example/a " + "今天有一条芯片行业的消息。" * 8 + "\n"
            "科技新闻二\nhttps://news.example/b " + "另外还有一条 AI 模型的进展。" * 8
        )
        self.llm.replies = [
            '{"actions":[{"type":"search_web","intent":"查今天的新闻",'
            '"queries":["今天的新闻"]}]}',
            "结论：今天有三条科技新闻\n1. 芯片行业有新动态（1）",
            '{"actions":[{"type":"say","messages":["查到了三条"]}]}',
        ]

        outcome = await self.engine.handle_reply(self.ctx())
        ordered = outcome.ordered_messages()

        self.assertIn("查到了三条", ordered)
        # 检索期间的工具行不再进群，群里是那条「正在联网搜索」，它要排在她说话之前
        tool_at = next(
            index for index, line in enumerate(ordered) if line.startswith("🌐")
        )
        say_at = ordered.index("查到了三条")
        self.assertLess(tool_at, say_at, ordered)
        # 交给主模型的是"要点+编号"，不是原始证据块
        self.assertIn("芯片行业有新动态", self.llm.calls[-1]["prompt"])

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
        """只勾了「工具调用 + 联网搜索」时：检索期间的工具行不出现，只出现那条联网搜索。"""

        raw = self.store.raw_world()
        raw["echo_types"] = ["tool_call", "search"]
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
            if text.startswith(("🔧", "📥", "▶️", "✅", "🎬", "⏭️", "🧠", "🌐"))
        ]
        self.assertTrue(echoed, outcomes[0].debug_messages)
        self.assertTrue(all(text.startswith("🌐") for text in echoed), echoed)

    async def test_legacy_echo_switch_migrates_to_the_common_set(self):
        """老配置里的 echo_actions: true 会变成「常用」那几类。"""

        raw = self.store.raw_world()
        raw["echo_actions"] = True
        self.store.save_world(raw)
        self.engine.reload_config()
        common = {
            "plan",
            "action_start",
            "action_done",
            "action",
            "search",
            "tool_call",
            "tool_result",
            "skip",
        }
        self.assertEqual(set(self.engine.world.echo_modes), common)
        self.assertTrue(
            all(mode == "full" for mode in self.engine.world.echo_modes.values())
        )
        self.assertEqual(self.engine.echo_types(), common)
        # 老字段已经迁进 echo_modes，不再留在配置里
        self.assertEqual(self.engine.world.echo_types, [])

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
        """工具型动作缺必填参数就跳过，跳过原因要写进日志。"""

        self.tools._tools = {"get_current_weather": "查天气"}
        self.engine.tools.schemas["web_search"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.engine.tools.schemas["get_current_weather"] = {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        }
        self.set_schedules(
            [
                {
                    "id": "search_log",
                    "enabled": True,
                    "time": "19:00",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "action_chain": [{"type": "check_weather"}],
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

    async def test_once_schedule_fires_on_its_date_then_deletes_itself(self):
        """一次性日程：只认自己那一天，跑完就把自己删掉。"""

        self.add_schedule(
            id="once_thing",
            time="12:00",
            once=True,
            date="2026-09-15",
            days=["mon"],
            note="主人说下午可能下雨，记得收衣服",
            action_chain=[{"type": "say", "messages": ["把衣服收了"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))  # 周二：星期表对一次性日程不管用
        await self.engine.tick()
        self.assertIn("把衣服收了", self.messenger.flat_messages)
        self.assertEqual(self.store.raw_schedules()["schedules"], [])
        events = await self.db.call("query_events", session_id=SESSION, limit=20)
        fired = [item for item in events if item["event_type"] == "schedule"]
        self.assertTrue(fired, events)
        self.assertTrue(fired[0]["detail"]["once"])
        self.assertIn("收衣服", fired[0]["detail"]["note"])

    async def test_once_schedule_waits_for_its_own_date(self):
        """日期还没到的一次性日程不会提前跑掉。"""

        self.add_schedule(
            id="tomorrow_thing",
            time="12:00",
            once=True,
            date="2026-09-16",
            action_chain=[{"type": "say", "messages": ["明天的事"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 12, 0))
        await self.engine.tick()
        self.assertEqual(self.messenger.flat_messages, [])
        self.assertEqual(len(self.store.raw_schedules()["schedules"]), 1)

    async def test_overdue_once_schedule_is_pruned(self):
        """日期已经过去的一次性日程留着没用，顺手清掉。"""

        self.add_schedule(
            id="yesterday_thing",
            time="12:00",
            once=True,
            date="2026-09-14",
            action_chain=[{"type": "say", "messages": ["昨天的事"]}],
        )
        self.clock.set_struct(datetime(2026, 9, 15, 9, 0))
        await self.engine.tick()
        self.assertEqual(self.store.raw_schedules()["schedules"], [])
        self.assertEqual(self.messenger.flat_messages, [])

    async def test_schedule_add_keeps_once_date_and_reason(self):
        """她排一件只做一次的事：日期与「为什么」都要落进配置。"""

        ok, note = await self.engine.schedule_add(
            {
                "time": "15:00",
                "once": True,
                "date": "2026-09-15",
                "note": "主人说下午可能下雨",
                "action_chain": [{"type": "say", "messages": ["收衣服"]}],
            }
        )
        self.assertTrue(ok, note)
        self.assertIn("只做这一次", note)
        saved = [
            item
            for item in self.store.raw_schedules()["schedules"]
            if item.get("created_by") == "bot"
        ][0]
        self.assertTrue(saved["once"])
        self.assertEqual(saved["date"], "2026-09-15")
        self.assertEqual(saved["note"], "主人说下午可能下雨")

    async def test_schedule_add_falls_back_to_daily_when_the_date_is_broken(self):
        """日期写成模型胡说八道的样子时退化成每天，而不是永远不触发。"""

        ok, _note = await self.engine.schedule_add(
            {
                "time": "15:00",
                "once": True,
                "date": "明天下午",
                "action_chain": [{"type": "say", "messages": ["收衣服"]}],
            }
        )
        self.assertTrue(ok)
        saved = [
            item
            for item in self.store.raw_schedules()["schedules"]
            if item.get("created_by") == "bot"
        ][0]
        self.assertFalse(saved["once"])
        self.assertEqual(saved["date"], "")

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
        self.assertIn("最近在聊什么", system)

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
