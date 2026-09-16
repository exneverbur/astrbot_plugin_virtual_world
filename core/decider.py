"""决策器（设计文档 4.5 / 4.4.10 节）。

三种决策源：规则（不调 LLM，覆盖大多数自主行为）、日程（由 ScheduleRunner 处理）、
LLM（复杂情境，低频抽样）。这里只负责「规则决策」和「是否该问 LLM」。
"""

from __future__ import annotations

import random
import time
from typing import Any

from .models import WorldConfig
from .pathfinding import find_path
from .planner import active_plan, create_plan
from .state import WorldState

# 「回复意愿」各项权重：越孤独、越无聊、心潮越高，越想说点什么；
# 累、正在睡、刚被冷落都会把意愿压下去。
WILLINGNESS_WEIGHTS = {
    "loneliness": 0.45,
    "boredom": 0.20,
    "curiosity": 0.10,
    "affect": 0.15,
    "tired": 0.35,
    "unanswered": 0.25,
}

# 插话的两条动机（详见 rule_plan 的第 0 条）：
# - A「想被注意到」：孤独感高、而且心情不太差时才愿意主动social；
# - B「想发作」：情绪上来了、心情又差，会憋不住呛一句。
INTERJECT_SOCIAL_VALENCE_FLOOR = 0.35
INTERJECT_VENT_AROUSAL = 0.6
INTERJECT_THRESHOLD_BUMP_MAX = 0.10
INTERJECT_THRESHOLD_BUMP_CLOSED = 0.15

# 心情低落时，自我调节类动作（发呆、看书）的门槛下调：
# 安全阀不是"触发一个动作"，而是让她的行为自然偏向自我修复。
SELF_CARE_VALENCE = 0.4
SELF_CARE_BOREDOM = 0.55
SELF_CARE_IDLE = 0.3


def reply_willingness(state: WorldState, weights: dict[str, float] | None = None) -> float:
    """她对「现在开口说话」的整体意愿（0~1），供本插件的决策与外部联动共用。"""

    w = dict(WILLINGNESS_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})
    if state.state in ("sleeping", "napping"):
        return 0.0
    score = (
        w["loneliness"] * float(state.loneliness)
        + w["boredom"] * float(state.boredom)
        + w["curiosity"] * float(state.curiosity)
        + w["affect"] * float(state.affect)
        - w["tired"] * (1.0 - float(state.energy))
    )
    if int(state.unanswered_count or 0) > 0:
        score -= w["unanswered"] * min(1.0, 0.5 + 0.25 * int(state.unanswered_count))
    return max(0.0, min(1.0, score))


