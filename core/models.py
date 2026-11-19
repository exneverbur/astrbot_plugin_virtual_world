"""配置数据模型（Pydantic）。

对应设计文档第 3 章：world.json / schedules.json / sessions.json。
模型只负责校验与补全，不做业务逻辑；校验失败时尽量「修好并警告」而不是让插件起不来。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from .defaults import (
    DEFAULT_AGENT_MAX_STEPS,
    DEFAULT_CAPTION_PROMPT,
    DEFAULT_FORWARD_PROMPT,
    DEFAULT_SLEEP_DELAY_WORDS,
    DEFAULT_WAKE_WORDS,
)
# 「无聊上网池」的声明解析（T20）：纯标准库，不会和这里的模型互相引用。
from .idle_activity import parse_declaration as parse_idle_declaration


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
    "read_web_page",
    "check_weather",
    # 翻聊天记录（内置动作，用户 2026-10-26）：引擎里有专门一支，靠
    # world.history.lookup_action_enabled 收口（关掉 = 提示词与解析都不出现）。
    "lookup_history",
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
    # 亲近一下不再是"扣四分之一"：降到 0.08，并且底下留了条 0.30 的线
    "state_dynamics.desire_relief": (0.25, 0.08),
    # 效价回落快一点：一句重话打进冷脸之后别挂那么久（0.01 → 0.014）
    "state_dynamics.valence_decay_per_min": (0.010, 0.014),
    # 「你在别处同时听到的」回到 12 行：40 行背景对便宜模型太长，她会顺着另一边
    # 的旧话题往下说（2026-10-20 实机：群回复里冒出私聊刚聊过的"朋友/开支"）。
    # 老配置里存着 40 的跟着降下来；自己调过别的值就不动。
    "context.chat_elsewhere_lines": (40.0, 12.0),
    # 图片下载这两项 2026-10-20 调整（并发 2→4、超时 20→30）：老配置跟着升，
    # 自己调过的（比如设成 1 路省带宽）不动。
    "media.download_concurrency": (2.0, 4.0),
    "media.fetch_timeout_seconds": (20.0, 30.0),
    # 分段打字 2026-10-22 调慢（0.03 → 0.16，硬顶 2.5 → 5）：0.03 一秒能"敲"三十
    # 多个字，看着像消息凭空蹦出来。老配置跟着新默认走，自己调过的不动。
    "reply_style.typing_delay_per_char": (0.03, 0.16),
    "reply_style.typing_delay_max": (2.5, 5.0),
    # 追问降噪 2026-10-26（最多追 2 次 → 只追 1 次）；2026-10-28 又调回来：
    # 等 10 分钟太慢，话题早过去了，改回 3 分钟。老存档里还是旧默认就跟着改，
    # 自己调过的（比如特意设成 5 分钟）不动。
    "follow_up.wait_minutes": (10.0, 3.0),
    "follow_up.max_times": (2.0, 1.0),
    # 召回条数 5 → 8（2026-10-28）：5 条装不下"这个人 + 这几天 + 这件旧事"，
    # 一超出就被更近的记忆挤掉。老配置还是 5 的跟着升，自己调过的不动。
    "limits.max_think_memory": (5.0, 8.0),
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


GLOBAL_SEARCH_TOOL = "@global_search"
"""动作里写这个名字 = "用全局「联网搜索」里配的工具"。

