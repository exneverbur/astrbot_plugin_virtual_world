"""T12 Step 1：她可以有自己的一步，但绝不拒绝真求助（执行层自主选择）。

设计稿：``VW-T12-真正的预期违背-设计稿.md`` 的「九·B」与「八节 Step 1」。

这个模块**只有纯函数**：不读库、不认状态对象、不写任何东西。随机一律用
``random.Random(f"vw-agency|{rng_seed}")`` 造出来，同一个 seed 一定得到同一份结果
（用户可以复现"她这一轮为什么拧了一下"）。写库、改动作列表、记 ``event_log``
都在 ``core/engine.py``（见 ``VirtualWorldEngine._agency_gate``）。

三条口径（缺一条就会退化成"她学会了装死"）：
1. **求助与急事永远照做**——``help`` / ``urgent`` 在闸 1 就返回 ``comply``，
   主人的身份不豁免任何一道闸；
2. **做不到不许包装成不想做**——本轮有失败 / warnings 时强制 ``comply``；
3. **她这一步必须顶在一个具体的东西上**——自己想做的事、累了、正忙着，
   三者都没有就老老实实照做，不许空转。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Sequence

# ===================== 常量（测试会逐条断言） =====================

AGENCY_MODES = ("comply", "negotiate", "ask_first", "partial")
"""四种执行方式：照做 / 协商等会儿 / 先反问 / 只做一部分。"""

AGENCY_REASONS = (
    "help_request",
    "no_request",
    "warnings",
    "rate_limited",
    "no_trigger",
    "stranger",
    "own_plan",
    "low_energy",
    "already_busy",
    "splittable",
    "has_action",
    "ask_unspoken",
)
"""机器可读的决定原因（``event_log`` 与测试都用它，不许写中文句子进来）。

