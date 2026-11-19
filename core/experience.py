"""阶段 C：谈资、曝光与续说去重（计划 §7 C1–C6 的纯逻辑层）。

为什么单独一个文件：计划 §3「当前模块落点与推荐新增模块」给阶段 C 的建议落点就是
``core/experience.py``（"经历、谈资、分享claim、曝光更新及查询契约"）。这里只做
**纯逻辑**，配套单测 ``tests/test_experience.py``：

- 不 import AstrBot、不碰 sqlite、不碰 WorldState、不写文件；
- 时间只从构造函数注入的 ``clock`` 拿（没有测试换不掉的 ``time.time()``），
  不取全局随机；
- 「需要 engine 传进来的东西」（候选、最近说过的话、来源可见性、评分分量、
  状态时间、share 行）全部是显式参数，模块自己**不读任何全局状态**；
- 谈资池 ``TopicPool`` 只管理素材，**从不发送**：曝光只来自已经登记的 ``shares``
  行（计划 §4.4）。所以"生成可选谈资"永远不等于"主动播报"（X10）。

落点与既有模块的分工（计划 §3.1 对照表，不另造同义结构）：

- 事件身份 ``normalize_event_id`` / 操作 key ``operation_id_for`` / ``Share`` 在
  ``core/source_events.py``，本模块直接复用，不重算；
- 「说过没有」问 ``core/db.py::shares_for_source(source_id, revision=...)``
  （调用方把结果作为 ``shares=`` 传进来），**绝不拿正文 hash 当身份**；
- 送达状态沿用 ``core/ports.py`` 的 ``SEND_*``：``accepted`` / ``delivered`` /
  ``legacy_observed`` 才算"说过"，``failed`` / ``unknown`` 不算（X04）。

续说去重（计划 §17 的最小可交付之一）落在 :class:`FollowupCoordinator`：
同一 ``(source_id, revision, session_id)`` 至多一个活跃 claim，**已完成**的 key
在多轮里不再重复续说；失败/取消按明确的租约释放后可重试。

本模块只新建文件：``topics`` / ``share_claims`` 两张表由主智能体接线时按
``Topic.to_row()`` / :meth:`ShareClaim.to_row` 的列名落地（见两份 ``to_row``）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .ports import SEND_ACCEPTED, SEND_DELIVERED
from .source_events import (
    SHARE_LEGACY_OBSERVED,
    VISIBILITY_LOCKED,
    VISIBILITY_NORMAL,
    VISIBILITY_PERSON,
    VISIBILITY_PRIVATE,
    Share,
    normalize_sensitivity,
    operation_id_for,
)

__all__ = [
    "ACT_ANSWER",
    "ACT_INTRODUCE",
    "ACT_OUTCOME",
    "ACT_PROGRESS",
    "ACT_REFERENCE",
    "CLAIM_ACTIVE",
    "CLAIM_DONE",
    "CLAIM_EXPIRED",
    "CLAIM_RELEASED",
    "CLAIM_STATUSES",
    "COMMUNICATIVE_ACTS",
    "DEFAULT_CLAIM_TTL",
    "DEFAULT_PROACTIVE_LIMIT",
    "DEFAULT_RECENCY_WINDOW",
    "DEFAULT_SCORE_WEIGHTS",
    "EXPERIENCE_SCHEMA_VERSION",
    "EXPOSURE_STATUSES",
    "MAX_PROACTIVE_PER_TURN",
    "PAGE_LIMIT_DEFAULT",
    "PAGE_LIMIT_MAX",
    "PROACTIVE_ACTS",
    "RESPONSIVE_ACTS",
    "SOURCE_KINDS",
    "SOURCE_KIND_ACTIVITY",
    "SOURCE_KIND_CHAT",
    "SOURCE_KIND_DREAM",
    "SOURCE_KIND_EVENT",
    "SOURCE_KIND_MANUAL",
    "SOURCE_KIND_MEDIA",
    "SOURCE_KIND_TOOL",
    "TOPIC_AVAILABLE",
    "TOPIC_DELETED",
    "TOPIC_DISABLED",
    "TOPIC_EXPIRED",
    "TOPIC_STATUSES",
    "VISIBILITY_LOCKED",
    "VISIBILITY_NORMAL",
    "VISIBILITY_PERSON",
    "VISIBILITY_PRIVATE",
    "ActionSignal",
    "Candidate",
    "CandidateScore",
    "ClaimResult",
    "ExperienceError",
    "ExposureLedger",
    "FollowupCoordinator",
    "ScoreComponents",
    "ShareClaim",
    "ShareDecision",
    "SourceSignal",
    "Topic",
    "TopicNotFound",
    "TopicPage",
    "TopicPool",
    "TopicVersionConflict",
    "VISIBILITY_LOCKED",
    "VISIBILITY_NORMAL",
    "VISIBILITY_ORDER",
    "VISIBILITY_PERSON",
    "VISIBILITY_PRIVATE",
    "VISIBILITY_SENSITIVITIES",
    "Verdict",
    "already_introduced",
    "can_merge_topics",
    "claim_id_for",
    "decide_share",
    "introduced_revisions",
    "is_empty_declaration",
    "is_exposure",
    "merge_visibility",
    "normalize_visibility",
    "score_candidate",
    "should_make_topic",
    "should_push_followup",
    "tighten_visibility",
    "topic_is_usable",
    "topic_visibility",
    "visibility_sensitivity",
    "visible_in_session",
]

EXPERIENCE_SCHEMA_VERSION = 1
"""谈资/claim 行的结构版本（表级版本见 ``schema_meta.schema_version``）。"""

# ---------------- 受控词表 ----------------

TOPIC_AVAILABLE = "available"
TOPIC_DISABLED = "disabled"
TOPIC_EXPIRED = "expired"
TOPIC_DELETED = "deleted"
TOPIC_STATUSES = frozenset({TOPIC_AVAILABLE, TOPIC_DISABLED, TOPIC_EXPIRED, TOPIC_DELETED})
"""计划 §4.3：``status=available/disabled/expired/deleted``。

「已发」不是全局状态——说过没有按 ``shares`` 分会话查（见 :class:`ExposureLedger`）。
"""

SOURCE_KIND_CHAT = "chat"
SOURCE_KIND_EVENT = "event"
SOURCE_KIND_ACTIVITY = "activity"
SOURCE_KIND_TOOL = "tool"
SOURCE_KIND_MEDIA = "media"
SOURCE_KIND_MANUAL = "manual"
SOURCE_KIND_DREAM = "dream"
"""梦（阶段F4）：单独一类素材——**不是事实**，页面与提示词都要看得见这个标记。"""

SOURCE_KINDS = frozenset(
    {
        SOURCE_KIND_CHAT,
        SOURCE_KIND_EVENT,
        SOURCE_KIND_ACTIVITY,
        SOURCE_KIND_TOOL,
        SOURCE_KIND_MEDIA,
        SOURCE_KIND_MANUAL,
        SOURCE_KIND_DREAM,
    }
)
"""来源种类：聊天 / 事件 / 活动 / 工具结果 / 媒体 / 手工素材（计划 §7 C1）。

``manual`` 表示用户手写，"不能冒充真实观察"（§4.3）——所以它是独立的一档，
不混进 ``chat``。
"""

ACT_INTRODUCE = "introduce"
ACT_PROGRESS = "progress"
ACT_OUTCOME = "outcome"
ACT_ANSWER = "answer"
ACT_REFERENCE = "reference"
COMMUNICATIVE_ACTS = frozenset(
    {ACT_INTRODUCE, ACT_PROGRESS, ACT_OUTCOME, ACT_ANSWER, ACT_REFERENCE}
)
"""计划 §4.4 的 ``communicative_act``：以什么交际用途说了这句。"""

PROACTIVE_ACTS = frozenset({ACT_INTRODUCE, ACT_PROGRESS, ACT_OUTCOME})
"""主动说的三类：防重只针对它们（§4.4）。"""

RESPONSIVE_ACTS = frozenset({ACT_ANSWER, ACT_REFERENCE})
"""应答类：用户问起才说，**不受**"同一 revision 已介绍"限制（X12）。"""

CLAIM_ACTIVE = "active"
CLAIM_RELEASED = "released"
CLAIM_DONE = "done"
CLAIM_EXPIRED = "expired"
CLAIM_STATUSES = frozenset({CLAIM_ACTIVE, CLAIM_RELEASED, CLAIM_DONE, CLAIM_EXPIRED})

DEFAULT_CLAIM_TTL = 120.0
"""一次续说 claim 的默认租约（秒）：进程崩了也不会永久占着 key。"""

PAGE_LIMIT_DEFAULT = 20
PAGE_LIMIT_MAX = 100
"""计划 §7 C6：分页上限 100。"""

DEFAULT_PROACTIVE_LIMIT = 1
MAX_PROACTIVE_PER_TURN = 3
"""默认一轮最多主动介绍 1 条；硬上限 3——"默认不因库存多就刷屏"（§7 C2）。"""

EXPOSURE_STATUSES = frozenset({SEND_ACCEPTED, SEND_DELIVERED, SHARE_LEGACY_OBSERVED})
"""算"说过"的送达状态：平台收下 / 拿到送达凭据 / 旧历史看得出。

``failed`` / ``unknown`` / ``queued`` / ``cancelled`` 都不算——不假称对方听过（X04）。
"""

DEFAULT_RECENCY_WINDOW = 86400.0
"""默认"时间新鲜度"窗口（秒）。"""

DEFAULT_SCORE_WEIGHTS: Mapping[str, float] = {
    "relevance": 1.0,
    "persona": 0.6,
    "freshness": 1.2,
    "interest": 0.7,
    "recency": 0.8,
    "cost": 0.9,
}
"""计划 §7 C2 的评分因素权重（对话关联/角色倾向/新信息/用户兴趣/时间/成本）。

