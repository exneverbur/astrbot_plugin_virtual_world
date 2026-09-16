"""LLM 输出的 JSON 动作解析与校验（设计文档 5.6 节）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 模型的思考段：有的模型即使关了思考开关也会吐出来（有时还漏掉开标签）。
# 这些内容既不能进群，也不能挡着后面的 JSON 解析。
_THINK_NAME = r"(?:thinking|think|reasoning|analysis|reflection|思考|反思)"
_THINK_BLOCK = re.compile(
    rf"<\s*{_THINK_NAME}\s*>.*?</\s*{_THINK_NAME}\s*>", re.DOTALL | re.IGNORECASE
)
_THINK_FENCE = re.compile(
    rf"```\s*{_THINK_NAME}\s*.*?```", re.DOTALL | re.IGNORECASE
)
_THINK_CLOSE = re.compile(rf"</\s*{_THINK_NAME}\s*>", re.IGNORECASE)
_THINK_OPEN = re.compile(rf"<\s*{_THINK_NAME}\s*>", re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """把模型可能吐出来的思考段剥掉，只留它真正要对外的内容。

    处理三种真实出现过的情况：

    1. 完整块：``<thinking>…</thinking>`` / ```` ```thinking … ``` ````；
    2. 只有闭合标签（漏了开标签，或 Provider 把开标签吃掉了）：
       丢掉最后一个闭合标签之前的所有内容；
    3. 只有开标签：从开标签后第一个 ``{`` 开始留。

    剥完之后的文本才拿去做 JSON 解析、才可能被当成"她直接说了句话"。
    """

    body = str(text or "")
    if not body:
        return ""
    body = _THINK_FENCE.sub(" ", body)
    body = _THINK_BLOCK.sub(" ", body)
    closes = list(_THINK_CLOSE.finditer(body))
    if closes:
        return body[closes[-1].end() :].strip()
    opener = _THINK_OPEN.search(body)
    if opener:
        tail = body[opener.end() :]
        index = tail.find("{")
        return tail[index:].strip() if index >= 0 else ""
    return body.strip()


def _balanced_slices(text: str) -> list[str]:
    """切出文本里**最外层**的、花括号配对的片段（忽略字符串里的括号）。

    只收最外层：``{"actions":[{"type":"say"}]}`` 会得到一个整体，
    不会把里面那个动作对象也当成候选（否则会解析出半截 JSON）。
    """

    slices: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, current in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                in_string = False
            continue
        if current == '"':
            in_string = True
        elif current == "{":
            if depth == 0:
                start = index
            depth += 1
        elif current == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    slices.append(text[start : index + 1])
                    start = -1
    return slices


@dataclass
class PlannedAction:
    """一条待执行的动作。"""

    type: str
    interject: bool = False
    """是否是「插话」——即接着群里正在聊的话题主动说一句。"""

    messages: list[str] = field(default_factory=list)
    target: str = ""
    target_node: str = ""
    content: str = ""
    intent: str = ""
    """工具型动作的「想干什么」。实际参数由辅助模型按工具定义补全。"""

    params: dict[str, Any] = field(default_factory=dict)
    duration: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.messages:
            data["messages"] = list(self.messages)
        if self.target:
            data["target"] = self.target
        if self.target_node:
            data["target_node"] = self.target_node
        if self.content:
            data["content"] = self.content
        if self.intent:
            data["intent"] = self.intent
        if self.params:
            data["params"] = dict(self.params)
        if self.duration:
            data["duration"] = self.duration
        return data


@dataclass
class ParseResult:
    actions: list[PlannedAction]
    reasoning: dict[str, str] = field(default_factory=dict)
    """推理草稿：模型在动手之前对「环境/状态/心情/在和谁说话/打算怎么办」的自我确认。

    它只用于调试和（可选的）联动，不会发到群里、不计入动作数量、也不会写进记忆。
    """

    warnings: list[str] = field(default_factory=list)
    fallback_used: bool = False
    raw_text: str = ""
    memory: str = ""
    """这次对话值得记住的一句话（以她的视角，由模型顺手总结）。留空表示模型没给。"""

    cancel: str = ""
    """模型是否要求取消她手头的安排：``now`` = 立刻停手并放弃剩下的，``queue`` = 只清掉还没开始的。"""

    chat_note: str = ""
    """一句话交代「刚才这段在聊什么」，下一轮当背景用，避免重复回应老话题。"""

    valence_delta: float = 0.0
    """这一轮的心情变化（模型给的 -1~1，正=变好、负=变差）。缺失当 0。"""

    tail: str = ""
    """JSON 之后残留下来的短尾巴（例如别的插件要求模型追加的 `[好感度 持平]`）。

    它不会跟着她的话发到群里，只在「回复钩子」那一步带上，让靠标记工作的插件还能读到。
    """


REASONING_KEYS = ("env", "state", "mood", "who", "intent")

# 只有这两档：「立刻停手」和「别按原计划走（手上这件做完）」
CANCEL_MODES = ("now", "queue")
_CANCEL_ALIASES = {
    "now": "now",
    "stop": "now",
    "all": "now",
    "cancel": "now",
    "立刻": "now",
    "现在": "now",
    "停下": "now",
    "queue": "queue",
    "later": "queue",
    "pending": "queue",
    "rest": "queue",
    "排队": "queue",
    "后面的": "queue",
}


def parse_cancel(payload: Any) -> str:
    """解析模型给出的 cancel 字段（写法五花八门，认不出来就当没写）。"""

    if payload is None:
        return ""
    if isinstance(payload, dict):
        text = str(payload.get("mode") or payload.get("cancel") or "").strip()
    else:
        text = str(payload).strip()
    return _CANCEL_ALIASES.get(text.lower(), _CANCEL_ALIASES.get(text, ""))


def parse_reasoning(payload: Any) -> dict[str, str]:
    """把模型给出的 reasoning 归一成固定字段（未知键丢弃，值截断到 120 字）。"""

    if payload is None:
        return {}
    if isinstance(payload, str):
        text = payload.strip()
        return {"intent": text[:120]} if text else {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, str] = {}
    for key in REASONING_KEYS:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            result[key] = text[:120]
    # 模型可能用了别名字段，兜底收进来
    if not result:
        for key, value in payload.items():
            text = str(value).strip()
            if text:
                result[str(key)[:20]] = text[:120]
    return result


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里挖出合法 JSON 对象（会先剥掉思考段）。

    候选顺序：代码块 → 整段文本 → 每个 ``{`` 起的花括号配对片段（从后往前试）。
    最后这一步是为"模型在思考里贴了一段示例 JSON、正文才是真 JSON"这种情况准备的。
    """

    if not text:
        return None
    body = strip_reasoning(text)
    if not body:
        return None
    candidates: list[str] = []
    match = _JSON_BLOCK.search(body)
    if match:
        candidates.append(match.group(1))
    candidates.append(body)
    # 一路退：从后往前试每个 { 起头的配对片段，先命中"最后那个完整的 JSON"
    candidates.extend(reversed(_balanced_slices(body)))
    for candidate in candidates:
        stripped = candidate.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"actions": data}
    return None


