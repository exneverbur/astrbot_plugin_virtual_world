"""把事件日志渲染成人能看懂的「她在干什么、为什么」。

引擎只负责写入结构化的事件（``event_type`` + ``detail``），这里负责把它翻译成中文时间线，
供 Web 编辑器的「日志」页和调试命令使用。纯函数，方便单测。
"""

from __future__ import annotations

from typing import Any

from .models import WorldConfig


def _join(items: Any, sep: str = " / ", limit: int = 3) -> str:
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, (list, tuple)) or not items:
        return ""
    texts = [str(item).strip() for item in items if str(item).strip()]
    if not texts:
        return ""
    shown = texts[:limit]
    suffix = f"…（共 {len(texts)} 条）" if len(texts) > limit else ""
    return sep.join(shown) + suffix


def _clip(text: Any, limit: int = 120) -> str:
    value = str(text or "").strip().replace("\n", " ")
    return value if len(value) <= limit else value[:limit] + "…"


# 事件是哪来的：日志与调试输出里标一行，一眼能看出这条是不是固定的
EVENT_SOURCE_LABELS: dict[str, str] = {
    "llm": "临场生成",
    "user": "用户投递",
    "follow": "线索续演",
}


def node_name(node_id: str, world: WorldConfig | None) -> str:
    if world is not None:
        node = world.node_map().get(node_id)
        if node is not None:
            return node.name or node.id
    return node_id or "未知地点"


def action_name(action_id: str, world: WorldConfig | None) -> str:
    if world is not None:
        action = world.action_map().get(action_id)
        if action is not None:
            return action.name or action.id
    return action_id


def _reasoning_text(reasoning: Any) -> str:
    if not isinstance(reasoning, dict) or not reasoning:
        return ""
    labels = {
        "env": "在哪",
        "state": "状态",
        "mood": "心情",
        "who": "在和谁说话",
        "intent": "打算",
    }
    parts = [
        f"{labels.get(key, key)}：{_clip(value, 40)}"
        for key, value in reasoning.items()
        if str(value).strip()
    ]
    return "；".join(parts)


def render_event(
    event: dict[str, Any],
    world: WorldConfig | None = None,
    *,
    compact: bool = False,
) -> str:
    """把一个事件渲染成一行中文说明。

    ``compact`` 是调试输出的精简模式：只留"发生了什么"，去掉参数、返回值、
    模型原话这些需要展开看的细节。

    """

    text = _render_body(event, world, compact=compact)
    where = _place_text(event)
    return f"{text}　〔在 {where}〕" if where else text


def _place_text(event: dict[str, Any]) -> str:
    """这件事发生在哪个会话（会话组里的几个群 / 私聊共用一册日志，得标出来）。"""

    detail = event.get("detail") or {}
    if not isinstance(detail, dict):
        return ""
    return " ".join(str(detail.get("session") or "").split())