工程初值，调用方可以整份覆盖（``weights=``）。
"""


# ---------------- 宽容取值（坏行不抛异常） ----------------


def _text(value: Any) -> str:
    """统一成去掉首尾空白的字符串（``None`` → 空串）。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8").strip()
        except UnicodeDecodeError:
            return ""
    return str(value).strip()


def _number(value: Any, default: float = 0.0) -> float:
    """转 float（坏值 / NaN / inf → default）。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_number(value: Any) -> float | None:
    """可缺的时间戳：``None`` / 认不出 / 非正数一律 ``None``（"不知道什么时候"）。"""

    if value is None:
        return None
    number = _number(value, 0.0)
    return number if number > 0 else None


def _whole(value: Any, default: int = 0) -> int:
    """转 int（``"1.0"`` 这种也吃得下）。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _ratio(value: Any) -> float:
    """把评分分量夹到 ``[0, 1]``（NaN / 负值 / 超界都收进来）。"""

    number = _number(value, 0.0)
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


def _cell(row: Any, key: str, default: Any = "") -> Any:
    """宽容地取一格：缺列（dict → KeyError，sqlite3.Row → IndexError）给默认值。"""

    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _json_object(value: Any) -> dict[str, Any]:
    """JSON 对象列 → dict；坏 JSON / 非对象一律空 dict，不抛。"""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return {str(key): item for key, item in data.items()} if isinstance(data, dict) else {}


def _json_list(value: Any) -> list[Any]:
    """JSON 数组列 → list；坏 JSON / 单值都不抛。"""

    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return [value] if value.strip() else []
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _text_tuple(value: Any) -> tuple[str, ...]:
    """任意形状 → 保序去重的字符串元组（``None`` / 空项丢掉）。"""

    result: list[str] = []
    for item in _json_list(value):
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return tuple(result)


# ---------------- 许可（可见性）声明：四档敏感度与"取最严"（清单 §2.2 / §2.3） ----------------
#
# 为什么要有一层"许可算法"而不是各处自己 if：谈资的可见性是**授权判定**，
# 一旦哪一处漏判或多判，漏判的后果是私密内容被讲给不该听的人。所以判定只有
# 一份实现（:func:`merge_visibility` / :func:`visible_in_session`），
# 别处一律调它们，包括"多来源取最严"这种跨来源的合并。

VISIBILITY_NORMAL = "normal"
"""档一：组内公开。本会话组里谁都能听到（含之后才进群的人）。"""

VISIBILITY_PERSON = "person"
"""档二：只关当事人。只有当事人所在的那个会话可以主动提，人物本身是 ``persons``。"""

VISIBILITY_PRIVATE = "private"
"""档三：私密。只有来源发生的那个会话（``origin_session_id``）能提。"""

VISIBILITY_LOCKED = "locked"
"""档四：锁定。**谁都不主动提**（用户点名问起仍可答，走应答用途）。

锁定与前三档的区别不是"范围更小"，而是"不再进入任何主动介绍"，所以它由
``lock`` 布尔表达——TTL 上也对应"永不过期"。
"""

VISIBILITY_SENSITIVITIES = (VISIBILITY_NORMAL, VISIBILITY_PERSON, VISIBILITY_PRIVATE)
"""能被"声明"的三档；``locked`` 单独用 ``lock`` 表示（见 :data:`VISIBILITY_LOCKED`）。"""

VISIBILITY_ORDER = {
    VISIBILITY_NORMAL: 0,
    VISIBILITY_PERSON: 1,
    VISIBILITY_PRIVATE: 2,
    "locked": 3,
}
"""越严越大（清单 §2 的"取最严"= 取最大）。未知档按 private 处理（见 calc），
所以这里不需要给未知值留位置。"""

VISIBILITY_ALIASES = {
    "公开": VISIBILITY_NORMAL,
    "组内": VISIBILITY_NORMAL,
    "组内公开": VISIBILITY_NORMAL,
    "普通": VISIBILITY_NORMAL,
    "public": VISIBILITY_NORMAL,
    "open": VISIBILITY_NORMAL,
    "本人": VISIBILITY_PERSON,
    "当事人": VISIBILITY_PERSON,
    "个人": VISIBILITY_PERSON,
    "人物": VISIBILITY_PERSON,
    "私密": VISIBILITY_PRIVATE,
    "私聊": VISIBILITY_PRIVATE,
    "隐私": VISIBILITY_PRIVATE,
    "秘密": VISIBILITY_PRIVATE,
    "self": VISIBILITY_PRIVATE,
    "私人": VISIBILITY_PRIVATE,
    "锁定": VISIBILITY_LOCKED,
    "禁止外提": VISIBILITY_LOCKED,
    "别说": VISIBILITY_LOCKED,
    "密封": VISIBILITY_LOCKED,
    "locked": VISIBILITY_LOCKED,
    "sealed": VISIBILITY_LOCKED,
}
"""中文/英文别名 → 四档。**含锁定档**——模型说"别说出去"要能表达成锁定。"""


def visibility_sensitivity(value: Any) -> str:
    """任意输入 → 四档之一（``normal`` / ``person`` / ``private`` / ``locked``）。

    两种用法：

    - 给一个**档位值**（模型给的字符串 / 配置里的值）→ 收成四档之一；
    - 给一条**声明字典** → 读它自己写的档位（``locked`` 优先，其次 ``sensitivity``，
      再退到老字段 ``private`` / ``audience``）。

    **认不出就是 ``private``**（清单口径：解析失败或未知值按 ``private``）。这条
    fail-closed 只往严的方向错：宁可少提一件谈资，也不能把私事讲出去。
    """

    if isinstance(value, Mapping):
        data = dict(value)
        if not data:
            return VISIBILITY_PRIVATE
        if data.get("locked") is True or data.get("lock") is True:
            return VISIBILITY_LOCKED
        raw = _text(data.get("sensitivity"))
        if raw:
            return normalize_sensitivity(raw)
        # 老声明只有 private/audience：按 private 理解（宁严勿宽），别当普通事。
        if data.get("private") is True or _text(data.get("audience")) in ("private", "self"):
            return VISIBILITY_PRIVATE
        return VISIBILITY_NORMAL
    return normalize_sensitivity(value)


