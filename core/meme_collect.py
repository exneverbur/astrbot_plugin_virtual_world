"""攒一批「她看到过的图」、拼图、一次评审收进收藏（用户 2026-10-24）。

为什么不是"每张图都问她一句"：那样每张图都要弹一次清单、占一次注意力，而且真正
好笑的图往往要过一会儿才想起来收。所以这里换成：

1. **攒**：她看到过的每张图（群聊、私聊都算）按 ``batch_probability`` 进候选池；
2. **等**：攒够 ``min_batch`` 张、且距上次评审超过 ``review_interval_hours``、
   今天还没到 ``max_reviews_per_day``，才在空闲 tick 里评审；
3. **一次调用**：两张 3×3 拼图 + 每行 20 字的文字清单，交给她一个
   ``collect_meme_batch`` 动作，一次决定这一批收哪些（可以一个都不要）；
4. **收尾**：整批候选一次性置 ``collected`` / ``passed``；``collected`` 永久排除，
   ``passed`` 超过 ``candidate_ttl_days`` 且她再次看到时可以重新入池一次（见
   ``note_seen``），不是永久否决。

聊天里的即时收藏（``collect_meme`` + ``图N``）**不受这里影响**：有人让她收，她照收。
两条路径写同一张 ``meme_entries``，靠既有的 ``media_id`` 唯一索引去重。

本模块只做判断与文本，不碰提示词模板（在 ``core/prompt.py``）、不碰回复链路
（在 ``core/engine.py``）；所有异常都不该往上抛——评审是后台事，坏了最多这一批不收。
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .db import (
    COLLECT_STATUS_COLLECTED,
    COLLECT_STATUS_PASSED,
    COLLECT_STATUS_PENDING,
)
from .meme_scale import (
    COLLECT_CELL_SIDE,
    COLLECT_SHEET_MAX,
    COLLECT_SHEET_SIZE,
    SheetResult,
    candidate_cache_dir,
    composed_collect_sheet,
)

from .host_log import logger  # noqa: PLC2701

#: 评审那一次内部调用用的动作名（主回复轮里没有它）。
COLLECT_BATCH_ACTION_ID = "collect_meme_batch"

#: 候选编号前缀：与「图N」严格分开——候选不是对方刚发的图。
COLLECT_REF_PREFIX = "候选"

#: 一次评审最多几张拼图（用户：最多两张 3×3）。
MAX_SHEETS = 2

#: 文字清单每行最多几个字（用户明确嫌长清单费注意力）。
SHORT_CAPTION_CHARS = 20

#: 模型写回来的字段长度上限（超了截断，不拒绝——她写得不好也比丢掉整批好）。
MAX_NAME_CHARS = 20
MAX_TEXT_CHARS = 60

#: 库存反馈只回看最近这么多秒的发送记录（"最近发得最多的是哪类"）。
INVENTORY_WINDOW_SECONDS = 30 * 86400

SOURCE_GROUP = "group"
SOURCE_PRIVATE = "private"
SOURCE_CHAT = "chat"

#: 配置缺项/坏值时的退路（与 ``core.models.MemeCollectConfig`` 的默认值一致）。
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "batch_probability": 0.35,
    "min_batch": 6,
    "max_batch": 18,
    "review_interval_hours": 12.0,
    "max_reviews_per_day": 3,
    "sheet_size": 9,
    "candidate_ttl_days": 7.0,
}

#: 标题必须写清"这是之前看到过的图"：不写的话模型会当成对方刚发的、回一句"收下了"。
CANDIDATE_BLOCK_TITLE = "【你之前看到过的一些图 · 不是对方刚发的】"

_REF_DIGITS = re.compile(r"(\d+)")


# --------------------------------------------------------------------------- #
# 小工具（纯函数，测试直接打）
# --------------------------------------------------------------------------- #


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if number == number else float(default)  # 挡 NaN


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def short_caption(text: Any, limit: int = SHORT_CAPTION_CHARS) -> str:
    """把画面描述压成一行 ``limit`` 字以内（空白折叠，超出加省略号）。"""

    body = " ".join(str(text or "").split())
    if not body:
        return ""
    cap = max(1, int(limit or SHORT_CAPTION_CHARS))
    if len(body) <= cap:
        return body
    return body[: max(1, cap - 1)] + "…"


def clip_text(text: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """模型写回来的字段裁剪（保留原话，只截断）。"""

    body = " ".join(str(text or "").split())
    return body[: max(1, int(limit or MAX_TEXT_CHARS))]


def clip_name(text: Any) -> str:
    """收藏名：20 字以内（与 ``meme`` 段原有的名字口径一致）。"""

    return clip_text(text, MAX_NAME_CHARS)


def parse_ref(value: Any) -> int:
    """``"候选10"`` / ``"10"`` / ``10`` → 10；认不出来给 0。"""

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if text.startswith(COLLECT_REF_PREFIX):
        text = text[len(COLLECT_REF_PREFIX) :]
    match = _REF_DIGITS.search(text)
    return int(match.group(1)) if match else 0


def source_kind(session_id: Any) -> str:
    """会话 id → ``group`` / ``private`` / ``chat``（只看 umo 前缀，不猜）。"""

    text = str(session_id or "")
    lowered = text.lower()
    if "groupmessage" in lowered or lowered.startswith("group"):
        return SOURCE_GROUP
    if "friendmessage" in lowered or "privatemessage" in lowered:
        return SOURCE_PRIVATE
    return SOURCE_CHAT


def start_of_day(stamp: Any) -> float:
    """本地时区当天零点（频率上限按"一天"算，不按滚动 24 小时）。"""

    try:
        moment = float(stamp)
    except (TypeError, ValueError):
        moment = time.time()
    try:
        local = time.localtime(moment)
        return float(
            time.mktime(
                (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
            )
        )
    except (OverflowError, OSError, ValueError):
        return moment - (moment % 86400.0)


def inventory_line(inventory: Any) -> str:
    """库存事实那一行（只给事实，不劝她多发；类目为空时就不说那半句）。"""

    data = inventory if isinstance(inventory, Mapping) else {}
    total = max(0, _as_int(data.get("total"), 0))
    never = max(0, min(total, _as_int(data.get("never_sent"), 0)))
    top = str(data.get("top_name") or "").strip()
    head = f"你收藏里有 {total} 张，其中 {never} 张从没发出去过"
    if top:
        return f"{head}；最近发得最多的是「{top}」类"
    return head + "。"


@dataclass(frozen=True)
class CollectSettings:
    """``world.meme_collect`` 的只读快照（坏值一律退回默认，绝不让配置读取炸掉 tick）。"""

    enabled: bool = True
    batch_probability: float = 0.35
    min_batch: int = 6
    max_batch: int = 18
    review_interval_hours: float = 12.0
    max_reviews_per_day: int = 3
    sheet_size: int = 9
    candidate_ttl_days: float = 7.0

    @property
    def active(self) -> bool:
        """总开关：关着时整条收图链路都不存在（不只是不拼图）。"""

        return bool(self.enabled)

    def values(self) -> dict[str, Any]:
        """回写配置用（编辑器保存时只覆盖这一节）。"""

        return {
            "enabled": self.enabled,
            "batch_probability": self.batch_probability,
            "min_batch": self.min_batch,
            "max_batch": self.max_batch,
            "review_interval_hours": self.review_interval_hours,
            "max_reviews_per_day": self.max_reviews_per_day,
            "sheet_size": self.sheet_size,
            "candidate_ttl_days": self.candidate_ttl_days,
        }


def collect_settings(world: Any) -> CollectSettings:
    """从 ``world.meme_collect`` 读（对象或 Mapping 都认；缺段按默认值）。"""

    raw = getattr(world, "meme_collect", None)
    if raw is None and isinstance(world, Mapping):
        raw = world.get("meme_collect")
    if isinstance(raw, Mapping):
        data: Mapping[str, Any] = raw
    elif raw is not None:
        data = {
            name: getattr(raw, name, DEFAULTS[name])
            for name in DEFAULTS
        }
    else:
        data = {}
    return CollectSettings(
        enabled=_as_bool(data.get("enabled", DEFAULTS["enabled"]), True),
        batch_probability=_as_float(
            data.get("batch_probability", DEFAULTS["batch_probability"]), 0.35
        ),
        min_batch=min(
            99,
            max(1, _as_int(data.get("min_batch", DEFAULTS["min_batch"]), 6)),
        ),
        max_batch=min(
            COLLECT_SHEET_MAX,
            max(1, _as_int(data.get("max_batch", DEFAULTS["max_batch"]), 18)),
        ),
        review_interval_hours=max(
            0.0,
            _as_float(
                data.get("review_interval_hours", DEFAULTS["review_interval_hours"]), 12.0
            ),
        ),
        max_reviews_per_day=max(
            0, _as_int(data.get("max_reviews_per_day", DEFAULTS["max_reviews_per_day"]), 3)
        ),
        sheet_size=min(
            COLLECT_SHEET_SIZE,
            max(1, _as_int(data.get("sheet_size", DEFAULTS["sheet_size"]), 9)),
        ),
        candidate_ttl_days=max(
            0.0,
            _as_float(
                data.get("candidate_ttl_days", DEFAULTS["candidate_ttl_days"]), 7.0
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# 池子的读写（数据侧全在 core/db.py，这里只做"要不要"与"怎么说"）
# --------------------------------------------------------------------------- #


class MemeCollectStore:
    """候选池 + 评审规划 + 收尾。``db`` 是同步的 ``core.db.Database``。

    ``clock`` / ``rng`` / ``collect`` 都可注入：测试要复现概率与时间，生产用默认。
    """

    def __init__(
        self,
        db: Any,
        *,
        world: Any = None,
        settings: CollectSettings | None = None,
        clock: Callable[[], float] | None = None,
        cache_dir: Any = None,
    ) -> None:
        self._db = db
        self._world = world
        self._settings = settings
        self._clock = clock or time.time
        self._cache_dir = cache_dir
        # 概率掷骰用自己的实例：**不用模块级 ``random``**，也不碰引擎的
        # ``engine.rng`` —— 攒图评审是在 tick 里顺手做的，不该改变主动找人等
        # 其它随机序列（用户 2026-10-24 复审明确要求）。
        self._picker = random.Random()

    # ---- 基础 ------------------------------------------------------------

    def refresh(self, world: Any) -> None:
        """换世界配置后重算设置（引擎在 world 换代时调用）。"""

        if world is not None:
            self._world = world
            self._settings = None

    @property
    def settings(self) -> CollectSettings:
        if self._settings is None:
            self._settings = collect_settings(self._world)
        return self._settings

    def now(self) -> float:
        try:
            return float(self._clock())
        except (TypeError, ValueError):
            return time.time()

    def cache_dir(self) -> Any:
        if self._cache_dir is not None:
            return self._cache_dir
        return candidate_cache_dir(getattr(self._db, "data_dir", "") or ".")

    # ---- 入池 ------------------------------------------------------------

    def note_seen(
        self,
        items: Iterable[Mapping[str, Any]],
        *,
        session_id: Any = "",
        rng: Any = None,
        now: Any = 0.0,
        collected: Callable[[str], bool] | None = None,
    ) -> int:
        """把这一轮"她看到过的图"按概率放进候选池，返回这次真正入池的张数。

        顺序很重要：**先看有没有收藏过**（已收藏的永不入池），再掷概率；``rng`` 注入让
        测试能复现（生产用实例自己的 ``Random``）。坏数据只跳过，不抛。
        """

        settings = self.settings
        if not settings.active or settings.batch_probability <= 0:
            return 0
        stamp = float(now or self.now())
        picker = rng if rng is not None else self._picker
        kind_default = source_kind(session_id)
        added = 0
        for item in items or ():
            if not isinstance(item, Mapping):
                continue
            media_id = str(item.get("media_id") or "").strip()
            if not media_id:
                continue
            try:
                known = (
                    collected(media_id)
                    if collected is not None
                    else self._db.find_meme_entry_by_media(media_id) is not None
                )
            except Exception as exc:  # pragma: no cover - 坏库
                logger.warning("候选池：查已收藏失败，跳过这张：%s", exc)
                continue
            if known:
                continue
            try:
                roll = float(picker.random())
            except Exception:  # pragma: no cover - 注入的 rng 坏了
                roll = 1.0
            if roll > settings.batch_probability:
                continue
            caption = short_caption(
                item.get("caption") or item.get("description") or "",
                SHORT_CAPTION_CHARS * 6,
            )
            try:
                inserted = self._db.add_meme_collect_candidate(
                    media_id=media_id,
                    session_id=str(session_id or ""),
                    caption=caption,
                    source=str(item.get("source") or kind_default),
                    seen_at=stamp,
                )
                if not inserted:
                    # 池里已有这张（insert 被唯一索引挡下）：如果它是"评审过但早过
                    # TTL"的 passed，这次又被她看到，就重新放回池子（每过一个 TTL
                    # 最多再获得一次机会，靠 seen_at 节流，不会反复弹）。
                    inserted = self._db.revive_meme_collect_candidate(
                        media_id=media_id,
                        now=stamp,
                        ttl_seconds=settings.candidate_ttl_days * 86400.0,
                        session_id=str(session_id or ""),
                        caption=caption,
                        source=str(item.get("source") or kind_default),
                    )
            except Exception as exc:
                logger.warning("候选池：入池失败（不影响回复）：%s", exc)
                continue
            if inserted:
                added += 1
        return added

    # ---- 规划 ------------------------------------------------------------

    def expire(self, *, now: Any = 0.0) -> int:
        """清掉超过 TTL 的候选（过期的不再出现在拼图与清单里）。"""

        settings = self.settings
        if settings.candidate_ttl_days <= 0:
            return 0
        stamp = float(now or self.now())
        deadline = stamp - settings.candidate_ttl_days * 86400.0
        try:
            return int(
                self._db.expire_meme_collect_candidates(
                    seen_before=deadline, now=stamp
                )
            )
        except Exception as exc:  # pragma: no cover - 坏库
            logger.warning("候选池：过期清理失败：%s", exc)
            return 0

    def pending_count(self, session_id: Any = "") -> int:
        """这个会话还有多少 ``pending`` 候选（tick 里决定要不要去捡画面描述）。"""

        try:
            return int(
                self._db.count_meme_collect_candidates(
                    session_id=str(session_id or ""),
                    status=COLLECT_STATUS_PENDING,
                )
            )
        except Exception:  # pragma: no cover - 坏库
            return 0

    def prepare(
        self,
        session_id: Any = "",
        *,
        now: Any = 0.0,
        captions: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """判断"现在该不该评审"；该，就分配「候选N」并返回这一批。

        返回 ``{"due": bool, "reason": str, "batch": [...], "sheets": int, "waiting": n}``。
        分配编号会写库：万一这一批判完之前进程挂了，``release`` 会把编号退回。

        ``captions``（``media_id -> 画面描述``）用来给入池时还没转述出来的候选补空白
        描述——入池在消息刚到的 ``on_any_message``，那时转述是异步的，落库只能是空串。
        """

        settings = self.settings
        if not settings.active:
            return {"due": False, "reason": "disabled", "batch": [], "sheets": 0}
        stamp = float(now or self.now())
        self.expire(now=stamp)
        if captions:
            try:
                self._db.fill_meme_collect_captions(
                    captions=dict(captions), session_id=str(session_id or "")
                )
            except Exception as exc:  # pragma: no cover - 补描述失败不影响评审
                logger.warning("候选池：补画面描述失败：%s", exc)
        try:
            last = self._db.last_meme_collect_review(session_id=str(session_id or ""))
        except Exception:  # pragma: no cover - 坏库
            last = None
        if isinstance(last, Mapping):
            previous = _as_float(last.get("created_at"), 0.0)
            gap = settings.review_interval_hours * 3600.0
            if previous > 0 and stamp - previous < gap:
                return {"due": False, "reason": "too_soon", "batch": [], "sheets": 0}
        try:
            today = int(
                self._db.count_meme_collect_reviews(
                    session_id=str(session_id or ""), since=start_of_day(stamp)
                )
            )
        except Exception:  # pragma: no cover - 坏库
            today = 0
        if settings.max_reviews_per_day <= 0 or today >= settings.max_reviews_per_day:
            return {"due": False, "reason": "daily_limit", "batch": [], "sheets": 0}
        size = max(1, min(settings.sheet_size, COLLECT_SHEET_SIZE))
        limit = max(1, min(settings.max_batch, size * MAX_SHEETS))
        try:
            rows = self._db.list_meme_collect_candidates(
                session_id=str(session_id or ""),
                status=COLLECT_STATUS_PENDING,
                unassigned=True,
                limit=limit,
            )
        except Exception as exc:  # pragma: no cover - 坏库
            logger.warning("候选池：读取失败：%s", exc)
            return {"due": False, "reason": "error", "batch": [], "sheets": 0}
        waiting = len(rows or ())
        if waiting < max(1, settings.min_batch):
            return {"due": False, "reason": "not_enough", "batch": [], "sheets": 0, "waiting": waiting}
        rows = list(rows)[:limit]
        batch: list[dict[str, Any]] = []
        sheets = 0
        for sheet_no in range((len(rows) + size - 1) // size):
            chunk = rows[sheet_no * size : (sheet_no + 1) * size]
            if not chunk:
                continue
            sheet_id = f"{session_id or 'ns'}:{int(stamp)}:{sheet_no + 1}"
            try:
                assigned = self._db.assign_meme_collect_refs(
                    candidate_ids=[str(item.get("candidate_id") or "") for item in chunk],
                    sheet_id=sheet_id,
                    start_index=sheet_no * size + 1,
                )
            except Exception as exc:  # pragma: no cover - 坏库
                logger.warning("候选池：分配候选编号失败：%s", exc)
                continue
            sheets += 1
            for row in assigned or ():
                batch.append(dict(row, sheet_no=sheet_no + 1))
        batch.sort(key=lambda item: _as_int(item.get("ref_index"), 0))
        if not batch:
            return {"due": False, "reason": "error", "batch": [], "sheets": 0}
        return {"due": True, "reason": "ready", "batch": batch, "sheets": sheets, "waiting": waiting}

    def release(self, batch: Sequence[Mapping[str, Any]]) -> int:
        """评审没跑完（异常/模型没回话）时退回编号，下次重新编号、重新拼图。"""

        keys = [str(item.get("candidate_id") or "") for item in batch or ()]
        keys = [key for key in keys if key]
        if not keys:
            return 0
        try:
            return int(self._db.release_meme_collect_refs(candidate_ids=keys))
        except Exception as exc:  # pragma: no cover - 坏库
            logger.warning("候选池：退回编号失败：%s", exc)
            return 0

    # ---- 文本 ------------------------------------------------------------

    def inventory(self, *, now: Any = 0.0) -> dict[str, Any]:
        """库存事实（现查 ``meme_entries`` / ``meme_usage``，不新增统计表）。"""

        since = float(now or self.now()) - INVENTORY_WINDOW_SECONDS
        try:
            data = self._db.meme_collect_inventory(since=since)
        except Exception as exc:  # pragma: no cover - 坏库
            logger.warning("库存反馈读取失败：%s", exc)
            return {"total": 0, "never_sent": 0, "top_name": "", "top_count": 0}
        return dict(data or {})

    def candidate_block(
        self,
        batch: Sequence[Mapping[str, Any]],
        *,
        inventory: Mapping[str, Any] | None = None,
        now: Any = 0.0,
    ) -> str:
        """候选块：标题 + 每行 ``候选N｜20 字`` + 库存事实一行（关着时给空串）。"""

        if not self.settings.active or not batch:
            return ""
        lines = [CANDIDATE_BLOCK_TITLE]
        for row in batch:
            index = _as_int(row.get("ref_index"), 0)
            if index <= 0:
                continue
            desc = short_caption(row.get("caption") or "") or "（没看清是什么）"
            lines.append(f"候选{index}｜{desc}")
        fact = inventory_line(
            inventory if inventory is not None else self.inventory(now=now)
        )
        if fact:
            lines.append("")
            lines.append(fact)
        return "\n".join(lines)

    # ---- 收尾 ------------------------------------------------------------

    def apply_selection(
        self,
        batch: Sequence[Mapping[str, Any]],
        items: Iterable[Mapping[str, Any]],
        *,
        collect: Callable[[str, dict[str, Any]], Any],
        now: Any = 0.0,
    ) -> dict[str, Any]:
        """把模型的 ``items`` 兑现成收藏；**没选的、收失败的都置 passed**。

        ``collect(media_id, fields) -> {"ok", "entry_id", "created", ...}`` 由引擎注入
        （真实现走 ``MemeStore.collect_image``）。返回
        ``{"collected": [...], "passed": n, "review_id": str}``，绝不抛。
        """

        stamp = float(now or self.now())
        by_ref: dict[int, Mapping[str, Any]] = {}
        for row in batch or ():
            index = _as_int(row.get("ref_index"), 0)
            if index > 0:
                by_ref[index] = row
        chosen: dict[int, Mapping[str, Any]] = {}
        for item in items or ():
            if not isinstance(item, Mapping):
                continue
            index = parse_ref(item.get("ref") if item.get("ref") is not None else item.get("index"))
            if index <= 0 or index not in by_ref or index in chosen:
                continue
            chosen[index] = item
        session_id = ""
        if batch:
            session_id = str(batch[0].get("session_id") or "")
        collected_ids: list[str] = []
        passed_ids: list[str] = []
        entry_ids: dict[str, str] = {}
        collected: list[dict[str, Any]] = []
        for index in sorted(by_ref):
            row = by_ref[index]
            candidate_id = str(row.get("candidate_id") or "")
            item = chosen.get(index)
            if item is None:
                passed_ids.append(candidate_id)
                continue
            fields = {
                "name": clip_name(item.get("name")),
                "meaning": clip_text(item.get("meaning")),
                "use_when": clip_text(item.get("use_when")),
                "avoid_when": clip_text(item.get("avoid_when")),
            }
            try:
                result = collect(str(row.get("media_id") or ""), fields)
            except Exception as exc:
                logger.warning("批量收藏：单张失败（按不收处理）：%s", exc)
                result = None
            if isinstance(result, Mapping) and result.get("ok"):
                collected_ids.append(candidate_id)
                entry_id = str(result.get("entry_id") or "")
                if entry_id:
                    entry_ids[candidate_id] = entry_id
                collected.append(
                    {
                        "ref": f"{COLLECT_REF_PREFIX}{index}",
                        "candidate_id": candidate_id,
                        "media_id": str(row.get("media_id") or ""),
                        "entry_id": entry_id,
                        "name": fields["name"],
                        "created": bool(result.get("created")),
                    }
                )
            else:
                passed_ids.append(candidate_id)
        if collected_ids:
            self._finish(collected_ids, COLLECT_STATUS_COLLECTED, entry_ids, stamp)
        if passed_ids:
            self._finish(passed_ids, COLLECT_STATUS_PASSED, {}, stamp)
        review_id = ""
        try:
            review = self._db.add_meme_collect_review(
                session_id=session_id,
                sheets=len({_as_int(row.get("sheet_no"), 1) for row in batch or ()}),
                offered=len(by_ref),
                picked=len(collected_ids),
                status="done",
                now=stamp,
            )
            review_id = str((review or {}).get("review_id") or "")
        except Exception as exc:  # pragma: no cover - 坏库
            logger.warning("批量收藏：评审记账失败：%s", exc)
        return {
            "collected": collected,
            "passed": len(passed_ids),
            "review_id": review_id,
        }

    def _finish(
        self,
        candidate_ids: Sequence[str],
        status: str,
        entry_ids: Mapping[str, str],
        stamp: float,
    ) -> None:
        try:
            self._db.finish_meme_collect_candidates(
                candidate_ids=candidate_ids,
                status=status,
                entry_ids=entry_ids,
                now=stamp,
            )
        except Exception as exc:  # pragma: no cover - 坏库
            logger.warning("批量收藏：候选状态写回失败：%s", exc)


# --------------------------------------------------------------------------- #
# 拼图渲染（独立函数：引擎只管调它，异常一律降级成"只有文字清单"）
# --------------------------------------------------------------------------- #


async def render_sheets(
    batch: Sequence[Mapping[str, Any]],
    *,
    media_file: Callable[[str], str],
    cache_dir: Any = None,
    cell_side: int = COLLECT_CELL_SIDE,
) -> list[str]:
    """把这一批拼成 1~2 张 3×3，返回图片路径；任何异常都返回空表（不抛）。

    ``media_file(media_id)`` 由引擎注入（它知道 ``MediaStore`` 在哪）。缺文件的格子
    直接不拼——少一格比整张图失败好，文字清单仍然完整。
    """

    groups: dict[int, list[Mapping[str, Any]]] = {}
    for row in batch or ():
        groups.setdefault(_as_int(row.get("sheet_no"), 1), []).append(row)
    paths: list[str] = []
    for sheet_no in sorted(groups):
        rows = sorted(groups[sheet_no], key=lambda item: _as_int(item.get("ref_index"), 0))
        sources: list[str] = []
        keys: list[str] = []
        start = 0
        for row in rows:
            try:
                path = str(media_file(str(row.get("media_id") or "")) or "")
            except Exception:
                path = ""
            if not path:
                continue
            if start <= 0:
                start = _as_int(row.get("ref_index"), 0)
            sources.append(path)
            keys.append(str(row.get("media_id") or ""))
        if not sources:
            continue
        try:
            result: SheetResult = await composed_collect_sheet(
                sources,
                media_ids=keys,
                start_index=max(1, start),
                cell_side=cell_side,
                cache_dir=cache_dir,
            )
        except Exception as exc:  # pragma: no cover - composed_collect_sheet 自己不抛
            logger.warning("候选拼图渲染失败（退回文字清单）：%s", exc)
            continue
        if result.composed and result.path:
            paths.append(result.path)
    return paths


__all__ = [
    "CANDIDATE_BLOCK_TITLE",
    "COLLECT_BATCH_ACTION_ID",
    "COLLECT_REF_PREFIX",
    "CollectSettings",
    "DEFAULTS",
    "INVENTORY_WINDOW_SECONDS",
    "MAX_SHEETS",
    "MemeCollectStore",
    "SHORT_CAPTION_CHARS",
    "SOURCE_CHAT",
    "SOURCE_GROUP",
    "SOURCE_PRIVATE",
    "clip_name",
    "clip_text",
    "collect_settings",
    "inventory_line",
    "parse_ref",
    "render_sheets",
    "short_caption",
    "source_kind",
    "start_of_day",
]
