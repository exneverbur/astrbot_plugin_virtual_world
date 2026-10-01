"""配置数据模型（Pydantic）。

对应设计文档第 3 章：world.json / schedules.json / sessions.json。
模型只负责校验与补全，不做业务逻辑；校验失败时尽量「修好并警告」而不是让插件起不来。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .defaults import (
    DEFAULT_CAPTION_PROMPT,
    DEFAULT_CAPTION_RELATION_PROMPT,
    DEFAULT_FORWARD_PROMPT,
    DEFAULT_WAKE_WORDS,
)


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

# 这些动作在引擎里有专门逻辑（说话的出口、寻路、睡眠门禁、内心活动、分享上限、
# 生图动作接到事件里……），删掉会让整个世界跑不起来，所以只能停用、不能删除。
REQUIRED_BUILTIN_ACTIONS: tuple[str, ...] = (
    "say",
    "walk_to",
    "sleep",
    "think",
    "share",
    "recall",
    "remember",
    "poke",
    "search_web",
    "check_weather",
    "schedule_list",
    "schedule_add",
    "schedule_remove",
    # 生图：接了图片插件才有用，所以默认停用（只能用/停用，不能删）
    "selfie",
    "take_photo",
    "leg_photo",
    "change_clothes",
    "make_video",
)

BUILTIN_ACTION_UPDATES: dict[str, dict[str, tuple[str, str]]] = {
    # 动作 id -> {字段: (老内置值, 新内置值)}
    # 老配置里存的是整套动作数据。**只有字段还等于老内置值时才跟着更新**——
    # 用户自己改过的名字 / 文案不动。
    "pour_tea": {
        "name": ("倒杯茶", "倒茶提醒喝水"),
        "template": ("（{bot}给你倒了杯热茶）", "（{bot}给你倒了杯茶，顺嘴提醒你去喝口水）"),
        "description": (
            "给某个人倒杯热茶，在吧台、厨房或客厅可用。",
            "给某个人倒杯茶，顺便提醒他喝口水（他容易忘）。在吧台、厨房或客厅可用。",
        ),
    },
}


TUNED_DEFAULT_UPGRADES: dict[str, tuple[float, float]] = {
    # 「配置里存着老默认值」时跟着新默认走：字段 -> (老默认, 新默认)。
    # 用户自己调过（值不等于老默认）就不动他——跟 BUILTIN_ACTION_UPDATES 一个道理。
    "profile.miss_growth_per_min": (0.0006, 0.0012),
    "profile.miss_cooldown_min_minutes": (45.0, 15.0),
    "profile.miss_cooldown_max_minutes": (240.0, 60.0),
    "profile.miss_push_threshold": (0.85, 0.70),
}
"""这一轮调过默认值的字段：老配置里还是旧默认就顺手升级，改过的不碰。"""


DEFAULT_INTIMACY: dict[str, float] = {
    # 默认动作库里"确实是**肢体接触**"的那些：摸头、抱抱、亲亲、蹭蹭、靠肩膀…
    # 数字是这一下有多解渴（0~1），动作做完时按它去满足欲求。
    # 只牵过手、拍过肩膀的算浅浅一下；抱紧、亲上、依偎是实实在在的一下。
    "hug": 1.0,
    "kiss": 1.0,
    "kiss_lips": 1.0,
    "nestle": 0.9,
    "kiss_forehead": 0.9,
    "nuzzle": 0.8,
    "lean_on": 0.8,
    "interlock": 0.7,
    "pat": 0.7,
    "hold_hands": 0.6,
    "close_eyes": 0.6,
    "massage": 0.6,
    "whisper_ear": 0.6,
    "drape_coat": 0.5,
    "feed_bite": 0.5,
    "ruffle_hair": 0.5,
    "bite_shoulder": 0.5,
    "bite_wrist": 0.4,
    "pinch_cheek": 0.4,
    "wipe_tears": 0.4,
    "tug_sleeve": 0.3,
    "cold_hands": 0.3,
    "bump_shoulder": 0.3,
    "pinch_nose": 0.3,
    "pat_shoulder": 0.2,
    "high_five": 0.1,
    "give_snack": 0.1,
    "hand_tissue": 0.1,
}
"""内置动作里的"亲密度"：动作没自己写 ``intimacy`` 时按这张表算。

