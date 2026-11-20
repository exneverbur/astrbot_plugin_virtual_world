"""LLM 输出的 JSON 动作解析与校验（设计文档 5.6 节）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Collection

from . import aliases as aliases_module
from . import intention_policy
from . import topic_steer
from .extensions import _keep_paragraphs
from .source_events import normalize_sensitivity

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

from .host_log import logger  # noqa: PLC2701
"""修复成功时打 WARN 用（用户 m00001：「修复成功时记一条 WARN」）。"""

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

TAIL_MAX_CHARS = 200
"""JSON 之后那段尾巴最多留多少字（给靠标记工作的插件；超了截断，不丢弃）。"""

LEADING_MAX_CHARS = 200
"""JSON 之前那段话最多留多少字（交给回复钩子链做前缀透传；超了截断，不丢弃）。"""


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


# ---------------- 模型输出里的裸控制字符 / 被劈开的键名（用户 m00001） ----------------
# 线上样本（tests/fixtures/model_raw_1026.txt）：JSON **结构完整**，但字符串值里混进了
# **真实换行**——``"env": "卧室，安静私\n密。"``（这里是真换行，不是转义）。JSON 规范禁止
# 字符串内出现裸控制字符，于是 json.loads 整段失败 → 解析器退到"既不是 JSON 也不是
# 一句话"→ 整轮丢弃 → 她保持安静。下面两个函数只做**纯函数修复**，所以能单测：
# 一个收拾字符串内外的控制字符，一个把被换行劈开的键名接回已知键。
#
# 字符串**外**的换行更阴：线上 10-26 12:11 那轮折行落在数字中间
# （``"affinity_delta": 0.<折行>1,``），JSON 规范说那是"可选空白"没错，但留着一个
# 换行就是 ``0.\n1`` → ``Expecting ',' delimiter``，整包作废。所以字符串外一律删掉。

REPAIR_NONE = ""
"""没修复（原样就是合法 JSON，或者根本救不回来）。"""

REPAIR_CONTROL_CHARS = "control_chars"
"""只修了字符串里的裸控制字符（字符串外折行也记在这里）。"""

REPAIR_KEYS = "keys"
"""只把被劈开的键名接回去了。"""

REPAIR_BOTH = "both"
"""控制字符和键名都修了。"""

REPAIR_KEYS_AFTER_PARSE = "keys_after_parse"
"""**解析成功之后**才把带空白的键名归回白名单（用户 m00001 的 M5）。

和 :data:`REPAIR_KEYS` 的区别：``REPAIR_KEYS`` 是"JSON 本来读不出来、改文本才读出来"，
这一档是"**JSON 本来就合法**，只是键名里混了空格（``"hands "`` / ``"valence_ delta"``）"，
以前会原样通过 → 字段取不到 → 动作被丢 / 情绪增量变 0，**静默失效**。它不改一个字节的
原文（只在解析出来的 dict 上归一），所以要单独一个标记，日志也只记 debug。
"""

_REPAIR_SEPARATOR = "+"
"""多个修复标记叠在一起时的连接符（``both+keys_after_parse``）。"""


_CJK_EXTRA = "…—～·―–"
"""CJK 文本里常见、但落在通用标点区的几个符号（不在下面的区间里）。"""

_CN_PUNCT = "…—～·―–‥・‘’“”＂＇、。，！？；：（）《》〈〉【】〔〕「」『』"
"""中文标点：折行落在它旁边时一律直粘（用户 m00001）。

这些字符里大部分本来就被 :func:`_is_cjk` 的码位区间盖住了（``，！？；：（）`` 在
``FF00`` 段、``。、《》【】「」`` 在 ``3000`` 段、``…—·`` 在 :data:`_CJK_EXTRA`），
显式列一遍是为了两件事：

- 弯引号 ``‘’“”``（U+2018/2019/201C/201D）和 ``‥``（U+2025）**不在**任何 CJK 区间里，
  ``“你\\n在忙什么？”`` 这种行以前会被补成一个空格；