def normalize_visibility(visibility: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compatibility view of the versioned policy; authorization stays centralized."""
    from .topic_policy import normalize_policy
    policy = normalize_policy(visibility)
    if policy["hard_private"]:
        policy["sensitivity"] = VISIBILITY_LOCKED
    policy.update({"scope": policy["scope_id"], "persons": policy["subject_ids"],
                   "sessions": policy["allowed_sessions"]["ids"],
                   "scopes": [policy["scope_id"]] if policy["scope_id"] else [],
                   "lock": policy["hard_private"], "locked": policy["hard_private"],
                   "private": policy["sensitivity"] == "private"})
    return policy


def _stricter_sensitivity(left: Any, right: Any) -> str:
    """两档取更严的那个（未知档在 :func:`visibility_sensitivity` 里已经归到 private）。"""

    a = visibility_sensitivity(left)
    b = visibility_sensitivity(right)
    return a if VISIBILITY_ORDER.get(a, 2) >= VISIBILITY_ORDER.get(b, 2) else b


def is_empty_declaration(value: Any) -> bool:
    """这算不算"一次可见性声明"——**没传 / 空 dict / 全 None 都不算**。

    T8 第一步的事故根因：``merge_visibility({}, real)`` 里的空 ``{}`` 会被
    ``merge_policies`` 规格化成一条 legacy_unknown 私密条款（scope/origin 都空），
    这条**已经带 policy_version、却没有身份**的子条款在 composite 回炉时又被
    ``normalize_policy`` 判成"损坏"，升级成 ``deny_all``；而合并只严不松，一旦落库
    就永久锁死（``authorize`` → "damaged or conflicting policy"）。所以"没说话"
    必须在**进场时**就认出来丢掉，不能让它有机会变成一句"谁都不许看"。

    显式 ``{"deny_all": True}`` **不是**空声明（它有真值），照旧锁死；坏声明
    （非空但不是 JSON 对象 / 非映射）也不是空声明，照旧交给 ``normalize_policy``
    按损坏处理（fail-closed）。
    """

    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return True
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return False
    if isinstance(value, Mapping):
        return not any(item is not None for item in value.values())
    return False


def merge_visibility(
    *declarations: Mapping[str, Any] | None,
    items: Iterable[Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Compatibility API, preserving every source as an AND clause.

    空声明（``None`` / ``{}`` / 全 ``None``）**不算一次声明**，不进合并（T8）：
    空声明与"显式声明 deny_all"在原始输入上是可区分的，只有在合并入口丢掉空声明，
    才能既修掉"空 dict 锁死"又不放宽真正的 deny_all。全空的输入返回 fail-closed
    的 legacy_unknown（非 deny_all、非粘性），由 ``authorize`` 继续拒绝。
    """
    from .topic_policy import merge_policies
    raw = list(items) if items is not None else list(declarations)
    batch = []
    for item in raw:
        if isinstance(item, (list, tuple)):
            batch.extend(item)
        else:
            batch.append(item)
    batch = [item for item in batch if not is_empty_declaration(item)]
    if not batch:
        return normalize_visibility(None)
    return normalize_visibility(merge_policies(*batch))


def tighten_visibility(*declarations: Mapping[str, Any] | None) -> dict[str, Any]:
    """All existing restrictions remain: session intersection, hard lock OR."""
    from .topic_policy import tighten_policy
    if not declarations:
        return normalize_visibility(None)
    result = declarations[0]
    for suggestion in declarations[1:]:
        result = tighten_policy(result, suggestion)
    return normalize_visibility(result)


def topic_visibility(visibility: Mapping[str, Any] | None) -> str:
    """主题级判定用的档位（``locked`` 也算一档，见 :data:`VISIBILITY_LOCKED`）。"""

    data = normalize_visibility(visibility)
    return _text(data.get("sensitivity")) or VISIBILITY_PRIVATE


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    """整数参数夹到 ``[low, high]``（坏值 → default，再夹）。"""

    number = _whole(value, default)
    return max(low, min(high, number))


_UNSET: Any = object()
"""``expires_at`` 的"没传"哨兵：``None`` 表示显式清掉有效期。"""


# ---------------- 判定结果 ----------------


@dataclass(frozen=True)
class Verdict:
    """一次"能不能"判定的结果：布尔 + 中文理由（理由给日志和 UI 用，不写废话）。"""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class ExperienceError(Exception):
    """谈资/曝光层的统一错误基类（API 层把它映射成 4xx）。"""


class TopicNotFound(ExperienceError):
    """谈资不存在，或**不在本次请求的 scope 内**（计划 §7 C6：ID 查询不越范围）。"""


class TopicVersionConflict(ExperienceError):
    """乐观锁冲突（计划 §7 C6：版本冲突 409）。"""

    def __init__(self, topic_id: str, *, expected: int, current: int) -> None:
        super().__init__(
            f"谈资 {topic_id} 已被改过：你手上是 v{expected}，现在是 v{current}"
        )
        self.topic_id = topic_id
        self.expected = expected
        self.current = current


# ---------------- 谈资（计划 §4.3 topics） ----------------


@dataclass(frozen=True)
class Topic:
    """一条可分享的谈资素材（不是待发送队列，计划 §3.1）。

    ``information_revision`` 只在**事实**变化时增长（走 :meth:`TopicPool.revise`
    这个纠错入口）；编辑 ``summary`` 只涨 ``version``，不改现实 ledger（§7 C6）。
    ``corrections`` 是纠错审计（谁在什么时候把哪一版改到哪一版），随
    ``details_json`` 一起落库——本模块只能新建文件，不能给既有表加列。
    """

    topic_id: str
    scope_id: str = ""
    episode_id: str = ""
    source_event_ids: tuple[str, ...] = ()
    information_revision: int = 1
    summary: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    source_kind: str = SOURCE_KIND_CHAT
    status: str = TOPIC_AVAILABLE
    pinned: bool = False
    expires_at: float | None = None
    importance: float = 0.0
    user_edited: bool = False
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    corrections: tuple[Mapping[str, Any], ...] = ()
    schema_version: int = EXPERIENCE_SCHEMA_VERSION

    def to_row(self) -> dict[str, Any]:
        """转成 ``topics`` 表的列字典（计划 §4.3 的列名逐一对齐）。

        ``details_json`` 里同时装 ``details`` 与 ``corrections``：接线时若把
        ``corrections`` 拆成独立列，读回来仍兼容（:meth:`from_row` 两种形状都认）。
        """

        return {
            "topic_id": _text(self.topic_id),
            "scope_id": _text(self.scope_id),
            "episode_id": _text(self.episode_id),
            "source_event_ids_json": json.dumps(
                list(_text_tuple(self.source_event_ids)), ensure_ascii=False
            ),
            "information_revision": _whole(self.information_revision, 1),
            "summary": _text(self.summary),
            "details_json": json.dumps(
                {
                    "details": dict(self.details or {}),
                    "corrections": [dict(item) for item in self.corrections or ()],
                },
                ensure_ascii=False,
                default=str,
            ),
            "source_kind": _text(self.source_kind) or SOURCE_KIND_CHAT,
            "status": _text(self.status) or TOPIC_AVAILABLE,
            "pinned": 1 if self.pinned else 0,
            "expires_at": (
                None if self.expires_at is None else _number(self.expires_at, 0.0)
            ),
            "importance": _number(self.importance, 0.0),
            "user_edited": 1 if self.user_edited else 0,
            "version": _whole(self.version, 1),
            "created_at": _number(self.created_at, 0.0),
            "updated_at": _number(self.updated_at, 0.0),
            "schema_version": _whole(self.schema_version, EXPERIENCE_SCHEMA_VERSION),
        }

    @classmethod
    def from_row(cls, row: Any) -> "Topic":
        """从一行（dict / sqlite3.Row）还原；缺列、坏 JSON 都降级，不抛异常。"""

        raw = _cell(row, "details_json", _cell(row, "details", {}))
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "replace")
        payload = _json_object(raw)
        if isinstance(payload.get("details"), Mapping) or isinstance(
            payload.get("corrections"), list
        ):
            details = _json_object(payload.get("details", {}))
            corrections = payload.get("corrections", [])
        else:
            # 兼容：整份 details_json 就是 details（外部/旧形状）
            details = payload
            corrections = []
        return cls(
            topic_id=_text(_cell(row, "topic_id")),
            scope_id=_text(_cell(row, "scope_id")),
            episode_id=_text(_cell(row, "episode_id")),
            source_event_ids=_text_tuple(_cell(row, "source_event_ids_json", [])),
            information_revision=_whole(_cell(row, "information_revision"), 1),
            summary=_text(_cell(row, "summary")),
            details=details,
            source_kind=_text(_cell(row, "source_kind")) or SOURCE_KIND_CHAT,
            status=_text(_cell(row, "status")) or TOPIC_AVAILABLE,
            pinned=bool(_whole(_cell(row, "pinned"), 0)),
            expires_at=_optional_number(_cell(row, "expires_at", None)),
            importance=_number(_cell(row, "importance"), 0.0),
            user_edited=bool(_whole(_cell(row, "user_edited"), 0)),
            version=_whole(_cell(row, "version"), 1),
            created_at=_number(_cell(row, "created_at"), 0.0),
            updated_at=_number(_cell(row, "updated_at"), 0.0),
            corrections=tuple(
                dict(item) for item in corrections if isinstance(item, Mapping)
            ),
            schema_version=_whole(_cell(row, "schema_version"), EXPERIENCE_SCHEMA_VERSION),
        )


def topic_is_usable(topic: Topic, *, now: float) -> Verdict:
    """这条谈资现在能不能**主动**用（计划 §7 C2「取少量相关、许可…的素材」）。

    - ``available`` 且未过期 → 可以；
    - ``pinned`` 的谈资不做自然过期判定（用户钉住的东西不因时间失效）；
    - ``disabled`` / ``expired`` / ``deleted`` → 不主动用（``deleted`` 是"阻止未来使用"，
      源聊天/事件/发出的消息都不受它影响）。
    """

    status = _text(topic.status) or TOPIC_AVAILABLE
    if status == TOPIC_DELETED:
        return Verdict(False, "谈资已删除：不再主动使用（源事件与已发消息不受影响）")
    if status == TOPIC_DISABLED:
        return Verdict(False, "谈资被禁用：不主动介绍")
    if status == TOPIC_EXPIRED:
        return Verdict(False, "谈资已过期：不主动介绍")
    if topic.expires_at is not None and not topic.pinned:
        if _number(topic.expires_at, 0.0) <= _number(now, 0.0):
            return Verdict(False, "谈资已到有效期：不再主动介绍")
    if not _text(topic.summary) and not topic.details:
        return Verdict(False, "谈资没有可说的内容：不主动介绍")
    return Verdict(True, "可用且未说过期")


# ---------------- 素材生成信号（计划 §7 C1，X10） ----------------


@dataclass(frozen=True)
class SourceSignal:
    """调用方对一条来源事件**声明**的语义信号。

    刻意不接受正文：计划 §7 C1/C4 要求不靠固定关键词（"茶/晚霞/食物"）判断重要性，
    所以判断输入只有结构化的声明字段——"重要性"由理解层给出，本层不做词表。
    """

    source_kind: str = SOURCE_KIND_CHAT
    new_information: bool = False
    facts: tuple[str, ...] = ()
    importance: float = 0.0
    is_greeting: bool = False
    is_duplicate_reply: bool = False
    is_debug_log: bool = False
    is_system_notice: bool = False
    visibility: Mapping[str, Any] = field(default_factory=dict)


def should_make_topic(signal: SourceSignal) -> Verdict:
    """这条来源要不要生成谈资（计划 §7 C1）。

    - 调试日志 / 系统通知：不生成（不是"她经历的事"）；
    - 纯寒暄、重复回复、没有新内容：不生成；
    - 有新事实 / 新信息 / 明确重要性：生成——**手工谈资**走 ``pool.manual()``，
      不靠这条信号。
    """

    if signal.is_debug_log:
        return Verdict(False, "调试日志不作谈资")
    if signal.is_system_notice:
        return Verdict(False, "系统通知不作谈资")
    facts = _text_tuple(signal.facts)
    has_content = bool(facts) or bool(signal.new_information) or _number(
        signal.importance, 0.0
    ) > 0
    if not has_content:
        if signal.is_greeting:
            return Verdict(False, "纯寒暄没有新信息：不建谈资")
        if signal.is_duplicate_reply:
            return Verdict(False, "重复回复没有新信息：不建谈资")
        return Verdict(False, "没有新内容：不建谈资")
    return Verdict(True, "有新事实或明确重要性：可以成为可选谈资（可用不等于必发）")