用户改了动作的 ``intimacy``（编辑器里那个「亲密程度」）就以他自己的为准；
表里没有的动作一律算 0——对欲求完全没有影响。
"""


def action_intimacy(action: Any) -> float:
    """这个动作算多亲密的肢体接触（0 = 不算）。"""

    value = getattr(action, "intimacy", None)
    if value is None:
        value = DEFAULT_INTIMACY.get(str(getattr(action, "id", "") or ""), 0.0)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


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

    @model_validator(mode="before")
    @classmethod
    def _tidy_string_lists(cls, data: Any) -> Any:
        """声明成 ``list[str]`` 的字段统一洗一遍。

        编辑器里的多选框在选项没有 id 时会写进 ``null``，手写 JSON 也可能留空串；
        这种脏值不该让整份配置存不下去（以前报的是一串英文校验错误）。
        """

        if not isinstance(data, dict):
            return data
        cleaned = data
        for name, field in cls.model_fields.items():
            if name not in data or not _declares_str_list(field):
                continue
            value = data.get(name)
            tidy = _clean_str_list(value)
            if value != tidy:
                if cleaned is data:
                    cleaned = dict(data)
                cleaned[name] = tidy
        return cleaned


def _clean_str_list(value: Any) -> list[str]:
    """把"一串字符串"洗干净：丢掉 None / 空串、去掉首尾空白、去重。

    只写了一个值时也认（``"42"`` 和 ``42`` 都当成"只有一个元素"）。
    """

    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    elif isinstance(value, (str, int, float)):
        items = [value]
    else:
        return []
    result: list[str] = []
    for item in items:
        if item is None or isinstance(item, bool):
            continue
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _declares_str_list(field: Any) -> bool:
    """这个字段是不是 ``list[str]``（只对这类字段动手，别的类型一律不碰）。"""

    annotation = getattr(field, "annotation", None)
    origin = getattr(annotation, "__origin__", None)
    return origin is list and getattr(annotation, "__args__", ()) == (str,)


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
    nickname_text: str = ""
    """她人在这个地点时，群名片上显示什么（留空则不改名片）。

    和动作上的同名文案一样：写在地点自己身上，换预设时跟着地点一起走。
    """

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


class ActionQuota(Permissive):
    """动作的使用次数上限（0 = 那一档不限制）。"""

    day: int = 0
    week: int = 0
    month: int = 0

    @field_validator("day", "week", "month", mode="before")
    @classmethod
    def _non_negative(cls, value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def limits(self) -> dict[str, int]:
        return {"day": self.day, "week": self.week, "month": self.month}


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
    intimacy: float | None = None
    """这个动作有多"亲密的肢体接触"：0 = 不是，1 = 就是抱抱亲亲这种。

    它只有一个用处：动作做完时按这个数去**满足欲求**（贴得越近，落得越多）。
    留空（``None``）表示"按内置对照表算"，也就是老配置原样不动就有正确的值；
    填 0 是**明确说这件事不算亲密接触**。见 ``DEFAULT_INTIMACY`` / ``action_intimacy``。
    """

    preconditions: Preconditions = Field(default_factory=Preconditions)
    params: dict[str, ParamDef] = Field(default_factory=dict)
    during: During = Field(default_factory=During)
    on_complete: OnComplete = Field(default_factory=OnComplete)
    template: str = ""
    desc_mode: Literal["full", "brief"] = "full"
    """动作说明写多细：``full`` = 写出描述（默认），``brief`` = 只写「id（名字）」。

    亲昵 / 打招呼这类"看一眼名字就知道干嘛"的动作用 ``brief``：几十个这样的动作
    各带一句说明，会把提示词撑得又长又没信息量；规则性的附加标记
    （工具型 / 指令型 / 时长由你定 / 只有这个地点才能做）照旧会拼在后面。
    """

    nickname_text: str = ""
    """她正在做这个动作时，群名片上显示什么（留空则退回"状态 → 文案"的兜底映射）。

    文案放在动作上而不是全局字典里：换预设时动作跟着一起换，名片文案也就配套了。
    """

    tool_name: str = ""
    """工具型动作调用的工具名。是 ``tool_names`` 的兼容写法，永远等于列表里的第一个。"""

    tool_names: list[str] = Field(default_factory=list)
    """工具型动作要用到的工具，可以配多个，执行时按顺序调用并汇总结果。"""

    tool_fallbacks: list[str] = Field(default_factory=list)
    """**已废弃**：备选工具现在直接并进 ``tool_names``（顺序即优先级）。

    解析老配置时会自动合并并清空，保留字段只是为了迁移时看得见原来的值。
    """

    tool_mode: Literal["sequence", "fallback", "smart"] = "sequence"
    """多个工具怎么用：

    - ``sequence``（默认）：装了的都调，按顺序，结果合并；
    - ``fallback``：只用一个——按顺序挑第一个能用的，失败就换下一个；
    - ``smart``：让辅助模型按她的意图挑一个（选择与补参数合并成一次调用），
      失败先补参数重试，仍失败就把那个工具从本轮候选里摘掉再问一次。
    """

    tool_flow: Literal["simple", "search"] = "simple"
    """工具动作的编排形态。

    - ``simple``（默认）：调用工具 → 把结果交回给她说一句；
    - ``search``：联网检索流水线——多查询、结果归一化成"证据"、可选读正文，
      再按证据讲给她听（「上网搜索」默认就是这个形态）。
    """

    reader_tool_names: list[str] = Field(default_factory=list)
    """联网检索形态下，用来读网页正文的工具（可以传 URL、返回 markdown 的那种）。

    留空就只用搜索结果的摘要，不抓正文。
    """

    search_depth: Literal["quick", "standard", "deep"] = "standard"
    """检索深度：quick 只查一次不读正文，standard 读前两篇、最多补查一轮，
    deep 读前三篇、最多补查两轮。"""

    search_max_queries: int = 3
    """一次动作最多发几条查询（她可以在动作里直接给多条 query）。"""

    search_max_reads: int = 3
    """最多读几篇正文。"""

    search_rounds: int = 2
    """证据不够时最多补查几轮。"""

    search_topic: str = ""
    """这个检索动作的固定主题（例如「今日新闻热点」）。

    自定义动作（比如「搜索新闻」）在日程里被调用、又没写意图时，就用它当搜索主题；
    优先级：日程里的意图 > 这里 > 查询模板 > 动作说明。
    """

    search_query_template: str = ""
    """查询模板：用 ``{topic}`` / ``{date}`` 占位，例如 ``{date} 新闻热点``。"""

    search_cite: bool = False
    """讲结果时是否允许带一句来源（默认不带，来源只进日志与调试输出）。"""

    search_satisfy_curiosity: float = 0.30
    """检索型动作真查到东西之后，好奇心回落多少（0 = 不回落）。

    动作自己配的 ``on_complete.effects`` 里那点好奇回落（默认 -0.06）抵不过每分钟的
    自然增长（0.06/小时），不补这一刀她会整天停在"很好奇"，冷却一到又去查同一件事。
    """


    trigger_command: str = ""
    """「指令触发」型动作要触发的 AstrBot 指令，例如 ``情感分析`` 或 ``/天气``。"""

    trigger_hint: str = ""
    """给辅助模型看的参数说明：这条指令需要哪些参数、怎么给（例如「城市名」）。"""

    group: str = ""
    """动作分组。留空时编辑器按用途自动归类；写了就用自己起的名字。"""

    builtin: bool = False
    """内置动作：引擎对它有专门逻辑，只能停用、不能删除（例如说话、移动、睡觉）。"""

    event_usable: Literal["auto", "allow", "deny"] = "auto"
    """她遇到事的时候，能不能为了这件事调用这个动作。

    - ``auto``（默认）：工具型 / 指令型动作（能用外部能力做事）算可用；
      「往群里发东西」那类（``target_type=group``）排除，免得事件里顺手刷屏；
    - ``allow``：强制允许（即使不符合上面的默认口径）；
    - ``deny``：事件里不允许调用。
    """

    quota: "ActionQuota" = Field(default_factory=lambda: ActionQuota())
    """使用次数上限：超过之后**不再出现在提示词里**，计划里排到也会被跳过。

    三个周期各自独立，填 0 表示那一档不限制（生图默认每天 5 次，录视频每天 2 次）。
    """

    state_slot: str = ""
    """把工具 / 指令返回的结果记进状态槽（留空 = 不记）。

    别的插件常常有自己的状态（生图插件的「今日穿搭」、背包、经济…）。填了槽名之后，
    结果会存进 ``state.external_state``，之后每一轮提示词里都带着，她不会说完就忘。
    """

    state_label: str = ""
    """这个槽在提示词里叫什么（例如「今日穿搭」）。留空就用槽名。"""

    state_ttl_minutes: int = 0
    """这个槽多久过期（分钟）。0 = 不过期（例如"今日穿搭"当天有效就填 720）。"""

    state_summarize: bool = False
    """开启后先让打杂模型把结果压成一句话再存（返回是一大段时用得上）。"""

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

    def tool_candidates(self) -> list[str]:
        """主选 + 备选的全部工具名。"""

        return _clean_names([*self.tool_list(), *(self.tool_fallbacks or [])])

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

    valence_decay_per_min: float = 0.010
    """效价偏移每分钟回落多少。默认 0.01 ≈ 一小时出头回落一半。"""

    chat_valence_cap: float = 0.05
    """一轮聊天最多推动效价多少。

    取的是模型给的 -1~1 乘上这个数。0.05 的意思是：**说得再好听，一轮也只值 0.05**，
    想看到明显起伏得靠真的经历（事件、约定、意外），而不是几句夸奖。
    """

    chat_valence_daily_cap: float = 0.15
    """聊天一天最多把效价推动多少（正负各算一份）。

    超过之后这一天再怎么聊都不再推高——日常陪伴改的是好感度，不是心情的量程。
    填 0 = 不限制。
    """

    boredom_growth_per_min: float = 0.0012
    sleep_energy_recovery_per_min: float = 0.0020
    nap_energy_recovery_per_min: float = 0.0008
    sleep_curiosity_decay_per_min: float = 0.0012
    """睡觉 / 小睡时好奇心每分钟回落多少（整觉 8 小时 ≈ 降 0.58）。

    睡一觉本来就该把"昨天攒的那点好奇"放下，不然她会带着满格好奇心入睡、
    醒来第一件事就是冲去查东西。
    """

    sleep_curiosity_floor: float = 0.30
    """睡一觉最多把好奇心压到这个值：醒来还是要对新鲜事有点兴趣。"""

    atmosphere_multiplier: float = 0.5
    mood_override_duration: int = 600

    daily_mood_enabled: bool = True
    """每天（或一觉睡醒）掷一次"今天的基调"，改的是各条数值曲线走得快慢。

    基调不改变她是谁：它只把精力 / 孤独 / 好奇 / 无聊 / 心潮 / 效价这几条的
    速率乘上一个倍率，剩下的照旧由数值、事件和情绪两轴自己长出来。
    """

    daily_mood_strength: float = 1.0
    """基调的作用强度，0~1。0 = 掷了也不生效（但状态页还看得见）。"""

    # ---------------- 欲求：想被碰一碰 ----------------

    desire_growth_per_min: float = 0.000231
    """欲求每分钟自然涨多少。默认约 72 小时攒满（没人碰她、也没人撩她）。"""

    desire_soft_top: float = 0.85
    """过了这条线涨速减半：不然攒满之后就一直吊在顶上，反而不像"想要"。"""

    desire_sleep_fall_per_hour: float = 0.05
    """睡着时每小时落一点：睡一觉起来没那么憋。"""

    desire_wake_keep: float = 0.7
    """睡醒时把欲求乘上这个系数。"""

    desire_relief: float = 0.25
    """被亲近一次落多少（再乘动作自己的「亲密程度」）。"""

    desire_tease: float = 0.05
    """被撩一下涨多少（再乘这个人的关系系数：越亲近越管用）。"""

    desire_low_energy_factor: float = 0.6
    desire_low_valence_factor: float = 0.2
    """累着 / 心情差的时候涨得慢：那会儿她想的是睡觉，不是贴贴。"""

    desire_push_enabled: bool = True
    """「想被碰一碰」时**交给大模型自己安排一轮**：去哪、找谁、做什么都由她定。"""

    desire_push_threshold: float = 0.75
    """欲求过这条线才会推一次（还会乘当天基调里「贴一下」那一条的权重）。"""

    desire_push_daily_max: int = 3
    """一天最多推几次（整个会话组一份）：推多了她会显得很黏。"""

    desire_push_min_interval_minutes: int = 40
    """两次之间至少隔这么久，别每一拍都问她一遍。"""

    # ---------------- 心事：她心里搁着的事 ----------------

    heart_knot_enabled: bool = True
    """心里会不会一直搁着一件事（"他上次那句话让我到现在还别扭"这种）。

    和事件线不同：事件是一条会推进的线索，心事**不推进**，只是挂着——
    影响她的语气与主动程度，时不时冒出来。所以它是"她自己的事"，不是"她在做的事"。
    """

    heart_knot_max: int = 2
    """同时最多搁几件。多了她会变成一个整天苦大仇深的人。"""

    heart_knot_decay_per_hour: float = 0.02
    """每小时淡掉多少。默认 0.02 ≈ 两天从满值淡到没有。说出来会掉得快些。"""

    heart_knot_max_days: float = 5.0
    """最多挂几天，到点无论如何都放下（写进记忆）。"""

    # ---------------- 她自己的事：答应过的、想做的 ----------------

    own_topic_enabled: bool = True
    """她自己记不记事（"答应过给他看照片""想做顿饭"这种还没做的）。"""

    own_topic_max: int = 2
    """同时最多记几件。多了她会变成一个待办清单。"""

    own_topic_days: int = 3
    """一件记多久（天）。太久了要么已经做完、要么她其实不在意，都该丢掉。"""

    @model_validator(mode="before")
    @classmethod
    def _legacy_keys(cls, data: Any) -> Any:
        return _rename_keys(data, {"social_decay_per_min": "affect_decay_per_min"})


class Limits(Permissive):
    max_actions_per_message: int = 3
    max_autonomous_per_hour: int = 6
    max_share_per_hour: int = 4
    max_replies_per_hour: int = 200
    """每小时最多回几条「被动回复」（被 @ 到 / 私聊 / 明确对她说）。

    超限之后仍然由本插件接管，但这一条**不回**（静默），也不会落回主人格。
    填 0 = 不限制。大群里压测时用它兜住回复量。
    """

    max_think_memory: int = 5
    max_messages_per_say: int = 3
    plan_valid_duration: int = 1800
    max_action_chain_depth: int = 5
    max_active_users_tracked: int = 100
    max_log_events: int = 1500
    """每个会话最多保留多少条事件日志（超过会自动删掉最旧的）。"""

    max_history_rows: int = 4320
    """每个会话最多保留多少帧数值历史（默认 1 tick 一行 ≈ 3 天）。"""

    llm_plan_min_interval_seconds: int = 900
    """两次「问 LLM 要计划」之间的最小间隔，避免每轮决策都烧 token。"""

    max_llm_plan_per_hour: int = 10
    """每小时最多几次 LLM 计划决策。"""

    max_llm_text_per_hour: int = 20
    """每小时最多几次「用 LLM 生成自主发言」。超出后**不再开口**——

    宁可这一轮什么都不说，也不要拿一句跟人设无关的通用短句顶上去。
    """

    max_tool_param_per_hour: int = 60
    """每小时最多几次「把意图翻译成工具参数」的辅助模型调用。"""

    max_arrival_decisions_per_hour: int = 20
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
    event_text: str = "事件中"
    """她正在处理一件事件时，群名片上显示的那一截（默认「事件中」）。"""


class ContentSafety(Permissive):
    blocked_words: list[str] = Field(default_factory=list)
    message_blocklist: list[str] = Field(default_factory=list)
    session_blocklist: list[str] = Field(default_factory=list)


SleepReplyMode = Literal["template", "silent", "normal"]

# 「调试输出」可以单独勾选的事件类型：key 是事件类型，值是行首图标。
# 编辑器里的勾选清单要跟这里保持一致（tests/test_units.py 有一条对齐检查）。
ECHO_EVENT_TYPES: dict[str, str] = {
    "incoming": "📨",
    "plan": "🧠",
    "action_start": "▶️",
    "action_done": "✅",
    "action": "🎬",
    "tool_call": "🔧",
    "tool_result": "📥",
    "command_call": "🧩",
    "command_result": "📤",
    "skip": "⏭️",
    "memory": "📝",
    "remember": "🗂️",
    "drowsy": "😪",
    "goodnight": "🌙",
    "goodmorning": "☀️",
    "nickname": "🏷️",
    "cancel": "🛑",
    "vision": "🖼️",
    "recall_start": "💭",
    "recall_done": "📖",
    "schedule_edit": "🗓️",
    "schedule": "📅",
    "engagement": "💤",
    "extreme": "🚨",
    "context": "🗜️",
    "wake_up": "🌅",
    "sleep_reply": "😴",
    "sleep_skip": "🤐",
    "mood_reset": "🌤️",
    "soothed": "🫂",
    "poke": "👉",
    "search_sources": "🔗",
    "weather": "🌤️",
    "search": "🌐",
    "search_digest": "🧾",
    "event": "🎰",
    "event_choice": "🌙",
    "event_action": "🧰",
    "event_check": "🎲",
    "event_result": "📖",
    "help": "🆘",
    "event_idle": "⏳",
}
# 「常用」那一档：决定、动作、工具、跳过——排查她"为什么这么做"最需要的几类
DEFAULT_ECHO_TYPES: tuple[str, ...] = (
    "plan",
    "action_start",
    "action_done",
    "action",
    "search",
    "tool_call",
    "tool_result",
    "skip",
)

# 老配置里一个事件同时带着"调用"和"返回"，拆成两栏之后要一一对上
ECHO_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "tool": ("tool_call", "tool_result"),
    "command": ("command_call", "command_result"),
}


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


class SleepStartled(Permissive):
    """迷糊惊醒：睡着时被吵得厉害，半睡半醒回两句，然后接着睡同一段觉。"""

    enabled: bool = True
    """总开关。关掉就只剩"固定文案"和"明确叫醒"两档。"""

    window_minutes: int = 5
    """"吵不吵"按这个时间窗口统计（分钟）。"""

    noise_threshold: int = 8
    """睡整觉时，窗口内被挡下多少条消息算吵。"""

    noise_threshold_nap: int = 5
    """小睡时用的门槛（沙发上被吵醒更常见，所以更低）。"""

    named_threshold: int = 2
    """窗口内被明确点她几次也算吵（@ 她 / 私聊 / 叫她名字）。"""

    skip_first_minutes: int = 30
    """睡下多久之内不惊醒（刚睡着最沉，也正撞上第一次记忆整理）。"""

    max_per_sleep: int = 1
    """一段睡眠最多惊醒几次（0 = 不许惊醒）。"""

    awake_minutes: int = 3
    """惊醒后的迷糊窗口（分钟）：这段时间能回两句，过了就安静睡回去。"""

    energy_penalty: float = 0.05
    """被吵醒扣掉的精力（固定值，不按比例）。"""

    grumpy_minutes: int = 15
    """惊醒带来的起床气持续多久（分钟）。"""

    grumpy_valence: float = -0.05
    grumpy_affect: float = 0.06


class SleepQuality(Permissive):
    """睡整觉醒来的结算：睡饱了就清爽，没睡够就有起床气。"""

    enabled: bool = True
    full_minutes: int = 360
    """睡满这么多分钟才算睡饱（默认 6 小时）。"""

    grumpy_minutes: int = 30
    """起床气持续多久（分钟）。"""

    grumpy_valence: float = -0.12
    grumpy_affect: float = 0.10
    rested_valence: float = 0.08
    rested_affect: float = -0.05


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

    drowsy: bool = True
    """睡前先迷糊一会儿（临睡期）再睡，而不是说完晚安立刻躺下。"""

    drowsy_minutes: int = 5
    """临睡期要安静这么久（没人跟她说话）才真的睡着。"""

    drowsy_max_minutes: int = 30
    """临睡期最多拖多久：到点无论如何都睡（不然又聊起来就熬到天亮了）。"""

    goodnight: bool = True
    """睡前给她一次机会说晚安：由她自己决定发给谁、发不发。"""

    goodmorning: bool = True
    """睡醒后给她一次机会说早安（同上，发不发由她定）。"""

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

    startled: SleepStartled = Field(default_factory=SleepStartled)
    """迷糊惊醒。"""

    quality: SleepQuality = Field(default_factory=SleepQuality)
    """醒来时的起床气结算。"""


class EventConfig(Permissive):
    """事件系统：她一个人待着的时候会不会遇上点事。

    频率用**每小时期望次数**表示（不是"每 N 分钟一次"）：固定间隔会带上整点感，
    一眼就能看出是定时器。命中之后由规则或打杂模型给出一个事件包。
    """

    enabled: bool = True
    """事件系统总开关（关掉 = 她只按动作与日程生活，不会遇到随机事件）。"""

    micro_per_hour: float = 1.0
    """微事件：只影响她自己的状态和记忆，不会为此说话。"""

    small_per_hour: float = 0.3
    """小事件：她要做个选择，会判定。"""

    big_per_hour: float = 0.02
    """大事件：会说、可能需要找人商量（默认约两天一次）。"""

    dwell_minutes: int = 5
    """在一个地点待够这么久，才算"在这里生活"，可以遇上事。"""

    min_gap_minutes: int = 30
    """两件事之间至少隔这么久（自动掷骰）。填 0 = 不限制。"""

    while_busy: bool = True
    """做持续动作（做饭、看书、发呆…）期间也允许发生事件。"""

    in_sleep: bool = False
    """睡觉 / 小睡时是否允许事件（默认关：睡着还出事很出戏）。"""

    max_steps: int = 6
    """一条线索最多几步；到顶必须收尾，不能无限连环。"""

    big_min_steps: int = 2
    """大事件（``big``）最少走几幕。到下限之前，模型不留伏笔也会被补上一步。

    大事件一步就收尾会显得特别潦草——"有人找我麻烦"下一句就是"我自己解决了"。
    """

    max_minutes: float = 120.0
    """一条线索最长活多久（分钟），超了也收尾。"""

    step_gap_seconds: float = 60.0
    """同一条线索里，两步之间至少隔这么久（不然会一口气连演三幕）。"""

    big_step_gap_minutes: float = 15.0
    """大事件两步之间的间隔下限（分钟）。大事件最少两幕，不拉开间隔就会连着刷屏。"""

    red_lines: list[str] = Field(
        default_factory=lambda: [
            "不要写她意识到自己是 AI / 机器人 / 程序这类自我指涉的事",
            "不要写真实群友之间的吵架、针对，也不要把她卷进群里的人际矛盾",
            "不要写流血、受伤、生病这类伤身的事（磕一下、烫一下不算）",
            "不要写灵异、恐怖、鬼怪这类吓人的事",
        ]
    )
    """不生成的事件类型（一句话一条，原样写进生成提示词）。"""

    fail_result_menu: list[str] = Field(
        default_factory=lambda: [
            "搞砸了",
            "误会了对方的意思",
            "东西找不到了",
            "白忙一场",
            "被人怼了一句",
            "网卡 / 设备出问题",
            "东西弄坏了但还能用",
            "被拒绝了",
            "迟了一步",
            "记错了",
        ]
    )
    """"没成"的写法参考（一句话一条，写进结算提示词）。

    不写这份清单时，模型会把每一次失败都写成"受伤了"——同一个梗连着出现三次就出戏了。
    """

    event_actor: Literal["admin", "all"] = "admin"
    """谁能用 `/vw event` 投递事件：admin = 只有管理员，all = 群里所有人都可以。"""

    persona_brief_chars: int = 250
    """「简易人设」的长度上限（给打杂模型的裁剪版人设，按人格缓存）。"""

    # ---------------- 日程闸门：日程撞上事件时怎么办 ----------------

    schedule_gate: bool = True
    """日程到点时如果她正在处理一件事件，先问一次"照做 / 推迟 / 今天算了"。

    只对**睡觉、小睡、换地方**这三类日程问；别的（伸懒腰、吃饭、看书）照常执行。
    没有正在进行的事件时完全不问，也不花模型调用。
    """

    schedule_delay_minutes: int = 30
    """选「推迟」时，隔这么久再检查一次这条日程。"""

    schedule_delay_limit_times: int = 2
    """同一条日程一天里最多推迟几次。到顶就得执行——睡觉这件事没有商量余地。"""

    schedule_delay_limit_minutes: int = 180
    """同一条日程一天里累计最多推迟多久（分钟），和次数谁先到算谁。"""

    stay_up_penalty: float = 1.5
    """熬夜代价：**夜里醒着**、以及为了事件推迟睡觉时，精力衰减的倍数。

    两处取较大值，不叠加。夜里该睡就睡（见「作息与夜晚」里的夜晚时段）。
    """

    max_say_lines: int = 4
    """事件里她一次最多说几句。求助那种要分几条发的场合，比平时宽松一些。"""

    max_say_lines_hard: int = 6
    """事件发言的绝对硬顶，谁都不许越（防刷屏）。"""

    event_action_calls: int = 2
    """一条线索里，她最多能为了这件事调用几次动作（查资料、做点准备）。

    每次调用之后都会把结果交回给她重新判断，所以这个数字同时也是"最多多问几轮"。
    填 0 = 事件里完全不给动作。
    """

    photo_chance: float = 0.3
    """事件每一幕推进 / 完结时，按这个概率让她拍一张图（自拍或拍照）。0 = 关闭。

    触发时会把**当前这件事的内容**写进出图意图，所以照片和剧情对得上。
    """

    photo_actions: list[str] = Field(
        default_factory=lambda: ["selfie", "take_photo"]
    )
    """事件出图用哪几个动作（按顺序挑第一个可用的）。留空 = 不出图。"""

    result_emotion: bool = True
    """判定结果要不要影响心潮 / 效价：大成功与成功偏正面，失败偏负面。

    只给一个保守的基准值，再走饱和与两轴耦合；关掉就只改能力值。
    """

    recent_window_hours: float = 24.0
    """「最近发生在我身上的事」往回看多久（小时）。超过就不写进提示词了。"""

    recent_max_lines: int = 8
    """「最近发生在我身上的事」最多几条。超了**先顶掉微事件**，再顶最旧的。"""

    open_thread_hint: bool = True
    """挂起中的那件事要不要留一行「我心里还挂着」（她做别的事时不会一直念叨）。"""

    suspend_after_minutes: int = 30
    """一件事的下一步要等超过这么久，就算"挂起"：不再占「我正在经历」那段。"""

    thread_resume_max_hours: float = 24.0
    """一件事挂多久就不再续演了（超过直接收尾写记忆）。"""

    genres: list[dict[str, Any]] = Field(
        default_factory=lambda: [
            {"name": "日常小事", "weight": 40,
             "examples": "做饭、找东西、东西坏了、收快递、打扫"},
            {"name": "人际", "weight": 20,
             "examples": "被误解、拌嘴、被冷落、被搭讪、被人跟着、遇到不讲理的"},
            {"name": "情绪与身体", "weight": 15,
             "examples": "失眠、头疼、太累、心里发闷、突然想起旧事"},
            {"name": "意外与麻烦", "weight": 12,
             "examples": "丢东西、下雨没带伞、被洒了一身、走错路、手机没电"},
            {"name": "好奇与发现", "weight": 8,
             "examples": "看到奇怪的东西、翻到旧物、听到传闻、想弄明白一件事"},
            {"name": "游戏与想象", "weight": 3,
             "examples": "打游戏卡关、看剧上头、脑补出一段剧情"},
            {"name": "外面的事", "weight": 2,
             "examples": "逛街、坐车、排队、去陌生地方"},
        ]
    )
    """事件题材 + 权重。**权重由代码掷**，不是让模型自己分配比例——

    模型每次调用都是独立采样，只会挑自己偏好的那类写，所以"请按 50/20/30 分配"
    这种话它不会遵守。做法是：代码按权重抽一个题材 → 告诉模型"这一件属于【人际】"，
    模型只负责在这个题材里编。权重为 0 就是不生成那一类。
    """

    genre_recency: int = 4
    """最近几件用过的题材先排除掉，免得连着三四次都是"日常小事"。"""

    genre_cooldown_minutes: int = 180
    """同一个题材用完之后，这么久之内不再抽中它（0 = 只按 ``genre_recency`` 去重）。"""

    genre_scale: str = ""
    """题材的尺度边界（可留空）。留空 = 不额外限制；想收紧就写一句，例如"不要写受伤"。"""

    # ---------------- 分享策略：哪些幕要说出来 ----------------

    share_micro: Literal["silent", "nodes", "always"] = "silent"
    """微事件要不要出声：``silent`` 只记进状态与记忆；``nodes`` 只在开场 / 收尾说；
    ``always`` 每一幕都可以说。"""

    share_small: Literal["silent", "nodes", "always"] = "nodes"
    """小事件：默认只在开场和收尾那两幕说，中间那些幕静默推演。"""

    share_big: Literal["silent", "nodes", "always"] = "always"
    """大事件：默认每一幕都可以说（大事件本来就是值得讲的）。"""

    event_into_chat_log: bool = True
    """事件的结果要不要写进聊天留档（作为"她自己身上发生的事"，不是她说的话）。

    写进去之后，后续对话里她能自然提起"我前两天把锅盖拧死了"这种事；
    关掉则只留在「最近发生在我身上的事」那一段。
    """

    event_digest_lines: int = 12
    """「这段时间你自己还遇上了这些事」最多留几条（顺手带一句时用）。"""

    remind_when_ignored: bool = True
    """她开口求助、群里没人接时，要不要再补一句（现由模型按人设写）。

    关掉 = 她求助完就安静等着，到点自己拿主意——不追这一句。
    """

    # ---------------- 干涉（她开口求助） ----------------

    intervene_enabled: bool = True
    """允许「需要协助」的事件向她开口求助。关掉则这类事件就地自己处理。"""

    active_seconds: float = 180.0
    """活跃等待：刚求救完的这三分钟她注意力在这件事上。"""

    idle_minutes: float = 60.0
    """总超时（分钟）：轻等待到点就自己走默认方案收尾。"""

    grace_seconds: float = 30.0
    """宽限期：她已经不等了之后，这几十秒内到的建议仍然算数。"""

    suggest_tools: bool = True
    """群友的建议会影响判定（关掉 = 只当普通聊天，不改概率也不改选择）。"""


class AbilitiesConfig(Permissive):
    """四项能力值（体力 / 智力 / 灵巧 / 心性）。"""

    enabled: bool = True
    """关掉 = 判定一律用固定值 0.6，能力值不再变化。"""

    stamina: float = 0.6
    wits: float = 0.6
    dexterity: float = 0.55
    composure: float = 0.6

    daily_limit: float = 0.10
    """每项能力值每天的累计变化上限（带符号），防止数值膨胀。"""

    step_limit: float = 0.05
    """单次事件最多改变多少。"""

    fail_growth: bool = True
    """失败涨经验：失败时更容易长能力值（这是"失败了还能再试"的依据）。"""

    @property
    def initial(self) -> dict[str, float]:
        return {
            "stamina": float(self.stamina),
            "wits": float(self.wits),
            "dexterity": float(self.dexterity),
            "composure": float(self.composure),
        }


class DeciderConfig(Permissive):
    """决策器参数：什么时候该主动搭话（插话）。"""

    enabled: bool = True
    """是否允许她主动接别人的话（插话总开关）。"""

    interject_threshold: float = 0.6
    """孤独感高于这个值（且群里正在聊天、插话开关打开）时，她会想插一句。"""

    chat_window_minutes: int = 180
    """多久之内的消息算「群里正在聊天」。"""

    chat_max_messages: int = 20
    """**已废弃**：进提示词的聊天行数搬到了 ``context.chat_lines``。

    老配置里这个键的含义是"原始条数"（默认 12，后来 60）；读取时会迁到
    ``context.chat_lines``（还是旧默认值的升到 20 行）。保留字段只为迁移时看得见。
    """

    llm_rate_min: float = 0.05
    """「要不要问大模型安排计划」的概率下限（她状态很平静时用这个）。"""

    llm_rate_max: float = 0.4
    """概率上限（她很想做点什么时用这个）。实际概率在两者之间按决策意愿插值。"""

    min_messages_to_interject: int = 2
    """窗口内至少有几条别人说的话，才值得插话。"""

    interject_cooldown_minutes: int = 20

    greet_gap_hours: int = 12
    """这个人多久没露面了算"好久没来"（小时）。0 = 关掉打招呼。

    只在群里算：有人隔了大半天又冒头，她可以戳一下、打个招呼；
    每人每天最多一次，全群每天最多 ``greet_daily_max`` 次——免得变成"谁来都点名"。
    """

    greet_daily_max: int = 2
    """一天最多这样主动打几次招呼（整个会话合计）。"""
    """两次主动插话之间的最短间隔，防止烦人。"""

    @model_validator(mode="before")
    @classmethod
    def _legacy_keys(cls, data: Any) -> Any:
        return _rename_keys(data, {"social_chat_threshold": "interject_threshold"})


class ContextConfig(Permissive):
    """群聊上下文怎么存、怎么带进提示词。"""

    chat_history_max: int = 300
    """原始群聊留档条数（持久化，重启后仍在）。超出后按下面策略处理。"""

    chat_overflow: Literal["discard", "compress"] = "compress"
    """留档超出上限时怎么办：直接丢弃最早的，或用压缩模型压成摘要。"""

    chat_compress_threshold: int = 200
    """留档达到多少条才触发一次压缩。"""

    chat_keep_after_compress: int = 30
    """压缩后保留多少条原文（更早的变成摘要）。"""

    summary_refresh_minutes: int = 10
    """两次压缩之间至少间隔多久，避免频繁调用模型。"""

    chat_note_max_minutes: int = 30
    """「刚才你们在聊什么」这句背景最多挂多久（分钟，0 = 不限）。

    它是"上一轮的话题背景"，隔了几个小时再当成"刚才在聊"就会误导她；超时就不再带上。
    """

    history_max_chars: int = 400
    """单条历史消息进入摘要前截断到多少字。"""

    open_topic_delay_minutes: int = 60
    """「还没聊完的事」隔多久之后可以提起来（分钟）。

    太短会像查户口（刚问完又问），太长就凉了。默认一小时起步。
    """

    open_topic_max_asks: int = 2
    """同一件事最多追问几次。问过还没结果就先放下，别变成催命。"""

    open_topic_days: int = 7
    """挂多久还没结果就不再当成"待续"（自动归档，不再进提示词）。"""

    open_topic_per_person: int = 2
    """同一个人身上同时最多挂几件没聊完的事。"""

    chat_lines: int = 40
    """**每个会话**「还没回过」的聊天最多带几行进提示词。

    同一个人连着说的几句算一行。别处的会话各算各的额度，不会互相挤。
    （老配置里这个值在 ``decider.chat_max_messages``，读取时自动迁过来。）
    """

    chat_answered_lines: int = 40
    """「这里刚聊过的（你已经回过话了）」最多带几行（不受时间窗限制）。"""

    chat_elsewhere_lines: int = 12
    """「你在别处同时听到的」最多带几行（只当背景）。"""

    chat_line_chars: int = 500
    """还没回过她的那批聊天记录：每条最多写多少字。

    这是她真正要读、要被回应的那几句，掐太短她就会以为对方"话说到一半"
    （以前统一按 100 字截，长一点的更新公告、群公告都只剩半句）。
    """

    chat_answered_line_chars: int = 200
    """她已经回过话的那批、以及别处同时听到的：每条最多写多少字（只当背景）。"""

    chat_total_chars: int = 16000
    """整段聊天记录（这里 + 已回过 + 别处）最多多少字。

    超了先丢最早的背景（已回过的 → 别处的），还没回过的那批最后才动。
    """

    quote_chars: int = 1000
    """群友引用一条聊天记录里没有的旧消息时，最多把那条原文写进来多少字。"""

    image_max: int = 3
    """一次回复最多把几张图直接交给能看图的主模型（自上次回复以来收到的那几张）。"""

    chat_image_inline: Literal["auto", "always", "never"] = "auto"
    """聊天记录里的图要不要直接发给主模型看。

    - ``auto``：主模型支持图像时打开（读 AstrBot 里这个 Provider 勾选的模态）；
    - ``always``：不管检测结果都发（自己确认主模型能吃图时用）；
    - ``never``：图片只以文字转述的形式出现。
    """

    chat_image_max: int = 1
    """聊天记录里最多附几张图（按时间取最近的几张，默认 1 张）。"""


class BondType(Permissive):
    """一种关系（主人 / 朋友 / 男友 / 敌人…）。

    ``cap`` 是这种关系允许到的**亲密度上限**（``ProfileConfig.levels`` 的下标）：
    普通关系聊再久也上不去，只有关系本身升级才会放宽——这就是"群友怎么聊都不能亲亲"。
    """

    name: str = ""
    slot: str = ""
    """槽位：同一个槽位里的关系互斥（``unique`` 时）：男友和女友同槽，一个人只能占一个。"""

    group: str = ""
    """同类关系：**同一个人身上，同一类只留一条**（留空 = 这条跟谁都不冲突）。

    ``slot`` 管的是"同一类关系全组只能有一个人"（男友不能有两个），
    ``group`` 管的是"同一个人身上哪几条是互相替代的"：群友 / 朋友 / 闺蜜 / 男友 / 女友
    默认同属 ``close``，她判断成新的那条之后，旧的那条自动变成"曾经"——
    所以"从朋友升到男友"是替换，而不是两条并排。
    主人、家人这类身份关系不填，照旧能和亲密关系并存。
    """

    cap: int = 2
    """这个关系允许到的**亲密度上限**（``levels`` 的下标）。

    "普通关系聊再久也不能亲亲"就靠它：群友的上限压在「熟人」，好感再高也上不去。
    """

    floor: int = 0
    """这个关系**至少**到哪一档（``levels`` 的下标，0 = 不抬）。

    关系本身也是一种态度：绑成男友之后，哪怕好感还没养起来，抱抱亲亲也该放开——
    否则提示词里会同时出现"他是你男友"和"你们还不熟，别贴贴"，自相矛盾。
    生效档位 = 好感给的档位，先被 ``floor`` 抬起来、再被 ``cap`` 压下去。

    只在好感**不为负**时生效：她可以对男友生气，惹到了就是「冷淡」，
    不能因为"关系是男友"就把冷淡翻成亲近。
    """

    unique: bool = False

    negative: bool = False
    """负面关系（讨厌的人 / 敌人）：好感为负、亲密度压到最低。"""

    aliases: list[str] = Field(default_factory=list)
    """别名：模型或用户写"男朋友"时对上这里的"男友"。"""


class IntimacyLevel(Permissive):
    """亲密度分级：门槛、称呼、这一档还不能做的动作，以及给模型的提示词文本。"""

    name: str = ""
    min_affinity: float = -100.0
    max_affinity: float = 100.0
    address: str = ""
    """她这一级怎么称呼他（留空 = 用名字）。"""

    deny: list[str] = Field(default_factory=list)
    """明确禁止的动作 id：写进提示词的"现在还不能：…"。

    刻意**不**把动作从她的候选列表里摘掉：群里常常同时有亲疏不同的人，一刀切摘掉会连
    最亲的那个人也用不了。分寸交给模型自己克制，提示词里会说明"动作按距离分档"。
    """

    proactive_per_day: int = 0
    """这一级每天最多主动找他几次。"""

    prompt: str = ""
    """这一级的提示词文本（可配置）：她现在能做什么、不能做什么。"""


def _default_profile_tables() -> tuple[list[BondType], list[IntimacyLevel]]:
    """默认的关系表 / 亲密度分级：只认 ``core.defaults`` 那一份数据。"""

    from .defaults import default_profile

    data = default_profile()
    bonds = [BondType.model_validate(item) for item in (data.get("bonds") or [])]
    levels = [IntimacyLevel.model_validate(item) for item in (data.get("levels") or [])]
    return bonds, levels


ASK_ABOUT_FIELDS_DEFAULT = ["性别", "生日", "年龄", "爱吃的东西", "所在地"]
"""「主动问」默认要打听的几件事（老清单里问的是"时区"，见 ``normalize_legacy_keys``）。"""


class ProfileConfig(Permissive):
    """用户画像 + 关系 + 好感度 + 回想回访（见 ``core/profile.py``）。"""

    enabled: bool = True
    """总开关：关掉就完全不记画像、不往提示词里带。"""

    consolidate_enabled: bool = True
    """睡眠整理开关（画像与记忆的消化 / 折叠都在那一步）。"""

    sleep_consolidate_minutes: int = 20
    """睡下多久开始第一次整理（分钟）：让"睡沉了"再整理，也避免刚躺下就动记忆。"""

    sleep_consolidate_late_minutes: int = 300
    """睡满多久补一次（分钟，默认 5 小时）：第一次整理发生在睡下 20 分钟，
    睡前最后聊的那几句还没进去，所以睡到后半段再补一次——**一段睡眠最多两次**。0 = 只整理一次。"""

    nap_consolidate: bool = True
    """小睡要不要轻整理（要点化 + 缩略版 + 一条梦），一段小睡一次。"""

    affinity_initial: float = 0.0
    affinity_min: float = -100.0
    affinity_max: float = 100.0

    affinity_reply_max: float = 3.0
    """主模型每轮能给同一个人的好感变化上限（±这个数）。"""

    affinity_daily_max: float = 10.0
    """主模型每天对同一个人能加（或减）的好感总量上限——防着靠私聊刷好感。"""

    presence_affinity: float = 0.05
    """「混脸熟」：他每出现一次加多少好感（不用 @ 她）。0 = 关掉。"""

    presence_cooldown_minutes: int = 10
    """同一个人多久之内只算一次混脸熟（连发一屏也只加一次）。"""

    presence_daily_max: float = 2.0
    """混脸熟每天给同一个人的上限。"""

    affinity_decay_per_day: float = 0.2
    """每天向 0 回落多少（疏远慢慢淡掉）。0 = 不回落。"""

    digest_chars: int = 60
    """缩略版画像最多多少字（非主要的人注入这一份）。"""

    digest_limit: int = 5
    """提示词里最多给几个"其他人"的缩略版画像。"""

    recall_daily_max: int = 5
    """回想回访：每天最多因此主动找人几次。"""

    recall_per_user_daily_max: int = 2
    """回想回访：同一个人每天最多几次。"""

    recall_topic_gap_days: int = 7
    """同一件事多久之内不重复提起。"""

    miss_growth_per_min: float = 0.0012
    """对某个人的"想念"涨多快（每分钟）：只在她没跟这个人说话时累积。"""

    miss_cooldown_min_minutes: int = 15
    """刚跟他聊过（或刚去找过他）之后，最少隔多久才会开始想他（分钟）。"""

    miss_cooldown_max_minutes: int = 60
    """最长隔多久开始想他（分钟）。每次清零后在这段区间里随机取一个，
    所以"想他"不是固定节拍——有时一下午就想，有时一整天都没想起来。"""

    miss_loneliness_weight: float = 0.8
    """孤独感给"想念"的加成：越没人陪她，越容易想起某个人。

    系数 = ``1 + 这个值 × 孤独感``：孤独 0 时不加成、孤独满了按 (1 + 这个值) 倍涨。
    默认 0.8 就是"最多快 1.8 倍"。
    """

    miss_threshold: float = 0.6
    """想念到什么程度会写进提示词（提醒她可以主动去找他）。"""

    miss_limit: int = 3
    """提示词里最多提几个"有点想的人"。"""

    miss_push_enabled: bool = True
    """「软推」：想念攒够了她会**主动去找他一次**（在哪说由她自己决定）。"""

    miss_push_threshold: float = 0.70
    """软推的触发线（比写进提示词的 ``miss_threshold`` 高：先想，真想得不行才动手）。"""

    miss_push_daily_max: int = 2
    """软推每天最多触发几次（整个会话组算一份，防着反复打扰）。"""

    ask_about_enabled: bool = True
    """关系熟了之后，主动问那些她还不知道的事（性别 / 生日 / 年龄…）。

    不是查户口：一次只提一件、同一件过一阵再问、每天有上限，而且由她自己找时机。
    """

    ask_about_fields: list[str] = Field(
        default_factory=lambda: list(ASK_ABOUT_FIELDS_DEFAULT)
    )
    """要问清哪些事。留空 = 不问。已经在画像事实里出现过的不会再问。"""

    ask_about_min_level: int = 3
    """从哪一档开始问（``levels`` 的下标，3 = 熟人）。刚认识就问生日很像查户口。"""

    ask_about_daily_max: int = 3
    """一天最多提几件（整个会话组算一份）。"""

    ask_about_person_gap_hours: int = 24
    """同一个人多久之内最多问一件（小时）。光有"同一件不重复问"不够——

    一晚上把性别、生日、年龄挨个问一遍，那就是查户口，不是关心。
    """

    ask_about_cooldown_days: int = 7
    """同一件事实多久之内不再问第二遍。"""

    grudge_enabled: bool = True
    """她会不会**记账**（他做了让她气着的事，她记着）。"""

    grudge_days: int = 7
    """一笔账记多久（天）。到期自己就淡了，并写进记忆。"""

    grudge_max: int = 2
    """同时最多记几笔（整个会话组算一份）。"""

    grudge_daily_max: int = 1
    """一天最多记几笔新的：不然她会天天记仇。"""

    grudge_level_drop: int = 1
    """气着的时候，对这个人的**生效亲密度降几档**（不改关系、不改好感数值）。"""

    call_name_blacklist: list[str] = Field(default_factory=list)
    """称呼黑名单：命中的候选直接丢掉（不想被叫的称呼）。"""

    bond_conflict_policy: Literal["reject", "replace", "ask"] = "reject"
    """关系撞车怎么办（同一个槽位只能有一个人的关系）：

    - ``reject``（默认）：不改关系，把那句话记成"他自称"，她可以认也可以不认；
    - ``replace``：新的顶掉旧的，旧的变成"曾经"；
    - ``ask``：同 ``reject``，但画像里写明"她还在犹豫"。
    """

    default_bond: str = "陌生人"
    bonds: list[BondType] = Field(default_factory=lambda: _default_profile_tables()[0])
    levels: list[IntimacyLevel] = Field(default_factory=lambda: _default_profile_tables()[1])

    @model_validator(mode="after")
    def _migrate_legacy_fields(self) -> "ProfileConfig":
        """把老配置补齐到现在这套语义。

        两件老账：

        - 默认初始关系以前用 ``bonds[].initial`` 标记，而**只有 ``default_bond`` 真的会被用来
          给新人建档**——编辑器里那个复选框改了没用，还会在关系升级时收错槽位。
          现在统一成一个来源。
        - ``floor``（关系下限）是新字段：老配置里没有，直接读成 0 就等于不生效。
          按关系名从内置默认表里补一份，这样"绑了男友却还不让抱抱"的老配置自动修好；
          名字对不上自定义关系的保持 0（不猜）。
        """

        names = [str(item.name) for item in self.bonds]
        if self.default_bond not in names:
            self.default_bond = names[0] if names else ""
        marked = [
            str(item.name)
            for item in self.bonds
            if bool(getattr(item, "initial", False))
        ]
        if marked and self.default_bond != marked[0]:
            self.default_bond = marked[0]
        try:
            from .defaults import default_profile

            defaults = {
                str(item.get("name") or ""): int(item.get("floor") or 0)
                for item in (default_profile().get("bonds") or [])
            }
        except Exception:
            defaults = {}
        for item in self.bonds:
            if "floor" in getattr(item, "model_fields_set", set()):
                continue
            item.floor = defaults.get(str(item.name), 0)
        return self

    # ---------------- 查询 ----------------

    def bond_by_name(self, value: str) -> BondType | None:
        """按关系名 / 别名找一种关系（她写的、模型写的都能对上）。"""

        text = " ".join(str(value or "").split())
        if not text:
            return None
        for bond in self.bonds:
            if text == str(bond.name):
                return bond
        for bond in self.bonds:
            if text in [str(alias) for alias in bond.aliases]:
                return bond
        return None

    def bond_cap(self, names: list[str]) -> int:
        """这几种关系里最高的亲密度上限（没有关系就用默认初始关系的上限）。"""

        caps = [
            int(bond.cap)
            for bond in (self.bond_by_name(name) for name in names or [])
            if bond is not None
        ]
        if caps:
            return max(caps)
        default = self.bond_by_name(self.default_bond)
        return int(default.cap) if default is not None else 2

    def bond_floor(self, names: list[str]) -> int:
        """这几种关系里最高的**下限**（没有关系就按默认初始关系算）。

        ``floor`` 放的是"关系本身带来的态度"：绑成男友之后，好感还没养起来也该能抱抱。
        """

        floors = [
            int(bond.floor)
            for bond in (self.bond_by_name(name) for name in names or [])
            if bond is not None
        ]
        if floors:
            return max(floors)
        default = self.bond_by_name(self.default_bond)
        return int(default.floor) if default is not None else 0

    def level_index(self, affinity: float) -> int:
        """好感度落在第几级（负数也认：冷淡 / 敌意）。"""

        value = float(affinity)
        for index, level in enumerate(self.levels):
            if float(level.min_affinity) <= value < float(level.max_affinity):
                return index
        return max(0, len(self.levels) - 1)

    def level_for(
        self,
        affinity: float,
        *,
        cap: int | None = None,
        floor: int | None = None,
    ) -> IntimacyLevel:
        """这一轮生效的亲密度级别：好感给的级别，先被关系下限**抬起**、再被关系上限**压下**。

        两个都写、而且下限比上限高时**以上限为准**（那是配置写反了，不是她想干什么）。
        **好感是负的时候下限不生效**：关系再亲也拦不住她生气——男友惹到她了照样是「冷淡」。
        """

        if not self.levels:
            return IntimacyLevel()
        return self.levels[self.level_index_for(affinity, cap=cap, floor=floor)]

    def level_index_for(
        self,
        affinity: float,
        *,
        cap: int | None = None,
        floor: int | None = None,
    ) -> int:
        """生效档位在 ``levels`` 里的下标（带上下限夹取）。

        需要"再熟一点会到哪一档"这类相邻档位信息时用它。
        """

        if not self.levels:
            return 0
        index = self.level_index(affinity)
        if floor is not None and float(affinity) >= 0:
            index = max(index, max(0, int(floor)))
        if cap is not None:
            index = min(index, max(0, int(cap)))
        return max(0, min(index, len(self.levels) - 1))


class VisionCaption(Permissive):
    """图片转述（把图变成文字再给主模型看）用的提示词。"""

    prompt: str = DEFAULT_CAPTION_PROMPT
    """**看图**提示词：只描述画面/类型/文字，不写与话题的关系。

    这段输出的内容与话题无关，所以可以按图片指纹缓存、跨话题复用。
    """

    relation_prompt: str = DEFAULT_CAPTION_RELATION_PROMPT
    """**关系**提示词：拿上一步的转述 + 当前消息 + 最近群聊，写一句「与话题的关系」。

    这一步不带图（纯文本），每次现算、不缓存。
    """

    relation_enabled: bool = True
    """是否补一次关系分析。关掉就只把画面/类型/文字交给主模型，让它自己判断关系。"""

    cache_enabled: bool = True
    """同一张图只转述一次：按图片指纹缓存，重启后仍然有效（表情包重复率极高）。"""

    cache_max: int = 500
    """最多记住多少张图。"""

    cache_days: int = 30
    """一张图的转述最多用这么久，过期后重新识别一次。"""

    forward_summary: bool = True
    """合并转发的聊天记录：让多模态模型读一遍（含里面的图）压成摘要，替换掉原来的占位符。

    关掉就还是老样子——只在聊天记录里落一句「这是一条转发的聊天记录」。
    """

    forward_prompt: str = DEFAULT_FORWARD_PROMPT
    """转发摘要的提示词（留空用内置默认）。"""

    forward_max_chars: int = 300
    """摘要最多多少字。"""


class WeatherConfig(Permissive):
    """天气：全局共享一份，按需静默刷新，写进提示词也画在编辑器地图页顶部。"""

    enabled: bool = True
    """总开关：关掉就不再静默查天气，也不写进提示词（手动查天气照常可用）。"""

    city: str = ""
    """所属城市。填了它，后台查天气就直接按这个城市查，不用模型猜。"""

    refresh_hours: float = 2.0
    """每隔几小时静默查一次。她主动查过之后，这个倒计时从头算。"""

    stale_hours: float = 24.0
    """超过这么久就不再把这份天气写进提示词（默认 1 天）。"""

    normalize: bool = True
    """查到之后先交给小模型压成「城市｜温度｜…」再存，横幅和提示词都用得着。"""


class ReplyStyle(Permissive):
    """说话的节奏：分段之间的打字延迟，以及"话太密"的判定。"""

    quote_mode: Literal["off", "always", "smart"] = "smart"
    """回复群友时，第一条消息要不要引用触发她的那条消息（她发的图片同样算第一条）。

    - ``off``：从不引用；
    - ``always``：每次都引用；
    - ``smart``（默认）：这一轮要回的是**一串消息**（她还在回上一条时又来了新的）才引用——
      单独一句对答不用顶着引用。

    只有自带引用段的平台能用（QQ / OneBot 这类，例如 aiocqhttp）；别的平台会跳过引用、
    照常把这条发出去。
    """

    interrupt_pending: bool = True
    """她还在生成回复时又来了一条消息：丢弃这次生成，让新消息重新触发一次回复。

    被丢弃的那次**不算"已经回过话"**（水位线不推进），所以下一轮仍然看得到那几条消息，
    相当于上一次回复没有触发过。关掉 = 旧行为：把两条并成一次请求一起回。
    """

    merge_wait_seconds: float = 5.0
    """收到消息先等这么久（秒）再开口：期间每来一条新消息就**重新计时**。

    对方连着打字时，她等对方说完再一起回，而不是答一句、再答一句。
    0 = 不等，收到就答。同一个会话组里的消息互相算数（群和私聊连着说也一样）。
    """

    merge_wait_max_seconds: float = 30.0
    """安静期的硬顶：一直有新消息时也不会等超过这么久，免得永远不开口。"""

    typing_delay_enabled: bool = True
    """分段发送时，按字数在两条之间停顿一下，像真的在打字。"""

    typing_delay_per_char: float = 0.03
    """每个字停顿多少秒。"""

    typing_delay_max: float = 2.5
    """单条消息最多停顿多少秒（不设上限的话长句子会等到天荒地老）。"""

    dense_window_minutes: int = 10
    """统计"她最近说了多少句"的时间窗。"""

    dense_max_lines: int = 4
    """窗口里她说超过这么多句，就提醒她这轮少说话、多做动作。"""


class DefaultState(Permissive):
    mood: str = "平静"
    energy: float = 0.6
    loneliness: float = 0.5
    curiosity: float = 0.5
    affect: float = 0.3
    boredom: float = 0.3
    desire: float = 0.2

    @model_validator(mode="before")
    @classmethod
    def _legacy_keys(cls, data: Any) -> Any:
        return _rename_keys(data, {"social": "affect"})

    @field_validator(
        "energy", "loneliness", "curiosity", "affect", "boredom", "desire", mode="before"
    )
    @classmethod
    def _clamp(cls, value: Any) -> float:
        return _clamp01(value, 0.5)


class NightConfig(Permissive):
    """夜晚时段：几点到几点算晚上。

    它同时管三件事：**睡觉动作只在夜里可选**、**夜里醒着要付熬夜代价**、
    以及规则决策里"夜里睡整觉 / 白天只小睡"的分界。
    """

    start_hour: int = 23
    """夜晚开始的小时（0~23，含）。默认 23 点。"""

    end_hour: int = 7
    """夜晚结束的小时（0~23，不含）。默认 7 点，即 23:00–07:00 算夜里。"""

    sleep_only_at_night: bool = True
    """开启后，非夜晚时段提示词里不提供「睡觉」，解析时也会把它挡掉。

    只想在白天也能睡整觉时关掉它（夜里那段熬夜代价仍然照算）。
    """


class PersonaConfig(Permissive):
    """她是谁：插件自己的一份人设。

    以前插件是"问 AstrBot：这个会话用哪份人格"——那份人格是**按会话 / 配置文件**解析的，
    所以换会话、改组、换主会话时她会跟着变成另一个人（甚至退回默认的助理人格）。
    这里给一份插件自己的角色卡，写进预设里可以整套带走，所有会话 / 会话组共用同一份。
    """

    mode: Literal["astrbot", "plugin", "append"] = "astrbot"
    """人设从哪来：

    - ``astrbot``（默认）：沿用 AstrBot 里这个会话选的人格，跟以前一样；
    - ``plugin``：用下面这份；**留空**时自动回落到 AstrBot 那份（免得不小心把人格弄没了）；
    - ``append``：AstrBot 那份打底，再把下面这份接在后面（她的设定更靠后、更"近"）。
    """

    text: str = ""
    """角色卡正文（她是谁、怎么说话、喜欢什么…）。跟着预设走。"""

    samples: list[dict[str, Any]] = Field(default_factory=list)
    """挑中的「声音样例」：她说过的、最能代表口吻的几句话。

    这些**不是台词模板**，是喂给主模型的范例——模仿样例比服从描述有效得多。
    按场景分（被撩 / 被怼 / 越界请求…），每一轮只抽 2~3 条相关的进去。
    """

    samples_per_turn: int = 3
    """每轮最多进提示词几条样例。全塞进去既费 token 又会把她锁成复读机。"""

    samples_max: int = 12
    """样例库最多留几条（超了就不让再加，而不是悄悄删掉用户挑的）。"""

    sample_candidates: list[dict[str, Any]] = Field(default_factory=list)
    """声音样例的**候选池**：生成出来、或者从聊天里挑出来的句子先攒在这儿。

    候选池里的不进提示词——用户勾选「采用」之后才会挪进 ``samples``。
    这样"生成一次就要当场决定"变成"先攒着，慢慢挑"。
    """

    candidates_max: int = 80
    """候选池最多攒多少条（超了丢最旧的）。"""


class WorldConfig(Permissive):
    world_id: str = "default"
    name: str = "小世界"
    bot_name: str = ""
    """Bot 的名字，用于动作模板里的 {bot} 占位符（留空时依次回落到群名片原名、再回落到"她"）。"""

    ui_knobs: dict[str, int] = Field(default_factory=dict)
    """「手感」滑块的档位（1~5，3 = 标准 = 内置默认值）。

    只是给编辑器记住滑块停在哪：真正生效的是那些被写进去的字段。跟着预设走，
    所以换预设时滑块位置也跟着换。缺项 = 还没动过，按标准算。
    """

    wizard_done: bool = False
    """设置向导是不是跑过一次（第一次打开编辑器时自动弹，之后只在按钮里进）。"""

    gender: Gender = "female"
    """性别：决定文案里的称呼（她 / 他 / ta）。"""

    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    """她是谁：插件自己的一份人设（跟着预设走，全会话共用一份）。"""

    tool_result_reply: bool = True
    """工具型动作拿到结果后，是否交回主模型说一句（关掉就只记进日志）。"""

    remote_action_travel: bool = True
    """允许她「想去别处做某事」：她自己没写移动时，插件替她走过去再执行。"""

    echo_types: list[str] = Field(default_factory=list)
    """调试用：这些类型的事件也作为群消息发出来（空列表 = 关闭）。

    可选项见 :data:`ECHO_EVENT_TYPES`。
    """

    echo_compact: bool = False
    """调试输出的精简模式：只发事件本身（谁调用了什么、决定了什么），
    不带参数、返回值、模型原话这些细节。"""

    echo_modes: dict[str, str] = Field(default_factory=dict)
    """每个调试输出类型的显示方式：``off`` / ``full`` / ``compact``。

    比一个全局精简开关更好用：工具调用要完整、事件只要精简，各调各的。
    老配置（``echo_types`` + ``echo_compact``）会在读取时自动迁到这里。
    """

    style_injection: bool = True
    """把情绪两轴翻译成「这一轮的表达方式」写进提示词（关掉 = 完全交给人设）。"""

    admin_ids: list[str] = Field(default_factory=list)
    """改用管理指令的 QQ 号（可多人）。AstrBot 自己的管理员始终可以执行。"""

    reply_mode: Literal["takeover", "inject"] = "takeover"
    """被 @（或消息走到大模型）时的处理方式。

    - ``takeover``：本插件接管这次回复，自己调大模型、按 JSON 动作执行并发送，同时阻止主人格重复回复；
    - ``inject``：只追加世界认知，由主人格用自然语言回复。
    """

    @field_validator("echo_types", mode="before")
    @classmethod
    def _clean_echo_types(cls, value: Any) -> list[str]:
        """老配置里的类型名换成新的，不认识的直接丢掉。"""

        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return _expand_echo_types([str(item) for item in value])

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
    events: EventConfig = Field(default_factory=EventConfig)
    abilities: AbilitiesConfig = Field(default_factory=AbilitiesConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    reply_style: ReplyStyle = Field(default_factory=ReplyStyle)
    vision: VisionCaption = Field(default_factory=VisionCaption)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    night: NightConfig = Field(default_factory=NightConfig)
    content_safety: ContentSafety = Field(default_factory=ContentSafety)
    zones: list[ZoneDef] = Field(default_factory=list)
    zone_edges: list[ZoneEdgeDef] = Field(default_factory=list)
    """跨区连线（门户对）：每条自带两端各自的房间。"""
    nodes: list[NodeDef] = Field(default_factory=list)
    edges: list[EdgeDef] = Field(default_factory=list)
    actions: list[ActionDef] = Field(default_factory=list)

    # --- 便捷索引 ---

    def is_night_time(self, hour: int) -> bool:
        """这一小时算不算「夜晚」（跨零点也算在内）。

        ``start_hour == end_hour`` 视为"没有夜晚时段"：睡觉不受时段限制，
        熬夜代价也不生效——想彻底关掉这套作息约束时就这么设。
        """

        start = int(self.night.start_hour) % 24
        end = int(self.night.end_hour) % 24
        if start == end:
            return False
        value = int(hour) % 24
        if start < end:
            return start <= value < end
        return value >= start or value < end

    def sleep_allowed_now(self, hour: int) -> bool:
        """这个小时能不能写「睡觉」这个动作。"""

        night = self.night
        if not bool(getattr(night, "sleep_only_at_night", True)):
            return True
        start = int(getattr(night, "start_hour", 23)) % 24
        end = int(getattr(night, "end_hour", 7)) % 24
        if start == end:
            return True
        return self.is_night_time(hour)

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
    intent: str = ""
    """这一步"想干什么"。工具型 / 指令型动作靠它补参数，空了会被跳过。"""

    queries: list[str] = Field(default_factory=list)
    """检索型步骤可以直接写几条查询词；留空就按 intent 让辅助模型翻。"""

    search_depth: str = ""
    """这一步想要的检索深度（quick / standard / deep）；留空按动作配置。"""

    read_pages: int = -1
    """这一步想读几篇正文；-1 表示没写。"""


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

    smart: bool = False
    """智能日程：到点时把这条日程交给大模型，让它按当下情况排一份带意图的计划再执行。

    关掉（默认）就是老老实实按动作链跑：每一步想干什么由动作链里的「意图」决定。
    """

    once: bool = False
    """一次性日程：跑完这一遍就删掉，不再每天重复。"""

    date: str = ""
    """一次性日程指定的日期（``YYYY-MM-DD``）。空着表示「下一次到点就跑」。"""

    note: str = ""
    """这条日程为什么排的（前因后果）。到点触发时带给主人格，免得忘了当初要干嘛。"""

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


class SessionGroupDef(Permissive):
    """一组共享生活的会话：几个群 / 私聊算同一个「她」。

    ``main_session`` 是这一组的代表会话：她的状态、计划、事件都存在它名下，
    tick 也按它走一份——不然她在两个群里会各老一分钟。
    """

    id: str
    name: str = ""
    sessions: list[str] = Field(default_factory=list)
    main_session: str = ""


class SessionsConfig(Permissive):
    sessions: list[SessionDef] = Field(default_factory=list)
    groups: list[SessionGroupDef] = Field(default_factory=list)


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
                "睡一整晚恢复精力（约 8 小时，夜里或凌晨、实在撑不住时用），需要先回到卧室。"
                "白天只是犯困就小睡一会儿，别在白天睡整觉。"
            ),
            "睡一整晚恢复精力（约 8 小时，夜里或精力见底时用），需要先回到卧室。": (
                "睡一整晚恢复精力（约 8 小时，夜里或凌晨、实在撑不住时用），需要先回到卧室。"
                "白天只是犯困就小睡一会儿，别在白天睡整觉。"
            ),
        }
    },
    "nap": {
        "description": {
            "打个盹，睡多久由你自己决定（10 分钟到 1 小时），睡得越久精力恢复越多。": (
                "白天犯困时打个盹（10 分钟到 1 小时，睡多久由你自己决定），缓过来接着过；"
                "睡得越久精力恢复越多。"
            ),
            "白天犯困时打个盹（10 分钟到 1 小时，睡多久由你自己决定），睡得越久精力恢复越多。": (
                "白天犯困时打个盹（10 分钟到 1 小时，睡多久由你自己决定），缓过来接着过；"
                "睡得越久精力恢复越多。"
            ),
        }
    },
    "schedule_add": {
        "description": {
            # v1.4 的文案
            "给自己加一条日程（到了点自动做某串动作）。"
            "把打算写成一句自然语言填进 intent，例如「每天早上七点去书房查新闻」。"
            "**加完不用再写别的动作**：系统会替你把时间、星期和动作链填好，再带着结果问你一次。": (
                "给自己加一条日程（到了点自动做某串动作）。"
                "把打算写成一句自然语言填进 intent，例如「每天早上七点去书房查新闻」；"
                "**只是提醒**（到点说一句话）时，再给两个参数："
                'params.at 写什么时候（「10 分钟后」「21:30」「晚上八点」都行），'
                "params.say 写到时候要说的话（1~2 句），例如 "
                '{"type":"schedule_add","intent":"十分钟后提醒主人喝水",'
                '"params":{"at":"10 分钟后","say":["该喝水啦 笨蛋"]}}；'
                "只做一次的事要说清哪天哪一刻、以及为什么要做，"
                "例如「明天下午三点去收衣服，因为主人说下午可能下雨」。"
                "**加完不用再写别的动作**：系统会替你把时间、星期和动作链填好，再带着结果问你一次。"
            ),
            # 2.0 早期版本的文案（还没带 params 说明）
            "给自己加一条日程（到了点自动做某串动作）。"
            "把打算写成一句自然语言填进 intent，例如「每天早上七点去书房查新闻」；"
            "只做一次的事要说清哪天哪一刻、以及为什么要做，"
            "例如「明天下午三点去收衣服，因为主人说下午可能下雨」。"
            "**加完不用再写别的动作**：系统会替你把时间、星期和动作链填好，再带着结果问你一次。": (
                "给自己加一条日程（到了点自动做某串动作）。"
                "把打算写成一句自然语言填进 intent，例如「每天早上七点去书房查新闻」；"
                "**只是提醒**（到点说一句话）时，再给两个参数："
                'params.at 写什么时候（「10 分钟后」「21:30」「晚上八点」都行），'
                "params.say 写到时候要说的话（1~2 句），例如 "
                '{"type":"schedule_add","intent":"十分钟后提醒主人喝水",'
                '"params":{"at":"10 分钟后","say":["该喝水啦 笨蛋"]}}；'
                "只做一次的事要说清哪天哪一刻、以及为什么要做，"
                "例如「明天下午三点去收衣服，因为主人说下午可能下雨」。"
                "**加完不用再写别的动作**：系统会替你把时间、星期和动作链填好，再带着结果问你一次。"
            ),
        }
    },
    "search_web": {
        "description": {
            "在书房上网查东西。参数由工具自己定义，不需要你填。": (
                "上网查东西（在书房用电脑）。**不知道、拿不准、或者涉及最新消息的事就用它查，"
                "不要凭印象猜、也不要编**：谁、什么时候、哪方面，写进 intent 说清楚就行，"
                "关键词由系统补全。查直播比分、天气、新闻、某个东西现在什么样，都走这个动作。"
            )
        },
        "prompt_hint": {
            "把搜索结果转成你的见闻，用第一人称，简短自然": (
                "把查到的内容讲给群里听：用你自己的口吻，挑最有用的两三点，别照抄原文、别念网址；"
                "查到什么说什么，查不到就直说没查到，别拿印象里的东西凑"
            )
        },
    },
    "check_weather": {
        "description": {
            "看一眼天气，需要 AstrBot 已注册 get_weather 工具。": (
                "查今天/现在的天气。有人问天气、或者你想提醒对方带伞加衣时用它；"
                "必须真的查到再说，工具没装或没查到就别猜。"
            )
        },
        "prompt_hint": {
            "用一句话说说天气": (
                "用两句讲讲天气本身：温度多少、体感怎么样（闷/干/风大）、要不要带伞或加衣、"
                "适合做点什么。别只感叹一句「好热」「好冷」，也别自己编温度；"
                "没查到就直说没查到"
            )
        },
    },
}


def _migrate_nickname_text(result: dict[str, Any], nodes: Any) -> None:
    """把老配置里的「状态 / 地点 → 名片文案」搬到动作与地点自己身上。

    以前文案躺在 `nickname_sync` 的一张全局表里，换预设时和动作对不上；现在文案跟着
    动作 / 地点走。这里做一次性搬迁，搬完那张表只当兜底（不再出现在编辑器里）。
    """

    config = result.get("nickname_sync")
    if not isinstance(config, dict):
        return
    status_map = config.get("status_map") if isinstance(config.get("status_map"), dict) else {}
    node_status = config.get("node_status") if isinstance(config.get("node_status"), dict) else {}
    if node_status and isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            label = str(node_status.get(str(node.get("id") or "")) or "").strip()
            if label and not str(node.get("nickname_text") or "").strip():
                node["nickname_text"] = label
    actions = result.get("actions")
    if status_map and isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            during = action.get("during") if isinstance(action.get("during"), dict) else {}
            label = str(status_map.get(str(during.get("state") or "")) or "").strip()
            if label and not str(action.get("nickname_text") or "").strip():
                action["nickname_text"] = label


def _upgrade_builtin_tool_action(action: dict[str, Any]) -> dict[str, Any]:
    """把内置工具动作从"旧默认值"升级到新版写法（只动还是旧默认值的那些）。"""


    action_id = str(action.get("id") or "")
    if action_id not in ("search_web", "check_weather"):
        return action

    changed = dict(action)
    if action.get("category") == "continuous" and (
        (action_id == "search_web" and action.get("duration_mode") == "llm")
        or (action_id == "check_weather" and int(action.get("duration") or 0) == 30)
    ):
        # 立刻调工具、拿到结果马上讲，而不是等"上网中"结束
        changed.update(
            {
                "category": "instant",
                "duration_mode": "fixed",
                "duration": 0,
                "duration_min": 0,
                "duration_max": 0,
            }
        )
    # 工具名字：老配置只写了 tool_name 时同步进 tool_names 就行。
    # 内置动作现在**不带默认工具**（用户自己挑），所以这里不再自动补备选。
    names = [str(item) for item in (changed.get("tool_names") or []) if str(item).strip()]
    if not names:
        single = str(changed.get("tool_name") or "").strip()
        if single:
            changed["tool_names"] = [single]
            changed["tool_name"] = single
    # 备选工具并进候选列表：以前"没装就换一个"和"装了都调"是两套字段，
    # 现在统一成"候选 + 用法"，合并后默认按"依次尝试"（正是原来备选的语义）。
    fallbacks = [
        str(item) for item in (changed.get("tool_fallbacks") or []) if str(item).strip()
    ]
    if fallbacks:
        merged = list(dict.fromkeys([*(changed.get("tool_names") or []), *fallbacks]))
        changed["tool_names"] = merged
        changed["tool_name"] = merged[0] if merged else ""
        changed["tool_fallbacks"] = []
        if str(changed.get("tool_mode") or "sequence") == "sequence" and len(merged) > 1:
            changed["tool_mode"] = "fallback"
    if action_id == "search_web" and "tool_flow" not in action:
        # 老配置的内置搜索还没有"调用形态"这个字段：默认升级成联网检索。
        # 之后用户在编辑器里改成"直接调用"时字段就写进去了，不会再被改回来。
        changed["tool_flow"] = "search"
    if action_id == "search_web":
        # 读几篇 / 补查几轮的旧默认值（2 / 1）偏保守，实际用起来经常"只拿到首页就收工"。
        # 只在还是旧默认值时上调一次，用户自己改过的值不动。
        if int(changed.get("search_max_reads") or 0) == 2:
            changed["search_max_reads"] = 3
        if int(changed.get("search_rounds") or 0) == 1:
            changed["search_rounds"] = 2
    return changed


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
    - 引用回复以前是个开关 `reply_style.quote_reply`，现在有三档 `quote_mode`
      （关 / 总是 / 智能），老开关按"开 = 总是、关 = 不引用"迁移。
    - 关系表新增的 `group`（同类关系只留一条）在老配置里没有这个键：按内置关系表补上，
      想让它"跟谁都不冲突"的人显式写成空串就行。
    """

    nodes = data.get("nodes")
    result = data
    # 「进提示词的聊天条数」以前按**原始条数**算、默认 12（后来改 60）：
    # 现在按**合并后的行数**算、默认 20。还是旧默认值的顺手升上去，
    # 用户自己调过的值不动。
    context = result.get("context") if isinstance(result.get("context"), dict) else {}
    # 「主动问」要打听的事：老内置清单里问的是时区，现在改成爱吃的东西。
    # 清单里**只有内置项**的（包括自己删过几项的）跟着内置清单走；
    # 一旦有自己加的项（比如"工作"）就一个字都不动。
    profile = result.get("profile")
    if isinstance(profile, dict):
        fields = profile.get("ask_about_fields")
        builtin = ["性别", "生日", "年龄", "时区", "所在地"]
        if isinstance(fields, list) and [str(item) for item in fields] and all(
            str(item) in builtin for item in fields
        ):
            result = {
                **result,
                "profile": {**profile, "ask_about_fields": ASK_ABOUT_FIELDS_DEFAULT},
            }
        # 「记的账要不要表现」这个开关删了（记仇直接生效），顺手把残留的键丢掉
        if isinstance(result.get("profile"), dict) and "grudge_visible" in result["profile"]:
            cleaned = {k: v for k, v in result["profile"].items() if k != "grudge_visible"}
            result = {**result, "profile": cleaned}
    decider = result.get("decider")
    if isinstance(decider, dict) and "chat_lines" not in context:
        # 「带几行聊天记录」以前挂在 decider 上、按原始条数算：迁到 context.chat_lines。
        legacy_lines = decider.get("chat_max_messages")
        try:
            legacy_value = int(legacy_lines) if legacy_lines is not None else 0
        except (TypeError, ValueError):
            legacy_value = 0
        if legacy_value > 0:
            result = {
                **result,
                "context": {**context, "chat_lines": legacy_value},
            }
    # 聊天记录那几项**默认值调大**了（一行/一次的窗口太小，群里别人的话还没读到就被裁掉）：
    # 只有还停在**老默认值**上的才跟着升，自己调过的数字一律不动。
    # (配置节里的键, 老默认值, 新默认值)
    bumps: tuple[tuple[str, str, tuple[int, ...], int], ...] = (
        ("context", "chat_lines", (12, 20, 60), 40),
        ("context", "chat_total_chars", (4000, 8000), 16000),
        ("context", "chat_answered_lines", (30,), 40),
        ("context", "chat_answered_line_chars", (100,), 200),
        ("context", "chat_history_max", (200,), 300),
        # 30 是「记忆勤奋度」滑块上线后的默认，80 是更早的默认
        ("context", "chat_compress_threshold", (30, 80), 200),
        ("decider", "chat_window_minutes", (60,), 180),
    )
    for section, key, old_values, new_value in bumps:
        block = result.get(section)
        if not isinstance(block, dict) or key not in block:
            continue
        try:
            current = int(block[key])
        except (TypeError, ValueError):
            continue
        if current in old_values:
            result = {**result, section: {**block, key: new_value}}
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
        # 群名片文案：以前是一张「状态 / 地点 → 文案」的全局表，现在文案写在动作和地点上。
        # 老配置里填过的映射在这里一次性搬过去，之后那张表只当兜底。
        _migrate_nickname_text(result, nodes)
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
    # 引用回复：老开关（开 / 关）→ 三档（总是 / 不引用）
    style = result.get("reply_style")
    if isinstance(style, dict) and "quote_reply" in style:
        legacy = bool(style.get("quote_reply"))
        migrated_style = {
            key: value for key, value in style.items() if key != "quote_reply"
        }
        migrated_style.setdefault("quote_mode", "always" if legacy else "off")
        result = {**result, "reply_style": migrated_style}
        style = migrated_style
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
            # 内置的「上网搜索 / 查天气」以前是持续动作：要等"上网中"结束才真的调工具，
            # 群里等好几分钟才看得到结果。还是旧默认值的话就改成瞬时动作（立刻查、立刻讲）。
            action = _upgrade_builtin_tool_action(action)
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
        result["echo_types"] = _expand_echo_types(migrated)
    # 调试输出：老的「勾选类型 + 一个全局精简开关」→ 每个类型各自的显示方式
    modes = result.get("echo_modes")
    if not isinstance(modes, dict) or not modes:
        # 走一遍别名展开：老配置里的「tool」要拆成调用 / 返回两条
        picked = _expand_echo_types(
            [str(name) for name in (result.get("echo_types") or []) if str(name).strip()]
        )
        if picked:
            compact_all = bool(result.get("echo_compact"))
            result = {
                **result,
                "echo_modes": {
                    name: ("compact" if compact_all else "full") for name in picked
                },
            }
    # 老的两个字段已经迁进 echo_modes，留在文件里只会让人以为还有效
    result = {
        key: value
        for key, value in result.items()
        if key not in ("echo_types", "echo_compact")
    }
    result = _migrate_bond_groups(result)
    return result