- 这份清单就是"中文标点"的定义，以后要加符号只改这里，不用动码位表。
"""

_CONTROL_WHITESPACE = "\r\n\t"
"""JSON 里**字符串外**是可选空白、字符串内是非法裸控制字符的那几个（用户 m00001）。
``\r\n`` 算一个换行：计数和"折行处补什么"都按一次算。"""


def _is_cjk(char: str) -> bool:
    """这个字符算不算"CJK 这边"：汉字、假名、全角标点、中文标点都算。"""

    if not char:
        return False
    if char in _CJK_EXTRA:
        return True
    code = ord(char)
    return (
        0x2E80 <= code <= 0x2FFF  # 部首 / 康熙部首
        or 0x3000 <= code <= 0x303F  # 中文标点（。「」『』、）与假名标点
        or 0x3040 <= code <= 0x30FF  # 平假名 / 片假名
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x4E00 <= code <= 0x9FFF  # 基本区
        or 0xF900 <= code <= 0xFAFF  # 兼容汉字
        or 0xFF00 <= code <= 0xFFEF  # 全角（，！？：；（）＊…）
        or 0x20000 <= code <= 0x2FA1F  # 扩展 B 及以后
    )


def _is_wrap_glue(char: str) -> bool:
    """这个字符算不算"折行处该直粘"的一侧：CJK 文字、中文标点（用户 m00001）。"""

    if not char:
        return False
    return _is_cjk(char) or char in _CN_PUNCT


def join_wrapped(prev_char: str, next_char: str) -> str:
    """折行处该补什么：**两侧任一侧**是中文（文字或标点）就直粘，否则补一个空格。

    上游中转（``api.openlux.ai``）会把输出**按显示宽度折行**，于是 JSON 字符串里
    出现了"真换行"。那是显示上的折行，不是原文里的空格——所以：

    - ``"安静私\\n密。"`` → ``安静私密``（两侧都是 CJK，删掉换行）；
    - ``"……\\n你在忙什么呀？"`` → ``……你在忙什么呀？``（10-26 事故：以前只认
      "两侧都在 CJK 区"，``……`` 后面被插了一个空格，群里的原话成了 ``…… 你在忙什么呀？``）；
    - ``"袋\\n子"`` / ``"看\\nnew"`` → 直粘（**任一侧**是中文就算词中间断开：
      中文和英文之间本来就不打空格）；
    - ``"say\\nhello"`` / ``"2026\\n-10"`` → ``say hello``（两侧都不是中文才补一个空格，
      不然英文会粘成一个词）。

    折行正好落在**值开头**（左邻居是开引号而不是正文）的情况由调用方处理：那里不是
    "词中间断开"，补空格会多出一个前导空格。

    单个换行处调用一次；``\\r\\n`` 只算一个换行（调用方已经合并），所以这里一次只判一对字符。
    """

    if prev_char and next_char and (
        _is_wrap_glue(prev_char) or _is_wrap_glue(next_char)
    ):
        return ""
    return " "


_NEWLINE_CHARS = "\r\n"
"""折行（``\\n`` / ``\\r`` / ``\\r\\n``）。与 :data:`_CONTROL_WHITESPACE` 不同：
这里**不含制表符**——制表符本来就是"空白"，交给调用方的 ``split()`` 处理，
不该被 :func:`glue_cjk_wraps` 当成折行粘掉。"""


def glue_cjk_wraps(text: str) -> str:
    """把**两侧有中文的折行**直接粘回去，其余换行先留成一个空格。

    这是 :func:`join_wrapped` 的"整段文本"版本，给**不是 JSON** 的那些地方用（用户
    m00001 的 M1/M3：写记忆、记忆输入的摘要）。那些地方以前一律 ``" ".join(text.split())``，
    于是模型输出里"按显示宽度折行"的换行被压成半角空格，中文词中间就多出一个空格
    （``在海\\n滨步道`` → ``在海 滨步道``）。

    规则与 JSON 那条路**完全同一套**（逐字符调 :func:`join_wrapped`）：

    - 任一侧是中文/中文标点 → 直粘（``海滨步道`` / ``……有`` / ``袋子``）；
    - 两侧都不是中文（英文、数字、ASCII 标点）→ 补一个空格（``say hello`` / ``1 2``）；
    - 折行落在**开头/结尾** → 什么都不补（不留前导 / 尾随空格）。

    这里**只处理换行**（``\\n`` / ``\\r`` / ``\\r\\n``，一段连续换行算一次折行）；
    其余空白（空格、制表符）原样留着——"压成单个空格"是调用方自己的事
    （通常接着跑 ``" ".join(text.split())``）。
    """

    body = str(text or "")
    if not body:
        return ""
    if "\n" not in body and "\r" not in body:
        return body
    out: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char not in _NEWLINE_CHARS:
            out.append(char)
            index += 1
            continue
        # 一段连续换行（``\r\n`` 混排、连续折行）算**一次**折行：只补一个分隔符。
        index += 1
        while index < length and body[index] in _NEWLINE_CHARS:
            index += 1
        if index >= length:
            # 折行落在结尾：不留尾随空格
            continue
        if not out:
            # 折行落在开头：不留前导空格
            continue
        out.append(join_wrapped(out[-1], body[index]))
    return "".join(out)


def repair_control_chars_counted(text: str) -> tuple[str, int]:
    """同 :func:`repair_control_chars`，另外带回「字符串内换掉了几个换行」。

    第二个返回值就是日志里的 ``in_string_newlines``：今天撞上折行节点几次，
    看这个数就够了（``\\r\\n`` 记 1）。

    先探一次 ``json.loads``：**本来就合法的 JSON 原样返回**（``0``），排版用的换行
    是原文的一部分，不许在这里被改。合法性探测同时证明了字符串里没有裸控制字符
    （JSON 规范不允许），所以那条路不用再扫一遍。
    """

    body = str(text or "")
    if not body:
        return "", 0
    try:
        json.loads(body)
    except (TypeError, ValueError):
        pass
    else:
        return body, 0
    out: list[str] = []
    in_string = False
    escaped = False
    replaced = 0
    just_opened = False
    """上一个写出去的非转义字符是**开引号**：折行落在值开头，不许补空格。"""
    index = 0
    length = len(body)
    while index < length:
        current = body[index]
        if not in_string:
            if current == '"':
                in_string = True
                just_opened = True
                out.append(current)
            elif current in _CONTROL_WHITESPACE:
                # 字符串外：换行 / 制表符是 JSON 语法里的**可选空白**，直接删掉
                # （不补空格、也不计进 ``in_string_newlines``）。线上 10-26 12:11
                # 那轮的 ``"affinity_delta": 0.<折行>1,`` 就是折行落在**数字中间**：
                # 留着它 json.loads 报 ``Expecting ',' delimiter``，整个动作包作废。
                pass
            else:
                out.append(current)
            index += 1
            continue
        if escaped:
            escaped = False
            just_opened = False
            out.append(current)
            index += 1
            continue
        if current == "\\":
            escaped = True
            just_opened = False
            out.append(current)
            index += 1
            continue
        if current == '"':
            in_string = False
            just_opened = False
            out.append(current)
            index += 1
            continue
        if current in _CONTROL_WHITESPACE:
            # \r\n 是一个换行：合并成一次替换，别补出两个空格
            if current == "\r" and index + 1 < length and body[index + 1] == "\n":
                index += 1
            # 折行是「显示折行」：中文之间直接粘回去，英文/数字边界才补空格；
            # 落在值开头（左邻居是开引号）就什么都不补，免得值多一个前导空格。
            if just_opened:
                out.append("")
            else:
                out.append(join_wrapped(out[-1] if out else "", body[index + 1] if index + 1 < length else ""))
            replaced += 1
            just_opened = False
            index += 1
            continue
        out.append(current)
        just_opened = False
        index += 1
    return "".join(out), replaced


def repair_control_chars(text: str) -> str:
    """把 JSON 里混进来的裸控制字符（``\\r\\n`` / ``\\n`` / ``\\r`` / ``\\t``）收拾掉。

    逐字符扫描，维护 ``in_string``（``"`` 开关，认得 ``\\\\`` 转义）与 ``escaped``：

    - 字符串内：换行按 :func:`join_wrapped` 处理（中文之间删掉、别处补一个空格）；
      ``\\r\\n`` 只算一个换行；``\\t`` 补一个空格；
      别的字符（包括非法转义 ``"\\x"``）原样留着——那是别的问题，不该在这里改；
    - 字符串外：``\\r\\n`` / ``\\n`` / ``\\r`` / ``\\t`` **一律删掉**。JSON 里字符串外的
      空白本来就是可选的，而线上折行会落进**数字中间**（``0.<折行>1``）——那种换行
      不是排版，是折痕，留着就是 ``Expecting ',' delimiter``；
    - 但**本来就合法的 JSON 原样返回**（一个字节都不动）：排版是原文的一部分。

    ``"\\""`` 这种转义引号不会被误判成字符串结束（``escaped`` 状态）。
    没开头的字符串（模型截断了）走到末尾就停下，不会抛异常。
    """

    return repair_control_chars_counted(text)[0]


def repair_json_keys(text: str, known: Collection[str] | None = None) -> str:
    """把**作为对象键**、被换行/空格劈开的键名接回已知键（``"open _topic"`` → ``"open_topic"``）。

    ``repair_control_chars`` 把 ``"open\\n_topic"`` 救成 ``"open _topic"``——这已经是合法
    JSON 了，但键名对不上 schema，``open_topic`` 照样读不到。所以再走一遍：只在
    引号内、且这个字符串**后面紧跟 ``:``**（即它确实是键名，不是值）时才动手；
    去掉全部空白后能在白名单里精确找到的才换，**未知键保持原样**（不乱猜，
    否则扩展自己声明的字段会被改写）。

    白名单默认是 :data:`_JSON_KEY_WHITELIST`（协议内建键 + reasoning 子键 + 动作键）；
    事件那套解析传自己的键集合（见 ``core/events.py``）。
    """

    body = str(text or "")
    names = _JSON_KEY_WHITELIST if known is None else frozenset(str(item) for item in known)
    if not names:
        return body
    lookup = {"".join(name.split()).lower(): name for name in names}
    out: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        current = body[index]
        if current != '"':
            out.append(current)
            index += 1
            continue
        start = index
        index += 1
        escaped = False
        closed = False
        while index < length:
            char = body[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                closed = True
                break
            index += 1
        if not closed:
            # 没有收尾引号（截断的输出）：剩下的原样留着
            out.append(body[start:])
            break
        content = body[start + 1 : index]
        index += 1
        probe = index
        while probe < length and body[probe] in " \t\r\n":
            probe += 1
        if probe < length and body[probe] == ":":
            # 键名归一只有两条规则，都要求"能精确对上"，不对就原样留着：
            # 1) 去掉**内部**全部空白后能对上已知键 → 换成规范名（`open _topic` → `open_topic`）；
            # 2) 只多出首尾空白（折行恰好落在键名末尾）→ 去掉首尾空白（`a ` → `a`）。
            target = lookup.get("".join(content.split()).lower())
            if target is not None and target != content:
                out.append('"' + target + '"')
                continue
            stripped_content = content.strip()
            if stripped_content and stripped_content != content:
                out.append('"' + stripped_content + '"')
                continue
        out.append(body[start:index])
    return "".join(out)


def normalize_json_keys_after_parse(
    data: Any, known: Collection[str] | None = None
) -> tuple[Any, bool]:
    """解析**成功之后**再按白名单把带空白的顶层键名归一（用户 m00001 的 M5）。

    为什么还要一遍：:func:`repair_json_keys` 只在"原文 ``json.loads`` 读不出来"时才跑。
    线上模型给过 ``{"hands ": false, "valence_ delta": 0, "actions": [...]}``——语法**完全
    合法**，于是走的是"原样解析成功"那条快路，``hands`` / ``valence_delta`` 取不到，
    动作被丢弃、情绪增量变 0，**一句日志都没有**。这里补上"合法也要看一眼键名"。

    只做两件事，都要求"能精确对上白名单"：

    1. 去掉**全部**空白后与某个白名单键**逐字相同**（``"valence_ delta"`` →
       ``valence_delta``、``"hands "`` → ``hands``）→ 用规范名；
    2. 对不上就**原样留着**（``"foo bar"`` → ``foobar`` 不在白名单 → 不改名）。

    边界（都很重要）：

    - **不做大小写模糊**：``"Hands"`` 不会变成 ``hands``（白名单里没有它就当未知键）；
    - **不递归**：只看顶层 dict。``actions`` 里的对象键由调用方自己按动作结构读，
      这里改深了会动到扩展声明的字段；
    - **不合并值**：改名后会撞上已存在的键（``{"hands": 1, "hands ": 2}``）就**放弃改名**，
      宁可不改也不能悄悄吞掉一个值。

    返回 ``(数据, 有没有改名)``；改名时返回**新 dict**，没改就原对象原样返回。
    """

    if not isinstance(data, dict):
        return data, False
    names = _JSON_KEY_WHITELIST if known is None else frozenset(str(item) for item in known)
    if not names:
        return data, False
    renamed = False
    rebuilt: dict[Any, Any] = {}
    for key, value in data.items():
        target = key
        if isinstance(key, str):
            flat = "".join(key.split())
            if flat != key and flat in names and flat not in data:
                target = flat
                renamed = True
        rebuilt[target] = value
    return (rebuilt if renamed else data), renamed


def repair_flags(repaired: str) -> tuple[str, ...]:
    """把 ``repaired`` 拆成标记元组（``both+keys_after_parse`` → ``("both", "keys_after_parse")``）。"""

    return tuple(part for part in str(repaired or "").split(_REPAIR_SEPARATOR) if part)


def repair_needs_notice(repaired: str) -> bool:
    """这次修复要不要进 ``warnings`` / WARN 日志。

    :data:`REPAIR_KEYS_AFTER_PARSE` **不算**：它处理的是本来就合法的 JSON，只是键名里
    混了空格，改的是解析出来的 dict、不是原文；记在 ``parse_repaired`` 字段和 debug 日志里
    就够了，每轮都往 warnings 里塞一条会把事件库和日志页刷满（用户 m00001：不要 WARN 刷屏）。
    """

    return any(flag != REPAIR_KEYS_AFTER_PARSE for flag in repair_flags(repaired))


def loads_json_repaired(
    text: str, *, known: Collection[str] | None = None
) -> tuple[Any, str, int]:
    """先按原样 ``json.loads``；失败再把裸控制字符 / 被劈开的键名修好重试。

    返回 ``(数据, 修复方式, 字符串内换行数)``：修复方式是 :data:`REPAIR_NONE` /
    :data:`REPAIR_CONTROL_CHARS` / :data:`REPAIR_KEYS` / :data:`REPAIR_BOTH` /
    :data:`REPAIR_KEYS_AFTER_PARSE`，几个叠在一起时用 ``+`` 连起来
    （例如 ``both+keys_after_parse``，见 :func:`repair_flags`）；第三个值是
    日志里那个 ``in_string_newlines``（没修就是 0）。修完还是读不出来时数据是 ``None``、
    修复方式是 :data:`REPAIR_NONE`——调用方据此走原来那套兜底，行为与修复前完全一致。

    ``known`` 是键名白名单（默认 :data:`_JSON_KEY_WHITELIST`）：事件那套解析
    （``core/events.py``）传自己的键集合，别让两边的键互相污染。

    **原文一个字节都不改**：本来就是合法 JSON 的输出走原样解析那条快路，唯一的
    后处理是 :func:`normalize_json_keys_after_parse` 在**解析出来的 dict** 上归一
    带空白的顶层键名（用户 m00001 的 M5：``"hands "`` 以前会静默失效）。

    **只有原样解析失败才会改文本**；两种文本修复在**同一次重试**里按顺序叠起来
    （控制字符 → 键名），中间不提前返回：``"open\\n_topic"`` 只修控制字符是合法 JSON
    （``"open _topic"``），但键名对不上 schema、``open_topic`` 照样读不到，所以键名归一
    必须接着做；``flags`` 如实记下各自动过没有（``control_chars`` / ``keys`` / ``both``，
    外加解析后的 ``keys_after_parse``）。
    """

    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        pass
    else:
        data, renamed = normalize_json_keys_after_parse(data, known)
        if not renamed:
            return data, REPAIR_NONE, 0
        # debug 级：这是"合法 JSON 的键名带空格"，不是上游折行事故，别 WARN 刷屏。
        logger.debug(
            "JSON 合法但顶层键名带空白，已按白名单归一：keys_after_parse raw_len=%d",
            len(str(text or "")),
        )
        return data, REPAIR_KEYS_AFTER_PARSE, 0
    control, newlines = repair_control_chars_counted(text)
    keyed = repair_json_keys(control, known)
    flags: list[str] = []
    if control != text:
        flags.append(REPAIR_CONTROL_CHARS)
    if keyed != control:
        flags.append(REPAIR_KEYS)
    if not flags:
        return None, REPAIR_NONE, 0
    try:
        data = json.loads(keyed)
    except (TypeError, ValueError):
        return None, REPAIR_NONE, 0
    data, renamed = normalize_json_keys_after_parse(data, known)
    base = REPAIR_BOTH if len(flags) > 1 else flags[0]
    if renamed:
        logger.debug(
            "JSON 修好后顶层键名仍带空白，已按白名单归一：keys_after_parse raw_len=%d",
            len(str(text or "")),
        )
        return data, base + _REPAIR_SEPARATOR + REPAIR_KEYS_AFTER_PARSE, newlines
    return data, base, newlines


def _repair_note(repaired: str, raw: str, newlines: int = 0) -> str:
    """修复成功时那一句话（进 ``warnings``，也就进了 ``reply`` 事件的 detail）。"""

    return (
        "模型输出的 JSON 里有裸换行 / 被劈开的键名，已修复"
        f"（repaired={repaired}, raw_len={len(str(raw or ''))}, "
        f"in_string_newlines={int(newlines or 0)}）"
    )


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
    """模型这一轮的原始输出（**不在这里截断**）。

    解析失败时（:func:`_fallback_result`）它就是"到底是什么让解析失败的"唯一证据：
    截断的 JSON、解释性文字、还是语法坏了。成功路径也留着它——``reply`` 事件用它记
    ``raw``（2 万字上限）。要写进其它事件时走 :func:`raw_output_fields`，
    并且**只在失败 / 有 ``warnings`` 时**记，别让事件库翻倍。
    """

    parse_repaired: str = ""
    """这一轮的 JSON 是靠**修复**才读出来的（用户 m00001）。

    取值 :data:`REPAIR_NONE` / :data:`REPAIR_CONTROL_CHARS` / :data:`REPAIR_KEYS` /
    :data:`REPAIR_BOTH`，解析后键名归一时再叠一段 ``+keys_after_parse``
    （:data:`REPAIR_KEYS_AFTER_PARSE`，见 :func:`repair_flags`）。线上样本：字符串值里
    混进真换行 → ``json.loads`` 整段失败 → 整轮被当成"既不是 JSON 也不是一句话"丢掉。
    修复成功时这里记下修复方式，``reply`` 事件的 detail 用它（同名字段 ``parse_repaired``）；
    原文照旧进 :attr:`raw_text`，一个字都不改。
    """

    in_string_newlines: int = 0
    """为修这个 JSON，字符串里换掉了几个裸换行（``\\r\\n`` 记 1）。

    上游中转会把输出**按显示宽度折行**，同一提示连续几次里有的折、有的不折。
    这个数就是"今天又撞上折行节点几次"的统计口径，跟着 ``parse_repaired`` 一起
    进 ``reply`` 事件的 detail。
    """

    dropped_json: bool = False
    """这一轮是"像 JSON 的原文"、又抢救不出台词，**整段丢掉**（用户 m00001）。

    置位的那一轮一个字都没发出去。引擎据此写一条 ``reply_dropped_json`` 事件
    （``raw_len`` / ``raw_head`` / ``raw_output``），线上照它就能把原文捞回来——
    10-26 事故里 2 次仍然把裸 JSON 发到了群里，就是这条路上没有这道闸。
    """

    salvaged_lines: int = 0
    """坏 JSON 抢救成功时，从 ``lines[].text`` / ``actions[].messages[]`` 抠出了几行。"""

    topic_proposal: dict[str, Any] | None = None
    used_topic_ids: list[str] = field(default_factory=list)
    communicative_act: str = ""
    request_kind: str = ""
    """他这句话**要什么**：``task`` / ``chat`` / ``help`` / ``urgent`` / ``affection`` / ``none``。

    只认白名单里的原样字符串（大小写、空格变体一律留空），交给上层决定要不要当成求助。
    留空 = 模型没写或写的东西认不出。
    """

    request_expect: str = ""
    """他具体**想让她做什么**（一句话，已清洗截断到 40 字）。留给上层当参考，不直接进群。"""

    memory: str = ""
    """这次对话值得记住的一句话（以她的视角，由模型顺手总结）。留空表示模型没给。"""

    sensitivity: str = ""
    """模型建议的**敏感度档位**（``normal`` / ``person`` / ``private`` / ``locked``）。

    它只描述 :attr:`memory` 这一句的传播范围（"这事能跟谁说"），交给登记谈资的地方
    写进来源事件。**模型只能建议更严**：这里认不出的值一律退回 ``private``
    （清单口径"解析失败或未知值按 private"），代码那边还会再取一次最严，
    所以模型写"正常"也顶不回一条已经更严的来源。
    """

    persons: list[str] = field(default_factory=list)
    """:attr:`memory` 里**涉及的人**（user_id / 昵称，模型点名的那几个）。

    只有敏感度是 ``person`` 时才有意义：这些人所在的会话可以提这件事。说这句话的
    人由代码补进去（来源事件的 ``sender_id``），模型漏写也漏不掉当事人。
    """

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

    intent: str = ""
    """**她自己说**要去做的一件事（≤40 字；顶层字段，不是 ``reasoning.intent``）。

    T12 Step 2 修：以前"她的打算"是系统从 ``own_topics`` 里替她猜的，于是聊天里冒出
    的半句技术细节也会变成"她打算做的事"，然后一拍就被放弃。现在只有她自己说出口的
    才算数；闲聊、客套、"没事"这类空话一律留空（见 :func:`_parse_self_intent`）。
    """

    share: str = ""
    """这一轮听到/想到、以后值得说的一件事（≤40 字）。空 = 这轮没有。

    T8-2：这是"谈资"的即时写入口——她跟人聊着聊着听到一件以后能当谈资的事，
    顺手记一句。过质量闸（具体动作/非第三人称/来源可讲）后才进谈资池；
    每轮最多 1 条，写进去的来源是本轮这个会话。
    """

    tone: str = ""
    """这一轮**对方对她是什么口吻**：``praise``（夸她）/ ``hug``（哄她、亲昵动作）/
    ``attack``（怼她、阴阳、冒犯）/ ``normal``（普通）／空 = 没判。

    以前这一步是拿关键词表猜的（"你可真行"会被当成夸奖），现在由主模型判——
    它反正要看这一整句话，多输出一个词不花钱。
    """

    addressing: str = ""
    """这一句**是在跟谁说话**：``me``（对她说）/ ``others``（在跟别人说）/
    ``unclear``（看不出来）／空 = 没判。

    群里一句「你倒是说啊」不写清是在问谁，很容易被她当成在问自己。
    """

    touch: list[str] = field(default_factory=list)
    """他这一轮**碰到她身上哪儿**（模型写的自由文本，例如 ``["腰", "耳后"]``）。

    只有扩展声明要这项（``ExtensionSpec.wants``）时提示词里才会要求模型填，
    没人要就永远是空列表——主插件自己不解释、不用它做任何事，
    只是原样转给声明了的扩展。
    """

    extra: dict[str, Any] = field(default_factory=dict)
    """扩展**自己声明**的那些字段（``ExtensionSpec.json_fields``）。

    主插件不认识它们是什么，只按扩展声明的形状收拾干净（截断 / 去重 / 限长）再转交。
    没人声明时这里永远是空的，主插件的提示词里也不会多一个字。
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

    expects_reply: bool | None = None
    """她这句话**发出去之后还会等对方回音吗**（用户 m28281）。

    ``None`` = 模型没给这个字段（老提示词 / 老模型）：调用方按字面兜底。
    ``False`` 明确说了"说完就算了"，就不该过一会儿再去追一句。
    """

    silent: bool = False
    """模型明确写了 ``"response": false``：**这一轮她不开口**。

    只丢 ``say``（要说的话），别的动作照常执行——"不接这句话"不等于"这一轮什么也不做"。
    它和"模型没给出任何动作"（一个错误）不是一回事：这是她自己的选择，
    调用方要照常推进水位线（否则同一批消息下一轮会被原样再喂一遍），
    也不该记成失败、更不该交回主人格替她说话。真值只在回复轮出现
    （自主轮次的提示词里没有这个字段）。
    """

    silent_reason: str = ""
    """她为什么不接（模型写的 ``response_reason``，已经清洗截断）。

    只进日志与调试，**不会发到群里**；留空时调用方自己兜一句默认说法。
    """

    topic: dict[str, str] = field(default_factory=dict)
    """这一轮她把话**拐到哪儿**（``{"move": str, "own": str}``，T12 Step 3 / E2）。

    模型在回复 JSON 里写 ``topic: {"move": "follow|own|probe|switch", "own": "…"}``。
    这里只收拾形状，**不判断允不允许**：许可在 ``topic_steer.steer_permission``，
    "own 是不是候选里那一条"的降级也在调用方。旧输出没有这个字段时是
    ``{"move": "follow", "own": ""}``——与今天完全一致，不会因为缺字段改变行为。
    """

    tease: dict[str, Any] = field(default_factory=dict)
    """这一轮**她自己**决定要不要损对方一句（``{"on": bool, "style": str, "target": str}``）。

    模型在回复 JSON 里写 ``tease: {"on": true, "style": "dry_irony", "target": "他说..."}``。
    这里只负责解析成三个字段，**不做任何决定**：旧输出没有这个字段时是
    ``{"on": False, ...}``（等于没写），风格是否可用由 ``response_variation`` 的许可判定说了算。
    """