2026-10-28：联网搜索的工具名统一挪进 `world.search` 之后，用户自己新建的
"联网搜索"动作在仪表盘上会报"必须选一个工具"，可它本来就该跟着全局配置走。
这里用一个不会和真工具重名的假名字表达这层引用：动作只声明"我要联网搜索"，
到底用 tavily 还是 bocha 由全局那一处决定——换工具只改一个地方。
"""


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

    is_home: bool = False
    """这里是不是"她的家"：久待之后会想出去走走，也是"出门/回家"的参照点。

    只标一个区域就够了（编辑器里是个勾选框）。一个都没标时按卧室所在的区域算。
    """


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


class ActionDiscovery(Permissive):
    """动作的**派生**发现信息（A3 追加）：简述 / 使用时机 / 关键限制 / 标签 / 详情策略。

    为什么另起一层、不摊平进 ``ActionDef``：这一段内容**不是权威定义**。它由辅助模型
    或人工写出来，会随着 ``description`` / 工具 / 地点这些权威字段变化而作废
    （所以带 ``input_hash`` / ``status`` / ``source_revision``）。摊平进去的话，
    "这句话是模型措辞还是用户定死的门槛"就再也分不出来——而口径是
    **生成说明不是授权**（设计 §4）：工具、地点、配额、关系数值一律仍以原字段为准。

    ``locked`` / ``locked_fields`` 是人工锁：锁住的字段后台整理**不许覆盖**
    （定义变了只提示冲突，不改用户写的字）。
    """

    summary: str = ""
    """一句话说清"这个动作能做什么"（索引里显示的那句）。"""

    when_to_use: str = ""
    """什么时候值得用它（"用户问胜率、近期对局时"这种）。"""

    key_constraints: list[str] = Field(default_factory=list)
    """模型补充的关键限制。**代码从权威字段提取的那部分不写在这里**
    （见 ``core/action_discovery.system_constraints``），两边在视图里合并展示。"""

    tags: list[str] = Field(default_factory=list)
    """仅用于检索 / 排序 / 编辑器展示，不参与可用性判定。"""

    detail_policy: Literal["auto", "always", "on_demand"] = "auto"
    """详情怎么给：``auto`` = 按需；``on_demand`` = 只在 ``inspect_capability`` 里给；
    ``always`` = 无法安全简述、必须带完整说明。"""

    input_hash: str = ""
    """生成这份简述时的**输入指纹**（只含权威字段，不含任何密钥）。对不上就是过期。"""

    generator_version: str = ""
    """整理器版本。换配方（提示词/字段取舍）时改它，好让旧结果整体作废。"""

    source_revision: str = ""
    """生成时依据的**有效定义版本**（见 ``core/action_registry.definition_revision``）。"""

    status: Literal["missing", "pending", "ready", "failed", "stale", "manual"] = "missing"
    """``missing`` 从没整理过；``pending`` 排队中；``ready`` 有可用结果；
    ``failed`` 试过且失败；``stale`` 定义变了、旧结果作废；``manual`` 人工写的。"""

    error_code: str = ""
    """上次失败的原因码（'' = 没错）。只放机器可判的短码，不放模型原话。"""

    updated_at: float = 0.0
    """这份派生信息最后一次被写入的时刻（由调用方给的时钟，库层不自己取时间）。"""

    needs_expand: bool = False
    """True = **无法安全简述**：索引里必须标"需展开"，完整说明照旧保留。"""

    locked: bool = False
    """整条人工锁定：后台整理一个字都不许写。"""

    locked_fields: list[str] = Field(default_factory=list)
    """按字段锁：只锁 ``summary`` / ``key_constraints`` 这种具体一项。"""

    @field_validator("updated_at", mode="before")
    @classmethod
    def _non_negative_float(cls, value: Any) -> float:
        """时间戳收成非负浮点；转不成数就按 0（= 没记过）。"""

        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0

    def is_empty(self) -> bool:
        """是不是"什么都没有"（序列化时用来决定要不要写这一层）。

        全默认值也算空：一个 ``{"discovery": null}`` 会出现在**每一条**动作的 JSON 里，
        用户手改 world.json 时要多看 76 行噪音，所以没内容就干脆不写。
        """

        return not any(
            (
                self.summary,
                self.when_to_use,
                self.key_constraints,
                self.tags,
                self.input_hash,
                self.generator_version,
                self.source_revision,
                self.error_code,
                self.locked_fields,
                self.updated_at,
                self.needs_expand,
                self.locked,
                self.detail_policy != "auto",
                self.status not in ("", "missing"),
            )
        )


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
    """默认查多深；她自己可以在 ``params.search_depth`` 里改（受 ``search_depth_max`` 夹住）。"""

    search_depth_max: Literal["quick", "standard", "deep"] = "deep"
    search_freshness_days: int = 0
    """时效硬过滤：标题/摘要里解析出的日期超过这么多天的结果直接丢（0 = 不过滤）。

    解析不出日期的结果一律保留——老页面（百科、教程）本来就不带日期。
    """
    """她允许开到的最高档：设成 ``standard`` 就谁也别想开深挖（省时间是用户的选择）。"""
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
    # 用途分组（P0-5）：留空 = 由 `core/action_purpose.py` 按 id/关键词确定性归类。
    # 用户可以填任意值；认得的内置分组进清单的对应段，认不得的原样当自定义组。
    use_purpose: str = ""
    enabled: bool = True
    """停用的动作等于"根本没有这个动作"：不进场景动作表、不进提示词、不会被大模型选中、计划里排到就跳过。"""

    discovery: ActionDiscovery | None = None
    """A3 追加：派生发现信息（简述 / 使用时机 / 关键限制 / 标签 / 详情策略 + 管理元数据）。

    **不是权威条件**，也不参与"有效定义版本"：``core/action_registry`` 与
    ``core/action_discovery`` 都把它当成派生容器算哈希时排除掉。不排除的话，
    后台每写一次简述就会让有效定义版本变一次，整理队列会把自己无限重新入队。
    没有它动作照常可用（A3 的回退视图），所以默认 ``None``。
    """

    idle_activity: dict[str, Any] | None = None
    """闲得发慌时会不会顺手做这件事（T20 无聊上网池）。

    **主插件只认这个字段，不认任何动作 id**：谁声明谁进池子——内置动作、扩展带来的
    动作、用户自己加的动作一视同仁；没声明就永远不会被那个池子抽中。

    .. code-block:: python

        {"weight": 3.0, "cooldown_minutes": 15, "min_boredom": 0.55}

    三个键都可以不写：

    * ``weight``：抽中的相对权重（默认 1.0，必须 > 0）；
    * ``cooldown_minutes``：这个动作隔多久才能再被抽中（默认 60，不能为负）；
    * ``min_boredom``：她自己额外要求的无聊门槛（默认只看全局 ``boredom_online``）。

    坏声明（不是 dict / weight ≤ 0 / cooldown < 0）**当作没声明**，不抛异常：
    一个扩展写错一个字段，不能把世界配置读崩。
    """

    @field_validator("idle_activity", mode="before")
    @classmethod
    def _normalize_idle_activity(cls, value: Any) -> dict[str, Any] | None:
        """把 ``idle_activity`` 归一成干净的小字典；坏声明一律 ``None``（＝没声明）。"""

        declaration = parse_idle_declaration(value)
        if declaration is None:
            return None
        data: dict[str, Any] = {
            "weight": declaration.weight,
            "cooldown_minutes": declaration.cooldown_minutes,
        }
        if declaration.min_boredom is not None:
            data["min_boredom"] = declaration.min_boredom
        return data

    @model_serializer(mode="wrap")
    def _serialize_with_discovery(self, handler: Any) -> dict[str, Any]:
        """序列化时**没内容的 discovery 不写出去**（老配置逐字节兼容）。

        为什么用 ``model_serializer`` 而不是在 ``model_dump`` 上 pop：pydantic v2 的
        嵌套模型由父级（``WorldConfig``）的序列化器直接处理，子类的 ``model_dump``
        根本不会被调到；只有模型级的 wrap 序列化器在嵌套时也生效。

        ``idle_activity`` 同理：没声明就不写这个键，老 world.json 存回去一个字节都不变。
        """

        data = handler(self)
        if not isinstance(data, dict):  # pragma: no cover - 兜底：pydantic 换实现时别炸
            return data
        discovery = self.discovery
        if discovery is None or (
            isinstance(discovery, ActionDiscovery) and discovery.is_empty()
        ):
            data.pop("discovery", None)
        if self.idle_activity is None:
            data.pop("idle_activity", None)
        return data

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

    valence_decay_per_min: float = 0.014
    """效价回落速度：默认 0.014 ≈ 50 分钟回到基线一半。

    以前是 0.01（一个半小时半衰）：一句重话打进冷脸之后要挂很久，聊天体验很差。
    """
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

    intimacy_valence_cap: float = 0.18
    """**亲密互动**那一轮，效价单轮最多推动多少（闲聊走 ``chat_valence_cap``）。

    为什么另开一个数、而不是把 ``chat_valence_cap`` 调大：那个 0.05 是给
    "被认真接话、被顺口夸一句"定的——它要挡住的是"聊得越多涨得越快"。可
    亲密互动是另一回事：被抱着亲着聊二十分钟，跟被夸两句显然不该同量程。
    所以亲密 tone（hug / comfort）和肢体接触动作走这条更宽的线，闲聊那本账
    一点没松。

    0.18 = 全局 0.05 的 3.6 倍：亲密一轮抵得上三句半认真的夸奖，但仍然要
    连着好几轮才顶得上一件事（事件脉冲一次能推 0.1~0.2）。
    """

    intimacy_valence_daily_cap: float = 0.45
    """**亲密互动**一天最多把效价推动多少（正负各算一份，``state.intimacy_valence_*`` 记账）。

    单独一本账，不抢闲聊那 0.15/天：不然亲密场景会被闲聊的额度卡住，或者反过来
    亲密把一天的额度一次吃光、之后连正常聊天都不涨。0.45/天 ≈ 亲密场景下两三轮
    就能看到明显起伏，但连着刷一下午也到不了满值。填 0 = 不限制。
    """

    loneliness_company_relief: float = 0.02
    """被**真的搭上话**一轮，孤独感落多少（再乘 0.4~1.0 的亲疏系数）。

    以前孤独感只涨不落，于是跟角色连着亲密聊二十分钟她还是"孤独感 1.00"，
    提示词一路写着"有点被晾着"——状态不认互动。这条补上这个方向，但单轮给得很小：
    0.02 一轮，真正管住它的是下面那条每小时上限。
    """

    loneliness_company_relief_hour_cap: float = 0.35
    """一小时里靠"有人陪着聊"最多卸掉多少孤独感（照 ``desire_relief_hour_cap`` 的写法）。

    这是主闸门。孤独感自然增长只有约 0.048/小时，0.35/小时 的意思是：
    十分钟十轮热聊最多掉 0.3，连着陪满一小时才可能从 1.0 拉到 0.65 上下——
    明显能感觉到"有人在陪她"，但几轮寒暄刷不出满血。填 0 = 不限。
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

    desire_relief: float = 0.08
    """被亲近一次落多少（再乘动作自己的「亲密程度」）。

    以前是 0.25——一次抱抱就把欲求扣掉四分之一，两次就见底了（戏还没开，
    她已经"完全不想"）。现在贴着贴着慢慢降，配合下面那条下限用。
    """

    desire_relief_floor: float = 0.30
    """亲近最多把欲求卸到这儿，不再往下压。

    那下面本来就是"淡淡的、不太想"那一段：日常抱抱摸摸不该把人推进去
    （以前没有这条，加上单次降幅 0.25，两次抱抱就把一天的欲求扣光了）。
    """

    desire_contact_warm: float = 0.005
    """已经在这一线以下时，再被亲近一下反而涨多少（× 亲密程度）。"""

    desire_relief_from_contact: bool = True
    """日常亲昵要不要「解渴」（默认**要**，这是原版自己的闭环）。

    关掉的话：抱一下、蹭一下只会更想要（走 ``desire_contact_warm``），欲求该攒就攒着。
    「日常亲昵不解渴、只有做爱才解渴」这种口味属于扩展的事——扩展通过
    ``ExtensionSpec.desire_relief`` 钩子按**这一次接触**给比例（0 = 不解渴），
    不必动这个总开关。
    """

    desire_relief_min_intimacy: float = 0.0
    """开着「日常亲昵解渴」时：多亲密的接触才算数（低于它只升温，不解渴）。"""

    desire_relief_hour_cap: float = 0.0
    """开着「日常亲昵解渴」时：一小时里靠亲昵最多卸掉多少欲求（0 = 不限）。"""

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
    reply_round_budget_seconds: float = 120.0
    """一次「回复轮」的总时限，从等安静期开始算（0 = 不限）。

    用在哪：这一轮已经花了这么久之后，**新到**的消息不再并进来重来一版——
    手上这一版照发，新消息另起一轮。模型慢、群友连着说话时，这就是
    「别让一条回复拖成几分钟」的闸门；正常一轮十几秒，碰不到它。
    """

    max_autonomous_per_hour: int = 6
    max_share_per_hour: int = 4
    max_replies_per_hour: int = 200
    """每小时最多回几条「被动回复」（被 @ 到 / 私聊 / 明确对她说）。

    超限之后仍然由本插件接管，但这一条**不回**（静默），也不会落回主人格。
    填 0 = 不限制。大群里压测时用它兜住回复量。
    """

    max_think_memory: int = 8
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

    burst_window_seconds: int = 180
    """峰谷判断的统计窗口（用户 2026-11-14）：看这么久之内别人说了多少条。"""

    burst_busy_messages: int = 12
    """窗口内至少这么多条算"热闹"：插话冷却缩短、"还在聊"的窗口放宽。

    调到用不上（比如 999）就等于关掉这条判据。
    """

    burst_quiet_messages: int = 1
    """窗口内不超过这么多条算"安静"：插话冷却拉长、"还在聊"的窗口收紧。"""


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
    "timeline": "⏱️",
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

    memory_limit: int = 1000
    """每个会话（同人格）最多留多少条记忆（容量闸，超了按淘汰顺序删）。

    以前写死 500 且"权重低的先删"——低权重的恰恰是还没被召回过的新记忆，于是越删
    越只剩老几条。默认取 1000：按她一天几十条动作+对话的量级，够放三四周的原始记忆，
    又不至于把真实库撑到十万行（实测 500 上限下老库已经贴着天花板）。
    """

    action_merge_minutes: int = 30
    """同一处、同一个动作在这段时间内合并成一条（刷新时间 + occurrence+1）。"""

    evict_protect_days: float = 7.0
    """最近这么多天被召回过的记忆，容量闸一律不删（刚用过的东西不能因容量消失）。"""

    decay_idle_days: float = 30.0
    """多久没被召回才开始衰减（这段时间内权重一动不动，只降不删）。"""

    decay_half_life_days: float = 30.0
    """开始衰减之后，权重每过这么多天减半（下限见 decay_floor）。"""

    decay_floor: float = 0.1
    """衰减的权重下限：再旧也停在 0.1，绝不删行。"""

    consolidate_memory_window: int = 240
    """整理时把最近多少条旧记忆喂给模型（旧记忆候选池，合并/折叠都从这个窗口挑）。

    以前写死 120：库里有几百条时，早先的记忆根本进不了"可以合并"的清单，模型只能
    看着最近一段说"没有可折叠的项"。240 是"够看见一两个月"与"提示词别太长"的折中。
    """


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

    delay_minutes: int = 20
    """「等会儿再睡」的延迟窗口（分钟）。

    他说了这类话就往后推这么久：窗口里不排睡觉、也不催她睡。
    连着说就续期。窗口里她照样可以是迷迷糊糊的临睡状态（``drowsy``），
    但不会真的睡过去——见 ``WorldState.sleep_delay_until``。
    """

    delay_words: list[str] = Field(default_factory=lambda: list(DEFAULT_SLEEP_DELAY_WORDS))
    """命中这些词就算「等会儿再睡」（默认要求 @ 她，见 ``delay_requires_mention``）。"""

    delay_requires_mention: bool = True
    """是否要求 @ 她（私聊等同）才算数：群里别人说「等会儿再睡」不是对她说的。"""

    pressure_enabled: bool = True
    """是否用困意（``WorldState.sleep_pressure``）决定该不该睡，而不是只看精力。"""

    pressure_threshold: float = 0.85
    """困意攒到多少她真的撑不住（0~1）。到这条线才会自己排睡觉。"""

    pressure_gain_per_hour: float = 0.5
    """深夜每小时攒多少困意。"""

    pressure_fall_per_hour: float = 0.6
    """白天每小时回落多少困意（天亮了自然清醒）。"""

    pressure_sleep_fall_per_hour: float = 2.0
    """睡着时每小时回落多少困意（睡一觉就缓过来）。"""

    pressure_delay_multiplier: float = 0.3
    """延迟窗口里困意涨得慢多少（他让她别睡，她也乐意再撑一会儿）。"""

    pressure_chat_relief: float = 0.05
    """每收到一条有人跟她说话的消息，困意被压下去多少。"""

    pressure_touch_relief: float = 0.2
    """被碰一碰 / 亲密接触一次压下去多少困意（这是"亲密优先"那一条）。"""

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

    big_per_hour: float = 0.028
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

    # ---------------- 跨度与独占（T15-S2） ----------------
    #
    # 事件不再全是"一回合内结束"：``span`` 分三档（scene / hours / days），
    # 每档有自己的存活上限和节奏。下面这些是**缺键时的默认值**：
    # ``EventConfig`` 是 Permissive，老 world.json 没有这些键照样能跑。

    scene_max_minutes: float = 120.0
    """``scene`` 档最长活多久（分钟）：一回合内的事，超了就该收尾。"""

    hours_max_hours: float = 72.0
    """``hours`` 档最长活多久（小时）。"""

    days_max_days: float = 30.0
    """``days`` 档最长活多久（天）：跨天的事按剧情收尾，这个只是最后的天花板。"""

    days_max_hours: float = 0.0
    """``days`` 档的时长上限（小时）；填了正数就按这个算，0 表示用 ``days_max_days``。"""

    hours_max_steps: int = 12
    """``hours`` 档最多几步。"""

    days_max_steps: int = 40
    """``days`` 档最多几步（每天最多一步，所以约等于 30 天 + 收尾幕）。"""

    hours_max_step_gap_minutes: float = 90.0
    """``hours`` 档单步间隔上限（分钟）；下限写死在代码里（30 分钟，防刷）。"""

    days_wake_hours: float = 12.0
    """``days`` 档挂起成 ``dormant`` 之后，隔多久可以被重新唤醒（小时）。"""

    ambient_max: int = 2
    """同时允许几条 ``ambient``（可并存）事件。"""

    exclusive_cooldown_minutes: float = 30.0
    """一条 ``exclusive`` 收尾后，隔多久才允许下一条独占开场（分钟）。"""

    retry_on_fail: bool = True
    """事件包生成失败时是否重试一次（仍失败才记 ``event_skip``）。"""

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

    photo_tendency: float = 0.3
    """**每一幕**拍不拍图的倾向：每一幕（每次推进 / 完结）各掷一次。

    同一幕最多拍一张，剧情推进到下一幕就重新掷——所以"这件事演了几幕"会自然
    变成几张照片，总张数由 :attr:`photo_max_per_event` 兜着。
    旧配置里写 ``photo_chance`` 的照样认（两个名字含义相同，优先读这个）。
    """

    photo_chance: float = 0.3
    """旧名字（每幕各掷一次）。含义与 ``photo_tendency`` 相同。"""

    photo_max_per_event: int = 3
    """一件事最多拍几张照片（防刷图）。填 0 = 不限，完全按每一幕的掷骰来。"""

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
            {
                "name": "日常小事",
                "weight": 15,
                "weights": {"micro": 45, "small": 15, "big": 0},
                "examples": (
                    "她的{某样东西}被{别人}顺手拿走/弄坏了|她去做{一件小事}，卡在{某个具体的坎}上|"
                    "她买回来/收下的东西跟想的不一样|{某个常用的东西}突然不好使了，偏她急着用|"
                    "她答应过{某人}的一件小事今天到期了|她收拾出{一样很久没见的东西}，想起{一段旧事}"
                ),
                "places": "厨房|书房|客厅|阳台|卧室",
                "cast": "合租的人|楼下便利店老板|隔壁的猫",
                "stakes": "晚饭的食材|刚买回来的东西|答应别人的事要黄|今晚的清净",
            },
            {
                "name": "情绪与身体",
                "weight": 5,
                "weights": {"micro": 25, "small": 5, "big": 0},
                "examples": (
                    "半夜醒了就再睡不着，脑子里{某件事}反复过|{某个部位}隐隐地疼，她硬撑着|"
                    "她一整天提不起劲，连{平时喜欢的事}都不想碰|{某件旧事}突然被{一个由头}勾出来|"
                    "她饿过头了，手有点抖"
                ),
                "places": "卧室|窗边|浴室",
                "cast": "",
                "stakes": "明天要用的精神|答应自己的事又泡汤|不想让人看出她不舒服",
            },
            {
                "name": "好奇与发现",
                "weight": 15,
                "weights": {"micro": 5, "small": 15, "big": 5},
                "examples": (
                    "她在{某个角落}翻到{一样东西}，不知道是谁的|她听见{某个人}在说一件让她在意的事|"
                    "她发现{某个日常的东西}有点不对劲|她看到一张写着{几个字}的纸条/消息|"
                    "她想弄明白{一件一直没搞懂的事}"
                ),
                "places": "旧货市场|阁楼|手机里|楼下信箱",
                "cast": "看着眼生的邻居|送快递的人",
                "stakes": "白折腾一晚上|被人发现她在打听|本来能弄清的事又断了",
            },
            {
                "name": "游戏与想象",
                "weight": 5,
                "weights": {"micro": 5, "small": 5, "big": 0},
                "examples": (
                    "她打{一个关卡}卡在同一个地方|她追{一部剧/一本小说}追到停不下来|"
                    "她脑补{一段剧情}，越补越真|她在{游戏里}被人摆了一道"
                ),
                "places": "房间里|沙发上|被窝里",
                "cast": "游戏里遇到的队友",
                "stakes": "一晚的睡眠|刚攒起来的好心情",
            },
            {
                "name": "人际",
                "weight": 30,
                "weights": {"micro": 10, "small": 30, "big": 15},
                "examples": (
                    "她被{某个人}当成{免费劳力/传话筒}|{某人}当众说了句让她下不来台的话|"
                    "她被人放了鸽子|有人跟她套近乎，她拿不准对方想要什么|"
                    "{某人}把她的好意当成了理所当然|她跟{某人}拌了两句嘴，话赶话"
                ),
                "places": "群里的线下聚会|常去的那家店|楼道|工位旁边",
                "cast": "熟但不亲的熟人|刚认识的人|一直不太对付的人|她帮过的人",
                "stakes": "这点交情|她在这群人里的面子|以后还找不找得着人帮忙|一口答应下来的事",
            },
            {
                "name": "意外与麻烦",
                "weight": 20,
                "weights": {"micro": 10, "small": 20, "big": 25},
                "examples": (
                    "她出门没带伞，被雨堵在半路|她{一样要紧的东西}丢了/落在别处|"
                    "她把{别人的东西}弄坏了，得赔|她走错路，越走越偏|"
                    "{某个设备}突然没电/坏了，耽误了要紧的事|她身上沾了{一片污渍}，还得见人"
                ),
                "places": "街上|地铁|楼道|店里",
                "cast": "催得很急的客户|看着她赔钱的人",
                "stakes": "这个月的余钱|今天本来要办成的事|别人对她的信任",
            },
            {
                "name": "外面的事",
                "weight": 10,
                "weights": {"micro": 0, "small": 10, "big": 10},
                "examples": (
                    "她排队排到一半发现排错了|她在陌生地方找不到回去的路|她坐车坐过站|"
                    "她逛到一家没见过的店|她在外面被人问路/搭话"
                ),
                "places": "商业街|车站|陌生的街区|夜市",
                "cast": "同车的陌生人|摆摊的人",
                "stakes": "晚上的时间|回去的末班车|身上带的钱",
            },
            {
                "name": "危险与冲突",
                "weight": 0,
                "weights": {"micro": 0, "small": 0, "big": 20},
                "examples": (
                    "她在{地点}被{陌生人}一路跟着|她和{某人}从拌嘴升级到推搡|"
                    "她发现有人在翻她的东西|她走夜路，{交通工具}半路出了状况|"
                    "她被几个人堵在{一个不好脱身的地方}|她听见有人吵架，越吵越凶，殃及到她"
                ),
                "places": "巷子|夜市|末班地铁|楼下|陌生街区",
                "cast": "喝多了的陌生人|一直跟着的人|看着不好惹的几个人|吵架的那两个人",
                "stakes": "身上带的钱|今晚能不能安全回去|手机还在不在|她攒下的那点胆气",
            },
            {
                "name": "机会与得失",
                "weight": 0,
                "weights": {"micro": 0, "small": 0, "big": 15},
                "examples": (
                    "有人给她一个来钱的路子，但要先垫{一笔钱}|她捡到/被托付{一样值钱的东西}，失主在找|"
                    "她被许诺一个位置，代价是要{得罪某人}|她撞见{一桩买卖}的门道，掺一脚就得担风险|"
                    "有人请她帮个{有好处也有坑}的忙"
                ),
                "places": "店里|路边摊|旧货市场|别人家门口",
                "cast": "看着很急的失主|笑得过分热情的中间人|想拉她入伙的人",
                "stakes": "要垫出去的钱|本来能到手的机会|跟某人的关系|她在这一带的信用",
            },
            {
                "name": "旧账与人情",
                "weight": 0,
                "weights": {"micro": 0, "small": 0, "big": 10},
                "examples": (
                    "{以前欠过她的人}突然来找她，开口就要{一件难办的事}|她以前做过的一件事被人当众提起|"
                    "她不得不去还{一笔旧人情}|{旧相识}带着{一个旧承诺}回来找她|"
                    "当年那件事的{另一个人}出现了"
                ),
                "places": "常去的店|老地方|群里的线下聚会|对方约的地方",
                "cast": "旧相识|被她得罪过的人|当年一起的人",
                "stakes": "当年那点交情|欠着的那笔人情|她在圈子里的名声",
            },
        ]
    )
    """事件题材 + 权重。**权重由代码掷**，不是让模型自己分配比例——

    模型每次调用都是独立采样，只会挑自己偏好的那类写，所以"请按 50/20/30 分配"
    这种话它不会遵守。做法是：代码按权重抽一个题材 → 告诉模型"这一件属于【人际】"，
    模型只负责在这个题材里编。权重为 0 就是不生成那一类。

    T15 起支持按档位分权：``weights`` 是 {"micro": 45, "small": 15, "big": 0}，
    抽事件时按当前档位取对应权重；没写 ``weights`` 的旧题材退回单一 ``weight``。
    这样"日常小事"不会再占掉 50 小时才轮到一次的 big 档位。

    ``examples`` 用 ``|`` 分隔，写成**有洞的形态**（``她的{某样东西}被{别人}顺手拿走``），
    每次只抽几条给模型，并在提示词里禁止照抄——写成品句必然被抄成同一件事。
    ``places`` / ``cast`` / ``stakes`` 是可选的素材维度：编的时候告诉模型
    "这件事通常发生在哪、可能有谁、输了会失去什么"，比只给一串名词更具体。
    """

    genre_recency: int = 4
    """最近几件用过的题材先排除掉，免得连着三四次都是"日常小事"。"""

    genre_cooldown_minutes: int = 180
    """同一个题材用完之后，这么久之内不再抽中它（0 = 只按 ``genre_recency`` 去重）。"""

    genre_scale: str = (
        "不写血腥、不写违法操作细节、不写自伤；危险到「差点出事 / 受了点伤」为止，不写死亡"
    )
    """题材的尺度边界（可留空）。留空 = 不额外限制。

    T15 起默认收紧一档：``危险与冲突`` 这类题材要的是"紧张 + 后果"，不是猎奇。
    """

    # ---------------- 分享策略：哪些幕要说出来 ----------------

    share_micro: Literal["silent", "nodes", "always"] = "silent"
    """微事件要不要出声：``silent`` 只记进状态与记忆；``nodes`` 只在开场 / 收尾说；
    ``always`` 每一幕都可以说。"""

    share_small: Literal["silent", "nodes", "always"] = "nodes"
    """小事件：默认只在开场和收尾那两幕说，中间那些幕静默推演。"""

    share_big: Literal["silent", "nodes", "always"] = "always"
    """大事件：默认每一幕都可以说（大事件本来就是值得讲的）。"""

    event_into_chat_log: bool = True
    """事件的结果要不要留档成她自己的**记忆**（不是她说的话，也不进聊天记录）。

    T15-S1 起这条开关只决定"要不要沉淀成一条 SCENE 记忆"：以前它会把事件旁白
    写成 ``recent_chat`` 里的一行（带井号标题、第三人称），非常出戏；现在不再往
    聊天记录里写，关掉则连记忆也不留，只靠「最近发生在我身上的事」那一段。
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
    """「你在别处同时听到的」最多带几行（只当背景，默认 12）。

    以前是 12，中间为了"别处刚发生的事别被整段截掉"提到过 40；但 40 行背景对便宜
    模型来说太长，她容易顺着另一边的旧话题说（2026-10-20 实机：群回复里冒出私聊
    刚聊过的"朋友/账本"，看着驴唇不对马嘴）。要更清楚地记得别处的事，再调大；
    上限之外还有 ``chat_total_chars`` 兜着，调到 40 也不会撑爆提示词。
    """

    chat_line_chars: int = 500
    """还没回过她的那批聊天记录：每条最多写多少字。

    这是她真正要读、要被回应的那几句，掐太短她就会以为对方"话说到一半"
    （以前统一按 100 字截，长一点的更新公告、群公告都只剩半句）。
    """

    chat_answered_line_chars: int = 200
    """她已经回过话的那批、以及别处同时听到的：每条最多写多少字（只当背景）。"""

    chat_total_chars: int = 16000
    """整段聊天记录（这里 + 已回过 + 别处）最多多少字。

    超了从最早的背景丢起（别处的 → 已回过的 → 更早的概览），
    「还没回过」的那批不参与丢弃，只会单条截断。
    """

    quote_chars: int = 1000
    """群友引用一条聊天记录里没有的旧消息时，最多把那条原文写进来多少字。"""

    use_host_history: bool = False
    """回话时要不要把 AstrBot 自己那段会话历史（``req.contexts``）也交给她。

    默认不带。宿主那份历史里有**别的插件 / 别的机器人**说过的话，而且可能已经是
    几个小时前的：2026-10-20 实机，群里有人和另一个 bot 聊过「不谈朋友开支就少」，
    她那轮提示词的宿主历史里正好只有这两条，于是她在群里接了一句「我哪谈朋友啦
    一个人账本清清爽爽的」——看着完全答非所问。

    她自己的聊天留档（「这里刚聊过的」「你在别处同时听到的」）已经带时间、带
    「这句是冲别人说的」标注，那份才是可信的上下文。真要复用宿主的会话历史
    （例如别的插件往里塞了有用的东西）再打开。
    """

    filter_host_context: bool = True
    """其他插件写进宿主 system_prompt 的内容里，要不要摘掉跟她无关的那几段。

    2026-10-20 实机取证（别的机器上的一整份提示词，「其他插件提供的上下文」7042 字符，
    整份 39199）：其中 AstrBot 内置技能清单 3283 字符（documents / pdf / skill-creator /
    spreadsheets，还附了 ``/root/.local/.../SKILL.md`` 绝对路径，要求她先用 shell ``cat``
    读）、宿主运行环境说明 1245 字符（Linux、``astrbot_shell_session``、
    ``Current workspace: /usr/local/astrbot/data/workspaces/...``）——她既没有 shell，
    也不该去读宿主安装目录，这些只会带偏。

    摘的是：内置 Skills 段、宿主运行环境段、以及「工具使用规范」段（前提是那段点名的
    工具一个都不在她手上）、还有没有 ``send_ai_voice`` 时的语音配置行。
    ``[System Note]``（昵称不是身份）、``[系统状态]``、``[当前群身份]`` 一律留着——
    那是防混淆的，删了反而危险。
    """

    image_max: int = 3
    """一次回复最多把几张图直接交给能看图的主模型。

    取的是**发送范围内的聊天记录里最近的这几张**（不管它是这一条消息带的、还是前面几条带的），
    按时间序在记录里标 图1/图2/…；更旧的图只以文字转述出现。
    """

    chat_image_inline: Literal["auto", "always", "never"] = "auto"
    """聊天记录里的图要不要直接发给主模型看。

    - ``auto``：主模型支持图像时打开（读 AstrBot 里这个 Provider 勾选的模态）；
    - ``always``：不管检测结果都发（自己确认主模型能吃图时用）；
    - ``never``：图片只以文字转述的形式出现。
    """

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

    dm_send_enabled: bool = True
    """能不能主动私聊"从没配过"的人（用户 m30450）：通讯录里认识的人都能试一次。

    默认开：私聊一个没在会话列表里出现过的人本来就是她该会做的事；发不出去
    时不会静默——她下一轮会拿到一句旁白「刚才那句话没送到」，提不提由她自己决定
    （见 ``_note_failed_dm``）。
    有些平台禁止陌生人私聊，那就把它关掉。
    """

    dm_session_template: str = ""
    """私聊会话 id 怎么拼，例如 ``aiocqhttp:FriendMessage:{user}``。

    留空按当前会话的平台前缀自动拼（``<平台>:FriendMessage:<user_id>``）。
    """

    dm_daily_max: int = 3
    """主动私聊"没配过的人"每天最多几次：防止她一高兴挨个敲人。"""

    allow_webchat_target: bool = False
    """她**主动**说出的话能不能落在 WebUI 测试会话（``webchat:*``）上。

    线上那回（2026-10-26）：组主页是 WebUI 测试私聊，她「想你」时按组主页算落点，
    24 小时 35 段输出里 25 段进了 ``webchat:FriendMessage:webchat!astrbot!vwprivate``，
    主人在 QQ（``snowluma``）一条都没收到。

    默认 False：主动发言（``miss_push`` / 「想被碰一碰」/ 自主开口 / 追问）**不落**
    ``webchat:*``；解析不出真正收得到的会话来就不发，并写一条 ``delivery_skipped``。
    只有"整组就一个 WebUI 测试会话、想让她在那儿试"时才打开。
    """

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

    miss_min_level: int = 3
    """从哪一档关系开始才会"想他"（``levels`` 的下标，3 = 熟人）。

    想念是熟起来之后的事：只在群里露过两面、还是"客气"档的人，
    不该出现在「你有点想他们了」里，也不该让她主动去找。
    0 = 不限（旧行为：只要想念值够就写）。
    """

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

    default_bond: str = "群友"
    """新认识的人先算什么关系（用户 m11008：默认从「陌生人」改成「群友」）。"""

    bond_chosen: bool = False
    """主人是否亲手选过初始关系；选过一次就不再被"默认值升级"改掉。"""

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
        # 亲手选过的默认关系优先：老存档里的 initial 标记只是"当年的默认"，
        # 不该把主人后来选的改回去（用户 m11008 的「群友」就被这条反悔过）。
        if marked and not self.bond_chosen and self.default_bond != marked[0]:
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

    这段输出的内容与话题无关，所以可以按图片指纹缓存、跨话题复用；
    "跟正在聊的事有没有关系"由主模型自己判断（2026-10-21 删掉了单独的二次调用）。
    """

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


