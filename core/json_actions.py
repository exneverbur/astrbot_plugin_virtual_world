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

MAX_SAY_LINES_HARD = 6
"""一次发言的防刷屏硬顶：比它多才真的截断，其余只提醒（提示词负责收敛）。"""


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
    queries: list[str] = field(default_factory=list)
    """检索型动作这一轮要查的几条查询词（可以只给一条；留空由引擎按意图兜）。"""

    search_depth: str = ""
    """她自己想要的检索深度（``quick`` / ``standard`` / ``deep``）。留空＝按动作配置。

    她只能往浅里调（配置是上限），免得每次都开深挖。
    """

    read_pages: int = -1
    """她想要读几篇正文；-1 表示没写，按动作配置来。"""

    raw: dict[str, Any] = field(default_factory=dict)

    send_to: str = ""
    """这句话说给哪个会话听（会话组里的群 / 私聊）。空 = 跟着这一轮的落点走。"""

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
        if self.queries:
            data["queries"] = list(self.queries)
        if self.search_depth:
            data["search_depth"] = self.search_depth
        if self.read_pages >= 0:
            data["read_pages"] = self.read_pages
        if self.send_to:
            data["send_to"] = self.send_to
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

    plan_mode: str = ""
    """模型给这一轮新安排的定位：``queue``（排队）/ ``interrupt``（插队）/ ``replace``（顶掉没做的）。

    它是给人看的选择结果；实际生效的仍是 :attr:`cancel`。
    """

    head_clean: bool = True
    """输出是不是**从 `{` 开始**的：推理草稿真的写在最前面时才算干净。"""

    leading_text: str = ""
    """`{` 之前多出来的内容（有它说明模型先说了别的，草稿被挤到后面）。"""

    chat_note: str = ""
    """一句话交代「刚才这段在聊什么」，下一轮当背景用，避免重复回应老话题。"""

    open_topic: str = ""
    """他有一件**还没聊完**的事（他去体检、他在纠结换工作…）。空 = 这轮没有。

    和 ``chat_note`` 的区别：chat_note 是"刚才在聊什么"（会过期、只防重复），
    这条是"这话题没说完、之后可以接着问"。
    """

    heart_knot: str = ""
    """她**心里搁着的一件事**（"他上次那句话让我到现在还别扭"）。空 = 这轮没添新的。

    和 ``open_topic`` 的区别：open_topic 是"别人的事、等着接着问"，
    这条是**她自己的事**——不推进、不解决，只是挂着，会自己淡掉。
    """

    grudge: str = ""
    """她在跟前的这个人身上**记一笔账**（"他答应的事又没做"）。空 = 这轮没记。

    和 ``heart_knot`` 的区别：心事是"她自己的心情"，记仇是**冲这个人**的——
    会让她对他冷一档，而且只在跟他说话时拿出来。
    """

    forgive: bool = False
    """他刚道歉 / 补上了 / 解释清楚了 → 之前记着的那笔账可以算了。"""

    own_topic: str = ""
    """**她自己**打算做、或者答应过别人的事（"答应给他看照片"）。空 = 这轮没添新的。"""

    own_topic_done: bool = False
    """上面记着的那件事**刚做完了** → 划掉它。"""

    tone: str = ""
    """这一轮**对方对她是什么口吻**：``praise``（夸她）/ ``hug``（哄她、亲昵动作）/
    ``attack``（怼她、阴阳、冒犯）/ ``normal``（普通）／空 = 没判。

    以前这一步是拿关键词表猜的（"你可真行"会被当成夸奖），现在由主模型判——
    它反正要看这一整句话，多输出一个词不花钱。
    """

    valence_delta: float = 0.0
    """这一轮的心情变化（模型给的 -1~1，正=变好、负=变差）。缺失当 0。"""

    affinity_delta: float = 0.0
    """这一轮对**当前说话人**的好感变化（-1~1，正=更亲近）。缺失当 0。

    真正落库前还会被代码再削一次（每轮上限 + 每人每天总额度，见 ``ProfileConfig``），
    所以模型写过头也不会把好感刷满。
    """

    tail: str = ""
    """JSON 之后残留下来的短尾巴（例如别的插件要求模型追加的 `[好感度 持平]`）。

    它不会跟着她的话发到群里，只在「回复钩子」那一步带上，让靠标记工作的插件还能读到。
    """


