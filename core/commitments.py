"""「等会儿」得变成一条真的待办（用户 m00001）。

上一批上线 ``agency`` 之后有个明摆着的缺口：协商成功时它会给一个
``deferred_at``（15/30/45/60 分钟），引擎只把这句话塞进了提示词，
**没有任何地方在到点之后回头找她**。于是"等会儿"成了一句空头承诺——
说与做不同源，正是用户反复强调不能糊弄的那类事。

这个模块只放**纯函数**：把一句待办压成能看懂的 ``task_text``、判断
"她这一轮算不算把这件事做了"。落库、提醒与关闭分别在 ``db`` / ``engine``，
这样判定可以直接单测，不用起引擎。
"""

from __future__ import annotations

import re
from typing import Any

COMMITMENT_TASK_CHARS = 120
"""``task_text`` 最多存几个字：再长就不是"一句待办"，是整段转述。"""

COMMITMENT_PROMISE_CHARS = 40
"""写进 ``state.intention`` 的字数上限（与 ``agency.AGENCY_INTENTION_CHARS`` 对齐）。"""

COMMITMENT_DONE_OVERLAP = 2
"""重叠到几个二元组才算"她去做了"：1 太松（"吃饭"会撞上"吃饭团"），2 刚够认人话。"""

COMMITMENT_DEFER_MARKS = ("等会儿", "待会儿", "稍后", "回头")
"""``ask_first`` 的 note 里出现这些词，就是在说"等会儿"（``negotiate`` 不用看 note）。"""

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_SQUASH = re.compile(r"[^\u4e00-\u9fffa-z0-9]+")


def clip_task_text(text: Any, limit: int = COMMITMENT_TASK_CHARS) -> str:
    """把一句待办压成一行（空白折叠 + 超长截断）；空串 = 没记下什么。"""

    body = " ".join(str(text or "").split())
    if not body:
        return ""
    if limit <= 0 or len(body) <= limit:
        return body
    return body[: max(1, limit - 1)].rstrip() + "…"


def _bigrams(text: Any) -> set[str]:
    """中文二元组集合：先按非汉字切开，再在每段里取相邻两字。

    只取汉字是故意的：标点、空格、英文都不参与重叠，免得"的""了"这种高频字
    凑出两个假命中。
    """

    grams: set[str] = set()
    for run in _CJK_RUN.findall(str(text or "")):
        for index in range(len(run) - 1):
            grams.add(run[index : index + 2])
    return grams


def _squash(text: Any) -> str:
    """只留汉字与字母数字，用来判"整句原样出现"。"""

    return _SQUASH.sub("", str(text or "").lower())


def keyword_overlap(task_text: Any, evidence: Any) -> int:
    """``task_text`` 的二元组里有多少个在 ``evidence`` 里也出现（纯函数）。"""

    task = _bigrams(task_text)
    if not task:
        return 0
    matched = len(task & _bigrams(evidence))
    if matched >= COMMITMENT_DONE_OVERLAP:
        return matched
    # 两三个字的短任务凑不出两个二元组：整句原样出现在她这一轮里也算做了
    body = _squash(task_text)
    if len(body) >= COMMITMENT_DONE_OVERLAP and body in _squash(evidence):
        return COMMITMENT_DONE_OVERLAP
    return matched


def commitment_done(task_text: Any, evidence: Any) -> bool:
    """她这一轮的动作 / 发言是否已经把那条待办做了。"""

    return keyword_overlap(task_text, evidence) >= COMMITMENT_DONE_OVERLAP
