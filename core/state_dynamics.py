"""内部状态数值演化（设计文档 4.4 节）。

纯函数式设计：输入状态与场景，输出新的数值，方便测试与复用。
"""

from __future__ import annotations

from typing import Any

from .models import NodeDef, StateDynamics as DynamicsConfig, WorldConfig
from .state import WorldState

# 事件 -> 数值影响（设计文档 4.4.6）
#
# 关于心潮（affect）：它是「情绪被激起的强度」，不是「开心程度」。
# 所以被夸、被抱会抬高它，被骂、被冷落同样会抬高它——差别体现在 mood 上。
EVENT_EFFECTS: dict[str, dict[str, float]] = {
    # 心潮是"情绪被激起的强度"，普通闲聊只该让她有一点起伏；
    # 真正的情绪事件（被夸/被骂/被抱）才给明显加成。见 affect_saturation。
    "mention_bot": {"affect": 0.06, "loneliness": -0.05},
    "positive_words": {"loneliness": -0.15, "affect": 0.15},
    "negative_words": {"loneliness": 0.10, "affect": 0.18},
    "hug_bot": {"loneliness": -0.20, "affect": 0.22},
    "group_lively": {"affect": 0.03, "loneliness": -0.05},
    "group_quiet": {"loneliness": 0.05, "boredom": 0.10},
    "user_joined": {"curiosity": 0.10},
    "user_left": {"loneliness": 0.05},
    "topic_engaged": {"affect": 0.02, "boredom": -0.05},
    "ignored": {"loneliness": 0.05, "boredom": 0.05, "affect": 0.04},
}

# 边际递减：心潮越高，同样的刺激能加进去的越少（最低保留 25% 效力）。
# 没有这一层的话，一两轮聊天就能顶到满值，之后一直卡在"难以平静"。
AFFECT_SATURATION_FLOOR = 0.25


def affect_saturation(affect: float) -> float:
    """按当前心潮算出「这一下还能加成多少」的系数。"""

    value = max(0.0, min(1.0, float(affect)))
    return max(AFFECT_SATURATION_FLOOR, 1.0 - 0.75 * value)

MOOD_SLEEPY = "困倦"
MOOD_MISSING = "想念"
MOOD_HAPPY = "开心"
MOOD_CURIOUS = "好奇"
MOOD_BORED = "无聊"
MOOD_CALM = "平静"
MOOD_STIRRED = "心潮起伏"
MOOD_INTENSE = "难以平静"


