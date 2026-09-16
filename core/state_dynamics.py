"""内部状态数值演化（设计文档 4.4 节）。

纯函数式设计：输入状态与场景，输出新的数值，方便测试与复用。

情绪两轴
--------

- **心潮（arousal）**：情绪被激起的强度，不是开心程度。
- **效价（valence）**：心情的好坏，0.5 是中性。

两者都朝各自的「基线」回落：心潮像一根被拨动的弦，会被事件拨高、然后回到
静止位置；效价则是「基线 + 一个短期偏移」，偏移随事件产生、随时间归零。
基线只跟五维状态和时段有关，本身不存历史——它慢，是因为五维本来就慢。

时间基准
--------

五维（精力 / 孤独 / 好奇 / 无聊）按固定的 tick 长度推进；情绪两轴按**真实时间**
结算（懒衰减）：事件到来前先把「上次结算到现在」的衰减补上，再叠加脉冲。
这样 tick 从 60 秒改成 5 分钟不会改变曲线形状，宿主卡顿也不会让衰减少算。
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any, Callable

from .models import NodeDef, StateDynamics as DynamicsConfig, WorldConfig
from .mood import mood_label
from .state import WorldState

# ---------------- 事件表 ----------------
#
# 「事件只负责拉高，不负责维持」：这里的数值是**脉冲**，衰减由 _sync_emotions 负责。
# `_tone`：这条事件对心情来说是好事(+1)、坏事(-1)还是只是被注意到(0)。
# 心潮的加成会按「当时的效价 × tone」做不对称——心情好时更容易被逗乐，
# 心情差时更容易被惹毛（见 valence_asymmetry）。
EVENT_EFFECTS: dict[str, dict[str, float]] = {
    "mention_bot": {"affect": 0.045, "loneliness": -0.05, "_tone": 0.0},
    "positive_words": {
        "affect": 0.10,
        "valence": 0.06,
        "loneliness": -0.15,
        "_tone": 1.0,
    },
    "negative_words": {
        "affect": 0.13,
        "valence": -0.08,
        "loneliness": 0.10,
        "_tone": -1.0,
    },
    "hug_bot": {"affect": 0.15, "valence": 0.07, "loneliness": -0.20, "_tone": 1.0},
    "group_lively": {"affect": 0.02, "loneliness": -0.05, "_tone": 0.0},
    "group_quiet": {"loneliness": 0.05, "boredom": 0.10},
    "user_joined": {"curiosity": 0.10},
    "user_left": {"loneliness": 0.05},
    "topic_engaged": {"affect": 0.015, "boredom": -0.05, "_tone": 0.0},
    # 被冷落只压心情，不再抬高心潮：否则她会从"退缩"直接跳成"发作"，
    # 群里看到的就是"刚被无视完突然开始阴阳怪气"。
    "ignored": {"valence": -0.08, "loneliness": 0.05, "boredom": 0.05, "_tone": -1.0},
    # 生活事件：工具、日程、发送这些线上一眼看不见的挫折与顺利
    "tool_failed": {"affect": 0.07, "valence": -0.05, "_tone": -1.0},
    "interrupted": {"affect": 0.08, "valence": -0.06, "_tone": -1.0},
    "schedule_done": {"affect": 0.02, "valence": 0.03, "_tone": 1.0},
    "send_failed": {"affect": 0.04, "valence": -0.03, "_tone": -1.0},
}

# 边际递减：已经很激动时，同样的刺激能加进去的更少（最低保留 25% 效力）。
# 没有这一层的话，一两轮聊天就能把心潮顶到满值，之后一直卡在"难以平静"。
AFFECT_SATURATION_FLOOR = 0.25
VALENCE_SATURATION_FLOOR = 0.25

# ---------------- 情绪两轴的基线 / 衰减 ----------------

AROUSAL_BASE = 0.30
"""心潮的静止位置：不吵不闹时的那根弦。"""

AROUSAL_ENERGY_WEIGHT = 0.25
"""精力越低，心潮基线整体下移——累了就不容易被打动。"""

VALENCE_BASE = 0.5
VALENCE_ENERGY_WEIGHT = 0.30
VALENCE_LONELINESS_WEIGHT = 0.30
VALENCE_BOREDOM_WEIGHT = 0.25
VALENCE_CURIOSITY_WEIGHT = 0.15
VALENCE_HOUR_WEIGHT = 0.4
"""以上几项决定效价基线：精力高偏正，孤独 / 无聊 / 好奇长期没被满足偏负。"""

VALENCE_DECAY_PER_MIN = 0.010
"""效价偏移每分钟回落的比例系数（指数衰减，永远朝基线）。"""

COUPLING_CAP = 0.5
"""两轴互相影响的上限：倍率夹在 0.5~1.5，避免情绪雪崩。"""

STORM_ENTER_AROUSAL = 0.7
STORM_ENTER_VALENCE = 0.35
STORM_EXIT_AROUSAL = 0.45
STORM_EXIT_VALENCE = 0.5
"""「正在气头上」标记的滞回区间：进得难、出得也难，避免来回抖。"""

STORM_MAX_MINUTES = 120
"""标记最长持续这么久，防止事件不断把它锁死。"""

CALM_BOOST_MAX = 1.5
"""负效价 + 高心潮时，平复速度最多加快到 2.5 倍（连续倍率，不是开关）。"""

SAFETY_VALVE_VALENCE = 0.25
SAFETY_VALVE_MINUTES = 30
"""效价持续低于这个值这么久，就把偏移减半——拉回基线，而不是硬设一个值。"""

IGNORED_RESET_MINUTES = 30
"""被冷落的递减惩罚：超过这么久没有新的冷落，连续次数重新算。"""

IGNORED_STREAK_MAGNITUDES = (1.0, 0.5, 0.0)
"""第 1/2/3 次被冷落的力度：第一次最疼，之后递减，三次之后暂时放下。"""


def clamp_value(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def affect_saturation(affect: float) -> float:
    """按当前心潮算出「这一下还能加成多少」的系数。"""

    value = clamp_value(float(affect or 0.0), 0.0, 1.0)
    return max(AFFECT_SATURATION_FLOOR, 1.0 - 0.75 * value)


def valence_saturation(valence: float) -> float:
    """效价版的边际递减：离中性越远，同样的刺激推得越少（正负对称）。"""

    value = clamp_value(float(valence or 0.5), 0.0, 1.0)
    return max(VALENCE_SATURATION_FLOOR, 1.0 - 0.75 * abs(2.0 * value - 1.0))


def valence_asymmetry(valence: float, tone: float) -> float:
    """心情差时更容易被惹毛、心情好时更容易被逗乐。

    ``tone`` 是事件本身的好坏方向；返回夹在 ``1 ± COUPLING_CAP`` 之间的倍率。
    """

    if not tone:
        return 1.0
    value = clamp_value(float(valence or 0.5), 0.0, 1.0)
    factor = 1.0 + COUPLING_CAP * (2.0 * value - 1.0) * float(tone)
    return clamp_value(factor, 1.0 - COUPLING_CAP, 1.0 + COUPLING_CAP)


def arousal_scale(affect: float) -> float:
    """心潮越高，效价对同一件事的反应越大（0.5~1.5 倍）。"""

    value = clamp_value(float(affect or 0.0), 0.0, 1.0)
    return clamp_value(0.5 + value, 1.0 - COUPLING_CAP, 1.0 + COUPLING_CAP)


def nonlinear_factor(gap: float) -> float:
    """离基线越远，回落越快；贴到基线附近反而更慢，避免显得情绪不稳。"""

    value = abs(float(gap or 0.0))
    if value >= 0.35:
        return 1.6
    if value >= 0.12:
        return 1.0
    return 0.6


def hour_curve(hour: int) -> float:
    """昼夜节律：清晨偏低、白天平、夜里偏高（夜聊氛围）。"""

    try:
        value = int(hour) % 24
    except (TypeError, ValueError):
        return 0.0
    if 5 <= value < 9:
        return -0.08
    if 9 <= value < 18:
        return 0.0
    if 18 <= value < 23:
        return 0.05
    return 0.08


def arousal_baseline(state: WorldState, *, hour: int | None = None) -> float:
    """心潮的静止位置：跟时段与精力有关（累了就不容易被激起）。"""

    base = AROUSAL_BASE
    if hour is not None:
        base += hour_curve(hour)
    base -= max(0.0, 0.5 - float(state.energy or 0.0)) * AROUSAL_ENERGY_WEIGHT
    return clamp_value(base, 0.05, 0.6)


def valence_baseline(state: WorldState, *, hour: int | None = None) -> float:
    """效价基线：由五维与时段推导，本身不存历史。"""

    base = VALENCE_BASE
    base += (float(state.energy or 0.0) - 0.5) * VALENCE_ENERGY_WEIGHT
    base -= max(0.0, float(state.loneliness or 0.0) - 0.4) * VALENCE_LONELINESS_WEIGHT
    base -= max(0.0, float(state.boredom or 0.0) - 0.4) * VALENCE_BOREDOM_WEIGHT
    base -= max(0.0, float(state.curiosity or 0.0) - 0.5) * VALENCE_CURIOSITY_WEIGHT
    if hour is not None:
        base += hour_curve(hour) * VALENCE_HOUR_WEIGHT
    return clamp_value(base, 0.15, 0.85)


class StateDynamics:
    """数值演化器。"""

    def __init__(
        self,
        config: DynamicsConfig | None = None,
        *,
        now_provider: Callable[[], float] | None = None,
        hour_provider: Callable[[], int] | None = None,
    ) -> None:
        self.config = config or DynamicsConfig()
        self._now_provider = now_provider
        self._hour_provider = hour_provider

    # ---------------- 时间 ----------------

    def _now(self) -> float:
        if self._now_provider is not None:
            try:
                return float(self._now_provider())
            except Exception:
                pass
        return time.time()

    def _hour(self, now: float | None = None) -> int:
        if self._hour_provider is not None:
            try:
                return int(self._hour_provider()) % 24
            except Exception:
                pass
        stamp = self._now() if now is None else float(now)
        try:
            return datetime.fromtimestamp(stamp).hour
        except (OverflowError, OSError, ValueError):
            return 12

    # ---------------- 自然演化 ----------------

    def tick(
        self,
        state: WorldState,
        *,
        node: NodeDef | None,
        elapsed_seconds: float,
        world: WorldConfig | None = None,
        now: float | None = None,
    ) -> bool:
        """按经过的秒数演化数值。tick 间隔由调用方换算成秒传入。

        返回 True 表示这一轮触发了安全阀（调用方可以据此记一条日志）。
        """

        minutes = max(0.0, float(elapsed_seconds)) / 60.0
        if minutes <= 0:
            return False
        atmosphere = node.atmosphere if node else None
        mult = self.config.atmosphere_multiplier

        if state.state in ("sleeping", "napping"):
            rate = (
                self.config.sleep_energy_recovery_per_min
                if state.state == "sleeping"
                else self.config.nap_energy_recovery_per_min
            )
            state.energy += rate * minutes
            # 睡着了也要让情绪平复：醒来时不该还揣着昨晚那口气
            reset = self._sync_emotions(state, now=now, node=node)
            self._clamp(state)
            state.mood = self.derive_mood(state)
            return reset

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

        # 无聊：基础增长；安静环境增长变慢；热闹环境下降
        boredom_rate = self.config.boredom_growth_per_min
        boredom_rate *= 1 - calm * mult * 0.5
        boredom_rate *= 1 - liveliness * mult * 0.5
        state.boredom += boredom_rate * minutes

        # 情绪两轴：按真实时间结算（tick 长度改了也不影响曲线形状）
        reset = self._sync_emotions(state, now=now, node=node)

        self._clamp(state)
        state.mood = self.derive_mood(state)
        return reset

    def _sync_emotions(
        self, state: WorldState, *, now: float | None = None, node: NodeDef | None = None
    ) -> bool:
        """把「上次结算到现在」的情绪衰减补上。

        事件到来前也要先跑一遍（懒衰减）：否则 tick 之间的那几分钟就白算了，
        而且 tick 一改长，脉冲与衰减的相对顺序就会失真。
        """

        stamp = self._now() if now is None else float(now)
        last = float(state.affect_synced_at or 0.0)
        if last <= 0:
            # 第一次观察（新会话 / 老存档升级）：从当下开始，不倒算历史
            state.affect_synced_at = stamp
            hour = self._hour(stamp)
            self._refresh(state, hour=hour)
            self._update_storm(state, now=stamp)
            return False
        minutes = (stamp - last) / 60.0
        if minutes <= 0:
            state.affect_synced_at = max(stamp, last)
            return False

        hour = self._hour(stamp)
        atmosphere = node.atmosphere if node else None
        liveliness = atmosphere.liveliness if atmosphere else 0.0
        intimate = atmosphere.intimacy if atmosphere else 0.0
        mult = self.config.atmosphere_multiplier

        base = arousal_baseline(state, hour=hour)
        current = float(state.affect)
        rate = self.config.affect_decay_per_min * nonlinear_factor(current - base)
        if liveliness > 0:
            rate -= self.config.affect_decay_per_min * liveliness * 1.5 * mult
        if intimate > 0:
            rate -= self.config.affect_decay_per_min * intimate * mult
        rate = max(0.0, rate)
        # 负效价 + 高心潮：平复得更快（连续倍率，避免锯齿）
        rate *= self._calm_boost(state, current=current)
        state.affect = base + (current - base) * math.exp(-rate * minutes)

        # 效价偏移：指数衰减回 0（也就回到了基线）
        offset = float(state.valence_offset)
        v_rate = VALENCE_DECAY_PER_MIN * nonlinear_factor(offset)
        state.valence_offset = offset * math.exp(-v_rate * minutes)

        state.affect_synced_at = stamp
        self._refresh(state, hour=hour)
        self._update_storm(state, now=stamp)
        if self._safety_valve(state, now=stamp):
            self._refresh(state, hour=hour)
            return True
        return False

    def _calm_boost(self, state: WorldState, *, current: float) -> float:
        """负效价 + 高心潮时的加速平复倍率（1.0 ~ 1+CALM_BOOST_MAX）。"""

        valence = float(state.valence)
        if valence >= 0.5 or current <= 0.5:
            boost = 0.0
        else:
            low = (0.5 - valence) / 0.5
            high = max(0.0, current - 0.5) / 0.5
            boost = CALM_BOOST_MAX * clamp_value(low * high, 0.0, 1.0)
        # 已经在标记里的，再给一点，保证"气头上"不会拖太久
        if state.storm:
            boost = max(boost, 0.6)
        return 1.0 + boost

    def _update_storm(self, state: WorldState, *, now: float) -> None:
        """维护「正在气头上」标记：带滞回，且不会无限持续。"""

        affect = float(state.affect)
        valence = float(state.valence)
        if state.storm:
            expired = bool(state.storm_since) and (
                now - float(state.storm_since) >= STORM_MAX_MINUTES * 60
            )
            if affect < STORM_EXIT_AROUSAL or valence > STORM_EXIT_VALENCE or expired:
                state.storm = False
                state.storm_since = 0.0
            return
        if affect > STORM_ENTER_AROUSAL and valence < STORM_ENTER_VALENCE:
            state.storm = True
            state.storm_since = now

    def _safety_valve(self, state: WorldState, *, now: float) -> bool:
        """效价长期偏低：把偏移减半（朝基线拉，而不是硬设一个值）。"""

        if float(state.valence) >= SAFETY_VALVE_VALENCE:
            state.low_valence_since = 0.0
            return False
        if not state.low_valence_since:
            state.low_valence_since = now
            return False
        if now - float(state.low_valence_since) < SAFETY_VALVE_MINUTES * 60:
            return False
        state.valence_offset = clamp_value(float(state.valence_offset) * 0.5, -0.5, 0.5)
        state.low_valence_since = now
        state.storm = False
        state.storm_since = 0.0
        return True

    def _refresh(self, state: WorldState, *, hour: int) -> None:
        """重新算出对外可见的效价（基线 + 偏移），并夹好范围。"""

        state.affect = clamp_value(float(state.affect), 0.0, 1.0)
        state.valence_offset = clamp_value(float(state.valence_offset), -0.5, 0.5)
        base = valence_baseline(state, hour=hour)
        state.valence = clamp_value(base + float(state.valence_offset), 0.0, 1.0)

    def refresh(self, state: WorldState, *, now: float | None = None) -> None:
        """外部改了五维之后，把效价基线跟着重算一遍。"""

        stamp = self._now() if now is None else float(now)
        self._refresh(state, hour=self._hour(stamp))

    # ---------------- 事件影响 ----------------

    def apply_event(
        self,
        state: WorldState,
        kind: str,
        magnitude: float = 1.0,
        *,
        now: float | None = None,
    ) -> bool:
        """事件脉冲。返回 True 表示顺带触发了安全阀。"""

        effects = EVENT_EFFECTS.get(kind)
        if not effects:
            return False
        return self._apply_pulse(state, effects, magnitude=magnitude, now=now)

    def apply_event_delta(
        self, state: WorldState, field: str, delta: float, *, now: float | None = None
    ) -> bool:
        """按事件的规则施加一个临时增量。

        主模型给的 `valence_delta` 走这条：它享受同样的边际递减与两轴耦合，
        但不在事件表里——那是个"这一刻她的感受"，不是预设好的一类事件。
        """

        if not delta:
            return False
        return self._apply_pulse(state, {field: float(delta)}, magnitude=1.0, now=now)

    def _apply_pulse(
        self,
        state: WorldState,
        effects: dict[str, float],
        *,
        magnitude: float = 1.0,
        now: float | None = None,
    ) -> bool:
        """事件脉冲的公共实现：先补衰减，再用同一个快照算两轴的互相影响。"""

        reset = self._sync_emotions(state, now=now)
        # 快照：两轴的互相影响都用"事件发生前"的值算，先后顺序不影响结果
        snap_affect = float(state.affect)
        snap_valence = float(state.valence)
        tone = float(effects.get("_tone", 0.0) or 0.0)
        for field, delta in effects.items():
            if str(field).startswith("_"):
                continue
            value = float(delta) * float(magnitude)
            if not value:
                continue
            if field == "affect":
                if value > 0:
                    value *= affect_saturation(snap_affect)
                    value *= valence_asymmetry(snap_valence, tone)
                state.affect = snap_affect + value
                continue
            if field == "valence":
                value *= valence_saturation(snap_valence)
                value *= arousal_scale(snap_affect)
                state.valence_offset = clamp_value(
                    float(state.valence_offset) + value, -0.5, 0.5
                )
                continue
            current = float(getattr(state, field, 0.0))
            setattr(state, field, current + value)
        self._clamp(state)
        hour = self._hour(now)
        self._refresh(state, hour=hour)
        self._update_storm(state, now=self._now() if now is None else float(now))
        state.mood = self.derive_mood(state)
        return reset

    # ---------------- 动作效果 ----------------

    def apply_effects(
        self,
        state: WorldState,
        effects: dict[str, Any],
        *,
        world: WorldConfig | None = None,
        scale: float = 1.0,
        now: float | None = None,
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
            # 效价对外是「基线 + 偏移」：配置里写的是她看到的总值，
            # 改动落在偏移上，否则下一次结算就会被基线覆盖掉。
            if field == "valence":
                current = float(state.valence)
                target = self._apply_operator(text, current, scale)
                state.valence_offset = clamp_value(
                    float(state.valence_offset) + (target - current), -0.5, 0.5
                )
                continue
            if not hasattr(state, field):
                continue
            current = float(getattr(state, field))
            new_value = self._apply_operator(text, current, scale)
            if field == "affect" and new_value > current:
                # 动作带来的情绪同样是边际递减的：已经很激动时，抱一下也加不了多少
                new_value = current + (new_value - current) * affect_saturation(current)
            setattr(state, field, new_value)
        self._clamp(state)
        self._refresh(state, hour=self._hour(now))

    @staticmethod
    def _apply_operator(text: str, current: float, scale: float) -> float:
        if text.startswith("="):
            return _to_float(text[1:], current)
        if text.startswith("×") or text.startswith("*"):
            return current * _to_float(text[1:], 1.0)
        if text.startswith("+"):
            return current + _to_float(text[1:], 0.0) * scale
        if text.startswith("-"):
            return current - _to_float(text[1:], 0.0) * scale
        return _to_float(text, current)

    # ---------------- mood ----------------

    def derive_mood(self, state: WorldState, *, world: WorldConfig | None = None) -> str:
        """心情词：由情绪两轴派生（精力只作修饰）。

        动作里写了 `mood:温柔` 时，覆盖期内直接用它——那是她在"演"这个语气，
        不是心境变了。
        """

        if state.world_time < state.mood_override_until:
            return state.mood
        return mood_label(state.affect, state.valence, energy=state.energy)

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

    def ignored_magnitude(self, state: WorldState, *, now: float) -> float:
        """被冷落的递减力度：第一次最疼，之后递减，隔一阵重新算。"""

        last = float(state.ignored_at or 0.0)
        if last and now - last >= IGNORED_RESET_MINUTES * 60:
            state.ignored_streak = 0
        streak = max(0, int(state.ignored_streak or 0))
        magnitude = (
            IGNORED_STREAK_MAGNITUDES[streak]
            if streak < len(IGNORED_STREAK_MAGNITUDES)
            else 0.0
        )
        state.ignored_streak = streak + 1
        state.ignored_at = now
        return magnitude

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
            "valence": round(state.valence, 4),
            "boredom": round(state.boredom, 4),
        }


def _to_float(text: str, default: float) -> float:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return default
