"""增量回收空闲页（``PRAGMA auto_vacuum = INCREMENTAL``）。

**为什么需要它**：SQLite 默认 ``auto_vacuum = NONE``，``DELETE`` 只把页挂进 freelist，
文件本身**不会缩**。2026-10 那次事故留下的现场就是这样：``decision_batches`` 从
12 万行删到几百行，文件还是 47 MB，其中约 44 MB 是永远还不回去的空闲页。

**为什么不能直接 ``VACUUM``**：普通 ``VACUUM`` 要整库重建、拿独占锁几十秒，挂在
每日维护或 tick 里会卡住所有读写。所以拆成两步：

1. :func:`ensure_incremental_vacuum` —— **一次性**把库切到 ``INCREMENTAL``。切换本身
   需要一次完整 ``VACUUM``，所以这里带两道保险：磁盘可用空间必须 ≥ 库大小 × 2.2，
   且先把库备份到 ``<data_dir>/backups/vacuum-*.db``。任何一步失败都只返回
   ``failed``，**绝不抛给调用方**（转换失败最多是"以后不缩"，不能让插件起不来）。
2. :func:`incremental_vacuum` —— 每次只搬 ``N`` 页（``PRAGMA incremental_vacuum(N)``），
   每搬一块看一眼 deadline，超时就停、下一页再来。锁只占一小会儿，没人能感觉到。

两个函数都只碰 ``conn.execute`` / ``conn.backup`` / ``conn.commit`` /
``conn.in_transaction`` 这几个接口，所以测试可以拿一个薄代理把调用记下来
（spy）而不必换掉真库。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from typing import Any, Callable

from .storage_health import human_bytes

AUTO_VACUUM_NONE = 0
"""``PRAGMA auto_vacuum`` 的 0：从不回收（SQLite 默认，也是老库的现状）。"""

AUTO_VACUUM_INCREMENTAL = 2
"""``PRAGMA auto_vacuum`` 的 2：允许 ``incremental_vacuum`` 分块回收。"""

DEFAULT_CHUNK_PAGES = 2000
"""每块搬多少页：一条 ``PRAGMA incremental_vacuum(N)`` 的粒度（默认约 8 MB）。"""

DEFAULT_DEADLINE_SECONDS = 1.5
"""一次回收最多占多久（秒）：超时立刻停手，剩下的下一次维护接着搬。"""

DEFAULT_MAX_PAGES = 20000
"""一次回收最多搬多少页（默认约 78 MB @ 4 KB 页）——再大就该分天了。"""

FREE_SPACE_FACTOR = 2.2
"""转换前要求：磁盘可用 ≥ 库大小 × 这个系数（``VACUUM`` 需要临时副本的空间）。"""

BACKUP_DIRNAME = "backups"
"""备份放在数据目录下的这个子目录里（和快照导出同处，用户找得到）。"""

BACKUP_PREFIX = "vacuum-"
"""备份文件名前缀：``vacuum-YYYYmmdd-HHMMSS.db``。"""


def _row_int(conn: Any, sql: str, default: int = -1) -> int:
    """跑一条返回单值的 PRAGMA；读不到就返回 ``default``（永不抛）。"""

    try:
        cursor = conn.execute(sql)
        try:
            row = cursor.fetchone()
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001 - 代理/旧驱动可能没有 close
                pass
        if not row:
            return default
        value = row[0]
        return int(value) if value is not None else default
    except Exception:  # noqa: BLE001 - 只读探测，失败按默认值走
        return default


def _database_path(conn: Any) -> str:
    """主库文件路径（``PRAGMA database_list`` 里 name='main' 那条）；读不到给空串。"""

    try:
        cursor = conn.execute("PRAGMA database_list")
        try:
            rows = cursor.fetchall()
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return ""
    for row in rows or ():
        try:
            name = str(row[1])
            path = str(row[2] or "")
        except (IndexError, TypeError):
            continue
        if name == "main" and path:
            return path
    return ""


def _execute(conn: Any, sql: str) -> None:
    """跑一条不需要结果集的语句（``PRAGMA`` / ``VACUUM``），把游标关掉。"""

    cursor = conn.execute(sql)
    try:
        cursor.fetchall()
    except Exception:  # noqa: BLE001 - VACUUM 之类没有结果集，取不到就算了
        pass
    finally:
        try:
            cursor.close()
        except Exception:  # noqa: BLE001
            pass


def _checkpoint(conn: Any) -> None:
    """把 WAL 并回主库并截断：不这么做，``os.path.getsize(主库)`` 看不到缩小。"""

    try:
        _execute(conn, "PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # noqa: BLE001 - checkpoint 失败不影响已搬走的页
        pass


def _restore_auto_vacuum(conn: Any, original: int, result: dict[str, Any]) -> None:
    """VACUUM 失败后把 header 里的 ``auto_vacuum`` 标记拨回原样。

    ``PRAGMA auto_vacuum=INCREMENTAL`` 会立刻写 header，而重建页布局的 ``VACUUM``
    可能在之后失败；不拨回去就留下「标记为 2、却从没按 2 重建过」的半状态。
    回滚本身失败也只能算了——绝不能因此抛出去。
    """

    restore = original if original in (AUTO_VACUUM_NONE, 1) else AUTO_VACUUM_NONE
    try:
        if _row_int(conn, "PRAGMA auto_vacuum", default=restore) != restore:
            _execute(conn, f"PRAGMA auto_vacuum={restore}")
            result["auto_vacuum"] = restore
    except Exception:  # noqa: BLE001 - 回滚失败不许影响调用方
        pass


def _backup(conn: Any, destination: str) -> None:
    """用 sqlite 在线备份 API 复制一份（不需要关闭连接、不阻塞读）。"""

    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
        target.commit()
    finally:
        target.close()


def ensure_incremental_vacuum(
    conn: Any,
    *,
    data_dir: str = "",
    disk_usage: Callable[[str], Any] | None = None,
    stamp: str | None = None,
) -> dict[str, Any]:
    """把库从 ``auto_vacuum=0`` 切到 ``INCREMENTAL``（一次性，带备份与空间闸门）。

    返回 ``{"status": "converted" | "already" | "skipped_low_space" | "failed", ...}``：

    * ``already``：本来就是 2，**不做任何写操作**（也不备份、不 VACUUM）；
    * ``skipped_low_space``：磁盘可用 < 库大小 × 2.2，主动放弃（库仍可用）；
    * ``failed``：备份 / PRAGMA / VACUUM 任一步出错，异常被吞在这里，库保持原样
      （``VACUUM`` 是原子的，不需要"回滚到备份"这种动作）。

    ``stamp`` 只给测试钉文件名用；生产走本地时间 ``YYYYmmdd-HHMMSS``。
    """

    started = time.monotonic()
    result: dict[str, Any] = {
        "status": "failed",
        "auto_vacuum": -1,
        "db_path": "",
        "db_bytes": 0,
        "free_bytes": None,
        "need_bytes": 0,
        "backup_path": "",
        "seconds": 0.0,
        "error": "",
    }
    try:
        mode = _row_int(conn, "PRAGMA auto_vacuum", default=-1)
        result["auto_vacuum"] = mode
        if mode == AUTO_VACUUM_INCREMENTAL:
            result["status"] = "already"
            return result
        path = _database_path(conn)
        result["db_path"] = path
        if not path or not os.path.isfile(path):
            result["status"] = "failed"
            result["error"] = "找不到主库文件（PRAGMA database_list 没有 main）"
            return result
        db_bytes = int(os.path.getsize(path))
        result["db_bytes"] = db_bytes
        need_bytes = int(db_bytes * FREE_SPACE_FACTOR)
        result["need_bytes"] = need_bytes
        usage = disk_usage or shutil.disk_usage
        free_bytes = int(usage(os.path.dirname(path) or ".").free)
        result["free_bytes"] = free_bytes
        if free_bytes < need_bytes:
            # 绝不硬来：空间不够时转换失败会把库卡在半路，比不缩更糟。
            result["status"] = "skipped_low_space"
            return result
        target_dir = str(data_dir or os.path.dirname(path) or ".")
        backup_dir = os.path.join(target_dir, BACKUP_DIRNAME)
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(
            backup_dir,
            f"{BACKUP_PREFIX}{stamp or time.strftime('%Y%m%d-%H%M%S')}.db",
        )
        _backup(conn, backup_path)
        result["backup_path"] = backup_path
        if getattr(conn, "in_transaction", False):
            conn.commit()
        _execute(conn, "PRAGMA auto_vacuum=INCREMENTAL")
        _execute(conn, "VACUUM")
        mode_after = _row_int(conn, "PRAGMA auto_vacuum", default=-1)
        result["auto_vacuum"] = mode_after
        if mode_after != AUTO_VACUUM_INCREMENTAL:
            result["status"] = "failed"
            result["error"] = f"VACUUM 之后 auto_vacuum={mode_after}（预期 2）"
            _restore_auto_vacuum(conn, mode, result)
            return result
        _checkpoint(conn)
        result["status"] = "converted"
    except Exception as exc:  # noqa: BLE001 - 转换失败绝不许让插件起不来
        _restore_auto_vacuum(conn, int(result.get("auto_vacuum", -1)), result)
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["seconds"] = round(time.monotonic() - started, 4)
    return result


def incremental_vacuum(
    conn: Any,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    chunk_pages: int = DEFAULT_CHUNK_PAGES,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """分块搬走空闲页，返回 ``{before, after, pages_freed, seconds, interrupted}``。

    * ``freelist_count == 0``：直接返回，**一条 PRAGMA 都不执行**；
    * 每块 ``N = min(chunk_pages, 剩余页, 本次剩余配额)``，搬完一块再看 deadline；
    * 到 deadline 就停，``interrupted=True``，没搬完的页下一次维护继续；
    * ``max_pages <= 0``：视作关闭，仅返回当前 freelist。

    异常同样被吞掉（写进 ``error``），调用方拿到的永远是统计字典。
    """

    started = time.monotonic()
    monotonic = clock or time.monotonic
    result: dict[str, Any] = {
        "before": 0,
        "after": 0,
        "pages_freed": 0,
        "page_size": _row_int(conn, "PRAGMA page_size", default=0),
        "seconds": 0.0,
        "interrupted": False,
        "error": "",
    }
    try:
        before = max(0, _row_int(conn, "PRAGMA freelist_count", default=0))
        result["before"] = before
        result["after"] = before
        if before <= 0:
            return result
        budget = int(max_pages)
        if budget <= 0:
            result["interrupted"] = True
            return result
        deadline = max(0.0, float(deadline_seconds))
        chunk_limit = max(1, int(chunk_pages))
        after = before
        while after > 0 and (before - after) < budget:
            if monotonic() - started >= deadline:
                break
            chunk = min(chunk_limit, after, budget - (before - after))
            if chunk <= 0:
                break
            _execute(conn, f"PRAGMA incremental_vacuum({int(chunk)})")
            new_after = max(0, _row_int(conn, "PRAGMA freelist_count", default=after))
            if new_after >= after:
                # 一页都搬不动（例如有长事务钉着旧快照）：别死循环，留给下一次。
                break
            after = new_after
            result["after"] = after
            result["pages_freed"] = before - after
        result["after"] = after
        result["pages_freed"] = max(0, before - after)
        if result["pages_freed"] > 0:
            # 主库文件要立刻变小，得先把 WAL 并回去；失败不算回收失败。
            _checkpoint(conn)
    except Exception as exc:  # noqa: BLE001 - 回收失败只记录，不抛
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["interrupted"] = bool(result["after"] > 0)
        result["seconds"] = round(time.monotonic() - started, 4)
    return result


def freelist_snapshot(conn: Any) -> dict[str, int]:
    """``{"freelist", "page_size", "db_bytes"}``：``/vw status`` 那一行用，只读、不抛。"""

    snapshot = {
        "freelist": max(0, _row_int(conn, "PRAGMA freelist_count", default=0)),
        "page_size": max(0, _row_int(conn, "PRAGMA page_size", default=0)),
        "db_bytes": 0,
    }
    try:
        path = _database_path(conn)
        if path and os.path.isfile(path):
            snapshot["db_bytes"] = int(os.path.getsize(path))
    except Exception:  # noqa: BLE001 - 量不到就给 0
        pass
    return snapshot


def vacuum_status_line(
    *,
    freelist: int,
    page_size: int = 0,
    last: dict[str, Any] | None = None,
    enabled: bool = True,
) -> str:
    """``/vw status`` 的「库空间」一行（只排版，不碰磁盘）。"""

    pages = max(0, int(freelist or 0))
    size = max(0, int(page_size or 0)) or 4096
    if last:
        record = f"{int(last.get('pages') or 0)} 页 / {int(last.get('ms') or 0)} ms"
    else:
        record = "未回收"
    suffix = "" if enabled else "（自动回收已关闭）"
    return (
        f"库空间：空闲页 {pages}（约 {human_bytes(pages * size)}），"
        f"上次回收 {record}{suffix}"
    )
