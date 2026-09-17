"""运行时状态对象（每个会话独立）。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

# bot_state 取值
STATE_IDLE = "idle"
STATE_AWAKENING = "awakening"
STATE_SLEEPING = "sleeping"
STATE_NAPPING = "napping"
STATE_STARING = "staring"
STATE_SEARCHING = "searching"
STATE_READING = "reading"
STATE_WALKING = "walking"
STATE_THINKING = "thinking"

# 同一个人在这段时间内发的内容差不多，就认为是同一条消息（两个钩子重复记一次）
_CHAT_DEDUPE_SECONDS = 60.0


def _clamp01(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(0.0, min(1.0, number))


def _same_chat_text(left: str, right: str) -> bool:
    """两条聊天文本算不算"同一句"。

    两个钩子拿到的文本可能一个带 @ 前缀、一个被别的插件改写过，
    所以去掉空白和标点后比较，并且容忍一方是另一方的子串。
    """

    def squeeze(text: str) -> str:
        return "".join(ch for ch in str(text or "") if ch.isalnum())

    a = squeeze(left)
    b = squeeze(right)
    if not a or not b:
        return not a and not b
    if a == b:
        return True
    return len(min(a, b, key=len)) >= 4 and (a in b or b in a)


def chat_item_is_fresh(
    item: dict[str, Any], *, replied_until: float = 0.0, replied_seq: int = 0
) -> bool:
    """这条群聊是不是"她还没回应过的"。

    优先用**序号**判断：时间戳的精度只有秒，她刚说完的同一秒里进来的消息会被
    误判成"已经回应过"。序号不够用时（老存档、内存里造的测试数据）退回时间比较，
    没有时间的则一律当成新消息——宁可多给她看，也别把话藏起来。
    """

    seq = item.get("seq")
    if seq is not None and int(replied_seq or 0) > 0:
        try:
            return int(seq) > int(replied_seq)
        except (TypeError, ValueError):
            pass
    at = float(item.get("at") or 0.0)
    if at <= 0:
        return True
    return at > float(replied_until or 0.0)


@dataclass
class WorldState:
    """一个会话的完整运行时状态。"""

    session_id: str
    world_time: int = 0
    node_id: str = ""
    state: str = STATE_IDLE
    mood: str = "平静"
    energy: float = 0.6
    loneliness: float = 0.5
    curiosity: float = 0.5
    affect: float = 0.3
    """心潮：情绪被激起的程度（0~1）。

    越高，她的内心活动越激烈、说出来的话情绪越浓、越容易做出亲昵或冲动的举动；
    接近 0 时偏平淡克制。由互动（被夸、被抱、吵架、被冷落）抬高，随时间回落。
    """

    valence: float = 0.5
    """效价：心情的好坏（0~1，0.5 是中性）。= 基线 + 偏移。

    基线由五维与时段推导（本身不存历史），偏移随事件产生、随时间归零。
    心潮管"有多激动"，效价管"激动成什么样"。
    """

    valence_offset: float = 0.0
    """效价的短期偏移：真正带历史的那个量（-0.5 ~ 0.5，0 = 回到基线）。"""

    affect_synced_at: float = 0.0
    """上一次结算情绪两维的时间（现实时间戳）。

    情绪两轴按真实时间衰减：事件到来前先补上这一段，所以 tick 长度改了、
    或者宿主卡顿了，都不会改变曲线形状。
    """

    storm: bool = False
    """「正在气头上」标记：高心潮 + 负效价，带滞回，用于提示词与安全阀。"""

    storm_since: float = 0.0
    """标记是从什么时候开始的（现实时间戳，用来兜底最长持续时间）。"""

    last_style_cell: str = ""
    """最近一次回复用的表达格（例如 excited+negative）：日志与状态页用来看映射有没有生效。"""

    last_say_limit: int = 0
    """那一次实际生效的句数上限（风格格、群聊硬顶、配置、密度提醒取更严格者）。"""

    interject_stats: dict[str, int] = field(default_factory=dict)
    """本小时「她想插话但被拦住」的计数：哪道闸在限流，一眼就能看出来。"""

    interject_hour_marker: int = 0
    """上面那份计数的所属小时。"""

    interject_closed_until: float = 0.0
    """「想被注意到」这条插话动机被关到什么时候（现实时间戳）。

    长期低落时关掉它，并保证至少关 15 分钟：否则效价一恢复就立刻打开，
    看起来像个开关。
    """

    low_valence_since: float = 0.0
    """效价持续偏低从什么时候开始（安全阀用）。"""

    ignored_streak: int = 0
    ignored_at: float = 0.0
    """被冷落的连续次数与时间：惩罚递减，隔一阵重新算（见 dynamics.ignored_magnitude）。"""

    boredom: float = 0.3
    current_action: dict[str, Any] | None = None
    current_plan: dict[str, Any] | None = None
    last_plan: dict[str, Any] = field(default_factory=dict)
    """最近一次生成的计划（做完了也留着，提示词里会带上，免得她每轮重新打算）。"""
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    thoughts: list[dict[str, Any]] = field(default_factory=list)
    last_reasoning: dict[str, Any] = field(default_factory=dict)
    """最近一次「推理草稿」（env/state/mood/who/intent + 时间戳）。

    只用于编辑器展示与排查：她刚才看到了什么、打算怎么做。
    不外发、不计动作数、不写记忆。
    """

    recent_chat: list[dict[str, Any]] = field(default_factory=list)
    """最近群聊内容（持久化，重启后仍在）。带上限，按配置决定丢弃还是压缩。"""

    pending_images: list[dict[str, Any]] = field(default_factory=list)
    """自上次回复以来收到的图片（没配转述模型时，会把它们直接交给多模态主模型）。

    只存地址和时间，回复一次就清空；上限由「全局设置 → 上下文 → 图片上限」决定。
    """

    chat_summary: str = ""
    """较早群聊的压缩摘要（`chat_overflow = compress` 时才会有内容）。"""

    chat_summary_at: float = 0.0
    """上次压缩摘要的时间戳。"""

    chat_replied_until: float = 0.0
    """「已回应水位线」：这个时间点之前的群聊不再进"最近在聊"（留档仍然保留）。"""

    chat_note: str = ""
    """上一次回复时顺手写下的一句「刚才在聊什么」，只作为下一轮的话题背景。"""

    chat_preview: str = ""
    """她刚回应过的那一批群聊的概览（谁说了什么、她回了什么）。

    每次她开口后由规则生成，下一轮作为"之前的群聊"背景出现——
    这些内容已经回应过，所以不再原样重复给她，只留一条概览。
    """

    recent_replies: list[str] = field(default_factory=list)
    """她最近说过的几句话（给"别用同样的句式"那一段用）。"""

    chat_seq: int = 0
    """群聊留档的递增序号（时间戳只有秒，判断"谁是新消息"不够用）。"""

    chat_replied_seq: int = 0
    """已回应水位线（序号版）：大于它的群聊才是"还没回应过的"。"""

    user_presence: dict[str, dict[str, Any]] = field(default_factory=dict)
    bot_base_nickname: str = ""
    bot_current_nickname: str = ""
    bot_nickname_locked: bool = False
    nickname_fail_count: int = 0
    """群名片连续失败次数：用来做退避（挂掉的协议端不会把日志刷爆）。"""
    unanswered_count: int = 0
    last_engagement_time: int = 0
    awaiting_reply: bool = False
    cooldown_until: int = 0
    proactive_block_until: int = 0
    """在这个 tick 之前不要主动开口（刚回过话之后的冷却）。被动回复不受影响。"""
    mood_override_until: int = 0
    last_user_activity_at: float = 0.0
    last_nickname_update_at: float = 0.0
    sleep_reply_at: float = 0.0
    sleep_skip_at: float = 0.0
    """上一次记录「她在睡觉所以没回」的时间（用来给日志节流）。"""

    sleep_skip_count: int = 0
    """距离上次记录又挡下了几条消息。"""
    """上次用「她睡着了」的固定文案回话的时间（冷却用，0 表示没回过）。"""
    no_sleep_until: int = 0
    """刚被叫醒的保护期：世界时间在此之前，规则不再安排她回去睡。"""
    cold_start_done: bool = False
    pending_memory: list[dict[str, Any]] = field(default_factory=list)
    """还没总结的对话片段：攒够条数、聊完、或她离开这个地点时，压成一条记忆。"""
    pending_memory_node: str = ""
    """上面那些片段发生在哪个地点。"""
    memory_hints: list[str] = field(default_factory=list)
    """大模型顺手给的一句话总结，作为写记忆时的提示。"""
    memory_flush_at: float = 0.0
    """上次写对话记忆的时间。"""
    memory_flush_wanted: bool = False
    """硬触发（换地点）标记：下一次 tick 立刻把这段总结掉。"""
    wake_note: str = ""
    """刚被叫醒时给提示词的一句说明（下一次回复用完即弃，见 wake_note_until）。"""
    wake_note_until: int = 0
    """上面那句说明的有效期（世界时间）。"""
    low_energy_since: int = 0
    high_loneliness_since: int = 0
    autonomous_count_hour: int = 0
    autonomous_hour_marker: int = 0
    share_count_hour: int = 0
    share_hour_marker: int = 0
    llm_plan_count_hour: int = 0
    llm_plan_hour_marker: int = 0
    llm_text_count_hour: int = 0
    llm_text_hour_marker: int = 0
    tool_param_count_hour: int = 0
    tool_param_hour_marker: int = 0
    arrival_count_hour: int = 0
    arrival_hour_marker: int = 0
    pending_arrival: bool = False
    """刚从别的地方走到这里（tick 循环会就地做一次决策）。"""
    last_llm_plan_at: float = 0.0
    last_forced_plan_at: float = 0.0
    last_forced_flag: str = ""
    last_interject_at: float = 0.0
    schedule_cursor: float = 0.0
    """日程检查游标（现实时间戳）。只处理「游标之后、现在之前」到点的日程。

    以前是拿 `HH:MM` 精确匹配当前这一分钟，tick 一旦漂移就会整分钟跳过去、
    那一条日程当天再也不会触发；改成游标之后，跳过的分钟会在下一次检查时补上。
    """

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---------------- 序列化 ----------------

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None, session_id: str) -> "WorldState":
        data = dict(payload or {})
        # 旧存档里的字段名是 social（社交欲），现在改叫 affect（心潮）
        if "affect" not in data and "social" in data:
            data["affect"] = data["social"]
        data["session_id"] = session_id
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        state = cls(**filtered)  # type: ignore[arg-type]
        state.clamp()
        if not isinstance(state.current_action, dict):
            state.current_action = None
        if not isinstance(state.current_plan, dict):
            state.current_plan = None
        if not isinstance(state.last_plan, dict):
            state.last_plan = {}
        if not isinstance(state.last_reasoning, dict):
            state.last_reasoning = {}
        return state

    def note_reasoning(self, reasoning: dict[str, Any] | None, *, source: str = "") -> None:
        """记下最近一次推理草稿（编辑器「实时状态」会显示它）。"""

        if not reasoning:
            return
        payload = {str(k): str(v) for k, v in dict(reasoning).items() if str(v).strip()}
        if not payload:
            return
        payload["_source"] = source
        payload["_at"] = int(self.world_time)
        self.last_reasoning = payload

    # ---------------- 便捷方法 ----------------

    def clamp(self) -> None:
        self.energy = _clamp01(self.energy, 0.6)
        self.loneliness = _clamp01(self.loneliness, 0.5)
        self.curiosity = _clamp01(self.curiosity, 0.5)
        self.affect = _clamp01(self.affect, 0.3)
        self.valence = _clamp01(self.valence, 0.5)
        try:
            offset = float(self.valence_offset)
        except (TypeError, ValueError):
            offset = 0.0
        self.valence_offset = max(-0.5, min(0.5, offset))
        self.boredom = _clamp01(self.boredom, 0.3)
        self.unanswered_count = max(0, int(self.unanswered_count))
        self.ignored_streak = max(0, int(self.ignored_streak or 0))
        self.storm = bool(self.storm)
        self.nickname_fail_count = max(0, int(self.nickname_fail_count or 0))
        self.world_time = max(0, int(self.world_time))
        self.cooldown_until = max(0, int(self.cooldown_until))
        self.mood_override_until = max(0, int(self.mood_override_until))
        self.no_sleep_until = max(0, int(self.no_sleep_until))
        self.memory_flush_wanted = bool(self.memory_flush_wanted)
        self.wake_note_until = max(0, int(self.wake_note_until))
        if not isinstance(self.interject_stats, dict):
            self.interject_stats = {}
        else:
            self.interject_stats = {
                str(key): max(0, int(value or 0))
                for key, value in self.interject_stats.items()
            }
        self.interject_hour_marker = max(0, int(self.interject_hour_marker or 0))
        if not isinstance(self.recent_replies, list):
            self.recent_replies = []
        else:
            self.recent_replies = [
                " ".join(str(item).split())
                for item in self.recent_replies
                if str(item or "").strip()
            ][-6:]
        if not isinstance(self.pending_memory, list):
            self.pending_memory = []
        else:
            self.pending_memory = [
                item for item in self.pending_memory if isinstance(item, dict)
            ][-60:]
        if not isinstance(self.memory_hints, list):
            self.memory_hints = []
        else:
            self.memory_hints = [str(item) for item in self.memory_hints][-5:]

    # 兼容旧字段名：老版本叫「社交欲（social）」，语义已经换成「心潮（affect）」。
    @property
    def social(self) -> float:
        return self.affect

    @social.setter
    def social(self, value: Any) -> None:
        self.affect = _clamp01(value, 0.3)

    @property
    def is_sleeping(self) -> bool:
        return self.state in (STATE_SLEEPING, STATE_NAPPING)

    @property
    def is_busy(self) -> bool:
        """正在执行不可打断的持续动作。"""

        action = self.current_action or {}
        if not action:
            return False
        return not bool(action.get("interruptible", True))

    @property
    def busy_with(self) -> str:
        return str((self.current_action or {}).get("type", ""))

    def add_event(self, kind: str, detail: dict[str, Any], keep: int = 40) -> None:
        self.recent_events.append(
            {"kind": kind, "detail": detail, "world_time": self.world_time}
        )
        if len(self.recent_events) > keep:
            self.recent_events = self.recent_events[-keep:]

    def add_thought(self, content: str, node_id: str = "", keep: int = 20) -> None:
        self.thoughts.append(
            {"content": content, "node_id": node_id or self.node_id, "world_time": self.world_time}
        )
        if len(self.thoughts) > keep:
            self.thoughts = self.thoughts[-keep:]

    def touch_user(
        self,
        user_id: str,
        *,
        name: str = "",
        anchor: str = "",
        now: float | None = None,
        max_tracked: int = 100,
    ) -> None:
        """更新用户存在感记录。"""

        record = self.user_presence.get(user_id, {})
        record.update(
            {
                "user_id": user_id,
                "name": name or record.get("name", "") or user_id,
                "presence": "active",
                "believed_anchor": anchor or record.get("believed_anchor", "topic_center"),
                "last_seen": now if now is not None else time.time(),
                "world_time": self.world_time,
            }
        )
        self.user_presence[user_id] = record
        if len(self.user_presence) > max_tracked:
            ordered = sorted(
                self.user_presence.items(),
                key=lambda item: float(item[1].get("last_seen", 0)),
                reverse=True,
            )[:max_tracked]
            self.user_presence = dict(ordered)

    def recent_active_users(self, limit: int = 5) -> list[dict[str, Any]]:
        items = sorted(
            self.user_presence.values(),
            key=lambda item: float(item.get("last_seen", 0)),
            reverse=True,
        )
        return items[:limit]

    def note_chat(
        self,
        *,
        user_id: str,
        name: str,
        text: str,
        now: float,
        keep: int = 12,
        is_self: bool = False,
    ) -> None:
        """记录一条群聊内容（用于判断"大家在聊什么"）。"""

        clean = (text or "").strip()
        if not clean:
            return
        # 同一条消息可能同时经过"旁观监听"和"LLM 请求"两个钩子，去重避免上下文里重复出现
        # （两个钩子拿到的文本可能差一点点：一个带 @ 前缀、一个被管线改写过，
        #   所以不是逐字比较，而是"同一个人、短时间内、内容基本一样"。）
        for item in reversed(self.recent_chat[-4:]):
            if str(item.get("user_id")) != str(user_id):
                continue
            if now - float(item.get("at", 0)) > _CHAT_DEDUPE_SECONDS:
                continue
            if _same_chat_text(str(item.get("text") or ""), clean):
                return
            break
        self.recent_chat.append(
            {
                "user_id": user_id,
                "name": name or user_id,
                "text": clean[:200],
                "at": now,
                "world_time": self.world_time,
                "is_self": bool(is_self),
                "seq": self.chat_seq + 1,
            }
        )
        self.chat_seq = int(self.chat_seq) + 1
        if len(self.recent_chat) > keep:
            self.recent_chat = self.recent_chat[-keep:]

    def recent_chat_within(
        self,
        *,
        now: float,
        seconds: float,
        limit: int = 0,
        after: float = 0.0,
        after_seq: int = 0,
    ) -> list[dict[str, Any]]:
        """取时间窗内的聊天记录。

        只影响"带进提示词"的视图，不删原始留档：留档大小由 note_chat 的 keep 控制，
        这样重启、或者某段时间没人说话之后，历史不会因为读一次就消失。
        """

        kept = [
            item
            for item in self.recent_chat
            if now - float(item.get("at", 0)) <= max(0.0, seconds)
            # 水位线只挡「别人说过的、已经回应过的」；她自己说过的话要留着，
            # 下一轮才能要求她「别重复刚才那句」。
            and (
                chat_item_is_fresh(item, replied_until=after, replied_seq=after_seq)
                or item.get("is_self")
            )
        ]
        if limit > 0:
            kept = kept[-limit:]
        return kept

    def chat_window(
        self, *, now: float, seconds: float, limit: int = 0
    ) -> list[dict[str, Any]]:
        """时间窗内的全部聊天（不过滤"已回应过的"）。

        提示词要用这份：水位线以前的内容会被压缩成一条概览、之后的内容原样列出，
        但两边都不该从上下文里消失。
        """

        kept = [
            item
            for item in self.recent_chat
            if now - float(item.get("at", 0)) <= max(0.0, seconds)
        ]
        if limit > 0:
            kept = kept[-limit:]
        return kept

    def note_reply(self, text: str, *, keep: int = 6) -> None:
        """记一句她刚说过的话（提示词里用来提醒她别重复句式）。"""

        clean = " ".join(str(text or "").split())
        if not clean:
            return
        if self.recent_replies and self.recent_replies[-1] == clean:
            return
        self.recent_replies = [*self.recent_replies, clean][-max(1, int(keep)) :]
