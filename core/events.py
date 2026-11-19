"""事件系统：能力值、掷骰判定、事件包、线索。

这一层刻意不碰 IO、不调模型：模型给的文本在这里被解析、夹紧、算成结果，
所以边界情况（难度、修正、四档分档、能力值上限、线索收敛）都能单独测一遍。

几个约定：

- **能力值**是慢变量（体力/智力/灵巧/心性），只由事件结果改变，几乎不随时间衰减；
  和"精力"不是一回事：精力睡一觉就回来，能力值是她的底子。
- **判定**是纯计算：``p = 能力值 × 情境修正 × 难度倍率``，夹在 0.05~0.95，
  掷一次分四档。**难度越大越难**（0.5 是中性），而且只有 ``check_probability``
  一处公式：提示词里的预览和真正掷骰必须同式，否则她的"我觉得能行"和实际概率
  会各说各话。每次判定都要能写成一行人话，进日志和调试输出。
- **主观档位**（她觉得自己行不行）和客观概率**允许不一致**：心潮高会高估自己，
  精力低会低估。这个偏差就是"以为稳却翻车"的来源。
- **线索**是一串事件：一步步追加"她选了什么 → 结果如何"，续线时整条喂回去。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import aliases

# 注意：``json_actions`` 在模块顶层不能导（它 → source_events → state → 本模块，成环）。
# 修复函数只在 :func:`parse_json_object` 里局部导入。

# ---------------- 能力值 ----------------

ABILITIES: tuple[str, ...] = ("stamina", "wits", "dexterity", "composure")

ABILITY_LABELS: dict[str, str] = {
    "stamina": "体力",
    "wits": "智力",
    "dexterity": "灵巧",
    "composure": "心性",
}

# 能力值的人话描述：提示词里只写这些，不写数字。
ABILITY_HINTS: tuple[tuple[float, str], ...] = (
    (0.25, "不太行"),
    (0.45, "一般"),
    (0.65, "还算顺手"),
    (0.8, "挺熟练"),
    (1.01, "很在行"),
)

DEFAULT_ABILITIES: dict[str, float] = {
    "stamina": 0.6,
    "wits": 0.6,
    "dexterity": 0.55,
    "composure": 0.6,
}

# 单次事件最多改变多少、每天累计最多改变多少（护栏，防止数值膨胀）
ABILITY_STEP_LIMIT = 0.05
ABILITY_DAY_LIMIT = 0.10


def default_abilities() -> dict[str, float]:
    return dict(DEFAULT_ABILITIES)


def normalize_abilities(raw: Any) -> dict[str, float]:
    """把配置/存档里那份能力值整理成合法的四项。"""

    data = raw if isinstance(raw, dict) else {}
    result: dict[str, float] = {}
    for name in ABILITIES:
        try:
            value = float(data.get(name, DEFAULT_ABILITIES[name]))
        except (TypeError, ValueError):
            value = DEFAULT_ABILITIES[name]
        result[name] = max(0.05, min(1.0, value))
    return result


def ability_hint(value: float) -> str:
    """能力值的人话档位（提示词里用这个，不用数字）。"""

    for limit, text in ABILITY_HINTS:
        if float(value) < limit:
            return text
    return ABILITY_HINTS[-1][1]


def ability_labels(names: list[str] | tuple[str, ...]) -> str:
    return "、".join(ABILITY_LABELS.get(str(name), str(name)) for name in names)


def clamp_ability_delta(
    deltas: dict[str, float],
    *,
    spent_today: dict[str, float] | None = None,
    day: str = "",
    today: str = "",
) -> tuple[dict[str, float], dict[str, float]]:
    """把模型给的能力值变化夹到护栏内，返回 ``(实际生效, 新的今日累计)``。

    ``spent_today`` 是这一项今天已经用掉的变化量（带符号），跨天自动清零。
    """

    spent = dict(spent_today or {})
    if day != today:
        spent = {}
    applied: dict[str, float] = {}
    for name, raw in (deltas or {}).items():
        key = str(name).strip()
        if key not in ABILITIES:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not value:
            continue
        value = max(-ABILITY_STEP_LIMIT, min(ABILITY_STEP_LIMIT, value))
        used = float(spent.get(key, 0.0) or 0.0)
        if value > 0:
            room = ABILITY_DAY_LIMIT - max(0.0, used)
            value = min(value, max(0.0, room))
        else:
            room = ABILITY_DAY_LIMIT + min(0.0, used)
            value = max(value, -max(0.0, room))
        if not value:
            continue
        applied[key] = round(value, 4)
        spent[key] = round(used + value, 4)
    return applied, spent


def apply_ability_delta(abilities: dict[str, float], delta: dict[str, float]) -> dict[str, float]:
    """把变化写进能力值（依旧夹在 0.05~1）。"""

    result = normalize_abilities(abilities)
    for name, value in (delta or {}).items():
        if name in result:
            result[name] = max(0.05, min(1.0, result[name] + float(value)))
    return result


# ---------------- 判定 ----------------

CHECK_FLOOR = 0.05
CHECK_CEIL = 0.95

DIFFICULTY_NEUTRAL = 0.5
"""难度 0.5 = 不额外加难也不减难（倍率 1.0）。"""


def difficulty_factor(difficulty: float) -> float:
    """难度 → 概率倍率：**难度越大越难**。

    以前这里是拿难度直接当倍率用（``p = 能力 × 难度``），于是"难度 0.8 的棘手事"
    反而比 0.3 的轻松事更容易成——语义正好反了。现在 0.5 是中性，0.8 压到 ×0.4，
    0.2 提到 ×1.6，与提示词里"0.2 很轻松 / 0.8 相当棘手"的说法一致。
    """

    try:
        value = max(0.05, min(0.95, float(difficulty)))
    except (TypeError, ValueError):
        value = DIFFICULTY_NEUTRAL
    return max(0.05, (1.0 - value) / (1.0 - DIFFICULTY_NEUTRAL))


def modifier_factor(modifiers: list[tuple[str, float]] | None) -> float:
    """情境修正连乘（坏值跳过，不让一个脏数字毁掉整次判定）。"""

    factor = 1.0
    for _label, value in modifiers or []:
        try:
            factor *= float(value)
        except (TypeError, ValueError):
            continue
    return factor


def check_probability(
    ability_value: float,
    difficulty: float,
    modifiers: list[tuple[str, float]] | None = None,
) -> float:
    """一次判定的成功概率——**整个工程只有这一处公式**。

    提示词里的预览（她"觉得"行不行之前看的客观概率）和真正掷骰必须调这个函数，
    否则两处各算各的，校准就无从谈起。
    """

    try:
        ability = float(ability_value)
    except (TypeError, ValueError):
        ability = 0.0
    probability = ability * modifier_factor(modifiers) * difficulty_factor(difficulty)
    return max(CHECK_FLOOR, min(CHECK_CEIL, probability))


TIER_GREAT = "great"
TIER_SUCCESS = "success"
TIER_NARROW = "narrow"
TIER_FAIL = "fail"
TIER_SKIP = "skip"
"""她主动选了「不做」：不掷骰，也不长能力值。"""

TIER_LABELS: dict[str, str] = {
    TIER_GREAT: "大成功",
    TIER_SUCCESS: "成功",
    TIER_NARROW: "勉强成功",
    TIER_FAIL: "失败",
    TIER_SKIP: "主动放弃",
}

TIER_EMOTION: dict[str, dict[str, float]] = {
    TIER_GREAT: {"valence": 0.05, "affect": 0.05},
    TIER_SUCCESS: {"valence": 0.03},
    TIER_NARROW: {"valence": 0.01, "affect": 0.02},
    # 失败是"憋着一股劲"：心情往下、心潮往上（高唤醒负效价）。
    # 幅度刻意压得小：连着两次没成不该把她一路推到谷底，教训已经记进能力值了。
    TIER_FAIL: {"valence": -0.02, "affect": 0.03},
    TIER_SKIP: {},
}
"""判定档位 → 情绪脉冲的基准值（保守，可关；见 ``events.result_emotion``）。"""


def tier_emotion(tier: str) -> dict[str, float]:
    """某一档判定给她带来的情绪脉冲（没有就返回空）。"""

    return dict(TIER_EMOTION.get(str(tier or ""), {}))


# 主观档位：她觉得自己行不行（给主模型看的人话，不是概率）
HINT_SURE = "有把握"
HINT_OK = "差不多"
HINT_RISKY = "有点悬"
HINT_HOPELESS = "基本没戏"

SUBJECTIVE_HINTS = (HINT_SURE, HINT_OK, HINT_RISKY, HINT_HOPELESS)

# 主观偏差：心潮高容易高估自己，精力低 / 心性低容易低估
HINT_BIAS_AFFECT = 0.18
HINT_BIAS_ENERGY = 0.12
HINT_BIAS_COMPOSURE = 0.10


def tier_for(margin: float) -> str:
    """按"掷骰值和目标值的差"分四档。"""

    value = float(margin)
    if value >= 0.25:
        return TIER_GREAT
    if value >= 0.0:
        return TIER_SUCCESS
    if value >= -0.15:
        return TIER_NARROW
    return TIER_FAIL


@dataclass
class CheckResult:
    """一次判定的全部中间量：看得见才好调。"""

    ability: str = ""
    ability_value: float = 0.0
    difficulty: float = 0.5
    modifiers: list[tuple[str, float]] = field(default_factory=list)
    probability: float = 0.0
    roll: float = 0.0
    margin: float = 0.0
    tier: str = TIER_FAIL

    @property
    def ok(self) -> bool:
        return self.tier in (TIER_GREAT, TIER_SUCCESS, TIER_NARROW, TIER_SKIP)

    @property
    def label(self) -> str:
        return TIER_LABELS.get(self.tier, self.tier)

    def line(self) -> str:
        """一行人话：能力值 × 修正 × 难度 = p → 掷骰 → 档位。"""

        parts = [f"{ABILITY_LABELS.get(self.ability, self.ability or '能力')} {self.ability_value:.2f}"]
        for label, factor in self.modifiers:
            if abs(float(factor) - 1.0) < 1e-6:
                continue
            if float(factor) >= 1.0:
                parts.append(f"{label} ×{float(factor):.2f}")
            else:
                parts.append(f"{label} ×{float(factor):.2f}")
        parts.append(f"难度 {self.difficulty:.2f}")
        head = " × ".join(parts)
        return (
            f"{head} = {self.probability:.2f} → 掷 {self.roll:.2f} → {self.label}"
        )


def roll_check(
    *,
    ability: str,
    ability_value: float,
    difficulty: float,
    modifiers: list[tuple[str, float]] | None = None,
    roll: Callable[[], float] | None = None,
) -> CheckResult:
    """掷一次骰子。``roll`` 便于测试注入固定值。"""

    try:
        diff = max(0.05, min(0.95, float(difficulty)))
    except (TypeError, ValueError):
        diff = DIFFICULTY_NEUTRAL
    probability = check_probability(ability_value, diff, modifiers)
    dice = float(roll()) if callable(roll) else 0.5
    dice = max(0.0, min(1.0, dice))
    margin = probability - dice
    return CheckResult(
        ability=str(ability),
        ability_value=float(ability_value),
        difficulty=diff,
        modifiers=list(modifiers or []),
        probability=round(probability, 4),
        roll=round(dice, 4),
        margin=round(margin, 4),
        tier=tier_for(margin),
    )


def subjective_hint(
    probability: float,
    *,
    affect: float = 0.3,
    energy: float = 0.6,
    composure: float = 0.6,
) -> str:
    """她觉得自己能不能成（带情绪偏差，故意允许和客观概率不一致）。

    心潮高 → 高估自己；精力低 → 低估；心性低 → 高估。
    """

    felt = float(probability)
    felt += (float(affect) - 0.35) * HINT_BIAS_AFFECT
    felt -= max(0.0, 0.5 - float(energy)) * HINT_BIAS_ENERGY
    felt += max(0.0, 0.6 - float(composure)) * HINT_BIAS_COMPOSURE
    if felt >= 0.7:
        return HINT_SURE
    if felt >= 0.45:
        return HINT_OK
    if felt >= 0.22:
        return HINT_RISKY
    return HINT_HOPELESS


def surprise(probability: float, hint: str) -> float:
    """主观与客观的落差：落差越大，这一下越"意外"（给情绪脉冲用）。"""

    expect = {
        HINT_SURE: 0.8,
        HINT_OK: 0.55,
        HINT_RISKY: 0.32,
        HINT_HOPELESS: 0.12,
    }.get(str(hint), 0.5)
    return round(abs(float(probability) - expect), 4)


# ---------------- 情境修正 ----------------

MOD_ENERGY_LOW = 0.85
MOD_AFFECT_HIGH = 0.95
MOD_SUGGEST_ALIGNED = 1.20
MOD_SUGGEST_SINGLE = 1.10
MOD_REPEAT_FAIL = 0.95

ENERGY_LOW_LINE = 0.35
AFFECT_HIGH_LINE = 0.7


def situational_modifiers(
    *,
    energy: float = 0.6,
    affect: float = 0.3,
    alignment: int = 0,
    failed_before: bool = False,
) -> list[tuple[str, float]]:
    """判定用的情境修正。

    ``alignment``：群友建议的可用条数（0 = 没人给建议，1 = 一条可用，≥2 = 多条一致）。
    """

    result: list[tuple[str, float]] = []
    if float(energy) < ENERGY_LOW_LINE:
        result.append(("疲惫", MOD_ENERGY_LOW))
    if float(affect) > AFFECT_HIGH_LINE:
        result.append(("太兴奋", MOD_AFFECT_HIGH))
    if int(alignment) >= 2:
        result.append(("有建议", MOD_SUGGEST_ALIGNED))
    elif int(alignment) == 1:
        result.append(("有建议", MOD_SUGGEST_SINGLE))
    if failed_before:
        result.append(("上次没成", MOD_REPEAT_FAIL))
    return result


# ---------------- 事件包 ----------------

TIER_MICRO = "micro"
TIER_SMALL = "small"
TIER_BIG = "big"
EVENT_TIERS = (TIER_MICRO, TIER_SMALL, TIER_BIG)

TIER_WEIGHT = {TIER_MICRO: 1, TIER_SMALL: 2, TIER_BIG: 3}

KIND_SOLO = "solo"
KIND_NEED_INTERVENE = "need_intervene"

MODE_REAL = "real"
MODE_IMAGINED = "imagined"

SCOPE_ACT = "act"
SCOPE_SESSION = "session"
SCOPE_DAY = "day"

# ---- 事件跨度（T15-S2） ----
#
# 一件事不是都该在一回合里结束：买瓶酱油是 ``scene``，修漏水的水管是
# ``hours``，跟房东扯押金是 ``days``。跨度决定**节奏**和**上限**，
# 但什么时候结束由剧情判（check / stake 结算 + 没有未兑现的伏笔），
# 不是到点硬收尾——见 ``core/engine.py`` 的 ``_resolve_event_step`` / ``_continue_thread``。

SPAN_SCENE = "scene"
SPAN_HOURS = "hours"
SPAN_DAYS = "days"
EVENT_SPANS: tuple[str, ...] = (SPAN_SCENE, SPAN_HOURS, SPAN_DAYS)

SPAN_LABELS = {SPAN_SCENE: "一回合", SPAN_HOURS: "几小时", SPAN_DAYS: "跨天"}

# 每档的天花板：步数 / 存活时长（分钟）。配置项 ``scene_max_minutes`` /
# ``hours_max_hours`` / ``days_max_days`` 会覆盖时长；缺键时用这里的默认。
SPAN_MAX_STEPS = {SPAN_SCENE: 6, SPAN_HOURS: 12, SPAN_DAYS: 40}
SPAN_MAX_MINUTES = {
    SPAN_SCENE: 120.0,
    SPAN_HOURS: 72.0 * 60.0,
    SPAN_DAYS: 30.0 * 24.0 * 60.0,
}

# 每步最小间隔（秒）：跨度越大节奏越慢，防"一天推八步"式刷屏。
SPAN_MIN_GAP_SECONDS = {
    SPAN_SCENE: 0.0,
    SPAN_HOURS: 30.0 * 60.0,
    SPAN_DAYS: 6.0 * 3600.0,
}

# ---- 独占（T15-S2） ----
EXCLUSIVITY_EXCLUSIVE = "exclusive"
EXCLUSIVITY_AMBIENT = "ambient"
EVENT_EXCLUSIVITIES: tuple[str, ...] = (EXCLUSIVITY_EXCLUSIVE, EXCLUSIVITY_AMBIENT)

# ---- 事的代价（T16） ----
#
# 以前失败只是"没长经验"：大事件输了既不掉精力也不掉人情，她自然不可能怕。
# 这里给每次没成（含勉强成功）配一份**量小但真实**的代价，按 ``stake_kind``
# 决定从哪扣：精力 / 人情 / 东西 / 机会。三条硬护栏都在
# :func:`plan_event_cost` 里算好：精力不下 0.10、单日总量不超 0.30、
# 同一线程连续第 3 次失败只说话不扣。

STAKE_ENERGY = "energy"
STAKE_RELATION = "relation"
STAKE_ITEM = "item"
STAKE_OPPORTUNITY = "opportunity"
STAKE_NONE = "none"

EVENT_STAKES: tuple[str, ...] = (
    STAKE_ENERGY,
    STAKE_RELATION,
    STAKE_ITEM,
    STAKE_OPPORTUNITY,
    STAKE_NONE,
)

STAKE_LABELS: dict[str, str] = {
    STAKE_ENERGY: "精力",
    STAKE_RELATION: "人情",
    STAKE_ITEM: "东西",
    STAKE_OPPORTUNITY: "机会",
    STAKE_NONE: "说不清",
}

_STAKE_ALIASES: dict[str, str] = {
    "energy": STAKE_ENERGY,
    "stamina": STAKE_ENERGY,
    "strength": STAKE_ENERGY,
    "body": STAKE_ENERGY,
    "精力": STAKE_ENERGY,
    "体力": STAKE_ENERGY,
    "力气": STAKE_ENERGY,
    "身体": STAKE_ENERGY,
    "relation": STAKE_RELATION,
    "bond": STAKE_RELATION,
    "trust": STAKE_RELATION,
    "face": STAKE_RELATION,
    "关系": STAKE_RELATION,
    "人情": STAKE_RELATION,
    "面子": STAKE_RELATION,
    "信任": STAKE_RELATION,
    "好感": STAKE_RELATION,
    "item": STAKE_ITEM,
    "object": STAKE_ITEM,
    "thing": STAKE_ITEM,
    "money": STAKE_ITEM,
    "东西": STAKE_ITEM,
    "物品": STAKE_ITEM,
    "物件": STAKE_ITEM,
    "钱": STAKE_ITEM,
    "opportunity": STAKE_OPPORTUNITY,
    "chance": STAKE_OPPORTUNITY,
    "timing": STAKE_OPPORTUNITY,
    "机会": STAKE_OPPORTUNITY,
    "时机": STAKE_OPPORTUNITY,
    "名额": STAKE_OPPORTUNITY,
    "none": STAKE_NONE,
    "nothing": STAKE_NONE,
    "无": STAKE_NONE,
    "没有": STAKE_NONE,
}


def stake_kind_of(raw: Any) -> str:
    """把模型写的"输在哪儿"收敛到固定枚举；认不出就算 ``none``（按精力兜底）。"""

    text = " ".join(str(raw or "").split()).strip().lower()
    if not text:
        return STAKE_NONE
    if text in EVENT_STAKES:
        return text
    return _STAKE_ALIASES.get(text, STAKE_NONE)


COST_TIER_NARROW = "narrow"
COST_TIER_FAIL = "fail"
COST_TIER_BIG_FAIL = "big_fail"
"""代价档位：勉强成功 / 失败 / 大事件失败（都来自 ``TIER_NARROW`` / ``TIER_FAIL``）。"""

EVENT_COST_TIERS: tuple[str, ...] = (
    COST_TIER_NARROW,
    COST_TIER_FAIL,
    COST_TIER_BIG_FAIL,
)

EVENT_COST_ENERGY: dict[str, float] = {
    COST_TIER_NARROW: 0.05,
    COST_TIER_FAIL: 0.09,
    COST_TIER_BIG_FAIL: 0.12,
}
"""精力档位：勉强成功 −0.05、失败 −0.09、大事件失败 −0.12。"""

EVENT_COST_RELATION: dict[str, float] = {
    COST_TIER_NARROW: -0.02,
    COST_TIER_FAIL: -0.03,
    COST_TIER_BIG_FAIL: -0.04,
}
"""人情档位（负值）：扣当事人的好感度。"""

EVENT_COST_NEUTRAL = 0.03
"""``stake_kind`` 缺失 / 说不清时的兜底：照扣 0.03 精力，只是没有名字。"""

EVENT_COST_ENERGY_FLOOR = 0.10
"""精力不得低于这个值：代价再大也不把人扣瘫。"""

EVENT_COST_DAY_LIMIT = 0.30
"""单个会话单日内事件扣减总量上限（按幅度算，不超过这个数）。"""

EVENT_COST_STREAK_LIMIT = 3
"""同一线程连续第 N 次失败只说话不扣：别让模型学会"瘫着最省事"。"""

EVENT_COST_RETRY_BONUS = 0.1
"""失败之后下一次判定的降难补偿（只给一次，用完清零）。"""

EVENT_COST_NOTE_LIMIT = 20
"""代价短句的字数上限：她得能用一句话说出来。"""

EVENT_TOPIC_DAYS = 3
"""东西弄丢 / 机会错过留下的心事挂几天（``note_own_topic`` 的按条过期）。"""

EVENT_EFFECT_LIMIT = 0.05
"""事件包自带 ``effects`` 的单字段幅度上限（T16：small/big 也吃这份脉冲）。"""

EVENT_FAIL_METHOD_GAIN = 0.01
"""失败时唯一的能力值增长：只有"真学会了新方法"才给这么多。"""


def event_cost_tier(tier: str, package_tier: str = TIER_SMALL) -> str:
    """判定档位 → 代价档位；成功 / 主动放弃返回空串（成功不受影响）。"""

    key = str(tier or "").strip().lower()
    if key == TIER_NARROW:
        return COST_TIER_NARROW
    if key != TIER_FAIL:
        return ""
    if str(package_tier or "").strip().lower() == TIER_BIG:
        return COST_TIER_BIG_FAIL
    return COST_TIER_FAIL


def _clip_text(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[: max(0, int(limit))]


def event_cost_note(
    kind: str,
    *,
    tier: str = "",
    stake: str = "",
    who: str = "",
) -> str:
    """这一下**弄丢了什么 / 错过了什么**——一句话，≤20 字，她随时能说出口。

    结算文案里必须带它：只有一个静默的数值变化，等于什么都没发生。
    """

    stake_kind = stake_kind_of(kind)
    thing = _clip_text(stake, 12).strip("，。、…~ ")
    if stake_kind == STAKE_ITEM:
        text = f"把{thing}弄丢了" if thing else "弄丢了一样东西"
    elif stake_kind == STAKE_OPPORTUNITY:
        text = f"错过了{thing}" if thing else "错过了一次机会"
    elif stake_kind == STAKE_RELATION:
        person = _clip_text(who, 8).strip()
        text = f"跟{person}生分了" if person else "跟人弄僵了"
    elif stake_kind == STAKE_ENERGY:
        text = f"把{thing}耗掉了" if thing else "把力气耗光了"
    else:
        text = f"{thing}也折腾进去了" if thing else "白折腾了一场"
    return _clip_text(text, EVENT_COST_NOTE_LIMIT)


def event_cost_topic(kind: str, *, stake: str = "") -> str:
    """东西 / 机会类的代价写成一件**她心里搁着的事**（``note_own_topic`` 用）。"""

    stake_kind = stake_kind_of(kind)
    thing = _clip_text(stake, 24).strip("，。、…~ ")
    if stake_kind == STAKE_OPPORTUNITY:
        return f"错过了{thing}" if thing else "错过了一次机会"
    return f"把{thing}弄丢了" if thing else "弄丢了一样东西"


@dataclass
class EventCost:
    """一次失败的代价明细（算好了再落账，便于单测直接断言数值）。"""

    stake_kind: str = STAKE_NONE
    tier: str = ""
    energy: float = 0.0
    """精力的增量（负值，已经夹过下限与单日额度）。"""

    relation: float = 0.0
    """人情（好感度）的增量（负值）。"""

    own_topic: str = ""
    """要写成心事的那句话（东西 / 机会类才有）。"""

    note: str = ""
    """她说的出口的那半句（≤20 字）。"""

    capped: bool = False
    """被某条护栏截断过（额度用完 / 触底 / 连续失败）。"""

    skipped: bool = False
    """这一档不该扣（成功 / 主动放弃 / 第 3 次连续失败）。"""

    retry_bonus: float = 0.0
    """这次失败留下的降难补偿（用一次就清）。"""

    @property
    def delta(self) -> float:
        """这次扣掉的幅度（≥0，只用来记单日额度）。"""

        return round(abs(float(self.energy)) + abs(float(self.relation)), 4)

    def as_detail(self) -> dict[str, Any]:
        """进 ``event_log`` 的那几个字段（事后能核对）。"""

        return {
            "stake_kind": self.stake_kind,
            "delta": self.delta,
            "capped": bool(self.capped),
            "cost_tier": self.tier,
            "energy": round(float(self.energy), 4),
            "relation": round(float(self.relation), 4),
            "own_topic": self.own_topic,
            "note": self.note,
            "retry_bonus": round(float(self.retry_bonus), 3),
        }


def plan_event_cost(
    *,
    stake_kind: Any = "",
    cost_tier: str = "",
    energy: float = 1.0,
    budget_left: float = EVENT_COST_DAY_LIMIT,
    fail_streak: int = 0,
    stake: str = "",
    who: str = "",
) -> EventCost:
    """算这一次的代价：按 ``stake_kind`` 兜底，三条硬护栏在这里。

    - ``energy``：她此刻的精力，扣完不许低于 ``EVENT_COST_ENERGY_FLOOR``；
    - ``budget_left``：今天还剩多少额度（``EVENT_COST_DAY_LIMIT`` 减已扣）；
    - ``fail_streak``：这条线索**连续**第几次失败（第 3 次只说话不扣）。
    """

    kind = stake_kind_of(stake_kind)
    key = str(cost_tier or "").strip().lower()
    if key not in EVENT_COST_TIERS:
        return EventCost(stake_kind=kind, skipped=True)
    # 人情得有当事人：线索上没记谁，就退回按精力兜底，别去扣一个不存在的人。
    if kind == STAKE_RELATION and not _clip_text(who, 8).strip():
        kind = STAKE_NONE
    note = event_cost_note(kind, tier=key, stake=stake, who=who)
    streak = max(0, int(fail_streak or 0))
    if streak >= EVENT_COST_STREAK_LIMIT:
        # 连着第三回没成：只说话，不再叠数值——不然模型学到的是"瘫着最省"。
        return EventCost(
            stake_kind=kind,
            tier=key,
            note=note,
            capped=True,
            skipped=True,
            retry_bonus=EVENT_COST_RETRY_BONUS,
        )
    if kind in (STAKE_ITEM, STAKE_OPPORTUNITY):
        return EventCost(
            stake_kind=kind,
            tier=key,
            own_topic=event_cost_topic(kind, stake=stake),
            note=note,
            retry_bonus=EVENT_COST_RETRY_BONUS,
        )
    room = max(0.0, float(energy) - EVENT_COST_ENERGY_FLOOR)
    budget = max(0.0, float(budget_left))
    if kind == STAKE_RELATION:
        want = abs(float(EVENT_COST_RELATION[key]))
        allowed = round(min(want, budget), 4)
        return EventCost(
            stake_kind=kind,
            tier=key,
            relation=-allowed if allowed > 0 else 0.0,
            note=note,
            capped=(want - allowed) > 1e-9,
            skipped=allowed <= 0,
            retry_bonus=EVENT_COST_RETRY_BONUS,
        )
    want = float(EVENT_COST_ENERGY[key]) if kind == STAKE_ENERGY else float(EVENT_COST_NEUTRAL)
    allowed = round(min(want, room, budget), 4)
    return EventCost(
        stake_kind=kind,
        tier=key,
        energy=-allowed if allowed > 0 else 0.0,
        note=note,
        capped=(want - allowed) > 1e-9,
        skipped=allowed <= 0,
        retry_bonus=EVENT_COST_RETRY_BONUS,
    )


def clamp_event_effects(
    effects: dict[str, Any] | None,
    *,
    limit: float = EVENT_EFFECT_LIMIT,
    energy: float | None = None,
    energy_floor: float = EVENT_COST_ENERGY_FLOOR,
) -> dict[str, float]:
    """把事件包自带的 ``effects`` 夹到护栏内（T16：small/big 也吃这份脉冲）。

    单字段幅度不超过 ``limit``；负向精力不许把人扣到 ``energy_floor`` 以下。
    """

    cap = abs(float(limit))
    result: dict[str, float] = {}
    for name, raw in (effects or {}).items():
        key = str(name).strip()
        if key not in _STATE_FIELDS:
            continue
        value = _as_float(raw)
        if abs(value) > cap:
            value = cap if value > 0 else -cap
        if key == "energy" and value < 0 and energy is not None:
            room = max(0.0, float(energy) - float(energy_floor))
            value = -min(abs(value), room)
        if not value:
            continue
        result[key] = round(value, 4)
    return result


@dataclass
class EventOption:
    desc: str = ""
    abilities: list[str] = field(default_factory=list)
    hint: str = ""
    no_check: bool = False
    difficulty: float = 0.0
    """相对整件事的难度偏移（正数更难），0 表示用事件本身的难度。"""
    risk: str = ""
    """这个选法输了会失去什么（≤20 字）。只进提示词，让她掂量代价用。"""

    def as_payload(self) -> dict[str, Any]:
        return {
            "desc": self.desc,
            "abilities": list(self.abilities),
            "hint": self.hint,
            "no_check": bool(self.no_check),
            "difficulty": float(self.difficulty),
            "risk": self.risk,
        }


@dataclass
class EventPackage:
    """一个事件：发生了什么 + 她能怎么选。"""

    title: str = ""
    scene: str = ""
    lead: str = ""
    """这件事是从哪儿来的（用户 2026-10-28）。

    ``hook`` 只写"这一刻发生了什么"，单拿出来是半句话；``lead`` 交代来路：
    她本来在做什么、什么东西先有了动静、谁先开的口。一句话、不依赖上下文
    就能读懂（谁 / 在哪 / 因为什么）。讲出去的那句话要能点住它，读者才知道
    你在说哪件事——没有 lead 的事件就是"没头没尾"。
    """

    hook: str = ""
    stake: str = ""
    """这件事要是没成，她会失去什么（一句具体的话，≤30 字；T15-S1）。

    没写的时候引擎会从 ``followup.fail`` 派生；再没有就填一句中性的
    （见 ``core/engine.py`` 的 ``_ensure_event_stake``）。它只用于质量闸和
    提示词，不进聊天记录。
    """

    stake_kind: str = STAKE_NONE
    """输在哪儿：``energy`` / ``relation`` / ``item`` / ``opportunity`` / ``none``。

    T16 起失败真的会按它扣东西（见 :func:`plan_event_cost`）；模型没写就是
    ``none``，按 0.03 精力兜底——**永远不能让"没写"变成"没代价"**。
    """

    tier: str = TIER_SMALL
    kind: str = KIND_SOLO
    mode: str = MODE_REAL
    place: str = ""
    """这件事发生在哪：世界里的节点 id；地图外的事留空，用 ``place_name``。"""

    place_name: str = ""
    """地点的人话（「厨房」「商业街」）；节点事件由引擎按 nodes 补上。"""

    difficulty: float = 0.5
    critical: bool = False
    """危险 / 紧急：日程撞上它时要让路（比如该睡觉了也得先处理完）。"""

    span: str = SPAN_SCENE
    """这件事拖多久：``scene`` 一回合内、``hours`` 几小时、``days`` 跨天。"""

    exclusivity: str = EXCLUSIVITY_AMBIENT
    """``exclusive`` 占住全局事件槽（同时最多一条）；``ambient`` 可与主线并存。"""

    options: list[EventOption] = field(default_factory=list)
    followup: dict[str, str] = field(default_factory=dict)
    outcome: str = ""
    """微事件直接用的结果（不调模型）。"""

    effects: dict[str, float] = field(default_factory=dict)
    """微事件直接用的状态脉冲。"""

    abilities: dict[str, float] = field(default_factory=dict)
    """微事件直接用的能力值变化。"""

    memory: str = ""
    source: str = ""
    """``pool`` / ``user`` / ``llm``：这个事件是哪来的。"""

    @property
    def needs_help(self) -> bool:
        return self.kind == KIND_NEED_INTERVENE

    @property
    def exclusive(self) -> bool:
        """只读派生：兼容"独占"口述成 bool 的写法。改请改 ``exclusivity``。"""

        return self.exclusivity == EXCLUSIVITY_EXCLUSIVE

    def as_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "scene": self.scene,
            "lead": self.lead,
            "hook": self.hook,
            "stake": self.stake,
            "stake_kind": self.stake_kind,
            "tier": self.tier,
            "kind": self.kind,
            "mode": self.mode,
            "place": self.place,
            "place_name": self.place_name,
            "difficulty": float(self.difficulty),
            "critical": bool(self.critical),
            "span": self.span,
            "exclusivity": self.exclusivity,
            "options": [item.as_payload() for item in self.options],
            "followup": dict(self.followup),
            "outcome": self.outcome,
            "effects": dict(self.effects),
            "abilities": dict(self.abilities),
            "memory": self.memory,
            "source": self.source,
        }


def _as_text(value: Any, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_options(raw: Any) -> list[EventOption]:
    options: list[EventOption] = []
    for item in list(raw or [])[:4]:
        if not isinstance(item, dict):
            continue
        desc = _as_text(item.get("desc") or item.get("text") or item.get("option"), 80)
        if not desc:
            continue
        abilities = [
            str(name).strip()
            for name in list(item.get("abilities") or [])
            if str(name).strip() in ABILITIES
        ]
        options.append(
            EventOption(
                desc=desc,
                abilities=abilities or ["wits"],
                hint=_as_text(item.get("hint"), 12) or "",
                no_check=bool(item.get("no_check")),
                difficulty=_as_float(item.get("difficulty"), 0.0),
                risk=_as_text(item.get("risk"), 20),
            )
        )
    return options


def event_from_payload(payload: dict[str, Any], *, source: str = "") -> EventPackage | None:
    """把一份字典整理成事件包（池子里的、模型给的、用户投递的都走这里）。"""

    if not isinstance(payload, dict):
        return None
    hook = _as_text(payload.get("hook") or payload.get("desc") or payload.get("title"), 200)
    title = _as_text(payload.get("title") or hook, 40)
    if not hook and not title:
        return None
    tier = str(payload.get("tier") or "").strip().lower()
    if tier not in EVENT_TIERS:
        tier = TIER_SMALL
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in (KIND_SOLO, KIND_NEED_INTERVENE):
        kind = KIND_SOLO
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in (MODE_REAL, MODE_IMAGINED):
        mode = MODE_REAL
    span = str(payload.get("span") or "").strip().lower()
    if span not in EVENT_SPANS:
        span = SPAN_SCENE
    exclusivity = str(payload.get("exclusivity") or "").strip().lower()
    if exclusivity not in EVENT_EXCLUSIVITIES:
        # 兼容只给 bool 的写法（``"exclusive": true``）
        exclusivity = EXCLUSIVITY_EXCLUSIVE if bool(payload.get("exclusive")) else EXCLUSIVITY_AMBIENT
    followup = payload.get("followup")
    followups: dict[str, str] = {}
    if isinstance(followup, dict):
        for key in ("success", "fail"):
            text = _as_text(followup.get(key), 120)
            if text:
                followups[key] = text
    elif isinstance(followup, str) and followup.strip():
        followups["success"] = _as_text(followup, 120)
    abilities = {
        str(name): _as_float(value)
        for name, value in dict(payload.get("abilities") or {}).items()
        if str(name) in ABILITIES
    }
    effects = {
        str(name): _as_float(value)
        for name, value in dict(payload.get("effects") or {}).items()
        if str(name) in ("affect", "valence", "energy", "loneliness", "curiosity", "boredom")
    }
    options = _parse_options(payload.get("options"))
    if tier == TIER_BIG:
        # T16：大事件不留"全身而退"的路。提示词里已经这么要求，但模型会忘——
        # 这里把 ``no_check`` 直接抹掉：躲开那条照样要掷骰、照样可能失手。
        for option in options:
            option.no_check = False
    return EventPackage(
        title=title,
        scene=_as_text(payload.get("scene"), 60),
        lead=_as_text(payload.get("lead") or payload.get("cause") or payload.get("setup"), 160),
        hook=hook,
        stake=_as_text(payload.get("stake"), 30),
        stake_kind=stake_kind_of(
            payload.get("stake_kind") or payload.get("stake_type") or payload.get("lose_kind")
        ),
        tier=tier,
        kind=kind,
        mode=mode,
        place=_as_text(payload.get("place"), 40),
        place_name=_as_text(payload.get("place_name") or payload.get("place"), 40),
        difficulty=max(0.05, min(0.95, _as_float(payload.get("difficulty"), 0.5))),
        critical=bool(payload.get("critical")),
        span=span,
        exclusivity=exclusivity,
        options=options,
        followup=followups,
        outcome=_as_text(payload.get("outcome"), 200),
        effects=effects,
        abilities=abilities,
        memory=_as_text(payload.get("memory"), 80),
        source=source or _as_text(payload.get("source"), 20),
    )


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

_EVENT_JSON_KEYS = frozenset(
    {
        # 事件包
        "id",
        "key",
        "source",
        "kind",
        "title",
        "place",
        "node",
        "options",
        "option",
        "desc",
        "action",
        "ask_help",
        "help",
        "abilities",
        "difficulty",
        "no_check",
        "pick",
        "choice",
        # 抉择 / 结算
        "phase",
        "reason",
        "send_to",
        "actions",
        "type",
        "intent",
        "params",
        "content",
        "messages",
        "target",
        "target_node",
        "duration",
        # 收尾台词（``lines`` 里至少要有 ``text``）
        "say",
        "plan",
        "success",
        "fail",
        "lines",
        "text",
        "delay",
    }
)
"""事件这条解析路径认的键名——``repair_json_keys`` 的白名单（见 ``json_actions``）。

