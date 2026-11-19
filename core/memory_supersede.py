"""同主题覆盖（supersede）：同一件事有了新说法，旧的那条退位。

事故（用户实机）：她对主人说「我明天要去体检」，而事实是昨天就说过、今天下午已经
做完；库里「明天要去体检」和「体检完了」两条同时存在，她混着说。根因不是日期格式，
是**旧事实没有作废**——新说法落库了，旧的那条还写着"明天要去"，两条一起被召回。

这个模块只做**判定**，不碰数据库、不读配置、不打日志：

- :func:`similarity`：两句话是不是在说同一件事；
- :func:`conflict_reason`：新的是不是"同一件事的新结论"（计划 → 完成/取消，或者
  单位 / 住址 / 关系换了值）；
- :func:`judge`：两件事一起判，给出 ``verdict``（``supersede`` / ``relate`` / ``keep``）
  和一句 ``why``——写进 ``event_log`` 的就是这句，事后回看能知道当初为什么作废。

为什么比"同一件事"要先剥掉计划词/完成词：计划句和完成句是同一件事的两种说法，
「明天要去体检」和「今天下午体检完了」字面上几乎不重合，只有把状态词剥掉、只比
"这件事本身"（体检），才认得出它们是同一件事。**剥状态词只影响相似度**，判"是不是
同一件事的后续"仍然要求 :func:`conflict_reason` 为真，所以：
「明天要去体检」+「今天下午体检完了」→ 覆盖；「明天要去体检」+「明天要去上班」→ 不覆盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

SIMILARITY_THRESHOLD = 0.55
"""Jaccard ≥ 这个数才算"在说同一件事"。低于它一律不覆盖（宁可不作废，不可错作废）。"""

CANDIDATE_DAYS = 14
"""路 B 只看最近两周的旧记忆：更早的旧事就算撞了词，也多半是另一件事。"""

MAX_CANDIDATES = 20
"""路 B 一次最多看多少条候选（写入路径上的硬上限，别把热路径拖慢）。"""

_TIME_WORDS = (
    "今天", "明天", "昨天", "后天", "前天", "当天", "次日", "上午", "下午", "中午",
    "晚上", "早上", "凌晨", "半夜", "现在", "刚才", "刚刚", "上周", "下周", "这周",
    "上个星期", "下个星期", "上个月", "下个月", "今年", "去年", "明年", "几点",
    "一会儿", "等会儿", "等下", "这两天", "这几天", "那天", "这天", "当时", "后来",
    "最近", "以前", "之前", "从前", "那天早上", "那天晚上",
)

_FILLER_WORDS = (
    "嗯", "啊", "呀", "吧", "呢", "哦", "啦", "嘛", "呗", "哈", "唉", "哎",
    "其实", "就是", "那个", "这个", "然后", "所以", "反正", "大概", "可能",
    "应该", "觉得", "真的", "有点", "有些", "一点", "已经", "了", "的", "地", "得",
)

_PRONOUN_WORDS = (
    "我们", "你们", "他们", "她们", "它们", "咱们", "自己", "我", "你", "他", "她",
    "它", "俺",
)

_PLAN_WORDS = (
    "明天", "后天", "下周", "下个月", "打算", "准备", "要去", "将会", "计划", "想要",
    "要去看", "要去见", "约了", "约好", "到时候", "等我", "待会", "快要去",
)

_DONE_WORDS = (
    "完了", "去过", "已经", "结束了", "取消了", "做完了", "看完", "回来了", "搞定了",
    "办完了", "不去了", "没去成", "改期了", "结果", "最后", "还是去", "终于",
)

_CHANGE_WORDS = (
    "换到", "换去", "换了", "换成", "换了个", "调到", "转到", "搬到", "搬去", "搬进",
    "改为", "改成", "转去",
)
""""换了 / 调到 / 搬到"这类词本身不说明结论，但会把两句话的字面拉开（A 单位 → B 单位）。

