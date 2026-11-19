"""追问（``follow_up``）：她说了句在等回话的话，隔几分钟没人接，就再问一句。

口径（用户 m27236 / m28281）：

- **等不等回话由主模型自己说**：回话那一步顺手多输出一个 ``expects_reply``，
  不要靠"句子里有没有问号"猜；规则那套只留作老模型的兜底；
- 等 N 分钟（默认 10）没回应 → 追一句；
- 追之前**再让模型判一次**（同一次调用里输出 ``expects_reply``）：已经过了多久、
  追过几次，都告诉它，由它按话题判断"还值得等"还是"他大概还没看/这事已经过去了"；
- 最多追 1 次就放下，别变成催命（用户 2026-10-26：追问太啰嗦，几乎每句都追、
  而且几分钟后又真回来追一句）；超过次数时模型的"还想等"一律无视。

这一个模块只有纯函数：判断、提示词、回复解析、状态推进。
"问模型"和"真的发出去"在 :mod:`core.engine` 里，数据库那层在 :mod:`core.db`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

STATUS_OPEN = "open"
"""还在等：到点就该追一句。"""
STATUS_DONE = "done"
"""结束了（对方回了 / 追够了 / 她睡着了 / 换了一句话）。终态，不再动。"""

#: 追问最多追几次的默认值（用户 2026-10-26：只追一次，别再追着问）。
DEFAULT_MAX_TIMES = 1

#: 等多久没回应才追（分钟）。默认 10 分钟：3 分钟就回来追一句太急了。
DEFAULT_WAIT_MINUTES = 3.0

#: 老模型不认识 ``expects_reply`` 时的兜底预筛：一句话里出现这些就算"可能是在问"。
#: 新模型给的是结构化判断，这里只在下游缺字段时才用到。
QUESTION_HINTS = (
    "?",
    "？",
    "吗",
    "呢",
    "好不好",
    "行不行",
    "要不要",
    "能不能",
    "可不可以",
    "怎么样",
    "好吗",
    "对吧",
    "你说",
    "在不在",
    "在吗",
    "回我",
    "等你",
    "猜猜",
    "帮我看",
    "你说呢",
)


@dataclass
class FollowUp:
    """一条"她说完了在等回话"的记录。"""

    id: int = 0
    session_id: str = ""
    said_text: str = ""
    due_at: float = 0.0
    attempts: int = 0
    max_times: int = DEFAULT_MAX_TIMES
    status: str = STATUS_OPEN
    reason: str = ""
    target_user: str = ""
    """她在等的是谁（群里只认这个人回话；私聊里就是他）。空 = 没记下具体是谁。"""

    @classmethod
    def from_row(cls, row: Any) -> "FollowUp":
        data = dict(row or {})
        return cls(
            id=int(data.get("id") or 0),
            session_id=str(data.get("session_id") or ""),
            said_text=str(data.get("said_text") or ""),
            due_at=float(data.get("due_at") or 0.0),
            attempts=int(data.get("attempts") or 0),
            max_times=int(data.get("max_times") or DEFAULT_MAX_TIMES),
            status=str(data.get("status") or STATUS_OPEN),
            reason=str(data.get("reason") or ""),
            target_user=str(data.get("target_user") or ""),
        )

    def is_open(self) -> bool:
        return self.status == STATUS_OPEN

    def due(self, now: float) -> bool:
        return self.is_open() and float(now) >= float(self.due_at)

    def spent(self) -> bool:
        """追满了没有。``max_times`` 写 0 等于"不追"，第一句就是最后一句。"""

        return self.attempts >= max(0, int(self.max_times))


def looks_like_question(text: str) -> bool:
    """便宜的预筛：这句有没有"在等人答"的味道。

    只是预筛——``?``/``吗``/``呢`` 这类标记既漏（"你猜"）也误（"走吧"），
    所以它只用来决定"要不要为此多问一次模型"，不直接当成结论。
    """

    body = " ".join(str(text or "").split())
    if not body:
        return False
    return any(hint in body for hint in QUESTION_HINTS)


def judge_prompt(said: str, incoming: str) -> tuple[str, str]:
    """（旧路，保留给不认识 ``expects_reply`` 的外壳）问模型："她这句是在等回话吗？"""

    system = (
        "你在帮她判断刚刚那句话是不是在等对方回话。"
        "只输出 JSON：{\"waiting\": true 或 false, \"why\": \"一句话理由\"}，不要解释、不要加别的键。"
    )
    parts = [f"她刚发出去的话：{said}"]
    if incoming:
        parts.append(f"对方之前说的是：{incoming}")
    parts.append(
        "判断标准：问了个问题、要约个答复、让对方做决定、明显在等一句回应 = true；"
        "陈述、自言自语、告别、说完就算了 = false。"
    )
    return system, "\n".join(parts)


