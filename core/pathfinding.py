"""地图寻路：BFS 最短路径。"""

from __future__ import annotations

from collections import deque

Graph = dict[str, list[tuple[str, int]]]


def find_path(graph: Graph, start: str, goal: str) -> list[str] | None:
    """返回从 start 到 goal 的节点序列（含首尾）；不可达返回 None。"""

    if start == goal:
        return [start]
    if start not in graph or goal not in graph:
        return None
    visited = {start}
    queue: deque[list[str]] = deque([[start]])
    while queue:
        path = queue.popleft()
        for neighbor, _ticks in graph.get(path[-1], []):
            if neighbor in visited:
                continue
            new_path = path + [neighbor]
            if neighbor == goal:
                return new_path
            visited.add(neighbor)
            queue.append(new_path)
    return None


def path_ticks(graph: Graph, path: list[str]) -> int:
    """计算路径耗时（tick）。"""

    if not path or len(path) < 2:
        return 0
    total = 0
    for index in range(len(path) - 1):
        src, dst = path[index], path[index + 1]
        step = min(
            (ticks for neighbor, ticks in graph.get(src, []) if neighbor == dst),
            default=None,
        )
        if step is None:
            return 0
        total += step
    return total


def travel_cost(graph: Graph, start: str, goal: str) -> int | None:
    """从 start 到 goal 的耗时（tick）。不可达返回 None。"""

    path = find_path(graph, start, goal)
    if path is None:
        return None
    return path_ticks(graph, path)


def nearest_node(graph: Graph, start: str, candidates: list[str]) -> str | None:
    """在候选节点里挑一个可达且最近的。"""

    best: tuple[int, str] | None = None
    for candidate in candidates:
        cost = travel_cost(graph, start, candidate)
        if cost is None:
            continue
        if best is None or cost < best[0]:
            best = (cost, candidate)
    return best[1] if best else None