只收协议里真的出现过的键；不在表里的键名被劈开也**不猜**。
"""

_LOGGER = logging.getLogger(__name__)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里挖一个 JSON 对象（容忍前后废话和 ``` 包裹）。

    上游中转会把输出按显示宽度折行，于是 JSON 字符串里出现**真换行**：
    ``json.loads`` 整段失败，事件收尾那句、抉择、结算全部解析不出来。这里和聊天那条
    解析路径走同一套修复（``json_actions.loads_json_repaired``）：裸换行按 CJK 规则
    粘回去，被换开的键名接回已知键。修不好仍然返回 ``None``，调用方照旧兜底。
    """

    body = str(text or "").strip()
    if not body:
        return None
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", body).strip()
    from .json_actions import loads_json_repaired  # 局部导入：顶层会成环（见文件头注释）

    candidates = [body]
    match = _JSON_BLOCK_RE.search(body)
    if match and match.group(0) != body:
        candidates.append(match.group(0))
    for candidate in candidates:
        data, repaired, newlines = loads_json_repaired(candidate, known=_EVENT_JSON_KEYS)
        if data is None:
            continue
        if repaired:
            _LOGGER.warning(
                "事件输出的 JSON 有裸控制字符 / 被劈开的键名，已修复："
                "repaired=%s raw_len=%d in_string_newlines=%d",
                repaired,
                len(str(text or "")),
                newlines,
            )
        if isinstance(data, dict):
            return data
    return None


