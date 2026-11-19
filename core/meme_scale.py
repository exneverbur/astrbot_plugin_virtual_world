"""表情包发送前缩图：长边 320、动图保真。

**为什么单独一个模块**：管理页早就在做同一件事（``main.py`` 的
``MEME_THUMB_MAX_PX`` / ``_meme_thumb_cache``），但那是"给页面看的小图"——键是
``(media_id, max_px)``、缓存是进程内 LRU、不落盘、也不看源文件时间。**发送**要的是
"落盘的、可复用的、跟着源文件更新的派生文件"，两者的 key 语义与生命周期都不同；
混用会让页面缩图缓存里混进要发出去的图，或者反过来（本轮需求明确要求不要复用那个 LRU）。

四条硬规矩：

1. **可选依赖，绝不抛**：Pillow 是宿主自带依赖（``core/meme_images`` 已经在借），
   但这里仍按可选 import 处理：没装、解不开、写不进去、源文件没了——一律返回
   **原图**并给出 ``reason``，调用方永远拿得到一个可用路径。发送链路不因为缩图失败
   而少发一张图。
2. **原图只读**：派生物写进 ``<data_dir>/meme_sent/``，文件名带 media_id 与目标长边；
   源文件 mtime 比派生物新就重建。``media_id = sha256(字节)``，改了原图会让收藏层的
   ``idx_meme_entries_media`` 唯一索引对不上，所以**一个字节都不动**。
3. **不划算就回退**：动图帧数 > :data:`MAX_ANIMATED_FRAMES`、解码+编码超过
   :data:`MAX_ANIMATED_SECONDS`、产出体积 ≥ 原图——全部回退原图。表情包是锦上添花，
   宁可发大一点的图，也不能让一条消息卡三秒。
4. **动图保真优先**：``animated="keep"`` 逐帧缩放后按原格式重打包（帧数、每帧时长、
   循环次数原样带过去）；``first`` 只发首帧；``skip`` 完全不碰动图。

**谁调用**：真正要把图片发出去的那一处（``core.engine.Engine.meme_send_source``），
以及 ``find_meme`` 的候选预览（:func:`compose_candidate_sheet`：先按同一把尺子缩图，
再多张拼成一张图——用户 2026-10-23 的要求）。管理页缩略图、导出、转述一律不走这里
——它们要的是"能看清/能读"，不是"发到群里别太大"。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # 可选依赖：宿主自带，但缺了也必须能跑（模块 docstring 第 1 条）
    from PIL import Image

    PILLOW_AVAILABLE = True
    PILLOW_PROBLEM = ""
except Exception as exc:  # pragma: no cover - 真机上 Pillow 由宿主提供
    Image = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False
    PILLOW_PROBLEM = f"{type(exc).__name__}: {exc}"

# --------------------------------------------------------------------------- #
# 常量（每个都写清出处）
# --------------------------------------------------------------------------- #

#: 默认长边。与 ``main.MEME_THUMB_MAX_PX`` 同值：管理页卡片与群里发的图一个尺度，
#: 用户在这两处看到的是同一张图的同一个大小。
DEFAULT_MAX_SIDE = 320

#: 长边 < 64 视为"关掉"（本轮需求的规格）。再小就不是缩图而是糊图了。
MIN_MAX_SIDE = 64

#: 长边上限。页面滑块的 240–1024 区间之外不再放大——缩图没有"放大"的道理。
MAX_MAX_SIDE = 1024

#: 动图策略（``world.meme_send_scale.animated``）。
ANIMATED_KEEP = "keep"
ANIMATED_FIRST = "first"
ANIMATED_SKIP = "skip"
ANIMATED_MODES = (ANIMATED_KEEP, ANIMATED_FIRST, ANIMATED_SKIP)

#: 动图帧数上限：超过就不缩（本轮需求的护栏）。按"声明帧数"先挡一道，逐帧处理时
#: 再按真实帧数挡一道——``n_frames`` 会撒谎（截断的 GIF 照样报一个漂亮的帧数）。
MAX_ANIMATED_FRAMES = 120

#: 解码 + 编码的耗时上限（秒）：动图缩图是同步 CPU 活，超时就不划算。
MAX_ANIMATED_SECONDS = 3.0

#: 静态 JPEG 的质量：85 是"群里看不太出差别、体积掉一个数量级"的常用档位。
JPEG_QUALITY = 85

#: webp 质量：比 JPEG 的 85 略低一点，webp 同质量下体积更小。
WEBP_QUALITY = 80

#: 派生物子目录：``<data_dir>/meme_sent/``（与 ``media`` / ``tmp`` 平级）。
CACHE_DIR_NAME = "meme_sent"

#: 扩展名 → Pillow 格式。清单外的（bmp/tiff/静态 gif…）一律落 PNG：
#: 无他，PNG 无损且 Pillow 一定写得出来。
_FORMAT_BY_SUFFIX = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}

#: 重采样：Pillow 12 把常量挪进了 ``Image.Resampling``，老版本还在 ``Image`` 上。
try:  # pragma: no cover - 取决于装的是哪个 Pillow
    _RESAMPLE = Image.Resampling.LANCZOS  # type: ignore[union-attr]
except Exception:  # pragma: no cover
    _RESAMPLE = getattr(Image, "LANCZOS", None)

#: 结果原因码 → 人话（日志与调试页用；不带变量的写死，带变量的走 :func:`reason_text`）。
REASON_TEXTS: dict[str, str] = {
    "": "",
    "scaled": "缩好了",
    "cache_hit": "复用上次缩好的",
    "disabled": "缩图关着",
    "already_small": "本来就不超过长边",
    "animated_skip": "动图按配置不处理",
    "pillow_missing": "没有 Pillow",
    "source_missing": "源文件不在了",
    "no_media_id": "没有 media_id",
    "decode_error": "图解不开",
    "cache_error": "缓存目录建不出来",
    "write_error": "派生文件写不进去",
    "no_gain": "缩完反而更大",
    "too_many_frames": "动图帧数太多",
    "too_slow": "缩图超时",
    "unsupported_animation": "这种动图不支持",
    "gif_off": "没开「发出去转 GIF」",
    "already_gif": "本来就是 GIF",
    "gif": "转成 GIF 了",
}

#: 需要留一条 ``logger.debug`` 的原因（关着 / 本来就小 / 缓存命中不算"出问题"，
#: 每次都记会刷屏；这里只记真的没缩成的那些）。
PROBLEM_REASONS = frozenset(
    {
        "pillow_missing",
        "source_missing",
        "no_media_id",
        "decode_error",
        "cache_error",
        "write_error",
        "no_gain",
        "too_many_frames",
        "too_slow",
        "unsupported_animation",
    }
)


#: GIF 派生物的文件名后缀（与缩图那批共用 ``meme_sent/``，靠后缀区分）。
GIF_SUFFIX = ".gif"


def _gif_name(key: str, max_side: int) -> str:
    return _cache_name(key, max_side, GIF_SUFFIX, tag="_gif")


def gif_file(
    source: Any,
    *,
    media_id: str,
    settings: ScaleSettings,
    cache_dir: Any,
) -> ScaleResult:
    """把**要发出去的那一张**转成 GIF；本身是动图的转回原图。

    为什么单独一步、而不是并进 ``scale_file``：缩图决定"多大"，转码决定"什么格式"，
    两者的缓存键与失败退化都不一样（转 GIF 允许体积变大——用户要的是 QQ 里的显示
    尺寸，不是省流量）。所以这里**不套 ``no_gain``**：GIF 比 PNG 大是常态。
    """

    started = time.monotonic()
    # ``_file_size`` 要 Path（缩图那条路进来的一直是 Path）；这里入口允许 str（引擎
    # 传的是 ``media_file_source`` 的字符串），统一转一次，别让"转 GIF"把发送搞崩。
    base_path = Path(str(source or ""))

    def keep(reason: str, *, width: int = 0, height: int = 0, frames: int = 1) -> ScaleResult:
        return ScaleResult(
            path=str(source or ""),
            scaled=False,
            reason=reason,
            width=width,
            height=height,
            frames=frames,
            source_bytes=_file_size(base_path),
            output_bytes=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    if not settings.gif:
        return keep("gif_off")
    if not PILLOW_AVAILABLE:
        return keep("pillow_missing")
    path = base_path
    if not str(source or "").strip() or not path.is_file():
        return keep("source_missing")
    if not str(media_id or "").strip():
        return keep("no_media_id")
    try:
        with Image.open(path) as probe:  # type: ignore[union-attr]
            fmt = str(getattr(probe, "format", "") or "").upper()
            frames = int(getattr(probe, "n_frames", 1) or 1)
            animated = bool(getattr(probe, "is_animated", False)) and frames > 1
            width, height = (int(probe.size[0]), int(probe.size[1]))
    except Exception:
        return keep("decode_error")
    if animated:
        # 动图转了会掉帧，用户明确说"本身是动图就不用转了"
        return keep("animated_skip", width=width, height=height, frames=frames)
    if fmt == "GIF":
        return keep("already_gif", width=width, height=height, frames=1)
    try:
        root = _cache_root(cache_dir)
        cache_path = _cache_path(root, _gif_name(str(media_id), settings.max_side))
    except Exception:
        return keep("cache_error")
    try:
        source_mtime = path.stat().st_mtime
    except OSError:
        return keep("source_missing")
    if _is_fresh(cache_path, source_mtime):
        return ScaleResult(
            path=str(cache_path),
            scaled=True,
            reason="cache_hit",
            width=width,
            height=height,
            frames=1,
            source_bytes=_file_size(base_path),
            output_bytes=_file_size(cache_path),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    try:
        with Image.open(path) as opened:  # type: ignore[union-attr]
            frame = opened.convert("RGBA")
    except Exception:
        return keep("decode_error")
    if settings.active and max(frame.size) > settings.max_side:
        frame.thumbnail((settings.max_side, settings.max_side), _RESAMPLE)
    try:
        palette = frame.convert("P", palette=Image.Palette.ADAPTIVE)  # type: ignore[union-attr]
        _atomic_write(cache_path, lambda tmp: _save(palette, tmp, "GIF"))
    except Exception:
        return keep("write_error")
    return ScaleResult(
        path=str(cache_path),
        scaled=True,
        reason="gif",
        width=int(frame.size[0]),
        height=int(frame.size[1]),
        frames=1,
        source_bytes=_file_size(base_path),
        output_bytes=_file_size(cache_path),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


async def gif_source(
    source: Any,
    *,
    media_id: str,
    settings: ScaleSettings,
    cache_dir: Any,
) -> ScaleResult:
    """异步包装：解码/编码是同步 CPU 活，挪进线程。"""

    return await asyncio.to_thread(
        gif_file,
        source,
        media_id=media_id,
        settings=settings,
        cache_dir=cache_dir,
    )


def reason_text(reason: str) -> str:
    """原因码 → 人话（未知原因码原样返回，方便日志里追）。"""

    key = str(reason or "")
    return REASON_TEXTS.get(key) or key or "没缩"


# --------------------------------------------------------------------------- #
# 配置读取（与 ``core.meme_settings`` 一个口径：只读、坏值退回默认）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScaleSettings:
    """这一轮要用的缩图口径（只读快照）。"""

    enabled: bool = True
    max_side: int = DEFAULT_MAX_SIDE
    animated: str = ANIMATED_KEEP
    gif: bool = True
    """发出去那一份转成 GIF（用户 2026-10-29）：QQ 里表情包按 GIF 显示尺寸更舒服。
    本身是动图的**不转**（转了会掉帧）。"""

    @property
    def active(self) -> bool:
        """真的会缩吗：开关开着**且**长边够长（< 64 视为关闭）。"""

        return bool(self.enabled) and int(self.max_side) >= MIN_MAX_SIDE


def _section(world: Any) -> Any:
    """取 ``world.meme_send_scale``（pydantic 模型或 dict 都认）。"""

    if world is None:
        return {}
    value = getattr(world, "meme_send_scale", None)
    if value is None and isinstance(world, Mapping):
        value = world.get("meme_send_scale")
    return {} if value is None else value


def _pick(section: Any, name: str, default: Any) -> Any:
    if section is None:
        return default
    if isinstance(section, Mapping):
        value = section.get(name, default)
    else:
        value = getattr(section, name, default)
    return default if value is None else value


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "开", "是"}:
            return True
        if text in {"0", "false", "no", "off", "关", "否"}:
            return False
    return default


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def scale_settings(world: Any) -> ScaleSettings:
    """从世界配置读出缩图口径。**只读**，坏值一律退回默认（不抛）。"""

    section = _section(world)
    enabled = _as_bool(_pick(section, "enabled", True), True)
    max_side = _as_int(_pick(section, "max_side", DEFAULT_MAX_SIDE), DEFAULT_MAX_SIDE)
    if max_side < 0:
        max_side = 0
    if max_side > MAX_MAX_SIDE:
        max_side = MAX_MAX_SIDE
    animated = str(_pick(section, "animated", ANIMATED_KEEP) or "").strip().lower()
    if animated not in ANIMATED_MODES:
        animated = ANIMATED_KEEP
    gif = _as_bool(_pick(section, "gif", True), True)
    return ScaleSettings(
        enabled=enabled, max_side=max_side, animated=animated, gif=gif
    )


#: 没有数据目录时的缓存根：**绝不写 CWD**（2026-10 测试跑出来的 5 个
#: ``sheet_*_256_*_c1*.jpg`` 就是 ``Path(".")`` 造成的）。退到系统临时目录，
#: 进程/机器重启可以丢——拼图与缩图本来就是可重建的派生物。
FALLBACK_CACHE_ROOT_NAME = "vw-meme-cache"


def _cache_root(data_dir: Any) -> Path:
    """数据目录 → 可写的缓存根；空值 / ``"."`` / CWD 本身一律退到临时目录。

    为什么连"CWD 本身"也要挡：``data_dir`` 缺省时老代码写的是 ``Path(".")``，
    只要进程的工作目录是仓库根，派生物就会直接躺在源码目录里（测试产物污染仓库）。
    """

    text = str(data_dir or "").strip()
    if not text or text in {".", "./", ".\\"}:
        return Path(tempfile.gettempdir()) / FALLBACK_CACHE_ROOT_NAME
    try:
        if Path(text).resolve() == Path.cwd().resolve():
            return Path(tempfile.gettempdir()) / FALLBACK_CACHE_ROOT_NAME
    except OSError:  # pragma: no cover - 坏路径/权限问题不该影响发送
        pass
    return Path(text)


def scale_cache_dir(data_dir: Any) -> Path:
    """派生物目录：``<data_dir>/meme_sent``（与资产库同一根，不另开一个）。"""

    return _cache_root(data_dir) / CACHE_DIR_NAME


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScaleResult:
    """一次缩图的结论：**要发哪个路径** + 为什么。"""

    path: str
    scaled: bool = False
    reason: str = ""
    width: int = 0
    height: int = 0
    frames: int = 1
    source_bytes: int = 0
    output_bytes: int = 0
    elapsed_ms: int = 0


class _Fallback(Exception):
    """内部用的"放弃缩图"信号（只在模块内抛，绝不外泄）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# 纯逻辑：给定源文件 + 口径 → 要发的路径
