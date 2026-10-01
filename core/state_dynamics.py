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
from .mood import cause_text, day_mood_info, day_mood_rate, mood_label, vague_word_for
from .state import WorldState

# ---------------- 事件表 ----------------
#
# 「事件只负责拉高，不负责维持」：这里的数值是**脉冲**，衰减由 _sync_emotions 负责。
# `_tone`：这条事件对心情来说是好事(+1)、坏事(-1)还是只是被注意到(0)。
# 心潮的加成会按「当时的效价 × tone」做不对称——心情好时更容易被逗乐，
# 心情差时更容易被惹毛（见 valence_asymmetry）。
EVENT_EFFECTS: dict[str, dict[str, float]] = {
    # 正面的这几条刻意压得小：它们是"每天都可能连着来"的脉冲，
    # 一句玩笑就推 0.2 的话，几句闲聊就能把她顶到满值、之后一直卡在兴奋档。
    # 真正的起伏交给主模型自己写的 valence_delta 和事件系统。
    "mention_bot": {"affect": 0.03, "loneliness": -0.04, "_tone": 0.0},
    "positive_words": {
        "affect": 0.06,
        "valence": 0.04,
        "loneliness": -0.12,
        "_tone": 1.0,
    },
    "negative_words": {
        "affect": 0.13,
        "valence": -0.08,
        "loneliness": 0.10,
        "_tone": -1.0,
    },
    "hug_bot": {"affect": 0.08, "valence": 0.04, "loneliness": -0.16, "_tone": 1.0},
    # 低潮时的安抚通道（见 ``StateDynamics.soothed``）：不吃"聊天推效价"的当天额度，
    # 靠"每小时/每天几次"限流。普通时候用不到，所以她真的难过了才有这一条路。
    "soothed": {"affect": 0.04, "valence": 0.10, "loneliness": -0.06, "_tone": 1.0},
    # 「有人听懂我那几句」：比单纯抱一下更管用，一天只认几次
    "understood": {
        "affect": 0.05,
        "valence": 0.12,
        "loneliness": -0.12,
        "_tone": 1.0,
    },
    # 群里热闹**既不算有人陪她，也不算"她不闷"**：
    # - 孤独要有"有人直接跟她说话"才降（mention_bot / positive_words / hug_bot）；
    # - 无聊是"她自己没事做"，跟群里热闹无关——以前这条扣无聊，群里活跃一整天
    #   她就一直待在一个地方不动（换地点是无聊驱动的）。
    # 只留一点心潮：氛围热闹，她跟着有点精神。
    "group_lively": {"affect": 0.02, "_tone": 0.0},
    # 群里热闹但没人在跟她说话：看着别人聊，自己插不上话——会更闷、更想找人说两句
    "left_out": {"boredom": 0.03, "loneliness": 0.02, "valence": -0.01, "_tone": -0.5},
    # 她记下一笔账：当场就有点上头，之后靠"降一档 + 提示词"体现，
    # 不在这里反复扣心情（不然一件旧账会把她一直吊在气头上）
    "grudge": {"affect": 0.06, "valence": -0.05, "_tone": -0.6},
    "group_quiet": {"loneliness": 0.05, "boredom": 0.10},
    "user_joined": {"curiosity": 0.10},
    "user_left": {"loneliness": 0.05},
    "topic_engaged": {"affect": 0.01, "boredom": -0.05, "_tone": 0.0},
    # 被冷落只压心情，不再抬高心潮：否则她会从"退缩"直接跳成"发作"，
    # 群里看到的就是"刚被无视完突然开始阴阳怪气"。
    "ignored": {"valence": -0.08, "loneliness": 0.05, "boredom": 0.05, "_tone": -1.0},
    # 生活事件：工具、日程、发送这些线上一眼看不见的挫折与顺利
    "tool_failed": {"affect": 0.07, "valence": -0.05, "_tone": -1.0},
    "interrupted": {"affect": 0.08, "valence": -0.06, "_tone": -1.0},
    "schedule_done": {"affect": 0.02, "valence": 0.03, "_tone": 1.0},
    "send_failed": {"affect": 0.04, "valence": -0.03, "_tone": -1.0},
}