REASONING_KEYS = ("env", "state", "mood", "who", "inner", "intent", "check", "meme", "hands")
"""推理草稿的字段顺序，也是提示词里要求的顺序。

``inner`` 是"心里想的"：第一人称的一两句心理活动（只给自己看，不进群）。

``meme`` / ``hands`` 是"手上有没有事"的表态（2026-10-19 决定 D3）：``meme`` 写想配
什么样的图、``hands`` 写想自己动手办什么，不想就写 ``false``。它们只是让她先想一下，
做不做仍由动作决定；留在这里才能被 ``WorldState.note_reasoning`` 收下、调试页看得见。
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


REQUEST_KINDS = ("task", "chat", "help", "urgent", "affection", "none")
"""``request.kind`` 的受控词表：他这句话要的是哪一类事。

只认原样的字符串（大小写、前后空格变体一律留空），空串在消费方等价于 ``none``。
"""


def parse_request(payload: Any) -> tuple[str, str]:
    """把顶层 ``request`` 归一成 ``(kind, expect)``。

    不是对象（缺失 / 字符串 / 列表）就两个都留空串；``kind`` 只认 :data:`REQUEST_KINDS`
    里的原样值，``expect`` 转字符串后截到 40 字。它**绝不抛错**，也不表示解析失败——
    模型没写或写了看不懂的东西，只说明这一轮没有可用的"他要什么"。
    """

    if not isinstance(payload, dict):
        return "", ""
    kind = payload.get("kind")
    # 不做 lower/strip：白名单就是白名单，大小写和空格变体都当作"没写对"
    kind_text = kind if isinstance(kind, str) and kind in REQUEST_KINDS else ""
    expect = payload.get("expect")
    expect_text = "" if expect is None else str(expect).strip()[:40]
    return kind_text, expect_text


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里挖出合法 JSON 对象（会先剥掉思考段）。

    候选顺序：代码块 → 整段文本 → 每个 ``{`` 起的花括号配对片段（从后往前试）。
    最后这一步是为"模型在思考里贴了一段示例 JSON、正文才是真 JSON"这种情况准备的。

    每个候选先按原样 ``json.loads``；失败才走 :func:`loads_json_repaired`（裸换行 /
    被换开的键名）。修复方式在 :func:`extract_json_object_with_repair` 里带出来。
    """

    data, _repaired, _newlines = extract_json_object_with_repair(text)
    return data


