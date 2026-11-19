"""T12 Step 3（E2 话题层）：她会不会接你抛来的话题。

分层口径与已上线的表达变奏（``core/response_variation.py`` 的 ``tease``）**完全一致**：

- **规则层只回答"这一轮允不允许拐"**（:func:`steer_permission`）：正经/叫停的回合不许、
  这一轮本来就有失败动作不许、冷却（最近 :data:`STEER_COOLDOWN_TURNS` 轮内已经拐过）
  不许、每天 :data:`STEER_DAILY_CAP` 次上限；动手/求助/急事永远不拐。
- **拐不拐、拐到哪一条是模型的决定**：它在回复 JSON 里写
  ``topic: {"move": "follow|own|probe|switch", "own": "候选里的哪一条"}``。
  缺字段 / 坏形状**一律当 ``follow``**（照常接话），不报错、不刷日志。
- **后果必须落地**（这条才是本步的重点）：``move=own`` 要写 ``event_log``、
  在 ``state`` 里记一条待兑现；tick 到点没接着说 → 注入一句提醒；再过一个窗口还没提
  → 标 ``dropped`` 并留日志。"提一句就没下文"这条路必须看得见。

为什么候选不新建表：``state.own_topics``（她自己记着的事）+ ``state.intention``
（Step 1 的打算）+ ``state.current_action``（她正在做的）已经够了，这里只是一层
**纯规则的翻译**（不调模型、不读库、不加依赖），最多 :data:`MAX_CANDIDATES` 条。

冷却 / 每日上限**按人落 ``state``**（``state.topic_steer_moves``），不用内存 dict——
上一批变奏就是因为内存冷却在重启后失效踩过坑。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_CANDIDATES = 3
"""最多翻译出几条「她自己的事」：条目再多，提示词里那一小段就变成任务清单了。"""

CANDIDATE_CHARS = 40
"""一条候选最多几个字（``own_topics`` 本身限 60 字，这里再收一刀）。"""

STEER_KINDS = frozenset({"chat", "affection"})
"""允许拐走的回合类型：闲聊 / 分享 / 八卦 / 要安慰。

``task`` 有自己的协商路径（Step 1），``help`` / ``urgent`` 是"永远照做"的硬闸。
"""

TOPIC_MOVES = ("follow", "own", "probe", "switch")
"""这一轮话题上的四种走法：接下去 / 拐到自己 / 追问细节 / 换话题。"""

STEER_COOLDOWN_TURNS = 5
"""冷却：最近这么多轮里已经拐走过，就不许再拐（按 ``state.chat_seq`` 算）。"""

STEER_DAILY_CAP = 2
"""每人每天最多拐走几次。"""

STEER_FOLLOW_TICKS = 2
"""拐走之后几个 tick 内要接上（``state.world_time`` 的 tick 数）。"""

STEER_DROP_TICKS = 3
"""提醒过了再过一个这么大的窗口还没提起 → 明确放弃（``dropped``）。"""

STEER_MOVES_KEEP = 20
"""``state.topic_steer_moves`` 只留最近这么多条：够算冷却与日上限就行。"""

STATUS_PENDING = "pending"
STATUS_FOLLOWED = "followed"
STATUS_DROPPED = "dropped"
"""待兑现的三种状态：挂着 / 接上了 / 明确放弃。"""

_STOP_BIGRAMS = frozenset(
    {
        "我们",
        "你们",
        "他们",
        "你的",
        "我的",
        "这个",
        "那个",
        "什么",
        "一下",
        "时候",
        "已经",
        "可以",
        "不是",
        "就是",
        "还是",
        "没有",
        "自己",
        "觉得",
        "现在",
        "今天",
        "然后",
        "因为",
        "所以",
        "起来",
        "出来",
    }
)
"""「她想的是不是同一件事」的词表里要跳过的口水二元组。