_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def parse_judgement(reply: str) -> dict[str, Any] | None:
    """把模型的判断读成 ``{"waiting": bool, "why": str}``；读不出来返回 None。"""

    text = str(reply or "").strip()
    if not text:
        return None
    match = _JSON_OBJECT.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "waiting" not in data:
        return None
    return {
        "waiting": bool(data.get("waiting")),
        "why": " ".join(str(data.get("why") or "").split())[:120],
    }


def followup_prompt(
    said: str,
    incoming: str,
    *,
    attempt: int = 1,
    max_times: int = DEFAULT_MAX_TIMES,
    waited_minutes: float = 0.0,
) -> tuple[str, str]:
    """写那一句追问，并让她自己说"还要不要继续等"。

    输出是一个 JSON：``text`` 是要发的话，``expects_reply`` 是"还想接着等吗"。
    已经过了多久、追过几次都写进提示词，让它按话题判断——而不是按字面。
    """

    system = (
        "你在替她决定要不要再追一句，以及追什么。"
        "只输出 JSON：{\"text\": \"要发的那一句\", \"expects_reply\": true 或 false}，"
        "不要解释、不要加别的键。"
    )
    parts = [f"她那句话是：{said}"]
    if incoming:
        parts.append(f"对方之前说的是：{incoming}")
    wait = float(waited_minutes or 0.0)
    if wait > 0:
        parts.append(f"从那句发出去到现在，已经过了大约 {max(1, int(round(wait)))} 分钟，对方一句话都没回。")
    attempt = max(1, int(attempt))
    total = max(1, int(max_times))
    parts.append(f"这会是第 {attempt} 次追问（最多 {total} 次）。")
    parts.append(
        "先判断还要不要接着等：看这件事本身——"
        "如果是约时间、要答复、让对方做决定、明显还等你一句，就 true；"
        "如果是随口一说、对方可能只是还没看见、这事已经过去了、再问会显得催，就 false。"
        "expects_reply 为 false 时 text 留空字符串。"
    )
    parts.append(
        "如果要追，text 只写这一句话本身：一句、不超过 30 字，不要引号、不要解释、不要写旁白；"
        "别重复原话，别质问、别催、别连发感叹号；可以换个由头（自嘲一句、说说自己在干嘛），"
        "语气跟平时的她一致。"
    )
    if attempt >= total:
        parts.append("这是最后一次追问：再没人回她就该放下这件事了。")
    return system, "\n".join(parts)


def followup_hint(
    said: str,
    *,
    attempt: int = 1,
    max_times: int = DEFAULT_MAX_TIMES,
    waited_minutes: float = 0.0,
) -> str:
    """给**主模型**的一段现场说明：她那句话没人接，现在要不要再问一句。

    追问走的是正常的续说提示词（人设、状态、最近的聊天都在），所以这里只补
    "现在是什么情况、你要顺手回一个 ``expects_reply``"，**不另起一套人设**。
    """

    attempt = max(1, int(attempt))
    total = max(1, int(max_times))
    parts: list[str] = []
    if said:
        parts.append(f"你刚才在这儿说的是：「{said}」。")
    wait = float(waited_minutes or 0.0)
    if wait <= 0:
        parts.append("这句话到现在还没有人接。")
    else:
        parts.append(
            f"这句话发出去已经过了大约 {max(1, int(round(wait)))} 分钟，中间没有人接。"
        )
    parts.append(f"这是第 {attempt} 次追问（最多 {total} 次）。")
    parts.append(
        "先判断还要不要接着等：约时间、要答复、让对方做决定、明显还在等一句，就接着等；"
        "随口一说、对方大概只是还没看见、这事已经过去了、再问会显得催，就别等了。"
        "不想再等就**什么都别说**（messages 留空）；要等就只说一句很短的话（≤30 字，别重复原话、"
        "别质问、别催），并按 JSON 格式给出 \"expects_reply\": true 或 false。"
    )
    if attempt >= total:
        parts.append("这已经是最后一次：再没人回，这件事就放下。")
    return "".join(parts)


def parse_followup_reply(reply: str) -> dict[str, Any]:
    """读 ``{"text": …, "expects_reply": …}``；不是 JSON 就整段当那句话（还接着等）。"""

    text = str(reply or "").strip()
    if not text:
        return {"text": "", "expects_reply": False}
    match = _JSON_OBJECT.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict) and ("text" in data or "expects_reply" in data):
            line = clean_reply_line(str(data.get("text") or ""))
            raw_want = data.get("expects_reply")
            if isinstance(raw_want, str):
                want = raw_want.strip().lower() not in ("false", "no", "0", "不", "否")
            else:
                want = bool(raw_want) if raw_want is not None else bool(line)
            return {"text": line, "expects_reply": bool(want) and bool(line)}
    line = clean_reply_line(text)
    return {"text": line, "expects_reply": bool(line)}