def extract_json_object_with_repair(
    text: str,
) -> tuple[dict[str, Any] | None, str, int]:
    """同 :func:`extract_json_object`，额外带出「靠什么修复才读出来的」和「换掉几个换行」。

    修复成功时在这里打一条 WARN（``repaired=… raw_len=… in_string_newlines=…``）——
    **原文一个字节都不改**，只是把它修到 ``json.loads`` 能读，后续照常走动作校验。
    """

    if not text:
        return None, REPAIR_NONE, 0
    body = strip_reasoning(text)
    if not body:
        return None, REPAIR_NONE, 0
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
        data, repaired, newlines = loads_json_repaired(stripped)
        if data is None:
            continue
        if repaired and repair_needs_notice(repaired):
            logger.warning(
                "模型输出的 JSON 有裸控制字符 / 被劈开的键名，已修复："
                "repaired=%s raw_len=%d in_string_newlines=%d",
                repaired,
                len(str(text or "")),
                newlines,
            )
        if isinstance(data, dict):
            return data, repaired, newlines
        if isinstance(data, list):
            return {"actions": data}, repaired, newlines
    return None, REPAIR_NONE, 0


def parse_action_payload(
    text: str,
    *,
    available_actions: set[str],
    valid_nodes: set[str] | None = None,
    valid_targets: set[str] | None = None,
    max_actions: int = 3,
    max_messages: int = 3,
    json_fields: Any = (),
) -> ParseResult:
    """把模型输出解析成动作列表。

    校验规则：
    - 解析失败 -> 降级为单条 say（原文），fallback_used=True；
    - type 不在当前可用动作里 -> 丢弃并记录警告；
    - target_node 不存在 -> 丢弃该动作；
    - messages 超限 -> 截断；
    - 工具型动作缺 params -> 丢弃。

    ``json_fields``：扩展声明要主模型额外标出来的字段（``ExtensionSpec.json_fields``），
    按声明的形状解析进 :attr:`ParseResult.extra`；不传就是空字典。
    """

    warnings: list[str] = []
    raw = text or ""
    # 推理草稿有没有真的写在最开头：`{` 之前冒出来的东西都算"先说了别的"
    leading_text = ""
    head_clean = True
    brace = str(raw).find("{")
    if brace > 0:
        leading_text = " ".join(str(raw)[:brace].split())[:LEADING_MAX_CHARS]
        head_clean = not leading_text
    elif brace < 0:
        head_clean = False
    # 思考段先剥掉：既不能让"她其实在思考"的内容被当成发言发出去，
    # 也不能让它挡着后面的 JSON。
    cleaned_text = strip_reasoning(raw)
    # 原样读不出来时，extract_json_object_with_repair 会先修裸换行 / 被劈开的键名再试
    # （用户 m00001：线上那轮就是被字符串里的真换行整段搞废的）。修复方式带出来，
    # 一路写进 reply 事件的 detail，原文照旧进 raw_text。
    payload, parse_repaired, in_string_newlines = extract_json_object_with_repair(raw)
    if payload is None:
        return _fallback_result(cleaned_text, raw)
    if parse_repaired and repair_needs_notice(parse_repaired):
        warnings.append(_repair_note(parse_repaired, raw, in_string_newlines))

    items = payload.get("actions")
    # 她自己说"这一轮不接"：与"模型没给出动作"（错误）分开处理。
    # 连 `actions` 写成一段文本的那种降级发言也要拦住——说了不接就真的不发。
    # 其余字段（expects_reply / request / 心情增量…）由 _build_result 统一从 payload 读，
    # 任何一个构造点（包括下面的早退分支）都漏不掉。
    silent = _parse_silence(payload.get("response"))
    if silent:
        # 说了不接话 = 这一轮**不开口**：只丢 say（要说的话），别的动作照常执行——
        # 她想接着想、想换个地方、想记点什么，都不该被"不接这句话"一起拦掉。
        if isinstance(items, dict):
            candidates: list[Any] = [items]
        elif isinstance(items, list):
            candidates = [item for item in items if item]
        else:
            candidates = []
        kept: list[Any] = []
        dropped_says = 0
        for item in candidates:
            if isinstance(item, dict) and str(item.get("type") or "").strip() == "say":
                dropped_says += 1
                continue
            kept.append(item)
        if dropped_says:
            warnings.append(
                f"模型写了 response: false：{dropped_says} 个 say 没有发出去"
                "（别的动作照常执行）"
            )
        elif speakable_messages(payload):
            warnings.append(
                "模型写了 response: false、又写了要说的话：那些话没有发出去"
            )
        items = kept
    if isinstance(items, dict):
        # 只写了一个动作对象、没包成数组：当成一条动作处理
        items = [items]
    if not isinstance(items, list):
        # actions 写成了字符串（模型偶尔这么干），或者干脆没写：
        # 只从 JSON 里抠出「她真要说的话」——**绝不能**把 reasoning 或整个 JSON 发到群里。
        #
        # 「干脆没写」这一支再分两层（线上 09:09 / 09:26 各丢过一轮）：
        # 模型把**事件收尾那句**的格式用在了聊天回合里——
        #   { "intent": "outcome", " lines": [{"text": "...", "delay": 0}] }
        # JSON 合法、没有 actions、也没有旧的 ``say``/``content``，于是整轮被丢掉，
        # 她一句话都不说。那不是"她决定不说"，是"她想说这一句、只是没写动作"。
        # 所以：**没有 actions 键**且能抠出台词时，按一句话发出去。
        # 反过来，写了 actions（哪怕是 null / 字符串）就不套这层兜底——那属于"动作写坏了"，
        # 照原样报 warning，别把一次失败的动手降级成"说说"。
        if not _has_actions_field(payload):
            speech = _speech_only_messages(payload)
            if speech and _is_quiet_intent(payload):
                # intent=quiet = 这一幕她**不说**：台词丢掉，但理由要看得见（不是解析失败）。
                return _build_result(
                    payload,
                    actions=[],
                    warnings=[SPEECH_ONLY_QUIET_WARNING],
                    fallback_used=True,
                    raw_text=raw,
                    silent=True,
                    json_fields=json_fields,
                    parse_repaired=parse_repaired,
            in_string_newlines=in_string_newlines,
                )
            if speech:
                return _build_result(
                    payload,
                    actions=[PlannedAction(type="say", messages=speech)],
                    warnings=[SPEECH_ONLY_WARNING],
                    fallback_used=True,
                    raw_text=raw,
                    silent=silent,
                    json_fields=json_fields,
                    parse_repaired=parse_repaired,
            in_string_newlines=in_string_newlines,
                )
        spoken = speakable_messages(payload)
        if spoken:
            return _build_result(
                payload,
                actions=[PlannedAction(type="say", messages=spoken)],
                warnings=["模型把 actions 写成了一段文本，已按发言处理"],
                fallback_used=True,
                raw_text=raw,
                silent=silent,
                json_fields=json_fields,
                parse_repaired=parse_repaired,
            in_string_newlines=in_string_newlines,
            )
        return _build_result(
            payload,
            actions=[],
            warnings=["模型输出缺少 actions 字段，已忽略（没有发到群里）"],
            fallback_used=True,
            raw_text=raw,
            silent=silent,
            json_fields=json_fields,
            parse_repaired=parse_repaired,
            in_string_newlines=in_string_newlines,
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

    if not head_clean and leading_text:
        warnings.append(f"模型在 JSON 之前先写了别的内容：{leading_text}")
    return _build_result(
        payload,
        actions=actions,
        warnings=warnings,
        raw_text=raw,
        tail=_json_tail(cleaned_text),
        head_clean=head_clean,
        leading_text=leading_text,
        silent=silent,
        json_fields=json_fields,
        parse_repaired=parse_repaired,
            in_string_newlines=in_string_newlines,
    )


def _build_result(
    payload: dict[str, Any],
    *,
    actions: list[PlannedAction],
    warnings: list[str],
    fallback_used: bool = False,
    raw_text: str = "",
    tail: str = "",
    head_clean: bool = True,
    leading_text: str = "",
    silent: bool = False,
    json_fields: Any = (),
    parse_repaired: str = "",
    in_string_newlines: int = 0,
) -> ParseResult:
    """构造 :class:`ParseResult`，并把从 ``payload`` 里读的字段一次性读齐。

    所有构造点都走这里。以前早退分支各自手写一份 ``ParseResult``，漏掉了
    ``valence_delta`` / ``affinity_delta`` / ``tone`` / ``memory`` / ``silent``
    这些字段——模型明明写了，心情却不落账（"边界没有声音"）。
    分支之间的差别只允许体现在显式实参上（``actions`` / ``warnings`` /
    ``fallback_used`` / ``raw_text`` / ``tail`` / ``head_clean`` /
    ``leading_text`` / ``silent`` / ``json_fields``），字段读法只有这一处。

    ``silent`` 由调用方传入：主流程要先用它决定丢不丢 ``say``，这里只负责落到字段上。
    """

    request_kind, request_expect = parse_request(payload.get("request"))
    cancel = parse_cancel(payload.get("cancel"))
    plan_mode = parse_plan_mode(payload.get("plan_mode"))
    if not cancel and plan_mode:
        cancel = _PLAN_MODE_TO_CANCEL.get(plan_mode, "")
    tease, tease_warning = _parse_tease(payload.get("tease"))
    if tease_warning:
        warnings.append(tease_warning)
    topic, topic_warning = _parse_topic(payload.get("topic"))
    if topic_warning:
        warnings.append(topic_warning)
    return ParseResult(
        actions=actions,
        reasoning=parse_reasoning(payload.get("reasoning")),
        warnings=warnings,
        fallback_used=fallback_used,
        raw_text=raw_text,
        head_clean=head_clean,
        leading_text=leading_text,
        topic_proposal=(
            dict(payload["topic_proposal"])
            if isinstance(payload.get("topic_proposal"), dict)
            else None
        ),
        used_topic_ids=(
            list(
                dict.fromkeys(
                    str(item).strip()
                    for item in payload.get("used_topic_ids", [])
                    if isinstance(item, str) and item.strip()
                )
            )[:2]
            if isinstance(payload.get("used_topic_ids"), list)
            else []
        ),
        communicative_act=(
            str(payload.get("communicative_act") or "")
            if payload.get("communicative_act")
            in {"answer", "reference", "introduce", "progress", "outcome"}
            else ""
        ),
        request_kind=request_kind,
        request_expect=request_expect,
        memory=parse_memory(payload.get("memory")),
        sensitivity=parse_sensitivity(payload.get("sensitivity")),
        persons=parse_persons(payload.get("persons")),
        cancel=cancel,
        plan_mode=plan_mode,
        chat_note=_clean_note(payload.get("chat_note")),
        open_topic=_clean_note(payload.get("open_topic")),
        heart_knot=_clean_note(payload.get("heart_knot")),
        grudge=_clean_note(payload.get("grudge")),
        forgive=_as_bool(payload.get("forgive")),
        own_topic=_clean_note(payload.get("own_topic")),
        own_topic_done=_as_bool(payload.get("own_topic_done")),
        intent=_parse_self_intent(payload.get("intent")),
        share=_parse_share(payload.get("share")),
        tone=_parse_tone(payload.get("tone")),
        addressing=_parse_addressing(payload.get("addressing")),
        touch=_parse_touch(payload.get("touch")),
        extra=_parse_extra_fields(payload, json_fields),
        valence_delta=_parse_valence_delta(payload.get("valence_delta")),
        affinity_delta=_parse_affinity_delta(payload.get("affinity_delta")),
        tail=tail,
        silent=silent,
        silent_reason=_clean_note(payload.get("response_reason")) if silent else "",
        expects_reply=_parse_expects_reply(payload.get("expects_reply")),
        tease=tease,
        topic=topic,
        parse_repaired=parse_repaired,
            in_string_newlines=in_string_newlines,
    )


TONES = ("praise", "hug", "comfort", "attack", "refuse", "normal")
"""对方口吻的合法取值。``normal`` 与空字符串都表示"不加情绪脉冲"。

``refuse`` 是他**明确叫停 / 拒绝**（他说了停、不要、别这样）：它不产生情绪脉冲，
但会被别的层读到——挂了扩展时，扩展据此收手并记下"这件事他不要"。
"""

ADDRESSING = ("me", "others", "unclear")
"""「这句是在跟谁说话」的合法取值。"""


def _parse_expects_reply(value: Any) -> bool | None:
    """「这句会不会等对方回音」：没写就是 ``None``（交给调用方兜底），不硬当 False。"""

    if value is None:
        return None
    if isinstance(value, str):
        body = value.strip().lower()
        if body in ("false", "no", "0", "不", "否", "不用"):
            return False
        if body in ("true", "yes", "1", "是", "要"):
            return True
        return None
    return bool(value)


def _parse_touch(value: Any) -> list[str]:
    """模型标的"他碰了她哪儿"：只收短名词，去重保序，最多 5 个。

    主插件**不解释**这些词——原样交给声明要它的扩展（谁要谁自己认）。
    """

    items: list[Any]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        items = re.split(r"[、,，/\s]+", text)
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    picked: list[str] = []
    for item in items:
        body = " ".join(str(item or "").split())[:12]
        if body and body not in picked:
            picked.append(body)
    return picked[:5]


_TEASE_WARNING_SENT = False
"""``tease`` 形状看不懂的警告只报一次（进程级）：每个回话轮次都报会把日志刷满。"""


def _parse_tease(value: Any) -> tuple[dict[str, Any], str]:
    """模型自己决定「这一轮要不要损对方一句」：``on`` / ``style`` / ``target``。

    只做形状收拾，**不判断能不能用**（策略是否合法、力度、冷却都在
    ``response_variation`` 的许可判定里）。缺失 / 认不出的形状一律 ``on=False``：
    旧输出没有这个字段，必须当成「没这个功能」而不是「默认开」。

    宽容两种偷懒写法：``"tease": true``（只写开关）和 ``"tease": "dry_irony"``
    （只写风格，视为 ``on=True``）。写 ``{"style": ...}`` 而漏了 ``on`` 也按写了风格算开。

    返回 ``(tease, warning)``：形状真的看不懂时给一句警告（由调用方并进
    ``warnings``），**整个进程只报一次**——每个回话轮次都记一条会把日志刷满。
    """

    blank = {"on": False, "style": "", "target": ""}
    if value is None:
        return blank, ""
    if isinstance(value, bool):
        return {"on": value, "style": "", "target": ""}, ""
    if isinstance(value, (int, float)):
        return {"on": bool(value), "style": "", "target": ""}, ""
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"false", "off", "no", "0", "none", "n/a"}:
            return blank, ""
        if text.lower() in {"true", "on", "yes", "1"}:
            return {"on": True, "style": "", "target": ""}, ""
        return {"on": True, "style": text[:40], "target": ""}, ""
    if isinstance(value, dict):
        style = str(value.get("style") or value.get("kind") or "").strip()
        target = _clean_note(value.get("target") or value.get("point") or value.get("about"))
        if "on" in value:
            on = _as_bool(value.get("on"))
        elif "enabled" in value:
            on = _as_bool(value.get("enabled"))
        else:
            on = bool(style)
        return {"on": bool(on), "style": style[:40], "target": target}, ""
    global _TEASE_WARNING_SENT
    warning = ""
    if not _TEASE_WARNING_SENT:
        _TEASE_WARNING_SENT = True
        warning = (
            f"tease 字段的形状看不懂（{type(value).__name__}），按 on=false 处理；"
            "这条只提醒一次"
        )
    return blank, warning


