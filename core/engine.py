"""虚拟世界引擎：把配置、状态、决策、动作、记忆串起来。

这是整个插件的核心 seam（见 docs/SEAMS.md），只依赖 core/ports.py 里的 Protocol，
因此可以在没有 AstrBot 的环境里完整测试。

职责边界：
- 注入模式（被 @ 时）：只产出「世界认知」文本交给主人格，不接管回复；
- 接管模式（自主行为）：自己调 LLM、自己发消息。两条路径互斥。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Callable

from .config_store import ConfigStore
from .db import AsyncDatabase
from .decider import Decider, reply_willingness
from .engagement import EngagementTracker
from .json_actions import (
    CANCEL_MODES,
    PlannedAction,
    extract_json_object,
    parse_action_payload,
    parse_plan_payload,
)
from .memory import INNER, INTERACTION, SCENE, MemoryEngine
from .models import (
    ActionDef,
    ChainStep,
    ColdStartMode,
    ECHO_EVENT_TYPES,
    NodeDef,
    SessionDef,
    WorldConfig,
    pronoun_for as pronounce_for,
)
from .nickname import compute_nickname, should_update
from .pathfinding import find_path, nearest_node, path_ticks
from .planner import active_plan, advance, create_plan, peek_step
from .prompt import WEEKDAY_NAMES, PromptBuilder, clock_text, period_of
from .timeline import render_event
from .ports import CardResult, ToolCallResult
from .state import (
    STATE_AWAKENING,
    STATE_IDLE,
    STATE_NAPPING,
    STATE_SLEEPING,
    WorldState,
)
from .state_dynamics import StateDynamics
from .tool_policy import allowed_tools, is_self_send_tool

from .generator import (
    auto_layout,
    clamp_node_count,
    clamp_per_node,
    link_plan,
    parse_generated_actions,
    parse_generated_nodes,
)

DEFAULT_TICK_SECONDS = 60.0


def _echo_payload(
    event_type: str, detail: dict[str, Any], enabled: set[str]
) -> bool:
    """这条事件要不要发到群里。

    调试回显是为了补上"群里本来看不见的东西"（决定、耗时、工具调用、被跳过的动作），
    所以她自己说过的话不再重复发一遍：可见动作的文本已经作为普通消息发出去了，
    而想事情这类静默动作（visible=False）的文本从不出现在群里，照旧回显。
    """

    if event_type not in enabled:
        return False
    if event_type == "action":
        return not (bool(detail.get("visible")) and bool(detail.get("messages")))
    return True
SELF_INITIATED_FLAG = "virtual_world_self_initiated"

# 写记忆时模型偶尔会"反过来问人名"。出现这些字样就不是记忆，改用兜底文案。
MEMORY_REFUSAL_HINTS = (
    "捏造",
    "没有出现对方",
    "没出现对方",
    "对方是谁",
    "对方的名字",
    "告诉我对方",
    "请告诉",
    "需要你告诉",
    "无法确定对方",
    "给不出",
)

# 超出 LLM 预算时使用的内置短句池（零 token，保持"她还活着"的观感）
TEXT_FALLBACKS = (
    "……有人在吗？",
    "有点安静呢。",
    "我在窗边看云，你们呢。",
    "刚刚发了会儿呆，现在回来了。",
    "要是在的话，跟我说说话嘛。",
)


@dataclass
class TickOutcome:
    """一次 tick 对某个会话产生的可见结果。"""

    session_id: str
    messages: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm_calls: int = 0
    auto_travel: list[str] = field(default_factory=list)
    """插件替她补的移动目的地（她想去别处做某事时）。"""

    debug_messages: list[str] = field(default_factory=list)
    """开启「把动作发到群里」时，这些行会作为普通消息发出去（不计入"她说了话"）。"""


@dataclass
class MessageContext:
    """处理一条用户消息需要的上下文。"""

    session_id: str
    user_id: str = ""
    user_name: str = ""
    text: str = ""
    is_wake: bool = False
    is_mentioned: bool = False
    """这条消息是不是真的 @ 了她（或私聊）。

    和 ``is_wake`` 分开是因为：意图路由这类插件会把 ``is_wake`` 置 True
    （它标记的是"这条消息要交给大模型"，不等于"有人在跟她说话"）。
    """
    is_private: bool = False
    persona_id: str = ""
    other_context: str = ""
    is_group_lively: bool = False
    mood_signal: str = ""
    image_urls: list[str] = field(default_factory=list)
    """这次要直接交给多模态主模型的图片地址（没配转述模型时才用）。"""


@dataclass
class SleepReply:
    """她在睡觉时对一条消息的处理结果。

    ``mode``：

    - ``template``：用配置的固定文案回一句（``messages`` 就是要发的内容）；
    - ``silent``：什么都不发，也不要交给主人格。

    （能把她叫醒的消息不会走到这里——那种情况返回 ``None``，交回正常回复路径。）
    """

    mode: str
    messages: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class ReplyOutcome:
    """一次「接管回复」的结果。"""

    ok: bool
    messages: list[str] = field(default_factory=list)
    reasoning: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    debug_messages: list[str] = field(default_factory=list)
    """开启「把动作发到群里」时，决定/动作/工具调用的说明行。"""
    error: str = ""


class VirtualWorldEngine:
    """世界状态机。"""

    def __init__(
        self,
        *,
        store: ConfigStore,
        db: AsyncDatabase,
        llm=None,
        helper_llm=None,
        context_llm=None,
        creator_llm=None,
        messenger=None,
        tools=None,
        commands=None,
        persona=None,
        clock=None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        decider_interval: float = 300.0,
        debug: bool = False,
        logger=None,
    ) -> None:
        self.store = store
        self.db = db
        self.llm = llm
        # 辅助模型：只负责"把意图翻译成工具参数"，留空时复用主模型
        self.helper_llm = helper_llm or llm
        # 上下文压缩模型：只负责把较早的群聊压成摘要
        self.context_llm = context_llm or self.helper_llm
        # 内容生成模型：只在编辑器里"批量生成动作 / 地点"时用，运行时不参与
        self.creator_llm = creator_llm or self.llm
        self.messenger = messenger
        self.tools = tools
        # 指令通道：把别家插件的指令转发出去（「指令触发」型动作用）
        self.commands = commands
        self.persona = persona
        self.clock = clock
        self.tick_seconds = max(1.0, float(tick_seconds))
        self.decider_interval = max(5.0, float(decider_interval))
        self.debug = debug
        self.logger = logger

        self.world: WorldConfig
        self.schedules = None
        self.sessions = None
        self.load_warnings: list[str] = []
        self._sessions_index: dict[str, SessionDef] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_decider_at: dict[str, float] = {}
        self._last_presence_at: dict[str, float] = {}
        self._event_writes: dict[str, int] = {}
        self._event_ids: dict[str, int] = {}
        """每个会话最后写入的事件 id，用来给「把动作发到群里」划一条起跑线。"""
        self._pending_echo: dict[str, list[str]] = {}
        """回复路径之外产生的调试回显，等下一次发送时带出去。"""
        self._filled_params: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_llm: dict[str, dict[str, Any]] = {}
        """每个会话最近一次主模型调用的结果（编辑器「模型通道」那行要看）。"""
        self._schedule_signature: list[tuple[Any, ...]] | None = None
        self._schedule_reset_pending = False
        self._last_tick_at: float = 0.0
        self._self_initiated = asyncio.Event()
        self._self_initiated_tasks: set[asyncio.Task] = set()

        self.reload_config()

    # ================= 配置 =================

    def reload_config(self) -> list[str]:
        """热加载配置：世界 / 日程 / 会话。"""

        world, schedules, sessions, warnings = self.store.reload()
        # 日程内容变了（改了时间/星期/动作）就把检查游标往回拨一次，
        # 否则「刚把时间改到现在」的日程会被当成已经检查过的过去。
        signature = [
            (
                item.id,
                bool(item.enabled),
                str(item.time),
                tuple(item.days or []),
                tuple(item.sessions or []),
                int(item.priority),
            )
            for item in (schedules.schedules if schedules is not None else [])
        ]
        if self._schedule_signature is not None and signature != self._schedule_signature:
            self._schedule_reset_pending = True
        self._schedule_signature = signature
        self.world = world
        self.schedules = schedules
        self.sessions = sessions
        self._sessions_index = {item.session_id: item for item in sessions.sessions}
        self.load_warnings = warnings

        self.dynamics = StateDynamics(world.state_dynamics)
        self.memory = MemoryEngine(self.db.raw, world)
        self.decider = Decider(world)
        self.engagement = EngagementTracker(world)
        self.prompts = PromptBuilder(
            world, tick_seconds=self.tick_seconds, now_provider=self.local_now
        )
        return warnings

    # ================= 会话 =================

    def is_enabled(self, session_id: str) -> bool:
        if not session_id:
            return False
        session = self._sessions_index.get(session_id)
        if session is None or not session.enabled:
            return False
        blocked = set(self.world.content_safety.session_blocklist or [])
        return session_id not in blocked

    def enabled_session_ids(self) -> list[str]:
        return [item.session_id for item in self.sessions.sessions if self.is_enabled(item.session_id)]

    def session_config(self, session_id: str) -> SessionDef | None:
        return self._sessions_index.get(session_id)

    def default_node_id(self) -> str:
        return self.world.default_node_id()

    def node(self, node_id: str) -> NodeDef | None:
        return self.world.node_map().get(node_id)

    # ================= 状态存取 =================

    def lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def load_state(self, session_id: str, *, cold_start: bool = True) -> WorldState:
        payload = await self.db.call("get_state", session_id)
        if payload is None:
            return await self._cold_start(session_id) if cold_start else self._default_state(session_id)
        return WorldState.from_payload(payload, session_id)

    def _default_state(self, session_id: str) -> WorldState:
        defaults = self.world.default_state
        session = self.session_config(session_id)
        node_id = (
            session.cold_start_node
            if session and session.cold_start_node in self.world.node_map()
            else self.default_node_id()
        )
        state = WorldState(
            session_id=session_id,
            node_id=node_id,
            mood=defaults.mood,
            energy=defaults.energy,
            loneliness=defaults.loneliness,
            curiosity=defaults.curiosity,
            affect=defaults.affect,
            boredom=defaults.boredom,
        )
        state.clamp()
        return state

    async def _cold_start(self, session_id: str) -> WorldState:
        """冷启动：生成初始状态与预设记忆。"""

        state = self._default_state(session_id)
        session = self.session_config(session_id)
        mode: ColdStartMode = session.cold_start_mode if session else "awakening"
        if mode == "awakening":
            state.state = STATE_AWAKENING
        elif mode == "silent":
            state.state = STATE_IDLE
        else:
            state.state = STATE_AWAKENING
        state.cold_start_done = False
        state.add_event("cold_start", {"mode": mode, "node": state.node_id})
        await self._seed_preset_memories(state)
        await self.save_state(state)
        return state

    async def _seed_preset_memories(self, state: WorldState) -> None:
        persona_id = ""
        for node in self.world.nodes:
            for preset in node.preset_memories:
                self.memory.remember(
                    session_id=state.session_id,
                    persona_id=persona_id,
                    node_id=node.id,
                    content=preset.content,
                    memory_type=SCENE,
                    emotion=preset.emotion,
                    weight=preset.weight,
                    scope=preset.scope,
                    source="preset",
                )

    async def save_state(self, state: WorldState) -> None:
        state.updated_at = time.time()
        state.clamp()
        await self.db.call("save_state", state.session_id, state.to_payload(), state.updated_at)

    @contextlib.asynccontextmanager
    async def session_state(self, session_id: str):
        """加锁 -> 加载 -> 交给调用方修改 -> 保存 -> 解锁。"""

        async with self.lock(session_id):
            state = await self.load_state(session_id)
            try:
                yield state
            finally:
                await self.save_state(state)

    # ================= 消息处理（注入模式） =================

    # ---------------- 睡觉时的门禁 ----------------

    def _is_asleep(self, state: WorldState) -> bool:
        """她现在算不算「睡着了」（小睡按配置决定要不要一起算）。"""

        if state.state == STATE_NAPPING:
            return bool(self.world.sleep.applies_to_nap)
        return state.state == STATE_SLEEPING

    def is_asleep(self, state: WorldState) -> bool:
        """对外暴露的「她睡着了吗」（联动方用，例如意图路由直接不判断）。"""

        return self._is_asleep(state)

    def _wake_hit(self, ctx: MessageContext) -> bool:
        """这条消息算不算「明确叫她起来」。"""

        words = [
            str(word).strip()
            for word in (self.world.sleep.wake_words or [])
            if str(word).strip()
        ]
        if not words:
            return False
        if self.world.sleep.wake_requires_mention and not (ctx.is_mentioned or ctx.is_private):
            return False  # 没 @ 她的闲聊不能把她弄醒
        text = ctx.text or ""
        return any(word in text for word in words)

    def _wake_note_text(self, ctx: MessageContext) -> str:
        """被叫醒时给提示词的一句话。

        重点是别让她"只顾着醒"：同一条消息里交代的事也要安排成动作。
        """

        note = (
            "你刚被叫醒，可以迷糊一点；但对方如果在同一条消息里交代了要做的事，"
            "先把那件事写成动作，再回一句话。"
        )
        if self._wake_leftover(ctx.text or ""):
            note += "对方不只是叫你起来，还交代了事情。"
        return note

    def _wake_leftover(self, text: str) -> str:
        """去掉唤醒词和标点之后，这条消息还剩什么（用来判断是不是还交代了事）。"""

        result = text or ""
        for word in self.world.sleep.wake_words or []:
            word = str(word).strip()
            if word:
                result = result.replace(word, "")
        cleaned = "".join(
            char for char in result if not char.isspace() and char not in "，。！？、,.!?~～…:：;；"
        )
        return cleaned

    @staticmethod
    def _take_wake_note(state: WorldState) -> str:
        """取走「刚被叫醒」这句提示（取走即失效，避免每轮都提）。"""

        if not state.wake_note or state.world_time > state.wake_note_until:
            state.wake_note = ""
            return ""
        note = state.wake_note
        state.wake_note = ""
        return note

    def _sleep_reply_text(self, state: WorldState, ctx: MessageContext) -> str:
        text = str(self.world.sleep.reply_text or "").strip()
        if not text:
            return ""
        return self.render_template(
            text,
            state,
            self.node(state.node_id),
            PlannedAction(type="say", target=ctx.user_id),
        ).strip()

    def _sleep_reply_on_cooldown(self, state: WorldState) -> bool:
        minutes = max(0, int(self.world.sleep.reply_cooldown_minutes))
        if minutes <= 0 or not state.sleep_reply_at:
            return False
        return (self._now() - float(state.sleep_reply_at)) < minutes * 60

    def _wake_up_state(self, state: WorldState, ctx: MessageContext) -> None:
        """把「被叫醒」落到状态上：停下动作、可选地清掉排队计划、给一段保护期。"""

        config = self.world.sleep
        state.current_action = None
        state.state = STATE_IDLE
        state.mood_override_until = 0
        if config.clear_plan_on_wake:
            state.current_plan = None
        grace_minutes = max(0, int(config.wake_grace_minutes))
        if grace_minutes:
            grace_ticks = max(1, int(round(grace_minutes * 60 / self.tick_seconds)))
            state.no_sleep_until = max(
                int(state.no_sleep_until), int(state.world_time) + grace_ticks
            )
        # 群名片要立刻从「睡觉中」改回来，所以顺手把冷却清零
        state.last_nickname_update_at = 0.0
        state.add_event("wake_up", {"by": ctx.user_id})
        # 「刚被叫醒」这句提示：接管模式和注入模式都会用到，写在状态里统一取
        state.wake_note = self._wake_note_text(ctx)
        state.wake_note_until = int(state.world_time) + 2

    async def should_block_sleep(self, ctx: MessageContext) -> bool:
        """睡着时要不要把这条消息整个挡下来（挡在其它插件之前）。

        默认只挡「没 @ 她」的消息：她被 @ 了、消息是 ``/指令``、或者本来就是私聊，
        都放行给后面的正常流程（@ 她会拿到固定文案，或者命中唤醒词被叫醒）。
        ``sleep.block_scope = all`` 时连「@ 了她但没说唤醒词」的消息也一起挡。
        """

        config = self.world.sleep
        if not config.block_plugins or not self.is_enabled(ctx.session_id):
            return False
        if ctx.is_private:
            return False  # 私聊等同被叫，永远放行
        if self._wake_hit(ctx):
            return False  # 明确叫醒 -> 放行
        if ctx.is_mentioned and config.block_scope != "all":
            return False  # @ 了她但没说唤醒词 -> 交给 sleep_gate 回固定文案
        async with self.session_state(ctx.session_id) as state:
            if not self._is_asleep(state):
                return False
            # 挡下来之前先记一笔：她醒来还得知道群里发生过什么
            self._note_presence(state, ctx)
            # 睡着的时候群里刷屏，不能每来一条就写一条日志——日志页会被刷爆。
            # 每 10 分钟最多记一条，并带上这段时间一共挡下了多少条。
            now = self._now()
            state.sleep_skip_count = int(state.sleep_skip_count) + 1
            if state.sleep_skip_at and now - float(state.sleep_skip_at) < 600.0:
                return True
            suppressed = int(state.sleep_skip_count)
            state.sleep_skip_count = 0
            state.sleep_skip_at = now
            await self._log_event(
                state,
                "sleep_skip",
                {
                    "user": ctx.user_name or ctx.user_id or "有人",
                    "reason": "她在睡觉，这条消息被整个挡下（其它插件也不会执行）",
                    "count": suppressed,
                },
            )
            return True

    async def sleep_gate(self, ctx: MessageContext) -> SleepReply | None:
        """她在睡觉时挡在回复路径前面的那道门。

        返回 ``None`` 表示照常回复（没在睡，或者这条消息把她叫醒了）；
        否则调用方按返回的 ``mode`` 处理，并且**不要**再交给主人格。
        """

        config = self.world.sleep
        if config.reply_mode == "normal" or not self.is_enabled(ctx.session_id):
            return None
        if not self._passes_safety(ctx.text):
            return None
        async with self.session_state(ctx.session_id) as state:
            if not self._is_asleep(state):
                return None
            if self._wake_hit(ctx):
                return None  # 正常路径会负责叫醒她
            who = ctx.user_name or ctx.user_id or "有人"
            echo = TickOutcome(session_id=ctx.session_id)
            if not (ctx.is_mentioned or ctx.is_private):
                await self._log_event(
                    state,
                    "sleep_skip",
                    {"user": who, "reason": "她在睡觉，而且这条消息没 @ 她"},
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)
                return SleepReply(mode="silent", reason="没在跟她说话")
            if config.reply_mode == "silent":
                await self._log_event(
                    state,
                    "sleep_skip",
                    {"user": who, "reason": "配置成睡觉时保持安静"},
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)
                return SleepReply(mode="silent", reason="睡觉时保持安静")
            text = self._sleep_reply_text(state, ctx)
            if not text:
                return SleepReply(mode="silent", reason="固定文案是空的")
            if self._sleep_reply_on_cooldown(state):
                await self._log_event(
                    state,
                    "sleep_skip",
                    {"user": who, "reason": "刚回睡觉文案不久，先安静着"},
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)
                return SleepReply(mode="silent", reason="固定文案还在冷却")
            state.sleep_reply_at = self._now()
            # 睡觉时回的这句也是"被搭话后的回复"，同样要压一压后面的主动搭话
            self.engagement.note_passive_reply(state, tick_seconds=self.tick_seconds)
            await self._log_event(
                state, "sleep_reply", {"user": who, "text": text}, outcome=echo
            )
            self._queue_echo(ctx.session_id, echo.debug_messages)
            return SleepReply(mode="template", messages=[text], reason="她在睡觉")

    async def handle_incoming(self, ctx: MessageContext) -> str | None:
        """处理一条用户消息，返回需要注入的「世界认知」文本。

        - 不在白名单 / 内容安全拦截 -> 返回 None（完全不影响主人格的正常回复）；
        - 计数与状态更新在这里完成，但**不接管回复**。
        """

        if not self.is_enabled(ctx.session_id):
            return None
        if not self._passes_safety(ctx.text):
            return None

        async with self.session_state(ctx.session_id) as state:
            node = self.node(state.node_id)
            if node is None:
                state.node_id = self.default_node_id()
                node = self.node(state.node_id)

            extra_notes, woke = self._record_user_message(state, ctx)
            density = self.speech_density_hint(state)
            if density:
                extra_notes.append(density)
            # 刚被搭话（她马上要回一句）：接下来一段时间别再因为孤独感主动开口
            self.engagement.note_passive_reply(state, tick_seconds=self.tick_seconds)
            if woke:
                echo = TickOutcome(session_id=ctx.session_id)
                await self._log_event(
                    state,
                    "wake_up",
                    {"by": ctx.user_name or ctx.user_id},
                    persona_id=ctx.persona_id,
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)

            memories = self.memory.recall(
                session_id=state.session_id,
                persona_id=ctx.persona_id,
                node_id=state.node_id,
                focus_user=ctx.user_id,
                limit=self.world.limits.max_think_memory,
            )
            injection = self.prompts.build_injection(
                state,
                node=node,
                memories=memories,
                engagement_hint=self.engagement.hint(state),
                extra_notes=extra_notes,
                focus_user=ctx.user_name or ctx.user_id,
                recent_chat=self.chat_context(state),
            )
            if ctx.other_context:
                injection += f"\n\n# 其他插件提供的上下文\n{ctx.other_context}"

            # 把这次对话放进"待总结"缓冲：一段聊完或她离开这个地点时，
            # 才让模型把它压成一条记忆（逐条存「谁说了什么」没有信息量）。
            self.note_dialogue(
                state,
                user_id=ctx.user_id,
                name=ctx.user_name or ctx.user_id,
                text=ctx.text,
                persona_id=ctx.persona_id,
            )
            await self._log_event(
                state,
                "user_message",
                {
                    "user": ctx.user_name or ctx.user_id,
                    "wake": ctx.is_wake,
                    "len": len(ctx.text or ""),
                    "text": (ctx.text or "")[:120],
                },
                persona_id=ctx.persona_id,
            )
            return injection

    # ---------------- 对话记忆：攒片段，再总结 ----------------

    def note_dialogue(
        self,
        state: WorldState,
        *,
        user_id: str = "",
        name: str = "",
        text: str = "",
        is_self: bool = False,
        persona_id: str = "",
    ) -> None:
        """把一条「她参与的对话」放进待总结缓冲（不立刻写记忆）。"""

        clean = " ".join(str(text or "").split())
        if not clean:
            return
        if not state.pending_memory:
            state.pending_memory_node = state.node_id
        state.pending_memory.append(
            {
                "user_id": "" if is_self else str(user_id or ""),
                "name": "我" if is_self else str(name or user_id or "有人"),
                "text": _clip_text(clean, 160),
                "is_self": bool(is_self),
                "persona_id": str(persona_id or ""),
                "at": self._now(),
                "world_time": int(state.world_time),
            }
        )
        limit = 60
        if len(state.pending_memory) > limit:
            state.pending_memory = state.pending_memory[-limit:]

    def note_memory_hint(self, state: WorldState, hint: str) -> None:
        """大模型顺手写的一句话总结，作为写记忆时的提示（不会单独成为一条记忆）。"""

        text = " ".join(str(hint or "").split())
        if not text:
            return
        state.memory_hints.append(_clip_text(text, 80))
        if len(state.memory_hints) > 5:
            state.memory_hints = state.memory_hints[-5:]

    def _memory_flush_reason(self, state: WorldState) -> str:
        """这段对话该总结了吗？返回触发原因，空字符串表示再攒攒。"""

        config = self.world.memory
        if not config.dialogue_summary or not state.pending_memory:
            return ""
        if state.memory_flush_wanted:
            return "她换地方了"
        if len(state.pending_memory) >= max(1, int(config.summary_trigger_messages)):
            return "这段对话够长了"
        idle_minutes = max(0, int(config.summary_idle_minutes))
        if idle_minutes:
            last_at = float(state.pending_memory[-1].get("at") or 0.0)
            if last_at and (self._now() - last_at) >= idle_minutes * 60:
                return "这段聊完了"
        return ""

    @staticmethod
    def _fallback_memory_text(entries: list[dict[str, Any]]) -> str:
        """模型不可用时的兜底：把这段对话压成一行，而不是什么都不记。"""

        recent = entries[:6]
        if recent and all(item.get("is_self") for item in recent):
            # 只有她自己：直接留下她自己说的话，别套「我说」那层壳
            own = [
                _clip_text(str(item.get("text") or ""), 50)
                for item in recent
                if str(item.get("text") or "").strip()
            ]
            return _clip_text("；".join(part for part in own if part), 120)
        parts: list[str] = []
        for item in recent:
            text = _clip_text(str(item.get("text") or ""), 40)
            if not text:
                continue
            who = "我" if item.get("is_self") else str(item.get("name") or "有人")
            parts.append(f"{who}说「{text}」")
        if not parts:
            return ""
        return _clip_text("；".join(parts), 120)

    async def flush_pending_memory(self, session_id: str) -> TickOutcome | None:
        """把攒着的对话片段总结成一条记忆（攒够 / 聊完 / 换地点三种触发）。"""

        config = self.world.memory
        if not config.dialogue_summary or not self.is_enabled(session_id):
            return None
        async with self.session_state(session_id) as state:
            reason = self._memory_flush_reason(state)
            if not reason:
                return None
            entries = [dict(item) for item in state.pending_memory]
            hints = list(state.memory_hints)
            node_id = state.pending_memory_node or state.node_id
            persona_id = next(
                (
                    str(item.get("persona_id") or "")
                    for item in reversed(entries)
                    if item.get("persona_id")
                ),
                "",
            )
            mood = state.mood
            affect = float(state.affect)
            state.pending_memory = []
            state.memory_hints = []
            state.pending_memory_node = ""
            state.memory_flush_wanted = False
            state.memory_flush_at = self._now()

        outcome = TickOutcome(session_id=session_id)
        outcome.notes.append(f"总结这段对话（{reason}）")
        summary = ""
        if self.llm is not None:
            where = self.node(node_id)
            system_prompt, prompt = self.prompts.build_memory_summary_prompt(
                node_name=(where.name if where else "") or "",
                lines=[
                    f"{item.get('name') or '有人'}: {item.get('text')}" for item in entries
                ],
                hints=hints,
                max_chars=max(10, int(config.summary_max_chars)),
            )
            reply = await self._ask_llm(session_id, system_prompt, prompt)
            outcome.llm_calls += 1
            summary = self._clean_memory_summary(reply)
        if not summary:
            summary = self._fallback_memory_text(entries)
        if not summary:
            return outcome

        users = [
            str(item.get("user_id"))
            for item in entries
            if item.get("user_id")
        ]
        users = list(dict.fromkeys(users))
        async with self.session_state(session_id) as state:
            echo_marker = await self._event_marker(state)
            self.memory.remember(
                session_id=session_id,
                persona_id=persona_id,
                node_id=node_id,
                content=summary,
                memory_type=INTERACTION,
                related_users=users,
                emotion=mood,
                weight=0.5,
                source="llm" if summary else "runtime",
                affect=affect,
            )
            await self._log_event(
                state,
                "memory",
                {"content": summary, "reason": reason, "where": node_id},
            )
            await self._echo_events_since(state, outcome, echo_marker)
        return outcome

    @staticmethod
    def _clean_memory_summary(reply: str | None) -> str:
        """模型有时候会带引号、前缀或者多写几行，这里只取第一句有用的。"""

        text = " ".join(str(reply or "").split())
        if not text:
            return ""
        if any(hint in text for hint in MEMORY_REFUSAL_HINTS):
            # 她一个人做事的时候，模型偶尔会反过来"要人名"。
            # 这种话不是记忆，丢掉，让调用方用兜底文案重写一条。
            return ""
        for quote in ("「", "『", '"', "'"):
            if text.startswith(quote) and text.endswith(quote) and len(text) > 2:
                text = text[1:-1]
        text = text.strip("。；;，,")
        return _clip_text(text, 80)

    def _passes_safety(self, text: str) -> bool:
        blocked = self.world.content_safety.blocked_words or []
        if not blocked or not text:
            return True
        return not any(word and word in text for word in blocked)

    # ---------------- 这句话是不是「对她说的」 ----------------

    def bot_names(self, state: WorldState) -> list[str]:
        """她的各种叫法：全局设置里的 Bot 名称 + 当前 / 原始群名片。"""

        names = [
            str(self.world.bot_name or "").strip(),
            str(state.bot_current_nickname or "").strip(),
            str(state.bot_base_nickname or "").strip(),
        ]
        return [name for name in dict.fromkeys(names) if name]

    def reply_addressing(self, state: WorldState, ctx: MessageContext) -> str:
        """这次回复是「对她说」还是「群里在聊、她去插一句」。

        只有 @ 了她、私聊、叫了她的名字，或者紧接着她自己那句话往下说，
        才算对她说；其余一律按插话处理——否则群里随便一句话都会被她当成
        "有人在指使我"，答非所问还会乱做动作。
        """

        if ctx.is_private or ctx.is_mentioned:
            return "direct"
        text = str(ctx.text or "")
        for name in self.bot_names(state):
            if name and name in text:
                return "direct"
        # 上下文里最后一条通常就是刚记下的这条消息（先记后回），
        # 把它去掉，看前一条是谁说的：她刚说完话就有人接上，多半还在同一个话题里。
        context = self.chat_context(state)
        prior = list(context)
        if prior and not prior[-1].get("is_self"):
            prior = prior[:-1]
        if prior and prior[-1].get("is_self"):
            return "direct"
        return "interject"

    def speech_density_hint(self, state: WorldState) -> str:
        """「最近话太密」的提示：把事实摆给模型，让她这轮少说多做。"""

        style = getattr(self.world, "reply_style", None)
        if style is None:
            return ""
        window_minutes = max(1, int(getattr(style, "dense_window_minutes", 10) or 10))
        limit = max(1, int(getattr(style, "dense_max_lines", 4) or 4))
        since = self._now() - window_minutes * 60
        mine = [
            item
            for item in state.recent_chat
            if item.get("is_self") and float(item.get("at") or 0) >= since
        ]
        if len(mine) < limit:
            return ""
        return (
            f"# 说话密度提醒\n"
            f"最近 {window_minutes} 分钟里你已经说了 {len(mine)} 句，密度偏高。"
            "这一轮尽量少说话：能只做动作（发呆、走动、戳一下、想事情…）就别开口；"
            "真有必要回一句时，短一点，或者干脆安静地做自己的事。"
        )

    # ================= 接管回复（被 @ 时由本插件回复） =================

    async def handle_reply(
        self,
        ctx: MessageContext,
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> ReplyOutcome:
        """接管一次回复：自己调大模型 → 解析 reasoning/actions → 执行 → 返回要发的话。

        ``ok=False`` 表示本插件没能完成这次回复（没有模型、调用失败、模型没给出任何动作），
        调用方应当回落到「注入模式」，让主人格照常回复，保证用户不会收不到回应。

        注意：调用方应当先调用 ``handle_incoming()``（记录存在感/数值/互动记忆），
        本方法只负责"读状态 → 生成回复 → 执行动作"。

        状态记录、prompt 组装在锁内完成；大模型调用放在锁外，避免拖住世界时钟。
        """

        if not self.is_enabled(ctx.session_id):
            return ReplyOutcome(ok=False, error="会话未启用")
        if not self._passes_safety(ctx.text):
            return ReplyOutcome(ok=False, error="命中内容安全屏蔽词")

        # --- 第一阶段：记录消息影响 + 组装提示词（持锁，很快） ---
        async with self.session_state(ctx.session_id) as state:
            node = self.node(state.node_id) or self.node(self.default_node_id())
            # 「刚被叫醒」之类的临时提示：接管模式也要带上，否则她会以完全清醒的状态回话
            wake_note = self._take_wake_note(state)
            extra_notes = [wake_note] if wake_note else []
            density = self.speech_density_hint(state)
            if density:
                extra_notes.append(density)
            extra_notes = extra_notes or None
            memories = self.memory.recall(
                session_id=state.session_id,
                persona_id=ctx.persona_id,
                node_id=state.node_id,
                focus_user=ctx.user_id,
                limit=self.world.limits.max_think_memory,
            )
            persona_text = await self._persona_text(state.session_id)
            system_prompt = self.prompts.build_autonomous_system_prompt(
                persona_text=persona_text,
                state=state,
                node=node,
                available_tools=self.available_tools(),
                memories=memories,
                engagement_hint=self.engagement.hint(state),
                recent_chat=self.chat_context(state),
                other_context=ctx.other_context,
                extra_notes=extra_notes,
                max_messages=self.world.limits.max_messages_per_say,
                reasoning=bool(self.world.reasoning_enabled),
            )
            user_prompt = self.prompts.build_reply_user_prompt(
                user_name=ctx.user_name,
                text=ctx.text,
                is_private=ctx.is_private,
                addressing=self.reply_addressing(state, ctx),
            )
            node_id = state.node_id

        # --- 第二阶段：调用大模型（不持锁） ---
        raw = await self._ask_llm(
            ctx.session_id,
            system_prompt,
            user_prompt,
            contexts=history,
            image_urls=list(ctx.image_urls) or None,
        )
        if raw is None:
            return ReplyOutcome(ok=False, error="大模型调用失败")

        parsed = parse_action_payload(
            raw,
            available_actions=self._parseable_action_ids(node_id),
            valid_nodes=set(self.world.node_map()),
            max_actions=self.world.limits.max_actions_per_message,
            max_messages=self.world.limits.max_messages_per_say,
        )
        # 空内容的 say 直接丢掉：模型没想好要说什么时不该发一条空气泡
        actions = [
            item
            for item in parsed.actions
            if not (item.type == "say" and not item.messages)
        ]
        if not actions:
            return ReplyOutcome(
                ok=False,
                reasoning=parsed.reasoning,
                warnings=parsed.warnings,
                error="模型没有给出任何动作",
            )

        # --- 第三阶段：执行动作（重新持锁） ---
        outcome = TickOutcome(session_id=ctx.session_id)
        async with self.session_state(ctx.session_id) as state:
            echo_marker = await self._event_marker(state)
            node = self.node(state.node_id) or self.node(self.default_node_id())
            state.note_reasoning(parsed.reasoning, source="reply")
            # 对方明确要求停下时，先把她的动作/安排停掉，再执行这一轮的动作
            await self.apply_cancel(state, parsed.cancel, ctx.text, outcome)
            await self._execute_actions(
                state, node, outcome, actions, depth=0, autonomous=False
            )
            # 她这次说出去的话也要进聊天上下文：下一轮提示词里才有「你最近说过的话」，
            # 模型才知道自己刚用了什么说法，才能被要求换一种。
            for message in outcome.messages:
                state.note_chat(
                    user_id="__self__",
                    name=state.bot_current_nickname or state.bot_base_nickname or "你",
                    text=message,
                    now=self._now(),
                    keep=self.chat_history_limit(),
                    is_self=True,
                )
                self.note_dialogue(state, text=message, is_self=True)
            if outcome.messages:
                # 她真的回了：把已回应水位线推上去，并记下"刚才在聊什么"
                self.mark_chat_replied(state)
            self.note_chat_note(state, parsed.chat_note)
            await self._echo_events_since(state, outcome, echo_marker)
            # 模型顺手给的总结只当"写记忆时的提示"，不单独落成一条记忆
            self.note_memory_hint(state, parsed.memory)
            await self._log_event(
                state,
                "reply",
                {
                    "user": ctx.user_name or ctx.user_id,
                    "wake": ctx.is_wake,
                    "messages": outcome.messages,
                    "reasoning": parsed.reasoning,
                    "actions": [item.type for item in actions],
                    # 模型原话 + 解析时丢掉的东西：以后排查"她为什么没做这件事"不用再猜
                    "raw": _clip_text(parsed.raw_text, 500),
                    "warnings": list(parsed.warnings),
                    "auto_travel": list(outcome.auto_travel),
                },
                persona_id=ctx.persona_id,
            )

        # 没有对外发言（例如她只 think / 只换了个地方）就交回主人格，
        # 保证用户不会因为"她今天不想说话"而收不到任何回应。
        return ReplyOutcome(
            ok=bool(outcome.messages),
            messages=list(outcome.messages),
            reasoning=parsed.reasoning,
            warnings=parsed.warnings,
            # 前面的门禁（被叫醒等）攒下的回显也一起带出去
            debug_messages=self.take_pending_echo(ctx.session_id)
            + list(outcome.debug_messages),
            error="" if outcome.messages else "模型没有产生对外发言",
        )

    def _record_user_message(
        self, state: WorldState, ctx: MessageContext
    ) -> tuple[list[str], bool]:
        """记录一条用户消息带来的全部影响，返回要给提示词的额外说明。

        注入模式和接管模式共用这一段，保证两条路径的状态演化完全一致。
        第二个返回值表示这次是不是把她叫醒了（调用方负责写一条事件日志）。
        """

        self._note_images(state, ctx.image_urls)
        state.touch_user(
            ctx.user_id or "unknown",
            name=ctx.user_name,
            anchor="near:bot" if ctx.is_wake else "topic_center",
            max_tracked=self.world.limits.max_active_users_tracked,
        )
        state.note_chat(
            user_id=ctx.user_id or "unknown",
            name=ctx.user_name,
            text=ctx.text,
            now=self._now(),
            keep=self.chat_history_limit(),
        )
        state.last_user_activity_at = self._now()
        self.engagement.on_user_replied(state)
        self.dynamics.apply_event(state, "topic_engaged")
        if ctx.is_wake:
            self.dynamics.apply_event(state, "mention_bot")
        if ctx.mood_signal == "positive":
            self.dynamics.apply_event(state, "positive_words")
        elif ctx.mood_signal == "negative":
            self.dynamics.apply_event(state, "negative_words")
        elif ctx.mood_signal == "hug":
            self.dynamics.apply_event(state, "hug_bot")
        if ctx.is_group_lively:
            self.dynamics.apply_event(state, "group_lively")

        # 醒来：冷启动之后第一次有消息，从"刚醒"转成空闲
        if state.state == STATE_AWAKENING and state.world_time > 0:
            state.state = STATE_IDLE

        notes: list[str] = []
        woke = False
        # 睡眠中被明确叫醒 -> 打断睡眠
        if self._is_asleep(state) and self._wake_hit(ctx):
            self._wake_up_state(state, ctx)
            if state.wake_note:
                notes.append(state.wake_note)
            woke = True
        if not state.cold_start_done:
            notes.append(
                "你刚从沉睡中苏醒，还有点迷糊。说话可以简短、带点困意。"
                "这是你第一次在这个会话里醒来。"
            )
            state.cold_start_done = True
        return notes, woke

    async def note_presence(self, ctx: MessageContext) -> None:
        """轻量记录「谁在说话」，不触发任何 LLM 调用。

        群聊里大部分消息不会走到主人格，但 Bot 需要知道谁在场，因此单独提供这个入口。
        每条消息都会记一笔（存在感 + 最近聊天上下文），状态体量很小、写入在 SQLite WAL 下开销可控。
        """

        if not self.is_enabled(ctx.session_id):
            return
        if not ctx.user_id:
            return
        now = self._now()
        async with self.session_state(ctx.session_id) as state:
            self._note_presence(state, ctx)

    def _note_presence(self, state: WorldState, ctx: MessageContext) -> None:
        """记下「谁在说话」（调用方负责持锁）。"""

        self._note_images(state, ctx.image_urls)
        state.touch_user(
            ctx.user_id,
            name=ctx.user_name,
            anchor="near:bot" if ctx.is_wake else "topic_center",
            max_tracked=self.world.limits.max_active_users_tracked,
        )
        if self._passes_safety(ctx.text):
            state.note_chat(
                user_id=ctx.user_id,
                name=ctx.user_name,
                text=ctx.text,
                now=self._now(),
                keep=self.chat_history_limit(),
            )
        state.last_user_activity_at = self._now()
        if ctx.is_wake:
            # 有人 @她，就算一次有效互动
            self.engagement.on_user_replied(state)

    def _note_images(self, state: WorldState, urls: list[str] | None) -> None:
        """记住自上次回复以来收到的图片（只在「没配转述模型」时用得上）。"""

        if not urls:
            return
        limit = max(1, int(self.world.context.image_max))
        seen = {str(item.get("url") or "") for item in state.pending_images}
        for url in urls:
            text = str(url or "").strip()
            if not text or text in seen:
                continue
            state.pending_images.append({"url": text, "at": self._now()})
            seen.add(text)
        if len(state.pending_images) > limit:
            state.pending_images = state.pending_images[-limit:]

    async def take_pending_images(self, session_id: str) -> list[str]:
        """取走并清空「自上次回复以来收到的图片」。"""

        if not self.is_enabled(session_id):
            return []
        async with self.session_state(session_id) as state:
            urls = [
                str(item.get("url") or "")
                for item in state.pending_images
                if str(item.get("url") or "")
            ]
            state.pending_images = []
            return urls

    async def note_vision(
        self, session_id: str, *, ok: bool, images: int, detail: str
    ) -> None:
        """记一条图片相关的事件：转述成功（写了什么）、失败（为什么）、或者直接交给主模型。"""

        if not self.is_enabled(session_id):
            return
        try:
            async with self.session_state(session_id) as state:
                echo = TickOutcome(session_id=session_id)
                await self._log_event(
                    state,
                    "vision",
                    {"ok": bool(ok), "images": int(images or 0), "detail": _clip_text(detail, 200)},
                    outcome=echo,
                )
                self._queue_echo(session_id, echo.debug_messages)
        except Exception as exc:  # 记日志失败不该影响回复
            self._log("debug", f"图片事件写入失败: {exc}")

    # ================= 工具 =================

    def available_tools(self) -> dict[str, str]:
        """工具名 -> 描述（描述里会带上工具自带的参数说明，供提示词直接使用）。"""

        if self.tools is None:
            return {}
        try:
            result: dict[str, str] = {}
            for info in self.tools.list_tools():
                description = (info.description or "").strip()
                param_text = render_param_text(info.parameters or {})
                if param_text and param_text != "（这个工具不需要参数）":
                    description = f"{description}｜参数：{param_text}" if description else f"参数：{param_text}"
                result[info.name] = description
            return result
        except Exception:
            return {}

    def tool_schemas(self) -> dict[str, dict[str, Any]]:
        """工具名 -> 工具自带的参数 schema（来自 AstrBot 工具定义，不需要用户手写）。"""

        if self.tools is None:
            return {}
        try:
            return {
                info.name: dict(info.parameters or {}) for info in self.tools.list_tools()
            }
        except Exception:
            return {}

    def tool_sources(self) -> dict[str, str]:
        """工具名 -> 来源（``official`` = AstrBot 内置，``plugin`` = 插件 / MCP）。"""

        if self.tools is None:
            return {}
        try:
            return {
                info.name: (info.source or "plugin")
                for info in self.tools.list_tools()
            }
        except Exception:
            return {}

    def tool_owners(self) -> dict[str, str]:
        """工具名 -> 提供它的插件名（拿不到就空串，编辑器按空串归到「插件 · 其它」）。"""

        if self.tools is None:
            return {}
        try:
            return {
                info.name: str(getattr(info, "plugin", "") or "")
                for info in self.tools.list_tools()
            }
        except Exception:
            return {}

    def tool_param_text(self, tool_name: str) -> str:
        """把某个工具的参数渲染成一行中文说明，用于提示词与 Web 编辑器。"""

        schema = self.tool_schemas().get(tool_name) or {}
        return render_param_text(schema)

    def missing_tool_params(
        self,
        action_id: str,
        params: dict[str, Any],
        *,
        node_id: str = "",
        tool_name: str = "",
    ) -> list[str]:
        """检查工具必填参数是否缺失（参数定义来自工具自身 schema）。"""

        chosen = self.resolve_tool(action_id, node_id, tool_name)
        if not chosen:
            return []
        return self.missing_params_for(chosen, params)

    def missing_params_for(self, chosen: str, params: dict[str, Any]) -> list[str]:
        """某个已确定的工具还缺哪些必填参数。"""

        schema = normalize_param_schema(self.tool_schemas().get(chosen) or {})
        required = [str(name) for name in (schema.get("required") or [])]
        given = {str(key) for key, value in (params or {}).items() if value not in ("", None)}
        return [name for name in required if name not in given]

    def declared_params(self, chosen: str) -> list[str]:
        """工具自己声明了哪些参数（不分必填可选）。"""

        schema = normalize_param_schema(self.tool_schemas().get(chosen) or {})
        return [str(name) for name in (schema.get("properties") or {})]

    def unfilled_params(self, chosen: str, params: dict[str, Any]) -> list[str]:
        """声明了但这次还没给的参数。

        工具作者经常把参数写成"可选"，实现里却必须要（例如天气工具的 city），
        所以只要工具声明了参数、而这次一个都没给全，就值得让辅助模型试一次。
        """

        given = {str(key) for key, value in (params or {}).items() if value not in ("", None)}
        return [name for name in self.declared_params(chosen) if name not in given]

    def allowed_tool_names(self, node_id: str) -> set[str]:
        return allowed_tools(self.world, self.node(node_id))

    # ================= 时钟 / 日程 =================

    def _now(self) -> float:
        if self.clock is not None:
            return float(self.clock.now())
        return time.time()

    def _resolve_tz(self):
        name = (self.world.timezone or "").strip()
        if not name:
            return None
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            return None

    def local_now(self) -> datetime:
        if self.clock is not None and hasattr(self.clock, "now_struct"):
            struct = self.clock.now_struct()
            if isinstance(struct, datetime):
                return struct
        tz = self._resolve_tz()
        if tz is None:
            return datetime.now()
        try:
            return datetime.now(tz)
        except Exception:
            return datetime.now()

    # 日程检查游标最多往前回看多久（现实秒）。用于重启 / 刚启用时不要把很久以前
    # 的日程翻出来补跑。
    SCHEDULE_LOOKBACK_SECONDS = 90
    SCHEDULE_MAX_GAP_SECONDS = 600

    max_followup_chars = 800
    """动作结果交给大模型续说前的截断长度（base64 图片地址不能整条塞进提示词）。"""

    def _clip_followup(self, text: Any) -> str:
        """续说前把结果截断：超长内容（例如 base64 图片）不能整条进提示词。"""

        body = str(text or "").strip()
        if len(body) <= self.max_followup_chars:
            return body
        return body[: self.max_followup_chars] + "…（内容过长已截断）"

    def _schedule_moment(self, now: datetime, schedule: Any) -> datetime | None:
        """这条日程「今天的触发时刻」；时间写得不对就返回 None。"""

        try:
            hour, minute = (int(part) for part in str(schedule.time).split(":")[:2])
        except (TypeError, ValueError):
            return None
        try:
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None

    def _schedule_cursor(self, state: WorldState, now: datetime) -> float:
        """这个会话上次检查到哪一刻（现实时间戳）。"""

        seconds = now.timestamp()
        cursor = float(getattr(state, "schedule_cursor", 0.0) or 0.0)
        if cursor <= 0 or cursor > seconds:
            return seconds - self.SCHEDULE_LOOKBACK_SECONDS
        if seconds - cursor > self.SCHEDULE_MAX_GAP_SECONDS:
            # 停了很久（重启、插件没跑）就别把这段时间里的日程全补一遍
            return seconds - self.SCHEDULE_LOOKBACK_SECONDS
        return cursor

    def _schedule_reason(self, state: WorldState, conditions: Any) -> str:
        """日程到点却没跑的原因（写进日志，省得用户猜）。"""

        if conditions is None:
            return ""
        if conditions.not_state and state.state in conditions.not_state:
            return f"她现在是「{state.state}」状态"
        if conditions.state and state.state not in conditions.state:
            return f"她现在是「{state.state}」状态，不在允许的列表里"
        if conditions.min_energy is not None and state.energy < conditions.min_energy:
            return f"精力 {state.energy:.2f} 低于要求的 {conditions.min_energy}"
        if conditions.max_energy is not None and state.energy > conditions.max_energy:
            return f"精力 {state.energy:.2f} 高于允许的 {conditions.max_energy}"
        if conditions.min_loneliness is not None and state.loneliness < conditions.min_loneliness:
            return f"孤独感 {state.loneliness:.2f} 低于要求的 {conditions.min_loneliness}"
        if conditions.node_in and state.node_id not in conditions.node_in:
            return f"她不在指定地点（现在在 {state.node_id}）"
        return "条件不满足"

    def node_label(self, node_id: Any) -> str:
        """地点的中文名（拿不到就原样返回 id）。"""

        key = str(node_id or "")
        node = self.node(key)
        return (node.name or node.id) if node is not None else key

    def _chain_labels(self, chain: list[Any] | None) -> list[str]:
        """动作链里每一步的中文名（日程日志用）。"""

        action_map = self.world.action_map()
        labels: list[str] = []
        for step in chain or []:
            action_id = str(getattr(step, "type", "") or "")
            definition = action_map.get(action_id)
            labels.append(
                (definition.name or definition.id) if definition else action_id
            )
        return [label for label in labels if label]

    def fallback_intent(self, definition: ActionDef, step: Any = None) -> str:
        """日程里的工具 / 指令步骤没写意图时的兜底说明。

        动作链本来就没有"想干什么"这一栏，缺了它工具型动作会直接跳过
        （"没有给出想做什么"）。这里用动作自己的说明拼一句，让它至少能跑起来；
        用户可以在日程里写清意图把它顶掉。
        """

        label = definition.name or definition.id
        parts = [f"做「{label}」这件事"]
        description = str(definition.description or "").strip()
        if description:
            parts.append(description)
        node_id = str(getattr(step, "target_node", "") or "")
        if node_id:
            parts.append(f"地点是{self.node_label(node_id)}")
        return "：".join([parts[0], "；".join(parts[1:])]) if len(parts) > 1 else parts[0]

    async def fill_schedule_intents(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """保存日程时，给缺意图的工具 / 指令步骤补一句「想干什么」。

      用内容生成模型（`creator_provider_id`，没配就复用主模型）写，写进日程 JSON 里，
        编辑器里能看到也能改。这样运行时不用再花钱、也不会因为缺意图被跳过。
        返回 ``(处理后的配置, 说明列表)``；模型不可用时原样返回。
        """

        if self.creator_llm is None or not isinstance(payload, dict):
            return payload, []
        action_map = self.world.action_map()
        filled: list[str] = []
        for schedule in payload.get("schedules") or []:
            if not isinstance(schedule, dict) or schedule.get("smart"):
                # 智能日程到点自己排计划，不需要每步的意图
                continue
            chain = schedule.get("action_chain") or []
            for index, step in enumerate(chain, start=1):
                if not isinstance(step, dict):
                    continue
                definition = action_map.get(str(step.get("type") or ""))
                if definition is None or definition.llm_level not in ("tool", "command"):
                    continue
                if str(step.get("intent") or "").strip():
                    continue
                intent = await self._generate_intent(definition, schedule)
                if not intent:
                    continue
                step["intent"] = intent
                label = definition.name or definition.id
                filled.append(f"{schedule.get('id') or '日程'} 第 {index} 步「{label}」：{intent}")
        return payload, filled

    async def _generate_intent(self, definition: ActionDef, schedule: Any) -> str:
        """让内容生成模型给一步工具 / 指令动作写一句意图。"""

        label = definition.name or definition.id
        schedule_id = str(schedule.get("id") or "") if isinstance(schedule, dict) else ""
        detail = [f"日程 id：{schedule_id or '（未命名）'}"]
        detail.append(f"这一步的动作：{label}（{definition.id}）")
        detail.append(f"动作说明：{definition.description or '（没写）'}")
        if definition.llm_level == "command":
            detail.append(f"要触发的指令：{definition.trigger_command or '（没填）'}")
        else:
            tools = "、".join(definition.tool_list())
            detail.append(f"要调用的工具：{tools or '（没选）'}")
        system_prompt = (
            "你在帮一个角色把日程里的一步说清楚：她到点要让某个工具或指令替她做什么。"
            "只输出一句中文，不要解释、不要引号、不要 Markdown。"
        )
        prompt = (
            "\n".join(detail)
            + "\n\n按这个动作的用途写一句「这一步想干什么」，"
            "它会交给模型去填工具 / 指令的参数（例如「看看今天有什么科技新闻」）。"
            "只说这一句，别写参数名。"
        )
        reply = await self._ask_creator(self._generator_session_id(), system_prompt, prompt)
        return " ".join(str(reply or "").split())[:80]

    def _steps_needing_intent(self, chain: list[Any] | None) -> list[tuple[int, ActionDef]]:
        """动作链里哪几步需要意图（工具型 / 指令型，且还没写）。1-based 下标。"""

        action_map = self.world.action_map()
        wanted: list[tuple[int, ActionDef]] = []
        for index, step in enumerate(chain or [], start=1):
            definition = action_map.get(str(getattr(step, "type", "") or ""))
            if definition is None or definition.llm_level not in ("tool", "command"):
                continue
            if str(getattr(step, "intent", "") or "").strip():
                continue
            wanted.append((index, definition))
        return wanted

    async def _smart_chain(
        self, state: WorldState, schedule: Any
    ) -> tuple[list[Any], str]:
        """智能日程：把动作链交给大模型，让它给几步补「想干什么」。

        **动作链本身一个字都不改**（步数、动作 id、时长、台词都照旧），模型只填意图——
        这样就不会出现"配的是查热搜新闻、它却去上网搜索"这种事，也不用它操心怎么走路
        （自动前往由插件补）。
        返回 ``(处理后的动作链, 说明文字)``。
        """

        chain = list(schedule.action_chain or [])
        wanted = self._steps_needing_intent(chain)
        if self.llm is None or not wanted:
            return chain, ""
        now = self.local_now()
        steps = [
            {
                "index": index,
                "label": definition.name or definition.id,
                "action_id": definition.id,
                "kind": "指令型" if definition.llm_level == "command" else "工具型",
                "description": str(definition.description or "").strip(),
            }
            for index, definition in wanted
        ]
        system_prompt, prompt = self.prompts.build_schedule_intent_prompt(
            schedule_id=str(schedule.id),
            when=f"{now.strftime('%Y-%m-%d %H:%M')}（{period_of(now.hour)}）",
            where=self.node_label(state.node_id),
            state_hint=state.mood or "",
            chat_note=str(getattr(state, "chat_note", "") or ""),
            persona=_clip_text(await self._persona_text(state.session_id), 800),
            steps=steps,
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        self._count_llm_plan(state)
        if reply is None:
            return chain, ""
        intents = self._parse_intents(reply, {index for index, _ in wanted}, len(chain))
        if not intents:
            return chain, ""
        filled: list[Any] = []
        for index, step in enumerate(chain, start=1):
            intent = intents.get(index)
            if intent:
                filled.append(step.model_copy(update={"intent": intent}))
            else:
                filled.append(step)
        action_map = self.world.action_map()
        parts: list[str] = []
        for index in sorted(intents):
            action_id = str(getattr(chain[index - 1], "type", "") or "")
            definition = action_map.get(action_id)
            label = (definition.name or definition.id) if definition else action_id
            parts.append(f"第 {index} 步「{label}」：{intents[index]}")
        return filled, "；".join(parts)

    @staticmethod
    def _parse_intents(reply: str, allowed: set[int], total: int) -> dict[int, str]:
        """从模型返回里挑出「第几步 → 意图」，写得不对的一律丢掉。"""

        payload = extract_json_object(reply)
        if not isinstance(payload, dict):
            return {}
        raw = payload.get("intents")
        pairs: list[tuple[Any, Any]] = []
        if isinstance(raw, dict):
            pairs = list(raw.items())
        elif isinstance(raw, list):
            for position, item in enumerate(raw, start=1):
                if isinstance(item, dict):
                    pairs.append(
                        (item.get("step", item.get("index", position)), item.get("intent"))
                    )
                else:
                    pairs.append((position, item))
        result: dict[int, str] = {}
        for key, value in pairs:
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            text = " ".join(str(value or "").split())
            if not text or index not in allowed or not (1 <= index <= total):
                continue
            result[index] = text[:80]
        return result

    async def run_schedules(self) -> list[TickOutcome]:
        """检查并触发到点的日程。

        判定不是「当前这一分钟刚好等于配置时间」，而是「游标之后、现在之前」
        这段时间里到点的都算——tick 被拖慢、整分钟被跳过时也能补上。
        """

        outcomes: list[TickOutcome] = []
        if self.schedules is None:
            return outcomes
        now = self.local_now()
        seconds = now.timestamp()
        day_key = now.strftime("%Y-%m-%d")
        weekday = self.WEEKDAY_KEYS[now.weekday()]
        candidates = [
            schedule
            for schedule in self.schedules.schedules
            if schedule.enabled and (not schedule.days or weekday in schedule.days)
        ]

        for session_id in self.enabled_session_ids():
            async with self.session_state(session_id) as state:
                if self._schedule_reset_pending:
                    cursor = seconds - self.SCHEDULE_LOOKBACK_SECONDS
                else:
                    cursor = self._schedule_cursor(state, now)
                state.schedule_cursor = seconds
                if not candidates:
                    continue
                due: list[tuple[Any, str]] = []
                for schedule in candidates:
                    moment = self._schedule_moment(now, schedule)
                    if moment is None:
                        continue
                    stamp = moment.timestamp()
                    if cursor < stamp <= seconds:
                        due.append((schedule, moment.strftime("%H:%M")))
                for schedule, slot in sorted(
                    due, key=lambda item: item[0].priority, reverse=True
                ):
                    if schedule.sessions and session_id not in schedule.sessions:
                        continue
                    label = schedule.id
                    if schedule.action_chain:
                        names = [
                            (self.world.action_map()[step.type].name or step.type)
                            if step.type in self.world.action_map()
                            else step.type
                            for step in schedule.action_chain
                        ]
                        if names:
                            label = f"{schedule.id}（{' → '.join(names)}）"
                    if not self._conditions_ok(state, schedule.conditions):
                        reason = self._schedule_reason(state, schedule.conditions) or "条件不满足"
                        await self._log_event(
                            state,
                            "skip",
                            {
                                "action": f"schedule:{schedule.id}",
                                "note": f"日程「{label}」到点了却没跑：{reason}",
                            },
                        )
                        continue
                    echo_marker = await self._event_marker(state)
                    claimed = await self.db.call(
                        "mark_schedule_fired",
                        session_id=session_id,
                        schedule_id=schedule.id,
                        day_key=day_key,
                        slot=slot,
                    )
                    if not claimed:
                        continue
                    outcome = TickOutcome(session_id=session_id)
                    chain = schedule.action_chain
                    smart_note = ""
                    if schedule.smart:
                        # 智能日程：让大模型给缺意图的步骤补一句（动作链本身不动）
                        chain, smart_note = await self._smart_chain(state, schedule)
                    if schedule.auto_travel:
                        chain = self.expand_chain_for_travel(state, chain)
                    # 「日程开始执行」要能进日志与调试输出：不然只能看到它的动作，
                    # 不知道这一串是谁安排的。
                    await self._log_event(
                        state,
                        "schedule",
                        {
                            "id": schedule.id,
                            "time": slot,
                            "actions": " → ".join(self._chain_labels(chain)),
                            "manual": False,
                            "smart": bool(schedule.smart),
                            "intents": smart_note,
                        },
                    )
                    await self._run_chain(state, chain, outcome, depth=0)
                    outcome.notes.append(f"日程 {schedule.id} 已触发")
                    state.add_event("schedule", {"id": schedule.id})
                    await self._echo_events_since(state, outcome, echo_marker)
                    outcomes.append(outcome)
        self._schedule_reset_pending = False
        return outcomes

    async def run_schedule_now(
        self, session_id: str, schedule_id: str, *, force: bool = True
    ) -> dict[str, Any]:
        """立刻跑一遍某条日程的动作链（编辑器按钮 / `/vw schedule run` 用）。

        - 不看时间与星期，这是"手动跑一次"，**不占当天的触发名额**，
          到点之后它照常还会正常触发；
        - ``force=True``（默认）连触发条件一起忽略，方便测试；
          传 False 时条件不满足会返回原因，不会硬跑。
        """

        if not self.is_enabled(session_id):
            return {"ok": False, "reason": "这个会话不在白名单里"}
        schedule = next(
            (
                item
                for item in (self.schedules.schedules if self.schedules else [])
                if item.id == schedule_id
            ),
            None,
        )
        if schedule is None:
            return {"ok": False, "reason": f"没有找到日程「{schedule_id}」"}
        if not schedule.enabled and not force:
            return {"ok": False, "reason": f"日程「{schedule.id}」是停用状态"}

        outcome = TickOutcome(session_id=session_id)
        async with self.session_state(session_id) as state:
            if not force and not self._conditions_ok(state, schedule.conditions):
                reason = self._schedule_reason(state, schedule.conditions) or "条件不满足"
                return {
                    "ok": False,
                    "reason": f"触发条件不满足：{reason}",
                    "schedule_id": schedule.id,
                }
            echo_marker = await self._event_marker(state)
            chain = schedule.action_chain
            smart_note = ""
            if schedule.smart:
                # 智能日程手动跑也一样：让大模型给缺意图的步骤补一句
                chain, smart_note = await self._smart_chain(state, schedule)
            if schedule.auto_travel:
                chain = self.expand_chain_for_travel(state, chain)
            await self._run_chain(state, chain, outcome, depth=0)
            state.add_event("schedule", {"id": schedule.id, "manual": True})
            await self._log_event(
                state,
                "schedule",
                {
                    "id": schedule.id,
                    "time": schedule.time,
                    "actions": " → ".join(self._chain_labels(chain)),
                    "manual": True,
                    "smart": bool(schedule.smart),
                    "intents": smart_note,
                },
            )
            await self._echo_events_since(state, outcome, echo_marker)
            running = str((state.current_action or {}).get("desc") or "")

        await self._deliver(outcome)
        notes = [str(item) for item in outcome.notes if str(item).strip()]
        if notes:
            summary = "；".join(notes)
        elif outcome.messages:
            summary = "她说：" + " / ".join(outcome.messages)
        elif running:
            summary = f"她现在在做：{running}"
        else:
            summary = "动作链跑完了"
        return {
            "ok": True,
            "schedule_id": schedule.id,
            "messages": list(outcome.messages),
            "notes": notes,
            "note": _clip_text(f"已执行「{schedule.time} {schedule.id}」：{summary}", 160),
        }

    def expand_chain_for_travel(self, state: WorldState, chain: list[Any]) -> list[Any]:
        """给日程的 auto_travel 用：把"需要先在某处"的动作前面自动插一步移动。

        例：日程动作链是 [上网搜索]，开启 auto_travel 后她在卧室时会变成
        [移动到书房, 上网搜索]，这样就不用管理员手写移动步骤。
        """

        graph = self.world.adjacent()
        tracked = state.node_id
        expanded: list[Any] = []
        for step in chain or []:
            action_id = str(getattr(step, "type", "") or "")
            definition = self.world.action_map().get(action_id)
            if action_id == "walk_to":
                target = str(
                    getattr(step, "target_node", "") or getattr(step, "target", "") or ""
                )
                if target in self.world.node_map():
                    tracked = target
                expanded.append(step)
                continue
            if definition is not None:
                wanted: list[str] = []
                if definition.scope == "node":
                    wanted = [
                        node_id
                        for node_id in definition.allowed_nodes
                        if node_id in self.world.node_map()
                    ]
                if wanted and tracked not in wanted:
                    target = nearest_node(graph, tracked, wanted)
                    if target:
                        expanded.append(ChainStep(type="walk_to", target_node=target))
                        tracked = target
            expanded.append(step)
        return expanded

    def _conditions_ok(self, state: WorldState, conditions) -> bool:
        if conditions is None:
            return True
        if conditions.not_state and state.state in conditions.not_state:
            return False
        if conditions.state and state.state not in conditions.state:
            return False
        if conditions.min_energy is not None and state.energy < conditions.min_energy:
            return False
        if conditions.max_energy is not None and state.energy > conditions.max_energy:
            return False
        if conditions.min_loneliness is not None and state.loneliness < conditions.min_loneliness:
            return False
        if conditions.node_in and state.node_id not in conditions.node_in:
            return False
        return True

    # ================= Tick =================

    async def tick(self) -> list[TickOutcome]:
        """推进所有启用会话的世界时钟。返回需要发送的消息。"""

        self._last_tick_at = self._now()
        outcomes: list[TickOutcome] = []
        for session_id in self.enabled_session_ids():
            try:
                outcome = await self._tick_session(session_id)
            except Exception as exc:  # 单个会话出错不影响其他会话
                self._log("warning", f"tick 失败 session={session_id}: {exc}")
                continue
            if outcome is not None:
                outcomes.append(outcome)
        try:
            outcomes.extend(await self.run_schedules())
        except Exception as exc:
            self._log("warning", f"日程检查失败: {exc}")
        # 对话记忆：把攒着的片段总结成一条（放在这里是为了不占着会话锁调模型）
        for session_id in self.enabled_session_ids():
            try:
                flushed = await self.flush_pending_memory(session_id)
            except Exception as exc:
                self._log("warning", f"总结对话记忆失败 session={session_id}: {exc}")
                continue
            if flushed is not None:
                outcomes.append(flushed)
        for outcome in outcomes:
            await self._deliver(outcome)
        return outcomes

    async def _tick_session(self, session_id: str) -> TickOutcome | None:
        outcome = TickOutcome(session_id=session_id)
        async with self.session_state(session_id) as state:
            echo_marker = await self._event_marker(state)
            node = self.node(state.node_id) or self.node(self.default_node_id())
            state.world_time += 1

            # 正在做的动作被停用了：立刻停下（否则"停用"看起来没生效）
            current = state.current_action or {}
            current_def = self.world.action_map().get(str(current.get("type") or ""))
            if current and current_def is not None and not current_def.enabled:
                state.current_action = None
                state.state = STATE_IDLE
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": current_def.id,
                        "note": f"「{current_def.name or current_def.id}」已停用，动作中止",
                    },
                )

            # 1) 推进持续动作
            await self._tick_continuous(state, node, outcome)

            # 2) 计划推进（无 LLM）
            await self._tick_plan(state, node, outcome, depth=0)

            # 2.5) 刚到新地方、手上又没安排：就地决定接下来做什么
            if state.pending_arrival:
                state.pending_arrival = False
                await self.decide_after_arrival(state, self.node(state.node_id) or node, outcome)

            # 3) 数值演化
            self.dynamics.tick(
                state, node=node, elapsed_seconds=self.tick_seconds, world=self.world
            )
            state.mood = self.dynamics.derive_mood(state)

            # 4) 群聊留档攒太多时压成摘要（只在配置成"压缩"时才会跑）
            await self._maybe_compress_chat(state, outcome)

            # 4) 无人回应保护
            was_cooling = bool(state.cooldown_until and state.world_time < state.cooldown_until)
            verdict = self.engagement.evaluate(state, tick_seconds=self.tick_seconds)
            if verdict.in_cooldown and not was_cooling:
                await self._log_event(
                    state,
                    "engagement",
                    {
                        "reason": (
                            f"连续 {verdict.unanswered_count} 次主动说话没人回应，"
                            f"安静 {int(self.world.engagement.cooldown_after_unanswered)} 分钟"
                        )
                    },
                )

            # 5) 极端保护
            tick = max(1, self.tick_seconds)
            flags = self.dynamics.check_extremes(
                state,
                low_energy_ticks=int(3 * 86400 / tick),
                high_loneliness_ticks=int(24 * 3600 / tick),
            )
            if (
                flags
                and state.current_plan is None
                and state.current_action is None
                and self._forced_plan_allowed(state)
            ):
                for flag in flags:
                    plan = self.decider.forced_plan(state, flag)
                    if (
                        plan is not None
                        and self.plan_speaks(plan)
                        and self.engagement.proactive_blocked(state)
                    ):
                        # 刚回过话说明有人理她，别再触发"太久没人说话，强制找人"
                        continue
                    if plan:
                        state.current_plan = plan
                        state.last_forced_plan_at = self._now()
                        state.last_forced_flag = flag
                        self._count_autonomous(state)
                        state.add_event("extreme", {"flag": flag})
                        await self._log_event(state, "extreme", {"flag": flag})
                        break

            # 6) 群名片同步
            await self._sync_nickname(state, node)

            if state.current_action or state.current_plan:
                node = self.node(state.node_id) or node
            outcome.notes.append(
                f"t={state.world_time} node={state.node_id} state={state.state} mood={state.mood}"
            )
            if outcome.messages:
                # 她这一轮主动开口了，也算回应过了这些群聊内容
                self.mark_chat_replied(state)
            await self._echo_events_since(state, outcome, echo_marker)
        return outcome

    async def _tick_continuous(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        action = state.current_action
        if not isinstance(action, dict):
            return
        action["elapsed_ticks"] = int(action.get("elapsed_ticks", 0)) + 1
        if int(action.get("elapsed_ticks", 0)) < int(action.get("duration_ticks", 0) or 0):
            return
        await self._finish_action(state, node, outcome, action)

    async def _finish_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        action: dict[str, Any],
    ) -> None:
        action_id = str(action.get("type", ""))
        definition = self.world.action_map().get(action_id)

        handled_followup = False
        if action_id == "walk_to":
            target_node = str(action.get("target_node", "") or "")
            if target_node and target_node in self.world.node_map():
                # 离开这个地点之前，把刚才在那儿聊的那段总结成一条记忆
                if bool(self.world.memory.summary_on_move):
                    state.memory_flush_wanted = True
                state.node_id = target_node
                state.add_event("move", {"to": target_node})
                # 特地走到一个新地方，落地后就地看看这里能做什么
                if int(action.get("arrival_decide", 1)):
                    state.pending_arrival = True
        elif definition is not None and definition.llm_level == "tool":
            # 工具型动作：配了几个工具就按顺序调几个，成功失败都记账，
            # 日志页要能看出"她到底查到了什么 / 为什么没查到"
            await self._run_tool_calls(state, definition, action)
        elif definition is not None and definition.llm_level == "command":
            # 持续型的指令动作：和工具型动作一样，到点才把指令发出去。
            # 以前这里没有这一支，"查天气"这种配成持续动作的指令永远不执行、也一条日志都没有。
            await self._run_command_action(
                state,
                node,
                outcome,
                definition,
                PlannedAction(
                    type=action_id,
                    intent=str(action.get("intent") or ""),
                    content=str(action.get("content") or ""),
                    target=str(action.get("target") or ""),
                    target_node=str(action.get("target_node") or ""),
                    params=dict(action.get("params") or {}),
                ),
            )
            handled_followup = True

        effects = (definition.on_complete.effects if definition else {}) or {}
        if effects:
            self.dynamics.apply_effects(state, effects, world=self.world)

        # 按「实际持续时间」缩放的效果：例如小睡 30 分钟 → 精力 +0.002×30
        per_minute = (definition.on_complete.effects_per_minute if definition else {}) or {}
        if per_minute:
            elapsed_seconds = int(action.get("elapsed_ticks", 0)) * self.tick_seconds
            minutes = max(0.0, elapsed_seconds / 60.0)
            if minutes > 0:
                self.dynamics.apply_effects(
                    state, per_minute, world=self.world, scale=minutes
                )

        # 行动完成 -> 记忆
        if definition is not None:
            self._remember_action(state, definition, action)

        state.current_action = None
        state.state = STATE_IDLE
        if action.get("from_plan") and state.current_plan is not None:
            advance(state)

        await self._log_event(
            state,
            "action_done",
            {
                "type": action_id,
                "elapsed_ticks": int(action.get("elapsed_ticks", 0)),
                "tool_result": _clip_text(action.get("tool_result"), 200),
            },
        )

        trigger = definition.on_complete.trigger if definition else "none"
        detail = str(action.get("tool_result", "") or "")
        images = list(action.get("tool_images") or [])
        if not detail and images:
            detail = "工具返回了一张图片，图片一起发给你了。"
        hint = definition.on_complete.prompt_hint if definition else ""
        want_followup = trigger == "llm_followup"
        is_tool_action = bool(definition is not None and definition.llm_level == "tool")
        tool_failed = is_tool_action and not bool(action.get("tool_ok", True))
        if handled_followup:
            # 指令动作自己已经把结果交回给她说过一句了，这里别再问一次
            want_followup = False
        if want_followup and is_tool_action and not detail:
            # 工具没拿到东西时不要让她"就着空气说话"——那只会编
            want_followup = False
            outcome.notes.append(
                f"工具动作 {action_id} 没有拿到结果"
                f"（{action.get('tool_error') or '结果为空'}），不作续说"
            )
        if (
            not want_followup
            and trigger == "none"
            and not tool_failed
            and detail
            and is_tool_action
            and bool(self.world.tool_result_reply)
        ):
            # 工具型动作拿到结果后，默认交回主模型说一句（可以用全局设置关掉）
            want_followup = True
            hint = hint or "把刚才查到的结果用你自己的话说出来，简短自然，别念成清单。"
        if want_followup and self.llm is not None:
            if not detail:
                # 不是工具动作（做饭、看书…）也要能续说：给她一句"刚刚做了什么"的交代
                minutes = max(
                    0, round(int(action.get("elapsed_ticks", 0)) * self.tick_seconds / 60)
                )
                label = definition.name or definition.id if definition else ""
                detail = f"你刚刚做完了「{label}」，用了大约 {minutes} 分钟。"
            detail = self._clip_followup(detail)
            await self._llm_followup(
                state, node, outcome, hint, detail, image_urls=images
            )
        elif trigger == "schedule":
            await self._run_linked_schedule(state, node, outcome, definition, depth=1)

    async def _run_linked_schedule(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        *,
        depth: int,
    ) -> None:
        """``on_complete.trigger = schedule``：动作做完后接着跑另一个日程的动作链。"""

        schedule_id = (definition.on_complete.schedule_id or "").strip()
        if not schedule_id or self.schedules is None:
            return
        target = next(
            (item for item in self.schedules.schedules if item.id == schedule_id), None
        )
        if target is None:
            outcome.notes.append(f"找不到后续日程 {schedule_id}")
            return
        if depth > self.world.limits.max_action_chain_depth:
            outcome.notes.append("动作链超过深度上限，已停止")
            return
        state.add_event("chain", {"schedule": schedule_id})
        await self._run_chain(state, target.action_chain, outcome, depth=depth)

    def _remember_action(
        self, state: WorldState, definition: ActionDef, action: dict[str, Any]
    ) -> None:
        label = definition.name or definition.id
        if definition.id == "think":
            content = str(action.get("content", "") or "")
            if content:
                state.add_thought(content)
                self.memory.remember(
                    session_id=state.session_id,
                    persona_id=str(action.get("persona_id", "")),
                    node_id=state.node_id,
                    content=content,
                    memory_type=INNER,
                    emotion=state.mood,
                    weight=0.35,
                    affect=state.affect,
                )
            return
        self.memory.remember(
            session_id=state.session_id,
            persona_id=str(action.get("persona_id", "")),
            node_id=state.node_id,
            content=f"在这里{label}",
            memory_type=SCENE,
            emotion=state.mood,
            weight=0.3,
            affect=state.affect,
        )

    async def _llm_followup(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        hint: str,
        tool_result: str,
        image_urls: list[str] | None = None,
    ) -> None:
        persona_text = await self._persona_text(state.session_id)
        system_prompt = self.prompts.build_autonomous_system_prompt(
            persona_text=persona_text,
            state=state,
            node=node,
            available_tools=self.available_tools(),
            memories=self.memory.recall(
                session_id=state.session_id,
                persona_id="",
                node_id=state.node_id,
                limit=3,
            ),
            engagement_hint=self.engagement.hint(state),
            max_messages=self.world.limits.max_messages_per_say,
            recent_chat=self.chat_context(state),
            reasoning=bool(self.world.reasoning_enabled),
        )
        prompt = self.prompts.build_reply_followup_prompt(hint, tool_result)
        reply = await self._ask_llm(
            state.session_id,
            system_prompt,
            prompt,
            image_urls=list(image_urls or []) or None,
        )
        outcome.llm_calls += 1
        if reply is None:
            return
        result = parse_action_payload(
            reply,
            available_actions=self._parseable_action_ids(state.node_id),
            valid_nodes=set(self.world.node_map()),
            max_actions=self.world.limits.max_actions_per_message,
            max_messages=self.world.limits.max_messages_per_say,
        )
        await self._execute_actions(state, node, outcome, result.actions, depth=1, autonomous=True)

    async def _tick_plan(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        *,
        depth: int,
    ) -> None:
        if depth > self.world.limits.max_action_chain_depth:
            return
        if state.current_action is not None:
            return
        step = peek_step(state)
        if step is None:
            return
        action = self._step_to_action(step)
        executed = await self._execute_actions(
            state, node, outcome, [action], depth=depth, autonomous=True, from_plan=True
        )
        if not executed:
            # 这一步被跳过了（地点/前置条件/缺工具…）。不推进的话它会每个 tick 重试一次。
            outcome.notes.append(f"计划里的「{action.type}」没做成，跳过这一步")
            advance(state)

    # ================= 自主行为 =================

    async def maybe_decide(
        self, session_id: str, *, force: bool = False
    ) -> TickOutcome | None:
        """决策器评估：规则优先，必要时（低频抽样）才问 LLM。

        ``force=True``（编辑器里手动点"触发决策"）会跳过间隔节流，
        但仍然尊重"她正在忙 / 在睡觉"这些硬条件——只是不再静默吞掉。
        """

        if not self.is_enabled(session_id):
            return None
        now = self._now()
        last = self._last_decider_at.get(session_id, 0.0)
        if not force and now - last < self.decider_interval:
            return None
        self._last_decider_at[session_id] = now

        outcome = TickOutcome(session_id=session_id)
        async with self.session_state(session_id) as state:
            echo_marker = await self._event_marker(state)
            node = self.node(state.node_id) or self.node(self.default_node_id())
            if state.current_action is not None or active_plan(state) is not None:
                return None
            if state.is_sleeping:
                return None
            if not self.engagement.can_speak(state):
                outcome.notes.append("处于冷却期，跳过自主行为")
            elif not self._within_hourly_limit(state):
                outcome.notes.append("达到每小时自主行为上限")
            else:
                plan = self.decider.rule_plan(
                    state,
                    group_chatting=self.group_is_chatting(state),
                    interject_allowed=self.interject_allowed(state),
                )
                # 要不要交给大模型，由「决策意愿」算出的概率决定；
                # 规则决策不受影响，命中就直接执行。
                if self.decider.should_ask_llm(state):
                    llm_plan = await self._ask_llm_for_plan(state, node, outcome)
                    plan = llm_plan or plan
                if plan is not None and self.plan_speaks(plan):
                    # 刚被搭话、她也回过了：这段时间别主动开口，免得跟被动回复挤在一起
                    if self.engagement.proactive_blocked(state):
                        outcome.notes.append("刚回过话，这轮不主动搭话")
                        await self._log_event(
                            state,
                            "engagement",
                            {"reason": "刚回过话，跳过主动发言"},
                        )
                        plan = None
                if plan is not None:
                    await self._apply_plan(state, node, outcome, plan)
                    self._count_autonomous(state)
                    await self._tick_plan(state, node, outcome, depth=0)
            await self._echo_events_since(state, outcome, echo_marker)
        if outcome.messages or outcome.debug_messages:
            await self._deliver(outcome)
        return outcome

    async def _apply_plan(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        plan: dict[str, Any],
    ) -> None:
        """把一份计划挂到状态上并记日志（自主决策与「抵达新地点」共用）。"""

        if any(step.get("interject") for step in plan.get("steps", [])):
            state.last_interject_at = self._now()
        state.current_plan = plan
        # 留一份「最近一次生成的计划」：做完之后提示词里还要能提到它
        state.last_plan = {
            "steps": [
                {
                    "action": str(step.get("action") or ""),
                    "target_node": str(step.get("target_node") or ""),
                }
                for step in plan.get("steps", [])
                if isinstance(step, dict)
            ],
            "reason": str(plan.get("reason") or ""),
            "source": str(plan.get("source") or ""),
            "at": int(state.world_time),
        }
        if plan.get("reason"):
            state.note_reasoning({"intent": str(plan.get("reason"))}, source="plan")
        state.add_event(
            "plan", {"reason": plan.get("reason"), "source": plan.get("source")}
        )
        await self._log_event(
            state,
            "plan",
            {
                "reason": plan.get("reason"),
                "source": plan.get("source"),
                "steps": plan.get("steps"),
                "raw": _clip_text(plan.get("raw"), 200),
            },
        )

    async def decide_after_arrival(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        """刚走到一个新地方：立刻看看这儿能做什么。

        「特地走过去」本身就是一次明确的意图，所以这一步直接问大模型，
        不看那条概率抽样、也不占「每小时最多自主行动次数」的额度；
        但仍然守冷却和一个专门的每小时上限，避免来回走 + 反复决策把 token 烧光。
        """

        if not self.world.decider.enabled or state.is_sleeping:
            return
        if state.current_plan is not None or state.current_action is not None:
            return
        if not self.engagement.can_speak(state):
            outcome.notes.append("抵达新地点，但处于冷却期，不做决策")
            return
        if not self._arrival_decision_allowed(state):
            outcome.notes.append("抵达新地点的决策次数已达每小时上限")
            return
        self._count_arrival_decision(state)
        plan = self.decider.rule_plan(
            state,
            group_chatting=self.group_is_chatting(state),
            interject_allowed=self.interject_allowed(state),
        )
        if plan is not None and self.plan_speaks(plan) and self.engagement.proactive_blocked(state):
            outcome.notes.append("刚回过话，到了新地方也不主动搭话")
            plan = None
        # 她特地走过来通常是有目的的：把刚才的打算和她现在能做什么摆在最前面，
        # 免得这次调用还在"重新规划整段时间"。
        where = (node.name if node else "") or state.node_id
        intent = str((state.last_reasoning or {}).get("intent") or "").strip()
        here = "、".join(
            f"{item.id}（{item.name or item.id}）" for item in self.world.actions_in(state.node_id)
        )
        hint = f"你刚刚特地走到了「{where}」"
        hint += f"，因为你打算：{intent}。" if intent else "。"
        hint += (
            f"这里能做：{here or '没什么特别的'}。"
            "直接安排你到了这里要做的那件事；如果你只是随便走走，就挑一件这儿能做的事。"
        )
        llm_plan = await self._ask_llm_for_plan(
            state, node, outcome, force=True, hint=hint
        )
        plan = llm_plan or plan
        if plan is None:
            return
        outcome.notes.append("抵达新地点，就地决定接下来做什么")
        await self._apply_plan(state, node, outcome, plan)
        await self._tick_plan(state, node, outcome, depth=0)

    async def _ask_llm_for_plan(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        *,
        force: bool = False,
        hint: str = "",
    ) -> dict[str, Any] | None:
        if self.llm is None:
            return None
        if not force and not self._llm_plan_allowed(state):
            outcome.notes.append("LLM 计划预算已用完，本轮只用规则决策")
            return None
        persona_text = await self._persona_text(state.session_id)
        system_prompt = self.prompts.build_autonomous_system_prompt(
            persona_text=persona_text,
            state=state,
            node=node,
            available_tools=self.available_tools(),
            memories=self.memory.recall(
                session_id=state.session_id, persona_id="", node_id=state.node_id, limit=4
            ),
            engagement_hint=self.engagement.hint(state),
            max_messages=self.world.limits.max_messages_per_say,
            recent_chat=self.chat_context(state),
            mode="plan",
            reasoning=bool(self.world.reasoning_enabled),
        )
        prompt = (
            (f"{hint}\n\n" if hint else "")
            + "按你自己的节奏决定接下来做什么，输出计划 JSON：\n"
            '{"plan":[{"action":"walk_to","target_node":"window"},'
            '{"action":"stare","duration":600},{"action":"think","content":"..."}],'
            '"valid_until":1800,"reason":"为什么这样安排"}\n'
            "计划的长度由你决定：可以是几分钟的小事，也可以睡一整晚"
            "（duration 是这一步持续多少秒，睡觉是 28800；valid_until 是这份计划大概管多少秒）。\n"
            "结合上面的日期时间、你的状态和作息挑一件最合适的事："
            "深夜或精力见底就该去睡觉，白天只是犯困才小睡。\n"
            "只输出 JSON。"
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        outcome.llm_calls += 1
        self._count_llm_plan(state)
        if reply is None:
            return None
        plan, _warnings = parse_plan_payload(
            reply,
            available_actions=self._parseable_action_ids(state.node_id),
            valid_nodes=set(self.world.node_map()),
        )
        if plan:
            created = create_plan(
                steps=plan.get("steps", []),
                world_time=state.world_time,
                valid_for=int(plan.get("valid_for", self.world.limits.plan_valid_duration)),
                reason=str(plan.get("reason", "")),
                source="llm",
            )
            if created is not None:
                # 留下模型原话，日志页里能看到"她为什么这么安排"
                created["raw"] = _clip_text(reply, 240)
            return created
        return None

    # ================= 动作执行 =================

    def _available_action_ids(self, node_id: str) -> set[str]:
        return {action.id for action in self.world.actions_in(node_id)}

    def _nearest_node_with_action(self, node_id: str, definition: ActionDef) -> str:
        """哪个可达地点能做这个动作（挑最近的）。"""

        candidates = [
            node.id for node in self.world.nodes if definition.available_in(node.id)
        ]
        if not candidates:
            return ""
        return nearest_node(self.world.adjacent(), node_id, candidates) or ""

    def _parseable_action_ids(self, node_id: str) -> set[str]:
        """解析模型输出时认可的动作集合。

        默认 = 当前地点能做的动作。开启「允许她想去别处做某事」后，
        可达地点能做的动作也算数——她写出来，插件负责带她过去。
        """

        if not bool(self.world.remote_action_travel):
            return self._available_action_ids(node_id)
        return {
            action.id
            for action in self.world.actions
            if action.available_in(node_id)
            or self._nearest_node_with_action(node_id, action)
        }

    def _step_to_action(self, step: dict[str, Any]) -> PlannedAction:
        return PlannedAction(
            type=str(step.get("action", "")),
            interject=bool(step.get("interject", False)),
            messages=list(step.get("messages") or []),
            target=str(step.get("target", "") or ""),
            target_node=str(step.get("target_node", "") or ""),
            content=str(step.get("content", "") or ""),
            intent=str(step.get("intent", "") or ""),
            params=dict(step.get("params") or {}),
            duration=int(step.get("duration", 0) or 0),
            raw=step,
        )

    @staticmethod
    def _step_payload(item: PlannedAction) -> dict[str, Any]:
        """把待执行动作转成计划里的一步。

        注意 ``intent`` 一定要带上：工具型动作的参数就是靠它补出来的，
        少了它这一步到点执行时只会得到「没有给出想做什么」。
        """

        return {
            "action": item.type,
            "interject": bool(item.interject),
            "messages": list(item.messages or []),
            "target": item.target,
            "target_node": item.target_node,
            "duration": item.duration,
            "content": item.content,
            "intent": item.intent,
            "params": dict(item.params or {}),
            "status": "pending",
        }

    @staticmethod
    def _plan_remaining_steps(
        plan: dict[str, Any] | None, *, skip_current: bool = False
    ) -> list[dict[str, Any]]:
        """一份计划里还没做完的步骤。

        ``skip_current=True`` 用于"正在执行计划里这一步"的场景：那一步已经拿在手上了，
        再排一遍会重复执行。
        """

        if not isinstance(plan, dict):
            return []
        steps = plan.get("steps") or []
        index = max(0, int(plan.get("current_step", 0) or 0))
        if skip_current:
            index += 1
        return [
            dict(step)
            for step in steps[index:]
            if isinstance(step, dict) and step.get("action")
        ]

    async def _execute_actions(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        actions: list[PlannedAction],
        *,
        depth: int,
        autonomous: bool,
        from_plan: bool = False,
    ) -> bool:
        """执行一串动作。返回"有没有真的执行到至少一个"。"""

        if depth > self.world.limits.max_action_chain_depth:
            outcome.notes.append("动作链超过深度上限，已停止")
            return False
        executed = False
        max_actions = self.world.limits.max_actions_per_message
        queued_actions = list(actions)[:max_actions]
        # 进入这一轮之前她自己没做完的安排：这一轮要排队时接在它们前面，而不是把它们顶掉。
        # 正在执行计划里的某一步时要跳过那一步，否则它会做第二遍。
        existing_plan = active_plan(state)
        carried = self._plan_remaining_steps(existing_plan, skip_current=from_plan)
        carried_reason = str((existing_plan or {}).get("reason") or "")
        carried_source = str((existing_plan or {}).get("source") or "")
        for index, action in enumerate(queued_actions):
            definition = self.world.action_map().get(action.type)
            if definition is None:
                outcome.notes.append(f"动作 {action.type} 不存在，已跳过")
                continue
            if not definition.enabled:
                # 停用 = 当作根本没有这个动作
                outcome.notes.append(f"动作 {action.type} 已停用，已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": action.type,
                        "note": f"「{definition.name or action.type}」已停用",
                    },
                )
                continue
            if not definition.available_in(state.node_id):
                # A2 兜底：她想去别处做这件事，自己又没写移动 —— 插件带她过去
                target = (
                    self._nearest_node_with_action(state.node_id, definition)
                    if bool(self.world.remote_action_travel)
                    else ""
                )
                if target:
                    await self._travel_then_continue(
                        state,
                        outcome,
                        definition,
                        target,
                        queued_actions[index:],
                        carried=carried,
                    )
                    return True
                outcome.notes.append(f"动作 {action.type} 在 {state.node_id} 不可用，已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {"action": action.type, "note": f"「{action.type}」在 {state.node_id} 不可用"},
                )
                continue
            ok, reason = self._check_preconditions(state, definition)
            if not ok:
                outcome.notes.append(f"动作 {action.type} 前置条件不满足（{reason}），已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {"action": action.type, "note": f"「{action.type}」前置条件不满足：{reason}"},
                )
                continue
            if definition.llm_level == "tool":
                # 主模型只给了"想干什么"，参数交给辅助模型按工具定义补全
                if not await self._prepare_tool_action(
                    state, definition, action, outcome
                ):
                    continue
            await self._start_action(
                state,
                node,
                outcome,
                definition,
                action,
                depth,
                autonomous,
                from_plan=from_plan,
            )
            executed = True
            # 她开始做一个要花时间的动作时，后面的动作不能立刻抢着执行，
            # 更不能把它顶掉——转成计划，等她忙完再做。
            if state.current_action is not None:
                rest = queued_actions[index + 1 :]
                if rest:
                    fresh = [self._step_payload(item) for item in rest]
                    state.current_plan = create_plan(
                        steps=fresh + carried,
                        world_time=state.world_time,
                        valid_for=self.world.limits.plan_valid_duration,
                        reason=carried_reason or "同一轮里还没做完的动作",
                        source=carried_source or "pending",
                    )
                    note = (
                        f"她开始「{definition.name or definition.id}」，"
                        f"剩下的 {len(fresh)} 个动作排队等它做完"
                    )
                    if carried:
                        note += f"；原来没做完的 {len(carried)} 步接在它们后面"
                    outcome.notes.append(note)
                return True
        return executed

    async def _travel_then_continue(
        self,
        state: WorldState,
        outcome: TickOutcome,
        definition: ActionDef,
        target_node: str,
        pending: list[PlannedAction],
        *,
        carried: list[dict[str, Any]] | None = None,
    ) -> None:
        """她想去别处做某件事、自己又没写移动：先送她过去，剩下的排进计划。

        与规则决策器里的「先走再做」是同一套逻辑，只是这里替大模型补上那一步。
        """

        outcome.notes.append(
            f"她想做「{definition.name or definition.id}」，但当前地点做不了，"
            f"先带她去{self._node_name(target_node)}"
        )
        outcome.auto_travel.append(target_node)
        # 她把"说一句"排在移动后面时，这一轮会一个字都不发、还会被判定成接管失败。
        # 瞬时动作不占时间，先执行掉——用户立刻看到回应，剩下的等走过去再做。
        instant = [
            item
            for item in pending
            if (self.world.action_map().get(item.type) is not None)
            and self.world.action_map()[item.type].category == "instant"
        ]
        for item in instant:
            await self._start_action(
                state,
                self.node(state.node_id),
                outcome,
                self.world.action_map()[item.type],
                item,
                0,
                False,
            )
        pending = [item for item in pending if item not in instant]
        if not pending:
            return
        await self._log_event(
            state,
            "plan",
            {
                "reason": f"为了做「{definition.name or definition.id}」先移动过去",
                "source": "auto_travel",
                "steps": [{"action": item.type} for item in pending],
            },
        )
        walk = PlannedAction(type="walk_to", target_node=target_node)
        await self._start_action(
            state, self.node(state.node_id), outcome, self.world.action_map()["walk_to"],
            walk, 0, False,
        )
        if state.current_action is None:
            return
        # 她自己原来没做完的安排接在后面：主人这一轮的要求先做，再回去做她自己的事
        tail = carried if carried is not None else self._plan_remaining_steps(active_plan(state))
        state.current_plan = create_plan(
            steps=[self._step_payload(item) for item in pending] + tail,
            world_time=state.world_time,
            valid_for=self.world.limits.plan_valid_duration,
            reason=f"到了{self._node_name(target_node)}之后要做的事",
            source="auto_travel",
        )

    def _node_name(self, node_id: str) -> str:
        node = self.node(node_id)
        return (node.name or node.id) if node else node_id

    def _check_preconditions(self, state: WorldState, definition: ActionDef) -> tuple[bool, str]:
        pre = definition.preconditions
        if pre.not_state and state.state in pre.not_state:
            return False, f"不能在 {state.state} 状态执行"
        if pre.min_energy is not None and state.energy < pre.min_energy:
            return False, "精力不足"
        return True, ""

    def _tool_exists(self, name: str) -> bool:
        return name in self.available_tools()

    def _tool_hint(self, limit: int = 8) -> str:
        """给「缺少工具」的报错补上"现在到底有哪些工具"，方便用户自己改配置。"""

        names = sorted(self.available_tools())
        if not names:
            return "（AstrBot 里当前没有注册任何工具）"
        shown = "、".join(names[:limit])
        if len(names) > limit:
            shown += f" 等 {len(names)} 个"
        return f"（当前可用工具：{shown}）"

    def resolve_tool(self, action_id: str, node_id: str = "", tool_name: str = "") -> str:
        """这个动作最终会用哪个工具。

        工具型动作必须显式指定工具：名字已注册、且不是「直发消息」类工具才算可用。
        """

        name = str(tool_name or "").strip()
        if not name:
            return ""
        if not self._tool_exists(name) or is_self_send_tool(name):
            return ""
        return name

    def resolve_tools(self, definition: ActionDef, node_id: str = "") -> list[str]:
        """这个动作最终会用到的工具：配了几个就返回几个，按配置顺序。"""

        result: list[str] = []
        for name in definition.tool_list():
            chosen = self.resolve_tool(definition.id, node_id, name)
            if chosen and chosen not in result:
                result.append(chosen)
        return result

    async def fill_tool_params(
        self,
        state: WorldState,
        definition: ActionDef,
        action: PlannedAction,
        *,
        tool_name: str = "",
        params: dict[str, Any] | None = None,
        previous_results: str = "",
        error_hint: str = "",
    ) -> tuple[dict[str, Any], str]:
        """把「她想干什么」翻译成工具需要的参数字典。

        返回 ``(params, note)``：note 非空表示没补全（原因），调用方据此决定跳过还是硬着头皮调。
        相同工具 + 相同意图会命中缓存，不会反复请求。

        ``previous_results`` 用于一个动作挂了多个工具的场景：把它传给补全模型，
        后面的工具就能用上前一个工具查回来的内容（例如先搜到网址、再去抓正文）。
        ``error_hint`` 是上一次调用报的错：带着它再补一次，能救回"schema 写可选、
        实现却必须要"这类工具。
        """

        base = dict(params if params is not None else (action.params or {}))
        chosen = self.resolve_tool(
            definition.id, state.node_id, tool_name or definition.tool_name
        )
        if not chosen or self.helper_llm is None:
            return {}, ""
        missing = self.missing_params_for(chosen, base)
        unfilled = self.unfilled_params(chosen, base)
        if not missing and not unfilled and not previous_results and not error_hint:
            # 已经齐了就不用问模型；但带了"上一个工具的结果"时是有意重算，必须再问一次
            return {}, ""
        intent = (action.intent or action.content or "").strip()
        if not intent:
            # 日程里的步骤没有"想干什么"这一栏：用动作自己的说明兜一句，
            # 否则这条工具动作到点就只会被跳过。
            intent = self.fallback_intent(definition)
        cache_key = (chosen, intent)
        # 带了上一个工具的结果、或者带了报错时都是"有意重算"，不吃缓存
        cached = (
            None
            if (previous_results or error_hint)
            else self._filled_params.get(cache_key)
        )
        if cached:
            return dict(cached), ""
        if not self._tool_param_allowed(state):
            return {}, "本小时的工具参数补全次数已用完"

        schema = self.tool_schemas().get(chosen) or {}
        description = self.available_tools().get(chosen, "")
        system_prompt, prompt = self.prompts.build_tool_params_prompt(
            tool_name=chosen,
            tool_description=description,
            param_text=render_param_text(schema),
            intent=intent,
            recent_chat=self.chat_context(state),
            previous_results=previous_results,
            error_hint=error_hint,
        )
        reply = await self._ask_helper(
            state.session_id, system_prompt, prompt
        )
        self._count_tool_param(state)
        if reply is None:
            return {}, "参数补全模型调用失败"
        payload = extract_json_object(reply)
        if not isinstance(payload, dict):
            return {}, "参数补全模型没有返回合法 JSON"
        filled = {
            str(key): value
            for key, value in payload.items()
            if not str(key).startswith("_")
        }
        if not filled:
            return {}, "参数补全模型没有给出参数"
        given = {k: v for k, v in base.items() if v not in ("", None)}
        # 平时以调用方给的参数为准；带上了上一个工具的结果时，以模型新给的为准（覆盖之前猜的）
        merged = {**given, **filled} if previous_results else {**filled, **given}
        still_missing = self.missing_params_for(chosen, merged)
        if still_missing:
            return {}, f"仍缺少参数 {still_missing}"
        if not previous_results:
            self._filled_params[cache_key] = dict(merged)
        if len(self._filled_params) > 200:
            self._filled_params.clear()
        return merged, ""

    async def _ask_helper(
        self, session_id: str, system_prompt: str, prompt: str
    ) -> str | None:
        if self.helper_llm is None:
            return None
        try:
            reply = await self.helper_llm.generate(
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.0,
            )
        except Exception as exc:
            self._log("debug", f"参数补全调用失败：{exc}")
            return None
        return reply.text if getattr(reply, "ok", False) else None

    def _tool_param_allowed(self, state: WorldState) -> bool:
        hour = int(self._now() // 3600)
        if state.tool_param_hour_marker != hour:
            state.tool_param_hour_marker = hour
            state.tool_param_count_hour = 0
        return state.tool_param_count_hour < int(self.world.limits.max_tool_param_per_hour)

    @staticmethod
    def _count_tool_param(state: WorldState) -> None:
        state.tool_param_count_hour = int(state.tool_param_count_hour) + 1

    @staticmethod
    def _act_get(action: Any, key: str, default: Any = None) -> Any:
        """待执行动作有两种形态（PlannedAction / 运行中的 dict），统一读法。"""

        if isinstance(action, dict):
            return action.get(key, default)
        return getattr(action, key, default)

    @staticmethod
    def _act_set(action: Any, key: str, value: Any) -> None:
        if isinstance(action, dict):
            action[key] = value
        else:
            setattr(action, key, value)

    async def _prepare_tool_action(
        self,
        state: WorldState,
        definition: ActionDef,
        action: Any,
        outcome: TickOutcome,
    ) -> bool:
        """工具型动作动手前的准备：确认工具可用，并把每个工具的参数补齐。

        返回 ``False`` 表示这次跑不了（跳过原因已经记进日志）。补好的参数写在
        ``action.tool_params``（工具名 -> 参数字典），执行时按顺序逐个调用。
        """

        names = self.resolve_tools(definition, state.node_id)
        if not names:
            picked = "、".join(definition.tool_list())
            if not picked:
                reason = "工具型动作必须选一个工具，这个动作还没选"
            elif is_self_send_tool(definition.tool_list()[0]):
                reason = f"「{definition.tool_list()[0]}」是直发消息的工具，插件不会调用（会绕过回复管线）"
            else:
                reason = f"选的工具「{picked}」在 AstrBot 里没注册{self._tool_hint()}"
            outcome.notes.append(f"工具动作 {definition.id} 找不到可用工具，已跳过")
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": f"工具动作「{definition.name or definition.id}」找不到可用工具：{reason}",
                },
            )
            return False

        # 用户在动作里配好的固定参数（例如查天气固定 city=武汉）先铺底，
        # 大模型这一轮写出来的参数覆盖在它上面。
        fixed: dict[str, str] = {}
        for key, spec in (definition.params or {}).items():
            value = getattr(spec, "value", "")
            if isinstance(value, str) and value.strip():
                fixed[str(key)] = value.strip()
        base = {**fixed, **dict(self._act_get(action, "params", {}) or {})}
        stored: dict[str, dict[str, Any]] = {}
        for index, name in enumerate(names):
            # 大模型写出来的参数只当第一个工具的，其余工具由辅助模型按各自定义补
            params = dict(base) if index == 0 else {}
            missing = self.missing_params_for(name, params)
            unfilled = self.unfilled_params(name, params)
            note = ""
            if missing or unfilled:
                filled, note = await self.fill_tool_params(
                    state, definition, action, tool_name=name, params=params
                )
                if filled:
                    params = {**params, **filled}
                missing = self.missing_params_for(name, params)
            if missing:
                outcome.notes.append(
                    f"工具动作 {definition.id} 缺少必填参数 {missing}，已跳过"
                    + (f"（{note}）" if note else "")
                )
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": definition.id,
                        "note": (
                            f"工具动作「{definition.id}」缺少必填参数 {missing}"
                            + (f"：{note}" if note else "")
                        ),
                    },
                )
                return False
            stored[name] = params

        self._act_set(action, "tool_params", stored)
        self._act_set(action, "params", dict(stored.get(names[0]) or {}))
        return True

    # ---------------- 日程：查看 / 添加 / 删除（内置动作与对外工具共用） ----------------

    async def _run_command_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
        event: Any = None,
    ) -> None:
        """指令触发：把意图拼成一条指令，交给 AstrBot 去执行，再把结果交回给她。

        有真实消息事件时直接用那条事件（图片、引用都跟着走）；
        自主触发时借用这个会话最近一条事件——所以在这种情况下没有图片。
        """

        command = str(definition.trigger_command or "").strip()
        if not command:
            outcome.notes.append(f"动作 {definition.id} 没有配置要触发的指令，已跳过")
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": "指令型动作没有配置要触发的指令"},
            )
            return
        intent = (action.intent or action.content or "").strip()
        if not intent:
            # 日程里的步骤没有"想干什么"这一栏：用动作自己的说明兜一句
            intent = self.fallback_intent(definition)

        line = await self._compose_command(state, definition, command, intent)
        if self.commands is None:
            outcome.notes.append("没有可用的指令通道，已跳过")
            return
        call = await self.commands.trigger(state.session_id, line)
        images = list(getattr(call, "image_urls", None) or [])
        await self._log_event(
            state,
            "command",
            {
                "action": definition.id,
                "command": line,
                "ok": bool(call.ok),
                "result": _clip_text(call.text, 400),
                "images": len(images),
                "error": call.error,
            },
            outcome=self._echo_into(outcome, state),
        )
        outcome.notes.append(
            f"触发指令「{line}」：" + ("成功" if call.ok else f"失败（{call.error}）")
        )
        detail = call.text if call.ok else f"（这条指令没跑成：{call.error}）"
        detail = self._clip_followup(detail)
        if not detail and images:
            detail = "这条指令返回了一张图片，图片一起发给你了。"

        # 说完不说一句，看动作里的「完成后」和全局的「工具结果回话」——
        # 与工具型动作同一套规则，不然用户想让她执行完指令别吭声也关不掉。
        trigger = str(definition.on_complete.trigger or "none")
        hint = str(definition.on_complete.prompt_hint or "").strip()
        want_followup = trigger == "llm_followup"
        if (
            not want_followup
            and trigger == "none"
            and bool(self.world.tool_result_reply)
            and (detail or images)
        ):
            want_followup = True
        if not want_followup:
            outcome.notes.append(f"指令「{line}」已执行，这次不开口")
            return
        if not hint:
            hint = (
                "用你自己的话把这条指令返回的内容讲一句，别照抄格式、别编。"
                if call.ok
                else "这条指令没跑成，用一句自然的话说明一下，别编内容。"
            )
        await self._llm_followup(
            state,
            node,
            outcome,
            hint,
            detail or "（没有返回内容）",
            image_urls=images,
        )

    async def _compose_command(
        self,
        state: WorldState,
        definition: ActionDef,
        command: str,
        intent: str,
    ) -> str:
        """把意图拼成一条完整指令；补不出参数就退回指令名本身。"""

        base = command if command.startswith("/") else f"/{command}"
        if self.helper_llm is None or not self._tool_param_allowed(state):
            return base
        system_prompt, prompt = self.prompts.build_command_prompt(
            command=base,
            hint=str(definition.trigger_hint or ""),
            intent=intent,
        )
        reply = await self._ask_helper(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        text = " ".join(str(reply or "").split()).strip().strip("`")
        if not text:
            return base
        # 补参模型偶尔会回一坨 JSON 或一整句话：那不是指令，宁可只发指令名本身，
        # 也别往群里发一条 `/{"actions":...}` 这种莫名其妙的东西。
        if any(mark in text for mark in ('{', '}', '[', ']', '"', "'")):
            self._log(
                "debug",
                f"指令参数补全返回的内容不像指令，已退回指令名：{_clip_text(text, 80)}",
            )
            return base
        return text if text.startswith("/") else f"/{text}"

    SCHEDULE_ACTION_IDS = ("schedule_list", "schedule_add", "schedule_remove")

    def schedule_text(self) -> str:
        """把所有日程渲染成一行行给模型看的文本。"""

        items = list(getattr(self.schedules, "schedules", None) or [])
        if not items:
            return "（现在没有任何日程）"
        lines = []
        for item in items:
            chain = " → ".join(step.type for step in (item.action_chain or []) if step.type)
            days = "/".join(item.days or [])
            who = "她自己加的" if str(getattr(item, "created_by", "") or "") == "bot" else "用户配的"
            state = "启用" if item.enabled else "停用"
            lines.append(
                f"- {item.id}｜{item.time}｜{days}｜{chain or '（空）'}｜{state}｜{who}"
                + ("｜到点自动先走过去" if item.auto_travel else "")
            )
        return "\n".join(lines)

    async def schedule_add(self, payload: dict[str, Any]) -> tuple[bool, str]:
        """加一条她自己安排的日程（校验时间、星期、动作链）。"""

        raw = self.store.raw_schedules()
        items = list(raw.get("schedules") or [])
        time_text = " ".join(str(payload.get("time") or "").split())
        # 常见写法都收："7:05" / "07:5" / "7:5" 统一成 HH:MM
        if not re.match(r"^\d{1,2}:\d{1,2}$", time_text):
            return False, f"时间「{time_text or '（空）'}」看不懂，要写成 HH:MM"
        hour, minute = (int(part) for part in time_text.split(":"))
        if hour > 23 or minute > 59:
            return False, f"时间「{time_text}」不对"
        time_text = f"{hour:02d}:{minute:02d}"

        days = [
            str(day)
            for day in (payload.get("days") or [])
            if str(day) in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        ]
        if not days:
            days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

        chain: list[dict[str, Any]] = []
        for step in payload.get("action_chain") or []:
            if not isinstance(step, dict):
                continue
            action_id = str(step.get("type") or step.get("action") or "").strip()
            definition = self.world.action_map().get(action_id)
            if definition is None or not definition.enabled:
                return False, f"动作「{action_id or '（空）'}」不存在或已停用，这条日程没加成"
            item: dict[str, Any] = {"type": action_id}
            for key in ("target_node", "target", "content", "duration", "messages", "params"):
                if step.get(key):
                    item[key] = step[key]
            chain.append(item)
        if not chain:
            return False, "日程里至少要有一个动作"
        if len(chain) > max(1, int(self.world.limits.max_action_chain_depth) + 2):
            return False, "日程里的动作太多了，拆成两条吧"

        schedule_id = str(payload.get("id") or "").strip()
        if not schedule_id:
            schedule_id = f"bot_{int(self._now())}"
        if any(str(item.get("id")) == schedule_id for item in items):
            return False, f"已经有一条叫「{schedule_id}」的日程了"

        items.append(
            {
                "id": schedule_id,
                "enabled": True,
                "time": time_text,
                "days": days,
                "action_chain": chain,
                "auto_travel": bool(payload.get("auto_travel", True)),
                "conditions": dict(payload.get("conditions") or {}),
                "sessions": list(payload.get("sessions") or []),
                "priority": 5,
                "created_by": "bot",
            }
        )
        self.store.save_schedules({**raw, "schedules": items})
        self.reload_config()
        names = " → ".join(
            (self.world.action_map().get(step["type"]).name or step["type"])
            if self.world.action_map().get(step["type"])
            else step["type"]
            for step in chain
        )
        return True, f"{time_text} 的日程加好了：{names}"

    async def schedule_remove(self, selector: dict[str, Any]) -> tuple[bool, str]:
        """删掉她自己加的一条日程（用户配的动不了）。"""

        raw = self.store.raw_schedules()
        items = list(raw.get("schedules") or [])
        target_id = str(selector.get("id") or "").strip()
        time_text = str(selector.get("time") or "").strip()
        keyword = str(selector.get("keyword") or "").strip()
        matched = None
        for item in items:
            if target_id and str(item.get("id")) == target_id:
                matched = item
                break
            if time_text and str(item.get("time")) == time_text:
                matched = item
                break
            if keyword:
                blob = " ".join(
                    [str(item.get("id") or ""), str(item.get("time") or "")]
                    + [str(step.get("type") or "") for step in item.get("action_chain") or []]
                )
                if keyword in blob:
                    matched = item
                    break
        if matched is None:
            return False, "没找到那条日程"
        if str(matched.get("created_by") or "") != "bot":
            return False, "那条是用户自己配的日程，我删不了"
        items = [item for item in items if item is not matched]
        self.store.save_schedules({**raw, "schedules": items})
        self.reload_config()
        return True, f"删掉了 {matched.get('time')} 那条日程"

    async def _run_schedule_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
    ) -> None:
        """日程三件套：把意图交给辅助模型解析成结构化操作，再落库。"""

        op = {
            "schedule_list": "list",
            "schedule_add": "add",
            "schedule_remove": "remove",
        }.get(definition.id, "list")
        intent = (action.intent or action.content or "").strip()
        payload: dict[str, Any] = {}
        if op in ("add", "remove"):
            if not intent:
                await self._log_event(
                    state,
                    "schedule_edit",
                    {"op": op, "ok": False, "note": "没有说清要做什么"},
                    outcome=self._echo_into(outcome, state),
                )
                outcome.notes.append("日程动作没有给出意图，已跳过")
                return
            payload = await self._parse_schedule_intent(state, op, intent)

        if op == "list":
            ok, note = True, self.schedule_text()
        elif op == "add":
            ok, note = await self.schedule_add(payload)
        else:
            ok, note = await self.schedule_remove(payload)

        await self._log_event(
            state,
            "schedule_edit",
            {"op": op, "ok": bool(ok), "note": note, "raw": payload},
            outcome=self._echo_into(outcome, state),
        )
        outcome.notes.append(f"日程 {op}：{note}")
        hint = (
            "用一句自然的话说说你刚安排的这件事（或者刚刚看到的日程），别念清单。"
            if ok
            else "刚才那件事没办成，用一句自然的话说明一下，别编。"
        )
        await self._llm_followup(state, node, outcome, hint, note)

    async def _parse_schedule_intent(
        self, state: WorldState, op: str, intent: str
    ) -> dict[str, Any]:
        """让辅助模型把「每天七点查新闻」翻成结构化日程参数。"""

        if self.helper_llm is None or not self._tool_param_allowed(state):
            return {"raw": intent}
        action_lines = [
            f"- {item.id}：{item.name or item.id}"
            for item in self.world.actions
            if item.enabled
        ]
        system_prompt, prompt = self.prompts.build_schedule_action_prompt(
            op=op,
            intent=intent,
            actions=action_lines,
            current=self.schedule_text(),
            now=self.local_now(),
        )
        reply = await self._ask_helper(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        payload = extract_json_object(reply) if reply else None
        return payload if isinstance(payload, dict) else {"raw": intent}

    async def _run_recall(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        action: PlannedAction,
    ) -> None:
        """主动回想：把「想回忆什么」翻成检索条件，翻记忆，再带着结果问她一次。"""

        intent = (action.intent or action.content or "").strip()
        if not intent and action.target_node:
            intent = f"回忆一下{self._node_name(action.target_node)}里的事"
        if not intent:
            outcome.notes.append("回想没有说要回忆什么，已跳过")
            await self._log_event(
                state, "recall_start", {"intent": "", "note": "没有说要回忆什么"}
            )
            return

        query = await self._parse_recall_query(state, intent, action.target_node)
        await self._log_event(
            state,
            "recall_start",
            {"intent": intent, "query": query},
            outcome=self._echo_into(outcome, state),
        )

        limit = max(1, min(5, int(query.get("limit") or 3)))
        memories = self.memory.recall(
            session_id=state.session_id,
            persona_id="",
            nodes=list(query.get("nodes") or []),
            keyword=str(query.get("keyword") or ""),
            memory_type=str(query.get("type") or ""),
            mode=(self.world.memory_scope_mode if self.world else None),
            limit=limit,
            now=self._now(),
        )
        try:
            stamp_now = self.local_now()
        except Exception:
            stamp_now = None
        detail = "\n".join(
            item.render(stamp=self.prompts._memory_stamp(item.created_at, stamp_now))
            for item in memories
        )
        keyword = str(query.get("keyword") or "")
        where = "、".join(query.get("node_names") or []) or "任何地方"
        await self._log_event(
            state,
            "recall_done",
            {
                "count": len(memories),
                "keyword": keyword,
                "where": where,
                "detail": detail or "什么都没想起来",
            },
            outcome=self._echo_into(outcome, state),
        )

        hint = (
            "把你想起来的这些用第一人称说一句（可以带一点当时的情绪），别逐条念、别编新的细节。"
            if detail
            else "这次什么都没想起来——照实说一句「想不起来」就好，不要编。也可以只输出 think 或空动作列表。"
        )
        await self._llm_followup(
            state, node, outcome, hint, detail or "（翻了一遍记忆，什么都没想起来）"
        )

    @staticmethod
    def _echo_into(outcome: TickOutcome, state: WorldState) -> TickOutcome:
        """回想的两条事件也走调试回显（勾了「回想开始/回想完成」就会发出来）。"""

        return TickOutcome(session_id=outcome.session_id)

    async def _parse_recall_query(
        self, state: WorldState, intent: str, target_node: str = ""
    ) -> dict[str, Any]:
        """解析检索条件：优先问辅助模型，拿不到就退化成"按名字猜"。"""

        nodes = list(self.world.node_map().values())
        node_names = [item.name or item.id for item in nodes]
        zone_names = [zone.name or zone.id for zone in self.world.zones]
        query: dict[str, Any] = {}
        if self.helper_llm is not None and self._tool_param_allowed(state):
            system_prompt, prompt = self.prompts.build_recall_query_prompt(
                intent=intent,
                node_names=node_names,
                zone_names=zone_names,
                memory_types=["scene", "interaction", "relation", "inner", "event"],
            )
            reply = await self._ask_helper(state.session_id, system_prompt, prompt)
            self._count_tool_param(state)
            payload = extract_json_object(reply) if reply else None
            if isinstance(payload, dict):
                query = payload

        # 地点/区域：结构化字段优先，其次从意图里出现的名字里找
        node_id = self._match_node(str(query.get("node") or "").strip() or target_node)
        zone_id = self._match_zone(str(query.get("zone") or "").strip())
        if not node_id and not zone_id:
            node_id = self._match_node_in_text(intent)
            zone_id = self._match_zone_in_text(intent)

        wanted: list[str] = []
        if node_id:
            wanted = [node_id]  # 指定地点：只看那个地点
        elif zone_id:
            wanted = [item.id for item in self.world.nodes_in_zone(zone_id)]
        return {
            "keyword": str(query.get("keyword") or "").strip() or ("" if (node_id or zone_id) else intent),
            "nodes": wanted,
            "node_names": [self._node_name(item) for item in wanted],
            "zone": zone_id,
            "type": str(query.get("type") or "").strip(),
            "limit": query.get("limit") or 3,
        }

    def _match_node(self, text: str) -> str:
        if not text:
            return ""
        key = text.strip()
        for node in self.world.node_map().values():
            if key in (node.id, node.name):
                return node.id
        return ""

    def _match_zone(self, text: str) -> str:
        if not text:
            return ""
        key = text.strip()
        for zone in self.world.zones:
            if key in (zone.id, zone.name):
                return zone.id
        return ""

    def _match_node_in_text(self, text: str) -> str:
        for node in self.world.node_map().values():
            name = node.name or node.id
            if name and name in text:
                return node.id
        return ""

    def _match_zone_in_text(self, text: str) -> str:
        for zone in self.world.zones:
            name = zone.name or zone.id
            if name and name in text:
                return zone.id
        return ""

    async def _run_tool_calls(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
    ) -> None:
        """按配置顺序调用动作挂着的工具，把结果汇总进 ``action``。"""

        names = self.resolve_tools(definition, state.node_id)
        stored = dict(action.get("tool_params") or {})
        texts: list[str] = []
        failed: list[str] = []
        images: list[str] = []
        last_params: dict[str, Any] = {}
        for index, name in enumerate(names):
            params = dict(stored.get(name) or action.get("params") or {})
            if index > 0 and texts:
                # 后面的工具拿前面的结果再补一次参数：需要网址/编号这类信息时不该靠猜
                merged, note = await self.fill_tool_params(
                    state,
                    definition,
                    PlannedAction(
                        type=definition.id,
                        intent=str(action.get("intent") or action.get("content") or ""),
                    ),
                    tool_name=name,
                    params=params,
                    previous_results="\n\n".join(texts),
                )
                if merged:
                    params = {**params, **merged}
                missing = self.missing_params_for(name, params)
                if missing:
                    failed.append(f"{name}：缺少参数 {missing}" + (f"（{note}）" if note else ""))
                    await self._log_event(
                        state,
                        "skip",
                        {
                            "action": definition.id,
                            "note": f"工具「{name}」缺少必填参数 {missing}，这次只调了前面的工具",
                        },
                    )
                    continue
            call = await self._call_tool(definition.id, params, state, tool_name=name)
            if not call.ok and _looks_like_argument_error(call.error):
                # 工具作者把参数写成"可选"、实现里却必须要：拿报错再补一次，只重试一次
                retried, note = await self.fill_tool_params(
                    state,
                    definition,
                    PlannedAction(
                        type=definition.id,
                        intent=str(action.get("intent") or action.get("content") or ""),
                    ),
                    tool_name=name,
                    params=params,
                    error_hint=str(call.error or ""),
                )
                if retried:
                    retry_params = {**params, **retried}
                    again = await self._call_tool(
                        definition.id, retry_params, state, tool_name=name
                    )
                    await self._log_event(
                        state,
                        "tool",
                        {
                            "action": definition.id,
                            "tool": again.tool or name,
                            "params": dict(again.params or retry_params),
                            "ok": bool(again.ok),
                            "result": _clip_text(again.text, 400),
                            "error": again.error,
                            "note": "按报错补了一次参数" + (f"：{note}" if note else ""),
                        },
                    )
                    if again.ok or again.text:
                        call = again
                        params = retry_params
            sent = dict(call.params or params)
            if not last_params:
                last_params = sent
            if call.ok and call.text:
                texts.append(str(call.text))
            elif not call.ok:
                failed.append(f"{call.tool or name}：{call.error or '没有返回结果'}")
            images.extend(getattr(call, "image_urls", None) or [])
            state.add_event(
                "tool",
                {
                    "action": definition.id,
                    "tool": call.tool or name,
                    "ok": bool(call.ok),
                    "params": sent,
                    "error": call.error,
                },
            )
            await self._log_event(
                state,
                "tool",
                {
                    "action": definition.id,
                    "tool": call.tool or name,
                    "params": sent,
                    "ok": bool(call.ok),
                    "result": _clip_text(call.text, 400),
                    "error": call.error,
                },
            )

        action["tool_names"] = list(names)
        action["tool_name"] = names[-1] if names else ""
        # 只有图片也算拿到结果：不然"生成一张图"这类工具会被当成失败
        action["tool_ok"] = bool(texts or images)
        action["tool_error"] = "；".join(failed)
        if images:
            action["tool_images"] = images
        if texts:
            action["tool_result"] = "\n\n".join(texts)
        elif last_params:
            action.setdefault("params", last_params)

    async def _start_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
        depth: int,
        autonomous: bool,
        *,
        from_plan: bool = False,
    ) -> None:
        # 这里是所有动作真正开始执行的唯一入口（计划 / 日程 / 自主行为 / 工具后续都走这里），
        # 因此把「能不能做」的校验统一放在这里，避免某条路径绕过检查。
        if not definition.available_in(state.node_id):
            outcome.notes.append(
                f"动作 {definition.id} 在 {state.node_id} 不可用，已跳过"
            )
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": f"「{definition.id}」在 {state.node_id} 不可用"},
            )
            return
        ok, reason = self._check_preconditions(state, definition)
        if not ok:
            outcome.notes.append(f"动作 {definition.id} 前置条件不满足（{reason}），已跳过")
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": f"「{definition.id}」前置条件不满足：{reason}",
                },
            )
            return
        if definition.llm_level == "tool":
            if not await self._prepare_tool_action(state, definition, action, outcome):
                return
        if definition.id == "share" and not self._share_allowed(state):
            outcome.notes.append("超过每小时分享上限，跳过分享")
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": "超过每小时分享上限"},
            )
            return

        if definition.category == "continuous":
            duration_ticks = self._duration_ticks(definition, action, state)
            if duration_ticks <= 0:
                duration_ticks = 1
            payload: dict[str, Any] = {
                "type": definition.id,
                "params": dict(action.params),
                "intent": action.intent,
                "target": action.target,
                "target_node": action.target_node,
                "content": action.content,
                "duration_ticks": duration_ticks,
                "elapsed_ticks": 0,
                "interruptible": bool(definition.interruptible),
                "visible": bool(definition.visible),
                "depth": depth,
                "persona_id": "",
                "from_plan": bool(from_plan),
                "desc": definition.name or definition.id,
            }
            if definition.id == "walk_to":
                target = action.target_node or action.target
                if target in self.world.node_map():
                    graph = self.world.adjacent()
                    path = find_path(graph, state.node_id, target)
                    if path is None:
                        outcome.notes.append(f"无法从 {state.node_id} 走到 {target}")
                        return
                    payload["duration_ticks"] = max(1, path_ticks(graph, path))
                    payload["target_node"] = target
                    payload["desc"] = f"正在走去{self.world.node_map()[target].name}"
                else:
                    outcome.notes.append(f"找不到目标 {target}，移动取消")
                    return
            state.current_action = payload
            if definition.during.state:
                state.state = definition.during.state
            state.add_event("action_start", {"type": definition.id})
            await self._log_event(
                state,
                "action_start",
                {
                    "type": definition.id,
                    "duration_ticks": duration_ticks,
                    "target_node": payload.get("target_node") or "",
                    "params": dict(action.params or {}),
                },
            )
            if (
                definition.llm_level == "template"
                and str(definition.template or "").strip()
                and self._action_speaks(definition)
            ):
                outcome.messages.append(
                    self.render_template(definition.template, state, node, action)
                )
            if definition.id in ("search_web", "check_weather"):
                outcome.notes.append(f"开始 {definition.id}")
            return

        # 瞬时动作
        if definition.id == "recall":
            # 主动回忆：翻记忆 → 带着结果再问她一次（她只需要在续说里讲话）
            await self._run_recall(state, node, outcome, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.id in self.SCHEDULE_ACTION_IDS:
            # 日程三件套：同样"做完再问她一次"，她只负责说话
            await self._run_schedule_action(state, node, outcome, definition, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.id == "poke":
            # 戳一戳：能戳就戳（群里什么都没说也看得出来她在闹），戳不了就退化成一句文案
            await self._run_poke(state, node, outcome, definition, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.llm_level == "command":
            # 指令触发：拼一条指令交给 AstrBot 跑，再把结果交回给她
            await self._run_command_action(state, node, outcome, definition, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.llm_level == "tool":
            # 瞬时工具动作也要真的调工具：当场调用 → 就着结果说一句。
            # from_plan 交回下面统一处理，避免计划被推进两次。
            payload: dict[str, Any] = {
                "type": definition.id,
                "params": dict(action.params),
                "tool_params": dict(getattr(action, "tool_params", {}) or {}),
                "intent": action.intent,
                "target": action.target,
                "content": action.content,
                "elapsed_ticks": 0,
                "duration_ticks": 0,
                "from_plan": False,
                "desc": definition.name or definition.id,
            }
            await self._finish_action(state, node, outcome, payload)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return

        messages = await self._instant_output(state, node, definition, action)
        if definition.id == "share":
            if messages:
                outcome.messages.extend(messages)
                self._count_share(state)
        elif self._action_speaks(definition):
            outcome.messages.extend(messages)
        elif definition.id == "think" and action.content:
            state.add_thought(action.content)
            self.memory.remember(
                session_id=state.session_id,
                persona_id="",
                node_id=state.node_id,
                content=action.content,
                memory_type=INNER,
                emotion=state.mood,
                weight=0.35,
                affect=state.affect,
            )
        self.dynamics.apply_effects(state, definition.on_complete.effects, world=self.world)
        state.add_event("action", {"type": definition.id, "visible": definition.visible})
        await self._log_event(
            state,
            "action",
            {
                "type": definition.id,
                "visible": bool(definition.visible),
                "messages": list(messages),
                "content": action.content,
                "target": action.target,
                "params": dict(action.params or {}),
            },
        )

        # 计划推进
        if from_plan and state.current_plan is not None:
            advance(state)
            await self._tick_plan(state, node, outcome, depth=depth + 1)

    # 「想事情」这类内容型动作：产出的文字是内心活动，永远不发到群里
    INNER_ACTIONS = ("think",)

    def _action_speaks(self, definition: ActionDef) -> bool:
        """这个动作产出的文本要不要发到群里。

        「动作会发到群里」这个开关藏得比较深，新建动作时默认还是关的，于是出现过
        两次同款事故：单轮动作让大模型写的话、模板动作写好的文案，都只进了日志，
        群里什么都看不到。所以判定改成看"这个动作有没有话要说"：

        - 内心活动类（想事情）永远不发，它的产出只进内心活动与记忆；
        - 模板写了文案 → 这句话本来就是给群里看的，发；
        - 单轮动作 → 它的定义就是"让大模型说一句"，发；
        - 目标写着「群 / 某个群友」的 → 也是明确想说话，发；
        - 剩下的（没有文案、纯空间动作）保持静默。
        """

        if definition.visible:
            return True
        if definition.id in self.INNER_ACTIONS:
            return False
        if definition.llm_level == "template":
            return bool(str(definition.template or "").strip())
        if definition.llm_level == "single":
            return True
        return definition.target_type in ("group", "user")

    async def _instant_output(
        self,
        state: WorldState,
        node: NodeDef | None,
        definition: ActionDef,
        action: PlannedAction,
    ) -> list[str]:
        """瞬时动作的输出文本。"""

        if definition.id == "say":
            if action.messages:
                return list(action.messages)[: self.world.limits.max_messages_per_say]
            if action.interject:
                return await self._generate_text_actions(
                    state,
                    node,
                    "群里正在聊上面「最近群里在聊」那些内容。"
                    "如果你确实有话想接，就自然地接一句（不要逐条复述、不要总结、不要点名批评）；"
                    "如果说不出什么，就返回空的动作列表，保持安静。",
                    allow_fallback=False,
                )
            return await self._generate_text_actions(
                state,
                node,
                "你现在想和群里的人说点什么。用一个符合你此刻状态和人设的短句说出来，"
                "自然、口语化、不要解释设定。",
                allow_fallback=True,
            )
        if definition.id == "share":
            if not self._share_allowed(state):
                return []
            if action.messages:
                return list(action.messages)[: self.world.limits.max_messages_per_say]
            return await self._generate_text_actions(
                state,
                node,
                "你想和大家分享一件此刻的小事。用 1~2 条简短自然的消息说出来。",
            )
        if definition.llm_level == "template":
            text = self.render_template(definition.template, state, node, action)
            return [text] if text else []
        if action.content:
            return [action.content]
        # 「单轮」动作：这一轮没有现成台词（日程、计划里的步骤都没有），
        # 就让大模型按动作语义现写一句——不然只会发一句机械文案。
        if definition.llm_level == "single" and self.llm is not None:
            generated = await self._generate_text_actions(
                state, node, self._single_action_instruction(state, definition, action)
            )
            if generated:
                return generated
        # 没配模型、发言额度用完、或者它没说出什么：退化成一句动作文案
        label = definition.name or definition.id
        target_name = self._target_name(state, action.target)
        if action.target and target_name:
            return [
                self.render_template(f"（{{bot}}{label}了 {{user}}）", state, node, action)
            ]
        return [self.render_template(f"（{{bot}}{label}）", state, node, action)]

    def _single_action_instruction(
        self, state: WorldState, definition: ActionDef, action: PlannedAction
    ) -> str:
        """「单轮」动作没现成台词时，给大模型的一句交代。"""

        label = definition.name or definition.id
        who = self._target_name(state, action.target)
        lines = [
            f"你刚刚做了「{label}」这个动作。",
            f"这个动作是什么意思：{definition.description or label}。",
        ]
        if action.intent and action.intent.strip() not in ("", label):
            lines.append(f"你当时的想法：{action.intent.strip()}")
        if who:
            lines.append(f"对象是 {who}。")
        lines.append(
            "用你自己的口吻说一句发到群里的话：短、自然、像随口说的，"
            "不要解释设定、不要分点、不要写成在招呼全场。"
        )
        return "\n".join(lines)

    @staticmethod
    def _target_name(state: WorldState, target: str) -> str:
        if not target:
            return ""
        record = state.user_presence.get(target)
        if record and record.get("name"):
            return str(record["name"])
        return target

    @staticmethod
    def _target_display(state: WorldState, target: str) -> str:
        """写进文案里的称呼：认得出名字就用名字，认不出就别把 QQ 号写进句子里。"""

        if not target:
            return ""
        record = state.user_presence.get(target)
        if record and record.get("name"):
            return str(record["name"])
        return ""

    async def _run_poke(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
    ) -> None:
        """戳一戳：调平台接口戳对方一下；平台不支持就退化成一句动作文案。"""

        target = str(action.target or "").strip()
        if not target:
            # 没给目标：挑一个最近说过话的人，谁在就戳谁
            recent = state.recent_active_users(limit=1)
            target = str(recent[0].get("user_id") or "") if recent else ""

        ok = False
        reason = ""
        poker = getattr(self.messenger, "poke", None)
        if not target:
            reason = "不知道要戳谁（群里还没有人说话）"
        elif not callable(poker):
            reason = "当前发送通道不支持戳一戳"
        else:
            try:
                result = await poker(state.session_id, target)
                ok = bool(getattr(result, "ok", result))
                reason = str(getattr(result, "reason", "") or "")
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"

        if ok:
            outcome.notes.append(f"戳了 {self._target_name(state, target)}")
        else:
            text = self.render_template(definition.template, state, node, action)
            if text:
                outcome.messages.append(text)
        self.dynamics.apply_effects(
            state, definition.on_complete.effects, world=self.world
        )
        state.add_event("action", {"type": "poke", "visible": bool(outcome.messages)})
        await self._log_event(
            state,
            "action",
            {
                "type": "poke",
                "target": target,
                "target_name": self._target_name(state, target),
                "ok": ok,
                "note": "戳了一戳" if ok else f"没戳成，改用文案：{reason}",
            },
        )

    def render_template(
        self,
        text: str,
        state: WorldState,
        node: NodeDef | None,
        action: PlannedAction,
    ) -> str:
        """渲染动作模板里的占位符：{bot} 她自己、{user} 目标群友、{node} 当前地点。"""

        if not text:
            return ""
        bot = (
            (self.world.bot_name or "").strip()
            or state.bot_base_nickname
            or pronounce_for(self.world.gender)
        )
        user = self._target_display(state, action.target) or "你"
        result = (
            text.replace("{bot}", bot)
            .replace("{user}", user)
            .replace("{node}", (node.name if node else "") or "")
        )
        return result

    async def _run_chain(
        self,
        state: WorldState,
        chain: list[Any],
        outcome: TickOutcome,
        *,
        depth: int,
    ) -> None:
        """执行日程动作链：瞬时动作立即执行；持续动作启动后，剩余步骤转为计划。"""

        for index, step in enumerate(chain or []):
            action = PlannedAction(
                type=str(getattr(step, "type", "") or ""),
                messages=list(getattr(step, "messages", []) or []),
                target=str(getattr(step, "target", "") or ""),
                target_node=str(getattr(step, "target_node", "") or ""),
                content=str(getattr(step, "content", "") or ""),
                params=dict(getattr(step, "params", {}) or {}),
                duration=int(getattr(step, "duration", 0) or 0),
            )
            definition = self.world.action_map().get(action.type)
            if definition is None:
                outcome.notes.append(f"日程里的动作 {action.type} 不存在，已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {"action": action.type, "note": f"日程里的动作「{action.type}」不存在"},
                )
                continue
            if not definition.enabled:
                # 停用 = 当作根本没有这个动作（日程里排到的这一步也跳过）
                outcome.notes.append(f"日程动作 {action.type} 已停用，已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": action.type,
                        "note": f"日程的这一步「{definition.name or action.type}」已停用",
                    },
                )
                continue
            ok, reason = self._check_preconditions(state, definition)
            if not ok:
                outcome.notes.append(
                    f"日程动作 {action.type} 前置条件不满足（{reason}），已跳过"
                )
                # 日程被跳过的原因必须进事件日志，否则日志页看不出"她为什么没做这件事"。
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": action.type,
                        "note": f"日程的这一步「{definition.name or action.type}」前置条件不满足：{reason}",
                    },
                )
                continue
            if definition.category == "continuous":
                await self._start_action(
                    state, self.node(state.node_id), outcome, definition, action, depth, True
                )
                rest = chain[index + 1 :]
                if rest and state.current_action is not None:
                    remaining = [
                        {
                            "action": str(getattr(item, "type", "") or ""),
                            "target_node": str(getattr(item, "target_node", "") or ""),
                            "target": str(getattr(item, "target", "") or ""),
                            "duration": int(getattr(item, "duration", 0) or 0),
                            "content": str(getattr(item, "content", "") or ""),
                            "intent": str(getattr(item, "intent", "") or ""),
                            "messages": list(getattr(item, "messages", []) or []),
                            "params": dict(getattr(item, "params", {}) or {}),
                        }
                        for item in rest
                    ]
                    state.current_plan = create_plan(
                        steps=remaining,
                        world_time=state.world_time,
                        valid_for=self.world.limits.plan_valid_duration,
                        reason="日程后续步骤",
                        source="schedule",
                    )
                return
            await self._execute_actions(
                state,
                self.node(state.node_id),
                outcome,
                [action],
                depth=depth,
                autonomous=True,
            )

    async def _generate_text_actions(
        self,
        state: WorldState,
        node: NodeDef | None,
        instruction: str,
        *,
        allow_fallback: bool = False,
    ) -> list[str]:
        """用 LLM 生成要说的话（say / share 没有现成文案时）。"""

        if self.llm is None:
            return [self._fallback_text()] if allow_fallback else []
        if not self._llm_text_allowed(state):
            return [self._fallback_text()] if allow_fallback else []
        persona_text = await self._persona_text(state.session_id)
        system_prompt = self.prompts.build_autonomous_system_prompt(
            persona_text=persona_text,
            state=state,
            node=node,
            available_tools=self.available_tools(),
            engagement_hint=self.engagement.hint(state),
            max_messages=self.world.limits.max_messages_per_say,
            recent_chat=self.chat_context(state),
            reasoning=bool(self.world.reasoning_enabled),
        )
        prompt = instruction + '\n只输出 JSON：{"actions":[{"type":"say","messages":["..."]}]}'
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        self._count_llm_text(state)
        if not reply:
            return [self._fallback_text()] if allow_fallback else []
        result = parse_action_payload(
            reply,
            available_actions={"say"},
            max_actions=1,
            max_messages=self.world.limits.max_messages_per_say,
        )
        for item in result.actions:
            if item.type == "say" and item.messages:
                return item.messages
        cleaned = reply.strip()
        return [cleaned] if cleaned else []

    def _duration_ticks(
        self, definition: ActionDef, action: PlannedAction, state: WorldState
    ) -> int:
        """把动作时长换算成 tick。

        - ``duration_mode=fixed``：用配置的固定秒数（大模型给的 duration 不生效）；
        - ``duration_mode=llm``：用大模型给的秒数，并夹在 duration_min~duration_max 之间
          （大模型没给就用下限，这样"小睡多久"这类决定权交给模型，同时有上限兜底）。
        """

        if definition.duration_mode == "llm":
            low = int(definition.duration_min or 60)
            high = int(definition.duration_max or max(low, int(definition.duration or low * 4)))
            if high < low:
                high = low
            seconds = int(action.duration or 0) or low
            seconds = min(max(seconds, low), high)
        else:
            seconds = action.duration or definition.duration or 0
        return max(1, int(round(seconds / max(1.0, self.tick_seconds))))

    async def _call_tool(
        self,
        action_id: str,
        params: dict[str, Any],
        state: WorldState,
        tool_name: str = "",
    ) -> ToolCallResult:
        """调用工具，返回结构化结果（成功文本 / 失败原因）。"""

        if self.tools is None:
            return ToolCallResult(ok=False, error="没有可用的工具通道")
        chosen = self.resolve_tool(action_id, state.node_id, tool_name)
        if not chosen:
            return ToolCallResult(ok=False, error="没有解析到可用工具")
        try:
            result = await self.tools.call_tool(
                chosen, dict(params or {}), state.session_id
            )
        except Exception as exc:
            self._log("warning", f"工具 {chosen} 调用失败: {exc}")
            return ToolCallResult(ok=False, error=str(exc), tool=chosen)
        if isinstance(result, ToolCallResult):
            if not result.tool:
                result.tool = chosen
            if not result.params:
                result.params = dict(params or {})
            return result
        # 兼容返回纯字符串的实现
        text = str(result or "").strip()
        if not text:
            return ToolCallResult(
                ok=False, error="工具返回了空结果", tool=chosen, params=dict(params or {})
            )
        return ToolCallResult(ok=True, text=text, tool=chosen, params=dict(params or {}))

    async def _ask_llm(
        self,
        session_id: str,
        system_prompt: str,
        prompt: str,
        contexts: list[dict[str, Any]] | None = None,
        image_urls: list[str] | None = None,
    ) -> str | None:
        def note(ok: bool, error: str = "") -> None:
            self._last_llm[session_id] = {
                "at": self._now(),
                "ok": bool(ok),
                "error": str(error)[:200],
            }

        if self.llm is None:
            note(False, "没有可用的模型")
            return None
        try:
            reply = await self.llm.generate(
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                contexts=list(contexts) if contexts else None,
                image_urls=list(image_urls) if image_urls else None,
            )
        except Exception as exc:
            self._log("warning", f"LLM 调用失败: {exc}")
            note(False, f"{type(exc).__name__}: {exc}")
            return None
        if reply is None or not getattr(reply, "ok", True):
            note(False, getattr(reply, "error", "") or "模型没有返回内容")
            return None
        note(True)
        return getattr(reply, "text", "") or ""

    async def _persona_text(self, session_id: str) -> str:
        if self.persona is None:
            return ""
        try:
            return await self.persona.get_persona_text(session_id)
        except Exception:
            return ""

    # ================= 打断 / 唤醒 =================

    async def _ask_creator(self, session_id: str, system_prompt: str, prompt: str) -> str | None:
        """内容生成模型：只在编辑器里批量生成动作/地点时调用。"""

        if self.creator_llm is None:
            return None
        try:
            reply = await self.creator_llm.generate(
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.8,
            )
        except Exception as exc:
            self._log("warning", f"内容生成模型调用失败: {exc}")
            return None
        if reply is None or not getattr(reply, "ok", True):
            return None
        return getattr(reply, "text", "") or ""

    def _generator_session_id(self) -> str:
        """生成时借一个会话 id 去取人格（没有启用会话就用空字符串）。"""

        ids = self.enabled_session_ids()
        return ids[0] if ids else ""

    async def generate_actions_for_zone(
        self, zone_id: str, per_node: Any = 0, node_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """按区域里的**每个地点**批量生成动作草稿（不落库，交给编辑器预览勾选）。

        只传了 ``node_ids`` 时就只为这几个地点生成（编辑器里"只给这个地点生成"）。
        """

        zone = self.world.zone_map().get(zone_id)
        if zone is None:
            return {"actions": [], "problems": ["找不到这个区域"]}
        inside = self.world.nodes_in_zone(zone_id)
        if node_ids:
            wanted = {str(item) for item in node_ids if str(item)}
            nodes = [node for node in inside if node.id in wanted]
        else:
            nodes = inside
        if not nodes:
            return {
                "actions": [],
                "problems": ["这个区域里还没有可用地点"],
            }
        count = clamp_per_node(per_node)
        session_id = self._generator_session_id()
        system_prompt, prompt = self.prompts.build_action_generator_prompt(
            persona_text=await self._persona_text(session_id),
            zone_name=zone.name or zone.id,
            zone_note=zone.note,
            nodes=[
                {"id": node.id, "name": node.name, "prompt": node.prompt or node.atmosphere.describe()}
                for node in nodes
            ],
            existing_actions=[action.name or action.id for action in self.world.actions],
            tools=sorted(self.available_tools()),
            per_node=count,
        )
        reply = await self._ask_creator(session_id, system_prompt, prompt)
        if reply is None:
            return {
                "actions": [],
                "problems": ["生成模型没有返回内容（检查插件配置里的「内容生成模型」）"],
            }
        actions, problems = parse_generated_actions(
            reply,
            node_ids=[node.id for node in nodes],
            tool_names=set(self.available_tools()),
        )
        return {"actions": actions, "problems": problems, "raw": _clip_text(reply, 1200)}

    async def generate_nodes_for_zone(
        self, zone_id: str, count: Any = 0
    ) -> dict[str, Any]:
        """给区域批量生成地点草稿：自动摆位 + 按方案 A 自动连线（同样不落库）。"""

        zone = self.world.zone_map().get(zone_id)
        if zone is None:
            return {"nodes": [], "edges": [], "problems": ["找不到这个区域"]}
        wanted = clamp_node_count(count)
        session_id = self._generator_session_id()
        inside = self.world.nodes_in_zone(zone_id)
        system_prompt, prompt = self.prompts.build_node_generator_prompt(
            persona_text=await self._persona_text(session_id),
            zone_name=zone.name or zone.id,
            zone_note=zone.note,
            existing_nodes=[node.name or node.id for node in inside],
            count=wanted,
        )
        reply = await self._ask_creator(session_id, system_prompt, prompt)
        if reply is None:
            return {
                "nodes": [],
                "edges": [],
                "problems": ["生成模型没有返回内容（检查插件配置里的「内容生成模型」）"],
            }
        nodes, problems = parse_generated_nodes(
            reply,
            existing_ids={node.id for node in self.world.nodes},
            max_nodes=wanted,
        )
        positions = auto_layout(
            [(float(node.x), float(node.y)) for node in inside], len(nodes)
        )
        for node, (x, y) in zip(nodes, positions):
            node["zone_id"] = zone_id
            node["x"], node["y"] = x, y
        # 方案 A：第一个新地点连到区域内最近的既有地点，之后依次串成链
        anchor = ""
        if inside and positions:
            anchor = min(
                inside,
                key=lambda node: (float(node.x) - positions[0][0]) ** 2
                + (float(node.y) - positions[0][1]) ** 2,
            ).id
        pairs = link_plan([node["id"] for node in nodes], anchor=anchor)
        edges = [
            {
                "id": f"e_{src}_{dst}",
                "from": src,
                "to": dst,
                "ticks": 1,
                "bidirectional": True,
            }
            for src, dst in pairs
        ]
        return {
            "nodes": nodes,
            "edges": edges,
            "problems": problems,
            "raw": _clip_text(reply, 1200),
        }

    async def set_values(
        self, session_id: str, values: dict[str, Any]
    ) -> dict[str, float]:
        """手改她的数值（编辑器「实时状态」用）：只认已知字段，自动夹到 0~1，并记一条日志。"""

        allowed = ("energy", "loneliness", "curiosity", "affect", "boredom")
        applied: dict[str, float] = {}
        async with self.session_state(session_id) as state:
            for key, value in (values or {}).items():
                if key not in allowed:
                    continue
                try:
                    number = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    continue
                setattr(state, key, number)
                applied[key] = round(number, 4)
            if applied:
                state.mood = self.dynamics.derive_mood(state)
                await self._log_event(state, "manual", {"values": applied})
        return applied

    async def apply_cancel(
        self,
        state: WorldState,
        mode: str,
        user_text: str,
        outcome: TickOutcome,
    ) -> str:
        """按模型给出的 ``cancel`` 停掉她的手头安排。

        - ``now``：立刻停手（尊重「可打断」标记），并放弃剩下的安排；
        - ``queue``：手上这件做完，但不按原计划继续了。

        模型既然判断要终止，就照做——否则会出现"嘴上说不干了、身体还在接着干"。
        ``user_text`` 只用来记日志（谁说了什么让她停的）。返回做了什么（空串 = 什么都没做）。
        """

        if mode not in CANCEL_MODES:
            return ""

        action = state.current_action if isinstance(state.current_action, dict) else None
        had_plan = isinstance(state.current_plan, dict) and bool(
            state.current_plan.get("steps")
        )
        stopped = ""
        blocked = ""
        if mode == "now" and action is not None:
            if action.get("interruptible", True):
                stopped = str(action.get("type") or "")
                state.current_action = None
                state.state = STATE_IDLE
            else:
                # 标了「不可打断」的动作（例如睡觉）不做一半就扔，但剩下的安排照样放弃
                blocked = str(action.get("type") or "")
        state.current_plan = None

        parts = []
        if stopped:
            definition = self.world.action_map().get(stopped)
            label = (definition.name or definition.id) if definition else stopped
            parts.append(f"停掉了「{label}」")
        if blocked:
            definition = self.world.action_map().get(blocked)
            label = (definition.name or definition.id) if definition else blocked
            parts.append(f"「{label}」不可打断，做完这件就停")
        if had_plan:
            parts.append("放弃了还没做的安排")
        if not parts:
            parts.append("本来就没在忙")
        note = "、".join(parts)
        outcome.notes.append(f"要她停下：{note}")
        await self._log_event(
            state,
            "cancel",
            {
                "mode": mode,
                "stopped": stopped,
                "blocked": blocked,
                "plan": had_plan,
                "by": str(user_text or "")[:60],
                "note": note,
            },
        )
        return note

    async def interrupt(self, session_id: str, *, force: bool = False) -> bool:
        """用户消息打断当前持续动作。返回是否真的打断了。"""

        if not self.is_enabled(session_id):
            return False
        async with self.session_state(session_id) as state:
            action = state.current_action
            if not isinstance(action, dict):
                return False
            if not force and not action.get("interruptible", True):
                return False
            state.add_event("interrupt", {"action": action.get("type")})
            state.current_action = None
            state.state = STATE_IDLE
            return True

    async def clear_plan(self, session_id: str) -> bool:
        """放弃还没做的安排（编辑器按钮与 /vw stop 用）。返回是否有东西被清掉。"""

        if not self.is_enabled(session_id):
            return False
        async with self.session_state(session_id) as state:
            plan = state.current_plan
            if not isinstance(plan, dict) or not plan.get("steps"):
                return False
            state.current_plan = None
            await self._log_event(
                state, "cancel", {"mode": "queue", "plan": True, "note": "放弃了还没做的安排"}
            )
            return True

    async def refresh_nickname(self, session_id: str) -> dict[str, Any]:
        """重新从平台读一次她现在的群名片（编辑器「重新获取」按钮用）。"""

        if not self.is_enabled(session_id):
            return {"ok": False, "card": "", "note": "这个会话没有启用虚拟世界"}
        card = ""
        try:
            card = str(await self.messenger.fetch_group_card(session_id) or "").strip()
        except Exception as exc:
            self._log("warning", f"读取群名片失败：{exc}")
        async with self.session_state(session_id) as state:
            before = state.bot_current_nickname
            if card:
                state.bot_current_nickname = card
                if not state.bot_base_nickname:
                    # 之前没记过原名，就把这次读到的当成原名
                    state.bot_base_nickname = card
                state.last_nickname_update_at = self._now()
            note = "" if card else "还没收到过这个群的消息，拿不到机器人句柄"
            await self._log_event(
                state,
                "nickname",
                {"manual": True, "to": card, "ok": bool(card), "note": note},
            )
        return {"ok": bool(card), "card": card, "before": before, "note": note}

    async def clear_all_states(self) -> int:
        """清掉所有会话的世界状态（切换预设时用）。记忆与日志不动。"""

        cleared = 0
        for session_id in list(self.state_session_ids()):
            try:
                await self.db.call("delete_state", session_id)
                cleared += 1
            except Exception as exc:
                self._log("warning", f"清状态失败 {session_id}: {exc}")
        self._event_ids.clear()
        self._last_decider_at.clear()
        return cleared

    def state_session_ids(self) -> list[str]:
        """有世界状态的会话 id。"""

        try:
            return [
                str(item)
                for item in self.db.raw.list_state_ids()
                if str(item)
            ]
        except Exception:
            return []

    async def wake_up(self, session_id: str) -> bool:
        if not self.is_enabled(session_id):
            return False
        async with self.session_state(session_id) as state:
            was_sleeping = state.is_sleeping
            state.current_action = None
            state.state = STATE_IDLE
            if state.world_time < state.mood_override_until:
                state.mood_override_until = 0
            return was_sleeping

    # ================= 限额 =================

    def _hour_index(self, state: WorldState) -> int:
        return int(state.world_time * self.tick_seconds // 3600)

    def _count_autonomous(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.autonomous_hour_marker != hour:
            state.autonomous_hour_marker = hour
            state.autonomous_count_hour = 0
        state.autonomous_count_hour += 1

    def _within_hourly_limit(self, state: WorldState) -> bool:
        hour = self._hour_index(state)
        if state.autonomous_hour_marker != hour:
            return True
        return state.autonomous_count_hour < self.world.limits.max_autonomous_per_hour

    def _arrival_decision_allowed(self, state: WorldState) -> bool:
        """「走到新地方」这类决策有独立的每小时上限（不占自主行动额度）。"""

        hour = self._hour_index(state)
        if state.arrival_hour_marker != hour:
            return True
        limit = max(0, int(self.world.limits.max_arrival_decisions_per_hour))
        return state.arrival_count_hour < limit

    def _count_arrival_decision(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.arrival_hour_marker != hour:
            state.arrival_hour_marker = hour
            state.arrival_count_hour = 0
        state.arrival_count_hour += 1

    def _count_share(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.share_hour_marker != hour:
            state.share_hour_marker = hour
            state.share_count_hour = 0
        state.share_count_hour += 1

    def _share_allowed(self, state: WorldState) -> bool:
        hour = self._hour_index(state)
        if state.share_hour_marker != hour:
            return True
        return state.share_count_hour < self.world.limits.max_share_per_hour

    # ---------------- LLM 预算 ----------------

    def _llm_plan_allowed(self, state: WorldState) -> bool:
        """是否还允许问 LLM 要计划（间隔 + 每小时次数双重限制）。"""

        limits = self.world.limits
        if self._now() - float(state.last_llm_plan_at or 0.0) < max(
            0, int(limits.llm_plan_min_interval_seconds)
        ):
            return False
        hour = self._hour_index(state)
        if state.llm_plan_hour_marker != hour:
            return True
        return state.llm_plan_count_hour < max(0, int(limits.max_llm_plan_per_hour))

    def _count_llm_plan(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.llm_plan_hour_marker != hour:
            state.llm_plan_hour_marker = hour
            state.llm_plan_count_hour = 0
        state.llm_plan_count_hour += 1
        state.last_llm_plan_at = self._now()

    def _llm_text_allowed(self, state: WorldState) -> bool:
        limits = self.world.limits
        hour = self._hour_index(state)
        if state.llm_text_hour_marker != hour:
            return True
        return state.llm_text_count_hour < max(0, int(limits.max_llm_text_per_hour))

    def _count_llm_text(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.llm_text_hour_marker != hour:
            state.llm_text_hour_marker = hour
            state.llm_text_count_hour = 0
        state.llm_text_count_hour += 1

    def _fallback_text(self) -> str:
        import random

        return random.choice(TEXT_FALLBACKS)

    # ---------------- 群聊上下文 / 插话 ----------------

    def chat_context(self, state: WorldState) -> list[dict[str, Any]]:
        """进提示词的最近群聊（时间窗 + 条数双重限制）。

        注意：这里只裁剪「带进提示词」的那份视图，原始留档由 :meth:`note_presence`
        按 `context.chat_history_max` 维护，所以重启后还能恢复。
        """

        config = self.world.decider
        return state.recent_chat_within(
            now=self._now(),
            seconds=max(60, int(config.chat_window_minutes) * 60),
            limit=max(1, int(config.chat_max_messages)),
            # 已经回应过的消息不再回放：她对那些话已经答过了，再带进去只会重复回应
            after=float(state.chat_replied_until or 0.0),
        )

    def mark_chat_replied(self, state: WorldState) -> None:
        """她真的回了一句：把"已回应水位线"推到当前，并清掉上一轮的背景句。"""

        state.chat_replied_until = self._now()

    async def mark_chat_replied_by_session(self, session_id: str) -> None:
        """按会话推进"已回应水位线"（注入模式下主人格替她回复时用）。"""

        if not self.is_enabled(session_id):
            return
        async with self.session_state(session_id) as state:
            self.mark_chat_replied(state)

    def note_chat_note(self, state: WorldState, text: str) -> None:
        """记下「刚才在聊什么」（模型顺手写的），下一轮当背景用。"""

        note = " ".join(str(text or "").split())
        if note:
            state.chat_note = _clip_text(note, 80)

    def group_is_chatting(self, state: WorldState) -> bool:
        """群里最近是否真的有人在聊。"""

        need = max(1, int(self.world.decider.min_messages_to_interject))
        return len(self.chat_context(state)) >= need

    @staticmethod
    def plan_speaks(plan: dict[str, Any] | None) -> bool:
        """这份计划里有没有"她主动开口"的动作。"""

        for step in (plan or {}).get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("interject"):
                return True
            if str(step.get("action") or "") in ("say", "share"):
                return True
        return False

    def willingness(self, state: WorldState) -> float:
        """她对「现在开口」的整体意愿（0~1）：给决策器与外部联动共用。"""

        return reply_willingness(state)

    # ---------------- 群聊上下文：留档与压缩 ----------------

    def chat_history_limit(self) -> int:
        return max(20, int(self.world.context.chat_history_max))

    def chat_summary_text(self, state: WorldState) -> str:
        return str(state.chat_summary or "").strip()

    async def clear_chat_context(self, session_id: str) -> dict[str, Any]:
        """清空这个会话的群聊留档与摘要（调试用：想看她"第一次听到"的反应时很有用）。"""

        async with self.session_state(session_id) as state:
            removed = len(state.recent_chat)
            had_summary = bool(str(state.chat_summary or "").strip())
            state.recent_chat = []
            state.chat_summary = ""
            state.chat_summary_at = 0.0
            await self._log_event(
                state,
                "context",
                {"note": "手动清空群聊上下文", "removed": removed},
            )
        return {"removed": removed, "had_summary": had_summary}

    async def _maybe_compress_chat(
        self, state: WorldState, outcome: TickOutcome
    ) -> None:
        """留档攒到阈值时，把较早的部分交给压缩模型压成一段摘要。"""

        config = self.world.context
        if str(config.chat_overflow) != "compress":
            return
        history = list(state.recent_chat)
        threshold = max(10, int(config.chat_compress_threshold))
        if len(history) < threshold:
            return
        refresh = max(60, int(config.summary_refresh_minutes)) * 60
        if self._now() - float(state.chat_summary_at or 0.0) < refresh:
            return
        keep = max(1, int(config.chat_keep_after_compress))
        older = history[: max(0, len(history) - keep)]
        if not older:
            return
        summary = await self._summarize_chat(state, older)
        if not summary:
            return
        state.chat_summary = summary
        state.chat_summary_at = self._now()
        state.recent_chat = history[-keep:]
        outcome.notes.append(f"已把较早的 {len(older)} 条群聊压成摘要")
        await self._log_event(
            state,
            "context",
            {"note": "压缩较早群聊", "compressed": len(older), "kept": keep},
        )

    async def _summarize_chat(
        self, state: WorldState, history: list[dict[str, Any]]
    ) -> str:
        if self.context_llm is None or not history:
            return ""
        limit = max(40, int(self.world.context.history_max_chars))
        lines = []
        for item in history:
            who = "她" if item.get("is_self") else (item.get("name") or item.get("user_id") or "")
            text = _clip_text(item.get("text"), limit)
            if text:
                lines.append(f"- {who}: {text}")
        if not lines:
            return ""
        previous = self.chat_summary_text(state)
        system_prompt = (
            "你负责把群聊记录压缩成简短的背景摘要。"
            "保留：谁在聊、聊了什么话题、有没有提到群里那位虚拟角色、有没有还没回应的事。"
            "不要逐条复述，不要评价，不要编造。只输出摘要本身，不超过 200 字。"
        )
        prompt = (
            (f"已有的更早摘要：\n{previous}\n\n" if previous else "")
            + "需要压缩的群聊记录：\n"
            + "\n".join(lines)
            + "\n\n请输出更新后的摘要。"
        )
        try:
            reply = await self.context_llm.generate(
                session_id=state.session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.2,
            )
        except Exception as exc:
            self._log("debug", f"上下文压缩失败：{exc}")
            return ""
        if not getattr(reply, "ok", False):
            return ""
        return str(reply.text or "").strip()[:600]

    def interject_allowed(self, state: WorldState) -> bool:
        """是否允许主动插话：插话冷却 + 无人回应冷却 + 每小时上限。"""

        if not self.world.decider.enabled:
            return False
        cooldown = max(0, int(self.world.decider.interject_cooldown_minutes)) * 60
        if self._now() - float(state.last_interject_at or 0.0) < cooldown:
            return False
        return self.engagement.can_speak(state) and self._within_hourly_limit(state)

    def _forced_plan_allowed(self, state: WorldState) -> bool:
        """极端保护也要守规矩：间隔、每小时上限、无人回应冷却一个都不能少。"""

        limits = self.world.limits
        if self._now() - float(state.last_forced_plan_at or 0.0) < max(
            0, int(limits.forced_plan_min_interval_seconds)
        ):
            return False
        if not self._within_hourly_limit(state):
            return False
        if not self.engagement.can_speak(state):
            return False
        return True

    # ================= 群名片 =================

    async def _sync_nickname(self, state: WorldState, node: NodeDef | None) -> None:
        config = self.world.nickname_sync
        if not config.enabled or self.messenger is None:
            return
        if ":GroupMessage:" not in state.session_id:
            return  # 私聊没有群名片
        if state.bot_nickname_locked:
            return
        # 第一次遇到这个会话时，先把她当前的名片读回来当"原名"
        if not state.bot_base_nickname:
            try:
                fetched = await self.messenger.fetch_group_card(state.session_id)
            except Exception as exc:
                self._log("debug", f"读取群名片失败: {exc}")
                fetched = ""
            if fetched:
                state.bot_base_nickname = fetched
                state.bot_current_nickname = state.bot_current_nickname or fetched
                await self._log_event(
                    state, "nickname", {"base": fetched, "note": "记下了她原来的群名片"}
                )
        desired = compute_nickname(self.world, state, node, base=state.bot_base_nickname)
        now = self._now()
        # 连续失败时按倍数退避（1× → 2× → 4× …，最多 1 小时一次），
        # 否则协议端一挂，这里会每隔一个冷却就重试并打一条错误日志。
        cooldown = max(10, int(config.cooldown_seconds))
        backoff = cooldown * (2 ** min(int(state.nickname_fail_count or 0), 6))
        if self._messenger_blocked(state.session_id) or state.nickname_fail_count:
            backoff = max(backoff, 300)
        if not should_update(
            state,
            desired,
            now=now,
            cooldown_seconds=min(backoff, 3600),
        ):
            return
        try:
            result = await self.messenger.set_group_card(state.session_id, desired)
        except Exception as exc:
            result = CardResult(ok=False, reason=str(exc), card=desired)
        if result.ok:
            state.bot_current_nickname = desired
            state.last_nickname_update_at = now
            state.nickname_fail_count = 0
            await self._log_event(state, "nickname", {"to": desired, "ok": True})
            return
        outcome_reason = result.reason or "没有说明原因"
        state.last_nickname_update_at = now  # 失败也占一次冷却，避免每 tick 重试
        state.nickname_fail_count = min(int(state.nickname_fail_count or 0) + 1, 8)
        await self._log_event(
            state, "nickname", {"to": desired, "ok": False, "note": outcome_reason}
        )
        # 只在第一次失败时打日志，后面交给退避；（事件日志里每一条都还在，方便排查）
        if state.nickname_fail_count == 1:
            self._log(
                "debug",
                f"群名片没改成：{outcome_reason}（接下来会按 5 分钟起退避重试）",
            )

    # ================= 发送 =================

    def _messenger_blocked(self, session_id: str) -> bool:
        """发送方是否正处在"刚失败"的冷却里（宿主适配器不一定实现这个方法）。"""

        if self.messenger is None:
            return False
        checker = getattr(self.messenger, "blocked", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(session_id))
        except Exception:
            return False

    async def _deliver(self, outcome: TickOutcome) -> None:
        if self.messenger is None:
            return
        said = [m for m in outcome.messages if m and str(m).strip()]
        echoed = self.take_pending_echo(outcome.session_id) + [
            m for m in outcome.debug_messages if m and str(m).strip()
        ]
        if not said and not echoed:
            return
        state = await self.load_state(outcome.session_id, cold_start=False)
        if not self.engagement.can_speak(state):
            outcome.notes.append("冷却期内不发送")
            return
        async with self.session_state(outcome.session_id) as state:
            if said:
                self.engagement.on_bot_spoke(state)
                await self._log_event(state, "bot_message", {"messages": said})
                # 自己说过的话也进聊天上下文：模型才知道"刚才那句是我说的"，
                # 并被要求换一种说法，避免每轮都用同一个句式开场。
                for message in said:
                    state.note_chat(
                        user_id="__self__",
                        name=state.bot_current_nickname or state.bot_base_nickname or "你",
                        text=message,
                        now=self._now(),
                        keep=self.chat_history_limit(),
                        is_self=True,
                    )
                    self.note_dialogue(state, text=message, is_self=True)
            # 调试用的动作回显不算"她说过的话"：不进聊天上下文、不计无人回应保护
        sent = await self.messenger.send_text(outcome.session_id, said + echoed)
        note = getattr(self.messenger, "take_fail_note", None)
        failed_note = note(outcome.session_id) if callable(note) else ""
        if failed_note:
            # 没发出去这件事也要留痕：日志里能看到"这条她说过、但平台没收"
            async with self.session_state(outcome.session_id) as state:
                await self._log_event(
                    state,
                    "send_failed",
                    {"messages": said + echoed, "note": failed_note},
                )
        elif not sent and said:
            outcome.notes.append("平台没有接收这批消息")

    # ================= 对外快照 =================

    # ---------------- 编辑器「实时状态」用的几个小查询 ----------------

    WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    def _clock_text(self) -> str:
        """现实时间 + 时段。用的是提示词同一套写法，免得两边说法不一致。"""

        try:
            return clock_text(self.local_now())
        except Exception:
            return ""

    @staticmethod
    def _duration_text(seconds: Any) -> str:
        """把秒数说成人话（世界时间对照那一行用）。"""

        try:
            total = max(0, int(round(float(seconds))))
        except (TypeError, ValueError):
            return ""
        days, rest = divmod(total, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        if days:
            return f"{days} 天 {hours} 小时"
        if hours:
            return f"{hours} 小时 {minutes} 分"
        return f"{minutes} 分钟"

    def _next_schedule(self, session_id: str) -> dict[str, Any] | None:
        """下一条要触发的日程：时间、还有多少分钟、里面有哪些动作。"""

        items = list(getattr(self.schedules, "schedules", None) or [])
        if not items:
            return None
        try:
            now = self.local_now()
        except Exception:
            return None
        action_map = self.world.action_map()
        best: dict[str, Any] | None = None
        for item in items:
            if not item.enabled:
                continue
            sessions = list(item.sessions or [])
            if sessions and session_id not in sessions:
                continue
            try:
                hour, minute = (int(part) for part in str(item.time).split(":")[:2])
            except (TypeError, ValueError):
                continue
            for offset in range(0, 8):
                moment = (now + timedelta(days=offset)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if moment <= now:
                    continue
                weekday = self.WEEKDAY_KEYS[moment.weekday()]
                if item.days and weekday not in item.days:
                    continue
                minutes = max(0, int(round((moment - now).total_seconds() / 60)))
                if best is None or minutes < int(best["in_minutes"]):
                    names = []
                    for step in item.action_chain or []:
                        definition = action_map.get(step.type)
                        names.append(
                            (definition.name or definition.id) if definition else step.type
                        )
                    best = {
                        "id": item.id,
                        "time": item.time,
                        "in_minutes": minutes,
                        "weekday": WEEKDAY_NAMES[moment.weekday()],
                        "actions": " → ".join(names),
                        "auto_travel": bool(item.auto_travel),
                    }
                break
        return best

    def _budget(self, state: WorldState) -> dict[str, dict[str, int]]:
        """本小时还剩多少额度（跨小时自动归零，和运行时用的是同一套计数）。"""

        hour = self._hour_index(state)
        limits = self.world.limits

        def quota(count: Any, marker: Any, limit: Any) -> dict[str, int]:
            total = max(0, int(limit))
            used = int(count) if int(marker or 0) == hour else 0
            return {"left": max(0, total - used), "used": min(used, total), "limit": total}

        return {
            "plan": quota(
                state.llm_plan_count_hour, state.llm_plan_hour_marker, limits.max_llm_plan_per_hour
            ),
            "text": quota(
                state.llm_text_count_hour, state.llm_text_hour_marker, limits.max_llm_text_per_hour
            ),
            "tool_param": quota(
                state.tool_param_count_hour,
                state.tool_param_hour_marker,
                limits.max_tool_param_per_hour,
            ),
            "share": quota(
                state.share_count_hour, state.share_hour_marker, limits.max_share_per_hour
            ),
            "autonomous": quota(
                state.autonomous_count_hour,
                state.autonomous_hour_marker,
                limits.max_autonomous_per_hour,
            ),
        }

    def _channel_status(self, session_id: str) -> dict[str, Any]:
        """发送通道与最近一次模型调用的状态（排查"她怎么不说话了"用）。"""

        blocked_seconds = 0
        try:
            getter = getattr(self.messenger, "blocked_seconds", None)
            if callable(getter):
                blocked_seconds = int(getter(session_id) or 0)
            elif self.messenger is not None and self.messenger.blocked(session_id):
                blocked_seconds = -1
        except Exception:
            blocked_seconds = 0
        record = dict(self._last_llm.get(session_id) or {})
        if record:
            record["ago_seconds"] = max(0, int(self._now() - float(record.get("at", 0.0))))
        return {
            "send_blocked_seconds": blocked_seconds,
            "last_llm": record,
            # 空串 = 跟随 AstrBot 当前会话的供应商
            "llm_provider": str(getattr(self.llm, "provider_id", "") or ""),
            "has_llm": self.llm is not None,
        }

    async def snapshot(self, session_id: str) -> dict[str, Any]:
        state = await self.load_state(session_id, cold_start=False)
        node = self.node(state.node_id)
        zone = self.world.zone_map().get(self.world.zone_of(state.node_id))
        return {
            "session_id": session_id,
            "world_time": state.world_time,
            "tick_seconds": self.tick_seconds,
            "node_id": state.node_id,
            "node_name": node.name if node else "",
            "state": state.state,
            "mood": state.mood,
            "values": self.dynamics.values(state),
            "current_action": state.current_action,
            "current_plan": state.current_plan,
            "last_reasoning": state.last_reasoning,
            "thoughts": state.thoughts[-5:],
            "unanswered_count": state.unanswered_count,
            "cooldown_until": state.cooldown_until,
            # 决策意愿，以及它换算出来的「问大模型」概率（编辑器里显示，方便调参）
            "willingness": round(reply_willingness(state), 4),
            "llm_sample_rate": round(self.decider.llm_sample_rate(state), 4),
            "recent_events": state.recent_events[-10:],
            # 群聊上下文：留档条数 + 摘要（编辑器里能看到、也能一键清空）
            "chat_history_count": len(state.recent_chat),
            "chat_unreplied_count": len(self.chat_context(state)),
            "chat_replied_count": max(
                0,
                len(state.recent_chat) - len(self.chat_context(state)),
            ),
            "chat_summary": str(state.chat_summary or ""),
            "chat_note": str(state.chat_note or ""),
            "nickname": state.bot_current_nickname or state.bot_base_nickname,
            "zone_id": zone.id if zone is not None else "",
            "zone_name": zone.name if zone is not None else "",
            "clock_text": self._clock_text(),
            "world_elapsed_seconds": int(state.world_time * self.tick_seconds),
            "world_elapsed_text": self._duration_text(state.world_time * self.tick_seconds),
            "next_schedule": self._next_schedule(session_id),
            "budget": self._budget(state),
            "channel": self._channel_status(session_id),
            # 名片是否被锁住（编辑器里用一个按钮切换，所以要能读到当前状态）
            "nickname_locked": bool(state.bot_nickname_locked),
            "nickname_base": state.bot_base_nickname,
            "user_presence": list(state.user_presence.values())[:20],
            # 从当前位置能直达哪里、各需要多少 tick（给编辑器和调试看）
            "travel": [
                {
                    "node_id": target,
                    "name": (
                        self.node(target).name if self.node(target) is not None else target
                    ),
                    "ticks": ticks,
                }
                for target, ticks in sorted(
                    self.world.adjacent().get(state.node_id, []),
                    key=lambda item: item[1],
                )
            ],
        }

    async def preview_injection(self, session_id: str, persona_id: str = "") -> str:
        """预览注入模式会写进主人格的文本（只读，不改变任何状态）。"""

        state = await self.load_state(session_id, cold_start=False)
        node = self.node(state.node_id)
        memories = self.memory.recall(
            session_id=session_id,
            persona_id=persona_id,
            node_id=state.node_id,
            limit=self.world.limits.max_think_memory,
        )
        return self.prompts.build_injection(
            state,
            node=node,
            memories=memories,
            engagement_hint=self.engagement.hint(state),
            recent_chat=self.chat_context(state),
        )

    async def overview(self) -> list[dict[str, Any]]:
        """所有启用会话的简要状态：编辑器地图页一次看全"谁在哪"。"""

        result: list[dict[str, Any]] = []
        for session_id in self.enabled_session_ids():
            try:
                state = await self.load_state(session_id, cold_start=False)
            except Exception:
                continue
            node = self.node(state.node_id)
            result.append(
                {
                    "session_id": session_id,
                    "node_id": state.node_id,
                    "node_name": node.name if node is not None else state.node_id,
                    "state": state.state,
                    "mood": state.mood,
                    "world_time": state.world_time,
                }
            )
        return result

    async def preview_autonomous_prompt(self, session_id: str, persona_id: str = "") -> str:
        """预览接管模式会发给 LLM 的 system prompt（只读）。"""

        state = await self.load_state(session_id, cold_start=False)
        node = self.node(state.node_id)
        return self.prompts.build_autonomous_system_prompt(
            persona_text=await self._persona_text(session_id),
            state=state,
            node=node,
            available_tools=self.available_tools(),
            memories=self.memory.recall(
                session_id=session_id,
                persona_id=persona_id,
                node_id=state.node_id,
                limit=self.world.limits.max_think_memory,
            ),
            engagement_hint=self.engagement.hint(state),
            max_messages=self.world.limits.max_messages_per_say,
            recent_chat=self.chat_context(state),
            reasoning=bool(self.world.reasoning_enabled),
        )

    async def _event_marker(self, state: WorldState) -> int:
        """本轮的起点事件 id。

        第一次用到（插件刚启动 / 刚重载）时要从库里取当前最大 id——不能默认 0，
        否则「把新事件发到群里」会把历史上最后 50 条日志当成新事件重放一遍，
        变成每个 tick 往群里刷一串老消息。
        """

        known = self._event_ids.get(state.session_id)
        if known is not None:
            return int(known)
        latest = 0
        try:
            rows = await self.db.call(
                "query_events", session_id=state.session_id, limit=1
            )
            if rows:
                latest = int(rows[0].get("id") or 0)
        except Exception:
            latest = 0
        self._event_ids[state.session_id] = latest
        return latest

    def echo_types(self) -> set[str]:
        """「调试输出」勾选的事件类型（只认认识的 key，空集合 = 关闭）。"""

        return {
            str(name).strip()
            for name in (self.world.echo_types or [])
            if str(name).strip() in ECHO_EVENT_TYPES
        }

    def _queue_echo(self, session_id: str, lines: list[str]) -> None:
        """回复路径之外的调试回显先存着，等下一次发送时一起带出去。

        （被叫醒、睡觉门禁这类事件发生在"还没决定要不要回复"之前，
        没法挂在某一轮回复的 outcome 上。）
        """

        if not lines:
            return
        self._pending_echo.setdefault(session_id, []).extend(
            line for line in lines if line
        )

    def take_pending_echo(self, session_id: str) -> list[str]:
        """取走并清空攒下的回显（发送方负责带到群里）。"""

        return self._pending_echo.pop(session_id, [])

    async def _echo_events_since(
        self, state: WorldState, outcome: TickOutcome | None, marker: int
    ) -> None:
        """把这一轮新产生的事件渲染成消息（只处理「调试输出」里勾选了的类型）。"""

        if outcome is None:
            return
        enabled = self.echo_types()
        if not enabled:
            return
        try:
            events = await self.db.call(
                "query_events", session_id=state.session_id, limit=50
            )
        except Exception:
            return
        fresh = [item for item in events if int(item.get("id") or 0) > marker]
        # 处理过就把游标推到最后一条：不然这一批会在下一个 tick 再发一遍
        # （tick 没有新事件时游标不会自己前进，这就是"同一句话一直刷"的来源）。
        self._event_ids[state.session_id] = max(
            [marker] + [int(item.get("id") or 0) for item in events]
        )
        # query_events 是新到旧；一次 tick 里最多回显最新的这么多条，
        # 再按时间正序发出去（连锁动作多的时候不要把群刷了）
        for item in reversed(fresh[:12]):
            kind = str(item.get("event_type") or "")
            detail = item.get("detail")
            if not isinstance(detail, dict):
                detail = {}
            if not _echo_payload(kind, detail, enabled):
                continue
            try:
                line = render_event(item, self.world)
            except Exception:
                continue
            if line:
                outcome.debug_messages.append(f"{ECHO_EVENT_TYPES[kind]} {line}")

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        if level == "debug" and not self.debug:
            return
        method = getattr(self.logger, level, None)
        if callable(method):
            method(f"[virtual_world] {message}")

    async def _log_event(
        self,
        state: WorldState,
        event_type: str,
        detail: dict[str, Any] | None = None,
        *,
        persona_id: str = "",
        outcome: TickOutcome | None = None,
    ) -> None:
        """写一条事件日志（编辑器的「日志」页会渲染成人话），并顺手做数量裁剪。

        传入 ``outcome`` 且开启了「把动作发到群里」时，同一行文本也会作为消息发出去。
        """

        payload = detail or {}
        enabled = self.echo_types()
        if (
            outcome is not None
            and enabled
            and _echo_payload(event_type, payload, enabled)
        ):
            try:
                line = render_event(
                    {
                        "event_type": event_type,
                        "detail": payload,
                        "world_time": state.world_time,
                    },
                    self.world,
                )
            except Exception:
                line = ""
            if line:
                outcome.debug_messages.append(
                    f"{ECHO_EVENT_TYPES.get(event_type, '•')} {line}"
                )

        try:
            new_id = await self.db.call(
                "add_event",
                session_id=state.session_id,
                persona_id=persona_id,
                world_time=state.world_time,
                event_type=event_type,
                detail=detail or {},
            )
            if new_id:
                self._event_ids[state.session_id] = int(new_id)
        except Exception as exc:  # 日志写失败不能影响主流程
            self._log("debug", f"写事件日志失败：{exc}")
            return
        count = self._event_writes.get(state.session_id, 0) + 1
        self._event_writes[state.session_id] = count
        if count % 100 == 0:
            try:
                await self.db.call(
                    "prune_session_events",
                    state.session_id,
                    int(self.world.limits.max_log_events),
                )
            except Exception:
                pass


_ARGUMENT_ERROR_MARKERS = (
    "required positional argument",
    "unexpected keyword argument",
    "missing 1 required",
    "required argument",
    "got an unexpected keyword",
    "positional argument",
    "参数",
)


def _looks_like_argument_error(error: Any) -> bool:
    """这次失败像不像"参数给少了 / 给错了"。

    很多第三方工具的 schema 写着参数可选，实现却必须要；调用当场报
    ``missing 1 required positional argument``。这种错值得带回去重新补一次参数。
    """

    text = str(error or "").lower()
    if not text:
        return False
    return any(marker in text for marker in _ARGUMENT_ERROR_MARKERS)


_SCHEMA_RESERVED_KEYS = {
    "type",
    "properties",
    "required",
    "title",
    "description",
    "additionalProperties",
    "$schema",
    "definitions",
    "$defs",
}


def normalize_param_schema(schema: Any) -> dict[str, Any]:
    """把各家工具写的参数描述统一成 ``{"properties": {...}, "required": [...]}``。

    AstrBot 里工具的参数定义至少有四种写法，真实插件里全都遇到过：

    - 标准 JSON Schema：``{"type": "object", "properties": {...}, "required": [...]}``；
    - 省掉外层、直接给键值表：``{"city": {"description": "地点"}}``；
    - 参数列表：``[{"name": "city", "description": "地点", "required": true}]``；
    - 属性值是字符串：``{"city": "地点，例如杭州"}``。

    统一之后，"哪些参数要填、哪些是必填"就有唯一答案了。
    """

    if isinstance(schema, list):
        properties: dict[str, Any] = {}
        required: list[str] = []
        for item in schema:
            if isinstance(item, str):
                properties[item] = {}
                continue
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("key") or "").strip()
            if not name:
                continue
            properties[name] = {
                "type": str(item.get("type") or "string"),
                "description": str(item.get("description") or ""),
            }
            if item.get("required"):
                required.append(name)
        return {"properties": properties, "required": required}

    if not isinstance(schema, dict):
        return {"properties": {}, "required": []}

    raw_properties = schema.get("properties")
    if not isinstance(raw_properties, dict):
        # 没有 properties 时，把顶层那些"不是 schema 关键字"的键当成参数表
        guessed = {
            str(key): value
            for key, value in schema.items()
            if key not in _SCHEMA_RESERVED_KEYS
        }
        raw_properties = guessed if guessed else {}

    properties = {}
    required = {str(name) for name in (schema.get("required") or []) if str(name)}
    for name, spec in raw_properties.items():
        key = str(name)
        if isinstance(spec, str):
            properties[key] = {"description": spec}
            continue
        if not isinstance(spec, dict):
            properties[key] = {}
            continue
        properties[key] = dict(spec)
        # 有的工具把"必填"写在属性自己身上
        if spec.get("required") is True:
            required.add(key)
    return {"properties": properties, "required": sorted(required)}


def render_param_text(schema: dict[str, Any]) -> str:
    """把工具自带的参数 schema 渲染成一行中文说明（提示词与编辑器共用）。"""

    normalized = normalize_param_schema(schema)
    properties = normalized.get("properties") or {}
    required = set(normalized.get("required") or [])
    if not properties:
        return "（这个工具不需要参数）"
    parts: list[str] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            spec = {}
        desc = str(spec.get("description") or "").strip()
        flag = "必填" if name in required else "可选"
        text = f"{name}（{flag}"
        if desc:
            text += f"，{desc}"
        text += "）"
        parts.append(text)
    return "、".join(parts)


def _clip_text(value: Any, limit: int = 200) -> str:
    """把任意值压成单行短文本，用于事件日志。"""

    if value is None:
        return ""
    text = str(value).strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"





