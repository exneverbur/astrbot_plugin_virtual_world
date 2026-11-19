"""阶段F：增量睡眠整理——输入窗口、作业记录与提案校验。

计划 §10 把"睡觉时整理"重做成四件事：

* **F1 输入游标**：素材不再只看"最近这段聊天窗口"，而是按 DB 里 source_events 的
  **登记序号**（sqlite ``rowid``，单调、不受时钟回拨影响）从上次成功的位置往后取；
  读的时候先冻结一个 ``to_seq``，整理期间新到的事件留到下一次作业。
* **F2 作业生命周期**：一次作业 = 短锁建 job + 快照 → **锁外**调模型 → 校验 →
  一个短事务提交 → 成功才推进游标。重启后按持久化的作业续跑，不重置次数；
  失败不推进游标、不占已完成次数，退避后重试。
* **F3 提案校验**：模型给的东西一律当**未验证提案**：数量、范围、参与者、记忆 id、
  可核对的证据与来源事件都要过一遍；宁可不记，也不写半个错的人。
* **F4 梦**：单独一类素材（``dream``），可以关、默认低优先级，不参与事实与关系结算。

本模块只放**纯逻辑**（记录、窗口、校验、时间归一），IO 与调用顺序在引擎侧。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

# ---------------- 模式开关（计划 §13.1） ----------------

MODE_INCREMENTAL = "incremental_jobs"
"""默认：按登记序号做增量作业整理（阶段F）。"""

MODE_LEGACY = "legacy"
"""旧路径：一次睡觉把整段聊天窗口喂给模型（阶段 A–E 的行为）。"""

_LEGACY_VALUES = frozenset(
    {
        "",
        "legacy",
        "old",
        "off",
        "false",
        "0",
        "none",
        "chat",
        "chat_window",
        "window",
        "inline",
        "sleep",
    }
)


def normalize_mode(value: Any) -> str:
    """把配置里的整理模式归一成 :data:`MODE_INCREMENTAL` / :data:`MODE_LEGACY`。

    认不出的值一律当**增量**（新装的世界拿到的就是新行为）；要退回旧路径，
    必须在配置里明确写 ``legacy``。
    """

    text = str(value or "").strip().lower()
    if text in _LEGACY_VALUES:
        return MODE_LEGACY
    return MODE_INCREMENTAL


def incremental_enabled(value: Any) -> bool:
    """这个配置值是不是走增量作业路径。"""

    return normalize_mode(value) == MODE_INCREMENTAL


# ---------------- 数量与数值边界（F3） ----------------

MAX_WINDOW = 200
"""一次作业最多吃多少条来源事件（剩下的留给下一批）。"""

MAX_ATTEMPTS = 3
"""同一个作业最多试几次；超过就彻底失败、不再自动重试。"""

RETRY_BASE_SECONDS = 60.0
RETRY_CAP_SECONDS = 3600.0

MAX_MEMORIES = 12
MAX_MERGES = 6
MAX_FACTS = 8
MAX_BONDS = 4
MAX_DIGESTS = 8
MAX_AFFINITY = 6
MAX_FORGET = 6
MAX_NOTES = 8
MAX_TALK = 3
"""一次整理最多抽几条谈资（T8-2 口径：1–3 条）。"""
MAX_SUPERSEDE = 8
"""一次整理最多作废几条旧记忆（同主题覆盖；宁少勿滥）。"""
MAX_KEYWORDS = 6
TEXT_LIMIT = 200

SELF_IDS = frozenset({"__self__", "self", "persona", "assistant", "bot"})
"""伪用户：她自己的发言。这些都**不是**可以写进画像的人（计划 §10 F3）。"""


# ---------------- 作业与分集记录（F2） ----------------

JOB_PENDING = "pending"
JOB_CLAIMED = "claimed"
JOB_DONE = "done"
JOB_NO_CHANGE = "success_no_change"
JOB_FAILED = "failed"

JOB_OPEN_STATUSES = (JOB_PENDING, JOB_CLAIMED, JOB_FAILED)
JOB_SETTLED_STATUSES = (JOB_DONE, JOB_NO_CHANGE)


def _now_text(now: float) -> float:
    try:
        return float(now)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class JobRecord:
    """一次整理作业：吃 ``(from_seq, to_seq]`` 这一段素材，成功才把游标推到 ``next_seq``。"""

    id: str
    session_id: str = ""
    mode: str = "full"
    episode_id: str = ""
    from_seq: int = 0
    to_seq: int = 0
    next_seq: int = 0
    status: str = JOB_PENDING
    attempts: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    claimed_at: float = 0.0
    done_at: float = 0.0
    error: str = ""
    note: str = ""
    counts: dict[str, Any] = field(default_factory=dict)
    event_ids: list[str] = field(default_factory=list)
    item_count: int = 0

    @property
    def open(self) -> bool:
        """还没了结（含失败待重试）的作业。"""

        return self.status in JOB_OPEN_STATUSES

    @property
    def settled(self) -> bool:
        return self.status in JOB_SETTLED_STATUSES

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "mode": self.mode,
            "episode_id": self.episode_id,
            "from_seq": int(self.from_seq),
            "to_seq": int(self.to_seq),
            "next_seq": int(self.next_seq),
            "status": self.status,
            "attempts": int(self.attempts),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "claimed_at": float(self.claimed_at),
            "done_at": float(self.done_at),
            "error": self.error,
            "note": self.note,
            "counts": dict(self.counts),
            "event_ids": list(self.event_ids),
            "item_count": int(self.item_count),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "JobRecord | None":
        if not isinstance(raw, Mapping):
            return None
        job_id = str(raw.get("id") or "")
        if not job_id:
            return None
        counts = raw.get("counts")
        event_ids = raw.get("event_ids")
        return cls(
            id=job_id,
            session_id=str(raw.get("session_id") or ""),
            mode=str(raw.get("mode") or "full"),
            episode_id=str(raw.get("episode_id") or ""),
            from_seq=_int(raw.get("from_seq"), 0),
            to_seq=_int(raw.get("to_seq"), 0),
            next_seq=_int(raw.get("next_seq"), 0),
            status=str(raw.get("status") or JOB_PENDING),
            attempts=_int(raw.get("attempts"), 0),
            created_at=_float(raw.get("created_at"), 0.0),
            updated_at=_float(raw.get("updated_at"), 0.0),
            claimed_at=_float(raw.get("claimed_at"), 0.0),
            done_at=_float(raw.get("done_at"), 0.0),
            error=str(raw.get("error") or ""),
            note=str(raw.get("note") or ""),
            counts=dict(counts) if isinstance(counts, Mapping) else {},
            event_ids=[str(item) for item in event_ids] if isinstance(event_ids, list) else [],
            item_count=_int(raw.get("item_count"), 0),
        )

    def claim(self, now: float) -> "JobRecord":
        """领取（短锁里做）：记下次数与时间，素材先留着，失败还能重取。"""

        now = _now_text(now)
        self.status = JOB_CLAIMED
        self.attempts += 1
        if not self.created_at:
            self.created_at = now
        self.claimed_at = now
        self.updated_at = now
        return self

    def finish(self, now: float, *, counts: Mapping[str, Any] | None = None, note: str = "") -> "JobRecord":
        """成功提交：推进游标、清掉错误。"""

        now = _now_text(now)
        self.status = JOB_DONE
        self.done_at = now
        self.updated_at = now
        self.error = ""
        self.note = str(note or "")
        if counts is not None:
            self.counts = dict(counts)
        return self

    def no_change(self, now: float, *, note: str = "", counts: Mapping[str, Any] | None = None) -> "JobRecord":
        """空对象：也算成功（游标要推进，否则每轮都在嚼同一段没内容的素材）。"""

        now = _now_text(now)
        self.status = JOB_NO_CHANGE
        self.done_at = now
        self.updated_at = now
        self.error = ""
        self.note = str(note or "")
        if counts is not None:
            self.counts = dict(counts)
        return self

    def fail(self, now: float, error: str) -> "JobRecord":
        """失败：不推进游标、不占已完成次数，只记错误等退避重试。"""

        now = _now_text(now)
        self.status = JOB_FAILED
        self.updated_at = now
        self.error = " ".join(str(error or "").split())[:300]
        return self

    @property
    def exhausted(self) -> bool:
        """试够次数了（不再自动重试，等人工或下一次睡眠重新建作业）。"""

        return self.attempts >= MAX_ATTEMPTS


@dataclass
class EpisodeRecord:
    """一次睡眠（或小睡）：作业挂在它下面，重启后照旧续跑。"""

    id: str
    session_id: str = ""
    node_id: str = ""
    kind: str = "sleep"
    started_at: float = 0.0
    closed_at: float = 0.0
    passes: int = 0
    jobs: list[str] = field(default_factory=list)
    cursor: int = 0
    dream_job_id: str = ""

    @property
    def open(self) -> bool:
        return not self.closed_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "node_id": self.node_id,
            "kind": self.kind,
            "started_at": float(self.started_at),
            "closed_at": float(self.closed_at),
            "passes": int(self.passes),
            "jobs": list(self.jobs),
            "cursor": int(self.cursor),
            "dream_job_id": self.dream_job_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "EpisodeRecord | None":
        if not isinstance(raw, Mapping):
            return None
        episode_id = str(raw.get("id") or "")
        if not episode_id:
            return None
        jobs = raw.get("jobs")
        return cls(
            id=episode_id,
            session_id=str(raw.get("session_id") or ""),
            node_id=str(raw.get("node_id") or ""),
            kind=str(raw.get("kind") or "sleep"),
            started_at=_float(raw.get("started_at"), 0.0),
            closed_at=_float(raw.get("closed_at"), 0.0),
            passes=_int(raw.get("passes"), 0),
            jobs=[str(item) for item in jobs] if isinstance(jobs, list) else [],
            cursor=_int(raw.get("cursor"), 0),
            dream_job_id=str(raw.get("dream_job_id") or ""),
        )


def job_id_for(session_id: str, *, from_seq: int, to_seq: int, mode: str = "full") -> str:
    """同一段素材只有一个作业 id（重跑不会变成第二个作业）。"""

    raw = f"{str(session_id or '')}|{str(mode or 'full')}|{_int(from_seq, 0)}|{_int(to_seq, 0)}"
    return "job-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def operation_key(job_id: str, kind: str, index: int) -> str:
    """幂等键：同一个作业里的同一条增量（记忆 / 好感）只该提交一次。"""

    return f"{str(job_id or '')}#{str(kind or 'item')}:{_int(index, 0)}"


def retry_delay(
    attempts: int, *, base: float = RETRY_BASE_SECONDS, cap: float = RETRY_CAP_SECONDS
) -> float:
    """退避时长：试过 1 次等 60s、2 次等 120s、3 次等 240s……封顶一小时。"""

    try:
        count = max(0, int(attempts) - 1)
    except (TypeError, ValueError):
        count = 0
    try:
        span = float(base) * (2 ** count)
    except OverflowError:
        span = float(cap)
    return float(min(float(cap), span))


def retry_due(job: JobRecord | Mapping[str, Any] | None, now: float) -> bool:
    """这个作业现在可以再试一次吗（失败后要等退避）。"""

    if job is None:
        return False
    record = job if isinstance(job, JobRecord) else JobRecord.from_dict(job)
    if record is None or record.settled:
        return False
    if record.attempts >= MAX_ATTEMPTS:
        return False
    moment = _now_text(now)
    return moment >= record.updated_at + retry_delay(record.attempts)


# ---------------- 输入窗口（F1） ----------------


@dataclass
class WindowPlan:
    """这一批要交给模型的素材，以及"成功后游标推到哪"。"""

    session_id: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    from_seq: int = 0
    to_seq: int = 0
    next_seq: int = 0

    @property
    def empty(self) -> bool:
        return not self.rows

    @property
    def sequences(self) -> list[int]:
        return [_int(row.get("seq"), 0) for row in self.rows]

    @property
    def event_ids(self) -> list[str]:
        ids: list[str] = []
        for row in self.rows:
            event_id = str(row.get("event_id") or "")
            if event_id and event_id not in ids:
                ids.append(event_id)
        return ids


def plan_window(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    session_id: str = "",
    from_seq: int = 0,
    to_seq: int = 0,
    limit: int = MAX_WINDOW,
) -> WindowPlan:
    """把"从 ``from_seq`` 之后到 ``to_seq``"的一批事件裁成一次作业的量。

    一次只吃 :data:`MAX_WINDOW` 条：素材再多也分批，免得一次提示词塞进一整天的群聊。
    这一批里没有任何行时，``next_seq`` 直接取冻结的 ``to_seq``——空区间也要把游标
    推过去，否则每一轮都在读同一段空素材。
    """

    try:
        size = max(1, int(limit))
    except (TypeError, ValueError):
        size = MAX_WINDOW
    selected = [dict(row) for row in (rows or [])][:size]
    seqs = [_int(row.get("seq"), 0) for row in selected if _int(row.get("seq"), 0) > 0]
    frozen = _int(to_seq, 0)
    next_seq = max(seqs) if seqs else max(_int(from_seq, 0), frozen)
    return WindowPlan(
        session_id=str(session_id or ""),
        rows=selected,
        from_seq=_int(from_seq, 0),
        to_seq=frozen or next_seq,
        next_seq=next_seq,
    )


def source_event_to_chat_row(event: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """来源事件 → 整理器认的聊天记录行（``text`` / ``name`` / ``user_id`` / ``at`` / ``origin``）。

    这条桥是 F1 的关键：素材从 DB 来，但 ``Consolidator.chat_lines`` 只认聊天记录行
    的形状，所以必须如实翻译，**不按正文去重**。
    """

    if not isinstance(event, Mapping):
        return None
    payload = event.get("payload")
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    text = ""
    for key in ("text", "message", "content", "say"):
        text = _text(payload.get(key), TEXT_LIMIT)
        if text:
            break
    sender = str(event.get("sender_id") or payload.get("sender_id") or "").strip()
    name = ""
    for key in ("name", "sender_name", "nickname", "user_name"):
        name = _text(payload.get(key), 40)
        if name:
            break
    is_self = bool(payload.get("is_self")) or sender in SELF_IDS
    origin = str(event.get("origin_session_id") or event.get("scope_id") or "").strip()
    message_id = str(event.get("event_id") or "").strip()
    if not text and not message_id:
        return None
    return {
        "text": text,
        "name": name or ("她自己" if is_self else sender or "有人"),
        "user_id": "" if is_self else sender,
        "at": _float(event.get("occurred_at"), 0.0),
        "origin": origin,
        "is_self": is_self,
        "message_id": message_id,
        "source_event_id": message_id,
        "seq": _int(event.get("seq"), 0),
    }


def window_chat_rows(rows: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """一批来源事件 → 聊天记录行（顺序照旧：登记顺序）。"""

    result: list[dict[str, Any]] = []
    for row in rows or []:
        item = source_event_to_chat_row(row)
        if item is not None:
            result.append(item)
    return result


# ---------------- 时间相对词（F3：按发生时转成具体时间，且保留原文） ----------------

_PHRASE_SECONDS = {
    "上周": -7 * 86400.0,
    "上个星期": -7 * 86400.0,
    "上上星期": -14 * 86400.0,
    "上个月": -30 * 86400.0,
    "去年": -365 * 86400.0,
    "前年": -730 * 86400.0,
    "这周": 0.0,
    "本周": 0.0,
    "这个月": 0.0,
    "本月": 0.0,
    "今年": 0.0,
}
"""整词相对说法：直接给一个秒数偏移（比逐字解析稳）。"""

_NOW_WORDS = ("刚刚", "刚才", "现在", "这会儿", "此时", "眼下")
"""指着"就是此刻"的说法：按发生的时刻写全时间。"""

_CN_NUMBERS = {
    "零": 0,
    "一": 1,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "半": 0.5,
}


def _amount(text: str) -> float:
    """``3`` / ``三`` / ``十几`` 之类的数量词 → 数字（认不出来给 0）。"""

    body = str(text or "").strip()
    if not body:
        return 0.0
    try:
        return float(body)
    except ValueError:
        pass
    if body == "十几":
        return 15.0
    total = 0.0
    for char in body:
        value = _CN_NUMBERS.get(char)
        if value is None:
            return 0.0
        total = total * 10 + value if total else value
    return total


_DAY_WORDS = {
    "大前天": -3,
    "前天": -2,
    "昨天": -1,
    "昨日": -1,
    "今天": 0,
    "今日": 0,
    "今晚": 0,
    "今早": 0,
    "今儿": 0,
    "明天": 1,
    "明日": 1,
    "明晚": 1,
    "后天": 2,
    "大后天": 3,
}
_PERIOD_HOURS = {
    "凌晨": 3,
    "清晨": 6,
    "早上": 8,
    "早晨": 8,
    "上午": 9,
    "中午": 12,
    "下午": 15,
    "傍晚": 18,
    "晚上": 20,
    "夜里": 22,
    "深夜": 23,
}
_UNIT_SECONDS = {
    "秒": 1.0,
    "分钟": 60.0,
    "分": 60.0,
    "小时": 3600.0,
    "天": 86400.0,
    "周": 7 * 86400.0,
    "星期": 7 * 86400.0,
    "个月": 30 * 86400.0,
    "月": 30 * 86400.0,
    "年": 365 * 86400.0,
}
_REL_PATTERN = re.compile(
    r"([0-9一二三四五六七八九十两半]+)\s*(个月|分钟|小时|星期|秒|分|天|周|月|年)\s*(前|后|以前|之后|以后|之前)"
)
_CLOCK_PATTERN = re.compile(r"(\d{1,2})\s*[:：点]\s*(\d{1,2})?")
_CN_DATE_PATTERN = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
_ISO_DATE_PATTERN = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})")


@dataclass
class NormalizedTime:
    """相对时间词归一的结果：绝对说法 + **原文照留**。"""

    text: str = ""
    original: str = ""
    shift_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.text)


def normalize_time_phrase(phrase: Any, *, at: float = 0.0, now: float = 0.0) -> NormalizedTime:
    """把"昨天下午""三天前"这类相对说法按**发生时**换算成具体时间。

    ``at`` 是这句话发生的时刻（来源事件的 ``occurred_at``），缺了就用 ``now``。
    认不出来就只把原文留下（``text`` 为空），绝不自作主张编一个时间。
    """

    original = _text(phrase, 40)
    if not original:
        return NormalizedTime()
    base = _float(at, 0.0) or _float(now, 0.0)
    if base <= 0:
        return NormalizedTime(original=original)
    moment = datetime.fromtimestamp(base)
    shift = 0.0
    date_known = False
    time_known = False
    for word, delta in _PHRASE_SECONDS.items():
        if word in original:
            moment += timedelta(seconds=delta)
            shift += delta
            date_known = True
            break
    if any(word in original for word in _NOW_WORDS):
        # "刚刚""现在"指的是此刻：时间也要写出来（不是只写个日期）
        date_known = True
        time_known = True
    for word, days in _DAY_WORDS.items():
        if word in original:
            moment += timedelta(days=days)
            shift += days * 86400.0
            date_known = True
            break
    match = _REL_PATTERN.search(original)
    if match:
        amount = _amount(match.group(1))
        unit = _UNIT_SECONDS.get(match.group(2), 0.0)
        direction = 1.0 if match.group(3) in ("后", "之后", "以后") else -1.0
        delta = amount * unit * direction
        moment += timedelta(seconds=delta)
        shift += delta
        date_known = True
    for word, hour in _PERIOD_HOURS.items():
        if word in original:
            moment = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
            time_known = True
            break
    clock = _CLOCK_PATTERN.search(original)
    if clock:
        hour = min(23, max(0, int(clock.group(1))))
        minute = min(59, max(0, int(clock.group(2) or 0)))
        moment = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        time_known = True
    iso = _ISO_DATE_PATTERN.search(original)
    if iso:
        year, month, day = (int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        try:
            moment = moment.replace(year=year, month=month, day=day)
            date_known = True
        except ValueError:
            pass
    else:
        cn_date = _CN_DATE_PATTERN.search(original)
        if cn_date:
            month, day = int(cn_date.group(1)), int(cn_date.group(2))
            try:
                moment = moment.replace(month=month, day=day)
                date_known = True
            except ValueError:
                pass
    if not (date_known or time_known):
        return NormalizedTime(original=original)
    if not time_known:
        return NormalizedTime(
            text=moment.strftime("%m-%d"), original=original, shift_seconds=shift
        )
    if moment.year != datetime.fromtimestamp(base).year:
        return NormalizedTime(
            text=moment.strftime("%Y-%m-%d %H:%M"), original=original, shift_seconds=shift
        )
    return NormalizedTime(
        text=moment.strftime("%m-%d %H:%M"), original=original, shift_seconds=shift
    )


# ---------------- 提案校验（F3） ----------------


@dataclass
class ValidationResult:
    """校验后的提案：能用的留下，丢掉的**写明原因**（进日志，不静默吞）。"""

    payload: dict[str, Any] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """这份提案会不会真的改动世界（空 = ``success_no_change``）。"""

        for value in self.payload.values():
            if isinstance(value, Mapping):
                if value:
                    return True
            elif isinstance(value, (list, tuple, set)):
                if value:
                    return True
        return False

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for key, value in self.payload.items():
            if isinstance(value, Mapping):
                counts[key] = len(value)
            elif isinstance(value, (list, tuple, set)):
                counts[key] = len(value)
        return counts


def _text(value: Any, limit: int = TEXT_LIMIT) -> str:
    return " ".join(str(value or "").split())[: max(1, int(limit))]


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _clamp(value: Any, low: float, high: float, default: float = 0.0) -> float:
    number = _float(value, default)
    return max(float(low), min(float(high), number))


def _rows(value: Any) -> list[dict[str, Any]]:
    """dict / list 都收：模型偶尔给一个对象而不是数组。"""

    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, (list, tuple)):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def clean_user_id(value: Any) -> str:
    """把"名字（QQ 123）"这类写法里的号抠出来，方便和通讯录对齐。"""

    text = str(value or "").strip()
    if not text:
        return ""
    for left, right in (("（", "）"), ("(", ")")):
        if left in text and right in text:
            inside = text.split(left, 1)[1].split(right, 1)[0].strip()
            if inside:
                text = inside
            break
    text = text.lstrip("@").strip()
    text = re.sub(r"^(?:qq|QQ|Qq|qQ|企鹅)\s*[:：]?\s*", "", text).strip()
    text = text.lstrip("#").strip()
    if text in SELF_IDS:
        return ""
    return text[:64]


def window_aliases(rows: Iterable[Any] | None) -> dict[str, str]:
    """这批素材里"名字 → QQ 号"的对照表。

    模型经常照着聊天记录里的昵称写，而不是写号码；只认**这批素材里真的出现过**的
    昵称，既容错，又不给它凭空造人的机会（计划 §10 F3）。
    """

    aliases: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        item: Mapping[str, Any] = row
        if str(row.get("event_id") or ""):
            converted = source_event_to_chat_row(row)
            if converted is not None:
                item = converted
        uid = clean_user_id(item.get("user_id") or item.get("sender_id"))
        if not uid:
            continue
        for key in ("name", "sender_name", "nickname", "user_name"):
            name = _text(item.get(key), 40)
            if name and name not in aliases:
                aliases[name] = uid
    return aliases


def _resolve_id(value: Any, known: set[str], aliases: Mapping[str, str] | None = None) -> str:
    """人物标识 → 允许写进画像的 QQ 号（认不出或这轮没出现就返回空）。"""

    uid = clean_user_id(value)
    if uid and uid in known:
        return uid
    name = str(value or "").strip().lstrip("@")
    if aliases:
        mapped = str(aliases.get(name) or "")
        if mapped and mapped in known:
            return mapped
    return ""


def _participants(
    value: Any, known: set[str], aliases: Mapping[str, str] | None = None
) -> list[str]:
    """参与者只留**真的出现过**的人（排除 ``__self__`` 伪用户）。"""

    result: list[str] = []
    for item in _as_list(value):
        uid = _resolve_id(item, known, aliases)
        if uid and uid not in result:
            result.append(uid)
    return result


def _keywords(value: Any) -> list[str]:
    if isinstance(value, str):
        raw: list[Any] = [item for item in re.split(r"[,，、;；\s]+", value)]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []
    result: list[str] = []
    for item in raw:
        word = _text(item, 24)
        if word and word not in result:
            result.append(word)
    return result[:MAX_KEYWORDS]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, Mapping):
        return [value]
    return [value]


def _evidence_text(value: Any) -> str:
    """证据必须是**能核对的原话**：dict 里取 text/quote，字符串直接用。"""

    if isinstance(value, Mapping):
        for key in ("text", "quote", "say", "snippet"):
            text = _text(value.get(key), TEXT_LIMIT)
            if text:
                return text
        return ""
    return _text(value, TEXT_LIMIT)


def _match_source(
    raw_ids: Any,
    evidence: str,
    material: list[tuple[str, str, float]],
) -> tuple[list[str], float]:
    """证据落在哪条来源事件上：先信模型给的 id（必须在窗口内），再**照原话核对**。

    返回 ``(命中的事件 id, 那条事件的时刻)``。一个都核不上就返回空——按 F3，
    这时候宁可丢掉这条事实，也不写一条不知道从哪来的记录。
    """

    allowed = {event_id for event_id, _text_body, _at in material if event_id}
    hits: list[str] = []
    stamps: list[float] = []
    for item in _as_list(raw_ids):
        event_id = str(item or "").strip()
        if event_id and event_id in allowed and event_id not in hits:
            hits.append(event_id)
            for known_id, _body, at in material:
                if known_id == event_id and at:
                    stamps.append(at)
                    break
    if hits:
        return hits, (max(stamps) if stamps else 0.0)
    quote = _evidence_text(evidence)
    if not quote:
        return [], 0.0
    for event_id, body, at in material:
        if not body:
            continue
        if quote in body or (len(quote) >= 4 and quote[:12] in body):
            return ([event_id] if event_id else []), at
    return [], 0.0


def validate_proposal(
    payload: Any,
    *,
    known: Iterable[Any] = (),
    allowed_memory_ids: Iterable[Any] = (),
    material: Iterable[Any] = (),
    aliases: Mapping[str, str] | None = None,
    session_id: str = "",
    at: float = 0.0,
    now: float = 0.0,
    max_items: int = MAX_MEMORIES,
) -> ValidationResult:
    """把模型给的提案过一遍（F3）：越界的、编出来的、没证据的一律丢掉并记账。

    ``known``：允许写进画像的 QQ 号（见 ``Consolidator.known_ids``）。
    ``allowed_memory_ids``：这一轮能合并 / 折叠的记忆 id（当前范围内查出来的）。
    ``aliases``：这批素材里"名字 → QQ 号"的对照（见 :func:`window_aliases`），
    模型写昵称时按它换算。
    ``material``：这批素材，元素可以是来源事件 dict（带 ``event_id`` / ``payload`` /
    ``occurred_at``）或 ``(event_id, text, occurred_at)`` 三元组——facts / relations
    的 ``source_event_ids`` 与证据原话都要在这里核得上。
    """

    result = ValidationResult()
    if not isinstance(payload, Mapping):
        result.skipped.append("提案不是一个对象")
        return result
    ids = {clean_user_id(item) for item in _as_list(known)}
    ids = {item for item in ids if item}
    allowed = {str(item).strip() for item in _as_list(allowed_memory_ids) if str(item).strip()}
    rows = _material_rows(material)
    moment = _float(at, 0.0) or _float(now, 0.0)

    known_keys = {
        "memories",
        "merge",
        "facts",
        "relations",
        "digests",
        "affinity",
        "forget",
        "notes",
        "talk",
        "dream",
        "supersede",
    }
    for key in payload:
        if str(key) not in known_keys:
            result.skipped.append(f"不认识的字段 {key}")

    # --- 记忆 ---
    memories: list[dict[str, Any]] = []
    for index, item in enumerate(_rows(payload.get("memories"))):
        if len(memories) >= max(1, int(max_items)):
            result.skipped.append("记忆条数超过上限")
            break
        body = _text(item.get("text") or item.get("content") or item.get("memory"))
        if not body:
            result.skipped.append("记忆缺内容")
            continue
        people = _participants(
            item.get("participants") or item.get("related_users"), ids, aliases
        )
        memories.append(
            {
                "text": body,
                "node_id": _text(item.get("node_id"), 64),
                # 键名对齐 Consolidator._write_memory（它读的是 ``type``）
                "type": _text(item.get("memory_type") or item.get("type"), 24),
                "emotion": _text(item.get("emotion"), 24),
                "weight": _clamp(item.get("weight"), 0.05, 1.0, 0.55),
                "participants": people,
                "keywords": _keywords(item.get("keywords")),
                "context": _text(item.get("context")),
                "pinned": bool(item.get("pinned")),
                "review_in_days": _clamp(item.get("review_in_days"), 0.0, 60.0, 0.0),
            }
        )
    if memories:
        result.payload["memories"] = memories

    # --- 合并 ---
    merges: list[dict[str, Any]] = []
    merged_away: set[str] = set()
    for item in _rows(payload.get("merge")):
        if len(merges) >= MAX_MERGES:
            result.skipped.append("合并条数超过上限")
            break
        keep = str(item.get("keep") or item.get("keep_id") or item.get("id") or "").strip()
        drops = [str(one).strip() for one in _as_list(item.get("drops") or item.get("drop"))]
        drops = [one for one in drops if one]
        if not keep or keep not in allowed:
            result.skipped.append("合并的保留项不在本轮记忆里")
            continue
        if keep in drops:
            result.skipped.append("合并不能把自己折进去")
            continue
        keep_drops = [one for one in drops if one in allowed and one != keep]
        if not keep_drops:
            result.skipped.append("合并没有可折叠的项")
            continue
        keep_id = _int(keep)
        if keep_id <= 0:
            result.skipped.append("合并的保留项不是一个有效的记忆号")
            continue
        merged_away.update(keep_drops)
        # 键名对齐 Consolidator._apply_full（它读 ``keep_id`` / ``drop_ids``，都是整数）
        row: dict[str, Any] = {
            "keep_id": keep_id,
            "drop_ids": [_int(one) for one in keep_drops if _int(one) > 0],
        }
        text = _text(item.get("text") or item.get("gist"))
        if text:
            row["text"] = text
        merges.append(row)
    if merges:
        result.payload["merge"] = merges

    # --- 事实（用户事实与她的解释分字段） ---
    facts: list[dict[str, Any]] = []
    for item in _rows(payload.get("facts")):
        if len(facts) >= MAX_FACTS:
            result.skipped.append("事实条数超过上限")
            break
        uid = _resolve_id(item.get("user_id") or item.get("uid") or item.get("who"), ids, aliases)
        if not uid:
            result.skipped.append("事实里的人这轮没出现过")
            continue
        body = _text(item.get("text") or item.get("fact"))
        if not body:
            result.skipped.append("事实缺内容")
            continue
        evidence = _evidence_text(item.get("evidence"))
        hits, hit_at = _match_source(item.get("source_event_ids"), evidence, rows)
        if not evidence or not hits:
            result.skipped.append("事实没有可核对的来源")
            continue
        when = normalize_time_phrase(
            item.get("time_phrase") or item.get("when"), at=hit_at or moment, now=now
        )
        row = {
            "user_id": uid,
            "text": body,
            "kind": _text(item.get("kind"), 24) or "other",
            "evidence": evidence,
            "source_event_ids": hits,
            "confidence": _clamp(item.get("confidence"), 0.0, 1.0, 0.6),
        }
        interpretation = _text(item.get("interpretation") or item.get("context"))
        if when.ok:
            row["time_text"] = when.text
            row["time_original"] = when.original
        # 时间按发生时换成具体说法（**原文照留**），和她的解释一起放进 context：
        # 下游 note_fact 的字段是"他的原话 + 她的解释"，没有单独的时间字段（F3）。
        if when.ok:
            note = f"（他说的是「{when.original}」，也就是 {when.text}）"
            interpretation = f"{interpretation}{note}" if interpretation else note
        if interpretation:
            row["context"] = interpretation
        facts.append(row)
    if facts:
        result.payload["facts"] = facts

    # --- 关系 ---
    relations: list[dict[str, Any]] = []
    for item in _rows(payload.get("relations")):
        if len(relations) >= MAX_BONDS:
            result.skipped.append("关系条数超过上限")
            break
        uid = _resolve_id(item.get("user_id") or item.get("uid") or item.get("who"), ids, aliases)
        if not uid:
            result.skipped.append("关系里的人这轮没出现过")
            continue
        bond = _text(item.get("type") or item.get("bond"), 24)
        evidence = _evidence_text(item.get("evidence"))
        hits, _hit_at = _match_source(item.get("source_event_ids"), evidence, rows)
        if not bond or not evidence or not hits:
            result.skipped.append("关系没有可核对的来源")
            continue
        relations.append(
            {
                "user_id": uid,
                "type": bond,
                "evidence": evidence,
                "source_event_ids": hits,
                "confidence": _clamp(item.get("confidence"), 0.0, 1.0, 0.6),
            }
        )
    if relations:
        result.payload["relations"] = relations

    # --- 印象 ---
    digests: dict[str, str] = {}
    raw_digests = payload.get("digests")
    if isinstance(raw_digests, Mapping):
        for key, value in raw_digests.items():
            if len(digests) >= MAX_DIGESTS:
                result.skipped.append("印象条数超过上限")
                break
            uid = _resolve_id(key, ids, aliases)
            body = _text(value)
            if not uid:
                result.skipped.append("印象里的人这轮没出现过")
                continue
            if not body:
                continue
            digests[uid] = body
    if digests:
        result.payload["digests"] = digests

    # --- 好感 ---
    affinity: list[dict[str, Any]] = []
    for item in _rows(payload.get("affinity")):
        if len(affinity) >= MAX_AFFINITY:
            result.skipped.append("好感条数超过上限")
            break
        uid = _resolve_id(item.get("user_id") or item.get("uid") or item.get("who"), ids, aliases)
        if not uid:
            result.skipped.append("好感里的人这轮没出现过")
            continue
        delta = _clamp(item.get("delta"), -5.0, 5.0, 0.0)
        if not delta:
            result.skipped.append("好感变化是 0")
            continue
        row = {
            "user_id": uid,
            "delta": delta,
            "reason": _text(item.get("reason")),
        }
        evidence = _evidence_text(item.get("evidence"))
        hits, _hit_at = _match_source(item.get("source_event_ids"), evidence, rows)
        if evidence and hits:
            row["evidence"] = evidence
            row["source_event_ids"] = hits
        affinity.append(row)
    if affinity:
        result.payload["affinity"] = affinity

    # --- 折叠（**不永久删除**：fold 只压成 gist，context 留着） ---
    forget: list[dict[str, Any]] = []
    for item in _as_list(payload.get("forget")):
        if len(forget) >= MAX_FORGET:
            result.skipped.append("折叠条数超过上限")
            break
        raw_id = item if not isinstance(item, Mapping) else (item.get("id") or "")
        memory_id = str(raw_id).strip()
        if not memory_id or memory_id not in allowed:
            result.skipped.append("折叠的记忆不在本轮范围里")
            continue
        if memory_id in merged_away:
            result.skipped.append("折叠的记忆已经被合并走了")
            continue
        number = _int(memory_id)
        if number <= 0:
            result.skipped.append("折叠的记忆号不是一个数字")
            continue
        # 键名对齐 Consolidator._apply_full（它读 dict 里的 ``id``）
        row = {"id": number}
        text = _text(item.get("text") if isinstance(item, Mapping) else "")
        if text:
            row["text"] = text
        if all(one["id"] != number for one in forget):
            forget.append(row)
    if forget:
        result.payload["forget"] = forget

    # --- 同主题覆盖（路 A：整理时顺手判"这条新说法让哪条旧说法作废"） ---
    #
    # 用户实机翻车：她对主人说「我明天要去体检」，可昨天说过、今天下午已经做完，
    # 库里两条并存。整理本来就在看这些记忆，让它顺手指出"谁覆盖谁"是零新增模型调用。
    # 这里的口径：两边都必须是本轮记忆窗口里的 id（模型编的 id 一律丢）、不许自指、
    # 不许重复。"成环"在 ``Consolidator._apply_full`` 里按现成的链再查一遍。
    supersede: list[dict[str, Any]] = []
    for item in _rows(payload.get("supersede")):
        if len(supersede) >= MAX_SUPERSEDE:
            result.skipped.append("作废条数超过上限")
            break
        old = str(item.get("old") or item.get("old_id") or "").strip()
        new = str(item.get("by") or item.get("by_id") or item.get("new") or "").strip()
        if not old or not new:
            result.skipped.append("作废缺 old / by")
            continue
        if old == new:
            result.skipped.append("作废不能指向自己")
            continue
        if old not in allowed or new not in allowed:
            result.skipped.append("作废引用了本轮记忆之外的条目")
            continue
        old_id, new_id = _int(old), _int(new)
        if old_id <= 0 or new_id <= 0:
            result.skipped.append("作废的 id 不是数字")
            continue
        if any(one["old"] == old_id for one in supersede):
            continue  # 同一条旧记忆被指了两次：留第一条
        supersede.append(
            {"old": old_id, "by": new_id, "why": _text(item.get("why"), 120)}
        )
    if supersede:
        result.payload["supersede"] = supersede

    # --- 小睡的要点 ---
    notes: list[dict[str, Any]] = []
    for item in _rows(payload.get("notes")):
        if len(notes) >= MAX_NOTES:
            result.skipped.append("要点条数超过上限")
            break
        body = _text(item.get("text") or item.get("content") or item.get("note"))
        if not body:
            continue
        notes.append(
            {
                "text": body,
                "type": _text(item.get("memory_type") or item.get("type"), 24),
                "emotion": _text(item.get("emotion"), 24),
                "weight": _clamp(item.get("weight"), 0.05, 1.0, 0.5),
                "participants": _participants(item.get("participants"), ids, aliases),
            }
        )
    if notes:
        result.payload["notes"] = notes

    # --- 谈资（T8-2 夜间通道：从今天的对话里抽 1–3 条以后能当谈资的事） ---
    #
    # 和 dream 一样是"单独一类"：不进记忆、不进事实，只做成谈资池里的素材。
    # 这里的口径比"记忆"严：必须有材料里的原话当证据（谈资以后要往外说，
    # 编出来的东西会变成她张口就来的假经历），summary 40 字以内。
    talk: list[dict[str, Any]] = []
    for item in _rows(payload.get("talk")):
        if len(talk) >= MAX_TALK:
            result.skipped.append("谈资条数超过上限")
            break
        body = _text(item.get("summary") or item.get("text") or item.get("talk"), 40)
        if not body:
            continue
        evidence = _text(item.get("quote") or item.get("evidence") or item.get("context"))
        hits, _hit_at = _match_source(item.get("source_event_ids"), evidence, rows)
        if not hits:
            result.skipped.append("谈资对不上材料里的原话")
            continue
        talk.append({"summary": body[:40], "source_event_ids": hits, "quote": evidence[:80]})
    if talk:
        result.payload["talk"] = talk

    # --- 梦（F4：单独一类，前面各节都不吃它） ---
    dream = _text(
        payload.get("dream") if not isinstance(payload.get("dream"), Mapping) else ""
    )
    if not dream and isinstance(payload.get("dream"), Mapping):
        dream = _text(payload["dream"].get("text") or payload["dream"].get("dream"))
    if dream:
        result.payload["dream"] = dream

    return result


def _material_rows(material: Iterable[Any]) -> list[tuple[str, str, float]]:
    """素材归一成 ``(event_id, 原文, 发生时刻)`` 三元组，供证据核对用。"""

    rows: list[tuple[str, str, float]] = []
    for item in material or []:
        if isinstance(item, Mapping):
            event = source_event_to_chat_row(item)
            if event is None:
                continue
            rows.append(
                (
                    str(event.get("source_event_id") or ""),
                    str(event.get("text") or ""),
                    _float(event.get("at"), 0.0),
                )
            )
            continue
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            rows.append(
                (
                    str(item[0] or ""),
                    _text(item[1]),
                    _float(item[2] if len(item) > 2 else 0.0, 0.0),
                )
            )
    return rows


def stamp_operation_keys(payload: Mapping[str, Any], job_id: str) -> dict[str, Any]:
    """给增量条目盖上幂等键（记忆、好感、事实、关系）。

    同一个作业被重复提交时，下游按 ``operation_key`` 认得出"这条已经落过了"，
    不会把同一次经历结算两遍（计划 §10 F3）。
    """

    result: dict[str, Any] = dict(payload or {})
    for key in ("memories", "affinity", "facts", "relations", "notes"):
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        stamped: list[Any] = []
        for index, item in enumerate(rows):
            if isinstance(item, Mapping):
                row = dict(item)
                row.setdefault("operation_key", operation_key(job_id, key, index))
                stamped.append(row)
            else:
                stamped.append(item)
        result[key] = stamped
    return result


def job_summary(job: JobRecord | None) -> dict[str, Any]:
    """给日志 / 面板用的一行摘要。"""

    if job is None:
        return {}
    return {
        "id": job.id,
        "mode": job.mode,
        "status": job.status,
        "attempts": int(job.attempts),
        "from_seq": int(job.from_seq),
        "to_seq": int(job.to_seq),
        "next_seq": int(job.next_seq),
        "items": int(job.item_count),
        "error": job.error,
        "note": job.note,
    }