def parse_action_payload(
    text: str,
    *,
    available_actions: set[str],
    valid_nodes: set[str] | None = None,
    valid_targets: set[str] | None = None,
    max_actions: int = 3,
    max_messages: int = 3,
) -> ParseResult:
    """把模型输出解析成动作列表。

    校验规则：
    - 解析失败 -> 降级为单条 say（原文），fallback_used=True；
    - type 不在当前可用动作里 -> 丢弃并记录警告；
    - target_node 不存在 -> 丢弃该动作；
    - messages 超限 -> 截断；
    - 工具型动作缺 params -> 丢弃。
    """

    warnings: list[str] = []
    raw = text or ""
    # 思考段先剥掉：既不能让"她其实在思考"的内容被当成发言发出去，
    # 也不能让它挡着后面的 JSON。
    cleaned_text = strip_reasoning(raw)
    payload = extract_json_object(raw)
    if payload is None:
        return _fallback_result(cleaned_text, raw)

    items = payload.get("actions")
    if not isinstance(items, list):
        cleaned = cleaned_text.strip().strip("`").strip()
        return ParseResult(
            actions=[PlannedAction(type="say", messages=[cleaned])] if cleaned else [],
            warnings=["模型输出缺少 actions 字段，已降级为直接发言"],
            fallback_used=True,
            raw_text=raw,
        )

    actions: list[PlannedAction] = []
    for item in items:
        if not isinstance(item, dict):
            warnings.append("动作不是对象，已丢弃")
            continue
        action_type = str(item.get("type", "")).strip()
        if not action_type:
            warnings.append("动作缺少 type，已丢弃")
            continue
        if available_actions and action_type not in available_actions:
            warnings.append(f"动作 {action_type} 在当前场景不可用，已丢弃")
            continue

        action = PlannedAction(type=action_type, raw=item)
        messages = item.get("messages")
        if isinstance(messages, str):
            messages = [messages]
        if isinstance(messages, list):
            cleaned = [str(m).strip() for m in messages if str(m).strip()]
            action.messages = cleaned[:max_messages]
            if len(cleaned) > max_messages:
                warnings.append(f"{action_type} 的消息超过 {max_messages} 条，已截断")
        elif action_type == "say":
            warnings.append("say 动作没有 messages，已丢弃")
            continue

        target = str(item.get("target", "") or "").strip()
        target_node = str(item.get("target_node", "") or "").strip()
        if target and valid_targets is not None and target not in valid_targets:
            # target 可能是节点 id 或用户 id，两者都不匹配才算非法
            if not (valid_nodes and target in valid_nodes):
                warnings.append(f"动作 {action_type} 的目标 {target} 不认识，已忽略目标")
                target = ""
        if target_node and valid_nodes is not None and target_node not in valid_nodes:
            warnings.append(f"动作 {action_type} 的目标节点 {target_node} 不存在，已丢弃")
            continue
        action.target = target
        action.target_node = target_node
        action.content = str(item.get("content", "") or "").strip()
        action.intent = str(
            item.get("intent", item.get("goal", item.get("purpose", ""))) or ""
        ).strip()[:200]

        params = item.get("params")
        if isinstance(params, dict):
            action.params = params
        # 工具型动作只给 intent、不给 params 是正常的：参数由辅助模型在调用前按工具 schema 补全。
        # 真正补不出来时，引擎会写一条带原因的 skip 事件——这里不需要再猜。

        try:
            action.duration = max(0, int(item.get("duration", 0) or 0))
        except (TypeError, ValueError):
            action.duration = 0

        actions.append(action)

    if len(actions) > max_actions:
        warnings.append(f"动作数量超过 {max_actions} 条，已截断")
        actions = actions[:max_actions]

    # 「先想后说」：think 一律排在其它动作之前（解析器不依赖模型给出的顺序）
    actions.sort(key=lambda item: 0 if item.type == "think" else 1)

    reasoning = parse_reasoning(payload.get("reasoning"))
    return ParseResult(
        actions=actions,
        reasoning=reasoning,
        warnings=warnings,
        raw_text=raw,
        memory=parse_memory(payload.get("memory")),
        cancel=parse_cancel(payload.get("cancel")),
        chat_note=_clean_note(payload.get("chat_note")),
        valence_delta=_parse_valence_delta(payload.get("valence_delta")),
        tail=_json_tail(cleaned_text),
    )