def parse_event_package(text: str, *, source: str = "") -> EventPackage | None:
    return event_from_payload(parse_json_object(text) or {}, source=source)


# ---------------- 抉择 / 结算 / 建议筛选 ----------------

SAY_SLOTS = ("plan", "success", "fail")
"""``success``/``fail`` 是旧格式（预先写好两版台词）；新流程只用 ``plan``。"""

PHASE_ACT = "act"
"""她这一轮先去做点什么（查资料、试一下），做完再拿主意。"""

PHASE_DECIDE = "decide"
"""她这一轮直接拿主意（判定 / 求助 / 收尾）。"""


def _parse_say_plan(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """解析她的发送计划：``{plan|success|fail: [{text, delay}]}``。

    ``success``/``fail`` 是旧格式（预先写好两版台词）——现在还认，是为了老数据
    和旧测试能继续读；新流程只写 ``plan``，真正的台词等结果落定后再说。
    """

    result: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(raw, dict):
        return result
    for slot in SAY_SLOTS:
        lines = _normalize_lines(raw.get(slot))
        if lines:
            result[slot] = lines
    return result


def _loose_keys(mapping: Any) -> dict[str, Any]:
    """把键名两端的空白与大小写抹平。

    只给"事件收尾那句"这条解析用（``parse_event_speech`` / ``_normalize_lines``
    的 ``loose_keys`` 分支）：线上见过模型返回 ``{" lines": [...]}``——键名前面多了
    一个空格，``payload.get("lines")`` 就取不到，她写的话整句丢掉。这里只归一这一层
    的键，不动 ``parse_json_object`` 的全局行为，免得到处掩盖真的写错了键。
    """

    if not isinstance(mapping, dict):
        return {}
    return {str(key).strip().lower(): value for key, value in mapping.items()}


def _normalize_lines(raw: Any, *, limit: int = 8, loose_keys: bool = False) -> list[dict[str, Any]]:
    """把 ``[{text, delay}]`` / ``["..."]`` / ``"..."`` 统一成发送计划的行。

    ``loose_keys`` 打开时元素里的键名也容忍两端空白与大小写（``" text"``、
    ``" Delay"``），只给 ``parse_event_speech`` 用；``_parse_say_plan`` 保持严格。
    """

    lines: list[dict[str, Any]] = []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    for item in list(items)[: max(1, int(limit))]:
        if isinstance(item, str):
            text, delay = item, 0.0
        elif isinstance(item, dict):
            fields = _loose_keys(item) if loose_keys else item
            text = str(fields.get("text") or fields.get("message") or "").strip()
            delay = max(0.0, min(30.0, _as_float(fields.get("delay"), 0.0)))
        else:
            continue
        text = " ".join(str(text or "").split())[:200]
        if text:
            lines.append({"text": text, "delay": delay})
    return lines


# ---------------- 结果落定之后的发言（阶段 E1/E5） ----------------
#
# 台词不再"预先写两版、按结果挑一版"：判定和结算先把事实提交进线索，
# 然后才让她**照着已经发生的事**说这一句。

SPEECH_INTENT_INTRODUCE = "introduce"
SPEECH_INTENT_REQUEST = "request"
SPEECH_INTENT_CLARIFY = "clarify"
SPEECH_INTENT_ACK = "ack"
SPEECH_INTENT_PROGRESS = "progress"
SPEECH_INTENT_OUTCOME = "outcome"
SPEECH_INTENT_QUIET = "quiet"

SPEECH_INTENTS: tuple[str, ...] = (
    SPEECH_INTENT_INTRODUCE,
    SPEECH_INTENT_REQUEST,
    SPEECH_INTENT_CLARIFY,
    SPEECH_INTENT_ACK,
    SPEECH_INTENT_PROGRESS,
    SPEECH_INTENT_OUTCOME,
    SPEECH_INTENT_QUIET,
)

SPEECH_INTENT_LABELS: dict[str, str] = {
    SPEECH_INTENT_INTRODUCE: "起个头",
    SPEECH_INTENT_REQUEST: "求人帮忙",
    SPEECH_INTENT_CLARIFY: "问清楚",
    SPEECH_INTENT_ACK: "应一声",
    SPEECH_INTENT_PROGRESS: "说说进展",
    SPEECH_INTENT_OUTCOME: "说结果",
    SPEECH_INTENT_QUIET: "这幕不说",
}


def parse_event_speech(text: str) -> tuple[str, list[dict[str, Any]]]:
    """解析"结果定了之后那一句"：``(intent, lines)``。

    认 ``{"intent": "...", "lines": [{"text", "delay"}]}``；模型只给了一串话
    （数组或纯文本）时按 ``outcome`` 收下。``intent=quiet`` = 这一幕她不说。

    键名容忍两端空白与大小写（线上见过 ``{" lines": [...]}``）：只在这一次解析里
    归一，``parse_json_object`` 本身不动。
    """

    payload = parse_json_object(text)
    if isinstance(payload, dict):
        fields = _loose_keys(payload)
        intent = str(fields.get("intent") or "").strip().lower()
        if intent not in SPEECH_INTENTS:
            intent = SPEECH_INTENT_OUTCOME
        raw = fields.get("lines")
        if raw is None:
            raw = fields.get("say") if fields.get("say") is not None else fields.get("text")
        return intent, _normalize_lines(raw, limit=4, loose_keys=True)
    text = str(text or "").strip()
    if not text:
        # 模型什么都没说（或者这次调用没成）：不算"她决定不说"，交回调用方——
        # 通常那边会退回她抉择时写的那一句。
        return SPEECH_INTENT_OUTCOME, []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, list):
            return SPEECH_INTENT_OUTCOME, _normalize_lines(data, limit=4, loose_keys=True)
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("[") or stripped.startswith("```"):
        # 长得像 JSON 却连修复都解析不出来：宁可这一句不说，也绝不把她没写完的
        # JSON 当台词发进聊天里（线上 10-26 10:41 / 10:43 各发过一次原样 JSON）。
        _LOGGER.warning(
            "事件收尾句是解析不出来的 JSON，已丢弃（不发原样 JSON）：raw_len=%d",
            len(text),
        )
        return SPEECH_INTENT_OUTCOME, []
    return SPEECH_INTENT_OUTCOME, _normalize_lines(text, limit=4, loose_keys=True)


