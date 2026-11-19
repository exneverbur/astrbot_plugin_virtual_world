"""表达变奏：反讽 / 玩笑是「表达策略」层，不是情绪层（设计文档第 11 章 G1~G3）。

为什么单独一个纯逻辑模块：

1. 「开不开玩笑」必须和心情、好感这类数值彻底分开。这个模块只读
   :class:`ResponseBrief` 里的结构化字段和调用方显式传进来的入参。心情只在
   「这一轮用允许用哪几种说法」上起**收窄**作用（负面情绪下不许 ``playful_tease``），
   绝不能让玩笑变多——心情不是变奏的开关。
2. 变奏是低频特例。两次变奏之间至少隔 ``min_gap`` 轮回复，频率上限 ``rate``；
   两个都是可配置参数，且冷却的**真值在存档里**（``WorldState``），重启后仍然生效。
3. **分工（用户 m00001 的 C 部分）**：规则层不再自己挑风格。它只回答两件事——
   :func:`tease_permission`「这一轮**允不允许**损一句」，:func:`validate_variation`
   「模型写的这一版**能不能用**」。风格由主模型在回复 JSON 的 ``tease`` 字段里给；
   模型说 ``on=false`` 或者根本不写，回复一个字都不改（调用方不得改写她的原话）。
4. 变奏只改表达策略，不改理解、事实、承诺、动作和稳定态度。:func:`validate_variation`
   把这条约束做成确定性的结构检查：数字 / 时间只有在**本轮证据**（这一轮真正发给模型的
   请求、用户原话、已提交事实）里找不到才算编造；「我已经发好了」这类完成声明在没有
   对应成功动作时也算编造。违规时调用方应当退回直答重来一次，**不允许把整句话吞掉**。

判断「这一轮能不能变奏」用的是 :class:`ResponseBrief` 的结构化字段（用户需求、交际目的、
情境风险、关系态度）与显式入参，**不靠话题关键词猜**——「这个话题很好玩」不等于
「这个对象可以被逗」。
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BOLD_STRATEGIES",
    "DEFAULT_MIN_GAP",
    "DEFAULT_RATE",
    "DIRECT_LEAD",
    "FIDELITY_CLAUSE",
    "MAGNITUDES",
    "MILD_STRATEGIES",
    "RNG",
    "STRATEGIES",
    "VARIATION_STRATEGIES",
    "ResponseBrief",
    "TeaseGate",
    "adjust_magnitude",
    "coerce_brief",
    "normalize_magnitude",
    "relationship_stage",
    "risk_level",
    "tease_lead",
    "tease_permission",
    "validate_variation",
    "variation_instruction",
]

# ---------------- 策略与力度 ----------------

STRATEGIES: tuple[str, ...] = (
    "direct",
    "playful_tease",
    "dry_irony",
    "mock_formal",
    "understatement",
)
"""全部合法策略。``direct`` 是默认值，另外四种才是「变奏」。"""

VARIATION_STRATEGIES: tuple[str, ...] = (
    "playful_tease",
    "dry_irony",
    "mock_formal",
    "understatement",
)

MILD_STRATEGIES: tuple[str, ...] = ("dry_irony", "understatement")
"""风险最低的两种。场景没有明确「开着」（``scene_open=False``）时只能用它们。"""

BOLD_STRATEGIES: tuple[str, ...] = ("playful_tease", "mock_formal")
"""打趣 / 夸张正经：需要场景明确开着、情境低风险、关系够熟，三者同时成立。"""

MAGNITUDES: tuple[str, ...] = ("none", "light", "medium", "strong")

DEFAULT_RATE = 0.15
"""默认只有 15% 的合格轮次允许变奏（可配置，见 ``tease_permission(rate=...)``）。"""

DEFAULT_MIN_GAP = 3
"""两次变奏之间至少间隔 3 轮回复（可配置，见 ``tease_permission(min_gap=...)``）。"""

RNG = random.SystemRandom()
"""频率骰子的来源：**真随机**，每次判定独立。

