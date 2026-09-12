"""工具可用范围：由「全局通用工具 + 该地点可用动作绑定的工具」决定。

工具只挂在动作上，地点能不能用某个工具由「这个地点有没有绑定它的动作」自然表达，
所以不再需要节点级的工具白名单。
"""

from __future__ import annotations

from .models import ActionDef, NodeDef, WorldConfig

# 这些工具会「直接把消息发给当前会话」。让插件自己的动作去调用它们，
# 等于绕过了 AstrBot 的回复管线（发送前插件、表情包识别、分段、合并转发都不会命中），
# 而且很容易造成重复发言，所以本插件一律不调用它们。
SELF_SEND_TOOLS: frozenset[str] = frozenset(
    {
        "send_message_to_user",
        "send_message",
        "send_msg",
        "reply_message",
    }
)


def is_self_send_tool(name: str) -> bool:
    return str(name or "").strip().lower() in SELF_SEND_TOOLS


def global_tool_names(world: WorldConfig) -> set[str]:
    return {str(name).strip() for name in (world.global_allowed_tools or []) if str(name).strip()}


def action_tool_name(action: ActionDef | None) -> str:
    """动作绑定的工具名（工具型动作才有）。"""

    if action is None or getattr(action, "llm_level", "") != "tool":
        return ""
    return str(getattr(action, "tool_name", "") or "").strip()


def node_tool_names(world: WorldConfig, node_id: str) -> set[str]:
    """这个地点能用到的工具 = 该地点可用动作绑定的工具。"""

    return {
        name
        for name in (action_tool_name(action) for action in world.actions_in(node_id))
        if name
    }


def allowed_tools(world: WorldConfig, node: NodeDef | None) -> set[str]:
    """允许集合 = 全局通用工具 ∪ 当前地点动作绑定的工具。"""

    allowed = global_tool_names(world)
    if node is not None:
        allowed |= node_tool_names(world, node.id)
    return allowed


def filter_tool_names(names: list[str], allowed: set[str]) -> list[str]:
    """过滤工具名列表，保持原顺序。"""

    if not allowed:
        return []
    return [name for name in names if name in allowed]


def tool_list_text(world: WorldConfig, node: NodeDef | None, available: dict[str, str]) -> str:
    """渲染提示词里的工具清单。available 为 工具名 -> 描述。"""

    names = sorted(allowed_tools(world, node) & set(available))
    if not names:
        return "（当前没有可用工具）"
    lines = []
    for name in names:
        desc = available.get(name, "")
        lines.append(f"- {name}：{desc}" if desc else f"- {name}")
    return "\n".join(lines)
