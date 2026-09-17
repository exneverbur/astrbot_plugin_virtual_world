"""群名片文案计算（纯函数，平台调用留在适配器里）。"""

from __future__ import annotations

from .models import NodeDef, WorldConfig
from .state import WorldState

# 有些状态不来自任何动作（例如冷启动后的「刚醒」），它们没有地方写文案，
# 所以在代码里留一张极小的内置表——这不是给用户配的。
_BUILTIN_STATUS_TEXT = {
    "awakening": "刚醒",
}


def compute_nickname(
    world: WorldConfig,
    state: WorldState,
    node: NodeDef | None,
    *,
    base: str = "",
) -> str:
    """按优先级计算应该显示的群名片。base 为空时用状态里记录的原名。"""

    config = world.nickname_sync
    if not config.enabled:
        return base or state.bot_base_nickname
    if state.bot_nickname_locked:
        return state.bot_current_nickname or state.bot_base_nickname

    # 原名优先级：调用方给的 → 状态里记的（曾经抓到过/设置过）→ 全局设置里的 Bot 名称。
    # 少了最后这层兜底，全新安装会算不出名片，"群名片同步"看起来就像没生效。
    original = (base or state.bot_base_nickname or world.bot_name or "").strip()
    if not original:
        return ""

    status = ""
    # 优先用「她正在做的这个动作」自己写的文案：文案跟着动作走，
    # 换预设（动作跟着预设换）时名片也就配套了，不用再手动维护一份映射。
    current = state.current_action if isinstance(state.current_action, dict) else None
    action_id = str((current or {}).get("type") or "")
    definition = world.action_map().get(action_id) if action_id else None
    action_label = str(getattr(definition, "nickname_text", "") or "").strip()
    if action_label:
        status = action_label
    else:
        # 「她在做什么」优先于「她在哪儿」：睡觉时名片该写「睡觉中」而不是「在卧室」
        state_label = (config.status_map or {}).get(state.state, "") or _BUILTIN_STATUS_TEXT.get(
            state.state, ""
        )
        if state_label:
            status = state_label
        else:
            # 状态没有文案时看地点自己的（「在书房」这种）
            node_text = str(getattr(node, "nickname_text", "") or "").strip()
            if node_text:
                status = node_text
            else:
                # 兜底：老配置里的「地点 → 文案」表
                node_label = (config.node_status or {}).get(node.id if node else "", "")
                if node_label:
                    status = node_label

    if not status:
        return original
    template = config.template or "{base} | {status}"
    if "{base}" not in template and "{status}" not in template:
        return _truncate(status, config.max_length)
    text = template.replace("{base}", original).replace("{status}", status)
    return _truncate(text, config.max_length)


def should_update(
    state: WorldState, new_nickname: str, *, now: float, cooldown_seconds: int
) -> bool:
    """是否真的需要改名片（避免频繁调用平台接口）。"""

    if not new_nickname:
        return False
    if new_nickname == state.bot_current_nickname:
        return False
    if now - float(state.last_nickname_update_at or 0.0) < max(0, cooldown_seconds):
        return False
    return True


def restore_target(world: WorldConfig, state: WorldState) -> str:
    """空闲后应该恢复的名片。"""

    return state.bot_base_nickname or ""


def _truncate(text: str, max_length: int) -> str:
    limit = max(1, int(max_length or 30))
    return text if len(text) <= limit else text[:limit]
