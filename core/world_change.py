"""世界扩建（P5）：区域 / 地点 / 连线的**变更包**。

为什么要有"变更包"而不是直接改配置：世界是她生活的地方，改坏了整段玩法就崩了。
所以一条变更必须走完四步，每一步都能被检查、被回看：

1. **草案**（``draft``）——要加哪些区域/地点/连线，纯数据；
2. **校验**（``validate``）——id 不撞、引用的地点存在、连线两端都在、区域可达；
   校验不过就**一个字节都不写**；
3. **预览**（``preview``）——算出应用后的世界长什么样（给用户看差异，不落盘）；
4. **应用**（``apply``）——纯函数返回新世界 + 回执；落盘由调用方（引擎）做，
   并带 ``operation_id`` 幂等（同一条变更重复应用不会加两遍）。

这一层**不碰 IO、不调模型**：所有判断都是确定性的，能被穷举测试。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: 一条变更最多动几个实体（防止一次塞进来一个世界）
MAX_NODES = 12
MAX_ZONES = 4
MAX_EDGES = 24

#: 校验失败的原因码（稳定；页面与回执都认）
ERR_DUPLICATE = "duplicate_id"
ERR_MISSING_ZONE = "missing_zone"
ERR_MISSING_NODE = "missing_node"
ERR_BAD_EDGE = "bad_edge"
ERR_TOO_MANY = "too_many"
ERR_EMPTY = "empty_change"
ERR_BAD_FIELD = "bad_field"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _edge_ends(edge: Mapping[str, Any]) -> tuple[str, str]:
    """一条连线的两端。

    世界配置里的字段叫 ``from`` / ``to``（见 ``core/models.py::EdgeDef``）；
    变更包里也认 ``from_node`` / ``to_node`` 这两种写法——模型和用户都可能这么写，
    认下来比报错有用。**写回去的一律是 ``from`` / ``to``**（世界的格式）。
    """

    left = _text(edge.get("from")) or _text(edge.get("from_node"))
    right = _text(edge.get("to")) or _text(edge.get("to_node"))
    return left, right


def _edge_row(left: str, right: str, *, index: int = 0) -> dict[str, Any]:
    """按世界的格式造一条连线（``id`` 必须唯一，撞了加序号）。"""

    base = f"e_{_slug(left)}_{_slug(right)}"
    edge_id = base if not index else f"{base}_{index}"
    return {
        "id": edge_id,
        "from": left,
        "to": right,
        "ticks": 1,
        "bidirectional": True,
    }


def _slug(value: Any) -> str:
    body = _text(value).lower()
    picked = [ch if (ch.isalnum() or ch in "-_") else "-" for ch in body]
    slug = "".join(picked).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:40]


@dataclass(frozen=True)
class WorldChange:
    """一条变更包：``operation_id`` 是幂等键，``revision`` 是"基于哪一版世界"。"""

    operation_id: str = ""
    revision: int = 0
    zones: tuple[dict[str, Any], ...] = ()
    nodes: tuple[dict[str, Any], ...] = ()
    edges: tuple[dict[str, Any], ...] = ()
    title: str = ""
    note: str = ""

    def is_empty(self) -> bool:
        return not (self.zones or self.nodes or self.edges)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "revision": int(self.revision),
            "title": self.title,
            "note": self.note,
            "zones": [dict(item) for item in self.zones],
            "nodes": [dict(item) for item in self.nodes],
            "edges": [dict(item) for item in self.edges],
        }


@dataclass(frozen=True)
class ChangeReport:
    ok: bool = False
    problems: tuple[tuple[str, str], ...] = ()
    summary: str = ""
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "problems": [{"code": code, "detail": detail} for code, detail in self.problems],
            "summary": self.summary,
            "counts": dict(self.counts),
        }


def operation_id_of(payload: Mapping[str, Any]) -> str:
    """幂等键：同一条变更（内容相同）永远得到同一个 id。"""

    body = json.dumps(
        {
            "zones": payload.get("zones") or [],
            "nodes": payload.get("nodes") or [],
            "edges": payload.get("edges") or [],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "chg_" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def from_payload(payload: Mapping[str, Any] | None) -> WorldChange:
    """从请求体读成变更包（缺字段就是空，不抛）。"""

    data = dict(payload or {})
    zones = tuple(dict(item) for item in (data.get("zones") or ()) if isinstance(item, Mapping))
    nodes = tuple(dict(item) for item in (data.get("nodes") or ()) if isinstance(item, Mapping))
    edges = tuple(dict(item) for item in (data.get("edges") or ()) if isinstance(item, Mapping))
    change = WorldChange(
        zones=zones,
        nodes=nodes,
        edges=edges,
        title=_text(data.get("title")),
        note=_text(data.get("note")),
        revision=int(data.get("revision") or 0),
    )
    return WorldChange(
        operation_id=_text(data.get("operation_id")) or operation_id_of(change.as_dict()),
        revision=change.revision,
        zones=change.zones,
        nodes=change.nodes,
        edges=change.edges,
        title=change.title,
        note=change.note,
    )


def validate(change: WorldChange, world: Mapping[str, Any] | None) -> ChangeReport:
    """校验：能不能把这个变更应用到这份世界上。**不过就一个字节都不写。**"""

    problems: list[tuple[str, str]] = []
    current = dict(world or {})
    zone_ids = {_text(item.get("id")) for item in (current.get("zones") or ()) if isinstance(item, Mapping)}
    node_ids = {_text(item.get("id")) for item in (current.get("nodes") or ()) if isinstance(item, Mapping)}
    edge_rows = [item for item in (current.get("edges") or ()) if isinstance(item, Mapping)]
    if change.is_empty():
        return ChangeReport(False, ((ERR_EMPTY, "这条变更什么都没改"),), "空变更", {})
    if len(change.zones) > MAX_ZONES:
        problems.append((ERR_TOO_MANY, f"一次最多加 {MAX_ZONES} 个区域"))
    if len(change.nodes) > MAX_NODES:
        problems.append((ERR_TOO_MANY, f"一次最多加 {MAX_NODES} 个地点"))
    if len(change.edges) > MAX_EDGES:
        problems.append((ERR_TOO_MANY, f"一次最多加 {MAX_EDGES} 条连线"))

    new_zone_ids: set[str] = set()
    for zone in change.zones:
        zone_id = _text(zone.get("id"))
        name = _text(zone.get("name"))
        if not zone_id or not name:
            problems.append((ERR_BAD_FIELD, "区域要有 id 和名字"))
            continue
        if zone_id in zone_ids or zone_id in new_zone_ids:
            problems.append((ERR_DUPLICATE, f"区域 id 撞了：{zone_id}"))
        new_zone_ids.add(zone_id)

    new_node_ids: set[str] = set()
    all_zones = zone_ids | new_zone_ids
    for node in change.nodes:
        node_id = _text(node.get("id"))
        name = _text(node.get("name"))
        zone_id = _text(node.get("zone_id"))
        if not node_id or not name:
            problems.append((ERR_BAD_FIELD, "地点要有 id 和名字"))
            continue
        if node_id in node_ids or node_id in new_node_ids:
            problems.append((ERR_DUPLICATE, f"地点 id 撞了：{node_id}"))
        if zone_id and zone_id not in all_zones:
            problems.append((ERR_MISSING_ZONE, f"地点 {node_id} 指向的区域不存在：{zone_id}"))
        new_node_ids.add(node_id)

    all_nodes = node_ids | new_node_ids
    existing_edges = {
        _edge_ends(item) for item in edge_rows if any(_edge_ends(item))
    }
    # 世界里的连线默认双向：两个方向都算"已经有了"
    existing_pairs = {
        pair for left, right in existing_edges for pair in ((left, right), (right, left))
    }
    for edge in change.edges:
        left, right = _edge_ends(edge)
        if not left or not right:
            problems.append((ERR_BAD_EDGE, "连线要有两端"))
            continue
        for endpoint in (left, right):
            if endpoint not in all_nodes:
                problems.append((ERR_MISSING_NODE, f"连线指向不存在的地点：{endpoint}"))
        if (left, right) in existing_pairs:
            problems.append((ERR_DUPLICATE, f"这条连线已经有了：{left}↔{right}"))

    counts = {
        "zones": len(change.zones),
        "nodes": len(change.nodes),
        "edges": len(change.edges),
    }
    if problems:
        return ChangeReport(False, tuple(problems[:6]), "校验没过，什么都没改", counts)
    summary = (
        f"加 {counts['zones']} 个区域、{counts['nodes']} 个地点、{counts['edges']} 条连线"
    )
    return ChangeReport(True, (), summary, counts)


def apply(change: WorldChange, world: Mapping[str, Any] | None) -> tuple[dict[str, Any], ChangeReport]:
    """应用变更（**纯函数**）：返回新世界 + 回执。校验不过就返回原世界。"""

    report = validate(change, world)
    if not report.ok:
        return dict(world or {}), report
    result = json.loads(json.dumps(dict(world or {}), ensure_ascii=False))
    result.setdefault("zones", [])
    result.setdefault("nodes", [])
    result.setdefault("edges", [])
    for zone in change.zones:
        result["zones"].append(dict(zone))
    for node in change.nodes:
        row = dict(node)
        row.setdefault("x", 0)
        row.setdefault("y", 0)
        row.setdefault("icon", "dot")
        row.setdefault("prompt", "")
        result["nodes"].append(row)
    for edge in change.edges:
        left, right = _edge_ends(edge)
        result["edges"].append(_edge_row(left, right, index=len(result["edges"])))
    # 新加的地点必须可达：把第一个新地点接到"已有第一个地点"上（没有就跳过）
    if change.nodes and result["nodes"]:
        anchor = _text(result["nodes"][0].get("id"))
        first_new = _text(change.nodes[0].get("id"))
        existing = {_edge_ends(item) for item in result["edges"]}
        if anchor and first_new and (anchor, first_new) not in existing:
            result["edges"].append(
                _edge_row(anchor, first_new, index=len(result["edges"]))
            )
    revision = int(change.revision or 0) + 1
    report = ChangeReport(
        True,
        (),
        f"{report.summary}；世界版本 → {revision}",
        dict(report.counts),
    )
    return result, report


def preview(change: WorldChange, world: Mapping[str, Any] | None) -> dict[str, Any]:
    """预览：应用之后世界会变成什么样（不落盘）。"""

    after, report = apply(change, world)
    return {
        "ok": report.ok,
        "report": report.as_dict(),
        "before": {
            "zones": len((world or {}).get("zones") or ()),
            "nodes": len((world or {}).get("nodes") or ()),
            "edges": len((world or {}).get("edges") or ()),
        },
        "after": {
            "zones": len(after.get("zones") or ()),
            "nodes": len(after.get("nodes") or ()),
            "edges": len(after.get("edges") or ()),
        },
        "added": {
            "zones": [_text(item.get("id")) for item in change.zones],
            "nodes": [_text(item.get("id")) for item in change.nodes],
            "edges": [
                f"{left}↔{right}" for left, right in (_edge_ends(item) for item in change.edges)
            ],
        },
    }


def sample_botanical_garden() -> dict[str, Any]:
    """P5-3 的贯通样例：「新增植物园」（一个区域 + 两个地点 + 一条连线）。"""

    return {
        "title": "在家里那一带加一个植物园",
        "zones": [{"id": "garden", "name": "植物园", "icon": "leaf"}],
        "nodes": [
            {
                "id": "garden_gate",
                "name": "植物园门口",
                "zone_id": "garden",
                "prompt": "一排低矮的灌木，风里有青草味。",
            },
            {
                "id": "garden_greenhouse",
                "name": "温室",
                "zone_id": "garden",
                "prompt": "玻璃顶上凝着水汽，里面比外头暖。",
            },
        ],
        "edges": [{"from": "garden_gate", "to": "garden_greenhouse"}],
    }