@dataclass
class Decision:
    pick: int = 0
    desc: str = ""
    abilities: list[str] = field(default_factory=list)
    difficulty_delta: float = 0.0
    no_check: bool = False
    ask_help: bool = False
    reason: str = ""
    say: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    phase: str = PHASE_DECIDE
    """这轮她是在**收集信息**（``act``）还是**直接拿主意**（``decide``）。"""

    actions: list[dict[str, Any]] = field(default_factory=list)
    """``phase=act`` 时她想先做的动作：``[{"type": ..., "intent": ...}]``。"""

    send_to: str = ""
    """这件事她想说给哪个会话听（会话组里的群 / 私聊）；空 = 落点默认。"""

    def lines(self, slot: str) -> list[dict[str, Any]]:
        if slot in self.say:
            return list(self.say.get(slot) or [])
        return list(self.say.get("plan") or [])


def parse_event_actions(raw: Any, allowed: set[str] | None = None) -> list[dict[str, Any]]:
    """解析「先去做点什么」那一栏：只留插件允许的动作，最多 2 个。"""

    result: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = [raw]
    for item in list(raw or [])[:2]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("type") or item.get("action") or "").strip()
        if not name:
            continue
        if allowed is not None and name not in allowed:
            continue
        intent = " ".join(str(item.get("intent") or item.get("content") or "").split())[:160]
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        result.append({"type": name, "intent": intent, "params": dict(params)})
    return result