_TOPIC_WARNING_SENT = False
"""``topic`` 形状看不懂的警告只报一次（进程级）：每个回话轮次都报会把日志刷满。"""


def _parse_topic(value: Any) -> tuple[dict[str, str], str]:
    """模型声明这一轮话题怎么走：``{"move": …, "own": …}``（T12 Step 3 / E2）。

    只做形状收拾，**不判断允不允许**（回合类型、冷却、上限、候选是否存在都在
    ``topic_steer`` 里）。缺失 / 坏形状 / 明确写 ``follow`` 一律回**空 dict**
    （=「这一轮没什么可记的」）：旧输出没有这个字段，必须当成「照常接话」而不是
    报错——**绝不因为字段不合法就吞掉 ``say``**。

    返回 ``(topic, warning)``：形状真的看不懂时给一句警告（由调用方并进
    ``warnings``），**整个进程只报一次**。
    """

    global _TOPIC_WARNING_SENT
    if value is None:
        return {}, ""
    if not isinstance(value, (str, dict, int, float, bool)):
        warning = ""
        if not _TOPIC_WARNING_SENT:
            _TOPIC_WARNING_SENT = True
            warning = (
                f"topic 字段的形状看不懂（{type(value).__name__}），按 follow 处理；"
                "这条只提醒一次"
            )
        return {}, warning
    move, own = topic_steer.normalize_move(value)
    if move == "follow":
        # 没声明 / 声明就是接话：留空，和今天没有这个字段的输出完全一样
        return {}, ""
    return {"move": move, "own": own}, ""