def parse_wait_reply(reply: str) -> dict[str, Any]:
    """读追问那一次的 ``{"text", "expects_reply"}``，保留"没写这个字段"的三态。

    跟 :func:`parse_followup_reply` 的差别只有一处：追问那一轮**可以什么都不说、
    但仍然接着等**（``text`` 空 + ``expects_reply=True``）。旧解析会把这种 combo
    塌成"不等了"，于是一次没话说就被当成"这件事放下了"，追问链路整条静默——
    这正是用户 2026-10-28 说的"追问没了下文"。所以 ``expects_reply`` 为 ``None``
    时必须原样传出去，由 :func:`core.engine.sweep_follow_ups` 决定要不要收口。
    """

    raw = str(reply or "").strip()
    if not raw:
        return {"text": "", "expects_reply": None}
    data = _wait_payload(raw)
    if data is not None:
        line = clean_reply_line(str(data.get("text") or ""))
        if not line:
            line = _first_say_line(data)
        want = data.get("expects_reply")
        if isinstance(want, str):
            flag: bool | None = want.strip().lower() not in ("false", "no", "0", "不", "否")
        elif want is None:
            flag = None
        else:
            flag = bool(want)
        return {"text": line, "expects_reply": flag}
    match = _JSON_OBJECT.search(raw)
    if match:
        try:
            data = json.loads(match.group(0))
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict) and (
            "text" in data
            or "expects_reply" in data
            or "actions" in data
            or "messages" in data
        ):
            line = clean_reply_line(str(data.get("text") or ""))
            if not line:
                line = _first_say_line(data)
            want = data.get("expects_reply")
            if isinstance(want, str):
                flag: bool | None = want.strip().lower() not in ("false", "no", "0", "不", "否")
            elif want is None:
                flag = None
            else:
                flag = bool(want)
            return {"text": line, "expects_reply": flag}
    line = clean_reply_line(raw)
    return {"text": line, "expects_reply": True if line else None}


def _wait_payload(raw: str) -> dict[str, Any] | None:
    """从模型输出里捞出追问那一个 JSON 对象（比"整段正则"稳）。

    2026-10-29 实机：她把
    ``{"text": "……还没动静呀？", "expects_reply": true}`` 原样发到了聊天里。
    解析失败的原因是"整段贪婪匹配"太粗——模型常在 JSON 前后多写一句解释、
    或者把契约模板也抄一遍，整段跨度一长就不是合法 JSON 了。这里改成：
    先整段直接 loads；不行就按"从左到右配对的花括号"逐个试，只要有一个对象
    带着 ``text`` / ``expects_reply`` 就算。
    """

    text = str(raw or "").strip()
    if not text:
        return None
    try:
        whole = json.loads(text)
    except (TypeError, ValueError):
        whole = None
    if isinstance(whole, dict) and _looks_like_wait(whole):
        return whole
    start = -1
    depth = 0
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                chunk = text[start:index + 1]
                try:
                    data = json.loads(chunk)
                except (TypeError, ValueError):
                    data = None
                if isinstance(data, dict) and _looks_like_wait(data):
                    return data
                start = -1
    return None


def _looks_like_wait(data: dict[str, Any]) -> bool:
    return any(key in data for key in ("text", "expects_reply", "messages", "actions"))


def looks_like_payload(text: str) -> bool:
    """这句话是不是"还没解析的 JSON"（这种绝不能再发给用户）。"""

    body = str(text or "").strip()
    if not body or not body.startswith("{"):
        return False
    return any(key in body for key in ("\"text\"", "expects_reply", "\"messages\"", "\"actions\""))


def _first_say_line(data: dict[str, Any]) -> str:
    """模型按老格式写 ``actions:[{type:"say", messages:[…]}]`` 时取第一条话。

    追问提示词现在只要 ``text``，但模型偶尔还是会把回话那套动作格式搬过来；
    读的时候多认一种写法，是容错，不是在提示词上打补丁——提示词里仍然只教一种。
    """

    for action in list(data.get("actions") or []):
        if not isinstance(action, dict) or str(action.get("type") or "") != "say":
            continue
        for text in list(action.get("messages") or []):
            line = clean_reply_line(str(text or ""))
            if line:
                return line
    messages = data.get("messages")
    if isinstance(messages, list):
        for text in messages:
            line = clean_reply_line(str(text or ""))
            if line:
                return line
    return ""


def clean_reply_line(reply: str, *, limit: int = 60) -> str:
    """把模型吐出来的那句收拾成可以直接发的样子（去引号、去前缀、压成一行）。"""

    text = str(reply or "").strip()
    if not text:
        return ""
    text = text.strip("\"'“”‘’")
    text = re.sub(r"^\s*(她说|回复|追问)\s*[:：]\s*", "", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text
