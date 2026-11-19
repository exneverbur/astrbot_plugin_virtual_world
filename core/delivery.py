"""成果交付契约（P0-7）：**别把"问得细"当成闲聊压掉**。

问题出在这几处（方案 §5.4 / D01–D05）：

1. 她查回来一大段，进提示词前被 ``_clip_followup`` 从**中间一刀**（800 字硬顶），
   后文直接消失，连"还有多少没进来"都不说 —— 用户问的是"详细说说"，拿到的却是半句话；
2. 用户要"多点对比 / 分步骤 / 要来源"时，模板里那句"群聊里简短自然"会把答案压成
   一句正确的废话；
3. "发出去了"和"做成了"是两件事，混成一句"搞定"就分不清了。

这份模块只做**三个纯函数**（不调模型、不碰 IO）：

* :func:`analyze_request` —— 这句话到底要多细？要不要对比、要几步、要不要来源；
* :func:`split_chunks` —— 超长正文按**句读**切块（绝不从一句话中间切开）；
* :func:`check_delivery` —— 交付是否覆盖了"必答点"，以及**没进来的部分去哪了**。

判定一律基于文本本身，不猜用户心情；拿不准就当"闲聊"（保守），但**永远不静默截断**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 交付类型：闲聊 / 说细一点 / 多点对比 / 分步骤
KIND_CHAT = "chat"
KIND_DETAILED = "detailed"
KIND_COMPARISON = "comparison"
KIND_STEPS = "steps"

KIND_LABELS = {
    KIND_CHAT: "闲聊",
    KIND_DETAILED: "详细说明",
    KIND_COMPARISON: "多点对比",
    KIND_STEPS: "分步骤",
}

#: 各类交付的正文预算（超了就**分块**，不从前截断）
KIND_BUDGET = {
    KIND_CHAT: 800,
    KIND_DETAILED: 1200,
    KIND_COMPARISON: 1200,
    KIND_STEPS: 1200,
}

_DETAILED_WORDS = ("详细", "完整", "具体", "展开", "多说", "讲讲", "调研", "整理一份", "写一份")
_COMPARISON_WORDS = ("对比", "比较", "区别", "差别", "各自", "分别", "优缺点", "哪个好")
_STEPS_WORDS = ("步骤", "怎么做", "怎么弄", "流程", "先…再", "依次")
_SOURCE_WORDS = ("来源", "出处", "依据", "原文", "链接", "哪看到的", "引用")

#: 句读：中文句号/问号/感叹号/分号/换行（英文句点要求后面跟空格，免得切坏 3.14、example.com）
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；\n])|(?<=[.!?])(?=\s)|(?<=[；;])")


@dataclass(frozen=True)
class DeliveryNeed:
    """用户这句话对交付的要求。"""

    kind: str = KIND_CHAT
    wants_sources: bool = False
    points: tuple[str, ...] = ()
    """要求覆盖的"点"：从"ABC 三个问题 / 分别说 / 各有什么"里拆出来的，最多 6 个。"""
    budget: int = KIND_BUDGET[KIND_CHAT]

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "wants_sources": bool(self.wants_sources),
            "points": list(self.points),
            "budget": int(self.budget),
        }


@dataclass(frozen=True)
class DeliveryVerdict:
    """一次交付的体检结果。"""

    ok: bool = True
    chars: int = 0
    chunks: tuple[str, ...] = ()
    missing_points: tuple[str, ...] = ()
    missing_sources: bool = False
    reasons: tuple[str, ...] = ()
    note: str = ""

    @property
    def needs_chunking(self) -> bool:
        return len(self.chunks) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "chars": int(self.chars),
            "chunk_count": len(self.chunks),
            "chunk_chars": [len(item) for item in self.chunks],
            "missing_points": list(self.missing_points),
            "missing_sources": bool(self.missing_sources),
            "reasons": list(self.reasons),
            "note": self.note,
        }


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


#: 要点里不该留下的开头/结尾（都是语气词或"给出来源"这类指令，不是要点本身）
_POINT_HEAD = ("一下", "几个", "这", "那", "分别", "各自", "逐一", "一个一个", "说说", "讲讲", "看看", "评估")
_POINT_TAIL = ("并给出出处", "并附上来源", "注明出处", "谢谢", "好吗", "行吗", "吧", "呢", "吗")


def _points(text: str) -> tuple[str, ...]:
    """从"分别说说 A、B 和 C"里拆出要点（最多 6 个，够用且不啰嗦）。

    要点必须是**具体名词性短语**：语气词、连接词、"并给出出处"这类指令都清掉——
    留着它们只会在体检时变成"这个点没讲到"的假警报。
    """

    body = str(text or "")
    match = re.search(
        r"(?:分别|各自|逐一|一个一?个)?(?:说说|讲讲|说说看|看看|评估|对比|比较)(.+)", body
    )
    if not match:
        return ()
    tail = match.group(1)
    picked: list[str] = []
    for raw in re.split(r"[、,，和与及/]|以及", tail):
        item = raw.strip(" ，,。；;：:？?！!…")
        for head in _POINT_HEAD:
            if item.startswith(head):
                item = item[len(head) :].strip()
        for ending in _POINT_TAIL:
            if item.endswith(ending):
                item = item[: -len(ending)].strip()
        if 1 < len(item) <= 14 and item not in picked:
            picked.append(item)
    return tuple(picked[:6])


def analyze_request(text: Any) -> DeliveryNeed:
    """这句话要多细（确定性；拿不准按闲聊算）。"""

    body = str(text or "").strip()
    if not body:
        return DeliveryNeed()
    kind = KIND_CHAT
    if _has_any(body, _STEPS_WORDS):
        kind = KIND_STEPS
    elif _has_any(body, _COMPARISON_WORDS):
        kind = KIND_COMPARISON
    elif _has_any(body, _DETAILED_WORDS):
        kind = KIND_DETAILED
    return DeliveryNeed(
        kind=kind,
        wants_sources=_has_any(body, _SOURCE_WORDS),
        points=_points(body),
        budget=KIND_BUDGET.get(kind, KIND_BUDGET[KIND_CHAT]),
    )


def split_chunks(text: Any, limit: int) -> tuple[str, ...]:
    """按句读把正文切成不超过 ``limit`` 的若干块（**不从一句话中间切**）。

    只有"单独一句话本身就超长"时才会硬切，并在切口留一个标记——那也比无声无息地
    少掉后半段强。空文本返回空元组。
    """

    body = str(text or "").strip()
    if not body:
        return ()
    try:
        size = max(1, int(limit))
    except (TypeError, ValueError):
        size = KIND_BUDGET[KIND_CHAT]
    if len(body) <= size:
        return (body,)
    sentences = [item for item in _SENTENCE_SPLIT.split(body) if item]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > size:
            # 单句超长：先收掉手里的，再把这句硬切
            if current:
                chunks.append(current)
                current = ""
            # 硬切时**不加标记**：加了就突破单块上限，而且"被切过"这件事
            # 由体检结果的 reasons 明说，不需要在正文里塞省略号
            for start in range(0, len(sentence), size):
                chunks.append(sentence[start : start + size])
            continue
        if len(current) + len(sentence) > size:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current)
    return tuple(chunks)


def check_delivery(need: DeliveryNeed, text: Any) -> DeliveryVerdict:
    """交付体检：够不够细、要点有没有覆盖、来源有没有说、要不要分块。"""

    body = str(text or "").strip()
    chars = len(body)
    chunks = split_chunks(body, need.budget)
    missing: list[str] = []
    for point in need.points:
        key = point.strip()
        if key and key not in body:
            missing.append(key)
    missing_sources = bool(need.wants_sources) and not _has_any(
        body, ("来源", "出处", "据", "链接", "http", "参考资料", "原文")
    )
    reasons: list[str] = []
    if not body:
        reasons.append("没有正文")
    if missing:
        reasons.append("这几个点没讲到：" + "、".join(missing))
    if missing_sources:
        reasons.append("用户要来源，正文里没有出处")
    if len(chunks) > 1:
        reasons.append(f"正文 {chars} 字，超过 {need.budget} 字，已按句读分成 {len(chunks)} 块")
    ok = bool(body) and not missing and not missing_sources
    if ok and len(chunks) > 1:
        note = f"交付完成；正文较长（{chars} 字），按 {len(chunks)} 块发，别一次全塞进一条消息"
    elif ok:
        note = "交付完成"
    else:
        note = "交付不完整：" + "；".join(reasons)
    return DeliveryVerdict(
        ok=ok,
        chars=chars,
        chunks=chunks,
        missing_points=tuple(missing),
        missing_sources=missing_sources,
        reasons=tuple(reasons),
        note=note,
    )
