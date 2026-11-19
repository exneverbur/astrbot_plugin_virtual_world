"""媒体资产的本地化缓存：内容寻址、格式嗅探、预算与失效清理。

计划 §6（阶段 B）。三件事：

1. **内容寻址**：文件名就是内容的 sha256（``content_id``），所以同一份字节换个
   URL、换个文件名回来仍然是同一份资产，只落一份文件、只写一条清单；不同字节
   即使同名也天然分开。
2. **真实格式**：只看文件头的魔数，不信 URL 后缀，也不信调用方声明的 mime。
   非图片内容直接拒收（``UnsupportedMediaError``），错误是给人看的中文。
3. **有界缓存**：单文件上限、总容量上限、保留期，加上"正在用的不许删"（pin）。
   清理只删 root 内、文件名合法、且 ``resolve()`` 后仍在 root 之内的文件。

对外只依赖标准库。所有时间都走注入的 ``clock``（默认 ``time.time``），逻辑里不
自己调 ``time.time()``，所以测试可以把表拨到任意时刻。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 工程初值：单文件、总容量、保留期。**这些是工程初值，不是用户确定的需求上限**，
# 可以也应该被构造参数覆盖；真实数值需要压测和配置校验后再定（计划 §6 B1）。
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_RETENTION_SECONDS = 14 * 24 * 3600

# 纯逻辑上限：防止 manifest 被写坏后无上限增长（同样可被构造参数覆盖）。
DEFAULT_MAX_ENTRIES = 5000

# 清单文件名（root 内）。损坏时安全降级为空清单，不抛异常。
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

MEDIA_DIR_NAME = "media"
TMP_DIR_NAME = "tmp"

# 未知格式的兜底类型。
OCTET_STREAM = "application/octet-stream"


class MediaError(Exception):
    """媒体库异常的基类：消息是给人看的中文。"""


class UnsupportedMediaError(MediaError):
    """内容不是能认出来的图片（或根本不是图片）。"""


class ContentTooLargeError(MediaError):
    """内容超过单文件预算。"""


class InvalidMediaIdError(MediaError):
    """media_id 不合法（不是 64 位十六进制，或带路径分隔符/穿越）。"""


# --------------------------------------------------------------------------- #
# 纯函数：内容寻址与格式嗅探
# --------------------------------------------------------------------------- #

def content_id(data: bytes) -> str:
    """返回内容的 sha256 十六进制串（小写），即 ``media_id``。

    只有字节决定 id：来源 URL、文件名、声明 mime 都不参与。因此同一份图换个
    临时链接回来仍然是同一个 id，不同内容即使同名也不会撞车。
    """

    return hashlib.sha256(bytes(data)).hexdigest()


def looks_like_media_id(value: object) -> bool:
    """media_id 是否"长得像"一个合法 id：正好 64 位十六进制。

    这里只做格式判断（快、可对任意输入调用）；真正落盘前还会再 ``resolve()``
    一次并校验仍在 root 之内，见 ``MediaStore._path_for``。
    """

    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


def is_safe_media_id(value: object) -> bool:
    """外部输入是否可作为 media_id 使用：格式合法，且不含路径成分。

    明确拒绝 ``..``、正/反斜杠、绝对路径——这些一律当作"查不到"，不做任何
    文件系统操作。
    """

    if not looks_like_media_id(value):
        return False
    assert isinstance(value, str)
    if ".." in value or "/" in value or "\\" in value:
        return False
    if os.path.isabs(value) or os.path.splitdrive(value)[0]:
        return False
    return True


def sniff_mime(data: bytes) -> str:
    """按文件头魔数判断图片类型；认不出来返回 ``"application/octet-stream"``。

    判断只看字节：不看文件名后缀，也不看调用方传入的 mime 声明。支持
    PNG / JPEG / GIF / WEBP / BMP。
    """

    head = bytes(data[:32])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head.startswith(b"BM"):
        return "image/bmp"
    # WEBP：RIFF 容器 + "WEBP" 标记（第 8~12 字节）
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return OCTET_STREAM


def is_image_mime(mime: str) -> bool:
    """这个 mime 是否是我们认得的图片类型。"""

    return str(mime or "").startswith("image/")


def caption_key(media_id: str, model_id: str, prompt_version: str, schema_version: str) -> str:
    """标题缓存的版本键：``media_id + model_id + prompt_version + schema_version``。

    语义（计划 §6 B2）：**模型 / prompt / schema 任何一个版本变了，旧标题就不再
    可信，缓存失效**——但失效只针对标题，图片资产本身不删、也不需要重新下载：
    资产由内容 hash 寻址，与用哪个模型看过它无关。键之间用 ``\\x1f`` 分隔，
    避免 ``"a|b"`` 和 ``"a"+"|b"`` 这类拼接歧义。
    """

    parts = (str(media_id or ""), str(model_id or ""), str(prompt_version or ""), str(schema_version or ""))
    return "\x1f".join(parts)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class MediaAsset:
    """一条媒体资产的元数据。

    注意 ``source``：内容寻址之下，同一份字节可能从多个来源进来。第一条来源单
    独保留，其余去重后放在 ``sources`` 里；``source`` 永远等于 ``sources[0]``。
    """

    media_id: str
    mime: str
    size: int
    created_at: float
    last_used_at: float
    source: str = ""
    sources: list[str] = field(default_factory=list)
    hits: int = 0
    pins: list[str] = field(default_factory=list)

    @property
    def pinned(self) -> bool:
        """是否被某个正在使用它的引用钉住（活跃任务、待发送）。"""

        return bool(self.pins)

    def to_dict(self) -> dict[str, Any]:
        """转成清单里存的那份朴素字典。"""

        return {
            "media_id": self.media_id,
            "mime": self.mime,
            "size": int(self.size),
            "created_at": float(self.created_at),
            "last_used_at": float(self.last_used_at),
            "source": self.source,
            "sources": list(self.sources),
            "hits": int(self.hits),
            "pins": list(self.pins),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaAsset | None":
        """从清单条目还原；字段不可用时返回 ``None``（调用方跳过这一条）。"""

        try:
            media_id = str(data.get("media_id") or "")
            if not looks_like_media_id(media_id):
                return None
            sources = [str(s) for s in (data.get("sources") or []) if str(s)]
            source = str(data.get("source") or "")
            if source and source not in sources:
                sources.insert(0, source)
            if not source and sources:
                source = sources[0]
            pins = [str(p) for p in (data.get("pins") or []) if str(p)]
            return cls(
                media_id=media_id,
                mime=str(data.get("mime") or OCTET_STREAM),
                size=max(0, int(data.get("size") or 0)),
                created_at=float(data.get("created_at") or 0.0),
                last_used_at=float(data.get("last_used_at") or 0.0),
                source=source,
                sources=sources,
                hits=max(0, int(data.get("hits") or 0)),
                pins=pins,
            )
        except Exception:
            return None


@dataclass
class GCReport:
    """一次 gc 的结果：删了什么、为什么，以及为什么留下了别的。"""

    removed_ids: list[str] = field(default_factory=list)
    removed_count: int = 0
    removed_bytes: int = 0
    # 每条资产被保留（或未被保留）的一句理由，便于解释"为什么这张还在"
    reasons: dict[str, str] = field(default_factory=dict)
    # 预算/保留期在**构造时**生效的取值，报告里带出来方便核对
    max_total_bytes: int = 0
    retention_seconds: float = 0.0

    @property
    def kept_count(self) -> int:
        """保留下来的条数。"""

        return len(self.reasons)

    def to_dict(self) -> dict[str, Any]:
        """给日志/页面用的朴素字典。"""

        return {
            "removed_ids": list(self.removed_ids),
            "removed_count": int(self.removed_count),
            "removed_bytes": int(self.removed_bytes),
            "reasons": dict(self.reasons),
            "kept_count": self.kept_count,
            "max_total_bytes": int(self.max_total_bytes),
            "retention_seconds": float(self.retention_seconds),
        }


@dataclass
class CaptionEntry:
    """一条标题缓存记录。"""

    key: str
    caption: str
    prefix: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """转成落盘用的字典。"""

        return {
            "key": self.key,
            "caption": self.caption,
            "prefix": self.prefix,
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaptionEntry | None":
        """从落盘字典还原；不可用时返回 ``None``。"""

        try:
            key = str(data.get("key") or "")
            if not key:
                return None
            return cls(
                key=key,
                caption=str(data.get("caption") or ""),
                prefix=str(data.get("prefix") or ""),
                updated_at=float(data.get("updated_at") or 0.0),
            )
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# 标题缓存
# --------------------------------------------------------------------------- #

class CaptionCache:
    """按版本键缓存图片标题：``caption_key`` -> 标题文本。

    版本键由 ``caption_key(media_id, model_id, prompt_version, schema_version)``
    生成。模型/prompt/schema 换版本时，旧键自然不再被查询命中——所以**标题缓存
    会失效，但图片资产不动**（资产归 ``MediaStore`` 管，按内容 hash 存）。

    ``path`` 给了就顺带落盘（原子写）；JSON 损坏时当作空缓存，不抛异常。
    """

    def __init__(self, path: "Path | None" = None, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path) if path is not None else None
        self._clock = clock
        self._entries: dict[str, CaptionEntry] = {}
        self._lock = threading.RLock()
        self._load()

    # ---------------- 读写 ----------------

    def get(self, key: str) -> str | None:
        """取标题；没有这个键返回 ``None``。"""

        with self._lock:
            entry = self._entries.get(str(key or ""))
            return None if entry is None else entry.caption

    def get_entry(self, key: str) -> CaptionEntry | None:
        """取整条记录（含 prefix 与写入时间）。"""

        with self._lock:
            return self._entries.get(str(key or ""))

    def put(self, key: str, caption: str, prefix: str = "") -> CaptionEntry | None:
        """写入/覆盖一条标题缓存并落盘；键为空时忽略并返回 ``None``。"""

        key = str(key or "")
        if not key:
            return None
        entry = CaptionEntry(
            key=key,
            caption=str(caption or ""),
            prefix=str(prefix or ""),
            updated_at=float(self._clock()),
        )
        with self._lock:
            self._entries[key] = entry
            self._save_locked()
        return entry

    def delete(self, key: str) -> bool:
        """删掉一条标题缓存（只删标题，不碰图片资产）。"""

        with self._lock:
            if self._entries.pop(str(key or ""), None) is None:
                return False
            self._save_locked()
            return True

    def invalidate_media(self, media_id: str) -> int:
        """把某个 media_id 的所有版本标题一起作废（版本升级时用）。"""

        prefix = f"{str(media_id or '')}\x1f"
        with self._lock:
            dead = [k for k in self._entries if k.startswith(prefix)]
            for key in dead:
                self._entries.pop(key, None)
            if dead:
                self._save_locked()
            return len(dead)

    def clear(self) -> int:
        """清空标题缓存；返回清掉的条数。"""

        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._save_locked()
            return count

    def stats(self) -> dict[str, Any]:
        """条数等概况。"""

        with self._lock:
            entries = list(self._entries.values())
        return {"count": len(entries), "path": str(self._path) if self._path else ""}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ---------------- 落盘 ----------------

    def _load(self) -> None:
        path = self._path
        if path is None:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError, OSError):
            return
        try:
            data = json.loads(raw)
            items = data.get("entries") if isinstance(data, dict) else data
            loaded: dict[str, CaptionEntry] = {}
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        entry = CaptionEntry.from_dict(item)
                        if entry is not None:
                            loaded[entry.key] = entry
            self._entries = loaded
        except Exception:
            # 损坏 = 没有缓存，不能因此让插件起不来
            self._entries = {}

    def _save_locked(self) -> None:
        path = self._path
        if path is None:
            return
        payload = {
            "version": MANIFEST_VERSION,
            "entries": [entry.to_dict() for entry in self._entries.values()],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            tmp = path.parent / f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
        except Exception:
            # 缓存落盘失败不该影响主流程（内存里那份仍然有效）
            pass


# --------------------------------------------------------------------------- #
# 媒体库
# --------------------------------------------------------------------------- #

class MediaStore:
    """图片资产的本地库：内容寻址落盘 + JSON 清单 + pin/GC。

    ``root`` 是数据目录（运行时由外部传入），库内自己维护两个子目录：

    - ``<root>/media/<media_id>``：正式文件，文件名就是内容 sha256；
    - ``<root>/tmp/``：写入中转，落盘靠 ``os.replace`` 保证原子。

    元数据（hits / last_used_at / pins / source / mime / size）保存在
    ``<root>/manifest.json``，损坏时降级为空清单而不是崩溃。

    参数 ``max_file_bytes`` / ``max_total_bytes`` / ``retention_seconds`` 是
    **工程初值，不是需求上限**（计划 §6 B1 明确要求可调）：调用方按配置覆盖即可。
    ``clock`` 用于测试注入时间。
    """

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], float] = time.time,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.root = Path(root)
        self.max_file_bytes = int(max_file_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self.retention_seconds = float(retention_seconds)
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self.media_dir = self.root / MEDIA_DIR_NAME
        self.tmp_dir = self.root / TMP_DIR_NAME
        self.manifest_path = self.root / MANIFEST_NAME
        self._lock = threading.RLock()
        self._tmp_seq = 0
        self._assets: dict[str, MediaAsset] = {}
        self.manifest_was_corrupt = False
        self._ensure_dirs()
        self._loaded_now = float(self._clock())
        self._load()

    # ---------------- 目录与清单 ----------------

    def _ensure_dirs(self) -> None:
        """建好 root / media / tmp。"""

        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        """读清单；文件不存在或内容损坏都降级为空清单。"""

        self._assets = {}
        self.manifest_was_corrupt = False
        try:
            raw = self.manifest_path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError, OSError):
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("manifest 顶层不是对象")
            items = data.get("assets")
            if not isinstance(items, dict):
                raise ValueError("manifest 缺少 assets 字典")
            for key, item in items.items():
                if not isinstance(item, dict):
                    continue
                item.setdefault("media_id", key)
                asset = MediaAsset.from_dict(item)
                if asset is None:
                    continue
                self._assets[asset.media_id] = asset
        except Exception:
            # 损坏：重建为空，不让插件因为一个坏 JSON 起不来
            self._assets = {}
            self.manifest_was_corrupt = True

    def _save_locked(self) -> None:
        """把清单原子写回（调用方需持锁）。

        落盘失败不抛异常：内存里那份元数据仍然有效，最多是这次变更没持久化；
        因为一个写不进去的 JSON 就让"发图"整条链路失败不值得。
        """

        try:
            self._ensure_dirs()
            payload = {
                "version": MANIFEST_VERSION,
                "updated_at": float(self._clock()),
                "assets": {mid: asset.to_dict() for mid, asset in self._assets.items()},
            }
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            tmp = self._new_tmp_path("manifest")
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._replace_locked(tmp, self.manifest_path)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
        except Exception:
            pass

    def save(self) -> None:
        """公开的清单落盘入口（正常路径里 put/pin/unpin/gc 已自动落盘）。"""

        with self._lock:
            self._save_locked()

    # ---------------- 路径安全 ----------------

    def _new_tmp_path(self, tag: str) -> Path:
        """在 ``<root>/tmp/`` 下生成一个唯一临时文件名。"""

        self._tmp_seq += 1
        stamp = int(float(self._clock()) * 1000)
        return self.tmp_dir / f"{tag}-{os.getpid()}-{self._tmp_seq}-{stamp}.part"

    def _replace_locked(self, src: Path, dst: Path) -> None:
        """原子替换；如果"目标目录"被替换成了软链接，删掉临时文件后拒绝。"""

        resolved = dst.resolve()
        if not self._within_root(resolved):
            try:
                src.unlink()
            except OSError:
                pass
            raise InvalidMediaIdError(f"拒绝写入数据目录之外的路径：{resolved}")
        os.replace(src, dst)

    def _within_root(self, resolved: Path) -> bool:
        """已 resolve 的路径是否仍在 root 之内。"""

        try:
            root = self.root.resolve()
        except OSError:
            return False
        return resolved == root or root in resolved.parents

    def _path_for(self, media_id: object) -> Path | None:
        """把 media_id 映射成文件路径；不合法或落在 root 之外一律 ``None``。

        三重校验：格式必须是 64 位十六进制、不能带任何路径成分、``resolve()``
        之后必须仍在 root 之内。任何一步不过都直接当"查不到"，绝不碰文件系统。
        """

        if not is_safe_media_id(media_id):
            return None
        assert isinstance(media_id, str)
        try:
            candidate = self.media_dir / media_id
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            return None
        if not self._within_root(resolved):
            return None
        return candidate

    # ---------------- 写 ----------------

    def put(self, data: bytes, *, source: str = "") -> MediaAsset:
        """登记一份图片字节，返回它的资产元数据。

        内容已存在时只增加 ``hits``、刷新 ``last_used_at``（并补记来源），**不
        重写文件**。写入始终是"先落 ``tmp/`` 再 ``os.replace``"，所以读到的文件
        要么不存在、要么是完整的。

        非图片内容抛 ``UnsupportedMediaError``；超过单文件预算抛
        ``ContentTooLargeError``。两者消息都是中文。
        """

        payload = bytes(data)
        size = len(payload)
        if self.max_file_bytes > 0 and size > self.max_file_bytes:
            raise ContentTooLargeError(
                f"图片 {size} 字节超过单文件上限 {self.max_file_bytes} 字节，已拒绝保存"
            )
        mime = sniff_mime(payload)
        if not is_image_mime(mime):
            raise UnsupportedMediaError(
                f"认不出这是什么图片（按文件头判断为 {mime}），"
                f"只接受 PNG/JPEG/GIF/WEBP/BMP，已拒绝保存"
            )
        media_id = content_id(payload)
        now = float(self._clock())
        src = str(source or "")

        with self._lock:
            existing = self._assets.get(media_id)
            target = self._path_for(media_id)
            if target is None:
                # 自己算出来的 id 必定合法，走到这里说明目录结构被换过，宁可不写
                raise InvalidMediaIdError(f"media_id 不合法或越出数据目录：{media_id}")
            if existing is not None and self._file_exists(target):
                # 命中已有资产：只记一次使用，不重写文件
                existing.hits += 1
                existing.last_used_at = now
                self._remember_source(existing, src)
                self._save_locked()
                return existing
            # 文件不在（首次写入，或清单里有记录但文件被手工删了）→ 补写
            self._write_locked(target, payload)
            if existing is None:
                existing = MediaAsset(
                    media_id=media_id,
                    mime=mime,
                    size=size,
                    created_at=now,
                    last_used_at=now,
                    source=src,
                    sources=[src] if src else [],
                    hits=0,
                    pins=[],
                )
                self._assets[media_id] = existing
                self._enforce_entries_locked()
            else:
                existing.mime = mime
                existing.size = size
                existing.last_used_at = now
                self._remember_source(existing, src)
            self._save_locked()
            return existing

    def _write_locked(self, target: Path, payload: bytes) -> None:
        """先写 ``tmp/`` 临时文件，再 ``os.replace`` 到目标；失败不留半截文件。"""

        self._ensure_dirs()
        tmp = self._new_tmp_path("media")
        try:
            with open(tmp, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # 先校验目标仍在 root 内，再原子换名
            self._replace_locked(tmp, target)
        except Exception:
            # 失败路径：把临时文件清掉，media/ 下不会出现半个文件
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _file_exists(path: Path) -> bool:
        """目标是否是一个真实的普通文件。"""

        try:
            return path.is_file()
        except OSError:
            return False

    @staticmethod
    def _remember_source(asset: MediaAsset, source: str) -> None:
        """记住来源；同一份字节可能从多个来源进来，保留第一条并去重其余。"""

        if not source:
            return
        if not asset.source:
            asset.source = source
        if source not in asset.sources:
            asset.sources.append(source)

    def _enforce_entries_locked(self) -> None:
        """条数上限：超出时淘汰最久未使用的**未被 pin** 的条目。

        这是清单本身的安全阀，和 gc 的字节预算相互独立。如果全都被 pin，就
        允许暂时超限——正在用的东西不能为了图表好看删掉。
        """

        if self.max_entries <= 0 or len(self._assets) <= self.max_entries:
            return
        order = sorted(
            (a for a in self._assets.values() if not a.pinned),
            key=lambda a: a.last_used_at,
        )
        overflow = len(self._assets) - self.max_entries
        for asset in order[:overflow]:
            self._assets.pop(asset.media_id, None)
            target = self._path_for(asset.media_id)
            if target is not None:
                self._safe_unlink(target)

    # ---------------- 读 ----------------

    def get(self, media_id: str) -> Path | None:
        """取出资产的本地路径；查不到/文件不在返回 ``None``。

        查到会记一次使用（hits +1、last_used_at 刷新）。
        """

        target = self._path_for(media_id)
        if target is None:
            return None
        with self._lock:
            if not self._file_exists(target):
                return None
            asset = self._assets.get(str(media_id))
            if asset is not None:
                asset.hits += 1
                asset.last_used_at = float(self._clock())
                self._save_locked()
            return target

    def by_source(self, source: str) -> "MediaAsset | None":
        """按「当初是从哪个地址收进来的」找回资产；没有返回 ``None``。

        2026-10-20 加：同一张图被反复发（表情包来回转、群里刷屏）时，源地址一样
        就不必再下一遍。只认**内容和文件都还在**的条目，避免把"已删掉的清单项"
        当成命中。

        为什么按线性扫：``_assets`` 上限几千条、这个调用每轮至多十几次，
        为它维护一张反向索引（还要处理 pin / 淘汰 / 多来源）不划算。
        """

        want = str(source or "").strip()
        if not want:
            return None
        with self._lock:
            for asset in self._assets.values():
                if want != asset.source and want not in asset.sources:
                    continue
                target = self._path_for(asset.media_id)
                if target is None or not self._file_exists(target):
                    continue
                asset.hits += 1
                asset.last_used_at = float(self._clock())
                self._save_locked()
                return asset
        return None

    def open_bytes(self, media_id: str) -> bytes | None:
        """读出资产字节；查不到返回 ``None``。不改变任何元数据。"""

        target = self._path_for(media_id)
        if target is None:
            return None
        with self._lock:
            if not self._file_exists(target):
                return None
            try:
                return target.read_bytes()
            except OSError:
                return None

    # ---------------- pin / unpin ----------------

    def pin(self, media_id: str, ref: str) -> bool:
        """钉住资产：``ref`` 是"谁在用"的标识（活跃任务、待发送消息）。

        被钉住的资产不参与 gc，哪怕它已经过了保留期。返回是否成功钉上
        （资产或 id 不合法时为 ``False``）。
        """

        target = self._path_for(media_id)
        if target is None:
            return False
        ref = str(ref or "")
        with self._lock:
            asset = self._assets.get(str(media_id))
            if asset is None or not self._file_exists(target):
                return False
            if not ref:
                return False
            if ref not in asset.pins:
                asset.pins.append(ref)
                self._save_locked()
            return True

    def unpin(self, media_id: str, ref: str) -> bool:
        """解除一个引用；返回是否真的移除了（幂等，重复解钉返回 ``False``）。"""

        if self._path_for(media_id) is None:
            return False
        ref = str(ref or "")
        with self._lock:
            asset = self._assets.get(str(media_id))
            if asset is None:
                return False
            if ref in asset.pins:
                asset.pins.remove(ref)
                self._save_locked()
                return True
            return False

    def pinned(self, media_id: str) -> bool:
        """资产当前是否被至少一个引用钉住。"""

        if self._path_for(media_id) is None:
            return False
        with self._lock:
            asset = self._assets.get(str(media_id))
            return bool(asset is not None and asset.pins)

    def unpin_ref(self, ref: str) -> int:
        """按引用一次性解钉（这一轮/这条待发送的消息结束了）。

        活跃任务与待发送的引用是**整批**结束时一起放掉的：调用方不用记住自己
        钉过哪几张图，给同一个 ``ref`` 就能把自己留下的钉子全拔掉。
        返回真正被解钉的资产数量。
        """

        ref = str(ref or "")
        if not ref:
            return 0
        touched = 0
        with self._lock:
            for asset in self._assets.values():
                if ref in asset.pins:
                    asset.pins.remove(ref)
                    touched += 1
            if touched:
                self._save_locked()
        return touched

    # ---------------- 清理 ----------------

    def discard(self, media_id: str) -> bool:
        """彻底扔掉一份资产：清单条目与文件一起删（**被 pin 的不动**）。

        与 :meth:`gc` 的区别：gc 是按预算/过期挑着删，这里是"明确不要这一张"。
        调用方要先确认没有别的东西还在引用它（例如同一张图还挂在另一条收藏上）。
        """

        key = str(media_id or "").strip()
        if not key:
            return False
        with self._lock:
            asset = self._assets.get(key)
            if asset is None:
                return False
            if asset.pinned:
                return False
            self._assets.pop(key, None)
            self._save_locked()
        target = self._path_for(key)
        return self._safe_unlink(target)

    def gc_plan(self) -> GCReport:
        """只看不动：把 ``gc()`` 会删的东西算一遍（清理菜单要显示影响）。"""

        removed_ids, removed_bytes, reasons = self._gc_decide()
        return GCReport(
            removed_ids=removed_ids,
            removed_count=len(removed_ids),
            removed_bytes=removed_bytes,
            reasons=reasons,
            max_total_bytes=self.max_total_bytes,
            retention_seconds=self.retention_seconds,
        )

    def _gc_decide(self) -> tuple[list[str], int, dict[str, str]]:
        """按「过期 → 超预算按最久未使用」算出该删哪些（不改任何状态）。"""

        now = float(self._clock())
        removed_ids: list[str] = []
        removed_bytes = 0
        reasons: dict[str, str] = {}

        assets = dict(self._assets)
        survivors: dict[str, MediaAsset] = {}
        for media_id, asset in assets.items():
            target = self._path_for(media_id)
            if target is None or not self._file_exists(target):
                # 清单有记录但文件没了：顺手清掉这条死记录
                reasons[media_id] = "文件已不存在，清掉清单里的死记录"
                removed_ids.append(media_id)
                removed_bytes += int(asset.size or 0)
                continue
            if asset.pinned:
                reasons[media_id] = f"有 {len(asset.pins)} 个使用中的引用钉住，保留"
                survivors[media_id] = asset
                continue
            if self.retention_seconds > 0 and now - float(asset.last_used_at) > self.retention_seconds:
                removed_ids.append(media_id)
                removed_bytes += int(asset.size or 0)
                reasons[media_id] = (
                    f"已 {int(now - float(asset.last_used_at))} 秒未使用，"
                    f"超过保留期 {int(self.retention_seconds)} 秒"
                )
                continue
            survivors[media_id] = asset

        total = sum(int(a.size or 0) for a in survivors.values())
        if self.max_total_bytes > 0 and total > self.max_total_bytes:
            # 只从**没有被 pin** 的里挑：正在用的东西不能为了压预算删掉，
            # 哪怕它最旧。全被 pin 的话允许暂时超预算。
            order = sorted(
                (a for a in survivors.values() if not a.pinned),
                key=lambda a: a.last_used_at,
            )
            for asset in order:
                if total <= self.max_total_bytes:
                    break
                if survivors.pop(asset.media_id, None) is None:
                    continue
                total -= int(asset.size or 0)
                removed_ids.append(asset.media_id)
                removed_bytes += int(asset.size or 0)
                reasons[asset.media_id] = (
                    f"总容量超出预算 {self.max_total_bytes} 字节，按最久未使用优先清理"
                )

        for media_id in survivors:
            reasons.setdefault(media_id, "未过期且在预算之内，保留")
        return removed_ids, removed_bytes, reasons

    def gc(self) -> GCReport:
        """清理缓存：过期优先，其次按最久未使用，**被 pin 的一律不动**。

        三条规则按顺序：

        1. 未被 pin **且** 距 ``last_used_at`` 超过 ``retention_seconds`` → 删；
        2. 未被 pin 且（清理后总量仍超 ``max_total_bytes``）→ 按 ``last_used_at``
           最旧优先删到预算之内；
        3. 其余保留，并在 ``reasons`` 里写明是哪一条保住了它。

        过期判断用注入的 ``clock``。返回 ``GCReport``（条数、字节数、理由）。
        只想看会删什么、先不动手就用 :meth:`gc_plan`。
        """

        removed_ids, removed_bytes, reasons = self._gc_decide()

        with self._lock:
            survivors = {
                media_id: asset
                for media_id, asset in self._assets.items()
                if media_id not in set(removed_ids)
            }
            self._assets = survivors
            self._save_locked()

        # 真正的删除放在锁外做，网络盘上 unlink 可能很慢
        for media_id in removed_ids:
            target = self._path_for(media_id)
            if target is not None:
                self._safe_unlink(target)

        return GCReport(
            removed_ids=removed_ids,
            removed_count=len(removed_ids),
            removed_bytes=removed_bytes,
            reasons=reasons,
            max_total_bytes=self.max_total_bytes,
            retention_seconds=self.retention_seconds,
        )

    def _safe_unlink(self, target: Path | None) -> bool:
        """删除前再确认一次：必须是普通文件、且 resolve() 后仍在 root 之内。

        这是计划 §6 B4"不能删除数据目录之外路径"的落点：任何一步不满足就放弃，
        不抛异常、不猜测。
        """

        if target is None:
            return False
        try:
            resolved = Path(target).resolve()
        except (OSError, RuntimeError):
            return False
        if not self._within_root(resolved):
            return False
        try:
            if not resolved.is_file():
                return False
            resolved.unlink()
            return True
        except OSError:
            return False

    # ---------------- 统计 ----------------

    def stats(self) -> dict[str, Any]:
        """条数、总字节、最旧/最新（按 last_used_at），以及 pin 情况。"""

        with self._lock:
            assets = list(self._assets.values())
        total_bytes = sum(int(a.size or 0) for a in assets)
        # "最旧/最新"按 last_used_at 排（缓存真正关心的是最后一次使用时间）
        least_used = min(assets, key=lambda a: a.last_used_at, default=None)
        most_used = max(assets, key=lambda a: a.last_used_at, default=None)
        oldest_created = min(assets, key=lambda a: a.created_at, default=None)
        newest_created = max(assets, key=lambda a: a.created_at, default=None)
        return {
            "count": len(assets),
            "total_bytes": total_bytes,
            "oldest": least_used.to_dict() if least_used is not None else None,
            "newest": most_used.to_dict() if most_used is not None else None,
            "oldest_created_at": float(oldest_created.created_at) if oldest_created is not None else 0.0,
            "newest_created_at": float(newest_created.created_at) if newest_created is not None else 0.0,
            "oldest_last_used_at": float(least_used.last_used_at) if least_used is not None else 0.0,
            "newest_last_used_at": float(most_used.last_used_at) if most_used is not None else 0.0,
            "pinned_count": sum(1 for a in assets if a.pinned),
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "retention_seconds": self.retention_seconds,
            "media_dir": str(self.media_dir),
            "manifest_corrupt": bool(self.manifest_was_corrupt),
        }

    def asset(self, media_id: str) -> MediaAsset | None:
        """取某条资产的元数据副本（不改动任何状态）。"""

        if self._path_for(media_id) is None:
            return None
        with self._lock:
            found = self._assets.get(str(media_id))
            return None if found is None else MediaAsset.from_dict(found.to_dict())

    def manifest(self) -> dict[str, Any]:
        """整份清单的副本（给页面/调试用）。"""

        with self._lock:
            return {mid: asset.to_dict() for mid, asset in self._assets.items()}

    def __len__(self) -> int:
        with self._lock:
            return len(self._assets)


__all__ = [
    "CaptionCache",
    "CaptionEntry",
    "ContentTooLargeError",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "DEFAULT_RETENTION_SECONDS",
    "GCReport",
    "InvalidMediaIdError",
    "MediaAsset",
    "MediaError",
    "MediaStore",
    "UnsupportedMediaError",
    "caption_key",
    "content_id",
    "is_image_mime",
    "is_safe_media_id",
    "looks_like_media_id",
    "sniff_mime",
]
