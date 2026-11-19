"""查询向量缓存与后台预热的纯逻辑（不碰库、不碰宿主）。

**为什么要它**：谈资召回要先把「这一轮聊的是什么」那段文本交给 embedding 供应商换成
一串数字（查询向量），这一步是整条召回路径上唯一等外部服务的地方——真机实测云端一次
3.4~4.3 秒。同一段文本 + 同一个向量空间 + 同一版本配方算出来的向量是同一个，所以这一步
的结果可以留着复用：主动跟话、主动计划这些不带新用户发言的路径，要的往往就是几分钟前
已经算过的那一句。

**这个模块只做两件事**：

* :class:`QueryVectorCache`——按缓存键记住「文本 → 向量」，带 TTL 与条数上限；
* :class:`PrefetchScheduler`——决定这一拍该不该发起一次后台预热、该轮到哪个会话
  （冷却 + 轮转），真正的嵌入调用由调用方（引擎）去做。

规则由调用方给：缓存键用 :func:`core.topic_query.query_cache_key` 生成（里面已经带了
配方版本与 provider/空间指纹），这里只当它是字符串，不解释它的含义。
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "CachedQueryVector",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_PREFETCH_INTERVAL_SECONDS",
    "DEFAULT_PREFETCH_TTL_SECONDS",
    "PrefetchScheduler",
    "QueryVectorCache",
]

DEFAULT_PREFETCH_TTL_SECONDS = 900.0
"""预热/缓存的查询向量默认能用多久（秒）：15 分钟。"""

DEFAULT_PREFETCH_INTERVAL_SECONDS = 300.0
"""同一个会话两次后台预热的最小间隔（秒）：5 分钟。"""

DEFAULT_MAX_ENTRIES = 32
"""缓存最多留几条查询向量；超了先丢最久没用过的。"""


@dataclass(frozen=True)
class CachedQueryVector:
    """一条缓存下来的查询向量：向量本身、维度、算出来的时刻（调用方的钟）。"""

    vector: tuple[float, ...]
    dim: int
    at: float


def _clean_vector(vector: Any) -> tuple[float, ...] | None:
    """把一串数字整理成元组；空、非数字、NaN/Inf 一律当作坏值（返回 None）。"""

    if isinstance(vector, (str, bytes, bytearray)) or vector is None:
        return None
    try:
        values = tuple(float(item) for item in vector)
    except (TypeError, ValueError):
        return None
    if not values:
        return None
    for value in values:
        if not math.isfinite(value):
            return None
    return values


class QueryVectorCache:
    """按缓存键记住查询向量，带 TTL、条数上限与「最久没用先丢」的淘汰。

    线程/任务安全说明：纯内存结构，单事件循环里用即可（引擎就是单循环），
    不自带锁；跨 await 的读改写由调用方自己保证（调用方只在同一拍里 put/get）。
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_PREFETCH_TTL_SECONDS,
                 max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.ttl_seconds = self._positive(ttl_seconds, DEFAULT_PREFETCH_TTL_SECONDS)
        self.max_entries = self._positive_int(max_entries, DEFAULT_MAX_ENTRIES)
        self._items: "OrderedDict[str, CachedQueryVector]" = OrderedDict()

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        """非正数/NaN/坏值回默认（配置写错不该炸）。"""

        if isinstance(value, bool):
            return default
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default

    @staticmethod
    def _positive(value: Any, default: float) -> float:
        """非正数/NaN/Inf/坏值回默认（配置写错不该炸）。"""

        if isinstance(value, bool):
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(number) or number <= 0:
            return default
        return number

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: str, *, now: float, dim: int = 0) -> tuple[float, ...] | None:
        """取一条还没过期的向量；空的键、过期、维度对不上都当没有（顺手删掉过期的）。"""

        cache_key = str(key or "")
        if not cache_key.strip():
            return None
        entry = self._items.get(cache_key)
        if entry is None:
            return None
        if self.ttl_seconds > 0 and float(now) - entry.at > self.ttl_seconds:
            self._items.pop(cache_key, None)
            return None
        want = int(dim or 0)
        if want and entry.dim and want != entry.dim:
            # 空间换了（维度就变了）：留着也没用，直接丢掉。
            self._items.pop(cache_key, None)
            return None
        self._items.move_to_end(cache_key)
        return entry.vector

    def put(self, key: str, vector: Sequence[float], *, now: float) -> bool:
        """存一条向量；坏键/坏向量返回 False（不覆盖已有条目）。"""

        cache_key = str(key or "")
        clean = _clean_vector(vector)
        if not cache_key.strip() or clean is None:
            return False
        self._items[cache_key] = CachedQueryVector(vector=clean, dim=len(clean), at=float(now))
        self._items.move_to_end(cache_key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)
        return True

    def has(self, key: str, *, now: float, dim: int = 0) -> bool:
        """有没有一条能用的（不取值，只回答有没有；预热前用它避免白跑一趟）。"""

        return self.get(key, now=now, dim=dim) is not None

    def forget(self, key: str) -> None:
        self._items.pop(str(key or ""), None)

    def clear(self) -> None:
        self._items.clear()


class PrefetchScheduler:
    """决定这一拍预热哪个会话：冷却期内的跳过，其余按「最久没试过」轮转。

    只记账，不发请求——真正调 embedding 的是调用方；失败要不要记冷却也由调用方决定
    （一般要：云端超时重试一次还是那么慢）。
    """

    def __init__(self, *, interval_seconds: float = DEFAULT_PREFETCH_INTERVAL_SECONDS,
                 max_per_pick: int = 1) -> None:
        self.interval_seconds = QueryVectorCache._positive(
            interval_seconds, DEFAULT_PREFETCH_INTERVAL_SECONDS)
        try:
            self.max_per_pick = max(1, int(max_per_pick))
        except (TypeError, ValueError):
            self.max_per_pick = 1
        self._last: dict[str, float] = {}

    def pick(self, session_ids: Sequence[str], *, now: float) -> list[str]:
        """从候选会话里挑出这一拍该预热的（最多 ``max_per_pick`` 个，先到先得）。"""

        moment = float(now)
        ready: list[tuple[float, int, str]] = []
        seen: set[str] = set()
        for index, raw in enumerate(session_ids or ()):
            session_id = str(raw or "")
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            last = self._last.get(session_id)
            if last is not None and moment - last < self.interval_seconds:
                continue
            # 从没试过的排最前（-inf），同档按候选顺序来（配置顺序，稳定可预期）。
            ready.append((last if last is not None else float("-inf"), index, session_id))
        ready.sort(key=lambda item: (item[0], item[1]))
        return [session_id for _at, _index, session_id in ready[:self.max_per_pick]]

    def mark(self, session_id: str, *, now: float) -> None:
        """记下「这个会话刚刚试过」——成功失败都该记，免得每拍都去撞同一个坏供应商。"""

        key = str(session_id or "")
        if key:
            self._last[key] = float(now)

    def last_at(self, session_id: str) -> float | None:
        return self._last.get(str(session_id or ""))

    def forget(self, session_id: str) -> None:
        self._last.pop(str(session_id or ""), None)

    def clear(self) -> None:
        self._last.clear()
