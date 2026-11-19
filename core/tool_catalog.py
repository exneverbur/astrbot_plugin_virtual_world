"""Agent 这一轮**真正要看**的工具说明书（P0-3）。

为什么单独一层：以前交给规划模型的是"宿主描述 + 参数字符串拼在一起"的一段话，
于是两件事说不清——

1. **参数是什么样**：必填还是可选、什么类型、留空会怎样。拼成一行的文字里，
   模型只能靠猜；
2. **这个工具到底在不在**：策略按名字放行了、宿主却没注册时，描述是空串，
   提示词里就剩一个光秃秃的 `- xxx` —— 模型就会开始编它的参数。

这里把两者都变成结构化数据：``params`` 里每一条都带 ``type`` / ``required`` /
``description``，注册表里没有的名字标 ``known=False``。渲染只负责拼人话，
判定一律看 :meth:`ToolSchema.known`。

不做的事（免得以后被当成承诺）：不执行、不校验调用参数、不替宿主补默认值——
那是执行器与宿主的事。这里只回答"说明书上写了什么"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ToolParam:
    """一个参数：名字、类型、要不要必填、一句人话。"""

    name: str
    type: str = ""
    required: bool = False
    description: str = ""

    def render(self) -> str:
        bits = [self.name]
        if self.type:
            bits.append(self.type)
        bits.append("必填" if self.required else "可选")
        head = "（".join([bits[0], "，".join(bits[1:]) + "）"])
        return f"{head}：{self.description}" if self.description else head


@dataclass(frozen=True)
class ToolSchema:
    """一个工具的说明书。``known=False`` 表示宿主里没有这个工具，**不可用**。"""

    name: str
    description: str = ""
    params: tuple[ToolParam, ...] = ()
    returns: str = ""
    known: bool = True
    tier: str = ""

    @classmethod
    def unknown(cls, name: str, tier: str = "") -> "ToolSchema":
        return cls(name=name, known=False, tier=tier)

    def render(self, *, limit: int = 220) -> str:
        """拼成提示词里的一行（超过 ``limit`` 就截断，别把预算吃光）。"""

        if not self.known:
            return "（宿主里没有这个工具，不可用；不要写它，也不要编它的参数）"
        text = _clip(self.description, limit)
        parts = [text] if text else []
        if self.params:
            params = "；".join(item.render() for item in self.params)
            parts.append(f"参数：{params}")
        else:
            parts.append("参数：（这个工具不需要参数）")
        if self.returns:
            parts.append(f"返回：{_clip(self.returns, 60)}")
        return "｜".join(parts)


def _clip(text: Any, limit: int) -> str:
    body = str(text or "").strip()
    return body if len(body) <= limit else body[:limit] + "…"


def _param_rows(schema: Mapping[str, Any] | None) -> tuple[ToolParam, ...]:
    """把 JSON Schema 那套 ``properties`` / ``required`` 拆成一行行参数。"""

    spec = dict(schema or {})
    properties = spec.get("properties")
    properties = dict(properties) if isinstance(properties, Mapping) else {}
    required = {str(item) for item in (spec.get("required") or [])}
    rows: list[ToolParam] = []
    for name, detail in properties.items():
        info = dict(detail) if isinstance(detail, Mapping) else {}
        type_name = str(info.get("type") or "").strip()
        if not type_name and isinstance(info.get("enum"), list):
            type_name = "枚举"
        description = str(info.get("description") or "").strip()
        if not description and isinstance(info.get("enum"), list):
            description = "可选：" + " / ".join(str(item) for item in info["enum"][:6])
        rows.append(
            ToolParam(
                name=str(name),
                type=type_name,
                required=str(name) in required,
                description=description,
            )
        )
    return tuple(rows)


def build_catalog(
    *,
    installed: Iterable[Any],
    wanted: Mapping[str, str] | None = None,
    tier_labels: Mapping[str, str] | None = None,
) -> dict[str, ToolSchema]:
    """按"这一轮允许的工具"建一份说明书。

    ``installed`` 是宿主报上来的工具（对象或 dict 都行，要有 ``name`` /
    ``description`` / ``parameters``）；``wanted`` 是名字 → 档位（执行器的策略结果）。
    返回顺序按档位排：只读在前，执行代码在后——模型天生爱先跑代码。
    """

    described: dict[str, ToolSchema] = {}
    order: dict[str, int] = {}
    for index, item in enumerate(installed):
        name = str(getattr(item, "name", "") or (item.get("name") if isinstance(item, Mapping) else "") or "")
        if not name:
            continue
        description = str(
            getattr(item, "description", "")
            or (item.get("description") if isinstance(item, Mapping) else "")
            or ""
        )
        parameters = getattr(item, "parameters", None)
        if parameters is None and isinstance(item, Mapping):
            parameters = item.get("parameters")
        described[name] = ToolSchema(
            name=name,
            description=description,
            params=_param_rows(parameters),
            returns=str(
                getattr(item, "returns", "")
                or (item.get("returns") if isinstance(item, Mapping) else "")
                or ""
            ),
        )
        order[name] = index
    labels = dict(tier_labels or {})
    tiers = list(labels)
    catalog: dict[str, ToolSchema] = {}
    wanted_map = dict(wanted or {})
    for name in sorted(
        wanted_map,
        key=lambda key: (
            tiers.index(str(wanted_map.get(key))) if str(wanted_map.get(key)) in tiers else 99,
            str(key),
        ),
    ):
        tier = str(wanted_map.get(name) or "")
        base = described.get(name)
        if base is None:
            catalog[name] = ToolSchema.unknown(name, tier=tier)
            continue
        catalog[name] = ToolSchema(
            name=name,
            description=base.description,
            params=base.params,
            returns=base.returns,
            known=True,
            tier=tier,
        )
    return catalog


def render_prompt_lines(catalog: Mapping[str, ToolSchema]) -> list[tuple[str, str]]:
    """给提示词的 ``(名字, 说明书)`` 列表；未知工具也会出现在里面（但写明不可用）。"""

    return [(name, schema.render()) for name, schema in dict(catalog or {}).items()]


def summarize(catalog: Mapping[str, ToolSchema]) -> dict[str, Any]:
    """给调试页的汇总：几个已知、几个未知、未知的都是谁。"""

    rows = list((catalog or {}).values())
    unknown = [row.name for row in rows if not row.known]
    with_params = [row.name for row in rows if row.known and row.params]
    return {
        "total": len(rows),
        "known": len([row for row in rows if row.known]),
        "unknown": len(unknown),
        "unknown_names": unknown,
        "with_params": len(with_params),
        "notes": (
            "参数说明来自宿主的工具定义；宿主没注册的工具在这里标成不可用，不会编参数"
        ),
    }
