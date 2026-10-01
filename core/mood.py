"""情绪两轴的表达映射：把 (心潮, 效价) 翻译成「心情词」和「这一轮怎么说话」。

一个格子同时给出两样东西，两者必须同源——否则会出现"心情写着难以平静、
风格却让她少用语气词"这种自相矛盾的提示词。

风格指令写的是**约束**（几条、多长、能不能分段、要不要用动作代替说话），
不是形容词。形容词留给 `reasoning.mood`，由模型自己写。
数值管形状，人设管声音。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


# ---------------- 今天的基调 ----------------
#
# 每天（或一觉睡醒）掷一次今天的「调子」。她不解释，但那一天各条曲线走得快慢
# 确实不一样：有的日子就是懒懒的，有的日子特别想找人说话，有的日子说不清。
#
# 之所以挂在这张表上，是因为"心情"本身是跟着数值走的——基调不改变她是谁，
# 只改变数值涨落的速度，剩下的照旧由数值、事件和两轴自己长出来。
#
# `rates` 是**变化速率的倍率**（1.0 = 跟平时一样）：
#   energy     精力掉得快不快
#   loneliness 孤独涨得快不快（越大越容易想找人）
#   curiosity  好奇心涨得快不快（越大越想找新鲜事）
#   boredom    无聊涨得快不快（越大越坐不住、越想换地方）
#   affect     心潮回落得快不快（越大越不容易上头，上头了也散得快）
#   valence    心情偏移回落得快不快（越大越快忘掉）


@dataclass(frozen=True)
class DayMood:
    """一天的基调：一个名字 + 一句进提示词的说明 + 各条曲线的倍率。"""

    id: str
    label: str
    hint: str
    rates: dict[str, float]


DAY_MOODS: dict[str, DayMood] = {
    "calm": DayMood(
        id="calm",
        label="平静",
        hint="今天跟平常没什么两样，照常过日子就行。",
        rates={},
    ),
    "lazy": DayMood(
        id="lazy",
        label="懒散",
        hint="今天提不起劲：精力掉得慢、也不怎么想知道新东西，坐得住，容易发呆。",
        rates={
            "energy": 0.75,
            "curiosity": 0.70,
            "boredom": 1.30,
            "affect": 0.85,
            "loneliness": 1.10,
        },
    ),
    "lively": DayMood(
        id="lively",
        label="活跃",
        hint="今天精神头足：想知道新鲜事、容易被逗起来，玩久了也累得快。",
        rates={
            "energy": 1.15,
            "curiosity": 1.35,
            "boredom": 1.20,
            "affect": 1.25,
            "valence": 1.10,
        },
    ),
    "clingy": DayMood(
        id="clingy",
        label="黏人",
        hint="今天格外想有人陪：一个人待着更容易发闷，心里那点情绪也挂得更久。",
        rates={
            "loneliness": 1.50,
            "valence": 0.90,
            "boredom": 1.10,
            "curiosity": 0.90,
        },
    ),
    "solitary": DayMood(
        id="solitary",
        label="想独处",
        hint="今天不太想应付人：一个人待着也挺好，倒是有心思鼓捣点自己的事。",
        rates={
            "loneliness": 0.60,
            "curiosity": 1.25,
            "affect": 0.85,
            "boredom": 1.15,
        },
    ),
    "vague": DayMood(
        id="vague",
        label="说不上来",
        hint="今天心里有点说不清的毛躁：没出什么事，但你不太讲得清自己是什么感觉。",
        rates={
            "energy": 0.95,
            "loneliness": 1.05,
            "curiosity": 1.05,
            "boredom": 1.05,
            "affect": 1.10,
            "valence": 0.85,
        },
    ),
}

DAY_MOOD_WEIGHTS: dict[str, float] = {
    "calm": 0.26,
    "lazy": 0.18,
    "lively": 0.16,
    "clingy": 0.15,
    "solitary": 0.15,
    "vague": 0.10,
}
"""各基调出现的权重：平静占大头，别的加起来才是"今天有点不一样"。"""

DAY_MOOD_RATE_KEYS = ("energy", "loneliness", "curiosity", "boredom", "affect", "valence")
"""能被基调调的那几条速率。"""

# 基调还会顺手拨一下**决策分支**的权重：数值只决定"哪件事够格了"，
# 权重决定"几件事同时够格时她挑哪一件"。分支名和 Decider.rule_plan 里的一一对应：
#   reach_out 去找人说话 / search 上网查东西 / wander 换个地方发呆 / read 去看书
# 没写到的分支按 1.0 算。除数是这套：权重一样就退回原来的优先级（不掷骰子）。
DAY_MOOD_BRANCHES: dict[str, dict[str, float]] = {
    "calm": {},
    "lazy": {"reach_out": 0.90, "search": 0.55, "wander": 0.70, "read": 1.30, "cuddle": 0.80},
    "lively": {"reach_out": 1.10, "search": 1.50, "wander": 1.40, "read": 0.85, "cuddle": 1.20},
    "clingy": {"reach_out": 2.00, "search": 0.80, "wander": 0.85, "read": 1.00, "cuddle": 1.70},
    "solitary": {"reach_out": 0.45, "search": 1.20, "wander": 1.20, "read": 1.50, "cuddle": 0.35},
    # 说不上来的一天：没有明显偏向，但也不完全按老顺序来
    "vague": {"reach_out": 1.15, "search": 0.95, "wander": 1.20, "read": 1.05, "cuddle": 1.10},
}

DAY_BRANCH_KEYS = ("reach_out", "search", "wander", "read", "cuddle")

VAGUE_WORDS = ("说不上来", "心浮", "没由来的烦", "空落落", "提不起劲", "有点飘")
"""模糊档用的词：同一天用同一个，别每轮换一个。"""

VAGUE_CELLS = ("a2v2", "a3v2", "a4v2")
"""只剩这几格会换成模糊词——它们的原文本来就写着"说不上好坏"。