不做分词：中文没有空格，这里只用"候选里出现的连续两字"去对方那句话里找。
口水词（"我们""一下"）命中不算提起，否则每一句都会被她算成"我说了"。
"""

_MOVE_ALIASES = {
    "接": "follow",
    "接话": "follow",
    "follow": "follow",
    "自己": "own",
    "拐": "own",
    "拐走": "own",
    "own": "own",
    "追问": "probe",
    "问": "probe",
    "probe": "probe",
    "换": "switch",
    "换话题": "switch",
    "switch": "switch",
}
"""模型写中文也认：它经常把 ``move`` 写成"拐走""追问"这种。"""


@dataclass(frozen=True)
class TopicCandidate:
    """一条「她自己的事」：候选正文 + 它从哪来（只用于日志/调试）。"""

    text: str
    source: str = ""
    """``doing``（正在做）/ ``intention``（打算）/ ``own_topic``（她自己记着的）。"""

    def to_dict(self) -> dict[str, str]:
        return {"text": self.text, "source": self.source}


@dataclass(frozen=True)
class SteerPermission:
    """这一轮**允不允许**把话拐走（规则层的唯一输出）。

    ``allowed=False`` 时 ``candidates`` 一定是空的：提示词里那一小段根本不会出现，
    于是"不许拐"的回合与今天的提示词逐字节相同。
    """

    allowed: bool
    blocked_by: str = ""
    candidates: tuple[TopicCandidate, ...] = ()
    reason: str = ""

    def cand_texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "candidates": [item.to_dict() for item in self.candidates],
        }


def clip_text(value: Any, limit: int = CANDIDATE_CHARS) -> str:
    """一句话收成一行、截断到 ``limit``：候选进提示词前必须干净。"""

    body = " ".join(str(value or "").split())
    return body[: max(1, int(limit))]


# ---------------- 候选：只翻译现成状态，不新建表 ----------------


def _intention_text(state: Any) -> str:
    """``state.intention`` 里那条**还活着**的打算（Step 1 上线的那份）。"""

    raw = getattr(state, "intention", None)
    if not isinstance(raw, Mapping):
        return ""
    text = clip_text(raw.get("text"))
    if not text:
        return ""
    if str(raw.get("state") or "") in {"expired", "fulfilled", "abandoned"}:
        return ""
    return text


def _action_text(state: Any) -> str:
    """``state.current_action`` 的一句话描述：她**此刻正在做的**那件事。"""

    action = getattr(state, "current_action", None)
    if not isinstance(action, Mapping):
        return ""
    return clip_text(action.get("desc") or action.get("type") or "")


def _own_topics(state: Any) -> list[str]:
    """``state.own_topics`` 里她答应过 / 想做还没做的（最多两条，与提示词那一段一致）。"""

    out: list[str] = []
    for item in list(getattr(state, "own_topics", None) or [])[:2]:
        if not isinstance(item, Mapping):
            continue
        text = clip_text(item.get("text"))
        if text and text not in out:
            out.append(text)
    return out


def candidates(state: Any) -> tuple[TopicCandidate, ...]:
    """把现成状态翻译成至多 :data:`MAX_CANDIDATES` 条「她自己的事」。

    顺序就是"她这会儿最可能真想起来"的顺序：正在做的 → 立着的打算 → 记着的事。
    空正文 / 重复正文一律丢掉；一条都没有时返回空元组（上游据此**不写**提示词）。
    """

    rows: list[TopicCandidate] = []
    seen: set[str] = set()

    def add(text: str, source: str) -> None:
        body = clip_text(text)
        if not body or body in seen:
            return
        seen.add(body)
        rows.append(TopicCandidate(text=body, source=source))

    add(_action_text(state), "doing")
    add(_intention_text(state), "intention")
    for text in _own_topics(state):
        add(text, "own_topic")
        if len(rows) >= MAX_CANDIDATES:
            break
    return tuple(rows[:MAX_CANDIDATES])


# ---------------- 冷却 / 上限：按人、落 state（跨重启仍生效） ----------------


def recent_moves(state: Any) -> list[dict[str, Any]]:
    """``state.topic_steer_moves`` 的干净副本（坏值一律当没有）。"""

    rows: list[dict[str, Any]] = []
    for item in list(getattr(state, "topic_steer_moves", None) or []):
        if isinstance(item, Mapping):
            rows.append(dict(item))
    return rows


def note_move(state: Any, *, now: float, turn: int, own: str, session: str = "") -> None:
    """记一次"她真的把话拐走了"（冷却 + 每日上限的唯一依据）。

    落 ``state`` 而不是内存：重启 / 换进程之后冷却与上限照样生效（上一批变奏踩过的坑）。
    """

    rows = recent_moves(state)
    rows.append(
        {
            "at": float(now),
            "turn": int(turn),
            "own": clip_text(own),
            "session": str(session or ""),
        }
    )
    try:
        setattr(state, "topic_steer_moves", rows[-STEER_MOVES_KEEP:])
    except Exception:  # noqa: BLE001 —— 记不上冷却也不能挡住回复
        pass


def last_move_turn(state: Any) -> int:
    """最近一次拐走是在第几轮（没有就是 0）。"""

    turns = [int(item.get("turn") or 0) for item in recent_moves(state)]
    return max(turns) if turns else 0


def moved_today(state: Any, *, now: float) -> int:
    """最近 24 小时里拐走了几次。"""

    stamp = float(now or 0.0)
    count = 0
    for item in recent_moves(state):
        try:
            at = float(item.get("at") or 0.0)
        except (TypeError, ValueError):
            continue
        if stamp - at < 86400.0 and stamp - at >= 0.0:
            count += 1
    return count


# ---------------- 规则层：这一轮允不允许拐 ----------------


def steer_permission(
    state: Any,
    *,
    kind: str,
    serious: bool = False,
    turn_failed: bool = False,
    now: float = 0.0,
    enabled: bool = True,
    cooldown_turns: int = STEER_COOLDOWN_TURNS,
    daily_cap: int = STEER_DAILY_CAP,
) -> SteerPermission:
    """规则层的唯一输出：``allowed`` + 被哪一道闸挡住 + 候选。

    顺序即优先级：总开关 → 回合类型 → 正经/叫停 → 本轮失败 → 有没有候选 →
    冷却 → 每日上限。**它不判断"这一轮拐不拐"**，只判断"允许不允许"。
    """

    if not enabled:
        return SteerPermission(False, "disabled", (), "话题层关着")
    if str(kind or "") not in STEER_KINDS:
        return SteerPermission(False, "kind", (), f"这一轮不是闲聊（{kind or '没判'}）")
    if serious:
        return SteerPermission(False, "serious", (), "这一轮要收着")
    if turn_failed:
        return SteerPermission(False, "failed", (), "这一轮本来就有没做成的事")
    pool = candidates(state)
    if not pool:
        return SteerPermission(False, "no_candidates", (), "她自己手头没有能提的事")
    turn = int(getattr(state, "chat_seq", 0) or 0)
    gap = max(0, int(cooldown_turns or 0))
    if gap:
        last = last_move_turn(state)
        if last and turn - last < gap:
            return SteerPermission(False, "cooldown", (), f"才拐过（第 {last} 轮）")
    cap = max(0, int(daily_cap or 0))
    if cap and moved_today(state, now=now) >= cap:
        return SteerPermission(False, "daily_cap", (), f"今天已经拐过 {cap} 次")
    return SteerPermission(True, "", pool, "")


# ---------------- 模型写的那个字段：只做形状收拾 ----------------


def normalize_move(value: Any) -> tuple[str, str]:
    """把模型写的 ``topic`` 收拾成 ``(move, own)``，**认不出的形状一律 ``follow``**。

    旧输出没有这个字段 → ``("follow", "")``，与今天完全一致（不是报错）。
    """

    blank = ("follow", "")
    if value is None or isinstance(value, bool):
        return blank
    if isinstance(value, str):
        body = value.strip().lower()
        if not body:
            return blank
        return (_MOVE_ALIASES.get(body, "follow"), "")
    if not isinstance(value, Mapping):
        return blank
    raw_move = str(value.get("move") or value.get("to") or value.get("do") or "").strip().lower()
    move = _MOVE_ALIASES.get(raw_move, "follow")
    if move == "follow":
        return blank
    own = clip_text(value.get("own") or value.get("text") or value.get("topic") or "")
    if move == "own" and not own:
        # 说"拐走"却没说是哪一条：按接话处理，不给它凭空编一条
        return blank
    return (move, own)


def mentions(text: str, own: str) -> bool:
    """她这句话里有没有**主动提起**那件事（``own`` 是从候选里挑的那一条）。

    不做分词：拿候选里的连续两字去这句话里找，命中口水词（"我们""一下"）不算。
    宁可漏判（那就等 tick 到点提醒一次），也不要错判成"她说过了"而把待兑现吞掉。
    """

    haystack = " ".join(str(text or "").split())
    needle = " ".join(str(own or "").split())
    if not haystack or not needle:
        return False
    if len(needle) >= 4 and needle in haystack:
        return True
    chars = [ch for ch in needle if not ch.isspace()]
    if len(chars) < 2:
        return False
    for index in range(len(chars) - 1):
        gram = chars[index] + chars[index + 1]
        if gram in _STOP_BIGRAMS:
            continue
        if gram in haystack:
            return True
    return False


# ---------------- 待兑现：state 里那一条 ----------------


def pending(state: Any) -> dict[str, Any]:
    """``state.topic_steer`` 的干净副本（旧存档没有这个字段 → 空 dict，不崩）。"""

    raw = getattr(state, "topic_steer", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def write_pending(state: Any, row: Mapping[str, Any] | None) -> None:
    """把待兑现写回 ``state``（``None`` / 空 dict 就是"没有挂着的事"）。"""

    try:
        setattr(state, "topic_steer", dict(row) if row else {})
    except Exception:  # noqa: BLE001 —— 写不上也只能算了，不能挡住这一轮
        pass


def open_pending(
    state: Any,
    *,
    own: str,
    text: str,
    tick: int,
    session: str = "",
    follow_ticks: int = STEER_FOLLOW_TICKS,
) -> dict[str, Any]:
    """开一条新的待兑现：``{own, text, created_tick, due_tick, status}``。

    ``tick`` 是 ``state.world_time``（世界时间），tick 钩子按它算到点。
    """

    created = int(tick or 0)
    window = max(1, int(follow_ticks or 1))
    return {
        "own": clip_text(own),
        "text": clip_text(text, 60),
        "created_tick": created,
        "due_tick": created + window,
        "drop_tick": created + window * 2,
        "status": STATUS_PENDING,
        "session": str(session or ""),
        "move": "own",
        "reminded_tick": 0,
        "followed_tick": 0,
        "follow_reason": "",
    }


def visible_line(row: Mapping[str, Any] | None) -> str:
    """``/vw status`` 那一行用的短句（没有待兑现的事时返回空串）。"""

    data = dict(row or {})
    own = clip_text(data.get("own"), 20)
    if not own:
        return ""
    status = str(data.get("status") or STATUS_PENDING)
    if status == STATUS_PENDING:
        return f"话头：拐到了「{own}」，还没接着说"
    if status == STATUS_FOLLOWED:
        return f"话头：拐到「{own}」之后接上了"
    if status == STATUS_DROPPED:
        return f"话头：拐到「{own}」之后没下文，已经放下"
    return ""


# ---------------- 待兑现的生命周期：纯规则，tick 钩子与回复路径共用 ----------------


def is_pending(row: Mapping[str, Any] | None) -> bool:
    """这条待兑现还挂着吗（``pending`` 才算）。"""

    return str(dict(row or {}).get("status") or "") == STATUS_PENDING


def reminder_text(row: Mapping[str, Any] | None) -> str:
    """到点没接着说时注入的那一句（走 ``extra_notes`` 通道，只说一次）。"""

    own = clip_text(dict(row or {}).get("own"), 30)
    if not own:
        return ""
    return (
        f"你刚才把话拐到「{own}」上，还没说下去。这一轮找机会真的说一句"
        "（或者干脆放下，别再提一句就跑）。"
    )


def mark_reminded(row: Mapping[str, Any], *, tick: int) -> dict[str, Any]:
    """到点没再提起 → 记下"提醒过"，并把要注入的那句话挂进 ``remind``。

    状态同时转 ``followed``（``follow_reason='reminded'``）：这句提醒**只注入一次**，
    再往后就是"她到底说没说"的问题——由 :func:`tick_verdict` 判 ``dropped``。
    """

    data = dict(row or {})
    stamp = int(tick or 0)
    data["status"] = STATUS_FOLLOWED
    data["follow_reason"] = "reminded"
    data["reminded_tick"] = stamp
    data["remind"] = reminder_text(data)
    return data


def mark_followed(
    row: Mapping[str, Any], *, tick: int, reason: str = "mentioned"
) -> dict[str, Any]:
    """她真的接着说下去了（或者这一轮又用 ``own`` 提了同一件）：转 ``followed``。

    顺手清掉 ``remind``：接上了就不该再提醒，也不该被后面的窗口判成放弃。
    """

    data = dict(row or {})
    data["status"] = STATUS_FOLLOWED
    data["follow_reason"] = str(reason or "mentioned")
    data["followed_tick"] = int(tick or 0)
    data["remind"] = ""
    return data


def mark_dropped(row: Mapping[str, Any], *, tick: int) -> dict[str, Any]:
    """提醒过又过了一个窗口还是没提 → 明确放弃（这条路径必须真的存在）。"""

    data = dict(row or {})
    data["status"] = STATUS_DROPPED
    data["dropped_tick"] = int(tick or 0)
    data["remind"] = ""
    return data


def take_reminder(row: Mapping[str, Any] | None) -> tuple[dict[str, Any], str]:
    """取出待注入的那句提醒并清空：返回 ``(新的 row, 要注入的文本)``。

    同一句只会被取走一次——取走之后 ``remind`` 就空了，下一轮提示词不再重复。
    """

    data = dict(row or {})
    note = str(data.get("remind") or "")
    if note:
        data["remind"] = ""
    return data, note


def tick_verdict(
    row: Mapping[str, Any] | None, *, tick: int, drop_ticks: int = STEER_DROP_TICKS
) -> str:
    """这一 tick 该对这条待兑现做什么：``remind`` / ``drop`` / 空串（不动）。

    顺序与语义：
    - 还 ``pending`` 且到了 ``due_tick`` → ``remind``（注入一次）；
    - 已经 ``remind`` 过（``follow_reason='reminded'``）且从提醒起又过了
      ``drop_ticks`` 个 tick → ``drop``（明确放弃，写日志）；
    - 其余（她自己接上了、已经放弃）→ 不动。
    """

    data = dict(row or {})
    status = str(data.get("status") or "")
    stamp = int(tick or 0)
    if status == STATUS_PENDING:
        due = int(data.get("due_tick") or 0)
        if due and stamp >= due:
            return "remind"
        return ""
    if status == STATUS_FOLLOWED and str(data.get("follow_reason") or "") == "reminded":
        window = max(1, int(drop_ticks or 1))
        reminded = int(data.get("reminded_tick") or 0)
        if reminded and stamp - reminded >= window:
            return "drop"
    return ""


# ---------------- 提示词里那一小段：只在允许时出现 ----------------


def steer_lead(perm: SteerPermission | None) -> str:
    """允许拐走时给模型的那一段（**不允许时返回空串**：提示词一个字都不加）。

    和 ``core/response_variation.py`` 的 ``tease_lead`` 一个口径：说明是可选的、
    给四个走法、给候选原话、并明确"拐走之后欠一个下文"。
    """

    if perm is None or not perm.allowed or not perm.candidates:
        return ""
    lines = "\n".join(f"- {item.text}" for item in perm.candidates)
    return (
        "这一轮你们在闲聊：你可以照常接他的话，也可以**把话拐到你自己的一件具体的事上**"
        "（可选，不是任务）：\n"
        f"{lines}\n"
        "想拐就在 JSON 里写 topic: "
        '{"move": "own", "own": "上面某一条的原话"}；'
        '也可以写 "probe"（追问他一句细节）、'
        '"switch"（这轮不想接，轻巧地换个话题）、'
        '"follow"（照常接，也是默认）。\n'
        "拐走之后你欠这件事一个下文：下一两轮要真的接着说下去，或者干脆放下——"
        "提一句就没下文，比不提更敷衍。\n"
        "他是在求助、说急事、或者正经交代事情的时候，别拐。"
    )


__all__ = [
    "MAX_CANDIDATES",
    "STATUS_DROPPED",
    "STATUS_FOLLOWED",
    "STATUS_PENDING",
    "STEER_COOLDOWN_TURNS",
    "STEER_DAILY_CAP",
    "STEER_DROP_TICKS",
    "STEER_FOLLOW_TICKS",
    "STEER_KINDS",
    "STEER_MOVES_KEEP",
    "TOPIC_MOVES",
    "SteerPermission",
    "TopicCandidate",
    "candidates",
    "clip_text",
    "is_pending",
    "last_move_turn",
    "mark_dropped",
    "mark_followed",
    "mark_reminded",
    "mentions",
    "moved_today",
    "note_move",
    "normalize_move",
    "open_pending",
    "pending",
    "recent_moves",
    "reminder_text",
    "steer_lead",
    "steer_permission",
    "take_reminder",
    "tick_verdict",
    "visible_line",
    "write_pending",
]