``has_action`` / ``ask_unspoken`` 是 :func:`refine_for_actions` 细化 ``ask_first`` 时用的：
前者"她真有能办事的动作"，后者"她压根没问出口"。
"""

AGENCY_HARD_KINDS = ("help", "urgent")
"""求求助与急事：永远照做，任何其他条件都不许覆盖它。"""

AGENCY_EMPTY_KINDS = ("", "none")
"""他没说要什么（或者说了"没有"）：不玩花样，照做。"""

AGENCY_TASK_KIND = "task"
"""只有"让她去做一件事"才可能被协商 / 反问 / 只做一部分。"""

AGENCY_KEEP_TYPES = ("say", "think")
"""无论怎么裁，说话和想事情都留着——她总得能开口解释自己。"""

AGENCY_WINDOW_SECONDS = 7 * 86400.0
"""频率窗口：**7 天**。"""

AGENCY_MAX_NON_COMPLY = 2
"""频率上限：7 天内最多 2 次不照做（第 3 次必须照做）。"""

AGENCY_LOW_ENERGY = 0.3
"""精力低于它就够格"拧一下"（0~1，状态里的 ``energy``）。"""

AGENCY_NOTE_MAX_CHARS = 60
"""注入提示词的那一句最多 60 字（太长会盖过人设）。"""

AGENCY_HISTORY_MAX = 20
"""``WorldState.agency_history`` 只留最近 20 条。"""

AGENCY_INTENTION_TTL = 30 * 60.0
"""一条「她自己的打算」活 30 分钟，过期就清（免得昨天的打算今天还在挡事）。"""

AGENCY_INTENTION_CHARS = 40
"""打算写进状态时截到 40 字。"""

AGENCY_REPEAT_NOTE_DAYS = 7.0
"""上次不照做还在这个天数以内 → 换一套"又来一次"的措辞，别当复读机。"""

AGENCY_FAMILIAR_AFFINITY = 0.35
"""好感到了这个数就算熟人（陌生人只许协商，不许反问 / 只做一半）。"""

AGENCY_DEFER_MINUTES = (15, 30, 45, 60)
"""协商时给的「等会儿」有这几种（分钟）。"""

AGENCY_SPLIT_MARKS = ("再", "然后", "顺便", "以及", "还有", "和")
"""请求描述里出现这些词就认为"这件事能拆成两半"（``splittable`` 的启发式）。"""

AGENCY_SEED_PREFIX = "vw-agency"
"""随机种子前缀：``f"vw-agency|{rng_seed}"``。"""

AGENCY_ASK_MARKS = ("？", "?")
"""她说的话里出现这些才算"真的问出口"（:func:`refine_for_actions` 的判据）。"""


@dataclass(frozen=True)
class AgencyDecision:
    """这一轮她怎么执行他的请求。"""

    mode: str
    """``comply`` / ``negotiate`` / ``ask_first`` / ``partial``（见 :data:`AGENCY_MODES`）。"""

    reason: str
    """机器可读的原因（见 :data:`AGENCY_REASONS`）。"""

    note: str = ""
    """要注入提示词的那一句中文（≤ ``AGENCY_NOTE_MAX_CHARS`` 字）；``comply`` 时是空串。"""

    deferred_at: float | None = None
    """「等会儿」是多久以后：**相对现在（now）的秒数**，不是绝对时间戳。

    只有 ``negotiate`` 会带；调用方要落成绝对时间就用 ``now + deferred_at``
    （纯函数拿不到 now，又必须可复现，所以这里存相对量）。
    """


# 每一档的措辞池：全都在 60 字以内，反问那档一律以问号结尾。
_AGENCY_NOTES: dict[str, tuple[str, ...]] = {
    "negotiate": (
        "等我把手头这点事收个尾，一会儿就给你办。",
        "这会儿正忙着自己那件事，晚点再说，行不行。",
        "先记着，等我缓过来就办——别催我。",
        "现在真不行，等我忙完这一段。",
    ),
    "ask_first": (
        "等等，你是要我先办这件，还是先记下来？",
        "先问一句：这件事急吗，还是等下再说？",
        "你说的这个，是现在就办，还是我先记本子上？",
    ),
    "partial": (
        "先办一件，剩下的等我缓过来再说。",
        "手上只来及做一样，先挑一件给你办。",
    ),
}

_AGENCY_NOTES_REPEAT: dict[str, tuple[str, ...]] = {
    "negotiate": (
        "又得等会儿了——我手上那件还没完。",
        "还是那句：等我这阵忙完，别催。",
    ),
    "ask_first": (
        "又是这种时候……你是想我现在就办，还是再等等？",
    ),
    "partial": (
        "跟上次一样，先做一件，剩下的搁后头。",
    ),
}


def clip_text(text: Any, limit: int) -> str:
    """把任意输入压成一句不超过 ``limit`` 字的短句（超了留"…"）。"""

    body = " ".join(str(text or "").split())
    if limit <= 0:
        return ""
    if len(body) <= limit:
        return body
    return body[: max(0, limit - 1)] + "…"


def normalize_kind(request_kind: Any) -> str:
    """请求类型归一：去空白、小写；认不出的原样返回（由闸 2 当"没说要什么"处理）。"""

    return str(request_kind or "").strip().lower()


def window_count(recent_modes: Sequence[float] | None, now: float | None = None) -> int:
    """频率窗口里已经发生了几次"没照做"。

    ``recent_modes`` 是**发生时间戳**（不是 mode 字符串）。给了 ``now`` 就自己按
    :data:`AGENCY_WINDOW_SECONDS` 筛一遍；没给就信调用方已经筛过（引擎里是前者）。
    """

    rows = [float(item) for item in (recent_modes or []) if _is_number(item)]
    if now is None:
        return len(rows)
    return len([item for item in rows if 0 <= float(now) - item <= AGENCY_WINDOW_SECONDS])


def trigger_reason(*, energy: float, busy: bool, own_intention: str) -> str:
    """她有"自己的一步"的理由；三条都不成立就返回空串（必须照做）。"""

    if str(own_intention or "").strip():
        return "own_plan"
    try:
        low = float(energy) < AGENCY_LOW_ENERGY
    except (TypeError, ValueError):
        low = False
    if low:
        return "low_energy"
    if busy:
        return "already_busy"
    return ""


def splittable_request(request_expect: Any) -> bool:
    """这句话描述的请求看起来能不能拆（"帮我把 A 弄了，再顺便 B"）。"""

    text = str(request_expect or "")
    if not text:
        return False
    return any(mark in text for mark in AGENCY_SPLIT_MARKS)


def agency_keep_indexes(
    action_types: Sequence[str], mode: str
) -> tuple[list[int], list[int]]:
    """按决定算「留哪些 / 剔哪些」的下标（纯函数，引擎照着切列表就行）。

    - ``comply``：全留；
    - ``negotiate`` / ``ask_first``：只留 ``say`` / ``think``；
    - ``partial``：留 ``say`` / ``think`` + **原顺序第一个**别的动作。
    """

    types = [str(item or "") for item in action_types]
    every = list(range(len(types)))
    if mode == "comply" or mode not in AGENCY_MODES:
        return every, []
    kept = [index for index, name in enumerate(types) if name in AGENCY_KEEP_TYPES]
    if mode == "partial":
        for index, name in enumerate(types):
            if name not in AGENCY_KEEP_TYPES:
                kept.append(index)
                break
    kept_set = set(kept)
    dropped = [index for index in every if index not in kept_set]
    return sorted(kept_set), dropped


def refine_for_actions(
    *,
    mode: str,
    action_types: Sequence[str] | None = None,
    say_text: str = "",
) -> tuple[str, str]:
    """把"算出来的 ``ask_first``"落回现实：她要么真问了，要么就只做一部分。

    线上那两条 ``ask_first``（``reason=own_plan``）暴露的是同一个问题：动作被没收了，
    她却只说了一句推脱、一个问号都没有。所以这里**只细化 ``ask_first``**，判定顺序
    （``help`` / ``urgent`` / 空 kind / ``had_failures`` / 频率 / ``no_trigger``）全在
    :func:`decide_agency` 里更早返回，这里碰不到它们。

    - 她说的话里**没有问号** → ``comply`` / ``ask_unspoken``：她根本没问，就不许先把
      动作没收掉（反问必须是她说出口的那句话，不是提示词里的一句话）；
    - 真问了，但这一轮有 ``say`` / ``think`` 之外的动作 → ``partial`` / ``has_action``：
      一边反问一边把能办的事全丢掉没有道理，留一件给她；
    - 其余（只剩说话 / 想事情，而且真问了）保持 ``ask_first``。

    返回 ``(mode, reason)``；原样返回时 ``reason`` 是空串（调用方保留原来的原因）。
    """

    if mode != "ask_first":
        return mode, ""
    spoken = str(say_text or "")
    if not any(mark in spoken for mark in AGENCY_ASK_MARKS):
        return "comply", "ask_unspoken"
    types = [str(item or "") for item in (action_types or ())]
    if any(name not in AGENCY_KEEP_TYPES for name in types):
        return "partial", "has_action"
    return mode, ""


def decide_agency(
    *,
    request_kind: Any = "",
    is_owner: bool = False,
    is_familiar: bool = True,
    energy: float = 1.0,
    busy: bool = False,
    own_intention: str = "",
    recent_modes: Sequence[float] | None = None,
    days_since_last: float = 999.0,
    rng_seed: Any = "",
    splittable: bool = False,
    had_failures: bool = False,
) -> AgencyDecision:
    """这一轮她照做、协商、先反问，还是只做一部分。

    判定顺序（**不可换**，换一条就会漏掉一种"她装死"的路径）：

    1. ``request_kind in {help, urgent}`` → ``comply`` / ``help_request``（永远照做）；
    2. ``request_kind in {"", none}`` → ``comply`` / ``no_request``（没说要什么）；
    3. ``had_failures``（本轮本来就有失败 / warnings）→ ``comply`` / ``warnings``：
       **她做不到的事，绝不许被包装成她不想做**；
    4. 频率：窗口里已经有 ``AGENCY_MAX_NON_COMPLY`` 次 → ``comply`` / ``rate_limited``；
    5. 触发：自己想做的事 / 精力 < 0.3 / 正忙，一条都不成立 → ``comply`` / ``no_trigger``；
    6. 选档：``task`` + 有打算 → ``ask_first``；``task`` + 可拆 → ``partial``；否则 ``negotiate``；
    7. 陌生人闸：``not is_familiar`` 时只许 ``negotiate``（可以协商，但不许拒绝也不许只做一半）；
    8. 措辞：同一 seed 同一句；最近 7 天内已经拧过一次就换"又来一次"的说法。

    ``is_owner`` **不豁免任何一道闸**（上面第 1~7 条对他一样严格）。
    """

    kind = normalize_kind(request_kind)
    intention = clip_text(own_intention, AGENCY_INTENTION_CHARS)

    # 闸 1：求助 / 急事。这一条永远在最前面，别的条件都盖不住它。
    if kind in AGENCY_HARD_KINDS:
        return AgencyDecision("comply", "help_request")
    # 闸 2：他没说要什么（含模型写了 none）。不玩花样，照做。
    if kind in AGENCY_EMPTY_KINDS:
        return AgencyDecision("comply", "no_request")
    # 主人不豁免任何一道闸：显式留一行，免得以后有人以为这里漏了判断。
    if is_owner:
        pass
    # 闸 3：做不到 / 出错的那一轮必须照做。她"没做成"和"不想做"是两件事。
    if had_failures:
        return AgencyDecision("comply", "warnings")
    # 闸 4：频率。7 天内已经拧过 AGENCY_MAX_NON_COMPLY 次，这一轮必须照做。
    if window_count(recent_modes) >= AGENCY_MAX_NON_COMPLY:
        return AgencyDecision("comply", "rate_limited")
    # 闸 5：她凭什么不照做——得有一个具体的、顶在她面前的东西。
    reason = trigger_reason(energy=energy, busy=busy, own_intention=intention)
    if not reason:
        return AgencyDecision("comply", "no_trigger")
    # 闸 6：选档。反问只给"让她做事"且她手上真有自己的打算；只做一半要求能拆。
    if kind == AGENCY_TASK_KIND and intention:
        mode = "ask_first"
    elif kind == AGENCY_TASK_KIND and bool(splittable):
        mode, reason = "partial", "splittable"
    else:
        mode = "negotiate"
    # 闸 7：陌生人只协商（不拒绝、也只问不反问）。
    if not is_familiar and mode != "negotiate":
        mode, reason = "negotiate", "stranger"

    rng = random.Random(f"{AGENCY_SEED_PREFIX}|{rng_seed}")
    note = pick_note(rng, mode, days_since_last=days_since_last)
    deferred: float | None = None
    if mode == "negotiate":
        deferred = float(rng.choice(AGENCY_DEFER_MINUTES) * 60)
    return AgencyDecision(mode=mode, reason=reason, note=note, deferred_at=deferred)


def pick_note(
    rng: random.Random, mode: str, *, days_since_last: float = 999.0
) -> str:
    """按档位挑一句措辞（同一 seed 同一句；最近拧过就换"又来一次"那套）。"""

    try:
        repeat = 0 <= float(days_since_last) < AGENCY_REPEAT_NOTE_DAYS
    except (TypeError, ValueError):
        repeat = False
    pool = (_AGENCY_NOTES_REPEAT if repeat else _AGENCY_NOTES).get(mode) or ()
    if not pool:
        return ""
    note = clip_text(rng.choice(pool), AGENCY_NOTE_MAX_CHARS)
    if mode == "ask_first" and not note.endswith(("？", "?")):
        # 反问必须真的是问句（少一个字就变成"通知"，那就不是商量了）
        note = clip_text(note, AGENCY_NOTE_MAX_CHARS - 1) + "？"
    return note


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