平静 / 来劲 / 气炸 这些是**说得清**的，硬换成"说不清"反而假。
"""


def roll_day_mood(rng: Any) -> str:
    """按权重掷一次今天的基调。"""

    ids = list(DAY_MOOD_WEIGHTS)
    total = sum(DAY_MOOD_WEIGHTS[item] for item in ids) or 1.0
    point = float(rng.random()) * total
    acc = 0.0
    for item in ids:
        acc += DAY_MOOD_WEIGHTS[item]
        if point < acc:
            return item
    return ids[-1]


def day_mood_info(day_mood: str) -> DayMood | None:
    """按 id 取基调；空 / 不认识的 id 返回 None（等于没有基调）。"""

    return DAY_MOODS.get(str(day_mood or ""))


def day_mood_rate(day_mood: str, key: str, *, strength: float = 1.0) -> float:
    """某条速率今天的倍率。``strength`` 是整体强度（0 = 完全按平时算）。"""

    info = day_mood_info(day_mood)
    if info is None:
        return 1.0
    try:
        factor = float(info.rates.get(str(key), 1.0))
    except (TypeError, ValueError):
        return 1.0
    try:
        weight = float(strength)
    except (TypeError, ValueError):
        weight = 1.0
    weight = max(0.0, min(1.0, weight))
    return 1.0 + (factor - 1.0) * weight


def day_mood_rates(day_mood: str, *, strength: float = 1.0) -> dict[str, float]:
    """一整组倍率（没配到的那几条是 1.0）。"""

    return {
        key: day_mood_rate(day_mood, key, strength=strength)
        for key in DAY_MOOD_RATE_KEYS
    }


def day_mood_branch(day_mood: str, branch: str, *, strength: float = 1.0) -> float:
    """今天这个决策分支的权重。认不出基调 / 没写这条分支就是 1.0。"""

    info = day_mood_info(day_mood)
    if info is None:
        return 1.0
    try:
        factor = float(DAY_MOOD_BRANCHES.get(info.id, {}).get(str(branch), 1.0))
    except (TypeError, ValueError):
        return 1.0
    try:
        weight = float(strength)
    except (TypeError, ValueError):
        weight = 1.0
    weight = max(0.0, min(1.0, weight))
    return 1.0 + (factor - 1.0) * weight


def day_mood_branches(day_mood: str, *, strength: float = 1.0) -> dict[str, float]:
    """一整组分支权重。"""

    return {
        name: day_mood_branch(day_mood, name, strength=strength)
        for name in DAY_BRANCH_KEYS
    }


def vague_word_for(day_key: str) -> str:
    """模糊档这一天用哪个词：按日期定，同一天不会来回变。"""

    text = str(day_key or "")
    if not text:
        return VAGUE_WORDS[0]
    acc = 0
    for char in text:
        acc = (acc * 31 + ord(char)) % 1000003
    return VAGUE_WORDS[acc % len(VAGUE_WORDS)]


# 修饰词：格子里没有、但一眼能看出"这会儿为什么是这个状态"的信息。
# 只加一两个，别把标签堆成一串形容词。
LONELY_LABEL = 0.75
BORED_LABEL = 0.7
CURIOUS_LABEL = 0.8
DESIRE_LABEL = 0.7
TIRED_LABEL = 0.3


def desire_text(value: float) -> str:
    """欲求到什么程度了（状态页与提示词共用一句话）。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if number >= 0.85:
        return "很想被人抱住、碰到"
    if number >= 0.7:
        return "有点想往他身边凑、贴一贴"
    if number >= 0.4:
        return "偶尔会想被摸摸头"
    return "还好，不怎么想被人碰"

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
    desire: float | None = None,
    cause: str = "",
    vague: str = "",
) -> str:
    """给状态页和提示词看的心情标签：格子词 + 修饰（+ 来源）。

    修饰来自"别的维度也明显地怎么样了"（困、想找人、闲得发慌、心痒），
    来源是最近一次让情绪变化的事（``cause``），过期后自动不显示。

    ``vague`` 是「模糊档」：这一天的基调就是"说不上来"时，本来就写着
    "说不上好坏"的那几格（见 ``VAGUE_CELLS``）换成这个词——她照样有起伏，
    只是自己讲不清。说得清的那些格子（平静 / 来劲 / 气炸）不受影响。
    """

    cell = cell_for(arousal, valence)
    if vague and cell.key in VAGUE_CELLS:
        cell = StyleCell(key=cell.key, mood=vague, style=cell.style, say_limit=cell.say_limit)
    modifiers: list[str] = []
    if float(energy if energy is not None else 0.5) < TIRED_LABEL:
        modifiers.append("困倦")
    if loneliness is not None and float(loneliness) >= LONELY_LABEL:
        modifiers.append("想找人" if float(valence or 0.5) >= 0.4 else "有点被晾着")
    if boredom is not None and float(boredom) >= BORED_LABEL:
        modifiers.append("闲得发慌")
    if curiosity is not None and float(curiosity) >= CURIOUS_LABEL:
        modifiers.append("心痒")
    if desire is not None and float(desire) >= DESIRE_LABEL:
        modifiers.append("想贴着人")
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