def _migrate_bond_groups(data: dict[str, Any]) -> dict[str, Any]:
    """给老关系表补上 ``group``：没写过这个键的按内置表补齐。

    判断依据是**键在不在**，不是值空不空：显式写成 ``""`` 表示"这条关系要能跟别的并存"，
    不能被这次迁移又填回去。
    """

    profile = data.get("profile")
    if not isinstance(profile, dict):
        return data
    bonds = profile.get("bonds")
    if not isinstance(bonds, list) or not bonds:
        return data
    from .defaults import default_world  # 局部导入：defaults 只用于这里的兜底

    builtin = default_world().get("profile", {}).get("bonds", [])
    groups = {
        str(item.get("name") or ""): str(item.get("group") or "")
        for item in builtin
        if isinstance(item, dict)
    }
    migrated: list[Any] = []
    changed = False
    for item in bonds:
        if isinstance(item, dict) and "group" not in item:
            name = str(item.get("name") or "")
            if name in groups:
                item = {**item, "group": groups[name]}
                changed = True
        migrated.append(item)
    if not changed:
        return data
    return {**data, "profile": {**profile, "bonds": migrated}}


def _expand_echo_types(names: list[str]) -> list[str]:
    """把老的事件名换成新的事件名，顺手丢掉不认识的。"""

    expanded: list[str] = []
    for name in names:
        key = str(name).strip()
        targets = ECHO_TYPE_ALIASES.get(key) or ((key,) if key in ECHO_EVENT_TYPES else ())
        for item in targets:
            if item not in expanded:
                expanded.append(item)
    return expanded


