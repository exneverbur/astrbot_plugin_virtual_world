"""Agent 任务的**纯逻辑**：一步任务怎么记、什么时候算终态、给页面看什么。

设计见 `.vwtest/b3-executor-plan.md` §1。这一层**不碰 IO**：任务长什么样、状态怎么走、
终态怎么幂等，全在这里；落库在 `core/db.py`，执行（调用工具、跑模型）在引擎里。

三条不变量（每条都有用例钉住）：
1. **终态只能落一次**：`succeeded`/`failed`/`cancelled` 之后再来的 `finish_task` 一律不改口
   （收尾途中被取消、取消之后又走到收尾，都会发生）；
2. **取消不许复活**：已经取消的任务，后到的 `finish_step`/`finish_task` 只能改步，不能把状态改回 running；
3. **步数由账本自己数**：`steps` 是唯一事实，别在别处再维护一个计数器。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_SUCCEEDED = "succeeded"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
# 用户 2026-10-22：步数预算用尽 ≠ 失败（前面做成的都算），也 ≠ 成功（这件事没走完）。
# 单独一个终态，页面 / 回执 / 经验账才不用猜。
TASK_BUDGET_EXHAUSTED = "budget_exhausted"

TASK_WAITING_VW = "waiting_vw"
TASK_WAITING_USER = "waiting_user"
TASK_WAITING_OWNER = "waiting_owner"
TASK_RESUME_READY = "resume_ready"
TASK_DEFERRED = "deferred"

TASK_STATUSES = (
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    TASK_FAILED,
    TASK_CANCELLED,
    TASK_BUDGET_EXHAUSTED,
    TASK_WAITING_VW,
    TASK_WAITING_USER,
    TASK_WAITING_OWNER,
    TASK_RESUME_READY,
    TASK_DEFERRED,
)
"""任务状态：排队 / 执行中 / 成功 / 失败 / 取消 / 步数用尽 / 等 VW 答 / 可恢复 / 暂缓。