_BUILTIN_JSON_KEYS = frozenset(
    {
        "reasoning",
        "memory",
        "sensitivity",
        "persons",
        "chat_note",
        "open_topic",
        "heart_knot",
        "grudge",
        "forgive",
        "own_topic",
        "own_topic_done",
        "intent",
        "share",
        "tone",
        "addressing",
        "touch",
        "tease",
        "topic",
        "valence_delta",
        "affinity_delta",
        "expects_reply",
        "request",
        "actions",
        "cancel",
        "plan_mode",
    }
)
"""主插件自己那套键名：扩展声明里写了它们也一律跳过（那是协议的地盘）。"""

_JSON_KEY_WHITELIST = _BUILTIN_JSON_KEYS | frozenset(REASONING_KEYS) | frozenset(
    {
        # 顶层里不在 _BUILTIN_JSON_KEYS 的那几个（沉默 / 话题提案 / 谈资用过的条目）
        "response",
        "response_reason",
        "topic_proposal",
        "used_topic_ids",
        "communicative_act",
        # 模型偶尔作为顶层字段写出来的那两个（提示词里曾经这么要求过）
        "meme",
        "hands",
        # 只有调试页看的那几个（模型写了就留着，被劈开了也接回去）
        "stage",
        "narration",
        "edge",
        # 动作对象里的键
        "type",
        "messages",
        "message",
        "content",
        "target",
        "target_node",
        "duration",
        "params",
        "queries",
        "search_depth",
        "read_pages",
        "send_to",
        "goal",
        "purpose",
        # tease / topic / request 的子键
        "on",
        "style",
        "move",
        "kind",
        "expect",
        # 事件收尾那句（``core/events.py`` 也走同一套修复）
        "lines",
        "text",
        "delay",
        "say",
        "plan",
        "success",
        "fail",
    }
)
"""``repair_json_keys`` 的白名单：只有能精确映射回这里的键名才动手。

线上样本里被劈开的是 ``open_topic`` / ``grudge`` / ``addressing`` / ``stage`` /
``affinity_delta`` / ``used_topic_ids`` / ``own_topic_done`` / ``request.kind``——
它们都在这里，所以修复后的 JSON 能被 :func:`_build_result` 正常读出来。
未知键（扩展自己声明的字段）**不在白名单里就不改**，宁可读不到也不乱猜。
"""


def _field_declaration(raw: Any) -> dict[str, Any]:
    """声明写成 dict 或 :class:`JsonField` 都认，统一成 dict。"""

    if isinstance(raw, dict):
        return dict(raw)
    return {
        "name": getattr(raw, "name", ""),
        "kind": getattr(raw, "kind", "list"),
        "max_items": getattr(raw, "max_items", 5),
        "max_chars": getattr(raw, "max_chars", 24),
    }


def _to_float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(-1_000_000.0, min(1_000_000.0, number))


def _parse_extra_fields(payload: dict[str, Any], fields: Any) -> dict[str, Any]:
    """按扩展声明的形状收拾"主模型额外标出来的字段"。

    声明长这样：``{"name": "arousal", "kind": "list|str|num|bool",
    "max_items": 5, "max_chars": 24}``（:class:`JsonField` 也认）。

    主插件不猜扩展想要什么——声明认不出来、或者模型没写这个键，就当作没这一项。
    """

    picked: dict[str, Any] = {}
    for raw in tuple(fields or ()):
        data = _field_declaration(raw)
        name = " ".join(str(data.get("name") or "").split())
        if not name or name in _BUILTIN_JSON_KEYS or name not in payload:
            continue
        value = payload.get(name)
        if value is None:
            continue
        kind = str(data.get("kind") or "list").strip().lower()
        # 列表型每一项最多 24 个字；字符串型整段最多 60 个字；长文本型最多 1200 个字
        hard_chars = {"str": 60, "text": 1200}.get(kind, 24)
        try:
            limit_chars = max(1, min(hard_chars, int(data.get("max_chars") or hard_chars)))
        except (TypeError, ValueError):
            limit_chars = hard_chars
        try:
            limit_items = max(1, min(5, int(data.get("max_items") or 5)))
        except (TypeError, ValueError):
            limit_items = 5
        if kind == "list":
            items: list[Any]
            if isinstance(value, str):
                items = re.split(r"[、,，/\s]+", value.strip()) if value.strip() else []
            elif isinstance(value, (list, tuple, set)):
                items = list(value)
            else:
                continue
            words: list[str] = []
            for item in items:
                body = " ".join(str(item or "").split())[:limit_chars]
                if body and body not in words:
                    words.append(body)
            if words:
                picked[name] = words[:limit_items]
        elif kind == "num":
            number = _to_float_or_none(value)
            if number is not None:
                picked[name] = number
        elif kind == "bool":
            picked[name] = _as_bool(value)
        elif kind == "text":
            # 长文本（正文那种）：**换行照原样留着**，它是靠空行分段的
            text = _keep_paragraphs(value)[:limit_chars]
            if text:
                picked[name] = text
        else:
            text = " ".join(str(value or "").split())[:limit_chars]
            if text:
                picked[name] = text
    return picked


def _parse_addressing(value: Any) -> str:
    """模型判的"在跟谁说话"：只认白名单，其余当没判。"""

    text = str(value or "").strip().lower()
    aliases = {
        "我": "me",
        "对我说": "me",
        "对我": "me",
        "自己": "me",
        "别人": "others",
        "对别人": "others",
        "他人": "others",
        "别人说的": "others",
        "不确定": "unclear",
        "看不清": "unclear",
        "未知": "unclear",
    }
    text = aliases.get(text, text)
    return text if text in ADDRESSING else ""


def _parse_tone(value: Any) -> str:
    """模型判的口吻：只认白名单，其余当没判（交给兜底）。"""

    text = str(value or "").strip().lower()
    # 中文别名表已经收到 core/aliases.py 一处（A 线收尾）；这里只做白名单校验
    text = aliases_module.TONE_ALIASES.get(text, text)
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


def _json_tail(text: str, limit: int = TAIL_MAX_CHARS) -> str:
    """JSON 之后剩下的尾巴（给「靠标记工作的插件」用，不会发到群里）。

    Step 0 之后尾巴不再只认 ``[【(`` 开头的短标记：插件的标记长什么样都行，
    所以这里只负责**保留**——超长就截断到 ``limit``，但绝不整段丢弃。
    """

    body = str(text or "").strip()
    if not body:
        return ""
    end = body.rfind("}")
    if end < 0 or end >= len(body) - 1:
        return ""
    tail = body[end + 1 :].strip().strip("`").strip()
    if not tail:
        return ""
    return tail[:limit]


def _clean_note(payload: Any) -> str:
    """群聊背景句：只取一句话，太长就截断。"""

    if payload is None:
        return ""
    text = " ".join(str(payload).split())
    return text[:80]


SHARE_TEXT_LIMIT = 40
"""``share`` 的字数上限（T8-2 口径：≤40 字）。"""


def _parse_share(payload: Any) -> str:
    """``share``：这一轮听到/想到、以后值得说的一件事（一句话，≤40 字）。

    模型偶尔会把没人说的话、或者"没什么可说的"写进来：空串 / ``null`` /
    ``"无"`` 这类占位一律当没写。剩下的截到 40 字——质量闸在引擎侧，
    解析层只负责长度和空值。
    """

    if payload is None or isinstance(payload, bool):
        return ""
    if isinstance(payload, (int, float)):
        return ""
    text = " ".join(str(payload).split())
    if not text or text in {"null", "None", "无", "没有", "空", "-"}:
        return ""
    return text[:SHARE_TEXT_LIMIT]


SELF_INTENT_LIMIT = 40
"""``intent`` 的字数上限：一条打算得一句话说完（T12 Step 2 口径：≤40 字）。"""


