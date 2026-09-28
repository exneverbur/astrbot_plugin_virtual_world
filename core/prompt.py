"""五层提示词组装（设计文档第 5 章）。

两种用途：
- ``build_injection``：@ 触发时走「注入模式」，只把「她现在在哪、什么状态」告诉主人格，
  不接管回复、不注入 JSON 协议。追加在 req.system_prompt 末尾，不覆盖其他插件的内容。
- ``build_autonomous_*``：自主行为走「接管模式」，由插件自己调 LLM 并约束 JSON 输出。

分层顺序按「越稳定越靠前」排：

1. 人设（整段固定）
2. 世界规则（整段固定）
3. 输出格式（整段固定）
4. 当前场景（随移动变化）
5. 运行时状态（每次调用都不同）

这样前 3 层可以在整个会话生命周期里构成同一段前缀，命中 provider 的前缀缓存；
容易变的部分留在后面，不会把它们后面的内容一起打成缓存未命中。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .memory import RecalledMemory
from .models import NodeDef, WorldConfig, pronoun_for
from .state import (
    FORWARD_SUMMARY_MARK,
    WorldState,
    chat_core_text,
    chat_item_is_fresh,
    group_chat_items,
    take_last_chat_groups,
)
from .pathfinding import travel_cost
from .tool_policy import allowed_tools

NO_MEMORY_TEXT = "（这里没有特别让你想起什么）"

# 已经回过话的那一截 / 别处听到的：条数上限来自世界配置
# （`context.chat_answered_lines` / `chat_elsewhere_lines`），这里只留兜底默认值
ANSWERED_CHAT_MAX = 30
ELSEWHERE_CHAT_MAX = 12

# 「最近发生的事」里不列这几类动作：它们只是"她开口说了一句 / 心里想了一下"，
# 聊天记录和「你最近说过的话」已经写清楚了，列在这儿只会把真正发生的事
# （做饭、走动、被人打断…）从这几个位置里挤出去。
# 亲昵动作、伸懒腰这类"确实做了点什么"的照旧列出来。
CHATTY_EVENT_ACTIONS = ("say", "think", "share")
# 只有这些事件带实际做的事；其余都是碎片记录（发言等待回应、内部状态之类），
# 硬塞进提示词只会写出一行原始数据，不如不写。
NOTABLE_EVENT_KINDS = frozenset(
    {
        "action",
        "action_start",
        "move",
        "plan",
        "tool",
        "tool_call",
        "tool_result",
        "chain",
        "schedule",
        "interrupt",
        "wake_up",
        "startled",
        "event",
        "event_help",
    }
)

# 聊天记录里每条最多留多少字：她要看的是"谁在怎么说话"，
# 掐太短会看不出语气和上下文，太长又会把提示词撑爆。
CHAT_LINE_CHARS = 100
# 「你最近说过的话」每句最多留多少字（只用来提醒她别重复句式）
MINE_LINE_CHARS = 100

# 转发摘要那一行放宽：摘要本身就是压过的，再掐到 100 字就只剩半句
FORWARD_LINE_CHARS = 420

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 小时的时段划分：上界 + 名称，按顺序查第一个命中的
PERIOD_RANGES = (
    (5, "凌晨"),
    (8, "清晨"),
    (11, "上午"),
    (13, "中午"),
    (17, "下午"),
    (19, "傍晚"),
    (23, "晚上"),
    (24, "深夜"),
)


def period_of(hour: int) -> str:
    """把小时换算成「凌晨 / 清晨 / 上午 / …」。"""

    for limit, name in PERIOD_RANGES:
        if hour < limit:
            return name
    return PERIOD_RANGES[-1][1]


def clock_text(now: datetime) -> str:
    """「日期 + 星期 + 时间 + 时段」。提示词与编辑器共用同一套写法。"""

    return (
        f"{now.strftime('%Y-%m-%d')}（{WEEKDAY_NAMES[now.weekday()]}）"
        f"{now.strftime('%H:%M')} —— {period_of(now.hour)}"
    )


def clock_line(now: datetime) -> str:
    """写进提示词的那一行：``现在是：…``。"""

    return f"现在是：{clock_text(now)}"


def prompt_section_index(text: str) -> list[dict[str, Any]]:
    """给一份提示词做分段索引：每段标题 + 字符数。

    提示词动辄三四千字，直接丢进聊天窗口很容易被平台或查看器截掉尾巴——
    有了索引就能一眼看出「哪一段在、哪一段是空的、总共多长」。
    """

    sections: list[dict[str, Any]] = []
    title = "（开头）"
    size = 0
    for line in (text or "").splitlines():
        stripped = line.strip()
        heading = ""
        if stripped.startswith("# ==========") and "：" in stripped:
            heading = stripped.strip("#= ").strip()
        elif stripped.startswith("# ") and not stripped.startswith("# ===="):
            heading = stripped[2:].strip()
        if heading:
            if "（" in heading and len(heading) > 16:
                heading = heading.split("（", 1)[0] + "…"
            heading = heading.rstrip("：: ")
            if size or sections:
                sections.append({"title": title, "chars": size})
            title = heading
            size = 0
            continue
        size += len(line) + 1
    if size or not sections:
        sections.append({"title": title, "chars": size})
    return sections

_REASONING_ORDER = ("env", "state", "mood", "who", "inner", "intent")
_REASONING_LABELS = {
    "env": "在哪",
    "state": "状态",
    "mood": "心情",
    "who": "在和谁说话",
    "inner": "心里想的",
    "intent": "打算怎么办",
}


def _mentions_me(text: str, names: list[str]) -> bool:
    """这条群聊记录里有没有点名找她：@ 了她，或者叫了她的名字/名片。

    别人消息里的「你」不算——中文里那个「你」几乎总是指群里另一个人。
    """

    body = str(text or "")
    if not body:
        return False
    if "@ 了：你" in body or "你（" in body:
        return True
    return any(name and name in body for name in names)


def _one_line(value: Any, limit: int = 140) -> str:
    """把任意内容压成一行：转发、引用、多段文本在提示词里都会把排版撑乱。"""

    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


# 截断时优先在这里收尾：不要切在词中间，读起来才像"这条就到这儿"
_SENTENCE_TAILS = ("。", "！", "？", "；", "…", ".", "!", "?", ";")


def clip_line(value: Any, limit: int) -> str:
    """聊天记录里的一行：太长就截断，并写清"这条还有多少字没显示"。

    只留一个光秃秃的「…」会让她以为对方话说到一半（"你倒是说完啊"），
    所以截断时把还剩多少字一并写出来，而且尽量切在句末。
    """

    text = " ".join(str(value or "").split())
    limit = max(1, int(limit or 0))
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for tail in _SENTENCE_TAILS:
        position = cut.rfind(tail)
        # 别为了断句砍掉太多：至少留一半
        if position >= limit // 2:
            cut = cut[: position + 1]
            break
    return f"{cut}…（这条还有 {len(text) - len(cut)} 字没显示）"


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """取字段：既认 ``PersonView`` 这种对象，也认引擎拼好的 dict。"""

    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _dwell_text(minutes: float) -> str:
    """「多久」说成人话：45 分钟 / 2 小时 / 3 小时 20 分。"""

    value = max(0, int(round(float(minutes or 0.0))))
    if value < 60:
        return f"{value} 分钟"
    hours = value // 60
    rest = value % 60
    return f"{hours} 小时" + (f" {rest} 分" if rest else "")


def _duration_text(seconds: Any) -> str:
    """把秒数说成人话（动作时长范围那一行用）。"""

    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return ""
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


def lean_prompt(text: str) -> str:
    """去掉"劝导语"，只留事实与格式——给 A/B 实验用的**纯后处理**。

    删的是三块"提醒她该怎么做人"的话（不删功能，规则本体还在格式段里）：
    1. 「你最近说过的话」那段负向约束；
    2. 「最后确认」那段近因锚点；
    3. 输出格式里"关于 reasoning / memory / chat_note / open_topic / valence_delta /
       affinity_delta"这一串字段劝导（JSON 示例与 actions 规范都留着）。

    放在这里而不是做成开关，是为了**不动生产路径**：只有测评会用它。
    """

    lines = str(text or "").splitlines()
    kept: list[str] = []
    skipping_section = False
    skipping_fields = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# 你最近说过的话") or stripped.startswith("# 最后确认"):
            skipping_section = True
            continue
        if skipping_section:
            if stripped.startswith("# "):
                skipping_section = False
            else:
                continue
        if stripped.startswith("关于 ") and stripped.endswith("："):
            skipping_fields = True
            continue
        if skipping_fields:
            if stripped.startswith("actions 是你要执行的动作列表") or stripped.startswith("# "):
                skipping_fields = False
            else:
                continue
        kept.append(line)
    return "\n".join(kept)


def _fingerprint(text: str) -> str:
    """给一句话算个"指纹"：只留字母数字，用来干掉提示词里的重复行。"""

    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


class PromptBuilder:
    """把世界状态渲染成提示词。"""

    def __init__(
        self,
        world: WorldConfig,
        tick_seconds: float = 60.0,
        now_provider: Any = None,
    ) -> None:
        self.world = world
        self.tick_seconds = max(1.0, float(tick_seconds or 60.0))
        # 取当前时间的回调（引擎传 local_now 进来）：提示词里要告诉她"现在几点、什么时候段"，
        # 否则模型分不清深夜该睡觉、白天才该小睡。
        self.now_provider = now_provider
        self._reach_cache: dict[str, str] = {}

    def _now(self) -> datetime | None:
        if self.now_provider is None:
            return None
        try:
            value = self.now_provider()
        except Exception:
            return None
        return value if isinstance(value, datetime) else None

    @staticmethod
    def _clock_line(now: datetime) -> str:
        """「现在是…」那一行：日期 + 星期 + 时间 + 时段。"""

        return clock_line(now)

    def _memory_stamp(self, created_at: float, now: datetime | None) -> str:
        """记忆的时间标签：日期 + 时段（今年只写月-日，往年带上年份）。

        只有日期的话，"早上跟她聊了什么"和"半夜跟她聊了什么"在记忆里长得一模一样，
        她回忆起来就会串。
        """

        if not created_at:
            return ""
        try:
            moment = datetime.fromtimestamp(created_at, tz=now.tzinfo if now else None)
        except Exception:
            return ""
        period = period_of(moment.hour)
        if now is not None and moment.year == now.year:
            return f"{moment.strftime('%m-%d')} {period}"
        return f"{moment.strftime('%Y-%m-%d')} {period}"

    def set_world(self, world: WorldConfig) -> None:
        self.world = world
        self._reach_cache.clear()

    def set_tick_seconds(self, tick_seconds: float) -> None:
        self.tick_seconds = max(1.0, float(tick_seconds or 60.0))
        self._reach_cache.clear()

    # ---------------- 第 1、2 层 ----------------

    def persona_layer(self, persona_text: str) -> str:
        return (persona_text or "").strip()

    def world_layer(self) -> str:
        base = (self.world.global_prompt or "").strip()
        extra = (
            "你生活在一个群聊虚拟世界里。你有一个私有空间，可以移动、做事、休息、上网。\n"
            "你的行为要自然地反映你的位置和状态，但不要向用户解释地图、规则或系统。\n"
            "针对群友的动作可以表现出来，自己换位置、发呆、睡觉等行为默认静默。\n"
            "关系不是只能往上走：处得来就更近，心里凉了也可以往后退——"
            "朋友退回群友、恋人退回朋友、甚至翻脸，都由你自己判断，不用替谁硬撑。\n"
            "**你能做到哪一步，看关系与好感**：亲昵的上限取「关系类型允许的」和"
            "「好感够不够」里更严的那个——群友怎么聊都到不了抱抱亲亲，"
            "得关系真的更近了才放宽。\n"
            "**排好的日程到点会自己执行**（你做的那一串动作，包括只是一句提醒），"
            "不用你盯着；到点时会带着当初写的前因再问你一次。日程只会落在你能说话的地方。\n"
            "**睡着的时候**，系统会替你把这段时间的经历整理成记忆与画像——不用你操心，也不用你复述。\n"
            "关于过去的事、关于某个人的事，先看上面给你的记忆与画像；确实想不起来就用"
            "「回想」这个动作去翻，**不要凭印象编**——想不起来就说想不起来。"
        )
        return f"{base}\n\n{extra}".strip()

    # ---------------- 第 3 层 ----------------

    def ability_map(self) -> str:
        """一行式能力地图：哪里能做什么。"""

        parts: list[str] = []
        for node in self.world.nodes:
            names = [
                action.name or action.id for action in self.world.actions_in(node.id)
            ]
            if names:
                parts.append(f"{node.name or node.id}: {'/'.join(names)}")
        return " | ".join(parts) if parts else "（没有可用地点）"

    def reach_table(self, node_id: str, *, with_actions: bool = True) -> str:
        """从当前位置出发的完整可达表。

        只列相邻地点的话，模型不知道远处有什么、要走多久，也不知道该往 JSON 里写哪个 id。
        这里一次给全：**按区域分组**的 地点名 + id + 走过去要几 tick + 到了能做什么。
        跨区走的耗时是"区域内走到出口 + 跨区 + 到目标房间"的总和，模型只写一次 walk_to 就行。
        """

        cached = self._reach_cache.get(node_id if with_actions else f"{node_id}!plain")
        if cached is not None:
            return cached
        node_map = self.world.node_map()
        if node_id not in node_map:
            return "你当前不在任何已知地点。"
        graph = self.world.adjacent()
        minutes = max(1, round(self.tick_seconds / 60))
        current_zone = self.world.zone_of(node_id)
        zone_map = self.world.zone_map()
        # 当前区域排最前，其余按配置顺序
        zone_order = [current_zone] + [
            zone.id for zone in self.world.zones if zone.id != current_zone
        ]
        rows: dict[str, list[tuple[int, str]]] = {zone_id: [] for zone_id in zone_order}
        blocked: list[str] = []
        for target_id, target in node_map.items():
            label = f"{target.name or target_id} {target_id}"
            zone_id = self.world.zone_of(target_id)
            if zone_id not in rows:
                rows[zone_id] = []
                zone_order.append(zone_id)
            if target_id == node_id:
                detail = "你现在在这里"
                if with_actions:
                    names = self._action_names(target_id)
                    if names:
                        detail += f"｜能做：{names}"
                rows[zone_id].append((-1, f"- {label}：{detail}"))
                continue
            cost = travel_cost(graph, node_id, target_id)
            if cost is None:
                blocked.append(label)
                continue
            detail = f"{cost} tick（约 {max(1, cost * minutes)} 分钟）"
            if with_actions:
                names = self._action_names(target_id)
                detail += f"｜能做：{names}" if names else "｜（没什么特别能做的）"
            rows[zone_id].append((cost, f"- {label}：{detail}"))

        sections: list[str] = []
        for zone_id in zone_order:
            zone_rows = rows.get(zone_id) or []
            if not zone_rows:
                continue
            zone_rows.sort(key=lambda item: item[0])
            zone = zone_map.get(zone_id)
            title = f"【{zone.name if zone else zone_id}】"
            if zone_id == current_zone:
                title += "（你现在在这里）"
            elif zone is not None and zone.note:
                title += f" {_one_line(zone.note, 30)}"
            sections.append(
                title + "\n" + "\n".join(line for _order, line in zone_rows)
            )
        text = (
            "# 你可以去的地方（tick 是走过去要花的步数，1 tick ≈ "
            f"{minutes} 分钟；想去就直接用 walk_to 写它的 id）\n"
            "（说话、想事情这类通用动作任何地方都能做，这里只列各地特有的；"
            "跨区域只要写目标地点的 id，路上的转车由系统安排）\n"
            + "\n".join(sections)
        )
        if blocked:
            text += "\n（现在过不去：" + "、".join(blocked) + "）"
        text += (
            "\n走远的地方不用分成好几步，一次 walk_to 就会沿最短路线走完，"
            "中途不会停下来。"
        )
        self._reach_cache[node_id if with_actions else f"{node_id}!plain"] = text
        return text

    def _action_names(self, node_id: str) -> str:
        """只列这个地点特有的动作：通用动作每个地方都一样，全列一遍纯属浪费。"""

        names = [
            action.name or action.id
            for action in self.world.actions_in(node_id)
            if action.id != "walk_to" and action.scope == "node"
        ]
        if not names:
            return "只有通用动作"
        return " / ".join(names)

    def remote_action_hint(self, node_id: str, hidden: set[str] | None = None) -> str:
        """「别处才能做的动作」：给出 id 和地点，她才写得出「walk_to + 那个动作」。

        上面那张「这一轮你能写的 type」只列当前位置能做的，模型据此以为别处的动作不能写，
        于是只写一步 walk_to 就完事——到了地方还得再问一次，或者在原地干等。
        """

        if not bool(getattr(self.world, "remote_action_travel", True)):
            return ""
        blocked = hidden or set()
        graph = self.world.adjacent()
        here = {item.id for item in self.world.actions_in(node_id)}
        nearest: dict[str, tuple[int, str]] = {}
        for target in self.world.nodes:
            if target.id == node_id:
                continue
            cost = travel_cost(graph, node_id, target.id)
            if cost is None:
                continue
            for action in self.world.actions_in(target.id):
                if action.id == "walk_to" or getattr(action, "scope", "global") != "node":
                    continue
                if action.id in here:
                    # 这里就能做：已经列在上面了，别混进"别处"那一行
                    continue
                if action.id in blocked or not self._action_usable_now(action):
                    continue
                known = nearest.get(action.id)
                if known is not None and known[0] <= cost:
                    continue
                nearest[action.id] = (cost, target.name or target.id)
        if not nearest:
            return ""
        rows = sorted(nearest.items(), key=lambda item: item[1][0])[:12]
        text = "、".join(f"{action_id}（{place}）" for action_id, (_cost, place) in rows)
        return (
            f"（别处才能做的动作也能写：{text}——写它们时前面先带一步 walk_to 去那个地点。）\n"
        )

    def precondition_hints(self, node_id: str) -> str:
        lines: list[str] = []
        for action in self.world.actions:
            pre = action.preconditions
            hints: list[str] = []
            if pre.not_state:
                hints.append(f"不能在 {'/'.join(pre.not_state)} 状态")
            if pre.min_energy is not None:
                hints.append(f"精力要高于 {pre.min_energy:.2f}")
            if hints:
                lines.append(f"- {action.id}：{'；'.join(hints)}")
        return "\n".join(lines) if lines else "（没有额外限制）"

    def scene_layer(
        self,
        node: NodeDef | None,
        available_tools: dict[str, str],
        hidden_actions: set[str] | None = None,
    ) -> str:
        if node is None:
            return "你当前不在任何已知地点。"
        hidden = hidden_actions or set()
        actions = [
            item
            for item in self.world.actions_in(node.id)
            if self._action_usable_now(item) and item.id not in hidden
        ]
        action_lines = "\n".join(
            self._action_line(a) for a in actions if a.id != "walk_to"
        )
        # 能写的 type 是随地点变化的，所以放在这一层（而不是固定的「输出格式」层）
        usable = [a.id for a in actions if a.id != "walk_to"]
        type_line = "、".join(usable) if usable else "say"
        if "walk_to" not in usable:
            type_line += "、walk_to"
        tool_lines = self.global_tool_lines(available_tools)
        zone = self.world.zone_map().get(self.world.zone_of(node.id))
        where = f"{zone.name} · {node.name or node.id}" if zone else (node.name or node.id)
        zone_note = f"\n{_one_line(zone.note, 40)}" if zone is not None and zone.note else ""
        exclusive = [
            a.name or a.id
            for a in actions
            if a.id != "walk_to" and getattr(a, "scope", "global") == "node"
        ]
        exclusive_line = (
            "这里有几件别处做不了的事："
            + "、".join(exclusive)
            + "——人都到这儿了，可以顺手挑一件做（别每次都同一件）。\n\n"
            if exclusive
            else ""
        )
        search_hint = self.search_hint(actions)
        remote_hint = self.remote_action_hint(node.id, hidden)
        return (
            f"你当前在【{where}】（id：{node.id}）。\n"
            f"{node.prompt}{zone_note}\n\n"
            f"这里的氛围：{node.atmosphere.describe()}\n\n"
            f"这一轮你能写的 type 只有：{type_line}\n\n"
            f"{exclusive_line}"
            f"你可以执行的动作（工具型动作只要说明想做什么，具体参数由系统转交）：\n"
            f"{action_lines}\n\n"
            f"{tool_lines}\n\n"
            f"{search_hint}"
            "想做只有别处能做的事（比如在书房上网）：**先写一步 walk_to，紧接着把要做的那个动作也写进\n"
            "同一串 actions**，系统会带你走过去再执行；只写移动的话，到了那儿还得再问你一次。\n\n"
            f"{remote_hint}"
            f"# 动作前置条件\n{self.precondition_hints(node.id)}"
        )

    def _action_usable_now(self, action: Any) -> bool:
        """这个动作在当前时段能不能列进提示词。

        目前只管一条：没到夜里就不给「睡觉」——不然她下午三点也能写睡觉，
        一觉醒来正好是半夜，作息就乱了。时段在「作息与夜晚」里配。
        """

        if str(getattr(action, "id", "")) != "sleep":
            return True
        now = self._now()
        if now is None:
            return True  # 拿不到钟点时不在这里拦，交给引擎的解析白名单
        return self.world.sleep_allowed_now(now.hour)

    @staticmethod
    def _looks_like_lookup(action: Any) -> bool:
        """这个动作是不是"去查外面的信息"（搜索、天气这类）。"""

        if str(getattr(action, "llm_level", "")) not in ("tool", "command"):
            return False
        haystack = " ".join(
            [
                str(getattr(action, "id", "") or ""),
                str(getattr(action, "name", "") or ""),
                str(getattr(action, "description", "") or ""),
                " ".join(str(item) for item in (action.tool_list() or [])),
            ]
        ).lower()
        return any(
            keyword in haystack
            for keyword in ("search", "web", "news", "weather", "查", "搜")
        )

    def search_hint(self, actions: list[Any]) -> str:
        """场景里有"能查东西"的动作时，加一段"不确定就去查、别瞎猜"的规矩。

        没有这类动作时不写——省得她以为有个能查的工具、凭空编一个。
        """

        if not any(self._looks_like_lookup(action) for action in actions):
            return ""
        return (
            "**不知道就先查，别猜。** 涉及最新消息、实时数据（比分/股价/天气/热搜）、具体数字、"
            "你不熟悉的人或事，先用上面能查的动作查一遍再回答；不要凭印象编，也不要含糊带过。\n"
            "查的时候把「查什么」写清楚（谁、什么时候、哪方面）；查到了用你自己的话讲重点，"
            "别照抄原文、别念网址；查不到或结果不相关，就照实说没查到，别拿旧印象凑。\n"
            "一个话题要分几个角度查时，可以在动作里一次写 2~3 条 queries"
            "（例如「今天的热点新闻」和「某某事件 最新进展」），系统会逐条查完再汇总；"
            "只查一件事就写 intent，不用勉强凑多条。\n"
            "已经查过、结果里已经有的，不要重复再查。\n\n"
        )

    def _action_line(self, action: Any) -> str:
        if str(getattr(action, "desc_mode", "full")) == "brief":
            # 看一眼名字就知道干嘛的动作（点头、抱抱、亲亲…）不写说明：
            # 几十个这样的动作各带一句描述，只会把提示词撑长，信息量为零。
            text = f"- {action.id}（{action.name or action.id}）"
        else:
            text = f"- {action.id}：{action.description or action.name}"
        if str(getattr(action, "tool_flow", "simple")) == "search":
            text += "（联网检索型：把要查的写进 intent，也可以给 1~3 条 queries 分角度查）"
        elif action.llm_level == "tool":
            text += "（工具型：只填 intent，说明你想做什么；参数会自动补全）"
        if action.llm_level == "command":
            text += "（指令型：只填 intent 说清想让它干什么，参数由系统按指令说明补全）"
        if str(getattr(action, "duration_mode", "fixed")) == "llm":
            # 睡多久这类事交给她自己定：把范围直接写在动作这一行，她才会填 duration
            low = max(60, int(getattr(action, "duration_min", 0) or 600))
            high = max(low, int(getattr(action, "duration_max", 0) or low))
            text += (
                f"（时长由你定：{_duration_text(low)} ~ {_duration_text(high)}，"
                "写 duration，单位是秒）"
            )
        if getattr(action, "scope", "global") == "node":
            text += "（只有在这个地点才能做）"
        return text

    def global_tool_lines(self, available_tools: dict[str, str]) -> str:
        """只列「任何地点都能用」的通用工具；地点专属工具留给动作去带。"""

        names = sorted(
            set(str(name) for name in (self.world.global_allowed_tools or []))
            & set(available_tools)
        )
        if not names:
            return "# 通用工具\n（当前没有配置任何地点通用的工具）"
        lines = "\n".join(
            f"- {name}：{available_tools.get(name, '')}" for name in names
        )
        return (
            "# 通用工具（不分地点，直接说你想做什么即可，参数由系统处理）\n" + lines
        )

    # ---------------- 第 4 层 ----------------

    def runtime_layer(
        self,
        state: WorldState,
        *,
        node: NodeDef | None,
        memories: list[RecalledMemory] | None = None,
        engagement_hint: str = "",
        other_context: str = "",
        extra_notes: list[str] | None = None,
        generic_memories: list[str] | None = None,
        focus_user: str = "",
        recent_chat: list[dict[str, Any]] | None = None,
        style_block: str = "",
        weather: str = "",
        recent_search: str = "",
        abilities_line: str = "",
        pending_event: str = "",
        event_journal: str = "",
        hidden_actions: set[str] | None = None,
        current_session: str = "",
        session_labels: dict[str, str] | None = None,
        chat_images: dict[str, str] | None = None,
        profile_text: str = "",
    ) -> str:
        now = self._now()
        # 这次是在哪个会话里说话：水位线、来源标签都按它算
        session_key = str(current_session or state.session_id or "")
        if session_key and session_key != str(state.session_id or ""):
            info = (getattr(state, "chat_watermarks", None) or {}).get(session_key) or {}
            replied_until = float(info.get("until") or 0.0)
            replied_seq = int(info.get("seq") or 0)
        else:
            replied_until = float(getattr(state, "chat_replied_until", 0.0) or 0.0)
            replied_seq = int(getattr(state, "chat_replied_seq", 0) or 0)
        # 记忆按时间从早到晚排，越靠下越新——模型读提示词时最后的更"近"。
        ordered = sorted(
            memories or [], key=lambda item: float(getattr(item, "created_at", 0.0))
        )
        memory_text = (
            "\n".join(
                item.render(stamp=self._memory_stamp(item.created_at, now))
                for item in ordered
            )
            if ordered
            else NO_MEMORY_TEXT
        )
        current_action = state.current_action or {}
        if current_action:
            action_desc = str(current_action.get("desc") or current_action.get("type", "无"))
        else:
            action_desc = "没在做什么"
        if state.state == "sleeping":
            action_desc = "正在睡觉"
        elif state.state == "napping":
            action_desc = "正在小睡"

        active_users = state.recent_active_users(5)
        user_text = (
            "、".join(
                f"{item.get('name') or item.get('user_id')}"
                for item in active_users
            )
            if active_users
            else "（最近没人说话）"
        )
        # 「最近发生的事」只留真的做了什么：光开口说话 / 心里想一下的不算
        # （那些在聊天记录和「你最近说过的话」里已经有了）
        lines: list[str] = []
        for item in state.recent_events or []:
            if self._trivial_event(item):
                continue
            line = self._event_line(item)
            if line:
                lines.append(line)
        event_text = "\n".join(lines[-5:]) or "（没什么特别的事）"
        thought_text = self._inner_voice(state)

        blocks = [
            "# 你的状态\n"
            + self._state_block(state, dwell_minutes=self._dwell_minutes(state)),
            f"# 当前动作：{action_desc}",
            f"# 当前位置：{node.name if node else '未知'}",
            f"# 最近活跃的用户：{user_text}",
        ]
        if now is not None:
            # 时间紧跟在"她是谁、在哪、在干嘛"之后：
            # 这几行在相邻两次调用之间通常是稳定的，放在最前面才能让前缀缓存尽量长；
            # 时钟每分钟都变，它后面的内容（记忆、群聊…）本来就每轮都变，放这里不浪费。
            blocks.append(self._clock_line(now))
        if weather:
            # 天气跟着时钟走（都是"此刻外面什么样"），同样属于每轮都可能变的内容
            blocks.append(weather)
        if recent_search:
            # 「最近查过什么」也贴在这附近：省得她隔一轮又用同样的词查一遍
            blocks.append(recent_search)
        if node is not None:
            # 可达表只跟"她在哪"有关，比状态稳、比记忆易变得多，放时钟后面
            blocks.append(self.reach_table(node.id))
        in_progress = self._in_progress_block(state)
        if in_progress:
            blocks.append(in_progress)
        if abilities_line:
            # 能力值只给人话，不给数字：她自己知道手稳不稳，不知道 0.62
            blocks.append(abilities_line)
        if pending_event:
            # 心里挂着的事：按需注入——没挂着事时这一整段都不存在
            blocks.append(pending_event)
        if event_journal:
            # 她自己的账：正在经历 / 最近发生在我身上的事 / 还挂着的
            blocks.append(event_journal)
        external = self.external_state_block(state)
        if external:
            # 别的插件的状态（今日穿搭、背包、经济…）：记在她这儿，别说完就忘
            blocks.append(external)
        if engagement_hint:
            blocks.append(engagement_hint)
        blocks.append(
            "# 这里让你想起（会影响你的情绪和语气，但不一定要说出来；"
            "按时间从早到晚，越靠下的事情发生得越近）：\n"
            f"{memory_text}"
        )
        if generic_memories:
            who = focus_user or "对方"
            blocks.append(
                f"# 关于 {who} 你还记得：\n"
                + "\n".join(f"- {m}" for m in generic_memories)
            )
        blocks.append(f"# 最近发生的事\n{event_text}")
        blocks.append(f"# 你的内心活动（仅你可见，绝对不要直接复述）\n{thought_text}")
        if profile_text:
            # 「这个人是谁」放在聊天记录前面：先认清人，再看说了什么
            blocks.append(profile_text)
        blocks.extend(
            self.chat_blocks(
                recent_chat,
                getattr(state, "chat_summary", ""),
                # 「刚才在聊什么」按会话取，过期的不带（老存档回落全局那份）
                state.chat_note_text(
                    session_key,
                    max_minutes=int(
                        getattr(getattr(self.world, "context", None), "chat_note_max_minutes", 30)
                        or 0
                    ),
                    # now 是 datetime（提示词里显示用），过期判定要的是时间戳
                    now=float(now.timestamp()) if hasattr(now, "timestamp") else float(now or 0.0),
                ),
                preview=state.chat_preview_text(session_key),
                summaries=dict(getattr(state, "chat_summaries", None) or {}),
                # 三个"带几行"都来自世界配置：渲染这一层不再写死
                answered_max=int(
                    getattr(getattr(self.world, "context", None), "chat_answered_lines", 30)
                    or 0
                ),
                elsewhere_max=int(
                    getattr(getattr(self.world, "context", None), "chat_elsewhere_lines", 12)
                    or 0
                ),
                replied_until=replied_until,
                replied_seq=replied_seq,
                recent_replies=list(getattr(state, "recent_replies", []) or []),
                mine_names=self.bot_name_list(state),
                current_session=current_session,
                session_labels=session_labels,
                image_marks=chat_images,
            )
        )
        # 其他插件注入的内容跟着这条消息走，每轮都不一样，放到靠后的位置
        if other_context:
            blocks.append("# 其他插件提供的上下文\n" + other_context)
        # 还没聊完的事：接在聊天记录之后——"接着上次那句问"就靠它
        topics_block = self.open_topics_layer(
            self._due_open_topics(state, current_session, focus_user=focus_user),
            who_name="",
            pronoun=pronoun_for(getattr(self.world, "gender", "female")),
        )
        if topics_block:
            blocks.append(topics_block)
        if extra_notes:
            blocks.extend(note for note in extra_notes if note)
        # 风格段放在最后：近因效应最强，而且是"这一轮这么说话"的直接指令
        if style_block:
            blocks.append(style_block)
        return "\n\n".join(blocks)

    # ---------------- 第 4 层的分段构件 ----------------

    def _due_open_topics(
        self, state: WorldState, session_key: str, *, focus_user: str = ""
    ) -> list[dict[str, Any]]:
        """到点可以提的「还没聊完的事」：按会话筛，到时间才带出来。

        没过时间的继续挂着（不到点就提像查户口）；同一个人身上最多带两条。
        """

        now = self._now()
        stamp = now.timestamp() if now is not None else 0.0
        key = str(session_key or state.session_id or "")
        picked: list[dict[str, Any]] = []
        for item in state.open_topics or []:
            if not isinstance(item, dict):
                continue
            topic_session = str(item.get("session") or "")
            if topic_session and key and topic_session != key:
                continue
            due = float(item.get("next_ask_at") or 0.0)
            if stamp and due and due > stamp:
                continue
            if focus_user and str(item.get("who") or "") not in ("", focus_user):
                continue
            picked.append(item)
        # 同一个人最多两条：多了她会像客服在逐条回访
        by_who: dict[str, int] = {}
        result: list[dict[str, Any]] = []
        for item in picked:
            who = str(item.get("who") or "")
            if by_who.get(who, 0) >= 2:
                continue
            by_who[who] = by_who.get(who, 0) + 1
            result.append(item)
        return result

    def _dwell_minutes(self, state: WorldState) -> float:
        """她在当前这个地点已经待了多久（分钟）；拿不到起始时间就是 0。"""

        since = float(state.node_since or 0.0)
        now = self._now()
        if since <= 0 or now is None:
            return 0.0
        return max(0.0, now.timestamp() - since) / 60.0

    @staticmethod
    def _state_block(state: WorldState, *, dwell_minutes: float = 0.0) -> str:
        """数值不只是给数字：连范围、含义、当前程度一起说清楚。"""

        lines = [
            "你的状态（每个数值都是 0~1，越大越强）：",
            f"- 心情：{state.mood}",
            f"- 精力 {state.energy:.2f}：低于 0.25 会明显犯困——白天先小睡一会儿（10~60 分钟）就能缓过来，"
            "睡整觉留给夜里和凌晨；高于 0.8 很有精神",
            f"- 孤独感 {state.loneliness:.2f}：超过 0.7 会很想找人说话，低于 0.3 觉得一个人也挺好",
            f"- 好奇心 {state.curiosity:.2f}：超过 0.7 想找点新鲜事做",
            f"- 心潮 {state.affect:.2f}：情绪被激起的强度（不是开心程度），"
            f"现在是「{_affect_hint(state.affect)}」；"
            "越高，内心活动越翻涌、说出来的感情越浓、越容易做亲昵或冲动的举动",
            f"- 效价 {state.valence:.2f}：心情的好坏（0.5 是中性），"
            f"现在是「{_valence_hint(state.valence)}」；"
            "它管的是情绪朝哪个方向，和心潮一起决定你这一轮的表达形态——"
            "两者都只是内部感受，不要报数字。",
            f"- 无聊 {state.boredom:.2f}：超过 0.8 待不住，想换个地方；超过 0.45 想找点事做",
            "这些是你的内部感受，不要报数字，但语气、动作和用词要能体现出来。",
        ]
        if dwell_minutes > 0:
            # 她得知道自己"在这儿待了多久"：久待无聊涨得更快，也是换地方的理由
            lines.append(
                f"- 你在这个地方已经待了 {_dwell_text(dwell_minutes)}；"
                "待得越久越坐不住，想换地方或找点事做都正常。"
            )
        return "\n".join(lines)

    def action_label(self, action_id: Any) -> str:
        action = self.world.action_map().get(str(action_id or ""))
        return (action.name or action.id) if action is not None else str(action_id or "")

    def node_label(self, node_id: Any) -> str:
        node = self.world.node_map().get(str(node_id or ""))
        return (node.name or node.id) if node is not None else str(node_id or "")

    @staticmethod
    def _trivial_event(item: dict[str, Any]) -> bool:
        """这条够不上「她做了什么」吗（是的话不进「最近发生的事」）。

        两种都算够不上：开口说话 / 心里想一下这类没留下痕迹的动作，
        以及发言等待回应之类的碎片记录——它们只会渲染成一行原始数据。
        """

        kind = str((item or {}).get("kind") or "")
        if kind not in NOTABLE_EVENT_KINDS:
            return True
        if kind not in ("action", "action_start"):
            return False
        detail = (item or {}).get("detail")
        if not isinstance(detail, dict):
            return False
        return str(detail.get("type") or "") in CHATTY_EVENT_ACTIONS

    def _event_line(self, item: dict[str, Any]) -> str:
        """把一条最近事件写成人话；写不出人话的就返回空串，不当众糊原始数据。"""

        kind = str(item.get("kind") or "")
        detail = item.get("detail") or {}
        if not isinstance(detail, dict):
            return ""
        if kind in ("action", "action_start"):
            label = self.action_label(detail.get("type"))
            head = "开始" if kind == "action_start" else "做了"
            return f"- {head}：{label}" if label else ""
        if kind == "move":
            where = self.node_label(detail.get("to"))
            return f"- 走到了：{where}" if where else ""
        if kind == "plan":
            reason = str(detail.get("reason") or "")
            return f"- 安排：{_short(reason, 40)}" if reason else ""
        if kind in ("tool", "tool_call", "tool_result"):
            label = self.action_label(detail.get("action"))
            return f"- 查了资料：{label}" if label else ""
        if kind == "chain":
            schedule = str(detail.get("schedule") or "")
            return f"- 接着执行日程：{schedule}" if schedule else ""
        if kind == "schedule":
            note = str(detail.get("note") or "")
            if note:
                return f"- 日程到点：{_short(note, 40)}"
            schedule = str(detail.get("id") or "")
            return f"- 日程到点：{schedule}" if schedule else ""
        if kind == "interrupt":
            label = self.action_label(detail.get("action"))
            return f"- 被打断：{label}" if label else "- 手头的事被打断了"
        if kind == "wake_up":
            return "- 被人叫醒了"
        if kind == "startled":
            return "- 被吵醒了"
        if kind in ("event", "event_help"):
            title = str(detail.get("title") or "")
            return f"- 遇到的事：{_short(title, 40)}" if title else ""
        return ""

    def _in_progress_block(self, state: WorldState) -> str:
        """她手头的事：正在做什么、计划里还剩什么。

        没有这段的话，她跨轮次就像失忆：上一轮安排的事、正要接着做的事都看不到。
        三段分开写是有原因的：只写「你安排好的是……」会让模型把"写进计划"当成
        "已经做完"，于是嘴上说照片拍了、其实那一步还排在后面。
        """

        lines: list[str] = []
        action = state.current_action or {}
        if action:
            total = max(1, int(action.get("duration_ticks", 1) or 1))
            left = max(0, total - int(action.get("elapsed_ticks", 0) or 0))
            what = action.get("desc") or self.action_label(action.get("type"))
            lines.append(f"- 正在做（还没做完）：{what}（还要约 {left} tick）")
        plan = state.current_plan or {}
        steps = plan.get("steps") or []
        current = int(plan.get("current_step", 0) or 0)
        done = [
            self.action_label(step.get("action"))
            for step in steps[:current]
            if isinstance(step, dict) and step.get("action")
        ]
        # 「下一步」如果就是她手上正在做的这件事，它已经在第一行写过了，这里别重复
        running = -1
        if (
            action
            and current < len(steps)
            and isinstance(steps[current], dict)
            and steps[current].get("action") == action.get("type")
        ):
            running = current
        pending = [
            self.action_label(step.get("action"))
            for index, step in enumerate(steps[current:], start=current)
            if index != running and isinstance(step, dict) and step.get("action")
        ]
        if done:
            lines.append("- 已经做完的：" + "、".join(done[-5:]))
        if pending:
            line = "- 接下来排队（**一件都还没做**）：" + "、".join(pending[:5])
            if plan.get("reason"):
                line += f"（这么安排是因为：{_one_line(plan.get('reason'), 30)}）"
            lines.append(line)
        if not lines:
            # 手头没安排时，把"最近一次生成的计划"也带上：她才知道刚才打算过什么，
            # 不至于每轮都从零开始重新想。
            last = state.last_plan if isinstance(state.last_plan, dict) else {}
            names = [
                self.action_label(step.get("action"))
                for step in (last.get("steps") or [])
                if isinstance(step, dict) and step.get("action")
            ]
            if names:
                line = "- 你上一次安排好的是：" + " → ".join(names[:5])
                if last.get("reason"):
                    line += f"（原因：{_one_line(last.get('reason'), 30)}）"
                if last.get("at"):
                    line += f"，那是 t={int(last.get('at'))} 的事，已经做完了"
                lines.append(line)
        if not lines:
            return ""
        text = "# 你手头的事（接着做，不用重新打算）\n" + "\n".join(lines)
        if pending:
            text += (
                "\n写进安排 ≠ 已经做完：「接下来排队」里的每一件都还没发生，"
                "不要当成自己做过了，也不要在 say 里提前汇报结果。"
            )
        return text

    def external_state_block(self, state: WorldState) -> str:
        """别的插件的状态（例如生图插件给的「今日穿搭」）。过期的不出现。"""

        items = dict(getattr(state, "external_state", None) or {})
        if not items:
            return ""
        now = self._now()
        now_ts = float(now.timestamp()) if hasattr(now, "timestamp") else float(now or 0.0)
        lines: list[str] = []
        for slot, info in items.items():
            if not isinstance(info, dict):
                continue
            try:
                expires = float(info.get("expires_at") or 0.0)
            except (TypeError, ValueError):
                expires = 0.0
            if expires and now_ts > expires:
                continue
            text = _one_line(info.get("text"), 200)
            if not text:
                continue
            label = str(info.get("label") or slot).strip()
            try:
                at = float(info.get("at") or 0.0)
            except (TypeError, ValueError):
                at = 0.0
            when = ""
            if at > 0:
                minutes = max(0, int((now_ts - at) // 60))
                if minutes >= 60:
                    when = f"（{minutes // 60} 小时前拿到的）"
                elif minutes > 0:
                    when = f"（{minutes} 分钟前拿到的）"
                else:
                    when = "（刚拿到的）"
            lines.append(f"- {label}：{text}{when}")
        if not lines:
            return ""
        return (
            "# 你的状态槽（按事实用，不要复述格式）\n"
            "（这些是你之前调动作时留下的：别的插件的返回，或者你自己定下来的事——"
            "比如「在看的剧」；下一轮还在，别当成第一次听说。）\n"
            + "\n".join(lines)
        )

    @staticmethod
    def _inner_voice(state: WorldState) -> str:
        """内心活动 = 最近一次推理草稿；模型没写草稿时退回她留下的心里话。"""

        reasoning = state.last_reasoning or {}
        if reasoning:
            lines = [
                f"- {_REASONING_LABELS.get(key, key)}：{reasoning[key]}"
                for key in _REASONING_ORDER
                if reasoning.get(key)
            ]
            if lines:
                return "\n".join(lines)
        thoughts = [
            str(item.get("content"))
            for item in state.thoughts[-3:]
            if item.get("content")
        ]
        if thoughts:
            return "\n".join(f"- {text}" for text in thoughts)
        return "（你还没细想这件事）"

    def chat_blocks(
        self,
        recent_chat: list[dict[str, Any]] | None,
        summary: str = "",
        note: str = "",
        *,
        summaries: dict[str, Any] | None = None,
        answered_max: int = 0,
        elsewhere_max: int = 0,
        preview: str = "",
        replied_until: float = 0.0,
        replied_seq: int = 0,
        recent_replies: list[Any] | None = None,
        mine_names: list[str] | None = None,
        current_session: str = "",
        session_labels: dict[str, str] | None = None,
        image_marks: dict[str, str] | None = None,
    ) -> list[str]:
        """把群聊背景拆成三块：之前聊过的概览、现在在聊什么、她刚说过的话。

        - **之前的群聊**：她上一轮已经回应过的那批（压缩成一条概览）+ 更早的压缩摘要。
          这些不该再被回应，但也不能凭空消失——否则她下一轮会忘了上下文。
        - **最近在聊什么**：水位线之后的原文，带时间；同一个人连着说的几句合并成一行；
          图片的转述描述跟着消息一起带进来。
        - **别的地方同时在发生什么**：同一个她在别的群 / 私聊里听到的话（会话组才有）。
          这些是背景，不用在这里回应——不然她会在群里答私聊的问题。
        - **你最近说过的话**：最近几条她自己发出去的，用来避免重复同样的开头和句式。

        ``image_marks``：这一轮**真的附上去**的那几张图的编号（``图片地址 -> 图1``）。
        带编号的记录会标上「（见图1）」，她才知道记录里的图和附件里的图怎么对上。
        """

        # 条数上限由调用方（engine.chat_window）按配置决定，这里不再二次截断，
        # 否则「最多携带多少条聊天」调大了也不会生效。
        items = list(recent_chat or [])
        now = self._now()
        blocks: list[str] = []
        current = str(current_session or "")
        marks = {str(key): str(value) for key, value in (image_marks or {}).items()}
        marks_used = False

        def _origin(item: dict[str, Any]) -> str:
            return str(item.get("origin") or "")

        def _is_here(item: dict[str, Any]) -> bool:
            origin = _origin(item)
            if not origin or not current:
                return True
            return origin == current

        label_map = dict(session_labels or {})
        here = [item for item in items if _is_here(item)]
        elsewhere = [item for item in items if not _is_here(item)]
        fresh = [
            item
            for item in here
            if chat_item_is_fresh(
                item, replied_until=replied_until, replied_seq=replied_seq
            )
        ]

        # 「水位线以下的也要原样给她看」：只给一行概览的话，她根本接不上前文。
        # 还没回过的和已经回过的是同一批留档的上下两截，分开列、各自标清楚。
        answered = [
            item
            for item in here
            if not chat_item_is_fresh(
                item, replied_until=replied_until, replied_seq=replied_seq
            )
        ]

        seen: set[str] = set()

        context = getattr(self.world, "context", None)
        fresh_chars = max(40, int(getattr(context, "chat_line_chars", 500) or 500))
        back_chars = max(
            20, int(getattr(context, "chat_answered_line_chars", 100) or 100)
        )
        budget = max(400, int(getattr(context, "chat_total_chars", 4000) or 4000))

        def _render_rows(rows: list[dict[str, Any]], line_chars: int) -> list[str]:
            """把留档渲染成一行行（同一个人连着说的并成一行）。

            ``line_chars``：这一批每条最多写多少字。**还没回过她的那批要给足**
            （那是她真正要读、要回的内容），已经回过 / 别处的按背景处理，可以短。
            """

            nonlocal marks_used
            rendered: list[tuple[dict[str, Any], str]] = []
            for item in rows:
                name = str(item.get("name") or item.get("user_id") or "").strip()
                raw = str(item.get("text") or "")
                if FORWARD_SUMMARY_MARK in raw:
                    # 转发摘要：标记换成短前缀，这一行的字数上限也放宽
                    raw = raw.replace(FORWARD_SUMMARY_MARK, "（转发：").rstrip() + "）"
                    text = clip_line(raw, max(FORWARD_LINE_CHARS, line_chars))
                else:
                    text = clip_line(raw, line_chars)
                if not text:
                    continue
                labels = [
                    marks[str(ref)]
                    for ref in (item.get("images") or [])
                    if str(ref) in marks
                ]
                if labels:
                    # 这条带的图这一次直接发给主模型看了：标上编号，方便和图对照
                    marks_used = True
                    text = f"{text}（见{'、'.join(labels)}）"
                # 同一条消息被两个钩子各记一次时，这里只留一行：
                # 提示词里同一句话出现两三遍，模型会以为对方反复说了同样的话。
                # 按**正文**算指纹：两份分别是"带注释"和"不带注释"时也要认出来
                # （短消息尤其容易漏，例如「亲亲」和「亲亲［这条消息 @ 了：你（…）］」）。
                fingerprint = f"{item.get('is_self')}:{_fingerprint(chat_core_text(text))}"
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                rendered.append((item, text))

            out: list[str] = []
            texts = {id(row_item): row_text for row_item, row_text in rendered}
            # 分组规则和"算几条"完全一致（见 core/state.py 的 group_chat_items）：
            # 同一个人 + 同一个会话 + 间隔不超过 5 分钟 → 并成一行
            for group in group_chat_items([item for item, _text in rendered]):
                item = group[-1]
                body = " / ".join(texts.get(id(one), "") for one in group)
                name = str(item.get("name") or item.get("user_id") or "").strip()
                if item.get("internal"):
                    # 插件写的"她身上发生的事"：标出来，免得被当成群里谁说的话
                    who = "（她自己身上发生的事）"
                elif item.get("is_self"):
                    who = "你"
                else:
                    identifier = str(item.get("user_id") or "").strip()
                    who = (
                        f"{name}({identifier})"
                        if name and identifier
                        else (name or identifier)
                    )
                at = float(item.get("at", 0) or 0.0)
                stamp = self._chat_time(at, now)
                out.append(f"- [{stamp}] {who}: {body}")
            return out

        def _render_other(rows: list[dict[str, Any]]) -> list[str]:
            """别的地方同时听到的话（只当背景）：同一个会话里同一个人连着说的并成一行。"""

            out: list[str] = []
            prepared: list[tuple[dict[str, Any], str]] = []
            for item in rows:
                text = clip_line(item.get("text"), back_chars)
                if not text:
                    continue
                prepared.append((item, text))
            texts = {id(row_item): row_text for row_item, row_text in prepared}
            for group in group_chat_items([item for item, _text in prepared]):
                item = group[-1]
                first = group[0]
                text = " / ".join(texts.get(id(one), "") for one in group)
                origin = _origin(item)
                where = label_map.get(origin, origin)
                name = str(item.get("name") or item.get("user_id") or "").strip()
                who = "你" if first.get("is_self") else (name or "有人")
                out.append(f"- 〔{where}〕{who}：{text}")
            return out

        def _last_groups(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
            """按"合并后的行"取最近 N 条：一个人连发十条只占一条额度。"""

            return take_last_chat_groups(rows, limit)

        # 已经回过的那一截：原样列出来，只是标明"这批已经答过了、别重复回"
        answered_rows = _last_groups(
            answered, answered_max if answered_max > 0 else ANSWERED_CHAT_MAX
        )
        answered_lines = _render_rows(answered_rows, back_chars)

        history: list[str] = []
        def _summary_rows() -> list[tuple[str, str]]:
            """→ [(会话 id, 摘要)]：当前会话排最前（没有分会话数据时用老的那一份）。"""

            rows: list[tuple[str, str]] = []
            for key, value in dict(summaries or {}).items():
                if isinstance(value, dict):
                    text = str(value.get("text") or "").strip()
                else:
                    text = str(value or "").strip()
                if text:
                    rows.append((str(key), text))
            if not rows and str(summary or "").strip():
                rows = [("", str(summary).strip())]
            rows.sort(key=lambda item: 0 if item[0] == current else 1)
            return rows

        # 「更早聊过的」按会话分开列：群里聊的和私聊聊的本来就不是一回事，
        # 混成一段她就分不清哪句是哪儿的。当前会话排最前。
        for key, text in _summary_rows():
            label = "这里" if (not key or key == current) else (
                label_map.get(key) or key
            )
            history.append(f"- 〔{label}〕更早聊过的：{_one_line(text, 600)}")
        note = " ".join(str(note or "").split())
        if not answered_lines:
            # 没有原文可列（老存档 / 这批已经被裁掉）时，退回那一行概览
            if preview:
                history.append(f"- 你刚回应过的那批：{_one_line(preview, 240)}")
            elif note:
                history.append(f"- 上一轮你们在聊：{_one_line(note, 120)}")
        elif note:
            # 她这一轮看到的是真原文，但"刚才在聊什么"是她自己上一轮写的总结，
            # 两边都给她才不会把话题接丢（以前这句只在没有原文时才出现，等于白写）
            history.append(f"- 上一轮你们在聊：{_one_line(note, 120)}（背景）")

        lines = _render_rows(fresh, fresh_chars)
        other_lines = _render_other(
            _last_groups(
                elsewhere, elsewhere_max if elsewhere_max > 0 else ELSEWHERE_CHAT_MAX
            )
            if elsewhere
            else []
        )

        # 整段聊天记录的字数预算：超了先丢"背景"（已回过的最老几条 → 别处的最老几条），
        # 还没回过的那批是这一轮真正要读的，最后才动。
        dropped_answered = 0
        while len("\n".join([*answered_lines, *history, *lines, *other_lines])) > budget and len(answered_lines) > 1:
            answered_lines.pop(0)
            dropped_answered += 1
        dropped_other = 0
        while len("\n".join([*answered_lines, *history, *lines, *other_lines])) > budget and len(other_lines) > 1:
            other_lines.pop(0)
            dropped_other += 1
        dropped_fresh = 0
        while len("\n".join([*answered_lines, *history, *lines, *other_lines])) > budget and len(lines) > 1:
            # 连"还没回过"的都放不下了（一条超长刷屏 + 一堆消息）：从最早的开始丢
            lines.pop(0)
            dropped_fresh += 1

        if answered_lines:
            tail = f"\n（更早的 {dropped_answered} 条已省略）" if dropped_answered else ""
            blocks.append(
                "# 这里刚聊过的（**这批你已经回过话了**：当背景看——不要复述、"
                "不要再回应一遍、也不要当成对方在等你答）\n"
                "按时间顺序，最后一条最新；带「你:」的是你自己说的。\n"
                + "\n".join(answered_lines)
                + tail
            )

        if history:
            blocks.append(
                "# 更早的聊天（已经压成概览，只当背景）\n" + "\n".join(history)
            )

        if lines:
            # 她自己不在这些消息里出现时，明确告诉她"这几条不是对你说的"：
            # 中文字面的「你」经常指群里另一个人，模型很容易当成有人在跟自己说话。
            names = [str(item).strip() for item in (mine_names or []) if str(item).strip()]
            mentioned = any(
                _mentions_me(str(item.get("text") or ""), names) for item in fresh
            )
            spoke = any(item.get("is_self") for item in fresh)
            if mentioned:
                address_line = "这几条里有人 @ 你 / 叫了你的名字：其中有话是对你说的。\n"
            elif spoke:
                address_line = (
                    "这几条里你刚说过话：接着你往下说的多半是对你说的；"
                    "但他们俩之间互相说的那部分别揽到自己身上，\n"
                    "不确定是在问谁时，别用「主人」这类专属称呼。\n"
                )
            else:
                address_line = (
                    "这几条里没有人 @ 你、也没有叫你的名字：他们是在互相说话。\n"
                    "别人句子里的「你」指的是群里另一个人，**不是指你**——"
                    "没点名找你时你只是在旁边看着，\n"
                    "想接话就自然接一句，但别写成「他们在跟你说话」。\n"
                )
            blocks.append(
                "# 这里最近在聊什么（按时间顺序，最后一条最新；带「你:」的是你自己说的）\n"
                + address_line
                + "这一串是原样的聊天记录：谁在跟谁说话、话题怎么接的，都看这里。\n"
                + "不要逐条复述、不要总结成列表。\n"
                + "\n".join(lines)
                + (
                    f"\n（更早的 {dropped_fresh} 条没列出来——太长放不下；"
                    "要接就接下面这几条）"
                    if dropped_fresh
                    else ""
                )
            )

        # 同一个她在别的地方听到的话：只当背景，不在这里回应
        if other_lines:
            tail = f"\n（更早的 {dropped_other} 条已省略）" if dropped_other else ""
            blocks.append(
                "# 你在别处同时听到的（同一件事的另一面，只是背景）\n"
                "这些是你在**另一个群 / 私聊**里说的话或听到的话，属于同一个你，"
                "但不用在这一轮回应（要接就往那边说）；"
                "**同一句话在两处都出现时，那是同一个人在另一处说过的话，"
                "不是他在这里又说了一遍**：\n"
                + "\n".join(other_lines)
                + tail
            )

        # 她最近说过的话：标上"在哪儿说的"，同一个地方别重复同样的句式
        mine: list[tuple[str, str]] = []
        for entry in recent_replies or []:
            if isinstance(entry, dict):
                text = " ".join(str(entry.get("text") or "").split())
                place = str(entry.get("session") or "")
            else:
                text = " ".join(str(entry or "").split())
                place = ""
            if text:
                mine.append((text, place))
        mine = mine[-3:]
        if mine:
            rendered: list[str] = []
            offsite = False
            for text, place in mine:
                line = _one_line(text, MINE_LINE_CHARS)
                if place and current and place != current:
                    where = label_map.get(place, place)
                    rendered.append(f"- 〔{where}〕{line}")
                    offsite = True
                else:
                    rendered.append(f"- {line}")
            header = "# 你最近说过的话（同一个地方别再重复这些句式和开头）"
            if offsite:
                header += "\n标着〔…〕的是你在**别的群 / 私聊**里说的；同一个地方别重复同样的句式。"
            blocks.append(header + "\n" + "\n".join(rendered))
        if marks_used:
            blocks.append(
                "# 这次一起发给你的图\n"
                "上面标着「（见图N）」的图已经按编号顺序附在这次请求的最前面"
                "（图1 是其中最早的那张，后面的图接在它后面）；其余的图只有文字描述。"
                "看图说话时按编号对，别把两张图认成一张。"
            )
        return blocks

    def bot_name_list(self, state: Any) -> list[str]:
        """她可能被叫到的名字：配置里的 bot 名字 + 群名片（原名/当前名）。

        用来判断"这几条群聊里有没有点名找她"。
        """

        names = [
            str(getattr(self.world, "bot_name", "") or "").strip(),
            str(getattr(state, "bot_current_nickname", "") or "").strip(),
            str(getattr(state, "bot_base_nickname", "") or "").strip(),
        ]
        # 昵称里常带表情/后缀（「凶猛蓝色虎鲸💢」），去掉非中文数字字母后再比一次
        extra: list[str] = []
        for name in names:
            cleaned = "".join(ch for ch in name if ch.isalnum())
            if cleaned and cleaned != name and len(cleaned) >= 2:
                extra.append(cleaned)
        result: list[str] = []
        for name in [*names, *extra]:
            if name and name not in result:
                result.append(name)
        return result

    def _chat_time(self, at: float, now: datetime | None) -> str:
        """群聊记录的时间戳：今天只写几点几分，不是今天的带上日期。

        只写 HH:MM 的话，凌晨聊的那几条在下午看起来也像"刚刚"——
        她分不清"现在是凌晨还是上午"，多半就是被这些时间戳带偏的。
        """

        if not at:
            return "--:--"
        try:
            moment = datetime.fromtimestamp(float(at), tz=now.tzinfo if now else None)
        except Exception:
            return "--:--"
        if now is not None:
            if moment.date() == now.date():
                return moment.strftime("%H:%M")
            if (now.date() - moment.date()).days == 1:
                return moment.strftime("昨天 %H:%M")
            return moment.strftime("%m-%d %H:%M")
        return moment.strftime("%H:%M")

    # ---------------- 第 3 层 ----------------

    def format_layer(
        self,
        *,
        max_messages: int = 3,
        reasoning: bool = True,
        mode: str = "actions",
        multi_session: bool = False,
    ) -> str:
        """输出格式约束。

        整段不依赖当前地点与状态，所以可以放在提示词前部当固定前缀用。
        能写哪些 type 由「当前场景」那一层给，这里只讲结构和字段规则。
        """

        reasoning_block = ""
        # 计划模式只解析 plan，没有 reasoning/memory，就别在提示里写这两段
        if reasoning and mode != "plan":
            reasoning_block = (
                "你的输出必须是 JSON：**第一个字符就是 `{`**，**reasoning 是第一个键、写在 actions 前面**，"
                "前面不要有任何解释、寒暄、Markdown 或代码块标记。\n\n"
                "{\n"
                '  "reasoning": {\n'
                '    "env": "你现在在哪、周围什么样（15 字以内）",\n'
                '    "state": "你此刻的状态如何（15 字以内）",\n'
                '    "mood": "你此刻的心情（8 字以内）",\n'
                '    "who": "读完上面的对话，你在跟谁说话、他在要什么（20 字以内）",\n'
                '    "inner": "用第一人称写下你此刻心里冒出的一两句（30 字以内，心里话，不是台词）",\n'
                '    "intent": "你打算怎么回应（20 字以内）"\n'
                "  },\n"
                '  "memory": "这次对话值得记住的一句话（20 字以内，以你的视角）",\n'
                '  "chat_note": "刚才你们在聊什么（20 字以内，只在这条消息带群聊背景时才写）",\n'
                '  "open_topic": "他有一件还没说完、之后你能接着问的事（20 字以内，'
                '时间写成具体日期，例如「9 月 29 号要去体检」；别写明天/下周；没有就留空）",\n'
                '  "heart_knot": "你心里搁着的一件事（20 字以内；没有就留空）",\n'
                '  "grudge": "他在你这儿记下的一笔账：他做了什么让你现在还气着'
                '（20 字以内；没有就留空）",\n'
                '  "forgive": false,\n'
                '  "own_topic": "你自己打算做、或者答应过别人的一件事（20 字以内，'
                '写清对谁；没有就留空）",\n'
                '  "own_topic_done": false,\n'
                '  "tone": "对方这一轮对你是什么口吻：praise | hug | attack | normal",\n'
                '  "valence_delta": 0,\n'
                + (
                    '  "affinity_delta": 0.1,\n'
                    if bool(getattr(self.world.profile, "enabled", True))
                    else ""
                )
                + '  "actions": [ ... ]\n'
                "}\n\n"
                "关于 reasoning：\n"
                "- 它是你动笔前的草稿，**永远不会发到群里**，也不计入动作数量；\n"
                "- 必须先写、写短、写具体（要引用你上面看到的环境/状态/对话），不要写空话；\n"
                "- inner 是纯粹的心理活动（第一人称、可以嘴硬、可以不讲道理），别写成旁白、别复述动作；\n"
                "- 严禁把 reasoning 的内容原样搬进 say。\n\n"
                "关于 memory：\n"
                "- 它是这次对话的**一句话总结**，会并进你在这个地方的记忆里；\n"
                "- 写「对方是谁、聊了什么、你怎么想」，例如「小明说他今天很累，我有点心疼」；\n"
                "- 纯寒暄、复读、没什么信息量的对话直接给空字符串；\n"
                "- 它不是回复，不会发到群里。\n\n"
                "关于 chat_note：\n"
                "- 它是给下一轮的**话题背景**：一句话说清「刚才这段在聊什么」；\n"
                "- 下一轮你会看到它，并且**已经回应过的内容不会再重复出现**，"
                "所以写清楚才不会重复回应老话题；\n"
                "- 没有群聊背景、或者纯寒暄时留空。\n\n"
                "关于 open_topic：\n"
                "- 它记的是**对方还没说完的事**（他说明天要去体检、他在纠结要不要换工作、"
                "他说等结果出来再说）——留着以后自然地接一句；\n"
                "- **时间要写成具体日期**（「9 月 29 号要去体检」），别写「明天」「下周」——"
                "隔两天再看就不知道是哪天了，你会去问一件早就过完的事；\n"
                "- 只写**别人**的事，不写你自己的动作或安排（那些有事件和日程管）；\n"
                "- 说完就有结果的小事不用记（「他问现在几点」这种）；\n"
                "- 没有就留空字符串，**大多数轮次都应该是空的**。\n\n"
                "关于 heart_knot：\n"
                "- 它记的是**你自己心里搁着的一件事**（他上次那句话让你到现在还别扭、"
                "你有点在意他刚才那个反应、你还在想白天那件事）——不推进、不用解决，"
                "先挂着，之后你说话时会自然带出来；\n"
                "- 它**只会在跟这个人说话的时候**想起来，所以直接用「他」写就行；\n"
                "- 只写**一遍就够**：已经挂着的那件事，不要每轮重复写；\n"
                "- 一句话、第一人称，别写成决心或计划；\n"
                "- 大多数轮次都该留空，**一次对话里最多冒出来一两件**。\n\n"
                "关于 grudge / forgive：\n"
                "- grudge 是**他在你这儿记的一笔账**：他答应的事又没做、放你鸽子、"
                "当众让你下不来台、骗了你——写清是哪件事，20 字以内；\n"
                "- 记了账你就会**对他冷一档**，而且只在跟他说话时提，所以**门槛要高**："
                "他只是说了句你不爱听的、或者你不确定他是不是故意的，就**不要记**；\n"
                "- 一天最多记一笔，同一件事不要重复写；\n"
                "- 他**道歉了 / 补上了 / 解释清楚了** → `forgive` 写 true，那笔账就划掉；\n"
                "- 大多数轮次这两个字段都该是空的 / false。\n\n"
                "关于 own_topic / own_topic_done：\n"
                "- own_topic 记的是**你自己**的事：你答应过别人的（答应给他看照片）、"
                "你自己想做的（想做顿饭）——写清对谁，20 字以内；\n"
                "- 只写**没做完**的；做完的那一刻用 `own_topic_done: true` 划掉它；\n"
                "- 别人的事用 open_topic，**别写在这儿**；\n"
                "- 同时最多留两件，没有就留空。\n\n"
                "关于 tone：\n"
                "- 它记的是**对方这一轮对你是什么口吻**，四选一：\n"
                "  praise＝夸你、认真接你的话；hug＝哄你、撒娇、亲昵的小动作；\n"
                "  attack＝怼你、阴阳怪气、拿你开玩笑；normal＝普通聊天。\n"
                "- 看**真实意思**，不要被字面骗：「你可真行啊」是 attack 不是 praise；\n"
                "  「大肥鱼」这种熟人式的逗你是 attack；单纯问事情就是 normal。\n"
                "- 拿不准就写 normal。\n\n"
                "关于 valence_delta：\n"
                "- 它是**这次互动把你的心情推了多少**，取 -1 ~ 1，**默认 0**——"
                "绝大多数轮次本来就该是 0，不写这一项也等于 0；\n"
                "- 刻度按「这件事在你这一天里算不算大事」来估：\n"
                "  0：没什么感觉（普通闲聊、日常打趣、被顺口夸一句）——**日常聊天基本都在这**；\n"
                "  ±0.2：接下来几个小时都还会想着它；\n"
                "  ±0.5：这一天的基调被它改写了；\n"
                "  ±1：能记一辈子的事（极罕见，日常对话里永远不该出现）。\n"
                "- 被认真接话、被夸、聊得开心，最多写到 +0.1 ~ +0.2：那是「听着舒服」，"
                "不是「心情被改写」；\n"
                "- 一直被哄、连着被夸、连着被逗时写 0——连着来的同一件事不稀奇了；\n"
                "- 被冷落、被怼、说了半天没人理才值得往负的方向写；\n"
                "- 它衡量的是**你的感受**，不是对方的语气；一句话很冲但你其实不在意，就写 0。\n\n"
                + (
                    "关于 affinity_delta：\n"
                    "- 它是**这一轮你对他的好感变没变**（跟上面那条「你的心情」是两回事），取 -1 ~ 1；\n"
                    "  被认真对待、被夸、被陪着聊、他记住了你说过的事 → +0.1 ~ +0.4；\n"
                    "  被冷落、被怼、被反复冒犯 → -0.1 ~ -0.4；\n"
                    "  普普通通的闲聊写 0 或者不写这一项——**这一项不是「聊得越多涨得越快」**；\n"
                    "- 只有这一轮真的让你更近/更远时才写；系统还会按每轮上限和每天额度再削一次。\n\n"
                    if bool(getattr(self.world.profile, "enabled", True))
                    else ""
                )
            )
        if mode == "plan":
            body = (
                "这一轮要输出的是计划，格式如下：\n\n"
                "{\n"
                '  "plan": [\n'
                '    { "action": "walk_to", "target_node": "window" },\n'
                '    { "action": "say", "messages": ["这风还挺大"], "send_to": "群 1001" },\n'
                '    { "action": "stare", "duration": 600 },\n'
                '    { "action": "think", "content": "内心活动" }\n'
                "  ],\n"
                '  "valid_until": 1800,\n'
                '  "reason": "为什么这样安排",\n'
                '  "send_to": "整串话默认说给谁；每一步也可以各自写"\n'
                "}\n\n"
                "字段说明：\n"
                "- plan：按先后顺序排的步骤，每步的 action 是动作 id，"
                "需要去哪就写 target_node，持续动作写 duration（秒）；"
                "标着「时长由你定」的动作要按它给的范围写 duration。\n"
                "- valid_until：这份计划大约管多少秒。\n"
                "- reason：一句话说明为什么这样安排。\n"
                "- send_to：这句话说给哪个会话听（见「你能说话的地方」）。"
                "**凡是别人看得见的动作（说话 / 分享 / 发图这类）都必须写**；"
                "只想自己待着（发呆 / 睡觉 / 想事情）就不用写。\n\n"
                "规则：\n"
                "1. action 只能用当前场景里列出的动作 id（写别的会被丢掉）。\n"
                "2. 想做只有别处能做的事，就把 walk_to 写成计划的第一步。\n"
                + (
                    "3. 你在几个地方都能说话，只是地方不同（见「你能说话的地方」）："
                    "想找谁就发给谁，不用只发给自己；回别人的话由系统按被问的地方处理。\n"
                    "4. send_to 可以写在每一步里：一句话要去两个地方（例如当众敷衍一句、"
                    "转头跟人单独吐槽一句），就写成两条 say，各自带 send_to，例如\n"
                    '   {"action":"say","messages":["行行行，你们说得都对"],"send_to":"群 1001"}、\n'
                    '   {"action":"say","messages":["烦死了，他们根本不懂"],"send_to":"私聊 2692047521"}。\n'
                    "5. **你在话里答应了「我去群里说他」这类承诺，就必须把对应动作排进 plan、"
                    "并写上 send_to**——只答应不动等于没做，也没人会替你转达。\n"
                    if multi_session
                    else "3. 想找谁就发给谁，别把不想公开的话说给所有人听。\n"
                )
                + "6. 不要输出解释、Markdown 或代码块，只输出 JSON。"
            )
            return reasoning_block + body
        return (
            reasoning_block
            + "actions 是你要执行的动作列表，格式如下：\n\n"
            "{\n"
            '  "actions": [\n'
            '    { "type": "say", "messages": ["消息1", "消息2"] },\n'
            '    { "type": "walk_to", "target_node": "window" },\n'
            '    { "type": "think", "content": "内心活动" },\n'
            '    { "type": "search_web", "intent": "查一下今天有什么科技新闻",\n'
            '      "queries": ["今日科技新闻", "AI 行业 最新进展"] }\n'
            "  ],\n"
            '  "cancel": "只有对方明确要求你停下 / 别做了 / 改主意时才写，写了就会立刻生效：'
            'now = 立刻停手并放弃剩下的安排，queue = 手上这件做完但不要再按原计划走；'
            '其它情况这一项不要出现",\n'
            '  "plan_mode": "queue | interrupt | replace（不写就是 queue）"\n'
            + (
                '  ,"send_to": "只想单独跟某人说的一句时写这里；不写就在他现在说话的地方答"\n'
                if multi_session
                else ""
            )
            + "}\n\n"
            + "字段说明：\n"
            f"- say：只填 messages，是发到群聊的文本，最多 {max_messages} 条。\n"
            "  有人逗你、撒娇、开玩笑时**要接梗**：回敬一句、嘴上嫌弃手上配合、"
            "顺手做个小动作都行。\n"
            "  别像客服一样只把问题答完就完事——那样看着很呆，也不像你自己。\n"
            + (
                "  别人看得见的话都要写 send_to（说给哪个会话听，见「你能说话的地方」），"
                "不写就当你在他现在说话的地方答。\n"
                if multi_session
                else ""
            )
            + "- 针对某个人的动作：只填 target，值是群友 ID"
            + (
                "。target 只说明「对象是谁」，这句话说给哪个会话听仍然看 send_to——"
                "想让亲亲、戳一戳这类动作的话只说给别处的某个人听，就自己补一个 send_to"
                if multi_session
                else ""
            )
            + "。\n"
            "- walk_to：只填 target_node（地点 ID）或 target（群友 ID）。\n"
            "- think：只填 content，是内心活动，不会发到群里。\n"
            "- duration：标着「时长由你定」的动作必须写它，单位是秒"
            "（7200 = 2 小时）——想做多久就写多久，但要在那个动作标注的范围里。\n"
            "- 工具型动作：只填 intent，用一句自然语言说清你想做什么；"
            "不要自己编参数，系统会按工具定义自动补全。\n\n"
            "- 标着「联网检索型」的动作：intent 写清楚查什么就够了；"
            "要分几个角度查时可以再加一条 queries，每条一个关键词句，最多 3 条，"
            "系统会逐条查完再汇总。\n"
            "  同一个动作还可以写 search_depth（quick 只查摘要 / standard 读正文 / deep 多读几篇）"
            "和 read_pages（最多读几篇）——简单问题用 quick，要读长文才说得清的才用 deep；"
            "写法不能超过动作自己配的上限。查完就把结果整理好交给你，"
            "**同一轮里不要再重复查同一件事**：没查到的就直说没查到。\n\n"
            "规则：\n"
            "1. type 只能用「当前场景」那一层列出的动作——**包括那里标着「别处才能做的动作」的那批**\n"
            "   （先写一步 walk_to 去那个地点，紧接着写它，系统会带你过去再做）；写别的会被丢弃。\n"
            "   actions 是数组，每个元素是一个动作对象；不要把要说的话直接写成 actions 的字符串，\n"
            "   也不要把单个动作写成对象（想说一句就用 {\"type\":\"say\",\"messages\":[\"…\"]}）。\n"
            "2. 工具型动作必须填 intent（想做什么），不要填 params。\n"
            "3. say 的 messages 是直接发到群聊的最终文本，简短自然，不要写「（动作）」以外的解释。\n"
            "4. think 的 content 不会发送到群聊。\n"
            "5. 针对空间或自己的动作静默执行，不发消息。\n"
            "6. 想做当前地点做不了的事，**必须写两步**：先 walk_to，紧接着把要做的那件事也写进同一串\n"
            '   actions，例如 [{"type":"walk_to","target_node":"study"},\n'
            '   {"type":"search_web","intent":"查今天的新闻"}]。只写移动不算安排——系统会带你过去，\n'
            "   但不会替你决定到了之后做什么，那会多花一次调用。\n"
            "7. 你在 say 里承诺了要做什么，就必须把对应的动作也写进 actions——只说不动等于没做。\n"
            + (
                "7.1 你在 say 里答应了「我去群里说他 / 我私聊他说」这类事，"
                "就必须把那句话排进 actions 并写上 send_to——只答应不动等于没做。\n"
                if multi_session
                else ""
            )
            + "8. 标着「只有在这个地点才能做」的动作是这里的特色：人在的时候就顺手挑一件，"
            "但不要每次都做同一件，也别重复最近刚做过的。\n"
            "9. 这次行动如果指向某个具体的人（踢谁、私聊谁、给谁画画像），"
            "**必须把对方的 id 一起写进 intent**（例如「把 123456 这个刷屏的踢掉」），"
            "能填 target 的就顺手填上；只写昵称系统不一定认得。\n"
            "10. 手头正在做的事不用等：你随时可以安排接下来的动作，插进来的安排会排队，"
            "等手上这件事做完自动接着做。想改这个先后顺序就写 plan_mode：\n"
            "   queue（默认）= 排在手上这件事和已经排好的步骤后面；\n"
            "   interrupt = 这件事更要紧，手上可打断的动作立刻停下、没做的步骤也一并放弃，先做新的；\n"
            "   replace = 手上这件做完，但不按原来排好的步骤继续了（等于把没做的换掉）。\n"
            "   对方明确说「别做了 / 不用了 / 停」时照样写 cancel。\n"
            "11. 清单里没有的动作就是**现在做不了**（不在这个地方 / 今天的次数用完了 / 没配好工具）："
            "换个地方或者换一件事，或者直接说你做不了——别在话里答应做不到的事。\n"
            "12. 不要输出解释、Markdown 或代码块，只输出 JSON。"
        )

    @staticmethod
    def reminder_layer(mode: str = "actions", *, multi_session: bool = False) -> str:
        """最末尾的几句提醒。

        格式细则已经提到前部（为了前缀缓存），这里只留三行「近因锚点」，
        让模型在动笔前再看一眼最容易犯的错。
        """

        lines = [
            "# 最后确认",
            "1. 只输出一个 JSON 对象，不要解释、不要 Markdown、不要代码块；",
        ]
        if mode == "plan":
            lines.append("2. plan 里的 action 只能用当前场景列出的动作 id；")
            lines.append("3. 计划里要说话时，也别重复你最近说过的句子和句式；")
            lines.append(PromptBuilder._place_reminder(multi_session))
        else:
            lines.append("2. 要写 reasoning 就先写 reasoning，再写 actions；")
            lines.append("3. 不要重复你最近说过的话，也不要再用同样的开头和句式；")
            lines.append(PromptBuilder._place_reminder(multi_session))
        if multi_session:
            lines.append(
                "5. 你在话里答应了「我去群里说一声 / 我私聊他」这类事，"
                "就必须把那条 say 一起写进 actions 并写上 send_to——只答应不动等于没做。"
            )
        return "\n".join(lines)

    @staticmethod
    def _place_reminder(multi_session: bool) -> str:
        """「能去哪儿说话」这一条：多会话和单会话写法不一样。

        以前的写法是「只能在你现在说话的地方说话」，在多会话下会直接把 send_to
        这条能力否掉——她在私聊里答应了"我去群里说"，然后就真的没去。
        """

        if multi_session:
            return (
                "4. 「你能说话的地方」那份名单里的地方你都能去（换地方说就写 send_to）；"
                "名单外的（别人让你私聊一个不在名单里的人、加好友、去别处找你）去不了就别答应，"
                "用自己的口气说明白。"
            )
        return (
            "4. 只能在你现在说话的地方说话：有人让你私聊他 / 加好友 / 去别处找你，"
            "你去不了就别答应，用自己的口气说明白。"
        )

    # ---------------- 组装 ----------------

    # 事实按类型分行：信息给全，但别写成一坨
    FACT_KIND_ORDER = ("基本信息", "关系", "约定", "喜好", "厌恶", "习惯", "近况", "other")
    FACT_KIND_LABELS = {
        "基本信息": "关于他",
        "关系": "他身边的人",
        "约定": "你们的约定",
        "喜好": "他喜欢",
        "厌恶": "他不喜欢",
        "习惯": "他的习惯",
        "近况": "他最近",
        "other": "其他",
    }

    def profile_block(
        self,
        person: Any,
        others: list[dict[str, Any]] | None = None,
        *,
        limit_others: int = 5,
        date_text: str = "",
    ) -> str:
        """「你在跟谁说话」：当前说话人的画像全文 + 其他人的缩略版一行。

        ``person`` 是 :class:`core.profile.PersonView`（没有画像时传 ``None``）；
        ``others`` 是 ``[{name, label, digest}]``。信息给全，但**只给会参与这一轮的人**：
        当前说话人全文，聊天记录里其他人各一行（默认最多 5 个）。
        """

        blocks: list[str] = []
        if person is not None:
            lines: list[str] = []
            label = str(_field(person, "name") or _field(person, "user_id") or "")
            header = f"- {label}（QQ {_field(person, 'user_id', '')}）"
            bonds = [str(item) for item in (_field(person, "affinities") or []) if str(item)]
            if bonds:
                header += "——" + "、".join(bonds)
            current = next(
                (
                    item
                    for item in (_field(person, "bonds") or [])
                    if isinstance(item, dict) and str(item.get("status")) == "current"
                ),
                None,
            )
            if current is not None and str(current.get("since") or ""):
                header += f"（{current.get('since')} 起）"
            lines.append(header)
            for item in (_field(person, "claims") or [])[:2]:
                since = str(item.get("since") or "")
                when = f"（{since}）" if since else ""
                lines.append(f"- 他自称是你的{item.get('type')}{when}，你还没认")
            for item in (_field(person, "past") or [])[:3]:
                span = " → ".join(
                    part
                    for part in (
                        str(item.get("since") or ""),
                        str(item.get("until") or ""),
                    )
                    if part
                )
                lines.append(f"- 曾经的{item.get('type')}" + (f"（{span}）" if span else ""))
            known = []
            days = int(_field(person, "days_known", 0) or 0)
            if days:
                known.append(f"认识 {days} 天")
            count = int(_field(person, "message_count", 0) or 0)
            if count:
                known.append(f"聊过 {count} 句")
            level = _field(person, "level")
            level_name = str(_field(level, "name", "") or "")
            affinity = _field(person, "affinity", 0.0)
            relation_line = "；".join(known)
            if level_name:
                relation_line += ("，" if relation_line else "") + (
                    f"你对他：{level_name}（{round(float(affinity))}/100）"
                )
            if relation_line:
                lines.append(f"- {relation_line}")
            # 时间锚点：她自己得知道"上次跟他说话是多久前"——不然"你昨天说的那个"
            # 永远是空话（想念那条线有这个数，但那只在她主动找你时才算）
            when = self._talked_ago_line(person)
            if when:
                lines.append(f"- {when}")
            call_me = str(_field(person, "call_me", "") or "")
            call_him = str(_field(person, "call_him", "") or "")
            if call_me or call_him:
                parts = []
                if call_me:
                    parts.append(f"他让你叫他「{call_me}」")
                if call_him:
                    parts.append(f"你叫他「{call_him}」")
                lines.append("- " + "；".join(parts))
            for text in self._fact_lines(person):
                lines.append(f"- {text}")
            level_prompt = str(_field(level, "prompt", "") or "")
            if level_prompt:
                lines.append(f"- {level_prompt}")
            # 这一档叫什么、再熟一点到哪儿：只讲"现在到哪儿为止"，
            # 不要把四级阶梯全铺给她（用不上的档位写进去只会分散注意力）。
            level_name = str(_field(level, "name", "") or "")
            next_name = str(_field(level, "next_name", "") or "")
            deny = [
                str(item) for item in (_field(level, "deny") or [])
                if str(item)
            ]
            if deny:
                where = f"（你们现在是「{level_name}」这一档）" if level_name else ""
                lines.append(
                    f"- 这一档还不能做：{'、'.join(deny)}{where}。**不做，也不提**。"
                    "他要是开口要，用你自己的语气拒掉或者把话题岔开（可以带情绪、可以嘴硬，"
                    "但别答应、也别做完再说）。"
                )
            if next_name:
                lines.append(f"- 再熟一点才到「{next_name}」那一档：比现在更亲的那些动作，先别碰。")
            # 关系本身是负面的时候统一压住：不然"讨厌的人"那档全靠各自的文案自己扛。
            if bool(_field(person, "negative", False)):
                lines.append(
                    "- 你对他没那个意思：**不主动找他、不撒娇、不做任何亲昵动作**；"
                    "他套近乎就冷淡回掉，别给他台阶。"
                )
            digest = str(_field(person, "digest", "") or "")
            if digest and not lines:
                lines.append(f"- 你记得他：{digest}")
            lines.append(
                "- 这些是你早就知道的事：不用问、不用复述，合适的时候自然带出来。"
            )
            blocks.append("# 你在跟谁说话\n" + "\n".join(lines))
        if others:
            rows: list[str] = []
            for item in list(others)[: max(0, int(limit_others))]:
                name = str(item.get("name") or item.get("user_id") or "有人")
                label = str(item.get("label") or "")
                digest = str(item.get("digest") or "").strip()
                body = f"- {name}"
                if label:
                    body += f"（{label}）"
                if digest:
                    body += f"：{digest}"
                rows.append(body)
            if rows:
                blocks.append(
                    "# 群里还有谁（只当背景；他们的话不一定是说给你的）\n" + "\n".join(rows)
                )
        return "\n\n".join(blocks)

    def _talked_ago_line(self, person: Any, now: datetime | None = None) -> str:
        """「上次跟他说话是什么时候」这一行（说不出来就返回空串）。"""

        try:
            talked = float(_field(person, "last_talked_at", 0.0) or 0.0)
            seen = float(_field(person, "last_seen_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            return ""
        moment = now or self._now()
        if moment is None or talked <= 0:
            # 没有当前时间就没法换算成"多久前"：宁可不说，也不写一个绝对时间戳
            if seen > 0 and talked <= 0:
                return "你们还没正经聊过"
            return ""
        stamp = moment.timestamp()
        minutes = max(0.0, (stamp - talked) / 60.0)
        text = f"他上次跟你说话是 {_dwell_text(minutes)}前"
        # 他在群里露过面、但没找她：这条很容易被"群里天天刷屏"糊弄过去
        if seen > talked + 60:
            text += f"；他 {_dwell_text(max(0.0, (stamp - seen) / 60.0))}前在群里露过面"
        return text

    def _fact_lines(self, person: Any) -> list[str]:
        """把事实按类型合并成几行（信息给全，但别写成一坨）。"""

        grouped: dict[str, list[str]] = {}
        for item in _field(person, "facts") or []:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            kind = str(item.get("kind") or "other")
            grouped.setdefault(kind, []).append(text)
        lines: list[str] = []
        for kind in self.FACT_KIND_ORDER:
            texts = grouped.pop(kind, [])
            if not texts:
                continue
            lines.append(f"{self.FACT_KIND_LABELS.get(kind, kind)}：" + "、".join(texts[:6]))
        for kind, texts in grouped.items():
            lines.append(f"{kind}：" + "、".join(texts[:6]))
        return lines

    def build_injection(
        self,
        state: WorldState,
        *,
        node: NodeDef | None,
        memories: list[RecalledMemory] | None = None,
        engagement_hint: str = "",
        extra_notes: list[str] | None = None,
        focus_user: str = "",
        recent_chat: list[dict[str, Any]] | None = None,
        style_block: str = "",
        weather: str = "",
        recent_search: str = "",
        abilities_line: str = "",
        pending_event: str = "",
        event_journal: str = "",
        current_session: str = "",
        session_labels: dict[str, str] | None = None,
        profile_text: str = "",
        samples: list[dict[str, Any]] | None = None,
    ) -> str:
        """注入模式：给主人格的一层「世界认知」。"""

        scene = self.scene_compact(node)
        runtime = self.runtime_layer(
            state,
            node=node,
            memories=memories,
            engagement_hint=engagement_hint,
            extra_notes=extra_notes,
            focus_user=focus_user,
            recent_chat=recent_chat,
            style_block=style_block,
            weather=weather,
            recent_search=recent_search,
            abilities_line=abilities_line,
            pending_event=pending_event,
            event_journal=event_journal,
            current_session=current_session,
            session_labels=session_labels,
            profile_text=profile_text,
        )
        sample_text = self.samples_layer(
            samples, pronoun=pronoun_for(getattr(self.world, "gender", "female"))
        )
        head = (
            "\n\n# ===== 虚拟世界状态（这是你此刻真实的处境）=====\n"
            "你有自己的空间和生活节奏，下面是你现在的处境。"
            "自然地把它融进你的反应里（比如刚睡醒、正在忙、心情如何），"
            "但不要向用户解释这套设定、不要复述状态数值。\n\n"
        )
        if sample_text:
            head += f"{sample_text}\n\n"
        return head + f"{scene}\n\n{runtime}\n# ===== 虚拟世界状态结束 ====="

    def scene_compact(self, node: NodeDef | None) -> str:
        if node is None:
            return "你当前不在任何已知地点。"
        actions = [a.id for a in self.world.actions_in(node.id) if a.id != "walk_to"]
        return (
            f"你在【{node.name or node.id}】（id：{node.id}）：{node.prompt}\n"
            f"氛围：{node.atmosphere.describe()}\n"
            f"这里能做：{'、'.join(actions) if actions else '发呆'}\n"
            f"{self.reach_table(node.id, with_actions=False)}"
        )

    def build_autonomous_system_prompt(
        self,
        *,
        persona_text: str,
        state: WorldState,
        node: NodeDef | None,
        available_tools: dict[str, str],
        memories: list[RecalledMemory] | None = None,
        engagement_hint: str = "",
        extra_notes: list[str] | None = None,
        max_messages: int = 3,
        recent_chat: list[dict[str, Any]] | None = None,
        other_context: str = "",
        reasoning: bool = True,
        mode: str = "actions",
        style_block: str = "",
        weather: str = "",
        recent_search: str = "",
        abilities_line: str = "",
        pending_event: str = "",
        event_journal: str = "",
        hidden_actions: set[str] | None = None,
        session_directory: str = "",
        current_session: str = "",
        session_labels: dict[str, str] | None = None,
        chat_images: dict[str, str] | None = None,
        profile_text: str = "",
        samples: list[dict[str, Any]] | None = None,
    ) -> str:
        """接管模式：完整五层，含 JSON 输出约束。

        ``mode`` 决定第 3 层讲的是 actions 还是 plan；两种模式共用前两层，
        所以计划决策和回复决策能吃到同一段前缀缓存。

        ``chat_images``：这一轮附给主模型的聊天记录图片（``地址 -> 图N``）。
        """

        layers = []
        persona = self.persona_layer(persona_text)
        if persona:
            layers.append(f"# ========== 第 1 层：你是谁 ==========\n{persona}")
        # 声音样例接在第 1 层末尾：它是"她怎么说话"，和角色卡是同一层的事
        sample_text = self.samples_layer(
            samples, pronoun=pronoun_for(getattr(self.world, "gender", "female"))
        )
        if sample_text:
            layers.append(sample_text)
        layers.append(f"# ========== 第 2 层：世界规则 ==========\n{self.world_layer()}")
        layers.append(
            "# ========== 第 3 层：输出格式 ==========\n"
            + self.format_layer(
                max_messages=max_messages,
                reasoning=reasoning,
                mode=mode,
                multi_session=bool(session_directory),
            )
        )
        layers.append(
            "# ========== 第 4 层：当前场景 ==========\n"
            + self.scene_layer(node, available_tools, hidden_actions)
        )
        if session_directory:
            # 她能在哪几个地方说话（会话组才有多个）：挑落点、单独跟人说一句都用它
            layers.append("# ========== 你能说话的地方 ==========\n" + session_directory)
        layers.append(
            "# ========== 第 5 层：运行时状态 ==========\n"
            + self.runtime_layer(
                state,
                node=node,
                memories=memories,
                engagement_hint=engagement_hint,
                extra_notes=extra_notes,
                recent_chat=recent_chat,
                other_context=other_context,
                style_block=style_block,
                weather=weather,
                recent_search=recent_search,
                abilities_line=abilities_line,
                pending_event=pending_event,
                event_journal=event_journal,
                current_session=current_session,
                session_labels=session_labels,
                chat_images=chat_images,
                profile_text=profile_text,
            )
        )
        layers.append(
            self.reminder_layer(mode, multi_session=bool(session_directory))
        )
        return "\n\n".join(layers)

    def build_reply_user_prompt(
        self,
        *,
        user_name: str,
        text: str,
        is_private: bool = False,
        addressing: str = "direct",
        extra_texts: list[str] | None = None,
        from_session: str = "",
    ) -> str:
        """被 @（或其他插件放行）时的用户消息包装。

        ``addressing``：
        - ``direct``：这句话是对她说的（@ 了她、叫了她的名字、或者接着她刚才的话）；
        - ``interject``：群里在聊，只是轮到她插一句——不能当成有人点名找她，
          更不能把别人之间的话当成对她的请求。

        ``extra_texts``：她还在调模型时同一人紧接着又发的几句（合并成一次回复用）。

        ``from_session``：这条消息来自哪个会话（「群 123456「群名」」这种）。
        多会话时一定要给，她才知道这句是在哪儿说的。
        """

        who = user_name or "群友"
        where = f"（这条来自 {from_session}）" if from_session else ""
        if addressing == "interject":
            parts = [
                f"群里正在聊，这句话是说给大家的（{who}）{where}：",
                text,
                "",
                "没有人点名找你。你可以顺着接一句，也可以觉得没必要说就只做自己的事；"
                "不要把这句话当成别人对你的请求或指令；"
                "别人话里的「你」指的是群里另一个人，不是指你——"
                "不确定是谁在跟谁说话时，别用「主人」这类专属称呼，也别写成在回应他。",
            ]
        elif addressing == "soft":
            parts = [
                f"{who} 在群里顺着话题跟你说（没有 @ 你）{where}：",
                text,
                "",
                "对方没有点名 @ 你，是接着眼下的话题对你说的；"
                "照常回应就好，但别写成「你 @ 了我」；"
                "句子里的「你」如果明显指别人，就别揽到自己身上。",
            ]
        else:
            parts = [f"{who} 对你说{where}：", text]
        for extra in list(extra_texts or []):
            body = " ".join(str(extra or "").split())
            if body:
                parts.append(f"（紧接着又说：{body}）")
        if extra_texts:
            parts.append("他这几句是一口气说完的，一起回应，别分两次答。")
        now = self._now()
        if now is not None:
            # 聊天记录里带的是各自发言的时间戳，跟"现在几点"很容易混：
            # 这里明确说一遍现在的时间，她就不会拿凌晨那几条当"刚刚"。
            parts.append(
                f"（现在是 {now.strftime('%H:%M')} · {period_of(now.hour)}；"
                "上面带时间的是那些话各自发出的时间，不代表现在。）"
            )
        if is_private:
            parts.append("（这是私聊）")
        parts.append("")
        parts.append("按「输出格式」那一层回复：先写 reasoning，再写 actions。只输出 JSON。")
        return "\n".join(parts)

    def build_autonomous_user_prompt(self, reason: str, context: str = "") -> str:
        parts = ["现在没有人在和你说话，你按自己的节奏决定接下来做什么。"]
        if reason:
            parts.append(f"触发原因：{reason}")
        if context:
            parts.append(context)
        parts.append("只输出 JSON。")
        return "\n".join(parts)

    AUTONOMOUS_MANNERS = (
        "# 说话时的分寸（自己开口时都按这个来）\n"
        "- 这几句是**你自己想说**的，不是被谁问到的：别写成在向全场吆喝、招呼、招揽；\n"
        "- 不要推销、不要讨好（例如「想吃吗」「吱一声」「要的扣 1」「我给你留了一份」）；\n"
        "- 不要和最近说过的话重复，换一种说法和句式；\n"
        "- 真没什么想说的就**保持安静**——不开口比硬找一句更像真人。\n"
    )
    """自己开口（不是被搭话）时的通用分寸：自主发言和插话共用同一份。"""

    def build_reply_followup_prompt(
        self,
        hint: str,
        tool_result: str,
        *,
        no_search: bool = False,
        event_digest: str = "",
    ) -> str:
        """续说提示词。``no_search=True`` 时会明确禁止这一轮再查（刚查完就别反复查）。

        ``event_digest`` 是这段时间她自己遇上的事（一行一条）：顺手带出来才像真的经历了。
        """

        no_search_rule = (
            "- 东西已经查完了：这一轮**不要再调检索/搜索类动作**，用手上的结果说话；"
            "确实没查到就直说没查到，不要换个词再查一遍。\n"
            if no_search
            else ""
        )
        event_block = (
            "这段时间你自己还遇上了这些事（想说就顺口带一句，不想提就算了，"
            "不要当成任务逐条汇报）：\n" + event_digest + "\n\n"
            if event_digest
            else ""
        )
        return (
            "你刚才做了一件事，这是结果：\n"
            f"{tool_result}\n\n"
            f"{event_block}"
            f"{hint or '用你自己的话简短地说说这件事。'}\n\n"
            "说话时的分寸：\n"
            f"{no_search_rule}"
            "- 如果刚才没有人在跟你说话，就当成随口一句自言自语，别写成在招呼全场；\n"
            "- 不要吆喝、不要招揽、不要推销（例如「想吃吗」「吱一声」「要的扣 1」），"
            "也不要用「我给你留了一份」这类讨好句式；\n"
            "- 不要和最近说过的话重复，换一种说法和句式；\n"
            "- 只有确实想分享时才用 say；不想说就只输出 think 或空动作列表。\n"
            "只输出 JSON。"
        )

    def build_action_generator_prompt(
        self,
        *,
        persona_text: str,
        zone_name: str,
        zone_note: str,
        nodes: list[dict[str, Any]],
        existing_actions: list[str],
        tools: list[str],
        per_node: int,
    ) -> tuple[str, str]:
        """批量生成动作的提示词（返回 system, user）。

        生成的动作是"她在某个地点能做的事"，所以必须把**区域里每个地点**都交代清楚，
        并要求每件动作绑到具体地点上。
        """

        system = (
            "你在帮一个群聊里的 AI 角色设计「她在某个地点能做的事」。"
            "输出必须是 JSON，不要解释、不要 Markdown。"
        )
        node_lines = [
            f"- id={item.get('id')}｜{item.get('name')}｜{_one_line(item.get('prompt'), 60)}"
            for item in nodes
        ]
        know = "、".join(existing_actions[:40]) if existing_actions else "（还没有动作）"
        tool_line = (
            "可用的工具（工具型动作只能从这里面选，写不出合适的就别写工具动作）："
            + "、".join(tools[:30])
            if tools
            else "（当前没有注册任何工具，不要生成工具型动作）"
        )
        prompt = (
            f"# 角色\n{_one_line(persona_text, 600) or '（没有额外设定，按活泼自然的群友来写）'}\n\n"
            f"# 区域\n{zone_name}：{_one_line(zone_note, 80)}\n\n"
            f"# 区域里的地点（每个地点都要照顾到）\n" + "\n".join(node_lines) + "\n\n"
            f"# 已经有的动作（不要重复这些名字和意图）\n{know}\n\n"
            f"# {tool_line}\n\n"
            f"# 要求\n"
            f"1. 每个地点写 {per_node} 个动作，一共 {per_node * max(1, len(nodes))} 个左右；\n"
            "2. 动作要「在她所在的地方真的做得出来」，做完以后有值得在群里讲一句的见闻"
            "（例如在阳台：晒被子、给花浇水、看楼下的人）；\n"
            "3. 不要写「说话」「分享」这类通用动作，也不要写需要联网或工具才能完成的事（除非用了上面列出的工具）；\n"
            "4. 效果幅度要小：属性只允许 energy / loneliness / curiosity / affect / boredom，"
            "写成 +0.05、-0.05、=0.5 这类；心情写成 mood:满足；\n"
            "5. 持续动作给 duration_mode=llm 和 duration_min / duration_max（秒）。\n\n"
            "# 输出格式（只输出这个 JSON）\n"
            "{\n"
            '  "actions": [\n'
            "    {\n"
            '      "id": "英文小写下划线，例如 balcony_water_flowers",\n'
            '      "node_id": "这个动作属于哪个地点（必须用上面给的 id）",\n'
            '      "name": "中文名，例如 给花浇水",\n'
            '      "description": "一句话说明她会做什么",\n'
            '      "category": "instant 或 continuous",\n'
            '      "llm_level": "single（让她自己说一句）/ template（固定文案）/ tool（要用工具）",\n'
            '      "tool_names": ["只有当 llm_level=tool 才填，从上面对应的工具清单里挑，可以选多个，按顺序调用"],\n'
            '      "visible": true,\n'
            '      "duration_mode": "llm",\n'
            '      "duration_min": 300,\n'
            '      "duration_max": 1800,\n'
            '      "template": "只有 template 才填，例如 （{bot}给花浇了点水）",\n'
            '      "on_complete": {\n'
            '        "trigger": "llm_followup",\n'
            '        "prompt_hint": "让她用第一人称随口讲这件事，不要说教、不要吆喝",\n'
            '        "effects": {"boredom": "-0.05", "affect": "+0.06"}\n'
            "      }\n"
            "    }\n"
            "  ]\n"
            "}"
        )
        return system, prompt

    def build_node_generator_prompt(
        self,
        *,
        persona_text: str,
        zone_name: str,
        zone_note: str,
        existing_nodes: list[str],
        count: int,
    ) -> tuple[str, str]:
        """批量生成地点的提示词（返回 system, user）。"""

        system = "你在帮一个群聊里的 AI 角色设计她的活动空间。输出必须是 JSON，不要解释。"
        prompt = (
            f"# 角色\n{_one_line(persona_text, 400) or '（没有额外设定）'}\n\n"
            f"# 区域\n{zone_name}：{_one_line(zone_note, 80)}\n\n"
            f"# 这个区域已有的地点（不要重复）\n"
            + ("、".join(existing_nodes) if existing_nodes else "（还没有地点）")
            + "\n\n"
            f"# 要求\n"
            f"1. 写 {count} 个新地点，彼此有明显区别，适合日常小动作（发呆、看风景、做事）；\n"
            "2. 每个地点给一句「这里是什么样」的描述（会进提示词，影响她的行为）；\n"
            "3. atmosphere 是 0~1 的小数：calm（安静）/ intimacy（私密）/ visibility（显眼）/ "
            "liveliness（热闹）/ loneliness（孤单感）/ curiosity（新鲜感）；\n"
            "4. nickname_text 是她在群里名片上显示的状态词，写成「在+地点简称」，"
            "不超过 5 个字（例如「在阳台」「在天台」）；\n"
            "5. 不要给坐标（位置由编辑器自动安排）。\n\n"
            "# 输出格式（只输出这个 JSON）\n"
            "{\n"
            '  "nodes": [\n'
            "    {\n"
            '      "id": "英文小写下划线，例如 balcony",\n'
            '      "name": "中文名，例如 阳台",\n'
            '      "nickname_text": "在阳台",\n'
            '      "prompt": "一句话描述",\n'
            '      "icon": "可选，写个 emoji",\n'
            '      "color": "#7FB2E5",\n'
            '      "atmosphere": {"calm": 0.7, "intimacy": 0.4, "visibility": 0.3, '
            '"liveliness": 0.4, "loneliness": 0.3, "curiosity": 0.5}\n'
            "    }\n"
            "  ]\n"
            "}"
        )
        return system, prompt

    def build_memory_summary_prompt(
        self,
        *,
        node_name: str,
        lines: list[str],
        hints: list[str] | None = None,
        max_chars: int = 60,
    ) -> tuple[str, str]:
        """把一段对话压成一条记忆（返回 system, user）。"""

        system = (
            "你在帮一个角色整理记忆。把下面这段记录压成一句她会记住的话，"
            f"不超过 {max_chars} 字。要求：她自己一律用「我」；"
            "有别人说话时写清对方是谁、聊了什么、她当时怎么想；"
            "如果整段只有她自己（自言自语、一个人做事），就写她做了什么、当时怎么想，"
            "不要提「对方」、也不要问对方是谁。"
            "只输出这一句话本身：不要逐条复述、不要客套、不要括号、"
            "不要写「他/她说了」这种流水账、不要向任何人提问或索要信息。"
        )
        others: list[str] = []
        for line in lines:
            name = str(line).split(":", 1)[0].strip()
            if name and name not in ("我", "有人") and name not in others:
                others.append(name)
        parts = [f"她当时在：{node_name or '某个地方'}"]
        if others:
            parts.append("这段里除了她还有：" + "、".join(others))
        else:
            parts.append("这段里没有别人说话，是她自己的事。")
        if hints:
            parts.append("她自己觉得值得记住的：" + "；".join(hints))
        parts.append("这段对话：")
        parts.extend(f"- {line}" for line in lines)
        return system, "\n".join(parts)

    def build_schedule_intent_prompt(
        self,
        *,
        schedule_id: str,
        when: str,
        where: str,
        state_hint: str,
        chat_note: str,
        persona: str = "",
        steps: list[dict[str, Any]],
    ) -> tuple[str, str]:
        """智能日程：只让模型给这几步写「这一步想干什么」。

        刻意只给最少的信息（人设、几点、在哪、什么状态、一句话题背景 + 要补的这几步），
        不给她平时的那套提示词：动作链本身是固定的，模型只需要按她的身份把意图说清楚。
        """

        system = (
            "你在给一条日程里的几个步骤写「这一步想干什么」。这些步骤会交给工具或指令去执行，"
            "你的这句话会被用来推断要传什么参数。只输出一个 JSON 对象，不要解释、不要 Markdown。"
        )
        persona_text = str(persona or "").strip()
        if persona_text:
            system += (
                "\n\n她的人设（照她的身份和口味选词，但这句话是给工具的明确交代，"
                "不要写成对群里说的话）：\n" + persona_text
            )
        lines = [f"现在是 {when}。" if when else ""]
        if where:
            lines.append(f"她这会儿在{where}。")
        if state_hint:
            lines.append(f"她的状态：{state_hint}。")
        if chat_note:
            lines.append(f"刚才群里在聊：{chat_note}（只是背景，别把日程写成回应它）。")
        step_lines = [
            "- 第 {index} 步：{label}（{kind}）——{description}".format(
                index=item.get("index"),
                label=item.get("label") or item.get("action_id") or "",
                kind=item.get("kind") or "工具型",
                description=item.get("description") or "（没有额外说明）",
            )
            for item in steps
        ]
        prompt = (
            "\n".join(line for line in lines if line)
            + "\n\n这一步一步要补的意图：\n"
            + "\n".join(step_lines)
            + "\n\n要求：\n"
            "1. 每个步骤一句话，说清这一步想让它干什么（例如「看看今天有什么科技新闻」）；\n"
            "2. 具体一点，别写参数名、别写工具名，也别编不存在的目标；\n"
            "3. 只写这几步，不要多写、不要改顺序、不要提别的动作；\n"
            '4. 输出格式：{"intents": [{"step": 1, "intent": "..."}]}'
        )
        return system, prompt

    def build_command_prompt(
        self,
        *,
        command: str,
        hint: str,
        intent: str,
    ) -> tuple[str, str]:
        """把「她想让那条指令帮她干什么」拼成一条真正的指令文本（返回 system, user）。"""

        system = (
            "你在把一句自然语言的意图拼成一条 AstrBot 指令。"
            "只输出这一条指令本身（以 / 开头，带好参数），不要解释、不要引号、不要 Markdown。"
        )
        prompt = (
            f"要触发的指令：{command}\n"
            f"这条指令的参数说明：{hint or '（没有额外说明，只填指令本身）'}\n\n"
            f"她的意图：{intent}\n\n"
            "要求：参数只能从意图里能确定的信息来写，别编；"
            "确实缺参数就只输出指令名本身。"
        )
        return system, prompt

    def build_schedule_action_prompt(
        self,
        *,
        op: str,
        intent: str,
        actions: list[str],
        current: str,
        now: Any = None,
        sessions: str = "",
    ) -> tuple[str, str]:
        """把「每天七点查新闻」翻译成日程参数（返回 system, user）。"""

        clock = ""
        if now is not None and hasattr(now, "strftime"):
            weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
            clock = f"现在是 {now.strftime('%Y-%m-%d')}（{weekdays[now.weekday()]}）{now.strftime('%H:%M')}"
        system = (
            "你在帮一个角色管理她自己的日程表。只输出一个 JSON 对象，"
            "不要解释、不要 Markdown。时间必须是 24 小时制的 HH:MM。"
        )
        if op == "add":
            body = (
                f"她想加一条日程：{intent}\n\n"
                f"{clock}\n\n"
                "可以用的动作（action_chain 里只能用这些 id）：\n"
                + "\n".join(actions)
                + "\n\n"
                "输出格式：\n"
                '{"time": "07:00", "days": ["mon","tue"],'
                ' "action_chain": [{"type": "walk_to", "target_node": "study"},'
                ' {"type": "search_web", "intent": "查今天的新闻"}],'
                ' "auto_travel": true, "once": false, "date": "",'
                ' "sessions": [], "note": "为什么排这件事"}\n\n'
                "说明：\n"
                "- days 不写就是每天；只挑一次就写对应星期；\n"
                "- 只做这一次（不是每天）时写 once=true，并给出 date（YYYY-MM-DD，"
                "用上面的「现在」推算；说「明天」就加上一天）；\n"
                "- note 用一句话写清这件事的前因后果，写给她自己看，"
                "例如「主人说下午可能下雨，让我三点收衣服」；\n"
                "- 动作链按先后顺序；只有别处能做的动作，前面加一步 walk_to；\n"
                "- 说话类动作可以带 content（说什么）。"
                + (
                    "\n- 她指定了只说给谁听时，把那个地方的名字写进 sessions"
                    "（写「你能说话的地方」里的名称、群号 / QQ 号或备注都行）；"
                    "没指定就留空数组，意思是每个会话各自跑。\n\n"
                    "她能说话的地方：\n" + sessions
                    if sessions
                    else ""
                )
            )
        else:
            body = (
                f"她想删掉一条日程：{intent}\n\n"
                "她现在的日程表：\n"
                f"{current}\n\n"
                "输出格式（三选一，挑最有把握的）：\n"
                '{"id": "日程 id"}\n'
                '{"time": "07:00"}\n'
                '{"keyword": "新闻"}'
            )
        return system, body

    def build_recall_query_prompt(
        self,
        *,
        intent: str,
        node_names: list[str],
        zone_names: list[str],
        memory_types: list[str],
    ) -> tuple[str, str]:
        """把「想回忆什么」翻译成检索条件（返回 system, user）。"""

        system = (
            "你在帮一个角色翻自己的记忆。根据她想回忆的内容，输出检索条件。"
            "只输出一个 JSON 对象，键是 keyword / node / zone / type / limit，"
            "没有把握的键就省略，不要编造地点名。"
        )
        prompt = (
            f"她想回忆：{intent}\n\n"
            "# 可以填的地点和区域（只能从这里面选，原样写名字）\n"
            f"- 地点：{'、'.join(node_names) or '（没有）'}\n"
            f"- 区域：{'、'.join(zone_names) or '（没有）'}\n\n"
            "# 记忆类型（可选）\n"
            f"{'、'.join(memory_types)}\n\n"
            "# 说明\n"
            "- keyword：想回忆的主题词，一两个词就够（例如「做饭」「生日」）；\n"
            "- node / zone：她想的是某个具体地点或整个区域时才填；\n"
            "- type：只想回忆某一类事情时才填；\n"
            "- limit：想要几条，默认 3，最多 5。\n\n"
            '例如 {"keyword": "做饭", "node": "厨房", "limit": 3}'
        )
        return system, prompt

    def build_tool_params_prompt(
        self,
        *,
        tool_name: str,
        tool_description: str,
        param_text: str,
        intent: str,
        recent_chat: list[dict[str, Any]] | None = None,
        previous_results: str = "",
        error_hint: str = "",
        extra_rules: str = "",
    ) -> tuple[str, str]:
        """把「她想干什么」翻译成工具参数时用的提示词（返回 system, user）。"""

        system = (
            "你是一个函数调用参数补全器。根据用户的意图和工具的参数定义，"
            "输出调用该工具所需的参数。只输出一个 JSON 对象，键是参数名、值是要传入的内容；"
            "不要解释、不要 Markdown、不要多余字段；无法判断的参数请省略，不要编造。"
        )
        if previous_results:
            system += (
                "这次还给了你「上一个工具返回的内容」：如果参数要用到里面的信息"
                "（比如要抓取的网址、要查询的编号），必须从里面取原样照抄，不许自己编。"
            )
        if error_hint:
            system += (
                "这次还给了你「上一次调用失败的原因」：说明上一次的参数不对或缺了，"
                "请针对这条报错把每一个参数都补齐（工具说明里写着「可选」的也可能是它真正需要的）。"
            )
        chat_lines = [
            f"- {item.get('name') or item.get('user_id')}: {_one_line(item.get('text'), 60)}"
            for item in list(recent_chat or [])[-6:]
        ]
        prompt = (
            f"工具名：{tool_name}\n"
            f"工具说明：{tool_description or '（无）'}\n"
            f"参数定义：\n{param_text}\n\n"
            f"她的意图：{intent}\n"
            + ("\n最近聊天（仅供参考）：\n" + "\n".join(chat_lines) if chat_lines else "")
            + (
                "\n\n上一个工具返回的内容（参数要用的信息从这里面找）：\n"
                + _one_line(previous_results, 1200)
                if previous_results
                else ""
            )
            + (
                "\n\n上一次调用失败的原因（照着补齐参数）：\n" + _one_line(error_hint, 300)
                if error_hint
                else ""
            )
            + (f"\n\n{extra_rules.strip()}" if extra_rules.strip() else "")
            + "\n\n请输出参数字典（JSON）。"
        )
        return system, prompt

    def build_tool_choice_prompt(
        self,
        *,
        tools: list[dict[str, str]],
        intent: str,
        recent_chat: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str]:
        """一个动作挂了多个工具、且用法是「智能选择」时用（返回 system, user）。

        选择与补参数合并成一次调用：模型同时给出用哪个工具、以及它的参数。
        """

        system = (
            "你是一个工具选择器。用户会给你几个候选工具的定义和一句意图，"
            "你要挑出**最合适的那一个**，并给出调用它需要的参数。"
            "只输出一个 JSON 对象，格式：{\"tool\": \"工具名\", \"params\": {...}}；"
            "不要解释、不要 Markdown；参数无法判断就省略，不要编造。"
            "候选里没有合适的也要挑一个最接近的，不要自己发明工具名。"
        )
        blocks = []
        for item in tools:
            blocks.append(
                f"### {item.get('name')}\n"
                f"说明：{item.get('description') or '（无）'}\n"
                f"参数定义：\n{item.get('param_text') or '（无参数）'}"
            )
        chat_lines = [
            f"- {item.get('name') or item.get('user_id')}: {_one_line(item.get('text'), 60)}"
            for item in list(recent_chat or [])[-6:]
        ]
        prompt = (
            "候选工具：\n\n"
            + "\n\n".join(blocks)
            + f"\n\n她的意图：{intent}\n"
            + ("\n最近聊天（仅供参考）：\n" + "\n".join(chat_lines) if chat_lines else "")
            + "\n\n请输出 {\"tool\": ..., \"params\": {...}}。"
        )
        return system, prompt

    def build_search_gap_prompt(
        self,
        *,
        topic: str,
        known: list[str],
        date_text: str = "",
        limit: int = 2,
    ) -> tuple[str, str]:
        """这一轮查回来的东西不够时，让辅助模型再想几个该补查的角度（返回 system, user）。"""

        system = (
            "你在帮一个角色补全联网检索的查询词。她刚才按一个主题查过一轮，"
            "现在需要你判断还缺什么，并给出一两条新的查询词。"
            "只输出一个 JSON 对象，格式：{\"queries\": [\"查询词1\", \"查询词2\"]}；"
            "不要解释、不要 Markdown；如果已经够了就输出 {\"queries\": []}。"
        )
        known_lines = "\n".join(f"- {_one_line(item, 80)}" for item in known[:8])
        prompt = (
            f"她要查的主题：{topic or '（没有写明）'}\n"
            + (f"今天的日期：{date_text}\n" if date_text else "")
            + "# 刚才查到的结果标题\n"
            + (known_lines or "（什么都没查到）")
            + "\n\n# 要求\n"
            f"1. 最多给 {max(1, int(limit))} 条查询词，每条不超过 30 字，要能当搜索框里的关键词用；\n"
            "2. 换角度、换关键词，不要和刚才查到的内容重复；\n"
            "3. 只需要补充关键信息（时间、地点、具体对象、官方来源）时给查询词；\n"
            "4. 已经够回答主题了就输出空列表。\n\n"
            "请输出 {\"queries\": [...]}。"
        )
        return system, prompt

    def build_search_continue_prompt(
        self,
        *,
        topic: str,
        asked: list[str],
        points: list[str],
        date_text: str = "",
        limit: int = 2,
    ) -> tuple[str, str]:
        """检索循环里"够了吗、还缺什么"的判断（返回 system, user）。

        这个判断交给**主模型**：它才知道手上这些材料够不够回答、还差哪一块。
        只输出 JSON：``{"done": true}`` 或 ``{"done": false, "queries": ["…"]}``。
        """

        system = (
            "你在判断「手上这些检索结果够不够回答问题」。"
            "只输出一个 JSON 对象：够了就 {\"done\": true}；"
            "不够就 {\"done\": false, \"queries\": [\"新的查询词\", …]}。"
            "不要解释、不要多余字段。"
        )
        asked_text = "、".join(f"「{_one_line(item, 24)}」" for item in asked[:6]) or "（还没查过）"
        point_lines = "\n".join(
            f"{index}. {_one_line(item, 120)}" for index, item in enumerate(points[:8], 1)
        )
        prompt = (
            f"要回答的主题：{topic or '（没写明）'}\n"
            + (f"今天的日期：{date_text}\n" if date_text else "")
            + f"\n已经查过的词：{asked_text}\n"
            + f"\n# 现在手上的结果\n{point_lines or '（什么都没查到）'}\n\n"
            "# 要求\n"
            "1. 已经有能回答主题的材料，就输出 {\"done\": true}；\n"
            "2. 下面这几种都算**不够**，要给新的查询词：\n"
            "   - 结果基本是首页 / 栏目页 / 导航页（读了也没有正文）；\n"
            "   - 主题要的是具体信息（发布日期、版本号、数字、结论），材料里却没有；\n"
            "   - 只有别人转述或第三方站点，没有官方公告。\n"
            f"   新查询词最多 {max(1, int(limit))} 条，要比上次更精确"
            "（例如补上「公告 / 官网 / 具体版本号 / 年份」），不要重复上面查过的；\n"
            "3. 只是结果不够完美、但已经能说清一件事，就算够了（不要没完没了地查）。"
        )
        return system, prompt

    def build_search_topic_prompt(
        self,
        *,
        where: str,
        state_hint: str,
        chat_lines: list[str] | None = None,
        persona_text: str = "",
    ) -> tuple[str, str]:
        """规则触发检索、她自己又没写想查什么时，让她自己想一句（返回 system, user）。

        比固定主题好在她会跟着此刻的处境和群里的话题走，而不是每次都搜同一个"今日热点"。
        """

        system = (
            "你是这个角色本人。用第一人称、一句话说清你现在想上网查什么。"
            "只输出这一句话，不要解释、不要加引号、不要罗列多个主题。"
        )
        lines = [f"- {item}" for item in (chat_lines or []) if str(item).strip()]
        prompt = (
            (f"# 你是谁\n{_one_line(persona_text, 400)}\n\n" if persona_text else "")
            + f"# 你此刻\n你在 {where or '某个地方'}；{state_hint}\n\n"
            + ("# 群里最近在聊\n" + "\n".join(lines) + "\n\n" if lines else "")
            + "# 要求\n"
            "1. 一句话，20 字以内，具体到能当搜索话题用（例如「今天有什么有意思的科技新闻」）；\n"
            "2. 只查一件事，不要写成「新闻、天气、比分」这种罗列；\n"
            "3. 群里正在聊的话题优先，没有就按你自己的兴趣来；\n"
            "4. 只输出这句话。"
        )
        return system, prompt

    def build_search_digest_prompt(
        self,
        *,
        topic: str,
        materials: list[str],
        limit: int = 6,
        date_text: str = "",
    ) -> tuple[str, str]:
        """把检索回来的材料压成"要点 + 编号"（返回 system, user）。

        给主模型的是要点而不是整篇原料：省 token，也让她不容易"没看清就再查一遍"。
        """

        system = (
            "你在整理检索到的资料。把材料压成几条要点，供另一个模型直接引用。\n"
            "只输出整理结果，不要解释、不要客套、不要 Markdown 标题。\n"
            "格式：\n"
            "结论：<一句话回答主题：材料里有什么就说什么>\n"
            "1. <要点，40 字以内>\n"
            "2. <要点>\n"
            "…最多 "
            f"{max(1, int(limit))} 条。要点末尾用（N）标出它来自第几条材料。\n"
            "规则：\n"
            "- 只写材料里出现过的事实，不要用常识补、不要编数字和时间；\n"
            "- **材料里有多少就写多少**：哪怕只是标题、栏目名、一句话摘要，也照样列出来；\n"
            "- 都不要写「材料里没有直接答案」这种空结论：真有缺口，就在结论里说清"
            "「材料只提到 X，没有 Y」，别一句话把整批材料否掉；\n"
            "- 重复的合并成一条；和主题无关的不要写。"
        )
        blocks = "\n".join(
            f"{index}. {_one_line(item, 400)}" for index, item in enumerate(materials, 1)
        )
        prompt = (
            f"要回答的主题：{topic or '（没写明）'}\n"
            + (f"今天的日期：{date_text}\n" if date_text else "")
            + f"\n# 材料\n{blocks}\n\n请按格式输出。"
        )

        return system, prompt

    # ---------------- 睡眠整理（睡眠记忆整理 / 用户画像） ----------------

    def build_consolidate_prompt(
        self,
        *,
        persona_text: str = "",
        chat_lines: list[str] | None = None,
        memories: list[str] | None = None,
        profiles: list[str] | None = None,
        pending: list[str] | None = None,
        date_text: str = "",
        mode: str = "full",
        max_items: int = 12,
    ) -> tuple[str, str]:
        """睡眠整理用的提示词（返回 ``system, user``）。

        ``mode``：``full`` = 整觉整理（消化 + 重组 + 遗忘 + 画像）；
        ``nap`` = 小睡轻整理（只做要点化 + 缩略版 + 一条梦，不动关系与事实）。
        """

        system = (
            "你在替一个角色整理她这段时间的经历。她睡着了，梦境与记忆都在重排。\n"
            "只输出一个 JSON 对象，不要解释、不要 Markdown、不要代码块。\n"
            "规则：\n"
            "- 只写材料里真的发生过的事，**不要编**：数字、时间、人名都必须来自材料；\n"
            "- 用她自己的口吻记下要点（第一人称，像她在回想）；\n"
            "- 提到具体的人时，用材料里的 QQ 号写进 participants，不要自己造人；\n"
            "- 只输出**改动**：没变的旧记忆不要抄回来；\n"
            "- 证据（evidence）写清材料里那个人原话的一小段，方便以后核对；\n"
            "- digests 里的那一段是**这个人的画像速写**：只写他是谁、怎么说话、喜欢什么、"
            "你们什么关系这类**稳定的特征**，**不要写他最近遇到的事 / 新闻 / 一次性的事件**"
            "（那种事放进 facts 的「近况」，或者留在记忆里）；\n"
            "- 关系可以升也可以降：该升级就写升级，你自己想清楚了也可以写降级"
            "（朋友退回群友、恋人退回朋友、甚至翻脸）——同类关系（群友 / 朋友 / 闺蜜 / "
            "男友 / 女友）她身上只会留最新那条，旧的那条自动变成「曾经」，所以"
            "**写你现在真实的想法**就行；降级同样要有原话当证据。"
        )
        chat = "\n".join(f"- {item}" for item in (chat_lines or [])[:400])
        old = "\n".join(f"- {item}" for item in (memories or [])[:120])
        people = "\n".join(f"- {item}" for item in (profiles or [])[:40])
        todo = "\n".join(f"- {item}" for item in (pending or [])[:20])
        if mode == "nap":
            schema = (
                "{\n"
                '  "notes": [{"text": "一句话要点", "participants": ["QQ号"], '
                '"emotion": "心情词", "context": "当时的原话"}],\n'
                '  "digests": {"QQ号": "这个人的画像速写（40 字内）"},\n'
                '  "dream": "她做了一个什么样的梦（一两句，可以荒诞一点）"\n'
                "}"
            )
        else:
            schema = (
                "{\n"
                '  "memories": [{"text": "要点（一句话）", "context": "当时的原话", '
                '"participants": ["QQ号"], "emotion": "心情词", "weight": 0.6, '
                '"keywords": ["关键词"], "review_in_days": 3}],\n'
                '  "merge": [{"keep_id": 12, "drop_ids": [13, 14], "text": "合并后的一句"}],\n'
                '  "facts": [{"user_id": "QQ号", "kind": "喜好|厌恶|习惯|基本信息|关系|约定|近况", '
                '"text": "关于他的事", "evidence": "他的原话", "confidence": 0.8}],\n'
                '  "relations": [{"user_id": "QQ号", "type": "主人|男友|朋友|闺蜜|家人|讨厌的人…", '
                '"asserted_by": "她的判断|他自称", "evidence": "原话", "confidence": 0.8}],\n'
                '  "digests": {"QQ号": "这个人的画像速写（40 字内）"},\n'
                '  "affinity": [{"user_id": "QQ号", "delta": 1, "reason": "为什么"}],\n'
                '  "forget": [{"id": 31, "text": "折叠成一句要点", "reason": "为什么可以忘掉"}],\n'
                '  "dream": "可选：她做了一个什么样的梦"\n'
                "}"
            )
        user = (
            (f"# 她是谁\n{_one_line(persona_text, 400)}\n\n" if persona_text else "")
            + (f"今天是 {date_text}。\n\n" if date_text else "")
            + f"# 这段时间发生的事（原始记录）\n{chat or '（这段时间没人说过话）'}\n\n"
            + (f"# 她现在的记忆（可以合并 / 改写 / 折叠）\n{old}\n\n" if old else "")
            + (f"# 她现在认识的人\n{people}\n\n" if people else "")
            + (f"# 还挂着的事（约定 / 没做完的）\n{todo}\n\n" if todo else "")
            + "# 要输出的 JSON\n" + schema + "\n\n"
            + f"最多给 {max(1, int(max_items))} 条新要点；没有可整理的就输出空对象 {{}}。"
        )
        return system, user

    # ---------------- 事件系统 ----------------

    def build_event_package_prompt(
        self,
        *,
        persona_brief: str,
        scene_line: str,
        state_line: str,
        thread_digest: str = "",
        recent_titles: list[str] | None = None,
        red_lines: list[str] | None = None,
        seed: str = "",
        tier: str = "small",
        recent_memories: list[str] | None = None,
        genre: dict[str, Any] | None = None,
        genre_scale: str = "",
    ) -> tuple[str, str]:
        """让打杂模型编一件她身边发生的事（返回 system, user）。

        这个提示词是**裁剪过**的：只带简易人设、她此刻在哪在做什么、这条线索的来龙去脉，
        不带世界规则、动作表、聊天记录——编一件小事用不上那些，带了反而分散注意力。
        """

        tier_note = {
            "micro": "微事件：只影响她自己的状态，不需要她做选择（options 留空）。",
            "small": "小事件：她要做个选择，给 2~3 个选项。",
            "big": "大事件：影响比较大，她可能需要找人商量，给 2~3 个选项。",
        }.get(str(tier), "小事件：她要做个选择，给 2~3 个选项。")
        system = (
            "你在给一个角色编一件她身边刚发生的事。可以是一件小事，"
            "也可以是一件需要她认真对待的事——不是任务、不是剧情大纲、不是给用户看的命题作文。\n"
            "只输出一个 JSON 对象，不要解释、不要 Markdown 代码块。\n"
            "输出格式：\n"
            "{\n"
            '  "title": "这件事的短标题（8 字以内）",\n'
            '  "scene": "一句话：她此刻在哪、在做什么",\n'
            '  "place_name": "这件事发生在哪（就在她现在这个地方就写那个地名；发生在别处或者外面，'
            '就写别处，例如「商业街」）",\n'
            '  "hook": "一句话：发生了什么（具体、有画面，不要抽象）",\n'
            '  "tier": "micro|small|big",\n'
            '  "kind": "solo|need_intervene",\n'
            '  "mode": "real|imagined",\n'
            '  "difficulty": 0.5,\n'
            '  "critical": false,\n'
            '  "options": [{"desc": "她会怎么处理（写动作，别写台词）",\n'
            '               "abilities": ["wits"]},\n'
            '              {"desc": "另一个做法", "abilities": ["dexterity"]},\n'
            '              {"desc": "先不管，去干别的", "no_check": true}],\n'
            '  "followup": {"success": "如果成了，下一步会怎样",\n'
            '               "fail": "如果没成，会怎样"},\n'
            '  "outcome": "微事件专用：直接发生的结果",\n'
            '  "effects": {"valence": -0.02},\n'
            '  "abilities": {"dexterity": -0.01},\n'
            '  "memory": "她自己会记住的一句话（可以留空）"\n'
            "}\n"
            "规则：\n"
            f"- {tier_note}\n"
            "- abilities 只能从 stamina（体力）/ wits（智力）/ dexterity（灵巧）/ "
            "composure（心性）里选；\n"
            "- 选项里**必须有一个能全身而退的**（no_check: true），她永远有退路；\n"
            "- difficulty 是这件事本身的难度：0.2 很轻松，0.5 得费点劲，0.8 相当棘手；\n"
            "- critical 只有在这件事**危险或紧急、她必须先处理完**时才填 true"
            "（例如被人跟着、正在下大雨回不去、屋里进了一只大虫子）；\n"
            "- 涉及判断、涉及别人、结果会分岔的事用 need_intervene（她会开口找人商量）；\n"
            "- 只有发生在想象、游戏、剧情里的事才用 mode: imagined（她的实际位置不变）；\n"
            "- **这件事必须和上面那个地点的描述对得上**：只能用那里本来就有的东西和设施，"
            "不要凭空给这个地点加东西（书房里不会突然出现自动售货机）；"
            "想不出合适的事，就写一件这里常见的小事；\n"
            "- 结果要具体，别写「发生了一些事」这种空话；\n"
            "- memory 要用**第一人称**写她经历的这件事（在哪、发生了什么、她怎么做、结果如何），"
            "写事实，不要写成经验或建议（不要出现「应该」「下次要」）；\n"
            "- 不要写她的台词，也不要提到「选项」「判定」「概率」。"
        )
        if genre:
            name = str(genre.get("name") or "").strip()
            examples = str(genre.get("examples") or "").strip()
            system += (
                "\n题材要求（这一件必须编在这个题材里，不要跑回别的小事）：\n"
                f"- 题材：{name or '日常'}"
                + (f"，例如：{examples}" if examples else "")
                + "\n"
            )
            if genre_scale.strip():
                system += f"- 尺度：{genre_scale.strip()}\n"
        blocks = [
            "# 她是谁（摘要）\n"
            f"{persona_brief or '（没有人设摘要，按常识演一个普通角色）'}"
        ]
        if state_line:
            blocks.append(f"# 她此刻\n{state_line}")
        if scene_line:
            blocks.append(f"# 场景\n{scene_line}")
        if thread_digest:
            blocks.append(f"# 这件事的来龙去脉（新事件要跟它接得上）\n{thread_digest}")
        if recent_titles:
            blocks.append(
                "# 最近已经发生过（不要重复这些）\n"
                + "\n".join(f"- {item}" for item in list(recent_titles)[:10])
            )
        if recent_memories:
            blocks.append(
                "# 她最近的记忆（可以当素材，别硬凑）\n"
                + "\n".join(f"- {item}" for item in list(recent_memories)[:4])
            )
        if red_lines:
            blocks.append(
                "# 不要生成这些\n" + "\n".join(f"- {item}" for item in list(red_lines))
            )
        if seed:
            blocks.append(f"# 这次要编的方向（用户指定的，必须照着来）\n{seed}")
        blocks.append("请按上面的 JSON 格式输出这一件事。")
        return system, "\n\n".join(blocks)

    def build_event_decision_prompt(
        self,
        *,
        persona_text: str,
        title: str,
        hook: str,
        options: list[str],
        state_line: str = "",
        ability_line: str = "",
        suggestions: str = "",
        second_round: bool = False,
        scene_note: str = "",
        action_lines: str = "",
        observations: str = "",
        act_rounds_left: int = 0,
        session_directory: str = "",
    ) -> tuple[str, str]:
        """她自己的抉择（返回 system, user）。

        这里**不给概率数字**，只给「她觉得行不行」的人话：她只管选，
        成不成由判定说了算。

        ``action_lines`` 非空时，她这一轮可以先用一个动作去查清楚/做点准备
        （``phase="act"``），把结果吃进去之后再拿主意（``phase="decide"``）。
        """

        persona = (persona_text or "").strip()
        system = (
            (persona + "\n\n" if persona else "")
            + "你现在没在跟人聊天，而是自己遇上了一件事，得决定怎么办。\n"
            "用你自己的性格和习惯来选，别当老实人，也别为了稳妥一律挑最保守的。\n"
            "只输出一个 JSON 对象，不要解释、不要 Markdown。\n"
            "先判断：现在就拿主意（phase=\"decide\"），还是先去做点什么再决定"
            "（phase=\"act\"，比如查一下、试一下——只有在给了「可以先用它做点什么」的时候才能这么写）。\n"
            "输出格式：\n"
            "{\n"
            '  "phase": "decide",\n'
            '  "pick": 1,\n'
            '  "reason": "一句你为什么这么选（给自己看的，不会发出去）",\n'
            '  "ask_help": false,\n'
            '  "send_to": "这件事想说给哪个会话听（见「你能说话的地方」）；不说就留空",\n'
            '  "say": {\n'
            '    "success": [{"text": "这件事办成之后你会说的话", "delay": 0}],\n'
            '    "fail": [{"text": "这件事没成你会说的话", "delay": 0}]\n'
            "  }\n"
            "}\n"
            "要先用一个动作时改成：\n"
            '{"phase": "act", "reason": "为什么先做这个",'
            ' "actions": [{"type": "动作 id", "intent": "你想用它做什么"}],'
            ' "say": {"plan": [{"text": "可以顺手说一句，也可以不写"}]}}\n'
            "规则：\n"
            "- phase=act 时只写 actions（最多一个动作）和可选的一句 say；"
            "动作的结果会交回给你，那时你再来拿主意——所以别在同一轮里又写 pick；\n"
            "- pick 是上面的选项编号（从 1 开始）；你要是想出了更好的做法，"
            '改用 "desc": "你的做法" 写清楚，并给出 "abilities"；\n'
            "- say 里**成功和失败两条都要写**：系统会按最终结果挑一条发出去，"
            "写不出来就给空数组（那就不说话）；\n"
            "- say 里的话要像在群里随口讲的（一两句、带你的语气、可以带情绪），"
            "**群里没人知道你刚遇上了什么**：别只写一句反应，用半句话把前因带上"
            "（「有人想拿本小姐当免费传话筒，想得美」比「本小姐又不是跑腿的」清楚得多），"
            "也别把事件标题原样当名词念；\n"
            "- 不要写成旁白、不要报流水账、不要提「选项」「判定」「概率」这类词；\n"
            "- delay 是隔几秒再发下一条（0~10 秒），分条发才像真人；\n"
            "- 这件事要是需要找人商量，把 ask_help 设为 true；能自己搞定就别麻烦别人；\n"
            "- send_to 是你说话的地方（哪几个群 / 私聊都是你，只是地方不同）："
            "想跟谁说话、想跟谁商量，就写那儿；留空就留在你现在待的地方。"
            + (
                "\n- 不要再调动作了：这一轮直接给 pick。"
                if action_lines and act_rounds_left <= 0
                else ""
            )
        )
        blocks = [f"# 你遇上的事：{title}\n{hook}"]
        if state_line:
            blocks.append(f"# 你此刻\n{state_line}")
        if ability_line:
            blocks.append(ability_line)
        if options:
            blocks.append("# 你能想到的做法\n" + "\n".join(options))
        if action_lines:
            blocks.append(
                "# 可以先用它做点什么（用动作去查清楚、或先做点准备）\n"
                + action_lines
                + (
                    "\n每一轮只挑一个。它的结果会交回给你，你再判断这件事怎么办。"
                    if act_rounds_left > 0
                    else "\n（这一轮已经不能再调了，直接拿主意。）"
                )
            )
        if observations:
            blocks.append(
                "# 你刚才做的事、拿到的结果（用来判断这件事怎么办，不用跟人复述格式）\n"
                + observations
            )
        if suggestions:
            blocks.append("# 群里刚有人回了话（不一定要听，自己判断）\n" + suggestions)
        if session_directory:
            blocks.append(session_directory)
        if scene_note:
            blocks.append("# 关于场景\n" + scene_note)
        blocks.append(
            "# 现在做决定\n"
            + (
                "上面这些是群友给的主意。你可以采纳、可以挑一条、也可以全不听，"
                "真拿不准就挑一个你觉得最靠谱的。"
                if second_round
                else "按你的性格挑一个做法，或者自己写一个。"
            )
            + "只输出 JSON。"
        )
        return system, "\n\n".join(blocks)

    def build_event_settle_prompt(
        self,
        *,
        title: str,
        hook: str,
        desc: str,
        tier_label: str,
        state_line: str = "",
        suggestions_digest: str = "",
        next_step_hint: str = "",
        fail_menu: list[str] | None = None,
        must_continue: bool = False,
    ) -> tuple[str, str]:
        """结算：这件事到底怎么样了（返回 system, user）。

        这一步**不写台词**——台词是主模型的事。这里只产出事实、数值变化和有没有后续。
        """

        fail_block = ""
        menu = [str(item) for item in list(fail_menu or []) if str(item).strip()]
        if menu:
            fail_block = (
                "- 「没成」不等于「受伤」：下面这些才是最常见的没成，每次挑一种，"
                "别一连几次都写成磕了碰了流血了：\n"
                + "\n".join(f"  · {item}" for item in menu)
                + "\n"
            )
        continue_rule = (
            "- 这件事**还没到收尾**：followup 必须写出下一步会发生什么（不能留空）；\n"
            if must_continue
            else ""
        )
        system = (
            "你在给一件事写结果。只输出一个 JSON 对象，不要解释、不要 Markdown。\n"
            "输出格式：\n"
            "{\n"
            '  "outcome": "发生了什么（一句话，具体，别写空话）",\n'
            '  "ability_delta": {"dexterity": -0.02},\n'
            '  "state_delta": {"valence": -0.03, "affect": 0.05},\n'
            '  "followup": "如果还有下一步，一句话说明；没有就留空字符串",\n'
            '  "memory": "她以第一人称记下的这件事（可以留空）"\n'
            "}\n"
            "规则：\n"
            "- outcome 必须和「她选的做法 + 判定档位」对得上：大成功要写出超预期的好，"
            "勉强成功要带点代价，失败要写出具体损失；\n"
            + fail_block
            + "- memory 用**第一人称**写她经历的这件事（在哪、发生了什么、她怎么做、结果如何）："
            "写事实，不要写成经验或建议（不要出现「应该」「下次要」）；\n"
            "- 失败也要有信息量：写清她**知道了什么 / 下次会怎么做**，别只写「没成功」；\n"
            "- ability_delta 每项在 ±0.05 以内，只改真的被这件事影响的项；"
            "失败时更容易长能力值（长了教训）；\n"
            + continue_rule
            + "- state_delta 每项在 ±0.1 以内，只写受影响的那几项："
            "affect（心潮：被激起的强度）、valence（心情好坏）、energy（精力）、"
            "loneliness（孤独感）、curiosity（好奇心）、boredom（无聊）；\n"
            "- followup 只在真的还有下文时才写（例如手烫着了得处理一下）；"
            "已经了结就留空；\n"
            "- 不要写台词、不要写旁白、不要提到「判定」「概率」「选项」。"
        )
        blocks = [f"# 这件事\n{title}：{hook}", f"# 她最后选的做法\n{desc}"]
        blocks.append(f"# 结果档位\n{tier_label}")
        if state_line:
            blocks.append(f"# 她此刻\n{state_line}")
        if suggestions_digest:
            blocks.append(f"# 群友给过的建议（影响过这件事）\n{suggestions_digest}")
        if next_step_hint:
            blocks.append(f"# 之前的伏笔\n{next_step_hint}")
        blocks.append("请按上面的 JSON 格式输出结果。")
        return system, "\n\n".join(blocks)

    def build_help_ask_prompt(
        self,
        *,
        persona_text: str,
        title: str,
        hook: str,
    ) -> tuple[str, str]:
        """她开口求助时说的那一两句（返回 system, user）。

        以前是代码拼一句固定文案，谁遇上什么事都是同一句；这里让她自己说，
        只要求两件事：说清发生了什么、像在群里开口求人（不是播报）。
        """

        persona = (persona_text or "").strip()
        system = (
            (persona + "\n\n" if persona else "")
            + "你遇上一件自己拿不准的事，想在群里开口问一句。\n"
            "只输出一个 JSON 对象，不要解释、不要 Markdown。\n"
            '输出格式：{"lines": ["第一句", "第二句（可以不要）"]}\n'
            "规则：\n"
            "- 一共 1~2 句，每句 30 字以内，像随手打的字；\n"
            "- 至少有一句要让群里听懂**发生了什么**（别只写「有人吗」这种空话）；\n"
            "- 用你自己的语气，可以带一点情绪，但别客套、别解释设定、别提「事件」「选项」。"
        )
        prompt = (
            f"# 你遇上的事\n{title}\n{hook}\n\n"
            "写你要发到群里的那一两句（JSON）。"
        )
        return system, prompt

    def build_help_remind_prompt(
        self,
        *,
        persona_text: str,
        title: str,
        hook: str,
        asked: str = "",
    ) -> tuple[str, str]:
        """她求助之后没人接，自我圆场的那一句（返回 system, user）。

        以前这里是代码写死三句「算了，本小姐自己来」——不管用户的人设是什么，
        她都会突然冒出一句"本小姐"，一句就把人设打破。这里交给她自己写，
        写不出来就干脆不说（静默推演比说错话强）。
        """

        persona = (persona_text or "").strip()
        system = (
            (persona + "\n\n" if persona else "")
            + "你刚才在群里问了一件事，但一直没人接话，你不想干等着了。\n"
            "只输出一个 JSON 对象，不要解释、不要 Markdown。\n"
            '输出格式：{"line": "你要说的那一句（不想说就留空字符串）"}\n'
            "规则：\n"
            "- **最多一句**，25 字以内；\n"
            "- 说的是「我自己来 / 不等了」这类圆场，但要用你自己的语气说出来，"
            "不要写成旁白、不要报流水账；\n"
            "- 不要抱怨群里没人理你（那会让看到这句话的人觉得被指责）；\n"
            "- 想不出合适的说法就把 line 写成空字符串——**宁可不说**。"
        )
        asked_line = f"# 你刚才说的是\n{asked.strip()}\n\n" if str(asked or "").strip() else ""
        prompt = (
            f"# 你遇上的事\n{title}\n{hook}\n\n"
            f"{asked_line}"
            "写下你现在要说的那一句（JSON）。"
        )
        return system, prompt

    def build_suggestion_filter_prompt(
        self,
        *,
        title: str,
        hook: str,
        her_plan: str,
        messages: list[str],
        red_lines: list[str] | None = None,
    ) -> tuple[str, str]:
        """分拣群友的回应（返回 system, user）。

        只丢「和这件事完全无关」的：起哄、打趣、打气、建议都要留下——
        她需要被起哄激将、需要有人打气，这些也是这件事的一部分。
        """

        system = (
            "你在分拣一群人针对某件事说的话。判断标准只有一条："
            "**这条是不是针对这件事说的**（不是有没有用）。\n"
            "只输出一个 JSON 对象，不要解释、不要 Markdown。\n"
            "输出格式：\n"
            "{\n"
            '  "related": [{"from": "说话人昵称", "kind": "suggestion|tease|cheer",\n'
            '               "point": "这条说了什么（30 字以内，写成她能照着做的样子）"}],\n'
            '  "irrelevant": [{"from": "说话人昵称", "point": "为什么与这件事无关"}]\n'
            "}\n"
            "规则：\n"
            "- 给具体做法的算 suggestion；起哄、看热闹、拿她开玩笑的算 tease；"
            "单纯加油鼓劲的算 cheer；\n"
            "- 起哄和打趣**一定要留下**（哪怕它没用），它们也是这件事的一部分；\n"
            "- 同一个人连发几条就合并成一条；\n"
            "- 只有在聊完全无关的话题（例如问今晚吃什么）才放进 irrelevant；\n"
            "- 一条都没有就两个数组都留空。"
        )
        if red_lines:
            system += "\n另外这些属于越界内容，直接算 irrelevant：" + "、".join(
                str(item) for item in list(red_lines)
            )
        blocks = [
            f"# 这件事\n{title}：{hook}",
            f"# 她打算怎么做\n{her_plan or '（还没定）'}",
            "# 群里的回应\n" + ("\n".join(messages) if messages else "（没有）"),
            "请按上面的 JSON 格式输出。",
        ]
        return system, "\n\n".join(blocks)

    def build_persona_brief_prompt(
        self, *, persona_text: str, limit: int = 250
    ) -> tuple[str, str]:
        """把主人设压成一段说话风格摘要，给打杂模型当上下文（返回 system, user）。"""

        low = max(80, int(limit) - 100)
        system = (
            "你在给同一个角色写一段摘要，供另一个模型在生成事件、整理结果时参考。\n"
            "这是摘要，不是新角色：不要新增设定、不要改口吻、不要美化、不要评价。\n"
            f"只输出摘要本身，一段话，{low}~{int(limit)} 字，不要标题、不要列表、不要引号。\n"
            "保留：身份与自称、口头禅与语气（毒舌/温柔/傲娇之类）、怎么称呼对方、"
            "平时说话的长度习惯、禁忌与边界、情绪上来了会怎么表现。\n"
            "去掉：世界观设定、外貌描写、背景故事、与说话风格无关的能力描述。"
        )
        prompt = (
            "# 完整人设\n"
            f"{str(persona_text or '').strip()[:4000]}\n\n"
            "请输出摘要，只写这段摘要本身。"
        )
        return system, prompt

    VOICE_SCENES = (
        ("tease", "被撩：对方说「想你了」这种"),
        ("snap", "被怼：对方说了很难听的话"),
        ("ignored", "被冷落：她说了话没人接"),
        ("night", "深夜、累了、想睡"),
        ("called", "群里被点名：很多人看着"),
        ("cant", "对方让她做她做不到的事"),
        ("boundary", "越界请求：要照片那种，要按亲密度拒绝"),
        ("busy", "她自己正在忙：锅还在火上"),
        ("jealous", "对方夸别人没夸她"),
        ("comfort", "对方难过，需要安慰"),
    )
    """声音样例的默认场景（场景 id + 给生成模型看的说明）。"""

    def build_persona_review_prompt(
        self, *, persona_text: str, pronoun: str = "她", length_hint: str = ""
    ) -> tuple[str, str]:
        """「优化人设」：先体检、再给可逐条采纳的改动（返回 system, user）。

        为什么要先体检：直接说"帮我优化"，模型会干两件不想看到的事——
        **把它改长**（人设是每轮都进提示词的第 1 层，长一截就挤掉后面的规则）、
        **把它改通用**（往"温柔可爱善解人意"的模板收，个性被洗掉）。
        所以顺序是：先诊断，再按问题改。
        """

        system = (
            f"你在帮一个角色卡做体检和优化。角色卡是给一个长期在群聊里生活的角色用的提示词，"
            f"每轮都会带上，所以**它同时决定{pronoun}怎么说话、也占用每一轮的预算**。\n\n"
            "你只做两件事：\n"
            "一、体检。按下面七条逐条给结论，只报**真的有问题**的：\n"
            "1. 重复：写了插件已经在做的（世界观、能移动、能做事、有日程、输出 JSON、"
            "关系与亲密度上限、情绪怎么用、记忆与回想）——这些要么删掉，要么写清优先级；\n"
            f"2. 缺可演维度：称呼（怎么叫对方 / 怎么自称）、说话长度与节奏、口癖与语气词、"
            f"怎么表达不满、怎么拒绝、怎么表达亲近、不熟时怎么保持距离、雷点、明确不做的事；\n"
            "3. 形容词没落地：「温柔」「傲娇」这类必须能举出行为（遇到什么会怎么做），举不出来就得补；\n"
            "4. 自相矛盾：例如又高冷又话唠、又说不主动又爱撒娇——要么选一个，要么写成带条件的；\n"
            "5. 篇幅：太长会稀释后面的规则；\n"
            "6. 不该出现的：系统/元信息、通用美德清单、诱发助手腔的表述"
            "（「尽力帮助用户」「让用户满意」）、与插件机制冲突的设定（永远待命、秒回、随时满足）；\n"
            "7. 怪癖：原文里那些**只有这个人会有**的怪癖要标出来保护，不许改。\n"
            "二、给改动。只给「跟原文的差别」，不要重写整份。\n\n"
            "铁律：\n"
            "- **身份、名字、性别、称呼、关系、禁区，一个字都不许动**；\n"
            "- 不知道的别编：原文没写过的经历、没提过的人，一律不许新增；\n"
            "- 不许变长太多"
            + (f"（这次的长度要求：{length_hint}）；\n" if length_hint else "；\n")
            + "- 只输出 JSON，不要解释、不要 Markdown 代码块。"
        )
        prompt = (
            "# 角色卡原文\n"
            f"{str(persona_text or '').strip()[:8000]}\n\n"
            "# 输出格式（只输出 JSON）\n"
            "{\n"
            '  "ok": ["原文里写得好的地方，最多 3 条（给用户信心，也提示你别乱动）"],\n'
            '  "issues": [\n'
            '    {"level": "建议改|和插件机制冲突", "kind": "重复|缺维度|形容词|矛盾|篇幅|不该有",'
            ' "detail": "一句话说清问题", "quote": "原文里对应的片段（没有就留空）"}\n'
            "  ],\n"
            '  "rewrite": [\n'
            '    {"before": "原文片段（必须是原文里能逐字找到的）", "after": "改后", "why": "10 字以内"}\n'
            "  ],\n"
            '  "add": [\n'
            '    {"field": "口头习惯", "text": "要补进去的一小段", "why": "10 字以内"}\n'
            "  ],\n"
            '  "questions": ["你猜不准、需要用户确认的点，最多 3 条"]\n'
            "}"
        )
        return system, prompt

    INTENT_CHOICES = (
        "闲聊",
        "夸她",
        "阴阳/挤对",
        "试探",
        "开玩笑",
        "求安慰",
        "求助",
        "越界越线",
        "邀约/叫你做事",
        "别的",
    )

    def build_analyst_prompt(
        self,
        *,
        state: WorldState,
        node: NodeDef | None,
        user_text: str = "",
        user_name: str = "",
        recent_chat: list[dict[str, Any]] | None = None,
        profile_text: str = "",
        memories: list[Any] | None = None,
        other_context: str = "",
        open_topics_text: str = "",
        prev_analysis: str = "",
        pronoun: str = "她",
    ) -> tuple[str, str]:
        """「分析层 v2」：**不给它自由发挥**——只让它在给定选项里挑，再点出该想起哪条记忆。

        v1（让它写小作文）实测会自己编一个叙事然后每轮套用（"她又在装可怜求关注"），
        与用户真正说的话脱钩。所以这一版：

        - 输入以**这一轮这句话**为锚，最近几句只当背景；
        - 意图只能从固定选项里挑；
        - 该想起哪条记忆，只准**按编号点**，不许自己编；
        - 不给"最近发生的事/事件线索"（那正是 v1 抓着不放的东西）；
        - 输出是固定几行，每行都有格式，写不出就写「无」。
        """

        system = (
            "你是这个角色的情境分析器。**不要写小作文、不要编故事、不要推测剧情**，"
            "只做判断题，按固定格式一行一行填。\n\n"
            "输出格式（严格照抄这六行，每行一句话；没有的写「无」）：\n"
            "意图：<从这些里挑一个：" + " / ".join(self.INTENT_CHOICES) + ">\n"
            "他要什么：<一句大白话，说清他此刻想要什么>\n"
            "可否亲昵：<可以 / 不可以>（看下面那份「现在还不能」）\n"
            "该想起：<只写下面记忆列表里的编号，例如「2」；没有就写 无>\n"
            "旧事：<提 / 不提>（有没有还没聊完、这轮适合接一句的）\n\n"
            "铁律：\n"
            "- **不许出现台词**：一个字都不许替她写；\n"
            "- 不许写「这一轮表面在…实际是…」这种编造；只回答上面的问题；\n"
            "- 记忆只准按编号引用，**不许自己编记忆**；\n"
            "- 绝大多数轮次就是普通闲聊（意图＝闲聊），别把人往复杂里想；\n"
            f"- **不要描述{pronoun}的状态或心情**——那部分主人格自己看得见，你写只会重复；\n"
            "- 同一件事不许连着两轮出现在你的分析里；上一轮的结论不要复述。"
            + (
                "\n- 上一轮你已经判过一次：这次只写这一轮的结果。"
                if prev_analysis
                else ""
            )
        )
        chat_lines: list[str] = []
        for item in (recent_chat or [])[-4:]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            if not text:
                continue
            who = str(item.get("name") or item.get("user_id") or "")
            chat_lines.append(f"{who}：{text}")
        memory_lines: list[str] = []
        for index, item in enumerate((memories or [])[:6], start=1):
            text = str(getattr(item, "content", "") or getattr(item, "text", "") or item)
            if text:
                memory_lines.append(f"{index}. {text}")
        blocks = [
            f"# 这一轮他说的那句话（分析对象）\n{user_name or '他'}：{str(user_text or '').strip()}",
            "# 前面几句（只是背景）\n" + ("\n".join(chat_lines) or "（没有）"),
            "# 她此刻（背景，不要每轮都提）\n"
            + "\n".join([f"位置：{node.name if node else '未知'}｜心情：{state.mood}"]),
        ]
        if profile_text:
            blocks.append("# 他跟她现在的关系\n" + profile_text.strip())
        if memory_lines:
            blocks.append("# 可能用得上的记忆（只准按编号引用）\n" + "\n".join(memory_lines))
        if other_context:
            blocks.append("# 别的会话里同时发生的\n" + str(other_context).strip()[:400])
        if open_topics_text:
            blocks.append(open_topics_text.strip())
        if prev_analysis:
            # 让它看见自己上一轮说了什么，才能真的"不重复"
            blocks.append("# 你上一轮的分析（这轮别复述）\n" + str(prev_analysis).strip()[:400])
        blocks.append("现在按那六行格式回答，只回答这六行。")
        return system, "\n\n".join(blocks)

    def build_analyst_prompt_v1(
        self,
        *,
        state: WorldState,
        node: NodeDef | None,
        recent_chat: list[dict[str, Any]] | None = None,
        profile_text: str = "",
        memories: list[Any] | None = None,
        other_context: str = "",
        open_topics_text: str = "",
        prev_analysis: str = "",
        pronoun: str = "她",
    ) -> tuple[str, str]:
        """「分析层」：把散落的数值 / 关系 / 记忆翻译成"这对这一轮意味着什么"（返回 system, user）。

        它**不决定怎么说**，只负责解释与找回关联——产出是给主人格的**补充**，不是替换。
        所以原始信息照样给主人格，它判错了也有人兜底。
        """

        system = (
            "你是这个角色的情境分析师。你的活是：把下面这一堆状态数值、关系档位、记忆和聊天记录，"
            f"翻译成**这个角色看得懂的大白话**，让她知道这一轮真正在发生什么。\n\n"
            "你只写这几件事，每件一行，能省就省：\n"
            "1. 这一轮的话题**实际上**是什么（有没有潜台词、有没有反话）；\n"
            "2. 对方此刻想要什么（讨照片？求安慰？只是随口一聊？）；\n"
            "3. 她跟他现在到哪一档、**因此什么能做、什么不能做**（结合关系档位与数值判断）；\n"
            "4. 有没有该想起来的旧事（点出具体哪一条，别泛泛说「记得以前」）；\n"
            "5. 她此刻的真实状态（累 / 烦 / 开心，用大白话，**别报数字**）；\n"
            "6. 跨会话要不要衔接（别的地方说过什么、这轮要不要接着提）。\n\n"
            "铁律：\n"
            "- **只写上面这些推不出来的判断**：不要复述聊天记录、不要念数值、不要替她决定语气风格；\n"
            f"- **一个字台词都不许写**：不许出现{pronoun}会说的句子；\n"
            "- 4~7 行，不要 JSON、不要标题、不要解释你的推理过程；\n"
            "- 真的没什么可分析就写「这一轮就是普通闲聊，照常应付」。"
            + (
                "\n- 上一轮你已经分析过一次了：**这次必须写不一样的东西**，"
                "上一轮的结论不要复述（除非这一轮确实发生了变化，那就写「变了什么」）。"
                if prev_analysis
                else ""
            )
        )
        chat_lines: list[str] = []
        for item in (recent_chat or [])[-8:]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            if not text:
                continue
            who = str(item.get("name") or item.get("user_id") or "")
            if who.startswith("你") or item.get("mine"):
                chat_lines.append(f"{pronoun}自己：{text}")
            else:
                chat_lines.append(f"{who}：{text}")
        blocks = [
            "# 她此刻\n"
            + "\n".join(
                [
                    f"位置：{node.name if node else '未知'}｜心情：{state.mood}",
                    f"精力 {state.energy:.2f}｜孤独感 {state.loneliness:.2f}｜无聊 {state.boredom:.2f}"
                    f"｜心潮 {state.affect:.2f}｜效价 {state.valence:.2f}",
                    f"手头的事：{(state.current_action or {}).get('desc') or '没在做什么'}",
                ]
            ),
        ]
        if profile_text:
            blocks.append("# 这个人的档案\n" + profile_text.strip())
        if memories:
            rows = []
            for item in memories[:6]:
                text = str(getattr(item, "content", "") or getattr(item, "text", "") or item)
                if text:
                    rows.append(f"- {text}")
            if rows:
                blocks.append("# 跟她有关、可能用得上的记忆\n" + "\n".join(rows))
        if chat_lines:
            blocks.append("# 最近的对话（分清谁说的）\n" + "\n".join(chat_lines))
        if other_context:
            blocks.append("# 别的会话里同时发生的\n" + str(other_context).strip()[:600])
        if open_topics_text:
            blocks.append(open_topics_text.strip())
        if prev_analysis:
            blocks.append("# 上一轮你的分析（这轮别复述）\n" + prev_analysis.strip()[:600])
        blocks.append("现在写这一轮的分析，4~7 行，只写分析本身。")
        return system, "\n\n".join(blocks)

    def analyst_layer(self, analysis: str, *, pronoun: str = "她") -> str:
        """把分析结果作为**补充**贴给主人格（原始信息仍然都在）。"""

        body = str(analysis or "").strip()
        if not body:
            return ""
        return (
            f"# 这一轮的分析（系统给的补充；**若跟上面的事实冲突，以事实为准**）\n"
            f"{body}"
        )

    def build_director_prompt(
        self,
        *,
        persona_text: str,
        state: WorldState,
        node: NodeDef | None,
        recent_chat: list[dict[str, Any]] | None = None,
        profile_text: str = "",
        open_topics_text: str = "",
        style_block: str = "",
        pronoun: str = "她",
    ) -> tuple[str, str]:
        """「导演层」：先用便宜模型写一份人话简报，再让主人格照着说（返回 system, user）。

        这一层存在的意义是把 1.6 万字压成 3~6 行——主人格那一轮只需要"这一轮怎么办"，
        不需要再读一遍全部状态。**简报绝对不许写台词**：它一写台词，主人格就变复述机。
        """

        system = (
            f"你是这个角色的导演。你只看一件事：{pronoun}此刻的处境，然后写一份**人话简报**，"
            f"告诉她这一轮该怎么办。\n\n"
            "只写这五件事，每件一行，能省就省：\n"
            "1. 她在干嘛、要不要马上回；\n"
            "2. 这一轮的态度（接梗 / 拆台 / 敷衍 / 认真 / 拒绝 / 只说半句）；\n"
            "3. 要不要提旧事（提哪一条）；\n"
            "4. 要不要把生活带进来（带哪一句）；\n"
            "5. 说给谁、说几句。\n\n"
            "铁律：\n"
            f"- **不要写台词**：不许出现{pronoun}会说的句子，也不要替她定稿；\n"
            "- 不要 JSON、不要标题、不要解释为什么这么判；\n"
            "- 3~6 行，每行一句话；真的没什么特别就写「照常回一句」；\n"
            "- 你看不到以后的事，别安排动作，只说这一轮。"
        )
        chat_lines: list[str] = []
        for item in (recent_chat or [])[-4:]:
            if not isinstance(item, dict):
                continue
            who = str(item.get("name") or item.get("user_id") or "")
            text = str(item.get("text") or "")
            if text:
                chat_lines.append(f"{who}：{text}")
        blocks = [
            "# 她是谁（摘要）\n" + str(persona_text or "").strip()[:900],
            "# 此刻\n"
            + "\n".join(
                [
                    f"位置：{node.name if node else '未知'}",
                    f"状态：{state.mood}｜精力 {state.energy:.2f}｜孤独 {state.loneliness:.2f}"
                    f"｜心潮 {state.affect:.2f}｜效价 {state.valence:.2f}",
                    f"手头的事：{(state.current_action or {}).get('desc') or '没在做什么'}",
                ]
            ),
        ]
        if chat_lines:
            blocks.append("# 最近几句\n" + "\n".join(chat_lines))
        if profile_text:
            blocks.append("# 他在她眼里\n" + profile_text.strip()[:600])
        if open_topics_text:
            blocks.append(open_topics_text.strip())
        if style_block:
            blocks.append(style_block.strip())
        blocks.append("现在写这一轮的简报，3~6 行，只写简报本身。")
        return system, "\n\n".join(blocks)

    def build_director_reply_system(
        self,
        *,
        persona_text: str,
        brief: str,
        samples: list[dict[str, Any]] | None = None,
        recent_chat: list[dict[str, Any]] | None = None,
        pronoun: str = "她",
    ) -> str:
        """主人格在"决策层"模式下拿到的提示词：人设 + 简报 + 最近几句 + 一个最小格式。

        刻意**不塞**状态、画像、记忆、可达表——那些本该由简报承担。
        """

        chat_lines: list[str] = []
        for item in (recent_chat or [])[-3:]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            if text:
                who = str(item.get("name") or item.get("user_id") or "")
                chat_lines.append(f"- {who}：{text}")
        layers = [
            f"# ========== 第 1 层：你是谁 ==========\n{str(persona_text or '').strip()}",
        ]
        sample_text = self.samples_layer(samples, pronoun=pronoun)
        if sample_text:
            layers.append(sample_text)
        layers.append(f"# 这一轮怎么办（导演写的，照这个来）\n{str(brief or '').strip()}")
        if chat_lines:
            layers.append("# 刚刚这几句（原文）\n" + "\n".join(chat_lines))
        layers.append(
            "# 输出格式\n"
            '只输出 JSON：{"actions":[{"type":"say","messages":["…"]}]}\n'
            "想说就填 messages；一句话拆成几条也行。**不想说就把 actions 写成空数组**。\n"
            "不要解释、不要 Markdown、不要代码块。"
        )
        return "\n\n".join(layers)

    def build_eval_script_prompt(
        self, *, persona_text: str, pronoun: str = "她", rounds: int = 20
    ) -> tuple[str, str]:
        """测评剧本：用同一份考卷比较不同模型演得像不像（返回 system, user）。

        **这份东西绝不进她的提示词**——进了就是背题，测不出模型能力。
        """

        system = (
            f"你在写一份用来**测模型演得像不像**的对话剧本：同一个剧本拿去跑不同模型，"
            f"让用户盲选哪一版更像{pronoun}。\n"
            "所以：只写用户说的话和场景，**绝对不要写角色会怎么回答**（那是要考的东西）。\n"
            "每轮给四样：场景、用户说的原话、「这一轮看什么」（一句可观察的行为）、禁忌（一句）。\n"
            "场景要覆盖：日常闲聊 / 被撩 / 被怼 / 被冷落 / 越界请求 / 让她做做不到的事 / "
            "她自己正在忙 / 深夜 / 群里多人同时说话 / 认真求助。\n"
            "只输出 JSON，不要解释、不要 Markdown 代码块。"
        )
        prompt = (
            "# 角色卡\n"
            f"{str(persona_text or '').strip()[:6000]}\n\n"
            f"# 要求\n写 {max(4, int(rounds))} 轮。\n\n"
            "# 输出格式（只输出 JSON）\n"
            "{\n"
            '  "rounds": [\n'
            '    {"scene": "被撩", "user_text": "用户原话", "watch": "这一轮看什么（可观察）",'
            ' "taboo": "这一轮不该出现什么"}\n'
            "  ]\n"
            "}"
        )
        return system, prompt

    def build_voice_sample_prompt(
        self, *, persona_text: str, pronoun: str = "她", name: str = "",
        scenes: list[str] | None = None,
    ) -> tuple[str, str]:
        """生成「范例台词」的提示词（返回 system, user）。

        产物是**样例**，不是规则：喂给主模型看"她平时怎么说话"。
        所以这里反复强调不许发明设定、不许出现助手腔——那不是她的声音。
        """

        picked = [item for item in self.VOICE_SCENES if item[0] in set(scenes or [])] or [
            item for item in self.VOICE_SCENES
        ]
        system = (
            f"你在给一个角色写「范例台词」。这些话会作为样本喂给角色自己，让{pronoun}照着这个口吻说话。\n\n"
            "铁律：\n"
            f"1. 第一人称，就是{pronoun}本人说的话。只写台词本身——不要旁白、不要括号里的动作说明。\n"
            "2. 每条 1~3 句，短。不许写成一段独白。\n"
            "3. 不许有助手腔：不出现「我理解你的感受」「有什么我可以帮你」「作为…」，也不解释自己是什么。\n"
            "4. 同一个场景里的几条必须是**不同的走法**（反问 / 拆台 / 只说半句 / 先说自己再说事 / "
            "直接拒绝 / 假装没听懂…），不是同一个意思换三种说法。\n"
            "5. 只依据角色卡。角色卡里没有的口癖、经历、关系，一律不许发明。\n"
            "6. 只输出 JSON，不要解释、不要 Markdown 代码块。"
        )
        scene_lines = "\n".join(f"- {item[1]}" for item in picked)
        prompt = (
            "# 角色卡\n"
            f"{str(persona_text or '').strip()[:6000]}\n\n"
            f"# 称呼设置\n{pronoun}"
            + (f"｜名字：{name}" if str(name or "").strip() else "")
            + "\n\n# 场景\n每个场景给 3 个候选。\n"
            f"{scene_lines}\n\n"
            "# 输出格式（只输出 JSON）\n"
            "{\n"
            '  "scenes": [\n'
            '    {"scene": "被撩", "candidates": [\n'
            '      {"text": "台词本身", "move": "这一条用的什么走法", "why": "10 字以内"}\n'
            "    ]}\n"
            "  ]\n"
            "}\n"
            "scene 用上面场景的第一个词（被撩 / 被怼 / 被冷落 / 深夜、累了、想睡 / 群里被点名 / "
            "对方让她做她做不到的事 / 越界请求 / 她自己正在忙 / 对方夸别人没夸她 / 对方难过，需要安慰）。"
        )
        return system, prompt

    def samples_layer(
        self, samples: list[dict[str, Any]] | None, *, pronoun: str = "她"
    ) -> str:
        """「{称呼}平时怎么说话」那一段（没样例就整段不出现）。"""

        lines = [
            str((item or {}).get("text") or "").strip()
            for item in (samples or [])
            if isinstance(item, dict)
        ]
        lines = [item for item in lines if item]
        if not lines:
            return ""
        body = "\n".join(f"- 「{item}」" for item in lines)
        return (
            f"# {pronoun}平时怎么说话（只学口吻，不是台词）\n\n"
            f"下面几句是「{pronoun}自己」说过的话，放在这里只是让你找回语气：\n\n"
            f"{body}\n\n"
            "**这些是口吻样本，不是这一轮要说的台词**：说什么由你自己决定。\n"
            "不要照抄、不要改写它们的句子发出去；也不要因为这里写着几句就每轮都说这么长。"
        )

    def open_topics_layer(
        self,
        topics: list[dict[str, Any]] | None,
        *,
        who_name: str = "",
        pronoun: str = "她",
    ) -> str:
        """「还没聊完的」那一段：他之前说过、还没结果的事（没有就整段不出现）。"""

        rows: list[str] = []
        for item in topics or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            name = str(item.get("who_name") or "").strip()
            asked = int(item.get("asked") or 0)
            when = self._said_when(item.get("at"))
            label = name if name and name != who_name else ""
            if when:
                label = f"{label}（{when}）" if label else f"（{when}）"
            prefix = f"{label}：" if label else ""
            hint = "（你已经问过一次了，别催）" if asked else ""
            rows.append(f"- {prefix}{text}{hint}")
        if not rows:
            return ""
        return (
            "# 还没聊完的\n"
            "这些是他之前说过、到现在还没结果的事。合适就自然地接一句问一下——"
            "**不要像查户口那样逐条问**，也不要硬转话题；不合适就这轮不提。\n"
            "后面括号里是**他说这句话的日期**：句子里写的「明天 / 后天 / 下周」都是"
            "**按那一天算的**，现在可能早就过了——过期的别照着原话问，"
            "改问结果（「体检结果出来了吗」），或者这轮就不提。\n"
            + "\n".join(rows[:4])
        )

    def _said_when(self, at: Any) -> str:
        """「09-25 说的，3 天前」这种时间说明；今天说的就写「今天说的」。

        没有这个，一句「他明天要去体检」挂三天后还是"明天"，
        她会去问一件早就过完的事。
        """

        try:
            stamp = float(at or 0.0)
        except (TypeError, ValueError):
            return ""
        if stamp <= 0:
            return ""
        now = self._now()
        if now is None:
            # 拿不到"现在"就没法算多久前：宁可不说，也不写一个可能差一天的日期
            return ""
        try:
            # 从"现在"往前推，跟着它的时区走，不另开一套本地时区（跨零点会差一天）
            said = now - timedelta(seconds=max(0.0, now.timestamp() - stamp))
        except (OSError, OverflowError, ValueError):
            return ""
        days = (now.date() - said.date()).days
        if days <= 0:
            return "今天说的"
        if days == 1:
            return "昨天说的"
        return f"{said.strftime('%m-%d')} 说的，{days} 天前"

    def build_schedule_gate_prompt(
        self,
        *,
        persona_text: str,
        schedule_text: str,
        event_line: str,
        state_line: str,
    ) -> tuple[str, str]:
        """日程撞上事件时的三选一（返回 system, user）。

        只在"这条日程是睡觉/小睡/换地方"而且她手上确实有件事件时才问，所以调用量很小。
        """

        persona = (persona_text or "").strip()
        system = (
            (persona + "\n\n" if persona else "")
            + "你正忙着处理一件事，这时候到了日程上安排的时间。\n"
            "决定：照常去做、往后推一会儿、还是今天这条就算了。\n"
            "只输出一个 JSON 对象，不要解释、不要 Markdown。\n"
            "输出格式：\n"
            '{"choice": "do|delay|cancel", "reason": "一句为什么（给自己看的）"}\n'
            "判断时按你自己的性格和眼前这件事的分量来：\n"
            "- 事情危险或者正卡在关键处 → 往后推（delay），别去睡觉也别走开；\n"
            "- 只是有点麻烦、并不紧急 → 到点了就照常做（do）；\n"
            "- 今天这条本来就可有可无（例如「再刷会儿手机」）→ 今天算了（cancel）；\n"
            "- 真的困到不行就先去睡（do），别硬撑——熬夜要付代价的。"
        )
        prompt = (
            f"# 你手上这件事\n{event_line}\n\n"
            f"# 日程到点了\n{schedule_text}\n\n"
            + (f"# 你此刻\n{state_line}\n\n" if state_line else "")
            + "只输出 JSON。"
        )
        return system, prompt


def _short(detail: Any, limit: int = 60) -> str:
    text = str(detail)
    return text if len(text) <= limit else text[:limit] + "…"


def _affect_hint(affect: float) -> str:
    """把心潮数值翻译成给大模型看的程度描述。"""

    value = float(affect or 0.0)
    if value >= 0.8:
        return "情绪翻涌，几乎压不住"
    if value >= 0.6:
        return "明显被勾起情绪"
    if value >= 0.4:
        return "心里有点起伏"
    if value >= 0.2:
        return "比较平静"
    return "情绪很淡，不太想表达"


def _valence_hint(valence: float) -> str:
    """把效价数值翻译成给大模型看的方向描述。"""

    value = float(valence if valence is not None else 0.5)
    if value >= 0.7:
        return "心情很好"
    if value >= 0.58:
        return "心情偏好"
    if value > 0.42:
        return "心情一般"
    if value > 0.3:
        return "心情有点差"
    return "心情很差"
