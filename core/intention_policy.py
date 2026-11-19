"""T12 Step 2 的意图判定：一条打算"算不算被推动"以及"该不该退休"。

设计稿：``VW-T12-真正的预期违背-设计稿.md``（意图的一生）。

这个模块**只有纯函数**：不读库、不认 ``WorldState``、不写任何东西、不抛异常。
``engine._agency_intention_progress`` / ``engine._sweep_intention`` /
``engine._note_self_intention`` 只负责把结果落进状态。

为什么要有这个模块（live 现场）：``_agency_intention_progress`` 原来只要她手上有
**任何** ``current_action`` / ``current_plan`` 就返回 ``(True, "开始动手了")``——
于是 ``rounds`` 立刻变成 1，90 秒的年龄下限当场失效，一条"她压根没动过"的打算
活到第 10 拍就被判 ``no_progress`` 放弃；``_refresh_intention`` 再从同一条
``own_topic`` 立一条新的，约 30 分钟一对 ``proposed → abandoned`` 的源头就在这里。

口径：
1. **在动**（``current_action`` / ``current_plan`` 存在）仍然算"她没闲着"，``idle_ticks``
   归零、状态 ``kept``——读一本书要二十拍，不能按拍数把她正做着的打算杀掉；
2. 但**只有与她打算对得上**的那一下才算推进（``rounds+1``）：打算正文与动作
   ``type/desc``、计划步骤文本各自分词，有交集算对得上；``kind`` 与动作类别一致也算。
3. 对不上的动作为 ``(True, "")``：在动、但不推进。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

RETIRED_SECONDS = 86400.0
"""放弃之后这条文字退休多久：24 小时之内不许再立（同一句话反复立了又放是空转）。"""

RETIRED_MAX = 50
"""``state.intention_retired`` 最多留几条：超了按 ``until`` 裁掉最旧的（快过期的先走）。"""

INTENTION_KINDS = ("plan", "promise")
"""她自己说要去做的事只粗分两类，沿用现有 ``kind`` 取值（提示词按这个选措辞）。"""

_PROMISE_MARKS = (
    "答应",
    "说好",
    "承诺",
    "保证",
    "一定会",
    "回头",
    "带给他",
    "带给她",
    "寄给",
    "约好",
    "欠着",
)
"""像"答应过别人的"那类措辞：只是挑措辞用，判错了也只是提示词里那句话换个说法。"""

KIND_ACTIONS: dict[str, frozenset[str]] = {
    "plan": frozenset(
        {
            "clean",
            "cook",
            "walk",
            "exercise",
            "practice",
            "build",
            "draw",
            "paint",
            "garden",
            "water",
            "shop",
            "organize",
            "repair",
        }
    ),
    "promise": frozenset(
        {
            "promise",
            "tell",
            "send",
            "give",
            "bring",
            "meet",
            "reply",
            "deliver",
            "wait",
            "visit",
        }
    ),
}
"""``kind`` ↔ 动作类别：两个集合**不相交**，只在明确对得上时算数（否则 A 那道闸就废了）。