class SearchConfig(Permissive):
    """联网搜索（用户 m10393）：不再挂在动作里，改成全局一份。

    以前这些字段长在 `search_web` 这个内置动作上——于是每个地点都得单独配一遍，
    用户还得先认识"联网检索设置"那一堆输入框。现在动作彻底隐藏，配置统一在
    全局设置 → 高级功能 → 联网搜索里；动作上残留的字段只在"全局没配"时兜底。
    """

    enabled: bool = True
    tool_names: list[str] = Field(default_factory=list)
    """搜目录用哪些宿主工具（比如 deepseek_web_search）；空 = 这个动作没法搜。"""
    reader_tool_names: list[str] = Field(default_factory=list)
    """读正文用哪些工具（能传网址、返回正文）；空 = 只吃摘要。"""
    tool_mode: Literal["sequence", "fallback", "smart"] = "sequence"
    depth: Literal["quick", "standard", "deep"] = "standard"
    """默认档位：她自己在 params.search_depth 里写了就以她写的为准。"""
    depth_max: Literal["quick", "standard", "deep"] = "deep"
    """她最多能开到多深（用户 m9296：不要"深挖配额"，只留这道上限）。"""
    max_queries: int = 3
    max_reads: int = 2
    rounds: int = 1
    freshness_days: int = 365
    """太旧的结果直接丢（0 = 不按日期过滤）：用户 m09693 要的时效硬过滤。"""
    budget_seconds: float = 20.0
    """一轮检索最多花多久（秒）。"""
    topic: str = ""
    query_template: str = ""
    cite: bool = True
    """讲结果时允不允许她补一条来源链接（用户 m09693 要的给出处）。"""


