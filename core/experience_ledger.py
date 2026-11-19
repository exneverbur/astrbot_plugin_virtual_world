"""每日经历账（E2 本体 / E3 概览）——**纯逻辑**，不碰 IO、不碰模型。

设计稿：`.vwtest/vw-daily-experience-ledger-design.md`（12 节）。这一层只做三件事：

1. **经历这一条数据长什么样**（§4）：状态词表、字段归一、幂等改写规则；
2. **什么时候算终态、unknown 能不能翻身**（§3/§4/§5）：`unknown` 不是失败也不是成功，
   它是"等回执"；真回执到了可以落终态一次，**但绝不允许把 planned/unknown 说成 succeeded**；
3. **今日概览怎么由代码写出来**（§6/§7/§8）：按时间段分段、合并连续同类活动、只写已确认事实、
   每条都能回指 ``experience_id``；模型润色是**可选**的，失败就退回这份代码模板。

落库在 `core/db.py`（`daily_experiences` 表），接线在引擎的真实动作/工具/回执边界上——
这一层不做那两件事，所以可以单独测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

# ---- 状态词表（§4）---------------------------------------------------------

EXP_PLANNED = "planned"
EXP_STARTED = "started"
EXP_SUCCEEDED = "succeeded"
EXP_FAILED = "failed"
EXP_CANCELLED = "cancelled"
EXP_PAUSED = "paused"
EXP_UNKNOWN = "unknown"

EXPERIENCE_STATUSES = (
    EXP_PLANNED,
    EXP_STARTED,
    EXP_SUCCEEDED,
    EXP_FAILED,
    EXP_CANCELLED,
    EXP_PAUSED,
    EXP_UNKNOWN,
)

STATUS_LABELS = {
    EXP_PLANNED: "还没开始",
    EXP_STARTED: "正在进行",
    EXP_SUCCEEDED: "已完成",
    EXP_FAILED: "没做成",
    EXP_CANCELLED: "已取消",
    EXP_PAUSED: "暂停了",
    EXP_UNKNOWN: "结果未知",
}

TERMINAL_STATUSES = (EXP_SUCCEEDED, EXP_FAILED, EXP_CANCELLED)
"""真·终态：到了这里就不再变（`unknown` **不在**这里面——它等回执）。"""

UNRESOLVED_STATUSES = (EXP_PLANNED, EXP_STARTED, EXP_PAUSED, EXP_UNKNOWN)
"""还没落定：概览里要进「还没结束」那一段。"""

# 时间段（按世界本地小时分档，只用于概览排版，不参与任何判定）
DAY_PARTS = ((5, "早上"), (11, "上午"), (13, "中午"), (17, "下午"), (22, "晚上"))
NIGHT_PART = "深夜"


def day_part(hour: int) -> str:
    """小时 → 中文时段（概览的「上午 / 下午 / 晚上」）。"""

    stamp = int(hour) % 24
    label = NIGHT_PART
    for start, name in DAY_PARTS:
        if stamp >= start:
            label = name
    return label


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    seen: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Experience:
    """一条经历（§4 的字段表，代码里能填的都在这）。

    ``short_text`` **只能写已确认的事实**；``self_view`` 是她的主观感受，**不能**反过来
    当作"动作成功了"的证据（§4 最后一段）。``unknown`` 是"等回执"，不是失败也不是成功。
    """

    experience_id: str
    experience_day: str = ""
    session_id: str = ""
    """这条经历发生在哪个会话（含分组代表会话）：概览与查询都按它取，**不是**全局账。"""

    activity_kind: str = ""
    status: str = EXP_PLANNED
    started_at: float = 0.0
    ended_at: float = 0.0
    location_id: str = ""
    location_name: str = ""
    persona_id: str = ""
    short_text: str = ""
    self_view: str = ""
    result_summary: str = ""
    source_event_ids: tuple[str, ...] = ()
    related_task_id: str = ""
    related_action_id: str = ""
    related_tool_call_ids: tuple[str, ...] = ()
    parent_id: str = ""
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    send: Mapping[str, Any] = field(default_factory=dict)
    """发送相关信息（§4）：``send_operation_id`` / ``receipt_status`` / ``message_id`` / ``segment_id``。"""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status or "未知")

    def to_row(self) -> dict[str, Any]:
        """给落库用的行（list/tuple 转成 JSON 友好的 list）。"""

        return {
            "experience_id": self.experience_id,
            "experience_day": self.experience_day,
            "session_id": self.session_id,
            "activity_kind": self.activity_kind,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "location_id": self.location_id,
            "location_name": self.location_name,
            "persona_id": self.persona_id,
            "short_text": self.short_text,
            "self_view": self.self_view,
            "result_summary": self.result_summary,
            "source_event_ids": list(self.source_event_ids),
            "related_task_id": self.related_task_id,
            "related_action_id": self.related_action_id,
            "related_tool_call_ids": list(self.related_tool_call_ids),
            "parent_id": self.parent_id,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "send": dict(self.send or {}),
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Experience | None":
        """库表行 → 经历（缺字段用默认值，坏值不抛）。"""

        if not isinstance(row, Mapping):
            return None
        key = _text(row.get("experience_id"))
        if not key:
            return None
        status = _text(row.get("status")) or EXP_PLANNED
        if status not in EXPERIENCE_STATUSES:
            status = EXP_UNKNOWN
        send = row.get("send")
        return cls(
            experience_id=key,
            experience_day=_text(row.get("experience_day")),
            session_id=_text(row.get("session_id")),
            activity_kind=_text(row.get("activity_kind")),
            status=status,
            started_at=_float(row.get("started_at")),
            ended_at=_float(row.get("ended_at")),
            location_id=_text(row.get("location_id")),
            location_name=_text(row.get("location_name")),
            persona_id=_text(row.get("persona_id")),
            short_text=_text(row.get("short_text")),
            self_view=_text(row.get("self_view")),
            result_summary=_text(row.get("result_summary")),
            source_event_ids=_ids(row.get("source_event_ids")),
            related_task_id=_text(row.get("related_task_id")),
            related_action_id=_text(row.get("related_action_id")),
            related_tool_call_ids=_ids(row.get("related_tool_call_ids")),
            parent_id=_text(row.get("parent_id")),
            revision=max(1, int(_float(row.get("revision"), 1))),
            created_at=_float(row.get("created_at")),
            updated_at=_float(row.get("updated_at")),
            send=dict(send) if isinstance(send, Mapping) else {},
        )


def new_experience_id(now: float, seq: int = 0) -> str:
    """``exp_<毫秒>_<序号>``：肉眼可排序，和别的账本 id 一个形状。"""

    return f"exp_{int(_float(now) * 1000)}_{max(0, int(seq)):04d}"


def plan_experience(
    *,
    experience_day: str,
    activity_kind: str,
    now: float,
    experience_id: str = "",
    location_id: str = "",
    location_name: str = "",
    persona_id: str = "",
    parent_id: str = "",
    related_task_id: str = "",
    related_action_id: str = "",
    source_event_ids: Iterable[str] = (),
    short_text: str = "",
    seq: int = 0,
) -> Experience:
    """"我打算做这件事"——**计划**，不是经历（§3：只计划不算完成）。

    ``short_text`` 在这里是"打算做什么"的一句话，**不是**已完成的事实：状态仍是
    ``planned``，概览不会把它写进"今天做过什么"（只会进"还挂着的事"）。
    """

    stamp = _float(now, time.time())
    return Experience(
        experience_id=_text(experience_id) or new_experience_id(stamp, seq),
        experience_day=_text(experience_day),
        activity_kind=_text(activity_kind),
        status=EXP_PLANNED,
        started_at=0.0,
        location_id=_text(location_id),
        location_name=_text(location_name),
        persona_id=_text(persona_id),
        parent_id=_text(parent_id),
        related_task_id=_text(related_task_id),
        related_action_id=_text(related_action_id),
        source_event_ids=_ids(source_event_ids),
        short_text=_text(short_text),
        created_at=stamp,
        updated_at=stamp,
    )


def start_experience(exp: Experience, *, now: float | None = None) -> Experience:
    """真开始了（动作/任务准备完成的那一刻）。已经终态的不动。"""

    if exp.terminal:
        return exp
    stamp = _float(now, time.time())
    return replace(
        exp,
        status=EXP_STARTED,
        started_at=exp.started_at or stamp,
        updated_at=stamp,
        revision=exp.revision + 1,
    )


def finish_experience(
    exp: Experience,
    *,
    status: str,
    short_text: str = "",
    result_summary: str = "",
    self_view: str = "",
    now: float | None = None,
    send: Mapping[str, Any] | None = None,
) -> Experience:
    """落一次结果。

    规则（§3/§4/§5，三条都有用例钉着）：
    - **真终态只落一次**：已经是 succeeded/failed/cancelled 的，再来一次不改口（幂等）；
    - **`unknown` 可以被真回执改一次**：它本来就是"等回执"，改的时候 ``revision`` +1
      并把回执记进 ``send``（重试不许产生第二段经历，只更新同一段）；
    - **不许把 planned/unknown 说成 succeeded**：想从 ``planned`` 直接落终态，必须显式
      先过 :func:`start_experience`（否则这次调用只更新文本、状态仍是 planned）。
    """

    wanted = _text(status)
    if wanted not in EXPERIENCE_STATUSES:
        raise ValueError(f"不是合法状态：{status}")
    stamp = _float(now, time.time())
    if exp.terminal:
        # 已落定：只允许补主观感受/文本，状态与时间一个字不动
        if not _text(self_view) and not _text(short_text):
            return exp
        return replace(
            exp,
            self_view=_text(self_view) or exp.self_view,
            short_text=_text(short_text) or exp.short_text,
            updated_at=stamp,
        )
    if wanted == EXP_PLANNED:
        return exp
    if exp.status == EXP_PLANNED and wanted in TERMINAL_STATUSES + (EXP_UNKNOWN, EXP_PAUSED,):
        # 没开始过就想落结果：按"计划还没变成事实"处理，只更文本
        return replace(exp, updated_at=stamp)

    changes: dict[str, Any] = {
        "status": wanted,
        "short_text": _text(short_text) or exp.short_text,
        "result_summary": _text(result_summary) or exp.result_summary,
        "self_view": _text(self_view) or exp.self_view,
        "updated_at": stamp,
        "revision": exp.revision + 1,
    }
    if wanted in TERMINAL_STATUSES or wanted == EXP_UNKNOWN:
        changes["ended_at"] = exp.ended_at or stamp
    if send is not None:
        merged = dict(exp.send or {})
        merged.update({key: value for key, value in dict(send).items() if value not in ("", None)})
        changes["send"] = merged
    return replace(exp, **changes)


def resolve_unknown(
    exp: Experience, *, status: str, result_summary: str = "", now: float | None = None,
    send: Mapping[str, Any] | None = None,
) -> Experience:
    """真回执到了：把 ``unknown`` 落成终态（只允许一次）。

    目标状态**先校验**（写错状态要当场炸，而不是因为"当前不是 unknown"就被静默吞掉）。
    """

    wanted = _text(status)
    if wanted not in TERMINAL_STATUSES:
        raise ValueError("只有真终态才能解掉 unknown")
    if exp.status != EXP_UNKNOWN:
        return exp
    return finish_experience(
        exp, status=wanted, result_summary=result_summary, now=now, send=send
    )


def attach_child(parent: Experience, child: Experience, *, now: float | None = None) -> Experience:
    """复杂任务的**总经历**记一笔子步骤（§5：总状态不覆盖子步骤的真实状态）。"""

    stamp = _float(now, time.time())
    merged = dict(parent.send or {})
    steps = list(merged.get("steps") or [])
    steps.append(
        {
            "experience_id": child.experience_id,
            "activity_kind": child.activity_kind,
            "status": child.status,
        }
    )
    merged["steps"] = steps
    return replace(parent, send=merged, updated_at=stamp)


def status_sentence(exp: Experience) -> str:
    """一句话事实（概览与提示词都用它，**不编号、不编细节**）。

    失败/未知时把 ``result_summary``（真实原因或回执状态）带上一句——不然概览只会说
    "睡觉，没做成"，读的人（和她自己）都不知道为什么，容易顺着猜。
    """

    what = exp.short_text or exp.activity_kind or "一件事"
    label = exp.status_label
    reason = " ".join(str(exp.result_summary or "").split())[:60]
    if exp.status in TERMINAL_STATUSES:
        if exp.status != EXP_SUCCEEDED and reason and reason != what:
            return f"{what}，{label}（{reason}）。"
        return f"{what}，{label}。"
    if exp.status == EXP_UNKNOWN:
        detail = reason or _text((exp.send or {}).get("receipt_status"))
        return f"{what}，结果还不确定{'（' + detail + '）' if detail else ''}。"
    if exp.status == EXP_PAUSED:
        return f"{what}，中途暂停了{'（' + reason + '）' if reason else ''}。"
    return f"{what}，{label}。"


def merge_continuous(items: Iterable[Experience]) -> list[Experience]:
    """合并**连续同类同地点**的活动（§8：概览别把"读书 1 分钟一条"铺成十几行）。

    只合并"同一 ``activity_kind`` + 同一 ``location_id`` + 同一终态"且时间相接的相邻条目；
    **不合并** unplanned/unknown 与终态混在一起的情况（那会把"没做成"洗成"做完了"）。
    """

    out: list[Experience] = []
    for exp in items:
        if out:
            last = out[-1]
            same_kind = last.activity_kind == exp.activity_kind and bool(exp.activity_kind)
            same_place = last.location_id == exp.location_id
            same_status = last.status == exp.status
            adjacent = last.ended_at and exp.started_at and exp.started_at - last.ended_at <= 1800
            if same_kind and same_place and same_status and adjacent and exp.terminal:
                out[-1] = replace(
                    last,
                    ended_at=max(last.ended_at, exp.ended_at or exp.started_at or last.ended_at),
                    revision=last.revision,
                )
                continue
        out.append(exp)
    return out


def build_overview(
    items: Iterable[Experience],
    *,
    day: str = "",
    now: float | None = None,
    current_location: str = "",
    limit: int = 12,
) -> dict[str, Any]:
    """代码生成的今日概览（§6/§8）：有界、按时间排、每行都能回指 ``experience_id``。

    返回 ``{"lines": [...], "open": [...], "changes": [...], "day": ..., "ids": [...]}``；
    ``lines`` 里每项是 ``{"part": "上午", "text": "...", "experience_id": "..."}``。
    没有模型参与——模型润色是**可选**的下一步，失败就退回它（§8）。
    """

    ordered = sorted(
        [item for item in items if isinstance(item, Experience)],
        key=lambda exp: (exp.started_at or exp.created_at, exp.experience_id),
    )
    ordered = merge_continuous(ordered)
    done = [item for item in ordered if item.terminal]
    open_items = [item for item in ordered if item.status in UNRESOLVED_STATUSES]

    lines: list[dict[str, str]] = []
    for exp in done[: max(1, int(limit))]:
        stamp = exp.ended_at or exp.started_at or exp.created_at
        part = day_part(time.localtime(stamp).tm_hour)
        lines.append(
            {
                "part": part,
                "text": status_sentence(exp),
                "experience_id": exp.experience_id,
            }
        )
    open_lines = [
        {"text": status_sentence(exp), "experience_id": exp.experience_id}
        for exp in open_items[: max(1, int(limit))]
    ]

    # 「今天的变化」：只写地点变化的骨架（§6 的第三段），不编具体情节
    changes: list[str] = []
    last_place = ""
    for exp in ordered:
        place = exp.location_name or exp.location_id
        if place and place != last_place:
            changes.append(place)
            last_place = place
    if current_location and current_location != last_place:
        changes.append(current_location)

    return {
        "day": _text(day),
        "lines": lines,
        "open": open_lines,
        "changes": changes,
        "ids": [entry["experience_id"] for entry in lines + open_lines],
        "merged": len(ordered),
        "total": len([item for item in items if isinstance(item, Experience)]),
        "generated_at": _float(now, time.time()),
    }


def overview_text(overview: Mapping[str, Any] | None, *, heading: str = "今天到现在") -> str:
    """把概览排成能直接进提示词的那段文本（§7 的形状）。"""

    data = overview if isinstance(overview, Mapping) else {}
    lines = list(data.get("lines") or [])
    open_lines = list(data.get("open") or [])
    changes = [str(item) for item in (data.get("changes") or []) if str(item).strip()]
    if not lines and not open_lines and not changes:
        return ""
    parts = [f"# {heading}（这是你自己今天经历过的事）"]
    for entry in lines:
        parts.append(f"- {entry.get('part') or '稍早'}：{entry.get('text') or ''}")
    if open_lines:
        parts.append("# 还挂着的事")
        for entry in open_lines:
            parts.append(f"- {entry.get('text') or ''}")
    if changes:
        parts.append("# 今天的变化")
        parts.append("　".join(changes) + "。")
    parts.append("这些是你的经历，不要求你主动复述；当前对话需要时自然引用，不相关就别硬提。")
    parts.append("planned / unknown 都不是成功——不确定的照实说不确定。")
    return "\n".join(parts)


def lookup(
    items: Iterable[Experience],
    *,
    experience_id: str = "",
    day: str = "",
    activity_kind: str = "",
    location_id: str = "",
    keyword: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """``experience_lookup`` 的纯逻辑半边（§7）：按 id/日/类型/地点/关键词取有界细节。"""

    key = _text(keyword).lower()
    wanted_id = _text(experience_id)
    found: list[Experience] = []
    for exp in items:
        if not isinstance(exp, Experience):
            continue
        if wanted_id and exp.experience_id != wanted_id:
            continue
        if day and exp.experience_day != day:
            continue
        if activity_kind and exp.activity_kind != activity_kind:
            continue
        if location_id and exp.location_id != location_id:
            continue
        if key:
            haystack = " ".join(
                (exp.short_text, exp.result_summary, exp.self_view, exp.activity_kind)
            ).lower()
            if key not in haystack:
                continue
        found.append(exp)
    found.sort(key=lambda exp: (exp.started_at or exp.created_at, exp.experience_id), reverse=True)
    out: list[dict[str, Any]] = []
    for exp in found[: max(1, int(limit))]:
        out.append(
            {
                "experience_id": exp.experience_id,
                "day": exp.experience_day,
                "kind": exp.activity_kind,
                "status": exp.status,
                "status_label": exp.status_label,
                "text": status_sentence(exp),
                "location": exp.location_name or exp.location_id,
                "started_at": exp.started_at,
                "ended_at": exp.ended_at,
                "revision": exp.revision,
                "send": dict(exp.send or {}),
            }
        )
    return out
