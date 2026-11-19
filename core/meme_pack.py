"""表情包打包 / 解包（用户 m14731：导出成压缩包分享给别人，再导回来）。

压缩包里固定两份东西：

* ``manifest.json``：格式版本、导出时间，以及每一条的名字 / 描述 / 用法 / 包内路径 /
  内容哈希；
* ``images/`` 下的原图，文件名按内容哈希取，同名的两条不会互相覆盖。

这一层只做纯粹的字节活（不碰数据库、不碰网络），所以能单独测、也能在
``asyncio.to_thread`` 里跑而不占事件循环。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 压缩包里那份说明文件的名字。
MANIFEST = "manifest.json"

#: 认得出的格式标记（导错包时给一句人话，而不是 KeyError）。
FORMAT = "vw-meme-pack"


def _safe_suffix(media_id: str) -> str:
    """从资产路径里抠扩展名；抠不出就按 png。"""

    text = str(media_id or "").strip()
    if "." in text:
        suffix = "." + text.rsplit(".", 1)[-1].lower()[:5]
        if suffix[1:].isalnum():
            return suffix
    return ".png"


def _safe_label(name: str) -> str:
    """名字里不能进文件系统的字符去掉，只留一小段。"""

    bad = set('\\/:*?"<>|\n\r\t')
    cleaned = "".join(ch for ch in str(name or "") if ch not in bad).strip()
    # 前导点会变出「..名字」、隐藏文件名，一律去掉（用户 m14731：包内路径要干净）
    cleaned = cleaned.lstrip(".").strip()
    return cleaned[:20] or "meme"


def pack(rows: Iterable[Mapping[str, Any]]) -> tuple[str, bytes, int]:
    """把 ``{view..., data: bytes}`` 一行行打进 zip，返回 (文件名, 字节, 条数)。"""

    buffer = io.BytesIO()
    entries: list[dict[str, Any]] = []
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            data = bytes(row.get("data") or b"")
            if not data:
                continue
            suffix = _safe_suffix(str(row.get("media_id") or ""))
            digest = hashlib.sha256(data).hexdigest()[:16]
            filename = f"images/{digest}_{_safe_label(str(row.get('name') or ''))}{suffix}"
            if filename in used:
                stem, tail = os.path.splitext(filename)
                index = 2
                while filename in used:
                    filename = f"{stem}_{index}{tail}"
                    index += 1
            used.add(filename)
            archive.writestr(filename, data)
            entries.append(
                {
                    "name": str(row.get("name") or ""),
                    "meaning": str(row.get("meaning") or ""),
                    "use_when": str(row.get("use_when") or ""),
                    "avoid_when": str(row.get("avoid_when") or ""),
                    "note": str(row.get("personal_note") or ""),
                    "file": filename,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            )
        manifest = {
            "format": FORMAT,
            "version": 1,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(entries),
            "entries": entries,
        }
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"vw-memes-{stamp}.zip", buffer.getvalue(), len(entries)


#: 导出目录（相对插件数据目录）：用户自己去宿主的这里拿压缩包。
EXPORTS_DIR = "exports"


def write_pack(directory: str | os.PathLike, blob: bytes, filename: str) -> Path:
    """把导出包写到宿主的数据目录里，返回最终路径。

    用户 m07533：与其把几十 MB 的 base64 搬过页面桥，不如直接落在
    ``<data>/exports/`` 下让人自己去取。同秒重名时加序号，不覆盖上一次的导出。
    """

    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = Path(str(filename or "")).name or "vw-memes.zip"
    if not name.lower().endswith(".zip"):
        name = f"{name}.zip"
    path = target_dir / name
    index = 2
    while path.exists():
        path = target_dir / f"{name[:-4]}-{index}.zip"
        index += 1
    tmp = target_dir / f".{name}.tmp"
    tmp.write_bytes(bytes(blob))
    os.replace(tmp, path)
    return path


def list_packs(directory: str | os.PathLike, limit: int = 40) -> list[dict[str, Any]]:
    """列出 ``<data>/exports/`` 里现成的压缩包，新的在前。

    用户 m07774：导出走落盘之后，导入也应当能"直接拿服务端那个文件"——
    大包再过一遍页面桥的 base64 不可靠，读盘最稳。
    """

    root = Path(directory)
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.glob("*.zip"):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append(
            {
                "name": path.name,
                "size": int(stat.st_size),
                "mtime": float(stat.st_mtime),
            }
        )
    rows.sort(key=lambda item: item["mtime"], reverse=True)
    return rows[: max(1, int(limit))]


def resolve_pack(directory: str | os.PathLike, name: str) -> Path:
    """把"导出目录里的一个文件名"翻成绝对路径；越界名一律拒绝。"""

    root = Path(directory)
    text = str(name or "").strip()
    if not text:
        raise ValueError("要选一个压缩包")
    if "/" in text or "\\" in text or text in (".", ".."):
        raise ValueError("文件名里不能带路径")
    path = root / text
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:  # 符号链接指到目录外也算越界
        raise ValueError("配置文件不在导出目录里") from exc
    if not path.is_file():
        raise ValueError("找不到这个压缩包")
    return path


def unpack(blob: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """读导出的压缩包：返回 (manifest, {包内路径: 字节})。

    格式不对直接抛 ``ValueError``，调用方翻成一句中文原因。
    """

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        if MANIFEST not in names:
            raise ValueError("缺少 manifest.json（这不是 VW 导出的表情包）")
        try:
            manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"manifest.json 读不出来：{exc}") from exc
        if not isinstance(manifest, dict) or str(manifest.get("format") or "") != FORMAT:
            raise ValueError("manifest.json 的格式不对")
        files = {
            name: archive.read(name)
            for name in names
            if name.startswith("images/") and not name.endswith("/")
        }
    return manifest, files
