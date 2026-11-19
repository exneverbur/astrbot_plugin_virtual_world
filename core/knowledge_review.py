"""P3-3：经验的校验与发布。

候选经验（P3-2 提炼出来的）**不能自动变成可用**：它先要过几道检查，才允许进召回。
这一层只回答"这条能不能发布 / 该不该降级"，不写库、不调模型。

发布门槛（宁可少发布，也不让一条错经验带歪所有后续任务）：

1. **有证据**：至少一条可追溯的回执 id；
2. **有作用域**：`scope` 非空（空作用域的经验永远召回不到，发布也没意义）；
3. **方法可读**：方法那句话至少两个字符、且不是"用 X"这种只有一个工具的废话；
4. **依赖齐全**：`dependency_hash` 非空（不然工具换了也没法判断失效）；
5. **没被撤销**：撤销过的永不复活。

发布之后还会**降级**：依赖变了（工具/动作改过）→ `deprecated`，并写清原因。
"""

from __future__ import annotations

from typing import Any, Mapping

from . import knowledge

#: 拒收原因码（稳定，页面与调试输出都认它）
REJECT_NO_EVIDENCE = "no_evidence"
REJECT_NO_SCOPE = "no_scope"
REJECT_WEAK_METHOD = "weak_method"
REJECT_NO_DEPENDENCY = "no_dependency"
REJECT_REVOKED = "revoked"
REJECT_NOT_CANDIDATE = "not_candidate"

REJECT_LABELS = {
    REJECT_NO_EVIDENCE: "没有可追溯的证据",
    REJECT_NO_SCOPE: "没有作用域（发布了也召回不到）",
    REJECT_WEAK_METHOD: "方法写得太含糊",
    REJECT_NO_DEPENDENCY: "没有依赖指纹（工具变了没法判断失效）",
    REJECT_REVOKED: "这条已被撤销",
    REJECT_NOT_CANDIDATE: "状态不是候选",
}


def validate(row: Mapping[str, Any] | None) -> tuple[bool, str]:
    """能不能发布：``(可以, 拒绝原因)``。原因码见 :data:`REJECT_LABELS`。"""

    item = knowledge.from_row(row)
    if item is None:
        return False, REJECT_NO_EVIDENCE
    if item.status == knowledge.STATUS_REVOKED:
        return False, REJECT_REVOKED
    if item.status != knowledge.STATUS_CANDIDATE:
        return False, REJECT_NOT_CANDIDATE
    if not item.evidence:
        return False, REJECT_NO_EVIDENCE
    if not item.scope:
        return False, REJECT_NO_SCOPE
    if len(item.method) < 2 or len(item.method.replace("用", "").strip()) < 1:
        return False, REJECT_WEAK_METHOD
    if not item.dependency:
        return False, REJECT_NO_DEPENDENCY
    return True, ""


def publish(
    row: Mapping[str, Any] | None, *, confidence: Any = None, now: float = 0.0
) -> tuple[knowledge.Experience | None, str]:
    """发布（候选 → 可用）。过不了门槛就原样返回 ``(None, 原因)``。"""

    ok, reason = validate(row)
    if not ok:
        return None, reason
    item = knowledge.from_row(row)
    if item is None:
        return None, REJECT_NO_EVIDENCE
    return knowledge.promote(item, confidence=confidence, now=now), ""


def needs_downgrade(
    row: Mapping[str, Any] | None, *, current_dependency: str
) -> bool:
    """依赖变了就该降级（工具改名 / 动作定义改过）。"""

    item = knowledge.from_row(row)
    if item is None or not item.usable:
        return False
    if not item.dependency:
        return False
    return item.dependency != str(current_dependency or "")


def downgrade_reason(old: str, new: str) -> str:
    if not old:
        return "这条经验没有依赖指纹"
    if not new:
        return "现在算不出依赖指纹（工具清单读不到）"
    return "依赖变了（用到的工具/动作改过），旧经验不再适用"


def summarize(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """给页面/调试的一段小结：各有几条候选、可用、过时、撤销。"""

    counts = {status: 0 for status in knowledge.STATUSES}
    rejections: dict[str, int] = {}
    for row in rows or ():
        item = knowledge.from_row(row)
        if item is None:
            continue
        counts[item.status] = counts.get(item.status, 0) + 1
        if item.status == knowledge.STATUS_CANDIDATE:
            ok, reason = validate(row)
            if not ok:
                rejections[reason] = rejections.get(reason, 0) + 1
    return {
        "total": len(rows or ()),
        "counts": counts,
        "rejections": rejections,
        "labels": dict(knowledge.STATUS_LABELS),
    }
