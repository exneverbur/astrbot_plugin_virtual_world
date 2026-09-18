"""开箱即用的默认世界配置。

设计目标：零依赖、零配置、装上就跑，同时体现「她住在群里」的生活感。

关于动作的分工（v1.7 调整）：
- **互动类动作**（抱抱/亲亲/摸头/微笑/挥手/倒茶）一律用模板层级 + ``{bot}``/``{user}`` 占位符，
  零 token、文案稳定，并且都带数值联动（孤独下降、心潮上升——抱抱亲亲比挥手更能搅动心绪）；
- **体验类动作**（发呆/看书/做饭）时长交给大模型决定，效果按"每持续 1 分钟"累加，
  做完还能让大模型把经历讲成人话（``llm_followup``）；
- 默认不启用任何工具（``global_allowed_tools`` 为空、动作绑定的工具可能并不存在），
  因此没装搜索/天气工具时相关动作会被自动跳过，不会报错。
"""

from __future__ import annotations

import copy
from typing import Any

# 「明确叫醒她」的说法：命中这些词（且消息是 @ 她的）才打断睡眠，否则只回睡觉时的固定文案
DEFAULT_WAKE_WORDS = ("醒醒", "起床", "别睡", "醒来", "起来啦", "别睡了")

# 图片转述模型的默认提示词（在「全局设置 → 图片转述」里可以改）。
# 除了画面本身，还要认出「这是不是表情包」——接梗图接错比接不上更尴尬。
# 「看图」提示词：只描述图片本身，**不写**与话题的关系——
# 关系由下面那段纯文本提示词单独生成，这样这段结果可以跨话题复用（缓存）。
DEFAULT_CAPTION_PROMPT = (
    "你是群聊图片转述器，只把图片转成一行中文，供没有多模态能力的主模型接话。"
    "只输出一行，不换行、不分点、不客套、不写「这张图片」、不编造看不到的内容。\n"
    "格式固定：画面描述｜类型｜文字\n"
    "画面描述：画面主体、动作、表情，40 字以内；文字为主的图写「文字图」即可。\n"
    "类型：表情包 / 梗图、截图、照片、其它。梗图写清能认出的梗和情绪；"
    "认不出写「疑似梗图，情绪X，具体梗不明」。\n"
    "文字：图里可见文字优先照抄关键句；内容多时给出主旨和结论，60~80 字；"
    "看不清写「文字模糊/部分可辨」；没有文字写「无」。"
    "文字里禁止出现「｜」和换行，用逗号或空格代替。\n"
    "不要写这张图和话题的关系——那部分由另一段提示词单独生成。"
)

# 「关系」提示词：输入是上一步的文字转述 + 当前消息 + 最近群聊，**不带图**。
DEFAULT_CAPTION_RELATION_PROMPT = (
    "你会拿到图片的文字转述，以及机器人收到的那条消息和最近群聊。"
    "你不需要看图，只判断这张图和正在聊的事有什么关系。\n"
    "只输出一行，格式固定：与话题的关系：…\n"
    "说清这张图在回应什么、和大家聊的事有什么关联；看不出直接关系就写「看不出直接关系」。"
    "不要复述画面描述、不要客套、不要分点。多张图时每张图各一行，以 图1：/图2： 开头。"
)

# 天气归一化提示词：输入可能是天气工具返回的一段文字，也可能是天气图的转述结果。
DEFAULT_WEATHER_PROMPT = (
    "你是天气信息整理器。把给你的天气内容压成一行中文，供另一个模型直接引用。\n"
    "只输出一行，不要解释、不要客套、不要分点、不要换行。\n"
    "格式固定：城市｜温度｜天气｜湿度｜风力｜一句话预报\n"
    "温度写成「26℃」，天气写「多云转小雨」，湿度写「68%」，风力写「东南风3级」，"
    "预报一句话说清接下来是转晴还是转雨、要不要带伞。\n"
    "内容里没提到的字段留空，不要编；实在读不出天气就只输出「读不出天气」。"
)