比对相似度时剥掉它们，只留下"这件事本身"（上班 / 住在哪），换没换值交给
:func:`entity_values` 判。
"""

_PREFERENCE_WORDS = (
    "喜欢", "讨厌", "爱吃", "爱喝", "偏好", "口味", "最烦", "最爱", "看不惯", "受不了",
)

_PREFERENCE_OVERRIDE_WORDS = (
    "现在不喜欢了", "不喜欢了", "不再喜欢", "现在讨厌", "改成", "换口味", "现在爱",
    "现在喜欢",
)
"""喜好类默认不覆盖（喜欢猫和喜欢狗可以并存）；只有本人明确说"改了"才覆盖。"""

_RELATION_TERMS = (
    "男朋友", "女朋友", "未婚夫", "未婚妻", "男友", "女友", "对象", "老公", "老婆",
    "丈夫", "妻子", "前夫", "前妻", "室友", "同桌", "同事", "上司", "老板", "导师",
    "学生", "邻居", "哥哥", "姐姐", "弟弟", "妹妹",
)

# 实体槽位：单位 / 住址。抓的是"槽位上的值"，值不同 = 同一件事换了说法。
_ENTITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "单位",
        re.compile(
            r"(?:在|到|去|进|入职|换到|调到|转到|换去|转去)"
            r"([\u4e00-\u9fa5A-Za-z0-9]{2,10}?)"
            r"(?:单位|公司|上班|工作|打工|实习)"
        ),
    ),
    (
        "住址",
        re.compile(
            r"(?:住在|搬到|搬去|搬进|家在|落户在?)"
            r"([\u4e00-\u9fa5A-Za-z0-9]{2,12})"
        ),
    ),
)

_MASK = "××"
"""换值的槽位在比对前盖成这个（两个字符，不会把二元组切碎）。"""


def _normalize(text: str) -> str:
    """只留中英文数字（去空白、标点、表情）。"""

    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


_ORIGINAL_TAIL_RE = re.compile(r"（原话：[^（）]*）")
"""记忆写入时挂在句末的原话（``core.time_ref`` 加的）：比对主题时整段不算。"""

_ISO_DATE_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?:-\d{1,2}(?:-\d{1,2})?)?年?")
"""归一化写进去的绝对日期（``2026-10-28`` / ``2026-10`` / ``2026年``）：不算主题。"""


def _strip_words(text: str, words: Iterable[str]) -> str:
    """按长度从长到短逐个删（"男朋友"必须先于"男友"，否则会剩一个"男"）。"""

    value = text
    for word in sorted((str(item) for item in words), key=len, reverse=True):
        if word:
            value = value.replace(word, "")
    return value


def topic_text(text: str) -> str:
    """剥掉"这是什么时候说的 / 谁在说 / 说成什么状态"，只留这件事本身。

    写入时相对时间已经被换成绝对日期、并在句末挂了一段 ``（原话：…）``（见
    :mod:`core.time_ref`）。这两截都是"什么时候说的"，不是"说的是什么"——不剥掉
    会把同一件事的两条记忆的字面拉远（``体检`` 两边都有，日期却各不相同）。
    """

    value = _ORIGINAL_TAIL_RE.sub("", str(text or ""))
    value = _ISO_DATE_RE.sub("", value)
    value = _normalize(value)
    value = _strip_words(value, _TIME_WORDS)
    value = _strip_words(value, _FILLER_WORDS)
    value = _strip_words(value, _PRONOUN_WORDS)
    value = _strip_words(value, _PLAN_WORDS)
    value = _strip_words(value, _DONE_WORDS)
    value = _strip_words(value, _CHANGE_WORDS)
    return value


def _clean_slot(value: str) -> str:
    """槽位值的兜底清洗：正则会把"今天在 / 了海淀区"这种残渣抓进来，这里挡掉。

    只留像"店名 / 地名 / 称谓"的东西：去掉首尾的助词与虚词，剩下的必须 ≥2 字，
    且不能再裹着时间词、代词或 ``在/了/的``。
    """

    text = str(value or "").strip()
    # "换到药店 / 改成海淀区"里，动作词不是值的一部分：先剥掉，事件里才写得出
    # 「单位从便利店变成药店」而不是「变成换到药店」。
    for word in _CHANGE_WORDS:
        if text.startswith(word):
            text = text[len(word) :]
    for _ in range(4):
        before = text
        for word in ("了", "的", "在", "了在"):
            if text.startswith(word):
                text = text[len(word) :]
        if text == before:
            break
    if len(text) < 2:
        return ""
    if any(word in text for word in _TIME_WORDS + _PRONOUN_WORDS + _FILLER_WORDS):
        return ""
    if any(ch in text for ch in "在了的"):
        return ""
    return text


def relation_terms(text: str) -> set[str]:
    """句子里出现的关系称谓（判"关系变了"用）。"""

    value = _normalize(text)
    return {term for term in _RELATION_TERMS if term in value}


def preference_override(text: str) -> bool:
    """新句有没有明说"这件事我改了"（现在不喜欢了 / 改成 / 换口味）。"""

    value = _normalize(text)
    return any(word in value for word in _PREFERENCE_OVERRIDE_WORDS)


def entity_values(text: str) -> dict[str, str]:
    """句子里能定位的实体槽位值：``{"单位": "超市", "住址": "朝阳区", "关系": "男友"}``。"""

    value = _normalize(text)
    found: dict[str, str] = {}
    for slot, pattern in _ENTITY_PATTERNS:
        match = pattern.search(value)
        if match:
            cleaned = _clean_slot(match.group(1))
            if cleaned:
                found.setdefault(slot, cleaned)
    terms = relation_terms(text)
    if terms:
        found["关系"] = "/".join(sorted(terms))
    return found


def shingles(text: str) -> set[str]:
    """中文二元组 + 单字。单字是给短句兜底的：「体检」对「我体检」不该被长度差罚死。"""

    value = str(text or "")
    if not value:
        return set()
    items = {ch for ch in value}
    items.update(value[index : index + 2] for index in range(len(value) - 1))
    return items


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def similarity(old_text: str, new_text: str) -> float:
    """两句话"在说同一件事"的分数（0~1），≥ :data:`SIMILARITY_THRESHOLD` 才算同主题。

    先各自剥成 :func:`topic_text`；两边在**同一个槽位**上值不同（A 单位 → B 单位、
    旧住址 → 新住址、男友 → 老公）时，先把值盖成 ``××`` 再比：换的是值，说的还是
    同一件事，不盖住的话"换了哪一家"会把两句话拉到毫不相似。
    """

    left = topic_text(old_text)
    right = topic_text(new_text)
    left_slots = entity_values(old_text)
    right_slots = entity_values(new_text)
    for slot, value in left_slots.items():
        other = right_slots.get(slot)
        if not other or other == value:
            continue
        if slot == "关系":
            # 关系槽位存的是"男友/老公"，逐个称谓盖掉
            for term in relation_terms(old_text) - relation_terms(new_text):
                left = left.replace(term, _MASK)
            for term in relation_terms(new_text) - relation_terms(old_text):
                right = right.replace(term, _MASK)
        else:
            left = left.replace(_normalize(value), _MASK)
            right = right.replace(_normalize(other), _MASK)
    if len(left) < 2 or len(right) < 2:
        # 剥完只剩一个字：短到没法判断是不是同一件事，按"不像"处理（不覆盖）
        return 0.0
    return round(jaccard(shingles(left), shingles(right)), 4)


def same_topic(old_text: str, new_text: str, *, threshold: float | None = None) -> bool:
    limit = SIMILARITY_THRESHOLD if threshold is None else float(threshold)
    return similarity(old_text, new_text) >= limit


HISTORY_WORDS = (
    "以前",
    "之前",
    "从前",
    "当年",
    "那时候",
    "那时",
    "曾经",
    "过去",
    "上次",
    "上回",
    "小时候",
)

"""「她在问以前的说法」的触发词。

