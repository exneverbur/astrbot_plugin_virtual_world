"""输入事件身份与操作 key（阶段 A1 的纯逻辑层）。

为什么单独一个文件：事件身份（``event_id``）与操作 key（``operation_id``）是后面
episode / share / consolidation job 全部幂等判定的公共键。这里只做纯计算，不碰
sqlite、不碰 WorldState；持久层（``core/db.py`` 的 ``source_events`` / ``schema_meta``）
与适配层（``main.py::_incoming_identity``）都调这里的函数，保证「同一个事件」
「同一次操作」在哪儿都换算出同一个字符串。

两条硬规则（计划 §2.3 不变量 / §5 A1）：

- **事件身份不认正文**：身份只来自平台 ``message_id`` 或监听侧生成的稳定内部 id，
  绝不用正文 hash 另造一套去重键——同样内容的两条是用户真的说了两遍。
- **操作 key 稳定可复现**：同一次真实操作的不同重试靠 ``attempt`` 区分，
  同一 ``(kind, session_id, action_id, anchor, attempt)`` 永远得到同一个 id。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# 事件身份的字符串格式只有一个真相：core.state.chat_event_key（recent_chat 与留档
# 里的 event_key 就是它算的）。这里直接复用，避免同一事件在两张表里对不上。
from .state import chat_event_key

# 分享的送达状态复用发送回执的口径（计划 §5 A2）：accepted 不是 delivered。
from .ports import SEND_ACCEPTED

__all__ = [
    "REALITY_BOT_EXTERNAL",
    "REALITY_FICTION",
    "REALITY_GENERATED",
    "REALITY_GENERATED_MEDIA",
    "REALITY_KINDS",
    "REALITY_LEGACY",
    "REALITY_REAL",
    "REALITY_TOOL",
    "SCHEMA_VERSION",
    "SENSITIVITY_LEVELS",
    "SHARE_KINDS",
    "SHARE_LEGACY_OBSERVED",
    "Share",
    "SourceEvent",
    "VISIBILITY_LOCKED",
    "VISIBILITY_NORMAL",
    "VISIBILITY_PERSON",
    "VISIBILITY_PRIVATE",
    "build_visibility",
    "chat_row_to_source_event",
    "normalize_event_id",
    "normalize_sensitivity",
    "operation_id_for",
    "share_id_for",
    "strictest_sensitivity",
    "visibility_level",
]

SCHEMA_VERSION = 1
"""``source_events`` 行的结构版本（表级版本见 ``schema_meta.schema_version``）。"""

REALITY_REAL = "real"
"""现实中真实发生的输入（用户打字、真实拍摄的图片）。"""

REALITY_TOOL = "tool"
"""工具/宿主能力产生的结果（搜索、命令回执）。"""

REALITY_FICTION = "fiction"
"""角色自己虚构的内容（内心戏、假设、设定内的演出）。"""

REALITY_GENERATED = "generated"
"""模型生成的意图/文本，不等于现实。"""

REALITY_GENERATED_MEDIA = "generated_media"
"""模型生成的图片等素材；不推断为「现实拍摄」。"""

REALITY_LEGACY = "legacy_unknown"
"""旧数据来源无法确定（计划 §4.0：不回填猜测值）。"""

REALITY_BOT_EXTERNAL = "bot_external"
"""别的 bot / 外部系统发来的事件。"""

REALITY_KINDS = frozenset(
    {
        REALITY_REAL,
        REALITY_TOOL,
        REALITY_FICTION,
        REALITY_GENERATED,
        REALITY_GENERATED_MEDIA,
        REALITY_LEGACY,
        REALITY_BOT_EXTERNAL,
    }
)
"""全部真实性标签（给界面与校验用；落库不强制校验，避免旧数据被拒）。"""

VISIBILITY_NORMAL = "normal"
"""普通事（天气、剧、爱好）：组内任意会话都可主动提。"""

VISIBILITY_PERSON = "person"
"""涉及某个具体人的事：只在**当事人所在的会话**主动提。"""

VISIBILITY_PRIVATE = "private"
"""敏感事（身体、感情、钱、家庭矛盾）：不主动提，被问起也只在本会话说。"""

VISIBILITY_LOCKED = "locked"
"""用户明确说了"别外传"：从宽到严排序里最严的一档，TTL 也按"永不过期"算。

