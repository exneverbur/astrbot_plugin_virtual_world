"""动作按**用途**分组的唯一分类器（P0-5）。

两个问题分开答，别混成一根轴：

1. **用途**（这动作是用来干什么的）——交流互动 / 记忆与记录 / 生活活动 /
   信息查询 / 媒体与成果 / 安排与任务。它只影响"清单怎么排、看着顺不顺眼"，
   **不改变能不能做**；
2. **执行条件**（此刻能不能做）——可用 / 需移动 / 缺依赖 / 受限。它完全由
   真实定义、地点、权限和宿主依赖推出来，**分类不许碰它**。

为什么要有这个模块：以前提示词里的动作索引是一长串平铺的行，编辑器卡片按
"原版/扩展/互动"分——两边各分各的，模型看到的顺序和用户看到的顺序不是一回事。
现在两边都调这里，同一份定义在两边得到同一个用途。

映射规则（确定性，零额外模型调用）：

1. 定义上写了 ``use_purpose``（用户手填 / 后台整理给的建议）且认得 → 用它；
2. ``discovery.tags`` / ``when_to_use`` 里的关键词；
3. 动作 id / 工具流 / 类型兜底（``tool_flow=search`` → 信息查询，``llm_level=command``
   → 安排与任务…）；
4. 都认不出 → ``life``（生活活动）——绝大多数内置动作就是这一类，比"其他"有用。

自定义动作可以写任意 ``use_purpose``；认不出的会被归到 ``life`` 并在
:func:`purpose_of` 的返回值里如实带出 ``recognized=False``（页面据此提示，但不拦）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: 用途分组（有序：清单按这个顺序分组，前面的更常用）
PURPOSES: tuple[tuple[str, str], ...] = (
    ("talk", "交流互动"),
    ("record", "记忆与记录"),
    ("life", "生活活动"),
    ("lookup", "信息查询"),
    ("media", "媒体与成果"),
    ("plan", "安排与任务"),
)

PURPOSE_LABELS: dict[str, str] = dict(PURPOSES)

#: 关键词 → 用途。按顺序匹配，先命中的算数（"搜索"优先于"记录"）。
#: 关键词要**具体**：只写"记"会把"记在心里"的捡贝壳也算成记录类。
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lookup", ("搜索", "查一下", "查询", "检索", "天气", "新闻", "资料", "搜索网页", "查天气")),
    ("plan", ("日程", "提醒", "安排", "待办", "计划", "定时", "预约")),
    ("media", ("照片", "截图", "视频", "音频", "表情包", "生成图", "下载", "自拍", "出图")),
    ("record", ("笔记", "清单", "备忘", "回忆", "档案", "记住", "记录")),
    ("talk", ("说", "聊", "回", "抱", "亲", "点头", "安慰", "分享", "递")),
)

#: id 前缀规则（比关键词可靠，放最前面）：日程、成果、互动各自一大片
_ID_PREFIX: tuple[tuple[str, str], ...] = (
    ("schedule_", "plan"),
    ("remind", "plan"),
    ("todo", "plan"),
    ("take_photo", "media"),
    ("leg_photo", "media"),
    ("photo", "media"),
    ("selfie", "media"),
    ("record_video", "media"),
    ("find_meme", "media"),
    ("send_meme", "media"),
    ("poke", "talk"),
    ("pour_tea", "talk"),
    ("hug", "talk"),
    ("kiss", "talk"),
    ("pat", "talk"),
)

#: 动作 id / 字段的硬规则（比关键词更可靠，放前面）
_BY_ID: dict[str, str] = {
    "say": "talk",
    "reply": "talk",
    "think": "record",
    "remember": "record",
    "recall": "record",
    "search_web": "lookup",
    "check_weather": "lookup",
    "agent_task": "plan",
    "walk_to": "life",
    "sleep": "life",
    "nap": "life",
    "stare": "life",
    "find_meme": "media",
    "send_meme": "media",
}

#: 执行条件的四档
CONDITION_AVAILABLE = "available"
CONDITION_NEEDS_TRAVEL = "needs_travel"
CONDITION_MISSING_DEPENDENCY = "missing_dependency"
CONDITION_RESTRICTED = "restricted"

CONDITION_LABELS: dict[str, str] = {
    CONDITION_AVAILABLE: "当前可用",
    CONDITION_NEEDS_TRAVEL: "需移动",
    CONDITION_MISSING_DEPENDENCY: "缺依赖",
    CONDITION_RESTRICTED: "受限",
}


@dataclass(frozen=True)
class PurposeOf:
    """一条动作的用途判定结果。``recognized=False`` 表示是靠兜底归类的。"""

    key: str
    label: str
    recognized: bool = True
    reason: str = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _discovery_text(definition: Any) -> str:
    discovery = getattr(definition, "discovery", None)
    if discovery is None:
        return ""
    tags = getattr(discovery, "tags", None) or ()
    parts = [str(item) for item in tags]
    parts.append(_text(getattr(discovery, "when_to_use", "")))
    parts.append(_text(getattr(discovery, "summary", "")))
    return " ".join(part for part in parts if part)


def purpose_of(definition: Any, *, action_id: str = "") -> PurposeOf:
    """这条动作用途是什么（**确定性**：同一份定义永远得到同一个答案）。"""

    explicit = _text(getattr(definition, "use_purpose", ""))
    if explicit:
        if explicit in PURPOSE_LABELS:
            return PurposeOf(explicit, PURPOSE_LABELS[explicit], True, "定义里写了 use_purpose")
        # 认不出的自定义用途不拦，也不硬塞成"其他"——原样带出去让页面提示
        return PurposeOf(explicit, explicit, False, "自定义用途（不在内置分组里）")

    key = _text(action_id) or _text(getattr(definition, "id", ""))
    if key in _BY_ID:
        return PurposeOf(_BY_ID[key], PURPOSE_LABELS[_BY_ID[key]], True, "按动作 id 归口")
    for prefix, purpose in _ID_PREFIX:
        if key.startswith(prefix):
            return PurposeOf(purpose, PURPOSE_LABELS[purpose], True, f"按 id 前缀「{prefix}」")

    flow = _text(getattr(definition, "tool_flow", ""))
    if flow == "search":
        return PurposeOf("lookup", PURPOSE_LABELS["lookup"], True, "检索流水线")
    haystack = " ".join(
        part
        for part in (
            _text(getattr(definition, "name", "")),
            _text(getattr(definition, "description", "")),
            _discovery_text(definition),
            _text(getattr(definition, "category", "")),
        )
        if part
    )
    for purpose, words in _KEYWORDS:
        for word in words:
            if word and word in haystack:
                return PurposeOf(purpose, PURPOSE_LABELS[purpose], True, f"命中关键词「{word}」")
    if _text(getattr(definition, "llm_level", "")) == "command":
        # 指令型走到这里说明关键词都没命中：多半是"让她去触发某个插件"，归到安排与任务
        return PurposeOf("plan", PURPOSE_LABELS["plan"], True, "指令型（安排与任务）")
    return PurposeOf("life", PURPOSE_LABELS["life"], False, "认不出来，按生活活动兜底")


def condition_of(*, here: bool, remote: bool, dependency: str = "", restricted: bool = False) -> str:
    """执行条件（四档之一）。**只读真实状态**，分类本身不改变授权。

    ``dependency`` 是引擎算出来的那句中依赖说明（空 = 依赖没问题）；``restricted``
    由调用方按关系档位 / 权限给（例如"这一档还不能做"）。
    """

    if restricted:
        return CONDITION_RESTRICTED
    if _text(dependency) and ("不可用" in _text(dependency) or "没有" in _text(dependency)):
        return CONDITION_MISSING_DEPENDENCY
    if not here:
        return CONDITION_NEEDS_TRAVEL if remote else CONDITION_RESTRICTED
    if _text(dependency):
        return CONDITION_MISSING_DEPENDENCY
    return CONDITION_AVAILABLE


@dataclass(frozen=True)
class GroupedEntry:
    purpose: str
    label: str
    entries: tuple[tuple[str, str], ...]


def group_entries(
    rows: Iterable[tuple[str, str, Any]], *, max_empty_groups: int = 0
) -> list[GroupedEntry]:
    """把 ``(动作 id, 一行文本, 定义)`` 按用途分组，组内保持传入顺序。

    ``max_empty_groups`` 默认为 0：空组不输出（清单里不出现"这一组什么都没"）。
    预算永远在渲染那一层管——这里只分组，不裁剪。
    """

    buckets: dict[str, list[tuple[str, str]]] = {}
    for action_id, line, definition in rows:
        purpose = purpose_of(definition, action_id=action_id).key
        buckets.setdefault(purpose, []).append((str(action_id), str(line)))
    ordered: list[GroupedEntry] = []
    for key, label in PURPOSES:
        entries = buckets.get(key) or []
        if not entries and max_empty_groups <= 0:
            continue
        ordered.append(GroupedEntry(key, label, tuple(entries)))
    # 自定义用途排在最后（认得的内置分组在前，方便扫）
    for key, entries in buckets.items():
        if key in PURPOSE_LABELS:
            continue
        ordered.append(GroupedEntry(key, key, tuple(entries)))
    return ordered


def summarize(counts: Mapping[str, int]) -> list[dict[str, Any]]:
    """给页面用的小结：每个用途几条（只列有内容的）。"""

    rows: list[dict[str, Any]] = []
    for key, label in PURPOSES:
        value = int(counts.get(key, 0) or 0)
        if value:
            rows.append({"purpose": key, "label": label, "count": value})
    for key, value in counts.items():
        if key not in PURPOSE_LABELS and int(value or 0) > 0:
            rows.append({"purpose": str(key), "label": str(key), "count": int(value)})
    return rows


def purposes_payload() -> list[dict[str, str]]:
    """给编辑器的分组清单（顺序与提示词一致）。"""

    return [{"key": key, "label": label} for key, label in PURPOSES]


def order_of(key: str) -> int:
    """用途在内置顺序里的位置（认不出排最后）——排序用。"""

    keys = [item for item, _label in PURPOSES]
    return keys.index(key) if key in keys else len(keys)


def is_known(key: str) -> bool:
    return str(key or "") in PURPOSE_LABELS


def rows_with_purpose(rows: Sequence[tuple[str, Any]]) -> list[dict[str, Any]]:
    """``(动作 id, 定义)`` → 带用途与条件的行（编辑器卡片直接用这份）。"""

    result: list[dict[str, Any]] = []
    for action_id, definition in rows:
        purpose = purpose_of(definition, action_id=str(action_id))
        result.append(
            {
                "id": str(action_id),
                "purpose": purpose.key,
                "purpose_label": purpose.label,
                "purpose_recognized": bool(purpose.recognized),
                "purpose_reason": purpose.reason,
            }
        )
    return result