DEFAULT_WORLD: dict[str, Any] = {
    "world_id": "default",
    "name": "小世界",
    "bot_name": "",
    "schema_version": 1,
    "global_prompt": (
        "这是一个群聊虚拟世界。你有一个私有空间和自己的生活节奏。"
        "你的行为应该自然地反映你的位置和状态，但不要向用户解释地图、规则或系统。"
    ),
    "timezone": "Asia/Shanghai",
    "default_state": {
        "mood": "平静",
        "energy": 0.6,
        "loneliness": 0.5,
        "curiosity": 0.5,
        "affect": 0.3,
        "boredom": 0.3,
    },
    "state_dynamics": {
        "energy_decay_per_min": 0.0015,
        "loneliness_growth_per_min": 0.0008,
        "curiosity_growth_per_min": 0.0010,
        "affect_decay_per_min": 0.02,
        "boredom_growth_per_min": 0.0012,
        "sleep_energy_recovery_per_min": 0.0020,
        "nap_energy_recovery_per_min": 0.0008,
        "atmosphere_multiplier": 0.5,
        "mood_override_duration": 600,
    },
    "limits": {
        "max_actions_per_message": 3,
        "max_autonomous_per_hour": 2,
        "max_share_per_hour": 1,
        "max_think_memory": 5,
        "max_messages_per_say": 3,
        "plan_valid_duration": 1800,
        "max_action_chain_depth": 5,
        "max_active_users_tracked": 100,
        "llm_plan_min_interval_seconds": 900,
        "max_llm_plan_per_hour": 4,
        "max_llm_text_per_hour": 6,
        "forced_plan_min_interval_seconds": 3600,
    },
    "engagement": {
        "unanswered_threshold": 3,
        "silence_window_minutes": 60,
        "cooldown_after_unanswered": 120,
        "halve_on_cooldown_end": True,
        "after_reply_cooldown_minutes": 10,
    },
    "nickname_sync": {
        "enabled": True,
        "template": "{base} | {status}",
        "max_length": 30,
        "cooldown_seconds": 60,
        "restore_on_idle": True,
        "restore_on_idle_delay": 30,
        # 文案现在写在动作和地点自己身上（动作 → 执行中名片文案、地点 → 在这里时名片文案）。
        # 这两张表留作老配置的兜底：新装用户是空的，老配置里的映射会在解析时搬到动作 / 地点上。
        "status_map": {},
        "node_status": {},
    },
    "memory_scope_mode": "group_persona",
    "memory_scope_fallback": True,
    "memory_scope_warn_on_switch": True,
    "memory_conflict_policy": "newest",
    "global_allowed_tools": [],
    "tool_filter_enabled": True,
    # 睡觉时怎么回应、怎么把她叫起来
    "sleep": {
        "reply_mode": "template",
        "reply_text": "zzz…（{bot}睡觉中，要叫她起来吗？）",
        "reply_cooldown_minutes": 5,
        "wake_words": list(DEFAULT_WAKE_WORDS),
        "wake_requires_mention": True,
        "clear_plan_on_wake": True,
        "applies_to_nap": True,
        "wake_grace_minutes": 12,
        "block_plugins": True,
        "block_scope": "unmentioned",
    },
    # 群聊上下文：留档持久化，带进提示词的只是其中一小份
    "context": {
        "chat_history_max": 200,
        "chat_overflow": "discard",
        "chat_compress_threshold": 80,
        "chat_keep_after_compress": 30,
        "summary_refresh_minutes": 60,
        "history_max_chars": 400,
    },
    # 说话的节奏：分段之间的打字停顿，以及"话太密"的判定
    "reply_style": {
        "typing_delay_enabled": True,
        "typing_delay_per_char": 0.03,
        "typing_delay_max": 2.5,
        "dense_window_minutes": 10,
        "dense_max_lines": 4,
    },
    # 图片转述：交给多模态模型的那段提示词（留空则用内置默认）
    "vision": {
        "prompt": DEFAULT_CAPTION_PROMPT,
        "relation_prompt": DEFAULT_CAPTION_RELATION_PROMPT,
        # 看图（多模态、贵）只做一次并缓存；关系分析是纯文本、每次现算
        "relation_enabled": True,
        # 同一张图只识别一次：表情包/梗图会反复出现，按图片指纹缓存、重启后仍在
        "cache_enabled": True,
        "cache_max": 500,
        "cache_days": 30,
    },
    # 天气：全局共享一份，按需静默刷新，写进提示词也画在编辑器地图页顶部
    "weather": {
        "enabled": True,
        "city": "",
        # 每隔几小时静默查一次（她主动查过会顺延）
        "refresh_hours": 2,
        # 超过这么久就不再写进提示词（默认 1 天）
        "stale_hours": 24,
        # 结果先交给小模型压成「城市｜温度｜…」再存
        "normalize": True,
    },
    "content_safety": {
        "blocked_words": [],
        "message_blocklist": [],
        "session_blocklist": [],
    },
    "zones": [
        {
            "id": "home",
            "name": "家中",
            "note": "她自己的小窝，安静、私密、什么都有。",
            "icon": "home",
            "color": "#7FB2E5",
            "x": 80,
            "y": 80,
        }
    ],
    "zone_edges": [],
    "nodes": [
        {
            "id": "bedroom",
            "name": "卧室",
            "zone_id": "home",
            "x": 40,
            "y": 60,
            "icon": "bed",
            "color": "#8B7DD8",
            "prompt": "安静、私密、适合休息。在这里你更容易困倦，也更容易做梦。",
            "nickname_text": "在卧室",
            "atmosphere": {
                "calm": 0.9,
                "intimacy": 0.8,
                "visibility": 0.2,
                "liveliness": 0.1,
                "loneliness": 0.6,
                "curiosity": 0.2,
            },
            "preset_memories": [
                {
                    "content": "在这里做过一个很温柔的梦",
                    "scope": "persona",
                    "emotion": "温柔",
                    "weight": 0.6,
                }
            ],
        },
        {
            "id": "study",
            "name": "书房",
            "zone_id": "home",
            "x": 220,
            "y": 60,
            "icon": "book",
            "color": "#4C8BF5",
            "prompt": "堆着书和旧笔记，桌上有一台电脑。适合看书、上网、想事情。",
            "nickname_text": "在书房",
            "atmosphere": {
                "calm": 0.7,
                "intimacy": 0.4,
                "visibility": 0.5,
                "liveliness": 0.3,
                "loneliness": 0.3,
                "curiosity": 0.8,
            },
            "preset_memories": [
                {
                    "content": "在这张桌子前查过很多奇怪的问题",
                    "scope": "node",
                    "emotion": "好奇",
                    "weight": 0.5,
                }
            ],
        },
        {
            "id": "window",
            "name": "窗边",
            "zone_id": "home",
            "x": 400,
            "y": 60,
            "icon": "window",
            "color": "#67C2A5",
            "prompt": "能看见外面天色和偶尔路过的云。发呆最合适的地方。",
            "nickname_text": "在窗边",
            "atmosphere": {
                "calm": 0.8,
                "intimacy": 0.6,
                "visibility": 0.6,
                "liveliness": 0.3,
                "loneliness": 0.6,
                "curiosity": 0.5,
            },
            "preset_memories": [],
        },
        {
            "id": "bar",
            "name": "吧台",
            "zone_id": "home",
            "x": 220,
            "y": 240,
            "icon": "cup",
            "color": "#E0A458",
            "prompt": "有水、杯子和一点零食。适合边喝东西边和人聊天。",
            "nickname_text": "在吧台",
            "atmosphere": {
                "calm": 0.5,
                "intimacy": 0.7,
                "visibility": 0.7,
                "liveliness": 0.7,
                "loneliness": 0.3,
                "curiosity": 0.4,
            },
            "preset_memories": [],
        },
        {
            "id": "kitchen",
            "name": "厨房",
            "zone_id": "home",
            "x": 400,
            "y": 240,
            "icon": "pot",
            "color": "#D9805F",
            "prompt": "有锅、有冰箱、有一排调料。做饭的时候你心情通常会变好，桌上很快就有热的东西。",
            "nickname_text": "在厨房",
            "atmosphere": {
                "calm": 0.4,
                "intimacy": 0.6,
                "visibility": 0.4,
                "liveliness": 0.6,
                "loneliness": 0.2,
                "curiosity": 0.5,
            },
            "preset_memories": [],
        },
        {
            "id": "lobby",
            "name": "大厅",
            "zone_id": "home",
            "x": 40,
            "y": 240,
            "icon": "home",
            "color": "#B0B7C3",
            "prompt": "连接所有房间的地方，谁来了都能第一时间看见。",
            "atmosphere": {
                "calm": 0.4,
                "intimacy": 0.5,
                "visibility": 0.9,
                "liveliness": 0.9,
                "loneliness": 0.2,
                "curiosity": 0.5,
            },
            "preset_memories": [],
        },
    ],
    "edges": [
        {"id": "e_bed_lobby", "from": "bedroom", "to": "lobby", "ticks": 1, "bidirectional": True},
        {"id": "e_study_lobby", "from": "study", "to": "lobby", "ticks": 1, "bidirectional": True},
        {"id": "e_bar_lobby", "from": "bar", "to": "lobby", "ticks": 1, "bidirectional": True},
        {"id": "e_window_study", "from": "window", "to": "study", "ticks": 1, "bidirectional": True},
        {"id": "e_bar_kitchen", "from": "bar", "to": "kitchen", "ticks": 1, "bidirectional": True},
    ],
    "actions": [
        # ---------------- 表达类 ----------------
        {
            "id": "say",
            "builtin": True,
            "name": "说话",
            "category": "instant",
            "llm_level": "single",
            "scope": "global",
            "target_type": "group",
            "visible": True,
            "priority": 1,
            "on_complete": {"effects": {"affect": "+0.06", "boredom": "-0.03"}},
            "description": "把想说的话发到群里。",
        },
        {
            "id": "think",
            "builtin": True,
            "name": "想事情",
            "category": "instant",
            "llm_level": "single",
            "scope": "global",
            "target_type": "none",
            "visible": False,
            "priority": 3,
            "description": "在心里想一件事，会变成这个地点的内心记忆。",
        },
        {
            "id": "share",
            "builtin": True,
            "name": "分享",
            "category": "instant",
            "llm_level": "single",
            "scope": "global",
            "target_type": "group",
            "visible": True,
            "preconditions": {},
            "priority": 5,
            "on_complete": {"effects": {"affect": "+0.10", "loneliness": "-0.05"}},
            "description": "把刚刚的见闻分享到群里。受每小时分享次数限制。",
        },
        {
            "id": "poke",
            "builtin": True,
            "name": "戳一戳",
            "category": "instant",
            "llm_level": "template",
            "scope": "global",
            "target_type": "user",
            "visible": False,
            "preconditions": {},
            "priority": 4,
            "template": "（{bot}戳了戳{user}）",
            "on_complete": {"effects": {"affect": "+0.06", "loneliness": "-0.04"}},
            "description": "戳一下某个群友（QQ 的「戳一戳」）。不用说话就能搭个话，适合提醒对方、或者不知道说什么的时候用。目标填对方的 id。",
        },
        {
            "id": "recall",
            "builtin": True,
            "name": "回想",
            "category": "instant",
            "llm_level": "single",
            "scope": "global",
            "target_type": "none",
            "visible": False,
            "preconditions": {},
            "priority": 4,
            "on_complete": {"trigger": "none"},
            "description": (
                "主动回忆。想回忆某段经历、某个地点（或某个区域）发生过的事，"
                "或者无聊、孤单时想翻一翻美好的记忆，都可以用它。"
                "只填 intent 说清想回忆什么，例如「想想上次在厨房做饭的事」"
                "或「回忆一下公园里的事」；想指定地点就填 target_node。"
                "**回忆完不需要再写别的动作**：系统会去记忆里翻，"
                "翻完带着结果再问你一次，那时候你再说话。"
            ),
        },
        {
            "id": "schedule_list",
            "builtin": True,
            "name": "查看日程",
            "category": "instant",
            "llm_level": "single",
            "scope": "global",
            "target_type": "none",
            "visible": False,
            "preconditions": {},
            "priority": 4,
            "on_complete": {"trigger": "none"},
            "description": (
                "看看自己每天安排好要做的事。不用填 intent；"
                "看完系统会带着日程表再问你一次，你那时候再说话。"
            ),
        },
        {
            "id": "schedule_add",
            "builtin": True,
            "name": "添加日程",
            "category": "instant",
            "llm_level": "single",
            "scope": "global",
            "target_type": "none",
            "visible": False,
            "preconditions": {},
            "priority": 4,
            "on_complete": {"trigger": "none"},
            "description": (
                "给自己加一条日程（到了点自动做某串动作）。"
                "把打算写成一句自然语言填进 intent，例如「每天早上七点去书房查新闻」。"
                "**加完不用再写别的动作**：系统会替你把时间、星期和动作链填好，再带着结果问你一次。"
            ),
        },
        {
            "id": "schedule_remove",
            "builtin": True,
            "name": "删除日程",
            "category": "instant",
            "llm_level": "single",
            "scope": "global",
            "target_type": "none",
            "visible": False,
            "preconditions": {},
            "priority": 4,
            "on_complete": {"trigger": "none"},
            "description": (
                "删掉自己之前加的一条日程。在 intent 里说清是哪条（时间或名字）即可，"
                "例如「把七点查新闻那条删了」。**注意**：用户自己配的日程她删不掉，只能删自己加的。"
            ),
        },
        {
            "id": "sing",
            "name": "哼歌",
            "category": "instant",
            "llm_level": "single",
            "scope": "global",
            "target_type": "group",
            "visible": True,
            "priority": 3,
            "on_complete": {"effects": {"affect": "+0.08", "boredom": "-0.05"}},
            "description": "随口哼两句，心情会好一点。",
        },
        # ---------------- 互动类（模板 + 数值联动） ----------------
        {
            "id": "hug",
            "name": "抱抱",
            "category": "instant",
            "llm_level": "template",
            "scope": "global",
            "target_type": "user",
            "template": "（{bot}抱了你一下）",
            "visible": True,
            "priority": 5,
            "on_complete": {"effects": {"loneliness": "-0.15", "affect": "+0.28"}},
            "description": "抱一下某个人，会让你们都放松一点。",
        },
        {
            "id": "kiss",
            "name": "亲亲",
            "category": "instant",
            "llm_level": "template",
            "scope": "global",
            "target_type": "user",
            "template": "（{bot}在你脸上轻轻亲了一下）",
            "visible": True,
            "priority": 5,
            "on_complete": {"effects": {"loneliness": "-0.20", "affect": "+0.35"}},
            "description": "亲一下某个人。只在关系亲近的时候用。",
        },
        {
            "id": "pat",
            "name": "摸摸头",
            "category": "instant",
            "llm_level": "template",
            "scope": "global",
            "target_type": "user",
            "template": "（{bot}摸了摸{user}的头）",
            "visible": True,
            "priority": 4,
            "on_complete": {"effects": {"loneliness": "-0.10", "affect": "+0.16"}},
            "description": "摸摸对方的头，适合安慰人的时候用。",
        },
        {
            "id": "smile",
            "name": "笑",
            "category": "instant",
            "llm_level": "template",
            "scope": "global",
            "target_type": "user",
            "template": "（{bot}对着你笑了笑）",
            "visible": True,
            "priority": 4,
            "on_complete": {"effects": {"affect": "+0.06"}},
            "description": "对某个人笑一下。",
        },
        {
            "id": "wave",
            "name": "挥手",
            "category": "instant",
            "llm_level": "template",
            "scope": "global",
            "target_type": "user",
            "template": "（{bot}朝你挥了挥手）",
            "visible": True,
            "priority": 4,
            "on_complete": {"effects": {"affect": "+0.06"}},
            "description": "对某个人挥手打招呼，适合刚进门或要离开的时候。",
        },
        {
            "id": "pour_tea",
            "name": "倒杯茶",
            "category": "instant",
            "llm_level": "template",
            "scope": "node",
            "allowed_nodes": ["bar", "kitchen"],
            "target_type": "user",
            "template": "（{bot}给你倒了杯热茶）",
            "visible": True,
            "priority": 4,
            "on_complete": {"effects": {"affect": "+0.10", "loneliness": "-0.05"}},
            "description": "给某个人倒杯热茶，在吧台或厨房可用。",
        },
        {
            "id": "listen",
            "name": "听你说",
            "category": "instant",
            "llm_level": "single",
            "scope": "global",
            "target_type": "user",
            "visible": True,
            "priority": 4,
            "on_complete": {"effects": {"loneliness": "-0.08", "affect": "+0.12"}},
            "description": "认真听某个人说话，并回应一句。",
        },
        {
            "id": "cheer",
            "name": "打气",
            "category": "instant",
            "llm_level": "single",
            "scope": "global",
            "target_type": "user",
            "visible": True,
            "priority": 4,
            "on_complete": {"effects": {"affect": "+0.10"}},
            "description": "给某个人加油打气，说一句鼓励的话。",
        },
        {
            "id": "stretch",
            "name": "伸懒腰",
            "category": "instant",
            "llm_level": "template",
            "scope": "global",
            "target_type": "none",
            "template": "（{bot}伸了个懒腰）",
            "visible": True,
            "priority": 3,
            "on_complete": {"effects": {"energy": "+0.02"}},
            "description": "伸个懒腰，动作会发到群里。",
        },
        # ---------------- 生活类（时长交给大模型 + 按分钟结算） ----------------
        {
            "id": "walk_to",
            "nickname_text": "移动中",
            "builtin": True,
            "name": "移动",
            "category": "continuous",
            "llm_level": "template",
            "scope": "global",
            "target_type": "none",
            "duration": 0,
            "interruptible": True,
            "visible": False,
            "priority": 2,
            "during": {"state": "walking"},
            "description": "走到另一个地点或某个人身边。耗时由地图上的连线决定。",
        },
        {
            "id": "sleep",
            "nickname_text": "睡觉中",
            "builtin": True,
            "name": "睡觉",
            "category": "continuous",
            "llm_level": "template",
            "scope": "node",
            "allowed_nodes": ["bedroom"],
            "target_type": "none",
            "duration": 28800,
            "interruptible": True,
            "preconditions": {},
            "during": {"state": "sleeping"},
            "on_complete": {
                "trigger": "none",
                "effects": {"energy": "=0.95", "mood": "mood:清爽"},
            },
            "visible": False,
            "priority": 8,
            "description": "睡一整晚恢复精力（约 8 小时，夜里或精力见底时用），需要先回到卧室。",
        },
        {
            "id": "nap",
            "nickname_text": "小睡中",
            "name": "小睡",
            "category": "continuous",
            "llm_level": "template",
            "scope": "node",
            "allowed_nodes": ["bedroom", "lobby"],
            "target_type": "none",
            "duration_mode": "llm",
            "duration_min": 600,
            "duration_max": 3600,
            "interruptible": True,
            "during": {"state": "napping"},
            "on_complete": {
                "trigger": "none",
                "effects_per_minute": {"energy": "+0.0015"},
            },
            "visible": False,
            "priority": 6,
            "description": "白天犯困时打个盹（10 分钟到 1 小时，睡多久由你自己决定），睡得越久精力恢复越多。",
        },
        {
            "id": "stare",
            "nickname_text": "发呆中",
            "name": "发呆",
            "category": "continuous",
            "llm_level": "template",
            "scope": "node",
            "allowed_nodes": ["window"],
            "target_type": "none",
            "duration_mode": "llm",
            "duration_min": 300,
            "duration_max": 1800,
            "interruptible": True,
            "during": {"state": "staring"},
            "on_complete": {
                "trigger": "none",
                "effects_per_minute": {"loneliness": "+0.0015", "boredom": "-0.002"},
            },
            "visible": False,
            "priority": 4,
            "description": "看着窗外发呆，发多久由你自己决定。待得越久越容易想起人。",
        },
        {
            "id": "read",
            "nickname_text": "看书",
            "name": "看书",
            "category": "continuous",
            "llm_level": "template",
            "scope": "node",
            "allowed_nodes": ["study"],
            "target_type": "none",
            "duration_mode": "llm",
            "duration_min": 600,
            "duration_max": 3600,
            "interruptible": True,
            "preconditions": {},
            "during": {"state": "reading"},
            "on_complete": {
                "trigger": "llm_followup",
                "prompt_hint": "把刚看到的内容变成一句你的感想",
                "effects_per_minute": {"curiosity": "-0.0008", "boredom": "-0.0015"},
            },
            "visible": False,
            "priority": 4,
            "description": "读一会儿书，读多久由你自己决定；读完可能想说点什么。",
        },
        {
            "id": "cook",
            "nickname_text": "做饭中",
            "name": "做饭",
            "category": "continuous",
            "llm_level": "template",
            "scope": "node",
            "allowed_nodes": ["kitchen"],
            "target_type": "none",
            "duration_mode": "llm",
            "duration_min": 900,
            "duration_max": 3600,
            "interruptible": True,
            "preconditions": {},
            "during": {"state": "cooking"},
            "on_complete": {
                "trigger": "llm_followup",
                "prompt_hint": "用第一人称随口说说刚做好的这道菜：做了什么、闻起来怎么样、你自己吃着什么感觉；不要招呼或招揽别人来吃",
                "effects": {"mood": "mood:满足"},
                "effects_per_minute": {"energy": "-0.0012", "boredom": "-0.0025"},
            },
            "visible": False,
            "priority": 6,
            "description": "在厨房做饭，做多久由你自己决定；做好之后会想跟大家说说这顿饭。",
        },
        # ---------------- 工具型（需要 AstrBot 里已注册对应工具） ----------------
        {
            "id": "search_web",
            "builtin": True,
            "name": "上网搜索",
            # 瞬时动作：动作一开始就真的去查，拿到结果紧接着用她自己的话讲出来。
            # 做成持续动作的话要等"上网中"结束才调用工具，群里要等好几分钟才看得到结果。
            "category": "instant",
            "llm_level": "tool",
            # 不带默认工具：本机装了哪个搜索工具由用户自己挑，
            # 没选时这一步会被跳过并在日志与编辑器横幅里写明。
            "tool_names": [],
            "tool_fallbacks": [],
            # 检索流水线：多查询 → 结果归一化成证据 →（可选）读正文 → 按证据讲
            "tool_flow": "search",
            "reader_tool_names": [],
            "search_depth": "standard",
            "scope": "node",
            "allowed_nodes": ["study"],
            "target_type": "none",
            "interruptible": True,
            "preconditions": {},
            "params": {},
            "on_complete": {
                "trigger": "llm_followup",
                "prompt_hint": (
                    "把查到的内容讲给群里听：用你自己的口吻，挑最有用的两三点，别照抄原文、别念网址；"
                    "查到什么说什么，查不到就直说没查到，别拿印象里的东西凑"
                ),
                "effects": {"curiosity": "-0.06", "boredom": "-0.05"},
            },
            "visible": False,
            "priority": 5,
            "description": (
                "上网查东西（在书房用电脑）。**不知道、拿不准、或者涉及最新消息的事就用它查，"
                "不要凭印象猜、也不要编**：谁、什么时候、哪方面，写进 intent 说清楚就行，"
                "关键词由系统补全。查直播比分、天气、新闻、某个东西现在什么样，都走这个动作。"
            ),
        },
        {
            "id": "check_weather",
            "builtin": True,
            "name": "查天气",
            "category": "instant",
            "llm_level": "tool",
            # 同上：查天气也要用户自己挑一个天气工具
            "tool_names": [],
            "tool_fallbacks": [],
            "scope": "node",
            "allowed_nodes": ["study", "window"],
            "target_type": "none",
            "interruptible": True,
            "preconditions": {},
            "params": {},
            "on_complete": {
                "trigger": "llm_followup",
                "prompt_hint": (
                    "用两句讲讲天气本身：温度多少、体感怎么样（闷/干/风大）、要不要带伞或加衣、"
                    "适合做点什么。别只感叹一句「好热」「好冷」，也别自己编温度；"
                    "没查到就直说没查到"
                ),
            },
            "visible": False,
            "priority": 4,
            "description": (
                "查今天/现在的天气。有人问天气、或者你想提醒对方带伞加衣时用它；"
                "必须真的查到再说，工具没装或没查到就别猜。"
            ),
        },
    ],
}

