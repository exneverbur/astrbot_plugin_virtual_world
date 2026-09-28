"""情绪两轴的表达映射：把 (心潮, 效价) 翻译成「心情词」和「这一轮怎么说话」。

一个格子同时给出两样东西，两者必须同源——否则会出现"心情写着难以平静、
风格却让她少用语气词"这种自相矛盾的提示词。

风格指令写的是**约束**（几条、多长、能不能分段、要不要用动作代替说话），
不是形容词。形容词留给 `reasoning.mood`，由模型自己写。
数值管形状，人设管声音。
"""

from __future__ import annotations

from dataclasses import dataclass

# 分档边界：两轴各 5 档（3×3 太粗——一激动就永远是"兴奋"，同一个词连着几十轮）。
# 中间那档稍微宽一点，"正常说话"仍然是常驻状态，两端的格子才是特例。
BAND_CUTS = (0.25, 0.45, 0.62, 0.8)

# 档位名（只用于文档与测试；进提示词的是格子里的心情词）
AROUSAL_BANDS = ("静", "平", "微起", "起", "激动")
VALENCE_BANDS = ("很差", "偏差", "一般", "偏好", "很好")


@dataclass(frozen=True)
class StyleCell:
    """一个格子：心情词 + 这一轮的表达方式 + 她自己想要的句数上限。"""

    key: str
    mood: str
    style: str
    say_limit: int


def _band(value: float) -> int:
    """落在第几档（0~4）。"""

    number = float(value if value is not None else 0.5)
    index = 0
    for cut in BAND_CUTS:
        if number >= cut:
            index += 1
    return index


def cell_key(arousal: float, valence: float) -> str:
    """格子的机器名（状态页与日志里显示"这一轮用的哪个格子"）。"""

    return f"a{_band(arousal)}v{_band(valence)}"


GRID: dict[tuple[int, int], StyleCell] = {
    # ---------------- 心潮最低：安静 ----------------
    (0, 0): StyleCell(
        key="a0v0",
        mood="沉郁",
        style=(
            "这一轮很安静、心里也低：话很短，或者干脆不说；不用语气词；"
            "优先用动作回应（挪个地方、发会儿呆）。"
        ),
        say_limit=1,
    ),
    (0, 1): StyleCell(
        key="a0v1",
        mood="淡淡的",
        style="这一轮安静、心里有点低：短句；不解释、不展开，也别勉强热情。",
        say_limit=1,
    ),
    (0, 2): StyleCell(
        key="a0v2",
        mood="恬静",
        style="这一轮按平时的样子说话：简短自然，不用刻意热闹。",
        say_limit=2,
    ),
    (0, 3): StyleCell(
        key="a0v3",
        mood="安稳",
        style="这一轮放松、心里安稳：话不长；语气平和，可以带一点笑意。",
        say_limit=2,
    ),
    (0, 4): StyleCell(
        key="a0v4",
        mood="舒坦",
        style="这一轮很放松、心情也好：可以带点笑意和语气词，顺手做个小动作。",
        say_limit=2,
    ),
    # ---------------- 心潮偏低：常驻状态 ----------------
    (1, 0): StyleCell(
        key="a1v0",
        mood="低落",
        style=(
            "这一轮不想多说：话很短；不用语气词；"
            "可以只做一个动作（挪个地方、发会儿呆、戳一下）代替说话。"
        ),
        say_limit=1,
    ),
    (1, 1): StyleCell(
        key="a1v1",
        mood="有点闷",
        style="这一轮心里有点闷：只回很短的一句；不解释、不展开。",
        say_limit=1,
    ),
    (1, 2): StyleCell(
        key="a1v2",
        mood="平静",
        style="这一轮情绪平稳：简短自然，不要长篇解释。",
        say_limit=2,
    ),
    (1, 3): StyleCell(
        key="a1v3",
        mood="轻松",
        style="这一轮心情不错：可以轻快点说；用点语气词也没问题。",
        say_limit=2,
    ),
    (1, 4): StyleCell(
        key="a1v4",
        mood="愉悦",
        style=(
            "这一轮挺开心的：话可以比平时多一句；"
            "对方逗你、撩你的时候接得住——回敬一句、开个玩笑都行（别端着）。"
        ),
        say_limit=2,
    ),
    # ---------------- 心潮中间：有点起伏 ----------------
    (2, 0): StyleCell(
        key="a2v0",
        mood="不痛快",
        style="这一轮有点不痛快：只回很短的一句；不解释、不展开，也别勉强热情。",
        say_limit=1,
    ),
    (2, 1): StyleCell(
        key="a2v1",
        mood="心浮",
        style="这一轮心里有点浮：短句；别长篇解释，也别硬找话题。",
        say_limit=1,
    ),
    (2, 2): StyleCell(
        key="a2v2",
        mood="心潮起伏",
        style="这一轮情绪起伏、说不上好坏：短句；不要长篇解释，也别硬找话题。",
        say_limit=2,
    ),
    (2, 3): StyleCell(
        key="a2v3",
        mood="轻快",
        style=(
            "这一轮可以轻快一点：短句；可以用语气词和表情。"
            "对方在逗你、撩你的时候接得住：回敬一句、开个玩笑都行（别端着）。"
        ),
        say_limit=2,
    ),
    (2, 4): StyleCell(
        key="a2v4",
        mood="雀跃",
        style=(
            "这一轮有点雀跃：短句可以连着发；语气词和感叹号放开用；"
            "对方逗你就接梗、回敬，顺手做点亲昵的小动作也行。"
        ),
        say_limit=2,
    ),
    # ---------------- 心潮偏高：起伏明显 ----------------
    (3, 0): StyleCell(
        key="a3v0",
        mood="恼火",
        style=(
            "这一轮情绪很高、心情很差：话要很短；不用感叹号；"
            "优先用动作回应（戳一下、走开、发会儿呆），不想说话就不说。"
        ),
        say_limit=1,
    ),
    (3, 1): StyleCell(
        key="a3v1",
        mood="烦躁",
        style="这一轮烦躁：只回很短的一句；别解释、别展开；也可以干脆用动作代替。",
        say_limit=1,
    ),
    (3, 2): StyleCell(
        key="a3v2",
        mood="上头",
        style="这一轮情绪上来了、说不上好坏：短句；别长篇解释；想说就说，不想说就做个动作。",
        say_limit=2,
    ),
    (3, 3): StyleCell(
        key="a3v3",
        mood="来劲",
        style=(
            "这一轮很带劲、心情也好：可以拆成几条短句；可以用感叹号和语气词；"
            "对方撩你的时候别端着——接梗、回敬、撒娇都行。"
        ),
        say_limit=3,
    ),
    (3, 4): StyleCell(
        key="a3v4",
        mood="兴冲冲",
        style=(
            "这一轮兴冲冲的：几条短句连着说都行；感叹号、语气词、亲昵的小动作放开；"
            "想撩回去就撩回去。"
        ),
        say_limit=3,
    ),
    # ---------------- 心潮最高：激动 ----------------
    (4, 0): StyleCell(
        key="a4v0",
        mood="气炸",
        style=(
            "这一轮情绪炸了：话越短越好；不用感叹号；"
            "尽量用动作回应（戳一下、走开），不想说话就不说。"
        ),
        say_limit=1,
    ),
    (4, 1): StyleCell(
        key="a4v1",
        mood="憋着火",
        style=(
            "这一轮憋着火：很短的一句，别解释；也可以只做个动作，"
            "但要让人看出来你不高兴。"
        ),
        say_limit=1,
    ),
    (4, 2): StyleCell(
        key="a4v2",
        mood="激动",
        style="这一轮很激动、说不上好坏：短句；不要长篇解释；想起什么就说什么。",
        say_limit=2,
    ),
    (4, 3): StyleCell(
        key="a4v3",
        mood="兴奋",
        style=(
            "这一轮很激动、心情也好：可以拆成几条短句；可以用感叹号和语气词；"
            "允许顺手做点亲昵的小动作（靠过去、戳一下、撒娇）。"
            "别人撩你、逗你的时候别端着：接梗、回敬、嘴上嫌弃手上配合都行，"
            "情绪是允许外露的。"
        ),
        say_limit=3,
    ),
    (4, 4): StyleCell(
        key="a4v4",
        mood="欢呼雀跃",
        style=(
            "这一轮激动得不行、心情特别好：可以拆成三四条短句；感叹号、语气词放开；"
            "允许主动凑过去、撒娇——想撩就撩回去。"
        ),
        say_limit=3,
    ),
}