旧实现把种子写成 ``f"vw-variation|{session_id}|{turn}"``，同一会话同一轮永远同一结果——
「运气」可复现，于是也就能被读成「她今天说不说俏皮话是排好的」。这里改成操作系统的
熵源：同一个会话同一轮连判两次，结果可以不同。要固定结果复盘时传 ``dice=`` 注入。
"""

# 每种策略自己的力度天花板：冷幽默 / 轻描淡写天生是「轻」，
# 打趣和夸张正经可以到「中」，但都不该到「强」（那已经不是玩笑，是换了个态度）。
STRATEGY_MAGNITUDE_CAP: dict[str, str] = {
    "direct": "none",
    "playful_tease": "medium",
    "dry_irony": "light",
    "mock_formal": "medium",
    "understatement": "light",
}

_RISK_MAGNITUDE_CAP: dict[str, str] = {"high": "none", "medium": "light", "low": "strong"}
_STAGE_MAGNITUDE_CAP: dict[str, str] = {"stranger": "none", "acquaintance": "light", "familiar": "strong"}

_MAGNITUDE_ALIASES: dict[str, str] = {
    "": "",
    "none": "none",
    "off": "none",
    "no": "none",
    "无": "none",
    "不": "none",
    "light": "light",
    "mild": "light",
    "slight": "light",
    "low": "light",
    "轻": "light",
    "轻微": "light",
    "小": "light",
    "medium": "medium",
    "moderate": "medium",
    "mid": "medium",
    "中": "medium",
    "中等": "medium",
    "strong": "strong",
    "high": "strong",
    "强": "strong",
    "大": "strong",
    "重": "strong",
    "放开": "strong",
}

# ---------------- 结构化词汇表 ----------------
#
# 这里对的是 brief 的**字段值**（受控词表），不是话题文本。
# 把「难过」写进 user_need 是结构信息；从用户那句话里搜「难过」两个字来猜，是设计禁止的做法。

SERIOUS_USER_NEEDS: frozenset[str] = frozenset(
    {
        "serious_help",
        "urgent_help",
        "help",
        "trouble",
        "comfort",
        "comfort_me",
        "grief",
        "apology",
        "apologize",
        "conflict",
        "repair",
        "complaint",
        "safety",
        "emergency",
        "medical",
        "crisis",
        "decision",
        "important",
        "求助",
        "认真求助",
        "紧急",
        "难过",
        "安慰",
        "道歉",
        "冲突",
        "安全",
        "重要",
        "严肃",
    }
)
"""对方这一轮的需求落在这些值上：属于脆弱 / 求助场景，任何策略都不许变奏。"""

SERIOUS_GOALS: frozenset[str] = frozenset(
    {
        "repair",
        "apologize",
        "comfort",
        "help",
        "warn",
        "refuse",
        "conflict",
        "safety",
        "grieve",
        "bad_news",
        "serious_answer",
        "confirm_commitment",
        "道歉",
        "安慰",
        "求助",
        "警告",
        "拒绝",
        "冲突",
        "严肃回答",
    }
)
"""本轮的实际交际目的落在这些值上：同样一律直答。"""

DEFENSIVE_STANCES: frozenset[str] = frozenset(
    {
        "stranger",
        "distant",
        "formal",
        "professional",
        "hostile",
        "grieving",
        "cautious",
        "陌生人",
        "陌生",
        "疏远",
        "正式",
        "敌对",
        "敌意",
        "戒备",
        "悲伤",
    }
)
"""brief 里声明的关系态度：已经明确疏远 / 正式 / 敌对时，不允许用玩笑靠近。"""

STRANGER_STAGES: frozenset[str] = frozenset(
    {"stranger", "new", "just_met", "unfamiliar", "外人", "陌生", "陌生人", "生人", "不熟", "刚认识", "初识", "不认识"}
)
ACQUAINTANCE_STAGES: frozenset[str] = frozenset(
    {"acquaintance", "colleague", "coworker", "normal", "路人", "普通熟人", "熟人", "同事", "一般"}
)
FAMILIAR_STAGES: frozenset[str] = frozenset(
    {"familiar", "friend", "close", "intimate", "buddy", "朋友", "亲近", "亲密", "很熟", "好友"}
)

HIGH_RISK: frozenset[str] = frozenset({"high", "severe", "critical", "blocked", "高", "很高", "严重"})
LOW_RISK: frozenset[str] = frozenset({"low", "none", "safe", "低", "无", "低风险"})

NEGATIVE_FEEDBACK: frozenset[str] = frozenset(
    {
        "negative",
        "neg",
        "bad",
        "dislike",
        "disliked",
        "annoyed",
        "uncomfortable",
        "refused",
        "refuse",
        "stop",
        "cool_down",
        "no_joke",
        "serious",
        "负面",
        "不喜欢",
        "反感",
        "叫停",
        "停下",
        "别开玩笑",
        "不好笑",
        "拒绝",
        "严肃",
    }
)
_NEGATIVE_MARKS: tuple[str, ...] = (
    "negative",
    "dislike",
    "annoy",
    "uncomfort",
    "not funny",
    "别开玩笑",
    "不喜欢",
    "不好笑",
    "叫停",
    "别闹",
    "反感",
    "拒绝",
    "停下",
    "收住",
)

BOT_ACTORS: frozenset[str] = frozenset({"bot", "self", "assistant", "character", "机器人", "角色", "她", "自己"})
"""反馈条目的 actor 落在这些值上时，是**她自己**说的反话，不能当成用户对玩笑的反馈。"""

# ---------------- 提示词片段 ----------------

FIDELITY_CLAUSE = (
    "硬约束（任何策略下都必须遵守）：不改变事实——只用上文已经给出的事实，"
    "不补充没出现过的细节、时间、地点或东西；不新增承诺——不说上文没有的"
    "“我保证 / 我一定 / 我答应你”，也不约定新的时间、地点、事情或东西；"
    "不改变已经定下的动作；不减少对对方的认可与亲近，不真的贬低、嘲笑或攻击对方。"
)

DIRECT_LEAD = "这一轮不采用表达变奏：直接回答，说清楚就行，不用反讽也不用玩笑。"

_STRATEGY_LEADS: dict[str, str] = {
    "playful_tease": (
        "这一轮采用「玩笑打趣」的表达策略（力度上限：{mag}）：接住话头轻轻逗一下，"
        "分寸停在对方能笑着接住的地方；对方不接就收住，不追着逗。"
    ),
    "dry_irony": (
        "这一轮采用「冷幽默 / 正经反讽」的表达策略（力度上限：{mag}）："
        "用一本正经的语气说一句轻微反着来的话，好笑来自语气反差，不来自内容失真。"
    ),
    "mock_formal": (
        "这一轮采用「夸张正经」的表达策略（力度上限：{mag}）：把事情说得过分正式、"
        "过分隆重，反差本身就是玩笑；别让对方以为你真的变了态度。"
    ),
    "understatement": (
        "这一轮采用「轻描淡写」的表达策略（力度上限：{mag}）：把明显的事情说得很小、"
        "很平常，留出让对方接话的空。"
    ),
}

_MAGNITUDE_HINT: dict[str, str] = {"none": "不施加", "light": "轻微", "medium": "中等", "strong": "放开一点"}


# ---------------- 小工具 ----------------


def _text(value: Any) -> str:
    """压平空白后的字符串；``None`` 一律变空串（缺字段不等于要猜）。"""

    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _key(value: Any) -> str:
    return _text(value).lower()


def _field(data: Any, name: str) -> Any:
    """从 dict 或任何带同名属性的对象里取字段（两种都接受，方便调用方直接传模型对象）。"""

    if isinstance(data, Mapping):
        return data.get(name)
    return getattr(data, name, None)


_MAPPING_TEXT_KEYS: tuple[str, ...] = ("text", "summary", "fact", "content", "name", "action", "id", "label")


def _texts(value: Any) -> tuple[str, ...]:
    """把任意形状的东西收敛成一组非空文本（str / dict / list / 对象都能吃）。"""

    if value is None:
        return ()
    if isinstance(value, str):
        item = _text(value)
        return (item,) if item else ()
    if isinstance(value, Mapping):
        items: list[str] = []
        for key, item in value.items():
            text = _text(item) or _text(key)
            if text:
                items.append(text)
        return tuple(items)
    if isinstance(value, Iterable):
        items = []
        for entry in value:
            if isinstance(entry, Mapping):
                text = ""
                for name in _MAPPING_TEXT_KEYS:
                    text = _text(entry.get(name))
                    if text:
                        break
            else:
                text = _text(entry)
            if text:
                items.append(text)
        return tuple(items)
    text = _text(value)
    return (text,) if text else ()


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _clamp01(value: Any) -> float | None:
    """把外部注入的骰子夹到 [0, 1]；给的不是数字就返回 None（表示「没有注入」）。"""

    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _magnitude_index(value: str) -> int:
    normalized = normalize_magnitude(value)
    return MAGNITUDES.index(normalized) if normalized in MAGNITUDES else 0


def normalize_magnitude(value: Any) -> str:
    """力度词的规范写法；不认识的词返回空串（不猜大也不猜小）。"""

    return _MAGNITUDE_ALIASES.get(_key(value), "")


def risk_level(value: Any) -> str:
    """情境风险的粗档。未知按 ``medium``：收紧到「轻」，但不至于像 ``high`` 那样直接禁掉。"""

    key = _key(value)
    if key in HIGH_RISK:
        return "high"
    if key in LOW_RISK:
        return "low"
    return "medium"


def relationship_stage(value: Any) -> str:
    """关系阶段的粗档。

    未知（包括没传）按「普通熟人」处理：只允许最轻的两档策略、力度不超过「轻」。
    这样默认值不会因为一个缺失字段就永远直答，也不会让陌生人直接挨玩笑。
    """

    key = _key(value)
    if key in STRANGER_STAGES:
        return "stranger"
    if key in FAMILIAR_STAGES:
        return "familiar"
    return "acquaintance"


# ---------------- ResponseBrief ----------------


@dataclass
class ResponseBrief:
    """一轮回复之前生成的小型结构说明（G2：正常 prompt 构造前生成）。

    这些字段是**判断能不能变奏的唯一依据**，所以刻意做得结构化：不给「这一轮心情」
    留位置。校验函数也靠它的 ``fact_sources`` / ``actions`` 判断变奏有没有编东西。
    """

    audience: str = ""
    """这一轮说给谁听（群 / 私聊 / 具体某人）。"""

    user_need: str = ""
    """对方这一轮的真实需求（受控词表，如 ``banter`` / ``serious_help`` / ``comfort``）。"""

    fact_sources: tuple[str, ...] = ()
    """这一轮允许使用的事实来源（已提交事实 / 用户原话的原文）。变奏不许超出它们。"""

    communicative_goal: str = ""
    """本轮的实际交际目的（受控词表，如 ``answer`` / ``comfort`` / ``repair``）。"""

    relationship_stance: str = ""
    """这一轮对她的关系态度（受控词表，如 ``close_friend`` / ``distant`` / ``formal``）。"""

    allowed_magnitude: str = ""
    """调用方允许的表达力度上限（``none`` / ``light`` / ``medium`` / ``strong``）。"""

    context_risk: str = ""
    """情境风险（``low`` / ``medium`` / ``high``）：冲突、公开、敏感话题都算高风险。"""

    actions: tuple[str, ...] = ()
    """本轮计划要做的动作。变奏只能改说法，不能加、也不能减这些动作。"""

    known_text: str = ""
    """可选：本轮上文（用户原话、已提交事实的原文）拼成的文本。

    校验「变奏有没有编事实」时会拿它当已知背景，调用方填得越全，误报越少。
    """

    extra: dict[str, Any] = field(default_factory=dict)
    """调用方自用的附加信息。本模块**不读**它——尤其不要拿它塞 mood 来影响策略。"""

    def to_dict(self) -> dict[str, Any]:
        """给持久化 / 日志用的普通 dict（列表字段转成 list，方便 json）。"""

        return {
            "audience": self.audience,
            "user_need": self.user_need,
            "fact_sources": list(self.fact_sources),
            "communicative_goal": self.communicative_goal,
            "relationship_stance": self.relationship_stance,
            "allowed_magnitude": self.allowed_magnitude,
            "context_risk": self.context_risk,
            "actions": list(self.actions),
            "known_text": self.known_text,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ResponseBrief":
        """从 dict（或带同名属性的对象）还原；缺字段按空处理，**绝不抛异常**。"""

        if data is None:
            return cls()
        extra_raw = _field(data, "extra")
        return cls(
            audience=_text(_field(data, "audience")),
            user_need=_text(_field(data, "user_need")),
            fact_sources=_texts(_field(data, "fact_sources")),
            communicative_goal=_text(_field(data, "communicative_goal")),
            relationship_stance=_text(_field(data, "relationship_stance")),
            allowed_magnitude=_text(_field(data, "allowed_magnitude")),
            context_risk=_text(_field(data, "context_risk")),
            actions=_texts(_field(data, "actions")),
            known_text=_text(_field(data, "known_text")),
            extra=dict(extra_raw) if isinstance(extra_raw, Mapping) else {},
        )

    def is_blank(self) -> bool:
        """一个字的结构信息都没有：等于「没有 brief」。"""

        return not any(
            (
                self.audience,
                self.user_need,
                self.fact_sources,
                self.communicative_goal,
                self.relationship_stance,
                self.allowed_magnitude,
                self.context_risk,
                self.actions,
                self.known_text,
            )
        )


def coerce_brief(value: Any) -> ResponseBrief | None:
    """把 ``ResponseBrief`` / dict / 带属性的对象统一成 brief；空的返回 ``None``。

    返回 ``None`` 是「信息不足」的信号，调用方应当按默认直答处理，不要替它猜。
    """

    if value is None:
        return None
    brief = value if isinstance(value, ResponseBrief) else ResponseBrief.from_dict(value)
    return None if brief.is_blank() else brief


# ---------------- 许可（TeaseGate） ----------------


@dataclass
class TeaseGate:
    """这一轮「损一句」的许可，同时是给观测 / 验收用的记录。

    规则层只回答「允不允许」：``allowed`` 是结论，``styles`` 是**这一轮允许的风格**
    （由风险、关系、场景、心情共同收窄），``magnitude`` 是力度上限。
    具体用哪种、损哪句，由主模型在回复 JSON 的 ``tease`` 字段里决定——这里不挑。

    ``actor`` 固定标成她（bot）：这些字段描述的是**她自己**的表达策略，
    不是对用户语气的判定，回写时不要拿它去给用户扣好感 / 记 attack。
    """

    allowed: bool = False
    reason: str = ""
    blocked_by: str = ""
    """机读的拦截原因：``brief`` / ``persona`` / ``refusal`` / ``serious`` / ``need`` /
    ``goal`` / ``risk`` / ``stance`` / ``stage`` / ``feedback`` / ``scene`` / ``mood`` /
    ``failed`` / ``gap`` / ``rate`` / ``magnitude``。空串表示放行。"""

    styles: tuple[str, ...] = ()
    magnitude: str = "none"
    roll: float = 0.0
    goal: str = ""
    stance: str = ""
    fact_sources: tuple[str, ...] = ()
    actor: str = "bot"

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "blocked_by": self.blocked_by,
            "styles": list(self.styles),
            "magnitude": self.magnitude,
            "roll": self.roll,
            "goal": self.goal,
            "stance": self.stance,
            "fact_sources": list(self.fact_sources),
            "actor": self.actor,
        }

    def allows(self, style: Any) -> bool:
        """模型给的风格这一轮能不能用（不知道 / 不在名单里一律不能用）。"""

        key = _key(style)
        return bool(self.allowed and key and key in self.styles)



# ---------------- 提示词指令 ----------------


def variation_instruction(strategy: str, *, magnitude: str = "light", reason: str = "") -> str:
    """生成给主模型看的中文指令：用哪种策略、力度上限多少、硬约束是什么。

    未知策略退回直接回答的措辞——猜策略比不猜更危险。
    """

    key = _key(strategy)
    lead = _STRATEGY_LEADS.get(key)
    if lead is None:
        lead = DIRECT_LEAD
    else:
        level = normalize_magnitude(magnitude) or "light"
        lead = lead.format(mag=_MAGNITUDE_HINT.get(level, "轻微"))
    lines = [lead]
    if reason:
        lines.append(f"（本轮判定：{reason}）")
    lines.append(FIDELITY_CLAUSE)
    return "\n".join(lines)


# ---------------- 频率 / 反馈 / 间隔 ----------------


def _feedback_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _first_value(data: Mapping[Any, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in data and _text(data[name]):
            return data[name]
    return None


def _feedback_entry(item: Any) -> tuple[str, str]:
    """把一条反馈归一成 ``(actor, verdict)``；认不出来就返回空串，不猜。"""

    if isinstance(item, Mapping):
        actor = _key(_first_value(item, ("actor", "source", "from", "who")))
        verdict = _key(_first_value(item, ("verdict", "feedback", "sentiment", "polarity", "result", "value", "kind")))
        if not verdict:
            for key, value in item.items():
                if _key(key) in BOT_ACTORS:
                    actor = actor or _key(key)
                elif _text(value):
                    verdict = verdict or _key(value)
        return actor, verdict
    if isinstance(item, str):
        return "", _key(item)
    actor = _key(getattr(item, "actor", ""))
    verdict = _key(getattr(item, "verdict", "") or getattr(item, "feedback", ""))
    if not verdict:
        verdict = _key(item)
    return actor, verdict


def _looks_negative(verdict: str) -> bool:
    return bool(verdict) and (verdict in NEGATIVE_FEEDBACK or any(mark in verdict for mark in _NEGATIVE_MARKS))


def _feedback_blocked(recent_feedback: Any) -> str:
    """用户近期对玩笑给过负面反馈就返回原因（空串 = 不阻塞）。

    她自己说的反话（actor 是 bot / self）直接跳过——G2 明确要求不能把 bot 的反话
    喂回「用户语气」判定，那会凭空生成一条对自己的负面反馈。
    """

    for item in _feedback_items(recent_feedback):
        actor, verdict = _feedback_entry(item)
        if actor and actor in BOT_ACTORS:
            continue
        if _looks_negative(verdict):
            return f"用户近期对玩笑给过负面反馈（{verdict}），这一轮直接回答"
    return ""


def _strategy_pool(stage: str, risk: str, scene_open: bool) -> tuple[str, ...]:
    """这一轮可选哪几种策略。

    打趣 / 夸张正经要三个条件同时成立：场景明确开着（用户接得住）、情境低风险、
    关系够熟。否则只留最轻的两档——变奏可以发生，但不会是「逗你」。
    """

    bold_ok = bool(scene_open) and risk == "low" and stage == "familiar"
    return VARIATION_STRATEGIES if bold_ok else MILD_STRATEGIES


# ---------------- 力度 ----------------


def _magnitude_cap(
    brief: ResponseBrief | Mapping[str, Any] | None,
    relationship: Any = None,
    *,
    style: str | None = None,
) -> str:
    """力度上限 = 情境风险、关系距离、brief 自己声明的上限（以及可选的风格上限）里最小的。

    许可判定时还没有风格（风格由模型给），所以 ``style`` 是可选的：
    判定阶段只算「这一轮最多能轻到什么程度」，模型挑完风格后由
    :func:`adjust_magnitude` 再把风格上限叠上。
    """

    resolved = coerce_brief(brief) or ResponseBrief()
    # brief 自己声明的上限也是上限，不能因为策略池签了 medium 就自动涨到 medium
    base = normalize_magnitude(resolved.allowed_magnitude) or "light"
    caps = [
        _RISK_MAGNITUDE_CAP[risk_level(resolved.context_risk)],
        _STAGE_MAGNITUDE_CAP[relationship_stage(relationship)],
        base,
    ]
    if style:
        caps.append(STRATEGY_MAGNITUDE_CAP.get(_key(style), "none"))
    if _key(resolved.relationship_stance) in DEFENSIVE_STANCES:
        caps.append("none")
    return min(caps, key=_magnitude_index)


def adjust_magnitude(
    brief: ResponseBrief | Mapping[str, Any] | None,
    strategy: str,
    *,
    relationship: Any = None,
) -> str:
    """这一轮真正允许的力度上限 = 策略上限、情境风险、关系距离三者里最小的那个。

    ``relationship`` 是可选的关键字参数：关系距离是调用方每一轮都有的运行时事实，
    放在这里比塞进 brief 更不容易过期。缺 ``allowed_magnitude`` 或写了不认识的词时
    兜底 ``light``——不猜大。
    """

    return _magnitude_cap(brief, relationship, style=strategy)


# ---------------- 许可判定 ----------------


def _style_hint(style: str) -> str:
    """一种风格的短说明：从 ``_STRATEGY_LEADS`` 里抽，避免同一句话写两遍。"""

    lead = _STRATEGY_LEADS.get(_key(style)) or ""
    match = re.match(r"^这一轮采用「(.+?)」的表达策略（力度上限：[^）]*）：(.+)$", lead)
    if not match:
        return lead
    return f"{match.group(1)}——{match.group(2)}"


def _mood_is_negative(value: Any) -> bool:
    """这一轮的情绪算不算负面（只用来**排除** playful_tease，不会让玩笑变多）。"""

    return _looks_negative(_key(value))


def tease_permission(
    brief: ResponseBrief | Mapping[str, Any] | None,
    *,
    recent_feedback: Iterable[Any] = (),
    relationship: Any = None,
    persona_allows: bool = True,
    last_variation_turn: int | None = None,
    turn_index: int = 0,
    rate: float = DEFAULT_RATE,
    min_gap: int = DEFAULT_MIN_GAP,
    scene_open: bool = False,
    busy: bool = False,
    mood: Any = "",
    failed: bool = False,
    dice: float | None = None,
    explicit_refusal: bool = False,
    serious: bool = False,
) -> TeaseGate:
    """判定这一轮**允不允许**损一句。它不挑风格——风格由主模型在 ``tease.style`` 里给。

    顺序（先硬门禁，再护栏）：

    1. 结构够不够（brief / 人设 / 明确拒绝 / 严肃场景 / 脆弱需求 / 严肃目的）；
    2. 情境够不够（高风险 / 疏远态度 / 陌生关系 / 近期负面反馈）；
    3. **三闸**：场景（她手头正忙着要紧事）、失败回合（这一轮已经有东西没做成）；
    4. 护栏：最小间隔 → 频率骰子（真随机）→ 力度是否归零；
    5. **心情**只用来收窄风格：负面情绪下排除 ``playful_tease``，更轻的两种仍然可用。
       心情**从不**增加变奏，也从不单独决定「这一轮要不要损」。

    关于骰子：它只决定「这个已经合格的轮次要不要变奏」，每次判定独立（:data:`RNG`）。
    同一个会话同一轮连判两次，结果可以不同；需要固定结果复盘时用 ``dice=`` 注入。

    关于失败回合的时序：动作在**生成之后**才执行，所以这个函数在生成前拿不到「本轮
    动作失败」这个事实。调用方生成前传 ``failed=False``，动作跑完后再补一次判定
    （那时 ``last_variation_turn`` 还没更新，其余门禁结论不变）。
    """

    injected = _clamp01(dice)
    roll = RNG.random() if injected is None else injected
    resolved = coerce_brief(brief)

    def blocked(kind: str, reason: str) -> TeaseGate:
        return TeaseGate(
            allowed=False,
            reason=reason,
            blocked_by=kind,
            roll=round(roll, 4),
            magnitude="none",
            goal=resolved.communicative_goal if resolved else "",
            stance=resolved.relationship_stance if resolved else "",
            fact_sources=resolved.fact_sources if resolved else (),
        )

    if resolved is None:
        return blocked("brief", "本轮没有可用的 ResponseBrief（结构信息缺失），不猜")
    missing = [
        name
        for name in ("communicative_goal", "user_need")
        if not _text(getattr(resolved, name))
    ]
    if missing:
        return blocked("brief", "ResponseBrief 缺关键字段（" + "、".join(missing) + "），不猜")
    if not persona_allows:
        return blocked("persona", "人设不允许表达变奏（persona_allows=False）")
    if explicit_refusal:
        return blocked("refusal", "用户明确要求正经 / 拒绝玩笑（explicit_refusal=True）")
    if serious:
        return blocked("serious", "本轮是严肃场景（serious=True）")

    need = _key(resolved.user_need)
    if need in SERIOUS_USER_NEEDS:
        return blocked(
            "need",
            f"对方这一轮的需求是「{resolved.user_need}」，属于脆弱 / 求助场景，不玩笑",
        )
    goal = _key(resolved.communicative_goal)
    if goal in SERIOUS_GOALS:
        return blocked("goal", f"本轮交际目的是「{resolved.communicative_goal}」，不适合表达变奏")
    risk = risk_level(resolved.context_risk)
    if risk == "high":
        return blocked("risk", f"情境风险「{resolved.context_risk}」偏高，不适合玩笑")
    stance = _key(resolved.relationship_stance)
    if stance in DEFENSIVE_STANCES:
        return blocked(
            "stance",
            f"关系态度「{resolved.relationship_stance}」明确疏远 / 正式，不用玩笑靠近",
        )
    stage = relationship_stage(relationship)
    if stage == "stranger":
        return blocked("stage", f"关系阶段「{_text(relationship) or '未知'}」还不允许玩笑")

    rejected = _feedback_blocked(recent_feedback)
    if rejected:
        return blocked("feedback", rejected)

    # 三闸之一：场景。她手头正压着一件不可打断的事时不损——这时的玩笑会被读成
    # 「没把要紧事当回事」。（可用性由调用方从 state.is_busy 取，见 engine。）
    if busy:
        return blocked("scene", "她这一轮手头正忙着要紧事（动作进行中），不打趣")

    # 三闸之二：失败回合。这一轮已经有动作 / 工具没做成时，损一句等于在挫折上补刀。
    if failed:
        return blocked("failed", "这一轮有动作 / 工具没做成，不损")

    gap_need = max(0, _as_int(min_gap, DEFAULT_MIN_GAP))
    if last_variation_turn is not None:
        gap = _as_int(turn_index, 0) - _as_int(last_variation_turn, 0)
        # gap < 0 说明轮次号倒流（换会话 / 换存档）：历史不算数，按没记录处理，别锁死功能
        if 0 <= gap < gap_need:
            return blocked(
                "gap", f"距上次变奏只隔 {gap} 轮（要求至少 {gap_need} 轮），这一轮不损"
            )

    rate_value = _as_float(rate, DEFAULT_RATE)
    if rate_value <= 0.0:
        return blocked("rate", "配置里关掉了表达变奏（rate≤0），这一轮不损")
    if roll >= rate_value:
        return blocked(
            "rate", f"这一轮没抽中变奏（骰子 {roll:.4f} ≥ 频率 {rate_value:g}），不损"
        )

    magnitude = _magnitude_cap(resolved, relationship)
    if magnitude == "none":
        return blocked(
            "magnitude",
            f"力度被收紧到 none（风险 {risk}、关系 {stage}），这一轮不施加变奏",
        )

    styles = tuple(
        style
        for style in _strategy_pool(stage, risk, scene_open)
        if STRATEGY_MAGNITUDE_CAP.get(style, "none") != "none"
    )
    if not styles:
        return blocked("magnitude", "没有可用力度的风格，这一轮不施加变奏")

    # 三闸之三：心情。负面情绪下只排除 playful_tease（打趣最容易被读成嘲讽），
    # 冷幽默 / 轻描淡写仍然允许。心情从不增加变奏，也不单独决定「不损」。
    if _mood_is_negative(mood):
        styles = tuple(style for style in styles if style != "playful_tease")
        if not styles:
            return blocked("mood", f"她这一轮情绪是负面的（{_text(mood)}），不打趣")

    reason = (
        f"合格轮次：骰子 {roll:.4f} < 频率 {rate_value:g}；"
        f"情境风险 {risk}、关系 {stage}、场景{'已开' if scene_open else '未明说'}；"
        f"允许的风格：{'、'.join(styles)}（力度上限 {magnitude}）"
    )
    return TeaseGate(
        allowed=True,
        reason=reason,
        blocked_by="",
        styles=styles,
        magnitude=magnitude,
        roll=round(roll, 4),
        goal=resolved.communicative_goal,
        stance=resolved.relationship_stance,
        fact_sources=resolved.fact_sources,
    )


def tease_lead(gate: TeaseGate, *, schema: bool = True) -> str:
    """给主模型的「这一轮可以损一句」提示；没有许可时返回空串（提示词一个字不改）。

    这里只说**怎么表达**和**硬约束**，不替模型决定要不要损：决定权在模型手里，
    调用方拿到 ``tease.on=true`` 之后再按许可 / 保真 / 冷却过一遍。
    """

    if not gate.allowed or not gate.styles:
        return ""
    hints = "；".join(f"{style}={_style_hint(style)}" for style in gate.styles)
    if not schema:  # [vw-eval-ABCD] A：精简 schema 里没有 tease 字段
        # reply_schema.legacy=true 时 format_layer 已把 tease 字段摘掉，
        # 再写"想损就写 tease:{...}"就是让模型去写一个不存在的键 → 它直接放弃表达。
        # 这里改回 10-21 的「指派」口气：这一轮就这么说，不说"可选"。
        return (
            "这一轮**采用**下面的表达策略（不是可选项）："
            f"顺手损他一句，风格取 {hints} 之一，"
            "损的是他这句话／这个点，不是他的人。\n"
            + FIDELITY_CLAUSE
        )
    return (
        "这一轮可以顺手损对方一句（**可选**，不是任务）：想损就在 JSON 里写 "
        'tease: {"on": true, "style": "...", "target": "..."}，'
        f"style 取 {hints} 之一，target 写清损的是哪句话 / 哪个点；"
        "不想损就写 tease.on=false，或者干脆不写这个字段。\n" + FIDELITY_CLAUSE
    )


# ---------------- 保真校验 ----------------

# 「新增承诺」的句型。命中的句子一旦在 brief 的已知文本里找不到，就判失败：
# 玩笑不许变出一句"我保证…"。
_COMMIT_RE = re.compile(
    r"(?:我|咱|这)[^。！？!?\n]{0,12}(?:保证|答应|承诺|发誓|说好了?|说定|包在我身上|一言为定)"
    r"|一定(?:会|要|给|帮|去|来)"
    r"|(?:promise|i\s+will\s+make\s+sure)",
    re.IGNORECASE,
)

# 事实锚点：具体数字与时间词。它们在**这一轮证据**里找不到，才算凭空多了一个细节。
_DIGIT_RE = re.compile(r"\d+(?:\.\d+)?")
_TIME_RE = re.compile(
    r"今天|明天|后天|大后天|昨天|前天|下周|这周|上周|周末|"
    r"周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"上午|中午|下午|晚上|凌晨|早上|夜里|半夜|"
    r"小时|分钟|半个月|一个月|明年|下个月"
)

# 真实贬低：玩笑可以反着说，但不能真骂。
_INSULT_RE = re.compile(r"你真(?:笨|蠢|傻|废|差)|白痴|废物|垃圾东西|蠢货|没用的东西|滚开")

# 「假完成态」：嘴上说已经做完了一件事，但这一轮根本没有对应的成功动作。
# 只认**声称自己执行过**的说法——「我记住了」这类口头应和不算（那是接话，不是报执行结果）。
_DONE_CLAIMS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"(?:已经|刚刚|刚)(?:给你|跟你)?(?:发|送|寄|传)(?:出去|过去|给你|了|好)"),
        "send",
        "发送",
    ),
    (re.compile(r"发好了|发出去了|传过去了|寄出去了|发给你了"), "send", "发送"),
    (
        re.compile(r"给你记好|记好了|记住了|记下了|记到备忘|记进备忘|已经记好|已经记下"),
        "remember",
        "记录",
    ),
    (
        re.compile(
            r"(?:已经|刚刚|刚)(?:办好|办妥|搞定|弄好|做完|做好)|(?:办好了|办妥了|搞定了|弄好了|做完了|做好了)"
        ),
        "do",
        "办完 / 做完",
    ),
)

_SEND_MARKS: tuple[str, ...] = (
    "send",
    "share",
    "meme",
    "image",
    "voice",
    "file",
    "mail",
    "upload",
    "deliver",
    "poke",
    "发送",
    "分享",
)
_REMEMBER_MARKS: tuple[str, ...] = ("remember", "memory", "note", "记")
# 这些不是「做成一件事」的动作：只说话 / 只想 / 只是等着，不能拿来给完成声明作证。
_NON_WORK_ACTIONS: frozenset[str] = frozenset({"", "say", "think", "wait", "silence", "noop", "none"})

_PLAN_TEXT_KEYS: tuple[str, ...] = ("say", "text", "reply", "message", "lines", "content")
_PLAN_ACTION_KEYS: tuple[str, ...] = ("actions", "action_list", "action", "plan")
_PLAN_DECLARED_KEYS: tuple[str, ...] = ("new_facts", "added_facts", "facts", "new_promises", "promises")


def _norm_number(token: str) -> str:
    """数字归一：``2024`` / ``2024.0`` 算同一个，避免「查到的是 2024 年」被误拦。"""

    try:
        number = float(token)
    except (TypeError, ValueError):
        return token
    return str(int(number)) if number.is_integer() else format(number, ".10g")


def _numbers_in(text: str) -> set[str]:
    return {_norm_number(match.group(0)) for match in _DIGIT_RE.finditer(text)}


def _evidence_texts(evidence: Any) -> list[str]:
    """把调用方给的「本轮证据」摊平成文本片段（dict / 对象 / 字符串都接受）。"""

    pieces: list[str] = []
    for item in _feedback_items(evidence):
        if isinstance(item, str):
            if item:
                pieces.append(item)
        elif isinstance(item, Mapping):
            pieces.extend(_texts(item.values()))
        else:
            pieces.extend(_texts(item))
    return pieces


def _action_ids(values: Any) -> set[str]:
    """把动作名 / 计划动作（str / dict / 带属性的对象）统一成小写 id 集合。"""

    ids: set[str] = set()
    for item in _feedback_items(values):
        if isinstance(item, Mapping):
            name = _first_value(item, ("action", "id", "type", "name", "kind"))
        else:
            name = getattr(item, "type", None) or getattr(item, "action", None) or item
        key = _key(name)
        if key:
            ids.add(key)
    return ids


def _claim_has_evidence(kind: str, actions: Any, completed: Any, failed: Any) -> bool:
    """这条完成声明有没有对应的**成功**动作（失败的那个不算）。"""

    pool = _action_ids(actions) | _action_ids(completed)
    if kind == "send":
        hits = {name for name in pool if any(mark in name for mark in _SEND_MARKS)}
    elif kind == "remember":
        hits = {name for name in pool if any(mark in name for mark in _REMEMBER_MARKS)}
    else:
        hits = {name for name in pool if name not in _NON_WORK_ACTIONS}
    if not hits:
        return False
    # 只有「该做的那个动作失败了」才拦住；别的动作失败不影响「我把东西发给你了」这类声明
    return bool(hits - _action_ids(failed))


def _known_blob(brief: ResponseBrief, *, evidence: Any = ()) -> str:
    """校验时当作「已知」的全部文本。

    来源三处：brief 字段（模型刚总结的）、``brief.known_text`` / ``fact_sources``、
    以及调用方传进来的**本轮证据**——这一轮真正发给模型的请求（含用户原话）、
    ``event_log`` / ``state`` 里可见的值。数字 / 时间只有在这里都找不到才算编造；
    找不到证据但不涉及具体数值的句子不拦（那是措辞，不是事实）。
    """

    parts = list(brief.fact_sources)
    parts.extend(
        [
            brief.known_text,
            brief.communicative_goal,
            brief.user_need,
            brief.audience,
            brief.relationship_stance,
            *brief.actions,
        ]
    )
    parts.extend(_evidence_texts(evidence))
    return "\n".join(part for part in parts if part)


def _plan_first_value(plan: Mapping[Any, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in plan and _texts(plan[name]):
            return plan[name]
    return None


def _plan_text(raw: Any) -> str:
    """从「文本或回复计划」里取出所有该当文本校验的片段。"""

    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        pieces: list[str] = []
        for name in _PLAN_TEXT_KEYS + _PLAN_DECLARED_KEYS:
            pieces.extend(_texts(raw.get(name)))
        return "\n".join(pieces)
    return "\n".join(_texts(raw))


def validate_variation(
    text: Any,
    brief: ResponseBrief | Mapping[str, Any] | None,
    *,
    strategy: str = "direct",
    evidence: Iterable[Any] = (),
    actions: Iterable[Any] = (),
    completed: Iterable[Any] = (),
    failed: Iterable[Any] = (),
) -> tuple[bool, list[str]]:
    """校验一段变奏（文本或回复计划）有没有改掉事实 / 动作 / 承诺。

    只做**确定性结构检查**，不做语义理解、不调 LLM：宁可有误报，也不放过
    「为了一句玩笑编出一个事实」。调用方拿到 ``ok=False`` 时必须**退回直答重来一次**
    或者原样发送，**绝不能把整句话吞掉**（那是上一批的教训）。

    检查项：
    1. 策略名合法；
    2. 新增承诺 / 约定（证据里本来没有的「我保证 / 我一定 / 我答应你」）；
    3. 新增事实锚点——数字（按数值比较，``2024`` 与 ``2024 年`` 等价）与时间词，
       只要在本轮证据里找不到才算编造；不含具体数值的句子不拦；
    4. **假完成态**——「已经发好了 / 办妥了」这类完成声明，这一轮没有任何对应的
       成功动作（``actions`` / ``completed`` 里有、且不在 ``failed`` 里）→ 拦；
    5. 真实贬低 / 攻击性表达；
    6. 回复计划新增或减少了 brief 已定的动作，或声明了证据里没有的事实 / 承诺。

    ``evidence`` 是「这一轮真正给到模型的信息」：调用方应当把请求快照（system + user
    prompt，里面就有用户原话）、可见的 ``event_log`` / ``state`` 值一起传进来。
    ``brief`` 为空时直接失败：没有 brief 就无法确认事实没被改写。
    """

    reasons: list[str] = []
    if _key(strategy) not in STRATEGIES:
        reasons.append(f"未知的表达策略：{strategy}")
    resolved = coerce_brief(brief)
    if resolved is None:
        reasons.append("没有可用的 ResponseBrief，无法确认事实与承诺没有被改写")
        return False, reasons

    joined = _plan_text(text)
    if not joined.strip():
        reasons.append("没有可校验的文本，无法确认事实与承诺没有被改写")
        return False, reasons

    known = _known_blob(resolved, evidence=evidence)

    match = _COMMIT_RE.search(joined)
    if match and not _COMMIT_RE.search(known):
        reasons.append(f"变奏新增了 brief 里没有的承诺 / 约定：「{_text(match.group(0))}」")

    known_numbers = _numbers_in(known)
    for found in _DIGIT_RE.finditer(joined):
        token = found.group(0)
        if _norm_number(token) not in known_numbers:
            reasons.append(f"变奏出现了本轮事实细节里没有的数字：{token}")

    for found in _TIME_RE.finditer(joined):
        token = found.group(0)
        if token not in known:
            reasons.append(f"变奏出现了本轮事实细节里没有的时间：{token}")

    for pattern, kind, label in _DONE_CLAIMS:
        hit = pattern.search(joined)
        if hit and not _claim_has_evidence(kind, actions, completed, failed):
            reasons.append(
                f"变奏声称「{hit.group(0)}」，但这一轮没有对应的成功{label}动作（假完成态）"
            )

    insult = _INSULT_RE.search(joined)
    if insult:
        reasons.append(f"变奏出现了真实贬低 / 攻击性表达：「{insult.group(0)}」，玩笑不许变成攻击")

    if isinstance(text, Mapping):
        if resolved.actions:
            planned = _texts(_plan_first_value(text, _PLAN_ACTION_KEYS))
            extra = [name for name in planned if name not in resolved.actions]
            if extra:
                reasons.append("回复计划新增了 brief 里没有的动作：" + "、".join(extra))
            lost = [name for name in resolved.actions if name not in planned]
            if lost:
                reasons.append("回复计划少了 brief 里已定的动作：" + "、".join(lost))
        for item in _texts(_plan_first_value(text, _PLAN_DECLARED_KEYS)):
            if item not in resolved.fact_sources and item not in known:
                reasons.append(f"回复计划声明了 brief 里没有的事实 / 承诺：{item}")

    return (not reasons), reasons