def parse_decision(
    text: str, package: EventPackage, *, allowed_actions: set[str] | None = None
) -> Decision | None:
    """解析主模型的抉择。挑不出选项时返回 None（调用方用默认项兜底）。"""

    payload = parse_json_object(text)
    if not payload:
        return None
    raw_phase = str(payload.get("phase") or "").strip().lower()
    phase = PHASE_ACT if raw_phase == PHASE_ACT else PHASE_DECIDE
    actions = parse_event_actions(payload.get("actions"), allowed_actions)
    if phase == PHASE_ACT and not actions:
        # 说要先做点什么、却没给出能做的动作：当作直接拿主意，别卡在原地
        phase = PHASE_DECIDE
    if phase == PHASE_ACT and actions:
        # 收集信息这一轮不需要选项：她想做的事已经写在 actions 里了
        return Decision(
            phase=PHASE_ACT,
            actions=actions,
            reason=_as_text(payload.get("reason"), 120),
            say=_parse_say_plan(payload.get("say")),
            send_to=_as_text(payload.get("send_to"), 60),
        )
    raw_pick = payload.get("pick", payload.get("option"))
    options = list(package.options)
    choice = payload.get("choice") if isinstance(payload.get("choice"), dict) else {}
    pick = 0
    desc = ""
    if isinstance(raw_pick, str) and raw_pick.strip().isdigit():
        pick = int(raw_pick.strip())
    elif isinstance(raw_pick, (int, float)):
        pick = int(raw_pick)
    elif isinstance(raw_pick, str):
        desc = _as_text(raw_pick, 80)
    if not desc:
        # 提示词里约定的写法：自己写一个做法时用顶层 desc
        desc = _as_text(payload.get("desc") or payload.get("action"), 80)
    if not desc and choice:
        desc = _as_text(choice.get("desc"), 80)
    if not desc and 1 <= pick <= len(options):
        return Decision(
            pick=pick - 1,
            desc=options[pick - 1].desc,
            abilities=list(options[pick - 1].abilities),
            difficulty_delta=float(options[pick - 1].difficulty),
            no_check=bool(options[pick - 1].no_check),
            ask_help=bool(payload.get("ask_help", payload.get("help", False))),
            reason=_as_text(payload.get("reason"), 120),
            say=_parse_say_plan(payload.get("say")),
            phase=phase,
            actions=actions,
            send_to=_as_text(payload.get("send_to"), 60),
        )
    if desc:
        abilities = [
            str(name).strip()
            for name in list((choice or {}).get("abilities") or payload.get("abilities") or [])
            if str(name).strip() in ABILITIES
        ]
        return Decision(
            pick=-1,
            desc=desc,
            abilities=abilities or ["wits"],
            difficulty_delta=_as_float((choice or {}).get("difficulty"), 0.0),
            no_check=bool((choice or {}).get("no_check")),
            ask_help=bool(payload.get("ask_help", payload.get("help", False))),
            reason=_as_text(payload.get("reason"), 120),
            say=_parse_say_plan(payload.get("say")),
            phase=phase,
            actions=actions,
            send_to=_as_text(payload.get("send_to"), 60),
        )
    return None