def _parse_valence_delta(value: Any) -> float:
    """模型给的心情变化：-1 ~ 1，缺失或写坏都当 0（不影响这一轮）。"""

    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(-1.0, min(1.0, number))


def _json_tail(text: str, limit: int = 60) -> str:
    """JSON 之后剩下的短尾巴（给「靠标记工作的插件」用，不会发到群里）。"""

    body = str(text or "").strip()
    if not body:
        return ""
    end = body.rfind("}")
    if end < 0 or end >= len(body) - 1:
        return ""
    tail = body[end + 1 :].strip().strip("`").strip()
    if not tail or len(tail) > limit:
        return ""
    # 只认"看起来是标记"的短尾巴，避免把模型多写的一句废话带进钩子
    return tail if tail[0] in "[【(" else ""


def _clean_note(payload: Any) -> str:
    """群聊背景句：只取一句话，太长就截断。"""

    if payload is None:
        return ""
    text = " ".join(str(payload).split())
    return text[:80]


# 兜底发言的长度上限：超过这个长度、或者还残留 JSON 结构的，就不当"她说了句话"处理。
# （模型偶尔会把整段思考当成回复，把它发到群里比什么都不说糟得多。）
_FALLBACK_MAX_CHARS = 160


def _fallback_result(cleaned: str, raw: str) -> ParseResult:
    """模型没给出 JSON 时的兜底。

    - 剩下来的是一句正常的话（短、不含 JSON 残留）→ 当成她说的一句，照发；
    - 剩下来的还是长文/思考/半个 JSON → 什么都不发，只在日志里留个警告。
    """

    body = str(cleaned or "").strip().strip("`").strip()
    looks_like_reasoning = (
        len(body) > _FALLBACK_MAX_CHARS
        or "{" in body
        or "}" in body
        or '"actions"' in body
        or "```" in body
    )
    if not body or looks_like_reasoning:
        return ParseResult(
            actions=[],
            warnings=["模型输出既不是 JSON 也不是一句话，已忽略（没有发到群里）"],
            fallback_used=True,
            raw_text=raw,
        )
    return ParseResult(
        actions=[PlannedAction(type="say", messages=[body])],
        warnings=["模型输出不是合法 JSON，已降级为直接发言"],
        fallback_used=True,
        raw_text=raw,
    )
    """群聊背景句：只取一句话，太长就截断。"""

    if payload is None:
        return ""
    text = " ".join(str(payload).split())
    return text[:80]


