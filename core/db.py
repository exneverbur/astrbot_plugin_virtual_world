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

-- 用户画像：一人一行（按"她"存，group_id = 会话组代表会话）
CREATE TABLE IF NOT EXISTS user_profile (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    affinity REAL NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL DEFAULT 0,
    last_seen_at REAL NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    digest TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_profile_seen ON user_profile (group_id, last_seen_at);

-- 关于他的事实（喜好 / 约定 / 习惯…），每条都留着"他说的原话"
CREATE TABLE IF NOT EXISTS user_fact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    text TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.6,
    status TEXT NOT NULL DEFAULT 'active',
    source_session TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    mentions TEXT NOT NULL DEFAULT '[]',
    first_at REAL NOT NULL,
    last_confirmed_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_user ON user_fact (group_id, user_id, status);

-- 关系（一个人可以有多条：既是主人也是男友；解除后保留成 past）
CREATE TABLE IF NOT EXISTS user_bond (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    slot TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'current',
    since REAL NOT NULL DEFAULT 0,
    until REAL NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.6,
    asserted_by TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bond_user ON user_bond (group_id, user_id, status);

-- 好感度变化日志（可见、可解释）
CREATE TABLE IF NOT EXISTS affinity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    at REAL NOT NULL,
    delta REAL NOT NULL DEFAULT 0,
    value_after REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_affinity_user ON affinity_log (group_id, user_id, at);

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

CREATE TABLE IF NOT EXISTS state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    world_time INTEGER NOT NULL DEFAULT 0,
    at REAL NOT NULL,
    affect REAL NOT NULL DEFAULT 0,
    valence REAL NOT NULL DEFAULT 0.5,
    energy REAL NOT NULL DEFAULT 0,
    loneliness REAL NOT NULL DEFAULT 0,
    curiosity REAL NOT NULL DEFAULT 0,
    boredom REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_state_history ON state_history (session_id, at);

CREATE TABLE IF NOT EXISTS image_cache (
    fingerprint TEXT PRIMARY KEY,
    caption TEXT NOT NULL DEFAULT '',
    prefix TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_fire (
    session_id TEXT NOT NULL,
    schedule_id TEXT NOT NULL,
    day_key TEXT NOT NULL,
    slot TEXT NOT NULL DEFAULT '',
    fired_at REAL NOT NULL,
    PRIMARY KEY (session_id, schedule_id, day_key, slot)
);

CREATE TABLE IF NOT EXISTS forward_cache (
    fingerprint TEXT PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL
);
"""

# 旧版本建的 schedule_fire 没有 slot 列（主键是 会话+日程+日期），
# 那样「当天改完时间」也不会再触发。这里做一次原地重建。
_MIGRATE_SCHEDULE_FIRE = """
ALTER TABLE schedule_fire RENAME TO schedule_fire_legacy;
CREATE TABLE schedule_fire (
    session_id TEXT NOT NULL,
    schedule_id TEXT NOT NULL,
    day_key TEXT NOT NULL,
    slot TEXT NOT NULL DEFAULT '',
    fired_at REAL NOT NULL,
    PRIMARY KEY (session_id, schedule_id, day_key, slot)
);
INSERT OR IGNORE INTO schedule_fire (session_id, schedule_id, day_key, slot, fired_at)
    SELECT session_id, schedule_id, day_key, '', fired_at FROM schedule_fire_legacy;
DROP TABLE schedule_fire_legacy;
"""


def _json_list(value: Any) -> list[Any]:
    """把库里存的 JSON 数组读回来（坏了就当空列表）。"""

    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(data) if isinstance(data, list) else []


def _profile_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        payload = json.loads(item.get("payload") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    item["payload"] = payload if isinstance(payload, dict) else {}
    item["affinity"] = float(item.get("affinity") or 0.0)
    return item


def _fact_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["mentions"] = _json_list(item.get("mentions"))
    item["pinned"] = bool(item.get("pinned"))
    item["confidence"] = float(item.get("confidence") or 0.0)
    return item


class Database:
    """同步 sqlite 封装。调用方应通过 AsyncDatabase 使用，避免阻塞事件循环。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock:
            self._conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        self._migrate(conn)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(schedule_fire)")}
        if columns and "slot" not in columns:
            conn.executescript(_MIGRATE_SCHEDULE_FIRE)
            conn.commit()
        # 记忆分层：raw（原始片段）→ gist（要点）；外加"当时原话"、参与者、回访时间
        memory_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(node_memory)")
        }
        for name, ddl in (
            ("tier", "ALTER TABLE node_memory ADD COLUMN tier TEXT NOT NULL DEFAULT 'raw'"),
            ("context", "ALTER TABLE node_memory ADD COLUMN context TEXT NOT NULL DEFAULT ''"),
            (
                "participants",
                "ALTER TABLE node_memory ADD COLUMN participants TEXT NOT NULL DEFAULT '[]'",
            ),
            (
                "keywords",
                "ALTER TABLE node_memory ADD COLUMN keywords TEXT NOT NULL DEFAULT '[]'",
            ),
            (
                "next_review_at",
                "ALTER TABLE node_memory ADD COLUMN next_review_at REAL NOT NULL DEFAULT 0",
            ),
            (
                "pinned",
                "ALTER TABLE node_memory ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "folded_at",
                "ALTER TABLE node_memory ADD COLUMN folded_at REAL NOT NULL DEFAULT 0",
            ),
        ):
            if memory_columns and name not in memory_columns:
                conn.execute(ddl)
        conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _ensure_open(self) -> None:
        """连接被关掉之后自己重新连上。

        插件热重载时宿主会先调 terminate()（关库），之后才解绑事件处理器，
        这个窗口里进来的消息还会走到旧实例；与其让它一直报
        「Cannot operate on a closed database」，不如自己把连接接回来。
        """

        try:
            self._conn.execute("SELECT 1")
            return
        except sqlite3.ProgrammingError:
            pass
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = self._connect()

    # ---------------- 通用 ----------------

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number != number:  # NaN
            return default
        return number

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            self._ensure_open()
            try:
                cursor = self._conn.execute(sql, tuple(params))
                self._conn.commit()
                return cursor
            except sqlite3.ProgrammingError:
                # 连接是在中途被关掉的：重连一次再试
                self._conn = self._connect()
                cursor = self._conn.execute(sql, tuple(params))
                self._conn.commit()
                return cursor

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            self._ensure_open()
            try:
                return list(self._conn.execute(sql, tuple(params)))
            except sqlite3.ProgrammingError:
                self._conn = self._connect()
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
        tier: str = "raw",
        context: str = "",
        participants: list[str] | None = None,
        keywords: list[str] | None = None,
        next_review_at: float = 0.0,
        pinned: bool = False,
    ) -> int:
        cursor = self._execute(
            "INSERT INTO node_memory (session_id, persona_id, scope, node_id, content, "
            "type, related_users, emotion, weight, created_at, last_recalled, recall_count, source, "
            "tier, context, participants, keywords, next_review_at, pinned, folded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, 0)",
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
                str(tier or "raw"),
                str(context or ""),
                json.dumps(list(participants or []), ensure_ascii=False),
                json.dumps(list(keywords or []), ensure_ascii=False),
                float(next_review_at or 0.0),
                1 if pinned else 0,
            ),
        )
        return int(cursor.lastrowid or 0)

    def query_memories(
        self,
        *,
        session_id: str | None = None,
        session_ids: list[str] | None = None,
        persona_id: str | None = None,
        node_id: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        min_weight: float | None = None,
        tiers: list[str] | None = None,
        participants: list[str] | None = None,
        due_review_before: float | None = None,
        limit: int = 200,
        order: str = "created_at DESC",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM node_memory WHERE 1 = 1"
        params: list[Any] = []
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        if session_ids:
            # 会话组的召回：组里几个会话的记忆一起看（记忆本身还是各存各的）
            marks = ",".join("?" for _ in session_ids)
            sql += f" AND session_id IN ({marks})"
            params.extend([str(item) for item in session_ids])
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
        if tiers:
            marks = ",".join("?" for _ in tiers)
            sql += f" AND tier IN ({marks})"
            params.extend([str(item) for item in tiers])
        if due_review_before is not None:
            sql += " AND next_review_at > 0 AND next_review_at <= ?"
            params.append(float(due_review_before))
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(int(limit))
        rows = [self._memory_row(row) for row in self._query(sql, params)]
        wanted = [str(item) for item in (participants or []) if str(item)]
        if wanted:
            rows = [
                row
                for row in rows
                if any(
                    user in [str(entry) for entry in (row.get("participants") or [])]
                    or user in [str(entry) for entry in (row.get("related_users") or [])]
                    for user in wanted
                )
            ]
        return rows

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["related_users"] = json.loads(item.get("related_users") or "[]")
        except json.JSONDecodeError:
            item["related_users"] = []
        for key in ("participants", "keywords"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except json.JSONDecodeError:
                item[key] = []
        item["pinned"] = bool(item.get("pinned"))
        item["tier"] = str(item.get("tier") or "raw")
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
        """召回 = 提取练习：被想起来的记忆**加强**一点（同时记下时间）。"""

        if not ids:
            return
        stamp = when if when is not None else time.time()
        with self._lock:
            self._conn.executemany(
                "UPDATE node_memory SET last_recalled = ?, recall_count = recall_count + 1, "
                "weight = MIN(1.0, weight + 0.02) "
                "WHERE id = ?",
                [(stamp, int(i)) for i in ids],
            )
            self._conn.commit()

    def fold_memory(self, memory_id: int, *, text: str = "", when: float | None = None) -> None:
        """把一条记忆"折叠"成要点：原文进 ``context``，正文换成更短的要点的。

        遗忘只做这一步，**不删**：她还能看到当时那句话，只是不再占着大段原文。
        """

        row = None
        rows = self._query("SELECT * FROM node_memory WHERE id = ?", (int(memory_id),))
        if rows:
            row = dict(rows[0])
        if row is None:
            return
        stamp = float(when if when is not None else time.time())
        body = " ".join(str(text or "").split()) or str(row.get("content") or "")
        context = str(row.get("context") or "").strip()
        if not context:
            context = str(row.get("content") or "")
        self.update_memory(
            int(memory_id),
            content=body[:200],
            context=context[:600],
            tier="gist",
            folded_at=stamp,
        )

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

    def memory_stats(
        self, session_id: str | None = None, session_ids: list[str] | None = None
    ) -> dict[str, Any]:
        # 会话组：把组里几个会话的记忆合起来统计
        wanted = [str(item) for item in (session_ids or []) if str(item)]
        if wanted:
            marks = ",".join("?" for _ in wanted)
            where = f"WHERE session_id IN ({marks})"
            params: tuple[Any, ...] = tuple(wanted)
        elif session_id:
            where = "WHERE session_id = ?"
            params = (session_id,)
        else:
            where = ""
            params = ()
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

    # ---------------- 用户画像 ----------------

    def get_user_profile(self, *, group_id: str, user_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM user_profile WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        return _profile_row(rows[0]) if rows else None

    def list_user_profiles(self, *, group_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """这个组里认识的人，最近说过话的排前面（编辑器与"缩略版画像"用）。"""

        rows = self._query(
            "SELECT * FROM user_profile WHERE group_id = ? "
            "ORDER BY last_seen_at DESC LIMIT ?",
            (group_id, max(1, int(limit))),
        )
        return [_profile_row(row) for row in rows]

    def upsert_user_profile(
        self,
        *,
        group_id: str,
        user_id: str,
        payload: dict[str, Any],
        affinity: float,
        first_seen_at: float,
        last_seen_at: float,
        message_count: int,
        digest: str,
    ) -> None:
        self._execute(
            "INSERT INTO user_profile (group_id, user_id, payload, affinity, first_seen_at, "
            "last_seen_at, message_count, digest, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(group_id, user_id) DO UPDATE SET payload = excluded.payload, "
            "affinity = excluded.affinity, first_seen_at = excluded.first_seen_at, "
            "last_seen_at = excluded.last_seen_at, message_count = excluded.message_count, "
            "digest = excluded.digest, updated_at = excluded.updated_at",
            (
                group_id,
                user_id,
                json.dumps(payload or {}, ensure_ascii=False),
                float(affinity),
                float(first_seen_at or 0.0),
                float(last_seen_at or 0.0),
                int(message_count or 0),
                str(digest or ""),
                time.time(),
            ),
        )

    def delete_user_profile(self, *, group_id: str, user_id: str) -> int:
        """把一个人整个忘掉（画像 + 事实 + 关系 + 好感日志）。"""

        removed = 0
        for table in ("user_profile", "user_fact", "user_bond", "affinity_log"):
            cursor = self._execute(
                f"DELETE FROM {table} WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            removed += int(cursor.rowcount or 0)
        return removed

    # ---------------- 关于他的事实 ----------------

    def add_user_fact(
        self,
        *,
        group_id: str,
        user_id: str,
        kind: str = "other",
        text: str,
        evidence: str = "",
        context: str = "",
        confidence: float = 0.6,
        status: str = "active",
        source_session: str = "",
        pinned: bool = False,
        mentions: list[str] | None = None,
        first_at: float | None = None,
    ) -> int:
        now = time.time()
        cursor = self._execute(
            "INSERT INTO user_fact (group_id, user_id, kind, text, evidence, context, "
            "confidence, status, source_session, pinned, mentions, first_at, last_confirmed_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                group_id,
                user_id,
                str(kind or "other"),
                str(text or "").strip(),
                str(evidence or ""),
                str(context or ""),
                float(confidence),
                str(status or "active"),
                str(source_session or ""),
                1 if pinned else 0,
                json.dumps(list(mentions or []), ensure_ascii=False),
                float(first_at if first_at is not None else now),
                now if status == "active" else 0.0,
                now,
            ),
        )
        return int(cursor.lastrowid or 0)

    def list_user_facts(
        self, *, group_id: str, user_id: str, statuses: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        wanted = [str(item) for item in (statuses or ["active", "candidate"]) if str(item)]
        marks = ",".join("?" for _ in wanted) or "?"
        params: list[Any] = [group_id, user_id, *wanted]
        rows = self._query(
            "SELECT * FROM user_fact WHERE group_id = ? AND user_id = ? "
            f"AND status IN ({marks}) ORDER BY pinned DESC, last_confirmed_at DESC, id DESC LIMIT ?",
            (*params, max(1, int(limit))),
        )
        return [_fact_row(row) for row in rows]

    def list_group_facts(
        self, *, group_id: str, user_ids: list[str], limit: int = 120
    ) -> list[dict[str, Any]]:
        """这一轮涉及的几个人的事实一起取（提示词注入用）。"""

        wanted = [str(item) for item in (user_ids or []) if str(item)]
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        rows = self._query(
            "SELECT * FROM user_fact WHERE group_id = ? AND user_id IN "
            f"({marks}) AND status = 'active' "
            "ORDER BY pinned DESC, last_confirmed_at DESC, id DESC LIMIT ?",
            (group_id, *wanted, max(1, int(limit))),
        )
        return [_fact_row(row) for row in rows]

    def update_user_fact(
        self,
        *,
        fact_id: int,
        text: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        pinned: bool | None = None,
        confidence: float | None = None,
        last_confirmed_at: float | None = None,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if text is not None:
            sets.append("text = ?")
            params.append(str(text))
        if kind is not None:
            sets.append("kind = ?")
            params.append(str(kind))
        if status is not None:
            sets.append("status = ?")
            params.append(str(status))
        if pinned is not None:
            sets.append("pinned = ?")
            params.append(1 if pinned else 0)
        if confidence is not None:
            sets.append("confidence = ?")
            params.append(float(confidence))
        if last_confirmed_at is not None:
            sets.append("last_confirmed_at = ?")
            params.append(float(last_confirmed_at))
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.append(time.time())
        cursor = self._execute(
            f"UPDATE user_fact SET {', '.join(sets)} WHERE id = ?",
            (*params, int(fact_id)),
        )
        return bool(cursor.rowcount)

    def delete_user_fact(self, *, fact_id: int) -> bool:
        cursor = self._execute("DELETE FROM user_fact WHERE id = ?", (int(fact_id),))
        return bool(cursor.rowcount)

    # ---------------- 关系 ----------------

    def add_user_bond(
        self,
        *,
        group_id: str,
        user_id: str,
        type: str,
        slot: str = "",
        status: str = "current",
        since: float = 0.0,
        evidence: str = "",
        confidence: float = 0.6,
        asserted_by: str = "",
    ) -> int:
        cursor = self._execute(
            "INSERT INTO user_bond (group_id, user_id, type, slot, status, since, until, "
            "evidence, confidence, asserted_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (
                group_id,
                user_id,
                str(type),
                str(slot or ""),
                str(status or "current"),
                float(since or time.time()),
                str(evidence or ""),
                float(confidence),
                str(asserted_by or ""),
                time.time(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def list_user_bonds(
        self,
        *,
        group_id: str,
        user_id: str = "",
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        where = ["group_id = ?"]
        params: list[Any] = [group_id]
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        wanted = [str(item) for item in (statuses or []) if str(item)]
        if wanted:
            marks = ",".join("?" for _ in wanted)
            where.append(f"status IN ({marks})")
            params.extend(wanted)
        rows = self._query(
            f"SELECT * FROM user_bond WHERE {' AND '.join(where)} "
            "ORDER BY status = 'current' DESC, since DESC, id DESC LIMIT ?",
            (*params, max(1, int(limit))),
        )
        return [dict(row) for row in rows]

    def get_user_bond(self, *, bond_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM user_bond WHERE id = ?", (int(bond_id),))
        return dict(rows[0]) if rows else None

    def close_user_bond(self, *, bond_id: int, until: float | None = None, status: str = "past") -> bool:
        cursor = self._execute(
            "UPDATE user_bond SET status = ?, until = ?, updated_at = ? WHERE id = ?",
            (str(status), float(until or time.time()), time.time(), int(bond_id)),
        )
        return bool(cursor.rowcount)

    def delete_user_bond(self, *, bond_id: int) -> bool:
        cursor = self._execute("DELETE FROM user_bond WHERE id = ?", (int(bond_id),))
        return bool(cursor.rowcount)

    # ---------------- 好感度日志 ----------------

    def add_affinity_log(
        self,
        *,
        group_id: str,
        user_id: str,
        delta: float,
        value_after: float,
        reason: str = "",
        source: str = "",
        at: float | None = None,
    ) -> int:
        cursor = self._execute(
            "INSERT INTO affinity_log (group_id, user_id, at, delta, value_after, reason, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                group_id,
                user_id,
                float(at if at is not None else time.time()),
                float(delta),
                float(value_after),
                str(reason or ""),
                str(source or ""),
            ),
        )
        return int(cursor.lastrowid or 0)

    def list_affinity_logs(
        self, *, group_id: str, user_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM affinity_log WHERE group_id = ? AND user_id = ? "
            "ORDER BY at DESC, id DESC LIMIT ?",
            (group_id, user_id, max(1, int(limit))),
        )
        return [dict(row) for row in rows]

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

    # ---------------- 数值历史（给编辑器画曲线） ----------------

    # ---------------- 图片转述缓存 ----------------
    #
    # 同一个表情包会在群里反复出现，看图（多模态）是这里最贵的一次调用。
    # 按"图片指纹"而不是地址缓存：同一个文件换个临时链接也能命中。

    def get_image_caption(
        self, *, fingerprint: str, max_age_seconds: float = 0.0
    ) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT caption, prefix, model, hits, created_at, last_used_at"
            " FROM image_cache WHERE fingerprint = ?",
            (str(fingerprint or ""),),
        )
        if not rows:
            return None
        row = dict(rows[0])
        if max_age_seconds > 0 and time.time() - float(row.get("created_at") or 0) > float(
            max_age_seconds
        ):
            return None
        return row

    def put_image_caption(
        self,
        *,
        fingerprint: str,
        caption: str,
        prefix: str = "",
        model: str = "",
    ) -> None:
        now = time.time()
        self._execute(
            "INSERT INTO image_cache (fingerprint, caption, prefix, model, hits, created_at,"
            " last_used_at) VALUES (?, ?, ?, ?, 0, ?, ?)"
            " ON CONFLICT(fingerprint) DO UPDATE SET caption = excluded.caption,"
            " prefix = excluded.prefix, model = excluded.model, created_at = excluded.created_at",
            (
                str(fingerprint or ""),
                str(caption or ""),
                str(prefix or ""),
                str(model or ""),
                now,
                now,
            ),
        )

    def touch_image_caption(self, *, fingerprint: str) -> None:
        """命中一次：计数 +1，并刷新使用时间（用来做 LRU 与"省了多少次"）。"""

        self._execute(
            "UPDATE image_cache SET hits = hits + 1, last_used_at = ? WHERE fingerprint = ?",
            (time.time(), str(fingerprint or "")),
        )

    def trim_image_cache(self, *, keep: int, max_age_seconds: float = 0.0) -> int:
        limit = max(1, int(keep))
        if max_age_seconds > 0:
            self._execute(
                "DELETE FROM image_cache WHERE created_at < ?",
                (time.time() - float(max_age_seconds),),
            )
        cursor = self._execute(
            "DELETE FROM image_cache WHERE fingerprint NOT IN ("
            " SELECT fingerprint FROM image_cache ORDER BY last_used_at DESC LIMIT ?)",
            (limit,),
        )
        return int(cursor.rowcount or 0)

    def image_cache_stats(self) -> dict[str, Any]:
        rows = self._query(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(hits), 0) AS hits FROM image_cache"
        )
        if not rows:
            return {"entries": 0, "hits": 0}
        return {"entries": int(rows[0]["entries"] or 0), "hits": int(rows[0]["hits"] or 0)}

    # ---------------- 合并转发摘要缓存 ----------------
    #
    # 一条转发往往几十条消息，摘要是这里最贵的一次调用；同一条转发
    # （别人转来转去、她反复被人 @ 同一张转发）按内容指纹复用。

    def get_forward_summary(
        self, *, fingerprint: str, max_age_seconds: float = 0.0
    ) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT summary, model, hits, created_at, last_used_at"
            " FROM forward_cache WHERE fingerprint = ?",
            (str(fingerprint or ""),),
        )
        if not rows:
            return None
        row = dict(rows[0])
        if max_age_seconds > 0 and time.time() - float(row.get("created_at") or 0) > float(
            max_age_seconds
        ):
            return None
        return row

    def put_forward_summary(
        self, *, fingerprint: str, summary: str, model: str = ""
    ) -> None:
        now = time.time()
        self._execute(
            "INSERT INTO forward_cache (fingerprint, summary, model, hits, created_at,"
            " last_used_at) VALUES (?, ?, ?, 0, ?, ?)"
            " ON CONFLICT(fingerprint) DO UPDATE SET summary = excluded.summary,"
            " model = excluded.model, created_at = excluded.created_at",
            (str(fingerprint or ""), str(summary or ""), str(model or ""), now, now),
        )

    def touch_forward_summary(self, *, fingerprint: str) -> None:
        """命中一次：计数 +1，并刷新使用时间（用来做 LRU）。"""

        self._execute(
            "UPDATE forward_cache SET hits = hits + 1, last_used_at = ?"
            " WHERE fingerprint = ?",
            (time.time(), str(fingerprint or "")),
        )

    def trim_forward_cache(self, *, keep: int, max_age_seconds: float = 0.0) -> int:
        limit = max(1, int(keep))
        if max_age_seconds > 0:
            self._execute(
                "DELETE FROM forward_cache WHERE created_at < ?",
                (time.time() - float(max_age_seconds),),
            )
        cursor = self._execute(
            "DELETE FROM forward_cache WHERE fingerprint NOT IN ("
            " SELECT fingerprint FROM forward_cache ORDER BY last_used_at DESC LIMIT ?)",
            (limit,),
        )
        return int(cursor.rowcount or 0)

    def add_state_history(
        self,
        *,
        session_id: str,
        world_time: int = 0,
        at: float,
        values: dict[str, Any] | None = None,
    ) -> None:
        """记一帧数值快照。

        每个 tick 一行（默认 1 分钟），一个会话一天 1440 行——为了画"她这几天
        过得怎么样"的曲线，这是最便宜的存法。
        """

        data = dict(values or {})
        self._execute(
            "INSERT INTO state_history (session_id, world_time, at, affect, valence, energy,"
            " loneliness, curiosity, boredom) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                int(world_time),
                float(at),
                self._as_float(data.get("affect"), 0.0),
                self._as_float(data.get("valence"), 0.5),
                self._as_float(data.get("energy"), 0.0),
                self._as_float(data.get("loneliness"), 0.0),
                self._as_float(data.get("curiosity"), 0.0),
                self._as_float(data.get("boredom"), 0.0),
            ),
        )

    def query_state_history(
        self, *, session_id: str, since: float = 0.0, limit: int = 6000
    ) -> list[dict[str, Any]]:
        """按时间正序取一段历史（越靠后越新）。"""

        rows = self._query(
            "SELECT world_time, at, affect, valence, energy, loneliness, curiosity, boredom"
            " FROM state_history WHERE session_id = ? AND at >= ? ORDER BY at ASC LIMIT ?",
            (session_id, float(since), int(limit)),
        )
        return [dict(row) for row in rows]

    def trim_state_history(self, *, session_id: str, keep: int) -> int:
        """只保留最近 N 行。"""

        limit = max(1, int(keep))
        cursor = self._execute(
            "DELETE FROM state_history WHERE session_id = ? AND id NOT IN ("
            " SELECT id FROM state_history WHERE session_id = ? ORDER BY id DESC LIMIT ?)",
            (session_id, session_id, limit),
        )
        return int(cursor.rowcount or 0)

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

    def mark_schedule_fired(
        self, *, session_id: str, schedule_id: str, day_key: str, slot: str = ""
    ) -> bool:
        """记录一次日程触发；同一个「日期 + 时间点」触发过就返回 False。

        ``slot`` 是配置里的时间（``HH:MM``）：把时间也纳入去重键之后，
        「当天改完触发时间」也能正常再触发一次。
        """

        try:
            self._execute(
                "INSERT INTO schedule_fire (session_id, schedule_id, day_key, slot, fired_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, schedule_id, day_key, str(slot or ""), time.time()),
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
