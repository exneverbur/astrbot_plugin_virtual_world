"""无聊上网池：动作自己声明的「闲得慌时可以干这事」（T20）。

主插件**只认字段** ``idle_activity``，一个动作 id 都不写死：

.. code-block:: python

    {"id": "search_web", "idle_activity": {"weight": 3.0, "cooldown_minutes": 15}}

谁想参加这个池子就声明（内置动作、扩展动作、用户自己加的动作一视同仁），
不想参加就别写——没声明的动作永远不会被这个池子抽中，主插件也不认识它叫什么。

判定链（全满足才开池）：

1. 全局段 ``boredom_online.enabled`` 开着；
2. 她这一拍是空闲（``state.state == "idle"``）；
3. 无聊值 ≥ 全局 ``min_boredom``（也 ≥ 动作自己声明的 ``min_boredom``）；
4. 距上一次从这个池子里抽中动作 ≥ 全局 ``min_gap_minutes``；
5. 动作本身**合格**（前置条件 / 配额 / 地点 / 可达性由 ``core.action_selection``
   的硬过滤负责，这里只做池内那一层）。

池内再按动作自己的 ``cooldown_minutes`` 冷却掉刚做过的，剩下的按 ``weight``
加权随机；``loneliness >= 0.6`` 时权重整体减半（心里空落落的时候，上网这件事
没那么有意思了），并且不允许连着两次抽中同一个动作。

本模块是**纯逻辑**：不碰世界状态、不读写库、不碰时钟——时钟和"上次什么时候抽的"
都由引擎传进来，这样抽签结果可以按种子原样回放。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_WEIGHT = 1.0
"""声明里没写 ``weight`` 时的权重。"""

DEFAULT_COOLDOWN_MINUTES = 60.0
"""声明里没写 ``cooldown_minutes`` 时，同一个动作隔多久才能再被抽中。"""

LONELY_THRESHOLD = 0.6
"""孤独到这个数，池子权重整体减半（和 ``state.loneliness`` 一个量程）。"""

LONELY_WEIGHT_FACTOR = 0.5
WHY_WEIGHTED_RANDOM = "weighted_random"
"""``idle_pick`` 事件里 ``why`` 的取值：这一件是按权重抽出来的。"""


@dataclass(frozen=True)
class IdleDeclaration:
    """一个动作对池子的声明（已经归一过：数字都是 ``float``，缺省都填好了）。"""

    weight: float = DEFAULT_WEIGHT
    cooldown_minutes: float = DEFAULT_COOLDOWN_MINUTES
    min_boredom: float | None = None
    """这个动作自己的门槛；``None`` = 只用全局那一个。

    只能在全局门槛之上**加码**（取两者的较大值），不能反过来把全局门槛压下去——
    否则一个动作就能把"她有多无聊才动手"这条全局口径绕开。
    """

    def effective_weight(self, *, loneliness: float) -> float:
        """这一拍它的实际权重（孤独的时候减半）。"""

        weight = float(self.weight)
        if _safe_float(loneliness, 0.0) >= LONELY_THRESHOLD:
            return weight * LONELY_WEIGHT_FACTOR
        return weight


def _safe_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def parse_declaration(value: Any) -> IdleDeclaration | None:
    """把动作上的 ``idle_activity`` 读成声明；坏声明返回 ``None``（＝当作没声明）。

    容错的三种情形（都**不抛**，只是这个动作不进池子）：

    * 根本不是 dict（``True`` / ``"3"`` / 列表）；
    * ``weight`` 解析不出来、≤ 0、或者 NaN / inf；
    * ``cooldown_minutes`` 解析不出来或者 < 0。

    ``min_boredom`` 单独容错：解析不出来就当没写（用全局门槛），超范围夹到 0~1。
    """

    if not isinstance(value, dict):
        return None
    raw_weight = value.get("weight")
    weight = _safe_float(
        DEFAULT_WEIGHT if raw_weight is None else raw_weight, float("nan")
    )
    if math.isnan(weight) or weight <= 0:
        return None
    raw_cooldown = value.get("cooldown_minutes")
    cooldown = _safe_float(
        DEFAULT_COOLDOWN_MINUTES if raw_cooldown is None else raw_cooldown,
        float("nan"),
    )
    if math.isnan(cooldown) or cooldown < 0:
        return None
    raw_limit = value.get("min_boredom")
    limit: float | None = None
    if raw_limit is not None:
        parsed = _safe_float(raw_limit, float("nan"))
        if not math.isnan(parsed):
            limit = max(0.0, min(1.0, parsed))
    return IdleDeclaration(
        weight=weight, cooldown_minutes=cooldown, min_boredom=limit
    )


def gap_ready(
    *, now: float, last_at: float | None, min_gap_minutes: float
) -> bool:
    """距上一次从池子里抽中动作，够不够 ``min_gap_minutes``。"""

    if last_at is None:
        return True
    gap = max(0.0, _safe_float(min_gap_minutes, 0.0)) * 60.0
    if gap <= 0:
        return True
    return _safe_float(now, 0.0) - _safe_float(last_at, 0.0) >= gap


@dataclass(frozen=True)
class PoolEntry:
    """池子里的一件：动作候选 + 它的声明 + 这一拍的实际权重。"""

    candidate: Any
    action_id: str
    declaration: IdleDeclaration
    weight: float

    @property
    def candidate_id(self) -> str:
        return str(getattr(self.candidate, "candidate_id", "") or "")


@dataclass(frozen=True)
class Pool:
    """建好的池子。``blocked`` 非空 = 连门都没开（原因在排查时有用）。"""

    entries: list[PoolEntry] = field(default_factory=list)
    blocked: str = ""
    declared: int = 0
    """合格候选里声明过 ``idle_activity`` 的有几个（冷却前的口径，排查用）。"""


def build_pool(
    candidates: Iterable[Any],
    declarations: Mapping[str, IdleDeclaration | None],
    *,
    boredom: float,
    loneliness: float,
    now: float,
    last_at: float | None,
    min_gap_minutes: float,
    min_boredom: float = 0.0,
    last_used: Mapping[str, float] | None = None,
) -> Pool:
    """把**已经合格**的候选收窄成池子（不做前置条件判断，那是硬过滤的事）。

    ``last_used``：``{action_id: 上次抽中它的时刻}``——引擎按会话传进来，
    冷却口径与全局的"上次抽中"口径各自独立，互不串会话。
    """

    if not gap_ready(now=now, last_at=last_at, min_gap_minutes=min_gap_minutes):
        return Pool(blocked="min_gap")
    used = dict(last_used or {})
    floor = max(0.0, _safe_float(min_boredom, 0.0))
    now_value = _safe_float(now, 0.0)
    boredom_value = _safe_float(boredom, 0.0)
    loneliness_value = _safe_float(loneliness, 0.0)
    entries: list[PoolEntry] = []
    declared = 0
    # 同一个动作会在候选里出现好几次：全局动作在**每个地点**都算"能在这儿做"，
    # 于是 `build_candidates` 会按地点各出一条。池子只认动作，不认地点——不去重的话
    # 它会被抽中的概率按出现次数被放大，`avoid_action` 也只剔掉其中一份。
    seen: set[str] = set()
    for candidate in candidates:
        action_id = str(getattr(candidate, "action_id", "") or "")
        if not action_id or action_id in seen:
            continue
        declaration = declarations.get(action_id)
        if declaration is None:
            continue
        seen.add(action_id)
        declared += 1
        limit = max(floor, declaration.min_boredom or 0.0)
        if boredom_value < limit:
            continue
        last = used.get(action_id)
        if last is not None:
            cooldown = max(0.0, _safe_float(declaration.cooldown_minutes, 0.0)) * 60.0
            if cooldown > 0 and now_value - _safe_float(last, 0.0) < cooldown:
                continue
        entries.append(
            PoolEntry(
                candidate=candidate,
                action_id=action_id,
                declaration=declaration,
                weight=declaration.effective_weight(loneliness=loneliness_value),
            )
        )
    return Pool(entries=entries, declared=declared)


def pick(
    entries: Sequence[PoolEntry], *, seed_text: str, avoid_action: str = ""
) -> PoolEntry | None:
    """按权重抽一件；``avoid_action`` 是上一次抽中的动作（不连着抽两次同一个）。

    种子用字符串：同一个会话、同一个世界时刻抽出来的必然是同一件，抽签结果可回放。
    只有一件时不再排除上一个——否则池子会被自己的规则清空。
    """

    pool = list(entries)
    if not pool:
        return None
    if avoid_action and len(pool) > 1:
        kept = [entry for entry in pool if entry.action_id != avoid_action]
        if kept:
            pool = kept
    total = sum(max(0.0, float(entry.weight)) for entry in pool)
    if total <= 0:  # 理论上进不来（weight 必须 > 0）；真来了也别把这一拍吞掉
        return pool[0]
    rng = random.Random(str(seed_text))
    dice = rng.random() * total
    walked = 0.0
    for entry in pool:
        walked += max(0.0, float(entry.weight))
        if dice < walked:
            return entry
    return pool[-1]