def default_decision(package: EventPackage) -> Decision:
    """没有任何可用输出时的兜底：选最稳的那个选项（最后一个 no_check 优先）。"""

    options = list(package.options)
    for index in range(len(options) - 1, -1, -1):
        if options[index].no_check:
            return Decision(
                pick=index,
                desc=options[index].desc,
                abilities=list(options[index].abilities),
                no_check=True,
            )
    if options:
        return Decision(
            pick=0,
            desc=options[0].desc,
            abilities=list(options[0].abilities),
        )
    return Decision(pick=-1, desc="照原计划继续", abilities=["composure"])


@dataclass
class Settlement:
    outcome: str = ""
    ability_delta: dict[str, float] = field(default_factory=dict)
    state_delta: dict[str, float] = field(default_factory=dict)
    followup: str = ""
    memory: str = ""
    next_gap_minutes: float = 0.0
    """她自己决定"下一步隔多久再来"（分钟）。0 = 没给，用配置里的固定间隔。"""

    new_method: bool = False
    """这一下**学会了一个新办法**（T16）：失败时唯一允许的能力值增长入口。

    模型写 ``new_method: true`` 才给 +0.01；没写就当没学到，失败一分经验都不涨。
    """


_STATE_FIELDS = ("affect", "valence", "energy", "loneliness", "curiosity", "boredom")


def parse_settlement(text: str) -> Settlement | None:
    """解析打杂模型给的结算结果（结果描述 + 能力值变化 + 状态脉冲 + 是否有后续）。"""

    payload = parse_json_object(text)
    if not payload:
        return None
    outcome = _as_text(payload.get("outcome") or payload.get("result"), 200)
    ability_delta = {
        str(name): _as_float(value)
        for name, value in dict(payload.get("ability_delta") or {}).items()
        if str(name) in ABILITIES
    }
    state_delta = {
        str(name): _as_float(value)
        for name, value in dict(payload.get("state_delta") or payload.get("state") or {}).items()
        if str(name) in _STATE_FIELDS
    }
    followup = _as_text(payload.get("followup"), 120)
    memory = _as_text(payload.get("memory"), 80)
    new_method = str(
        payload.get("new_method") or payload.get("learned_method") or ""
    ).strip().lower() in ("1", "true", "yes", "on", "是")
    try:
        next_gap = max(0.0, float(payload.get("next_gap") or 0.0))
    except (TypeError, ValueError):
        next_gap = 0.0
    if not outcome and not ability_delta and not state_delta and not followup:
        return None
    return Settlement(
        outcome=outcome,
        ability_delta=ability_delta,
        state_delta=state_delta,
        followup=followup,
        memory=memory,
        next_gap_minutes=min(next_gap, 360.0),
        new_method=new_method,
    )


CONTRIBUTION_KINDS = (
    "question",
    "advice",
    "new_fact",
    "offer",
    "cheer",
    "joke",
    "refusal",
    "unrelated",
)
"""群友那句话属于哪一种（阶段 E3）。

问清楚 / 出主意 / 带来新情况 / 愿意帮忙 / 打气 / 起哄 / 拒绝 / 无关。
"""

CONTRIBUTION_LABELS = {
    "question": "问情况",
    "advice": "出主意",
    "new_fact": "带来新情况",
    "offer": "愿意帮忙",
    "cheer": "打气",
    "joke": "起哄",
    "refusal": "不看好",
    "unrelated": "无关",
}

CONTRIBUTION_ACTIONABLE = ("advice", "new_fact", "offer")
"""这些才算"真的改变了做法"：打气和起哄只是接住，不该替她改路线。"""

_KIND_ALIASES: dict[str, str] = aliases.CONTRIBUTION_ALIASES
"""群友贡献类型的中文/英文别名（A 线收尾：表已收到 ``core/aliases.py`` 一处）。

同形不同义提醒：这里的 ``refuse``→``refusal`` 与口吻表里的 ``refuse`` 是两个域，
``hug`` 既是口吻取值也是动作 id——**别拿这个表去查动作**。
"""


def contribution_kind(raw: Any) -> str:
    """把模型写的分类收敛到固定枚举；认不出就算"出了个主意"。"""

    kind = str(raw or "").strip().lower()
    if not kind:
        return "advice"
    return _KIND_ALIASES.get(kind, "advice")


@dataclass
class Suggestions:
    """群友回复的分类结果：只丢无关的，起哄照留。"""

    suggestions: list[dict[str, Any]] = field(default_factory=list)
    teasers: list[dict[str, Any]] = field(default_factory=list)
    cheers: list[dict[str, Any]] = field(default_factory=list)
    irrelevant: list[dict[str, Any]] = field(default_factory=list)

    @property
    def useful(self) -> list[dict[str, Any]]:
        """真的能改变做法的那几条（阶段 E3：建议 / 新情况 / 愿意帮忙）。"""

        return [
            item
            for item in self.suggestions
            if str(item.get("kind") or "") in CONTRIBUTION_ACTIONABLE
        ]

    @property
    def alignment(self) -> int:
        """可用于情境修正的条数：只有**真的改变做法**的才算。"""

        return len(self.useful)

    @property
    def related(self) -> list[dict[str, Any]]:
        return [*self.suggestions, *self.teasers, *self.cheers]

    def summary(self) -> str:
        bits = []
        if self.suggestions:
            bits.append(f"建议 {len(self.suggestions)}")
        if self.teasers:
            bits.append(f"起哄 {len(self.teasers)}")
        if self.cheers:
            bits.append(f"打气 {len(self.cheers)}")
        if self.irrelevant:
            bits.append(f"无关 {len(self.irrelevant)}")
        return "｜".join(bits) or "没有回应"

    def prompt_block(self) -> str:
        """给主模型看的那一段（建议 + 起哄，都给她看）。"""

        lines: list[str] = []
        for item in self.suggestions:
            lines.append(f"- {item.get('from') or '有人'}：{item.get('point')}")
        for item in self.teasers:
            lines.append(f"- {item.get('from') or '有人'}（起哄）：{item.get('point')}")
        for item in self.cheers:
            lines.append(f"- {item.get('from') or '有人'}（打气）：{item.get('point')}")
        return "\n".join(lines)

    def digest(self) -> str:
        """一行摘要，给日志/调试输出用。"""

        parts = []
        if self.suggestions:
            parts.append(
                "建议 "
                + "、".join(
                    f"{item.get('from') or '有人'}：{item.get('point')}"
                    for item in self.suggestions[:2]
                )
            )
        if self.teasers:
            parts.append(f"起哄 {len(self.teasers)}")
        if self.irrelevant:
            parts.append(f"无关 {len(self.irrelevant)}")
        return "；".join(parts)


def parse_suggestions(text: str) -> Suggestions | None:
    payload = parse_json_object(text)
    if not payload:
        return None
    result = Suggestions()
    for item in list(payload.get("related") or payload.get("suggestions") or []):
        if not isinstance(item, dict):
            continue
        point = _as_text(item.get("point") or item.get("text") or item.get("desc"), 100)
        if not point:
            continue
        entry = {
            "from": _as_text(item.get("from") or item.get("who"), 24),
            "point": point,
            "kind": contribution_kind(item.get("kind") or item.get("type")),
        }
        if entry["kind"] in ("joke", "cheer", "unrelated"):
            if entry["kind"] == "joke":
                result.teasers.append(entry)
            elif entry["kind"] == "cheer":
                result.cheers.append(entry)
            else:
                result.irrelevant.append(entry)
        else:
            result.suggestions.append(entry)
    for item in list(payload.get("irrelevant") or []):
        if not isinstance(item, dict):
            continue
        text_value = _as_text(item.get("point") or item.get("text") or item.get("why"), 100)
        if text_value:
            result.irrelevant.append(
                {
                    "from": _as_text(item.get("from") or item.get("who"), 24),
                    "point": text_value,
                    "kind": "unrelated",
                }
            )
    if not (result.suggestions or result.teasers or result.cheers or result.irrelevant):
        return None
    return result


