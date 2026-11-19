"""动作发现信息（``discovery``）的派生、回退与写入口（A3）。

要解决的两件事
--------------

1. **没有 discovery 的动作必须照常可用**。老动作库（内置 76 条、用户自己攒的、扩展带来的）
   里一条派生信息都没有，索引不能因此空洞、动作也不能因此消失。所以这里有一份
   **回退视图**：名称取 ``name``（没写就用 id）、简述取「够短且不含限制词」的原说明，
   取不到就标「需展开」并**保留完整说明**。
2. **模型写的字不能当授权**。生成说明不是授权（设计 §4）：工具、地点、关系数值、配额
   仍然只在权威字段里，由代码提取成 :func:`system_constraints` 与模型的
   ``key_constraints`` **分开**展示。

回退口径里最要紧的一条
----------------------

**绝不截断长描述**。截断会丢掉否定与限制（「不要在没有原话证据时记住任何事」截一半就
反过来了），所以只有「整段够短、且不含限制词」的原说明才配当简述；其余一律
``needs_expand=True`` + ``full_description`` 原样保留。

写入口只认允许的字段
--------------------

:func:`merge_derived` 只接受 :data:`ALLOWED_WRITE_FIELDS`；别的键进 ``rejected``，
**不写**。人工锁（``locked`` / ``locked_fields``）里的字段一律不动，只在 ``conflicts``
里给一句提示——「定义变了只提示，不静默覆盖用户写的字」。

时间 / 哈希都不自己取
----------------------

``now`` 由调用方给（库层与队列层都不自己取时钟，跟邻近模块一个规矩）；``input_hash``
是**出处**（这份简述是从哪一版定义生成的），不是「待整理的新版本」——所以
:func:`mark_pending` **不会**刷新它（刷了就等于"旧简述自动变成新定义的"，
回退口径当场失效）。

认不出 / 坏输入一律降级、绝不外抛
----------------------------------

定义上没有 ``discovery``、值是坏 dict、字段类型不对、定义根本不是 ``ActionDef``——
全部退化成「空派生信息 + 回退视图」，不抛异常。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .action_registry import (
    ActionRegistry,
    build_registry,
    canonical_json,
    definition_payload,
    identity_text,
)
from .models import ActionDef, ActionDiscovery, clean_identifier


def _float_or(value: Any, default: float = 0.0) -> float:
    """转浮点；转不成 / 非有限回 ``default``（跟 ``core/db.py`` 的同名工具一个口径）。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _as_definition(definition: Any) -> Any:
    """只读入口的统一预处理：``dict`` 先校成 ``ActionDef``，别的原样返回。

    为什么要这一步：上游（编辑器 / 保存入口）手里常常还是**原始 dict**。
    直接 ``getattr(dict, "enabled", True)`` 会永远拿到 ``True``——"停用"这类权威条件会被
    静默答错。这里校验一遍；坏 dict 回 ``None``，调用方按"认不出"处理，绝不抛。
    """

    if isinstance(definition, dict):
        try:
            return ActionDef.model_validate(dict(definition))
        except Exception:
            return None
    return definition


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DISCOVERY_GENERATOR_VERSION = "d1"
"""整理器配方版本。改提示词 / 改字段取舍时一起改它，旧结果就整体作废（口径见设计 §5）。"""

DISCOVERY_STATUS_MISSING = "missing"
DISCOVERY_STATUS_PENDING = "pending"
DISCOVERY_STATUS_READY = "ready"
DISCOVERY_STATUS_FAILED = "failed"
DISCOVERY_STATUS_STALE = "stale"
DISCOVERY_STATUS_MANUAL = "manual"
DISCOVERY_STATUSES: tuple[str, ...] = (
    DISCOVERY_STATUS_MISSING,
    DISCOVERY_STATUS_PENDING,
    DISCOVERY_STATUS_READY,
    DISCOVERY_STATUS_FAILED,
    DISCOVERY_STATUS_STALE,
    DISCOVERY_STATUS_MANUAL,
)

DISCOVERY_SOURCE_DISCOVERY = "discovery"
DISCOVERY_SOURCE_DESCRIPTION = "description"
DISCOVERY_SOURCE_NONE = "none"

ALLOWED_WRITE_FIELDS: tuple[str, ...] = (
    "summary",
    "when_to_use",
    "key_constraints",
    "tags",
    "detail_policy",
)
"""后台整理**允许回写**的字段（:func:`merge_derived` 的白名单）。

刻意不含 ``description`` / ``template`` / ``tool_names`` / ``preconditions`` 这些权威字段：
模型要改它们只能生成独立建议，走原来的编辑流程（设计 §6）。
"""

MANAGED_FIELDS: tuple[str, ...] = (
    "input_hash",
    "generator_version",
    "source_revision",
    "status",
    "error_code",
    "updated_at",
    "needs_expand",
    "locked",
    "locked_fields",
)
"""管理元数据：**只有代码写**。模型返回里带了这几个键也一律丢进 ``rejected``。"""

