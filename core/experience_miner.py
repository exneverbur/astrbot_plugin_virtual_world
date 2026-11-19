"""从**已经做成的事**里提炼候选经验（P3-2）。

这一层刻意**不调模型**。原因有三：

1. 提炼的原料是结构化的：任务的 goal、每一步用的工具、每一步成没成、最后那句 note、
   以及工具回执的 id。把这种东西交给模型"总结一下"，只会引入编造的风险，
   而它本来就够清楚（方案 §4.2：后台候选提炼，不在聊天主路径额外调整理模型）；
2. 主路径已经够重了：多一次模型调用就是多一份延迟与费用，而经验的价值是"下次省事"，
   不值得让这一轮陪着等；
3. 纯函数才能被穷举测试：什么该提炼、什么绝不提炼，全都能写成用例。

提炼规则（宁缺毋滥）：

* 只认 ``succeeded`` 的任务；
* 必须**至少有一次成功的工具调用**（没有工具证据的"我记住了"不算能力）；
* ``situation`` 用任务的 goal（压成一句）、``method`` 用成功的工具序列（去重保序）；
* ``limitations`` 只写**真实发生过的挫折**（失败的步骤 / 超时 / 被拒），一条一句；
* 证据 = 那些工具回执的 id（E1 的 ``tool_call_id``）；
* 依赖指纹 = 用到的工具名集合（工具换了，经验就该失效）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import knowledge

#: 一次提炼里最多认几个工具（再多也只是一句"用了这些"）
MAX_TOOLS = 6

#: 方法那一句最长多少字（超过就截断，别把一整段塞进提示词）
MAX_METHOD_CHARS = 80


def _text(value: Any) -> str:
    return str(value or "").strip()


def step_succeeded(step: Mapping[str, Any]) -> bool:
    """这一步成没成：``status == "ok"``（执行器写的）或 ``ok`` 为真（老/测试数据）。

    两种都认是有原因的：执行器落库用的是 ``status``（复用 ``ToolResult.status``），
    而很早的测试夹具里写的是布尔值。判错一边都会把"做成了"读成"没做成"。
    """

    status = _text((step or {}).get("status")).lower()
    if status:
        return status == "ok"
    return bool((step or {}).get("ok"))


def _steps(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = (task or {}).get("steps")
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    return [dict(item) for item in (raw or ()) if isinstance(item, Mapping)]


@dataclass(frozen=True)
class Candidate:
    """一条提炼结果（还没入库）。``reason`` 非空 = 没有提炼，并说明为什么。"""

    situation: str = ""
    method: str = ""
    limitations: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    dependency: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.situation and self.method and self.evidence)

    def as_knowledge(self, *, scope: str = "", persona_id: str = "", now: float = 0.0):
        """转成一条**候选**经验（还没发布，等 P3-3 校验）。"""

        if not self.ok:
            return knowledge.Experience()
        return knowledge.build(
            situation=self.situation,
            method=self.method,
            limitations="；".join(self.limitations),
            evidence=self.evidence,
            dependency=self.dependency,
            scope=scope,
            persona_id=persona_id,
            source="agent",
            confidence=0.4,
            now=now,
            # 经验默认半年后失效：工具生态会变，旧经验不该永久有效
            ttl_seconds=180 * 24 * 3600,
        )


def mine(
    task: Mapping[str, Any],
    *,
    receipts: Iterable[Mapping[str, Any]] = (),
) -> Candidate:
    """从一条**已经结束**的任务提炼候选（纯函数、不写库、不调模型）。"""

    row = dict(task or {})
    status = _text(row.get("status"))
    if status != "succeeded":
        return Candidate(reason=f"这条任务不是做成的（{status or '没状态'}），不提炼")
    steps = _steps(row)
    if not steps:
        return Candidate(reason="没有分步记录，说不清是怎么做成的")
    ok_steps = [step for step in steps if step_succeeded(step)]
    tools: list[str] = []
    evidence: list[str] = []
    by_call = {
        _text(item.get("tool_call_id")): dict(item)
        for item in (receipts or ())
        if _text(item.get("tool_call_id"))
    }
    for step in ok_steps:
        tool = _text(step.get("tool"))
        if tool and tool not in tools:
            tools.append(tool)
        call_id = _text(step.get("tool_call_id"))
        if call_id:
            evidence.append(call_id)
    if not tools:
        return Candidate(reason="没有一次成功的工具调用，算不上「做这类事的方法」")
    # 证据也允许来自回执（有些路径只留回执、没写进 steps）
    if not evidence:
        evidence = [key for key in by_call if key][:MAX_TOOLS]
    if not evidence:
        return Candidate(reason="没有可追溯的证据（回执 id 都没有），不提炼")
    goal = " ".join(_text(row.get("goal")).split())
    if not goal:
        return Candidate(reason="任务没写清要办什么，提炼出来的方法没意义")
    situation = goal if len(goal) <= 40 else goal[:39] + "…"
    method = "、".join(f"用 {name}" for name in tools[:MAX_TOOLS])
    if len(method) > MAX_METHOD_CHARS:
        method = method[: MAX_METHOD_CHARS - 1] + "…"
    limitations: list[str] = []
    for step in steps:
        if step_succeeded(step):
            continue
        tool = _text(step.get("tool")) or "某一步"
        reason = _text(step.get("error")) or _text(step.get("note"))
        limitations.append(f"{tool} 那步没成" + (f"（{reason}）" if reason else ""))
    return Candidate(
        situation=situation,
        method=method,
        limitations=tuple(limitations[:3]),
        evidence=tuple(evidence[:MAX_TOOLS]),
        dependency=knowledge.dependency_hash(*tools[:MAX_TOOLS]),
    )


def should_store(
    candidate: Candidate,
    *,
    existing: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """要不要入库（去重 + 门槛）。返回 ``(要不要, 为什么)``。"""

    if not candidate.ok:
        return False, candidate.reason or "没有可提炼的内容"
    item = knowledge.from_row(existing) if existing else None
    if item is None:
        return True, "新候选"
    if item.status == knowledge.STATUS_REVOKED:
        # 撤销过的做法不许被"又做成一次"悄悄复活——那是人的判断，不是系统的
        return False, "这条经验被撤销过，不再自动入库"
    return True, "已有同一条，只追加证据"