def contribution_row(
    item: dict[str, Any],
    *,
    event_id: str = "",
    user_id: str = "",
    user_name: str = "",
    session_id: str = "",
    step: int = 0,
    revision: int = 0,
    now: float = 0.0,
    kind: str = "",
) -> dict[str, Any]:
    """把群友的一句话记成一条**可追溯**的贡献（阶段 E3 的"来源"要求）。

    记下来源（哪件事、哪一幕、哪个版本、谁说的、在哪个会话）之后，迟到的话
    才判得出来"它是对哪个版本的回应"，也才能不重复消费同一句。
    """

    return {
        "kind": contribution_kind(kind or item.get("kind")),
        "point": _as_text(item.get("point") or item.get("text"), 100),
        "who": _as_text(user_id, 40),
        "who_name": _as_text(user_name or item.get("from"), 24),
        "session": _as_text(session_id, 60),
        "event_id": _as_text(event_id, 40),
        "step": max(0, int(step or 0)),
        "revision": max(0, int(revision or 0)),
        "at": float(now or 0.0),
    }


def contribution_digest(rows: list[dict[str, Any]] | None, *, limit: int = 3) -> str:
    """一行摘要：谁出了什么主意（给日志和调试输出用）。"""

    parts = []
    for item in list(rows or [])[-limit:]:
        if not isinstance(item, dict):
            continue
        label = CONTRIBUTION_LABELS.get(str(item.get("kind") or ""), "回应")
        who = str(item.get("who_name") or item.get("who") or "有人")
        point = " ".join(str(item.get("point") or "").split())[:40]
        if point:
            parts.append(f"{who}（{label}）：{point}")
    return "；".join(parts)


def suggestions_from_contributions(rows: list[dict[str, Any]] | None) -> "Suggestions | None":
    """把记下来的贡献还原成一份 :class:`Suggestions`（短窗收集后再处理用）。"""

    result = Suggestions()
    for item in list(rows or []):
        if not isinstance(item, dict):
            continue
        entry = {
            "from": str(item.get("who_name") or item.get("who") or "有人"),
            "point": str(item.get("point") or ""),
            "kind": contribution_kind(item.get("kind")),
        }
        if not entry["point"]:
            continue
        if entry["kind"] == "joke":
            result.teasers.append(entry)
        elif entry["kind"] == "cheer":
            result.cheers.append(entry)
        elif entry["kind"] == "unrelated":
            result.irrelevant.append(entry)
        else:
            result.suggestions.append(entry)
    if not (result.suggestions or result.teasers or result.cheers):
        return None
    return result


# ---------------- 线索 ----------------


def new_thread_id(now: float | None = None) -> str:
    stamp = int(float(now if now is not None else time.time()))
    return f"th{stamp % 1000000}"


def new_thread(
    package: EventPackage,
    *,
    now: float,
    scope: str = SCOPE_SESSION,
    thread_id: str = "",
) -> dict[str, Any]:
    return {
        "id": thread_id or new_thread_id(now),
        "title": package.title,
        "scene": package.scene,
        "place": package.place,
        "place_name": package.place_name,
        "tier": package.tier,
        "mode": package.mode,
        "span": package.span if package.span in EVENT_SPANS else SPAN_SCENE,
        "exclusivity": (
            package.exclusivity
            if package.exclusivity in EVENT_EXCLUSIVITIES
            else EXCLUSIVITY_AMBIENT
        ),
        "opened_at": float(now),
        "updated_at": float(now),
        "closed_at": 0.0,
        "scope": scope if scope in (SCOPE_ACT, SCOPE_SESSION, SCOPE_DAY) else SCOPE_SESSION,
        "status": "open",
        "root": package.as_payload(),
        "current_step": {},
        "revision": 0,
        "phase": PHASE_PROPOSED,
        "steps": [],
        "line": "",
        "next_step_at": 0.0,
    }


# ---------------- 事件实例的状态机（阶段 E） ----------------
#
# 一件事（thread）从"刚冒出来"到"收尾"要经过几个阶段。**转换由代码负责**：
# 模型只能提议，代码决定能不能走到下一步、要不要留期限、同一步不能提交两次。

PHASE_PROPOSED = "proposed"
PHASE_VALIDATED = "validated"
PHASE_QUIET = "quiet"
PHASE_INTRODUCED = "introduced"
PHASE_DISCUSSING = "discussing"
PHASE_COMMITTED = "committed"
PHASE_EXECUTING = "executing"
PHASE_OUTCOME_COMMITTED = "outcome_committed"
PHASE_FOLLOWUP_READY = "followup_ready"
PHASE_CLOSED = "closed"

EVENT_PHASES: tuple[str, ...] = (
    PHASE_PROPOSED,
    PHASE_VALIDATED,
    PHASE_QUIET,
    PHASE_INTRODUCED,
    PHASE_DISCUSSING,
    PHASE_COMMITTED,
    PHASE_EXECUTING,
    PHASE_OUTCOME_COMMITTED,
    PHASE_FOLLOWUP_READY,
    PHASE_CLOSED,
)

# 允许的下一步：写死在这里，模型说"直接上"也得先经过中间阶段。
PHASE_NEXT: dict[str, tuple[str, ...]] = {
    PHASE_PROPOSED: (PHASE_VALIDATED, PHASE_QUIET, PHASE_DISCUSSING),
    PHASE_VALIDATED: (PHASE_INTRODUCED, PHASE_QUIET, PHASE_DISCUSSING),
    PHASE_QUIET: (PHASE_INTRODUCED, PHASE_DISCUSSING, PHASE_CLOSED),
    PHASE_INTRODUCED: (PHASE_DISCUSSING, PHASE_COMMITTED, PHASE_QUIET, PHASE_CLOSED),
    PHASE_DISCUSSING: (PHASE_COMMITTED, PHASE_QUIET, PHASE_CLOSED),
    PHASE_COMMITTED: (PHASE_EXECUTING, PHASE_DISCUSSING, PHASE_CLOSED),
    PHASE_EXECUTING: (PHASE_OUTCOME_COMMITTED, PHASE_CLOSED),
    PHASE_OUTCOME_COMMITTED: (PHASE_FOLLOWUP_READY, PHASE_CLOSED),
    PHASE_FOLLOWUP_READY: (PHASE_DISCUSSING, PHASE_CLOSED),
    PHASE_CLOSED: (),
}

# 已经"定了"的阶段：进到这里之后，新的聊天输入只能讨论，不能改写结果。
PHASE_SETTLED: tuple[str, ...] = (
    PHASE_EXECUTING,
    PHASE_OUTCOME_COMMITTED,
    PHASE_FOLLOWUP_READY,
    PHASE_CLOSED,
)


def thread_revision(thread: dict[str, Any] | None) -> int:
    """这条线索是第几版（每换一幕 +1）。"""

    try:
        return int((thread or {}).get("revision") or 0)
    except (TypeError, ValueError):
        return 0


def thread_phase(thread: dict[str, Any] | None) -> str:
    """这条线索现在处在哪个阶段；老数据没写就按已有痕迹推断。"""

    item = thread or {}
    phase = str(item.get("phase") or "").strip()
    if phase in EVENT_PHASES:
        return phase
    if str(item.get("status") or "open") != "open":
        return PHASE_CLOSED
    if list(item.get("steps") or []):
        return PHASE_DISCUSSING
    return PHASE_PROPOSED


def phase_settled(thread: dict[str, Any] | None) -> bool:
    """结果是否已经定了（定了之后迟到的话只能用来讨论，不能改写）。"""

    return thread_phase(thread) in PHASE_SETTLED


def advance_phase(thread: dict[str, Any], phase: str, *, now: float = 0.0) -> bool:
    """把线索推进到 ``phase``。

    幂等：已经在那个阶段就什么都不做（不改 revision、不动 updated_at）。
    不允许的跳跃直接拒绝（返回 ``False``），由调用方决定要不要记一笔。
    """

    if not isinstance(thread, dict):
        return False
    target = str(phase or "").strip()
    if target not in EVENT_PHASES:
        return False
    current = thread_phase(thread)
    if target == current:
        return False
    allowed = PHASE_NEXT.get(current, ())
    if target not in allowed:
        return False
    previous = thread.get("phase")
    thread["phase"] = target
    if previous != target:
        thread["updated_at"] = float(now) if now else float(thread.get("updated_at") or 0.0)
    if target == PHASE_CLOSED:
        thread["status"] = "closed"
        thread["closed_at"] = float(now) if now else float(thread.get("closed_at") or 0.0)
        thread["pending_followup"] = ""
        thread["next_step_at"] = 0.0
    elif str(thread.get("status") or "open") != "open":
        thread["status"] = "open"
        thread["closed_at"] = 0.0
    return True


def set_current_step(
    thread: dict[str, Any], package: EventPackage, *, now: float = 0.0
) -> int:
    """把"现在正在演的那一幕"换成 ``package``，并让 revision +1。

    **``root`` 永远不动**：开局的前提是这件事的原始事实，后面每一幕都只是它的
    一个当下切片。以前续线时直接覆盖 ``root``，于是"她当初到底遇到了什么"被
    后来的一幕替换掉了，帮她的模型看到的前提也跟着变——这就是 E1 要修的那处。
    """

    if not isinstance(thread, dict) or package is None:
        return thread_revision(thread)
    thread["current_step"] = package.as_payload()
    thread["revision"] = thread_revision(thread) + 1
    thread["updated_at"] = float(now) if now else float(thread.get("updated_at") or 0.0)
    return thread["revision"]


def current_step_payload(thread: dict[str, Any] | None) -> dict[str, Any]:
    """此刻那一幕的包；老数据没有 ``current_step`` 就回落到 ``root``。"""

    item = thread or {}
    payload = item.get("current_step")
    if isinstance(payload, dict) and payload:
        return dict(payload)
    root = item.get("root")
    return dict(root) if isinstance(root, dict) else {}


