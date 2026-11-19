"""动作语义与选择：候选构建（D2）、分层展开（D3）、加权抽样与执行前复核（D4）。

这里是**纯逻辑**：不读数据库、不调模型、不碰全局随机源，随机性一律来自显式 seed。

硬条件只认动作真实声明过的字段（enabled / available_in / preconditions / 工具指令能力 /
配额 / 冷却 / 扩展闸门 / 隐藏名单 / 可达性 / 目标声明）；字段缺失或写法不认识时
**不额外加限制**。名称、描述、tags 里的字眼只参与软分组与软权重，绝不当作门槛——
"休息"两个字不代表现在精力必须够，那得看动作自己有没有写 ``preconditions.min_energy``。
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PURPOSES",
    "PURPOSE_TEXT",
    "Candidate",
    "Facts",
    "FarSummary",
    "LayeredView",
    "SelectionResult",
    "build_candidates",
    "build_weights",
    "check_candidate",
    "facts_from",
    "filter_eligible",
    "layer_view",
    "purpose_of",
    "recheck",
    "reselect_once",
    "sample",
    "select_from_view",
    "soft_weights",
    "total_weight",
]

# 第一层「用途」的分层顺序（D3）：先想干什么，再看具体动作。
PURPOSES: tuple[str, ...] = (
    "continue",
    "rest",
    "social",
    "errand",
    "explore",
    "light",
    "wait",
    "other",
)

# 用途的中文一句话（写进 far 摘要，也方便日志阅读）。
PURPOSE_TEXT: dict[str, str] = {
    "continue": "继续手头这件事",
    "rest": "休息、恢复精力",
    "social": "和人打交道",
    "errand": "处理事情、把待办推进一步",
    "explore": "换个地方、找点新鲜事",
    "light": "轻活动、打发时间",
    "wait": "什么都不做、等一等",
    "other": "可做的事",
}

# 软分组关键词：只在没有任何声明式分组信息时才看名字/描述，仅影响软排序。
_PURPOSE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("continue", ("继续", "延续", "接着", "保持", "continue", "resume", "ongoing")),
    ("wait", ("等待", "等一会", "等等", "待着", "什么都不做", "wait", "idle", "hold")),
    ("rest", ("休息", "睡觉", "睡", "躺", "歇", "小憩", "打盹", "rest", "sleep", "nap")),
    ("social", ("社交", "聊天", "闲聊", "搭话", "打招呼", "联系", "找人", "social", "chat", "greet", "talk")),
    ("errand", ("处理", "事务", "工作", "学习", "上班", "整理", "写", "errand", "task", "work", "study", "chore")),
    ("explore", ("探索", "逛逛", "出门", "冒险", "explore", "wander", "outing", "adventure")),
    ("light", ("轻", "听歌", "发呆", "喝水", "看看", "散步", "light", "stroll", "music")),
)

# 用途的写法别名（声明式字段里出现的都算数，中英不限）。
_PURPOSE_ALIASES: dict[str, str] = {
    "延续": "continue",
    "继续": "continue",
    "休息": "rest",
    "睡眠": "rest",
    "社交": "social",
    "处理事项": "errand",
    "事项": "errand",
    "待办": "errand",
    "探索": "explore",
    "轻活动": "light",
    "等待": "wait",
}
for _name in PURPOSES:
    _PURPOSE_ALIASES[_name] = _name

_CJK = re.compile(r"[\u4e00-\u9fff]")

_PERIOD_TEXT = {"day": "今日", "week": "本周", "month": "本月"}

# 复核/构建时从自由状态里读出来的数值字段（缺省就是"不知道"，不据此限制）。
_VALUE_KEYS = (
    "energy",
    "satiety",
    "mood",
    "loneliness",
    "curiosity",
    "arousal",
    "valence",
    "intimacy",
)


def _as_mapping(value: Any) -> dict[str, Any]:
    """把 dict / pydantic 模型 / 鸭子对象统一读成 dict（读不出来就空 dict）。"""

    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except Exception:
            pass
    try:
        return dict(vars(value))
    except TypeError:
        return {}


def _declared(source: Any, name: str, default: Any = None) -> Any:
    """读一个声明字段：dict 用 get，对象用 getattr；没有就是 default。"""

    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _clean_names(values: Any) -> tuple[str, ...]:
    """归一化字符串列表（去空、去重、保序）；不是列表就当空。"""

    if values is None or isinstance(values, (str, bytes)):
        return ()
    try:
        items = list(values)
    except TypeError:
        return ()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def purpose_of(source: Any) -> str:
    """这个动作（或候选）属于哪一层用途。

    只做**软分组**：优先读声明式字段（purpose / group / tags），都没有才退回
    名称与描述的关键词。任何情况下都不影响是否可用。
    """

    declared = _declared(source, "purpose")
    if isinstance(declared, str) and declared.strip():
        text = declared.strip()
        if text in _PURPOSE_ALIASES:
            return _PURPOSE_ALIASES[text]

    for key in ("group", "tags", "category", "description", "name"):
        raw = _declared(source, key)
        if not raw:
            continue
        if isinstance(raw, (list, tuple, set)):
            words = [str(item) for item in raw if str(item or "").strip()]
            for word in words:
                text = word.strip()
                if text in _PURPOSE_ALIASES:
                    return _PURPOSE_ALIASES[text]
            joined = " ".join(words)
        else:
            text = str(raw).strip()
            if text in _PURPOSE_ALIASES:
                return _PURPOSE_ALIASES[text]
            joined = text
        lowered = joined.lower()
        for purpose, keywords in _PURPOSE_KEYWORDS:
            if purpose == "continue" and key != "name" and key != "description":
                continue
            for keyword in keywords:
                if keyword.lower() in lowered:
                    return purpose
    return "other"


def _is_continuation(source: Any, purpose: str) -> bool:
    """这个动作是不是"把手里的事接着做"（D1 的延续优先，抽样时会钉住不丢）。"""

    for key in ("is_continuation", "continues", "continuation", "continuing"):
        if _declared(source, key):
            return True
    return purpose == "continue"


@dataclass
class Candidate:
    """一个可执行的候选动作（D2 的候选记录）。

    ``reasons`` 只装**被排除的理由**（可读中文，空列表 = 合格）；
    ``notes`` 装"没能校验/按默认处理"的备注，看过一眼就行，不参与判定。
    """

    candidate_id: str
    action_id: str
    name: str = ""
    enabled: bool = True
    available_in: tuple[str, ...] = ()
    preconditions: dict[str, Any] = field(default_factory=dict)
    estimated_cost: float = 0.0
    score_components: dict[str, float] = field(default_factory=dict)
    source_refs: tuple[str, ...] = ()
    eligible: bool = True
    reasons: list[str] = field(default_factory=list)
    # 归一化出来的真实声明：复核时直接用，不再回读原对象（对象可能已经变了）。
    node_id: str = ""
    scope: str = "global"
    travel_cost: float = 0.0
    relocate: bool = True
    llm_level: str = "template"
    tool_names: tuple[str, ...] = ()
    trigger_command: str = ""
    quota: dict[str, int] = field(default_factory=dict)
    target_type: str = "none"
    requires_target: bool = False
    allowed_targets: tuple[str, ...] = ()
    purpose: str = "other"
    is_continuation: bool = False
    description: str = ""
    priority: int = 5
    duration: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class FarSummary:
    """远处节点的摘要条目：名字 + 一句话用途 + 代价估计（D3）。"""

    node_id: str
    name: str
    purpose: str
    distance: float | None = None
    action_count: int = 0
    sample_actions: tuple[str, ...] = ()
    reason: str = ""

    def cost_text(self) -> str:
        """代价估计的短文本（日志与调试用）。"""

        return "未知" if self.distance is None else f"{self.distance:g}"


@dataclass
class LayeredView:
    """分层展开结果：near 是完整候选，far 只有摘要。"""

    near: list[Candidate] = field(default_factory=list)
    far: list[FarSummary] = field(default_factory=list)
    near_nodes: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)


@dataclass
class SelectionResult:
    """抽样的完整记录：分数构成、骰值、选中者、被拒理由（D4 要求可解释）。"""

    chosen: Candidate | None
    scores: dict[str, dict[str, float]] = field(default_factory=dict)
    dice: float = 0.0
    seed: int = 0
    rejected: dict[str, str] = field(default_factory=dict)
    declined: bool = False
    decline_reason: str = ""
    reselects: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Facts:
    """构建候选与复核时用的运行时事实。

    每个字段的缺省都表示**不知道**：不知道就不限制（``available_tools`` /
    ``available_commands`` / ``reachable`` 为 None 时对应硬条件不生效）。
    """

    node_id: str = ""
    scope: str = "private"
    now: float = 0.0
    hidden: frozenset[str] = frozenset()
    gate_reasons: Mapping[str, str] = field(default_factory=dict)
    quota_used: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    cooldowns: Mapping[str, float] = field(default_factory=dict)
    available_tools: frozenset[str] | None = None
    available_commands: frozenset[str] | None = None
    reachable: Mapping[str, float | None] | None = None
    targets: frozenset[str] = frozenset()
    values: Mapping[str, float] = field(default_factory=dict)
    statuses: frozenset[str] = frozenset()


def facts_from(source: Any = None, **overrides: Any) -> Facts:
    """把 state（dict / 状态对象 / Facts）读成 Facts；别名与嵌套都认。"""

    if isinstance(source, Facts):
        base: dict[str, Any] = {
            "node_id": source.node_id,
            "scope": source.scope,
            "now": source.now,
            "hidden": source.hidden,
            "gate_reasons": source.gate_reasons,
            "quota_used": source.quota_used,
            "cooldowns": source.cooldowns,
            "available_tools": source.available_tools,
            "available_commands": source.available_commands,
            "reachable": source.reachable,
            "targets": source.targets,
            "values": source.values,
            "statuses": source.statuses,
        }
    else:
        base = {}
        aliases: dict[str, tuple[str, ...]] = {
            "node_id": ("node_id", "node", "current_node"),
            "scope": ("scope", "session_scope"),
            "now": ("now", "world_time", "time"),
            "hidden": ("hidden", "hidden_actions", "hidden_action_ids"),
            "gate_reasons": ("gate_reasons", "gates", "gate_reason"),
            "quota_used": ("quota_used", "quotas"),
            "cooldowns": ("cooldowns", "cooldown", "cooldown_seconds"),
            "available_tools": ("available_tools", "tools"),
            "available_commands": ("available_commands", "commands", "instructions"),
            "reachable": ("reachable", "distances", "travel"),
            "targets": ("targets", "declared_targets"),
            "values": ("values", "stats", "state_values"),
            "statuses": ("statuses", "status", "current_state", "state_name"),
        }
        for key, names in aliases.items():
            for name in names:
                value = _declared(source, name)
                if value is not None:
                    base[key] = value
                    break
        # 自由状态对象：认识的数值字段挑出来，供 preconditions 判断。
        values = dict(base.get("values") or {})
        for name in _VALUE_KEYS:
            if name in values:
                continue
            value = _declared(source, name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values[name] = float(value)
        if values:
            base["values"] = values
        if base.get("available_tools") is not None:
            base["available_tools"] = frozenset(str(x) for x in base["available_tools"])
        if base.get("available_commands") is not None:
            base["available_commands"] = frozenset(str(x) for x in base["available_commands"])
        for key in ("hidden", "targets"):
            if key in base:
                base[key] = frozenset(str(x) for x in base[key])
        if "statuses" in base:
            raw = base["statuses"]
            if isinstance(raw, (str, bytes)):
                base["statuses"] = frozenset([str(raw)])
            else:
                base["statuses"] = frozenset(str(x) for x in raw)

    for key, value in overrides.items():
        if value is not None:
            base[key] = value
    # 兜底：缺的字段回落到 dataclass 默认值。
    defaults = Facts()
    for key in (
        "node_id",
        "scope",
        "now",
        "hidden",
        "gate_reasons",
        "quota_used",
        "cooldowns",
        "available_tools",
        "available_commands",
        "reachable",
        "targets",
        "values",
        "statuses",
    ):
        base.setdefault(key, getattr(defaults, key))
    return Facts(**base)


def _declared_quota(definition: Any) -> dict[str, int]:
    """动作声明的配额（只有真声明了非零上限才算数）。"""

    raw = _declared(definition, "quota")
    if raw is None:
        return {}
    limits = getattr(raw, "limits", None)
    if callable(limits):
        try:
            raw = limits()
        except Exception:
            raw = {}
    table = _as_mapping(raw)
    out: dict[str, int] = {}
    for period in ("day", "week", "month"):
        limit = int(_number(table.get(period, 0)))
        if limit > 0:
            out[period] = limit
    return out


def _declared_preconditions(definition: Any) -> dict[str, Any]:
    """动作声明的 preconditions（只认 not_state / min_energy 这两种已知写法）。"""

    raw = _declared(definition, "preconditions")
    table = _as_mapping(raw)
    out: dict[str, Any] = {}
    not_state = _clean_names(table.get("not_state"))
    if not_state:
        out["not_state"] = list(not_state)
    if table.get("min_energy") is not None:
        out["min_energy"] = _number(table.get("min_energy"))
    return out


def _external_available(definition: Any) -> tuple[tuple[str, ...], bool]:
    """动作关于"在哪些地点可用"的声明，以及"是否允许移动前往"。

    返回 (地点元组, relocate)。元组为空 = 没声明过地点限制。
    """

    raw = _declared(definition, "available_in")
    nodes: tuple[str, ...] = ()
    if not callable(raw) and not isinstance(raw, bool):
        if isinstance(raw, Mapping):
            nodes = _clean_names(raw.get("nodes") or raw.get("node_ids"))
        elif raw is not None and not isinstance(raw, (str, bytes)):
            nodes = _clean_names(raw)
        else:
            nodes = ()
    if not nodes:
        nodes = _clean_names(_declared(definition, "allowed_nodes"))
    relocate = True
    for key in ("relocate", "can_travel", "travel"):
        value = _declared(definition, key)
        if isinstance(value, bool):
            relocate = value
            break
    return nodes, relocate


def _duration_cost(definition: Any) -> float:
    """用声明过的时长折算代价（分钟）；没声明就是 0，不猜。"""

    duration = _number(_declared(definition, "duration"), 0.0)
    if duration <= 0:
        duration_max = _number(_declared(definition, "duration_max"), 0.0)
        duration = duration_max
    return duration / 60.0 if duration > 0 else 0.0


def _node_index(nodes: Any) -> dict[str, Any]:
    """地点表归一化：Mapping 或对象列表都收。"""

    index: dict[str, Any] = {}
    if not nodes:
        return index
    if isinstance(nodes, Mapping):
        for key, value in nodes.items():
            index[str(key)] = value
        return index
    try:
        items = list(nodes)
    except TypeError:
        return index
    for item in items:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            index[str(item[0])] = item[1]
            continue
        node_id = str(_declared(item, "id", "") or "")
        if node_id:
            index[node_id] = item
    return index


def _node_name(node_id: str, index: Mapping[str, Any]) -> str:
    name = str(_declared(index.get(node_id), "name", "") or "").strip()
    return name or node_id


def _node_note(node_id: str, index: Mapping[str, Any]) -> str:
    definition = index.get(node_id)
    for key in ("note", "prompt", "description", "desc"):
        text = str(_declared(definition, key, "") or "").strip()
        if text:
            return text
    return ""


def _pick_node(
    definition: Any,
    *,
    requested: str,
    declared_nodes: tuple[str, ...],
    facts: Facts,
) -> tuple[str, float, str]:
    """决定这个动作要在哪个地点执行、路上要花多少代价。

    返回 (地点, 移动代价, 备注)。地点为空串表示"没有可用地点"（真不可执行）。
    缺少可达性信息（``facts.reachable is None``）时按"能过去"处理，只留备注，
    不自己发明一个走不过去的结论。
    """

    current = str(facts.node_id or "")
    if requested:
        return requested, 0.0, ""
    if not declared_nodes:
        return current, 0.0, ""
    if current and current in declared_nodes:
        return current, 0.0, ""
    if facts.reachable is None:
        return declared_nodes[0], 0.0, "未提供可达性信息，按可前往处理（未校验）"
    best: tuple[float, str] | None = None
    for node in declared_nodes:
        cost = facts.reachable.get(node)
        if cost is None:
            continue
        value = _number(cost, -1.0)
        if value < 0:
            continue
        if best is None or value < best[0]:
            best = (value, node)
    if best is None:
        return "", 0.0, ""
    return best[1], best[0], ""


def check_candidate(candidate: Candidate, facts: Facts) -> list[str]:
    """候选在给定事实下还成不成立：返回排除理由列表（空 = 合格）。

    只用候选里**真实声明过**的字段；没声明的字段不产生任何限制。
    """

    reasons: list[str] = []

    if not candidate.enabled:
        reasons.append("动作已停用（enabled=False）")

    if candidate.scope == "node":
        if not candidate.available_in:
            reasons.append("动作声明了按地点限定（scope=node），但没有写任何可用地点")
        elif candidate.node_id and candidate.node_id not in candidate.available_in:
            reasons.append(
                f"当前要执行的地点 {candidate.node_id} 不在动作声明的可用地点里"
                f"（available_in={'/'.join(candidate.available_in)}）"
            )

    # 目标声明：只有动作自己说要目标、或写了允许的目标清单时才拦。
    if candidate.requires_target and not facts.targets:
        reasons.append("动作需要目标，但当前没有已声明的目标")
    if candidate.allowed_targets and facts.targets:
        if not (set(candidate.allowed_targets) & set(facts.targets)):
            reasons.append(
                "已声明的目标都不在动作允许的目标里"
                f"（allowed_targets={'/'.join(candidate.allowed_targets)}）"
            )

    # 工具能力：工具型动作必须至少配了一个工具（老代码里 action_usable 就是这个口径）。
    if candidate.tool_names or candidate.llm_level == "tool":
        if not candidate.tool_names:
            reasons.append("工具型动作没有配置任何工具（tool_names 为空），执行不了")
        elif facts.available_tools is not None:
            usable = [name for name in candidate.tool_names if name in facts.available_tools]
            if not usable:
                reasons.append(
                    f"需要的工具都不可用（{'/'.join(candidate.tool_names)}）"
                )

    if candidate.trigger_command and facts.available_commands is not None:
        if candidate.trigger_command not in facts.available_commands:
            reasons.append(f"指令不可用（{candidate.trigger_command}）")

    for period, limit in (candidate.quota or {}).items():
        if limit <= 0:
            continue
        used = int(_number((facts.quota_used.get(candidate.action_id) or {}).get(period, 0)))
        if used >= limit:
            reasons.append(f"{_PERIOD_TEXT.get(period, period)}配额已用完（{used}/{limit}）")

    remaining = _number(facts.cooldowns.get(candidate.action_id, 0.0))
    if remaining > 0:
        reasons.append(f"冷却中（还需 {remaining:g} 秒）")

    if candidate.action_id in facts.hidden or candidate.candidate_id in facts.hidden:
        reasons.append("动作被隐藏（配额用尽或被要求藏起来）")
    gate = str(facts.gate_reasons.get(candidate.action_id, "") or "").strip()
    if gate:
        reasons.append(f"扩展闸门不允许：{gate}")

    precondition = candidate.preconditions or {}
    not_state = _clean_names(precondition.get("not_state"))
    if not_state and facts.statuses:
        hit = [name for name in not_state if name in facts.statuses]
        if hit:
            reasons.append(f"当前状态是「{hit[0]}」，preconditions.not_state 不允许")
    min_energy = precondition.get("min_energy")
    if min_energy is not None and "energy" in facts.values:
        energy = _number(facts.values.get("energy"))
        if energy < _number(min_energy):
            reasons.append(
                f"精力 {energy:g} 低于动作要求 {_number(min_energy):g}（preconditions.min_energy）"
            )

    return reasons


def build_candidates(
    actions: Iterable[Any],
    *,
    node_id: str,
    nodes: Any = (),
    scope: str = "private",
    now: float = 0.0,
    state: Any = None,
    facts: Facts | Mapping[str, Any] | None = None,
    hidden: Iterable[str] | None = None,
    gate_reasons: Mapping[str, str] | None = None,
    quota_used: Mapping[str, Mapping[str, int]] | None = None,
    cooldowns: Mapping[str, float] | None = None,
    available_tools: Iterable[str] | None = None,
    available_commands: Iterable[str] | None = None,
    reachable: Mapping[str, float | None] | None = None,
    declared_targets: Iterable[str] | None = None,
) -> list[Candidate]:
    """把动作定义归一化成候选记录（D2）。

    ``actions`` 里每个元素可以是动作定义（在其声明的地点执行，或按可达性移动前往），
    也可以是 ``(动作定义, 地点)`` 二元组——那种情况下就在给定地点评估这个动作，
    地点不匹配时直接按 available_in 排除。

    所有 ``None`` 的关键字都表示"这次没提供这项事实"：沿用 ``facts`` / ``state`` 的
    值，或者保持未知——未知不产生任何限制。
    """

    index = _node_index(nodes)
    base = facts_from(facts if facts is not None else state)
    merged = facts_from(
        base,
        node_id=str(node_id or base.node_id or ""),
        scope=str(scope or base.scope or "private"),
        now=_number(now, base.now),
        hidden=frozenset(str(x) for x in hidden) if hidden is not None else None,
        gate_reasons=dict(gate_reasons) if gate_reasons is not None else None,
        quota_used=dict(quota_used) if quota_used is not None else None,
        cooldowns=dict(cooldowns) if cooldowns is not None else None,
        available_tools=(
            frozenset(str(x) for x in available_tools) if available_tools is not None else None
        ),
        available_commands=(
            frozenset(str(x) for x in available_commands)
            if available_commands is not None
            else None
        ),
        reachable=dict(reachable) if reachable is not None else None,
        targets=frozenset(str(x) for x in declared_targets) if declared_targets is not None else None,
    )

    out: list[Candidate] = []
    for item in list(actions or []):
        requested = ""
        definition = item
        if isinstance(item, (tuple, list)) and len(item) == 2:
            definition, requested = item[0], str(item[1] or "")
        if definition is None:
            continue

        action_id = str(_declared(definition, "id", "") or "").strip()
        if not action_id:
            action_id = str(_declared(definition, "name", "") or "").strip()
        if not action_id:
            continue
        declared_scope = str(_declared(definition, "scope", "global") or "global")
        declared_nodes, relocate = _external_available(definition)
        node_scope = declared_scope == "node" or bool(declared_nodes)
        if not node_scope:
            declared_nodes = ()

        notes: list[str] = []
        travel = 0.0
        if not node_scope and not requested:
            target_node = str(merged.node_id or "")
        elif not node_scope:
            target_node = requested
        elif not relocate and merged.node_id and merged.node_id not in declared_nodes:
            target_node = ""
            notes.append(
                f"只在地点 {'/'.join(declared_nodes)} 可用，当前在 {merged.node_id}，"
                "且动作声明了不移动（relocate=False）"
            )
        else:
            target_node, travel, note = _pick_node(
                definition, requested=requested, declared_nodes=declared_nodes, facts=merged
            )
            if note:
                notes.append(note)
            if not target_node:
                notes.append(
                    f"动作声明的可用地点（{'/'.join(declared_nodes) or '空'}）都到不了"
                    "（可达性/不存在）"
                )
        if not node_scope:
            travel = 0.0

        score_components: dict[str, float] = {}
        raw_scores = _declared(definition, "score_components") or _declared(definition, "scores")
        for key, value in _as_mapping(raw_scores).items():
            score_components[str(key)] = _number(value)

        declared_cost = _declared(definition, "estimated_cost", None)
        if declared_cost is None:
            declared_cost = _declared(definition, "cost", None)
        # 代价只用声明过的东西折算：没写 estimated_cost 就用时长（分钟），再没写就是 0。
        cost = _duration_cost(definition) if declared_cost is None else _number(declared_cost)
        cost += _number(travel, 0.0)

        tool_names = _clean_names(_declared(definition, "tool_names"))
        if not tool_names:
            single_tool = str(_declared(definition, "tool_name", "") or "").strip()
            tool_names = (single_tool,) if single_tool else ()
        tool_fallbacks = _clean_names(_declared(definition, "tool_fallbacks"))
        if tool_fallbacks:
            tool_names = tuple(dict.fromkeys([*tool_names, *tool_fallbacks]))

        source_refs = _clean_names(
            _declared(definition, "source_refs")
            or _declared(definition, "sources")
            or _declared(definition, "source")
        )
        if not source_refs:
            source_refs = (f"action:{action_id}",)

        purpose = purpose_of(definition)
        candidate = Candidate(
            candidate_id=f"{action_id}@{target_node or merged.node_id or '?'}",
            action_id=action_id,
            name=str(_declared(definition, "name", "") or action_id),
            enabled=bool(_declared(definition, "enabled", True)),
            available_in=tuple(declared_nodes),
            preconditions=_declared_preconditions(definition),
            estimated_cost=cost,
            score_components=score_components,
            source_refs=source_refs,
            node_id=target_node or str(merged.node_id or ""),
            scope="node" if node_scope else "global",
            travel_cost=_number(travel, 0.0),
            relocate=relocate,
            llm_level=str(_declared(definition, "llm_level", "template") or "template"),
            tool_names=tool_names,
            trigger_command=str(_declared(definition, "trigger_command", "") or "").strip(),
            quota=_declared_quota(definition),
            target_type=str(_declared(definition, "target_type", "none") or "none"),
            requires_target=bool(
                _declared(definition, "requires_target")
                or _declared(definition, "needs_target")
                or _declared(definition, "target_required")
            ),
            allowed_targets=_clean_names(_declared(definition, "allowed_targets")),
            purpose=purpose,
            is_continuation=_is_continuation(definition, purpose),
            description=str(_declared(definition, "description", "") or ""),
            priority=int(_number(_declared(definition, "priority", 5), 5)),
            duration=int(_number(_declared(definition, "duration", 0), 0)),
            notes=notes,
        )

        reasons = check_candidate(candidate, merged)
        if not any(
            _declared(definition, key) is not None
            for key in ("enabled", "scope", "available_in", "allowed_nodes", "preconditions")
        ) and not candidate.tool_names and not candidate.trigger_command:
            candidate.notes.append("未声明任何硬条件（enabled/available_in/preconditions 都缺省），不额外限制")
        if candidate.preconditions and not merged.statuses and "energy" not in merged.values:
            candidate.notes.append("未提供状态，preconditions 未校验（不据此限制）")
        candidate.reasons = reasons
        candidate.eligible = not reasons
        out.append(candidate)
    return out


def filter_eligible(candidates: Iterable[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    """按 ``eligible`` 拆成 (合格, 被排除)；被排除的仍带着理由。"""

    eligible: list[Candidate] = []
    rejected: list[Candidate] = []
    for candidate in candidates or []:
        if getattr(candidate, "eligible", True):
            eligible.append(candidate)
        else:
            rejected.append(candidate)
    return eligible, rejected


def _purpose_rank(purpose: str) -> int:
    try:
        return PURPOSES.index(purpose)
    except ValueError:
        return len(PURPOSES)


def _trim_top_k(items: list[Candidate], top_k: int) -> list[Candidate]:
    """按用途顺序取 top_k；"延续"和"等待"只要有就钉住不丢（D3 要求）。"""

    ordered = sorted(items, key=lambda c: (_purpose_rank(c.purpose), c.candidate_id))
    if top_k is None or top_k <= 0:
        return []
    if len(ordered) <= top_k:
        return ordered
    chosen = ordered[: int(top_k)]
    for pinned in ("continue", "wait"):
        if any(c.purpose == pinned for c in chosen):
            continue
        extra = next((c for c in ordered if c.purpose == pinned), None)
        if extra is None:
            continue
        victim_index = None
        for position in range(len(chosen) - 1, -1, -1):
            if chosen[position].purpose not in ("continue", "wait"):
                victim_index = position
                break
        if victim_index is None:
            continue
        chosen[victim_index] = extra
    return sorted(chosen, key=lambda c: (_purpose_rank(c.purpose), c.candidate_id))


def layer_view(
    candidates: Iterable[Candidate],
    *,
    current_node: str,
    distance: Mapping[str, float | None] | None = None,
    near_radius: float = 1,
    expand_limit: int = 3,
    top_k: int = 4,
    nodes: Any = (),
) -> LayeredView:
    """分层展开（D3）：near 给完整候选，far 只给"名字 + 用途 + 代价"。

    ``expand_limit`` 是**展开节点总数上限（含当前节点）**，默认 3 = 当前节点 + 2 个
    最近可达节点。``distance`` 是节点 → 路径成本的映射；没给就只把当前节点当近处。
    """

    index = _node_index(nodes)
    current = str(current_node or "")
    distance = dict(distance or {})
    pool = [c for c in (candidates or []) if getattr(c, "eligible", True)]

    def _dist(node: str) -> float | None:
        if node and node == current:
            return 0.0
        value = distance.get(node)
        if value is None:
            return None
        cost = _number(value, -1.0)
        return cost if cost >= 0 else None

    by_node: dict[str, list[Candidate]] = {}
    for candidate in sorted(pool, key=lambda c: c.candidate_id):
        by_node.setdefault(str(getattr(candidate, "node_id", "") or current), []).append(candidate)

    ranked: list[tuple[float, str]] = []
    if current in by_node:
        ranked.append((0.0, current))
    for node in by_node:
        if node == current:
            continue
        cost = _dist(node)
        if cost is not None and cost <= float(near_radius):
            ranked.append((cost, node))
    ranked.sort(key=lambda pair: (pair[0], pair[1]))
    limit = max(1, int(expand_limit))
    near_nodes = [node for _cost, node in ranked[:limit]]

    near: list[Candidate] = []
    for node in near_nodes:
        near.extend(by_node[node])
    near = _trim_top_k(near, int(top_k))

    far: list[FarSummary] = []
    for node, items in by_node.items():
        if node in near_nodes:
            continue
        counts: dict[str, int] = {}
        for candidate in items:
            counts[candidate.purpose] = counts.get(candidate.purpose, 0) + 1
        dominant = sorted(counts.items(), key=lambda pair: (-pair[1], _purpose_rank(pair[0])))[0][0]
        cost = _dist(node)
        note = _node_note(node, index)
        names = tuple(c.name or c.action_id for c in items[:3])
        purpose_text = note or f"{PURPOSE_TEXT.get(dominant, PURPOSE_TEXT['other'])}（{len(items)} 个动作）"
        reason = (
            f"距离 {cost:g} 超出展开半径 {float(near_radius):g}，只给摘要"
            if cost is not None
            else "距离未知，只给摘要"
        )
        far.append(
            FarSummary(
                node_id=node,
                name=_node_name(node, index),
                purpose=purpose_text,
                distance=cost,
                action_count=len(items),
                sample_actions=names,
                reason=reason,
            )
        )
    far.sort(key=lambda item: (item.distance is None, item.distance or 0.0, item.node_id))

    notes: list[str] = []
    if not near and pool:
        notes.append("当前节点与近处没有合格候选，只能看远处摘要或等一等")
    return LayeredView(near=near, far=far, near_nodes=tuple(near_nodes), notes=notes)


# 软权重的因子名：分数越高越想做，cost 是扣分项。
_SOFT_FACTORS: tuple[str, ...] = ("need", "interest", "todo", "freshness", "mood")
_DEFAULT_COST_SCALE = 60.0


def soft_weights(candidate: Any, *, factors: Mapping[str, float] | None = None) -> dict[str, float]:
    """把候选的软因素折算成权重构成（D4 的软加权）。

    ``candidate.score_components`` 里各项按 0~1 读（缺项 = 0，不猜）；``factors`` 是
    各因子的系数。代价按 ``cost_scale``（默认 60 个代价单位吃掉 1.0 基准分）折成扣分项；
    某个候选的分数构成会被完整记进 SelectionResult，日志里看得到，不由模型随手编。
    """

    factors = dict(factors or {})
    components = _as_mapping(_declared(candidate, "score_components"))
    out: dict[str, float] = {"base": _number(factors.get("base", 1.0), 1.0)}
    for name in _SOFT_FACTORS:
        weight = _number(factors.get(name, 1.0), 1.0)
        out[name] = _clamp01(components.get(name, 0.0)) * weight
    cost_scale = _number(factors.get("cost_scale", _DEFAULT_COST_SCALE), _DEFAULT_COST_SCALE)
    if cost_scale <= 0:
        cost_scale = _DEFAULT_COST_SCALE
    if "cost" in components:
        normalized_cost = _clamp01(components.get("cost"))
    else:
        normalized_cost = min(1.0, _number(_declared(candidate, "estimated_cost"), 0.0) / cost_scale)
    out["cost"] = -normalized_cost * _number(factors.get("cost", 1.0), 1.0)
    return out


def total_weight(components: Mapping[str, float]) -> float:
    """权重构成的合计（扣分后垫到 0，0 就是不参与抽样）。"""

    return max(0.0, sum(_number(value) for value in (components or {}).values()))


def build_weights(
    candidates: Iterable[Candidate], *, factors: Mapping[str, float] | None = None
) -> dict[str, dict[str, float]]:
    """给一批候选算出 {candidate_id: 分数构成}。"""

    return {
        str(candidate.candidate_id): soft_weights(candidate, factors=factors)
        for candidate in (candidates or [])
    }


def _weight_of(value: Any) -> float:
    if isinstance(value, Mapping):
        return total_weight(value)
    return max(0.0, _number(value))


def sample(
    eligible: Iterable[Any],
    weights: Mapping[str, Any],
    *,
    seed: int,
    dice: float | None = None,
    rejected: Mapping[str, str] | None = None,
    factors: Mapping[str, float] | None = None,
) -> SelectionResult:
    """带 seed 的加权抽样（D4）。

    - ``eligible`` 可以是候选对象，也可以是 candidate_id 字符串；顺序不影响结果
      （内部先按 id 排序，所以同一 seed + 同一输入必然同一结果）。
    - ``weights`` 的值可以是分数构成 dict（走 ``total_weight``）或直接的数字。
    - ``dice`` 传入时用它当骰值（0~1），便于测试与回放；不传就用 ``random.Random(seed)``。
    """

    seed = int(seed)
    rng = random.Random(seed)
    used_dice = rng.random() if dice is None else min(max(_number(dice), 0.0), 0.999999999)

    by_id: dict[str, Any] = {}
    for item in eligible or []:
        if isinstance(item, str):
            by_id[item] = item
        else:
            by_id[str(getattr(item, "candidate_id", item))] = item

    scores: dict[str, dict[str, float]] = {}
    for cid in sorted(by_id):
        value = weights.get(cid) if isinstance(weights, Mapping) else None
        scores[cid] = dict(value) if isinstance(value, Mapping) else {"total": _weight_of(value)}

    reasons: dict[str, str] = dict(rejected or {})
    entries: list[tuple[str, float]] = []
    for cid in sorted(by_id):
        weight = _weight_of(weights.get(cid) if isinstance(weights, Mapping) else None)
        if weight <= 0:
            reasons.setdefault(cid, "软权重为 0，不参与抽样")
            continue
        entries.append((cid, weight))

    notes: list[str] = []
    if not entries:
        return SelectionResult(
            chosen=None,
            scores=scores,
            dice=used_dice,
            seed=seed,
            rejected=reasons,
            declined=True,
            decline_reason="没有可抽样的候选（权重全为 0 或没有合格候选）",
            notes=notes,
        )

    total = sum(weight for _cid, weight in entries)
    target = used_dice * total
    chosen_id = entries[-1][0]
    cursor = 0.0
    for cid, weight in entries:
        cursor += weight
        if target < cursor:
            chosen_id = cid
            break
    chosen = by_id[chosen_id]
    if isinstance(chosen, str):
        notes.append("传入的是 candidate_id，没有对应的候选对象可回传")
    return SelectionResult(
        chosen=chosen if not isinstance(chosen, str) else None,
        scores=scores,
        dice=used_dice,
        seed=seed,
        rejected=reasons,
        declined=False,
        notes=notes,
    )


def _fact_view(state: Any) -> Facts:
    return state if isinstance(state, Facts) else facts_from(state)


def _rejections(candidates: Iterable[Candidate], facts: Facts) -> dict[str, str]:
    out: dict[str, str] = {}
    for candidate in candidates or []:
        reasons = check_candidate(candidate, facts)
        if reasons:
            out[str(candidate.candidate_id)] = "；".join(reasons)
    return out


def _best_alternative(
    chosen: Any,
    candidates: Iterable[Candidate],
    facts: Facts,
    *,
    seed: int,
    dice: float | None,
    factors: Mapping[str, float] | None,
) -> Candidate | None:
    """在剩下的候选里按同一套软权重抽一个回退（最多一个）。"""

    chosen_id = str(getattr(chosen, "candidate_id", "") or "")
    pool = [
        candidate
        for candidate in (candidates or [])
        if str(candidate.candidate_id) != chosen_id and not check_candidate(candidate, facts)
    ]
    if not pool:
        return None
    weights = build_weights(pool, factors=factors)
    result = sample(pool, weights, seed=seed, dice=dice, factors=factors)
    return result.chosen


def recheck(
    chosen: Candidate | None,
    candidates: Iterable[Candidate],
    *,
    state: Any,
    allow_reselect: bool = True,
    factors: Mapping[str, float] | None = None,
    seed: int = 0,
    dice: float | None = None,
) -> tuple[bool, Candidate | None]:
    """执行前复核（D4）：被选中的候选现在还能不能做。

    返回 ``(仍然可用, 回退候选)``。不可用且 ``allow_reselect`` 时给出一个回退候选
    （再抽一次，权重口径与主抽样一致）；没有可回退的就是 ``(False, None)``。
    """

    facts = _fact_view(state)
    if chosen is None:
        return False, None
    if not check_candidate(chosen, facts):
        return True, chosen
    if not allow_reselect:
        return False, None
    return False, _best_alternative(
        chosen, candidates, facts, seed=seed, dice=dice, factors=factors
    )


def reselect_once(
    chosen: Candidate | None,
    candidates: Iterable[Candidate],
    *,
    state: Any,
    factors: Mapping[str, float] | None = None,
    seed: int = 0,
    dice: float | None = None,
) -> tuple[Candidate | None, SelectionResult]:
    """复核 + **最多一次**重选（D4：状态变了就取消/重选，绝不执行旧候选）。

    返回 (最终候选, 记录)。记录里的 ``reselects`` 只可能是 0 或 1：回退候选再不合格
    也不会继续往下换人，宁可放弃这一轮。
    """

    facts = _fact_view(state)
    candidates = list(candidates or [])
    rejected = _rejections(candidates, facts)
    scores = build_weights(candidates, factors=factors)
    notes: list[str] = []

    if chosen is None:
        return None, SelectionResult(
            chosen=None,
            scores=scores,
            dice=_number(dice, 0.0),
            seed=int(seed),
            rejected=rejected,
            declined=True,
            decline_reason="没有候选可复核",
            notes=notes,
        )

    reasons = check_candidate(chosen, facts)
    if not reasons:
        return chosen, SelectionResult(
            chosen=chosen, scores=scores, dice=_number(dice, 0.0), seed=int(seed), rejected=rejected
        )

    notes.append(f"原候选 {chosen.candidate_id} 已不可用：{'；'.join(reasons)}")
    fallback = _best_alternative(
        chosen, candidates, facts, seed=seed, dice=dice, factors=factors
    )
    if fallback is None:
        return None, SelectionResult(
            chosen=None,
            scores=scores,
            dice=_number(dice, 0.0),
            seed=int(seed),
            rejected=rejected,
            declined=True,
            decline_reason="原候选已不可用，且没有可回退的候选",
            reselects=1,
            notes=notes,
        )
    fallback_reasons = check_candidate(fallback, facts)
    if fallback_reasons:
        notes.append(f"回退候选 {fallback.candidate_id} 复核也不过：{'；'.join(fallback_reasons)}")
        return None, SelectionResult(
            chosen=None,
            scores=scores,
            dice=_number(dice, 0.0),
            seed=int(seed),
            rejected=rejected,
            declined=True,
            decline_reason="原候选与回退候选都不可用（只重选一次）",
            reselects=1,
            notes=notes,
        )
    notes.append(f"重选为 {fallback.candidate_id}（只重选一次）")
    return fallback, SelectionResult(
        chosen=fallback,
        scores=scores,
        dice=_number(dice, 0.0),
        seed=int(seed),
        rejected=rejected,
        reselects=1,
        notes=notes,
    )


def select_from_view(
    view: LayeredView,
    *,
    seed: int,
    factors: Mapping[str, float] | None = None,
    dice: float | None = None,
    rejected: Mapping[str, str] | None = None,
) -> SelectionResult:
    """在 near 层直接抽样（省得调用方自己接三根管子）。"""

    near = list(getattr(view, "near", []) or [])
    weights = build_weights(near, factors=factors)
    return sample(near, weights, seed=seed, dice=dice, rejected=rejected, factors=factors)