class MediaConfig(Permissive):
    """媒体资产（阶段B）：把图真的存一份到本地，按内容寻址复用。

    以前图片只留一个平台地址：临时签名会过期、协议端的中转文件会被清掉，
    过几天再看那行留档就只剩一句"她发过图"。这里把字节留下来：
    同一个文件（同内容）永远同一个 ``media_id``，换链接也认得出来。
    """

    enabled: bool = True
    """总开关：关掉就退回旧行为（只留地址，不下载、不落盘）。"""

    max_file_mb: float = 10.0
    """单个文件最多多大（超过就放弃，只留地址）。"""

    max_total_mb: float = 512.0
    """整个资产库最多占多大（超了由清理按保留策略腾地方）。"""

    retention_days: float = 14.0
    """没被引用的资产保留多少天。"""

    keep_entries: int = 5000
    """资产条目数上限（按最后使用时间 LRU）。"""

    download_concurrency: int = 4
    """同时在下载几个文件（协议端的图床大多不禁并发，但别一次开一堆）。

    2026-10-20 从 2 提到 4：群里一次九张图时，2 路下载 + 20 秒超时把 hook 段拖到
    20~40 秒（timeline 实测），这一步是纯 I/O 等待，4 路不会更费钱。
    """

    fetch_timeout_seconds: float = 30.0
    """单个请求最多等多久。

    2026-10-20 从 20 提到 30：QQ/mika 图床偶发慢响应，20 秒会在"其实下得动"的图上
    判死；30 秒配合并行下载，总时长的上界基本不变。
    """

    caption_max_per_round: int = 6
    """一轮最多转述几张（超出部分只留编号，等下一轮再说）。

    转述是一次视觉模型调用，一轮十张既慢又贵；聊天里真正要看清楚的通常是最近几张。
    2026-10-20 新增：上限单独一个旋钮，不动 ``max_per_round``（那是"收进资产库"的额度）。
    """

    max_per_round: int = 12
    """一轮最多收几张（超出的只留地址：别人一次刷 20 张图不该拖住这一轮）。

    2026-10-20 从 4 提到 12：群里一次发八九张是常态，4 张的额度让"8 张里只看 4 张"
    成了默认行为；已经收过同一串地址的图（``cached``）不占这个额度。
    """

    reaction_enabled: bool = True
    """看图主动接话：群里有人发了张有意思的图，她可以自己接一句。

    用户口径（2026-10-19）："看图有趣就说话不应该依赖意图路由，用户有可能没装"，
    所以这一步走 VW 自己的收消息链路，和 ``astrbot_plugin_intent_router`` 无关。
    """

    reaction_cooldown_minutes: int = 15
    """同一个群两次"看图开口"至少隔多久（分钟）。标准档 15 分钟；0 = 不限。"""

    reaction_window_minutes: int = 5
    """收到图之后多久之内还值得开口（分钟）：超过就当成"这事儿已经过去了"。"""

    reaction_interest: int = 2
    """有趣分到几才算值得开口（0~3，默认 2 =「有点意思」；0 = 不评，看到就考虑）。"""

    reaction_max_images: int = 6
    """一次最多看几张（多发的只当背景，不逐张转述）。"""


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
    """**已废弃**（用户 m09933）：回复不再有"引用档位"，也不默认引用触发她的那条。

    现在的规则只有一条：她在说那句话时自己写了方括号编号（``"quote": "3"``）才引用，
    没写就一条都不带；按内容抄字匹配的兜底也已删除（会引到别人头上）。
    字段保留只为兼容旧 ``world.json``，改动它不会再影响行为。
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

    mention_wait_seconds: float = 3.0
    """群里真的 @ 了她时先等这么久（秒），比普通安静期短。

    @ 的意思是"现在答我"，压满一整个安静期只是把延迟叠在延迟上；但也不能不等——
    @ 完紧跟着补半句是常态，给他这点时间比答两遍体面。0 = 收到就答。
    """

    mention_wait_max_seconds: float = 10.0
    """@ 时的硬顶：对方一直补话，这一轮也不会等超过这么久。0 = 不再延长。"""

    typing_delay_enabled: bool = True
    """分段发送时，按字数在两条之间停顿一下，像真的在打字。"""

    typing_delay_per_char: float = 0.16
    """每个字停顿多少秒（0.16 ≈ 一秒六个字，像真人拿手机敲）。"""

    typing_delay_max: float = 5.0
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


class FollowUpConfig(Permissive):
    """追问（用户 m27236）：她说了句在等回话的话，隔几分钟没人接就再问一句。

    只在她**自己真的说出去**之后才起算（平台没收下的不算）；对方一开口就作废；
    连续追 ``max_times`` 次就放下——追着问是催命，不是聊天。

    默认等 10 分钟、只追 1 次（用户 2026-10-26：追问太啰嗦，几乎每句都追，
    而且几分钟后真的又回来追一句）。宁可少追，也不要催。
    """

    enabled: bool = True
    """总开关。默认开：她问了一句就没人影，比"多问一句"更像坏掉。"""

    wait_minutes: float = 3.0
    """等多久没回应才追（分钟）。0 = 到下一拍就追（不建议）。"""

    max_times: int = 1
    """连续最多追几次（默认只追一次）。0 = 不追（等于关掉这套机制，但保留判断与记录）。"""


class ActionSelectionConfig(Permissive):
    """分层候选与加权抽样（阶段 D）：硬过滤 → 按目的分层 → 有 seed 的抽样。"""

    enabled: bool = False
    """总开关。**默认关**（用户 m26298：回到旧的规则顺序，别让她随手抽到拍照、自己解决这类）。

    开着时：先硬过滤，再按目的把候选分层、带 seed 抽样；抽不出合适的退回原来的顺序。
    """

    config_version: int = 3
    """配置版本。老配置第一次读进来时按新默认补一次，之后自己在页面上关掉就不会再被顶开。"""

    top_k: int = 4
    """同一层里最多留几个候选给她抽（含"继续/等待"这类保底方向）。"""

    near_radius: float = 2.0
    """多远的算"就近"：路径成本不超过这个值的房间会被展开成具体候选。"""

    expand_limit: int = 3
    """就近层级最多展开几个房间。"""

    command_quiet_minutes: float = 30.0
    """指挥型动作的同类冷却（分钟）：刚指挥过外部插件（拍照/拍腿/自拍这类），
    这么久之内不再抽任何指挥型动作。

    为什么按"一类"而不是按 id 冷却（用户 m25880：一会儿拍腿、一会儿拍自己，连着三次）：
    拍照、拍腿、自拍是三个不同的动作 id，按 id 算的新鲜度拦不住它们轮流中签，
    一件"指挥别人干活"的事连着来三遍就是很怪。写 0 = 关掉这条冷却。
    """


class BoredomOnlineConfig(Permissive):
    """无聊上网池（T20）：闲得慌的时候，从**动作自己声明**过的候选里抽一件事做。

    池子里有哪些动作**不由这里配**：动作定义上写了 ``idle_activity`` 就进池子
    （见 :attr:`ActionDef.idle_activity`），没写就永不参与。所以这个段只有三个
    "多无聊才动手 / 多久一次"的旋钮，没有成员列表——扩展装了、卸了都不用改这里。
    """

    enabled: bool = True
    """总开关。默认开：无聊到一定程度自己找点事上网做，本来就是她该有的样子。"""

    during_cooldown: bool = True
    """冷却期（``engagement`` 因"主动说话没人回"让她安静）里要不要也做独处活动。

    默认开：冷却拦的是"她一直主动开口"，不该连"一个人找点事做"一起拦。池子里的
    动作本来就不冲人开口（见 :func:`core.engine.is_solo_action`），所以做这些事不
    会变成变相搭话。写 ``false`` = 回滚到旧行为：冷却期什么都不做，等冷却过去再说。
    """

    min_boredom: float = 0.55
    """无聊到多少她会自己上网找点事做（0~1）。0.55 ≈ 闲着没事、但还没到抓耳挠腮。"""

    min_gap_minutes: float = 20.0
    """两次"上网找点事"之间至少隔多少分钟。写 0 = 不限制（每一拍无聊都能抽）。"""

    @field_validator("min_boredom", mode="before")
    @classmethod
    def _clamp_boredom(cls, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.55
        if number != number:  # NaN
            return 0.55
        return max(0.0, min(1.0, number))

    @field_validator("min_gap_minutes", mode="before")
    @classmethod
    def _non_negative_gap(cls, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 20.0
        if number != number:
            return 20.0
        return max(0.0, number)


class ContributionConfig(Permissive):
    """群友的回应怎么算进这件事里（阶段 E3）。"""

    max_rounds: int = 3
    """同一条线索最多为群友的话重新拿几次主意：防止来回追问变成无限循环。"""

    window_seconds: float = 120.0
    """收话的短窗：>0 时，头一句有用的话之后先等这么久，把这段时间里的话一起处理。

    默认 120 秒（两分钟）：群友接话、补充、改口都算进同一轮，不会一句话就让她重拿一次主意。
    写 0 就是收到就立刻处理。
    """

    config_version: int = 3
    """配置版本。老配置第一次读进来时按新默认补一次，之后自己改回 0 也不会再被顶开。"""

    keep_rounds: int = 12
    """一条线索最多留几条贡献记录（给提示词和追溯用）。"""


class ConsolidationConfig(Permissive):
    """睡眠整理怎么跑（阶段F）：输入游标、作业生命周期、提案校验。"""

    mode: str = "incremental_jobs"
    """``incremental_jobs``（默认，阶段F）：按登记序号做增量作业，一次作业一个事务；
    ``legacy``：旧的整窗整理（阶段 A–E 的行为，一次睡觉把最近这段聊天全喂给模型）。"""

    window: int = 200
    """一次作业最多吃多少条新素材（素材再多就分批，下一批接着做）。"""

    jobs_per_sleep: int = 4
    """一段睡眠最多做几个作业：积压很多时也不会一口气跑一整夜。"""

    max_attempts: int = 3
    """同一个作业最多试几次（失败退避重试；超过就不再自动重试）。"""

    keep_jobs: int = 40
    """每个会话最多留几条作业记录（给网页看进度，太久的不留）。"""


class DreamConfig(Permissive):
    """梦（阶段F4）：单独一类素材，可以关、默认低优先级，不算事实、不结关系。"""

    enabled: bool = True
    """总开关。默认开：开一次小睡时按 ``chance`` 决定做不做梦，梦只是低权重素材，不进事实与关系。"""

    config_version: int = 3
    """配置版本。老配置第一次读进来时按新默认补一次，之后自己在页面上关掉就不会再被顶开。"""

    chance: float = 0.3
    """开着的时候，一次睡眠整理之后做不做梦。"""

    keep: int = 30
    """最多留几条梦（网页可看、可删；梦只存在记忆里，永远不会变成事实）。"""


class VariationConfig(Permissive):
    """表达变奏（阶段 G）：偶尔调笑 / 反话 / 故意严肃，只改**说法**。"""

    enabled: bool = True
    """总开关。默认开：只是换个说法，判定不过（正经事、风险高、关系还远）她会自动退回直答。"""

    rate: float = 0.15
    """合格轮次里真的用变奏的比例（0 = 从来不，1 = 每轮都试）。"""

    min_gap_turns: int = 3
    """两次变奏之间至少隔几个回复轮（按会话的 ``chat_seq`` 算）。"""

    magnitude: str = "strong"
    """允许的力度上限：``none`` / ``light`` / ``medium`` / ``strong``（默认大）。

    只是**上限**：真正用多大还会被策略本身、情境风险与关系远近继续收紧
    （见 ``core/response_variation.py::adjust_magnitude``）。
    """

    config_version: int = 2
    """这一节的格式版本：老存档里没有这个字段，会被**迁移一次**（见 ``parse_world``）。

    迁移过的配置再保存时会写上 2；此后 ``enabled`` / ``magnitude`` 就是用户自己的选择，
    这里不会再动它（否则用户关掉之后每次加载都会被顶回开着）。
    """


class TopicSteerConfig(Permissive):
    """话题层（T12 Step 3 / E2）：闲聊里她会不会把话拐到自己身上。

    默认开着：规则层只在闲聊/分享/安慰的回合放行，正经事、求助、急事、有失败动作的
    回合一律不拐，冷却与每日上限都按人落 ``state``。**关掉这一个开关就整条路回退**
    （提示词里不出现 ``topic`` 字段与那几句，行为与改动前逐字节相同）——这是最轻的回滚点。
    """

    enabled: bool = True
    """总开关：关掉时提示词、解析、tick 钩子全部退回"没有这个话题层"。"""

    cooldown_turns: int = 5
    """两次拐走之间至少隔几个回复轮（按 ``state.chat_seq`` 算）。"""

    daily_cap: int = 2
    """每人每天最多拐走几次（24 小时内，按 ``state.topic_steer_moves`` 里的 ``at`` 算）。"""

    follow_ticks: int = 2
    """拐走之后几个 tick 内没接着提，就往下一轮注入一次"还没说下去"。"""

    drop_ticks: int = 3
    """提醒过之后再过一个这么大的窗口还没提 → 标 ``dropped``（明确放弃）。"""


class TopicRetrievalConfig(Permissive):
    """谈资的语义检索（阶段 B）：把谈资算成向量，按语义找候选。

    默认值偏保守：**一次回复里查询的总预算按秒卡死**，超了就当作没配 embedding，
    静默退回原来的候选，绝不把等待转嫁给用户；索引是后台的事，一批一批慢慢跑，
    不占回复路径。

    阶段 B0 只落字段与默认值：真正读它们的是后面的工作包。
    """

    candidate_limit: int = 12
    """语义召回先取几条候选（还要交给现有的挑选逻辑重排）。"""

    context_topic_limit: int = 2
    """最多几条谈资进提示词（其余只是候选，不占上下文）。"""

    query_total_budget_seconds: float = 6.0
    """一轮回复里做语义查询的总预算（秒）：超了就按没配 embedding 降级。

    云端 embedding 一次调用实测 3.5~4.3 秒（api.openlux.ai + text-embedding-3-small），
    0.8~2 秒的旧默认值会让语义召回每次都超时、等于没开；这里给到 6 秒仍然是有界的，
    超时只降级不回错。
    """

    request_timeout_seconds: float = 5.0
    """单次嵌入请求最多等多久（秒）：召回路径用，必须能装下一次云端调用。"""

    index_request_timeout_seconds: float = 20.0
    """后台索引单次嵌入请求最多等多久（秒）：不占回复路径，给足余量。"""

    index_batch_seconds: int = 30
    """后台一批索引最多跑多少秒（不占回复路径）。"""

    index_batch_size: int = 16
    """一批索引几条谈资。"""

    prefetch_enabled: bool = True
    """后台预热：按当前话题提前算好查询向量，回话时直接拿它排序。

    开着的收益是**回复不再等宿主**（云端一次嵌入实测 3.5~4.3 秒，全在后台花掉）；
    代价是语义那一半用的是「新消息之前」的语境——词法那一半仍然看当前这句话，
    而且预热只在**有人说新话**时才重算（没人说话就不动），所以偏得很有限。
    关掉就退回"每次回话同步调一次"的老行为。
    """

    prefetch_interval_seconds: float = 300.0
    """同一个会话两次预热至少隔多少秒（冷却）：默认 5 分钟。"""

    prefetch_ttl_seconds: float = 900.0
    """预热好的查询向量能用多久（秒）：默认 15 分钟，过期就回同步调用。"""

    max_steps: int = 5
    """分步检索最多走几步（B3 用）：B0 只落字段与默认值，现在没有任何行为读它。"""


class TopicConfig(Permissive):
    """谈资（阶段 C）：她的经历做成可查看/可编辑的素材。

    默认开：她经历过的事本来就该记得；说不说仍由她自己的判定决定
    （``proactive_limit = 0`` 就是"问到才说、不主动提"）。
    关着的时候**不谈资、不发谈资、页面也不给用**。
    """

    enabled: bool = True
    """总开关。关着时：不建谈资、不按曝光去重、动作续说走老路。"""

    claim_ttl_seconds: float = 120.0
    """一条"我来说这件事"的租期：进程崩了之后这个 key 最多占这么久。"""

    proactive_limit: int = 1
    """一轮最多主动提几条谈资（硬上限见 ``experience.MAX_PROACTIVE_PER_TURN``）。0 = 一轮都不主动提。"""

    recall_limit: int = 3
    """一轮最多想起几条谈资当素材（0 = 这一轮完全不翻自己的经历）。

    这是**素材**的上限，不是"能说几件"：翻出 3 件，她从中挑最合适的一件顺口提
    （真往外说几句由 ``proactive_limit`` 管）。候选太少会显得她没得聊。
    """

    proactive_min_importance: float = 0.5
    """主动提的门槛：重要度低于它的谈资不往外说（0 = 什么都敢提，1 = 只说最要紧的）。"""

    topic_daily_share_limit: int = 2
    """一天最多主动外发几条谈资（T8-2 口径：默认 2）。0 = 不限。

    只数**主动**用途（introduce / progress / outcome）的 share 行，问到才说的
    answer / reference 不算"外发"。
    """

    normal_ttl_seconds: float = 2592000.0
    """普通事存多久（秒，30 天）：日常闲聊过期就该忘，留着只会让她絮叨旧事。"""

    person_ttl_seconds: float = 1209600.0
    """关于某个人的事存多久（秒，14 天）：人事会变，留短一点更不容易说错。"""

    private_ttl_seconds: float = 7776000.0
    """私密事存多久（秒，90 天）：用户自己的事，她不介意存着就别急着清。

    锁定档（``locked``，用户明确说"别外传"）**不受这里影响**：那条永不过期，
    什么时候删由用户决定，代码不替用户忘。
    """

    share_purpose_weight: float = 1.0
    """分享目的相对软权重，不授予权限或制造回复义务。"""
    experience_willingness: float = 0.5
    """同一次主模型带经历的软倾向（0~1）。"""
    expression_length: int = 120
    """谈资展开字符软建议，不是平台裁剪限制。"""
    retrieval: TopicRetrievalConfig = Field(default_factory=TopicRetrievalConfig)
    """语义检索（阶段 B）：只落配置，行为由后面的工作包接上。"""

    settings_version: int = 2
    overrides: dict[str, dict[str, Any]] = Field(default_factory=lambda: {
        name: {"mode": "auto"} for name in
        ("recall_limit", "proactive_limit", "proactive_min_importance",
         "share_purpose_weight", "experience_willingness", "expression_length")
    })

    @model_validator(mode="before")
    @classmethod
    def _topic_override_migration(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        supplied = data.get("overrides")
        overrides = dict(supplied) if isinstance(supplied, dict) else {}
        for name in ("recall_limit", "proactive_limit", "proactive_min_importance",
         "share_purpose_weight", "experience_willingness", "expression_length"):
            if name not in overrides:
                overrides[name] = ({"mode": "custom", "value": data[name]}
                                   if name in data and data.get("settings_version") != 1
                                   else {"mode": "auto"})
            item = overrides[name]
            if not isinstance(item, dict) or item.get("mode") not in ("auto", "custom"):
                raise ValueError(f"invalid topics override: {name}")
            if item["mode"] == "custom":
                import math
                number = item.get("value")
                if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                    raise ValueError(f"invalid custom topics value: {name}")
                if name in ("recall_limit", "proactive_limit", "expression_length") and int(number) != number:
                    raise ValueError(f"integer topics count required: {name}")
                if number < 0 or (name in ("proactive_min_importance", "experience_willingness") and number > 1):
                    raise ValueError(f"out of range topics value: {name}")
            overrides[name] = dict(item)
        data["overrides"] = {name: overrides[name] for name in
                             ("recall_limit", "proactive_limit", "proactive_min_importance",
         "share_purpose_weight", "experience_willingness", "expression_length")}
        # 1 → 2：这一版多了 retrieval 段。老配置（settings_version=1 或没有这一段）
        # 上面已经按老格式处理过 overrides：用户自己写进字段的值一个不动，模式回落 auto。
        # 写回时统一带 2，此后再打开就是新格式；用户自己的选择不会再被顶掉。
        data["settings_version"] = 2
        return data

    config_version: int = 2
    """这一节的格式版本（含义同 ``VariationConfig.config_version``）。"""


class MemeConfig(Permissive):
    """表情包系统（阶段 M1）：收藏、展示、可用状态与管理页。

    M1 只落**字段与默认值**：真正读它们的是 :func:`core.meme_settings.meme_settings`
    与后面的 M2（回复路径）。这一节里没有任何字段会在 M1 影响聊天行为。

    默认值偏保守：一轮最多自己发 1 张、一轮最多搜 1 次、预热总预算 500 毫秒——
    表情包是"锦上添花"，宁可少发一张，也不能让她的回复变慢或变得像机器人在复读。
    """

    enabled: bool = True
    """总开关。关着时：闸门全静默、不写展示记录；管理页仍可查看与清理。"""

    # 以前这里有一个 ``auto_collect_enabled``（"看到就全收进图库"）。
    # 用户 2026-10-29 拍板删掉：收图只有一条路——``world.meme_collect`` 攒进候选池、
    # 拼图评审、通过了才收藏；没有任何"把所有图直接收进素材库"的开关。
    max_prompt_images: int = 10
    """一轮最多看几张图（1~20）：比 ``world.media.max_per_round`` 更靠前的硬上限。"""

    meme_image_max: int = 5
    """一轮最多挑几张表情包当候选（0~8）。0 = 只看、不发。"""

    prefetch_enabled: bool = True
    """后台预热：按当前话题提前算好查询向量，回话时不用等嵌入服务。"""

    retrieval_mode: Literal["auto", "keyword", "keyword_semantic", "embedding"] = "auto"
    """怎么找表情包：四个档，口径见 ``core/meme_settings.py:RETRIEVAL_*``。

    - ``auto``：有嵌入通道就按中间档走，没有就纯关键词；
    - ``keyword``：只按关键词（M3 原口径，排序一个字不改）；
    - ``keyword_semantic``：关键词打底 + 向量软排（显式点名中间档）；
    - ``embedding``：纯向量预选 + 关键词复核；没配服务商/没向量就**如实报用不了**，
      不偷偷退回关键词（点名的就是纯向量）。
    """

    embedding_provider_id: str = ""
    """语义检索用哪个嵌入服务商（留空 = 跟插件配置里的那个）。"""

    prefetch_candidate_count: int = 3
    """预热时先取几条候选（0~8）。"""

    search_candidate_count: int = 5
    """一轮检索真正取几条候选（1~8）。"""

    max_candidate_images_per_prompt: int = 5
    """最多几张候选图进提示词（0~8）：进得越多越占上下文。"""

    max_candidate_preview_bytes: int = 1000000
    """单张候选图给模型看的字节上限（超过就先压/先跳过）。"""

    max_asset_bytes: int = 10000000
    """单个资产字节上限（约 10 MB）：比这大的图不进收藏。"""

    exposure_keep_days: int = 30
    """展示记录留多少天（1~365）：与图片转述缓存的 ``cache_days`` 同口径。"""

    recent_context_messages: int = 6
    """检索时看最近几条聊天记录（1~20）。"""

    query_max_chars: int = 1200
    """检索查询词最多多少字（200~4000）：超了截断，免得把长文整段丢给嵌入服务。"""

    prefetch_total_budget_ms: int = 500
    """一轮预热的总预算（毫秒，100~2000）：超了就放弃预热，不拖回复。"""

    max_search_rounds_per_reply: int = 1
    """一次回复里最多搜几轮（0~2）。0 = 完全不搜。"""

    max_send_per_reply: int = 1
    """一次回复里最多发几张表情包（0~3）。"""

    send_meme_per_hour: int = 20
    """每小时最多发几张表情包（0~200）：防止她刷图。"""

    max_send_per_day: int = 100
    """每天最多发几张表情包（0~1000）：小时配额挡不住"每小时卡着上限发一整天"（M3 加）。"""

    repeat_cooldown: int = 0
    """同一张图两次发送之间至少隔几条消息（0 = 不限制）。"""

    recent_use_window: int = 20
    """"最近用过"的判定窗口（条消息，0~100）：窗口内的图优先换一张。"""

    max_asset_frames: int = 200
    """动图最多留几帧：超过就只留前若干帧（M1 只记这个上限，不做抽帧）。"""

    storage_quota_bytes: int = 500000000
    """收藏占用的总配额（约 500 MB）：超了不再自动收藏，管理页会提示清理。"""


class MemeSendScaleConfig(Permissive):
    """表情包**发出去之前**缩一次图（长边 320、动图保真）。

    为什么单独一节、而不是塞进 ``MemeConfig``：``world.meme`` 的键是收紧的
    （``tests/test_meme_config.py`` 逐字钉死 23 个键），加键会让那份规格守护红；
    而且这一节管的是"发送端交给通道的那张图"，与收藏/检索/配额是两件事。

    只读方是 :func:`core.meme_scale.scale_settings`；调用链只有
    ``Engine.meme_send_source``（管理页缩略图、LLM 候选预览都不走这里）。
    """

    enabled: bool = True
    """总开关：关着时发送路径与改动前逐字节一致（发原图）。"""

    max_side: int = 320
    """长边像素（64~1024；< 64 视为关闭）。320 与管理页卡片缩略图同尺度。"""

    animated: str = "keep"
    """动图策略：``keep`` 保真（逐帧缩放）/ ``first`` 只发首帧 / ``skip`` 不处理。"""

    gif: bool = True
    """发出去那一份转成 GIF（默认开）：QQ 里 GIF 的显示尺寸比同尺寸 PNG 更舒服。
    本身就是动图的不转（会掉帧）。"""


class MemeCollectConfig(Permissive):
    """攒一批"她看到过的图"、拼图、一次评审收进收藏（用户 2026-10-24）。

    为什么又是一个新的顶层段、而不是塞进 ``world.meme``：``world.meme`` 的 23 个键
    被 ``tests/test_meme_config.py`` 逐字钉死，往里加键会让那份规格守护红；而且这一节
    管的是"收图"，与发送端缩图（``meme_send_scale``）是两件事。

    读取方只有 :func:`core.meme_collect.collect_settings`；``enabled=False`` 时整条
    收图链路（入池、拼图、评审、``collect_meme`` 动作行）全部消失，已收藏的图不受影响。
    """

    enabled: bool = True
    """总开关：关着时与"收图这套东西不存在"一致（``send_meme`` / ``find_meme`` 照常）。"""

    batch_probability: float = 0.35
    """每张她看到过的图进入候选池的概率（0 = 只有被明确要求时才收）。"""

    min_batch: int = 6
    """攒够几张才评审（少于这个数就一直攒着）。"""

    max_batch: int = 18
    """一次评审最多看几张（两张 3×3 拼图 = 18）。"""

    review_interval_hours: float = 12.0
    """两次评审之间至少隔多久。"""

    max_reviews_per_day: int = 3
    """一天最多评审几次（同一张图不会被重弹）。"""

    sheet_size: int = 9
    """每张拼图几格（3×3；一次评审最多两张）。"""

    candidate_ttl_days: float = 7.0
    """候选在池子里最多躺几天（过期清掉，不再出现）。"""


class AgentConfig(Permissive):
    """agent（自主能力）的配置：**从插件配置搬来**，跟着世界配置一起存、在编辑器里改。

    搬家的口径（用户 m17187）：这几个开关和模型槽位是"这个世界里她能不能自己动
    起来"的设定，跟世界其它参数一样该在世界编辑器里看得见、改得了、存得下；
    AstrBot 的插件配置页只留真正的基础设施项（如 ``embedding_provider_id``）。
    字段与旧插件配置一一对应，只是去掉 ``agent_`` 前缀：``agent_provider_id``
    → ``provider_id``。

    **字段存在 ≠ 能力开启**：这一版只把用户配了什么原样存回来，真正的工具注册与
    任务创建在 B3——本段不实现执行器，也不拿这些值做任何决策。
    """

    provider_id: str = ""
    """agent 用的模型（老字段，只为兼容存档保留）。

    用户裁定（m20632 第 1 条）：agent 用哪个模型属于**插件配置**的
    ``agent_provider_id``，那才是正主；这里只在插件配置留空时被静默回退读到。
    编辑器已经不再写它。
    """

    enabled: bool = False
    """开启Agent功能：关着不注册任何 agent 工具、不创建任务，也不降级成直接执行。"""

    history_lookup_enabled: bool = False
    """子开关：允许她**查记录**（只在「开启Agent功能」打开时有效）。

    管两个插件自带的只读工具：`vw_history_lookup`（群聊留档）与
    `vw_experience_lookup`（每日经历账）。两件事都是"翻过去的记录"，分成两个开关
    只会让人多关一个；要单独关掉某一个，改 `core/agent_tools.INTERNAL_TOOLS` 即可。
    """

    artifact_generation_enabled: bool = False
    """子开关：允许生成产物文件（文档、图之类）。"""

    file_send_enabled: bool = False
    """子开关：允许把生成好的文件发到群里。"""

    code_execution_enabled: bool = False
    """子开关：允许在受限环境里执行代码（有风险，只在隔离环境开）。"""

    research_enabled: bool = True
    """办事卡住时允许她先上网查一次资料再办（用户 m30450 第 7 条）。

    某个工具步骤失败后，执行器只会多给一次机会：让她用查资料的工具
    （网页搜索 / 知识库）找做法，而不是照原样重试同一个错参数。关掉就是
    "失败了直接停"。
    """

    plugin_forge_enabled: bool = False
    """允许她**自己写插件**（用户 m31581，L1 草稿层；默认关）。

    开着才有 ``vw_plugin_draft``：她把一份 AstrBot 插件写进
    ``<插件数据>/drafts/<slug>/``，页面上能看全文、一键装、一键丢。写草稿不碰宿主，
    最坏结果只是几个废文件，所以这一层只要求这个开关。

    默认关：这是"给她造新能力"的开关，第一次用的人应该自己决定。
    """

    plugin_forge_auto_install: bool = False
    """允许她**自己把草稿装进 AstrBot**（用户 m31581，L2 自装层；默认关）。

    与 ``plugin_forge_enabled`` 同时开着，``vw_plugin_install`` 才出现在工具清单里：
    拷进 ``data/plugins/``、让宿主加载、再把 ``plugin.json`` 里声明的动作写进世界配置。
    关着时她只能写草稿，由人在页面上按下"装上"——这就是"要在主人的许可下"。

    哪怕开着，装之前草稿也要先过 :mod:`core.plugin_forge` 的校验（必须有
    ``main.py`` / ``metadata.yaml``、语法得能过、工具名要对得上）。
    """

    owner_approval_enabled: bool = True
    """麻烦一点的任务要**主人点头**才动手（用户 m31581）。

    只在"提要求的人不是主人"时生效：模型的规划里可以标 ``owner_check``，
    或者目标里出现会动到系统本身的字眼（装插件、改配置、删东西、跑代码）时，
    任务会先停在"等主人决定"，由主人回一句"做 / 别做"。

    关掉 = 谁提的要求都直接做（回到之前的行为）。
    """

    owner_approval_mode: Literal["", "off", "risky", "all"] = ""
    """问到什么程度（三档，用户 m01035 决定 D4）：``off`` 不问、``risky`` 只问危险的、
    ``all`` 全问。

    留空 = 看旧的 ``owner_approval_enabled``（True → 「只问危险」、False → 「不问」），
    老存档不用迁移也能读出原来的意思。低权限用户提的活"全问"时，哪怕只是查个资料
    也要先等一句——适合把 bot 借给别人玩的场景。
    """

    owner_user_id: str = ""
    """主人是谁（QQ 号或会话里认识的 user_id）。**留空 = 谁都不是主人**。

    只认这一个字段（2026-10-24 用户拍板）：关系表里的「主人 / 守护者」是世界设定，
    不等于账号权限，所以留空时不会再拿它去人设里反查。``owner_user_id()`` 为空时
    ``is_owner()`` 对任何人都是 False，"主人裁决"整段跳过，启动时有一条 WARN。
    """

    ask_user_enabled: bool = True
    """P2 追加：缺信息时允许她**真的去问一句**（默认开，用户 2026-10-17 拍板）。

    为什么默认开：不让她问，任务就只能搁着或者瞎猜——那是更坏的失败。追问**不占**
    说话的预算与频率限制（那是闲聊的额度），只受任务自己的 TTL 与"每个任务最多问几次"
    约束；问句发给哪个会话由模型自己选，与 `say` 同一套落点机制。
    """

    ask_user_max_per_task: int = 3
    """同一个任务最多问她/他几次（防止一个任务把整个群刷屏）。0 = 不许问。"""

    inspect_capability_enabled: bool = False
    """A6 追加：允许她**按需查看某个动作的详细说明**（只读，默认关）。

    为什么单独一个开关、而且默认关：它是"多给你一次问答换取更准的选择"，不是
    "多一种做事的能力"——用户拍板默认关，关了就跟没有这个能力完全一样（提示词里
    一个字都不出现、执行入口回一句中文原因）。它**不依赖** ``enabled``：查说明
    既不注册工具也不开任务，与 Agent 总开关是两件事。
    """

    # ---- B3：执行器自己的预算（以前这几项只有 topics.retrieval 一份，Agent 页
    # 显示的和执行器实际用的不是一回事；接线后「先读 agent 自己的、缺了再回退
    # retrieval」，所以这里的默认值与 retrieval 那几个保持一致，老存档不吃惊）。
    max_steps: int = DEFAULT_AGENT_MAX_STEPS
    """一次任务最多**做成**几步（每一步 = 一次工具调用）。

    默认 32（用户 2026-10-22 定：128 步的 token 成本没人买单，5 步又走不完一件
    像样的活）。计数口径是"做成的步"——失败 / 被拒 / 超时的步不占额度，另有一道
    尝试次数闸兜住死循环（见 ``agent_tasks.attempt_limit_for``）。
    """

    query_total_seconds: float = 600.0
    """一次任务的**总**耗时上限（秒）：到点就收尾，不半路扔下她。

    默认 10 分钟（用户 m25774：180 秒太短，写一份东西、跑几步工具根本不够）。
    单次调用仍由 ``request_timeout_seconds`` 单独夹住，所以整件事的预算放宽
    不会让一次卡死的请求拖住全程。
    """

    request_timeout_seconds: float = 60.0
    """单次调用（工具 / 规划模型）的超时（秒）。"""

    tasks_per_hour: int = 3
    """同一个会话每小时最多开几个任务（0 = 不许开）；默认保守。"""

    code_runs_per_task: int = 0
    """一次任务里最多执行几次代码（0 = 不许执行）；风险最高的一档，默认 1。"""

    artifact_max_bytes: int = 5_000_000
    """单个产物文件的字节上限（约 5 MB）：超了**拒绝**，不截断。"""

    artifact_keep_days: int = 7
    """产物留多少天（0 = 不过期）；过期且已发送的产物在日结时清掉。"""

    commitment_enabled: bool = True
    """她说「等会儿」时，是否把这句话记成一条真待办（关掉 = 老样子，只留在提示词里）。"""

    commitment_nudge_minutes: int = 30
    """隔多久回来提醒她一次（分钟）：第一次到点就提，之后每这么多分钟再提一次。"""

    commitment_max_nudges: int = 2
    """最多提醒几次：提满还没做就丢掉这条待办、不再纠缠（避免复读机）。"""


class HistoryConfig(Permissive):
    """「翻聊天记录」这个**内置动作**的开关与夹取（用户 2026-10-26）。

    为什么单独一段：原来「翻聊天记录」只在 agent 档里注册（``core/agent_tools.py``
    的 ``vw_history_lookup``，开关 ``agent.history_lookup_enabled``，默认关）——
    她要翻一次记录得先开「开启Agent功能」，还得真去跑一个任务。用户的原话是
    「我 agent 功能都开着的，但她只要不调 agent 就查不到吧？能不能做成内置动作」。

    现在它是世界动作表里的 ``lookup_history``：任何一轮都能自己翻一次，与 agent
    那条链**互不依赖**。查库与成文走的是同一个实现（``Engine._history_lookup_text``），
    所以两边的口径完全一样：必须有由头（关键词或时间窗）、窗口 ≤30 天、
    条数 ≤50、总字数 ≤2400。
    """

    lookup_action_enabled: bool = True
    """关掉 = 这个动作**不注册**（提示词里不列、解析白名单不收），不是列出来再报错。"""

    default_days: float = 7.0
    """她没给时间范围时默认往回翻几天（仍受 30 天硬上限夹取，夹了会告诉她）。"""

    max_rows: int = 50
    """一次最多翻几条（硬上限仍是 ``history_lookup.HARD_LIMIT`` = 50，改大也不会超过）。"""


class TempSessionConfig(Permissive):
    """临时会话自动接管：她主动私聊出去的人，先由她接管一阵子。

    只有**她成功发出一条到某个当前未启用会话**的消息时才会登记（见
    ``Engine._deliver`` 里的绑定点）；白名单里已有的会话一概不动，仍走 AstrBot
    默认路由。到期 / 聊满的收尾规则见 ``Engine`` 的 ``temp_*`` 方法。
    """

    enabled: bool = True
    """总开关：关掉 = 她私聊出去的人回到"AstrBot 默认路由"，不登记、不接管。"""

    ttl_minutes: int = 30
    """临时会话活多久（分钟）。到期不主动发消息，只把状态置 ``expired``。"""

    max_rounds: int = 20
    """最多聊几轮（**会话轮**：对方一条消息 + 她的回复算 1 轮，多段也算 1 轮）。"""

    familiar_keep: bool = True
    """熟人及以上不参与到期（长期保留、上下文持续注入）。"""

    familiar_level: int = 4
    """从哪一档关系算「熟人及以上」（``profile.levels`` 的下标，4 = 朋友）。

    默认与 ``Engine.VARIATION_FAMILIAR_LEVEL`` 一致；填 3 就是"熟人档就算"。
    """

    group_temp: bool = True
    """群临时会话（``send_private_msg`` 带 ``group_id``）与好友私聊同等处理。"""


class StorageConfig(Permissive):
    """存储自检与瘦身（2026-10 磁盘满事故之后加的）。

    事故现场：生产机磁盘写满 → 每轮回复的世界状态/决策写不进去 → 异常被吞 →
    她整轮不说话，而 ``plugin_data`` 已经涨到 179 MB、日志里却只有几百 K。
    所以这里放三组旋钮：**什么时候告警**（阈值）、**留多久**（保留天数）、
    **清单给谁看**（``/vw status`` 列几个大文件）。全部有默认值，``world.json``
    里不写也能跑；改这些不需要重启插件之外的东西（下次 tick / 下次 status 生效）。
    """

    enabled: bool = True
    """总开关：关掉 = 不做自检也不做瘦身（写失败留痕仍然生效，那是排障底线）。"""

    notify_write_failure: bool = True
    """写库失败时，除了日志，还在她这一轮的回复之后补一条系统提示。

    关掉 = 只记日志、不发消息（``/vw status`` 的存储段与 ``last_write_error`` 照常）。
    为什么默认开：2026-10 磁盘满那次，用户只看到她"突然不理人"，几小时才查到是写库
    失败——这条提示就是把"该去修环境了"直接送到主人眼前。
    """

    min_free_mb: int = 500
    """分区可用空间低于这么多 MB 就告警（sqlite 写 WAL/临时文件也要地方）。"""

    max_data_mb: int = 300
    """数据目录超过这么多 MB 就告警（179 MB 那次就是这样被忽略过去的）。"""

    top_files: int = 5
    """``/vw status`` 里列几个最大的文件（含 ``.bak`` / ``.broken`` / 导出 zip）。"""

    decision_batch_keep_days: int = 14
    """``decision_batches`` 留多少天：一天清一次，只删更老的（0 = 不清）。"""

    state_history_keep_days: int = 30
    """``state_history`` 按时间留多少天（另有按条数裁剪兜底；0 = 不清）。"""

    event_keep_days: int = 30
    """``event_log`` 按时间留多少天（另有按会话条数裁剪兜底；0 = 不清）。"""

    keep_snapshots: int = 1
    """每类旧快照保留几份（``<=0`` = 关掉自动清理）。

    「快照」指插件自己留下的一次性残留：``state.db.bak-*``、``state.db.broken-*``、
    ``.vwtest-state-broken-*.db``、``.vwtest_repair/*.db``、``backups/snapshot-*/``、
    ``world.json.bak-*``。**五类各自独立**算「最近 N 份」，不是合在一起算。
    为什么默认只留 1 份：2026-10 那台机的 340 MB 里绝大多数就是这些残留（单份
    30~47 MB），用户不会自己去删，而它们只在「修复 / 升级 / 测头像」那一下有用。
    真按每类 3 份实测首次只腾 1.8 MB（涨得比清得快）；留 1 份仍保留「最近一次
    可以回退的东西」，再多就是纯垃圾。
    """

    auto_clean_snapshots: bool = True
    """启动 / 每日维护时是否真的按 :attr:`keep_snapshots` 清旧快照。

    只删白名单里那五类；主库（``state.db`` 和它的 ``-wal``/``-shm``）、``world.json``、
    ``sessions.json``、``presets/`` / ``media/`` / ``docs/`` / ``tests/`` 等一律不碰，
    任何符号链接也不碰（规则见 ``core/storage_health.py`` 的 ``plan_snapshot_cleanup``）。
    关掉之后只会「算出来给你看」，不删任何东西。
    """

    vacuum_enabled: bool = True
    """每日维护时是否自动回收空闲页（``PRAGMA incremental_vacuum``，一次只搬有上限的一批）。

    删行只把页挂进 freelist，**文件不会自己缩**；这个开关就是"把空出来的页真的
    还给文件系统"。关掉 = 库只涨不缩，其它自检与裁剪照常。
    """

    vacuum_pages_per_run: int = 20000
    """每次维护最多回收多少页（约 78 MB @ 4 KB 页；``0`` = 不回收）。

    一次搬太多会长时间占写锁；搬不完的下一次维护接着搬（另有 1.5 秒硬 deadline）。
    """


class KnowledgeConfig(Permissive):
    """知识卡（用户 2026-10-24）：「查了某个梗什么意思，查完就过去了，聊天记录刷掉
    她就不记得，也不会活用。」

    这一节只管"记多少、记多久、什么时候拿出来讲"，全部有默认值，``world.json`` 里
    不写也能跑。卡的内容**不经过模型**：摘要与原文都是从那次搜索的真实结果里截出来的
    （见 ``core/knowledge_card.py``），所以这里没有"用哪个模型整理"之类的键——
    v1 一次额外的模型调用都不发。

    键与读取方一一对应：``enabled`` / ``max_cards_in_prompt`` / ``min_overlap`` 在
    召回注入那一段（``core/engine.py`` 回复路径），``max_age_days`` 同时决定写入时的
    ``expires_at`` 与每日维护里的过期清理，``max_summary_chars`` 决定摘要长度。
    """

    enabled: bool = True
    """总开关：关掉 = 不写新卡、也不召回（已经存在的卡留在库里，不删）。"""

    max_cards_in_prompt: int = 3
    """一次最多把几张卡写进提示词（多了会挤掉别的上下文，她那轮也可能硬提）。"""

    max_age_days: int = 90
    """一张卡最多留多少天（``0`` = 永不过期；过期只改状态，不删行）。"""

    min_overlap: int = 2
    """至少同时命中几个关键词才算"问到了这件事"。

    一个也算太松：「什么」「意思」这种词几乎每句都有，命中一个就拿出来讲很容易
    讲错题。默认要两个（比如「地狱笑话」同时命中「地狱」「笑话」）。
    """

    max_summary_chars: int = 200
    """卡片摘要最多多少字（直接从结果正文截，不经过模型改写）。"""


class ReplySchemaConfig(Permissive):
    """回复格式（用户 2026-10-26）：回话轮的 JSON 要不要带上那五个"额外"字段。

    用户判定：回复 JSON 被新字段撑复杂了（``format_layer`` 从 6494 字涨到 8065 字），
    性格被压平——字段越多，她越是在"填表"，不是在说话。这一节只控制**提示词里写不写**
    那些字段，功能代码（解析、落库、变奏、拐话头）一行都不删：旧输出带着它们照样认。

    默认 ``legacy=True``：只要求 ``request{kind,expect}``，其余五个字段（顶层 ``share``、
    ``tease``、``topic``、顶层 ``intent``、``reasoning.check``）既不出现字段行、也不出现
    说明段。想换回 2026-10-26 那版完整 schema，把 ``legacy`` 写 false 即可（编辑器里
    「全局设置 → 说话与决策 → 回复格式（精简/完整）」那一个开关就是它）。
    """

    legacy: bool = True
    """True＝精简：提示词里只发 ``request``，五个新字段逐字不出现（默认）。
    False＝完整：恢复 2026-10-26 的完整 schema（share / tease / topic / 顶层 intent /
    reasoning.check 一起回到提示词里）。"""


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

    agent: AgentConfig = Field(default_factory=AgentConfig)
    """她能不能自己动起来（模型槽位 + 五个开关）。

    以前这六个键在 AstrBot 的插件配置里（``agent_*``），现在跟着世界配置走，编辑器
    里能选能存。老用户在插件配置里填过的值会在插件启动时**一次性**搬进来（见
    ``main.py`` 的种子迁移），此后只认世界配置。
    """

    history: HistoryConfig = Field(default_factory=HistoryConfig)
    """内置动作「翻聊天记录」（与 agent 那条只读工具链互不依赖）。"""

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

    reply_mode: Literal["takeover"] = "takeover"
    """被 @（或消息走到大模型）时的处理方式：**只保留接管**。

    插件调大模型、按 JSON 动作执行并发送，然后阻止主人格重复回复——
    「只注入世界认知、交主人格回复」那条路已经在 v2.2 移除：主人格是另一个人设，
    顶上来会让用户看到「她」突然换了个说法。老配置里的 ``inject`` 读到时会自动迁到
    ``takeover``（见 :meth:`_migrate_reply_mode`），不报错、也不会再进那条分支。
    """

    @field_validator("reply_mode", mode="before")
    @classmethod
    def _migrate_reply_mode(cls, value: Any) -> str:
        """老配置 / 手工改坏的配置一律迁到 ``takeover``，绝不让插件退化成注入模式。"""

        text = str(value or "").strip().lower()
        if text != "takeover":
            return "takeover"
        return text

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
    temp_session: TempSessionConfig = Field(default_factory=TempSessionConfig)
    """临时会话自动接管：她私聊出去的人先接管一阵子（到期 / 聊满就收尾）。"""
    variation: VariationConfig = Field(default_factory=VariationConfig)
    """表达变奏（阶段 G）：只改表达策略，不动 mood / 好感 / 关系数值。"""
    topic_steer: TopicSteerConfig = Field(default_factory=TopicSteerConfig)
    """话题层（T12 Step 3）：闲聊里她可以把话拐到自己身上，但必须给下文或明确放下。"""
    reply_schema: ReplySchemaConfig = Field(default_factory=ReplySchemaConfig)
    """回复格式（用户 2026-10-26）：精简（只发 request，默认）还是完整（2026-10-26 那版）。"""
    action_selection: ActionSelectionConfig = Field(default_factory=ActionSelectionConfig)
    """分层候选（阶段 D）：先硬过滤、再分层展开、最后带 seed 加权抽样。"""
    boredom_online: BoredomOnlineConfig = Field(default_factory=BoredomOnlineConfig)
    """无聊上网池（T20）：闲得慌时从动作自己声明的候选里抽一件事做。"""
    contributions: ContributionConfig = Field(default_factory=ContributionConfig)
    """群友的回应（阶段 E3）：分类留痕、有界追问、真正改变做法才算数。"""
    consolidation: ConsolidationConfig = Field(default_factory=ConsolidationConfig)
    """睡眠整理（阶段F）：增量作业、输入游标、提案校验。"""
    dream: DreamConfig = Field(default_factory=DreamConfig)
    """梦（阶段F4）：单独一类素材，默认开着，永远不算事实。"""
    topics: TopicConfig = Field(default_factory=TopicConfig)
    meme: MemeConfig = Field(default_factory=MemeConfig)
    """表情包系统（阶段 M1）：收藏/展示/可用状态与管理页；M1 只落配置与数据。"""
    meme_send_scale: MemeSendScaleConfig = Field(default_factory=MemeSendScaleConfig)
    """发送前缩图：长边 320、动图保真；关着时发送路径与改动前一致。"""
    meme_collect: MemeCollectConfig = Field(default_factory=MemeCollectConfig)
    """攒一波图再评审收进收藏（用户 2026-10-24）：池子、拼图、批量收藏与判据。"""
    reply_style: ReplyStyle = Field(default_factory=ReplyStyle)
    follow_up: FollowUpConfig = Field(default_factory=FollowUpConfig)
    """追问（用户 m27236）：她问了没人答，隔 10 分钟再问一句，默认只追一次。"""
    vision: VisionCaption = Field(default_factory=VisionCaption)
    media: MediaConfig = Field(default_factory=MediaConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    """联网搜索（用户 m10393）：全局一份配置，动作里不再露出这些字段。"""

    """媒体资产库（阶段B）：入站/工具/发出的图落一份本地副本，按内容寻址。"""
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    night: NightConfig = Field(default_factory=NightConfig)
    content_safety: ContentSafety = Field(default_factory=ContentSafety)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    """存储自检与瘦身：阈值、保留天数、清单条数（磁盘满事故的护栏）。"""
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    """知识卡（用户 2026-10-24）：她查过的梗 / 词 / 事记下来，之后能拿出来用。"""
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

    def home_zone_id(self) -> str:
        """"她的家"在哪个区域：标了 ``is_home`` 就用它，否则按卧室所在的区域算。

        不再拿"第一个区域"当家——那只是配置顺序，换个顺序家就跑了。
        """

        for zone in self.zones:
            if bool(getattr(zone, "is_home", False)):
                return zone.id
        node = self.node_map().get(self.default_node_id())
        if node is not None and node.zone_id:
            return node.zone_id
        return self.default_zone_id()

    def is_home_zone(self, node_id: str) -> bool:
        """这个地点算不算"在家里"（用来判断她有没有出门）。"""

        return self.zone_of(node_id) == self.home_zone_id()

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


MORNING_SCHEDULE_ID = "morning_greet"
"""默认「早安」那条日程的 id（迁移时给它补一句话，见 ``parse_schedules``）。"""

DEFAULT_SCHEDULE_NOTES = {
    MORNING_SCHEDULE_ID: (
        "早上起来打个招呼：走到客厅伸个懒腰，说一句早安"
        "（说自己昨晚睡得怎么样、今天想做什么都行）。"
    ),
    "dinner_cook": "做晚饭：走到厨房做一顿饭，做完可以顺口说一句做好了、或者叫人来吃。",
    "night_sleep": "睡前收尾：走回卧室躺下睡觉，别再摸手机。",
}
"""默认日程的「这条日程是干什么的」。只在用户没写描述时补上（用户 m29086）。"""

DEFAULT_SCHEDULE_NAMES = {
    MORNING_SCHEDULE_ID: "早安",
    "dinner_cook": "做晚饭",
    "night_sleep": "睡觉",
}


REPEAT_KINDS = ("daily", "weekly", "monthly", "every", "workdays", "once", "cron")
"""一条日程的频率可以怎么排。

