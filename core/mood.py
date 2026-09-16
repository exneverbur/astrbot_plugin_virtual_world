"""情绪两轴的表达映射：把 (心潮, 效价) 翻译成「心情词」和「这一轮怎么说话」。

一个格子同时给出两样东西，两者必须同源——否则会出现"心情写着难以平静、
风格却让她少用语气词"这种自相矛盾的提示词。

风格指令写的是**约束**（几条、多长、能不能分段、要不要用动作代替说话），
不是形容词。形容词留给 `reasoning.mood`，由模型自己写。
数值管形状，人设管声音。
"""

from __future__ import annotations

from dataclasses import dataclass

# 分档边界：中间带宽一点，"正常说话"才是常驻状态，两端才是特例
BAND_LOW = 0.35
BAND_HIGH = 0.65


@dataclass(frozen=True)
class StyleCell:
    """一个格子：心情词 + 这一轮的表达方式 + 她自己想要的句数上限。"""

    key: str
    mood: str
    style: str
    say_limit: int


def _band(value: float) -> str:
    number = float(value if value is not None else 0.5)
    if number < BAND_LOW:
        return "low"
    if number > BAND_HIGH:
        return "high"
    return "mid"


GRID: dict[tuple[str, str], StyleCell] = {
    ("low", "high"): StyleCell(
        key="calm+positive",
        mood="舒坦",
        style="这一轮放松一点：1~2 句、总共 60 字以内；语气平和，可以带一点笑意。",
        say_limit=2,
    ),
    ("low", "mid"): StyleCell(
        key="calm+neutral",
        mood="平静",
        style="这一轮按平时的样子说话：1~2 句、简短自然，不用刻意热闹。",
        say_limit=2,
    ),
    ("low", "low"): StyleCell(
        key="calm+negative",
        mood="低落",
        style=(
            "这一轮不想多说：最多 1 句、20 字以内；不用语气词；"
            "可以只做一个动作（挪个地方、发会儿呆、戳一下）代替说话。"
        ),
        say_limit=1,
    ),
    ("mid", "high"): StyleCell(
        key="stirred+positive",
        mood="轻快",
        style="这一轮可以轻快一点：1~2 句、总共 60 字以内；可以用语气词和表情。",
        say_limit=2,
    ),
    ("mid", "mid"): StyleCell(
        key="stirred+neutral",
        mood="平静",
        style="这一轮情绪平稳：1~2 句、简短自然，不要长篇解释。",
        say_limit=2,
    ),
    ("mid", "low"): StyleCell(
        key="stirred+negative",
        mood="不痛快",
        style="这一轮有点不痛快：最多 1 句、30 字以内；不解释、不展开，也别勉强热情。",
        say_limit=1,
    ),
    ("high", "high"): StyleCell(
        key="excited+positive",
        mood="兴奋",
        style=(
            "这一轮很激动、心情也好：可以拆成 2~3 条短句；可以用感叹号和语气词；"
            "允许顺手做点亲昵的小动作。"
        ),
        say_limit=3,
    ),
    ("high", "mid"): StyleCell(
        key="excited+neutral",
        mood="心潮起伏",
        style="这一轮情绪起伏、说不上好坏：最多 2 条短句；不要长篇解释，也别硬找话题。",
        say_limit=2,
    ),
    ("high", "low"): StyleCell(
        key="excited+negative",
        mood="恼火",
        style=(
            "这一轮情绪很高、心情很差：最多 1 句、15 字以内；不用感叹号；"
            "优先用动作回应（戳一下、走开、发会儿呆），不想说话就不说。"
        ),
        say_limit=1,
    ),
}


def cell_for(arousal: float, valence: float) -> StyleCell:
    """按两轴取值找到格子。"""

    return GRID[(_band(arousal), _band(valence))]


def mood_label(arousal: float, valence: float, *, energy: float = 0.5) -> str:
    """给状态页和提示词看的心情词。

    精力低的时候加个「困倦」修饰，而不是单独占一个格——她累了所以话少，
    那是精力在收紧，不该变成另一种心情。
    """

    label = cell_for(arousal, valence).mood
    if float(energy if energy is not None else 0.5) < 0.3:
        return f"困倦 · {label}"
    return label


def style_block(
    cell: StyleCell, *, say_limit: int, group: bool = True, note: str = ""
) -> str:
    """这一轮的风格段：直接进提示词的最后一段。"""

    where = "群聊" if group else "私聊"
    extra = f"\n{note}" if note else ""
    return (
        "# 这一轮的表达方式\n"
        f"{cell.style}{extra}\n"
        f"（仍然是你，只是这一轮的表达形态如此：{where}里最多说 {say_limit} 条，别超。）"
    )


SELF_CARE_NOTE = (
    "（你现在更想自己待会儿：可以主动去做点让自己缓过来的事，"
    "比如去窗边吹吹风、听首歌、躺一会儿。）"
)


# ---------------- 关键词兜底 ----------------
#
# 主路径是主模型在 JSON 里给 valence_delta（它真的懂语义）。这张表只在消息
# 明显带情绪时垫一点，避免小模型漏字段时情绪系统完全没有输入。
_HUG_WORDS = ("抱抱", "抱一下", "摸摸头", "贴贴", "亲亲")
_POSITIVE_WORDS = (
    "谢谢",
    "辛苦",
    "喜欢你",
    "爱你",
    "真棒",
    "好可爱",
    "厉害",
    "乖",
    "奖励",
)
_NEGATIVE_WORDS = (
    "讨厌你",
    "烦死",
    "闭嘴",
    "滚",
    "生气",
    "恶心",
    "垃圾",
    "傻",
    "笨蛋",
    "骂你",
)


def keyword_signal(text: str) -> str:
    """从一句话里猜情绪方向：``hug`` / ``positive`` / ``negative`` / 空。"""

    content = str(text or "")
    if not content:
        return ""
    if any(word in content for word in _HUG_WORDS):
        return "hug"
    if any(word in content for word in _NEGATIVE_WORDS):
        return "negative"
    if any(word in content for word in _POSITIVE_WORDS):
        return "positive"
    return ""