def _render_body(
    event: dict[str, Any],
    world: WorldConfig | None = None,
    *,
    compact: bool = False,
) -> str:
    kind = str(event.get("event_type") or "")
    detail = event.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {"value": detail}

    if kind == "cold_start":
        return f"冷启动（{detail.get('mode', 'awakening')}），从「{node_name(detail.get('node', ''), world)}」开始"

    if kind == "user_message":
        who = _clip(detail.get("user") or "有人", 24)
        flag = "，@了她" if detail.get("wake") else ""
        return f"{who} 说话（{detail.get('len', 0)} 字{flag}）"

    if kind == "reply":
        said = _join(detail.get("messages"), sep="\n　　")
        who = detail.get("user") or ""
        head = f"回复 {who}：{said}" if who else f"回复：{said}"
        reason = _reasoning_text(detail.get("reasoning"))
        text = f"{head}　〔她的判断：{reason}〕" if reason else head
        actions = [str(item) for item in (detail.get("actions") or []) if item]
        if actions:
            text += f"　〔动作：{'、'.join(actions)}〕"
        mode = str(detail.get("plan_mode") or "")
        if mode == "interrupt":
            text += "　⏭️ 这一步插队：手上的事让开了"
        elif mode == "replace":
            text += "　⏭️ 顶上：没做的那几步不做了"
        # 她判出来"这句其实是在跟别人说话"时标一下：群里那种没 @ 的「你倒是说啊」
        # 最容易被她当成在问自己，日志里能直接核对
        if str(detail.get("addressing") or "") == "others":
            text += "　🙉 她判断这句是在跟别人说话"
        for node_id in detail.get("auto_travel") or []:
            text += f"\n　　↪ 她想去别处做这件事，已自动前往「{node_name(str(node_id), world)}」"
        images = int(detail.get("images") or 0)
        if images:
            text += f"　（还贴了 {images} 张图）"
        for warning in detail.get("warnings") or []:
            text += f"\n　　⚠ {warning}"
        return text

    if kind == "bot_message":
        text = f"主动发言：{_join(detail.get('messages'), sep='\n　　')}"
        images = int(detail.get("images") or 0)
        if images:
            text += f"　（还贴了 {images} 张图）"
        return text

    if kind == "plan":
        steps = detail.get("steps") or []
        names = [
            action_name(str(step.get("action", "")), world)
            if isinstance(step, dict)
            else str(step)
            for step in steps
        ]
        reason = _clip(detail.get("reason"), 40)
        source = {"rule": "规则", "llm": "大模型", "schedule": "日程", "extreme": "极端保护"}.get(
            str(detail.get("source") or ""), str(detail.get("source") or "")
        )
        text = f"决定：{' → '.join(names) or '（空计划）'}"
        where = " ".join(str(detail.get("send_to") or "").split())
        if where:
            # 这些话准备在哪儿说：和末尾的〔在 …〕（决定发生在哪）分开看
            text += f"（说给 {where}）"
        if reason:
            text += f"　〔原因：{reason}"
            text += f"；来源：{source}〕" if source else "〕"
        elif source:
            text += f"　〔来源：{source}〕"
        if compact:
            return text
        raw = _clip(detail.get("raw"), 160)
        return f"{text}\n　　模型原话：{raw}" if raw else text

    if kind == "action_start":
        name = action_name(str(detail.get("type", "")), world)
        ticks = detail.get("duration_ticks")
        where = detail.get("target_node")
        text = f"开始「{name}」"
        if ticks:
            text += f"，预计 {ticks} 个 tick"
        if where:
            text += f"，目标「{node_name(str(where), world)}」"
        return text

    if kind == "action_done":
        name = action_name(str(detail.get("type", "")), world)
        text = f"做完「{name}」"
        if compact:
            return text
        result = _clip(detail.get("tool_result"), 80)
        return f"{text}：{result}" if result else text

    if kind == "action":
        name = action_name(str(detail.get("type", "")), world)
        said = _join(detail.get("messages"), sep="\n　　")
        thought = _clip(detail.get("content"), 80)
        visible = detail.get("visible")
        text = f"执行「{name}」"
        if thought:
            text += f"　〔内心：{thought}〕"
        if said:
            text += f"：{said}"
        elif visible is False:
            text += "（静默）"
        return text

    if kind == "move":
        return f"走到「{node_name(str(detail.get('to', '')), world)}」"

    if kind in ("tool_call", "tool"):
        name = str(detail.get("tool") or detail.get("action") or "工具")
        if compact:
            return f"调用「{name}」"
        params = _clip(detail.get("params"), 80)
        return f"调用「{name}」参数 {params}" if params else f"调用「{name}」"

    if kind == "tool_result":
        name = str(detail.get("tool") or detail.get("action") or "工具")
        if compact:
            return f"「{name}」返回"
        result = _clip(detail.get("result"), 80)
        if detail.get("ok") is False:
            reason = _clip(detail.get("error"), 80) or "没有返回结果"
            return f"「{name}」没成功：{reason}"
        return f"「{name}」返回：{result}" if result else f"「{name}」没有返回内容"

    if kind == "schedule":
        who = "你点了「立即执行」" if detail.get("manual") else "到点触发"
        when = _clip(detail.get("time"), 8)
        head = f"日程「{detail.get('id', '')}」{who}"
        if when:
            head = f"{head}（{when}）"
        actions = _clip(detail.get("actions"), 80)
        return f"{head}：{actions}" if actions else head

    if kind == "nickname":
        if detail.get("manual"):
            to = detail.get("to") or ""
            if detail.get("ok") is False:
                return f"重新读群名片失败：{_clip(detail.get('note'), 80)}"
            return f"重新读了她的群名片：「{to}」"
        if detail.get("base"):
            return f"记下了她原来的群名片「{detail.get('base')}」"
        to = detail.get("to") or ""
        if detail.get("ok") is False:
            return f"群名片没改成「{to}」：{_clip(detail.get('note'), 80)}"
        return f"群名片改成「{to}」"

    if kind == "extreme":
        label = {
            "force_sleep": "精力透支，强制休息",
            "force_reach_out": "太久没和人说话，强制找人",
            "need_change": "想换换环境",
        }.get(str(detail.get("flag") or ""), str(detail.get("flag") or ""))
        return f"极端保护触发：{label}"

    if kind == "interrupt":
        return f"动作被打断：{action_name(str(detail.get('action') or ''), world)}"

    if kind == "cancel":
        return f"按对方说的停了：{_clip(detail.get('note'), 60)}"

    if kind == "vision":
        count = int(detail.get("images") or 0)
        suffix = f"（{count} 张）" if count else ""
        if compact:
            return f"图片内容{suffix}"
        if detail.get("ok") is False:
            return f"图片没看成{suffix}：{_clip(detail.get('detail'), 80)}"
        return f"图片内容{suffix}：{_clip(detail.get('detail'), 80)}"

    if kind == "recall_start":
        query = detail.get("query") or {}
        where = "、".join(query.get("node_names") or []) or query.get("zone") or "任何地方"
        keyword = str(query.get("keyword") or "").strip()
        text = f"开始回想：{_clip(detail.get('intent'), 60)}（范围：{where}"
        text += f"；主题：{keyword}）" if keyword else "）"
        return text

    if kind == "recall_done":
        if compact:
            return f"回想起 {int(detail.get('count') or 0)} 件事"
        if not int(detail.get("count") or 0):
            return f"回想完了：什么都没想起来（找的是 {_clip(detail.get('keyword'), 20) or '旧事'}）"
        return f"回想起来 {int(detail.get('count') or 0)} 件事：{_clip(detail.get('detail'), 120)}"

    if kind == "schedule_edit":
        what = {"add": "加了一条日程", "remove": "删掉了一条日程", "list": "翻了自己的日程"}.get(
            str(detail.get("op") or ""), "改了日程"
        )
        if detail.get("ok") is False:
            return f"日程没改成（{what}）：{_clip(detail.get('note'), 80)}"
        return f"{what}：{_clip(detail.get('note'), 80)}"

    if kind in ("command", "command_call", "command_result"):
        line = _clip(detail.get("command"), 60)
        action = _clip(detail.get("action"), 20)
        head = f"执行指令「{line}」" + (f"（动作：{action}）" if action else "")
        if kind == "command_call" or compact:
            return head
        if detail.get("ok") is False:
            return f"{head}失败：{_clip(detail.get('error'), 60)}"
        result = _clip(detail.get("result"), 80)
        return f"{head}　→ {result}" if result else head

    if kind == "wake_up":
        return f"被 {detail.get('by') or '有人'} 叫醒"

    if kind == "mood_reset":
        return (
            f"心情低落太久了，她自己缓了缓（效价回到 {float(detail.get('valence') or 0):.2f}）"
        )

    if kind == "day_mood":
        label = _clip(detail.get("label") or detail.get("day_mood"), 12)
        hint = _clip(detail.get("hint"), 60)
        return f"今天的调子：{label}——{hint}" if hint else f"今天的调子：{label}"

    if kind == "desire_push":
        who = "、".join(
            str(item) for item in (detail.get("people") or []) if str(item)
        )
        reason = _clip(detail.get("reason"), 60)
        head = "想被人碰一碰"
        if who:
            head += f"（能找的人：{_clip(who, 40)}）"
        return f"{head}：{reason}" if reason else head

    if kind == "touch":
        who = _clip(detail.get("by"), 20)
        parts = "、".join(
            _clip(item, 12) for item in (detail.get("parts") or []) if str(item)
        )
        head = f"{who} 碰到了她：{parts}" if parts else f"{who} 碰了她"
        return head

    if kind == "ext_fields":
        # 扩展自己声明的字段（主插件不解释内容，只记下"模型标了什么"）
        pairs = "、".join(
            f"{key}={_clip(value, 30) if not isinstance(value, list) else '/'.join(_clip(item, 12) for item in value)}"
            for key, value in (detail.get("fields") or {}).items()
        )
        return f"记下了扩展要的信息：{pairs}" if pairs else "记下了扩展要的信息"

    if kind == "poke":
        who = _clip(detail.get("name") or detail.get("target") or "对方", 20)
        if detail.get("ok") is False:
            return f"想戳 {who} 但没戳成：{_clip(detail.get('note'), 90)}"
        return f"戳了 {who} 一下"

    if kind == "search":
        queries = [str(item) for item in (detail.get("queries") or []) if str(item)]
        head = "正在联网搜索"
        # 这一行就靠查询词说明她在查什么，精简模式下也留着
        if queries:
            head += f"「{'、'.join(_clip(item, 20) for item in queries[:4])}」"
            if len(queries) > 4:
                head += " 等"
        return head

    if kind == "search_sources":
        count = int(detail.get("count") or 0)
        queries = [str(item) for item in (detail.get("queries") or []) if str(item)]
        head = f"查到 {count} 条可用来源"
        if queries:
            head = f"查了「{'、'.join(_clip(item, 20) for item in queries)}」：{count} 条来源"
        sources = [str(item) for item in (detail.get("sources") or []) if str(item)]
        if not sources:
            return head
        if compact:
            return f"{head}（{sources[0]}）"
        return head + "\n" + "\n".join(f"　　· {item}" for item in sources[:5])

    if kind == "storm":
        if detail.get("on"):
            return "被惹到了：情绪上来了，还在气头上"
        return "气消了"

    if kind == "sleep_reply":
        return (
            "她在睡觉，只回了一句固定文案："
            f"「{_clip(detail.get('text'), 60)}」"
        )

    if kind == "sleep_skip":
        count = int(detail.get("count") or 0)
        if count > 1:
            return f"她在睡觉，又挡下了这期间的 {count} 条消息（其它插件也一起挡了）"
        return f"她在睡觉，没有回复（{_clip(detail.get('reason'), 60)}）"

    if kind == "send_failed":
        said = _join(detail.get("messages"), sep=" / ", limit=2)
        note = _clip(detail.get("note"), 60)
        return f"这条没能发出去（{note}）：{said}"

    if kind == "manual":
        values = detail.get("values") or {}
        labels = {
            "energy": "精力",
            "loneliness": "孤独感",
            "curiosity": "好奇心",
            "affect": "心潮",
            "boredom": "无聊",
        }
        pairs = "、".join(
            f"{labels.get(key, key)} {float(value):.2f}"
            for key, value in values.items()
            if isinstance(value, (int, float))
        )
        return f"手动改了数值：{pairs}" if pairs else "手动改了数值"

    if kind == "memory":
        where = node_name(str(detail.get("where") or ""), world)
        text = _clip(detail.get("content"), 60)
        suffix = f"（{_clip(detail.get('reason'), 20)}）" if detail.get("reason") else ""
        return f"记住了一件事{suffix}：{text}" + (f"　@ {where}" if where else "")

    if kind == "chain":
        return f"接着执行日程「{detail.get('schedule', '')}」"

    if kind == "skip":
        return f"跳过：{_clip(detail.get('note'), 120)}"

    if kind == "engagement":
        return f"进入安静期：{_clip(detail.get('reason'), 80)}"

    if kind == "takeover_failed":
        # 接管没生效：这一条她没说话（不交回主人格），原因写在这儿供排查
        reason = _clip(detail.get("reason"), 60) or "没有对外输出"
        head = f"这条没接上，她没说话：{reason}"
        if compact:
            return head
        warnings = [str(item) for item in (detail.get("warnings") or []) if str(item)]
        if warnings:
            head += f"　〔{_clip('；'.join(warnings), 100)}〕"
        return head

    if kind == "bot_spoke":
        return "她说了话，等待回应"

    # ---------------- 事件系统 ----------------

    if kind == "event":
        title = _clip(detail.get("title"), 40) or "一件小事"
        where = node_name(str(detail.get("node") or ""), world)
        head = f"遇到「{title}」"
        if where:
            head += f"（{where}）"
        if detail.get("imagined"):
            head += "｜虚构场景"
        if detail.get("need_help"):
            head += "｜需要协助"
        source = str(detail.get("source") or "")
        label = EVENT_SOURCE_LABELS.get(source, source)
        if label:
            head += f"｜{label}"
        if compact:
            return head
        return f"{head}：{_clip(detail.get('hook'), 80)}"

    if kind == "event_choice":
        desc = _clip(detail.get("desc"), 60) or "照原计划"
        abilities = _clip(detail.get("abilities"), 20)
        head = f"她选：{desc}"
        if abilities:
            head += f"（{abilities}）"
        if compact:
            return head
        reason = _clip(detail.get("reason"), 60)
        return f"{head}　〔{reason}〕" if reason else head

    if kind == "search_digest":
        topic = _clip(detail.get("topic"), 30)
        head = (
            f"检索整理：{topic}｜材料 {int(detail.get('materials') or 0)} 条 / "
            f"{int(detail.get('material_chars') or 0)} 字"
        )
        if compact:
            return head
        preview = _clip(detail.get("preview"), 60)
        digest = _clip(detail.get("digest"), 120)
        if preview:
            head += f"\n　　材料开头：{preview}"
        if digest:
            head += f"\n　　压成：{digest}"
        return head

    if kind == "incoming":
        who = _clip(detail.get("user") or detail.get("user_id"), 20) or "有人"
        body = _clip(detail.get("text"), 60)
        marks = []
        if detail.get("mentioned"):
            marks.append("喊了她")
        elif detail.get("wake"):
            marks.append("交给了她")
        if detail.get("private"):
            marks.append("私聊")
        if int(detail.get("images") or 0):
            marks.append(f"{int(detail.get('images'))} 张图")
        tail = f"（{'、'.join(marks)}）" if marks else ""
        return f"收到消息：{who}：{body}{tail}"

    if kind == "event_check":
        ability = _clip(detail.get("ability"), 12)
        if detail.get("skip"):
            # 她自己选了「先不做」：没有骰子可掷，别显示成概率 0
            return f"{ability or '这件事'}：她主动放弃了，不掷骰"
        value = float(detail.get("ability_value") or 0.0)
        difficulty = float(detail.get("difficulty") or 0.0)
        probability = float(detail.get("probability") or 0.0)
        roll = float(detail.get("roll") or 0.0)
        tier = _clip(detail.get("tier"), 12)
        mods = [
            f"{_clip(item.get('label'), 10)} ×{float(item.get('factor') or 1):.2f}"
            for item in list(detail.get("modifiers") or [])
            if isinstance(item, dict)
        ]
        head = f"{ability} {value:.2f}"
        if mods and not compact:
            head += " × " + " × ".join(mods)
        # 成功条件写清楚：掷出的点数 ≤ 概率才算成——只给"概率"看不出差多少
        head += f" × 难度 {difficulty:.2f} = 要掷到 ≤ {probability:.2f}"
        return f"{head}，掷出 {roll:.2f} → {tier}"

    if kind == "event_result":
        outcome = _clip(detail.get("outcome"), 90)
        parts = [f"结果：{outcome}"] if outcome else ["结果"]
        changes = detail.get("ability_delta") or {}
        if isinstance(changes, dict) and changes:
            parts.append(
                "能力值 "
                + "、".join(
                    f"{key} {'+' if float(value) >= 0 else ''}{float(value):.2f}"
                    for key, value in changes.items()
                )
            )
        if detail.get("followup"):
            parts.append("还有后续")
        elif detail.get("closed"):
            parts.append("这条线索到此为止")
        return "｜".join(parts)

    if kind == "help":
        stage = str(detail.get("stage") or "")
        if stage == "ask":
            return f"她开口求助：{_clip(detail.get('text'), 60)}"
        if stage == "reply":
            body = _clip(detail.get("text"), 80) or _clip(detail.get("digest"), 80)
            dropped = int(detail.get("dropped") or 0)
            kept = int(detail.get("kept") or 0)
            tail = f"（有效 {kept} 条 / 无关 {dropped} 条）" if kept or dropped else ""
            return f"收到群友回应：{body}{tail}"
        if stage == "resolve":
            return f"她把建议听进去了：{_clip(detail.get('note'), 80)}"
        if stage == "timeout":
            return f"等不到人，她自己收尾：{_clip(detail.get('note'), 60)}"
        if stage == "remind":
            return f"轻提醒一句：{_clip(detail.get('text'), 60)}"
        return f"求助：{_clip(detail.get('note'), 60)}"

    if kind == "event_idle":
        stage = str(detail.get("stage") or "")
        title = _clip(detail.get("title"), 40) or "这件事"
        if stage == "hold":
            return f"「{title}」先挂起来，她去做别的了"
        if stage == "expired":
            return f"「{title}」放太久，告一段落"
        if stage == "abandon":
            return f"「{title}」她自己收尾了（没人搭手）"
        if stage == "stopped":
            return f"「{title}」在编辑器里被手动完结了"
        return f"「{title}」暂时搁下"

    if kind == "context":
        # 群聊上下文的动作（压缩成摘要 / 留档满了）：日志页要能一眼看懂
        note = str(detail.get("note") or "").strip() or "上下文有变化"
        compressed = int(detail.get("compressed") or 0)
        if compressed:
            note += f"（{compressed} 条）"
        hint = str(detail.get("hint") or "").strip()
        return note + (f"\n　　💡 {hint}" if hint else "")

    if kind == "remember":
        # 她当场用「记住」动作写进通讯录的东西（不是睡前整理补的）
        written = [str(item) for item in (detail.get("written") or []) if str(item)]
        skipped = [str(item) for item in (detail.get("skipped") or []) if str(item)]
        head = "；".join(written) if written else "这一条没记下什么"
        text = f"她主动记进了通讯录：{head}"
        if skipped:
            text += f"　〔{'；'.join(skipped)}〕"
        return text

    if kind == "soothed":
        cause = _clip(detail.get("cause") or "被安抚了一会儿", 40)
        value = detail.get("valence")
        tail = f"（效价 {float(value):.2f}）" if isinstance(value, (int, float)) else ""
        return f"心情缓过来一点：{cause}{tail}"

    if kind == "drowsy":
        return str(detail.get("note") or "困了，先进临睡期")

    if kind == "goodnight":
        if bool(detail.get("said")):
            return "睡前说了晚安（说给谁由她自己定）"
        return "睡前没说话，安静地睡了"

    if kind == "goodmorning":
        if bool(detail.get("said")):
            return "睡醒说了句早安"
        return "睡醒了，没吭声"

    # 未知类型：把 detail 原样压成一行，至少不丢信息
    if detail:
        # session 由外层统一补在行尾，这里不再重复一份
        pairs = "，".join(
            f"{key}={_clip(value, 40)}"
            for key, value in detail.items()
            if key != "session"
        )
        if pairs:
            return f"{kind}：{pairs}"
    return kind or "（未知事件）"


def build_timeline(
    events: list[dict[str, Any]], world: WorldConfig | None = None
) -> list[dict[str, Any]]:
    """给一组事件补上渲染文本，返回给前端。"""

    result: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        item["text"] = render_event(event, world)
        result.append(item)
    return result