def _parse_self_intent(payload: Any) -> str:
    """``intent``：她自己说"我要去做某件事"（一句话，≤40 字）。

    只要模型"为了填满字段"硬编一句，就会冒出一条她根本没打算做的计划，所以这里
    卡两道：占位/空话（``null`` / ``没事`` / ``算了`` / ``无`` …）一律当没写；剩下
    压平空白、截到 :data:`SELF_INTENT_LIMIT` 字。全程 fail-soft——解析出任何岔子都
    返回空串，绝不把异常带到回复链路上。
    """

    try:
        if payload is None or isinstance(payload, bool):
            return ""
        if isinstance(payload, (int, float)):
            return ""
        if isinstance(payload, (dict, list, tuple, set)):
            # 结构化的垃圾（模型把字段写成对象/数组）当没写，别 str() 出一串花括号
            return ""
        text = " ".join(str(payload).split())
        if not text or intention_policy.is_empty_talk(text):
            return ""
        return text[:SELF_INTENT_LIMIT]
    except Exception:  # noqa: BLE001 —— 新字段出岔子只是"这轮没有打算"
        return ""


# 模型写"这一轮不接话"时可能用的说法（除布尔 false 与数字 0 之外）
_SILENT_WORDS = frozenset(
    {"false", "no", "none", "null", "0", "否", "不", "不用", "不要", "不回复", "不回应", "不接"}
)


def _parse_silence(value: Any) -> bool:
    """``"response"`` 字段是不是明确的"这一轮不接话"。

    只认**明确写出来的否**：缺字段、``true``、``"true"`` 都算照常回话。
    模型把字段写成一句话（"不接了"）也认，但只在整句就是否定词时才算。
    """

    if value is None:
        return False
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return float(value) == 0.0
    text = " ".join(str(value).split()).strip().lower()
    return text in _SILENT_WORDS


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

# 一眼就能看出是 JSON / 思考残留的记号：带这些的一律不当发言。
# 注意这里**没有** ``[``：台词里出现方括号（``［笑］`` / ``[捂脸]``）是正常写法，
# "以 ``[`` 开头"这条只在 :func:`_looks_like_json_blob` 里按**开头**判。
_JSON_SMELL = (
    "{",
    "}",
    "```",
    '"actions"',
    '"lines"',
    '"reasoning"',
    '"intent"',
    '"type"',
)

_JSON_HEAD = ("{", "[")
"""像 JSON 的原文最常见的两种开头：对象、数组（用户 m00001）。"""

DROPPED_JSON_WARNING = "模型输出是像 JSON 的原文，没有当成发言发出去（已忽略）"
"""坏 JSON 又抢救不出台词时留在 ``warnings`` 里的话（线上 10-26 事故）。"""

SALVAGED_JSON_WARNING = "模型输出不是合法 JSON，只抢救出其中的台词"
"""坏 JSON 抢救成功：只发抠出来的可读文本，原文不发。"""


def _looks_like_json_blob(body: str) -> bool:
    """这段文本是不是"像 JSON 的原文"：不许当话发出去（用户 m00001）。

    10-26 线上：模型输出的 JSON 字符串里有裸换行 → 解析失败 → 兜底把**整段裸 JSON**
    当成一句话发到了群里（用户看到 ``{ "intent": "outcome", "lines": [{"text": …``）。
    这里就是那道闸：strip 后以 ``{`` / ``[`` 开头，或者带着 ``"lines"`` / ``"actions"``
    / ``"reasoning"`` / ``"intent"`` 这类 JSON 键，一律算"像 JSON"，不能当发言。
    """

    text = str(body or "").strip()
    if not text:
        return False
    if text.startswith(_JSON_HEAD):
        return True
    return any(mark in text for mark in _JSON_SMELL)


def _clean_speakable(value: Any) -> str:
    """一句话能不能发出去：短的、不带 JSON / 思考残留的才算话。"""

    body = str(value or "").strip().strip("`").strip()
    if not body or len(body) > _FALLBACK_MAX_CHARS:
        return ""
    if any(mark in body for mark in _JSON_SMELL):
        return ""
    return body


# 「只说了话、没给动作」时按这个顺序找台词（线上样本是 ``lines``）。
# 顺序是有讲究的：``lines`` 最具体（一条一条的台词），``say`` / ``text`` / ``message``
# 是模型换了名字；``reasoning`` 从来不在这里（那是思考，发出去比不说糟得多）。
_SPEECH_ONLY_KEYS = ("lines", "say", "text", "message")

SPEECH_ONLY_WARNING = "模型只给了台词、没给动作，已按直接发言处理"
"""只给了台词的那一轮留在 ``warnings`` 里的话（线上排查要能一眼认出来）。"""

SPEECH_ONLY_QUIET_WARNING = (
    "模型只给了台词、但 intent 是 quiet（这一幕不说话）：台词没有发出去"
)
"""``intent=quiet`` 又写了台词：尊重"这一幕不开口"，但要写清为什么丢掉台词。"""


def _loose_fields(mapping: Any) -> dict[str, Any]:
    """把**顶层**键名两端的空白与大小写抹平（同名键以第一个为准）。

    只给"只说了话、没给动作"这条兜底用：线上见过
    ``{ "intent": "outcome", " lines": [{"text": ...}] }``——键名前面多了一个空格，
    ``payload.get("lines")`` 就取不到，她写的话整句丢掉，一整轮什么都不说。

    这里**只归一这一层**，不动解析主路径的取键方式：``actions`` 里把 ``type`` /
    ``messages`` 写成别的名字仍然算那一轮写坏了，交给现有 warning 与原文日志处理。
    """

    if not isinstance(mapping, dict):
        return {}
    fields: dict[str, Any] = {}
    for key, value in mapping.items():
        name = str(key).strip().lower()
        if name and name not in fields:
            fields[name] = value
    return fields


def _speech_candidates(value: Any) -> list[str]:
    """把一个字段值摊平成候选台词：``str`` / ``[str]`` / ``[{"text"|"messages"}]``。"""

    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        return []
    lines: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            lines.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        inner = _loose_fields(entry)
        for sub in ("text", "messages", "message"):
            text = inner.get(sub)
            if isinstance(text, str):
                lines.append(text)
                break
            if isinstance(text, list):
                lines.extend(str(item) for item in text if isinstance(item, str))
                break
    return lines


def _speech_only_messages(payload: dict[str, Any]) -> list[str]:
    """payload 里**没有 actions** 时，按 ``lines`` → ``say`` → ``text`` → ``message``
    的顺序抠出"她想说这一句"（抠不到就返回空列表）。

    和 :func:`speakable_messages` 的区别：那条路是"``actions`` 写坏了"的降级，
    会把 ``actions`` 本身也算进候选；这条路专门给"压根没写 ``actions``、只写了台词"
    的情况（线上 09:09 / 09:26 各丢过一轮），候选里没有 ``actions``，也不含
    ``reasoning`` / ``content`` / ``reply``——它们的归属见调用处的顺序说明。
    """

    fields = _loose_fields(payload)
    for key in _SPEECH_ONLY_KEYS:
        cleaned = [
            item
            for item in (
                _clean_speakable(text) for text in _speech_candidates(fields.get(key))
            )
            if item
        ]
        if cleaned:
            return cleaned[:MAX_SAY_LINES_HARD]
    return []


def _has_actions_field(payload: dict[str, Any]) -> bool:
    """payload 里有没有 ``actions`` 这个键（键名同样容忍两端空白与大小写）。

    注意区分"没写"和"写了但是 ``null`` / 字符串"：只要有这个键，就说明模型**想**给
    动作，走原来的严格路径（写坏了照原样报 warning），不套用"只说了话"的兜底。
    """

    return any(str(key).strip().lower() == "actions" for key in (payload or {}))


def _is_quiet_intent(payload: dict[str, Any]) -> bool:
    """``intent`` 是不是明确的 ``quiet``（这一幕她不说）。大小写/空白不敏感。"""

    value = _loose_fields(payload).get("intent")
    if not isinstance(value, str):
        return False
    return " ".join(value.split()).strip().lower() == "quiet"


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


_SALVAGE_TEXT_RE = re.compile(r'"(?:text|messages|say|message)"\s*:\s*("(?:\\.|[^"\\])*")')
"""JSON 已经坏到 ``json.loads`` 读不动时，按字段名抠双引号字符串（**不是**新的 JSON 解析器）。

只认 ``lines[].text`` / ``actions[].messages[]`` / ``say`` / ``text`` 这四个字段名，
值必须是完整的双引号字符串，抠出来的还要过 :func:`_clean_speakable`：截断在半截的
JSON 能救回来，而"整段裸 JSON"永远不会因此被当成话发出去。
"""

# 抢救时按这个顺序找台词（用户 m00001）：lines[].text → actions[].messages[] → say → text
_SALVAGE_KEYS = ("lines", "actions", "say", "text")


def _salvage_texts(body: str) -> list[str]:
    """从一段语法坏掉的 JSON 文本里按字段名抠台词（抠不到返回空列表）。"""

    found: list[str] = []
    for match in _SALVAGE_TEXT_RE.finditer(body):
        try:
            text = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        cleaned = _clean_speakable(text)
        if cleaned:
            found.append(cleaned)
        if len(found) >= MAX_SAY_LINES_HARD:
            break
    return found