它和 :data:`VISIBILITY_PRIVATE` 的传播范围完全一样（都不主动提），差别只在
有效期：锁定是**用户的明确意愿**，所以不能被自动过期悄悄清掉。
"""

SENSITIVITY_LEVELS = (
    VISIBILITY_NORMAL,
    VISIBILITY_PERSON,
    VISIBILITY_PRIVATE,
    VISIBILITY_LOCKED,
)
"""敏感度四档，**从宽到严**排列（取最严 = 取下标最大那个）。"""

_SENSITIVITY_ALIASES: dict[str, str] = {
    # 中文与常见别名：模型不一定严格按词表给值，能认就认，认不出才降级。
    "普通": VISIBILITY_NORMAL,
    "公开": VISIBILITY_NORMAL,
    "public": VISIBILITY_NORMAL,
    "open": VISIBILITY_NORMAL,
    "人物": VISIBILITY_PERSON,
    "某人": VISIBILITY_PERSON,
    "personal": VISIBILITY_PERSON,
    "涉及他人": VISIBILITY_PERSON,
    "私密": VISIBILITY_PRIVATE,
    "保密": VISIBILITY_PRIVATE,
    "secret": VISIBILITY_PRIVATE,
    "sensitive": VISIBILITY_PRIVATE,
    "锁定": VISIBILITY_LOCKED,
    "别外传": VISIBILITY_LOCKED,
    "不要外传": VISIBILITY_LOCKED,
}
"""档位别名 → 规范档位；**没有"更宽"的别名**，别名只用来认同一档。"""


def normalize_sensitivity(value: Any) -> str:
    """把模型/调用方给的敏感度收成四档之一；**认不出的一律按最严**。

    「模型只能往严调」这条规则在这里的第一道闸就是：未知值、空值、类型不对，
    全部落到 :data:`VISIBILITY_PRIVATE`——宁可按私密存着（少说一句），
    也不能因为写错一个字就把不该外传的事当普通事讲出去。
    """

    text = _text(value).lower()
    if not text:
        return VISIBILITY_PRIVATE
    if text in SENSITIVITY_LEVELS:
        return text
    return _SENSITIVITY_ALIASES.get(text, VISIBILITY_PRIVATE)


def strictest_sensitivity(*values: Any) -> str:
    """一组档位取**最严**（``locked`` > ``private`` > ``person`` > ``normal``）。

    一个谈资往往由多条来源撑着，每条来源各说各的敏感度；只按第一条算，
    等于给最宽那条开了后门——所以这里按"最严的那条说了算"。
    """

    best = VISIBILITY_NORMAL
    best_rank = 0
    for value in values:
        level = normalize_sensitivity(value)
        rank = SENSITIVITY_LEVELS.index(level)
        if rank > best_rank:
            best, best_rank = level, rank
    return best


def build_visibility(
    *,
    scope_id: str = "",
    origin_session_id: str = "",
    sensitivity: Any = VISIBILITY_NORMAL,
    persons: Iterable[Any] = (),
    sessions: Iterable[Any] = (),
    hard: bool = False,
    allowed_sessions: Mapping[str, Any] | None = None,
    authority: str = "trusted_rule",
) -> dict[str, Any]:
    """构造一条 ``visibility`` 声明（**唯一**的拼装入口，别处不再手拼 dict）。

    返回的字典就是 ``source_events.visibility_json`` 的内容：

    - ``sensitivity``：四档之一（:func:`normalize_sensitivity` 收口，认不出按 private）；
    - ``scope``：会话组 id——组之间不共享，所以它同时是"最外层的墙"；
    - ``origin_session_id``：来源会话（``private`` 档只在这一条会话里说）；
    - ``persons`` / ``sessions``：更细的许可名单（``person`` 档靠 ``persons``）；
    - ``private`` / ``locked``：布尔标识，给只认老字段的读取端留后路。

    ``hard=True`` 表示用户明确说了"别外传"：档位直接升到
    :data:`VISIBILITY_LOCKED`——**代码只允许往严调**，调用方想把它塞回普通档
    是塞不进去的（模型给的更宽档位在这一步被丢掉）。
    """

    level = normalize_sensitivity(sensitivity)
    if hard and level != VISIBILITY_LOCKED:
        level = VISIBILITY_LOCKED
    # 去重但保持顺序：这个 dict 会落库、会被测试逐字比对，顺序稳定才好看。
    person_ids = tuple(dict.fromkeys(_text(item) for item in persons or () if _text(item)))
    session_ids = tuple(
        dict.fromkeys(_text(item) for item in sessions or () if _text(item))
    )
    origin = _text(origin_session_id)
    from .topic_policy import build_policy
    policy = build_policy(
        scope_id=_text(scope_id), origin_session_id=origin, sensitivity=level,
        subject_ids=person_ids, hard_private=hard or level == VISIBILITY_LOCKED,
        allowed_sessions=allowed_sessions if allowed_sessions is not None else ({"mode": "restricted", "ids": list(session_ids)} if session_ids else None),
        authority=authority,
    )
    return {**policy,
        "sensitivity": VISIBILITY_LOCKED if policy["hard_private"] else policy["sensitivity"],
        "scope": _text(scope_id),
        "origin_session_id": origin,
        "persons": list(person_ids),
        "sessions": list(session_ids),
        "private": level in (VISIBILITY_PRIVATE, VISIBILITY_LOCKED),
        "locked": level == VISIBILITY_LOCKED,
    }


def visibility_level(visibility: Any) -> str:
    """读出一条声明的档位；**没有声明时按"什么都能提"的普通档**。

    注意这是"这条声明自己写了什么"，不是"这条谈资允不允许"——
    多条来源取最严要用 :func:`strictest_sensitivity`。
    """

    data = visibility if isinstance(visibility, Mapping) else {}
    if not data:
        return VISIBILITY_PRIVATE
    if data.get("locked") is True:
        return VISIBILITY_LOCKED
    raw = data.get("sensitivity")
    if _text(raw):
        return normalize_sensitivity(raw)
    # 老声明只写 private/audience，没有 sensitivity：按最严那档理解，别当普通事。
    if data.get("private") is True or _text(data.get("audience")) in ("private", "self"):
        return VISIBILITY_PRIVATE
    return VISIBILITY_NORMAL


def _text(value: Any) -> str:
    """统一成去掉首尾空白的字符串（None → 空串）。"""

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
    """转 float（坏值 / NaN → default）。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):  # NaN / inf
        return default
    return number