LOCKABLE_FIELDS: tuple[str, ...] = ALLOWED_WRITE_FIELDS
"""可以按字段人工锁定的字段（就是允许回写的那几个）。"""

FIELD_LABELS: dict[str, str] = {
    "summary": "简述",
    "when_to_use": "使用时机",
    "key_constraints": "关键限制",
    "tags": "标签",
    "detail_policy": "详情策略",
}
"""中文标签（给冲突提示与编辑器用）。"""

DISCOVERY_INPUT_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "description",
    "category",
    "llm_level",
    "scope",
    "allowed_nodes",
    "target_type",
    "preconditions",
    "params",
    "duration_mode",
    "duration_min",
    "duration_max",
    "tool_names",
    "tool_mode",
    "tool_flow",
    "reader_tool_names",
    "search_depth",
    "search_topic",
    "search_query_template",
    "trigger_command",
    "trigger_hint",
    "event_usable",
    "state_slot",
    "state_label",
)
"""参与「输入指纹」的权威字段：**改了它们才值得重新整理**。

刻意**不含**这几类：

* 展示类（``priority`` / ``visible`` / ``group`` / ``nickname_text`` / ``desc_mode``）——
  改图标、改排序、换名片文案不该花一次模型调用（设计 §5 的表格）；
* ``enabled``——停用/启用一次就重新整理一遍纯属浪费，能力没变；
* ``template`` / ``during`` / ``on_complete``——执行文案与结算，不影响「能做什么」；
* ``quota``——数值条件由代码管，模型整理不了它（设计 §6）。
"""

SENSITIVE_FIELD_MARKS: tuple[str, ...] = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "cookie",
    "credential",
    "authorization",
    "auth",
    "apikey",
    "private",
    "webhook",
    "session_id",
    "dsn",
)
"""指纹与模型输入的**敏感字段黑名单**（子串匹配、小写比较）。

为什么要有它：``DISCOVERY_INPUT_FIELDS`` 是显式白名单，本来就不该捞到密钥；但动作定义是
``Permissive`` 的、用户可以手写任意额外键，将来有人往白名单里加字段时很容易顺手把
``*_api_key`` 加进来。这里再加一道闸：即使字段名进了白名单，只要名字像密钥，
:func:`pick_input_fields` 也把它剔掉并报出来。**指纹里绝不含密钥**是硬要求。
"""

SAFE_FIELD_NAMES: tuple[str, ...] = ("key_constraints",)
"""名字里带敏感词、但其实不是密钥的已知字段（黑名单的假阳性出口）。

``key_constraints`` 的 "key" 是"要点"，不是"密钥"；不显式放行的话，将来把它加进
:data:`DISCOVERY_INPUT_FIELDS` 会被静默剔掉，排查起来很费劲。
"""

SUMMARY_FALLBACK_MAX_CHARS = 40
"""回退简述的最大长度（整段不超过它才敢直接当简述用；**绝不截断**）。"""

UNSAFE_SUMMARY_MARKS: tuple[str, ...] = (
    "不",
    "别",
    "禁止",
    "勿",
    "必须",
    "只能",
    "仅",
    "除非",
    "至少",
    "最多",
    "需要",
    "依赖",
    "等结果",
    "条件",
    "如果",
    "若",
    "not ",
    "never",
    "only",
    "unless",
    "must",
    "require",
)
"""出现这些词就说明原说明里有**限制或否定**，整段拿来当简述不安全。

宁可标「需展开」多给一行字，也不能让截断/简化把「不要记住没有原话的事」变成
「记住这件事」。
"""

DETAIL_POLICY_AUTO = "auto"
DETAIL_POLICY_ALWAYS = "always"
DETAIL_POLICY_ON_DEMAND = "on_demand"
DETAIL_POLICIES: tuple[str, ...] = (
    DETAIL_POLICY_AUTO,
    DETAIL_POLICY_ALWAYS,
    DETAIL_POLICY_ON_DEMAND,
)


# ---------------------------------------------------------------------------
# 读取（纯函数）
# ---------------------------------------------------------------------------


def discovery_of(definition: Any) -> ActionDiscovery:
    """读出定义上的派生信息；没有 / 坏值一律回**空对象**（绝不抛）。

    注意返回的是**新对象**（没有派生信息时）或者定义上那个对象本身（有时），所以
    "改返回值就等于改配置"这件事**不成立**——要写回请走 :func:`apply_derived` /
    :func:`mark_pending` 这些写入口。
    """

    raw: Any = None
    if isinstance(definition, dict):
        raw = definition.get("discovery")
    else:
        raw = getattr(definition, "discovery", None)
    if isinstance(raw, ActionDiscovery):
        return raw
    if isinstance(raw, dict):
        try:
            return ActionDiscovery.model_validate(raw)
        except Exception:
            return ActionDiscovery()
    return ActionDiscovery()