def _salvage_speakable_text(raw: str) -> list[str]:
    """坏 JSON 的抢救：只取可读台词，取不到返回空列表（用户 m00001）。

    两步，都用**现有**工具，不新写 JSON 解析器：

    1. :func:`extract_json_object_with_repair`（内含 ``repair_control_chars_counted``
       + ``repair_json_keys``）把坏 JSON 修到 ``json.loads`` 能读，再按
       ``lines[].text`` → ``actions[].messages[]`` → ``say`` → ``text`` 取台词；
    2. 修不动（截断得只剩半截）才退到按字段名抠字符串（:func:`_salvage_texts`）。

    返回空列表 = 抢救失败，调用方必须保持安静（写 ``reply_dropped_json``，绝不发原文）。
    """

    text = str(raw or "").strip()
    if not text:
        return []
    payload, _repaired, _newlines = extract_json_object_with_repair(text)
    if isinstance(payload, dict):
        fields = _loose_fields(payload)
        for key in _SALVAGE_KEYS:
            messages = [
                item
                for item in (
                    _clean_speakable(candidate)
                    for candidate in _speech_candidates(fields.get(key))
                )
                if item
            ]
            if messages:
                return messages[:MAX_SAY_LINES_HARD]
    body = strip_reasoning(text)
    if not body:
        return []
    # 只在"明显是 JSON 片段"的文本里按字段名抠：连 lines/actions 都没有，就不猜了
    if '"lines"' not in body and '"actions"' not in body:
        return []
    repaired_body, _ = repair_control_chars_counted(body)
    return _salvage_texts(repaired_body)


def _fallback_result(cleaned: str, raw: str) -> ParseResult:
    """模型没给出 JSON 时的兜底。

    这里没有 payload 可读，所以不经过 :func:`_build_result`（别的构造点都必须经过）。

    三条路（用户 m00001）：

    - 剩下来的是一句正常的话（短、不含 JSON 残留）→ 当成她说的一句，照发；
    - 剩下来的**像 JSON**（以 ``{`` / ``[`` 开头，或带 ``"lines"`` / ``"actions"`` /
      ``"reasoning"`` 这类键）→ **绝不整段发**：先 :func:`_salvage_speakable_text`
      抢救，抢救出来只发抠到的台词，抢救不出来就安静 + ``dropped_json``（引擎写
      ``reply_dropped_json`` 事件）。10-26 事故就是这条路把整段裸 JSON 发了出去；
    - 别的一律不发（长文 / 思考 / 半个 JSON），只在日志里留个警告。
    """

    body = str(cleaned or "").strip().strip("`").strip()
    speakable = _clean_speakable(body)
    if speakable and not _looks_like_json_blob(speakable):
        return ParseResult(
            actions=[PlannedAction(type="say", messages=[speakable])],
            warnings=["模型输出不是合法 JSON，已降级为直接发言"],
            fallback_used=True,
            raw_text=raw,
        )
    if _looks_like_json_blob(body) or _looks_like_json_blob(raw):
        salvaged = _salvage_speakable_text(raw)
        if salvaged:
            return ParseResult(
                actions=[PlannedAction(type="say", messages=list(salvaged))],
                warnings=[SALVAGED_JSON_WARNING],
                fallback_used=True,
                raw_text=raw,
                salvaged_lines=len(salvaged),
            )
        return ParseResult(
            actions=[],
            warnings=[DROPPED_JSON_WARNING],
            fallback_used=True,
            raw_text=raw,
            dropped_json=True,
        )
    return ParseResult(
        actions=[],
        warnings=["模型输出既不是 JSON 也不是一句话，已忽略（没有发到群里）"],
        fallback_used=True,
        raw_text=raw,
    )


# ---- 解析失败时留给日志的原文（用户 m00001） ----
# 线上只看到一句「模型输出既不是 JSON 也不是一句话」，看不到模型到底给了什么：
# 是截断、是解释性文字、还是 JSON 语法坏了。这里只做观测，**不改解析行为**。
RAW_OUTPUT_LIMIT = 2000
"""``raw_output`` 最多留多少字（超出截断并标明原始长度）。"""

RAW_DROPPED_LIMIT = 20000
"""``reply_dropped_json`` 里 ``raw_output`` 的上限（用户 m00001）。

这条事件**唯一的用途就是留住原文**（那一轮一个字都没发出去），所以放宽到和 ``reply``
事件的 ``raw`` 同一个口径（2 万字），不跟解析失败的 2000 字观测上限挤在一起。
"""

RAW_OUTPUT_HEAD = 200
"""``raw_head`` 留多少字：给时间线一行预览用。"""

RAW_WARN_LIMIT = 500
"""那行 WARN 日志里正文最多留多少字（全文进事件库）。"""

# 模型输出正常不会带密钥；真混进来时日志不该成为新的泄露面。
_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")


def scrub_secrets(text: str) -> str:
    """把像 ``sk-`` 密钥的长串打掉（只用于写日志，不回写解析结果）。"""

    return _SECRET_PATTERN.sub("sk-***", str(text or ""))


def raw_output_fields(text: str, *, limit: int = RAW_OUTPUT_LIMIT) -> dict[str, Any]:
    """给 ``event_log.detail`` 用的「原始输出」三件套。

    - ``raw_output``：原文；超过 ``limit`` 截断并追加「…（共 N 字符）」；
    - ``raw_len``：**原始长度**（截不截断都一样）；
    - ``raw_head``：前 200 字，给时间线一行预览用。

    只在一轮**出过问题**时调用（解析失败 / 有 ``warnings``）：正常成功的那一轮，
    ``reply`` 事件里已经存过 ``raw``（2 万字上限），再存一份只会让事件库翻倍。
    换行与引号原样保留——写库走 ``json.dumps``，不需要自己转义。
    空文本返回空字典，调用方可以直接 ``detail.update(...)``。
    """

    raw = str(text or "")
    if not raw:
        return {}
    safe = scrub_secrets(raw)
    body = safe if len(safe) <= limit else safe[:limit] + f"…（共 {len(raw)} 字符）"
    return {
        "raw_output": body,
        "raw_len": len(raw),
        "raw_head": safe[:RAW_OUTPUT_HEAD],
    }


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


def parse_sensitivity(payload: Any) -> str:
    """模型建议的敏感度档位；**认不出的一律按 ``private``**（清单 §2.2 第 4 条）。

    三种输入分别对待：

    - **字段缺省**（``None`` / 空串）→ 返回空串 = "模型没说"。这一档不能直接当
      ``normal``：调用方（engine）会在"没说"时按 ``private`` 处理——聊天里的事
      默认可能是私事，这个默认值不该藏在解析层，否则读代码的人看不见它；
    - **认得出的词**（四档 + 中英文别名）→ 归一成四档之一；
    - **认不出的词**（模型自己发明了"半公开"）→ ``private``，往严的那一侧错。

    这里不做"取最严"——那是登记时 :func:`build_visibility` 的活。解析层只负责
    "把模型的话翻成四档之一"，翻译错了最坏是少说一句，翻译宽了才是事故。
    """

    if payload is None:
        return ""
    text = str(payload).strip()
    if not text:
        return ""
    level = normalize_sensitivity(text)
    # 认不出的词会被 normalize 成 private；这里要把它和"真的写了 private"区分开吗？
    # 不需要：两者对下游的效果完全一样（都按私密存），而且 private 本身就是从严。
    return level


def parse_persons(payload: Any) -> list[str]:
    """``memory`` 里涉及的人（列表 / 逗号分隔的字符串都认）。

    只做形状收拾（去空白、去重、限长限量），**不判断谁是谁**：模型给的是昵称还是
    user_id 由它自己决定，代码在登记时把说话人补进去、判定时按集合比。
    """

    if payload is None:
        return []
    if isinstance(payload, (list, tuple, set)):
        raw_items: list[Any] = list(payload)
    elif isinstance(payload, dict):
        # {"id": "..."} 这种写法按人本身理解，别把整个 dict 的字符串塞进去
        raw_items = [
            payload.get("id") or payload.get("user_id") or payload.get("name") or ""
        ]
    else:
        raw_items = str(payload).replace("，", ",").split(",")
    people: list[str] = []
    for item in raw_items:
        person = str(item or "").strip()[:40]
        if person and person not in people:
            people.append(person)
        if len(people) >= 8:
            break
    return people


def parse_plan_payload(
    text: str,
    *,
    available_actions: set[str],
    valid_nodes: set[str] | None = None,
    max_steps: int = 8,
    allow_messages: bool = True,
) -> tuple[dict[str, Any] | None, list[str]]:
    """解析决策器要求的「计划」输出。

    ``allow_messages=False``：计划里**不收台词**（自主流 §1）。她要说什么，等这一步
    真轮到的时候再单独生成一次——「决定做什么」和「这口气怎么说」是两件事，挤在
    同一次输出里，台词就会变成对动作的解说，而不是真在说话。
    """

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
                "messages": (
                    step.get("messages")
                    if allow_messages and isinstance(step.get("messages"), list)
                    else []
                ),
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
    if not allow_messages:
        wrote = [
            step for step in steps
            if isinstance(step, dict) and isinstance(step.get("messages"), list) and step.get("messages")
        ]
        if wrote:
            warnings.append("计划里的台词已忽略：这一刻先决定做什么，台词等这一步轮到时再说")
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


def parse_messages(text: str, *, limit: int = 3) -> list[str]:
    """只解析「她这一句说什么」：``{"messages": [...]}``。

    自主流 §1 单独生成台词时用。模型偶尔会直接说人话（不带 JSON）：那种情况一律
    **不收**——宁可这一拍不开口，也不能把一段解释当成她说的话发出去。
    """

    payload = extract_json_object(text or "")
    if not isinstance(payload, dict):
        return []
    raw = payload.get("messages")
    if not isinstance(raw, (list, str)) and isinstance(payload.get("actions"), list):
        # 模型把动作外壳一起带回来了：只认第一个 say/share 的台词，别把别的动作当话
        for item in payload["actions"]:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") in {"say", "share"} and item.get("messages") is not None:
                raw = item.get("messages")
                break
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    lines = [" ".join(str(item).split()) for item in raw if str(item or "").strip()]
    return [line for line in lines if line][: max(1, int(limit))]


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