后三个（P1-3）**不是终态**：它们只是"先停在这儿"，还会被恢复或到期收掉。
``budget_exhausted`` 是终态，但既不是成功也不是失败：预算用完了，事情停在半路。
"""

TASK_STATUS_LABELS = {
    TASK_QUEUED: "排队中",
    TASK_RUNNING: "执行中",
    TASK_SUCCEEDED: "成功",
    TASK_FAILED: "失败",
    TASK_CANCELLED: "已取消",
    TASK_BUDGET_EXHAUSTED: "步数用尽",
    TASK_WAITING_VW: "等 VW 回答",
    TASK_WAITING_USER: "等对方回答",
    TASK_WAITING_OWNER: "等主人决定",
    TASK_RESUME_READY: "可以接着做",
    TASK_DEFERRED: "暂缓",
}

WAITING_STATUSES = (
    TASK_WAITING_VW,
    TASK_WAITING_USER,
    TASK_WAITING_OWNER,
    TASK_RESUME_READY,
    TASK_DEFERRED,
)
"""非终态的"停着呢"：等待 / 可恢复 / 暂缓。"""

#: 等待到期后落成什么原因码（账本/回执/页面都认它）
REASON_WAIT_EXPIRED = "wait_expired"

TERMINAL_STATUSES = (TASK_SUCCEEDED, TASK_FAILED, TASK_CANCELLED, TASK_BUDGET_EXHAUSTED)
"""终态：到了这里就不再变（`:func:`finish_task` 落地一次）。"""

AGENT_TASK_ACTION_ID = "agent_task"
"""她自己动手干活的入口动作。

**不在** ``DEFAULT_WORLD["actions"]`` 里（老存档里永远不会有这一条），所以照 M2
``collect_meme`` 那一套收口：提示词层由引擎按总开关补行、``_parseable_action_ids``
按总开关放行、``_run_action_batch`` 查表前拦截；总开关关着时提示词与解析**逐字节
等于"系统不存在"**（方案 §2）。写在这里是因为提示词、引擎、测试三处都要用它，
别各写一份字符串。
"""

# 失败原因里用的稳定码（回执/账本/页面都认这几个，别写成句子）
#: 交付形态（P1-1）：只回报结论 / 要产出文件 / 只要一句话答案
DELIVER_KINDS = ("report", "artifact", "answer")
DELIVER_LABELS = {
    "report": "回报结论（说清做成了什么、没做成什么）",
    "artifact": "要产出文件（写完告诉我路径）",
    "answer": "只要一句话答案",
}

REASON_STEPS_EXHAUSTED = "steps_exhausted"
REASON_CANCELLED = "cancelled"
# 用户叫停（不是世界那边取消、也不是进程重启）：2026-10-21 用户 m10183
# 「agent 干活时又来新指令会怎样」——现在的回答是"会停下来，并把前面做过的留着"。
REASON_USER_CANCELLED = "user_cancelled"
REASON_INTERRUPTED = "interrupted"
REASON_TOOL_FAILED = "tool_failed"
# 失败步不占步数额度，所以还要有另一道闸：连着试了很多次都没做成（每次都在
# 烧模型调用）→ 按「尝试次数超限」收尾。与「预算用尽」分开，文案不同。
REASON_ATTEMPTS_EXHAUSTED = "attempts_exhausted"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


NEWLINE = chr(10)
"""这一层要拼好几段多行文本；用常量而不是字面量，免得被工具链的转义吃掉。"""

#: 任务包的协议版本：以后字段改形状时靠它判断"这条是老的还是新的"
BRIEF_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class TaskBrief:
    """交给 Agent 的**小任务包**（P1-1）。

    为什么要有它：以前交接的是"一整句目标"，执行器只能从目标里猜交付要求与约束；
    而 VW 那边有世界配置、人格、聊天记录——全带过去既贵又容易让她把工具调用写成寒暄
    （方案 §5.2 / §6）。这里只留**办事必需**的五样：

    * ``goal``：要办成什么（一句话）；
    * ``deliver``：交付形态（说清楚 / 给文件 / 只回报结论）；
    * ``constraints``：硬约束（预算、地点、不许做的事）；
    * ``tools``：这一轮允许的工具名（名字+档位由策略给，这里只记名字）；
    * ``experiences``：相关的经验条目 id（P3 才有内容，先留空列表）。

    **不带**：人格文本、聊天记录、世界配置、记忆。缺字段不拦，但 :meth:`missing`
    会如实说缺什么——执行器据此决定"先澄清"而不是"瞎猜"。
    """

    goal: str = ""
    deliver: str = "report"
    """``report`` = 回报结论；``artifact`` = 要产出文件；``answer`` = 只要一句话答案。"""
    constraints: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    experiences: tuple[str, ...] = ()
    session_id: str = ""
    parent_task_id: str = ""
    schema_version: int = BRIEF_SCHEMA_VERSION

    def missing(self) -> tuple[str, ...]:
        """缺什么（中文，直接能显示给用户/模型）。空元组 = 齐了。"""

        gaps: list[str] = []
        if not _text(self.goal):
            gaps.append("没说清要办成什么")
        if self.deliver not in DELIVER_KINDS:
            gaps.append(f"交付形态认不出来（{self.deliver}）")
        if not self.tools:
            gaps.append("这一轮没有允许的工具")
        return tuple(gaps)

    @property
    def complete(self) -> bool:
        return not self.missing()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "goal": self.goal,
            "deliver": self.deliver,
            "constraints": list(self.constraints),
            "tools": list(self.tools),
            "experiences": list(self.experiences),
            "session_id": self.session_id,
            "parent_task_id": self.parent_task_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def render(self) -> str:
        """拼成提示词里那几行（**只有这几行**，别把世界塞进来）。"""

        lines = ["# 要办的事" + NEWLINE + (_text(self.goal) or "（没写清楚）")]
        lines.append("# 交付形态" + NEWLINE + DELIVER_LABELS.get(self.deliver, self.deliver))
        if self.constraints:
            lines.append("# 硬约束" + NEWLINE + NEWLINE.join("- " + item for item in self.constraints))
        if self.tools:
            lines.append("# 允许的工具" + NEWLINE + "、".join(self.tools))
        if self.experiences:
            lines.append("# 可以借鉴的经验" + NEWLINE + "、".join(self.experiences))
        gaps = self.missing()
        if gaps:
            lines.append("# 还缺什么（缺就问，别猜）" + NEWLINE + NEWLINE.join("- " + item for item in gaps))
        return NEWLINE.join(lines)


def brief_from(
    *,
    goal: Any,
    deliver: Any = "report",
    constraints: Any = None,
    tools: Any = None,
    experiences: Any = None,
    session_id: Any = "",
    parent_task_id: Any = "",
) -> TaskBrief:
    """从原始参数**宽进严出**地造一个任务包：认不出的交付形态退回 ``report``。"""

    kind = str(deliver or "").strip().lower()
    if kind not in DELIVER_KINDS:
        kind = "report"
    return TaskBrief(
        goal=_text(goal),
        deliver=kind,
        constraints=tuple(str(item) for item in (constraints or ()) if str(item or "").strip()),
        tools=tuple(str(item) for item in (tools or ()) if str(item or "").strip()),
        experiences=tuple(
            str(item) for item in (experiences or ()) if str(item or "").strip()
        ),
        session_id=_text(session_id),
        parent_task_id=_text(parent_task_id),
    )


def brief_from_row(row: Mapping[str, Any] | None) -> TaskBrief | None:
    """从库里那一行读回任务包；没有或读不懂就返回 ``None``（老任务就是这样）。"""

    raw = (row or {}).get("brief_json")
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return None
    if not isinstance(data, Mapping):
        return None
    return brief_from(
        goal=data.get("goal"),
        deliver=data.get("deliver"),
        constraints=data.get("constraints"),
        tools=data.get("tools"),
        experiences=data.get("experiences"),
        session_id=data.get("session_id"),
        parent_task_id=data.get("parent_task_id"),
    )


def new_task_id(now_ms: int, seq: int = 0) -> str:
    """``task_<毫秒>_<序号>``：与 ``ToolResult.task_id`` 同一个域，肉眼也能排序。"""

    return f"task_{_int(now_ms)}_{max(0, _int(seq)):03d}"


def steps_of(task: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """把 ``steps`` 读成列表（存的是 JSON 文本，坏值当空）。"""

    raw = (task or {}).get("steps")
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [dict(item) for item in parsed if isinstance(item, Mapping)]
    return []


def steps_json(task: Mapping[str, Any] | None) -> str:
    """``steps`` 落库用的 JSON 文本（中文不转义，排查时看得懂）。"""

    return json.dumps(steps_of(task), ensure_ascii=False)


def build_task(
    session_id: str,
    goal: str,
    *,
    now: float,
    task_id: str = "",
    brief: "TaskBrief | None" = None,
) -> dict[str, Any]:
    """开一次任务：排队态、空步数、时间戳就位。

    ``brief``（P1-1）给了就一起存：执行器后面每一步都看它，页面也能看出"这次交接了什么"。
    没给（老调用方 / 测试）就是老样子，行为逐字节不变。
    """

    stamp = _float(now)
    task = {
        "task_id": _text(task_id) or new_task_id(int(stamp * 1000)),
        "session_id": _text(session_id),
        "goal": _text(goal),
        "status": TASK_QUEUED,
        "steps": [],
        "note": "",
        "error": "",
        "created_at": stamp,
        "updated_at": stamp,
        "finished_at": 0.0,
        "schema_version": BRIEF_SCHEMA_VERSION,
        "revision": 1,
    }
    if brief is not None:
        task["brief_json"] = brief.to_json()
        task["parent_task_id"] = brief.parent_task_id
    return task


def is_terminal(task: Mapping[str, Any] | None) -> bool:
    return _text((task or {}).get("status")) in TERMINAL_STATUSES


def mark_running(task: Mapping[str, Any], *, now: float = 0.0) -> dict[str, Any]:
    """进执行态（已经终态 / 已经 running 都不动它）。"""

    item = dict(task)
    if is_terminal(item) or _text(item.get("status")) == TASK_RUNNING:
        return item
    item["status"] = TASK_RUNNING
    if _float(now) > 0:
        item["updated_at"] = _float(now)
    return item


def start_step(
    task: Mapping[str, Any],
    *,
    tool: str,
    action: str = "",
    now: float = 0.0,
) -> dict[str, Any]:
    """记下"要调这个工具了"：追加一条 running 的步（终态任务不再追加）。"""

    item = dict(task)
    if is_terminal(item):
        return item
    steps = steps_of(item)
    steps.append(
        {
            "idx": len(steps) + 1,
            "tool": _text(tool),
            "action": _text(action),
            "status": "",
            "summary": "",
            "tool_call_id": "",
            "duration_ms": 0.0,
            "at": _float(now),
        }
    )
    item["steps"] = steps
    item["status"] = TASK_RUNNING if _text(item.get("status")) != TASK_RUNNING else item["status"]
    if _float(now) > 0:
        item["updated_at"] = _float(now)
    return item


def is_waiting(task: Mapping[str, Any] | None) -> bool:
    """这条任务是不是"停着呢"（等 VW / 可恢复 / 暂缓）。"""

    return _text((task or {}).get("status")) in WAITING_STATUSES


def active_seconds(task: Mapping[str, Any] | None) -> float:
    """这条任务**真正跑**了多久（等待时长不算）。"""

    return max(0.0, _float((task or {}).get("active_elapsed")))


def checkpoint_of(task: Mapping[str, Any] | None) -> dict[str, Any]:
    """读检查点（没有就是空 dict，不抛）。"""

    raw = (task or {}).get("checkpoint_json")
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def wait_vw(
    task: Mapping[str, Any],
    *,
    question: str,
    now: float = 0.0,
    ttl_seconds: float = 0.0,
    checkpoint: Mapping[str, Any] | None = None,
    kind: str = "info",
    needed_for: str = "",
) -> dict[str, Any]:
    """把任务挂到"等 VW 答"（**不是终态**）。

    ``revision`` +1：这是 CAS 的依据——同一个答案不能把任务恢复两次。
    ``checkpoint`` 记下"已经做到哪一步"，恢复时据此**不重放已完成的步骤**。
    """

    item = dict(task)
    if is_terminal(item):
        return item  # 终态不许改口
    stamp = _float(now)
    ttl = max(0.0, _float(ttl_seconds))
    item["status"] = TASK_WAITING_VW
    item["wait_reason"] = _text(question)
    item["error"] = ""
    item["revision"] = int(_int(item.get("revision"), 0)) + 1
    item["expires_at"] = (stamp + ttl) if ttl > 0 else 0.0
    item["updated_at"] = stamp or _float(item.get("updated_at"))
    payload = dict(checkpoint or checkpoint_of(item))
    payload.setdefault("stalled_at", stamp)
    payload.setdefault("wait_kind", _text(kind) or "info")
    payload.setdefault("needed_for", _text(needed_for))
    item["checkpoint_json"] = json.dumps(payload, ensure_ascii=False)
    return item


def wait_owner(
    task: Mapping[str, Any],
    *,
    reason: str,
    now: float = 0.0,
    ttl_seconds: float = 0.0,
    checkpoint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把任务挂到"等主人决定"（用户 m31581：不是主人提的麻烦事要先问一句）。

    与 :func:`wait_vw` 同一套机制（``revision`` +1、可恢复），只是状态不同——
    恢复时执行器用 ``owner_answer`` 决定做还是不做。
    """

    item = dict(task)
    if is_terminal(item):
        return item
    stamp = _float(now)
    ttl = max(0.0, _float(ttl_seconds))
    item["status"] = TASK_WAITING_OWNER
    item["wait_reason"] = _text(reason)
    item["error"] = ""
    item["revision"] = int(_int(item.get("revision"), 0)) + 1
    item["expires_at"] = (stamp + ttl) if ttl > 0 else 0.0
    item["updated_at"] = stamp or _float(item.get("updated_at"))
    payload = dict(checkpoint or checkpoint_of(item))
    payload.setdefault("stalled_at", stamp)
    payload.setdefault("wait_kind", "owner")
    item["checkpoint_json"] = json.dumps(payload, ensure_ascii=False)
    return item


def answer_waiting(task: Mapping[str, Any], *, answer: str, now: float = 0.0) -> dict[str, Any]:
    """VW 答了：``waiting_vw`` → ``resume_ready``（**仍然不是 running**，等执行器来领）。"""

    item = dict(task)
    status = _text(item.get("status"))
    if is_terminal(item) or status not in (TASK_WAITING_VW, TASK_WAITING_OWNER):
        return item
    stamp = _float(now)
    item["status"] = TASK_RESUME_READY
    item["revision"] = int(_int(item.get("revision"), 0)) + 1
    payload = checkpoint_of(item)
    # 等主人的那一步记成 owner_answer：执行器开头要用它决定"做 / 不做"
    # （用户 m31581：不是主人提的麻烦事要主人点头）。等 VW 的仍走 vw_answer。
    if status == TASK_WAITING_OWNER:
        payload["owner_answer"] = _text(answer)
    else:
        payload["vw_answer"] = _text(answer)
    payload["answered_at"] = stamp
    item["checkpoint_json"] = json.dumps(payload, ensure_ascii=False)
    item["updated_at"] = stamp or _float(item.get("updated_at"))
    return item


def resume_started(
    task: Mapping[str, Any], *, expected_revision: int, now: float = 0.0
) -> dict[str, Any] | None:
    """CAS 一次：只有 ``resume_ready`` 且 ``revision`` 对得上才拉回 running。

    返回 ``None`` 表示"这次不该动"（已经恢复过 / 状态不对 / 被别人抢先）——调用方
    据此**不重放、不重复扣费**。
    """

    item = dict(task)
    if is_terminal(item):
        return None
    if _text(item.get("status")) not in (TASK_RESUME_READY, TASK_WAITING_VW):
        return None
    if int(_int(item.get("revision"), 0)) != int(expected_revision):
        return None
    stamp = _float(now)
    item["status"] = TASK_RUNNING
    item["revision"] = int(_int(item.get("revision"), 0)) + 1
    item["expires_at"] = 0.0
    item["wait_reason"] = ""
    item["updated_at"] = stamp or _float(item.get("updated_at"))
    return item


#: 重启时发现"上一进程还在跑"的终态原因码（副作用未知，绝不自动重放）
REASON_INTERRUPTED_AT_RESTART = "interrupted_at_restart"


def recover_after_restart(
    task: Mapping[str, Any], *, has_open_question: bool = True, now: float = 0.0
) -> dict[str, Any]:
    """重启后处置一条没做完的任务（**不重放任何副作用**）。

    规则：
    * ``running`` / ``queued``：上一进程正是在做的时候没的——副作用可能已经发生、
      也可能没发生，**不能自动重来**；落成 ``failed`` + ``interrupted_at_restart``；
    * ``waiting_vw``：等她自己答 → 保持原样（下一拍还能处理）；
    * ``waiting_user``：如果那条问题还在（``has_open_question``）就继续等；
      问题已经不在（被答掉 / 过期了）→ 落到 ``deferred``，别凭空恢复；
    * ``deferred``：保持（到期由扫尾处理）；
    * 终态：不动。
    """

    item = dict(task)
    status = _text(item.get("status"))
    stamp = _float(now)
    if status in (TASK_RUNNING, TASK_QUEUED):
        item["status"] = TASK_FAILED
        item["error"] = REASON_INTERRUPTED_AT_RESTART
        item["note"] = item.get("note") or "上一进程在它跑到一半时停了，这件事先算没做成。"
        item["finished_at"] = stamp
        item["updated_at"] = stamp
        return item
    if status == TASK_WAITING_USER and not has_open_question:
        item["status"] = TASK_DEFERRED
        item["note"] = item.get("note") or "那条问题已经不在等着了，这件事先搁着。"
        item["updated_at"] = stamp
        return item
    return item


def expire_waiting(task: Mapping[str, Any], *, now: float = 0.0) -> dict[str, Any]:
    """等待/暂缓到期：落成 ``failed`` + ``wait_expired``（**终态**，不许再复活）。

    到期时间 0 = 没设上限，永远不自动过期（靠取消或恢复收场）。
    """

    item = dict(task)
    if not is_waiting(item):
        return item
    stamp = _float(now)
    expires = _float(item.get("expires_at"))
    if expires <= 0 or stamp < expires:
        return item
    item["status"] = TASK_FAILED
    item["error"] = REASON_WAIT_EXPIRED
    item["note"] = item.get("note") or "等太久了，这件事先算了。"
    item["finished_at"] = stamp
    item["updated_at"] = stamp
    item["expires_at"] = 0.0
    return item


def finish_step(
    task: Mapping[str, Any],
    envelope: Any,
    *,
    now: float = 0.0,
) -> dict[str, Any]:
    """把最近一条 running 的步按 ``ToolResult`` 收尾（没有 running 的步就原样返回）。

    只填**结果**，不改任务状态：任务成没成由 :func:`finish_task` 决定，两件事分开
    才不会出现"某步成功了所以整个任务算成功"这种假话。
    """

    item = dict(task)
    steps = steps_of(item)
    target = None
    for step in reversed(steps):
        if not _text(step.get("status")):
            target = step
            break
    if target is None:
        return item
    status = getattr(envelope, "status", "")
    target["status"] = _text(getattr(status, "value", status))
    target["summary"] = _text(getattr(envelope, "summary", ""))[:200]
    target["tool_call_id"] = _text(getattr(envelope, "tool_call_id", ""))
    target["duration_ms"] = round(_float(getattr(envelope, "duration_ms", 0.0)), 1)
    target["at"] = _float(now) or _float(target.get("at"))
    if bool(getattr(envelope, "budget_free", False)):
        # 重复草稿那种"做过了、没重新做"的步：照记（有回执），但不吃步数预算。
        target["budget_free"] = True
    item["steps"] = steps
    if _float(now) > 0:
        item["updated_at"] = _float(now)
    return item


def step_failures(task: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """没收成（不是 ok）的步：任务收尾时用来写一句原因。"""

    return [
        step
        for step in steps_of(task)
        if _text(step.get("status")) not in ("", "ok")
    ]


def ok_steps(task: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """做成的步（``status == ok``）——**步数预算只按它们计**。

    用户 2026-10-22：以前按"追加了几条步"算，失败 / 被拒 / 超时的步也占额度，
    五步里踩两次坑就见底，看起来像"工具调用次数被限死"。现在失败的步不占额度，
    死循环交给 :func:`attempt_limit_for` 那道硬闸。

    带 ``budget_free`` 的步（重复提交同一份草稿）也不算：那一步根本没重新做。
    """

    return [
        step
        for step in steps_of(task)
        if _text(step.get("status")) == "ok" and not step.get("budget_free")
    ]


def attempt_limit_for(max_steps: int) -> int:
    """一次任务**总共最多试几次**（含失败步）——防死循环的硬闸。

    失败步不占预算之后，模型完全可以一直调一个失败的工具：每次都记一条 failed 步，
    做成 0 步，预算永远用不完。所以另加一道与预算无关的闸：``max_steps`` 的两倍，
    且至少 ``max_steps + 8``（预算很小时也留够重试余地）。32 步 → 64 次尝试；
    每次尝试都要花一次模型调用，64 次已经足够多，再多就是空转。
    """

    budget = max(0, _int(max_steps))
    return max(budget * 2, budget + 8)


def finish_task(
    task: Mapping[str, Any],
    *,
    status: str = "",
    note: str = "",
    error: str = "",
    now: float = 0.0,
) -> dict[str, Any]:
    """落终态（**幂等**：已经是终态就一个字节都不改）。

    ``status`` 留空 = 自己按步数判：有没收成的步就 ``failed``、否则 ``succeeded``。
    """

    item = dict(task)
    if is_terminal(item):
        return item
    wanted = _text(status)
    if wanted and wanted not in TERMINAL_STATUSES:
        raise ValueError(f"不是终态：{wanted}")
    if not wanted:
        wanted = TASK_FAILED if step_failures(item) else TASK_SUCCEEDED
    stamp = _float(now)
    item["status"] = wanted
    item["note"] = _text(note)
    item["error"] = _text(error)
    item["finished_at"] = stamp
    if stamp > 0:
        item["updated_at"] = stamp
    return item


def cancel_task(
    task: Mapping[str, Any],
    *,
    reason: str = REASON_CANCELLED,
    now: float = 0.0,
) -> dict[str, Any]:
    """取消（用户打断 / 配置变更 / 停机收尾都走它）。"""

    return finish_task(task, status=TASK_CANCELLED, error=_text(reason), now=now)


def exhaust_task(
    task: Mapping[str, Any],
    *,
    now: float = 0.0,
    max_steps: int = 0,
    attempt_limit: int = 0,
    pending: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """走到上限了：按**是预算用尽还是尝试超限**分别收尾，文案写清"做到哪、还差什么"。

    - **步数预算用尽**（做成 ``max_steps`` 步）→ ``budget_exhausted``：既不是成功
      （事情没走完）也不是失败（前面做成的都在），页面标签是「步数用尽」；
    - **尝试次数超限**（失败 / 被拒的步太多，做成的还没到预算）→ ``failed``，
      原因 ``attempts_exhausted``。这是防死循环那道闸，不能报成"成功"。

    ``pending`` 是计划里排着还没做的步：写进 note，**不许静默丢弃**。
    没给 ``max_steps`` 的老调用方（测试 / 兼容）按"已做成的步数"当预算，即"用尽了"。
    """

    done = ok_steps(task)
    failures = step_failures(task)
    attempts = len(steps_of(task))
    budget = _int(max_steps)
    limit = _int(attempt_limit)
    todo = [
        _text(item.get("tool"))
        for item in (pending or [])
        if isinstance(item, Mapping) and _text(item.get("tool"))
    ]
    if limit and attempts >= limit and (not budget or len(done) < budget):
        # 尝试次数闸先判：预算还没用完就试到头了，说明一直在失败 —— 必须报失败。
        tried = "、".join(
            name for name in (_text(step.get("tool")) for step in failures) if name
        ) or "那几步"
        note = (
            f"连着试了 {attempts} 次（尝试次数上限 {limit} 次），"
            f"做成了 {len(done)} 步，{tried} 一直没走通。"
            "这件事先停下，免得一直空转；要接着做就换个说法再来。"
        )
        return finish_task(
            task,
            status=TASK_FAILED,
            note=note,
            error=REASON_ATTEMPTS_EXHAUSTED,
            now=now,
        )
    head = (
        f"走到步数上限了（上限 {budget} 步，做成 {len(done)} 步）"
        if budget > 0
        else f"走到步数上限了（做成 {len(done)} 步）"
    )
    parts = [head]
    made = "、".join(
        name for name in (_text(step.get("tool")) for step in done) if name
    )
    if made:
        parts.append(f"做到的：{made}")
    bad = "、".join(
        f"{_text(step.get('tool'))}（{_text(step.get('summary')) or '没成'}）"
        for step in failures
        if _text(step.get("tool"))
    )
    if bad:
        parts.append(f"没走通的：{bad}")
    if todo:
        parts.append(f"计划里还排着没做的：{'、'.join(todo)}")
    parts.append(
        "这件事停在半路（前面做成的都还在），不算失败；"
        "想让她接着做，把世界配置 agent.max_steps 调大再来。"
    )
    return finish_task(
        task,
        status=TASK_BUDGET_EXHAUSTED,
        note="；".join(parts) + "。",
        error=REASON_STEPS_EXHAUSTED,
        now=now,
    )


def task_duration_ms(task: Mapping[str, Any] | None) -> float:
    """任务从建到收尾花了多久（没收尾就是到现在为止）。"""

    item = task or {}
    start = _float(item.get("created_at"))
    if start <= 0:
        return 0.0
    end = _float(item.get("finished_at")) or start
    return round(max(0.0, (end - start) * 1000.0), 1)


def task_view(task: Mapping[str, Any] | None) -> dict[str, Any]:
    """给 ``agent_section()["tasks"]["items"]`` 的只读形状（页面直接渲染它）。"""

    item = dict(task or {})
    status = _text(item.get("status"))
    steps = steps_of(item)
    view = {
        "id": _text(item.get("task_id")),
        "goal": _text(item.get("goal")),
        "status": status,
        "status_label": TASK_STATUS_LABELS.get(status, status or "未记录"),
        "session": _text(item.get("session_id")),
        "steps": len(steps),
        "ok_steps": len([step for step in steps if _text(step.get("status")) == "ok"]),
        "note": _text(item.get("note")),
        "error": _text(item.get("error")),
        "started_at": _float(item.get("created_at")),
        "finished_at": _float(item.get("finished_at")),
        "duration_ms": task_duration_ms(item),
        "done": is_terminal(item),
    }
    return view


def build_artifact(
    *,
    artifact_id: str,
    task_id: str = "",
    session_id: str = "",
    name: str = "",
    path: str = "",
    mime: str = "",
    size_bytes: int = 0,
    created_at: float = 0.0,
    expires_at: float = 0.0,
) -> dict[str, Any]:
    """产物台账的一行（B4 用；这里只保证形状与归一，落库在 db 层）。"""

    return {
        "artifact_id": _text(artifact_id),
        "task_id": _text(task_id),
        "session_id": _text(session_id),
        "name": _text(name),
        "path": _text(path),
        "mime": _text(mime),
        "size_bytes": max(0, _int(size_bytes)),
        "created_at": _float(created_at),
        "expires_at": _float(expires_at),
        "sent_at": 0.0,
    }


def artifact_view(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """给 ``agent_section()["artifacts"]["items"]`` 的只读形状（页面直接渲染它）。

    ``sent`` 是给页面看的判据（``sent_at > 0``）：只报一个时间戳的话，前端每处都要
    自己写一遍"大于零算发出去了"。``downloadable`` 同理——路径是空的就是没落盘。
    """

    item = dict(row or {})
    sent_at = _float(item.get("sent_at"))
    return {
        "id": _text(item.get("artifact_id")),
        "task_id": _text(item.get("task_id")),
        "session": _text(item.get("session_id")),
        "name": _text(item.get("name")),
        "mime": _text(item.get("mime")),
        "size_bytes": max(0, _int(item.get("size_bytes"))),
        "created_at": _float(item.get("created_at")),
        "expires_at": _float(item.get("expires_at")),
        "sent_at": sent_at,
        "sent": sent_at > 0,
        "downloadable": bool(_text(item.get("path"))),
    }


# ---------------------------------------------------------------------------
# 下一步做什么：把模型那句话读成步骤（纯解析，不碰 IO、不碰模型）
# ---------------------------------------------------------------------------

PLAN_STEP_LIMIT = 8
"""一次规划最多认几步：模型偶尔会一口气列十几步，全认下来只会挤掉后面的检查。"""


def _json_object(text: Any) -> dict[str, Any] | None:
    """从一段文本里抠出第一个 JSON 对象（整段 / ``` 围栏 / 前后带解释都要能吃）。

    读不出来返回 ``None``。**不猜**：半截 JSON 不会靠补括号救活——那种救法会把
    "她其实只写了一半"变成"系统替她编了后半句"，账里就再也说不清是谁的意思。
    """

    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    # ```json ... ``` 围栏：取围栏里面的那一段
    fence = raw.find("```")
    if fence >= 0:
        inner = raw[fence + 3 :]
        if inner[:4].lower() == "json":
            inner = inner[4:]
        end = inner.find("```")
        candidates.append(inner if end < 0 else inner[:end])
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


#: 规划输出 v2 的步骤类型（P1-2）
STEP_TOOL = "tool"
STEP_SAY = "say"
STEP_ASK_VW = "ask_vw"
STEP_COMPLETE = "complete"
STEP_STOP = "stop"
STEP_KINDS = (STEP_TOOL, STEP_SAY, STEP_ASK_VW, STEP_COMPLETE, STEP_STOP)

STEP_LABELS = {
    STEP_TOOL: "调工具",
    STEP_SAY: "说一句",
    STEP_ASK_VW: "问 VW",
    STEP_COMPLETE: "做完",
    STEP_STOP: "停下",
}

#: 认不出类型时的稳定原因码（账本/回执/页面都认它，别写成句子）
REASON_UNKNOWN_STEP = "unknown_step_type"


def _typed_step(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """把一步读成 v2 形状；认不出的类型返回带原因的那一步（由上层决定整份停下）。

    只做"读"：不执行、不校验参数合法性（那是执行器的事）。
    """

    raw_kind = _text(item.get("type") or item.get("action"))
    kind = raw_kind.lower()
    tool = _text(item.get("tool") or item.get("name"))
    if not kind:
        # 没写类型：有工具名就是 tool（老写法兼容），否则谈不上一步
        kind = STEP_TOOL if tool else ""
    if kind not in STEP_KINDS:
        return {
            "type": STEP_STOP,
            "unknown_type": raw_kind,
            "reason": REASON_UNKNOWN_STEP,
            "why": _text(item.get("why") or item.get("reason")),
        }
    if kind == STEP_TOOL:
        if not tool:
            return None
        params = item.get("params")
        return {
            "type": STEP_TOOL,
            "tool": tool,
            "params": dict(params) if isinstance(params, Mapping) else {},
            "why": _text(item.get("why") or item.get("reason")),
        }
    if kind == STEP_SAY:
        messages = item.get("messages")
        if isinstance(messages, str):
            messages = [messages]
        rows = [_text(row) for row in (messages or ()) if _text(row)]
        if not rows:
            return None
        return {"type": STEP_SAY, "messages": rows, "why": _text(item.get("why"))}
    if kind == STEP_ASK_VW:
        question = _text(item.get("question") or item.get("ask") or item.get("text"))
        if not question:
            return None
        return {
            "type": STEP_ASK_VW,
            "question": question,
            "kind": _text(item.get("kind") or item.get("need")) or "info",
            "needed_for": _text(item.get("needed_for") or item.get("for")),
            "options": [_text(row) for row in (item.get("options") or ()) if _text(row)],
        }
    # complete / stop
    return {"type": kind, "note": _text(item.get("note") or item.get("result"))}


def parse_plan_v2(text: Any) -> dict[str, Any]:
    """读 v2 规划：``{plan, steps, done, note, stop, stop_reason, unknown_type}``。

    与 :func:`parse_plan` 的关系：后者是"老接口"，行为逐字节不变；这里是新增的读法。
    两者都从同一段 JSON 读，所以模型写成新格式时老代码也不会崩——只是看不懂的部分
    会被当成"没有可执行步骤"。
    """

    empty = {
        "plan": "",
        "steps": [],
        "done": False,
        "note": "",
        "stop": False,
        "stop_reason": "",
        "unknown_type": "",
        "schema_version": 2,
    }
    data = _json_object(text)
    if data is None:
        return dict(empty)
    steps: list[dict[str, Any]] = []
    result = {
        "plan": _text(data.get("plan") or data.get("thinking")),
        "steps": steps,
        "done": bool(data.get("done")),
        "note": _text(data.get("note") or data.get("result")),
        "stop": False,
        "stop_reason": "",
        "unknown_type": "",
        "schema_version": 2,
    }
    for item in list(data.get("steps") or [])[:PLAN_STEP_LIMIT]:
        if not isinstance(item, Mapping):
            continue
        step = _typed_step(item)
        if step is None:
            continue
        if step.get("type") == STEP_STOP and step.get("reason") == REASON_UNKNOWN_STEP:
            # 认不出的类型**整份停下**：绝不能"跳过它继续做下一步"——那等于把
            # 一个没人理解的意图当成没事发生
            result["stop"] = True
            result["stop_reason"] = REASON_UNKNOWN_STEP
            result["unknown_type"] = str(step.get("unknown_type") or "")
            result["steps"] = []
            return result
        steps.append(step)
        if step.get("type") in (STEP_COMPLETE, STEP_STOP):
            # complete/stop 之后的东西不执行（模型偶尔会多写一步）
            break
    result["stop"] = bool(
        result["stop"]
        or any(step.get("type") == STEP_STOP for step in steps)
    )
    if any(step.get("type") == STEP_COMPLETE for step in steps):
        result["done"] = True
    why = next(
        (str(step.get("note") or "") for step in steps if step.get("type") in (STEP_COMPLETE, STEP_STOP)),
        "",
    )
    if why and not result["note"]:
        result["note"] = why
    return result


def parse_plan(text: Any) -> dict[str, Any]:
    """把模型那句"下一步做什么"读成 ``{plan, steps, done, note}``。

    ``steps`` 的每一项是 ``{"tool", "params", "why"}``，只留**写了工具名**的那些：
    没写工具名的一步执行器没法做，收进来只会变成一条"工具是空的"的假失败。

    读不懂就返回空步骤（``done=False``）：执行器据此收尾并说明"她没给出可执行的
    步骤"，绝不替她编一步。
    """

    empty = {"plan": "", "steps": [], "done": False, "note": ""}
    data = _json_object(text)
    if data is None:
        return dict(empty)
    steps: list[dict[str, Any]] = []
    for item in list(data.get("steps") or [])[:PLAN_STEP_LIMIT]:
        if not isinstance(item, Mapping):
            continue
        tool = _text(item.get("tool") or item.get("name"))
        if not tool:
            continue
        params = item.get("params")
        steps.append(
            {
                "tool": tool,
                "params": dict(params) if isinstance(params, Mapping) else {},
                "why": _text(item.get("why") or item.get("reason")),
            }
        )
    return {
        "plan": _text(data.get("plan") or data.get("thinking")),
        "steps": steps,
        "done": bool(data.get("done")),
        "note": _text(data.get("note") or data.get("result")),
    }
