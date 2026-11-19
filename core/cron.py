"""5 段 cron：解析、求触发时刻、翻译成人话。纯函数，无 I/O。

为什么要自己写而不引 ``croniter``：日程这一路要的是**可解释**——解析失败要能指出
第几段、哪一段错了；``*``/``*/n``/区间/列表以外的东西一律拒绝（``L``/``W``/``#``/
秒级/年字段），而不是"看起来支持、跑起来行为微妙"。这里面每个函数都是纯的，测试
不需要数据库、不需要时钟。

字段顺序（标准 5 段）::

    分 0-59 | 时 0-23 | 日 1-31 | 月 1-12 | 周 0-6（0 和 7 都算周日）

行为与 Vixie cron 一致的两处关键语义：

* **日与周**：两者都写了具体值时取**或**（"每月 1 号"或"每周五"任意一个到点都算）；
  其中一个是 ``*`` 时只看另一个。
* **不补跑**：本模块只回答"下一次什么时候"，补跑策略（一次 tick 里最多跑一次、
  最多久没跑就不补）由调用方决定。

时区：所有 datetime 都是**本地朴素时间**（与 ``engine.local_now()`` 同一口径），
不做 DST 折算——世界里的时间是按现实时钟走的，DST 那一小时的空隙宁可少触发一次，
也不能让"今天的日程"跑到明天去。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_YEARS_AHEAD = 4
"""往前找触发点时最多看这么多年：闰年 + 2 月 29 号这种组合四年一轮，够用。"""

MONTH_NAMES = (
    "1 月",
    "2 月",
    "3 月",
    "4 月",
    "5 月",
    "6 月",
    "7 月",
    "8 月",
    "9 月",
    "10 月",
    "11 月",
    "12 月",
)

WEEKDAY_NAMES = ("周日", "周一", "周二", "周三", "周四", "周五", "周六")
"""按 cron 的 0-6 排，0 = 周日。"""

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")

ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
"""几个常用别名：UI 的高级输入框里直接写 ``@daily`` 也认。"""


class CronError(ValueError):
    """cron 表达式不合法；``str(exc)`` 就是给用户看的那句话。"""


@dataclass(frozen=True)
class CronSpec:
    """一份解析好的 cron。字段都已经是展开后的具体取值集合。"""

    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    """按 cron 口径（0 = 周日）；转成 ``datetime.weekday()`` 时用 :meth:`py_weekdays`。"""
    days_wildcard: bool
    weekdays_wildcard: bool

    def py_weekdays(self) -> frozenset[int]:
        """转成 Python ``weekday()`` 口径（周一 = 0，周日 = 6）。"""

        return frozenset((item - 1) % 7 for item in self.weekdays)

    @property
    def every_minute(self) -> bool:
        return len(self.minutes) >= 60 and len(self.hours) >= 24

    def day_matches(self, moment: datetime) -> bool:
        """这一天要不要触发：日与周都写死时取**或**。"""

        if not self.days_wildcard and moment.day in self.days:
            return True
        if not self.weekdays_wildcard and moment.weekday() in self.py_weekdays():
            return True
        return self.days_wildcard and self.weekdays_wildcard

    def matches(self, moment: datetime) -> bool:
        """这个具体时刻（精确到分钟）会不会触发。"""

        return (
            moment.month in self.months
            and moment.minute in self.minutes
            and moment.hour in self.hours
            and self.day_matches(moment)
        )


def _expand_field(
    text: str,
    *,
    lo: int,
    hi: int,
    label: str,
    names: tuple[str, ...] | None = None,
) -> frozenset[int]:
    """把一段（``*``/``*/n``/``a``/``a-b``/``a-b/n``/``a,b``）展开成取值集合。"""

    raw = str(text or "").strip()
    if not raw:
        raise CronError(f"第 {label} 段是空的")
    values: set[int] = set()
    for piece in raw.split(","):
        chunk = piece.strip()
        if not chunk:
            raise CronError(f"第 {label} 段里有多余的逗号（{raw!r}）")
        step = 1
        if "/" in chunk:
            base, _, step_text = chunk.partition("/")
            if not step_text.strip().isdigit():
                raise CronError(f"第 {label} 段的步长必须是正整数（{chunk!r}）")
            step = int(step_text.strip())
            if step <= 0:
                raise CronError(f"第 {label} 段的步长必须大于 0（{chunk!r}）")
        else:
            base = chunk
        base = base.strip()
        if base in ("*", "?"):
            if base == "?":
                raise CronError(f"第 {label} 段不支持 ?（{raw!r}）")
            start, end = lo, hi
        elif "-" in base:
            left, _, right = base.partition("-")
            start = _value_of(left, lo=lo, hi=hi, label=label, names=names)
            end = _value_of(right, lo=lo, hi=hi, label=label, names=names)
            if start > end:
                raise CronError(f"第 {label} 段的区间写反了（{base!r}）")
        else:
            start = _value_of(base, lo=lo, hi=hi, label=label, names=names)
            end = hi if "/" in chunk else start
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"第 {label} 段没有展开出任何取值（{raw!r}）")
    return frozenset(values)


def _value_of(
    text: str,
    *,
    lo: int,
    hi: int,
    label: str,
    names: tuple[str, ...] | None,
) -> int:
    raw = str(text or "").strip()
    if not raw:
        raise CronError(f"第 {label} 段里少了一个数（{text!r}）")
    if names:
        folded = raw.lower()[:3]
        if folded in names:
            return names.index(folded) + (1 if lo == 1 else 0)
    if raw == "7" and label == "周" and hi == 6:
        return 0  # 周日写成 7 也认
    if any(mark in raw for mark in ("L", "W", "#", "l", "w")):
        raise CronError(
            f"第 {label} 段不支持 {raw!r} 这种写法（不认 L / W / #，也别写英文字母）"
        )
    try:
        value = int(raw)
    except ValueError:
        raise CronError(f"第 {label} 段不是数字（{raw!r}）") from None
    if value < lo or value > hi:
        raise CronError(f"第 {label} 段必须在 {lo}-{hi} 之间（收到 {value}）")
    return value


def parse(expr: str) -> CronSpec:
    """解析 5 段 cron；不合法抛 :class:`CronError`（消息里带段号，UI 直接显示）。"""

    text = str(expr or "").strip()
    if not text:
        raise CronError("cron 表达式是空的")
    alias = ALIASES.get(text.lower())
    if alias:
        text = alias
    parts = text.split()
    if len(parts) != 5:
        raise CronError(
            f"cron 要 5 段（分 时 日 月 周），收到 {len(parts)} 段：{text!r}"
        )
    minute_text, hour_text, day_text, month_text, weekday_text = parts
    minutes = _expand_field(minute_text, lo=0, hi=59, label="分")
    hours = _expand_field(hour_text, lo=0, hi=23, label="时")
    days = _expand_field(day_text, lo=1, hi=31, label="日")
    months = _expand_field(month_text, lo=1, hi=12, label="月", names=_MONTHS)
    weekdays = _expand_field(weekday_text, lo=0, hi=6, label="周", names=_WEEKDAYS)
    return CronSpec(
        expr=text,
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        days_wildcard=day_text.strip().startswith("*"),
        weekdays_wildcard=weekday_text.strip().startswith("*"),
    )


def next_after(spec: CronSpec, moment: datetime) -> datetime | None:
    """``moment`` 之后（不含此刻）的第一个触发时刻；四年内没有就返回 ``None``。"""

    cursor = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = moment + timedelta(days=int(365.25 * MAX_YEARS_AHEAD))
    day = cursor
    while day <= limit:
        if day.month not in spec.months:
            year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
            day = day.replace(year=year, month=month, day=1, hour=0, minute=0)
            continue
        if not spec.day_matches(day):
            day = (day + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        for hour in sorted(spec.hours):
            if hour < day.hour:
                continue
            for minute in sorted(spec.minutes):
                if hour == day.hour and minute < day.minute:
                    continue
                candidate = day.replace(hour=hour, minute=minute)
                if candidate > moment:
                    return candidate
        day = (day + timedelta(days=1)).replace(hour=0, minute=0)
    return None


def occurrences_between(
    spec: CronSpec, start: datetime, end: datetime, *, limit: int = 48
) -> list[datetime]:
    """``(start, end]`` 之间的触发时刻（按时间升序，最多 ``limit`` 个）。"""

    found: list[datetime] = []
    if limit <= 0 or end <= start:
        return found
    cursor = start
    while len(found) < limit:
        moment = next_after(spec, cursor)
        if moment is None or moment > end:
            break
        found.append(moment)
        cursor = moment
    return found


def prev_before(spec: CronSpec, moment: datetime) -> datetime | None:
    """``moment`` 之前（不含此刻）最近的一次触发；最多往前找 4 年。"""

    cursor = moment.replace(second=0, microsecond=0) - timedelta(minutes=1)
    limit = moment - timedelta(days=int(365.25 * MAX_YEARS_AHEAD))
    day = cursor
    while day >= limit:
        if day.month not in spec.months:
            year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
            last = calendar.monthrange(year, month)[1]
            day = day.replace(year=year, month=month, day=last, hour=23, minute=59)
            continue
        if not spec.day_matches(day):
            day = (day - timedelta(days=1)).replace(hour=23, minute=59)
            continue
        for hour in sorted(spec.hours, reverse=True):
            if hour > day.hour:
                continue
            for minute in sorted(spec.minutes, reverse=True):
                if hour == day.hour and minute > day.minute:
                    continue
                candidate = day.replace(hour=hour, minute=minute)
                if candidate < moment:
                    return candidate
        day = (day - timedelta(days=1)).replace(hour=23, minute=59)
    return None


# ---------------------------------------------------------------------------
# 翻译成人话
# ---------------------------------------------------------------------------


def _range_text(values: frozenset[int], *, unit: str, offset: int = 0) -> str:
    """把一组整数压成「a-b、c」这种最省字的写法。"""

    ordered = sorted(values)
    spans: list[list[int]] = []
    for value in ordered:
        if spans and value == spans[-1][1] + 1:
            spans[-1][1] = value
        else:
            spans.append([value, value])
    bits = [
        f"{span[0] + offset}" if span[0] == span[1] else f"{span[0] + offset}-{span[1] + offset}"
        for span in spans
    ]
    return "、".join(bits) + unit


def _step_of(values: frozenset[int], *, total: int) -> int:
    """如果这组取值正好是从 0 起、等步长铺满的，就返回步长；否则 0。"""

    ordered = sorted(values)
    if not ordered or ordered[0] != 0:
        return 0
    if len(ordered) < 2:
        return 0
    step = ordered[1]
    if step <= 0:
        return 0
    if ordered != list(range(0, total, step)):
        return 0
    return step


def describe(spec: CronSpec) -> str:
    """把 cron 翻成中文（设置页与提示词共用同一份口径）。"""

    all_minutes = len(spec.minutes) >= 60
    all_hours = len(spec.hours) >= 24

    # 时间部分
    step = _step_of(spec.minutes, total=60)
    if all_minutes and all_hours:
        time_text = "每分钟"
    elif all_hours and len(spec.minutes) == 1 and next(iter(spec.minutes)) == 0:
        time_text = "每小时一次"
    elif all_hours and len(spec.minutes) == 1:
        time_text = f"每小时的第 {next(iter(spec.minutes)):02d} 分"
    elif all_minutes:
        time_text = "每小时的第 " + _range_text(spec.minutes, unit=" 分")
    elif step and not all_hours:
        time_text = f"{_range_text(spec.hours, unit=' 点')}之间每 {step} 分钟"
    elif step:
        time_text = f"每 {step} 分钟"
    elif len(spec.hours) == 1 and len(spec.minutes) == 1:
        hour = next(iter(spec.hours))
        minute = next(iter(spec.minutes))
        time_text = f"{hour:02d}:{minute:02d}"
    elif len(spec.minutes) == 1:
        minute = next(iter(spec.minutes))
        time_text = f"在 {_range_text(spec.hours, unit=' 点')} 的第 {minute:02d} 分"
    else:
        time_text = (
            f"在 {_range_text(spec.hours, unit=' 点')} 的 {_range_text(spec.minutes, unit=' 分')}"
        )

    # 日期部分
    day_bits: list[str] = []
    if spec.days_wildcard and spec.weekdays_wildcard:
        day_bits.append("每天")
    else:
        if not spec.weekdays_wildcard:
            weekdays = spec.py_weekdays()
            names = [WEEKDAY_NAMES[(item + 1) % 7] for item in sorted(weekdays)]
            if weekdays == frozenset({0, 1, 2, 3, 4}):
                day_bits.append("工作日")
            elif weekdays == frozenset({5, 6}):
                day_bits.append("周末")
            else:
                day_bits.append("每" + "、".join(names))
        if not spec.days_wildcard:
            day_bits.append("每月 " + _range_text(spec.days, unit=" 号"))
    if not spec.days_wildcard and not spec.weekdays_wildcard:
        day_text = "或".join(day_bits) + "（任一满足）"
    else:
        day_text = "".join(day_bits)

    month_text = ""
    if len(spec.months) < 12:
        month_text = "只在 " + "、".join(MONTH_NAMES[item - 1] for item in sorted(spec.months))

    head = " ".join(bit for bit in (day_text, time_text, month_text) if bit)
    if spec.every_minute:
        return "每分钟"
    return head.strip() or "（没有可触发的时间）"


__all__ = [
    "ALIASES",
    "CronError",
    "CronSpec",
    "MAX_YEARS_AHEAD",
    "MONTH_NAMES",
    "WEEKDAY_NAMES",
    "describe",
    "next_after",
    "occurrences_between",
    "parse",
    "prev_before",
]