MOOD_CAUSE_TEXT: dict[str, str] = {
    "mention_bot": "有人点名找你",
    "positive_words": "被夸了",
    "negative_words": "被怼了",
    "hug_bot": "被哄了一下",
    "soothed": "被安抚了一会儿",
    "understood": "有人听懂你那几句",
    "ignored": "你说了话没人接",
    "tool_failed": "手上的活没做成",
    "interrupted": "手上的事被打断了",
    "schedule_done": "安排的事做成了",
    "send_failed": "消息没发出去",
}
"""这类事件推动情绪时，顺手记一句来源（提示词里会写"因为：…"）。"""

# 边际递减：已经很激动时，同样的刺激能加进去的更少（最低保留 10% 效力）。
# 没有这一层的话，一两轮聊天就能把心潮顶到满值，之后一直卡在"难以平静"。
# 地板压到 0.10 是因为满值附近几乎不该再被推动——100 分只该留给真的极端时刻。
AFFECT_SATURATION_FLOOR = 0.10
VALENCE_SATURATION_FLOOR = 0.10

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
"""效价偏移每分钟回落的比例系数（指数衰减，永远朝基线）。

回落速度别再往上加：真正该收紧的是"一下能推多少"，不是"多久忘掉"——
吵一架难受两个小时是对的，被夸两句就满值才是错的。
"""

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

SOOTHE_VALENCE_BELOW = 0.38
"""效价低于这儿，"被安抚"才另算一份。

心情好的时候抱一下只是亲密（本来就降孤独、抬心潮），不需要再补心情；
真的难过了才该有一条更宽的路——日常的效价量程仍然由
``chat_valence_daily_cap`` 管着，不然"被抱几句"就能盖过她真经历的一件事。
"""

SOOTHE_MAX_PER_HOUR = 3
SOOTHE_MAX_PER_DAY = 6
UNDERSTOOD_MAX_PER_DAY = 3
"""安抚通道的限流：每小时 / 每天最多认几次（按小时算的是两条路合起来）。"""

SOOTHE_LOG_KEEP = 30
"""安抚记录最多留几条（够算当天次数就行）。"""

REPEAT_TRACKED = ("mention_bot", "positive_words", "hug_bot")
"""会被"连着来"磨掉效力的事件：被叫、被夸、被哄。

这三条是每天来得最密的正面脉冲。不递减的话，语速快的会话几十轮下来
心潮必然钉在满值——之后一直"激动得不行"，表达格子也永远落在同一格。
"""

REPEAT_MAGNITUDES = (1.0, 0.6, 0.35, 0.2, 0.12)
"""连着第 1/2/3/4/5 次的力度，第 6 次起固定为 REPEAT_FLOOR。"""

REPEAT_FLOOR = 0.08
"""麻木之后保留的效力：不是完全没有反应，只是不再叠加。"""

REPEAT_RESET_MINUTES = 20
"""隔这么久没有同类好事，就当"缓过来了"，重新按第一次算。"""

CURIOSITY_SOFT_CAP = 0.7
"""好奇心过了这条线就开始"饱和"：越接近满值涨得越慢。

没有这一层的话，好奇心是**只涨不落**的（自然增长 1.44/天，而唯一的下落是随机事件里
那几口干 -0.02，以及动作配的 -0.06），一天下来必然钉死在 1.0，之后就一直"想知道点什么"。
"""


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


