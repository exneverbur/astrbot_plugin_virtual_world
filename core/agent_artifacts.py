"""B4：产物的**磁盘**那一半 —— 写、列、删，全部限制在任务自己的目录里。

方案见 `.vwtest/b3-executor-plan.md` §3。路径策略（允许写到哪）在
:mod:`core.agent_tools`；这里只负责真的落盘、算大小、按台账清理。

为什么不进媒体资产库（`core/media_store.py`）：那是**图片**域（有内容身份、去重、GC、
手动 pin），这里是她生成的**文档**（每条都该独立留着、按任务组织、过期就删）。
两套东西混在一起，资产库那套"同内容只留一份"的语义反而会把两份内容相同的报告
合成一份——那是错的。
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from .agent_tools import artifact_root, safe_artifact_path

# 单个产物的默认上限（真正生效的上限由配置给，这里只是兜底）
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


def ensure_task_dir(base: Any, task_id: Any) -> Path:
    """建好这次任务的产物目录（已经存在就直接用）。"""

    root = artifact_root(base, task_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def guess_mime(name: Any) -> str:
    """按扩展名猜类型（猜不出给 ``application/octet-stream``，别编）。"""

    kind, _encoding = mimetypes.guess_type(str(name or ""))
    return str(kind or "application/octet-stream")


def write_artifact(
    base: Any,
    task_id: Any,
    name: Any,
    data: Any,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """把字节写进这次任务的目录，返回台账那一行要的字段（失败给 ``ok=False``）。

    **只认 bytes/str**：``str`` 按 UTF-8 编码（她写文档时最自然的形式）。
    路径不安全、超过上限、磁盘写不动都如实回原因，绝不半途留下一个坏文件。
    """

    target = safe_artifact_path(base, task_id, name)
    if target is None:
        return {"ok": False, "reason": "bad_name", "detail": f"这个文件名不能用：{name}"}
    if isinstance(data, str):
        blob = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        blob = bytes(data)
    else:
        return {"ok": False, "reason": "bad_data", "detail": "只接受文本或字节"}
    limit = int(max_bytes) if int(max_bytes or 0) > 0 else DEFAULT_MAX_BYTES
    if len(blob) > limit:
        return {
            "ok": False,
            "reason": "too_large",
            "detail": f"产物太大（{len(blob)} 字节，上限 {limit}）",
        }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    except OSError as exc:
        return {"ok": False, "reason": "write_failed", "detail": f"写文件失败：{exc}"}
    return {
        "ok": True,
        "reason": "",
        "detail": "",
        "name": target.name,
        "path": str(target),
        "mime": guess_mime(target.name),
        "size_bytes": len(blob),
    }


def read_artifact(base: Any, task_id: Any, name: Any) -> bytes | None:
    """读回一份产物（越界或不存在返回 ``None``）。"""

    target = safe_artifact_path(base, task_id, name)
    if target is None or not target.is_file():
        return None
    try:
        return target.read_bytes()
    except OSError:
        return None


def delete_artifact(path: Any) -> bool:
    """删一份产物文件（不存在也算成功；删不掉返回 ``False``）。"""

    text = str(path or "").strip()
    if not text:
        return False
    try:
        Path(text).unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def list_task_files(base: Any, task_id: Any) -> list[dict[str, Any]]:
    """这次任务的目录里现在有哪些文件（给页面与"孤儿文件"清理用）。"""

    root = artifact_root(base, task_id)
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for item in sorted(root.iterdir()):
        if not item.is_file():
            continue
        try:
            size = item.stat().st_size
        except OSError:
            size = 0
        found.append(
            {"name": item.name, "path": str(item), "size_bytes": size, "mime": guess_mime(item.name)}
        )
    return found


def prune_task_dirs(base: Any, keep_task_ids: Any) -> list[str]:
    """删掉**不在台账里**的任务目录（重启/清理之后留下的孤儿文件）。

    ``keep_task_ids`` 是台账里还有记录的任务号集合；返回被删掉的目录名。
    """

    root = Path(str(base)) / "artifacts"
    if not root.is_dir():
        return []
    keep = {str(item) for item in (keep_task_ids or [])}
    removed: list[str] = []
    for item in sorted(root.iterdir()):
        if not item.is_dir() or item.name in keep:
            continue
        try:
            for child in sorted(item.iterdir(), reverse=True):
                try:
                    child.unlink()
                except OSError:
                    pass
            os.rmdir(item)
        except OSError:
            continue
        removed.append(item.name)
    return removed
