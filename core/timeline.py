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


def render_event(event: dict[str, Any], world: WorldConfig | None = None) -> str:
    """把一个事件渲染成一行中文说明。"""

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
        for node_id in detail.get("auto_travel") or []:
            text += f"\n　　↪ 她想去别处做这件事，已自动前往「{node_name(str(node_id), world)}」"
        for warning in detail.get("warnings") or []:
            text += f"\n　　⚠ {warning}"
        return text

    if kind == "bot_message":
        return f"主动发言：{_join(detail.get('messages'), sep='\n　　')}"

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
        if reason:
            text += f"　〔原因：{reason}"
            text += f"；来源：{source}〕" if source else "〕"
        elif source:
            text += f"　〔来源：{source}〕"
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

    if kind == "tool":
        name = str(detail.get("tool") or detail.get("action") or "工具")
        params = _clip(detail.get("params"), 80)
        result = _clip(detail.get("result"), 80)
        text = f"调用「{name}」参数 {params}" if params else f"调用「{name}」"
        if detail.get("ok") is False:
            reason = _clip(detail.get("error"), 80) or "没有返回结果"
            return f"{text}　✗ 失败：{reason}"
        return f"{text}　→ {result}" if result else text

    if kind == "schedule":
        return f"日程「{detail.get('id', '')}」到点触发"

    if kind == "nickname":
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

    if kind == "wake_up":
        return f"被 {detail.get('by') or '有人'} 叫醒"

    if kind == "sleep_reply":
        return (
            "她在睡觉，只回了一句固定文案："
            f"「{_clip(detail.get('text'), 60)}」"
        )

    if kind == "sleep_skip":
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

    if kind == "bot_spoke":
        return "她说了话，等待回应"

    # 未知类型：把 detail 原样压成一行，至少不丢信息
    if detail:
        pairs = "，".join(f"{key}={_clip(value, 40)}" for key, value in detail.items())
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


