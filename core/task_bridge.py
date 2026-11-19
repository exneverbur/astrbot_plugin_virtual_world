"""VW 侧处理 Agent 的追问（P1-4）：自己答 / 改目标 / 暂缓 / 取消。

为什么不让"一问就转发给用户"：Agent 缺的常常不是用户才知道的事——会话、地点、
现在几点、今天天气、她的画像、刚才聊过什么，这些 VW 自己就有。把这类问题丢给用户，
既打扰人，也把"她该自己决定的事"推给了别人（方案 §7.3）。

这份模块只做**判定**（纯函数，不调模型、不写库）：

* :func:`decide` —— 给定问题 + VW 手上已知的事实（``facts``，名字→值）：
  - 能答：事实里有对应的一条 → ``answer``；
  - 已答过：检查点里记着同一个问题的答案 → 直接复用（**不重复确认**）；
  - 答不了：``defer``（P2 才真去问用户；这一版只是"先搁着"，到期收尾）；
  - 问题本身不该问（涉及权限/隐私/越权）：``cancel``；
* :func:`match_fact` —— 问题里出现事实的名字或同义词就认作"能答"。

**不做**：不猜用户偏好、不替用户授权、不在这一层发消息。这几条是 P2 的事，
而且必须由真实的发送回执与作用域校验支撑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

DECISION_ANSWER = "answer"
DECISION_DEFER = "defer"
DECISION_CANCEL = "cancel"
DECISION_ASK_USER = "ask_user"
"""P2 才会真的做：把问题发给用户。P1-4 里它只会被降级成 ``defer``。"""

DECISION_LABELS = {
    DECISION_ANSWER: "自己答",
    DECISION_DEFER: "先搁着",
    DECISION_CANCEL: "不做了",
    DECISION_ASK_USER: "问她/他",
}

#: 一看就不该由 Agent 来问的（越权 / 隐私 / 让她替用户做决定）
_FORBIDDEN_MARKS = (
    "密码",
    "密钥",
    "身份证",
    "银行卡",
    "授权码",
    "别人的聊天",
    "私聊内容",
    "把消息发到",
    "删掉所有",
)

#: 事实名 → 问题里可能出现的写法（同义词越多，越少无谓的追问）
_FACT_ALIASES: dict[str, tuple[str, ...]] = {
    "session": ("会话", "哪个群", "在哪说", "发到哪"),
    "node": ("在哪", "地点", "地方", "位置"),
    "time": ("几点", "现在时间", "什么时候", "今天几号"),
    "weather": ("天气", "下雨", "几度", "温度"),
    "persona": ("她叫", "名字", "怎么称呼", "人设"),
    "tone": ("语气", "口吻", "怎么说"),
}


@dataclass(frozen=True)
class Decision:
    """VW 对一次追问的处理结果。"""

    action: str = DECISION_DEFER
    answer: str = ""
    reason: str = ""
    fact: str = ""
    """命中的事实名（``answer`` 时有值）。"""
    resolved_from: str = ""
    """``fact`` = 从事实里答的；``checkpoint`` = 之前答过；空 = 没答。"""

    @property
    def label(self) -> str:
        return DECISION_LABELS.get(self.action, self.action)

    @property
    def needs_user(self) -> bool:
        return self.action in (DECISION_ASK_USER, DECISION_DEFER)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "label": self.label,
            "answer": self.answer,
            "reason": self.reason,
            "fact": self.fact,
            "resolved_from": self.resolved_from,
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def looks_forbidden(question: str) -> bool:
    """这句是不是在问"不该由她打听"的东西。"""

    body = _text(question)
    return any(mark in body for mark in _FORBIDDEN_MARKS)


def match_fact(question: str, facts: Mapping[str, Any] | None) -> tuple[str, str]:
    """在问题里找一条已知事实；返回 ``(事实名, 值)``，找不到就是 ``("", "")``。

    先按事实名本身匹配，再按同义词。**只认明确写进去的事实**——没写就不算知道。
    """

    body = _text(question)
    if not body:
        return "", ""
    for name, value in (facts or {}).items():
        key = _text(name)
        if not key or not _text(value):
            continue
        if key in body or key in question:
            return key, _text(value)
    for name, aliases in _FACT_ALIASES.items():
        value = _text((facts or {}).get(name))
        if not value:
            continue
        for alias in aliases:
            if alias in body:
                return name, value
    return "", ""


def already_answered(question: str, checkpoint: Mapping[str, Any] | None) -> str:
    """检查点里记着的、同一问题的答案（有就复用，不重复确认）。"""

    body = _text(question)
    pairs = (checkpoint or {}).get("vw_answers") or {}
    if not isinstance(pairs, Mapping):
        return ""
    for asked, answer in pairs.items():
        if _text(asked) == body and _text(answer):
            return _text(answer)
    return ""


def remember_answer(checkpoint: Mapping[str, Any] | None, question: str, answer: str) -> dict[str, Any]:
    """把"问过什么、答了什么"记进检查点（最多留 8 条，别无限长）。"""

    payload = dict(checkpoint or {})
    pairs = payload.get("vw_answers")
    pairs = dict(pairs) if isinstance(pairs, Mapping) else {}
    pairs[_text(question)] = _text(answer)
    while len(pairs) > 8:
        pairs.pop(next(iter(pairs)))
    payload["vw_answers"] = pairs
    return payload


#: "要不要真的问一句"的判定原因码（账本/回执都认它）
ASK_OK = "ok"
ASK_DISABLED = "ask_disabled"
ASK_TOO_MANY = "too_many_asks"
ASK_NO_TARGET = "no_target"


@dataclass(frozen=True)
class AskVerdict:
    """要不要真的去问用户（P2-1）。"""

    allowed: bool = False
    reason: str = ASK_DISABLED
    asked_so_far: int = 0
    limit: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "reason": self.reason,
            "asked_so_far": int(self.asked_so_far),
            "limit": int(self.limit),
        }


def should_ask(
    *,
    enabled: bool,
    asked_so_far: int,
    max_per_task: int,
    target_session: str = "",
) -> AskVerdict:
    """要不要真的问一句。

    **不看的**：说话预算、说话频率限制 —— 追问不是闲聊，为了省一句额度把任务卡死是
    更坏的失败（用户 2026-10-17 的口径）。**只看的**：开关、这个任务已经问了几次、
    有没有可发的落点。
    """

    stated = max(0, int(asked_so_far or 0))
    limit = max(0, int(max_per_task or 0))
    if not enabled:
        return AskVerdict(False, ASK_DISABLED, stated, limit)
    if limit <= 0 or stated >= limit:
        return AskVerdict(False, ASK_TOO_MANY, stated, limit)
    if not _text(target_session):
        return AskVerdict(False, ASK_NO_TARGET, stated, limit)
    return AskVerdict(True, ASK_OK, stated, limit)


def decide(
    question: str,
    *,
    facts: Mapping[str, Any] | None = None,
    checkpoint: Mapping[str, Any] | None = None,
    allow_ask_user: bool = False,
) -> Decision:
    """VW 的决定：自己答 / 先搁着 / 不做了 / （P2）问她。

    ``allow_ask_user=False``（P1-4 的默认）时，本该问用户的会退成 ``defer``——
    这一版**不真去打扰人**，但也不会假装答了。
    """

    body = _text(question)
    if not body:
        return Decision(DECISION_CANCEL, reason="Agent 没写出要问什么，这件事没法继续")
    if looks_forbidden(body):
        return Decision(
            DECISION_CANCEL,
            reason="这个问题不该问她：涉及不该打听的东西（密码/凭据/别人的私事）",
        )
    remembered = already_answered(body, checkpoint)
    if remembered:
        return Decision(
            DECISION_ANSWER,
            answer=remembered,
            reason="之前答过同一个问题，直接复用（不重复确认）",
            resolved_from="checkpoint",
        )
    name, value = match_fact(body, facts)
    if name and value:
        return Decision(
            DECISION_ANSWER,
            answer=value,
            reason=f"她手上就有这个信息（{name}）",
            fact=name,
            resolved_from="fact",
        )
    if allow_ask_user:
        return Decision(
            DECISION_ASK_USER,
            reason="她不知道，但可以问一句（这一版还没接发送）",
        )
    return Decision(
        DECISION_DEFER,
        reason="她不知道，也没有可以问的人；先搁着，到期再收尾",
    )


@dataclass(frozen=True)
class FactBook:
    """VW 手上"这一轮能确定的事实"（引擎每轮现取，别缓存成全局）。"""

    rows: dict[str, str] = field(default_factory=dict)

    def with_fact(self, name: str, value: Any) -> "FactBook":
        rows = dict(self.rows)
        if _text(name) and _text(value):
            rows[_text(name)] = _text(value)
        return FactBook(rows)

    def as_dict(self) -> dict[str, str]:
        return dict(self.rows)
