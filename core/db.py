"""持久层：标准库 sqlite3（运行时状态 + 记忆 + 事件 + 键值）。

为什么不用 SQLModel/aiosqlite：避免与宿主环境的 SQLAlchemy/驱动版本冲突，
也避免给用户增加安装依赖。所有写操作用 WAL + busy_timeout，读写都在线程池里执行
（见 AsyncDatabase），不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import sqlite3
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

# 阶段A1：事件身份 / 真实性标签 / 结构版本与 core/source_events.py 保持同一个真相
from .source_events import REALITY_REAL
from .source_events import SCHEMA_VERSION as SOURCE_EVENTS_SCHEMA_VERSION
from .source_events import Share, SourceEvent, share_id_for

# 阶段A2：分享登记的送达状态沿用发送回执口径（accepted 不是 delivered）
from .ports import SEND_ACCEPTED, SEND_FAILED, SEND_UNKNOWN

# 阶段B3：任务账本的形状与状态词表以 core/agent_tasks.py 的纯逻辑为唯一真相
# （本文件只负责存得住、读得回，不重复实现状态机）
from . import agent_tasks

# 用户 m00001：她说"等会儿"时那句待办的字数上限以 core/commitments.py 为唯一真相
from .commitments import COMMITMENT_TASK_CHARS, clip_task_text

# 阶段C：谈资 / 续说租约的列名与取值口径以 core/experience.py 的 to_row()/from_row()
# 为唯一真相（本文件不另起列名，也不重复定义状态词表）
from .experience import (
    CLAIM_ACTIVE,
    CLAIM_EXPIRED,
    CLAIM_STATUSES,
    EXPERIENCE_SCHEMA_VERSION,
    SOURCE_KINDS,
    PROACTIVE_ACTS,
    COMMUNICATIVE_ACTS,
    SOURCE_KIND_CHAT,
    TOPIC_AVAILABLE,
    TOPIC_DELETED,
    TOPIC_EXPIRED,
    TOPIC_STATUSES,
    ShareClaim,
    Topic,
    claim_id_for,
    is_empty_declaration,
    merge_visibility,
    normalize_visibility,
    visibility_sensitivity,
)

# 流程型 skill 的形状与版本号以 core/skills.py 为唯一真相（本文件只管存得住、读得回）
from . import skills as skill_policy

logger = logging.getLogger(__name__)

#: ``vw_skills`` 的 v2 形状：每个字段都会进提示词（见 core/skills.py 的渲染），
#: 所以不再有 ``detail`` 这种"整包塞进去、谁也读不到"的列。
VW_SKILLS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS vw_skills (
    skill_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    when_to_use TEXT NOT NULL DEFAULT '[]',
    when_not TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    capabilities TEXT NOT NULL DEFAULT '[]',
    verify TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    scope TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'agent',
    version INTEGER NOT NULL DEFAULT 1,
    hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 2
);
CREATE INDEX IF NOT EXISTS idx_vw_skills_status
    ON vw_skills (status, updated_at DESC);
"""

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

-- 信赖值观测（T15-S3）：这条记录是**结算凭据**，不是展示数据。
-- 唯一约束 ``(source_kind, source_id, person_id)`` 就是幂等闸门：同一次整理重试、
-- 同一条承诺结算两遍，第二次 INSERT 直接被挡掉，绝不会二次加分。
-- B 通道（事件内即时结算）还没落地，``due_at`` / ``horizon_days`` / ``status``
-- 这些承诺类列等 B 实现时再补，本轮只落 A 通道用得到的部分。
CREATE TABLE IF NOT EXISTS trust_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL DEFAULT '',
    person_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'advice',
    delta REAL NOT NULL DEFAULT 0,
    value_after REAL NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    applied INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    UNIQUE (source_kind, source_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_trust_person
    ON trust_observations (group_id, person_id, created_at);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    world_time INTEGER NOT NULL DEFAULT 0,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    tool_call_id TEXT NOT NULL DEFAULT '',
    origin_session TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_event_session ON event_log (session_id, created_at);
-- 注意：``idx_event_tool_call``（event_log.tool_call_id）**不能**写在这里。
-- SCHEMA 在 ``_migrate()`` **之前**执行，老库上 ``CREATE TABLE IF NOT EXISTS`` 是空操作
-- （表已存在、没有这一列），紧跟的建索引语句就会 "no such column" 把插件整个挡在启动外。
-- 新列的索引一律交给 ``_migrate_event_log_tool_call_id()``：它先 ALTER 加列再建索引，
-- 新库（列已在）与老库（补列）两条路径都覆盖。

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

CREATE TABLE IF NOT EXISTS image_caption_failure (
    fingerprint TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    failed_at REAL NOT NULL
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
    # 同 _memory_row：老库可能没有这两列，补默认值让调用方永远拿得到键
    item["status"] = str(item.get("status") or "active")
    item["superseded_by"] = str(item.get("superseded_by") or "")
    try:
        item["superseded_at"] = float(item.get("superseded_at") or 0.0)
    except (TypeError, ValueError):
        item["superseded_at"] = 0.0
    return item


class _TransactionConnection:
    """事务期间的连接代理：让 ``conn.commit()`` 变成"先攒着"（阶段F3）。

    整理结果必须**一个事务提交**（计划 §10 F3"提交前任何出错 rollback，不部分成功
    却标全成功"）。可记忆 / 画像 / 游标的写函数各自都会 ``conn.commit()``，
    所以事务期间把 ``Database._conn`` 换成这个代理：``execute`` 照旧落到真连接，
    ``commit()`` 什么都不做，等 :meth:`Database.transaction` 退出时统一提交或回滚。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def commit(self) -> None:
        """事务里由外层统一提交。"""

    def rollback(self) -> None:
        """事务里由外层统一回滚。"""

    def close(self) -> None:
        """代理不负责关闭连接。"""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _strict_json_object(value: Any) -> str:
    import math
    def validate(item: Any) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError('non-finite KV claim payload')
            return
        if isinstance(item, list):
            for entry in item:
                validate(entry)
            return
        if isinstance(item, dict):
            for key, entry in item.items():
                if not isinstance(key, str):
                    raise ValueError('KV claim object keys must be strings')
                validate(entry)
            return
        raise ValueError('KV claim payload must contain only JSON values')
    if not isinstance(value, dict):
        raise ValueError('KV claim payload must be an object')
    try:
        validate(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (RecursionError, TypeError) as exc:
        raise ValueError('invalid KV claim payload') from exc


_WRITE_SQL_WORDS = frozenset(
    {"or", "ignore", "replace", "rollback", "abort", "fail", "into", "from"}
)


def _is_read_sql(sql: str) -> bool:
    """这条 SQL 是不是纯读（读失败不算"写失败"，不该进 ``last_write_error``）。"""

    return str(sql or "").lstrip()[:6].lower() in {"select"}


def _write_op(sql: str) -> str:
    """把写 SQL 前两个词当操作名：``insert:world_state`` / ``delete:decision_batches``。

    只为了在 ``/vw status`` 里一眼看出**哪条写**失败了——磁盘满的时候，
    知道「是 world_state 写不进去」比知道「有个异常」有用得多。认不出来就退回动词。
    """

    words = [word.strip('"`[];()') for word in str(sql or "").split()]
    if not words:
        return "write"
    head = words[0].lower()
    for word in words[1:4]:
        low = word.lower()
        if not low or low in _WRITE_SQL_WORDS:
            continue
        if not (low[0].isalpha() or low[0] == "_"):
            continue
        return f"{head}:{low}"
    return head


class Database:
    """同步 sqlite 封装。调用方应通过 AsyncDatabase 使用，避免阻塞事件循环。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._in_transaction = False
        self.write_error_sink: Callable[[str, BaseException], None] | None = None
        """写失败回调 ``(op, exc)``，由引擎挂上来记 ``last_write_error``（storage_health）。

        默认 ``None`` = 没人听；即便如此异常也照常往外抛——**绝不在这里吞掉**。
        2026-10 磁盘满事故就是写失败被吞、她整轮不说话，用户连一条线索都没有。
        """

        self._reported_exc: BaseException | None = None
        """最近一次已上报的异常对象：同一个异常从 ``_execute`` 冒到 ``in_transaction``
        只报一次，否则"连续 3 次写失败"会被一次回复凑满（假告警比不告警更坏）。"""
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
        # 记忆作废三列是不是真的在：迁移是 fail-soft 的，老库上万一 ALTER 失败，
        # 读路径就不能拼 ``status = 'active'``（那会 "no such column" 把召回打挂）。
        # 缺列时退回升级前的行为（不过滤），插件照常起来。
        self._memory_has_status = _table_has_column(conn, "node_memory", "status")
        self._fact_has_status = _table_has_column(conn, "user_fact", "status")
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
        # 阶段A1 追加：source_events / schema_meta（见文件末尾同名分区，幂等可重复跑）
        _migrate_source_events(conn)
        # 阶段A2 追加：shares（分享登记，见文件末尾同名分区，幂等可重复跑）
        _migrate_shares(conn)
        # 阶段C 追加：topics / share_claims（谈资与续说租约，见文件末尾同名分区，幂等可重复跑）
        _migrate_topics(conn)
        # 阶段C 追加：source_events 的软删列（来源撤回连坐要用，见同名分区）
        _migrate_source_event_soft_delete(conn)
        # 阶段F3 追加：applied_operations（增量提交的幂等账，幂等可重复跑）
        _migrate_applied_operations(conn)
        _migrate_topics_storage(conn)
        # P1：agent_tasks 的等待态与任务包字段（缺列就补，幂等）
        _ensure_agent_task_columns(conn)
        # 2026-10 重写：vw_skills 整表换成 v2（旧内容归档到 backups/ 后连表清掉）
        _migrate_vw_skills(conn)
        # 阶段B0 追加：topic_vectors（谈资向量表，只建表/建索引/给读写函数，
        # B0 不接检索、不索引任何数据；见文件末尾同名分区，幂等可重复跑）
        _migrate_topic_vectors(conn)
        # 阶段B1 追加：topic_index_queue（谈资索引队列，只建表/建索引/给读写函数，
        # 不索引任何数据；见文件末尾同名分区，幂等可重复跑）
        _migrate_topic_index_queue(conn)
        # 记忆向量缓存表（记忆语义召回用；跟 topic_vectors 同构，见文件末尾）
        _migrate_memory_vectors(conn)
        # E1 追加：event_log.tool_call_id（工具调用回执的串联列，幂等可重复跑）
        _migrate_event_log_tool_call_id(conn)
        # 追加：event_log.origin_session（真实来源会话 umo，幂等可重复跑）
        _migrate_event_log_origin_session(conn)
        # 阶段M1 追加：meme_assets / meme_entries / meme_sources / meme_variants /
        # meme_usage / image_exposures（表情包收藏与展示记录，只建表/建索引/给读写
        # 函数，不接回复路径；见文件末尾同名分区，幂等可重复跑）
        _migrate_memes(conn)
        # 阶段M4 追加：meme_vectors（表情包向量，独立成表；只建表/建索引/给读写函数）
        _migrate_meme_vectors(conn)
        # 攒图评审收藏（用户 2026-10-24）：meme_collect_candidates / meme_collect_reviews
        # （候选池与评审频率记账；只建表/建索引/给读写函数，判断在 core/meme_collect.py）
        _migrate_meme_collect(conn)
        # 阶段E2 追加：daily_experiences（每日经历账；只建表/建索引/给读写函数）
        _migrate_daily_experiences(conn)
        # 阶段B3 追加：agent_tasks / agent_artifacts（任务账本与产物台账，只建表/建索引/
        # 给读写函数，不接执行路径；见文件末尾同名分区，幂等可重复跑）
        _migrate_agent_tasks(conn)
        # 阶段A4 追加：config_index_queue（配置异步整理队列：租约 / 过期结果丢弃 /
        # 删除即失效 / 重启可恢复；见文件末尾同名分区，幂等可重复跑）
        _migrate_config_index_queue(conn)
        # 追问（用户 m27236）：她说完等回话、隔几分钟没接就再问一句；只建表/建索引/
        # 给读写函数，判断与发送在引擎里（幂等可重复跑）
        _migrate_follow_ups(conn)
        # 临时会话（用户 m00001）：她私聊出去的陌生人先接管一阵子；只建表/建索引/
        # 给读写函数，超时与收尾判断在引擎里（幂等可重复跑）
        _migrate_temp_sessions(conn)
        # 「等会儿」的待办（用户 m00001）：协商里那句 deferred_at 要落成一条能兑现的
        # 待办；只建表/建索引/给读写函数，到点提醒与关闭判断在引擎里（幂等可重复跑）
        _migrate_deferred_commitments(conn)
        # 记忆修复（P2）：node_memory 的 occurrence 计数列 + "按会话取最近/按权重淘汰"
        # 两条索引。只加列与索引、不改既有数据（幂等可重复跑）
        _migrate_memory_retention(conn)
        # 同主题覆盖（用户实机翻车：旧说法没作废，"明天要去体检"和"体检完了"并存）：
        # node_memory / user_fact 的 status / superseded_by / superseded_at 三列与
        # (status) 索引。幂等 + fail-soft，判定规则在 core/memory_supersede.py
        _migrate_memory_supersede(conn)
        # 知识卡（用户：「查完就过去了，聊天记录刷掉她就不记得」）：她查过的梗 / 词 /
        # 事记成一张卡，下次能拿出来用。只建表/建索引/给读写函数，写与召回在引擎里
        # （幂等可重复跑）
        _migrate_knowledge_cards(conn)
        # T8 第一步：修"空声明被当成 deny_all"锁死的历史来源（一次性、幂等；
        # 判据与放弃条件见文件末尾同名分区）
        _migrate_source_visibility_empty_declaration_repair(conn)

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

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """短事务：这一段里的写入要么全落，要么全不落（阶段F3）。

        只在**同一个线程**里包住一串同步写函数。事务期间 ``self._conn`` 换成
        :class:`_TransactionConnection`，各写函数里的 ``conn.commit()`` 不生效；
        退出时统一 ``COMMIT``，中途抛错则 ``ROLLBACK`` 并把异常继续往外抛。
        已经在事务里（同线程嵌套）就沿用外层，不重复 ``BEGIN``。
        """

        with self._lock:
            self._ensure_open()
            if self._in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            proxy = _TransactionConnection(self._conn)
            self._conn = proxy
            self._in_transaction = True
            try:
                yield proxy
            except BaseException:
                self._conn = proxy._conn
                self._in_transaction = False
                proxy._conn.rollback()
                raise
            else:
                self._conn = proxy._conn
                self._in_transaction = False
                try:
                    proxy._conn.commit()
                except Exception as exc:  # noqa: BLE001 - 提交失败也要留痕（磁盘满就在这里炸）
                    self._report_write_error("commit:transaction", exc)
                    raise

    def in_transaction(self, func: Callable[[sqlite3.Connection], Any]) -> Any:
        """把 ``func(conn)`` 整个放进**同一个事务**里跑（阶段F3）。

        记忆 / 画像 / 游标这些写都是同步的，直接在这个函数里连着调；中途抛异常
        就整体回滚——"要么全落，要么全不落，不许部分成功却标全成功"（计划 §10 F3）。
        """

        try:
            with self.transaction() as conn:
                return func(conn)
        except Exception as exc:  # noqa: BLE001 - 事务里的写失败也要留痕（不再静默）
            # 大部分失败已经在 _execute 里报过（同一对象会被去重）；这里是兜住
            # 直接拿 conn.execute 写的那几处。
            self._report_write_error("transaction", exc)
            raise

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number != number:  # NaN
            return default
        return number

    def _report_write_error(self, op: str, exc: BaseException) -> None:
        """把一次写失败报给上层（引擎记 ``last_write_error``）——**只报告，不抛**。

        2026-10 真实事故：磁盘写满 → ``world_state`` / ``decision_batches`` 写不进去
        → 异常在 ``except Exception: pass`` 里被吞 → 她整轮不说话，用户只看到
        「她不理我」，查不出是磁盘满了。现在至少留下「时间 + 哪条写 + 异常」，
        ``/vw status`` 能直接看到。

        同一个异常对象会被 ``_execute`` 和 ``in_transaction`` 各报一次，这里按对象
        身份去重（"连续失败 ≥3 次"必须是真的连续 3 次写失败）。
        """

        if exc is self._reported_exc:
            return
        self._reported_exc = exc
        sink = self.write_error_sink
        if sink is None:
            return
        try:
            sink(op, exc)
        except Exception:  # noqa: BLE001 - 记账本身绝不许把写路径搞崩
            pass

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
                try:
                    cursor = self._conn.execute(sql, tuple(params))
                    self._conn.commit()
                    return cursor
                except Exception as exc:  # noqa: BLE001 - 写失败必须留痕，不能静默
                    if not _is_read_sql(sql):
                        self._report_write_error(_write_op(sql), exc)
                    raise
            except Exception as exc:  # noqa: BLE001 - 写失败必须留痕，不能静默
                if not _is_read_sql(sql):
                    self._report_write_error(_write_op(sql), exc)
                raise

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
        include_empty_node: bool = False,
        scope: str | None = None,
        memory_type: str | None = None,
        min_weight: float | None = None,
        tiers: list[str] | None = None,
        participants: list[str] | None = None,
        due_review_before: float | None = None,
        limit: int = 200,
        order: str = "created_at DESC",
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM node_memory WHERE 1 = 1"
        params: list[Any] = []
        if not include_superseded and getattr(self, "_memory_has_status", False):
            # 默认只用"现在还算数"的记忆：被同主题新说法覆盖掉的旧条目（status=
            # 'superseded'）不再进召回，否则她会把"明天要去体检"和"体检完了"
            # 一起想起来，混着说。老库缺列时（迁移 fail-soft 失败）不过滤，
            # 行为与升级前一致。
            sql += " AND status = 'active'"
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
            if include_empty_node:
                # 空 node_id = 全局记忆（整理/做梦产出的条目就是这个形状）：
                # 节点过滤不能把它挡在外面，否则整批 gist 永远召不回来
                sql += " AND (node_id = ? OR node_id = '')"
                params.append(node_id)
            else:
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
        # 三列可能不在（迁移 fail-soft 失败的老库）：补默认值，调用方永远拿得到键
        item["status"] = str(item.get("status") or "active")
        item["superseded_by"] = str(item.get("superseded_by") or "")
        try:
            item["superseded_at"] = float(item.get("superseded_at") or 0.0)
        except (TypeError, ValueError):
            item["superseded_at"] = 0.0
        return item

    def update_memory(self, memory_id: int, **fields: Any) -> None:
        if not fields:
            return
        columns: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            columns.append(f"{key} = ?")
            if key in ("related_users", "participants", "keywords") and isinstance(
                value, (list, tuple)
            ):
                # 这三列都是 JSON 数组（跨节点去重合并时要把人名/关键词并起来）
                value = json.dumps(list(value), ensure_ascii=False)
            params.append(value)
        params.append(int(memory_id))
        self._execute(
            f"UPDATE node_memory SET {', '.join(columns)} WHERE id = ?", params
        )

    # ---------------- 同主题覆盖（supersede） ----------------
    #
    # 只改状态、**不删行**：被覆盖的旧说法留在库里（status='superseded' +
    # superseded_by 指向新那条 + superseded_at），她提到「以前/之前」时还能回看
    # （``include_superseded=True``），也能手动恢复（:meth:`restore_memory`）。

    def supersede_memory(
        self,
        memory_id: int,
        *,
        by: object = "",
        when: float | None = None,
        why: str = "",
        via: str = "rule",
    ) -> bool:
        """把一条记忆标成 ``superseded``：返回是否真的改了一行（已经作废过 = False）。"""

        number = _as_int(memory_id)
        if number <= 0 or not getattr(self, "_memory_has_status", False):
            return False
        stamp = float(when if when is not None else time.time())
        with self._lock:
            row = self._query(
                "SELECT session_id, persona_id, content, status FROM node_memory WHERE id = ?",
                (number,),
            )
            if not row:
                return False
            if str(row[0]["status"] or "active") != "active":
                return False  # 已经作废 / 已被删：幂等，不重复记账
            self._conn.execute(
                "UPDATE node_memory SET status = 'superseded', superseded_by = ?, "
                "superseded_at = ? WHERE id = ? AND status = 'active'",
                (str(by or ""), stamp, number),
            )
            self._conn.commit()
            detail = {
                "old": number,
                "by": _as_int(by),
                "why": str(why or ""),
                "via": str(via or "rule"),
                "text": str(row[0]["content"] or "")[:200],
            }
            self.add_event(
                session_id=str(row[0]["session_id"] or ""),
                persona_id=str(row[0]["persona_id"] or ""),
                event_type="memory_superseded",
                detail=detail,
            )
        return True

    def restore_memory(self, memory_id: int, *, when: float | None = None) -> bool:
        """把一条被覆盖的记忆恢复成 ``active``（手动作废/恢复的另一半）。"""

        number = _as_int(memory_id)
        if number <= 0 or not getattr(self, "_memory_has_status", False):
            return False
        stamp = float(when if when is not None else time.time())
        with self._lock:
            cursor = self._execute(
                "UPDATE node_memory SET status = 'active', superseded_by = '', "
                "superseded_at = 0 WHERE id = ? AND status = 'superseded'",
                (number,),
            )
            self._conn.commit()
            if int(cursor.rowcount or 0) <= 0:
                return False
            self.add_event(
                session_id="",
                event_type="memory_restored",
                detail={"id": number, "at": stamp},
            )
        return True

    def supersede_links(self) -> dict[str, str]:
        """``{旧记忆 id: 覆盖它的新 id}`` 的现成链（整理判环用）。"""

        if not getattr(self, "_memory_has_status", False):
            return {}
        rows = self._query(
            "SELECT id, superseded_by FROM node_memory "
            "WHERE status = 'superseded' AND superseded_by != ''"
        )
        return {
            str(row["id"]): str(row["superseded_by"])
            for row in rows
            if str(row["superseded_by"] or "")
        }

    def memory_supersede_candidates(
        self,
        *,
        session_ids: list[str] | None = None,
        persona_id: str | None = None,
        memory_type: str = "",
        since: float = 0.0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """写入时判覆盖用的候选：同会话（组）、同人格、同类型、最近 ``since`` 之后、仍 active。"""

        if not getattr(self, "_memory_has_status", False):
            return []
        sql = "SELECT * FROM node_memory WHERE status = 'active'"
        params: list[Any] = []
        if session_ids:
            marks = ",".join("?" for _ in session_ids)
            sql += f" AND session_id IN ({marks})"
            params.extend([str(item) for item in session_ids])
        if persona_id is not None:
            sql += " AND persona_id = ?"
            params.append(str(persona_id))
        if memory_type:
            sql += " AND type = ?"
            params.append(str(memory_type))
        if since > 0:
            sql += " AND created_at >= ?"
            params.append(float(since))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [self._memory_row(row) for row in self._query(sql, params)]

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

    def find_action_memory(
        self,
        *,
        session_id: str,
        persona_id: str,
        node_id: str,
        content: str,
        since: float,
        scope: str | None = None,
    ) -> dict[str, Any] | None:
        """取「同会话 + 同节点 + 同样一句动作」最近的一条（``created_at >= since``）。

        动作记忆（``在这里{地点}``）每个 tick 都会写一条，同一个人在同一处反复发呆
        会被灌成几十行。引擎拿它做合并：命中就刷新这一行，不再新增。
        """

        sql = (
            "SELECT * FROM node_memory WHERE session_id = ? AND persona_id = ? "
            "AND node_id = ? AND content = ? AND created_at >= ?"
        )
        params: list[Any] = [
            str(session_id),
            str(persona_id or ""),
            str(node_id or ""),
            str(content),
            float(since),
        ]
        if scope is not None:
            sql += " AND scope = ?"
            params.append(str(scope))
        sql += " ORDER BY created_at DESC LIMIT 1"
        rows = self._query(sql, params)
        if not rows:
            return None
        return self._memory_row(rows[0])

    def touch_action_memory(self, memory_id: int, *, when: float | None = None) -> None:
        """刷新一条动作记忆的时间戳并把 ``occurrence`` +1（不新增行、不提权重）。"""

        stamp = float(when if when is not None else time.time())
        self._execute(
            "UPDATE node_memory SET created_at = ?, occurrence = occurrence + 1 WHERE id = ?",
            (stamp, int(memory_id)),
        )

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
            session_id=session_id,
            persona_id=persona_id,
            node_id=node_id,
            limit=100000,
            # 「彻底忘掉」必须连被覆盖的旧说法一起删：否则 /bot forget me 之后，
            # 那些 status='superseded' 的行还留在库里，她一翻旧账又想起来了。
            include_superseded=True,
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
        self,
        *,
        session_id: str,
        half_life_days: float = 30.0,
        floor: float = 0.1,
        idle_days: float = 30.0,
        now: float | None = None,
        limit: int = 5000,
    ) -> int:
        """时间衰减：**只降权重，绝不删除**。

        锚点是 ``last_recalled``（从没被召回过就用 ``created_at``）：距现在不超过
        ``idle_days`` 的记忆一动不动——最近用过的东西不该被罚；超过之后才按
        ``half_life_days`` 半衰，权重**最低停在 floor**。

        和旧实现的两点不同（旧实现低于 floor 且没被召回就直接 DELETE）：
        删除是不可逆的，而她"想不起来"和"这条不在了"是两件事——真删只由容量闸
        （:meth:`core.memory.MemoryEngine._enforce_limit`）按明确的淘汰顺序做，
        衰减只负责把权重压下去。返回权重被改写的条数。
        """

        stamp = float(now if now is not None else time.time())
        rows = self.query_memories(session_id=session_id, limit=limit)
        updated = 0
        for row in rows:
            if bool(row.get("pinned")):
                continue
            anchor = float(row.get("last_recalled") or 0.0) or float(
                row.get("created_at") or 0.0
            )
            idle = max(0.0, (stamp - anchor) / 86400.0)
            if idle <= max(0.1, float(idle_days)):
                continue
            factor = 0.5 ** ((idle - float(idle_days)) / max(0.1, float(half_life_days)))
            new_weight = max(float(floor), float(row["weight"]) * factor)
            if new_weight >= float(row["weight"]):
                continue
            if abs(new_weight - float(row["weight"])) <= 1e-4:
                continue
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

    def profile_rows_for_user(self, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """同一个 QQ 号在**各个群**里的画像行（跨群对名片用，用户 m14014）。"""

        rows = self._query(
            "SELECT * FROM user_profile WHERE user_id = ? "
            "ORDER BY last_seen_at DESC LIMIT ?",
            (str(user_id), max(1, int(limit))),
        )
        return [_profile_row(row) for row in rows]

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
        """把一个人整个忘掉（画像 + 事实 + 关系 + 好感日志 + 信赖观测）。"""

        removed = 0
        for table in ("user_profile", "user_fact", "user_bond", "affinity_log"):
            cursor = self._execute(
                f"DELETE FROM {table} WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            removed += int(cursor.rowcount or 0)
        # 信赖观测的主键列叫 person_id（不是 user_id），单独清一遍：
        # 忘了这个人还留着他的结算凭据，下次加回来就再也涨不起来了。
        cursor = self._execute(
            "DELETE FROM trust_observations WHERE group_id = ? AND person_id = ?",
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
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """取关于这个人的事实。

        默认**只给还算数的**（``active`` / ``candidate``）：被新说法覆盖掉的旧事实
        （``status='superseded'``）不再进提示词，否则"明天要去体检"和"体检完了"
        会一起写进画像。``include_superseded=True`` 时连旧说法一起给（渲染时标
        「（旧说法）」），她提到「以前 / 之前」用得上。
        """

        wanted = [str(item) for item in (statuses or ["active", "candidate"]) if str(item)]
        if include_superseded and "superseded" not in wanted:
            wanted.append("superseded")
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

    def supersede_user_fact(
        self,
        *,
        fact_id: int,
        by: object = "",
        when: float | None = None,
        why: str = "",
        via: str = "rule",
        group_id: str = "",
        user_id: str = "",
    ) -> bool:
        """把一条通讯录事实标成 ``superseded``（旧说法退位，**不删行**）。

        ``group_id`` / ``user_id`` 只用于事件记账；不传就从行里读。
        """

        number = _as_int(fact_id)
        if number <= 0 or not getattr(self, "_fact_has_status", False):
            return False
        stamp = float(when if when is not None else time.time())
        with self._lock:
            row = self._query(
                "SELECT group_id, user_id, text, status FROM user_fact WHERE id = ?",
                (number,),
            )
            if not row:
                return False
            if str(row[0]["status"] or "active") != "active":
                return False  # 已经作废 / 曾经 / 候选：不重复记账
            self._conn.execute(
                "UPDATE user_fact SET status = 'superseded', superseded_by = ?, "
                "superseded_at = ?, updated_at = ? WHERE id = ? AND status = 'active'",
                (str(by or ""), stamp, time.time(), number),
            )
            self._conn.commit()
            self.add_event(
                session_id="",
                event_type="memory_superseded",
                detail={
                    "old": number,
                    "by": _as_int(by),
                    "why": str(why or ""),
                    "via": str(via or "rule"),
                    "kind": "user_fact",
                    "group_id": str(group_id or row[0]["group_id"] or ""),
                    "user_id": str(user_id or row[0]["user_id"] or ""),
                    "text": str(row[0]["text"] or "")[:120],
                },
            )
        return True

    def restore_user_fact(self, *, fact_id: int, when: float | None = None) -> bool:
        """把一条被覆盖的通讯录事实恢复成 ``active``。"""

        number = _as_int(fact_id)
        if number <= 0 or not getattr(self, "_fact_has_status", False):
            return False
        stamp = float(when if when is not None else time.time())
        with self._lock:
            cursor = self._execute(
                "UPDATE user_fact SET status = 'active', superseded_by = '', "
                "superseded_at = 0, last_confirmed_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'superseded'",
                (stamp, stamp, number),
            )
            self._conn.commit()
            if int(cursor.rowcount or 0) <= 0:
                return False
            self.add_event(
                session_id="",
                event_type="memory_restored",
                detail={"id": number, "at": stamp, "kind": "user_fact"},
            )
        return True

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

    # ---------------- 信赖值观测（T15-S3） ----------------

    def add_trust_observation(
        self,
        *,
        group_id: str,
        person_id: str,
        delta: float,
        kind: str = "advice",
        value_after: float = 0.0,
        evidence: str = "",
        source_kind: str = "",
        source_id: str = "",
        reason: str = "",
        applied: bool = True,
        at: float | None = None,
    ) -> bool:
        """记一条信赖观测；**同一个来源只收一次**。

        返回 ``True`` = 这次真的记下了（调用方可以去改信赖值）；
        ``False`` = 这个 ``(source_kind, source_id, person_id)`` 已经结算过，
        调用方必须**原地停手**，不许再加分——幂等全靠这一条 SQL 的唯一约束。
        """

        cursor = self._execute(
            "INSERT INTO trust_observations (group_id, person_id, kind, delta, value_after, "
            "evidence, source_kind, source_id, reason, applied, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_kind, source_id, person_id) DO NOTHING",
            (
                str(group_id or ""),
                str(person_id or ""),
                str(kind or "advice"),
                float(delta),
                float(value_after or 0.0),
                str(evidence or ""),
                str(source_kind or ""),
                str(source_id or ""),
                str(reason or ""),
                int(bool(applied)),
                float(at if at is not None else time.time()),
            ),
        )
        return bool(cursor.rowcount)

    def list_trust_observations(
        self, *, group_id: str, person_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """这个人最近的信赖观测（倒序）：调试与"为什么涨/跌"用。"""

        rows = self._query(
            "SELECT * FROM trust_observations WHERE group_id = ? AND person_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (str(group_id or ""), str(person_id or ""), max(1, int(limit))),
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
        tool_call_id: str = "",
        origin_session: str = "",
    ) -> int:
        """写一条事件。

        ``tool_call_id`` 是给工具调用回执用的串联列（E1 加）：**默认空串**，
        所以老的调用方逐字节不变；只有工具那条权威边界会填。

        ``origin_session`` 是"这条事件由哪个会话（umo）触发"的来源列：``session_id``
        记的是状态键（同组会话共用主会话那一行），所以只看它无法追溯来源。
        同样是**默认空串**，老的调用方与老数据逐字节不变。
        """

        cursor = self._execute(
            "INSERT INTO event_log (session_id, persona_id, world_time, event_type, detail, "
            "created_at, tool_call_id, origin_session) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                persona_id,
                int(world_time),
                event_type,
                json.dumps(detail or {}, ensure_ascii=False),
                time.time(),
                str(tool_call_id or ""),
                str(origin_session or ""),
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

    # ---------------- 转述失败标记（用户 2026-10-23，问题2） ----------------
    #
    # "这张图看不成"必须落库、带 TTL：文件已被清理 / 超过 10MB / 不是图片这几件
    # 几小时内不会变好，而进程内那份负缓存只有 60 秒——不落库就会每轮重试、
    # 每轮把"没读出内容"再注记一遍。

    def record_image_caption_failures(
        self, fingerprints: list[str], reason: str, ttl_seconds: float
    ) -> int:
        now = time.time()
        keys = [str(item) for item in (fingerprints or []) if str(item)]
        for key in keys:
            self._execute(
                "INSERT INTO image_caption_failure (fingerprint, reason, failed_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(fingerprint) DO UPDATE SET reason = excluded.reason,"
                " failed_at = excluded.failed_at",
                (key, str(reason or ""), now),
            )
        # 顺手清掉过期的：这张表只用来挡 TTL 内的重试，不必留历史
        if float(ttl_seconds or 0) > 0:
            self._execute(
                "DELETE FROM image_caption_failure WHERE failed_at < ?",
                (now - float(ttl_seconds),),
            )
        return len(keys)

    def recent_image_caption_failures(
        self, fingerprints: list[str], ttl_seconds: float
    ) -> list[str]:
        keys = [str(item) for item in (fingerprints or []) if str(item)]
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        floor = time.time() - max(0.0, float(ttl_seconds or 0))
        rows = self._query(
            f"SELECT fingerprint FROM image_caption_failure"
            f" WHERE failed_at >= ? AND fingerprint IN ({placeholders})",
            (floor, *keys),
        )
        return [str(row["fingerprint"]) for row in rows or []]

    def clear_image_caption_failures(self, fingerprints: list[str]) -> int:
        keys = [str(item) for item in (fingerprints or []) if str(item)]
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        cursor = self._execute(
            f"DELETE FROM image_caption_failure WHERE fingerprint IN ({placeholders})",
            tuple(keys),
        )
        return int(cursor.rowcount or 0)

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

    def trim_state_history_before(self, *, before: float) -> int:
        """按**时间**裁 ``state_history``：删 ``at < before`` 的行，返回删了几行。

        和上面的 ``trim_state_history``（按条数）互补：条数上限管不住"很久以前、
        但总数还没到上限"的那部分，所以 2026-10 事故复盘后补了时间维度。
        """

        cursor = self._execute("DELETE FROM state_history WHERE at < ?", (float(before),))
        return int(cursor.rowcount or 0)

    def trim_events_before(self, *, before: float) -> int:
        """按**时间**裁 ``event_log``：删 ``created_at < before`` 的行。"""

        cursor = self._execute("DELETE FROM event_log WHERE created_at < ?", (float(before),))
        return int(cursor.rowcount or 0)

    def count_decision_batches(self) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM decision_batches")
        return int(rows[0]["n"]) if rows else 0

    def trim_decision_batches(self, *, before: float) -> int:
        """删掉 ``created_at < before`` 的决策批次，返回删掉的行数（0 = 没啥可删）。

        为什么盯着它：本机 state.db 里 ``decision_batches`` 占 40.7 MB（整库约 40%），
        119,318 行里 119,158 行是 ``pending``、其中 119,154 行既没有出站分段也没有
        决策输入——就是"建了批次再没下文"的孤儿。一天清一次、只留 14 天，幂等。

        连带清 ``decision_inputs``（纯记账行，指向被删的批次，留着就是永远取不到的
        死行，只会把库撑大）；**不动** ``outbox_segments``——那里是"她本来要说的话"，
        悄悄删掉就等于她又"不理人"了，正是这次要修的那类症状。

        ``created_at`` 上没有索引，所以这是全表扫：只在维护时一天跑一次，
        不在消息路径上（119k 行本机实测约几十毫秒）。
        """

        cutoff = float(before)
        self._execute(
            "DELETE FROM decision_inputs WHERE decision_id IN ("
            " SELECT decision_id FROM decision_batches WHERE created_at < ?)",
            (cutoff,),
        )
        cursor = self._execute(
            "DELETE FROM decision_batches WHERE created_at < ?", (cutoff,)
        )
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------
    # T0-b：孤儿决策批次回收
    # ------------------------------------------------------------------
    # 与上面的 ``trim_decision_batches`` 是**两件事**，别混：
    # - ``trim_decision_batches`` 是纯时间谓词（``created_at < before``），另一个批次
    #   在用，语义不动；它既删孤儿，也删"有出站分段但已经很老"的批次。
    # - 这里只删"建了批次再没下文"的孤儿：状态非终态、超过 1 小时宽限、
    #   **没有** ``outbox_segments`` 行、**没有** ``decision_inputs`` 行，四条同时满足。
    #   少了后两条就会把她本来要说的话（分段）连坐删掉，正是这次要修的那类症状。

    ORPHAN_DECISION_GRACE = 3600.0
    """孤儿批次的宽限期（秒）：1 小时内建的批次可能正在处理，绝不删。"""

    def reap_orphan_decisions(self, *, now: float = 0.0, limit: int = 5000) -> int:
        """删掉"建了批次再没下文"的孤儿决策批次，返回删掉的行数。

        判定四条必须**同时**满足，全部在 SQL 里精确表达，不在 Python 里二次过滤：

        1. 状态非终态：不在 ``decided_silent`` / ``sent`` / ``cancelled`` 里
           （终态集合就是 ``_PROCESSED_STATUSES``）；
        2. ``created_at < now - 3600``：1 小时宽限，避免删掉正在进行的批次；
        3. 该批次**没有** ``outbox_segments`` 行——那里是她要说的话，一条都不能连坐；
        4. 该批次**没有** ``decision_inputs`` 行——输入账指向已删批次就是永远取不到的死行。

        ``limit`` 是**每批**删除的行数，不是总量：删完一批提交一次（``_execute``
        自带 commit），再删下一批，直到删空。这样 12 万行的积压不会变成一次长事务
        把库锁死。``now`` 不传（``0``）就用墙上时间；不是数字时返回 0，什么都不删。

        ``created_at`` 上的 ``idx_decision_batches_created(created_at, status)``
        由 ``_migrate_topics_storage`` 建；没有它这就是全表扫（只在维护里跑，能接受，
        但有了它 12 万行也只扫候选那一段）。
        """

        try:
            stamp = float(now)
        except (TypeError, ValueError):
            return 0
        if not math.isfinite(stamp):
            return 0
        if stamp <= 0:
            stamp = time.time()
        try:
            batch = int(limit)
        except (TypeError, ValueError, OverflowError):
            return 0
        if batch <= 0:
            return 0
        cutoff = stamp - self.ORPHAN_DECISION_GRACE
        # 终态用集合拼参数，不把状态名抄第二遍：抄错了就会删到终态行。
        terminal = tuple(sorted(_PROCESSED_STATUSES))
        marks = ",".join("?" for _ in terminal)
        sql = (
            "DELETE FROM decision_batches WHERE decision_id IN ("
            " SELECT b.decision_id FROM decision_batches b"
            f" WHERE b.status NOT IN ({marks})"
            " AND b.created_at < ?"
            " AND NOT EXISTS (SELECT 1 FROM outbox_segments s"
            "  WHERE s.decision_id = b.decision_id)"
            " AND NOT EXISTS (SELECT 1 FROM decision_inputs i"
            "  WHERE i.decision_id = b.decision_id)"
            " ORDER BY b.created_at LIMIT ?)"
        )
        total = 0
        while True:
            cursor = self._execute(sql, (*terminal, cutoff, batch))
            removed = int(cursor.rowcount or 0)
            total += removed
            if removed < batch:
                return total

    def query_events(
        self,
        *,
        session_id: str | None = None,
        limit: int = 100,
        event_type: str | None = None,
        keyword: str | None = None,
        before_id: int | None = None,
        tool_call_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM event_log WHERE 1 = 1"
        params: list[Any] = []
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        if tool_call_id:
            # 按工具调用 id 取那一条（或者它那一串）：E1 的串联列
            sql += " AND tool_call_id = ?"
            params.append(str(tool_call_id))
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

    def count_events_since(
        self,
        *,
        event_type: str,
        since: float,
        session_id: str | None = None,
    ) -> int:
        """某类事件在 ``since``（epoch 秒）之后有几条（用户 m30450：主动私聊的日限额）。

        用 ``created_at``（墙上时间）而不是世界时钟：配额是"一天最多几次"这种
        真实时间口径，跟世界时钟压缩多少无关。
        """

        sql = "SELECT COUNT(*) AS n FROM event_log WHERE event_type = ? AND created_at >= ?"
        params: list[Any] = [str(event_type), float(since)]
        if session_id:
            sql += " AND session_id = ?"
            params.append(str(session_id))
        row = self._query(sql, params)
        try:
            return int(row[0]["n"] if row else 0)
        except (KeyError, IndexError, TypeError, ValueError):
            return 0

    # 编辑器 Agent 页的「工具调用」：只取 ``tool_receipt``（工具调用的权威回执）。    # **不收动作账本**（``action_done``）——移动、小睡、瘫在沙发上这些是她的世界生活，
    # 归「日志 / 事件」页；Agent 页只列她自己调用了哪些工具（用户 m20782 的纠正）。
    # **只读**：不写库、不加列、不动写入语义。
    def list_agent_runs(
        self,
        *,
        session_id: str | None = None,
        limit: int = 50,
        before_id: int | None = None,
        only_failed: bool = False,
    ) -> list[dict[str, Any]]:
        """取最近几次工具调用（倒序），不传 ``session_id`` 就是全部会话。

        ``only_failed`` 用 ``detail NOT LIKE '%"status": "ok"%'`` 判：与
        :meth:`query_events` 的 ``keyword`` 同一套办法（都对 ``detail`` 做 LIKE），
        代价是不给 ``event_log`` 加列；代价的另一面是"缺 status 字段的行"也会算进去，
        那正好符合"只有 ok 才算成功"（:attr:`ToolResult.ok`）的口径。
        """

        sql = (
            "SELECT id, session_id, world_time, event_type, detail, created_at, tool_call_id"
            " FROM event_log WHERE event_type = ?"
        )
        params: list[Any] = [self.TOOL_RECEIPT_EVENT_TYPE]
        if session_id:
            sql += " AND session_id = ?"
            params.append(str(session_id))
        if before_id:
            sql += " AND id < ?"
            params.append(int(before_id))
        if only_failed:
            sql += " AND detail NOT LIKE ?"
            params.append('%"status": "ok"%')
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self._query(sql, params)
        result = []
        for row in rows:
            item = dict(row)
            try:
                detail = json.loads(item.get("detail") or "{}")
            except json.JSONDecodeError:
                detail = {}
            item["detail"] = detail if isinstance(detail, dict) else {}
            result.append(item)
        return result

    def prune_events(self, keep_per_session: int = 500) -> None:
        """全局兜底裁剪：只留最新的 N 条（**所有事件类型挤在同一个池子里**）。

        这个池子不分类型：``tool_receipt`` 与普通事件一起计数。它**没有生产调用方**
        ——会话级裁剪走 :meth:`prune_session_events`（引擎每写够 100 条事件调一次），
        这条留着是给手工兜底用的；分池的配额在会话级那条上（见它的 docstring）。

        删除条数不返回：它就是"清到还剩这么多"，调用方也不需要知道删了多少。
        """

        self._execute(
            "DELETE FROM event_log WHERE id NOT IN "
            "(SELECT id FROM event_log ORDER BY id DESC LIMIT ?)",
            (int(keep_per_session) * 50,),
        )

    # ``tool_receipt`` 的**独立**配额（B1 收尾）。回执是 E1 加的权威记录：每条工具调用
    # 一行、每轮大约多两行。它要是与普通事件挤在同一个池子里计数，就等于把普通事件的
    # 保留窗口按比例削掉一截（1 轮 30 行普通事件 + 2 行回执 ⇒ 同样 1500 行装下的轮次
    # 变少），反过来普通事件多的时候也能把回执挤出去——两边都说不清「到底留了多久」。
    # 所以分池：回执拿一个**明确更小**的配额（默认普通配额的 1/10，下限 50 行）。
    # 为什么更小：回执的用处集中在「最近一段」（对上当轮的 ``tool_result`` 视图、排查刚
    # 失败的工具调用），不需要和普通事件一样长的窗口；普通事件才是日志页与经历账的正文。
    TOOL_RECEIPT_EVENT_TYPE = "tool_receipt"
    TOOL_RECEIPT_KEEP_DIVISOR = 10
    TOOL_RECEIPT_KEEP_MIN = 50

    @classmethod
    def tool_receipt_keep_for(cls, keep: int) -> int:
        """普通事件配额 ``keep`` 对应的回执配额：更小、但有**下限**（不会变成 0）。

        下限的意义：配额再小也总得留着最近几次工具调用的回执，不然对上
        ``tool_result`` 视图这一步立刻就没证据了。
        """

        return max(
            cls.TOOL_RECEIPT_KEEP_MIN,
            max(50, int(keep)) // cls.TOOL_RECEIPT_KEEP_DIVISOR,
        )

    def prune_session_events(
        self, session_id: str, keep: int = 1500, *, receipt_keep: int | None = None
    ) -> int:
        """只保留某个会话最近 N 条事件，防止日志无限增长。返回删除条数。

        **两个池子各算各的**（B1 收尾）：``keep`` 只管 ``tool_receipt`` 之外的普通事件，
        回执由 ``receipt_keep`` 单独管（默认 :meth:`tool_receipt_keep_for`）。这样每轮
        多出来的那两行回执不会削掉普通事件的保留窗口，普通事件也挤不掉回执；这个会话的
        ``event_log`` 因此是**有界**的：至多 ``keep + receipt_keep`` 行（默认 1500+150）。

        裁剪由引擎每写够 100 条事件触发一次（``_emit_event`` 里那个计数），所以触发间隔
        内两个池子都可能短暂超出各自配额几条——超出量跟着"这 100 条里写了什么"走，是软的
        上限，不是无界增长。
        """

        if not session_id:
            return 0
        limit = max(50, int(keep))
        receipts = (
            self.tool_receipt_keep_for(limit)
            if receipt_keep is None
            else max(1, int(receipt_keep))
        )
        # 普通事件：把「回执之外」的裁到 limit 行
        removed = int(
            self._execute(
                "DELETE FROM event_log WHERE session_id = ? AND event_type <> ? AND id NOT IN "
                "(SELECT id FROM event_log WHERE session_id = ? AND event_type <> ? "
                "ORDER BY id DESC LIMIT ?)",
                (
                    session_id,
                    self.TOOL_RECEIPT_EVENT_TYPE,
                    session_id,
                    self.TOOL_RECEIPT_EVENT_TYPE,
                    limit,
                ),
            ).rowcount
            or 0
        )
        # 回执：独立配额，和普通事件互不挤占
        removed += int(
            self._execute(
                "DELETE FROM event_log WHERE session_id = ? AND event_type = ? AND id NOT IN "
                "(SELECT id FROM event_log WHERE session_id = ? AND event_type = ? "
                "ORDER BY id DESC LIMIT ?)",
                (
                    session_id,
                    self.TOOL_RECEIPT_EVENT_TYPE,
                    session_id,
                    self.TOOL_RECEIPT_EVENT_TYPE,
                    receipts,
                ),
            ).rowcount
            or 0
        )
        return removed

    def prune_orphan_events(self, *, before: float, limit: int = 1000) -> int:
        """见模块级 :func:`prune_orphan_events`：清掉**没有会话归属**的旧事件行。

        B1 收尾第 3 条的后半截：`prune_session_events` 是按会话裁的（`session_id` 为空
        直接返回 0），所以"页面级操作"（管理员上传表情包失败那条 `media_cache`）写下的
        无会话行**永远不会被任何一条路径清掉**。这里给它一个按时间的出口。
        """

        with self._lock:
            self._ensure_open()
            return prune_orphan_events(self._conn, before=before, limit=limit)

    # ---------------- 增量回收空闲页（见 core/storage_vacuum.py） ----------------

    def ensure_incremental_vacuum(
        self, *, data_dir: str = "", disk_usage: Any = None
    ) -> dict[str, Any]:
        """一次性把库切到 ``auto_vacuum=INCREMENTAL``（备份 + 一次完整 ``VACUUM``）。

        失败只返回 ``{"status": "failed", ...}``，不抛——启动路径要 fail-soft。
        """

        from .storage_vacuum import ensure_incremental_vacuum

        with self._lock:
            self._ensure_open()
            return ensure_incremental_vacuum(
                self._conn,
                data_dir=data_dir or str(Path(self.path).parent),
                disk_usage=disk_usage,
            )

    def incremental_vacuum(
        self,
        *,
        max_pages: int | None = None,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        """搬走最多 ``max_pages`` 个空闲页（有 deadline，可中断；返回统计不抛）。"""

        from .storage_vacuum import (
            DEFAULT_DEADLINE_SECONDS,
            DEFAULT_MAX_PAGES,
            incremental_vacuum,
        )

        with self._lock:
            self._ensure_open()
            return incremental_vacuum(
                self._conn,
                max_pages=DEFAULT_MAX_PAGES if max_pages is None else int(max_pages),
                deadline_seconds=(
                    DEFAULT_DEADLINE_SECONDS
                    if deadline_seconds is None
                    else float(deadline_seconds)
                ),
            )

    def freelist_snapshot(self) -> dict[str, int]:
        """``{"freelist", "page_size", "db_bytes"}``：``/vw status`` 用，只读、不抛。"""

        from .storage_vacuum import freelist_snapshot

        with self._lock:
            self._ensure_open()
            return freelist_snapshot(self._conn)

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

    def schedule_fire_stats(
        self, *, session_id: str, schedule_id: str, day_key: str
    ) -> dict[str, Any]:
        """这条日程「今天」触发了几次、上一次是什么时候（限流闸门用）。

        频率从"一天一条"变成"一天几百条"之后，必须有一道刹车：
        分钟级的日程在 tick 被拖慢、或者她把某件事拖长的时候不该连炸一片。
        """

        rows = self._query(
            "SELECT COUNT(*) AS n, MAX(fired_at) AS last FROM schedule_fire "
            "WHERE session_id = ? AND schedule_id = ? AND day_key = ?",
            (session_id, schedule_id, str(day_key)),
        )
        row = rows[0] if rows else {}
        return {
            "count": int(row.get("n") or 0),
            "last": float(row.get("last") or 0.0),
        }

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

    def kv_put_if_absent(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        """Atomic first-write claim; never overwrite or silently repair old facts."""
        if not isinstance(key, str) or not key.strip():
            raise ValueError('nonempty KV claim key required')
        payload = _strict_json_object(value)
        with self.transaction() as conn:
            cursor = conn.execute('INSERT OR IGNORE INTO kv(key,value,updated_at) VALUES(?,?,?)',
                                  (key, payload, time.time()))
            row = conn.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
            if row is None:
                raise RuntimeError('KV claim persistence failed')
            winner = json.loads(row['value'])
            _strict_json_object(winner)
            return {'created': cursor.rowcount == 1, 'value': winner}

    def kv_delete(self, key: str) -> None:
        self._execute("DELETE FROM kv WHERE key = ?", (key,))

    # ---------------- 输入事件（阶段A1 追加） ----------------
    #
    # 这几个只是把连接接上模块级同名函数（见文件末尾分区），方便
    # ``AsyncDatabase.call("record_source_event", event)`` 这样从事件循环调用；
    # 真正的读写逻辑只有一份，在下面。

    def record_source_event(self, event: Any) -> bool:
        """见模块级 :func:`record_source_event`：True = 这次真的插进去了。"""

        with self._lock:
            self._ensure_open()
            return record_source_event(self._conn, event)

    def get_source_event(self, event_id: str) -> dict[str, Any] | None:
        """见模块级 :func:`get_source_event`。"""

        with self._lock:
            self._ensure_open()
            return get_source_event(self._conn, event_id)

    def set_source_event_visibility(self, event: Any, visibility: Any) -> bool:
        """Merge and persist authority atomically across database connections."""
        with self.transaction() as conn:
            return set_source_event_visibility(conn, event, visibility)

    def source_event_visibility(self, event_id: str) -> dict[str, Any]:
        """见模块级 :func:`source_event_visibility`（``{}`` = 查不到许可）。"""

        with self._lock:
            self._ensure_open()
            return source_event_visibility(self._conn, event_id)

    def source_events_visibility(self, event_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """见模块级 :func:`source_events_visibility`（按候选 ID 批量精确查）。"""

        with self._lock:
            self._ensure_open()
            return source_events_visibility(self._conn, event_ids)

    def retract_source_event(self, event_id: str, *, now: float = 0.0) -> list[str]:
        """见模块级 :func:`retract_source_event`（来源撤回 + 派生谈资连坐）。"""

        with self._lock:
            self._ensure_open()
            return retract_source_event(self._conn, event_id, now=now)

    def list_source_events(
        self, *, scope_id: str = "", since: float = 0.0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`list_source_events`。"""

        with self._lock:
            self._ensure_open()
            return list_source_events(
                self._conn, scope_id=scope_id, since=since, limit=limit
            )

    def list_source_events_window(
        self, *, after_seq: int = 0, to_seq: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`list_source_events_window`（阶段F1 的登记序号窗口）。"""

        with self._lock:
            self._ensure_open()
            return list_source_events_window(
                self._conn, after_seq=after_seq, to_seq=to_seq, limit=limit
            )

    def latest_source_event_seq(self) -> int:
        """见模块级 :func:`latest_source_event_seq`。"""

        with self._lock:
            self._ensure_open()
            return latest_source_event_seq(self._conn)

    def count_source_events(self, *, scope_id: str = "") -> int:
        """见模块级 :func:`count_source_events`。"""

        with self._lock:
            self._ensure_open()
            return count_source_events(self._conn, scope_id=scope_id)

    def record_operations(
        self,
        keys: Iterable[str],
        *,
        kind: str = "",
        session_id: str = "",
        now: float = 0.0,
    ) -> set[str]:
        """见模块级 :func:`record_operations`（阶段F3 的幂等账）。"""

        with self._lock:
            self._ensure_open()
            return record_operations(
                self._conn, keys, kind=kind, session_id=session_id, now=now
            )

    def has_operation(self, key: str) -> bool:
        """这个幂等键是不是已经被用过了（**只读**，不抢占）。"""

        with self._lock:
            self._ensure_open()
            return has_operation(self._conn, key)

    def list_applied_operations(
        self, *, session_id: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`list_applied_operations`。"""

        with self._lock:
            self._ensure_open()
            return list_applied_operations(self._conn, session_id=session_id, limit=limit)

    def attach_operation(self, event_id: str, operation_id: str) -> bool:
        """见模块级 :func:`attach_operation`：False = 该 operation_id 已被别的占用。"""

        with self._lock:
            self._ensure_open()
            return attach_operation(self._conn, event_id, operation_id)

    def find_event_by_operation(self, operation_id: str) -> dict[str, Any] | None:
        """见模块级 :func:`find_event_by_operation`。"""

        with self._lock:
            self._ensure_open()
            return find_event_by_operation(self._conn, operation_id)

    def get_schema_version(self) -> int:
        """见模块级 :func:`get_schema_version`（缺省 0）。"""

        with self._lock:
            self._ensure_open()
            return get_schema_version(self._conn)

    # ---------------- 分享登记（阶段A2 追加） ----------------
    #
    # 和上面一样：连接接上模块级同名函数，方便
    # ``AsyncDatabase.call("record_share", share)`` 从事件循环调用。

    def record_share(self, share: Any) -> bool:
        """见模块级 :func:`record_share`：True = 这次真的插进去了（重复提交返回 False）。"""

        with self._lock:
            self._ensure_open()
            return record_share(self._conn, share)

    def get_share(self, share_id: str) -> dict[str, Any] | None:
        """见模块级 :func:`get_share`。"""

        with self._lock:
            self._ensure_open()
            return get_share(self._conn, share_id)

    def list_shares(
        self,
        *,
        scope_id: str = "",
        source_id: str = "",
        session_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`list_shares`。"""

        with self._lock:
            self._ensure_open()
            return list_shares(
                self._conn,
                scope_id=scope_id,
                source_id=source_id,
                session_id=session_id,
                limit=limit,
            )

    def count_shares(
        self, *, scope_id: str = "", source_id: str = "", session_id: str = ""
    ) -> int:
        """见模块级 :func:`count_shares`。"""

        with self._lock:
            self._ensure_open()
            return count_shares(
                self._conn,
                scope_id=scope_id,
                source_id=source_id,
                session_id=session_id,
            )

    def shares_for_source(
        self, source_id: str, *, revision: int | None = None
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`shares_for_source`（同一份信息的 revision 是否已介绍过）。"""

        with self._lock:
            self._ensure_open()
            return shares_for_source(self._conn, source_id, revision=revision)

    def mark_share_sent(
        self,
        share_id: str,
        *,
        status: str = "",
        platform_message_ids: Any = (),
        sent_at: float | None = None,
    ) -> bool:
        """见模块级 :func:`mark_share_sent`：True = 真的改到了行。"""

        with self._lock:
            self._ensure_open()
            return mark_share_sent(
                self._conn,
                share_id,
                status=status,
                platform_message_ids=platform_message_ids,
                sent_at=sent_at,
            )

    # ---------------- 谈资 / 续说租约（阶段C 追加） ----------------
    #
    # 和前两个分区一样：连接接上模块级同名函数，方便
    # ``AsyncDatabase.call("record_topic", topic)`` 从事件循环调用。真正的读写
    # 逻辑只有一份，在文件末尾的「阶段C」分区里。

    def record_topic(self, topic: Any) -> bool:
        """见模块级 :func:`record_topic`：True = 这次真的插进去了。"""

        with self._lock:
            self._ensure_open()
            return record_topic(self._conn, topic)

    def get_topic(self, topic_id: str) -> dict[str, Any] | None:
        """见模块级 :func:`get_topic`。"""

        with self._lock:
            self._ensure_open()
            return get_topic(self._conn, topic_id)

    def list_topics(
        self,
        *,
        session_id: str = "",
        scope_id: str = "",
        status: str = "",
        cursor: str = "",
        now: float = 0.0,
        limit: int = 100,
        source_kind: str = "",
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`list_topics`（默认不返回软删除的谈资，支持游标下推）。"""

        with self._lock:
            self._ensure_open()
            return list_topics(
                self._conn,
                session_id=session_id,
                scope_id=scope_id,
                status=status,
                cursor=cursor,
                now=now,
                limit=limit,
                source_kind=source_kind,
            )

    def list_visible_topics(
        self,
        *,
        session_id: str = "",
        scope_id: str = "",
        status: str = "",
        now: float = 0.0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`list_visible_topics`（过期的都不返回，给候选池用）。"""

        with self._lock:
            self._ensure_open()
            return list_visible_topics(
                self._conn,
                session_id=session_id,
                scope_id=scope_id,
                status=status,
                now=now,
                limit=limit,
            )

    def count_topics(
        self, *, session_id: str = "", scope_id: str = "", status: str = "",
        source_kind: str = "", now: float = 0.0
    ) -> int:
        """见模块级 :func:`count_topics`。"""

        with self._lock:
            self._ensure_open()
            return count_topics(
                self._conn, session_id=session_id, scope_id=scope_id, status=status,
                source_kind=source_kind, now=now
            )

    def update_topic(self, topic: Any, *, expected_version: int | None = None) -> bool:
        """见模块级 :func:`update_topic`：False = 版本冲突或行不存在，库里的行没动。"""

        with self._lock:
            self._ensure_open()
            return update_topic(self._conn, topic, expected_version=expected_version)

    def expire_topics_due(self, *, now: float) -> int:
        """见模块级 :func:`expire_topics_due`：返回这次转 expired 的条数。"""

        with self._lock:
            self._ensure_open()
            return expire_topics_due(self._conn, now=now)

    def delete_topic(self, topic_id: str, *, soft: bool = True) -> bool:
        """见模块级 :func:`delete_topic`：软删除默认查不到，``soft=False`` 才真删。"""

        with self._lock:
            self._ensure_open()
            return delete_topic(self._conn, topic_id, soft=soft)

    def record_claim(self, claim: Any) -> bool:
        """见模块级 :func:`record_claim`：False = 这个 key 已经有活跃 claim（抢锁失败）。"""

        with self._lock:
            self._ensure_open()
            return record_claim(self._conn, claim)

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        """见模块级 :func:`get_claim`。"""

        with self._lock:
            self._ensure_open()
            return get_claim(self._conn, claim_id)

    def list_claims(
        self,
        *,
        source_id: str = "",
        session_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`list_claims`。"""

        with self._lock:
            self._ensure_open()
            return list_claims(
                self._conn,
                source_id=source_id,
                session_id=session_id,
                status=status,
                limit=limit,
            )

    def update_claim(self, claim: Any) -> bool:
        """见模块级 :func:`update_claim`：False = 行不存在或改完会撞上活跃唯一索引。"""

        with self._lock:
            self._ensure_open()
            return update_claim(self._conn, claim)

    def expire_claims(self, *, now: float, ttl: float | None = None) -> int:
        """见模块级 :func:`expire_claims`（``now`` 必传，库里不自己取时钟）。"""

        with self._lock:
            self._ensure_open()
            return expire_claims(self._conn, now=now, ttl=ttl)

    # ---------------- 谈资向量（阶段B0 追加） ----------------
    #
    # 和前面几个分区一样：连接接上模块级同名函数，方便
    # ``AsyncDatabase.call("upsert_topic_vector", ...)`` 从事件循环调用；真正的读写
    # 逻辑只有一份，在文件末尾的「阶段B0」分区里。**这些方法今天没有任何生产
    # 调用点**（B0 只立表与接口，B1 才接检索），加进来是为了 B1 不用再改 db.py 的
    # 接口层。

    def upsert_topic_vector(self, **kwargs: Any) -> bool:
        """见模块级 :func:`upsert_topic_vector`：True = 这次真的写进去了。"""

        with self._lock:
            self._ensure_open()
            return upsert_topic_vector(self._conn, **kwargs)

    def get_topic_vector(
        self,
        topic_id: str,
        information_revision: int,
        provider_id: str,
        space_fingerprint: str,
        recipe_version: str,
    ) -> dict[str, Any] | None:
        """见模块级 :func:`get_topic_vector`（``vector`` 是原始 BLOB）。

        参数顺序与模块级函数、``TOPIC_VECTOR_KEY_COLUMNS`` 一致：
        ``space_fingerprint`` 在 ``recipe_version`` **前面**。
        """

        with self._lock:
            self._ensure_open()
            return get_topic_vector(
                self._conn,
                topic_id,
                information_revision,
                provider_id,
                space_fingerprint,
                recipe_version,
            )

    def delete_topic_vectors(self, topic_id: str) -> int:
        """见模块级 :func:`delete_topic_vectors`：返回删掉的条数。"""

        with self._lock:
            self._ensure_open()
            return delete_topic_vectors(self._conn, topic_id)

    def count_topic_vectors(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_topic_vectors`（空表 = 0）。"""

        with self._lock:
            self._ensure_open()
            return count_topic_vectors(self._conn, **kwargs)

    def list_topic_vectors(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_topic_vectors`（B0 只给接口，B1 才用）。"""

        with self._lock:
            self._ensure_open()
            return list_topic_vectors(self._conn, **kwargs)

    # ---- 阶段B1 追加：谈资索引队列（topic_index_queue）---------------------
    #
    # 和上面 B0 那组一个规矩：连接接上模块级同名函数，于是
    # ``AsyncDatabase.call("enqueue_topic_index", ...)`` 就能从事件循环里调；真正的
    # 读写逻辑只有一份，在文件末尾的「阶段B1」分区里。**唯一的生产调用方是
    # ``core/embedding_index.py`` 的 ``TopicIndexQueue``。**

    def enqueue_topic_index(self, **kwargs: Any) -> bool:
        """见模块级 :func:`enqueue_topic_index`：True = 这次真的新入队一行。"""

        with self._lock:
            self._ensure_open()
            return enqueue_topic_index(self._conn, **kwargs)

    def claim_topic_index_due(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`claim_topic_index_due`（只挑选，不改状态、不锁行）。"""

        with self._lock:
            self._ensure_open()
            return claim_topic_index_due(self._conn, **kwargs)

    def mark_topic_index(self, row_id: int, **kwargs: Any) -> bool:
        """见模块级 :func:`mark_topic_index`（done / failed / stale / pending）。"""

        with self._lock:
            self._ensure_open()
            return mark_topic_index(self._conn, row_id, **kwargs)

    def count_pending_topic_index(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_pending_topic_index`（空表 = 0）。"""

        with self._lock:
            self._ensure_open()
            return count_pending_topic_index(self._conn, **kwargs)

    def clear_topic_index(self, **kwargs: Any) -> int:
        """见模块级 :func:`clear_topic_index`：返回清掉的队列行数。"""

        with self._lock:
            self._ensure_open()
            return clear_topic_index(self._conn, **kwargs)

    def list_topic_ids_for_scope(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_topic_ids_for_scope`（分页列"当前有效"的谈资）。"""

        with self._lock:
            self._ensure_open()
            return list_topic_ids_for_scope(self._conn, **kwargs)

    def list_topic_vectors_for_topics(
        self, topic_ids: Iterable[str], **kwargs: Any
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`list_topic_vectors_for_topics`（``vector`` 是原始 BLOB）。"""

        with self._lock:
            self._ensure_open()
            return list_topic_vectors_for_topics(self._conn, topic_ids, **kwargs)

    def delete_topic_vectors_for_scope(self, **kwargs: Any) -> int:
        """见模块级 :func:`delete_topic_vectors_for_scope`：返回删掉的向量行数。"""

        with self._lock:
            self._ensure_open()
            return delete_topic_vectors_for_scope(self._conn, **kwargs)

    def upsert_memory_vector(self, **kwargs: Any) -> bool:
        """见模块级 :func:`upsert_memory_vector`：True = 这次真的写进去了。"""

        with self._lock:
            self._ensure_open()
            return upsert_memory_vector(self._conn, **kwargs)

    def list_memory_vectors_for(self, memory_ids: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_memory_vectors_for`（``vector`` 是原始 BLOB）。"""

        with self._lock:
            self._ensure_open()
            return list_memory_vectors_for(self._conn, memory_ids, **kwargs)

    def ensure_memory_vectors(self) -> None:
        """把 ``memory_vectors`` 表补上（幂等）。

        为什么需要它：迁移只在 ``Database`` **打开**的时候跑一遍，而已经跑着的实例
        不会因为插件更新就重开库——于是"新加的表"在这个进程里根本不存在。预热要写
        这张表，所以先补一次（``_run_ddl`` 与迁移共用同一份 DDL）。
        """

        with self._lock:
            self._ensure_open()
            _migrate_memory_vectors(self._conn)

    def count_memory_vectors(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_memory_vectors`（空表 = 0）。"""

        with self._lock:
            self._ensure_open()
            return count_memory_vectors(self._conn, **kwargs)

    def delete_memory_vectors_for_space(self, **kwargs: Any) -> int:
        """见模块级 :func:`delete_memory_vectors_for_space`：返回删掉的向量行数。"""

        with self._lock:
            self._ensure_open()
            return delete_memory_vectors_for_space(self._conn, **kwargs)

    def delete_memory_vectors_not_in_space(self, **kwargs: Any) -> int:
        """见模块级 :func:`delete_memory_vectors_not_in_space`：返回删掉的向量行数。"""

        with self._lock:
            self._ensure_open()
            return delete_memory_vectors_not_in_space(self._conn, **kwargs)

    def topics_missing_vectors(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`topics_missing_vectors`（``rescan`` 用）。"""

        with self._lock:
            self._ensure_open()
            return topics_missing_vectors(self._conn, **kwargs)

    # ---- 阶段A4 追加：配置异步整理队列（config_index_queue）-----------------
    #
    # 和上面 B1 那组一个规矩：连接接上模块级同名函数，于是
    # ``AsyncDatabase.call("claim_config_index_due", ...)`` 就能从事件循环里调；真正的
    # 读写逻辑只有一份，在文件末尾的「阶段A4」分区里。**唯一的生产调用方是
    # ``core/config_index_queue.py`` 的 ``ConfigIndexQueue``。**

    def enqueue_config_index(self, **kwargs: Any) -> bool:
        """见模块级 :func:`enqueue_config_index`：True = 这次真的新入队一行（或复活死行）。"""

        with self._lock:
            self._ensure_open()
            return enqueue_config_index(self._conn, **kwargs)

    def save_vw_skill(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return save_vw_skill(self._conn, row)

    def get_vw_skill(self, skill_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return get_vw_skill(self._conn, skill_id)

    def list_vw_skills(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            return list_vw_skills(self._conn, **kwargs)

    def delete_vw_skill(self, skill_id: str) -> bool:
        with self._lock:
            self._ensure_open()
            return delete_vw_skill(self._conn, skill_id)

    def save_capability_experience(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return save_capability_experience(self._conn, row)

    def get_capability_experience(self, experience_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return get_capability_experience(self._conn, experience_id)

    def list_capability_experiences(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            return list_capability_experiences(self._conn, **kwargs)

    def list_unfinished_tasks(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            return list_unfinished_tasks(self._conn, **kwargs)

    def open_follow_up(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return open_follow_up(self._conn, **kwargs)

    def get_open_follow_up(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return get_open_follow_up(self._conn, **kwargs)

    def list_due_follow_ups(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            return list_due_follow_ups(self._conn, **kwargs)

    def bump_follow_up(self, follow_up_id: Any, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return bump_follow_up(self._conn, follow_up_id, **kwargs)

    def close_follow_up(self, follow_up_id: Any, **kwargs: Any) -> int:
        with self._lock:
            self._ensure_open()
            return close_follow_up(self._conn, follow_up_id, **kwargs)

    def cancel_follow_ups(self, **kwargs: Any) -> int:
        with self._lock:
            self._ensure_open()
            return cancel_follow_ups(self._conn, **kwargs)

    # ---- 「等会儿」的待办（用户 m00001）：引擎一律走 self.db.call("…", **kwargs) ----

    def add_commitment(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return add_commitment(self._conn, **kwargs)

    def due_commitments(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            return due_commitments(self._conn, **kwargs)

    def list_commitments(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            return list_commitments(self._conn, **kwargs)

    def nudge_commitment(self, commitment_id: Any, **kwargs: Any) -> int:
        with self._lock:
            self._ensure_open()
            return nudge_commitment(self._conn, commitment_id, **kwargs)

    def close_commitment(self, commitment_id: Any, **kwargs: Any) -> int:
        with self._lock:
            self._ensure_open()
            return close_commitment(self._conn, commitment_id, **kwargs)

    # ---- 临时会话（用户 m00001）：引擎一律走 self.db.call("…", **kwargs) ----

    def upsert_temp_session(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return upsert_temp_session(self._conn, **kwargs)

    def get_temp_session(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return get_temp_session(self._conn, **kwargs)

    def list_temp_sessions(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            return list_temp_sessions(self._conn, **kwargs)

    def list_due_temp_sessions(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            return list_due_temp_sessions(self._conn, **kwargs)

    def bump_temp_session_rounds(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return bump_temp_session_rounds(self._conn, **kwargs)

    def finish_temp_session(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return finish_temp_session(self._conn, **kwargs)

    def extend_temp_session(self, **kwargs: Any) -> int:
        with self._lock:
            self._ensure_open()
            return extend_temp_session(self._conn, **kwargs)

    def set_temp_session_archived(self, **kwargs: Any) -> int:
        with self._lock:
            self._ensure_open()
            return set_temp_session_archived(self._conn, **kwargs)

    def set_temp_session_origin(self, **kwargs: Any) -> int:
        with self._lock:
            self._ensure_open()
            return set_temp_session_origin(self._conn, **kwargs)

    def interrupt_running_tasks(self, *, now: Any = 0.0) -> int:
        with self._lock:
            self._ensure_open()
            return interrupt_running_tasks(self._conn, now=now)

    def save_task_question(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return save_task_question(self._conn, row)

    def get_task_question(self, question_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return get_task_question(self._conn, question_id)

    def list_open_questions(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            return list_open_questions(self._conn, **kwargs)

    def consume_task_question(self, question_id: str, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            return consume_task_question(self._conn, question_id, **kwargs)

    def claim_waiting_task(self, task_id: str, revision: int, *, now: Any = 0.0) -> bool:
        """见模块级 :func:`claim_waiting_task`（CAS：同一个 revision 只认一次）。"""

        with self._lock:
            self._ensure_open()
            return claim_waiting_task(self._conn, task_id, revision, now=now)

    def list_waiting_tasks(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_waiting_tasks`（等 VW / 可恢复 / 暂缓的那些）。"""

        with self._lock:
            self._ensure_open()
            return list_waiting_tasks(self._conn, **kwargs)

    def claim_config_index_due(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`claim_config_index_due`（回收过期租约 + 原子认领 + 按令牌取回）。"""

        with self._lock:
            self._ensure_open()
            return claim_config_index_due(self._conn, **kwargs)

    def mark_config_index(self, row_id: int, **kwargs: Any) -> bool:
        """见模块级 :func:`mark_config_index`（不带租约校验的通用状态写入口）。"""

        with self._lock:
            self._ensure_open()
            return mark_config_index(self._conn, row_id, **kwargs)

    def release_config_index_lease(self, row_id: int, **kwargs: Any) -> bool:
        """见模块级 :func:`release_config_index_lease`（带令牌交还租约）。"""

        with self._lock:
            self._ensure_open()
            return release_config_index_lease(self._conn, row_id, **kwargs)

    def complete_config_index(self, row_id: int, **kwargs: Any) -> str:
        """见模块级 :func:`complete_config_index`（``ready`` / ``superseded`` / ``lease_lost``）。"""

        with self._lock:
            self._ensure_open()
            return complete_config_index(self._conn, row_id, **kwargs)

    def fail_config_index(self, row_id: int, **kwargs: Any) -> bool:
        """见模块级 :func:`fail_config_index`（带令牌记失败 / 退避 / 终态）。"""

        with self._lock:
            self._ensure_open()
            return fail_config_index(self._conn, row_id, **kwargs)

    def has_newer_config_index(self, row_id: int) -> bool:
        """见模块级 :func:`has_newer_config_index`（True = 这一行已经是过期作业）。"""

        with self._lock:
            self._ensure_open()
            return has_newer_config_index(self._conn, row_id)

    def apply_config_index_result(self, row_id: int, **kwargs: Any) -> dict[str, Any] | None:
        """见模块级 :func:`apply_config_index_result`（``ready`` → ``done``，拿不到回 None）。"""

        with self._lock:
            self._ensure_open()
            return apply_config_index_result(self._conn, row_id, **kwargs)

    def invalidate_config_index(self, **kwargs: Any) -> int:
        """见模块级 :func:`invalidate_config_index`（删除 / 禁用即失效，返回作废行数）。"""

        with self._lock:
            self._ensure_open()
            return invalidate_config_index(self._conn, **kwargs)

    def recover_config_index_leases(self, **kwargs: Any) -> int:
        """见模块级 :func:`recover_config_index_leases`（重启可恢复，返回回收行数）。"""

        with self._lock:
            self._ensure_open()
            return recover_config_index_leases(self._conn, **kwargs)

    def count_config_index(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_config_index`（按类型 / 状态 / 作用域计数）。"""

        with self._lock:
            self._ensure_open()
            return count_config_index(self._conn, **kwargs)

    def list_config_index_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_config_index_rows`（按条件列行，id 升序）。"""

        with self._lock:
            self._ensure_open()
            return list_config_index_rows(self._conn, **kwargs)

    def get_config_index_row(self, row_id: int) -> dict[str, Any] | None:
        """见模块级 :func:`get_config_index_row`（认不出 / 没有回 None）。"""

        with self._lock:
            self._ensure_open()
            return get_config_index_row(self._conn, row_id)

    def clear_config_index(self, **kwargs: Any) -> int:
        """见模块级 :func:`clear_config_index`（删行，返回删掉的条数）。"""

        with self._lock:
            self._ensure_open()
            return clear_config_index(self._conn, **kwargs)

    def drop_superseded_topic_index(self, **kwargs: Any) -> dict[str, int]:
        """见模块级 :func:`drop_superseded_topic_index`（``refresh`` 用，返回两个计数）。"""

        with self._lock:
            self._ensure_open()
            return drop_superseded_topic_index(self._conn, **kwargs)

    # ---- 阶段M1 追加：表情包收藏与展示记录 --------------------------------
    #
    # 和 B0 / B1 那两组一个规矩：连接接上模块级同名函数，于是
    # ``AsyncDatabase.call("create_meme_entry", ...)`` 就能从事件循环里调；真正的读写
    # 逻辑只有一份，在文件末尾的「阶段M1」分区里。**M1 没有生产调用点**（不接回复
    # 路径），唯一的使用者是管理页接口与测试。

    def insert_meme_asset(self, **kwargs: Any) -> dict[str, Any] | None:
        """见模块级 :func:`insert_meme_asset`（按内容身份去重，只补空字段）。"""

        with self._lock:
            self._ensure_open()
            return insert_meme_asset(self._conn, **kwargs)

    def get_meme_asset(self, media_id: str) -> dict[str, Any] | None:
        """见模块级 :func:`get_meme_asset`。"""

        with self._lock:
            self._ensure_open()
            return get_meme_asset(self._conn, media_id)

    def list_meme_assets(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_meme_assets`。"""

        with self._lock:
            self._ensure_open()
            return list_meme_assets(self._conn, **kwargs)

    def cleanup_meme_assets(self, **kwargs: Any) -> int:
        """见模块级 :func:`cleanup_meme_assets`：返回删掉的资产元数据行数。"""

        with self._lock:
            self._ensure_open()
            return cleanup_meme_assets(self._conn, **kwargs)

    def create_meme_entry(self, **kwargs: Any) -> dict[str, Any]:
        """见模块级 :func:`create_meme_entry`（``created`` 区分新建/撞上已有的）。"""

        with self._lock:
            self._ensure_open()
            return create_meme_entry(self._conn, **kwargs)

    def find_meme_entry_by_media(self, media_id: str) -> dict[str, Any] | None:
        """见模块级 :func:`find_meme_entry_by_media`。"""

        with self._lock:
            self._ensure_open()
            return find_meme_entry_by_media(self._conn, media_id)

    def assign_meme_id(self) -> str:
        """见模块级 :func:`assign_meme_id`（只取号不建行）。"""

        with self._lock:
            self._ensure_open()
            return assign_meme_id(self._conn)

    def get_meme_entry(self, entry_id: str) -> dict[str, Any] | None:
        """见模块级 :func:`get_meme_entry`（含已软删的行）。"""

        with self._lock:
            self._ensure_open()
            return get_meme_entry(self._conn, entry_id)

    def list_meme_entries(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_meme_entries`（连资产元数据一起带出）。"""

        with self._lock:
            self._ensure_open()
            return list_meme_entries(self._conn, **kwargs)

    def count_meme_entries(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_meme_entries`。"""

        with self._lock:
            self._ensure_open()
            return count_meme_entries(self._conn, **kwargs)

    def update_meme_entry(self, entry_id: str, **kwargs: Any) -> dict[str, Any]:
        """见模块级 :func:`update_meme_entry`（值没变则 ``revision`` 不动）。"""

        with self._lock:
            self._ensure_open()
            return update_meme_entry(self._conn, entry_id, **kwargs)

    def replace_meme_entry_media(
        self, entry_id: str, media_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        """见模块级 :func:`replace_meme_entry_media`（换底层图片）。"""

        with self._lock:
            self._ensure_open()
            return replace_meme_entry_media(self._conn, entry_id, media_id, **kwargs)

    def set_meme_entry_status(
        self, entry_id: str, status: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """见模块级 :func:`set_meme_entry_status`（``revision`` 跟着 +1）。"""

        with self._lock:
            self._ensure_open()
            return set_meme_entry_status(self._conn, entry_id, status, **kwargs)

    def delete_meme_entry(self, entry_id: str, **kwargs: Any) -> bool:
        """见模块级 :func:`delete_meme_entry`（软删，记录留着）。"""

        with self._lock:
            self._ensure_open()
            return delete_meme_entry(self._conn, entry_id, **kwargs)

    def purge_meme_entry(self, entry_id: str) -> bool:
        """见模块级 :func:`purge_meme_entry`（硬删，连带来源/变体/用法）。"""

        with self._lock:
            self._ensure_open()
            return purge_meme_entry(self._conn, entry_id)

    def add_meme_source(self, **kwargs: Any) -> int:
        """见模块级 :func:`add_meme_source`：返回来源行的 ``source_id``。"""

        with self._lock:
            self._ensure_open()
            return add_meme_source(self._conn, **kwargs)

    def list_meme_sources(self, entry_id: str) -> list[dict[str, Any]]:
        """见模块级 :func:`list_meme_sources`。"""

        with self._lock:
            self._ensure_open()
            return list_meme_sources(self._conn, entry_id)

    def meme_scope_set(self, entry_id: str) -> list[str]:
        """见模块级 :func:`meme_scope_set`（各来源的 ``allowed_scopes``）。"""

        with self._lock:
            self._ensure_open()
            return meme_scope_set(self._conn, entry_id)

    # ---- 阶段M4 追加：表情包向量（独立成表） ------------------------------

    def list_meme_vectors_for_entries(
        self, entry_ids: Iterable[str], **kwargs: Any
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`list_meme_vectors_for_entries`（``vector`` 是原始 BLOB）。"""

        with self._lock:
            self._ensure_open()
            return list_meme_vectors_for_entries(self._conn, entry_ids, **kwargs)

    def upsert_meme_vector(self, **kwargs: Any) -> bool:
        """见模块级 :func:`upsert_meme_vector`（同键覆盖）。"""

        with self._lock:
            self._ensure_open()
            return upsert_meme_vector(self._conn, **kwargs)

    def delete_meme_vectors_for_entries(self, entry_ids: Iterable[str], **kwargs: Any) -> int:
        """见模块级 :func:`delete_meme_vectors_for_entries`：返回删掉的条数。"""

        with self._lock:
            self._ensure_open()
            return delete_meme_vectors_for_entries(self._conn, entry_ids, **kwargs)

    def count_meme_vectors(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_meme_vectors`。"""

        with self._lock:
            self._ensure_open()
            return count_meme_vectors(self._conn, **kwargs)

    def meme_entries_missing_vectors(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`meme_entries_missing_vectors`（后台补索引用）。"""

        with self._lock:
            self._ensure_open()
            return meme_entries_missing_vectors(self._conn, **kwargs)

    def update_meme_variant_relation(
        self, entry_a: Any, entry_b: Any, **kwargs: Any
    ) -> dict[str, Any] | None:
        """见模块级 :func:`update_meme_variant_relation`（人工确认变体用）。"""

        with self._lock:
            self._ensure_open()
            return update_meme_variant_relation(self._conn, entry_a, entry_b, **kwargs)

    # ---- 攒图评审收藏（用户 2026-10-24）：候选池与评审记账 ----------------

    def add_meme_collect_candidate(self, **kwargs: Any) -> bool:
        """见模块级 :func:`add_meme_collect_candidate`（同一 ``media_id`` 只进一次）。"""

        with self._lock:
            self._ensure_open()
            return add_meme_collect_candidate(self._conn, **kwargs)

    def revive_meme_collect_candidate(self, **kwargs: Any) -> bool:
        """见模块级 :func:`revive_meme_collect_candidate`（过期的 passed 可再进池一次）。"""

        with self._lock:
            self._ensure_open()
            return revive_meme_collect_candidate(self._conn, **kwargs)

    def fill_meme_collect_captions(self, **kwargs: Any) -> int:
        """见模块级 :func:`fill_meme_collect_captions`（给空白描述补上画面）。"""

        with self._lock:
            self._ensure_open()
            return fill_meme_collect_captions(self._conn, **kwargs)

    def list_meme_collect_candidates(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_meme_collect_candidates`。"""

        with self._lock:
            self._ensure_open()
            return list_meme_collect_candidates(self._conn, **kwargs)

    def count_meme_collect_candidates(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_meme_collect_candidates`。"""

        with self._lock:
            self._ensure_open()
            return count_meme_collect_candidates(self._conn, **kwargs)

    def assign_meme_collect_refs(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`assign_meme_collect_refs`（一次事务分配"候选N"）。"""

        with self._lock:
            self._ensure_open()
            return assign_meme_collect_refs(self._conn, **kwargs)

    def release_meme_collect_refs(self, **kwargs: Any) -> int:
        """见模块级 :func:`release_meme_collect_refs`。"""

        with self._lock:
            self._ensure_open()
            return release_meme_collect_refs(self._conn, **kwargs)

    def finish_meme_collect_candidates(self, **kwargs: Any) -> int:
        """见模块级 :func:`finish_meme_collect_candidates`（一批一次性收尾）。"""

        with self._lock:
            self._ensure_open()
            return finish_meme_collect_candidates(self._conn, **kwargs)

    def expire_meme_collect_candidates(self, **kwargs: Any) -> int:
        """见模块级 :func:`expire_meme_collect_candidates`（TTL 清理）。"""

        with self._lock:
            self._ensure_open()
            return expire_meme_collect_candidates(self._conn, **kwargs)

    def add_meme_collect_review(self, **kwargs: Any) -> dict[str, Any]:
        """见模块级 :func:`add_meme_collect_review`（只追加的评审记账）。"""

        with self._lock:
            self._ensure_open()
            return add_meme_collect_review(self._conn, **kwargs)

    def count_meme_collect_reviews(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_meme_collect_reviews`。"""

        with self._lock:
            self._ensure_open()
            return count_meme_collect_reviews(self._conn, **kwargs)

    def last_meme_collect_review(self, **kwargs: Any) -> dict[str, Any] | None:
        """见模块级 :func:`last_meme_collect_review`。"""

        with self._lock:
            self._ensure_open()
            return last_meme_collect_review(self._conn, **kwargs)

    def meme_collect_inventory(self, **kwargs: Any) -> dict[str, Any]:
        """见模块级 :func:`meme_collect_inventory`（库存事实，只读）。"""

        with self._lock:
            self._ensure_open()
            return meme_collect_inventory(self._conn, **kwargs)

    # ---- 阶段E2 追加：每日经历账 ------------------------------------------

    def save_experience(self, experience: Mapping[str, Any]) -> dict[str, Any] | None:
        """见模块级 :func:`save_experience`（同一编号只更新不新增）。"""

        with self._lock:
            self._ensure_open()
            return save_experience(self._conn, experience)

    def get_experience(self, experience_id: Any) -> dict[str, Any] | None:
        """见模块级 :func:`get_experience`。"""

        with self._lock:
            self._ensure_open()
            return get_experience(self._conn, experience_id)

    def list_experiences(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_experiences`（时间倒序、有界）。"""

        with self._lock:
            self._ensure_open()
            return list_experiences(self._conn, **kwargs)

    def count_experiences(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_experiences`。"""

        with self._lock:
            self._ensure_open()
            return count_experiences(self._conn, **kwargs)

    def prune_experiences(self, **kwargs: Any) -> int:
        """见模块级 :func:`prune_experiences`（按世界日删旧的）。"""

        with self._lock:
            self._ensure_open()
            return prune_experiences(self._conn, **kwargs)

    def add_meme_variant(self, **kwargs: Any) -> dict[str, Any]:
        """见模块级 :func:`add_meme_variant`。"""

        with self._lock:
            self._ensure_open()
            return add_meme_variant(self._conn, **kwargs)

    def list_meme_variants(self, entry_id: str) -> list[dict[str, Any]]:
        """见模块级 :func:`list_meme_variants`（两个方向都算）。"""

        with self._lock:
            self._ensure_open()
            return list_meme_variants(self._conn, entry_id)

    def remove_meme_variant(self, entry_id: str, other_id: str, **kwargs: Any) -> bool:
        """见模块级 :func:`remove_meme_variant`。"""

        with self._lock:
            self._ensure_open()
            return remove_meme_variant(self._conn, entry_id, other_id, **kwargs)

    def add_meme_usage(self, **kwargs: Any) -> int:
        """见模块级 :func:`add_meme_usage`：返回 ``usage_id``。"""

        with self._lock:
            self._ensure_open()
            return add_meme_usage(self._conn, **kwargs)

    def list_meme_usage(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_meme_usage`（``detail`` 已解析成 dict）。"""

        with self._lock:
            self._ensure_open()
            return list_meme_usage(self._conn, **kwargs)

    def count_meme_usage(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_meme_usage`（``since`` 之后的才数）。"""

        with self._lock:
            self._ensure_open()
            return count_meme_usage(self._conn, **kwargs)

    def claim_exposure(self, **kwargs: Any) -> dict[str, Any]:
        """见模块级 :func:`claim_exposure`。

        走 :meth:`transaction`：认领是**读-判-写**，必须在同一个事务里，否则两个端
        可能都读到 ``pending`` 然后都认领成功（方案 §3.3 的"防并发双端补附"）。
        """

        with self._lock:
            self._ensure_open()
            return self.in_transaction(lambda conn: claim_exposure(conn, **kwargs))

    def mark_exposure(self, **kwargs: Any) -> bool:
        """见模块级 :func:`mark_exposure`（只认 ``delivered`` / ``failed``）。"""

        with self._lock:
            self._ensure_open()
            return mark_exposure(self._conn, **kwargs)

    def release_exposure(self, **kwargs: Any) -> bool:
        """见模块级 :func:`release_exposure`（``claimed`` → ``pending``）。"""

        with self._lock:
            self._ensure_open()
            return release_exposure(self._conn, **kwargs)

    def get_exposure(self, **kwargs: Any) -> dict[str, Any] | None:
        """见模块级 :func:`get_exposure`。"""

        with self._lock:
            self._ensure_open()
            return get_exposure(self._conn, **kwargs)

    def find_exposure(self, occurrence_id: str) -> list[dict[str, Any]]:
        """见模块级 :func:`find_exposure`（一次出现在各阶段上的记录）。"""

        with self._lock:
            self._ensure_open()
            return find_exposure(self._conn, occurrence_id)

    def mark_exposure_delivered(self, **kwargs: Any) -> bool:
        """见模块级 :func:`mark_exposure_delivered`（只有它封死这次出现）。"""

        with self._lock:
            self._ensure_open()
            return mark_exposure_delivered(self._conn, **kwargs)

    def mark_exposure_failed(self, **kwargs: Any) -> bool:
        """见模块级 :func:`mark_exposure_failed`（不算展示，下一轮可重试）。"""

        with self._lock:
            self._ensure_open()
            return mark_exposure_failed(self._conn, **kwargs)

    def count_exposures(self, **kwargs: Any) -> int:
        """见模块级 :func:`count_exposures`。"""

        with self._lock:
            self._ensure_open()
            return count_exposures(self._conn, **kwargs)

    def cleanup_meme_exposures(self, **kwargs: Any) -> int:
        """见模块级 :func:`cleanup_meme_exposures`（按 ``exposure_keep_days`` 清）。"""

        with self._lock:
            self._ensure_open()
            return cleanup_meme_exposures(self._conn, **kwargs)

    def cleanup_meme_usage(self, **kwargs: Any) -> int:
        """见模块级 :func:`cleanup_meme_usage`。"""

        with self._lock:
            self._ensure_open()
            return cleanup_meme_usage(self._conn, **kwargs)

    # ---- 阶段B3：Agent 任务账本与产物台账的薄壳 ----
    #
    # 模块级那几个函数（`save_agent_task` 等）以 ``conn`` 当第一个参数，直接写更容易
    # 在事务里连着调。但引擎那一侧手上是 :class:`Database`（或它的异步壳），同步读
    # （`agent_section()` 这种只读观测）与异步写都要能走到，所以照 `get_meme_entry`
    # 那套补一层薄壳：**只转发，不加任何判断**（状态逻辑全在 core/agent_tasks.py）。

    def save_agent_task(self, task: Mapping[str, Any], *, now: Any = 0.0) -> dict[str, Any] | None:
        """见模块级 :func:`save_agent_task`（终态在库里也不许被改口）。"""

        with self._lock:
            self._ensure_open()
            return save_agent_task(self._conn, task, now=now)

    def get_agent_task(self, task_id: Any) -> dict[str, Any] | None:
        """见模块级 :func:`get_agent_task`。"""

        with self._lock:
            self._ensure_open()
            return get_agent_task(self._conn, task_id)

    def list_agent_tasks(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_agent_tasks`（按时间倒序，可按会话 / 状态筛）。"""

        with self._lock:
            self._ensure_open()
            return list_agent_tasks(self._conn, **kwargs)

    def unfinished_agent_tasks(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`unfinished_agent_tasks`（启动收尾用）。"""

        with self._lock:
            self._ensure_open()
            return unfinished_agent_tasks(self._conn, **kwargs)

    def insert_agent_artifact(self, artifact: Mapping[str, Any]) -> dict[str, Any] | None:
        """见模块级 :func:`insert_agent_artifact`。"""

        with self._lock:
            self._ensure_open()
            return insert_agent_artifact(self._conn, artifact)

    def get_agent_artifact(self, artifact_id: Any) -> dict[str, Any] | None:
        """见模块级 :func:`get_agent_artifact`。"""

        with self._lock:
            self._ensure_open()
            return get_agent_artifact(self._conn, artifact_id)

    def list_agent_artifacts(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`list_agent_artifacts`。"""

        with self._lock:
            self._ensure_open()
            return list_agent_artifacts(self._conn, **kwargs)

    def mark_agent_artifact_sent(self, artifact_id: Any, *, now: Any = 0.0) -> dict[str, Any] | None:
        """见模块级 :func:`mark_agent_artifact_sent`（时间只记第一次）。"""

        with self._lock:
            self._ensure_open()
            return mark_agent_artifact_sent(self._conn, artifact_id, now=now)

    def prune_agent_artifacts(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`prune_agent_artifacts`（默认只删已发送且过期的）。"""

        with self._lock:
            self._ensure_open()
            return prune_agent_artifacts(self._conn, **kwargs)


    # Exact storage APIs use one transaction for read snapshots and state transitions.
    def _storage_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        with self.transaction() as conn:
            return globals()[name](conn, *args, **kwargs)

    def query_topics_metadata(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            return query_topics_metadata(self._conn, **kwargs)

    def resolve_topic_page_now(self, **kwargs: Any) -> float:
        return resolve_topic_page_now(**kwargs)

    def list_topics_page(self, **kwargs: Any) -> dict[str, Any]:
        return self._storage_call('list_topics_page', **kwargs)

    def get_source_events_by_ids(self, event_ids: Iterable[str], **kwargs: Any) -> dict[str, Any]:
        return self._storage_call('get_source_events_by_ids', event_ids, **kwargs)

    def shares_for_sources(self, source_ids: Iterable[str], **kwargs: Any) -> list[dict[str, Any]]:
        return self._storage_call('shares_for_sources', source_ids, **kwargs)

    def get_topic_snapshot(self, topic_ids: Iterable[str], **kwargs: Any) -> dict[str, Any]:
        return self._storage_call('get_topic_snapshot', topic_ids, **kwargs)

    def begin_decision(self, decision_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._storage_call('begin_decision', decision_id, **kwargs)

    def cancel_plan_execution(self, execution_id: str, *, association: dict[str, Any], now: float) -> dict[str, Any]:
        return self._storage_call('cancel_plan_execution', execution_id, association=association, now=now)

    def cancel_decision_for_regeneration(self, decision_id: str, *, now: float) -> bool:
        return self._storage_call('cancel_decision_for_regeneration', decision_id, now=now)

    def seal_outbox(self, decision_id: str, *, now: float) -> bool:
        return self._storage_call('seal_outbox', decision_id, now=now)

    def extend_decision_inputs(self, decision_id: str, *, event_ids: Iterable[str], now: float) -> bool:
        return self._storage_call('extend_decision_inputs', decision_id, event_ids=event_ids, now=now)

    def finalize_segment_exposure(self, segment_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._storage_call('finalize_segment_exposure', segment_id, **kwargs)

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        return self._storage_call('get_decision', decision_id)

    def set_decision_status(self, decision_id: str, status: str, **kwargs: Any) -> dict[str, Any]:
        return self._storage_call('set_decision_status', decision_id, status, **kwargs)

    def get_processed_cursor(self, **kwargs: Any) -> int:
        return self._storage_call('get_processed_cursor', **kwargs)

    def event_processing_status(self, event_id: str, **kwargs: Any) -> dict[str, Any] | None:
        return self._storage_call('event_processing_status', event_id, **kwargs)

    def prepare_outbox_segment(self, segment_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._storage_call('prepare_outbox_segment', segment_id, **kwargs)

    def reject_generated_segment(self, segment_id: str, *, now: float = 0.0) -> bool:
        return self._storage_call('reject_generated_segment', segment_id, now=now)

    def begin_segment_send(self, segment_id: str, **kwargs: Any) -> bool:
        return self._storage_call('begin_segment_send', segment_id, **kwargs)

    def record_segment_receipt(self, segment_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._storage_call('record_segment_receipt', segment_id, **kwargs)

    def list_outbox_segments(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._storage_call('list_outbox_segments', **kwargs)

    def acquire_share_claim(self, **kwargs: Any) -> bool:
        return self._storage_call('acquire_share_claim', **kwargs)

    def release_share_claim(self, **kwargs: Any) -> int:
        return self._storage_call('release_share_claim', **kwargs)

    def processed_event_ids(self, event_ids: Iterable[str], **kwargs: Any) -> list[str]:
        return self._storage_call('processed_event_ids', event_ids, **kwargs)

    def finish_decision(self, decision_id: str, status: str, **kwargs: Any) -> dict[str, Any]:
        return self.set_decision_status(decision_id, status, **kwargs)

    # ------------------------------------------------------------------
    # 知识卡（她查过的梗 / 词 / 事，见文件末尾同名分区）
    # ------------------------------------------------------------------

    def upsert_knowledge_card(self, **kwargs: Any) -> dict[str, Any] | None:
        """见模块级 :func:`upsert_knowledge_card`（同一个 subject 只更新，不新增行）。"""

        with self._lock:
            self._ensure_open()
            return upsert_knowledge_card(self._conn, **kwargs)

    def find_knowledge_cards(self, **kwargs: Any) -> list[dict[str, Any]]:
        """见模块级 :func:`find_knowledge_cards`（按关键词命中数召回）。"""

        with self._lock:
            self._ensure_open()
            return find_knowledge_cards(self._conn, **kwargs)

    def touch_knowledge_card(self, card_id: Any, *, now: Any = 0.0) -> bool:
        """见模块级 :func:`touch_knowledge_card`（只加计数，不改内容）。"""

        with self._lock:
            self._ensure_open()
            return touch_knowledge_card(self._conn, card_id, now=now)

    def expire_knowledge_cards(self, *, now: Any = 0.0) -> int:
        """见模块级 :func:`expire_knowledge_cards`（过期只改 status，不删行）。"""

        with self._lock:
            self._ensure_open()
            return expire_knowledge_cards(self._conn, now=now)

    def list_knowledge_cards(
        self, session_id: Any, *, include_expired: bool = False
    ) -> list[dict[str, Any]]:
        """见模块级 :func:`list_knowledge_cards`（管理页 / 排查用）。"""

        with self._lock:
            self._ensure_open()
            return list_knowledge_cards(
                self._conn, session_id, include_expired=include_expired
            )


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

    async def transaction(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """在**同一个事务**里跑一个同步函数（阶段F3）。

        ``func(conn, *args, **kwargs)`` 在写线程里执行；抛异常就整体回滚并把异常
        原样抛回来（调用方按"这次作业失败了"处理，不推进游标）。
        """

        return await asyncio.to_thread(
            self._db.in_transaction, lambda conn: func(conn, *args, **kwargs)
        )


# ---------------------------------------------------------------------------
# 阶段A1 追加分区：输入事件身份（source_events）与 schema 版本（schema_meta）
#
# 设计见计划 §4.0 / §4.1 / §5(A1)：只增表 / 增索引 / 增函数，不动上面任何既有表
# 与既有函数。身份与操作 key 的纯计算在 ``core/source_events.py``，这里只管存。
#
# 幂等：``_migrate_source_events`` 由 ``Database._migrate()`` 调用，空库 / 旧库 /
# 已迁移库三种起点都能重复跑，重复执行不报错、不丢数据。
# ---------------------------------------------------------------------------


_SOURCE_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS source_events (
    event_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT '',
    origin_session_id TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    event_kind TEXT NOT NULL DEFAULT '',
    occurred_at REAL NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    reality_kind TEXT NOT NULL DEFAULT 'real',
    visibility_json TEXT NOT NULL DEFAULT '{}',
    operation_id TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 1,
    deleted_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_source_events_scope
    ON source_events (scope_id, occurred_at);
-- 部分唯一索引：只对非空 operation_id 生效（大量事件本来就没有 operation_id）
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_events_operation
    ON source_events (operation_id) WHERE operation_id != '';
"""

_SOURCE_EVENTS_SOFT_DELETE_DDL = """
ALTER TABLE source_events ADD COLUMN deleted_at REAL NOT NULL DEFAULT 0;
"""


def _ensure_agent_task_columns(conn: sqlite3.Connection) -> None:
    """给 ``agent_tasks`` 补齐 P1 需要的列（**幂等**，失败不改表）。

    为什么不做"版本迁移"：这一版还没发布过，老版本根本没有 agent 功能，也就不存在
    需要按版本号迁移的库。唯一要照顾的是**已经有表的本机实例**——它们建表时还没有这些列。
    所以这里只做一件事：``PRAGMA table_info`` 比对，缺哪列 ``ADD COLUMN`` 哪列。
    """

    wanted = (
        ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
        ("brief_json", "TEXT NOT NULL DEFAULT ''"),
        ("checkpoint_json", "TEXT NOT NULL DEFAULT ''"),
        ("revision", "INTEGER NOT NULL DEFAULT 0"),
        ("wait_reason", "TEXT NOT NULL DEFAULT ''"),
        ("active_elapsed", "REAL NOT NULL DEFAULT 0"),
        ("expires_at", "REAL NOT NULL DEFAULT 0"),
        ("parent_task_id", "TEXT NOT NULL DEFAULT ''"),
    )
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_tasks)")}
    except sqlite3.Error:
        # 表还没有（全新库由 CREATE TABLE 建好），这里什么都不用做
        return
    if not columns:
        return
    missing = [(name, ddl) for name, ddl in wanted if name not in columns]
    if not missing:
        return
    try:
        for name, ddl in missing:
            conn.execute(f"ALTER TABLE agent_tasks ADD COLUMN {name} {ddl}")
        conn.commit()
    except sqlite3.Error:
        # 补列失败就回滚，让依赖新列的功能按"没有这份数据"降级——不许留下半张表
        conn.rollback()


def _vw_skills_data_dir(conn: sqlite3.Connection) -> Path | None:
    """数据库文件所在目录（= 数据目录）；内存库/临时库返回 None。"""

    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        try:
            name, path = row[1], row[2]
        except (IndexError, TypeError):
            continue
        if str(name) == "main" and str(path or ""):
            return Path(str(path)).resolve().parent
    return None


def _archive_vw_skills(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> str:
    """把旧表内容写到 ``<数据目录>/backups/vw_skills-v1-<ts>.json``，返回路径。"""

    directory = _vw_skills_data_dir(conn)
    if directory is None:
        return ""
    target_dir = directory / "backups"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    path = target_dir / f"vw_skills-v1-{stamp}.json"
    index = 2
    while path.exists():
        path = target_dir / f"vw_skills-v1-{stamp}-{index}.json"
        index += 1
    payload = {
        "schema_version": 1,
        "archived_at": time.time(),
        "count": len(rows),
        "skills": rows,
    }
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return ""
    return str(path)


def _seed_vw_skills(conn: sqlite3.Connection) -> None:
    """把内置流程重灌进库（**镜像**：提示词仍以内置 ``skills/*.md`` 为准）。"""

    now = time.time()
    for skill in skill_policy.builtin_skills(now=now):
        try:
            save_vw_skill(conn, skill.as_dict())
        except sqlite3.Error:  # pragma: no cover - 灌不进去就当库里少一份镜像，提示词照旧走文件
            logger.warning("内置流程重灌失败：%s", skill.skill_id)


def _migrate_vw_skills(conn: sqlite3.Connection) -> None:
    """旧形状 → 归档 + drop + 重建 + 重灌内置（幂等；形状已对就什么都不做）。

    用户 2026-10 定的口径：旧的自凝练 skill **不迁移**，直接连表一起清掉；
    清之前把内容留一份 JSON 在 ``backups/`` 里，事后能翻出来看。
    """

    try:
        info = conn.execute("PRAGMA table_info(vw_skills)").fetchall()
    except sqlite3.Error:
        return
    columns: set[str] = set()
    for row in info:
        try:
            columns.add(str(row["name"]))
        except (IndexError, TypeError):
            columns.add(str(row[1]))
    if not columns:
        return
    if "schema_version" in columns:
        try:
            versions = {
                int(row[0] or 0) for row in conn.execute("SELECT DISTINCT schema_version FROM vw_skills")
            }
        except (sqlite3.Error, TypeError, ValueError):
            versions = set()
        # 空表（版本列在、没有行）= 已经是新形状，只把内置镜像补进去；
        # 全等于当前版本 = 什么都不用做
        if not versions:
            try:
                empty = conn.execute("SELECT 1 FROM vw_skills LIMIT 1").fetchone() is None
            except sqlite3.Error:
                empty = False
            if empty:
                _seed_vw_skills(conn)
            return
        if versions == {skill_policy.SCHEMA_VERSION}:
            return
    try:
        rows = [dict(row) for row in conn.execute("SELECT * FROM vw_skills").fetchall()]
    except sqlite3.Error:
        rows = []
    backup = _archive_vw_skills(conn, rows) if rows else ""
    try:
        conn.execute("DROP TABLE IF EXISTS vw_skills")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return
    _run_ddl(conn, VW_SKILLS_TABLE_SQL)
    _seed_vw_skills(conn)
    logger.info(
        "旧技能已归档并重建（%s 条旧数据 → %s）",
        len(rows),
        backup or "内存库未留档",
    )


def _migrate_source_event_soft_delete(conn: sqlite3.Connection) -> None:
    """给老库的 ``source_events`` 补 ``deleted_at``（幂等；补过就什么都不做）。

    为什么要这一列：来源被**撤回/纠错**时，派生出来的谈资要跟着作废（清单 §2.4 的
    「来源撤回连坐」），而"这条来源还算不算数"必须是一个**存在库里的事实**，
    不能靠删行——删了行就再也说不清"当时到底有没有这一条"。
    """

    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source_events'"
    ).fetchone():
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(source_events)")}
    if "deleted_at" in columns:
        return
    try:
        conn.execute(_SOURCE_EVENTS_SOFT_DELETE_DDL)
        conn.commit()
    except sqlite3.Error:  # pragma: no cover - 并发补列时别人已经加上了
        conn.rollback()


_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _json_dict(value: Any) -> dict[str, Any]:
    """把库里存的 JSON 对象读回来（坏 JSON / 非对象一律当空 dict，不抛异常）。"""

    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _source_event_row(row: Any) -> dict[str, Any]:
    """source_events 一行 → dict（payload / visibility 解成对象，坏 JSON 降级空 dict）。"""

    item = dict(row)
    for key in (
        "event_id",
        "scope_id",
        "origin_session_id",
        "sender_id",
        "event_kind",
        "reality_kind",
        "operation_id",
    ):
        item[key] = str(item.get(key) or "")
    try:
        item["occurred_at"] = float(item.get("occurred_at") or 0.0)
    except (TypeError, ValueError):
        item["occurred_at"] = 0.0
    try:
        item["schema_version"] = int(item.get("schema_version") or 0)
    except (TypeError, ValueError):
        item["schema_version"] = 0
    item["payload"] = _json_dict(item.get("payload_json"))
    item["visibility"] = _json_dict(item.get("visibility_json"))
    return item


def record_source_event(conn: sqlite3.Connection, event: Any) -> bool:
    """登记一条输入事件（``INSERT OR IGNORE``）。

    **返回 True 表示这次真的插进去了，False 表示这条 event_id 已经存在**
    ——这就是 X03 去重的证据，不是「操作成功/失败」。

    传进来的可以是 :class:`core.source_events.SourceEvent`，也可以是任何能
    被 ``SourceEvent.from_row`` 吃下的行（dict / sqlite3.Row）。``event_id`` 为空
    （"不知道身份"）时不写库，直接返回 False：空身份由调用方按保守规则处理，
    不在这里造一个假身份。
    """

    row = event.to_row() if hasattr(event, "to_row") else SourceEvent.from_row(event).to_row()
    event_id = str(row.get("event_id") or "").strip()
    if not event_id:
        return False
    cursor = conn.execute(
        "INSERT OR IGNORE INTO source_events (event_id, scope_id, origin_session_id, sender_id,"
        " event_kind, occurred_at, payload_json, reality_kind, visibility_json, operation_id,"
        " schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            str(row.get("scope_id") or ""),
            str(row.get("origin_session_id") or ""),
            str(row.get("sender_id") or ""),
            str(row.get("event_kind") or ""),
            float(row.get("occurred_at") or 0.0),
            str(row.get("payload_json") or "{}"),
            str(row.get("reality_kind") or REALITY_REAL),
            str(row.get("visibility_json") or "{}"),
            str(row.get("operation_id") or ""),
            int(row.get("schema_version") or SOURCE_EVENTS_SCHEMA_VERSION),
        ),
    )
    conn.commit()
    return int(cursor.rowcount or 0) > 0


def _cell_of(row: Any, key: str, default: Any = "") -> Any:
    """从任意行形状里取一列（dict / sqlite3.Row / 对象都认，坏形状给 default）。"""

    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def set_source_event_visibility(
    conn: sqlite3.Connection, event: Any, visibility: Mapping[str, Any]
) -> bool:
    with _storage_transaction(conn):
        return _set_source_event_visibility(_TransactionConnection(conn), event, visibility)


def _set_source_event_visibility(
    conn: sqlite3.Connection, event: Any, visibility: Mapping[str, Any]
) -> bool:
    """把一条来源事件的可见性声明写进 ``visibility_json``（清单 §2.2）。

    返回 True 表示这次真的落库了。**"它已经在库里、但当时没有声明"** 也要补写——
    这正是登记谈资时顺手给来源补声明的场景（``record_source_event`` 是
    ``INSERT OR IGNORE``，只补不覆盖正文）。

    **只写更严**：新声明会与库里已存的声明取最严（见 :func:`merge_visibility`），
    所以"先按 normal 记过、后来才发现是私密"能收紧，反过来收不松——模型只能建议
    更严的限制（计划 §1 第 4 条）。

    **空声明不写**（T8）：没传 / ``{}`` / 全 None 表示"这次没有声明"，不是"声明
    谁都不许看"；这种调用直接返回 False，一行都不碰（根因与历史数据修复见
    :func:`_migrate_source_visibility_empty_declaration_repair`）。
    """

    event_id = _source_event_id_of(event)
    if not event_id:
        return False
    if is_empty_declaration(visibility):
        return False
    existing: dict[str, Any] = {}
    present = False
    if _source_events_has_visibility(conn):
        row = conn.execute(
            "SELECT visibility_json FROM source_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is not None:
            present = True
            existing = _json_dict(row["visibility_json"])
    merged = merge_visibility(existing, visibility)
    payload = json.dumps(dict(merged), ensure_ascii=False, default=str, sort_keys=True)
    unchanged = existing == dict(merged)
    if not present:
        # 库里还没有这条事件：只补一条**只带可见性**的门牌，
        # 不去编造正文字段（"不知道身份/正文"的来源由调用方按保守规则处理）。
        conn.execute(
            "INSERT OR IGNORE INTO source_events (event_id, scope_id, origin_session_id,"
            " visibility_json, schema_version) VALUES (?, ?, ?, ?, ?)",
            (
                event_id,
                str(_cell_of(event, "scope_id", "") or merged.get("scope") or ""),
                str(_cell_of(event, "origin_session_id", "") or merged.get("origin_session_id") or ""),
                payload,
                SOURCE_EVENTS_SCHEMA_VERSION,
            ),
        )
    else:
        conn.execute(
            "UPDATE source_events SET visibility_json = ?, policy_revision = policy_revision + "
            "? WHERE event_id = ?",
            (payload, 0 if unchanged else 1, event_id),
        )
    conn.commit()
    return True


def _source_event_id_of(event: Any) -> str:
    """行 / 字典 / 对象 / **裸 id 字符串** → event_id（取不到就是空串）。

    允许裸字符串是为了让调用方少写一层 ``{"event_id": ...}``：这个函数的入参
    语义本来就是"哪条来源"，不是"一整行来源"。
    """

    if isinstance(event, str):
        return event.strip()
    return str(_cell_of(event, "event_id", "")).strip()


def _source_events_has_visibility(conn: sqlite3.Connection) -> bool:
    """``source_events`` 建出来没有（没建出来就别去查它）。"""

    return bool(
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='source_events'"
        ).fetchone()
    )


def source_event_visibility(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    """按 event_id 精确读一条来源的可见性声明（读不到 / 坏 JSON → ``{}``）。

    ``{}`` 的含义是**"库里没有这条来源，或者它从来没声明过"**——调用方必须把它
    当成"查不到许可"来处理，而不是当成"公开"（清单 §2.3 的 fail-closed）。
    """

    target = str(event_id or "").strip()
    if not target or not _source_events_has_visibility(conn):
        return {}
    row = conn.execute(
        "SELECT visibility_json FROM source_events WHERE event_id = ?", (target,)
    ).fetchone()
    if row is None:
        return {}
    return _json_dict(row["visibility_json"])


def source_events_visibility(
    conn: sqlite3.Connection, event_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """按候选 ID 批量精确查可见性声明（清单 §2.3：不再靠"前 1000 条"当全量）。

    只返回**真的在库里**的那些 id；没查到的 id 由调用方按"读不出来 → 不推荐"处理。
    返回 ``{event_id: visibility}``，重复 id 只查一次。
    """

    wanted = []
    seen: set[str] = set()
    for raw in event_ids or ():
        key = str(raw or "").strip()
        if key and key not in seen:
            seen.add(key)
            wanted.append(key)
    if not wanted or not _source_events_has_visibility(conn):
        return {}
    found: dict[str, dict[str, Any]] = {}
    step = 200  # 离 SQLite 的变量上限还有很大余量，也避免一条 SQL 太长
    for start in range(0, len(wanted), step):
        chunk = wanted[start : start + step]
        marks = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT event_id, visibility_json FROM source_events WHERE event_id IN ({marks})",
            tuple(chunk),
        ).fetchall()
        for row in rows:
            found[str(row["event_id"])] = _json_dict(row["visibility_json"])
    return found


def soft_delete_source_event(conn: sqlite3.Connection, event_id: str, *, now: float = 0.0) -> bool:
    """软删一条来源事件（清单 §2.4 的"来源撤回"）。

    soft delete 只是**阻止未来使用**，不删行、不删原始消息（计划 §4）：
    这样"当时到底说过没有"永远还在，而派生出来的谈资可以借它作废。
    返回 True 表示这次真的把它标成了已撤回。
    """

    target = str(event_id or "").strip()
    if not target or not _source_events_has_deleted_at_column(conn):
        return False
    cursor = conn.execute(
        "UPDATE source_events SET deleted_at = ? WHERE event_id = ? AND COALESCE(deleted_at, 0) = 0",
        (float(now or 0.0), target),
    )
    conn.commit()
    return int(cursor.rowcount or 0) > 0


def _source_events_has_deleted_at_column(conn: sqlite3.Connection) -> bool:
    """``source_events`` 有没有软删列（老库可能还没迁移过）。"""

    if not _source_events_has_visibility(conn):
        return False
    try:
        return "deleted_at" in {row["name"] for row in conn.execute("PRAGMA table_info(source_events)")}
    except sqlite3.Error:  # pragma: no cover
        return False


def retract_source_event(
    conn: sqlite3.Connection, event_id: str, *, now: float = 0.0
) -> list[str]:
    """撤回一条来源，并**连坐**作废由它派生出来的谈资（清单 §2.4）。

    返回这次被连坐作废的 ``topic_id``（可能为空）。"发现来源错了"和"用户要求别再用
    这件事"都走这里：来源标软删，派生谈资转 ``deleted``——否则谈资池里那一条会一直
    被当成有效素材讲下去，而它的事实来源已经不算数了。
    """

    target = str(event_id or "").strip()
    if not target:
        return []
    with _storage_transaction(conn):
        stamp = float(now or time.time())
        cursor = conn.execute('UPDATE source_events SET deleted_at=?,policy_revision=policy_revision+1 '
                              'WHERE event_id=? AND COALESCE(deleted_at,0)=0', (stamp, target))
        if not cursor.rowcount:
            return []
        # The proxy suppresses commit in the legacy helper, preserving atomicity.
        return soft_delete_topics_for_source_event(_TransactionConnection(conn), target)


def soft_delete_topics_for_source_event(conn: sqlite3.Connection, event_id: str) -> list[str]:
    """把"引用了这条来源"的谈资全部软删，返回被删的 ``topic_id``。

    判定用 JSON 里的 id 精确匹配（``"<event_id>"`` 连着引号查），而不是 LIKE 模糊
    匹配：来源 id 之间常有公共前缀，模糊匹配会误伤别人的谈资。
    """

    target = str(event_id or "").strip()
    if not target:
        return []
    rows = conn.execute(
        "SELECT topic_id FROM topics WHERE status != ? AND EXISTS "
        "(SELECT 1 FROM json_each(CASE WHEN json_valid(source_event_ids_json) "
        "THEN source_event_ids_json ELSE '[]' END) WHERE value = ?)",
        (TOPIC_DELETED, target),
    ).fetchall()
    removed: list[str] = []
    for row in rows:
        topic_id = str(row["topic_id"] or "")
        if not topic_id:
            continue
        cursor = conn.execute(
            "UPDATE topics SET status = ?, version = version + 1 WHERE topic_id = ? AND status != ?",
            (TOPIC_DELETED, topic_id, TOPIC_DELETED),
        )
        if int(cursor.rowcount or 0) > 0:
            removed.append(topic_id)
    conn.commit()
    return removed


def get_source_event(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    """按 event_id 取一条（``payload`` / ``visibility`` 已经解成对象）。"""

    target = str(event_id or "").strip()
    if not target:
        return None
    row = conn.execute(
        "SELECT * FROM source_events WHERE event_id = ?", (target,)
    ).fetchone()
    return _source_event_row(row) if row is not None else None


def list_source_events(
    conn: sqlite3.Connection, *, scope_id: str = "", since: float = 0.0, limit: int = 100
) -> list[dict[str, Any]]:
    """按发生时间升序取一段事件（``scope_id`` 为空表示不限范围）。

    ``limit`` 上限 500：这个表会一直长，别让一次查询把整个历史拉进内存。
    """

    sql = "SELECT * FROM source_events"
    where: list[str] = []
    params: list[Any] = []
    if str(scope_id or ""):
        where.append("scope_id = ?")
        params.append(str(scope_id))
    try:
        floor = float(since or 0.0)
    except (TypeError, ValueError):
        floor = 0.0
    if floor:
        where.append("occurred_at >= ?")
        params.append(floor)
    if where:
        sql += " WHERE " + " AND ".join(where)
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 100
    sql += " ORDER BY occurred_at ASC, event_id ASC LIMIT ?"
    params.append(max(1, min(size, 500)))
    return [_source_event_row(row) for row in conn.execute(sql, tuple(params))]


def list_source_events_window(
    conn: sqlite3.Connection, *, after_seq: int = 0, to_seq: int = 0, limit: int = 200
) -> list[dict[str, Any]]:
    """按**登记顺序**取 ``(after_seq, to_seq]`` 这一段输入事件（阶段F1）。

    游标是单调的登记序号（sqlite 的 ``rowid``），不是发生时间：时间会被回拨、
    也会并列，序号不会。``to_seq`` 为 0 表示"一直取到最新"；调用方通常先冻结
    一个 ``to_seq`` 再读，整理期间新到的事件留到下一次作业（计划 §10 F1）。
    返回的每一行都带 ``seq``。
    """

    try:
        low = max(0, int(after_seq or 0))
    except (TypeError, ValueError):
        low = 0
    try:
        high = max(0, int(to_seq or 0))
    except (TypeError, ValueError):
        high = 0
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 200
    sql = "SELECT rowid AS seq, * FROM source_events WHERE rowid > ?"
    params: list[Any] = [low]
    if high:
        sql += " AND rowid <= ?"
        params.append(high)
    sql += " ORDER BY rowid ASC LIMIT ?"
    params.append(max(1, min(size, 500)))
    return [_source_event_row(row) for row in conn.execute(sql, tuple(params))]


def latest_source_event_seq(conn: sqlite3.Connection) -> int:
    """当前最新的登记序号（一条都没有时是 0）。"""

    row = conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) AS seq FROM source_events"
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


# ---------------------------------------------------------------------------
# 阶段F3 追加分区：增量提交的幂等账（applied_operations）
#
# 一个问题：同一份提案被重放（重试 / 手工再点一次 / 两次作业覆盖同一段素材）时，
# "好感 +1"这类**增量**不能被结算两次。做法是给每条会改状态的新条目一个
# operation key（作业 id + 类型 + 序号，见 core/consolidate_jobs.py 的
# operation_key / stamp_operation_keys），提交时先在这里抢一次：抢到了才算数，
# 抢不到就跳过。**必须在同一事务里抢**，否则事务回滚会留下"已经结算过"的假账。
# ---------------------------------------------------------------------------

_APPLIED_OPERATIONS_DDL = """
CREATE TABLE IF NOT EXISTS applied_operations (
    operation_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    applied_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_applied_operations_session
    ON applied_operations (session_id, applied_at);
"""


def _migrate_applied_operations(conn: sqlite3.Connection) -> None:
    """建 ``applied_operations``（幂等：空库 / 旧库 / 已迁移库都能跑）。"""

    try:
        conn.executescript(_APPLIED_OPERATIONS_DDL)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def record_operations(
    conn: sqlite3.Connection,
    keys: Iterable[str],
    *,
    kind: str = "",
    session_id: str = "",
    now: float = 0.0,
) -> set[str]:
    """抢一批 operation key，返回**这次真正认领到的**那些。

    已经记过的 key 抢不到（``INSERT OR IGNORE`` + ``rowcount``），调用方据此
    跳过重复提交。空 key 不算数也不抢。在事务里调用时不会提前提交。
    """

    claimed: set[str] = set()
    for key in keys or ():
        value = str(key or "")
        if not value:
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO applied_operations"
            " (operation_key, kind, session_id, applied_at) VALUES (?, ?, ?, ?)",
            (value, str(kind or ""), str(session_id or ""), float(now or 0.0)),
        )
        if cursor.rowcount:
            claimed.add(value)
    conn.commit()
    return claimed


def has_operation(conn: sqlite3.Connection, key: str) -> bool:
    """这个幂等键是不是已经记过了（**只读**：不抢占、不写库）。"""

    value = str(key or "")
    if not value:
        return False
    row = conn.execute(
        "SELECT 1 FROM applied_operations WHERE operation_key = ? LIMIT 1", (value,)
    ).fetchone()
    return row is not None


def list_applied_operations(
    conn: sqlite3.Connection, *, session_id: str = "", limit: int = 200
) -> list[dict[str, Any]]:
    """已经结算过的 operation key（面板/排查用，新的在前）。"""

    size = max(1, min(int(limit or 200), 1000))
    if str(session_id or ""):
        rows = conn.execute(
            "SELECT * FROM applied_operations WHERE session_id = ?"
            " ORDER BY applied_at DESC, operation_key DESC LIMIT ?",
            (str(session_id), size),
        )
    else:
        rows = conn.execute(
            "SELECT * FROM applied_operations"
            " ORDER BY applied_at DESC, operation_key DESC LIMIT ?",
            (size,),
        )
    return [dict(row) for row in rows]



def count_source_events(conn: sqlite3.Connection, *, scope_id: str = "") -> int:
    """这个范围里已经登记了多少条事件。"""


    if str(scope_id or ""):
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM source_events WHERE scope_id = ?", (str(scope_id),)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS c FROM source_events").fetchone()
    return int(row[0] or 0) if row is not None else 0


def attach_operation(
    conn: sqlite3.Connection, event_id: str, operation_id: str
) -> bool:
    """给已存在的事件补上 operation_id。

    ``False`` 有三种情况：事件不存在、参数为空、或**该 operation_id 已被别的事件
    占用**（部分唯一索引 ``idx_source_events_operation`` 抛 ``sqlite3.IntegrityError``，
    这里捕获后回滚，绝不覆盖原归属）。
    """

    target = str(event_id or "").strip()
    operation = str(operation_id or "").strip()
    if not target or not operation:
        return False
    try:
        cursor = conn.execute(
            "UPDATE source_events SET operation_id = ? WHERE event_id = ?",
            (operation, target),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return int(cursor.rowcount or 0) > 0


def find_event_by_operation(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    """operation 幂等查询：这个 operation_id 已经落在哪条事件上了。"""

    operation = str(operation_id or "").strip()
    if not operation:
        return None
    row = conn.execute(
        "SELECT * FROM source_events WHERE operation_id = ? LIMIT 1", (operation,)
    ).fetchone()
    return _source_event_row(row) if row is not None else None


def ensure_schema_meta(conn: sqlite3.Connection) -> None:
    """建 ``schema_meta(key, value)``（幂等）。"""

    conn.executescript(_SCHEMA_META_DDL)
    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """读 ``schema_meta.schema_version``；表不存在 / 值坏了都给 0。

    读一律以 ``schema_meta`` 为准，``PRAGMA user_version`` 只是冗余镜像。
    """

    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    try:
        return int(str(row[0]).strip())
    except (TypeError, ValueError):
        return 0


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """写 ``schema_meta.schema_version``（``INSERT ... ON CONFLICT``，幂等）。

    顺带把 ``PRAGMA user_version`` 写成同一个数（冗余镜像，方便别的 SQLite 工具
    看一眼）；读的时候不用它。
    """

    try:
        number = int(version)
    except (TypeError, ValueError):
        return
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(number),),
    )
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_applied_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(time.time()),),
    )
    conn.commit()
    try:
        conn.execute(f"PRAGMA user_version = {number}")
    except sqlite3.Error:  # pragma: no cover - 个别 SQLite 构建不给写，不影响真相
        pass


def _migrate_source_events(conn: sqlite3.Connection) -> None:
    """建 ``source_events`` / ``schema_meta`` 并写版本号。

    可重复调用：空库、只有旧表的旧库、已迁移过的库都能跑，重复执行不报错、
    不丢数据；失败整体回滚（旧库保持可用）后把异常抛出去，不静默降级。
    """

    try:
        conn.executescript(_SOURCE_EVENTS_DDL + _SCHEMA_META_DDL)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    if get_schema_version(conn) < SOURCE_EVENTS_SCHEMA_VERSION:
        set_schema_version(conn, SOURCE_EVENTS_SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# 阶段A2 追加分区：分享登记（shares）
#
# 设计见计划 §4.4 / §5(A2)：只增表 / 增索引 / 增函数，不动上面任何既有表与既有
# 函数。一行 = **一次已确认送出的分享**：谁（session_id）以什么用途
# （communicative_act）把哪份信息的第几版（source_id + information_revision）说了
# 出去，平台给了什么回执（status / platform_message_ids）。
#
# 口径与 ``core/ports.py`` 的发送回执一致：``accepted`` ≠ ``delivered``；失败的一律
# 不落库（"对方没听到"不算分享）；旧历史里看得出来但没有平台回执的标
# ``legacy_observed``，绝不补一个 delivered。
#
# 幂等：``_migrate_shares`` 由 ``Database._migrate()`` 调用，空库 / 旧库 / 已迁移库
# 三种起点都能重复跑，重复执行不报错、不丢数据。
# ---------------------------------------------------------------------------


_SHARES_DDL = """
CREATE TABLE IF NOT EXISTS shares (
    share_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    information_revision INTEGER NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL DEFAULT '',
    communicative_act TEXT NOT NULL DEFAULT '',
    operation_id TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    media_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'accepted',
    platform_message_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL DEFAULT 0,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS idx_shares_scope
    ON shares (scope_id, created_at);
CREATE INDEX IF NOT EXISTS idx_shares_source
    ON shares (source_id, information_revision);
CREATE INDEX IF NOT EXISTS idx_shares_session
    ON shares (session_id, created_at);
"""


def _json_text_list(value: Any) -> list[str]:
    """JSON 数组列 → 字符串列表（坏 JSON、单串、None 都不抛）。"""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = [value]
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        value = [value]
    try:
        items = list(value)
    except TypeError:
        items = [value]
    result: list[str] = []
    for item in items:
        text = item.decode("utf-8", "replace") if isinstance(item, (bytes, bytearray)) else str(item)
        text = text.strip()
        if text and text not in result:
            result.append(text)
    return result


def _share_row(row: Any) -> dict[str, Any] | None:
    """shares 一行 → dict（两个 id 列表解成数组，坏 JSON 降级空列表）。"""

    if row is None:
        return None
    item = dict(row)
    for key in (
        "share_id",
        "scope_id",
        "source_id",
        "session_id",
        "communicative_act",
        "operation_id",
        "content_hash",
        "text",
        "status",
    ):
        item[key] = str(item.get(key) or "")
    for key in ("information_revision",):
        try:
            item[key] = int(item.get(key) or 0)
        except (TypeError, ValueError):
            item[key] = 0
    try:
        item["created_at"] = float(item.get("created_at") or 0.0)
    except (TypeError, ValueError):
        item["created_at"] = 0.0
    sent = item.get("sent_at")
    try:
        item["sent_at"] = None if sent is None else float(sent)
    except (TypeError, ValueError):
        item["sent_at"] = None
    item["media_ids"] = _json_text_list(item.get("media_ids_json"))
    item["platform_message_ids"] = _json_text_list(
        item.get("platform_message_ids_json")
    )
    return item


def record_share(conn: sqlite3.Connection, share: Any) -> bool:
    """登记一次分享（``INSERT OR IGNORE``）。

    **返回 True 表示这次真的插进去了，False 表示同一件事已经登记过**（``share_id``
    已存在）或**根本拿不出身份**（不编一个假 key 塞进库里）。

    传进来的可以是 :class:`core.source_events.Share`，也可以是任何能被
    ``Share.from_row`` 吃下的行（dict / sqlite3.Row）。``share_id`` 为空时按
    ``share_id_for(...)`` 从 scope / session / 来源 / revision / 用途 / 正文 hash
    现算——同一个 operation 重复提交仍落到同一行。
    """

    row = share.to_row() if hasattr(share, "to_row") else Share.from_row(share).to_row()
    share_id = str(row.get("share_id") or "").strip()
    scope_id = str(row.get("scope_id") or "")
    source_id = str(row.get("source_id") or "")
    session_id = str(row.get("session_id") or "")
    communicative_act = str(row.get("communicative_act") or "")
    operation_id = str(row.get("operation_id") or "")
    content_hash = str(row.get("content_hash") or "")
    try:
        revision = int(row.get("information_revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    if not share_id:
        share_id = share_id_for(
            scope_id=scope_id,
            session_id=session_id,
            source_id=source_id,
            information_revision=revision,
            communicative_act=communicative_act,
            operation_id=operation_id,
            content_hash=content_hash,
        )
    if not share_id:
        return False
    status = str(row.get("status") or "") or SEND_ACCEPTED
    # 失败 / 无法确认的发送**不算分享**："对方没听到"不能进登记（计划 §5 A2 / X04）。
    # 库层直接挡掉，避免调用方手滑；accepted / delivered / legacy_observed 才落库。
    if status in (SEND_FAILED, SEND_UNKNOWN):
        return False
    try:
        created_at = float(row.get("created_at") or 0.0)
    except (TypeError, ValueError):
        created_at = 0.0
    sent_at = row.get("sent_at")
    try:
        sent_at = None if sent_at is None else float(sent_at)
    except (TypeError, ValueError):
        sent_at = None
    cursor = conn.execute(
        "INSERT OR IGNORE INTO shares (share_id, scope_id, source_id, information_revision,"
        " session_id, communicative_act, operation_id, content_hash, text, media_ids_json,"
        " status, platform_message_ids_json, created_at, sent_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            share_id,
            scope_id,
            source_id,
            revision,
            session_id,
            communicative_act,
            operation_id,
            content_hash,
            str(row.get("text") or ""),
            json.dumps(_json_text_list(row.get("media_ids_json")), ensure_ascii=False),
            str(row.get("status") or "") or SEND_ACCEPTED,
            json.dumps(
                _json_text_list(row.get("platform_message_ids_json")),
                ensure_ascii=False,
            ),
            created_at,
            sent_at,
        ),
    )
    conn.commit()
    return cursor.rowcount == 1


def get_share(conn: sqlite3.Connection, share_id: str) -> dict[str, Any] | None:
    """按 ``share_id`` 取一行；没有就是 ``None``。"""

    target = str(share_id or "").strip()
    if not target:
        return None
    row = conn.execute("SELECT * FROM shares WHERE share_id = ?", (target,)).fetchone()
    return _share_row(row)


def list_shares(
    conn: sqlite3.Connection,
    *,
    scope_id: str = "",
    source_id: str = "",
    session_id: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按 scope / 来源 / 会话筛，时间倒序，``limit`` 上限（默认 100）。"""

    sql = "SELECT * FROM shares"
    where: list[str] = []
    args: list[Any] = []
    if str(scope_id or ""):
        where.append("scope_id = ?")
        args.append(str(scope_id))
    if str(source_id or ""):
        where.append("source_id = ?")
        args.append(str(source_id))
    if str(session_id or ""):
        where.append("session_id = ?")
        args.append(str(session_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, share_id DESC LIMIT ?"
    try:
        count = max(1, int(limit))
    except (TypeError, ValueError):
        count = 100
    args.append(count)
    return [_share_row(row) or {} for row in conn.execute(sql, tuple(args)).fetchall()]


def count_shares(
    conn: sqlite3.Connection,
    *,
    scope_id: str = "",
    source_id: str = "",
    session_id: str = "",
) -> int:
    """同 :func:`list_shares` 的筛法，返回条数。"""

    sql = "SELECT COUNT(*) AS c FROM shares"
    where: list[str] = []
    args: list[Any] = []
    if str(scope_id or ""):
        where.append("scope_id = ?")
        args.append(str(scope_id))
    if str(source_id or ""):
        where.append("source_id = ?")
        args.append(str(source_id))
    if str(session_id or ""):
        where.append("session_id = ?")
        args.append(str(session_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = conn.execute(sql, tuple(args)).fetchone()
    return int(row[0]) if row is not None else 0


def shares_for_source(
    conn: sqlite3.Connection, source_id: str, *, revision: int | None = None
) -> list[dict[str, Any]]:
    """某一来源（``source_id``）已经分享过的记录，时间倒序。

    ``revision`` 给了就只看这一版——阶段 C 用它判断"这份信息的这一版是不是已经
    介绍过了"（X12：同一 revision 不重复介绍）。
    """

    target = str(source_id or "").strip()
    if not target:
        return []
    if revision is None:
        rows = conn.execute(
            "SELECT * FROM shares WHERE source_id = ?"
            " ORDER BY created_at DESC, share_id DESC",
            (target,),
        ).fetchall()
    else:
        try:
            number = int(revision)
        except (TypeError, ValueError):
            return []
        rows = conn.execute(
            "SELECT * FROM shares WHERE source_id = ? AND information_revision = ?"
            " ORDER BY created_at DESC, share_id DESC",
            (target, number),
        ).fetchall()
    return [_share_row(row) or {} for row in rows]


def mark_share_sent(
    conn: sqlite3.Connection,
    share_id: str,
    *,
    status: str = "",
    platform_message_ids: Any = (),
    sent_at: float | None = None,
) -> bool:
    """补一次分享的送达事实（``accepted`` → ``delivered`` + 平台消息 id）。

    只改 ``status`` / ``platform_message_ids`` / ``sent_at``，**绝不**动
    ``source_id`` / ``information_revision`` / ``content_hash``（"说过什么"是事实，
    回执只是补充）。True = 真的改到了行。
    """

    target = str(share_id or "").strip()
    if not target:
        return False
    sets: list[str] = []
    args: list[Any] = []
    if str(status or ""):
        sets.append("status = ?")
        args.append(str(status))
    ids = _json_text_list(platform_message_ids)
    if ids:
        sets.append("platform_message_ids_json = ?")
        args.append(json.dumps(ids, ensure_ascii=False))
    if sent_at is not None:
        try:
            sets.append("sent_at = ?")
            args.append(float(sent_at))
        except (TypeError, ValueError):
            pass
    if not sets:
        return False
    args.append(target)
    cursor = conn.execute(
        f"UPDATE shares SET {', '.join(sets)} WHERE share_id = ?", tuple(args)
    )
    conn.commit()
    return cursor.rowcount == 1


def ensure_shares(conn: sqlite3.Connection) -> None:
    """建 ``shares`` 与索引（幂等）。"""

    conn.executescript(_SHARES_DDL)
    conn.commit()


def _migrate_shares(conn: sqlite3.Connection) -> None:
    """建 ``shares``（可重复调用；失败整体回滚后把异常抛出去）。"""

    try:
        conn.executescript(_SHARES_DDL)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# 阶段C 追加分区：谈资 / 续说租约（topics / share_claims）
#
# 设计见计划 §4.3 / §7（C1 谈资、C2 曝光与续说去重）：只增表 / 增索引 / 增函数，
# 不动上面任何既有表与既有函数。两张表都只是 ``core/experience.py`` 纯逻辑的落库
# 载体，**列名一律取自** ``Topic.to_row()`` / ``ShareClaim.to_row()``，不另起名字；
# 读出来的行直接喂 ``Topic.from_row`` / ``ShareClaim.from_row`` 就能还原。
#
# - ``topics``：一行 = 一条可以主动提起的谈资。``version`` 是乐观锁（写回时比对），
#   ``status=deleted`` 是软删除（行还在，"别再用了"）。
# - ``share_claims``：一行 = 一个续说 key（``source_id + revision + session_id``）
#   的抢锁记录，状态在行内流转（active → done / released / expired）。
#   **同一 key 至多一个活跃 claim**，由部分唯一索引在数据库层兜住并发。
#
# 幂等：``_migrate_topics`` 由 ``Database._migrate()`` 调用，空库 / 旧库 / 已迁移库
# 三种起点都能重复跑，重复执行不报错、不丢数据、版本号不回退；建表在**一个事务**
# 里完成（见 :func:`_run_ddl`），失败整体回滚后把异常抛出去，绝不留下半张表。
# ---------------------------------------------------------------------------


# 注：索引只建在真实存在的列上。``topics`` 没有 ``session_id`` / ``source_id`` 列
# （列名必须等于 ``Topic.to_row()`` 的键，Topic 上只有 scope_id / episode_id，
# 来源是一串 source_event_ids），所以"会话 + 状态"落在 ``scope_id, status`` 上，
# "来源 + 版本"落在 ``share_claims`` 上（那边才有 source_id / revision）。会话维度
# 的兜底规则见 :func:`_topic_filters`。
_TOPICS_DDL = """
CREATE TABLE IF NOT EXISTS topics (
    topic_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT '',
    episode_id TEXT NOT NULL DEFAULT '',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    information_revision INTEGER NOT NULL DEFAULT 1,
    summary TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    source_kind TEXT NOT NULL DEFAULT 'chat',
    status TEXT NOT NULL DEFAULT 'available',
    pinned INTEGER NOT NULL DEFAULT 0,
    expires_at REAL,
    importance REAL NOT NULL DEFAULT 0,
    user_edited INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 1
);
-- 按范围取 / 按范围 + 状态取（会话维度归一到 scope_id，见上面的注）
CREATE INDEX IF NOT EXISTS idx_topics_scope_status
    ON topics (scope_id, status);
-- 按范围 + 信息版本取（"这份信息的第几版落库了"）
CREATE INDEX IF NOT EXISTS idx_topics_scope_revision
    ON topics (scope_id, information_revision);
-- 分页/快照的稳定排序键（与 experience._sort_key 同序：updated_at, topic_id）
CREATE INDEX IF NOT EXISTS idx_topics_page
    ON topics (updated_at DESC, topic_id DESC);
-- 按段落回查（episode_id 是可选的经历分段标记）
CREATE INDEX IF NOT EXISTS idx_topics_episode
    ON topics (episode_id);
"""


_SHARE_CLAIMS_DDL = f"""
CREATE TABLE IF NOT EXISTS share_claims (
    claim_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL DEFAULT '',
    intent_class TEXT NOT NULL DEFAULT '',
    owner_operation_id TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
);
-- 并发抢锁的兜底：同一 (source_id, revision, session_id) 至多一个活跃 claim。
-- 部分唯一索引只约束 status='active' 的行，done / released / expired 的历史行可以并存。
CREATE UNIQUE INDEX IF NOT EXISTS idx_share_claims_active
    ON share_claims (source_id, revision, session_id)
    WHERE status = '{CLAIM_ACTIVE}';
CREATE INDEX IF NOT EXISTS idx_share_claims_source
    ON share_claims (source_id, revision);
CREATE INDEX IF NOT EXISTS idx_share_claims_session
    ON share_claims (session_id, status);
-- 过期清理扫的就是"还是活跃 + 租约已到期"这一片
CREATE INDEX IF NOT EXISTS idx_share_claims_lease
    ON share_claims (status, lease_until);
"""


def _run_ddl(conn: sqlite3.Connection, script: str) -> None:
    """把一个 DDL 脚本在**一个事务**里跑完（失败整体回滚后抛出，不留半张表）。

    坑：``executescript`` 会先把调用方挂着的事务隐式 COMMIT 掉，所以事务边界必须
    写在**脚本里面**（``BEGIN; ... COMMIT;``），在外面 ``conn.execute("BEGIN")``
    再调 ``executescript`` 是没用的——中间失败会留下建了一半的表。
    """

    try:
        conn.executescript(f"BEGIN;\n{script}\nCOMMIT;")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def _int_or(value: Any, default: int) -> int:
    """整数列容错：``int()`` 不了就用默认值（坏值不抛）。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_or(value: Any, default: float) -> float:
    """浮点列容错：``float()`` 不了就用默认值。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_object_text(value: Any, default: str = "{}") -> str:
    """JSON 对象列 → 合法 JSON 文本（坏 JSON / None 降级 default，不抛）。

    ``Topic.to_row()`` 写 ``details_json`` 时是把 ``details`` 和 ``corrections``
    包成一个对象再序列化，这里只保证落库的是合法 JSON，**不改结构**。
    """

    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            json.loads(value)
        except (TypeError, ValueError):
            return default
        return value
    if value is None:
        return default
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return default


def _as_topic_row(topic: Any) -> dict[str, Any] | None:
    """任意行（:class:`Topic` / dict / ``sqlite3.Row``）→ ``to_row()`` 形状的 dict。

    认不出来的（None、字符串、结构不对的东西）给 ``None``，调用方据此返回 False：
    **不编一条空谈资塞进库里**（同 ``record_share`` 不编假 share_id 的口径）。
    """

    try:
        if hasattr(topic, "to_row"):
            return dict(topic.to_row())
        return dict(Topic.from_row(topic).to_row())
    except (TypeError, ValueError, AttributeError, KeyError, IndexError):
        return None


def _as_claim_row(claim: Any) -> dict[str, Any] | None:
    """同 :func:`_as_topic_row`，换成 :class:`ShareClaim`。"""

    try:
        if hasattr(claim, "to_row"):
            return dict(claim.to_row())
        return dict(ShareClaim.from_row(claim).to_row())
    except (TypeError, ValueError, AttributeError, KeyError, IndexError):
        return None


def _topic_row(row: Any) -> dict[str, Any] | None:
    """``topics`` 一行 → dict（键与 :meth:`Topic.to_row` 一致，JSON 列保持文本）。

    JSON 列**原样**给字符串：返回的 dict 直接喂 ``Topic.from_row`` 就能还原
    ``details`` / ``corrections`` / ``source_event_ids``，中间不再解一次再拼一次。
    """

    if row is None:
        return None
    item = dict(row)
    for key in (
        "topic_id",
        "scope_id",
        "episode_id",
        "source_event_ids_json",
        "summary",
        "details_json",
        "source_kind",
        "status",
    ):
        item[key] = str(item.get(key) or "")
    for key, default in (
        ("information_revision", 1),
        ("pinned", 0),
        ("user_edited", 0),
        ("version", 1),
        ("schema_version", EXPERIENCE_SCHEMA_VERSION),
    ):
        item[key] = _int_or(item.get(key), default)
    for key in ("importance", "created_at", "updated_at"):
        item[key] = _float_or(item.get(key), 0.0)
    expires = item.get("expires_at")
    item["expires_at"] = None if expires is None else _float_or(expires, 0.0)
    return item


def _claim_row(row: Any) -> dict[str, Any] | None:
    """``share_claims`` 一行 → dict（键与 :meth:`ShareClaim.to_row` 一致）。"""

    if row is None:
        return None
    item = dict(row)
    for key in (
        "claim_id",
        "source_id",
        "session_id",
        "intent_class",
        "owner_operation_id",
        "status",
    ):
        item[key] = str(item.get(key) or "")
    item["revision"] = _int_or(item.get("revision"), 0)
    item["lease_until"] = _float_or(item.get("lease_until"), 0.0)
    return item


def ensure_topics(conn: sqlite3.Connection) -> None:
    """建 ``topics`` / ``share_claims`` 与索引（幂等，一个事务）。"""

    _run_ddl(conn, _TOPICS_DDL + _SHARE_CLAIMS_DDL)


def _migrate_topics(conn: sqlite3.Connection) -> None:
    """建 ``topics`` / ``share_claims`` 并把 ``schema_meta.schema_version`` 推到阶段C。

    可重复调用：空库、没有这两张表的旧库、已迁移过的库都能跑，旧数据一行不动。
    版本号**只增不减**（已经不小于 :data:`EXPERIENCE_SCHEMA_VERSION` 时一个字节
    都不写）；失败整体回滚（旧库保持可用）后把异常抛出去，不静默降级。
    """

    _run_ddl(conn, _TOPICS_DDL + _SHARE_CLAIMS_DDL + _SCHEMA_META_DDL)
    if get_schema_version(conn) < EXPERIENCE_SCHEMA_VERSION:
        set_schema_version(conn, EXPERIENCE_SCHEMA_VERSION)


def record_topic(conn: sqlite3.Connection, topic: Any) -> bool:
    """登记一条谈资（``INSERT OR IGNORE``）。

    **True = 这次真的插进去了**；False = 同一个 ``topic_id`` 已经登记过，或者
    ``topic_id`` 为空 / 这行认不出来 / ``status`` 不在
    :data:`core.experience.TOPIC_STATUSES` 里 / ``source_kind`` 不在
    :data:`core.experience.SOURCE_KINDS` 里（认不出就不落库，不编一条空谈资）。

    传进来的可以是 :class:`core.experience.Topic`，也可以是任何能被
    ``Topic.from_row`` 吃下的行（``get_topic`` 的返回值 / dict / ``sqlite3.Row``），
    所以"读出来改一改再写回去"不用手工拼列。
    """

    row = _as_topic_row(topic)
    if row is None:
        return False
    topic_id = str(row.get("topic_id") or "").strip()
    if not topic_id:
        return False
    status = str(row.get("status") or "") or TOPIC_AVAILABLE
    if status not in TOPIC_STATUSES:
        return False
    source_kind = str(row.get("source_kind") or "") or SOURCE_KIND_CHAT
    if source_kind not in SOURCE_KINDS:
        return False
    expires = row.get("expires_at")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO topics (topic_id, scope_id, episode_id,"
        " source_event_ids_json, information_revision, summary, details_json, source_kind,"
        " status, pinned, expires_at, importance, user_edited, version, created_at,"
        " updated_at, schema_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            topic_id,
            str(row.get("scope_id") or ""),
            str(row.get("episode_id") or ""),
            json.dumps(
                _json_text_list(row.get("source_event_ids_json")), ensure_ascii=False
            ),
            _int_or(row.get("information_revision"), 1),
            str(row.get("summary") or ""),
            _json_object_text(row.get("details_json")),
            source_kind,
            status,
            1 if _int_or(row.get("pinned"), 0) else 0,
            None if expires is None else _float_or(expires, 0.0),
            _float_or(row.get("importance"), 0.0),
            1 if _int_or(row.get("user_edited"), 0) else 0,
            _int_or(row.get("version"), 1),
            _float_or(row.get("created_at"), 0.0),
            _float_or(row.get("updated_at"), 0.0),
            _int_or(row.get("schema_version"), EXPERIENCE_SCHEMA_VERSION),
        ),
    )
    conn.commit()
    return int(cursor.rowcount or 0) > 0


def get_topic(conn: sqlite3.Connection, topic_id: str) -> dict[str, Any] | None:
    """按 ``topic_id`` 取一行；没有就是 ``None``。

    软删除的行**照样取得到**（行还在），要不要用看 ``status``——默认查询会把它
    藏起来，这里不藏，审计和恢复都要靠它。
    """

    target = str(topic_id or "").strip()
    if not target:
        return None
    row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (target,)).fetchone()
    return _topic_row(row)


def _topic_filters(
    *, session_id: str = "", scope_id: str = "", status: str = "", source_kind: str = ""
) -> tuple[list[str], list[Any]]:
    """``list_topics`` / ``count_topics`` 共用的 WHERE 片段。

    坑：``topics`` 表**没有 session_id 列**（列名必须等于 ``Topic.to_row()`` 的键，
    而 ``Topic`` 上只有 ``scope_id`` / ``episode_id``），会话维度落在 ``shares`` /
    ``share_claims`` 上。engine 的 ``scope_id(session_id)`` 在没分组时就等于
    ``session_id``，所以这里把 ``session_id`` 当**范围兜底**：只有没给 ``scope_id``
    时才拿它去比 ``scope_id``（``scope_id`` 优先）。分了组的会话要按组查，得由调用方
    自己换算成 ``scope_id``——库层没有分组表，不猜。

    ``status`` 不给（空串）时**排除软删除的行**；要看被删的，显式传 ``deleted``。
    """

    where: list[str] = []
    args: list[Any] = []
    target_scope = str(scope_id or "")
    target_session = str(session_id or "")
    if target_scope:
        where.append("scope_id = ?")
        args.append(target_scope)
    elif target_session:
        where.append("scope_id = ?")
        args.append(target_session)
    wanted = str(status or "")
    if wanted:
        where.append("status = ?")
        args.append(wanted)
    else:
        where.append("status != ?")
        args.append(TOPIC_DELETED)
    if source_kind:
        # Topic's persisted primary classification, not UI labels or event_kind.
        where.append('source_kind = ?')
        args.append(str(source_kind))
    return where, args


def list_topics(
    conn: sqlite3.Connection,
    *,
    session_id: str = "",
    scope_id: str = "",
    status: str = "",
    cursor: str = "",
    now: float = 0.0,
    limit: int = 100,
    source_kind: str = "",
) -> list[dict[str, Any]]:
    """按范围 / 状态筛谈资，``updated_at DESC, topic_id DESC``（与分页游标同序）。

    默认**不返回软删除**（``status=deleted``）的行；``limit`` 至少 1，默认 100。
    筛法与坑见 :func:`_topic_filters`。

    ``cursor`` 是上一页最后一条的**位置**（见 :func:`encode_topic_cursor`）：给了它
    就只查严格排在它后面的行——SQL 层下推，不再"把前 1000 条读进内存再切片"
    （清单 §2.5 / 计划 §4）。``now`` 大于 0 时额外跳过**已经到有效期**的非置顶行，
    这样过期的谈资连"读出来"这一步都到不了，不靠调用方自觉。

    排序键是 ``(updated_at, topic_id)`` 双键倒序，所以游标必须带两个值：只带
    ``topic_id`` 会在"同一秒写过很多条"时跳行或重复（见 :func:`_topic_cursor_clause`）。
    """

    where, args = _topic_filters(
        session_id=session_id, scope_id=scope_id, status=status, source_kind=source_kind
    )
    stamp = 0.0
    try:
        stamp = float(now or 0.0)
    except (TypeError, ValueError):
        stamp = 0.0
    if stamp > 0:
        where.append("(pinned != 0 OR expires_at IS NULL OR expires_at > ?)")
        args.append(stamp)
    clause, cursor_args = _topic_cursor_clause(cursor)
    if clause:
        where.append(clause)
        args.extend(cursor_args)
    sql = "SELECT * FROM topics WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, topic_id DESC LIMIT ?"
    try:
        count = max(1, int(limit))
    except (TypeError, ValueError):
        count = 100
    args.append(count)
    return [_topic_row(row) or {} for row in conn.execute(sql, tuple(args)).fetchall()]


def encode_topic_cursor(row: Any) -> str:
    """一行谈资 → 分页游标（``"<updated_at>|<topic_id>"``，与 ``list_topics`` 同序）。

    ``updated_at`` 用 ``repr(float)`` 存：它必须能**逐字**还原成同一个浮点数，
    否则"小于游标"的比较会在边界上跳行或重复。
    """

    updated = _float_or(_cell_of(row, "updated_at", 0.0), 0.0)
    topic_id = str(_cell_of(row, "topic_id", "") or "")
    if not topic_id:
        return ""
    return f"{updated!r}|{topic_id}"


def _topic_cursor_clause(cursor: str) -> tuple[str, list[Any]]:
    """游标 → SQL 条件（只认自己写的形状；认不出的游标当"没有游标"）。"""

    raw = str(cursor or "").strip()
    if not raw:
        return "", []
    head, _, tail = raw.partition("|")
    if not tail:
        # 只有 id 的旧形状：退化成"id 排在它后面"，不猜时间
        return "topic_id < ?", [head]
    try:
        updated = float(head)
    except (TypeError, ValueError):
        return "topic_id < ?", [tail]
    return "(updated_at < ? OR (updated_at = ? AND topic_id < ?))", [updated, updated, tail]


def list_visible_topics(
    conn: sqlite3.Connection,
    *,
    session_id: str = "",
    scope_id: str = "",
    status: str = "",
    now: float = 0.0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """:func:`list_topics` 的"只要能用的"版本：**连 expired 的都不返回**。

    主动候选是"现在要拿去说的话"，所以它要比页面列表更严：过期的、已经软删的
    都不该进候选池（清单 §2.4）。状态列写死的 ``!= deleted`` 与 ``!= expired``
    是为了让"忘了读 expires_at"也不可能把过期素材推出去。
    """

    stamp = 0.0
    try:
        stamp = float(now or 0.0)
    except (TypeError, ValueError):
        stamp = 0.0
    where, args = _topic_filters(
        session_id=session_id, scope_id=scope_id, status=status or TOPIC_AVAILABLE
    )
    if stamp > 0:
        where.append("(pinned != 0 OR expires_at IS NULL OR expires_at > ?)")
        args.append(stamp)
    sql = "SELECT * FROM topics WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, topic_id DESC LIMIT ?"
    try:
        count = max(1, int(limit))
    except (TypeError, ValueError):
        count = 100
    args.append(count)
    return [_topic_row(row) or {} for row in conn.execute(sql, tuple(args)).fetchall()]


def count_topics(
    conn: sqlite3.Connection,
    *,
    session_id: str = "",
    scope_id: str = "",
    status: str = "",
    source_kind: str = "",
    now: float = 0.0,
) -> int:
    """同 :func:`list_topics` 的筛法（不含 ``limit``），返回条数。"""

    where, args = _topic_filters(
        session_id=session_id, scope_id=scope_id, status=status, source_kind=source_kind
    )
    if float(now) > 0:
        where.append('(pinned != 0 OR expires_at IS NULL OR expires_at > ?)')
        args.append(float(now))
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM topics WHERE " + " AND ".join(where), tuple(args)
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def expire_topics_due(conn: sqlite3.Connection, *, now: float) -> int:
    """把到期谈资批量转 ``expired``，返回条数（清单 §2.4 的"按时转状态"）。

    权威判定放在 SQL 里，而不是只在内存池上过一遍：池子只装了"前 1000 条"那个
    快照，快照之后新建的谈资它根本看不见，靠它过期会**漏掉**——而漏掉的恰恰是
    最该按时清掉的那几条（刚登记的）。所以 tick 调的是这个函数。

    只动 ``available`` 且 ``expires_at <= now`` 的行；``pinned`` 豁免（"固定只
    影响自然过期"，与 ``TopicPool.expire_due`` 同口径）。``version`` 跟着 +1，
    这样页面上"刚过了一会儿"看得出版本变了。
    """

    stamp = 0.0
    try:
        stamp = float(now or 0.0)
    except (TypeError, ValueError):
        stamp = 0.0
    if stamp <= 0:
        return 0
    cursor = conn.execute(
        "UPDATE topics SET status = ?, version = version + 1 "
        "WHERE status = ? AND pinned = 0 AND expires_at IS NOT NULL AND expires_at <= ?",
        (TOPIC_EXPIRED, TOPIC_AVAILABLE, stamp),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def update_topic(
    conn: sqlite3.Connection, topic: Any, *, expected_version: int | None = None
) -> bool:
    """整行覆盖写一条谈资，带**乐观锁**。

    - ``expected_version`` 给了就只更新 ``version`` 正好等于它的那一行
      （``WHERE topic_id = ? AND version = ?``）：**版本冲突返回 False，库里的行
      一个字段都不动**，调用方重新读一次再改。
    - 不论给不给 ``expected_version``，``version`` 都**不许回退**
      （``WHERE version <= 新 version``）：拿旧快照去覆盖新版本一律 False。
    - 行不存在 / ``topic_id`` 为空 / 认不出的输入 / 状态或来源类型不在词表里，
      也是 False（同 :func:`record_topic` 的"认不出就不写"口径）。

    整行覆盖是为了让"读出来 → 改 → 写回"这条路上没有半截更新；要改单个字段就
    拿 ``get_topic`` 的行改完再传回来。
    """

    row = _as_topic_row(topic)
    if row is None:
        return False
    topic_id = str(row.get("topic_id") or "").strip()
    if not topic_id:
        return False
    status = str(row.get("status") or "") or TOPIC_AVAILABLE
    if status not in TOPIC_STATUSES:
        return False
    source_kind = str(row.get("source_kind") or "") or SOURCE_KIND_CHAT
    if source_kind not in SOURCE_KINDS:
        return False
    version = _int_or(row.get("version"), 1)
    if expected_version is not None and version < _int_or(expected_version, 0):
        return False
    expires = row.get("expires_at")
    sql = (
        "UPDATE topics SET scope_id = ?, episode_id = ?, source_event_ids_json = ?,"
        " information_revision = ?, summary = ?, details_json = ?, source_kind = ?,"
        " status = ?, pinned = ?, expires_at = ?, importance = ?, user_edited = ?,"
        " version = ?, created_at = ?, updated_at = ?, schema_version = ?"
        " WHERE topic_id = ? AND version <= ?"
    )
    args: list[Any] = [
        str(row.get("scope_id") or ""),
        str(row.get("episode_id") or ""),
        json.dumps(
            _json_text_list(row.get("source_event_ids_json")), ensure_ascii=False
        ),
        _int_or(row.get("information_revision"), 1),
        str(row.get("summary") or ""),
        _json_object_text(row.get("details_json")),
        source_kind,
        status,
        1 if _int_or(row.get("pinned"), 0) else 0,
        None if expires is None else _float_or(expires, 0.0),
        _float_or(row.get("importance"), 0.0),
        1 if _int_or(row.get("user_edited"), 0) else 0,
        version,
        _float_or(row.get("created_at"), 0.0),
        _float_or(row.get("updated_at"), 0.0),
        _int_or(row.get("schema_version"), EXPERIENCE_SCHEMA_VERSION),
        topic_id,
        version,
    ]
    if expected_version is not None:
        sql += " AND version = ?"
        args.append(_int_or(expected_version, -1))
    cursor = conn.execute(sql, tuple(args))
    conn.commit()
    return int(cursor.rowcount or 0) > 0


def delete_topic(conn: sqlite3.Connection, topic_id: str, *, soft: bool = True) -> bool:
    """删一条谈资；``soft=True``（默认）是**软删除**。

    软删除只把 ``status`` 改成 ``deleted`` 并把 ``version`` +1（手上还拿着旧版本的
    写入者立刻变成版本冲突），行本身留着，默认查询不再返回它；已经删过的再删一次
    返回 False。``soft=False`` 才是真 DELETE。

    库里不取时钟，所以软删除**不动** ``updated_at``；要留删除时间由调用方改完
    （或先用 :func:`update_topic` 写一版）再说。
    """

    target = str(topic_id or "").strip()
    if not target:
        return False
    if soft:
        cursor = conn.execute(
            "UPDATE topics SET status = ?, version = version + 1"
            " WHERE topic_id = ? AND status != ?",
            (TOPIC_DELETED, target, TOPIC_DELETED),
        )
    else:
        cursor = conn.execute("DELETE FROM topics WHERE topic_id = ?", (target,))
    conn.commit()
    return int(cursor.rowcount or 0) > 0


def record_claim(conn: sqlite3.Connection, claim: Any) -> bool:
    """抢一个续说 key（``INSERT OR IGNORE``）。

    **True = 真的抢到了**；False = 这个 ``(source_id, revision, session_id)`` 已经有
    活跃 claim（部分唯一索引 ``idx_share_claims_active`` 拦下，并发下也只有一个赢家；
    失败**不抛**，抢不到就是抢不到）、``claim_id`` 推不出来（来源和会话都空，不编假
    key）、状态不在 :data:`core.experience.CLAIM_STATUSES` 里，或 ``claim_id`` 已存在。

    ``claim_id`` 为空时按 :func:`core.experience.claim_id_for` 从
    ``(source_id, revision, session_id)`` 现算——与纯逻辑
    ``FollowupCoordinator.claim_id()`` 是同一个 key，先抢后落库不会错位。
    """

    row = _as_claim_row(claim)
    if row is None:
        return False
    source_id = str(row.get("source_id") or "")
    session_id = str(row.get("session_id") or "")
    revision = _int_or(row.get("revision"), 0)
    claim_id = str(row.get("claim_id") or "").strip()
    if not claim_id:
        if not source_id.strip() and not session_id.strip():
            return False
        claim_id = claim_id_for(
            source_id=source_id.strip(),
            revision=revision,
            session_id=session_id.strip(),
        )
    status = str(row.get("status") or "") or CLAIM_ACTIVE
    if status not in CLAIM_STATUSES:
        return False
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO share_claims (claim_id, source_id, revision,"
            " session_id, intent_class, owner_operation_id, lease_until, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim_id,
                source_id,
                revision,
                session_id,
                str(row.get("intent_class") or ""),
                str(row.get("owner_operation_id") or ""),
                _float_or(row.get("lease_until"), 0.0),
                status,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:  # pragma: no cover - OR IGNORE 已经先兜一层
        conn.rollback()
        return False
    return int(cursor.rowcount or 0) > 0


def get_claim(conn: sqlite3.Connection, claim_id: str) -> dict[str, Any] | None:
    """按 ``claim_id`` 取一行；没有就是 ``None``（历史状态照样取得到）。"""

    target = str(claim_id or "").strip()
    if not target:
        return None
    row = conn.execute(
        "SELECT * FROM share_claims WHERE claim_id = ?", (target,)
    ).fetchone()
    return _claim_row(row)


def list_claims(
    conn: sqlite3.Connection,
    *,
    source_id: str = "",
    session_id: str = "",
    status: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按来源 / 会话 / 状态筛续说 claim，``limit`` 默认 100。

    表里没有创建时间列，排序用 ``lease_until DESC, claim_id DESC``（租约最晚的在
    最前，同值按 id 稳定）；``status`` 空串表示不筛状态（**活跃和历史都返回**，
    要看"谁抢到了"就传 ``active``）。
    """

    sql = "SELECT * FROM share_claims"
    where: list[str] = []
    args: list[Any] = []
    if str(source_id or ""):
        where.append("source_id = ?")
        args.append(str(source_id))
    if str(session_id or ""):
        where.append("session_id = ?")
        args.append(str(session_id))
    if str(status or ""):
        where.append("status = ?")
        args.append(str(status))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY lease_until DESC, claim_id DESC LIMIT ?"
    try:
        count = max(1, int(limit))
    except (TypeError, ValueError):
        count = 100
    args.append(count)
    return [_claim_row(row) or {} for row in conn.execute(sql, tuple(args)).fetchall()]


def update_claim(conn: sqlite3.Connection, claim: Any) -> bool:
    """整行覆盖写一个 claim（状态在行内流转：``active`` → ``done`` / ``released`` / ``expired``）。

    False 四种情况：``claim_id`` 为空、找不到那一行、状态不在
    :data:`core.experience.CLAIM_STATUSES` 里，以及**改完会撞上"同一 key 至多一个
    活跃"的部分唯一索引**（``sqlite3.IntegrityError`` 捕获后回滚，绝不静默把别人的
    活跃 claim 挤掉）。
    """

    row = _as_claim_row(claim)
    if row is None:
        return False
    claim_id = str(row.get("claim_id") or "").strip()
    if not claim_id:
        return False
    status = str(row.get("status") or "") or CLAIM_ACTIVE
    if status not in CLAIM_STATUSES:
        return False
    try:
        cursor = conn.execute(
            "UPDATE share_claims SET source_id = ?, revision = ?, session_id = ?,"
            " intent_class = ?, owner_operation_id = ?, lease_until = ?, status = ?"
            " WHERE claim_id = ?",
            (
                str(row.get("source_id") or ""),
                _int_or(row.get("revision"), 0),
                str(row.get("session_id") or ""),
                str(row.get("intent_class") or ""),
                str(row.get("owner_operation_id") or ""),
                _float_or(row.get("lease_until"), 0.0),
                status,
                claim_id,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return int(cursor.rowcount or 0) > 0


def expire_claims(
    conn: sqlite3.Connection, *, now: float, ttl: float | None = None
) -> int:
    """把过期的活跃 claim 标成 ``expired``，返回本次改到几条。

    判定口径与纯逻辑 ``FollowupCoordinator.expire_due`` 一致：还是 ``active`` **且**
    ``lease_until <= now`` 才算过期——活着的租约绝不动。``now`` 必须由调用方显式传
    （库里不取时钟）；``now`` 不是数字时返回 0，什么都不动。

    ``ttl`` 与 ``FollowupCoordinator.claim`` 里的 ttl 同义（**租期**），只对
    ``lease_until`` 没写过（``<= 0``）的行生效：给了正数表示这些行按"从 ``now`` 起
    还有 ttl 秒租期"算，**这次不清**；不给就按上面的口径一起清掉。
    """

    try:
        stamp = float(now)
    except (TypeError, ValueError):
        return 0
    sql = "UPDATE share_claims SET status = ? WHERE status = ? AND lease_until <= ?"
    args: list[Any] = [CLAIM_EXPIRED, CLAIM_ACTIVE, stamp]
    grace = None if ttl is None else _float_or(ttl, 0.0)
    if grace is not None and grace > 0:
        sql += " AND lease_until > 0"
    cursor = conn.execute(sql, tuple(args))
    conn.commit()
    return int(cursor.rowcount or 0)


# A2/A4 exact storage. Legacy inventory APIs remain available; these APIs never
# interpret a database failure as an empty inventory or a send failure as silence.
TOPICS_STORAGE_VERSION = 3
_DECISION_STATUSES = frozenset({'pending', 'decided_silent', 'ready_to_send',
                              'partially_sent', 'sent', 'failed', 'cancelled'})
_PROCESSED_STATUSES = frozenset({'decided_silent', 'sent', 'cancelled'})
# 「沉默」不等于「办完了」：``decided_silent`` 只说明那一拍她没接话，不是这件事有了结论。
# 同一条消息被平台重投、或同一批消息在用户眼里"还没被回"时，只要沉默终态落库已超过这个
# 窗口，就允许新批次接管重做（真机 2026-10-23 15:37：重投撞上沉默终态 → 她一直没回）。
# 窗口内仍然保持安静——避免同一批消息在一次生成还没跑完时被重复接手。
DECISION_SILENT_RECLAIM_SECONDS = 30.0
_RECEIPT_STATUSES = frozenset({'generated', 'accepted', 'delivered', 'failed', 'unknown', 'cancelled'})
_TOPICS_STORAGE_DDL = """
CREATE INDEX IF NOT EXISTS idx_topics_scope_page ON topics(scope_id, status, updated_at DESC, topic_id DESC);
CREATE TABLE IF NOT EXISTS decision_batches (
 decision_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, session_id TEXT NOT NULL,
 config_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT NOT NULL DEFAULT '',
 side_effects_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS decision_inputs (
 scope_id TEXT NOT NULL, session_id TEXT NOT NULL, event_id TEXT NOT NULL,
 event_seq INTEGER NOT NULL, decision_id TEXT NOT NULL,
 PRIMARY KEY(scope_id,session_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_decision_inputs_batch ON decision_inputs(decision_id,event_seq);
CREATE INDEX IF NOT EXISTS idx_source_scope_session_seq ON source_events(scope_id,origin_session_id);
CREATE TABLE IF NOT EXISTS processed_cursors (
 scope_id TEXT NOT NULL, session_id TEXT NOT NULL, event_seq INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(scope_id,session_id)
);
CREATE TABLE IF NOT EXISTS outbox_segments (
 segment_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, target_session_id TEXT NOT NULL,
 text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'generated', receipt_id TEXT NOT NULL DEFAULT '',
 platform_message_ids_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
 updated_at REAL NOT NULL, error TEXT NOT NULL DEFAULT '', attempt_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_decision ON outbox_segments(decision_id,status);
CREATE TABLE IF NOT EXISTS outbox_topic_mappings (
 segment_id TEXT NOT NULL, topic_id TEXT NOT NULL, information_revision INTEGER NOT NULL,
 policy_revision INTEGER NOT NULL, source_ids_json TEXT NOT NULL,
 PRIMARY KEY(segment_id,topic_id)
);
CREATE TABLE IF NOT EXISTS plan_execution_cancellations (
 execution_id TEXT PRIMARY KEY, association_json TEXT NOT NULL, cancelled_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS segment_exposure_finalizations (
 segment_id TEXT PRIMARY KEY, communicative_act TEXT NOT NULL, finalized_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_claims (
 scope_id TEXT NOT NULL, source_id TEXT NOT NULL, revision INTEGER NOT NULL,
 session_id TEXT NOT NULL, usage_family TEXT NOT NULL, owner_operation_id TEXT NOT NULL,
 lease_until REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
 PRIMARY KEY(scope_id,source_id,revision,session_id,usage_family)
);
"""


@contextlib.contextmanager
def _storage_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Works with both raw connections and Database.transaction's commit proxy."""
    owned = not conn.in_transaction
    if owned:
        conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
    except BaseException:
        if owned:
            conn.rollback()
        raise
    else:
        if owned:
            conn.commit()


def _migrate_topics_storage(conn: sqlite3.Connection) -> None:
    # A separate meta key avoids breaking existing domain schema versions.
    with _storage_transaction(conn):
        columns = {str(row[1]) for row in conn.execute('PRAGMA table_info(source_events)')}
        if 'policy_revision' not in columns:
            conn.execute('ALTER TABLE source_events ADD COLUMN policy_revision INTEGER NOT NULL DEFAULT 1')
        for statement in _TOPICS_STORAGE_DDL.split(';'):
            if statement.strip():
                conn.execute(statement)
        # T0-b：孤儿决策回收按 (created_at, status) 找候选。老库在这里补建，
        # ``IF NOT EXISTS`` 保证空库 / 旧库 / 已迁移库重复跑都不报错。
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_decision_batches_created'
            ' ON decision_batches(created_at, status)'
        )
        batch_columns = {str(row[1]) for row in conn.execute('PRAGMA table_info(decision_batches)')}
        if 'outbox_sealed' not in batch_columns:
            conn.execute('ALTER TABLE decision_batches ADD COLUMN outbox_sealed INTEGER NOT NULL DEFAULT 0')
            # Historical completed work remains completed; pending legacy work
            # must explicitly finish its producer, never guess completion.
            conn.execute("UPDATE decision_batches SET outbox_sealed=1 WHERE status='sent'")
        if 'released_inputs_json' not in batch_columns:
            conn.execute("ALTER TABLE decision_batches ADD COLUMN released_inputs_json TEXT NOT NULL DEFAULT '[]'")
        segment_columns = {str(row[1]) for row in conn.execute('PRAGMA table_info(outbox_segments)')}
        if 'communicative_act' not in segment_columns:
            conn.execute("ALTER TABLE outbox_segments ADD COLUMN communicative_act TEXT NOT NULL DEFAULT 'reference'")
            conn.execute('UPDATE outbox_segments SET communicative_act=(SELECT communicative_act FROM segment_exposure_finalizations f '
                         'WHERE f.segment_id=outbox_segments.segment_id) WHERE EXISTS '
                         '(SELECT 1 FROM segment_exposure_finalizations f WHERE f.segment_id=outbox_segments.segment_id)')
        conn.execute("INSERT INTO schema_meta(key,value) VALUES('topics_storage_version',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER),excluded.value)",
                     (TOPICS_STORAGE_VERSION,))


def _exact_ids(values: Iterable[str]) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _id_chunks(values: Iterable[str]) -> Iterator[list[str]]:
    ids = _exact_ids(values)
    for start in range(0, len(ids), 400):
        yield ids[start:start + 400]


def query_topics_metadata(conn: sqlite3.Connection, *, scope_id: str,
                          query_terms: Iterable[str], now: float, status: str = 'available',
                          source_kind: str = '', limit: int = 200,
                          max_metadata_rows: int = 20000, timeout_seconds: float = 0.25) -> dict[str, Any]:
    """Bounded literal lexical shortlist, never an authorization or semantic rank.

    Owns a short deferred read transaction and progress handler. Raw-connection
    callers must not concurrently use the connection or install another handler.
    Database's wrapper serializes access. SQL failures propagate; only our own
    deadline interruption returns partial metadata from completed pages.
    """
    import heapq
    import math
    if not isinstance(scope_id, str) or not isinstance(status, str) or not isinstance(source_kind, str):
        raise ValueError('metadata filters must be strings')
    if isinstance(now, bool) or isinstance(timeout_seconds, bool):
        raise ValueError('invalid metadata time')
    stamp, seconds = float(now), float(timeout_seconds)
    if not math.isfinite(stamp) or not math.isfinite(seconds) or seconds <= 0 or seconds > 10:
        raise ValueError('invalid metadata time/budget')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError('metadata limit must be 1..1000')
    if isinstance(max_metadata_rows, bool) or not isinstance(max_metadata_rows, int) or not 1 <= max_metadata_rows <= 100000:
        raise ValueError('metadata row budget must be 1..100000')
    if isinstance(query_terms, (str, bytes)):
        raise ValueError('metadata terms require an iterable of strings')
    terms = []
    for term_index, term in enumerate(query_terms):
        if term_index >= 128:
            raise ValueError('metadata raw terms exceed budget')
        if not isinstance(term, str):
            raise ValueError('metadata term must be a string')
        term = term.strip()
        if not 2 <= len(term) <= 32:
            raise ValueError('metadata term length must be 2..32')
        if term not in terms:
            terms.append(term)
        if len(terms) > 8 or sum(map(len, terms)) > 128:
            raise ValueError('metadata terms exceed budget')
    if not terms:
        raise ValueError('empty metadata terms use neutral topic pagination')
    patterns = ['%' + term.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%' for term in terms]
    details = "CASE WHEN json_valid(details_json) THEN details_json ELSE '{}' END"
    contexts = "CASE WHEN json_type(" + details + ", '$.details.relevant_contexts')='array' THEN json_extract(" + details + ", '$.details.relevant_contexts') ELSE '[]' END"
    hit = "CASE WHEN substr(summary,1,512) LIKE ? ESCAPE '!' OR EXISTS (SELECT 1 FROM json_each(" + contexts + ") c WHERE CAST(c.key AS INTEGER)<8 AND c.type='text' AND substr(c.value,1,64) LIKE ? ESCAPE '!') THEN 1 ELSE 0 END"
    score_sql = ' + '.join(hit for _ in terms)
    pattern_args = [pattern for pattern in patterns for _ in range(2)]
    where, filter_args = _topic_filters(scope_id=scope_id, status=status, source_kind=source_kind)
    if not scope_id:
        where.append('scope_id=?')
        filter_args.append('')
    if stamp > 0:
        where.append('(pinned != 0 OR expires_at IS NULL OR expires_at > ?)')
        filter_args.append(stamp)
    where.append('rowid>?')
    sql = 'SELECT rowid AS scan_seq,topic_id,information_revision,version,source_kind,importance,updated_at,(' + score_sql + ') AS lexical_hits FROM topics WHERE ' + ' AND '.join(where) + ' ORDER BY rowid LIMIT ?'
    started = time.monotonic()
    deadline = started + seconds
    interrupted = False
    def progress() -> int:
        nonlocal interrupted
        if time.monotonic() >= deadline:
            interrupted = True
            return 1
        return 0
    owned = not conn.in_transaction
    scanned = matched = last_seq = 0
    exhausted = False
    reason = 'metadata_row_budget'
    heap = []
    if owned:
        conn.execute('BEGIN')
    try:
        conn.set_progress_handler(progress, 1000)
        while scanned < max_metadata_rows:
            if time.monotonic() >= deadline:
                reason = 'time_budget'
                break
            size = min(256, max_metadata_rows - scanned)
            try:
                rows = conn.execute(sql, (*pattern_args, *filter_args, last_seq, size)).fetchall()
            except sqlite3.OperationalError as exc:
                if interrupted and 'interrupted' in str(exc).lower():
                    reason = 'time_budget'
                    break
                raise
            for raw in rows:
                row = dict(raw)
                last_seq = row.pop('scan_seq')
                scanned += 1
                if row['lexical_hits'] <= 0:
                    continue
                matched += 1
                for key in ('importance', 'updated_at'):
                    value = _float_or(row[key], 0.0)
                    row[key] = value if math.isfinite(value) else 0.0
                rank = (row['lexical_hits'], row['importance'], row['updated_at'], row['topic_id'])
                entry = (rank, row)
                if len(heap) < limit:
                    heapq.heappush(heap, entry)
                elif rank > heap[0][0]:
                    heapq.heapreplace(heap, entry)
            if len(rows) < size:
                exhausted = True
                reason = 'exhausted'
                break
        # Exact budget-size exhaustion needs one cheap lookahead, not a fake full
        # scan. Handler remains bounded by the same deadline.
        if not exhausted and reason != 'time_budget' and scanned == max_metadata_rows:
            try:
                more = conn.execute('SELECT 1 FROM topics WHERE ' + ' AND '.join(where) + ' LIMIT 1', (*filter_args, last_seq)).fetchone()
                if more is None:
                    exhausted, reason = True, 'exhausted'
            except sqlite3.OperationalError as exc:
                if interrupted and 'interrupted' in str(exc).lower():
                    reason = 'time_budget'
                else:
                    raise
        conn.set_progress_handler(None, 0)
        if owned:
            conn.commit()
    except BaseException:
        conn.set_progress_handler(None, 0)
        if owned:
            conn.rollback()
        raise
    finally:
        conn.set_progress_handler(None, 0)
    truncated = matched > limit
    if exhausted and truncated:
        reason = 'shortlist_limit'
    return {'items': [entry[1] for entry in sorted(heap, key=lambda entry: entry[0], reverse=True)],
            'coverage': {'status': 'complete' if exhausted and not truncated else 'partial',
                         'reason': reason, 'ranking': 'lexical_substring',
                         'consistency': 'request_read_snapshot', 'scanned_metadata_rows': scanned,
                         'matched_metadata_rows': matched, 'shortlist_rows': len(heap),
                         'metadata_exhausted': exhausted, 'shortlist_truncated': truncated,
                         'query_now': stamp, 'field_recipe_version': 1,
                         'semantic_coverage': 'not_measured',
                         'elapsed_seconds': time.monotonic() - started}}


def resolve_topic_page_now(*, scope_id: str, now: float, status: str = '',
                           source_kind: str = '', cursor: str = '', query_version: int = 1) -> float:
    """HTTP pagination adapter: recover frozen time, never relax query bindings.

    Pass the result to BOTH list_topics_page and count_topics. This cursor is not
    an authorization token; callers must still authorize scope independently.
    """
    import base64
    import math
    stamp = float(now)
    if not math.isfinite(stamp):
        raise ValueError('non-finite topic query time')
    if not cursor:
        return stamp
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        binding = payload['binding']
        if not isinstance(binding, list) or len(binding) != 6:
            raise ValueError('invalid topic cursor binding')
        frozen = binding[2]
        if isinstance(frozen, bool) or not isinstance(frozen, (int, float)) or not math.isfinite(frozen):
            raise ValueError('invalid frozen topic query time')
        expected = [str(scope_id), str(status), frozen, int(query_version), TOPICS_STORAGE_VERSION, str(source_kind)]
        if binding != expected:
            raise ValueError('topic cursor binding mismatch')
        at, topic_id = payload['at'], payload['id']
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
            raise ValueError('invalid topic cursor position')
        if not isinstance(topic_id, str) or not topic_id:
            raise ValueError('invalid topic cursor id')
        return float(frozen)
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError) as exc:
        raise ValueError('invalid or mismatched topic cursor') from exc


def list_topics_page(conn: sqlite3.Connection, *, scope_id: str, status: str = '',
                     now: float = 0.0, cursor: str = '', limit: int = 100,
                     query_version: int = 1, source_kind: str = '') -> dict[str, Any]:
    """Bound composite keyset; changing filters requires restarting at page one.

    now=0 is inventory mode; a positive, fixed now is expiry-filtered mode. The
    cursor freezes that value, not a database snapshot: concurrent edits may move
    rows, so consistent exports must run inside a caller-owned read transaction.
    """
    import base64
    import math
    size = max(1, min(int(limit), 1000))
    stamp = float(now)
    if not math.isfinite(stamp):
        raise ValueError('non-finite topic query time')
    binding = [str(scope_id), str(status), stamp, int(query_version), TOPICS_STORAGE_VERSION, str(source_kind)]
    where, args = _topic_filters(scope_id=scope_id, status=status, source_kind=source_kind)
    if not scope_id:
        # Empty scope is an exact legacy namespace, never an all-scope query.
        where.append('scope_id = ?')
        args.append('')
    if stamp > 0:
        where.append('(pinned != 0 OR expires_at IS NULL OR expires_at > ?)')
        args.append(stamp)
    if cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if payload['binding'] != binding or not math.isfinite(float(payload['at'])):
                raise ValueError('topic cursor binding mismatch')
            topic_id = payload['id']
            if not isinstance(topic_id, str) or not topic_id:
                raise ValueError('invalid topic cursor id')
            at = float(payload['at'])
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise ValueError('invalid or mismatched topic cursor') from exc
        where.append('(updated_at < ? OR (updated_at = ? AND topic_id < ?))')
        args.extend((at, at, topic_id))
    rows = conn.execute('SELECT * FROM topics WHERE ' + ' AND '.join(where) +
                        ' ORDER BY updated_at DESC, topic_id DESC LIMIT ?', (*args, size + 1)).fetchall()
    more = len(rows) > size
    items = [_topic_row(row) for row in rows[:size]]
    next_cursor = ''
    if more and items:
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            {'binding': binding, 'at': last['updated_at'], 'id': last['topic_id']},
            ensure_ascii=False, separators=(',', ':')).encode()).decode()
    return {'items': items, 'next_cursor': next_cursor, 'has_more': more}


def get_source_events_by_ids(conn: sqlite3.Connection, event_ids: Iterable[str], *,
                             scope_id: str = '') -> dict[str, dict[str, Any]]:
    """Missing IDs are absent; corrupt policy is {}, SQL failure propagates."""
    found = {}
    for ids in _id_chunks(event_ids):
        sql = 'SELECT rowid AS seq,* FROM source_events WHERE event_id IN (' + ','.join('?' for _ in ids) + ')'
        args: list[Any] = list(ids)
        if scope_id:
            sql += ' AND scope_id=?'
            args.append(scope_id)
        for row in conn.execute(sql, args):
            item = _source_event_row(row)
            item['policy_revision'] = int(row['policy_revision'])
            item['deleted_at'] = row['deleted_at']
            found[item['event_id']] = item
    return found


def shares_for_sources(conn: sqlite3.Connection, source_ids: Iterable[str], *, scope_id: str = '',
                       session_id: str = '') -> list[dict[str, Any]]:
    """All exposure rows for a finite source set, with no global row limit."""
    found = []
    for ids in _id_chunks(source_ids):
        sql = 'SELECT * FROM shares WHERE source_id IN (' + ','.join('?' for _ in ids) + ')'
        args: list[Any] = list(ids)
        for column, value in (('scope_id', scope_id), ('session_id', session_id)):
            if value:
                sql += ' AND ' + column + '=?'
                args.append(value)
        found.extend(_share_row(row) for row in conn.execute(sql, args))
    return sorted(found, key=lambda row: (row['created_at'], row['share_id']))


def get_topic_snapshot(conn: sqlite3.Connection, topic_ids: Iterable[str], *, scope_id: str,
                       now: float) -> dict[str, Any]:
    with _storage_transaction(conn):
        topics = {}
        for ids in _id_chunks(topic_ids):
            rows = conn.execute('SELECT * FROM topics WHERE scope_id=? AND status=? '
                                'AND (pinned!=0 OR expires_at IS NULL OR expires_at>?) AND topic_id IN (' +
                                ','.join('?' for _ in ids) + ')', (scope_id, TOPIC_AVAILABLE, float(now), *ids))
            for row in rows:
                topics[row['topic_id']] = _topic_row(row)
        sources = get_source_events_by_ids(conn, [source for row in topics.values()
                        for source in _json_text_list(row['source_event_ids_json'])], scope_id=scope_id)
        return {'topics': topics, 'sources': sources,
                'shares': shares_for_sources(conn, [*topics, *sources], scope_id=scope_id)}


def get_decision(conn: sqlite3.Connection, decision_id: str) -> dict[str, Any] | None:
    row = conn.execute('SELECT * FROM decision_batches WHERE decision_id=?', (decision_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['inputs'] = [dict(item) for item in conn.execute(
        'SELECT event_id,event_seq FROM decision_inputs WHERE decision_id=? ORDER BY event_seq', (decision_id,))]
    result['side_effects'] = _json_dict(result['side_effects_json'])
    result['released_inputs'] = _json_list(result['released_inputs_json'])
    return result


def _registered_plan_association(conn: sqlite3.Connection, execution_id: str) -> dict[str, str] | None:
    row = conn.execute('SELECT value FROM kv WHERE key=?', ('plan-execution:' + execution_id,)).fetchone()
    if row is None:
        return None
    registration = json.loads(row[0])
    if not isinstance(registration, dict):
        raise ValueError('invalid plan registration object')
    step = registration.get('step', {})
    phase_id = registration.get('phase_id')
    if not phase_id and registration.get('trusted_internal') is True:
        return None  # This narrow phaseful API does not grant internal phase authority.
    if not isinstance(step, dict) or step.get('generation_execution_id') != execution_id or not phase_id or step.get('generation_phase_id') != phase_id:
        raise ValueError('unsupported or invalid phaseful plan registration')
    phase_row = conn.execute('SELECT value FROM kv WHERE key=?', ('generation-phase:' + phase_id,)).fetchone()
    if phase_row is None:
        raise ValueError('missing plan generation phase')
    phase = json.loads(phase_row[0])
    if not isinstance(phase, dict):
        raise ValueError('invalid plan generation phase object')
    parent = get_decision(conn, phase.get('decision_id', ''))
    if not parent or phase.get('phase_id') != phase_id or parent['session_id'] != phase.get('origin_session_id'):
        raise ValueError('plan authority origin mismatch')
    if parent.get('error') == 'cancelled_for_regeneration':
        raise ValueError('released plan generation authority')
    ids = _exact_ids(phase.get('input_event_ids', ()))
    owned = {item['event_id']: item['event_seq'] for item in parent['inputs']}
    sources = get_source_events_by_ids(conn, ids, scope_id=parent['scope_id'])
    if any(event not in owned or event not in sources or sources[event]['origin_session_id'] != parent['session_id'] or sources[event]['seq'] != owned[event] for event in ids):
        raise ValueError('plan authority input snapshot mismatch')
    association = {'execution_id': execution_id, 'authority_decision_id': parent['decision_id'],
                   'generation_phase_id': phase_id, 'scope_id': parent['scope_id'],
                   'origin_session_id': parent['session_id'], 'config_version': registration.get('config_version')}
    if any(not isinstance(value, str) or not value for value in association.values()):
        raise ValueError('invalid plan association fields')
    return association


def _check_plan_child(conn: sqlite3.Connection, decision_id: str) -> bool:
    """Validate explicit registered child; return False for a durable cancellation."""
    row = get_decision(conn, decision_id)
    effects = row['side_effects'] if row else {}
    execution_id = effects.get('execution_id') or decision_id
    association = _registered_plan_association(conn, execution_id)
    if association is None:
        if effects.get('execution_id'):
            raise ValueError('missing explicitly linked plan registration')
        return True
    if execution_id != decision_id:
        raise ValueError('plan child ID must equal execution ID')
    if row:
        if (row['scope_id'], row['session_id'], row['config_version']) != (association['scope_id'], association['origin_session_id'], association['config_version']) or row['inputs']:
            raise ValueError('plan child snapshot mismatch')
        expected = {key: association[key] for key in ('execution_id','authority_decision_id','generation_phase_id')}
        legacy = {key: association[key] for key in ('authority_decision_id','generation_phase_id')}
        if effects not in ({}, legacy, expected):
            raise ValueError('plan child association drift')
        if effects == legacy:
            conn.execute('UPDATE decision_batches SET side_effects_json=? WHERE decision_id=?', (json.dumps(expected), decision_id))
    tombstone = conn.execute('SELECT association_json FROM plan_execution_cancellations WHERE execution_id=?', (execution_id,)).fetchone()
    if tombstone:
        if json.loads(tombstone[0]) != association:
            raise ValueError('cancelled plan association drift')
        return False
    return True


def cancel_plan_execution(conn: sqlite3.Connection, execution_id: str, *, association: dict[str, Any], now: float) -> dict[str, Any]:
    with _storage_transaction(conn):
        registered = _registered_plan_association(conn, execution_id)
        if registered is None or association != registered:
            raise ValueError('plan cancellation association mismatch')
        existing = conn.execute('SELECT association_json FROM plan_execution_cancellations WHERE execution_id=?', (execution_id,)).fetchone()
        if existing and json.loads(existing[0]) != association:
            raise ValueError('plan cancellation association drift')
        _check_plan_child(conn, execution_id)
        conn.execute('INSERT OR IGNORE INTO plan_execution_cancellations VALUES(?,?,?)', (execution_id, _strict_json_object(association), float(now)))
        ids = [row[0] for row in conn.execute("SELECT segment_id FROM outbox_segments WHERE decision_id=? AND status='generated' AND attempt_count=0", (execution_id,))]
        conn.execute("UPDATE outbox_segments SET status='cancelled',error='plan_execution_cancelled',updated_at=? WHERE decision_id=? AND status='generated' AND attempt_count=0", (float(now), execution_id))
        return {'cancelled': True, 'cancelled_segment_ids': ids}


def silent_decision_reclaimable(status: Any, updated_at: Any, now: float) -> bool:
    """这条沉默终态是否已经过了接管窗口（``:data:`DECISION_SILENT_RECLAIM_SECONDS```）。

    ``decided_silent`` 是"当时没接"，不是"办完了"：重投进来的同一批消息超过窗口就
    允许重开；``sent`` / ``cancelled`` 是真正的终态，永远不接管。
    """

    if str(status or '') != 'decided_silent':
        return False
    try:
        stamp = float(updated_at or 0.0)
    except (TypeError, ValueError):
        return False
    try:
        current = float(now or 0.0)
    except (TypeError, ValueError):
        return False
    return current - stamp > DECISION_SILENT_RECLAIM_SECONDS


def begin_decision(conn: sqlite3.Connection, decision_id: str, *, scope_id: str, session_id: str,
                   event_ids: Iterable[str], config_version: str = '', now: float = 0.0,
                   refresh_config_version: bool = False) -> dict[str, Any]:
    """开一次决策（幂等：同一个 ``decision_id`` 再来一次就返回既有那行）。

    ``refresh_config_version``（2026-10-16 真机事故修）：**只有** ``config_version``
    对不上时，把库里那行刷成新值并当同一次决策返回，而不是抛"快照漂移"。

    为什么非要有这个开关，见 ``core/engine.py:_tick_session`` 的 tick 身份：
    ``tick:<scope>:<session>:<world_time+1>`` 里**不含配置版本**，所以玩家一改世界配置
    （哪怕只是加了一个旋钮），同一个 ``world_time`` 的 tick 身份不变、配置哈希变了
    → 守卫抛 ``decision id reused with a different snapshot`` → 这一拍在 ``begin_decision``
    就炸掉、``world_time`` 永远推进不了 → **同一个 id 每 5 秒再炸一次，这个会话从此不动**。
    配置变更是**合法**的新快照（不是漂移），所以这里认它、并且把新版本记下来。

    ``scope_id`` / ``session_id`` / 事件集合对不上仍然抛（那才是真的漂移 / 撞号）；
    事件集合为空且已有 ``side_effects`` 的行照旧原样返回——调用方靠它避免重复产生外部动作。
    """

    ids = _exact_ids(event_ids)
    if not decision_id or not scope_id or not session_id:
        raise ValueError('decision identity requires decision_id/scope_id/session_id')
    with _storage_transaction(conn):
        registered = _registered_plan_association(conn, decision_id)
        if registered:
            if ids or (scope_id, session_id, str(config_version)) != (registered['scope_id'], registered['origin_session_id'], registered['config_version']):
                raise ValueError('plan child creation snapshot mismatch')
            if not _check_plan_child(conn, decision_id):
                raise ValueError('plan execution cancelled')
        existing = get_decision(conn, decision_id)
        if (
            existing is not None
            and (existing['scope_id'], existing['session_id']) == (scope_id, session_id)
            and set(ids) == {item['event_id'] for item in existing['inputs']}
            and not existing['outbox_sealed']
            and silent_decision_reclaimable(existing['status'], existing['updated_at'], now)
        ):
            # 同一条决策（同会话 + 同一组事件）落在沉默终态、且已经过了接管窗口：把这一批
            # 重开成 pending，让调用方照常生成。归属本来就在它名下，不用动 decision_inputs。
            # （真机 2026-10-23：平台把同一条群消息投递了两次，第一次落了 decided_silent，
            # 第二次撞幂等原样返回沉默 → 用户看到的是"她没回"。）
            conn.execute(
                'UPDATE decision_batches SET status=?, config_version=?, updated_at=?, error=? '
                'WHERE decision_id=?',
                (
                    'pending',
                    str(config_version) if refresh_config_version else str(existing.get('config_version') or ''),
                    float(now),
                    '',
                    decision_id,
                ),
            )
            return get_decision(conn, decision_id)
        reclaimed = False
        if (
            existing
            and not existing['inputs']
            and (
                existing['status'] not in _PROCESSED_STATUSES
                # 被打断的那一轮（``cancel_decision_for_regeneration``）：状态是终态
                # ``cancelled``、输入已释放、outbox 没封、没有任何副作用——它就是
                # "什么都没发出去"。同样这几条消息再来一次（会话 + 事件集合算出同一个
                # decision_id）必须能把归属拿回来重做，否则用户看到的就是"被打断之后
                # 那一轮的话永远没人回"（用户 m09370 的例子）。
                or (
                    str(existing.get('error') or '') == 'cancelled_for_regeneration'
                    and not existing['outbox_sealed']
                    and not existing['side_effects']
                )
            )
        ):
            # 这一批的输入曾被后来的一批接管走（见下面的 ``adopted``）：decision_id
            # 由"会话 + 事件集合"唯一决定，能再算出同一个 id 就说明同样的几条消息又
            # 回来了——把归属还回来，别报成"撞号"。
            conn.execute(
                'UPDATE decision_batches SET status=?, config_version=?, updated_at=?, error=? '
                'WHERE decision_id=?',
                ('pending', str(config_version), float(now), '', decision_id),
            )
            existing = get_decision(conn, decision_id)
            reclaimed = True
        if existing and not reclaimed:
            same_identity = (
                (existing['scope_id'], existing['session_id']) == (scope_id, session_id)
                and set(ids) == {item['event_id'] for item in existing['inputs']}
            )
            if same_identity and refresh_config_version:
                if str(existing.get('config_version') or '') != str(config_version):
                    conn.execute(
                        'UPDATE decision_batches SET config_version=?, updated_at=? WHERE decision_id=?',
                        (str(config_version), float(now), decision_id),
                    )
                    existing = get_decision(conn, decision_id)
                return existing
            if (existing['scope_id'], existing['session_id'], existing['config_version']) != (scope_id, session_id, str(config_version)) or set(ids) != {item['event_id'] for item in existing['inputs']}:
                raise ValueError('decision id reused with a different snapshot')
            return existing
        if existing and reclaimed and (existing['scope_id'], existing['session_id']) != (scope_id, session_id):
            raise ValueError('decision id reused with a different snapshot')
        sources = get_source_events_by_ids(conn, ids, scope_id=scope_id)
        if set(sources) != set(ids) or any(item['origin_session_id'] != session_id for item in sources.values()):
            raise ValueError('decision input missing or outside scope/session')
        # 这些事件可能已经挂在别的批次名下：上一轮被打断（REPLY_INTERRUPTED）时它的
        # 消息会被放回待回队列，可 decision_inputs 里的归属还留着。直接 INSERT 会撞
        # PRIMARY KEY，报成一句看不懂的 "UNIQUE constraint failed"（真机 m29：用户发
        # 图+文字，第一条被打断，第二条就再也回不了了）。
        adopted: list[str] = []
        for event_id in ids:
            owner = event_processing_status(conn, event_id, scope_id=scope_id, session_id=session_id)
            if owner is None or owner['decision_id'] == decision_id:
                continue
            if str(owner.get('status') or '') in _PROCESSED_STATUSES and not silent_decision_reclaimable(
                owner.get('status'), owner.get('updated_at'), now
            ):
                # sent / cancelled 是真终态；decided_silent 只有过了接管窗口（它当时没接话，
                # 不是这件事已经办完）才允许把这条消息改判给新批次。
                raise ValueError(
                    f'event already answered by another batch ({owner["decision_id"]})'
                )
            # 旧批次没走完（被打断 / 失败）：这条消息改由这一批回答
            conn.execute(
                'DELETE FROM decision_inputs WHERE scope_id=? AND session_id=? AND event_id=?',
                (scope_id, session_id, event_id),
            )
            adopted.append(str(owner['decision_id']))
        if not reclaimed:
            conn.execute('INSERT INTO decision_batches(decision_id,scope_id,session_id,config_version,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                         (decision_id, scope_id, session_id, str(config_version), float(now), float(now)))
        for event_id in ids:
            conn.execute('INSERT INTO decision_inputs(scope_id,session_id,event_id,event_seq,decision_id) VALUES(?,?,?,?,?)',
                         (scope_id, session_id, event_id, sources[event_id]['seq'], decision_id))
        for previous in dict.fromkeys(adopted):
            # 它已经没有输入了：记成 failed（"这一批没走完"）；**不记 cancelled**——
            # cancelled 是终态、不许再动，而同样这几条消息再来一次时会算出同一个
            # decision_id（由"会话 + 事件集合"唯一决定），那时要能把归属还回来。
            remaining = conn.execute(
                'SELECT COUNT(*) FROM decision_inputs WHERE decision_id=?', (previous,)
            ).fetchone()[0]
            if remaining:
                continue
            try:
                set_decision_status(
                    conn, previous, 'failed', now=now,
                    error=f'superseded_by:{decision_id}',
                )
            except Exception:  # noqa: BLE001 - 已经被收口过的批次不用再动
                pass
        return get_decision(conn, decision_id)


def extend_decision_inputs(conn: sqlite3.Connection, decision_id: str, *,
                           event_ids: Iterable[str], now: float) -> bool:
    """Append inputs only before output freeze; never steal another batch's inputs."""
    ids = _exact_ids(event_ids)
    with _storage_transaction(conn):
        decision = get_decision(conn, decision_id)
        if not decision or decision['status'] != 'pending' or decision['outbox_sealed']:
            return False
        if conn.execute('SELECT 1 FROM outbox_segments WHERE decision_id=? LIMIT 1', (decision_id,)).fetchone():
            return False
        sources = get_source_events_by_ids(conn, ids, scope_id=decision['scope_id'])
        if set(sources) != set(ids) or any(row['origin_session_id'] != decision['session_id'] for row in sources.values()):
            return False
        for event_id in ids:
            owner = event_processing_status(conn, event_id, scope_id=decision['scope_id'], session_id=decision['session_id'])
            if owner and owner['decision_id'] != decision_id:
                return False
        for event_id in ids:
            conn.execute('INSERT OR IGNORE INTO decision_inputs(scope_id,session_id,event_id,event_seq,decision_id) VALUES(?,?,?,?,?)',
                         (decision['scope_id'], decision['session_id'], event_id, sources[event_id]['seq'], decision_id))
        conn.execute('UPDATE decision_batches SET updated_at=? WHERE decision_id=?', (float(now), decision_id))
        return True


def finalize_segment_exposure(conn: sqlite3.Connection, segment_id: str, *,
                              communicative_act: str | None = None, now: float = 0.0) -> list[dict[str, Any]]:
    """Recoverable atomic exposure from confirmed immutable segment mappings.

    Mapping/content semantic verification is the caller's obligation before
    prepare. No caller-supplied scope, target or sources may replace this freeze.
    """
    import hashlib
    if communicative_act is not None and communicative_act not in COMMUNICATIVE_ACTS:
        raise ValueError('invalid exposure communicative act')
    with _storage_transaction(conn):
        segment = _outbox_row(conn, segment_id)
        if not segment or segment['status'] not in ('accepted', 'delivered'):
            raise ValueError('exposure requires a confirmed segment receipt')
        if communicative_act is not None and communicative_act != segment['communicative_act']:
            raise ValueError('exposure usage differs from frozen segment')
        communicative_act = segment['communicative_act']
        if communicative_act not in COMMUNICATIVE_ACTS:
            raise ValueError('invalid frozen segment usage')
        decision = get_decision(conn, segment['decision_id'])
        marker = conn.execute('SELECT communicative_act FROM segment_exposure_finalizations WHERE segment_id=?', (segment_id,)).fetchone()
        if marker and marker[0] != communicative_act:
            raise ValueError('finalized exposure usage cannot be changed')
        result = []
        content_hash = hashlib.sha256(segment['text'].encode()).hexdigest()
        proxy = _TransactionConnection(conn)
        for mapping in segment['topic_mappings']:
            # Preserve both topic-indexed legacy consumers and source-level dedup.
            for source_id in _exact_ids([mapping['topic_id'], *mapping['source_ids']]):
                identity = json.dumps([segment_id, mapping['topic_id'], source_id], separators=(',', ':'))
                share_id = 'segment:' + hashlib.sha256(identity.encode()).hexdigest()
                share = Share(share_id=share_id, scope_id=decision['scope_id'], source_id=source_id,
                              information_revision=mapping['information_revision'],
                              session_id=segment['target_session_id'], communicative_act=communicative_act,
                              operation_id=segment['decision_id'], content_hash=content_hash,
                              text=segment['text'], status=segment['status'],
                              platform_message_ids=tuple(segment['platform_message_ids']),
                              created_at=float(now), sent_at=segment['updated_at'])
                record_share(proxy, share)
                if segment['status'] == 'delivered':
                    conn.execute("UPDATE shares SET status='delivered' WHERE share_id=?", (share_id,))
                row = get_share(conn, share_id)
                if row is None:
                    raise RuntimeError('segment exposure persistence failed')
                result.append(row)
        conn.execute('INSERT OR IGNORE INTO segment_exposure_finalizations VALUES(?,?,?)',
                     (segment_id, communicative_act, float(now)))
        return result


def cancel_decision_for_regeneration(conn: sqlite3.Connection, decision_id: str, *, now: float) -> bool:
    """Release only interrupted generation, never external side effects.

    Callers MUST persist a nonempty side_effects entry BEFORE invoking any action.
    Empty durable evidence cannot prove absence of unrecorded external effects.
    """
    with _storage_transaction(conn):
        row = get_decision(conn, decision_id)
        if not row:
            return False
        if row['status'] not in ('pending', 'failed') or row['outbox_sealed']:
            return False
        try:
            effects = json.loads(row['side_effects_json'])
        except (TypeError, ValueError):
            return False
        if not isinstance(effects, dict) or effects:
            return False
        if conn.execute('SELECT 1 FROM outbox_segments WHERE decision_id=? LIMIT 1', (decision_id,)).fetchone():
            return False
        conn.execute("UPDATE decision_batches SET status='cancelled',updated_at=?,error='cancelled_for_regeneration',released_inputs_json=? WHERE decision_id=?",
                     (float(now), json.dumps(row['inputs']), decision_id))
        conn.execute('DELETE FROM decision_inputs WHERE decision_id=?', (decision_id,))
        # Deliberately no processed cursor advancement: released events remain
        # holes until another batch actually finishes processing them.
        return True


def get_processed_cursor(conn: sqlite3.Connection, *, scope_id: str, session_id: str) -> int:
    row = conn.execute('SELECT event_seq FROM processed_cursors WHERE scope_id=? AND session_id=?',
                       (scope_id, session_id)).fetchone()
    return int(row[0]) if row else 0


def _advance_processed_cursor(conn: sqlite3.Connection, scope_id: str, session_id: str) -> int:
    previous = get_processed_cursor(conn, scope_id=scope_id, session_id=session_id)
    # Find the earliest unresolved actual input, not MAX(completed_seq). Inputs
    # not assigned a decision are holes too; unrelated sessions do not block.
    marks = ','.join('?' for _ in _PROCESSED_STATUSES)
    hole = conn.execute('SELECT s.rowid FROM source_events s LEFT JOIN decision_inputs i '
                        'ON i.event_id=s.event_id AND i.scope_id=? AND i.session_id=? '
                        'LEFT JOIN decision_batches d ON d.decision_id=i.decision_id '
                        "WHERE s.scope_id=? AND s.origin_session_id=? AND s.rowid>? AND s.event_kind IN ('message','chat') "
                        'AND (d.status IS NULL OR d.status NOT IN (' + marks + ')) ORDER BY s.rowid LIMIT 1',
                        (scope_id, session_id, scope_id, session_id, previous, *_PROCESSED_STATUSES)).fetchone()
    sql = "SELECT MAX(rowid) FROM source_events WHERE scope_id=? AND origin_session_id=? AND rowid>? AND event_kind IN ('message','chat')"
    args: list[Any] = [scope_id, session_id, previous]
    if hole:
        sql += ' AND rowid<?'
        args.append(hole[0])
    row = conn.execute(sql, args).fetchone()
    current = max(previous, int(row[0] or previous))
    conn.execute('INSERT INTO processed_cursors VALUES(?,?,?) ON CONFLICT(scope_id,session_id) '
                 'DO UPDATE SET event_seq=MAX(event_seq,excluded.event_seq)', (scope_id, session_id, current))
    return current


def event_processing_status(conn: sqlite3.Connection, event_id: str, *, scope_id: str,
                            session_id: str) -> dict[str, Any] | None:
    row = conn.execute('SELECT i.*,d.status,d.updated_at FROM decision_inputs i JOIN decision_batches d '
                       'ON d.decision_id=i.decision_id WHERE i.event_id=? AND i.scope_id=? AND i.session_id=?',
                       (event_id, scope_id, session_id)).fetchone()
    return dict(row) if row else None


def set_decision_status(conn: sqlite3.Connection, decision_id: str, status: str, *, now: float = 0.0,
                        error: str = '', side_effects: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if status not in _DECISION_STATUSES:
        raise ValueError('invalid decision status')
    with _storage_transaction(conn):
        row = get_decision(conn, decision_id)
        if not row:
            raise ValueError('unknown decision')
        if row['status'] in _PROCESSED_STATUSES and row['status'] != status:
            # 窄例外：沉默终态被更新的一批接管时才允许收口成 failed（error 必须自报
            # superseded_by:）。其余终态（sent / cancelled）永不再动。
            superseded_silent = (
                row['status'] == 'decided_silent'
                and status == 'failed'
                and str(error or '').startswith('superseded_by:')
            )
            if not superseded_silent:
                raise ValueError('terminal decision cannot be reopened')
        segments = [item[0] for item in conn.execute('SELECT status FROM outbox_segments WHERE decision_id=?', (decision_id,))]
        if status == 'decided_silent' and segments:
            raise ValueError('silent decision cannot contain outbox segments')
        if status == 'sent' and row['status'] != 'sent' and not row['outbox_sealed']:
            raise ValueError('sent decision requires a sealed producer')
        if status == 'sent' and (not segments or any(item not in ('accepted','delivered','cancelled') for item in segments)):
            raise ValueError('sent decision requires confirmed segment receipts')
        if status == 'cancelled' and any(item in ('unknown','accepted','delivered') for item in segments):
            raise ValueError('cannot cancel ambiguous or successful sends')
        effects = dict(row['side_effects'])
        if side_effects:
            # Successful side effects cannot be undone by a later failed send.
            for key, value in side_effects.items():
                if effects.get(key) != 'succeeded':
                    effects[key] = value
        conn.execute('UPDATE decision_batches SET status=?,updated_at=?,error=?,side_effects_json=? WHERE decision_id=?',
                     (status, float(now), error, json.dumps(effects), decision_id))
        _advance_processed_cursor(conn, row['scope_id'], row['session_id'])
        return get_decision(conn, decision_id)


def _outbox_row(conn: sqlite3.Connection, segment_id: str) -> dict[str, Any] | None:
    row = conn.execute('SELECT * FROM outbox_segments WHERE segment_id=?', (segment_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['platform_message_ids'] = _json_text_list(result['platform_message_ids_json'])
    result['topic_mappings'] = []
    for mapping in conn.execute('SELECT * FROM outbox_topic_mappings WHERE segment_id=? ORDER BY topic_id', (segment_id,)):
        item = dict(mapping)
        item['source_ids'] = _json_text_list(item['source_ids_json'])
        result['topic_mappings'].append(item)
    return result


def prepare_outbox_segment(conn: sqlite3.Connection, segment_id: str, *, decision_id: str,
                           target_session_id: str, text: str = '',
                           topic_mappings: Iterable[Mapping[str, Any]] = (), now: float = 0.0,
                           communicative_act: str = 'reference') -> dict[str, Any]:
    if communicative_act not in COMMUNICATIVE_ACTS:
        raise ValueError('invalid segment communicative act')
    if not segment_id or not target_session_id:
        raise ValueError('segment identity required')
    mappings = []
    for item in topic_mappings:
        mappings.append({'topic_id': str(item['topic_id']),
                         'information_revision': int(item['information_revision']),
                         'policy_revision': int(item.get('policy_revision', 1)),
                         'source_ids': sorted(_exact_ids(item.get('source_ids', ())))})
    if len({item['topic_id'] for item in mappings}) != len(mappings):
        raise ValueError('duplicate topic mapping')
    with _storage_transaction(conn):
        decision = get_decision(conn, decision_id)
        if decision is None:
            raise ValueError('missing outbox decision')
        if not _check_plan_child(conn, decision_id):
            raise ValueError('plan execution cancelled')
        existing = _outbox_row(conn, segment_id)
        if existing:
            expected = sorted(mappings, key=lambda item: item['topic_id'])
            actual = [{key: item[key] for key in ('topic_id','information_revision','policy_revision','source_ids')}
                      for item in existing['topic_mappings']]
            if (existing['decision_id'], existing['target_session_id'], existing['text'], existing['communicative_act'], actual) != (decision_id, target_session_id, text, communicative_act, expected):
                raise ValueError('segment id reused with different content or mapping')
            return existing
        if decision['outbox_sealed'] or decision['status'] in _PROCESSED_STATUSES:
            raise ValueError('sealed or terminal producer cannot add segments')
        conn.execute('INSERT INTO outbox_segments(segment_id,decision_id,target_session_id,text,created_at,updated_at,communicative_act) VALUES(?,?,?,?,?,?,?)',
                     (segment_id, decision_id, target_session_id, text, float(now), float(now), communicative_act))
        for item in mappings:
            conn.execute('INSERT INTO outbox_topic_mappings VALUES(?,?,?,?,?)',
                         (segment_id, item['topic_id'], item['information_revision'], item['policy_revision'], json.dumps(item['source_ids'])))
        return _outbox_row(conn, segment_id)


def reject_generated_segment(conn: sqlite3.Connection, segment_id: str, *, now: float = 0.0) -> bool:
    """CAS for authorization rejection, NOT a platform failure receipt.

    Never downgrades an attempted/ambiguous send. The durable audit marker also
    prevents a concurrent sender from treating this rejection as retryable.
    """
    with _storage_transaction(conn):
        cursor = conn.execute("UPDATE outbox_segments SET status='failed',error='authorization_rejected_before_send',updated_at=? "
                              "WHERE segment_id=? AND status='generated' AND attempt_count=0",
                              (float(now), segment_id))
        if cursor.rowcount != 1:
            return False
        row = _outbox_row(conn, segment_id)
        _recompute_outbox_decision(conn, row['decision_id'], now=now, error='authorization_rejected_before_send')
        return True


def begin_segment_send(conn: sqlite3.Connection, segment_id: str, *, now: float = 0.0) -> bool:
    # Persist ambiguity BEFORE the external side effect. A crash between this
    # commit and the platform call sacrifices liveness, never duplicates a send.
    with _storage_transaction(conn):
        segment = _outbox_row(conn, segment_id)
        if segment is None or not _check_plan_child(conn, segment['decision_id']):
            return False
        cursor = conn.execute("UPDATE outbox_segments SET status='unknown',attempt_count=attempt_count+1,updated_at=? "
                              "WHERE segment_id=? AND status IN ('generated','failed') AND error!='authorization_rejected_before_send' AND decision_id IN "
                              "(SELECT decision_id FROM decision_batches WHERE status NOT IN ('sent','cancelled','decided_silent'))",
                              (float(now), segment_id))
        return cursor.rowcount == 1


def record_segment_receipt(conn: sqlite3.Connection, segment_id: str, *, status: str,
                           receipt_id: str = '', platform_message_ids: Iterable[str] = (),
                           now: float = 0.0, error: str = '') -> dict[str, Any]:
    if status not in _RECEIPT_STATUSES or status == 'generated':
        raise ValueError('invalid receipt status')
    with _storage_transaction(conn):
        row = _outbox_row(conn, segment_id)
        if row is None:
            raise ValueError('unknown segment')
        if row['status'] in ('accepted', 'delivered') and status not in ('accepted', 'delivered'):
            raise ValueError('successful receipt cannot be downgraded')
        if row['status'] == 'delivered' and status == 'accepted':
            status = 'delivered'
        conn.execute('UPDATE outbox_segments SET status=?,receipt_id=?,platform_message_ids_json=?,updated_at=?,error=? WHERE segment_id=?',
                     (status, receipt_id or row['receipt_id'], json.dumps(_exact_ids(platform_message_ids) or row['platform_message_ids']), float(now), error, segment_id))
        _recompute_outbox_decision(conn, row['decision_id'], now=now, error=error)
        return _outbox_row(conn, segment_id)


def _recompute_outbox_decision(conn: sqlite3.Connection, decision_id: str, *, now: float, error: str = '') -> None:
    decision = get_decision(conn, decision_id)
    if decision['status'] in _PROCESSED_STATUSES:
        return  # Historical completed decisions must never be downgraded.
    statuses = [item[0] for item in conn.execute('SELECT status FROM outbox_segments WHERE decision_id=?', (decision_id,))]
    if not statuses:
        return  # Pure silence uses explicit decided_silent, not invented sends.
    if decision['outbox_sealed'] and all(item in ('accepted','delivered','cancelled') for item in statuses):
        status = 'sent' if any(item in ('accepted','delivered') for item in statuses) else 'cancelled'
    elif any(item in ('accepted','delivered') for item in statuses):
        status = 'partially_sent'
    elif any(item == 'failed' for item in statuses):
        status = 'failed'
    else:
        status = 'ready_to_send'
    set_decision_status(conn, decision_id, status, now=now, error=error)


def seal_outbox(conn: sqlite3.Connection, decision_id: str, *, now: float) -> bool:
    """Persist producer completion; in-flight unknown sends remain unresolved."""
    with _storage_transaction(conn):
        decision = get_decision(conn, decision_id)
        if not decision:
            return False
        if decision['outbox_sealed']:
            return True
        if decision['status'] in _PROCESSED_STATUSES:
            return False
        conn.execute('UPDATE decision_batches SET outbox_sealed=1,updated_at=? WHERE decision_id=?',
                     (float(now), decision_id))
        _recompute_outbox_decision(conn, decision_id, now=now)
        return True


def list_outbox_segments(conn: sqlite3.Connection, *, decision_id: str = '', status: str = '',
                         limit: int = 100, cursor: str = '') -> list[dict[str, Any]]:
    """Stable keyset queue scan; next_cursor on each row binds the query filters.

    Continue with the last row's next_cursor until an empty page. A status change
    behind the cursor becomes visible on the next sweep, not this dynamic scan.
    """
    import base64
    import math
    binding = [decision_id, status, 1]
    where, args = [], []
    for key, value in (('decision_id', decision_id), ('status', status)):
        if value:
            where.append(key + '=?')
            args.append(value)
    if cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            at, segment = float(payload['at']), payload['id']
            if payload['binding'] != binding or not math.isfinite(at) or not isinstance(segment, str) or not segment:
                raise ValueError('outbox cursor binding mismatch')
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise ValueError('invalid or mismatched outbox cursor') from exc
        where.append('(created_at > ? OR (created_at = ? AND segment_id > ?))')
        args.extend((at, at, segment))
    sql = 'SELECT segment_id FROM outbox_segments' + (' WHERE ' + ' AND '.join(where) if where else '')
    sql += ' ORDER BY created_at,segment_id LIMIT ?'
    rows = conn.execute(sql, (*args, max(1, min(int(limit), 1000)))).fetchall()
    result = []
    for row in rows:
        item = _outbox_row(conn, row[0])
        item['next_cursor'] = base64.urlsafe_b64encode(json.dumps(
            {'binding': binding, 'at': item['created_at'], 'id': item['segment_id']},
            ensure_ascii=False, separators=(',', ':')).encode()).decode()
        result.append(item)
    return result


def acquire_share_claim(conn: sqlite3.Connection, *, scope_id: str, source_ids: Iterable[str],
                        revision: int, session_id: str, owner_operation_id: str, now: float,
                        ttl: float = 60.0, usage: str = 'introduce') -> bool:
    """All sources claimed atomically. introduce/progress share one usage family.

    Unknown in-flight sends block re-introduction even after lease expiration.
    Answer/reference keys are request-specific and do not collide with proactive.
    """
    import math
    stamp, lease = float(now), float(ttl)
    sources = _exact_ids(source_ids)
    if not sources or not scope_id or not session_id or not owner_operation_id or not math.isfinite(stamp) or not math.isfinite(lease) or lease <= 0:
        raise ValueError('invalid claim identity/time')
    family = 'proactive' if usage in PROACTIVE_ACTS else 'request:' + owner_operation_id
    proactive_sql = ','.join("'" + act + "'" for act in sorted(PROACTIVE_ACTS))
    with _storage_transaction(conn):
        if family == 'proactive':
            for source in sources:
                blocked = conn.execute("SELECT 1 FROM outbox_topic_mappings m JOIN outbox_segments o ON o.segment_id=m.segment_id "
                                       "JOIN decision_batches d ON d.decision_id=o.decision_id "
                                       "JOIN json_each(m.source_ids_json) j WHERE d.scope_id=? AND o.target_session_id=? "
                                       "AND m.information_revision=? AND j.value=? AND o.status IN ('unknown','accepted','delivered') "
                                       f"AND o.communicative_act IN ({proactive_sql}) LIMIT 1",
                                       (scope_id, session_id, int(revision), source)).fetchone()
                if blocked:
                    return False
                exposed = conn.execute("SELECT 1 FROM shares WHERE scope_id=? AND (source_id=? OR source_id IN "
                                       "(SELECT topic_id FROM topics WHERE scope_id=? AND EXISTS "
                                       "(SELECT 1 FROM json_each(CASE WHEN json_valid(source_event_ids_json) "
                                       "THEN source_event_ids_json ELSE '[]' END) WHERE value=?))) AND information_revision=? "
                                       f"AND session_id=? AND communicative_act IN ({proactive_sql}) "
                                       "AND status IN ('accepted','delivered') LIMIT 1",
                                       (scope_id, source, scope_id, source, int(revision), session_id)).fetchone()
                if exposed:
                    return False
        for source in sources:
            row = conn.execute('SELECT * FROM topic_claims WHERE scope_id=? AND source_id=? AND revision=? AND session_id=? AND usage_family=?',
                               (scope_id, source, int(revision), session_id, family)).fetchone()
            if row and row['status'] == 'active' and row['lease_until'] > stamp:
                if row['owner_operation_id'] != owner_operation_id:
                    return False
        for source in sources:
            conn.execute('INSERT INTO topic_claims VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope_id,source_id,revision,session_id,usage_family) '
                         'DO UPDATE SET owner_operation_id=excluded.owner_operation_id,lease_until=excluded.lease_until,status=excluded.status',
                         (scope_id, source, int(revision), session_id, family, owner_operation_id, stamp + lease, 'active'))
        return True


def release_share_claim(conn: sqlite3.Connection, *, scope_id: str, session_id: str,
                        owner_operation_id: str, status: str = 'released') -> int:
    if status not in ('released', 'done', 'expired'):
        raise ValueError('invalid released claim status')
    with _storage_transaction(conn):
        cursor = conn.execute('UPDATE topic_claims SET status=? WHERE scope_id=? AND session_id=? '
                              'AND owner_operation_id=? AND status=?',
                              (status, scope_id, session_id, owner_operation_id, 'active'))
        return cursor.rowcount


def processed_event_ids(conn: sqlite3.Connection, event_ids: Iterable[str], *, scope_id: str,
                        session_id: str) -> list[str]:
    ids = _exact_ids(event_ids)
    found = set()
    for chunk in _id_chunks(ids):
        rows = conn.execute('SELECT i.event_id FROM decision_inputs i JOIN decision_batches d '
                            'ON d.decision_id=i.decision_id WHERE i.scope_id=? AND i.session_id=? '
                            "AND d.status IN ('decided_silent','sent','cancelled') AND i.event_id IN (" +
                            ','.join('?' for _ in chunk) + ')', (scope_id, session_id, *chunk))
        found.update(row[0] for row in rows)
    return [event_id for event_id in ids if event_id in found]


finish_decision = set_decision_status


# ---------------------------------------------------------------------------
# 阶段B0 追加分区：谈资向量表（topic_vectors）
#
# 设计见计划 §5（WP3 / B0）：**只建表结构 + 只给读写函数**——不接检索、不索引任何
# 数据、不动上面任何既有表与既有函数。真正的写入方（调 embedding）与读取方
# （相似度检索）都在 B1，那时候才用得上 ``provider_id`` / ``space_fingerprint`` /
# ``recipe_version`` 这三个隔离维度。
#
# 为什么主键是五列 ``(topic_id, information_revision, provider_id,
# space_fingerprint, recipe_version)``：一条谈资在"同一份信息版本 + 同一个服务商 +
# 同一个向量空间 + 同一套配方"下的向量只该有一份；换了服务商（``provider_id``）或换了
# 配方（``recipe_version``，例如改了提示词、换了池化方式）就是**另一个向量空间**的
# 向量，必须能并存而不是互相覆盖——跨空间比相似度是没有意义的。
# ``information_revision`` 进主键则保证谈资改过之后，旧版本的向量不会把新版本的向量
# 挤掉，B1 可以按版本决定用哪一份、什么时候清旧的。
#
# 为什么 ``space_fingerprint`` **必须在主键里**：它描述"这个向量落在哪个可比空间"
# （模型名 + 维度 + 归一化方式等调用方约定的一串指纹），是 B1 隔离向量空间的依据。
# 同一个 ``(topic_id, information_revision, provider_id, recipe_version)`` 底下换了
# 模型或换了维度（= 换了空间）是常事，新旧向量**必须并存成两行**；要是只把它当普通列，
# 覆盖写会留下旧行的指纹、却把 ``vector`` 换成新空间的，这一行就"自称空间 A、装的却是
# 空间 B 的向量"，B1 会拿不可比的向量去算相似度，而且查都查不出来。B1 扫候选时还要按
# 它成片取（见 ``idx_topic_vectors_space``）。
#
# ``persona_id`` 是**预留列**：B0 既不写、也不查、更不参与任何隔离判断（主键五列与
# 所有过滤条件里都没有它），只是先把列摆上，免得 B1 再改表结构；默认空串 ''，含义是
# "没指定角色"，不是说"属于空角色"。
#
# ``vector`` 存**小端 float32 的 BLOB**（``struct`` 打包，见 :func:`pack_vector` /
# :func:`unpack_vector`）。不存 JSON：检索侧要的是能直接喂
# ``numpy.frombuffer(blob, dtype='<f4')`` 的字节，JSON 文本既费空间又要解析。
#
# 幂等：``_migrate_topic_vectors`` 由 ``Database._migrate()`` 调用，空库 / 旧库 /
# 已迁移库三种起点都能重复跑，重复执行不报错、不丢数据；建表与建索引在**一个事务**
# 里完成（见 :func:`_run_ddl`），失败整体回滚后把异常抛出去，绝不留下半张表。
#
# **今天没有任何生产调用点**：下面每个函数（``upsert_topic_vector`` /
# ``get_topic_vector`` / ``delete_topic_vectors`` / ``count_topic_vectors`` /
# ``list_topic_vectors`` / ``pack_vector`` / ``unpack_vector``）以及 ``Database``
# 上的同名包装方法，B0 阶段都没有被插件入口或 ``core/`` 里任何模块调用过，唯一的
# 使用者是 ``tests/test_topic_vectors.py``；B1 接检索时才接上生产调用点。
# ---------------------------------------------------------------------------


# 建表列顺序（测试按这个顺序验"列齐全"）。
TOPIC_VECTOR_COLUMNS = (
    "topic_id",
    "information_revision",
    "provider_id",
    "space_fingerprint",
    "recipe_version",
    "dim",
    "text_hash",
    "vector",
    "status",
    "persona_id",
    "created_at",
    "updated_at",
)

# 主键五列（与 DDL 里的 PRIMARY KEY 逐字对齐，测试也拿它比对）；``get_topic_vector``
# 的位置参数也照这个顺序排，省得调用方在两种顺序之间来回翻译。
TOPIC_VECTOR_KEY_COLUMNS = (
    "topic_id",
    "information_revision",
    "provider_id",
    "space_fingerprint",
    "recipe_version",
)

# 新写入的默认状态。B1 可以自己传别的（例如把没跟上的行标成 'stale'），
# 这里只钉默认值，不限制取值范围。
TOPIC_VECTOR_DEFAULT_STATUS = "ready"

_TOPIC_VECTORS_DDL = f"""
CREATE TABLE IF NOT EXISTS topic_vectors (
    topic_id TEXT NOT NULL,
    information_revision INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    space_fingerprint TEXT NOT NULL,
    recipe_version TEXT NOT NULL,
    dim INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    vector BLOB NOT NULL,
    status TEXT NOT NULL DEFAULT '{TOPIC_VECTOR_DEFAULT_STATUS}',
    -- 预留列：B0 不写、不查、不参与隔离判断（默认空串 = 没指定角色）
    persona_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (topic_id, information_revision, provider_id, space_fingerprint, recipe_version)
);
-- 供 B1 按"某个向量空间 + 状态"成片取候选：provider_id + space_fingerprint 就是
-- 一个可比较的空间，status 把 ready 与将来的 stale / failed 分开（B0 只建索引）。
CREATE INDEX IF NOT EXISTS idx_topic_vectors_space
    ON topic_vectors (provider_id, space_fingerprint, status);
"""


def _migrate_topic_vectors(conn: sqlite3.Connection) -> None:
    """建 ``topic_vectors`` 表与 ``idx_topic_vectors_space`` 索引（可重复调用）。

    空库、没有这张表的旧库、已经迁移过的库都能跑，既有数据一行不动；失败整体回滚
    （旧库保持可用）后把异常抛出去，不静默降级。**B0 只建表，不往里写任何数据。**
    """

    _run_ddl(conn, _TOPIC_VECTORS_DDL)


def pack_vector(values: Iterable[float]) -> bytes:
    """``list[float] -> bytes``：按小端 float32 紧凑打包（与 ``numpy '<f4'`` 同字节序）。

    一个数 4 字节、空列表打包成 ``b''``；不做 NaN / Inf 校验（那是 B1 写库前的
    事，这里只负责字节层的可往返）。值不是实数（字符串以外的东西、对象……）时抛
    :class:`ValueError`——纯函数，不静默吞掉坏输入。
    """

    try:
        data = [float(value) for value in values]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("vector values must be real numbers") from exc
    return struct.pack("<%df" % len(data), *data)


def unpack_vector(blob: bytes | bytearray | memoryview) -> list[float]:
    """``bytes -> list[float]``：把 :func:`pack_vector` 的字节解回浮点列表（可往返）。

    ``b''`` 解成 ``[]``；长度不是 4 的倍数（不是 float32 载荷）、或者给来的是
    ``str``（把文本误当 BLOB）、``int``（``bytes(8)`` 会静默造出 8 个零字节，比"报错"
    危险得多）时抛 :class:`ValueError`，不猜、不截断。解出来的数是 float32 精度
    （例如 ``0.1`` 会变成 ``0.10000000149011612``），调用方按 float32 的误差容忍比对。
    """

    if isinstance(blob, (str, int)):
        raise ValueError("vector blob must be bytes, not text or a byte count")
    try:
        raw = bytes(blob)
    except (TypeError, ValueError) as exc:
        raise ValueError("vector blob must be bytes") from exc
    if len(raw) % 4 != 0:
        raise ValueError("vector blob length must be a multiple of 4")
    if not raw:
        return []
    return list(struct.unpack("<%df" % (len(raw) // 4), raw))


def _topic_vector_text(value: Any) -> str:
    """身份列的文本归一：``None`` 当空串，两端空白去掉（写入与查询用同一套）。"""

    return str(value or "").strip()


def _topic_vector_filters(
    *,
    provider_id: Any = None,
    space_fingerprint: Any = None,
    status: Any = None,
) -> tuple[list[str], list[Any]]:
    """``count_topic_vectors`` / ``list_topic_vectors`` 共用的 WHERE 片段。

    不给（``None``）或给空串 = 这一维不过滤；给了就精确相等。
    """

    where: list[str] = []
    args: list[Any] = []
    for column, value in (
        ("provider_id", provider_id),
        ("space_fingerprint", space_fingerprint),
        ("status", status),
    ):
        wanted = _topic_vector_text(value)
        if wanted:
            where.append(column + " = ?")
            args.append(wanted)
    return where, args


def upsert_topic_vector(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    information_revision: int,
    provider_id: str,
    recipe_version: str,
    space_fingerprint: str,
    dim: int,
    text_hash: str,
    vector: Iterable[float],
    status: str = TOPIC_VECTOR_DEFAULT_STATUS,
    now: float = 0.0,
) -> bool:
    """写一条谈资向量：同一主键（**含 ``space_fingerprint``**）存在则更新 ``vector`` /
    ``dim`` / ``text_hash`` / ``status`` / ``updated_at``，``created_at`` 保留首次的值。

    **B0 没有任何生产调用点**（B1 调 embedding 之后才写），这里只把接口立起来。参数全是
    关键字参数（``*`` 后面的），所以书写顺序不影响调用，签名里的顺序只是读起来顺。

    **True = 这次真的写进去了**（首插或覆盖）；False = 认不出，一行不动，也不抛
    异常（和邻近的 :func:`record_topic` 一个规矩，"认不出就不落库"）。认不出的情形：
    ``topic_id`` / ``provider_id`` / ``recipe_version`` / ``space_fingerprint`` 去空白
    后为空（空身份说明不了这向量属于哪个空间，而空 ``space_fingerprint`` 还会把两个
    不同空间的行混进同一个索引桶，检索时会拿出不可比的向量）；``vector`` 是空的；
    ``vector`` 里有非实数值；``dim`` 给了正数但与 ``len(vector)`` 不一致。

    ``dim`` 传 0（或负数 / 不给）表示"按 ``vector`` 的长度来"。``now`` 由调用方传
    （库里不自己取时钟，和邻近写函数一致）：首次写进 ``created_at``，每次写都进
    ``updated_at``。

    ``persona_id`` 是**预留列**，B0 一律写空串——这个函数根本没有 ``persona_id``
    参数，也不参与主键与任何隔离判断。

    ``space_fingerprint`` 是**主键的一部分**（不是"顺带更新的普通列"）：换了模型 / 维度
    / 归一化方式就是换空间，新向量落进**新的一行**，旧行原样留着（留还是删是 B1 的事，
    见 :func:`delete_topic_vectors`）。这样库里不可能出现"指纹说空间 A、``vector`` 却是
    空间 B"的行。``dim`` / ``text_hash`` 反过来**在覆盖列里**：它们描述这一行当前装的
    内容，同一个空间里重算一次向量，这两个值就该跟着走（例如换了维度实现但指纹相同属于
    调用方 bug，这里以"最后一次写入"为准）。
    """

    target = _topic_vector_text(topic_id)
    provider = _topic_vector_text(provider_id)
    recipe = _topic_vector_text(recipe_version)
    fingerprint = _topic_vector_text(space_fingerprint)
    if not target or not provider or not recipe or not fingerprint:
        return False
    if isinstance(vector, (bytes, bytearray, memoryview)):
        # 只收 float 序列：收 bytes 会让"2 个 float"和"8 个字节"分不清。
        return False
    try:
        data = [float(value) for value in vector]
    except (TypeError, ValueError, OverflowError):
        return False
    if not data:
        return False
    declared = _int_or(dim, 0)
    if declared <= 0:
        declared = len(data)
    elif declared != len(data):
        return False
    stamp = _float_or(now, 0.0)
    conn.execute(
        "INSERT INTO topic_vectors (topic_id, information_revision, provider_id,"
        " space_fingerprint, recipe_version, dim, text_hash, vector, status, persona_id,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (topic_id, information_revision, provider_id, space_fingerprint,"
        " recipe_version)"
        " DO UPDATE SET vector = excluded.vector, dim = excluded.dim,"
        " text_hash = excluded.text_hash, status = excluded.status,"
        " updated_at = excluded.updated_at",
        (
            target,
            _int_or(information_revision, 0),
            provider,
            fingerprint,
            recipe,
            declared,
            _topic_vector_text(text_hash),
            pack_vector(data),
            _topic_vector_text(status) or TOPIC_VECTOR_DEFAULT_STATUS,
            "",  # persona_id：预留列，B0 一律空串
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return True


def get_topic_vector(
    conn: sqlite3.Connection,
    topic_id: str,
    information_revision: int,
    provider_id: str,
    space_fingerprint: str,
    recipe_version: str,
) -> dict[str, Any] | None:
    """按主键取一条谈资向量；没有就是 ``None``。

    **B0 没有任何生产调用点**（B1 检索时才读）。返回的是整行 dict，``vector`` 保持
    库里的**原始 BLOB**——B1 要拿它直接喂 ``numpy.frombuffer(blob, dtype='<f4')``；
    想要 ``list[float]`` 就再过一手 :func:`unpack_vector`。``persona_id`` 是预留列，
    这里只是原样带出来，**不参与任何过滤或隔离判断**。

    位置参数顺序 = 主键列顺序（``TOPIC_VECTOR_KEY_COLUMNS``）：
    ``topic_id, information_revision, provider_id, space_fingerprint, recipe_version``。
    照主键排是为了让"读一行"和"写一行"用的是同一套身份顺序，肉眼比对调用点与 DDL 时
    不用在脑内换序；**注意 ``space_fingerprint`` 在 ``recipe_version`` 前面**，按
    "topic / revision / provider / recipe / fingerprint"的旧顺序传参会把配方名当成
    指纹（B0 没有生产调用点，不存在需要兼容的旧调用）。
    """

    row = conn.execute(
        "SELECT * FROM topic_vectors WHERE topic_id = ? AND information_revision = ?"
        " AND provider_id = ? AND space_fingerprint = ? AND recipe_version = ?",
        (
            _topic_vector_text(topic_id),
            _int_or(information_revision, 0),
            _topic_vector_text(provider_id),
            _topic_vector_text(space_fingerprint),
            _topic_vector_text(recipe_version),
        ),
    ).fetchone()
    return None if row is None else dict(row)


def delete_topic_vectors(conn: sqlite3.Connection, topic_id: str) -> int:
    """删掉**这条谈资的全部向量**（不分版本 / 服务商 / 配方），返回删掉的条数。

    **B0 没有任何生产调用点**：留给 B1 在谈资被删 / 被撤回时连坐清理（一条谈资没了，
    它的向量留着只会变成查不到的垃圾）。``topic_id`` 为空时返回 0，什么都不删——
    空条件不做全表删除。``persona_id`` 是预留列，不参与这里的匹配。
    """

    target = _topic_vector_text(topic_id)
    if not target:
        return 0
    cursor = conn.execute("DELETE FROM topic_vectors WHERE topic_id = ?", (target,))
    conn.commit()
    return int(cursor.rowcount or 0)


def count_topic_vectors(
    conn: sqlite3.Connection,
    *,
    provider_id: Any = None,
    space_fingerprint: Any = None,
    status: Any = None,
) -> int:
    """数向量行数；三个维度可任意组合，空表返回 0。

    **B0 没有任何生产调用点**（B1 用来看某个空间攒了多少条）。三个维度不给
    （``None``）或给空串都表示"不过滤"，给了就精确相等；``persona_id`` 是预留列，
    **不能也不会**作为这里的筛选条件。
    """

    where, args = _topic_vector_filters(
        provider_id=provider_id,
        space_fingerprint=space_fingerprint,
        status=status,
    )
    sql = "SELECT COUNT(*) FROM topic_vectors"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(conn.execute(sql, tuple(args)).fetchone()[0] or 0)


def list_topic_vectors(
    conn: sqlite3.Connection,
    *,
    status: Any = None,
    provider_id: Any = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按主键顺序分页列向量行（每行 ``vector`` 是原始 BLOB）；**B0 只给接口，B1 才用**。

    **B0 没有任何生产调用点**：B1 拿它扫某个空间的行、喂相似度计算。``status`` /
    ``provider_id`` 不给（``None``）或给空串都表示不过滤；``limit`` 上限 1000
    （给 0 / 负数按 100 算），``offset`` 负数按 0 算。排序键就是主键五列（含
    ``space_fingerprint``），同一 `(topic, revision, provider, recipe)` 下的不同空间
    行也有确定次序，翻页稳定。``persona_id`` 是预留列，只原样带出，不参与过滤。
    """

    where, args = _topic_vector_filters(status=status, provider_id=provider_id)
    rows_wanted = _int_or(limit, 0)
    if rows_wanted <= 0:
        rows_wanted = 100
    rows_wanted = min(rows_wanted, 1000)
    skip = max(_int_or(offset, 0), 0)
    sql = "SELECT * FROM topic_vectors"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (
        " ORDER BY topic_id, information_revision, provider_id, space_fingerprint,"
        " recipe_version LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, (*args, rows_wanted, skip)).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 记忆向量表（memory_vectors）
#
# 与 topic_vectors 同构，只把 ``topic_id`` 换成 ``memory_id``（记忆主键是 INTEGER）。
# 主键仍是五列：一条记忆在「同一份文本版本 + 同一个服务商 + 同一个向量空间 + 同一套
# 配方」下只留一份向量；换模型 = 换空间，新旧两行必须并存——跨空间比相似度没有意义，
# 所以 ``space_fingerprint`` 必须在主键里：覆盖写会造出"自称空间 A、装的却是空间 B
# 的向量"这种查都查不出来的行。
#
# 与谈资的关键区别：**记忆的向量是按需现算 + 缓存**，不排队、不后台重攒。缓存只用来
# 省一次宿主调用；``text_hash`` 或空间指纹对不上就重算一行。所以「换 embedding 模型」
# 不需要重建索引，下一轮自然落到新空间上——旧空间的行留着无害，想省空间可以一键清掉
# （``/{plugin}/embedding/memory/rebuild``）。
# ---------------------------------------------------------------------------

MEMORY_VECTOR_COLUMNS = (
    "memory_id",
    "memory_revision",
    "provider_id",
    "space_fingerprint",
    "recipe_version",
    "dim",
    "text_hash",
    "vector",
    "status",
    "persona_id",
    "created_at",
    "updated_at",
)

MEMORY_VECTOR_KEY_COLUMNS = (
    "memory_id",
    "memory_revision",
    "provider_id",
    "space_fingerprint",
    "recipe_version",
)

MEMORY_VECTOR_DEFAULT_STATUS = "ready"

_MEMORY_VECTORS_DDL = f"""
CREATE TABLE IF NOT EXISTS memory_vectors (
    memory_id INTEGER NOT NULL,
    memory_revision INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    space_fingerprint TEXT NOT NULL,
    recipe_version TEXT NOT NULL,
    dim INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    vector BLOB NOT NULL,
    status TEXT NOT NULL DEFAULT '{MEMORY_VECTOR_DEFAULT_STATUS}',
    persona_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (memory_id, memory_revision, provider_id, space_fingerprint, recipe_version)
);
CREATE INDEX IF NOT EXISTS idx_memory_vectors_space
    ON memory_vectors (provider_id, space_fingerprint, status);
"""


def _migrate_memory_vectors(conn: sqlite3.Connection) -> None:
    """建 ``memory_vectors`` 表与 ``idx_memory_vectors_space`` 索引（可重复调用）。

    空库 / 旧库 / 已迁移库都能跑；失败整体回滚后抛出去，不静默降级。
    """

    _run_ddl(conn, _MEMORY_VECTORS_DDL)


def upsert_memory_vector(
    conn: sqlite3.Connection,
    *,
    memory_id: int,
    memory_revision: int,
    provider_id: str,
    recipe_version: str,
    space_fingerprint: str,
    dim: int,
    text_hash: str,
    vector: Iterable[float],
    status: str = MEMORY_VECTOR_DEFAULT_STATUS,
    persona_id: str = "",
    now: float = 0.0,
) -> bool:
    """写一行记忆向量（同一五列主键则覆盖 dim / text_hash / vector / status / updated_at）。

    返回 True 表示确实落了盘；``vector`` 走 :func:`pack_vector`，坏输入抛 ValueError。
    """

    if not provider_id or not space_fingerprint or not recipe_version:
        raise ValueError("memory vector needs provider / space / recipe")
    blob = pack_vector(vector)
    stamp = float(now or time.time())
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO memory_vectors (
                    memory_id, memory_revision, provider_id, space_fingerprint,
                    recipe_version, dim, text_hash, vector, status, persona_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (memory_id, memory_revision, provider_id, space_fingerprint,
                             recipe_version) DO UPDATE SET
                    dim = excluded.dim,
                    text_hash = excluded.text_hash,
                    vector = excluded.vector,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    int(memory_id),
                    int(memory_revision),
                    provider_id,
                    space_fingerprint,
                    recipe_version,
                    int(dim),
                    text_hash,
                    blob,
                    status,
                    persona_id,
                    stamp,
                    stamp,
                ),
            )
    except sqlite3.Error:
        return False
    return True


def list_memory_vectors_for(
    conn: sqlite3.Connection,
    memory_ids: Iterable[int],
    *,
    provider_id: str,
    space_fingerprint: str,
    recipe_version: str,
) -> list[dict[str, Any]]:
    """按记忆 id 批量取向量行（只取同一个空间 / 配方的，别的空间一概不碰）。"""

    ids = [int(value) for value in memory_ids]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT memory_id, memory_revision, dim, text_hash, vector, status
        FROM memory_vectors
        WHERE provider_id = ? AND space_fingerprint = ? AND recipe_version = ?
          AND memory_id IN ({placeholders})
        """,
        [provider_id, space_fingerprint, recipe_version, *ids],
    ).fetchall()
    return [
        {
            "memory_id": row[0],
            "memory_revision": row[1],
            "dim": row[2],
            "text_hash": row[3],
            "vector": row[4],
            "status": row[5],
        }
        for row in rows
    ]


def count_memory_vectors(
    conn: sqlite3.Connection,
    *,
    provider_id: str = "",
    space_fingerprint: str = "",
) -> int:
    """数向量行：给了 provider/space 就只数那一格，都不给数全表。"""

    clauses = []
    params: list[Any] = []
    if provider_id:
        clauses.append("provider_id = ?")
        params.append(provider_id)
    if space_fingerprint:
        clauses.append("space_fingerprint = ?")
        params.append(space_fingerprint)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    row = conn.execute(f"SELECT COUNT(*) FROM memory_vectors{where}", params).fetchone()
    return int(row[0] or 0) if row else 0


def delete_memory_vectors_for_space(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    space_fingerprint: str,
) -> int:
    """清掉「某一个向量空间」的全部记忆向量行（换模型后清旧缓存用），返回条数。"""

    with conn:
        cursor = conn.execute(
            "DELETE FROM memory_vectors WHERE provider_id = ? AND space_fingerprint = ?",
            [provider_id, space_fingerprint],
        )
    return int(cursor.rowcount or 0)


def delete_memory_vectors_not_in_space(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    space_fingerprint: str,
) -> int:
    """清掉「不属于当前空间」的全部记忆向量行（一键重建 = 只清别的空间）。"""

    with conn:
        cursor = conn.execute(
            "DELETE FROM memory_vectors WHERE NOT (provider_id = ? AND space_fingerprint = ?)",
            [provider_id, space_fingerprint],
        )
    return int(cursor.rowcount or 0)


# 阶段B1 追加分区：topic_index_queue（谈资索引队列，B1-WP1）
#
# 设计见 .vwtest/embedding-b-work-arrangement.md §10.3 的 B1-WP1 行与 §10.2 的铁律。
# 这个队列只记「哪条谈资该重算向量」：**不存向量**（向量在 ``topic_vectors``）、
# **不存向量空间**（空间在真正调 embedding 的那一刻才由调用方决定）。于是换
# provider / 模型 / 维度之后，队里还没算完的行会自动落进新空间；重建索引的入口是
# ``core/embedding_index.TopicIndexQueue.clear()`` / ``.rescan()``，不是队列键。
#
# 幂等：``_migrate_topic_index_queue`` 由 ``Database._migrate()`` 调用，空库 / 旧库 /
# 已迁移的库都能重复跑，既有数据一行不动；失败整体回滚后把异常抛出去（旧库保持
# 可用），不静默降级。**唯一的生产写入方是 ``core/embedding_index.py``。**
# ---------------------------------------------------------------------------


# 建表列顺序（测试按这个顺序验"列齐全"）。
TOPIC_INDEX_QUEUE_COLUMNS = (
    "id",
    "topic_id",
    "information_revision",
    "scope_id",
    "text",
    "text_hash",
    "status",
    "attempts",
    "next_attempt_at",
    "created_at",
    "updated_at",
)

# 唯一键三列：谈资身份 + 信息版本 + 索引文本指纹。**不含 provider / space_fingerprint**
# ——索引文本与空间无关，把空间写进队列键会让"换 provider 后重建索引"永远扫不到旧行。
TOPIC_INDEX_QUEUE_UNIQUE_COLUMNS = ("topic_id", "information_revision", "text_hash")

# 队列行的状态取值（``mark_topic_index`` 只认这四个）。
TOPIC_INDEX_QUEUE_STATUSES = ("pending", "done", "failed", "stale")
TOPIC_INDEX_QUEUE_DEFAULT_STATUS = "pending"

# 重试退避上限（秒）：延迟 = min(这个值, 2 ** attempts)，与 embedding_index 同源。
TOPIC_INDEX_QUEUE_MAX_BACKOFF = 60.0

_TOPIC_INDEX_QUEUE_DDL = f"""
CREATE TABLE IF NOT EXISTS topic_index_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id TEXT NOT NULL,
    information_revision INTEGER NOT NULL,
    -- 入队时谈资所属的会话组：清空索引 / 重扫要能按组过滤（'' = 没有归属的旧行）
    scope_id TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '{TOPIC_INDEX_QUEUE_DEFAULT_STATUS}',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE (topic_id, information_revision, text_hash)
);
-- 取 due 行就是按 (status, next_attempt_at, id) 升序扫的，这条索引照那个查询建
CREATE INDEX IF NOT EXISTS idx_topic_index_queue_due
    ON topic_index_queue (status, next_attempt_at, id);
-- 按会话组清空 / 计数
CREATE INDEX IF NOT EXISTS idx_topic_index_queue_scope
    ON topic_index_queue (scope_id, status);
"""


def _migrate_topic_index_queue(conn: sqlite3.Connection) -> None:
    """建 ``topic_index_queue`` 表与两条索引（可重复调用）。

    空库、没有这张表的旧库、已经迁移过的库都能跑，既有数据一行不动；失败整体回滚
    （旧库保持可用）后把异常抛出去，不静默降级。**只建表，不往里写任何数据。**
    """

    _run_ddl(conn, _TOPIC_INDEX_QUEUE_DDL)


def _topic_index_text(value: Any) -> str:
    """索引队列身份列的文本归一：``None`` 当空串，两端空白去掉（写入与查询同一套）。"""

    return str(value or "").strip()


# ---------------- 事件日志：工具调用串联列（E1 追加） ----------------
#
# 为什么要一列而不是只塞进 ``detail`` JSON：``tool_call_id`` 是"这次工具调用"的
# 稳定身份，读的时候要能**按它取一行**（回执 ←→ 动作日志），塞进 JSON 就只能
# ``LIKE '%…%'`` 全表扫。所以给 ``event_log`` 加一列 + 一条索引；老库用
# ``ALTER TABLE ... ADD COLUMN`` 补，默认空串 ⇒ 老数据与老调用方逐字节不变。

_EVENT_LOG_TOOL_CALL_ID_COLUMN = "tool_call_id"


def _migrate_event_log_tool_call_id(conn: sqlite3.Connection) -> None:
    """给 ``event_log`` 补 ``tool_call_id`` 列与索引（可重复调用）。

    空库（表还没建）、老库（没有这一列）、已经迁移过的库都能跑；既有数据一行不动。
    只加列与索引，不写任何数据。
    """

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(event_log)")
    }
    if columns and _EVENT_LOG_TOOL_CALL_ID_COLUMN not in columns:
        conn.execute(
            "ALTER TABLE event_log ADD COLUMN tool_call_id TEXT NOT NULL DEFAULT ''"
        )
    if columns:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_tool_call ON event_log (tool_call_id)"
        )
    conn.commit()


# 用户 m30538：``session_id`` 是**状态键**（同组会话被 ``state_key()`` 映射到主会话），
# 所以同一组的几条会话在 ``event_log`` 里共用一行来源；要追溯"这条是谁触发的"
# 只能另存一列真实的 umo。同样是 ALTER 补列、默认空串、老调用方逐字节不变；
# 不给它建索引（读侧是 ``SELECT *`` 全取，没有按它筛的需求）。
_EVENT_LOG_ORIGIN_SESSION_COLUMN = "origin_session"


def _migrate_event_log_origin_session(conn: sqlite3.Connection) -> None:
    """给 ``event_log`` 补 ``origin_session`` 列（可重复调用）。

    空库（表还没建）、老库（没有这一列）、已经迁移过的库都能跑；既有数据一行不动。
    只加列，不写任何数据、不建索引。
    """

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(event_log)")
    }
    if columns and _EVENT_LOG_ORIGIN_SESSION_COLUMN not in columns:
        conn.execute(
            "ALTER TABLE event_log ADD COLUMN origin_session TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()


def _topic_index_scope_filter(scope_id: Any) -> str:
    """会话组过滤值：``None`` / 空串 = **不按会话组过滤**，非空 = 精确相等。

    与邻近的 :func:`get_source_events_by_ids` / :func:`shares_for_sources` 同一规矩
    （那里也是 ``if scope_id: ... AND scope_id = ?``）。注意 ``topics`` 的读接口
    （:func:`list_topics_page` / :func:`get_topic_snapshot`）把空串当**名字就是空串的
    那个组**处理，两种规矩不一样——本分区里的"空串"一律是"不过滤"。
    """

    return _topic_index_text(scope_id)


def _topic_index_row(row: Any) -> dict[str, Any] | None:
    """``topic_index_queue`` 一行 → dict（列名与 DDL 一致，数值列强转回数）。"""

    if row is None:
        return None
    item = dict(row)
    item["id"] = _int_or(item.get("id"), 0)
    item["topic_id"] = str(item.get("topic_id") or "")
    item["information_revision"] = _int_or(item.get("information_revision"), 0)
    item["scope_id"] = str(item.get("scope_id") or "")
    item["text"] = str(item.get("text") or "")
    item["text_hash"] = str(item.get("text_hash") or "")
    item["status"] = str(item.get("status") or "")
    item["attempts"] = _int_or(item.get("attempts"), 0)
    item["next_attempt_at"] = _float_or(item.get("next_attempt_at"), 0.0)
    item["created_at"] = _float_or(item.get("created_at"), 0.0)
    item["updated_at"] = _float_or(item.get("updated_at"), 0.0)
    return item


def enqueue_topic_index(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    information_revision: int,
    text: str,
    text_hash: str,
    scope_id: str = "",
    now: float = 0.0,
) -> bool:
    """入队一行谈资索引作业：**True = 这次真的入队（新插一行，或把死行拉起来）**。

    幂等键是唯一键三列 ``(topic_id, information_revision, text_hash)``：同一份索引文本
    重复入队**不产生第二行**。冲突时按**旧行的 ``status``** 分两种处置：

    * ``failed`` / ``stale`` = **死行，可复活**：同一份索引文本再入队一次就把这行拉回
      ``pending``，并把 ``attempts`` 归零、``next_attempt_at`` 重设成 ``now``（= 立即可取）、
      ``scope_id`` 刷成这次的入参。这两行原本已经没有任何自动路径能再认领
      （:func:`claim_topic_index_due` 只捞 ``pending``），于是"供应商短暂故障烧光重试次数"
      或者"换 provider 之前标过 stale"会让这条谈资**永远没有向量**；复活是它们的唯一出路，
      ``core/embedding_index.TopicIndexQueue.rescan()`` 只要还看得见"这条谈资缺向量"就能救回来。
    * ``pending`` / ``done`` = **一行不动**（幂等语义）：不重置 ``attempts`` / ``status`` /
      ``next_attempt_at``，重复入队不会把已经算完的行重新拉起来，也不会把退避中的行提前。

    换空间（换 provider / 模型 / 维度）不是靠这条语句：索引文本与空间无关、队列键里也没有
    空间，旧空间的 ``done`` 行会继续挡住新空间的重入队。换空间由
    ``core/embedding_index.TopicIndexQueue.rescan()`` **先 clear（清队列 + 删当前空间向量）、
    再用旧指纹删旧空间向量、最后按新空间重建**（见那里的 docstring）。

    认不出（返回 False，一行不动、不抛）的情形：``topic_id`` / ``text_hash`` 去空白后为
    空（没有身份就说不清这行属于谁），``text`` 为空（空文本算出来的是零向量，没有意义）。

    ``scope_id`` 只用于按会话组清空 / 计数，**不参与幂等键**；``now`` 由调用方传
    （库里不自己取时钟，和邻近写函数一致）：新行的 ``created_at`` / ``updated_at`` /
    ``next_attempt_at`` 都是它，也就是"入队即可取"。
    """

    target = _topic_index_text(topic_id)
    digest = _topic_index_text(text_hash)
    body = str(text or "")
    if not target or not digest or not body:
        return False
    stamp = _float_or(now, 0.0)
    # 冲突分支只复活 failed / stale（死行），pending / done 一行不动：
    #   * 复活 = 拉回 pending、attempts 归零、next_attempt_at 设成 now（入队即可取）、
    #     刷新 scope_id / updated_at；text / text_hash / information_revision 是冲突键
    #     本身，created_at 保留（排查"这行是什么时候第一次入的队"要看它）。
    #   * WHERE 不成立时 SQLite 不改这一行，rowcount = 0 → 返回值仍是 False（幂等）。
    cursor = conn.execute(
        "INSERT INTO topic_index_queue (topic_id, information_revision, scope_id, text,"
        " text_hash, status, attempts, next_attempt_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)"
        " ON CONFLICT (topic_id, information_revision, text_hash) DO UPDATE SET"
        " status = excluded.status,"
        " attempts = 0,"
        " next_attempt_at = excluded.next_attempt_at,"
        " updated_at = excluded.updated_at,"
        " scope_id = excluded.scope_id"
        " WHERE topic_index_queue.status IN ('failed', 'stale')",
        (
            target,
            _int_or(information_revision, 0),
            str(scope_id or ""),
            body,
            digest,
            TOPIC_INDEX_QUEUE_DEFAULT_STATUS,
            stamp,
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return bool(cursor.rowcount)


def claim_topic_index_due(
    conn: sqlite3.Connection, *, now: float = 0.0, limit: int = 16
) -> list[dict[str, Any]]:
    """挑出当前 due 的队列行（``status='pending'`` 且 ``next_attempt_at <= now``）。

    按 ``id`` 升序（先入先算），最多 ``limit`` 行（<=0 按 16 算，上限 1000）。

    **"claim" 是只读挑选，不改任何一行**：这里不把行标成"处理中"，所以进程中途死掉
    只会退化成"下一轮再捞一次"，不会留下永远卡在中间状态的行；同一时刻只有一个索引
    循环在跑（``TopicIndexQueue.run_batch`` 是单飞的），不需要行级锁。也正因为不改行，
    它不持有写事务、不长时间占锁。
    """

    wanted = _int_or(limit, 0)
    if wanted <= 0:
        wanted = 16
    wanted = min(wanted, 1000)
    rows = conn.execute(
        "SELECT * FROM topic_index_queue WHERE status = ? AND next_attempt_at <= ?"
        " ORDER BY id LIMIT ?",
        (TOPIC_INDEX_QUEUE_DEFAULT_STATUS, _float_or(now, 0.0), wanted),
    ).fetchall()
    return [_topic_index_row(row) for row in rows]


def mark_topic_index(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    status: str,
    now: float = 0.0,
    attempts: int | None = None,
    next_attempt_at: float | None = None,
) -> bool:
    """改一行队列作业的状态：``done`` / ``failed`` / ``stale`` / ``pending``。

    ``done`` = 向量已落库；``failed`` = 重试次数用尽，不再自动重试；``stale`` = 迟到
    结果（作者谈资的 ``information_revision`` 已经变了），**不写向量**；``pending`` =
    这次失败但还有重试机会（配合 ``attempts + 1`` 与退避后的 ``next_attempt_at``）。

    ``attempts`` / ``next_attempt_at`` 给 ``None`` 就**保持原值**（例如标 ``done`` 时
    不该把重试计数清零，那是排查线索）；``now`` 进 ``updated_at``。

    **True = 真的改了一行**；状态不在白名单、``row_id`` 认不出、行不存在都是 False
    （不抛异常，和邻近写函数一个规矩）。
    """

    wanted = _topic_index_text(status)
    if wanted not in TOPIC_INDEX_QUEUE_STATUSES:
        return False
    ident = _int_or(row_id, 0)
    if ident <= 0:
        return False
    sql = "UPDATE topic_index_queue SET status = ?, updated_at = ?"
    args: list[Any] = [wanted, _float_or(now, 0.0)]
    if attempts is not None:
        sql += ", attempts = ?"
        args.append(max(_int_or(attempts, 0), 0))
    if next_attempt_at is not None:
        sql += ", next_attempt_at = ?"
        args.append(_float_or(next_attempt_at, 0.0))
    sql += " WHERE id = ?"
    args.append(ident)
    cursor = conn.execute(sql, tuple(args))
    conn.commit()
    return bool(cursor.rowcount)


def count_pending_topic_index(conn: sqlite3.Connection, *, scope_id: Any = None) -> int:
    """数还有多少行没算完（``status='pending'``）；空表返回 0。

    ``scope_id`` 不给（``None``）或给空串 = **所有会话组**，给了就只数那一组
    （见 :func:`_topic_index_scope_filter`）。
    """

    sql = "SELECT COUNT(*) FROM topic_index_queue WHERE status = ?"
    args: list[Any] = [TOPIC_INDEX_QUEUE_DEFAULT_STATUS]
    scope = _topic_index_scope_filter(scope_id)
    if scope:
        sql += " AND scope_id = ?"
        args.append(scope)
    return int(conn.execute(sql, tuple(args)).fetchone()[0] or 0)


def clear_topic_index(conn: sqlite3.Connection, *, scope_id: Any = None) -> int:
    """清掉队列行（**所有状态**，不只是 pending），返回删掉的条数。

    ``scope_id`` 不给（``None``）或给空串 = 清所有会话组；给了就只清那一组。
    这里**不动 ``topic_vectors``**——清向量是向量表的事，见
    :func:`delete_topic_vectors_for_scope`（``TopicIndexQueue.clear`` 两个都调）。
    """

    scope = _topic_index_scope_filter(scope_id)
    if scope:
        cursor = conn.execute("DELETE FROM topic_index_queue WHERE scope_id = ?", (scope,))
    else:
        cursor = conn.execute("DELETE FROM topic_index_queue")
    conn.commit()
    return int(cursor.rowcount or 0)


def _topic_facts(details_json: Any) -> list[str]:
    """``topics.details_json`` → 关键事实列表（坏 JSON / 没有 facts 都降级成 ``[]``）。

    ``Topic.to_row()`` 把 ``details`` 与 ``corrections`` 包成一个对象再序列化，关键事实
    在 ``details['facts']``（见 ``core/experience.py`` 的 ``merged.setdefault('facts', ...)``）。
    **所以先读嵌套层，再退回扁平层**：只有嵌套形状才有生产写入方，扁平层是给手写/旧数据
    的兼容兜底——以前这里直接读 ``payload['facts']``，于是生产数据里 facts 永远是 ``[]``，
    索引文本只剩 summary。
    这里只负责把它读出来，**不做任何排序或改写**（索引文本的配方在
    ``core/embedding_index.index_text``，库层不掺和）。
    """

    if isinstance(details_json, (bytes, bytearray, memoryview)):
        details_json = bytes(details_json).decode("utf-8", "replace")
    if not isinstance(details_json, str) or not details_json:
        return []
    try:
        payload = json.loads(details_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    nested = payload.get("details")
    facts = nested.get("facts") if isinstance(nested, dict) else None
    if facts is None:
        # 兼容扁平形状（手写数据/旧行）：只有嵌套层才是生产写入的形状。
        facts = payload.get("facts")
    if isinstance(facts, str):
        return [facts] if facts.strip() else []
    if not isinstance(facts, (list, tuple)):
        return []
    return [str(fact) for fact in facts if str(fact or "").strip()]


def _topic_available_where(
    *, scope_id: Any = "", now: float = 0.0, status: str = TOPIC_AVAILABLE, alias: str = ""
) -> tuple[list[str], list[Any]]:
    """``list_topic_ids_for_scope`` / ``topics_missing_vectors`` 共用的 WHERE 片段。

    ``alias`` 是表的别名（``topics_missing_vectors`` 里是 ``t``），给了就加前缀，免得
    子查询里的列名和外表撞车。``now <= 0`` = **不看有效期**（管理 / 重建索引的口径，
    与 ``list_topics_page`` 的 inventory 模式一致）；``now > 0`` = 只算还没过期的
    （``pinned`` 的永不过期）。``scope_id`` 空 = 所有会话组。
    """

    prefix = (str(alias) + ".") if alias else ""
    where = [prefix + "status = ?"]
    args: list[Any] = [_topic_index_text(status) or TOPIC_AVAILABLE]
    stamp = _float_or(now, 0.0)
    if stamp > 0:
        where.append(
            "(" + prefix + "pinned != 0 OR " + prefix + "expires_at IS NULL"
            " OR " + prefix + "expires_at > ?)"
        )
        args.append(stamp)
    scope = _topic_index_scope_filter(scope_id)
    if scope:
        where.append(prefix + "scope_id = ?")
        args.append(scope)
    return where, args


def _topic_index_source_row(row: Any) -> dict[str, Any]:
    """``topics`` 一行 → 入队作业的原料（``topic_id`` / 版本 / 组 / 摘要 / 关键事实）。"""

    item = _topic_row(row) or {}
    return {
        "topic_id": str(item.get("topic_id") or ""),
        "information_revision": _int_or(item.get("information_revision"), 1),
        "scope_id": str(item.get("scope_id") or ""),
        "summary": str(item.get("summary") or ""),
        "facts": _topic_facts(item.get("details_json")),
    }


def list_topic_ids_for_scope(
    conn: sqlite3.Connection,
    *,
    scope_id: Any = "",
    now: float = 0.0,
    status: str = TOPIC_AVAILABLE,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """分页列"当前有效"的谈资（每项 = 入队要的原料，见 :func:`_topic_index_source_row`）。

    过滤顺序就是 B1 铁律第 4 条要的"**先过滤后召回**"：``scope_id``（空 = 所有组）→
    ``status``（默认只算 ``available``）→ ``now``（``> 0`` 时只算没过期的，见
    :func:`_topic_available_where`）。排序键与 ``list_topics_page`` 同序
    （``updated_at DESC, topic_id DESC``），翻页稳定；``limit`` 上限 1000，``offset``
    负数按 0 算。
    """

    where, args = _topic_available_where(scope_id=scope_id, now=now, status=status)
    rows_wanted = _int_or(limit, 0)
    if rows_wanted <= 0:
        rows_wanted = 200
    rows_wanted = min(rows_wanted, 1000)
    skip = max(_int_or(offset, 0), 0)
    rows = conn.execute(
        "SELECT * FROM topics WHERE " + " AND ".join(where)
        + " ORDER BY updated_at DESC, topic_id DESC LIMIT ? OFFSET ?",
        (*args, rows_wanted, skip),
    ).fetchall()
    return [_topic_index_source_row(row) for row in rows]


def list_topic_vectors_for_topics(
    conn: sqlite3.Connection,
    topic_ids: Iterable[str],
    *,
    provider_id: str,
    space_fingerprint: str,
    recipe_version: str,
    status: Any = TOPIC_VECTOR_DEFAULT_STATUS,
) -> list[dict[str, Any]]:
    """按 ``topic_ids`` 取某个**向量空间**里的向量行（``vector`` 保持原始 BLOB）。

    三个身份列（``provider_id`` / ``space_fingerprint`` / ``recipe_version``）都必须给，
    缺一个就返回 ``[]``——不给自己猜空间（猜错就会拿到不可比的向量）；``status`` 默认
    只要 ``ready``，给空串则不过滤。``topic_ids`` 超过 400 个会自动分块查（sqlite 的
    变量上限），返回顺序按分块顺序，**不重排**（要排序是调用方的事）。
    """

    provider = _topic_vector_text(provider_id)
    fingerprint = _topic_vector_text(space_fingerprint)
    recipe = _topic_vector_text(recipe_version)
    if not provider or not fingerprint or not recipe:
        return []
    wanted = _topic_vector_text(status)
    found: list[dict[str, Any]] = []
    for ids in _id_chunks(topic_ids):
        sql = (
            "SELECT * FROM topic_vectors WHERE provider_id = ? AND space_fingerprint = ?"
            " AND recipe_version = ?"
        )
        args: list[Any] = [provider, fingerprint, recipe]
        if wanted:
            sql += " AND status = ?"
            args.append(wanted)
        sql += " AND topic_id IN (" + ",".join("?" for _ in ids) + ")"
        args.extend(ids)
        found.extend(dict(row) for row in conn.execute(sql, tuple(args)))
    return found


def delete_topic_vectors_for_scope(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    space_fingerprint: str,
    recipe_version: Any = None,
    scope_id: Any = None,
) -> int:
    """清掉某个**会话组 + 某个向量空间**的向量行，返回删掉的条数（"清空索引"用）。

    ``scope_id`` 不给（``None``）或空串 = 不过滤（连**已经查不到谈资的孤儿向量行**一起
    清，这才是"把这个空间清干净"）；给了就只清该组谈资的向量
    （``topic_id IN (SELECT topic_id FROM topics WHERE scope_id = ?)``）。

    ``recipe_version`` 不给（``None``）或空串 = 清这个空间下**所有配方**的行（换配方
    重建时不留旧行）；给了就只清那一个配方。``provider_id`` / ``space_fingerprint``
    缺一个就返回 0、什么都不删——空空间条件不做全表删除。

    **只删向量，不动队列**（队列是 :func:`clear_topic_index` 的事）。
    """

    provider = _topic_vector_text(provider_id)
    fingerprint = _topic_vector_text(space_fingerprint)
    if not provider or not fingerprint:
        return 0
    where = ["provider_id = ?", "space_fingerprint = ?"]
    args: list[Any] = [provider, fingerprint]
    recipe = _topic_vector_text(recipe_version)
    if recipe:
        where.append("recipe_version = ?")
        args.append(recipe)
    scope = _topic_index_scope_filter(scope_id)
    if scope:
        where.append("topic_id IN (SELECT topic_id FROM topics WHERE scope_id = ?)")
        args.append(scope)
    cursor = conn.execute("DELETE FROM topic_vectors WHERE " + " AND ".join(where), tuple(args))
    conn.commit()
    return int(cursor.rowcount or 0)


def topics_missing_vectors(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    space_fingerprint: str,
    recipe_version: str,
    scope_id: Any = "",
    now: float = 0.0,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """列"当前有效、但在这个向量空间里还没有 ``ready`` 向量"的谈资（``rescan`` 用）。

    "没有向量"是拿 ``topics`` 左连接 ``topic_vectors`` 的**反连接**判的，条件是四列
    同时相等：``topic_id`` + ``information_revision`` + ``provider_id`` +
    ``space_fingerprint`` + ``recipe_version``，且 ``status='ready'``。所以：

    * 换了 provider / 模型 / 维度（= 换了 ``space_fingerprint``）之后**全部谈资都算缺**，
      一次 ``rescan`` 就能重建，不需要删旧行（旧空间的行还在原位，互不污染）；
    * 谈资的 ``information_revision`` 变了之后**同一空间也算缺**，会重新入队。

    参数口径与 :func:`list_topic_ids_for_scope` 一致（``scope_id`` 空 = 所有组，
    ``now <= 0`` = 不看有效期），返回项也是同一形状（可直接喂
    ``TopicIndexQueue.enqueue``）。``provider_id`` / ``space_fingerprint`` /
    ``recipe_version`` 缺一个就返回 ``[]``。
    """

    provider = _topic_vector_text(provider_id)
    fingerprint = _topic_vector_text(space_fingerprint)
    recipe = _topic_vector_text(recipe_version)
    if not provider or not fingerprint or not recipe:
        return []
    where, args = _topic_available_where(scope_id=scope_id, now=now, alias="t")
    rows_wanted = _int_or(limit, 0)
    if rows_wanted <= 0:
        rows_wanted = 200
    rows_wanted = min(rows_wanted, 1000)
    sql = (
        "SELECT * FROM topics t WHERE " + " AND ".join(where)
        + " AND NOT EXISTS (SELECT 1 FROM topic_vectors v WHERE v.topic_id = t.topic_id"
        " AND v.information_revision = t.information_revision AND v.provider_id = ?"
        " AND v.space_fingerprint = ? AND v.recipe_version = ? AND v.status = ?)"
        " ORDER BY t.updated_at DESC, t.topic_id DESC LIMIT ?"
    )
    args.extend([provider, fingerprint, recipe, TOPIC_VECTOR_DEFAULT_STATUS, rows_wanted])
    rows = conn.execute(sql, tuple(args)).fetchall()
    return [_topic_index_source_row(row) for row in rows]


def drop_superseded_topic_index(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    information_revision: int,
    text_hash: str,
    provider_id: str = "",
    space_fingerprint: str = "",
    recipe_version: str = "",
) -> dict[str, int]:
    """删掉**同一条谈资**已经用不上的索引行，返回 ``{'queue': n, 'vectors': m}``。

    "索引文本改了要重算"（``TopicIndexQueue.refresh``）专用，**只认这一个 ``topic_id``**，
    别的谈资一行不碰：

    * ``topic_index_queue``：删掉**除目标幂等键之外**的全部行——别的
      ``information_revision``（改版后留下的旧作业）与**同一个 revision 下别的
      ``text_hash``**（改了 summary / details 但 ``information_revision`` 没变，于是旧行
      还是 ``done``／``pending``，:func:`enqueue_topic_index` 的幂等分支一行不动，这条谈资
      的向量就永远停在旧文本上）。目标那一行留着不动，交给 :func:`enqueue_topic_index` 走
      它自己的冲突分支（``pending`` / ``done`` 一行不动、死行复活），这里的删除**不越权**。
    * ``topic_vectors``：删同一个向量空间（``provider_id`` / ``space_fingerprint`` /
      ``recipe_version`` 三列同时相等）里 ``information_revision`` **与目标不等**的行。
      同一个 revision 的行**不删**——向量主键里没有 ``text_hash``，重算时
      :func:`upsert_topic_vector` 原地覆盖就是对的（先删反而会留下"没有向量"的窗口）。
      空间身份三列缺一个就**整段跳过**、一个向量行都不删（空空间条件不做删除，与
      :func:`delete_topic_vectors_for_scope` 同一规矩）。

    认不出（返回两个 0，一行不动、不抛）：``topic_id`` / ``text_hash`` 去空白后为空。
    队列的删除**不需要**空间三列：队列行与向量空间本来就无关（见
    :data:`TOPIC_INDEX_QUEUE_UNIQUE_COLUMNS` 上面的注释）。返回的两个计数是"删掉的条数"，
    调用方只用来观测。
    """

    target = _topic_index_text(topic_id)
    digest = _topic_index_text(text_hash)
    counts = {"queue": 0, "vectors": 0}
    if not target or not digest:
        return counts
    revision = _int_or(information_revision, 0)
    cursor = conn.execute(
        "DELETE FROM topic_index_queue WHERE topic_id = ?"
        " AND (information_revision <> ? OR text_hash <> ?)",
        (target, revision, digest),
    )
    counts["queue"] = int(cursor.rowcount or 0)
    provider = _topic_vector_text(provider_id)
    fingerprint = _topic_vector_text(space_fingerprint)
    recipe = _topic_vector_text(recipe_version)
    if provider and fingerprint and recipe:
        cursor = conn.execute(
            "DELETE FROM topic_vectors WHERE topic_id = ? AND provider_id = ?"
            " AND space_fingerprint = ? AND recipe_version = ? AND information_revision <> ?",
            (target, provider, fingerprint, recipe, revision),
        )
        counts["vectors"] = int(cursor.rowcount or 0)
    conn.commit()
    return counts


# ---------------------------------------------------------------------------
# 阶段M1 追加大分区：表情包收藏与展示记录
#
# 设计见 .vwtest/vw-meme-plan.md §3.2/§3.3/§8。这一批**只建表、只给读写函数**：
# 没有任何生产调用点读它们（M2 才把闸门接进回复路径），管理页接口是唯一使用者。
#
# 身份口径（§1，不可动摇）：
#   * 资产身份 = **内容 sha256**，直接复用 ``core/media_store.py`` 的 ``content_id``
#     ——它就是资产库的 ``media_id``。**不用 URL、不用平台文件 id 做身份**：
#     换链接回来的同一张图必须还是同一条收藏（``meme_entries.media_id`` 去重）。
#   * 一次「出现」（谁在哪条消息里发了这张图）= 一个 ``occurrence_id``，
#     形如 ``"{session}#{seq}#{index}"``，``seq`` 是留档行序号。别人重发同一张图
#     = 新的 occurrence（收藏实体仍按 ``media_id`` 去重）。
#
# 幂等：``_migrate_memes`` 由 ``Database._migrate()`` 调用，空库 / 旧库 / 已迁移的库
# 都能重复跑，既有数据一行不动；失败整体回滚后把异常抛出去（旧库保持可用）。
# ---------------------------------------------------------------------------

# 收藏 ID 的形式：``meme_000042``。补零是为了让字典序 = 数值序，
# 页面上按 ID 排序不会出现 meme_9 排在 meme_10 后面这种别扭。
MEME_ID_PREFIX = "meme_"
MEME_ID_DIGITS = 6

# 展示（出现）记录的状态。语义见 §3.3——**只有 ``delivered`` 封死这一次出现**；
# ``failed``（请求失败 / 超时 / 被预算丢弃）不算展示，下一轮可以重试。
MEME_EXPOSURE_STATES = ("pending", "claimed", "delivered", "failed")
MEME_EXPOSURE_PENDING = "pending"
MEME_EXPOSURE_CLAIMED = "claimed"
MEME_EXPOSURE_DELIVERED = "delivered"
MEME_EXPOSURE_FAILED = "failed"

# 收藏条目的可用状态（与 ``core/meme_settings.AVAILABILITY_STATES`` 同一套口径）
MEME_ENTRY_STATUS_ENABLED = "enabled"
MEME_ENTRY_STATUS_DISABLED = "disabled"
MEME_ENTRY_STATUS_DELETED = "deleted"

# 用法的结果（§6 的回执口径）：尝试过、发出去了、平台回执、被接受
MEME_USAGE_ATTEMPTED = "attempted"
MEME_USAGE_SENT = "sent"
MEME_USAGE_RECEIPT = "receipt"
MEME_USAGE_ACCEPTED = "accepted"
MEME_USAGE_REJECTED = "rejected"
MEME_USAGE_FAILED = "failed"

# pin 引用前缀：收藏钉住的资产不参与 gc（``MediaStore.pin`` 的 ``ref``）
MEME_PIN_REF_PREFIX = "meme:"

# 来源范围的"不限"记号（与 ``core.meme_settings.SCOPE_ANY`` 同一个字面量：
# 这里不 import 那个模块，免得 db 层反向依赖配置层）
MEME_SCOPE_ANY = "*"

_MEME_ASSETS_DDL = """
CREATE TABLE IF NOT EXISTS meme_assets (
    media_id TEXT PRIMARY KEY,
    mime TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    -- 图片尺寸与帧数（M1 只记下来，抽帧/缩放是后面的事）
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    frame_count INTEGER NOT NULL DEFAULT 0,
    is_animated INTEGER NOT NULL DEFAULT 0,
    -- 逗号分隔的标签：M1 只留列，不给编辑入口
    tags TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_meme_assets_created
    ON meme_assets (created_at DESC);
"""

_MEME_ENTRIES_DDL = """
CREATE TABLE IF NOT EXISTS meme_entries (
    entry_id TEXT PRIMARY KEY,
    -- 内容身份：与 meme_assets / 资产库同一个 media_id（sha256）。
    -- 同一条收藏换底层文件要**显式改这一列**，那张图于是换了身份。
    media_id TEXT NOT NULL DEFAULT '',
    -- 世界作用域（默认 '' = 整个世界共用一本收藏）+ 角色（预留，M1 不按它过滤）
    scope_id TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    -- 语义四件套：什么时候用、什么时候别用、她自己的批注
    meaning TEXT NOT NULL DEFAULT '',
    use_when TEXT NOT NULL DEFAULT '',
    avoid_when TEXT NOT NULL DEFAULT '',
    personal_note TEXT NOT NULL DEFAULT '',
    -- 软偏好：越大越优先（0 = 不偏不倚）
    preference REAL NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'enabled',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    deleted_at REAL NOT NULL DEFAULT 0
);
-- 同一本收藏里名字不能撞（名字留空的不算）
CREATE UNIQUE INDEX IF NOT EXISTS idx_meme_entries_scope_name
    ON meme_entries (scope_id, name) WHERE name != '';
-- 一个 media_id 只允许一条没删的收藏：这就是"同字节 → 同一个 media_id、不重复建条目"
CREATE UNIQUE INDEX IF NOT EXISTS idx_meme_entries_media
    ON meme_entries (media_id) WHERE status != 'deleted' AND media_id != '';
CREATE INDEX IF NOT EXISTS idx_meme_entries_scope_status
    ON meme_entries (scope_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_meme_entries_media_id
    ON meme_entries (media_id);
"""

_MEME_SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS meme_sources (
    source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    -- 人发过它的**真实来源**（会话 id / 群号 / 导入来源），与"使用范围"分开存
    origin TEXT NOT NULL DEFAULT '',
    origin_kind TEXT NOT NULL DEFAULT '',
    -- 使用范围：'*' = 不限；否则逗号分隔的 kind / kind:id。
    -- **同资源多来源取交集**（见 core.meme_settings.meme_scope_intersection）：
    -- 多一个来源不能放宽限制。
    allowed_scopes TEXT NOT NULL DEFAULT '*',
    occurred_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_meme_sources_entry ON meme_sources (entry_id);
CREATE INDEX IF NOT EXISTS idx_meme_sources_session ON meme_sources (session_id, occurred_at);
"""

_MEME_VARIANTS_DDL = """
CREATE TABLE IF NOT EXISTS meme_variants (
    variant_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_a TEXT NOT NULL,
    entry_b TEXT NOT NULL,
    -- suspected（形近，可能只是换字） / confirmed（人工确认是同一个梗）
    relation TEXT NOT NULL DEFAULT 'suspected',
    similarity REAL NOT NULL DEFAULT 0,
    -- 差异说明：换字近似图**保留差异**，合并是有意的、可解释的
    diff_note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE (entry_a, entry_b)
);
CREATE INDEX IF NOT EXISTS idx_meme_variants_a ON meme_variants (entry_a);
CREATE INDEX IF NOT EXISTS idx_meme_variants_b ON meme_variants (entry_b);
"""

_MEME_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS meme_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    phase_id TEXT NOT NULL DEFAULT '',
    -- attempted（想发/试过） / sent（发出去了） / receipt（平台回执）
    -- / accepted（真正被接受） / rejected / failed
    kind TEXT NOT NULL DEFAULT 'attempted',
    outcome TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_meme_usage_entry ON meme_usage (entry_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_meme_usage_session ON meme_usage (session_id, created_at DESC);
"""

# 严格按方案 §3.3 的 DDL：主键 ``(occurrence_id, phase_id)`` 就是"防并发双端补附"
# 的那把锁（``claimed`` 靠它挡住两个端同时给同一条回复补图）。
_MEME_EXPOSURES_DDL = """
CREATE TABLE IF NOT EXISTS image_exposures (
    occurrence_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    media_id TEXT,
    phase_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    claimed_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (occurrence_id, phase_id)
);
CREATE INDEX IF NOT EXISTS idx_image_exposures_state
    ON image_exposures (state, updated_at);
CREATE INDEX IF NOT EXISTS idx_image_exposures_media
    ON image_exposures (media_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_image_exposures_session
    ON image_exposures (session_id, updated_at);
"""

# 攒图评审收藏（用户 2026-10-24）：她"看到过"的图先进候选池，攒够一批再拼图评审。
# 唯一性**只约束还在池里的候选**（``status = 'pending'`` 的部分唯一索引）：评审完置
# collected / passed 之后，同一张图还能重新进池——passed 不是永久否决，超过
# ``candidate_ttl_days`` 且她再次看到时可以重置成 pending（见 ``note_seen``）。
# collected 仍永久排除：``meme_entries`` 的媒体唯一索引已经兜住收藏去重。
# ``ref_index`` / ``sheet_id`` 是"这一批评审里它是候选几"的记账。
_MEME_COLLECT_DDL = """
CREATE TABLE IF NOT EXISTS meme_collect_candidates (
    candidate_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    media_id TEXT NOT NULL,
    caption TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'chat',
    status TEXT NOT NULL DEFAULT 'pending',
    seen_at REAL NOT NULL DEFAULT 0,
    reviewed_at REAL NOT NULL DEFAULT 0,
    sheet_id TEXT NOT NULL DEFAULT '',
    ref_index INTEGER NOT NULL DEFAULT 0,
    entry_id TEXT NOT NULL DEFAULT ''
);
-- 老库上可能建过"media_id 全量唯一"的索引：先拆掉，再建只约束 pending 的部分索引
DROP INDEX IF EXISTS idx_meme_collect_media;
CREATE UNIQUE INDEX IF NOT EXISTS idx_meme_collect_media
    ON meme_collect_candidates (media_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_meme_collect_session
    ON meme_collect_candidates (session_id, status, seen_at);
CREATE INDEX IF NOT EXISTS idx_meme_collect_sheet
    ON meme_collect_candidates (sheet_id, ref_index);

CREATE TABLE IF NOT EXISTS meme_collect_reviews (
    review_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    sheets INTEGER NOT NULL DEFAULT 0,
    offered INTEGER NOT NULL DEFAULT 0,
    picked INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'done',
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_meme_collect_reviews_session
    ON meme_collect_reviews (session_id, created_at);
"""

# B3：Agent 任务账本与产物台账（方案 `.vwtest/b3-executor-plan.md` §1）。
# ``steps`` 存 JSON 文本：一步一行，只记"调了什么、成没成、花了多久"，**不存正文**
# （正文在 ``tool_receipt`` 回执里，已有裁剪）。`task_id` 与 ``ToolResult.task_id`` 同域。
_AGENT_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    steps TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0,
    -- P1-1：结构化任务包与等待态要用的列（开发期自愈会补齐老库）
    schema_version INTEGER NOT NULL DEFAULT 1,
    brief_json TEXT NOT NULL DEFAULT '',
    checkpoint_json TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    wait_reason TEXT NOT NULL DEFAULT '',
    active_elapsed REAL NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0,
    parent_task_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_session
    ON agent_tasks (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status
    ON agent_tasks (status, updated_at DESC);
CREATE TABLE IF NOT EXISTS agent_artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    mime TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0,
    sent_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_artifacts_task
    ON agent_artifacts (task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_artifacts_expiry
    ON agent_artifacts (expires_at);
CREATE TABLE IF NOT EXISTS task_questions (
    question_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL DEFAULT '',
    task_revision INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'info',
    payload_json TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'asked',
    scope TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    asker TEXT NOT NULL DEFAULT '',
    delivery_status TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    answer_source TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0,
    answered_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_task_questions_open
    ON task_questions (scope, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_questions_task
    ON task_questions (task_id, created_at DESC);
CREATE TABLE IF NOT EXISTS capability_experiences (
    experience_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    situation TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',
    limitations TEXT NOT NULL DEFAULT '',
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    dependency_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    revision INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'agent',
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_capability_experiences_lookup
    ON capability_experiences (scope, status, updated_at DESC);
""" + VW_SKILLS_TABLE_SQL


def _migrate_agent_tasks(conn: sqlite3.Connection) -> None:
    """建 Agent 任务/产物两张表与索引（可重复调用，**只建表不写数据**）。"""

    _run_ddl(conn, _AGENT_TASKS_DDL)


_FOLLOW_UPS_DDL = """
CREATE TABLE IF NOT EXISTS follow_ups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    scope_id TEXT NOT NULL DEFAULT '',
    said_text TEXT NOT NULL DEFAULT '',
    target_user TEXT NOT NULL DEFAULT '',
    due_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_times INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'open',
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_follow_ups_due
    ON follow_ups (status, due_at);
CREATE INDEX IF NOT EXISTS idx_follow_ups_session
    ON follow_ups (session_id, status, created_at DESC);
"""


def _migrate_follow_ups(conn: sqlite3.Connection) -> None:
    """建追问表（可重复调用，**只建表不写数据**），老库补上"在等谁"那一列。"""

    _run_ddl(conn, _FOLLOW_UPS_DDL)
    try:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(follow_ups)").fetchall()
        }
    except sqlite3.Error:
        return
    if "target_user" not in columns:
        # 老库：加一列，已有行留空（空 = 没记下具体是谁，按"谁都算接话"处理）
        try:
            conn.execute(
                "ALTER TABLE follow_ups ADD COLUMN target_user TEXT NOT NULL DEFAULT ''"
            )
            conn.commit()
        except sqlite3.Error:
            pass


_TEMP_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS temp_sessions (
    umo TEXT PRIMARY KEY,
    origin TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'private',
    state TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER NOT NULL DEFAULT 0,
    rounds INTEGER NOT NULL DEFAULT 0,
    ended_at INTEGER NOT NULL DEFAULT 0,
    end_reason TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    relation_at_end TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_temp_sessions_state
    ON temp_sessions (state, expires_at);
"""

# 后加的列一律走这里补（老库 ALTER 上去，新库建表时就已经有了）。
_TEMP_SESSIONS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("origin", "TEXT NOT NULL DEFAULT ''"),
    ("source", "TEXT NOT NULL DEFAULT 'private'"),
    ("state", "TEXT NOT NULL DEFAULT 'active'"),
    ("created_at", "INTEGER NOT NULL DEFAULT 0"),
    ("expires_at", "INTEGER NOT NULL DEFAULT 0"),
    ("rounds", "INTEGER NOT NULL DEFAULT 0"),
    ("ended_at", "INTEGER NOT NULL DEFAULT 0"),
    ("end_reason", "TEXT NOT NULL DEFAULT ''"),
    ("summary", "TEXT NOT NULL DEFAULT ''"),
    ("relation_at_end", "TEXT NOT NULL DEFAULT ''"),
    ("archived", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate_temp_sessions(conn: sqlite3.Connection) -> None:
    """建临时会话表（可重复调用，**只建表不写数据**）。"""

    _run_ddl(conn, _TEMP_SESSIONS_DDL)
    try:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(temp_sessions)").fetchall()
        }
    except sqlite3.Error:
        return
    if not columns:
        return
    for name, ddl in _TEMP_SESSIONS_COLUMNS:
        if name in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE temp_sessions ADD COLUMN {name} {ddl}")
            conn.commit()
        except sqlite3.Error:
            pass


# ---------------- 「等会儿」的待办（用户 m00001） ----------------
#
# 她协商着说"等会儿"时，agency 会给一个 deferred_at（15/30/45/60 分钟）。这里把
# 它落成一条 pending：**同一个会话同时最多一条**。到点由引擎的
# ``sweep_commitments`` 写回 ``state.intention`` 提醒她；提醒满次数还没动就
# dropped（不再纠缠，避免复读机）。状态只有三种：
# ``pending`` → ``done``（她真做了）/ ``dropped``（忘了，不再提）。

_DEFERRED_COMMITMENTS_DDL = """
CREATE TABLE IF NOT EXISTS deferred_commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    task_text TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    due_at REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending',
    nudges INTEGER NOT NULL DEFAULT 0,
    last_nudge_at REAL NOT NULL DEFAULT 0,
    closed_at REAL NOT NULL DEFAULT 0,
    close_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_deferred_commitments_due
    ON deferred_commitments (session_id, state, due_at);
"""

# 后加的列一律走这里补（老库 ALTER 上去，新库建表时就已经有了）。
_DEFERRED_COMMITMENTS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("session_id", "TEXT NOT NULL DEFAULT ''"),
    ("origin", "TEXT NOT NULL DEFAULT ''"),
    ("task_text", "TEXT NOT NULL DEFAULT ''"),
    ("created_at", "REAL NOT NULL DEFAULT 0"),
    ("due_at", "REAL NOT NULL DEFAULT 0"),
    ("state", "TEXT NOT NULL DEFAULT 'pending'"),
    ("nudges", "INTEGER NOT NULL DEFAULT 0"),
    ("last_nudge_at", "REAL NOT NULL DEFAULT 0"),
    ("closed_at", "REAL NOT NULL DEFAULT 0"),
    ("close_reason", "TEXT NOT NULL DEFAULT ''"),
)


def _migrate_deferred_commitments(conn: sqlite3.Connection) -> None:
    """建「等会儿」待办表（可重复调用，**只建表不写数据**）。"""

    _run_ddl(conn, _DEFERRED_COMMITMENTS_DDL)
    try:
        columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(deferred_commitments)"
            ).fetchall()
        }
    except sqlite3.Error:
        return
    if not columns:
        return
    for name, ddl in _DEFERRED_COMMITMENTS_COLUMNS:
        if name in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE deferred_commitments ADD COLUMN {name} {ddl}")
            conn.commit()
        except sqlite3.Error:
            pass


_MEMES_DDL = (
    _MEME_ASSETS_DDL
    + _MEME_ENTRIES_DDL
    + _MEME_SOURCES_DDL
    + _MEME_VARIANTS_DDL
    + _MEME_USAGE_DDL
    + _MEME_EXPOSURES_DDL
)


# 阶段M4 追加：meme_vectors（表情包向量，独立成表、授权独立；方案 §12.1 第 3 条）。
# 只建表/建索引/给读写函数，**不索引任何数据**（索引是引擎的事）。
_MEME_VECTORS_DDL = """
CREATE TABLE IF NOT EXISTS meme_vectors (
    entry_id TEXT NOT NULL,
    information_revision INTEGER NOT NULL DEFAULT 1,
    provider_id TEXT NOT NULL,
    space_fingerprint TEXT NOT NULL,
    recipe_version TEXT NOT NULL,
    dim INTEGER NOT NULL DEFAULT 0,
    text_hash TEXT NOT NULL DEFAULT '',
    vector BLOB NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    persona_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (entry_id, information_revision, provider_id, space_fingerprint, recipe_version)
);
CREATE INDEX IF NOT EXISTS idx_meme_vectors_space
    ON meme_vectors (provider_id, space_fingerprint, status);
"""


def _migrate_meme_vectors(conn: sqlite3.Connection) -> None:
    """建表情包向量表与索引（可重复调用，只建表不写数据）。"""

    _run_ddl(conn, _MEME_VECTORS_DDL)


# 阶段E2 追加：daily_experiences（每日经历账，设计稿 §4）。只建表/建索引/给读写函数，
# **不写任何数据**（记录发生在引擎的真实动作/工具/回执边界上）。
_DAILY_EXPERIENCES_DDL = """
CREATE TABLE IF NOT EXISTS daily_experiences (
    experience_id TEXT PRIMARY KEY,
    experience_day TEXT NOT NULL DEFAULT '',
    activity_kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'planned',
    started_at REAL NOT NULL DEFAULT 0,
    ended_at REAL NOT NULL DEFAULT 0,
    location_id TEXT NOT NULL DEFAULT '',
    location_name TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    short_text TEXT NOT NULL DEFAULT '',
    self_view TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT '',
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    related_task_id TEXT NOT NULL DEFAULT '',
    related_action_id TEXT NOT NULL DEFAULT '',
    related_tool_call_ids TEXT NOT NULL DEFAULT '[]',
    parent_id TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    send_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_daily_experiences_day
    ON daily_experiences (experience_day, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_daily_experiences_session
    ON daily_experiences (session_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_daily_experiences_status
    ON daily_experiences (status, updated_at DESC);
"""


def _migrate_daily_experiences(conn: sqlite3.Connection) -> None:
    """建每日经历账表与索引（可重复调用，只建表不写数据）。"""

    _run_ddl(conn, _DAILY_EXPERIENCES_DDL)


def _migrate_memes(conn: sqlite3.Connection) -> None:
    """建表情包那六张表与各自的索引（可重复调用）。

    空库、没有这些表的旧库、已经迁移过的库都能跑，既有数据一行不动；失败整体回滚
    （旧库保持可用）后把异常抛出去，不静默降级。**只建表，不往里写任何数据。**
    """

    _run_ddl(conn, _MEMES_DDL)


def _migrate_meme_collect(conn: sqlite3.Connection) -> None:
    """建候选池与评审记录两张表（可重复调用，只建表不写数据）。"""

    _run_ddl(conn, _MEME_COLLECT_DDL)


# ---------------------------------------------------------------------------
# 阶段M4 分区：表情包向量的读写（照 topic_vectors 的口径，独立成表）
#
# 三条纪律与谈资那边一致：① 空间身份三列缺一个就**不查**（猜错空间会拿到不可比的
# 向量，比没有向量更糟）；② 写进去的向量要跟"当时那份索引文本"绑在一起
# （``text_hash``），文本变了要能认出旧行；③ 只读路径**永不写库**。
# ---------------------------------------------------------------------------


def _meme_vector_blob(vector: Any) -> bytes | None:
    """向量落库前的字节化：``bytes`` 原样、浮点序列用 :func:`pack_vector`，其余 ``None``。

    两种输入都要认（与 ``MemeStore.load_vectors`` 的读侧口径对称）：引擎那边可能
    已经拿到打包好的字节，也可能手里就是浮点序列。
    """

    if isinstance(vector, (bytes, bytearray, memoryview)):
        return bytes(vector) or None
    try:
        return pack_vector(vector) or None
    except (TypeError, ValueError):
        return None


def list_meme_vectors_for_entries(
    conn: sqlite3.Connection,
    entry_ids: Iterable[str],
    *,
    provider_id: str,
    space_fingerprint: str,
    recipe_version: str = "",
    status: Any = "ready",
) -> list[dict[str, Any]]:
    """按收藏编号取某个**向量空间**里的向量行（``vector`` 保持原始 BLOB）。

    ``provider_id`` 与 ``space_fingerprint`` 必须给；``recipe_version`` 给空串时
    **不过滤配方**（调用方明确要"这个空间里所有配方"时才这么用）。``entry_ids``
    超过 400 个自动分块（sqlite 变量上限），**不重排**（排序是调用方的事）。
    """

    provider = _topic_vector_text(provider_id)
    fingerprint = _topic_vector_text(space_fingerprint)
    if not provider or not fingerprint:
        return []
    recipe = _topic_vector_text(recipe_version)
    wanted = _topic_vector_text(status)
    found: list[dict[str, Any]] = []
    for ids in _id_chunks(entry_ids):
        sql = (
            "SELECT * FROM meme_vectors WHERE provider_id = ? AND space_fingerprint = ?"
        )
        args: list[Any] = [provider, fingerprint]
        if recipe:
            sql += " AND recipe_version = ?"
            args.append(recipe)
        if wanted:
            sql += " AND status = ?"
            args.append(wanted)
        sql += " AND entry_id IN (" + ",".join("?" for _ in ids) + ")"
        args.extend(ids)
        found.extend(dict(row) for row in conn.execute(sql, tuple(args)))
    return found


def upsert_meme_vector(
    conn: sqlite3.Connection,
    *,
    entry_id: Any,
    information_revision: Any = 1,
    provider_id: Any,
    space_fingerprint: Any,
    recipe_version: Any,
    vector: Any,
    dim: Any = 0,
    text_hash: Any = "",
    status: Any = "ready",
    persona_id: Any = "",
    now: Any = 0.0,
) -> bool:
    """写一条表情包向量（同键覆盖），返回是否写入。

    认不出（返回 ``False``、一行不动、不抛）：编号/服务商/空间指纹/配方有一个为空，
    或者 ``vector`` 是空的——没有身份或没有向量的行写进去只会变成垃圾。
    """

    entry = _topic_vector_text(entry_id)
    provider = _topic_vector_text(provider_id)
    fingerprint = _topic_vector_text(space_fingerprint)
    recipe = _topic_vector_text(recipe_version)
    if not entry or not provider or not fingerprint or not recipe:
        return False
    payload = _meme_vector_blob(vector)
    if payload is None:
        return False
    stamp = _now_or(now, 0.0)
    conn.execute(
        "INSERT INTO meme_vectors (entry_id, information_revision, provider_id,"
        " space_fingerprint, recipe_version, dim, text_hash, vector, status, persona_id,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (entry_id, information_revision, provider_id, space_fingerprint,"
        " recipe_version) DO UPDATE SET dim = excluded.dim, text_hash = excluded.text_hash,"
        " vector = excluded.vector, status = excluded.status,"
        " persona_id = excluded.persona_id, updated_at = excluded.updated_at",
        (
            entry,
            _meme_int(_int_or(information_revision, 1), 1),
            provider,
            fingerprint,
            recipe,
            _meme_int(dim, 0),
            _topic_vector_text(text_hash),
            payload,
            _topic_vector_text(status) or "ready",
            _topic_vector_text(persona_id),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return True


def delete_meme_vectors_for_entries(
    conn: sqlite3.Connection,
    entry_ids: Iterable[str],
    *,
    provider_id: Any = "",
    space_fingerprint: Any = "",
    recipe_version: Any = "",
) -> int:
    """删掉这些收藏的向量行，返回删了几条。

    空间三列给得越全删得越窄；一个都不给 = 这个收藏在**所有**空间里的向量都删
    （"这条收藏没了"时才该这么用）。``entry_ids`` 为空返回 0。
    """

    ids = [text for text in (_topic_vector_text(item) for item in entry_ids) if text]
    if not ids:
        return 0
    removed = 0
    for chunk in _id_chunks(ids):
        sql = "DELETE FROM meme_vectors WHERE entry_id IN (" + ",".join("?" for _ in chunk) + ")"
        args: list[Any] = list(chunk)
        for column, value in (
            ("provider_id", provider_id),
            ("space_fingerprint", space_fingerprint),
            ("recipe_version", recipe_version),
        ):
            text = _topic_vector_text(value)
            if text:
                sql += f" AND {column} = ?"
                args.append(text)
        removed += int(conn.execute(sql, tuple(args)).rowcount or 0)
    conn.commit()
    return removed


def count_meme_vectors(
    conn: sqlite3.Connection,
    *,
    provider_id: Any = "",
    space_fingerprint: Any = "",
    status: Any = "",
) -> int:
    """数一数表情包向量行（三个条件都可选：不给就全库数）。"""

    sql = "SELECT COUNT(*) FROM meme_vectors WHERE 1 = 1"
    args: list[Any] = []
    for column, value in (
        ("provider_id", provider_id),
        ("space_fingerprint", space_fingerprint),
        ("status", status),
    ):
        text = _topic_vector_text(value)
        if text:
            sql += f" AND {column} = ?"
            args.append(text)
    row = conn.execute(sql, tuple(args)).fetchone()
    return int(row[0] or 0) if row else 0


def meme_entries_missing_vectors(
    conn: sqlite3.Connection,
    *,
    provider_id: Any,
    space_fingerprint: Any,
    recipe_version: Any,
    limit: Any = 200,
    now: Any = 0.0,
) -> list[dict[str, Any]]:
    """哪些**可用**收藏在这个空间里还没有 ready 向量（后台补索引用）。

    只挑 ``status='enabled'`` 的收藏：停用/软删的不该占索引额度。返回行带
    ``entry_id`` / ``information_revision`` / ``media_id``，按最近更新排。
    """

    provider = _topic_vector_text(provider_id)
    fingerprint = _topic_vector_text(space_fingerprint)
    recipe = _topic_vector_text(recipe_version)
    if not provider or not fingerprint or not recipe:
        return []
    wanted = max(1, min(2000, _meme_int(limit, 200)))
    rows = conn.execute(
        # ⚠️ 收藏表里那一列叫 ``revision``（内容/语义改了几次），不是谈资那边的
        # ``information_revision``；向量行沿用 ``information_revision`` 这个名字，
        # 所以这里显式对齐两侧。
        "SELECT e.entry_id, e.revision AS information_revision, e.media_id, e.updated_at"
        " FROM meme_entries e"
        " WHERE e.status = 'enabled' AND NOT EXISTS ("
        "   SELECT 1 FROM meme_vectors v WHERE v.entry_id = e.entry_id"
        "   AND v.information_revision = e.revision"
        "   AND v.provider_id = ? AND v.space_fingerprint = ? AND v.recipe_version = ?"
        "   AND v.status = 'ready')"
        " ORDER BY e.updated_at DESC LIMIT ?",
        (provider, fingerprint, recipe, wanted),
    ).fetchall()
    return [dict(row) for row in rows]


def update_meme_variant_relation(
    conn: sqlite3.Connection,
    entry_a: Any,
    entry_b: Any,
    *,
    relation: Any,
    similarity: Any = None,
    diff_note: Any = None,
    now: Any = 0.0,
) -> dict[str, Any] | None:
    """改一条**已存在**的变体关系（例如把 ``suspected`` 升级成 ``confirmed``）。

    为什么需要它：``add_meme_variant`` 对已存在的行**只回不改**（那是它的幂等语义），
    所以"人工确认"这条路以前升不了级。这里只改 ``relation`` / ``similarity`` /
    ``diff_note`` / ``updated_at``，**不动** 两条 ``entry`` 与关系的主键；认不出来
    （关系行不存在 / 编号为空）回 ``None``。
    """

    first = _meme_text(entry_a)
    second = _meme_text(entry_b)
    wanted = _meme_text(relation)
    if not first or not second or not wanted:
        return None
    pair = (first, second) if first <= second else (second, first)
    existing = conn.execute(
        "SELECT * FROM meme_variants WHERE entry_a = ? AND entry_b = ?", pair
    ).fetchone()
    if existing is None:
        return None
    fields: dict[str, Any] = {"relation": wanted, "updated_at": _now_or(now, 0.0)}
    if similarity is not None:
        fields["similarity"] = _now_or(similarity, 0.0)
    if diff_note is not None:
        fields["diff_note"] = _meme_text(diff_note)
    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE meme_variants SET {assignments} WHERE entry_a = ? AND entry_b = ?",
        (*fields.values(), *pair),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM meme_variants WHERE entry_a = ? AND entry_b = ?", pair
    ).fetchone()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 阶段E2 分区：每日经历账（设计稿 §4/§5）
#
# 三条纪律：① 同一 ``experience_id`` 只更新不新增（重试不产生第二段经历）；
# ② 状态与时间戳由调用方（`core/experience_ledger.py` 的纯逻辑）算好交进来，
# 这一层不判断状态机；③ 落库的文本只可能是"已确认事实"——这里不认识模型输出。
# ---------------------------------------------------------------------------


def _experience_row(row: Any) -> dict[str, Any]:
    """库表行 → 经历字典（JSON 列翻成 list/dict，坏值当空）。"""

    item = dict(row)
    for key in ("source_event_ids", "related_tool_call_ids"):
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = []
            item[key] = list(parsed) if isinstance(parsed, list) else []
        elif not isinstance(raw, list):
            item[key] = []
    send = item.get("send_json")
    if isinstance(send, str) and send.strip():
        try:
            parsed = json.loads(send)
        except json.JSONDecodeError:
            parsed = {}
        item["send"] = dict(parsed) if isinstance(parsed, dict) else {}
    elif not isinstance(send, dict):
        item["send"] = {}
    item.pop("send_json", None)
    return item


def save_experience(conn: sqlite3.Connection, experience: Mapping[str, Any]) -> dict[str, Any] | None:
    """存一条经历（按 ``experience_id`` upsert；没有 id 就当垃圾不写）。

    ⚠️ **终态不许被改口**：库里已经是终态（``succeeded``/``failed``/``cancelled``）时，
    这次写入只更新文本与 ``send``，状态与 ``ended_at`` 保持库里那份——纯逻辑已经保证
    幂等，这里再挡一道（并发收尾、重放老调用方都不会把"失败"洗成"成功"）。
    """

    key = _meme_text(experience.get("experience_id"))
    if not key:
        return None
    existing = get_experience(conn, key)
    terminal = {"succeeded", "failed", "cancelled"}
    status = _meme_text(experience.get("status")) or "planned"
    ended = _now_or(experience.get("ended_at"))
    if existing is not None and _meme_text(existing.get("status")) in terminal:
        status = _meme_text(existing.get("status"))
        ended = _now_or(existing.get("ended_at"))
    payload = {
        "experience_id": key,
        "experience_day": _meme_text(experience.get("experience_day")),
        "activity_kind": _meme_text(experience.get("activity_kind")),
        "status": status,
        "started_at": _now_or(experience.get("started_at")),
        "ended_at": ended,
        "location_id": _meme_text(experience.get("location_id")),
        "location_name": _meme_text(experience.get("location_name")),
        "persona_id": _meme_text(experience.get("persona_id")),
        "session_id": _meme_text(experience.get("session_id")),
        "short_text": _meme_text(experience.get("short_text")),
        "self_view": _meme_text(experience.get("self_view")),
        "result_summary": _meme_text(experience.get("result_summary")),
        "source_event_ids": json.dumps(
            [str(item) for item in (experience.get("source_event_ids") or []) if str(item)],
            ensure_ascii=False,
        ),
        "related_task_id": _meme_text(experience.get("related_task_id")),
        "related_action_id": _meme_text(experience.get("related_action_id")),
        "related_tool_call_ids": json.dumps(
            [str(item) for item in (experience.get("related_tool_call_ids") or []) if str(item)],
            ensure_ascii=False,
        ),
        "parent_id": _meme_text(experience.get("parent_id")),
        "revision": max(1, _meme_int(experience.get("revision"), 1)),
        "send_json": json.dumps(dict(experience.get("send") or {}), ensure_ascii=False),
        "created_at": _now_or(experience.get("created_at")),
        "updated_at": _now_or(experience.get("updated_at")),
    }
    conn.execute(
        "INSERT INTO daily_experiences (experience_id, experience_day, activity_kind, status,"
        " started_at, ended_at, location_id, location_name, persona_id, session_id, short_text,"
        " self_view, result_summary, source_event_ids, related_task_id, related_action_id,"
        " related_tool_call_ids, parent_id, revision, send_json, created_at, updated_at)"
        " VALUES (:experience_id, :experience_day, :activity_kind, :status, :started_at,"
        " :ended_at, :location_id, :location_name, :persona_id, :session_id, :short_text,"
        " :self_view, :result_summary, :source_event_ids, :related_task_id, :related_action_id,"
        " :related_tool_call_ids, :parent_id, :revision, :send_json, :created_at, :updated_at)"
        " ON CONFLICT(experience_id) DO UPDATE SET"
        " experience_day = excluded.experience_day, activity_kind = excluded.activity_kind,"
        " status = excluded.status, started_at = excluded.started_at, ended_at = excluded.ended_at,"
        " location_id = excluded.location_id, location_name = excluded.location_name,"
        " persona_id = excluded.persona_id, session_id = excluded.session_id,"
        " short_text = excluded.short_text, self_view = excluded.self_view,"
        " result_summary = excluded.result_summary,"
        " source_event_ids = excluded.source_event_ids,"
        " related_task_id = excluded.related_task_id,"
        " related_action_id = excluded.related_action_id,"
        " related_tool_call_ids = excluded.related_tool_call_ids,"
        " parent_id = excluded.parent_id, revision = excluded.revision,"
        " send_json = excluded.send_json, updated_at = excluded.updated_at",
        payload,
    )
    conn.commit()
    return get_experience(conn, key)


def get_experience(conn: sqlite3.Connection, experience_id: Any) -> dict[str, Any] | None:
    """按编号取一条经历（没有就是 ``None``）。"""

    key = _meme_text(experience_id)
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM daily_experiences WHERE experience_id = ?", (key,)
    ).fetchone()
    return _experience_row(row) if row is not None else None


def list_experiences(
    conn: sqlite3.Connection,
    *,
    day: Any = "",
    session_id: Any = "",
    persona_id: Any = "",
    status: Any = "",
    limit: Any = 50,
) -> list[dict[str, Any]]:
    """按世界日 / 会话 / 人格 / 状态取经历（时间倒序）。

    ``day`` 给空串 = 不限日（"今天到现在"由调用方给 ``world_day_key`` 算出来的那一天）。
    """

    sql = "SELECT * FROM daily_experiences WHERE 1 = 1"
    params: list[Any] = []
    for column, value in (
        ("experience_day", day),
        ("session_id", session_id),
        ("persona_id", persona_id),
        ("status", status),
    ):
        text = _meme_text(value)
        if text:
            sql += f" AND {column} = ?"
            params.append(text)
    sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
    params.append(max(1, min(500, _meme_int(limit, 50))))
    return [_experience_row(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def count_experiences(conn: sqlite3.Connection, *, day: Any = "", session_id: Any = "") -> int:
    """数一数经历（可只数某一天 / 某个会话）。"""

    sql = "SELECT COUNT(*) FROM daily_experiences WHERE 1 = 1"
    params: list[Any] = []
    for column, value in (("experience_day", day), ("session_id", session_id)):
        text = _meme_text(value)
        if text:
            sql += f" AND {column} = ?"
            params.append(text)
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(row[0] or 0) if row else 0


def prune_experiences(conn: sqlite3.Connection, *, before_day: Any, keep_days_note: str = "") -> int:
    """删掉 ``before_day`` 之前的经历（按世界日字符串比大小，``YYYY-MM-DD`` 可直接比）。

    ``before_day`` 为空就不删（调用方给的是"保留 N 天"算出来的那一天）。
    """

    floor = _meme_text(before_day)
    if not floor:
        return 0
    removed = int(
        conn.execute(
            "DELETE FROM daily_experiences WHERE experience_day <> '' AND experience_day < ?",
            (floor,),
        ).rowcount
        or 0
    )
    conn.commit()
    return removed


def prune_orphan_events(
    conn: sqlite3.Connection, *, before: float, limit: int = 1000
) -> int:
    """清掉**没有会话归属**（``session_id = ''``）且早于 ``before`` 的事件行，返回删了几条。

    为什么需要它（B1 收尾第 3 条）：``prune_session_events`` 是按会话裁的，会话为空就
    直接返回 0；而**页面级操作**（管理员在表情包页上传图，失败时那条 ``media_cache``）
    没有会话可写，于是那些行既不参与会话裁剪、也没有别的清理路径——会一直堆着。
    这里给它们一个按时间的出口（和会话级裁剪是**两个池子**，互不影响）。

    ``before`` 是 epoch 秒；``<= 0`` 或非数（NaN）直接返回 0、**一行不删**（调用方算不出
    截止时刻时不许清库）。``limit`` 夹在 1..10000，只删最老的那么多条——一次别把库按住
    太久（下一轮再接着删）。
    """

    try:
        cutoff = float(before)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(cutoff) or cutoff <= 0:
        return 0
    wanted = max(1, min(10000, _int_or(limit, 1000)))
    removed = int(
        conn.execute(
            "DELETE FROM event_log WHERE id IN ("
            " SELECT id FROM event_log WHERE session_id = '' AND created_at < ?"
            " ORDER BY id ASC LIMIT ?)",
            (cutoff, wanted),
        ).rowcount
        or 0
    )
    conn.commit()
    return removed


def _now_or(value: Any, default: float = 0.0) -> float:
    """时间戳容错：``float()`` 不了就用默认值（坏值不抛）。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------------------
# 阶段B3 追加分区：Agent 任务账本与产物台账（只增表 / 只给读写函数）
#
# 只做三件事：把 `core/agent_tasks.py` 那份**纯逻辑**算出来的任务存下来、按会话/状态
# 读出来、把产物登记与"发出去了"记上。**判断状态的逻辑一行都不在这里**（那在纯逻辑
# 模块里，可单测）；这里只负责"存得住、读得回、越界不崩"。
# ---------------------------------------------------------------------------


def _agent_task_row(row: Any) -> dict[str, Any]:
    """库表行 → 任务字典（``steps`` 翻成 list，坏 JSON 当空表）。"""

    item = dict(row)
    item["steps"] = agent_tasks.steps_of(item)
    return item


def claim_waiting_task(
    conn: sqlite3.Connection, task_id: str, revision: int, *, now: Any = 0.0
) -> bool:
    """把"可以接着做"的任务原子地拉回 running；True = 这次抢到了。"""

    key = _config_index_text(task_id)
    if not key:
        return False
    stamp = _float_or(now, 0.0)
    cursor = conn.execute(
        "UPDATE agent_tasks SET status = 'running', revision = revision + 1,"
        " expires_at = 0, wait_reason = '', updated_at = ?"
        " WHERE task_id = ? AND revision = ?"
        " AND status IN ('resume_ready', 'waiting_vw')",
        (stamp, key, int(revision)),
    )
    conn.commit()
    return bool(cursor.rowcount)


def list_waiting_tasks(
    conn: sqlite3.Connection, *, session_id: str = "", limit: int = 20
) -> list[dict[str, Any]]:
    """等着的任务（等 VW / 可恢复 / 暂缓），最近更新的在前。"""

    sql = "SELECT * FROM agent_tasks WHERE status IN ('waiting_vw', 'resume_ready', 'deferred')"
    args = []
    if _config_index_text(session_id):
        sql += " AND session_id = ?"
        args.append(_config_index_text(session_id))
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


_TASK_QUESTION_COLUMNS = (
    "question_id",
    "task_id",
    "task_revision",
    "kind",
    "payload_json",
    "status",
    "scope",
    "persona_id",
    "asker",
    "delivery_status",
    "message_id",
    "answer",
    "answer_source",
    "created_at",
    "expires_at",
    "answered_at",
    "updated_at",
)


def save_task_question(conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any] | None:
    """登记/更新一条问题（按 ``question_id`` upsert）。没有 id 就当垃圾不写。"""

    key = _meme_text(row.get("question_id"))
    if not key:
        return None
    values = [row.get(column) for column in _TASK_QUESTION_COLUMNS]
    values[0] = key
    placeholders = ", ".join("?" for _ in _TASK_QUESTION_COLUMNS)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in _TASK_QUESTION_COLUMNS
        if column != "question_id"
    )
    conn.execute(
        f"INSERT INTO task_questions ({', '.join(_TASK_QUESTION_COLUMNS)})"
        f" VALUES ({placeholders}) ON CONFLICT(question_id) DO UPDATE SET {updates}",
        values,
    )
    conn.commit()
    return get_task_question(conn, key)


def get_task_question(conn: sqlite3.Connection, question_id: str) -> dict[str, Any] | None:
    key = _meme_text(question_id)
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM task_questions WHERE question_id = ?", (key,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_open_questions(
    conn: sqlite3.Connection, *, scope: str = "", task_id: str = "", limit: int = 20
) -> list[dict[str, Any]]:
    """还等着回答的问题（同一会话里最近的在前）。"""

    sql = (
        "SELECT * FROM task_questions WHERE status IN ('asked', 'delivered')"
    )
    args: list[Any] = []
    if _meme_text(scope):
        sql += " AND scope = ?"
        args.append(_meme_text(scope))
    if _meme_text(task_id):
        sql += " AND task_id = ?"
        args.append(_meme_text(task_id))
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def consume_task_question(
    conn: sqlite3.Connection, question_id: str, *, answer: str = "", user_id: str = "", now: Any = 0.0
) -> dict[str, Any] | None:
    """**一次消费**：只有本人回答才把状态改成 answered（CAS 在 SQL 里）。

    别人的回答由上层记成"建议"（不想把建议也写进 answer 列）。
    返回更新后的行；抢不到（已经答过/已过期/不是本人）返回 ``None``。
    """

    key = _meme_text(question_id)
    if not key:
        return None
    row = get_task_question(conn, key)
    if row is None:
        return None
    from . import task_questions as questions

    if questions.classify_answer(row, user_id=user_id) != questions.ANSWER_BY_OWNER:
        return None
    stamp = _float_or(now, 0.0)
    cursor = conn.execute(
        "UPDATE task_questions SET status = 'answered', answer = ?, answer_source = 'owner',"
        " answered_at = ?, updated_at = ?"
        " WHERE question_id = ? AND status IN ('asked', 'delivered')",
        (_meme_text(answer), stamp, stamp, key),
    )
    conn.commit()
    if not cursor.rowcount:
        return None
    return get_task_question(conn, key)


_EXPERIENCE_COLUMNS = (
    "experience_id",
    "scope",
    "persona_id",
    "situation",
    "method",
    "limitations",
    "evidence_refs",
    "dependency_hash",
    "status",
    "revision",
    "confidence",
    "source",
    "note",
    "created_at",
    "updated_at",
    "expires_at",
)


def save_capability_experience(
    conn: sqlite3.Connection, row: Mapping[str, Any]
) -> dict[str, Any] | None:
    """按 ``experience_id`` upsert（身份稳定：同一件事同一个做法只一行）。"""

    key = _meme_text(row.get("experience_id"))
    if not key:
        return None
    values = [row.get(column) for column in _EXPERIENCE_COLUMNS]
    values[0] = key
    placeholders = ", ".join("?" for _ in _EXPERIENCE_COLUMNS)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in _EXPERIENCE_COLUMNS
        if column != "experience_id"
    )
    conn.execute(
        f"INSERT INTO capability_experiences ({', '.join(_EXPERIENCE_COLUMNS)})"
        f" VALUES ({placeholders}) ON CONFLICT(experience_id) DO UPDATE SET {updates}",
        values,
    )
    conn.commit()
    return get_capability_experience(conn, key)


def get_capability_experience(
    conn: sqlite3.Connection, experience_id: str
) -> dict[str, Any] | None:
    key = _meme_text(experience_id)
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM capability_experiences WHERE experience_id = ?", (key,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_capability_experiences(
    conn: sqlite3.Connection,
    *,
    scope: str = "",
    persona_id: str = "",
    status: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按作用域/状态列经验（最常用的在前）。"""

    sql = "SELECT * FROM capability_experiences WHERE 1 = 1"
    args: list[Any] = []
    if _meme_text(scope):
        sql += " AND scope = ?"
        args.append(_meme_text(scope))
    if _meme_text(persona_id):
        sql += " AND persona_id = ?"
        args.append(_meme_text(persona_id))
    if _meme_text(status):
        sql += " AND status = ?"
        args.append(_meme_text(status))
    sql += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


_SKILL_COLUMNS = (
    "skill_id",
    "title",
    "summary",
    "when_to_use",
    "when_not",
    "steps",
    "capabilities",
    "verify",
    "tags",
    "scope",
    "source",
    "version",
    "hash",
    "status",
    "use_count",
    "last_used_at",
    "note",
    "created_at",
    "updated_at",
    "schema_version",
)


def save_vw_skill(conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any] | None:
    """按 ``skill_id`` upsert。**内置 skill 不许被覆盖**：由调用方过滤。"""

    key = _meme_text(row.get("skill_id"))
    if not key:
        return None
    values = [row.get(column) for column in _SKILL_COLUMNS]
    values[0] = key
    placeholders = ", ".join("?" for _ in _SKILL_COLUMNS)
    updates = ", ".join(
        f"{column} = excluded.{column}" for column in _SKILL_COLUMNS if column != "skill_id"
    )
    try:
        conn.execute(
            f"INSERT INTO vw_skills ({', '.join(_SKILL_COLUMNS)}) VALUES ({placeholders})"
            f" ON CONFLICT(skill_id) DO UPDATE SET {updates}",
            values,
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return None
    return get_vw_skill(conn, key)


def get_vw_skill(conn: sqlite3.Connection, skill_id: str) -> dict[str, Any] | None:
    key = _meme_text(skill_id)
    if not key:
        return None
    try:
        row = conn.execute("SELECT * FROM vw_skills WHERE skill_id = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def list_vw_skills(
    conn: sqlite3.Connection, *, status: str = "", limit: int = 50, source: str = ""
) -> list[dict[str, Any]]:
    """按更新时间倒序列出；``source`` 可只看某一类（official 是内置镜像）。"""

    sql = "SELECT * FROM vw_skills WHERE 1 = 1"
    args: list[Any] = []
    if _meme_text(status):
        sql += " AND status = ?"
        args.append(_meme_text(status))
    if _meme_text(source):
        sql += " AND source = ?"
        args.append(_meme_text(source))
    sql += " ORDER BY updated_at DESC, use_count DESC LIMIT ?"
    args.append(max(1, int(limit)))
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def bump_vw_skills_usage(
    conn: sqlite3.Connection, skill_ids: Iterable[Any], *, now: float = 0.0
) -> int:
    """记一次"这条流程真被摆到她面前了"：``use_count + 1``、``last_used_at = now``。

    空列表/写失败都返回 0（**fail-soft**：记账不该影响回复）。
    """

    keys = [_meme_text(item) for item in (skill_ids or ())]
    keys = [key for key in keys if key]
    if not keys:
        return 0
    stamp = float(now or time.time())
    try:
        conn.executemany(
            "UPDATE vw_skills SET use_count = use_count + 1, last_used_at = ?"
            " WHERE skill_id = ?",
            [(stamp, key) for key in keys],
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return 0
    return len(keys)


def delete_vw_skill(conn: sqlite3.Connection, skill_id: str) -> bool:
    """删掉一条流程（内置的不走这里——由调用方先挡）。"""

    key = _meme_text(skill_id)
    if not key:
        return False
    try:
        cursor = conn.execute("DELETE FROM vw_skills WHERE skill_id = ?", (key,))
        conn.commit()
    except sqlite3.Error:
        return False
    return bool(cursor.rowcount)



def open_follow_up(
    conn: sqlite3.Connection,
    *,
    session_id: Any,
    scope_id: Any = "",
    said_text: Any = "",
    target_user: Any = "",
    due_at: Any = 0.0,
    max_times: Any = 2,
    now: Any = 0.0,
) -> dict[str, Any] | None:
    """登记一条"她在等回话"。同一个会话只留一条：旧的先以 ``superseded`` 收口。"""

    sid = _meme_text(session_id)
    if not sid:
        return None
    stamp = _float_or(now, 0.0)
    conn.execute(
        "UPDATE follow_ups SET status = 'done', reason = 'superseded', updated_at = ?"
        " WHERE session_id = ? AND status = 'open'",
        (stamp, sid),
    )
    conn.execute(
        "INSERT INTO follow_ups (session_id, scope_id, said_text, target_user, due_at,"
        " attempts, max_times, status, reason, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, ?, 'open', '', ?, ?)",
        (
            sid,
            _meme_text(scope_id),
            _meme_text(said_text),
            _meme_text(target_user),
            _float_or(due_at, 0.0),
            max(0, _meme_int(max_times, 2)),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return get_open_follow_up(conn, session_id=sid)


def get_open_follow_up(conn: sqlite3.Connection, *, session_id: Any) -> dict[str, Any] | None:
    sid = _meme_text(session_id)
    if not sid:
        return None
    row = conn.execute(
        "SELECT * FROM follow_ups WHERE session_id = ? AND status = 'open'"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return dict(row) if row is not None else None


def list_due_follow_ups(
    conn: sqlite3.Connection, *, now: Any = 0.0, limit: int = 20
) -> list[dict[str, Any]]:
    """到点该追一句的那些（``due_at`` 已经过了）。"""

    try:
        rows = conn.execute(
            "SELECT * FROM follow_ups WHERE status = 'open' AND due_at <= ?"
            " ORDER BY due_at ASC LIMIT ?",
            (_float_or(now, 0.0), max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def bump_follow_up(
    conn: sqlite3.Connection,
    follow_up_id: Any,
    *,
    attempts: Any = 0,
    due_at: Any = None,
    now: Any = 0.0,
) -> dict[str, Any] | None:
    """追了一句之后推进：记次数，排下一次的时间（``due_at=None`` 就不动时间）。"""

    key = _meme_int(follow_up_id, 0)
    if key <= 0:
        return None
    stamp = _float_or(now, 0.0)
    if due_at is None:
        conn.execute(
            "UPDATE follow_ups SET attempts = ?, updated_at = ? WHERE id = ?",
            (max(0, int(attempts)), stamp, key),
        )
    else:
        conn.execute(
            "UPDATE follow_ups SET attempts = ?, due_at = ?, updated_at = ? WHERE id = ?",
            (max(0, int(attempts)), _float_or(due_at, 0.0), stamp, key),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM follow_ups WHERE id = ?", (key,)).fetchone()
    return dict(row) if row is not None else None


def close_follow_up(
    conn: sqlite3.Connection,
    follow_up_id: Any,
    *,
    reason: Any = "",
    now: Any = 0.0,
) -> int:
    """收口一条（终态）。返回改了几行。"""

    key = _meme_int(follow_up_id, 0)
    if key <= 0:
        return 0
    cursor = conn.execute(
        "UPDATE follow_ups SET status = 'done', reason = ?, updated_at = ?"
        " WHERE id = ? AND status = 'open'",
        (_meme_text(reason), _float_or(now, 0.0), key),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def cancel_follow_ups(
    conn: sqlite3.Connection, *, session_id: Any, reason: Any = "", now: Any = 0.0
) -> int:
    """对方开口了 / 换了一句话：这个会话里所有"还在等"的都收口。"""

    sid = _meme_text(session_id)
    if not sid:
        return 0
    cursor = conn.execute(
        "UPDATE follow_ups SET status = 'done', reason = ?, updated_at = ?"
        " WHERE session_id = ? AND status = 'open'",
        (_meme_text(reason), _float_or(now, 0.0), sid),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


# ---------------- 「等会儿」的待办：读写（用户 m00001） ----------------
#
# 「算不算做了」的判定在 core/commitments.py 的纯函数里，这里只管存得住、读得回。


def add_commitment(
    conn: sqlite3.Connection,
    *,
    session_id: Any,
    origin: Any = "",
    task_text: Any = "",
    due_at: Any = 0.0,
    now: Any = 0.0,
) -> dict[str, Any] | None:
    """落一条待办；同会话已有 pending 就复用它（``due_at`` 取更晚的那个）。

    返回落库后的整行；``None`` = 没落成（空会话 / 写库失败）。调用方拿到 ``None``
    必须把这一轮退回 ``comply``——宁可当场照做，也不许说一句兑现不了的"等会儿"。
    """

    sid = _meme_text(session_id)
    if not sid:
        return None
    text = clip_task_text(task_text, COMMITMENT_TASK_CHARS)
    stamp = _float_or(now, 0.0)
    due = _float_or(due_at, 0.0)
    if due <= 0:
        due = stamp + 15 * 60.0
    try:
        row = conn.execute(
            "SELECT * FROM deferred_commitments"
            " WHERE session_id = ? AND state = 'pending'"
            " ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if row is not None:
            item = dict(row)
            later = max(_float_or(item.get("due_at"), 0.0), due)
            conn.execute(
                "UPDATE deferred_commitments SET due_at = ?, task_text = ?"
                " WHERE id = ? AND state = 'pending'",
                (later, text or str(item.get("task_text") or ""), int(item["id"])),
            )
            conn.commit()
            item["due_at"] = later
            item["task_text"] = text or str(item.get("task_text") or "")
            item["reused"] = True
            return item
        cursor = conn.execute(
            "INSERT INTO deferred_commitments"
            " (session_id, origin, task_text, created_at, due_at, state, nudges)"
            " VALUES (?, ?, ?, ?, ?, 'pending', 0)",
            (sid, _meme_text(origin), text, stamp, due),
        )
        conn.commit()
        new_id = int(cursor.lastrowid or 0)
    except sqlite3.Error:
        return None
    try:
        created = conn.execute(
            "SELECT * FROM deferred_commitments WHERE id = ?", (new_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return dict(created) if created is not None else None


def due_commitments(
    conn: sqlite3.Connection, *, now: Any = 0.0, limit: int = 20
) -> list[dict[str, Any]]:
    """到点该提醒的那些（``state='pending' AND due_at <= now``）。"""

    try:
        rows = conn.execute(
            "SELECT * FROM deferred_commitments"
            " WHERE state = 'pending' AND due_at <= ?"
            " ORDER BY due_at ASC LIMIT ?",
            (_float_or(now, 0.0), max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def list_commitments(
    conn: sqlite3.Connection,
    *,
    session_id: Any,
    states: Any = ("pending",),
    limit: int = 20,
) -> list[dict[str, Any]]:
    """这个会话里指定状态的待办（默认只看 pending，最近建的在前）。"""

    sid = _meme_text(session_id)
    if not sid:
        return []
    wanted = [
        str(item)
        for item in (states or ())
        if str(item) in {"pending", "done", "dropped"}
    ]
    if not wanted:
        wanted = ["pending"]
    marks = ",".join("?" for _ in wanted)
    try:
        rows = conn.execute(
            "SELECT * FROM deferred_commitments"
            f" WHERE session_id = ? AND state IN ({marks})"
            " ORDER BY id DESC LIMIT ?",
            (sid, *wanted, max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def nudge_commitment(
    conn: sqlite3.Connection,
    commitment_id: Any,
    *,
    now: Any = 0.0,
    due_at: Any = 0.0,
) -> int:
    """记一次提醒，并把下一次到期时间往后挪（返回新的 ``nudges``；0 = 没改到行）。"""

    try:
        ident = int(commitment_id or 0)
    except (TypeError, ValueError):
        return 0
    if ident <= 0:
        return 0
    stamp = _float_or(now, 0.0)
    next_due = _float_or(due_at, 0.0)
    try:
        if next_due > 0:
            cursor = conn.execute(
                "UPDATE deferred_commitments"
                " SET nudges = nudges + 1, last_nudge_at = ?, due_at = ?"
                " WHERE id = ? AND state = 'pending'",
                (stamp, next_due, ident),
            )
        else:
            cursor = conn.execute(
                "UPDATE deferred_commitments"
                " SET nudges = nudges + 1, last_nudge_at = ?"
                " WHERE id = ? AND state = 'pending'",
                (stamp, ident),
            )
        conn.commit()
        if not int(cursor.rowcount or 0):
            return 0
        row = conn.execute(
            "SELECT nudges FROM deferred_commitments WHERE id = ?", (ident,)
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row["nudges"]) if row is not None else 0


def close_commitment(
    conn: sqlite3.Connection,
    commitment_id: Any,
    *,
    reason: Any = "",
    now: Any = 0.0,
) -> int:
    """关闭一条待办：``reason="done"`` 记完成，其它（如 ``forgotten``）记放弃。"""

    try:
        ident = int(commitment_id or 0)
    except (TypeError, ValueError):
        return 0
    if ident <= 0:
        return 0
    reason_text = _meme_text(reason)
    target = "done" if reason_text == "done" else "dropped"
    cursor = conn.execute(
        "UPDATE deferred_commitments SET state = ?, closed_at = ?, close_reason = ?"
        " WHERE id = ? AND state = 'pending'",
        (target, _float_or(now, 0.0), reason_text, ident),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


# ---------------- 临时会话（她私聊出去的人，先接管一阵子） ----------------
#
# 状态机：``active``（接管中）→ ``expired``（到期，等他再开口就收尾）→ ``ended``
# （收尾说完了，之后他的消息全吞掉）。``archived`` 只是编辑器里的"收起"，与状态无关。
# ``released`` 是用户在编辑器里点「解除接管」：接管控点撤掉、这一行只留摘要当记录。
# 它也是 ``ended`` 的出口——没有 release 的话 ended 会永久吞掉那个 umo 的消息
# （见 ``release_temp_session`` 与 main.py 的 ``api_temp_session_release``）。


def _temp_state(value: Any, default: str = "ended") -> str:
    """临时会话状态只认四种；写坏了按 ``ended``（宁可不理，也不冒充活跃）。"""

    text = _meme_text(value).lower()
    return text if text in {"active", "expired", "ended", "released"} else default


def upsert_temp_session(
    conn: sqlite3.Connection,
    *,
    umo: Any,
    origin: Any = "",
    source: Any = "private",
    state: Any = "active",
    created_at: Any = 0.0,
    expires_at: Any = 0.0,
    rounds: Any = 0,
    summary: Any = "",
    relation: Any = "",
) -> dict[str, Any] | None:
    """登记 / 重新激活一条临时会话（同一个 umo 只留一行）。

    再次出站到同一个 umo 时**复用**这一行：轮数归零、状态回到 ``active``、
    ``archived`` 复位；但 ``created_at`` 与 ``summary`` 留着——摘要是"上次聊到
    哪儿"的唯一凭据，重新启用时要注回提示词（需求 9）。

    ``origin`` 是"发起这条临时会话的那个会话 umo"（用户 m00001 追加需求）：临时
    会话共用它的状态与画像。**空串是合法的**（不知道来源 / 来源就是自己）：早先
    这里写成 ``_meme_text(origin) or key``，把空串折回 umo 自己，等于"自己指向
    自己"，state_key 就无从映射——空串必须能原样落库。
    """

    key = _meme_text(umo)
    if not key:
        return None
    conn.execute(
        "INSERT INTO temp_sessions (umo, origin, source, state, created_at, expires_at,"
        " rounds, ended_at, end_reason, summary, relation_at_end, archived)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?, 0)"
        " ON CONFLICT(umo) DO UPDATE SET origin = excluded.origin,"
        " source = excluded.source, state = excluded.state,"
        " expires_at = excluded.expires_at, rounds = excluded.rounds,"
        " ended_at = 0, end_reason = '',"
        " summary = CASE WHEN excluded.summary != '' THEN excluded.summary"
        " ELSE temp_sessions.summary END,"
        " relation_at_end = excluded.relation_at_end, archived = 0",
        (
            key,
            # 空串 = 不知道来源：原样存空，绝不折回 umo 自己（那等于"自己指自己"）
            _meme_text(origin),
            _meme_text(source) or "private",
            _temp_state(state, "active"),
            int(_float_or(created_at, 0.0)),
            int(_float_or(expires_at, 0.0)),
            max(0, _meme_int(rounds, 0)),
            _meme_text(summary),
            _meme_text(relation),
        ),
    )
    conn.commit()
    return get_temp_session(conn, umo=key)


def get_temp_session(conn: sqlite3.Connection, *, umo: Any) -> dict[str, Any] | None:
    key = _meme_text(umo)
    if not key:
        return None
    try:
        row = conn.execute("SELECT * FROM temp_sessions WHERE umo = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def list_temp_sessions(
    conn: sqlite3.Connection, *, state: Any = "", limit: int = 500
) -> list[dict[str, Any]]:
    """编辑器用：默认最近登记的在前（``state`` 给空串就是全部）。"""

    sql = "SELECT * FROM temp_sessions"
    params: list[Any] = []
    text = _meme_text(state).lower()
    if text:
        sql += " WHERE state = ?"
        params.append(_temp_state(text, text))
    sql += " ORDER BY created_at DESC, umo ASC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def list_due_temp_sessions(
    conn: sqlite3.Connection, *, now: Any = 0.0, limit: int = 50
) -> list[dict[str, Any]]:
    """到点该置 ``expired`` 的那些（``expires_at <= now``，0 表示长期保留）。"""

    try:
        rows = conn.execute(
            "SELECT * FROM temp_sessions WHERE state = 'active' AND expires_at > 0"
            " AND expires_at <= ? ORDER BY expires_at ASC LIMIT ?",
            (int(_float_or(now, 0.0)), max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def bump_temp_session_rounds(
    conn: sqlite3.Connection, *, umo: Any, relation: Any = ""
) -> dict[str, Any] | None:
    """记一轮（她的回复真的发出去了之后才调），返回更新后的行。

    用 ``rounds = rounds + 1`` 自增，而不是"读出来 + 1 再整值写回"：
    ``main.py`` 的 ``_reply_turn`` 等锁超时后照样继续发，两条回复可以真并发，
    只有原子自增才不丢轮。``WHERE state = 'active'`` 是第二道闸——已经
    ``expired`` / ``ended`` / ``released`` 的行不再计数。

    返回 ``None`` = 没改到行（行没了 / 不是 active）：调用方这时**不能**拿手里
    那份旧行写回内存缓存（那会造出"内存 active、DB ended"的假活口），要以 DB
    为准重新读一次。
    """

    key = _meme_text(umo)
    if not key:
        return None
    cursor = conn.execute(
        "UPDATE temp_sessions SET rounds = rounds + 1, relation_at_end = ?"
        " WHERE umo = ? AND state = 'active'",
        (_meme_text(relation), key),
    )
    conn.commit()
    if not int(cursor.rowcount or 0):
        return None
    return get_temp_session(conn, umo=key)


# 临时会话只许单向迁移：``ended`` 不能被并发的 sweep 改回 ``expired``，
# ``expired`` 也不能盖掉 ``ended``。``released`` 例外——它的用途就是从
# ``ended`` 里把 umo 放出来（用户点「解除接管」）。
_TEMP_ALLOWED_FROM: dict[str, tuple[str, ...]] = {
    "ended": ("active", "expired", "ended"),
    "expired": ("active",),
    "released": ("active", "expired", "ended", "released"),
}


def finish_temp_session(
    conn: sqlite3.Connection,
    *,
    umo: Any,
    state: Any = "ended",
    ended_at: Any = 0.0,
    end_reason: Any = "",
    summary: Any = None,
    relation: Any = "",
) -> dict[str, Any] | None:
    """收尾：写状态、收尾时刻、原因；``summary`` 不为 None 时才动摘要那一列。

    迁移方向受 ``_TEMP_ALLOWED_FROM`` 限制；没改到行时返回 ``None``，调用方据此
    知道"别人先收尾了"，应回读 DB 而不是把旧行写回缓存。
    """

    key = _meme_text(umo)
    if not key:
        return None
    target = _temp_state(state)
    allowed = _TEMP_ALLOWED_FROM.get(target, ())
    if not allowed:
        return None
    marks = ",".join("?" for _ in allowed)
    stamp = int(_float_or(ended_at, 0.0))
    reason = _meme_text(end_reason)
    relation_text = _meme_text(relation)
    params: tuple[Any, ...]
    if summary is None:
        sql = (
            "UPDATE temp_sessions SET state = ?, ended_at = ?, end_reason = ?,"
            " relation_at_end = ? WHERE umo = ? AND state IN (" + marks + ")"
        )
        params = (target, stamp, reason, relation_text, key, *allowed)
    else:
        sql = (
            "UPDATE temp_sessions SET state = ?, ended_at = ?, end_reason = ?,"
            " summary = ?, relation_at_end = ? WHERE umo = ? AND state IN (" + marks + ")"
        )
        params = (target, stamp, reason, _meme_text(summary), relation_text, key, *allowed)
    cursor = conn.execute(sql, params)
    conn.commit()
    if not int(cursor.rowcount or 0):
        return None
    return get_temp_session(conn, umo=key)


def extend_temp_session(conn: sqlite3.Connection, *, umo: Any, expires_at: Any = 0.0) -> int:
    """熟人及以上不参与到期：把期限往后推（只动 ``active`` 行）。"""

    key = _meme_text(umo)
    if not key:
        return 0
    cursor = conn.execute(
        "UPDATE temp_sessions SET expires_at = ? WHERE umo = ? AND state = 'active'",
        (int(_float_or(expires_at, 0.0)), key),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def set_temp_session_archived(
    conn: sqlite3.Connection, *, umo: Any, archived: Any = False
) -> int:
    """归档 / 恢复（编辑器里的"默认收起"）。返回改了几行。"""

    key = _meme_text(umo)
    if not key:
        return 0
    cursor = conn.execute(
        "UPDATE temp_sessions SET archived = ? WHERE umo = ?",
        (1 if bool(archived) else 0, key),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def set_temp_session_origin(conn: sqlite3.Connection, *, umo: Any, origin: Any = "") -> int:
    """改写一条临时会话的 ``origin``（历史数据自愈用，用户 m00001 追加需求）。

    只动这一列：``state`` / ``expires_at`` / ``rounds`` / ``summary`` 一律不碰——
    自愈不该顺手把一条 ``expired`` 会话重新激活，也不该把轮数清零。
    返回改了几行（0 = 没有这条 / 值没变）。
    """

    key = _meme_text(umo)
    if not key:
        return 0
    cursor = conn.execute(
        "UPDATE temp_sessions SET origin = ? WHERE umo = ?",
        (_meme_text(origin), key),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def list_unfinished_tasks(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """没做完的任务（排队/执行中/等 VW/等对方/可恢复/暂缓），最近的在前。"""
    sql = (
        "SELECT * FROM agent_tasks WHERE status IN"
        " ('queued', 'running', 'waiting_vw', 'waiting_user', 'resume_ready', 'deferred')"
        " ORDER BY updated_at DESC LIMIT ?"
    )
    try:
        rows = conn.execute(sql, (max(1, int(limit)),)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def interrupt_running_tasks(conn: sqlite3.Connection, *, now: Any = 0.0) -> int:
    """把"上一进程还在跑"的任务一次性标成失败（**不重放**）。返回改了几行。

    为什么放在一条 SQL 里：重启恢复必须**原子且幂等**——分多次改，中途崩溃会留下
    一半 failed 一半 running 的烂摊子；一条 UPDATE 就不存在这种中间态。
    """

    stamp = _float_or(now, 0.0)
    cursor = conn.execute(
        "UPDATE agent_tasks SET status = 'failed', error = 'interrupted_at_restart',"
        " finished_at = ?, updated_at = ?,"
        " note = CASE WHEN note = '' THEN '上一进程在它跑到一半时停了，这件事先算没做成。'"
        " ELSE note END"
        " WHERE status IN ('running', 'queued')",
        (stamp, stamp),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def save_agent_task(
    conn: sqlite3.Connection, task: Mapping[str, Any], *, now: Any = 0.0
) -> dict[str, Any] | None:
    """存一份任务（按 ``task_id`` upsert；没有 id 就当垃圾不写）。

    ⚠️ **终态不许被改口**：库里已经是终态时，这次写入只更新 `steps`/`note`/`error`
    里**非空**的部分，`status`/`finished_at` 保持库里那份——纯逻辑已经保证幂等，
    这里再挡一道（并发收尾、重放老调用方都不会把"失败"洗成"成功"）。
    """

    task_id = _meme_text(task.get("task_id"))
    if not task_id:
        return None
    stamp = _now_or(now) or _now_or(task.get("updated_at")) or _now_or(task.get("created_at"))
    existing = get_agent_task(conn, task_id)
    terminal = {"succeeded", "failed", "cancelled"}
    if existing is not None and _meme_text(existing.get("status")) in terminal:
        status = _meme_text(existing.get("status"))
        finished_at = _now_or(existing.get("finished_at"))
        note = _meme_text(task.get("note")) or _meme_text(existing.get("note"))
        error = _meme_text(task.get("error")) or _meme_text(existing.get("error"))
    else:
        status = _meme_text(task.get("status")) or "queued"
        finished_at = _now_or(task.get("finished_at"))
        note = _meme_text(task.get("note"))
        error = _meme_text(task.get("error"))
    conn.execute(
        "INSERT INTO agent_tasks (task_id, session_id, goal, status, steps, note, error,"
        " created_at, updated_at, finished_at, schema_version, brief_json,"
        " checkpoint_json, revision, wait_reason, active_elapsed, expires_at, parent_task_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(task_id) DO UPDATE SET session_id = excluded.session_id,"
        " goal = excluded.goal, status = excluded.status, steps = excluded.steps,"
        " note = excluded.note, error = excluded.error, updated_at = excluded.updated_at,"
        " finished_at = excluded.finished_at,"
        # P1-1：任务包与等待态。空值不覆盖已有内容（老调用方不带这些键）
        " schema_version = excluded.schema_version,"
        " brief_json = CASE WHEN excluded.brief_json = '' THEN agent_tasks.brief_json"
        " ELSE excluded.brief_json END,"
        " checkpoint_json = CASE WHEN excluded.checkpoint_json = ''"
        " THEN agent_tasks.checkpoint_json ELSE excluded.checkpoint_json END,"
        " revision = MAX(agent_tasks.revision, excluded.revision),"
        " wait_reason = excluded.wait_reason,"
        " active_elapsed = MAX(agent_tasks.active_elapsed, excluded.active_elapsed),"
        " expires_at = excluded.expires_at,"
        " parent_task_id = CASE WHEN excluded.parent_task_id = ''"
        " THEN agent_tasks.parent_task_id ELSE excluded.parent_task_id END",
        (
            task_id,
            _meme_text(task.get("session_id")),
            _meme_text(task.get("goal")),
            status,
            agent_tasks.steps_json(task),
            note,
            error,
            _now_or(task.get("created_at")) or stamp,
            stamp,
            finished_at,
            int(agent_tasks._int(task.get("schema_version"), 1)),
            agent_tasks._text(task.get("brief_json")),
            agent_tasks._text(task.get("checkpoint_json")),
            int(agent_tasks._int(task.get("revision"), 0)),
            agent_tasks._text(task.get("wait_reason")),
            float(agent_tasks._int(task.get("active_elapsed"), 0) or 0),
            float(agent_tasks._int(task.get("expires_at"), 0) or 0),
            agent_tasks._text(task.get("parent_task_id")),
        ),
    )
    conn.commit()
    return get_agent_task(conn, task_id)


def get_agent_task(conn: sqlite3.Connection, task_id: Any) -> dict[str, Any] | None:
    """按编号取一条任务（没有就是 ``None``）。"""

    key = _meme_text(task_id)
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM agent_tasks WHERE task_id = ?", (key,)
    ).fetchone()
    return _agent_task_row(row) if row is not None else None


def list_agent_tasks(
    conn: sqlite3.Connection,
    *,
    session_id: Any = "",
    status: Any = "",
    limit: Any = 20,
) -> list[dict[str, Any]]:
    """按时间倒序取任务（可只取某个会话 / 某个状态）。"""

    sql = "SELECT * FROM agent_tasks WHERE 1 = 1"
    params: list[Any] = []
    key = _meme_text(session_id)
    if key:
        sql += " AND session_id = ?"
        params.append(key)
    wanted = _meme_text(status)
    if wanted:
        sql += " AND status = ?"
        params.append(wanted)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(max(1, min(200, _meme_int(limit, 20))))
    return [_agent_task_row(row) for row in conn.execute(sql, params).fetchall()]


def unfinished_agent_tasks(conn: sqlite3.Connection, *, limit: Any = 50) -> list[dict[str, Any]]:
    """还没落终态的任务（重启收尾用：它们一律按 interrupted 收掉）。"""

    rows = conn.execute(
        "SELECT * FROM agent_tasks WHERE status NOT IN ('succeeded', 'failed', 'cancelled')"
        " ORDER BY created_at ASC LIMIT ?",
        (max(1, min(500, _meme_int(limit, 50))),),
    ).fetchall()
    return [_agent_task_row(row) for row in rows]


def insert_agent_artifact(
    conn: sqlite3.Connection, artifact: Mapping[str, Any]
) -> dict[str, Any] | None:
    """登记一份产物（按 ``artifact_id`` 幂等：重复登记只补空字段）。"""

    artifact_id = _meme_text(artifact.get("artifact_id"))
    if not artifact_id:
        return None
    row = dict(artifact)
    conn.execute(
        "INSERT INTO agent_artifacts (artifact_id, task_id, session_id, name, path, mime,"
        " size_bytes, created_at, expires_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(artifact_id) DO UPDATE SET"
        " name = CASE WHEN agent_artifacts.name = '' THEN excluded.name ELSE agent_artifacts.name END,"
        " path = CASE WHEN agent_artifacts.path = '' THEN excluded.path ELSE agent_artifacts.path END,"
        " mime = CASE WHEN agent_artifacts.mime = '' THEN excluded.mime ELSE agent_artifacts.mime END,"
        " size_bytes = CASE WHEN agent_artifacts.size_bytes = 0"
        " THEN excluded.size_bytes ELSE agent_artifacts.size_bytes END,"
        " expires_at = CASE WHEN agent_artifacts.expires_at = 0"
        " THEN excluded.expires_at ELSE agent_artifacts.expires_at END",
        (
            artifact_id,
            _meme_text(row.get("task_id")),
            _meme_text(row.get("session_id")),
            _meme_text(row.get("name")),
            _meme_text(row.get("path")),
            _meme_text(row.get("mime")),
            max(0, _meme_int(row.get("size_bytes"), 0)),
            _now_or(row.get("created_at")),
            _now_or(row.get("expires_at")),
            _now_or(row.get("sent_at")),
        ),
    )
    conn.commit()
    return get_agent_artifact(conn, artifact_id)


def get_agent_artifact(conn: sqlite3.Connection, artifact_id: Any) -> dict[str, Any] | None:
    key = _meme_text(artifact_id)
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM agent_artifacts WHERE artifact_id = ?", (key,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_agent_artifacts(
    conn: sqlite3.Connection,
    *,
    task_id: Any = "",
    session_id: Any = "",
    limit: Any = 20,
) -> list[dict[str, Any]]:
    """按时间倒序取产物（可只取某次任务 / 某个会话的）。"""

    sql = "SELECT * FROM agent_artifacts WHERE 1 = 1"
    params: list[Any] = []
    key = _meme_text(task_id)
    if key:
        sql += " AND task_id = ?"
        params.append(key)
    session = _meme_text(session_id)
    if session:
        sql += " AND session_id = ?"
        params.append(session)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(max(1, min(200, _meme_int(limit, 20))))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def mark_agent_artifact_sent(
    conn: sqlite3.Connection, artifact_id: Any, *, now: Any = 0.0
) -> dict[str, Any] | None:
    """记"这份产物真的发出去了"（时间只记第一次）。"""

    key = _meme_text(artifact_id)
    if not key:
        return None
    conn.execute(
        "UPDATE agent_artifacts SET sent_at = CASE WHEN sent_at <= 0 THEN ? ELSE sent_at END"
        " WHERE artifact_id = ?",
        (_now_or(now), key),
    )
    conn.commit()
    return get_agent_artifact(conn, key)


def prune_agent_artifacts(
    conn: sqlite3.Connection, *, before: Any, only_sent: bool = True
) -> list[dict[str, Any]]:
    """删掉过期的产物台账行，返回被删的那些（调用方拿它去删磁盘文件）。

    ``only_sent=True``（默认）只删**已经发出去**的：还没发的留着，别把用户还没看到的
    东西偷偷清掉。``before<=0`` 表示"不过期"，一条都不删。
    """

    stamp = _now_or(before)
    if stamp <= 0:
        return []
    sql = "SELECT * FROM agent_artifacts WHERE expires_at > 0 AND expires_at <= ?"
    params: list[Any] = [stamp]
    if only_sent:
        sql += " AND sent_at > 0"
    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    if rows:
        conn.executemany(
            "DELETE FROM agent_artifacts WHERE artifact_id = ?",
            [(row.get("artifact_id"),) for row in rows],
        )
        conn.commit()
    return rows


def _meme_text(value: Any) -> str:
    """收藏相关文本列的归一：``None`` 当空串，两端空白去掉。"""

    return str(value or "").strip()


def _meme_int(value: Any, default: int = 0) -> int:
    """收藏相关整数列的归一：``bool`` 不当数字，坏值退回默认。"""

    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _meme_float(value: Any, default: float = 0.0) -> float:
    """收藏相关浮点列的归一：坏值退回默认。"""

    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_meme_id(number: int) -> str:
    """把序号格式化成收藏 ID（``42 -> 'meme_000042'``）。"""

    try:
        value = int(number)
    except (TypeError, ValueError):
        value = 0
    return f"{MEME_ID_PREFIX}{max(0, value):0{MEME_ID_DIGITS}d}"


def _meme_id_sequence(entry_id: Any) -> int:
    """从收藏 ID 里取回序号（认不出的返回 0）。"""

    text = _meme_text(entry_id)
    if not text.startswith(MEME_ID_PREFIX):
        return 0
    return _meme_int(text[len(MEME_ID_PREFIX):], 0)


def _meme_entry_row(conn: sqlite3.Connection, entry_id: str) -> dict[str, Any] | None:
    """按 ID 取一行收藏（没有返回 ``None``）。**不做任何权限或状态过滤。**"""

    target = _meme_text(entry_id)
    if not target:
        return None
    row = conn.execute(
        "SELECT * FROM meme_entries WHERE entry_id = ?", (target,)
    ).fetchone()
    return dict(row) if row is not None else None


def insert_meme_asset(
    conn: sqlite3.Connection,
    *,
    media_id: Any,
    mime: Any = "",
    size_bytes: Any = 0,
    content_hash: Any = "",
    width: Any = 0,
    height: Any = 0,
    frame_count: Any = 0,
    is_animated: Any = False,
    tags: Any = "",
    now: Any = 0.0,
) -> dict[str, Any] | None:
    """登记一份资产（按内容身份 ``media_id`` 去重）。

    已存在时**只补空字段**、不动 ``created_at``：哈希与大小是内容的属性，不该因为
    又见了一次就变；``mime`` / 尺寸 / 帧数第一次没测出来、第二次测出来了就补上。
    返回该资产的行；``media_id`` 为空或不是合法内容身份时返回 ``None``（一行不写）。
    """

    key = _meme_text(media_id)
    if not key:
        return None
    stamp = _now_or(now)
    digest = _meme_text(content_hash) or key
    conn.execute(
        "INSERT OR IGNORE INTO meme_assets"
        " (media_id, mime, size_bytes, content_hash, width, height, frame_count,"
        "  is_animated, tags, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            _meme_text(mime),
            _meme_int(size_bytes, 0),
            digest,
            _meme_int(width, 0),
            _meme_int(height, 0),
            _meme_int(frame_count, 0),
            1 if is_animated else 0,
            _meme_text(tags),
            stamp,
            stamp,
        ),
    )
    conn.execute(
        "UPDATE meme_assets SET"
        " mime = CASE WHEN mime = '' THEN ? ELSE mime END,"
        " size_bytes = CASE WHEN size_bytes = 0 THEN ? ELSE size_bytes END,"
        " width = CASE WHEN width = 0 THEN ? ELSE width END,"
        " height = CASE WHEN height = 0 THEN ? ELSE height END,"
        " frame_count = CASE WHEN frame_count = 0 THEN ? ELSE frame_count END,"
        " is_animated = CASE WHEN ? = 1 THEN 1 ELSE is_animated END,"
        " tags = CASE WHEN tags = '' THEN ? ELSE tags END,"
        " updated_at = ?"
        " WHERE media_id = ?",
        (
            _meme_text(mime),
            _meme_int(size_bytes, 0),
            _meme_int(width, 0),
            _meme_int(height, 0),
            _meme_int(frame_count, 0),
            1 if is_animated else 0,
            _meme_text(tags),
            stamp,
            key,
        ),
    )
    conn.commit()
    return get_meme_asset(conn, key)


def get_meme_asset(
    conn: sqlite3.Connection, media_id: Any
) -> dict[str, Any] | None:
    """按 ``media_id`` 取一份资产的元数据（没有返回 ``None``）。"""

    key = _meme_text(media_id)
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM meme_assets WHERE media_id = ?", (key,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_meme_assets(
    conn: sqlite3.Connection,
    *,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """列资产元数据（新的在前）。``limit`` 上限 5000（``<= 0`` 按 500 算）。"""

    rows_wanted = _meme_int(limit, 500)
    if rows_wanted <= 0:
        rows_wanted = 500
    rows_wanted = min(rows_wanted, 5000)
    skip = max(_meme_int(offset, 0), 0)
    rows = conn.execute(
        "SELECT * FROM meme_assets ORDER BY created_at DESC, media_id"
        " LIMIT ? OFFSET ?",
        (rows_wanted, skip),
    ).fetchall()
    return [dict(row) for row in rows]


def cleanup_meme_assets(
    conn: sqlite3.Connection, extra_keep: Iterable[str] = ()
) -> int:
    """删掉不再被任何收藏引用的资产元数据行（在**删除收藏之后**清理）。

    只认**真没被引用**的：还有条目（含被禁用的、已软删的）指向它就留着——
    软删的收藏还能恢复，它的资产就不能先没了。``extra_keep`` 是"另外还要保住的
    ``media_id``"（例如刚上传还没建条目的那份），可以留空。
    返回删掉的行数。**不动文件**：底层图片由资产库自己决定去留。
    """

    keep = [key for key in (_meme_text(item) for item in extra_keep) if key]
    sql = (
        "DELETE FROM meme_assets WHERE media_id NOT IN"
        " (SELECT DISTINCT media_id FROM meme_entries WHERE media_id <> '')"
    )
    args: tuple[Any, ...] = ()
    if keep:
        placeholders = ",".join("?" for _ in keep)
        sql += f" AND media_id NOT IN ({placeholders})"
        args = tuple(keep)
    cursor = conn.execute(sql, args)
    conn.commit()
    return int(cursor.rowcount or 0)


def _next_meme_entry_id(conn: sqlite3.Connection) -> str:
    """下一个收藏 ID（``meme_000042``）。

    取的是**已用过的最大序号 + 1**，所以软删掉的条目不会被重新分配 ID：
    ``meme_007`` 删了以后下一个是 ``meme_008``，不会有人拿到别人用过的 ID。
    """

    rows = conn.execute("SELECT entry_id FROM meme_entries").fetchall()
    used = max((_meme_id_sequence(row["entry_id"]) for row in rows), default=0)
    return format_meme_id(used + 1)


def create_meme_entry(
    conn: sqlite3.Connection,
    *,
    media_id: Any,
    name: Any = "",
    scope_id: Any = "",
    persona_id: Any = "",
    meaning: Any = "",
    use_when: Any = "",
    avoid_when: Any = "",
    personal_note: Any = "",
    preference: Any = 0.0,
    status: Any = MEME_ENTRY_STATUS_ENABLED,
    pinned: Any = False,
    entry_id: Any = "",
    now: Any = 0.0,
) -> dict[str, Any]:
    """建一条收藏，返回它的行。

    ``entry_id`` 留空时自动分配 ``meme_XXXXXX``。**同一个 ``media_id`` 已经有没删的
    收藏时直接返回那一条**（不新建、不改它）——这就是"相同字节 → 同一个 media_id、
    不重复建条目"的落点；调用方拿 ``created`` 区分是新建还是撞上了已有的。
    """

    key = _meme_text(media_id)
    existing = find_meme_entry_by_media(conn, key) if key else None
    if existing is not None:
        existing["created"] = False
        return existing
    stamp = _now_or(now)
    wanted = _meme_text(entry_id) or _next_meme_entry_id(conn)
    row_status = _meme_text(status) or MEME_ENTRY_STATUS_ENABLED
    conn.execute(
        "INSERT INTO meme_entries"
        " (entry_id, media_id, scope_id, persona_id, name, meaning, use_when,"
        "  avoid_when, personal_note, preference, revision, status, pinned,"
        "  created_at, updated_at, deleted_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0)",
        (
            wanted,
            key,
            _meme_text(scope_id),
            _meme_text(persona_id),
            _meme_text(name),
            _meme_text(meaning),
            _meme_text(use_when),
            _meme_text(avoid_when),
            _meme_text(personal_note),
            _meme_float(preference, 0.0),
            row_status,
            1 if pinned else 0,
            stamp,
            stamp,
        ),
    )
    conn.commit()
    row = _meme_entry_row(conn, wanted)
    assert row is not None  # 刚插进去的行必然在
    row["created"] = True
    return row


def find_meme_entry_by_media(
    conn: sqlite3.Connection, media_id: Any
) -> dict[str, Any] | None:
    """按内容身份找**没被删**的那条收藏（没有返回 ``None``）。

    一个 ``media_id`` 至多一条（``idx_meme_entries_media`` 那条部分唯一索引钉住的）。
    """

    key = _meme_text(media_id)
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM meme_entries WHERE media_id = ? AND status != ?"
        " ORDER BY created_at LIMIT 1",
        (key, MEME_ENTRY_STATUS_DELETED),
    ).fetchone()
    return dict(row) if row is not None else None


def assign_meme_id(conn: sqlite3.Connection) -> str:
    """只取号不建行（页面"新建收藏"先拿 ID 再去传图时用）。"""

    return _next_meme_entry_id(conn)


# ---------------------------------------------------------------------------
# 攒图评审收藏分区（用户 2026-10-24）：候选池 + 评审频率记账
#
# 只负责存得住、读得回：入池概率、攒够几张、拼图、要不要调模型、收哪些，全在
# ``core/meme_collect.py``。三条纪律：① 同一 ``media_id`` 只入池一次（唯一索引）；
# ② 评审过的候选置 collected/passed，**永不回到 pending**（不重弹）；③ 评审记账只
# 追加，不删不改（频率上限靠数 ``created_at >= 今天零点`` 的行）。
# ---------------------------------------------------------------------------
COLLECT_STATUS_PENDING = "pending"
COLLECT_STATUS_COLLECTED = "collected"
COLLECT_STATUS_PASSED = "passed"
COLLECT_STATUS_EXPIRED = "expired"
COLLECT_STATUSES = (
    COLLECT_STATUS_PENDING,
    COLLECT_STATUS_COLLECTED,
    COLLECT_STATUS_PASSED,
    COLLECT_STATUS_EXPIRED,
)


def _collect_candidate_row(
    conn: sqlite3.Connection, candidate_id: Any
) -> dict[str, Any] | None:
    """按候选编号取一行（没有返回 ``None``）。"""

    row = conn.execute(
        "SELECT * FROM meme_collect_candidates WHERE candidate_id = ?",
        (_meme_text(candidate_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def add_meme_collect_candidate(
    conn: sqlite3.Connection,
    *,
    media_id: Any,
    session_id: Any = "",
    caption: Any = "",
    source: Any = "chat",
    seen_at: Any = 0.0,
) -> bool:
    """把一张"她看到过的图"放进候选池；同一 ``media_id`` 已在池里就不重复入池。

    返回 ``True`` 表示这次真入池了。``caption`` 只留前 120 字（评审时每行最多用 20 字，
    原文留着是给"以后想复核"用，不进提示词）。

    唯一性口径（用户 2026-10-24）：``pending`` / ``passed`` / ``collected`` 都挡住重复插入
    （``pending`` 另有部分唯一索引兜底）；``passed`` 想再给一次机会走
    :func:`revive_meme_collect_candidate` 原地重置，不新插一行；``expired`` 不挡。
    """

    key = _meme_text(media_id)
    if not key:
        return False
    cursor = conn.execute(
        "INSERT INTO meme_collect_candidates"
        " (candidate_id, session_id, media_id, caption, source, status, seen_at,"
        "  reviewed_at, sheet_id, ref_index, entry_id)"
        " SELECT ?, ?, ?, ?, ?, ?, ?, 0, '', 0, ''"
        " WHERE NOT EXISTS (SELECT 1 FROM meme_collect_candidates"
        "  WHERE media_id = ? AND status IN (?, ?, ?))",
        (
            "mc_" + uuid.uuid4().hex[:16],
            _meme_text(session_id),
            key,
            _meme_text(caption)[:120],
            _meme_text(source) or "chat",
            COLLECT_STATUS_PENDING,
            _now_or(seen_at),
            key,
            COLLECT_STATUS_PENDING,
            COLLECT_STATUS_COLLECTED,
            COLLECT_STATUS_PASSED,
        ),
    )
    conn.commit()
    return int(cursor.rowcount or 0) > 0


def revive_meme_collect_candidate(
    conn: sqlite3.Connection,
    *,
    media_id: Any,
    now: Any = 0.0,
    ttl_seconds: float = 0.0,
    session_id: Any = "",
    caption: Any = "",
    source: Any = "chat",
) -> bool:
    """把"评审过但没过期"的 ``passed`` 候选重新放回池子（用户 2026-10-24 补）。

    只在 ``reviewed_at`` **早于** ``now - ttl_seconds`` 时重置——没到 TTL 就维持
    passed，不反复骚扰。``collected`` / ``expired`` / ``pending`` 一律不动：前者已经
    收藏（``meme_entries`` 唯一索引兜底），后两者不需要重置。

    重置会清掉 ``sheet_id`` / ``ref_index`` / ``reviewed_at``，把 ``seen_at`` 记成
    "这一次重新看到的时间"——TTL 靠 ``seen_at`` 自然节流，所以不会"每 7 天无限重弹"
    （每张图每过一个 TTL 最多再获得一次机会）。返回 ``True`` 表示这次真的复活了。
    """

    key = _meme_text(media_id)
    if not key:
        return False
    stamp = _now_or(now)
    cutoff = float(stamp) - max(0.0, float(ttl_seconds or 0.0))
    cursor = conn.execute(
        "UPDATE meme_collect_candidates"
        " SET status = ?, seen_at = ?, reviewed_at = 0, sheet_id = '', ref_index = 0,"
        "     session_id = CASE WHEN ? != '' THEN ? ELSE session_id END,"
        "     caption = CASE WHEN ? != '' THEN ? ELSE caption END,"
        "     source = ?, entry_id = ''"
        " WHERE media_id = ? AND status = ?"
        "   AND reviewed_at > 0 AND reviewed_at <= ?",
        (
            COLLECT_STATUS_PENDING,
            stamp,
            _meme_text(session_id),
            _meme_text(session_id),
            _meme_text(caption)[:120],
            _meme_text(caption)[:120],
            _meme_text(source) or "chat",
            key,
            COLLECT_STATUS_PASSED,
            cutoff,
        ),
    )
    conn.commit()
    return int(cursor.rowcount or 0) > 0


def fill_meme_collect_captions(
    conn: sqlite3.Connection,
    *,
    captions: Mapping[str, Any],
    session_id: Any = "",
) -> int:
    """给还在池里的候选补上画面描述，返回补了几行。

    为什么要这一步：入池发生在消息**刚到时**（``on_any_message``），那时后台转述
    还没出来，候选落库的 ``caption`` 只能是空的；等到评审时（可能十几小时后）转述
    早就在聊天留档里了，用当时的描述把空白补上，清单里才不会整屏"没看清"。
    只补 ``status = 'pending' AND caption = ''`` 的行，已有描述绝不覆盖。
    """

    if not captions:
        return 0
    scope = _meme_text(session_id)
    rows = [
        (_meme_text(caption)[:120], _meme_text(media_id))
        for media_id, caption in captions.items()
        if _meme_text(media_id) and _meme_text(caption)
    ]
    if not rows:
        return 0
    if scope:
        sql = (
            "UPDATE meme_collect_candidates SET caption = ?"
            " WHERE media_id = ? AND status = ? AND caption = ''"
            "   AND session_id = ?"
        )
        payload = [(*row, COLLECT_STATUS_PENDING, scope) for row in rows]
    else:
        sql = (
            "UPDATE meme_collect_candidates SET caption = ?"
            " WHERE media_id = ? AND status = ? AND caption = ''"
        )
        payload = [(*row, COLLECT_STATUS_PENDING) for row in rows]
    filled = 0
    for params in payload:
        cursor = conn.execute(sql, params)
        filled += int(cursor.rowcount or 0)
    conn.commit()
    return filled


def list_meme_collect_candidates(
    conn: sqlite3.Connection,
    *,
    session_id: Any = "",
    status: Any = "",
    sheet_id: Any = None,
    unassigned: bool = False,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """列候选（按 ``seen_at`` 从旧到新）。``unassigned=True`` 只要还没分到拼图格子的。"""

    clauses: list[str] = []
    params: list[Any] = []
    if _meme_text(session_id):
        clauses.append("session_id = ?")
        params.append(_meme_text(session_id))
    if _meme_text(status):
        clauses.append("status = ?")
        params.append(_meme_text(status))
    if sheet_id is not None:
        clauses.append("sheet_id = ?")
        params.append(_meme_text(sheet_id))
    if unassigned:
        clauses.append("ref_index = 0")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT * FROM meme_collect_candidates"
        + where
        + " ORDER BY seen_at ASC, candidate_id ASC"
    )
    if int(limit or 0) > 0:
        sql += f" LIMIT {int(limit)}"
    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def count_meme_collect_candidates(
    conn: sqlite3.Connection, *, session_id: Any = "", status: Any = ""
) -> int:
    """数候选（``session_id`` / ``status`` 不给就不筛）。"""

    clauses: list[str] = []
    params: list[Any] = []
    if _meme_text(session_id):
        clauses.append("session_id = ?")
        params.append(_meme_text(session_id))
    if _meme_text(status):
        clauses.append("status = ?")
        params.append(_meme_text(status))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM meme_collect_candidates" + where, tuple(params)
    ).fetchone()
    return int(row["n"] if row is not None else 0)


def assign_meme_collect_refs(
    conn: sqlite3.Connection,
    *,
    candidate_ids: Iterable[Any],
    sheet_id: Any,
    start_index: int = 1,
) -> list[dict[str, Any]]:
    """给这一批评审的候选分配"候选N"与拼图 ``sheet_id``（一次事务，返回更新后的行）。

    编号**在这里定**：拼图与文本清单都读 ``ref_index``，两边永远不会错位。
    """

    keys = [_meme_text(item) for item in candidate_ids if _meme_text(item)]
    if not keys:
        return []
    sheet = _meme_text(sheet_id)
    rows: list[dict[str, Any]] = []
    try:
        for offset, key in enumerate(keys):
            index = int(start_index) + offset
            conn.execute(
                "UPDATE meme_collect_candidates SET sheet_id = ?, ref_index = ?"
                " WHERE candidate_id = ? AND status = ?",
                (sheet, index, key, COLLECT_STATUS_PENDING),
            )
            row = _collect_candidate_row(conn, key)
            if row is not None:
                rows.append(row)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    rows.sort(key=lambda item: int(item.get("ref_index") or 0))
    return rows


def release_meme_collect_refs(
    conn: sqlite3.Connection, *, candidate_ids: Iterable[Any]
) -> int:
    """评审没跑完时把编号退回（下次重新编号，不留下半张拼图的格子）。"""

    keys = [_meme_text(item) for item in candidate_ids if _meme_text(item)]
    if not keys:
        return 0
    marks = ",".join("?" for _ in keys)
    cursor = conn.execute(
        "UPDATE meme_collect_candidates SET sheet_id = '', ref_index = 0"
        f" WHERE candidate_id IN ({marks}) AND status = ?",
        (*keys, COLLECT_STATUS_PENDING),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def finish_meme_collect_candidates(
    conn: sqlite3.Connection,
    *,
    candidate_ids: Iterable[Any],
    status: Any,
    entry_ids: Mapping[Any, Any] | None = None,
    now: Any = 0.0,
) -> int:
    """评审收尾：把这一批候选一次性置 ``collected`` / ``passed``（不回到 pending）。"""

    state = _meme_text(status)
    if state not in (COLLECT_STATUS_COLLECTED, COLLECT_STATUS_PASSED):
        raise ValueError(f"unknown collect status: {status!r}")
    keys = [_meme_text(item) for item in candidate_ids if _meme_text(item)]
    if not keys:
        return 0
    stamp = _now_or(now)
    written = 0
    mapping = entry_ids or {}
    try:
        for key in keys:
            entry_id = _meme_text(mapping.get(key, ""))
            conn.execute(
                "UPDATE meme_collect_candidates"
                " SET status = ?, reviewed_at = ?, entry_id = ?"
                " WHERE candidate_id = ? AND status = ?",
                (state, stamp, entry_id, key, COLLECT_STATUS_PENDING),
            )
            written += 1
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return written


def expire_meme_collect_candidates(
    conn: sqlite3.Connection, *, seen_before: Any, now: Any = 0.0
) -> int:
    """清掉躺太久的候选（``seen_at < seen_before`` 的 pending 置 expired）。"""

    cursor = conn.execute(
        "UPDATE meme_collect_candidates SET status = ?, reviewed_at = ?"
        " WHERE status = ? AND seen_at > 0 AND seen_at < ?",
        (
            COLLECT_STATUS_EXPIRED,
            _now_or(now),
            COLLECT_STATUS_PENDING,
            _now_or(seen_before),
        ),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def add_meme_collect_review(
    conn: sqlite3.Connection,
    *,
    session_id: Any = "",
    sheets: int = 0,
    offered: int = 0,
    picked: int = 0,
    status: Any = "done",
    note: Any = "",
    now: Any = 0.0,
) -> dict[str, Any]:
    """记一次评审（只追加；频率上限与去重窗口都数这里的行）。"""

    review_id = "mr_" + uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO meme_collect_reviews"
        " (review_id, session_id, sheets, offered, picked, status, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            review_id,
            _meme_text(session_id),
            max(0, int(sheets or 0)),
            max(0, int(offered or 0)),
            max(0, int(picked or 0)),
            _meme_text(status) or "done",
            _meme_text(note)[:200],
            _now_or(now),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM meme_collect_reviews WHERE review_id = ?", (review_id,)
    ).fetchone()
    return dict(row) if row is not None else {"review_id": review_id}


def count_meme_collect_reviews(
    conn: sqlite3.Connection, *, session_id: Any = "", since: Any = 0.0
) -> int:
    """数评审次数（``since`` 之后）。"""

    clauses = ["created_at >= ?"]
    params: list[Any] = [_now_or(since)]
    if _meme_text(session_id):
        clauses.append("session_id = ?")
        params.append(_meme_text(session_id))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM meme_collect_reviews WHERE "
        + " AND ".join(clauses),
        tuple(params),
    ).fetchone()
    return int(row["n"] if row is not None else 0)


def last_meme_collect_review(
    conn: sqlite3.Connection, *, session_id: Any = ""
) -> dict[str, Any] | None:
    """最近一次评审（没评审过返回 ``None``）。"""

    clauses: list[str] = []
    params: list[Any] = []
    if _meme_text(session_id):
        clauses.append("session_id = ?")
        params.append(_meme_text(session_id))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    row = conn.execute(
        "SELECT * FROM meme_collect_reviews"
        + where
        + " ORDER BY created_at DESC, review_id DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    return dict(row) if row is not None else None


def meme_collect_inventory(
    conn: sqlite3.Connection, *, since: Any = 0.0
) -> dict[str, Any]:
    """库存事实（只读）：收藏总数、从未发出去过的张数、最近发得最多的那一类。

    "类"用收藏的 ``name`` 当类目（她的命名本来就是"这类图叫什么"），没有发送记录时
    ``top_name`` 为空串——调用方只把有值的那半句念出来，不编。
    """

    total_row = conn.execute(
        "SELECT COUNT(*) AS n FROM meme_entries WHERE status = ?",
        (MEME_ENTRY_STATUS_ENABLED,),
    ).fetchone()
    never_row = conn.execute(
        "SELECT COUNT(*) AS n FROM meme_entries e WHERE e.status = ?"
        " AND NOT EXISTS (SELECT 1 FROM meme_usage u WHERE u.entry_id = e.entry_id"
        " AND u.kind IN (?, ?))",
        (MEME_ENTRY_STATUS_ENABLED, MEME_USAGE_SENT, MEME_USAGE_ACCEPTED),
    ).fetchone()
    top = conn.execute(
        "SELECT e.name AS name, COUNT(*) AS n FROM meme_usage u"
        " JOIN meme_entries e ON e.entry_id = u.entry_id"
        " WHERE u.kind IN (?, ?) AND u.created_at >= ? AND e.status = ?"
        " GROUP BY e.entry_id ORDER BY n DESC, e.name ASC LIMIT 1",
        (
            MEME_USAGE_SENT,
            MEME_USAGE_ACCEPTED,
            _now_or(since),
            MEME_ENTRY_STATUS_ENABLED,
        ),
    ).fetchone()
    return {
        "total": int(total_row["n"] if total_row is not None else 0),
        "never_sent": int(never_row["n"] if never_row is not None else 0),
        "top_name": str(top["name"] or "") if top is not None else "",
        "top_count": int(top["n"] or 0) if top is not None else 0,
    }


def get_meme_entry(
    conn: sqlite3.Connection, entry_id: Any
) -> dict[str, Any] | None:
    """按 ID 取一条收藏（**含**已软删的；调用方自己看 ``status``）。"""

    return _meme_entry_row(conn, _meme_text(entry_id))


def list_meme_entries(
    conn: sqlite3.Connection,
    *,
    scope_id: Any = None,
    status: Any = None,
    media_id: Any = None,
    include_deleted: bool = False,
    limit: int = 200,
    offset: int = 0,
    order: str = "collected",
    desc: bool = True,
) -> list[dict[str, Any]]:
    """列收藏（连资产元数据一起带出，页面上一条 SQL 就够，不用 N+1）。

    ``scope_id`` / ``status`` / ``media_id`` 不给（``None``）或给空串表示不过滤。
    ``include_deleted=False``（默认）时软删的条目不出现在列表里；要看它们得显式要。
    ``limit`` 上限 1000（``<= 0`` 按 200 算）。

    ``order``（用户 m21273 要的排序）：``collected``（收藏时间，默认）/
    ``used``（已用次数）/ ``updated``（最后改动）。``desc`` 默认 True——最新收藏、
    用得最多的在最前面。排序键之外一律按 ``entry_id`` 兜底，保证翻页稳定。
    """

    where: list[str] = []
    args: list[Any] = []
    scope = _meme_text(scope_id)
    if scope:
        where.append("e.scope_id = ?")
        args.append(scope)
    state = _meme_text(status)
    if state:
        where.append("e.status = ?")
        args.append(state)
    elif not include_deleted:
        where.append("e.status != ?")
        args.append(MEME_ENTRY_STATUS_DELETED)
    media = _meme_text(media_id)
    if media:
        where.append("e.media_id = ?")
        args.append(media)
    rows_wanted = _meme_int(limit, 200)
    if rows_wanted <= 0:
        rows_wanted = 200
    rows_wanted = min(rows_wanted, 1000)
    skip = max(_meme_int(offset, 0), 0)
    # 已用次数：一次子查询数好，别在页面里 N+1（用户 m21273 要"每张显示已用次数"）。
    # 只算"真发出去"的那几种 kind，``attempted`` 是"想发"、不该算使用。
    usage_join = (
        " LEFT JOIN ("
        "SELECT entry_id, COUNT(*) AS use_count FROM meme_usage"
        " WHERE kind IN ('sent', 'accepted', 'receipt') GROUP BY entry_id"
        ") u ON u.entry_id = e.entry_id"
    )
    sql = (
        "SELECT e.*, COALESCE(u.use_count, 0) AS use_count,"
        " a.mime AS asset_mime, a.size_bytes AS asset_size_bytes,"
        " a.width AS asset_width, a.height AS asset_height,"
        " a.frame_count AS asset_frame_count, a.is_animated AS asset_is_animated,"
        " a.tags AS asset_tags, a.content_hash AS asset_content_hash,"
        " CASE WHEN a.media_id IS NULL THEN 0 ELSE 1 END AS asset_known"
        " FROM meme_entries e LEFT JOIN meme_assets a ON a.media_id = e.media_id"
    ) + usage_join
    if where:
        sql += " WHERE " + " AND ".join(where)
    direction = "DESC" if desc else "ASC"
    sort_expr = {
        "used": "COALESCE(u.use_count, 0)",
        "updated": "e.updated_at",
        "collected": "e.created_at",
    }.get(str(order or "").strip().lower(), "e.created_at")
    sql += (
        f" ORDER BY {sort_expr} {direction}, e.created_at {direction}, e.entry_id DESC"
        " LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, (*args, rows_wanted, skip)).fetchall()
    return [dict(row) for row in rows]


def count_meme_entries(
    conn: sqlite3.Connection, *, status: Any = None, include_deleted: bool = False
) -> int:
    """数收藏条数（``status`` 不给时按 ``include_deleted`` 决定算不算软删的）。"""

    where: list[str] = []
    args: list[Any] = []
    state = _meme_text(status)
    if state:
        where.append("status = ?")
        args.append(state)
    elif not include_deleted:
        where.append("status != ?")
        args.append(MEME_ENTRY_STATUS_DELETED)
    sql = "SELECT COUNT(*) AS n FROM meme_entries"
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = conn.execute(sql, tuple(args)).fetchone()
    return int(row["n"] if row is not None else 0)


# ``update_meme_entry`` 允许改的列。**故意不包含** ``entry_id`` / ``media_id`` /
# ``created_at`` / ``revision`` / ``deleted_at``：身份与版本号不接受外部直接赋值，
# 换底层文件要走 :func:`replace_meme_entry_media`，版本号由这里自己 +1。
MEME_ENTRY_EDITABLE_COLUMNS = (
    "name",
    "scope_id",
    "persona_id",
    "meaning",
    "use_when",
    "avoid_when",
    "personal_note",
    "preference",
    "pinned",
)


def update_meme_entry(
    conn: sqlite3.Connection,
    entry_id: Any,
    *,
    expected_revision: Any = None,
    now: Any = 0.0,
    **fields: Any,
) -> dict[str, Any]:
    """改一条收藏的语义字段。

    * ``expected_revision`` 给了且与当前 ``revision`` 不等 → 不写、返回
      ``{"ok": False, "reason": "version_conflict", ...}``（页面并发编辑口径）。
    * 真的改了东西 → ``revision + 1``、``updated_at`` 更新。
    * 一个字段都没认出来 / 值没变 → ``revision`` **不动**（不制造假版本）。

    ``status`` / ``deleted_at`` 不在这里改：禁用走 :func:`set_meme_entry_status`，
    删除走 :func:`delete_meme_entry`。
    """

    target = _meme_text(entry_id)
    row = _meme_entry_row(conn, target)
    if row is None:
        return {"ok": False, "reason": "not_found", "entry_id": target}
    wanted_revision = _meme_int(expected_revision, 0) if expected_revision else 0
    if wanted_revision and wanted_revision != _meme_int(row.get("revision"), 1):
        return {
            "ok": False,
            "reason": "version_conflict",
            "entry_id": target,
            "revision": _meme_int(row.get("revision"), 1),
        }
    assignments: list[str] = []
    args: list[Any] = []
    for name in MEME_ENTRY_EDITABLE_COLUMNS:
        if name not in fields:
            continue
        value = fields[name]
        if name == "preference":
            value = _meme_float(value, 0.0)
        elif name == "pinned":
            value = 1 if value else 0
        else:
            value = _meme_text(value)
        current = row.get(name)
        if isinstance(current, float) or isinstance(value, float):
            same = _meme_float(current, 0.0) == _meme_float(value, 0.0)
        else:
            same = current == value
        if same:
            continue
        assignments.append(f"{name} = ?")
        args.append(value)
    if not assignments:
        row.pop("created", None)
        return {"ok": True, "entry": row, "changed": False, "revision": row["revision"]}
    next_revision = _meme_int(row.get("revision"), 1) + 1
    assignments.append("revision = ?")
    args.append(next_revision)
    assignments.append("updated_at = ?")
    args.append(_now_or(now))
    conn.execute(
        f"UPDATE meme_entries SET {', '.join(assignments)} WHERE entry_id = ?",
        (*args, target),
    )
    conn.commit()
    updated = _meme_entry_row(conn, target)
    assert updated is not None
    return {"ok": True, "entry": updated, "changed": True, "revision": next_revision}


def replace_meme_entry_media(
    conn: sqlite3.Connection,
    entry_id: Any,
    media_id: Any,
    *,
    now: Any = 0.0,
) -> dict[str, Any]:
    """给一条收藏换底层图片（改 ``media_id`` + ``revision + 1``）。

    单独一个函数而不是塞进 ``update_meme_entry`` 的通用字段里：换图是"这条收藏变成
    另一张图"，必须显式走这一步，页面上也才好单独给提示。
    """

    target = _meme_text(entry_id)
    key = _meme_text(media_id)
    row = _meme_entry_row(conn, target)
    if row is None or not key:
        return {"ok": False, "reason": "not_found", "entry_id": target}
    if _meme_text(row.get("media_id")) == key:
        row.pop("created", None)
        return {"ok": True, "entry": row, "changed": False, "revision": row["revision"]}
    owner = find_meme_entry_by_media(conn, key)
    if owner is not None and _meme_text(owner.get("entry_id")) != target:
        return {
            "ok": False,
            "reason": "media_in_use",
            "entry_id": target,
            "owner": _meme_text(owner.get("entry_id")),
        }
    next_revision = _meme_int(row.get("revision"), 1) + 1
    conn.execute(
        "UPDATE meme_entries SET media_id = ?, revision = ?, updated_at = ?"
        " WHERE entry_id = ?",
        (key, next_revision, _now_or(now), target),
    )
    conn.commit()
    updated = _meme_entry_row(conn, target)
    assert updated is not None
    return {"ok": True, "entry": updated, "changed": True, "revision": next_revision}


def set_meme_entry_status(
    conn: sqlite3.Connection,
    entry_id: Any,
    status: Any,
    *,
    now: Any = 0.0,
) -> dict[str, Any] | None:
    """改收藏的启用状态（``enabled`` / ``disabled`` / ``deleted``）。

    ``revision`` **跟着 +1**：状态也是这条收藏的一个版本（页面靠它挡并发编辑）。
    ``deleted`` 顺带写 ``deleted_at``；从 ``deleted`` 回到 ``enabled`` 时清掉它。
    """

    target = _meme_text(entry_id)
    wanted = _meme_text(status)
    if wanted not in (
        MEME_ENTRY_STATUS_ENABLED,
        MEME_ENTRY_STATUS_DISABLED,
        MEME_ENTRY_STATUS_DELETED,
    ):
        return None
    row = _meme_entry_row(conn, target)
    if row is None:
        return None
    stamp = _now_or(now)
    next_revision = _meme_int(row.get("revision"), 1) + 1
    deleted_at = stamp if wanted == MEME_ENTRY_STATUS_DELETED else 0.0
    conn.execute(
        "UPDATE meme_entries SET status = ?, revision = ?, deleted_at = ?,"
        " updated_at = ? WHERE entry_id = ?",
        (wanted, next_revision, deleted_at, stamp, target),
    )
    conn.commit()
    return _meme_entry_row(conn, target)


def delete_meme_entry(
    conn: sqlite3.Connection, entry_id: Any, *, now: Any = 0.0
) -> bool:
    """软删一条收藏（``status = 'deleted'``）：记录留着、底层文件不动。"""

    return set_meme_entry_status(
        conn, entry_id, MEME_ENTRY_STATUS_DELETED, now=now
    ) is not None


def purge_meme_entry(conn: sqlite3.Connection, entry_id: Any) -> bool:
    """**硬删**一条收藏（连带来源/变体/用法记录）。

    管理页里的"彻底删掉这条记录"用它——与软删分开：软删只是不让她用，
    硬删是"我连这条收藏都不想要了"。底层图片文件仍然由资产库自己决定去留。
    """

    target = _meme_text(entry_id)
    if not target:
        return False
    conn.execute("DELETE FROM meme_sources WHERE entry_id = ?", (target,))
    conn.execute("DELETE FROM meme_usage WHERE entry_id = ?", (target,))
    conn.execute(
        "DELETE FROM meme_variants WHERE entry_a = ? OR entry_b = ?", (target, target)
    )
    cursor = conn.execute("DELETE FROM meme_entries WHERE entry_id = ?", (target,))
    conn.commit()
    return bool(cursor.rowcount)


def add_meme_source(
    conn: sqlite3.Connection,
    *,
    entry_id: Any,
    session_id: Any = "",
    message_id: Any = "",
    sender_id: Any = "",
    origin: Any = "",
    origin_kind: Any = "",
    allowed_scopes: Any = MEME_SCOPE_ANY,
    occurred_at: Any = 0.0,
    now: Any = 0.0,
) -> int:
    """给一条收藏登记一个来源（同一来源重复登记只留一行）。

    返回来源行的 ``source_id``；``entry_id`` 为空时返回 0（不写）。
    **多来源不会放宽限制**：每个来源各自记 ``allowed_scopes``，真正判断走
    :func:`core.meme_settings.meme_scope_intersection`（全过才算过）。
    """

    target = _meme_text(entry_id)
    if not target:
        return 0
    session = _meme_text(session_id)
    message = _meme_text(message_id)
    origin_text = _meme_text(origin)
    scopes = _meme_text(allowed_scopes) or MEME_SCOPE_ANY
    existing = conn.execute(
        "SELECT source_id FROM meme_sources WHERE entry_id = ? AND session_id = ?"
        " AND message_id = ? AND origin = ?",
        (target, session, message, origin_text),
    ).fetchone()
    if existing is not None:
        return int(existing["source_id"])
    stamp = _now_or(now)
    cursor = conn.execute(
        "INSERT INTO meme_sources"
        " (entry_id, session_id, message_id, sender_id, origin, origin_kind,"
        "  allowed_scopes, occurred_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target,
            session,
            message,
            _meme_text(sender_id),
            origin_text,
            _meme_text(origin_kind),
            scopes,
            _now_or(occurred_at),
            stamp,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def list_meme_sources(
    conn: sqlite3.Connection, entry_id: Any
) -> list[dict[str, Any]]:
    """一条收藏的全部来源（多来源不能放宽限制，所以判断时要全看一遍）。"""

    target = _meme_text(entry_id)
    if not target:
        return []
    rows = conn.execute(
        "SELECT * FROM meme_sources WHERE entry_id = ? ORDER BY source_id",
        (target,),
    ).fetchall()
    return [dict(row) for row in rows]


def meme_scope_set(conn: sqlite3.Connection, entry_id: Any) -> list[str]:
    """一条收藏各来源的 ``allowed_scopes``（给 ``meme_scope_intersection`` 用）。"""

    return [_meme_text(row.get("allowed_scopes")) or MEME_SCOPE_ANY
            for row in list_meme_sources(conn, entry_id)]


def add_meme_variant(
    conn: sqlite3.Connection,
    *,
    entry_a: Any,
    entry_b: Any,
    relation: Any = "suspected",
    similarity: Any = 0.0,
    diff_note: Any = "",
    now: Any = 0.0,
) -> dict[str, Any]:
    """登记一次"这两条是变体"（疑似或确认），**保留差异说明**。

    两条 ID 相同 / 有一条为空 → 返回 ``{"ok": False, "reason": ...}``，一行不写
    （"这张图是它自己的变体"没有意义）。顺序无关：``(a, b)`` 与 ``(b, a)`` 归一行。
    """

    left = _meme_text(entry_a)
    right = _meme_text(entry_b)
    if not left or not right:
        return {"ok": False, "reason": "missing_entry"}
    if left == right:
        return {"ok": False, "reason": "same_entry"}
    first, second = sorted((left, right))
    stamp = _now_or(now)
    existing = conn.execute(
        "SELECT * FROM meme_variants WHERE entry_a = ? AND entry_b = ?",
        (first, second),
    ).fetchone()
    if existing is not None:
        row = dict(existing)
        row["created"] = False
        return {"ok": True, "variant": row, "created": False}
    conn.execute(
        "INSERT INTO meme_variants"
        " (entry_a, entry_b, relation, similarity, diff_note, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            first,
            second,
            _meme_text(relation) or "suspected",
            _meme_float(similarity, 0.0),
            _meme_text(diff_note),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM meme_variants WHERE entry_a = ? AND entry_b = ?",
        (first, second),
    ).fetchone()
    result = dict(row) if row is not None else {}
    result["created"] = True
    return {"ok": True, "variant": result, "created": True}


def list_meme_variants(
    conn: sqlite3.Connection, entry_id: Any
) -> list[dict[str, Any]]:
    """一条收藏牵扯到的全部变体关系（两个方向都算）。"""

    target = _meme_text(entry_id)
    if not target:
        return []
    rows = conn.execute(
        "SELECT * FROM meme_variants WHERE entry_a = ? OR entry_b = ?"
        " ORDER BY variant_id",
        (target, target),
    ).fetchall()
    return [dict(row) for row in rows]


def remove_meme_variant(
    conn: sqlite3.Connection,
    entry_id: Any,
    other_id: Any,
    *,
    now: Any = 0.0,
) -> bool:
    """取消一次变体关系（**只在疑似误判时用**；确认过的变体关系不该被静默丢掉）。"""

    left = _meme_text(entry_id)
    right = _meme_text(other_id)
    if not left or not right or left == right:
        return False
    first, second = sorted((left, right))
    cursor = conn.execute(
        "DELETE FROM meme_variants WHERE entry_a = ? AND entry_b = ?",
        (first, second),
    )
    conn.commit()
    return bool(cursor.rowcount)


def add_meme_usage(
    conn: sqlite3.Connection,
    *,
    entry_id: Any,
    session_id: Any = "",
    message_id: Any = "",
    phase_id: Any = "",
    kind: Any = MEME_USAGE_ATTEMPTED,
    outcome: Any = "",
    reason: Any = "",
    detail: Any = None,
    now: Any = 0.0,
) -> int:
    """记一次用法/回执（想发、发出去了、平台回执、真正被接受）。

    返回 ``usage_id``；``entry_id`` 空则返回 0。``detail`` 是任意 JSON 化的补充信息。
    """

    target = _meme_text(entry_id)
    if not target:
        return 0
    payload = detail if isinstance(detail, dict) else {}
    cursor = conn.execute(
        "INSERT INTO meme_usage"
        " (entry_id, session_id, message_id, phase_id, kind, outcome, reason,"
        "  detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target,
            _meme_text(session_id),
            _meme_text(message_id),
            _meme_text(phase_id),
            _meme_text(kind) or MEME_USAGE_ATTEMPTED,
            _meme_text(outcome),
            _meme_text(reason),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            _now_or(now),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def list_meme_usage(
    conn: sqlite3.Connection,
    *,
    entry_id: Any = None,
    session_id: Any = None,
    kind: Any = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """列用法记录（新的在前）。三个过滤项不给就不过滤，``limit`` 上限 1000。"""

    where: list[str] = []
    args: list[Any] = []
    for column, value in (
        ("entry_id", entry_id),
        ("session_id", session_id),
        ("kind", kind),
    ):
        text = _meme_text(value)
        if text:
            where.append(f"{column} = ?")
            args.append(text)
    rows_wanted = _meme_int(limit, 200)
    if rows_wanted <= 0:
        rows_wanted = 200
    rows_wanted = min(rows_wanted, 1000)
    sql = "SELECT * FROM meme_usage"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY usage_id DESC LIMIT ?"
    rows = conn.execute(sql, (*args, rows_wanted)).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["detail"] = json.loads(item.get("detail_json") or "{}")
        except (TypeError, ValueError):
            item["detail"] = {}
        result.append(item)
    return result


def count_meme_usage(
    conn: sqlite3.Connection,
    *,
    entry_id: Any = None,
    session_id: Any = None,
    kind: Any = None,
    since: Any = None,
) -> int:
    """数用法记录；``since`` 给时间戳时只算之后的（"这一小时发了几张"用它）。"""

    where: list[str] = []
    args: list[Any] = []
    for column, value in (
        ("entry_id", entry_id),
        ("session_id", session_id),
        ("kind", kind),
    ):
        text = _meme_text(value)
        if text:
            where.append(f"{column} = ?")
            args.append(text)
    if since is not None:
        where.append("created_at >= ?")
        args.append(_now_or(since))
    sql = "SELECT COUNT(*) AS n FROM meme_usage"
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = conn.execute(sql, tuple(args)).fetchone()
    return int(row["n"] if row is not None else 0)


def build_occurrence_id(session_id: Any, seq: Any, index: Any = 0) -> str:
    """拼一次"出现"的 ID：``"{session}#{seq}#{index}"``。

    ``seq`` 是留档行序号（同一条消息里同一个 seq 就是同一"次"），``index`` 是这次
    出现里第几张图。**别人重发同一张图 = 新的 seq = 新的 occurrence**，
    收藏实体那边仍按 ``media_id`` 去重。
    """

    session = _meme_text(session_id)
    return f"{session}#{_meme_int(seq, 0)}#{_meme_int(index, 0)}"


def parse_occurrence_id(occurrence_id: Any) -> dict[str, Any]:
    """把 ``occurrence_id`` 拆回 ``session`` / ``seq`` / ``index``（认不出给空值）。"""

    text = _meme_text(occurrence_id)
    parts = text.rsplit("#", 2)
    if len(parts) != 3:
        return {"session": "", "seq": 0, "index": 0}
    session, seq, index = parts
    return {"session": session, "seq": _meme_int(seq, 0), "index": _meme_int(index, 0)}


def claim_exposure(
    conn: sqlite3.Connection,
    *,
    occurrence_id: Any,
    phase_id: Any,
    session_id: Any = "",
    media_id: Any = None,
    now: Any = 0.0,
) -> dict[str, Any]:
    """认领"这一次出现"（**并发安全**，靠 ``(occurrence_id, phase_id)`` 主键）。

    语义（§3.3）：

    * ``delivered`` 封死：已经发出去过的那一次出现**不允许**再认领。
    * ``failed`` 可重试：请求失败 / 超时 / 被预算丢弃都不算展示，下一轮还能认领。
    * 已有活跃 ``claimed``（别的端正在补附）→ 不抢，返回 ``reserved``。
    * ``media_id`` 给了就核对：出现记录上钉的是另一份内容时不认领（``media_changed``），
      防止"同一次出现已经换成别的图了"还被补上旧图。

    返回 ``{"ok": bool, "state": str, "reason": str, "row": dict|None}``。
    """

    occurrence = _meme_text(occurrence_id)
    phase = _meme_text(phase_id)
    if not occurrence or not phase:
        return {"ok": False, "state": "", "reason": "bad_key", "row": None}
    stamp = _now_or(now)
    key = _meme_text(media_id)
    for _attempt in range(2):
        row = conn.execute(
            "SELECT * FROM image_exposures WHERE occurrence_id = ? AND phase_id = ?",
            (occurrence, phase),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO image_exposures"
                " (occurrence_id, session_id, media_id, phase_id, state, claimed_at,"
                "  updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    occurrence,
                    _meme_text(session_id),
                    key if key else None,
                    phase,
                    MEME_EXPOSURE_CLAIMED,
                    stamp,
                    stamp,
                ),
            )
            conn.commit()
            if cursor.rowcount:
                return {
                    "ok": True,
                    "state": MEME_EXPOSURE_CLAIMED,
                    "reason": "claimed",
                    "row": _exposure_row(conn, occurrence, phase),
                }
            continue  # 有人抢先插进去了，下面按已有行判断
        current = _meme_text(row["state"])
        stored_media = _meme_text(row["media_id"])
        if current == MEME_EXPOSURE_DELIVERED:
            return {
                "ok": False,
                "state": current,
                "reason": "already_delivered",
                "row": dict(row),
            }
        if current == MEME_EXPOSURE_CLAIMED:
            return {
                "ok": False,
                "state": current,
                "reason": "reserved",
                "row": dict(row),
            }
        if key and stored_media and stored_media != key:
            return {
                "ok": False,
                "state": current,
                "reason": "media_changed",
                "row": dict(row),
            }
        # pending / failed → 认领（重试）
        cursor = conn.execute(
            "UPDATE image_exposures SET state = ?, claimed_at = ?, updated_at = ?,"
            " session_id = CASE WHEN ? = '' THEN session_id ELSE ? END,"
            " media_id = CASE WHEN ? IS NULL THEN media_id ELSE ? END"
            " WHERE occurrence_id = ? AND phase_id = ? AND state = ?",
            (
                MEME_EXPOSURE_CLAIMED,
                stamp,
                stamp,
                _meme_text(session_id),
                _meme_text(session_id),
                key if key else None,
                key if key else None,
                occurrence,
                phase,
                current,
            ),
        )
        conn.commit()
        if cursor.rowcount:
            return {
                "ok": True,
                "state": MEME_EXPOSURE_CLAIMED,
                "reason": "claimed",
                "row": _exposure_row(conn, occurrence, phase),
            }
        # 状态在两次读之间又变了（别的端抢先）→ 再读一次按新状态判断
    row = conn.execute(
        "SELECT * FROM image_exposures WHERE occurrence_id = ? AND phase_id = ?",
        (occurrence, phase),
    ).fetchone()
    return {
        "ok": False,
        "state": _meme_text(row["state"]) if row is not None else "",
        "reason": "raced",
        "row": dict(row) if row is not None else None,
    }


def _exposure_row(
    conn: sqlite3.Connection, occurrence_id: str, phase_id: str
) -> dict[str, Any] | None:
    """取一行展示记录（认领成功后的回执用）。"""

    row = conn.execute(
        "SELECT * FROM image_exposures WHERE occurrence_id = ? AND phase_id = ?",
        (occurrence_id, phase_id),
    ).fetchone()
    return dict(row) if row is not None else None


def mark_exposure(
    conn: sqlite3.Connection,
    *,
    occurrence_id: Any,
    phase_id: Any,
    state: Any,
    now: Any = 0.0,
) -> bool:
    """把一次认领收尾成 ``delivered`` / ``failed``（别的取值不认，返回 ``False``）。

    ``delivered`` 之后这一行就**封死**了（:func:`claim_exposure` 不再认领）；
    ``failed`` 表示"这次不算展示"，下一轮可以重试。已经 ``delivered`` 的行不会
    被 ``failed`` 覆盖回去（发出去了就是发出去了）。
    """

    wanted = _meme_text(state)
    if wanted not in (MEME_EXPOSURE_DELIVERED, MEME_EXPOSURE_FAILED):
        return False
    occurrence = _meme_text(occurrence_id)
    phase = _meme_text(phase_id)
    if not occurrence or not phase:
        return False
    cursor = conn.execute(
        "UPDATE image_exposures SET state = ?, updated_at = ?"
        " WHERE occurrence_id = ? AND phase_id = ? AND state != ?",
        (wanted, _now_or(now), occurrence, phase, MEME_EXPOSURE_DELIVERED),
    )
    conn.commit()
    return bool(cursor.rowcount)


def release_exposure(
    conn: sqlite3.Connection,
    *,
    occurrence_id: Any,
    phase_id: Any,
    now: Any = 0.0,
) -> bool:
    """把认领退回 ``pending``（进程崩了留下的租约要能放掉）。

    ``delivered`` 的行不动（已经发出去了，不能因为崩过一次就允许重发）。
    """

    occurrence = _meme_text(occurrence_id)
    phase = _meme_text(phase_id)
    if not occurrence or not phase:
        return False
    cursor = conn.execute(
        "UPDATE image_exposures SET state = ?, claimed_at = 0, updated_at = ?"
        " WHERE occurrence_id = ? AND phase_id = ? AND state = ?",
        (
            MEME_EXPOSURE_PENDING,
            _now_or(now),
            occurrence,
            phase,
            MEME_EXPOSURE_CLAIMED,
        ),
    )
    conn.commit()
    return bool(cursor.rowcount)


def get_exposure(
    conn: sqlite3.Connection, *, occurrence_id: Any, phase_id: Any
) -> dict[str, Any] | None:
    """取一行展示记录（没有返回 ``None``）。"""

    occurrence = _meme_text(occurrence_id)
    phase = _meme_text(phase_id)
    if not occurrence or not phase:
        return None
    return _exposure_row(conn, occurrence, phase)


def find_exposure(
    conn: sqlite3.Connection, occurrence_id: Any
) -> list[dict[str, Any]]:
    """一次出现在各阶段（phase）上的全部记录。"""

    occurrence = _meme_text(occurrence_id)
    if not occurrence:
        return []
    rows = conn.execute(
        "SELECT * FROM image_exposures WHERE occurrence_id = ?"
        " ORDER BY phase_id",
        (occurrence,),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_exposure_delivered(
    conn: sqlite3.Connection, *, occurrence_id: Any, phase_id: Any, now: Any = 0.0
) -> bool:
    """:func:`mark_exposure` 的 ``delivered`` 简写（只有它封死这次出现）。"""

    return mark_exposure(
        conn,
        occurrence_id=occurrence_id,
        phase_id=phase_id,
        state=MEME_EXPOSURE_DELIVERED,
        now=now,
    )


def mark_exposure_failed(
    conn: sqlite3.Connection, *, occurrence_id: Any, phase_id: Any, now: Any = 0.0
) -> bool:
    """:func:`mark_exposure` 的 ``failed`` 简写（**不算展示**，下一轮可重试）。"""

    return mark_exposure(
        conn,
        occurrence_id=occurrence_id,
        phase_id=phase_id,
        state=MEME_EXPOSURE_FAILED,
        now=now,
    )


def count_exposures(
    conn: sqlite3.Connection, *, state: Any = None, session_id: Any = None
) -> int:
    """数展示记录（``state`` / ``session_id`` 不给就不过滤）。"""

    where: list[str] = []
    args: list[Any] = []
    wanted = _meme_text(state)
    if wanted:
        where.append("state = ?")
        args.append(wanted)
    session = _meme_text(session_id)
    if session:
        where.append("session_id = ?")
        args.append(session)
    sql = "SELECT COUNT(*) AS n FROM image_exposures"
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = conn.execute(sql, tuple(args)).fetchone()
    return int(row["n"] if row is not None else 0)


def cleanup_meme_exposures(
    conn: sqlite3.Connection,
    *,
    keep_days: Any = 30,
    now: Any = 0.0,
) -> int:
    """按天数清理展示记录（与图片转述缓存的 ``cache_days`` 同口径）。

    ``keep_days <= 0`` 表示**不过期**（一行不删）——与 ``vision.cache_days = 0``
    的既有语义一致。只看 ``updated_at``：``delivered`` 的行过了保留期照样清掉，
    它挡住的是"这一次出现"，不是"这条收藏"。
    返回删掉的行数。
    """

    try:
        days = int(keep_days)
    except (TypeError, ValueError):
        days = 30
    if days <= 0:
        return 0
    cutoff = _now_or(now) - days * 86400.0
    cursor = conn.execute(
        "DELETE FROM image_exposures WHERE updated_at < ?", (cutoff,)
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def cleanup_meme_usage(
    conn: sqlite3.Connection, *, keep_days: Any = 90, now: Any = 0.0
) -> int:
    """按天数清用法记录（默认留 90 天）。``keep_days <= 0`` = 不过期。"""

    try:
        days = int(keep_days)
    except (TypeError, ValueError):
        days = 90
    if days <= 0:
        return 0
    cutoff = _now_or(now) - days * 86400.0
    cursor = conn.execute("DELETE FROM meme_usage WHERE created_at < ?", (cutoff,))
    conn.commit()
    return int(cursor.rowcount or 0)


# ---------------------------------------------------------------------------
# 阶段A4 追加分区：config_index_queue（配置异步整理队列）
#
# 设计见 .vwtest/embedding-b-work-arrangement.md §11.3 的 A4 行与
# .vwtest/vw-action-capability-index-design.md §5。这个队列记的是「哪一条配置该重新
# 整理派生信息」：**不存配置本体**（本体在世界配置里）、**不存密钥**（指纹只由调用方
# 按白名单算好传进来，见 core/action_discovery.pick_input_fields）。
#
# 与 topic_index_queue 的关系：**表独立**（那是谈资向量，这是配置派生），但范式照抄——
# 幂等键、due 挑选、状态白名单、按目标清空。多出来的两样是这一层真正需要的东西：
#
# * **租约**（``lease_owner`` / ``lease_token`` / ``lease_expires_at``）：多 worker 抢同一行
#   时只有一个能写回；写回一律带令牌，令牌对不上就**丢弃迟到结果**（不能拿旧结果盖新编辑）；
# * **结果暂存**（``result_json``）：worker 只把**允许回写的派生字段**写进这一列，
#   真正落进世界配置由上层（A5）显式 ``apply`` 一次，避免"后台拿着旧快照覆盖整份配置"。
#
# 幂等：``_migrate_config_index_queue`` 由 ``Database._migrate()`` 调用，空库 / 旧库 /
# 已迁移的库都能重复跑，既有数据一行不动；失败整体回滚后把异常抛出去（旧库保持可用），
# 不静默降级。
# ---------------------------------------------------------------------------


# 建表列顺序（测试按这个顺序验"列齐全"）。
CONFIG_INDEX_QUEUE_COLUMNS = (
    "id",
    "kind",
    "target_id",
    "scope_id",
    "input_hash",
    "source_revision",
    "status",
    "attempts",
    "next_attempt_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "result_json",
    "error_code",
    "created_at",
    "updated_at",
)

# 唯一键三列：处理器类型 + 目标身份 + 输入指纹。同一个目标的同一份输入只排一次队；
# 定义改了（指纹变了）就是**另一行**，新行做完之前旧行的结果不许落（见 complete）。
CONFIG_INDEX_QUEUE_UNIQUE_COLUMNS = ("kind", "target_id", "input_hash")

CONFIG_INDEX_QUEUE_STATUSES = (
    "pending",
    "leased",
    "ready",
    "done",
    "failed",
    "stale",
    "cancelled",
)
"""``pending`` 待整理；``leased`` 被某个 worker 租下；``ready`` 结果算好了、等上层落配置；
``done`` 已落；``failed`` 重试用尽；``stale`` 迟到结果被丢弃；``cancelled`` 目标删除 / 禁用。"""

CONFIG_INDEX_QUEUE_DEFAULT_STATUS = "pending"

CONFIG_INDEX_QUEUE_LIVE_STATUSES = ("pending", "leased", "ready")
"""还在"路上"的状态：目标被删 / 禁用时要一次性全部作废的就是这三种。"""

CONFIG_INDEX_QUEUE_TERMINAL_STATUSES = ("done", "failed", "stale", "cancelled")
"""终态：不会被自动重试，只有新的入队（新指纹）才会再排一行。"""

# 重试退避上限（秒）：延迟 = min(这个值, 2 ** attempts)，与 embedding_index 同源。
CONFIG_INDEX_QUEUE_MAX_BACKOFF = 60.0

# 租约默认时长（秒）：worker 在这段时间内没动静，别的 worker（或重启后的自己）可以回收。
CONFIG_INDEX_QUEUE_DEFAULT_LEASE_SECONDS = 120.0

# ``complete_config_index`` / ``fail_config_index`` 的返回原因码。
CONFIG_INDEX_COMPLETE_READY = "ready"
CONFIG_INDEX_COMPLETE_SUPERSEDED = "superseded"
CONFIG_INDEX_COMPLETE_LEASE_LOST = "lease_lost"

_CONFIG_INDEX_QUEUE_DDL = f"""
CREATE TABLE IF NOT EXISTS config_index_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 处理器类型（core/config_index_queue.py 的处理器表按它派发；'' 一律拒收）
    kind TEXT NOT NULL,
    -- 目标身份文本（动作索引里就是 "core::say" 这种 (命名空间, 动作 id) 身份）
    target_id TEXT NOT NULL,
    -- 配置 / 世界身份：按作用域清空与计数用（'' = 不分组）
    scope_id TEXT NOT NULL DEFAULT '',
    -- 输入指纹：**只含本处理器需要的字段**，由调用方算好传进来（绝不写密钥）
    input_hash TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '{CONFIG_INDEX_QUEUE_DEFAULT_STATUS}',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    lease_expires_at REAL NOT NULL DEFAULT 0,
    -- 只放"允许回写的派生字段"（JSON 对象文本），不落原始配置、不落密钥
    result_json TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE (kind, target_id, input_hash)
);
-- 取 due 行就是按 (kind, status, next_attempt_at, id) 升序扫的，这条索引照那个查询建
CREATE INDEX IF NOT EXISTS idx_config_index_queue_due
    ON config_index_queue (kind, status, next_attempt_at, id);
-- 同一个目标的历史行 / 有没有更新的行（过期结果丢弃要按它查）
CREATE INDEX IF NOT EXISTS idx_config_index_queue_target
    ON config_index_queue (kind, target_id, id);
-- 按作用域清空 / 计数
CREATE INDEX IF NOT EXISTS idx_config_index_queue_scope
    ON config_index_queue (scope_id, status);
"""


def _migrate_config_index_queue(conn: sqlite3.Connection) -> None:
    """建 ``config_index_queue`` 表与三条索引（可重复调用）。

    空库、没有这张表的旧库、已经迁移过的库都能跑，既有数据一行不动；失败整体回滚
    （旧库保持可用）后把异常抛出去，不静默降级。**只建表，不往里写任何数据。**
    """

    _run_ddl(conn, _CONFIG_INDEX_QUEUE_DDL)


def _config_index_text(value: Any) -> str:
    """身份列的文本归一：``None`` 当空串，两端空白去掉（写入与查询同一套）。"""

    return str(value or "").strip()


def _config_index_row(row: Any) -> dict[str, Any] | None:
    """``config_index_queue`` 一行 → dict（列名与 DDL 一致，数值列强转回数）。"""

    if row is None:
        return None
    item = dict(row)
    item["id"] = _int_or(item.get("id"), 0)
    item["kind"] = str(item.get("kind") or "")
    item["target_id"] = str(item.get("target_id") or "")
    item["scope_id"] = str(item.get("scope_id") or "")
    item["input_hash"] = str(item.get("input_hash") or "")
    item["source_revision"] = str(item.get("source_revision") or "")
    item["status"] = str(item.get("status") or "")
    item["attempts"] = _int_or(item.get("attempts"), 0)
    item["next_attempt_at"] = _float_or(item.get("next_attempt_at"), 0.0)
    item["lease_owner"] = str(item.get("lease_owner") or "")
    item["lease_token"] = str(item.get("lease_token") or "")
    item["lease_expires_at"] = _float_or(item.get("lease_expires_at"), 0.0)
    item["result_json"] = str(item.get("result_json") or "")
    item["error_code"] = str(item.get("error_code") or "")
    item["created_at"] = _float_or(item.get("created_at"), 0.0)
    item["updated_at"] = _float_or(item.get("updated_at"), 0.0)
    return item


def enqueue_config_index(
    conn: sqlite3.Connection,
    *,
    kind: str,
    target_id: str,
    input_hash: str,
    scope_id: str = "",
    source_revision: str = "",
    now: float = 0.0,
    force: bool = False,
) -> bool:
    """入队一行整理作业：**True = 这次真的入队**（新插一行，或把死行拉起来）。

    ``force=True`` 是**强制重算**（P0-2）：把 ``ready`` / ``done`` 的行也拉回 ``pending``
    重跑一遍——用户点"重建"时得真的重建，不能只是回一句"重复跳过"。只用于显式请求；
    自动入队永远走默认（幂等）。

    幂等键是唯一键三列 ``(kind, target_id, input_hash)``：同一份输入重复入队**不产生
    第二行**。冲突时按旧行的 ``status`` 分两种处置：

    * ``failed`` / ``stale`` / ``cancelled`` = **死行，可复活**：同一份输入再入队一次就
      把这行拉回 ``pending``，``attempts`` 归零、``next_attempt_at`` 设成立即可取、
      清掉租约与上次的结果与错误码（新作业不该继承旧结果）；这是"供应商短暂故障烧光
      重试次数""目标被误禁用过""迟到结果被丢弃"的唯一出路。
    * ``pending`` / ``leased`` / ``ready`` / ``done`` = **一行不动**（幂等语义）：重复入队
      不会把正在跑的作业打断、不会把已经算好等落配置的结果清掉、也不会把做过的事再做一遍。
      定义**改了**就是另一个指纹、另起一行——新行做完之前旧行的结果落不下去（见
      :func:`complete_config_index` 与 :func:`has_newer_config_index`）。

    认不出（返回 False，一行不动、不抛）的情形：``kind`` / ``target_id`` / ``input_hash``
    去空白后为空（没有身份就说不清这行属于谁、也算不出指纹）。

    ``scope_id`` / ``source_revision`` 只跟着行存，**不参与幂等键**；``now`` 由调用方传
    （库里不自己取时钟，和邻近写函数一致）：新行的三个时间列都是它，也就是"入队即可取"。
    """

    handler = _config_index_text(kind)
    target = _config_index_text(target_id)
    digest = _config_index_text(input_hash)
    if not handler or not target or not digest:
        return False
    stamp = _float_or(now, 0.0)
    cursor = conn.execute(
        "INSERT INTO config_index_queue (kind, target_id, scope_id, input_hash,"
        " source_revision, status, attempts, next_attempt_at, lease_owner, lease_token,"
        " lease_expires_at, result_json, error_code, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, ?, '', '', 0, '', '', ?, ?)"
        " ON CONFLICT (kind, target_id, input_hash) DO UPDATE SET"
        " status = excluded.status,"
        " attempts = 0,"
        " next_attempt_at = excluded.next_attempt_at,"
        " scope_id = excluded.scope_id,"
        " source_revision = excluded.source_revision,"
        " lease_owner = '',"
        " lease_token = '',"
        " lease_expires_at = 0,"
        " result_json = '',"
        " error_code = '',"
        " updated_at = excluded.updated_at"
        " WHERE config_index_queue.status IN ('failed', 'stale', 'cancelled')"
        + (" OR ? = 1" if force else ""),
        (
            handler,
            target,
            str(scope_id or ""),
            digest,
            str(source_revision or ""),
            CONFIG_INDEX_QUEUE_DEFAULT_STATUS,
            stamp,
            stamp,
            stamp,
            *((1,) if force else ()),
        ),
    )
    conn.commit()
    return bool(cursor.rowcount)


def claim_config_index_due(
    conn: sqlite3.Connection,
    *,
    kind: str,
    owner: str,
    token: str,
    now: float = 0.0,
    limit: int = 8,
    lease_seconds: float = CONFIG_INDEX_QUEUE_DEFAULT_LEASE_SECONDS,
) -> list[dict[str, Any]]:
    """按类型租一批 due 行（``status='pending'`` 且 ``next_attempt_at <= now``）。

    三步，顺序有意义：

    1. **回收过期租约**（同类型）：``status='leased'`` 且 ``lease_expires_at`` 已经过去的行
       拉回 ``pending``。这是"worker 中途死掉 / 进程重启"的自愈路径；``attempts`` **不再加**
       ——那是"没干成"的计数，进程死掉不该算供应商的账。
    2. **原子认领**：一条 UPDATE 把选中的行标成 ``leased``、写上 ``lease_owner`` /
       ``lease_token`` / ``lease_expires_at``，并 ``attempts + 1``。用一条语句做，是因为
       "先查再改"在两个 worker 之间会双领。
    3. **按令牌取回**：``SELECT ... WHERE lease_token = ?``——只有自己这条令牌的行才是
       这次领到的，别的 worker 领走的行不会被误读。

    返回 ``[]``（不抛）的情形：``kind`` / ``owner`` / ``token`` 有一个是空的
    （没有身份就没法安全地租，宁可什么都不做）。``limit <= 0`` 按 8 算（上限 1000）；
    ``lease_seconds <= 0`` 按 :data:`CONFIG_INDEX_QUEUE_DEFAULT_LEASE_SECONDS` 算。
    """

    handler = _config_index_text(kind)
    who = _config_index_text(owner)
    mark = _config_index_text(token)
    if not handler or not who or not mark:
        return []
    stamp = _float_or(now, 0.0)
    wanted = _int_or(limit, 0)
    if wanted <= 0:
        wanted = 8
    wanted = min(wanted, 1000)
    lease = _float_or(lease_seconds, 0.0)
    if lease <= 0.0:
        lease = CONFIG_INDEX_QUEUE_DEFAULT_LEASE_SECONDS
    # 1) 过期租约回收
    conn.execute(
        "UPDATE config_index_queue SET status = ?, lease_owner = '', lease_token = '',"
        " lease_expires_at = 0, updated_at = ?"
        " WHERE kind = ? AND status = 'leased' AND lease_expires_at > 0"
        " AND lease_expires_at <= ?",
        (CONFIG_INDEX_QUEUE_DEFAULT_STATUS, stamp, handler, stamp),
    )
    # 2) 原子认领
    conn.execute(
        "UPDATE config_index_queue SET status = 'leased', lease_owner = ?, lease_token = ?,"
        " lease_expires_at = ?, attempts = attempts + 1, updated_at = ?"
        " WHERE id IN (SELECT id FROM config_index_queue"
        "   WHERE kind = ? AND status = ? AND next_attempt_at <= ?"
        "   ORDER BY id LIMIT ?)",
        (
            who,
            mark,
            stamp + lease,
            stamp,
            handler,
            CONFIG_INDEX_QUEUE_DEFAULT_STATUS,
            stamp,
            wanted,
        ),
    )
    rows = conn.execute(
        "SELECT * FROM config_index_queue WHERE kind = ? AND status = 'leased'"
        " AND lease_token = ? ORDER BY id",
        (handler, mark),
    ).fetchall()
    conn.commit()
    return [item for item in (_config_index_row(row) for row in rows) if item is not None]


def mark_config_index(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    status: str,
    now: float = 0.0,
    attempts: int | None = None,
    next_attempt_at: float | None = None,
    error_code: str | None = None,
) -> bool:
    """改一行作业的状态（**不带租约校验**的通用写入口）。

    ``done`` = 结果已经落进配置；``ready`` = 结果算好了等上层落；``pending`` = 还有重试机会；
    ``failed`` = 重试用尽；``stale`` = 迟到结果被丢弃；``cancelled`` = 目标删除 / 禁用。

    * ``attempts`` / ``next_attempt_at`` / ``error_code`` 给 ``None`` 就**保持原值**
      （标 ``done`` 时不该把重试计数或上次的错误擦掉，那是排查线索）；
    * 状态白名单外、``row_id`` 认不出、行不存在都是 ``False``（不抛异常，和邻近写函数一个规矩）；
    * 走这条路径**不清租约**——要交还租约请用 :func:`release_config_index_lease`，
      要按令牌写回请用 :func:`complete_config_index` / :func:`fail_config_index`。

    这是给"人工重试 / 恢复 / 运维脚本 / 测试"用的；正常 worker 路径请走带令牌的那两个。
    """

    wanted = _config_index_text(status)
    if wanted not in CONFIG_INDEX_QUEUE_STATUSES:
        return False
    ident = _int_or(row_id, 0)
    if ident <= 0:
        return False
    sql = "UPDATE config_index_queue SET status = ?, updated_at = ?"
    args: list[Any] = [wanted, _float_or(now, 0.0)]
    if attempts is not None:
        sql += ", attempts = ?"
        args.append(max(_int_or(attempts, 0), 0))
    if next_attempt_at is not None:
        sql += ", next_attempt_at = ?"
        args.append(_float_or(next_attempt_at, 0.0))
    if error_code is not None:
        sql += ", error_code = ?"
        args.append(str(error_code or ""))
    sql += " WHERE id = ?"
    args.append(ident)
    cursor = conn.execute(sql, tuple(args))
    conn.commit()
    return bool(cursor.rowcount)


def release_config_index_lease(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    lease_token: str,
    now: float = 0.0,
    next_attempt_at: float | None = None,
    attempts: int | None = None,
    error_code: str | None = None,
) -> bool:
    """把一行**自己租着**的作业交还成 ``pending``（带令牌校验，交不了回 ``False``）。

    什么时候用：这个 kind 现在没有可用处理器、或者调用方主动放弃这一轮（预算用完）。
    跟 :func:`fail_config_index` 的区别是"这不算失败"：默认**不动** ``attempts``
    （需要抵掉认领时那次 ``+1`` 就显式传 ``attempts``），``next_attempt_at`` 不给就保持原值。

    ``lease_token`` 对不上（行已经被回收给别人）时什么都不做：交还一个不属于自己的租约
    等于把别人的活抢回来重做。
    """

    ident = _int_or(row_id, 0)
    mark = _config_index_text(lease_token)
    if ident <= 0 or not mark:
        return False
    sql = (
        "UPDATE config_index_queue SET status = ?, lease_owner = '', lease_token = '',"
        " lease_expires_at = 0, updated_at = ?"
    )
    args: list[Any] = [CONFIG_INDEX_QUEUE_DEFAULT_STATUS, _float_or(now, 0.0)]
    if next_attempt_at is not None:
        sql += ", next_attempt_at = ?"
        args.append(_float_or(next_attempt_at, 0.0))
    if attempts is not None:
        sql += ", attempts = ?"
        args.append(max(_int_or(attempts, 0), 0))
    if error_code is not None:
        sql += ", error_code = ?"
        args.append(str(error_code or ""))
    sql += " WHERE id = ? AND status = 'leased' AND lease_token = ?"
    args.extend([ident, mark])
    cursor = conn.execute(sql, tuple(args))
    conn.commit()
    return bool(cursor.rowcount)


def complete_config_index(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    lease_token: str,
    result_json: str = "",
    now: float = 0.0,
) -> str:
    """把算好的结果写进 ``result_json`` 并标 ``ready``（**带令牌 + 过期结果丢弃**）。

    返回原因码（不抛）：

    * :data:`CONFIG_INDEX_COMPLETE_READY`：真的写进去了（行是 ``leased``、令牌对得上、
      而且**没有更新的活跃行**）；
    * :data:`CONFIG_INDEX_COMPLETE_SUPERSEDED`：同一个 ``(kind, target_id)`` 已经有一行
      ``id`` 更大、状态还在 ``pending`` / ``leased`` / ``ready`` 的作业——说明定义又改过
      （或者另一次整理已经算好了更新的结果）。**这份迟到结果被丢弃**，调用方应把它标
      ``stale``（本函数不动状态，让调用方决定怎么记账）；
    * :data:`CONFIG_INDEX_COMPLETE_LEASE_LOST`：行不是 ``leased``、或者令牌对不上
      （租约被回收给了别人），同样什么都不写。

    ``result_json`` 由调用方保证"只含允许回写的派生字段"（A4 的处理器在
    ``core/config_index_queue.py`` 里过滤完再传进来）；库层只当它是文本，不解析、不校验
    ——库层不认识配置模型，硬校验只会把口径复制到两处。
    """

    ident = _int_or(row_id, 0)
    mark = _config_index_text(lease_token)
    if ident <= 0 or not mark:
        return CONFIG_INDEX_COMPLETE_LEASE_LOST
    stamp = _float_or(now, 0.0)
    cursor = conn.execute(
        "UPDATE config_index_queue SET status = 'ready', result_json = ?, error_code = '',"
        " lease_owner = '', lease_token = '', lease_expires_at = 0, updated_at = ?"
        " WHERE id = ? AND status = 'leased' AND lease_token = ?"
        " AND NOT EXISTS (SELECT 1 FROM config_index_queue AS newer"
        "   WHERE newer.kind = config_index_queue.kind"
        "     AND newer.target_id = config_index_queue.target_id"
        "     AND newer.id > config_index_queue.id"
        "     AND newer.status IN ('pending', 'leased', 'ready'))",
        (str(result_json or ""), stamp, ident, mark),
    )
    conn.commit()
    if cursor.rowcount:
        return CONFIG_INDEX_COMPLETE_READY
    row = conn.execute(
        "SELECT status, lease_token FROM config_index_queue WHERE id = ?", (ident,)
    ).fetchone()
    if row is not None and str(row["status"]) == "leased" and str(row["lease_token"]) == mark:
        return CONFIG_INDEX_COMPLETE_SUPERSEDED
    return CONFIG_INDEX_COMPLETE_LEASE_LOST


def fail_config_index(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    lease_token: str,
    error_code: str = "provider_error",
    attempts: int | None = None,
    next_attempt_at: float | None = None,
    terminal: bool = False,
    now: float = 0.0,
) -> bool:
    """记一次失败：``terminal=True`` 标 ``failed``，否则退回 ``pending`` 等退避后重试。

    带令牌校验（令牌对不上回 ``False``，一个字不写）。``attempts`` / ``next_attempt_at``
    不给就保持原值——认领时已经 ``+1`` 过一次，退避时间由调用方按 ``attempts`` 算好传进来
    （退避公式在 ``core/config_index_queue.py``，库层不掺和策略）。
    """

    ident = _int_or(row_id, 0)
    mark = _config_index_text(lease_token)
    if ident <= 0 or not mark:
        return False
    sql = (
        "UPDATE config_index_queue SET status = ?, error_code = ?, lease_owner = '',"
        " lease_token = '', lease_expires_at = 0, updated_at = ?"
    )
    args: list[Any] = [
        "failed" if terminal else CONFIG_INDEX_QUEUE_DEFAULT_STATUS,
        str(error_code or ""),
        _float_or(now, 0.0),
    ]
    if attempts is not None:
        sql += ", attempts = ?"
        args.append(max(_int_or(attempts, 0), 0))
    if next_attempt_at is not None:
        sql += ", next_attempt_at = ?"
        args.append(_float_or(next_attempt_at, 0.0))
    sql += " WHERE id = ? AND status = 'leased' AND lease_token = ?"
    args.extend([ident, mark])
    cursor = conn.execute(sql, tuple(args))
    conn.commit()
    return bool(cursor.rowcount)


def has_newer_config_index(conn: sqlite3.Connection, row_id: int) -> bool:
    """这一行后面还有没有更新的活跃作业（``pending`` / ``leased`` / ``ready``）。

    True = 这一行是**过期作业**：它的结果不该落进配置（定义改过，或者更新的结果已经在等）。
    ``row_id`` 认不出 / 行不存在一律回 ``False``（"没有更新的"是安全默认：不冤枉正常作业）。
    """

    ident = _int_or(row_id, 0)
    if ident <= 0:
        return False
    row = conn.execute(
        "SELECT kind, target_id FROM config_index_queue WHERE id = ?", (ident,)
    ).fetchone()
    if row is None:
        return False
    found = conn.execute(
        "SELECT 1 FROM config_index_queue WHERE kind = ? AND target_id = ? AND id > ?"
        " AND status IN ('pending', 'leased', 'ready') LIMIT 1",
        (str(row["kind"]), str(row["target_id"]), ident),
    ).fetchone()
    return found is not None


def apply_config_index_result(
    conn: sqlite3.Connection, row_id: int, *, now: float = 0.0
) -> dict[str, Any] | None:
    """把一行 ``ready`` 的结果**原子地**翻成 ``done`` 并返回这一行（拿不到回 ``None``）。

    上层（A5）拿着返回值里的 ``result_json`` 落进世界配置，然后再调一次这个函数——
    第二次会拿到 ``None``（已经不是 ``ready`` 了），于是"同一份结果落两遍"天然不可能。

    两种"不给"的情形：

    * 行不是 ``ready``（还在排队 / 已经被别人 apply 过 / 已经作废）；
    * 这一行是**过期结果**（后面有更新的活跃作业）→ 顺手把它标成 ``stale``，不返回内容。
    """

    ident = _int_or(row_id, 0)
    if ident <= 0:
        return None
    stamp = _float_or(now, 0.0)
    row = conn.execute("SELECT * FROM config_index_queue WHERE id = ?", (ident,)).fetchone()
    if row is None:
        return None
    item = _config_index_row(row)
    if item is None or item.get("status") != "ready":
        return None
    if has_newer_config_index(conn, ident):
        mark_config_index(conn, ident, status="stale", now=stamp)
        return None
    cursor = conn.execute(
        "UPDATE config_index_queue SET status = 'done', updated_at = ?"
        " WHERE id = ? AND status = 'ready'",
        (stamp, ident),
    )
    conn.commit()
    if not cursor.rowcount:
        return None
    item["status"] = "done"
    item["updated_at"] = stamp
    return item


def invalidate_config_index(
    conn: sqlite3.Connection,
    *,
    kind: str = "",
    target_id: str = "",
    target_ids: Any = (),
    now: float = 0.0,
) -> int:
    """目标**删除 / 禁用**时立即作废它的作业：活跃行标 ``cancelled``、清结果、清租约。

    为什么不是删行：``cancelled`` 是一条**可查的事实**（"这条为什么没有派生信息"），删掉就
    只剩"从来没有过"；而幂等键还在，同指纹重新入队时会走 :func:`enqueue_config_index` 的
    死行复活分支。``result_json`` 一定要清——已经算好、还没落配置的结果如果留着，
    删除动作之后它还会被 :func:`apply_config_index_result` 落下去。

    过滤条件：``kind`` 给了就限类型；``target_id`` / ``target_ids`` 至少给一个（都不给
    **直接返回 0**，防止一句空调用把整张表作废——要清全表请用 :func:`clear_config_index`）。
    返回被作废的行数。
    """

    handler = _config_index_text(kind)
    targets = [item for item in ([target_id] if _config_index_text(target_id) else [])]
    if isinstance(target_ids, str):
        targets.extend(
            item for item in [_config_index_text(target_ids)] if item
        )
    else:
        try:
            targets.extend(
                _config_index_text(item) for item in (target_ids or ()) if _config_index_text(item)
            )
        except TypeError:
            pass
    targets = list(dict.fromkeys(targets))
    if not targets:
        return 0
    placeholders = ", ".join("?" for _ in targets)
    sql = (
        "UPDATE config_index_queue SET status = 'cancelled', result_json = '',"
        " lease_owner = '', lease_token = '', lease_expires_at = 0, error_code = '',"
        " updated_at = ? WHERE target_id IN (" + placeholders + ")"
        " AND status IN ('pending', 'leased', 'ready')"
    )
    args: list[Any] = [_float_or(now, 0.0), *targets]
    if handler:
        sql += " AND kind = ?"
        args.append(handler)
    cursor = conn.execute(sql, tuple(args))
    conn.commit()
    return int(cursor.rowcount or 0)


def recover_config_index_leases(
    conn: sqlite3.Connection, *, kind: str = "", now: float = 0.0, force: bool = False
) -> int:
    """把 ``leased`` 的行收回成 ``pending``（**重启可恢复**），返回回收条数。

    * ``force=False``（默认）：只收 ``lease_expires_at`` 已经过去的行——多 worker 下
      抢别人的活是错的；
    * ``force=True``：收**所有** ``leased`` 行。单进程插件启动时用这一档：那些租约一定是
      上一个已经死掉的进程留下的，等过期只是白白晚一会儿。多 worker 场景**不要**用它。

    回收**不动** ``attempts``（进程死掉不是供应商的账）；``kind`` 给了就只收那一类。
    """

    stamp = _float_or(now, 0.0)
    sql = (
        "UPDATE config_index_queue SET status = ?, lease_owner = '', lease_token = '',"
        " lease_expires_at = 0, next_attempt_at = ?, updated_at = ?"
        " WHERE status = 'leased'"
    )
    args: list[Any] = [CONFIG_INDEX_QUEUE_DEFAULT_STATUS, stamp, stamp]
    handler = _config_index_text(kind)
    if handler:
        sql += " AND kind = ?"
        args.append(handler)
    if not force:
        sql += " AND lease_expires_at > 0 AND lease_expires_at <= ?"
        args.append(stamp)
    cursor = conn.execute(sql, tuple(args))
    conn.commit()
    return int(cursor.rowcount or 0)


def count_config_index(
    conn: sqlite3.Connection, *, kind: str = "", status: str = "pending", scope_id: Any = None
) -> int:
    """数某一类 / 某一状态还有多少行；``status`` 给空串 = 不分状态全数。

    ``scope_id`` 不给（``None``）或空串 = 所有作用域。
    """

    sql = "SELECT COUNT(*) FROM config_index_queue WHERE 1 = 1"
    args: list[Any] = []
    handler = _config_index_text(kind)
    if handler:
        sql += " AND kind = ?"
        args.append(handler)
    wanted = _config_index_text(status)
    if wanted:
        sql += " AND status = ?"
        args.append(wanted)
    scope = _config_index_text(scope_id)
    if scope:
        sql += " AND scope_id = ?"
        args.append(scope)
    return int(conn.execute(sql, tuple(args)).fetchone()[0] or 0)


def list_config_index_rows(
    conn: sqlite3.Connection,
    *,
    kind: str = "",
    target_id: str = "",
    statuses: Any = (),
    scope_id: Any = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按条件列出行（``id`` 升序，最多 ``limit`` 条，``limit <= 0`` 按 100 算、上限 1000）。

    任何一个过滤条件认不出（不是字符串 / 不在白名单）就当**不过滤**；行数据坏不改写、
    原样归一返回。空表回 ``[]``。
    """

    sql = "SELECT * FROM config_index_queue WHERE 1 = 1"
    args: list[Any] = []
    handler = _config_index_text(kind)
    if handler:
        sql += " AND kind = ?"
        args.append(handler)
    target = _config_index_text(target_id)
    if target:
        sql += " AND target_id = ?"
        args.append(target)
    scope = _config_index_text(scope_id)
    if scope:
        sql += " AND scope_id = ?"
        args.append(scope)
    try:
        wanted = [str(item) for item in (statuses or ()) if str(item) in CONFIG_INDEX_QUEUE_STATUSES]
    except TypeError:
        wanted = []
    if wanted:
        sql += " AND status IN (" + ", ".join("?" for _ in wanted) + ")"
        args.extend(wanted)
    wanted_limit = _int_or(limit, 0)
    if wanted_limit <= 0:
        wanted_limit = 100
    wanted_limit = min(wanted_limit, 1000)
    sql += " ORDER BY id LIMIT ?"
    args.append(wanted_limit)
    rows = conn.execute(sql, tuple(args)).fetchall()
    return [item for item in (_config_index_row(row) for row in rows) if item is not None]


def get_config_index_row(conn: sqlite3.Connection, row_id: int) -> dict[str, Any] | None:
    """按 id 取一行（认不出 / 没有回 ``None``）。"""

    ident = _int_or(row_id, 0)
    if ident <= 0:
        return None
    row = conn.execute("SELECT * FROM config_index_queue WHERE id = ?", (ident,)).fetchone()
    return _config_index_row(row)


def clear_config_index(conn: sqlite3.Connection, *, kind: str = "") -> int:
    """**删行**（所有状态），返回删掉的条数；``kind`` 给了就只删那一类。

    这是"清空重来"的运维入口（A4 的 ``clear``），跟 :func:`invalidate_config_index`
    的语义不同：那个是"作废但留痕"，这个是"不要了"。
    """

    handler = _config_index_text(kind)
    if handler:
        cursor = conn.execute("DELETE FROM config_index_queue WHERE kind = ?", (handler,))
    else:
        cursor = conn.execute("DELETE FROM config_index_queue")
    conn.commit()
    return int(cursor.rowcount or 0)



# ---------------- 记忆保留策略（P2 追加） ----------------
#
# 三件事，全部幂等、可重复执行、不写任何数据：
#
# 1. ``node_memory.occurrence``：同一条重复记忆被"再次想起来"的次数。动作记忆
#    （``在这里{地点}`` 那种）落在同一个 session+node+label 上时不再新写一行，而是
#    刷新 ``created_at`` 并把这一列 +1，于是"她在这儿干过几次"仍然读得出来，
#    而记忆表不会被 tick 灌满。
# 2. ``idx_session_created``：召回候选窗与跨节点去重都是"同会话按时间倒序取最近
#    N 条"，没有这条索引就是全表扫 + 临时 B 树排序（见 core/memory.py 的
#    ``_query`` / ``_merge_duplicate``）。
# 3. ``idx_session_weight``：容量闸（``_enforce_limit``）要按权重挑淘汰对象。
#
# 索引**不能**写进 ``SCHEMA``：老库上 ``CREATE TABLE IF NOT EXISTS`` 是空操作，
# 紧跟的 ``CREATE INDEX`` 会撞 "no such column" 把插件挡在启动外（同 169-173 行的
# 老坑）。所以照 ``_migrate_event_log_tool_call_id`` 的写法：先 ``PRAGMA table_info``
# 判列，缺了才 ``ALTER TABLE``，索引最后建。


_MEMORY_RETENTION_COLUMNS = (
    ("occurrence", "ALTER TABLE node_memory ADD COLUMN occurrence INTEGER NOT NULL DEFAULT 0"),
)

_MEMORY_RETENTION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_session_created ON node_memory (session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_session_weight ON node_memory (session_id, weight)",
)


def _migrate_memory_retention(conn: sqlite3.Connection) -> None:
    """给 ``node_memory`` 补 ``occurrence`` 列与两条保留策略索引（可重复调用）。

    空库（表还没建）、老库（没有这一列）、已经迁移过的库都能跑；既有数据一行不动，
    权重/时间都不改写。
    """

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(node_memory)")}
    if not columns:
        return
    for name, ddl in _MEMORY_RETENTION_COLUMNS:
        if name not in columns:
            conn.execute(ddl)
    for ddl in _MEMORY_RETENTION_INDEXES:
        conn.execute(ddl)
    conn.commit()


# ---------------- 同主题覆盖（supersede）：旧说法退位，不物理删除 ----------------
#
# 事故（用户实机）：她对主人说「我明天要去体检」，可昨天就说过、今天下午已经做完；
# 库里「明天要去体检」和「体检完了」两条并存，她混着说。根因不是日期格式，是
# **旧事实没有作废**——新说法落库了，旧那条还写着"明天要去"，两条一起被召回。
#
# 判定规则（同一件事 + 有冲突）在 ``core/memory_supersede.py``（纯函数、可单测），
# 这里只管三件事：补列、按状态过滤、把一条记忆 / 画像事实标成 superseded。
#
# 三列**不能**写进 ``SCHEMA``：老库上 ``CREATE TABLE IF NOT EXISTS`` 是空操作，紧跟
# 建 ``(status)`` 索引会撞 "no such column" 把插件挡在启动外（同 12575 行那个坑）。
# 所以照 ``_migrate_memory_retention`` 的写法：``PRAGMA table_info`` 判列，缺了才
# ``ALTER TABLE``，索引最后建；**fail-soft**——任何一步失败只 WARN，插件照常起来。

_MEMORY_SUPERSEDE_COLUMNS = (
    ("status", "ALTER TABLE node_memory ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
    (
        "superseded_by",
        "ALTER TABLE node_memory ADD COLUMN superseded_by TEXT NOT NULL DEFAULT ''",
    ),
    (
        "superseded_at",
        "ALTER TABLE node_memory ADD COLUMN superseded_at REAL NOT NULL DEFAULT 0",
    ),
)

_MEMORY_SUPERSEDE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_memory_status ON node_memory (status)",
)

_FACT_SUPERSEDE_COLUMNS = (
    ("status", "ALTER TABLE user_fact ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
    (
        "superseded_by",
        "ALTER TABLE user_fact ADD COLUMN superseded_by TEXT NOT NULL DEFAULT ''",
    ),
    (
        "superseded_at",
        "ALTER TABLE user_fact ADD COLUMN superseded_at REAL NOT NULL DEFAULT 0",
    ),
)

_FACT_SUPERSEDE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_fact_status ON user_fact (status)",
)


def _as_int(value: object) -> int:
    """``int(value)`` 但绝不抛：""/None/"abc" 都算 0（模型编出来的 id 会走到这里）。"""

    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """表里有没有这一列（读不到表/列都算没有；fail-soft，不抛）。"""

    try:
        return any(
            str(row["name"]) == column for row in conn.execute(f"PRAGMA table_info({table})")
        )
    except sqlite3.Error:
        return False


def _migrate_memory_supersede(conn: sqlite3.Connection) -> None:
    """给 ``node_memory`` / ``user_fact`` 补 supersede 三列与 ``(status)`` 索引。

    幂等 + fail-soft：``PRAGMA table_info`` 判列，缺才 ``ALTER TABLE``；空库（表还
    没建）直接跳过；任何一步失败只 ``logger.warning``，**绝不把插件挡在启动外**。
    既有数据一行不动——新列的默认值让老行一律 ``status='active'``，行为与升级前一致。
    """

    for table, columns, indexes in (
        ("node_memory", _MEMORY_SUPERSEDE_COLUMNS, _MEMORY_SUPERSEDE_INDEXES),
        ("user_fact", _FACT_SUPERSEDE_COLUMNS, _FACT_SUPERSEDE_INDEXES),
    ):
        try:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error as exc:  # 迁移只该"尽力而为"，不该挡启动
            logger.warning("记忆作废：读不到 %s 的列，跳过（%s）", table, exc)
            continue
        if not existing:
            continue
        for name, ddl in columns:
            if name in existing:
                continue
            try:
                conn.execute(ddl)
            except sqlite3.Error as exc:
                logger.warning("记忆作废：给 %s 补 %s 失败，跳过（%s）", table, name, exc)
        for ddl in indexes:
            try:
                conn.execute(ddl)
            except sqlite3.Error as exc:
                logger.warning("记忆作废：给 %s 建索引失败，跳过（%s）", table, exc)
    conn.commit()


# ---------------- 空声明锁死修复（T8 第一步追加） ----------------
#
# 事故（2026-10 实测）：``set_source_event_visibility`` 在"库里还没有这条事件"时
# 走 INSERT 分支，写的是 ``merge_visibility(existing={}, visibility)`` 的结果。
# 当时的 ``merge_visibility`` 把空 ``{}`` 也当成一次声明：
#
#   normalize_policy({})            → legacy_unknown / private / scope '' / origin ''
#   merge_policies(那一份)          → 把它再 normalize 一遍，于是它**有**
#                                     policy_version、**没有** scope/origin，
#                                     被判成"损坏"，升级成 deny_all（一份子条款）
#   normalize_visibility(composite) → 顶层 scope/origin 因为两份条款不一致变 '',
#                                     allowed_sessions 变 restricted []
#
# 合并是"只写更严"，所以这份 deny_all 条款从此赖在 ``source_policies`` 里，
# ``authorize`` 每次都返回 "damaged or conflicting policy"——这条来源永久锁死，
# 它支撑的谈资再也讲不出去（2026-10-23 只读副本实测：56 条 event 谈资里 45 条
# 全中，占全部 92→93 条谈资的一半；迁移后这 45 条全部恢复）。
#
# 写侧已在 ``merge_visibility`` / ``_set_source_event_visibility`` 修掉：空声明
# 不进合并、不落库。这个迁移只修**历史数据**，判据只有一条：
# **能证明那份 deny_all 条款是空声明变的**，否则一行不动。
#
# 证据链（全部满足才重算）：
# 1. 行里是 composite（有 source_policies），且至少一条子条款逐字段等于"空声明
#    条款"（:func:`_empty_declaration_clause` 的产物，policy_revision 除外）；
# 2. 顶层 scope_id / origin_session_id 都是空——这正是"两份条款 scope 不一致"
#    的合并产物，真实声明不会写出这种顶层身份；
# 3. 行自身的 scope_id / origin_session_id 也是空——INSERT 分支的行身份是从
#    被污染的 merged 里取的（''），说明它当初是**只带可见性**的门牌行；
# 4. 其余子条款都是自洽的可信声明：trusted_rule、非 deny_all、不带嵌套 composite、
#    scope 与 origin 都非空，且彼此一致；
# 5. 重算结果再验一遍：非 deny_all、身份不漂、`normalize`（normal 档）下
#    ``_check`` 真的放行。
# 任一条不满足就放弃该行，并按原因计数（写进 schema_meta 的标记里）。
#
# 一次性：``schema_meta`` 里记标记，做过就跳过；执行函数本身也幂等（修过的行
# 不再满足第 1、2 条，重跑是 0 行）。

_SOURCE_VISIBILITY_REPAIR_MARKER = "source_visibility_empty_declaration_repair"
_SOURCE_VISIBILITY_REPAIR_VERSION = 1


def _empty_declaration_clause() -> dict[str, Any]:
    """``{}`` 被"当声明"之后，最终留在 composite 里的那条子条款（deny_all 那份）。"""

    from .topic_policy import normalize_policy

    return normalize_policy(normalize_policy({}))


def _is_empty_declaration_clause(clause: Any, fingerprint: Mapping[str, Any]) -> bool:
    """这一条子条款是不是"空声明"变的（逐字段相等；policy_revision 只是记账）。"""

    from .topic_policy import normalize_policy

    try:
        normalized = normalize_policy(clause)
    except (TypeError, ValueError):
        return False
    left = {key: value for key, value in normalized.items() if key != "policy_revision"}
    right = {key: value for key, value in fingerprint.items() if key != "policy_revision"}
    return left == right


def plan_source_visibility_repair(
    conn: sqlite3.Connection,
) -> tuple[list[tuple[str, dict[str, Any], str, str]], list[tuple[str, str]]]:
    """只读规划：哪些行能证明"deny_all 来自空声明"、哪些放弃及原因。

    返回 ``(repairs, skipped)``：``repairs`` 里每项是
    ``(event_id, 重算后的 visibility, scope_id, origin_session_id)``；``skipped``
    里每项是 ``(event_id, 原因)``。**不改任何数据**（测量脚本也用它）。
    """

    from .topic_policy import merge_policies, normalize_policy

    fingerprint = _empty_declaration_clause()
    repairs: list[tuple[str, dict[str, Any], str, str]] = []
    skipped: list[tuple[str, str]] = []
    rows = conn.execute(
        "SELECT event_id, scope_id, origin_session_id, visibility_json FROM source_events"
        " ORDER BY event_id"
    ).fetchall()
    for row in rows:
        event_id = str(row[0])
        row_scope = str(row[1] or "")
        row_origin = str(row[2] or "")
        data = _json_dict(row[3])
        clauses = data.get("source_policies")
        if not isinstance(clauses, (list, tuple)) or not clauses:
            continue  # 普通行：没有 composite，谈不上这个 bug
        if not all(isinstance(clause, Mapping) for clause in clauses):
            skipped.append((event_id, "unreadable composite"))
            continue
        artifacts = [c for c in clauses if _is_empty_declaration_clause(c, fingerprint)]
        if not artifacts:
            continue  # 没有"空声明条款"：不是这个 bug 的受害者
        if str(data.get("scope_id") or "") or str(data.get("origin_session_id") or ""):
            skipped.append((event_id, "aggregate keeps its own identity"))
            continue
        if row_scope or row_origin:
            skipped.append((event_id, "row keeps its own identity"))
            continue
        trusted: list[dict[str, Any]] = []
        reason = ""
        for clause in clauses:
            if _is_empty_declaration_clause(clause, fingerprint):
                continue
            policy = normalize_policy(clause)
            if policy.get("deny_all") is True:
                reason = "another deny_all clause"
                break
            if policy.get("source_policies"):
                reason = "nested composite clause"
                break
            if policy.get("authority") != "trusted_rule":
                reason = "untrusted clause"
                break
            if not policy.get("scope_id") or not policy.get("origin_session_id"):
                reason = "clause without exact identity"
                break
            trusted.append(policy)
        if reason:
            skipped.append((event_id, reason))
            continue
        if not trusted:
            skipped.append((event_id, "nothing left after the empty declaration"))
            continue
        scopes = {policy["scope_id"] for policy in trusted}
        origins = {policy["origin_session_id"] for policy in trusted}
        if len(scopes) != 1 or len(origins) != 1:
            skipped.append((event_id, "ambiguous clause identity"))
            continue
        rebuilt = (
            normalize_visibility(trusted[0])
            if len(trusted) == 1
            else normalize_visibility(merge_policies(*trusted))
        )
        if rebuilt.get("deny_all") is True:
            skipped.append((event_id, "rebuild still deny_all"))
            continue
        if rebuilt.get("scope_id") != trusted[0]["scope_id"] or (
            rebuilt.get("origin_session_id") != trusted[0]["origin_session_id"]
        ):
            skipped.append((event_id, "rebuild identity drifted"))
            continue
        survivors = rebuilt.get("source_policies") or []
        if any(_is_empty_declaration_clause(c, fingerprint) for c in survivors):
            skipped.append((event_id, "empty declaration survived rebuild"))
            continue
        if rebuilt.get("sensitivity") == "normal":
            from .topic_policy import PolicyTarget, _check

            if not _check(
                rebuilt,
                PolicyTarget(
                    scope_id=rebuilt["scope_id"],
                    session_id=rebuilt["origin_session_id"],
                ),
                "introduce",
                False,
            ):
                skipped.append((event_id, "rebuild still denies"))
                continue
        repairs.append(
            (event_id, rebuilt, rebuilt["scope_id"], rebuilt["origin_session_id"])
        )
    return repairs, skipped


def repair_source_visibility_empty_declarations(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """执行修复：只改 :func:`plan_source_visibility_repair` 证明过的行。

    返回统计 ``{"repaired", "skipped", "reasons", "scope_backfilled"}``。修过的行：
    ``visibility_json`` 换成重算的声明、空的行身份补成真实 scope/origin、
    ``policy_revision`` +1（这是真的改了一次）。其余行一个字节都不动。
    """

    repairs, skipped = plan_source_visibility_repair(conn)
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(source_events)").fetchall()
    }
    bump = "policy_revision = policy_revision + 1, " if "policy_revision" in columns else ""
    backfilled = 0
    with _storage_transaction(conn):
        for event_id, policy, scope, origin in repairs:
            payload = json.dumps(policy, ensure_ascii=False, sort_keys=True)
            cursor = conn.execute(
                "UPDATE source_events SET "
                + bump
                + " visibility_json = ?,"
                " scope_id = CASE WHEN scope_id = '' THEN ? ELSE scope_id END,"
                " origin_session_id = CASE WHEN origin_session_id = '' THEN ?"
                " ELSE origin_session_id END WHERE event_id = ?",
                (payload, scope, origin, event_id),
            )
            backfilled += int(cursor.rowcount or 0)
    reasons: dict[str, int] = {}
    for _event_id, reason in skipped:
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "repaired": len(repairs),
        "skipped": len(skipped),
        "reasons": reasons,
        "scope_backfilled": backfilled,
    }


def _migrate_source_visibility_empty_declaration_repair(
    conn: sqlite3.Connection,
) -> None:
    """一次性数据修复：把被"空声明"锁成 deny_all 的来源重算（幂等，做过即跳过）。"""

    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source_events'"
    ).fetchone():
        return
    ensure_schema_meta(conn)
    if conn.execute(
        "SELECT 1 FROM schema_meta WHERE key = ?", (_SOURCE_VISIBILITY_REPAIR_MARKER,)
    ).fetchone():
        return  # 一次性：标记在就不重跑
    try:
        stats = repair_source_visibility_empty_declarations(conn)
        value = json.dumps(
            {
                "version": _SOURCE_VISIBILITY_REPAIR_VERSION,
                "at": time.time(),
                **stats,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_SOURCE_VISIBILITY_REPAIR_MARKER, value),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# 知识卡（用户：「查了某个梗什么意思，查完就过去了，聊天记录刷掉她就不记得」）
#
# 一族「世界知识」：**不绑 user_id**、也不写进 ``node_memory``——它记的是"这件
# 事是什么"，不是"我跟他之间发生过什么"。内容完全由引擎从真实工具结果里截出来
# （见 ``core/knowledge_card.py``），这一层只负责存／查／计数／过期：
#
# ① 一个 ``subject`` 只有一条 active 卡（部分唯一索引；过期后可以再建一条）；
# ② 召回按关键词命中数排序，过期的一律不返回（``expires_at`` 为 0 = 永不过期）；
# ③ 过期只把 ``status`` 改成 expired，**不删行**——留着才能事后看出来"她曾经知道过"。
# ---------------------------------------------------------------------------

_KNOWLEDGE_CARD_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_card (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    subject_key TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    source_url TEXT NOT NULL DEFAULT '',
    source_title TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0,
    use_count INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_subject
    ON knowledge_card (session_id, subject_key) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_knowledge_used
    ON knowledge_card (session_id, status, last_used_at);
"""

_KNOWLEDGE_CARD_COLUMNS: dict[str, str] = {
    "session_id": "TEXT NOT NULL DEFAULT ''",
    "subject": "TEXT NOT NULL DEFAULT ''",
    "subject_key": "TEXT NOT NULL DEFAULT ''",
    "summary": "TEXT NOT NULL DEFAULT ''",
    "keywords": "TEXT NOT NULL DEFAULT '[]'",
    "source_url": "TEXT NOT NULL DEFAULT ''",
    "source_title": "TEXT NOT NULL DEFAULT ''",
    "excerpt": "TEXT NOT NULL DEFAULT ''",
    "confidence": "REAL NOT NULL DEFAULT 0.5",
    "created_at": "REAL NOT NULL DEFAULT 0",
    "updated_at": "REAL NOT NULL DEFAULT 0",
    "last_used_at": "REAL NOT NULL DEFAULT 0",
    "use_count": "INTEGER NOT NULL DEFAULT 0",
    "expires_at": "REAL NOT NULL DEFAULT 0",
    "status": "TEXT NOT NULL DEFAULT 'active'",
}

#: 同一张卡再被查到时置信度加多少（上限 1.0）
KNOWLEDGE_CONFIDENCE_STEP = 0.15
#: 新卡的初始置信度
KNOWLEDGE_CONFIDENCE_BASE = 0.5


def _migrate_knowledge_cards(conn: sqlite3.Connection) -> None:
    """建知识卡表与索引（可重复调用，只建表不写数据）。

    老库（没有这张表）启动时自动建；已经建过的库再跑一次什么都不变。半新不旧的表
    （有人手工建过缺列）按 ``PRAGMA table_info`` 补齐缺的列，既有行一个字段不动。
    """

    _run_ddl(conn, _KNOWLEDGE_CARD_DDL)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(knowledge_card)")}
    for name, ddl in _KNOWLEDGE_CARD_COLUMNS.items():
        if columns and name not in columns:
            conn.execute(f"ALTER TABLE knowledge_card ADD COLUMN {name} {ddl}")
    conn.commit()


def _knowledge_card_row(row: Any) -> dict[str, Any] | None:
    """一行 → dict（``keywords`` 解成列表；数值字段统一成 float / int）。"""

    if row is None:
        return None
    item = dict(row)
    item["keywords"] = [str(word) for word in _json_list(item.get("keywords"))]
    item["id"] = int(item.get("id") or 0)
    item["confidence"] = float(item.get("confidence") or 0.0)
    item["created_at"] = float(item.get("created_at") or 0.0)
    item["updated_at"] = float(item.get("updated_at") or 0.0)
    item["last_used_at"] = float(item.get("last_used_at") or 0.0)
    item["expires_at"] = float(item.get("expires_at") or 0.0)
    item["use_count"] = int(item.get("use_count") or 0)
    return item


def _knowledge_subject_key(subject: Any) -> str:
    """``subject`` 的归一键；延迟 import，免得 ``db`` 把搜索那一串拖进来。"""

    from .knowledge_card import subject_key  # noqa: PLC0415

    return subject_key(subject)


def upsert_knowledge_card(
    conn: sqlite3.Connection,
    *,
    session_id: Any,
    subject: Any,
    summary: Any,
    keywords: Any = (),
    source_url: Any = "",
    source_title: Any = "",
    excerpt: Any = "",
    confidence: Any = 0.0,
    expires_at: Any = 0.0,
    now: Any = 0.0,
    subject_key: Any = "",
) -> dict[str, Any] | None:
    """写一张知识卡：同一个 ``subject_key`` 已有 active 行就更新，否则新增。

    返回 ``{"action": "added"|"updated", "card_id": int, "confidence": float}``；
    参数不全（没有会话 / 没有 subject / 没有摘要）返回 ``None``，什么都不写。
    """

    session = str(session_id or "").strip()
    label = str(subject or "").strip()
    text = str(summary or "").strip()
    if not session or not label or not text:
        return None
    key = str(subject_key or "").strip() or _knowledge_subject_key(label)
    if not key:
        return None
    stamp = float(now or 0.0) or time.time()
    base = float(confidence or 0.0) or KNOWLEDGE_CONFIDENCE_BASE
    words = [str(word).strip() for word in (keywords or []) if str(word).strip()]
    payload = json.dumps(words, ensure_ascii=False)
    expiry = float(expires_at or 0.0)
    row = conn.execute(
        "SELECT id, confidence, expires_at FROM knowledge_card"
        " WHERE session_id = ? AND subject_key = ? AND status = 'active'",
        (session, key),
    ).fetchone()
    if row is not None:
        card_id = int(row["id"])
        raised = min(1.0, float(row["confidence"] or base) + KNOWLEDGE_CONFIDENCE_STEP)
        # 再查到时顺手续一次期：刚被用过的知识不该在同一天过期
        keep_expiry = expiry if expiry > 0 else float(row["expires_at"] or 0.0)
        conn.execute(
            "UPDATE knowledge_card SET summary = ?, excerpt = ?, keywords = ?,"
            " source_url = ?, source_title = ?, confidence = ?, updated_at = ?,"
            " expires_at = ? WHERE id = ?",
            (
                text,
                str(excerpt or ""),
                payload,
                str(source_url or ""),
                str(source_title or ""),
                raised,
                stamp,
                keep_expiry,
                card_id,
            ),
        )
        conn.commit()
        return {"action": "updated", "card_id": card_id, "confidence": raised}
    cursor = conn.execute(
        "INSERT INTO knowledge_card (session_id, subject, subject_key, summary,"
        " keywords, source_url, source_title, excerpt, confidence, created_at,"
        " updated_at, last_used_at, use_count, expires_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 'active')",
        (
            session,
            label,
            key,
            text,
            payload,
            str(source_url or ""),
            str(source_title or ""),
            str(excerpt or ""),
            min(1.0, base),
            stamp,
            stamp,
            expiry,
        ),
    )
    conn.commit()
    return {
        "action": "added",
        "card_id": int(cursor.lastrowid or 0),
        "confidence": min(1.0, base),
    }


def find_knowledge_cards(
    conn: sqlite3.Connection,
    *,
    session_id: Any,
    keywords: Any = (),
    limit: int = 3,
    min_overlap: int = 2,
    now: Any = 0.0,
) -> list[dict[str, Any]]:
    """按关键词召回这个会话里还活着的知识卡（命中多的、最近用过的排前面）。

    ``min_overlap`` 是"至少同时命中几个关键词"：一个也算命中太松（"什么""意思"
    这种词几乎每句都有），默认要两个。过期（``expires_at`` 早于 ``now``）的不返回，
    ``expires_at = 0`` 表示永不过期。
    """

    session = str(session_id or "").strip()
    wanted = {
        str(word).strip().lower() for word in (keywords or []) if str(word).strip()
    }
    if not session or not wanted:
        return []
    stamp = float(now or 0.0) or time.time()
    floor = max(1, int(min_overlap or 1))
    rows = conn.execute(
        "SELECT * FROM knowledge_card WHERE session_id = ? AND status = 'active'"
        " AND (expires_at <= 0 OR expires_at > ?)",
        (session, stamp),
    ).fetchall()
    scored: list[tuple[int, float, float, dict[str, Any]]] = []
    for row in rows:
        card = _knowledge_card_row(row)
        if card is None:
            continue
        overlap = len(wanted & {word.lower() for word in card["keywords"]})
        if overlap < floor:
            continue
        scored.append((overlap, card["last_used_at"], card["updated_at"], card))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    top = max(1, int(limit or 3))
    result: list[dict[str, Any]] = []
    for overlap, _used, _updated, card in scored[:top]:
        item = dict(card)
        item["overlap"] = overlap
        result.append(item)
    return result


def touch_knowledge_card(
    conn: sqlite3.Connection, card_id: Any, *, now: Any = 0.0
) -> bool:
    """记一次"被用到了"：``use_count + 1``、刷新 ``last_used_at``；内容一个字不改。"""

    try:
        target = int(card_id or 0)
    except (TypeError, ValueError):
        return False
    if target <= 0:
        return False
    stamp = float(now or 0.0) or time.time()
    cursor = conn.execute(
        "UPDATE knowledge_card SET use_count = use_count + 1, last_used_at = ?"
        " WHERE id = ?",
        (stamp, target),
    )
    conn.commit()
    return bool(cursor.rowcount)


def expire_knowledge_cards(conn: sqlite3.Connection, *, now: Any = 0.0) -> int:
    """把到期的卡标成 ``expired``（不删行），返回改了几条。"""

    stamp = float(now or 0.0) or time.time()
    cursor = conn.execute(
        "UPDATE knowledge_card SET status = 'expired'"
        " WHERE status = 'active' AND expires_at > 0 AND expires_at <= ?",
        (stamp,),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def list_knowledge_cards(
    conn: sqlite3.Connection, session_id: Any, *, include_expired: bool = False
) -> list[dict[str, Any]]:
    """列这个会话的知识卡（默认只看还活着的），新的在前。"""

    session = str(session_id or "").strip()
    if not session:
        return []
    sql = "SELECT * FROM knowledge_card WHERE session_id = ?"
    params: list[Any] = [session]
    if not include_expired:
        sql += " AND status = 'active'"
    sql += " ORDER BY updated_at DESC, id DESC"
    rows = conn.execute(sql, tuple(params)).fetchall()
    cards: list[dict[str, Any]] = []
    for row in rows:
        card = _knowledge_card_row(row)
        if card is not None:
            cards.append(card)
    return cards
