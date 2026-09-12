"""持久层：标准库 sqlite3（运行时状态 + 记忆 + 事件 + 键值）。

为什么不用 SQLModel/aiosqlite：避免与宿主环境的 SQLAlchemy/驱动版本冲突，
也避免给用户增加安装依赖。所有写操作用 WAL + busy_timeout，读写都在线程池里执行
（见 AsyncDatabase），不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS world_state (
    session_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS node_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'group_persona',
    node_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'scene',
    related_users TEXT NOT NULL DEFAULT '[]',
    emotion TEXT NOT NULL DEFAULT '',
    weight REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL,
    last_recalled REAL NOT NULL DEFAULT 0,
    recall_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'runtime'
);
CREATE INDEX IF NOT EXISTS idx_session_persona_node
    ON node_memory (session_id, persona_id, node_id);
CREATE INDEX IF NOT EXISTS idx_session_node ON node_memory (session_id, node_id);
CREATE INDEX IF NOT EXISTS idx_persona_node ON node_memory (persona_id, node_id);

CREATE TABLE IF NOT EXISTS user_relation (
    session_id TEXT NOT NULL,
    persona_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    memories TEXT NOT NULL DEFAULT '[]',
    affinity REAL NOT NULL DEFAULT 0.5,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, persona_id, user_id)
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    world_time INTEGER NOT NULL DEFAULT 0,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_session ON event_log (session_id, created_at);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_fire (
    session_id TEXT NOT NULL,
    schedule_id TEXT NOT NULL,
    day_key TEXT NOT NULL,
    fired_at REAL NOT NULL,
    PRIMARY KEY (session_id, schedule_id, day_key)
);
"""


