"""表情包系统（阶段 M1）：**唯一**的配置读取点与四道闸门。

为什么单独一个模块：M1 只落配置与数据，M2 才把闸门接进回复路径。把
:func:`meme_settings` 与判定函数放在这里，M2 只 import 这一个模块就能拿到全部
口径，不用碰 ``core/engine.py``（那个文件有别的包正在改）。

设计要点：

* **只读纯函数**：``meme_settings()`` 绝不改传入的配置，也不写任何东西。
  页面上删掉的收藏不会因为读一次配置又冒出来（既有 ``topic_settings`` 的教训）。
* **坏值一律退回默认并记来源**：每个字段都有类型与上下限，越界就夹住，类型不对
  就用默认值，来源写进 ``sources``，管理页能把"这个数是夹过的"讲清楚。
* **总开关关着时闸门全静默但不改数值**：``enabled=False`` 时所有 ``gate_*`` 都
  返回拒绝原因，可是字段值原样保留——页面上还要显示"关着的时候预算是多少"。
* **四道闸门的顺序**：总开关 → 会话/来源范围 → 条目可用状态 → 发送前复核。
  顺序不能换：关着的时候不该去查数据库，越界的会话也不该先建展示记录。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

# --------------------------------------------------------------------------- #
# 字段表：一处定义，读取、夹取、校验、页面说明全都从这里取
# --------------------------------------------------------------------------- #

# 总开关这类布尔字段，单独一张表
# 收图另有专门的一节（``world.meme_collect``）：**没有"看到就全收进图库"这一档**，
# 只有"攒进候选池 -> 拼图评审 -> 通过才收藏"（用户 2026-10-29 拍板删掉了那个开关）。
# 这里的默认值也是"world 是 Mapping 但缺 meme 节"时的兜底，改这里就等于改那一档。
BOOLEAN_FIELDS: dict[str, bool] = {
    "enabled": True,
    "prefetch_enabled": True,
}

# 整数字段：名字 -> (默认值, 下限, 上限)
INT_FIELDS: dict[str, tuple[int, int, int]] = {
    "max_prompt_images": (10, 1, 20),
    "meme_image_max": (5, 0, 8),
    "prefetch_candidate_count": (3, 0, 8),
    "search_candidate_count": (5, 1, 8),
    "max_candidate_images_per_prompt": (5, 0, 8),
    "max_candidate_preview_bytes": (1000000, 0, 1 << 40),
    "max_asset_bytes": (10000000, 0, 1 << 40),
    "exposure_keep_days": (30, 1, 365),
    "recent_context_messages": (6, 1, 20),
    "query_max_chars": (1200, 200, 4000),
    "prefetch_total_budget_ms": (500, 100, 2000),
    "max_search_rounds_per_reply": (1, 0, 2),
    "max_send_per_reply": (1, 0, 3),
    "send_meme_per_hour": (20, 0, 200),
    # M3 新增：小时配额挡不住"每小时卡着上限发一整天"，所以再要一道按天的硬闸门。
    # 默认 100 比 20/小时宽松得多：正常聊天根本摸不到它，只有真刷图才会撞上。
    "max_send_per_day": (100, 0, 1000),
    "repeat_cooldown": (0, 0, 0),
    "recent_use_window": (20, 0, 100),
    "max_asset_frames": (200, 1, 10000),
    "storage_quota_bytes": (500000000, 0, 1 << 50),
}

# ``repeat_cooldown`` 的上限由「会话里最近用过的条数窗口」反推：比窗口还大的冷却
# 等于永远不再发同一张，那不是用户想要的语义，所以上限 = recent_use_window 的上限。
INT_FIELDS["repeat_cooldown"] = (0, 0, 100)

# 字符串字段：名字 -> (默认值, 允许取值)
# ``retrieval_mode`` 的三个档：``keyword``（纯关键词）/ ``embedding``（纯向量）/
# ``auto``（关键词打底 + 向量软排）。M4 另外接受 ``keyword_semantic`` 作为 ``auto``
# 的**显式写法**（同一个档、同一个行为）——这样"我要中间档"这件事能直接写出来，
# 不用靠"auto 到底是哪个 auto"去猜；默认值仍是 ``auto``，不动方案 §9 的那一格。
TEXT_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "retrieval_mode": (
        "auto",
        ("auto", "keyword", "keyword_semantic", "embedding"),
    ),
    "embedding_provider_id": ("", ()),
}

# 检索口径的三个档（``retrieval_mode`` 解析后的取值，见
# :attr:`MemeSettings.resolved_retrieval_mode`）
RETRIEVAL_KEYWORD = "keyword"
RETRIEVAL_KEYWORD_SEMANTIC = "keyword_semantic"
RETRIEVAL_EMBEDDING = "embedding"

# 所有字段的名字（顺序稳定，便于测试与页面按同一顺序显示）
FIELD_ORDER: tuple[str, ...] = (
    "enabled",
    "max_prompt_images",
    "meme_image_max",
    "prefetch_enabled",
    "retrieval_mode",
    "embedding_provider_id",
    "prefetch_candidate_count",
    "search_candidate_count",
    "max_candidate_images_per_prompt",
    "max_candidate_preview_bytes",
    "max_asset_bytes",
    "exposure_keep_days",
    "recent_context_messages",
    "query_max_chars",
    "prefetch_total_budget_ms",
    "max_search_rounds_per_reply",
    "max_send_per_reply",
    "send_meme_per_hour",
    "max_send_per_day",
    "repeat_cooldown",
    "recent_use_window",
    "max_asset_frames",
    "storage_quota_bytes",
)

# 闸门（gate）返回的固定原因串。文案在 :data:`GATE_REASONS`，只此一份。
GATE_OK = "ok"
CONFIG_DISABLED = "config_disabled"
SCOPE_UNAVAILABLE = "scope_unavailable"
STATE_DELETED = "state_deleted"
STATE_DISABLED = "state_disabled"
STATE_MISSING = "state_missing"
STATE_UNKNOWN = "state_unknown"
RATE_LIMITED = "rate_limited"
DAY_LIMITED = "day_limited"
COOLDOWN = "cooldown"
NO_CANDIDATE = "no_candidate"
PROVIDER_MISSING = "provider_missing"
ROUND_LIMIT = "round_limit"
SEND_LIMIT = "send_limit"

GATE_REASONS: dict[str, str] = {
    GATE_OK: "可以",
    CONFIG_DISABLED: "表情包总开关关着",
    SCOPE_UNAVAILABLE: "当前会话/来源范围用不了这条收藏",
    STATE_DELETED: "收藏记录已经没了",
    STATE_DISABLED: "这条收藏被禁用了",
    STATE_MISSING: "底层图片文件不见了",
    STATE_UNKNOWN: "查不到这条收藏",
    RATE_LIMITED: "这一小时发的表情包到上限了",
    DAY_LIMITED: "今天发的表情包到上限了",
    COOLDOWN: "同一张图刚发过，还在冷却里",
    NO_CANDIDATE: "什么都没搜到",
    PROVIDER_MISSING: "没配嵌入服务商，语义检索用不了",
    ROUND_LIMIT: "这次回复的检索轮数用完了",
    SEND_LIMIT: "这次回复已经发过表情包了",
}

# 条目可用状态（§6 的五态）。``deleted`` 是"记录没了但底层文件还在"，
# 与 ``missing``（记录在、文件没了）**方向相反**，不能合并成一个"不可用"。
STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_DELETED = "deleted"
STATE_MISSING = "missing"
AVAILABILITY_STATES: tuple[str, ...] = (
    STATE_ENABLED,
    STATE_DISABLED,
    STATE_DELETED,
    STATE_MISSING,
    "scope_unavailable",
)

AVAILABILITY_LABELS: dict[str, str] = {
    STATE_ENABLED: "启用",
    STATE_DISABLED: "已禁用",
    STATE_DELETED: "收藏记录已删除（底层文件还在）",
    STATE_MISSING: "文件缺失",
    "scope_unavailable": "当前范围不可用",
}

# 五态里属于"这条收藏不能发"的那几个（``state_unknown`` 是查不到，另算）
_BLOCKING_STATES = frozenset(
    {STATE_DISABLED, STATE_DELETED, STATE_MISSING, "scope_unavailable"}
)

# 来源范围的默认值：``*`` = 各会话/各来源都算数。
# 同资源多来源时**取交集**（见 ``meme_scope_allows``）：多一个来源不能放宽限制。
SCOPE_ANY = "*"


def _int_value(
    value: Any, default: int, low: int, high: int
) -> tuple[int, str]:
    """一个整数字段：``(值, 来源)``。坏值退回默认，越界夹住。"""

    if isinstance(value, bool) or value is None:
        return default, "default"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default, "invalid_fallback"
    if float(number) != float(value):
        # 3.7 这种小数当整数用是不小心的，按默认值处理而不是悄悄截断
        return default, "invalid_fallback"
    if number < low:
        return low, "clamped"
    if number > high:
        return high, "clamped"
    return number, "base"


def _bool_value(value: Any, default: bool) -> tuple[bool, str]:
    """一个布尔字段：``(值, 来源)``。只认真正的 ``True`` / ``False``。"""

    if value is True:
        return True, "base"
    if value is False:
        return False, "base"
    return default, "default"


def _text_value(
    value: Any, default: str, allowed: tuple[str, ...]
) -> tuple[str, str]:
    """一个字符串字段：``(值, 来源)``。枚举字段只认表里的取值。"""

    if value is None:
        return default, "default"
    if not isinstance(value, str):
        return default, "invalid_fallback"
    text = value.strip()
    if allowed and text not in allowed:
        return default, "invalid_fallback"
    return text, "base" if text == default else "custom"


def _section(world: Any) -> Any:
    """从世界配置里取 ``meme`` 这一节（对象、dict 都认；没有就返回空 dict）。"""

    if world is None:
        return {}
    if isinstance(world, Mapping):
        return world.get("meme", {}) or {}
    return getattr(world, "meme", None) or {}


def _field(section: Any, name: str, default: Any) -> Any:
    """从 ``meme`` 这一节里取一个字段的值（缺字段 = 用默认值）。"""

    if isinstance(section, Mapping):
        return section.get(name, default)
    return getattr(section, name, default)


@dataclass(frozen=True)
class MemeSettings:
    """一次解析出来的表情包生效配置（不可变，改不动原配置）。"""

    enabled: bool
    max_prompt_images: int
    meme_image_max: int
    prefetch_enabled: bool
    retrieval_mode: str
    embedding_provider_id: str
    prefetch_candidate_count: int
    search_candidate_count: int
    max_candidate_images_per_prompt: int
    max_candidate_preview_bytes: int
    max_asset_bytes: int
    exposure_keep_days: int
    recent_context_messages: int
    query_max_chars: int
    prefetch_total_budget_ms: int
    max_search_rounds_per_reply: int
    max_send_per_reply: int
    send_meme_per_hour: int
    max_send_per_day: int
    repeat_cooldown: int
    recent_use_window: int
    max_asset_frames: int
    storage_quota_bytes: int
    sources: Mapping[str, str]

    # ---------------- 派生量 ----------------

    @property
    def exposure_keep_seconds(self) -> float:
        """展示记录保留多少秒（与图片转述缓存的 ``cache_days`` 同口径）。"""

        return float(self.exposure_keep_days) * 86400.0

    @property
    def prefetch_total_budget_seconds(self) -> float:
        """预热总预算换算成秒（M2 计时用）。"""

        return float(self.prefetch_total_budget_ms) / 1000.0

    @property
    def send_limit_per_reply(self) -> int:
        """一轮实际最多发几张：候选上限与发送上限里更小的那个。

        用户填的原始值原样保留（页面按他填的显示），这里只给一个自洽的派生量：
        挑了 5 张却只能发 1 张不是错，但一轮发 3 张却只挑 1 张候选就没得发。
        """

        return max(0, min(self.max_send_per_reply, self.meme_image_max))

    @property
    def candidate_limit(self) -> int:
        """一轮实际最多挑几张候选：不超过检索取回的上限。"""

        return max(0, min(self.meme_image_max, self.search_candidate_count))

    @property
    def resolved_retrieval_mode(self) -> str:
        """``retrieval_mode`` 解析成**实际走哪一档**（M4 的三种口径）。

        为什么要有这一步：配置里那个值是给用户填的（``auto`` 是"你替我挑"的意思），
        检索层要的是"具体走哪条路"。映射只有一处，免得引擎与检索层各解释一遍：

        * ``keyword`` → :data:`RETRIEVAL_KEYWORD`；
        * ``embedding`` → :data:`RETRIEVAL_EMBEDDING`；
        * 其余（``auto`` / ``keyword_semantic`` / 任何认不出来的值）→
          :data:`RETRIEVAL_KEYWORD_SEMANTIC`——**默认走中间档**，也就是方案 §9 说的
          "无 embedding 时回退关键词"：中间档在拿不到向量时本来就降级成关键词，
          所以它是最安全的默认。
        """

        mode = str(self.retrieval_mode or "").strip()
        if mode == RETRIEVAL_KEYWORD:
            return RETRIEVAL_KEYWORD
        if mode == RETRIEVAL_EMBEDDING:
            return RETRIEVAL_EMBEDDING
        return RETRIEVAL_KEYWORD_SEMANTIC

    @property
    def values(self) -> dict[str, Any]:
        """只有数值，不带来源（页面、日志用）。"""

        return {name: getattr(self, name) for name in FIELD_ORDER}

    def to_dict(self) -> dict[str, Any]:
        return {"values": self.values, "sources": dict(self.sources)}

    def source_of(self, name: str) -> str:
        return str(self.sources.get(name, "default"))

    # ---------------- 闸门 1：总开关 ----------------

    def gate_config(self) -> str:
        """总开关。关着时后面三道闸门都不必问，也不该写任何记录。"""

        return GATE_OK if self.enabled else CONFIG_DISABLED

    # ---------------- 闸门 3：条目可用状态 ----------------

    def gate_state(self, state: str) -> str:
        """条目可用状态（:data:`AVAILABILITY_STATES` 之一）。"""

        if state == STATE_ENABLED:
            return GATE_OK
        if state == STATE_DISABLED:
            return STATE_DISABLED
        if state == STATE_DELETED:
            return STATE_DELETED
        if state == STATE_MISSING:
            return STATE_MISSING
        if state == "scope_unavailable":
            return SCOPE_UNAVAILABLE
        return STATE_UNKNOWN

    # ---------------- 闸门 4：发送前复核 ----------------

    def gate_resend(
        self,
        *,
        sent_this_reply: int = 0,
        sent_this_hour: int = 0,
        sent_this_day: int = 0,
        repeats: int = 0,
    ) -> str:
        """发送前复核：这一轮还能不能发、这一小时/这一天还有没有额度、是不是刚发过。

        ``repeats`` 是"这张图在最近 ``recent_use_window`` 条消息里出现过几次"：
        ``repeat_cooldown = 0`` 时完全不看它（默认就是 0，等于不限制）。

        一轮的实际上限是派生量 :attr:`send_limit_per_reply`（``max_send_per_reply``
        与 ``meme_image_max`` 取小），不直接用 ``max_send_per_reply``：只挑
        ``meme_image_max`` 张的场合，填再大的 ``max_send_per_reply`` 也发不多。

        ``sent_this_hour`` / ``sent_this_day`` 是**调用方数出来的真实发出张数**
        （M3 从 ``meme_usage`` 里数），不是估算：数不到就传 0，那等于"还没发过"，
        所以调用方必须真的去数，否则闸门形同虚设。两道都填 0 时按"不限"放行——
        那正是配额旋钮填 0 的语义（0 = 一张都不许发，走下面那两个分支）。
        """

        if self.send_limit_per_reply <= 0:
            return SEND_LIMIT
        if sent_this_reply >= self.send_limit_per_reply:
            return SEND_LIMIT
        if self.send_meme_per_hour <= 0 or sent_this_hour >= self.send_meme_per_hour:
            return RATE_LIMITED
        if self.max_send_per_day <= 0 or sent_this_day >= self.max_send_per_day:
            return DAY_LIMITED
        if self.repeat_cooldown > 0 and repeats > 0:
            return COOLDOWN
        return GATE_OK

    def gate_search_rounds(self, rounds_used: int = 0) -> str:
        """这一轮还能不能再搜一次。

        ``rounds_used`` 是**还要用的轮数**（0 = 本轮不搜），所以
        ``max_search_rounds_per_reply = 1`` 这个默认值放行 0 和 1、拦住 2——
        "一次回复最多搜几轮"说的就是上限本身。
        """

        limit = max(0, int(self.max_search_rounds_per_reply))
        used = max(0, int(rounds_used))
        return ROUND_LIMIT if used > limit else GATE_OK

    def gate_provider(self) -> str:
        """语义检索配没配服务商。``retrieval_mode = keyword`` 时永远算配上了。"""

        if self.retrieval_mode == "keyword":
            return GATE_OK
        # 留空 = 跟插件配置里的那个；这里没法知道宿主配了什么，所以只把显式空串
        # 且强制 embedding 的情况当作"缺服务商"。
        if self.retrieval_mode == "embedding" and not self.embedding_provider_id:
            return PROVIDER_MISSING
        return GATE_OK


def gate_reason_text(reason: str) -> str:
    """闸门原因 → 给人看的中文（回执与页面共用一份）。"""

    return GATE_REASONS.get(reason, reason or GATE_OK)


def meme_scope_allows(
    allowed_scopes: Any, scope_kind: str, scope_id: str = ""
) -> bool:
    """这条收藏在当前会话/来源范围内算不算数。

    ``allowed_scopes`` 是 ``meme_sources`` 那套口径的文本表示：``"*"`` = 不限；
    否则是逗号分隔的 ``kind``（``group`` / ``private`` / ``group_persona`` …）。
    另外支持 ``"kind:id"`` 精确到某个会话。

    **同资源多来源取交集**：调用方要把每个来源各自的 scope 都判一遍，全过才
    ``True``（见 :func:`meme_scope_intersection`）。多一个来源不许放宽限制。

    判定按"够具体才算"：``scope_id`` 留空时只认不带 ``:id`` 的条目
    （``"group"`` 放行、``"group:g1"`` 不放行——说"只有某个群"的收藏不该在任何
    未知群里生效）；``scope_id`` 非空时必须精确匹配。
    """

    text = str(allowed_scopes or "").strip()
    if not text or text == SCOPE_ANY:
        return True
    items = [part.strip() for part in text.split(",") if part.strip()]
    if SCOPE_ANY in items:
        return True
    kind = str(scope_kind or "").strip()
    ident = str(scope_id or "").strip()
    for item in items:
        if ":" in item:
            item_kind, _, item_id = item.partition(":")
            if item_kind.strip() == kind and item_id.strip() == ident:
                return True
        elif item == kind:
            return True
    return False


def meme_scope_intersection(scopes: Any, scope_kind: str, scope_id: str = "") -> bool:
    """一组来源各自的 scope 全都允许才算允许（交集）。

    一条都没有时按 ``True`` 算：没有来源记录 = 没限制过（老数据 / 手工建条目）。
    """

    items = list(scopes or [])
    if not items:
        return True
    return all(meme_scope_allows(item, scope_kind, scope_id) for item in items)


def meme_settings(world: Any) -> MemeSettings:
    """把世界配置里的 ``world.meme`` 解析成生效配置（只读，绝不改传入对象）。

    缺字段、坏值、越界都能跑：依次退回默认值 / 夹到边界，并把每个字段的来源写进
    ``sources``（``base`` / ``custom`` / ``clamped`` / ``default`` / ``invalid_fallback``）。
    于是老 ``world.json``（完全没有 ``meme`` 这一节）也能启动并拿到全套默认值。
    """

    section = _section(world)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for name, default in BOOLEAN_FIELDS.items():
        value, source = _bool_value(_field(section, name, default), default)
        values[name] = value
        sources[name] = source

    for name, (default, low, high) in INT_FIELDS.items():
        value, source = _int_value(_field(section, name, default), default, low, high)
        values[name] = value
        sources[name] = source

    for name, (default, allowed) in TEXT_FIELDS.items():
        value, source = _text_value(_field(section, name, default), default, allowed)
        values[name] = value
        sources[name] = source

    # 字段值一律原样保留（页面要按用户填的数字显示）；"能不能真发出去"这种
    # 自洽判断放在派生量里（``send_limit_per_reply`` / ``candidate_limit``）。
    ordered = {name: values[name] for name in FIELD_ORDER}
    return MemeSettings(
        **ordered, sources=MappingProxyType(dict(sources))
    )


# 名字长的别名，方便别处按"这就是表情包那一节"读写
resolve_world_meme_settings = meme_settings
