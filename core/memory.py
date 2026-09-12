"""场景记忆引擎（设计文档 4.13 节）。

记忆与「地点 + 人」强相关，因此召回用 SQL 过滤 + 权重打分即可，不需要向量检索：
- 召回空间天然很小（同一会话、同一节点、同一人格的记忆通常几十条）；
- 过滤维度是结构化的（会话/人格/节点/用户），SQL 比向量更精确；
- 省掉 embedding provider 依赖和每次召回的额外 API 开销。
如果将来要做「同义语义召回」，可以在 recall 后面挂一个可选的向量重排，
当前接口（recall 返回打分后的列表）已经预留了这个位置。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from .db import Database
from .models import WorldConfig

SCENE = "scene"
RELATION = "relation"
EVENT = "event"
INNER = "inner"
INTERACTION = "interaction"

SCOPE_GROUP = "group"
SCOPE_PERSONA = "persona"
SCOPE_GROUP_PERSONA = "group_persona"
SCOPE_GLOBAL = "global"
SCOPE_NODE = "node"

_NEGATION_WORDS = ("不", "没", "别", "讨厌", "不想", "不再")

# 情绪强度词典：写进记忆时，"越激烈的经历记得越牢"。
# 值直接作为权重加成（0~0.25），所以强情绪的记忆更容易被召回。
_INTENSE_WORDS: dict[str, float] = {
    "生气": 0.22, "愤怒": 0.25, "吵架": 0.24, "讨厌": 0.18, "难过": 0.20,
    "委屈": 0.22, "心疼": 0.20, "想哭": 0.24, "哭": 0.16, "害怕": 0.20,
    "担心": 0.16, "害怕失去": 0.25, "喜欢": 0.18, "爱": 0.22, "想念": 0.20,
    "开心": 0.16, "高兴": 0.14, "激动": 0.20, "兴奋": 0.20, "感动": 0.22,
    "惊喜": 0.20, "害羞": 0.16, "紧张": 0.16, "嫉妒": 0.22, "吃醋": 0.22,
    "重要": 0.12, "第一次": 0.12, "约定": 0.12, "承诺": 0.14, "对不起": 0.14,
    "谢谢": 0.10, "安慰": 0.14, "陪我": 0.14, "别走": 0.22, "秘密": 0.16,
}

# 情绪越强的 mood 本身也加成
_MOOD_INTENSITY: dict[str, float] = {
    "难以平静": 0.25,
    "心潮起伏": 0.18,
    "开心": 0.15,
    "想念": 0.15,
    "委屈": 0.2,
    "生气": 0.22,
    "害羞": 0.14,
}


def emotional_weight(
    base: float,
    *,
    emotion: str = "",
    mood: str = "",
    text: str = "",
    affect: float = 0.0,
) -> float:
    """按"这段记忆的情绪有多强"给权重加成。

    加成来源（取最大的一项 + 心潮的一小部分，避免叠加爆炸）：
    - 显式情绪标签（动作/编辑器里填的）
    - 正文里出现的强情绪词
    - 当时的 mood
    - 当时的心潮（affect）
    """

    scored: list[float] = []
    if emotion:
        scored.append(max((_INTENSE_WORDS.get(word, 0.0) for word in _emotion_tokens(emotion)), default=0.12))
    if mood and mood in _MOOD_INTENSITY:
        scored.append(_MOOD_INTENSITY[mood])
    if text:
        hits = [value for word, value in _INTENSE_WORDS.items() if word in text]
        if hits:
            scored.append(max(hits))
    bonus = max(scored) if scored else 0.0
    bonus += max(0.0, min(1.0, float(affect))) * 0.15
    return max(0.0, min(1.0, float(base) + bonus))


def _emotion_tokens(emotion: str) -> list[str]:
    return [part.strip() for part in re.split(r"[/、,，\s]+", str(emotion or "")) if part.strip()]


@dataclass
class RecalledMemory:
    """召回结果。"""

    id: int
    content: str
    node_id: str
    memory_type: str
    emotion: str
    weight: float
    score: float
    related_users: list[str]
    created_at: float = 0.0
    """写入时间（unix 秒）。提示词里会带上日期，所以它必须跟着召回结果一起走。"""

    def render(self, *, stamp: str = "") -> str:
        """渲染成提示词里的一行；``stamp`` 是日期标签，例如 ``09-11``。"""

        prefix = f"[{stamp}] " if stamp else ""
        suffix = f"（{self.emotion}）" if self.emotion else ""
        return f"- {prefix}{self.content}{suffix}"


class MemoryEngine:
    """记忆的创建、召回、衰减与用户控制。"""

    def __init__(self, db: Database, world: WorldConfig | None = None) -> None:
        self.db = db
        self.world = world

    def set_world(self, world: WorldConfig) -> None:
        self.world = world

    # ---------------- 创建 ----------------

    def remember(
        self,
        *,
        session_id: str,
        persona_id: str,
        node_id: str,
        content: str,
        memory_type: str = SCENE,
        related_users: list[str] | None = None,
        emotion: str = "",
        weight: float = 0.5,
        scope: str | None = None,
        source: str = "runtime",
        conflict_policy: str | None = None,
        affect: float = 0.0,
    ) -> int | None:
        """写入一条记忆。返回记忆 id；因冲突被跳过时返回 None。

        ``weight`` 是基础权重，实际写入时会被「情绪强度」抬高：
        越激动的经历记得越牢（见 :func:`emotional_weight`）。
        """

        text = (content or "").strip()
        if not text:
            return None
        scope = scope or (self.world.memory_scope_mode if self.world else SCOPE_GROUP_PERSONA)
        policy = conflict_policy or (
            self.world.memory_conflict_policy if self.world else "newest"
        )
        related_users = [str(u) for u in (related_users or [])]
        weight = emotional_weight(
            weight, emotion=emotion, mood=emotion, text=text, affect=affect
        )

        conflict = self._find_conflict(
            session_id=session_id, persona_id=persona_id, node_id=node_id, content=text
        )
        if conflict is not None:
            if policy == "skip_conflict":
                return None
            if policy == "highest_weight" and float(conflict["weight"]) > float(weight):
                return None
            self.db.delete_memories(memory_id=int(conflict["id"]))

        self._enforce_limit(session_id=session_id, persona_id=persona_id)
        return self.db.add_memory(
            session_id=session_id,
            persona_id=persona_id,
            scope=scope,
            node_id=node_id,
            content=text,
            memory_type=memory_type,
            related_users=related_users,
            emotion=emotion,
            weight=max(0.0, min(1.0, float(weight))),
            source=source,
        )

    def _find_conflict(
        self, *, session_id: str, persona_id: str, node_id: str, content: str
    ) -> dict[str, Any] | None:
        """找出与待写入内容冲突的旧记忆。"""

        candidates = self.db.query_memories(
            session_id=session_id, persona_id=persona_id, node_id=node_id, limit=200
        )
        normalized = _normalize(content)
        for item in candidates:
            other = _normalize(str(item.get("content", "")))
            if not other:
                continue
            if other == normalized:
                return item
            if _opposite(normalized, other):
                return item
        return None

    def _enforce_limit(
        self, *, session_id: str, persona_id: str, max_per_scope: int = 500
    ) -> None:
        rows = self.db.query_memories(
            session_id=session_id, persona_id=persona_id, limit=max_per_scope + 50
        )
        if len(rows) <= max_per_scope:
            return
        ordered = sorted(rows, key=lambda item: float(item.get("weight", 0.0)))
        for item in ordered[: len(rows) - max_per_scope]:
            self.db.delete_memories(memory_id=int(item["id"]))

    # ---------------- 召回 ----------------

    def recall(
        self,
        *,
        session_id: str,
        persona_id: str,
        node_id: str = "",
        focus_user: str = "",
        mode: str | None = None,
        limit: int = 5,
        now: float | None = None,
        fallback: bool = True,
    ) -> list[RecalledMemory]:
        """召回记忆。返回按分数降序的结果。"""

        mode = mode or (self.world.memory_scope_mode if self.world else SCOPE_GROUP_PERSONA)
        now = now if now is not None else time.time()
        candidates = self._query(session_id, persona_id, mode, node_id)
        # 降级策略只用于「人格未知」（persona_id 为空）的旧数据兼容：
        # 此时把本会话的所有记忆拉进来，但绝不跨会话取 group 级记忆，避免隐私泄露。
        if not candidates and fallback and not persona_id:
            candidates = self._query(session_id, persona_id, SCOPE_GROUP, node_id)

        scored: list[tuple[float, dict[str, Any]]] = []
        for item in candidates:
            score = self._score(item, focus_user=focus_user, node_id=node_id, now=now)
            if score <= 0:
                continue
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        results = [self._to_recalled(score, item) for score, item in scored[:limit]]
        if results:
            self.db.mark_recalled([item.id for item in results], when=now)
        return results

    def _query(
        self, session_id: str, persona_id: str, mode: str, node_id: str = ""
    ) -> list[dict[str, Any]]:
        """按作用域模式取候选记忆。

        语义（写死在测试里，改动会在 tests/test_memory.py 里暴露）：
        - group：本会话的全部记忆 + 全局记忆；
        - persona：该人格在所有会话的记忆 + 全局记忆；
        - group_persona：本会话本身的 + 该人格的 + 全局（默认，最贴合「同一个她在不同群里共享人格记忆」）；
        - global：所有记忆。

        另外，「节点专属记忆」（scope=node）只要地点对得上就会被想起，与模式无关。
        """

        rows: list[dict[str, Any]] = []
        if node_id:
            # 节点专属记忆只属于「这个会话的这个地点」，必须按会话过滤，
            # 否则 A 群的房间记忆会漏进 B 群（真实环境实测到的 bug）。
            node_rows = self.db.query_memories(
                session_id=session_id if session_id else None,
                node_id=node_id,
                scope=SCOPE_NODE,
                limit=200,
            )
            rows.extend(
                item
                for item in node_rows
                if not persona_id
                or not item.get("persona_id")
                or item.get("persona_id") == persona_id
            )
        if mode == SCOPE_GLOBAL:
            for scope in (SCOPE_GLOBAL, SCOPE_GROUP, SCOPE_PERSONA, SCOPE_GROUP_PERSONA):
                rows.extend(self.db.query_memories(scope=scope, limit=500))
            return _dedupe(rows)

        if mode in (SCOPE_GROUP, SCOPE_GROUP_PERSONA) and session_id:
            rows.extend(
                item
                for item in self.db.query_memories(session_id=session_id, limit=500)
                if item.get("scope") in (SCOPE_GROUP, SCOPE_GROUP_PERSONA)
            )
        if mode in (SCOPE_PERSONA, SCOPE_GROUP_PERSONA) and persona_id:
            rows.extend(
                item
                for item in self.db.query_memories(persona_id=persona_id, limit=500)
                if item.get("scope") in (SCOPE_PERSONA, SCOPE_GROUP_PERSONA)
            )
        rows.extend(self.db.query_memories(scope=SCOPE_GLOBAL, limit=200))
        return _dedupe(rows)

    def _score(
        self, item: dict[str, Any], *, focus_user: str, node_id: str, now: float
    ) -> float:
        weight = max(0.0, min(1.0, float(item.get("weight", 0.5))))
        score = weight

        # 用户匹配加成
        related = [str(u) for u in item.get("related_users") or []]
        if focus_user and focus_user in related:
            score *= 1.5

        # 节点匹配加成
        if node_id and item.get("node_id") == node_id:
            score *= 1.3

        # 时间衰减：30 天半衰期
        age_days = max(0.0, (now - float(item.get("created_at", now))) / 86400.0)
        score *= 0.5 ** (age_days / 30.0)

        # 反复召回会略微降权，避免来来回回提同一件事
        recall_count = int(item.get("recall_count", 0))
        score *= 1.0 / (1.0 + recall_count * 0.2)

        return score

    @staticmethod
    def _to_recalled(score: float, item: dict[str, Any]) -> RecalledMemory:
        return RecalledMemory(
            id=int(item["id"]),
            content=str(item.get("content", "")),
            node_id=str(item.get("node_id", "")),
            memory_type=str(item.get("type", SCENE)),
            emotion=str(item.get("emotion", "")),
            weight=float(item.get("weight", 0.0)),
            score=round(score, 4),
            related_users=[str(u) for u in item.get("related_users") or []],
            created_at=float(item.get("created_at") or 0.0),
        )

    # ---------------- 衰减 ----------------

    def decay(self, *, session_id: str, half_life_days: float = 30.0) -> int:
        return self.db.decay_memories(
            session_id=session_id, half_life_days=half_life_days
        )

    # ---------------- 用户控制 ----------------

    def forget_user(self, *, session_id: str, persona_id: str, user_id: str, topic: str = "") -> int:
        return self.db.delete_memories(
            session_id=session_id, persona_id=persona_id, user_id=user_id, topic=topic or None
        )

    def export_user(self, *, session_id: str, persona_id: str, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_memories(
            session_id=session_id, persona_id=persona_id, limit=5000
        )
        result: list[dict[str, Any]] = []
        for item in rows:
            related = [str(u) for u in item.get("related_users") or []]
            if user_id in related or (not related and user_id in str(item.get("content", ""))):
                result.append(item)
        return result

    def correct(
        self,
        *,
        session_id: str,
        persona_id: str,
        old_text: str,
        new_text: str,
    ) -> int:
        """把包含 old_text 的记忆改写为 new_text。返回修改条数。"""

        rows = self.db.query_memories(
            session_id=session_id, persona_id=persona_id, limit=5000
        )
        changed = 0
        needle = (old_text or "").strip()
        if not needle:
            return 0
        for item in rows:
            content = str(item.get("content", ""))
            if needle in content:
                self.db.update_memory(
                    int(item["id"]), content=content.replace(needle, new_text.strip())
                )
                changed += 1
        return changed

    def stats(self, session_id: str | None = None) -> dict[str, Any]:
        return self.db.memory_stats(session_id)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _opposite(a: str, b: str) -> bool:
    """粗略的语义相反判断：一条含否定词、另一条不含，且主干相同。"""

    if a == b:
        return False
    a_neg = any(word in a for word in _NEGATION_WORDS)
    b_neg = any(word in b for word in _NEGATION_WORDS)
    if a_neg == b_neg:
        return False
    core_a = _strip_negation(a)
    core_b = _strip_negation(b)
    return bool(core_a) and len(core_a) >= 2 and core_a == core_b


def _strip_negation(text: str) -> str:
    result = text
    for word in _NEGATION_WORDS:
        result = result.replace(word, "")
    return result


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for item in rows:
        item_id = int(item.get("id", 0))
        if item_id in seen:
            continue
        seen.add(item_id)
        result.append(item)
    return result