_INVISIBLE_ID_CHARS = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None
)


def clean_identifier(value: Any) -> str:
    """把 id 里的不可见字符和首尾空白清掉。

    复制粘贴（尤其是从聊天里粘配置）很容易带上零宽字符：动作 id 看着是 ``selfie``，
    实际是 ``selfie\\u200c``，模型写出来的 id 就对不上，表现为"这个动作在当前场景不可用"。
    """

    text = str(value or "").replace("\u00a0", " ")
    text = text.translate(_INVISIBLE_ID_CHARS)
    return "".join(ch for ch in text if ch.isprintable()).strip()


# 配置字段的中文说明。编辑器里滑块明细的「会改哪些参数」用它，
# 免得用户对着一串 limits.max_llm_plan_per_hour 猜这是干什么的。
FIELD_LABELS: dict[str, str] = {
    "limits.max_replies_per_hour": "每小时最多回你几次",
    "limits.max_autonomous_per_hour": "每小时最多自己动几次",
    "limits.max_share_per_hour": "每小时最多分享几次",
    "limits.max_llm_text_per_hour": "每小时最多主动说几次话",
    "limits.max_llm_plan_per_hour": "每小时最多让模型排几次计划",
    "limits.max_tool_param_per_hour": "每小时最多补几次工具参数",
    "limits.max_messages_per_say": "一次最多说几条",
    "limits.max_history_rows": "数值曲线最多留几帧",
    "decider.llm_rate_min": "最低多少概率交给模型定",
    "decider.llm_rate_max": "最高多少概率交给模型定",
    "decider.interject_threshold": "多孤独才插话（越大越难）",
    "decider.interject_cooldown_minutes": "插话后至少安静几分钟",
    "decider.min_messages_to_interject": "群里至少聊几条她才插嘴",
    "profile.miss_growth_per_min": "想念涨得多快（每分钟）",
    "profile.miss_threshold": "多想你才会主动找你",
    "profile.miss_push_daily_max": "每天最多主动找你几次",
    "profile.miss_cooldown_min_minutes": "两次主动找你的间隔（分钟）",
    "profile.miss_cooldown_max_minutes": "刚聊完之后最久隔多久才开始想你（分钟）",
    "profile.miss_loneliness_weight": "孤独感对想念的影响",
    "profile.miss_push_threshold": "想念到多少她才主动去找你",
    "state_dynamics.desire_growth_per_min": "欲求涨得多快（每分钟）",
    "state_dynamics.desire_push_threshold": "欲求到多少她会想要人贴过来",
    "profile.digest_chars": "画像缩略版最长几个字",
    "profile.digest_limit": "缩略版最多写几个人",
    "reply_style.dense_max_lines": "多少行算说话太密",
    "reply_style.dense_window_minutes": "统计说话密度的窗口（分钟）",
    "state_dynamics.chat_valence_cap": "聊一句最多涨多少效价",
    "state_dynamics.chat_valence_daily_cap": "聊天涨效价的每天上限",
    "state_dynamics.valence_decay_per_min": "效价每分钟回落多少",
    "state_dynamics.mood_override_duration": "情绪上头压过理智多久（秒）",
    "state_dynamics.daily_mood_enabled": "每天掷一次今天的基调",
    "state_dynamics.daily_mood_strength": "今日基调的作用强度",
    "events.micro_per_hour": "每小时约几件小事",
    "events.small_per_hour": "每小时约几件中等事",
    "events.big_per_hour": "每小时约几件大事",
    "events.min_gap_minutes": "两件事至少隔几分钟",
    "events.photo_chance": "每幕顺手拍张照的概率",
    "events.genre_cooldown_minutes": "同类事多久内不重复（分钟）",
    "context.chat_compress_threshold": "攒多少条聊天记录压缩一次",
    "context.summary_refresh_minutes": "多久重写一次聊天摘要",
    "context.chat_answered_lines": "提示词里带多少条聊天记录",
    "context.chat_history_max": "留档最多存多少条聊天记录",
    "context.chat_overflow": "留档超了怎么办（丢弃 / 压缩）",
}