召回默认只用 ``status='active'``（不然"要去体检"和"体检完了"会一起被想起来）；
只有这句话在往回看的时候，调用方才把被覆盖的旧条目也带上，渲染时加「（旧说法）」。
"""


def wants_history(text: str) -> bool:
    """这句话是不是在问"以前"（调用方据此把 ``include_superseded`` 打开）。"""

    body = str(text or "")
    return any(word in body for word in HISTORY_WORDS)


def _core_has_content(core: str) -> bool:
    """公共片段里除了"喜欢 / 改成"这类词，还剩不剩真东西。

    "喜欢猫" 和 "现在不喜欢狗了，改成喜欢兔子" 的公共片段是「喜欢」——那只是评价动词，
    不能因此认定"同一件事"，否则改了对狗的看法会顺手作废对猫的喜好。
    """

    text = str(core or "")
    for word in _PREFERENCE_WORDS + _PREFERENCE_OVERRIDE_WORDS:
        text = text.replace(word, "")
    return len(text.strip()) >= 1


def longest_common(old_text: str, new_text: str) -> str:
    """两句（已经剥成 :func:`topic_text`）最长的公共片段；没有就返回空串。"""

    left = topic_text(old_text)
    right = topic_text(new_text)
    best = ""
    for start in range(len(left)):
        for end in range(len(left), start + len(best), -1):
            piece = left[start:end]
            if piece in right:
                best = piece
                break
    return best


def topic_match(old_text: str, new_text: str) -> tuple[bool, float, str]:
    """是不是在说同一件事；返回 ``(命中, 相似度, 为什么)``。

    主判据是 :func:`similarity` ≥ :data:`SIMILARITY_THRESHOLD`。另有一条**很窄的
    豁免**：计划 → 结果这一对（:func:`status_conflict` 为真）除了状态词几乎不可能
    有高相似度——「明天要去体检」和「今天下午体检完了，结果一切都好」的 Jaccard
    只有 0.14——所以这**一种**关系额外允许"剥掉时间/状态词后有 ≥2 字的公共片段"
    （这里就是「体检」）也算同主题。

    豁免只对"计划 → 结果"开口子：换单位 / 换住址 / 关系变化仍然走 Jaccard（它们的
    字面本来就高度重合），喜好类另有一道 :func:`preference_guard`，所以不会因为这条
    豁免误伤"喜欢猫 / 喜欢狗"。
    """

    score = similarity(old_text, new_text)
    if score >= SIMILARITY_THRESHOLD:
        return True, score, f"同主题（相似度 {score:.2f}）"
    hit, why = entity_conflict(old_text, new_text)
    if hit:
        # 同一个槽位换了值（A 单位 → B 单位、旧住址 → 新住址、男友 → 老公）：
        # 槽位本身就是这件事，"换成了哪儿"不改变"说的是同一件事"。
        return True, score, why
    if status_conflict(old_text, new_text):
        core = longest_common(old_text, new_text)
        if len(core) >= 2:
            return True, score, f"计划 → 结果，同一件事（「{core}」）"
    if preference_override(new_text):
        core = longest_common(old_text, new_text)
        if len(core) >= 2 and _core_has_content(core):
            return True, score, f"同一样东西改了看法（「{core}」）"
    return False, score, ""


def status_conflict(old_text: str, new_text: str) -> bool:
    """旧的是计划、新的说做完了/取消了（方向只认"计划 → 结果"）。

    反过来（旧的已完成、新的又是个计划）**不算冲突**：那是下一次的事，不是这件
    事的后续，旧的完成记录仍然是真的。
    """

    old = _normalize(old_text)
    new = _normalize(new_text)
    old_plan = any(word in old for word in _PLAN_WORDS)
    new_done = any(word in new for word in _DONE_WORDS)
    return bool(old_plan and new_done)


def preference_guard(old_text: str, new_text: str) -> bool:
    """喜好类不覆盖：两边都在说喜好，且新句没有明说"改了"，就都不动。"""

    old = _normalize(old_text)
    new = _normalize(new_text)
    if not (any(word in old for word in _PREFERENCE_WORDS) and any(
        word in new for word in _PREFERENCE_WORDS
    )):
        return False
    if preference_override(new_text) or preference_override(old_text):
        # 本人明确说"现在不喜欢了 / 改成…"：这不是并存的两条喜好，是改了
        return False
    return True


def entity_conflict(old_text: str, new_text: str) -> tuple[bool, str]:
    """同一个槽位换了值（单位 / 住址 / 关系）。返回 ``(是否冲突, 人话原因)``。"""

    left = entity_values(old_text)
    right = entity_values(new_text)
    for slot, value in left.items():
        other = right.get(slot)
        if other and other != value:
            label = {"单位": "单位", "住址": "住址", "关系": "关系"}.get(slot, slot)
            if slot == "关系":
                return True, f"关系从「{value}」变成「{other}」"
            return True, f"{label}从「{value}」变成「{other}」"
    return False, ""


def conflict_reason(old_text: str, new_text: str) -> tuple[bool, str]:
    """新句是不是"同一件事的新结论"。返回 ``(是否冲突, 人话原因)``。"""

    hit, why = entity_conflict(old_text, new_text)
    if hit:
        return True, why
    if status_conflict(old_text, new_text):
        return True, "旧的是计划，新说法是已经做完 / 取消 / 结束了"
    if preference_override(new_text) and preference_guard(old_text, new_text) is False:
        old = _normalize(old_text)
        if any(word in old for word in _PREFERENCE_WORDS):
            return True, "喜好改了（新说法明说现在不喜欢了 / 改成别的了）"
    return False, ""


@dataclass(frozen=True)
class Verdict:
    """一次判定的结论。``verdict`` 是 ``supersede`` / ``relate`` / ``keep``。"""

    verdict: str
    similar: float
    why: str

    @property
    def supersede(self) -> bool:
        return self.verdict == "supersede"

    @property
    def relate(self) -> bool:
        """同主题但没冲突：只记关联，**不覆盖**。"""

        return self.verdict == "relate"

    @property
    def score(self) -> float:
        """``similar`` 的别名（调用方按"相似度"念更顺口）。"""

        return self.similar


def judge(old_text: str, new_text: str) -> Verdict:
    """判定「新说法该不该让旧说法退位」。

    - ``supersede``：同主题 + 有冲突（计划→结果、换单位/住址/关系）→ 旧条作废；
    - ``relate``：同主题但没有冲突（只是补充细节）→ 只记一条关联，**不覆盖**；
    - ``keep``：不像同一件事 / 喜好类 / 判不了 → 什么都不做。
    """

    old = str(old_text or "").strip()
    new = str(new_text or "").strip()
    if not old or not new:
        return Verdict("keep", 0.0, "有一条是空的")
    if _normalize(old) == _normalize(new):
        return Verdict("keep", 1.0, "同一句话，交给原来的去重逻辑")
    matched, score, why_match = topic_match(old, new)
    if not matched:
        limit = SIMILARITY_THRESHOLD
        return Verdict("keep", score, f"不像同一件事（相似度 {score:.2f} < {limit:.2f}）")
    if preference_guard(old, new):
        return Verdict("keep", score, "喜好类不覆盖（喜欢猫和喜欢狗可以并存）")
    hit, why = conflict_reason(old, new)
    if not hit:
        return Verdict("relate", score, "同主题但没有新结论，只记关联、不覆盖")
    return Verdict("supersede", score, why or why_match)


def creates_cycle(old_id: object, by_id: object, links: Mapping[str, str]) -> bool:
    """``old`` 指向 ``by`` 之后会不会绕成环：顺着 ``by`` 已有的链走，撞回 ``old`` 就是环。"""

    target = str(old_id or "")
    cursor = str(by_id or "")
    seen: set[str] = set()
    while cursor and cursor not in seen:
        if cursor == target:
            return True
        seen.add(cursor)
        cursor = str(links.get(cursor) or "")
    return False