def thread_deadline(thread: dict[str, Any] | None) -> float:
    """这件事的期限（秒时间戳；0 = 没有期限）。"""

    item = thread or {}
    for key in ("deadline", "next_step_at"):
        try:
            value = float(item.get(key) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def set_thread_deadline(thread: dict[str, Any], when: float) -> None:
    """给这件事定个期限：到点没进展就走"她自己拿主意"那条路。"""

    if not isinstance(thread, dict):
        return
    try:
        stamp = float(when or 0.0)
    except (TypeError, ValueError):
        stamp = 0.0
    thread["deadline"] = stamp


STEP_MAX_TEXT = 80


def append_step(
    thread: dict[str, Any],
    *,
    outcome: dict[str, Any],
    now: float,
    step: dict[str, Any],
) -> None:
    """往线索里追加一步：她选了什么、判定如何、结果怎样。"""

    steps = thread.setdefault("steps", [])
    steps.append({**step, "at": float(now)})
    thread["updated_at"] = float(now)
    thread["status"] = str(outcome.get("status") or thread.get("status") or "open")
    followup = str(outcome.get("followup") or "").strip()
    thread["next_step_at"] = float(now) + float(outcome.get("next_step_gap") or 0.0) if followup else 0.0
    thread["pending_followup"] = followup
    if thread["status"] != "open":
        thread["next_step_at"] = 0.0
        thread["pending_followup"] = ""


def thread_step_lines(thread: dict[str, Any], limit: int = 8) -> list[str]:
    """线索履历（压缩版）：每一步一行「选择 → 结果」。"""

    lines: list[str] = []
    for step in list(thread.get("steps") or [])[-limit:]:
        desc = str(step.get("desc") or "").strip()
        result = str(step.get("result") or "").strip()
        tier = TIER_LABELS.get(str(step.get("tier") or ""), "")
        head = f"{desc} → {tier}" if tier else desc
        lines.append(f"{head} → {result}" if result else head)
    return lines


def thread_digest(thread: dict[str, Any]) -> str:
    """喂给打杂模型的那段"这件事的来龙去脉"。"""

    if not isinstance(thread, dict):
        return ""
    root = thread.get("root") or {}
    lines = [f"初始：{root.get('title') or thread.get('title') or ''}（{root.get('hook') or ''}）"]
    for index, line in enumerate(thread_step_lines(thread), start=1):
        lines.append(f"第 {index} 步：{line}")
    if thread.get("status") == "closed":
        lines.append("（这条线索已经结束）")
    return "\n".join(line for line in lines if line.strip())


THREAD_OPEN = "open"
THREAD_DORMANT = "dormant"
THREAD_CLOSED = "closed"


def thread_status(thread: dict[str, Any]) -> str:
    if not isinstance(thread, dict):
        return THREAD_CLOSED
    return str(thread.get("status") or THREAD_OPEN).strip().lower()


def thread_span(thread: dict[str, Any]) -> str:
    """这条线索的跨度：线程自己记的那份优先，老存档回落到根事件包。"""

    if not isinstance(thread, dict):
        return SPAN_SCENE
    for source in (thread, thread.get("root") or {}, thread.get("current_step") or {}):
        if isinstance(source, dict):
            value = str(source.get("span") or "").strip().lower()
            if value in EVENT_SPANS:
                return value
    return SPAN_SCENE


def thread_exclusivity(thread: dict[str, Any]) -> str:
    """这条线索占不占全局槽：``exclusive`` 只能有一条，``ambient`` 可并存。"""

    if not isinstance(thread, dict):
        return EXCLUSIVITY_AMBIENT
    for source in (thread, thread.get("root") or {}, thread.get("current_step") or {}):
        if not isinstance(source, dict):
            continue
        value = str(source.get("exclusivity") or "").strip().lower()
        if value in EVENT_EXCLUSIVITIES:
            return value
        if source.get("exclusive") is True:
            return EXCLUSIVITY_EXCLUSIVE
    return EXCLUSIVITY_AMBIENT


def thread_exclusive(thread: dict[str, Any]) -> bool:
    return thread_exclusivity(thread) == EXCLUSIVITY_EXCLUSIVE


def thread_alive(thread: dict[str, Any]) -> bool:
    """还占着她心里的位置吗：``open`` 和 ``dormant``（睡着等下次唤醒）都算。"""

    return thread_status(thread) in (THREAD_OPEN, THREAD_DORMANT)


def live_threads(threads: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [item for item in list(threads or []) if isinstance(item, dict) and thread_alive(item)]


def thread_exhausted(
    thread: dict[str, Any],
    *,
    now: float,
    max_steps: int | None = None,
    max_minutes: float | None = None,
    span: str = "",
) -> bool:
    """步数或时长到顶就必须收尾，不能无限连环。

    ``span`` 缺省时按线索自己记的那档取上限（``SPAN_MAX_STEPS`` /
    ``SPAN_MAX_MINUTES``）；显式给了 ``max_steps`` / ``max_minutes`` 就用给的
    （配置项优先，老调用点照旧传 6 / 120 也不会变）。
    """

    if not isinstance(thread, dict):
        return True
    span = span if span in EVENT_SPANS else thread_span(thread)
    steps_limit = int(max_steps) if max_steps else SPAN_MAX_STEPS.get(span, 6)
    minutes_limit = float(max_minutes) if max_minutes else SPAN_MAX_MINUTES.get(span, 120.0)
    if len(list(thread.get("steps") or [])) >= max(1, steps_limit):
        return True
    opened = float(thread.get("opened_at") or 0.0)
    if opened > 0 and minutes_limit > 0:
        return (float(now) - opened) >= minutes_limit * 60.0
    return False


def _uniform(rng: Any, low: float, high: float) -> float:
    """取一个 [low, high) 的随机数；注入的假 rng 没有 uniform 就取中值。"""

    fn = getattr(rng, "uniform", None)
    if callable(fn):
        try:
            return float(fn(float(low), float(high)))
        except Exception:  # 假 rng 抛错不该让一整步挂掉
            pass
    return (float(low) + float(high)) / 2.0


def span_gap_seconds(
    span: str,
    *,
    base_gap: float = 60.0,
    asked_gap: float = 0.0,
    rng: Any = None,
) -> float:
    """这一步之后隔多久再推进（秒）。

    跨度越小节奏越快：``scene`` 用 ``step_gap_seconds``，``hours`` 30–90 分钟，
    ``days`` 大约一天一次（带抖动，免得像定时器）。每档都有下限（防刷），
    模型给的 ``next_gap`` 只在区间内生效。
    """

    span = span if span in EVENT_SPANS else SPAN_SCENE
    minimum = float(SPAN_MIN_GAP_SECONDS.get(span, 0.0))
    if span == SPAN_SCENE:
        gap = max(float(base_gap or 0.0), minimum)
        if asked_gap and float(asked_gap) > 0:
            gap = max(gap, min(float(asked_gap), 360.0 * 3600.0))
        return max(1.0, gap)
    if span == SPAN_HOURS:
        low, high, cap = 30.0 * 60.0, 90.0 * 60.0, 6.0 * 3600.0
    else:
        low, high, cap = 20.0 * 3600.0, 28.0 * 3600.0, 24.0 * 3600.0
    if asked_gap and float(asked_gap) > 0:
        return max(minimum, min(float(asked_gap), cap))
    return max(minimum, _uniform(rng, low, high))


def open_threads(threads: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        item
        for item in list(threads or [])
        if isinstance(item, dict) and str(item.get("status") or "open") == "open"
    ]


# ---------------- 触发加权（T15-S2） ----------------
#
# 一次事件候选的权重 = 基础频率 × 地点 × 她正在干的事 × 未兑现的伏笔 × 独占冲突。
# 全是纯函数：引擎只把世界状态翻译成标签 / 计数，排序方向可以单独测。


PLACE_WEIGHTS = {
    "厨房": 3.0,
    "浴室": 3.0,
    "街道": 1.0,
}

ACTION_WEIGHTS = {
    "做饭": 3.0,
    "洗澡": 3.0,
    "睡觉": 0.3,
    "通勤": 2.0,
}


def place_factor(*labels: Any) -> float:
    """地点系数：在厨房 / 浴室容易出事，街道和其它地方维持 1。"""

    for label in labels:
        text = str(label or "").strip()
        if not text:
            continue
        for key, value in PLACE_WEIGHTS.items():
            if key in text:
                return float(value)
    return 1.0


def action_factor(*labels: Any) -> float:
    """她正在做的事的系数：做饭 / 洗澡最容易被打断，睡觉时基本别出事。"""

    for label in labels:
        text = str(label or "").strip()
        if not text:
            continue
        for key, value in ACTION_WEIGHTS.items():
            if key in text:
                return float(value)
    return 1.0


def unresolved_factor(
    *,
    pending_followups: int = 0,
    promised_hours: float = 0.0,
    self_topics: int = 0,
) -> float:
    """没兑现的伏笔抬权重：有挂着的下一步 ×3，承诺超过 24 小时没回 ×4，自己的话题 ×2。"""

    factor = 1.0
    if int(pending_followups) > 0:
        factor *= 3.0
    if float(promised_hours) >= 24.0:
        factor *= 4.0
    if int(self_topics) > 0:
        factor *= 2.0
    return min(factor, 24.0)


def conflict_factor(*, exclusive_active: bool = False, related: bool = False) -> float:
    """独占进行中，跟它无关的事降频（×0.2），相关的照常。"""

    if exclusive_active and not related:
        return 0.2
    return 1.0


def ambient_dampen_factor(*, exclusive_active: bool = False) -> float:
    """独占进行中，别的事整体再降频（×1/3）——一山不容二虎。"""

    return (1.0 / 3.0) if exclusive_active else 1.0


def event_weight(
    *,
    tier_rate: float,
    place: float = 1.0,
    action: float = 1.0,
    unresolved: float = 1.0,
    conflict: float = 1.0,
) -> float:
    """一次候选的权重；负数一律当 0（权重不能是负的）。"""

    weight = (
        float(tier_rate)
        * float(place)
        * float(action)
        * float(unresolved)
        * float(conflict)
    )
    return max(0.0, weight)

