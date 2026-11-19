"""M5 图片事实生产者：真尺寸 / 真帧数 / 感知哈希 / 按需取帧。

**为什么单独一个模块**：``meme_assets`` 从 M1 起就有 ``width`` / ``height`` /
``frame_count`` 三列，但一直没有产出者——``core.meme_store.asset_facts`` 只填
hash / mime / size / is_animated，于是卡片与详情里的「尺寸 / 帧数」恒为 0，
动图也只能按扩展名猜（方案 §10 M5 的活）。这里就是那个产出者。

四条硬规矩（本模块逐条兑现）：

1. **可选依赖，优雅降级**：Pillow 是宿主自带依赖，但按可选 import 处理。没装就
   一切回 ``ok=False`` + ``reason="pillow_missing"``，**绝不**拿扩展名 / 字节数 /
   文件大小编造尺寸（方案 §1「判定要有出处，不编造」）；调用方在探不到时回落到
   老口径（按 mime 猜 ``is_animated``）。
2. **绝不抛**：坏图、截断、超大、解压炸弹全部在函数内收口成 ``ok=False`` + 原因，
   调用方拿不到异常。
3. **不整图解码**：帧数用 ``seek`` 数（元数据 ``n_frames`` 会撒谎——截断的 GIF
   照样报一个漂亮的帧数），取帧只 ``seek`` 目标帧一次、只 ``load()`` 一次，
   任何时刻内存里只有一帧。
4. **判据带阈值与出处**：见 :data:`DHASH_MAX_DISTANCE` / :data:`PIXEL_HASH_SIZE`
   / :data:`DEFAULT_MAX_PIXELS` 的注释。变体判定只判"像"，**绝不判"是同一张"**
   （方案 §1：「保留变体关系，绝不静默合并」）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from io import BytesIO
from typing import Any

try:  # 可选依赖：宿主自带，但缺了也必须能跑（见模块 docstring 第 1 条）
    from PIL import Image

    PILLOW_AVAILABLE = True
    PILLOW_PROBLEM = ""
except Exception as exc:  # pragma: no cover - 真机上 Pillow 由宿主提供
    Image = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False
    PILLOW_PROBLEM = f"{type(exc).__name__}: {exc}"

# --------------------------------------------------------------------------- #
# 常量：闸门与判据（每一个都写清出处）
# --------------------------------------------------------------------------- #

#: 默认单张字节上限——与 ``world.meme.max_asset_bytes`` 的默认值同源（10MB）。
DEFAULT_MAX_BYTES = 10_000_000

#: 默认解码帧上限——与 ``world.meme.max_asset_frames`` 的默认值同源（200）。
DEFAULT_MAX_FRAMES = 200

#: 解压炸弹闸门（像素数）：Pillow 自带的 ``Image.MAX_IMAGE_PIXELS`` 是 ~8900 万，
#: 对聊天表情包来说太宽（8900 万像素 × 4 字节 = 356MB 一次解码）。这里取 4000 万
#: 像素（约 6300×6300）作为"表情包不可能这么大"的界，超了一律 ``ok=False``，
#: **而且在 ``load()`` 之前就拒**（尺寸来自文件头，不解码）。
DEFAULT_MAX_PIXELS = 40_000_000

#: dHash 的边长：8×8 相邻比较 = 64 位（``imagehash`` 库的 ``hash_size=8`` 惯例）。
DHASH_SIZE = 8
DHASH_BITS = DHASH_SIZE * DHASH_SIZE

#: 64 位 dHash 的汉明距离阈值：**≤ 6 位**算"画面近似"。
#: 出处：``imagehash`` 生态的通行口径（64 位差值哈希，距离 ≤ 5~10 视为相似；
#: 距离 0 才是同一张）。取 6 是保守中值——比 10 严（少登记噪声关系），
#: 比 5 宽（容得下重压缩带来的抖动）。调用方可用 ``dhash_max_distance=`` 覆盖。
DHASH_MAX_DISTANCE = 6

#: 规范化解码像素哈希的采样尺寸：先解码成 RGB、再缩到 32×32、再取 sha256。
#: 出处：方案 §1「相同画面（规范化解码像素哈希）」——同一画面换编码（PNG↔无损
#: WebP）解码后像素一致 → 32×32 采样一致 → 哈希一致；有损重压（JPEG）会变值，
#: 那种情况归 dHash 的"近似"，不是"相同"。阈值是**逐字节相等**，没有容差。
PIXEL_HASH_SIZE = (32, 32)

#: 取帧输出的格式（卡片 / 内容判定都要能直接塞进附件）。
EXTRACT_MIME = "image/png"

#: Pillow 的格式名 → mime。**不用** ``Image.MIME``：那个注册表要 ``Image.init()``
#: 之后才有内容（没初始化时全空），真机上会给出空 mime。
_FORMAT_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "JPG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "ICO": "image/x-icon",
    "AVIF": "image/avif",
}

#: 原因码 → 中文（日志与页面共用一份）
PROBE_REASON_TEXTS: dict[str, str] = {
    "": "读到了",
    "empty": "没有字节可读（文件是空的）",
    "too_large": "单张超过字节上限，没有解码",
    "too_many_pixels": "像素数超过闸门，没有解码",
    "pillow_missing": "环境里没有 Pillow，读不出尺寸与帧数",
    "decode_error": "这个文件不是能解码的图片",
    "truncated": "文件被截断了，首帧都读不出来",
    "truncated_frames": "文件尾部被截断：尺寸是真的，帧数只是数得出来的下界",
    "out_of_range": "要的那一帧不在这张图里",
    "no_bytes": "资产库里没有这张图的字节",
}

#: Pillow 在"被截断 / 结构不完整"的文件上抛的几种典型异常文本。命中就当
#: ``truncated``（比笼统的 ``decode_error`` 精确，回执里能说清是"文件坏了"还是
#: "根本不是图片"）。
_TRUNCATED_MARKERS = (
    "truncat",
    "not processed",
    "unexpected eof",
    "eof",
    "unpack_from",
    "out of range",
    "buffer",
)

#: 变体判定原因码 → 中文
VARIANT_REASON_TEXTS: dict[str, str] = {
    "same_picture": "同一画面、不同编码",
    "same_first_frame_frames_differ": "首帧相同但帧数不同",
    "near_picture": "画面近似（感知哈希接近）",
    "near_picture_frames_differ": "首帧近似但帧数不同",
    "different_picture": "画面差得远",
    "flat_no_gradient": "画面没有梯度（纯色块），感知哈希判不了",
    "same_media": "根本是同一份字节",
    "no_profile": "缺少可比的图片事实（没解码出来）",
    "bad_media": "内容身份为空",
}

_SAME_MEDIA = "same_media"

_RESAMPLE = getattr(Image, "LANCZOS", 1) if PILLOW_AVAILABLE else 1


def _reason_text(reason: str) -> str:
    """原因码 → 中文（未知码原样回，不吞掉）。"""

    return PROBE_REASON_TEXTS.get(reason, reason or "")


def _looks_truncated(text: str) -> bool:
    """异常文本像不像"文件被截断 / 结构不完整"（见 :data:`_TRUNCATED_MARKERS`）。"""

    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _TRUNCATED_MARKERS)


def decoder_available() -> bool:
    """环境里有没有可用的图片解码器（页面用它显示"尺寸/帧数能不能读"）。"""

    return PILLOW_AVAILABLE


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #


def _open_image(data: Any) -> Any:
    """开一个图片解码器（惰性：尺寸来自文件头，像素等 ``load()`` 才解）。

    单独一个函数是为了**可替换**：测试把它换成"记账的包装"，用来证明取帧只
    ``seek`` 一次、只 ``load`` 一次（"别把整个 GIF 解进内存里"这条要能被验，
    不能只靠注释保证）。
    """

    return Image.open(BytesIO(bytes(data)))


def _blank_facts(size: int = 0) -> dict[str, Any]:
    """一份"什么都还不知道"的事实骨架（键一个不少，调用方不用 ``in`` 判断）。"""

    return {
        "ok": False,
        "reason": "",
        "reason_text": "",
        "size_bytes": int(size or 0),
        "mime": "",
        "format": "",
        "width": 0,
        "height": 0,
        "frame_count": 0,
        "frames_probed": 0,
        "frame_count_capped": False,
        "is_animated": False,
        "truncated": False,
        "pixel_hash": "",
        "dhash": "",
        "dhash_flat": False,
        "decode_error": "",
    }


def _fail(facts: dict[str, Any], reason: str, detail: str = "") -> dict[str, Any]:
    """把一份事实标成失败（原因码 + 中文 + 可选底层异常文本）。"""

    facts["ok"] = False
    facts["reason"] = reason
    facts["reason_text"] = _reason_text(reason)
    if detail:
        facts["decode_error"] = detail
    return facts


def _walk_frames(image: Any, declared: int, cap: int) -> tuple[int, bool]:
    """``seek`` 数帧：返回 ``(数出来的帧数, 是否被截断)``。**不 load**。

    为什么不直接信 ``n_frames``：截断的 GIF 会报一个比真实帧数大的漂亮数字，
    照抄就是编造。这里的做法是逐帧 ``seek``（只移动解码位置、不取像素）：

    * ``EOFError`` = "没有下一帧了"，正常到底；
    * 别的异常（``OSError: image file is truncated``）= 文件真的坏了，标 ``truncated``；
    * 数满 ``cap`` 就收工并标 ``frame_count_capped``（配置的上限生效，且如实说）；
    * 数完之后**多探一帧**：完整文件在这里抛 ``EOFError``，被截断的文件抛
      ``OSError``——这是把"正常结束"和"尾部损坏"分开的唯一可靠办法（实测：
      只砍掉 GIF 尾字节时，前 N 帧的 ``seek`` 全部成功）。
    """

    limit = int(cap) if int(cap) > 0 else max(1, int(declared))
    target = min(int(declared), limit) if int(declared) > 0 else limit
    counted = 0
    try:
        for index in range(target):
            image.seek(index)
            counted += 1
    except EOFError:
        pass
    except Exception:
        return counted, True
    if counted < limit:
        try:
            image.seek(counted)
        except EOFError:
            pass
        except Exception:
            return counted, True
    return counted, False


def _hashes_from_frame(image: Any) -> dict[str, Any]:
    """已经解到某一帧的图片 → ``{pixel_hash, dhash, dhash_flat}``（纯计算）。

    * ``pixel_hash``：RGB → 32×32（LANCZOS）→ sha256。同画面换编码一致。
    * ``dhash``：灰度 → 9×8 → 每行相邻像素比较，64 位，十六进制串。
    * ``dhash_flat``：dHash 全是 0 或全是 1 = **这张图没有横向梯度**（纯色块、
      单色背景）。dHash 是"梯度方向"的哈希，对纯色图它不带任何信息——两张颜色
      完全不同的纯色图都会算出全 0。标出来，变体判定据此**拒绝用 dHash 下结论**
      （宁可说"判不了"，也不许拿一个恒为 0 的哈希说"这两张像"）。
    """

    try:
        rgb = image.convert("RGB").resize(PIXEL_HASH_SIZE, _RESAMPLE)
        pixel_hash = hashlib.sha256(rgb.tobytes()).hexdigest()
        gray = image.convert("L").resize((DHASH_SIZE + 1, DHASH_SIZE), _RESAMPLE)
        bits = 0
        for row in range(DHASH_SIZE):
            for column in range(DHASH_SIZE):
                bits = (bits << 1) | (
                    1 if gray.getpixel((column, row)) > gray.getpixel((column + 1, row)) else 0
                )
        full = (1 << DHASH_BITS) - 1
        return {
            "pixel_hash": pixel_hash,
            "dhash": f"{bits:0{DHASH_BITS // 4}x}",
            "dhash_flat": bits in (0, full),
        }
    except Exception:
        return {"pixel_hash": "", "dhash": "", "dhash_flat": False}


def probe_bytes(
    data: Any,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_frames: int = DEFAULT_MAX_FRAMES,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    hashes: bool = False,
) -> dict[str, Any]:
    """一份图片字节 → 真实事实（尺寸 / 帧数 / 动图 / 可选哈希）。

    ``ok=True`` 的含义是"尺寸与帧数是数出来的真值"；``ok=False`` 一律带原因码，
    且**一个尺寸都不编**：没打开成功的文件 ``width`` / ``height`` 保持 0。两类
    "部分成功"分开报：

    * 尾部被截断的多帧图：``ok=True`` + ``truncated=True`` +
      ``reason="truncated_frames"``——尺寸是文件头里的真值、帧数是数得出来的下界，
      比整条判失败更有用，也绝不谎报一个完整帧数；
    * 首帧都读不出来的：``ok=False`` + ``reason="truncated"``。**注意**：这种情况下
      ``width`` / ``height`` 可能仍然是**文件头里读出来的真值**（信息本身是真的，
      不是编的），但 ``ok=False`` 就表示"这份事实不完整、不许落库"——调用方只许在
      ``ok=True`` 时写 ``meme_assets``（``MemeStore.backfill_asset_facts`` 就是这么判的）。

    ``hashes=True`` 时顺带算 ``pixel_hash`` / ``dhash``（多一次首帧 resize，仍只
    ``load()`` 一次）；变体判定要它们，普通资产事实不需要。
    """

    facts = _blank_facts(len(data) if isinstance(data, (bytes, bytearray)) else 0)
    if not data:
        return _fail(facts, "empty")
    if max_bytes and len(data) > int(max_bytes):
        return _fail(facts, "too_large")
    if not PILLOW_AVAILABLE:
        return _fail(facts, "pillow_missing", PILLOW_PROBLEM)
    image = None
    try:
        image = _open_image(data)
        fmt = str(getattr(image, "format", "") or "").upper()
        try:
            width, height = int(image.size[0]), int(image.size[1])
        except Exception:
            width, height = 0, 0
        if width <= 0 or height <= 0:
            return _fail(facts, "decode_error", "文件头里没有可用尺寸")
        if max_pixels and width * height > int(max_pixels):
            # 尺寸是从文件头读的，没解码；这里就拒，别把 4 亿像素解进内存
            facts["width"], facts["height"] = width, height
            facts["format"] = fmt
            return _fail(facts, "too_many_pixels")
        facts["mime"] = _FORMAT_MIME.get(fmt, "")
        facts["format"] = fmt
        facts["width"], facts["height"] = width, height
        declared = int(getattr(image, "n_frames", 1) or 1)
        counted, truncated = _walk_frames(image, declared, int(max_frames or 0))
        facts["frame_count"] = counted
        facts["frames_probed"] = counted
        facts["truncated"] = bool(truncated)
        facts["frame_count_capped"] = bool(not truncated and declared > counted)
        facts["is_animated"] = counted > 1
        # 首帧必须真的解得开：截断到首帧都坏的文件在这里露馅（不抛，转成原因码）
        try:
            image.seek(0)
            frame = image.convert("RGB")
            frame.load()
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"
            return _fail(
                facts,
                "truncated" if _looks_truncated(text) else "decode_error",
                text,
            )
        if hashes:
            facts.update(_hashes_from_frame(image))
        facts["ok"] = True
        if truncated:
            facts["reason"] = "truncated_frames"
            facts["reason_text"] = _reason_text("truncated_frames")
        return facts
    except Exception as exc:
        return _fail(
            facts,
            "truncated" if _looks_truncated(f"{type(exc).__name__}: {exc}") else "decode_error",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        # 关掉解码器持有的文件句柄（不关的话 Windows 上临时文件删不掉）
        try:
            if image is not None:
                image.close()
        except Exception:  # pragma: no cover - 关不掉不该影响事实
            pass


def image_profile(data: Any, **kwargs: Any) -> dict[str, Any]:
    """``probe_bytes(hashes=True)`` 的别名：变体判定要的那份"图片画像"。

    单独给个名字是因为调用点的意图不同（一个要"资产事实"、一个要"像不像"），
    但两者必须是同一份事实——分两个实现早晚会漂。
    """

    kwargs["hashes"] = True
    return probe_bytes(data, **kwargs)


def middle_frame_index(frame_count: Any) -> int:
    """「中间帧做内容判定」取哪一帧（``frame_count<=1`` 就是第 0 帧）。

    为什么不用第 1 帧：GIF 的首帧常常是空白 / 起手式，中间帧才代表这张图在说什么
    （方案 §10 M5「GIF 不按第一帧判同一」的同一件事）。
    """

    try:
        count = int(frame_count or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 1:
        return 0
    return count // 2


def extract_frame(
    data: Any,
    index: Any = 0,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_frames: int = DEFAULT_MAX_FRAMES,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    fmt: str = "PNG",
) -> dict[str, Any]:
    """只取指定的一帧 → PNG 字节（**只 seek 一次、只 load 一次**）。

    帧号用**文件头里的** ``n_frames`` 校验（不为了校验去把整条动图遍历一遍——
    那正是"别把整个 GIF 解进内存里"要避免的）；元数据撒谎时 ``seek`` 自己会抛，
    这里照样转成 ``ok=False`` + 原因。

    返回 ``{"ok","reason","reason_text","index","frame_count","frames_declared",
    "width","height","mime","bytes","size_bytes","is_animated"}``。
    """

    result: dict[str, Any] = {
        "ok": False,
        "reason": "",
        "reason_text": "",
        "index": 0,
        "frame_count": 0,
        "frames_declared": 0,
        "width": 0,
        "height": 0,
        "mime": EXTRACT_MIME,
        "bytes": b"",
        "size_bytes": 0,
        "is_animated": False,
    }
    if not data:
        result.update(reason="empty", reason_text=_reason_text("empty"))
        return result
    if max_bytes and len(data) > int(max_bytes):
        result.update(reason="too_large", reason_text=_reason_text("too_large"))
        return result
    if not PILLOW_AVAILABLE:
        result.update(reason="pillow_missing", reason_text=_reason_text("pillow_missing"))
        return result
    try:
        wanted = int(index)
    except (TypeError, ValueError):
        wanted = 0
    image = None
    try:
        image = _open_image(data)
        width, height = int(image.size[0]), int(image.size[1])
        if max_pixels and width * height > int(max_pixels):
            result.update(
                reason="too_many_pixels",
                reason_text=_reason_text("too_many_pixels"),
                width=width,
                height=height,
            )
            return result
        declared = int(getattr(image, "n_frames", 1) or 1)
        result.update(
            width=width,
            height=height,
            frames_declared=declared,
            frame_count=declared,
            is_animated=declared > 1,
        )
        if wanted < 0 or wanted >= declared:
            result.update(reason="out_of_range", reason_text=_reason_text("out_of_range"))
            return result
        if wanted:
            image.seek(wanted)
        frame = image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB")
        frame.load()
        buffer = BytesIO()
        frame.save(buffer, format=str(fmt or "PNG"))
        payload = buffer.getvalue()
        result.update(ok=True, index=wanted, bytes=payload, size_bytes=len(payload))
        return result
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        result.update(
            reason="truncated" if _looks_truncated(text) else "decode_error",
            reason_text="",
            index=wanted,
        )
        result["reason_text"] = _reason_text(str(result["reason"]))
        result["decode_error"] = text
        return result
    finally:
        try:
            if image is not None:
                image.close()
        except Exception:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# 变体判据（纯函数）
# --------------------------------------------------------------------------- #


def _hash_int(value: Any) -> int | None:
    """十六进制串 / 整数 → 64 位整数；认不出来回 ``None``（**不猜**）。"""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 0 <= value < (1 << DHASH_BITS) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text, 16)
    except ValueError:
        return None
    return number if 0 <= number < (1 << DHASH_BITS) else None


def hamming_distance(left: Any, right: Any) -> int:
    """两个 dHash 的汉明距离；任一边认不出来回满距离 :data:`DHASH_BITS`（= 判不了）。

    缺哈希按"最远"算是**保守方向**：宁可漏登记一个变体，也不许凭半个哈希
    说"这两张像"（方案 §1 的铁律）。
    """

    first = _hash_int(left)
    second = _hash_int(right)
    if first is None or second is None:
        return DHASH_BITS
    return int(bin(first ^ second).count("1"))


def _profile_ready(profile: Any) -> bool:
    """这份画像够不够判变体：要有能解码的事实 + 至少一个哈希。"""

    if not isinstance(profile, Mapping):
        return False
    if not profile.get("ok"):
        return False
    return bool(str(profile.get("pixel_hash") or "") or str(profile.get("dhash") or ""))


def variant_decision(
    left: Any,
    right: Any,
    *,
    dhash_max_distance: int = DHASH_MAX_DISTANCE,
) -> dict[str, Any]:
    """两份图片画像 → 一条"疑似变体"判定（纯函数，**只判像、绝不判是同一张**）。

    判据（全部是常量，改一处就够）：

    1. 任一边没画像（没解码出来 / 没哈希）→ ``relation=""、reason="no_profile"``，
       **不猜**；
    2. 同一个 ``media_id`` → ``same_media``（相同字节已经是同一条收藏，谈不上变体）；
    3. **规范化解码像素哈希相等** → ``reason="same_picture"``：同一画面换了编码
       （PNG↔无损 WebP），相似度记 1.0；
    4. 否则看 64 位 dHash 的汉明距离：``<= dhash_max_distance`` → ``near_picture``
       （相似度 = ``1 - 距离/64``）；超过 → ``different_picture``，不登记；
       任一边的 dHash **没有梯度**（``dhash_flat``，纯色图）→ ``flat_no_gradient``，
       **不登记**：一个恒为 0 的哈希不带信息，拿它下结论就是编；
    5. **帧数不同**永远是单独一条线索：上面 3/4 判出来的关系会保留，但
       ``same_picture`` 强制为 ``False``、``first_frame_same`` 记首帧是否一致，
       并在 ``diff_note`` 里写明"帧数不同（A vs B）——绝不按第一帧判同一"。
       这就是方案 §10 M5 的验收点：**GIF 不按第一帧判同一**。

    返回 ``{"relation","similarity","reason","reason_text","same_picture",
    "first_frame_same","frames_differ","dhash_distance","basis","diff_note"}``：
    ``relation`` 只有 ``"suspected"`` / ``""`` 两种——``confirmed`` 只能人工写
    （见 ``MemeStore.confirm_variant``），机器不许自己确认。
    """

    decision: dict[str, Any] = {
        "relation": "",
        "similarity": 0.0,
        "reason": "",
        "reason_text": "",
        "same_picture": False,
        "first_frame_same": False,
        "frames_differ": False,
        "dhash_distance": -1,
        "basis": "",
        "diff_note": "",
    }
    if not _profile_ready(left) or not _profile_ready(right):
        decision["reason"] = "no_profile"
        decision["reason_text"] = VARIANT_REASON_TEXTS["no_profile"]
        return decision
    left_id = str(left.get("media_id") or "")
    right_id = str(right.get("media_id") or "")
    if left_id and left_id == right_id:
        decision["reason"] = _SAME_MEDIA
        decision["reason_text"] = VARIANT_REASON_TEXTS[_SAME_MEDIA]
        return decision

    left_frames = int(left.get("frame_count") or 0)
    right_frames = int(right.get("frame_count") or 0)
    frames_differ = bool(left_frames and right_frames and left_frames != right_frames)
    left_pixel = str(left.get("pixel_hash") or "")
    right_pixel = str(right.get("pixel_hash") or "")
    first_frame_same = bool(left_pixel and left_pixel == right_pixel)
    distance = hamming_distance(left.get("dhash"), right.get("dhash"))
    decision["dhash_distance"] = distance
    decision["first_frame_same"] = first_frame_same
    decision["frames_differ"] = frames_differ
    decision["similarity"] = max(0.0, 1.0 - (distance / float(DHASH_BITS)))

    if first_frame_same:
        decision.update(relation="suspected", similarity=1.0, basis="pixel_hash")
        decision["same_picture"] = not frames_differ
        decision["diff_note"] = "同一画面、不同编码（规范化解码像素哈希一致）"
        decision["reason"] = (
            "same_first_frame_frames_differ" if frames_differ else "same_picture"
        )
    elif bool(left.get("dhash_flat")) or bool(right.get("dhash_flat")):
        # 纯色图：dHash 恒为 0（或全 1），它区分不了两张不同颜色的纯色图。
        # 这时候"判不了"才是唯一诚实的结论——不登记任何关系。
        decision["reason"] = "flat_no_gradient"
    elif distance <= int(dhash_max_distance):
        decision.update(relation="suspected", basis="dhash")
        decision["diff_note"] = (
            f"画面近似（感知哈希距离 {distance}/{DHASH_BITS}）："
            "可能是配字 / 裁剪 / 画质差异，**没判成同一张**"
        )
        decision["reason"] = (
            "near_picture_frames_differ" if frames_differ else "near_picture"
        )
    else:
        decision["reason"] = "different_picture"
    if decision["relation"] and frames_differ:
        decision["same_picture"] = False
        decision["diff_note"] += (
            f"；帧数不同（{left_frames} vs {right_frames}）——"
            "绝不按第一帧判同一，只留关系、不合并"
        )
    decision["reason_text"] = VARIANT_REASON_TEXTS.get(
        str(decision["reason"]), str(decision["reason"])
    )
    return decision


__all__ = [
    "DHASH_BITS",
    "DHASH_MAX_DISTANCE",
    "DHASH_SIZE",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_FRAMES",
    "DEFAULT_MAX_PIXELS",
    "EXTRACT_MIME",
    "PILLOW_AVAILABLE",
    "PILLOW_PROBLEM",
    "PIXEL_HASH_SIZE",
    "PROBE_REASON_TEXTS",
    "VARIANT_REASON_TEXTS",
    "decoder_available",
    "extract_frame",
    "hamming_distance",
    "image_profile",
    "middle_frame_index",
    "probe_bytes",
    "variant_decision",
]