def curiosity_growth_factor(curiosity: float) -> float:
    """好奇心的增长系数：软上限以下照常涨，以上越接近满值涨得越慢。

    到 1.0 时系数归零，所以好奇心是**渐近**贴近满值，而不是一口气顶死在那里。
    """

    value = clamp_value(float(curiosity or 0.0), 0.0, 1.0)
    if value <= CURIOSITY_SOFT_CAP:
        return 1.0
    span = max(1e-6, 1.0 - CURIOSITY_SOFT_CAP)
    return clamp_value((1.0 - value) / span, 0.0, 1.0)


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
            # 睡一觉把"昨天攒的好奇"放下：不然她会是带着满格好奇心入睡、
            # 一醒来（所有门禁放开）就冲去查东西。无聊 / 孤独在睡眠里维持原样。
            curiosity = float(state.curiosity)
            if curiosity > 0:
                cool = max(0.0, float(self.config.sleep_curiosity_decay_per_min)) * minutes
                floor = clamp_value(float(self.config.sleep_curiosity_floor), 0.0, 1.0)
                state.curiosity = max(min(floor, curiosity), curiosity - cool)
            # 睡着的时候那股"想被碰一碰"也淡下去：睡醒没那么憋
            state.desire = clamp_value(
                float(state.desire)
                - max(0.0, float(self._desire_config("desire_sleep_fall_per_hour", 0.05)))
                * minutes
                / 60.0,
                0.0,
                1.0,
            )
            state.desire_slept = True
            # 睡着了也要让情绪平复：醒来时不该还揣着昨晚那口气
            reset = self._sync_emotions(state, now=now, node=node)
            self._clamp(state)
            state.mood = self.derive_mood(state)
            return reset

        calm = atmosphere.calm if atmosphere else 0.0
        liveliness = atmosphere.liveliness if atmosphere else 0.0
        lonely_air = atmosphere.loneliness if atmosphere else 0.0
        curious_air = atmosphere.curiosity if atmosphere else 0.0

        # 刚睡醒：睡了一觉，那股"想被碰一碰"松下来一截
        if bool(getattr(state, "desire_slept", False)):
            state.desire = clamp_value(
                float(state.desire) * self._desire_config("desire_wake_keep", 0.7),
                0.0,
                1.0,
            )
            state.desire_slept = False

        # 今天的基调：只调这几条曲线的快慢，不改别的（见 mood.DAY_MOODS）
        day_energy = self._day_rate(state, "energy")
        day_lonely = self._day_rate(state, "loneliness")
        day_curious = self._day_rate(state, "curiosity")
        day_bored = self._day_rate(state, "boredom")

        # 精力：基础衰减；安静环境恢复更快
        energy_delta = -self.config.energy_decay_per_min * day_energy * minutes
        if calm > 0:
            energy_delta += (
                self.config.energy_decay_per_min * day_energy * calm * mult * minutes
            )
        if state.state == "walking":
            energy_delta *= 1.2
        state.energy += energy_delta

        # 孤独：基础增长；窗边发呆加速；氛围调制
        lonely_rate = self.config.loneliness_growth_per_min * day_lonely
        lonely_rate *= 1 + lonely_air * mult
        if state.state == "staring":
            lonely_rate *= 1.5
        state.loneliness += lonely_rate * minutes

        # 好奇：基础增长；氛围调制；搜索时消耗
        curiosity_rate = self.config.curiosity_growth_per_min * day_curious
        curiosity_rate *= 1 + curious_air * mult
        # 已经很想知道点什么了：再涨就慢下来，别一天下来钉死在满值（见 CURIOSITY_SOFT_CAP）
        curiosity_rate *= curiosity_growth_factor(state.curiosity)
        state.curiosity += curiosity_rate * minutes
        if state.state == "searching":
            state.curiosity -= 0.002 * minutes
            state.boredom -= 0.003 * minutes

        # 无聊：基础增长；安静环境增长变慢；热闹环境下降
        boredom_rate = self.config.boredom_growth_per_min * day_bored
        boredom_rate *= 1 - calm * mult * 0.5
        boredom_rate *= 1 - liveliness * mult * 0.5
        # 同一个地方待得越久越坐不住：换地点（node_since 重置）就把这个系数清零
        boredom_rate *= self.dwell_factor(state, now)
        state.boredom += boredom_rate * minutes

        # 欲求：想被碰一碰。没人碰就一直慢慢涨，累着 / 心情差的时候涨得慢。
        desire_rate = self._desire_config("desire_growth_per_min", 0.000231)
        if float(state.desire) >= self._desire_config("desire_soft_top", 0.85):
            desire_rate *= 0.5
        if float(state.energy) < 0.3:
            desire_rate *= self._desire_config("desire_low_energy_factor", 0.6)
        if float(state.valence) < 0.15:
            desire_rate *= self._desire_config("desire_low_valence_factor", 0.2)
        state.desire = clamp_value(float(state.desire) + desire_rate * minutes, 0.0, 1.0)

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
        day_affect = self._day_rate(state, "affect")
        rate = (
            self.config.affect_decay_per_min
            * day_affect
            * nonlinear_factor(current - base)
        )
        if liveliness > 0:
            rate -= self.config.affect_decay_per_min * day_affect * liveliness * 1.5 * mult
        if intimate > 0:
            rate -= self.config.affect_decay_per_min * day_affect * intimate * mult
        rate = max(0.0, rate)
        # 负效价 + 高心潮：平复得更快（连续倍率，避免锯齿）
        rate *= self._calm_boost(state, current=current)
        state.affect = base + (current - base) * math.exp(-rate * minutes)

        # 效价偏移：指数衰减回 0（也就回到了基线）
        offset = float(state.valence_offset)
        v_rate = (
            self._valence_decay_per_min()
            * self._day_rate(state, "valence")
            * nonlinear_factor(offset)
        )
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
        if kind in REPEAT_TRACKED:
            # 连着被哄会麻木：第二次起同样的好事推不动那么多了
            magnitude = float(magnitude) * self.repeat_magnitude(state, kind, now=now)
        return self._apply_pulse(
            state,
            effects,
            magnitude=magnitude,
            now=now,
            cause=MOOD_CAUSE_TEXT.get(kind, ""),
            chat=kind in REPEAT_TRACKED,
        )

    def soothed(
        self,
        state: WorldState,
        *,
        kind: str = "soothed",
        now: float | None = None,
    ) -> bool:
        """安抚通道：她真的难过时，抱一会儿、有人听懂她那几句，走这条路。

        日常陪伴改的是好感度，心情的量程由 ``chat_valence_daily_cap`` 管着——
        那是为了不让"被夸两句"盖过她真经历的一件事。但她低潮的时候该有另一条更宽的路：
        这一下**不占当天的聊天额度**，改用"每小时 / 每天几次"限流，
        所以连着哄十次和哄一次不是同一回事，也不会被刷成永动机。

        ``kind``：``soothed``（被安抚，要求当前效价偏低）/ ``understood``（被理解，一天只认几次）。
        返回 True 表示这一次真的推动了心情（也才记进限额）。
        """

        if kind not in ("soothed", "understood"):
            return False
        if kind == "soothed" and float(state.valence) >= SOOTHE_VALENCE_BELOW:
            return False
        stamp = self._now() if now is None else float(now)
        try:
            day = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            day = ""
        log = [
            dict(item)
            for item in list(getattr(state, "soothe_log", []) or [])
            if isinstance(item, dict)
        ]
        hour_ago = stamp - 3600
        recent = [item for item in log if float(item.get("at") or 0.0) >= hour_ago]
        if len(recent) >= SOOTHE_MAX_PER_HOUR:
            return False
        today = [item for item in log if str(item.get("day") or "") == day]
        limit = UNDERSTOOD_MAX_PER_DAY if kind == "understood" else SOOTHE_MAX_PER_DAY
        if len([item for item in today if str(item.get("kind")) == kind]) >= limit:
            return False
        before = float(state.valence)
        self.apply_event(state, kind, now=stamp)
        if abs(float(state.valence) - before) < 0.005:
            # 一点都没推动（比如效价已经贴顶）：不占额度，下次还有机会
            return False
        log.append({"at": stamp, "day": day, "kind": kind})
        state.soothe_log = log[-SOOTHE_LOG_KEEP:]
        return True

    def apply_event_delta(
        self,
        state: WorldState,
        field: str,
        delta: float,
        *,
        now: float | None = None,
        cause: str = "",
        chat: bool = False,
    ) -> bool:
        """按事件的规则施加一个临时增量。

        主模型给的 `valence_delta` 走这条：它享受同样的边际递减与两轴耦合，
        但不在事件表里——那是个"这一刻她的感受"，不是预设好的一类事件。

        ``chat=True`` 表示这一下是**聊天**推的（不是她真经历了什么）：
        正向部分要占当天的聊天额度，见 ``_spend_chat_valence``。
        """

        if not delta:
            return False
        return self._apply_pulse(
            state,
            {field: float(delta)},
            magnitude=1.0,
            now=now,
            cause=cause,
            chat=chat,
        )

    def apply_pulse(
        self,
        state: WorldState,
        effects: dict[str, Any],
        *,
        now: float | None = None,
        cause: str = "",
        chat: bool = False,
    ) -> bool:
        """事件系统给的一串状态脉冲（``{字段: 增量}``）。

        和 ``apply_event`` 走同一条路：饱和、两轴耦合、夹紧、重算心情都照旧，
        只是这次的数值不是预设好的事件表，而是当场算出来的。
        """

        if not effects:
            return False
        clean: dict[str, float] = {}
        for name, value in dict(effects).items():
            key = str(name).strip()
            if key.startswith("_") or not hasattr(state, key):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number:
                clean[key] = number
        if not clean:
            return False
        return self._apply_pulse(
            state, clean, magnitude=1.0, now=now, cause=cause, chat=chat
        )

    def _apply_pulse(
        self,
        state: WorldState,
        effects: dict[str, float],
        *,
        magnitude: float = 1.0,
        now: float | None = None,
        cause: str = "",
        chat: bool = False,
    ) -> bool:
        """事件脉冲的公共实现：先补衰减，再用同一个快照算两轴的互相影响。

        ``cause`` 是"为什么变成这样"的一句短语；这一下如果**真的**把情绪推动了
        （心潮 ≥0.03 或效价 ≥0.02），就把它记成当前的心情来源，供提示词与状态页用。

        ``chat``：这一下是聊天推的。**正向**的效价要占当天的聊天额度——
        日常陪伴改的是好感度，不是心情的量程；负向不占（难过不因为今天被怼过就不算数）。
        """

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
                if chat and value > 0:
                    value = self._spend_chat_valence(state, value, now=now)
                if not value:
                    continue
                state.valence_offset = clamp_value(
                    float(state.valence_offset) + value, -0.5, 0.5
                )
                continue
            current = float(getattr(state, field, 0.0))
            setattr(state, field, current + value)
        self._clamp(state)
        hour = self._hour(now)
        self._refresh(state, hour=hour)
        stamp = self._now() if now is None else float(now)
        self._update_storm(state, now=stamp)
        moved = abs(float(state.affect) - snap_affect) >= 0.03 or abs(
            float(state.valence) - float(snap_valence)
        ) >= 0.02
        if cause and moved:
            state.mood_cause = " ".join(str(cause).split())[:24]
            state.mood_cause_at = stamp
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

        标签 = 格子词 + 修饰（困 / 想找人 / 闲得发慌 / 心痒）+ 来源（为什么变成这样）。
        修饰和来源都是**当下成立**才加：来源超过半小时就不再挂了。
        """

        if state.world_time < state.mood_override_until:
            return state.mood
        return mood_label(
            state.affect,
            state.valence,
            energy=state.energy,
            loneliness=state.loneliness,
            boredom=state.boredom,
            curiosity=state.curiosity,
            desire=float(getattr(state, "desire", 0.0) or 0.0),
            cause=cause_text(
                state.mood_cause, at=state.mood_cause_at, now=self._now()
            ),
            vague=self._vague_word(state),
        )

    def _day_rate(self, state: WorldState, key: str) -> float:
        """今天这条速率要乘多少。没开基调 / 不是那几条之一时恒为 1.0。"""

        if not bool(getattr(self.config, "daily_mood_enabled", True)):
            return 1.0
        try:
            strength = float(getattr(self.config, "daily_mood_strength", 1.0))
        except (TypeError, ValueError):
            strength = 1.0
        return day_mood_rate(getattr(state, "day_mood", ""), key, strength=strength)

    @staticmethod
    def _vague_word(state: WorldState) -> str:
        """今天是"说不上来"的一天时，给模糊档的那个词；否则空串。"""

        if str(getattr(state, "day_mood", "") or "") != "vague":
            return ""
        return vague_word_for(str(getattr(state, "day_mood_day", "") or ""))

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

    def repeat_magnitude(
        self, state: WorldState, kind: str, *, now: float | None = None
    ) -> float:
        """同一类好事连着来的递减力度（被夸第 N 次就不稀奇了）。

        和 ``ignored_magnitude`` 同一套思路，只是按事件名分开记：
        "被叫了 20 次"和"被夸了 20 次"是两回事，不该互相磨掉。
        """

        stamp = self._now() if now is None else float(now)
        streak_at = state.praise_streak_at if isinstance(state.praise_streak_at, dict) else {}
        streak_map = state.praise_streak if isinstance(state.praise_streak, dict) else {}
        last = float(streak_at.get(kind) or 0.0)
        streak = 0 if (last and stamp - last >= REPEAT_RESET_MINUTES * 60) else max(
            0, int(streak_map.get(kind) or 0)
        )
        magnitude = (
            REPEAT_MAGNITUDES[streak]
            if streak < len(REPEAT_MAGNITUDES)
            else REPEAT_FLOOR
        )
        streak_at[kind] = stamp
        streak_map[kind] = streak + 1
        state.praise_streak = streak_map
        state.praise_streak_at = streak_at
        return magnitude

    # ---------------- 欲求：想被碰一碰 ----------------

    def _desire_config(self, key: str, fallback: float) -> float:
        """读一条欲求相关的配置；老存档 / 测试桩里没有就用兜底值。"""

        try:
            value = float(getattr(self.config, key, fallback))
        except (TypeError, ValueError):
            return float(fallback)
        return value if value >= 0 else float(fallback)

    def satisfy_desire(
        self, state: WorldState, intimacy: float = 1.0, *, scale: float = 1.0
    ) -> float:
        """被**实实在在地亲近**了一次：欲求落一截，落多少看动作自己的亲密程度。

        返回实际落了多少（0 表示这一步不算亲密接触）。

        两条护栏（以前没有，结果是"两次抱抱就把一天的欲求扣光"）：

        - 单次降幅小（``desire_relief`` 默认 0.08）：贴着贴着慢慢降，不是一下抽干；
        - 降到 ``desire_relief_floor``（默认 0.30）就**不再往下降**——那下面是
          "淡淡的、不太想"那一段，日常亲密不该把人推进去；再贴反而是往上一点
          （``desire_contact_warm``，默认 0.005×亲密度）。
        """

        weight = max(0.0, float(intimacy or 0.0)) * max(0.0, float(scale or 0.0))
        if weight <= 0:
            return 0.0
        before = float(state.desire)
        floor = max(0.0, min(0.95, float(self._desire_config("desire_relief_floor", 0.30))))
        if before <= floor:
            warm = max(0.0, float(self._desire_config("desire_contact_warm", 0.005))) * weight
            state.desire = clamp_value(before + warm, 0.0, 1.0)
            return 0.0
        relief = max(0.0, float(self._desire_config("desire_relief", 0.08))) * weight
        state.desire = clamp_value(max(floor, before - relief), 0.0, 1.0)
        return before - float(state.desire)

    def tease_desire(self, state: WorldState, *, scale: float = 1.0) -> float:
        """被**撩**了一下（只是嘴上/氛围上，不是真碰到）：欲求往上跳一点。

        ``scale`` 由调用方按关系亲疏给（越亲近的人越管用）。
        """

        bonus = self._desire_config("desire_tease", 0.05) * max(0.0, float(scale or 0.0))
        if bonus <= 0:
            return 0.0
        before = float(state.desire)
        state.desire = clamp_value(before + bonus, 0.0, 1.0)
        return float(state.desire) - before

    # ---------------- 工具 ----------------

    def _valence_decay_per_min(self) -> float:
        """效价回落速度：读配置，读不到（老存档 / 测试桩）就用默认常量。"""

        try:
            value = float(getattr(self.config, "valence_decay_per_min", VALENCE_DECAY_PER_MIN))
        except (TypeError, ValueError):
            return VALENCE_DECAY_PER_MIN
        return value if value > 0 else VALENCE_DECAY_PER_MIN

    def _spend_chat_valence(
        self, state: WorldState, value: float, *, now: float | None = None
    ) -> float:
        """聊天能推动的效价走当天额度：额度用完了就不再推。

        正负各算一份：今天开心够了不该挡着她难过。
        额度是"聊天"这条路专用的——她真经历一件事（事件系统）不受它限制。
        """

        try:
            budget = float(
                getattr(self.config, "chat_valence_daily_cap", 0.15) or 0.0
            )
        except (TypeError, ValueError):
            budget = 0.15
        if budget <= 0:
            return value
        stamp = self._now() if now is None else float(now)
        try:
            today = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            today = ""
        if str(state.chat_day or "") != today:
            state.chat_day = today
            state.chat_valence_spent = 0.0
        try:
            spent = float(state.chat_valence_spent or 0.0)
        except (TypeError, ValueError):
            spent = 0.0
        left = budget - max(0.0, spent)
        allowed = min(float(value), max(0.0, left))
        if allowed <= 0:
            return 0.0
        state.chat_valence_spent = spent + allowed
        return allowed

    @staticmethod
    def _clamp(state: WorldState) -> None:
        state.clamp()

    # 「在这儿待了多久」的分段：越久，无聊涨得越快
    DWELL_STEPS: tuple[tuple[float, float], ...] = (
        (10.0, 1.0),
        (30.0, 1.3),
        (60.0, 1.6),
        (180.0, 2.0),
    )
    """``(停留分钟上限, 系数)``：超过 180 分钟按最后一档算。"""

    def dwell_minutes(self, state: WorldState, now: float | None = None) -> float:
        """她在当前这个地点待了多久（分钟；拿不到起始时间就返回 0）。"""

        since = float(state.node_since or 0.0)
        if since <= 0:
            return 0.0
        return max(0.0, (self._now() if now is None else float(now)) - since) / 60.0

    def dwell_factor(self, state: WorldState, now: float | None = None) -> float:
        """无聊增长要乘的"待久了"系数（换地点就回到 1.0）。"""

        minutes = self.dwell_minutes(state, now)
        for limit, value in self.DWELL_STEPS:
            if minutes < limit:
                return value
        return self.DWELL_STEPS[-1][1]

    def values(self, state: WorldState) -> dict[str, float]:
        return {
            "energy": round(state.energy, 4),
            "loneliness": round(state.loneliness, 4),
            "curiosity": round(state.curiosity, 4),
            "affect": round(state.affect, 4),
            "valence": round(state.valence, 4),
            "boredom": round(state.boredom, 4),
            "desire": round(float(state.desire), 4),
        }


def _to_float(text: str, default: float) -> float:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return default
