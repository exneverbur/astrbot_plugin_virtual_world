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

# 名字里带这些词的，一看就是**产出内容**的工具（图 / 视频 / 语音 / 文件…）。
# 它们有可能恰好也叫 ``send_xxx``（"生成完顺手发出去"那种），但绝不该被
# 当成"直发消息"挡掉：出图工具被误判后，图只能等别的路径补发，
# 图和正文的落点就会分家（文字进私聊、图发到群里）。
CONTENT_TOOL_MARKS: tuple[str, ...] = (
    "image",
    "img",
    "picture",
    "photo",
    "selfie",
    "avatar",
    "video",
    "movie",
    "audio",
    "voice",
    "tts",
    "music",
    "song",
    "draw",
    "paint",
    "render",
    "generate",
    "t2i",
    "i2i",
    "file",
    "upload",
    "download",
)


def is_self_send_tool(name: str) -> bool:
    """这个名字是不是「整件事就是把消息发出去」的工具。

    判定收得很紧：名字（去掉插件名前缀之后）必须**整个**对上已知的直发消息工具，
    多一个词都不算——``send_image`` / ``image_send_message`` 这种一律放行。而且不许带
    「产出内容」的词（图 / 视频 / 语音…）。这条例外是给正常出图工具留的：
    它一旦被当成直发消息挡掉，生成的图就只能等别的路径补发，落点会跑偏。
    """

    full = " ".join(str(name or "").split()).strip().lower()
    if not full:
        return False
    if any(mark in full for mark in CONTENT_TOOL_MARKS):
        return False
    # 有些工具带命名空间（``某插件.send_message``）：只看最后一段
    key = full.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[-1].strip()
    return key in SELF_SEND_TOOLS


def global_tool_names(world: WorldConfig) -> set[str]:
    return {str(name).strip() for name in (world.global_allowed_tools or []) if str(name).strip()}


def action_tool_name(action: ActionDef | None) -> str:
    """动作绑定的工具名（工具型动作才有）。"""

    if action is None or getattr(action, "llm_level", "") != "tool":
        return ""
    return str(getattr(action, "tool_name", "") or "").strip()


def action_tool_names(action: ActionDef | None) -> list[str]:
    """动作声明的全部工具名：候选写法（内置搜索 / 查天气）也要算进来。"""

    if action is None or getattr(action, "llm_level", "") != "tool":
        return []
    candidates = getattr(action, "tool_candidates", None)
    if callable(candidates):
        return [str(item).strip() for item in candidates() if str(item).strip()]
    single = str(getattr(action, "tool_name", "") or "").strip()
    return [single] if single else []


def node_tool_names(world: WorldConfig, node_id: str) -> set[str]:
    """这个地点能用到的工具 = 该地点可用动作绑定的工具。"""

    names: set[str] = set()
    for action in world.actions_in(node_id):
        names.update(action_tool_names(action))
    return names


def tool_name_matches(installed: set[str], candidates: list[str]) -> bool:
    """配的工具能不能用上：同名的算，只有一个候选时按前缀算。

    官方搜索工具实际叫 ``web_search_tavily`` 这类，内置动作默认写的 ``web_search``
    要能对上它（只配一个工具的动作才这么放宽）。
    """

    names = [str(item).strip() for item in candidates if str(item).strip()]
    if not names:
        return False
    installed_set = {str(item).strip() for item in installed}
    if any(name in installed_set for name in names):
        return True
    if len(names) > 1:
        return False
    wanted = names[0].lower()
    return len(wanted) >= 5 and any(
        name.lower().startswith(wanted) for name in installed_set
    )


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
