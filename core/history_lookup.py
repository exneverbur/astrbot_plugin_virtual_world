"""B2：聊天记录**只读查询**的纯逻辑半边（解析 / 夹取 / 游标 / 成文）。

为什么单独一层：让她"自己翻聊天记录"最容易出的两种事故，都不是数据库问题——
一是**无差别 dump**（把几百条历史塞进上下文，钱和效果一起烂），二是**越界**
（查到别的会话/别人格的私事）。所以这一层把三件事钉死：

1. **一定要有个由头**：要么给关键词、要么给时间窗，否则直接拒（不许"给我看看聊天记录"）；
2. **窗口与条数都夹死**：时间窗默认 7 天、最多 30 天，条数默认 20、最多 50，
   总字数也封顶；夹取的地方**要告诉模型**（不是悄悄改小）；
3. **游标是不透明字符串**：只认本模块编出来的，坏游标当"从最新开始"，绝不解析成 SQL。

落库查询在 `core/db.py`（走既有的会话事件查询），接线在引擎的只读工具档——
这一层不做那两件事，所以可以单独测。
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

LOCAL_ERROR = "local"

LOOKUP_ACTION_ID = "lookup_history"
"""内置动作的 id（世界动作表里那一条，用户 2026-10-26）。

与 agent 档里的只读工具（``vw_history_lookup``）是**两件事**：那条要开
「开启Agent功能」再跑一个任务，这条她任何一轮都能自己翻一次。两边查库与成文
共用 ``Engine._history_lookup_text``，所以夹取口径只有这一份。
"""

DEFAULT_WINDOW_DAYS = 7.0
MAX_WINDOW_DAYS = 30.0
DEFAULT_LIMIT = 20
HARD_LIMIT = 50
CHARS_PER_ROW = 200
MAX_TOTAL_CHARS = 2400

ORDER_RECENT = "recent"
ORDER_OLDEST = "oldest"
ORDERS = (ORDER_RECENT, ORDER_OLDEST)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LookupRequest:
    """一次只读查询的**规范化**参数（夹取之后的，不是模型原话）。"""

    session_id: str = ""
    keyword: str = ""
    sender: str = ""
    since: float = 0.0
    until: float = 0.0
    limit: int = DEFAULT_LIMIT
    order: str = ORDER_RECENT
    cursor: str = ""
    kinds: tuple[str, ...] = ()
    """只要哪几类事件（留空 = 聊天记录本身）。"""

    has_reason: bool = False
    """有没有"由头"（关键词或时间窗）——没有就拒绝执行。"""

    notes: tuple[str, ...] = ()
    """夹取与归一过程中要**告诉模型**的话（不是静默修改）。"""


def parse_request(
    params: Mapping[str, Any] | None,
    *,
    session_id: str,
    now: float | None = None,
    default_window_days: float = DEFAULT_WINDOW_DAYS,
    max_window_days: float = MAX_WINDOW_DAYS,
    default_limit: int = DEFAULT_LIMIT,
    hard_limit: int = HARD_LIMIT,
) -> LookupRequest:
    """把模型给的参数夹成 :class:`LookupRequest`（不认识的一律忽略 + 记一条说明）。"""

    data = dict(params or {}) if isinstance(params, Mapping) else {}
    stamp = _float(now, time.time())
    notes: list[str] = []

    keyword = _text(data.get("keyword") or data.get("query") or data.get("关键词"))
    if len(keyword) > 60:
        keyword = keyword[:60]
        notes.append("关键词太长，只取了前 60 个字")

    sender = _text(data.get("sender") or data.get("user") or data.get("from"))
    window = _float(data.get("days") or data.get("window_days"), 0.0)
    since = _float(data.get("since"), 0.0)
    until = _float(data.get("until"), 0.0)
    if window > 0:
        since = stamp - window * 86400.0
    if since <= 0 and until <= 0:
        # 没给时间窗：按默认窗口兜（并在 notes 里说清）
        since = stamp - max(0.1, _float(default_window_days, DEFAULT_WINDOW_DAYS)) * 86400.0
        notes.append(f"没给时间范围，按最近 {int(_float(default_window_days, DEFAULT_WINDOW_DAYS))} 天查")
    if since > 0 and until <= 0:
        until = stamp
    if since > 0 and until > 0 and since > until:
        since, until = until, since
        notes.append("起止时间写反了，已经对调")
    if since > 0:
        floor = stamp - max(1.0, _float(max_window_days, MAX_WINDOW_DAYS)) * 86400.0
        if since < floor:
            since = floor
            notes.append(f"时间范围最多往前 {int(_float(max_window_days, MAX_WINDOW_DAYS))} 天，已经收窄")

    limit = _int(data.get("limit"), default_limit)
    if limit <= 0:
        limit = default_limit
    if limit > hard_limit:
        notes.append(f"一次最多 {hard_limit} 条，已经收窄")
        limit = hard_limit

    order = _text(data.get("order")).lower()
    if order not in ORDERS:
        order = ORDER_RECENT

    kinds_raw = data.get("kinds") or data.get("kind") or ()
    if isinstance(kinds_raw, str):
        kinds_raw = (kinds_raw,)
    kinds = tuple(
        dict.fromkeys(_text(item) for item in kinds_raw if _text(item))
    ) if isinstance(kinds_raw, (list, tuple, set, frozenset)) else ()

    cursor = _text(data.get("cursor"))
    if cursor and decode_cursor(cursor) is None:
        notes.append("游标看不懂，从最新一条重新开始")
        cursor = ""

    has_reason = bool(keyword) or bool(_float(data.get("since"), 0.0) or _float(data.get("until"), 0.0) or window > 0)
    return LookupRequest(
        session_id=_text(session_id),
        keyword=keyword,
        sender=sender,
        since=since,
        until=until,
        limit=limit,
        order=order,
        cursor=cursor,
        kinds=kinds,
        has_reason=has_reason,
        notes=tuple(notes),
    )


def refusal_reason(request: LookupRequest) -> str:
    """没由头 / 没会话就拒绝（回给模型看的一句话，不是异常）。"""

    if not request.session_id:
        return "查不了：不知道要查哪个会话。"
    if not request.has_reason:
        return "查不了：得说清找什么（给个关键词）或者哪个时间段，不能把聊天记录整段翻出来。"
    return ""


def encode_cursor(row: Mapping[str, Any]) -> str:
    """把一行的位置编成不透明游标（``时间戳 + id``，base64 成一段字符串）。"""

    stamp = _float(row.get("created_at") or row.get("at") or row.get("ts"))
    key = _text(row.get("id") or row.get("message_id") or row.get("event_id"))
    if stamp <= 0:
        return ""
    payload = json.dumps({"t": round(stamp, 6), "k": key}, ensure_ascii=False)
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(text: Any) -> tuple[float, str] | None:
    """解游标；看不懂一律 ``None``（调用方当"没有游标"，绝不拼进 SQL）。"""

    raw = _text(text)
    if not raw:
        return None
    padded = raw + "=" * (-len(raw) % 4)
    try:
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    stamp = _float(data.get("t"))
    if stamp <= 0:
        return None
    return stamp, _text(data.get("k"))


def format_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    request: LookupRequest,
    chars_per_row: int = CHARS_PER_ROW,
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> dict[str, Any]:
    """把查到的行排成**有界**的结果（每条都带时间与人，正文按条裁剪）。

    返回 ``{items, count, next_cursor, truncated, chars, scope, note}``；
    ``truncated=True`` 的意思不是"没查到"，而是"还有下一页"。
    """

    items: list[dict[str, Any]] = []
    chars = 0
    last_row: Mapping[str, Any] | None = None
    clipped = 0
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        if len(items) >= max(1, int(request.limit)):
            break
        text = _text(row.get("text") or row.get("content") or row.get("summary"))
        if len(text) > chars_per_row:
            text = text[:chars_per_row] + "…"
            clipped += 1
        if chars + len(text) > max_total_chars and items:
            break
        stamp = _float(row.get("created_at") or row.get("at") or row.get("ts"))
        items.append(
            {
                "at": stamp,
                "at_text": time.strftime("%m-%d %H:%M", time.localtime(stamp)) if stamp else "",
                "sender": _text(row.get("sender") or row.get("sender_name") or row.get("user_id")),
                "text": text,
                "id": _text(row.get("id") or row.get("message_id") or row.get("event_id")),
            }
        )
        chars += len(text)
        last_row = row
    next_cursor = encode_cursor(last_row) if last_row is not None else ""
    return {
        "items": items,
        "count": len(items),
        "next_cursor": next_cursor,
        "truncated": bool(next_cursor),
        "chars": chars,
        "clipped": clipped,
        "scope": scope_note(request),
        "note": "；".join(request.notes),
    }


def scope_note(request: LookupRequest) -> str:
    """这次到底查了哪一块（写清边界，模型才不会以为"全世界都查过了"）。"""

    parts = ["这一个会话"]
    if request.since > 0:
        days = max(0.0, (request.until - request.since) / 86400.0)
        parts.append(f"最近 {days:.1f} 天")
    if request.keyword:
        parts.append(f"含「{request.keyword}」")
    if request.sender:
        parts.append(f"只找 {request.sender} 说的")
    if request.kinds:
        parts.append("只看 " + "、".join(request.kinds))
    parts.append(f"最多 {request.limit} 条")
    return "、".join(parts)


def render(result: Mapping[str, Any] | None) -> str:
    """把结果排成给模型看的一段文本（空的就说没找到，不编）。"""

    data = result if isinstance(result, Mapping) else {}
    items = list(data.get("items") or [])
    scope = _text(data.get("scope"))
    if not items:
        return f"（在{scope}里没找到）" if scope else "（没找到）"
    lines = [f"在{scope}里找到 {len(items)} 条："] if scope else [f"找到 {len(items)} 条："]
    for entry in items:
        who = _text(entry.get("sender")) or "有人"
        when = _text(entry.get("at_text"))
        lines.append(f"- {when} {who}：{_text(entry.get('text'))}")
    if data.get("truncated"):
        lines.append("（还有更早的，可以带 cursor 继续翻）")
    note = _text(data.get("note"))
    if note:
        lines.append(f"（{note}）")
    return "\n".join(lines)
