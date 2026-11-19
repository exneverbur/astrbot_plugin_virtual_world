"""P2-2：问题和回答的账本（`task_questions`）。

一条记录 = 她问出去的一个问题。三种人会给回应，必须分开处理：

* **被问的那个人**（``asker`` 本人）答的 → 才算"拍板"，可以推进任务；
* **别人**答的 → 只当**建议**（`advice`），任务继续等那个人——别人不能替人拍板；
* **没人理** → 到期作废（`expired`），**沉默不等于同意**。

为什么要有账本而不是"在内存里等一句"：重启会把内存清空，而"她问过什么、问的是谁、
发出去没有"必须留得下来，否则重启后她会**重复问同一个问题**、或者把别人的话当授权。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

QUESTION_ASKED = "asked"
QUESTION_DELIVERED = "delivered"
QUESTION_ANSWERED = "answered"
QUESTION_EXPIRED = "expired"
QUESTION_CANCELLED = "cancelled"

QUESTION_STATUSES = (
    QUESTION_ASKED,
    QUESTION_DELIVERED,
    QUESTION_ANSWERED,
    QUESTION_EXPIRED,
    QUESTION_CANCELLED,
)

QUESTION_STATUS_LABELS = {
    QUESTION_ASKED: "已登记",
    QUESTION_DELIVERED: "已发出",
    QUESTION_ANSWERED: "已答复",
    QUESTION_EXPIRED: "已过期",
    QUESTION_CANCELLED: "已取消",
}

#: 回答来源：本人（能拍板）/ 别人（只是建议）
ANSWER_BY_OWNER = "owner"
ANSWER_BY_OTHER = "other"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def new_question_id(now_ms: int, seq: int = 0) -> str:
    return f"q_{int(now_ms)}_{max(0, int(seq)):03d}"


def build_question(
    *,
    question_id: str,
    task_id: str,
    task_revision: int,
    question: str,
    session_id: str,
    asker: str = "",
    persona_id: str = "",
    kind: str = "info",
    now: float = 0.0,
    ttl_seconds: float = 0.0,
    options: Any = None,
    message_id: str = "",
) -> dict[str, Any]:
    """登记一条问题（还没发出去：状态是 ``asked``）。"""

    stamp = _float(now)
    ttl = max(0.0, _float(ttl_seconds))
    return {
        "question_id": _text(question_id),
        "task_id": _text(task_id),
        "task_revision": int(task_revision or 0),
        "kind": _text(kind) or "info",
        "payload_json": json.dumps(
            {
                "question": _text(question),
                "options": [str(item) for item in (options or ())],
            },
            ensure_ascii=False,
        ),
        "status": QUESTION_ASKED,
        "scope": _text(session_id),
        "persona_id": _text(persona_id),
        "asker": _text(asker),
        "delivery_status": "",
        "message_id": _text(message_id),
        "answer": "",
        "answer_source": "",
        "created_at": stamp,
        "expires_at": (stamp + ttl) if ttl > 0 else 0.0,
        "answered_at": 0.0,
        "updated_at": stamp,
    }


def question_text(row: Mapping[str, Any] | None) -> str:
    """这条问题问的是什么（读不懂就当空）。"""

    raw = (row or {}).get("payload_json")
    if not raw:
        return ""
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return ""
    return _text((data or {}).get("question"))


def is_open(row: Mapping[str, Any] | None) -> bool:
    """还等着人回答（可以接一句）。"""

    return _text((row or {}).get("status")) in (QUESTION_ASKED, QUESTION_DELIVERED)


def mark_delivered(
    row: Mapping[str, Any], *, receipt: Any = None, now: float = 0.0
) -> dict[str, Any]:
    """把"发送结果"如实记下来：发出去了才算 delivered，没确认就是 asked。"""

    item = dict(row)
    if not is_open(item):
        return item
    status = str(getattr(receipt, "status", "") or "")
    ids = list(getattr(receipt, "platform_message_ids", ()) or ())
    delivered = bool(receipt) or status in ("accepted", "delivered")
    item["delivery_status"] = status or ("accepted" if delivered else "failed")
    if ids:
        item["message_id"] = str(ids[0])
    if delivered:
        item["status"] = QUESTION_DELIVERED
    item["updated_at"] = _float(now) or _float(item.get("updated_at"))
    return item


def classify_answer(row: Mapping[str, Any], *, user_id: str) -> str:
    """这句回答是"本人拍板"还是"别人的建议"。

    没记 ``asker`` 的时候**一律当建议**——宁可多等一轮，也不把别人当成当事人。
    """

    asker = _text((row or {}).get("asker"))
    who = _text(user_id)
    if asker and who and asker == who:
        return ANSWER_BY_OWNER
    return ANSWER_BY_OTHER


def consume_answer(
    row: Mapping[str, Any],
    *,
    answer: str,
    user_id: str = "",
    now: float = 0.0,
) -> dict[str, Any]:
    """接一句回答。**一次消费**：已经答过的不再改口；别人的只记建议不改状态。

    返回的 dict 里 ``consumed`` 表示"这次真的推进了任务"，``advice`` 表示"只是建议"。
    """

    item = dict(row)
    text = _text(answer)
    item["consumed"] = False
    item["advice"] = False
    if not is_open(item) or not text:
        return item
    who = classify_answer(item, user_id=user_id)
    stamp = _float(now) or _float(item.get("updated_at"))
    if who == ANSWER_BY_OWNER:
        item["status"] = QUESTION_ANSWERED
        item["answer"] = text
        item["answer_source"] = ANSWER_BY_OWNER
        item["answered_at"] = stamp
        item["updated_at"] = stamp
        item["consumed"] = True
        return item
    # 别人的话只当建议：状态不动，留给本人拍板
    item["advice"] = True
    item["advice_text"] = text
    item["advice_by"] = _text(user_id)
    item["advice_at"] = stamp
    item["updated_at"] = stamp
    return item


def expire_question(row: Mapping[str, Any], *, now: float = 0.0) -> dict[str, Any]:
    """到期作废（沉默不等于同意）。"""

    item = dict(row)
    if not is_open(item):
        return item
    stamp = _float(now)
    expires = _float(item.get("expires_at"))
    if expires <= 0 or stamp < expires:
        return item
    item["status"] = QUESTION_EXPIRED
    item["updated_at"] = stamp
    return item


def cancel_question(row: Mapping[str, Any], *, now: float = 0.0) -> dict[str, Any]:
    item = dict(row)
    if not is_open(item):
        return item
    item["status"] = QUESTION_CANCELLED
    item["updated_at"] = _float(now) or _float(item.get("updated_at"))
    return item