REASONING_KEYS = ("env", "state", "mood", "who", "inner", "intent")
"""推理草稿的字段顺序，也是提示词里要求的顺序。

``inner`` 是"心里想的"：第一人称的一两句心理活动（只给自己看，不进群）。
"""

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


# 新安排相对「手上这件事」的位置：排队 / 插队 / 顶掉没做的那几步
PLAN_MODES = ("queue", "interrupt", "replace")
_PLAN_MODE_ALIASES = {
    "queue": "queue",
    "later": "queue",
    "after": "queue",
    "排队": "queue",
    "排在后面": "queue",
    "interrupt": "interrupt",
    "now": "interrupt",
    "插队": "interrupt",
    "打断": "interrupt",
    "立刻": "interrupt",
    "replace": "replace",
    "clear": "replace",
    "顶掉": "replace",
    "替换": "replace",
}
# plan_mode 最终落到 cancel 上：interrupt 就是「立刻停手」，replace 就是「手上这件做完就停」
_PLAN_MODE_TO_CANCEL = {"interrupt": "now", "replace": "queue"}


def parse_plan_mode(payload: Any) -> str:
    """解析模型给出的 ``plan_mode``（认不出来当没写）。"""

    if payload is None:
        return ""
    if isinstance(payload, dict):
        text = str(payload.get("mode") or payload.get("plan_mode") or "").strip()
    else:
        text = str(payload).strip()
    return _PLAN_MODE_ALIASES.get(text.lower(), _PLAN_MODE_ALIASES.get(text, ""))


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
    # 推理草稿有没有真的写在最开头：`{` 之前冒出来的东西都算"先说了别的"
    leading_text = ""
    head_clean = True
    brace = str(raw).find("{")
    if brace > 0:
        leading_text = " ".join(str(raw)[:brace].split())[:80]
        head_clean = not leading_text
    elif brace < 0:
        head_clean = False
    # 思考段先剥掉：既不能让"她其实在思考"的内容被当成发言发出去，
    # 也不能让它挡着后面的 JSON。
    cleaned_text = strip_reasoning(raw)
    payload = extract_json_object(raw)
    if payload is None:
        return _fallback_result(cleaned_text, raw)

    items = payload.get("actions")
    if isinstance(items, dict):
        # 只写了一个动作对象、没包成数组：当成一条动作处理
        items = [items]
    if not isinstance(items, list):
        # actions 写成了字符串（模型偶尔这么干），或者干脆没写：
        # 只从 JSON 里抠出「她真要说的话」——**绝不能**把 reasoning 或整个 JSON 发到群里。
        spoken = speakable_messages(payload)
        reasoning = parse_reasoning(payload.get("reasoning"))
        if spoken:
            return ParseResult(
                actions=[PlannedAction(type="say", messages=spoken)],
                reasoning=reasoning,
                warnings=["模型把 actions 写成了一段文本，已按发言处理"],
                fallback_used=True,
                raw_text=raw,
            )
        return ParseResult(
            actions=[],
            reasoning=reasoning,
            warnings=["模型输出缺少 actions 字段，已忽略（没有发到群里）"],
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
            hints = "、".join(sorted(available_actions)[:8])
            warnings.append(
                f"动作 {action_type} 在当前场景不可用，已丢弃"
                + (f"（这里能用：{hints}）" if hints else "")
            )
            continue

        action = PlannedAction(type=action_type, raw=item)
        messages = item.get("messages")
        if isinstance(messages, str):
            messages = [messages]
        if isinstance(messages, list):
            cleaned = [str(m).strip() for m in messages if str(m).strip()]
            # 条数只提醒、不砍（她多说了两句就让它说）：真正的兜底是防刷屏的硬顶。
            action.messages = cleaned[:MAX_SAY_LINES_HARD]
            if len(cleaned) > max_messages:
                warnings.append(
                    f"{action_type} 说了 {len(cleaned)} 句，比这一轮建议的 "
                    f"{max_messages} 句多（照发，提示词里会提醒收敛）"
                )
            if len(cleaned) > MAX_SAY_LINES_HARD:
                warnings.append(
                    f"{action_type} 的消息超过防刷屏硬顶 {MAX_SAY_LINES_HARD} 条，已截断"
                )
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
        action.send_to = _clean_send_to(item.get("send_to"))

        params = item.get("params")
        if isinstance(params, dict):
            action.params = params
        # 工具型动作只给 intent、不给 params 是正常的：参数由辅助模型在调用前按工具 schema 补全。
        # 真正补不出来时，引擎会写一条带原因的 skip 事件——这里不需要再猜。
        action.queries = _parse_queries(item.get("queries"))
        depth = str(item.get("search_depth", "") or "").strip().lower()
        action.search_depth = depth if depth in ("quick", "standard", "deep") else ""
        try:
            wanted_reads = int(item.get("read_pages", -1))
        except (TypeError, ValueError):
            wanted_reads = -1
        action.read_pages = wanted_reads if wanted_reads >= 0 else -1

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
    if not head_clean and leading_text:
        warnings.append(f"模型在 JSON 之前先写了别的内容：{leading_text}")
    cancel = parse_cancel(payload.get("cancel"))
    plan_mode = parse_plan_mode(payload.get("plan_mode"))
    if not cancel and plan_mode:
        cancel = _PLAN_MODE_TO_CANCEL.get(plan_mode, "")
    return ParseResult(
        actions=actions,
        reasoning=reasoning,
        warnings=warnings,
        raw_text=raw,
        head_clean=head_clean,
        leading_text=leading_text,
        memory=parse_memory(payload.get("memory")),
        cancel=cancel,
        plan_mode=plan_mode,
        chat_note=_clean_note(payload.get("chat_note")),
        open_topic=_clean_note(payload.get("open_topic")),
        heart_knot=_clean_note(payload.get("heart_knot")),
        grudge=_clean_note(payload.get("grudge")),
        forgive=_as_bool(payload.get("forgive")),
        own_topic=_clean_note(payload.get("own_topic")),
        own_topic_done=_as_bool(payload.get("own_topic_done")),
        tone=_parse_tone(payload.get("tone")),
        valence_delta=_parse_valence_delta(payload.get("valence_delta")),
        affinity_delta=_parse_affinity_delta(payload.get("affinity_delta")),
        tail=_json_tail(cleaned_text),
    )


TONES = ("praise", "hug", "attack", "normal")
"""对方口吻的合法取值。``normal`` 与空字符串都表示"不加情绪脉冲"。"""


def _parse_tone(value: Any) -> str:
    """模型判的口吻：只认白名单，其余当没判（交给兜底）。"""

    text = str(value or "").strip().lower()
    aliases = {
        "夸奖": "praise",
        "夸": "praise",
        "亲昵": "hug",
        "哄": "hug",
        "怼": "attack",
        "阴阳": "attack",
        "攻击": "attack",
        "普通": "normal",
    }
    text = aliases.get(text, text)
    return text if text in TONES else ""


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


def _parse_affinity_delta(value: Any) -> float:
    """模型给的好感变化：-1 ~ 1（正=更亲近），缺失或写坏都当 0。

    这里只做范围校验；"每轮最多多少、每天最多多少"由 ``ProfileStore`` 那层再削一次。
    """

    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(-1.0, min(1.0, number))


MAX_QUERIES = 5


def _parse_queries(payload: Any) -> list[str]:
    """检索型动作给的查询词：接受列表或一行一条的字符串，去重并限量。

    这里是纯清洗，真正的条数上限（按动作配置）由引擎那一步再截一次。
    """

    raw: list[Any] = []
    if isinstance(payload, str):
        raw = [line for line in payload.splitlines() if line.strip()]
    elif isinstance(payload, (list, tuple)):
        for entry in payload:
            if isinstance(entry, str) and "\n" in entry:
                raw.extend(line for line in entry.splitlines() if line.strip())
            else:
                raw.append(entry)
    result: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, (str, int, float)) or isinstance(entry, bool):
            continue
        text = " ".join(str(entry).split())[:120]
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= MAX_QUERIES:
            break
    return result


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