def parse_world(data: dict[str, Any]) -> tuple[WorldConfig, list[str]]:
    """校验并修复世界配置，返回 (配置, 警告列表)。"""

    warnings: list[str] = []
    data = data or {}
    world = WorldConfig.model_validate(
        normalize_legacy_keys(normalize_edge_keys(data))
    )
    # 调过的默认值要跟上：**只有还等于老默认值**才动（用户自己改过的不碰）。
    # 不然老配置里存着 0.85 / 240 分钟这些旧数字，改了默认值也白改。
    for path, (before, after) in TUNED_DEFAULT_UPGRADES.items():
        target: Any = world
        parts = path.split(".")
        for part in parts[:-1]:
            target = getattr(target, part, None)
            if target is None:
                break
        if target is None:
            continue
        current = getattr(target, parts[-1], None)
        try:
            same = current is not None and float(current) == float(before)
        except (TypeError, ValueError):
            same = False
        if same:
            setattr(target, parts[-1], after)
            warnings.append(
                f"「{FIELD_LABELS.get(path, path)}」用的是老默认值，已按新版默认调整"
                f"（{before} → {after}）"
            )
    # 「朋友」那一档去掉了别名「老铁」：老配置里还留着的话顺手摘掉
    for bond in list(getattr(getattr(world, "profile", None), "bonds", None) or []):
        aliases = [str(item) for item in (getattr(bond, "aliases", None) or [])]
        if str(getattr(bond, "name", "")) == "朋友" and "老铁" in aliases:
            bond.aliases = [item for item in aliases if item != "老铁"]
            warnings.append("「朋友」这一档去掉了别名「老铁」")

    # 节点：去重、保证 id 非空
    seen: set[str] = set()
    nodes: list[NodeDef] = []
    for node in world.nodes:
        node_id = clean_identifier(node.id)
        if node_id != str(node.id):
            warnings.append(f"节点 id 里混进了不可见字符，已清理：{node.id!r} → {node_id}")
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
        zone_id = clean_identifier(zone.id)
        if zone_id != str(zone.id):
            warnings.append(f"区域 id 里混进了不可见字符，已清理：{zone.id!r} → {zone_id}")
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
        action_id = clean_identifier(action.id)
        if action_id != str(action.id):
            warnings.append(f"动作 id 里混进了不可见字符，已清理：{action.id!r} → {action_id}")
        if not action_id:
            warnings.append("发现一个没有 id 的动作，已丢弃")
            continue
        if action_id in action_ids:
            warnings.append(f"动作 id 重复，已丢弃后一个：{action_id}")
            continue
        action_ids.add(action_id)
        action.id = action_id
        for field, (before, after) in (BUILTIN_ACTION_UPDATES.get(action_id) or {}).items():
            # 老配置里存的是老内置值：没被用户改过就跟着更新
            if str(getattr(action, field, "") or "") == before:
                setattr(action, field, after)
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
        elif action.builtin:
            # 「内置」只属于上面那批动作。复制内置动作、手改 JSON、导入别人的预设都可能
            # 把这个标记带过来，留着它副本在编辑器里就删不掉了——这里直接清掉。
            action.builtin = False
    world.actions = actions

    # 用户画像：老配置里没有这一节、或者关系表 / 分级表被清空时，按默认补回来
    # （没有关系表和分级，画像没法判断"能做什么、不能做什么"）。
    if not world.profile.bonds or not world.profile.levels:
        from .defaults import default_profile

        defaults = default_profile()
        if not world.profile.bonds:
            world.profile.bonds = [
                BondType.model_validate(item) for item in (defaults.get("bonds") or [])
            ]
            warnings.append("关系表为空，已按默认关系补回")
        if not world.profile.levels:
            world.profile.levels = [
                IntimacyLevel.model_validate(item)
                for item in (defaults.get("levels") or [])
            ]
            warnings.append("亲密度分级为空，已按默认分级补回")

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

    known = {session.session_id for session in kept}
    groups: list[SessionGroupDef] = []
    used: set[str] = set()
    placed: dict[str, str] = {}
    for group in config.groups:
        group_id = str(group.id).strip()
        if not group_id:
            warnings.append("发现一个没有 id 的会话组，已丢弃")
            continue
        if group_id in used:
            warnings.append(f"会话组 {group_id} 重复，已丢弃后一个")
            continue
        members = []
        for item in group.sessions:
            session_id = str(item).strip()
            if not session_id:
                continue
            if session_id not in known:
                # 组里的会话必须在白名单里：不然"共享上下文"会读到永远不动的空壳
                warnings.append(f"会话组 {group_id} 里的 {session_id} 不在会话白名单里，已跳过")
                continue
            if session_id in members:
                continue
            if session_id in placed:
                # 一个会话只能属于一个组：不然"共享上下文"到底跟谁共享就说不清了
                warnings.append(
                    f"{session_id} 已经在会话组 {placed[session_id]} 里了，"
                    f"已从 {group_id} 里去掉"
                )
                continue
            members.append(session_id)
            placed[session_id] = group_id
        main = str(group.main_session or "").strip()
        if main and main not in members:
            warnings.append(f"会话组 {group_id} 的主会话 {main} 不在成员里，已改回第一个成员")
            main = ""
        if not main:
            main = members[0] if members else ""
        used.add(group_id)
        groups.append(
            SessionGroupDef(
                id=group_id,
                name=str(group.name or group_id).strip(),
                sessions=members,
                main_session=main,
            )
        )
    config.groups = groups
    return config, warnings



