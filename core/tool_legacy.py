"""旧「通用工具名单」的收口报告（P0-6）。

背景：工具现在**由动作声明**（每个工具型动作自己写 `tool_names` / `tool_fallbacks`），
但世界配置里还留着老字段 ``global_allowed_tools``（"任何地点都能直接用的工具"）。
两条路径同时存在，于是有三件事说不清：

1. **旧名单里有没有"只在名单里、没被任何动作认领"的工具**——一旦有人把老字段清掉，
   这些工具会**悄悄消失**，而界面上没有任何提示；
2. 旧名单里的工具**是不是也被权限档管着**——不能因为它是"通用工具"就绕过
   Agent 的四个开关（只读 / 生成文件 / 发文件 / 执行代码）；
3. 到底该删还是该迁：**本模块不删、不改、不自动建动作**。它只把差异摊开给用户看，
   迁移动作由用户自己决定（建一个动作去认领它）——自动建动作等于替用户扩权。

所以这里只做一件事：给定"世界配置 + 宿主工具 + 各动作绑定的工具"，算出一张对账表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class LegacyToolRow:
    """一个旧名单里的工具，以及它现在到底被谁认领。"""

    name: str
    installed: bool = False
    tier: str = ""
    """权限档（read / artifact / send / code）；空 = 认不出档位，**不会**被放行。"""
    switch_ok: bool = False
    """管这一档的子开关是不是开着（关着 = 给了名字也调不动）。"""
    bound_by: tuple[str, ...] = ()
    """哪些动作绑了它（动作 id）。空 = 只有旧名单在撑着，迁移时会丢。"""

    @property
    def orphan(self) -> bool:
        """只在旧名单里、没有任何动作认领——迁移前必须先处理的那几个。"""

        return not self.bound_by


@dataclass(frozen=True)
class LegacyToolReport:
    rows: tuple[LegacyToolRow, ...] = ()
    note: str = ""
    counts: dict[str, int] = field(default_factory=dict)

    def orphan_names(self) -> list[str]:
        return [row.name for row in self.rows if row.orphan]

    def missing_names(self) -> list[str]:
        return [row.name for row in self.rows if not row.installed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "orphans": self.orphan_names(),
            "missing": self.missing_names(),
            "rows": [
                {
                    "name": row.name,
                    "installed": bool(row.installed),
                    "tier": row.tier,
                    "switch_ok": bool(row.switch_ok),
                    "bound_by": list(row.bound_by),
                    "orphan": bool(row.orphan),
                }
                for row in self.rows
            ],
            "note": self.note,
        }


def _binders(actions: Iterable[Any]) -> dict[str, list[str]]:
    """工具名 → 认领它的动作 id（只算工具型动作，按声明顺序）。"""

    bound: dict[str, list[str]] = {}
    for action in actions or ():
        if str(getattr(action, "llm_level", "") or "") != "tool":
            continue
        action_id = str(getattr(action, "id", "") or "")
        names: list[str] = []
        try:
            names = [str(item) for item in (action.tool_list() or [])]
        except Exception:
            names = [str(item) for item in (getattr(action, "tool_names", None) or [])]
        for name in names:
            name = str(name or "").strip()
            if not name:
                continue
            bound.setdefault(name, [])
            if action_id and action_id not in bound[name]:
                bound[name].append(action_id)
    return bound


def build_report(
    *,
    legacy_names: Iterable[str],
    actions: Iterable[Any],
    available: Mapping[str, Any] | None = None,
    tier_of: Any = None,
    switch_enabled: Any = None,
) -> LegacyToolReport:
    """算对账表（**纯函数**，不碰 IO）。

    ``tier_of(name)`` / ``switch_enabled(tier)`` 由调用方注入（这里是纯策略，不认引擎）；
    不给就按"档位未知、开关关着"处理——宁可报告成"用不了"，也不假装它能用。
    """

    bound = _binders(actions)
    available_names = {str(name) for name in (available or {})}
    rows: list[LegacyToolRow] = []
    seen: set[str] = set()
    for raw in legacy_names or ():
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        tier = ""
        try:
            tier = str(tier_of(name) or "") if callable(tier_of) else ""
        except Exception:
            tier = ""
        switch_ok = False
        if tier:
            try:
                switch_ok = bool(switch_enabled(tier)) if callable(switch_enabled) else False
            except Exception:
                switch_ok = False
        rows.append(
            LegacyToolRow(
                name=name,
                installed=name in available_names,
                tier=tier,
                switch_ok=switch_ok,
                bound_by=tuple(bound.get(name) or ()),
            )
        )
    counts = {
        "total": len(rows),
        "installed": len([row for row in rows if row.installed]),
        "missing": len([row for row in rows if not row.installed]),
        "orphans": len([row for row in rows if row.orphan]),
        "unusable": len([row for row in rows if row.installed and row.tier and not row.switch_ok]),
    }
    if not rows:
        note = "旧「通用工具名单」是空的：工具全部由动作声明，没有遗留项。"
    elif counts["orphans"]:
        note = (
            f"旧名单里有 {counts['orphans']} 个工具**没有动作认领**（"
            + "、".join(row.name for row in rows if row.orphan)
            + "）：它们现在只靠这份旧名单撑着。要迁的话，请建一个工具型动作去认领它；"
            "本插件不会替你自动建（那等于替你扩权）。"
        )
    else:
        note = (
            f"旧名单里 {counts['total']} 个工具都已有动作认领（"
            + "、".join(row.name for row in rows if row.name)
            + "），这份旧名单可以留着不用，删掉也不会少东西。"
        )
    return LegacyToolReport(rows=tuple(rows), note=note, counts=counts)
