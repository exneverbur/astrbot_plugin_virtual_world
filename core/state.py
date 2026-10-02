"""运行时状态对象（每个会话独立）。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .events import normalize_abilities

# bot_state 取值
STATE_IDLE = "idle"
STATE_AWAKENING = "awakening"
STATE_SLEEPING = "sleeping"
STATE_NAPPING = "napping"
STATE_DROWSY = "drowsy"
"""临睡期：困得不行、正要睡，但还没躺下。

夜里睡前的那一小段：这段时间她还能说话（而且说得迷迷糊糊），
等安静下来才真的睡——"说完晚安立刻断线"看着像关机，不像人。
"""
STATE_STARING = "staring"
STATE_SEARCHING = "searching"
STATE_READING = "reading"
STATE_WALKING = "walking"
STATE_THINKING = "thinking"

# 同一个人在这段时间内发的内容差不多，就认为是同一条消息（两个钩子重复记一次）
_CHAT_DEDUPE_SECONDS = 60.0

_CHAT_DEDUPE_ANNOTATED_SECONDS = 600.0
"""带了注释的那一份可以放得宽一点：它一定是"同一条消息的另一份记录"，
而两个钩子之间偶尔会被排队拖到几十秒以上（连发合并、管线忙）。
"""

FORWARD_SUMMARY_MARK = "【转发的聊天记录·摘要】"
"""转发摘要写进聊天记录时的标记：提示词里这一行放宽字数，也用来认出它是转发。"""

_CHAT_TEXT_CHARS = 200
"""普通聊天记录一条最多存多少字（进提示词时会再按行截一次）。"""

_FORWARD_TEXT_CHARS = 800
"""带转发摘要的那一条放宽：摘要本身就是压过的，再掐200字就只剩半句。"""

CHAT_DROPPED_KEEP = 120
"""留档超上限被顶掉的那几条先攒着，等下一次压缩一起并进摘要（配置成"压缩"时）。"""

MAX_CHAT_IMAGE_REFS = 2
"""一条聊天记录最多记住几张图（再多也没人会去对照）。"""

_CHAT_IMAGE_REF_CHARS = 300
"""图片地址最多存这么长：base64 这种超长内容不进留档，免得状态文件被撑爆。"""


def _chat_image_refs(images: Any) -> list[str]:
    """挑出值得存进聊天留档的图片地址（太长的一律不要）。"""

    if not images:
        return []
    result: list[str] = []
    for item in images:
        ref = str(item or "").strip()
        if not ref or len(ref) > _CHAT_IMAGE_REF_CHARS or ref in result:
            continue
        result.append(ref)
        if len(result) >= MAX_CHAT_IMAGE_REFS:
            break
    return result


def _clamp01(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(0.0, min(1.0, number))


def _same_chat_text(left: str, right: str) -> bool:
    """两条聊天文本算不算"同一句"。

    两个钩子拿到的文本可能一个带 @ 前缀、一个被别的插件改写过，
    所以去掉空白和标点后比较，并且容忍一方是另一方的子串。
    """

    def squeeze(text: str) -> str:
        return "".join(ch for ch in str(text or "") if ch.isalnum())

    a = squeeze(left)
    b = squeeze(right)
    if not a or not b:
        return not a and not b
    if a == b:
        return True
    return len(min(a, b, key=len)) >= 4 and (a in b or b in a)


_ANNOTATION_MARKS = ("\n［", "\n[", " ［", " [")
"""正文后面那段"注释"的起头：@ 了谁、引用了什么、图片里有什么。

