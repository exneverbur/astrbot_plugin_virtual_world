"""中文别名 → 引擎取值的**唯一**映射处（A 线收尾：合并散落的别名表）。

为什么要有这个文件
------------------

A 线要给动作身份加命名空间（``(source_namespace, action_id)``）。做这件事之前先把
"中文词 → 某个引擎取值"的映射收拢：它们以前散在各自的消费者里
（``core/json_actions.py`` 的口吻、``core/events.py`` 的贡献类型），改身份时容易看漏。

**但它们属于不同的域，所以这里不是把三张表揉成一张大表**——那是错的（``打气`` 和
``亲昵`` 压根不是一回事）。这里做的是：每一域一张表、表名带域名、只有一份定义，
消费者一律从这里取。

⚠️ 同形不同义，改身份时别误伤（审计 §11.6 的原话）
--------------------------------------------------

- ``hug`` 既是**口吻**取值（:data:`TONE_ALIASES` 里的目标），也是**动作 id**；
- ``refuse``（口吻）与 ``refusal``（贡献类型）长得像，是两个域；
- ``share``、``selfie`` 在预算名 / 工具名里还有别的意思。

**这些表里的值不是动作 id**，只是恰好同名的普通字符串，别拿它们去查动作表。
"""

from __future__ import annotations

from typing import Any

TONE_ALIASES: dict[str, str] = {
    "夸奖": "praise",
    "夸": "praise",
    "亲昵": "hug",
    "哄": "hug",
    "安慰": "comfort",
    "理解": "comfort",
    "共情": "comfort",
    "怼": "attack",
    "阴阳": "attack",
    "攻击": "attack",
    "拒绝": "refuse",
    "明确拒绝": "refuse",
    "叫停": "refuse",
    "喊停": "refuse",
    "制止": "refuse",
    "不要": "refuse",
    "停": "refuse",
    "普通": "normal",
}
"""口吻：她这句话是在夸、在哄、在怼，还是普通（值域见 ``core/json_actions.py:TONES``）。"""

CONTRIBUTION_ALIASES: dict[str, str] = {
    "question": "question",
    "ask": "question",
    "query": "question",
    "问题": "question",
    "advice": "advice",
    "suggestion": "advice",
    "tip": "advice",
    "建议": "advice",
    "new_fact": "new_fact",
    "fact": "new_fact",
    "info": "new_fact",
    "news": "new_fact",
    "情报": "new_fact",
    "新情况": "new_fact",
    "offer": "offer",
    "help": "offer",
    "帮忙": "offer",
    "cheer": "cheer",
    "support": "cheer",
    "打气": "cheer",
    "joke": "joke",
    "tease": "joke",
    "heckle": "joke",
    "起哄": "joke",
    "refusal": "refusal",
    "refuse": "refusal",
    "no": "refusal",
    "拒绝": "refusal",
    "irrelevant": "unrelated",
    "noise": "unrelated",
    "unrelated": "unrelated",
    "无关": "unrelated",
}
"""群友那句话属于哪一种（值域见 ``core/events.py:CONTRIBUTION_KINDS``）。"""


def _lookup(table: dict[str, str], raw: Any) -> str:
    """查表：别名（原样 / 小写）→ 否则看它是不是**已经就是**那个取值。

    第二步是必须的：模型有时直接给规范值（``praise``），老代码靠"查不到就原样
    往下传"实现这件事；表收到这里之后要显式补上，否则消费者会突然收到空串。
    """

    text = str(raw or "").strip()
    if not text:
        return ""
    hit = table.get(text) or table.get(text.lower())
    if hit:
        return hit
    lowered = text.lower()
    return lowered if lowered in table.values() else ""


def tone(raw: Any) -> str:
    """``夸奖``/``哄``/``praise`` → 口吻取值；认不出回空串（调用方自己决定兜底）。"""

    return _lookup(TONE_ALIASES, raw)


def contribution(raw: Any) -> str:
    """``打气``/``support`` → 贡献类型；认不出回空串（调用方自己决定兜底）。"""

    return _lookup(CONTRIBUTION_ALIASES, raw)