DEFAULT_SCHEDULES: dict[str, Any] = {
    "schedules": [
        {
            "id": "morning_greet",
            "enabled": True,
            "time": "08:30",
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "action_chain": [
                {"type": "walk_to", "target_node": "lobby"},
                {"type": "stretch"},
            ],
            "conditions": {"not_state": ["sleeping"], "min_energy": 0.35},
            "priority": 5,
            "auto_travel": True,
        },
        {
            "id": "dinner_cook",
            "enabled": True,
            "time": "18:00",
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "action_chain": [
                {"type": "walk_to", "target_node": "kitchen"},
                {"type": "cook"},
            ],
            "conditions": {"not_state": ["sleeping"], "min_energy": 0.3},
            "priority": 6,
            "auto_travel": True,
        },
        {
            "id": "night_sleep",
            "enabled": True,
            "time": "23:30",
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "action_chain": [
                {"type": "walk_to", "target_node": "bedroom"},
                {"type": "sleep", "duration": 28800},
            ],
            "conditions": {"not_state": ["sleeping"]},
            "priority": 10,
            "auto_travel": True,
        },
    ]
}


def default_world() -> dict[str, Any]:
    """返回默认世界的深拷贝，避免调用方修改常量。"""

    return copy.deepcopy(DEFAULT_WORLD)


def default_schedules() -> dict[str, Any]:
    """返回默认日程的深拷贝。"""

    return copy.deepcopy(DEFAULT_SCHEDULES)


def default_actions() -> list[dict[str, Any]]:
    """返回默认动作列表的深拷贝（用来补回被删掉的内置动作）。"""

    return copy.deepcopy(DEFAULT_WORLD.get("actions") or [])