故意**不收** ``read`` / ``write`` / ``study`` / ``work`` 这类"泛动作"：她任何一条 plan
都可能配着"看看书、写点东西"发生，一旦算对上，``rounds`` 又会立刻 +1，90 秒下限
重新失效——那正是这次要修的空转（"在动但对不上"必须留得住 ``rounds=0``）。
真正的对应关系（"读完那本书" ↔ 动作 desc "读那本书"）靠词交集那一支判。
"""

_SPLIT = re.compile(r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff]+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

_STOP_CJK = frozenset(
    {
        "一个",
        "一件",
        "一下",
        "一点",
        "这件",
        "那件",
        "这次",
        "那次",
        "这样",
        "那样",
        "这个",
        "那个",
        "这些",
        "那些",
        "我们",
        "你们",
        "他们",
        "她们",
        "咱们",
        "现在",
        "已经",
        "刚才",
        "今天",
        "明天",
        "昨天",
        "时候",
        "然后",
        "之后",
        "之前",
        "因为",
        "所以",
        "但是",
        "如果",
        "可能",
        "应该",
        "需要",
        "觉得",
        "以为",
        "知道",
        "还是",
        "就是",
        "只是",
        "有点",
        "什么",
        "怎么",
        "为什",
        "打算",
        "准备",
        "开始",
        "接着",
        "而已",
        "自己",
        "事情",
        "东西",
        "上面",
        "下面",
        "里面",
        "外面",
        "起来",
        "出来",
        "过来",
        "下来",
    }
)
"""中文里太泛的二元组：留着会让"任何事情"都跟"任何动作"对得上。"""

_STOP_LATIN = frozenset(
    {"the", "and", "for", "with", "that", "this", "you", "your", "will", "have", "from", "into"}
)
"""``type`` 里常见的高频英文词同样不算线索。"""


def tokens(text: Any) -> tuple[str, ...]:
    """把一段话拆成可比较的词：中文二元组 + 长度 ≥ 2 的拉丁词，去停用词/纯数字。

    纯函数、绝不抛：坏输入（``None`` / 数字 / 怪对象）一律当空串。
    """

    try:
        body = str(text or "").lower()
    except Exception:  # noqa: BLE001 —— 取字符串都能炸的话，就当没线索
        return ()
    out: list[str] = []
    for chunk in _SPLIT.split(body):
        if not chunk:
            continue
        runs: list[list[Any]] = []
        for ch in chunk:
            cjk = bool(_CJK.match(ch))
            if runs and runs[-1][0] == cjk:
                runs[-1][1] += ch
            else:
                runs.append([cjk, ch])
        for cjk, run in runs:
            if cjk:
                if len(run) < 2:
                    continue  # 单字（把/的/了）太泛
                for index in range(len(run) - 1):
                    gram = run[index : index + 2]
                    if gram in _STOP_CJK or gram.isdigit():
                        continue
                    out.append(gram)
            else:
                if len(run) >= 2 and run not in _STOP_LATIN and not run.isdigit():
                    out.append(run)
    seen: set[str] = set()
    result: list[str] = []
    for token in out:
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
    return tuple(result)


def kind_matches(kind: Any, action_type: Any) -> bool:
    """``kind`` 与动作类别一致吗（``plan`` ↔ 做事类，``promise`` ↔ 兑现类）。

    两个集合不相交：只有**明确**属于这一类才算，不然任何动作都能"对得上"。
    """

    try:
        types = KIND_ACTIONS.get(str(kind or "").strip().lower())
        if not types:
            return False
        return str(action_type or "").strip().lower() in types
    except Exception:  # noqa: BLE001
        return False


def matches_intention(
    text: Any,
    action_sig: Any = "",
    plan_step: Any = "",
    *,
    kind: Any = "",
    action_type: Any = "",
) -> bool:
    """这条打算跟"她手上正在做的事"对得上吗（纯函数，异常一律 ``False``）。

    ``action_sig`` / ``plan_step`` 是现成的动作指纹（``type:desc`` / 计划步骤文本）：
    分词后有交集就算对得上；``kind`` 与 ``action_type`` 分属同一类别也算。
    """

    try:
        want = set(tokens(text))
        if not want:
            return False
        got = set(tokens(action_sig)) | set(tokens(plan_step))
        if want & got:
            return True
        return kind_matches(kind, action_type)
    except Exception:  # noqa: BLE001 —— 判定失败只能算"对不上"，绝不往上抛
        return False


def classify_kind(text: Any) -> str:
    """粗分 ``kind``：像"答应过别人的"归 ``promise``，其余归 ``plan``。"""

    try:
        body = str(text or "")
    except Exception:  # noqa: BLE001
        return "plan"
    for mark in _PROMISE_MARKS:
        if mark in body:
            return "promise"
    return "plan"


def clean_intent(text: Any, limit: int = 40) -> str:
    """把模型给的 ``intent`` 规整成一句话：空白压平、截断到 ``limit`` 字。

    只认字符串：``2.0`` / ``True`` / ``{"text": …}`` 这类结构化垃圾一律当没写——否则
    ``str()`` 会把它变成一条"打算"（``"2.0"`` 就是一条合法正文，退休表都拦不住）。
    """

    try:
        if not isinstance(text, str):
            return ""
        body = " ".join(str(text or "").split())
        if not body:
            return ""
        return body[: max(1, int(limit))]
    except Exception:  # noqa: BLE001
        return ""


def text_key(text: Any) -> str:
    """一条打算文字的稳定键（退休表用它：同一句话不管谁立的都算同一件）。"""

    try:
        body = " ".join(str(text or "").split()).lower()
        if not body:
            return ""
        return hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return ""


def retired_table(raw: Any) -> dict[str, float]:
    """把 ``state.intention_retired`` 洗成 ``{text_hash: until}``（坏条目一律丢掉）。"""

    table: dict[str, float] = {}
    if not isinstance(raw, Mapping):
        return table
    try:
        items = list(raw.items())
    except Exception:  # noqa: BLE001
        return table
    for key, value in items:
        try:
            name = str(key or "").strip()
            until = float(value)
        except (TypeError, ValueError):
            continue
        except Exception:  # noqa: BLE001
            continue
        if not name or until <= 0.0:
            continue
        table[name] = until
    return table


def prune_retired(
    raw: Any, *, now: float = 0.0, limit: float = RETIRED_MAX
) -> dict[str, float]:
    """过期条目清掉；超过 ``limit`` 条时按 ``until`` 裁掉最旧的（小的先走）。"""

    table = retired_table(raw)
    try:
        stamp = float(now or 0.0)
    except (TypeError, ValueError):
        stamp = 0.0
    if stamp > 0.0:
        table = {key: value for key, value in table.items() if value > stamp}
    try:
        keep = max(1, int(limit))
    except (TypeError, ValueError):
        keep = int(RETIRED_MAX)
    if len(table) > keep:
        rows = sorted(table.items(), key=lambda item: item[1], reverse=True)[:keep]
        table = dict(rows)
    return table


def retired_until(raw: Any, text: Any, *, now: float = 0.0) -> float:
    """这条文字的解禁时刻；没退休 / 已到期一律返回 ``0.0``。"""

    key = text_key(text)
    if not key:
        return 0.0
    until = float(retired_table(raw).get(key) or 0.0)
    try:
        stamp = float(now or 0.0)
    except (TypeError, ValueError):
        stamp = 0.0
    if stamp > 0.0 and until <= stamp:
        return 0.0
    return until


def retire(
    raw: Any,
    text: Any,
    *,
    now: float,
    seconds: float = RETIRED_SECONDS,
    limit: float = RETIRED_MAX,
) -> dict[str, float]:
    """把这条文字写进退休表（``now + seconds``），顺手过期 + 裁剪；返回**新的**表。"""

    try:
        stamp = float(now)
    except (TypeError, ValueError):
        stamp = 0.0
    table = prune_retired(raw, now=stamp, limit=limit)
    key = text_key(text)
    if not key or stamp <= 0.0:
        return table
    try:
        span = float(seconds)
    except (TypeError, ValueError):
        span = RETIRED_SECONDS
    table[key] = stamp + span
    return prune_retired(table, now=stamp, limit=limit)


def should_retire(reason: Any) -> bool:
    """只有 ``no_progress``（连拍没推进）才退休：到期/被丢弃不算她的错。"""

    return str(reason or "").strip() == "no_progress"


def is_empty_talk(text: Any) -> bool:
    """模型是不是在敷衍（"没事""算了"这种）：是的话别立打算。"""

    body = clean_intent(text)
    if not body:
        return True
    stripped = body.strip("。.!！?？…~～,，、 ")
    if not stripped:
        return True
    if stripped.lower() in _EMPTY_WORDS:
        return True
    return len(stripped) < 2


_EMPTY_WORDS = frozenset(
    {
        "null",
        "none",
        "n/a",
        "na",
        "无",
        "没有",
        "没事",
        "没什么",
        "算了",
        "不用",
        "不必",
        "不想",
        "空",
        "空着",
        "先这样",
        "-",
    }
)
"""这些不算"她要做的事"：立了也只是空转一条打算。"""
