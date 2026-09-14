"""主动发言无人回应保护（设计文档 4.16 节）。"""

from __future__ import annotations

from dataclasses import dataclass

from .models import WorldConfig
from .state import WorldState


@dataclass
class EngagementVerdict:
    """一次评估的结论。"""

    unanswered_count: int
    in_cooldown: bool
    cooldown_ticks_left: int
    hint: str = ""

    @property
    def can_speak(self) -> bool:
        return not self.in_cooldown


class EngagementTracker:
    """跟踪「Bot 主动说话之后有没有人理」。"""

    def __init__(self, world: WorldConfig) -> None:
        self.world = world

    def set_world(self, world: WorldConfig) -> None:
        self.world = world

    # ---------------- 事件 ----------------

    def on_bot_spoke(self, state: WorldState) -> None:
        """记录一次主动发言，等待回应。"""

        state.last_engagement_time = state.world_time
        state.awaiting_reply = True
        state.add_event("bot_spoke", {"world_time": state.world_time})

    def on_user_replied(self, state: WorldState) -> None:
        """有人理她了。"""

        state.unanswered_count = 0
        state.last_engagement_time = state.world_time
        state.awaiting_reply = False
        if state.cooldown_until:
            state.cooldown_until = 0

    def note_passive_reply(self, state: WorldState, *, tick_seconds: float) -> None:
        """她刚回完一条消息：接下来一小段时间不要再主动开口。"""

        minutes = max(0, int(self.world.engagement.after_reply_cooldown_minutes))
        if minutes <= 0:
            return
        ticks = max(1, int(round(minutes * 60 / max(1.0, tick_seconds))))
        state.proactive_block_until = max(
            int(state.proactive_block_until), int(state.world_time) + ticks
        )

    def proactive_blocked(self, state: WorldState) -> bool:
        """是不是处在"刚回过话"的主动发言冷却里。"""

        return bool(state.proactive_block_until) and state.world_time < int(
            state.proactive_block_until
        )

    # ---------------- 评估 ----------------

    def evaluate(self, state: WorldState, *, tick_seconds: float) -> EngagementVerdict:
        config = self.world.engagement
        window_ticks = max(1, int(config.silence_window_minutes * 60 / max(1.0, tick_seconds)))

        # 冷却结束处理
        if state.cooldown_until and state.world_time >= state.cooldown_until:
            state.cooldown_until = 0
            if config.halve_on_cooldown_end:
                state.unanswered_count = max(0, state.unanswered_count // 2)

        # 等待回应的窗口过了还没人理 -> 计数 +1
        if state.awaiting_reply:
            waited = state.world_time - state.last_engagement_time
            if waited >= window_ticks:
                if waited < window_ticks * 2:
                    state.unanswered_count += 1
                state.last_engagement_time = state.world_time

        if not state.cooldown_until and state.unanswered_count >= config.unanswered_threshold:
            cooldown_ticks = max(
                1, int(config.cooldown_after_unanswered * 60 / max(1.0, tick_seconds))
            )
            state.cooldown_until = state.world_time + cooldown_ticks

        in_cooldown = bool(state.cooldown_until and state.world_time < state.cooldown_until)
        return EngagementVerdict(
            unanswered_count=state.unanswered_count,
            in_cooldown=in_cooldown,
            cooldown_ticks_left=max(0, state.cooldown_until - state.world_time),
            hint=self.hint(state),
        )

    def hint(self, state: WorldState) -> str:
        """给提示词用的一句话。"""

        if state.cooldown_until and state.world_time < state.cooldown_until:
            return ""
        if state.unanswered_count <= 0:
            return ""
        return (
            f"你最近连续 {state.unanswered_count} 次主动说话都没人回应。"
            "你有点犹豫，不太确定要不要再主动开口。"
            "如果这次还是没人回应，你可能需要考虑安静一会儿。"
        )

    def can_speak(self, state: WorldState) -> bool:
        return not (state.cooldown_until and state.world_time < state.cooldown_until)