def discovery_status(definition: Any) -> str:
    """派生信息的状态；认不出回 ``"missing"``。"""

    status = str(getattr(discovery_of(definition), "status", "") or "")
    return status if status in DISCOVERY_STATUSES else DISCOVERY_STATUS_MISSING


def pick_input_fields(
    definition: Any, *, fields: Sequence[str] = DISCOVERY_INPUT_FIELDS
) -> tuple[dict[str, Any], list[str]]:
    """按白名单取权威字段，返回 ``(数据, 被剔掉的敏感字段名)``。

    第二步是给「指纹里绝不含密钥」兜底的：字段名里有 :data:`SENSITIVE_FIELD_MARKS`
    的一律不进返回值（并如实报出来，好让上层发现"白名单被写歪了"）。
    """

    payload = definition_payload(definition)
    picked: dict[str, Any] = {}
    dropped: list[str] = []
    for name in fields:
        if name not in payload:
            continue
        if is_sensitive_field(name):
            dropped.append(str(name))
            continue
        picked[str(name)] = payload[name]
    return picked, dropped


def is_sensitive_field(name: Any) -> bool:
    """这个字段名像不像密钥 / 凭据（子串匹配、小写比较）。

    先用 :data:`SAFE_FIELD_NAMES` 挡掉已知的**假阳性**（``key_constraints`` 里的 "key"
    只是"要点"的意思），再按子串匹配。宁可多剔一个普通字段（有 :func:`pick_input_fields`
    的第二个返回值兜着，上层能看见），也不能把密钥算进指纹。
    """

    text = str(name or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered in SAFE_FIELD_NAMES:
        return False
    return any(mark in lowered for mark in SENSITIVE_FIELD_MARKS)


def discovery_input_payload(
    definition: Any, *, fields: Sequence[str] = DISCOVERY_INPUT_FIELDS
) -> dict[str, Any]:
    """只含必需字段的输入载荷（绝不包含密钥）。"""

    return pick_input_fields(definition, fields=fields)[0]


def discovery_input_hash(
    definition: Any, *, fields: Sequence[str] = DISCOVERY_INPUT_FIELDS
) -> str:
    """输入指纹：白名单字段的稳定哈希前 32 位；没有可用字段回空串。

    用途有两个：判断"这份简述是不是已经过期"（与 ``ActionDiscovery.input_hash`` 比），
    以及给整理队列当作业身份（A4 的 ``input_hash`` 列直接用它）。
    """

    payload = discovery_input_payload(definition, fields=fields)
    if not payload:
        return ""
    text = canonical_json(payload)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 系统提取的关键限制（权威，代码算）
# ---------------------------------------------------------------------------


def _int_attr(definition: Any, name: str, default: int = 0) -> int:
    try:
        return int(getattr(definition, name, default) or default)
    except (TypeError, ValueError):
        return default


def system_constraints(definition: Any) -> list[str]:
    """从**权威字段**里用代码提出来的关键限制（模型不许改的那部分）。

    这些是索引里"关键限制"的可靠来源：地点、工具依赖、对象、时长、事件、前置条件、
    配额、停用。模型补充的限制（``discovery.key_constraints``）在视图里排在它们后面，
    两者**不混在一起**——生成说明不是授权。

    ``definition`` 是原始 dict 时先校成 ``ActionDef``（否则"停用"这类条件会被答错）；
    坏定义 / 认不出的字段一律跳过；此函数**不会抛**，最坏返回空列表。
    """

    definition = _as_definition(definition)
    if definition is None:
        return []
    picked: list[str] = []
    if not bool(getattr(definition, "enabled", True)):
        picked.append("当前已停用")
    scope = str(getattr(definition, "scope", "") or "")
    nodes = [str(item) for item in (getattr(definition, "allowed_nodes", None) or []) if str(item)]
    if scope == "node":
        picked.append(
            f"只在指定地点可用（{len(nodes)} 个）" if nodes else "地点限定为空，已按全局处理"
        )
    level = str(getattr(definition, "llm_level", "") or "")
    tools: list[str] = []
    candidates = getattr(definition, "tool_candidates", None)
    if callable(candidates):
        try:
            tools = [str(item).strip() for item in candidates() if str(item).strip()]
        except Exception:
            tools = []
    if not tools:
        tools = [str(item).strip() for item in (getattr(definition, "tool_names", None) or []) if str(item)]
    if level == "tool":
        if tools:
            picked.append(f"依赖外部工具：{'、'.join(tools)}（装没装由系统在执行前核对）")
        else:
            picked.append("工具型动作但没有绑定工具，执行会失败")
        if str(getattr(definition, "tool_flow", "") or "") == "search":
            picked.append("联网检索型：要等搜索结果回来再说")
    elif level == "command":
        command = str(getattr(definition, "trigger_command", "") or "").strip()
        picked.append(f"触发宿主指令：{command}" if command else "指令型动作但没有配置指令名")
    target = str(getattr(definition, "target_type", "") or "")
    if target == "user":
        picked.append("要指定某个人")
    elif target == "group":
        picked.append("会发到当前会话")
    if str(getattr(definition, "duration_mode", "") or "") == "llm":
        low = _int_attr(definition, "duration_min")
        high = _int_attr(definition, "duration_max")
        picked.append(f"时长由你定（{low}~{high} 秒）" if high > low else "时长由你定")
    state_slot = str(getattr(definition, "state_slot", "") or "").strip()
    if state_slot:
        label = str(getattr(definition, "state_label", "") or "").strip() or state_slot
        picked.append(f"结果会记进状态槽「{label}」")
    if str(getattr(definition, "event_usable", "") or "") == "deny":
        picked.append("遇到事件时不能调用")
    preconditions = getattr(definition, "preconditions", None)
    if preconditions is not None:
        try:
            min_energy = getattr(preconditions, "min_energy", None)
        except Exception:
            min_energy = None
        if min_energy is not None:
            try:
                picked.append(f"体力至少 {float(min_energy):g}")
            except (TypeError, ValueError):
                pass
        not_state = [str(item) for item in (getattr(preconditions, "not_state", None) or []) if str(item)]
        if not_state:
            picked.append(f"这些状态里不能做：{'、'.join(not_state)}")
    quota = getattr(definition, "quota", None)
    if quota is not None:
        day = _int_attr(quota, "day")
        week = _int_attr(quota, "week")
        month = _int_attr(quota, "month")
        parts = [
            text
            for text, value in (("每天", day), ("每周", week), ("每月", month))
            if value > 0
        ]
        if parts:
            picked.append("有次数上限（" + "、".join(parts) + "）")
    return picked


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryView:
    """一条动作的**发现视图**：有派生信息就用它，没有就走安全回退。

    这份视图是渲染（A6）与观测（A6）的唯一输入口径：``summary`` 可能是空的，
    但 ``full_description`` **永远保留**，``needs_expand`` 说明"索引里必须标需展开"。
    """

    action_id: str
    identity: tuple[str, str]
    namespace: str
    name: str
    summary: str
    when_to_use: str
    key_constraints: tuple[str, ...]
    tags: tuple[str, ...]
    detail_policy: str
    needs_expand: bool
    degraded: bool
    source: str
    status: str
    stale_reason: str
    full_description: str
    enabled: bool
    builtin: bool

    @property
    def identity_text(self) -> str:
        return identity_text(*self.identity)

    def index_line(self, *, max_constraints: int = 3) -> str:
        """这一条在能力索引里的那一行（渲染细节见 :func:`render_index_line`）。"""

        return render_index_line(self, max_constraints=max_constraints)

    def detail_text(self) -> str:
        """**完整说明兼容模式**：简述写不出来时，索引让位、这里照旧给全文。"""

        lines = [f"{self.action_id}｜{self.name}"]
        if self.summary:
            lines.append(f"能做什么：{self.summary}")
        if self.when_to_use:
            lines.append(f"适用：{self.when_to_use}")
        if self.key_constraints:
            lines.append("关键限制：" + "；".join(self.key_constraints))
        if self.full_description:
            lines.append(f"完整说明：{self.full_description}")
        return "\n".join(lines)


INDEX_SUMMARY_CLIP = 40
INDEX_WHEN_CLIP = 24
"""索引一行的"能做什么 / 什么时候用"各留多少字。

索引现在是**唯一清单**（不再受字数上限），所以它必须自己瘦：一句能说清的就不给
两句，剩下的细节归 ``inspect_capability`` 展开。这两个数字是"一眼能读懂"的边界，
不是预算的替代品。
"""


def _clip_index_text(text: str, limit: int) -> str:
    body = str(text or "").strip()
    if limit <= 0 or len(body) <= limit:
        return body
    return body[:limit].rstrip("，、；;：: ") + "…"


def render_index_line(view: DiscoveryView, *, max_constraints: int = 3) -> str:
    """把一条视图渲染成索引里的一行（**回退口径的落地点**）。

    形状：``action_id｜名称：能做什么。适用：…。限制：…``（设计 §2）。
    写不出简述时这里必须显示「简述需要展开」，而不是硬编一句——模型看不到它，
    就会去读完整说明（``detail_text`` / 未来的 ``inspect_capability``），
    而不是凭名字猜。
    """

    head = f"{view.action_id}｜{view.name}"
    if view.summary:
        line = f"{head}：{_clip_index_text(view.summary, INDEX_SUMMARY_CLIP)}"
    else:
        line = f"{head}：简述需要展开"
    if view.when_to_use:
        line += " 适用：" + _clip_index_text(view.when_to_use, INDEX_WHEN_CLIP)
    limits = list(view.key_constraints)
    if max_constraints > 0 and len(limits) > max_constraints:
        extra = len(limits) - max_constraints
        limits = limits[:max_constraints] + [f"另有 {extra} 条"]
    if limits:
        line += " 限制：" + "；".join(limits)
    if view.needs_expand:
        line += "（完整说明需展开）"
    if not view.enabled:
        line += "（已停用）"
    return line


def _summary_state(
    discovery: ActionDiscovery, current_hash: str
) -> tuple[bool, str, str]:
    """这份派生简述现在还能不能用：``(可用, 来源, 原因)``。

    规则（顺序有意义）：

    1. 人工锁 / 人工写的字**优先**：锁定且非空 → 直接用（后台不许覆盖它）；
    2. 没有简述 → 不可用（回退）；
    3. 有 ``input_hash`` 且与当前指纹不一致 → **过期**，不可用（旧简述已经和新能力不符，
       继续拿它当现状就是骗模型）；
    4. 状态是 ``pending`` / ``failed`` / ``stale`` → 不可用（正在整理 / 整理失败了，
       手上的字是上一版留下的）；
    5. 其余（``ready`` / ``manual`` / ``missing`` 但写了字）→ 可用。
    """

    summary = str(getattr(discovery, "summary", "") or "").strip()
    if not summary:
        return (False, "", "empty")
    locked_fields = {str(item) for item in (getattr(discovery, "locked_fields", None) or [])}
    if bool(getattr(discovery, "locked", False)) or "summary" in locked_fields:
        return (True, DISCOVERY_SOURCE_DISCOVERY, "manual_lock")
    if current_hash and str(getattr(discovery, "input_hash", "") or "").strip():
        if str(discovery.input_hash).strip() != current_hash:
            return (False, "", "input_hash_mismatch")
    status = str(getattr(discovery, "status", "") or "")
    if status in (DISCOVERY_STATUS_PENDING, DISCOVERY_STATUS_FAILED, DISCOVERY_STATUS_STALE):
        return (False, "", status)
    return (True, DISCOVERY_SOURCE_DISCOVERY, "ok")


def fallback_summary(definition: Any) -> tuple[str, bool]:
    """没有可用派生简述时的回退：``(简述, 是否需展开)``。

    * ``desc_mode == "brief"``：作者已经表态「看一眼名字就懂」→ 直接用原说明；
    * 原说明整段不超过 :data:`SUMMARY_FALLBACK_MAX_CHARS` **且不含限制词** → 直接用它；
    * 其余（长、或含「不 / 必须 / 只能 / 除非…」）→ ``("", True)``：**标需展开、不截断**。
    """

    definition = _as_definition(definition)
    if definition is None:
        return ("", True)
    text = str(getattr(definition, "description", "") or "").strip()
    if not text:
        return ("", True)
    if str(getattr(definition, "desc_mode", "") or "") == "brief":
        return (text, False)
    if len(text) <= SUMMARY_FALLBACK_MAX_CHARS and not has_unsafe_marks(text):
        return (text, False)
    return ("", True)


def has_unsafe_marks(text: Any) -> bool:
    """这句话里有没有"限制 / 否定"的味道（有就不敢当简述用）。"""

    body = str(text or "")
    if not body:
        return False
    lowered = body.lower()
    return any(mark in body or mark in lowered for mark in UNSAFE_SUMMARY_MARKS)


def _dedupe(items: Iterable[Any]) -> tuple[str, ...]:
    """去空、去重、保序（中文限制短语可能被代码与模型各写一遍）。"""

    picked: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in picked:
            picked.append(text)
    return tuple(picked)


def build_view(
    definition: Any,
    *,
    identity: tuple[str, str] | None = None,
    action_id: str = "",
    current_hash: str = "",
) -> DiscoveryView:
    """把一条定义变成发现视图（**绝不抛**：坏定义退化成空名 + 需展开）。

    ``definition`` 是原始 dict（还没走 ``parse_world`` 的那种）时先校一遍——
    否则 ``name`` / ``description`` / ``enabled`` 这些字段会全部读不到。
    """

    definition = _as_definition(definition)
    action_id = clean_identifier(action_id) or clean_identifier(getattr(definition, "id", ""))
    namespace = identity[0] if identity else ""
    discovery = discovery_of(definition)
    if not current_hash:
        current_hash = discovery_input_hash(definition)
    usable, source, why = _summary_state(discovery, current_hash)
    model_limits = tuple(
        str(item).strip()
        for item in (getattr(discovery, "key_constraints", None) or [])
        if str(item).strip()
    )
    tags = tuple(
        str(item).strip() for item in (getattr(discovery, "tags", None) or []) if str(item).strip()
    )
    if usable:
        summary = str(getattr(discovery, "summary", "") or "").strip()
        when_to_use = str(getattr(discovery, "when_to_use", "") or "").strip()
        needs_expand = bool(getattr(discovery, "needs_expand", False))
        detail_policy = str(getattr(discovery, "detail_policy", "") or DETAIL_POLICY_AUTO)
        if detail_policy not in DETAIL_POLICIES:
            detail_policy = DETAIL_POLICY_AUTO
        degraded = False
    else:
        summary, needs_expand = fallback_summary(definition)
        when_to_use = ""
        model_limits = ()
        detail_policy = DETAIL_POLICY_ALWAYS if needs_expand else DETAIL_POLICY_AUTO
        degraded = True
    constraints = _dedupe([*system_constraints(definition), *(model_limits if usable else ())])
    return DiscoveryView(
        action_id=action_id,
        identity=identity or (namespace, action_id),
        namespace=namespace,
        name=str(getattr(definition, "name", "") or "").strip() or action_id,
        summary=summary,
        when_to_use=when_to_use,
        key_constraints=constraints,
        tags=tags if usable else (),
        detail_policy=detail_policy,
        needs_expand=needs_expand,
        degraded=degraded,
        source=source if usable else (DISCOVERY_SOURCE_DESCRIPTION if summary else DISCOVERY_SOURCE_NONE),
        status=str(getattr(discovery, "status", "") or DISCOVERY_STATUS_MISSING),
        stale_reason="" if usable else why,
        full_description=str(getattr(definition, "description", "") or "").strip(),
        enabled=bool(getattr(definition, "enabled", True)),
        builtin=bool(getattr(definition, "builtin", False)),
    )


def build_views(
    source: Any,
    *,
    extension_actions: Iterable[tuple[str, dict]] = (),
    defaults: Any = None,
) -> list[DiscoveryView]:
    """一次算出一批视图（按裸 id 升序，跨机器可复现）。

    ``source`` 认三种：:class:`ActionRegistry`（用它的**生效**条目）、一串 ``ActionDef``、
    或者任何能交给 :func:`build_registry` 的 world（``WorldConfig`` / dict / ``None``）。
    """

    if isinstance(source, ActionRegistry):
        registry = source
    elif isinstance(source, (list, tuple)):
        registry = build_registry(
            {"actions": list(source)}, extension_actions=extension_actions, defaults=defaults
        )
    else:
        registry = build_registry(source, extension_actions=extension_actions, defaults=defaults)
    views = [
        build_view(entry.definition, identity=entry.identity, action_id=entry.action_id)
        for entry in registry.effective.values()
    ]
    views.sort(key=lambda item: item.action_id)
    return views


# ---------------------------------------------------------------------------
# 写入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryWriteResult:
    """一次派生信息回写的结果（成功与否都返回它，调用方不用靠异常分流）。"""

    discovery: ActionDiscovery
    written: tuple[str, ...] = ()
    skipped_locked: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    applied: bool = False
    """是否真的写回了定义对象（纯 :func:`merge_derived` 恒为 ``False``）。"""

    @property
    def changed(self) -> bool:
        """这次有没有真的写进至少一个字段。"""

        return bool(self.written)


def _normalize_write_value(field_name: str, value: Any) -> Any:
    """把模型给的字段值洗成该字段的类型；洗不出来回 ``None``（= 不写）。"""

    if field_name in ("summary", "when_to_use"):
        text = " ".join(str(value or "").split())
        return text or None
    if field_name in ("key_constraints", "tags"):
        if isinstance(value, str):
            items: Sequence[Any] = [value]
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            return None
        picked = _dedupe(items)
        return list(picked) or None
    if field_name == "detail_policy":
        text = str(value or "").strip().lower()
        return text if text in DETAIL_POLICIES else None
    return None


def merge_derived(
    current: Any,
    payload: Any,
    *,
    input_hash: str = "",
    source_revision: str = "",
    generator_version: str = DISCOVERY_GENERATOR_VERSION,
    now: float = 0.0,
    manual: bool = False,
) -> DiscoveryWriteResult:
    """把一份整理结果**按白名单**合进派生信息（纯函数，不动定义对象）。

    规则：

    * 只接受 :data:`ALLOWED_WRITE_FIELDS`；别的键（含 :data:`MANAGED_FIELDS`）进
      ``rejected``；洗不出值（类型不对、取值不合法）的键也进 ``rejected``——
      "这次没写进去"对调用方是同一件事，不该有一类悄悄消失；
    * 人工锁优先：``locked=True`` 时（``manual=False``）**一个字段都不写**；
      ``locked_fields`` 里的字段跳过并进 ``skipped_locked`` + ``conflicts``；
    * **空值不写**：模型返回空串 / 空列表时保留已有内容（不把人工或上一次的简述抹掉）；
    * 写完刷新管理元数据：``input_hash``（这次依据的指纹）/ ``generator_version`` /
      ``source_revision`` / ``updated_at`` / ``status``（``manual`` 或 ``ready``）/
      ``error_code=""``；
    * ``needs_expand`` 跟着结果走：写出简述就清掉，写不出仍为空就标上。

    ``current`` 可以是 ``ActionDiscovery`` / dict / 定义对象 / ``None``；认不出按空处理。
    任何坏输入都不会抛——洗不出来的字段进 ``rejected``。
    """

    base = current
    if not isinstance(base, ActionDiscovery):
        if isinstance(base, dict) and ("summary" in base or "status" in base):
            try:
                base = ActionDiscovery.model_validate(base)
            except Exception:
                base = ActionDiscovery()
        else:
            base = discovery_of(current) if current is not None else ActionDiscovery()
    result = base.model_copy(deep=True)
    rejected: list[str] = []
    skipped: list[str] = []
    conflicts: list[str] = []
    written: list[str] = []

    body = payload if isinstance(payload, dict) else {}
    locked_all = bool(result.locked) and not manual
    for key, raw in body.items():
        name = str(key)
        if name not in ALLOWED_WRITE_FIELDS:
            rejected.append(name)
            continue
        if locked_all:
            skipped.append(name)
            continue
        if name in {str(item) for item in (result.locked_fields or [])} and not manual:
            skipped.append(name)
            conflicts.append(f"「{FIELD_LABELS.get(name, name)}」已人工锁定，后台整理不覆盖")
            continue
        value = _normalize_write_value(name, raw)
        if value is None:
            rejected.append(name)
            continue
        setattr(result, name, value)
        written.append(name)

    if locked_all and body:
        conflicts.append("这一条已整条人工锁定，后台整理一个字都没写")

    if written or manual:
        result.input_hash = str(input_hash or "")
        result.generator_version = str(generator_version or "")
        if source_revision:
            result.source_revision = str(source_revision)
        result.updated_at = _float_or(now)
        result.status = DISCOVERY_STATUS_MANUAL if manual else DISCOVERY_STATUS_READY
        result.error_code = ""
        result.needs_expand = not str(result.summary or "").strip()
    return DiscoveryWriteResult(
        discovery=result,
        written=tuple(sorted(set(written))),
        skipped_locked=tuple(sorted(set(skipped))),
        rejected=tuple(sorted(set(rejected))),
        conflicts=tuple(conflicts),
    )


def apply_derived(
    definition: Any,
    payload: Any,
    *,
    input_hash: str = "",
    source_revision: str = "",
    generator_version: str = DISCOVERY_GENERATOR_VERSION,
    now: float = 0.0,
    manual: bool = False,
) -> DiscoveryWriteResult:
    """:func:`merge_derived` + 写回定义对象（句柄与 ``merge_derived`` 完全一致）。

    写回失败（定义既不是 dict 也没有可写属性）时结果里 ``applied=False``，
    但 ``discovery`` 仍然是算好的那一份——调用方可以自己决定怎么落。
    """

    merged = merge_derived(
        discovery_of(definition),
        payload,
        input_hash=input_hash,
        source_revision=source_revision,
        generator_version=generator_version,
        now=now,
        manual=manual,
    )
    applied = store_discovery(definition, merged.discovery)
    return DiscoveryWriteResult(
        discovery=merged.discovery,
        written=merged.written,
        skipped_locked=merged.skipped_locked,
        rejected=merged.rejected,
        conflicts=merged.conflicts,
        applied=applied,
    )


def store_discovery(definition: Any, discovery: ActionDiscovery) -> bool:
    """把一份**已经校验过**的派生信息写到定义上；写不进去回 ``False``（不抛）。

    ⚠️ 这是低级写入口，**不接受模型返回的裸 dict**：模型结果必须先过
    :func:`merge_derived` / :func:`apply_derived` 的白名单与人工锁。
    """

    if not isinstance(discovery, ActionDiscovery):
        return False
    if isinstance(definition, dict):
        try:
            definition["discovery"] = discovery.model_dump(mode="json")
            return True
        except Exception:
            return False
    if definition is None:
        return False
    try:
        setattr(definition, "discovery", discovery)
        return True
    except Exception:
        return False


def mark_pending(definition: Any, *, now: float = 0.0, source_revision: str = "") -> ActionDiscovery:
    """把状态标成「排队整理中」并写回定义。

    **不刷 ``input_hash``**：那个字段是"当前这份简述是从哪一版定义生成的"（出处）。
    刷成新指纹的话，视图就会把一份按旧定义写的简述当成新定义的——回退口径当场失效。
    需要知道"待整理的是哪一版"请用队列行里的指纹（A4）。
    """

    discovery = discovery_of(definition).model_copy(deep=True)
    discovery.status = DISCOVERY_STATUS_PENDING
    discovery.error_code = ""
    discovery.updated_at = _float_or(now)
    if source_revision:
        discovery.source_revision = str(source_revision)
    store_discovery(definition, discovery)
    return discovery


def mark_failed(
    definition: Any, *, error_code: str = "provider_error", now: float = 0.0
) -> ActionDiscovery:
    """把状态标成「整理失败」并写回定义（**不动已有的简述文字**）。

    保留旧文字是为了让视图能区分"有过一份、但已经过期"与"从来没有过"：前者在
    观测里要能看出"这条一直在失败"，后者是"还没排上队"。
    """

    discovery = discovery_of(definition).model_copy(deep=True)
    discovery.status = DISCOVERY_STATUS_FAILED
    discovery.error_code = str(error_code or "provider_error")
    discovery.updated_at = _float_or(now)
    store_discovery(definition, discovery)
    return discovery


def mark_stale(definition: Any, *, now: float = 0.0) -> ActionDiscovery:
    """把状态标成「定义变了、旧结果作废」并写回定义（迟到结果丢弃后调它）。"""

    discovery = discovery_of(definition).model_copy(deep=True)
    discovery.status = DISCOVERY_STATUS_STALE
    discovery.updated_at = _float_or(now)
    store_discovery(definition, discovery)
    return discovery


def set_lock(definition: Any, field_name: str = "", locked: bool = True) -> ActionDiscovery:
    """人工锁定：``field_name`` 空 = 整条锁定，给了就是按字段锁。写回定义并返回新值。

    解锁只动锁，**不动内容**——用户写的字归用户，后台要改只能等他解锁。
    """

    discovery = discovery_of(definition).model_copy(deep=True)
    name = str(field_name or "").strip()
    if not name:
        discovery.locked = bool(locked)
    elif name in LOCKABLE_FIELDS:
        current = [str(item) for item in (discovery.locked_fields or [])]
        if locked:
            if name not in current:
                current.append(name)
        else:
            current = [item for item in current if item != name]
        discovery.locked_fields = current
    store_discovery(definition, discovery)
    return discovery


def is_locked(definition: Any, field_name: str = "") -> bool:
    """整条锁着、或者这个字段锁着（``field_name`` 空 = 只问整条锁）。"""

    discovery = discovery_of(definition)
    if bool(getattr(discovery, "locked", False)):
        return True
    name = str(field_name or "").strip()
    if not name:
        return False
    return name in {str(item) for item in (getattr(discovery, "locked_fields", None) or [])}


def needs_refresh(definition: Any, *, generator_version: str = DISCOVERY_GENERATOR_VERSION) -> bool:
    """这条动作现在**需不需要**（重新）整理——A5 保存入口的判据。

    需要 = 没有可用简述 / 指纹对不上 / 整理器版本变了 / 上次失败或作废；
    **整条人工锁定的一律不需要**（不许后台去覆盖用户写的字）。
    """

    discovery = discovery_of(definition)
    if bool(getattr(discovery, "locked", False)):
        return False
    current = discovery_input_hash(definition)
    usable, _source, why = _summary_state(discovery, current)
    if not usable:
        return True
    if str(getattr(discovery, "generator_version", "") or "") != str(generator_version or ""):
        return True
    return why not in ("ok", "manual_lock")


__all__ = [
    "ALLOWED_WRITE_FIELDS",
    "DETAIL_POLICIES",
    "DETAIL_POLICY_ALWAYS",
    "DETAIL_POLICY_AUTO",
    "DETAIL_POLICY_ON_DEMAND",
    "DISCOVERY_GENERATOR_VERSION",
    "DISCOVERY_INPUT_FIELDS",
    "DISCOVERY_SOURCE_DESCRIPTION",
    "DISCOVERY_SOURCE_DISCOVERY",
    "DISCOVERY_SOURCE_NONE",
    "DISCOVERY_STATUSES",
    "DISCOVERY_STATUS_FAILED",
    "DISCOVERY_STATUS_MANUAL",
    "DISCOVERY_STATUS_MISSING",
    "DISCOVERY_STATUS_PENDING",
    "DISCOVERY_STATUS_READY",
    "DISCOVERY_STATUS_STALE",
    "DiscoveryView",
    "DiscoveryWriteResult",
    "FIELD_LABELS",
    "LOCKABLE_FIELDS",
    "MANAGED_FIELDS",
    "SAFE_FIELD_NAMES",
    "SENSITIVE_FIELD_MARKS",
    "SUMMARY_FALLBACK_MAX_CHARS",
    "UNSAFE_SUMMARY_MARKS",
    "apply_derived",
    "build_view",
    "build_views",
    "discovery_input_hash",
    "discovery_input_payload",
    "discovery_of",
    "discovery_status",
    "fallback_summary",
    "has_unsafe_marks",
    "is_locked",
    "is_sensitive_field",
    "mark_failed",
    "mark_pending",
    "mark_stale",
    "merge_derived",
    "needs_refresh",
    "pick_input_fields",
    "render_index_line",
    "set_lock",
    "store_discovery",
    "system_constraints",
]