class StateDynamics:
    """数值演化器。"""

    def __init__(self, config: DynamicsConfig | None = None) -> None:
        self.config = config or DynamicsConfig()

    # ---------------- 自然演化 ----------------

    def tick(
        self,
        state: WorldState,
        *,
        node: NodeDef | None,
        elapsed_seconds: float,
        world: WorldConfig | None = None,
    ) -> None:
        """按经过的秒数演化数值。tick 间隔由调用方换算成秒传入。"""

        minutes = max(0.0, float(elapsed_seconds)) / 60.0
        if minutes <= 0:
            return
        atmosphere = node.atmosphere if node else None
        mult = self.config.atmosphere_multiplier

        if state.state == "sleeping":
            state.energy += self.config.sleep_energy_recovery_per_min * minutes
            self._clamp(state)
            return
        if state.state == "napping":
            state.energy += self.config.nap_energy_recovery_per_min * minutes
            self._clamp(state)
            return

        calm = atmosphere.calm if atmosphere else 0.0
        liveliness = atmosphere.liveliness if atmosphere else 0.0
        lonely_air = atmosphere.loneliness if atmosphere else 0.0
        curious_air = atmosphere.curiosity if atmosphere else 0.0

        # 精力：基础衰减；安静环境恢复更快
        energy_delta = -self.config.energy_decay_per_min * minutes
        if calm > 0:
            energy_delta += self.config.energy_decay_per_min * calm * mult * minutes
        if state.state == "walking":
            energy_delta *= 1.2
        state.energy += energy_delta

        # 孤独：基础增长；窗边发呆加速；氛围调制
        lonely_rate = self.config.loneliness_growth_per_min
        lonely_rate *= 1 + lonely_air * mult
        if state.state == "staring":
            lonely_rate *= 1.5
        state.loneliness += lonely_rate * minutes

        # 好奇：基础增长；氛围调制；搜索时消耗
        curiosity_rate = self.config.curiosity_growth_per_min
        curiosity_rate *= 1 + curious_air * mult
        state.curiosity += curiosity_rate * minutes
        if state.state == "searching":
            state.curiosity -= 0.002 * minutes
            state.boredom -= 0.003 * minutes

        # 心潮：随时间回落；热闹/私密的氛围让她更难平静
        intimate = atmosphere.intimacy if atmosphere else 0.0
        affect_rate = self.config.affect_decay_per_min
        if liveliness > 0:
            affect_rate -= self.config.affect_decay_per_min * liveliness * 1.5 * mult
        if intimate > 0:
            affect_rate -= self.config.affect_decay_per_min * intimate * mult
        state.affect -= affect_rate * minutes

        # 无聊：基础增长；安静环境增长变慢；热闹环境下降
        boredom_rate = self.config.boredom_growth_per_min
        boredom_rate *= 1 - calm * mult * 0.5
        boredom_rate *= 1 - liveliness * mult * 0.5
        state.boredom += boredom_rate * minutes

        self._clamp(state)
        state.mood = self.derive_mood(state)

    # ---------------- 事件影响 ----------------

    def apply_event(self, state: WorldState, kind: str, magnitude: float = 1.0) -> None:
        effects = EVENT_EFFECTS.get(kind)
        if not effects:
            return
        for field, delta in effects.items():
            value = delta * float(magnitude)
            if field == "affect" and value > 0:
                value *= affect_saturation(state.affect)
            current = float(getattr(state, field, 0.0))
            setattr(state, field, current + value)
        self._clamp(state)
        state.mood = self.derive_mood(state)

    # ---------------- 动作效果 ----------------

    def apply_effects(
        self,
        state: WorldState,
        effects: dict[str, Any],
        *,
        world: WorldConfig | None = None,
        scale: float = 1.0,
    ) -> None:
        """套用动作定义的 on_complete.effects。

        支持格式：``"+0.8"``、``"-0.3"``、``"=0.5"``、``"×1.5"``/``"*1.5"``、``"mood:温柔"``。

        ``scale`` 用于「按持续时长缩放」的效果（每持续 1 分钟 +0.002）：
        只对增减类（``+`` / ``-``）生效，设值（``=``）与倍数（``×``）不受影响。
        """

        for field, raw in (effects or {}).items():
            text = str(raw).strip()
            if not text:
                continue
            if field == "mood" or text.startswith("mood:"):
                value = text.split(":", 1)[1] if ":" in text else text
                state.mood = value
                duration = (
                    world.state_dynamics.mood_override_duration
                    if world
                    else self.config.mood_override_duration
                )
                state.mood_override_until = state.world_time + max(1, int(duration))
                continue
            if not hasattr(state, field):
                continue
            current = float(getattr(state, field))
            if text.startswith("="):
                new_value = _to_float(text[1:], current)
            elif text.startswith("×") or text.startswith("*"):
                new_value = current * _to_float(text[1:], 1.0)
            elif text.startswith("+"):
                new_value = current + _to_float(text[1:], 0.0) * scale
            elif text.startswith("-"):
                new_value = current - _to_float(text[1:], 0.0) * scale
            else:
                new_value = _to_float(text, current)
            if field == "affect" and new_value > current:
                # 动作带来的情绪同样是边际递减的：已经很激动时，抱一下也加不了多少
                new_value = current + (new_value - current) * affect_saturation(current)
            setattr(state, field, new_value)
        self._clamp(state)

    # ---------------- mood ----------------

    def derive_mood(self, state: WorldState, *, world: WorldConfig | None = None) -> str:
        if state.world_time < state.mood_override_until:
            return state.mood
        if state.energy < 0.3:
            return MOOD_SLEEPY
        # 心潮是"情绪有多强"，最强的时候压过其它状态
        if state.affect > 0.75:
            return MOOD_INTENSE
        if state.affect > 0.55:
            return MOOD_STIRRED
        if state.loneliness > 0.7:
            return MOOD_MISSING
        if state.curiosity > 0.7:
            return MOOD_CURIOUS
        if state.boredom > 0.7:
            return MOOD_BORED
        return MOOD_CALM

    # ---------------- 极端保护 ----------------

    def check_extremes(
        self, state: WorldState, *, low_energy_ticks: int, high_loneliness_ticks: int
    ) -> list[str]:
        """返回需要强制触发的行为标记，例如 ``["force_sleep"]``。"""

        flags: list[str] = []
        if state.energy < 0.1:
            state.low_energy_since = state.low_energy_since or state.world_time
            if state.world_time - state.low_energy_since >= low_energy_ticks:
                flags.append("force_sleep")
        else:
            state.low_energy_since = 0
        if state.loneliness > 0.9:
            state.high_loneliness_since = state.high_loneliness_since or state.world_time
            if state.world_time - state.high_loneliness_since >= high_loneliness_ticks:
                flags.append("force_reach_out")
        else:
            state.high_loneliness_since = 0
        if state.curiosity <= 0.01:
            flags.append("need_change")
        return flags

    # ---------------- 工具 ----------------

    @staticmethod
    def _clamp(state: WorldState) -> None:
        state.clamp()

    def values(self, state: WorldState) -> dict[str, float]:
        return {
            "energy": round(state.energy, 4),
            "loneliness": round(state.loneliness, 4),
            "curiosity": round(state.curiosity, 4),
            "affect": round(state.affect, 4),
            "boredom": round(state.boredom, 4),
        }


def _to_float(text: str, default: float) -> float:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return default