def parse_memory(payload: Any) -> str:
    """这次对话要记下来的一句话（模型可能给字符串，也可能给 {"summary": "..."}）。"""

    if payload is None:
        return ""
    if isinstance(payload, dict):
        for key in ("summary", "content", "text", "note"):
            value = payload.get(key)
            if value:
                return str(value).strip()[:120]
        return ""
    return str(payload).strip()[:120]


def parse_plan_payload(
    text: str,
    *,
    available_actions: set[str],
    valid_nodes: set[str] | None = None,
    max_steps: int = 8,
) -> tuple[dict[str, Any] | None, list[str]]:
    """解析决策器要求的「计划」输出。"""

    warnings: list[str] = []
    payload = extract_json_object(text or "")
    if payload is None:
        return None, ["模型输出不是合法 JSON，计划已忽略"]
    steps = payload.get("plan")
    if not isinstance(steps, list) or not steps:
        return None, ["模型输出缺少 plan 字段，计划已忽略"]

    normalized: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        action_type = str(step.get("action", step.get("type", ""))).strip()
        if not action_type or (available_actions and action_type not in available_actions):
            warnings.append(f"计划里的动作 {action_type or '?'} 不可用，已跳过")
            continue
        target_node = str(step.get("target_node", "") or "").strip()
        if target_node and valid_nodes is not None and target_node not in valid_nodes:
            warnings.append(f"计划里的目标节点 {target_node} 不存在，已跳过")
            continue
        normalized.append(
            {
                "action": action_type,
                "target_node": target_node,
                "target": str(step.get("target", "") or ""),
                "duration": _to_int(step.get("duration", 0)),
                "content": str(step.get("content", "") or ""),
                "intent": str(
                    step.get("intent", step.get("goal", step.get("purpose", ""))) or ""
                ).strip()[:200],
                "messages": step.get("messages") if isinstance(step.get("messages"), list) else [],
                "params": step.get("params") if isinstance(step.get("params"), dict) else {},
                "status": "pending",
            }
        )
        if len(normalized) >= max_steps:
            warnings.append(f"计划步数超过 {max_steps}，已截断")
            break
    if not normalized:
        return None, warnings + ["计划为空"]
    try:
        valid_for = int(payload.get("valid_until", 1800) or 1800)
    except (TypeError, ValueError):
        valid_for = 1800
    plan = {
        "steps": normalized,
        "current_step": 0,
        "valid_for": max(60, valid_for),
        "reason": str(payload.get("reason", "") or ""),
        "source": "llm",
    }
    return plan, warnings


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default