# 模型把「要说什么」写在别的字段名下时按这个顺序去找（都没有就什么都不发）
_SPEAK_KEYS = (
    "actions",
    "action",
    "say",
    "speech",
    "message",
    "messages",
    "content",
    "reply",
    "text",
)

# 一眼就能看出是 JSON / 思考残留的记号：带这些的一律不当发言
_JSON_SMELL = ("{", "}", "```", '"actions"', '"reasoning"', '"type"')


def _clean_speakable(value: Any) -> str:
    """一句话能不能发出去：短的、不带 JSON / 思考残留的才算话。"""

    body = str(value or "").strip().strip("`").strip()
    if not body or len(body) > _FALLBACK_MAX_CHARS:
        return ""
    if any(mark in body for mark in _JSON_SMELL):
        return ""
    return body


def speakable_messages(payload: dict[str, Any]) -> list[str]:
    """从模型给出的 JSON 里抠出「她要说的话」（抠不到就返回空列表）。

    只认明确的文本字段：``actions`` 写成字符串、或者换了 ``say`` / ``content``
    这类名字时，都按"她想说这一句"处理；``reasoning`` 从来不在这里。
    """

    for key in _SPEAK_KEYS:
        value = payload.get(key)
        lines: list[str] = []
        if isinstance(value, str):
            lines = [value]
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, str):
                    lines.append(entry)
                    continue
                if not isinstance(entry, dict):
                    continue
                for sub in ("messages", "message", "content", "text", "say"):
                    inner = entry.get(sub)
                    if isinstance(inner, str):
                        lines.append(inner)
                        break
                    if isinstance(inner, list):
                        lines.extend(str(item) for item in inner)
                        break
        else:
            continue
        cleaned = [item for item in (_clean_speakable(text) for text in lines) if item]
        if cleaned:
            return cleaned[:MAX_SAY_LINES_HARD]
    return []


