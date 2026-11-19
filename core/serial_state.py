"""连载进度（「在看的剧」）的结构化记录（VW-T19）。

槽里原来存的是她当轮说的一句自由文本，于是「看到第几集」没有权威来源 ——
这一轮说第 6 集、下一轮又说第 5 集，进度永远不往前。这里把槽值升级成一份
小 JSON 记录：集数只由「看剧动作完成了几次」推进（:func:`advance`），她说的
集数只在**旧文本升级**（:func:`parse_serial_slot`）时当一次性参考。

全部是纯函数（不读写库、不依赖引擎），升级 / 推进 / 渲染 / 过期判定都能单测。
"""

from __future__ import annotations

import json
import re
from typing import Any

KIND = "serial"
"""记录的类型标记：槽里 ``text`` 是一份 JSON 且 ``kind == "serial"`` 才算连载记录。"""

SERIAL_SLOTS = frozenset({"watching"})
"""哪些状态槽按连载进度管理（目前只有「在看的剧」）。"""

DEFAULT_TTL_MINUTES = 720
"""动作没配 ``state_ttl_minutes`` 时的兜底过期时间：12 小时没更新就不注入。

一次看剧最多 7200 秒（2 小时），12 小时足够覆盖「一晚上连看几集」；
再久没动静，那些集数就不该再当成她此刻的事实。
"""

TITLE_MAX = 40
NOTE_MAX = 60

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100}

_NUM = r"([0-9０-９]+|[零〇一二三四五六七八九十百两]+)"
_EPISODE_RE = re.compile(r"第\s*" + _NUM + r"\s*[集话期回]")
_TOTAL_RE = re.compile(r"(?:共|总共|一共|全)\s*" + _NUM + r"\s*[集话期]")
_TITLE_RE = re.compile(r"《([^》]{1,40})》")
_LABEL_RE = re.compile(r"^(?:-\s*)?在看的剧\s*[：:]\s*")
_STATUS_DONE_RE = re.compile(r"看完|看过了|大结局|完结|收尾|结局|刷完")
_STATUS_DOING_RE = re.compile(r"在看|正在看|刚看|开始看|接着看|看了一半|看到一半|追到")
_META_RE = re.compile(r"[（(][^（）()]{0,30}?前拿到的[）)]")
_SPLIT_RE = re.compile(r"[，,。！？!?；;、\s…·~～—]+")
_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")


# ---------------------------------------------------------------- 基础工具