def can_merge_topics(
    existing: Topic, *, source_event_ids: Iterable[Any] = (), facts: Iterable[Any] = ()
) -> Verdict:
    """两条素材能不能合成同一条谈资（计划 §7 C1）。

    规则只有两条，都是**可判定**的：有共同的来源事件，或者要加的事实已经在里面。
    主题/摘要看着像**不算**理由——计划明确要求"不要仅按主题 hash 合并为同事件，
    必须检查来源与事实"。所以这里根本不做正文比较。
    """

    known = set(_text_tuple(existing.source_event_ids))
    incoming = set(_text_tuple(source_event_ids))
    if known & incoming:
        return Verdict(True, "有共同的来源事件：同一段经历")
    existing_facts = set(_text_tuple(existing.details.get("facts", ())))
    new_facts = set(_text_tuple(facts))
    if new_facts and new_facts <= existing_facts:
        return Verdict(True, "这些事实已经记在同一条谈资里")
    return Verdict(False, "没有共同来源、也没有已记录的事实：另建一条（不按主题合并）")


# ---------------- 可见性（计划 §5 A1 visibility + §7 C2 许可） ----------------
# 判定只留一份（上面的 VISIBILITY_* / normalize_visibility / merge_visibility /
# visible_in_session）——**别再在下面重复定义一遍**：后定义的会静默盖掉前面的，
# 于是"我改的那一处"根本没生效，而漏判的代价是私事被讲出去。


def visible_in_session(
    visibility: Mapping[str, Any] | None,
    *,
    session_id: str,
    scope_id: str = "",
    person_id: str = "",
) -> Verdict:
    """Legacy proactive helper. Never grants private/unknown access.

    person_id alone cannot prove all subjects are present in another session.
    Use topic_policy.authorize with a verified PolicyTarget for that case.
    """
    from .topic_policy import normalize_policy, PolicyTarget, _check
    decision = _check(normalize_policy(visibility),
                      PolicyTarget(scope_id=scope_id, session_id=session_id),
                      "introduce", False)
    return Verdict(decision.allowed, decision.reason)


# ---------------- 曝光：说过没有（计划 §4.4 + §7 C2，X12） ----------------


def is_exposure(share: Any) -> bool:
    """这条 share 算不算"真的说过了"（X04：失败/无法确认不算）。"""

    status = _text(_cell(share, "status", ""))
    if isinstance(share, Share):
        status = share.status
    return status in EXPOSURE_STATUSES


def _share_of(row: Any) -> Share | None:
    """任意 share 行（dict / sqlite3.Row / :class:`Share`）→ :class:`Share`。"""

    if isinstance(row, Share):
        return row
    if row is None:
        return None
    return Share.from_row(row)


def introduced_revisions(shares: Iterable[Any], *, session_id: str) -> tuple[int, ...]:
    """这个会话里，这个来源**真的说过**的 revision 有哪些（升序去重）。

    ``shares`` 由调用方给：engine 接 ``db.shares_for_source(conn, source_id)``
    （不给 ``revision`` 就是全部版本，这正是判断"有没有更新的一版"需要的）。
    """

    target = _text(session_id)
    if not target:
        return ()
    found: set[int] = set()
    for raw in shares or ():
        share = _share_of(raw)
        if share is None or not is_exposure(share):
            continue
        if _text(share.session_id) != target:
            continue
        found.add(_whole(share.information_revision, 0))
    return tuple(sorted(found))


def already_introduced(
    shares: Iterable[Any], *, session_id: str, revision: int
) -> bool:
    """这一版信息在这个会话已经主动介绍过没有。"""

    return _whole(revision, 0) in introduced_revisions(shares, session_id=session_id)


@dataclass(frozen=True)
class ShareDecision:
    """这次要不要说、以什么用途说（计划 §7 C2 的可判定结论）。"""

    allowed: bool = False
    act: str = ""
    revision: int = 0
    topic_id: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def decide_share(
    topic: Topic,
    *,
    session_id: str,
    shares: Iterable[Any] = (),
    requested_act: str = "",
    now: float = 0.0,
) -> ShareDecision:
    """针对当前会话决定这条谈资说不说、以什么交际用途说（计划 §7 C2）。

    规则（都是可判定的，不猜正文）：

    1. 主动用途（``introduce`` / ``progress`` / ``outcome``）要求谈资可用
       （:func:`topic_is_usable`）；
    2. 这个会话**没说过**这个来源 → ``introduce``；
    3. 说过，但手上有**更高**的 ``information_revision`` → ``progress``
       （新事实可以继续讲）；
    4. 说过同一版 → 跳过。**换个句式不算新信息**——所以判断只看 revision，
       不看正文 hash；
    5. 应答用途（``answer`` / ``reference``）不受 2–4 限制：用户问起可以答、
       可以短 recap（"群里发过不认为所有成员已看"）；但 ``deleted`` 的谈资不答
       （软删除阻止未来使用，源事件本身仍可被 engine 直接引用）。
    """

    act = _text(requested_act)
    if act and act not in COMMUNICATIVE_ACTS:
        return ShareDecision(
            False, "", topic.information_revision, topic.topic_id, f"未知交际用途：{act}"
        )
    if _text(topic.status) == TOPIC_DELETED:
        return ShareDecision(
            False, "", topic.information_revision, topic.topic_id, "谈资已删除：不再使用"
        )
    target_session = _text(session_id)
    if not target_session:
        return ShareDecision(
            False, "", topic.information_revision, topic.topic_id, "没有会话：不知道说给谁听"
        )
    say_in = introduced_revisions(shares, session_id=target_session)
    want = act or _default_act(say_in, _whole(topic.information_revision, 1))
    if not want:
        if say_in:
            return ShareDecision(
                False,
                "",
                topic.information_revision,
                topic.topic_id,
                "这个会话已经介绍过这个来源，也没有新的事实版本：不重复主动介绍",
            )
        return ShareDecision(
            False, "", topic.information_revision, topic.topic_id, "没有可主动说的用途：不主动说"
        )
    if want == ACT_INTRODUCE and say_in:
        return ShareDecision(
            False,
            "",
            topic.information_revision,
            topic.topic_id,
            "这个会话已经介绍过这个来源：不重复主动介绍",
        )
    if want == ACT_PROGRESS:
        newest = max(say_in) if say_in else 0
        if _whole(topic.information_revision, 1) <= newest:
            return ShareDecision(
                False,
                "",
                topic.information_revision,
                topic.topic_id,
                "没有新事实版本：换句式不算新信息",
            )
    if want == ACT_OUTCOME and say_in and _whole(topic.information_revision, 1) <= max(say_in):
        return ShareDecision(
            False,
            "",
            topic.information_revision,
            topic.topic_id,
            "这个会话已经说过这段结果：不重复续说",
        )
    if want in PROACTIVE_ACTS:
        usable = topic_is_usable(topic, now=now)
        if not usable:
            return ShareDecision(
                False, "", topic.information_revision, topic.topic_id, usable.reason
            )
    return ShareDecision(
        bool(want),
        want,
        topic.information_revision,
        topic.topic_id,
        "可以介绍" if want == ACT_INTRODUCE else f"可以{want}",
    )


def _default_act(said: Sequence[int], revision: int) -> str:
    """没指定用途时的默认选择：第一次说就介绍，有新版本就 progress，否则不主动说。"""

    if not said:
        return ACT_INTRODUCE
    if _whole(revision, 1) > max(said):
        return ACT_PROGRESS
    return ""


def claim_id_for(*, source_id: Any, revision: Any, session_id: Any) -> str:
    """一个续说 key 的稳定 claim id：``(source_id, revision, session_id)`` 的 hash。

    计划 §4.4 的唯一约束是**同一主动介绍 key 至多一个活跃 claim**，所以行身份就是
    key 本身：一行一个 key，状态（active/done/released/expired）在行内流转。
    ``intent_class`` 是意图、不是 key——这正是不让"动作续说"和"谈资"抢同一个 key
    的原因（X13）。
    """

    return operation_id_for(
        kind="share_claim",
        session_id=_text(session_id),
        action_id=_text(source_id),
        anchor=str(_whole(revision, 0)),
    )


@dataclass(frozen=True)
class ShareClaim:
    """一次"我来说这件事"的占用（计划 §4.4 ``share_claims``）。

    ``lease_until`` 两用：``active`` 时是租约到期时间；``released`` 时是最小重试
    间隔的门槛（复用同一列，不新增字段）。
    """

    claim_id: str
    source_id: str = ""
    revision: int = 0
    session_id: str = ""
    intent_class: str = ""
    owner_operation_id: str = ""
    lease_until: float = 0.0
    status: str = CLAIM_ACTIVE

    def to_row(self) -> dict[str, Any]:
        """转成 ``share_claims`` 表的列字典（计划 §4.4 的列名）。"""

        return {
            "claim_id": _text(self.claim_id),
            "source_id": _text(self.source_id),
            "revision": _whole(self.revision, 0),
            "session_id": _text(self.session_id),
            "intent_class": _text(self.intent_class),
            "owner_operation_id": _text(self.owner_operation_id),
            "lease_until": _number(self.lease_until, 0.0),
            "status": _text(self.status) or CLAIM_ACTIVE,
        }

    @classmethod
    def from_row(cls, row: Any) -> "ShareClaim":
        """从一行还原（缺列、坏值都降级）。"""

        return cls(
            claim_id=_text(_cell(row, "claim_id")),
            source_id=_text(_cell(row, "source_id")),
            revision=_whole(_cell(row, "revision"), 0),
            session_id=_text(_cell(row, "session_id")),
            intent_class=_text(_cell(row, "intent_class")),
            owner_operation_id=_text(_cell(row, "owner_operation_id")),
            lease_until=_number(_cell(row, "lease_until"), 0.0),
            status=_text(_cell(row, "status")) or CLAIM_ACTIVE,
        )

    def is_live(self, *, now: float) -> bool:
        """活跃且租约未过期。"""

        return self.status == CLAIM_ACTIVE and _number(self.lease_until, 0.0) > _number(
            now, 0.0
        )