# --------------------------------------------------------------------------- #


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _safe_token(text: Any) -> str:
    """文件名里的 media_id：只留安全字符（sha256 十六进制原样通过）。"""

    token = re.sub(r"[^0-9A-Za-z_-]+", "-", str(text or "")).strip("-")
    return token[:96] or "image"


def _cache_name(media_id: str, max_side: int, suffix: str, tag: str = "") -> str:
    """``<media_id>_<长边><tag><扩展名>``——media_id 必须在文件名里。

    ``tests/test_meme_send.py`` 有 ``assertIn(media_id, refs[0])`` 的老断言：发送的
    路径必须还能反查到是哪张收藏，所以 media_id 不截断（sha256 正好 64 个字符）。
    """

    return f"{_safe_token(media_id)}_{int(max_side)}{tag}{suffix}"


def _cache_path(cache_dir: Any, name: str) -> Path:
    """``cache_dir`` 就是 :func:`scale_cache_dir` 算出来的 ``meme_sent`` 目录本身。

    传 ``None`` / ``"."`` 时退到临时目录（见 :func:`_cache_root`）：拼图调用方
    ``render_sheets(..., cache_dir=None)`` 曾经因此把文件写进 CWD。
    """

    return _cache_root(cache_dir) / name


def _is_fresh(target: Path, source_mtime: float) -> bool:
    try:
        return target.is_file() and target.stat().st_mtime >= source_mtime
    except OSError:
        return False


