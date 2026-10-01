"""虚拟世界引擎：把配置、状态、决策、动作、记忆串起来。

这是整个插件的核心 seam（见 docs/SEAMS.md），只依赖 core/ports.py 里的 Protocol，
因此可以在没有 AstrBot 的环境里完整测试。

职责边界：
- 注入模式（被 @ 时）：只产出「世界认知」文本交给主人格，不接管回复；
- 接管模式（自主行为）：自己调 LLM、自己发消息。两条路径互斥。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from typing import Any, Callable

from .config_store import ConfigStore
from .db import AsyncDatabase
from .defaults import DEFAULT_WEATHER_PROMPT
from .decider import DESIRE_CUDDLE, Decider, reply_willingness
from .engagement import EngagementTracker
from .events import (
    ABILITIES,
    ABILITY_LABELS,
    KIND_NEED_INTERVENE,
    KIND_SOLO,
    MODE_IMAGINED,
    PHASE_ACT,
    SCOPE_SESSION,
    TIER_LABELS,
    TIER_BIG,
    TIER_MICRO,
    Decision,
    EventPackage,
    append_step,
    ability_hint,
    apply_ability_delta,
    clamp_ability_delta,
    default_decision,
    event_from_payload,
    new_thread,
    normalize_abilities,
    open_threads,
    parse_decision,
    parse_event_package,
    parse_settlement,
    parse_suggestions,
    roll_check,
    situational_modifiers,
    subjective_hint,
    surprise,
    thread_digest,
    tier_emotion,
    thread_exhausted,
    thread_step_lines,
)
from .json_actions import (
    CANCEL_MODES,
    MAX_SAY_LINES_HARD,
    PlannedAction,
    extract_json_object,
    parse_action_payload,
    parse_plan_payload,
    speakable_messages,
)
from .memory import INNER, INTERACTION, SCENE, MemoryEngine
from .models import (
    ActionDef,
    ChainStep,
    ColdStartMode,
    ECHO_EVENT_TYPES,
    NodeDef,
    SessionDef,
    WorldConfig,
    action_intimacy,
    pronoun_for as pronounce_for,
)
from .nickname import compute_nickname, should_update
from .pathfinding import find_path, nearest_node, path_ticks
from .planner import active_plan, advance, create_plan, peek_step
from .consolidate import Consolidator
from .profile import ProfileStore
from .prompt import WEEKDAY_NAMES, PromptBuilder, clock_text, lean_prompt, period_of
from .timeline import render_event
from .ports import CardResult, ToolCallResult
from .state import (
    STATE_AWAKENING,
    STATE_DROWSY,
    STATE_IDLE,
    STATE_NAPPING,
    STATE_SLEEPING,
    STATE_WALKING,
    WorldState,
    chat_item_is_fresh,
    group_chat_items,
    take_last_chat_groups,
)
from .state_dynamics import SOOTHE_VALENCE_BELOW, StateDynamics
from .search import (
    Evidence,
    clean_passage,
    evidence_text,
    failed_text,
    looks_like_homepage,
    looks_like_nav,
    merge_evidence,
    parse_search_results,
    queries_too_similar,
    render_evidence,
    sources_of,
    unwrap_payload_text,
)
from .weather import (
    WEATHER_KEY,
    WEATHER_TRY_KEY,
    WeatherRecord,
    banner as weather_banner,
    parse_parts,
    prompt_block as weather_prompt_block,
)
from .tool_policy import allowed_tools, is_self_send_tool
from .mood import (
    SELF_CARE_NOTE,
    StyleCell,
    cell_for,
    day_mood_branch,
    day_mood_info,
    keyword_signal,
    roll_day_mood,
    style_block as render_style_block,
)

from .generator import (
    auto_layout,
    clamp_node_count,
    clamp_per_node,
    link_plan,
    parse_generated_actions,
    parse_generated_nodes,
)

DEFAULT_TICK_SECONDS = 60.0

EXTENSION_LAYER_HEADING = "# ========== 扩展注入层： =========="
"""预览里给扩展那一层起的小标题（调试页的分段索引按它列一段）。"""

# 检索流水线：整条动作（多条查询 + 读正文 + 补查）一共最多花这么多秒
SEARCH_BUDGET_SECONDS = 20.0
# 读回来的正文按链接缓存这么久，避免同一篇被反复抓
READ_CACHE_SECONDS = 6 * 3600
# 一篇正文最多留多少字给模型看
READ_PASSAGE_CHARS = 1200

# 「自上次回复以来收到的图」最多留几张：多留几张是为了让超出内联上限的旧图
# 还能被转述成文字（只留上限张数的话，多出来的图除了丢掉没有别的出路）
PENDING_IMAGE_KEEP = 12
# 调试回显的防抖窗口（秒）：同一工具的连续调用（例如一次检索并行查 4 条）
# 只发第一条，别把群刷成一排一样的行
DEBUG_ECHO_DEBOUNCE_SECONDS = 2.5
_DEBUG_ECHO_TYPES = ("tool_call", "tool_result", "command_call", "command_result")
# 只给排查用的类型：选「完整」才发到群里，选「精简」只留在日志页
DEBUG_ONLY_ECHO_TYPES = ("incoming",)
# 没写意图、也没配主题/模板时的兜底搜索主题（见 PromptBuilder 里那段说明）
DEFAULT_SEARCH_TOPIC = "今天有什么新鲜事"
# 「合并重启」：她还在调模型时又来了新消息，就把几条并成一次请求重新发起。
# 只在**还没开始发送**的窗口里做，所以不会出现"说了半句又改口"。
MERGE_WINDOW_SECONDS = 5.0
# 「合并重启」的次数上限与一次能并进来的条数上限
MERGE_MAX_RESTARTS = 2
MERGE_MAX_TEXTS = 8
# 连续打断的保护：同一个人连着打断太多次时，先让她把这一轮说完，
# 免得刷屏的人让她永远开不了口
INTERRUPT_STREAK_MAX = 3
INTERRUPT_STREAK_WINDOW_SECONDS = 180.0
REPLY_INTERRUPTED = "被新消息打断：这一次生成作废"
"""``handle_reply`` 的 ``error`` 标记：这轮被新消息打断，调用方不要再补一句。"""

REPLY_MUTED = "本小时被动回复已达上限：这一条不回"
"""``handle_reply`` 的 ``error`` 标记：配额用尽，接管但静默（不落回主人格）。"""

REPLY_GUARD_RETRIES = 3
"""扩展判"这一轮是废的"（模型拒答）时最多重问几次；问满就这一轮不出声。"""
# 一轮里最多往群里贴几张工具 / 指令生成的图（批量出图时不至于刷屏）
MAX_GROUP_IMAGES = 9
# 事件掷骰最多按这么久折算概率：tick 被拖慢时不该一次补出"必中"
EVENT_ROLL_MAX_GAP_HOURS = 0.25
# 最近一次检索记在 kv 里的键前缀与有效期（提示词里提醒她"刚查过什么"）
SEARCH_LOG_KEY = "search.last"
SEARCH_LOG_MINUTES = 60
# 好奇高过这条线时，除了上网查，也会想去问人（和 decider 里"好奇→搜索"用同一条线）
ASK_ABOUT_CURIOSITY = 0.7
# 别人的负面口吻在她这儿有多重，按**亲密度**缩放：路人几乎不往心里去，
# 特别的人一句话顶别人几句。曲线是 `floor + (ceil - floor) * (档位比例 ^ 1.6)`，
# 七档时的实际值大约是：敌意 0.20 / 冷淡 0.27 / 客气 0.42 / 熟人 0.63 /
# 朋友 0.88 / 亲近 1.17 / 特别的人 1.50。
TONE_INTIMACY_FLOOR = 0.2
TONE_INTIMACY_CEIL = 1.5

DESIRE_TEASE_FLOOR = 0.3
DESIRE_TEASE_CEIL = 1.6
"""被撩一下的加成倍数：路人撩她几乎没用，特别的人撩一下跳得最明显。"""
# 生成搜索关键词时的额外要求：不写清楚，模型会给你一个"什么都要"的万能查询
SEARCH_QUERY_RULES = (
    "这次要填的是**搜索关键词**：写成能直接丢进搜索框的词（谁 / 什么时候 / 哪方面），"
    "20 字以内；只查一件事，不要罗列多个主题，"
    "不要写成「获取……的最新信息/实时更新」这种句子；"
    "最近聊天里提到过的话题优先。"
)


def _clean_search_topic(reply: Any) -> str:
    """把"想查什么"的那句回答洗干净；看起来不像话题（JSON、代码、空话）就用空串。"""

    text = " ".join(str(reply or "").split()).strip().strip("「」\"'。!！?？")
    if not text or len(text) > 40:
        return ""
    banned = ("{", "}", "query", "参数", "JSON", "json")
    if any(item in text for item in banned):
        return ""
    return text


def _echo_payload(
    event_type: str, detail: dict[str, Any], enabled: set[str]
) -> bool:
    """这条事件要不要发到群里。

    调试回显是为了补上"群里本来看不见的东西"（决定、耗时、工具调用、被跳过的动作），
    所以她自己说过的话不再重复发一遍：可见动作的文本已经作为普通消息发出去了，
    而想事情这类静默动作（visible=False）的文本从不出现在群里，照旧回显。
    """

    if event_type not in enabled:
        return False
    if event_type == "action":
        return not (bool(detail.get("visible")) and bool(detail.get("messages")))
    return True
# 写记忆时模型偶尔会"反过来问人名"。出现这些字样就不是记忆，改用兜底文案。
MEMORY_REFUSAL_HINTS = (
    "捏造",
    "没有出现对方",
    "没出现对方",
    "对方是谁",
    "对方的名字",
    "告诉我对方",
    "请告诉",
    "需要你告诉",
    "无法确定对方",
    "给不出",
)

@dataclass
class TickOutcome:
    """一次 tick 对某个会话产生的可见结果。"""

    session_id: str
    messages: list[str] = field(default_factory=list)
    routed: dict[str, list[str]] = field(default_factory=dict)
    """要说给**别的会话**的话：``{session_id: [文本…]}``。

    一个人可以在几个地方同时说话（群里应付一句、私聊里再吐槽一句），
    所以"说给谁"是逐句的，不能只挂在整轮的 session 上。
    """

    routed_images: dict[str, list[str]] = field(default_factory=dict)
    """同上，按会话分开的图片。"""

    speech_home: str = ""
    """这一轮"她正在跟谁说话"：没写 ``send_to`` 的话（动作文案、续说）默认落这儿。

    动作是"在哪个会话里被安排的就回到哪儿"——私聊里起的做饭，做完那句续说回私聊，
    不会跑到群里去。空 = 就用 ``session_id``。
    """

    def home(self) -> str:
        """默认落点：没有"正在说的会话"时就是这一轮的会话。"""

        return str(self.speech_home or self.session_id)

    def take_messages(self) -> list[str]:
        """取走"还没发出去"的几句话（调用方负责真的发出去）。"""

        taken = [str(item) for item in self.messages if str(item).strip()]
        self.messages = []
        return taken

    def said_messages(self) -> list[str]:
        """这一轮她**说过**的全部话：已经即时发出去的 + 还没发的。"""

        return [*self.live_messages, *self.messages]

    notes: list[str] = field(default_factory=list)
    llm_calls: int = 0
    auto_travel: list[str] = field(default_factory=list)
    """插件替她补的移动目的地（她想去别处做某事时）。"""

    live_messages: list[str] = field(default_factory=list)
    """已经**当场发出去**的那几句（慢动作之前先说的）。收尾时不再重复发。"""

    debug_messages: list[str] = field(default_factory=list)
    """开启「把动作发到群里」时，这些行会作为普通消息发出去（不计入"她说了话"）。"""

    debug_positions: list[int] = field(default_factory=list)
    """与 ``debug_messages`` 一一对应：记下它产生时 ``messages`` 里已经有多少条。

    发送时按这个下标把两类消息重新插回真实顺序——她先调了工具、拿到结果才说话，
    群里也该是这个次序，而不是"话发完再补一句我刚查了天气"。
    """

    echoed_event_ids: set[int] = field(default_factory=set)
    """已经在发生的当下插好位置的事件 id：收尾那次批量回显要跳过它们，不然会重复发。"""

    speech_kind: str = ""
    """这一轮发言的性质（``""`` = 正常回复，``event`` = 事件/求助）。

    事件里的求助不算「她主动找人聊天」：不该吃无人回应保护，也不该把她的冷却期拖长。
    """

    place: str = ""
    """这一轮**发生在哪个会话**（被搭话就是那个群 / 私聊；自主轮留空）。

    一个会话组里的几个群、私聊共用一份状态与一册日志（她的日志挂在组代表会话名下），
    所以要单独记下"这件事发生在哪儿"，不然私聊里说的话看起来像是群里说的。
    """

    images: list[str] = field(default_factory=list)
    """这一轮要发到群里的图片（工具 / 指令生成的结果图）。

    和她的话分开排队：先说内容、再把图贴上去，群里读起来才顺。
    """

    def add_debug(self, line: str) -> None:
        """记一条调试回显，并记住它落在哪两条正式回复之间。"""

        if not str(line).strip():
            return
        self.debug_messages.append(str(line))
        self.debug_positions.append(len(self.messages))

    def add_speech(self, text: str, session_id: str = "") -> None:
        """记一句她要说的：没说给谁就是这一轮的会话，说了就投到那个会话去。

        ``session_id`` 传 ``None`` 表示"她想去的地方根本不存在"：这句话丢掉，
        免得本该私下说的内容当众说出来。
        """

        body = str(text or "").strip()
        if not body:
            return
        if session_id is None:
            return
        target = str(session_id or "").strip()
        if not target or target == str(self.session_id):
            self.messages.append(body)
            return
        self.routed.setdefault(target, []).append(body)

    def add_images(self, images: list[str], session_id: str = "") -> None:
        """按会话分开记图片（和 ``add_speech`` 配套）。"""

        target = str(session_id or "").strip()
        for item in images or []:
            ref = str(item or "").strip()
            if not ref:
                continue
            if not target or target == str(self.session_id):
                if ref not in self.images:
                    self.images.append(ref)
                continue
            bucket = self.routed_images.setdefault(target, [])
            if ref not in bucket:
                bucket.append(ref)

    def ordered_messages(self) -> list[str]:
        """正式回复 + 调试回显，按实际发生的先后排好。"""

        if not self.debug_messages:
            return list(self.messages)
        buckets: dict[int, list[str]] = {}
        for index, line in enumerate(self.debug_messages):
            position = (
                self.debug_positions[index]
                if index < len(self.debug_positions)
                else len(self.messages)
            )
            buckets.setdefault(min(position, len(self.messages)), []).append(line)
        result: list[str] = []
        for slot in range(len(self.messages) + 1):
            result.extend(buckets.get(slot, []))
            if slot < len(self.messages):
                result.append(self.messages[slot])
        return result


@dataclass
class MessageContext:
    """处理一条用户消息需要的上下文。"""

    session_id: str
    user_id: str = ""
    user_name: str = ""
    text: str = ""
    is_wake: bool = False
    is_mentioned: bool = False
    """这条消息是不是真的 @ 了她（或私聊）。

    和 ``is_wake`` 分开是因为：意图路由这类插件会把 ``is_wake`` 置 True
    （它标记的是"这条消息要交给大模型"，不等于"有人在跟她说话"）。
    """
    is_private: bool = False
    persona_id: str = ""
    other_context: str = ""
    is_group_lively: bool = False
    mood_signal: str = ""
    group_name: str = ""
    """平台给的群名（拿不到就是空串）。她名册里的名字优先用编辑器里的备注。"""

    is_soft_wake: bool = False
    """这条消息是**语义上**冲着她说的（意图路由放行），但原话里没人 @ 她。

    路由插件为了让消息重新走通管道会补一个 @，那会让她以为"群里有人点名找我"；
    有这个标记时按"顺着话题对她说"处理。
    """

    image_urls: list[str] = field(default_factory=list)
    """这次要直接交给多模态主模型的图片地址（没配转述模型时才用）。"""

    chat_images: list[str] = field(default_factory=list)
    """这条消息带的图片地址（不管转述成没成功都留着）。

    存进聊天留档用：聊天记录里出现过的图，可以按配置把最近几张直接交给
    多模态主模型看，并在记录里标上「图1 / 图2」方便对照。
    """

    image_marks: dict[str, str] = field(default_factory=dict)
    """这一轮真的附给主模型的聊天记录图片：``地址 -> 图N``（提示词里标「（见图N）」）。"""

    at_targets: list[dict] = field(default_factory=list)
    """这条消息 @ 了谁：``[{"id": "42", "name": "小明", "self": False}]``（含她自己在内）。

    渲染聊天记录时用它写清"这句是冲谁说的"：光看正文分不清别人话里的「你」
    指的是谁，尤其是一条消息同时 @ 了两个人的时候。
    """

    reply_to: dict = field(default_factory=dict)
    """这条消息引用/回复的是谁：``{"id": "42", "name": "小明"}``；没引用就是空的。"""

    addressing: str = ""
    """这句是冲谁说的：``me``（点了她）/ ``others``（在叫别人）/ 空（看不出来）。"""


@dataclass
class SleepReply:
    """她在睡觉时对一条消息的处理结果。

    ``mode``：

    - ``template``：用配置的固定文案回一句（``messages`` 就是要发的内容）；
    - ``silent``：什么都不发，也不要交给主人格。

    （能把她叫醒的消息不会走到这里——那种情况返回 ``None``，交回正常回复路径。）
    """

    mode: str
    messages: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class ReplyOutcome:
    """一次「接管回复」的结果。"""

    ok: bool
    messages: list[str] = field(default_factory=list)
    live_messages: list[str] = field(default_factory=list)
    """已经**当场发出去**的几句（慢动作之前先说的）：调用方别再发一遍。"""

    live_sent: bool = False
    """这一轮是不是已经即时发过话了：收尾那条就不用再引用她的消息。"""

    reasoning: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    debug_messages: list[str] = field(default_factory=list)
    """开启「把动作发到群里」时，决定/动作/工具调用的说明行。"""
    debug_positions: list[int] = field(default_factory=list)
    """与 ``debug_messages`` 一一对应：产生时 ``messages`` 里已有多少条。"""
    tail: str = ""
    """JSON 之后留下的短尾巴（例如别的插件要求模型追加的 `[好感度 持平]`）。

    只交给「回复钩子」那一步，让靠标记工作的插件还能读到；不会跟着她的话发出去。
    """
    error: str = ""

    quote_hint: str = ""
    """这条回复要不要引用触发她的消息：``always`` / ``off``（按配置与合并情况算好）。"""

    images: list[str] = field(default_factory=list)
    """工具 / 指令这一轮生成出来的图片，跟着她的话一起发到群里。"""

    routed: dict[str, list[str]] = field(default_factory=dict)
    """说给**别的会话**的话：``{session_id: [文本…]}``。

    回复里也可以有一句"只想单独跟某人说"，那几句不走 ``messages``（当前会话），
    而是按会话分开放，由发送方逐条投递。
    """

    routed_images: dict[str, list[str]] = field(default_factory=dict)
    """同上，按会话分开的图片。"""

    def routed_sessions(self) -> list[str]:
        """这一轮额外说到了哪些会话。"""

        return [
            str(item)
            for item in list(self.routed or {}) + list(self.routed_images or {})
            if str(item)
        ]

    def ordered_messages(self) -> list[str]:
        """正式回复 + 调试回显，按实际发生的先后排好。"""

        if not self.debug_messages:
            return list(self.messages)
        buckets: dict[int, list[str]] = {}
        for index, line in enumerate(self.debug_messages):
            position = (
                self.debug_positions[index]
                if index < len(self.debug_positions)
                else len(self.messages)
            )
            buckets.setdefault(min(position, len(self.messages)), []).append(line)
        result: list[str] = []
        for slot in range(len(self.messages) + 1):
            result.extend(buckets.get(slot, []))
            if slot < len(self.messages):
                result.append(self.messages[slot])
        return result


_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_DURATION_PATTERN = re.compile(
    r"(?P<num>\d+(?:\.\d+)?|[零一二两三四五六七八九十]+|半)\s*"
    r"(?P<unit>个?\s*小时|个?\s*钟头|分钟|分)"
)

# 日程里读「什么时候」用的两条：相对时间（十分钟后）与口语钟点（晚上八点半）
_RELATIVE_WHEN = re.compile(
    r"(?P<num>\d+|[零一二两三四五六七八九十]+|半)\s*个?\s*"
    r"(?P<unit>小时|钟头|分钟|分)"
)
_SPOKEN_HOUR = re.compile(
    r"(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜)?\s*"
    r"(?P<num>\d{1,2}|[零一二两三四五六七八九十]+)\s*(?:点|点钟)\s*"
    r"(?:(?P<half>半)|(?P<minutes>\d{1,2})\s*分?)?"
)


def _spoken_hour(text: str) -> tuple[int, int] | None:
    """「晚上八点」「下午三点半」→ (时, 分)；读不出来返回 None。"""

    match = _SPOKEN_HOUR.search(str(text or ""))
    if not match:
        return None
    value = _duration_number(match.group("num"))
    if value is None:
        return None
    hour = int(value)
    minutes = 30 if match.group("half") else int(match.group("minutes") or 0)
    period = str(match.group("period") or "")
    if period in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    if not (0 <= hour <= 23) or not (0 <= minutes <= 59):
        return None
    return hour, minutes


def _duration_number(token: str) -> float | None:
    """时长里的数字：阿拉伯数字、汉字数字、还有「半」。"""

    text = str(token or "").strip()
    if not text:
        return None
    if text == "半":
        return 0.5
    try:
        return float(text)
    except ValueError:
        pass
    if text == "十":
        return 10.0
    if text.startswith("十") and len(text) > 1:
        return float(10 + _CN_DIGITS.get(text[1], 0))  # 十二 → 12
    if text.endswith("十") and text[:-1] in _CN_DIGITS:
        return float(_CN_DIGITS[text[:-1]] * 10)  # 二十 → 20
    if len(text) == 1 and text in _CN_DIGITS:
        return float(_CN_DIGITS[text])
    return None


def _guess_duration_seconds(text: str) -> int:
    """从她自己的话里读时长（「小睡三小时」「半小时」「20 分钟」），读不出返回 0。

    模型经常把"睡多久"写在 intent 或台词里，却忘了 duration 字段；这一层只做兜底，
    真正的上下限仍然由动作配置的 duration_min / duration_max 夹住。
    """

    body = str(text or "")
    if not body:
        return 0
    # 「一个半小时 / 一小时半」先单独认，免得被后面的「一小时」抢走
    if re.search(r"(?:一|1)\s*个?\s*半\s*(?:小时|钟头)", body) or re.search(
        r"(?:一|1)\s*个?\s*(?:小时|钟头)\s*半", body
    ):
        return 5400
    for match in _DURATION_PATTERN.finditer(body):
        value = _duration_number(match.group("num"))
        if value is None:
            continue
        unit = match.group("unit")
        per_unit = 3600 if ("小时" in unit or "钟头" in unit) else 60
        seconds = int(round(value * per_unit))
        if seconds > 0:
            return seconds
    return 0


class VirtualWorldEngine:
    """世界状态机。"""

    def __init__(
        self,
        *,
        store: ConfigStore,
        db: AsyncDatabase,
        llm=None,
        helper_llm=None,
        judge_llm=None,
        context_llm=None,
        creator_llm=None,
        event_llm=None,
        consolidate_llm=None,
        describer=None,
        messenger=None,
        tools=None,
        commands=None,
        persona=None,
        clock=None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        decider_interval: float = 300.0,
        debug: bool = False,
        logger=None,
        extensions=None,
    ) -> None:
        self.store = store
        self.db = db
        # 扩展挂载点：没装扩展时就是个空壳（见 core/extensions.py）
        from .extensions import ExtensionHost

        self.extensions = extensions if extensions is not None else ExtensionHost()
        self.llm = llm
        # 辅助模型：只负责"把意图翻译成工具参数"，留空时复用主模型
        self.helper_llm = helper_llm or llm
        # 判断模型：「挑一个 / 打分 / 抽字段」这类纯判断走它；没配就跟随打杂模型。
        # 和打杂分开是有原因的——打杂里混着"要文笔"的活（写事件结局、她求助的话），
        # 那些塞给最小的模型会明显掉质量。
        self.judge_llm = judge_llm or self.helper_llm
        # 上下文压缩模型：只负责把较早的群聊压成摘要
        self.context_llm = context_llm or self.helper_llm
        # 内容生成模型：编事件包、以及编辑器里"批量生成动作 / 地点"
        self.creator_llm = creator_llm or self.llm
        # 事件模型：写"这件事最后怎么样了"。它决定事件读起来尬不尬，
        # 所以单独留一个槽位——想变强就换它，不用连带换掉别的打杂活。
        self.event_llm = event_llm or self.helper_llm
        # 睡眠整理模型：消化记忆、更新画像、写梦（留空时复用辅助模型）
        self.consolidate_llm = consolidate_llm or self.helper_llm
        # 看图：天气工具返回的图也要读出来（插件那边传 AstrBotVision；没有就跳过图片）
        self.describer = describer
        self.messenger = messenger
        self.tools = tools
        # 指令通道：把别家插件的指令转发出去（「指令触发」型动作用）
        self.commands = commands
        self.persona = persona
        self.clock = clock
        self.tick_seconds = max(1.0, float(tick_seconds))
        self.decider_interval = max(5.0, float(decider_interval))
        self.debug = debug
        self.logger = logger

        self.world: WorldConfig
        self.schedules = None
        self.sessions = None
        self.load_warnings: list[str] = []
        self._sessions_index: dict[str, SessionDef] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_decider_at: dict[str, float] = {}
        self._last_presence_at: dict[str, float] = {}
        self._event_writes: dict[str, int] = {}
        self._history_writes: dict[str, int] = {}
        self._event_ids: dict[str, int] = {}
        """每个会话最后写入的事件 id，用来给「把动作发到群里」划一条起跑线。"""
        self._pending_echo: dict[str, list[str]] = {}
        # 平台报上来的群名（拿不到就只用号码 / 编辑器里的备注）
        self._session_names: dict[str, dict[str, Any]] = {}
        """回复路径之外产生的调试回显，等下一次发送时带出去。"""
        self._filled_params: dict[tuple[str, str], dict[str, Any]] = {}
        self.debug_sink = None
        """调试行"立刻发到群里"的通道（由适配层注入；没注入就攒到收尾一起发）。"""

        self.say_sink = None
        """她"先说的那几句"在慢动作之前立刻发出去的通道（同上）。

        没有它的话，「坐好等我两分钟～」要等整段检索跑完，和结果一起冒出来——
        群里看起来就是她半天不吭声、然后一口气说三句。
        """
        """实时调试通道：插件层注入 ``async (session_id, message) -> bool``。

        有它的时候，调试回显在**发生的事情当时**就发出去（不再是整轮跑完才一起发）；
        测试里没有这个通道，就退回"挂到 outcome 上、收尾批量发"的老办法。
        """
        self._echoed_ids: dict[str, set[int]] = {}
        """已经实时发过的事件 id：收尾那次批量回显要跳过，不然会重复。"""
        self._echo_debounce: dict[tuple[str, str, str], float] = {}
        """``(会话, 事件类型, 工具名) -> 上次发的时间``：同一串调用只发第一条。"""
        self._read_cache: dict[str, tuple[float, str]] = {}
        """URL -> (写入时刻, 正文)，抓过的网页短期内不再重复抓。"""

        self._image_captioned: dict[str, float] = {}
        """已经转述过的图（按图片地址记）：同一张图不再转述第二遍。"""

        self._group_lively_at: dict[str, float] = {}
        """每个会话上次结算"群里很热闹"的时刻（给这条脉冲限流）。"""

        self._sleep_mark: dict[str, float] = {}
        """每个会话"睡下的时刻"：睡够一会儿才跑整理（小睡另算）。"""

        self._sleep_passes: dict[str, int] = {}
        """这一段睡眠里已经整理过几次（一段最多两次）。"""

        self._pending_replies: dict[str, list[dict[str, Any]]] = {}
        # 连着被打断的次数（按"她"记，带时间窗）：刷屏时先让她把话说完
        self._interrupt_streak: dict[str, tuple[int, float]] = {}
        """刚进来的消息（可能被合并进正在进行的那次回复）：会话 -> [{text, at, absorbed}]。"""

        self._silent_followup = 0
        """大于 0 时，动作完成后不触发"续说"。

        事件里"先去做点什么"那一轮用它：工具结果只交回给她判断，
        不能顺手把查到的内容发到群里。
        """

        self._tool_failures: dict[str, dict[str, Any]] = {}
        """工具的连续失败次数与退避到期时间（熔断用，只在内存里）。"""
        self._last_llm: dict[str, dict[str, Any]] = {}
        """每个会话最近一次主模型调用的结果（编辑器「模型通道」那行要看）。"""
        self._schedule_signature: list[tuple[Any, ...]] | None = None
        self._schedule_reset_pending = False
        # 一次性日程的过期清理每天只做一遍（值就是做过的那一天）
        self._once_pruned_day = ""
        self._last_tick_at: float = 0.0
        self.rng = random.Random()
        """事件系统的骰子。测试里可以换成固定序列的假随机。"""

        self.miss_rng = random.Random()
        """「想念」下次开闸的随机时长。跟事件骰子分开，免得互相打扰。"""

        self.reload_config()

    # ================= 配置 =================

    # ---------------- 扩展挂载点 ----------------

    def _apply_extension_actions(self, world: Any) -> None:
        """把扩展包注册的动作合进动作库。

        撞名的以原有动作为准（用户自己配过的东西不该被扩展悄悄改掉）；
        扩展给来的定义跑一遍校验，坏的就丢掉并记一条日志。
        """

        raw = self.extensions.actions()
        if not raw or world is None:
            return
        existing = {str(item.id) for item in getattr(world, "actions", []) or []}
        for item in raw:
            try:
                definition = ActionDef.model_validate(dict(item))
            except Exception as exc:
                self._log("warning", f"扩展动作定义不对，已忽略：{exc}")
                continue
            if not definition.id or definition.id in existing:
                continue
            definition.builtin = False
            world.actions.append(definition)
            existing.add(definition.id)

    def _extension_prompt(self, state: WorldState, session_id: str = "") -> str:
        """扩展要加进提示词的那一段（没装扩展就是空串）。"""

        try:
            return self.extensions.prompt_text(state, session_id or state.session_id)
        except Exception:
            return ""

    def _extension_gate(self, definition: ActionDef, state: WorldState) -> str:
        """扩展不让做这件事时给一句原因（空串 = 允许）。"""

        try:
            return self.extensions.gate_reason(definition, state, state.session_id)
        except Exception:
            return ""

    def _hidden_actions(self, state: WorldState, session_id: str = "") -> set[str]:
        """这一轮连名字都不该出现的动作：配额用尽的 + 扩展要求藏起来的。

        只在执行时拦不够——动作名出现在"你能写的 type"清单里就是泄漏。
        """

        picked = set(self.exhausted_actions(state))
        sid = str(session_id or state.session_id or "")
        # 扩展说"这个会话里不让做"的动作，连名字都不该出现——
        # 直接用它的 gate 判一遍，不指望扩展再单独维护一份隐藏名单
        for definition in getattr(self.world, "actions", []) or []:
            try:
                if self.extensions.gate_reason(definition, state, sid):
                    picked.add(str(definition.id))
            except Exception:
                continue
        try:
            picked.update(self.extensions.hidden_actions(state, sid))
        except Exception:
            pass
        return picked

    def reload_config(self) -> list[str]:
        """热加载配置：世界 / 日程 / 会话。"""

        world, schedules, sessions, warnings = self.store.reload()
        # 日程内容变了（改了时间/星期/动作）就把检查游标往回拨一次，
        # 否则「刚把时间改到现在」的日程会被当成已经检查过的过去。
        signature = [
            (
                item.id,
                bool(item.enabled),
                str(item.time),
                tuple(item.days or []),
                tuple(item.sessions or []),
                int(item.priority),
            )
            for item in (schedules.schedules if schedules is not None else [])
        ]
        if self._schedule_signature is not None and signature != self._schedule_signature:
            self._schedule_reset_pending = True
        self._schedule_signature = signature
        # 扩展包带来的动作：合并进动作库（坏的定义直接丢，不影响原动作）
        self._apply_extension_actions(world)
        self.world = world
        self.schedules = schedules
        self.sessions = sessions
        self._sessions_index = {item.session_id: item for item in sessions.sessions}
        # 会话组：几个群 / 私聊算同一个她。这里建两张反查表，
        # 提示词、记忆、聊天上下文都靠它们找到"同组的其它会话"。
        self.session_groups = list(getattr(sessions, "groups", None) or [])
        self._group_index: dict[str, Any] = {
            item.id: item for item in self.session_groups
        }
        self._group_of: dict[str, str] = {}
        for group in self.session_groups:
            for member in group.sessions or []:
                self._group_of[str(member)] = group.id
        self.load_warnings = warnings
        # 配置一改就清掉工具熔断：最常见的"工具坏了"其实是刚装好 / 刚补上 key，
        # 用户保存之后应该立刻能再试，而不是等退避结束。
        self._tool_failures.clear()

        # 情绪两轴需要"现在是几点"和"现实时间"：交给演化器自己取，
        # 免得每个调用点都要把 now / hour 传一遍。
        self.dynamics = StateDynamics(
            world.state_dynamics,
            now_provider=self._now,
            hour_provider=lambda: self.local_now().hour,
        )
        # 记忆按会话各存一份，召回时把同组的会话一起当条件（改组不会把记忆挂错地方）
        self.memory = MemoryEngine(self.db.raw, world, sessions_of=self.group_sessions)
        # 用户画像：一人一份（按"她"存），记忆回答"发生过什么"，它回答"这个人是谁"
        self.profiles = ProfileStore(
            self.db.raw, world, group_of=self.state_key
        )
        # 睡眠整理：消化记忆 + 更新画像 + 写梦（小睡只做轻整理）
        self.consolidator = Consolidator(
            world=world,
            memory=self.memory,
            profiles=self.profiles,
            sessions_of=self.group_sessions,
        )
        self.decider = Decider(
            world,
            now_provider=self._now,
            # 规则要知道"现在几点"：夜里该睡整觉，白天只小睡
            hour_provider=lambda: self.local_now().hour,
            tick_seconds=self.tick_seconds,
        )
        self.engagement = EngagementTracker(world)
        self.prompts = PromptBuilder(
            world, tick_seconds=self.tick_seconds, now_provider=self.local_now
        )
        if getattr(self, "consolidator", None) is not None:
            self.consolidator.prompts = self.prompts
            self.consolidator.set_world(world)
        return warnings

    # ================= 会话 =================

    def is_enabled(self, session_id: str) -> bool:
        if not session_id:
            return False
        session = self._sessions_index.get(session_id)
        if session is None or not session.enabled:
            return False
        blocked = set(self.world.content_safety.session_blocklist or [])
        return session_id not in blocked

    def enabled_session_ids(self) -> list[str]:
        return [item.session_id for item in self.sessions.sessions if self.is_enabled(item.session_id)]

    def session_config(self, session_id: str) -> SessionDef | None:
        return self._sessions_index.get(session_id)

    # ---------------- 会话组 ----------------

    def group_of(self, session_id: str) -> Any | None:
        """这个会话属于哪个组（没分组返回 None）。"""

        group_id = self._group_of.get(str(session_id or ""))
        return self._group_index.get(group_id) if group_id else None

    def member_sessions(self, session_id: str) -> list[str]:
        """同组的其它会话（不含自己）。"""

        group = self.group_of(session_id)
        if group is None:
            return []
        return [str(item) for item in (group.sessions or []) if str(item) != str(session_id)]

    def group_by_id(self, group_id: str) -> Any | None:
        """按组 id 找组（编辑器选的可能就是组）。"""

        return self._group_index.get(str(group_id or ""))

    def scope_session(self, value: str) -> str:
        """编辑器里的选择换成"真正要操作的会话"。

        选的可能是会话、也可能是会话组；是组就落到它的代表会话上
        （她的状态、日志、日程都挂在那儿）。
        """

        text = str(value or "").strip()
        if not text:
            return ""
        if text in self._group_index:
            group = self._group_index[text]
            members = [str(item) for item in (group.sessions or [])]
            if not members:
                return text
            main = str(group.main_session or members[0])
            if self.is_enabled(main):
                return main
            for member in members:
                if self.is_enabled(member):
                    return member
            return main
        return text

    def group_sessions(self, session_id: str) -> list[str]:
        """同组的全部会话（含自己；没分组就只有自己）。"""

        group = self.group_of(session_id)
        if group is None:
            return [str(session_id)]
        members = [str(item) for item in (group.sessions or [])]
        return members or [str(session_id)]

    def scope_id(self, session_id: str) -> str:
        """共享作用域的 id：分过组就是组 id，记忆和聊天上下文都按它存。"""

        group = self.group_of(session_id)
        return str(group.id) if group is not None else str(session_id)

    def main_session(self, session_id: str) -> str:
        """她这一组的"主会话"；没分组就是它自己。"""

        group = self.group_of(session_id)
        if group is None:
            return str(session_id)
        return str(group.main_session or session_id)

    def state_key(self, session_id: str) -> str:
        """她的状态存在哪个会话名下。

        分过组就是同一个她：位置、数值、计划、事件都存一份，几个群 / 私聊共用。
        主会话要是被停用了，就退到组里第一个可用的成员，免得她"住在"一个不响应的会话里。
        """

        group = self.group_of(session_id)
        if group is None:
            return str(session_id)
        main = str(group.main_session or session_id)
        if self.is_enabled(main):
            return main
        for member in group.sessions or []:
            if self.is_enabled(str(member)):
                return str(member)
        return main

    def tick_session_ids(self) -> list[str]:
        """真正要推进时钟的会话：同一个她只推一次（一个组一份）。"""

        picked: list[str] = []
        seen: set[str] = set()
        for session_id in self.enabled_session_ids():
            key = self.state_key(session_id)
            if key in seen:
                continue
            seen.add(key)
            picked.append(key)
        return picked

    def schedule_target(self, schedule: Any, session_id: str) -> str:
        """这条日程落在哪个会话里说。

        ``sessions`` 从"在哪几个会话触发"变成"落点"：勾了私聊，她的日程就发私聊；
        勾了会话组，就落在那一组的代表会话里；勾多个时优先当前会话，其次列表里的第一个。
        """

        picked = [str(item) for item in (getattr(schedule, "sessions", None) or [])]
        if not picked:
            return str(session_id)
        group = self.group_of(session_id)
        if group is None:
            return str(session_id)
        members = {str(member) for member in (group.sessions or [])}
        if str(group.id) in picked:
            # 勾的是这一组：落在组代表（也就是这一轮的会话）
            return str(session_id)
        allowed = [item for item in picked if item in members]
        if not allowed:
            return str(session_id)
        if str(session_id) in allowed:
            return str(session_id)
        return allowed[0]

    def schedule_in_scope(self, schedule: Any, session_id: str) -> bool:
        """这条日程属不属于"这个她"。

        勾了会话组就按组算；勾了具体会话就按组里的成员算；什么都没勾 = 每个她都要跑。
        """

        picked = [str(item) for item in (getattr(schedule, "sessions", None) or [])]
        if not picked:
            return True
        group = self.group_of(session_id)
        if group is None:
            return str(session_id) in picked
        if str(group.id) in picked:
            return True
        return bool({str(member) for member in (group.sessions or [])}.intersection(picked))

    def session_note(self, session_id: str) -> str:
        """这个会话的备注：有备注就用备注，不看群名。"""

        session = self.session_config(session_id)
        return " ".join(str(getattr(session, "note", "") or "").split())

    def session_group_name(self, session_id: str) -> str:
        """平台给的群名（拿不到就是空串）。"""

        info = (getattr(self, "_session_names", None) or {}).get(str(session_id)) or {}
        return " ".join(str(info.get("name") or "").split())

    def note_session_name(self, session_id: str, name: str) -> None:
        """把平台给的群名记下来（只在拿到时调）。"""

        text = " ".join(str(name or "").split())[:40]
        if not text:
            return
        self._session_names = getattr(self, "_session_names", {}) or {}
        self._session_names[str(session_id)] = {"name": text}

    def session_display_name(
        self, session_id: str, state: WorldState | None = None
    ) -> str:
        """她在提示词里看到的会话名：备注 > 群名 > 最近说话的人 > 只有号码。

        私聊通常没有群名，就用"最近在这儿跟她说话的那个人"当名字——
        这样通讯录里写的是「私聊 2692047521「主人」」而不是一串数字。
        """

        note = self.session_note(session_id)
        if note:
            return note
        group_name = self.session_group_name(session_id)
        if group_name:
            return group_name
        if state is not None:
            info = (getattr(state, "session_activity", None) or {}).get(
                str(session_id)
            ) or {}
            who = " ".join(str(info.get("user_name") or "").split())
            if who:
                return who[:12]
        return ""

    def session_label(self, session_id: str, state: WorldState | None = None) -> str:
        """通讯录里的一行标题：``群 123456「小米粥群」`` 这种。"""

        session = self.session_config(session_id)
        kind = "私聊" if session is not None and session.type == "private" else "群"
        number = str(session_id or "").split(":")[-1] or str(session_id or "")
        name = self.session_display_name(session_id, state)
        return f"{kind} {number}「{name}」" if name else f"{kind} {number}"

    def session_directory(self, state: WorldState) -> str:
        """她能说话的地方：每条带号码、名字、最近有没有人跟她说话。"""

        group = self.group_of(state.session_id)
        if group is None:
            # 只有一个地方就没什么可挑的：不往提示词里加这段
            return ""
        members = [str(item) for item in (group.sessions or [])] if group else [
            str(state.session_id)
        ]
        if not members:
            members = [str(state.session_id)]
        now = self._now()
        activity = dict(getattr(state, "session_activity", None) or {})
        lines: list[str] = []
        for session_id in members:
            if not self.is_enabled(session_id):
                continue
            info = activity.get(session_id) or {}
            at = float(info.get("at") or 0.0)
            who = " ".join(str(info.get("user_name") or "").split())[:12]
            if at <= 0:
                when = "还没有人跟你说过话"
            else:
                minutes = max(0, int((now - at) // 60))
                when = (
                    f"{who or '有人'}最近跟你说话是 "
                    + ("刚刚" if minutes < 1 else f"{minutes} 分钟前" if minutes < 60 else f"{minutes // 60} 小时前")
                )
            lines.append(f"- {self.session_label(session_id, state)}：{when}")
        if not lines:
            return ""
        return (
            "# 你能说话的地方\n"
            + f"你现在在【{self.session_label(state.session_id, state)}】里说话。\n"
            + "\n".join(lines)
            + "\n这些都是你，只是说话的地方不同。除了回别人的话（在哪儿被问就在哪儿答），"
            "你想主动说什么就说给谁；下面的时间只是给你参考，怎么挑由你自己决定。"
            "**位置、心情、日程、记忆、你和每个人的关系只有一份**，换个地方说话不会变成另一个你。"
            "同一件事可以一次给两处各写一句（比如群里应付一句、私聊里再吐槽一句）——"
            "把两条 say 分别写上 send_to 就行。\n"
            "**上面这份名单就是你能说话的全部地方**：别人让你私聊他、加好友、"
            "或者去别的地方找他，而那人不在名单里，你就是过不去——那就别答应，"
            "用你自己的口气说明白（「本小姐就在这儿待着，要找我过来找」那种），"
            "别说「我私聊你」，也别把本来想私下说的话发在群里。"
        )

    def resolve_send_to(self, state: WorldState, value: Any, *, fallback: str) -> str:
        """把她写的落点（会话 id / 群号 / 昵称 / 备注）对上真正的会话。"""

        text = " ".join(str(value or "").split()).strip()
        if not text:
            return fallback
        group = self.group_of(state.session_id)
        members = [str(item) for item in (group.sessions or [])] if group else [
            str(state.session_id)
        ]
        for session_id in members:
            if not self.is_enabled(session_id):
                continue
            if self.session_ref_matches(session_id, text):
                return str(session_id)
        return fallback

    def session_ref_matches(self, session_id: str, text: str) -> bool:
        """她写的一串字能不能指到这个会话（会话 id / 号码 / 备注 / 群名）。"""

        lowered = " ".join(str(text or "").split()).lower()
        if not lowered:
            return False
        number = str(session_id).split(":")[-1]
        names = [self.session_note(session_id), self.session_group_name(session_id)]
        for candidate in [session_id, number, *names]:
            if candidate and str(candidate).lower() in lowered:
                return True
        return False

    def resolve_session_refs(
        self, values: Any, *, session_id: str = ""
    ) -> list[str]:
        """把一组"落点"说法换成会话 id / 会话组 id（日程用）。"""

        refs = [str(session_id)] if session_id else list(self.enabled_session_ids())
        picked: list[str] = []
        for raw_value in list(values or []):
            text = " ".join(str(raw_value or "").split())
            if not text:
                continue
            if self.group_by_id(text) is not None:
                if text not in picked:
                    picked.append(text)
                continue
            for ref in refs:
                group = self.group_of(ref)
                members = (
                    [str(item) for item in (group.sessions or [])]
                    if group
                    else [str(ref)]
                )
                hit = next(
                    (item for item in members if self.session_ref_matches(item, text)),
                    "",
                )
                if hit and hit not in picked:
                    picked.append(hit)
                if hit:
                    break
        return picked

    def default_node_id(self) -> str:
        return self.world.default_node_id()

    def node(self, node_id: str) -> NodeDef | None:
        return self.world.node_map().get(node_id)

    # ================= 状态存取 =================

    def lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def is_busy(self, session_id: str) -> bool:
        """她现在是不是正忙（有人在跑这个会话的动作/检索/模型调用）。

        只看会话锁，**不排队**：编辑器点「推进 tick」时用它做前置判断——
        忙着就直接跳过这一次，免得排队到动作结束后一口气推进好几个 tick。
        """

        # 锁挂在组代表会话上：按成员会话来问也要落到同一把锁
        lock = self._locks.get(self.state_key(session_id))
        return bool(lock is not None and lock.locked())

    async def load_state(self, session_id: str, *, cold_start: bool = True) -> WorldState:
        # 分过组的会话共用组代表会话的那一份：别的地方按成员会话来读时
        # 也要落到同一份上，不然会读到一份空壳。
        key = self.state_key(session_id)
        payload = await self.db.call("get_state", key)
        if payload is None:
            adopted = await self._adopt_group_state(key)
            if adopted is not None:
                return adopted
            return await self._cold_start(key) if cold_start else self._default_state(key)
        return WorldState.from_payload(payload, key)

    async def _adopt_group_state(self, key: str) -> WorldState | None:
        """刚把几个会话编成一组时，接过她原来那份状态。

        老数据是每个会话各存一份；合成一个她以后只认组代表那一份。
        代表名下还没有状态时，就挑组里"活得最久"的那份接着用，
        免得她一下回到刚出生、位置和数值全清零。
        """

        group = self.group_of(key)
        if group is None:
            return None
        best: WorldState | None = None
        for member in group.sessions or []:
            session_id = str(member)
            if session_id == str(key):
                continue
            try:
                payload = await self.db.call("get_state", session_id)
            except Exception:
                continue
            if payload is None:
                continue
            state = WorldState.from_payload(payload, key)
            if best is None or int(state.world_time) > int(best.world_time):
                best = state
        return best

    def _default_state(self, session_id: str) -> WorldState:
        defaults = self.world.default_state
        session = self.session_config(session_id)
        node_id = (
            session.cold_start_node
            if session and session.cold_start_node in self.world.node_map()
            else self.default_node_id()
        )
        state = WorldState(
            session_id=session_id,
            node_id=node_id,
            mood=defaults.mood,
            energy=defaults.energy,
            loneliness=defaults.loneliness,
            curiosity=defaults.curiosity,
            affect=defaults.affect,
            boredom=defaults.boredom,
            desire=defaults.desire,
            abilities=dict(self.world.abilities.initial),
            node_since=self._now(),
        )
        state.clamp()
        return state

    async def _cold_start(self, session_id: str) -> WorldState:
        """冷启动：生成初始状态与预设记忆。"""

        state = self._default_state(session_id)
        session = self.session_config(session_id)
        mode: ColdStartMode = session.cold_start_mode if session else "awakening"
        if mode == "awakening":
            state.state = STATE_AWAKENING
        elif mode == "silent":
            state.state = STATE_IDLE
        else:
            state.state = STATE_AWAKENING
        state.cold_start_done = False
        state.add_event("cold_start", {"mode": mode, "node": state.node_id})
        await self._seed_preset_memories(state)
        await self.save_state(state)
        return state

    async def _seed_preset_memories(self, state: WorldState) -> None:
        persona_id = ""
        for node in self.world.nodes:
            for preset in node.preset_memories:
                self.memory.remember(
                    session_id=state.session_id,
                    persona_id=persona_id,
                    node_id=node.id,
                    content=preset.content,
                    memory_type=SCENE,
                    emotion=preset.emotion,
                    weight=preset.weight,
                    scope=preset.scope,
                    source="preset",
                )

    async def save_state(self, state: WorldState) -> None:
        state.updated_at = time.time()
        state.clamp()
        await self.db.call("save_state", state.session_id, state.to_payload(), state.updated_at)

    @contextlib.asynccontextmanager
    async def session_state(self, session_id: str):
        """加锁 -> 加载 -> 交给调用方修改 -> 保存 -> 解锁。

        分过组的会话共用同一份状态（存在组代表会话名下）：几个群 / 私聊里的
        都是同一个她，位置、数值、计划、事件只有一份。
        """

        key = self.state_key(session_id)
        async with self.lock(key):
            state = await self.load_state(key)
            # 留档超上限时要不要先把顶掉的几条攒起来（配置成"压成摘要"才需要）：
            # 挂成临时属性，note_chat 会读它——不写进存档。
            state.buffer_dropped_chat = self._buffer_dropped_chat()
            try:
                yield state
            finally:
                await self.save_state(state)

    def _buffer_dropped_chat(self) -> bool:
        """被上限顶掉的群聊要不要先攒着等压缩（"直接丢弃"就没必要留）。"""

        return str(getattr(self.world.context, "chat_overflow", "discard")) == "compress"

    # ================= 事件系统 =================
    #
    # 一条线索（thread）是一串事件：初始事件 → 她怎么选 → 判定 → 结果 →（可能还有下一步）。
    # 生成和结算走打杂 / 生成器模型，只有「她怎么选」用主模型——那是角色性最强的一步。
    # 判定是纯计算，不调模型：所以这四个步骤都写得进日志，也都能单独测。

    def abilities_line(self, state: WorldState) -> str:
        """能力值那一行（人话，不是数字）。按需注入：关掉能力值就整段不出现。"""

        config = self.world.abilities
        if not bool(getattr(config, "enabled", True)):
            return ""
        values = normalize_abilities(state.abilities or config.initial)
        parts = [
            f"{ABILITY_LABELS[name]}{ability_hint(values[name])}"
            for name in ABILITIES
        ]
        return (
            "# 你自己的本事\n"
            + "、".join(parts)
            + "（这是你平时的手感，用来判断一件事对你难不难；不用说出来）"
        )

    def pending_event_line(self, state: WorldState) -> str:
        """心里挂着的事：等待协助、或者手头还有没完的线索。"""

        info = state.pending_help or {}
        stage = str(info.get("state") or "")
        title = str(info.get("title") or "那件事")
        if stage == "active":
            return (
                "# 你心里挂着的事\n"
                f"你刚开口找人拿主意（{title}），还在等人回话。你可以照常做别的事、"
                "照常接别人聊的话题；对方可能一直不回，等不到就自己拿主意。"
            )
        if stage == "idle":
            return (
                "# 你心里挂着的事\n"
                f"{title} 这件事还没解决，但你已经不催了。有人提起就接一句，"
                "没人提就先放着，别反复提。"
            )
        open_ones = [item for item in open_threads(state.event_threads) if item.get("pending_followup")]
        if open_ones:
            lines = [f"- {item.get('title') or '一件事'}：{item.get('pending_followup')}" for item in open_ones[:2]]
            return "# 手头没完的事\n" + "\n".join(lines)
        return ""

    def autonomous_prompt_note(self, state: WorldState) -> str:
        """自主开口时那句"刚才的情况"：被冷落了就别追着说。

        以前这句话只出现在系统提示词里，位置靠后、又和一堆规则混在一起，
        模型经常当没看见。现在把它挪到用户提示词里，紧跟"现在该你说话了"。
        """

        hint = self.engagement.hint(state)
        if not hint:
            return ""
        return (
            "# 刚才的情况\n"
            f"{hint}\n"
            "（没人接的时候，下一轮**大概率就别追着说了**——除非这件事真的需要有人回应。"
            "真想继续说，就说你自己身上发生的事，别对着空气追问。）"
        )

    def _ensure_abilities(self, state: WorldState) -> dict[str, float]:
        """能力值兜底：新会话按配置初始化，老存档缺项按默认补齐。"""

        if not state.abilities:
            config = self.world.abilities
            state.abilities = {
                name: float(getattr(config, name, 0.6) or 0.6) for name in ABILITIES
            }
        state.abilities = normalize_abilities(state.abilities)
        return state.abilities

    def _event_time(self) -> float:
        return self._now()

    def _interrupt_pending(self) -> bool:
        """她还在生成回复时又来了一条：是**丢弃这次生成**（默认）还是并成一次回。

        开关在「说话节奏」里（``interrupt_pending``）：打开 = 新消息打断旧的，
        被丢弃的那次不算"回过话"，所以水位线不动、新那一轮仍然看得到完整上下文。
        """

        style = getattr(self.world, "reply_style", None)
        return bool(getattr(style, "interrupt_pending", True))

    def _event_gap_reason(self, state: WorldState, now: float) -> str:
        """两件事之间的最小间隔（``min_gap_minutes``）：没到点就先别出事。"""

        minutes = max(0, int(getattr(self.world.events, "min_gap_minutes", 30) or 0))
        if minutes <= 0:
            return ""
        last = float(state.last_event_started_at or 0.0)
        if last <= 0:
            return ""
        left = minutes * 60 - (float(now) - last)
        if left <= 0:
            return ""
        return f"距上一件事还不到 {minutes} 分钟"

    def _stay_up_penalty(self, state: WorldState) -> float:
        """熬夜代价的倍数：**夜里醒着**、或**为事件推迟了睡觉**，取较大值不叠加。

        睡整觉、小睡都算休息，不算熬夜；夜里时段和倍数分别在
        「作息与夜晚」和「事件」里配。
        """

        penalty = float(getattr(self.world.events, "stay_up_penalty", 1.0) or 1.0)
        if penalty <= 1.0:
            return 1.0
        awake = str(state.state or "") not in (STATE_SLEEPING, STATE_NAPPING)
        if awake and self.world.is_night_time(self.local_now().hour):
            return penalty
        if state.stay_up_until and state.world_time <= int(state.stay_up_until):
            return penalty
        return 1.0

    @staticmethod
    def _today_key(now: float) -> str:
        try:
            return datetime.fromtimestamp(float(now)).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""

    def _roll_day_mood(self, state: WorldState, now: float) -> bool:
        """每天掷一次"今天的基调"，同一天只掷一次。

        掷的是**曲线快慢**，不是性格：今天懒散就精力掉得慢、坐得住，
        今天黏人就更容易觉得孤单。她不会主动解释这件事，但状态页和提示词里
        都写着，免得"为什么她今天怪怪的"只能靠猜。

        返回 True 表示这一次刚好掷出了新的一天（用来记一条日志）。
        """

        if not bool(getattr(self.world.state_dynamics, "daily_mood_enabled", True)):
            state.day_mood = ""
            state.day_mood_day = ""
            return False
        day = self._today_key(now)
        if not day:
            return False
        if str(state.day_mood_day or "") == day and str(state.day_mood or ""):
            return False
        state.day_mood = roll_day_mood(self.rng)
        state.day_mood_day = day
        return True

    def _event_block_reason(
        self, state: WorldState, now: float, *, manual: bool = False
    ) -> str:
        """现在能不能出事？能就返回空串，不能就返回一句**具体**的原因。

        ``manual=True`` 是用户直接投递（指令 / 编辑器按钮）：这种情况不受「在同一个地方
        待够几分钟」的限制——那是给自动掷骰用的门槛，人都点名要一件事了就别拿它挡。
        """

        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return "事件系统在全局设置里关着"
        if self._is_asleep(state) and not bool(getattr(config, "in_sleep", False)):
            return "她正在睡觉（想要梦里的剧情可以打开「睡觉时也出事」）"
        if state.state == STATE_WALKING:
            return "她正在走路"
        current = state.current_action or {}
        if current:
            if str(current.get("type") or "") == "walk_to":
                return "她正在走路"
            label = str(current.get("desc") or "").strip()
            definition = self.world.action_map().get(str(current.get("type") or ""))
            name = label or (definition.name if definition is not None else "") or str(
                current.get("type") or "手上的事"
            )
            return f"她正在「{name}」"
        if manual:
            return ""
        # 自动掷骰：得在这个地方待够一段时间，才算"在这儿生活"
        dwell = max(0, int(getattr(config, "dwell_minutes", 5) or 0)) * 60.0
        since = float(state.node_since or 0.0)
        if since <= 0:
            state.node_since = float(now)
            return "她刚到这个世界，先让她缓一会儿"
        waited = float(now) - since
        if waited < dwell:
            left = max(1, int(round((dwell - waited) / 60.0)))
            return f"她刚到这里还不到 {max(1, int(getattr(config, 'dwell_minutes', 5) or 0))} 分钟（还差约 {left} 分钟）"
        return ""

    def _event_allowed(self, state: WorldState, now: float) -> bool:
        """现在这一刻允许自动发生事件吗。"""

        return not self._event_block_reason(state, now)

    def _event_roll_gap_hours(self, state: WorldState, now: float) -> float:
        """距上次掷骰过了多久（小时），并把时钟推到现在。

        关键是**每个 tick 都推进**：以前只在"能掷骰"的时候推，睡觉 / 忙一件长动作
        期间会攒下好几个小时的"欠账"，醒来那一 tick 的概率被顶到接近 100%，
        看起来就像"刚醒就必出事"。现在过去的就让它过去。
        """

        last = float(state.event_last_roll_at or 0.0)
        state.event_last_roll_at = float(now)
        if last <= 0:
            return 0.0
        gap = max(0.0, (float(now) - last) / 3600.0)
        return min(gap, EVENT_ROLL_MAX_GAP_HOURS)

    def _roll_event_tier(
        self, state: WorldState, now: float, gap_hours: float
    ) -> tuple[str, dict[str, float]]:
        """掷骰：这一刻要不要出事、出哪一档。

        用"每小时期望次数"折算成这一段时间的概率（泊松近似），不是固定间隔——
        固定间隔会带上整点感，一眼就看出是定时器。
        """

        config = self.world.events
        dt_hours = max(0.0, float(gap_hours))
        info = {"gap_hours": round(dt_hours, 4), "chance": 0.0, "roll": 0.0}
        if dt_hours <= 0:
            return "", info
        rates = {
            "micro": max(0.0, float(getattr(config, "micro_per_hour", 1.0) or 0.0)),
            "small": max(0.0, float(getattr(config, "small_per_hour", 0.3) or 0.0)),
            "big": max(0.0, float(getattr(config, "big_per_hour", 0.02) or 0.0)),
        }
        total = sum(rates.values())
        if total <= 0:
            return "", info
        chance = min(1.0, total * dt_hours)
        roll = self.rng.random()
        info = {"gap_hours": round(dt_hours, 4), "chance": round(chance, 4), "roll": round(roll, 4)}
        if roll >= chance:
            return "", info
        point = self.rng.random() * total
        chosen = ""
        for tier in ("micro", "small", "big"):
            if rates[tier] <= 0:
                continue
            chosen = tier
            point -= rates[tier]
            if point <= 0:
                return tier, info
        return chosen, info

    def _pick_genre(self, state: WorldState) -> dict[str, Any] | None:
        """按权重抽一个事件题材（最近用过的排除，用过的还要过冷却期）。

        权重必须由代码掷：模型每次调用都是独立采样，告诉它"按 50/20/30 分配"它也不会遵守，
        只会挑自己偏好的那类写——结果就是永远只有"温馨小事"。
        """

        config = self.world.events
        entries = [
            item
            for item in list(getattr(config, "genres", []) or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        weighted: list[tuple[dict[str, Any], float]] = []
        for item in entries:
            try:
                weight = float(item.get("weight") or 0.0)
            except (TypeError, ValueError):
                weight = 0.0
            if weight > 0:
                weighted.append((item, weight))
        if not weighted:
            return None
        recent_keep = max(0, int(getattr(config, "genre_recency", 4) or 0))
        recent = {str(name) for name in list(state.event_recent_genres or [])[-recent_keep:]} if recent_keep else set()
        # 题材冷却：权重 40% 的「日常小事」光靠"排除最近 N 条"压不住，
        # 隔一件就又回来了。冷却期内直接不给权重，剩下的按原比例分。
        cooldown = max(0.0, float(getattr(config, "genre_cooldown_minutes", 0) or 0)) * 60.0
        now = self._now()
        used_at = dict(getattr(state, "event_genre_at", None) or {})

        def blocked(item: dict[str, Any]) -> bool:
            name = str(item.get("name") or "")
            if name in recent:
                return True
            if cooldown <= 0:
                return False
            stamp = float(used_at.get(name) or 0.0)
            return stamp > 0 and (now - stamp) < cooldown

        pool = [item for item in weighted if not blocked(item[0])] or weighted
        total = sum(weight for _item, weight in pool)
        point = self.rng.random() * total
        for item, weight in pool:
            point -= weight
            if point <= 0:
                return item
        return pool[-1][0]

    def _active_thread(self, state: WorldState) -> dict[str, Any] | None:
        """正在进行中的那件事（同一时间只允许一件）。

        优先返回"她正在等群友拿主意"的那条；否则返回最近开的那条未完结线索。
        """

        info = state.pending_help or {}
        if str(info.get("state") or "") in ("active", "idle"):
            found = self._thread_by_id(state, str(info.get("thread_id") or ""))
            if found is not None:
                return found
        opened = open_threads(state.event_threads)
        if not opened:
            return None
        return max(opened, key=lambda item: float(item.get("opened_at") or 0.0))

    def _thread_critical(self, thread: dict[str, Any] | None) -> bool:
        """这条事件是不是"危险 / 紧急"（日程要让路）。"""

        root = (thread or {}).get("root") or {}
        return bool(root.get("critical"))

    def _event_place(self, package: EventPackage, state: WorldState) -> tuple[str, str]:
        """这件事发生在哪：``(记忆用的地点键, 人话名字)``。

        世界里的节点事件（锅盖卡死→厨房）用节点 id；地图外的事（逛街、坐车）用
        ``outside:场景名``——**不能拿她的物理位置顶替**，否则"在窗边"会把逛街的事记进窗边，
        以后按地点根本搜不到。
        """

        node_map = self.world.node_map()
        raw = str(package.place or "").strip()
        if raw and raw in node_map:
            node = node_map[raw]
            return raw, str(package.place_name or node.name or raw)
        # 生成器给的是人话地名时，先把「外面·商业街」这种拆出来找节点
        for key, node in node_map.items():
            name = str(node.name or "")
            if raw and (raw == name or raw == key):
                return key, name or key
            if package.place_name and package.place_name == name:
                return key, name or key
        external = str(package.place_name or raw or package.scene or "").strip()
        if external:
            return f"outside:{external}", external
        # 都没有就退回她此刻所在的地方（老事件 / 池子里的条目）
        node = node_map.get(state.node_id)
        return state.node_id, str(node.name if node is not None else state.node_id)

    def _event_place_line(self, thread: dict[str, Any] | None) -> str:
        """这件事发生在哪（给提示词里那一行用）。"""

        item = thread or {}
        root = item.get("root") or {}
        for source in (root, item):
            name = str(source.get("place_name") or "").strip()
            if name:
                return name
            place = str(source.get("place") or "").strip()
            if place:
                return place.split(":", 1)[-1]
        return ""

    def _journal_time_text(self, at: float, now: float) -> str:
        """她自己的账里的时间标注：今天/昨天/前天 + HH:MM，更早写日期。"""

        try:
            moment = datetime.fromtimestamp(float(at))
            today = datetime.fromtimestamp(float(now))
        except (OverflowError, OSError, ValueError):
            return ""
        days = (today.date() - moment.date()).days
        clock = moment.strftime("%H:%M")
        if days <= 0:
            return f"今天 {clock}"
        if days == 1:
            return f"昨天 {clock}"
        if days == 2:
            return f"前天 {clock}"
        return f"{moment.strftime('%m-%d')} {clock}"

    def event_journal(self, state: WorldState, now: float | None = None) -> str:
        """她自己的账（三段），直接进提示词。

        - **我正在经历**：当前那一件事的详细处境；
        - **最近发生在我身上的事**：窗口内的全部事件，按时间从早到晚、每条一行带时间；
        - **有件事我心里还挂着**：挂起中的那件事，一行轻提示。

        末尾再补一段「你排好的日程」：每条写清是什么、上次什么时候跑的、
        下次大概什么时候（一次性日程没有"下次"）。
        """

        moment = float(now if now is not None else self._now())
        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return ""
        window = max(0.0, float(getattr(config, "recent_window_hours", 24.0) or 0.0)) * 3600.0
        limit = max(0, int(getattr(config, "recent_max_lines", 8) or 0))
        threads = [item for item in list(state.event_threads or []) if isinstance(item, dict)]
        active = self._active_thread(state)
        suspended = active is not None and self._thread_suspended(state, active, moment)
        detailed = active if (active is not None and not suspended) else None
        detailed_id = str((detailed or {}).get("id") or "")

        recent: list[dict[str, Any]] = []
        for item in threads:
            if str(item.get("id") or "") == detailed_id:
                continue
            stamp = float(item.get("closed_at") or item.get("updated_at") or item.get("opened_at") or 0.0)
            if window and moment - stamp > window:
                continue
            recent.append(item)
        recent.sort(key=lambda item: float(item.get("updated_at") or item.get("opened_at") or 0.0))
        # 条数超了：先顶掉微事件（最旧的），再顶最旧的
        if limit and len(recent) > limit:
            drop = len(recent) - limit
            light = [item for item in recent if str(item.get("tier") or "") == TIER_MICRO]
            dropped: list[str] = []
            for item in light:
                if len(dropped) >= drop:
                    break
                dropped.append(str(item.get("id") or ""))
            for item in recent:
                if len(dropped) >= drop:
                    break
                key = str(item.get("id") or "")
                if key not in dropped:
                    dropped.append(key)
            recent = [item for item in recent if str(item.get("id") or "") not in dropped]

        blocks: list[str] = []
        if detailed is not None:
            head = f"# 我正在经历（{self._journal_time_text(float(detailed.get('opened_at') or moment), moment)}"
            place = self._event_place_line(detailed)
            if place:
                head += f" · {place}"
            head += "）"
            lines = [f"{detailed.get('title') or '一件事'}：{str((detailed.get('root') or {}).get('hook') or '')}"]
            for step in thread_step_lines(detailed)[-3:]:
                lines.append(f"- {step}")
            pend = str(detailed.get("pending_followup") or "").strip()
            if pend:
                lines.append(f"- 还没完：{pend}")
            blocks.append(head + "\n" + "\n".join(lines))
        if recent:
            rows = []
            for item in recent:
                stamp = float(item.get("updated_at") or item.get("opened_at") or 0.0)
                when = self._journal_time_text(stamp, moment)
                place = self._event_place_line(item)
                title = str(item.get("title") or "").strip()
                text = str(item.get("line") or "").strip()
                if title and text and title not in text:
                    text = f"{title}：{text}"
                elif not text:
                    text = title or "一件事"
                rows.append(f"- {when}　{'（' + place + '）' if place else ''}{text}")
            blocks.append(
                "# 最近发生在我身上的事（按时间从早到晚，都是**你自己经历**的）\n"
                + "\n".join(rows)
                + "\n（提到时间时说人话：今天 / 昨天 / 前几天，别报数字）"
            )
        open_ones = [
            item
            for item in threads
            if str(item.get("status") or "open") == "open"
            and str(item.get("id") or "") != detailed_id
            and bool(getattr(config, "open_thread_hint", True))
            and (not window or moment - float(item.get("updated_at") or item.get("opened_at") or 0.0) <= window)
        ]
        if open_ones:
            rows = []
            for item in open_ones[:3]:
                place = self._event_place_line(item)
                head = str(item.get("title") or "一件事")
                if place:
                    head += f"（{place}）"
                note = str(item.get("pending_followup") or "").strip() or "还没弄清楚"
                rows.append(f"- {head}：{note}")
            blocks.append("# 有件事我心里还挂着（不用现在提，别反复念叨）\n" + "\n".join(rows))

        plan = self.schedule_journal(state, moment)
        if plan:
            blocks.append(plan)
        return "\n\n".join(blocks)


    def schedule_journal(self, state: WorldState, now: float | None = None) -> str:
        """「你排好的日程」：是什么、上次什么时候跑的、下次什么时候（一次性没有“下次”）。"""

        items = [
            item
            for item in list(getattr(self.schedules, "schedules", None) or [])
            if bool(getattr(item, "enabled", True))
        ]
        if not items:
            return ""
        moment = datetime.fromtimestamp(float(now if now is not None else self._now()))
        last_map = dict(getattr(state, "schedule_last_fired", None) or {})
        rows: list[str] = []
        for item in items:
            bits: list[str] = []
            note = str(getattr(item, "note", "") or "").strip()
            when = str(getattr(item, "time", "") or "").strip()
            once = bool(getattr(item, "once", False))
            days = [str(day) for day in (getattr(item, "days", None) or [])]
            if once:
                date = str(getattr(item, "date", "") or "").strip()
                label = date or "下一次到点就跑"
                bits.append("只做这一次（" + label + " " + when + "）")
            else:
                day_text = "每天" if (not days or len(days) >= 7) else "、".join(days)
                bits.append(f"{day_text} {when}")
            if note:
                bits.append(f"做什么：{note}")
            last = float(last_map.get(str(getattr(item, "id", "") or "")) or 0.0)
            bits.append("上次：" + (self._ago_text(max(0.0, float(self._now()) - last)) if last > 0 else "还没跑过"))
            if not once:
                nxt = self._next_time_text(item, moment)
                if nxt:
                    bits.append("下次：" + nxt)
            rows.append("- " + "；".join(bits))
        return "# 你排好的日程\n" + "\n".join(rows[:8])

    @staticmethod
    def _ago_text(seconds: float) -> str:
        minutes = max(0, int(seconds // 60))
        if minutes < 1:
            return "刚刚"
        if minutes < 60:
            return f"{minutes} 分钟前"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} 小时前"
        return f"{hours // 24} 天前"

    def _next_time_text(self, schedule: Any, moment: datetime) -> str:
        """这条日程下一次什么时候：今天/明天/N 天后 + 时间。"""

        try:
            hour, minute = (int(part) for part in str(schedule.time).split(":")[:2])
        except (TypeError, ValueError):
            return ""
        days = [str(day) for day in (schedule.days or [])]
        for offset in range(0, 8):
            when = (moment + timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if when <= moment:
                continue
            if days and self.WEEKDAY_KEYS[when.weekday()] not in days:
                continue
            label = "今天" if offset == 0 else ("明天" if offset == 1 else f"{offset} 天后")
            return f"{label} {hour:02d}:{minute:02d}"
        return ""

    def _thread_suspended(
        self, state: WorldState, thread: dict[str, Any] | None, now: float
    ) -> bool:
        """这件事是不是"挂起"了：不在等群友回应、下一步还要等很久。

        挂起的那件不占「我正在经历」那一段——中间那几小时她在做饭看书，不该被它绑着。
        """

        if not isinstance(thread, dict):
            return False
        info = state.pending_help or {}
        if str(info.get("state") or "") in ("active", "idle"):
            return False  # 正在等群友回话，算"正在经历"
        when = float(thread.get("next_step_at") or 0.0)
        if when <= 0:
            return False
        minutes = max(0, int(getattr(self.world.events, "suspend_after_minutes", 30) or 0))
        if minutes <= 0:
            return False
        return (when - float(now)) > minutes * 60.0

    async def _generate_event_package(
        self,
        state: WorldState,
        node: NodeDef | None,
        *,
        tier: str,
        seed: str = "",
        thread: dict[str, Any] | None = None,
        genre: dict[str, Any] | None = None,
    ) -> EventPackage | None:
        """让打杂 / 生成器模型现编一件（提示词是裁剪过的，不带世界规则与动作表）。"""

        config = self.world.events
        llm = self.creator_llm or self.helper_llm
        if llm is None:
            return None
        system, prompt = self.prompts.build_event_package_prompt(
            persona_brief=await self.event_persona_brief(state.session_id),
            scene_line=self.prompts.scene_compact(node),
            state_line=self._state_persona_line(state, node),
            thread_digest=thread_digest(thread) if thread else "",
            recent_titles=list(state.event_recent_titles or []),
            red_lines=list(getattr(config, "red_lines", []) or []),
            seed=seed,
            tier=tier,
            genre=genre,
            genre_scale=str(getattr(config, "genre_scale", "") or ""),
            recent_memories=[
                str(getattr(item, "content", "") or "")
                for item in self.memory.recall(
                    session_id=state.session_id,
                    persona_id="",
                    node_id=state.node_id,
                    limit=4,
                )
            ],
        )
        try:
            reply = await self._ask_creator(state.session_id, system, prompt)
        except Exception as exc:
            self._log("debug", f"生成事件失败：{exc}")
            return None
        package = parse_event_package(reply or "", source="user" if seed else "llm")
        if package is None:
            return None
        package.tier = tier
        return package

    def _state_persona_line(self, state: WorldState, node: NodeDef | None) -> str:
        """给她/打杂模型看的一行状态（人话）。"""

        bits = [
            f"地点：{node.name if node else state.node_id or '未知'}",
            f"心情：{state.mood}",
            f"精力 {'不错' if state.energy > 0.6 else ('一般' if state.energy > 0.3 else '很低')}",
        ]
        current = state.current_action or {}
        if current:
            desc = str(current.get("desc") or current.get("type") or "")
            if desc:
                bits.append(f"正在做：{desc}")
        if state.storm:
            bits.append("有点上头")
        return "；".join(bits)

    async def _maybe_run_event(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        """tick 里的那一步：先推进等待中的求助，再看要不要出事。"""

        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return
        now = self._event_time()
        self._ensure_abilities(state)
        # 掷骰时钟每 tick 都推进：过去的就让它过去，不攒"欠账"
        gap = self._event_roll_gap_hours(state, now)
        await self._advance_pending_help(state, node, outcome, now)

        # 有线索等着续：接着演下一步
        if await self._continue_thread(state, node, outcome, now):
            return

        # 还有没完的事（挂在后台、等群友回应、或者刚收尾没多久）就先不掷新的
        if self._active_thread(state) is not None:
            return

        blocked = self._event_block_reason(state, now)
        if blocked:
            self._log("debug", f"这次不安排事件：{blocked}")
            return
        # 两件事之间硬性隔开：刚完结又来一件，群里看着就像在刷
        gap_blocked = self._event_gap_reason(state, now)
        if gap_blocked:
            self._log("debug", f"这次不安排事件：{gap_blocked}")
            return
        tier, roll_info = self._roll_event_tier(state, now, gap)
        if not tier:
            self._last_roll_info = roll_info
            return
        await self._start_event(
            state, node, outcome, tier=tier, source="roll", roll_info=roll_info
        )

    async def _start_event(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        *,
        tier: str,
        seed: str = "",
        source: str = "roll",
        thread: dict[str, Any] | None = None,
        roll_info: dict[str, float] | None = None,
    ) -> None:
        """开一件新事（或者给已有线索接上一步）。"""

        config = self.world.events
        package: EventPackage | None = None
        # 同一时间只允许一件事：有没完的就别开新的（续线走上一条）
        if thread is None:
            active = self._active_thread(state)
            if active is not None:
                self._log(
                    "debug",
                    f"手上有没完的事件（{active.get('title')}），这次不安排新的",
                )
                return
        genre: dict[str, Any] | None = None
        if seed:
            # 用户投递的事件：照着他给的种子现编一件
            package = await self._generate_event_package(
                state, node, tier=tier, seed=seed, thread=thread, genre=None
            )
            if package is None:
                package = self._seed_package(seed, tier=tier)
        else:
            # 题材由代码按权重掷（模型只会挑自己偏好的那类写），每一件都临场生成
            genre = self._pick_genre(state)
            package = await self._generate_event_package(
                state, node, tier=tier, thread=thread, genre=genre
            )
        if package is None:
            # 生成失败（没配模型 / 模型没给出可用的包）：这一轮就当没出事
            self._log("debug", "事件生成没拿到可用的包，这次跳过")
            await self._log_event(
                state,
                "event_skip",
                {"tier": tier, "note": "生成模型没给出可用的事件包"},
                outcome=outcome,
            )
            return

        if thread is None:
            place_key, place_name = self._event_place(package, state)
            package.place = place_key
            package.place_name = place_name
            thread = new_thread(package, now=self._event_time(), scope=SCOPE_SESSION)
            state.event_threads = [*list(state.event_threads or []), thread][-6:]
            # 记下"这件事是什么时候开始的"：下一件要等 min_gap_minutes
            state.last_event_started_at = self._event_time()
        else:
            thread["root"] = package.as_payload()

        if package.title:
            state.event_recent_titles = [
                *list(state.event_recent_titles or []),
                package.title,
            ][-10:]
        genre_name = str((genre or {}).get("name") or "").strip()
        if genre_name:
            thread["genre"] = genre_name
            state.event_recent_genres = [
                *list(state.event_recent_genres or []),
                genre_name,
            ][-8:]
            # 题材冷却的账：同一个题材用完之后要过一会儿才允许再抽中
            state.event_genre_at = {
                **dict(getattr(state, "event_genre_at", None) or {}),
                genre_name: self._event_time(),
            }

        await self._log_event(
            state,
            "event",
            {
                "title": package.title,
                "hook": package.hook,
                "node": state.node_id,
                "tier": package.tier,
                "kind": package.kind,
                "imagined": package.mode == MODE_IMAGINED,
                "need_help": package.needs_help,
                "source": package.source or source,
                "thread": thread.get("id"),
                "genre": genre_name or str(thread.get("genre") or ""),
                "critical": bool(package.critical),
                # 掷骰明细：时间差、当时的概率、掷出的值（排查"为什么突然连出两件"用）
                "roll": dict(roll_info or {}),
            },
            outcome=outcome,
        )

        if package.tier == TIER_MICRO or not package.options:
            await self._settle_micro(state, outcome, thread, package)
            return
        await self._run_event_step(state, node, outcome, thread, package)

    def _seed_package(self, seed: str, *, tier: str = "small") -> EventPackage:
        """用户投递的种子没有模型可用时的兜底：给三个通用选项。"""

        text = " ".join(str(seed or "").split())
        return event_from_payload(
            {
                "title": text[:16],
                "scene": "",
                "hook": text,
                "tier": tier,
                "kind": KIND_SOLO,
                "options": [
                    {"desc": "硬着头皮上", "abilities": ["composure"]},
                    {"desc": "换个更稳的办法", "abilities": ["wits"]},
                    {"desc": "先放一放", "no_check": True},
                ],
            },
            source="user",
        ) or EventPackage(title=text[:16], hook=text, tier=tier)

    def _cached_decision(self, thread: dict[str, Any]) -> Decision | None:
        """把线索里存的那次抉择还原出来（超时自己收尾时要用）。"""

        raw = thread.get("decision") if isinstance(thread, dict) else None
        if not isinstance(raw, dict):
            return None
        return Decision(
            pick=int(raw.get("pick") or -1),
            desc=str(raw.get("desc") or ""),
            abilities=[str(name) for name in list(raw.get("abilities") or [])],
            no_check=bool(raw.get("no_check")),
            reason=str(raw.get("reason") or ""),
            say=dict(raw.get("say") or {}),
            send_to=str(raw.get("send_to") or ""),
        )

    async def _run_event_step(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
        *,
        second_round: bool = False,
        suggestions: Any = None,
        recap: bool = False,
    ) -> None:
        """一步事件：她抉择 →（求助 or 判定）→ 结算。"""

        options = self._render_options(state, package)
        scene_note = ""
        if recap:
            place = str(thread.get("place_name") or "")
            scene_note = (
                f"这件事发生在{('「' + place + '」') if place else '别处'}，"
                "你现在人在别的地方、只是又想起了它——不要说成自己此刻就在那里，"
                "也别描述现在的环境是那个地方。"
            )
        action_defs = self._event_action_defs(state, thread)
        action_lines = self.event_action_lines(action_defs)
        # 「先去做点什么」那一轮的预算：她可以连着查几次，查完就得拿主意
        rounds_left = self._event_action_budget(state, thread)
        decision = default_decision(package)
        while True:
            allow_act = bool(action_lines) and rounds_left > 0 and not second_round
            system, prompt = self.prompts.build_event_decision_prompt(
                persona_text=await self._persona_text(state.session_id),
                title=package.title,
                hook=package.hook,
                options=options,
                state_line=self._state_persona_line(state, node),
                ability_line=self.abilities_line(state),
                suggestions=suggestions.prompt_block() if suggestions is not None else "",
                second_round=second_round,
                scene_note=scene_note,
                action_lines=action_lines if allow_act else "",
                observations=self._event_observation_text(thread),
                act_rounds_left=rounds_left,
                session_directory=self.session_directory(state),
            )
            reply = await self._ask_llm(state.session_id, system, prompt)
            outcome.llm_calls += 1
            parsed = parse_decision(
                reply or "",
                package,
                allowed_actions={item.id for item in action_defs} if allow_act else set(),
            )
            decision = parsed or default_decision(package)
            if decision.phase != PHASE_ACT or not decision.actions or not allow_act:
                break
            await self._run_event_action(state, node, outcome, thread, package, decision)
            rounds_left -= 1
        # 这件事她想说给谁听：她自己挑，挑不出来就留在原来的落点上
        raw_target = str(decision.send_to or "").strip()
        if raw_target:
            picked = self.resolve_send_to(state, raw_target, fallback="")
            if picked:
                outcome.session_id = picked
            else:
                outcome.notes.append(
                    f"她本来想跟「{raw_target}」说这件事，但那儿不在她能说话的地方里"
                )
        thread["decision"] = {
            "pick": decision.pick,
            "desc": decision.desc,
            "abilities": list(decision.abilities),
            "no_check": bool(decision.no_check),
            "reason": decision.reason,
            "say": dict(decision.say),
            "send_to": decision.send_to,
        }
        await self._log_event(
            state,
            "event_choice",
            {
                "title": package.title,
                "desc": decision.desc,
                "abilities": "、".join(
                    ABILITY_LABELS.get(name, name) for name in decision.abilities
                ),
                "reason": decision.reason,
            },
            outcome=outcome,
        )

        want_help = (
            decision.ask_help
            and not second_round
            and bool(getattr(self.world.events, "intervene_enabled", True))
            and package.tier != TIER_MICRO
        ) or package.needs_help and not second_round
        if want_help:
            blocked = self._help_block_reason(state)
            if blocked:
                # 群里没人接话、或者她正在睡：这句话发出去只会显得莫名其妙，
                # 直接让她自己拿主意（结果照样进记忆和日志）。
                await self._log_event(
                    state,
                    "help",
                    {"title": package.title, "stage": "alone", "note": blocked},
                    outcome=outcome,
                )
                outcome.notes.append(f"没开口求助：{blocked}")
                # 那几句是"求助的话"，不问了就别再拿去当结果说
                # （`Decision.lines` 在没写 success/fail 时会退回 plan）
                decision.say.pop("plan", None)
                want_help = False
        if want_help:
            await self._ask_for_help(state, node, outcome, thread, package, decision)
            return
        await self._resolve_event_step(
            state, node, outcome, thread, package, decision, suggestions=suggestions
        )

    def _render_options(self, state: WorldState, package: EventPackage) -> list[str]:
        """把选项渲染成"带她的主观判断"的人话。不给概率数字。"""

        labels = ("A", "B", "C", "D")
        lines: list[str] = []
        for index, option in enumerate(package.options[:4]):
            abilities = [name for name in option.abilities if name in ABILITIES]
            ability_value = 0.6
            if abilities:
                values = normalize_abilities(state.abilities)
                ability_value = sum(values.get(name, 0.6) for name in abilities) / len(abilities)
            difficulty = max(0.05, min(0.95, float(package.difficulty) + float(option.difficulty)))
            head = f"{index + 1}. {option.desc}"
            if option.no_check:
                lines.append(f"{head}（稳，但这件事就算了）")
                continue
            probability = ability_value * difficulty
            hint = subjective_hint(
                probability,
                affect=state.affect,
                energy=state.energy,
                composure=normalize_abilities(state.abilities).get("composure", 0.6),
            )
            names = "、".join(ABILITY_LABELS.get(name, name) for name in abilities)
            lines.append(f"{head}（{hint}，靠{names or '心性'}）" if abilities else f"{head}（{hint}）")
        if not lines:
            lines = ["1. 硬着头皮上（差不多）", "2. 先放一放（稳）"]
        return lines

    async def _ask_for_help(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
        decision: Decision,
    ) -> None:
        """她开口求助：发出去（可以分几条），然后进入活跃等待。"""

        now = self._event_time()
        config = self.world.events
        lines = [str(item.get("text") or "") for item in decision.lines("plan")]
        lines = [text for text in lines if text.strip()]
        if not lines:
            lines = await self._compose_help_lines(state, package)
        lines = lines[: self._event_say_limit("ask")]
        state.pending_help = {
            "thread_id": thread.get("id"),
            "title": package.title,
            "state": "active",
            "asked_at": float(now),
            "active_until": float(now) + max(30.0, float(getattr(config, "active_seconds", 180.0))),
            "idle_until": 0.0,
            "reminder_sent": False,
            "suggestions": [],
            # 她当时是怎么问的：等没人接、要写那句自我圆场时得知道上文
            "asked": " / ".join(lines)[:120],
        }
        outcome.messages.extend(lines)
        outcome.speech_kind = "event"
        await self._log_event(
            state,
            "help",
            {
                "title": package.title,
                "stage": "ask",
                "text": " / ".join(lines),
                "wait_seconds": int(getattr(config, "active_seconds", 180.0)),
            },
            outcome=outcome,
        )
        state.add_event("event_help", {"title": package.title})

    async def _compose_help_lines(
        self, state: WorldState, package: EventPackage
    ) -> list[str]:
        """求助那两句让她自己说；模型不可用时才用一句兜底。

        以前是代码拼死的一句「有人吗」：不管遇上什么都一样，群里看着像机器人。
        """

        helper = self.helper_llm or self.llm
        hook = _clip_text(package.hook, 60).rstrip("。")
        fallback = [f"{hook}……这可怎么办" if hook else "遇到点事……"]
        if helper is None:
            return fallback
        try:
            system, prompt = self.prompts.build_help_ask_prompt(
                persona_text=await self._persona_text(state.session_id),
                title=package.title,
                hook=package.hook,
            )
        except Exception:
            return fallback
        reply = await self._ask_helper(state.session_id, system, prompt)
        if not reply:
            return fallback
        lines: list[str] = []
        payload = extract_json_object(reply)
        if isinstance(payload, dict):
            raw = payload.get("lines")
            if isinstance(raw, str):
                raw = [raw]
            lines = [
                " ".join(str(item).split())
                for item in list(raw or [])
                if str(item).strip()
            ]
            # 返回了 JSON 但没有 lines：不要把她自己的乱 JSON 当话发出去
            return [item[:60] for item in lines if item][:2] or fallback
        lines = [" ".join(item.split()) for item in str(reply).splitlines() if item.strip()]
        lines = [item[:60] for item in lines if item][:2]
        return lines or fallback

    def _max_say_lines(self) -> int:
        """事件里她一次最多说几句。

        求助那种"有人吗 / 我这是… / 怎么办啊"要分几条发，所以给得比平时宽松；
        但绝对硬顶拦着，不会刷屏。``kind``：``ask`` 求助 / ``result`` 结果 / ``beat`` 心跳更新。
        """

        return self._event_say_limit("ask")

    def _help_block_reason(self, state: WorldState) -> str:
        """现在不适合开口求助的原因（空串 = 可以喊）。

        群里没人接话时喊「有人吗」只有一种结果：她在那儿干等，群友看到一句
        没头没尾的话——比她自己把事情办了还奇怪。
        """

        if self._is_asleep(state):
            return "她在睡觉"
        if not self.group_is_alive(state):
            return "群里最近没人说话"
        return ""

    def _event_public_speech(self, state: WorldState) -> bool:
        """事件里的话要不要发到群里。睡着（打盹也算）时只记状态和记忆。"""

        return not self._is_asleep(state)

    def _event_say_limit(self, kind: str = "ask") -> int:
        config = self.world.events
        try:
            base = int(getattr(config, "max_say_lines", 4) or 4)
        except (TypeError, ValueError):
            base = 4
        try:
            hard = int(getattr(config, "max_say_lines_hard", 6) or 6)
        except (TypeError, ValueError):
            hard = 6
        limit = max(1, min(base, hard))
        if kind == "result":
            limit = min(limit, 2)
        elif kind == "beat":
            limit = 1
        return limit

    async def _resolve_event_step(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
        decision: Decision,
        *,
        suggestions: Any = None,
        forced: bool = False,
    ) -> None:
        """判定 + 结算 + 她的话。"""

        now = self._event_time()
        config = self.world.events
        values = normalize_abilities(state.abilities)
        ability = (decision.abilities or ["wits"])[0]
        ability_value = values.get(ability, 0.6)
        difficulty = max(
            0.05, min(0.95, float(package.difficulty) + float(decision.difficulty_delta))
        )
        if decision.no_check:
            # 她选了「先不做」：不掷骰，也不长能力值
            from .events import CheckResult, TIER_SKIP

            modifiers: list[tuple[str, float]] = []
            check = CheckResult(
                ability=ability,
                ability_value=ability_value,
                difficulty=difficulty,
                tier=TIER_SKIP,
            )
        else:
            modifiers = situational_modifiers(
                energy=state.energy,
                affect=state.affect,
                alignment=int(getattr(suggestions, "alignment", 0) or 0)
                if bool(getattr(config, "suggest_tools", True))
                else 0,
                failed_before=any(
                    str(step.get("tier") or "") == "fail"
                    for step in list(thread.get("steps") or [])
                ),
            )
            check = roll_check(
                ability=ability,
                ability_value=ability_value,
                difficulty=difficulty,
                modifiers=modifiers,
                roll=self.rng.random,
            )
        await self._log_event(
            state,
            "event_check",
            {
                "title": package.title,
                "ability": ABILITY_LABELS.get(ability, ability),
                "ability_value": round(ability_value, 3),
                "difficulty": round(difficulty, 3),
                "probability": check.probability,
                "roll": check.roll,
                "tier": check.label,
                "skip": bool(decision.no_check),
                "modifiers": [
                    {"label": label, "factor": factor} for label, factor in modifiers
                ],
            },
            outcome=outcome,
        )

        settlement = await self._settle_event(
            state,
            node,
            package,
            decision,
            check=check,
            suggestions=suggestions,
            thread=thread,
        )
        delta, spent = clamp_ability_delta(
            settlement.ability_delta,
            spent_today=state.ability_spent_today,
            day=state.ability_day,
            today=self._today_key(now),
        )
        if delta:
            state.abilities = apply_ability_delta(state.abilities, delta)
        state.ability_day = self._today_key(now)
        state.ability_spent_today = spent
        if settlement.state_delta:
            self.dynamics.apply_pulse(
                state,
                settlement.state_delta,
                now=now,
                cause=f"刚才那件事（{package.title}）",
            )
        # 判定档位本身也带一点情绪：成功偏正面、失败偏负面（可关）
        if bool(getattr(config, "result_emotion", True)):
            pulse = tier_emotion(check.tier)
            if pulse:
                self.dynamics.apply_pulse(
                    state,
                    pulse,
                    now=now,
                    cause=f"刚才那件事（{package.title}）",
                )
                state.mood = self.dynamics.derive_mood(state)
        if settlement.outcome:
            keep = max(1, int(getattr(config, "event_digest_lines", 12) or 12))
            state.event_digest = [
                *list(state.event_digest or []),
                f"{package.title}：{settlement.outcome}",
            ][-keep:]
        if settlement.memory or settlement.outcome:
            # 记忆记在**事件发生的地点**，不是她此刻的位置：
            # 否则"在窗边"会把逛街被跟的事记进窗边，以后按地点根本搜不到。
            place_key, place_name = self._event_place(package, state)
            content = str(settlement.memory or settlement.outcome)[:120]
            if place_name and place_name not in content:
                content = f"在{place_name}：{content}"[:120]
            self.memory.remember(
                session_id=state.session_id,
                persona_id="",
                node_id=place_key,
                content=content,
                memory_type=SCENE,
                emotion=state.mood,
                weight=0.4,
                affect=state.affect,
                valence=state.valence,
            )

        followup = str(settlement.followup or "").strip()
        exhausted = thread_exhausted(
            thread,
            now=now,
            max_steps=int(getattr(config, "max_steps", 6) or 6),
            max_minutes=float(getattr(config, "max_minutes", 120.0) or 120.0),
        )
        if exhausted:
            followup = ""
        elif not followup and self._event_must_continue(thread, tier=package.tier):
            # 大事件不许一步收尾：模型忘了留伏笔时由代码补一步，
            # 否则"有人找我麻烦"下一句就变成"我自己解决了"，看着特别潦草。
            followup = (
                f"{package.title}还没完，下一步得接着处理"
            )
        # 这一幕能不能说出口：**必须在 append_step 之前算**——
        # 那之后步数就变成"下一幕"了，开会话的判定会整体错开一幕。
        share_allowed = self._event_share_allows(
            state, tier=package.tier, thread=thread, followup=followup
        )
        share_step = self._event_step_no(thread)
        # 下一幕隔多久：模型可以在结算里自己定（``next_gap``，分钟），
        # 配置里的 step_gap_seconds 既是最小值也是它的兜底。
        base_gap = max(0.0, float(getattr(config, "step_gap_seconds", 60.0) or 60.0))
        if str(package.tier or "").strip().lower() == TIER_BIG:
            # 大事件要演两幕以上，间隔太密会看着像刷屏
            base_gap = max(
                base_gap,
                max(0.0, float(getattr(config, "big_step_gap_minutes", 0.0) or 0.0)) * 60.0,
            )
        asked_gap = float(getattr(settlement, "next_gap_minutes", 0.0) or 0.0) * 60.0
        next_gap = max(base_gap, min(asked_gap, 360.0 * 60.0)) if asked_gap else base_gap
        append_step(
            thread,
            outcome={
                "status": "open" if followup else "closed",
                "followup": followup,
                "next_step_gap": next_gap,
            },
            now=now,
            step={
                "desc": decision.desc,
                "ability": ABILITY_LABELS.get(ability, ability),
                "tier": check.tier,
                "tier_label": check.label,
                "result": settlement.outcome,
                "ability_delta": dict(delta),
                "suggestions": int(getattr(suggestions, "alignment", 0) or 0),
            },
        )
        if not followup:
            thread["status"] = "closed"
            thread["closed_at"] = float(now)
        # 她自己的账：每一幕的结果并进这一条（不是每幕一条，免得读起来像流水账）
        line = str(settlement.memory or settlement.outcome or "").strip()
        if line:
            old = str(thread.get("line") or "").strip()
            thread["line"] = (f"{old}；{line}" if old else line)[:220]

        slot = "success" if check.ok else "fail"
        said = [str(item.get("text") or "") for item in decision.lines(slot)]
        said = [text for text in said if text.strip()][: self._event_say_limit("result")]
        # 先记进留档，再决定这一句能不能说出口：静默的幕也该留下"她经历过"的痕迹
        self._note_event_in_chat(state, package, settlement, thread, followup=followup)
        if said and not share_allowed:
            # 睡着（或这一档本来就是静默推演）时事件照演，但不在群里出声——
            # 群友只会看到"她说自己在睡觉，却在群里讲话"。
            outcome.notes.append(f"这一幕按策略没有发到群里（{package.title}）")
            await self._log_event(
                state,
                "event_mute",
                {
                    "title": package.title,
                    "tier": package.tier,
                    "share": self._event_share_mode(package.tier),
                    "step": share_step,
                    "text": " / ".join(said)[:160],
                },
                outcome=outcome,
            )
            said = []
        if said:
            outcome.messages.extend(said)
            outcome.speech_kind = "event"
        await self._log_event(
            state,
            "event_result",
            {
                "title": package.title,
                "outcome": settlement.outcome,
                "tier": check.label,
                "ability_delta": {
                    ABILITY_LABELS.get(name, name): round(value, 3)
                    for name, value in delta.items()
                },
                "followup": followup,
                "closed": not followup,
                "suggestions": getattr(suggestions, "digest", lambda: "")()
                if suggestions is not None
                else "",
            },
            outcome=outcome,
        )
        await self._maybe_photo_for_event(state, node, outcome, thread, package)

    def _photo_action_id(self) -> str:
        """事件出图用哪个动作：按配置里的顺序挑第一个启用、且带指令的。"""

        for name in list(getattr(self.world.events, "photo_actions", []) or []):
            action = self.world.action_map().get(str(name).strip())
            if action is None or not action.enabled:
                continue
            if str(getattr(action, "llm_level", "")) == "command" and not str(
                getattr(action, "trigger_command", "") or ""
            ).strip():
                continue
            return action.id
        return ""

    async def _maybe_photo_for_event(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
    ) -> None:
        """事件每一幕推进 / 完结时，按概率让她拍一张（图会带上这件事的内容）。

        出图动作自己会说话（``on_complete=llm_followup``），所以这里只负责
        把"现在发生了什么"写进意图，让图跟剧情对得上。
        """

        config = self.world.events
        try:
            chance = float(getattr(config, "photo_chance", 0.0) or 0.0)
        except (TypeError, ValueError):
            chance = 0.0
        if chance <= 0:
            return
        if self.rng.random() >= min(1.0, chance):
            return
        action_id = self._photo_action_id()
        if not action_id:
            return
        steps = [item for item in list(thread.get("steps") or []) if isinstance(item, dict)]
        last = steps[-1] if steps else {}
        bits = [
            f"你刚经历的事：{package.title}",
            f"发生了什么：{package.hook}",
        ]
        if last.get("result"):
            bits.append(f"结果：{last.get('result')}")
        bits.append("拍一张和这件事对得上的照片，意图里写清画面内容与此刻的环境。")
        intent = "；".join(str(item) for item in bits if str(item).strip())
        planned = PlannedAction(type=action_id, intent=intent, content=intent)
        await self._execute_actions(
            state, node, outcome, [planned], depth=0, autonomous=True
        )
        await self._log_event(
            state,
            "event_action",
            {
                "title": package.title,
                "action": action_id,
                "intent": intent,
                "note": "这一件事推进时顺手拍了张照",
            },
            outcome=outcome,
        )

    async def _settle_micro(
        self,
        state: WorldState,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
    ) -> None:
        """微事件：生成时就带好了结果与效果，这里只落账，**不再调模型**。"""

        now = self._event_time()
        delta, spent = clamp_ability_delta(
            package.abilities,
            spent_today=state.ability_spent_today,
            day=state.ability_day,
            today=self._today_key(now),
        )
        if delta:
            state.abilities = apply_ability_delta(state.abilities, delta)
        state.ability_day = self._today_key(now)
        state.ability_spent_today = spent
        if package.effects:
            self.dynamics.apply_pulse(
                state,
                package.effects,
                now=now,
                cause=f"刚才那件小事（{package.title}）",
            )
        result = package.outcome or package.hook
        if result:
            # 微事件群里不主动说，但会进"这段时间发生的事"，动作完成续说时顺口带一句
            keep = max(1, int(getattr(self.world.events, "event_digest_lines", 12) or 12))
            state.event_digest = [
                *list(state.event_digest or []),
                f"{package.title}：{result}",
            ][-keep:]
        if package.memory or result:
            place_key, place_name = self._event_place(package, state)
            content = str(package.memory or result)[:120]
            if place_name and place_name not in content:
                content = f"在{place_name}：{content}"[:120]
            self.memory.remember(
                session_id=state.session_id,
                persona_id="",
                node_id=place_key,
                content=content,
                memory_type=SCENE,
                emotion=state.mood,
                weight=0.25,
                affect=state.affect,
                valence=state.valence,
            )
        append_step(
            thread,
            outcome={"status": "closed"},
            now=now,
            step={"desc": "（没有选择）", "result": result, "ability_delta": dict(delta)},
        )
        thread["status"] = "closed"
        thread["closed_at"] = float(now)
        line = str(package.memory or result or "").strip()
        if line:
            old = str(thread.get("line") or "").strip()
            thread["line"] = (f"{old}；{line}" if old else line)[:220]
        await self._log_event(
            state,
            "event_result",
            {
                "title": package.title,
                "outcome": result,
                "closed": True,
                "ability_delta": {
                    ABILITY_LABELS.get(name, name): round(value, 3)
                    for name, value in delta.items()
                },
            },
            outcome=outcome,
        )

    async def _settle_event(
        self,
        state: WorldState,
        node: NodeDef | None,
        package: EventPackage,
        decision: Decision,
        *,
        check: Any,
        suggestions: Any = None,
        thread: dict[str, Any] | None = None,
    ):
        """让事件模型写结果；它给不出东西就用事件包里的伏笔兜底。"""

        config = self.world.events
        system, prompt = self.prompts.build_event_settle_prompt(
            title=package.title,
            hook=package.hook,
            desc=decision.desc,
            tier_label=check.label,
            state_line=self._state_persona_line(state, node),
            suggestions_digest=getattr(suggestions, "digest", lambda: "")()
            if suggestions is not None
            else "",
            next_step_hint=str((thread or {}).get("pending_followup") or ""),
            fail_menu=list(getattr(config, "fail_result_menu", []) or []),
            must_continue=self._event_must_continue(thread, tier=package.tier),
        )
        raw = None
        if bool(getattr(config, "enabled", True)):
            raw = await self._ask_event(state.session_id, system, prompt)
            self._count_tool_param(state)
        settlement = parse_settlement(raw or "") if raw else None
        if settlement is None:
            fallback = (
                package.followup.get("success" if check.ok else "fail")
                or package.followup.get("success")
                or f"{package.title}这件事就这样过去了"
            )
            from .events import Settlement

            settlement = Settlement(outcome=str(fallback)[:200])
        if not settlement.outcome:
            settlement.outcome = f"{package.title}这件事讲完了"
        # 失败涨经验：模型忘了给变化时补一点，保证"失败了也能再试"
        if not check.ok and not settlement.ability_delta:
            settlement.ability_delta = {str(decision.abilities[0] if decision.abilities else "composure"): 0.02}
        return settlement

    def _thread_by_id(self, state: WorldState, thread_id: str) -> dict[str, Any] | None:
        wanted = str(thread_id or "")
        if not wanted:
            return None
        for item in list(state.event_threads or []):
            if isinstance(item, dict) and str(item.get("id") or "") == wanted:
                return item
        return None

    # ---------------- 事件里她可以先去做点什么 ----------------

    def _event_action_budget(self, state: WorldState, thread: dict[str, Any]) -> int:
        """这条线索还剩几次「先做点什么」的额度。"""

        total = max(0, int(getattr(self.world.events, "event_action_calls", 2) or 0))
        used = max(0, int(thread.get("action_calls") or 0))
        return max(0, total - used)

    def _event_action_defs(
        self, state: WorldState, thread: dict[str, Any]
    ) -> list[ActionDef]:
        """事件里允许她调用的动作。

        默认口径：工具型 / 指令型（能借外部能力做事），排除「往群里发东西」那类；
        每个动作还能用 ``event_usable`` 覆盖（强制允许 / 禁止）。
        """

        if self._event_action_budget(state, thread) <= 0:
            return []
        node_id = state.node_id
        known_tools = set(self.available_tools())
        picked: list[ActionDef] = []
        for action in self.world.actions:
            if not action.enabled:
                continue
            rule = str(getattr(action, "event_usable", "auto") or "auto")
            if rule == "deny":
                continue
            if rule != "allow":
                if action.llm_level not in ("tool", "command"):
                    continue
                if str(getattr(action, "target_type", "none")) == "group":
                    continue
            # 只列真的跑得起来的：工具动作得挂了已注册的工具，指令动作得有指令名
            if action.llm_level == "tool" and not (
                set(action.tool_list()) & known_tools
            ):
                continue
            if action.llm_level == "command" and not str(
                getattr(action, "trigger_command", "") or ""
            ).strip():
                continue
            if not self._action_usable_now(action):
                continue
            if not action.available_in(node_id) and not self._nearest_node_with_action(
                node_id, action
            ):
                continue
            picked.append(action)
        picked.sort(key=lambda item: (int(item.priority or 5), item.id))
        return picked[:12]

    def event_action_lines(self, defs: list[ActionDef]) -> str:
        """动作清单进事件提示词的样子：只有 id 和一句说明，不带参数定义。"""

        lines: list[str] = []
        for action in defs:
            note = " ".join(str(action.description or action.name or "").split())
            lines.append(f"- {action.id}：{_clip_text(note, 60) or '（没有说明）'}")
        return "\n".join(lines)

    def _event_observation_text(self, thread: dict[str, Any]) -> str:
        """已经查到的结果，交给下一轮判断。"""

        items = [str(item).strip() for item in list(thread.get("observations") or [])]
        items = [item for item in items if item]
        return "\n".join(f"- {_clip_text(item, 200)}" for item in items[-6:])

    async def _run_event_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
        decision: Decision,
    ) -> None:
        """执行她在事件里挑的动作，把结果写进「已经查到的」里。

        这一轮严格不说话：动作结果只用来帮她判断这件事怎么办，
        要说的话留到拿主意那一轮（提示词里也是这么约定的）。
        """

        item = dict(decision.actions[0])
        action_id = str(item.get("type") or "")
        definition = self.world.action_map().get(action_id)
        if definition is None or not definition.enabled:
            thread["observations"] = [
                *list(thread.get("observations") or []),
                f"想用「{action_id}」，但这个动作现在用不了",
            ][-6:]
            return
        planned = PlannedAction(
            type=definition.id,
            intent=str(item.get("intent") or ""),
            content=str(item.get("intent") or ""),
            params=dict(item.get("params") or {}),
        )
        marker = await self._latest_event_id(state.session_id)
        self._silent_followup += 1
        try:
            await self._execute_actions(
                state, node, outcome, [planned], depth=0, autonomous=True
            )
        finally:
            self._silent_followup -= 1
        thread["action_calls"] = int(thread.get("action_calls") or 0) + 1
        results = await self._action_results_since(state.session_id, marker)
        if not results:
            results = ["（这个动作跑完了，但没有拿到能用的内容）"]
        observations = [f"{definition.name or definition.id}：{text}" for text in results]
        thread["observations"] = [
            *list(thread.get("observations") or []),
            *observations,
        ][-6:]
        await self._log_event(
            state,
            "event_action",
            {
                "title": package.title,
                "action": definition.id,
                "intent": str(item.get("intent") or ""),
                "result": _clip_log("；".join(results)),
            },
            outcome=outcome,
        )

    async def _latest_event_id(self, session_id: str) -> int:
        """当前最大事件 id（当作"这次动作之前"的记号）。"""

        try:
            rows = await self.db.call("query_events", session_id=session_id, limit=1)
        except Exception:
            return 0
        return int((list(rows or [{}]) or [{}])[0].get("id") or 0)

    async def _action_results_since(self, session_id: str, marker: int) -> list[str]:
        """这次动作产生的结果文本（工具型和指令型都从日志里取）。"""

        try:
            rows = await self.db.call("query_events", session_id=session_id, limit=40)
        except Exception:
            return []
        texts: list[str] = []
        for row in reversed(list(rows or [])):
            if int(row.get("id") or 0) <= int(marker):
                continue
            kind = str(row.get("event_type") or "")
            detail = row.get("detail") or {}
            if kind in ("tool_result", "command_result"):
                body = str(detail.get("result") or "").strip()
                if not bool(detail.get("ok", True)):
                    body = f"没做成（{detail.get('error') or '没有返回内容'}）"
                if body:
                    texts.append(body)
            elif kind == "skip":
                note = str(detail.get("note") or "").strip()
                if note:
                    texts.append(f"这一步没做成：{note}")
        return [_clip_text(text, 400) for text in texts[-4:]]

    def _followup_package(self, thread: dict[str, Any], hint: str) -> EventPackage:
        """没有模型时，把上一句伏笔直接当成下一步事件。"""

        text = " ".join(str(hint or "").split())
        title = str(thread.get("title") or "")
        return event_from_payload(
            {
                "title": title or text[:16],
                "hook": text,
                "tier": "small",
                "difficulty": 0.5,
                "options": [
                    {"desc": "接着处理", "abilities": ["composure"]},
                    {"desc": "换个办法", "abilities": ["wits"]},
                    {"desc": "先放着", "no_check": True},
                ],
            },
            source="pool",
        ) or EventPackage(title=title or text[:16], hook=text, tier="small")

    async def _continue_thread(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        now: float,
        *,
        thread_id: str = "",
    ) -> bool:
        """有线索的下一步到点了：接着演（续线时把整条线索喂回去）。

        ``thread_id`` 只给编辑器上的「立即推进一幕」用：那一次只推这一条，
        不能顺手把别的到点线索也演了。
        """

        config = self.world.events
        for thread in open_threads(state.event_threads):
            if thread_id and str(thread.get("id") or "") != thread_id:
                continue
            followup = str(thread.get("pending_followup") or "").strip()
            when = float(thread.get("next_step_at") or 0.0)
            if not followup or when <= 0 or float(now) < when:
                continue
            if self._is_asleep(state) and not bool(getattr(config, "in_sleep", False)):
                continue
            # 挂太久了就不再续演：直接收尾，写一条"那件事后来没再提"
            resume_hours = max(0.0, float(getattr(config, "thread_resume_max_hours", 24.0) or 0.0))
            idle = float(now) - float(thread.get("updated_at") or thread.get("opened_at") or now)
            if resume_hours and idle > resume_hours * 3600.0:
                thread["status"] = "closed"
                thread["closed_at"] = float(now)
                thread["pending_followup"] = ""
                thread["next_step_at"] = 0.0
                self.memory.remember(
                    session_id=state.session_id,
                    persona_id="",
                    node_id=str(thread.get("place") or state.node_id),
                    content=f"（{thread.get('title') or '一件事'}）那件事后来没再提",
                    memory_type=SCENE,
                    emotion=state.mood,
                    weight=0.25,
                    affect=state.affect,
                    valence=state.valence,
                )
                await self._log_event(
                    state,
                    "event_idle",
                    {"title": thread.get("title"), "stage": "expired"},
                    outcome=outcome,
                )
                continue
            base = event_from_payload(thread.get("root") or {}, source="pool")
            tier = str((base.tier if base else "") or "small")
            package = await self._generate_event_package(
                state, node, tier=tier, thread=thread
            )
            if package is None:
                package = self._followup_package(thread, followup)
            # 这一步是"接着上一步演"，来源标成线索续演（日志里一眼能区分）
            package.source = "follow"
            thread["pending_followup"] = ""
            thread["next_step_at"] = 0.0
            await self._log_event(
                state,
                "event",
                {
                    "title": package.title,
                    "hook": package.hook,
                    "node": state.node_id,
                    "tier": package.tier,
                    "kind": package.kind,
                    "imagined": package.mode == MODE_IMAGINED,
                    "need_help": package.needs_help,
                    "source": package.source or "follow",
                    "thread": thread.get("id"),
                    "step": len(list(thread.get("steps") or [])) + 1,
                },
                outcome=outcome,
            )
            if package.tier == TIER_MICRO or not package.options:
                await self._settle_micro(state, outcome, thread, package)
            else:
                # 分集续演：她这会儿人可能在别处，这一幕按"想起来/复盘"来演，
                # 不硬把场景搬回去（也没法搬——外部场景不在世界地图上）
                await self._run_event_step(
                    state,
                    node,
                    outcome,
                    thread,
                    package,
                    recap=self._needs_recap(state, thread),
                )
            return True
        return False

    def _needs_recap(self, state: WorldState, thread: dict[str, Any]) -> bool:
        """这一幕要不要写成"想起来"：她此刻不在事件发生的那个地方。"""

        place = str(thread.get("place") or "")
        if not place:
            return False
        if place.startswith("outside:"):
            return True
        return place != state.node_id

    def _event_share_mode(self, tier: str) -> str:
        """这一档事件要不要出声（``silent`` / ``nodes`` / ``always``）。"""

        config = self.world.events
        name = str(tier or "small").strip().lower()
        if name not in ("micro", "small", "big"):
            name = "small"
        value = str(getattr(config, f"share_{name}", "") or "").strip().lower()
        return value if value in ("silent", "nodes", "always") else "nodes"

    def _event_step_no(self, thread: dict[str, Any] | None) -> int:
        """这是这条线索的第几幕（从 1 开始；还没记进去的那一幕算下一幕）。"""

        try:
            done = len([item for item in list((thread or {}).get("steps") or []) if item])
        except TypeError:
            done = 0
        return done + 1

    def _event_share_allows(
        self,
        state: WorldState,
        *,
        tier: str,
        thread: dict[str, Any] | None = None,
        followup: str = "",
        ask_for_help: bool = False,
    ) -> bool:
        """这一幕她能不能把话说到群里。

        分档的用意：事件是"她身上发生的事"，不是"要演给群友看的节目"。
        大部分幕只在记忆和状态里过一遍，只有起头、收尾这种真正的节点才说出来——
        这样既不会一个人没头没尾地尬演，日后聊天时又有材料可以当谈资。
        """

        if not self._event_public_speech(state):
            return False
        if ask_for_help:
            # 求助是独立的一档：真要人帮忙才开口，不归分享策略管
            return True
        mode = self._event_share_mode(tier)
        if mode == "always":
            return True
        if mode == "silent":
            return False
        # nodes：只在第一幕和收尾那一幕说
        step = self._event_step_no(thread)
        if step <= 1:
            return True
        return not str(followup or "").strip()

    def _event_must_continue(self, thread: dict[str, Any] | None, *, tier: str) -> bool:
        """这一幕之后必须还有下一幕吗（大事件不许一步收尾）。"""

        if str(tier or "").strip().lower() != TIER_BIG:
            return False
        floor = max(1, int(getattr(self.world.events, "big_min_steps", 1) or 1))
        return self._event_step_no(thread) < floor

    def _note_event_in_chat(
        self,
        state: WorldState,
        package: EventPackage,
        settlement: Any,
        thread: dict[str, Any],
        *,
        followup: str,
    ) -> None:
        """把这件事的结果写进聊天留档——不是她说的话，是"她身上发生的事"。

        只在收尾那一幕写一条，写的是整条线索的累积摘要。这样她日后聊天时
        可以自然提起"我前两天把锅盖拧死了"，而不是每件事都只活在提示词的另一段里。
        """

        if not bool(getattr(self.world.events, "event_into_chat_log", True)):
            return
        if str(followup or "").strip():
            # 还没完：等收尾那一幕再写，免得留档里全是半截话
            return
        line = str(thread.get("line") or "").strip() or str(
            getattr(settlement, "outcome", "") or ""
        ).strip()
        if not line:
            return
        _key, place_name = self._event_place(package, state)
        where = f"·{place_name}" if place_name else ""
        state.note_chat(
            user_id="",
            name="（她自己）",
            text=f"〔她自己身上 #{package.title}{where}〕{line}",
            now=self._now(),
            keep=self.chat_history_limit(),
            internal=True,
            origin=state.session_id,
        )

    def _apply_thread_target(
        self, state: WorldState, outcome: TickOutcome, thread: dict[str, Any] | None
    ) -> None:
        """把"这件事她想说给谁听"落到这一轮的落点上。

        第一幕在 :meth:`_run_event_step` 里已经落过一次；**后面几幕是新的 tick**，
        默认落点是那个 tick 所在的会话——如果她当初选了别的会话（会话组里的另一个），
        这里得再对一次，否则"我在私聊里跟他说这件事"会跑到群里去。
        """

        info = dict((thread or {}).get("decision") or {})
        raw = str(info.get("send_to") or "").strip()
        if not raw:
            return
        picked = self.resolve_send_to(state, raw, fallback="")
        if picked:
            outcome.session_id = picked

    async def _compose_remind_line(
        self,
        state: WorldState,
        *,
        title: str,
        hook: str,
        asked: str,
    ) -> str:
        """求助没人接时她那句自我圆场——让她自己按人设写，写不出来就不说。"""

        channel = self.helper_llm
        if channel is None:
            return ""
        try:
            system, prompt = self.prompts.build_help_remind_prompt(
                persona_text=await self._persona_text(state.session_id),
                title=title,
                hook=hook,
                asked=asked,
            )
        except Exception:
            return ""
        try:
            reply = await channel.generate(
                session_id=state.session_id,
                system_prompt=system,
                prompt=prompt,
                temperature=0.4,
            )
        except Exception as exc:
            self._log("debug", f"生成求助圆场那句失败：{exc}")
            return ""
        self._count_tool_param(state)
        text = getattr(reply, "text", "") if getattr(reply, "ok", False) else ""
        payload = extract_json_object(str(text or ""))
        line = ""
        if isinstance(payload, dict):
            line = " ".join(str(payload.get("line") or "").split())
        elif str(text or "").strip():
            line = " ".join(str(text).split())
        # 只要一句：模型偶尔会顺手写两行，后面的丢掉
        line = line.splitlines()[0].strip() if line else ""
        if line.startswith("{"):
            return ""
        return line[:40]

    async def _advance_pending_help(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome, now: float
    ) -> None:
        """等待中的求助：活跃 →（轻提醒）轻等待 →（超时）自己收尾。"""

        info = dict(state.pending_help or {})
        stage = str(info.get("state") or "")
        if stage not in ("active", "idle"):
            return
        config = self.world.events
        thread = self._thread_by_id(state, str(info.get("thread_id") or ""))
        if stage == "active":
            if float(now) < float(info.get("active_until") or 0.0):
                return
            info["state"] = "idle"
            info["reminder_sent"] = True
            info["idle_until"] = float(info.get("asked_at") or now) + max(
                1.0, float(getattr(config, "idle_minutes", 60.0) or 60.0)
            ) * 60.0
            state.pending_help = info
            root = dict((thread or {}).get("root") or {})
            text = ""
            if bool(getattr(config, "remind_when_ignored", True)):
                text = await self._compose_remind_line(
                    state,
                    title=str(info.get("title") or root.get("title") or "那件事"),
                    hook=str(root.get("hook") or ""),
                    asked=str(info.get("asked") or ""),
                )
            # 这句话该说给哪个会话：她当初挑了别的会话就跟着她
            self._apply_thread_target(state, outcome, thread)
            if text and self._event_public_speech(state):
                outcome.messages.append(text)
                outcome.speech_kind = "event"
            elif text:
                outcome.notes.append("她在睡觉，这句提醒没有发到群里")
            else:
                outcome.notes.append("没人接话，她自己圆场的那句没写出来（这一轮不说）")
            await self._log_event(
                state,
                "help",
                {
                    "title": info.get("title"),
                    "stage": "remind",
                    "text": text,
                    "note": "" if text else "没写出圆场的话，保持安静",
                },
                outcome=outcome,
            )
            return
        if float(now) < float(info.get("idle_until") or 0.0):
            return
        state.pending_help = {}
        if thread is None:
            return
        package = event_from_payload(thread.get("root") or {}, source="pool")
        decision = self._cached_decision(thread)
        if package is None:
            thread["status"] = "closed"
            return
        if decision is None:
            decision = default_decision(package)
        # 收尾这一句也要回到她当初选的那个会话，别落在 tick 所在的组代表会话上
        self._apply_thread_target(state, outcome, thread)
        await self._log_event(
            state,
            "help",
            {
                "title": package.title,
                "stage": "timeout",
                "note": "等不到人，她按自己的判断收尾",
            },
            outcome=outcome,
        )
        await self._log_event(
            state,
            "event_idle",
            {"title": package.title, "stage": "abandon"},
            outcome=outcome,
        )
        await self._resolve_event_step(
            state, node, outcome, thread, package, decision, suggestions=None
        )

    async def _filter_suggestions(
        self,
        state: WorldState,
        package: EventPackage,
        decision: Decision,
        messages: list[str],
    ):
        """群友的回应分拣：只丢无关的，起哄照留。"""

        config = self.world.events
        if not bool(getattr(config, "suggest_tools", True)):
            return None
        system, prompt = self.prompts.build_suggestion_filter_prompt(
            title=package.title,
            hook=package.hook,
            her_plan=decision.desc or "",
            messages=messages,
            red_lines=list(getattr(config, "red_lines", []) or []),
        )
        raw = await self._ask_judge(state.session_id, system, prompt)
        self._count_tool_param(state)
        return parse_suggestions(raw or "")

    async def _consume_help_reply(self, ctx: MessageContext) -> ReplyOutcome | None:
        """她正在等协助时，群友的回应先给事件线看一眼：能消化就消化，否则照常回复。"""

        if self.llm is None:
            return None
        async with self.session_state(ctx.session_id) as state:
            info = state.pending_help or {}
            if str(info.get("state") or "") not in ("active", "idle"):
                return None
            thread = self._thread_by_id(state, str(info.get("thread_id") or ""))
            package = event_from_payload((thread or {}).get("root") or {}, source="pool")
            if thread is None or package is None:
                state.pending_help = {}
                return None
            decision = self._cached_decision(thread) or default_decision(package)
            messages = [f"{ctx.user_name or '有人'}：{ctx.text}"]
            suggestions = await self._filter_suggestions(state, package, decision, messages)
            if suggestions is None or not suggestions.related:
                return None
            # 有人对这件事说了话：事件线把这一轮吃掉，她不再走普通回复
            state.pending_help = {}
            node = self.node(state.node_id) or self.node(self.default_node_id())
            outcome = TickOutcome(session_id=ctx.session_id)
            outcome.place = ctx.session_id
            await self._log_event(
                state,
                "help",
                {
                    "title": package.title,
                    "stage": "reply",
                    # 原文 + 保留条数：只写"摘要"时，全被丢掉就会剩一条空记录
                    "text": _clip_log(" / ".join(messages)),
                    "kept": len(list(getattr(suggestions, "related", []) or [])),
                    "dropped": len(list(getattr(suggestions, "irrelevant", []) or [])),
                    "digest": suggestions.digest(),
                },
                outcome=outcome,
            )
            await self._log_event(
                state,
                "help",
                {
                    "title": package.title,
                    "stage": "resolve",
                    "note": "她把建议听进去了，重新拿主意",
                },
                outcome=outcome,
            )
            await self._run_event_step(
                state,
                node,
                outcome,
                thread,
                package,
                second_round=True,
                suggestions=suggestions,
            )
            return ReplyOutcome(
                ok=True,
                messages=list(outcome.messages),
                debug_messages=list(outcome.debug_messages),
                debug_positions=list(outcome.debug_positions),
            )

    # ---------------- 简易人设（按人格缓存） ----------------

    @staticmethod
    def _persona_brief_key(persona: str) -> str:
        """用主人设的指纹当键：人设一改，键就变了，自然"提示重新生成"。"""

        import hashlib

        body = " ".join(str(persona or "").split())
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
        return f"persona_brief.{digest}"

    PERSONA_BRIEF_LATEST = "persona_brief.latest"
    """最近一次保存的简易人设（不按人设指纹分）。

    指纹变了（换人格、换了会话读出的人设不一样）时还能把它拿出来给用户看，
    不然编辑器里那一段直接变空，看着就像"重装插件被重置了"。
    """

    async def persona_brief_state(self, session_id: str) -> dict[str, Any]:
        """当前人格的简易人设：内容 + 是不是生成出来的 + 主人设长度。"""

        persona = await self._persona_text(session_id)
        key = self._persona_brief_key(persona)
        payload = None
        try:
            payload = await self.db.call("kv_get", key)
        except Exception:
            payload = None
        data = payload if isinstance(payload, dict) else {}
        return {
            "brief": str(data.get("brief") or "").strip(),
            "source": str(data.get("source") or ""),
            "at": float(data.get("at") or 0.0),
            "key": key,
            "persona_chars": len(persona or ""),
            # 指纹对不上时编辑器还能拿这份兜底（并提示"建议重新生成"）
            "latest": await self._latest_persona_brief(),
        }

    async def _latest_persona_brief(self) -> dict[str, Any]:
        try:
            payload = await self.db.call("kv_get", self.PERSONA_BRIEF_LATEST)
        except Exception:
            return {}
        data = payload if isinstance(payload, dict) else {}
        brief = str(data.get("brief") or "").strip()
        if not brief:
            return {}
        return {
            "brief": brief,
            "source": str(data.get("source") or ""),
            "at": float(data.get("at") or 0.0),
            "key": str(data.get("key") or ""),
        }

    async def save_persona_brief(
        self,
        session_id: str,
        brief: str,
        *,
        source: str = "manual",
        persona_text: str = "",
    ) -> str:
        """存下这份简易人设。

        按"当时用的那份人设"的指纹存：向导里用草稿生成时也按草稿存，
        等用户把草稿保存成正式人设，指纹就对得上，不会一保存就变"建议重新生成"。
        """

        persona = str(persona_text or "").strip() or await self._persona_text(session_id)
        key = self._persona_brief_key(persona)
        text = " ".join(str(brief or "").split())[:800]
        record = {"brief": text, "source": source, "at": self._now()}
        try:
            await self.db.call("kv_set", key, record)
            if text:
                # 最近一份：换了人格 / 换了会话时编辑器还能把它捞出来
                await self.db.call(
                    "kv_set", self.PERSONA_BRIEF_LATEST, {**record, "key": key}
                )
        except Exception as exc:
            self._log("warning", f"简易人设存不进去：{exc}")
        return text

    async def generate_persona_brief(self, session_id: str, *, persona_text: str = "") -> str:
        """用生成器模型把主人设压成一段摘要。

        ``persona_text``：编辑器里刚改过、还没保存的角色卡。传了就用它——
        向导里刚写完人设就点"生成"，读配置只会拿到旧人设，生成的摘要跟眼前这份对不上。
        """

        persona = str(persona_text or "").strip() or await self._persona_text(session_id)
        if not persona.strip():
            return ""
        limit = int(getattr(self.world.events, "persona_brief_chars", 250) or 250)
        system, prompt = self.prompts.build_persona_brief_prompt(
            persona_text=persona, limit=limit
        )
        brief = await self._ask_creator(session_id, system, prompt) or ""
        brief = " ".join(brief.split())[: max(80, limit * 2)]
        if brief:
            await self.save_persona_brief(
                session_id, brief, source="generated", persona_text=persona
            )
        return brief

    async def event_persona_brief(self, session_id: str) -> str:
        """打杂模型用的那份人设：生成过的优先，否则退回主人设前 200 字。"""

        state = await self.persona_brief_state(session_id)
        brief = str(state.get("brief") or "").strip()
        if brief:
            return brief
        persona = await self._persona_text(session_id)
        return " ".join(str(persona or "").split())[:200]

    # ---------------- 声音样例 ----------------

    def persona_samples(self) -> list[dict[str, Any]]:
        """挑中的样例（配置里存的，跟预设走）。"""

        raw = list(getattr(self.world.persona, "samples", None) or [])
        return [dict(item) for item in raw if isinstance(item, dict) and str(item.get("text") or "").strip()]

    async def run_eval(
        self,
        session_id: str,
        rounds: list[dict[str, Any]],
        *,
        llm: Any = None,
        concurrency: int = 3,
        user_name: str = "测试者",
        variant: str = "full",
        director_llm: Any = None,
    ) -> dict[str, Any]:
        """把剧本逐轮喂给"带着完整提示词的她"，**并发**收集她的真实回复。

        和真机对话的区别只有一个：**不写任何状态**——水位线、记忆、心情、动作都不动，
        所以可以反复跑、跑给它十遍也不会污染她的世界。它测的正是"这套提示词 + 这个模型"
        在这一轮会说出什么。

        ``variant="lean"`` 时用 :func:`core.prompt.lean_prompt` 剥掉劝导语，
        用来做"提示词该不该减负"的 A/B。

        ``variant="director"`` 时先让便宜模型写一份 3~6 行人话简报，主人格只拿
        「人设 + 简报 + 最近三句 + 最小格式」——用来验证"决策层能不能顶替那一万六千字"。
        """

        model = llm or self.llm
        if model is None:
            return {"ok": False, "reason": "没有可用的模型"}
        if not rounds:
            return {"ok": False, "reason": "没有要问的问题"}
        state = await self.load_state(session_id, cold_start=False)
        node = self.node(state.node_id)
        persona = await self._persona_text(session_id)
        # 画像那一段要有人才渲染：用这个会话最近说话的人，和真机一致
        actives = state.recent_active_users(1)
        who_id = str((actives[0] or {}).get("user_id") or "") if actives else ""
        who_name = str((actives[0] or {}).get("name") or "") if actives else ""
        if who_id:
            profile_text = self.profile_block(
                state,
                SimpleNamespace(session_id=session_id, user_id=who_id, user_name=who_name),
            )
        else:
            profile_text = self.profile_block(state)
        limit = max(1, min(8, int(concurrency or 3)))
        gate = asyncio.Semaphore(limit)
        recent_chat = self.chat_window(state)

        async def one(
            index: int, item: dict[str, Any], prev_analysis: str = ""
        ) -> dict[str, Any]:
            text = " ".join(str(item.get("user_text") or "").split())
            row = {
                "index": index + 1,
                "scene": str(item.get("scene") or ""),
                "user_text": text,
                "watch": str(item.get("watch") or ""),
                "taboo": str(item.get("taboo") or ""),
                "reply": [],
                "raw": "",
                "error": "",
            }
            if not text:
                row["error"] = "这一轮没问题文本"
                return row
            async with gate:
                try:
                    _cell, say_limit, style_text = self.style_for(state, session_id)
                    system_prompt = self.prompts.build_autonomous_system_prompt(
                        persona_text=persona,
                        state=state,
                        node=node,
                        available_tools=self.available_tools(),
                        memories=self.memory.recall(
                            session_id=session_id,
                            persona_id="",
                            node_id=state.node_id,
                            limit=self.world.limits.max_think_memory,
                        ),
                        engagement_hint=self.engagement.hint(state),
                        max_messages=say_limit,
                        recent_chat=list(recent_chat),
                        reasoning=bool(self.world.reasoning_enabled),
                        style_block=style_text,
                        session_directory=self.session_directory(state),
                        current_session=session_id,
                        session_labels=self.session_labels(state),
                        profile_text=profile_text,
                        samples=self.voice_sample_lines(
                            state, session_id, preview=True
                        ),
                        # 测评也要跟实跑完全一致：该有的字段一个都不能少
                        wants_touch=self.extensions.wants("touch"),
                        json_fields=self.extensions.json_fields(),
                        hidden_actions=self.exhausted_actions(state),
                        **await self.runtime_notes(session_id),
                    )
                    user_prompt = self.prompts.build_reply_user_prompt(
                        user_name=user_name,
                        text=text,
                        is_private=True,
                    )
                    if variant == "lean":
                        system_prompt = lean_prompt(system_prompt)
                    elif variant == "analyst":
                        channel = director_llm or self.judge_llm or self.helper_llm
                        analysis = ""
                        if channel is not None:
                            a_system, a_user = self.prompts.build_analyst_prompt(
                                state=state,
                                node=node,
                                user_text=text,
                                user_name=user_name,
                                recent_chat=recent_chat,
                                profile_text=profile_text,
                                memories=self.memory.recall(
                                    session_id=session_id,
                                    persona_id="",
                                    node_id=state.node_id,
                                    limit=self.world.limits.max_think_memory,
                                ),
                                other_context=str(
                                    getattr(state, "last_other_context", "") or ""
                                ),
                                open_topics_text=self.prompts.open_topics_layer(
                                    self.prompts._due_open_topics(state, session_id)
                                ),
                                prev_analysis=prev_analysis,
                                pronoun=pronounce_for(self.world.gender),
                            )
                            try:
                                a_reply = await channel.generate(
                                    session_id=session_id,
                                    system_prompt=a_system,
                                    prompt=a_user,
                                    temperature=0.2,
                                )
                                analysis = str(getattr(a_reply, "text", "") or "").strip()
                            except Exception as exc:
                                row["director_error"] = f"{type(exc).__name__}: {exc}"
                        row["analysis"] = analysis
                        extra = self.prompts.analyst_layer(
                            analysis, pronoun=pronounce_for(self.world.gender)
                        )
                        if extra:
                            # 补充而不是替换：原始信息照旧，分析贴在最后（近因位置）
                            system_prompt = f"{system_prompt}\n\n{extra}"
                    elif variant == "director":
                        brief = ""
                        channel = director_llm or self.judge_llm or self.helper_llm
                        if channel is not None:
                            d_system, d_user = self.prompts.build_director_prompt(
                                persona_text=persona,
                                state=state,
                                node=node,
                                recent_chat=recent_chat,
                                profile_text=profile_text,
                                open_topics_text=self.prompts.open_topics_layer(
                                    self.prompts._due_open_topics(state, session_id)
                                ),
                                style_block=style_text,
                                pronoun=pronounce_for(self.world.gender),
                            )
                            try:
                                d_reply = await channel.generate(
                                    session_id=session_id,
                                    system_prompt=d_system,
                                    prompt=d_user,
                                    temperature=0.2,
                                )
                                brief = str(getattr(d_reply, "text", "") or "").strip()
                            except Exception as exc:
                                row["director_error"] = f"{type(exc).__name__}: {exc}"
                        row["brief"] = brief
                        system_prompt = self.prompts.build_director_reply_system(
                            persona_text=persona,
                            brief=brief or "照常回一句。",
                            samples=self.voice_sample_lines(
                                state, session_id, preview=True
                            ),
                            recent_chat=recent_chat,
                            pronoun=pronounce_for(self.world.gender),
                        )
                    row["prompt_chars"] = len(system_prompt)
                    reply = await model.generate(
                        session_id=session_id,
                        system_prompt=system_prompt,
                        prompt=user_prompt,
                    )
                    raw = str(getattr(reply, "text", "") or "")
                    row["raw"] = raw[:2000]
                    # 动作这条线要和真机一致：用**真实白名单**解析，
                    # 这样"她写了个现在做不了的动作"才会被丢掉并记下来
                    allowed_ids = self._parseable_action_ids(state.node_id)
                    parsed = parse_action_payload(
                        raw,
                        available_actions=allowed_ids,
                        valid_nodes=set(self.world.node_map()),
                        max_actions=self.world.limits.max_actions_per_message,
                        max_messages=say_limit,
                        json_fields=self.extensions.json_fields(),
                    )
                    for action in parsed.actions:
                        if action.type == "say" and action.messages:
                            row["reply"].extend(action.messages)
                    if not row["reply"]:
                        spoken = speakable_messages(
                            extract_json_object(raw) or {}
                        )
                        if spoken:
                            row["reply"] = spoken[:say_limit]
                        elif raw.strip() and not raw.lstrip().startswith("{"):
                            row["reply"] = [raw.strip()[:200]]
                    if parsed.reasoning:
                        row["reasoning"] = parsed.reasoning
                    row["actions"] = [action.type for action in parsed.actions]
                    row["tools"] = [
                        {"action": action.type, "intent": str(action.intent or "")[:80]}
                        for action in parsed.actions
                        if str(action.intent or "").strip()
                    ]
                    row["actions_dropped"] = [
                        item
                        for item in (parsed.warnings or [])
                        if "动作" in str(item) or "action" in str(item).lower()
                    ][:4]
                    # 提示词里那一轮能用哪些动作，也记一份：方便看"她是不是没用到手边的能力"
                    row["actions_available"] = sorted(
                        str(item) for item in allowed_ids if str(item) != "say"
                    )[:20]
                except Exception as exc:  # 一轮挂了不该让整份考卷作废
                    row["error"] = f"{type(exc).__name__}: {exc}"
            return row

        if variant == "analyst":
            # 分析层要能看到"上一轮说过什么"，所以按顺序跑（其余分支是并发的）
            results = []
            last = ""
            for index, item in enumerate(rounds):
                row = await one(index, item, prev_analysis=last)
                last = str(row.get("analysis") or last)
                results.append(row)
        else:
            results = list(
                await asyncio.gather(
                    *(one(index, item) for index, item in enumerate(rounds))
                )
            )
        return {
            "ok": True,
            "count": len(results),
            "concurrency": limit,
            "results": sorted(results, key=lambda item: item["index"]),
        }

    async def generate_eval_script(self, session_id: str, rounds: int = 20) -> dict[str, Any]:
        """生成测评剧本（**不进提示词**，只给用户拿去比模型）。"""

        persona = self._persona_text_now()
        if not persona.strip():
            return {"ok": False, "reason": "还没有角色卡：先在「她」那一页填上，或从 AstrBot 导入一份"}
        system, prompt = self.prompts.build_eval_script_prompt(
            persona_text=persona,
            pronoun=pronounce_for(self.world.gender),
            rounds=rounds,
        )
        raw = await self._ask_creator(session_id, system, prompt)
        if not raw:
            return {"ok": False, "reason": "生成模型没有返回内容（检查插件配置里的「内容生成模型」）"}
        payload = extract_json_object(raw)
        if not isinstance(payload, dict):
            return {"ok": False, "reason": "模型没给出 JSON", "raw": raw[:600]}
        rows = []
        for item in payload.get("rounds") or []:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("user_text") or "").split())
            if not text:
                continue
            rows.append(
                {
                    "scene": " ".join(str(item.get("scene") or "").split())[:20],
                    "user_text": text[:200],
                    "watch": " ".join(str(item.get("watch") or "").split())[:120],
                    "taboo": " ".join(str(item.get("taboo") or "").split())[:120],
                }
            )
        if not rows:
            return {"ok": False, "reason": "模型没给出可用的剧本", "raw": raw[:600]}
        return {"ok": True, "rounds": rows, "count": len(rows)}

    def _persona_text_now(self) -> str:
        """插件自己那份角色卡正文（优化人设动的是它）。"""

        return str(getattr(self.world.persona, "text", "") or "")

    async def review_persona(self, session_id: str) -> dict[str, Any]:
        """体检 + 给可逐条采纳的改动（**不落库**：人设是用户最在意的东西）。"""

        persona = self._persona_text_now()
        if not persona.strip():
            return {
                "ok": False,
                "reason": "还没有插件自己的角色卡：先在「她」那一页填上，或从 AstrBot 导入一份",
            }
        length_hint = f"改完总长控制在原文的 {int(len(persona) * 0.8)}~{int(len(persona) * 1.5)} 字之间"
        system, prompt = self.prompts.build_persona_review_prompt(
            persona_text=persona,
            pronoun=pronounce_for(self.world.gender),
            length_hint=length_hint,
        )
        raw = await self._ask_creator(session_id, system, prompt)
        if not raw:
            return {"ok": False, "reason": "生成模型没有返回内容（检查插件配置里的「内容生成模型」）"}
        payload = extract_json_object(raw)
        if not isinstance(payload, dict):
            return {"ok": False, "reason": "模型没给出 JSON", "raw": raw[:600]}

        def clean(value: Any, limit: int = 200) -> str:
            return " ".join(str(value or "").split())[:limit]

        issues = []
        for item in payload.get("issues") or []:
            if not isinstance(item, dict):
                continue
            detail = clean(item.get("detail"))
            if not detail:
                continue
            issues.append(
                {
                    "level": clean(item.get("level"), 20) or "建议改",
                    "kind": clean(item.get("kind"), 20) or "其他",
                    "detail": detail,
                    "quote": clean(item.get("quote"), 120),
                }
            )
        rewrites = []
        for item in payload.get("rewrite") or []:
            if not isinstance(item, dict):
                continue
            before = str(item.get("before") or "").strip()
            after = str(item.get("after") or "").strip()
            # 逐条替换的前提是 before 能在原文里逐字找到——找不到就不让它上桌
            if not before or not after or before not in persona:
                continue
            rewrites.append(
                {
                    "before": before,
                    "after": after,
                    "why": clean(item.get("why"), 20),
                    "found": True,
                }
            )
        adds = []
        for item in payload.get("add") or []:
            if not isinstance(item, dict):
                continue
            text = clean(item.get("text"), 300)
            if not text:
                continue
            adds.append(
                {
                    "field": clean(item.get("field"), 20) or "补充",
                    "text": text,
                    "why": clean(item.get("why"), 20),
                }
            )
        return {
            "ok": True,
            "persona_chars": len(persona),
            "length_hint": length_hint,
            "ok_points": [clean(item) for item in (payload.get("ok") or []) if clean(item)],
            "issues": issues,
            "rewrite": rewrites,
            "add": adds,
            "questions": [clean(item) for item in (payload.get("questions") or []) if clean(item)],
        }

    def apply_persona_changes(
        self, rewrites: list[dict[str, Any]] | None = None, adds: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """把**你点过**的改动写进角色卡（写前留历史；找不到原文的条目跳过并报出来）。"""

        persona = self._persona_text_now()
        if not persona.strip():
            return {"ok": False, "reason": "角色卡是空的，没什么可改"}
        applied: list[str] = []
        skipped: list[str] = []
        text = persona
        for item in rewrites or []:
            if not isinstance(item, dict):
                continue
            before = str(item.get("before") or "")
            after = str(item.get("after") or "")
            if not before or not after:
                continue
            if before not in text:
                skipped.append(f"找不到这段原文：{before[:20]}")
                continue
            text = text.replace(before, after, 1)
            applied.append(f"改写：{before[:16]} → {after[:16]}")
        appended: list[str] = []
        for item in adds or []:
            if not isinstance(item, dict):
                continue
            chunk = " ".join(str(item.get("text") or "").split())
            if not chunk:
                continue
            field = " ".join(str(item.get("field") or "").split()) or "补充"
            appended.append(f"{field}：{chunk}")
        if appended:
            text = text.rstrip() + "\n\n" + "\n".join(f"- {item}" for item in appended)
            applied.extend(f"新增：{item[:24]}" for item in appended)
        if not applied:
            return {"ok": False, "reason": "没有可应用的改动", "skipped": skipped}
        world = self.store.raw_world()
        persona_raw = dict(world.get("persona") or {})
        persona_raw["text"] = text
        world["persona"] = persona_raw
        warnings = self.store.save_world(world, reason="优化人设")
        self.reload_config()
        return {
            "ok": True,
            "applied": applied,
            "skipped": skipped,
            "warnings": warnings,
            "chars": len(text),
        }

    async def generate_voice_samples(
        self,
        session_id: str,
        scenes: list[str] | None = None,
        *,
        persona_text: str = "",
    ) -> dict[str, Any]:
        """让内容生成模型按场景出候选（**不落库**，交给编辑器挑）。

        生成物一律先进草稿：样例是要长期占提示词位置的，不能被模型自己写进去。

        ``persona_text`` 同 :meth:`generate_persona_brief`：编辑器里还没保存的角色卡优先。
        """

        persona = str(persona_text or "").strip() or await self._persona_text(session_id)
        if not persona.strip():
            return {"ok": False, "reason": "还没有人设：先去「她」那一页填角色卡或从 AstrBot 导入"}
        system, prompt = self.prompts.build_voice_sample_prompt(
            persona_text=persona,
            pronoun=pronounce_for(self.world.gender),
            name=str(getattr(self.world, "bot_name", "") or ""),
            scenes=scenes,
        )
        raw = await self._ask_creator(session_id, system, prompt)
        if not raw:
            return {"ok": False, "reason": "生成模型没有返回内容（检查插件配置里的「内容生成模型」）"}
        payload = extract_json_object(raw)
        scenes_out: list[dict[str, Any]] = []
        known = {key: label for key, label in self.prompts.VOICE_SCENES}
        # 模型可能只回「被撩」这样的短名：用标签开头那一段当别名来认
        alias_to_key = {}
        for key, label in known.items():
            head = label.split("：")[0].split("，")[0].strip()
            if head:
                alias_to_key[head] = key
            alias_to_key[label] = key
        if isinstance(payload, dict):
            for item in payload.get("scenes") or []:
                if not isinstance(item, dict):
                    continue
                scene_label = " ".join(str(item.get("scene") or "").split())
                key = next(
                    (
                        value
                        for alias, value in alias_to_key.items()
                        if alias and (alias in scene_label or scene_label in alias)
                    ),
                    "",
                )
                candidates = []
                for cand in item.get("candidates") or []:
                    if not isinstance(cand, dict):
                        continue
                    text = " ".join(str(cand.get("text") or "").split())
                    if not text:
                        continue
                    candidates.append(
                        {
                            "text": text[:120],
                            "move": " ".join(str(cand.get("move") or "").split())[:20],
                            "why": " ".join(str(cand.get("why") or "").split())[:20],
                        }
                    )
                if candidates:
                    scenes_out.append(
                        {
                            "scene": key,
                            "label": scene_label or known.get(key, ""),
                            "candidates": candidates[:4],
                        }
                    )
        if not scenes_out:
            return {"ok": False, "reason": "模型没给出可用的候选", "raw": raw[:600]}
        return {"ok": True, "scenes": scenes_out}

    TONE_SIGNALS: dict[str, str] = {
        "praise": "positive",
        "attack": "negative",
        "hug": "hug",
        "comfort": "comfort",
        "positive": "positive",
        "negative": "negative",
    }
    """主模型判的口吻 → 声音样例的场景组。``normal`` / 空不映射。"""

    def tone_signal(self, state: WorldState, text: str = "") -> str:
        """这一轮该按哪种场景抽样例。

        先用上一轮主模型判的口吻（它认得反话），没有再用关键词表兜底
        （"你可真行啊"这种反话，词表一定会认错）。
        """

        mapped = self.TONE_SIGNALS.get(str(getattr(state, "last_user_tone", "") or ""))
        if mapped:
            return mapped
        return keyword_signal(text)

    def voice_sample_lines(
        self,
        state: WorldState,
        session_id: str,
        *,
        signal: str = "",
        preview: bool = False,
    ) -> list[dict[str, Any]]:
        """这一轮抽哪几条样例进去：先按场景对，再轮换，避免连着两轮同一组。

        ``signal`` 是**这一轮该按哪种场景说话**（``hug`` / ``negative`` / ``positive``），
        由 ``tone_signal`` 算出来：主模型上一轮判的口吻最准，没有才退回关键词表。
        ``preview=True`` 时只算不记账——预览不该把轮换状态往前推。
        """

        samples = self.persona_samples()
        if not samples:
            return []
        limit = max(1, int(getattr(self.world.persona, "samples_per_turn", 3) or 3))
        prefer: tuple[str, ...] = ()
        if signal == "hug":
            prefer = ("tease",)
        elif signal == "negative":
            prefer = ("snap", "ignored")
        elif signal == "positive":
            prefer = ("comfort", "tease")
        wanted = [item for item in samples if str(item.get("scene") or "") in prefer]
        rest = [item for item in samples if item not in wanted]
        # 轮换：上一轮用过的排到后面，省得连着两轮同一组
        used = set((state.last_voice_samples or []) if isinstance(
            getattr(state, "last_voice_samples", None), list
        ) else [])
        def order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            fresh = [item for item in items if str(item.get("id") or item.get("text") or "") not in used]
            stale = [item for item in items if str(item.get("id") or item.get("text") or "") in used]
            return [*fresh, *stale]

        picked = [*order(wanted), *order(rest)][:limit]
        if not preview:
            state.last_voice_samples = [
                str(item.get("id") or item.get("text") or "") for item in picked
            ]
        return picked

    # ---------------- 对外接口（指令 / 编辑器 / 意图路由） ----------------

    def voice_sample_candidates_from_chat(
        self, state: WorldState, *, limit: int = 12
    ) -> list[dict[str, Any]]:
        """从近期留档里挑"她自己说过、而且被接住了"的话，当样例候选。

        只挑被接住的：没人理的那几句进了样例库，等于把她教成"没人理也要继续说"。
        每条都带上当时对方说的那一句，用户才知道这句话该不该学。
        """

        rows = [item for item in list(state.recent_chat or []) if isinstance(item, dict)]
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(rows):
            if not item.get("is_self"):
                continue
            name = str(item.get("name") or "")
            if name.startswith("（") or item.get("internal"):
                # 插件自己写的"她身上发生的事"，不是她说的话
                continue
            text = " ".join(str(item.get("text") or "").split())
            if not text or len(text) > 60:
                continue
            if any(mark in text for mark in ("[图片]", "[表情", "［图片", "［这条消息", "（")):
                continue
            origin = str(item.get("origin") or "")
            seq = int(item.get("seq") or 0)
            # 后面得有人说话，才算"被接住了"
            answered = any(
                str(later.get("origin") or "") == origin
                and not later.get("is_self")
                and not later.get("internal")
                and int(later.get("seq") or 0) > seq
                for later in rows[index + 1 :]
            )
            if not answered:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            # 上一句别人说的话：当作这条样例的上下文
            context = ""
            for earlier in reversed(rows[:index]):
                if str(earlier.get("origin") or "") != origin:
                    continue
                if earlier.get("is_self") or earlier.get("internal"):
                    continue
                context = " ".join(str(earlier.get("text") or "").split())[:40]
                break
            signal = keyword_signal(context)
            scene = {"hug": "tease", "negative": "snap", "positive": "comfort"}.get(
                signal, ""
            )
            out.append(
                {
                    "id": f"chat{len(out) + 1}",
                    "scene": scene,
                    "label": "",
                    "move": "",
                    "text": text[:120],
                    "context": context,
                    "source": "chat",
                }
            )
            if len(out) >= max(1, int(limit)):
                break
        return out

    async def pending_intervention(self, session_id: str) -> float:
        """她正在等协助吗？返回结束时间戳（0 = 不在等）。

        给「意图路由」用：这段时间里它会合并放行、免冷却，把群友的回应尽快交过来。
        """

        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return 0.0
        info = state.pending_help or {}
        if str(info.get("state") or "") != "active":
            return 0.0
        try:
            until = float(info.get("active_until") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return until if until > self._now() else 0.0

    async def submit_event(self, session_id: str, text: str) -> str:
        """用户用指令投递一个事件（等于"强行掷骰命中"）。返回一句说明。"""

        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return "事件系统在全局设置里关着"
        seed = " ".join(str(text or "").split())[:300]
        if not seed:
            return "要给她什么事？例如：/vw event 出门忘了带伞"
        if not self.is_enabled(session_id):
            return "这个会话还没启用虚拟世界"
        outcome: TickOutcome | None = None
        note = ""
        async with self.session_state(session_id) as state:
            self._ensure_abilities(state)
            now = self._event_time()
            # 忙就直说，**不排队**：排队会让人以为生效了，其实一直没发生
            blocked = self._event_block_reason(state, now, manual=True)
            active = self._active_thread(state)
            if blocked:
                note = f"这次没安排上：{blocked}。等她闲下来再投一次。"
            elif active is not None:
                title = str(active.get("title") or "一件事")
                note = f"她手上还有一件事没完（{title}）。等它收尾再投新的吧。"
            else:
                node = self.node(state.node_id) or self.node(self.default_node_id())
                outcome = TickOutcome(session_id=session_id)
                outcome.place = session_id
                state.event_recent_titles = list(state.event_recent_titles or [])
                await self._start_event(
                    state, node, outcome, tier="small", seed=seed, source="user"
                )
        if outcome is not None:
            await self._deliver(outcome)
        return note

    async def advance_thread(self, session_id: str, thread_id: str = "") -> str:
        """手动把没完的那件事往前推一幕（编辑器上的「立即推进」）。

        正常节奏是等 ``step_gap_seconds`` 到了才续演；这里就是"别等了，现在就来"。
        非要等群友拿主意的那件不推——那时候该等的是人，不是时钟。
        """

        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return "事件系统在全局设置里关着"
        if not self.is_enabled(session_id):
            return "这个会话还没启用虚拟世界"
        outcome: TickOutcome | None = None
        note = ""
        async with self.session_state(session_id) as state:
            now = self._event_time()
            thread = self._thread_by_id(state, thread_id) if thread_id else self._active_thread(state)
            if thread is None:
                note = "她现在没有没完的事"
            elif str(thread.get("status") or "open") != "open":
                note = "这件事已经完结了"
            elif self._thread_waiting_help(state, thread):
                note = "这件事她在等群友拿主意，先别替她往下演"
            elif not str(thread.get("pending_followup") or "").strip():
                note = "这件事没有下一步了"
            elif self._is_asleep(state) and not bool(getattr(config, "in_sleep", False)):
                note = "她在睡觉，先叫醒再推进这件事"
            else:
                title = str(thread.get("title") or "一件事")
                node = self.node(state.node_id) or self.node(self.default_node_id())
                outcome = TickOutcome(session_id=session_id)
                outcome.place = session_id
                # 把"到点时间"挪到现在：续演那条路只认时间，不改它就推不动
                thread["next_step_at"] = float(now)
                advanced = await self._continue_thread(
                    state, node, outcome, now, thread_id=str(thread.get("id") or "")
                )
                if advanced:
                    note = f"推进了一幕：{title}"
                elif str(thread.get("status") or "") == "closed":
                    note = f"这件事挂太久了，已经收尾：{title}"
                else:
                    note = f"这次没推进：{title}（她可能正忙着，或者这件事已经收尾）"
        if outcome is not None:
            await self._deliver(outcome)
        return note

    async def close_thread(self, session_id: str, thread_id: str = "") -> str:
        """手动完结一件事（编辑器上的「立刻完结」）。

        她不再往下演：线索标成已完结、等着的那条也放掉，记忆里留一句收尾。
        """

        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return "事件系统在全局设置里关着"
        if not self.is_enabled(session_id):
            return "这个会话还没启用虚拟世界"
        async with self.session_state(session_id) as state:
            thread = self._thread_by_id(state, thread_id) if thread_id else self._active_thread(state)
            if thread is None:
                return "她现在没有正在进行的事"
            if str(thread.get("status") or "open") != "open":
                return "这件事已经完结了"
            now = self._event_time()
            title = str(thread.get("title") or "一件事")
            thread["status"] = "closed"
            thread["closed_at"] = float(now)
            thread["pending_followup"] = ""
            thread["next_step_at"] = 0.0
            # 正在等群友回话的那条一起放掉：不然下一次有人说话还会被她当成"在帮忙"
            info = dict(state.pending_help or {})
            if str(info.get("thread_id") or "") == str(thread.get("id") or ""):
                state.pending_help = {}
            content = f"（{title}）这件事就到这里了"
            settled = str(thread.get("line") or "").strip()
            if settled:
                content = f"{settled}；这件事就到这里了"[:220]
            self.memory.remember(
                session_id=state.session_id,
                persona_id="",
                node_id=str(thread.get("place") or state.node_id),
                content=content,
                memory_type=SCENE,
                emotion=state.mood,
                weight=0.3,
                affect=state.affect,
                valence=state.valence,
            )
            await self._log_event(
                state,
                "event_idle",
                {"title": title, "stage": "stopped"},
            )
            return f"已完结：{title}"

    def _thread_waiting_help(self, state: WorldState, thread: dict[str, Any]) -> bool:
        """这条线索是不是正等着群友拿主意。"""

        info = state.pending_help or {}
        if str(info.get("state") or "") not in ("active", "idle"):
            return False
        return str(info.get("thread_id") or "") == str(thread.get("id") or "")

    async def event_overview(self, session_id: str) -> dict[str, Any]:
        """给编辑器与指令看的事件概览：能力值、当前这件事、以及最近的线索。

        每条线索都带上**每一幕发生了什么**（她选了什么、判定如何、结果怎样），
        编辑器的「历史事件」弹窗直接照着渲染，不用再拼日志。
        """

        state = await self.load_state(session_id, cold_start=False)
        values = normalize_abilities(state.abilities or self.world.abilities.initial)
        threads = [item for item in list(state.event_threads or []) if isinstance(item, dict)]
        opened = [item for item in threads if str(item.get("status") or "open") == "open"]
        info = state.pending_help or {}
        now = self._event_time()
        active = self._active_thread(state)
        active_id = str((active or {}).get("id") or "")
        help_thread_id = str(info.get("thread_id") or "")
        help_state = str(info.get("state") or "")
        return {
            "abilities": {
                name: {
                    "value": round(values[name], 3),
                    "label": ABILITY_LABELS[name],
                    "hint": ability_hint(values[name]),
                }
                for name in ABILITIES
            },
            "ability_day": state.ability_day,
            "ability_spent_today": dict(state.ability_spent_today or {}),
            "enabled": bool(getattr(self.world.events, "enabled", True)),
            "now": float(now),
            "world_time": int(state.world_time),
            "pending_help": {
                "state": help_state,
                "title": str(info.get("title") or ""),
                "until": float(info.get("active_until") or info.get("idle_until") or 0.0),
                "thread_id": help_thread_id,
            },
            "active_id": active_id,
            "threads": [
                self._thread_overview(
                    state,
                    item,
                    now=now,
                    active_id=active_id,
                    help_thread_id=help_thread_id,
                    help_state=help_state,
                )
                for item in threads[::-1]
            ],
            "open_count": len(opened),
            "recent_titles": list(state.event_recent_titles or []),
        }

    def _thread_overview(
        self,
        state: WorldState,
        thread: dict[str, Any],
        *,
        now: float,
        active_id: str,
        help_thread_id: str,
        help_state: str,
    ) -> dict[str, Any]:
        """一条线索给编辑器看的细节（含每一幕）。"""

        root = thread.get("root") or {}
        status = str(thread.get("status") or "open")
        tier = str(thread.get("tier") or "")
        steps: list[dict[str, Any]] = []
        for index, step in enumerate(list(thread.get("steps") or []), start=1):
            if not isinstance(step, dict):
                continue
            steps.append(
                {
                    "index": index,
                    "at": float(step.get("at") or 0.0),
                    "desc": str(step.get("desc") or ""),
                    "ability": str(step.get("ability") or ""),
                    "tier": str(step.get("tier") or ""),
                    "tier_label": str(
                        step.get("tier_label")
                        or TIER_LABELS.get(str(step.get("tier") or ""), "")
                    ),
                    "result": str(step.get("result") or ""),
                    "ability_delta": {
                        ABILITY_LABELS.get(str(name), str(name)): round(float(value), 3)
                        for name, value in dict(step.get("ability_delta") or {}).items()
                    },
                    "suggestions": int(step.get("suggestions") or 0),
                }
            )
        thread_id = str(thread.get("id") or "")
        waiting_help = status == "open" and thread_id == help_thread_id and help_state in ("active", "idle")
        can_advance = (
            status == "open"
            and bool(str(thread.get("pending_followup") or "").strip())
            and not waiting_help
        )
        return {
            "id": thread_id,
            "title": str(thread.get("title") or "一件事"),
            "hook": str(root.get("hook") or ""),
            "status": status,
            "tier": tier,
            "tier_label": str(TIER_LABELS.get(tier, "")),
            "kind": str(root.get("kind") or ""),
            "critical": bool(root.get("critical")),
            "imagined": str(root.get("mode") or "") == MODE_IMAGINED,
            "place": str(thread.get("place") or ""),
            "place_name": str(thread.get("place_name") or ""),
            "genre": str(thread.get("genre") or ""),
            "opened_at": float(thread.get("opened_at") or 0.0),
            "updated_at": float(thread.get("updated_at") or 0.0),
            "closed_at": float(thread.get("closed_at") or 0.0),
            "line": str(thread.get("line") or ""),
            "pending_followup": str(thread.get("pending_followup") or ""),
            "next_step_at": float(thread.get("next_step_at") or 0.0),
            "steps": steps,
            "step_count": len(steps),
            "is_active": thread_id == active_id,
            "waiting_help": waiting_help,
            "suspended": status == "open"
            and self._thread_suspended(state, thread, now)
            and not waiting_help,
            "can_advance": can_advance,
            "can_close": status == "open",
        }



    # ---------------- 睡觉时的门禁 ----------------

    def _is_asleep(self, state: WorldState) -> bool:
        """她现在算不算「睡着了」（小睡按配置决定要不要一起算）。"""

        if state.state == STATE_NAPPING:
            return bool(self.world.sleep.applies_to_nap)
        return state.state == STATE_SLEEPING

    def is_asleep(self, state: WorldState) -> bool:
        """对外暴露的「她睡着了吗」（联动方用，例如意图路由直接不判断）。"""

        return self._is_asleep(state)

    def _wake_hit(self, ctx: MessageContext) -> bool:
        """这条消息算不算「明确叫她起来」。"""

        words = [
            str(word).strip()
            for word in (self.world.sleep.wake_words or [])
            if str(word).strip()
        ]
        if not words:
            return False
        if self.world.sleep.wake_requires_mention and not (ctx.is_mentioned or ctx.is_private):
            return False  # 没 @ 她的闲聊不能把她弄醒
        text = ctx.text or ""
        return any(word in text for word in words)

    def _wake_note_text(self, ctx: MessageContext) -> str:
        """被叫醒时给提示词的一句话。

        重点是别让她"只顾着醒"：同一条消息里交代的事也要安排成动作。
        """

        note = (
            "你刚被叫醒，可以迷糊一点；但对方如果在同一条消息里交代了要做的事，"
            "先把那件事写成动作，再回一句话。"
        )
        if self._wake_leftover(ctx.text or ""):
            note += "对方不只是叫你起来，还交代了事情。"
        return note

    def _wake_leftover(self, text: str) -> str:
        """去掉唤醒词和标点之后，这条消息还剩什么（用来判断是不是还交代了事）。"""

        result = text or ""
        for word in self.world.sleep.wake_words or []:
            word = str(word).strip()
            if word:
                result = result.replace(word, "")
        cleaned = "".join(
            char for char in result if not char.isspace() and char not in "，。！？、,.!?~～…:：;；"
        )
        return cleaned

    @staticmethod
    def _take_wake_note(state: WorldState) -> str:
        """取走「刚被叫醒」这句提示（取走即失效，避免每轮都提）。"""

        if not state.wake_note or state.world_time > state.wake_note_until:
            state.wake_note = ""
            return ""
        note = state.wake_note
        state.wake_note = ""
        return note

    def _sleep_reply_text(self, state: WorldState, ctx: MessageContext) -> str:
        text = str(self.world.sleep.reply_text or "").strip()
        if not text:
            return ""
        return self.render_template(
            text,
            state,
            self.node(state.node_id),
            PlannedAction(type="say", target=ctx.user_id),
        ).strip()

    def _sleep_reply_on_cooldown(self, state: WorldState) -> bool:
        minutes = max(0, int(self.world.sleep.reply_cooldown_minutes))
        if minutes <= 0 or not state.sleep_reply_at:
            return False
        return (self._now() - float(state.sleep_reply_at)) < minutes * 60

    # ---------------- 迷糊惊醒 ----------------

    def _startled_config(self):
        return getattr(self.world.sleep, "startled", None)

    def _startled_active(self, state: WorldState) -> bool:
        """现在是不是在"被吵醒的迷糊窗口"里（这段时间她还算睡着）。"""

        return int(state.startled_until or 0) > int(state.world_time or 0)

    def _note_sleep_noise(self, state: WorldState, ctx: MessageContext) -> None:
        """记一笔"睡着时被吵"：滑动窗口里的条数，以及有几次是冲她来的。"""

        config = self._startled_config()
        if config is None or not bool(getattr(config, "enabled", True)):
            return
        now = self._now()
        window = max(60.0, float(getattr(config, "window_minutes", 5) or 5) * 60)
        if not state.sleep_noise_at or now - float(state.sleep_noise_at) > window:
            state.sleep_noise_at = now
            state.sleep_noise_count = 0
            state.sleep_named_count = 0
        state.sleep_noise_count = int(state.sleep_noise_count or 0) + 1
        named = bool(ctx.is_mentioned or ctx.is_private)
        if not named:
            # 没 @ 她，但喊了她的名字也算冲她来的
            named = self._named_at_her(state, ctx.text or "")
        if named:
            state.sleep_named_count = int(state.sleep_named_count or 0) + 1

    def _named_at_her(self, state: WorldState, text: str) -> bool:
        """她自己的名字出现在消息里没有（用来判断这条是不是冲她来的）。"""

        body = text or ""
        names = [
            str(name)
            for name in (
                self.world.bot_name,
                state.bot_base_nickname,
            )
            if str(name or "").strip()
        ]
        return any(name in body for name in names)

    def _startled_threshold(self, state: WorldState) -> int:
        config = self._startled_config()
        if config is None:
            return 0
        if str(state.state) == STATE_NAPPING:
            return max(1, int(getattr(config, "noise_threshold_nap", 5) or 5))
        return max(1, int(getattr(config, "noise_threshold", 8) or 8))

    async def _maybe_startle_wake(
        self, state: WorldState, ctx: MessageContext
    ) -> bool:
        """吵到位了就"迷糊惊醒"一次；返回 True 表示这条消息放行让她回。"""

        config = self._startled_config()
        if config is None or not bool(getattr(config, "enabled", True)):
            return False
        if not self._is_asleep(state):
            return False
        if self._startled_active(state):
            # 已经在窗口里：只放"冲她来的"那几条过去，别的照旧挡着——
            # 刷屏的人不该因为她在半睡半醒就把每一句都塞给她
            return bool(
                ctx.is_mentioned
                or ctx.is_private
                or self._named_at_her(state, ctx.text or "")
            )
        max_times = max(0, int(getattr(config, "max_per_sleep", 1) or 0))
        if int(state.startled_count or 0) >= max_times:
            return False
        # 刚睡着那一段不惊醒：那时候最沉，也正撞上第一次记忆整理
        started = int(state.sleep_started_at or 0)
        skip_ticks = max(0, int(getattr(config, "skip_first_minutes", 30) or 0)) * 60
        if started and skip_ticks:
            elapsed = max(0, int(state.world_time or 0) - started) * int(self.tick_seconds)
            if elapsed < skip_ticks:
                return False
        named = int(state.sleep_named_count or 0)
        threshold = self._startled_threshold(state)
        if named >= max(1, int(getattr(config, "named_threshold", 2) or 2)):
            reason = f"被连着叫了 {named} 次"
        elif int(state.sleep_noise_count or 0) >= threshold:
            reason = f"群里 {int(state.sleep_noise_count)} 条消息吵得厉害"
        else:
            return False
        await self._startle(state, ctx, reason=reason)
        return True

    async def _startle(self, state: WorldState, ctx: MessageContext, *, reason: str) -> None:
        """把"被吵醒"落到状态上：**不打断睡眠**，只开一个能说话的窗口。"""

        config = self._startled_config()
        quality = getattr(self.world.sleep, "quality", None)
        awake_ticks = max(1, int(getattr(config, "awake_minutes", 3) or 3)) * 60
        state.startled_until = int(state.world_time) + max(
            1, int(round(awake_ticks / max(1.0, float(self.tick_seconds))))
        )
        state.startled_count = int(state.startled_count or 0) + 1
        state.sleep_noise_count = 0
        state.sleep_named_count = 0
        state.sleep_noise_at = 0.0
        penalty = abs(float(getattr(config, "energy_penalty", 0.05) or 0.0))
        if penalty:
            state.energy = max(0.0, float(state.energy) - penalty)
        self._set_grumpy(
            state,
            minutes=int(getattr(config, "grumpy_minutes", 15) or 0),
            valence=float(getattr(config, "grumpy_valence", -0.05) or 0.0),
            affect=float(getattr(config, "grumpy_affect", 0.06) or 0.0),
            note="你刚被吵醒，还在起床气里",
        )
        state.startled_note = (
            "你刚才被吵醒了，现在是半睡半醒：只用「说话」回一两句，"
            "语气迷糊、可以带点起床气，偶尔打错一两个字也正常；"
            "**不要安排任何别的动作**，回完就接着睡，别把话题铺开。"
        )
        state.add_event("startled", {"by": ctx.user_id, "reason": reason})
        await self._log_event(
            state,
            "startled",
            {
                "user": ctx.user_name or ctx.user_id or "有人",
                "reason": reason,
                "awake_minutes": int(getattr(config, "awake_minutes", 3) or 3),
                "energy_penalty": penalty,
            },
        )
        self._log("info", f"[virtual_world] 她在睡觉，被吵醒了一次（{reason}）")

    def _set_grumpy(
        self,
        state: WorldState,
        *,
        minutes: int,
        valence: float,
        affect: float,
        note: str,
    ) -> None:
        """挂一段起床气：调效价 / 心潮，并在提示词里说清她这会儿是什么状态。"""

        if minutes <= 0:
            return
        ticks = max(1, int(round(minutes * 60 / max(1.0, float(self.tick_seconds)))))
        state.grumpy_until = max(
            int(state.grumpy_until or 0), int(state.world_time) + ticks
        )
        state.grumpy_note = note
        self.dynamics.apply_effects(
            state,
            {"valence": _signed(valence), "affect": _signed(affect)},
            world=self.world,
            now=self._now(),
        )

    def sleep_notes(self, state: WorldState) -> list[str]:
        """睡着被吵醒 / 起床气这几句临时说明（提示词用，过期自动失效）。"""

        notes: list[str] = []
        if str(getattr(state, "state", "")) == STATE_DROWSY or state.drowsy_sleep_step:
            # 临睡期：她已经困得不行了，只是还没躺下
            notes.append(
                "你现在困得不行了（正要睡）：**说话短、断断续续、迷迷糊糊的**——"
                "可以只说半句、只说一个词、或者说到一半就没下文；别安排事情、别长篇解释，"
                "也别主动挑话题。"
            )
        if self._startled_active(state) and state.startled_note:
            notes.append(state.startled_note)
        elif int(state.startled_until or 0) and int(state.startled_until) <= int(
            state.world_time or 0
        ):
            state.startled_note = ""
        if int(state.grumpy_until or 0) > int(state.world_time or 0):
            note = state.grumpy_note or "你现在有点起床气"
            notes.append(
                f"{note}：说话短一点、耐心少一点，别主动撒娇示好，也别急着安排大事。"
            )
        elif state.grumpy_note:
            state.grumpy_note = ""
        return notes

    # ---------------- 临睡期与晚安 / 早安 ----------------

    def _should_drowse(self, state: WorldState, definition: ActionDef, *, skip_drowsy: bool = False) -> bool:
        """这一步是不是"夜里睡前"——要不要先进入临睡期。

        只有**睡整觉**（不是小睡）走这一套，而且一天最多一次（回笼觉就直接躺）。
        极端保护的强制补觉不拖：那是她已经透支了。
        """

        if skip_drowsy or definition.id != "sleep":
            return False
        if not bool(getattr(self.world.sleep, "drowsy", True)):
            return False
        if str((active_plan(state) or {}).get("source") or "") == "forced":
            return False
        if state.state == STATE_DROWSY or state.drowsy_sleep_step:
            return False
        return str(state.drowsy_day or "") != self._today_key(self._now())

    async def _enter_drowsy(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
        depth: int,
    ) -> None:
        """进入临睡期：把"真的睡"那一步先揣着，先迷糊一会儿。

        这一段她还在（能回消息、会说晚安），等安静下来才真的躺下。
        """

        now = self._now()
        payload = (
            action.model_dump(mode="json")
            if hasattr(action, "model_dump")
            else asdict(action)
        )
        state.drowsy_sleep_step = dict(payload or {})
        state.drowsy_started_world_time = int(state.world_time or 0)
        state.drowsy_started_at = float(now)
        state.drowsy_day = self._today_key(now)
        state.current_action = None
        state.state = STATE_DROWSY
        state.add_event("drowsy", {"action": definition.id})
        await self._log_event(
            state,
            "drowsy",
            {
                "action": definition.id,
                "note": (
                    f"困了，先临睡一会儿（安静 {int(getattr(self.world.sleep, 'drowsy_minutes', 5) or 5)} "
                    f"分钟、最多 {int(getattr(self.world.sleep, 'drowsy_max_minutes', 30) or 30)} 分钟后去睡）"
                ),
            },
            outcome=outcome,
        )
        # 睡前这一段：要不要说声晚安，由她自己定（发给谁、发不发都行）
        await self._maybe_say_goodnight(state, node, outcome)

    async def _drowsy_tick(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        """临睡期的一拍：够安静了（或者拖太久了）就真的去睡。"""

        if not state.drowsy_sleep_step:
            # 没揣着那一步（老存档 / 中途被清）：就当它结束了
            state.state = STATE_IDLE
            state.drowsy_started_world_time = 0
            state.drowsy_started_at = 0.0
            return
        config = self.world.sleep
        quiet_minutes = max(1, int(getattr(config, "drowsy_minutes", 5) or 5))
        max_minutes = max(quiet_minutes, int(getattr(config, "drowsy_max_minutes", 30) or 30))
        ticks = max(1, int(round(max_minutes * 60 / max(1.0, self.tick_seconds))))
        elapsed = max(0, int(state.world_time or 0) - int(state.drowsy_started_world_time or 0))
        # "安静了多久"从进临睡期的这一刻算起，不能被更早的那次发言提前满足
        base = max(float(state.drowsy_started_at or 0.0), float(state.last_user_activity_at or 0.0))
        idle_seconds = self._now() - base
        if elapsed >= ticks:
            await self._go_to_sleep(state, node, outcome, reason="拖太久了，去睡吧")
            return
        if idle_seconds >= quiet_minutes * 60:
            await self._go_to_sleep(state, node, outcome, reason="安静下来了，去睡吧")

    async def _go_to_sleep(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome, *, reason: str
    ) -> None:
        """临睡期结束：把揣着的那一步睡觉真的执行掉。"""

        step = dict(state.drowsy_sleep_step or {})
        state.drowsy_sleep_step = {}
        state.drowsy_started_world_time = 0
        state.drowsy_started_at = 0.0
        state.state = STATE_IDLE
        definition = self.world.action_map().get("sleep")
        if not step or definition is None:
            return
        outcome.notes.append(reason)
        # 存的是 PlannedAction 的字段（``type``），这里换回计划步骤的写法（``action``）
        action = self._step_to_action({**step, "action": step.get("type") or "sleep"})
        # 这一步已经在临睡期里"等过了"：直接睡（skip_drowsy）
        await self._execute_actions(
            state,
            node,
            outcome,
            [action],
            depth=0,
            autonomous=True,
            from_plan=False,
            skip_drowsy=True,
        )

    async def _maybe_say_goodnight(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> bool:
        """睡前一次机会：要不要说晚安、发给谁——**由她自己决定**，也可以不发。"""

        if not bool(getattr(self.world.sleep, "goodnight", True)) or self.llm is None:
            return False
        if str(state.goodnight_day or "") == self._today_key(self._now()):
            return False
        now = self.local_now()
        hint = (
            f"这会儿你困得不行，正准备睡（现在是 {now.strftime('%H:%M')}，{period_of(now.hour)}）。\n"
            "**要不要说声晚安，由你自己决定**：\n"
            "- 想发就写一条 `say`，写清 `send_to`——发给谁、发到哪个会话都看你："
            "想单独跟谁道晚安就发他的私聊，想让大家都看到就发群里，"
            "也可以分别给几个人各发一条；\n"
            "- **不想发就什么都不写**（返回空 actions）：今天没人理你、你懒得开口、"
            "或者白天刚聊过，都可以不说，不会被记成「没礼貌」；\n"
            "- 要说就说得短一点、迷迷糊糊的（「睡了啊…明天再聊」「困死了，先躺了」），"
            "别写小作文，也别挨个点名问候。\n"
            "你手边能说话的地方见上面的「你能说话的地方」。"
        )
        plan = await self._ask_llm_for_plan(state, node, outcome, force=True, hint=hint)
        if plan is None or not self.plan_speaks(plan):
            # 她决定不说了：记一笔，今天不再问
            state.goodnight_day = self._today_key(self._now())
            await self._log_event(state, "goodnight", {"said": False}, outcome=outcome)
            return False
        outcome.session_id = self._freeze_plan_target(
            state, outcome, plan, fallback=state.session_id
        )
        await self._apply_plan(state, node, outcome, plan)
        state.goodnight_day = self._today_key(self._now())
        await self._tick_plan(state, node, outcome, depth=0)
        await self._log_event(
            state,
            "goodnight",
            {"said": True, "note": str(plan.get("reason") or "")[:80]},
            outcome=outcome,
        )
        return True

    async def _maybe_say_goodmorning(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> bool:
        """睡醒后一次机会：要不要说早安、发给谁——同样由她自己决定。"""

        if not bool(getattr(self.world.sleep, "goodmorning", True)) or self.llm is None:
            return False
        if str(state.goodmorning_day or "") == self._today_key(self._now()):
            return False
        state.goodmorning_day = self._today_key(self._now())
        now = self.local_now()
        hint = (
            f"你刚睡醒（现在是 {now.strftime('%H:%M')}，{period_of(now.hour)}）。\n"
            "**要不要说声早安，由你自己决定**：\n"
            "- 想发就写一条 `say` 并写清 `send_to`：单独跟谁说就发私聊，想让大家都看到就发群里；\n"
            "- **不想发就什么都不写**（返回空 actions）——刚醒还没缓过来、或者昨天没人接你的话，"
            "都可以不主动冒头；\n"
            "- 要说就短一点、带着刚醒的迷糊（「早…」「醒了，有点渴」），别写成问候模板。\n"
            "你手边能说话的地方见上面的「你能说话的地方」。"
        )
        plan = await self._ask_llm_for_plan(state, node, outcome, force=True, hint=hint)
        if plan is None or not self.plan_speaks(plan):
            await self._log_event(state, "goodmorning", {"said": False}, outcome=outcome)
            return False
        outcome.session_id = self._freeze_plan_target(
            state, outcome, plan, fallback=state.session_id
        )
        await self._apply_plan(state, node, outcome, plan)
        await self._tick_plan(state, node, outcome, depth=0)
        await self._log_event(
            state,
            "goodmorning",
            {"said": True, "note": str(plan.get("reason") or "")[:80]},
            outcome=outcome,
        )
        return True

    def _apply_wake_quality(
        self, state: WorldState, *, slept_minutes: float, startled: int = 0
    ) -> None:
        """睡整觉结束时的结算：睡饱 -> 清爽；没睡够 / 被吵过 -> 起床气。"""

        config = getattr(self.world.sleep, "quality", None)
        if config is None or not bool(getattr(config, "enabled", True)):
            return
        full = max(1, int(getattr(config, "full_minutes", 360) or 360))
        if slept_minutes >= full and not startled:
            self.dynamics.apply_effects(
                state,
                {
                    "valence": _signed(
                        float(getattr(config, "rested_valence", 0.08) or 0.0)
                    ),
                    "affect": _signed(
                        float(getattr(config, "rested_affect", -0.05) or 0.0)
                    ),
                },
                world=self.world,
                now=self._now(),
            )
            return
        if slept_minutes >= full:
            # 睡够了但中途被吵醒：算不上起床气，清爽也打个折
            self.dynamics.apply_effects(
                state,
                {
                    "valence": _signed(
                        round(
                            float(getattr(config, "rested_valence", 0.08) or 0.0) / 3, 4
                        )
                    ),
                    "affect": _signed(
                        round(
                            abs(float(getattr(config, "rested_affect", -0.05) or 0.0)) / 2,
                            4,
                        )
                    ),
                },
                world=self.world,
                now=self._now(),
            )
            self._set_grumpy(
                state,
                minutes=int(getattr(config, "grumpy_minutes", 30) or 0),
                valence=0.0,
                affect=0.0,
                note="你睡够了，但中途被吵醒过，还有点没缓过来",
            )
            return
        self._set_grumpy(
            state,
            minutes=int(getattr(config, "grumpy_minutes", 30) or 0),
            valence=float(getattr(config, "grumpy_valence", -0.12) or 0.0),
            affect=float(getattr(config, "grumpy_affect", 0.10) or 0.0),
            note="你没睡够，这会儿有起床气",
        )

    def _wake_up_state(self, state: WorldState, ctx: MessageContext) -> None:
        """把「被叫醒」落到状态上：停下动作、可选地清掉排队计划、给一段保护期。"""

        config = self.world.sleep
        state.current_action = None
        state.state = STATE_IDLE
        state.mood_override_until = 0
        if config.clear_plan_on_wake:
            state.current_plan = None
        grace_minutes = max(0, int(config.wake_grace_minutes))
        if grace_minutes:
            grace_ticks = max(1, int(round(grace_minutes * 60 / self.tick_seconds)))
            state.no_sleep_until = max(
                int(state.no_sleep_until), int(state.world_time) + grace_ticks
            )
        # 群名片要立刻从「睡觉中」改回来，所以顺手把冷却清零
        state.last_nickname_update_at = 0.0
        state.add_event("wake_up", {"by": ctx.user_id})
        # 「刚被叫醒」这句提示：接管模式和注入模式都会用到，写在状态里统一取
        state.wake_note = self._wake_note_text(ctx)
        state.wake_note_until = int(state.world_time) + 2

    async def should_block_sleep(self, ctx: MessageContext) -> bool:
        """睡着时要不要把这条消息整个挡下来（挡在其它插件之前）。

        默认只挡「没 @ 她」的消息：她被 @ 了、消息是 ``/指令``、或者本来就是私聊，
        都放行给后面的正常流程（@ 她会拿到固定文案，或者命中唤醒词被叫醒）。
        ``sleep.block_scope = all`` 时连「@ 了她但没说唤醒词」的消息也一起挡。
        """

        config = self.world.sleep
        if not config.block_plugins or not self.is_enabled(ctx.session_id):
            return False
        if ctx.is_private:
            return False  # 私聊等同被叫，永远放行
        if self._wake_hit(ctx):
            return False  # 明确叫醒 -> 放行
        if ctx.is_mentioned and config.block_scope != "all":
            return False  # @ 了她但没说唤醒词 -> 交给 sleep_gate 回固定文案
        async with self.session_state(ctx.session_id) as state:
            if not self._is_asleep(state):
                return False
            # 挡下来之前先记一笔：她醒来还得知道群里发生过什么
            self._note_presence(state, ctx)
            # 也是"吵"的一笔：吵到位就把这条放行，让她迷糊回一句（她还算睡着）
            self._note_sleep_noise(state, ctx)
            if await self._maybe_startle_wake(state, ctx):
                return False
            # 睡着的时候群里刷屏，不能每来一条就写一条日志——日志页会被刷爆。
            # 每 10 分钟最多记一条，并带上这段时间一共挡下了多少条。
            now = self._now()
            state.sleep_skip_count = int(state.sleep_skip_count) + 1
            if state.sleep_skip_at and now - float(state.sleep_skip_at) < 600.0:
                return True
            suppressed = int(state.sleep_skip_count)
            state.sleep_skip_count = 0
            state.sleep_skip_at = now
            await self._log_event(
                state,
                "sleep_skip",
                {
                    "user": ctx.user_name or ctx.user_id or "有人",
                    "reason": "她在睡觉，这条消息被整个挡下（其它插件也不会执行）",
                    "count": suppressed,
                },
            )
            return True

    async def sleep_gate(self, ctx: MessageContext) -> SleepReply | None:
        """她在睡觉时挡在回复路径前面的那道门。

        返回 ``None`` 表示照常回复（没在睡，或者这条消息把她叫醒了）；
        否则调用方按返回的 ``mode`` 处理，并且**不要**再交给主人格。
        """

        config = self.world.sleep
        if config.reply_mode == "normal" or not self.is_enabled(ctx.session_id):
            return None
        if not self._passes_safety(ctx.text):
            return None
        async with self.session_state(ctx.session_id) as state:
            if not self._is_asleep(state):
                return None
            if self._wake_hit(ctx):
                return None  # 正常路径会负责叫醒她
            # @ 她 / 私聊也算"吵"：连着来几次就把她吵醒一次，之后三分钟里她能真回话
            self._note_sleep_noise(state, ctx)
            if await self._maybe_startle_wake(state, ctx):
                return None
            if self._startled_active(state):
                return None  # 迷糊窗口里：让她自己回，而不是那句固定文案
            who = ctx.user_name or ctx.user_id or "有人"
            echo = TickOutcome(session_id=ctx.session_id)
            echo.place = ctx.session_id
            if not (ctx.is_mentioned or ctx.is_private):
                await self._log_event(
                    state,
                    "sleep_skip",
                    {"user": who, "reason": "她在睡觉，而且这条消息没 @ 她"},
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)
                return SleepReply(mode="silent", reason="没在跟她说话")
            if config.reply_mode == "silent":
                await self._log_event(
                    state,
                    "sleep_skip",
                    {"user": who, "reason": "配置成睡觉时保持安静"},
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)
                return SleepReply(mode="silent", reason="睡觉时保持安静")
            text = self._sleep_reply_text(state, ctx)
            if not text:
                return SleepReply(mode="silent", reason="固定文案是空的")
            if self._sleep_reply_on_cooldown(state):
                await self._log_event(
                    state,
                    "sleep_skip",
                    {"user": who, "reason": "刚回睡觉文案不久，先安静着"},
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)
                return SleepReply(mode="silent", reason="固定文案还在冷却")
            state.sleep_reply_at = self._now()
            # 睡觉时回的这句也是"被搭话后的回复"，同样要压一压后面的主动搭话
            self.engagement.note_passive_reply(state, tick_seconds=self.tick_seconds)
            await self._log_event(
                state, "sleep_reply", {"user": who, "text": text}, outcome=echo
            )
            self._queue_echo(ctx.session_id, echo.debug_messages)
            return SleepReply(mode="template", messages=[text], reason="她在睡觉")

    async def handle_incoming(self, ctx: MessageContext) -> str | None:
        """处理一条用户消息，返回需要注入的「世界认知」文本。

        - 不在白名单 / 内容安全拦截 -> 返回 None（完全不影响主人格的正常回复）；
        - 计数与状态更新在这里完成，但**不接管回复**。
        """

        if not self.is_enabled(ctx.session_id):
            return None
        if not self._passes_safety(ctx.text):
            return None

        async with self.session_state(ctx.session_id) as state:
            node = self.node(state.node_id)
            if node is None:
                state.node_id = self.default_node_id()
                node = self.node(state.node_id)

            extra_notes, woke = self._record_user_message(state, ctx)
            density = self.speech_density_hint(state)
            if density:
                extra_notes.append(density)
            missing = self.extra_reminders(state, ctx=ctx)
            if missing:
                extra_notes.append(missing)
            # 刚被搭话（她马上要回一句）：接下来一段时间别再因为孤独感主动开口
            self.engagement.note_passive_reply(state, tick_seconds=self.tick_seconds)
            if woke:
                echo = TickOutcome(session_id=ctx.session_id)
                echo.place = ctx.session_id
                await self._log_event(
                    state,
                    "wake_up",
                    {"by": ctx.user_name or ctx.user_id},
                    persona_id=ctx.persona_id,
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)

            memories = self.memory.recall(
                session_id=state.session_id,
                persona_id=ctx.persona_id,
                node_id=state.node_id,
                focus_user=ctx.user_id,
                limit=self.world.limits.max_think_memory,
            )
            injection = self.prompts.build_injection(
                state,
                node=node,
                memories=memories,
                engagement_hint=self.engagement.hint(state),
                extra_notes=extra_notes,
                focus_user=ctx.user_name or ctx.user_id,
                recent_chat=self.chat_window(state),
                profile_text=self.profile_block(state, ctx),
                samples=self.voice_sample_lines(
                    state,
                    ctx.session_id,
                    signal=self.tone_signal(state, ctx.text),
                ),
                hidden_actions=self._hidden_actions(state, ctx.session_id),
                **await self.runtime_notes(state.session_id),
            )
            if ctx.other_context:
                injection += f"\n\n# 其他插件提供的上下文\n{ctx.other_context}"

            # 把这次对话放进"待总结"缓冲：一段聊完或她离开这个地点时，
            # 才让模型把它压成一条记忆（逐条存「谁说了什么」没有信息量）。
            self.note_dialogue(
                state,
                user_id=ctx.user_id,
                name=ctx.user_name or ctx.user_id,
                text=ctx.text,
                persona_id=ctx.persona_id,
            )
            await self._log_event(
                state,
                "user_message",
                {
                    "user": ctx.user_name or ctx.user_id,
                    "wake": ctx.is_wake,
                    "len": len(ctx.text or ""),
                    "text": (ctx.text or "")[:120],
                },
                persona_id=ctx.persona_id,
                place=ctx.session_id,
            )
            # 扩展层（装了扩展才有）：只在它自己允许的会话里加
            layer = self._extension_prompt(state, ctx.session_id)
            if layer:
                injection = f"{injection}\n\n{layer}"
            return injection

    # ---------------- 对话记忆：攒片段，再总结 ----------------

    def note_dialogue(
        self,
        state: WorldState,
        *,
        user_id: str = "",
        name: str = "",
        text: str = "",
        is_self: bool = False,
        persona_id: str = "",
    ) -> None:
        """把一条「她参与的对话」放进待总结缓冲（不立刻写记忆）。"""

        clean = " ".join(str(text or "").split())
        if not clean:
            return
        if not state.pending_memory:
            state.pending_memory_node = state.node_id
        state.pending_memory.append(
            {
                "user_id": "" if is_self else str(user_id or ""),
                "name": "我" if is_self else str(name or user_id or "有人"),
                "text": _clip_text(clean, 160),
                "is_self": bool(is_self),
                "persona_id": str(persona_id or ""),
                "at": self._now(),
                "world_time": int(state.world_time),
            }
        )
        limit = 60
        if len(state.pending_memory) > limit:
            state.pending_memory = state.pending_memory[-limit:]

    def note_memory_hint(self, state: WorldState, hint: str) -> None:
        """大模型顺手写的一句话总结，作为写记忆时的提示（不会单独成为一条记忆）。"""

        text = " ".join(str(hint or "").split())
        if not text:
            return
        state.memory_hints.append(_clip_text(text, 80))
        if len(state.memory_hints) > 5:
            state.memory_hints = state.memory_hints[-5:]

    def _memory_flush_reason(self, state: WorldState) -> str:
        """这段对话该总结了吗？返回触发原因，空字符串表示再攒攒。"""

        config = self.world.memory
        if not config.dialogue_summary or not state.pending_memory:
            return ""
        if state.memory_flush_wanted:
            return "她换地方了"
        if len(state.pending_memory) >= max(1, int(config.summary_trigger_messages)):
            return "这段对话够长了"
        idle_minutes = max(0, int(config.summary_idle_minutes))
        if idle_minutes:
            last_at = float(state.pending_memory[-1].get("at") or 0.0)
            if last_at and (self._now() - last_at) >= idle_minutes * 60:
                return "这段聊完了"
        return ""

    @staticmethod
    def _fallback_memory_text(entries: list[dict[str, Any]]) -> str:
        """模型不可用时的兜底：把这段对话压成一行，而不是什么都不记。"""

        recent = entries[:6]
        if recent and all(item.get("is_self") for item in recent):
            # 只有她自己：直接留下她自己说的话，别套「我说」那层壳
            own = [
                _clip_text(str(item.get("text") or ""), 50)
                for item in recent
                if str(item.get("text") or "").strip()
            ]
            return _clip_text("；".join(part for part in own if part), 120)
        parts: list[str] = []
        for item in recent:
            text = _clip_text(str(item.get("text") or ""), 40)
            if not text:
                continue
            who = "我" if item.get("is_self") else str(item.get("name") or "有人")
            parts.append(f"{who}说「{text}」")
        if not parts:
            return ""
        return _clip_text("；".join(parts), 120)

    async def flush_pending_memory(self, session_id: str) -> TickOutcome | None:
        """把攒着的对话片段总结成一条记忆（攒够 / 聊完 / 换地点三种触发）。"""

        config = self.world.memory
        if not config.dialogue_summary or not self.is_enabled(session_id):
            return None
        async with self.session_state(session_id) as state:
            reason = self._memory_flush_reason(state)
            if not reason:
                return None
            entries = [dict(item) for item in state.pending_memory]
            hints = list(state.memory_hints)
            node_id = state.pending_memory_node or state.node_id
            persona_id = next(
                (
                    str(item.get("persona_id") or "")
                    for item in reversed(entries)
                    if item.get("persona_id")
                ),
                "",
            )
            mood = state.mood
            affect = float(state.affect)
            valence = float(state.valence)
            state.pending_memory = []
            state.memory_hints = []
            state.pending_memory_node = ""
            state.memory_flush_wanted = False
            state.memory_flush_at = self._now()

        outcome = TickOutcome(session_id=session_id)
        outcome.notes.append(f"总结这段对话（{reason}）")
        summary = ""
        if self.llm is not None:
            where = self.node(node_id)
            system_prompt, prompt = self.prompts.build_memory_summary_prompt(
                node_name=(where.name if where else "") or "",
                lines=[
                    f"{item.get('name') or '有人'}: {item.get('text')}" for item in entries
                ],
                hints=hints,
                max_chars=max(10, int(config.summary_max_chars)),
            )
            reply = await self._ask_llm(session_id, system_prompt, prompt)
            outcome.llm_calls += 1
            summary = self._clean_memory_summary(reply)
        if not summary:
            summary = self._fallback_memory_text(entries)
        if not summary:
            return outcome

        users = [
            str(item.get("user_id"))
            for item in entries
            if item.get("user_id")
        ]
        users = list(dict.fromkeys(users))
        async with self.session_state(session_id) as state:
            echo_marker = await self._event_marker(state)
            self.memory.remember(
                session_id=session_id,
                persona_id=persona_id,
                node_id=node_id,
                content=summary,
                memory_type=INTERACTION,
                related_users=users,
                emotion=mood,
                weight=0.5,
                source="llm" if summary else "runtime",
                affect=affect,
                valence=valence,
            )
            await self._log_event(
                state,
                "memory",
                {"content": summary, "reason": reason, "where": node_id},
            )
            await self._echo_events_since(state, outcome, echo_marker)
        return outcome

    @staticmethod
    def _clean_memory_summary(reply: str | None) -> str:
        """模型有时候会带引号、前缀或者多写几行，这里只取第一句有用的。"""

        text = " ".join(str(reply or "").split())
        if not text:
            return ""
        if any(hint in text for hint in MEMORY_REFUSAL_HINTS):
            # 她一个人做事的时候，模型偶尔会反过来"要人名"。
            # 这种话不是记忆，丢掉，让调用方用兜底文案重写一条。
            return ""
        for quote in ("「", "『", '"', "'"):
            if text.startswith(quote) and text.endswith(quote) and len(text) > 2:
                text = text[1:-1]
        text = text.strip("。；;，,")
        return _clip_text(text, 80)

    def _passes_safety(self, text: str) -> bool:
        blocked = self.world.content_safety.blocked_words or []
        if not blocked or not text:
            return True
        return not any(word and word in text for word in blocked)

    @staticmethod
    def command_words(text: str) -> list[str]:
        """把一条指令拆成词：``/vw event h`` 和斜杠被吃掉的 ``vw event h`` 都认。"""

        parts = [item for item in str(text or "").strip().split() if item]
        if not parts:
            return []
        parts[0] = parts[0].lstrip("/／!！").lower()
        return parts

    @classmethod
    def is_plugin_command(cls, text: str) -> bool:
        """是不是「本插件的指令」（``/vw …``）。

        这种消息是管理入口，不是有人在跟她讲话：不能进她的聊天记录，
        否则她会把「/vw event 逛街被跟了」当成"主人讲给我听的一件事"。
        """

        raw = str(text or "").strip()
        body = str(text or "").strip().lower()
        for prefix in ("/vw", "！vw", "!vw", "／vw"):
            if body.startswith(prefix):
                return True
        # AstrBot 在派发指令时会把**开头的斜杠**吃掉：她那边看到的其实是
        # 「vw event h」。光认带斜杠那几种写法挡不住，这里再按「第一个词 + 子命令」认一次，
        # 不然指令会被记成"他刚说的话"，她会一本正经地回一句"你怎么又敲这指令"。
        parts = cls.command_words(raw)
        if not parts or parts[0] not in ("vw", "世界", "virtualworld"):
            return False
        if len(parts) == 1:
            return True
        return parts[1] in cls.PLUGIN_SUBCOMMANDS

    @classmethod
    def is_event_command(cls, text: str) -> bool:
        """``/vw event …``（斜杠被吃掉也算）：这一条是"开一件事"，不是"跟她说话"。"""

        parts = cls.command_words(text)
        return bool(
            len(parts) >= 2
            and parts[0] in ("vw", "世界", "virtualworld")
            and parts[1] in ("event", "事件")
        )

    PLUGIN_SUBCOMMANDS = frozenset(
        {
            "state", "状态", "plan", "计划", "stop", "停", "停下", "打断",
            "memories", "记忆", "memory", "session", "会话", "tools", "工具",
            "prompt", "提示词", "autoprompt", "自主提示词", "tick", "推进",
            "decide", "决策", "schedule", "日程", "map", "地图",
            "nickname", "名片", "debug", "调试", "reload", "重载",
            "reset", "重置", "restore-default", "恢复默认", "event", "事件",
            "ability", "能力", "能力值", "thread", "线索", "未了",
            "help", "帮助", "?",
        }
    )
    """``/vw <这些>`` 认得出来的子命令：斜杠被吃掉之后靠它兜底。"""

    # ---------------- 这句话是不是「对她说的」 ----------------

    def bot_names(self, state: WorldState) -> list[str]:
        """她的各种叫法：全局设置里的 Bot 名称 + 当前 / 原始群名片。"""

        names = [
            str(self.world.bot_name or "").strip(),
            str(state.bot_current_nickname or "").strip(),
            str(state.bot_base_nickname or "").strip(),
        ]
        return [name for name in dict.fromkeys(names) if name]

    PROFILE_HINT_WORDS = (
        "我是", "我叫", "我的名字", "我生日", "我今年", "我喜欢", "我不喜欢", "我不吃",
        "我讨厌", "我爱吃", "我住", "以后叫我", "叫我", "我上班", "我工作", "我养",
        "我老婆", "我老公", "我女朋友", "我男朋友", "我妈", "我爸",
    )
    """他的话里出现这些词，通常是在说"关于他自己"的稳定信息。"""

    def remember_hint(self, text: str, *, ctx: MessageContext | None = None) -> str:
        """他说了关于自己的事时，提醒她顺手把这条写进通讯录。

        「记住」这个动作以前藏在七十多个动作的清单里，说明里还写着"日常寒暄不要用"，
        于是实际上几乎没人用它——档案全靠睡前整理补。这里只在真的像"个人信息"的那一轮
        加一句，平时不啰嗦。
        """

        if not bool(getattr(self.world.profile, "enabled", True)):
            return ""
        body = str(text or "")
        if not body or not any(word and word in body for word in self.PROFILE_HINT_WORDS):
            return ""
        who = str(getattr(ctx, "user_id", "") or "").strip()
        target = (
            f"`user` 填他的号码 `{who}`"
            if who
            else "`user` 填他的号码（上面聊天记录里名字后面括号里那串）"
        )
        return (
            "# 顺手记进通讯录\n"
            "他这句里有关于他自己的信息（称呼、喜好、生日、工作、关系这类以后还用得上的事）。\n"
            "回话之外，**再写一个 `remember` 动作**把它记进通讯录："
            f"{target}，`text` 一句话说清是什么，`evidence` 填**他的原话**（必填），"
            "`kind` 选 喜好 / 厌恶 / 习惯 / 基本信息 / 关系 / 约定。\n"
            "只写进 `memory` 是不够的：那是你自己的回忆，不进他的档案，"
            "下次问起你还是答不出来。\n"
            "如果这句只是玩笑、或者他并没有真的说清楚，就别写。\n"
        )

    def reply_addressing(self, state: WorldState, ctx: MessageContext) -> str:
        """这次回复是「对她说」还是「群里在聊、她去插一句」。

        只有 @ 了她、私聊、叫了她的名字，或者紧接着她自己那句话往下说，
        才算对她说；其余一律按插话处理——否则群里随便一句话都会被她当成
        "有人在指使我"，答非所问还会乱做动作。
        """

        if ctx.is_private or ctx.is_mentioned:
            return "direct"
        if ctx.is_soft_wake:
            # 没人 @ 她，但路由判定这句就是冲她说的：按"对她说"处理，
            # 只是提示词里会写清"没人点名"，别写成"有人 @ 你"。
            return "soft"
        text = str(ctx.text or "")
        for name in self.bot_names(state):
            if name and name in text:
                return "direct"
        # 上下文里最后一条通常就是刚记下的这条消息（先记后回），
        # 把它去掉，看前一条是谁说的：她刚说完话就有人接上，多半还在同一个话题里。
        context = self.chat_context(state)
        prior = list(context)
        if prior and not prior[-1].get("is_self"):
            prior = prior[:-1]
        if prior and prior[-1].get("is_self"):
            return "direct"
        return "interject"

    def speech_density_hint(self, state: WorldState, *, direct: bool = False) -> str:
        """「最近话太密」的提示：把事实摆给模型，让她这轮少说多做。

        ``direct``（有人正在跟她说话）时不劝她闭嘴——只提醒别刷屏。
        不然一群人围着逗她，她反而被自己的"话太密"提示按成一句不说，看着就很呆。
        """

        style = getattr(self.world, "reply_style", None)
        if style is None:
            return ""
        window_minutes = max(1, int(getattr(style, "dense_window_minutes", 10) or 10))
        limit = max(1, int(getattr(style, "dense_max_lines", 4) or 4))
        since = self._now() - window_minutes * 60
        mine = [
            item
            for item in state.recent_chat
            if item.get("is_self") and float(item.get("at") or 0) >= since
        ]
        if len(mine) < limit:
            return ""
        if direct:
            return (
                f"# 说话密度提醒\n"
                f"最近 {window_minutes} 分钟里你已经说了 {len(mine)} 句，偏多。"
                "这一轮**照常接话**，但每句短一点、别连着刷："
                "一句能说清就别拆成一串，该回的还是要回。\n"
            )
        return (
            f"# 说话密度提醒\n"
            f"最近 {window_minutes} 分钟里你已经说了 {len(mine)} 句，密度偏高。"
            "这一轮尽量少说话：能只做动作（发呆、走动、戳一下、想事情…）就别开口；"
            "真有必要回一句时，短一点，或者干脆安静地做自己的事。"
        )

    def chat_is_group(self, session_id: str) -> bool:
        """这个会话是不是群聊。续说那条路径拿不到 ctx，所以从会话配置取。"""

        session = self.session_config(session_id)
        return getattr(session, "type", "group") != "private"

    def _group_is_lively(self, state: WorldState, *, now: float | None = None) -> bool:
        """群里这几分钟是不是聊得很热闹（带动一下她的心潮）。"""

        stamp = self._now() if now is None else float(now)
        window = min(300.0, max(60.0, float(self.world.decider.chat_window_minutes) * 60))
        recent = [
            item
            for item in state.recent_chat
            if not item.get("is_self") and stamp - float(item.get("at") or 0.0) <= window
        ]
        need = max(3, int(self.world.decider.min_messages_to_interject) + 2)
        return len(recent) >= need

    TONE_EVENTS = {
        "praise": "positive_words",
        "hug": "hug_bot",
        "comfort": "hug_bot",
        "attack": "negative_words",
        "positive": "positive_words",
        "negative": "negative_words",
    }
    """口吻 → 情绪脉冲。``normal`` / 空不产生任何脉冲。"""

    def _she_was_hurting(self, state: WorldState, *, user_id: str = "") -> bool:
        """她这会儿是不是真的难受着——决定"被理解"算不算数。

        不设这道门的话，对方随手一句"我懂你"就能加一次心情，那条通道会变成免费回血。
        三个信号随便中一个就算：效价偏低、心里搁着一件事、或者本来就在记他的账。
        """

        if float(state.valence) < 0.45:
            return True
        if str(getattr(state, "heart_knot", "") or "").strip():
            return True
        uid = str(user_id or "")
        if uid:
            try:
                if self.grudge_for(state, uid):
                    return True
            except Exception:
                pass
        return False

    def _mark_reply_addressing(
        self, state: WorldState, ctx: MessageContext, addressing: str
    ) -> str:
        """把"这句其实是在跟别人说话"记在那条消息上。

        结构化信息（@ 名单、引用对象）只能认出"明说"的情况；一句没有 @ 的
        「你倒是说啊」得靠主模型判。它说是别人就信它：下一轮渲染聊天记录时
        会带上「（这句是冲别人说的，不是在问你）」，免得她再揽一次。
        """

        kind = str(addressing or "")
        if kind not in ("me", "others"):
            return ""
        for item in reversed(list(state.recent_chat or [])[-5:]):
            if not isinstance(item, dict):
                continue
            if str(item.get("user_id")) != str(ctx.user_id or ""):
                continue
            item["addressing"] = kind
            return kind
        return ""

    def _comfort_pulse(
        self, state: WorldState, tone: str, *, user_id: str = ""
    ) -> str:
        """安慰类的口吻走"安抚通道"：不占当天的聊天额度（见 dynamics.soothed）。

        - ``hug``：他哄她、贴一贴——她正低落才算；
        - ``comfort``：他还听懂了她说的那几句——要求多一点，效果也强一点。
        """

        now = self._now()
        if str(tone or "") == "comfort":
            if not self._she_was_hurting(state, user_id=user_id):
                return ""
            if self.dynamics.soothed(state, kind="understood", now=now):
                return "understood"
            return ""
        if self.dynamics.soothed(state, kind="soothed", now=now):
            return "soothed"
        return ""

    INTIMACY_CACHE_KEY = "action_intimacy"
    """动作"亲密度"标签的缓存键（存在 kv 表，不写进世界配置 / 预设）。"""

    def _intimacy_traits(self) -> dict[str, Any]:
        cache = getattr(self, "_intimacy_traits_data", None)
        if not isinstance(cache, dict):
            cache = {}
            self._intimacy_traits_data = cache
        return cache

    @staticmethod
    def _action_fingerprint(definition: ActionDef) -> str:
        """动作改过（名字或说明变了）就重新判一次，别拿旧标签糊弄。"""

        raw = f"{definition.name or ''}|{definition.description or ''}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]

    def intimacy_of(self, definition: ActionDef | None) -> float | None:
        """这个动作有多"亲密"（0~1）：决定它能不能走安抚通道。

        默认动作库靠效果就能认出来（冲着某个人、降孤独 / 抬心潮），不用问模型；
        用户自己加的动作看不出来时返回 ``None``，交给便宜模型判一次（判完记住）。
        """

        if definition is None:
            return 0.0
        if str(getattr(definition, "target_type", "") or "") != "user":
            return 0.0
        complete = getattr(definition, "on_complete", None)
        effects = dict(getattr(complete, "effects", {}) or {})
        warm = any(
            str(effects.get(key) or "").strip().startswith(sign)
            for key, sign in (("loneliness", "-"), ("affect", "+"))
        )
        if warm:
            return 1.0
        cached = self._intimacy_traits().get(str(definition.id))
        if isinstance(cached, dict) and str(cached.get("fp") or "") == self._action_fingerprint(
            definition
        ):
            return float(cached.get("score") or 0.0)
        return None

    async def _ensure_intimacy_traits(self) -> None:
        """把缓存的标签读进来（每个进程一次）。"""

        if getattr(self, "_intimacy_traits_loaded", False):
            return
        self._intimacy_traits_loaded = True
        try:
            value = await self.db.call("kv_get", self.INTIMACY_CACHE_KEY)
        except Exception:
            value = None
        if isinstance(value, dict):
            self._intimacy_traits_data = {
                str(key): dict(item)
                for key, item in value.items()
                if isinstance(item, dict)
            }

    async def _classify_intimacy(
        self, state: WorldState, definition: ActionDef
    ) -> float:
        """让便宜模型判一次"这个动作算不算亲昵接触"，判完缓存（不会每轮都问）。"""

        channel = self.helper_llm or self.llm
        if channel is None:
            return 0.0
        system = (
            "你在给一个角色插件的动作分类。判断这个动作是不是**亲昵的身体接触**"
            "（抱、亲、贴、拉手、摸头、靠肩膀这类，重点是「主动贴近某个人」）。"
            "\n只输出 JSON：{\"intimacy\": 1} 或 {\"intimacy\": 0}"
        )
        prompt = (
            f"动作 id：{definition.id}\n"
            f"动作名字：{definition.name or definition.id}\n"
            f"动作说明：{definition.description or '（没有说明）'}\n"
            f"它的目标：{getattr(definition, 'target_type', '') or '无'}"
        )
        try:
            reply = await channel.generate(
                session_id=state.session_id,
                system_prompt=system,
                prompt=prompt,
                temperature=0.0,
            )
        except Exception as exc:
            self._log("debug", f"判断动作亲密度失败：{exc}")
            return 0.0
        text = getattr(reply, "text", "") if getattr(reply, "ok", False) else ""
        payload = extract_json_object(str(text or ""))
        score = 0.0
        if isinstance(payload, dict):
            try:
                score = 1.0 if float(payload.get("intimacy") or 0) >= 0.5 else 0.0
            except (TypeError, ValueError):
                score = 0.0
        else:
            body = str(text or "").strip()
            if body.startswith("{"):
                return 0.0
            score = 1.0 if ("亲密" in body or "是" == body[:1]) else 0.0
        cache = self._intimacy_traits()
        cache[str(definition.id)] = {"fp": self._action_fingerprint(definition), "score": score}
        try:
            await self.db.call("kv_set", self.INTIMACY_CACHE_KEY, cache)
        except Exception:
            pass
        return score

    async def _maybe_soothe_from_action(
        self,
        state: WorldState,
        definition: ActionDef | None,
        *,
        outcome: TickOutcome | None = None,
    ) -> bool:
        """她主动贴过来这一下，如果她正低落，也算一次安抚（不占聊天额度）。"""

        if definition is None or float(state.valence) >= SOOTHE_VALENCE_BELOW:
            return False
        await self._ensure_intimacy_traits()
        score = self.intimacy_of(definition)
        if score is None:
            score = await self._classify_intimacy(state, definition)
        if float(score or 0.0) < 0.5:
            return False
        if not self.dynamics.soothed(state, kind="soothed", now=self._now()):
            return False
        await self._log_event(
            state,
            "soothed",
            {
                "kind": "soothed",
                "cause": f"她自己贴过来（{definition.name or definition.id}）",
                "valence": round(float(state.valence), 3),
            },
            outcome=outcome,
        )
        return True

    async def _apply_tone_pulse(
        self,
        state: WorldState,
        tone: str,
        *,
        user_id: str = "",
        outcome: TickOutcome | None = None,
    ) -> str:
        """按"对方这一轮的口吻"给一次情绪脉冲（心潮 / 孤独感的来源之一）。

        主路径是主模型在 JSON 里写的 ``tone``；只有它没写时才用关键词表兜底——
        表只认字面，遇到反话就会判错（"你可真行啊"当年被当成夸奖）。

        安慰类（``hug`` / ``comfort``）还额外走一次"安抚通道"：她低潮时那一下不占
        当天的聊天额度，见 ``_comfort_pulse``。
        """

        key = str(tone or "").strip().lower()
        event = self.TONE_EVENTS.get(key)
        if not event:
            return ""
        magnitude = self._tone_magnitude(state, event, user_id)
        # 扩展可以给这一下打折（例如"正演着那一场戏，他说的重话是玩闹"）：
        # 没扩展表态就是 1.0，一切照旧。
        scale = self.extensions.tone_scale(state, key)
        if scale != 1.0:
            magnitude = round(magnitude * scale, 4)
            if outcome is not None:
                outcome.notes.append(f"这一轮的口吻脉冲按 {scale:g} 倍走")
        self.dynamics.apply_event(state, event, magnitude=magnitude, now=self._now())
        if key in ("hug", "comfort"):
            kind = self._comfort_pulse(state, key, user_id=user_id)
            if kind:
                await self._log_event(
                    state,
                    "soothed",
                    {
                        "kind": kind,
                        "cause": (
                            "他听懂了你那几句" if kind == "understood" else "被安抚了一会儿"
                        ),
                        "valence": round(float(state.valence), 3),
                    },
                    outcome=outcome,
                )
            # 被他撩了一下（这一轮只是嘴上/氛围上，不是真的碰到）：想被碰一碰的劲头跳一点。
            # 同一句口吻连着来不重复算，越亲近的人越撩得动她。
            if key != str(getattr(state, "last_user_tone", "") or ""):
                gained = self.dynamics.tease_desire(
                    state, scale=self._desire_tease_scale(state, user_id)
                )
                if gained > 0 and outcome is not None:
                    outcome.notes.append(f"被撩了一下：欲求 +{gained:.2f}")
        return event

    def _desire_tease_scale(self, state: WorldState, user_id: str) -> float:
        """这一下撩动她多少：按关系档位从 0.3 涨到 1.6（跟"被怼多疼"同一套档位）。"""

        uid = str(user_id or "")
        if not uid or not self.profiles.enabled():
            return 1.0
        try:
            view = self.profiles.view(state.session_id, uid)
        except Exception:
            return 1.0
        if view is None:
            return DESIRE_TEASE_FLOOR
        levels = list(getattr(self.world.profile, "levels", None) or [])
        if len(levels) <= 1:
            return 1.0
        index = max(0, min(int(getattr(view, "level_index", 0) or 0), len(levels) - 1))
        ratio = index / (len(levels) - 1)
        return round(DESIRE_TEASE_FLOOR + (DESIRE_TEASE_CEIL - DESIRE_TEASE_FLOOR) * ratio, 3)

    def _tone_magnitude(self, state: WorldState, event: str, user_id: str) -> float:
        """这一句话在她这儿有多重：**越亲近的人，伤她越深**。

        路人怼她一句几乎不往心里去（0.2 倍），特别的人说一句顶别人几句（1.5 倍）。
        只对负面口吻（``negative_words``）加权：正面那几条本来就压得很小、还带"连着被哄会麻木"，
        再按亲密度放大反而会让熟人一句夸奖把她顶满。
        """

        if event != "negative_words":
            return 1.0
        uid = str(user_id or "")
        if not uid or not self.profiles.enabled():
            return 1.0
        try:
            view = self.profiles.view(state.session_id, uid)
        except Exception:
            return 1.0
        if view is None:
            return TONE_INTIMACY_FLOOR
        levels = list(getattr(self.world.profile, "levels", None) or [])
        if len(levels) <= 1:
            return 1.0
        index = max(0, min(int(getattr(view, "level_index", 0) or 0), len(levels) - 1))
        # 归一化到 0~1 再上一条幂曲线：低档更平（路人几乎没感觉），高档更陡
        ratio = index / (len(levels) - 1)
        return round(TONE_INTIMACY_FLOOR + (TONE_INTIMACY_CEIL - TONE_INTIMACY_FLOOR) * ratio**1.6, 3)

    def note_open_topic(self, state: WorldState, ctx: Any, text: str) -> None:
        """记下"他没说完的事"。空字符串 = 这轮没有，什么都不做。

        同一件事反复说只刷新时间；同一个人身上最多留两件——攒多了她会变成回访客服。
        """

        body = " ".join(str(text or "").split())[:80]
        if not body:
            return
        config = self.world.context
        delay = max(5, int(getattr(config, "open_topic_delay_minutes", 60) or 60)) * 60
        per_person = max(1, int(getattr(config, "open_topic_per_person", 2) or 2))
        now = self._now()
        who = str(getattr(ctx, "user_id", "") or "")
        session = str(getattr(ctx, "session_id", "") or state.session_id)
        items = [dict(item) for item in (state.open_topics or []) if isinstance(item, dict)]
        for item in items:
            existing = str(item.get("text") or "")
            if str(item.get("who") or "") != who:
                continue
            if existing == body or (existing and (existing in body or body in existing)):
                item["text"] = body
                item["at"] = now
                item["next_ask_at"] = now + delay
                item["asked"] = 0
                state.open_topics = items
                return
        items.append(
            {
                "text": body,
                "who": who,
                "who_name": str(getattr(ctx, "user_name", "") or ""),
                "session": session,
                "at": now,
                "next_ask_at": now + delay,
                "asked": 0,
            }
        )
        mine = [item for item in items if str(item.get("who") or "") == who]
        if len(mine) > per_person:
            keep_ids = {id(item) for item in mine[-per_person:]}
            items = [item for item in items if item not in mine or id(item) in keep_ids]
        state.open_topics = items[-8:]
        self._log("debug", f"记下一件没聊完的事：{body}")

    def mark_open_topics_asked(self, state: WorldState, ctx: Any) -> None:
        """这一轮跟他说过话：把到点的那几件标成"已经问过"，并按次数退避。"""

        if not state.open_topics:
            return
        config = self.world.context
        delay = max(5, int(getattr(config, "open_topic_delay_minutes", 60) or 60)) * 60
        max_asks = max(1, int(getattr(config, "open_topic_max_asks", 2) or 2))
        keep_days = max(1, int(getattr(config, "open_topic_days", 7) or 7))
        now = self._now()
        who = str(getattr(ctx, "user_id", "") or "")
        session = str(getattr(ctx, "session_id", "") or state.session_id)
        kept: list[dict[str, Any]] = []
        for item in state.open_topics:
            if not isinstance(item, dict):
                continue
            topic_session = str(item.get("session") or "")
            due = float(item.get("next_ask_at") or 0.0)
            aged = now - float(item.get("at") or 0.0) >= keep_days * 86400
            same_person = str(item.get("who") or "") == who
            same_place = not topic_session or topic_session == session
            if aged:
                continue
            if same_person and same_place and (not due or due <= now):
                asked = int(item.get("asked") or 0) + 1
                if asked >= max_asks:
                    continue
                item["asked"] = asked
                item["next_ask_at"] = now + delay * (asked + 1)
            kept.append(item)
        state.open_topics = kept

    def _apply_valence_delta(self, state: WorldState, raw: float) -> None:
        """把模型给的 -1~1 心情变化落到效价上（单轮上限 + 每天额度）。

        日常聊天是最高频的事，所以这里卡得比"她真经历了一件事"更紧：
        单轮最多 ``chat_valence_cap``（模型给 1.0 也只有这么多），
        一天的正向累计最多 ``chat_valence_daily_cap``（额度在 dynamics 那层统一扣，
        关键词兜底的正面脉冲也走同一份额度）。
        没有这两道闸，随便撩两句就能把效价顶满，那"100 = 极端"就不成立了，
        表达格子也会永远落在同一格（看起来就是死板）。
        """

        try:
            value = float(raw)
        except (TypeError, ValueError):
            return
        try:
            per_turn = float(
                getattr(self.world.state_dynamics, "chat_valence_cap", 0.05) or 0.0
            )
        except (TypeError, ValueError):
            per_turn = 0.05
        delta = max(-1.0, min(1.0, value)) * per_turn
        if not delta:
            return
        # 这一轮聊下来的感受：记成"心情来源"，她下一轮就知道自己为什么这样
        cause = "刚才这几句聊得开心" if delta > 0 else "刚才这几句聊得不舒服"
        self.dynamics.apply_event_delta(
            state, "valence", delta, now=self._now(), cause=cause, chat=True
        )

    # ---------------- 数值历史（给编辑器画曲线） ----------------

    async def _record_history(self, state: WorldState, node: NodeDef | None) -> None:
        """每个 tick 记一帧数值快照，顺手做数量裁剪。"""

        try:
            await self.db.call(
                "add_state_history",
                session_id=state.session_id,
                world_time=state.world_time,
                at=self._now(),
                values=self.dynamics.values(state),
            )
        except Exception as exc:  # 画曲线失败不能影响世界运行
            self._log("debug", f"写数值历史失败：{exc}")
            return
        count = self._history_writes.get(state.session_id, 0) + 1
        self._history_writes[state.session_id] = count
        if count % 60 == 0:
            try:
                await self.db.call(
                    "trim_state_history",
                    session_id=state.session_id,
                    keep=max(120, int(self.world.limits.max_history_rows)),
                )
            except Exception:
                pass

    async def state_history(self, session_id: str, *, hours: int = 24) -> dict[str, Any]:
        """取最近一段数值历史，并算出「她这段时间过得怎么样」的三个指标。"""

        span = max(1, int(hours)) * 3600
        since = self._now() - span
        rows = await self.db.call(
            "query_state_history",
            session_id=session_id,
            since=since,
            limit=max(120, int(self.world.limits.max_history_rows)),
        )
        series: list[dict[str, Any]] = []
        swing = 0.0
        peak_arousal = 0.0
        low_minutes = 0.0
        previous: dict[str, Any] | None = None
        for row in rows:
            item = {
                "at": float(row.get("at") or 0.0),
                "world_time": int(row.get("world_time") or 0),
                "affect": round(_as_float(row.get("affect")), 4),
                "valence": round(_as_float(row.get("valence"), 0.5), 4),
            }
            series.append(item)
            peak_arousal = max(peak_arousal, item["affect"])
            if previous is not None:
                minutes = max(0.0, (item["at"] - previous["at"]) / 60.0)
                swing += abs(item["affect"] - previous["affect"]) + abs(
                    item["valence"] - previous["valence"]
                )
                if item["valence"] < 0.35:
                    low_minutes += minutes
            previous = item
        return {
            "session_id": session_id,
            "hours": int(hours),
            "points": series,
            # 起伏：单位时间内的平均变化量；峰值：这段时间最激动到哪
            "metrics": {
                "swing_per_hour": round(swing / max(1.0, int(hours)), 3),
                "peak_arousal": round(peak_arousal, 3),
                "low_minutes": round(low_minutes, 1),
            },
        }

    def style_for(
        self, state: WorldState, session_id: str, *, direct: bool = False
    ) -> tuple[StyleCell | None, int, str]:
        """这一轮的表达方式：（格子、生效句数上限、进提示词的文字）。

        句数上限取**更严格者**：配置上限、格子上限、群聊硬顶（2 条），
        再加上"最近话太密"时的收紧——两条指令同时出现时不能互相抵消。

        ``direct``：这一轮是**有人正在跟她说话**（@ / 私聊 / 顺着话题对她说）。
        话太密时只提醒"短一点"，不再把她按到"能不说话就不说"——那样看着很呆。
        """

        configured = int(self.world.limits.max_messages_per_say or 3)
        if not bool(getattr(self.world, "style_injection", True)):
            state.last_style_cell = ""
            state.last_say_limit = max(1, configured)
            return None, state.last_say_limit, ""
        cell = cell_for(state.affect, state.valence)
        group = self.chat_is_group(session_id)
        limit = min(configured, cell.say_limit, 2 if group else 3)
        if self.speech_density_hint(state, direct=direct) and not direct:
            limit = min(limit, 1)
        limit = max(1, limit)
        state.last_style_cell = cell.key
        state.last_say_limit = limit
        # 长期低落时补一句"想自己待会儿"：安全阀是行为倾向，不是给她安排动作
        note = ""
        if float(state.valence) < 0.35:
            note = SELF_CARE_NOTE
        return cell, limit, render_style_block(
            cell, say_limit=limit, group=group, note=note
        )

    # ================= 接管回复（被 @ 时由本插件回复） =================

    def merge_scope(self, session_id: str) -> str:
        """合并/打断按"她"算，不按会话算。

        同一个会话组里的群和私聊是同一个她，所以一条在群里、一条在私聊也能
        互相合并 / 互相打断；不然"连说两句"永远各回一次。
        """

        return self.state_key(session_id)

    def register_incoming(self, session_id: str, text: str) -> dict[str, Any]:
        """记下"这条消息刚进来"。

        如果她正在为前一条调模型，这条可能会被**并进那一次回复**（窗口很短），
        调用方拿到这个记录后，排队轮到自己时要问一句 :meth:`was_absorbed`。
        """

        scope = self.merge_scope(session_id)
        queue = [
            item
            for item in self._pending_replies.get(scope, [])
            if not item.get("absorbed") and not item.get("picked")
        ]
        key = " ".join(str(text or "").split())
        now = time.monotonic()
        # 同一条消息被两个钩子（或重注入的副本）各记一次时只留一条：
        # 留两条的话提示词里会出现"对方把同一句话说了两遍"。
        for item in reversed(queue):
            if str(item.get("session") or "") != str(session_id or ""):
                continue
            if " ".join(str(item.get("text") or "").split()) != key:
                break
            if now - float(item.get("at") or 0.0) <= MERGE_WINDOW_SECONDS:
                return item
            break
        record = {
            "text": str(text or ""),
            # 这条是从哪个会话来的：合并只认同一个会话的连发
            # （跨会话并成一条会在群里答私聊的话），跨会话靠"按她分锁"串行就够
            "session": str(session_id or ""),
            "at": now,
            "absorbed": False,
            "picked": False,
        }
        queue.append(record)
        self._pending_replies[scope] = queue[-8:]
        return record

    @staticmethod
    def was_absorbed(record: dict[str, Any] | None) -> bool:
        """这条消息是不是已经并进别人的那次回复里了（那它自己就不用再回一遍）。"""

        return bool(record and record.get("absorbed"))

    def _take_merge_texts(
        self,
        session_id: str,
        started_at: float,
        *,
        exclude_text: str = "",
        window: float | None = None,
    ) -> list[dict[str, Any]]:
        """调用期间到达、且还在窗口内的新消息（会被并进这次请求）。"""

        scope = self.merge_scope(session_id)
        limit = float(MERGE_WINDOW_SECONDS if window is None else window)
        queue = self._pending_replies.get(scope) or []
        picked: list[dict[str, Any]] = []
        current = " ".join(str(exclude_text or "").split())
        origin = str(session_id or "")
        for item in queue:
            if item.get("absorbed") or item.get("picked"):
                continue
            if str(item.get("session") or "") != origin:
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if current and " ".join(text.split()) == current:
                # 这一轮正在答的就是它：别把它当成"调用期间新来的"（否则她会答两遍）
                item["picked"] = True
                continue
            if float(item.get("at") or 0.0) - float(started_at) > limit:
                continue
            item["picked"] = True  # 只可能被并进一次，别被下一轮又捞出来
            picked.append(item)
        self._pending_replies[scope] = [
            item for item in queue if not item.get("absorbed")
        ]
        return picked

    def _latest_incoming_at(self, session_id: str) -> float:
        """这个"她"最近一条还在等着的消息是什么时候到的（安静期用）。"""

        queue = self._pending_replies.get(self.merge_scope(session_id)) or []
        stamps = [
            float(item.get("at") or 0.0)
            for item in queue
            if not item.get("absorbed")
            and not item.get("picked")
            and str(item.get("session") or "") == str(session_id or "")
        ]
        return max(stamps) if stamps else 0.0

    def _release_records(
        self, session_id: str, records: list[dict[str, Any]]
    ) -> None:
        """这一轮作废了：把并进来的那几条恢复成"还没回过"。

        被作废的那次生成等于没发生，所以它带上的消息必须还给下一次接管——
        否则那几条会既没被回过、又谁都不再管。
        """

        scope = self.merge_scope(session_id)
        queue = [
            item
            for item in self._pending_replies.get(scope, [])
            if not item.get("absorbed") and not item.get("picked")
        ]
        for record in records:
            if not isinstance(record, dict):
                continue
            record["absorbed"] = False
            record["picked"] = False
            if not any(item is record for item in queue):
                queue.append(record)
        queue.sort(key=lambda item: float(item.get("at") or 0.0))
        self._pending_replies[scope] = queue[-8:]

    async def _wait_for_quiet(self, session_id: str) -> None:
        """安静期：先等几秒看对方还有没有下文；有新消息就重新计时（有硬顶）。

        这是"像真人打字"的核心——连发两句她会听完一起回，而不是答一句、再答一句。
        只算**同一个会话**里的话：群里说一句、私聊里说一句是两件事，
        并成一条会让她在群里答私聊的问题（跨会话只靠"按她分锁"串行）。
        """

        style = getattr(self.world, "reply_style", None)
        window = float(getattr(style, "merge_wait_seconds", 0.0) or 0.0)
        if window <= 0:
            return
        cap = float(getattr(style, "merge_wait_max_seconds", 0.0) or 0.0)
        deadline = time.monotonic() + (cap if cap > 0 else window)
        while True:
            latest = self._latest_incoming_at(session_id)
            now = time.monotonic()
            if latest <= 0 or (now - latest) >= window or now >= deadline:
                return
            await asyncio.sleep(min(0.25, max(0.05, latest + window - now)))

    def _interrupt_allowed(self, session_id: str) -> bool:
        """连着被打断太多次就先让她说完，免得刷屏的人让她永远开不了口。"""

        scope = self.merge_scope(session_id)
        count, at = self._interrupt_streak.get(scope, (0, 0.0))
        if time.monotonic() - float(at or 0.0) > INTERRUPT_STREAK_WINDOW_SECONDS:
            return True
        return count < INTERRUPT_STREAK_MAX

    def _note_interrupt(self, session_id: str) -> None:
        """记一次打断（连着打断的计数）。"""

        scope = self.merge_scope(session_id)
        count, at = self._interrupt_streak.get(scope, (0, 0.0))
        now = time.monotonic()
        if now - float(at or 0.0) > INTERRUPT_STREAK_WINDOW_SECONDS:
            count = 0
        self._interrupt_streak[scope] = (int(count) + 1, now)

    async def handle_reply(
        self,
        ctx: MessageContext,
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> ReplyOutcome:
        """接管一次回复：自己调大模型 → 解析 reasoning/actions → 执行 → 返回要发的话。

        ``ok=False`` 表示本插件没能完成这次回复（没有模型、调用失败、模型没给出任何动作），
        调用方应当回落到「注入模式」，让主人格照常回复，保证用户不会收不到回应。

        注意：调用方应当先调用 ``handle_incoming()``（记录存在感/数值/互动记忆），
        本方法只负责"读状态 → 生成回复 → 执行动作"。

        状态记录、prompt 组装在锁内完成；大模型调用放在锁外，避免拖住世界时钟。
        """

        if not self.is_enabled(ctx.session_id):
            return ReplyOutcome(ok=False, error="会话未启用")
        if not self._passes_safety(ctx.text):
            return ReplyOutcome(ok=False, error="命中内容安全屏蔽词")

        # 压测护栏：本小时被动回复额度用完了就接管但静默（不落回主人格）
        async with self.session_state(ctx.session_id) as state:
            allowed = self._passive_reply_allowed(state)
            if not allowed:
                used, limit = self.reply_quota(state)
                await self._log_event(
                    state,
                    "engagement",
                    {"reason": "被动回复触顶，这条静默", "used": used, "limit": limit},
                    place=ctx.session_id,
                )
        if not allowed:
            return ReplyOutcome(ok=False, error=REPLY_MUTED)

        # 她正在等群友拿主意：这条回应先给事件线看一眼。
        # 能消化（是冲着这件事说的）就由事件线走完剩下的流程；完全无关就照常回复。
        try:
            consumed = await self._consume_help_reply(ctx)
        except Exception as exc:
            self._log("warning", f"处理求助回应失败：{exc}")
            consumed = None
        if consumed is not None:
            return consumed

        # --- 第一阶段：记录消息影响 + 组装提示词（持锁，很快） ---
        #
        # 「合并重启」：她还在调模型时又来了新消息，就把几条并成一次请求重新发起。
        # 只在还没开始发送的窗口里做（最多 MERGE_MAX_RESTARTS 次），所以不会有"说了半句又改口"。
        # 先等一个「安静期」：对方连着打字就等他打完，一起回（新消息会刷新计时）
        wait_started = time.monotonic()
        await self._wait_for_quiet(ctx.session_id)
        early = self._take_merge_texts(
            ctx.session_id,
            wait_started,
            exclude_text=ctx.text,
            window=max(MERGE_WINDOW_SECONDS, time.monotonic() - wait_started + 1.0),
        )
        # 按到达顺序排：她要回的是"对方连着说的这几句"，顺序反了模型会读成两轮对话
        early.sort(key=lambda item: float(item.get("at") or 0.0))
        for record in early:
            record["absorbed"] = True
        texts = [*(str(item.get("text") or "") for item in early), str(ctx.text or "")]
        texts = texts[:MERGE_MAX_TEXTS]
        if self.is_event_command(ctx.text):
            # 「/vw event h」这种指令是开关，不是他说的话：原样交给她，她会一本正经地
            # 回一句"你怎么又敲这指令"。换成一句世界内的话，让她按"他刚把这一场开了起来"往下接。
            texts = ["（他把这一场开了起来）"]
        restarts = 0
        guard_strikes = 0
        wake_note_kept = ""
        raw: str | None = None
        blocked_actions: set[str] = set()
        for attempt in range(MERGE_MAX_RESTARTS + 1 + REPLY_GUARD_RETRIES):
            async with self.session_state(ctx.session_id) as state:
                node = self.node(state.node_id) or self.node(self.default_node_id())
                # 「刚被叫醒」之类的临时提示：接管模式也要带上，否则她会以完全清醒的状态回话
                wake_note = wake_note_kept or self._take_wake_note(state)
                wake_note_kept = wake_note
                extra_notes = [wake_note] if wake_note else []
                extra_notes.extend(self.sleep_notes(state))
                density = self.speech_density_hint(state, direct=True)
                if density:
                    extra_notes.append(density)
                if len(texts) > 1:
                    extra_notes.append(
                        "对方在一条之后紧接着又说了几句，都要一起回应；"
                        "不要当成两轮对话、也不要分两次回答。"
                    )
                extra_notes = extra_notes or None
                missing = self.extra_reminders(state, ctx=ctx)
                if missing:
                    extra_notes = [*(extra_notes or []), missing]
                remember_note = self.remember_hint(" ".join(texts), ctx=ctx)
                if remember_note:
                    extra_notes = [*(extra_notes or []), remember_note]
                layer = self._extension_prompt(state, ctx.session_id)
                if layer:
                    extra_notes = [*(extra_notes or []), layer]
                # 「你还不知道他的事」：只有知道是谁在说话时才算得出来
                ask = self._ask_about_hint(state, ctx)
                if ask:
                    extra_notes = [*(extra_notes or []), ask]
                memories = self.memory.recall(
                    session_id=state.session_id,
                    persona_id=ctx.persona_id,
                    node_id=state.node_id,
                    focus_user=ctx.user_id,
                    limit=self.world.limits.max_think_memory,
                )
                persona_text = await self._persona_text(state.session_id)
                # 有人正在跟她说话：话太密时只提醒别刷屏，不把她按成哑巴
                _cell, say_limit, style_text = self.style_for(
                    state, ctx.session_id, direct=True
                )
                # 配额用尽的动作：这一轮既不列进提示词，也不接受模型写它
                blocked_actions = self._hidden_actions(state, ctx.session_id)
                system_prompt = self.prompts.build_autonomous_system_prompt(
                    persona_text=persona_text,
                    state=state,
                    node=node,
                    available_tools=self.available_tools(),
                    memories=memories,
                    engagement_hint=self.engagement.hint(state),
                    recent_chat=self.chat_context_for_reply(state, ctx),
                    other_context=ctx.other_context,
                    extra_notes=extra_notes,
                    max_messages=say_limit,
                    reasoning=bool(self.world.reasoning_enabled),
                    style_block=style_text,
                    hidden_actions=blocked_actions,
                    session_directory=self.session_directory(state),
                    current_session=ctx.session_id,
                    session_labels=self.session_labels(state),
                    chat_images=dict(ctx.image_marks or {}),
                    profile_text=self.profile_block(state, ctx),
                    samples=self.voice_sample_lines(
                        state, ctx.session_id, signal=self.tone_signal(state, ctx.text)
                    ),
                    wants_touch=self.extensions.wants("touch"),
                    json_fields=self.extensions.json_fields(),
                    **await self.runtime_notes(state.session_id),
                )
                user_prompt = self.prompts.build_reply_user_prompt(
                    user_name=ctx.user_name,
                    text=texts[0],
                    is_private=ctx.is_private,
                    addressing=self.reply_addressing(state, ctx),
                    extra_texts=texts[1:],
                    from_session=self.reply_place(state, ctx.session_id),
                )
                node_id = state.node_id

            # --- 第二阶段：调用大模型（不持锁） ---
            started = time.monotonic()
            raw = await self._ask_llm(
                ctx.session_id,
                system_prompt,
                user_prompt,
                contexts=history,
                image_urls=list(ctx.image_urls) or None,
            )
            if raw is None:
                return ReplyOutcome(ok=False, error="大模型调用失败")
            # 扩展可以判"这一轮是废的"（模型丢了一句"我不能生成这类内容"）：
            # 不解析、不执行、不发送，直接重问一次；问满次数就这一轮不出声。
            if self.extensions.reply_is_bad(state, raw):
                guard_strikes += 1
                self._log(
                    "debug",
                    f"这一轮被扩展判为拒答/废输出，重新生成（第 {guard_strikes} 次）",
                )
                if guard_strikes <= REPLY_GUARD_RETRIES:
                    continue
                return ReplyOutcome(
                    ok=False,
                    error=REPLY_MUTED,
                    warnings=["模型拒答，重试后仍失败：这一轮不出声"],
                )
            if attempt >= MERGE_MAX_RESTARTS or len(texts) >= MERGE_MAX_TEXTS:
                break
            merged = self._take_merge_texts(
                ctx.session_id, started, exclude_text=ctx.text
            )
            if not merged:
                break
            if self._interrupt_pending() and self._interrupt_allowed(ctx.session_id):
                # 打断：这一轮生成直接作废，让新来的那条走自己的回复管线。
                # 水位线不推进（见 mark_chat_replied 的调用点），所以新那一轮
                # 仍然看得到刚才那几条——上一次回复等于没触发过。
                merged.sort(key=lambda item: float(item.get("at") or 0.0))
                self._release_records(ctx.session_id, [*early, *merged])
                self._log(
                    "debug",
                    f"又来了一条消息，这次生成作废（共 {len(texts) + len(merged)} 条待回）",
                )
                self._note_interrupt(ctx.session_id)
                return ReplyOutcome(
                    ok=False,
                    error=REPLY_INTERRUPTED,
                    debug_messages=self.take_pending_echo(ctx.session_id),
                )
            restarts += 1
            merged.sort(key=lambda item: float(item.get("at") or 0.0))
            for record in merged:
                record["absorbed"] = True
            # merge 回来的都是"刚到的"，排在最后 = 时间顺序
            texts.extend(str(record.get("text") or "") for record in merged)
            texts = texts[:MERGE_MAX_TEXTS]
            self._log(
                "debug",
                f"又来了一条消息，合并成一次回复重新发起（共 {len(texts)} 条）",
            )
        if raw is None:
            return ReplyOutcome(ok=False, error="大模型调用失败")

        parsed = parse_action_payload(
            raw,
            available_actions=self._parseable_action_ids(
                node_id, blocked=blocked_actions
            ),
            valid_nodes=set(self.world.node_map()),
            max_actions=self.world.limits.max_actions_per_message,
            max_messages=say_limit,
            json_fields=self.extensions.json_fields(),
        )
        # 空内容的 say 直接丢掉：模型没想好要说什么时不该发一条空气泡
        actions = [
            item
            for item in parsed.actions
            if not (item.type == "say" and not item.messages)
        ]
        if not actions:
            return ReplyOutcome(
                ok=False,
                reasoning=parsed.reasoning,
                warnings=parsed.warnings,
                error="模型没有给出任何动作",
            )

        # --- 第三阶段：执行动作（重新持锁） ---
        outcome = TickOutcome(session_id=ctx.session_id)
        # 这一轮是在哪儿发生的：日志里要标出来（她的存档与日志挂在组代表会话名下）
        outcome.place = ctx.session_id
        phase3_started = time.monotonic()
        async with self.session_state(ctx.session_id) as state:
            echo_marker = await self._event_marker(state)
            node = self.node(state.node_id) or self.node(self.default_node_id())
            state.note_reasoning(parsed.reasoning, source="reply")
            # 这一轮她对**当前说话人**的好感变化（模型自己判断，代码再削一次上限）
            if ctx.user_id:
                applied = self.profiles.apply_reply_affinity(
                    ctx.session_id,
                    ctx.user_id,
                    parsed.affinity_delta,
                    reason=str((parsed.reasoning or {}).get("intent") or "聊了这一轮")[:60],
                    now=self._now(),
                )
                if abs(applied) > 1e-9:
                    outcome.notes.append(f"好感 {applied:+.2f}")
            # 被吵醒的迷糊窗口里只准说话：不然她一边"半睡半醒"一边出门逛街，看着很假
            if self._startled_active(state):
                allowed = {"say", "think"}
                kept = [item for item in actions if item.type in allowed]
                dropped = [item.type for item in actions if item.type not in allowed]
                if dropped:
                    parsed.warnings.append(
                        "被吵醒的这一轮只让她说话 / 想事情，已丢掉："
                        + "、".join(dict.fromkeys(dropped))
                    )
                actions = kept
                if not actions:
                    return ReplyOutcome(
                        ok=False,
                        reasoning=parsed.reasoning,
                        warnings=parsed.warnings,
                        error="被吵醒的这一轮只允许说话",
                    )
            # 对方明确要求停下时，先把她的动作/安排停掉，再执行这一轮的动作
            await self.apply_cancel(state, parsed.cancel, ctx.text, outcome)
            await self._execute_actions(
                state, node, outcome, actions, depth=0, autonomous=False
            )
            # 「只要还没发出去就能打断」：动作这一段可能花很久，期间来的新消息
            # 仍然可以把这一轮作废——注意这时她的话还没写进留档、更没发出去。
            if restarts < MERGE_MAX_RESTARTS:
                late = self._take_merge_texts(
                    ctx.session_id,
                    phase3_started,
                    exclude_text=ctx.text,
                )
                if (
                    late
                    and self._interrupt_pending()
                    and self._interrupt_allowed(ctx.session_id)
                ):
                    self._release_records(ctx.session_id, [*early, *late])
                    self._log(
                        "debug",
                        f"这一轮还没发出去，被新消息打断（共 {len(late)} 条待回）",
                    )
                    self._note_interrupt(ctx.session_id)
                    return ReplyOutcome(
                        ok=False,
                        error=REPLY_INTERRUPTED,
                        # 慢动作之前她已经说出口的那几句已经发出去了：调用方别再发，
                        # 但也别当成"什么都没发生"
                        live_messages=list(outcome.live_messages),
                        live_sent=bool(outcome.live_messages),
                        debug_messages=self.take_pending_echo(ctx.session_id),
                    )
            # 扩展要补的一段（例如叙述/正文）：接在她这一轮说完之后、同一个发送批次。
            # 直接塞进 outcome.messages，所以不占"一次最多说几条"的额度。
            extra_text = await self.extensions.reply_extra(state, parsed.extra)
            if extra_text:
                outcome.messages.append(extra_text)
                await self._log_event(
                    state,
                    "ext_reply_extra",
                    {"chars": len(extra_text), "text": extra_text[:200]},
                    outcome=outcome,
                )
            # 她这次说出去的话也要进聊天上下文：下一轮提示词里才有「你最近说过的话」，
            # 模型才知道自己刚用了什么说法，才能被要求换一种。
            # 已经即时发出去的那几句也算她说过（不然她刚说"等我两分钟"就不记得了）。
            for message in outcome.said_messages():
                state.note_chat(
                    user_id="__self__",
                    name=state.bot_current_nickname or state.bot_base_nickname or "你",
                    text=message,
                    now=self._now(),
                    keep=self.chat_history_limit(),
                    is_self=True,
                    origin=ctx.session_id,
                )
                state.note_reply(message, session_id=ctx.session_id)
                self.note_dialogue(state, text=message, is_self=True)
                # 这句话是在哪个会话里说的：留档只有一份，来源要标出来
                self._tag_chat_origin(state, ctx.session_id)
            # 水位线不在这里推进：改成**确实发出去之后**由发送方通知
            # （见 main.py 里的 mark_chat_replied_by_session）。这样被丢弃的、
            # 或发送失败的回复不会把"这批消息已经回过"记下来，下一轮还会看到它们。
            self.note_chat_note(state, parsed.chat_note, ctx.session_id)
            # 模型自己标的心情变化（主路径）：只在这一轮是她真的跟人说话时接受，
            # 自主轮不写，免得她凭空给自己加心情。
            if parsed.valence_delta:
                self._apply_valence_delta(state, parsed.valence_delta)
            # 「他还有件没说完的事」：记下来，之后到点可以接一句
            self.note_open_topic(state, ctx, parsed.open_topic)
            # 「你心里搁着的事」：她自己的事，挂着，会自己淡掉
            if parsed.heart_knot:
                self.note_heart_knot(state, parsed.heart_knot, about=str(ctx.user_id or ""))
            # 「他这笔账我记着了」：冲人的，会让她对他冷一档，而且只在他面前提
            if parsed.grudge:
                self.note_grudge(
                    state,
                    parsed.grudge,
                    user_id=str(getattr(ctx, "user_id", "") or ""),
                    user_name=str(getattr(ctx, "user_name", "") or ""),
                    session_id=str(getattr(ctx, "session_id", "") or ""),
                )
            # 「他道歉了 / 补上了」：那笔账划掉
            if parsed.forgive:
                self.resolve_grudge(state, user_id=str(getattr(ctx, "user_id", "") or ""))
            # 「我自己答应过的事」：她自己的账本，跟 open_topic 正好对称
            if parsed.own_topic:
                self.note_own_topic(
                    state,
                    parsed.own_topic,
                    user_id=str(getattr(ctx, "user_id", "") or ""),
                    user_name=str(getattr(ctx, "user_name", "") or ""),
                    session_id=str(getattr(ctx, "session_id", "") or ""),
                )
            if parsed.own_topic_done:
                self.mark_own_topic_done(
                    state, user_id=str(getattr(ctx, "user_id", "") or "")
                )
            self.mark_open_topics_asked(state, ctx)
            # 对方的口吻：模型判的优先，没判才用关键词表兜底（表只认字面，会认错反话）
            # 带上"谁说的"：同样一句难听的，亲近的人说她更受伤（见 _tone_magnitude）
            await self._apply_tone_pulse(
                state,
                parsed.tone or keyword_signal(ctx.text),
                user_id=str(getattr(ctx, "user_id", "") or ""),
                outcome=outcome,
            )
            # 「他这一轮碰了她哪儿」：主插件不解释这些词，只转给声明要它的扩展
            # （没人声明时 parsed.touch 一定是空的，这里什么都不做）
            if parsed.touch:
                handled = await self.extensions.touch(state, parsed.touch)
                if handled:
                    await self._log_event(
                        state,
                        "touch",
                        {"parts": list(parsed.touch), "by": ctx.user_name or ctx.user_id},
                        outcome=outcome,
                    )
            # 扩展**自己声明**的字段：主插件同样不解释，只按声明的形状转交
            # （没人声明时 parsed.extra 一定是空的，这里什么都不做）
            if parsed.extra:
                handled = await self.extensions.extra(state, parsed.extra)
                if handled:
                    await self._log_event(
                        state,
                        "ext_fields",
                        {
                            "fields": dict(parsed.extra),
                            "by": ctx.user_name or ctx.user_id,
                        },
                        outcome=outcome,
                    )
            # 记下来给下一轮抽声音样例用：模型判的口吻比关键词表准得多
            state.last_user_tone = str(parsed.tone or "")
            # 主模型判的"这句在跟谁说话"：它比结构化信息看得多（没有 @ 的一句
            # 「你倒是说啊」它也能判出来），所以它说是别人就信它
            self._mark_reply_addressing(state, ctx, parsed.addressing)
            await self._echo_events_since(state, outcome, echo_marker)
            # 模型顺手给的总结只当"写记忆时的提示"，不单独落成一条记忆
            self.note_memory_hint(state, parsed.memory)
            await self._log_event(
                state,
                "reply",
                {
                    "user": ctx.user_name or ctx.user_id,
                    "wake": ctx.is_wake,
                    # 已经即时发出去的那几句也要算：日志里记的是她这一轮说过的全部话
                    "messages": outcome.said_messages(),
                    "reasoning": parsed.reasoning,
                    "actions": [item.type for item in actions],
                    "plan_mode": parsed.plan_mode,
                    "cancel": parsed.cancel,
                    # 模型原话 + 解析时丢掉的东西：以后排查"她为什么没做这件事"不用再猜
                    "raw": _clip_text(parsed.raw_text, 500),
                    "warnings": list(parsed.warnings),
                    "auto_travel": list(outcome.auto_travel),
                    # 这一轮用的是哪个表达格、实际允许几条：日志页能直接对照
                    "style_cell": state.last_style_cell,
                    "say_limit": int(state.last_say_limit or 0),
                    # 模型自己标的心情变化（正 = 变好，负 = 变差）
                    "valence_delta": round(float(parsed.valence_delta or 0.0), 3),
                    # 对方这一轮的口吻（模型判的）：信号不对时，日志里能一眼看出来
                    "tone": str(parsed.tone or ""),
                    # 这句是在跟谁说话（模型判的）：判成 "others" 时这一条会被标出来
                    "addressing": str(parsed.addressing or ""),
                    # 这一轮生成出来的图（生图 / 出图的动作）：日志里能看出贴了几张
                    "images": len(self._group_images(outcome)),
                },
                persona_id=ctx.persona_id,
                place=ctx.session_id,
            )

        # 没有对外发言（例如她只 think / 只换了个地方）就交回主人格，
        # 保证用户不会因为"她今天不想说话"而收不到任何回应。
        # 前面的门禁（被叫醒等）攒下的回显排在最前面——它们本来就发生在这轮之前。
        pending = self.take_pending_echo(ctx.session_id)
        quote_mode = str(
            getattr(getattr(self.world, "reply_style", None), "quote_mode", "smart")
            or "smart"
        )
        if quote_mode == "always":
            quote_hint = "always"
        elif quote_mode == "smart" and len(texts) > 1:
            # 智能：这一轮要回的是一串消息（她还在回上一条时又来新的）才引用
            quote_hint = "always"
        else:
            quote_hint = "off"
        if outcome.said_messages() or outcome.routed or outcome.routed_images:
            # 她真的开口了（哪怕只是私下补了一句）：记进本小时的被动回复额度
            async with self.session_state(ctx.session_id) as state:
                self._count_passive_reply(state)
        return ReplyOutcome(
            # 只要她在**任何地方**说了话，这一轮就算成立（不然只写私聊那句时
            # 会被判成"没产生对外发言"，交回主人格又多回一条）
            ok=bool(
                outcome.said_messages() or outcome.routed or outcome.routed_images
            ),
            messages=list(outcome.messages),
            # 已经即时发出去的那几句：调用方别再发一遍，但日志与额度要算上
            live_messages=list(outcome.live_messages),
            live_sent=bool(outcome.live_messages),
            images=self._group_images(outcome),
            routed={
                str(key): list(value)
                for key, value in (outcome.routed or {}).items()
                if list(value)
            },
            routed_images={
                str(key): list(value)
                for key, value in (outcome.routed_images or {}).items()
                if list(value)
            },
            quote_hint=quote_hint,
            reasoning=parsed.reasoning,
            warnings=parsed.warnings,
            tail=parsed.tail,
            debug_messages=pending + list(outcome.debug_messages),
            debug_positions=[0] * len(pending) + list(outcome.debug_positions),
            error=(
                "" 
                if (
                    outcome.said_messages()
                    or outcome.routed
                    or outcome.routed_images
                )
                else "模型没有产生对外发言"
            ),
        )

    def _record_user_message(
        self, state: WorldState, ctx: MessageContext
    ) -> tuple[list[str], bool]:
        """记录一条用户消息带来的全部影响，返回要给提示词的额外说明。

        注入模式和接管模式共用这一段，保证两条路径的状态演化完全一致。
        第二个返回值表示这次是不是把她叫醒了（调用方负责写一条事件日志）。
        """

        now = self._now()
        self._note_images(state, ctx.image_urls)
        state.touch_user(
            ctx.user_id or "unknown",
            name=ctx.user_name,
            anchor="near:bot" if ctx.is_wake else "topic_center",
            max_tracked=self.world.limits.max_active_users_tracked,
        )
        if not self.is_plugin_command(ctx.text):
            # 插件自己的指令（/vw …）不算"群里说的话"：它是管理入口，不是有人在跟她讲话。
            # 不挡的话她会把「/vw event 逛街被跟了」当成"主人讲给我听的事"。
            state.note_chat(
                user_id=ctx.user_id or "unknown",
                name=ctx.user_name,
                text=ctx.text,
                now=now,
                keep=self.chat_history_limit(),
                images=ctx.chat_images,
                origin=ctx.session_id,
            )
            self._tag_chat_origin(state, ctx.session_id)
        self._note_session_activity(state, ctx)
        state.last_user_activity_at = now
        self.engagement.on_user_replied(state)
        # 名字/昵称也在这儿再刷一次（不带计数，免得和 note_presence 里那次重复）
        self.profiles.touch(
            ctx.session_id, ctx.user_id, ctx.user_name, now=now, count_message=False
        )
        if ctx.is_wake or ctx.is_mentioned or ctx.is_private:
            # 他直接跟她说话了：对他的想念清零（群里别人聊得再热都不算）
            state.miss[str(ctx.user_id or "")] = 0.0
            # 而且下一次"想他"也要重新随机等一段时间，别刚聊完转头又惦记
            state.miss_ready_at[str(ctx.user_id or "")] = self._next_miss_open_at(now)
            # 「聊过」和「见过」分开记：想念只认"他直接跟她说过话"
            self.profiles.note_talked(ctx.session_id, ctx.user_id, now=now)
        self.dynamics.apply_event(state, "topic_engaged", now=now)
        # 对方的口吻**由主模型判**（`tone` 字段），在回复生成之后统一落地
        # （见 ``handle_reply`` 里的 ``_apply_tone_pulse``）。
        # 这里只做「有人叫了她」这一件事：口吻不再在这里二次触发，
        # 否则同一句"亲亲"会被算两次脉冲。
        if ctx.is_wake:
            self.dynamics.apply_event(state, "mention_bot", now=now)
        # 群里热闹这条**要限流**：以前每来一条消息就结算一次，刷屏时把孤独一路扣光。
        # 现在每 5 分钟最多算一次，而且它只给一点心潮——孤独与无聊都不动
        # （无聊驱动她换地方，不能因为群里吵就一直待着）。
        if (
            ctx.is_group_lively or self._group_is_lively(state, now=now)
        ) and self._group_lively_allowed(ctx.session_id, now):
            self.dynamics.apply_event(state, "group_lively", now=now)
            if not ctx.is_wake:
                # 群里在聊、但没人在跟她说话：更闷、更想找人说两句
                self.dynamics.apply_event(state, "left_out", now=now)

        # 醒来：冷启动之后第一次有消息，从"刚醒"转成空闲
        if state.state == STATE_AWAKENING and state.world_time > 0:
            state.state = STATE_IDLE

        notes: list[str] = []
        woke = False
        # 睡眠中被明确叫醒 -> 打断睡眠
        if self._is_asleep(state) and self._wake_hit(ctx):
            self._wake_up_state(state, ctx)
            if state.wake_note:
                notes.append(state.wake_note)
            woke = True
        notes.extend(self.sleep_notes(state))
        if not state.cold_start_done:
            notes.append(
                "你刚从沉睡中苏醒，还有点迷糊。说话可以简短、带点困意。"
                "这是你第一次在这个会话里醒来。"
            )
            state.cold_start_done = True
        return notes, woke

    async def note_presence(self, ctx: MessageContext) -> None:
        """轻量记录「谁在说话」，不触发任何 LLM 调用。

        群聊里大部分消息不会走到主人格，但 Bot 需要知道谁在场，因此单独提供这个入口。
        每条消息都会记一笔（存在感 + 最近聊天上下文），状态体量很小、写入在 SQLite WAL 下开销可控。
        """

        if not self.is_enabled(ctx.session_id):
            return
        if not ctx.user_id:
            return
        now = self._now()
        async with self.session_state(ctx.session_id) as state:
            self._note_presence(state, ctx)
            # 到达记录只在调试模式下写：正常运行时"谁说了什么、判断成什么"
            # 由意图路由插件那本流水负责，这边不用再留一份。
            if self.debug:
                await self._log_event(
                    state,
                    "incoming",
                    {
                        "user": ctx.user_name or ctx.user_id,
                        "user_id": ctx.user_id,
                        "text": _clip_log(ctx.text),
                        "wake": bool(ctx.is_wake),
                        "mentioned": bool(ctx.is_mentioned),
                        "soft": bool(ctx.is_soft_wake),
                        "private": bool(ctx.is_private),
                        "images": len(list(ctx.image_urls or [])),
                    },
                )

    # ---------------- 用户画像：提示词那一段 ----------------

    @staticmethod
    def _date_text(stamp: Any) -> str:
        """画像里的日期说成人话（``2026-09-17``）。"""

        try:
            value = float(stamp or 0.0)
        except (TypeError, ValueError):
            return ""
        if value <= 0:
            return ""
        return time.strftime("%Y-%m-%d", time.localtime(value))

    def _next_level_name(self, index: int) -> str:
        """比当前档位再高一档叫什么（到顶了返回空串）。"""

        levels = list(getattr(self.world.profile, "levels", None) or [])
        nxt = int(index) + 1
        if 0 <= nxt < len(levels):
            return str(getattr(levels[nxt], "name", "") or "")
        return ""

    def person_payload(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        """把一个人的画像整理成提示词要用的 dict（关系写日期、动作写人话）。"""

        view = self.profiles.view(session_id, user_id)
        if view is None:
            return None
        bonds: list[dict[str, Any]] = []
        for item in self.profiles.bonds(session_id, user_id, statuses=["current"]):
            bonds.append(
                {
                    "type": str(item.get("type") or ""),
                    "status": "current",
                    "since": self._date_text(item.get("since")),
                }
            )
        return {
            "user_id": view.user_id,
            "name": view.name,
            "affinities": list(view.affinities),
            "bonds": bonds,
            "claims": [
                {
                    "type": str(item.get("type") or ""),
                    "since": self._date_text(item.get("since")),
                    "asserted_by": str(item.get("asserted_by") or ""),
                }
                for item in view.claims
            ],
            "past": [
                {
                    "type": str(item.get("type") or ""),
                    "since": self._date_text(item.get("since")),
                    "until": self._date_text(item.get("until")),
                }
                for item in view.past
            ],
            "affinity": view.affinity,
            "level": {
                "name": view.level.name,
                "index": int(view.level_index or 0),
                "prompt": view.level.prompt,
                "deny": [
                    self.profiles.action_label(item) for item in (view.level.deny or [])
                ],
                # 再熟一点会到哪一档：提示词里用它说清"现在到哪儿为止"
                "next_name": self._next_level_name(int(view.level_index or 0)),
            },
            "negative": bool(getattr(view, "negative", False)),
            "facts": [
                {
                    "kind": str(item.get("kind") or "other"),
                    "text": str(item.get("text") or ""),
                }
                for item in view.facts
            ],
            "call_me": view.call_me,
            "call_him": view.call_him,
            "qq_name": view.qq_name,
            "cards": dict(view.cards or {}),
            "digest": view.digest,
            "note": view.note,
            "message_count": view.message_count,
            "days_known": view.days_known,
            # 时间锚点：提示词里要能说"上次跟你说话是多久前"
            #（想念那条线用的是同一对数，但那只在她主动找人时才算）
            "last_talked_at": view.last_talked_at,
            "last_seen_at": view.last_seen_at,
        }

    def other_people(
        self, state: WorldState, session_id: str, *, exclude_user_id: str = ""
    ) -> list[dict[str, Any]]:
        """聊天记录里最近说过话的其他人（去重，最多按配置给几个）。"""

        limit = max(1, int(self.world.profile.digest_limit))
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in reversed(self.chat_window(state, session_id)):
            user_id = str(item.get("user_id") or "")
            if not user_id or item.get("is_self") or user_id in seen:
                continue
            if exclude_user_id and user_id == str(exclude_user_id):
                continue
            seen.add(user_id)
            profile = self.profiles.profile(session_id, user_id)
            if profile is None:
                continue
            payload = dict(profile.get("payload") or {})
            names = [str(entry) for entry in (payload.get("names") or []) if str(entry)]
            bonds = self.profiles.bonds(session_id, user_id, statuses=["current"])
            rows.append(
                {
                    "user_id": user_id,
                    "name": names[-1] if names else str(payload.get("qq_name") or user_id),
                    "label": "、".join(str(bond.get("type") or "") for bond in bonds),
                    "digest": str(profile.get("digest") or ""),
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def profile_block(self, state: WorldState, ctx: MessageContext | None = None) -> str:
        """「你在跟谁说话」那一段：当前说话人全文 + 其他人各一行。"""

        if not self.profiles.enabled():
            return ""
        session_id = str(getattr(ctx, "session_id", "") or state.session_id)
        user_id = str(getattr(ctx, "user_id", "") or "")
        person = self.person_payload(session_id, user_id) if user_id else None
        if person is not None:
            person = self._apply_grudge_coldness(state, user_id, person)
        others = self.other_people(state, session_id, exclude_user_id=user_id)
        return self.prompts.profile_block(
            person, others, limit_others=int(self.world.profile.digest_limit)
        )

    def _apply_grudge_coldness(
        self, state: WorldState, user_id: str, person: dict[str, Any]
    ) -> dict[str, Any]:
        """她记着这个人的账 → 对他的**生效档位降一档**（关系、好感都不动）。

        降档走的是同一套 levels 表，所以"这一档还不能做"会自动跟着变。
        """

        config = self.world.profile
        if not bool(getattr(config, "grudge_enabled", True)):
            return person
        if self.grudge_for(state, user_id) is None:
            return person
        drop = max(0, int(getattr(config, "grudge_level_drop", 1) or 0))
        levels = list(getattr(config, "levels", None) or [])
        if drop <= 0 or not levels:
            return person
        index = max(0, int((person.get("level") or {}).get("index") or 0) - drop)
        level = levels[index]
        cold = dict(person)
        cold["level"] = {
            "name": level.name,
            "index": index,
            "prompt": level.prompt,
            "deny": [self.profiles.action_label(item) for item in (level.deny or [])],
            "next_name": self._next_level_name(index),
            # 编辑器/调试里能看出这一档是被"气着"压下来的
            "cold": True,
        }
        return cold

    # ---------------- 睡眠整理 ----------------

    CONSOLIDATE_NAP_MINUTES = 5.0
    """小睡的轻整理：睡得短，只做要点化 + 缩略版 + 一条梦（一段小睡一次）。"""

    CONSOLIDATE_MAX_PASSES = 2
    """**一段睡眠最多整理两次**：入睡后一次、睡到后半段补一次（见配置项）。"""

    # ---------------- 回想回访：到点"想起"一件事，然后主动找人 ----------------

    REVIEW_LINE_PREFIX = "# 你忽然想起"

    def _recall_focus_user(self, state: WorldState, text: str) -> str:
        """这段话里点名了谁（按画像里的名字 / 昵称 / QQ 号对）。"""

        body = str(text or "")
        if not body.strip():
            return ""
        for row in self.profiles.list_people(state.session_id, limit=200):
            user_id = str(row.get("user_id") or "")
            payload = dict(row.get("payload") or {})
            names = [str(item) for item in (payload.get("names") or []) if str(item)]
            if str(payload.get("qq_name") or ""):
                names.append(str(payload.get("qq_name")))
            for name in [*names, user_id]:
                if name and len(name) >= 2 and name in body:
                    return user_id
        return ""

    def take_due_reviews(self, state: WorldState) -> list[dict[str, Any]]:
        """到点该回访的记忆：按配额取几条，并把下一次回访往后排。

        配额（可配）：每天最多 ``recall_daily_max`` 次、同一个人每天最多
        ``recall_per_user_daily_max`` 次、同一件事 ``recall_topic_gap_days`` 天内不重复提。
        """

        config = self.world.profile
        if not bool(getattr(config, "enabled", True)):
            return []
        now = time.time()
        daily_max = max(0, int(getattr(config, "recall_daily_max", 5) or 0))
        per_user_max = max(0, int(getattr(config, "recall_per_user_daily_max", 2) or 0))
        gap_days = max(0, int(getattr(config, "recall_topic_gap_days", 7) or 0))
        if daily_max <= 0:
            return []
        log = [item for item in list(getattr(state, "review_visits", None) or []) if isinstance(item, dict)]
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        today_rows = [
            item for item in log if str(item.get("day") or "") == today
        ]
        if len(today_rows) >= daily_max:
            return []
        per_user: dict[str, int] = {}
        for item in today_rows:
            key = str(item.get("user_id") or "")
            per_user[key] = per_user.get(key, 0) + 1
        picked: list[dict[str, Any]] = []
        rows = self.memory.due_reviews(
            session_ids=self.group_sessions(state.session_id), now=now, limit=8
        )
        for row in rows:
            user_id = next(
                (
                    str(entry)
                    for entry in (row.get("participants") or row.get("related_users") or [])
                    if str(entry)
                ),
                "",
            )
            if user_id and per_user.get(user_id, 0) >= per_user_max:
                continue
            if gap_days:
                mentioned = [
                    item
                    for item in log
                    if int(item.get("memory_id") or 0) == int(row.get("id") or 0)
                ]
                if mentioned and now - float(mentioned[-1].get("at") or 0.0) < gap_days * 86400:
                    continue
            picked.append(row)
            per_user[user_id] = per_user.get(user_id, 0) + 1
            if len(picked) >= 2:
                break
        if not picked:
            return []
        for row in picked:
            step = int(row.get("recall_count") or 0)
            days = (1.0, 3.0, 7.0, 30.0)[max(0, min(step, 3))]
            self.memory.schedule_review(int(row["id"]), next_at=now + days * 86400)
            user_id = next(
                (
                    str(entry)
                    for entry in (row.get("participants") or row.get("related_users") or [])
                    if str(entry)
                ),
                "",
            )
            log.append(
                {
                    "at": now,
                    "day": today,
                    "memory_id": int(row.get("id") or 0),
                    "user_id": user_id,
                }
            )
        state.review_visits = log[-100:]
        return picked

    def review_block(self, state: WorldState) -> str:
        """「你忽然想起」那一段：到点的回访写进提示词，给她主动找人的由头。"""

        rows = self.take_due_reviews(state)
        if not rows:
            return ""
        lines: list[str] = []
        for row in rows:
            text = " ".join(str(row.get("content") or "").split())
            if not text:
                continue
            user_id = next(
                (
                    str(entry)
                    for entry in (row.get("participants") or row.get("related_users") or [])
                    if str(entry)
                ),
                "",
            )
            name = ""
            where = ""
            if user_id:
                profile = self.profiles.profile(state.session_id, user_id)
                if profile is not None:
                    payload = dict(profile.get("payload") or {})
                    names = [str(item) for item in (payload.get("names") or []) if str(item)]
                    name = names[-1] if names else str(payload.get("qq_name") or user_id)
                where = self._person_places(state, user_id)
            body = f"- {text}"
            if name:
                body += f"（关于 {name}）"
            if where:
                body += f"；想提就对他说（{where}）"
            lines.append(body)
        if not lines:
            return ""
        return (
            self.REVIEW_LINE_PREFIX
            + "\n（这些是你自己想起来的事：想提就顺口提一句，不想说也可以只放在心里）\n"
            + "\n".join(lines)
        )

    async def maybe_consolidate(self, state: WorldState) -> dict[str, Any] | None:
        """她睡着时整理这段经历；**一段睡眠最多两次**，不满足条件就什么都不做。

        节奏（都可配）：睡下 20 分钟 → 第一次（把白天的经历消化掉）；睡满 5 小时 →
        补一次（睡前最后聊的那几句这时才进去）。醒来这一段就结束了，下次睡下重新算。
        小睡只做轻整理，一段一次。
        """

        config = self.world.profile
        if not bool(getattr(config, "enabled", True)):
            return None
        if not bool(getattr(config, "consolidate_enabled", True)):
            return None
        key = str(state.session_id)
        now = self._now()
        awake = str(state.state) not in (STATE_SLEEPING, STATE_NAPPING)
        if awake:
            # 醒了：这一段睡眠结束，下次重新计时
            self._sleep_mark.pop(key, None)
            self._sleep_passes.pop(key, None)
            return None
        mark = self._sleep_mark.get(key)
        if mark is None:
            self._sleep_mark[key] = now
            self._sleep_passes[key] = 0
            return None
        elapsed = max(0.0, float(now) - float(mark))
        passes = int(self._sleep_passes.get(key) or 0)
        if str(state.state) == STATE_NAPPING:
            if not bool(getattr(config, "nap_consolidate", True)):
                return None
            if passes >= 1 or elapsed < self.CONSOLIDATE_NAP_MINUTES * 60:
                return None
            mode = "nap"
        else:
            first_after = max(1, int(getattr(config, "sleep_consolidate_minutes", 20) or 20))
            late_after = int(getattr(config, "sleep_consolidate_late_minutes", 300) or 0)
            if passes == 0:
                if elapsed < first_after * 60:
                    return None
            elif passes < self.CONSOLIDATE_MAX_PASSES and late_after > 0:
                # 第二次的条件：**睡到后半段**了（不是"距上次半小时又跑一次"）
                if elapsed < late_after * 60:
                    return None
            else:
                return None
            mode = "full"
        cursor = float(getattr(state, "consolidate_cursor", 0.0) or 0.0)
        if cursor and now - cursor < 60:
            # 同一瞬间被别的路径也触发过：只认一次
            return None
        result = await self.consolidate_now(state, mode=mode)
        if result is not None:
            self._sleep_passes[key] = passes + 1
        return result

    async def consolidate_now(
        self, state: WorldState, *, mode: str = "full", dry_run: bool = False
    ) -> dict[str, Any] | None:
        """立刻整理一次（睡眠到点 / 编辑器手动点）。

        ``dry_run=True``：只跑一遍模型给你们看（提示词 + 模型原话 + 解析结果），不写库。
        """

        if self.consolidator is None:
            return None
        session_id = str(state.session_id)
        records = self.chat_window(state, session_id)
        result = await self.consolidator.run(
            session_id=session_id,
            llm=(
                (lambda system, prompt: self._ask_consolidate(session_id, system, prompt))
                if self.consolidate_llm is not None
                else None
            ),
            mode=mode,
            chat_records=records,
            persona_text=await self._persona_text(session_id),
            pending=self._consolidate_pending(state),
            dry_run=dry_run,
        )
        if dry_run:
            return {
                "mode": mode,
                "ok": bool(result.ok),
                "note": result.note,
                "dry_run": True,
                "preview": result.preview,
                "raw": result.raw,
                "parsed": result.parsed,
            }
        # 游标用引擎时钟（和 maybe_consolidate 的记账同一个域，测试里可以推进假时钟）
        state.consolidate_cursor = self._now()
        if result.dream:
            state.last_dream = result.dream
        await self._log_event(
            state,
            "consolidate",
            {
                "mode": mode,
                "ok": bool(result.ok),
                "note": result.note or result.summary(),
                "memories": result.memories,
                "folded": result.folded,
                "facts": result.facts,
                "relations": result.relations,
                "digests": result.digests,
                "dream": result.dream,
                "skipped": result.skipped[:5],
                # 模型原话也留一份：整理结果不对时不用再复现
                "raw": result.raw[:600],
            },
        )
        return {
            "mode": mode,
            "ok": bool(result.ok),
            "note": result.note or result.summary(),
            "summary": result.summary(),
            "dream": result.dream,
            "skipped": result.skipped[:5],
            "raw": result.raw[:600],
            "counts": {
                "memories": result.memories,
                "merged": result.merged,
                "folded": result.folded,
                "facts": result.facts,
                "relations": result.relations,
                "digests": result.digests,
                "affinity": result.affinity,
            },
        }

    def _consolidate_pending(self, state: WorldState) -> list[str]:
        """还挂着的事（未完成的线索 / 约定）：整理时要优先"重放"它们。"""

        rows: list[str] = []
        thread = self._active_thread(state)
        if thread is not None:
            rows.append(f"还没完的事：{thread.get('title') or '一件事'}")
        for item in list(getattr(state, "event_digest", None) or [])[-3:]:
            rows.append(f"最近的事：{item}")
        return rows

    GROUP_LIVELY_COOLDOWN_SECONDS = 300.0
    """「群里很热闹」这条情绪脉冲最多 5 分钟结算一次（不然刷屏会把孤独扣光）。"""

    def _group_lively_allowed(self, session_id: str, now: float) -> bool:
        last = float(self._group_lively_at.get(str(session_id), 0.0) or 0.0)
        if now - last < self.GROUP_LIVELY_COOLDOWN_SECONDS:
            return False
        self._group_lively_at[str(session_id)] = float(now)
        return True

    def _update_miss(self, state: WorldState) -> None:
        """「想念」每分钟长一点：只算"她没跟这个人说话"的人。

        群里再热闹也不算陪她（见 ``EVENT_EFFECTS["group_lively"]``），所以
        群里刷一天也可能攒出"有点想主人了"——这正是主动去找他的动机。

        两件事分开看：

        - **见过**（`last_seen_at`）：他在群里露过面就算，只用来判断"他在不在"；
        - **聊过**（`last_talked_at`）：他直接跟她说过话才算陪过她——想念只认这个。

        每次清零之后会随机等一段时间（``miss_cooldown_min/max_minutes``）再开始涨，
        所以"想他"不是固定节拍：有时一下午就想，有时一整天都没想起来。

        另外**越孤独涨得越快**（``miss_loneliness_weight``）：没人陪她的时候，
        她更容易想起某个具体的人；刚聊得热乎时就不太会惦记。
        """

        config = self.world.profile
        if not bool(getattr(config, "enabled", True)):
            return
        now = self._now()
        minutes = max(0.1, float(self.tick_seconds) / 60.0)
        rate = max(0.0, float(getattr(config, "miss_growth_per_min", 0.0006) or 0.0))
        if rate <= 0:
            return
        lonely = max(0.0, min(1.0, float(state.loneliness or 0.0)))
        lonely_bonus = 1.0 + max(
            0.0, float(getattr(config, "miss_loneliness_weight", 0.8) or 0.0)
        ) * lonely
        miss = dict(state.miss or {})
        ready = dict(getattr(state, "miss_ready_at", None) or {})
        for user_id in list(state.user_presence.keys()):
            uid = str(user_id or "")
            if not uid:
                continue
            view = self.profiles.view(state.session_id, uid)
            if view is None:
                continue
            # 不喜欢的人不惦记：挂过"讨厌的人 / 敌人"、或者好感是负的，直接清零不算
            if self._miss_ignored(view):
                miss.pop(uid, None)
                ready.pop(uid, None)
                continue
            # 刚聊过 / 刚去找过他：先随机等一段，再开始想
            open_at = float(ready.get(uid) or 0.0)
            if open_at <= 0:
                open_at = self._next_miss_open_at(now)
                ready[uid] = open_at
                miss[uid] = 0.0
                continue
            if now < open_at:
                miss[uid] = 0.0
                continue
            # 越亲近越想：陌生人不会让她惦记
            bond_bonus = 0.4 * max(0, len(view.affinities) - 1)
            closeness = max(0.0, min(1.0, float(view.affinity) / 100.0))
            miss[uid] = min(1.0, float(miss.get(uid) or 0.0) + rate * minutes * (
                0.5 + closeness + bond_bonus
            ) * lonely_bonus)
        state.miss = {key: value for key, value in miss.items() if value > 0}
        state.miss_ready_at = {key: value for key, value in ready.items() if key in state.user_presence}

    def _next_miss_open_at(self, now: float) -> float:
        """下一次"开始想他"是从什么时候起：在配置区间里随机取一个。"""

        config = self.world.profile
        low = max(0.0, float(getattr(config, "miss_cooldown_min_minutes", 45) or 0)) * 60
        high = max(low, float(getattr(config, "miss_cooldown_max_minutes", 240) or 0) * 60)
        if high <= 0:
            return float(now)
        return float(now) + low + (high - low) * float(self.miss_rng.random())

    def _miss_ignored(self, view: Any) -> bool:
        """这个人值不值得惦记：负面关系 / 负好感不算。"""

        if float(getattr(view, "affinity", 0.0) or 0.0) < 0:
            return True
        for name in list(getattr(view, "affinities", None) or []):
            bond = self.world.profile.bond_by_name(str(name))
            if bond is not None and bool(getattr(bond, "negative", False)):
                return True
        return False

    @staticmethod
    def _ago_text(minutes: int) -> str:
        """把"多少分钟以前"说成人话：3 小时 / 2 天。"""

        minutes = max(0, int(minutes))
        if minutes >= 1440:
            return f"{minutes // 1440} 天"
        if minutes >= 60:
            hours = minutes // 60
            return f"{hours} 小时"
        return f"{max(1, minutes)} 分钟"

    def _miss_idle_text(self, state: WorldState, user_id: str, profile: dict) -> str:
        """「多久没聊过」（他直接跟她说话）与「多久没见过」（露过面）分开说。

        这两件事分开之后，主人才不会被"群里天天刷屏"糊弄过去：他一直在群里
        说话，但没找过她，她想念里看到的仍然是"上次跟他说话是 X 前"。
        """

        now = self._now()
        talked = self.profiles.last_talked_at(state.session_id, user_id)
        try:
            seen = float((profile or {}).get("last_seen_at") or 0.0)
        except (TypeError, ValueError):
            seen = 0.0
        parts: list[str] = []
        if talked > 0:
            parts.append(f"上次跟他说话是 {self._ago_text((now - talked) / 60)}前")
        elif seen > 0:
            parts.append("你们还没正经聊过")
        if seen > 0 and seen > talked + 60:
            parts.append(f"他 {self._ago_text((now - seen) / 60)}前在群里露过面")
        return "，".join(parts)

    def miss_overview(self, state: WorldState) -> dict[str, Any]:
        """「她想找谁」的只读快照：给编辑器看，不用去翻代码或日志。

        包含三样东西：
        - 每个人的**想念值**（0~1，到阈值她就会主动去找）；
        - 刚聊过、还在冷却里的人（想念值暂时为 0，标出还要多久才会开始想他）——
          不然"她怎么不想我"看不出来；
        - 今天的主动找人额度用掉了几次。
        """

        now = self._now()
        config = self.world.profile
        try:
            threshold = float(getattr(config, "miss_push_threshold", 0.70) or 0.70)
        except (TypeError, ValueError):
            threshold = 0.70
        presence = {
            str(item.get("user_id") or ""): str(item.get("name") or "")
            for item in (state.user_presence or {}).values()
            if isinstance(item, dict)
        }
        ready_map = dict(getattr(state, "miss_ready_at", None) or {})
        people: list[dict[str, Any]] = []
        for uid, value in (state.miss or {}).items():
            uid = str(uid or "")
            if not uid:
                continue
            view = self.profiles.view(state.session_id, uid)
            name = (getattr(view, "name", "") or presence.get(uid) or uid) if view else (
                presence.get(uid) or uid
            )
            try:
                score = float(value or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if score <= 0:
                continue
            people.append(
                {
                    "user_id": uid,
                    "name": name,
                    "value": round(score, 3),
                    "waiting": False,
                    "ready_in_minutes": 0,
                    "will_reach_out": score >= threshold,
                }
            )
        known = {item["user_id"] for item in people}
        for uid, open_at in ready_map.items():
            uid = str(uid or "")
            if not uid or uid in known:
                continue
            try:
                at = float(open_at or 0.0)
            except (TypeError, ValueError):
                at = 0.0
            if at <= now:
                continue
            minutes = max(1, int(round((at - now) / 60.0)))
            people.append(
                {
                    "user_id": uid,
                    "name": presence.get(uid) or uid,
                    "value": 0.0,
                    "waiting": True,
                    "ready_in_minutes": minutes,
                    "will_reach_out": False,
                }
            )
        people.sort(key=lambda item: (item["value"], -item["ready_in_minutes"]), reverse=True)
        try:
            cap = max(0, int(getattr(config, "miss_push_daily_max", 0) or 0))
        except (TypeError, ValueError):
            cap = 0
        return {
            "enabled": bool(getattr(config, "miss_push_enabled", True)),
            "threshold": round(threshold, 3),
            "push_left": self._miss_push_left(state),
            "push_cap": cap,
            "people": people[:8],
        }

    def _miss_push_left(self, state: WorldState) -> int:
        """今天还能软推几次（整个会话组一份，按世界日期算）。"""

        cap = max(0, int(getattr(self.world.profile, "miss_push_daily_max", 0) or 0))
        if cap <= 0:
            return 0
        today = time.strftime("%Y-%m-%d", time.localtime(self._now()))
        if str(state.miss_push_day or "") != today:
            state.miss_push_day = today
            state.miss_push_count = 0
        return max(0, cap - int(state.miss_push_count or 0))

    def _top_miss_candidate(self, state: WorldState) -> tuple[str, float] | None:
        """现在最想找、而且今天还允许主动去找的那个人。"""

        threshold = float(
            getattr(self.world.profile, "miss_push_threshold", 0.70) or 0.70
        )
        best: tuple[str, float] | None = None
        for user_id, value in (state.miss or {}).items():
            try:
                score = float(value)
            except (TypeError, ValueError):
                continue
            if score < threshold:
                continue
            uid = str(user_id or "")
            view = self.profiles.view(state.session_id, uid)
            if view is None or self._miss_ignored(view):
                continue
            if self.profiles.proactive_quota_left(state.session_id, uid) <= 0:
                continue
            if best is None or score > best[1]:
                best = (uid, score)
        return best

    # ---------------- 想被碰一碰：交给她自己安排 ----------------
    #
    # 这里**不替她决定动作**：只把"你现在很想被人碰一下"+ 手边有哪些人、
    # 哪些肢体接触、可以在哪些会话里做，一起交给大模型，让她自己挑一个。
    # （规则决策器只知道数值、不认识人，硬塞一个动作会变成"她一想要就去抱"，
    #   抱谁、抱成什么样、在群里还是私聊，那些都得看关系和她当时的心情。）

    def desire_push_ready(self, state: WorldState) -> bool:
        """现在该不该把这一轮交给大模型，让她自己想想怎么贴。"""

        config = self.world.state_dynamics
        if not bool(getattr(config, "desire_push_enabled", True)):
            return False
        if float(getattr(state, "desire", 0.0) or 0.0) <= 0.0:
            return False
        if not self._desire_push_left(state):
            return False
        gap = max(0, int(getattr(config, "desire_push_min_interval_minutes", 40) or 0))
        last = float(getattr(state, "desire_push_at", 0.0) or 0.0)
        if gap and last and self._now() - last < gap * 60:
            return False
        # 今天的基调也会拨一下：黏人的日子更容易"想要"，想独处的日子基本不会
        try:
            strength = float(getattr(config, "daily_mood_strength", 1.0))
        except (TypeError, ValueError):
            strength = 1.0
        weight = day_mood_branch(
            str(getattr(state, "day_mood", "") or ""), "cuddle", strength=strength
        )
        threshold = float(getattr(config, "desire_push_threshold", 0.75) or 0.75)
        return float(state.desire) * weight >= threshold

    def _desire_push_left(self, state: WorldState) -> int:
        """今天还能推几次（整个会话组一份，按世界日期算）。"""

        cap = max(
            0, int(getattr(self.world.state_dynamics, "desire_push_daily_max", 0) or 0)
        )
        if cap <= 0:
            return 0
        today = time.strftime("%Y-%m-%d", time.localtime(self._now()))
        if str(state.desire_push_day or "") != today:
            state.desire_push_day = today
            state.desire_push_count = 0
        return max(0, cap - int(state.desire_push_count or 0))

    def _cuddle_people(self, state: WorldState, session_id: str) -> list[dict[str, Any]]:
        """能去贴的人，**按亲密度从高到低**排。

        每人带上：现在是什么关系档、这一档明确不许的动作、今天还剩几次主动找他的额度。
        """

        rows: list[dict[str, Any]] = []
        presence = state.user_presence or {}
        for user_id in list(presence.keys()):
            uid = str(user_id or "").strip()
            if not uid:
                continue
            view = self.profiles.view(session_id, uid)
            if view is None or self._miss_ignored(view):
                continue
            level = getattr(view, "level", None)
            rows.append(
                {
                    "user_id": uid,
                    "name": str(getattr(view, "name", "") or uid),
                    "affinity": float(getattr(view, "affinity", 0.0) or 0.0),
                    "level": str(getattr(level, "name", "") or ""),
                    "deny": [str(item) for item in (getattr(level, "deny", None) or [])],
                    "quota_left": int(
                        self.profiles.proactive_quota_left(session_id, uid) or 0
                    ),
                    "places": self._person_places(state, uid),
                }
            )
        rows.sort(key=lambda item: (-item["affinity"], item["user_id"]))
        return rows

    async def _maybe_desire_push(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        session_id: str,
    ) -> bool:
        """「很想被人碰一碰」：把这一轮交给大模型，让她自己安排。

        提示词里给全三件事——**现在什么状态**、**能找谁**（按亲密度排，附关系档与
        那条档位不许的动作）、**能做哪些肢体接触**（附亲密程度），然后让她自己决定
        去哪说（``send_to``）、做什么、对谁做。什么都不想做也可以，写别的计划就行。
        """

        if self.llm is None or not self.desire_push_ready(state):
            return False
        people = [
            item for item in self._cuddle_people(state, session_id) if item["quota_left"] > 0
        ]
        selfs = self._self_comfort_options(state)
        if not people and not selfs:
            # 既没人可找、也没有"自己解决"这种动作 → 这一轮没什么可摆给她的
            return False
        actions = (
            self._cuddle_action_lines(state, people[0].get("deny") or []) if people else []
        )
        who_lines = []
        for item in people[:4]:
            deny = "、".join(
                self.world.action_map().get(name, None).name or name
                if self.world.action_map().get(name, None)
                else name
                for name in (item["deny"] or [])[:6]
            )
            where = f"；他最近在：{item['places']}" if item["places"] else ""
            who_lines.append(
                f"- {item['name']}（好感 {item['affinity']:.0f}，{item['level'] or '还不熟'}；"
                f"今天还能主动找他 {item['quota_left']} 次{where}）"
                + (f"。**跟他不能做**：{deny}" if deny else "")
            )
        parts = [
            f"你这会儿**很想被人碰一碰**（欲求 {float(state.desire):.2f}）。"
            "不是非得做什么——想凑过去蹭一下、想被摸摸头、想拉他的手、想凑近了说句话、"
            "或者只想让他注意到你，都算。**想要就直接说**——说清你想被怎么碰、"
            "想让他在哪儿、想他怎么做，别只绕着「抱紧我一点」打转；"
            "**做什么、对谁做、在哪儿做都由你定。**"
        ]
        if who_lines:
            parts.append(
                "你能找的人（越靠前越亲近）：\n"
                + "\n".join(who_lines)
                + "\n\n能做的肢体接触（括号里是贴得多近，0~1）：\n"
                + ("\n".join(actions) or "（这个地点这会儿没有能做的接触动作）")
                + "\n\n在哪说也由你定：想私密、想撒娇就发他的私聊；"
                "想让别人看见你们关系好就发群里（计划里写 send_to）。"
            )
        else:
            parts.append(
                "这会儿没有能找的人（不在身边、或者今天的次数已经用完了）——"
                "那就别硬凑上去。"
            )
        if selfs:
            listed = "、".join(f"{action_id}（{label}）" for action_id, label in selfs)
            parts.append(
                "**也可以自己解决**（如果你不想让任何人知道、或者实在没人在）："
                f"{listed}。\n"
                "　自己解决不用找谁、也不会被谁看见，但**只是没那么急了**，不算被满足——"
                "做完会有点空落、反而更想他；被人抱着是完全不一样的。"
            )
        parts.append(
            "**也可以什么都不做**：如果这会儿不方便、或者你其实不太想，"
            "那就写个别的计划（看会儿书、发会儿呆都行），不用勉强。"
        )
        hint = "\n\n".join(parts) + "\n" + self.autonomous_prompt_note(state)
        plan = await self._ask_llm_for_plan(
            state, node, outcome, force=True, hint=hint, with_extension=True
        )
        state.desire_push_at = self._now()
        state.desire_push_count = int(state.desire_push_count or 0) + 1
        if plan is None:
            outcome.notes.append("想被人碰一碰，但这一轮没想出要做什么")
            return False
        outcome.session_id = self._freeze_plan_target(
            state, outcome, plan, fallback=session_id
        )
        await self._apply_plan(state, node, outcome, plan)
        self._count_autonomous(state)
        await self._log_event(
            state,
            "desire_push",
            {
                "desire": round(float(state.desire), 3),
                "people": [item["name"] for item in people[:4]],
                "self_options": [action_id for action_id, _label in selfs],
                "plan": plan.get("steps"),
                "reason": plan.get("reason"),
            },
            outcome=outcome,
        )
        return True

    def _self_comfort_options(self, state: WorldState) -> list[tuple[str, str]]:
        """扩展提供的"不用找人也能解决"的动作：动作上打了 ``self_option`` 标记的。

        主插件不认识具体是哪个扩展：**谁注册了带这个标记的动作，就多出那个选项**。
        没装扩展 → 空列表 → 那一轮只摆「找谁」和「什么都不做」。
        """

        picked: list[tuple[str, str]] = []
        for definition in self.world.actions_in(state.node_id):
            if not bool(getattr(definition, "enabled", True)):
                continue
            label = str(getattr(definition, "self_option", "") or "").strip()
            if not label:
                continue
            picked.append((definition.id, label))
        return picked

    def _cuddle_action_lines(
        self, state: WorldState, deny: list[str] | None = None
    ) -> list[str]:
        """这个地点现在能做的"肢体接触"动作，一行一个（附亲密程度）。"""

        blocked = {str(item) for item in (deny or [])}
        rows: list[tuple[float, str]] = []
        for definition in self.world.actions_in(state.node_id):
            if not bool(getattr(definition, "enabled", True)):
                continue
            if str(getattr(definition, "target_type", "none") or "none") != "user":
                continue
            if definition.id in blocked:
                continue
            score = action_intimacy(definition)
            if score <= 0:
                continue
            rows.append((score, definition.id))
        rows.sort(key=lambda item: -item[0])
        lines: list[str] = []
        for score, action_id in rows[:10]:
            definition = self.world.action_map().get(action_id)
            name = (definition.name or action_id) if definition is not None else action_id
            lines.append(f"- {action_id}（{name}，{score:.1f}）")
        return lines

    def desire_panel(self, state: WorldState) -> dict[str, Any]:
        """编辑器「想念」那一栏旁边要显示的欲求信息（含"还要多久才轮到她主动"）。"""

        config = self.world.state_dynamics
        return {
            "value": round(float(getattr(state, "desire", 0.0) or 0.0), 3),
            "threshold": round(
                float(getattr(config, "desire_push_threshold", 0.75) or 0.75), 3
            ),
            "enabled": bool(getattr(config, "desire_push_enabled", True)),
            "push_left": self._desire_push_left(state),
            "push_cap": max(0, int(getattr(config, "desire_push_daily_max", 0) or 0)),
            "last_push_at": float(getattr(state, "desire_push_at", 0.0) or 0.0),
        }

    async def _maybe_miss_push(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        session_id: str,
    ) -> bool:
        """「想他想到不行了」：软推一次，让她自己去把他找出来。

        在哪说由她自己定：想私密、想撒娇就发私聊；想让别人看到关系好就发群里——
        提示词里把她能说话的地方都摆出来（``session_directory``），她照着挑一个写 ``send_to``。
        """

        config = self.world.profile
        if not bool(getattr(config, "enabled", True)):
            return False
        if not bool(getattr(config, "miss_push_enabled", True)):
            return False
        if self._miss_push_left(state) <= 0:
            return False
        candidate = self._top_miss_candidate(state)
        if candidate is None or self.llm is None:
            return False
        uid, score = candidate
        profile = self.profiles.profile(state.session_id, uid)
        payload = dict((profile or {}).get("payload") or {})
        names = [str(item) for item in (payload.get("names") or []) if str(item)]
        name = names[-1] if names else str(payload.get("qq_name") or uid)
        idle = self._miss_idle_text(state, uid, profile or {})
        where = self._person_places(state, uid)
        hint = (
            f"你这会儿特别想 {name}（{idle or '刚想起来'}），想去跟他说句话。\n"
            "**在哪说由你自己决定**：想私密一点、想撒娇、想问他一句只给他听的话，"
            "就发到他的私聊；想让别人都看到你们关系好，就发到你们都在的群里。"
            "想先做点什么（走动、倒杯茶顺手提醒他喝水）再开口也可以，一起排进计划。\n"
            "注意：**换地方说 ≠ 把刚才那句话再说一遍**。"
            "如果你刚在群里说过一句、没人理，那就别把它原样搬到私聊——"
            "要么接着往下说（“刚才那个…”），要么说点别的（你正在做的事、你自己的事）。\n"
            f"他现在能在这些地方跟你说上话：{where or '（还没记录到他最近在哪儿说话）'}。\n"
            + self.autonomous_prompt_note(state)
            + "\n按你自己的节奏安排这一轮要做什么，输出计划 JSON。"
        )
        plan = await self._ask_llm_for_plan(state, node, outcome, force=True, hint=hint)
        if plan is None:
            outcome.notes.append(f"想找 {name}，但这一轮没想出要说什么")
            # 想不出来就先把这个人的想念压一半，别每轮都来问一次
            state.miss[uid] = max(0.0, score * 0.5)
            return False
        outcome.session_id = self._freeze_plan_target(
            state, outcome, plan, fallback=session_id
        )
        await self._apply_plan(state, node, outcome, plan)
        self._count_autonomous(state)
        self.profiles.note_proactive(state.session_id, uid)
        state.miss[uid] = 0.0
        # 刚找过他：下一次"想他"重新随机等一段时间
        state.miss_ready_at[uid] = self._next_miss_open_at(self._now())
        state.miss_push_count = int(state.miss_push_count or 0) + 1
        await self._log_event(
            state,
            "miss_push",
            {
                "user": uid,
                "score": round(score, 3),
                "note": str(plan.get("reason") or "")[:80],
            },
            outcome=outcome,
        )
        self._log("debug", f"[virtual_world] 想他想到主动去找：{uid}")
        await self._tick_plan(state, node, outcome, depth=0)
        return True

    def miss_block(self, state: WorldState) -> str:
        """「有点想他」那一段：超过阈值的人才写，附带他能在哪儿说话。"""

        config = self.world.profile
        if not bool(getattr(config, "enabled", True)):
            return ""
        threshold = float(getattr(config, "miss_threshold", 0.6) or 0.6)
        limit = max(1, int(getattr(config, "miss_limit", 3) or 3))
        rows: list[tuple[float, str]] = []
        for user_id, value in (state.miss or {}).items():
            try:
                score = float(value)
            except (TypeError, ValueError):
                continue
            if score < threshold:
                continue
            rows.append((score, str(user_id)))
        rows.sort(reverse=True)
        lines: list[str] = []
        for _score, user_id in rows[:limit]:
            profile = self.profiles.profile(state.session_id, user_id)
            if profile is None:
                continue
            payload = dict(profile.get("payload") or {})
            names = [str(item) for item in (payload.get("names") or []) if str(item)]
            name = names[-1] if names else str(payload.get("qq_name") or user_id)
            idle = self._miss_idle_text(state, user_id, profile)
            where = self._person_places(state, user_id)
            body = f"- {name}"
            if idle:
                body += f"（{idle}）"
            if where:
                body += f"：找他可以说给 {where}"
            else:
                body += "：你有点想他"
            lines.append(body)
        if not lines:
            return ""
        return (
            "# 你有点想他们了\n"
            "（不用马上做什么；真想找人就主动说一句，落点写上面那个）\n" + "\n".join(lines)
        )

    def extra_reminders(
        self, state: WorldState, *, autonomy: bool = False, ctx: Any = None
    ) -> str:
        """给提示词的几段提醒：有点想谁 + 刚想起的事（后者读过一次就不再重复提）。

        ``autonomy``：这一轮是"她自己决定要做什么"（不是回谁的话）。只有这时候才把
        「好奇心上来了，想去问问谁」带进去——回话时带这段等于把话题拐到第三个人身上。

        ``ctx``：正在跟谁说话。心事 / 记着的账 / 她自己的事都是**分人**的，
        不回话的场合（``ctx`` 为空）就按"她自己心里的事"带上名字。
        """

        parts = [self.miss_block(state)]
        parts.append(self._greet_hint(state))
        parts.append(self._heart_knot_block(state, ctx, autonomy=autonomy))
        parts.append(self._grudge_block(state, ctx, autonomy=autonomy))
        parts.append(self._own_topic_block(state, ctx, autonomy=autonomy))
        if autonomy:
            parts.append(self._ask_about_block(state))
        pending = str(getattr(state, "pending_review", "") or "").strip()
        if pending:
            parts.append(pending)
            state.pending_review = ""
        return "\n\n".join(part for part in parts if part)

    def _note_greet_candidate(
        self, state: WorldState, ctx: MessageContext, *, now: float
    ) -> None:
        """有人隔了大半天又冒头 → 记一笔，让提示词里提一句"这人好久没来了"。

        只在群里算：私聊本来就是一对一，谈不上"回来打个招呼"。
        阈值、每人每天一次、全群每天几次都在「自主决策」里配。
        """

        config = self.world.decider
        gap_hours = max(0, int(getattr(config, "greet_gap_hours", 0) or 0))
        if gap_hours <= 0 or ctx.is_private:
            return
        uid = str(ctx.user_id or "")
        if not uid:
            return
        previous = float((state.user_presence.get(uid) or {}).get("last_seen") or 0.0)
        if previous <= 0:
            return  # 第一次见到他，不算"好久没来"
        gap = (float(now) - previous) / 3600.0
        if gap < gap_hours:
            return
        day = self._today_key(now)
        if str(state.greet_done.get(uid) or "") == day:
            return
        daily_max = max(0, int(getattr(config, "greet_daily_max", 0) or 0))
        if daily_max <= 0:
            return
        state.greet_pending[uid] = {
            "name": str(ctx.user_name or uid),
            "gap_hours": round(gap, 1),
            "at": float(now),
        }

    def _greet_hint(self, state: WorldState) -> str:
        """「这人好久没来了」那段提示词（读过一次就把这一笔销掉）。"""

        config = self.world.decider
        day = self._today_key(self._now())
        if str(state.greet_day or "") != day:
            state.greet_day = day
            state.greet_count = 0
        daily_max = max(0, int(getattr(config, "greet_daily_max", 0) or 0))
        if daily_max <= 0:
            state.greet_pending = {}
            return ""
        if int(state.greet_count or 0) >= daily_max:
            state.greet_pending = {}
            return ""
        picked: list[str] = []
        for uid, info in list((state.greet_pending or {}).items()):
            if len(picked) >= max(1, daily_max - int(state.greet_count or 0)):
                break
            name = str((info or {}).get("name") or uid)
            gap = float((info or {}).get("gap_hours") or 0.0)
            when = f"{gap / 24:.0f} 天" if gap >= 24 else f"{gap:.0f} 小时"
            picked.append(f"- {name}（QQ {uid}）上次露面是 {when} 前，刚回来了")
            state.greet_done[uid] = day
            state.greet_count = int(state.greet_count or 0) + 1
            state.greet_pending.pop(uid, None)
        if not picked:
            return ""
        return (
            "# 有人好久没来了\n"
            "这人好久没来了，刚在群里冒头。**可以戳一戳 ta 并打个招呼**——"
            "一句话就够，别追问人家去哪了，也别拉着一块儿聊。\n" + "\n".join(picked)
        )

    def _ask_about_hint(self, state: WorldState, ctx: MessageContext | None) -> str:
        """「你还不知道他的这些事」那段提示词。

        关系熟了之后才会出现；一次只提一件、同一件有冷却、每天有上限——
        不这么做就是查户口，不是关心。
        """

        config = self.world.profile
        if not bool(getattr(config, "ask_about_enabled", True)):
            return ""
        if ctx is None or not str(getattr(ctx, "user_id", "") or ""):
            return ""
        wanted = [str(item) for item in (getattr(config, "ask_about_fields", None) or []) if str(item)]
        if not wanted:
            return ""
        uid = str(ctx.user_id or "")
        view = self.profiles.view(state.session_id, uid)
        if view is None:
            return ""
        floor = max(0, int(getattr(config, "ask_about_min_level", 0) or 0))
        if int(getattr(view, "level_index", 0) or 0) < floor:
            return ""
        known = " ".join(str(item.get("text") or "") for item in (view.facts or []))
        now = self._now()
        day = self._today_key(now)
        if str(state.ask_day or "") != day:
            state.ask_day = day
            state.ask_count = 0
        room = max(0, int(getattr(config, "ask_about_daily_max", 0) or 0)) - int(
            state.ask_count or 0
        )
        if room <= 0:
            return ""
        cooldown = max(0.0, float(getattr(config, "ask_about_cooldown_days", 7) or 7)) * 86400.0
        log = dict((state.ask_log or {}).get(uid) or {})
        # 对同一个人：一天最多提一件（不然一晚上把生日年龄性别挨个问一遍，像查户口）
        person_gap = max(
            0.0, float(getattr(config, "ask_about_person_gap_hours", 24) or 24)
        ) * 3600.0
        if person_gap > 0 and now - float(log.get("_last") or 0.0) < person_gap:
            return ""
        missing = [item for item in wanted if item not in known]
        missing = [
            item for item in missing if now - float(log.get(item) or 0.0) >= cooldown
        ]
        if not missing:
            return ""
        picked = missing[0]
        log[picked] = now
        log["_last"] = now
        state.ask_log[uid] = log
        state.ask_count = int(state.ask_count or 0) + 1
        others = "、".join(missing[1:3])
        return (
            "# 你还不知道他的事\n"
            f"你到现在还不知道他的{picked}。**合适的时候自然问一句就行**（你自己找时机，"
            "别在人家正说着别的的时候硬插）。"
            + (f"另外也不知道他的{others}，以后有机会再说。" if others else "")
        )

    def _ask_about_block(self, state: WorldState) -> str:
        """「好奇心上来了，想去问问谁」那一段：只给自主决策用。

        好奇高的时候除了上网查，还可以去问人——这条补的是"搜索不是唯一出路"。
        问谁、问什么跟回话路径共用一套配额（每天几次、对同一个人隔多久、
        同一件事多久内不再问），所以好奇心不会变成查户口。

        回话路径不掺这段：别人正说着话，她突然打听第三个人的生日，那是跳戏。
        """

        config = self.world.profile
        if not bool(getattr(config, "ask_about_enabled", True)):
            return ""
        if float(state.curiosity) < ASK_ABOUT_CURIOSITY:
            return ""
        wanted = [
            str(item)
            for item in (getattr(config, "ask_about_fields", None) or [])
            if str(item)
        ]
        if not wanted:
            return ""
        now = self._now()
        day = self._today_key(now)
        if str(state.ask_day or "") != day:
            state.ask_day = day
            state.ask_count = 0
        room = max(0, int(getattr(config, "ask_about_daily_max", 0) or 0)) - int(
            state.ask_count or 0
        )
        if room <= 0:
            return ""
        floor = max(0, int(getattr(config, "ask_about_min_level", 0) or 0))
        cooldown = (
            max(0.0, float(getattr(config, "ask_about_cooldown_days", 7) or 7)) * 86400.0
        )
        person_gap = (
            max(0.0, float(getattr(config, "ask_about_person_gap_hours", 24) or 24))
            * 3600.0
        )
        # 先问"提示词里已经出现的人"：她刚看着这人说过话，顺口问一句最自然，
        # 而且「群里还有谁」那几行里已经有他的缩略版，不至于问到个她全无概念的人。
        order: list[str] = []
        for item in self.other_people(state, state.session_id):
            uid = str(item.get("user_id") or "")
            if uid and uid not in order:
                order.append(uid)
        for row in self.profiles.list_people(state.session_id, limit=12):
            uid = str(row.get("user_id") or "")
            if uid and uid not in order:
                order.append(uid)
        for uid in order:
            view = self.profiles.view(state.session_id, uid)
            if view is None or bool(getattr(view, "negative", False)):
                continue
            if int(getattr(view, "level_index", 0) or 0) < floor:
                continue
            log = dict((state.ask_log or {}).get(uid) or {})
            if person_gap > 0 and now - float(log.get("_last") or 0.0) < person_gap:
                continue
            known = " ".join(str(item.get("text") or "") for item in (view.facts or []))
            missing = [
                item
                for item in wanted
                if item not in known and now - float(log.get(item) or 0.0) >= cooldown
            ]
            if not missing:
                continue
            picked = missing[0]
            log[picked] = now
            log["_last"] = now
            state.ask_log[uid] = log
            state.ask_count = int(state.ask_count or 0) + 1
            name = str(getattr(view, "name", "") or uid)
            bonds = "、".join(
                str(item) for item in (getattr(view, "affinities", None) or []) if str(item)
            )
            digest = str(getattr(view, "digest", "") or "").strip()
            who = f"- 他是谁：{name}" + (f"（{bonds}）" if bonds else "")
            if digest:
                who += f"，你记得他：{digest}"
            where = self._person_places(state, uid)
            tail = (
                f"- 想问就问一句，落点写这儿：{where}"
                if where
                else "- 想问就问一句"
            )
            others = "、".join(missing[1:3])
            return (
                "# 好奇心上来了\n"
                f"你到现在还不知道{name}的{picked}，这事最近老在你脑子里转。\n"
                f"{who}\n{tail}\n"
                "**随口一问就行，别像查户口**。"
                + (
                    f"（他的{others}你也不知道，以后有机会再说。）"
                    if others
                    else ""
                )
            )
        return ""

    def _heart_knot_block(
        self, state: WorldState, ctx: Any = None, *, autonomy: bool = False
    ) -> str:
        """「心里搁着的事」那一段：挂着、不推进，影响她说话的样子。

        心事是**分了人的**（写进来时记了 ``about``）：跟谁说话就只想谁的那件，
        不然她会把 A 的事拿到 B 面前说，还容易认错人。
        自主决策那一路把名字带上（她可能要为此去找人）。
        """

        who = str(getattr(ctx, "user_id", "") or "")
        lines: list[str] = []
        for item in list(state.heart_knots or [])[:3]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            about = str(item.get("about") or "")
            # 跟谁说话就只想谁的事；没有归属的（早期数据 / 整理时发现的）两边都带
            if who and about and about != who:
                continue
            since = float(item.get("since") or 0.0)
            days = (self._now() - since) / 86400.0 if since > 0 else 0.0
            age = f"（搁了 {days:.0f} 天了）" if days >= 1 else ""
            label = ""
            if autonomy and about and about != who:
                name = self._user_label(state, about)
                label = f"（关于 {name}）" if name else ""
            lines.append(f"- {label}{text}{age}")
        if not lines:
            return ""
        return (
            "# 你心里搁着的事\n"
            "这些不是任务、也不用去解决，只是你心里还惦记着——说话时会带出来一点："
            "要么语气淡一些，要么在合适的时机主动提一句。**别反复念叨、也别硬把话题拽过去**。\n"
            + "\n".join(lines)
        )

    def _user_label(self, state: WorldState, user_id: str) -> str:
        """这个 QQ 号在她眼里叫什么（拿不到名字就回落到号码）。"""

        uid = str(user_id or "")
        if not uid:
            return ""
        if self.profiles.enabled():
            profile = self.profiles.profile(state.session_id, uid)
            if profile is not None:
                payload = dict(profile.get("payload") or {})
                names = [str(item) for item in (payload.get("names") or []) if str(item)]
                if names:
                    return names[-1]
                if str(payload.get("qq_name") or ""):
                    return str(payload["qq_name"])
        return uid

    # ---------------- 她记着的账（记仇） ----------------

    def grudge_for(self, state: WorldState, user_id: str) -> dict[str, Any] | None:
        """这个人身上有没有她还记着的账。"""

        uid = str(user_id or "")
        if not uid:
            return None
        for item in list(state.grudges or []):
            if isinstance(item, dict) and str(item.get("user_id") or "") == uid:
                return item
        return None

    def note_grudge(
        self,
        state: WorldState,
        reason: str,
        *,
        user_id: str = "",
        user_name: str = "",
        session_id: str = "",
        force: bool = False,
    ) -> bool:
        """记一笔账：他做了什么让她现在还气着。

        门槛（配额）是刻意卡紧的：同一个人最多一笔、一天最多记一笔新的、
        同时最多 ``grudge_max`` 笔——不然她会变成一个天天记仇的人。
        ``force``：编辑器里手动加的（不走"每天最多一笔"那道闸）。
        """

        config = self.world.profile
        if not bool(getattr(config, "grudge_enabled", True)):
            return False
        body = " ".join(str(reason or "").split())[:60]
        uid = str(user_id or "")
        if not body or not uid:
            return False
        now = self._now()
        day = self._today_key(now)
        if str(state.grudge_day or "") != day:
            state.grudge_day = day
            state.grudge_count = 0
        daily = max(0, int(getattr(config, "grudge_daily_max", 1) or 0))
        if not force and int(state.grudge_count or 0) >= daily:
            self._log("debug", "今天记的账够多了，这一笔不记")
            return False
        items = [dict(item) for item in (state.grudges or []) if isinstance(item, dict)]
        # 同一个人只留一笔：这件事比那件事更气人，就换掉，但不叠加
        existing = next((item for item in items if str(item.get("user_id") or "") == uid), None)
        if existing is not None and body in str(existing.get("reason") or ""):
            return False
        keep_days = max(1, int(getattr(config, "grudge_days", 7) or 7))
        record = {
            "user_id": uid,
            "who_name": str(user_name or "") or self._user_label(state, uid),
            "reason": body,
            "at": now,
            "until": now + keep_days * 86400.0,
            "session": str(session_id or "") or state.session_id,
        }
        items = [item for item in items if str(item.get("user_id") or "") != uid]
        items.append(record)
        limit = max(1, int(getattr(config, "grudge_max", 2) or 2))
        state.grudges = items[-limit:]
        state.grudge_count = int(state.grudge_count or 0) + 1
        # 当场气一下：只给一次脉冲，之后靠"冷一档 + 提示词"，不再反复扣
        self.dynamics.apply_event(state, "grudge", now=float(now))
        self._log("debug", f"记了一笔账：{body}")
        return True

    def resolve_grudge(self, state: WorldState, *, user_id: str = "") -> bool:
        """这笔账算了：他道歉 / 补上 / 解释清楚了。返回有没有真的划掉一笔。"""

        uid = str(user_id or "")
        record = self.grudge_for(state, uid)
        if record is None:
            return False
        state.grudges = [
            item
            for item in (state.grudges or [])
            if not (isinstance(item, dict) and str(item.get("user_id") or "") == uid)
        ]
        reason = str(record.get("reason") or "").strip()
        name = str(record.get("who_name") or "") or self._user_label(state, uid)
        self.memory.remember(
            session_id=state.session_id,
            persona_id="",
            node_id=state.node_id,
            content=f"（跟他算清了一笔账）{name}：{reason}"[:120],
            memory_type=INNER,
            emotion=state.mood,
            weight=0.35,
            affect=state.affect,
            valence=state.valence,
        )
        self._log("debug", f"这笔账算了：{reason}")
        return True

    def _decay_grudges(self, state: WorldState, *, now: float) -> None:
        """到期的账自己淡掉（写进记忆），不留着一个半年前的仇。"""

        config = self.world.profile
        if not bool(getattr(config, "grudge_enabled", True)):
            state.grudges = []
            return
        keep: list[dict[str, Any]] = []
        for item in list(state.grudges or []):
            if not isinstance(item, dict):
                continue
            until = float(item.get("until") or 0.0)
            if until > 0 and float(now) >= until:
                reason = str(item.get("reason") or "").strip()
                name = str(item.get("who_name") or "")
                if reason:
                    self.memory.remember(
                        session_id=state.session_id,
                        persona_id="",
                        node_id=state.node_id,
                        content=f"（气过一阵就算了）{name}：{reason}"[:120],
                        memory_type=INNER,
                        emotion=state.mood,
                        weight=0.3,
                        affect=state.affect,
                        valence=state.valence,
                    )
                continue
            keep.append(item)
        state.grudges = keep

    def _grudge_block(
        self, state: WorldState, ctx: Any = None, *, autonomy: bool = False
    ) -> str:
        """「他还欠着我一笔」那一段。

        只对当事人说：跟别人说话时一个字都不提（这是"她记着这个人的账"，
        不是"她今天心情不好"）。
        """

        config = self.world.profile
        if not bool(getattr(config, "grudge_enabled", True)):
            return ""
        who = str(getattr(ctx, "user_id", "") or "")
        lines: list[str] = []
        for item in list(state.grudges or [])[:2]:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("user_id") or "")
            if who and uid and uid != who:
                continue
            reason = str(item.get("reason") or "").strip()
            if not reason:
                continue
            days = max(0.0, (self._now() - float(item.get("at") or 0.0)) / 86400.0)
            age = f"（{days:.0f} 天前的事）" if days >= 1 else ""
            label = ""
            if autonomy and uid and uid != who:
                name = str(item.get("who_name") or "") or self._user_label(state, uid)
                label = f"{name}：" if name else ""
            lines.append(f"- {label}{reason}{age}")
        if not lines:
            return ""
        return (
            "# 你还记着他一笔账\n"
            "他有一件事到现在还没给你个说法。**你现在对他还气着**：说话可以短一点、"
            "淡一点、可以带刺，但**别翻旧账、别每轮都提**，也别拿这件事去跟别人说。"
            "他要是道歉了、补上了，你心里那口气就松了——可以顺台阶下，也可以再嘴硬两句。\n"
            + "\n".join(lines)
        )

    # ---------------- 她自己的事（答应过的、想做的） ----------------

    def note_own_topic(
        self,
        state: WorldState,
        text: str,
        *,
        user_id: str = "",
        user_name: str = "",
        session_id: str = "",
    ) -> bool:
        """记一件**她自己**的事：答应过别人的、自己想做的，还没做。"""

        config = self.world.state_dynamics
        if not bool(getattr(config, "own_topic_enabled", True)):
            return False
        body = " ".join(str(text or "").split())[:60]
        if not body:
            return False
        now = self._now()
        uid = str(user_id or "")
        items = [
            dict(item) for item in (state.own_topics or []) if isinstance(item, dict)
        ]
        for item in items:
            existing = str(item.get("text") or "")
            if existing == body or (existing and (existing in body or body in existing)):
                item["text"] = body
                item["at"] = now
                state.own_topics = items
                return True
        items.append(
            {
                "text": body,
                "who": uid,
                "who_name": str(user_name or ""),
                "session": str(session_id or "") or state.session_id,
                "at": now,
            }
        )
        limit = max(1, int(getattr(config, "own_topic_max", 2) or 2))
        state.own_topics = items[-limit:]
        self._log("debug", f"记下一件自己的事：{body}")
        return True

    def mark_own_topic_done(self, state: WorldState, *, user_id: str = "") -> int:
        """她自己那件事做完了：划掉。返回划掉了几件。"""

        uid = str(user_id or "")
        kept: list[dict[str, Any]] = []
        removed = 0
        for item in list(state.own_topics or []):
            if not isinstance(item, dict):
                continue
            mine = str(item.get("who") or "")
            # 只划掉跟当前这个人相关的；没写对谁的就当是她自己的打算
            if not mine or not uid or mine == uid:
                removed += 1
                continue
            kept.append(item)
        state.own_topics = kept
        if removed:
            self._log("debug", f"划掉了 {removed} 件她自己的事")
        return removed

    def _decay_own_topics(self, state: WorldState, *, now: float) -> None:
        """放太久的自己那点事：要么早做完了、要么她其实不在意，丢掉。"""

        keep_days = max(1, int(getattr(self.world.state_dynamics, "own_topic_days", 3) or 3))
        state.own_topics = [
            item
            for item in list(state.own_topics or [])
            if isinstance(item, dict)
            and float(now) - float(item.get("at") or now) < keep_days * 86400.0
        ]

    def _own_topic_block(
        self, state: WorldState, ctx: Any = None, *, autonomy: bool = False
    ) -> str:
        """「你自己还记着的事」那一段：她答应过、想做但还没做的。"""

        config = self.world.state_dynamics
        if not bool(getattr(config, "own_topic_enabled", True)):
            return ""
        who = str(getattr(ctx, "user_id", "") or "")
        lines: list[str] = []
        for item in list(state.own_topics or [])[:2]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            owner = str(item.get("who") or "")
            if who and owner and owner != who:
                continue
            when = self._said_when_text(float(item.get("at") or 0.0))
            label = "（你自己记着的）"
            if autonomy and owner and owner != who:
                name = str(item.get("who_name") or "") or self._user_label(state, owner)
                label = f"（对 {name} 说的）" if name else "（对别人说的）"
            lines.append(f"- {label}{text}{when}")
        if not lines:
            return ""
        return (
            "# 你自己还记着的事\n"
            "这些是**你自己**答应过、或者想做的，还没做完。想起来了就顺手做掉——"
            "比如答应给他看照片就把照片发出去；也可以顺口提一句（「上次说要给你看的」）。\n"
            "**别当成任务清单逐条念**，也不用解释这是哪来的。\n" + "\n".join(lines)
        )

    def _said_when_text(self, stamp: float) -> str:
        """「（今天记的）/（3 天前记的）」这种尾巴。"""

        if stamp <= 0:
            return ""
        days = (self._now() - stamp) / 86400.0
        if days >= 1:
            return f"（{days:.0f} 天前记的）"
        return ""

    def _decay_heart_knots(self, state: WorldState, *, now: float) -> None:
        """心事随时间淡掉、到期放下（写进记忆）。"""

        config = self.world.state_dynamics
        if not bool(getattr(config, "heart_knot_enabled", True)):
            state.heart_knots = []
            return
        keep: list[dict[str, Any]] = []
        per_hour = max(0.0, float(getattr(config, "heart_knot_decay_per_hour", 0.0) or 0.0))
        for item in list(state.heart_knots or []):
            if not isinstance(item, dict):
                continue
            last = float(item.get("at") or item.get("since") or now)
            hours = max(0.0, (float(now) - last) / 3600.0)
            strength = float(item.get("strength") or 0.0) - per_hour * hours
            until = float(item.get("until") or 0.0)
            expired = until > 0 and float(now) >= until
            if strength <= 0.02 or expired:
                text = str(item.get("text") or "").strip()
                if text:
                    self.memory.remember(
                        session_id=state.session_id,
                        persona_id="",
                        node_id=state.node_id,
                        content=f"（心里搁过的事）{text}"[:120],
                        memory_type=INNER,
                        emotion=state.mood,
                        weight=0.3,
                        affect=state.affect,
                        valence=state.valence,
                    )
                continue
            keep.append({**item, "strength": round(strength, 4), "at": float(now)})
        state.heart_knots = keep

    def note_heart_knot(self, state: WorldState, text: str, about: str = "") -> None:
        """记下一件心事（她自己在回复里写的，或者整理时发现的）。"""

        config = self.world.state_dynamics
        if not bool(getattr(config, "heart_knot_enabled", True)):
            return
        body = " ".join(str(text or "").split())[:60]
        if not body:
            return
        now = self._now()
        max_days = max(0.0, float(getattr(config, "heart_knot_max_days", 5.0) or 5.0))
        items = [
            item
            for item in list(state.heart_knots or [])
            if str(item.get("text") or "").strip() != body
        ]
        items.append(
            {
                "text": body,
                "about": " ".join(str(about or "").split())[:20],
                "strength": 0.7,
                "since": now,
                "at": now,
                "until": now + max_days * 86400.0 if max_days > 0 else 0.0,
            }
        )
        limit = max(1, int(getattr(config, "heart_knot_max", 2) or 2))
        # 超了先扔最旧的（新的那件说明她现在更在意）
        state.heart_knots = items[-limit:]

    def _person_places(self, state: WorldState, user_id: str) -> str:
        """这个人最近在哪个会话跟她说过话（"找他发到哪儿"）。"""

        activity = dict(getattr(state, "session_activity", None) or {})
        hits: list[str] = []
        for session_id, info in activity.items():
            if str((info or {}).get("user_id") or "") != str(user_id):
                continue
            hits.append(self.session_label(str(session_id), state))
        return "、".join(hits[:2])

    def _note_presence(self, state: WorldState, ctx: MessageContext) -> None:
        """记下「谁在说话」（调用方负责持锁）。"""

        now = self._now()
        self._note_images(state, ctx.image_urls)
        # 「好久没来的人回来了」：要在 touch_user 覆盖 last_seen **之前**算
        self._note_greet_candidate(state, ctx, now=now)
        state.touch_user(
            ctx.user_id,
            name=ctx.user_name,
            anchor="near:bot" if ctx.is_wake else "topic_center",
            max_tracked=self.world.limits.max_active_users_tracked,
        )
        # 画像：每条消息都更新"他是谁"（昵称、最后出现、聊了多少、来过几天），
        # 第一次见到就按默认关系建档（默认陌生人）。
        self.profiles.touch(ctx.session_id, ctx.user_id, ctx.user_name, now=now)
        # 「混脸熟」：他露个面就加一点好感（不用 @ 她，也不用等她回）
        self.profiles.note_contact(ctx.session_id, ctx.user_id, now=now)
        if ctx.is_wake or ctx.is_mentioned or ctx.is_private:
            # 「聊过」单独记一份：他直接跟她说话了，才算"陪过她"——
            # 想念看的是这个时间，不是"他有没有在群里露过面"
            self.profiles.note_talked(ctx.session_id, ctx.user_id, now=now)
        self._note_session_activity(state, ctx)
        # 旁观这条钩子也会收到每一句话：指令（``/vw …``）在这儿同样得挡住，
        # 不然它会从这条路溜进她的聊天记录——她就会一本正经地回一句"你怎么又敲这指令"。
        if self._passes_safety(ctx.text) and not self.is_plugin_command(ctx.text):
            state.note_chat(
                user_id=ctx.user_id,
                name=ctx.user_name,
                text=ctx.text,
                now=self._now(),
                keep=self.chat_history_limit(),
                images=ctx.chat_images,
                origin=ctx.session_id,
                at=ctx.at_targets,
                reply_to=ctx.reply_to,
                addressing=ctx.addressing,
            )
            self._tag_chat_origin(state, ctx.session_id)
        state.last_user_activity_at = self._now()
        if ctx.is_wake:
            # 有人 @她，就算一次有效互动
            self.engagement.on_user_replied(state)

    def _note_images(self, state: WorldState, urls: list[str] | None) -> None:
        """记住自上次回复以来收到的图片。

        真正交给主模型看的张数由 ``context.image_max`` 决定（超出上限的旧图会被转成文字），
        但这里要多留几张：不留就没有"超出的那几张"可以转述，只能白白丢掉。
        """

        if not urls:
            return
        limit = max(1, int(self.world.context.image_max), PENDING_IMAGE_KEEP)
        seen = {str(item.get("url") or "") for item in state.pending_images}
        for url in urls:
            text = str(url or "").strip()
            if not text or text in seen:
                continue
            state.pending_images.append({"url": text, "at": self._now()})
            seen.add(text)
        if len(state.pending_images) > limit:
            state.pending_images = state.pending_images[-limit:]

    async def take_pending_images(self, session_id: str) -> list[str]:
        """取走并清空「自上次回复以来收到的图片」。"""

        if not self.is_enabled(session_id):
            return []
        async with self.session_state(session_id) as state:
            urls = [
                str(item.get("url") or "")
                for item in state.pending_images
                if str(item.get("url") or "")
            ]
            state.pending_images = []
            return urls

    def uncaptioned_images(self, urls: list[str]) -> list[str]:
        """这几张里哪些还没转述过（同一张图不再转述第二遍）。"""

        return [
            str(url)
            for url in urls
            if str(url).strip() and str(url) not in self._image_captioned
        ]

    def mark_images_captioned(self, urls: list[str]) -> None:
        """记下"这几张已经转述过了"。"""

        now = time.time()
        for url in urls:
            key = str(url or "").strip()
            if key:
                self._image_captioned[key] = now
        if len(self._image_captioned) > 400:
            # 只留最近的一批：早期那几张早就掉出聊天窗口了
            keep = sorted(self._image_captioned.items(), key=lambda item: item[1])[-200:]
            self._image_captioned = dict(keep)

    async def attach_image_captions(
        self, session_id: str, captions: dict[str, str]
    ) -> int:
        """把转述结果挂回"当初发了这张图的那条聊天记录"上，返回挂上了几条。

        给"没随这次请求交给主模型的旧图"用：图片本身留在留档里，
        但下一轮（以及更晚的压缩）只能读到文字，所以描述要落在它自己那一行上，
        而不是蹭在最新那条消息的注解里。
        """

        if not captions or not self.is_enabled(session_id):
            return 0
        attached = 0
        async with self.session_state(session_id) as state:
            for item in state.recent_chat:
                refs = [str(ref) for ref in (item.get("images") or []) if str(ref)]
                hits = [ref for ref in refs if ref in captions]
                if not hits:
                    continue
                texts = []
                for ref in hits:
                    text = str(captions.pop(ref) or "").strip()
                    if text:
                        texts.append(text)
                        attached += 1
                if not texts:
                    continue
                body = str(item.get("text") or "").rstrip()
                item["text"] = f"{body}（图片：{'；'.join(texts)}）".strip()
        return attached

    async def note_vision(
        self, session_id: str, *, ok: bool, images: int, detail: str
    ) -> None:
        """记一条图片相关的事件：转述成功（写了什么）、失败（为什么）、或者直接交给主模型。"""

        if not self.is_enabled(session_id):
            return
        try:
            async with self.session_state(session_id) as state:
                echo = TickOutcome(session_id=session_id)
                await self._log_event(
                    state,
                    "vision",
                    {"ok": bool(ok), "images": int(images or 0), "detail": _clip_text(detail, 200)},
                    outcome=echo,
                )
                self._queue_echo(session_id, echo.debug_messages)
        except Exception as exc:  # 记日志失败不该影响回复
            self._log("debug", f"图片事件写入失败: {exc}")

    # ================= 工具 =================

    def available_tools(self) -> dict[str, str]:
        """工具名 -> 描述（描述里会带上工具自带的参数说明，供提示词直接使用）。"""

        if self.tools is None:
            return {}
        try:
            result: dict[str, str] = {}
            for info in self.tools.list_tools():
                description = (info.description or "").strip()
                param_text = render_param_text(info.parameters or {})
                if param_text and param_text != "（这个工具不需要参数）":
                    description = f"{description}｜参数：{param_text}" if description else f"参数：{param_text}"
                result[info.name] = description
            return result
        except Exception:
            return {}

    def tool_schemas(self) -> dict[str, dict[str, Any]]:
        """工具名 -> 工具自带的参数 schema（来自 AstrBot 工具定义，不需要用户手写）。"""

        if self.tools is None:
            return {}
        try:
            return {
                info.name: dict(info.parameters or {}) for info in self.tools.list_tools()
            }
        except Exception:
            return {}

    def tool_sources(self) -> dict[str, str]:
        """工具名 -> 来源（``official`` = AstrBot 内置，``plugin`` = 插件 / MCP）。"""

        if self.tools is None:
            return {}
        try:
            return {
                info.name: (info.source or "plugin")
                for info in self.tools.list_tools()
            }
        except Exception:
            return {}

    def tool_owners(self) -> dict[str, str]:
        """工具名 -> 提供它的插件名（拿不到就空串，编辑器按空串归到「插件 · 其它」）。"""

        if self.tools is None:
            return {}
        try:
            return {
                info.name: str(getattr(info, "plugin", "") or "")
                for info in self.tools.list_tools()
            }
        except Exception:
            return {}

    def tool_param_text(self, tool_name: str) -> str:
        """把某个工具的参数渲染成一行中文说明，用于提示词与 Web 编辑器。"""

        schema = self.tool_schemas().get(tool_name) or {}
        return render_param_text(schema)

    def missing_tool_params(
        self,
        action_id: str,
        params: dict[str, Any],
        *,
        node_id: str = "",
        tool_name: str = "",
    ) -> list[str]:
        """检查工具必填参数是否缺失（参数定义来自工具自身 schema）。"""

        chosen = self.resolve_tool(action_id, node_id, tool_name)
        if not chosen:
            return []
        return self.missing_params_for(chosen, params)

    def missing_params_for(self, chosen: str, params: dict[str, Any]) -> list[str]:
        """某个已确定的工具还缺哪些必填参数。"""

        schema = normalize_param_schema(self.tool_schemas().get(chosen) or {})
        required = [str(name) for name in (schema.get("required") or [])]
        given = {str(key) for key, value in (params or {}).items() if value not in ("", None)}
        return [name for name in required if name not in given]

    def declared_params(self, chosen: str) -> list[str]:
        """工具自己声明了哪些参数（不分必填可选）。"""

        schema = normalize_param_schema(self.tool_schemas().get(chosen) or {})
        return [str(name) for name in (schema.get("properties") or {})]

    def unfilled_params(self, chosen: str, params: dict[str, Any]) -> list[str]:
        """声明了但这次还没给的参数。

        工具作者经常把参数写成"可选"，实现里却必须要（例如天气工具的 city），
        所以只要工具声明了参数、而这次一个都没给全，就值得让辅助模型试一次。
        """

        given = {str(key) for key, value in (params or {}).items() if value not in ("", None)}
        return [name for name in self.declared_params(chosen) if name not in given]

    def allowed_tool_names(self, node_id: str) -> set[str]:
        return allowed_tools(self.world, self.node(node_id))

    # ================= 时钟 / 日程 =================

    def _now(self) -> float:
        if self.clock is not None:
            return float(self.clock.now())
        return time.time()

    def _resolve_tz(self):
        name = (self.world.timezone or "").strip()
        if not name:
            return None
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            return None

    def local_now(self) -> datetime:
        if self.clock is not None and hasattr(self.clock, "now_struct"):
            struct = self.clock.now_struct()
            if isinstance(struct, datetime):
                return struct
        tz = self._resolve_tz()
        if tz is None:
            return datetime.now()
        try:
            return datetime.now(tz)
        except Exception:
            return datetime.now()

    # 日程检查游标最多往前回看多久（现实秒）。用于重启 / 刚启用时不要把很久以前
    # 的日程翻出来补跑。
    SCHEDULE_LOOKBACK_SECONDS = 90
    SCHEDULE_MAX_GAP_SECONDS = 600

    max_followup_chars = 800
    """动作结果交给大模型续说前的截断长度（base64 图片地址不能整条塞进提示词）。"""

    def _clip_followup(self, text: Any) -> str:
        """续说前把结果截断：超长内容（例如 base64 图片）不能整条进提示词。"""

        body = str(text or "").strip()
        if len(body) <= self.max_followup_chars:
            return body
        return body[: self.max_followup_chars] + "…（内容过长已截断）"

    def _schedule_moment(self, now: datetime, schedule: Any) -> datetime | None:
        """这条日程「今天的触发时刻」；时间写得不对就返回 None。"""

        try:
            hour, minute = (int(part) for part in str(schedule.time).split(":")[:2])
        except (TypeError, ValueError):
            return None
        try:
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None

    def _schedule_cursor(self, state: WorldState, now: datetime) -> float:
        """这个会话上次检查到哪一刻（现实时间戳）。"""

        seconds = now.timestamp()
        cursor = float(getattr(state, "schedule_cursor", 0.0) or 0.0)
        if cursor <= 0 or cursor > seconds:
            return seconds - self.SCHEDULE_LOOKBACK_SECONDS
        if seconds - cursor > self.SCHEDULE_MAX_GAP_SECONDS:
            # 停了很久（重启、插件没跑）就别把这段时间里的日程全补一遍
            return seconds - self.SCHEDULE_LOOKBACK_SECONDS
        return cursor

    def _schedule_reason(self, state: WorldState, conditions: Any) -> str:
        """日程到点却没跑的原因（写进日志，省得用户猜）。"""

        if conditions is None:
            return ""
        if conditions.not_state and state.state in conditions.not_state:
            return f"她现在是「{state.state}」状态"
        if conditions.state and state.state not in conditions.state:
            return f"她现在是「{state.state}」状态，不在允许的列表里"
        if conditions.min_energy is not None and state.energy < conditions.min_energy:
            return f"精力 {state.energy:.2f} 低于要求的 {conditions.min_energy}"
        if conditions.max_energy is not None and state.energy > conditions.max_energy:
            return f"精力 {state.energy:.2f} 高于允许的 {conditions.max_energy}"
        if conditions.min_loneliness is not None and state.loneliness < conditions.min_loneliness:
            return f"孤独感 {state.loneliness:.2f} 低于要求的 {conditions.min_loneliness}"
        if conditions.node_in and state.node_id not in conditions.node_in:
            return f"她不在指定地点（现在在 {state.node_id}）"
        return "条件不满足"

    def node_label(self, node_id: Any) -> str:
        """地点的中文名（拿不到就原样返回 id）。"""

        key = str(node_id or "")
        node = self.node(key)
        return (node.name or node.id) if node is not None else key

    def _chain_labels(self, chain: list[Any] | None) -> list[str]:
        """动作链里每一步的中文名（日程日志用）。"""

        action_map = self.world.action_map()
        labels: list[str] = []
        for step in chain or []:
            action_id = str(getattr(step, "type", "") or "")
            definition = action_map.get(action_id)
            labels.append(
                (definition.name or definition.id) if definition else action_id
            )
        return [label for label in labels if label]

    def fallback_intent(self, definition: ActionDef, step: Any = None) -> str:
        """日程里的工具 / 指令步骤没写意图时的兜底说明。

        动作链本来就没有"想干什么"这一栏，缺了它工具型动作会直接跳过
        （"没有给出想做什么"）。这里用动作自己的说明拼一句，让它至少能跑起来；
        用户可以在日程里写清意图把它顶掉。
        """

        label = definition.name or definition.id
        parts = [f"做「{label}」这件事"]
        description = str(definition.description or "").strip()
        if description:
            parts.append(description)
        node_id = str(getattr(step, "target_node", "") or "")
        if node_id:
            parts.append(f"地点是{self.node_label(node_id)}")
        return "：".join([parts[0], "；".join(parts[1:])]) if len(parts) > 1 else parts[0]

    async def fill_schedule_intents(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """保存日程时，给缺意图的工具 / 指令步骤补一句「想干什么」。

      用内容生成模型（`creator_provider_id`，没配就复用主模型）写，写进日程 JSON 里，
        编辑器里能看到也能改。这样运行时不用再花钱、也不会因为缺意图被跳过。
        返回 ``(处理后的配置, 说明列表)``；模型不可用时原样返回。
        """

        if self.creator_llm is None or not isinstance(payload, dict):
            return payload, []
        action_map = self.world.action_map()
        filled: list[str] = []
        for schedule in payload.get("schedules") or []:
            if not isinstance(schedule, dict) or schedule.get("smart"):
                # 智能日程到点自己排计划，不需要每步的意图
                continue
            chain = schedule.get("action_chain") or []
            for index, step in enumerate(chain, start=1):
                if not isinstance(step, dict):
                    continue
                definition = action_map.get(str(step.get("type") or ""))
                if definition is None or definition.llm_level not in ("tool", "command"):
                    continue
                if str(step.get("intent") or "").strip():
                    continue
                intent = await self._generate_intent(definition, schedule)
                if not intent:
                    continue
                step["intent"] = intent
                label = definition.name or definition.id
                filled.append(f"{schedule.get('id') or '日程'} 第 {index} 步「{label}」：{intent}")
        return payload, filled

    async def _generate_intent(self, definition: ActionDef, schedule: Any) -> str:
        """让内容生成模型给一步工具 / 指令动作写一句意图。"""

        label = definition.name or definition.id
        schedule_id = str(schedule.get("id") or "") if isinstance(schedule, dict) else ""
        detail = [f"日程 id：{schedule_id or '（未命名）'}"]
        detail.append(f"这一步的动作：{label}（{definition.id}）")
        detail.append(f"动作说明：{definition.description or '（没写）'}")
        if definition.llm_level == "command":
            detail.append(f"要触发的指令：{definition.trigger_command or '（没填）'}")
        else:
            tools = "、".join(definition.tool_list())
            detail.append(f"要调用的工具：{tools or '（没选）'}")
        system_prompt = (
            "你在帮一个角色把日程里的一步说清楚：她到点要让某个工具或指令替她做什么。"
            "只输出一句中文，不要解释、不要引号、不要 Markdown。"
        )
        prompt = (
            "\n".join(detail)
            + "\n\n按这个动作的用途写一句「这一步想干什么」，"
            "它会交给模型去填工具 / 指令的参数（例如「看看今天有什么科技新闻」）。"
            "只说这一句，别写参数名。"
        )
        reply = await self._ask_creator(self._generator_session_id(), system_prompt, prompt)
        return " ".join(str(reply or "").split())[:80]

    def _steps_needing_intent(self, chain: list[Any] | None) -> list[tuple[int, ActionDef]]:
        """动作链里哪几步需要意图（工具型 / 指令型，且还没写）。1-based 下标。"""

        action_map = self.world.action_map()
        wanted: list[tuple[int, ActionDef]] = []
        for index, step in enumerate(chain or [], start=1):
            definition = action_map.get(str(getattr(step, "type", "") or ""))
            if definition is None or definition.llm_level not in ("tool", "command"):
                continue
            if str(getattr(step, "intent", "") or "").strip():
                continue
            wanted.append((index, definition))
        return wanted

    async def _smart_chain(
        self, state: WorldState, schedule: Any
    ) -> tuple[list[Any], str]:
        """智能日程：把动作链交给大模型，让它给几步补「想干什么」。

        **动作链本身一个字都不改**（步数、动作 id、时长、台词都照旧），模型只填意图——
        这样就不会出现"配的是查热搜新闻、它却去上网搜索"这种事，也不用它操心怎么走路
        （自动前往由插件补）。
        返回 ``(处理后的动作链, 说明文字)``。
        """

        chain = list(schedule.action_chain or [])
        wanted = self._steps_needing_intent(chain)
        if self.llm is None or not wanted:
            return chain, ""
        now = self.local_now()
        steps = [
            {
                "index": index,
                "label": definition.name or definition.id,
                "action_id": definition.id,
                "kind": "指令型" if definition.llm_level == "command" else "工具型",
                "description": str(definition.description or "").strip(),
            }
            for index, definition in wanted
        ]
        system_prompt, prompt = self.prompts.build_schedule_intent_prompt(
            schedule_id=str(schedule.id),
            when=f"{now.strftime('%Y-%m-%d %H:%M')}（{period_of(now.hour)}）",
            where=self.node_label(state.node_id),
            state_hint=state.mood or "",
            chat_note=self.chat_note_for(state),
            persona=_clip_text(await self._persona_text(state.session_id), 800),
            steps=steps,
            # 「这条日程是干什么的」+ 完整动作链：只给"这一步是什么动作"，
            # 补出来的意图容易跟日程本来的目的对不上
            schedule_note=str(getattr(schedule, "note", "") or ""),
            chain_labels=self._chain_labels(chain),
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        self._count_llm_plan(state)
        if reply is None:
            return chain, ""
        intents = self._parse_intents(reply, {index for index, _ in wanted}, len(chain))
        if not intents:
            return chain, ""
        filled: list[Any] = []
        for index, step in enumerate(chain, start=1):
            intent = intents.get(index)
            if intent:
                filled.append(step.model_copy(update={"intent": intent}))
            else:
                filled.append(step)
        action_map = self.world.action_map()
        parts: list[str] = []
        for index in sorted(intents):
            action_id = str(getattr(chain[index - 1], "type", "") or "")
            definition = action_map.get(action_id)
            label = (definition.name or definition.id) if definition else action_id
            parts.append(f"第 {index} 步「{label}」：{intents[index]}")
        return filled, "；".join(parts)

    @staticmethod
    def _parse_intents(reply: str, allowed: set[int], total: int) -> dict[int, str]:
        """从模型返回里挑出「第几步 → 意图」，写得不对的一律丢掉。"""

        payload = extract_json_object(reply)
        if not isinstance(payload, dict):
            return {}
        raw = payload.get("intents")
        pairs: list[tuple[Any, Any]] = []
        if isinstance(raw, dict):
            pairs = list(raw.items())
        elif isinstance(raw, list):
            for position, item in enumerate(raw, start=1):
                if isinstance(item, dict):
                    pairs.append(
                        (item.get("step", item.get("index", position)), item.get("intent"))
                    )
                else:
                    pairs.append((position, item))
        result: dict[int, str] = {}
        for key, value in pairs:
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            text = " ".join(str(value or "").split())
            if not text or index not in allowed or not (1 <= index <= total):
                continue
            result[index] = text[:80]
        return result

    # ---------------- 日程闸门：日程撞上事件时怎么办 ----------------
    #
    # 只在"这条日程是睡觉 / 小睡 / 换地方"而且她手上确实有件没完的事件时才介入，
    # 所以绝大多数日程照常执行、一次模型都不调。

    GATE_HEAVY_ACTIONS = ("sleep", "nap")
    GATE_MOVING_ACTIONS = ("walk_to",)

    def _schedule_gate_kind(self, schedule: Any) -> str:
        """这条日程要不要过闸门：``heavy``（睡觉类）/ ``moving``（换地方）/ 空串。"""

        kinds = {str(getattr(step, "type", "") or "") for step in list(schedule.action_chain or [])}
        if kinds & set(self.GATE_HEAVY_ACTIONS):
            return "heavy"
        if kinds & set(self.GATE_MOVING_ACTIONS):
            return "moving"
        return ""

    def _schedule_delay_record(self, state: WorldState, schedule_id: str) -> dict[str, Any]:
        record = (state.schedule_delays or {}).get(str(schedule_id))
        return dict(record) if isinstance(record, dict) else {}

    def _schedule_delay_due(
        self,
        state: WorldState,
        schedule: Any,
        day_key: str,
        cursor: float,
        seconds: float,
    ) -> bool:
        """这条日程是不是"推迟到点了"，该再检查一次。"""

        record = self._schedule_delay_record(state, schedule.id)
        if not record or str(record.get("day") or "") != day_key:
            return False
        try:
            until = float(record.get("until") or 0.0)
        except (TypeError, ValueError):
            return False
        return cursor < until <= seconds

    def _delay_slot(self, state: WorldState, schedule: Any) -> str:
        return str(self._schedule_delay_record(state, schedule.id).get("slot") or "")

    def _delay_exhausted(self, state: WorldState, schedule: Any, day_key: str) -> bool:
        """推迟到顶了没有：次数或总时长任一超了就得执行。"""

        record = self._schedule_delay_record(state, schedule.id)
        if str(record.get("day") or "") != day_key:
            return False
        config = self.world.events
        times = max(0, int(getattr(config, "schedule_delay_limit_times", 2) or 0))
        minutes = max(0, int(getattr(config, "schedule_delay_limit_minutes", 180) or 0))
        if times and int(record.get("count") or 0) >= times:
            return True
        if minutes and float(record.get("minutes") or 0.0) >= minutes:
            return True
        return False

    def _delay_schedule(
        self, state: WorldState, schedule: Any, day_key: str, slot: str
    ) -> None:
        """记一次推迟：隔 ``schedule_delay_minutes`` 再检查；睡觉类要付熬夜代价。"""

        config = self.world.events
        minutes = max(5, int(getattr(config, "schedule_delay_minutes", 30) or 30))
        record = self._schedule_delay_record(state, schedule.id)
        if str(record.get("day") or "") != day_key:
            record = {"day": day_key, "count": 0, "minutes": 0.0}
        record["count"] = int(record.get("count") or 0) + 1
        record["minutes"] = float(record.get("minutes") or 0.0) + minutes
        record["until"] = self.local_now().timestamp() + minutes * 60
        record["slot"] = str(slot or record.get("slot") or "")
        state.schedule_delays = {**(state.schedule_delays or {}), str(schedule.id): record}
        if self._schedule_gate_kind(schedule) == "heavy":
            # 熬夜代价：这段时间精力掉得更快
            ticks = max(1, int(minutes * 60 / max(1.0, self.tick_seconds)))
            state.stay_up_until = max(int(state.stay_up_until or 0), state.world_time + ticks)

    def _cancel_schedule(self, state: WorldState, schedule: Any, day_key: str) -> None:
        """今天这条不跑了：记一笔，明天照常。"""

        record = self._schedule_delay_record(state, schedule.id)
        record.update({"day": day_key, "until": 0.0, "cancelled": True})
        state.schedule_delays = {**(state.schedule_delays or {}), str(schedule.id): record}

    async def _schedule_gate(
        self, state: WorldState, schedule: Any, *, label: str
    ) -> tuple[str, str]:
        """日程到点时的闸门：返回 ``("do"|"delay"|"cancel", 说明)``。"""

        config = self.world.events
        if not bool(getattr(config, "schedule_gate", True)):
            return "do", ""
        kind = self._schedule_gate_kind(schedule)
        if not kind:
            return "do", ""  # 伸懒腰、吃饭、看书这类照常做
        thread = self._active_thread(state)
        if thread is None:
            return "do", ""
        day_key = self.local_now().strftime("%Y-%m-%d")
        if self._delay_exhausted(state, schedule, day_key):
            return "do", "推迟到上限了，按日程执行"
        if self._thread_critical(thread):
            # 危险事件：不问模型，直接让路（"被跟踪时不能回卧室睡觉"）
            return "delay", "她手上这件危险的事还没完，先让路"
        choice, reason = await self._ask_schedule_choice(state, schedule, label, thread)
        if choice == "do":
            return "do", ""
        if choice == "cancel":
            return "cancel", f"她自己决定今天不做了（{reason}）"
        return "delay", f"她自己决定先处理手上的事（{reason}）"

    async def _ask_schedule_choice(
        self, state: WorldState, schedule: Any, label: str, thread: dict[str, Any]
    ) -> tuple[str, str]:
        """问主模型：照做 / 推迟 / 今天算了。没有模型时按类型兜底。"""

        kind = self._schedule_gate_kind(schedule)
        fallback = ("delay", "先处理手上的事") if kind == "heavy" else ("do", "")
        if self.llm is None:
            return fallback
        event_line = (
            f"{thread.get('title') or '一件事'}"
            + (f"（第 {len(list(thread.get('steps') or [])) + 1} 轮）" if thread.get("steps") else "")
        )
        system, prompt = self.prompts.build_schedule_gate_prompt(
            persona_text=await self._persona_text(state.session_id),
            schedule_text=label,
            event_line=event_line,
            state_line=self._state_persona_line(state, self.node(state.node_id)),
        )
        raw = await self._ask_llm(state.session_id, system, prompt)
        payload = extract_json_object(raw or "") or {}
        choice = str(payload.get("choice") or "").strip().lower()
        if choice not in ("do", "delay", "cancel"):
            return fallback
        reason = " ".join(str(payload.get("reason") or "").split())[:60]
        return choice, reason

    async def run_schedules(self) -> list[TickOutcome]:
        """检查并触发到点的日程。

        判定不是「当前这一分钟刚好等于配置时间」，而是「游标之后、现在之前」
        这段时间里到点的都算——tick 被拖慢、整分钟被跳过时也能补上。
        """

        outcomes: list[TickOutcome] = []
        if self.schedules is None:
            return outcomes
        now = self.local_now()
        seconds = now.timestamp()
        day_key = now.strftime("%Y-%m-%d")
        self._prune_once_schedules(day_key)
        weekday = self.WEEKDAY_KEYS[now.weekday()]
        # 一次性日程只认它自己那一天：日期过了/还没到的都不参与今天的判定，
        # 星期几也不再看（「明天下午三点」这种说法不该被星期表挡住）。
        candidates = [
            schedule
            for schedule in self.schedules.schedules
            if schedule.enabled
            and not (schedule.once and schedule.date and schedule.date != day_key)
            and (
                bool(schedule.once)
                or not schedule.days
                or weekday in schedule.days
            )
        ]

        # 她的一天只过一遍：同一个她（会话组）只检查一次日程
        for session_id in self.tick_session_ids():
            async with self.session_state(session_id) as state:
                if self._schedule_reset_pending:
                    cursor = seconds - self.SCHEDULE_LOOKBACK_SECONDS
                else:
                    cursor = self._schedule_cursor(state, now)
                state.schedule_cursor = seconds
                if not candidates:
                    continue
                due: list[tuple[Any, str]] = []
                for schedule in candidates:
                    moment = self._schedule_moment(now, schedule)
                    if moment is None:
                        continue
                    stamp = moment.timestamp()
                    if cursor < stamp <= seconds:
                        due.append((schedule, moment.strftime("%H:%M")))
                    elif self._schedule_delay_due(state, schedule, day_key, cursor, seconds):
                        # 之前被事件推迟的那条：到重试时间了，再走一遍闸门
                        due.append((schedule, str(self._delay_slot(state, schedule) or "")))
                for schedule, slot in sorted(
                    due, key=lambda item: item[0].priority, reverse=True
                ):
                    if not self.schedule_in_scope(schedule, session_id):
                        # 这条日程是别的会话 / 别的组的事
                        continue
                    target = self.schedule_target(schedule, session_id)
                    label = schedule.id
                    if schedule.action_chain:
                        names = [
                            (self.world.action_map()[step.type].name or step.type)
                            if step.type in self.world.action_map()
                            else step.type
                            for step in schedule.action_chain
                        ]
                        if names:
                            label = f"{schedule.id}（{' → '.join(names)}）"
                    if not self._conditions_ok(state, schedule.conditions):
                        reason = self._schedule_reason(state, schedule.conditions) or "条件不满足"
                        await self._log_event(
                            state,
                            "skip",
                            {
                                "action": f"schedule:{schedule.id}",
                                "note": f"日程「{label}」到点了却没跑：{reason}",
                            },
                        )
                        continue
                    echo_marker = await self._event_marker(state)
                    claimed = await self.db.call(
                        "mark_schedule_fired",
                        session_id=session_id,
                        schedule_id=schedule.id,
                        day_key=day_key,
                        slot=slot,
                    )
                    if not claimed:
                        continue
                    # 记下"这条日程上次什么时候真的跑了"：提示词里要写给她看
                    last_map = dict(getattr(state, "schedule_last_fired", None) or {})
                    last_map[str(schedule.id)] = float(self._now())
                    state.schedule_last_fired = last_map
                    # 日程闸门：她正忙着处理一件事件时，睡觉/小睡/换地方要先问一句
                    choice, gate_note = await self._schedule_gate(
                        state, schedule, label=label
                    )
                    if choice != "do":
                        await self._log_event(
                            state,
                            "skip",
                            {
                                "action": f"schedule:{schedule.id}",
                                "note": (
                                    f"日程「{label}」{gate_note}"
                                    if gate_note
                                    else f"日程「{label}」被事件挤开了"
                                ),
                            },
                        )
                        if choice == "delay":
                            self._delay_schedule(state, schedule, day_key, slot)
                        else:  # cancel：今天这条就算了
                            self._cancel_schedule(state, schedule, day_key)
                        continue
                    # 她的日程落在哪个会话里说（勾了私聊就发私聊）
                    outcome = TickOutcome(session_id=target)
                    # 这条日程发生在哪儿：调试回显要跟着它走（不然私聊的日程把回显发进群）
                    outcome.place = target
                    chain = schedule.action_chain
                    smart_note = ""
                    if schedule.smart:
                        # 智能日程：让大模型给缺意图的步骤补一句（动作链本身不动）
                        chain, smart_note = await self._smart_chain(state, schedule)
                    if schedule.auto_travel:
                        chain = self.expand_chain_for_travel(state, chain)
                    # 「日程开始执行」要能进日志与调试输出：不然只能看到它的动作，
                    # 不知道这一串是谁安排的。
                    await self._log_event(
                        state,
                        "schedule",
                        {
                            "id": schedule.id,
                            "time": slot,
                            "actions": " → ".join(self._chain_labels(chain)),
                            "manual": False,
                            "smart": bool(schedule.smart),
                            "once": bool(schedule.once),
                            "note": str(schedule.note or ""),
                            "intents": smart_note,
                        },
                    )
                    # 「这条日程是干什么的」+ 完整动作链，一并带进这一轮的上下文：
                    # 她说话时才有背景（不然只知道自己"正在做某个动作"，
                    # 不知道为什么做、后面还要做什么）。
                    chain_labels = self._chain_labels(chain)
                    brief: list[str] = []
                    if str(schedule.note or "").strip():
                        brief.append(f"到点了：这是你之前专门排的一件事——{schedule.note}")
                    if chain_labels:
                        brief.append("这条日程要做的事（按顺序）：" + " → ".join(chain_labels))
                    if brief:
                        state.event_digest = [*list(state.event_digest or []), *brief][-6:]
                    await self._run_chain(state, chain, outcome, depth=0)
                    outcome.notes.append(f"日程 {schedule.id} 已触发")
                    # 做完了这一整串要有交代：她之后提起"我刚做了…"才有据可依
                    if chain_labels:
                        state.event_digest = [
                            *list(state.event_digest or []),
                            "按日程做完了：" + " → ".join(chain_labels),
                        ][-6:]
                    state.add_event(
                        "schedule",
                        {"id": schedule.id, "note": str(schedule.note or "")},
                    )
                    await self._echo_events_since(state, outcome, echo_marker)
                    outcomes.append(outcome)
                    if schedule.once and self._drop_schedule(schedule.id):
                        outcome.notes.append(f"一次性日程 {schedule.id} 跑完了，已删掉")
        self._schedule_reset_pending = False
        return outcomes

    async def run_schedule_now(
        self, session_id: str, schedule_id: str, *, force: bool = True
    ) -> dict[str, Any]:
        """立刻跑一遍某条日程的动作链（编辑器按钮 / `/vw schedule run` 用）。

        - 不看时间与星期，这是"手动跑一次"，**不占当天的触发名额**，
          到点之后它照常还会正常触发；
        - ``force=True``（默认）连触发条件一起忽略，方便测试；
          传 False 时条件不满足会返回原因，不会硬跑。
        """

        if not self.is_enabled(session_id):
            return {"ok": False, "reason": "这个会话不在白名单里"}
        schedule = next(
            (
                item
                for item in (self.schedules.schedules if self.schedules else [])
                if item.id == schedule_id
            ),
            None,
        )
        if schedule is None:
            return {"ok": False, "reason": f"没有找到日程「{schedule_id}」"}
        if not schedule.enabled and not force:
            return {"ok": False, "reason": f"日程「{schedule.id}」是停用状态"}

        # 手动跑也认这条日程自己的落点（勾了私聊就发私聊）
        outcome = TickOutcome(session_id=self.schedule_target(schedule, session_id))
        outcome.place = outcome.session_id
        async with self.session_state(session_id) as state:
            if not force and not self._conditions_ok(state, schedule.conditions):
                reason = self._schedule_reason(state, schedule.conditions) or "条件不满足"
                return {
                    "ok": False,
                    "reason": f"触发条件不满足：{reason}",
                    "schedule_id": schedule.id,
                }
            echo_marker = await self._event_marker(state)
            chain = schedule.action_chain
            smart_note = ""
            if schedule.smart:
                # 智能日程手动跑也一样：让大模型给缺意图的步骤补一句
                chain, smart_note = await self._smart_chain(state, schedule)
            if schedule.auto_travel:
                chain = self.expand_chain_for_travel(state, chain)
            # 手动跑一次也把"这条日程是什么 + 动作链"记进她自己的账
            manual_labels = self._chain_labels(chain)
            manual_brief: list[str] = []
            if str(schedule.note or "").strip():
                manual_brief.append(f"你刚跑了一遍日程：{schedule.note}")
            if manual_labels:
                manual_brief.append("这条日程要做的事（按顺序）：" + " → ".join(manual_labels))
            if manual_brief:
                state.event_digest = [*list(state.event_digest or []), *manual_brief][-6:]
            await self._run_chain(state, chain, outcome, depth=0)
            # 手动跑一次也算"上次执行"：编辑器里试跑之后，提示词里就能看到时间
            last_map = dict(getattr(state, "schedule_last_fired", None) or {})
            last_map[str(schedule.id)] = float(self._now())
            state.schedule_last_fired = last_map
            state.add_event(
                "schedule",
                {"id": schedule.id, "manual": True, "note": str(schedule.note or "")},
            )
            await self._log_event(
                state,
                "schedule",
                {
                    "id": schedule.id,
                    "time": schedule.time,
                    "actions": " → ".join(self._chain_labels(chain)),
                    "manual": True,
                    "smart": bool(schedule.smart),
                    "intents": smart_note,
                },
            )
            await self._echo_events_since(state, outcome, echo_marker)
            running = str((state.current_action or {}).get("desc") or "")

        await self._deliver(outcome)
        notes = [str(item) for item in outcome.notes if str(item).strip()]
        if notes:
            summary = "；".join(notes)
        elif outcome.messages:
            summary = "她说：" + " / ".join(outcome.messages)
        elif running:
            summary = f"她现在在做：{running}"
        else:
            summary = "动作链跑完了"
        return {
            "ok": True,
            "schedule_id": schedule.id,
            "messages": list(outcome.messages),
            "notes": notes,
            "note": _clip_text(f"已执行「{schedule.time} {schedule.id}」：{summary}", 160),
        }

    def expand_chain_for_travel(self, state: WorldState, chain: list[Any]) -> list[Any]:
        """给日程的 auto_travel 用：把"需要先在某处"的动作前面自动插一步移动。

        例：日程动作链是 [上网搜索]，开启 auto_travel 后她在卧室时会变成
        [移动到书房, 上网搜索]，这样就不用管理员手写移动步骤。
        """

        graph = self.world.adjacent()
        tracked = state.node_id
        expanded: list[Any] = []
        for step in chain or []:
            action_id = str(getattr(step, "type", "") or "")
            definition = self.world.action_map().get(action_id)
            if action_id == "walk_to":
                target = str(
                    getattr(step, "target_node", "") or getattr(step, "target", "") or ""
                )
                if target in self.world.node_map():
                    tracked = target
                expanded.append(step)
                continue
            if definition is not None:
                wanted: list[str] = []
                if definition.scope == "node":
                    wanted = [
                        node_id
                        for node_id in definition.allowed_nodes
                        if node_id in self.world.node_map()
                    ]
                if wanted and tracked not in wanted:
                    target = nearest_node(graph, tracked, wanted)
                    if target:
                        expanded.append(ChainStep(type="walk_to", target_node=target))
                        tracked = target
            expanded.append(step)
        return expanded

    def _conditions_ok(self, state: WorldState, conditions) -> bool:
        if conditions is None:
            return True
        if conditions.not_state and state.state in conditions.not_state:
            return False
        if conditions.state and state.state not in conditions.state:
            return False
        if conditions.min_energy is not None and state.energy < conditions.min_energy:
            return False
        if conditions.max_energy is not None and state.energy > conditions.max_energy:
            return False
        if conditions.min_loneliness is not None and state.loneliness < conditions.min_loneliness:
            return False
        if conditions.node_in and state.node_id not in conditions.node_in:
            return False
        return True

    # ================= Tick =================

    # ================= 天气 =================

    async def weather_record(self) -> WeatherRecord:
        """当前那份天气（全局共享，重启后仍在）。"""

        try:
            payload = await self.db.call("kv_get", WEATHER_KEY)
        except Exception:
            return WeatherRecord()
        return WeatherRecord.from_payload(payload)

    async def weather_line(self) -> str:
        """写进提示词的那一段；关掉、没记录、或者旧到不值得提都返回空串。"""

        config = self.world.weather
        if not bool(getattr(config, "enabled", True)):
            return ""
        record = await self.weather_record()
        if record.empty:
            return ""
        return weather_prompt_block(
            record,
            self._now(),
            stale_hours=float(getattr(config, "stale_hours", 24.0) or 0.0),
            tz=self._resolve_tz(),
        )

    async def weather_payload(self) -> dict[str, Any]:
        """编辑器横幅要的数据。"""

        record = await self.weather_record()
        return weather_banner(record, self._now(), tz=self._resolve_tz())

    async def runtime_notes(self, session_id: str) -> dict[str, str]:
        """提示词里那几段"此刻的事实"：天气 + 最近查过什么。"""

        notes = {
            "weather": await self.weather_line(),
            "recent_search": await self._recent_search_line(session_id),
        }
        if not session_id:
            return notes
        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return notes
        # 能力值给的是人话；心里挂着的事按需注入（没挂着就整段不出现）
        notes["abilities_line"] = self.abilities_line(state)
        notes["pending_event"] = self.pending_event_line(state)
        # 她自己的账：正在经历 / 最近发生在我身上的事 / 还挂着的（窗口外的不出现）
        notes["event_journal"] = self.event_journal(state)
        return notes

    async def _recent_search_line(self, session_id: str) -> str:
        """最近一次检索的摘要（一小时内才写进提示词）：省得她拿同样的词再查一遍。"""

        if not session_id:
            return ""
        try:
            payload = await self.db.call("kv_get", f"{SEARCH_LOG_KEY}.{session_id}")
        except Exception:
            return ""
        if not isinstance(payload, dict):
            return ""
        try:
            at = float(payload.get("at") or 0.0)
        except (TypeError, ValueError):
            return ""
        age = self._now() - at
        if at <= 0 or age < 0 or age > SEARCH_LOG_MINUTES * 60:
            return ""
        queries = [str(item) for item in (payload.get("queries") or []) if str(item)][:3]
        if not queries:
            return ""
        minutes = max(1, int(round(age / 60)))
        found = int(payload.get("found") or 0)
        return (
            "# 最近查过\n"
            f"{minutes} 分钟前你查过「{'」「'.join(queries)}」，拿到 {found} 条材料。"
            "同一件事不要马上再查一遍；要接着查就换个更具体的角度。\n"
            "如果这批材料你还没讲给群里听过，就直接讲内容——不要再回一句"
            "「正在挑 / 马上端上来 / 等着」这类话。"
        )

    async def _remember_search(
        self, session_id: str, queries: list[str], found: int
    ) -> None:
        if not session_id or not queries:
            return
        try:
            await self.db.call(
                "kv_set",
                f"{SEARCH_LOG_KEY}.{session_id}",
                {
                    "at": float(self._now()),
                    "queries": [str(item) for item in queries[:5]],
                    "found": int(found),
                },
            )
        except Exception:
            pass

    async def weather_next_at(self) -> float:
        """下一次静默刷新大约在什么时候（0 = 不会再刷新）。"""

        config = self.world.weather
        if not bool(getattr(config, "enabled", True)):
            return 0.0
        hours = max(0.0, float(getattr(config, "refresh_hours", 2.0) or 0.0))
        if hours <= 0:
            return 0.0
        try:
            last = float(await self.db.call("kv_get", WEATHER_TRY_KEY) or 0.0)
        except Exception:
            last = 0.0
        if last <= 0:
            return self._now()
        return last + hours * 3600.0

    def _weather_channel(self, definition: ActionDef | None) -> str:
        """「查天气」这次走哪条路：``command`` 指令型 / ``tool`` 工具型 / ``""`` 两者都不是。"""

        if definition is None:
            return ""
        level = str(getattr(definition, "llm_level", "") or "")
        if level == "command":
            return "command"
        return "tool" if level == "tool" else ""

    async def maybe_refresh_weather(
        self, *, force: bool = False, session_id: str = ""
    ) -> str:
        """到点了就静默查一次天气：不说话、不发群，只更新那份记录。

        ``force=True`` 用于编辑器上的「立即刷新」：跳过倒计时，其余条件照旧。
        ``session_id`` 是编辑器里选中的那条会话：指令型动作要借它的消息当上下文。
        返回一句说明（编辑器直接弹给用户看，免得"点了没反应"）；空串表示成功。
        """

        config = self.world.weather
        if not bool(getattr(config, "enabled", True)):
            return "天气功能在全局设置里关着"
        hours = max(0.0, float(getattr(config, "refresh_hours", 2.0) or 0.0))
        definition = self.world.action_map().get("check_weather")
        channel = self._weather_channel(definition)
        if not channel:
            # 「查天气」可以是工具型，也可以是指令型；两者都没配就没法查
            return "「查天气」动作得配成工具型（选一个天气工具）或指令型（填要触发的指令）"
        if channel == "command":
            if self.commands is None:
                return "拿不到 AstrBot 的指令通道"
            if not str(getattr(definition, "trigger_command", "") or "").strip():
                return "「查天气」是指令型，但没填要触发的指令名"
        elif self.tools is None:
            return "拿不到 AstrBot 的工具通道"
        if hours <= 0 and not force:
            # 间隔 0 = 不在后台查；手动点「刷新」照样要查
            return "后台刷新间隔是 0（只在手动点的时候查）"
        now = self._now()
        try:
            last_try = float(await self.db.call("kv_get", WEATHER_TRY_KEY) or 0.0)
        except Exception:
            last_try = 0.0
        if not force and last_try > 0 and (now - last_try) < hours * 3600:
            return "还没到下一次刷新的时间"
        if channel == "tool":
            # 工具需要一条真实消息当上下文：群里还没人说过话就先不查，等下一轮
            has_context = getattr(self.tools, "has_context", None)
            if callable(has_context) and not bool(has_context()):
                return "群里还没人说过话，工具拿不到消息当上下文——先让群里说一句再试"
        sessions = self.enabled_session_ids()
        wanted = str(session_id or "").strip()
        target = wanted if wanted and wanted in sessions else ""
        if not target and channel == "command":
            # 指令要借一条真实消息当上下文：优先白名单里最近说过话的那个会话
            picker = getattr(self.commands, "session_with_context", None)
            if callable(picker):
                try:
                    target = str(picker(sessions) or "")
                except Exception:
                    target = ""
        target = target or (sessions[0] if sessions else "")
        if not target:
            return "没有启用的会话（先在会话白名单里加一个群）"
        # 先记"试过了"：失败也要等下一个周期，不要每个 tick 都去撞
        try:
            await self.db.call("kv_set", WEATHER_TRY_KEY, float(now))
        except Exception:
            pass
        record, note = await self._weather_once(target)
        if not record.empty:
            return ""
        return note or "这次没查到（日志里有原因）"

    async def refresh_weather(self, session_id: str) -> WeatherRecord:
        """查一次天气并记下来（静默：不触发续说，也不回显到群里）。"""

        record, _note = await self._weather_once(session_id)
        return record

    async def _weather_once(self, session_id: str) -> tuple[WeatherRecord, str]:
        """查一次天气：返回 ``(记录, 没查到的原因)``；查到了就写进全局天气记录。"""

        definition = self.world.action_map().get("check_weather")
        channel = self._weather_channel(definition)
        if definition is None or not channel:
            return WeatherRecord(), "「查天气」动作没配成工具型或指令型"
        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return WeatherRecord(), "读不到她在那个会话里的状态，先让她在群里说一句话"
        if channel == "command":
            # 查天气配成「指令型」时就走指令通道
            command = str(getattr(definition, "trigger_command", "") or "").strip()
            if not command:
                return WeatherRecord(), "「查天气」是指令型，但没填要触发的指令名"
            if self.commands is None:
                return WeatherRecord(), "拿不到 AstrBot 的指令通道"
            city = str(getattr(self.world.weather, "city", "") or "").strip()
            intent = f"查一下{city}现在的天气" if city else "现在外面的天气"
            line = await self._compose_command(state, definition, command, intent)
            call = await self.commands.trigger(session_id, line)
            if not call.ok:
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": definition.id,
                        "note": f"查天气的指令「{line}」没跑成：{call.error or '没有返回内容'}",
                    },
                )
                return WeatherRecord(), (
                    f"指令「{line}」没跑成：{call.error or '没有返回内容'}"
                )
            if not str(call.text or "").strip() and not list(
                getattr(call, "image_urls", None) or []
            ):
                return WeatherRecord(), f"指令「{line}」跑完了，但没返回天气内容"
            record = await self._store_weather(
                state,
                definition,
                {
                    "tool_result": call.text,
                    "tool_images": list(getattr(call, "image_urls", None) or []),
                },
                source="auto",
            )
            if record.empty:
                return record, "指令返回了内容，但没能整理成天气（日志里有原因）"
            return record, ""
        if self.tools is None:
            return WeatherRecord(), "拿不到 AstrBot 的工具通道"
        outcome = TickOutcome(session_id=session_id)
        outcome.place = session_id
        action = PlannedAction(type=definition.id, intent="现在外面的天气")
        city = str(getattr(self.world.weather, "city", "") or "").strip()
        if city:
            action.params = self._city_params(definition, state, city)
        if not await self._prepare_tool_action(state, definition, action, outcome):
            return WeatherRecord(), (
                "「查天气」选的天气工具在 AstrBot 里没注册或没选："
                "先确认工具装好了，或者把它改成指令型动作"
            )
        payload: dict[str, Any] = {
            "type": definition.id,
            "params": dict(action.params),
            "tool_params": dict(getattr(action, "tool_params", {}) or {}),
            "intent": action.intent,
            "queries": [],
        }
        # 这里刻意不传 outcome：静默刷新不该出现在群里（调试回显也不该）
        await self._run_tool_calls(state, definition, payload)
        record = await self._store_weather(state, definition, payload, source="auto")
        if record.empty:
            return record, "天气工具没返回可用的内容（日志里有原因）"
        return record, ""

    async def _store_weather(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        *,
        source: str,
    ) -> WeatherRecord:
        """把这次查到的天气写进全局记录；什么都没拿到就什么都不改。"""

        raw = str(action.get("tool_result") or "").strip()
        images = [str(item) for item in (action.get("tool_images") or []) if str(item)]
        if not raw and not images:
            return WeatherRecord()
        text = await self._normalize_weather(state, raw, images)
        if not text:
            return WeatherRecord()
        record = WeatherRecord(
            text=text,
            parts=parse_parts(text),
            at=self._now(),
            source=source,
            raw=_clip_text(raw, 2000),
            images=images,
        )
        try:
            await self.db.call("kv_set", WEATHER_KEY, record.to_payload())
        except Exception as exc:
            self._log("warning", f"天气记录写不进去：{exc}")
            return WeatherRecord()
        # 静默刷新也算"刚查过"：下一次静默刷新从头计时
        try:
            await self.db.call("kv_set", WEATHER_TRY_KEY, float(self._now()))
        except Exception:
            pass
        await self._log_event(
            state,
            "weather",
            {
                "action": definition.id,
                "source": source,
                "text": record.line(),
                "images": len(images),
                "raw": _clip_text(raw, 300),
            },
        )
        return record

    async def _normalize_weather(
        self, state: WorldState, raw: str, images: list[str]
    ) -> str:
        """把天气结果压成「城市｜温度｜天气｜湿度｜风力｜预报」。

        返回图片先交给多模态模型读出来，再把文字交给小模型归一化；
        哪一步没配、或者读不出来，就退回原文，绝不编。
        """

        text = str(raw or "").strip()
        if images and self.describer is not None:
            described = ""
            try:
                described = await self.describer.describe_to_text(
                    images, DEFAULT_WEATHER_PROMPT
                )
            except Exception as exc:
                self._log("debug", f"天气图读不出来：{exc}")
            if described:
                text = f"{described}\n{text}".strip()
        if not text:
            return ""
        if parse_parts(text):
            return " ".join(text.split())[:200]
        if not bool(getattr(self.world.weather, "normalize", True)):
            return text
        if self.helper_llm is None or not self._tool_param_allowed(state):
            return text
        reply = await self._ask_helper(
            state.session_id,
            DEFAULT_WEATHER_PROMPT,
            f"天气内容：\n{_clip_text(text, 1200)}",
        )
        self._count_tool_param(state)
        cleaned = " ".join(str(reply or "").split())
        if not cleaned or "读不出" in cleaned:
            return "" if images else text
        # 模型没按格式给（拆不出字段）时保留原文：宁可她讲得啰嗦，也别丢信息
        return cleaned[:200] if parse_parts(cleaned) else text

    def _city_params(
        self, definition: ActionDef, state: WorldState, city: str
    ) -> dict[str, Any]:
        """把「全局设置里的城市」填进天气工具的城市参数（认得出参数名才填）。"""

        names = self.resolve_tools(definition, state.node_id)
        if not names:
            return {}
        key = self._city_param_name(names[0])
        return {key: city} if key else {}

    def _city_param_name(self, tool: str) -> str:
        """天气工具里"城市"该填哪个参数。"""

        schema = normalize_param_schema(self.tool_schemas().get(tool) or {})
        properties = [str(name) for name in (schema.get("properties") or {})]
        required = [str(name) for name in (schema.get("required") or [])]
        exact = {
            "city",
            "location",
            "place",
            "region",
            "area",
            "district",
            "城市",
            "地点",
        }
        for pool in (required, properties):
            for name in pool:
                low = name.lower()
                if low in exact or "city" in low or "location" in low:
                    return name
        return required[0] if required else ""

    async def tick(self) -> list[TickOutcome]:
        """推进所有启用会话的世界时钟。返回需要发送的消息。"""

        self._last_tick_at = self._now()
        try:
            # 天气是全局的：整轮只查一次，放在会话循环之前，
            # 这样它写的事件也已经越过各会话的回显游标（静默刷新不快发到群里）
            await self.maybe_refresh_weather()
        except Exception as exc:
            self._log("debug", f"静默刷新天气失败：{exc}")
        outcomes: list[TickOutcome] = []
        # 同一个她只推一次：组里的几个群 / 私聊共用一份状态，
        # 按会话各推一遍等于她的时间、精力、事件频率乘以会话数。
        for session_id in self.tick_session_ids():
            try:
                outcome = await self._tick_session(session_id)
            except Exception as exc:  # 单个会话出错不影响其他会话
                self._log("warning", f"tick 失败 session={session_id}: {exc}")
                continue
            if outcome is not None:
                outcomes.append(outcome)
        try:
            outcomes.extend(await self.run_schedules())
        except Exception as exc:
            self._log("warning", f"日程检查失败: {exc}")
        # 对话记忆：把攒着的片段总结成一条（放在这里是为了不占着会话锁调模型）
        for session_id in self.tick_session_ids():
            try:
                flushed = await self.flush_pending_memory(session_id)
            except Exception as exc:
                self._log("warning", f"总结对话记忆失败 session={session_id}: {exc}")
                continue
            if flushed is not None:
                outcomes.append(flushed)
        for outcome in outcomes:
            await self._deliver(outcome)
        return outcomes

    async def _tick_session(self, session_id: str) -> TickOutcome | None:
        outcome = TickOutcome(session_id=session_id)
        # 自主行为默认落在这一组的代表会话；动作续说会临时改成"她当时说话的地方"
        outcome.speech_home = session_id
        async with self.session_state(session_id) as state:
            echo_marker = await self._event_marker(state)
            node = self.node(state.node_id) or self.node(self.default_node_id())
            state.world_time += 1

            # 正在做的动作被停用了：立刻停下（否则"停用"看起来没生效）
            current = state.current_action or {}
            current_def = self.world.action_map().get(str(current.get("type") or ""))
            if current and current_def is not None and not current_def.enabled:
                state.current_action = None
                state.state = STATE_IDLE
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": current_def.id,
                        "note": f"「{current_def.name or current_def.id}」已停用，动作中止",
                    },
                )

            # 1) 推进持续动作
            await self._tick_continuous(state, node, outcome)

            # 2) 计划推进（无 LLM）
            await self._tick_plan(state, node, outcome, depth=0)

            # 2.6) 临睡期：安静够了（或拖太久了）就真的去睡；否则这一轮什么都不做
            if state.state == STATE_DROWSY and state.current_action is None:
                await self._drowsy_tick(state, node, outcome)

            # 2.5) 刚到新地方、手上又没安排：就地决定接下来做什么
            #      ——只在她真的空下来、而且计划里也没有下一步时才问；
            #      "移动后本来就有动作"（同一串动作或计划里的下一步）不该再问一次
            if state.pending_arrival and state.current_action is None:
                if self._plan_has_pending_step(state):
                    state.pending_arrival = False
                    outcome.notes.append("落地后计划里还有下一步，不再额外决策")
                else:
                    state.pending_arrival = False
                    await self.decide_after_arrival(
                        state, self.node(state.node_id) or node, outcome
                    )

            # 3) 数值演化
            #    扩展包先走一拍（数值、状态结算），再走主插件自己的演化
            try:
                notes = await self.extensions.on_tick(state, self.world, self._now())
                outcome.notes.extend(notes)
            except Exception as exc:
                self._log("warning", f"扩展这一拍出错：{exc}")
            was_storm = bool(state.storm)
            # 新的一天：掷一次今天的基调（曲线快慢），一天只掷一次
            if self._roll_day_mood(state, self._now()):
                info = day_mood_info(state.day_mood)
                await self._log_event(
                    state,
                    "day_mood",
                    {
                        "day_mood": state.day_mood,
                        "label": info.label if info is not None else "",
                        "hint": info.hint if info is not None else "",
                    },
                )
            mood_reset = self.dynamics.tick(
                state,
                node=node,
                elapsed_seconds=self.tick_seconds,
                world=self.world,
                now=self._now(),
            )
            state.mood = self.dynamics.derive_mood(state)
            # 好感每天朝 0 回落一点（长期不理就慢慢淡；一个会话组一天只结算一次）
            decayed = self.profiles.decay_affinity(state.session_id, now=self._now())
            if decayed:
                await self._log_event(
                    state,
                    "affinity_decay",
                    {"count": len(decayed), "step": self.world.profile.affinity_decay_per_day},
                )
            # 「有点想他」：跟群热闹无关，只跟"多久没跟这个人说话"有关
            self._update_miss(state)
            # 到点的回访：想起一件事 → 接下来这一轮她可以顺口提，也可以去找那个人
            review = self.review_block(state)
            if review:
                state.pending_review = review
            # 睡着了：这段时间的经历该消化了（小睡只做轻整理）
            await self.maybe_consolidate(state)
            if mood_reset:
                await self._log_event(state, "mood_reset", {"valence": round(state.valence, 3)})
            if bool(state.storm) != was_storm:
                await self._log_event(
                    state,
                    "storm",
                    {"on": bool(state.storm), "valence": round(state.valence, 3)},
                )
            await self._record_history(state, node)

            # 3.2) 熬夜代价：夜里醒着，或者为了事件推迟了睡觉，精力掉得更快
            penalty = self._stay_up_penalty(state)
            if penalty > 1.0:
                extra = (
                    float(self.world.state_dynamics.energy_decay_per_min)
                    * (penalty - 1.0)
                    * (self.tick_seconds / 60.0)
                )
                state.energy = max(0.0, float(state.energy) - max(0.0, extra))
            if state.stay_up_until and state.world_time > int(state.stay_up_until):
                state.stay_up_until = 0

            # 3.5) 事件系统：她自己遇上的事、以及等群友拿主意这件事
            #      （生成与结算走打杂模型，只有"她怎么选"用主模型；判定是纯计算）
            try:
                await self._maybe_run_event(state, node, outcome)
            except Exception as exc:
                self._log("warning", f"事件系统出错：{exc}")

            # 4) 群聊留档攒太多时压成摘要（只在配置成"压缩"时才会跑）
            await self._maybe_compress_chat(state, outcome)

            # 4.5) 心事随时间淡掉；到期就放下（写进记忆）
            self._decay_heart_knots(state, now=self._now())
            # 4.6) 她记着的账、她自己的事：都到点就放下（账会写进记忆）
            self._decay_grudges(state, now=self._now())
            self._decay_own_topics(state, now=self._now())

            # 4) 无人回应保护
            was_cooling = bool(state.cooldown_until and state.world_time < state.cooldown_until)
            before_unanswered = int(state.unanswered_count or 0)
            verdict = self.engagement.evaluate(state, tick_seconds=self.tick_seconds)
            if int(state.unanswered_count or 0) > before_unanswered:
                # 主动说话没人理 = 被冷落：只压心情，不抬心潮（否则她会从退缩跳成发作）
                magnitude = self.dynamics.ignored_magnitude(state, now=self._now())
                if magnitude:
                    self.dynamics.apply_event(
                        state, "ignored", magnitude=magnitude, now=self._now()
                    )
            if verdict.in_cooldown and not was_cooling:
                await self._log_event(
                    state,
                    "engagement",
                    {
                        "reason": (
                            f"连续 {verdict.unanswered_count} 次主动说话没人回应，"
                            f"安静 {int(self.world.engagement.cooldown_after_unanswered)} 分钟"
                        )
                    },
                )

            # 5) 极端保护
            tick = max(1, self.tick_seconds)
            flags = self.dynamics.check_extremes(
                state,
                low_energy_ticks=int(3 * 86400 / tick),
                high_loneliness_ticks=int(24 * 3600 / tick),
            )
            if (
                flags
                and state.current_plan is None
                and state.current_action is None
                and self._forced_plan_allowed(state)
            ):
                for flag in flags:
                    plan = self.decider.forced_plan(state, flag)
                    if (
                        plan is not None
                        and self.plan_speaks(plan)
                        and self.engagement.proactive_blocked(state)
                    ):
                        # 刚回过话说明有人理她，别再触发"太久没人说话，强制找人"
                        continue
                    if plan:
                        state.current_plan = plan
                        state.last_forced_plan_at = self._now()
                        state.last_forced_flag = flag
                        self._count_autonomous(state)
                        state.add_event("extreme", {"flag": flag})
                        await self._log_event(state, "extreme", {"flag": flag})
                        break

            # 6) 群名片同步
            await self._sync_nickname(state, node)

            if state.current_action or state.current_plan:
                node = self.node(state.node_id) or node
            outcome.notes.append(
                f"t={state.world_time} node={state.node_id} state={state.state} mood={state.mood}"
            )
            if outcome.messages:
                # 她这一轮主动开口了，也算回应过了这些群聊内容
                self.mark_chat_replied(state, outcome.session_id)
            for target in list((outcome.routed or {}).keys()):
                # 说给别处的那几句，也让那个会话的留言算"回应过"
                self.mark_chat_replied(state, target)
            await self._echo_events_since(state, outcome, echo_marker)
        return outcome

    async def _tick_continuous(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        action = state.current_action
        if not isinstance(action, dict):
            return
        action["elapsed_ticks"] = int(action.get("elapsed_ticks", 0)) + 1
        if int(action.get("elapsed_ticks", 0)) < int(action.get("duration_ticks", 0) or 0):
            return
        await self._finish_action(
            state, node, outcome, action, depth=int(action.get("depth") or 0)
        )

    async def _start_live_call(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
        payload: dict[str, Any],
    ) -> None:
        """持续型的工具 / 指令动作：开始时就把活儿干完，结果留在 payload 里等收尾。

        好处是图片、指令结果在"她刚说要拍"之后立刻发出来，而持续时间只负责占位；
        收尾时用留下来的结果补一句话，句子就不会赶在图片前面。
        """

        try:
            if definition.llm_level == "tool":
                await self._run_tool_calls(state, definition, payload, outcome=outcome)
                if definition.id == "check_weather":
                    await self._store_weather(state, definition, payload, source="manual")
                payload["tool_live"] = True
                # 结果一拿到就记进状态槽：持续动作还要占位一段时间，
                # 没必要等收尾——那段占位时间里提示词也该看得到「今天穿的是…」
                if await self._remember_external_state(
                    state, definition, payload, outcome
                ):
                    payload["slot_recorded"] = True
                return
            info: dict[str, Any] = {}
            await self._run_command_action(
                state,
                node,
                outcome,
                definition,
                PlannedAction(
                    type=definition.id,
                    intent=action.intent,
                    content=action.content,
                    target=action.target,
                    target_node=action.target_node,
                    params=dict(action.params or {}),
                ),
                out=info,
                speak=False,
            )
            if info:
                payload["cmd_cache"] = info
                payload["tool_live"] = True
                if await self._remember_external_state(
                    state, definition, payload, outcome
                ):
                    payload["slot_recorded"] = True
        except Exception as exc:
            # 这次没跑成不算完：不标 tool_live，收尾时会照常再走一遍
            self._log("warning", f"「{definition.id}」的即时调用失败：{exc}")
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": f"即时调用出错，稍后再试：{exc}"},
                outcome=outcome,
            )

    async def _finish_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        action: dict[str, Any],
        *,
        depth: int = 0,
    ) -> None:
        action_id = str(action.get("type", ""))
        definition = self.world.action_map().get(action_id)

        # 这个动作是在哪个会话里被安排的：续说/结果那句话回那儿说。
        # （私聊里让她做饭，做完那句就回私聊，不会跑到群里）
        previous_home = outcome.speech_home
        action_home = str(action.get("session") or "")
        if action_home:
            outcome.speech_home = action_home

        handled_followup = False
        if action_id == "walk_to":
            target_node = str(action.get("target_node", "") or "")
            if target_node and target_node in self.world.node_map():
                # 离开这个地点之前，把刚才在那儿聊的那段总结成一条记忆
                if bool(self.world.memory.summary_on_move):
                    state.memory_flush_wanted = True
                state.node_id = target_node
                # 换地方了：事件系统按"在这里待了多久"决定要不要出事，重新计时
                state.node_since = self._now()
                state.add_event("move", {"to": target_node})
                # 特地走到一个新地方，落地后就地看看这里能做什么
                if int(action.get("arrival_decide", 1)):
                    state.pending_arrival = True
        elif definition is not None and definition.llm_level == "tool":
            # 工具型动作：配了几个工具就按顺序调几个，成功失败都记账，
            # 日志页要能看出"她到底查到了什么 / 为什么没查到"。
            # 持续型动作在开始的那一刻就调过了（``tool_live``），这里不再调第二次。
            if action.get("tool_live"):
                outcome.notes.append(
                    f"「{definition.name or definition.id}」的结果在开始时就拿到了，占位时间结束"
                )
            else:
                await self._run_tool_calls(state, definition, action, outcome=outcome)
                if definition.id == "check_weather":
                    # 查到的天气顺带进全局记录：提示词、编辑器横幅、静默刷新都读它
                    await self._store_weather(state, definition, action, source="manual")
        elif definition is not None and definition.llm_level == "command":
            # 持续型的指令动作：和工具型动作一样，到点才把指令发出去。
            # 以前这里没有这一支，"查天气"这种配成持续动作的指令永远不执行、也一条日志都没有。
            # 开始时就跑过的（``tool_live``）只用当初的结果补一句话，不再执行第二次。
            await self._run_command_action(
                state,
                node,
                outcome,
                definition,
                PlannedAction(
                    type=action_id,
                    intent=str(action.get("intent") or ""),
                    content=str(action.get("content") or ""),
                    target=str(action.get("target") or ""),
                    target_node=str(action.get("target_node") or ""),
                    params=dict(action.get("params") or {}),
                ),
                cached=dict(action.get("cmd_cache") or {}) or None,
                remember=action if isinstance(action, dict) else None,
            )
            handled_followup = True

        effects = (definition.on_complete.effects if definition else {}) or {}
        if effects:
            self.dynamics.apply_effects(state, effects, world=self.world, now=self._now())
        # 持续型亲密动作（靠着、按摩这类）做完也算安抚，同上
        await self._maybe_soothe_from_action(state, definition, outcome=outcome)

        # 查完东西的"满足感"：检索型动作真拿到结果之后，好奇心按动作配置回落一次
        if definition is not None:
            self._satisfy_curiosity(state, definition, action, outcome)
            # 亲近了一下：抱一抱、摸摸头这种**真的碰到**的动作会满足欲求
            await self._satisfy_desire_from_action(state, definition, outcome)

        # 按「实际持续时间」缩放的效果：例如小睡 30 分钟 → 精力 +0.002×30
        per_minute = (definition.on_complete.effects_per_minute if definition else {}) or {}
        if per_minute:
            elapsed_seconds = int(action.get("elapsed_ticks", 0)) * self.tick_seconds
            minutes = max(0.0, elapsed_seconds / 60.0)
            if minutes > 0:
                self.dynamics.apply_effects(
                    state, per_minute, world=self.world, scale=minutes, now=self._now()
                )

        if definition is not None and definition.id == "sleep":
            # 睡整觉结束：睡了多久、中途被吵醒过没有，决定她是清爽还是起床气
            self._apply_wake_quality(
                state,
                slept_minutes=int(action.get("elapsed_ticks", 0)) * self.tick_seconds / 60.0,
                startled=int(state.startled_count or 0),
            )
            state.sleep_started_at = 0
            # 睡醒给她一次机会说早安（发不发、发给谁由她自己定，一天一次）
            await self._maybe_say_goodmorning(state, node, outcome)

        # 行动完成 -> 记忆
        if definition is not None:
            self._remember_action(state, definition, action)

        state.current_action = None
        state.state = STATE_IDLE
        if action.get("from_plan") and state.current_plan is not None:
            plan = state.current_plan
            recorded = action.get("plan_created_at")
            if recorded is None:
                # 没记下自己属于哪份计划（老存档、别的来源起跑的）：保持原来的做法
                advance(state)
            else:
                try:
                    same_plan = int(plan.get("created_at") or 0) == int(recorded)
                    same_step = plan.get("current_step") is not None and int(
                        plan.get("current_step")
                    ) == int(action.get("plan_index", -1))
                except (TypeError, ValueError):
                    same_plan = same_step = False
                if same_plan and same_step:
                    # 计划还是带它起跑的那一份、也还指着它：推掉这一步
                    advance(state)
                else:
                    # 计划中途换过 / 已经有新的安排排到前面：这一步不是计划的了，
                    # 不能顺手把排在她前面的动作标成"做过了"
                    outcome.notes.append("计划已经有别的安排了，这一步不再往后推")

        await self._log_event(
            state,
            "action_done",
            {
                "type": action_id,
                "elapsed_ticks": int(action.get("elapsed_ticks", 0)),
                "tool_result": _clip_text(action.get("tool_result"), 200),
            },
            outcome=outcome,
        )

        trigger = definition.on_complete.trigger if definition else "none"
        detail = str(action.get("tool_result", "") or "")
        evidence_text = self._search_evidence_text(definition, action)
        digest = str(action.get("tool_digest") or "").strip()
        # 别的插件的状态（穿搭、背包…）：把结果记进状态槽，之后每轮都带着
        # （开始时已经记过的就别再记一遍，免得槽里的时间被推到收尾那一刻）
        if not action.get("slot_recorded"):
            await self._remember_external_state(state, definition, action, outcome)
        if digest:
            # 检索型动作交回主模型的是"要点+编号"，不是一堆原文
            detail = digest
        elif evidence_text:
            # 没压出要点时退回编号证据块，也不是被截断的一坨原文
            detail = evidence_text
        images = list(action.get("tool_images") or [])
        if not detail and images:
            detail = "工具返回了一张图片，图片一起发给你了。"
        hint = definition.on_complete.prompt_hint if definition else ""
        if (digest or evidence_text) and not str(hint or "").strip():
            hint = (
                "把查到的内容讲给群里听：只能依据上面这些材料，"
                "不要用印象补充、不要编数字或时间；材料里没有就直说没查到。"
                "别照抄原文、别念网址，用你自己的口吻挑最有用的两三点。"
            )
        if (digest or evidence_text) and bool(getattr(definition, "search_cite", False)):
            hint = (
                str(hint or "").rstrip()
                + " 讲完可以在末尾用括号补一条你参考的来源链接。"
            )
        want_followup = trigger == "llm_followup"
        is_tool_action = bool(definition is not None and definition.llm_level == "tool")
        tool_failed = is_tool_action and not bool(action.get("tool_ok", True))
        if handled_followup:
            # 指令动作自己已经把结果交回给她说过一句了，这里别再问一次
            want_followup = False
        if want_followup and is_tool_action and not detail:
            failure = str(action.get("tool_error") or "").strip()
            if failure:
                # 没做成也要说一句：把**失败原因**交给她，让她照实讲，
                # 别让这一步静默消失（用户会以为她根本没试）。
                detail = f"（这一步没做成：{failure}）"
                hint = (
                    hint
                    or "刚才那件事没做成，用一句自然的话说明一下，别编内容、别道歉过头。"
                )
                want_followup = True
            else:
                # 工具真的一点东西都没回：别让她"就着空气说话"——那只会编
                want_followup = False
                outcome.notes.append(
                    f"工具动作 {action_id} 没有拿到结果（结果为空），不作续说"
                )
        if (
            not want_followup
            and trigger == "none"
            and not tool_failed
            and detail
            and is_tool_action
            and bool(self.world.tool_result_reply)
        ):
            # 工具型动作拿到结果后，默认交回主模型说一句（可以用全局设置关掉）
            want_followup = True
            hint = hint or "把刚才查到的结果用你自己的话说出来，简短自然，别念成清单。"
        if want_followup and self._silent_followup:
            # 事件里"先去做点什么"那一轮：结果只交回给她判断，不往群里说
            want_followup = False
            outcome.notes.append(f"动作 {action_id} 的结果只用于事件判断，不作续说")
        if want_followup and self.llm is not None:
            if not detail:
                # 不是工具动作（做饭、看书…）也要能续说：给她一句"刚刚做了什么"的交代
                minutes = max(
                    0, round(int(action.get("elapsed_ticks", 0)) * self.tick_seconds / 60)
                )
                label = definition.name or definition.id if definition else ""
                detail = f"你刚刚做完了「{label}」，用了大约 {minutes} 分钟。"
            detail = self._clip_followup(detail)
            await self._llm_followup(
                state,
                node,
                outcome,
                hint,
                detail,
                image_urls=images,
                depth=depth + 1,
                after_search=bool(
                    definition is not None
                    and str(getattr(definition, "tool_flow", "simple")) == "search"
                ),
                definition=definition,
            )
        elif trigger == "schedule":
            await self._run_linked_schedule(state, node, outcome, definition, depth=1)

        if action_home:
            outcome.speech_home = previous_home

    async def _remember_external_state(
        self,
        state: WorldState,
        definition: ActionDef | None,
        action: dict[str, Any],
        outcome: TickOutcome | None = None,
    ) -> bool:
        """把这次的结果记进状态槽（别的插件的状态就靠它留下来）。

        只有配了 ``state_slot`` 的动作才记：留档里那些"今天穿的是白色卫衣"之类的
        东西，下一轮提示词里要继续看得到。

        返回值表示**真的写进去了**（没配槽、没有结果文本时返回 ``False``），
        调用方靠它决定要不要在收尾时再试一次。
        """

        slot = str(getattr(definition, "state_slot", "") or "").strip()
        if not slot or definition is None:
            return False
        raw = str(
            action.get("tool_result")
            or action.get("tool_digest")
            or (dict(action.get("cmd_cache") or {}) or {}).get("text")
            or ""
        ).strip()
        if not raw:
            return False
        text = raw
        if bool(getattr(definition, "state_summarize", False)) and self.helper_llm is not None:
            summary = await self._summarize_state_text(state, definition, raw)
            if summary:
                text = summary
        label = str(getattr(definition, "state_label", "") or "").strip() or slot
        try:
            ttl = max(0, int(getattr(definition, "state_ttl_minutes", 0) or 0))
        except (TypeError, ValueError):
            ttl = 0
        now = self._now()
        state.external_state = {
            **dict(getattr(state, "external_state", None) or {}),
            slot: {
                "text": _clip_text(text, 400),
                "label": label,
                "at": now,
                "expires_at": (now + ttl * 60) if ttl else 0.0,
            },
        }
        await self._log_event(
            state,
            "state_slot",
            {"slot": slot, "label": label, "text": _clip_text(text, 200)},
            outcome=self._echo_into(outcome, state),
        )
        return True

    async def _summarize_state_text(
        self, state: WorldState, definition: ActionDef, raw: str
    ) -> str:
        """让打杂模型把结果压成一句话（返回是一大段时用）。"""

        label = str(getattr(definition, "state_label", "") or definition.name or "").strip()
        try:
            reply = await self._ask_helper(
                state.session_id,
                "你把一段结果压成一句话，给角色自己看。只输出这句话本身，"
                "保留具体内容（颜色、名称、数字），不要客套、不要解释。",
                f"这是「{label or '当前状态'}」的内容：\n{_clip_text(raw, 1200)}\n\n"
                "用一句话说清它现在是什么。",
            )
        except Exception as exc:
            self._log("debug", f"压缩状态槽失败：{exc}")
            return ""
        return " ".join(str(reply or "").split())[:200]

    async def _run_linked_schedule(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        *,
        depth: int,
    ) -> None:
        """``on_complete.trigger = schedule``：动作做完后接着跑另一个日程的动作链。"""

        schedule_id = (definition.on_complete.schedule_id or "").strip()
        if not schedule_id or self.schedules is None:
            return
        target = next(
            (item for item in self.schedules.schedules if item.id == schedule_id), None
        )
        if target is None:
            outcome.notes.append(f"找不到后续日程 {schedule_id}")
            return
        if depth > self.world.limits.max_action_chain_depth:
            outcome.notes.append("动作链超过深度上限，已停止")
            return
        state.add_event("chain", {"schedule": schedule_id})
        await self._run_chain(state, target.action_chain, outcome, depth=depth)

    def _remember_action(
        self, state: WorldState, definition: ActionDef, action: dict[str, Any]
    ) -> None:
        label = definition.name or definition.id
        if definition.id == "think":
            content = str(action.get("content", "") or "")
            if content:
                state.add_thought(content)
                self.memory.remember(
                    session_id=state.session_id,
                    persona_id=str(action.get("persona_id", "")),
                    node_id=state.node_id,
                    content=content,
                    memory_type=INNER,
                    emotion=state.mood,
                    weight=0.35,
                    affect=state.affect,
                    valence=state.valence,
                )
            return
        self.memory.remember(
            session_id=state.session_id,
            persona_id=str(action.get("persona_id", "")),
            node_id=state.node_id,
            content=f"在这里{label}",
            memory_type=SCENE,
            emotion=state.mood,
            weight=0.3,
            affect=state.affect,
            valence=state.valence,
        )

    async def _llm_followup(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        hint: str,
        tool_result: str,
        image_urls: list[str] | None = None,
        *,
        depth: int = 1,
        after_search: bool = False,
        definition: Any = None,
    ) -> None:
        persona_text = await self._persona_text(state.session_id)
        _cell, say_limit, style_text = self.style_for(state, state.session_id)
        blocked_actions = self._hidden_actions(state)
        system_prompt = self.prompts.build_autonomous_system_prompt(
            persona_text=persona_text,
            state=state,
            node=node,
            available_tools=self.available_tools(),
            memories=self.memory.recall(
                session_id=state.session_id,
                persona_id="",
                node_id=state.node_id,
                limit=3,
            ),
            engagement_hint=self.engagement.hint(state),
            max_messages=say_limit,
            recent_chat=self.chat_window(state),
            reasoning=bool(self.world.reasoning_enabled),
            style_block=style_text,
            hidden_actions=blocked_actions,
            session_directory=self.session_directory(state),
            # 她此刻是在哪个会话里说话：这里/别处的划分、水位线都按它算
            current_session=outcome.home(),
            session_labels=self.session_labels(state),
            profile_text=self.profile_block(state),
            extra_notes=[note for note in [self.extra_reminders(state)] if note],
            json_fields=self.extensions.json_fields(),
            **await self.runtime_notes(state.session_id),
        )
        prompt = self.prompts.build_reply_followup_prompt(
            hint,
            tool_result,
            no_search=after_search,
            event_digest="；".join(
                str(item)
                for item in list(state.event_digest or [])[-4:]
                if str(item).strip()
            ),
        )
        # 用过了就清掉：这段时间发生的事只带出来一次
        state.event_digest = []
        reply = await self._ask_llm(
            state.session_id,
            system_prompt,
            prompt,
            image_urls=list(image_urls or []) or None,
        )
        outcome.llm_calls += 1
        if reply is None:
            return
        result = parse_action_payload(
            reply,
            available_actions=self._parseable_action_ids(
                state.node_id, blocked=blocked_actions
            ),
            valid_nodes=set(self.world.node_map()),
            max_actions=self.world.limits.max_actions_per_message,
            max_messages=say_limit,
            json_fields=self.extensions.json_fields(),
        )
        actions, blocked = self._followup_actions(
            result.actions, depth=depth, after_search=after_search
        )
        # 「她在看什么 / 在玩什么」这类内容：不是工具返回的，是她自己刚说出来的。
        # 配了状态槽的动作把它记下来，之后每一轮提示词里都带着——不然每次追剧
        # 都像第一次看，只会凭空吐槽（"这剧情也太离谱了"，但你不知道她在看什么）。
        self._note_action_state_slot(
            state,
            definition,
            [text for item in actions if item.type == "say" for text in (item.messages or [])],
        )
        if blocked:
            await self._log_event(
                state,
                "skip",
                {
                    "action": "、".join(blocked),
                    "note": "续说这一轮不再接新的检索 / 工具动作（刚查完或链条太深），只让她说话",
                },
                outcome=outcome,
            )
        await self._execute_actions(
            state, node, outcome, actions, depth=depth, autonomous=True
        )

    def _note_action_state_slot(
        self, state: WorldState, definition: Any, said: list[str]
    ) -> bool:
        """把"她刚说的那句"记进动作声明的状态槽（非工具型动作用）。

        追剧、看书、打游戏这类动作没有工具返回值，内容是**她自己定的**。
        记下来之后提示词里就有"在看的剧：…"，她不会每次都对同一个剧重新开始。
        """

        slot = str(getattr(definition, "state_slot", "") or "").strip()
        if not slot or definition is None:
            return False
        text = " ".join(" ".join(str(item or "").split()) for item in said if str(item or "").strip())
        if not text:
            return False
        label = str(getattr(definition, "state_label", "") or "").strip() or slot
        try:
            ttl = max(0, int(getattr(definition, "state_ttl_minutes", 0) or 0))
        except (TypeError, ValueError):
            ttl = 0
        now = self._now()
        state.external_state = {
            **dict(getattr(state, "external_state", None) or {}),
            slot: {
                "text": _clip_text(text, 200),
                "label": label,
                "at": now,
                "expires_at": (now + ttl * 60) if ttl else 0.0,
            },
        }
        return True

    def _followup_actions(
        self, actions: list[Any], *, depth: int, after_search: bool
    ) -> tuple[list[Any], list[str]]:
        """续说那一轮能用哪些动作。

        两条闸门：
        - **刚查完就别再查**：不然她会"查一段、说一段、再查一段"，一路查下去；
        - **链条太深就只让她说话**：以前每轮都从 depth=1 重新起链，等于没有上限。
        """

        limit = int(self.world.limits.max_action_chain_depth)
        too_deep = depth > limit
        kept: list[Any] = []
        blocked: list[str] = []
        for item in actions:
            definition = self.world.action_map().get(str(getattr(item, "type", "")))
            is_search = (
                definition is not None
                and str(getattr(definition, "tool_flow", "simple")) == "search"
            )
            is_tool = definition is not None and definition.llm_level in ("tool", "command")
            if (after_search and is_search) or (too_deep and is_tool):
                blocked.append(str(getattr(item, "type", "")))
                continue
            kept.append(item)
        if blocked:
            self._log(
                "debug",
                f"续说这一轮不接新的检索/工具动作：{'、'.join(blocked)}",
            )
        return kept, blocked

    async def _tick_plan(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        *,
        depth: int,
    ) -> None:
        if depth > self.world.limits.max_action_chain_depth:
            return
        if state.state == STATE_DROWSY:
            # 临睡期里计划不推进：她正打着盹，"睡觉"那一步已经揣在手里了，
            # 这时候放计划往前走就等于当晚没打盹——下一拍又直接躺下了。
            return
        if state.current_action is not None:
            return
        step = peek_step(state)
        if step is None:
            return
        action = self._step_to_action(step)
        step_home = str(step.get("session") or "")
        previous_home = outcome.speech_home
        if step_home:
            outcome.speech_home = step_home
        if str(step.get("kind") or "") == "reply":
            # 这一句是当时有人跟她说话时排下的：轮到时按"回话"发，不算她主动开口
            outcome.speech_kind = "reply"
        executed = await self._execute_actions(
            state,
            node,
            outcome,
            [action],
            depth=depth,
            autonomous=True,
            from_plan=True,
            # 日程排下来的步骤同样不算"她自己想找他"
            from_schedule=str((active_plan(state) or {}).get("source") or "") == "schedule",
        )
        outcome.speech_home = previous_home
        if not executed:
            # 这一步被跳过了（地点/前置条件/缺工具…）。不推进的话它会每个 tick 重试一次。
            outcome.notes.append(f"计划里的「{action.type}」没做成，跳过这一步")
            advance(state)

    # ================= 自主行为 =================

    async def maybe_decide(
        self, session_id: str, *, force: bool = False
    ) -> TickOutcome | None:
        """决策器评估：规则优先，必要时（低频抽样）才问 LLM。

        ``force=True``（编辑器里手动点"触发决策"）会跳过间隔节流，
        但仍然尊重"她正在忙 / 在睡觉"这些硬条件——只是不再静默吞掉。
        """

        if not self.is_enabled(session_id):
            return None
        now = self._now()
        # 节流按"这一整个她"算，不按会话：同一个组的几个会话共用一个她
        pace_key = self.state_key(session_id)
        last = self._last_decider_at.get(pace_key, 0.0)
        if not force and now - last < self.decider_interval:
            return None
        self._last_decider_at[pace_key] = now

        outcome = TickOutcome(session_id=session_id)
        outcome.place = session_id
        pushed = False
        async with self.session_state(session_id) as state:
            echo_marker = await self._event_marker(state)
            node = self.node(state.node_id) or self.node(self.default_node_id())
            if state.current_action is not None or active_plan(state) is not None:
                return None
            if state.is_sleeping:
                return None
            if not self.engagement.can_speak(state):
                outcome.notes.append("处于冷却期，跳过自主行为")
            elif not self._within_hourly_limit(state):
                outcome.notes.append("达到每小时自主行为上限")
            else:
                # 长期低落会关掉「想被注意到」这条动机（带最小关闭时长）
                self._refresh_interject_closed(state)
                # 先看"是不是想他想得不行了"：软推她自己去找他一次（在哪说由她决定）
                pushed = await self._maybe_miss_push(state, node, outcome, session_id)
                if not pushed:
                    # 想被碰一碰：同样是"交给她自己决定"，但要排在"想他"后面
                    pushed = await self._maybe_desire_push(
                        state, node, outcome, session_id
                    )
                if not pushed:
                    await self._decide_normally(state, node, outcome, session_id)
            await self._echo_events_since(state, outcome, echo_marker)
        # 发消息这件事必须在**出锁之后**做：_deliver 自己还要拿这把会话锁
        if outcome.messages or outcome.live_messages or outcome.debug_messages:
            await self._deliver(outcome)
        return outcome

    async def _decide_normally(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        session_id: str,
    ) -> None:
        """没被软推打断时的常规自主决策（规则优先，低频抽样才问 LLM）。"""

        # 「她想插话但被拦住」按闸门分类计数：单看频率数字没法知道是谁在限流
        gate = self.interject_gate(state)
        wants_interject = self.group_is_chatting(state) and float(
            state.loneliness
        ) >= float(self.world.decider.interject_threshold)
        if wants_interject:
            self._count_interject_gate(state, gate)
        plan = self.decider.rule_plan(
            state,
            group_chatting=self.group_is_chatting(state),
            interject_allowed=gate == "",
        )
        # 要不要交给大模型，由「决策意愿」算出的概率决定；
        # 规则决策不受影响，命中就直接执行。
        if self.decider.should_ask_llm(state):
            llm_plan = await self._ask_llm_for_plan(state, node, outcome)
            plan = llm_plan or plan
        if plan is not None and self.plan_speaks(plan):
            # 刚被搭话、她也回过了：这段时间别主动开口，免得跟被动回复挤在一起
            if self.engagement.proactive_blocked(state):
                outcome.notes.append("刚回过话，这轮不主动搭话")
                await self._log_event(
                    state,
                    "engagement",
                    {"reason": "刚回过话，跳过主动发言"},
                )
                plan = None
        if plan is not None:
            # 她想说给谁：她自己在计划里挑了一个会话就发那儿；没挑就按动机定——
            # 「因为别人在说话所以她想接一句」要落在**那个说话的会话**，
            # 其余情况落在这一次是在哪儿决定的。
            outcome.session_id = self._freeze_plan_target(
                state,
                outcome,
                plan,
                fallback=self._plan_home_for(state, plan, session_id),
            )
            await self._apply_plan(state, node, outcome, plan)
            self._count_autonomous(state)
            await self._tick_plan(state, node, outcome, depth=0)

    async def _apply_plan(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        plan: dict[str, Any],
    ) -> None:
        """把一份计划挂到状态上并记日志（自主决策与「抵达新地点」共用）。"""

        if any(step.get("interject") for step in plan.get("steps", [])):
            state.last_interject_at = self._now()
        state.current_plan = plan
        # 留一份「最近一次生成的计划」：做完之后提示词里还要能提到它
        state.last_plan = {
            "steps": [
                {
                    "action": str(step.get("action") or ""),
                    "target_node": str(step.get("target_node") or ""),
                }
                for step in plan.get("steps", [])
                if isinstance(step, dict)
            ],
            "reason": str(plan.get("reason") or ""),
            "source": str(plan.get("source") or ""),
            "at": int(state.world_time),
            "send_to": str(plan.get("send_to") or ""),
        }
        if plan.get("reason"):
            state.note_reasoning({"intent": str(plan.get("reason"))}, source="plan")
        state.add_event(
            "plan", {"reason": plan.get("reason"), "source": plan.get("source")}
        )
        # 这份计划要说的话落在哪个会话：日志里跟着一起写出来，
        # 免得"决定在私聊"和"话说在群里"看起来对不上
        target = str(plan.get("send_to") or "").strip()
        target_label = self.session_label(target, state) if target else ""
        await self._log_event(
            state,
            "plan",
            {
                "reason": plan.get("reason"),
                "source": plan.get("source"),
                "steps": plan.get("steps"),
                "send_to": target_label,
                "raw": _clip_text(plan.get("raw"), 200),
            },
            outcome=outcome,
        )

    async def decide_after_arrival(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        """刚走到一个新地方：立刻看看这儿能做什么。

        「特地走过去」本身就是一次明确的意图，所以这一步直接问大模型，
        不看那条概率抽样、也不占「每小时最多自主行动次数」的额度；
        但仍然守冷却和一个专门的每小时上限，避免来回走 + 反复决策把 token 烧光。
        """

        if not self.world.decider.enabled or state.is_sleeping:
            return
        if state.current_plan is not None or state.current_action is not None:
            return
        if not self.engagement.can_speak(state):
            outcome.notes.append("抵达新地点，但处于冷却期，不做决策")
            return
        if not self._arrival_decision_allowed(state):
            outcome.notes.append("抵达新地点的决策次数已达每小时上限")
            return
        self._count_arrival_decision(state)
        plan = self.decider.rule_plan(
            state,
            group_chatting=self.group_is_chatting(state),
            interject_allowed=self.interject_allowed(state),
        )
        if plan is not None and self.plan_speaks(plan) and self.engagement.proactive_blocked(state):
            outcome.notes.append("刚回过话，到了新地方也不主动搭话")
            plan = None
        # 她特地走过来通常是有目的的：把刚才的打算和她现在能做什么摆在最前面，
        # 免得这次调用还在"重新规划整段时间"。
        where = (node.name if node else "") or state.node_id
        intent = str((state.last_reasoning or {}).get("intent") or "").strip()
        here = "、".join(
            f"{item.id}（{item.name or item.id}）" for item in self.world.actions_in(state.node_id)
        )
        hint = f"你刚刚特地走到了「{where}」"
        hint += f"，因为你打算：{intent}。" if intent else "。"
        hint += (
            f"这里能做：{here or '没什么特别的'}。"
            "直接安排你到了这里要做的那件事；如果你只是随便走走，就挑一件这儿能做的事。"
        )
        llm_plan = await self._ask_llm_for_plan(
            state, node, outcome, force=True, hint=hint
        )
        plan = llm_plan or plan
        if plan is None:
            return
        outcome.notes.append("抵达新地点，就地决定接下来做什么")
        outcome.session_id = self._freeze_plan_target(
            state,
            outcome,
            plan,
            fallback=self._plan_home_for(state, plan, outcome.session_id),
        )
        await self._apply_plan(state, node, outcome, plan)
        await self._tick_plan(state, node, outcome, depth=0)

    async def _ask_llm_for_plan(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        *,
        force: bool = False,
        hint: str = "",
        with_extension: bool = True,
    ) -> dict[str, Any] | None:
        if self.llm is None:
            return None
        if not force and not self._llm_plan_allowed(state):
            outcome.notes.append("LLM 计划预算已用完，本轮只用规则决策")
            return None
        persona_text = await self._persona_text(state.session_id)
        _cell, say_limit, style_text = self.style_for(state, state.session_id)
        # 配额用尽的动作：计划里既不列出来，也不接受模型写它
        blocked_actions = self._hidden_actions(state)
        system_prompt = self.prompts.build_autonomous_system_prompt(
            persona_text=persona_text,
            state=state,
            node=node,
            available_tools=self.available_tools(),
            memories=self.memory.recall(
                session_id=state.session_id, persona_id="", node_id=state.node_id, limit=4
            ),
            engagement_hint=self.engagement.hint(state),
            max_messages=say_limit,
            recent_chat=self.chat_window(state),
            mode="plan",
            reasoning=bool(self.world.reasoning_enabled),
            style_block=style_text,
            hidden_actions=blocked_actions,
            session_directory=self.session_directory(state),
            current_session=state.session_id,
            session_labels=self.session_labels(state),
            profile_text=self.profile_block(state),
            # 这是"她自己决定下一步做什么"的那一轮：好奇心可以推动她去问人
            extra_notes=[
                note for note in [self.extra_reminders(state, autonomy=True)] if note
            ],
            **await self.runtime_notes(state.session_id),
        )
        # 装了扩展（注册了提示词层的那种）就把它的那一层也带上：
        # 她自己决定这一轮做什么的时候，得看得见扩展那几个数值。
        if with_extension:
            layer = self._extension_prompt(state, state.session_id)
            if layer:
                system_prompt = f"{system_prompt}\n\n{layer}"
        note = self.autonomous_prompt_note(state)
        prompt = (
            (f"{hint}\n\n" if hint else "")
            + ("\n\n" + note + "\n\n" if note else "")
            + "按你自己的节奏决定接下来做什么，输出计划 JSON：\n"
            '{"plan":[{"action":"walk_to","target_node":"window"},'
            '{"action":"stare","duration":600},{"action":"think","content":"..."}],'
            '"valid_until":1800,"reason":"为什么这样安排"}\n'
            "计划的长度由你决定：可以是几分钟的小事，也可以睡一整晚"
            "（duration 是这一步持续多少秒，睡觉是 28800；valid_until 是这份计划大概管多少秒）。\n"
            "结合上面的日期时间、你的状态和作息挑一件最合适的事："
            "夜里或凌晨、或者实在撑不住了，才去睡整觉（28800 秒）；"
            "白天只是犯困就先小睡一会儿（10~60 分钟，睡多久你自己定），缓过来接着过，"
            "别在白天一睡八小时——那样醒来正好是半夜。\n"
            + self.prompts.AUTONOMOUS_MANNERS
            + "只输出 JSON。"
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        outcome.llm_calls += 1
        self._count_llm_plan(state)
        if reply is None:
            return None
        plan, _warnings = parse_plan_payload(
            reply,
            available_actions=self._parseable_action_ids(
                state.node_id, blocked=blocked_actions
            ),
            valid_nodes=set(self.world.node_map()),
        )
        if plan:
            created = create_plan(
                steps=plan.get("steps", []),
                world_time=state.world_time,
                valid_for=int(plan.get("valid_for", self.world.limits.plan_valid_duration)),
                reason=str(plan.get("reason", "")),
                source="llm",
                send_to=str(plan.get("send_to") or ""),
            )
            if created is not None:
                # 留下模型原话，日志页里能看到"她为什么这么安排"
                created["raw"] = _clip_text(reply, 240)
            return created
        return None

    # ================= 动作执行 =================

    def _available_action_ids(self, node_id: str) -> set[str]:
        return {
            action.id
            for action in self.world.actions_in(node_id)
            if self._action_usable_now(action)
        }

    def _usable_action_ids(self, state: WorldState, node_id: str) -> set[str]:
        """这一轮真的能用的动作：地点 + 时段 + 没配额用尽。"""

        blocked = self.exhausted_actions(state)
        return {
            action.id for action in self.world.actions_in(node_id) if action.id not in blocked
        }

    def _action_usable_now(self, action: Any) -> bool:
        """这个动作现在这个时段能不能用。

        目前只有一条：非夜晚不给「睡觉」（提示词不提供、解析也拒掉）。
        日程 / 手动投递这类明确指令不受影响——那是用户自己安排的。
        """

        if str(getattr(action, "id", "")) != "sleep":
            return True
        return self.world.sleep_allowed_now(self.local_now().hour)

    def _nearest_node_with_action(self, node_id: str, definition: ActionDef) -> str:
        """哪个可达地点能做这个动作（挑最近的）。"""

        candidates = [
            node.id for node in self.world.nodes if definition.available_in(node.id)
        ]
        if not candidates:
            return ""
        return nearest_node(self.world.adjacent(), node_id, candidates) or ""

    def _parseable_action_ids(
        self, node_id: str, *, blocked: set[str] | None = None
    ) -> set[str]:
        """解析模型输出时认可的动作集合。

        默认 = 当前地点能做的动作。开启「允许她想去别处做某事」后，
        可达地点能做的动作也算数——她写出来，插件负责带她过去。
        ``blocked`` 是这一轮要挡掉的（配额用尽那批）：提示词里不列、解析也拒。
        """

        if not bool(self.world.remote_action_travel):
            ids = self._available_action_ids(node_id)
            return {item for item in ids if item not in (blocked or set())}
        ids = {
            action.id
            for action in self.world.actions
            if self._action_usable_now(action)
            and (
                action.available_in(node_id)
                or self._nearest_node_with_action(node_id, action)
            )
        }
        return {item for item in ids if item not in (blocked or set())}

    @staticmethod
    def _plan_has_pending_step(state: WorldState) -> bool:
        """这一步是否还有安排（计划里还有没做的步骤）。

        只看、不动状态：``peek_step`` 会在计划结束时顺手清掉计划，
        这里只想知道"落地之后是不是还有事要做"。
        """

        plan = active_plan(state)
        if not isinstance(plan, dict):
            return False
        steps = plan.get("steps") or []
        return int(plan.get("current_step", 0)) < len(steps)

    def _step_to_action(self, step: dict[str, Any]) -> PlannedAction:
        return PlannedAction(
            type=str(step.get("action", "")),
            interject=bool(step.get("interject", False)),
            messages=list(step.get("messages") or []),
            target=str(step.get("target", "") or ""),
            target_node=str(step.get("target_node", "") or ""),
            content=str(step.get("content", "") or ""),
            intent=str(step.get("intent", "") or ""),
            params=dict(step.get("params") or {}),
            duration=int(step.get("duration", 0) or 0),
            queries=[str(item) for item in (step.get("queries") or []) if str(item)],
            search_depth=str(step.get("search_depth", "") or ""),
            read_pages=_to_int_or_default(step.get("read_pages"), -1),
            send_to=str(step.get("send_to", "") or ""),
            raw=step,
        )

    def _speech_target(
        self, state: WorldState, action: PlannedAction, outcome: TickOutcome
    ) -> str | None:
        """这一步说给哪个会话听。

        - 她没写落点：跟着这一轮的落点走；
        - 写了、而且对得上：发到那个会话；
        - 写了、但对不上（例如「私聊某个不在名单里的人」）：``None``，
          这句话干脆不说——总比把她想私下说的话发到群里强。
        """

        raw = str(getattr(action, "send_to", "") or "").strip()
        if not raw:
            # 没写落点：她"正在说的那个会话"优先（动作续说、亲昵动作都跟着它走）
            return outcome.home()
        target = self.resolve_send_to(state, raw, fallback="")
        return target or None

    def _plan_target(
        self,
        state: WorldState,
        outcome: TickOutcome,
        plan: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        """计划级的落点：她写了就用她的，写不出来就留在当前会话（并记一笔）。"""

        raw = str((plan or {}).get("send_to") or "").strip()
        if not raw:
            return str(fallback)
        target = self.resolve_send_to(state, raw, fallback="")
        if target:
            return target
        outcome.notes.append(
            f"落点「{raw}」不在她能说话的地方里，这一轮按当前会话处理"
        )
        return str(fallback)

    def _freeze_plan_target(
        self,
        state: WorldState,
        outcome: TickOutcome,
        plan: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        """把"这份计划要在哪儿说"当场定死，写进计划和每一步。

        不定死的话，落点是**执行那一刻**按"谁在推进这一拍"算的：一个会话组里几个
        群 / 私聊共用同一份状态，她在私聊里决定要说的话，可能被群里那一拍捡去执行，
        说出口的地方就跟着变了（日志写着私聊、消息却出现在群里）。
        每一步上的 ``session`` 就是执行时用的落点，``send_to`` 留给人看。
        """

        target = self._plan_target(state, outcome, plan, fallback=fallback)
        plan["send_to"] = str(target or "")
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            raw = str(step.get("send_to") or "").strip()
            if raw:
                # 她自己给某一步单独写了落点（"先去群里说，再私聊告诉他"）
                picked = self.resolve_send_to(state, raw, fallback="")
                if not picked:
                    outcome.notes.append(
                        f"这一步想说到「{raw}」，但那儿不在她能说话的地方里，"
                        "改按整份计划的落点处理"
                    )
                    step["send_to"] = ""
                    picked = str(target or "")
            else:
                picked = str(target or "")
            if not str(step.get("session") or "").strip():
                step["session"] = picked
        return str(target or "")

    def talking_session(self, state: WorldState) -> str:
        """最近别人是在哪个会话里说话的。

        她想接话、想找人聊的时候，落点看这个：会话组里几个群 / 私聊共用一份状态，
        「群里正热闹」里的"群里"是**那个真的有人在说话的会话**，不是碰巧在推进的这一拍。
        """

        for item in reversed(self.chat_window(state)):
            if item.get("is_self"):
                continue
            origin = str(item.get("origin") or "")
            if origin and self.is_enabled(origin):
                return origin
        return str(state.session_id)

    def _plan_home_for(
        self, state: WorldState, plan: dict[str, Any], fallback: str
    ) -> str:
        """这份计划没写落点时该落在哪儿。

        规则决策里"她主动开口"的动机（群里热闹想接一句、孤独了想找人说话）本来就来自
        某个人在某个会话里说的话，那就落在那个会话；其余情况留在决定它的地方。
        """

        steps = [step for step in (plan.get("steps") or []) if isinstance(step, dict)]
        if not steps:
            return str(fallback)
        wants_people = any(step.get("interject") for step in steps)
        if not wants_people and str(plan.get("source") or "") == "rule":
            # 规则排的"去大厅找人说话"这类计划：没有 interject 标记，但同样是冲着人去的
            wants_people = any(
                str(step.get("action") or "") == "say" for step in steps
            )
        if not wants_people:
            return str(fallback)
        return self.talking_session(state) or str(fallback)

    @staticmethod
    def _step_payload(
        item: PlannedAction, session: str = "", kind: str = ""
    ) -> dict[str, Any]:
        """把待执行动作转成计划里的一步。

        注意 ``intent`` 一定要带上：工具型动作的参数就是靠它补出来的，
        少了它这一步到点执行时只会得到「没有给出想做什么」。

        ``kind`` 记的是这一步的来路（``reply`` = 当时有人在跟她说话）：
        排队到后面才轮到时，这种话不该被"主动发言冷却"吞掉。
        """

        return {
            "action": item.type,
            "kind": str(kind or ""),
            "interject": bool(item.interject),
            "messages": list(item.messages or []),
            "target": item.target,
            "target_node": item.target_node,
            "duration": item.duration,
            "content": item.content,
            "intent": item.intent,
            "params": dict(item.params or {}),
            "queries": list(item.queries or []),
            "search_depth": str(item.search_depth or ""),
            "read_pages": int(item.read_pages),
            "send_to": str(item.send_to or ""),
            # 这一步是"在哪儿安排的"：轮到她做的时候，说话回那儿
            "session": str(session or ""),
            "status": "pending",
        }

    @staticmethod
    def _plan_remaining_steps(
        plan: dict[str, Any] | None,
        *,
        skip_current: bool = False,
        after: int | None = None,
    ) -> list[dict[str, Any]]:
        """一份计划里还没做完的步骤。

        ``skip_current=True`` 用于"正在执行计划里这一步"的场景：那一步已经拿在手上了，
        再排一遍会重复执行。
        ``after`` 直接指定从哪一步之后开始算：她自己那一步正在跑时必须用它，
        光看 ``current_step`` 已经不准了（中途可能往队里插过新的动作）。
        """

        if not isinstance(plan, dict):
            return []
        steps = plan.get("steps") or []
        if after is None:
            index = max(0, int(plan.get("current_step", 0) or 0))
            if skip_current:
                index += 1
        else:
            index = max(0, int(after) + 1)
        return [
            dict(step)
            for step in steps[index:]
            if isinstance(step, dict) and step.get("action")
        ]

    def _running_plan_index(
        self, state: WorldState, plan: dict[str, Any] | None
    ) -> int | None:
        """她手上正在做的那一步在这份计划里的下标。

        计划中途可能被插进新的安排（``current_step`` 会指到别的地方），所以不能只看
        下标；这里要求动作是**从这份计划**起跑的，计划号和下标都对得上才算数。
        """

        if not isinstance(plan, dict):
            return None
        action = state.current_action if isinstance(state.current_action, dict) else None
        if not action or not action.get("from_plan"):
            return None
        recorded = action.get("plan_created_at")
        if recorded is None:
            return None
        try:
            if int(recorded) != int(plan.get("created_at") or 0):
                return None
            index = int(action.get("plan_index") or 0)
        except (TypeError, ValueError):
            return None
        if 0 <= index < len(plan.get("steps") or []):
            return index
        return None

    def _plan_queue_anchor(self, state: WorldState, plan: dict[str, Any]) -> int:
        """新排的动作插在计划的第几步：她手上那一步正跑就插在它后面，否则插在当前步上。

        插进去而不是"重建一份计划"，是为了让正在跑的那一步还认得出自己——
        重建会把它的下标冲掉，等它做完时一推进就会误伤排在前面的新动作。
        """

        steps = plan.get("steps") or []
        running = self._running_plan_index(state, plan)
        if running is not None:
            index = running + 1
        else:
            index = int(plan.get("current_step", 0) or 0)
        return max(0, min(index, len(steps)))

    def _queue_steps(
        self,
        state: WorldState,
        outcome: TickOutcome,
        rest: list[PlannedAction],
        carried: list[dict[str, Any]],
        *,
        kind: str,
        note: str,
    ) -> None:
        """把这一批里还没做的动作排进计划：接在她手头那一步之后，原来的安排照样保住。"""

        fresh = [self._step_payload(item, outcome.home(), kind) for item in rest]
        plan = state.current_plan if isinstance(state.current_plan, dict) else None
        if plan is None:
            state.current_plan = create_plan(
                steps=fresh + carried,
                world_time=state.world_time,
                valid_for=self.world.limits.plan_valid_duration,
                reason="同一轮里还没做完的动作",
                source="pending",
            )
        else:
            anchor = self._plan_queue_anchor(state, plan)
            steps = list(plan.get("steps") or [])
            steps[anchor:anchor] = fresh
            plan["steps"] = steps
            # 新排的动作有自己的有效期，别一排队就过期
            plan["valid_until"] = max(
                int(plan.get("valid_until") or 0),
                int(state.world_time) + max(60, int(self.world.limits.plan_valid_duration)),
            )
        if note:
            outcome.notes.append(note)

    async def _execute_actions(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        actions: list[PlannedAction],
        *,
        depth: int,
        autonomous: bool,
        from_plan: bool = False,
        allow_remote_travel: bool = True,
        from_schedule: bool = False,
        skip_drowsy: bool = False,
    ) -> bool:
        """执行一串动作。返回"有没有真的执行到至少一个"。

        ``allow_remote_travel=False`` 时不做「她想去别处做这件事」的兜底：
        日程链的地点限制由日程自己的开关决定，不能偷偷替它补一步移动。
        ``from_schedule=True``：这串动作是**用户排的日程**，不是她自己临时起意——
        「每天最多主动找他几次」那套额度只管她自己想找人，不该卡用户配好的日程。
        """

        if depth > self.world.limits.max_action_chain_depth:
            outcome.notes.append("动作链超过深度上限，已停止")
            return False
        executed = False
        max_actions = self.world.limits.max_actions_per_message
        queued_actions = list(actions)[:max_actions]
        # 进入这一轮之前她自己没做完的安排：这一轮要排队时接在它们前面，而不是把它们顶掉。
        # 正在执行计划里的某一步时要跳过那一步，否则它会做第二遍。
        existing_plan = active_plan(state)
        # 她自己那一步还握在手上：那一步既不能再排一遍（会做两遍），
        # 也不能算进"还没做完的步骤"里
        running_index = self._running_plan_index(state, existing_plan)
        if running_index is not None:
            carried = self._plan_remaining_steps(existing_plan, after=running_index)
        else:
            carried = self._plan_remaining_steps(existing_plan, skip_current=from_plan)
        carried_reason = str((existing_plan or {}).get("reason") or "")
        carried_source = str((existing_plan or {}).get("source") or "")
        # 这一批是"有人跟她说话"（被动回复）还是她自己想做什么：
        # 排队久了以后，被动回复那几句不该被"主动发言冷却"当成刷屏吞掉
        batch_kind = "" if autonomous else "reply"
        # 这一批里有没有动作把她的"手头那件事"换掉：只有那种情况才需要排队——
        # 她原来就在做的事不该拦住"说一句话"这种瞬时动作，否则对方等的是几分钟后的回话
        started_here = False
        for index, action in enumerate(queued_actions):
            definition = self.world.action_map().get(action.type)
            if definition is None:
                outcome.notes.append(f"动作 {action.type} 不存在，已跳过")
                continue
            if not definition.enabled:
                # 停用 = 当作根本没有这个动作
                outcome.notes.append(f"动作 {action.type} 已停用，已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": action.type,
                        "note": f"「{definition.name or action.type}」已停用",
                    },
                    outcome=outcome,
                )
                continue
            if not definition.available_in(state.node_id):
                # A2 兜底：她想去别处做这件事，自己又没写移动 —— 插件带她过去
                target = (
                    self._nearest_node_with_action(state.node_id, definition)
                    if bool(self.world.remote_action_travel) and allow_remote_travel
                    else ""
                )
                if target:
                    await self._travel_then_continue(
                        state,
                        outcome,
                        definition,
                        target,
                        queued_actions[index:],
                        carried=carried,
                    )
                    return True
                outcome.notes.append(f"动作 {action.type} 在 {state.node_id} 不可用，已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {"action": action.type, "note": f"「{action.type}」在 {state.node_id} 不可用"},
                    outcome=outcome,
                )
                continue
            ok, reason = self._check_preconditions(state, definition)
            if not ok:
                outcome.notes.append(f"动作 {action.type} 前置条件不满足（{reason}），已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {"action": action.type, "note": f"「{action.type}」前置条件不满足：{reason}"},
                    outcome=outcome,
                )
                continue
            if definition.llm_level == "tool":
                # 主模型只给了"想干什么"，参数交给辅助模型按工具定义补全
                if not await self._prepare_tool_action(
                    state, definition, action, outcome
                ):
                    continue
            if self._is_slow_action(definition):
                # 「等我两分钟～」这类排在慢动作前面说的话：先发出去，再去做这件慢事
                await self._flush_say(outcome)
            if (
                autonomous
                and not from_schedule
                and action.target
                and str(getattr(definition, "target_type", "")) == "user"
                and bool(getattr(self.world.profile, "enabled", True))
            ):
                # 她**主动**对某个人做事（抱抱、戳一戳这些）受"这一级每天最多主动找他几次"限制：
                # 陌生 / 客气级别是 0 次（不主动动手），越亲近额度越大。
                view = self.profiles.view(state.session_id, action.target)
                left = self.profiles.proactive_quota_left(state.session_id, action.target)
                if left <= 0:
                    cap = getattr(getattr(view, "level", None), "proactive_per_day", 0)
                    outcome.notes.append(
                        f"主动找 {action.target} 的次数今天用完了（这一级最多 {cap} 次），跳过这一步"
                    )
                    await self._log_event(
                        state,
                        "skip",
                        {
                            "action": action.type,
                            "note": f"主动找 {action.target} 的今日额度用完了（这一级 {cap} 次）",
                        },
                        outcome=outcome,
                    )
                    continue
                self.profiles.note_proactive(
                    state.session_id, action.target, now=self._now()
                )
            before_action = state.current_action
            await self._start_action(
                state,
                node,
                outcome,
                definition,
                action,
                depth,
                autonomous,
                from_plan=from_plan,
                skip_drowsy=skip_drowsy,
            )
            if state.current_action is not before_action:
                # 她手头换成了这一批里的动作：后面的才需要排队等它做完
                started_here = True
            if state.state == STATE_DROWSY and state.drowsy_sleep_step:
                # 这一步没真做，被揣进临睡期了：得从计划里划掉，否则醒来还会再做一遍。
                # 同一串里排在它后面的动作接在睡觉之后，等她睡下再继续。
                if from_plan:
                    advance(state)
                rest = queued_actions[index + 1 :]
                if rest:
                    self._queue_steps(
                        state,
                        outcome,
                        rest,
                        carried,
                        kind=batch_kind,
                        note=(
                            f"她正要睡下，剩下的 {len(rest)} 个动作排在她睡下之后"
                        ),
                    )
                return True
            if action.type != "walk_to" and state.pending_arrival:
                # 走到这儿之后紧接着就有安排（同一串动作 / 计划里的下一步）：
                # 「落地后再问一次大模型」是给"只移动、没说到了做什么"兜底的，
                # 这里已经有要做的事，就别再花一次调用
                state.pending_arrival = False
                outcome.notes.append(
                    f"落地后已经有「{definition.name or definition.id}」，不再额外决策"
                )
            executed = True
            # 她开始做一个要花时间的动作时，后面的动作不能立刻抢着执行，
            # 更不能把它顶掉——转成计划，等她忙完再做。
            #
            # 「她本来就忙着」是另一回事：手上的事不是这一批开的，就不该拦住一句
            # 瞬时的话（那是对方正等着的回话，排到十几分钟后就没意义了）。
            if state.current_action is not None:
                rest = queued_actions[index + 1 :]
                if rest and started_here:
                    self._queue_steps(
                        state,
                        outcome,
                        rest,
                        carried,
                        kind=batch_kind,
                        note="",
                    )
                    note = (
                        f"她开始「{definition.name or definition.id}」，"
                        f"剩下的 {len(rest)} 个动作排队等它做完"
                    )
                    if carried:
                        note += f"；原来没做完的 {len(carried)} 步接在它们后面"
                    outcome.notes.append(note)
                    return True
                if not rest:
                    return True
                # 剩下的是她正等着的回话：这一批里继续做掉，不排队
        return executed

    @staticmethod
    def _is_slow_action(definition: ActionDef) -> bool:
        """这个动作会不会花上一会儿：要调工具 / 指令 / 模型，或者要持续一段时间。"""

        if str(getattr(definition, "category", "instant")) != "instant":
            return True
        return str(getattr(definition, "llm_level", "")) in ("tool", "command")

    async def _flush_say(self, outcome: TickOutcome) -> None:
        """慢动作开始前，把她已经说出口的话**当场发出去**。

        没有这一步，「坐好等我两分钟～」会等整段检索（含读正文、补查）跑完，
        和结果一起冒出来——群里看起来就是她半天不吭声、然后一口气说三句。
        发出去的那几句记进 ``live_messages``：收尾只发剩下的，日志与聊天记录里算她说过话。
        ``say`` 排在慢动作后面（本来就是"做完再说"）时不受影响。
        """

        if self.say_sink is None or not outcome.messages:
            return
        lines = outcome.take_messages()
        sent_count = 0
        for line in lines:
            try:
                sent = bool(await self.say_sink(outcome.home(), line))
            except Exception as exc:
                self._log("debug", f"即时发言失败：{exc}")
                sent = False
            if not sent:
                # 发不出去（会话没配 / 平台在冷却里）：从这里开始都留到收尾一起发，别丢
                break
            outcome.live_messages.append(line)
            sent_count += 1
        if sent_count >= len(lines):
            return
        outcome.messages = lines[sent_count:]
        # 回显的位置是"排在第几句之后"：前面几句已经当场发出去了，位置整体往前挪
        outcome.debug_positions = [
            max(0, int(position) - sent_count) for position in outcome.debug_positions
        ]

    async def _travel_then_continue(
        self,
        state: WorldState,
        outcome: TickOutcome,
        definition: ActionDef,
        target_node: str,
        pending: list[PlannedAction],
        *,
        carried: list[dict[str, Any]] | None = None,
    ) -> None:
        """她想去别处做某件事、自己又没写移动：先送她过去，剩下的排进计划。

        与规则决策器里的「先走再做」是同一套逻辑，只是这里替大模型补上那一步。
        """

        outcome.notes.append(
            f"她想做「{definition.name or definition.id}」，但当前地点做不了，"
            f"先带她去{self._node_name(target_node)}"
        )
        outcome.auto_travel.append(target_node)
        # 她把"说一句"排在移动后面时，这一轮会一个字都不发、还会被判定成接管失败。
        # 瞬时动作不占时间，先执行掉——用户立刻看到回应，剩下的等走过去再做。
        # 只有当前地点就做得成的才算：要换地方才做得成的那一步正是"为什么先走过去"。
        instant = []
        for item in pending:
            candidate = self.world.action_map().get(item.type)
            if candidate is None or candidate.category != "instant":
                continue
            if candidate.available_in(state.node_id):
                instant.append(item)
        for item in instant:
            await self._start_action(
                state,
                self.node(state.node_id),
                outcome,
                self.world.action_map()[item.type],
                item,
                0,
                False,
            )
        pending = [item for item in pending if item not in instant]
        if not pending:
            return
        await self._log_event(
            state,
            "plan",
            {
                "reason": f"为了做「{definition.name or definition.id}」先移动过去",
                "source": "auto_travel",
                "steps": [{"action": item.type} for item in pending],
            },
            outcome=outcome,
        )
        walk = PlannedAction(type="walk_to", target_node=target_node)
        await self._start_action(
            state, self.node(state.node_id), outcome, self.world.action_map()["walk_to"],
            walk, 0, False,
        )
        if state.current_action is None:
            return
        # 她自己原来没做完的安排接在后面：主人这一轮的要求先做，再回去做她自己的事
        tail = carried if carried is not None else self._plan_remaining_steps(active_plan(state))
        state.current_plan = create_plan(
            steps=[self._step_payload(item, outcome.home()) for item in pending] + tail,
            world_time=state.world_time,
            valid_for=self.world.limits.plan_valid_duration,
            reason=f"到了{self._node_name(target_node)}之后要做的事",
            source="auto_travel",
        )

    def _node_name(self, node_id: str) -> str:
        node = self.node(node_id)
        return (node.name or node.id) if node else node_id

    def _check_preconditions(self, state: WorldState, definition: ActionDef) -> tuple[bool, str]:
        pre = definition.preconditions
        if pre.not_state and state.state in pre.not_state:
            return False, f"不能在 {state.state} 状态执行"
        if pre.min_energy is not None and state.energy < pre.min_energy:
            return False, "精力不足"
        return True, ""

    def _tool_exists(self, name: str) -> bool:
        return name in self.available_tools()

    # ---------------- 工具熔断 ----------------
    #
    # 连续两次"工具本身不可用"（没注册 handler / 抛异常 / 超时）就临时不用它，
    # 退避 30 秒起、逐次翻倍、上限 30 分钟。参数缺失或返回空结果**不算**失败。

    TOOL_BREAK_AFTER = 2
    TOOL_BREAK_BASE_SECONDS = 30.0
    TOOL_BREAK_MAX_SECONDS = 1800.0

    def _tool_breaker(self, name: str) -> dict[str, Any]:
        return self._tool_failures.setdefault(
            str(name or ""), {"count": 0, "until": 0.0, "reason": ""}
        )

    def tool_unavailable_reason(self, name: str) -> str:
        """这个工具现在是不是被熔断了；返回剩余秒数的说明，空串表示可用。"""

        record = self._tool_failures.get(str(name or ""))
        if not record:
            return ""
        until = float(record.get("until") or 0.0)
        if until <= 0:
            return ""
        left = until - self._now()
        if left <= 0:
            # 退避到期：放一次试探（half-open），成功就会清零
            record["until"] = 0.0
            return ""
        return f"刚失败过，{int(left)} 秒后再试（{_clip_text(record.get('reason'), 60)}）"

    def tool_breaker_state(self) -> list[dict[str, Any]]:
        """当前被熔断的工具（编辑器里显示 + 手动解除用）。"""

        now = self._now()
        items: list[dict[str, Any]] = []
        for name, record in self._tool_failures.items():
            until = float(record.get("until") or 0.0)
            if until <= now:
                continue
            items.append(
                {
                    "tool": name,
                    "seconds_left": int(until - now),
                    "fails": int(record.get("count") or 0),
                    "reason": _clip_text(record.get("reason"), 80),
                }
            )
        return sorted(items, key=lambda item: item["seconds_left"], reverse=True)

    def reset_tool_breakers(self, name: str = "") -> int:
        """解除熔断：给名字就清这一个，留空清全部（配置一改就全清）。"""

        if name:
            record = self._tool_failures.pop(str(name), None)
            return 1 if record else 0
        count = len(self._tool_failures)
        self._tool_failures.clear()
        return count

    def note_tool_result(self, name: str, *, ok: bool, error: str = "") -> None:
        """记一次工具调用的结果，决定要不要熔断。"""

        key = str(name or "")
        if not key:
            return
        if ok:
            # 成功即清零：这也是"暂不可用"最主要的自动解除方式
            self._tool_failures.pop(key, None)
            return
        if not _looks_like_tool_broken(error):
            return
        record = self._tool_breaker(key)
        record["count"] = int(record.get("count") or 0) + 1
        record["reason"] = str(error or "")
        if record["count"] >= self.TOOL_BREAK_AFTER:
            backoff = self.TOOL_BREAK_BASE_SECONDS * (2 ** (record["count"] - self.TOOL_BREAK_AFTER))
            record["until"] = self._now() + min(self.TOOL_BREAK_MAX_SECONDS, backoff)

    def _tool_hint(self, limit: int = 8) -> str:
        """给「缺少工具」的报错补上"现在到底有哪些工具"，方便用户自己改配置。"""

        names = sorted(self.available_tools())
        if not names:
            return "（AstrBot 里当前没有注册任何工具）"
        shown = "、".join(names[:limit])
        if len(names) > limit:
            shown += f" 等 {len(names)} 个"
        return f"（当前可用工具：{shown}）"

    def resolve_tool(self, action_id: str, node_id: str = "", tool_name: str = "") -> str:
        """这个动作最终会用哪个工具。

        工具型动作必须显式指定工具：名字已注册、且不是「直发消息」类工具才算可用。
        """

        name = str(tool_name or "").strip()
        if not name:
            return ""
        if not self._tool_exists(name):
            return ""
        if is_self_send_tool(name):
            # 别静默跳过：被挡下来的如果是出图这类工具，图只能等别的路径补发，
            # 落点就跟正文分家了——至少留一条日志，排查时有迹可循
            self._log("debug", f"工具「{name}」是直发消息类，插件不调用它")
            return ""
        if self.tool_unavailable_reason(name):
            # 刚连续失败过：退避期内不再用它（到点会自动放行试探）
            return ""
        return name

    def resolve_tool_prefix(self, tool_name: str) -> str:
        """按前缀找一个装了的工具：``web_search`` 能对上官方的 ``web_search_tavily``。

        只用在"只配了一个工具"的动作上：用户明确挑了哪几个工具的多工具链不做替换。
        """

        wanted = str(tool_name or "").strip().lower()
        if len(wanted) < 5:
            return ""
        for installed in sorted(self.available_tools()):
            if is_self_send_tool(installed):
                continue
            if str(installed).lower().startswith(wanted):
                return str(installed)
        return ""

    def resolve_tools(self, definition: ActionDef, node_id: str = "") -> list[str]:
        """这个动作最终会用到的工具：配了几个就返回几个，按配置顺序。

        ``tool_names`` 一个都没装时才轮到 ``tool_fallbacks``（内置搜索 / 查天气用得上），
        那时只取备选里第一个装了的——同一个意思的工具没必要挨个调一遍。
        """

        names = definition.tool_list()
        result: list[str] = []
        for name in names:
            chosen = self.resolve_tool(definition.id, node_id, name)
            if chosen and chosen not in result:
                result.append(chosen)
        if result:
            return result
        # 只配了一个工具的动作允许按前缀找（官方搜索工具叫 web_search_tavily 这种）
        allow_prefix = len(names) <= 1
        if allow_prefix:
            for name in names:
                chosen = self.resolve_tool_prefix(name)
                if chosen:
                    return [chosen]
        for name in definition.tool_fallbacks or []:
            chosen = self.resolve_tool(definition.id, node_id, name) or (
                self.resolve_tool_prefix(name) if allow_prefix else ""
            )
            if chosen and chosen not in result:
                result.append(chosen)
                break
        return result

    async def fill_tool_params(
        self,
        state: WorldState,
        definition: ActionDef,
        action: PlannedAction,
        *,
        tool_name: str = "",
        params: dict[str, Any] | None = None,
        previous_results: str = "",
        error_hint: str = "",
        extra_rules: str = "",
    ) -> tuple[dict[str, Any], str]:
        """把「她想干什么」翻译成工具需要的参数字典。

        返回 ``(params, note)``：note 非空表示没补全（原因），调用方据此决定跳过还是硬着头皮调。
        相同工具 + 相同意图会命中缓存，不会反复请求。

        ``previous_results`` 用于一个动作挂了多个工具的场景：把它传给补全模型，
        后面的工具就能用上前一个工具查回来的内容（例如先搜到网址、再去抓正文）。
        ``error_hint`` 是上一次调用报的错：带着它再补一次，能救回"schema 写可选、
        实现却必须要"这类工具。
        """

        base = dict(params if params is not None else (action.params or {}))
        chosen = self.resolve_tool(
            definition.id, state.node_id, tool_name or definition.tool_name
        )
        if not chosen or self.helper_llm is None:
            return {}, ""
        missing = self.missing_params_for(chosen, base)
        unfilled = self.unfilled_params(chosen, base)
        if not missing and not unfilled and not previous_results and not error_hint:
            # 已经齐了就不用问模型；但带了"上一个工具的结果"时是有意重算，必须再问一次
            return {}, ""
        intent = (action.intent or action.content or "").strip()
        if not intent:
            # 日程里的步骤没有"想干什么"这一栏：用动作自己的说明兜一句，
            # 否则这条工具动作到点就只会被跳过。
            intent = self.fallback_intent(definition)
        cache_key = (chosen, intent)
        # 带了上一个工具的结果、或者带了报错时都是"有意重算"，不吃缓存
        cached = (
            None
            if (previous_results or error_hint)
            else self._filled_params.get(cache_key)
        )
        if cached:
            return dict(cached), ""
        if not self._tool_param_allowed(state):
            return {}, "本小时的工具参数补全次数已用完"

        schema = self.tool_schemas().get(chosen) or {}
        description = self.available_tools().get(chosen, "")
        system_prompt, prompt = self.prompts.build_tool_params_prompt(
            tool_name=chosen,
            tool_description=description,
            param_text=render_param_text(schema),
            intent=intent,
            recent_chat=self.chat_window(state),
            previous_results=previous_results,
            error_hint=error_hint,
            extra_rules=extra_rules,
        )
        reply = await self._ask_judge(
            state.session_id, system_prompt, prompt
        )
        self._count_tool_param(state)
        if reply is None:
            return {}, "参数补全模型调用失败"
        payload = extract_json_object(reply)
        if not isinstance(payload, dict):
            return {}, "参数补全模型没有返回合法 JSON"
        filled = {
            str(key): value
            for key, value in payload.items()
            if not str(key).startswith("_")
        }
        if not filled:
            return {}, "参数补全模型没有给出参数"
        given = {k: v for k, v in base.items() if v not in ("", None)}
        # 平时以调用方给的参数为准；带上了上一个工具的结果时，以模型新给的为准（覆盖之前猜的）
        merged = {**given, **filled} if previous_results else {**filled, **given}
        still_missing = self.missing_params_for(chosen, merged)
        if still_missing:
            return {}, f"仍缺少参数 {still_missing}"
        if not previous_results:
            self._filled_params[cache_key] = dict(merged)
        if len(self._filled_params) > 200:
            self._filled_params.clear()
        return merged, ""

    async def _ask_helper(
        self, session_id: str, system_prompt: str, prompt: str
    ) -> str | None:
        if self.helper_llm is None:
            return None
        try:
            reply = await self.helper_llm.generate(
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.0,
            )
        except Exception as exc:
            self._log("debug", f"参数补全调用失败：{exc}")
            return None
        return reply.text if getattr(reply, "ok", False) else None

    async def _ask_judge(
        self, session_id: str, system_prompt: str, prompt: str
    ) -> str | None:
        """纯判断通道（挑一个 / 打分 / 抽字段）：走判断模型，没配就跟随打杂模型。

        判断模型不可用或报错时**自动退回打杂模型**——轻决策不该把主流程拖挂。
        """

        if self.judge_llm is not None and self.judge_llm is not self.helper_llm:
            try:
                reply = await self.judge_llm.generate(
                    session_id=session_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    temperature=0.0,
                )
                if getattr(reply, "ok", False) and str(getattr(reply, "text", "") or "").strip():
                    return reply.text
                self._log("debug", "判断模型没给出内容，这一轮退回打杂模型")
            except Exception as exc:
                self._log("debug", f"判断模型调用失败（{exc}），退回打杂模型")
        return await self._ask_helper(session_id, system_prompt, prompt)

    async def _ask_event(
        self, session_id: str, system_prompt: str, prompt: str
    ) -> str | None:
        """事件结算的模型通道（独立配置，留空跟随打杂模型）。

        它写的是"这件事最后怎么样了"——一句话定下场，还带着情绪与能力值的变化。
        这一段最吃"会不会写人话"，所以单独留了槽位。
        """

        if self.event_llm is not None and self.event_llm is not self.helper_llm:
            try:
                reply = await self.event_llm.generate(
                    session_id=session_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    temperature=0.4,
                )
                if getattr(reply, "ok", False) and str(getattr(reply, "text", "") or "").strip():
                    return reply.text
                self._log("debug", "事件模型没给出内容，这一轮退回打杂模型")
            except Exception as exc:
                self._log("debug", f"事件模型调用失败（{exc}），退回打杂模型")
        return await self._ask_helper(session_id, system_prompt, prompt)

    async def _ask_consolidate(
        self, session_id: str, system_prompt: str, prompt: str
    ) -> str | None:
        """睡眠整理的模型通道（独立配置，留空回落到辅助模型）。"""

        channel = self.consolidate_llm
        if channel is None:
            return None
        try:
            reply = await channel.generate(
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.4,
            )
        except Exception as exc:
            self._log("debug", f"睡眠整理调用失败：{exc}")
            return None
        return reply.text if getattr(reply, "ok", False) else None

    def _tool_param_allowed(self, state: WorldState) -> bool:
        hour = int(self._now() // 3600)
        if state.tool_param_hour_marker != hour:
            state.tool_param_hour_marker = hour
            state.tool_param_count_hour = 0
        return state.tool_param_count_hour < int(self.world.limits.max_tool_param_per_hour)

    @staticmethod
    def _count_tool_param(state: WorldState) -> None:
        state.tool_param_count_hour = int(state.tool_param_count_hour) + 1

    @staticmethod
    def _act_get(action: Any, key: str, default: Any = None) -> Any:
        """待执行动作有两种形态（PlannedAction / 运行中的 dict），统一读法。"""

        if isinstance(action, dict):
            return action.get(key, default)
        return getattr(action, key, default)

    @staticmethod
    def _act_set(action: Any, key: str, value: Any) -> None:
        if isinstance(action, dict):
            action[key] = value
        else:
            setattr(action, key, value)

    async def _prepare_tool_action(
        self,
        state: WorldState,
        definition: ActionDef,
        action: Any,
        outcome: TickOutcome,
    ) -> bool:
        """工具型动作动手前的准备：确认工具可用，并把每个工具的参数补齐。

        返回 ``False`` 表示这次跑不了（跳过原因已经记进日志）。补好的参数写在
        ``action.tool_params``（工具名 -> 参数字典），执行时按顺序逐个调用。
        """

        names = self.resolve_tools(definition, state.node_id)
        if not names:
            picked = "、".join(definition.tool_candidates())
            if not picked:
                reason = "工具型动作必须选一个工具，这个动作还没选"
            elif is_self_send_tool(definition.tool_list()[0] if definition.tool_list() else ""):
                reason = (
                    f"「{definition.tool_list()[0]}」是直发消息的工具，"
                    "插件不会调用（会绕过回复管线）"
                )
            else:
                reason = f"选的工具「{picked}」在 AstrBot 里没注册{self._tool_hint()}"
            outcome.notes.append(f"工具动作 {definition.id} 找不到可用工具，已跳过")
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": f"工具动作「{definition.name or definition.id}」找不到可用工具：{reason}",
                },
                outcome=outcome,
            )
            return False

        # 用户在动作里配好的固定参数（例如查天气固定 city=武汉）先铺底，
        # 大模型这一轮写出来的参数覆盖在它上面。
        base = self._action_base_params(definition, action)
        if str(getattr(definition, "tool_flow", "simple")) == "search":
            # 检索型动作不在这一步补参数：查询词和参数在真正调用时按查询逐条生成
            # （见 ``_run_search_flow``），这里只把这一轮要查什么定下来。
            self._act_set(action, "queries", self._explicit_queries(definition, action))
            self._act_set(action, "tool_params", {})
            self._act_set(action, "params", dict(base))
            return True
        stored: dict[str, dict[str, Any]] = {}
        for index, name in enumerate(names):
            # 大模型写出来的参数只当第一个工具的，其余工具由辅助模型按各自定义补
            params = dict(base) if index == 0 else {}
            missing = self.missing_params_for(name, params)
            unfilled = self.unfilled_params(name, params)
            note = ""
            if missing or unfilled:
                filled, note = await self.fill_tool_params(
                    state, definition, action, tool_name=name, params=params
                )
                if filled:
                    params = {**params, **filled}
                missing = self.missing_params_for(name, params)
            if missing:
                outcome.notes.append(
                    f"工具动作 {definition.id} 缺少必填参数 {missing}，已跳过"
                    + (f"（{note}）" if note else "")
                )
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": definition.id,
                        "note": (
                            f"工具动作「{definition.id}」缺少必填参数 {missing}"
                            + (f"：{note}" if note else "")
                        ),
                    },
                    outcome=outcome,
                )
                return False
            stored[name] = params

        self._act_set(action, "tool_params", stored)
        self._act_set(action, "params", dict(stored.get(names[0]) or {}))
        return True

    # ---------------- 日程：查看 / 添加 / 删除（内置动作与对外工具共用） ----------------

    async def _run_command_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
        event: Any = None,
        *,
        out: dict[str, Any] | None = None,
        speak: bool = True,
        cached: dict[str, Any] | None = None,
        remember: dict[str, Any] | None = None,
    ) -> None:
        """指令触发：把意图拼成一条指令，交给 AstrBot 去执行，再把结果交回给她。

        有真实消息事件时直接用那条事件（图片、引用都跟着走）；
        自主触发时借用这个会话最近一条事件——所以在这种情况下没有图片。

        ``speak=False`` 只执行不说话，把结果写进 ``out``；``cached`` 反过来——
        指令早就跑过了，这里只负责把当初的结果交回给她。
        """

        command = str(definition.trigger_command or "").strip()
        if not command:
            outcome.notes.append(f"动作 {definition.id} 没有配置要触发的指令，已跳过")
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": "指令型动作没有配置要触发的指令"},
                outcome=outcome,
            )
            return
        intent = (action.intent or action.content or "").strip()
        if not intent:
            # 日程里的步骤没有"想干什么"这一栏：用动作自己的说明兜一句
            intent = self.fallback_intent(definition)

        if cached is None:
            line = await self._compose_command(state, definition, command, intent)
            if self.commands is None:
                outcome.notes.append("没有可用的指令通道，已跳过")
                return
            await self._log_event(
                state,
                "command_call",
                {"action": definition.id, "command": line, "intent": intent},
                outcome=self._echo_into(outcome, state),
            )
            # 指令在"她现在说话的地方"触发：私聊里让她拍照，指令就带着私聊的上下文跑
            # 落点当场定下来，图和正文都用它——不然私聊里起的动作会把图发到群里
            landing = self._image_home(outcome, action)
            call = await self.commands.trigger(landing, line)
            images = list(getattr(call, "image_urls", None) or [])
            # 指令生成出来的图（多张也算）贴着正文一起发，别只交回给她自己看
            attachments = list(getattr(call, "attachments", None) or [])
            self._attach_images(outcome, attachments, session_id=landing)
            if attachments:
                self._note_own_image(
                    state,
                    intent or definition.name or definition.id,
                    landing,
                )
            await self._log_event(
                state,
                "command_result",
                {
                    "action": definition.id,
                    "command": line,
                    "ok": bool(call.ok),
                    "result": _clip_log(call.text),
                    "images": len(images),
                    "error": call.error,
                },
                outcome=self._echo_into(outcome, state),
            )
            outcome.notes.append(
                f"触发指令「{line}」：" + ("成功" if call.ok else f"失败（{call.error}）")
            )
            if (
                definition.id == "check_weather"
                and call.ok
                and str(call.text or "").strip()
            ):
                # 查天气配成「指令型」时，结果同样要进全局天气记录
                await self._store_weather(
                    state,
                    definition,
                    {"tool_result": call.text, "tool_images": images},
                    source="manual",
                )
            payload = {
                "line": line,
                "ok": bool(call.ok),
                "text": str(call.text or ""),
                "images": images,
                "error": str(call.error or ""),
            }
            ok = payload["ok"]
            if remember is not None:
                # 让调用方拿到结果（状态槽、续说都要用）
                remember["tool_result"] = payload["text"]
                remember["tool_ok"] = payload["ok"]
                remember["tool_error"] = payload["error"]
                if images:
                    remember["tool_images"] = list(images)
            if out is not None:
                out.update(payload)
            if not speak:
                # 只执行不说话：结果先留着，等这一步真的做完再交回给她
                return
            detail = (
                payload["text"]
                if payload["ok"]
                else f"（这条指令没跑成：{payload['error']}）"
            )
        else:
            line = str(cached.get("line") or "")
            ok = bool(cached.get("ok"))
            images = list(cached.get("images") or [])
            detail = (
                str(cached.get("text") or "")
                if ok
                else f"（这条指令没跑成：{cached.get('error') or ''}）"
            )
        detail = self._clip_followup(detail)
        if not detail and images:
            detail = "这条指令返回了一张图片，图片一起发给你了。"

        # 说完不说一句，看动作里的「完成后」和全局的「工具结果回话」——
        # 与工具型动作同一套规则，不然用户想让她执行完指令别吭声也关不掉。
        trigger = str(definition.on_complete.trigger or "none")
        hint = str(definition.on_complete.prompt_hint or "").strip()
        want_followup = trigger == "llm_followup"
        if (
            not want_followup
            and trigger == "none"
            and bool(self.world.tool_result_reply)
            and (detail or images)
        ):
            want_followup = True
        if not want_followup:
            outcome.notes.append(f"指令「{line}」已执行，这次不开口")
            return
        if not hint:
            hint = (
                "用你自己的话把这条指令返回的内容讲一句，别照抄格式、别编。"
                if ok
                else "这条指令没跑成，用一句自然的话说明一下，别编内容。"
            )
        await self._llm_followup(
            state,
            node,
            outcome,
            hint,
            detail or "（没有返回内容）",
            image_urls=images,
        )

    async def _compose_command(
        self,
        state: WorldState,
        definition: ActionDef,
        command: str,
        intent: str,
    ) -> str:
        """把意图拼成一条完整指令；补不出参数就退回指令名本身。"""

        base = command if command.startswith("/") else f"/{command}"
        if self.helper_llm is None or not self._tool_param_allowed(state):
            return base
        system_prompt, prompt = self.prompts.build_command_prompt(
            command=base,
            hint=str(definition.trigger_hint or ""),
            intent=intent,
            # 出图类指令的参数就是一段画面描述：得让她知道此刻在哪儿、穿着什么，
            # 缺的那几项才能按现状补齐（而不是留空或者瞎编）
            state_text=self._compose_state_text(state),
        )
        reply = await self._ask_judge(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        text = " ".join(str(reply or "").split()).strip().strip("`")
        if not text:
            return base
        # 补参模型偶尔会回一坨 JSON 或一整句话：那不是指令，宁可只发指令名本身，
        # 也别往群里发一条 `/{"actions":...}` 这种莫名其妙的东西。
        if any(mark in text for mark in ('{', '}', '[', ']', '"', "'")):
            self._log(
                "debug",
                f"指令参数补全返回的内容不像指令，已退回指令名：{_clip_text(text, 80)}",
            )
            return base
        return text if text.startswith("/") else f"/{text}"

    def _compose_state_text(self, state: WorldState) -> str:
        """拼指令参数时给她「此刻是什么样」：地点、手头的事、心情、状态槽。

        出图 / 出视频这类指令的参数就是一段画面描述，光有意图写不出完整画面
        （服装、环境、光线都在她当前的状态里）。这里只给事实，不替她编。
        """

        lines: list[str] = []
        node = self.node(state.node_id) or self.node(self.default_node_id())
        if node is not None:
            scene = " ".join(str(getattr(node, "prompt", "") or "").split())
            line = f"- 地点：{node.name or node.id}"
            if scene:
                line += f"——{_clip_text(scene, 90)}"
            lines.append(line)
        current = state.current_action if isinstance(state.current_action, dict) else None
        current_type = str((current or {}).get("type") or "")
        if current_type:
            lines.append(f"- 手头的事：{self.prompts.action_label(current_type)}")
        if str(state.mood or "").strip():
            lines.append(f"- 心情：{state.mood}")
        now = self._now()
        for slot, info in dict(getattr(state, "external_state", None) or {}).items():
            if not isinstance(info, dict):
                continue
            text = " ".join(str(info.get("text") or "").split())
            if not text:
                continue
            try:
                expires = float(info.get("expires_at") or 0.0)
            except (TypeError, ValueError):
                expires = 0.0
            if expires and now > expires:
                continue
            label = str(info.get("label") or slot).strip()
            lines.append(f"- {label}：{_clip_text(text, 140)}")
        return "\n".join(lines)

    SCHEDULE_ACTION_IDS = ("schedule_list", "schedule_add", "schedule_remove")

    def schedule_text(self) -> str:
        """把所有日程渲染成一行行给模型看的文本。"""

        items = list(getattr(self.schedules, "schedules", None) or [])
        if not items:
            return "（现在没有任何日程）"
        lines = []
        for item in items:
            chain = " → ".join(step.type for step in (item.action_chain or []) if step.type)
            days = "/".join(item.days or [])
            who = "她自己加的" if str(getattr(item, "created_by", "") or "") == "bot" else "用户配的"
            state = "启用" if item.enabled else "停用"
            when = f"{item.date} " if (item.once and item.date) else ""
            line = (
                f"- {item.id}｜{when}{item.time}｜{days}｜{chain or '（空）'}｜{state}｜{who}"
                + ("｜只做这一次" if item.once else "")
                + ("｜到点自动先走过去" if item.auto_travel else "")
            )
            landed = [
                str(value) for value in (getattr(item, "sessions", None) or []) if str(value)
            ]
            if landed:
                line += "｜落点：" + "、".join(
                    self.session_label(value) if ":" in value else f"会话组 {value}"
                    for value in landed
                )
            else:
                line += "｜落点：每个会话各自跑"
            note = " ".join(str(getattr(item, "note", "") or "").split())
            if note:
                line += f"｜当初为什么排：{note}"
            lines.append(line)
        return "\n".join(lines)

    async def schedule_add(
        self, payload: dict[str, Any], *, session_id: str = ""
    ) -> tuple[bool, str]:
        """加一条她自己安排的日程（校验时间、星期、动作链）。"""

        raw = self.store.raw_schedules()
        items = list(raw.get("schedules") or [])
        time_text = " ".join(str(payload.get("time") or "").split())
        # 常见写法都收："7:05" / "07:5" / "7:5" 统一成 HH:MM
        if not re.match(r"^\d{1,2}:\d{1,2}$", time_text):
            return False, f"时间「{time_text or '（空）'}」看不懂，要写成 HH:MM"
        hour, minute = (int(part) for part in time_text.split(":"))
        if hour > 23 or minute > 59:
            return False, f"时间「{time_text}」不对"
        time_text = f"{hour:02d}:{minute:02d}"

        days = [
            str(day)
            for day in (payload.get("days") or [])
            if str(day) in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        ]
        if not days:
            days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

        chain: list[dict[str, Any]] = []
        for step in payload.get("action_chain") or []:
            if not isinstance(step, dict):
                continue
            action_id = str(step.get("type") or step.get("action") or "").strip()
            definition = self.world.action_map().get(action_id)
            if definition is None or not definition.enabled:
                return False, f"动作「{action_id or '（空）'}」不存在或已停用，这条日程没加成"
            if action_id in self.SCHEDULE_ACTION_IDS:
                # 日程里再去排日程是个死循环，只会到点报错
                return False, "日程里不能再排日程，换个到点要做的动作"
            item: dict[str, Any] = {"type": action_id}
            for key in ("target_node", "target", "content", "duration", "messages", "params"):
                if step.get(key):
                    item[key] = step[key]
            chain.append(item)
        if not chain:
            return False, "日程里至少要有一个动作"
        if len(chain) > max(1, int(self.world.limits.max_action_chain_depth) + 2):
            return False, "日程里的动作太多了，拆成两条吧"

        schedule_id = str(payload.get("id") or "").strip()
        if not schedule_id:
            schedule_id = f"bot_{int(self._now())}"
        if any(str(item.get("id")) == schedule_id for item in items):
            return False, f"已经有一条叫「{schedule_id}」的日程了"

        # 一次性日程：只在那一天跑一遍，跑完就删。日期写不出来就退化成「每天」，
        # 免得模型给个看不懂的日期之后这条日程永远不会到点。
        once = bool(payload.get("once"))
        date_text = str(payload.get("date") or "").strip()
        if once and date_text:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
                once, date_text = False, ""
            else:
                try:
                    datetime.strptime(date_text, "%Y-%m-%d")
                except ValueError:
                    once, date_text = False, ""
        note = " ".join(str(payload.get("note") or "").split())[:120]
        # 落点：她可能写的是会话名 / 群号 / 备注，也可能直接写会话组 id
        landed = self.resolve_session_refs(payload.get("sessions"), session_id=session_id)

        items.append(
            {
                "id": schedule_id,
                "enabled": True,
                "time": time_text,
                "days": days,
                "action_chain": chain,
                "once": once,
                "date": date_text if once else "",
                "note": note,
                "auto_travel": bool(payload.get("auto_travel", True)),
                "conditions": dict(payload.get("conditions") or {}),
                "sessions": landed,
                "priority": 5,
                "created_by": "bot",
            }
        )
        self.store.save_schedules(
            {**raw, "schedules": items}, reason="她给自己加了日程"
        )
        self.reload_config()
        names = " → ".join(
            (self.world.action_map().get(step["type"]).name or step["type"])
            if self.world.action_map().get(step["type"])
            else step["type"]
            for step in chain
        )
        when = f"{date_text} {time_text}（只做这一次）" if once else f"{time_text} 的日程"
        return True, f"{when}加好了：{names}"

    def _drop_schedule(self, schedule_id: str) -> bool:
        """按 id 摘掉一条日程（一次性日程跑完就用这个收尾）。"""

        raw = self.store.raw_schedules()
        items = list(raw.get("schedules") or [])
        kept = [item for item in items if str(item.get("id")) != str(schedule_id)]
        if len(kept) == len(items):
            return False
        self.store.save_schedules({**raw, "schedules": kept}, reason="日程跑完了回收")
        self.reload_config()
        return True

    def _prune_once_schedules(self, day_key: str) -> None:
        """日期已经过去的一次性日程留着没用，顺手清掉（每天只扫一次）。"""

        if day_key == self._once_pruned_day:
            return
        self._once_pruned_day = day_key
        raw = self.store.raw_schedules()
        items = list(raw.get("schedules") or [])
        kept = [
            item
            for item in items
            if not (
                item.get("once")
                and str(item.get("date") or "")
                and str(item.get("date")) < day_key
            )
        ]
        if len(kept) == len(items):
            return
        self.store.save_schedules(
            {**raw, "schedules": kept}, reason="清理过期的一次性日程"
        )
        self.reload_config()

    async def schedule_remove(self, selector: dict[str, Any]) -> tuple[bool, str]:
        """删掉她自己加的一条日程（用户配的动不了）。"""

        raw = self.store.raw_schedules()
        items = list(raw.get("schedules") or [])
        target_id = str(selector.get("id") or "").strip()
        time_text = str(selector.get("time") or "").strip()
        keyword = str(selector.get("keyword") or "").strip()
        matched = None
        for item in items:
            if target_id and str(item.get("id")) == target_id:
                matched = item
                break
            if time_text and str(item.get("time")) == time_text:
                matched = item
                break
            if keyword:
                blob = " ".join(
                    [str(item.get("id") or ""), str(item.get("time") or "")]
                    + [str(step.get("type") or "") for step in item.get("action_chain") or []]
                )
                if keyword in blob:
                    matched = item
                    break
        if matched is None:
            return False, "没找到那条日程"
        if str(matched.get("created_by") or "") != "bot":
            return False, "那条是用户自己配的日程，我删不了"
        items = [item for item in items if item is not matched]
        self.store.save_schedules(
            {**raw, "schedules": items}, reason="她自己删了一条日程"
        )
        self.reload_config()
        return True, f"删掉了 {matched.get('time')} 那条日程"

    async def _run_schedule_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
    ) -> None:
        """日程三件套。

        「提醒类」日程直接在代码里拼好（模型只要给时间 + 到时候说什么），
        复杂的（到点做一串动作）才交给辅助模型解析——那条路会改写她的原话，
        一条单纯的提醒没必要绕一圈。
        """

        op = {
            "schedule_list": "list",
            "schedule_add": "add",
            "schedule_remove": "remove",
        }.get(definition.id, "list")
        intent = (action.intent or action.content or "").strip()
        payload: dict[str, Any] = {}
        if op in ("add", "remove"):
            if not intent:
                await self._log_event(
                    state,
                    "schedule_edit",
                    {"op": op, "ok": False, "note": "没有说清要做什么"},
                    outcome=self._echo_into(outcome, state),
                )
                outcome.notes.append("日程动作没有给出意图，已跳过")
                return
            payload = None
            if op == "add":
                payload = self._schedule_from_action(action, intent)
            if payload is None:
                payload = await self._parse_schedule_intent(state, op, intent)

        if op == "list":
            ok, note = True, self.schedule_text()
        elif op == "add":
            ok, note = await self.schedule_add(payload, session_id=state.session_id)
        else:
            ok, note = await self.schedule_remove(payload)

        await self._log_event(
            state,
            "schedule_edit",
            {"op": op, "ok": bool(ok), "note": note, "raw": payload},
            outcome=self._echo_into(outcome, state),
        )
        outcome.notes.append(f"日程 {op}：{note}")
        hint = (
            "用一句自然的话说说你刚安排的这件事（或者刚刚看到的日程），别念清单。"
            if ok
            else "刚才那件事没办成，用一句自然的话说明一下，别编。"
        )
        await self._llm_followup(state, node, outcome, hint, note)

    @staticmethod
    def _param_lines(value: Any) -> list[str]:
        """把模型给的台词参数（字符串 / 列表）整理成一行行。"""

        if isinstance(value, str):
            items: list[Any] = [value]
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            return []
        lines: list[str] = []
        for item in items:
            text = " ".join(str(item or "").split())
            if text:
                lines.append(text)
        return lines[:MAX_SAY_LINES_HARD]

    def _when_payload(
        self, target: datetime, base: datetime, *, recurring: bool
    ) -> dict[str, Any]:
        """把"什么时候"整理成日程字段：一次性写 once + date，循环的只写 time。"""

        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        once = not recurring
        return {
            "time": target.strftime("%H:%M"),
            "days": list(days),
            "once": once,
            "date": target.strftime("%Y-%m-%d") if once else "",
        }

    def _parse_when_text(self, text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        """从一句话里读「什么时候」：相对时间（十分钟后）或绝对时刻（21:30 / 晚上八点）。

        读不出来返回 ``None``，调用方会回落到辅助模型。
        """

        body = " ".join(str(text or "").split())
        if not body:
            return None
        base = now if isinstance(now, datetime) else self.local_now()
        recurring = any(
            word in body for word in ("每天", "天天", "每晚", "每早", "每周", "每周一")
        )
        relative = _RELATIVE_WHEN.search(body)
        if relative:
            amount = _duration_number(relative.group("num"))
            if amount:
                unit = relative.group("unit") or ""
                minutes = int(round(amount * (60.0 if unit in ("小时", "钟头") else 1.0)))
                if minutes > 0:
                    return self._when_payload(
                        base + timedelta(minutes=minutes), base, recurring=recurring
                    )
        absolute = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", body)
        if absolute:
            hour, minute = int(absolute.group(1)), int(absolute.group(2))
            if hour <= 23 and minute <= 59:
                target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if "明天" in body or target <= base:
                    target += timedelta(days=1)
                return self._when_payload(target, base, recurring=recurring)
        spoken = _spoken_hour(body)
        if spoken is not None:
            hour, minute = spoken
            target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if "明天" in body or target <= base:
                target += timedelta(days=1)
            return self._when_payload(target, base, recurring=recurring)
        return None

    def _schedule_from_action(
        self, action: PlannedAction, intent: str
    ) -> dict[str, Any] | None:
        """「提醒类」日程：时间 + 到时候说的话都在模型给的参数里，直接拼，不用辅助模型。

        少一次模型调用，也不会把她的原话改写走样（"提醒她喝水"被写成
        "提醒自己不要喝水"这种事就是这么来的）。拼不出来返回 None。
        """

        params = dict(action.params or {})
        when = self._parse_when_text(
            str(params.get("at") or params.get("when") or params.get("time") or "")
            or intent
        )
        if not when:
            return None
        lines = self._param_lines(
            params.get("say")
            or params.get("messages")
            or params.get("content")
            or params.get("text")
        )
        if not lines:
            return None
        payload: dict[str, Any] = {
            **when,
            "action_chain": [{"type": "say", "messages": lines}],
            "note": intent,
            "auto_travel": False,
        }
        sessions = params.get("sessions")
        if sessions:
            payload["sessions"] = (
                sessions if isinstance(sessions, list) else [sessions]
            )
        return payload

    async def _parse_schedule_intent(
        self, state: WorldState, op: str, intent: str
    ) -> dict[str, Any]:
        """让辅助模型把「每天七点查新闻」翻成结构化日程参数。"""

        if self.helper_llm is None or not self._tool_param_allowed(state):
            return {"raw": intent}
        action_lines = [
            f"- {item.id}：{item.name or item.id}"
            for item in self.world.actions
            if item.enabled
        ]
        system_prompt, prompt = self.prompts.build_schedule_action_prompt(
            op=op,
            intent=intent,
            actions=action_lines,
            current=self.schedule_text(),
            now=self.local_now(),
            sessions=self.session_directory(state),
        )
        reply = await self._ask_judge(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        payload = extract_json_object(reply) if reply else None
        return payload if isinstance(payload, dict) else {"raw": intent}

    async def _run_recall(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        action: PlannedAction,
    ) -> None:
        """主动回想：把「想回忆什么」翻成检索条件，翻记忆，再带着结果问她一次。"""

        intent = (action.intent or action.content or "").strip()
        if not intent and action.target_node:
            intent = f"回忆一下{self._node_name(action.target_node)}里的事"
        if not intent:
            outcome.notes.append("回想没有说要回忆什么，已跳过")
            await self._log_event(
                state, "recall_start", {"intent": "", "note": "没有说要回忆什么"}
            )
            return

        query = await self._parse_recall_query(state, intent, action.target_node)
        await self._log_event(
            state,
            "recall_start",
            {"intent": intent, "query": query},
            outcome=self._echo_into(outcome, state),
        )

        limit = max(1, min(5, int(query.get("limit") or 3)))
        memories = self.memory.recall(
            session_id=state.session_id,
            persona_id="",
            nodes=list(query.get("nodes") or []),
            keyword=str(query.get("keyword") or ""),
            memory_type=str(query.get("type") or ""),
            mode=(self.world.memory_scope_mode if self.world else None),
            limit=limit,
            now=self._now(),
        )
        # 「回想一下关于主人的事」：她点名了某个人时，优先给跟这个人有关的记忆
        focus = self._recall_focus_user(state, f"{intent} {query.get('keyword') or ''}")
        if focus:
            related = [
                item
                for item in memories
                if focus in [str(entry) for entry in (item.participants or [])]
                or focus in [str(entry) for entry in (item.related_users or [])]
            ]
            if related:
                memories = related
        try:
            stamp_now = self.local_now()
        except Exception:
            stamp_now = None
        detail = "\n".join(
            item.render(stamp=self.prompts._memory_stamp(item.created_at, stamp_now))
            for item in memories
        )
        keyword = str(query.get("keyword") or "")
        where = "、".join(query.get("node_names") or []) or "任何地方"
        await self._log_event(
            state,
            "recall_done",
            {
                "count": len(memories),
                "keyword": keyword,
                "where": where,
                "detail": detail or "什么都没想起来",
            },
            outcome=self._echo_into(outcome, state),
        )

        hint = (
            "把你想起来的这些用第一人称说一句（可以带一点当时的情绪），别逐条念、别编新的细节。"
            if detail
            else "这次什么都没想起来——照实说一句「想不起来」就好，不要编。也可以只输出 think 或空动作列表。"
        )
        if detail and bool(getattr(self.world.profile, "enabled", True)):
            # 翻旧账时如果发现"关于他的事 / 你们的关系已经变了"，允许她当场用 remember 更新
            hint += (
                "如果翻出来的旧事里，关于某个人的信息或者你们的关系**已经和现在不一样了**"
                "（称呼变了、喜好变了、约定作废了、亲疏变了），可以顺手写一个 remember "
                "把新的情况改掉（记得带上他的原话）；没有变化就不用写。"
            )
        await self._llm_followup(
            state, node, outcome, hint, detail or "（翻了一遍记忆，什么都没想起来）"
        )

    async def _run_remember(
        self, state: WorldState, outcome: TickOutcome, action: PlannedAction
    ) -> None:
        """内置动作「记住」：把这一刻值得记的事当场写进通讯录。

        走的是和睡眠整理**同一套写入**（``ProfileStore``）与同一套校验：

        - 没带"他的原话"（evidence）一律不记——最容易编的就是这一类；
        - 只认她**真的认识的人**（会话里有档案的），免得顺着昵称编出一个号；
        - 事实同类去重、正反冲突自动替换；关系冲突按配置策略处理
          （默认记成"他自称"，唯一槽位全组一份）；同类关系只留一条。
        """

        store = getattr(self, "profiles", None)
        params = dict(action.params or {})
        user_id = str(
            params.get("user") or params.get("user_id") or action.target or ""
        ).strip()
        text = " ".join(str(params.get("text") or "").split())
        evidence = " ".join(str(params.get("evidence") or "").split())
        kind = str(params.get("kind") or "近况").strip()
        bond = str(params.get("bond") or "").strip()
        delta = params.get("affinity_delta")
        notes: list[str] = []
        if store is None or not bool(getattr(self.world.profile, "enabled", True)):
            outcome.notes.append("通讯录功能没开，这一条不记")
            return
        if not user_id:
            outcome.notes.append("「记住」没写清是记谁的，已跳过")
            await self._log_event(
                state,
                "skip",
                {"action": "remember", "note": "没有指定是记谁的"},
                outcome=outcome,
            )
            return
        known = {str(row.get("user_id") or "") for row in store.list_people(state.session_id, limit=500)}
        if user_id not in known:
            # 只认她真的见过的人：编出来的号码不进通讯录
            outcome.notes.append(f"「记住」里那个人（{user_id}）她还没见过，已跳过")
            await self._log_event(
                state,
                "skip",
                {"action": "remember", "note": f"材料里没这个人：{user_id}"},
                outcome=outcome,
            )
            return
        if not text and not bond and delta in (None, ""):
            outcome.notes.append("「记住」没说清要记什么，已跳过")
            return
        if (text or bond) and not evidence:
            outcome.notes.append("没带上他的原话，这一条不记（免得编）")
            await self._log_event(
                state,
                "skip",
                {"action": "remember", "note": "没带原话，事实 / 关系不记"},
                outcome=outcome,
            )
            return
        written: list[str] = []
        if text:
            result = store.note_fact(
                state.session_id,
                user_id,
                text=text,
                kind=kind,
                evidence=evidence,
                context="她当场记住的",
                confidence=0.9,
                source_session=state.session_id,
            )
            if result.get("ok"):
                written.append(f"记住了「{text}」（{kind}）")
            else:
                notes.append(str(result.get("reason") or "事实没记下"))
        if bond:
            result = store.note_bond(
                state.session_id,
                user_id,
                type=bond,
                evidence=evidence,
                confidence=0.9,
                asserted_by="她的判断",
            )
            if result.get("ok"):
                action_name = str(result.get("action") or "")
                if action_name == "claim":
                    written.append(f"「{bond}」这条先记成他自称")
                else:
                    written.append(f"关系改成「{bond}」")
            else:
                notes.append(str(result.get("reason") or "关系没记下"))
        if delta not in (None, ""):
            try:
                amount = float(delta)
            except (TypeError, ValueError):
                amount = 0.0
            if amount:
                result = store.adjust_affinity(
                    state.session_id,
                    user_id,
                    amount,
                    reason="她自己记住的",
                    source="remember",
                )
                if result.get("ok"):
                    value = result.get("value")
                    tail = f"，现在 {round(float(value))}" if value is not None else ""
                    written.append(f"好感 {amount:+.0f}{tail}")
        await self._log_event(
            state,
            "remember",
            {
                "user": user_id,
                "written": written,
                "skipped": notes,
                "evidence": _clip_log(evidence),
            },
            outcome=outcome,
        )
        summary = "；".join([*written, *notes]) or "没什么可记的"
        outcome.notes.append(f"「记住」：{summary}")
        self._log("debug", f"[virtual_world] 记住：{user_id} -> {summary}")

    @staticmethod
    def _echo_into(outcome: TickOutcome, state: WorldState) -> TickOutcome:
        """这几条事件也走调试回显（勾了对应类型就会发出来）。

        复制一份 TickOutcome 是为了"只回显、不掺进这一轮的动作输出"，
        但**发生地必须带过去**：不带的话回显会掉回她的存档会话——
        在私聊里让她拍腿照，🧩「执行指令 /看看腿」那行就跑到群里去了。
        """

        echo = TickOutcome(session_id=outcome.session_id)
        echo.place = str(getattr(outcome, "place", "") or "")
        return echo

    async def _parse_recall_query(
        self, state: WorldState, intent: str, target_node: str = ""
    ) -> dict[str, Any]:
        """解析检索条件：优先问辅助模型，拿不到就退化成"按名字猜"。"""

        nodes = list(self.world.node_map().values())
        node_names = [item.name or item.id for item in nodes]
        zone_names = [zone.name or zone.id for zone in self.world.zones]
        query: dict[str, Any] = {}
        if self.helper_llm is not None and self._tool_param_allowed(state):
            system_prompt, prompt = self.prompts.build_recall_query_prompt(
                intent=intent,
                node_names=node_names,
                zone_names=zone_names,
                memory_types=["scene", "interaction", "relation", "inner", "event"],
            )
            reply = await self._ask_judge(state.session_id, system_prompt, prompt)
            self._count_tool_param(state)
            payload = extract_json_object(reply) if reply else None
            if isinstance(payload, dict):
                query = payload

        # 地点/区域：结构化字段优先，其次从意图里出现的名字里找
        node_id = self._match_node(str(query.get("node") or "").strip() or target_node)
        zone_id = self._match_zone(str(query.get("zone") or "").strip())
        if not node_id and not zone_id:
            node_id = self._match_node_in_text(intent)
            zone_id = self._match_zone_in_text(intent)

        wanted: list[str] = []
        if node_id:
            wanted = [node_id]  # 指定地点：只看那个地点
        elif zone_id:
            wanted = [item.id for item in self.world.nodes_in_zone(zone_id)]
        return {
            "keyword": str(query.get("keyword") or "").strip() or ("" if (node_id or zone_id) else intent),
            "nodes": wanted,
            "node_names": [self._node_name(item) for item in wanted],
            "zone": zone_id,
            "type": str(query.get("type") or "").strip(),
            "limit": query.get("limit") or 3,
        }

    def _match_node(self, text: str) -> str:
        if not text:
            return ""
        key = text.strip()
        for node in self.world.node_map().values():
            if key in (node.id, node.name):
                return node.id
        return ""

    def _match_zone(self, text: str) -> str:
        if not text:
            return ""
        key = text.strip()
        for zone in self.world.zones:
            if key in (zone.id, zone.name):
                return zone.id
        return ""

    def _match_node_in_text(self, text: str) -> str:
        for node in self.world.node_map().values():
            name = node.name or node.id
            if name and name in text:
                return node.id
        return ""

    def _match_zone_in_text(self, text: str) -> str:
        for zone in self.world.zones:
            name = zone.name or zone.id
            if name and name in text:
                return zone.id
        return ""

    async def _run_tool_calls(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        *,
        outcome: TickOutcome | None = None,
    ) -> None:
        """按配置顺序调用动作挂着的工具，把结果汇总进 ``action``。"""

        names = self.resolve_tools(definition, state.node_id)
        if str(getattr(definition, "tool_flow", "simple")) == "search":
            # 联网检索形态走自己的流水线（多查询 / 证据 / 读正文 / 补查）
            await self._run_search_flow(
                state, definition, action, names, outcome=outcome
            )
            return
        mode = str(getattr(definition, "tool_mode", "sequence") or "sequence")
        stored = dict(action.get("tool_params") or {})
        texts: list[str] = []
        failed: list[str] = []
        images: list[str] = []
        last_params: dict[str, Any] = {}
        used: list[str] = []
        if mode == "smart" and len(names) > 1:
            await self._run_tools_smart(
                state,
                definition,
                action,
                names,
                outcome=outcome,
                texts=texts,
                failed=failed,
                images=images,
                used=used,
            )
        else:
            for index, name in enumerate(names):
                entry = await self._call_one_tool(
                    state,
                    definition,
                    action,
                    name,
                    params=dict(stored.get(name) or action.get("params") or {}),
                    previous_results="\n\n".join(texts) if index > 0 else "",
                    outcome=outcome,
                )
                if entry is None:
                    failed.append(f"{name}：缺少参数")
                    continue
                call, sent = entry
                used.append(call.tool or name)
                if not last_params:
                    last_params = sent
                if call.ok and call.text:
                    texts.append(str(call.text))
                elif not call.ok:
                    failed.append(f"{call.tool or name}：{call.error or '没有返回结果'}")
                images.extend(getattr(call, "image_urls", None) or [])
                # 结果里带回来的图要发出去（生图这类动作的意义就在这里）：
                # 落点跟着这一步的正文走，别一律塞回这一拍的会话
                attachments = list(getattr(call, "attachments", None) or [])
                landing = self._image_home(outcome, action)
                self._attach_images(outcome, attachments, session_id=landing)
                if attachments:
                    self._note_own_image(
                        state,
                        str(action.get("intent") or action.get("content") or definition.name),
                        landing,
                    )
                await self._log_tool_outcome(
                    state, definition, name, call, outcome=outcome
                )
                if mode == "fallback" and call.ok and (call.text or images):
                    # 依次尝试：这一个跑通了就不再碰后面的
                    break

        action["tool_names"] = list(names)
        action["tool_name"] = names[-1] if names else ""
        action["tool_used"] = used
        # 只有图片也算拿到结果：不然"生成一张图"这类工具会被当成失败
        action["tool_ok"] = bool(texts or images)
        action["tool_error"] = "；".join(failed)
        if images:
            action["tool_images"] = images
        if texts:
            action["tool_result"] = "\n\n".join(texts)
        elif last_params:
            action.setdefault("params", last_params)
    def _action_base_params(self, definition: ActionDef, action: Any) -> dict[str, Any]:
        """动作上配的固定参数打底，这一轮模型给的参数覆盖在它上面。"""

        fixed: dict[str, Any] = {}
        for key, spec in (definition.params or {}).items():
            value = getattr(spec, "value", "")
            if isinstance(value, str) and value.strip():
                fixed[str(key)] = value.strip()
        return {**fixed, **dict(self._act_get(action, "params", {}) or {})}

    def _explicit_queries(
        self, definition: ActionDef, action: Any, *, limit: int = 0
    ) -> list[str]:
        """她自己写进动作里的查询词（没写就是空列表，交给调用方兜底）。"""

        cap = int(limit or getattr(definition, "search_max_queries", 3) or 1)
        cap = max(1, min(cap, 5))
        result: list[str] = []
        for item in self._act_get(action, "queries", []) or []:
            text = " ".join(str(item).split())[:120]
            if text and text not in result:
                result.append(text)
            if len(result) >= cap:
                break
        return result

    def _search_intent(self, action: Any) -> str:
        """她自己写明的"想查什么"。"""

        return str(
            self._act_get(action, "intent", "")
            or self._act_get(action, "content", "")
            or ""
        ).strip()

    def _search_topic(self, definition: ActionDef, action: Any) -> str:
        """她已经写明的意图 / 动作里配好的主题 / 查询模板；都没有就返回空串。

        注意别拿动作说明兜底：那写的是"这个动作怎么用"，不是"要查什么"。
        真正的兜底（让大模型自己想一句、或者中性主题）在 ``_search_intent_for`` 里。
        """

        intent = self._search_intent(action)
        if intent:
            return intent
        topic = str(getattr(definition, "search_topic", "") or "").strip()
        template = str(getattr(definition, "search_query_template", "") or "").strip()
        if template:
            date_text = self.local_now().strftime("%Y-%m-%d")
            text = " ".join(template.replace("{topic}", topic).replace("{date}", date_text).split())
            if text:
                return text[:120]
        if topic:
            return topic
        return ""

    async def _search_intent_for(
        self, state: WorldState, definition: ActionDef, action: Any
    ) -> str:
        """这一轮"想查什么"：她写的 > 配置的主题/模板 > 让她自己想 > 中性兜底。"""

        given = self._search_topic(definition, action)
        if given:
            return given
        generated = await self._ask_search_topic(state)
        return generated or DEFAULT_SEARCH_TOPIC

    async def _ask_search_topic(self, state: WorldState) -> str:
        """规则触发、她自己又没写意图时，问一句"你现在想查什么"。

        问出来的话题贴着她此刻的处境和群里正在聊的事，比固定主题自然；
        没模型 / 额度用完 / 回答不可用时返回空串，由调用方兜底。
        """

        if self.llm is None or not self._llm_text_allowed(state):
            return ""
        node = self.node(state.node_id)
        chat_lines = [
            f"{item.get('name') or item.get('user_id')}：{_clip_text(item.get('text'), 40)}"
            for item in self.chat_window(state)[-6:]
        ]
        system_prompt, prompt = self.prompts.build_search_topic_prompt(
            where=(node.name or node.id) if node else "",
            state_hint=(
                f"心情 {state.mood}，无聊 {state.boredom:.2f}，好奇心 {state.curiosity:.2f}"
            ),
            chat_lines=chat_lines,
            persona_text=_clip_text(await self._persona_text(state.session_id), 400),
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        self._count_llm_text(state)
        return _clean_search_topic(reply)

    def _query_param_name(self, tool: str) -> str:
        """工具里"查询词"该填哪个参数：按名字猜，认不出来就取第一个参数。"""

        schema = normalize_param_schema(self.tool_schemas().get(tool) or {})
        properties = [str(name) for name in (schema.get("properties") or {})]
        required = [str(name) for name in (schema.get("required") or [])]
        exact = {
            "q",
            "query",
            "queries",
            "keyword",
            "keywords",
            "wd",
            "text",
            "prompt",
            "words",
            "search_query",
            "search_keyword",
        }
        for pool in (required, properties):
            for name in pool:
                low = name.lower()
                if low in exact or "query" in low or "keyword" in low:
                    return name
        return required[0] if required else (properties[0] if properties else "")

    @staticmethod
    def _query_from_params(params: dict[str, Any]) -> str:
        """工具 schema 认不出来时，从这一轮已经带上的参数里找一个像查询词的值。

        日程 / 计划里常常直接写了 ``params: {query: ...}``，别让它白写。
        """

        values = [
            str(value).strip()
            for value in (params or {}).values()
            if isinstance(value, str) and str(value).strip()
        ]
        if len(values) == 1:
            return values[0]
        for key, value in (params or {}).items():
            low = str(key).lower()
            if any(token in low for token in ("query", "keyword", "wd")) and str(
                value
            ).strip():
                return str(value).strip()
        return ""

    def _url_param_name(self, tool: str) -> str:
        """阅读工具里"网址"该填哪个参数。"""

        schema = normalize_param_schema(self.tool_schemas().get(tool) or {})
        properties = [str(name) for name in (schema.get("properties") or {})]
        required = [str(name) for name in (schema.get("required") or [])]
        for pool in (required, properties):
            for name in pool:
                low = name.lower()
                if low in ("url", "uri", "link", "href", "address", "webpage", "page"):
                    return name
                if "url" in low or "link" in low:
                    return name
        return required[0] if required else (properties[0] if properties else "")

    def _reader_tools(self, definition: ActionDef, state: WorldState) -> list[str]:
        """这个检索动作配了哪些可用的阅读网页工具。"""

        result: list[str] = []
        for name in definition.reader_tool_names or []:
            chosen = self.resolve_tool(definition.id, state.node_id, name) or (
                self.resolve_tool_prefix(name)
            )
            if chosen and chosen not in result:
                result.append(chosen)
        return result

    @staticmethod
    def _search_budget(
        definition: ActionDef, action: dict[str, Any] | None = None
    ) -> tuple[int, int]:
        """``(最多读几篇正文, 最多补查几轮)``；``quick`` 档查一轮就收工。

        动作里配的是**上限**：她自己写 ``search_depth`` / ``read_pages`` 时可以收着点用，
        但不能超过配置（免得她每次都开深挖）。
        """

        ranks = {"quick": 0, "standard": 1, "deep": 2}
        configured = str(getattr(definition, "search_depth", "standard") or "standard")
        wanted = str((action or {}).get("search_depth") or "").strip().lower()
        depth = configured
        if wanted in ranks and ranks[wanted] < ranks.get(configured, 1):
            depth = wanted
        reads = max(0, int(getattr(definition, "search_max_reads", 2) or 0))
        rounds = max(0, int(getattr(definition, "search_rounds", 1) or 0))
        asked_reads = (action or {}).get("read_pages")
        try:
            if asked_reads is not None and int(asked_reads) >= 0:
                reads = min(reads, int(asked_reads))
        except (TypeError, ValueError):
            pass
        if depth == "quick":
            return 0, 0
        if depth == "deep":
            return max(reads, 3), max(rounds, 2)
        return reads, rounds

    @staticmethod
    def _search_needs_more(items: list[Evidence]) -> bool:
        """证据是不是太薄：条数太少、或者全是短摘要没有正文。"""

        return not VirtualWorldEngine._search_sufficient(items)

    @staticmethod
    def _search_sufficient(items: list[Evidence]) -> bool:
        """证据够不够用：至少两条，而且其中一条有像样的正文或摘要。"""

        if len(items) < 2:
            return False
        return any(
            len((item.passage or item.snippet or "").strip()) >= 60 for item in items
        )

    async def _search_once(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        tool: str,
        query: str,
        base: dict[str, Any],
        query_key: str,
        *,
        outcome: TickOutcome | None = None,
        echo: bool = True,
        texts: list[str],
        items: list[Evidence],
        failed: list[str],
        images: list[str],
        used: list[str],
    ) -> ToolCallResult | None:
        """按一条查询词调一次搜索工具，结果直接并进证据。"""

        params = dict(base)
        if query_key:
            params[query_key] = query
        entry = await self._call_one_tool(
            state,
            definition,
            action,
            tool,
            params=params,
            outcome=outcome,
            echo=echo,
        )
        if entry is None:
            failed.append(f"{tool}：缺少参数")
            return None
        call, _sent = entry
        used.append(call.tool or tool)
        if call.ok and call.text:
            texts.append(str(call.text))
            items.extend(parse_search_results(str(call.text), source=query))
        elif not call.ok:
            failed.append(f"{call.tool or tool}：{call.error or '没有返回结果'}")
        images.extend(getattr(call, "image_urls", None) or [])
        await self._log_tool_outcome(
            state, definition, tool, call, outcome=outcome, echo=echo
        )
        return call

    @staticmethod
    def _next_search_tool(tool: str, candidates: list[str]) -> str:
        """下一个候选搜索工具（没有就返回空串）。"""

        if tool not in candidates:
            return ""
        index = candidates.index(tool)
        return candidates[index + 1] if index + 1 < len(candidates) else ""

    async def _search_batch(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        queries: list[str],
        base: dict[str, Any],
        query_key: str,
        tool: str,
        candidates: list[str],
        *,
        outcome: TickOutcome | None,
        texts: list[str],
        items: list[Evidence],
        failed: list[str],
        images: list[str],
        used: list[str],
        asked: list[str],
    ) -> str:
        """并发查一批查询词；整批都因为"工具本身坏了"失败时，换下一个候选重试一遍。

        并发是刻意的：串行时每条要等上一条搜完（一次 3~4 秒），既慢、回显也会散成好几行。
        """

        if not queries:
            return tool
        asked.extend(queries)
        # 检索期间的工具调用**不进群**（只写日志）：这里只留一条「正在联网搜索」
        await self._log_event(
            state,
            "search",
            {"action": definition.id, "queries": list(queries)},
            outcome=outcome,
        )
        for _ in range(len(candidates) + 1):
            results = await asyncio.gather(
                *(
                    self._search_once(
                        state,
                        definition,
                        action,
                        tool,
                        query,
                        base,
                        query_key,
                        outcome=outcome,
                        echo=False,
                        texts=texts,
                        items=items,
                        failed=failed,
                        images=images,
                        used=used,
                    )
                    for query in queries
                ),
                return_exceptions=True,
            )
            usable = 0
            broken = 0
            for entry in results:
                if isinstance(entry, Exception):
                    broken += 1
                    failed.append(f"{tool}：{entry}")
                    continue
                if entry is None:
                    broken += 1
                    continue
                if entry.ok or _looks_like_argument_error(str(entry.error or "")):
                    # 跑通了、或者只是参数写错（工具本身没坏）都不算"工具坏了"
                    usable += 1
                else:
                    broken += 1
            if usable or broken < len(queries):
                return tool
            nxt = self._next_search_tool(tool, candidates)
            if not nxt:
                return tool
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": f"搜索工具「{tool}」用不了，改用「{nxt}」",
                },
                outcome=outcome,
            )
            tool = nxt
        return tool

    async def _search_continue(
        self,
        state: WorldState,
        outcome: TickOutcome | None,
        *,
        topic: str,
        asked: list[str],
        items: list[Evidence],
        limit: int = 2,
    ) -> tuple[bool, list[str]]:
        """问主模型：手上这些够了吗？不够就再给几条查询词。

        返回 ``(是否够了, 新的查询词)``。判断不出来（没模型 / 返回不可解析）就当"够了"，
        宁可这次少查一轮，也不要无限查下去。
        """

        if self.llm is None:
            return True, []
        points = []
        for item in items:
            text = "｜".join(
                part
                for part in (
                    item.title,
                    item.published_at,
                    evidence_text(item)[:120],
                )
                if part
            )
            if looks_like_homepage(item.url):
                text += "（这条像首页/栏目页，多半没有正文）"
            points.append(text)
        system_prompt, prompt = self.prompts.build_search_continue_prompt(
            topic=topic,
            asked=asked,
            points=points,
            date_text=self.local_now().strftime("%Y-%m-%d"),
            limit=limit,
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        if outcome is not None:
            outcome.llm_calls += 1
        payload = extract_json_object(reply) if reply else None
        if not isinstance(payload, dict):
            return True, []
        if bool(payload.get("done")):
            return True, []
        raw = payload.get("queries")
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        if not isinstance(raw, list):
            return True, []
        result: list[str] = []
        for entry in raw:
            text = " ".join(str(entry).split())[:60]
            if not text:
                continue
            # 补查最容易给回同一件事的几种说法（更新公告 / 更新内容 / 前瞻回顾），
            # 那样等于把同一批结果再搜一遍：和问过的太像就丢掉
            if any(queries_too_similar(text, old) for old in (*asked, *result)):
                continue
            if text not in result:
                result.append(text)
            if len(result) >= max(1, int(limit)):
                break
        return (not result), result

    async def _search_gap_queries(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        items: list[Evidence],
        *,
        topic: str = "",
    ) -> list[str]:
        """证据不够时让辅助模型补几个查询角度（最多两条）。"""

        if self.helper_llm is None or not self._tool_param_allowed(state):
            return []
        topic = topic or self._search_intent(action) or self._search_topic(definition, action)
        system_prompt, prompt = self.prompts.build_search_gap_prompt(
            topic=topic,
            known=[item.title or item.snippet for item in items],
            date_text=self.local_now().strftime("%Y-%m-%d"),
            limit=2,
        )
        reply = await self._ask_judge(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        payload = extract_json_object(reply) if reply else None
        if not isinstance(payload, dict):
            return []
        raw = payload.get("queries")
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        for entry in raw:
            text = " ".join(str(entry).split())[:60]
            if text and text not in result:
                result.append(text)
        return result[:2]

    async def _read_passage(
        self,
        state: WorldState,
        definition: ActionDef,
        readers: list[str],
        url: str,
        *,
        outcome: TickOutcome | None = None,
        echo: bool = True,
    ) -> str:
        """用阅读工具抓一篇正文（带缓存），失败返回空串。"""

        key = str(url or "").split("?")[0].split("#")[0].rstrip("/").lower()
        cached = self._read_cache.get(key) if key else None
        if cached and (time.monotonic() - cached[0]) < READ_CACHE_SECONDS:
            return cached[1]
        for tool in readers:
            param = self._url_param_name(tool)
            if not param:
                continue
            params = {param: url}
            await self._log_event(
                state,
                "tool_call",
                {
                    "action": definition.id,
                    "tool": tool,
                    "params": dict(params),
                    "note": "读正文",
                },
                outcome=outcome,
                silent=not echo,
            )
            call = await self._call_tool(definition.id, params, state, tool_name=tool)
            await self._log_tool_outcome(
                state, definition, tool, call, outcome=outcome, echo=echo
            )
            if call.ok and call.text:
                # 有的阅读工具返回的是 JSON 外壳（``{"url":…,"content":…}``）：
                # 先取出正文再去掉导航条，最后才截断——顺序反了就会把 JSON 截成半截
                text = _clip_text(
                    clean_passage(unwrap_payload_text(str(call.text))), READ_PASSAGE_CHARS
                )
                if text:
                    if failed_text(text):
                        # 工具有的会"成功返回"一句 extract_failed：那不是正文
                        continue
                    if looks_like_nav(text):
                        # 抓到的是导航/栏目页：当正文用只会把材料带偏，换下一篇
                        await self._log_event(
                            state,
                            "skip",
                            {
                                "action": definition.id,
                                "note": f"这一篇抓回来的是导航页（{url[:60]}），没当正文用",
                            },
                            outcome=outcome,
                        )
                        continue
                    if key:
                        self._read_cache[key] = (time.monotonic(), text)
                        if len(self._read_cache) > 200:
                            self._read_cache.clear()
                    return text
        return ""

    async def _run_search_flow(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        names: list[str],
        *,
        outcome: TickOutcome | None = None,
    ) -> None:
        """联网检索形态的动作：多条查询 → 证据 →（可选）读正文 →（可选）补查。"""

        tool = names[0] if names else ""
        candidates = list(names)
        deadline = time.monotonic() + SEARCH_BUDGET_SECONDS
        base = self._action_base_params(definition, action)
        queries = self._explicit_queries(definition, action)
        texts: list[str] = []
        failed: list[str] = []
        images: list[str] = []
        used: list[str] = []
        items: list[Evidence] = []
        asked: list[str] = []
        reads = 0

        action["tool_names"] = list(names)
        action["tool_used"] = []
        if not tool:
            action["tool_name"] = ""
            action["tool_ok"] = False
            action["tool_error"] = "这个动作还没选好可用的搜索工具"
            return

        query_key = self._query_param_name(tool)
        intent = ""
        if not queries:
            # 她自己没写查询词，但参数里已经带了：就用那一条
            given = (
                str(base.get(query_key) or "").strip()
                if query_key
                else self._query_from_params(base)
            )
            if given:
                queries = [given]
        if not queries:
            # 还是没写：先定"要查什么"（她写的 / 配好的 / 让她自己想），
            # 再让辅助模型把这句意图翻成查询词
            intent = await self._search_intent_for(state, definition, action)
            filled, _note = await self.fill_tool_params(
                state,
                definition,
                PlannedAction(type=definition.id, intent=intent),
                tool_name=tool,
                params=base,
                extra_rules=SEARCH_QUERY_RULES,
            )
            if filled:
                base = {**base, **filled}
            generated = str(base.get(query_key) or "").strip() if query_key else ""
            if not generated:
                generated = intent
                if query_key:
                    base[query_key] = generated
            queries = [generated] if generated else []
        elif query_key:
            missing = [
                name for name in self.missing_params_for(tool, base) if name != query_key
            ]
            if missing:
                filled, _note = await self.fill_tool_params(
                    state,
                    definition,
                    PlannedAction(type=definition.id, intent=queries[0]),
                    tool_name=tool,
                    params=base,
                )
                if filled:
                    base = {**base, **filled}

        # 多条查询**并发**发出去：串行的话每条要等上一条搜完（一次 3~4 秒），
        # 既慢又让回显分散成好几行；并发之后它们几乎同时发生，防抖自然合成一行
        tool = await self._search_batch(
            state,
            definition,
            action,
            queries,
            base,
            query_key,
            tool,
            candidates,
            outcome=outcome,
            texts=texts,
            items=items,
            failed=failed,
            images=images,
            used=used,
            asked=asked,
        )

        read_budget, rounds = self._search_budget(definition, action)
        for _ in range(rounds):
            if time.monotonic() > deadline:
                break
            # 够不够、还要不要再来一轮：交给主模型判断
            done, gaps = await self._search_continue(
                state,
                outcome,
                topic=intent or (asked[0] if asked else ""),
                asked=asked,
                items=items,
            )
            if done or not gaps:
                break
            before = len(items)
            # 同一个意思换个说法的查询词（更新内容 / 更新公告 / 前瞻回顾）只当一条，
            # 不然补查一轮只是把同一批结果再搜一遍
            fresh = [
                entry
                for entry in gaps
                if entry not in asked
                and not any(queries_too_similar(entry, old) for old in asked)
            ]
            if not fresh:
                break
            tool = await self._search_batch(
                state,
                definition,
                action,
                fresh,
                base,
                query_key,
                tool,
                candidates,
                outcome=outcome,
                texts=texts,
                items=items,
                failed=failed,
                images=images,
                used=used,
                asked=asked,
            )
            if len(items) <= before:
                # 补了一轮什么都没新拿到：别再往下问了，直接用现有的说
                break

        if read_budget > 0:
            readers = self._reader_tools(definition, state)
            if readers:
                # 只读"看着像某一篇"的链接：搜索经常先给一堆首页/栏目页，
                # 读它们只会浪费调用（回来的还是导航，反而把摘要挤掉）。
                concrete = [
                    item
                    for item in items
                    if item.url and not item.passage and not looks_like_homepage(item.url)
                ]
                # 带摘要的优先读：摘要里已经有线索的那几篇，正文通常也真
                concrete.sort(key=lambda item: 0 if item.snippet else 1)
                # 多备一个候选：抓不到 / 抓到导航页时顶上，不至于白读一篇
                picked = concrete[: read_budget + 1]
                if not concrete:
                    outcome.notes.append(
                        "搜到的都是首页/栏目页，没读正文，直接用搜索摘要"
                    )
                # 读正文同样并发：几篇一起抓，比一篇篇等快得多
                passages = await asyncio.gather(
                    *(
                        self._read_passage(
                            state,
                            definition,
                            readers,
                            item.url,
                            outcome=outcome,
                            echo=False,
                        )
                        for item in picked
                    ),
                    return_exceptions=True,
                )
                for item, passage in zip(picked, passages):
                    if reads >= read_budget:
                        break
                    if isinstance(passage, Exception) or not passage:
                        continue
                    item.passage = str(passage)
                    reads += 1

        items = merge_evidence(items, limit=8)
        digest = await self._digest_evidence(
            state, items, topic=intent or (asked[0] if asked else ""), outcome=outcome
        )
        action["tool_name"] = tool
        action["tool_used"] = used
        action["tool_ok"] = bool(texts or images)
        action["tool_error"] = "；".join(failed)
        action["tool_queries"] = list(asked)
        if images:
            action["tool_images"] = images
        if texts:
            action["tool_result"] = "\n\n".join(texts)
        elif base:
            action.setdefault("params", dict(base))
        if digest:
            # 交给主模型的是"要点+编号"，不是一堆原文；材料本身仍然留在日志里
            action["tool_digest"] = digest
        if items:
            await self._store_search_evidence(
                state, definition, action, items, asked, outcome=outcome
            )
        await self._remember_search(state.session_id, asked, len(items))

    async def _digest_evidence(
        self,
        state: WorldState,
        items: list[Evidence],
        *,
        topic: str,
        outcome: TickOutcome | None = None,
    ) -> str:
        """把证据压成"要点 + 编号"；没模型/没额度时返回空串（调用方用原始证据）。"""

        if not items or self.helper_llm is None or not self._tool_param_allowed(state):
            return ""
        # 材料 = 正文（像正文时）或搜索摘要。读到导航页 / 抓取失败时**不能把摘要一起丢**：
        # 以前只喂"精读正文"，于是"搜索明明带回了内容，压出来却说材料里没有"。
        usable = [
            (item, evidence_text(item))
            for item in items
            if evidence_text(item)
        ]
        if len(usable) < 2:
            return ""
        # 材料本来就很少很短时不值得再花一次调用：直接用证据块
        if sum(len(text) for _item, text in usable) < 160:
            return ""
        materials = [
            "｜".join(
                part
                for part in (
                    item.title,
                    item.published_at,
                    text[:400],
                )
                if part
            )
            for item, text in usable[:10]
        ]
        system_prompt, prompt = self.prompts.build_search_digest_prompt(
            topic=topic,
            materials=materials,
            date_text=self.local_now().strftime("%Y-%m-%d"),
        )
        reply = await self._ask_helper(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        text = str(reply or "").strip()
        # 把"喂进去的材料"和"压出来的要点"都留一份：材料全是入口页 / 压缩丢内容
        # 这两种情况只靠最终发言分不出来（用户问过"为什么搜出来却说什么都没查到"）。
        await self._log_event(
            state,
            "search_digest",
            {
                "topic": topic,
                "materials": len(materials),
                "material_chars": sum(len(item) for item in materials),
                "preview": _clip_log(" ／ ".join(item[:60] for item in materials[:3])),
                "digest": _clip_log(text),
            },
            outcome=outcome,
        )
        # 压缩说"材料里没有直接答案"时**别拿它当结论**：改用原始证据块，
        # 让主模型自己看搜索摘要（否则"搜到了却说什么都没查到"）。
        if not text or "材料里没有" in text or "没有直接答案" in text:
            return ""
        return _clip_text(text, 1200)

    async def _store_search_evidence(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        items: list[Evidence],
        queries: list[str],
        *,
        outcome: TickOutcome | None = None,
    ) -> None:
        """证据写进 ``action``：编号块给模型看，原文完整留在日志里。"""

        action["tool_evidence"] = [
            {
                "title": item.title,
                "url": item.url,
                "snippet": item.snippet,
                "published_at": item.published_at,
                "passage": item.passage,
            }
            for item in items
        ]
        action["tool_evidence_text"] = render_evidence(items)
        sources = sources_of(items)
        action["tool_sources"] = sources
        await self._log_event(
            state,
            "search_sources",
            {
                "action": definition.id,
                "count": len(items),
                "sources": sources,
                "queries": list(queries or []),
            },
            outcome=outcome,
        )

    @staticmethod
    def _search_evidence_text(
        definition: ActionDef | None, action: dict[str, Any]
    ) -> str:
        """检索型动作交给模型的证据块；其它动作返回空串。"""

        if definition is None:
            return ""
        if str(getattr(definition, "tool_flow", "simple")) != "search":
            return ""
        return str(action.get("tool_evidence_text") or "")

    def _satisfy_curiosity(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        outcome: TickOutcome | None = None,
    ) -> float:
        """检索型动作真查到东西之后，好奇心按配置回落一次，返回降了多少。

        为什么要在动作之外再扣一刀：动作自己配的 ``curiosity: -0.06`` 只够抵消一小时的
        自然增长，而她的好奇心是按分钟涨的——指望那一口把好奇心压到阈值以下是做不到的，
        结果就是冷却（45 分钟）一到又去查同一件事，看起来像"疯狂上网"。
        """

        if definition is None:
            return 0.0
        if str(getattr(definition, "tool_flow", "simple")) != "search":
            return 0.0
        amount = float(getattr(definition, "search_satisfy_curiosity", 0.30) or 0.0)
        if amount <= 0:
            return 0.0
        # 什么都没查到（没配检索工具 / 工具全报错）：这一次不算"满足"
        found = str(
            action.get("tool_evidence_text") or action.get("tool_result") or ""
        ).strip()
        if not found:
            return 0.0
        before = float(state.curiosity)
        state.curiosity = max(0.0, min(1.0, before - amount))
        dropped = before - state.curiosity
        if dropped <= 0:
            return 0.0
        if outcome is not None:
            outcome.notes.append(f"查完东西了：好奇心 -{dropped:.2f}")
        return dropped

    async def desire_intimacy(self, state: WorldState, definition: ActionDef | None) -> float:
        """这个动作算多"亲密的肢体接触"（0~1），用来满足欲求。

        三层，从确定到不确定：

        1. 动作自己在编辑器里写了「亲密程度」→ 直接用；
        2. 内置对照表（`DEFAULT_INTIMACY`）里有的 → 用表里的值，不花任何调用；
        3. **用户自己新加的动作**：交给便宜模型判一次（跟"能不能走安抚通道"共用
           同一份判断与缓存，动作改过名字 / 说明才会重判）。
        """

        if definition is None:
            return 0.0
        explicit = getattr(definition, "intimacy", None)
        if explicit is not None:
            try:
                return max(0.0, min(1.0, float(explicit)))
            except (TypeError, ValueError):
                return 0.0
        known = action_intimacy(definition)
        if known > 0:
            return known
        if str(getattr(definition, "target_type", "none") or "none") == "none":
            return 0.0
        await self._ensure_intimacy_traits()
        score = self.intimacy_of(definition)
        if score is None:
            score = await self._classify_intimacy(state, definition)
        try:
            return max(0.0, min(1.0, float(score or 0.0)))
        except (TypeError, ValueError):
            return 0.0

    async def _satisfy_desire_from_action(
        self,
        state: WorldState,
        definition: ActionDef,
        outcome: TickOutcome | None = None,
    ) -> float:
        """亲近一下：抱一抱、摸摸头这种**真的碰到**的动作会满足欲求。

        跟好奇心那条不一样——它不需要"查到东西"才算数：她是真把这一下做到人身上了，
        做完就是做完了。欲求是**跟人有关**的驱力（见 WorldState.desire），
        所以只有对着人的动作才动它，冲着空气做的动作（发呆、看书）没有影响。

        返回降了多少（0 = 这一步不算亲密接触）。
        """

        if definition is None:
            return 0.0
        if not self.extensions.allows_desire_relief(state):
            # 扩展说了"这回不算满足"（例如她正处在只会更想要的状态里）
            return 0.0
        weight = await self.desire_intimacy(state, definition)
        if weight <= 0:
            return 0.0
        dropped = self.dynamics.satisfy_desire(state, weight)
        if dropped <= 0:
            return 0.0
        if outcome is not None:
            outcome.notes.append(f"亲近了一下：欲求 -{dropped:.2f}")
        return dropped

    async def _call_one_tool(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        name: str,
        *,
        params: dict[str, Any],
        previous_results: str = "",
        outcome: TickOutcome | None = None,
        echo: bool = True,
    ) -> tuple[ToolCallResult, dict[str, Any]] | None:
        """准备参数 → 调工具 → 参数报错时再补一次。

        返回 ``(调用结果, 实际参数)``；连必填参数都补不出来时返回 ``None``（跳过这个工具）。
        ``previous_results`` 用于一个动作挂多个工具的场景：把前一个工具的结果给补参模型，
        它才能填出网址、编号这类只有前面才知道的参数。
        """

        intent = str(action.get("intent") or action.get("content") or "")
        if previous_results:
            merged, note = await self.fill_tool_params(
                state,
                definition,
                PlannedAction(type=definition.id, intent=intent),
                tool_name=name,
                params=params,
                previous_results=previous_results,
            )
            if merged:
                params = {**params, **merged}
            missing = self.missing_params_for(name, params)
            if missing:
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": definition.id,
                        "note": f"工具「{name}」缺少必填参数 {missing}，这次只调了前面的工具"
                        + (f"（{note}）" if note else ""),
                    },
                    outcome=outcome,
                    silent=not echo,
                )
                return None
        call = await self._call_tool(definition.id, params, state, tool_name=name)
        if not call.ok and _looks_like_argument_error(call.error):
            # 工具作者把参数写成"可选"、实现里却必须要：拿报错再补一次，只重试一次
            retried, note = await self.fill_tool_params(
                state,
                definition,
                PlannedAction(type=definition.id, intent=intent),
                tool_name=name,
                params=params,
                error_hint=str(call.error or ""),
            )
            if retried:
                retry_params = {**params, **retried}
                await self._log_event(
                    state,
                    "tool_call",
                    {
                        "action": definition.id,
                        "tool": name,
                        "params": dict(retry_params),
                        "note": "按报错补了一次参数" + (f"：{note}" if note else ""),
                    },
                    outcome=outcome,
                    silent=not echo,
                )
                again = await self._call_tool(
                    definition.id, retry_params, state, tool_name=name
                )
                await self._log_event(
                    state,
                    "tool_result",
                    {
                        "action": definition.id,
                        "tool": again.tool or name,
                        "ok": bool(again.ok),
                        "result": _clip_log(again.text),
                        "error": again.error,
                    },
                    outcome=outcome,
                    silent=not echo,
                )
                if again.ok or again.text:
                    call = again
                    params = retry_params
        await self._log_event(
            state,
            "tool_call",
            {"action": definition.id, "tool": name, "params": dict(params)},
            outcome=outcome,
            silent=not echo,
        )
        return call, dict(call.params or params)

    async def _log_tool_outcome(
        self,
        state: WorldState,
        definition: ActionDef,
        name: str,
        call: ToolCallResult,
        *,
        outcome: TickOutcome | None = None,
        echo: bool = True,
    ) -> None:
        """工具返回（或失败）写进日志，失败还会记一笔挫败感。"""

        if not call.ok:
            # 工具没跑成 = 挫败感：这条线上一眼看不见，但确实影响她的心情
            self.dynamics.apply_event(state, "tool_failed", now=self._now())
        state.add_event(
            "tool_result",
            {"action": definition.id, "tool": call.tool or name, "ok": bool(call.ok)},
        )
        await self._log_event(
            state,
            "tool_result",
            {
                "action": definition.id,
                "tool": call.tool or name,
                "ok": bool(call.ok),
                "result": _clip_log(call.text),
                "error": call.error,
            },
            outcome=outcome,
            silent=not echo,
        )

    async def _run_tools_smart(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        names: list[str],
        *,
        outcome: TickOutcome | None = None,
        texts: list[str],
        failed: list[str],
        images: list[str],
        used: list[str],
    ) -> None:
        """智能选择：让辅助模型按意图挑一个工具，失败先补参、再换下一个。

        每次重问都会把"刚失败的那个"从候选里摘掉，这样它不会反复选中同一个坏工具。
        """

        remaining = list(names)
        while remaining:
            picked, params = await self._pick_tool_for_action(
                state, definition, action, remaining, outcome=outcome
            )
            if picked not in remaining:
                picked, params = remaining[0], {}
            entry = await self._call_one_tool(
                state,
                definition,
                action,
                picked,
                params=dict(params or {}),
                outcome=outcome,
            )
            if entry is None:
                failed.append(f"{picked}：缺少参数")
                remaining = [item for item in remaining if item != picked]
                continue
            call, sent = entry
            used.append(call.tool or picked)
            if call.ok and call.text:
                texts.append(str(call.text))
            elif not call.ok:
                failed.append(f"{call.tool or picked}：{call.error or '没有返回结果'}")
            images.extend(getattr(call, "image_urls", None) or [])
            attachments = list(getattr(call, "attachments", None) or [])
            landing = self._image_home(outcome, action)
            self._attach_images(outcome, attachments, session_id=landing)
            if attachments:
                self._note_own_image(
                    state,
                    str(action.get("intent") or action.get("content") or definition.name),
                    landing,
                )
            await self._log_tool_outcome(
                state, definition, picked, call, outcome=outcome
            )
            if call.ok and (call.text or images):
                return
            if len(remaining) <= 1:
                return
            # 工具本身不可用才换：参数问题的重试已经在 _call_one_tool 里做过了
            remaining = [item for item in remaining if item != picked]

    async def _pick_tool_for_action(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        names: list[str],
        *,
        outcome: TickOutcome | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """智能选择：一次调用同时挑工具和补参数；挑不出来返回 ("", {})。"""

        if self.helper_llm is None or not names:
            return "", {}
        if not self._tool_param_allowed(state):
            return "", {}
        intent = str(action.get("intent") or action.get("content") or "")
        if not intent:
            intent = self.fallback_intent(definition)
        schemas = self.tool_schemas()
        descriptions = self.available_tools()
        tools = [
            {
                "name": name,
                "description": descriptions.get(name, ""),
                "param_text": render_param_text(schemas.get(name) or {}),
            }
            for name in names
        ]
        system_prompt, prompt = self.prompts.build_tool_choice_prompt(
            tools=tools,
            intent=intent,
            recent_chat=self.chat_context(state),
        )
        reply = await self._ask_judge(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        payload = extract_json_object(reply) if reply else None
        if not isinstance(payload, dict):
            return "", {}
        picked = str(payload.get("tool") or "").strip()
        if picked not in names:
            return "", {}
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}
        await self._log_event(
            state,
            "tool_call",
            {
                "action": definition.id,
                "tool": picked,
                "note": f"智能选择：从 {'、'.join(names)} 里挑了这个",
            },
            outcome=outcome,
        )
        return picked, {
            str(key): value for key, value in params.items() if not str(key).startswith("_")
        }

    async def _start_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
        depth: int,
        autonomous: bool,
        *,
        from_plan: bool = False,
        skip_drowsy: bool = False,
    ) -> None:
        # 这里是所有动作真正开始执行的唯一入口（计划 / 日程 / 自主行为 / 工具后续都走这里），
        # 因此把「能不能做」的校验统一放在这里，避免某条路径绕过检查。
        blocked = self._extension_gate(definition, state)
        if blocked:
            # 扩展说了不让做（例如"这个动作只能在私聊里做"）：跳过并留痕
            outcome.notes.append(f"动作 {definition.id} 被扩展拦下：{blocked}")
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": f"「{definition.id}」被扩展拦下：{blocked}"},
                outcome=outcome,
            )
            return
        # 扩展想在"这件事真的做起来"时记一笔（返回的话顺口说出去）
        try:
            note = self.extensions.action_note(definition, state, state.session_id)
        except Exception:
            note = ""
        if note:
            outcome.add_speech(note, outcome.home())
        if not definition.available_in(state.node_id):
            outcome.notes.append(
                f"动作 {definition.id} 在 {state.node_id} 不可用，已跳过"
            )
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": f"「{definition.id}」在 {state.node_id} 不可用"},
                outcome=outcome,
            )
            return
        ok, reason = self._check_preconditions(state, definition)
        if not ok:
            outcome.notes.append(f"动作 {definition.id} 前置条件不满足（{reason}），已跳过")
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": f"「{definition.id}」前置条件不满足：{reason}",
                },
                outcome=outcome,
            )
            return
        if self.action_quota_left(state, definition) <= 0:
            # 配额用尽：这一步不做（日志里写清是哪个周期到顶了）
            outcome.notes.append(f"动作 {definition.id} 今天/本周/本月的次数已用完，已跳过")
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": f"「{definition.name or definition.id}」的使用次数已达上限",
                },
                outcome=outcome,
            )
            return
        self._count_action_use(state, definition)
        if self._should_drowse(state, definition, skip_drowsy=skip_drowsy):
            # 睡前先进临睡期：不立刻躺下（见 _enter_drowsy）
            await self._enter_drowsy(state, node, outcome, definition, action, depth)
            return
        if definition.llm_level == "tool":
            if not await self._prepare_tool_action(state, definition, action, outcome):
                return
        if definition.id == "share" and not self._share_allowed(state):
            outcome.notes.append("超过每小时分享上限，跳过分享")
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": "超过每小时分享上限"},
                outcome=outcome,
            )
            return

        if definition.category == "continuous":
            duration_ticks = self._duration_ticks(definition, action, state)
            if duration_ticks <= 0:
                duration_ticks = 1
            payload: dict[str, Any] = {
                "type": definition.id,
                "params": dict(action.params),
                "intent": action.intent,
                "target": action.target,
                "target_node": action.target_node,
                "content": action.content,
                "duration_ticks": duration_ticks,
                "elapsed_ticks": 0,
                "interruptible": bool(definition.interruptible),
                "visible": bool(definition.visible),
                "depth": depth,
                "persona_id": "",
                "from_plan": bool(from_plan),
                # 这个动作是在哪个会话里被安排/触发的：做完的续说回那儿
                "session": outcome.home(),
                "desc": definition.name or definition.id,
            }
            if from_plan and isinstance(state.current_plan, dict):
                # 记下"这一步属于哪份计划的第几步"：计划中途可能被插进新的安排，
                # 只凭下标认不出自己，做完时就可能把别人的步骤当成自己的推掉
                payload["plan_created_at"] = int(state.current_plan.get("created_at") or 0)
                payload["plan_index"] = int(state.current_plan.get("current_step") or 0)
            if definition.id == "walk_to":
                target = action.target_node or action.target
                if target in self.world.node_map():
                    graph = self.world.adjacent()
                    path = find_path(graph, state.node_id, target)
                    if path is None:
                        outcome.notes.append(f"无法从 {state.node_id} 走到 {target}")
                        return
                    payload["duration_ticks"] = max(1, path_ticks(graph, path))
                    payload["target_node"] = target
                    payload["desc"] = f"正在走去{self.world.node_map()[target].name}"
                else:
                    outcome.notes.append(f"找不到目标 {target}，移动取消")
                    return
            # 正在做的事被顶掉时留个痕迹：以前是"无声替换"，
            # 日志里既看不到她原本在干什么，心情上也没有任何反应。
            if definition.during.state:
                state.state = definition.during.state
            if definition.id in ("sleep", "nap"):
                # 一段新的睡眠：多久、被吵醒过几次、噪音计数都从这里重新算
                state.sleep_started_at = int(state.world_time or 0)
                state.startled_count = 0
                state.startled_until = 0
                state.startled_note = ""
                state.sleep_noise_at = 0.0
                state.sleep_noise_count = 0
                state.sleep_named_count = 0
            previous = state.current_action if isinstance(state.current_action, dict) else None
            if previous and str(previous.get("type") or "") != definition.id:
                self.dynamics.apply_event(state, "interrupted", now=self._now())
                state.add_event("interrupt", {"action": previous.get("type")})
                await self._log_event(
                    state,
                    "interrupt",
                    {
                        "action": previous.get("type"),
                        "note": f"她正在做「{previous.get('desc') or previous.get('type')}」，"
                        f"这一步把它顶掉了",
                    },
                    outcome=outcome,
                )
            if previous and previous.get("from_plan"):
                # 被顶掉的那一步别再捡回来接着做：从计划里划掉，
                # 否则新动作做完之后又会回到旧动作，看着像"同一件事做了两遍"
                index = self._running_plan_index(state, state.current_plan)
                if index is not None:
                    advance(state)
                    outcome.notes.append(
                        f"「{previous.get('desc') or previous.get('type')}」被打断，"
                        "计划里那一步不再接着做"
                    )
            state.current_action = payload
            state.add_event("action_start", {"type": definition.id})
            await self._log_event(
                state,
                "action_start",
                {
                    "type": definition.id,
                    "duration_ticks": duration_ticks,
                    "target_node": payload.get("target_node") or "",
                    "params": dict(action.params or {}),
                },
                outcome=outcome,
            )
            # 工具 / 指令型的持续动作：**当场**就把工具、指令跑掉，出图也立刻发出去，
            # 剩下的时间只是占位。不然图要等到持续时间走完才出来，
            # 而"做完再补一句"反而会赶在图片前面。
            if definition.llm_level in ("tool", "command"):
                await self._start_live_call(state, node, outcome, definition, action, payload)
            if (
                definition.llm_level == "template"
                and str(definition.template or "").strip()
                and self._action_speaks(definition)
            ):
                outcome.add_speech(
                    self.render_template(definition.template, state, node, action),
                    self._speech_target(state, action, outcome),
                )
            if definition.id in ("search_web", "check_weather"):
                outcome.notes.append(f"开始 {definition.id}")
            return

        # 瞬时动作
        if definition.id == "recall":
            # 主动回忆：翻记忆 → 带着结果再问她一次（她只需要在续说里讲话）
            await self._run_recall(state, node, outcome, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.id in self.SCHEDULE_ACTION_IDS:
            # 日程三件套：同样"做完再问她一次"，她只负责说话
            await self._run_schedule_action(state, node, outcome, definition, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.id == "remember":
            # 当场记住：她判断值得记的事直接写进通讯录（事实 / 关系 / 好感）
            await self._run_remember(state, outcome, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.id == "poke":
            # 戳一戳：能戳就戳（群里什么都没说也看得出来她在闹），戳不了就退化成一句文案
            await self._run_poke(state, node, outcome, definition, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.llm_level == "command":
            # 指令触发：拼一条指令交给 AstrBot 跑，再把结果交回给她
            payload = {"type": definition.id, "session": outcome.home()}
            await self._run_command_action(
                state, node, outcome, definition, action, remember=payload
            )
            # 指令型动作也能记状态槽（生图插件的「刷新穿搭」走的就是这条路）
            await self._remember_external_state(state, definition, payload, outcome)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.llm_level == "tool":
            # 瞬时工具动作也要真的调工具：当场调用 → 就着结果说一句。
            # from_plan 交回下面统一处理，避免计划被推进两次。
            payload: dict[str, Any] = {
                "type": definition.id,
                "params": dict(action.params),
                "tool_params": dict(getattr(action, "tool_params", {}) or {}),
                "intent": action.intent,
                "target": action.target,
                "content": action.content,
                "queries": list(getattr(action, "queries", []) or []),
                "depth": depth,
                "elapsed_ticks": 0,
                "duration_ticks": 0,
                "from_plan": False,
                "desc": definition.name or definition.id,
            }
            await self._finish_action(state, node, outcome, payload)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return

        messages = await self._instant_output(
            state, node, definition, action, session_id=outcome.home()
        )
        speaker = self._speech_target(state, action, outcome)
        dropped = speaker is None and bool(messages)
        if dropped:
            # 她想去的地方不在名单里（例如「私聊某个没在组里的人」）：
            # 这句不发出去，也不改发到当前会话——免得私下的话当众说出口。
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": (
                        f"她想去「{action.send_to}」说这句，但那儿不在她能说话的地方里，"
                        "这句没说出口"
                    ),
                },
                outcome=outcome,
            )
            outcome.notes.append(
                f"落点「{action.send_to}」不在她能说话的地方里，这句话已丢弃"
            )
        if definition.id == "share":
            if messages and not dropped:
                for text in messages:
                    outcome.add_speech(text, speaker)
                self._count_share(state)
        elif self._action_speaks(definition):
            if not dropped:
                for text in messages:
                    outcome.add_speech(text, speaker)
        elif definition.id == "think" and action.content:
            state.add_thought(action.content)
            self.memory.remember(
                session_id=state.session_id,
                persona_id="",
                node_id=state.node_id,
                content=action.content,
                memory_type=INNER,
                emotion=state.mood,
                weight=0.35,
                affect=state.affect,
                valence=state.valence,
            )
        self.dynamics.apply_effects(
            state, definition.on_complete.effects, world=self.world, now=self._now()
        )
        # 她主动贴过来这一下：低落时也算一次安抚（不占当天聊天额度）
        await self._maybe_soothe_from_action(state, definition, outcome=outcome)
        # 瞬时动作也走这条完成路径（`_finish_action` 是持续 / 工具动作那条），
        # 所以"亲近一下满足欲求"两边都要接上
        await self._satisfy_desire_from_action(state, definition, outcome)
        state.add_event("action", {"type": definition.id, "visible": definition.visible})
        await self._log_event(
            state,
            "action",
            {
                "type": definition.id,
                "visible": bool(definition.visible),
                "messages": list(messages),
                "content": action.content,
                "target": action.target,
                "params": dict(action.params or {}),
            },
            outcome=outcome,
        )

        # 计划推进
        if from_plan and state.current_plan is not None:
            advance(state)
            await self._tick_plan(state, node, outcome, depth=depth + 1)

    # 「想事情」这类内容型动作：产出的文字是内心活动，永远不发到群里
    INNER_ACTIONS = ("think",)

    def _action_speaks(self, definition: ActionDef) -> bool:
        """这个动作产出的文本要不要发到群里。

        「动作会发到群里」这个开关藏得比较深，新建动作时默认还是关的，于是出现过
        两次同款事故：单轮动作让大模型写的话、模板动作写好的文案，都只进了日志，
        群里什么都看不到。所以判定改成看"这个动作有没有话要说"：

        - 内心活动类（想事情）永远不发，它的产出只进内心活动与记忆；
        - 模板写了文案 → 这句话本来就是给群里看的，发；
        - 单轮动作 → 它的定义就是"让大模型说一句"，发；
        - 目标写着「群 / 某个群友」的 → 也是明确想说话，发；
        - 剩下的（没有文案、纯空间动作）保持静默。
        """

        if definition.visible:
            return True
        if definition.id in self.INNER_ACTIONS:
            return False
        if definition.llm_level == "template":
            return bool(str(definition.template or "").strip())
        if definition.llm_level == "single":
            return True
        return definition.target_type in ("group", "user")

    async def _instant_output(
        self,
        state: WorldState,
        node: NodeDef | None,
        definition: ActionDef,
        action: PlannedAction,
        *,
        session_id: str = "",
    ) -> list[str]:
        """瞬时动作的输出文本。"""

        if definition.id == "say":
            if action.messages:
                return list(action.messages)[: self.world.limits.max_messages_per_say]
            if action.interject:
                return await self._generate_text_actions(
                    state,
                    node,
                    "群里正在聊上面「最近群里在聊」那些内容。"
                    "如果你确实有话想接，就自然地接一句（不要逐条复述、不要总结、不要点名批评）；"
                    "先看清那几句是谁在对谁说：没点名找你的话，就当他们在互相聊，"
                    "别把别人话里的「你」当成你自己，也别用「主人」这类专属称呼去接；"
                    "如果说不出什么，就返回空的动作列表，保持安静。",
                    session_id=session_id,
                )
            return await self._generate_text_actions(
                state,
                node,
                "你现在想和群里的人说点什么。用一个符合你此刻状态和人设的短句说出来，"
                "自然、口语化、不要解释设定。",
                session_id=session_id,
                # 这一句是她自己想说的：好奇心高的时候可以顺着问一句还不知道的事
                autonomy=True,
            )
        if definition.id == "share":
            if not self._share_allowed(state):
                return []
            if action.messages:
                return list(action.messages)[: self.world.limits.max_messages_per_say]
            return await self._generate_text_actions(
                state,
                node,
                "你想和大家分享一件此刻的小事。用 1~2 条简短自然的消息说出来。",
                session_id=session_id,
            )
        if definition.llm_level == "template":
            text = self.render_template(definition.template, state, node, action)
            return [text] if text else []
        if action.content:
            return [action.content]
        # 「单轮」动作：这一轮没有现成台词（日程、计划里的步骤都没有），
        # 就让大模型按动作语义现写一句——不然只会发一句机械文案。
        if definition.llm_level == "single" and self.llm is not None:
            generated = await self._generate_text_actions(
                state,
                node,
                self._single_action_instruction(state, definition, action),
                session_id=session_id,
            )
            if generated:
                return generated
        # 没配模型、发言额度用完、或者它没说出什么：退化成一句动作文案
        label = definition.name or definition.id
        target_name = self._target_name(state, action.target)
        if action.target and target_name:
            return [
                self.render_template(f"（{{bot}}{label}了 {{user}}）", state, node, action)
            ]
        return [self.render_template(f"（{{bot}}{label}）", state, node, action)]

    def _single_action_instruction(
        self, state: WorldState, definition: ActionDef, action: PlannedAction
    ) -> str:
        """「单轮」动作没现成台词时，给大模型的一句交代。"""

        label = definition.name or definition.id
        who = self._target_name(state, action.target)
        lines = [
            f"你刚刚做了「{label}」这个动作。",
            f"这个动作是什么意思：{definition.description or label}。",
        ]
        if action.intent and action.intent.strip() not in ("", label):
            lines.append(f"你当时的想法：{action.intent.strip()}")
        if who:
            lines.append(f"对象是 {who}。")
        lines.append(
            "用你自己的口吻说一句发到群里的话：短、自然、像随口说的，"
            "不要解释设定、不要分点、不要写成在招呼全场。"
        )
        return "\n".join(lines)

    @staticmethod
    def _target_name(state: WorldState, target: str) -> str:
        if not target:
            return ""
        record = state.user_presence.get(target)
        if record and record.get("name"):
            return str(record["name"])
        return target

    @staticmethod
    def _target_display(state: WorldState, target: str) -> str:
        """写进文案里的称呼：认得出名字就用名字，认不出就别把 QQ 号写进句子里。"""

        if not target:
            return ""
        record = state.user_presence.get(target)
        if record and record.get("name"):
            return str(record["name"])
        return ""

    async def _run_poke(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
    ) -> None:
        """戳一戳：调平台接口戳对方一下；平台不支持就退化成一句动作文案。"""

        target = str(action.target or "").strip()
        if not target:
            # 没给目标：挑一个最近说过话的人，谁在就戳谁
            recent = state.recent_active_users(limit=1)
            target = str(recent[0].get("user_id") or "") if recent else ""

        ok = False
        reason = ""
        route = ""
        poker = getattr(self.messenger, "poke", None)
        if not target:
            reason = "不知道要戳谁（群里还没有人说话）"
        elif not callable(poker):
            reason = "当前发送通道不支持戳一戳"
        else:
            try:
                # 在"她现在说话的地方"戳：私聊里说话就戳私聊，别戳到群里去
                result = await poker(outcome.home(), target)
                ok = bool(getattr(result, "ok", result))
                reason = str(getattr(result, "reason", "") or "")
                route = str(getattr(result, "route", "") or "")
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"

        if ok:
            outcome.notes.append(f"戳了 {self._target_name(state, target)}")
            await self._log_event(
                state,
                "poke",
                {
                    "target": target,
                    "name": self._target_name(state, target),
                    "ok": True,
                    "route": route,
                },
                outcome=outcome,
            )
        else:
            # 戳不动也要留痕：不然只看到她"变成了固定文案"，不知道是为什么
            outcome.notes.append(f"戳不动 {target}：{reason or '未知原因'}")
            await self._log_event(
                state,
                "poke",
                {
                    "target": target,
                    "name": self._target_name(state, target),
                    "ok": False,
                    "note": reason or "没有可用的戳一戳通道",
                },
                outcome=outcome,
            )
            text = self.render_template(definition.template, state, node, action)
            if text:
                outcome.add_speech(text, self._speech_target(state, action, outcome))
        self.dynamics.apply_effects(
            state, definition.on_complete.effects, world=self.world, now=self._now()
        )
        state.add_event("action", {"type": "poke", "visible": bool(outcome.messages)})
        await self._log_event(
            state,
            "action",
            {
                "type": "poke",
                "target": target,
                "target_name": self._target_name(state, target),
                "ok": ok,
                "note": "戳了一戳" if ok else f"没戳成，改用文案：{reason}",
            },
            outcome=outcome,
        )

    def render_template(
        self,
        text: str,
        state: WorldState,
        node: NodeDef | None,
        action: PlannedAction,
    ) -> str:
        """渲染动作模板里的占位符：{bot} 她自己、{user} 目标群友、{node} 当前地点。"""

        if not text:
            return ""
        bot = (
            (self.world.bot_name or "").strip()
            or state.bot_base_nickname
            or pronounce_for(self.world.gender)
        )
        user = self._target_display(state, action.target) or "你"
        result = (
            text.replace("{bot}", bot)
            .replace("{user}", user)
            .replace("{node}", (node.name if node else "") or "")
        )
        return result

    async def _run_chain(
        self,
        state: WorldState,
        chain: list[Any],
        outcome: TickOutcome,
        *,
        depth: int,
    ) -> None:
        """执行日程动作链：瞬时动作立即执行；持续动作启动后，剩余步骤转为计划。"""

        skipped = 0
        for index, step in enumerate(chain or []):
            action = PlannedAction(
                type=str(getattr(step, "type", "") or ""),
                messages=list(getattr(step, "messages", []) or []),
                target=str(getattr(step, "target", "") or ""),
                target_node=str(getattr(step, "target_node", "") or ""),
                content=str(getattr(step, "content", "") or ""),
                # 这一步"想干什么"必须带过去：工具型动作的参数就是照它补的，
                # 丢了它就只能拿动作说明兜底（以前日程里写的意图等于白写）
                intent=str(getattr(step, "intent", "") or ""),
                params=dict(getattr(step, "params", {}) or {}),
                duration=int(getattr(step, "duration", 0) or 0),
                queries=[
                    str(item) for item in (getattr(step, "queries", []) or []) if str(item)
                ],
                search_depth=str(getattr(step, "search_depth", "") or ""),
                read_pages=_to_int_or_default(getattr(step, "read_pages", None), -1),
            )
            definition = self.world.action_map().get(action.type)
            if definition is None:
                outcome.notes.append(f"日程里的动作 {action.type} 不存在，已跳过")
                skipped += 1
                await self._log_event(
                    state,
                    "skip",
                    {"action": action.type, "note": f"日程里的动作「{action.type}」不存在"},
                    outcome=outcome,
                )
                continue
            if not definition.enabled:
                # 停用 = 当作根本没有这个动作（日程里排到的这一步也跳过）
                outcome.notes.append(f"日程动作 {action.type} 已停用，已跳过")
                skipped += 1
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": action.type,
                        "note": f"日程的这一步「{definition.name or action.type}」已停用",
                    },
                    outcome=outcome,
                )
                continue
            ok, reason = self._check_preconditions(state, definition)
            if not ok:
                outcome.notes.append(
                    f"日程动作 {action.type} 前置条件不满足（{reason}），已跳过"
                )
                skipped += 1
                # 日程被跳过的原因必须进事件日志，否则日志页看不出"她为什么没做这件事"。
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": action.type,
                        "note": f"日程的这一步「{definition.name or action.type}」前置条件不满足：{reason}",
                    },
                    outcome=outcome,
                )
                continue
            if definition.category == "continuous":
                await self._start_action(
                    state, self.node(state.node_id), outcome, definition, action, depth, True
                )
                rest = chain[index + 1 :]
                if rest and (
                    state.current_action is not None or state.state == STATE_DROWSY
                ):
                    # 临睡期也算"手上这件事没做完"：日程后面那几步要等她睡下再继续，
                    # 不能因为还没真躺下就把它们丢掉。
                    remaining = [
                        {
                            "action": str(getattr(item, "type", "") or ""),
                            "target_node": str(getattr(item, "target_node", "") or ""),
                            "target": str(getattr(item, "target", "") or ""),
                            "duration": int(getattr(item, "duration", 0) or 0),
                            "content": str(getattr(item, "content", "") or ""),
                            "intent": str(getattr(item, "intent", "") or ""),
                            "messages": list(getattr(item, "messages", []) or []),
                            "params": dict(getattr(item, "params", {}) or {}),
                        }
                        for item in rest
                    ]
                    state.current_plan = create_plan(
                        steps=remaining,
                        world_time=state.world_time,
                        valid_for=self.world.limits.plan_valid_duration,
                        reason="日程后续步骤",
                        source="schedule",
                    )
                return
            await self._execute_actions(
                state,
                self.node(state.node_id),
                outcome,
                [action],
                depth=depth,
                autonomous=True,
                from_schedule=True,
                # 地点限制由日程的「自动先走过去」开关决定：没开就是硬条件，
                # 到了别处这一步就跳过，而不是悄悄替她走一趟。
                allow_remote_travel=False,
            )
        if chain and not skipped:
            # 整条日程顺顺利利跑完：心情上给一点正反馈
            self.dynamics.apply_event(state, "schedule_done", now=self._now())

    async def _generate_text_actions(
        self,
        state: WorldState,
        node: NodeDef | None,
        instruction: str,
        *,
        session_id: str = "",
        autonomy: bool = False,
    ) -> list[str]:
        """用 LLM 生成要说的话（say / share 没有现成文案时）。

        ``session_id``：这句话准备说给哪个会话听。**必须带上**——提示词里
        「你能说话的地方」和"这里是哪儿"都按它算；不带的话她会以为只有脚下这一处
        能说话，跨会话的承诺（"我去群里说一声"）就落不了地。

        ``autonomy``：这句话是"她自己想说的"（不是接别人的话、也不是某个动作的台词）。
        只有这种场合才把「好奇心上来了，想去问问谁」带进来，接话和台词都别拐弯。

        生成不出来时**一律返回空**（这一轮她保持安静）。以前这里会退到一句
        写死的通用短句——那句话跟谁的人设都不搭，说错话比不说伤害大得多。
        """

        if self.llm is None:
            return []
        if not self._llm_text_allowed(state):
            self._log("debug", "自主发言的 LLM 预算用完了，这一轮保持安静")
            return []
        persona_text = await self._persona_text(state.session_id)
        _cell, say_limit, style_text = self.style_for(state, state.session_id)
        current = str(session_id or state.session_id or "")
        system_prompt = self.prompts.build_autonomous_system_prompt(
            persona_text=persona_text,
            state=state,
            node=node,
            available_tools=self.available_tools(),
            engagement_hint=self.engagement.hint(state),
            max_messages=say_limit,
            recent_chat=self.chat_window(state),
            reasoning=bool(self.world.reasoning_enabled),
            style_block=style_text,
            profile_text=self.profile_block(state),
            extra_notes=[
                note for note in [self.extra_reminders(state, autonomy=autonomy)] if note
            ],
            session_directory=self.session_directory(state),
            current_session=current,
            session_labels=self.session_labels(state),
            hidden_actions=self.exhausted_actions(state),
            samples=self.voice_sample_lines(state, state.session_id),
            **await self.runtime_notes(state.session_id),
        )
        note = self.autonomous_prompt_note(state)
        prompt = (
            instruction
            + ("\n\n" + note if note else "")
            + "\n\n"
            + self.prompts.AUTONOMOUS_MANNERS
            + '\n只输出 JSON：{"actions":[{"type":"say","messages":["..."]}]}'
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        self._count_llm_text(state)
        if not reply:
            return []
        result = parse_action_payload(
            reply,
            available_actions={"say"},
            max_actions=1,
            max_messages=say_limit,
        )
        for item in result.actions:
            if item.type == "say" and item.messages:
                return item.messages
        cleaned = reply.strip()
        if not cleaned:
            return []
        if cleaned.lstrip().startswith("{"):
            # 模型又回了一段 JSON、但里面没有能说的话（例如 actions 是空的 = 她不想开口）：
            # **绝不能**把整段 JSON（连 reasoning 一起）当成台词发出去
            payload = extract_json_object(cleaned)
            spoken = speakable_messages(payload) if isinstance(payload, dict) else []
            if spoken:
                return spoken[:say_limit]
            self._log("debug", "主动发言这一轮模型给的是 JSON 且没有要说的话，按保持安静处理")
            return []
        return [cleaned]

    def _duration_ticks(
        self, definition: ActionDef, action: PlannedAction, state: WorldState
    ) -> int:
        """把动作时长换算成 tick。

        - ``duration_mode=fixed``：用配置的固定秒数（大模型给的 duration 不生效）；
        - ``duration_mode=llm``：用大模型给的秒数，并夹在 duration_min~duration_max 之间
          （大模型没给就看她的 intent / 台词里有没有说多久，都没有才用下限——
          「小睡三小时」这种话写在 intent 里、duration 忘了填是最常见的写法）。
        """

        if definition.duration_mode == "llm":
            low = int(definition.duration_min or 60)
            high = int(definition.duration_max or max(low, int(definition.duration or low * 4)))
            if high < low:
                high = low
            seconds = int(action.duration or 0)
            if not seconds:
                for hint in (action.intent, *list(action.messages or [])):
                    seconds = _guess_duration_seconds(hint)
                    if seconds:
                        break
            seconds = seconds or low
            seconds = min(max(seconds, low), high)
        else:
            seconds = action.duration or definition.duration or 0
        return max(1, int(round(seconds / max(1.0, self.tick_seconds))))

    async def _call_tool(
        self,
        action_id: str,
        params: dict[str, Any],
        state: WorldState,
        tool_name: str = "",
    ) -> ToolCallResult:
        """调用工具，返回结构化结果（成功文本 / 失败原因）。"""

        if self.tools is None:
            return ToolCallResult(ok=False, error="没有可用的工具通道")
        chosen = self.resolve_tool(action_id, state.node_id, tool_name)
        if not chosen:
            return ToolCallResult(ok=False, error="没有解析到可用工具")
        try:
            result = await self.tools.call_tool(
                chosen, dict(params or {}), state.session_id
            )
        except Exception as exc:
            self._log("warning", f"工具 {chosen} 调用失败: {exc}")
            self.note_tool_result(chosen, ok=False, error=str(exc))
            return ToolCallResult(ok=False, error=str(exc), tool=chosen)
        if isinstance(result, ToolCallResult):
            if not result.tool:
                result.tool = chosen
            if not result.params:
                result.params = dict(params or {})
            # 成功就清掉失败计数；"工具坏了"的失败才计入熔断
            self.note_tool_result(
                chosen, ok=bool(result.ok), error=str(result.error or "")
            )
            return result
        # 兼容返回纯字符串的实现
        text = str(result or "").strip()
        if not text:
            self.note_tool_result(chosen, ok=False, error="工具返回了空结果")
            return ToolCallResult(
                ok=False, error="工具返回了空结果", tool=chosen, params=dict(params or {})
            )
        self.note_tool_result(chosen, ok=True)
        return ToolCallResult(ok=True, text=text, tool=chosen, params=dict(params or {}))

    async def _ask_llm(
        self,
        session_id: str,
        system_prompt: str,
        prompt: str,
        contexts: list[dict[str, Any]] | None = None,
        image_urls: list[str] | None = None,
    ) -> str | None:
        def note(ok: bool, error: str = "") -> None:
            self._last_llm[session_id] = {
                "at": self._now(),
                "ok": bool(ok),
                "error": str(error)[:200],
            }

        if self.llm is None:
            note(False, "没有可用的模型")
            return None
        try:
            reply = await self.llm.generate(
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                contexts=list(contexts) if contexts else None,
                image_urls=list(image_urls) if image_urls else None,
            )
        except Exception as exc:
            self._log("warning", f"LLM 调用失败: {exc}")
            note(False, f"{type(exc).__name__}: {exc}")
            return None
        if reply is None or not getattr(reply, "ok", True):
            note(False, getattr(reply, "error", "") or "模型没有返回内容")
            return None
        note(True)
        return getattr(reply, "text", "") or ""

    async def _persona_text(self, session_id: str) -> str:
        if self.persona is None:
            return ""
        try:
            return await self.persona.get_persona_text(session_id)
        except Exception:
            return ""

    # ================= 打断 / 唤醒 =================

    async def _ask_creator(self, session_id: str, system_prompt: str, prompt: str) -> str | None:
        """内容生成模型：编事件包，以及编辑器里批量生成动作 / 地点。"""

        if self.creator_llm is None:
            return None
        try:
            reply = await self.creator_llm.generate(
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.8,
            )
        except Exception as exc:
            self._log("warning", f"内容生成模型调用失败: {exc}")
            return None
        if reply is None or not getattr(reply, "ok", True):
            return None
        return getattr(reply, "text", "") or ""

    def _generator_session_id(self) -> str:
        """生成时借一个会话 id 去取人格（没有启用会话就用空字符串）。"""

        ids = self.enabled_session_ids()
        return ids[0] if ids else ""

    async def generate_actions_for_zone(
        self, zone_id: str, per_node: Any = 0, node_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """按区域里的**每个地点**批量生成动作草稿（不落库，交给编辑器预览勾选）。

        只传了 ``node_ids`` 时就只为这几个地点生成（编辑器里"只给这个地点生成"）。
        """

        zone = self.world.zone_map().get(zone_id)
        if zone is None:
            return {"actions": [], "problems": ["找不到这个区域"]}
        inside = self.world.nodes_in_zone(zone_id)
        if node_ids:
            wanted = {str(item) for item in node_ids if str(item)}
            nodes = [node for node in inside if node.id in wanted]
        else:
            nodes = inside
        if not nodes:
            return {
                "actions": [],
                "problems": ["这个区域里还没有可用地点"],
            }
        count = clamp_per_node(per_node)
        session_id = self._generator_session_id()
        system_prompt, prompt = self.prompts.build_action_generator_prompt(
            persona_text=await self._persona_text(session_id),
            zone_name=zone.name or zone.id,
            zone_note=zone.note,
            nodes=[
                {"id": node.id, "name": node.name, "prompt": node.prompt or node.atmosphere.describe()}
                for node in nodes
            ],
            existing_actions=[action.name or action.id for action in self.world.actions],
            tools=sorted(self.available_tools()),
            per_node=count,
        )
        reply = await self._ask_creator(session_id, system_prompt, prompt)
        if reply is None:
            return {
                "actions": [],
                "problems": ["生成模型没有返回内容（检查插件配置里的「内容生成模型」）"],
            }
        actions, problems = parse_generated_actions(
            reply,
            node_ids=[node.id for node in nodes],
            tool_names=set(self.available_tools()),
        )
        return {"actions": actions, "problems": problems, "raw": _clip_text(reply, 1200)}

    async def generate_nodes_for_zone(
        self, zone_id: str, count: Any = 0
    ) -> dict[str, Any]:
        """给区域批量生成地点草稿：自动摆位 + 按方案 A 自动连线（同样不落库）。"""

        zone = self.world.zone_map().get(zone_id)
        if zone is None:
            return {"nodes": [], "edges": [], "problems": ["找不到这个区域"]}
        wanted = clamp_node_count(count)
        session_id = self._generator_session_id()
        inside = self.world.nodes_in_zone(zone_id)
        system_prompt, prompt = self.prompts.build_node_generator_prompt(
            persona_text=await self._persona_text(session_id),
            zone_name=zone.name or zone.id,
            zone_note=zone.note,
            existing_nodes=[node.name or node.id for node in inside],
            count=wanted,
        )
        reply = await self._ask_creator(session_id, system_prompt, prompt)
        if reply is None:
            return {
                "nodes": [],
                "edges": [],
                "problems": ["生成模型没有返回内容（检查插件配置里的「内容生成模型」）"],
            }
        nodes, problems = parse_generated_nodes(
            reply,
            existing_ids={node.id for node in self.world.nodes},
            max_nodes=wanted,
        )
        positions = auto_layout(
            [(float(node.x), float(node.y)) for node in inside], len(nodes)
        )
        for node, (x, y) in zip(nodes, positions):
            node["zone_id"] = zone_id
            node["x"], node["y"] = x, y
        # 方案 A：第一个新地点连到区域内最近的既有地点，之后依次串成链
        anchor = ""
        if inside and positions:
            anchor = min(
                inside,
                key=lambda node: (float(node.x) - positions[0][0]) ** 2
                + (float(node.y) - positions[0][1]) ** 2,
            ).id
        pairs = link_plan([node["id"] for node in nodes], anchor=anchor)
        edges = [
            {
                "id": f"e_{src}_{dst}",
                "from": src,
                "to": dst,
                "ticks": 1,
                "bidirectional": True,
            }
            for src, dst in pairs
        ]
        return {
            "nodes": nodes,
            "edges": edges,
            "problems": problems,
            "raw": _clip_text(reply, 1200),
        }

    async def set_values(
        self, session_id: str, values: dict[str, Any]
    ) -> dict[str, float]:
        """手改她的数值（编辑器「实时状态」用）：只认已知字段，自动夹到 0~1，并记一条日志。"""

        allowed = (
            "energy",
            "loneliness",
            "curiosity",
            "affect",
            "valence",
            "boredom",
            "desire",
        )
        applied: dict[str, float] = {}
        async with self.session_state(session_id) as state:
            for key, value in (values or {}).items():
                if key not in allowed:
                    continue
                try:
                    number = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    continue
                if key == "valence":
                    # 手改的是"她看到的心情"，所以落成基线 + 新偏移
                    self.dynamics.apply_effects(state, {"valence": f"={number}"})
                else:
                    setattr(state, key, number)
                applied[key] = round(number, 4)
            if applied:
                self.dynamics.refresh(state, now=self._now())
                state.mood = self.dynamics.derive_mood(state)
                await self._log_event(state, "manual", {"values": applied})
        return applied

    async def apply_cancel(
        self,
        state: WorldState,
        mode: str,
        user_text: str,
        outcome: TickOutcome,
    ) -> str:
        """按模型给出的 ``cancel`` 停掉她的手头安排。

        - ``now``：立刻停手（尊重「可打断」标记），并放弃剩下的安排；
        - ``queue``：手上这件做完，但不按原计划继续了。

        模型既然判断要终止，就照做——否则会出现"嘴上说不干了、身体还在接着干"。
        ``user_text`` 只用来记日志（谁说了什么让她停的）。返回做了什么（空串 = 什么都没做）。
        """

        if mode not in CANCEL_MODES:
            return ""

        action = state.current_action if isinstance(state.current_action, dict) else None
        had_plan = isinstance(state.current_plan, dict) and bool(
            state.current_plan.get("steps")
        )
        stopped = ""
        blocked = ""
        if mode == "now" and action is not None:
            if action.get("interruptible", True):
                stopped = str(action.get("type") or "")
                state.current_action = None
                state.state = STATE_IDLE
            else:
                # 标了「不可打断」的动作（例如睡觉）不做一半就扔，但剩下的安排照样放弃
                blocked = str(action.get("type") or "")
        state.current_plan = None

        parts = []
        if stopped:
            definition = self.world.action_map().get(stopped)
            label = (definition.name or definition.id) if definition else stopped
            parts.append(f"停掉了「{label}」")
        if blocked:
            definition = self.world.action_map().get(blocked)
            label = (definition.name or definition.id) if definition else blocked
            parts.append(f"「{label}」不可打断，做完这件就停")
        if had_plan:
            parts.append("放弃了还没做的安排")
        if not parts:
            parts.append("本来就没在忙")
        note = "、".join(parts)
        outcome.notes.append(f"要她停下：{note}")
        await self._log_event(
            state,
            "cancel",
            {
                "mode": mode,
                "stopped": stopped,
                "blocked": blocked,
                "plan": had_plan,
                "by": str(user_text or "")[:60],
                "note": note,
            },
            outcome=outcome,
        )
        return note

    async def interrupt(self, session_id: str, *, force: bool = False) -> bool:
        """打断当前持续动作。返回是否真的打断了。

        **打断睡觉 = 叫醒**：只把动作停掉是不够的——计划里那一步还在，下一个 tick
        又会把她放倒（看起来就是"打断了却马上又睡了，@ 她也没反应"）。
        所以这里走和「叫醒」同一套：停动作、清掉排队的计划、给一段不会再睡的保护期。
        """

        if not self.is_enabled(session_id):
            return False
        async with self.session_state(session_id) as state:
            action = state.current_action
            asleep = self._is_asleep(state)
            if asleep:
                self._wake_up_state(
                    state, MessageContext(session_id=session_id, user_name="你", text="")
                )
                return True
            if not isinstance(action, dict):
                return False
            if not force and not action.get("interruptible", True):
                return False
            state.add_event("interrupt", {"action": action.get("type")})
            state.current_action = None
            state.state = STATE_IDLE
            return True

    async def clear_plan(self, session_id: str) -> bool:
        """放弃还没做的安排（编辑器按钮与 /vw stop 用）。返回是否有东西被清掉。"""

        if not self.is_enabled(session_id):
            return False
        async with self.session_state(session_id) as state:
            plan = state.current_plan
            if not isinstance(plan, dict) or not plan.get("steps"):
                return False
            state.current_plan = None
            await self._log_event(
                state, "cancel", {"mode": "queue", "plan": True, "note": "放弃了还没做的安排"}
            )
            return True

    async def refresh_nickname(self, session_id: str) -> dict[str, Any]:
        """重新从平台读一次她现在的群名片（编辑器「重新获取」按钮用）。"""

        if not self.is_enabled(session_id):
            return {"ok": False, "card": "", "note": "这个会话没有启用虚拟世界"}
        card = ""
        try:
            card = str(await self.messenger.fetch_group_card(session_id) or "").strip()
        except Exception as exc:
            self._log("warning", f"读取群名片失败：{exc}")
        async with self.session_state(session_id) as state:
            before = state.bot_current_nickname
            if card:
                state.bot_current_nickname = card
                if not state.bot_base_nickname:
                    # 之前没记过原名，就把这次读到的当成原名
                    state.bot_base_nickname = card
                state.last_nickname_update_at = self._now()
            note = "" if card else "还没收到过这个群的消息，拿不到机器人句柄"
            await self._log_event(
                state,
                "nickname",
                {"manual": True, "to": card, "ok": bool(card), "note": note},
            )
        return {"ok": bool(card), "card": card, "before": before, "note": note}

    async def clear_all_states(self) -> int:
        """清掉所有会话的世界状态（切换预设时用）。记忆与日志不动。"""

        cleared = 0
        for session_id in list(self.state_session_ids()):
            try:
                await self.db.call("delete_state", session_id)
                cleared += 1
            except Exception as exc:
                self._log("warning", f"清状态失败 {session_id}: {exc}")
        self._event_ids.clear()
        self._last_decider_at.clear()
        return cleared

    async def repair_states_after_config_change(self) -> list[str]:
        """换过世界（地图 / 动作）之后修一遍状态，不清状态也不让她卡住。

        换了地图还不清状态时会出现两种"卡住"：人站在一个**已经不存在的房间**里，
        或者手上挂着**已经不存在的动作 / 地点**。这里就地修好并记一条日志——
        总比让她在虚空里发呆、或者每轮都去执行一个被丢弃的步骤要好。
        """

        notes: list[str] = []
        nodes = set(self.world.node_map())
        actions = set(self.world.action_map())
        default_node = self.default_node_id()
        for session_id in list(self.state_session_ids()):
            fixed: list[str] = []
            try:
                async with self.session_state(session_id) as state:
                    if state.node_id and state.node_id not in nodes:
                        fixed.append(f"地点 {state.node_id} 没了，落到 {default_node}")
                        state.node_id = default_node
                        state.node_since = self._now()
                    action = state.current_action
                    if isinstance(action, dict):
                        kind = str(action.get("type") or "")
                        node = str(action.get("target_node") or "")
                        if (kind and kind not in actions) or (node and node not in nodes):
                            fixed.append(f"丢掉手上的「{kind or '动作'}」")
                            state.current_action = None
                            state.state = STATE_IDLE
                    plan = state.current_plan
                    if isinstance(plan, dict) and plan.get("steps"):
                        kept: list[dict[str, Any]] = []
                        for step in plan["steps"]:
                            if not isinstance(step, dict):
                                continue
                            kind = str(step.get("action") or "")
                            node = str(step.get("target_node") or "")
                            if (kind and kind not in actions) or (node and node not in nodes):
                                continue
                            kept.append(step)
                        if len(kept) != len(plan["steps"]):
                            fixed.append(f"排队的计划里删掉 {len(plan['steps']) - len(kept)} 步")
                            plan["steps"] = kept
                            if not kept:
                                state.current_plan = None
                    if fixed:
                        await self._log_event(
                            state, "repair", {"note": "；".join(fixed)}
                        )
            except Exception as exc:
                self._log("warning", f"修状态失败 {session_id}: {exc}")
            if fixed:
                notes.append(f"{self.scope_session(session_id)}：{'；'.join(fixed)}")
        return notes

    def state_session_ids(self) -> list[str]:
        """有世界状态的会话 id。"""

        try:
            return [
                str(item)
                for item in self.db.raw.list_state_ids()
                if str(item)
            ]
        except Exception:
            return []

    async def wake_up(self, session_id: str) -> bool:
        """手动叫醒（编辑器按钮 / `/vw 叫醒`）。

        走的是和「@ 她 + 唤醒词」完全一样的那套：停下动作、**清掉排队的计划**
        （否则计划里那一步「睡觉」下一个 tick 又把她放倒）、给一段不会再睡的保护期、
        顺手把群名片从「睡觉中」改回来。
        """

        if not self.is_enabled(session_id):
            return False
        async with self.session_state(session_id) as state:
            # 临睡期被叫醒 = "别睡了，再陪我一会儿"：把揣着的那一步睡觉丢掉
            was_sleeping = state.is_sleeping or state.state == STATE_DROWSY
            if state.state == STATE_DROWSY:
                state.drowsy_sleep_step = {}
                state.drowsy_started_world_time = 0
                state.drowsy_started_at = 0.0
                state.state = STATE_IDLE
            self._wake_up_state(
                state, MessageContext(session_id=session_id, user_name="你", text="")
            )
            return was_sleeping

    # ================= 限额 =================

    def _hour_index(self, state: WorldState) -> int:
        return int(state.world_time * self.tick_seconds // 3600)

    def _count_autonomous(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.autonomous_hour_marker != hour:
            state.autonomous_hour_marker = hour
            state.autonomous_count_hour = 0
        state.autonomous_count_hour += 1

    def _within_hourly_limit(self, state: WorldState) -> bool:
        hour = self._hour_index(state)
        if state.autonomous_hour_marker != hour:
            return True
        return state.autonomous_count_hour < self.world.limits.max_autonomous_per_hour

    def reply_quota(self, state: WorldState) -> tuple[int, int]:
        """本小时被动回复的 ``(已用, 上限)``（上限 0 = 不限制）。

        分过组的会话共用同一份状态，所以这里天然就是"整组的额度"。
        """

        hour = self._hour_index(state)
        used = int(state.reply_count_hour or 0) if state.reply_hour_marker == hour else 0
        return used, max(0, int(self.world.limits.max_replies_per_hour or 0))

    def _passive_reply_allowed(self, state: WorldState) -> bool:
        """这一条被动回复还在额度内吗。"""

        used, limit = self.reply_quota(state)
        return limit <= 0 or used < limit

    def _count_passive_reply(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.reply_hour_marker != hour:
            state.reply_hour_marker = hour
            state.reply_count_hour = 0
        state.reply_count_hour = int(state.reply_count_hour or 0) + 1

    # ---------------- 动作的使用次数上限 ----------------

    def _usage_keys(self) -> dict[str, str]:
        """当前这三种周期各自的键（本地时间；换天/换周/换月自动换键 = 重新计数）。"""

        now = self.local_now()
        iso = now.isocalendar()
        return {
            "day": f"d:{now.strftime('%Y-%m-%d')}",
            "week": f"w:{iso[0]}-{iso[1]:02d}",
            "month": f"m:{now.strftime('%Y-%m')}",
        }

    def action_quota_left(self, state: WorldState, action: ActionDef) -> int:
        """这个动作还剩几次可用（没有配额就返回一个很大的数）。"""

        quota = getattr(action, "quota", None)
        limits = quota.limits() if hasattr(quota, "limits") else {}
        table = dict((state.action_usage or {}).get(action.id) or {})
        keys = self._usage_keys()
        left: list[int] = []
        for period, limit in limits.items():
            if not limit or limit <= 0:
                continue
            key = keys.get(period, "")
            used = int(table.get(key, 0) or 0)
            left.append(max(0, int(limit) - used))
        return min(left) if left else 10 ** 6

    def exhausted_actions(self, state: WorldState) -> set[str]:
        """这一轮不能再用的动作 id（配额用尽）。"""

        return {
            action.id
            for action in self.world.actions
            if self.action_quota_left(state, action) <= 0
        }

    def _count_action_use(self, state: WorldState, action: ActionDef) -> None:
        """记一次动作使用（只记真正开始执行的那些）。"""

        table = dict((state.action_usage or {}).get(action.id) or {})
        for key in self._usage_keys().values():
            table[key] = int(table.get(key, 0) or 0) + 1
        # 只留最近的一小段，免得这个字典无限长
        if len(table) > 12:
            table = dict(sorted(table.items())[-12:])
        state.action_usage = {**(state.action_usage or {}), action.id: table}

    def _arrival_decision_allowed(self, state: WorldState) -> bool:
        """「走到新地方」这类决策有独立的每小时上限（不占自主行动额度）。"""

        hour = self._hour_index(state)
        if state.arrival_hour_marker != hour:
            return True
        limit = max(0, int(self.world.limits.max_arrival_decisions_per_hour))
        return state.arrival_count_hour < limit

    def _count_arrival_decision(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.arrival_hour_marker != hour:
            state.arrival_hour_marker = hour
            state.arrival_count_hour = 0
        state.arrival_count_hour += 1

    def _count_share(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.share_hour_marker != hour:
            state.share_hour_marker = hour
            state.share_count_hour = 0
        state.share_count_hour += 1

    def _share_allowed(self, state: WorldState) -> bool:
        hour = self._hour_index(state)
        if state.share_hour_marker != hour:
            return True
        return state.share_count_hour < self.world.limits.max_share_per_hour

    # ---------------- LLM 预算 ----------------

    def _llm_plan_allowed(self, state: WorldState) -> bool:
        """是否还允许问 LLM 要计划（间隔 + 每小时次数双重限制）。"""

        limits = self.world.limits
        if self._now() - float(state.last_llm_plan_at or 0.0) < max(
            0, int(limits.llm_plan_min_interval_seconds)
        ):
            return False
        hour = self._hour_index(state)
        if state.llm_plan_hour_marker != hour:
            return True
        return state.llm_plan_count_hour < max(0, int(limits.max_llm_plan_per_hour))

    def _count_llm_plan(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.llm_plan_hour_marker != hour:
            state.llm_plan_hour_marker = hour
            state.llm_plan_count_hour = 0
        state.llm_plan_count_hour += 1
        state.last_llm_plan_at = self._now()

    def _llm_text_allowed(self, state: WorldState) -> bool:
        limits = self.world.limits
        hour = self._hour_index(state)
        if state.llm_text_hour_marker != hour:
            return True
        return state.llm_text_count_hour < max(0, int(limits.max_llm_text_per_hour))

    def _count_llm_text(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.llm_text_hour_marker != hour:
            state.llm_text_hour_marker = hour
            state.llm_text_count_hour = 0
        state.llm_text_count_hour += 1

    def _fallback_text(self) -> str:
        """她没话可说时的兜底：**什么都不说**。

        以前这里从一句写死的通用短句池里随机挑一句（"……有人在吗？"）。
        那些句子跟任何人设都不搭，说出来只会让群里看到一句不属于这个角色的话；
        保持安静反而更像一个真的人。
        """

        return ""

    # ---------------- 群聊上下文 / 插话 ----------------

    def _watermark(self, state: WorldState, session_id: str = "") -> tuple[float, int]:
        """这个会话的"已回应水位线"。

        分过组时留档只有一份，但"回过话"是逐会话的：在群里答过，不该把
        私聊里还没答的留言也一起标成答过了。
        """

        target = str(session_id or state.session_id)
        if target == str(state.session_id):
            return (
                float(state.chat_replied_until or 0.0),
                int(state.chat_replied_seq or 0),
            )
        info = (getattr(state, "chat_watermarks", None) or {}).get(target) or {}
        return float(info.get("until") or 0.0), int(info.get("seq") or 0)

    def _set_watermark(
        self, state: WorldState, session_id: str, *, until: float, seq: int
    ) -> None:
        target = str(session_id or state.session_id)
        if target == str(state.session_id):
            state.chat_replied_until = float(until)
            state.chat_replied_seq = int(seq)
            return
        state.chat_watermarks = {
            **(getattr(state, "chat_watermarks", None) or {}),
            target: {"until": float(until), "seq": int(seq)},
        }

    def chat_context(
        self, state: WorldState, session_id: str = ""
    ) -> list[dict[str, Any]]:
        """进提示词的最近群聊（时间窗 + 条数双重限制）。

        注意：这里只裁剪「带进提示词」的那份视图，原始留档由 :meth:`note_presence`
        按 `context.chat_history_max` 维护，所以重启后还能恢复。
        """

        config = self.world.decider
        until, seq = self._watermark(state, session_id)
        return state.recent_chat_within(
            now=self._now(),
            seconds=max(60, int(config.chat_window_minutes) * 60),
            limit=max(1, int(self.world.context.chat_lines)),
            # 已经回应过的消息不再回放：她对那些话已经答过了，再带进去只会重复回应
            after=until,
            after_seq=seq,
            # 只算**这里**的留言：别处的话归别处，不然她会在群里答私聊的问题
            only_session=str(session_id or state.session_id),
        )

    def chat_window(
        self, state: WorldState, session_id: str = ""
    ) -> list[dict[str, Any]]:
        """时间窗内的全部群聊（含她已经回应过的那批）。

        提示词需要完整的一段：水位线以前的内容会被压成一条概览、之后的原样列出，
        但两边都不该从上下文里消失（否则她下一轮就像失忆）。

        时间窗只管「还没回应过」的那批；**她已经回过话的部分不受时间限制**——
        聊过一个小时之后，"刚才在说什么"不该只剩一行概览（那会导致接不上话）。

        「最多带几行」是**按会话各自算**的：这里那一份取 `chat_lines` 行，
        别处同时听到的取 `chat_elsewhere_lines` 行。以前是在所有会话的留档上一起取
        "最近 N 行"，于是私聊聊得多的时候，群里刚说的话会被挤出去。
        """

        config = self.world.decider
        context = self.world.context
        target = str(session_id or state.session_id or "")
        rows = state.chat_window(
            now=self._now(),
            seconds=max(60, int(config.chat_window_minutes) * 60),
        )
        here: list[dict[str, Any]] = []
        elsewhere: list[dict[str, Any]] = []
        for item in rows:
            if self._chat_origin(item, state) == target:
                here.append(item)
            else:
                elsewhere.append(item)
        picked = [
            *take_last_chat_groups(here, max(1, int(context.chat_lines))),
            *take_last_chat_groups(elsewhere, max(0, int(context.chat_elsewhere_lines))),
        ]
        picked.sort(key=lambda item: int(item.get("seq") or 0))
        return self._merge_answered_chat(state, target, picked)

    @staticmethod
    def _chat_origin(item: dict[str, Any], state: WorldState) -> str:
        """一条留档算在哪个会话头上（老条目没有 origin：算作她的存档会话）。"""

        return str(item.get("origin") or state.session_id or "")

    def _merge_answered_chat(
        self, state: WorldState, target: str, current: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把最近这批"已经回过话"的原样补进来（不限时间），按 seq 去重后按时间排好。"""

        until, seq = self._watermark(state, target)
        limit = max(0, int(self.world.context.chat_answered_lines))
        extra: list[dict[str, Any]] = []
        for item in reversed(list(state.recent_chat or [])):
            if str(item.get("origin") or state.session_id) != target:
                continue
            if chat_item_is_fresh(item, replied_until=until, replied_seq=seq):
                continue
            extra.append(item)
        # 按"合并后的行"限额（同一个人连着说的算一行）
        extra = take_last_chat_groups(list(reversed(extra)), limit)
        if not extra:
            return current
        seen = {int(item.get("seq") or 0) for item in current}
        # extra 已经是"从早到晚"（上面 reverse 回来过），直接按顺序补在前面
        older = [item for item in extra if int(item.get("seq") or 0) not in seen]
        return [*older, *current]

    def chat_context_for_reply(
        self, state: WorldState, ctx: MessageContext
    ) -> list[dict[str, Any]]:
        """接管回复用的群聊背景：把"刚进来的这一条"从背景里摘掉。

        这条消息紧接着会以「XX 对你说：…」的形式单独交给模型，留在背景里
        等于同一句话在提示词里出现两遍——模型会以为对方把话重复说了好几次。
        """

        context = self.chat_window(state, ctx.session_id)
        if not context:
            return context
        last = context[-1]
        if last.get("is_self"):
            return context
        same_user = str(last.get("user_id") or "") == str(ctx.user_id or "unknown")
        if same_user and _same_text(str(last.get("text") or ""), str(ctx.text or "")):
            return context[:-1]
        return context

    async def quote_in_records(self, session_id: str, text: str) -> bool:
        """被引用的那条，聊天记录里还看得到吗。

        看得到就只给她一个开头（让她自己往上对照：省 token，也更像人）；
        看不到（很久以前的、已经掉出留档的）才需要把原文写进提示词。
        """

        probe = " ".join(str(text or "").split())[:24]
        if not probe or not self.is_enabled(session_id):
            return False
        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return False
        # 只看"这一轮真的会带进提示词"的那一份（时间窗 + 已回过的那截），
        # 原始留档里还留着、但早就没带进去的那几条，说了"照上面看"她也找不到
        for item in self.chat_window(state, session_id):
            body = " ".join(str(item.get("text") or "").split())
            if probe in body:
                return True
        return False

    async def chat_images_for_reply(
        self, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """聊天记录里最近几张图（可以直接交给多模态主模型看的那几张）。

        返回按时间排好、带 ``label``（图1 / 图2…）——编号要和这一轮真的附上去的
        那几张一一对应，提示词里的「（见图N）」才有意义。
        """

        if int(limit or 0) <= 0 or not self.is_enabled(session_id):
            return []
        async with self.session_state(session_id) as state:
            return self.pick_chat_images(state, session_id, int(limit))

    def pick_chat_images(
        self, state: WorldState, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """（调用方负责持锁）取这个会话聊天记录里最近的几张图。"""

        limit = max(0, int(limit or 0))
        if limit <= 0:
            return []
        target = str(session_id or state.session_id)
        items = [
            item
            for item in self.chat_window(state, target)
            if str(item.get("origin") or state.session_id) == target
            and not item.get("is_self")
        ]
        picks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in reversed(items):
            for ref in reversed(list(item.get("images") or [])):
                key = str(ref or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                picks.append({"url": key, "at": float(item.get("at") or 0.0)})
                if len(picks) >= limit:
                    break
            if len(picks) >= limit:
                break
        picks.reverse()
        for index, pick in enumerate(picks, start=1):
            pick["label"] = f"图{index}"
        return picks

    def mark_chat_replied(self, state: WorldState, session_id: str = "") -> None:
        """她真的开口了：把这一批群聊压成概览，并把"已回应水位线"推到当前。

        水位线之后的消息才是"还没回应过的"，会原样进提示词；水位线以内（也就是她刚
        回应过的这批）压成一条概览，供下一轮了解背景，不再重复回应。
        """

        now = self._now()
        target = str(session_id or state.session_id)
        previous, previous_seq = self._watermark(state, target)
        batch = [
            item
            for item in state.recent_chat
            if (
                str(item.get("origin") or state.session_id) == target
                and chat_item_is_fresh(
                    item,
                    replied_until=previous,
                    replied_seq=previous_seq,
                )
            )
        ]
        preview = _summarize_batch(batch)
        if preview:
            # 按会话存：群里回的那批不该跑到私聊里当"刚聊过的"
            state.chat_previews = {
                **dict(state.chat_previews or {}),
                target: {"text": preview, "at": now},
            }
        _, seq_now = self._watermark(state, target)
        # 水位线推到"这个会话里最新那条"：跨会话的水位线不能互相顶掉
        session_seq = max(
            [
                int(item.get("seq") or 0)
                for item in state.recent_chat
                if str(item.get("origin") or state.session_id) == target
            ]
            or [int(state.chat_seq or 0)]
        )
        self._set_watermark(
            state, target, until=now, seq=max(session_seq, seq_now)
        )

    async def mark_chat_replied_by_session(self, session_id: str) -> None:
        """按会话推进"已回应水位线"（注入模式下主人格替她回复时用）。"""

        if not self.is_enabled(session_id):
            return
        async with self.session_state(session_id) as state:
            self.mark_chat_replied(state, session_id)

    def note_chat_note(
        self, state: WorldState, text: str, session_id: str = ""
    ) -> None:
        """记下「刚才在聊什么」（模型顺手写的），下一轮当背景用。

        **按会话存**：一个会话组里群和私聊共用一份状态，写在一起的后果是
        私聊刚聊的背景跑到群里当"这里刚才在聊的事"。
        """

        note = " ".join(str(text or "").split())
        if not note:
            return
        target = str(session_id or state.session_id)
        state.chat_notes = {
            **dict(state.chat_notes or {}),
            target: {"text": _clip_text(note, 80), "at": self._now()},
        }

    def chat_note_for(self, state: WorldState, session_id: str = "") -> str:
        """这个会话「刚才在聊什么」（老存档回落全局那份；过期的不算）。"""

        limit = max(0, int(getattr(self.world.context, "chat_note_max_minutes", 30) or 0))
        return state.chat_note_text(session_id, max_minutes=limit, now=self._now())

    def chat_preview_for(self, state: WorldState, session_id: str = "") -> str:
        """这个会话「她刚回应过的那批」的概览（留档被裁掉时的兜底背景）。"""

        return state.chat_preview_text(session_id)

    def _chat_counts(self, state: WorldState, session_id: str = "") -> dict[str, int]:
        """编辑器「群聊上下文」那几个数：**只算这个会话**，而且按"合并后的行"。

        以前"已回应"是用留档总数减出来的——那个差值里混着别处会话的消息和时间窗之外
        的消息，数字看着大，其实跟她看到的对不上。
        """

        target = str(session_id or state.session_id or "")
        view = self.chat_window(state, target)
        here = [item for item in view if self._chat_origin(item, state) == target]
        until, seq = self._watermark(state, target)
        fresh: list[dict[str, Any]] = []
        answered: list[dict[str, Any]] = []
        for item in here:
            fresh_flag = chat_item_is_fresh(
                item, replied_until=until, replied_seq=seq
            )
            (fresh if fresh_flag else answered).append(item)
        return {
            "chat_history_count": sum(
                1 for item in state.recent_chat if self._chat_origin(item, state) == target
            ),
            "chat_unreplied_count": len(group_chat_items(fresh)),
            "chat_replied_count": len(group_chat_items(answered)),
        }

    def group_is_chatting(self, state: WorldState) -> bool:
        """群里最近是否真的有人在聊。"""

        need = max(1, int(self.world.decider.min_messages_to_interject))
        return len(self.chat_context(state)) >= need

    def group_is_alive(self, state: WorldState, *, minutes: int = 0) -> bool:
        """群里最近有没有人在说话（她自己的话不算）。

        和 :meth:`group_is_chatting` 的区别：那个问的是"有没有新消息等着她回"，
        这个问的是"群里现在有人吗"。开口求助看的是后者——群里空着的时候喊一声
        「有人吗」，群友视角就是一句没头没尾的怪话。
        """

        window = max(1, int(minutes or self.world.decider.chat_window_minutes or 20)) * 60
        for item in state.chat_window(now=self._now(), seconds=window):
            if item.get("is_self"):
                continue
            if str(item.get("text") or "").strip():
                return True
        return False

    @staticmethod
    def plan_speaks(plan: dict[str, Any] | None) -> bool:
        """这份计划里有没有"她主动开口"的动作。"""

        for step in (plan or {}).get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("interject"):
                return True
            if str(step.get("action") or "") in ("say", "share"):
                return True
        return False

    def willingness(self, state: WorldState) -> float:
        """她对「现在开口」的整体意愿（0~1）：给决策器与外部联动共用。"""

        return reply_willingness(state)

    # ---------------- 群聊上下文：留档与压缩 ----------------

    def chat_history_limit(self) -> int:
        return max(20, int(self.world.context.chat_history_max))

    def chat_summary_text(self, state: WorldState) -> str:
        """默认取"存档会话"那一份（老存档回落到全局摘要）。"""

        return self.chat_summary_for(state, state.session_id)

    def chat_summary_for(self, state: WorldState, session_id: str) -> str:
        """某个会话"更早聊过的"那一段（分会话存；老存档只有全局那份）。"""

        key = str(session_id or state.session_id or "")
        entry = dict((state.chat_summaries or {}).get(key) or {})
        text = str(entry.get("text") or "").strip()
        if text:
            return text
        return str(state.chat_summary or "").strip()

    def chat_summary_map(self, state: WorldState) -> dict[str, str]:
        """每个会话一份"更早聊过的"，按"最近更新"排序（提示词按这个顺序渲染）。"""

        rows: list[tuple[float, str, str]] = []
        for key, entry in dict(state.chat_summaries or {}).items():
            info = dict(entry or {}) if isinstance(entry, dict) else {}
            text = str(info.get("text") or "").strip()
            if not text:
                continue
            try:
                at = float(info.get("at") or 0.0)
            except (TypeError, ValueError):
                at = 0.0
            rows.append((at, str(key), text))
        if not rows and str(state.chat_summary or "").strip():
            # 老存档（升级前压过摘要）：归到存档会话名下
            rows.append(
                (
                    float(state.chat_summary_at or 0.0),
                    str(state.session_id),
                    str(state.chat_summary).strip(),
                )
            )
        rows.sort(key=lambda item: item[0])
        return {key: text for _at, key, text in rows}

    async def clear_chat_context(self, session_id: str) -> dict[str, Any]:
        """清空**这个会话**的群聊留档与摘要（调试用：想看她"第一次听到"的反应时很有用）。

        会话组里别的群 / 私聊的留档与摘要不受影响——它们本来就是分开记的。
        """

        async with self.session_state(session_id) as state:
            target = str(session_id or state.session_id)
            def _from_here(item: dict[str, Any]) -> bool:
                return str(item.get("origin") or state.session_id) == target

            kept = [item for item in state.recent_chat if not _from_here(item)]
            removed = len(state.recent_chat) - len(kept)
            had_summary = bool(self.chat_summary_for(state, target))
            state.recent_chat = kept
            state.chat_dropped = [
                item for item in list(getattr(state, "chat_dropped", None) or []) if not _from_here(item)
            ]
            state.chat_summaries = {
                key: value
                for key, value in dict(state.chat_summaries or {}).items()
                if str(key) != target
            }
            if target == str(state.session_id):
                # 老字段（全局摘要）就是这个会话的，一起清掉
                state.chat_summary = ""
            await self._log_event(
                state,
                "context",
                {
                    "note": "手动清空群聊上下文",
                    "removed": removed,
                    "session": self.session_label(target, state),
                },
            )
        return {"removed": removed, "had_summary": had_summary}

    async def _maybe_compress_chat(
        self, state: WorldState, outcome: TickOutcome
    ) -> None:
        """留档攒到阈值时，把较早的部分交给压缩模型压成一段摘要。

        **按会话分开压**：一个会话组里的群和私聊共用一份留档，一起压的话
        群里聊的和私聊聊的会混进同一段摘要（提示词里她就分不清哪句是哪儿的）。
        """

        config = self.world.context
        if str(config.chat_overflow) != "compress":
            await self._warn_chat_overflow(state)
            return
        threshold = max(10, int(config.chat_compress_threshold))
        refresh = max(60, int(config.summary_refresh_minutes)) * 60
        keep = max(1, int(config.chat_keep_after_compress))
        now = self._now()
        dropped = list(getattr(state, "chat_dropped", None) or [])
        # 按"在哪儿说的"分堆：先放"被上限顶掉、还没进过摘要"的那几条（它们更早），
        # 不带上就等于凭空消失了（聊得快的时候正是这样丢的记忆）。
        buckets: dict[str, list[dict[str, Any]]] = {}
        for item in [*dropped, *state.recent_chat]:
            key = str(item.get("origin") or state.session_id)
            buckets.setdefault(key, []).append(item)
        dropped_ids = {id(item) for item in dropped}
        summaries: dict[str, dict[str, Any]] = {
            str(key): dict(info)
            for key, info in dict(state.chat_summaries or {}).items()
            if isinstance(info, dict)
        }
        keep_ids: set[int] = set()
        compressed = 0
        for key, items in buckets.items():
            # 有"被上限顶掉、还没进过摘要"的那几条时必须压：不压就等于凭空消失
            has_dropped = any(id(item) in dropped_ids for item in items)
            if len(items) < threshold and not has_dropped:
                keep_ids.update(id(item) for item in items)
                continue
            last_at = float((summaries.get(key) or {}).get("at") or 0.0)
            if last_at <= 0.0:
                # 老存档只有一个全局的节流时间：第一次按它算
                last_at = float(state.chat_summary_at or 0.0)
            if now - last_at < refresh:
                keep_ids.update(id(item) for item in items)
                continue
            older = items[: max(0, len(items) - keep)]
            if not older:
                keep_ids.update(id(item) for item in items)
                continue
            summary = await self._summarize_chat(state, older, session_id=key)
            if not summary:
                keep_ids.update(id(item) for item in items)
                continue
            summaries[key] = {"text": summary, "at": now}
            compressed += len(older)
            keep_ids.update(id(item) for item in items[-keep:])
        if compressed <= 0:
            return
        state.chat_summaries = summaries
        state.chat_summary_at = now
        # 已经按会话存了，老字段清掉：留着会在提示词里重复出现一次
        state.chat_summary = ""
        state.recent_chat = [item for item in state.recent_chat if id(item) in keep_ids]
        state.chat_dropped = [item for item in dropped if id(item) in keep_ids]
        outcome.notes.append(f"已把较早的 {compressed} 条群聊压成摘要")
        await self._log_event(
            state,
            "context",
            {
                "note": "压缩较早群聊",
                "compressed": compressed,
                "kept": keep,
                "sessions": len(buckets),
            },
            outcome=outcome,
        )

    async def _warn_chat_overflow(self, state: WorldState) -> None:
        """留档已经顶到上限、而且是"直接丢弃"：提醒一次，免得她"忘事"却查不出原因。

        一小时最多提醒一次。
        """

        limit = self.chat_history_limit()
        # 留档是按会话各自留的：这里要看"最多的那个会话"够没够上限
        buckets: dict[str, int] = {}
        for item in state.recent_chat:
            key = self._chat_origin(item, state)
            buckets[key] = buckets.get(key, 0) + 1
        if max(buckets.values(), default=0) < limit:
            return
        now = self._now()
        if now - float(getattr(state, "chat_overflow_warned_at", 0.0) or 0.0) < 3600:
            return
        state.chat_overflow_warned_at = now
        await self._log_event(
            state,
            "context",
            {
                "note": "群聊留档已满，最早的会被直接丢弃",
                "hint": "想让她记得久一点：全局设置 → 上下文 → 留档超了怎么办 → 压成摘要",
                "limit": limit,
            },
        )

    async def _summarize_chat(
        self,
        state: WorldState,
        history: list[dict[str, Any]],
        *,
        session_id: str = "",
    ) -> str:
        if self.context_llm is None or not history:
            return ""
        limit = max(40, int(self.world.context.history_max_chars))
        lines = []
        for item in history:
            who = "她" if item.get("is_self") else (item.get("name") or item.get("user_id") or "")
            text = _clip_text(item.get("text"), limit)
            if text:
                lines.append(f"- {who}: {text}")
        if not lines:
            return ""
        # 只接着"这个会话"的上一份摘要往下写：群里和私聊的脉络本来就不一样
        previous = (
            self.chat_summary_for(state, session_id)
            if str(session_id or "").strip()
            else self.chat_summary_text(state)
        )
        system_prompt = (
            "你负责把群聊记录整理成一段背景摘要，供同一个角色之后回忆这段日子。\n"
            "要写清：参与的人（谁在跟谁说话）、正在聊的话题及其来龙去脉、"
            "已经约定或承诺过的事、还没回应的事，以及**具体细节**"
            "（时间、数字、物品、称呼、喜欢/讨厌的东西）——细节比概括有用得多，"
            "她后面要靠它接话。\n"
            "已经有更早的摘要时：接着它往下写，把仍然成立的事并进去，过时的不再写。"
            "不要逐条复述、不要评价、不要编造。只输出摘要本身，不超过 600 字。"
        )
        prompt = (
            (f"已有的更早摘要：\n{previous}\n\n" if previous else "")
            + "需要压缩的群聊记录：\n"
            + "\n".join(lines)
            + "\n\n请输出更新后的摘要。"
        )
        try:
            reply = await self.context_llm.generate(
                session_id=state.session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.2,
            )
        except Exception as exc:
            self._log("debug", f"上下文压缩失败：{exc}")
            return ""
        if not getattr(reply, "ok", False):
            return ""
        return str(reply.text or "").strip()[:600]

    def interject_allowed(self, state: WorldState) -> bool:
        """是否允许主动插话：插话冷却 + 无人回应冷却 + 每小时上限。"""

        return self.interject_gate(state) == ""

    def interject_gate(self, state: WorldState) -> str:
        """插话被哪道闸拦住（空字符串 = 放行）。

        分工：`cooldown` 是两次插话的间隔，`engage` 是无人回应保护，
        `hourly` 是每小时自主额度，`reply_cd` 是"刚回过话就别主动开口"。
        把它们分开报出来，才能知道到底是谁在限流。
        """

        if not self.world.decider.enabled:
            return "disabled"
        cooldown = max(0, int(self.world.decider.interject_cooldown_minutes)) * 60
        if self._now() - float(state.last_interject_at or 0.0) < cooldown:
            return "cooldown"
        if not self.engagement.can_speak(state):
            return "engage"
        if not self._within_hourly_limit(state):
            return "hourly"
        if self.engagement.proactive_blocked(state):
            return "reply_cd"
        return ""

    def _count_interject_gate(self, state: WorldState, gate: str) -> None:
        """记一笔「她这轮想插话，结果如何」——按小时归零。"""

        hour = int(self._now() // 3600)
        if state.interject_hour_marker != hour:
            state.interject_hour_marker = hour
            state.interject_stats = {}
        key = gate or "allowed"
        stats = dict(state.interject_stats or {})
        stats[key] = int(stats.get(key, 0)) + 1
        state.interject_stats = stats

    def _refresh_interject_closed(self, state: WorldState) -> None:
        """长期低落时关掉「想被注意到」这条插话动机，并且至少关 15 分钟。

        动机 B（想发作）不受影响：被惹毛的人反而是想开口的。
        """

        now = self._now()
        if float(state.valence) >= 0.2:
            return
        started = float(state.low_valence_since or 0.0)
        if not started or now - started < 30 * 60:
            return
        state.interject_closed_until = max(
            float(state.interject_closed_until or 0.0), now + 15 * 60
        )

    def _forced_plan_allowed(self, state: WorldState) -> bool:
        """极端保护也要守规矩：间隔、每小时上限、无人回应冷却一个都不能少。"""

        limits = self.world.limits
        if self._now() - float(state.last_forced_plan_at or 0.0) < max(
            0, int(limits.forced_plan_min_interval_seconds)
        ):
            return False
        if not self._within_hourly_limit(state):
            return False
        if not self.engagement.can_speak(state):
            return False
        return True

    # ================= 群名片 =================

    async def _sync_nickname(self, state: WorldState, node: NodeDef | None) -> None:
        config = self.world.nickname_sync
        if not config.enabled or self.messenger is None:
            return
        if state.bot_nickname_locked:
            return
        # 一个人的名片要跟着她走：组里的每个群都改一遍（私聊没有群名片，跳过）
        targets = [
            str(item)
            for item in self.group_sessions(state.session_id)
            if ":GroupMessage:" in str(item) and self.is_enabled(str(item))
        ]
        if not targets:
            return
        primary = targets[0]
        # 第一次遇到这个会话时，先把她当前的名片读回来当"原名"
        if not state.bot_base_nickname:
            try:
                fetched = await self.messenger.fetch_group_card(primary)
            except Exception as exc:
                self._log("debug", f"读取群名片失败: {exc}")
                fetched = ""
            if fetched:
                state.bot_base_nickname = fetched
                state.bot_current_nickname = state.bot_current_nickname or fetched
                await self._log_event(
                    state, "nickname", {"base": fetched, "note": "记下了她原来的群名片"}
                )
        desired = compute_nickname(self.world, state, node, base=state.bot_base_nickname)
        now = self._now()
        # 连续失败时按倍数退避（1× → 2× → 4× …，最多 1 小时一次），
        # 否则协议端一挂，这里会每隔一个冷却就重试并打一条错误日志。
        cooldown = max(10, int(config.cooldown_seconds))
        backoff = cooldown * (2 ** min(int(state.nickname_fail_count or 0), 6))
        if self._messenger_blocked(state.session_id) or state.nickname_fail_count:
            backoff = max(backoff, 300)
        if not should_update(
            state,
            desired,
            now=now,
            cooldown_seconds=min(backoff, 3600),
        ):
            return
        try:
            result = await self.messenger.set_group_card(primary, desired)
        except Exception as exc:
            result = CardResult(ok=False, reason=str(exc), card=desired)
        if result.ok:
            state.bot_current_nickname = desired
            state.last_nickname_update_at = now
            state.nickname_fail_count = 0
            await self._log_event(state, "nickname", {"to": desired, "ok": True})
            # 同一个她在别的群里的名片跟着一起改：不再各算一次冷却
            for session_id in targets[1:]:
                try:
                    await self.messenger.set_group_card(session_id, desired)
                except Exception as exc:
                    self._log("debug", f"同步群名片到 {session_id} 失败：{exc}")
            return
        outcome_reason = result.reason or "没有说明原因"
        state.last_nickname_update_at = now  # 失败也占一次冷却，避免每 tick 重试
        state.nickname_fail_count = min(int(state.nickname_fail_count or 0) + 1, 8)
        await self._log_event(
            state, "nickname", {"to": desired, "ok": False, "note": outcome_reason}
        )
        # 只在第一次失败时打日志，后面交给退避；（事件日志里每一条都还在，方便排查）
        if state.nickname_fail_count == 1:
            self._log(
                "debug",
                f"群名片没改成：{outcome_reason}（接下来会按 5 分钟起退避重试）",
            )

    # ================= 发送 =================

    def _messenger_blocked(self, session_id: str) -> bool:
        """发送方是否正处在"刚失败"的冷却里（宿主适配器不一定实现这个方法）。"""

        if self.messenger is None:
            return False
        checker = getattr(self.messenger, "blocked", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(session_id))
        except Exception:
            return False

    def _image_home(self, outcome: TickOutcome | None, action: Any = None) -> str:
        """这一步生成的图该落在哪个会话。

        和同一步的正文必须是**同一个落点**：正文走 ``add_speech(text, 落点)``，
        图以前一律挂在 ``outcome.session_id`` 上，于是私聊里起的动作会把图
        发到群里（正文私聊、图片群聊）。

        优先级：计划里冻结的落点（``speech_home``）→ 这一步自己带的落点 →
        这一轮的会话（兜底）。兜底意味着"没定下落点"，多会话时很容易发错地方，
        所以补一条警告日志。
        """

        if outcome is None:
            return ""
        frozen = str(getattr(outcome, "speech_home", "") or "").strip()
        if frozen:
            return frozen
        stepped = str(self._act_get(action, "session", "") or "").strip() if action is not None else ""
        if stepped:
            return stepped
        session_id = str(getattr(outcome, "session_id", "") or "").strip()
        place = str(getattr(outcome, "place", "") or "").strip()
        if place:
            # 有人跟她说话、或者规则决策刚敲定：这一轮发生在哪儿是确定的，
            # 图落在那儿本来就对，没什么可警告的
            return session_id
        if len(self.group_sessions(session_id)) > 1:
            # 单会话时"没定落点"是正常事，不用吵；多会话才是真隐患
            # （老计划里的步骤没写 session 就会走到这儿）
            self._log(
                "warning",
                f"这一轮没定下落点，图片按当前会话发：{session_id}",
            )
            outcome.notes.append("这一轮没定下落点，图片按当前会话发")
        return session_id

    def _attach_images(
        self,
        outcome: TickOutcome | None,
        images: list[Any] | None,
        *,
        session_id: str = "",
    ) -> None:
        """把工具 / 指令返回的图片挂到这一轮的结果上（去重、限量）。

        只挂在"这一轮的结果"上，不写进动作状态——生图工具给的常常是 base64，
        落进 state 里会把存档撑得很丑。

        ``session_id`` 是这些图要落在哪个会话：留空 = 这一轮的会话；
        给了别的会话（私聊里起的动作，而这一拍推的是群里）就投到那个会话去，
        ``_deliver`` 会照着分开发。图和正文分家就是这么来的，所以这里必须收落点。
        """

        if outcome is None or not images:
            return
        target = str(session_id or "").strip() or str(outcome.session_id or "")
        refs = [str(item or "").strip() for item in images]
        refs = [item for item in refs if item]
        if not refs:
            return
        if target and target != str(outcome.session_id or ""):
            outcome.add_images(refs, target)
            return
        for ref in refs:
            if ref not in outcome.images:
                outcome.images.append(ref)
        if len(outcome.images) > MAX_GROUP_IMAGES:
            outcome.notes.append(
                f"这一轮生成的图片太多（{len(outcome.images)} 张），只贴前 {MAX_GROUP_IMAGES} 张"
            )
            del outcome.images[MAX_GROUP_IMAGES:]

    def _note_own_image(
        self, state: WorldState, label: str, session_id: str = ""
    ) -> None:
        """她发出去的图片也要进"她自己的聊天记录"。

        不然下一轮有人引用那张图，她只会看到"对方引用了某某发的［图片］"，
        既不知道那图是自己发的、也想不起来自己刚发过照片。
        """

        note = " ".join(str(label or "").split())[:40]
        text = f"［我发了一张图片：{note}］" if note else "［我发了一张图片］"
        state.note_chat(
            user_id="__self__",
            name=state.bot_current_nickname or state.bot_base_nickname or "你",
            text=text,
            now=self._now(),
            keep=self.chat_history_limit(),
            is_self=True,
            origin=session_id or state.session_id,
        )
        self._tag_chat_origin(state, session_id or state.session_id)

    def _tag_chat_origin(self, state: WorldState, session_id: str) -> None:
        """给刚记下的那条留档标上"在哪儿说的"。

        分过组的会话共用同一份留档，别的地方说的话要标出来源——
        提示词里她才知道那句不是当前这个群里说的。
        """

        if not state.recent_chat:
            return
        item = state.recent_chat[-1]
        origin = str(session_id or state.session_id)
        item["origin"] = origin
        item.pop("via", None)

    def session_labels(self, state: WorldState) -> dict[str, str]:
        """通讯录里每个会话怎么称呼：渲染"你在别处听到的"时用。"""

        labels: dict[str, str] = {}
        for session_id in self.group_sessions(state.session_id):
            labels[str(session_id)] = self.session_label(session_id, state)
        return labels

    def reply_place(self, state: WorldState, session_id: str) -> str:
        """这条消息来自哪个会话：多会话（同一个她有好几个地方）时才写。"""

        if len(self.group_sessions(state.session_id)) <= 1:
            return ""
        return self.session_label(session_id, state)

    def _note_session_activity(self, state: WorldState, ctx: MessageContext) -> None:
        """记下"他在哪个会话里跟她说话"。

        她挑落点、提示词标来源、通讯录里的"最近有人跟你说话是……"都读这一份。
        """

        now = self._now()
        record = dict(state.user_presence.get(ctx.user_id) or {})
        record["session"] = ctx.session_id
        state.user_presence[ctx.user_id] = record
        state.session_activity[ctx.session_id] = {
            "at": now,
            "user_id": ctx.user_id,
            "user_name": ctx.user_name or ctx.user_id,
        }
        if ctx.group_name:
            self.note_session_name(ctx.session_id, ctx.group_name)

    def _group_images(self, outcome: TickOutcome) -> list[str]:
        """这一轮真要发出去的图片（去重、限量，顺序保持生成顺序）。"""

        picked: list[str] = []
        for item in list(outcome.images or []):
            ref = str(item or "").strip()
            if ref and ref not in picked:
                picked.append(ref)
        return picked[:MAX_GROUP_IMAGES]

    async def _send_group_images(self, session_id: str, images: list[str]) -> bool:
        """把结果图发到群里。发送通道没实现这个方法时安静跳过。"""

        sender = getattr(self.messenger, "send_images", None)
        if not callable(sender):
            return False
        try:
            return bool(await sender(session_id, list(images)))
        except Exception as exc:
            # 图片发送失败不算"她没说话"：只留一条日志，别把这一轮的其他内容也搅黄
            self._log("warning", f"发送结果图片失败：{exc}")
            return False

    async def _deliver(self, outcome: TickOutcome) -> None:
        if self.messenger is None:
            return
        # 已经即时发出去的算"她说过话"（进日志、进聊天上下文、算主动发言冷却），
        # 但不再发一遍。
        said = [m for m in outcome.said_messages() if m and str(m).strip()]
        # 下面取的是"还没发出去的那部分"：即时发言已经从 messages 里取走了，
        # 所以按真实顺序合成时不会再把她刚说过的话发第二遍。
        # 说给别的会话的话（群里应付一句、私聊里再吐槽一句）：逐条投过去
        routed = {
            str(sid): [m for m in (msgs or []) if m and str(m).strip()]
            for sid, msgs in (outcome.routed or {}).items()
        }
        routed = {sid: msgs for sid, msgs in routed.items() if msgs}
        routed_images = {
            str(sid): [str(ref) for ref in (refs or []) if str(ref).strip()]
            for sid, refs in (outcome.routed_images or {}).items()
        }
        routed_images = {sid: refs for sid, refs in routed_images.items() if refs}
        pending = self.take_pending_echo(outcome.session_id)
        images = self._group_images(outcome)
        if (
            not said
            and not routed
            and not routed_images
            and not outcome.debug_messages
            and not pending
            and not images
        ):
            return
        # 让她的话和调试回显按真实顺序出现：先调工具、再说话。
        # 门禁攒下的回显（被叫醒之类）发生在这轮之前，排在最前面。
        ordered = [
            m
            for m in (pending + outcome.ordered_messages())
            if m and str(m).strip()
        ]
        state = await self.load_state(outcome.session_id, cold_start=False)
        # 事件里的发言（求助 / 事件结果）不算"她主动找人聊天"：
        # 不走无人回应保护，也不会把她的冷却期拖长。
        is_event_speech = str(outcome.speech_kind or "") == "event"
        # 排在后面的回话（当时有人跟她说话，她在忙，轮到她时才说）同样不算"主动开口"：
        # 被"主动发言冷却"吞掉的话，对方再也等不到那句回应，而计划上却记成"已经说过了"
        is_reply_speech = str(outcome.speech_kind or "") == "reply"
        # 已经即时发出去的几句挡不回来：这一轮不能再看冷却，否则群里会"说了一句就没下文"，
        # 那句即时发言也不进聊天记录（下一轮她就不知道自己刚说过）
        spoke_live = bool(outcome.live_messages)
        if (
            not is_event_speech
            and not is_reply_speech
            and not spoke_live
            and not self.engagement.can_speak(state)
        ):
            outcome.notes.append("冷却期内不发送")
            return
        async with self.session_state(outcome.session_id) as state:
            if said or images or routed or routed_images:
                if not is_event_speech and not is_reply_speech:
                    self.engagement.on_bot_spoke(state)
                await self._log_event(
                    state,
                    "bot_message",
                    {
                        "messages": said,
                        # 生图 / 出图的动作：日志里能看出这一轮除了话还贴了几张图
                        "images": len(images),
                        "style_cell": state.last_style_cell,
                        "say_limit": int(state.last_say_limit or 0),
                    },
                )
                # 自己说过的话也进聊天上下文：模型才知道"刚才那句是我说的"，
                # 并被要求换一种说法，避免每轮都用同一个句式开场。
                for message in said:
                    state.note_chat(
                        user_id="__self__",
                        name=state.bot_current_nickname or state.bot_base_nickname or "你",
                        text=message,
                        now=self._now(),
                        keep=self.chat_history_limit(),
                        is_self=True,
                        origin=outcome.session_id,
                    )
                    state.note_reply(message, session_id=outcome.session_id)
                    self.note_dialogue(state, text=message, is_self=True)
                    # 这句是在哪个会话里说的（留档只有一份，来源要标出来）
                    self._tag_chat_origin(state, outcome.session_id)
                for target, messages in routed.items():
                    await self._log_event(
                        state,
                        "bot_message",
                        {
                            "messages": messages,
                            "session": self.session_label(target, state),
                            "images": len(routed_images.get(target) or []),
                            "note": "这一句说给了另一个会话",
                        },
                    )
                    for message in messages:
                        state.note_chat(
                            user_id="__self__",
                            name=state.bot_current_nickname
                            or state.bot_base_nickname
                            or "你",
                            text=message,
                            now=self._now(),
                            keep=self.chat_history_limit(),
                            is_self=True,
                            origin=target,
                        )
                        state.note_reply(message, session_id=target)
                        self.note_dialogue(state, text=message, is_self=True)
                        self._tag_chat_origin(state, target)
            # 调试用的动作回显不算"她说过的话"：不进聊天上下文、不计无人回应保护
        sent = False
        if ordered:
            sent = await self.messenger.send_text(outcome.session_id, ordered)
        if images:
            await self._send_group_images(outcome.session_id, images)
        for target, messages in routed.items():
            try:
                await self.messenger.send_text(target, messages)
            except Exception as exc:
                outcome.notes.append(f"发到 {target} 失败：{exc}")
                self._log("warning", f"发到 {target} 失败：{exc}")
        for target, refs in routed_images.items():
            await self._send_group_images(target, refs)
        note = getattr(self.messenger, "take_fail_note", None)
        failed_note = note(outcome.session_id) if callable(note) else ""
        if failed_note:
            # 没发出去这件事也要留痕：日志里能看到"这条她说过、但平台没收"
            async with self.session_state(outcome.session_id) as state:
                self.dynamics.apply_event(state, "send_failed", now=self._now())
                await self._log_event(
                    state,
                    "send_failed",
                    {"messages": ordered, "note": failed_note},
                )
        elif not sent and ordered:
            outcome.notes.append("平台没有接收这批消息")

    # ================= 对外快照 =================

    # ---------------- 编辑器「实时状态」用的几个小查询 ----------------

    WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    def _clock_text(self) -> str:
        """现实时间 + 时段。用的是提示词同一套写法，免得两边说法不一致。"""

        try:
            return clock_text(self.local_now())
        except Exception:
            return ""

    @staticmethod
    def _duration_text(seconds: Any) -> str:
        """把秒数说成人话（世界时间对照那一行用）。"""

        try:
            total = max(0, int(round(float(seconds))))
        except (TypeError, ValueError):
            return ""
        days, rest = divmod(total, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        if days:
            return f"{days} 天 {hours} 小时"
        if hours:
            return f"{hours} 小时 {minutes} 分"
        return f"{minutes} 分钟"

    def _next_schedule(self, session_id: str) -> dict[str, Any] | None:
        """下一条要触发的日程：时间、还有多少分钟、里面有哪些动作。"""

        items = list(getattr(self.schedules, "schedules", None) or [])
        if not items:
            return None
        try:
            now = self.local_now()
        except Exception:
            return None
        action_map = self.world.action_map()
        best: dict[str, Any] | None = None
        for item in items:
            if not item.enabled:
                continue
            sessions = list(item.sessions or [])
            if sessions and session_id not in sessions:
                continue
            try:
                hour, minute = (int(part) for part in str(item.time).split(":")[:2])
            except (TypeError, ValueError):
                continue
            for offset in range(0, 8):
                moment = (now + timedelta(days=offset)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if moment <= now:
                    continue
                weekday = self.WEEKDAY_KEYS[moment.weekday()]
                if item.days and weekday not in item.days:
                    continue
                minutes = max(0, int(round((moment - now).total_seconds() / 60)))
                if best is None or minutes < int(best["in_minutes"]):
                    names = []
                    for step in item.action_chain or []:
                        definition = action_map.get(step.type)
                        names.append(
                            (definition.name or definition.id) if definition else step.type
                        )
                    best = {
                        "id": item.id,
                        "time": item.time,
                        "in_minutes": minutes,
                        "weekday": WEEKDAY_NAMES[moment.weekday()],
                        "actions": " → ".join(names),
                        "auto_travel": bool(item.auto_travel),
                    }
                break
        return best

    def _budget(self, state: WorldState) -> dict[str, dict[str, int]]:
        """本小时还剩多少额度（跨小时自动归零，和运行时用的是同一套计数）。"""

        hour = self._hour_index(state)
        limits = self.world.limits

        def quota(count: Any, marker: Any, limit: Any) -> dict[str, int]:
            total = max(0, int(limit))
            used = int(count) if int(marker or 0) == hour else 0
            return {"left": max(0, total - used), "used": min(used, total), "limit": total}

        return {
            "plan": quota(
                state.llm_plan_count_hour, state.llm_plan_hour_marker, limits.max_llm_plan_per_hour
            ),
            "text": quota(
                state.llm_text_count_hour, state.llm_text_hour_marker, limits.max_llm_text_per_hour
            ),
            "tool_param": quota(
                state.tool_param_count_hour,
                state.tool_param_hour_marker,
                limits.max_tool_param_per_hour,
            ),
            "share": quota(
                state.share_count_hour, state.share_hour_marker, limits.max_share_per_hour
            ),
            "autonomous": quota(
                state.autonomous_count_hour,
                state.autonomous_hour_marker,
                limits.max_autonomous_per_hour,
            ),
        }

    def _channel_status(self, session_id: str) -> dict[str, Any]:
        """发送通道与最近一次模型调用的状态（排查"她怎么不说话了"用）。"""

        blocked_seconds = 0
        try:
            getter = getattr(self.messenger, "blocked_seconds", None)
            if callable(getter):
                blocked_seconds = int(getter(session_id) or 0)
            elif self.messenger is not None and self.messenger.blocked(session_id):
                blocked_seconds = -1
        except Exception:
            blocked_seconds = 0
        record = dict(self._last_llm.get(session_id) or {})
        if record:
            record["ago_seconds"] = max(0, int(self._now() - float(record.get("at", 0.0))))
        return {
            "send_blocked_seconds": blocked_seconds,
            "last_llm": record,
            # 空串 = 跟随 AstrBot 当前会话的供应商
            "llm_provider": str(getattr(self.llm, "provider_id", "") or ""),
            "has_llm": self.llm is not None,
        }

    async def snapshot(self, session_id: str) -> dict[str, Any]:
        state = await self.load_state(session_id, cold_start=False)
        node = self.node(state.node_id)
        zone = self.world.zone_map().get(self.world.zone_of(state.node_id))
        return {
            "session_id": session_id,
            "world_time": state.world_time,
            "tick_seconds": self.tick_seconds,
            "node_id": state.node_id,
            "node_name": node.name if node else "",
            "state": state.state,
            "mood": state.mood,
            # 今天的基调（每天掷一次）：只调各条曲线的快慢，界面上说清是哪一种
            "day_mood": {
                "id": str(state.day_mood or ""),
                "label": (
                    day_mood_info(state.day_mood).label
                    if day_mood_info(state.day_mood) is not None
                    else ""
                ),
                "hint": (
                    day_mood_info(state.day_mood).hint
                    if day_mood_info(state.day_mood) is not None
                    else ""
                ),
                "day": str(state.day_mood_day or ""),
                "enabled": bool(getattr(self.world.state_dynamics, "daily_mood_enabled", True)),
            },
            "values": self.dynamics.values(state),
            # 情绪两轴的派生结果：心情词、正在气头上的标记、这一轮的表达格
            "storm": bool(state.storm),
            "style": {
                "cell": state.last_style_cell,
                "say_limit": int(state.last_say_limit or 0),
            },
            "interject": dict(state.interject_stats or {}),
            "tool_breakers": self.tool_breaker_state(),
            "current_action": state.current_action,
            "current_plan": state.current_plan,
            "last_reasoning": state.last_reasoning,
            "thoughts": state.thoughts[-5:],
            # 别的插件的状态（今日穿搭、背包…）：编辑器里能看到、能手清
            "external_state": dict(getattr(state, "external_state", None) or {}),
            "unanswered_count": state.unanswered_count,
            "cooldown_until": state.cooldown_until,
            # 决策意愿，以及它换算出来的「问大模型」概率（编辑器里显示，方便调参）
            "willingness": round(reply_willingness(state), 4),
            "llm_sample_rate": round(self.decider.llm_sample_rate(state), 4),
            "recent_events": state.recent_events[-10:],
            # 事件系统：能力值（人话）、未了的事、正在等谁拿主意
            "abilities": {
                name: {
                    "label": ABILITY_LABELS[name],
                    "value": round(normalize_abilities(state.abilities or self.world.abilities.initial)[name], 3),
                    "hint": ability_hint(
                        normalize_abilities(state.abilities or self.world.abilities.initial)[name]
                    ),
                }
                for name in ABILITIES
            },
            "event_threads": [
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "tier": item.get("tier"),
                    "steps": thread_step_lines(item),
                    "pending_followup": item.get("pending_followup"),
                    "next_step_at": item.get("next_step_at"),
                    "updated_at": item.get("updated_at"),
                }
                for item in list(state.event_threads or [])[-4:][::-1]
                if isinstance(item, dict)
            ],
            "pending_help": {
                "state": str((state.pending_help or {}).get("state") or ""),
                "title": str((state.pending_help or {}).get("title") or ""),
                "until": float(
                    (state.pending_help or {}).get("active_until")
                    or (state.pending_help or {}).get("idle_until")
                    or 0.0
                ),
            },
            "event_digest": list(state.event_digest or []),
            "events_enabled": bool(getattr(self.world.events, "enabled", True)),
            # 群聊上下文：留档条数 + 摘要（编辑器里能看到、也能一键清空）
            **self._chat_counts(state, session_id),
            # 分会话的摘要：编辑器按会话列出来（老存档只有一份全局的）
            "chat_summary": self.chat_summary_for(state, session_id),
            "chat_summaries": [
                {
                    "session": str(key),
                    "label": "这里" if str(key) == str(session_id) else self.session_label(str(key), state),
                    "text": text,
                }
                for key, text in self.chat_summary_map(state).items()
            ],
            "chat_note": self.chat_note_for(state, session_id),
            "chat_notes": {
                str(key): str(dict(value or {}).get("text") or "")
                for key, value in dict(state.chat_notes or {}).items()
                if isinstance(value, dict)
            },
            "nickname": state.bot_current_nickname or state.bot_base_nickname,
            "zone_id": zone.id if zone is not None else "",
            "zone_name": zone.name if zone is not None else "",
            "clock_text": self._clock_text(),
            "world_elapsed_seconds": int(state.world_time * self.tick_seconds),
            "world_elapsed_text": self._duration_text(state.world_time * self.tick_seconds),
            "next_schedule": self._next_schedule(session_id),
            "budget": self._budget(state),
            # 「聊天这条路的情绪额度」用得怎么样了：调情绪量程时看这个最直观
            "chat_mood": {
                "turn_cap": round(
                    float(getattr(self.world.state_dynamics, "chat_valence_cap", 0.05) or 0.0),
                    4,
                ),
                "daily_cap": round(
                    float(
                        getattr(self.world.state_dynamics, "chat_valence_daily_cap", 0.15)
                        or 0.0
                    ),
                    4,
                ),
                "spent": round(float(state.chat_valence_spent or 0.0), 4),
                "day": str(state.chat_day or ""),
            },
            # 「还没聊完的事」：状态页能看到她在惦记什么
            "open_topics": [
                {
                    "text": str(item.get("text") or ""),
                    "who": str(item.get("who") or ""),
                    "who_name": str(item.get("who_name") or ""),
                    "asked": int(item.get("asked") or 0),
                    "next_ask_at": float(item.get("next_ask_at") or 0.0),
                }
                for item in (state.open_topics or [])
                if isinstance(item, dict)
            ],
            # 「她记着的账」与「她自己的事」：状态页/调试里也能看见
            "grudges": [
                {
                    "user_id": str(item.get("user_id") or ""),
                    "who_name": str(item.get("who_name") or ""),
                    "reason": str(item.get("reason") or ""),
                    "at": float(item.get("at") or 0.0),
                    "until": float(item.get("until") or 0.0),
                }
                for item in (state.grudges or [])
                if isinstance(item, dict)
            ],
            "own_topics": [
                {
                    "text": str(item.get("text") or ""),
                    "who": str(item.get("who") or ""),
                    "who_name": str(item.get("who_name") or ""),
                    "at": float(item.get("at") or 0.0),
                }
                for item in (state.own_topics or [])
                if isinstance(item, dict)
            ],
            "heart_knots": [
                {
                    "text": str(item.get("text") or ""),
                    "about": str(item.get("about") or ""),
                    "since": float(item.get("since") or 0.0),
                }
                for item in (state.heart_knots or [])
                if isinstance(item, dict)
            ],
            "channel": self._channel_status(session_id),
            # 名片是否被锁住（编辑器里用一个按钮切换，所以要能读到当前状态）
            "nickname_locked": bool(state.bot_nickname_locked),
            "nickname_base": state.bot_base_nickname,
            # 按最近说话时间排好、只带前几个：状态页一行放得下，也不会越攒越长
            "user_presence": state.recent_active_users(8),
            "user_presence_total": len(state.user_presence),
            # 「她想找谁」：想念值 / 冷却中的冷却时间 / 今天的主动额度
            "miss": self.miss_overview(state),
            # 「想被碰一碰」：欲求值、触发线、今天还能推几次（和想念并排显示）
            "desire": self.desire_panel(state),
            # 从当前位置能直达哪里、各需要多少 tick（给编辑器和调试看）
            "travel": [
                {
                    "node_id": target,
                    "name": (
                        self.node(target).name if self.node(target) is not None else target
                    ),
                    "ticks": ticks,
                }
                for target, ticks in sorted(
                    self.world.adjacent().get(state.node_id, []),
                    key=lambda item: item[1],
                )
            ],
        }

    async def preview_injection(self, session_id: str, persona_id: str = "") -> str:
        """预览注入模式会写进主人格的文本（只读，不改变任何状态）。"""

        state = await self.load_state(session_id, cold_start=False)
        node = self.node(state.node_id)
        memories = self.memory.recall(
            session_id=session_id,
            persona_id=persona_id,
            node_id=state.node_id,
            limit=self.world.limits.max_think_memory,
        )
        text = self.prompts.build_injection(
            state,
            node=node,
            memories=memories,
            engagement_hint=self.engagement.hint(state),
            recent_chat=self.chat_window(state),
            # 预览要和实跑一致：带上通讯录和"这条来自哪儿"、以及扩展要藏的 / 要加的
            current_session=session_id,
            session_labels=self.session_labels(state),
            profile_text=self.profile_block(state),
            samples=self.voice_sample_lines(state, session_id, preview=True),
            hidden_actions=self._hidden_actions(state, session_id),
            **await self.runtime_notes(state.session_id),
        )
        layer = self._extension_prompt(state, session_id)
        # 单独起个小标题：不然这一段混在正文里，调试页的分段索引里看不到它
        return f"{text}\n\n{EXTENSION_LAYER_HEADING}\n{layer}" if layer else text

    async def overview(self) -> list[dict[str, Any]]:
        """所有启用会话的简要状态：编辑器地图页一次看全"谁在哪"。"""

        result: list[dict[str, Any]] = []
        for session_id in self.enabled_session_ids():
            try:
                state = await self.load_state(session_id, cold_start=False)
            except Exception:
                continue
            node = self.node(state.node_id)
            result.append(
                {
                    "session_id": session_id,
                    "node_id": state.node_id,
                    "node_name": node.name if node is not None else state.node_id,
                    "state": state.state,
                    "mood": state.mood,
                    "world_time": state.world_time,
                }
            )
        return result

    async def preview_autonomous_prompt(self, session_id: str, persona_id: str = "") -> str:
        """预览接管模式会发给 LLM 的 system prompt（只读）。"""

        state = await self.load_state(session_id, cold_start=False)
        node = self.node(state.node_id)
        _cell, say_limit, style_text = self.style_for(state, session_id)
        text = self.prompts.build_autonomous_system_prompt(
            persona_text=await self._persona_text(session_id),
            state=state,
            node=node,
            available_tools=self.available_tools(),
            memories=self.memory.recall(
                session_id=session_id,
                persona_id=persona_id,
                node_id=state.node_id,
                limit=self.world.limits.max_think_memory,
            ),
            engagement_hint=self.engagement.hint(state),
            max_messages=say_limit,
            recent_chat=self.chat_window(state),
            reasoning=bool(self.world.reasoning_enabled),
            style_block=style_text,
            # 预览要和实跑一致：自主提示词里也要有"你能说话的地方"
            session_directory=self.session_directory(state),
            current_session=session_id,
            session_labels=self.session_labels(state),
            # 配额用完的动作，实跑时不会出现在提示词里，预览也不该出现
            hidden_actions=self.exhausted_actions(state),
            profile_text=self.profile_block(state),
            # 声音样例也要一致：预览里看不到，用户就没法确认挑中的那几句到底进没进
            samples=self.voice_sample_lines(state, session_id, preview=True),
            # 预览要跟实跑一致：有扩展声明要"他碰了她哪儿"时，这里也得带上那个字段
            wants_touch=self.extensions.wants("touch"),
            json_fields=self.extensions.json_fields(),
            # 预览要跟实跑一致：实跑会带上「好奇心」那一段，这里也要带
            extra_notes=[
                note for note in [self.extra_reminders(state, autonomy=True)] if note
            ],
            **await self.runtime_notes(session_id),
        )
        # 预览要和实跑一致：实跑会带上扩展的注入层（engine 里的自主计划那条路），
        # 以前这里漏了，调试页看"自主提示词"就像没装扩展一样
        layer = self._extension_prompt(state, session_id)
        return f"{text}\n\n{EXTENSION_LAYER_HEADING}\n{layer}" if layer else text

    async def preview_voice_samples(self, session_id: str) -> list[dict[str, Any]]:
        """调试页用：**这一轮会抽到哪几条声音样例**。

        提示词两万字，样例排在第 2 段，不点出来基本找不着——顺手连"为什么是这几条"
        （各自的场景标签）一起给。
        """

        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return []
        picked = self.voice_sample_lines(state, session_id, preview=True)
        labels = {
            key: str(label).split("：")[0] for key, label in self.prompts.VOICE_SCENES
        }
        return [
            {
                "text": str(item.get("text") or ""),
                "scene": str(item.get("scene") or ""),
                "scene_label": labels.get(str(item.get("scene") or ""), "") or "不限",
                "move": str(item.get("move") or ""),
            }
            for item in picked
        ]

    async def preview_state_slots(self, session_id: str) -> list[dict[str, Any]]:
        """调试页用：她当前的状态槽，以及每一条会不会写进提示词。

        过期的那几条**不会**进提示词，调试页要能说清这件事，
        不然只会看到"状态槽里明明有，提示词里却没有"。
        """

        state = await self.load_state(session_id, cold_start=False)
        now = self._now()
        slots: list[dict[str, Any]] = []
        for slot, info in dict(getattr(state, "external_state", None) or {}).items():
            if not isinstance(info, dict):
                continue
            try:
                expires = float(info.get("expires_at") or 0.0)
            except (TypeError, ValueError):
                expires = 0.0
            slots.append(
                {
                    "slot": str(slot),
                    "label": str(info.get("label") or slot),
                    "text": str(info.get("text") or ""),
                    "at": float(info.get("at") or 0.0),
                    "expired": bool(expires and now > expires),
                }
            )
        return slots

    async def _event_marker(self, state: WorldState) -> int:
        """本轮的起点事件 id。

        第一次用到（插件刚启动 / 刚重载）时要从库里取当前最大 id——不能默认 0，
        否则「把新事件发到群里」会把历史上最后 50 条日志当成新事件重放一遍，
        变成每个 tick 往群里刷一串老消息。
        """

        known = self._event_ids.get(state.session_id)
        if known is not None:
            return int(known)
        latest = 0
        try:
            rows = await self.db.call(
                "query_events", session_id=state.session_id, limit=1
            )
            if rows:
                latest = int(rows[0].get("id") or 0)
        except Exception:
            latest = 0
        self._event_ids[state.session_id] = latest
        return latest

    def echo_types(self) -> set[str]:
        """「调试输出」勾选的事件类型（只认认识的 key，空集合 = 关闭）。"""

        return set(self.echo_modes())

    def echo_modes(self) -> dict[str, str]:
        """每个类型各自的显示方式：``full`` / ``compact``（``off`` 的不列出来）。"""

        modes: dict[str, str] = {}
        for name, mode in dict(getattr(self.world, "echo_modes", {}) or {}).items():
            key = str(name).strip()
            value = str(mode or "").strip().lower()
            if key in ECHO_EVENT_TYPES and value in ("full", "compact"):
                modes[key] = value
        if modes:
            return modes
        # 兼容：还没迁移过的老配置
        legacy = {
            str(name).strip()
            for name in (self.world.echo_types or [])
            if str(name).strip() in ECHO_EVENT_TYPES
        }
        fallback = "compact" if self.echo_compact() else "full"
        return {name: fallback for name in legacy}

    def echo_compact_for(self, event_type: str) -> bool:
        """这一类调试输出是不是精简模式。"""

        return str((self.echo_modes() or {}).get(str(event_type or ""), "full")) == "compact"

    def echo_compact(self) -> bool:
        """精简模式：调试输出只发事件本身，不带参数与结果。"""

        return bool(getattr(self.world, "echo_compact", False))

    def _queue_echo(self, session_id: str, lines: list[str]) -> None:
        """回复路径之外的调试回显先存着，等下一次发送时一起带出去。

        （被叫醒、睡觉门禁这类事件发生在"还没决定要不要回复"之前，
        没法挂在某一轮回复的 outcome 上。）
        """

        if not lines:
            return
        self._pending_echo.setdefault(session_id, []).extend(
            line for line in lines if line
        )

    def take_pending_echo(self, session_id: str) -> list[str]:
        """取走并清空攒下的回显（发送方负责带到群里）。"""

        return self._pending_echo.pop(session_id, [])

    async def _send_debug_live(self, session_id: str, message: str) -> bool:
        """把一条调试行立刻发到群里（发送失败冷却、分段逻辑都在 messenger 里）。"""

        text = str(message or "").strip()
        if self.debug_sink is None or not text:
            return False
        try:
            return bool(await self.debug_sink(session_id, text))
        except Exception as exc:
            self._log("debug", f"调试回显发送失败：{exc}")
            return False

    def _echo_debounced(
        self, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> bool:
        """同一工具的连续调用要不要吞掉：一串并行查询只留第一条。"""

        if event_type not in _DEBUG_ECHO_TYPES:
            return False
        tool = str(payload.get("tool") or payload.get("command") or "")
        if not tool:
            return False
        key = (session_id, event_type, tool)
        now = time.monotonic()
        last = self._echo_debounce.get(key, 0.0)
        self._echo_debounce[key] = now
        if len(self._echo_debounce) > 200:
            self._echo_debounce.clear()
            self._echo_debounce[key] = now
        return bool(last and (now - last) < DEBUG_ECHO_DEBOUNCE_SECONDS)

    async def _echo_events_since(
        self, state: WorldState, outcome: TickOutcome | None, marker: int
    ) -> None:
        """把这一轮新产生的事件渲染成消息（只处理「调试输出」里勾选了的类型）。"""

        if outcome is None:
            return
        enabled = self.echo_types()
        if not enabled:
            return
        try:
            events = await self.db.call(
                "query_events", session_id=state.session_id, limit=50
            )
        except Exception:
            return
        fresh = [item for item in events if int(item.get("id") or 0) > marker]
        # 处理过就把游标推到最后一条：不然这一批会在下一个 tick 再发一遍
        # （tick 没有新事件时游标不会自己前进，这就是"同一句话一直刷"的来源）。
        self._event_ids[state.session_id] = max(
            [marker] + [int(item.get("id") or 0) for item in events]
        )
        # query_events 是新到旧；一次 tick 里最多回显最新的这么多条，
        # 再按时间正序发出去（连锁动作多的时候不要把群刷了）
        modes = self.echo_modes()
        if not modes:
            return
        # 有些事件在发生的当下就已经插好位置了（见 ``_log_event`` 的 ``outcome`` 参数），
        # 这里再发一遍就会重复——跳过它们，剩下的才补在这一轮末尾。
        already = set(getattr(outcome, "echoed_event_ids", ()) or ())
        already |= set(self._echoed_ids.get(state.session_id, ()) or ())
        for item in reversed(fresh[:12]):
            if int(item.get("id") or 0) in already:
                continue
            kind = str(item.get("event_type") or "")
            detail = item.get("detail")
            if not isinstance(detail, dict):
                detail = {}
            if not _echo_payload(kind, detail, enabled):
                continue
            mode = modes.get(kind)
            if not mode:
                continue
            if kind in DEBUG_ONLY_ECHO_TYPES and mode == "compact":
                continue
            try:
                line = render_event(item, self.world, compact=mode == "compact")
            except Exception:
                continue
            if line:
                outcome.add_debug(f"{ECHO_EVENT_TYPES[kind]} {line}")

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        if level == "debug" and not self.debug:
            return
        method = getattr(self.logger, level, None)
        if callable(method):
            method(f"[virtual_world] {message}")

    async def _log_event(
        self,
        state: WorldState,
        event_type: str,
        detail: dict[str, Any] | None = None,
        *,
        persona_id: str = "",
        outcome: TickOutcome | None = None,
        silent: bool = False,
        place: str = "",
    ) -> None:
        """写一条事件日志（编辑器的「日志」页会渲染成人话），并顺手做数量裁剪。

        传入 ``outcome`` 且开启了「把动作发到群里」时，同一行文本也会作为消息发出去。
        ``silent=True`` 表示"这条已经并进别的行里了"：不发，但要让收尾那次批量回显跳过它。

        ``place`` / ``outcome.place`` 是"这件事发生在哪个会话"：和她的存档会话不是同一个时，
        事件里会多记一条 ``session``（渲染出来就是「私聊 123456「主人」」这种），
        这样一份日志里也能分清哪句是群里说的、哪句是私聊说的。
        """

        payload = detail or {}
        where = str(place or (getattr(outcome, "place", "") if outcome else "") or "")
        if where and where != str(state.session_id):
            label = self.session_label(where, state)
            if label:
                payload = {**payload, "session": label}
        enabled = self.echo_types()
        line_added = False
        line = ""
        muted = event_type in DEBUG_ONLY_ECHO_TYPES and self.echo_compact_for(event_type)
        if (
            not silent
            and not muted
            and enabled
            and _echo_payload(event_type, payload, enabled)
        ):
            try:
                line = render_event(
                    {
                        "event_type": event_type,
                        "detail": payload,
                        "world_time": state.world_time,
                    },
                    self.world,
                    compact=self.echo_compact_for(event_type),
                )
            except Exception:
                line = ""
        consumed = False
        live = False
        # 回显发到"这件事发生的那个会话"：她在私聊里被搭话、调了工具，
        # 那几行回显就该出现在私聊，而不是她存档所在的那个群
        echo_target = where or str(state.session_id)
        if line:
            rendered = f"{ECHO_EVENT_TYPES.get(event_type, '•')} {line}"
            if self._echo_debounced(echo_target, event_type, payload):
                # 同一工具的连续调用（一次检索并行查好几条）只发第一条
                consumed = True
            elif self.debug_sink is not None:
                live = await self._send_debug_live(echo_target, rendered)
            elif outcome is not None:
                outcome.add_debug(rendered)
                line_added = True

        try:
            new_id = await self.db.call(
                "add_event",
                session_id=state.session_id,
                persona_id=persona_id,
                world_time=state.world_time,
                event_type=event_type,
                detail=payload,
            )
            if new_id:
                self._event_ids[state.session_id] = int(new_id)
                if outcome is not None and (line_added or silent or live or consumed):
                    # 这一行已经在发生的当下插好了位置：收尾那次批量回显要跳过它
                    outcome.echoed_event_ids.add(int(new_id))
                if live or consumed:
                    self._echoed_ids.setdefault(state.session_id, set()).add(int(new_id))
        except Exception as exc:  # 日志写失败不能影响主流程
            self._log("debug", f"写事件日志失败：{exc}")
            return
        count = self._event_writes.get(state.session_id, 0) + 1
        self._event_writes[state.session_id] = count
        if count % 100 == 0:
            try:
                await self.db.call(
                    "prune_session_events",
                    state.session_id,
                    int(self.world.limits.max_log_events),
                )
            except Exception:
                pass


_ARGUMENT_ERROR_MARKERS = (
    "required positional argument",
    "unexpected keyword argument",
    "missing 1 required",
    "required argument",
    "got an unexpected keyword",
    "positional argument",
    "参数",
)


def _looks_like_argument_error(error: Any) -> bool:
    """这次失败像不像"参数给少了 / 给错了"。

    很多第三方工具的 schema 写着参数可选，实现却必须要；调用当场报
    ``missing 1 required positional argument``。这种错值得带回去重新补一次参数。
    """

    text = str(error or "").lower()
    if not text:
        return False
    return any(marker in text for marker in _ARGUMENT_ERROR_MARKERS)


# 「工具自己坏了」的迹象：只有这些才计入熔断——参数写错是补参的问题，不算工具坏。
_BROKEN_TOOL_MARKERS = (
    "没有可调用的 handler",
    "没有可调用",
    "not callable",
    "no such tool",
    "tool not found",
    "找不到工具",
    "无法调用",
    "timeout",
    "timed out",
    "超时",
    "connection",
    "connect",
    "refused",
    "gateway",
    "502",
    "503",
    "500",
    "rate limit",
    "quota",
    "unauthorized",
    "401",
    "403",
)


def _looks_like_tool_broken(error: Any) -> bool:
    """这次失败像不像"工具本身不可用"（而不是参数/意图的问题）。"""

    text = str(error or "").lower()
    if not text:
        # 没有报错信息、也没返回内容：当成工具没给出结果，不计入熔断
        return False
    if _looks_like_argument_error(text):
        return False
    return any(marker in text for marker in _BROKEN_TOOL_MARKERS)


_SCHEMA_RESERVED_KEYS = {
    "type",
    "properties",
    "required",
    "title",
    "description",
    "additionalProperties",
    "$schema",
    "definitions",
    "$defs",
}


def normalize_param_schema(schema: Any) -> dict[str, Any]:
    """把各家工具写的参数描述统一成 ``{"properties": {...}, "required": [...]}``。

    AstrBot 里工具的参数定义至少有四种写法，真实插件里全都遇到过：

    - 标准 JSON Schema：``{"type": "object", "properties": {...}, "required": [...]}``；
    - 省掉外层、直接给键值表：``{"city": {"description": "地点"}}``；
    - 参数列表：``[{"name": "city", "description": "地点", "required": true}]``；
    - 属性值是字符串：``{"city": "地点，例如杭州"}``。

    统一之后，"哪些参数要填、哪些是必填"就有唯一答案了。
    """

    if isinstance(schema, list):
        properties: dict[str, Any] = {}
        required: list[str] = []
        for item in schema:
            if isinstance(item, str):
                properties[item] = {}
                continue
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("key") or "").strip()
            if not name:
                continue
            properties[name] = {
                "type": str(item.get("type") or "string"),
                "description": str(item.get("description") or ""),
            }
            if item.get("required"):
                required.append(name)
        return {"properties": properties, "required": required}

    if not isinstance(schema, dict):
        return {"properties": {}, "required": []}

    raw_properties = schema.get("properties")
    if not isinstance(raw_properties, dict):
        # 没有 properties 时，把顶层那些"不是 schema 关键字"的键当成参数表
        guessed = {
            str(key): value
            for key, value in schema.items()
            if key not in _SCHEMA_RESERVED_KEYS
        }
        raw_properties = guessed if guessed else {}

    properties = {}
    required = {str(name) for name in (schema.get("required") or []) if str(name)}
    for name, spec in raw_properties.items():
        key = str(name)
        if isinstance(spec, str):
            properties[key] = {"description": spec}
            continue
        if not isinstance(spec, dict):
            properties[key] = {}
            continue
        properties[key] = dict(spec)
        # 有的工具把"必填"写在属性自己身上
        if spec.get("required") is True:
            required.add(key)
    return {"properties": properties, "required": sorted(required)}


def render_param_text(schema: dict[str, Any]) -> str:
    """把工具自带的参数 schema 渲染成一行中文说明（提示词与编辑器共用）。"""

    normalized = normalize_param_schema(schema)
    properties = normalized.get("properties") or {}
    required = set(normalized.get("required") or [])
    if not properties:
        return "（这个工具不需要参数）"
    parts: list[str] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            spec = {}
        desc = str(spec.get("description") or "").strip()
        flag = "必填" if name in required else "可选"
        text = f"{name}（{flag}"
        if desc:
            text += f"，{desc}"
        text += "）"
        parts.append(text)
    return "、".join(parts)


def _as_float(value: Any, default: float = 0.0) -> float:
    """尽量转成 float，坏值退回默认值。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _summarize_batch(items: list[dict[str, Any]], *, limit: int = 240) -> str:
    """把一批群聊压成一条概览：「小明：123；你：来了来了」。

    她回应过的那批不再原样重复给模型，但也不能凭空消失——这一行就是下一轮的背景。
    """

    parts: list[str] = []
    for item in items or []:
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        who = "你" if item.get("is_self") else str(
            item.get("name") or item.get("user_id") or "有人"
        )
        parts.append(f"{who}：{text[:40]}")
    text = "；".join(parts)
    return text if len(text) <= limit else text[:limit] + "…"


def _signed(value: float) -> str:
    """把数值写成带正负号的增量（效果表里 "+0.08" / "-0.05" 才是"加减"）。"""

    return f"{float(value or 0.0):+.4f}"


def _clip_text(value: Any, limit: int = 200) -> str:
    """把任意值压成单行短文本，用于事件日志。"""

    if value is None:
        return ""
    text = str(value).strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _clip_log(value: Any, limit: int = 1200) -> str:
    """写进日志页的正文：留长一些，并且**标明是被截断的**（免得以为工具只返回了这么点）。"""

    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（完整 {len(text)} 字，日志里只留存前 {limit} 字）"


def _to_int_or_default(value: Any, default: int = 0) -> int:
    """能转成整数就用它（0 也算有效值），转不出来才用默认值。"""

    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _same_text(left: str, right: str) -> bool:
    """两段文本算不算"同一条消息"：忽略空白标点，也容忍一方被管线改写。"""

    def squeeze(text: str) -> str:
        return "".join(ch for ch in str(text or "").lower() if ch.isalnum())

    a = squeeze(left)
    b = squeeze(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(min(a, b, key=len)) >= 4 and (a in b or b in a)