@dataclass(frozen=True)
class ClaimResult:
    """一次 claim 请求的结果（``granted=False`` 时 ``reason`` 说清被谁挡了）。"""

    granted: bool = False
    claim: ShareClaim | None = None
    existing: ShareClaim | None = None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.granted


@dataclass(frozen=True)
class ActionSignal:
    """动作/事件完成时调用方声明的信号（决定要不要**立刻**续说，计划 §7 C3）。

    同样不接受正文：要不要续说由"完成了没有 / 开没开 llm_followup / 是不是普通小事件 /
    用户有没有明确问起"决定，不靠关键词。
    """

    completed: bool = True
    followup_enabled: bool = False
    small_event: bool = False
    user_requested: bool = False
    event_kind: str = "action"


def should_push_followup(signal: ActionSignal) -> Verdict:
    """动作完成后要不要马上续说一句（计划 §7 C1「普通小事件不强制主动播报」+ C3）。

    顺序有意义：先"结果立好了没有"，再"这是不是值得播报的东西"，最后才看配置。
    """

    if not signal.completed:
        return Verdict(False, "动作还没完成：先建 episode result/source 再谈续说")
    if _text(signal.event_kind) in ("debug", "system", "notice"):
        return Verdict(False, "调试/系统事件不主动播报")
    if signal.user_requested:
        return Verdict(True, "用户明确问起：以 answer/outcome 续说")
    if signal.small_event:
        return Verdict(False, "普通小事件不强制主动播报（仍可成为可选谈资）")
    if not signal.followup_enabled:
        return Verdict(False, "没有开启动作续说（llm_followup）：不立即续说")
    return Verdict(True, "开启续说且有新结果：可以立刻续说一次")


# ---------------- 评分（计划 §7 C2 的软因素） ----------------


@dataclass(frozen=True)
class ScoreComponents:
    """软因素分量，全部是 ``[0, 1]``（由调用方算，本层不猜）。

    ``relevance`` 对话关联、``persona`` 角色倾向、``freshness`` 新信息、
    ``interest`` 用户兴趣、``recency`` 时间、``cost`` 成本（减项）。
    """

    relevance: float = 0.0
    persona: float = 0.0
    freshness: float = 0.0
    interest: float = 0.0
    recency: float = 0.0
    cost: float = 0.0


@dataclass(frozen=True)
class CandidateScore:
    """评分结果：总分 + 每个分量的贡献（日志里逐项显示，不让模型自己捏概率）。"""

    total: float = 0.0
    components: ScoreComponents = field(default_factory=ScoreComponents)
    parts: tuple[tuple[str, float, float, float], ...] = ()
    """``(分量名, 夹紧后的分量, 权重, 贡献)`` 四元组，顺序固定，便于对日志。"""

    def explain(self) -> str:
        """中文一行说明，例如"总分 1.83（freshness 1.00×1.2=1.20 …）"。"""

        if not self.parts:
            return f"总分 {self.total:.2f}（没有分量）"
        body = "，".join(
            f"{name} {value:.2f}×{weight:.2f}={gift:+.2f}"
            for name, value, weight, gift in self.parts
        )
        return f"总分 {self.total:.2f}（{body}）"


def score_candidate(
    components: ScoreComponents, *, weights: Mapping[str, float] | None = None
) -> CandidateScore:
    """按权重把分量合成一个总分（计划 §7 C2："参与评分的因素包含…"）。

    成本是**减项**；其余都是加分项。分量先夹到 ``[0, 1]``，坏值不会把分数带飞。
    """

    table = dict(DEFAULT_SCORE_WEIGHTS if weights is None else weights)
    parts: list[tuple[str, float, float, float]] = []
    total = 0.0
    for name in ("relevance", "persona", "freshness", "interest", "recency"):
        value = _ratio(getattr(components, name, 0.0))
        weight = _number(table.get(name, DEFAULT_SCORE_WEIGHTS.get(name, 0.0)), 0.0)
        gift = value * weight
        total += gift
        parts.append((name, value, weight, gift))
    cost = _ratio(components.cost)
    cost_weight = _number(table.get("cost", DEFAULT_SCORE_WEIGHTS["cost"]), 0.0)
    gift = -(cost * cost_weight)
    total += gift
    parts.append(("cost", cost, cost_weight, gift))
    return CandidateScore(total=total, components=components, parts=tuple(parts))


def _apply_score_bonus(score: CandidateScore, value: Any, weight: float) -> CandidateScore:
    """给总分加一项"外部相似度"加分（阶段B1/WP3 的语义分钩子，纯加法）。

    只改**排序**，不改授权：调用方（引擎）传进来之前已经逐条过了
    :func:`core.topic_policy.authorize`。分量夹到 ``[0, 1]`` 再乘权重，贡献记进
    ``parts`` 末尾（分量名 ``semantic``），日志里看得见。权重为 0 时调用方根本不会
    走到这儿——那正是"与 A 逐字节相同"的那条分支。
    """

    scale = _number(weight, 0.0)
    if not scale:
        return score
    ratio = _ratio(value)
    gift = ratio * scale
    return CandidateScore(total=score.total + gift, components=score.components,
                          parts=tuple(score.parts) + (("semantic", ratio, scale, gift),))


@dataclass(frozen=True)
class Candidate:
    """一条主动介绍候选：谈资 + 判定 + 评分（给 engine 排序/日志用）。"""

    topic: Topic
    decision: ShareDecision
    score: CandidateScore

    def explain(self) -> str:
        return f"{self.topic.topic_id}：{self.decision.reason}；{self.score.explain()}"


# ---------------- 谈资池（计划 §4.3 + §7 C1/C2/C6） ----------------