def _whole(value: Any, default: int = 0) -> int:
    """转 int（"1.0" 这种也要吃得下）。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _optional_number(value: Any) -> float | None:
    """可缺的时间戳：``None`` / 认不出的值 / 非正数一律给 ``None``（"不知道什么时候"）。"""

    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _cell(row: Any, key: str, default: Any = "") -> Any:
    """宽容地取一格：缺列（dict → KeyError，sqlite3.Row → IndexError）给默认值。"""

    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _keys(row: Any) -> list[str]:
    """行的列名；拿不到就是空列表。"""

    getter = getattr(row, "keys", None)
    if not callable(getter):
        return []
    try:
        return [str(key) for key in getter()]
    except (TypeError, ValueError):
        return []


def _json_object(value: Any) -> dict[str, Any]:
    """payload / visibility：坏 JSON、空串、非对象一律降级成空 dict，不抛异常。"""

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


@dataclass(frozen=True)
class SourceEvent:
    """一条**已捕获**的输入事件（或系统动作结果）的身份与来源。

    ``payload`` / ``visibility`` 是任意结构的附加信息，落库时序列化成
    ``payload_json`` / ``visibility_json``；:meth:`to_row` / :meth:`from_row`
    负责在两者之间互转，并且**从不就地修改**传进来的对象。
    """

    event_id: str
    scope_id: str = ""
    origin_session_id: str = ""
    sender_id: str = ""
    event_kind: str = "message"
    occurred_at: float = 0.0
    payload: Mapping[str, Any] = field(default_factory=dict)
    reality_kind: str = REALITY_REAL
    visibility: Mapping[str, Any] = field(default_factory=dict)
    operation_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_row(self) -> dict[str, Any]:
        """转成 ``source_events`` 表的列字典（payload/visibility → JSON 字符串）。"""

        return {
            "event_id": _text(self.event_id),
            "scope_id": _text(self.scope_id),
            "origin_session_id": _text(self.origin_session_id),
            "sender_id": _text(self.sender_id),
            "event_kind": _text(self.event_kind),
            "occurred_at": _number(self.occurred_at, 0.0),
            "payload_json": json.dumps(
                dict(self.payload or {}), ensure_ascii=False, default=str
            ),
            "reality_kind": _text(self.reality_kind) or REALITY_REAL,
            "visibility_json": json.dumps(
                dict(self.visibility or {}), ensure_ascii=False, default=str
            ),
            "operation_id": _text(self.operation_id),
            "schema_version": _whole(self.schema_version, SCHEMA_VERSION),
        }

    @classmethod
    def from_row(cls, row: Any) -> "SourceEvent":
        """从一行（dict / sqlite3.Row）还原；缺列、坏 JSON 都降级成默认值，不抛异常。"""

        from .topic_policy import normalize_policy
        raw_visibility = _cell(row, "visibility_json", _cell(row, "visibility", {}))
        parsed_visibility = _json_object(raw_visibility)
        if isinstance(raw_visibility, str) and raw_visibility.strip():
            try:
                decoded = json.loads(raw_visibility)
            except (ValueError, TypeError):
                decoded = None
            if not isinstance(decoded, dict):
                parsed_visibility = normalize_policy(raw_visibility)
        return cls(
            event_id=_text(_cell(row, "event_id")),
            scope_id=_text(_cell(row, "scope_id")),
            origin_session_id=_text(_cell(row, "origin_session_id")),
            sender_id=_text(_cell(row, "sender_id")),
            event_kind=_text(_cell(row, "event_kind")) or "message",
            occurred_at=_number(_cell(row, "occurred_at"), 0.0),
            payload=_json_object(
                _cell(row, "payload_json", _cell(row, "payload", {}))
            ),
            reality_kind=_text(_cell(row, "reality_kind")) or REALITY_REAL,
            visibility=parsed_visibility,
            operation_id=_text(_cell(row, "operation_id")),
            schema_version=_whole(_cell(row, "schema_version"), SCHEMA_VERSION),
        )


def normalize_event_id(
    *, message_id: Any = "", event_id: Any = "", origin: Any = "", fallback: Any = ""
) -> str:
    """把「这条消息是谁」归一成一个稳定字符串。

    规则（计划 §5 A1：复用已有 incoming identity，**不另造正文 hash**）：

    1. 有平台 ``message_id`` → ``f"{origin}|m:{message_id}"``；
    2. 否则用调用方给的内部 ``event_id`` → ``f"{origin}|e:{event_id}"``；
    3. 再看 ``fallback``（例如引擎记录里算好的 ``event_key``）；
    4. 全空返回 ``""``——**空串表示"不知道身份"**，由调用方决定丢弃或走保守兜底。

    字符串格式由 :func:`core.state.chat_event_key` 定义（这里直接复用它），所以
    ``source_events.event_id`` 与 ``recent_chat`` / 留档里的 ``event_key`` 永远是
    同一个字符串，集成时能直接对上；以后改格式也只改一处，不会再分叉。

    本函数**没有**接收正文的参数：同样内容的两条消息只要 ``message_id`` 不同，
    身份就不同（合法重复不吞）；反过来，正文被注解改过也不影响身份。
    """

    key = chat_event_key(_text(origin), _text(message_id), _text(event_id))
    if key:
        return key
    return _text(fallback)


def operation_id_for(
    *,
    kind: str,
    session_id: str,
    action_id: str = "",
    anchor: str = "",
    attempt: int = 0,
) -> str:
    """一次真实操作的稳定 key：``sha1("{kind}|{session_id}|{action_id}|{anchor}|{attempt}")``
    的前 32 位十六进制。

    同一次真实操作的不同重试靠 ``attempt`` 区分（重试传同一个 ``attempt`` 就是
    同一个操作，不会重复提交副作用）；同一 ``(kind, session_id, action_id, anchor,
    attempt)`` 永远得到同一个 id——它是 §4.4 claim、§4.6 job 与副作用去重的公共键。
    """

    raw = "|".join(
        (
            _text(kind),
            _text(session_id),
            _text(action_id),
            _text(anchor),
            str(_whole(attempt, 0)),
        )
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


_CHAT_ROW_IDENTITY_KEYS = frozenset(
    {
        "event_id",
        "event_key",
        "message_id",
        "origin",
        "origin_session_id",
        "scope_id",
        "session",
        "session_id",
        "sender_id",
        "user_id",
        "occurred_at",
        "event_kind",
        "reality_kind",
        "visibility",
        "visibility_json",
        "payload",
        "payload_json",
        "operation_id",
        "schema_version",
    }
)
"""已经落进 SourceEvent 专用字段的列名：不再重复塞进 ``payload``。"""


def chat_row_to_source_event(
    row: Any,
    *,
    scope_id: Any = "",
    origin_session_id: Any = "",
    occurred_at: Any = 0.0,
    event_kind: Any = "message",
    reality_kind: Any = REALITY_REAL,
) -> SourceEvent | None:
    """引擎聊天记录行（dict / sqlite3.Row）→ :class:`SourceEvent`。

    ``row`` 里认这些列（缺了也不报错）：``message_id`` / ``event_id`` / ``event_key``
    / ``origin``（或 ``session`` / ``session_id``）/ ``sender_id``（或 ``user_id``）/
    ``text`` 等；**没有任何可用身份时返回 ``None``**（计划要求「合法重复不吞」，
    所以这里只判身份、绝不按正文去重）。

    除身份/来源列之外的普通列会原样抄进 ``payload``（浅拷贝，不改调用方对象）。
    """

    if row is None:
        return None
    if not _keys(row) and not isinstance(row, Mapping):
        return None

    origin = (
        _text(_cell(row, "origin"))
        or _text(_cell(row, "session"))
        or _text(_cell(row, "session_id"))
    )
    identity = normalize_event_id(
        message_id=_cell(row, "message_id"),
        event_id=_cell(row, "event_id"),
        origin=origin,
        fallback=_cell(row, "event_key"),
    )
    if not identity:
        return None

    payload: dict[str, Any] = {}
    for name in _keys(row):
        if name in _CHAT_ROW_IDENTITY_KEYS:
            continue
        payload[name] = _cell(row, name)
    if origin:
        payload.setdefault("origin", origin)
    extra = _cell(row, "payload", None)
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            payload.setdefault(str(key), value)

    scope = _text(scope_id) or _text(_cell(row, "scope_id")) or origin
    session = _text(origin_session_id) or origin
    stamp = 0.0
    for name in ("occurred_at", "at", "created_at", "timestamp", "ts"):
        stamp = _number(_cell(row, name), 0.0)
        if stamp > 0:
            break
    if stamp <= 0:
        stamp = _number(occurred_at, 0.0)

    return SourceEvent(
        event_id=identity,
        scope_id=scope,
        origin_session_id=session,
        sender_id=_text(_cell(row, "sender_id")) or _text(_cell(row, "user_id")),
        event_kind=_text(_cell(row, "event_kind")) or _text(event_kind) or "message",
        occurred_at=stamp,
        payload=payload,
        reality_kind=_text(_cell(row, "reality_kind")) or _text(reality_kind) or REALITY_REAL,
        visibility=_json_object(_cell(row, "visibility", {})),
        operation_id=_text(_cell(row, "operation_id")),
        schema_version=_whole(_cell(row, "schema_version"), SCHEMA_VERSION),
    )


# ---------------- 分享登记（计划 §4.4 / §5 A2） ----------------

SHARE_KINDS = ("reply", "live_say", "routed", "schedule", "extension", "media", "legacy")
"""``communicative_act`` 的受控词表：以什么交际用途说了这句。"""

SHARE_LEGACY_OBSERVED = "legacy_observed"
"""旧历史里看得出来"她说过"，但没有平台回执：如实标注，绝不冒充 delivered（计划 §9）。"""


def _text_list(value: Any) -> list[str]:
    """把任意形状归一成字符串列表：单项 / 序列 / 逗号串都收，空项丢掉，保序去重。"""

    if value is None:
        return []
    if isinstance(value, str):
        raw: list[Any] = [value]
    elif isinstance(value, Mapping):
        raw = [value]
    else:
        try:
            raw = list(value)
        except TypeError:
            raw = [value]
    result: list[str] = []
    for item in raw:
        if isinstance(item, Mapping):
            text = ""
            for key in ("id", "media_id", "message_id", "text"):
                text = _text(item.get(key))
                if text:
                    break
        else:
            text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


def _json_list(value: Any) -> list[str]:
    """JSON 数组列（``media_ids_json`` / ``platform_message_ids_json``）→ 字符串列表。"""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return _text_list(value)
    return _text_list(value)


def share_id_for(
    *,
    scope_id: Any = "",
    session_id: Any = "",
    source_id: Any = "",
    information_revision: Any = 0,
    communicative_act: Any = "",
    operation_id: Any = "",
    content_hash: Any = "",
) -> str:
    """一条分享的稳定 key（同一件事重复提交落到同一行）。

    同一次操作（``operation_id``）在同一个会话里只该有一条 share：发出去了就记一次，
    重试 / 重复提交靠 ``INSERT OR IGNORE`` 落到同一行。``operation_id`` 为空时退回到
    「来源 + revision + 交际用途 + 正文 hash」——仍然尽量让重复提交落到同一行。
    什么都给不出来就返回 ``""``：**不知道身份就不落库**，不编一个。
    """

    parts = [
        _text(source_id),
        str(_whole(information_revision, 0)) if _whole(information_revision, 0) else "",
        _text(communicative_act),
        _text(content_hash),
        _text(operation_id),
    ]
    # 身份要素一个都没有就返回 ""：revision 默认 0 不算身份（否则每条空记录都会算
    # 出同一个 key，往库里塞一行"没有身份的分享"）。宁可不落库，也不编一个。
    if not any(part.strip() for part in parts):
        return ""
    anchor = "|".join(parts)
    return operation_id_for(
        kind="share",
        session_id=_text(session_id),
        action_id=_text(scope_id),
        anchor=anchor,
    )


@dataclass(frozen=True)
class Share:
    """**已确认送出**的一次分享（计划 §4.4）。

    ``status`` 记的是送达事实——``accepted``（平台接下了）/ ``delivered``（拿到了平台
    消息 id）/ ``legacy_observed``（旧历史看得出来但没有回执）：绝不把 accepted 当
    delivered，也绝不把失败记成"对方听过了"。``media_ids`` /
    ``platform_message_ids`` 落库是 JSON 数组。
    """

    share_id: str
    scope_id: str = ""
    source_id: str = ""
    information_revision: int = 0
    session_id: str = ""
    communicative_act: str = ""
    operation_id: str = ""
    content_hash: str = ""
    text: str = ""
    media_ids: tuple[str, ...] = ()
    status: str = SEND_ACCEPTED
    platform_message_ids: tuple[str, ...] = ()
    created_at: float = 0.0
    sent_at: float | None = None

    def to_row(self) -> dict[str, Any]:
        """转成 ``shares`` 表的列字典（两个 id 列表 → JSON 字符串）。"""

        return {
            "share_id": _text(self.share_id),
            "scope_id": _text(self.scope_id),
            "source_id": _text(self.source_id),
            "information_revision": _whole(self.information_revision, 0),
            "session_id": _text(self.session_id),
            "communicative_act": _text(self.communicative_act),
            "operation_id": _text(self.operation_id),
            "content_hash": _text(self.content_hash),
            "text": _text(self.text),
            "media_ids_json": json.dumps(
                _text_list(self.media_ids), ensure_ascii=False
            ),
            "status": _text(self.status) or SEND_ACCEPTED,
            "platform_message_ids_json": json.dumps(
                _text_list(self.platform_message_ids), ensure_ascii=False
            ),
            "created_at": _number(self.created_at, 0.0),
            "sent_at": (
                None if self.sent_at is None else _number(self.sent_at, 0.0)
            ),
        }

    @classmethod
    def from_row(cls, row: Any) -> "Share":
        """``shares`` 一行 → :class:`Share`（缺列、坏 JSON、``None`` 都不抛）。"""

        return cls(
            share_id=_text(_cell(row, "share_id")),
            scope_id=_text(_cell(row, "scope_id")),
            source_id=_text(_cell(row, "source_id")),
            information_revision=_whole(_cell(row, "information_revision"), 0),
            session_id=_text(_cell(row, "session_id")),
            communicative_act=_text(_cell(row, "communicative_act")),
            operation_id=_text(_cell(row, "operation_id")),
            content_hash=_text(_cell(row, "content_hash")),
            text=_text(_cell(row, "text")),
            media_ids=tuple(_json_list(_cell(row, "media_ids_json", []))),
            status=_text(_cell(row, "status")) or SEND_ACCEPTED,
            platform_message_ids=tuple(
                _json_list(_cell(row, "platform_message_ids_json", []))
            ),
            created_at=_number(_cell(row, "created_at"), 0.0),
            sent_at=_optional_number(_cell(row, "sent_at", None)),
        )
