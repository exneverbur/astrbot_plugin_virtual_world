"""相对时间 → 绝对日期（记忆写入用）。

为什么单独成模块：记忆一旦按原话存成「我明天要去体检」，第二天再读还是「明天」，
她就会把**已经做完的事**当成还没做（用户实机事故：体检第二天她还在问"要不要提醒你"）。
所以写入那一刻就把相对时间换成绝对日期，原话留在句末 ``（原话：…）`` 里，溯源不丢。

三条设计约束：

* **纯函数**：不 import db、不碰世界状态，可以被 memory / profile / consolidate 共用，
  也可以单独测（``tests/test_time_ref.py``）；
* **幂等**：已经归一过（句末带 ``（原话：…）``）的文本再归一一次不会再变——否则
  ``_write_memory`` 归一一次、``remember`` 再归一一次，原话里的"明天"会被二次换算；
* **只动认识的时间词**：不认识的词原样保留，没有相对词时原样返回、第二个元素为空列表。

支持词表（``SUPPORTED_WORDS``）：

* 日：今天 / 今日 / 明天 / 明日 / 后天 / 大后天 / 昨天 / 昨日 / 前天 / 大前天
* 周：本周X / 这周X / 下周X / 下下周X / 上周X / 周X / 星期X / 礼拜X
  （X = 一…六 / 日 / 天，也认 1…7；前缀后允许带"个"，如「下个星期一」）；
  不带 X 的 本周 / 这周 / 下周 / 下下周 / 上周 落到**那一周的周一**
* 月：这个月 / 本月 / 下个月 / 上个月 → ``YYYY-MM``（只到月，不猜日）
* 年：今年 / 明年 / 去年 → ``YYYY``（只到年，不猜月日）
* 相对量：「N 天 / 日 / 周 / 星期 / 个星期 / 礼拜 / 个月 / 年 + 后|之后|以后|前|之前|以前」，
  N 认阿拉伯数字与中文数字（一…十、十二、二十三、两）

边界（刻意不做的事，见 ``normalize_relative_dates`` 文档）：

* 「周X」按**本周**算（今天周三说"周一"就是本周一，不往后滚）；
* 「上午 / 下午 / 晚上 / 凌晨」这类时段词不动（日期换了就够，时间点由上下文给）；
* 「周末」「隔天」「大半年」这类不认识的词不动。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

__all__ = [
    "SUPPORTED_WORDS",
    "format_absolute",
    "normalize_relative_dates",
]

ORIGINAL_PREFIX = "（原话："
ORIGINAL_SUFFIX = "）"

SUPPORTED_WORDS: dict[str, tuple[str, ...]] = {
    "日": ("今天", "今日", "明天", "明日", "后天", "大后天", "昨天", "昨日", "前天", "大前天"),
    "周": (
        "本周X", "这周X", "下周X", "下下周X", "上周X",
        "周X", "星期X", "礼拜X",
        "本周", "这周", "下周", "下下周", "上周",
    ),
    "月": ("这个月", "本月", "下个月", "上个月"),
    "年": ("今年", "明年", "去年"),
    "相对量": ("N天", "N日", "N周", "N星期", "N个星期", "N礼拜", "N个月", "N年"),
    "方位词": ("后", "之后", "以后", "前", "之前", "以前"),
}
"""支持的时间词总表（``X`` = 周一…周日 / 星期一…星期天，也认 1…7）。"""

# ---------------------------------------------------------------- 词表 / 正则

_DAY_OFFSETS: dict[str, int] = {
    "大后天": 3,
    "大前天": -3,
    "后天": 2,
    "前天": -2,
    "今天": 0,
    "今日": 0,
    "明天": 1,
    "明日": 1,
    "昨天": -1,
    "昨日": -1,
}

_MONTH_OFFSETS: dict[str, int] = {"这个月": 0, "本月": 0, "下个月": 1, "上个月": -1}
_YEAR_OFFSETS: dict[str, int] = {"今年": 0, "明年": 1, "去年": -1}

_WEEKDAY_INDEX: dict[str, int] = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}

_WEEK_PREFIX_OFFSETS: dict[str, int] = {"下下": 14, "下": 7, "本": 0, "这": 0, "上": -7}

# 「大后天」必须排在「后天」前面：交替是从左往右试的，长词先试才不会只吃掉半截。
_DAY_RE = re.compile("|".join(sorted(_DAY_OFFSETS, key=len, reverse=True)))

_WEEK_PREFIXED_RE = re.compile(
    r"(?P<pre>下下|下|本|这|上)个?(?P<unit>周|星期|礼拜)(?P<wd>[一二三四五六日天1-7])?(?!末)"
)

_WEEKDAY_RE = re.compile(r"(?P<unit>周|星期|礼拜)(?P<wd>[一二三四五六日天1-7])(?!末)")

_MONTH_RE = re.compile(r"这个月|本月|下个月|上个月")
_YEAR_RE = re.compile(r"今年|明年|去年")

_NUM = r"[0-9]+|[一二三四五六七八九十两]+"
_UNIT = r"个星期|个礼拜|个月|星期|礼拜|周|天|日|年|月"
_DIRECTION = r"之后|以后|之前|以前|后|前"

_RELATIVE_RE = re.compile(rf"(?P<n>{_NUM})(?P<unit>{_UNIT})(?P<dir>{_DIRECTION})")

# 我们自己写上去的那截尾巴：再归一一次时要整段跳过，否则原话里的"明天"会被二次换算。
_ORIGINAL_TAIL_RE = re.compile(r"（原话：[^（）]*）\s*$")

_CN_DIGITS: dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "两": 2,
}


# ---------------------------------------------------------------- 小工具


def _zone(tz: Any) -> Any:
    """``tz`` 可以是 ``None`` / IANA 名字 / tzinfo。"""

    if tz is None:
        return None
    if isinstance(tz, str):
        name = tz.strip()
        if not name:
            return None
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - 时区名写错不该让记忆写不进去
            return None
    return tz


def _local_date(now: Any, tz: Any = None) -> date:
    """把 ``now`` 折成"她那边的今天"。

    接受 ``datetime``（带不带 tzinfo 都行）或时间戳，方便调用方直接把自己手里的时钟
    （世界时钟 / 真实时钟）递进来。
    """

    if isinstance(now, datetime):
        moment = now
    elif isinstance(now, date):
        return now
    else:
        moment = datetime.fromtimestamp(float(now))
    zone = _zone(tz)
    if zone is not None:
        try:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=zone)
            else:
                moment = moment.astimezone(zone)
        except Exception:  # noqa: BLE001 - 换算失败就按原样取日期
            pass
    return moment.date()


def _parse_count(token: str) -> int | None:
    """中文/阿拉伯数字 → int（认 一…十、十二、二十三、两）。"""

    text = str(token or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        if left and left not in _CN_DIGITS:
            return None
        if right and right not in _CN_DIGITS:
            return None
        tens = _CN_DIGITS[left] if left else 1
        ones = _CN_DIGITS[right] if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(text)


def _add_months(day: date, months: int) -> date:
    """按月加减，日超出该月天数时收到月末（1 月 31 日 + 1 个月 = 2 月 28/29 日）。"""

    total = day.year * 12 + (day.month - 1) + int(months)
    year, month0 = divmod(total, 12)
    month = month0 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def format_absolute(value: date, level: str = "day") -> str:
    """统一的输出格式：日 ``YYYY-MM-DD``、月 ``YYYY-MM``、年 ``YYYY``。"""

    if level == "year":
        return f"{value.year:04d}"
    if level == "month":
        return f"{value.year:04d}-{value.month:02d}"
    return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"


# ---------------------------------------------------------------- 各段的解析


def _resolve_day(match: re.Match[str], today: date) -> str | None:
    offset = _DAY_OFFSETS.get(match.group(0))
    if offset is None:
        return None
    return format_absolute(today + timedelta(days=offset))


def _is_weather_day(match: re.Match[str]) -> bool:
    """「上周天气」「本周天气」里的「天」是"天气"的天，不是星期天。

    中文没有词边界，``周天`` 既可能是周日、也可能是「周/天」的巧合。跟着"气/台/空"
    的时候一律只认前面那段（``上周`` = 上周一），别把天气读成周日。
    """

    if match.group("wd") != "天":
        return False
    nxt = match.string[match.end(): match.end() + 1]
    return nxt in ("气", "台", "空")


def _resolve_week_prefixed(match: re.Match[str], today: date) -> str | None:
    offset = _WEEK_PREFIX_OFFSETS.get(match.group("pre"))
    if offset is None:
        return None
    monday = _week_monday(today)
    wd = match.group("wd")
    if wd and not _is_weather_day(match):
        index = _WEEKDAY_INDEX.get(wd)
        if index is None:
            return None
        return format_absolute(monday + timedelta(days=offset + index))
    return format_absolute(monday + timedelta(days=offset))


def _resolve_weekday(match: re.Match[str], today: date) -> str | None:
    if _is_weather_day(match):
        return format_absolute(_week_monday(today))
    index = _WEEKDAY_INDEX.get(match.group("wd"))
    if index is None:
        return None
    return format_absolute(_week_monday(today) + timedelta(days=index))


def _resolve_month(match: re.Match[str], today: date) -> str | None:
    offset = _MONTH_OFFSETS.get(match.group(0))
    if offset is None:
        return None
    return format_absolute(_add_months(today, offset), level="month")


def _resolve_year(match: re.Match[str], today: date) -> str | None:
    offset = _YEAR_OFFSETS.get(match.group(0))
    if offset is None:
        return None
    return format_absolute(date(today.year + offset, 1, 1), level="year")


def _resolve_relative(match: re.Match[str], today: date) -> str | None:
    count = _parse_count(match.group("n"))
    if not count:
        return None
    unit = match.group("unit")
    direction = match.group("dir")
    sign = -1 if direction in ("前", "之前", "以前") else 1
    if unit in ("年",):
        return format_absolute(date(today.year + sign * count, 1, 1), level="year")
    if unit in ("个月", "月"):
        return format_absolute(_add_months(today, sign * count), level="day")
    if unit in ("周", "星期", "个星期", "礼拜", "个礼拜"):
        return format_absolute(today + timedelta(days=sign * count * 7))
    return format_absolute(today + timedelta(days=sign * count))


# 顺序有意义：日词 → 带前缀的周 → 光杆周X → 月 → 年 → 相对量。
# 「大后天」先于「后天」、「下下周X」先于「下周X」都靠同一段正则里的长词优先保证。
_PASSES: tuple[tuple[re.Pattern[str], Any], ...] = (
    (_DAY_RE, _resolve_day),
    (_WEEK_PREFIXED_RE, _resolve_week_prefixed),
    (_WEEKDAY_RE, _resolve_weekday),
    (_MONTH_RE, _resolve_month),
    (_YEAR_RE, _resolve_year),
    (_RELATIVE_RE, _resolve_relative),
)


def _split_original_tail(text: str) -> tuple[str, str]:
    """拆出句末的 ``（原话：…）``：正文 / 尾巴（没有尾巴时第二个元素为空串）。"""

    match = _ORIGINAL_TAIL_RE.search(text)
    if match is None:
        return text, ""
    return text[: match.start()], text[match.start():]


# ---------------------------------------------------------------- 入口


def normalize_relative_dates(
    text: str,
    now: datetime,
    *,
    tz: Any = None,
) -> tuple[str, list[tuple[str, str]]]:
    """把一句话里的相对时间词换成绝对日期。

    返回 ``(改写后的文本, [(原词, 绝对日期), ...])``：

    * 句中没有相对词 → 原样返回、第二个元素是空列表；
    * 有相对词 → 原词全部换成 ``YYYY-MM-DD``（只有月 ``YYYY-MM``、只有年 ``YYYY``），
      并在**句末**追加一次 ``（原话：替换前的完整原句）``；
    * 已经归一过的文本（句末带 ``（原话：…）``）再归一一次结果不变。

    ``now`` 是"她认为的现在"，``tz`` 可给 IANA 名字（如 ``Asia/Shanghai``）。
    """

    body = str(text or "")
    if not body.strip():
        return body, []
    today = _local_date(now, tz)
    head, _tail = _split_original_tail(body)
    pairs: list[tuple[str, str]] = []
    rewritten = head

    def _replace(match: re.Match[str], resolver: Any) -> str:
        word = match.group(0)
        try:
            absolute = resolver(match, today)
        except Exception:  # noqa: BLE001 - 单个词解析失败就当不认识，别丢整句
            absolute = None
        if not absolute:
            return word
        pairs.append((word, absolute))
        return absolute

    for pattern, resolver in _PASSES:
        rewritten = pattern.sub(lambda m, r=resolver: _replace(m, r), rewritten)
    if not pairs:
        return body, []
    return f"{rewritten}{ORIGINAL_PREFIX}{head}{ORIGINAL_SUFFIX}", pairs


def iter_supported_words() -> Iterable[str]:
    """把支持词表摊平成一串（报错信息 / 文档用）。"""

    for items in SUPPORTED_WORDS.values():
        for item in items:
            yield item