- ``daily``：每天，``at`` 里可以写多个时间点；
- ``weekly``：只在 ``weekdays`` 勾的那几天；
- ``monthly``：只在 ``month_days`` 那几号（小月自动跳过不存在的日子）；
- ``every``：每 ``every_minutes`` 分钟一次，``window`` 限定一天里的哪几小时；
- ``workdays``：周一到周五；
- ``once``：只做一次，用 ``date`` + ``at[0]``（老字段同一套口径）；
- ``cron``：``expr`` 里写标准 5 段 cron（高级模式）。
"""


class ScheduleWindow(Permissive):
    start: str = "00:00"
    end: str = "23:59"

    @field_validator("start", "end")
    @classmethod
    def _valid_clock(cls, value: str) -> str:
        return _clock_text(value)


class ScheduleRepeat(Permissive):
    """日程的「什么时候触发」，结构化存储（cron 表达式只是其中一档）。

    为什么不直接存 cron 字符串：编辑器里要能回填成"每周一三五 08:00"这种控件，
    cron 字符串回填不回去；而且普通人改日程时不该看到 ``0 8 * * 1,3,5``。
    提交时若填的是 cron，也转换成这里的结构化字段 + 保留 ``expr``。
    """

    kind: str = "daily"
    at: list[str] = Field(default_factory=lambda: ["08:00"])
    """一天里的时间点（``HH:MM``，可以多个）。``daily`` / ``weekly`` / ``monthly`` /
    ``workdays`` / ``once`` 都用它。"""

    weekdays: list[str] = Field(default_factory=list)
    """``weekly`` 用：``mon``…``sun``，空 = 每天都算。"""

    month_days: list[int] = Field(default_factory=list)
    """``monthly`` 用：1~31，空 = 每月 1 号。"""

    every_minutes: int = 60
    """``every`` 用：间隔分钟数（最小 5，见 ``EVERY_MINUTEs`` 的校验）。"""

    window: ScheduleWindow = Field(default_factory=ScheduleWindow)
    """``every`` 用：只在一天里的这一段时间内触发（跨午夜请写成两段 cron）。"""

    expr: str = ""
    """``cron`` 用：标准 5 段表达式。"""

    note: str = ""
    """可选的人类可读备注（保存时由引擎回填，便于排错时一眼看懂）。"""

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, value: str) -> str:
        text = str(value or "").strip().lower()
        return text if text in REPEAT_KINDS else "daily"

    @field_validator("at")
    @classmethod
    def _valid_at(cls, value: Any) -> list[str]:
        items = value if isinstance(value, (list, tuple)) else [value]
        cleaned: list[str] = []
        for item in items:
            text = str(item or "").strip()
            if not text:
                continue
            cleaned.append(_clock_text(text))
        return cleaned or ["08:00"]

    @field_validator("weekdays")
    @classmethod
    def _valid_weekdays(cls, value: Any) -> list[str]:
        items = value if isinstance(value, (list, tuple)) else [value]
        allowed = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        cleaned: list[str] = []
        for item in items:
            text = str(item or "").strip().lower()
            if text in allowed and text not in cleaned:
                cleaned.append(text)
        return cleaned

    @field_validator("month_days")
    @classmethod
    def _valid_month_days(cls, value: Any) -> list[int]:
        items = value if isinstance(value, (list, tuple)) else [value]
        cleaned: list[int] = []
        for item in items:
            try:
                day = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= day <= 31 and day not in cleaned:
                cleaned.append(day)
        return sorted(cleaned)

    @field_validator("every_minutes")
    @classmethod
    def _valid_every(cls, value: Any) -> int:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return 60
        return max(5, min(1440, minutes))


def _clock_text(value: str) -> str:
    """把 ``H:MM`` / ``HH:M`` 统一成 ``HH:MM``；写不成时间就退回 ``08:00``。"""

    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return "08:00"
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return "08:00"
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "08:00"
    return f"{hour:02d}:{minute:02d}"


_WEEKDAY_INDEX = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}


def _split_clocks(times: list[str]) -> list[tuple[int, int]]:
    """``["08:00", "22:30"]`` → ``[(8, 0), (22, 30)]``（去重、按时间排序）。"""

    seen: set[tuple[int, int]] = set()
    for item in times or []:
        text = str(item or "").strip()
        parts = text.split(":")
        if len(parts) != 2:
            continue
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            seen.add((hour, minute))
    return sorted(seen) or [(8, 0)]


def _hour_of(text: str) -> int:
    try:
        return max(0, min(23, int(str(text or "0").split(":")[0])))
    except ValueError:
        return 0


class ScheduleDef(Permissive):
    id: str
    enabled: bool = True
    name: str = ""
    """这条日程叫什么（编辑器列表上显示的名字；空着就显示 id）。"""
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

    repeat: "ScheduleRepeat | None" = None
    """结构化频率（新）。``None`` = 老日程，按 ``time`` + ``days`` 那套口径走。

    编辑器保存时一律写这个字段；老存档不写就继续用老字段，
    所以升级不会改动任何一条现有日程的触发时刻。
    """

    note: str = ""
    """这条日程为什么排的（前因后果）。到点触发时带给主模型，免得忘了当初要干嘛。"""

    # ---------------- 频率 → cron ----------------

    def repeat_kind(self) -> str:
        """当前生效的频率种类（老日程按 ``days`` 推断）。"""

        if self.repeat is not None:
            return str(self.repeat.kind or "daily")
        if self.once:
            return "once"
        days = [str(item) for item in self.days or []]
        if not days or len(days) == 7:
            return "daily"
        if set(days) == {"mon", "tue", "wed", "thu", "fri"}:
            return "workdays"
        return "weekly"

    def cron_exprs(self) -> list[str]:
        """把这条日程摊成若干条 5 段 cron；一条日程可以有多条（多个时间点）。"""

        repeat = self.repeat
        if repeat is not None and str(repeat.kind or "") == "cron":
            text = str(repeat.expr or "").strip()
            return [text] if text else []
        kind = self.repeat_kind()
        if kind == "once":
            return []
        if repeat is None:
            # 老日程：time + days
            times = [self.time]
            weekdays = [str(item) for item in self.days or []]
        else:
            times = list(repeat.at or ["08:00"])
            weekdays = list(repeat.weekdays or [])
        if kind == "every" and repeat is not None:
            return [self._every_expr(repeat)]
        if kind == "monthly" and repeat is not None:
            days = ",".join(str(day) for day in (repeat.month_days or [1]))
            return [f"{minute} {hour} {days} * *" for hour, minute in _split_clocks(times)]
        if kind == "workdays":
            return [f"{minute} {hour} * * 1-5" for hour, minute in _split_clocks(times)]
        if kind == "weekly" and weekdays and len(weekdays) < 7:
            dow = ",".join(str(_WEEKDAY_INDEX[item]) for item in weekdays if item in _WEEKDAY_INDEX)
            return [f"{minute} {hour} * * {dow}" for hour, minute in _split_clocks(times)]
        return [f"{minute} {hour} * * *" for hour, minute in _split_clocks(times)]

    def _every_expr(self, repeat: "ScheduleRepeat") -> str:
        """「每 N 分钟」翻译成 cron：<=60 用分钟步长，>60 用整点步长。"""

        minutes = max(5, int(repeat.every_minutes or 60))
        start = _hour_of(repeat.window.start)
        end = _hour_of(repeat.window.end)
        if end < start:
            end = 23
        # cron 的分钟步长只认「能整除 60」的值：*/7 会被读成 0,7,14…56 再回到 0，
        # 跨小时之后就不是每 7 分钟了。整除不了的按整点步长近似，别给出错的节奏。
        if minutes < 60 and 60 % minutes == 0:
            return f"*/{minutes} {start}-{end} * * *"
        step = max(1, min(23, int(round(minutes / 60.0))))
        return f"0 {start}-{end}/{step} * * *"

    def trigger_times(self) -> list[str]:
        """这条日程一天里会在哪几个时刻触发（``HH:MM``），给提示词与日志用。"""

        exprs = self.cron_exprs()
        times: list[str] = []
        for expr in exprs:
            try:
                from .cron import parse as _parse_cron

                spec = _parse_cron(expr)
            except Exception:
                continue
            if spec.every_minute and len(spec.minutes) > 3:
                times.append(f"每 {self.repeat.every_minutes if self.repeat else 60} 分钟")
                continue
            for hour in sorted(spec.hours):
                for minute in sorted(spec.minutes):
                    times.append(f"{hour:02d}:{minute:02d}")
        return times or [self.time]

    def when_text(self) -> str:
        """一句人话描述（编辑器列表 / 提示词里用）。"""

        kind = self.repeat_kind()
        if kind == "once":
            return f"{self.date or '最近一次'} {self.time}"
        if kind == "every" and self.repeat is not None:
            window = self.repeat.window
            return (
                f"每 {int(self.repeat.every_minutes)} 分钟"
                f"（{window.start}-{window.end}）"
            )
        times = self.trigger_times()
        clocks = "、".join(times) if len(times) <= 4 else f"{times[0]} 等 {len(times)} 个时间点"
        if kind == "workdays":
            return f"工作日 {clocks}"
        if kind == "monthly" and self.repeat is not None:
            days = "、".join(str(day) for day in (self.repeat.month_days or [1]))
            return f"每月 {days} 号 {clocks}"
        if kind == "weekly":
            names = {
                "mon": "一", "tue": "二", "wed": "三", "thu": "四",
                "fri": "五", "sat": "六", "sun": "日",
            }
            picked = self.repeat.weekdays if self.repeat is not None else self.days
            labels = "".join(names.get(str(item), "") for item in (picked or []))
            if labels and len(labels) < 7:
                return f"周{labels} {clocks}"
        return f"每天 {clocks}"

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

    temporary: bool = False
    """这一条是不是「临时会话自动接管」登记进来的（她自己私聊出去的落点）。

    临时会话和白名单会话走同一套路由（`is_enabled` / 会话组 / 留档都照旧），
    差别只在期限与记忆：见 :class:`TempSessionConfig` 与 ``temp_sessions`` 表。
    到期时刻**只**存在 ``temp_sessions.expires_at`` 一处，这里不再复制一份。
    """

    temp_origin: str = ""
    """临时会话的「这是从哪儿认识的」：留档与摘要都按这个 origin 存。"""

    temp_source: str = ""
    """``private`` / ``group_temp``：这条私聊是普通私聊还是群临时会话发出去的。"""


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

    @field_validator("sessions", "groups", mode="before")
    @classmethod
    def _accept_foreign_instances(cls, value: Any) -> Any:
        """同一份模型被两个模块名加载时（`core.models` 与测试里的包名导入），
        互传实例会撞 pydantic 的 `model_type` 校验——哪怕字段完全一样。

        这里把"能 dump 出同样字段"的实例就地转成 dict；真正的类型错误（少字段、
        字段名不对）随后仍会被校验器抓住。
        """

        if not isinstance(value, (list, tuple)):
            return value
        out: list[Any] = []
        for item in value:
            dump = getattr(item, "model_dump", None)
            if isinstance(item, (SessionDef, SessionGroupDef)) or not callable(dump):
                out.append(item)
                continue
            try:
                out.append(dump())
            except Exception:  # noqa: BLE001 - 不是 pydantic 模型就原样交给校验器
                out.append(item)
        return out


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
                "睡一整晚恢复精力（约 8 小时，夜里或凌晨、困得撑不住时用），需要先回到卧室。"
                "白天只是犯困就小睡一会儿，别在白天睡整觉。"
            ),
            "睡一整晚恢复精力（约 8 小时，夜里或精力见底时用），需要先回到卧室。": (
                "睡一整晚恢复精力（约 8 小时，夜里或凌晨、困得撑不住时用），需要先回到卧室。"
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
    """把老的事件名换成新的事件名，顺手丢掉不认识的。

    ``ext_*`` 是扩展自己注册的类型（见 ``ExtensionSpec.debug_events``）：主插件
    这边不认识具体有哪些，但**不能把用户勾的丢掉**——执行时再按注册表核对。
    """

    expanded: list[str] = []
    for name in names:
        key = str(name).strip()
        targets = ECHO_TYPE_ALIASES.get(key) or (
            (key,) if key in ECHO_EVENT_TYPES or key.startswith("ext_") else ()
        )
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
    "state_dynamics.desire_relief": "被亲近一次欲求落多少",
    "state_dynamics.desire_relief_floor": "亲昵最低把欲求卸到这儿",
    "state_dynamics.desire_contact_warm": "已在低位时被亲近反而涨多少",
    "state_dynamics.desire_relief_from_contact": "日常亲昵也让它落下去",
    "state_dynamics.desire_relief_min_intimacy": "多亲密才算真的解渴",
    "state_dynamics.desire_relief_hour_cap": "一小时最多落多少（0 = 不限）",
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
    "state_dynamics.intimacy_valence_cap": "亲密一轮最多涨多少效价",
    "state_dynamics.intimacy_valence_daily_cap": "亲密涨效价的每天上限",
    "state_dynamics.loneliness_company_relief": "被认真陪聊一轮能降多少孤独",
    "state_dynamics.loneliness_company_relief_hour_cap": "一小时最多靠陪聊降多少孤独",
    "state_dynamics.valence_decay_per_min": "效价每分钟回落多少",
    "state_dynamics.mood_override_duration": "情绪上头压过理智多久（秒）",
    "state_dynamics.daily_mood_enabled": "每天掷一次今天的基调",
    "state_dynamics.daily_mood_strength": "今日基调的作用强度",
    "events.micro_per_hour": "每小时约几件小事",
    "events.small_per_hour": "每小时约几件中等事",
    "events.big_per_hour": "每小时约几件大事",
    "events.min_gap_minutes": "两件事至少隔几分钟",
    "events.photo_tendency": "每一幕拍不拍图（当场各掷一次）",
    "events.photo_chance": "旧名字：同上",
    "events.photo_max_per_event": "一件事最多拍几张照",
    "events.genre_cooldown_minutes": "同类事多久内不重复（分钟）",
    "consolidation.mode": "整理怎么跑：增量作业 / 老整窗",
    "consolidation.window": "一次整理最多吃多少条新素材",
    "consolidation.jobs_per_sleep": "一段睡眠最多做几个整理作业",
    "consolidation.max_attempts": "同一个作业最多试几次",
    "action_selection.enabled": "分层候选：从能做的事里按目的挑一件",
    "boredom_online.enabled": "无聊到一定程度会自己上网找点事做",
    "boredom_online.during_cooldown": "冷却期也允许她一个人找点事做",
    "boredom_online.min_boredom": "无聊到多少会自己找点事上网做",
    "boredom_online.min_gap_minutes": "两次上网找事至少隔几分钟",
    "contributions.window_seconds": "收话短窗：先攒一会儿再一起处理（秒）",
    "dream.enabled": "睡觉时做不做梦（不算事实）",
    "dream.chance": "做一次梦的概率",
    "dream.keep": "最多留几条梦",
    "context.chat_compress_threshold": "攒多少条聊天记录压缩一次",
    "context.summary_refresh_minutes": "多久重写一次聊天摘要",
    "context.chat_answered_lines": "提示词里带多少条聊天记录",
    "context.chat_history_max": "留档最多存多少条聊天记录",
    "context.chat_overflow": "留档超出上限时的处理（丢弃 / 压缩）",
    "topics.recall_limit": "一轮最多想起几件谈资",
    "topics.proactive_limit": "一轮最多主动提几件",
    "topics.proactive_min_importance": "多要紧的事才主动提",
    "topics.claim_ttl_seconds": "一件事占住的时长（秒）",
    "topics.normal_ttl_seconds": "普通事记多久（秒）",
    "topics.person_ttl_seconds": "关于某人的事记多久（秒）",
    "topics.private_ttl_seconds": "私密事记多久（秒）",
    "topics.retrieval.candidate_limit": "语义召回先取几条候选",
    "topics.retrieval.context_topic_limit": "最多几条谈资进提示词",
    "topics.retrieval.query_total_budget_seconds": "一轮检索总预算（秒）",
    "topics.retrieval.request_timeout_seconds": "单次嵌入请求超时（秒，召回用）",
    "topics.retrieval.index_request_timeout_seconds": "单次索引请求超时（秒，后台用）",
    "topics.retrieval.index_batch_seconds": "一批索引最多跑多久（秒）",
    "topics.retrieval.index_batch_size": "一批索引几条谈资",
    "topics.retrieval.prefetch_enabled": "后台预热谈资召回（省回复延迟）",
    "topics.retrieval.prefetch_interval_seconds": "两次预热至少隔多少秒",
    "topics.retrieval.prefetch_ttl_seconds": "预热好的查询向量能用多少秒",
    "topics.retrieval.max_steps": "语义检索最多分几步",
    "agent.provider_id": "agent 用哪个模型",
    "agent.enabled": "允许她自主干活（开启Agent功能）",
    "agent.history_lookup_enabled": "允许她查历史",
    "agent.artifact_generation_enabled": "允许她做文件",
    "agent.file_send_enabled": "允许她把文件发出来",
    "agent.code_execution_enabled": "允许她执行代码（有风险）",
    "agent.inspect_capability_enabled": "允许她查看动作说明",
    "agent.max_steps": "一个任务最多几步",
    "agent.query_total_seconds": "一个任务最多跑多久（秒）",
    "agent.request_timeout_seconds": "单次调用超时（秒）",
    "agent.tasks_per_hour": "每小时最多开几个任务",
    "agent.code_runs_per_task": "一个任务最多跑几次代码（0 = 不限）",
    "agent.artifact_max_bytes": "单个产物字节上限",
    "agent.artifact_keep_days": "产物留多少天",
    "agent.commitment_enabled": "说「等会儿」要记成待办",
    "agent.commitment_nudge_minutes": "隔多久提醒她一次（分钟）",
    "agent.commitment_max_nudges": "最多提醒几次（提满就丢掉）",
    "history.lookup_action_enabled": "允许她翻聊天记录（内置动作）",
    "history.default_days": "没给时间范围时往回翻几天",
    "history.max_rows": "一次最多翻几条",
    "variation.rate": "每一轮换说法的概率",
    "variation.min_gap_turns": "两次换说法至少隔几轮",
    "variation.magnitude": "换说法最大能多放开",
    "reply_schema": "回复格式（精简/完整）",
    "reply_schema.legacy": "只要求她填 request（精简）",
    "meme.enabled": "表情包总开关",
    "meme.max_prompt_images": "一轮最多看几张图",
    "meme.meme_image_max": "一轮最多挑几张表情包",
    "meme.prefetch_enabled": "后台预热表情包检索",
    "meme.retrieval_mode": "怎么找表情包（自动/词法/语义）",
    "meme.embedding_provider_id": "语义检索用哪个嵌入服务",
    "meme.prefetch_candidate_count": "预热先取几条候选",
    "meme.search_candidate_count": "一轮检索取几条候选",
    "meme.max_candidate_images_per_prompt": "最多几张候选图进提示词",
    "meme.max_candidate_preview_bytes": "单张候选图字节上限",
    "meme.max_asset_bytes": "单个资产字节上限",
    "meme.exposure_keep_days": "展示记录留多少天",
    "meme.recent_context_messages": "检索看最近几条聊天",
    "meme.query_max_chars": "检索查询词最多几个字",
    "meme.prefetch_total_budget_ms": "预热总预算（毫秒）",
    "meme.max_search_rounds_per_reply": "一次回复最多搜几轮",
    "meme.max_send_per_reply": "一次回复最多发几张",
    "meme.send_meme_per_hour": "每小时最多发几张",
    "meme.max_send_per_day": "每天最多发几张",
    "meme.repeat_cooldown": "同一张图至少隔几条消息",
    "meme.recent_use_window": "刚发过的判定窗口（条）",
    "meme.max_asset_frames": "动图最多留几帧",
    "meme.storage_quota_bytes": "收藏总配额（字节）",
    "meme_send_scale.enabled": "发表情包前先缩图",
    "meme_send_scale.max_side": "缩图长边（像素）",
    "meme_send_scale.gif": "发表情包时转成 GIF",
    "meme_send_scale.animated": "动图处理方式（保持动图/仅首帧/不处理）",
    "meme_collect.enabled": "攒图评审收藏（总开关）",
    "meme_collect.batch_probability": "每张看到的图入池概率",
    "meme_collect.min_batch": "攒够几张才评审",
    "meme_collect.max_batch": "一次最多评审几张",
    "meme_collect.review_interval_hours": "两次评审间隔（小时）",
    "meme_collect.max_reviews_per_day": "每天最多评审几次",
    "meme_collect.sheet_size": "每张拼图几格",
    "meme_collect.candidate_ttl_days": "候选保留几天",
    "storage.vacuum_enabled": "自动缩库（回收空闲页）",
    "storage.vacuum_pages_per_run": "每次最多回收多少页",
}


def _search_from_actions(actions: list[Any]) -> dict[str, Any]:
    """把老存档里 search_web 上的检索字段搬成一份全局配置。"""

    for item in actions:
        if not isinstance(item, dict):
            continue
        if item.get("id") != "search_web" and item.get("tool_flow") != "search":
            continue
        return {
            "tool_names": list(item.get("tool_names") or []),
            "reader_tool_names": list(item.get("reader_tool_names") or []),
            "tool_mode": str(item.get("tool_mode") or "sequence"),
            "depth": str(item.get("search_depth") or "standard"),
            "depth_max": str(item.get("search_depth_max") or "deep"),
            "max_queries": int(item.get("search_max_queries") or 3),
            "max_reads": int(item.get("search_max_reads") or 2),
            "rounds": int(item.get("search_rounds") or 1),
            "freshness_days": int(item.get("search_freshness_days") or 0),
            "topic": str(item.get("search_topic") or ""),
            "query_template": str(item.get("search_query_template") or ""),
            "cite": bool(item.get("search_cite") or False),
        }
    return {}


def parse_world(data: dict[str, Any]) -> tuple[WorldConfig, list[str]]:
    """校验并修复世界配置，返回 (配置, 警告列表)。"""

    warnings: list[str] = []
    data = data or {}
    # 用户 m11008：默认初始关系从「陌生人」改成「群友」。只动"没亲手选过"的老存档
    # （bond_chosen 缺失），选过的保持原样；新世界在默认值里已经是「群友」。
    profile_data = data.get("profile")
    if (
        isinstance(profile_data, dict)
        and str(profile_data.get("default_bond") or "") == "陌生人"
        and not profile_data.get("bond_chosen")
    ):
        bonds = profile_data.get("bonds")
        moved: list[Any] = []
        if isinstance(bonds, list):
            # 老默认表里「陌生人」还带着 initial=True，ProfileConfig 的迁移
            # 会按那个标记把 default_bond 又改回陌生人（真机上就是这么被反悔的）。
            # 一起搬：只有「群友」保留初始标记。
            for item in bonds:
                if not isinstance(item, dict):
                    moved.append(item)
                    continue
                name = str(item.get("name") or "")
                moved.append({**item, "initial": name == "群友"})
        data = {
            **data,
            "profile": {
                **profile_data,
                "default_bond": "群友",
                "bond_chosen": True,
                **({"bonds": moved} if moved else {}),
            },
        }
        # 只补内存里这份，让世界照常跑；**不再往 warnings 里塞**（T17）：每次读盘都会跑
        # 到这里，而这里改的是局部 dict，从来落不到盘上——于是同一条提醒永远清不掉。
        # 提示改由 ``core.migrations`` 的 ``profile:default_bond`` 驱动。下同。
    # 联网搜索从"动作上的字段"搬到 world.search（用户 m10393）：
    # 老存档里没有 search 段，就把 search_web 上已经配好的值搬过来，别让老用户
    # 升级后丢掉自己挑的工具/档位。
    if "search" not in data:
        moved = _search_from_actions(data.get("actions") or [])
        if moved:
            data = {**data, "search": moved}
            # 提示见 ``core.migrations`` 的 ``search:from_actions``。
    world = WorldConfig.model_validate(
        normalize_legacy_keys(normalize_edge_keys(data))
    )
    # 调过的默认值**不再在这里自动改写**（T17）：`parse_world` 每次读盘都会跑，
    # 而 `save_world` 写回去的是入参原始 dict —— 内存里改的这两样从来落不到盘上，
    # 于是「用的是老默认值」的提醒每次进编辑器、每次保存都重新出现一次。
    # 现在由 `core.migrations.detect_all` 检测（纯函数），用户在编辑器里确认之后才经
    # `apply_ids` 只写目标路径。`TUNED_DEFAULT_UPGRADES` 表保留，是那份检测的数据源。
    # 谈资与表达变奏改成**默认开**（力度上限默认「大」）：只对**老格式**的配置跑一次迁移。
    # 判据是这两节里还没有 `config_version` —— 新版本保存时会写上 2，所以用户自己在页面上
    # 关掉之后再保存，这里就不会再把它顶回开着（以前只看 enabled is False，等于关不掉）。
    for path, label in (("variation", "表达变奏"), ("topics", "谈资")):
        section = getattr(world, path, None)
        if section is None:
            continue
        raw_section = data.get(path)
        raw_version = 0
        if isinstance(raw_section, dict):
            try:
                raw_version = int(raw_section.get("config_version") or 0)
            except (TypeError, ValueError):
                raw_version = 0
        if raw_version >= 2:
            continue
        setattr(section, "config_version", 2)
        if isinstance(raw_section, dict):
            raw_section["config_version"] = 2
        if getattr(section, "enabled", None) is False and path != "topics":
            section.enabled = True
            warnings.append(f"「{label}」现在默认开着，已经替你打开（不想用可以在页面上关掉）")
        if path == "variation" and str(getattr(section, "magnitude", "") or "") == "light":
            section.magnitude = "strong"
            warnings.append("「变奏力度上限」用的是老默认值，已按新版默认调整（轻 → 大）")
    # 分层候选、梦、收话短窗：同样只对老格式跑一次，
    # 判据是这三节里还没有 `config_version`（新版本保存时会写上 3）。
    # **分层候选不在这一组里**——它已经回到旧的默认（关），见下面单独那一段。
    for path, label in (("dream", "梦"), ("contributions", "收话短窗")):
        section = getattr(world, path, None)
        if section is None:
            continue
        raw_section = data.get(path)
        raw_version = 0
        if isinstance(raw_section, dict):
            try:
                raw_version = int(raw_section.get("config_version") or 0)
            except (TypeError, ValueError):
                raw_version = 0
        if raw_version >= 3:
            continue
        setattr(section, "config_version", 3)
        if isinstance(raw_section, dict):
            raw_section["config_version"] = 3
        if path == "contributions":
            try:
                window = float(getattr(section, "window_seconds", 0.0) or 0.0)
            except (TypeError, ValueError):
                window = 0.0
            if window <= 0:
                section.window_seconds = 120.0
                warnings.append("「收话短窗」现在默认攒 120 秒，已经替你打开（不想等可以在页面上改回 0）")
        elif getattr(section, "enabled", None) is False:
            section.enabled = True
            warnings.append(f"「{label}」现在默认开着，已经替你打开（不想用可以在页面上关掉）")

    # 分层候选回到旧默认（用户 m26298：退回规则顺序，别让她随手抽到拍照、自己解决）。
    # 老格式（`config_version` 还没有 4）一次性关掉，并写清"想开在页面上勾"；
    # 之后以页面上的选择为准，不再来回顶。
    selection = getattr(world, "action_selection", None)
    if selection is not None:
        raw_selection = data.get("action_selection")
        selection_version = 0
        if isinstance(raw_selection, dict):
            try:
                selection_version = int(raw_selection.get("config_version") or 0)
            except (TypeError, ValueError):
                selection_version = 0
        if selection_version < 4:
            setattr(selection, "config_version", 4)
            if isinstance(raw_selection, dict):
                raw_selection["config_version"] = 4
            if bool(getattr(selection, "enabled", False)):
                selection.enabled = False
                # 也写回 raw：保存一次之后文件里就是"关着"，下次再读不会又看见一个 true
                if isinstance(raw_selection, dict):
                    raw_selection["enabled"] = False
                warnings.append(
                    "「分层候选」已经按新默认关掉（回到旧的规则顺序：插话 / 睡觉 / 查东西 / 换地方 / 出门；"
                    "想开可以在页面上勾）"
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
    #
    # 这里**只补内存**，不往 warnings 里塞（T17，用户报的「缺少新内置动作的提示消不掉」
    # 就是这一条）：`parse_world` 每次读盘都跑，而 `save_world` 写回去的是入参原始 dict，
    # 补进来的动作从来落不到盘上——所以提示每进一次编辑器、每保存一次就重新出现一次。
    # 现在由 `core.migrations` 的 ``actions:missing_builtins`` 检测；用户在编辑器里点
    # 「应用」（追加到盘上的动作表）或「跳过」（记下来，之后不再问）之后就不再出现。
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
    # 同样只补内存，提示交给 ``core.migrations`` 的 ``profile:bonds_levels``。
    if not world.profile.bonds or not world.profile.levels:
        from .defaults import default_profile

        defaults = default_profile()
        if not world.profile.bonds:
            world.profile.bonds = [
                BondType.model_validate(item) for item in (defaults.get("bonds") or [])
            ]
        if not world.profile.levels:
            world.profile.levels = [
                IntimacyLevel.model_validate(item)
                for item in (defaults.get("levels") or [])
            ]

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

        fallback = default_world()
        # Recover the world graph, not permission gates or explicit topic settings.
        fallback["topics"] = world.topics.model_dump(mode="json")
        fallback["ui_knobs"] = dict(world.ui_knobs)
        recovered, recovery_warnings = parse_world(fallback)
        return recovered, warnings + recovery_warnings

    return world, warnings


def repeat_preview(payload: dict[str, Any] | None = None, *, now: float | None = None) -> dict[str, Any]:
    """把编辑器提交的频率配置翻译成 cron + 人话 + 接下来三次。

    编辑器不自己算 cron（前端只能猜到「每天 08:30」这一层，「工作日」「每 90 分钟」
    投影成表达式各有十几条边界），所以统一交给后端：参数与保存时同一套模型
    （:class:`ScheduleRepeat`），校验、投影、描述都在这里一次做完。

    ``payload`` 可以是完整的日程字段（``time`` / ``days`` / ``once`` / ``date``），
    也可以只给 ``repeat`` 或散装的 ``kind`` + ``at`` 等。返回 ``ok=False`` 时
    ``error`` 是给人看的原因，UI 原样显示即可。
    """

    data = dict(payload or {})
    schedule_payload: dict[str, Any] = {"id": "preview", "name": "预览"}
    for key in ("time", "days", "once", "date", "enabled"):
        if key in data:
            schedule_payload[key] = data[key]
    repeat = data.get("repeat")
    if isinstance(repeat, dict) and repeat:
        schedule_payload["repeat"] = repeat
    elif data.get("kind"):
        schedule_payload["repeat"] = {
            key: data[key]
            for key in ("kind", "at", "weekdays", "month_days", "every_minutes", "window", "expr", "note")
            if key in data
        }
    bad_clock = _preview_bad_clock(schedule_payload.get("repeat"))
    if bad_clock:
        # 模型会把写坏的时间点悄悄换成 08:00（运行时宁可跑起来），但预览要说实话
        return {"ok": False, "error": bad_clock}
    try:
        schedule = ScheduleDef.model_validate(schedule_payload)
    except Exception as exc:  # pydantic 的报错带 JSON，剥一层给人看
        return {"ok": False, "error": _preview_error_text(exc)}
    exprs = schedule.cron_exprs()
    result: dict[str, Any] = {
        "ok": True,
        "kind": schedule.repeat_kind(),
        "exprs": exprs,
        "trigger_times": schedule.trigger_times(),
        "when_text": schedule.when_text(),
        "next": [],
        "next_text": "",
    }
    from datetime import datetime as _datetime

    start = _datetime.fromtimestamp(now if now is not None else time.time())
    upcoming: list[Any] = []
    broken = ""
    for expr in exprs:
        try:
            spec = _preview_cron(expr)
        except Exception as exc:
            broken = broken or f"{expr}：{_preview_error_text(exc)}"
            continue
        cursor = start
        for _ in range(3):
            try:
                cursor = _preview_next(spec, cursor)
            except Exception:
                break
            upcoming.append(cursor)
    upcoming.sort()
    stamps = []
    seen: set[str] = set()
    for moment in upcoming:
        label = moment.strftime("%m-%d %H:%M")
        if label in seen:
            continue
        seen.add(label)
        stamps.append(label)
        if len(stamps) >= 3:
            break
    result["next"] = stamps
    if stamps:
        result["next_text"] = "下一次：" + stamps[0]
    elif schedule.repeat_kind() == "once":
        result["next_text"] = "只在这一天触发一次"
    elif broken:
        # 表达式写错了别假装没事：编辑器要把这句原样显示出来
        return {"ok": False, "error": f"这句话系统读不懂：{broken}", "exprs": exprs}
    return result


def _preview_bad_clock(repeat: Any) -> str:
    """预览专用：``at`` 里写坏的时间点要报错，别让 ``_clock_text`` 悄悄改成 08:00。"""

    if not isinstance(repeat, dict):
        return ""
    clocks = repeat.get("at")
    if isinstance(clocks, str):
        clocks = [clocks]
    for item in clocks or []:
        text = str(item or "").strip()
        parts = text.split(":")
        ok = len(parts) == 2
        if ok:
            try:
                hour, minute = int(parts[0]), int(parts[1])
                ok = 0 <= hour <= 23 and 0 <= minute <= 59
            except ValueError:
                ok = False
        if not ok:
            return f"时间点「{text}」不是 24 小时制的 HH:MM"
    return ""


def _preview_error_text(exc: Exception) -> str:
    text = str(exc)
    return text.split(chr(10), 1)[0][:200] or "这条频率写得不对"


def _preview_cron(expr: str):
    from .cron import parse as _parse

    return _parse(expr)


def _preview_next(spec, moment: Any) -> Any:
    from .cron import next_after as _next

    return _next(spec, moment)


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
        # 默认那三条日程原先没有描述（用户 m29086），早安那条还缺一句「说一句早安」。
        # 但**不在这里补**：以前在解析时改内存 + 每次都 warning，属于"自动改配置、
        # 每次加载都重来一遍"（T17 要消灭的模式）。现在交给待确认的内容迁移
        # （core/migrations.py 的 schedules:notes / schedules:morning_say），
        # 用户在编辑器里勾了才写盘。没应用之前，老存档就是"没有描述、不说早安"的样子。
        if schedule.repeat is not None:
            # 结构化频率是主口径：把老的 time/days 回填成它的"投影"，
            # 这样提示词、日志、老编辑器字段看到的时间仍然是同一天里的同一个时刻。
            repeat = schedule.repeat
            times = list(repeat.at or ["08:00"])
            schedule.time = times[0]
            kind = str(repeat.kind or "daily")
            if kind == "workdays":
                schedule.days = ["mon", "tue", "wed", "thu", "fri"]
            elif kind == "weekly" and repeat.weekdays:
                schedule.days = list(repeat.weekdays)
            elif kind in ("daily", "monthly", "every", "cron", "once"):
                schedule.days = sorted(valid_days)
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



# ===========================================================================
# B0 契约区（B 阶段新契约 —— 今天无人消费）
#
# 依据：`.vwtest/embedding-b-and-agent-capabilities-development.md` §11「B0：契约与观测」
# 与 `.vwtest/embedding-b-work-arrangement.md` 的 WP1。这一区只定义「长什么样、谁写、
# 谁读、B 阶段哪一步开始用」：不接 provider、不落盘、不改 A 阶段行为；A 阶段的代码
# 一个都不会 import 这一区。
#
# 本文件已有的内容被 B0 规则冻结（不许改已有类），所以这一区需要的 import 放在区首：
# 模块加载时它们先于下面的类定义执行，效果等同于写在文件顶部。
# ===========================================================================

from collections.abc import Mapping, Sequence  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from enum import Enum  # noqa: E402
import hashlib  # noqa: E402
import math  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402


EMBEDDING_FINGERPRINT_LEN = 16
"""``EmbeddingSpace.fingerprint`` 取 sha256 的前若干位十六进制（够用，日志里短）。"""


@dataclass(frozen=True)
class EmbeddingSpace:
    """一个向量空间的身份证：**谁**（供应商）、**哪个模型**、**多少维**。

    谁写：B0/WP4 的宿主适配层与配置层（从插件配置读出 ``provider_id`` 再拼出来）。
    谁读：B1 的召回（只有同一个空间才允许比相似度）、缓存与向量表（``embedding_model``
          + ``dim`` 当键）、降级判断（换了模型，旧向量一律作废）。
    为什么要 ``fingerprint``：三个字段散落在库表、缓存键和日志里，逐字段比较容易漏；
    指纹把它们压成一个稳定短串，**同样的三元组必得同样指纹**，跨进程也一致
    （用的是 sha256，不带 ``hash()`` 的随机盐）。
    """

    provider_id: str
    model: str
    dim: int

    def __post_init__(self) -> None:
        """非法空间当场拒绝：带着坏维度往下走，只会在召回时变成静默错配。"""

        if not str(self.provider_id).strip():
            raise ValueError("EmbeddingSpace 需要 provider_id（空串分不出供应商）")
        if not str(self.model).strip():
            raise ValueError("EmbeddingSpace 需要 model（空串分不出向量空间）")
        try:
            dim = int(self.dim)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"EmbeddingSpace.dim 必须是整数，收到 {self.dim!r}") from exc
        if dim <= 0:
            raise ValueError(f"EmbeddingSpace.dim 必须是正整数，收到 {dim}")

    @property
    def fingerprint(self) -> str:
        """稳定指纹：同样三元组必得同样结果（跨进程、跨次运行一致）。"""

        payload = (
            f"vw-embedding-space-v1\x1f{self.provider_id}"
            f"\x1f{self.model}\x1f{int(self.dim)}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:EMBEDDING_FINGERPRINT_LEN]

    def __str__(self) -> str:
        return f"{self.provider_id}/{self.model}@{int(self.dim)}d"


def check_embedding_vectors(
    vectors: Any,
    space: EmbeddingSpace | None,
    *,
    texts: Sequence[str] | None = None,
) -> str:
    """校验一批向量配不配得上它申报的空间：合法返回空串，非法返回中文原因。

    参数故意标成 ``Any``：这是**边界校验**，输入可能是外部适配器塞过来的任何东西。

    拒绝清单（B0 定死，宁可 ``ok=False`` 也不许静默放行）：
    1. ``space is None``：不知道空间和维度，收了就是错配；
    2. ``vectors`` 不是「向量序列」（每项不可迭代 / 分量不是数）；
    3. 一批空向量，或条数与 ``texts`` 对不上（传了 ``texts`` 才查）；
    4. 任何一条向量的长度 ≠ ``space.dim`` —— **这条必须拦住**；
    5. 任何分量是 NaN / ±Inf：入库会污染之后所有相似度；
    6. 全零向量：长度为零、没有方向，B1 的余弦会除零。这里当坏数据拒掉，
       而不是留给下游去撞 ``ZeroDivisionError``。
    """

    if space is None:
        return "缺少 embedding 空间（provider_id/model/dim 未知），无法判断向量能不能用"
    if isinstance(vectors, (str, bytes)) or not isinstance(vectors, Sequence):
        return f"vectors 需要「向量序列」，收到 {type(vectors).__name__}"
    if not vectors:
        return "embedding 结果是空的（texts 为空，或供应商一条向量都没返回）"
    if texts is not None and len(vectors) != len(texts):
        return f"向量条数 {len(vectors)} 与 texts 条数 {len(texts)} 对不上"
    for index, vector in enumerate(vectors):
        if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
            return f"第 {index} 条不是向量（{type(vector).__name__}）"
        if len(vector) != int(space.dim):
            return (
                f"第 {index} 条向量维度 {len(vector)} 与空间 {space} 声明的 "
                f"{int(space.dim)} 不一致"
            )
        for value in vector:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return f"第 {index} 条向量里有非数值分量 {value!r}"
            if not math.isfinite(number):
                return f"第 {index} 条向量含 NaN/Inf，不能入库也不能算相似度"
            if number != 0.0:
                break
        else:
            return f"第 {index} 条是全零向量（没有方向，算余弦会除零）"
    return ""


@dataclass(frozen=True)
class EmbeddingBatch:
    """一次 ``EmbeddingPort.embed`` 的结果信封。

    谁写：``EmbeddingPort`` 的实现（B1 的宿主适配层；B0 只有 ``tests/stub_ports.py``
          的替身 ``StubEmbedding``）。
    谁读：B1 的索引队列与召回（先看 ``ok``）；B2/B3 的查询工具；
          WP7 的日志与验收（读 ``latency_ms`` / ``usage``）。
    字段：
    - ``vectors``：``(n, dim)``，与传入的 ``texts`` 一一对应、顺序不变；
    - ``space``：这批向量**实际**属于哪个空间（失败时可以是 ``None``）；
    - ``ok`` / ``error``：**只有 ``ok=True`` 才允许写库或算相似度**；
      ``ok=False`` 时 ``vectors`` 约定为空元组，``error`` 说清原因；
    - ``usage`` / ``latency_ms``：观测用（B0 只落字段，WP7 记日志时才消费）。
    构造：正常批次走 :meth:`success`（内部按契约校验，坏数据**自动降级**成失败批次），
    失败批次走 :meth:`failure`。直接调构造器不会校验 —— 那是给「还原已校验过的数据」用的。
    注意：frozen 但**不可哈希**（``usage`` 是 dict）。要当缓存键请用
    ``space.fingerprint`` + 文本哈希。
    """

    vectors: tuple[tuple[float, ...], ...] = ()
    space: EmbeddingSpace | None = None
    ok: bool = False
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0

    @property
    def count(self) -> int:
        """向量条数（= 这次调用处理的文本数）。"""

        return len(self.vectors)

    @property
    def dim(self) -> int:
        """向量维度：优先看实际向量，其次看申报的空间，都没有就是 0。"""

        if self.vectors:
            return len(self.vectors[0])
        return int(self.space.dim) if self.space is not None else 0

    def validate(self) -> str:
        """把这条批次按契约再查一遍（消费方收到「外来」批次时问一句）；合法返回空串。"""

        if not self.ok:
            return self.error or "批次标记为 ok=False"
        return check_embedding_vectors(self.vectors, self.space)

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        space: EmbeddingSpace | None = None,
        latency_ms: float = 0.0,
        usage: Mapping[str, Any] | None = None,
    ) -> "EmbeddingBatch":
        """构造失败批次：``vectors`` 必为空 —— **失败时绝不带半截向量**给调用方。"""

        return cls(
            vectors=(),
            space=space,
            ok=False,
            error=str(error or "未知原因"),
            usage=dict(usage or {}),
            latency_ms=float(latency_ms or 0.0),
        )

    @classmethod
    def success(
        cls,
        vectors: Sequence[Sequence[float]],
        space: EmbeddingSpace | None,
        *,
        texts: Sequence[str] | None = None,
        latency_ms: float = 0.0,
        usage: Mapping[str, Any] | None = None,
    ) -> "EmbeddingBatch":
        """构造正常批次，但**先按契约校验**：维度不符 / NaN / 空 / 全零一律降级成失败批次。

        ``texts`` 可选：传了就顺带查「条数对不对」。降级时 ``error`` 写明原因，
        调用方只看 ``ok`` 就够了，不许「先信了再说」。
        """

        try:
            materialized = tuple(tuple(float(item) for item in vector) for vector in vectors)
        except (TypeError, ValueError) as exc:
            return cls.failure(
                f"vectors 不是二维数值序列：{exc}",
                space=space,
                latency_ms=latency_ms,
                usage=usage,
            )
        error = check_embedding_vectors(materialized, space, texts=texts)
        if error:
            return cls.failure(error, space=space, latency_ms=latency_ms, usage=usage)
        return cls(
            vectors=materialized,
            space=space,
            ok=True,
            error="",
            usage=dict(usage or {}),
            latency_ms=float(latency_ms or 0.0),
        )


class ToolStatus(str, Enum):
    """``ToolResult.status`` 的取值：**只允许**这五个（其它一律拒绝，不许自造）。

    沿用 A 阶段 ``SendReceipt`` 的口径：``unknown`` 是「结果无法确认」，**不等于成功**，
    也不许当成失败去盲重试；``partial`` 是「做成了一部分」，
    ``error`` / ``summary`` 里要说清缺了哪部分。
    """

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"
    PARTIAL = "partial"


TOOL_STATUSES: tuple[str, ...] = tuple(item.value for item in ToolStatus)
"""纯字符串白名单（配置校验 / 日志过滤 / 外部 JSON 用得上）。"""


def coerce_tool_status(value: Any) -> ToolStatus:
    """把 ``"ok"`` / ``ToolStatus.OK`` 归一成枚举；其它值抛 ``ValueError``（不猜、不放行）。"""

    if isinstance(value, ToolStatus):
        return value
    try:
        return ToolStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"未知的 ToolResult.status：{value!r}；只允许 {list(TOOL_STATUSES)}"
        ) from exc


def _b0_float(value: Any, field_name: str) -> float:
    """把 JSON 里的时间戳（数 / 数字字符串 / None）转成 float；其它抛 ``ValueError``。"""

    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是数，收到 {value!r}") from exc


def _b0_text_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    """把字符串序列转成元组；字符串本身不算序列（否则会被按字符拆开）。"""

    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} 需要字符串序列，收到 {value!r}")
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的统一回执信封（B2/B3 都用它；A 阶段不消费）。

    谁写：B2/B3 的工具执行器 —— 调用完工具立刻组装，**不许**回头补写（时间戳要对得上）。
    谁读：
    - B2 的临时聊天查询：先看 ``summary``（给模型的人话），内容太长就换成
      ``artifact_id`` 分页，绝不把原始正文整段塞进上下文；
    - B3 的任务状态机：看 ``status`` / ``next_cursor`` / ``side_effects`` 决定下一步、
      能否重试（``unknown`` 不重试）、任务能否恢复；
    - WP7 的日志与验收：靠 ``task_id`` / ``tool_call_id`` / ``started_at`` /
      ``finished_at`` 把一次任务的多步调用串起来。
    字段（与 `.vwtest/embedding-b-work-arrangement.md` WP1 逐字对齐）：
    - ``tool_call_id``：这一次工具调用的 id（B3 一次任务里多次调用各一个）；
    - ``task_id``：属于哪个任务（B2 的单轮查询也给一个，便于串联）；
    - ``action``：动作名（引擎里的动作 id，不是随手填的工具名）；
    - ``status``：只允许 ok / failed / timeout / unknown / partial；
    - ``started_at`` / ``finished_at``：epoch 秒（用 ``ClockPort.now()`` 填，测试才好注入）；
    - ``target``：作用对象（会话 / 设备 / 文件的稳定标识）；
    - ``summary``：给模型看的一句人话；
    - ``content``：原始结果正文（可能被 ``truncated`` 截断）；
    - ``artifact_id``：正文太大时的分页句柄（指向 :class:`QueryArtifact`）；
    - ``source_event_ids``：结果引用了哪些事件（B2 溯源用）；
    - ``side_effects``：这条结果**已经造成**的副作用（B4 的确认与审计靠它；只读工具应为空）；
    - ``next_cursor``：分页游标（空串 = 没有下一页）；
    - ``truncated``：``content`` 是否被截断（截断了就得给 ``artifact_id`` 或 ``next_cursor``）；
    - ``budget_free``：这一步**不占步数预算**（只给"同一任务里重复提交同一份草稿"用；
      默认 ``False``，不对齐 WP1，是 2026-10-22 加的第六个可选字段）。
    """

    tool_call_id: str = ""
    task_id: str = ""
    action: str = ""
    status: ToolStatus = ToolStatus.UNKNOWN
    started_at: float = 0.0
    finished_at: float = 0.0
    target: str = ""
    summary: str = ""
    content: str = ""
    artifact_id: str = ""
    source_event_ids: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    next_cursor: str = ""
    truncated: bool = False
    budget_free: bool = False
    """这一步**不占步数预算**（用户 2026-10-22）。

    只有一种情况：同一任务里重复提交**完全一样**的插件草稿（同 slug、同文件、同内容）
    ——那一步做过了，只回一句"这步做过了，草稿内容未变"，不重复写盘、也不吃预算。
    记在信封上是为了让账本能把这一条从"做成的步"里排除（见
    ``agent_tasks.ok_steps``）；状态仍是 ``ok``（这次调用本身是成功的，不是失败）。
    """

    def __post_init__(self) -> None:
        """``status`` 当场归一成枚举；非法值抛 ``ValueError``（frozen，用 ``object.__setattr__``）。"""

        object.__setattr__(self, "status", coerce_tool_status(self.status))

    @property
    def ok(self) -> bool:
        """只有 ``status == ok`` 才算成功（``partial`` 不算 —— 它只成功了一部分）。"""

        return self.status is ToolStatus.OK

    @property
    def duration_ms(self) -> float:
        """这一步花了多久（拿不到起止时间就是 0）。"""

        if not self.finished_at or not self.started_at:
            return 0.0
        return max(0.0, (float(self.finished_at) - float(self.started_at)) * 1000.0)

    def problems(self) -> tuple[str, ...]:
        """这条信封自己说不说得通（消费方在写进任务状态前问一句）；自洽就是空元组。

        只查逻辑自洽，不查业务：
        - 非 ok 却没有 ``summary``：失败得说不出原因，事故复盘时等于没记；
        - 非 ok 却带着 ``content``：失败结果不许带正文（和 ``EmbeddingBatch`` 同一口径）；
        - ``truncated`` 为真却没有 ``artifact_id`` / ``next_cursor``：被截掉的正文找不回来；
        - ``status == ok`` 却带着 ``side_effects``：要么该记 partial/failed，要么字段填错了；
        - ``finished_at`` 早于 ``started_at``：时间戳填反了。
        """

        issues: list[str] = []
        if not self.ok:
            if not self.summary:
                issues.append("status 不是 ok 却没有 summary（失败得说不出原因）")
            if self.content:
                issues.append("status 不是 ok 却带着 content")
        if self.truncated and not (self.artifact_id or self.next_cursor):
            issues.append("truncated 为真但没有 artifact_id / next_cursor")
        if self.ok and self.side_effects:
            issues.append("只读结果（ok）不该带 side_effects")
        if self.finished_at and self.started_at and self.finished_at < self.started_at:
            issues.append("finished_at 早于 started_at")
        return tuple(issues)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的普通 dict（入库 / 进事件 / 给 WebUI 都用这一份）。"""

        return {
            "tool_call_id": self.tool_call_id,
            "task_id": self.task_id,
            "action": self.action,
            "status": self.status.value,
            "started_at": float(self.started_at),
            "finished_at": float(self.finished_at),
            "target": self.target,
            "summary": self.summary,
            "content": self.content,
            "artifact_id": self.artifact_id,
            "source_event_ids": [str(item) for item in self.source_event_ids],
            "side_effects": [str(item) for item in self.side_effects],
            "next_cursor": self.next_cursor,
            "truncated": bool(self.truncated),
            "budget_free": bool(self.budget_free),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ToolResult":
        """从 :meth:`to_dict` 的输出（或库表行 / 事件 JSON）还原信封。

        非法输入**抛 ``ValueError``**（不返回「带 error 的结果」）：契约问题要在读写边界上
        炸出来，而不是变成一个「看起来失败了的工具结果」混进任务状态机。
        拒绝清单：不是 dict；``status`` 缺失；``status`` 不在五个取值里；``truncated`` /
        ``budget_free`` 不是布尔；``source_event_ids`` / ``side_effects`` 不是字符串序列；
        ``started_at`` / ``finished_at`` 不是数。
        额外键（B2/B3 以后新增的字段）**忽略**，这样旧读取器遇到新行不会崩。
        """

        if not isinstance(data, Mapping):
            raise ValueError(f"ToolResult.from_dict 需要 dict，收到 {type(data).__name__}")
        if "status" not in data:
            raise ValueError(
                "ToolResult.from_dict 缺 status：不许默认成 unknown"
                "（那是「结果待确认」，不是「没填」）"
            )
        status = coerce_tool_status(data.get("status"))
        truncated_raw = data.get("truncated", False)
        if not isinstance(truncated_raw, bool):
            raise ValueError(f"ToolResult.truncated 必须是布尔，收到 {truncated_raw!r}")
        budget_free_raw = data.get("budget_free", False)
        if not isinstance(budget_free_raw, bool):
            raise ValueError(f"ToolResult.budget_free 必须是布尔，收到 {budget_free_raw!r}")
        return cls(
            tool_call_id=str(data.get("tool_call_id") or ""),
            task_id=str(data.get("task_id") or ""),
            action=str(data.get("action") or ""),
            status=status,
            started_at=_b0_float(data.get("started_at"), "ToolResult.started_at"),
            finished_at=_b0_float(data.get("finished_at"), "ToolResult.finished_at"),
            target=str(data.get("target") or ""),
            summary=str(data.get("summary") or ""),
            content=str(data.get("content") or ""),
            artifact_id=str(data.get("artifact_id") or ""),
            source_event_ids=_b0_text_tuple(
                data.get("source_event_ids"), "ToolResult.source_event_ids"
            ),
            side_effects=_b0_text_tuple(data.get("side_effects"), "ToolResult.side_effects"),
            next_cursor=str(data.get("next_cursor") or ""),
            truncated=truncated_raw,
            budget_free=budget_free_raw,
        )


MEDIA_CACHE_REASONS = (
    "ok",
    "cached",
    "empty_source",
    "disabled",
    "download_failed",
    "store_failed",
    "over_round_limit",
    "timed_out",
)
"""``MediaCacheOutcome.reason`` 的封闭取值（E1 第 ④ 件）。

