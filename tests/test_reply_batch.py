"""本轮待回应 batch：事件身份、唯一呈现、标准聊天记录渲染。

这里回答的是用户报的那个"我明明一条一条发的，她非说我重复说了三遍"：

- 同一个事件被几套上下文各呈现一份（监听钩子一份、LLM 钩子一份、聊天记录一份）；
- 连发合并成一条时丢了发言人身份（同群两个人说话被写成同一个人又说了一遍）。

所以这一层测的都是**实际喂给模型的那份请求原文**：假 LLM 端口把每次
``system_prompt`` / ``contexts`` / ``prompt`` 原样记下来，断言查的是"某个标记
在这份请求里出现了几次、先后顺序如何"，而不是某个中间函数的返回值。
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import ConfigStore  # noqa: E402
from core.db import AsyncDatabase  # noqa: E402
from core.engine import MessageContext, VirtualWorldEngine  # noqa: E402
from core.ports import LLMReply  # noqa: E402
from core.state import chat_event_key  # noqa: E402
from tests.stub_ports import (  # noqa: E402
    StubClock,
    StubMessenger,
    StubPersona,
    StubTools,
)
from tests.test_events import FakeRandom  # noqa: E402

SESSION = "aiocqhttp:GroupMessage:1001"
PRIVATE_SESSION = "aiocqhttp:PrivateMessage:2002"
SAY_REPLY = '{"actions":[{"type":"say","messages":["哦。"]}]}'


class CapturingLLM:
    """假 LLM 端口：把每次请求的入参原文留下来。

    断言要能查"整份请求里某个标记出现几次、顺序如何"，所以这里不做任何加工，
    存 ``generate`` 的 kwargs 原文，另外提供两种拼法：

    - :meth:`request_text`：``system_prompt`` + ``contexts``（JSON 化）+ ``prompt``
      拼成一份——用户看到的那一整份请求；
    - :meth:`user_prompt`：只看最后那条 user 消息（``prompt``）。
    """

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []
        self.default_reply = SAY_REPLY
        self.ok = True
        self.on_call = None

    async def generate(self, **kwargs: Any) -> LLMReply:
        self.calls.append(dict(kwargs))
        hook, self.on_call = self.on_call, None
        if hook is not None:
            result = hook(len(self.calls) - 1)
            if inspect.isawaitable(result):
                await result
        text = self.replies.pop(0) if self.replies else self.default_reply
        return LLMReply(text=text, ok=self.ok)

    def request_text(self, index: int = -1) -> str:
        call = self.calls[index]
        parts = [str(call.get("system_prompt") or "")]
        for item in call.get("contexts") or []:
            parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        parts.append(str(call.get("prompt") or ""))
        return "\n".join(parts)

    def user_prompt(self, index: int = -1) -> str:
        return str(self.calls[index].get("prompt") or "")


class ReplyBatchTest(unittest.IsolatedAsyncioTestCase):
    """事件身份贯穿「监听 → 待回队列 → batch → 一次请求」。"""

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
        # 安静期在测试里没有意义（真的会睡 5 秒），窗口判定用 MERGE_WINDOW_SECONDS
        raw["reply_style"]["merge_wait_seconds"] = 0
        self.store.save_world(raw)

        self.db = AsyncDatabase(self.store.db_path)
        self.addAsyncCleanup(self.db.close)

        self.llm = CapturingLLM()
        self.helper = CapturingLLM()
        self.creator = CapturingLLM()
        self.messenger = StubMessenger()
        self.tools = StubTools({"web_search": "搜索网页"})
        self.persona = StubPersona("你是一只又懒又爱面子的鲸鱼少女，自称本小姐。")
        self.clock = StubClock(now=1_700_000_000.0)
        self.engine = VirtualWorldEngine(
            store=self.store,
            db=self.db,
            llm=self.llm,
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
        self.engine.decider.rng = FakeRandom([1.0])
        self.engine.reload_config()

    # ---------------- 工具 ----------------

    def merge_mode(self, *, interrupt: bool = False) -> None:
        """这一档要的是"合并成一次"（打断那档由调用方显式打开）。"""

        raw = self.store.raw_world()
        raw["reply_style"]["interrupt_pending"] = interrupt
        raw["reply_style"]["merge_wait_seconds"] = 0
        self.store.save_world(raw)
        self.engine.reload_config()

    def add_group(self, *, sessions=None, main: str = "") -> None:
        """把两个会话编成同一个"她"（同组共享待回队列）。"""

        members = list(sessions or [SESSION, PRIVATE_SESSION])
        for item in members:
            if item == SESSION:
                continue
            self.store.add_session(
                item,
                session_type="private" if "Private" in item else "group",
                platform=item.split(":", 1)[0],
            )
        raw = self.store.raw_sessions()
        raw["groups"] = [
            {
                "id": "team",
                "name": "team",
                "sessions": members,
                "main_session": main or members[0],
            }
        ]
        self.store.save_sessions(raw)
        self.engine.reload_config()

    def ctx(
        self,
        text: str,
        *,
        user_id: str = "1",
        user_name: str = "小明",
        session: str = SESSION,
        message_id: str = "",
        event_id: str = "",
        addressing: str = "direct",
        raw_text: str | None = None,
        is_private: bool = False,
        **extra: Any,
    ) -> MessageContext:
        return MessageContext(
            session_id=session,
            user_id=user_id,
            user_name=user_name,
            text=text,
            raw_text=text if raw_text is None else raw_text,
            message_id=message_id,
            event_id=event_id,
            is_wake=True,
            is_mentioned=True,
            is_private=is_private,
            addressing=addressing,
            **extra,
        )

    def register(
        self,
        text: str,
        *,
        user_id: str = "1",
        user_name: str = "小明",
        session: str = SESSION,
        message_id: str = "",
        event_id: str = "",
        raw_text: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """模拟旁观钩子的登记（带事件身份）。"""

        identity: dict[str, Any] = {
            "user_id": user_id,
            "user_name": user_name,
            "raw_text": text if raw_text is None else raw_text,
        }
        if message_id:
            identity["message_id"] = message_id
        if event_id:
            identity["event_id"] = event_id
        identity.update(extra)
        return self.engine.register_incoming(session, text, identity=identity)

    def queue(self) -> list[dict[str, Any]]:
        return self.engine._pending_replies.get(self.engine.merge_scope(SESSION)) or []

    def assert_shows_once(self, text: str, markers: tuple[str, ...]) -> None:
        counts = {marker: text.count(marker) for marker in markers}
        for marker in markers:
            self.assertEqual(counts[marker], 1, f"「{marker}」只该出现一次：{counts}")

    # ---------------- 1 / 2：同一个人连发 ----------------

    async def test_burst_from_one_person_keeps_order_and_shows_each_once(self):
        """同人连发 A/B/C：顺序 A/B/C，每条待回事件在整份请求里只有一份。"""

        self.merge_mode()
        first = self.register("今天开会吗", message_id="m1")
        second = self.register("顺便问问会议室", message_id="m2")
        third = self.register("咖啡没了", message_id="m3")

        reply = await self.engine.handle_reply(self.ctx("今天开会吗", message_id="m1"))

        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(len(self.llm.calls), 1, "batch 凑齐了就不该再为合并重启一次")
        whole = self.llm.request_text()
        markers = ("今天开会吗", "顺便问问会议室", "咖啡没了")
        self.assert_shows_once(whole, markers)
        positions = [whole.find(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions), "整份请求里的顺序必须是 A/B/C")
        for record in (first, second, third):
            self.assertTrue(self.engine.was_absorbed(record), "这一轮里的三条都算回过了")

        prompt = self.llm.user_prompt()
        # 同一个人的连发并成一条记录、续行缩进两格（和背景共用同一套渲染规则）
        self.assertIn("小明(1): 今天开会吗", prompt)
        self.assertIn("\n  顺便问问会议室", prompt)
        self.assertIn("\n  咖啡没了", prompt)
        head = next(line for line in prompt.splitlines() if "今天开会吗" in line)
        self.assertTrue(head.startswith("- ["), head)
        # 续行缩进两格：拼成一行时模型会把「a / b / c」误读成"同一句说了三遍"
        for marker in ("顺便问问会议室", "咖啡没了"):
            line = next(line for line in prompt.splitlines() if marker in line)
            self.assertEqual(line, f"  {marker}", line)

    async def test_same_topic_burst_is_not_rendered_as_one_sentence_three_times(self):
        """同主题、不同内容：三条各自成条，不写成"同一句重复三遍"。"""

        self.merge_mode()
        self.register("明天几点开", message_id="t1")
        self.register("会议室订了吗", message_id="t2")
        self.register("要不要带电脑", message_id="t3")

        reply = await self.engine.handle_reply(self.ctx("明天几点开", message_id="t1"))

        self.assertTrue(reply.ok, reply.error)
        prompt = self.llm.user_prompt()
        for marker in ("明天几点开", "会议室订了吗", "要不要带电脑"):
            self.assertEqual(prompt.count(marker), 1, marker)
        self.assertNotIn("（紧接着又说：", prompt, "对话体那套话术要随改造删掉")
        self.assertNotIn("一口气说完", prompt)
        # 三句都是各自独立的记录行（第一句带时间戳，后两句缩进续行）
        self.assertEqual(
            len([line for line in prompt.splitlines() if line.startswith("- [")]),
            1,
            "同一个人的这三句应该并成一条记录（沿用聊天记录的分组规则）",
        )

    # ---------------- 3：同群两个人 ----------------

    async def test_two_people_burst_keeps_each_speaker(self):
        """两个人几乎同时说：合并成一轮，但逐条标出真实发言人。"""

        self.merge_mode()
        self.register("今天开会吗", user_id="1001", user_name="A", message_id="p1")
        # B 那句是冲别人说的：渲染器要按条给它标出「这句是冲别人说的」
        self.register(
            "我去买咖啡",
            user_id="1002",
            user_name="B",
            message_id="p2",
            addressing="others",
        )
        # C 那条例子里 @ 了她：渲染器要按条标出「→ 你：」
        self.register(
            "几点",
            user_id="1003",
            user_name="C",
            message_id="p3",
            at_targets=[{"id": "1", "name": "本小姐", "self": True}],
        )

        reply = await self.engine.handle_reply(
            self.ctx(
                "几点",
                user_id="1003",
                user_name="C",
                message_id="p3",
                at_targets=[{"id": "1", "name": "本小姐", "self": True}],
            )
        )

        self.assertTrue(reply.ok, reply.error)
        prompt = self.llm.user_prompt()
        for marker in ("A(1001): 今天开会吗", "B(1002): 我去买咖啡", "C(1003): → 你：几点"):
            self.assertIn(marker, prompt)
            line = next(line for line in prompt.splitlines() if marker in line)
            self.assertTrue(line.startswith("- ["), line)
        # B / C 的话绝不能挂到 A 名下
        self.assertNotIn("A(1001): 我去买咖啡", prompt)
        self.assertNotIn("A(1001): 几点", prompt)
        self.assertEqual(prompt.count("我去买咖啡"), 1)
        # 冲别人说的那句，渲染器按条点一句；@ 了她的那句不点
        hint = "（这句是冲别人说的，不是在问你）"
        b_line = next(line for line in prompt.splitlines() if "我去买咖啡" in line)
        c_line = next(line for line in prompt.splitlines() if "C(1003):" in line)
        self.assertIn(hint, b_line)
        self.assertNotIn(hint, c_line)
        # addressing 基调由触发者那一条决定，仍然写在最后那条 user 消息里
        self.assertIn("C 对你说：", prompt)

    # ---------------- 4：真的说了两遍 + 两个钩子的副本 ----------------

    async def test_true_duplicates_stay_and_hook_copies_collapse(self):
        """用户真的同文发两遍：两条都在；同一条被两个钩子各记一次：只有一份。"""

        self.merge_mode()
        first = self.register("么么哒", message_id="d1")
        second = self.register("么么哒", message_id="d2")
        again = self.register("么么哒", message_id="d1")

        self.assertIsNot(first, second, "同文不同事件必须是两条")
        self.assertIs(again, first, "同一条消息被两个钩子各记一次只留一条")

        reply = await self.engine.handle_reply(self.ctx("么么哒", message_id="d1"))

        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(len(self.llm.calls), 1)
        whole = self.llm.request_text()
        self.assertEqual(whole.count("么么哒"), 2, "真的说了两遍，两遍都要在")
        self.assertEqual(len(self.queue()), 0)

    async def test_note_chat_dedupes_by_event_identity_not_by_text(self):
        """留档那一层：同一条消息留一份，正文一样的另一条照留。"""

        async with self.engine.session_state(SESSION) as state:
            for _ in range(2):
                state.note_chat(
                    user_id="1",
                    name="小明",
                    text="亲亲",
                    now=100.0,
                    origin=SESSION,
                    message_id="d1",
                )
            state.note_chat(
                user_id="1",
                name="小明",
                text="亲亲",
                now=100.0,
                origin=SESSION,
                message_id="d2",
            )
            rows = [
                item
                for item in state.recent_chat
                if str(item.get("user_id") or "") == "1"
            ]
            self.assertEqual(len(rows), 2, "同一个事件一份、真的两遍两份")
            self.assertEqual(rows[0]["event_key"], chat_event_key(SESSION, "d1"))
            self.assertEqual(rows[1]["event_key"], chat_event_key(SESSION, "d2"))

    # ---------------- 5：注解改正文，改不了事件身份 ----------------

    async def test_annotated_trigger_never_reenters_the_batch(self):
        """触发消息被注解过（ctx.text ≠ 监听侧原文）也不能重新进 early。"""

        self.merge_mode()
        record = self.register("亲亲", message_id="a1")
        annotated = "亲亲［这条消息 @ 了：你（本小姐）］"
        # main.py 在接管分支里就是这样把监听侧那条补全的
        self.engine.refresh_incoming(
            record,
            annotated,
            identity={
                "message_id": "a1",
                "user_id": "1",
                "user_name": "小明",
                "raw_text": "亲亲",
            },
        )

        reply = await self.engine.handle_reply(
            self.ctx(annotated, message_id="a1", raw_text="亲亲")
        )

        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(len(self.llm.calls), 1)
        prompt = self.llm.user_prompt()
        self.assertEqual(prompt.count("亲亲"), 1, "同一句话不能既当触发消息、又当新来的")
        self.assertEqual(prompt.count("这条消息 @ 了"), 1)
        self.assertEqual(record["message_id"], "a1", "注解不能改掉事件身份")

    # ---------------- 5b：背景侧排除本轮 batch（同一个事件只在 user 消息里） ----------------

    #: 真实管线里 ``note_chat`` 只多带一个 ``at``（见 engine._record_user_message）。
    #: 其余参数两处必须一致，否则事件身份键对不上、背景那一条就删不掉。
    def listen(
        self,
        *,
        user_id: str,
        name: str,
        text: str,
        origin: str,
        now: float,
        **extra: Any,
    ):
        """旁观监听在 ``note_chat`` 上写的那份参数（与 ``_record_user_message`` 对齐）。

        ``now`` 是必填的（见 :meth:`core.state.WorldState.note_chat` 的签名）；
        ``at`` / ``images`` / ``reply_to`` / ``addressing`` 在真实管线里都可以是空的。
        """

        return {
            "user_id": user_id,
            "name": name,
            "text": text,
            "origin": origin,
            "now": now,
            "at": extra.pop("at", None),
            "images": extra.pop("images", None),
            "reply_to": extra.pop("reply_to", None),
            "addressing": extra.pop("addressing", ""),
            "message_id": extra.pop("message_id", ""),
            "event_id": extra.pop("event_id", ""),
            **extra,
        }

    async def test_background_history_drops_this_round_events_by_identity(self):
        """留档里的同一个事件要被摘掉；别的记录（没有事件身份）照旧留着。

        这一条覆盖的是"本轮 batch 只在一个位置完整呈现"那条分工：
        :meth:`engine.chat_context_for_reply` 的排除集合是**本轮 batch 的事件键**。
        """

        self.merge_mode()
        first = self.register("今天开会吗", message_id="h1")
        second = self.register("咖啡没了", message_id="h2")
        async with self.engine.session_state(SESSION) as state:
            # 老存档：没有 message_id / event_id → 没有事件身份，只能靠兜底规则
            state.note_chat(
                user_id="9",
                name="老王",
                text="昨天那事算了",
                now=1_699_999_000.0,
                origin=SESSION,
            )
            # 这两个事件同时在待回队列和留档里（真实管线里两边都会写）
            state.note_chat(
                **self.listen(
                    user_id="1",
                    name="小明",
                    text="今天开会吗",
                    origin=SESSION,
                    now=self.clock.now(),
                    message_id="h1",
                    at=[],
                )
            )
            state.note_chat(
                **self.listen(
                    user_id="1",
                    name="小明",
                    text="咖啡没了",
                    origin=SESSION,
                    now=self.clock.now(),
                    message_id="h2",
                    at=[],
                )
            )
            keys = {
                str(item.get("event_key") or "")
                for item in state.recent_chat
                if str(item.get("event_key") or "")
            }

        self.assertEqual(keys, {chat_event_key(SESSION, "h1"), chat_event_key(SESSION, "h2")})

        reply = await self.engine.handle_reply(self.ctx("今天开会吗", message_id="h1"))

        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(len(self.llm.calls), 1)
        whole = self.llm.request_text()
        # 整份请求里：本轮的这两条各只出现一次（背景那一份被摘掉了）
        self.assertEqual(whole.count("今天开会吗"), 1, "同一个事件不能在背景里再来一份")
        self.assertEqual(whole.count("咖啡没了"), 1)
        # 没有事件身份的老记录不受影响，照旧当背景
        self.assertEqual(whole.count("昨天那事算了"), 1)
        self.assertTrue(self.engine.was_absorbed(first))
        self.assertTrue(self.engine.was_absorbed(second))

    # ---------------- 6：重启合并 / 溢出 / 打断释放 ----------------

    async def test_restart_merges_into_the_same_batch(self):
        """调模型期间又来一条：合并后重新发起，且用的是同一个 batch。"""

        self.merge_mode()
        trigger = self.register("第一句", message_id="r1")
        holder: dict[str, Any] = {}
        self.llm.replies = [SAY_REPLY, SAY_REPLY]

        def hook(_index: int) -> None:
            holder["second"] = self.register("还有一句话", message_id="r2")

        self.llm.on_call = hook
        reply = await self.engine.handle_reply(self.ctx("第一句", message_id="r1"))

        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(len(self.llm.calls), 2, "合并之后要重新发起一次")
        prompt = self.llm.user_prompt()
        self.assertEqual(prompt.count("第一句"), 1)
        self.assertEqual(prompt.count("还有一句话"), 1)
        self.assertLess(
            prompt.find("第一句"), prompt.find("还有一句话"), "顺序按真实到达排"
        )
        self.assertTrue(self.engine.was_absorbed(trigger))
        self.assertTrue(self.engine.was_absorbed(holder["second"]))
        self.assertEqual(len(self.queue()), 0)

    async def test_overflow_records_stay_for_a_later_round(self):
        """batch 满了（MERGE_MAX_TEXTS）之后多出来的那些：留给后面的轮次。"""

        self.merge_mode()
        # 触发者登记得最早：待回队列只留最后 8 条，它会被挤出去 ——
        # 这一条正是"溢出项先标 absorbed 再丢"那个 bug 的现场
        self.register("第0句", message_id="o0")
        extras = [
            self.register(f"第{index}句", message_id=f"o{index}")
            for index in range(1, 9)
        ]

        reply = await self.engine.handle_reply(self.ctx("第0句", message_id="o0"))

        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(len(self.llm.calls), 1, "batch 满了就不再为合并重开一次")
        whole = self.llm.request_text()
        for index in range(0, 8):
            self.assertEqual(whole.count(f"第{index}句"), 1, f"第{index}句该在 batch 里")
        self.assertEqual(whole.count("第8句"), 0, "超出的那条不该混进这一轮")
        overflow = extras[-1]
        self.assertFalse(
            self.engine.was_absorbed(overflow), "溢出项不能先标 absorbed 再丢"
        )
        self.assertIn(overflow, self.queue(), "溢出项要留在待回队列里")
        self.assertFalse(overflow.get("picked"))

        # 留待后续处理：它自己那一轮照常回
        later = await self.engine.handle_reply(self.ctx("第8句", message_id="o8"))
        self.assertTrue(later.ok, later.error)
        self.assertEqual(self.llm.user_prompt().count("第8句"), 1)
        self.assertTrue(self.engine.was_absorbed(overflow))

    async def test_interrupt_releases_the_whole_batch(self):
        """被打断作废：batch 里每一条都恢复成"还没回过"。"""

        self.merge_mode(interrupt=True)
        trigger = self.register("第一句", message_id="i1")
        early = self.register("第二句", message_id="i2")
        holder: dict[str, Any] = {}
        self.llm.replies = [SAY_REPLY]

        def hook(_index: int) -> None:
            holder["late"] = self.register("第三句", message_id="i3")

        self.llm.on_call = hook
        reply = await self.engine.handle_reply(self.ctx("第一句", message_id="i1"))

        self.assertFalse(reply.ok)
        self.assertIn("打断", reply.error)
        self.assertEqual(len(self.llm.calls), 1, "被打断的那次不再重新发起")
        for record in (trigger, early, holder["late"]):
            self.assertFalse(
                self.engine.was_absorbed(record), "作废后这一轮里每一条都要放回去"
            )
            self.assertIn(record, self.queue())

    # ---------------- 7：群 / 私聊互为背景但不互相合并 ----------------

    async def test_a_private_message_is_not_merged_into_the_group_round(self):
        """同一个"她"的群和私聊共用待回队列，但来源约束不丢。"""

        self.merge_mode()
        self.add_group(sessions=[SESSION, PRIVATE_SESSION], main=SESSION)
        self.assertEqual(
            self.engine.merge_scope(SESSION),
            self.engine.merge_scope(PRIVATE_SESSION),
            "同组就是同一个她：待回队列是共用的一条",
        )
        group_record = self.register("群里说的", message_id="g1")
        private_record = self.register(
            "私聊说的",
            user_id="2",
            user_name="小红",
            session=PRIVATE_SESSION,
            message_id="v1",
        )

        reply = await self.engine.handle_reply(self.ctx("群里说的", message_id="g1"))

        self.assertTrue(reply.ok, reply.error)
        prompt = self.llm.user_prompt()
        self.assertEqual(prompt.count("群里说的"), 1)
        self.assertEqual(prompt.count("私聊说的"), 0, "私聊那句不该并进群里的这一轮")
        self.assertTrue(self.engine.was_absorbed(group_record))
        self.assertFalse(
            self.engine.was_absorbed(private_record), "它在私聊那一轮里照常回"
        )

    async def test_a_group_message_is_not_merged_into_the_private_round(self):
        """反方向：私聊那一轮也不该把群里的话捞进来。"""

        self.merge_mode()
        self.add_group(sessions=[SESSION, PRIVATE_SESSION], main=SESSION)
        group_record = self.register("群里说的", message_id="g2")
        private_record = self.register(
            "私聊说的",
            user_id="2",
            user_name="小红",
            session=PRIVATE_SESSION,
            message_id="v2",
        )

        reply = await self.engine.handle_reply(
            self.ctx(
                "私聊说的",
                user_id="2",
                user_name="小红",
                session=PRIVATE_SESSION,
                message_id="v2",
                is_private=True,
            )
        )

        self.assertTrue(reply.ok, reply.error)
        prompt = self.llm.user_prompt()
        self.assertEqual(prompt.count("私聊说的"), 1)
        self.assertEqual(prompt.count("群里说的"), 0, "群里那句不该并进私聊这一轮")
        self.assertTrue(self.engine.was_absorbed(private_record))
        self.assertFalse(self.engine.was_absorbed(group_record))

    # ---------------- 6：正文一样但根本不是同一个事件 ----------------

    async def test_same_text_from_another_session_is_kept(self):
        """同一个人在两个会话里说了同一句话：两边都是真事件，一条都不能吞。"""

        self.merge_mode()
        self.add_group(sessions=[SESSION, PRIVATE_SESSION])

        async with self.engine.session_state(SESSION) as state:
            state.note_chat(
                **self.listen(
                    user_id="1",
                    name="小明",
                    text="在吗",
                    origin=SESSION,
                    now=self.clock.now(),
                    message_id="s1",
                )
            )
            state.note_chat(
                **self.listen(
                    user_id="1",
                    name="小明",
                    text="在吗",
                    origin=PRIVATE_SESSION,
                    now=self.clock.now(),
                    message_id="s2",
                )
            )
            rows = [
                item
                for item in state.recent_chat
                if str(item.get("text") or "") == "在吗"
            ]
            self.assertEqual(len(rows), 2, "跨会话同文是两条：去重不能按会话之外的正文走")
            self.assertEqual(
                {row["event_key"] for row in rows},
                {chat_event_key(SESSION, "s1"), chat_event_key(PRIVATE_SESSION, "s2")},
            )

    async def test_same_text_from_two_people_in_one_group_is_kept(self):
        """同一个群里两个人说了同一句话：两条都要在，归属各是各的。"""

        self.merge_mode()
        first = self.register("哈哈哈哈", user_id="1", user_name="小明", message_id="p1")
        second = self.register("哈哈哈哈", user_id="2", user_name="小红", message_id="p2")

        self.assertIsNot(first, second, "同群同文不同人是两条")
        self.assertEqual(second["user_name"], "小红")

        reply = await self.engine.handle_reply(self.ctx("哈哈哈哈", message_id="p1"))

        self.assertTrue(reply.ok, reply.error)
        whole = self.llm.request_text()
        self.assertEqual(whole.count("哈哈哈哈"), 2, "两个人各说一遍，两遍都要在")
        self.assertIn("小红", whole, "发言人不能丢掉")

    async def test_a_fresh_message_is_not_swallowed_by_an_answered_twin(self):
        """刚回过一句一模一样的：新来的这条仍然是新的，不能被当成"已回过"。"""

        self.merge_mode()
        async with self.engine.session_state(SESSION) as state:
            # 上一轮已经回过的「在吗」（老存档：没有事件身份，只有水位线）
            state.note_chat(
                user_id="1",
                name="小明",
                text="在吗",
                now=self.clock.now() - 60.0,
                origin=SESSION,
            )
            self.engine._set_watermark(
                state, SESSION, until=self.clock.now(), seq=int(state.chat_seq)
            )

        fresh = self.register("在吗", message_id="f1")
        reply = await self.engine.handle_reply(self.ctx("在吗", message_id="f1"))

        self.assertTrue(reply.ok, reply.error)
        self.assertTrue(self.engine.was_absorbed(fresh), "新来的这条必须进这一轮")
        prompt = self.llm.user_prompt()
        self.assertEqual(prompt.count("在吗"), 1, "这一轮的那条只出现一次")

    async def test_bot_reply_is_recorded_only_after_delivery_commit(self):
        """生成成功不是送达成功：发送前不能留下虚假的 bot 发言。"""

        self.merge_mode()
        self.register("晚饭吃什么", message_id="deliver-1")
        reply = await self.engine.handle_reply(
            self.ctx("晚饭吃什么", message_id="deliver-1")
        )
        self.assertTrue(reply.ok, reply.error)

        async with self.engine.session_state(SESSION) as state:
            self.assertFalse(
                any(item.get("is_self") for item in state.recent_chat),
                "平台尚未确认前，不能把模型草稿写成已经发出的消息",
            )

        await self.engine.mark_reply_batch_replied(SESSION, messages=reply.messages)
        async with self.engine.session_state(SESSION) as state:
            sent = [item for item in state.recent_chat if item.get("is_self")]
            self.assertEqual([item.get("text") for item in sent], ["哦。"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