def _fallback_result(cleaned: str, raw: str) -> ParseResult:
    """模型没给出 JSON 时的兜底。

    - 剩下来的是一句正常的话（短、不含 JSON 残留）→ 当成她说的一句，照发；
    - 剩下来的还是长文/思考/半个 JSON → 什么都不发，只在日志里留个警告。
    """

    body = str(cleaned or "").strip().strip("`").strip()
    if not _clean_speakable(body):
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
                "queries": _parse_queries(step.get("queries")),
                # 计划里每一步可以自己挑说话的地方：群里应付一句、私聊里再吐槽一句
                "send_to": _clean_send_to(step.get("send_to")),
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
        # 她想把这些话说给谁：会话组里的哪个群 / 私聊（留空 = 落点默认）
        "send_to": _clean_send_to(payload.get("send_to")),
        "source": "llm",
    }
    return plan, warnings


def _clean_send_to(value: Any) -> str:
    """模型写的落点：可能是会话 id、群号、昵称，也可能是个小对象。"""

    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("session", "id", "session_id", "to", "name", "target"):
            item = value.get(key)
            if item:
                return " ".join(str(item).split())[:60]
        return ""
    return " ".join(str(value).split())[:60]


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    """模型给的布尔值：真布尔、字符串 "true"/"是"/"对"、数字 1 都算真。

    它经常把 true 写成 "true" 或者 "是"，直接 bool() 会把 "false" 也当成真。
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in ("1", "true", "yes", "y", "是", "对", "真的", "算")