放在模块级而不是类属性：带类型注解的类属性会被 ``@dataclass`` 当成字段，
一个 tuple 默认值直接违反"不许用可变默认值"的规矩。
"""


@dataclass(frozen=True)
class MediaCacheOutcome:
    """一次「把图收进本地资产库」的**真实结果**（E1 第 ④ 件）。

    为什么要有它：``Engine.cache_media`` 的老口径是"失败一律返回空串"，
    调用方（以及以后的经历账）**分不出**三种完全不同的情况——
    「压根没开这个功能」「地址本来就是空的」「取了但没入库」。
    经历账要求"生成/上传了文件"必须**确实写入成功**才能算完成经历，
    所以这一层必须把真实成败说出来。

    谁写：``Engine.cache_media_ex``（以及 ``cache_media_list`` / ``cache_own_media``
    在限流、超时那两条路径上）。
    谁读：``Engine.recent_media_cache_failures()``；以后 E2 组装"生成文件"类经历时；
    ``engine_log`` 里 ``media_cache`` 事件的 detail。

    字段：
    - ``source``：被收的那个地址（原样，不裁剪；调用方要看全文就自己看）；
    - ``media_id``：成功时非空，失败时一律空串（与老口径一致）；
    - ``ok``：真 = 确实入库了；
    - ``reason``：``MEDIA_CACHE_REASONS`` 里的一个码，机器判断看这个；
    - ``detail``：给人看的一句话（可进账、可写日志）。
    """

    source: str = ""
    media_id: str = ""
    ok: bool = False
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的普通 dict（写进 ``media_cache`` 事件的 detail）。"""

        return {
            "source": str(self.source or ""),
            "media_id": str(self.media_id or ""),
            "ok": bool(self.ok),
            "reason": str(self.reason or ""),
            "detail": str(self.detail or ""),
        }