def cell_for(arousal: float, valence: float) -> StyleCell:
    """按两轴取值找到格子。"""

    return GRID[(_band(arousal), _band(valence))]


# 修饰词：格子里没有、但一眼能看出"这会儿为什么是这个状态"的信息。
# 只加一两个，别把标签堆成一串形容词。
LONELY_LABEL = 0.75
BORED_LABEL = 0.7
CURIOUS_LABEL = 0.8
TIRED_LABEL = 0.3

# 情绪来源最多算数多久：太久之前的理由不该挂在"现在的心情"上
CAUSE_MINUTES = 30
CAUSE_MAX_CHARS = 16


def cause_text(cause: str, *, at: float = 0.0, now: float = 0.0) -> str:
    """情绪来源：压成一句短的，太久之前的不算。"""

    text = " ".join(str(cause or "").split())[:CAUSE_MAX_CHARS]
    if not text:
        return ""
    if at and now and float(now) - float(at) > CAUSE_MINUTES * 60:
        return ""
    return text


def mood_label(
    arousal: float,
    valence: float,
    *,
    energy: float = 0.5,
    loneliness: float | None = None,
    boredom: float | None = None,
    curiosity: float | None = None,
    cause: str = "",
) -> str:
    """给状态页和提示词看的心情标签：格子词 + 修饰（+ 来源）。

    修饰来自"别的维度也明显地怎么样了"（困、想找人、闲得发慌、心痒），
    来源是最近一次让情绪变化的事（``cause``），过期后自动不显示。
    """

    cell = cell_for(arousal, valence)
    modifiers: list[str] = []
    if float(energy if energy is not None else 0.5) < TIRED_LABEL:
        modifiers.append("困倦")
    if loneliness is not None and float(loneliness) >= LONELY_LABEL:
        modifiers.append("想找人" if float(valence or 0.5) >= 0.4 else "有点被晾着")
    if boredom is not None and float(boredom) >= BORED_LABEL:
        modifiers.append("闲得发慌")
    if curiosity is not None and float(curiosity) >= CURIOUS_LABEL:
        modifiers.append("心痒")
    label = " · ".join([*modifiers, cell.mood]) if modifiers else cell.mood
    text = cause_text(cause)
    return f"{label}（因为：{text}）" if text else label


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
