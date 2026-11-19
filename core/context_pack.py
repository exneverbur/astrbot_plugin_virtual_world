"""上下文装配（P3-4）：**只带这一轮用得上的那几块**，而且总长有上限。

为什么要单独一层：知识、经验、动作索引、画像这些东西一旦多了，最省事的做法就是
"全塞进提示词"——然后每一轮都变贵、变慢、还更容易被无关内容带偏。这里做三件事：

1. **按优先级装**：`persona > rules > scene > capabilities > knowledge > extras`；
   排在后面的先被裁；
2. **按预算裁**：整包不超过 `max_chars`，裁掉的每一条都记进 ``dropped``（带原因），
   不许"少了一块却没人知道"；
3. **可解释**：``included`` / ``dropped`` / ``chars`` / ``approx_tokens`` 都给调试页看。

刻意不做的事：不做语义检索（那是 embeddings 的事）、不调模型、不读数据库——
调用方把"这一轮想要哪些块"准备好，这里只负责装与裁。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: 块名 → 优先级（数字小的先装，装不下就从最大的那个开始裁）
BLOCK_ORDER = (
    "persona",
    "rules",
    "format",
    "scene",
    "capabilities",
    "knowledge",
    "experiences",
    "extras",
)

BLOCK_LABELS = {
    "persona": "人设",
    "rules": "世界规则",
    "format": "输出格式",
    "scene": "当前场景",
    "capabilities": "动作索引",
    "knowledge": "知识",
    "experiences": "经验",
    "extras": "补充提醒",
}

#: 默认总预算（字符）。按"每字约 0.5 token"估算，约 3000 token。
DEFAULT_MAX_CHARS = 6000


@dataclass(frozen=True)
class Block:
    name: str
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "label": BLOCK_LABELS.get(self.name, self.name),
                "chars": len(self.text)}


@dataclass(frozen=True)
class Pack:
    """装好的结果。``text`` 直接能进提示词；``dropped`` 每条都带原因。"""

    text: str = ""
    included: tuple[str, ...] = ()
    dropped: tuple[tuple[str, str], ...] = ()
    chars: int = 0
    approx_tokens: int = 0
    max_chars: int = DEFAULT_MAX_CHARS
    truncated_blocks: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "included": list(self.included),
            "dropped": [{"block": name, "reason": reason} for name, reason in self.dropped],
            "chars": int(self.chars),
            "approx_tokens": int(self.approx_tokens),
            "max_chars": int(self.max_chars),
            "truncated": list(self.truncated_blocks),
        }


def build(
    blocks: Iterable[Any],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_block_chars: int = 40,
) -> Pack:
    """按优先级装块，超预算就裁（**从低优先级的尾部开始**）。

    ``min_block_chars``：一块至少要能留这么多字；连这个都留不下就整块不要
    （留半句话比没有更糟）。
    """

    try:
        budget = int(max_chars)
    except (TypeError, ValueError):
        budget = DEFAULT_MAX_CHARS
    if budget <= 0:
        budget = DEFAULT_MAX_CHARS

    prepared: list[Block] = []
    dropped: list[tuple[str, str]] = []
    for item in list(blocks or ()):
        if isinstance(item, Block):
            block = item
        elif isinstance(item, Mapping):
            block = Block(str(item.get("name") or ""), str(item.get("text") or ""))
        else:
            continue
        if not block.name:
            dropped.append(("", "no_name"))
            continue
        if not block.text.strip():
            dropped.append((block.name, "empty"))
            continue
        prepared.append(Block(block.name, block.text.strip()))

    order = {name: index for index, name in enumerate(BLOCK_ORDER)}
    prepared.sort(key=lambda item: order.get(item.name, len(BLOCK_ORDER)))

    kept: list[Block] = []
    used = 0
    truncated: list[str] = []
    for index, block in enumerate(prepared):
        cost = len(block.text) + (2 if kept else 0)
        if used + cost <= budget:
            kept.append(block)
            used += cost
            continue
        rest = budget - used - (2 if kept else 0)
        if rest >= min_block_chars:
            # 能留一部分：在句读处截断，并标记"被截过"
            cut = _clip_at_sentence(block.text, rest)
            if cut:
                kept.append(Block(block.name, cut))
                used += len(cut) + (2 if len(kept) > 1 else 0)
                truncated.append(block.name)
                continue
        dropped.append((block.name, "over_budget"))
        # 后面的块也不再装：保持"优先级从高到低"的可解释性
        for later in prepared[index + 1 :]:
            dropped.append((later.name, "over_budget"))

    text = "\n\n".join(block.text for block in kept)
    return Pack(
        text=text,
        included=tuple(block.name for block in kept),
        dropped=tuple(dropped),
        chars=len(text),
        approx_tokens=max(1, len(text) // 2) if text else 0,
        max_chars=budget,
        truncated_blocks=tuple(truncated),
    )


def _clip_at_sentence(text: str, limit: int) -> str:
    """在限额内尽量切在句读处（切不到就硬切，并在末尾标记）。"""

    body = str(text or "").strip()
    if limit <= 0 or not body:
        return ""
    if len(body) <= limit:
        return body
    window = body[:limit]
    for mark in ("。", "！", "？", "\n", "；", ". "):
        index = window.rfind(mark)
        if index >= max(0, limit // 2):
            return window[: index + len(mark)].rstrip()
    return window[: max(0, limit - 1)] + "…"


def select_knowledge(
    rows: Iterable[Mapping[str, Any]],
    *,
    scope: str = "",
    persona_id: str = "",
    limit: int = 5,
    now: float = 0.0,
) -> list[Any]:
    """从候选里挑**这一轮能用**的经验（作用域对、状态可用、依赖没变、未过期）。

    只做过滤与排序（页面上"可用"的排前面），不做语义匹配——那是下一步的事。
    """

    from . import knowledge

    picked: list[Any] = []
    for row in rows or ():
        if not knowledge.scope_allows(row, scope=scope, persona_id=persona_id):
            continue
        if not knowledge.usable(row, now=now):
            continue
        item = knowledge.from_row(row)
        if item is None:
            continue
        picked.append(item)
    picked.sort(key=lambda item: (-float(item.confidence), -float(item.updated_at)))
    return picked[: max(0, int(limit))]


def knowledge_block(items: Iterable[Any]) -> str:
    """把选中的经验拼成提示词里的一段（空就返回空串，整块不出现）。"""

    from . import knowledge

    lines = [knowledge.prompt_line(item) for item in items or ()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    return "【做过类似的事，可以借鉴】\n" + "\n".join(lines)