class TopicPool:
    """谈资池：候选生成、CRUD、查询、主动候选选取。

    只做纯逻辑 + 内存索引；**不发送、不落库、不读全局状态**。需要 engine 传进来的
    东西都是显式参数：

    - ``clock``：唯一的时间来源（``() -> float``），没有全局 ``time.time()``；
    - ``shares_for``：``(source_id) -> share 行序列``——engine 接
      ``db.shares_for_source(conn, source_id)``（X12 的"这一版说过没有"）；
    - ``components_for``：``(topic) -> ScoreComponents``——角色倾向/用户兴趣这些
      需要人设与会话上下文的因素由 engine 算好传进来；
    - ``visibility_for``：``(topic) -> Mapping``——来源事件的可见性
      （``source_events.visibility_json``）。

    持久化：``snapshot()`` / ``restore(rows)`` 用 :meth:`Topic.to_row` /
    :meth:`Topic.from_row` 与外部交换，本模块不碰文件与 sqlite。
    """

    def __init__(
        self,
        clock: Callable[[], float],
        *,
        id_factory: Callable[[], str] | None = None,
        recency_window: float = DEFAULT_RECENCY_WINDOW,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory
        self._recency_window = _number(recency_window, DEFAULT_RECENCY_WINDOW)
        self._topics: dict[str, Topic] = {}
        self._counter = 0

    # -- 内部 --------------------------------------------------------------

    def _now(self, now: float | None) -> float:
        return _number(self._clock() if now is None else now, 0.0)

    def _next_id(self) -> str:
        if self._id_factory is not None:
            generated = _text(self._id_factory())
            if generated:
                return generated
        self._counter += 1
        # 不用随机数：id 由（时钟毫秒 + 自增序号）决定，测试里完全可复现。
        return f"topic-{int(self._now(None) * 1000)}-{self._counter}"

    def _find_in_scope(self, topic_id: str, scope_id: str) -> Topic | None:
        topic = self._topics.get(_text(topic_id))
        if topic is None:
            return None
        target = _text(scope_id)
        if target and _text(topic.scope_id) != target:
            return None  # 越范围：当作不存在，不暴露别人 scope 的 ID（§7 C6）
        return topic

    def _require(self, topic_id: str, scope_id: str) -> Topic:
        topic = self._find_in_scope(topic_id, scope_id)
        if topic is None:
            raise TopicNotFound(f"找不到谈资 {_text(topic_id) or '(空)'}")
        return topic

    @staticmethod
    def _check_version(topic: Topic, expected_version: Any) -> None:
        if expected_version is None:
            return
        expected = _whole(expected_version, topic.version)
        if expected != topic.version:
            raise TopicVersionConflict(topic.topic_id, expected=expected, current=topic.version)

    # -- 建 ----------------------------------------------------------------

    def create(
        self,
        *,
        scope_id: str,
        summary: str = "",
        source_kind: str = SOURCE_KIND_CHAT,
        episode_id: str = "",
        source_event_ids: Iterable[Any] = (),
        details: Mapping[str, Any] | None = None,
        information_revision: int = 1,
        importance: float = 0.0,
        expires_at: float | None = None,
        status: str = TOPIC_AVAILABLE,
        pinned: bool = False,
        topic_id: str = "",
        user_edited: bool = False,
    ) -> Topic:
        """建一条谈资；**同一个来源事件已经有谈资时返回既有那条**，不新建。

        这条复用规则与 :func:`can_merge_topics` 一致：按来源事件去重，绝不按摘要
        相似度合并。已删除（软删除）的不算可复用——"阻止未来使用"。
        """

        scope = _text(scope_id)
        if not scope:
            raise ExperienceError("谈资必须属于一个 scope：没有 scope 不落库")
        events = _text_tuple(source_event_ids)
        for existing in self._topics.values():
            if _text(existing.scope_id) != scope:
                continue
            if _text(existing.status) == TOPIC_DELETED:
                continue
            if events and set(events) & set(existing.source_event_ids):
                return existing
        stamp = self._now(None)
        topic = Topic(
            topic_id=_text(topic_id) or self._next_id(),
            scope_id=scope,
            episode_id=_text(episode_id),
            source_event_ids=events,
            information_revision=max(1, _whole(information_revision, 1)),
            summary=_text(summary),
            details=dict(details or {}),
            source_kind=_text(source_kind) or SOURCE_KIND_CHAT,
            status=_text(status) or TOPIC_AVAILABLE,
            pinned=bool(pinned),
            expires_at=expires_at,
            importance=_number(importance, 0.0),
            user_edited=bool(user_edited),
            version=1,
            created_at=stamp,
            updated_at=stamp,
        )
        self._topics[topic.topic_id] = topic
        return topic

    def create_from_signal(
        self,
        signal: SourceSignal,
        *,
        scope_id: str,
        summary: str = "",
        episode_id: str = "",
        source_event_ids: Iterable[Any] = (),
        details: Mapping[str, Any] | None = None,
        expires_at: float | None = None,
        importance: float | None = None,
    ) -> Topic | None:
        """按 :func:`should_make_topic` 决定要不要建；不建返回 ``None``。

        ``None`` 是正常结果，不是错误：计划 §7 C1"没有新内容不必产生"。
        """

        verdict = should_make_topic(signal)
        if not verdict:
            return None
        merged = dict(details or {})
        facts = _text_tuple(signal.facts)
        if facts:
            merged.setdefault("facts", list(facts))
        return self.create(
            scope_id=scope_id,
            summary=summary,
            source_kind=signal.source_kind,
            episode_id=episode_id,
            source_event_ids=source_event_ids,
            details=merged,
            importance=_number(signal.importance, 0.0) if importance is None else importance,
            expires_at=expires_at,
        )

    def manual(
        self,
        *,
        scope_id: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
        importance: float = 0.0,
        expires_at: float | None = None,
        pinned: bool = False,
    ) -> Topic:
        """手工谈资（计划 §7 C1"手工谈资允许"）。

        标 ``source_kind=manual`` 且 ``user_edited=True``：它明确是用户/她自己编的素材，
        不能冒充真实观察（§4.3）。
        """

        return self.create(
            scope_id=scope_id,
            summary=summary,
            source_kind=SOURCE_KIND_MANUAL,
            details=details,
            importance=importance,
            expires_at=expires_at,
            pinned=pinned,
            user_edited=True,
        )

    # -- 查 / 改 -----------------------------------------------------------

    def get(self, topic_id: str, *, scope_id: str = "") -> Topic | None:
        """按 id 取（给了 ``scope_id`` 就越范围返回 ``None``，不泄露存在性）。"""

        return self._find_in_scope(topic_id, scope_id)

    def require(self, topic_id: str, *, scope_id: str = "") -> Topic:
        """同 :meth:`get`，取不到直接抛 :class:`TopicNotFound`。"""

        return self._require(topic_id, scope_id)

    def count(self, *, scope_id: str = "", include_deleted: bool = False) -> int:
        """条数（默认不数软删除的）。"""

        target = _text(scope_id)
        total = 0
        for topic in self._topics.values():
            if target and _text(topic.scope_id) != target:
                continue
            if not include_deleted and _text(topic.status) == TOPIC_DELETED:
                continue
            total += 1
        return total

    def edit(
        self,
        topic_id: str,
        *,
        scope_id: str = "",
        expected_version: Any = None,
        summary: Any = None,
        details: Mapping[str, Any] | None = None,
        status: Any = None,
        pinned: Any = None,
        importance: Any = None,
        expires_at: Any = _UNSET,
    ) -> Topic:
        """改**表达**（摘要/细节/状态/固定/有效期）：只涨 ``version``。

        计划 §7 C6："修改事实只能走明确的纠错入口…不因编辑 summary 改现实 ledger"。
        所以这里**绝不**动 ``information_revision``——改事实请用 :meth:`revise`。
        """

        current = self._require(topic_id, scope_id)
        self._check_version(current, expected_version)
        changes: dict[str, Any] = {"version": current.version + 1, "updated_at": self._now(None)}
        if summary is not None:
            changes["summary"] = _text(summary)
            changes["user_edited"] = True
        if details is not None:
            changes["details"] = {**dict(current.details), **dict(details)}
            changes["user_edited"] = True
        if status is not None:
            new_status = _text(status)
            if new_status not in TOPIC_STATUSES:
                raise ExperienceError(f"未知谈资状态：{new_status}")
            changes["status"] = new_status
        if pinned is not None:
            changes["pinned"] = bool(pinned)
        if importance is not None:
            changes["importance"] = _number(importance, current.importance)
        if expires_at is not _UNSET:
            changes["expires_at"] = _optional_number(expires_at)
        updated = replace(current, **changes)
        self._topics[updated.topic_id] = updated
        return updated

    def revise(
        self,
        topic_id: str,
        *,
        scope_id: str = "",
        expected_version: Any = None,
        facts: Iterable[Any] = (),
        reason: str = "",
        information_revision: Any = None,
    ) -> Topic:
        """**纠错入口**：改事实 → ``information_revision`` +1，并留审计（计划 §7 C6）。

        这是唯一会让"新事实可以 progress"生效的地方：只有走过这里，B 会话才会从
        "这一版讲过了"变成"有新的一版可以继续讲"。
        """

        current = self._require(topic_id, scope_id)
        self._check_version(current, expected_version)
        stamp = self._now(None)
        old_revision = current.information_revision
        new_revision = (
            max(old_revision + 1, _whole(information_revision, old_revision + 1))
            if information_revision is not None
            else old_revision + 1
        )
        merged = dict(current.details)
        new_facts = _text_tuple(facts)
        if new_facts:
            merged["facts"] = _text_tuple(
                _text_tuple(merged.get("facts", ())) + new_facts
            )
        entry: dict[str, Any] = {
            "at": stamp,
            "reason": _text(reason),
            "from_revision": old_revision,
            "to_revision": new_revision,
            "facts": list(new_facts),
        }
        updated = replace(
            current,
            information_revision=new_revision,
            details=merged,
            corrections=tuple(current.corrections) + (entry,),
            version=current.version + 1,
            user_edited=True,
            updated_at=stamp,
        )
        self._topics[updated.topic_id] = updated
        return updated

    def set_status(
        self, topic_id: str, status: str, *, scope_id: str = "", expected_version: Any = None
    ) -> Topic:
        """启用/禁用/软删除（计划 §7 C6 的"禁用/启用"操作）。"""

        return self.edit(
            topic_id, scope_id=scope_id, expected_version=expected_version, status=status
        )

    def delete(
        self, topic_id: str, *, scope_id: str = "", expected_version: Any = None, soft: bool = True
    ) -> Topic | None:
        """删除谈谈资；默认**软删除**（只阻止未来使用）。

        计划 §7 C6：删除不删源聊天/事件/已发出的消息，也不改现实 ledger。
        ``soft=False`` 才真的从池子里移除（源数据同样不动）。
        """

        current = self._require(topic_id, scope_id)
        self._check_version(current, expected_version)
        if soft:
            return self.set_status(current.topic_id, TOPIC_DELETED)
        return self._topics.pop(current.topic_id, None)

    def restore(self, topic_id: str, *, scope_id: str = "", expected_version: Any = None) -> Topic:
        """恢复软删除的谈资（计划 §7 C6"恢复可选"）。"""

        return self.set_status(
            topic_id, TOPIC_AVAILABLE, scope_id=scope_id, expected_version=expected_version
        )

    def pin(self, topic_id: str, *, scope_id: str = "", pinned: bool = True) -> Topic:
        """固定/取消固定。固定只影响"不自然过期"，不强制发送。"""

        return self.edit(topic_id, scope_id=scope_id, pinned=pinned)

    def expire_due(self, *, now: float | None = None) -> tuple[Topic, ...]:
        """到期的谈资转 ``expired``（计划 §7 C1"允许自然过期"）。

        ``pinned`` 的豁免；已经 expired/disabled/deleted 的不动。返回本次过期的那些。
        """

        stamp = self._now(now)
        changed: list[Topic] = []
        for topic in list(self._topics.values()):
            if topic.pinned or topic.expires_at is None:
                continue
            if _text(topic.status) != TOPIC_AVAILABLE:
                continue
            if _number(topic.expires_at, 0.0) > stamp:
                continue
            updated = self.edit(
                topic.topic_id,
                scope_id=topic.scope_id,
                status=TOPIC_EXPIRED,
            )
            changed.append(updated)
        return tuple(changed)

    # -- 查询契约（计划 §7 C6，X11） ---------------------------------------

    def query(
        self,
        *,
        scope_id: str = "",
        status: Any = None,
        source_kind: Any = None,
        cursor: Any = "",
        limit: Any = PAGE_LIMIT_DEFAULT,
        include_deleted: bool = False,
    ) -> "TopicPage":
        """分页查询：``(updated_at, topic_id)`` 倒序，游标分页，``limit`` 上限 100。

        ``status`` 给了就只留那一档（含 ``deleted``）；没给则默认隐藏软删除的，
        ``include_deleted=True`` 才把软删除的也列出来。排序明确、游标稳定——
        UI 的"谈资"页签直接用这个契约（计划 §7 C6）。
        """

        size = _clamp(limit, 1, PAGE_LIMIT_MAX, PAGE_LIMIT_DEFAULT)
        wanted_status = _text(status)
        wanted_kind = _text(source_kind)
        target_scope = _text(scope_id)
        items = [
            topic
            for topic in self._topics.values()
            if (not target_scope or _text(topic.scope_id) == target_scope)
            and (not wanted_status or _text(topic.status) == wanted_status)
            and (not wanted_kind or _text(topic.source_kind) == wanted_kind)
            and (
                bool(wanted_status)
                or include_deleted
                or _text(topic.status) != TOPIC_DELETED
            )
        ]
        total = len(items)
        marker = _decode_cursor(cursor)
        if marker is not None:
            items = [topic for topic in items if _sort_key(topic) < marker]
        items.sort(key=_sort_key, reverse=True)
        page = items[:size]
        next_cursor = _encode_cursor(page[-1]) if len(items) > size and page else ""
        return TopicPage(items=tuple(page), next_cursor=next_cursor, limit=size, total=total)

    # -- 快照（交给 engine/main 去落库，本模块自己不落） --------------------

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        """全部谈资的行字典（给接线层写表/写 JSON 用，保持稳定顺序）。"""

        return tuple(
            topic.to_row() for topic in sorted(self._topics.values(), key=_sort_key)
        )

    def load_snapshot(self, rows: Iterable[Any]) -> int:
        """从行字典重建池子（与 :meth:`snapshot` 配对）；返回恢复的条数。

        与 :meth:`restore` 不同——那个是把**软删除的谈资**恢复成可用状态；
        这个是把落库过的谈资读回内存池。
        """

        count = 0
        for row in rows or ():
            topic = Topic.from_row(row)
            if not topic.topic_id:
                continue
            self._topics[topic.topic_id] = topic
            count += 1
        return count

    def all_topics(self) -> tuple[Topic, ...]:
        """池内全部谈资（按 :meth:`snapshot` 同序），读模型用。"""

        return tuple(sorted(self._topics.values(), key=_sort_key))

    # -- 主动候选（计划 §7 C2） -------------------------------------------

    def select_proactive(
        self,
        *,
        session_id: str,
        shares_for: Callable[[str], Sequence[Any]],
        scope_id: str = "",
        now: float | None = None,
        limit: Any = DEFAULT_PROACTIVE_LIMIT,
        components_for: Callable[[Topic], ScoreComponents] | None = None,
        visibility_for: Callable[[Topic], Mapping[str, Any]] | None = None,
        person_id: str = "",
        weights: Mapping[str, float] | None = None,
        min_importance: Any = 0.0,
        authorization_for: Callable[[Topic], Any] | None = None,
        score_bonus_for: Callable[[Topic], Any] | None = None,
        score_bonus_weight: Any = 0.0,
    ) -> tuple[Candidate, ...]:
        """取少量、许可、未重复主动介绍过的谈资（计划 §7 C2）。

        - ``shares_for(source_id)``：这个来源的全部 share 行（engine 接
          ``db.shares_for_source``）——"这一版说过没有"由此判断，不看正文 hash；
        - ``components_for`` / ``visibility_for``：角色倾向、用户兴趣、来源可见性
          由调用方算好传进来；
        - ``limit`` 默认 1、硬上限 :data:`MAX_PROACTIVE_PER_TURN`：库存再多也不刷屏。
          ``limit = 0`` 是"这一轮一件都不主动提"（``topics.proactive_limit = 0``），
          **不是**"不限"——别重演 ``take_last_chat_groups(rows, 0)`` 那个坑；
        - ``min_importance``：重要度低于门槛的不主动提
          （``topics.proactive_min_importance``），0 = 不过滤。它只挡"主动提"，
          续说（``source_id``）那条路不走这里。
        - ``score_bonus_for`` / ``score_bonus_weight``：阶段B1（WP3）的**加法**语义分
          钩子。权重 0（默认）或钩子为 ``None`` 时完全不参与——排序与不传这两个参数
          时逐字节相同；权重非 0 时把 ``score_bonus_for(topic)`` 夹到 ``[0, 1]`` 再乘
          权重加到总分上。它**只影响排序，不影响任何许可判断**（授权仍然只看
          ``authorization_for``）。

        返回按分数降序（同分按 ``topic_id`` 稳定）的候选，**不含**被判定跳过的。
        """

        stamp = self._now(now)
        # 0（和负数）是"一件都不提"，不是"不限"：下界取 0 再判，别夹成 1。
        size = _clamp(limit, 0, MAX_PROACTIVE_PER_TURN, DEFAULT_PROACTIVE_LIMIT)
        if size <= 0:
            return ()
        threshold = _number(min_importance, 0.0)
        bonus_weight = _number(score_bonus_weight, 0.0)
        bonus_for = score_bonus_for if bonus_weight else None
        target_scope = _text(scope_id)
        candidates: list[Candidate] = []
        for topic in self._topics.values():
            if target_scope and _text(topic.scope_id) != target_scope:
                continue
            if threshold > 0 and _number(topic.importance, 0.0) < threshold:
                # 不够要紧的事不主动提；续说（source_id）那条路不经过这里
                continue
            if authorization_for is not None:
                from .topic_policy import PolicyDecision
                authorization = authorization_for(topic)
                if not isinstance(authorization, PolicyDecision) or not authorization.allowed:
                    continue
            elif visibility_for is not None:
                visibility = visibility_for(topic) or {}
                seen = visible_in_session(
                    visibility,
                    session_id=session_id,
                    scope_id=topic.scope_id,
                    person_id=person_id,
                )
                if not seen:
                    continue
            else:
                # No authoritative source callback is not permission. Legacy
                # callers must provide visibility or centralized authorization.
                continue
            shares = shares_for(topic.topic_id) or ()
            decision = decide_share(
                topic, session_id=session_id, shares=shares, now=stamp
            )
            if not decision:
                continue
            components = (
                components_for(topic) if components_for is not None else self._default_components(topic, stamp)
            )
            score = score_candidate(components, weights=weights)
            if bonus_for is not None:
                # 加法钩子（默认关闭）：只在权重非 0 时走这一步，所以"关闭"时
                # `score` 就是 `score_candidate(...)` 本身，排序与旧行为完全一致。
                score = _apply_score_bonus(score, bonus_for(topic), bonus_weight)
            candidates.append(
                Candidate(
                    topic=topic,
                    decision=decision,
                    score=score,
                )
            )
        candidates.sort(key=lambda item: (-item.score.total, item.topic.topic_id))
        return tuple(candidates[:size])

    def _default_components(self, topic: Topic, now: float) -> ScoreComponents:
        """没给评分口径时的中性分量：新信息 + 按创建时间算的时间新鲜度。"""

        window = self._recency_window if self._recency_window > 0 else DEFAULT_RECENCY_WINDOW
        age = max(0.0, _number(now, 0.0) - _number(topic.created_at, 0.0))
        return ScoreComponents(
            freshness=1.0,
            recency=_ratio(1.0 - age / window),
            interest=_ratio(topic.importance),
        )


_UNSET: Any = object()
"""``expires_at`` 的"没传"哨兵：``None`` 表示显式清掉有效期。"""


def _sort_key(topic: Topic) -> tuple[float, str]:
    """分页/快照的稳定排序键：``(updated_at, topic_id)``。"""

    return (_number(topic.updated_at, 0.0), _text(topic.topic_id))


def _encode_cursor(topic: Topic) -> str:
    """游标 = 最后一条的排序键（对调用方不透明）。"""

    stamp, topic_id = _sort_key(topic)
    # 用 repr 原样往返：``.6f`` 会把 time.time() 的微秒级尾数抹掉，解出来的游标
    # 比真实排序键略小，于是下一页会把边界那条**再发一次**（X11 翻页重复行）。
    return f"{stamp!r}|{topic_id}"


def _decode_cursor(cursor: Any) -> tuple[float, str] | None:
    """解游标；空串 / 坏值 → ``None``（从头开始，不报错）。"""

    text = _text(cursor)
    if not text:
        return None
    stamp, _, topic_id = text.partition("|")
    try:
        value = float(stamp)
    except (TypeError, ValueError):
        return None
    if not topic_id or not math.isfinite(value):
        return None
    return (value, topic_id)


@dataclass(frozen=True)
class TopicPage:
    """一页谈资（计划 §7 C6 的列表契约）。"""

    items: tuple[Topic, ...] = ()
    next_cursor: str = ""
    limit: int = PAGE_LIMIT_DEFAULT
    total: int = 0

    def __len__(self) -> int:
        return len(self.items)


# ---------------- 曝光读模型（计划 §4.4 + §7 C2/C6） ----------------


class ExposureLedger:
    """"这件事跟谁说过、说到哪一步"的读模型，数据源**只有** share 行。

    与 ``recent_replies`` 水位线分工（计划 §4.0）：水位线只回答"回没回过"，
    这里回答"以什么交际用途说过、发到哪个会话、送达状态如何"，二者不互相推导。
    """

    def __init__(self, shares: Iterable[Any] = ()) -> None:
        self._shares: tuple[Share, ...] = ()
        self.with_shares(shares)

    def with_shares(self, shares: Iterable[Any]) -> "ExposureLedger":
        """换一批 share 行（返回自身，便于链式构造）。"""

        parsed: list[Share] = []
        for raw in shares or ():
            share = _share_of(raw)
            if share is not None and _text(share.share_id):
                parsed.append(share)
        self._shares = tuple(parsed)
        return self

    def exposures(
        self, *, session_id: str = "", revision: Any = None, include_unconfirmed: bool = False
    ) -> tuple[Share, ...]:
        """算"说过"的 share（时间倒序）；``include_unconfirmed=True`` 连失败/无法确认也列。"""

        target = _text(session_id)
        wanted = None if revision is None else _whole(revision, -1)
        found = [
            share
            for share in self._shares
            if (include_unconfirmed or is_exposure(share))
            and (not target or _text(share.session_id) == target)
            and (wanted is None or _whole(share.information_revision, -1) == wanted)
        ]
        found.sort(key=lambda item: (_number(item.created_at, 0.0), item.share_id), reverse=True)
        return tuple(found)

    def introduced_revisions(self, session_id: str) -> tuple[int, ...]:
        """这个会话已经说过的版本（升序去重）。"""

        return introduced_revisions(
            [share.to_row() for share in self._shares], session_id=session_id
        )

    def has_introduced(self, *, session_id: str, revision: Any) -> bool:
        """这一版在这个会话说过没有。"""

        return _whole(revision, 0) in self.introduced_revisions(session_id)

    def exposed_sessions(self, *, revision: Any = None) -> tuple[str, ...]:
        """哪些会话已经听过（计划 §7 C6 列表里的"已向哪些会话分享"）。"""

        wanted = None if revision is None else _whole(revision, -1)
        found: list[str] = []
        for share in self.exposures(revision=None if wanted is None else wanted):
            session = _text(share.session_id)
            if session and session not in found:
                found.append(session)
        return tuple(found)

    def last_exposure(self, session_id: str) -> Share | None:
        """这个会话最近一次"说过"；没说过返回 ``None``。"""

        found = self.exposures(session_id=session_id)
        return found[0] if found else None

    def notes(self, *, session_id: str = "") -> tuple[str, ...]:
        """给日志/UI 看的中文说明（每条一张 share 一行）。"""

        return tuple(
            f"{share.share_id}→{_text(share.session_id) or '(未知会话)'}"
            f" rev{_whole(share.information_revision, 0)}"
            f" {_text(share.communicative_act) or '(未标用途)'}"
            f" [{_text(share.status)}]"
            for share in self.exposures(session_id=session_id)
        )


# ---------------- 续说协调（计划 §7 C3，X13） ----------------


class FollowupCoordinator:
    """动作续说与谈资抢同一个来源时的 claim 协调（计划 §4.4 ``share_claims`` + §7 C3）。

    规则（可判定，不猜）：

    - key = ``(source_id, revision, session_id)``，``intent_class`` 只是意图不是 key：
      **动作续说先占了，谈资就抢不到**（X13）；
    - 同一 key 至多一个活跃 claim；别人的活跃 claim 挡回 ``busy``，**同一个**
      ``owner_operation_id`` 重试自己的 claim 视为成功（幂等）；
    - 租约过期的活跃 claim 可以被接管（进程崩了不永久占 key）；
    - ``done`` 的 key 在多轮里不再重复续说——这就是"同一个动作/事件反复续说"的
      可判定规则；要再说只能是新 revision（新事实）；
    - 失败/取消用 :meth:`fail` / :meth:`release` 释放，并按最小重试间隔决定何时能再来。

    需要 engine 传进来的东西：``owner_operation_id``（计划 §4.1 的 operation key，
    用 ``source_events.operation_id_for`` 算）、``ttl``、会话/来源/revision。
    不读写数据库：接线时用 :meth:`ShareClaim.to_row` / :meth:`from_row` 落到
    ``share_claims`` 表（唯一索引建在 ``source_id, revision, session_id``）。
    """

    def __init__(
        self,
        clock: Callable[[], float],
        *,
        default_ttl: float = DEFAULT_CLAIM_TTL,
        retry_delay: float = 0.0,
    ) -> None:
        self._clock = clock
        self._default_ttl = _number(default_ttl, DEFAULT_CLAIM_TTL)
        self._retry_delay = max(0.0, _number(retry_delay, 0.0))
        self._claims: dict[str, ShareClaim] = {}

    def _now(self, now: float | None) -> float:
        return _number(self._clock() if now is None else now, 0.0)

    def get(self, claim_id: str) -> ShareClaim | None:
        """按 id 取一条 claim。"""

        return self._claims.get(_text(claim_id))

    def active_claims(
        self, *, source_id: str = "", session_id: str = "", now: float | None = None
    ) -> tuple[ShareClaim, ...]:
        """当前活跃（且租约未过期）的 claim。"""

        stamp = self._now(now)
        wanted_source = _text(source_id)
        wanted_session = _text(session_id)
        return tuple(
            claim
            for claim in self._claims.values()
            if claim.is_live(now=stamp)
            and (not wanted_source or claim.source_id == wanted_source)
            and (not wanted_session or claim.session_id == wanted_session)
        )

    def request(
        self,
        *,
        source_id: str,
        revision: Any,
        session_id: str,
        intent_class: str = ACT_INTRODUCE,
        owner_operation_id: str = "",
        ttl: Any = None,
        now: float | None = None,
    ) -> ClaimResult:
        """请求占用"我来说这件事"（计划 §7 C3：请求前占用）。

        ``granted=True`` 时返回的 ``claim`` 就是本次占用；被挡回时 ``existing``
        给出挡路的 claim 与中文理由（给日志/UI 排查）。
        """

        stamp = self._now(now)
        source = _text(source_id)
        session = _text(session_id)
        if not source or not session:
            return ClaimResult(
                False, None, None, "缺少来源或会话：不知道要跟谁说哪件事"
            )
        claim_id = claim_id_for(source_id=source, revision=revision, session_id=session)
        existing = self._claims.get(claim_id)
        if existing is not None:
            if existing.status == CLAIM_DONE:
                return ClaimResult(
                    False,
                    None,
                    existing,
                    f"这个来源的这一版在 {session} 已经续说过了：不重复续说",
                )
            if existing.status == CLAIM_ACTIVE:
                if existing.is_live(now=stamp):
                    if existing.owner_operation_id and existing.owner_operation_id == _text(
                        owner_operation_id
                    ):
                        return ClaimResult(True, existing, existing, "同一次操作重试：沿用原 claim")
                    return ClaimResult(
                        False,
                        None,
                        existing,
                        f"{existing.intent_class or '别的用途'}已经占住这个来源：不抢同一个 key",
                    )
                # 租约过期：接管
                self._claims[claim_id] = replace(existing, status=CLAIM_EXPIRED)
                existing = self._claims[claim_id]
            elif existing.status == CLAIM_RELEASED:
                if _number(existing.lease_until, 0.0) > stamp:
                    return ClaimResult(
                        False,
                        None,
                        existing,
                        "上次续说失败后的最小重试间隔还没到：晚点再说",
                    )
        lease = stamp + (
            self._default_ttl if ttl is None else max(0.0, _number(ttl, self._default_ttl))
        )
        claim = ShareClaim(
            claim_id=claim_id,
            source_id=source,
            revision=_whole(revision, 0),
            session_id=session,
            intent_class=_text(intent_class),
            owner_operation_id=_text(owner_operation_id),
            lease_until=lease,
            status=CLAIM_ACTIVE,
        )
        self._claims[claim_id] = claim
        return ClaimResult(True, claim, existing, "占用成功")

    def complete(self, claim_id: str) -> ShareClaim | None:
        """续说**真的送出**了：标 done——这个 key 之后不再续说（除非新 revision）。"""

        claim = self._claims.get(_text(claim_id))
        if claim is None:
            return None
        done = replace(claim, status=CLAIM_DONE)
        self._claims[done.claim_id] = done
        return done

    def fail(
        self, claim_id: str, *, reason: str = "", retry_delay: Any = None, now: float | None = None
    ) -> ShareClaim | None:
        """续说失败/取消：释放 key，并按最小重试间隔留在 ``lease_until`` 上。

        计划 §7 C3："失败释放claim并按明确重试/以后分享策略处理"——所以失败**不是**
        done，之后还能再来（间隔由 ``retry_delay`` 显式控制）。
        """

        claim = self._claims.get(_text(claim_id))
        if claim is None:
            return None
        delay = self._retry_delay if retry_delay is None else max(0.0, _number(retry_delay, 0.0))
        released = replace(
            claim,
            status=CLAIM_RELEASED,
            lease_until=self._now(now) + delay,
        )
        self._claims[released.claim_id] = released
        del reason  # 理由给调用方日志用；claim 行只记状态与门槛
        return released

    def release(self, claim_id: str, *, now: float | None = None) -> ShareClaim | None:
        """立刻释放（可以马上再抢），用于"用户叫停"这类明确放弃。"""

        claim = self._claims.get(_text(claim_id))
        if claim is None:
            return None
        released = replace(claim, status=CLAIM_RELEASED, lease_until=self._now(now))
        self._claims[released.claim_id] = released
        return released

    def expire_due(self, *, now: float | None = None) -> tuple[ShareClaim, ...]:
        """把过期租约的活跃 claim 标成 ``expired``（一次性回收，便于观测）。"""

        stamp = self._now(now)
        changed: list[ShareClaim] = []
        for claim in list(self._claims.values()):
            if claim.status != CLAIM_ACTIVE:
                continue
            if _number(claim.lease_until, 0.0) > stamp:
                continue
            expired = replace(claim, status=CLAIM_EXPIRED)
            self._claims[expired.claim_id] = expired
            changed.append(expired)
        return tuple(changed)

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        """全部 claim 的行字典（给接线层落 ``share_claims`` 表）。"""

        return tuple(
            self._claims[key].to_row() for key in sorted(self._claims)
        )

    def restore(self, rows: Iterable[Any]) -> int:
        """从行字典重建（与 :meth:`snapshot` 配对）；返回恢复条数。"""

        count = 0
        for row in rows or ():
            claim = ShareClaim.from_row(row)
            if not claim.claim_id:
                continue
            self._claims[claim.claim_id] = claim
            count += 1
        return count

    def claim_id_for(
        self, *, source_id: str, revision: Any, session_id: str
    ) -> str:
        """本协调器用的 key（与模块级 :func:`claim_id_for` 一致，便于测试对日志）。"""

        return claim_id_for(source_id=source_id, revision=revision, session_id=session_id)