def _as_int(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def cn_to_int(text: Any) -> int | None:
    """中文数字转阿拉伯数字（``十一`` → 11、``二十`` → 20、``一百零五`` → 105）。

    只处理到百位——集数用不到更大；认不出来返回 ``None``（调用方自己兜底）。
    """

    raw = str(text or "").strip().translate(_FULLWIDTH)
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    total = 0
    current = 0
    for ch in raw:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            total += (current or 1) * unit
            current = 0
        else:
            return None
    total += current
    return total


# ---------------------------------------------------------------- 记录读写


def blank_record(**extra: Any) -> dict[str, Any]:
    """一条空记录（还没看到第 1 集）。"""

    data = {
        "kind": KIND,
        "title": "",
        "episode": 0,
        "episode_total": 0,
        "note": "",
        "updated_at": 0.0,
        "last_ticks": 0,
    }
    data.update(extra)
    return normalize(data)


def normalize(record: Any) -> dict[str, Any]:
    """把任意来源的 dict 规整成一份合法记录（多余的键原样保留）。"""

    src = dict(record) if isinstance(record, dict) else {}
    out: dict[str, Any] = dict(src)
    out["kind"] = KIND
    out["title"] = _clip(src.get("title"), TITLE_MAX)
    out["episode"] = _as_int(src.get("episode"))
    out["episode_total"] = _as_int(src.get("episode_total"))
    out["note"] = _clip(src.get("note"), NOTE_MAX)
    out["updated_at"] = _as_float(src.get("updated_at"))
    out["last_ticks"] = _as_int(src.get("last_ticks"))
    if "finished_title" in out:
        out["finished_title"] = _clip(out.get("finished_title"), TITLE_MAX)
    if "label" in out:
        out["label"] = _clip(out.get("label"), 20)
    if not out.get("parse_failed"):
        out.pop("parse_failed", None)
    return out


def load(text: Any) -> dict[str, Any]:
    """把槽里的 ``text`` 读成记录；不是连载 JSON 就给一条空记录。"""

    raw = str(text or "").strip()
    if not raw:
        return blank_record()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return blank_record()
    if not isinstance(data, dict) or str(data.get("kind") or "") != KIND:
        return blank_record()
    return normalize(data)


def is_serial_json(text: Any) -> bool:
    """这段槽文本是不是已经升级过的连载 JSON。"""

    raw = str(text or "").strip()
    if not raw.startswith("{"):
        return False
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and str(data.get("kind") or "") == KIND


def to_json(record: Any) -> str:
    """记录 → 存进槽 ``text`` 的字符串（紧凑、保留中文）。"""

    return json.dumps(normalize(record), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- 旧文本解析


def _strip_meta(text: str) -> str:
    return " ".join(_META_RE.sub(" ", str(text or "")).split())


def _title_without_brackets(body: str) -> str:
    """没书名号时：取「在看的剧：」后到第一个空格 / 标点的那一小段。"""

    match = _LABEL_RE.match(body)
    if not match:
        return ""
    rest = body[match.end():]
    piece = _SPLIT_RE.split(rest, maxsplit=1)[0].strip()
    return _clip(piece, TITLE_MAX)


def _sentence_around(body: str, span: tuple[int, int]) -> str:
    """集数所在的那一小句（只在这一句里找「看完了 / 在看」这类状态词）。"""

    start = 0
    for index in range(span[0] - 1, -1, -1):
        if _SPLIT_RE.match(body[index]):
            start = index + 1
            break
    end = len(body)
    for index in range(span[1], len(body)):
        if _SPLIT_RE.match(body[index]):
            end = index
            break
    return body[start:end]


def _pick_note(body: str) -> str:
    """细节：剥掉剧名 / 集数 / 状态之外最长的那一小句（≤60 字）。"""

    candidates: list[str] = []
    for fragment in _SPLIT_RE.split(body):
        piece = fragment.strip()
        if len(piece) < 4:
            continue
        if _TITLE_RE.search(piece) or _EPISODE_RE.search(piece):
            continue
        if _STATUS_DONE_RE.search(piece) or _STATUS_DOING_RE.search(piece):
            continue
        candidates.append(piece)
    if not candidates:
        return ""
    return _clip(max(candidates, key=len), NOTE_MAX)


def parse_serial_slot(text: Any) -> dict[str, Any]:
    """把旧槽里的自由文本解析成记录（升级时用一次）。

    规则（执行单 §2）：
    - 标题取 ``《…》``；没有书名号就取「在看的剧：」后第一段；
    - 集数认 ``第 N 集``（阿拉伯 / 中文都认，最多到百位）；
    - ``共 N 集`` 记进 ``episode_total``；
    - 「看完了 / 大结局 / 完结」→ 这一集已完成（``episode = N``）；
      「在看 / 刚看」→ 这一集还没看完（``episode = N - 1``）；
    - 解析不出标题也解析不出集数 → 兜底 ``title = 原文前 20 字``、``episode = 0``，
      并打 ``parse_failed`` 让引擎记一条事件。
    """

    raw = str(text or "").strip()
    if is_serial_json(raw):
        return normalize(json.loads(raw))
    body = _strip_meta(raw)
    record = blank_record()

    title_match = _TITLE_RE.search(body)
    title = _clip(title_match.group(1), TITLE_MAX) if title_match else _title_without_brackets(body)

    episodes = list(_EPISODE_RE.finditer(body))
    total_match = _TOTAL_RE.search(body)
    total = cn_to_int(total_match.group(1)) if total_match else 0

    episode = 0
    if episodes:
        number = cn_to_int(episodes[-1].group(1)) or 0
        sentence = _sentence_around(body, episodes[-1].span())
        if _STATUS_DONE_RE.search(sentence):
            episode = number
        elif _STATUS_DOING_RE.search(sentence):
            episode = max(0, number - 1)
        else:
            # 没明说：旧槽就是「这一集收尾」时写进去的，按看完了算
            episode = number
    elif total and _STATUS_DONE_RE.search(body):
        episode = total

    record.update(
        {
            "title": title,
            "episode": episode,
            "episode_total": total or 0,
            "note": _pick_note(body),
            "parse_failed": not title and not episode,
        }
    )
    if record["parse_failed"]:
        record["title"] = _clip(body or raw, 20)
        record["episode"] = 0
    return normalize(record)


# ---------------------------------------------------------------- 推进 / 渲染


def advance(
    record: Any,
    *,
    now: float = 0.0,
    ticks: int = 0,
    title: str = "",
    note: str = "",
) -> dict[str, Any]:
    """完成一次「追剧」：集数 +1（单调，不看她说的第几集）。

    幂等：同一个 ``(updated_at, duration_ticks)`` 再来一次只返回原记录，
    所以一次 action_done 被重复处理（重放 / 重试）不会连加两集。

    越过 ``episode_total`` 时收尾：清空 title / episode，留 ``finished_title``
    给引擎去写 own_topic（「《X》看完了」）。
    """

    record_in = normalize(record)
    ticks = _as_int(ticks)
    stamp = _as_float(now)
    if stamp > 0 and record_in["updated_at"] == stamp and record_in["last_ticks"] == ticks:
        return record_in

    name = _clip(title, TITLE_MAX) or record_in["title"]
    if not name and record_in.get("finished_title"):
        # 上一部已经看完了（槽里只剩这条墓碑）：她没有说出新剧名就当没这回事
        return record_in
    out = dict(record_in)
    out["title"] = name
    out["last_ticks"] = ticks
    out["updated_at"] = stamp if stamp > 0 else record_in["updated_at"]
    if note:
        out["note"] = _clip(note, NOTE_MAX)
    if not name:
        # 连剧名都没有：没法往前推，别凭空 +1
        return normalize(out)

    episode = record_in["episode"] + 1
    total = record_in["episode_total"]
    if total > 0 and episode > total:
        out.update(
            {
                "title": "",
                "episode": 0,
                "episode_total": 0,
                "note": "",
                "finished_title": name,
                "finished_at": out["updated_at"],
            }
        )
        out.pop("parse_failed", None)
        return normalize(out)

    out["episode"] = episode
    out.pop("parse_failed", None)
    out.pop("finished_title", None)
    out.pop("finished_at", None)
    return normalize(out)


def render(record: Any) -> str:
    """把记录渲染成执行单 §4 的三行（标题空 / 已完成 → 空串，不注入）。"""

    rec = normalize(record)
    title = rec["title"]
    if not title:
        return ""
    label = _clip(rec.get("label") or "在看的剧", 20) or "在看的剧"
    episode = rec["episode"]
    total = rec["episode_total"]
    if episode <= 0:
        head = f"《{title}》刚定下来要看，还没看到第 1 集。"
    elif total > 0:
        head = f"《{title}》已经看到第 {episode} 集（共 {total} 集），下一集是第 {episode + 1} 集。"
    else:
        head = f"《{title}》已经看到第 {episode} 集，下一集是第 {episode + 1} 集。"
    note = rec["note"]
    if note:
        head += f"上次记住的细节：{note}。"
    if episode > 0:
        tail = f"（自然提，不用报清单；看完一集就说看完这集，别说成还在看第 {episode} 集）"
    else:
        # 还没看到第 1 集：别让提示词里出现"还在看第 1 集"这种没发生过的事实
        tail = "（自然提，不用报清单；看完一集就说看完这集，别说成还没开始看）"
    return "\n".join([f"# {label}", head, tail])


def is_expired(record: Any, minutes: int = 0, *, now: float = 0.0) -> bool:
    """记录是不是太久没更新了（``minutes <= 0`` 视为不过期）。"""

    limit = _as_int(minutes)
    if limit <= 0:
        return False
    stamp = _as_float(normalize(record).get("updated_at"))
    if stamp <= 0 or _as_float(now) <= 0:
        return False
    return (_as_float(now) - stamp) > limit * 60


__all__ = [
    "DEFAULT_TTL_MINUTES",
    "KIND",
    "NOTE_MAX",
    "SERIAL_SLOTS",
    "TITLE_MAX",
    "advance",
    "blank_record",
    "cn_to_int",
    "is_expired",
    "is_serial_json",
    "load",
    "normalize",
    "parse_serial_slot",
    "render",
    "to_json",
]