class Decider:
    """规则决策器。"""

    def __init__(
        self,
        world: WorldConfig,
        rng: random.Random | None = None,
        *,
        now_provider: Any = None,
    ) -> None:
        self.world = world
        self.rng = rng or random.Random()
        self._now_provider = now_provider

    def _now(self) -> float:
        if self._now_provider is None:
            return time.time()
        try:
            return float(self._now_provider())
        except Exception:
            return time.time()

    def set_world(self, world: WorldConfig) -> None:
        self.world = world

    # ---------------- 是否需要新计划 ----------------

    def needs_plan(self, state: WorldState) -> bool:
        active = active_plan(state)
        if active is not None:
            return False
        if state.current_action:
            return False
        return True

    def llm_sample_rate(self, state: WorldState) -> float:
        """问大模型的概率：在她「决策意愿」的 0~1 之间，按配置的上下限线性插值。

        意愿本身就来自那几个数值（孤独/无聊/好奇/心潮，再扣掉疲惫和被冷落），
        所以她是"闲得慌"还是"刚忙完很平静"，落到这里的概率是不一样的。
        """

        config = self.world.decider
        low = float(config.llm_rate_min)
        high = float(config.llm_rate_max)
        if low > high:
            low, high = high, low
        low = max(0.0, min(1.0, low))
        high = max(0.0, min(1.0, high))
        willingness = reply_willingness(state)
        return max(0.0, min(1.0, low + (high - low) * willingness))

    def should_ask_llm(self, state: WorldState) -> bool:
        """这一次评估要不要交给大模型安排。

        规则决策不受这个概率影响——规则命中就直接执行；这里决定的是
        「要不要额外问一次大模型」。没被抽中、规则又给不出计划时，这一轮她就自己待着。
        """

        return self.rng.random() < self.llm_sample_rate(state)

    # ---------------- 规则决策 ----------------

    def rule_plan(
        self,
        state: WorldState,
        *,
        group_chatting: bool = False,
        interject_allowed: bool = True,
    ) -> dict[str, Any] | None:
        """按优先级给出一段计划；无法判断时返回 None。

        ``group_chatting``：群里最近是否有人在聊（由引擎提供）。
        ``interject_allowed``：插话冷却与上限是否允许（由引擎提供）。
        """

        node_id = state.node_id
        graph = self.world.adjacent()
        home = self.world.default_node_id()

        # 0) 群里正热闹 -> 主动插一句。两条动机分开算：
        #    A「想被注意到」：孤独感够了、心情也不太差才愿意主动social；
        #    B「想发作」：情绪上来了、心情又差，会憋不住呛一句（形态由风格格决定）。
        #    孤独感管"想不想说话"，心潮/效价管"说成什么样"。
        if self.world.decider.enabled and interject_allowed and group_chatting:
            motive = self.interject_motive(state)
            if motive:
                return create_plan(
                    steps=[{"action": "say", "interject": True}],
                    world_time=state.world_time,
                    valid_for=min(600, self._plan_valid()),
                    reason=motive,
                    source="rule",
                )

        # 1) 精力过低 -> 回卧室睡觉（刚被叫醒的保护期里不安排，免得叫醒几分钟又被抓回去睡）
        if state.energy < 0.25 and state.world_time >= state.no_sleep_until:
            steps = self._travel_then(node_id, home, graph)
            steps.append({"action": "sleep", "duration": self._action_duration("sleep")})
            return create_plan(
                steps=steps,
                world_time=state.world_time,
                valid_for=self._plan_valid(),
                reason="精力过低，该休息了",
                source="rule",
            )

        # 2) 孤独感过高 -> 去大厅找人说话
        if state.loneliness > 0.7:
            steps = self._travel_then(node_id, "lobby", graph)
            steps.append({"action": "say"})
            return create_plan(
                steps=steps,
                world_time=state.world_time,
                valid_for=self._plan_valid(),
                reason="孤独感偏高，想找人说话",
                source="rule",
            )

        # 3) 好奇心高且在书房 -> 上网搜索并分享
        if state.curiosity > 0.7 and node_id == "study" and self.action_usable("search_web"):
            return create_plan(
                steps=[{"action": "search_web", "params": {}}],
                world_time=state.world_time,
                valid_for=self._plan_valid(),
                reason="好奇心高，想查点东西",
                source="rule",
            )

        # 心情低落时，自我调节类动作的门槛下调：她更容易选择"缓一缓"，
        # 而不是等着谁来看穿她（安全阀是行为倾向，不是触发一个动作）。
        low_mood = float(state.valence) < SELF_CARE_VALENCE

        # 4) 无聊 -> 换个地方发呆
        if state.boredom > (SELF_CARE_BOREDOM if low_mood else 0.8):
            target = self._pick_idle_node(exclude=node_id)
            steps = self._travel_then(node_id, target, graph) if target else []
            steps.append({"action": "stare"})
            return create_plan(
                steps=steps,
                world_time=state.world_time,
                valid_for=self._plan_valid(),
                reason="太无聊了，换个环境",
                source="rule",
            )

        # 5) 精力尚可且是白天 -> 去书房看书
        if (
            state.energy > 0.5
            and state.boredom > (SELF_CARE_IDLE if low_mood else 0.45)
            and node_id != "study"
        ):
            steps = self._travel_then(node_id, "study", graph)
            steps.append({"action": "read", "duration": self._action_duration("read")})
            return create_plan(
                steps=steps,
                world_time=state.world_time,
                valid_for=self._plan_valid(),
                reason="有点闲，去看会儿书",
                source="rule",
            )

        return None

    def forced_plan(self, state: WorldState, flag: str) -> dict[str, Any] | None:
        """极端保护触发的计划。"""

        graph = self.world.adjacent()
        home = self.world.default_node_id()
        if flag == "force_sleep":
            steps = self._travel_then(state.node_id, home, graph)
            steps.append({"action": "sleep", "duration": self._action_duration("sleep")})
            return create_plan(
                steps=steps,
                world_time=state.world_time,
                valid_for=self._plan_valid(),
                reason="精力透支，强制休息",
                source="extreme",
            )
        if flag == "force_reach_out":
            steps = self._travel_then(state.node_id, "lobby", graph)
            steps.append({"action": "say"})
            return create_plan(
                steps=steps,
                world_time=state.world_time,
                valid_for=self._plan_valid(),
                reason="太久没和人说话了",
                source="extreme",
            )
        if flag == "need_change":
            target = self._pick_idle_node(exclude=state.node_id)
            if target:
                steps = self._travel_then(state.node_id, target, graph)
                steps.append({"action": "stare"})
                return create_plan(
                    steps=steps,
                    world_time=state.world_time,
                    valid_for=self._plan_valid(),
                    reason="想换换环境",
                    source="extreme",
                )
        return None

    # ---------------- 工具 ----------------

    def _travel_then(
        self, current: str, target: str, graph: dict[str, list[tuple[str, int]]]
    ) -> list[dict[str, Any]]:
        if not target or current == target:
            return []
        if find_path(graph, current, target) is None:
            return []
        return [{"action": "walk_to", "target_node": target}]

    def _pick_idle_node(self, *, exclude: str = "") -> str:
        """挑一个能发呆的地方：动作归属只认动作那一份（不再看节点自己的列表）。"""

        candidates = [
            node.id
            for node in self.world.nodes
            if node.id != exclude
            and any(action.id == "stare" for action in self.world.actions_in(node.id))
        ]
        if not candidates:
            candidates = [n.id for n in self.world.nodes if n.id != exclude]
        if not candidates:
            return ""
        return self.rng.choice(candidates)

    def _action_duration(self, action_id: str) -> int:
        action = self.world.action_map().get(action_id)
        return int(action.duration) if action else 0

    def _plan_valid(self) -> int:
        return int(self.world.limits.plan_valid_duration)

    def has_tool(self, name: str) -> bool:
        """她能不能用到这个工具：绑了它的动作存在，或者它是全局通用工具。"""

        wanted = str(name or "").strip()
        if not wanted:
            return False
        if wanted in {str(item).strip() for item in (self.world.global_allowed_tools or [])}:
            return True
        return any(
            action.llm_level == "tool" and wanted in action.tool_list()
            for action in self.world.actions
        )

    def action_usable(self, action_id: str) -> bool:
        """这个动作现在真的跑得起来吗（存在、没停用、需要工具时至少配了一个）。"""

        action = self.world.action_map().get(str(action_id or "").strip())
        if action is None or not action.enabled:
            return False
        if action.llm_level == "tool":
            return bool(action.tool_list())
        return True

    # ---------------- 插话 ----------------

    def interject_threshold(self, state: WorldState) -> float:
        """这一刻要多少孤独感才愿意主动接话。

        **浮动阈值**（不是浮动意愿值）：心情差的时候更难开口，但一旦开口，
        说成什么样仍归风格格管——两件事不混在一起。
        """

        base = float(self.world.decider.interject_threshold)
        bump = max(
            0.0,
            min(
                INTERJECT_THRESHOLD_BUMP_MAX,
                (0.5 - float(state.valence)) / 3.0,
            ),
        )
        if self.motive_a_closed(state):
            # 长期低落：动机 A 关闭，而且至少关 15 分钟（避免阈值边上反复抖）
            bump = INTERJECT_THRESHOLD_BUMP_CLOSED
        return base + bump

    def motive_a_closed(self, state: WorldState) -> bool:
        """「想被注意到」这条动机是不是被关掉了。"""

        return self._now() < float(state.interject_closed_until or 0.0)

    def interject_motive(self, state: WorldState) -> str:
        """她想插话的理由；不想插话时返回空字符串。"""

        valence = float(state.valence)
        if valence > INTERJECT_SOCIAL_VALENCE_FLOOR and not self.motive_a_closed(state):
            if state.loneliness >= self.interject_threshold(state):
                return "群里正聊得热闹，想接一句"
        if valence < INTERJECT_SOCIAL_VALENCE_FLOOR and float(state.affect) >= INTERJECT_VENT_AROUSAL:
            return "情绪上来了，忍不住想插一句"
        return ""
