"""内容迁移：只**检测**，不落盘；用户勾过才改。

为什么要单独一个模块
--------------------

这一版之前，`parse_world()` 每读一次盘就把「还等于老默认值」的字段顺手改成新默认值，
然后 append 一条 warning。可是 `ConfigStore.save_world()` 写回文件的是**入参原始 dict**，
内存里那份改动从来落不到盘上 —— 于是同一条提醒每次进编辑器、每次保存都重新出现一次。

现在拆成两半：

* :func:`detect_all` 是**纯函数**：同一份世界配置永远得到同一份清单，不看文件、不写盘；
* :func:`apply_ids` 只按清单里的 `field_path` 逐个写值（深拷贝后写），绝不整份重建 ——
  整份重建会丢掉我们不认识的键，那正是 `save_world` 当初的教训。

日程（``schedules.json``）不在 ``world.json`` 里，是**另一个文件**：带日程的条目自己标着
``target="schedules"``，得用 :func:`apply_batch` 拿到两份新配置，再分别交给
``save_world`` / ``save_schedules`` 落盘。

三种判定（用户 2026-10-23 追加的三方对比）
-----------------------------------------

===========  ==================================  ==================
盘上的值      判定                                 弹窗里
===========  ==================================  ==================
== 老默认     我们发过的默认值变了，跟着走不丢东西   ``upgrade``，默认勾选
== 新默认     无事                                 不出现
都不是        用户自己调过（或不知从哪来的值）      ``conflict``，默认不勾
===========  ==================================  ==================

文字类（内置动作的描述、事件题材表）即使命中 ``upgrade`` 也默认不勾：换掉的是用户看得见
的内容，让他自己决定。``reversible=False`` 表示「应用之后没法在编辑器里一键改回去」，
前端据此默认不勾。

跳过记录
--------

``data/migrations_skipped.json`` 按 **目标路径 + 新值** 记（:func:`skipped_key`）：
同一个问题记一次就不再问；我们把默认值再改一版（新值变了）就会重新问一次。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .defaults import DEFAULT_AGENT_MAX_STEPS, default_actions, default_profile
from .models import (
    BUILTIN_ACTION_UPDATES,
    DEFAULT_SCHEDULE_NAMES,
    DEFAULT_SCHEDULE_NOTES,
    MORNING_SCHEDULE_ID,
    REQUIRED_BUILTIN_ACTIONS,
    TUNED_DEFAULT_UPGRADES,
    WorldConfig,
    _search_from_actions,
)

SKIPPED_FILE = "migrations_skipped.json"

# 「调过的默认值」的中文标签：表里的键是点路径，`models.FIELD_LABELS` 只覆盖了一部分
# （它给的是「想念涨得多快（每分钟）」这类界面标题，这里补上缺的那几个）。
MIGRATION_LABELS: dict[str, str] = {
    "profile.miss_growth_per_min": "想念涨得多快（每分钟）",
    "profile.miss_cooldown_min_minutes": "两次主动找你的最短间隔（分钟）",
    "profile.miss_cooldown_max_minutes": "刚聊完之后最久隔多久才想你（分钟）",
    "profile.miss_push_threshold": "想念到多少她才主动去找你",
    "state_dynamics.desire_relief": "被亲近一次欲求落多少",
    "state_dynamics.valence_decay_per_min": "效价每分钟回落多少",
    "context.chat_elsewhere_lines": "「你在别处同时听到的」给几行",
    "media.download_concurrency": "同时下载几张图",
    "media.fetch_timeout_seconds": "下载一张图的超时（秒）",
    "reply_style.typing_delay_per_char": "每个字打多久（秒）",
    "reply_style.typing_delay_max": "一条消息最多打多久（秒）",
    # 追问降噪 2026-10-26：等 3 分钟太急、追 2 次太啰嗦。
    "follow_up.wait_minutes": "问完等多久没人接才追一句（分钟）",
    "follow_up.max_times": "同一句最多追几次",
}

#: Agent 的时间预算：每一档都是我们发出去过的默认值（整对相等才算）
AGENT_BUDGET_STALE: tuple[tuple[dict[str, float], str], ...] = (
    (
        {"query_total_seconds": 6.0, "request_timeout_seconds": 5.0},
        "最初那版（单次请求 5 秒 / 整件事 6 秒）",
    ),
    (
        {"query_total_seconds": 180.0, "request_timeout_seconds": 60.0},
        "上一版（单次请求 60 秒 / 整件事 180 秒）",
    ),
)
#: 当前这一档（`core/defaults.py` 里的 agent 段是同一份口径）
AGENT_BUDGET_TARGET: dict[str, float] = {
    "query_total_seconds": 600.0,
    "request_timeout_seconds": 60.0,
}
#: 最初那版发出去的步数默认值：只有正好等于它的才算「还是我们发的默认」
AGENT_OLD_MAX_STEPS = 5

#: 最初那版的事件题材表：**一笔不差**地对上才算旧默认（用户加过/改过就不动）
OLD_EVENT_GENRES: tuple[tuple[str, float, str], ...] = (
    ("日常小事", 40, "做饭、找东西、东西坏了、收快递、打扫"),
    ("人际", 20, "被误解、拌嘴、被冷落、被搭讪、被人跟着、遇到不讲理的"),
    ("情绪与身体", 15, "失眠、头疼、太累、心里发闷、突然想起旧事"),
    ("意外与麻烦", 12, "丢东西、下雨没带伞、被洒了一身、走错路、手机没电"),
    ("好奇与发现", 8, "看到奇怪的东西、翻到旧物、听到传闻、想弄明白一件事"),
    ("游戏与想象", 3, "打游戏卡关、看剧上头、脑补出一段剧情"),
    ("外面的事", 2, "逛街、坐车、排队、去陌生地方"),
)

KIND_UPGRADE = "upgrade"
KIND_CONFLICT = "conflict"
KIND_GROUP = "group"

#: 这条迁移写到哪个文件。日程在 **另一个文件**（``schedules.json``），不在 ``world.json`` 里
#: （见 ``core/config_store.py`` 的 ``WORLD_FILE`` / ``SCHEDULES_FILE``），所以清单里必须
#: 带上归属，apply 才知道该交给 ``save_world`` 还是 ``save_schedules``。
TARGET_WORLD = "world"
TARGET_SCHEDULES = "schedules"


# ---------------------------------------------------------------------------
# 清单条目
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationItem:
    """一条等待用户确认的迁移。

    Attributes:
        id: 稳定标识（前端勾选、apply/skip 都用它）。
        title: 分组标题（同一类迁移共用一个，弹窗里按它分段）。
        field_path: 目标路径（点路径；列表按语义 id 找，见 :func:`read_path`）。
        before: 盘上的当前值（``conflict`` 时就是用户自己调出来的那个值）。
        after: 应用之后的值。
        reversible: 应用后还能不能在编辑器里改回去（文字类换掉就回不去了）。
        kind: ``upgrade``（还等于老默认）/ ``conflict``（用户改过）/ ``group``（一条改多个路径）。
        label: 中文标签。
        note: 给用户看的补充说明（``conflict`` 的「你改过：…」）。
        writes: 要写的 ``路径 -> 值``；留空表示只写 ``field_path: after`` 这一处。
        default_on: 弹窗里是否默认勾选（数值/行为型才默认勾）。
        target: 写哪个文件（``world`` = world.json / ``schedules`` = schedules.json）。
        appends: 要**追加**到某个列表尾部的条目（``列表路径 -> [条目, ...]``）。
            与 ``writes`` 的区别：``writes`` 换掉整个列表，``appends`` 只在列表末尾
            加还缺的那几条（按 ``id`` 去重），已有条目一个字节都不动、顺序也不变。
            用来补回被删掉的内置动作 / 空掉的关系表——那些都不是"改某个值"。
    """

    id: str
    title: str
    field_path: str
    before: Any
    after: Any
    reversible: bool
    kind: str
    label: str
    note: str = ""
    writes: dict[str, Any] = field(default_factory=dict)
    default_on: bool = False
    target: str = TARGET_WORLD
    appends: dict[str, list[Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """给 ``GET /migrations`` 用的一份普通 dict（值原样，不转字符串）。"""

        return {
            "id": self.id,
            "title": self.title,
            "field_path": self.field_path,
            "before": self.before,
            "after": self.after,
            "reversible": self.reversible,
            "kind": self.kind,
            "label": self.label,
            "note": self.note,
            "default_on": bool(self.default_on),
        }

    def targets(self) -> dict[str, Any]:
        """这一条要写的路径与值（``group`` 用 ``writes``，其余就是单点）。

        ``appends`` 类型的条目**没有**要写的单点：它的 ``before`` / ``after`` 只是给人
        看的说明文字，写盘走 :meth:`append_targets`。这里返回空 dict，免得谁顺手把
        ``{"actions": "补回 3 个内置动作"}`` 当成值写进去。
        """

        if self.appends:
            return {}
        if self.writes:
            return dict(self.writes)
        return {self.field_path: self.after}

    def append_targets(self) -> dict[str, list[Any]]:
        """这一条要追加到哪些列表、追加什么（按 ``id`` 去重，已有的不动）。"""

        return {path: list(entries) for path, entries in self.appends.items()}


# ---------------------------------------------------------------------------
# 路径读写（只碰目标那一条，绝不整份重建）
# ---------------------------------------------------------------------------

_INDEXED = re.compile(r"^(?P<key>[^\[\]]+)\[(?P<field>[A-Za-z_]+)=(?P<value>[^\]]+)\]$")


def split_path(path: str) -> list[str]:
    """``actions[id=pour_tea].name`` → ``["actions[id=pour_tea]", "name"]``。"""

    return [part for part in str(path).split(".") if part]


def _locate(node: Any, segment: str) -> tuple[bool, Any, Any]:
    """在一层里找到 ``segment`` 指向的东西，返回 ``(找到没, 容器, 键)``。

    ``key[id=xxx]`` 表示「列表里 id 等于 xxx 的那一项」——按语义标识找，**不按下标**：
    用户在列表中间插一条，也不会整表错位。
    """

    match = _INDEXED.match(segment)
    if match is None:
        if not isinstance(node, dict) or segment not in node:
            return False, node, segment
        return True, node, segment
    if not isinstance(node, dict):
        return False, node, match.group("key")
    bucket = node.get(match.group("key"))
    if not isinstance(bucket, list):
        return False, node, match.group("key")
    wanted = match.group("field")
    needle = match.group("value")
    for index, entry in enumerate(bucket):
        if isinstance(entry, dict) and str(entry.get(wanted)) == needle:
            return True, bucket, index
    return False, node, match.group("key")


def read_path(raw: Any, path: str) -> tuple[bool, Any]:
    """按路径读值：返回 ``(找到没, 值)``。"""

    node = raw
    for segment in split_path(path):
        found, container, key = _locate(node, segment)
        if not found:
            return False, None
        try:
            node = container[key]
        except (IndexError, KeyError, TypeError):
            return False, None
    return True, node


def write_path(raw: dict[str, Any], path: str, value: Any) -> bool:
    """把 ``value`` 写到 ``path`` 指的位置，只创建这条路径上缺的 dict。

    返回写成功没有。列表里的项必须已经存在（按 id 找得到）—— 找不到就跳过，
    免得把「用户删掉的条目」又变出来。
    """

    segments = split_path(path)
    if not segments or not isinstance(raw, dict):
        return False
    node: Any = raw
    for position, segment in enumerate(segments):
        last = position == len(segments) - 1
        match = _INDEXED.match(segment)
        if match is None:
            if not isinstance(node, dict):
                return False
            if last:
                node[segment] = value
                return True
            child = node.get(segment)
            if not isinstance(child, (dict, list)):
                child = {}
                node[segment] = child
            node = child
            continue
        if not isinstance(node, dict):
            return False
        bucket = node.get(match.group("key"))
        if not isinstance(bucket, list):
            return False
        wanted, needle = match.group("field"), match.group("value")
        index = next(
            (
                offset
                for offset, entry in enumerate(bucket)
                if isinstance(entry, dict) and str(entry.get(wanted)) == needle
            ),
            None,
        )
        if index is None:
            return False
        if last:
            return False  # 「列表里的整个条目」不是可写的叶子：要写到它下面的字段
        node = bucket[index]
    return False


def append_path(raw: dict[str, Any], path: str, entries: Iterable[Any]) -> bool:
    """把 ``entries`` **追加**到 ``path`` 指向的列表末尾，返回写成功没有。

    和 :func:`write_path` 的区别：这里只做"补上还缺的"，已经有的一个字节都不动 ——
    用户改过的动作描述、列表原来的顺序、我们不认识的键全部原样保留。

    * 列表按条目里的 ``id`` 去重（没有 ``id`` 的条目——比如关系表的 ``name``——
      按整条内容去重）；已经在列表里的跳过。
    * 路径上的中间层必须是 dict（缺了就建一个），**绝不去改**用户删掉的其它段。
    * 目标不是列表（手改坏了的 JSON）就不动它，返回 ``False`` 让这次应用记成失败——
      宁可报错，也别把 ``actions`` 写成一个字符串。
    """

    segments = split_path(path)
    if not segments or not isinstance(raw, dict):
        return False
    node: Any = raw
    for segment in segments[:-1]:
        if not isinstance(node, dict):
            return False
        child = node.get(segment)
        if not isinstance(child, dict):
            return False  # 中间层不是 dict（比如被手改成了列表）：不猜，直接报失败
        node = child
    if not isinstance(node, dict):
        return False
    leaf = segments[-1]
    bucket = node.get(leaf)
    if bucket is None:
        bucket = []
        node[leaf] = bucket
    if not isinstance(bucket, list):
        return False
    present_ids = {
        str(entry.get("id"))
        for entry in bucket
        if isinstance(entry, dict) and str(entry.get("id") or "")
    }
    present_bodies = {
        json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str) for entry in bucket
    }
    for entry in entries or ():
        entry_id = str(entry.get("id") or "") if isinstance(entry, dict) else ""
        body = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
        if entry_id:
            if entry_id in present_ids:
                continue
        elif body in present_bodies:
            continue
        bucket.append(copy.deepcopy(entry))
        if entry_id:
            present_ids.add(entry_id)
        present_bodies.add(body)
    return True


# ---------------------------------------------------------------------------
# 数字与类型
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_declared_type(current: Any, value: float) -> Any:
    """按盘上那份的类型写回（表里为了统一写成 float）。

    不这么做的话 ``chat_elsewhere_lines`` 会存成 ``12.0``：字段是 int，pydantic 序列化
    时会把它变成浮点，编辑器里跟着显示成小数。
    """

    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int):
        return int(value)
    if isinstance(current, str):
        return str(value)
    return value


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


# ---------------------------------------------------------------------------
# 检测（纯函数）
# ---------------------------------------------------------------------------


def _tuned_items(raw: dict[str, Any]) -> list[MigrationItem]:
    items: list[MigrationItem] = []
    for path, (before, after) in TUNED_DEFAULT_UPGRADES.items():
        found, current = read_path(raw, path)
        if not found:
            continue
        current_number = _number(current)
        if current_number is None:
            continue  # 盘上不是数字（手改坏了）：不猜、也不提醒
        if current_number == float(after):
            continue  # 已经是新默认：这一项上没什么可说的
        kind = KIND_UPGRADE if current_number == float(before) else KIND_CONFLICT
        label = MIGRATION_LABELS.get(path, path)
        items.append(
            MigrationItem(
                id=f"tuned:{path}",
                title="调过的默认值",
                field_path=path,
                before=current,
                after=_as_declared_type(current, after),
                reversible=True,
                kind=kind,
                label=label,
                note=() if kind == KIND_UPGRADE else _conflict_note(current, after),
                default_on=kind == KIND_UPGRADE,
            )
        )
    return items


def _conflict_note(current: Any, after: Any) -> str:
    return f"你改过：{_fmt(current)}；新默认：{_fmt(after)}"


def _agent_items(raw: dict[str, Any]) -> list[MigrationItem]:
    section = raw.get("agent")
    if not isinstance(section, dict):
        return []  # 整个 agent 段都没有：那是结构补全（config_store 直接写），不是内容迁移
    items: list[MigrationItem] = []
    stale_label = ""
    for pair, label in AGENT_BUDGET_STALE:
        if all(_number(section.get(name)) == value for name, value in pair.items()):
            stale_label = label
            break
    current_query = _number(section.get("query_total_seconds"))
    current_timeout = _number(section.get("request_timeout_seconds"))
    pair_is_target = current_query == AGENT_BUDGET_TARGET["query_total_seconds"] and (
        current_timeout == AGENT_BUDGET_TARGET["request_timeout_seconds"]
    )
    if stale_label:
        items.append(
            MigrationItem(
                id="agent:budgets",
                title="Agent 任务预算",
                field_path="agent",
                before=stale_label,
                after="单次请求 60 秒 / 整件事 10 分钟",
                reversible=True,
                kind=KIND_GROUP,
                label="Agent 的时间预算",
                note="一档比一档长：最初那版连一次规划都等不完。",
                writes={
                    f"agent.{name}": value
                    for name, value in AGENT_BUDGET_TARGET.items()
                },
                default_on=True,
            )
        )
    elif not pair_is_target and None not in (current_query, current_timeout):
        items.append(
            MigrationItem(
                id="agent:budgets",
                title="Agent 任务预算",
                field_path="agent",
                before=f"单次请求 {_fmt(current_timeout)} 秒 / 整件事 {_fmt(current_query)} 秒",
                after="单次请求 60 秒 / 整件事 10 分钟",
                reversible=True,
                kind=KIND_CONFLICT,
                label="Agent 的时间预算",
                note=(
                    f"你改过：单次请求 {_fmt(current_timeout)} 秒 / "
                    f"整件事 {_fmt(current_query)} 秒；"
                    "新默认：单次请求 60 秒 / 整件事 10 分钟"
                ),
                writes={
                    f"agent.{name}": value
                    for name, value in AGENT_BUDGET_TARGET.items()
                },
                default_on=False,
            )
        )
    steps = _number(section.get("max_steps"))
    if steps == AGENT_OLD_MAX_STEPS:
        items.append(
            MigrationItem(
                id="agent:max_steps",
                title="Agent 任务预算",
                field_path="agent.max_steps",
                before=section.get("max_steps"),
                after=DEFAULT_AGENT_MAX_STEPS,
                reversible=True,
                kind=KIND_UPGRADE,
                label="Agent 一次任务最多几步",
                note="一步 = 一次工具调用，5 步两步就掐断。",
                default_on=True,
            )
        )
    elif steps is not None and steps != float(DEFAULT_AGENT_MAX_STEPS):
        items.append(
            MigrationItem(
                id="agent:max_steps",
                title="Agent 任务预算",
                field_path="agent.max_steps",
                before=section.get("max_steps"),
                after=DEFAULT_AGENT_MAX_STEPS,
                reversible=True,
                kind=KIND_CONFLICT,
                label="Agent 一次任务最多几步",
                note=_conflict_note(section.get("max_steps"), DEFAULT_AGENT_MAX_STEPS),
                default_on=False,
            )
        )
    return items


def _old_genres(raw: dict[str, Any]) -> list[dict[str, Any]] | None:
    """盘上的题材表**一笔不差**地等于最初那版时返回它，否则 ``None``。"""

    events = raw.get("events")
    if not isinstance(events, dict):
        return None
    genres = events.get("genres")
    if not isinstance(genres, list) or len(genres) != len(OLD_EVENT_GENRES):
        return None
    for entry, (name, weight, examples) in zip(genres, OLD_EVENT_GENRES):
        if not isinstance(entry, dict):
            return None
        if set(entry.keys()) != {"name", "weight", "examples"}:
            return None
        if str(entry.get("name") or "") != name:
            return None
        if str(entry.get("examples") or "") != examples:
            return None
        if _number(entry.get("weight")) != float(weight):
            return None
    return genres


def _fresh_genres() -> dict[str, Any]:
    """当前这一版的题材表（``DEFAULT_WORLD`` 里没有 events 段，得问模型）。"""

    events = WorldConfig().events.model_dump(mode="json")
    return {
        "genres": copy.deepcopy(events.get("genres") or []),
        "genre_scale": events.get("genre_scale") or "",
    }


def _genre_items(raw: dict[str, Any]) -> list[MigrationItem]:
    events = raw.get("events")
    if not isinstance(events, dict):
        return []
    genres = events.get("genres")
    if not isinstance(genres, list) or not genres:
        return []
    fresh = _fresh_genres()
    fresh_names = [str(item.get("name") or "") for item in fresh["genres"]]
    current_names = [str(item.get("name") or "") for item in genres if isinstance(item, dict)]
    writes: dict[str, Any] = {"events.genres": fresh["genres"]}
    if not str(events.get("genre_scale") or "").strip() and fresh["genre_scale"]:
        writes["events.genre_scale"] = fresh["genre_scale"]
    old = _old_genres(raw)
    if old is not None:
        return [
            MigrationItem(
                id="events:genres",
                title="事件题材表",
                field_path="events.genres",
                before=f"最初那版（{len(old)} 类，一个权重管三档）",
                after=f"按档位分权的一版（{len(fresh['genres'])} 类）",
                reversible=False,
                kind=KIND_GROUP,
                label="事件题材表",
                note="大事件现在只从人际 / 意外 / 危险 / 机会 / 旧账 / 外面里抽。",
                writes=writes,
                default_on=False,
            )
        ]
    if current_names == fresh_names:
        return []  # 已经是新的一版
    return [
        MigrationItem(
            id="events:genres",
            title="事件题材表",
            field_path="events.genres",
            before=f"你现在的 {len(genres)} 类",
            after=f"按档位分权的一版（{len(fresh['genres'])} 类）",
            reversible=False,
            kind=KIND_CONFLICT,
            label="事件题材表",
            note=f"你改过：{len(genres)} 类题材；新默认：{len(fresh['genres'])} 类。换掉会丢你写的例子。",
            writes=writes,
            default_on=False,
        )
    ]


def _action_items(raw: dict[str, Any]) -> list[MigrationItem]:
    """内置动作的文案也按「老内置值 / 新内置值 / 用户自己写过的」三方比。"""

    actions = raw.get("actions")
    if not isinstance(actions, list):
        return []
    by_id = {
        str(item.get("id")): item
        for item in actions
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    field_labels = {"name": "名字", "template": "发出的动作描述", "description": "给模型看的说明"}
    items: list[MigrationItem] = []
    for action_id, fields in BUILTIN_ACTION_UPDATES.items():
        entry = by_id.get(action_id)
        if entry is None:
            continue
        for name, (old, new) in fields.items():
            if name not in entry:
                continue
            current = str(entry.get(name) or "")
            if current == new:
                continue
            kind = KIND_UPGRADE if current == old else KIND_CONFLICT
            items.append(
                MigrationItem(
                    id=f"action:{action_id}.{name}",
                    title="内置动作描述更新",
                    field_path=f"actions[id={action_id}].{name}",
                    before=current,
                    after=new,
                    reversible=False,
                    kind=kind,
                    label=f"{action_id} 的{field_labels.get(name, name)}",
                    note=() if kind == KIND_UPGRADE else _conflict_note(current, new),
                    default_on=False,  # 换掉的是用户看得见的内容：一律不默认勾
                )
            )
    return items


# ---------------------------------------------------------------------------
# 补回类：盘上少了几条内置动作 / 画像的两张表空着 / 联网搜索还挂在动作上
# ---------------------------------------------------------------------------
#
# 这几件以前都是 `parse_world` 里「改内存 + append 一条 warning」：读一次盘补一次，
# 而 `save_world` 写回去的是入参原始 dict，补的东西从来落不到盘上 —— 于是那条提示
# 每进一次编辑器、每保存一次就重新出现一次，用户只能「改一下描述再改回来保存」骗过去
# （那次保存顺手把整份动作表写回了盘）。T17 的规矩：**检测归检测，写盘要用户点**。


def _missing_builtin_items(raw: dict[str, Any]) -> list[MigrationItem]:
    """盘上的动作表缺了几个内置动作（用户报的「提示消不掉」就是这一条）。"""

    actions = raw.get("actions")
    if not isinstance(actions, list):
        return []  # 整个动作表都没有：那是结构补全（config_store 直接写），不是内容迁移
    have = {
        str(entry.get("id"))
        for entry in actions
        if isinstance(entry, dict) and str(entry.get("id") or "")
    }
    by_id: dict[str, dict[str, Any]] = {}
    for entry in default_actions():
        if isinstance(entry, dict) and entry.get("id"):
            by_id[str(entry["id"])] = entry
    missing = [
        action_id
        for action_id in REQUIRED_BUILTIN_ACTIONS
        if action_id not in have and action_id in by_id
    ]
    if not missing:
        return []
    labels = [str(by_id[action_id].get("name") or action_id) for action_id in missing]
    return [
        MigrationItem(
            id="actions:missing_builtins",
            title="缺的内置动作",
            field_path="actions",
            before="动作表里没有：" + "、".join(labels),
            after=f"按默认定义补上 {len(missing)} 个：" + "、".join(labels),
            reversible=True,
            kind=KIND_UPGRADE,
            label=f"补回 {len(missing)} 个内置动作",
            note=(
                "内置动作引擎有专门逻辑（说话、走路、睡觉、日程、自拍……），可以停用、"
                "不能删除。补回的是出厂那一份：同 id 的动作不会重叠，你自己改过的字段也不动，"
                "追加在动作表末尾。不想要就跳过，之后不再问。"
            ),
            appends={"actions": [by_id[action_id] for action_id in missing]},
            default_on=True,
        )
    ]


def _default_bond_items(raw: dict[str, Any]) -> list[MigrationItem]:
    """老存档的默认关系还是「陌生人」、而且不是用户亲手选的。"""

    profile = raw.get("profile")
    if not isinstance(profile, dict):
        return []  # 整段没有：结构补全会写盘，不在这里管
    if str(profile.get("default_bond") or "") != "陌生人" or profile.get("bond_chosen"):
        return []
    writes: dict[str, Any] = {
        "profile.default_bond": "群友",
        "profile.bond_chosen": True,
    }
    bonds = profile.get("bonds")
    if isinstance(bonds, list) and bonds:
        # 老默认表里「陌生人」还带着 initial=True，ProfileConfig 会按那个标记把
        # default_bond 又改回陌生人：一起搬，只有「群友」保留初始标记。
        moved: list[Any] = []
        for entry in bonds:
            if not isinstance(entry, dict):
                moved.append(entry)
                continue
            moved.append({**entry, "initial": str(entry.get("name") or "") == "群友"})
        writes["profile.bonds"] = moved
    return [
        MigrationItem(
            id="profile:default_bond",
            title="用户画像",
            field_path="profile.default_bond",
            before="陌生人",
            after="群友",
            reversible=True,
            kind=KIND_UPGRADE,
            label="默认关系：陌生人 → 群友",
            note=(
                "老默认是「陌生人」，一上来全是客气话。这一项不是你亲手选的，所以问你一句；"
                "应用后还能在画像里改回去。"
            ),
            writes=writes,
            default_on=True,
        )
    ]


def _profile_items(raw: dict[str, Any]) -> list[MigrationItem]:
    """关系表 / 亲密度分级被清空（画像没法判断「能做什么、不能做什么」）。"""

    profile = raw.get("profile")
    if not isinstance(profile, dict):
        return []
    defaults = default_profile()
    appends: dict[str, list[Any]] = {}
    names: list[str] = []
    bonds = profile.get("bonds")
    if isinstance(bonds, list) and not bonds:
        fresh = [entry for entry in (defaults.get("bonds") or []) if isinstance(entry, dict)]
        if fresh:
            appends["profile.bonds"] = fresh
            names.append(f"关系表（{len(fresh)} 档）")
    levels = profile.get("levels")
    if isinstance(levels, list) and not levels:
        fresh = [entry for entry in (defaults.get("levels") or []) if isinstance(entry, dict)]
        if fresh:
            appends["profile.levels"] = fresh
            names.append(f"亲密度分级（{len(fresh)} 档）")
    if not appends:
        return []
    return [
        MigrationItem(
            id="profile:bonds_levels",
            title="用户画像",
            field_path="profile",
            before="空着：" + "、".join(names),
            after="按默认补回：" + "、".join(names),
            reversible=True,
            kind=KIND_UPGRADE,
            label="补回默认的" + "、".join(names),
            note=(
                "画像要靠关系表和分级判断「能做什么、不能做什么」；空着的时候引擎只能拿默认表"
                "顶上。补回的是出厂那几档，追加在列表末尾。不想要就跳过，之后不再问。"
            ),
            appends=appends,
            default_on=True,
        )
    ]


def _search_items(raw: dict[str, Any]) -> list[MigrationItem]:
    """联网搜索的设置还挂在 ``search_web`` 动作上（新版挪到了全局 ``search``）。"""

    if "search" in raw:
        return []
    moved = _search_from_actions(raw.get("actions") or [])
    if not moved:
        return []
    return [
        MigrationItem(
            id="search:from_actions",
            title="联网搜索",
            field_path="search",
            before="检索设置还挂在「联网搜索」这个动作上",
            after="搬到全局设置（高级功能 → 联网搜索）",
            reversible=True,
            kind=KIND_UPGRADE,
            label="联网搜索设置搬到全局",
            note="动作上那几个检索字段（工具、档位、篇数）搬到全局，老动作本身不动。",
            writes={"search": moved},
            default_on=True,
        )
    ]


# ---------------------------------------------------------------------------
# 日程（schedules.json）：默认那三条的描述、早安那句「说一句早安」
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """去掉两头的空白；``None`` 当空串。"""

    return str(value if value is not None else "").strip()


def _schedule_entries(raw_schedules: Any) -> dict[str, dict[str, Any]]:
    """``schedules.json`` 里按 id 索引的原始条目（重复 id 只认第一个）。"""

    if not isinstance(raw_schedules, dict):
        return {}
    schedules = raw_schedules.get("schedules")
    if not isinstance(schedules, list):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for entry in schedules:
        if not isinstance(entry, dict):
            continue
        schedule_id = _text(entry.get("id"))
        if schedule_id and schedule_id not in entries:
            entries[schedule_id] = entry
    return entries


def _has_say_step(entry: dict[str, Any]) -> bool:
    chain = entry.get("action_chain")
    if not isinstance(chain, list):
        return False
    return any(
        isinstance(step, dict) and _text(step.get("type")) == "say" for step in chain
    )


def _morning_say_wanted(entry: dict[str, Any]) -> bool:
    """早安那条要不要补一句「说一句早安」（子项 ``schedules:morning_say``）。

    只在「这条日程还是我们发的那份」时问：描述空着，或者只等于我们补上去的那份
    （先应用了 ``schedules:notes`` 也还能单独补这一句）。用户自己写过描述的，
    这条日程就是他自己在管，不拿动作去烦他。
    """

    if _has_say_step(entry):
        return False
    note = _text(entry.get("note"))
    return not note or note == DEFAULT_SCHEDULE_NOTES.get(MORNING_SCHEDULE_ID)


def _schedule_items(raw_schedules: Any) -> list[MigrationItem]:
    """默认日程的「补描述」与「早安补一句」两条迁移。

    以前这两件事在 ``parse_schedules`` 里自动改内存 + 每次读盘都 warning 一遍
    （改了也不落盘，于是永远重来）——T17 要消灭的正是这个模式。现在拆成待确认的
    迁移：描述和那句早安是两件独立的事，用户可以只补描述、不要那句早安。
    """

    entries = _schedule_entries(raw_schedules)
    if not entries:
        return []
    items: list[MigrationItem] = []

    writes: dict[str, Any] = {}
    described: list[str] = []
    renamed: list[str] = []
    for schedule_id, note in DEFAULT_SCHEDULE_NOTES.items():
        entry = entries.get(schedule_id)
        if entry is None or _text(entry.get("note")):
            continue
        writes[f"schedules[id={schedule_id}].note"] = note
        label = DEFAULT_SCHEDULE_NAMES.get(schedule_id) or schedule_id
        described.append(label)
        if not _text(entry.get("name")) and DEFAULT_SCHEDULE_NAMES.get(schedule_id):
            writes[f"schedules[id={schedule_id}].name"] = DEFAULT_SCHEDULE_NAMES[schedule_id]
            renamed.append(label)
    if writes:
        renamed_note = (
            "（" + "、".join(renamed) + " 连名字都没填，一并补上。）" if renamed else ""
        )
        items.append(
            MigrationItem(
                id="schedules:notes",
                title="默认日程的描述",
                field_path="schedules",
                before="没写描述：" + "、".join(described),
                after="补上我们发的那份默认描述",
                reversible=True,
                kind=KIND_UPGRADE,
                label="默认日程补描述：" + "、".join(described),
                note=(
                    "早先那版默认日程没写「这件事是干什么的」，到点她只知道要做动作。"
                    + renamed_note
                    + "不想要就跳过（跳过之后不再问）；也可以先应用，再去日程里自己改。"
                ),
                writes=writes,
                default_on=True,
                target=TARGET_SCHEDULES,
            )
        )

    morning = entries.get(MORNING_SCHEDULE_ID)
    if morning is not None and _morning_say_wanted(morning):
        chain = morning.get("action_chain")
        steps = [copy.deepcopy(step) for step in chain] if isinstance(chain, list) else []
        items.append(
            MigrationItem(
                id="schedules:morning_say",
                title="默认日程的描述",
                field_path=f"schedules[id={MORNING_SCHEDULE_ID}].action_chain",
                before="早安这条只有走过去、伸个懒腰，一句话都不说",
                after="末尾加一步：说一句早安",
                reversible=True,
                kind=KIND_UPGRADE,
                label="早安这条日程：末尾补一句「说一句早安」",
                note=(
                    "以前她做完这一串就走开了，从来不说话。不想要这句早安就跳过——"
                    "描述照补，只是不加这一步；也可以应用之后去日程里把最后一步删掉。"
                ),
                writes={
                    f"schedules[id={MORNING_SCHEDULE_ID}].action_chain": [
                        *steps,
                        {"type": "say", "messages": []},
                    ]
                },
                default_on=True,
                target=TARGET_SCHEDULES,
            )
        )
    return items


def skipped_key(item: MigrationItem) -> str:
    """跳过记录的键：``目标路径 + 新值``（默认值再变一版就会重新问）。"""

    return f"{item.field_path}@{_fmt(item.after) if not isinstance(item.after, str) else item.after}"


def detect_all(
    raw_world: dict[str, Any],
    skipped_keys: Iterable[str] = (),
    raw_schedules: dict[str, Any] | None = None,
) -> list[MigrationItem]:
    """列出「可以升级 / 你改过」的配置项。**纯函数：不读文件、不写盘、不改入参。**

    Args:
        raw_world: 盘上那份世界配置（``ConfigStore.raw_world()``）。
        skipped_keys: 用户点过「别再问」的 :func:`skipped_key`，这些不再出现在清单里。
        raw_schedules: 盘上那份日程（``ConfigStore.raw_schedules()``）。日程存在**另一个
            文件**里，所以得单独传；不传就只列 ``world.json`` 里的条目（老调用方不用改）。

    Returns:
        清单（顺序稳定：默认值 → Agent 预算 → 题材表 → 内置动作 → 缺的内置动作 →
        画像 → 联网搜索 → 日程）。
    """

    skipped = set(skipped_keys or ())
    items: list[MigrationItem] = []
    if isinstance(raw_world, dict):
        items = (
            _tuned_items(raw_world)
            + _agent_items(raw_world)
            + _genre_items(raw_world)
            + _action_items(raw_world)
            + _missing_builtin_items(raw_world)
            + _default_bond_items(raw_world)
            + _profile_items(raw_world)
            + _search_items(raw_world)
        )
    if raw_schedules is not None:
        items = items + _schedule_items(raw_schedules)
    return [item for item in items if skipped_key(item) not in skipped]


# ---------------------------------------------------------------------------
# 应用（只改目标路径）
# ---------------------------------------------------------------------------


def apply_batch(
    raw_world: dict[str, Any],
    raw_schedules: dict[str, Any] | None,
    ids: Iterable[str],
) -> tuple[dict[str, Any], dict[str, Any], list[str], list[str]]:
    """把勾中的几条写进两份**深拷贝**，返回 ``(新世界, 新日程, 成功, 失败)``。

    日程在 ``schedules.json``，世界在 ``world.json``，两个文件分开写盘，所以这里按
    每条迁移自己的 ``target`` 挑容器。调用方落盘时也要分开：``save_world(new_world)`` /
    ``save_schedules(new_schedules)``。

    只写这些条目自己的 ``field_path``（``group`` 写它声明的那几处，``appends`` 只往
    列表末尾追加还缺的那几条），别的键一个字节都不动 —— 未知键、用户自己加的段、
    键的顺序全部原样保留。
    """

    new_world = copy.deepcopy(raw_world if isinstance(raw_world, dict) else {})
    new_schedules = copy.deepcopy(raw_schedules if isinstance(raw_schedules, dict) else {})
    known = {item.id: item for item in detect_all(new_world, (), new_schedules)}
    applied: list[str] = []
    failed: list[str] = []
    seen: set[str] = set()
    for item_id in ids or ():
        key = str(item_id)
        if not key or key in seen:
            continue
        seen.add(key)
        item = known.get(key)
        if item is None:
            failed.append(key)
            continue
        bucket = new_schedules if item.target == TARGET_SCHEDULES else new_world
        ok = True
        for path, entries in item.append_targets().items():
            if not append_path(bucket, path, entries):
                ok = False
        for path, value in item.targets().items():
            if not write_path(bucket, path, value):
                ok = False
        if ok:
            applied.append(key)
        else:
            failed.append(key)
    return new_world, new_schedules, applied, failed


def apply_ids(
    raw_world: dict[str, Any],
    ids: Iterable[str],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """把勾中的几条写进一份**深拷贝**，返回 ``(新配置, 应用成功的 id, 失败的 id)``。

    只管 ``world.json``。带日程的迁移（``target="schedules"``）走 :func:`apply_batch`，
    否则那条迁移在这里只会记成失败。
    """

    new_world, _new_schedules, applied, failed = apply_batch(raw_world, {}, ids)
    return new_world, applied, failed


# ---------------------------------------------------------------------------
# 跳过记录（data/migrations_skipped.json）
# ---------------------------------------------------------------------------


def skipped_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / SKIPPED_FILE


def load_skipped(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    """读跳过记录；文件不在 / 坏了都当成「没跳过任何东西」。"""

    path = skipped_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, dict):
        return {}
    return {str(key): dict(value) for key, value in items.items() if isinstance(value, dict)}


def save_skipped(data_dir: str | Path, items: dict[str, dict[str, Any]]) -> None:
    path = skipped_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": items}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def skip_ids(
    raw_world: dict[str, Any],
    data_dir: str | Path,
    ids: Iterable[str],
    *,
    world_rev_value: str = "",
    raw_schedules: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """把这几条记成「别再问」，返回 ``(记下的 id, 不认识的 id)``。

    ``raw_schedules`` 同 :func:`detect_all`：日程在另一个文件里，不传就只认 world.json 的条目。
    """

    items = {item.id: item for item in detect_all(raw_world, (), raw_schedules)}
    records = load_skipped(data_dir)
    skipped: list[str] = []
    failed: list[str] = []
    for item_id in ids or ():
        key = str(item_id)
        item = items.get(key)
        if item is None:
            failed.append(key)
            continue
        records[skipped_key(item)] = {
            "skipped_at": int(time.time()),
            "field_path": item.field_path,
            "world_rev": world_rev_value,
        }
        skipped.append(key)
    if skipped:
        save_skipped(data_dir, records)
    return skipped, failed


def world_rev(raw_world: dict[str, Any]) -> str:
    """给前端去重 / 跳过记录用的世界配置指纹（内容变了才变）。"""

    try:
        payload = json.dumps(raw_world, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = repr(raw_world)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