它是 ``_annotate_message`` 补上去的，而两个钩子（旁观监听 / LLM 请求）不一定都补得上，
同一条消息就会存成"带注释"和"不带注释"两种样子。
"""


def chat_core_text(text: str) -> str:
    """切掉注释尾巴，只留"人说的那句话"。"""

    return split_annotation(text)[0]


def split_annotation(text: str) -> tuple[str, str]:
    """把一行拆成「人说的那句话」和尾部那段注释。

    注释是 ``_annotate_message`` 补上去的（@ 了谁、引用了什么、图里有什么），
    它是"这句到底冲谁说的"的关键信息，截断正文的时候不能连它一起掐掉。
    """

    body = str(text or "")
    cut = -1
    for mark in _ANNOTATION_MARKS:
        index = body.find(mark)
        if index >= 0 and (cut < 0 or index < cut):
            cut = index
    if cut < 0:
        return body, ""
    return body[:cut], body[cut:]


ANNOTATION_KEEP_CHARS = 240
"""注释最多留多少字：注释本身也可能很长（@ 了五个人 + 引用 + 三张图）。"""


CHAT_MERGE_GAP_SECONDS = 300.0
"""同一个人连着说的这几句算"一条"：间隔不超过这么久才算连着。"""


def chat_group_key(item: dict[str, Any]) -> tuple[str, str]:
    """一条聊天记录属于哪一组：**谁在哪个会话说的**。

    认人用 QQ 号，不用昵称——昵称 / 群名片会变（群里还带「在书房」这种后缀），
    按名字分组会把同一个人拆成好几行。
    """

    if item.get("is_self"):
        who = "__self__"
    else:
        who = str(item.get("user_id") or item.get("name") or "")
    return who, str(item.get("origin") or "")


def group_chat_items(
    items: list[dict[str, Any]], *, gap_seconds: float = CHAT_MERGE_GAP_SECONDS
) -> list[list[dict[str, Any]]]:
    """按「谁在哪个会话连着说」分组。

    提示词渲染（并成一行）和"最多带几条"的计数都用它，保证算的条数和看到的行数一致：
    一个人连发十条只算一条，不会把额度吃光。
    """

    groups: list[list[dict[str, Any]]] = []
    for item in items:
        if groups:
            previous = groups[-1][-1]
            same = chat_group_key(previous) == chat_group_key(item)
            gap = float(item.get("at") or 0.0) - float(previous.get("at") or 0.0)
            if same and gap <= max(0.0, float(gap_seconds)):
                groups[-1].append(item)
                continue
        groups.append([item])
    return groups


def take_last_chat_groups(
    items: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """取最近 ``limit`` **行**（同一人连着说的算一行）。

    「最多带几行聊天记录」这件事**只在这里实现一次**：留档视图、提示词渲染、
    按会话分桶都调用它，省得同一份配置在两处算出两个数。
    """

    rows = list(items or [])
    if limit <= 0 or not rows:
        return rows
    groups = group_chat_items(rows)
    return [item for group in groups[-limit:] for item in group]


def chat_item_is_fresh(
    item: dict[str, Any], *, replied_until: float = 0.0, replied_seq: int = 0
) -> bool:
    """这条群聊是不是"她还没回应过的"。

    优先用**序号**判断：时间戳的精度只有秒，她刚说完的同一秒里进来的消息会被
    误判成"已经回应过"。序号不够用时（老存档、内存里造的测试数据）退回时间比较，
    没有时间的则一律当成新消息——宁可多给她看，也别把话藏起来。
    """

    if item.get("internal"):
        # 插件自己写进去的"她身上发生的事"：是背景，不是等着被回的消息
        return False
    seq = item.get("seq")
    if seq is not None and int(replied_seq or 0) > 0:
        try:
            return int(seq) > int(replied_seq)
        except (TypeError, ValueError):
            pass
    at = float(item.get("at") or 0.0)
    if at <= 0:
        return True
    return at > float(replied_until or 0.0)


@dataclass
class WorldState:
    """一个会话的完整运行时状态。"""

    session_id: str
    world_time: int = 0
    node_id: str = ""
    state: str = STATE_IDLE
    mood: str = "平静"
    energy: float = 0.6
    loneliness: float = 0.5
    curiosity: float = 0.5
    affect: float = 0.3
    """心潮：情绪被激起的程度（0~1）。

    越高，她的内心活动越激烈、说出来的话情绪越浓、越容易做出亲昵或冲动的举动；
    接近 0 时偏平淡克制。由互动（被夸、被抱、吵架、被冷落）抬高，随时间回落。
    """

    valence: float = 0.5
    """效价：心情的好坏（0~1，0.5 是中性）。= 基线 + 偏移。

    基线由五维与时段推导（本身不存历史），偏移随事件产生、随时间归零。
    心潮管"有多激动"，效价管"激动成什么样"。
    """

    valence_offset: float = 0.0
    """效价的短期偏移：真正带历史的那个量（-0.5 ~ 0.5，0 = 回到基线）。"""

    affect_synced_at: float = 0.0
    """上一次结算情绪两维的时间（现实时间戳）。

    情绪两轴按真实时间衰减：事件到来前先补上这一段，所以 tick 长度改了、
    或者宿主卡顿了，都不会改变曲线形状。
    """

    storm: bool = False
    """「正在气头上」标记：高心潮 + 负效价，带滞回，用于提示词与安全阀。"""

    storm_since: float = 0.0
    """标记是从什么时候开始的（现实时间戳，用来兜底最长持续时间）。"""

    last_style_cell: str = ""
    """最近一次回复用的表达格（例如 excited+negative）：日志与状态页用来看映射有没有生效。"""

    last_voice_samples: list[str] = field(default_factory=list)
    """上一轮用过的声音样例（id）：连着两轮别用同一组，免得她变成复读机。"""

    open_topics: list[dict[str, Any]] = field(default_factory=list)
    """还没聊完的话题：`{text, who, who_name, session, at, next_ask_at, asked}`。

    聊天记录只有「刚才在聊什么」（30 分钟就过期），话题一断她就再也不提了。
    这份账本记的是"他说了一半、还没结果"的事，到点且这个人再出现时提醒她可以接一句。
    """

    grudges: list[dict[str, Any]] = field(default_factory=list)
    """她记着的账：`{user_id, who_name, reason, at, until, asked}`。

    跟「心事」不一样：这一条是**冲人**的——他在她这儿还有一笔没算完，
    所以她对他会冷一档，而且**只在跟他说话时**才拿出来，不在别人面前提。
    """

    grudge_day: str = ""
    """上面那份账今天记了几笔（日期 + 计数，跟着配额走）。"""

    grudge_count: int = 0

    own_topics: list[dict[str, Any]] = field(default_factory=list)
    """她自己记着的事：`{text, who, who_name, at}`——答应过别人的、自己想做的，还没做。

    跟「还没聊完的」正好对称：那份记的是**别人的**事，这份记的是**她自己的**事。
    """

    last_say_limit: int = 0
    """那一次实际生效的句数上限（风格格、群聊硬顶、配置、密度提醒取更严格者）。"""

    interject_stats: dict[str, int] = field(default_factory=dict)
    """本小时「她想插话但被拦住」的计数：哪道闸在限流，一眼就能看出来。"""

    interject_hour_marker: int = 0
    """上面那份计数的所属小时。"""

    interject_closed_until: float = 0.0
    """「想被注意到」这条插话动机被关到什么时候（现实时间戳）。

    长期低落时关掉它，并保证至少关 15 分钟：否则效价一恢复就立刻打开，
    看起来像个开关。
    """

    low_valence_since: float = 0.0
    """效价持续偏低从什么时候开始（安全阀用）。"""

    ignored_streak: int = 0
    ignored_at: float = 0.0
    """被冷落的连续次数与时间：惩罚递减，隔一阵重新算（见 dynamics.ignored_magnitude）。"""

    praise_streak: dict[str, int] = field(default_factory=dict)
    praise_streak_at: dict[str, float] = field(default_factory=dict)
    """连着被哄/被逗的计数与时间（按事件名分开记）。

    真人被连着夸十遍会麻木，这里也一样：同一类好事短时间反复发生，加成就递减，
    隔一阵重新算（见 dynamics.repeat_magnitude）。
    """

    chat_day: str = ""
    chat_valence_spent: float = 0.0
    """聊天（主模型给的 valence_delta）今天累计推动了多少效价（带符号，跨天清零）。

    日常聊天是最高频的事，不能让它成为心情的主要来源——不然"被夸两句"比
    "她真的经历了一件事"还管用，尺子就反了。
    """

    soothe_log: list[dict[str, Any]] = field(default_factory=list)
    """安抚通道（低潮时的抱抱、有人听懂她）最近的几次记录。

    这条路不吃"聊天推效价"的当天额度，改用"每小时/每天几次"限流，所以要自己记账。
    每项形如 ``{"at": 时间戳, "day": "2026-09-29", "kind": "soothed|understood"}``。
    """

    ext_data: dict[str, dict[str, Any]] = field(default_factory=dict)
    """扩展包自己的状态（按扩展名分格）。

    主插件既不读也不渲染这里，谁挂上来的谁自己管——没装扩展时它就是空的。
    """

    # ---------------- 能力值（跑团味的那四项） ----------------

    abilities: dict[str, float] = field(default_factory=dict)
    """体力 / 智力 / 灵巧 / 心性。慢变量：只由事件结果改变，几乎不随时间衰减。

    和「精力」不是一回事：精力睡一觉就回来，能力值是她的底子。空字典 = 用默认值。
    """

    ability_day: str = ""
    """能力值「今天已经变了多少」所属的日期（跨天清零）。"""

    ability_spent_today: dict[str, float] = field(default_factory=dict)
    """每项能力值今天的累计变化（带符号），用来卡每日上限。"""

    # ---------------- 事件与线索 ----------------

    event_threads: list[dict[str, Any]] = field(default_factory=list)
    """事件线索：一条线索是一串事件（初始 → 她怎么选 → 结果 → 后续）。"""

    event_last_roll_at: float = 0.0
    """上一次掷「要不要发生事件」的真实时间（按小时期望次数折算概率）。"""

    last_event_started_at: float = 0.0
    """上一件事是什么时候开始的（真实时间）：用来保证两件事之间的最小间隔。"""

    node_since: float = 0.0
    """她是什么时候到这个地点的（真实时间）：待够一段时间才算"在这儿生活"。"""

    event_recent_titles: list[str] = field(default_factory=list)
    """最近发生过的事件标题：生成新事件时喂回去做去重。"""

    event_recent_genres: list[str] = field(default_factory=list)
    """最近用过的事件题材：代码掷题材时先把最近的排除掉。"""

    event_genre_at: dict[str, float] = field(default_factory=dict)
    """每个题材上一次用是什么时候（真实时间）：题材也有冷却，不然连着几件都是一个味。"""

    greet_pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    """刚冒头的"好久没来"的人：``uid -> {name, gap_hours, at}``。提示词读过一次就清掉。"""

    greet_day: str = ""
    greet_count: int = 0
    """今天这样打过几次招呼（整天合计）。"""

    greet_done: dict[str, str] = field(default_factory=dict)
    """``uid -> 日期``：这天已经招呼过他，别连着说两次。"""

    ask_log: dict[str, dict[str, float]] = field(default_factory=dict)
    """``uid -> {事项: 上次问的时间}``：同一件事别追着问。"""

    ask_day: str = ""
    ask_count: int = 0
    """今天问过几件（整天合计）。"""

    heart_knots: list[dict[str, Any]] = field(default_factory=list)
    """她心里搁着的事：``[{text, about, strength, since, until}]``。

    不推进、不完成任务，只是挂着——会淡、会过期，说出来掉得快些。
    """

    last_user_tone: str = ""
    """上一轮主模型判的"对方口吻"（praise / hug / attack / normal）。

    两个用处：声音样例按它挑场景组（它认得反话，关键词表认不出）；
    日志里也能对照"她是不是把一句挤对当成了夸奖"。
    """

    schedule_delays: dict[str, dict[str, Any]] = field(default_factory=dict)
    schedule_last_fired: dict[str, float] = field(default_factory=dict)
    """每条日程上次真正跑起来的时间戳（按日程 id 记）：提示词里要写清"上次什么时候做的"。"""
    """日程被事件推迟的记录：``日程 id -> {count, minutes, until, day, slot}``。

    推迟有上限（次数 + 总时长），到顶就必须执行——睡觉这件事没有商量余地。
    """

    stay_up_until: int = 0
    """为了事件熬夜到什么时候（世界时间）：这段时间里精力掉得更快。"""

    event_digest: list[str] = field(default_factory=list)
    """这段时间发生的事（一行一条）；动作完成续说时带出来，然后清空。"""

    pending_help: dict[str, Any] = field(default_factory=dict)
    """悬而未决的求助。

    `{"thread_id", "state": "active"|"idle", "asked_at", "active_until",
      "idle_until", "reminder_sent", "suggestions": [...]}`。
    等待**不是冻结状态**：她照常做自己的事、照常接群聊，这件事挂在后台。
    """

    boredom: float = 0.3
    desire: float = 0.2
    """欲求：对**亲密的肢体接触**的需求（摸摸头、抱抱、亲亲、蹭蹭、靠着）。

    跟孤独感是两回事：孤独是"想有人说话、有人在"，欲求是"想被实实在在碰一下"。
    没人碰她它就慢慢涨，被亲近一次就落一截——所以它是**身体接触**那一路的驱力，
    亲密动作（动作自己的「亲密程度」不为 0）会来满足它。
    """
    desire_slept: bool = False
    """这一觉睡过没有：睡醒时欲求要按「睡醒系数」松一截，只做一次。"""
    current_action: dict[str, Any] | None = None
    current_plan: dict[str, Any] | None = None
    last_plan: dict[str, Any] = field(default_factory=dict)
    """最近一次生成的计划（做完了也留着，提示词里会带上，免得她每轮重新打算）。"""
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    thoughts: list[dict[str, Any]] = field(default_factory=list)
    last_reasoning: dict[str, Any] = field(default_factory=dict)
    """最近一次「推理草稿」（env/state/mood/who/intent + 时间戳）。

    只用于编辑器展示与排查：她刚才看到了什么、打算怎么做。
    不外发、不计动作数、不写记忆。
    """

    recent_chat: list[dict[str, Any]] = field(default_factory=list)
    """最近群聊内容（持久化，重启后仍在）。带上限，按配置决定丢弃还是压缩。"""

    pending_images: list[dict[str, Any]] = field(default_factory=list)
    """自上次回复以来收到的图片（没配转述模型时，会把它们直接交给多模态主模型）。

    只存地址和时间，回复一次就清空；上限由「全局设置 → 上下文 → 图片上限」决定。
    """

    chat_summary: str = ""
    """较早群聊的压缩摘要（`chat_overflow = compress` 时才会有内容）。

    老存档只有这一份**全局**摘要；新版改用按会话分开的 :attr:`chat_summaries`，
    这里只在读取旧存档时当回落。
    """

    chat_summary_at: float = 0.0
    """上次压缩摘要的时间戳。"""

    chat_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    """按会话分开的"更早聊过的"：``{会话 id: {"text": 摘要, "at": 时间戳}}``。

    同一个会话组里的群和私聊共用一份状态，摘要也要分会话存——不然群里聊的和私聊聊的
    会被压进同一段，她就分不清哪句是哪儿的。
    """

    chat_dropped: list[dict[str, Any]] = field(default_factory=list)
    """被留档上限顶掉、还没并进摘要的那几条（配置成"压缩"时才会攒）。

    没有它的话，聊得快的时候"还没轮到压缩就已经被丢掉"的内容会彻底消失，
    她之后就再也想不起那一段。
    """

    chat_overflow_warned_at: float = 0.0
    """上次提醒"留档满了、最早的在被丢掉"的时间（配置成"直接丢弃"时才用）。"""

    chat_replied_until: float = 0.0
    """「已回应水位线」：这个时间点之前的群聊不再进"最近在聊"（留档仍然保留）。"""

    chat_note: str = ""
    """「刚才你们在聊什么」（模型顺手写的），老存档只有这一份全局的。

    新版按会话分开存在 :attr:`chat_notes` 里，这里只在读取旧存档时当回落。
    """

    chat_notes: dict[str, dict[str, Any]] = field(default_factory=dict)
    """按会话分开的"刚才在聊什么"：``{会话 id: {"text": …, "at": 时间戳}}``。

    一个会话组里群和私聊共用一份状态，这一句也必须分开存——不然私聊刚写的背景
    会在群里被当成"这里刚才在聊的事"。
    """

    chat_previews: dict[str, dict[str, Any]] = field(default_factory=dict)
    """按会话分开的"她刚回应过的那批"的概览（留档被裁掉时的兜底背景）。"""

    chat_preview: str = ""
    """她刚回应过的那一批群聊的概览（谁说了什么、她回了什么）。

    每次她开口后由规则生成，下一轮作为"之前的群聊"背景出现——
    这些内容已经回应过，所以不再原样重复给她，只留一条概览。
    """

    recent_replies: list[dict[str, Any]] = field(default_factory=list)
    """她最近说过的几句话：``[{"text": 原文, "session": 在哪个会话说的}]``。

    给"别用同样的句式"那一段用；带上会话是因为她在几个地方说话，
    同一个地方别重复，别处说过的不一定要避讳。
    """

    chat_seq: int = 0
    """群聊留档的递增序号（时间戳只有秒，判断"谁是新消息"不够用）。"""

    chat_replied_seq: int = 0
    """已回应水位线（序号版）：大于它的群聊才是"还没回应过的"。"""

    chat_watermarks: dict[str, dict[str, Any]] = field(default_factory=dict)
    """按来源会话分开的已回应水位线：``{session_id: {"until": ts, "seq": n}}``。

    她同时在几个地方说话时，在一个地方回过话不该把另一个地方的留言也标成"答过了"。
    """

    user_presence: dict[str, dict[str, Any]] = field(default_factory=dict)
    miss: dict[str, float] = field(default_factory=dict)
    """对某个人的想念：``{user_id: 0~1}``。

    群里再热闹也不算"有人陪她"，只有直接跟她说话才会清零——所以群里刷一天，
    她也可能攒出"有点想主人了"，从而主动去找他。
    """

    miss_ready_at: dict[str, float] = field(default_factory=dict)
    """``{user_id: 时间戳}``：这个人下次"开始想他"是从什么时候算起。

    每次清零（他跟她说话了、或者她刚去找过他）都重新随机一个等待时长，
    所以不会"每隔固定一段时间就去找一次"。
    """

    consolidate_cursor: float = 0.0
    """上次睡眠整理的时刻（同一段睡眠只整理一次；重启后也不会立刻重跑）。"""

    last_dream: str = ""
    """最近一次做的梦（小睡也可能做）：她想提就提一句。"""

    review_visits: list[dict[str, Any]] = field(default_factory=list)
    """回想回访的台账：``[{at, day, memory_id, user_id}]``（每天 / 每人的配额按它算）。"""

    pending_review: str = ""
    """刚想起的一两件事：写进提示词，等被读过就清掉（同一件事只提一次）。"""
    session_activity: dict[str, dict[str, Any]] = field(default_factory=dict)
    """每个会话最近有没有人跟她说话：``{session_id: {at, user_id, user_name}}``。

    分过组时几个群 / 私聊共用一份状态，她就用这本册子知道"哪儿有人"。
    """

    external_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    """别的插件的状态：``{槽名: {"text", "label", "at", "expires_at"}}``。

    例如生图插件给的「今日穿搭」：指令回来之后存这儿，提示词里就会一直带着，
    不会说完就忘。
    """

    bot_base_nickname: str = ""
    bot_current_nickname: str = ""
    bot_nickname_locked: bool = False
    nickname_fail_count: int = 0
    """群名片连续失败次数：用来做退避（挂掉的协议端不会把日志刷爆）。"""
    unanswered_count: int = 0
    last_engagement_time: int = 0
    awaiting_reply: bool = False
    cooldown_until: int = 0
    proactive_block_until: int = 0
    """在这个 tick 之前不要主动开口（刚回过话之后的冷却）。被动回复不受影响。"""
    mood_override_until: int = 0
    mood_cause: str = ""
    """这会儿的心情是**为什么**（「被哄了一下」「说话没人接」）：提示词与状态页都会带上。"""

    mood_cause_at: float = 0.0
    """上面那句是什么时候记的：太旧的来源不再挂在"现在的心情"上。"""

    day_mood: str = ""
    """今天的基调（懒散 / 活跃 / 黏人 / 想独处 / 说不上来）：每天掷一次，
    改的是各条数值曲线走得快慢，不是她的性格。"""

    day_mood_day: str = ""
    """上面那条基调是哪一天的（YYYY-MM-DD）：换天重掷一次。"""

    last_user_activity_at: float = 0.0
    last_nickname_update_at: float = 0.0
    sleep_reply_at: float = 0.0
    sleep_skip_at: float = 0.0
    """上一次记录「她在睡觉所以没回」的时间（用来给日志节流）。"""

    sleep_skip_count: int = 0
    """距离上次记录又挡下了几条消息。"""
    """上次用「她睡着了」的固定文案回话的时间（冷却用，0 表示没回过）。"""
    no_sleep_until: int = 0
    """刚被叫醒的保护期：世界时间在此之前，规则不再安排她回去睡。"""

    last_outdoor_at: float = 0.0
    """上一次在外面（不在家那一片）是什么时候：久了她会想出去走走。"""

    outdoor_hint_at: float = 0.0
    """上一次在提示词里提醒她"可以出去走走"是什么时候（别每轮都念）。"""

    cold_start_done: bool = False
    pending_memory: list[dict[str, Any]] = field(default_factory=list)
    """还没总结的对话片段：攒够条数、聊完、或她离开这个地点时，压成一条记忆。"""
    pending_memory_node: str = ""
    """上面那些片段发生在哪个地点。"""
    memory_hints: list[str] = field(default_factory=list)
    """大模型顺手给的一句话总结，作为写记忆时的提示。"""
    memory_flush_at: float = 0.0
    """上次写对话记忆的时间。"""
    memory_flush_wanted: bool = False
    """硬触发（换地点）标记：下一次 tick 立刻把这段总结掉。"""
    wake_note: str = ""
    """刚被叫醒时给提示词的一句说明（下一次回复用完即弃，见 wake_note_until）。"""
    wake_note_until: int = 0
    """上面那句说明的有效期（世界时间）。"""

    sleep_started_at: int = 0
    """这一段睡眠是从哪个 tick 开始的（算"睡了多久"，起床气按它结账）。"""

    drowsy_sleep_step: dict[str, Any] = field(default_factory=dict)
    """临睡期里**存着的那一步睡觉**：安静下来之后就拿它真的躺下。

    睡前先进临睡期（可能又说了两句、可能被消息打断），所以这一步不能当场执行，
    先揣着——不然"说完晚安立刻睡死"看着像关机，不像人。
    """

    drowsy_started_world_time: int = 0
    """临睡期从第几个 tick 开始：兜底用（太久了就赶紧睡，别熬到天亮）。"""

    drowsy_started_at: float = 0.0
    """临睡期开始的真实时间："安静了多久"要从这一刻算起（不能拿她上次说话的时间
    当起点——那样一进临睡期就立刻睡着了）。"""

    drowsy_day: str = ""
    """上一次进临睡期是哪天：一晚只走一次临睡期（回笼觉就直接躺）。"""

    goodnight_day: str = ""
    """上一次「睡前要不要说晚安」是哪天：同一段睡眠只问一次，别一晚问三遍。"""

    goodmorning_day: str = ""
    """上一次「睡醒要不要说早安」是哪天。"""

    startled_count: int = 0
    """这一段睡眠里被吵醒过几次（``sleep.max_per_sleep`` 管着上限）。"""
    sleep_noise_at: float = 0.0
    """"吵"的统计窗口起点（真实时间）。"""
    sleep_noise_count: int = 0
    """上面这个窗口里，她被挡下了几条消息。"""
    sleep_named_count: int = 0
    """上面这个窗口里，有几次是明确冲她来的（@ 她 / 私聊 / 叫她名字）。"""
    startled_until: int = 0
    """迷糊惊醒的窗口（世界时间）：这段时间里她还算睡着，但可以回两句。"""
    startled_note: str = ""
    """迷糊惊醒时给提示词的一句说明。"""
    grumpy_until: int = 0
    """起床气持续到哪个 tick（被吵醒、或者没睡够就醒）。"""
    grumpy_note: str = ""
    """起床气的一句说明（给提示词看）。"""

    miss_push_day: str = ""
    """「想他想到主动去找他」这一天记到哪天了（软推每天有次数上限）。"""
    miss_push_count: int = 0
    """上面那一天已经软推过几次。"""
    desire_push_day: str = ""
    """「想被碰一碰」这一天记到哪天了（交给大模型自己安排的那条）。"""
    desire_push_count: int = 0
    """上面那一天已经推过几次。"""
    desire_push_at: float = 0.0
    """上一次「想被碰一碰」是什么时候：两次之间要隔一会儿，别每拍都问她一遍。"""
    low_energy_since: int = 0
    high_loneliness_since: int = 0
    autonomous_count_hour: int = 0
    autonomous_hour_marker: int = 0
    reply_count_hour: int = 0
    """本小时已经回过几条被动回复（被 @ / 私聊 / 明确对她说）。"""

    reply_hour_marker: int = 0
    """上面那个计数属于哪个小时（世界时间的整点序号）。"""

    action_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    """动作使用计数：``{动作 id: {"d:2026-09-21": 3, ...}}``（按天 / 周 / 月分别记账）。"""
    share_count_hour: int = 0
    share_hour_marker: int = 0
    llm_plan_count_hour: int = 0
    llm_plan_hour_marker: int = 0
    llm_text_count_hour: int = 0
    llm_text_hour_marker: int = 0
    tool_param_count_hour: int = 0
    tool_param_hour_marker: int = 0
    desire_relief_hour_sum: float = 0.0
    """这一小时里"被亲昵卸掉的欲求"累计了多少（配合 ``desire_relief_hour_cap``）。"""

    desire_relief_hour_marker: int = 0
    """上面那个累计数属于哪个小时（换小时就清零）。"""

    negative_valence_at: float = 0.0
    """上一次"负面情绪压效价"是什么时候（同一轮里要合起来看）。"""

    negative_valence_sum: float = 0.0
    """这一轮负面已经压掉了多少效价（连着两笔也不会超上限）。"""

    negative_tone_streak: int = 0
    """连着被伤了几次（第一次最疼，之后递减）。"""

    negative_tone_at: float = 0.0
    """上一次被伤是什么时候（隔久了重新算"第一次"）。"""

    arrival_count_hour: int = 0
    arrival_hour_marker: int = 0
    pending_arrival: bool = False
    """刚从别的地方走到这里（tick 循环会就地做一次决策）。"""
    last_llm_plan_at: float = 0.0
    last_forced_plan_at: float = 0.0
    last_forced_flag: str = ""
    last_interject_at: float = 0.0
    schedule_cursor: float = 0.0
    """日程检查游标（现实时间戳）。只处理「游标之后、现在之前」到点的日程。

    以前是拿 `HH:MM` 精确匹配当前这一分钟，tick 一旦漂移就会整分钟跳过去、
    那一条日程当天再也不会触发；改成游标之后，跳过的分钟会在下一次检查时补上。
    """

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---------------- 序列化 ----------------

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None, session_id: str) -> "WorldState":
        data = dict(payload or {})
        # 旧存档里的字段名是 social（社交欲），现在改叫 affect（心潮）
        if "affect" not in data and "social" in data:
            data["affect"] = data["social"]
        data["session_id"] = session_id
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        state = cls(**filtered)  # type: ignore[arg-type]
        state.clamp()
        if not isinstance(state.current_action, dict):
            state.current_action = None
        if not isinstance(state.current_plan, dict):
            state.current_plan = None
        if not isinstance(state.last_plan, dict):
            state.last_plan = {}
        if not isinstance(state.last_reasoning, dict):
            state.last_reasoning = {}
        # 老存档只有一份全局摘要：挂到存档会话名下，下次压缩自然按会话替换
        if not isinstance(state.chat_summaries, dict):
            state.chat_summaries = {}
        legacy = str(state.chat_summary or "").strip()
        if legacy and not state.chat_summaries:
            state.chat_summaries = {
                str(session_id): {
                    "text": legacy,
                    "at": float(state.chat_summary_at or 0.0),
                }
            }
        # 「刚才在聊什么」/「她刚回应过的那批」以前也是全局一份：同样挂到存档会话名下
        for field_name, legacy_field in (
            ("chat_notes", "chat_note"),
            ("chat_previews", "chat_preview"),
        ):
            rows = getattr(state, field_name, None)
            if not isinstance(rows, dict):
                rows = {}
                setattr(state, field_name, rows)
            text = str(getattr(state, legacy_field, "") or "").strip()
            if text and not rows:
                rows[str(session_id)] = {"text": text, "at": 0.0}
        return state

    def note_reasoning(self, reasoning: dict[str, Any] | None, *, source: str = "") -> None:
        """记下最近一次推理草稿（编辑器「实时状态」会显示它）。"""

        if not reasoning:
            return
        payload = {str(k): str(v) for k, v in dict(reasoning).items() if str(v).strip()}
        if not payload:
            return
        payload["_source"] = source
        payload["_at"] = int(self.world_time)
        self.last_reasoning = payload

    # ---------------- 便捷方法 ----------------

    def clamp(self) -> None:
        self.energy = _clamp01(self.energy, 0.6)
        self.loneliness = _clamp01(self.loneliness, 0.5)
        self.curiosity = _clamp01(self.curiosity, 0.5)
        self.affect = _clamp01(self.affect, 0.3)
        self.valence = _clamp01(self.valence, 0.5)
        try:
            offset = float(self.valence_offset)
        except (TypeError, ValueError):
            offset = 0.0
        self.valence_offset = max(-0.5, min(0.5, offset))
        self.boredom = _clamp01(self.boredom, 0.3)
        self.desire = _clamp01(self.desire, 0.2)
        self.desire_slept = bool(self.desire_slept)
        self.unanswered_count = max(0, int(self.unanswered_count))
        self.ignored_streak = max(0, int(self.ignored_streak or 0))
        if not isinstance(self.praise_streak, dict):
            self.praise_streak = {}
        else:
            self.praise_streak = {
                str(key): max(0, int(value or 0))
                for key, value in self.praise_streak.items()
            }
        if not isinstance(self.praise_streak_at, dict):
            self.praise_streak_at = {}
        else:
            self.praise_streak_at = {
                str(key): float(value or 0.0)
                for key, value in self.praise_streak_at.items()
            }
        self.chat_day = str(self.chat_day or "")
        if not isinstance(self.soothe_log, list):
            self.soothe_log = []
        else:
            self.soothe_log = [
                dict(item) for item in self.soothe_log if isinstance(item, dict)
            ][-30:]
        if not isinstance(self.ext_data, dict):
            self.ext_data = {}
        else:
            self.ext_data = {
                str(key): dict(value)
                for key, value in self.ext_data.items()
                if isinstance(value, dict)
            }
        if not isinstance(self.last_voice_samples, list):
            self.last_voice_samples = []
        else:
            self.last_voice_samples = [str(item) for item in self.last_voice_samples]
        if not isinstance(self.open_topics, list):
            self.open_topics = []
        else:
            self.open_topics = [
                dict(item) for item in self.open_topics if isinstance(item, dict)
            ][:8]
        if not isinstance(self.grudges, list):
            self.grudges = []
        else:
            self.grudges = [
                dict(item) for item in self.grudges if isinstance(item, dict)
            ][:8]
        self.grudge_day = str(self.grudge_day or "")
        self.grudge_count = max(0, int(self.grudge_count or 0))
        if not isinstance(self.own_topics, list):
            self.own_topics = []
        else:
            self.own_topics = [
                dict(item) for item in self.own_topics if isinstance(item, dict)
            ][:8]
        try:
            self.chat_valence_spent = float(self.chat_valence_spent or 0.0)
        except (TypeError, ValueError):
            self.chat_valence_spent = 0.0
        self.storm = bool(self.storm)
        self.nickname_fail_count = max(0, int(self.nickname_fail_count or 0))
        self.world_time = max(0, int(self.world_time))
        self.cooldown_until = max(0, int(self.cooldown_until))
        self.mood_override_until = max(0, int(self.mood_override_until))
        self.day_mood = str(self.day_mood or "")
        self.day_mood_day = str(self.day_mood_day or "")
        self.desire_push_day = str(self.desire_push_day or "")
        self.desire_push_count = max(0, int(self.desire_push_count or 0))
        try:
            self.desire_push_at = float(self.desire_push_at or 0.0)
        except (TypeError, ValueError):
            self.desire_push_at = 0.0
        self.no_sleep_until = max(0, int(self.no_sleep_until))
        try:
            self.last_outdoor_at = max(0.0, float(self.last_outdoor_at or 0.0))
        except (TypeError, ValueError):
            self.last_outdoor_at = 0.0
        try:
            self.outdoor_hint_at = max(0.0, float(self.outdoor_hint_at or 0.0))
        except (TypeError, ValueError):
            self.outdoor_hint_at = 0.0
        for name in ("negative_valence_at", "negative_tone_at"):
            try:
                setattr(self, name, max(0.0, float(getattr(self, name, 0.0) or 0.0)))
            except (TypeError, ValueError):
                setattr(self, name, 0.0)
        try:
            self.negative_valence_sum = max(0.0, float(self.negative_valence_sum or 0.0))
        except (TypeError, ValueError):
            self.negative_valence_sum = 0.0
        self.negative_tone_streak = max(0, int(self.negative_tone_streak or 0))
        self.memory_flush_wanted = bool(self.memory_flush_wanted)
        self.wake_note_until = max(0, int(self.wake_note_until))
        self.sleep_started_at = max(0, int(self.sleep_started_at or 0))
        if not isinstance(self.drowsy_sleep_step, dict):
            self.drowsy_sleep_step = {}
        self.drowsy_started_world_time = max(0, int(self.drowsy_started_world_time or 0))
        try:
            self.drowsy_started_at = float(self.drowsy_started_at or 0.0)
        except (TypeError, ValueError):
            self.drowsy_started_at = 0.0
        self.goodnight_day = str(self.goodnight_day or "")
        self.goodmorning_day = str(self.goodmorning_day or "")
        self.drowsy_day = str(self.drowsy_day or "")
        self.startled_count = max(0, int(self.startled_count or 0))
        self.sleep_noise_count = max(0, int(self.sleep_noise_count or 0))
        self.sleep_named_count = max(0, int(self.sleep_named_count or 0))
        self.startled_until = max(0, int(self.startled_until or 0))
        self.grumpy_until = max(0, int(self.grumpy_until or 0))
        if not isinstance(self.interject_stats, dict):
            self.interject_stats = {}
        else:
            self.interject_stats = {
                str(key): max(0, int(value or 0))
                for key, value in self.interject_stats.items()
            }
        if not isinstance(self.session_activity, dict):
            self.session_activity = {}
        else:
            # 每个会话只留"最近一次"：条数不多，但别让脏存档把它撑成别的形状
            self.session_activity = {
                str(key): dict(value)
                for key, value in self.session_activity.items()
                if isinstance(value, dict)
            }
        if not isinstance(self.chat_watermarks, dict):
            self.chat_watermarks = {}
        else:
            self.chat_watermarks = {
                str(key): dict(value)
                for key, value in self.chat_watermarks.items()
                if isinstance(value, dict)
            }
        if not isinstance(self.miss, dict):
            self.miss = {}
        else:
            # 想念值只留 0~1 的数字，脏存档不该把它撑成别的形状
            self.miss = {
                str(key): max(0.0, min(1.0, float(value or 0.0)))
                for key, value in self.miss.items()
                if isinstance(value, (int, float))
            }
        if not isinstance(self.review_visits, list):
            self.review_visits = []
        else:
            self.review_visits = [
                dict(item) for item in self.review_visits if isinstance(item, dict)
            ][-100:]
        if not isinstance(self.external_state, dict):
            self.external_state = {}
        else:
            self.external_state = {
                str(key): dict(value)
                for key, value in self.external_state.items()
                if isinstance(value, dict)
            }
        if not isinstance(self.chat_dropped, list):
            self.chat_dropped = []
        else:
            self.chat_dropped = [
                dict(item) for item in self.chat_dropped if isinstance(item, dict)
            ][-CHAT_DROPPED_KEEP:]
        self.interject_hour_marker = max(0, int(self.interject_hour_marker or 0))
        self.mood_cause = " ".join(str(self.mood_cause or "").split())
        self.mood_cause_at = max(0.0, float(self.mood_cause_at or 0.0))
        if not isinstance(self.action_usage, dict):
            self.action_usage = {}
        else:
            self.action_usage = {
                str(action_id): {
                    str(period): max(0, int(value or 0))
                    for period, value in (table or {}).items()
                }
                for action_id, table in self.action_usage.items()
                if isinstance(table, dict)
            }
        if not isinstance(self.recent_replies, list):
            self.recent_replies = []
        else:
            # 老存档里存的是纯字符串：读的时候补成 {text, session} 的形状
            normalized: list[dict[str, Any]] = []
            for item in self.recent_replies:
                if isinstance(item, dict):
                    text = " ".join(str(item.get("text") or "").split())
                    session = str(item.get("session") or "")
                else:
                    text = " ".join(str(item or "").split())
                    session = ""
                if text:
                    normalized.append({"text": text, "session": session})
            self.recent_replies = normalized[-6:]
        # 能力值：老存档里没有这一项，按默认值补齐；异常的项重算
        self.abilities = normalize_abilities(self.abilities)
        self.ability_day = str(self.ability_day or "")
        if not isinstance(self.ability_spent_today, dict):
            self.ability_spent_today = {}
        if not isinstance(self.event_threads, list):
            self.event_threads = []
        else:
            self.event_threads = [
                item for item in self.event_threads if isinstance(item, dict)
            ][-6:]
        if not isinstance(self.event_recent_titles, list):
            self.event_recent_titles = []
        else:
            self.event_recent_titles = [
                " ".join(str(item).split()) for item in self.event_recent_titles if str(item).strip()
            ][-10:]
        if not isinstance(self.event_recent_genres, list):
            self.event_recent_genres = []
        else:
            self.event_recent_genres = [
                str(item) for item in self.event_recent_genres if str(item).strip()
            ][-8:]
        if not isinstance(self.event_genre_at, dict):
            self.event_genre_at = {}
        else:
            cleaned: dict[str, float] = {}
            for name, stamp in self.event_genre_at.items():
                key = str(name or "").strip()
                if not key:
                    continue
                try:
                    cleaned[key] = float(stamp or 0.0)
                except (TypeError, ValueError):
                    continue
            self.event_genre_at = cleaned
        if not isinstance(self.schedule_delays, dict):
            self.schedule_delays = {}
        if not isinstance(self.schedule_last_fired, dict):
            self.schedule_last_fired = {}
        else:
            cleaned_last: dict[str, float] = {}
            for key, stamp in self.schedule_last_fired.items():
                try:
                    cleaned_last[str(key)] = float(stamp or 0.0)
                except (TypeError, ValueError):
                    continue
            self.schedule_last_fired = cleaned_last
        self.stay_up_until = max(0, int(self.stay_up_until or 0))
        if not isinstance(self.event_digest, list):
            self.event_digest = []
        else:
            self.event_digest = [
                " ".join(str(item).split()) for item in self.event_digest if str(item).strip()
            ][-40:]
        # 上一轮的口吻：只认白名单，写坏的当没判过
        tone = str(self.last_user_tone or "").strip().lower()
        self.last_user_tone = (
            tone if tone in ("praise", "hug", "attack", "refuse", "normal") else ""
        )
        # 打招呼 / 主动问的账本：清了坏的，别的照原样
        if not isinstance(self.greet_pending, dict):
            self.greet_pending = {}
        if not isinstance(self.greet_done, dict):
            self.greet_done = {}
        self.greet_count = max(0, int(self.greet_count or 0))
        if not isinstance(self.ask_log, dict):
            self.ask_log = {}
        self.ask_count = max(0, int(self.ask_count or 0))
        knots: list[dict[str, Any]] = []
        for item in list(self.heart_knots or []):
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            try:
                strength = float(item.get("strength") or 0.0)
            except (TypeError, ValueError):
                strength = 0.0
            knots.append(
                {
                    **item,
                    "text": text[:60],
                    "about": str(item.get("about") or "")[:20],
                    "strength": max(0.0, min(1.0, strength)),
                }
            )
        self.heart_knots = knots[-4:]
        if not isinstance(self.pending_help, dict):
            self.pending_help = {}
        self.node_since = max(0.0, float(self.node_since or 0.0))
        if not isinstance(self.pending_memory, list):
            self.pending_memory = []
        else:
            self.pending_memory = [
                item for item in self.pending_memory if isinstance(item, dict)
            ][-60:]
        if not isinstance(self.memory_hints, list):
            self.memory_hints = []
        else:
            self.memory_hints = [str(item) for item in self.memory_hints][-5:]

    # 兼容旧字段名：老版本叫「社交欲（social）」，语义已经换成「心潮（affect）」。
    @property
    def social(self) -> float:
        return self.affect

    @social.setter
    def social(self, value: Any) -> None:
        self.affect = _clamp01(value, 0.3)

    @property
    def is_sleeping(self) -> bool:
        return self.state in (STATE_SLEEPING, STATE_NAPPING)

    @property
    def is_busy(self) -> bool:
        """正在执行不可打断的持续动作。"""

        action = self.current_action or {}
        if not action:
            return False
        return not bool(action.get("interruptible", True))

    @property
    def busy_with(self) -> str:
        return str((self.current_action or {}).get("type", ""))

    def add_event(self, kind: str, detail: dict[str, Any], keep: int = 40) -> None:
        self.recent_events.append(
            {"kind": kind, "detail": detail, "world_time": self.world_time}
        )
        if len(self.recent_events) > keep:
            self.recent_events = self.recent_events[-keep:]

    def add_thought(self, content: str, node_id: str = "", keep: int = 20) -> None:
        self.thoughts.append(
            {"content": content, "node_id": node_id or self.node_id, "world_time": self.world_time}
        )
        if len(self.thoughts) > keep:
            self.thoughts = self.thoughts[-keep:]

    def touch_user(
        self,
        user_id: str,
        *,
        name: str = "",
        anchor: str = "",
        now: float | None = None,
        max_tracked: int = 100,
    ) -> None:
        """更新用户存在感记录。"""

        record = self.user_presence.get(user_id, {})
        record.update(
            {
                "user_id": user_id,
                "name": name or record.get("name", "") or user_id,
                "presence": "active",
                "believed_anchor": anchor or record.get("believed_anchor", "topic_center"),
                "last_seen": now if now is not None else time.time(),
                "world_time": self.world_time,
            }
        )
        self.user_presence[user_id] = record
        if len(self.user_presence) > max_tracked:
            ordered = sorted(
                self.user_presence.items(),
                key=lambda item: float(item[1].get("last_seen", 0)),
                reverse=True,
            )[:max_tracked]
            self.user_presence = dict(ordered)

    def recent_active_users(self, limit: int = 5) -> list[dict[str, Any]]:
        items = sorted(
            self.user_presence.values(),
            key=lambda item: float(item.get("last_seen", 0)),
            reverse=True,
        )
        return items[:limit]

    def note_chat(
        self,
        *,
        user_id: str,
        name: str,
        text: str,
        now: float,
        keep: int = 12,
        is_self: bool = False,
        internal: bool = False,
        images: Any = None,
        buffer_dropped: bool = False,
        origin: str = "",
        at: Any = None,
        reply_to: Any = None,
        addressing: str = "",
    ) -> None:
        """记录一条群聊内容（用于判断"大家在聊什么"）。

        ``images`` 是这条消息带的图片地址：留着是为了「聊天记录里的图直接交给
        多模态主模型」那一步——她能看到"这条带的是哪张图"。

        ``buffer_dropped``：被上限顶掉的那几条先攒到 :attr:`chat_dropped`，
        等下一次压缩并进摘要（只有配置成"压成摘要"时才需要传 True）。

        ``origin``：这条消息是在哪个会话说的。**要在记下来的同时就给**——
        留档是按会话各自限额裁的，晚一步标就会算到别的会话头上。

        ``internal``：插件自己写进去的"她身上发生的事"（事件的结果）。
        它不是谁说的话，也不该被当成"还没回的消息"——渲染时会标出来，
        水位线判定也会直接跳过它。
        """

        clean = (text or "").strip()
        if not clean:
            return
        # 同一条消息可能同时经过"旁观监听"和"LLM 请求"两个钩子，去重避免上下文里重复出现
        # （两个钩子拿到的文本可能差一点点：一个带 @ 前缀、一个被管线改写过，
        #   所以不是逐字比较，而是"同一个人、短时间内、内容基本一样"。）
        for item in reversed(self.recent_chat[-4:]):
            if str(item.get("user_id")) != str(user_id):
                continue
            age = now - float(item.get("at", 0))
            old = str(item.get("text") or "")
            if age <= _CHAT_DEDUPE_SECONDS and _same_chat_text(old, clean):
                return
            # 一份带注释、一份不带（例如「亲亲」和「亲亲［这条消息 @ 了：你（…）］」）：
            # 把注释切掉再比一次。上面那条规则要求最少 4 个字，短消息靠它认不出来，
            # 于是同一句话会存两遍，她就会以为对方说了两次。
            trimmed_old, trimmed_new = chat_core_text(old), chat_core_text(clean)
            if (
                age <= _CHAT_DEDUPE_ANNOTATED_SECONDS
                and (trimmed_old != old or trimmed_new != clean)
                and _same_chat_text(trimmed_old, trimmed_new)
            ):
                return
            break
        limit = (
            _FORWARD_TEXT_CHARS if FORWARD_SUMMARY_MARK in clean else _CHAT_TEXT_CHARS
        )
        # 注释（@ 了谁、引用了什么）不跟着正文一起掐：它正是"这句冲谁说的"的关键，
        # 掐掉半句（"（其中 老普机器人… 还有 12 字没显示"）反而会让她误会
        body_text, note_text = split_annotation(clean)
        kept = body_text[:limit]
        if note_text:
            kept = f"{kept}{note_text[:ANNOTATION_KEEP_CHARS]}"
        item = {
            "user_id": user_id,
            "name": name or user_id,
            "text": kept,
            "at": now,
            "world_time": self.world_time,
            "is_self": bool(is_self),
            "seq": self.chat_seq + 1,
        }
        if internal:
            item["internal"] = True
        if str(origin or "").strip():
            item["origin"] = str(origin).strip()
        # 「这句是冲谁说的」：@ 名单与引用对象结构化留一份，渲染聊天记录时画箭头用
        at_list = [
            {
                "id": str(entry.get("id") or ""),
                "name": str(entry.get("name") or ""),
                "self": bool(entry.get("self")),
            }
            for entry in list(at or [])
            if isinstance(entry, dict)
        ]
        if at_list:
            # 注意别用 "at"：那是这条消息的时间戳
            item["at_targets"] = at_list
        if isinstance(reply_to, dict) and (reply_to.get("id") or reply_to.get("name")):
            item["reply_to"] = {
                "id": str(reply_to.get("id") or ""),
                "name": str(reply_to.get("name") or ""),
            }
        if str(addressing or "") in ("me", "others"):
            item["addressing"] = str(addressing)
        refs = _chat_image_refs(images)
        if refs:
            item["images"] = refs
        self.recent_chat.append(item)
        self.chat_seq = int(self.chat_seq) + 1
        dropped = self._trim_chat(keep)
        if dropped:
            wanted = bool(getattr(self, "buffer_dropped_chat", buffer_dropped))
            if wanted:
                self.chat_dropped = [*self.chat_dropped, *dropped][-CHAT_DROPPED_KEEP:]

    def _trim_chat(self, keep: int) -> list[dict[str, Any]]:
        """留档超上限时裁掉最早的那几条，**按会话各自裁**。

        以前是整个会话组一起裁：一个热闹的群能把私聊的留档挤光。
        现在每个会话各留 ``keep`` 条，互不影响。
        """

        keep = max(1, int(keep))
        buckets: dict[str, list[dict[str, Any]]] = {}
        for item in self.recent_chat:
            key = str(item.get("origin") or self.session_id or "")
            buckets.setdefault(key, []).append(item)
        dropped: list[dict[str, Any]] = []
        kept_ids: set[int] = set()
        for items in buckets.values():
            if len(items) <= keep:
                kept_ids.update(id(entry) for entry in items)
                continue
            dropped.extend(items[:-keep])
            kept_ids.update(id(entry) for entry in items[-keep:])
        if not dropped:
            return []
        dropped.sort(key=lambda entry: int(entry.get("seq") or 0))
        self.recent_chat = [item for item in self.recent_chat if id(item) in kept_ids]
        return dropped

    def recent_chat_within(
        self,
        *,
        now: float,
        seconds: float,
        limit: int = 0,
        after: float = 0.0,
        after_seq: int = 0,
        only_session: str = "",
    ) -> list[dict[str, Any]]:
        """取时间窗内的聊天记录。

        只影响"带进提示词"的视图，不删原始留档：留档大小由 note_chat 的 keep 控制，
        这样重启、或者某段时间没人说话之后，历史不会因为读一次就消失。
        """

        kept = [
            item
            for item in self.recent_chat
            if now - float(item.get("at", 0)) <= max(0.0, seconds)
            # 只看这个会话：别处的留言由别处处理，不然她会在群里答私聊的问题
            and (
                not only_session
                or str(item.get("origin") or "") in ("", str(only_session))
            )
            # 水位线只挡「别人说过的、已经回应过的」；她自己说过的话要留着，
            # 下一轮才能要求她「别重复刚才那句」。
            and (
                chat_item_is_fresh(item, replied_until=after, replied_seq=after_seq)
                or item.get("is_self")
            )
        ]
        if limit > 0:
            # 按"合并后的行"算：同一个人连着说的几句只占一行额度
            kept = take_last_chat_groups(kept, limit)
        return kept

    def chat_window(
        self, *, now: float, seconds: float, limit: int = 0
    ) -> list[dict[str, Any]]:
        """时间窗内的全部聊天（不过滤"已回应过的"）。

        提示词要用这份：水位线以前的内容会被压缩成一条概览、之后的内容原样列出，
        但两边都不该从上下文里消失。

        ``limit`` 是**合并之后的行数**（同一个人连着说的并成一行）：
        一个人连发十条只占一条额度，不会把窗口吃光。
        """

        kept = [
            item
            for item in self.recent_chat
            if now - float(item.get("at", 0)) <= max(0.0, seconds)
        ]
        if limit > 0:
            kept = take_last_chat_groups(kept, limit)
        return kept

    def chat_note_text(
        self, session_id: str = "", *, max_minutes: int = 0, now: float = 0.0
    ) -> str:
        """这个会话「刚才在聊什么」：模型上一轮顺手写的那句背景。

        ``max_minutes`` 之后就不再算"刚才"（0 = 不限）；老存档里只有一份全局的，
        这里当回落。提示词和编辑器都用这一个实现，免得两处口径不一样。
        """

        target = str(session_id or self.session_id or "")
        entry = dict((self.chat_notes or {}).get(target) or {})
        text = str(entry.get("text") or "").strip()
        try:
            at = float(entry.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        if not text:
            text = str(self.chat_note or "").strip()
            at = 0.0
        if not text:
            return ""
        if max_minutes > 0 and at > 0 and now > 0 and (now - at) > max_minutes * 60:
            return ""
        return text

    def chat_preview_text(self, session_id: str = "") -> str:
        """这个会话「她刚回应过的那批」的概览（留档被裁掉时的兜底背景）。"""

        target = str(session_id or self.session_id or "")
        entry = dict((self.chat_previews or {}).get(target) or {})
        text = str(entry.get("text") or "").strip()
        if text:
            return text
        return str(self.chat_preview or "").strip()

    def note_reply(self, text: str, *, keep: int = 6, session_id: str = "") -> None:
        """记一句她刚说过的话（提示词里用来提醒她别重复句式），并记下在哪儿说的。"""

        clean = " ".join(str(text or "").split())
        if not clean:
            return
        last = self.recent_replies[-1] if self.recent_replies else {}
        if (
            isinstance(last, dict)
            and str(last.get("text") or "") == clean
            and str(last.get("session") or "") == str(session_id or "")
        ):
            return
        self.recent_replies = [
            *self.recent_replies,
            {"text": clean, "session": str(session_id or "")},
        ][-max(1, int(keep)) :]
