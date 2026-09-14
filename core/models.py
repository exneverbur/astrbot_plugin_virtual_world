"""配置数据模型（Pydantic）。

对应设计文档第 3 章：world.json / schedules.json / sessions.json。
模型只负责校验与补全，不做业务逻辑；校验失败时尽量「修好并警告」而不是让插件起不来。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .defaults import DEFAULT_WAKE_WORDS


def _rename_keys(data: Any, mapping: dict[str, str]) -> Any:
    """把旧配置键名换成新的（仅当新键缺失时），用于兼容老 world.json。"""

    if not isinstance(data, dict):
        return data
    result = dict(data)
    for old, new in mapping.items():
        if old in result and new not in result:
            result[new] = result[old]
    return result

Category = Literal["instant", "continuous"]
LLMLevel = Literal["template", "single", "tool", "command"]
ActionScope = Literal["global", "node"]
TargetType = Literal["none", "user", "group"]
MemoryScopeMode = Literal["group", "persona", "group_persona", "global", "node"]
ColdStartMode = Literal["awakening", "silent", "custom"]
Gender = Literal["female", "male", "other"]

PRONOUNS: dict[str, str] = {"female": "她", "male": "他", "other": "ta"}

# 这些动作在引擎里有专门逻辑（说话的出口、寻路、睡眠门禁、内心活动、分享上限），
# 删掉会让整个世界跑不起来，所以只能停用、不能删除。
REQUIRED_BUILTIN_ACTIONS: tuple[str, ...] = (
    "say",
    "walk_to",
    "sleep",
    "think",
    "share",
    "recall",
    "schedule_list",
    "schedule_add",
    "schedule_remove",
)


def _clean_names(names: Any, fallback: Any = "") -> list[str]:
    """整理工具名列表：去空、去重、保序；列表为空时退回单个兼容字段。"""

    result: list[str] = []
    for item in list(names or []):
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    if not result:
        text = str(fallback or "").strip()
        if text:
            result.append(text)
    return result


def pronoun_for(gender: str | None) -> str:
    """把性别映射成称呼：她 / 他 / ta。未知取值按 ta 处理。"""

    return PRONOUNS.get(str(gender or "").strip(), PRONOUNS["other"])


class Permissive(BaseModel):
    """允许未知字段，方便未来扩展与用户手写多余配置。"""

    model_config = ConfigDict(extra="allow")


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


class Atmosphere(Permissive):
    """节点氛围，用于调制内部状态的变化率。"""

    calm: float = 0.5
    intimacy: float = 0.5
    visibility: float = 0.5
    liveliness: float = 0.5
    loneliness: float = 0.0
    curiosity: float = 0.0

    @field_validator("*", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> float:
        return _clamp01(value, 0.5)

    def describe(self) -> str:
        """渲染成人话，用于提示词第 3 层。"""

        parts: list[str] = []
        if self.calm >= 0.7:
            parts.append("安静")
        if self.intimacy >= 0.7:
            parts.append("私密")
        if self.visibility >= 0.7:
            parts.append("容易被大家看到")
        if self.liveliness >= 0.7:
            parts.append("热闹")
        if self.loneliness >= 0.6:
            parts.append("让人有点想人")
        if self.curiosity >= 0.6:
            parts.append("让人想找点新鲜事")
        return "、".join(parts) if parts else "没什么特别的"


class MemoryPreset(Permissive):
    content: str = ""
    scope: MemoryScopeMode = "persona"
    emotion: str = ""
    weight: float = 0.5

    @field_validator("weight", mode="before")
    @classmethod
    def _clamp_weight(cls, value: Any) -> float:
        return _clamp01(value, 0.5)


class NodeDef(Permissive):
    id: str
    name: str = ""
    x: float = 0.0
    y: float = 0.0
    icon: str = ""
    color: str = "#8B7DD8"
    prompt: str = ""
    atmosphere: Atmosphere = Field(default_factory=Atmosphere)
    preset_memories: list[MemoryPreset] = Field(default_factory=list)
    zone_id: str = ""
    """所属区域。区域地图里的房间节点靠它归属；空字符串会在校验时落回默认区域。"""


class ZoneDef(Permissive):
    """区域：世界地图上的一个「节点」，里面装着自己的房间。

    注意它和房间是**两套坐标**：区域节点的 x/y 只在世界地图上有效，
    和区域内房间的坐标互不影响（拖区域不会动房间）。
    """

    id: str
    name: str = ""
    note: str = ""
    """区域说明，会写进提示词（例如「外面人来人往，容易遇到新鲜事」）。"""

    icon: str = ""
    color: str = "#7FB2E5"
    x: float = 0.0
    """世界地图上的位置。"""
    y: float = 0.0


class EdgeDef(Permissive):
    id: str = ""
    from_: str = Field(default="", alias="from")
    to: str = ""
    ticks: int = 1
    bidirectional: bool = True

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @field_validator("ticks", mode="before")
    @classmethod
    def _min_ticks(cls, value: Any) -> int:
        try:
            ticks = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, ticks)

    def endpoints(self, nodes: dict[str, NodeDef]) -> list[tuple[str, str]]:
        """返回这条边实际连通的有向组合（考虑 bidirectional）。默认双向；用户仍可手动删除反向边。"""

        pairs = [(self.from_, self.to)]
        if self.bidirectional:
            pairs.append((self.to, self.from_))
        return [(a, b) for a, b in pairs if a in nodes and b in nodes]


class ZoneEdgeDef(Permissive):
    """跨区连线：一条「门户对」，两端各是某个区域里的一个具体房间。

    例：公园↔商场这条线，对应「公园·北门 ↔ 商场·大门」；公园↔学校那条，
    对应「公园·南门 ↔ 学校·校门口」。同一对区域可以有多条这样的线。
    """

    id: str = ""
    from_zone: str = ""
    to_zone: str = ""
    from_node: str = ""
    to_node: str = ""
    ticks: int = 1
    bidirectional: bool = True

    @field_validator("ticks", mode="before")
    @classmethod
    def _min_ticks(cls, value: Any) -> int:
        try:
            ticks = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, ticks)

    def endpoints(self, nodes: dict[str, NodeDef]) -> list[tuple[str, str]]:
        """实际连通的有向房间对（考虑双向）。"""

        pairs = [(self.from_node, self.to_node)]
        if self.bidirectional:
            pairs.append((self.to_node, self.from_node))
        return [(a, b) for a, b in pairs if a in nodes and b in nodes]

    def touches(self, node_id: str) -> bool:
        return node_id in (self.from_node, self.to_node)

    def other_side(self, node_id: str) -> tuple[str, str]:
        """给一端房间，返回 (另一端所在区域 id, 另一端房间 id)。"""

        if node_id == self.from_node:
            return self.to_zone, self.to_node
        if node_id == self.to_node:
            return self.from_zone, self.from_node
        return "", ""


class ParamDef(Permissive):
    type: str = "string"
    required: bool = False
    description: str = ""
    value: str = ""
    """固定值：填了之后这个参数不再交给辅助模型猜（例如查天气固定 city=武汉）。"""


class OnComplete(Permissive):
    trigger: Literal["none", "llm_followup", "schedule"] = "none"
    prompt_hint: str = ""
    schedule_id: str = ""
    effects: dict[str, str] = Field(default_factory=dict)
    """完成时一次性叠加的效果，支持 "+0.2" / "-0.1" / "=0.9" / "×1.5" / "mood:温柔"。"""

    effects_per_minute: dict[str, str] = Field(default_factory=dict)
    """按「实际持续分钟数」缩放的效果，例如 {"energy": "+0.002"} 表示每持续 1 分钟精力 +0.002。"""


class During(Permissive):
    state: str = ""


class Preconditions(Permissive):
    not_state: list[str] = Field(default_factory=list)
    min_energy: float | None = None


class ActionDef(Permissive):
    id: str
    name: str = ""
    category: Category = "instant"
    llm_level: LLMLevel = "template"
    scope: ActionScope = "global"
    allowed_nodes: list[str] = Field(default_factory=list)
    target_type: TargetType = "none"
    duration: int = 0
    duration_mode: Literal["fixed", "llm"] = "fixed"
    """``fixed``：时长固定为 duration；``llm``：由大模型在 duration_min~duration_max 之间自己决定。"""

    duration_min: int = 0
    """``duration_mode=llm`` 时的最短时长（秒）。"""

    duration_max: int = 0
    """``duration_mode=llm`` 时的最长时长（秒）。"""

    interruptible: bool = True
    preconditions: Preconditions = Field(default_factory=Preconditions)
    params: dict[str, ParamDef] = Field(default_factory=dict)
    during: During = Field(default_factory=During)
    on_complete: OnComplete = Field(default_factory=OnComplete)
    template: str = ""
    tool_name: str = ""
    """工具型动作调用的工具名。是 ``tool_names`` 的兼容写法，永远等于列表里的第一个。"""

    tool_names: list[str] = Field(default_factory=list)
    """工具型动作要用到的工具，可以配多个，执行时按顺序调用并汇总结果。"""

    trigger_command: str = ""
    """「指令触发」型动作要触发的 AstrBot 指令，例如 ``情感分析`` 或 ``/天气``。"""

    trigger_hint: str = ""
    """给辅助模型看的参数说明：这条指令需要哪些参数、怎么给（例如「城市名」）。"""

    group: str = ""
    """动作分组。留空时编辑器按用途自动归类；写了就用自己起的名字。"""

    builtin: bool = False
    """内置动作：引擎对它有专门逻辑，只能停用、不能删除（例如说话、移动、睡觉）。"""

    visible: bool = False
    priority: int = 5
    description: str = ""
    enabled: bool = True
    """停用的动作等于"根本没有这个动作"：不进场景动作表、不进提示词、不会被大模型选中、计划里排到就跳过。"""

    @model_validator(mode="after")
    def _normalize_tools(self) -> "ActionDef":
        """把 ``tool_name`` / ``tool_names`` 两份写法对齐，老配置也能直接用。"""

        self.tool_names = _clean_names(self.tool_names or [], self.tool_name)
        self.tool_name = self.tool_names[0] if self.tool_names else ""
        return self

    def tool_list(self) -> list[str]:
        """这个动作要用到的工具（只写了 ``tool_name`` 的老配置也能读出来）。"""

        return _clean_names(self.tool_names or [], self.tool_name)

    def set_tools(self, names: list[str]) -> None:
        """设置工具列表，同时把兼容字段 ``tool_name`` 同步成第一个。"""

        self.tool_names = _clean_names(names)
        self.tool_name = self.tool_names[0] if self.tool_names else ""

    @field_validator("duration", mode="before")
    @classmethod
    def _non_negative(cls, value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def available_in(self, node_id: str) -> bool:
        """该动作在这个节点是否可用。"""

        if not self.enabled:
            return False
        if self.scope == "global":
            return True
        return node_id in self.allowed_nodes


class StateDynamics(Permissive):
    energy_decay_per_min: float = 0.0015
    loneliness_growth_per_min: float = 0.0008
    curiosity_growth_per_min: float = 0.0010
    affect_decay_per_min: float = 0.02
    """心潮每分钟回落多少。默认 0.02 ≈ 50 分钟从满值回到平静。"""

    boredom_growth_per_min: float = 0.0012
    sleep_energy_recovery_per_min: float = 0.0020
    nap_energy_recovery_per_min: float = 0.0008
    atmosphere_multiplier: float = 0.5
    mood_override_duration: int = 600

    @model_validator(mode="before")
    @classmethod
    def _legacy_keys(cls, data: Any) -> Any:
        return _rename_keys(data, {"social_decay_per_min": "affect_decay_per_min"})


class Limits(Permissive):
    max_actions_per_message: int = 3
    max_autonomous_per_hour: int = 2
    max_share_per_hour: int = 1
    max_think_memory: int = 5
    max_messages_per_say: int = 3
    plan_valid_duration: int = 1800
    max_action_chain_depth: int = 5
    max_active_users_tracked: int = 100
    max_log_events: int = 1500
    """每个会话最多保留多少条事件日志（超过会自动删掉最旧的）。"""

    llm_plan_min_interval_seconds: int = 900
    """两次「问 LLM 要计划」之间的最小间隔，避免每轮决策都烧 token。"""

    max_llm_plan_per_hour: int = 4
    """每小时最多几次 LLM 计划决策。"""

    max_llm_text_per_hour: int = 6
    """每小时最多几次「用 LLM 生成自主发言」，超出后使用内置短句池。"""

    max_tool_param_per_hour: int = 30
    """每小时最多几次「把意图翻译成工具参数」的辅助模型调用。"""

    max_arrival_decisions_per_hour: int = 12
    """每小时最多几次「走到新地方就地决策」。这类决策不占自主行动额度，单独限流。"""

    forced_plan_min_interval_seconds: int = 3600
    """极端保护（强制睡觉 / 强制找人）两次触发之间的最小间隔。"""


class Engagement(Permissive):
    unanswered_threshold: int = 3
    silence_window_minutes: int = 60
    cooldown_after_unanswered: int = 120
    halve_on_cooldown_end: bool = True
    after_reply_cooldown_minutes: int = 10
    """刚被搭话、她回完话之后的这段时间里，不要再因为孤独感主动开口。

    被动回复和主动搭话挨得太近会显得像刷屏；被动回复本身不受这个限制。
    """


class NicknameSync(Permissive):
    enabled: bool = True
    template: str = "{base} | {status}"
    max_length: int = 30
    cooldown_seconds: int = 60
    restore_on_idle: bool = True
    restore_on_idle_delay: int = 30
    status_map: dict[str, str] = Field(default_factory=dict)
    node_status: dict[str, str] = Field(default_factory=dict)


class ContentSafety(Permissive):
    blocked_words: list[str] = Field(default_factory=list)
    message_blocklist: list[str] = Field(default_factory=list)
    session_blocklist: list[str] = Field(default_factory=list)


SleepReplyMode = Literal["template", "silent", "normal"]

# 「调试输出」可以单独勾选的事件类型：key 是事件类型，值是行首图标。
# 编辑器里的勾选清单要跟这里保持一致（tests/test_units.py 有一条对齐检查）。
ECHO_EVENT_TYPES: dict[str, str] = {
    "plan": "🧠",
    "action_start": "▶️",
    "action_done": "✅",
    "action": "🎬",
    "tool": "🔧",
    "skip": "⏭️",
    "memory": "📝",
    "nickname": "🏷️",
    "cancel": "🛑",
    "vision": "🖼️",
    "recall_start": "💭",
    "recall_done": "📖",
    "schedule_edit": "🗓️",
    "command": "🧩",
    "engagement": "💤",
    "extreme": "🚨",
    "context": "🗜️",
    "wake_up": "🌅",
    "sleep_reply": "😴",
    "sleep_skip": "🤐",
}
# 「常用」那一档：决定、动作、工具、跳过——排查她"为什么这么做"最需要的几类
DEFAULT_ECHO_TYPES: tuple[str, ...] = (
    "plan",
    "action_start",
    "action_done",
    "action",
    "tool",
    "skip",
)


class MemoryConfig(Permissive):
    """对话记忆怎么写。

    逐条存「某某说了什么」几乎没有信息量，所以对话是**攒成片段再总结**一条：
    一段聊完（安静下来）、攒够条数、或者她离开这个地点时，才让模型把这几句压成
    一句以她的视角写的记忆。
    """

    dialogue_summary: bool = True
    """是否把对话总结成片段记下来（关掉就不再记录聊天内容，只留动作与内心活动）。"""

    summary_trigger_messages: int = 10
    """同一段对话攒够多少条触发一次总结。"""

    summary_idle_minutes: int = 30
    """一段对话安静这么久就当作聊完了，触发总结。"""

    summary_on_move: bool = True
    """她换地点时，把刚才那段对话总结掉。"""

    summary_max_chars: int = 60
    """总结的长度上限（写进提示词约束模型）。"""

    recall_penalty_minutes: int = 120
    """「临时召回惩罚」的窗口：刚被想起过的记忆在这段时间内会轻一点被再次想起。"""

    recall_penalty_strength: float = 0.5
    """窗口内最多打几折（0.5 = 最多降一半权重），出了窗口自动恢复。"""


class SleepConfig(Permissive):
    """她睡觉时怎么回应消息、怎么把她叫起来。"""

    reply_mode: SleepReplyMode = "template"
    """被叫（但没说要叫醒她）时怎么办：template=回一句固定文案，silent=完全不回，normal=照常回复。"""

    reply_text: str = "zzz…（{bot}睡觉中，要叫她起来吗？）"
    """``reply_mode=template`` 时发出去的文案，支持 ``{bot}`` 与 ``{user}``。"""

    reply_cooldown_minutes: int = 5
    """同一条固定文案的最短间隔（分钟）：被连着 @ 也不会刷屏，期间保持安静。"""

    wake_words: list[str] = Field(default_factory=lambda: list(DEFAULT_WAKE_WORDS))
    """命中这些词才算「明确叫醒」：打断睡眠并正常回复。"""

    wake_requires_mention: bool = True
    """是否要求 @ 她（私聊等同）才算叫醒。"""

    clear_plan_on_wake: bool = True
    """叫醒时连同手头排队的计划一起清掉，避免睡醒后补发几小时前的台词。"""

    applies_to_nap: bool = True
    """小睡（nap）也按睡觉处理。"""

    wake_grace_minutes: int = 12
    """刚被叫醒的保护期（分钟）：这段时间里规则不再安排她回去睡。"""

    block_plugins: bool = True
    """睡着时是否把消息整个挡下来。

    开启（默认）：没被明确叫醒的消息会在这条消息的**最前面**截住，
    其他插件（例如意图路由）也不会执行——既不浪费一次判断，也不会把睡着的她拖进对话。
    关掉：只保证本插件自己不出声，别的插件照常跑。
    """

    block_scope: Literal["unmentioned", "all"] = "unmentioned"
    """挡住哪些消息。

    - ``unmentioned``（默认）：只挡「没 @ 她」的消息；@ 她但没唤醒词的仍会回一句固定文案；
    - ``all``：除了 `/指令` 和含唤醒词的 @ 消息，其余全部挡掉（连固定文案也不回）。
    """


class DeciderConfig(Permissive):
    """决策器参数：什么时候该主动搭话（插话）。"""

    enabled: bool = True
    """是否允许她主动接别人的话（插话总开关）。"""

    interject_threshold: float = 0.6
    """孤独感高于这个值（且群里正在聊天、插话开关打开）时，她会想插一句。"""

    chat_window_minutes: int = 20
    """多久之内的消息算「群里正在聊天」。"""

    chat_max_messages: int = 12
    """进提示词的最近聊天条数（原始留档见 ContextConfig.chat_history_max）。"""

    llm_rate_min: float = 0.05
    """「要不要问大模型安排计划」的概率下限（她状态很平静时用这个）。"""

    llm_rate_max: float = 0.4
    """概率上限（她很想做点什么时用这个）。实际概率在两者之间按决策意愿插值。"""

    min_messages_to_interject: int = 2
    """窗口内至少有几条别人说的话，才值得插话。"""

    interject_cooldown_minutes: int = 20
    """两次主动插话之间的最短间隔，防止烦人。"""

    @model_validator(mode="before")
    @classmethod
    def _legacy_keys(cls, data: Any) -> Any:
        return _rename_keys(data, {"social_chat_threshold": "interject_threshold"})


class ContextConfig(Permissive):
    """群聊上下文怎么存、怎么带进提示词。"""

    chat_history_max: int = 200
    """原始群聊留档条数（持久化，重启后仍在）。超出后按下面策略处理。"""

    chat_overflow: Literal["discard", "compress"] = "discard"
    """留档超出上限时怎么办：直接丢弃最早的，或用压缩模型压成摘要。"""

    chat_compress_threshold: int = 80
    """留档达到多少条才触发一次压缩。"""

    chat_keep_after_compress: int = 30
    """压缩后保留多少条原文（更早的变成摘要）。"""

    summary_refresh_minutes: int = 60
    """两次压缩之间至少间隔多久，避免频繁调用模型。"""

    history_max_chars: int = 400
    """单条历史消息进入摘要前截断到多少字。"""

    image_max: int = 3
    """没配图片转述模型时，最多把几张图片直接交给多模态主模型（自上次回复以来）。"""


class DefaultState(Permissive):
    mood: str = "平静"
    energy: float = 0.6
    loneliness: float = 0.5
    curiosity: float = 0.5
    affect: float = 0.3
    boredom: float = 0.3

    @model_validator(mode="before")
    @classmethod
    def _legacy_keys(cls, data: Any) -> Any:
        return _rename_keys(data, {"social": "affect"})

    @field_validator("energy", "loneliness", "curiosity", "affect", "boredom", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> float:
        return _clamp01(value, 0.5)


class WorldConfig(Permissive):
    world_id: str = "default"
    name: str = "小世界"
    bot_name: str = ""
    """Bot 的名字，用于动作模板里的 {bot} 占位符（留空时依次回落到群名片原名、再回落到"她"）。"""

    gender: Gender = "female"
    """性别：决定文案里的称呼（她 / 他 / ta）。"""

    tool_result_reply: bool = True
    """工具型动作拿到结果后，是否交回主模型说一句（关掉就只记进日志）。"""

    remote_action_travel: bool = True
    """允许她「想去别处做某事」：她自己没写移动时，插件替她走过去再执行。"""

    echo_types: list[str] = Field(default_factory=list)
    """调试用：这些类型的事件也作为群消息发出来（空列表 = 关闭）。

    可选项见 :data:`ECHO_EVENT_TYPES`。
    """

    reply_mode: Literal["takeover", "inject"] = "takeover"
    """被 @（或消息走到大模型）时的处理方式。

    - ``takeover``：本插件接管这次回复，自己调大模型、按 JSON 动作执行并发送，同时阻止主人格重复回复；
    - ``inject``：只追加世界认知，由主人格用自然语言回复。
    """

    reasoning_enabled: bool = True
    """是否要求大模型在 actions 之前先输出 reasoning（推理草稿）。"""

    schema_version: int = 1
    global_prompt: str = ""
    timezone: str = "Asia/Shanghai"
    default_state: DefaultState = Field(default_factory=DefaultState)
    state_dynamics: StateDynamics = Field(default_factory=StateDynamics)
    limits: Limits = Field(default_factory=Limits)
    engagement: Engagement = Field(default_factory=Engagement)
    sleep: SleepConfig = Field(default_factory=SleepConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    nickname_sync: NicknameSync = Field(default_factory=NicknameSync)
    memory_scope_mode: MemoryScopeMode = "group_persona"
    memory_scope_fallback: bool = True
    memory_scope_warn_on_switch: bool = True
    memory_conflict_policy: Literal["newest", "highest_weight", "skip_conflict"] = "newest"
    global_allowed_tools: list[str] = Field(default_factory=list)
    tool_filter_enabled: bool = True
    decider: DeciderConfig = Field(default_factory=DeciderConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    content_safety: ContentSafety = Field(default_factory=ContentSafety)
    zones: list[ZoneDef] = Field(default_factory=list)
    zone_edges: list[ZoneEdgeDef] = Field(default_factory=list)
    """跨区连线（门户对）：每条自带两端各自的房间。"""
    nodes: list[NodeDef] = Field(default_factory=list)
    edges: list[EdgeDef] = Field(default_factory=list)
    actions: list[ActionDef] = Field(default_factory=list)

    # --- 便捷索引 ---
    def node_map(self) -> dict[str, NodeDef]:
        return {node.id: node for node in self.nodes}

    def action_map(self) -> dict[str, ActionDef]:
        return {action.id: action for action in self.actions}

    def actions_in(self, node_id: str) -> list[ActionDef]:
        return [a for a in self.actions if a.available_in(node_id)]

    def adjacent(self) -> dict[str, list[tuple[str, int]]]:
        """房间层的**完整**邻接表：区域内连线 + 跨区门户。

        跨区连线两端本来就是具体房间，所以寻路只需要这一张图——
        她人在地图上怎么走、走多久，都由它算出来。
        """

        nodes = self.node_map()
        graph = self.room_adjacent()
        for edge in self.zone_edges:
            for src, dst in edge.endpoints(nodes):
                graph.setdefault(src, []).append((dst, edge.ticks))
        return graph

    def room_adjacent(self) -> dict[str, list[tuple[str, int]]]:
        """只含同区域内连线的邻接表（区域地图上画的就是这些线）。"""

        nodes = self.node_map()
        graph: dict[str, list[tuple[str, int]]] = {node_id: [] for node_id in nodes}
        for edge in self.edges:
            for src, dst in edge.endpoints(nodes):
                graph.setdefault(src, []).append((dst, edge.ticks))
        return graph

    def zone_map(self) -> dict[str, ZoneDef]:
        return {zone.id: zone for zone in self.zones}

    def default_zone_id(self) -> str:
        return self.zones[0].id if self.zones else ""

    def zone_of(self, node_id: str) -> str:
        node = self.node_map().get(node_id)
        return (node.zone_id if node else "") or self.default_zone_id()

    def nodes_in_zone(self, zone_id: str) -> list[NodeDef]:
        return [node for node in self.nodes if self.zone_of(node.id) == zone_id]

    def zone_adjacent(self) -> dict[str, list[tuple[str, int]]]:
        """区域层的邻接表（世界地图用）：区域 -> [(邻居区域, 最省的那条线的 tick)]。"""

        graph: dict[str, list[tuple[str, int]]] = {zone.id: [] for zone in self.zones}
        for edge in self.zone_edges:
            if edge.from_zone not in graph or edge.to_zone not in graph:
                continue
            graph[edge.from_zone].append((edge.to_zone, edge.ticks))
            if edge.bidirectional:
                graph[edge.to_zone].append((edge.from_zone, edge.ticks))
        return graph

    def default_node_id(self) -> str:
        if any(node.id == "bedroom" for node in self.nodes):
            return "bedroom"
        return self.nodes[0].id if self.nodes else ""


class ChainStep(Permissive):
    type: str
    target_node: str = ""
    target: str = ""
    duration: int = 0
    content: str = ""
    messages: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class ScheduleConditions(Permissive):
    not_state: list[str] = Field(default_factory=list)
    state: list[str] = Field(default_factory=list)
    min_energy: float | None = None
    max_energy: float | None = None
    min_loneliness: float | None = None
    node_in: list[str] = Field(default_factory=list)


class ScheduleDef(Permissive):
    id: str
    enabled: bool = True
    time: str = "00:00"
    days: list[str] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    )
    action_chain: list[ChainStep] = Field(default_factory=list)
    conditions: ScheduleConditions = Field(default_factory=ScheduleConditions)
    priority: int = 5
    sessions: list[str] = Field(default_factory=list)
    auto_travel: bool = False
    """开启后，若某一步要求的地点不满足（例如在书房才能上网），会先自动走到那个地点再执行。"""

    @field_validator("time")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        text = str(value).strip()
        parts = text.split(":")
        if len(parts) != 2:
            raise ValueError(f"时间格式必须是 HH:MM，收到 {value!r}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"时间超出范围：{value!r}")
        return f"{hour:02d}:{minute:02d}"


class SchedulesConfig(Permissive):
    schedules: list[ScheduleDef] = Field(default_factory=list)


class SessionDef(Permissive):
    session_id: str
    type: Literal["group", "private"] = "group"
    platform: str = ""
    enabled: bool = True
    cold_start_mode: ColdStartMode = "awakening"
    cold_start_node: str = "bedroom"
    cold_start_prompt: str = ""
    added_at: int = 0
    note: str = ""


class SessionsConfig(Permissive):
    sessions: list[SessionDef] = Field(default_factory=list)


def normalize_edge_keys(data: dict[str, Any]) -> dict[str, Any]:
    """连线的起点在文件里叫 `from`（`from` 是 Python 关键字，模型字段名是 `from_`）。

    按字段名 dump 出来的配置会带上 `from_`，这里统一收敛回 `from`，
    避免它以"多余字段"的形式留在文件里、或者让前端读不到端点。
    """

    edges = data.get("edges")
    if not isinstance(edges, list):
        return data
    normalized: list[Any] = []
    for edge in edges:
        if not isinstance(edge, dict):
            normalized.append(edge)
            continue
        item = {key: value for key, value in edge.items() if key != "from_"}
        if "from" not in item and edge.get("from_") is not None:
            item["from"] = edge["from_"]
        normalized.append(item)
    return {**data, "edges": normalized}


# 默认文案升级：只替换「和旧版默认值一字不差」的字段，用户自己改过的不动。
DEFAULT_ZONE_ID = "home"
DEFAULT_ZONE_NAME = "家中"

_DEFAULT_TEXT_UPGRADES: dict[str, dict[str, dict[str, str]]] = {
    "cook": {
        "prompt_hint": {
            "用第一人称说说刚做好的这道菜：做了什么、闻起来怎么样、想不想分给大家": (
                "用第一人称随口说说刚做好的这道菜：做了什么、闻起来怎么样、"
                "你自己吃着什么感觉；不要招呼或招揽别人来吃"
            )
        }
    },
    "sleep": {
        "description": {
            "睡一觉恢复精力，需要先回到卧室。": (
                "睡一整晚恢复精力（约 8 小时，夜里或精力见底时用），需要先回到卧室。"
            )
        }
    },
    "nap": {
        "description": {
            "打个盹，睡多久由你自己决定（10 分钟到 1 小时），睡得越久精力恢复越多。": (
                "白天犯困时打个盹（10 分钟到 1 小时，睡多久由你自己决定），"
                "睡得越久精力恢复越多。"
            )
        }
    },
}


def normalize_legacy_keys(data: dict[str, Any]) -> dict[str, Any]:
    """丢掉已经废弃的字段，并把老写法迁到新写法。

    - 节点上的 `allowed_tools`：工具现在只由动作声明，直接丢掉；
    - 动作前置条件里的 `tool_available`：它表达的正是「这个动作需要哪个工具」，
      所以先迁移成动作自己的 `tool_name`（原本没填时），再丢掉；
    - 少数内置动作的默认提示语改过措辞：还是旧默认值就顺手换掉；
    - 调试输出以前只有一个总开关 `echo_actions`，现在改成可勾选的类型列表 `echo_types`；
    - 地图以前是一张平铺的图，现在分了区域：老配置里的节点全部归进默认区域「家中」。
    - 动作归属以前在节点和动作上各存一份（`node.allowed_actions` + `action.allowed_nodes`），
      现在只认动作那一份：节点自己声明过的、非全局的动作会补进它的 `allowed_nodes`，然后丢掉节点那份。
    """

    nodes = data.get("nodes")
    result = data
    # 区域：老配置没有 zones 时，建一个默认区域，把已有节点全放进去
    zones = result.get("zones")
    if not isinstance(zones, list) or not zones:
        result = {
            **result,
            "zones": [
                {
                    "id": DEFAULT_ZONE_ID,
                    "name": DEFAULT_ZONE_NAME,
                    "note": "",
                    "x": 60,
                    "y": 60,
                }
            ],
        }
        zones = result["zones"]
    # 没写归属的节点一律落进第一个区域
    first_zone = ""
    if isinstance(zones, list) and zones and isinstance(zones[0], dict):
        first_zone = str(zones[0].get("id") or "").strip()
    if first_zone and isinstance(nodes, list):
        nodes = [
            (
                {**node, "zone_id": node.get("zone_id") or first_zone}
                if isinstance(node, dict)
                else node
            )
            for node in nodes
        ]
    if isinstance(nodes, list):
        # 节点声明过的动作 → 补进动作的 allowed_nodes（全局动作不受节点列表影响）
        raw_actions = result.get("actions")
        if isinstance(raw_actions, list):
            by_id = {
                str(item.get("id")): item
                for item in raw_actions
                if isinstance(item, dict) and item.get("id")
            }
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                claimed = [
                    str(name).strip()
                    for name in (node.get("allowed_actions") or [])
                    if str(name).strip()
                ]
                for action_id in claimed:
                    action = by_id.get(action_id)
                    if not isinstance(action, dict):
                        continue
                    if str(action.get("scope") or "global") == "global":
                        continue
                    allowed = [str(name) for name in (action.get("allowed_nodes") or [])]
                    node_id = str(node.get("id") or "")
                    if node_id and node_id not in allowed:
                        action["allowed_nodes"] = allowed + [node_id]
        result = {
            **result,
            "nodes": [
                (
                    {
                        key: value
                        for key, value in node.items()
                        if key not in ("allowed_tools", "allowed_actions")
                    }
                    if isinstance(node, dict)
                    else node
                )
                for node in nodes
            ],
        }
    actions = result.get("actions")
    if isinstance(actions, list):
        cleaned_actions: list[Any] = []
        for action in actions:
            if not isinstance(action, dict):
                cleaned_actions.append(action)
                continue
            pre = action.get("preconditions")
            if isinstance(pre, dict) and ("tool_available" in pre or "node_in" in pre):
                legacy = [
                    str(name).strip()
                    for name in (pre.get("tool_available") or [])
                    if str(name).strip()
                ]
                if legacy and not str(action.get("tool_name") or "").strip():
                    action = {**action, "tool_name": legacy[0]}
                # node_in 与「限定地点」是同一件事，迁移成 scope/allowed_nodes
                node_in = [
                    str(name).strip()
                    for name in (pre.get("node_in") or [])
                    if str(name).strip()
                ]
                if node_in:
                    existing = [str(name) for name in (action.get("allowed_nodes") or [])]
                    merged = list(dict.fromkeys(existing + node_in))
                    action = {**action, "scope": "node", "allowed_nodes": merged}
                action = {
                    **action,
                    "preconditions": {
                        key: value
                        for key, value in pre.items()
                        if key not in ("tool_available", "node_in")
                    },
                }
            upgrades = _DEFAULT_TEXT_UPGRADES.get(str(action.get("id") or ""))
            completed = action.get("on_complete")
            if upgrades and isinstance(completed, dict):
                table = upgrades.get("prompt_hint") or {}
                hint = completed.get("prompt_hint")
                if isinstance(hint, str) and hint in table:
                    action = {
                        **action,
                        "on_complete": {**completed, "prompt_hint": table[hint]},
                    }
            if upgrades:
                table = upgrades.get("description") or {}
                current = action.get("description")
                if isinstance(current, str) and current in table:
                    action = {**action, "description": table[current]}
            cleaned_actions.append(action)
        result = {**result, "actions": cleaned_actions}
    if "echo_actions" in result:
        legacy_echo = bool(result.get("echo_actions"))
        migrated = [
            str(name) for name in (result.get("echo_types") or []) if str(name).strip()
        ]
        if legacy_echo and not migrated:
            migrated = list(DEFAULT_ECHO_TYPES)
        result = {
            key: value for key, value in result.items() if key != "echo_actions"
        }
        result["echo_types"] = [name for name in migrated if name in ECHO_EVENT_TYPES]
    return result


def parse_world(data: dict[str, Any]) -> tuple[WorldConfig, list[str]]:
    """校验并修复世界配置，返回 (配置, 警告列表)。"""

    warnings: list[str] = []
    world = WorldConfig.model_validate(
        normalize_legacy_keys(normalize_edge_keys(data or {}))
    )

    # 节点：去重、保证 id 非空
    seen: set[str] = set()
    nodes: list[NodeDef] = []
    for node in world.nodes:
        node_id = str(node.id).strip()
        if not node_id:
            warnings.append("发现一个没有 id 的节点，已丢弃")
            continue
        if node_id in seen:
            warnings.append(f"节点 id 重复，已丢弃后一个：{node_id}")
            continue
        seen.add(node_id)
        node.id = node_id
        if not node.name:
            node.name = node_id
        nodes.append(node)
    world.nodes = nodes
    node_map = world.node_map()

    # 区域：id 去重、保证至少有一个；节点归属落到存在的区域上
    zone_ids: set[str] = set()
    zones: list[ZoneDef] = []
    for zone in world.zones:
        zone_id = str(zone.id).strip()
        if not zone_id:
            warnings.append("发现一个没有 id 的区域，已丢弃")
            continue
        if zone_id in zone_ids:
            warnings.append(f"区域 id 重复，已丢弃后一个：{zone_id}")
            continue
        zone_ids.add(zone_id)
        zone.id = zone_id
        if not zone.name:
            zone.name = zone_id
        zones.append(zone)
    if not zones:
        warnings.append("世界上没有任何区域，已自动补一个「家中」")
        zones.append(ZoneDef(id=DEFAULT_ZONE_ID, name=DEFAULT_ZONE_NAME))
        zone_ids = {DEFAULT_ZONE_ID}
    world.zones = zones
    fallback_zone = zones[0].id
    for node in world.nodes:
        zone_id = str(node.zone_id or "").strip()
        if zone_id not in zone_ids:
            if zone_id:
                warnings.append(f"节点 {node.id} 所属区域 {zone_id} 不存在，已归入「{fallback_zone}」")
            node.zone_id = fallback_zone

    # 边：端点必须存在
    edges: list[EdgeDef] = []
    for edge in world.edges:
        if edge.from_ not in node_map or edge.to not in node_map:
            warnings.append(f"连线 {edge.from_} -> {edge.to} 的端点不存在，已丢弃")
            continue
        if edge.from_ == edge.to:
            warnings.append(f"连线 {edge.from_} 连到了自己，已丢弃")
            continue
        if not edge.id:
            edge.id = f"e_{edge.from_}_{edge.to}"
        edges.append(edge)
    world.edges = edges

    # 动作：id 去重，allowed_nodes 过滤
    action_ids: set[str] = set()
    actions: list[ActionDef] = []
    for action in world.actions:
        action_id = str(action.id).strip()
        if not action_id:
            warnings.append("发现一个没有 id 的动作，已丢弃")
            continue
        if action_id in action_ids:
            warnings.append(f"动作 id 重复，已丢弃后一个：{action_id}")
            continue
        action_ids.add(action_id)
        action.id = action_id
        if not action.name:
            action.name = action_id
        if action.scope == "node":
            kept = [n for n in action.allowed_nodes if n in node_map]
            missing = [n for n in action.allowed_nodes if n not in node_map]
            if missing:
                warnings.append(
                    f"动作 {action_id} 引用了不存在的节点 {missing}，已忽略"
                )
            action.allowed_nodes = kept
            if not kept:
                warnings.append(f"动作 {action_id} 限定节点为空，已改为全局动作")
                action.scope = "global"
        actions.append(action)
    world.actions = actions

    # 内置动作：引擎对它们有专门逻辑，缺失会让世界跑不起来，所以自动补回来。
    # 用户删不掉它们（编辑器里删除按钮是禁用的），手改 JSON、导入预设也拦得住。
    have = {action.id for action in actions}
    missing_builtin = [
        action_id for action_id in REQUIRED_BUILTIN_ACTIONS if action_id not in have
    ]
    if missing_builtin:
        from .defaults import default_actions

        defaults = {item["id"]: item for item in default_actions()}
        for action_id in missing_builtin:
            payload = defaults.get(action_id)
            if payload:
                actions.append(ActionDef.model_validate(payload))
                warnings.append(f"缺少内置动作 {action_id}，已按默认值补回（它只能停用，不能删除）")
    for action in actions:
        if action.id in REQUIRED_BUILTIN_ACTIONS:
            action.builtin = True
    world.actions = actions

    # 跨区连线（门户对）：两端的区域与房间都必须存在，且房间确实属于那一端区域
    zone_edges: list[ZoneEdgeDef] = []
    for edge in world.zone_edges:
        if edge.from_zone not in zone_ids or edge.to_zone not in zone_ids:
            warnings.append(
                f"跨区连线 {edge.from_zone} -> {edge.to_zone} 的区域不存在，已丢弃"
            )
            continue
        if edge.from_node not in node_map or edge.to_node not in node_map:
            warnings.append(
                f"跨区连线 {edge.from_node} -> {edge.to_node} 的端点房间不存在，已丢弃"
            )
            continue
        if edge.from_node == edge.to_node:
            warnings.append(f"跨区连线的两端是同一个房间 {edge.from_node}，已丢弃")
            continue
        if world.zone_of(edge.from_node) != edge.from_zone:
            warnings.append(
                f"跨区连线：{edge.from_node} 不属于区域 {edge.from_zone}，已丢弃"
            )
            continue
        if world.zone_of(edge.to_node) != edge.to_zone:
            warnings.append(
                f"跨区连线：{edge.to_node} 不属于区域 {edge.to_zone}，已丢弃"
            )
            continue
        if not edge.id:
            edge.id = f"z_{edge.from_node}_{edge.to_node}"
        zone_edges.append(edge)
    world.zone_edges = zone_edges

    if not world.nodes:
        warnings.append("世界没有任何节点，将使用默认世界")
        from .defaults import default_world

        return parse_world(default_world())

    return world, warnings


def parse_schedules(data: dict[str, Any]) -> tuple[SchedulesConfig, list[str]]:
    """校验日程配置。"""

    warnings: list[str] = []
    config = SchedulesConfig.model_validate(data or {})
    valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    seen: set[str] = set()
    kept: list[ScheduleDef] = []
    for schedule in config.schedules:
        schedule_id = str(schedule.id).strip()
        if not schedule_id:
            warnings.append("发现一个没有 id 的日程，已丢弃")
            continue
        if schedule_id in seen:
            warnings.append(f"日程 id 重复，已丢弃后一个：{schedule_id}")
            continue
        seen.add(schedule_id)
        schedule.id = schedule_id
        invalid_days = [d for d in schedule.days if d not in valid_days]
        if invalid_days:
            warnings.append(f"日程 {schedule_id} 的星期取值非法 {invalid_days}，已忽略")
            schedule.days = [d for d in schedule.days if d in valid_days]
        if not schedule.days:
            schedule.days = sorted(valid_days)
        if not schedule.action_chain:
            warnings.append(f"日程 {schedule_id} 没有动作，已禁用")
            schedule.enabled = False
        kept.append(schedule)
    config.schedules = kept
    return config, warnings


def parse_sessions(data: dict[str, Any]) -> tuple[SessionsConfig, list[str]]:
    """校验会话白名单。"""

    warnings: list[str] = []
    config = SessionsConfig.model_validate(data or {})
    seen: set[str] = set()
    kept: list[SessionDef] = []
    for session in config.sessions:
        session_id = str(session.session_id).strip()
        if not session_id:
            warnings.append("发现一个没有 session_id 的会话，已丢弃")
            continue
        if session_id in seen:
            warnings.append(f"会话 {session_id} 重复，已丢弃后一个")
            continue
        seen.add(session_id)
        session.session_id = session_id
        if not session.platform:
            session.platform = session_id.split(":", 1)[0]
        kept.append(session)
    config.sessions = kept
    return config, warnings