def _atomic_write(target: Path, writer) -> None:
    """先写同目录临时文件再 ``os.replace``：半个文件不能被发出去。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        writer(tmp)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()


def _save(
    image: Any,
    target: Path,
    fmt: str,
    *,
    frames: list[Any] | None = None,
    durations: list[int] | None = None,
    loop: int = 0,
) -> None:
    """按格式落盘（动图带 ``save_all``；JPEG 不能带 alpha）。"""

    rest = list(frames or [])
    if not rest:
        still = image
        if fmt == "JPEG" and still.mode not in ("RGB", "L", "CMYK"):
            still = still.convert("RGB")
        kwargs: dict[str, Any] = {}
        if fmt == "JPEG":
            kwargs = {"quality": JPEG_QUALITY, "optimize": True}
        elif fmt == "WEBP":
            kwargs = {"quality": WEBP_QUALITY}
        still.save(target, format=fmt, **kwargs)
        return
    kwargs = {"save_all": True, "append_images": rest}
    if durations:
        kwargs["duration"] = list(durations)
    if fmt in ("GIF", "WEBP", "PNG"):
        kwargs["loop"] = int(loop)
    if fmt == "WEBP":
        kwargs["quality"] = WEBP_QUALITY
    if fmt == "GIF":
        # GIF 的 ``optimize`` 会重排调色板与帧差，动图容易掉色/闪烁；宁可大一点也不动它。
        kwargs["optimize"] = False
    image.save(target, format=fmt, **kwargs)


def _frames_of(image: Any) -> int:
    try:
        value = int(getattr(image, "n_frames", 1) or 1)
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def _is_animated(image: Any) -> bool:
    try:
        return bool(getattr(image, "is_animated", False)) or _frames_of(image) > 1
    except Exception:  # pragma: no cover - 极端坏文件
        return False


def scale_file(
    source: Any,
    *,
    media_id: str,
    settings: ScaleSettings,
    cache_dir: Any,
) -> ScaleResult:
    """给定原图路径与口径，返回**要发出去的那个路径**。

    纯同步、绝不抛：所有失败都收口成 ``scaled=False`` + ``reason``，``path`` 一律
    是可用的原图路径。阻塞调用请用 :func:`scaled_source`（它会挪进线程）。
    """

    started = time.monotonic()
    path = Path(str(source))
    original = str(source)
    source_bytes = _file_size(path)

    def as_is(reason: str, *, width: int = 0, height: int = 0, frames: int = 1) -> ScaleResult:
        return ScaleResult(
            path=original,
            scaled=False,
            reason=reason,
            width=width,
            height=height,
            frames=frames,
            source_bytes=source_bytes,
            output_bytes=source_bytes,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # 1) 开关：关着时行为与改动前**逐字节一致**（原样返回原图路径）。
    if not settings.active:
        return as_is("disabled")
    key = str(media_id or "").strip()
    if not key:
        return as_is("no_media_id")
    if not PILLOW_AVAILABLE or Image is None:  # pragma: no cover - 真机上一定有
        return as_is("pillow_missing")
    if not path.is_file():
        return as_is("source_missing")
    try:
        source_mtime = path.stat().st_mtime
    except OSError:
        return as_is("source_missing")

    try:
        with Image.open(path) as image:
            width, height = (int(image.size[0]), int(image.size[1]))
            fmt = str(image.format or "").upper()
            frames = _frames_of(image)
            animated = _is_animated(image)

            # 2) 动图：先看策略，再看"要不要缩"。
            if animated:
                if settings.animated == ANIMATED_SKIP:
                    return as_is("animated_skip", width=width, height=height, frames=frames)
                if settings.animated == ANIMATED_FIRST:
                    return _first_frame(
                        image=image,
                        path=path,
                        key=key,
                        settings=settings,
                        cache_dir=cache_dir,
                        width=width,
                        height=height,
                        source_bytes=source_bytes,
                        started=started,
                        as_is=as_is,
                    )
                return _keep_animation(
                    image=image,
                    path=path,
                    key=key,
                    fmt=fmt,
                    settings=settings,
                    cache_dir=cache_dir,
                    width=width,
                    height=height,
                    frames=frames,
                    source_bytes=source_bytes,
                    source_mtime=source_mtime,
                    started=started,
                    as_is=as_is,
                )

            # 3) 静态图：长边已经够小就原样发（不复制、不重编码）。
            if max(width, height) <= settings.max_side:
                return as_is("already_small", width=width, height=height)

            suffix = path.suffix.lower()
            out_fmt = _FORMAT_BY_SUFFIX.get(suffix, "PNG")
            out_ext = suffix if suffix in _FORMAT_BY_SUFFIX else ".png"
            cache_path = _cache_path(cache_dir, _cache_name(key, settings.max_side, out_ext))
            if _is_fresh(cache_path, source_mtime):
                return ScaleResult(
                    path=str(cache_path),
                    scaled=True,
                    reason="cache_hit",
                    width=width,
                    height=height,
                    frames=1,
                    source_bytes=source_bytes,
                    output_bytes=_file_size(cache_path),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            work = image.copy()
            work.thumbnail((settings.max_side, settings.max_side), _RESAMPLE)
            out_w, out_h = work.size
            try:
                _atomic_write(cache_path, lambda tmp: _save(work, tmp, out_fmt))
            except OSError:
                return as_is("write_error", width=width, height=height)
            except Exception as exc:
                if isinstance(exc, _Fallback):
                    return as_is(exc.reason, width=width, height=height)
                return as_is("write_error", width=width, height=height)
            out_bytes = _file_size(cache_path)
            # 4) 不划算：缩完比原图还大，就删掉派生物、发原图（下次还会再算一次，
            #    但这种情况本来就少；留着只会让后续每条消息都发更大的图）。
            if out_bytes >= source_bytes > 0:
                with contextlib.suppress(OSError):
                    cache_path.unlink()
                return as_is("no_gain", width=width, height=height)
            return ScaleResult(
                path=str(cache_path),
                scaled=True,
                reason="scaled",
                width=out_w,
                height=out_h,
                frames=1,
                source_bytes=source_bytes,
                output_bytes=out_bytes,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
    except _Fallback as fallback:  # pragma: no cover - as_is 已在内部返回
        return as_is(fallback.reason)
    except Exception:
        return as_is("decode_error")


def _keep_animation(
    *,
    image: Any,
    path: Path,
    key: str,
    fmt: str,
    settings: ScaleSettings,
    cache_dir: Any,
    width: int,
    height: int,
    frames: int,
    source_bytes: int,
    source_mtime: float,
    started: float,
    as_is,
) -> ScaleResult:
    """动图保真：逐帧缩放后按原格式重打包；不划算就回退原图。"""

    if max(width, height) <= settings.max_side:
        return as_is("already_small", width=width, height=height, frames=frames)
    if frames > MAX_ANIMATED_FRAMES:
        return as_is("too_many_frames", width=width, height=height, frames=frames)
    if fmt == "GIF":
        out_fmt, out_ext = "GIF", ".gif"
    elif fmt == "WEBP":
        out_fmt, out_ext = "WEBP", ".webp"
    elif fmt == "PNG":  # APNG：Pillow 用同一套 save_all 接口
        out_fmt, out_ext = "PNG", ".png"
    else:
        return as_is("unsupported_animation", width=width, height=height, frames=frames)

    cache_path = _cache_path(cache_dir, _cache_name(key, settings.max_side, out_ext))
    if _is_fresh(cache_path, source_mtime):
        return ScaleResult(
            path=str(cache_path),
            scaled=True,
            reason="cache_hit",
            width=width,
            height=height,
            frames=frames,
            source_bytes=source_bytes,
            output_bytes=_file_size(cache_path),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    rendered: list[Any] = []
    durations: list[int] = []
    loop = 0
    try:
        loop = int(image.info.get("loop", 0) or 0)
    except (TypeError, ValueError):
        loop = 0
    index = 0
    try:
        while True:
            try:
                image.seek(index)
            except EOFError:
                break
            frame = image.copy()
            if max(frame.size) > settings.max_side:
                frame.thumbnail((settings.max_side, settings.max_side), _RESAMPLE)
            rendered.append(frame)
            try:
                durations.append(int(image.info.get("duration", 0) or 0))
            except (TypeError, ValueError):
                durations.append(0)
            index += 1
            if index > MAX_ANIMATED_FRAMES:
                raise _Fallback("too_many_frames")
            if time.monotonic() - started > MAX_ANIMATED_SECONDS:
                raise _Fallback("too_slow")
    except _Fallback as fallback:
        return as_is(fallback.reason, width=width, height=height, frames=frames)
    except Exception:
        return as_is("decode_error", width=width, height=height, frames=frames)

    if len(rendered) <= 1:
        # 声明是动图、实际只有一帧：当静态图处理，别硬套 save_all。
        single = rendered[0] if rendered else image.copy()
        work = single
        work.thumbnail((settings.max_side, settings.max_side), _RESAMPLE)
        try:
            _atomic_write(cache_path, lambda tmp: _save(work, tmp, "PNG"))
        except Exception:
            return as_is("write_error", width=width, height=height)
        out_bytes = _file_size(cache_path)
        if out_bytes >= source_bytes > 0:
            with contextlib.suppress(OSError):
                cache_path.unlink()
            return as_is("no_gain", width=width, height=height, frames=1)
        return ScaleResult(
            path=str(cache_path),
            scaled=True,
            reason="scaled",
            width=int(work.size[0]),
            height=int(work.size[1]),
            frames=1,
            source_bytes=source_bytes,
            output_bytes=out_bytes,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    head, rest = rendered[0], rendered[1:]
    try:
        _atomic_write(
            cache_path,
            lambda tmp: _save(head, tmp, out_fmt, frames=rest, durations=durations, loop=loop),
        )
    except Exception:
        return as_is("write_error", width=width, height=height, frames=frames)
    elapsed = time.monotonic() - started
    if elapsed > MAX_ANIMATED_SECONDS:
        with contextlib.suppress(OSError):
            cache_path.unlink()
        return as_is("too_slow", width=width, height=height, frames=frames)
    out_bytes = _file_size(cache_path)
    if out_bytes >= source_bytes > 0:
        with contextlib.suppress(OSError):
            cache_path.unlink()
        return as_is("no_gain", width=width, height=height, frames=frames)
    return ScaleResult(
        path=str(cache_path),
        scaled=True,
        reason="scaled",
        width=int(head.size[0]),
        height=int(head.size[1]),
        frames=len(rendered),
        source_bytes=source_bytes,
        output_bytes=out_bytes,
        elapsed_ms=int(elapsed * 1000),
    )


def _first_frame(
    *,
    image: Any,
    path: Path,
    key: str,
    settings: ScaleSettings,
    cache_dir: Any,
    width: int,
    height: int,
    source_bytes: int,
    started: float,
    as_is,
) -> ScaleResult:
    """``animated="first"``：只发首帧——**无论大小**都落一张静态 PNG。

    这一个策略的用户意图是"动图我不要动"，所以即使长边已经够小也要换成本地静图；
    其余路径（小图直接发原图）保持不变。
    """

    cache_path = _cache_path(
        cache_dir, _cache_name(key, settings.max_side, ".png", tag="_first")
    )
    try:
        source_mtime = path.stat().st_mtime
    except OSError:
        return as_is("source_missing", width=width, height=height)
    if _is_fresh(cache_path, source_mtime):
        return ScaleResult(
            path=str(cache_path),
            scaled=True,
            reason="cache_hit",
            width=width,
            height=height,
            frames=1,
            source_bytes=source_bytes,
            output_bytes=_file_size(cache_path),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    try:
        image.seek(0)
        frame = image.copy()
    except Exception:
        return as_is("decode_error", width=width, height=height)
    if max(frame.size) > settings.max_side:
        frame.thumbnail((settings.max_side, settings.max_side), _RESAMPLE)
    try:
        _atomic_write(cache_path, lambda tmp: _save(frame, tmp, "PNG"))
    except Exception:
        return as_is("write_error", width=width, height=height)
    out_bytes = _file_size(cache_path)
    if out_bytes >= source_bytes > 0:
        with contextlib.suppress(OSError):
            cache_path.unlink()
        return as_is("no_gain", width=width, height=height)
    return ScaleResult(
        path=str(cache_path),
        scaled=True,
        reason="scaled",
        width=int(frame.size[0]),
        height=int(frame.size[1]),
        frames=1,
        source_bytes=source_bytes,
        output_bytes=out_bytes,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


async def scaled_source(
    source: Any,
    *,
    media_id: str,
    settings: ScaleSettings,
    cache_dir: Any,
) -> ScaleResult:
    """异步包装：解码/编码是同步 CPU 活，挪进线程，别卡住事件循环。"""

    return await asyncio.to_thread(
        scale_file,
        source,
        media_id=media_id,
        settings=settings,
        cache_dir=cache_dir,
    )


# --------------------------------------------------------------------------- #
# 候选拼图（用户 2026-10-23：find_meme 的候选预览拼成一张图，不再一张张发原图）
# --------------------------------------------------------------------------- #

#: 拼图缓存子目录：``<data_dir>/meme_candidates/``（与 ``meme_sent`` 各管各的）。
CANDIDATE_DIR_NAME = "meme_candidates"

#: 每格长边：跟发送缩图同一个尺度（用户要求 320）。
CANDIDATE_CELL_SIDE = DEFAULT_MAX_SIDE

#: 拼图版本号：布局 / 字体 / 徽标一改就换号，老缓存自然失效。
CANDIDATE_SHEET_VERSION = "v2"

#: 至少几张才值得拼：N ≤ 3 直接发单张缩图（拼图反而更贵，用户 m00080）。
CANDIDATE_SHEET_MIN = 4

#: 一张拼图最多几格：再多的候选只留在文字清单里，标「（未预览）」。
CANDIDATE_SHEET_MAX = 6

#: 格子里画面部分的高度（名字条带另算，避免名字压在画面主体上）。
CANDIDATE_NAME_BAND = 26

#: 攒图评审的拼图（用户 2026-10-24）：一次评审最多两张 3×3，每格只印「候选N」，
#: **不印名字**——名字要等模型自己写，先印上会让它把旧名字配到下一格。
COLLECT_SHEET_SIZE = 9
COLLECT_SHEET_COLUMNS = 3
COLLECT_CELL_SIDE = 256
COLLECT_SHEET_VERSION = "c1"
COLLECT_SHEET_MAX = COLLECT_SHEET_SIZE * 2


@dataclass(frozen=True)
class SheetResult:
    """一次候选拼图的结论：**要看哪个路径** + 为什么。"""

    path: str
    composed: bool = False
    reason: str = ""
    count: int = 0
    columns: int = 0
    rows: int = 0
    width: int = 0
    height: int = 0
    output_bytes: int = 0
    elapsed_ms: int = 0


#: 字体候选：Windows 自带 + 常见 Linux 字体；一个都找不到就用 Pillow 内置位图字体。
_FONT_CANDIDATES = (
    "msyh.ttc",
    "msyhl.ttc",
    "simhei.ttf",
    "simsun.ttc",
    "NotoSansCJK-Regular.ttc",
    "DejaVuSans.ttf",
    "arial.ttf",
)

_font_cache: dict[tuple[str, int], Any] = {}


def _load_font(size: int, *, bold: bool = False) -> Any:
    """按需加载字体；**绝不抛**（找不到就退 Pillow 内置字体，再不行给 None）。"""

    key = ("bold" if bold else "normal", int(size))
    if key in _font_cache:
        return _font_cache[key]
    font: Any = None
    if PILLOW_AVAILABLE:
        from PIL import ImageFont

        names = (
            ("msyhbd.ttc", "simhei.ttf", "arialbd.ttf") + _FONT_CANDIDATES
            if bold
            else _FONT_CANDIDATES
        )
        for name in names:
            try:
                font = ImageFont.truetype(name, int(size))
                break
            except Exception:
                continue
        if font is None:
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
    _font_cache[key] = font
    return font


def _text_length(draw: Any, text: str, font: Any) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except Exception:
        return float(len(text) * 8)


def _ellipsize(draw: Any, text: str, font: Any, max_width: float) -> str:
    body = " ".join(str(text or "").split())
    if not body or max_width <= 0:
        return body
    if _text_length(draw, body, font) <= max_width:
        return body
    trimmed = body
    while trimmed and _text_length(draw, trimmed + "…", font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed or body[:1]) + "…"


def _draw_badge(draw: Any, x: int, y: int, text: str, font: Any) -> None:
    """左上角编号徽标（深底白字）：和文字清单里的「候选N」一一对应。"""

    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    except Exception:
        left = top = 0
        right, bottom = len(text) * 8, 14
    width = int(right - left) + 12
    height = int(bottom - top) + 8
    box = (x, y, x + width, y + height)
    try:
        draw.rounded_rectangle(box, radius=6, fill=(24, 24, 28))
    except Exception:
        draw.rectangle(box, fill=(24, 24, 28))
    draw.text((x + 6 - int(left), y + 4 - int(top)), text, fill=(255, 255, 255), font=font)


def candidate_cache_dir(data_dir: Any) -> Path:
    """``<data_dir>/meme_candidates``（拼图独立缓存目录；没有数据目录就进临时目录）。"""

    return _cache_root(data_dir) / CANDIDATE_DIR_NAME


def candidate_columns(count: int) -> int:
    """几张 → 几列：4 张 2×2；5/6 张 3×2；≤3 张一行（单张缩图的场景不会走到这里）。"""

    number = int(count or 0)
    if number >= 5:
        return 3
    if number == 4:
        return 2
    return max(1, min(number, 3))


def candidate_sheet_name(
    media_ids: Any,
    *,
    cell_side: int = CANDIDATE_CELL_SIDE,
    columns: int,
    names: Any = (),
    version: str = CANDIDATE_SHEET_VERSION,
    suffix: str = ".jpg",
) -> str:
    """缓存键 = 排序前的 media_id 列表 + 每格尺寸 + 列数 + 名字 + 版本号。

    用**列表本身**而不是排序后的：拼图里的格子顺序和文字清单的「候选N」必须
    一一对应，重排会让编号错位（同一批候选顺序稳定，缓存照样命中）。名字进
    键也是必须的：2026-10-23 起名字就印在标签条里（``候选1·无语``），换了名字
    不能还拿旧图。
    """

    parts = [str(item or "").strip() for item in (media_ids or ())]
    labels = [str(item or "").strip() for item in (names or ())]
    token = hashlib.sha1("|".join(parts).encode("utf-8", "ignore")).hexdigest()[:16]
    if labels:
        token += "_" + hashlib.sha1(
            "|".join(labels).encode("utf-8", "ignore")
        ).hexdigest()[:8]
    return (
        f"sheet_{token}_{int(cell_side)}_{int(columns)}"
        f"_{_safe_token(version)}{suffix}"
    )


def compose_candidate_sheet(
    sources: Any,
    *,
    media_ids: Any = (),
    names: Any = (),
    cell_side: int = CANDIDATE_CELL_SIDE,
    columns: int = 0,
    cache_dir: Any = None,
    version: str = CANDIDATE_SHEET_VERSION,
    max_cells: int = CANDIDATE_SHEET_MAX,
    start_index: int = 1,
) -> SheetResult:
    """把 N 张缩图拼成一张网格图（同步；**绝不抛**，失败给 ``composed=False``）。

    * 每格 **正方形**：先 center-crop 成正方形再缩放，四格必然等高，长条图不会
      把整行撑高（用户 2026-10-23）；
    * 左上角一条黑色标签条写编号 **和名字**：``候选1·无语``（名字不再单独印在
      格子下方——那样模型会把名字配到下一格）；
    * 缓存命中直接复用；任何异常都返回 ``composed=False``，调用方退回"只发文字清单"。

    ``max_cells`` / ``start_index`` 是攒图评审（用户 2026-10-24）加的两个口子：
    一次评审两张 3×3 时，第二张要 ``max_cells=9, start_index=10``，格子上的编号才是
    「候选10..候选18」而不会从 1 重新数（文字清单与拼图必须编号一致）。
    """

    started = time.monotonic()
    paths = [str(item or "").strip() for item in (sources or ())]
    keys = [str(item or "").strip() for item in (media_ids or ())]
    labels = [str(item or "").strip() for item in (names or ())]
    offset = max(0, int(start_index) - 1)
    count = min(len(paths), max(1, int(max_cells or CANDIDATE_SHEET_MAX)))
    if count <= 0:
        return SheetResult("", reason="no_source")
    if not PILLOW_AVAILABLE:
        return SheetResult("", reason="pillow_missing")
    cols = int(columns or 0) or candidate_columns(count)
    cols = max(1, min(cols, count))
    rows = (count + cols - 1) // cols
    cell = max(MIN_MAX_SIDE, min(int(cell_side or CANDIDATE_CELL_SIDE), MAX_MAX_SIDE))
    width = cols * cell
    height = rows * cell
    try:
        target = _cache_path(
            cache_dir,
            candidate_sheet_name(
                keys[:count] or paths[:count],
                cell_side=cell,
                columns=cols,
                names=labels[:count],
                version=version if offset == 0 else f"{version}s{offset + 1}",
            ),
        )
    except Exception as exc:  # pragma: no cover - 极端坏 cache_dir
        return SheetResult("", reason=f"cache_error:{type(exc).__name__}", count=count)
    newest = 0.0
    for path in paths[:count]:
        try:
            newest = max(newest, Path(path).stat().st_mtime)
        except OSError:
            continue
    if _is_fresh(target, newest):
        return SheetResult(
            str(target),
            composed=True,
            reason="cache_hit",
            count=count,
            columns=cols,
            rows=rows,
            width=width,
            height=height,
            output_bytes=_file_size(target),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    try:
        from PIL import ImageDraw, ImageOps
    except Exception as exc:  # pragma: no cover - PILLOW_AVAILABLE 已经挡过
        return SheetResult("", reason=f"pillow_missing:{type(exc).__name__}", count=count)
    try:
        canvas = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)
        badge_font = _load_font(max(12, cell // 16), bold=True)
        name_font = _load_font(max(11, CANDIDATE_NAME_BAND - 10))
        for index, path in enumerate(paths[:count]):
            column = index % cols
            row = index // cols
            x = column * cell
            y = row * cell
            draw.rectangle(
                (x, y, x + cell - 1, y + cell - 1), fill=(244, 244, 246)
            )
            frame = None
            try:
                with Image.open(path) as raw:
                    frame = ImageOps.exif_transpose(raw).convert("RGB")
                    # 用户 2026-10-23：每格先 center-crop 成正方形再缩放，
                    # 四格等高、长条图不撑高整行。
                    side_px = min(frame.width, frame.height)
                    if side_px > 0 and (frame.width, frame.height) != (side_px, side_px):
                        left = (frame.width - side_px) // 2
                        top = (frame.height - side_px) // 2
                        frame = frame.crop((left, top, left + side_px, top + side_px))
                    inner = max(1, cell - 8)
                    frame = frame.resize((inner, inner), _RESAMPLE)
            except Exception:
                frame = None
            if frame is not None:
                canvas.paste(
                    frame,
                    (x + (cell - frame.width) // 2, y + (cell - frame.height) // 2),
                )
            else:
                draw.text(
                    (x + cell // 2, y + cell // 2),
                    "预览失败",
                    fill=(150, 150, 150),
                    font=name_font,
                    anchor="mm",
                )
            name = labels[index] if index < len(labels) else ""
            badge_text = f"候选{offset + index + 1}"
            if name:
                badge_text += f"·{name}"
            badge_text = _ellipsize(draw, badge_text, badge_font, cell - 24)
            _draw_badge(draw, x + 6, y + 6, badge_text, badge_font)
        _atomic_write(target, lambda tmp: _save(canvas, tmp, "JPEG"))
    except Exception as exc:
        return SheetResult(
            "",
            reason=f"write_error:{type(exc).__name__}",
            count=count,
            columns=cols,
            rows=rows,
        )
    return SheetResult(
        str(target),
        composed=True,
        reason="composed",
        count=count,
        columns=cols,
        rows=rows,
        width=width,
        height=height,
        output_bytes=_file_size(target),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


async def composed_sheet_source(
    sources: Any,
    *,
    media_ids: Any = (),
    names: Any = (),
    cell_side: int = CANDIDATE_CELL_SIDE,
    columns: int = 0,
    cache_dir: Any = None,
) -> SheetResult:
    """异步包装：拼图是同步 CPU 活，挪进线程。"""

    return await asyncio.to_thread(
        compose_candidate_sheet,
        sources,
        media_ids=media_ids,
        names=names,
        cell_side=cell_side,
        columns=columns,
        cache_dir=cache_dir,
    )


def compose_collect_sheet(
    sources: Any,
    *,
    media_ids: Any = (),
    start_index: int = 1,
    cell_side: int = COLLECT_CELL_SIDE,
    cache_dir: Any = None,
) -> SheetResult:
    """攒图评审的拼图入口（3×3、每格只印「候选N」；同步，**绝不抛**）。

    与 :func:`compose_candidate_sheet` 的差别只有三处：默认 9 格、默认 3 列、**不带
    名字**（名字是模型这次评审要写的东西，先印旧的会错配）。第二张拼图用
    ``start_index=10`` 接着数。
    """

    return compose_candidate_sheet(
        sources,
        media_ids=media_ids,
        names=(),
        cell_side=cell_side,
        columns=COLLECT_SHEET_COLUMNS,
        cache_dir=cache_dir,
        version=COLLECT_SHEET_VERSION,
        max_cells=COLLECT_SHEET_SIZE,
        start_index=start_index,
    )


async def composed_collect_sheet(
    sources: Any,
    *,
    media_ids: Any = (),
    start_index: int = 1,
    cell_side: int = COLLECT_CELL_SIDE,
    cache_dir: Any = None,
) -> SheetResult:
    """异步包装：评审拼图也挪进线程，绝不卡回复链路。"""

    return await asyncio.to_thread(
        compose_collect_sheet,
        sources,
        media_ids=media_ids,
        start_index=start_index,
        cell_side=cell_side,
        cache_dir=cache_dir,
    )


__all__ = [
    "ANIMATED_FIRST",
    "ANIMATED_KEEP",
    "ANIMATED_MODES",
    "ANIMATED_SKIP",
    "CACHE_DIR_NAME",
    "CANDIDATE_CELL_SIDE",
    "CANDIDATE_DIR_NAME",
    "CANDIDATE_NAME_BAND",
    "CANDIDATE_SHEET_MAX",
    "CANDIDATE_SHEET_MIN",
    "CANDIDATE_SHEET_VERSION",
    "COLLECT_CELL_SIDE",
    "COLLECT_SHEET_COLUMNS",
    "COLLECT_SHEET_MAX",
    "COLLECT_SHEET_SIZE",
    "COLLECT_SHEET_VERSION",
    "DEFAULT_MAX_SIDE",
    "MAX_ANIMATED_FRAMES",
    "MAX_ANIMATED_SECONDS",
    "MAX_MAX_SIDE",
    "MIN_MAX_SIDE",
    "PILLOW_AVAILABLE",
    "PILLOW_PROBLEM",
    "PROBLEM_REASONS",
    "GIF_SUFFIX",
    "REASON_TEXTS",
    "gif_file",
    "gif_source",
    "ScaleResult",
    "ScaleSettings",
    "SheetResult",
    "candidate_cache_dir",
    "candidate_columns",
    "candidate_sheet_name",
    "compose_candidate_sheet",
    "compose_collect_sheet",
    "composed_collect_sheet",
    "composed_sheet_source",
    "reason_text",
    "scale_cache_dir",
    "scale_file",
    "scale_settings",
    "scaled_source",
]
