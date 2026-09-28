"""事件系统：能力值、掷骰判定、事件包、线索。

这一层刻意不碰 IO、不调模型：模型给的文本在这里被解析、夹紧、算成结果，
所以边界情况（难度、修正、四档分档、能力值上限、线索收敛）都能单独测一遍。

几个约定：

- **能力值**是慢变量（体力/智力/灵巧/心性），只由事件结果改变，几乎不随时间衰减；
  和"精力"不是一回事：精力睡一觉就回来，能力值是她的底子。
- **判定**是纯计算：``p = 能力值 × 情境修正 × 难度``，夹在 0.05~0.95，
  掷一次分四档。每次判定都要能写成一行人话，进日志和调试输出。
- **主观档位**（她觉得自己行不行）和客观概率**允许不一致**：心潮高会高估自己，
  精力低会低估。这个偏差就是"以为稳却翻车"的来源。
- **线索**是一串事件：一步步追加"她选了什么 → 结果如何"，续线时整条喂回去。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

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

    factor = 1.0
    for _label, value in modifiers or []:
        try:
            factor *= float(value)
        except (TypeError, ValueError):
            continue
    try:
        diff = max(0.05, min(0.95, float(difficulty)))
    except (TypeError, ValueError):
        diff = 0.5
    probability = float(ability_value) * factor * diff
    probability = max(CHECK_FLOOR, min(CHECK_CEIL, probability))
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


@dataclass
class EventOption:
    desc: str = ""
    abilities: list[str] = field(default_factory=list)
    hint: str = ""
    no_check: bool = False
    difficulty: float = 0.0
    """相对整件事的难度偏移（正数更难），0 表示用事件本身的难度。"""

    def as_payload(self) -> dict[str, Any]:
        return {
            "desc": self.desc,
            "abilities": list(self.abilities),
            "hint": self.hint,
            "no_check": bool(self.no_check),
            "difficulty": float(self.difficulty),
        }


@dataclass
class EventPackage:
    """一个事件：发生了什么 + 她能怎么选。"""

    title: str = ""
    scene: str = ""
    hook: str = ""
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

    def as_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "scene": self.scene,
            "hook": self.hook,
            "tier": self.tier,
            "kind": self.kind,
            "mode": self.mode,
            "place": self.place,
            "place_name": self.place_name,
            "difficulty": float(self.difficulty),
            "critical": bool(self.critical),
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
    return EventPackage(
        title=title,
        scene=_as_text(payload.get("scene"), 60),
        hook=hook,
        tier=tier,
        kind=kind,
        mode=mode,
        place=_as_text(payload.get("place"), 40),
        place_name=_as_text(payload.get("place_name") or payload.get("place"), 40),
        difficulty=max(0.05, min(0.95, _as_float(payload.get("difficulty"), 0.5))),
        critical=bool(payload.get("critical")),
        options=_parse_options(payload.get("options")),
        followup=followups,
        outcome=_as_text(payload.get("outcome"), 200),
        effects=effects,
        abilities=abilities,
        memory=_as_text(payload.get("memory"), 80),
        source=source or _as_text(payload.get("source"), 20),
    )


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里挖一个 JSON 对象（容忍前后废话和 ``` 包裹）。"""

    body = str(text or "").strip()
    if not body:
        return None
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", body).strip()
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        pass
    match = _JSON_BLOCK_RE.search(body)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_event_package(text: str, *, source: str = "") -> EventPackage | None:
    return event_from_payload(parse_json_object(text) or {}, source=source)


# ---------------- 抉择 / 结算 / 建议筛选 ----------------

SAY_SLOTS = ("plan", "success", "fail")

PHASE_ACT = "act"
"""她这一轮先去做点什么（查资料、试一下），做完再拿主意。"""

PHASE_DECIDE = "decide"
"""她这一轮直接拿主意（判定 / 求助 / 收尾）。"""


def _parse_say_plan(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """解析她的发送计划：``{plan|success|fail: [{text, delay}]}``。"""

    result: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(raw, dict):
        return result
    for slot in SAY_SLOTS:
        lines: list[dict[str, Any]] = []
        for item in list(raw.get(slot) or [])[:8]:
            if isinstance(item, str):
                text, delay = item, 0.0
            elif isinstance(item, dict):
                text = str(item.get("text") or item.get("message") or "").strip()
                delay = max(0.0, min(30.0, _as_float(item.get("delay"), 0.0)))
            else:
                continue
            text = " ".join(str(text or "").split())[:200]
            if text:
                lines.append({"text": text, "delay": delay})
        if lines:
            result[slot] = lines
    return result


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
    )


@dataclass
class Suggestions:
    """群友回复的分类结果：只丢无关的，起哄照留。"""

    suggestions: list[dict[str, Any]] = field(default_factory=list)
    teasers: list[dict[str, Any]] = field(default_factory=list)
    cheers: list[dict[str, Any]] = field(default_factory=list)
    irrelevant: list[dict[str, Any]] = field(default_factory=list)

    @property
    def alignment(self) -> int:
        """可用于情境修正的建议条数。"""

        return len(self.suggestions)

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
        entry = {"from": _as_text(item.get("from") or item.get("who"), 24), "point": point}
        kind = str(item.get("kind") or "suggestion").strip().lower()
        if kind in ("tease", "joke", "heckle", "起哄"):
            result.teasers.append(entry)
        elif kind in ("cheer", "support", "打气"):
            result.cheers.append(entry)
        elif kind in ("irrelevant", "noise"):
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
                }
            )
    if not (result.suggestions or result.teasers or result.cheers or result.irrelevant):
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
        "opened_at": float(now),
        "updated_at": float(now),
        "closed_at": 0.0,
        "scope": scope if scope in (SCOPE_ACT, SCOPE_SESSION, SCOPE_DAY) else SCOPE_SESSION,
        "status": "open",
        "root": package.as_payload(),
        "steps": [],
        "line": "",
        "next_step_at": 0.0,
    }


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


def thread_exhausted(
    thread: dict[str, Any], *, now: float, max_steps: int, max_minutes: float
) -> bool:
    """步数或时长到顶就必须收尾，不能无限连环。"""

    if len(list(thread.get("steps") or [])) >= max(1, int(max_steps)):
        return True
    opened = float(thread.get("opened_at") or 0.0)
    if opened > 0 and max_minutes > 0:
        return (float(now) - opened) >= max_minutes * 60.0
    return False


def open_threads(threads: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        item
        for item in list(threads or [])
        if isinstance(item, dict) and str(item.get("status") or "open") == "open"
    ]