TRACE_ID_PREFIX = "tr"
"""``new_trace_id()`` 的前缀：日志里一眼认出「这是串一次任务多步调用的线索」。"""


def new_trace_id(prefix: str = TRACE_ID_PREFIX, now: float | None = None) -> str:
    """生成一个 ``trace_id``：**一次任务的多步调用共用同一个**。

    谁写：B2/B3 在任务入口（一次查询、一次多轮任务）生成一次，挂到这次任务的所有
    ``tool_call_id`` / 模型调用上；B0 只提供生成函数，还没接进引擎。
    谁读：WP7 的日志与验收（把同一次任务的多次模型调用、多次工具调用串成一条线）；
    B3 的重启恢复（新步骤挂回原任务）。
    格式：``tr<秒级时间戳后六位>-<8 位随机十六进制>`` —— 短、可读、同秒内也不撞。
    ``now`` 只为测试注入（对齐 ``core/events.py`` 的 ``new_thread_id(now=...)``）。
    """

    stamp = int(float(now if now is not None else time.time()))
    return f"{prefix or TRACE_ID_PREFIX}{stamp % 1000000:06d}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class QueryArtifact:
    """一次性只读产物（分页 / 大结果）的**句柄** —— B0 只定义类型，**不落盘**。

    谁写：B2 的查询工具（正文太长时写成临时产物，只把 id 放进 ``ToolResult.artifact_id``，
    正文不进模型上下文）；B0 不写盘，所以今天没有任何代码生成它。
    谁读：B2 的二次读取与分页；B3 的任务步骤（同一个 artifact_id 可以被多个步骤读）；
    宿主适配层（按 ``path`` 读，按 ``owner_session`` 做越权检查）。
    字段：
    - ``artifact_id``：句柄 id；
    - ``kind``：产物种类（如 ``text`` / ``json`` / ``table``），决定读取器怎么解；
    - ``path``：宿主上的位置（相对或绝对路径；B0 不写盘，这里只是约定）；
    - ``bytes``：字节数（B2 用它决定要不要分页、能不能整段塞进上下文）；
    - ``created_at`` / ``expires_at``：epoch 秒；``expires_at`` 为 0 表示不过期；
    - ``owner_session``：属主会话；跨会话读取必须由调用方**显式**授权，不许默认放开。
    """

    artifact_id: str = ""
    kind: str = ""
    path: str = ""
    bytes: int = 0
    created_at: float = 0.0
    expires_at: float = 0.0
    owner_session: str = ""

    def is_expired(self, now: float | None = None) -> bool:
        """过期了没（``expires_at`` 为 0 = 永不过期）；读之前先问一句。"""

        if not self.expires_at:
            return False
        return float(now if now is not None else time.time()) >= float(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的普通 dict（B2 存这段句柄时用）。"""

        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "path": self.path,
            "bytes": int(self.bytes),
            "created_at": float(self.created_at),
            "expires_at": float(self.expires_at),
            "owner_session": self.owner_session,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "QueryArtifact":
        """从 :meth:`to_dict` 的输出还原句柄；非法输入抛 ``ValueError``（同 ToolResult 口径）。"""

        if not isinstance(data, Mapping):
            raise ValueError(f"QueryArtifact.from_dict 需要 dict，收到 {type(data).__name__}")
        raw_size = data.get("bytes")
        try:
            size = int(raw_size or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"QueryArtifact.bytes 必须是整数，收到 {raw_size!r}") from exc
        return cls(
            artifact_id=str(data.get("artifact_id") or ""),
            kind=str(data.get("kind") or ""),
            path=str(data.get("path") or ""),
            bytes=size,
            created_at=_b0_float(data.get("created_at"), "QueryArtifact.created_at"),
            expires_at=_b0_float(data.get("expires_at"), "QueryArtifact.expires_at"),
            owner_session=str(data.get("owner_session") or ""),
        )
