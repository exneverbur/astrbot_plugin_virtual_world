"""天气：记录怎么存、"多久之前"怎么说、提示词与编辑器横幅共用的那一份数据。

天气是现实世界的属性，跟她所在的会话、地图区域都无关，所以整份记录全局共享，
存在 DB 的 kv 表里；提示词、横幅、静默刷新读的都是同一份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

# kv 里的两个 key：一份是查到的结果，一份是"上次尝试是什么时候"（节流用）
WEATHER_KEY = "weather"
WEATHER_TRY_KEY = "weather.last_try"

# 归一化之后的字段顺序：城市｜温度｜天气｜湿度｜风力｜预报
PART_KEYS = ("city", "temp", "desc", "humidity", "wind", "forecast")
PART_SEP = "｜"
_PART_SEPARATORS = ("｜", "|")


@dataclass
class WeatherRecord:
    """一次查到的天气。``at`` 是查到它的时刻——提示词里的"多久之前"按它算。"""

    text: str = ""
    parts: dict[str, str] = field(default_factory=dict)
    at: float = 0.0
    source: str = ""
    raw: str = ""
    images: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.line()

    def line(self) -> str:
        """给模型看的那一行：优先用归一化结果，没有就把字段拼回去。"""

        text = str(self.text or "").strip()
        return text or format_parts(self.parts)

    def to_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "parts": {key: value for key, value in self.parts.items() if value},
            "at": float(self.at or 0.0),
            "source": self.source,
            "raw": self.raw,
            "images": list(self.images),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "WeatherRecord":
        """读 kv 里的值；坏数据当没有，不要让编辑器页面跟着炸掉。"""

        if not isinstance(payload, dict):
            return cls()
        raw_parts = payload.get("parts")
        parts: dict[str, str] = {}
        if isinstance(raw_parts, dict):
            for key, value in raw_parts.items():
                text = str(value or "").strip()
                if text:
                    parts[str(key)] = text
        try:
            at = float(payload.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        images = payload.get("images")
        return cls(
            text=str(payload.get("text") or ""),
            parts=parts,
            at=at,
            source=str(payload.get("source") or ""),
            raw=str(payload.get("raw") or ""),
            images=[str(item) for item in images if str(item).strip()]
            if isinstance(images, list)
            else [],
        )


def parse_parts(text: str) -> dict[str, str]:
    """把 ``城市｜温度｜天气｜湿度｜风力｜预报`` 拆成字段；拆不出来返回空字典。"""

    body = str(text or "").strip()
    separator = ""
    for candidate in _PART_SEPARATORS:
        if candidate in body:
            separator = candidate
            break
    if not separator:
        return {}
    chunks = [item.strip() for item in body.split(separator)]
    parts: dict[str, str] = {}
    for index, key in enumerate(PART_KEYS):
        value = chunks[index] if index < len(chunks) else ""
        if value:
            parts[key] = value
    return parts


def format_parts(parts: dict[str, str]) -> str:
    """字段拼回一行（末尾的空字段不补分隔符）。"""

    values = [str(parts.get(key) or "").strip() for key in PART_KEYS]
    while values and not values[-1]:
        values.pop()
    return PART_SEP.join(values)


def age_text(at: float, now: float, *, tz: tzinfo | None = None) -> str:
    """多久之前：刚刚 / X 小时前 / 昨天 / 前天 / X 天前。

    按自然日算：同一天说"几小时前"，昨天前天直说，更久就说天数。
    """

    try:
        at_value = float(at or 0.0)
        now_value = float(now or 0.0)
    except (TypeError, ValueError):
        return ""
    if at_value <= 0 or now_value <= 0:
        return ""
    delta = max(0.0, now_value - at_value)
    try:
        day_now = datetime.fromtimestamp(now_value, tz).date()
        day_at = datetime.fromtimestamp(at_value, tz).date()
        days = (day_now - day_at).days
    except (OverflowError, OSError, ValueError):
        # 时间戳离谱时退回按 24 小时算，至少不会抛
        days = int(delta // 86400)
    if days <= 0:
        hours = int(delta // 3600)
        return "刚刚" if hours < 1 else f"{hours} 小时前"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    return f"{days} 天前"


def prompt_block(
    record: WeatherRecord,
    now: float,
    *,
    stale_hours: float = 24.0,
    tz: tzinfo | None = None,
) -> str:
    """写进提示词的那一段；没有记录、或者旧到不值得提就返回空串。"""

    line = record.line()
    if not line:
        return ""
    at = float(record.at or 0.0)
    if at > 0 and stale_hours > 0 and (float(now) - at) > stale_hours * 3600:
        return ""
    age = age_text(at, now, tz=tz)
    head = f"外面的天气（{age}查的）：" if age else "外面的天气："
    return f"# 外面的天气\n{head}{line}"


def banner(record: WeatherRecord, now: float, *, tz: tzinfo | None = None) -> dict[str, Any]:
    """编辑器横幅要的数据（前端自己算"多久之前"，所以这里只给原始时间）。"""

    parts = dict(record.parts)
    return {
        "text": record.line(),
        "parts": parts,
        "city": parts.get("city", ""),
        "temp": parts.get("temp", ""),
        "desc": parts.get("desc", ""),
        "at": float(record.at or 0.0),
        "age": age_text(record.at, now, tz=tz),
        "source": record.source,
    }
