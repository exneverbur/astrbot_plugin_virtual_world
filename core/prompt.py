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

from datetime import datetime
from typing import Any

from .memory import RecalledMemory
from .models import NodeDef, WorldConfig
from .state import WorldState
from .pathfinding import travel_cost
from .tool_policy import allowed_tools

NO_MEMORY_TEXT = "（这里没有特别让你想起什么）"

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

_REASONING_ORDER = ("env", "state", "mood", "who", "intent")
_REASONING_LABELS = {
    "env": "在哪",
    "state": "状态",
    "mood": "心情",
    "who": "在和谁说话",
    "intent": "打算怎么办",
}


def _one_line(value: Any, limit: int = 140) -> str:
    """把任意内容压成一行：转发、引用、多段文本在提示词里都会把排版撑乱。"""

    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


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
        """记忆的日期标签：今年只写月-日，往年带上年份。"""

        if not created_at:
            return ""
        try:
            moment = datetime.fromtimestamp(created_at, tz=now.tzinfo if now else None)
        except Exception:
            return ""
        if now is not None and moment.year == now.year:
            return moment.strftime("%m-%d")
        return moment.strftime("%Y-%m-%d")

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
            "针对群友的动作可以表现出来，自己换位置、发呆、睡觉等行为默认静默。"
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

    def scene_layer(self, node: NodeDef | None, available_tools: dict[str, str]) -> str:
        if node is None:
            return "你当前不在任何已知地点。"
        actions = self.world.actions_in(node.id)
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
        return (
            f"你当前在【{where}】（id：{node.id}）。\n"
            f"{node.prompt}{zone_note}\n\n"
            f"这里的氛围：{node.atmosphere.describe()}\n\n"
            f"这一轮你能写的 type 只有：{type_line}\n\n"
            f"{exclusive_line}"
            f"你可以执行的动作（工具型动作只要说明想做什么，具体参数由系统转交）：\n"
            f"{action_lines}\n\n"
            f"{tool_lines}\n\n"
            "想做只有别处能做的事（比如在书房上网）：**先写一步 walk_to，紧接着把要做的那个动作也写进\n"
            "同一串 actions**，系统会带你走过去再执行；只写移动的话，到了那儿还得再问你一次。\n\n"
            f"# 动作前置条件\n{self.precondition_hints(node.id)}"
        )

    def _action_line(self, action: Any) -> str:
        text = f"- {action.id}：{action.description or action.name}"
        if action.llm_level == "tool":
            text += "（工具型：只填 intent，说明你想做什么；参数会自动补全）"
        if action.llm_level == "command":
            text += "（指令型：只填 intent 说清想让它干什么，参数由系统按指令说明补全）"
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
    ) -> str:
        now = self._now()
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
        events = state.recent_events[-5:]
        event_text = (
            "\n".join(self._event_line(item) for item in events)
            if events
            else "（没什么特别的事）"
        )
        thought_text = self._inner_voice(state)

        blocks = [
            "# 你的状态\n" + self._state_block(state),
            f"# 当前动作：{action_desc}",
            f"# 当前位置：{node.name if node else '未知'}",
            f"# 最近活跃的用户：{user_text}",
        ]
        if now is not None:
            # 时间紧跟在"她是谁、在哪、在干嘛"之后：
            # 这几行在相邻两次调用之间通常是稳定的，放在最前面才能让前缀缓存尽量长；
            # 时钟每分钟都变，它后面的内容（记忆、群聊…）本来就每轮都变，放这里不浪费。
            blocks.append(self._clock_line(now))
        if node is not None:
            # 可达表只跟"她在哪"有关，比状态稳、比记忆易变得多，放时钟后面
            blocks.append(self.reach_table(node.id))
        in_progress = self._in_progress_block(state)
        if in_progress:
            blocks.append(in_progress)
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
        blocks.extend(
            self.chat_blocks(
                recent_chat,
                getattr(state, "chat_summary", ""),
                getattr(state, "chat_note", ""),
            )
        )
        # 其他插件注入的内容跟着这条消息走，每轮都不一样，放到靠后的位置
        if other_context:
            blocks.append("# 其他插件提供的上下文\n" + other_context)
        if extra_notes:
            blocks.extend(note for note in extra_notes if note)
        return "\n\n".join(blocks)

    # ---------------- 第 4 层的分段构件 ----------------

    @staticmethod
    def _state_block(state: WorldState) -> str:
        """数值不只是给数字：连范围、含义、当前程度一起说清楚。"""

        lines = [
            "你的状态（每个数值都是 0~1，越大越强）：",
            f"- 心情：{state.mood}",
            f"- 精力 {state.energy:.2f}：低于 0.25 会明显想睡觉，高于 0.8 很有精神",
            f"- 孤独感 {state.loneliness:.2f}：超过 0.7 会很想找人说话，低于 0.3 觉得一个人也挺好",
            f"- 好奇心 {state.curiosity:.2f}：超过 0.7 想找点新鲜事做",
            f"- 心潮 {state.affect:.2f}：情绪被激起的强度（不是开心程度），"
            f"现在是「{_affect_hint(state.affect)}」；"
            "越高，内心活动越翻涌、说出来的感情越浓、越容易做亲昵或冲动的举动",
            f"- 无聊 {state.boredom:.2f}：超过 0.8 待不住，想换个地方；超过 0.45 想找点事做",
            "这些是你的内部感受，不要报数字，但语气、动作和用词要能体现出来。",
        ]
        return "\n".join(lines)

    def action_label(self, action_id: Any) -> str:
        action = self.world.action_map().get(str(action_id or ""))
        return (action.name or action.id) if action is not None else str(action_id or "")

    def node_label(self, node_id: Any) -> str:
        node = self.world.node_map().get(str(node_id or ""))
        return (node.name or node.id) if node is not None else str(node_id or "")

    def _event_line(self, item: dict[str, Any]) -> str:
        """把一条最近事件写成人话——原始 dict 对模型没什么用。"""

        kind = str(item.get("kind") or "")
        detail = item.get("detail") or {}
        if not isinstance(detail, dict):
            return f"- {kind}: {_short(detail, 40)}"
        if kind in ("action", "action_start"):
            return f"- {'开始' if kind == 'action_start' else '做了'}：{self.action_label(detail.get('type'))}"
        if kind == "move":
            return f"- 走到了：{self.node_label(detail.get('to'))}"
        if kind == "plan":
            return f"- 安排：{_short(detail.get('reason'), 40)}"
        if kind == "tool":
            return f"- 查了资料：{self.action_label(detail.get('action'))}"
        if kind == "chain":
            return f"- 接着执行日程：{detail.get('schedule')}"
        return f"- {kind}: {_short(detail, 40)}"

    def _in_progress_block(self, state: WorldState) -> str:
        """她手头的事：正在做什么、计划里还剩什么。

        没有这段的话，她跨轮次就像失忆：上一轮安排的事、正要接着做的事都看不到。
        """

        lines: list[str] = []
        action = state.current_action or {}
        if action:
            total = max(1, int(action.get("duration_ticks", 1) or 1))
            left = max(0, total - int(action.get("elapsed_ticks", 0) or 0))
            what = action.get("desc") or self.action_label(action.get("type"))
            lines.append(f"- 你正在做：{what}（还要约 {left} tick）")
        plan = state.current_plan or {}
        steps = plan.get("steps") or []
        current = int(plan.get("current_step", 0) or 0)
        pending = [
            self.action_label(step.get("action"))
            for step in steps[current:]
            if isinstance(step, dict) and step.get("action")
        ]
        if pending:
            line = "- 你安排好还没做的：" + "、".join(pending[:5])
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
        return "# 你手头的事（接着做，不用重新打算）\n" + "\n".join(lines)

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
    ) -> list[str]:
        """把群聊背景拆成「更早的摘要」「别人在聊」「你说过的话」几段。"""

        # 条数上限由调用方（engine.chat_context）按配置决定，这里不再二次截断，
        # 否则「最多携带多少条聊天」调大了也不会生效。
        items = list(recent_chat or [])
        if not items and not summary and not note:
            return []
        others: list[str] = []
        mine: list[str] = []
        for item in items:
            name = str(item.get("name") or item.get("user_id") or "").strip()
            text = _one_line(item.get("text"))
            if not text:
                continue
            if item.get("is_self"):
                mine.append(f"- {text}")
                continue
            identifier = str(item.get("user_id") or "").strip()
            who = f"{name}({identifier})" if name and identifier else (name or identifier)
            others.append(f"- {who}: {text}")
        blocks: list[str] = []
        if note:
            blocks.append(
                "# 刚才你们聊过（已经回应过，只是背景，不要重复回应）\n"
                f"- {_one_line(note, 80)}"
            )
        if summary:
            blocks.append(
                "# 更早的群聊（摘要，只是背景，不要复述）\n" + _one_line(summary, 400)
            )
        if others:
            blocks.append(
                "# 群里最近在聊（只是背景资料，不要逐条复述、不要总结成列表）\n"
                + "\n".join(others)
            )
        if mine:
            blocks.append(
                "# 你最近说过的话（不要重复这些句子，也不要再用同样的开头和句式）\n"
                + "\n".join(mine)
            )
        return blocks

    # ---------------- 第 3 层 ----------------

    def format_layer(
        self,
        *,
        max_messages: int = 3,
        reasoning: bool = True,
        mode: str = "actions",
    ) -> str:
        """输出格式约束。

        整段不依赖当前地点与状态，所以可以放在提示词前部当固定前缀用。
        能写哪些 type 由「当前场景」那一层给，这里只讲结构和字段规则。
        """

        reasoning_block = ""
        # 计划模式只解析 plan，没有 reasoning/memory，就别在提示里写这两段
        if reasoning and mode != "plan":
            reasoning_block = (
                "你的输出必须是 JSON，而且 **reasoning 必须写在 actions 前面**：\n\n"
                "{\n"
                '  "reasoning": {\n'
                '    "env": "你现在在哪、周围什么样（15 字以内）",\n'
                '    "state": "你此刻的状态如何（15 字以内）",\n'
                '    "mood": "你此刻的心情（8 字以内）",\n'
                '    "who": "读完上面的对话，你在跟谁说话、他在要什么（20 字以内）",\n'
                '    "intent": "你打算怎么回应（20 字以内）"\n'
                "  },\n"
                '  "memory": "这次对话值得记住的一句话（20 字以内，以你的视角）",\n'
                '  "chat_note": "刚才你们在聊什么（20 字以内，只在这条消息带群聊背景时才写）",\n'
                '  "actions": [ ... ]\n'
                "}\n\n"
                "关于 reasoning：\n"
                "- 它是你动笔前的草稿，**永远不会发到群里**，也不计入动作数量；\n"
                "- 必须先写、写短、写具体（要引用你上面看到的环境/状态/对话），不要写空话；\n"
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
            )
        if mode == "plan":
            body = (
                "这一轮要输出的是计划，格式如下：\n\n"
                "{\n"
                '  "plan": [\n'
                '    { "action": "walk_to", "target_node": "window" },\n'
                '    { "action": "stare", "duration": 600 },\n'
                '    { "action": "think", "content": "内心活动" }\n'
                "  ],\n"
                '  "valid_until": 1800,\n'
                '  "reason": "为什么这样安排"\n'
                "}\n\n"
                "字段说明：\n"
                "- plan：按先后顺序排的步骤，每步的 action 是动作 id，"
                "需要去哪就写 target_node，持续动作写 duration（秒）。\n"
                "- valid_until：这份计划大约管多少秒。\n"
                "- reason：一句话说明为什么这样安排。\n\n"
                "规则：\n"
                "1. action 只能用当前场景里列出的动作 id（写别的会被丢掉）。\n"
                "2. 想做只有别处能做的事，就把 walk_to 写成计划的第一步。\n"
                "3. 不要输出解释、Markdown 或代码块，只输出 JSON。"
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
            '    { "type": "search_web", "intent": "查一下今天有什么科技新闻" }\n'
            "  ],\n"
            '  "cancel": "只有对方明确要求你停下 / 别做了 / 改主意时才写，写了就会立刻生效：'
            'now = 立刻停手并放弃剩下的安排，queue = 手上这件做完但不要再按原计划走；'
            '其它情况这一项不要出现"\n'
            "}\n\n"
            "字段说明：\n"
            f"- say：只填 messages，是发到群聊的文本，最多 {max_messages} 条。\n"
            "- 针对某个人的动作：只填 target，值是群友 ID。\n"
            "- walk_to：只填 target_node（地点 ID）或 target（群友 ID）。\n"
            "- think：只填 content，是内心活动，不会发到群里。\n"
            "- 工具型动作：只填 intent，用一句自然语言说清你想做什么；"
            "不要自己编参数，系统会按工具定义自动补全。\n\n"
            "规则：\n"
            "1. 只能从「当前场景」那一层列出的动作里选 type，写别的会被丢弃。\n"
            "2. 工具型动作必须填 intent（想做什么），不要填 params。\n"
            "3. say 的 messages 是直接发到群聊的最终文本，简短自然，不要写「（动作）」以外的解释。\n"
            "4. think 的 content 不会发送到群聊。\n"
            "5. 针对空间或自己的动作静默执行，不发消息。\n"
            "6. 想做当前地点做不了的事，**必须写两步**：先 walk_to，紧接着把要做的那件事也写进同一串\n"
            '   actions，例如 [{"type":"walk_to","target_node":"study"},\n'
            '   {"type":"search_web","intent":"查今天的新闻"}]。只写移动不算安排——系统会带你过去，\n'
            "   但不会替你决定到了之后做什么，那会多花一次调用。\n"
            "7. 你在 say 里承诺了要做什么，就必须把对应的动作也写进 actions——只说不动等于没做。\n"
            "8. 标着「只有在这个地点才能做」的动作是这里的特色：人在的时候就顺手挑一件，"
            "但不要每次都做同一件，也别重复最近刚做过的。\n"
            "9. 这次行动如果指向某个具体的人（踢谁、私聊谁、给谁画画像），"
            "**必须把对方的 id 一起写进 intent**（例如「把 123456 这个刷屏的踢掉」），"
            "能填 target 的就顺手填上；只写昵称系统不一定认得。\n"
            "10. 手头正在做的事不用等：你随时可以安排接下来的动作，插进来的安排会排队，"
            "等手上这件事做完自动接着做。只有对方明确说「别做了 / 不用了 / 停」时才写 cancel。\n"
            "11. 不要输出解释、Markdown 或代码块，只输出 JSON。"
        )

    @staticmethod
    def reminder_layer(mode: str = "actions") -> str:
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
            lines.append("3. 计划里要说话时，也别重复你最近说过的句子和句式。")
        else:
            lines.append("2. 要写 reasoning 就先写 reasoning，再写 actions；")
            lines.append("3. 不要重复你最近说过的话，也不要再用同样的开头和句式。")
        return "\n".join(lines)

    # ---------------- 组装 ----------------

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
        )
        return (
            "\n\n# ===== 虚拟世界状态（这是你此刻真实的处境）=====\n"
            "你有自己的空间和生活节奏，下面是你现在的处境。"
            "自然地把它融进你的反应里（比如刚睡醒、正在忙、心情如何），"
            "但不要向用户解释这套设定、不要复述状态数值。\n\n"
            f"{scene}\n\n{runtime}\n"
            "# ===== 虚拟世界状态结束 ====="
        )

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
    ) -> str:
        """接管模式：完整五层，含 JSON 输出约束。

        ``mode`` 决定第 3 层讲的是 actions 还是 plan；两种模式共用前两层，
        所以计划决策和回复决策能吃到同一段前缀缓存。
        """

        layers = []
        persona = self.persona_layer(persona_text)
        if persona:
            layers.append(f"# ========== 第 1 层：你是谁 ==========\n{persona}")
        layers.append(f"# ========== 第 2 层：世界规则 ==========\n{self.world_layer()}")
        layers.append(
            "# ========== 第 3 层：输出格式 ==========\n"
            + self.format_layer(
                max_messages=max_messages, reasoning=reasoning, mode=mode
            )
        )
        layers.append(
            "# ========== 第 4 层：当前场景 ==========\n"
            + self.scene_layer(node, available_tools)
        )
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
            )
        )
        layers.append(self.reminder_layer(mode))
        return "\n\n".join(layers)

    def build_reply_user_prompt(
        self,
        *,
        user_name: str,
        text: str,
        is_private: bool = False,
    ) -> str:
        """被 @（或其他插件放行）时的用户消息包装。"""

        who = user_name or "群友"
        parts = [f"{who} 对你说：", text]
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

    def build_reply_followup_prompt(self, hint: str, tool_result: str) -> str:
        return (
            "你刚才做了一件事，这是结果：\n"
            f"{tool_result}\n\n"
            f"{hint or '用你自己的话简短地说说这件事。'}\n\n"
            "说话时的分寸：\n"
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
            "4. 不要给坐标（位置由编辑器自动安排）。\n\n"
            "# 输出格式（只输出这个 JSON）\n"
            "{\n"
            '  "nodes": [\n'
            "    {\n"
            '      "id": "英文小写下划线，例如 balcony",\n'
            '      "name": "中文名，例如 阳台",\n'
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
                ' "auto_travel": true}\n\n'
                "说明：\n"
                "- days 不写就是每天；只挑一次就写对应星期；\n"
                "- 动作链按先后顺序；只有别处能做的动作，前面加一步 walk_to；\n"
                "- 说话类动作可以带 content（说什么）。"
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
            + "\n\n请输出参数字典（JSON）。"
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