class Database:
    """同步 sqlite 封装。调用方应通过 AsyncDatabase 使用，避免阻塞事件循环。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------------- 通用 ----------------

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    # ---------------- 世界状态 ----------------

    def get_state(self, session_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT payload FROM world_state WHERE session_id = ?", (session_id,)
        )
        if not rows:
            return None
        try:
            return json.loads(rows[0]["payload"])
        except json.JSONDecodeError:
            return None

    def save_state(
        self, session_id: str, payload: dict[str, Any], updated_at: float | None = None
    ) -> None:
        blob = json.dumps(payload, ensure_ascii=False)
        self._execute(
            "INSERT INTO world_state (session_id, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (session_id, blob, updated_at if updated_at is not None else time.time()),
        )

    def list_state_ids(self) -> list[str]:
        return [row["session_id"] for row in self._query("SELECT session_id FROM world_state")]

    def delete_state(self, session_id: str) -> None:
        self._execute("DELETE FROM world_state WHERE session_id = ?", (session_id,))

    # ---------------- 记忆 ----------------

    def add_memory(
        self,
        *,
        session_id: str,
        persona_id: str,
        scope: str,
        node_id: str,
        content: str,
        memory_type: str = "scene",
        related_users: list[str] | None = None,
        emotion: str = "",
        weight: float = 0.5,
        source: str = "runtime",
        created_at: float | None = None,
    ) -> int:
        cursor = self._execute(
            "INSERT INTO node_memory (session_id, persona_id, scope, node_id, content, "
            "type, related_users, emotion, weight, created_at, last_recalled, recall_count, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)",
            (
                session_id,
                persona_id,
                scope,
                node_id,
                content,
                memory_type,
                json.dumps(list(related_users or []), ensure_ascii=False),
                emotion,
                float(weight),
                created_at if created_at is not None else time.time(),
                source,
            ),
        )
        return int(cursor.lastrowid or 0)

    def query_memories(
        self,
        *,
        session_id: str | None = None,
        persona_id: str | None = None,
        node_id: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        min_weight: float | None = None,
        limit: int = 200,
        order: str = "created_at DESC",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM node_memory WHERE 1 = 1"
        params: list[Any] = []
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        if persona_id is not None:
            sql += " AND persona_id = ?"
            params.append(persona_id)
        if node_id is not None:
            sql += " AND node_id = ?"
            params.append(node_id)
        if scope is not None:
            sql += " AND scope = ?"
            params.append(scope)
        if memory_type is not None:
            sql += " AND type = ?"
            params.append(memory_type)
        if min_weight is not None:
            sql += " AND weight >= ?"
            params.append(float(min_weight))
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(int(limit))
        return [self._memory_row(row) for row in self._query(sql, params)]

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["related_users"] = json.loads(item.get("related_users") or "[]")
        except json.JSONDecodeError:
            item["related_users"] = []
        return item

    def update_memory(self, memory_id: int, **fields: Any) -> None:
        if not fields:
            return
        columns: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            columns.append(f"{key} = ?")
            if key == "related_users" and isinstance(value, (list, tuple)):
                value = json.dumps(list(value), ensure_ascii=False)
            params.append(value)
        params.append(int(memory_id))
        self._execute(
            f"UPDATE node_memory SET {', '.join(columns)} WHERE id = ?", params
        )

    def mark_recalled(self, ids: list[int], when: float | None = None) -> None:
        if not ids:
            return
        stamp = when if when is not None else time.time()
        with self._lock:
            self._conn.executemany(
                "UPDATE node_memory SET last_recalled = ?, recall_count = recall_count + 1 "
                "WHERE id = ?",
                [(stamp, int(i)) for i in ids],
            )
            self._conn.commit()

    def delete_memories(
        self,
        *,
        memory_id: int | None = None,
        session_id: str | None = None,
        persona_id: str | None = None,
        user_id: str | None = None,
        node_id: str | None = None,
        topic: str | None = None,
    ) -> int:
        """按条件删除记忆，返回删除条数。

        user_id 按「记忆关联的用户」过滤（related_users），与其它条件取交集，
        这样 /bot forget me 只删与该用户有关的记忆，不会误删整个会话。
        """

        rows = self.query_memories(
            session_id=session_id, persona_id=persona_id, node_id=node_id, limit=100000
        )
        if memory_id is not None:
            rows = [row for row in rows if int(row["id"]) == int(memory_id)]
        if topic:
            rows = [row for row in rows if topic in str(row.get("content", ""))]
        if user_id:
            rows = [
                row
                for row in rows
                if user_id in [str(u) for u in row.get("related_users") or []]
            ]
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        self._execute(f"DELETE FROM node_memory WHERE id IN ({placeholders})", ids)
        return len(ids)

    def delete_memories_by_ids(self, ids: list[int]) -> int:
        """按 id 批量删除记忆（编辑器里多选删除）。返回删除条数。"""

        clean = [int(item) for item in (ids or [])]
        if not clean:
            return 0
        placeholders = ",".join("?" * len(clean))
        cursor = self._execute(
            f"DELETE FROM node_memory WHERE id IN ({placeholders})", clean
        )
        return int(cursor.rowcount or 0)

    def clear_memories(self, session_id: str | None = None) -> int:
        """清空记忆（可按会话）。返回删除条数。"""

        if session_id:
            cursor = self._execute(
                "DELETE FROM node_memory WHERE session_id = ?", (session_id,)
            )
        else:
            cursor = self._execute("DELETE FROM node_memory")
        return int(cursor.rowcount or 0)

    def delete_events_by_ids(self, ids: list[int]) -> int:
        """按 id 批量删除事件日志。返回删除条数。"""

        clean = [int(item) for item in (ids or [])]
        if not clean:
            return 0
        placeholders = ",".join("?" * len(clean))
        cursor = self._execute(
            f"DELETE FROM event_log WHERE id IN ({placeholders})", clean
        )
        return int(cursor.rowcount or 0)

    def clear_events(self, session_id: str | None = None) -> int:
        """清空事件日志（可按会话）。返回删除条数。"""

        if session_id:
            cursor = self._execute(
                "DELETE FROM event_log WHERE session_id = ?", (session_id,)
            )
        else:
            cursor = self._execute("DELETE FROM event_log")
        return int(cursor.rowcount or 0)

    def memory_stats(self, session_id: str | None = None) -> dict[str, Any]:
        where = "WHERE session_id = ?" if session_id else ""
        params: tuple[Any, ...] = (session_id,) if session_id else ()
        total = self._query(f"SELECT COUNT(*) AS c FROM node_memory {where}", params)[0]["c"]
        by_type = {
            row["type"]: row["c"]
            for row in self._query(
                f"SELECT type, COUNT(*) AS c FROM node_memory {where} GROUP BY type", params
            )
        }
        by_scope = {
            row["scope"]: row["c"]
            for row in self._query(
                f"SELECT scope, COUNT(*) AS c FROM node_memory {where} GROUP BY scope", params
            )
        }
        day_ago = time.time() - 86400
        extra = "AND" if where else "WHERE"
        today_added = self._query(
            f"SELECT COUNT(*) AS c FROM node_memory {where} {extra} created_at >= ?",
            params + (day_ago,),
        )[0]["c"]
        today_recalled = self._query(
            f"SELECT COUNT(*) AS c FROM node_memory {where} {extra} last_recalled >= ?",
            params + (day_ago,),
        )[0]["c"]
        avg = self._query(f"SELECT AVG(weight) AS a FROM node_memory {where}", params)[0]["a"]
        return {
            "total": int(total or 0),
            "by_type": by_type,
            "by_scope": by_scope,
            "today_added": int(today_added or 0),
            "today_recalled": int(today_recalled or 0),
            "avg_weight": round(float(avg or 0.0), 3),
        }

    def decay_memories(
        self, *, session_id: str, half_life_days: float = 30.0, floor: float = 0.05
    ) -> int:
        """时间衰减：按半衰期降低权重，低于 floor 的记忆删除。"""

        now = time.time()
        rows = self.query_memories(session_id=session_id, limit=5000)
        updated = 0
        for row in rows:
            age_days = max(0.0, (now - float(row["created_at"])) / 86400.0)
            factor = 0.5 ** (age_days / max(0.1, half_life_days))
            new_weight = float(row["weight"]) * factor
            if new_weight < floor and int(row["recall_count"]) == 0:
                self._execute("DELETE FROM node_memory WHERE id = ?", (row["id"],))
                updated += 1
            elif abs(new_weight - float(row["weight"])) > 1e-4:
                self._execute(
                    "UPDATE node_memory SET weight = ? WHERE id = ?",
                    (new_weight, row["id"]),
                )
                updated += 1
        return updated

    # ---------------- 关系 ----------------

    def upsert_relation(
        self,
        *,
        session_id: str,
        persona_id: str,
        user_id: str,
        memories: list[str] | None = None,
        affinity: float | None = None,
    ) -> None:
        existing = self.get_relation(
            session_id=session_id, persona_id=persona_id, user_id=user_id
        )
        if existing is None:
            self._execute(
                "INSERT INTO user_relation (session_id, persona_id, user_id, memories, "
                "affinity, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    persona_id,
                    user_id,
                    json.dumps(list(memories or []), ensure_ascii=False),
                    float(affinity if affinity is not None else 0.5),
                    time.time(),
                ),
            )
            return
        merged = list(existing.get("memories") or [])
        for item in memories or []:
            if item not in merged:
                merged.append(item)
        self._execute(
            "UPDATE user_relation SET memories = ?, affinity = ?, updated_at = ? "
            "WHERE session_id = ? AND persona_id = ? AND user_id = ?",
            (
                json.dumps(merged[-50:], ensure_ascii=False),
                float(affinity if affinity is not None else existing.get("affinity", 0.5)),
                time.time(),
                session_id,
                persona_id,
                user_id,
            ),
        )

    def get_relation(
        self, *, session_id: str, persona_id: str, user_id: str
    ) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM user_relation WHERE session_id = ? AND persona_id = ? AND user_id = ?",
            (session_id, persona_id, user_id),
        )
        if not rows:
            return None
        item = dict(rows[0])
        try:
            item["memories"] = json.loads(item.get("memories") or "[]")
        except json.JSONDecodeError:
            item["memories"] = []
        return item

    # ---------------- 事件日志 ----------------

    def add_event(
        self,
        *,
        session_id: str,
        persona_id: str = "",
        world_time: int = 0,
        event_type: str,
        detail: dict[str, Any] | None = None,
    ) -> int:
        cursor = self._execute(
            "INSERT INTO event_log (session_id, persona_id, world_time, event_type, detail, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                persona_id,
                int(world_time),
                event_type,
                json.dumps(detail or {}, ensure_ascii=False),
                time.time(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def query_events(
        self,
        *,
        session_id: str | None = None,
        limit: int = 100,
        event_type: str | None = None,
        keyword: str | None = None,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM event_log WHERE 1 = 1"
        params: list[Any] = []
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        if keyword:
            sql += " AND detail LIKE ?"
            params.append(f"%{keyword}%")
        if before_id:
            sql += " AND id < ?"
            params.append(int(before_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self._query(sql, params)
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.get("detail") or "{}")
            except json.JSONDecodeError:
                item["detail"] = {}
            result.append(item)
        return result

    def prune_events(self, keep_per_session: int = 500) -> None:
        self._execute(
            "DELETE FROM event_log WHERE id NOT IN "
            "(SELECT id FROM event_log ORDER BY id DESC LIMIT ?)",
            (int(keep_per_session) * 50,),
        )

    def prune_session_events(self, session_id: str, keep: int = 1500) -> int:
        """只保留某个会话最近 N 条事件，防止日志无限增长。返回删除条数。"""

        if not session_id:
            return 0
        cursor = self._execute(
            "DELETE FROM event_log WHERE session_id = ? AND id NOT IN "
            "(SELECT id FROM event_log WHERE session_id = ? ORDER BY id DESC LIMIT ?)",
            (session_id, session_id, max(50, int(keep))),
        )
        return int(cursor.rowcount or 0)

    def event_types(self, *, session_id: str | None = None) -> list[str]:
        """该会话出现过的所有事件类型（给日志页的筛选下拉用）。"""

        if session_id:
            rows = self._query(
                "SELECT DISTINCT event_type FROM event_log WHERE session_id = ? "
                "ORDER BY event_type",
                (session_id,),
            )
        else:
            rows = self._query("SELECT DISTINCT event_type FROM event_log ORDER BY event_type")
        return [str(row["event_type"]) for row in rows]

    # ---------------- 日程触发记录 ----------------

    def mark_schedule_fired(self, *, session_id: str, schedule_id: str, day_key: str) -> bool:
        """记录一次日程触发；如果当天已经触发过，返回 False。"""

        try:
            self._execute(
                "INSERT INTO schedule_fire (session_id, schedule_id, day_key, fired_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, schedule_id, day_key, time.time()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def clear_schedule_fires(self, older_than_days: int = 7) -> None:
        self._execute(
            "DELETE FROM schedule_fire WHERE fired_at < ?",
            (time.time() - older_than_days * 86400,),
        )

    # ---------------- 键值 ----------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM kv WHERE key = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except json.JSONDecodeError:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )

    def kv_delete(self, key: str) -> None:
        self._execute("DELETE FROM kv WHERE key = ?", (key,))


class AsyncDatabase:
    """把同步 Database 包装成异步接口，避免阻塞 AstrBot 事件循环。"""

    def __init__(self, path: str | Path) -> None:
        self._db = Database(path)
        self._write_lock = asyncio.Lock()

    @property
    def raw(self) -> Database:
        return self._db

    async def close(self) -> None:
        await asyncio.to_thread(self._db.close)

    async def call(self, func_name: str, *args: Any, **kwargs: Any) -> Any:
        func = getattr(self._db, func_name)
        return await asyncio.to_thread(func, *args, **kwargs)

    async def run(self, func, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(func, *args, **kwargs)
