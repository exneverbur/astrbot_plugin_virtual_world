"""AstrBot 虚拟世界插件（Star 入口）。

两条互斥的路径：
- **注入模式**：用户 @Bot 时，AstrBot 主人格正常回复，本插件只把「她此刻在哪、什么状态、
  想起什么」追加进 system_prompt，并顺带按区域裁剪可用工具（`on_llm_request`）。
- **接管模式**：自主行为（日程、发呆、搜索、主动搭话）由插件自己调 LLM 并用 JSON 约束输出，
  自己发送消息。自己发起的调用按协程打标记，绝不会被自己重复注入 / 接管。

配置热加载、Web 编辑器、状态与记忆都在 core/ 里实现，本文件只做 AstrBot 适配。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import time
import types
import urllib.parse
from collections import OrderedDict
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from pydantic import ValidationError

from .core.config_store import HISTORY_KEEP, ConfigStore
from .core.db import AsyncDatabase
from .core.defaults import (
    DEFAULT_CAPTION_PROMPT,
    DEFAULT_CAPTION_RELATION_PROMPT,
    DEFAULT_FORWARD_PROMPT,
    DEFAULT_WORLD,
)
from .core.engine import (
    PENDING_IMAGE_KEEP,
    REPLY_INTERRUPTED,
    REPLY_MUTED,
    MessageContext,
    VirtualWorldEngine,
)
from .core.models import (
    FIELD_LABELS,
    normalize_edge_keys,
    normalize_legacy_keys,
    pronoun_for,
)
from .core.nickname import compute_nickname
from .core.ports import CardResult, LLMReply, PokeResult, ToolCallResult, ToolInfo
from .core.prompt import clip_line, prompt_section_index
from .core.state import FORWARD_SUMMARY_MARK
from .core.timeline import build_timeline

PLUGIN_NAME = "astrbot_plugin_virtual_world"
# 状态页那些按钮（推进 tick / 触发决策 / 打断 / 叫醒）最多等这么久：
# 她正在检索或调模型时可能占着会话，超时就明确回一句，别让前端"点了没反应"
STATE_ACTION_TIMEOUT = 6.0
# 她正在调模型时点「推进 tick」：这次推进不排队，但愿意等这一轮跑完（秒）
STATE_ACTION_BUSY_TIMEOUT = 90.0
# 「推进 tick」这一下最多等多久（秒）：这一轮里可能夹着大模型调用（刚走到新地点要就地决定、
# 检索…），等不到就把它留在后台跑完，**绝不中途取消**——取消会把这一轮砍在半路。
STATE_TICK_WAIT_SECONDS = 45.0
# 钩子优先级：比默认 0 低，让其他插件先写完 system_prompt / 先决定要不要接管。
# AstrBot 按 priority 从高到低执行钩子，并且一旦某个钩子 stop 了事件，后面的就不再执行。
LLM_HOOK_PRIORITY = -100
# 睡觉门禁要抢在「意图路由」这类消息级插件前面：它们通常注册在 100 左右，
# 取值比它们高才会先执行；一旦这里 stop_event()，后面的处理器都不会跑。
SLEEP_GUARD_PRIORITY = 200
# 同一条会话的前一条回复最多让后一条等这么久；超时就不再排队（宁可多说一句，也别一直不说话）
REPLY_TURN_WAIT_SECONDS = 25.0
# 事件上挂的「这条消息已经在待回队列里了」标记（连发合并用）
VW_INCOMING_EXTRA = "vw_incoming_record"
TOKEN_TTL_SECONDS = 24 * 3600
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
DATA_DIR_ENV = "VIRTUAL_WORLD_DATA_DIR"


def _resolve_data_dir(plugin_dir: str) -> str:
    """数据目录：AstrBot 的 data/plugin_data/<插件名>/（可用环境变量覆盖）。"""

    override = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if override:
        return override
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
    except Exception:
        # 极少数情况下拿不到宿主路径，退回插件目录，保证插件仍然可用
        return os.path.join(plugin_dir, "data")


# ======================================================================
# 适配器：把 AstrBot 的能力包成 core/ports.py 里的 Protocol
# ======================================================================


def node_label(world: Any, node_id: str) -> str:
    """节点 id -> 中文名（拿不到就退回 id）。"""

    node = world.node_map().get(node_id) if world is not None else None
    return (node.name or node.id) if node is not None else (node_id or "？")


def _tool_owner(tool: Any) -> str:
    """这个工具是哪个插件注册的（拿不到线索时返回空串，编辑器会归到「插件 · 其它」）。"""

    module_path = str(getattr(tool, "handler_module_path", "") or "")
    if not module_path:
        return ""
    try:
        from astrbot.core.star.star import star_map

        metadata = star_map.get(module_path)
    except Exception:
        return ""
    return str(getattr(metadata, "name", "") or "")


def _is_at_bot(event: Any) -> bool:
    """这条消息里是不是真的 @ 了她。

    不能只看 ``event.is_wake_up()``：意图路由那类插件会在放行时把 ``is_wake`` 置 True，
    那是"要交给大模型"，不等于"有人在跟她说话"。
    """

    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    if not self_id:
        return False
    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ != "At":
            continue
        qq = str(getattr(component, "qq", "") or "")
        if qq and qq == self_id:
            return True
    return False


def _is_soft_wake(event: Any) -> bool:
    """意图路由放行时补的 @ 是假的：原话里没人 @ 她，只是"顺着话题对她说"。"""

    try:
        return bool(event.get_extra("intent_router_no_at", False))
    except Exception:
        return False


def _group_name(event: Any) -> str:
    """平台报的群名（拿不到就返回空串，提示词里就只显示群号 / 备注）。"""

    getter = getattr(event, "get_group_name", None)
    if callable(getter):
        try:
            value = " ".join(str(getter() or "").split())
        except Exception:
            value = ""
        if value:
            return value[:40]
    group = getattr(getattr(event, "message_obj", None), "group", None)
    for attr in ("group_name", "name"):
        value = " ".join(str(getattr(group, attr, "") or "").split())
        if value:
            return value[:40]
    return ""


def _mention_note(event: Any) -> str:
    """这条消息 @ 了谁——包括"@ 了她自己"。

    她不知道自己的 QQ 号，所以别人 @ 她的时候，光看文本里的 ``@昵称`` 她分不清
    那是不是在叫她。这里把 id 一起写出来，并且把她自己标成「你」。
    """

    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    message_obj = getattr(event, "message_obj", None)
    targets: list[str] = []
    others: list[str] = []
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ not in ("At", "AtAll"):
            continue
        qq = str(getattr(component, "qq", "") or "").strip()
        name = str(getattr(component, "name", "") or "").strip()
        if qq == "all":
            targets.append("所有人")
            continue
        label = f"{name}({qq})" if name and qq else (name or qq)
        if not label:
            continue
        if qq and self_id and qq == self_id:
            label = f"你（{label}）"
        elif qq or name:
            # @ 的是别人：这段不能让她以为是在叫她
            others.append(label)
        targets.append(label)
    if not targets:
        return ""
    note = f"这条消息 @ 了：{'、'.join(dict.fromkeys(targets))}"
    if others:
        note += f"（其中 {'、'.join(dict.fromkeys(others))} 是别人，不是在 @ 你）"
    return note


def _is_command(text: str) -> bool:
    """看起来是一条指令（/xxx、！xxx 这类）。指令永远不该被她睡觉挡住。"""

    stripped = (text or "").lstrip()
    return bool(stripped) and stripped[0] in "/!！.。"


_IMAGE_COMPONENT_NAMES = (
    "Image",
    "MarketFace",
    "Marketface",
    "Mface",
    "Sticker",
    "CustomFace",  # 协议端自定义表情：有的会带 url/file，能当图片读
)
"""会被当成"图"处理的组件：普通图片 + QQ 商城表情 / 表情包。

QQ 的表情包（大表情）在协议端不是 ``Image``：它常常只有一个 ``summary``（那句文案）、
``emoji_id`` 和一个拿不到的 ``key``，所以以前"这张图"就整个丢了——识图模型收到空地址，
只能回一句"未提供图片"。这里把它一起收进来，取不到图时至少把文案当文字用。
"""

_FACE_COMPONENT_NAMES = ("Face", "FaceEmoji", "MarketFace", "Marketface", "Mface")
"""系统小黄脸 / 表情包：没有图可读时，用它的名字或文案当文字描述。"""


def _component_named(component: Any, name: str) -> bool:
    return type(component).__name__ == name


def _is_image_component(component: Any) -> bool:
    return type(component).__name__ in _IMAGE_COMPONENT_NAMES


def _image_components(event: Any) -> list[Any]:
    """这条消息里的图片组件，以及**被引用消息**里的图片组件。

    引用（回复）消息的图片挂在 ``Reply.chain`` 上，只看当前消息会漏掉
    「引用了一张图再问她」这种最常见的用法。
    """

    message_obj = getattr(event, "message_obj", None)
    found: list[Any] = []
    for component in list(getattr(message_obj, "message", []) or []):
        if _is_image_component(component):
            found.append(component)
            continue
        if _component_named(component, "Reply"):
            for inner in list(getattr(component, "chain", None) or []):
                if _is_image_component(inner):
                    found.append(inner)
    return found


def _raw_image_ref(component: Any) -> str:
    """组件里现成的地址：优先 http(s)，其次文件 URI / 本地路径。"""

    for attr in ("url", "file", "path", "url_image", "image_url", "emoji_url"):
        value = str(getattr(component, attr, "") or "").strip()
        if not value:
            continue
        if attr == "path" and "://" not in value:
            try:
                return Path(value).resolve().as_uri()
            except Exception:
                return value
        return value
    return ""


def _is_usable_image_ref(ref: str) -> bool:
    """这个地址能不能直接交给 AstrBot 的模型调用（它自己会去下载/解码）。"""

    if not ref:
        return False
    try:
        from astrbot.core.utils.image_ref_utils import is_supported_image_ref
    except Exception:
        return ref.startswith(("http://", "https://", "base64://", "file://"))
    try:
        return bool(is_supported_image_ref(ref))
    except Exception:
        return False


MAX_SENT_IMAGES = 9
"""一条消息里最多贴几张结果图（和引擎那边的上限对齐，防刷屏）。"""


def _sendable_images(images: list[str] | None) -> list[str]:
    """挑出能真正发出去的图片地址：去重、限量、丢掉取不到的。"""

    picked: list[str] = []
    for item in list(images or []):
        ref = str(item or "").strip()
        if not ref or ref in picked:
            continue
        if not _is_usable_image_ref(ref):
            continue
        picked.append(ref)
        if len(picked) >= MAX_SENT_IMAGES:
            break
    return picked


async def _resolve_image_ref(component: Any) -> str:
    """把一个图片组件换成「模型能吃到」的地址。

    NapCat 这类协议端给的 ``file`` 经常只是一个文件名，直接丢给模型是取不到的；
    这时交给 AstrBot 自己的 ``convert_to_base64()``（http / file / base64 都能处理），
    换成 ``base64://`` 再送出去。
    """

    ref = _raw_image_ref(component)
    if _is_usable_image_ref(ref):
        return ref
    converter = getattr(component, "convert_to_base64", None)
    if callable(converter):
        try:
            data = await converter()
        except Exception:
            data = ""
        if data:
            return f"base64://{data}"
    return ref


async def _image_sources(event: Any) -> list[str]:
    """挑出这条消息里的图片，返回可以喂给多模态模型的地址。"""

    sources: list[str] = []
    for component in _image_components(event):
        ref = await _resolve_image_ref(component)
        # 只交"真的能取到"的地址：拿不到时留给 _face_note / 注解去说清，
        # 不然识图模型会收到一个取不到的名字，回一句"未提供图片"（等于白花一次调用）
        if ref and _is_usable_image_ref(ref) and ref not in sources:
            sources.append(ref)
    return sources


def _sticker_note(event: Any) -> str:
    """QQ 表情 / 表情包：没有图可读时，用它的名字或文案写成一句人话。

    ``Face`` 是系统小黄脸（只有 id / 名字），``MarketFace`` 是商城表情包
    （通常带一句 ``summary``，例如「笑死」）。这些以前直接丢失，她只看到空气。
    """

    message_obj = getattr(event, "message_obj", None)
    parts: list[str] = []
    for component in list(getattr(message_obj, "message", []) or []):
        if not _component_named_any(component, _FACE_COMPONENT_NAMES):
            continue
        summary = " ".join(
            str(
                getattr(component, "summary", "")
                or getattr(component, "name", "")
                or getattr(component, "text", "")
                or ""
            ).split()
        )
        face_id = str(
            getattr(component, "face_id", "")
            or getattr(component, "id", "")
            or getattr(component, "emoji_id", "")
            or ""
        ).strip()
        kind = "表情包" if _component_named(component, "MarketFace") else "表情"
        label = summary or (f"{kind} {face_id}" if face_id else kind)
        parts.append(f"对方发了个{kind}：{label}" if summary else f"对方发了个{kind}（{label}）")
    if not parts:
        return ""
    return "；".join(dict.fromkeys(parts))


def _component_named_any(component: Any, names: tuple[str, ...]) -> bool:
    return type(component).__name__ in names


def _provider_supports_images(provider: Any) -> bool | None:
    """这个 Provider 勾选的模态里有没有「图像」。

    AstrBot 把勾选结果记在 ``provider_config['modalities']``：空列表是"没配"
    （按老版本行为当作支持），没有这一项就读不到——那就交给配置里的开关决定。
    """

    config = getattr(provider, "provider_config", None)
    if not isinstance(config, dict):
        return None
    modalities = config.get("modalities", None)
    if modalities == []:
        return True
    if isinstance(modalities, list):
        return "image" in modalities
    return None


def _image_components_debug(event: Any) -> str:
    """取不到图片地址时留下的线索：协议端到底给了什么字段。"""

    parts: list[str] = []
    for component in _image_components(event):
        fields = {
            key: str(getattr(component, key, "") or "")
            for key in ("file", "url", "path", "_type")
        }
        parts.append(" ".join(f"{key}={value}" for key, value in fields.items() if value))
    return " / ".join(part for part in parts if part)


def _is_forwarded(event: Any) -> bool:
    """这条消息是不是「合并转发」。"""

    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ in ("Forward", "Node", "Nodes"):
            return True
        if type(component).__name__ == "Json" and _multimsg_text(component):
            return True
    text = str(getattr(message_obj, "message_str", "") or "")
    return "[合并转发]" in text or "[聊天记录]" in text


# ---------------- 合并转发：读内容 → 交给多模态模型压成摘要 ----------------

MAX_FORWARD_NODES = 60
"""一条转发最多读多少条消息（再多也没人在意，摘要也放不下）。"""

MAX_FORWARD_IMAGES = 6
"""转发里的图最多送几张给多模态模型。"""

MAX_FORWARD_TEXT_CHARS = 1500
"""送进摘要提示词的原文上限（防一条转发把上下文撑爆）。"""

_FORWARD_PLACEHOLDER_RE = re.compile(
    r"[\[【(（]\s*(?:合并转发|转发消息|转发|聊天记录|message\s*record|forward(?:\s*message)?)\s*[\]】)）]",
    re.IGNORECASE,
)
"""渲染出来的转发占位符：摘要替换正文时要把这些清掉。"""


def _multimsg_text(component: Any) -> str:
    """QQ 的「合并转发」有时是一个 multimsg JSON：把里面的条目读成文本。"""

    data = getattr(component, "data", None)
    if isinstance(data, str):
        try:
            data = json.loads(data.replace("&#44;", ","))
        except Exception:
            return ""
    return _multimsg_text_from(data)


def _multimsg_text_from(data: Any) -> str:
    """multimsg 的 JSON 负载（dict）→ 里面的条目文本。"""

    if not isinstance(data, dict):
        return ""
    if str(data.get("app") or "") != "com.tencent.multimsg":
        return ""
    meta = data.get("meta")
    detail = meta.get("detail") if isinstance(meta, dict) else None
    news = detail.get("news") if isinstance(detail, dict) else None
    if not isinstance(news, list):
        return ""
    lines: list[str] = []
    for item in news:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").replace("[图片]", "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def _strip_forward_placeholders(text: str) -> str:
    """去掉正文里的「[转发消息]」这类占位符，剩下的才是对方自己写的话。"""

    cleaned = _FORWARD_PLACEHOLDER_RE.sub(" ", str(text or ""))
    return " ".join(cleaned.split()).strip()


def _component_to_segment(component: Any) -> dict[str, Any] | None:
    """AstrBot 的消息组件 → OneBot 那样的 ``{"type":…, "data":…}``。

    转发内容的两种来源（协议端拉回来的 JSON、AstrBot 已经解析好的组件）
    归一成同一种写法，后面只需要一套走法。
    """

    if isinstance(component, dict):
        return component
    name = type(component).__name__
    if name == "Plain":
        return {"type": "text", "data": {"text": str(getattr(component, "text", "") or "")}}
    if name == "Image":
        ref = _raw_image_ref(component)
        return {"type": "image", "data": {"file": ref, "url": ref}}
    if name == "At":
        return {
            "type": "at",
            "data": {
                "name": str(getattr(component, "name", "") or ""),
                "qq": str(getattr(component, "qq", "") or ""),
            },
        }
    if name == "Face":
        return {"type": "face", "data": {}}
    if name == "File":
        return {"type": "file", "data": {"name": str(getattr(component, "name", "") or "")}}
    if name == "Json":
        return {"type": "json", "data": {"data": getattr(component, "data", "")}}
    if name:
        return {"type": name.lower(), "data": {}}
    return None


def _as_node(item: Any) -> dict[str, Any]:
    """AstrBot 的 Node 组件 / OneBot 的节点字典 → 统一的节点字典。"""

    if isinstance(item, dict):
        return item
    name = type(item).__name__
    if name == "Node":
        sender = str(getattr(item, "name", "") or getattr(item, "uin", "") or "").strip()
        content = list(getattr(item, "content", None) or [])
        return {
            "sender": {"nickname": sender},
            "content": [
                segment for segment in (_component_to_segment(c) for c in content)
                if segment is not None
            ],
        }
    return {}


def _node_list(value: Any) -> list[dict[str, Any]]:
    """把各种形态的转发内容归一成节点列表。"""

    if isinstance(value, dict):
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        nodes = (
            data.get("messages")
            or data.get("message")
            or data.get("nodes")
            or data.get("nodeList")
        )
        if isinstance(nodes, list):
            return [_as_node(item) for item in nodes if _as_node(item)]
        return [_as_node(value)] if _as_node(value) else []
    if isinstance(value, (list, tuple)):
        return [_as_node(item) for item in value if _as_node(item)]
    return []


def _node_segments(node: dict[str, Any]) -> list[dict[str, Any]]:
    """节点的内容（可能是段列表，也可能是一段 JSON 文本）。"""

    raw = node.get("message") or node.get("content") or []
    if isinstance(raw, str):
        body = raw.strip()
        if not body:
            return []
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            raw = parsed
        else:
            raw = [{"type": "text", "data": {"text": body}}]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _collect_forward(
    nodes: list[dict[str, Any]],
    *,
    out_text: list[str],
    out_images: list[str],
    out_ids: list[str],
    depth: int = 0,
) -> None:
    """把节点列表摊成「谁说了什么」+ 图片地址（嵌套转发只记 id，等着再去拉）。"""

    if depth > 3:
        return
    for node in nodes:
        if len(out_text) >= MAX_FORWARD_NODES:
            return
        sender = node.get("sender") if isinstance(node.get("sender"), dict) else {}
        who = str(
            sender.get("nickname")
            or sender.get("card")
            or sender.get("user_id")
            or node.get("name")
            or node.get("uin")
            or ""
        ).strip()
        parts: list[str] = []
        for segment in _node_segments(node):
            kind = str(segment.get("type") or "").lower()
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if kind in ("text", "plain"):
                parts.append(str(data.get("text") or ""))
            elif kind == "image":
                ref = str(data.get("url") or data.get("file") or "").strip()
                if ref:
                    out_images.append(ref)
                    parts.append(f"［图片{len(out_images)}］")
                else:
                    parts.append("［图片］")
            elif kind == "at":
                parts.append(f"@{str(data.get('name') or data.get('qq') or '').strip()}")
            elif kind == "file":
                parts.append(f"［文件:{str(data.get('name') or data.get('file') or '').strip()}］")
            elif kind == "face":
                parts.append("［表情］")
            elif kind == "video":
                parts.append("［视频］")
            elif kind == "record":
                parts.append("［语音］")
            elif kind in ("forward", "forward_msg"):
                fid = data.get("id") or data.get("message_id")
                if fid:
                    out_ids.append(str(fid))
                    parts.append("［里面还有一条转发］")
                else:
                    _collect_forward(
                        _node_list(data.get("content")),
                        out_text=out_text,
                        out_images=out_images,
                        out_ids=out_ids,
                        depth=depth + 1,
                    )
            elif kind == "json":
                nested = _multimsg_text_from(data.get("data"))
                if nested:
                    parts.append(nested)
            elif kind == "nodes":
                _collect_forward(
                    _node_list(data.get("content")),
                    out_text=out_text,
                    out_images=out_images,
                    out_ids=out_ids,
                    depth=depth + 1,
                )
        body = "".join(parts).strip()
        if body:
            out_text.append(f"{who}：{body}" if who else body)


async def _fetch_forward(event: Any, forward_id: str) -> dict[str, Any] | None:
    """去协议端把一条转发拉回来（拿不到就返回 None，调用方保持原样）。"""

    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    call_action = getattr(api, "call_action", None)
    if not callable(call_action):
        return None
    candidates: list[dict[str, Any]] = [{"message_id": forward_id}, {"id": forward_id}]
    if str(forward_id).isdigit():
        candidates.extend([{"message_id": int(forward_id)}, {"id": int(forward_id)}])
    for params in candidates:
        try:
            result = await call_action("get_forward_msg", **params)
        except Exception:
            continue
        if isinstance(result, dict):
            return result
    return None


async def _forward_digest(event: Any) -> tuple[str, list[str], list[str]]:
    """这条消息里的合并转发 → (原文摘要, 图片地址, 转发 id)。

    三种形态都认：自带内容的 Node/Nodes、只给了 id 的 Forward（去协议端拉一次）、
    QQ 的 multimsg JSON。读不出来就返回空串，调用方保持原来的那句提示。
    """

    forward_ids: list[str] = []
    nodes: list[dict[str, Any]] = []
    multimsg: list[str] = []
    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        name = type(component).__name__
        if name == "Forward":
            fid = str(getattr(component, "id", "") or "").strip()
            if fid:
                forward_ids.append(fid)
        elif name == "Node":
            nodes.extend(_node_list(component))
        elif name == "Nodes":
            nodes.extend(_node_list(list(getattr(component, "nodes", None) or [])))
        elif name == "Json":
            text = _multimsg_text(component)
            if text:
                multimsg.append(text)
    for fid in list(forward_ids):
        payload = await _fetch_forward(event, fid)
        if payload is None:
            continue
        nodes.extend(_node_list(payload))
    out_text: list[str] = []
    out_images: list[str] = []
    nested_ids: list[str] = []
    _collect_forward(nodes, out_text=out_text, out_images=out_images, out_ids=nested_ids)
    for fid in nested_ids:
        payload = await _fetch_forward(event, fid)
        if payload is None:
            continue
        _collect_forward(
            _node_list(payload), out_text=out_text, out_images=out_images, out_ids=[]
        )
    if not out_text and not out_images and multimsg:
        out_text = list(multimsg)
    digest = "\n".join(out_text).strip()[:MAX_FORWARD_TEXT_CHARS]
    return digest, out_images, forward_ids


def _forward_fingerprint(forward_ids: list[str], digest: str, images: list[str]) -> str:
    """同一条转发的指纹：协议端给了 id 就用 id，否则按内容算。"""

    ids = sorted({str(item).strip() for item in forward_ids if str(item).strip()})
    key = "|".join(ids) if ids else f"{digest}#{'|'.join(images)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _clean_forward_summary(text: str, limit: int) -> str:
    """转发摘要的收尾：压成一行、去掉模型爱加的「摘要：」开头、按配置截断。"""

    body = " ".join(str(text or "").split()).strip()
    if not body:
        return ""
    for prefix in ("摘要：", "摘要:", "内容摘要：", "内容摘要:", "总结：", "总结:"):
        if body.startswith(prefix):
            body = body[len(prefix) :].strip()
            break
    body = body.strip("「」“”\"'` ")
    if not body:
        return ""
    if len(body) > limit:
        body = body[: max(1, limit - 1)].rstrip() + "…"
    return body


def _with_forward_summary(text: str, summary: str) -> str:
    """把转发摘要拼进正文：清掉占位符，摘要跟在对方自己写的话后面。"""

    base = _strip_forward_placeholders(text)
    body = f"{FORWARD_SUMMARY_MARK}{summary}"
    return f"{base}\n{body}" if base else body


def _quoted_parts(event: Any) -> tuple[str, str]:
    """这条消息引用了谁说的什么：返回 ``(谁, 内容)``；没有引用就返回两个空串。

    两件容易漏的事：

    - 引用的那条可能**只有图片**（她刚发的照片），这时 ``message_str`` 是空的，
      只照着文本读会得到空串——她会完全不知道对方在指什么；
    - 引用的那条可能就是**她自己**发的：昵称她并不认识，要明确写成「你自己」。
    """

    message_obj = getattr(event, "message_obj", None)
    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ != "Reply":
            continue
        text = " ".join(str(getattr(component, "message_str", "") or "").split())
        if not text:
            text = _chain_plain_text(component)
        if not text:
            continue
        sender_id = str(getattr(component, "sender_id", "") or "").strip()
        who = str(getattr(component, "sender_nickname", "") or "").strip()
        if self_id and sender_id and sender_id == self_id:
            who = "你自己"
        return who, text
    return "", ""


def _quoted_text(event: Any) -> str:
    """引用消息压成一行（``小明：晚上吃鱼``）：给看图那一步当背景用。"""

    who, text = _quoted_parts(event)
    if not text:
        return ""
    return f"{who}：{text}" if who else text


def _chain_plain_text(component: Any) -> str:
    """把被引用消息的消息链压成一行（图片写［图片］，表情写［表情］）。"""

    parts: list[str] = []
    for inner in list(getattr(component, "chain", None) or []):
        if _component_named(inner, "Plain"):
            text = str(getattr(inner, "text", "") or "").strip()
            if text:
                parts.append(text)
        elif _component_named(inner, "Image"):
            parts.append("［图片］")
        elif _component_named(inner, "Face"):
            parts.append("［表情］")
        elif _component_named(inner, "At"):
            name = str(getattr(inner, "name", "") or getattr(inner, "qq", "") or "").strip()
            if name:
                parts.append(f"@{name}")
    return " ".join(" ".join(part.split()) for part in parts if part)


class AstrBotLLM:
    """LLMPort 实现。"""

    def __init__(
        self,
        plugin: "VirtualWorldPlugin",
        provider_id: str = "",
        role: str = "main",
        sampling: dict[str, float] | None = None,
    ) -> None:
        self.plugin = plugin
        self.provider_id = provider_id
        self.role = role
        """``main`` = 说话用的主模型（它的原始响应要留给别的插件看），其余是辅助模型。"""
        self.sampling = dict(sampling or {})
        """采样参数（温度 / top_p / 重复惩罚）。空 = 不覆盖，用 Provider 自己的设置。

        这些参数在最外层适配器上统一加：多轮聊天的复读和"每轮都一个腔调"，
        一半是采样参数的事，而插件自己的每次调用都不该各写一遍。
        """
        self._sampling_rejected = False
        """Provider 不认这些参数时，退回只传温度，别让主回复跟着挂掉。"""

    async def generate(
        self,
        *,
        session_id: str,
        system_prompt: str,
        prompt: str,
        contexts: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        image_urls: list[str] | None = None,
    ) -> LLMReply:
        context = self.plugin.context
        provider_id = (self.provider_id or self.plugin.llm_provider_id or "").strip()
        if not provider_id:
            try:
                provider_id = await context.get_current_chat_provider_id(session_id)
            except Exception as exc:
                return LLMReply(ok=False, error=f"没有可用的 LLM Provider: {exc}")
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        # 采样参数由适配器统一加（配置关掉时 self.sampling 是空的，等于不覆盖）
        if self.sampling and not self._sampling_rejected:
            for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
                if key in self.sampling and (key != "temperature" or temperature is None):
                    kwargs[key] = self.sampling[key]
        images = [str(item) for item in (image_urls or []) if str(item).strip()]
        token = self.plugin.set_self_initiated()
        try:
            response = await context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                contexts=list(contexts) if contexts else None,
                image_urls=images or None,
                **kwargs,
            )
        except Exception as exc:
            # Provider 不认采样参数：退回只传温度，别让主回复跟着挂掉
            if self.sampling and not self._sampling_rejected and len(kwargs) > 1:
                self._sampling_rejected = True
                self.plugin.logger.warning(
                    f"[virtual_world] Provider 不接受采样参数（{exc}），"
                    "已退回只用温度；要调就去 Panel 的 Provider 里调"
                )
                kwargs = {key: value for key, value in kwargs.items() if key == "temperature"}
                try:
                    response = await context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        contexts=list(contexts) if contexts else None,
                        image_urls=images or None,
                        **kwargs,
                    )
                    if self.role == "main":
                        self.plugin.remember_llm_response(session_id, response)
                    text = getattr(response, "completion_text", "") or ""
                    return LLMReply(text=text, ok=True)
                except Exception as retry_exc:
                    return LLMReply(ok=False, error=f"LLM 调用失败: {retry_exc}")
            # Provider 不支持图片（或图片取不回来）时，退回纯文字再来一次
            if images:
                try:
                    response = await context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        contexts=list(contexts) if contexts else None,
                        **kwargs,
                    )
                    self.plugin.logger.warning(
                        f"[virtual_world] 带图片调用失败（{exc}），已退回纯文字"
                    )
                    if self.role == "main":
                        self.plugin.remember_llm_response(session_id, response)
                    text = getattr(response, "completion_text", "") or ""
                    return LLMReply(text=text, ok=True)
                except Exception as retry_exc:
                    return LLMReply(ok=False, error=str(retry_exc))
            # 历史上下文格式不被某些 Provider 接受时，退化成不带历史再试一次
            if contexts:
                try:
                    response = await context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        **kwargs,
                    )
                except Exception as retry_exc:
                    return LLMReply(ok=False, error=str(retry_exc))
            else:
                return LLMReply(ok=False, error=str(exc))
        finally:
            self.plugin.reset_self_initiated(token)
        if self.role == "main":
            # 主模型的原始响应留给「回复钩子」用：别的插件可能要看 usage / id 这些字段，
            # 自己拼一个假的响应它们会读不到而报错。
            self.plugin.remember_llm_response(session_id, response)
        text = getattr(response, "completion_text", "") or ""
        return LLMReply(text=text, ok=True)


CAPTION_SYSTEM_PROMPT = DEFAULT_CAPTION_PROMPT
"""看图提示词的内置默认值；「全局设置 → 图片转述」里可以改成自己的一套。"""

CAPTION_RELATION_PROMPT = DEFAULT_CAPTION_RELATION_PROMPT
"""关系提示词的内置默认值；同样可以在「全局设置 → 图片转述」里改。"""

# 多图合并成一次调用时，要模型按「图1：…」逐行输出；解析不出来就退回逐张。
_MULTI_IMAGE_RULE = (
    "\n\n这次一次给你 {count} 张图：每张图各一行，"
    "以「图1：」「图2：」这样开头，顺序和图片顺序一致，不要合并成一行。"
)


def _split_multi_caption(text: str, count: int) -> list[str]:
    """把「图1：… 图2：…」拆成逐图结果；对不上就返回空列表（调用方退回逐张）。"""

    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < count:
        return []
    picked: list[str] = []
    for index, line in enumerate(lines[:count], start=1):
        for prefix in (f"图{index}：", f"图{index}:", f"{index}：", f"{index}.", f"{index})", f"{index}）"):
            if line.startswith(prefix):
                line = line[len(prefix) :].strip()
                break
        picked.append(line)
    return picked if all(picked) else []

# 「与话题的关系」之后的内容是会过期的：换个话题再说同一张图，这半句就不对了。
# 命中缓存时只复用前面的画面/类型描述，关系交回主模型判断。
_CAPTION_RELATION_MARKERS = ("与话题的关系", "和话题的关系", "与当前话题", "关系：")


def _caption_prefix(caption: str) -> str:
    """从一整条转述里切出"与话题无关"的那半句。切不出来就整条返回。"""

    text = " ".join(str(caption or "").split())
    if not text:
        return ""
    cut = len(text)
    for marker in _CAPTION_RELATION_MARKERS:
        index = text.find(marker)
        if index > 0:
            cut = min(cut, index)
    prefix = text[:cut].strip(" ｜|，,。;；")
    return prefix or text


def _strip_image_index(text: str) -> str:
    """剥掉模型自己加的「图1：」「图 1：」「1.」这类编号前缀。"""

    body = str(text or "").strip()
    for _ in range(2):
        match = re.match(
            r"^(?:图\s*[0-9一二三四五六七八九十]+|[0-9]+|[一二三四五六七八九十]+)\s*[：:.、)）]\s*",
            body,
        )
        if not match:
            break
        body = body[match.end() :].strip()
    return body


def _image_fingerprint(source: str) -> str:
    """一张图的稳定指纹：同一个文件换链接也能认出来。

    - ``base64://`` 直接对内容算 sha1（协议端把图转成 base64 时最可靠）；
    - http(s) 去掉 query（临时签名/token 每次都变）、只留路径；
    - 其它（本地文件、协议端给的 md5 名）原样取用。
    """

    raw = str(source or "").strip()
    if not raw:
        return ""
    digest = ""
    if raw.startswith("base64://"):
        payload = raw[len("base64://") :]
        digest = hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()
    elif raw.startswith("data:"):
        digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()
    elif raw.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(raw)
        digest = hashlib.sha1(
            f"{parsed.netloc}{parsed.path}".encode("utf-8", "ignore")
        ).hexdigest()
    else:
        digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()
    return f"img:{digest}"


class AstrBotVision:
    """把图片转成一段文字，让不带视觉能力的模型也能"看到"。"""

    def __init__(self, plugin: "VirtualWorldPlugin", provider_id: str = "") -> None:
        self.plugin = plugin
        self.provider_id = provider_id
        self._negative: dict[str, float] = {}
        """失败过的指纹 → 时间：短时内不再重试，但**不**写进持久缓存。"""

        self._inflight: dict[str, asyncio.Future] = {}
        """同一张图正在识别时，其它消息先等它——不然一张图会被认好几遍。"""

        self.hits = 0
        """本次运行命中了多少次缓存（省下的转述调用）。"""

        self.last_error: str = ""
        """最近一次转述失败的原因（写进日志，方便排查"模型看不见图片"）。"""

    @property
    def enabled(self) -> bool:
        return bool(self.provider_id.strip())

    def _system_prompt(self) -> str:
        """当前生效的**看图**提示词：全局设置里填了就用它，没填用内置默认。"""

        try:
            prompt = str(self.plugin.engine.world.vision.prompt or "").strip()
        except Exception:
            prompt = ""
        return prompt or CAPTION_SYSTEM_PROMPT

    def _relation_prompt(self) -> str:
        """当前生效的**关系**提示词。"""

        try:
            prompt = str(self.plugin.engine.world.vision.relation_prompt or "").strip()
        except Exception:
            prompt = ""
        return prompt or CAPTION_RELATION_PROMPT

    def _relation_enabled(self) -> bool:
        try:
            return bool(getattr(self.plugin.engine.world.vision, "relation_enabled", True))
        except Exception:
            return True

    async def describe(
        self,
        image_sources: list[str],
        *,
        question: str = "",
        quoted: str = "",
        context_lines: list[str] | None = None,
    ) -> list[str]:
        """把图片逐个转述成文字。失败或未配置时返回空串（调用方自己降级）。

        分两步：**看图**（多模态，多张图合并成一次，按图片指纹缓存）
        → **关系**（纯文本、不带图、每次现算）。最终拼成
        `画面描述｜类型｜文字｜与话题的关系：…`，对下游完全兼容。
        """

        scene = self._scene_text(question=question, quoted=quoted, context_lines=context_lines)
        # **看图那一步不带话题上下文**：把"最近群里在聊什么"塞给它，弱一点的模型
        # 会照着话题脑补画面（实测：一张"口蘑"表情包被描述成"群聊截图、两个人在
        # 争论蘑菇是不是植物"）。提示词里本来就写着"不要写这张图和话题的关系——
        # 那部分由另一段提示词单独生成"，把上下文提前给它等于自相矛盾。
        # 话题相关性交给下一步「关系」——它本来就是为这个准备的，而且不带图。
        looks = await self._look_many(image_sources, "")
        if self._relation_enabled() and any(item for item in looks):
            relations = await self._relate(looks, scene)
        else:
            relations = ["" for _ in looks]
        return [
            self._combine(look, relation)
            for look, relation in zip(looks, relations)
        ]

    @staticmethod
    def _combine(look: str, relation: str) -> str:
        """把「画面｜类型｜文字」和「与话题的关系：…」拼成一条。"""

        text = " ".join(str(look or "").split())
        extra = " ".join(str(relation or "").split())
        if not text:
            return ""
        # 关系那一步经常顺手带上「图1：」（提示词里给了编号的样例），
        # 先剥掉编号再判断，免得拼出「与话题的关系：图1：与话题的关系：…」
        extra = _strip_image_index(extra)
        if not extra:
            return text
        for marker in _CAPTION_RELATION_MARKERS:
            if extra.startswith(marker):
                body = extra[len(marker) :].lstrip("：: ｜|")
                return f"{text}｜与话题的关系：{body}" if body else text
        return f"{text}｜与话题的关系：{extra}"

    async def _look_many(self, image_sources: list[str], scene: str) -> list[str]:
        """看图：命中的走缓存，未命中的合并成一次调用（失败退回逐张）。"""

        results: list[str] = ["" for _ in image_sources]
        pending: list[int] = []
        for index, source in enumerate(image_sources):
            if not source:
                continue
            cached = await self._cached_look(source)
            if cached:
                results[index] = cached
                continue
            pending.append(index)
        if not pending:
            return results
        if not self.enabled:
            return results
        if len(pending) == 1:
            index = pending[0]
            results[index] = await self._describe_cached(image_sources[index], scene)
            return results
        # 多张一起看：一次多模态调用
        sources = [image_sources[index] for index in pending]
        merged = await self._describe_batch(sources, scene)
        for slot, index in enumerate(pending):
            caption = merged[slot] if slot < len(merged) else ""
            if not caption:
                # 合并解析失败：这张退回逐张调用，稳妥优先
                caption = await self._describe_cached(image_sources[index], scene)
            else:
                await self._store_look(image_sources[index], caption)
            results[index] = caption
        return results

    async def describe_to_text(self, image_sources: list[str], prompt: str = "") -> str:
        """给引擎用的看图入口（天气工具返回的图要读出来）：带缓存、多图合并成一次调用。"""

        sources = [str(item) for item in (image_sources or []) if str(item).strip()]
        if not sources or not self.enabled:
            return ""
        looks = await self._look_many(sources, str(prompt or "").strip())
        return "\n".join(text for text in looks if text).strip()

    async def summarize_forward(self, digest: str, images: list[str], *, fingerprint: str) -> str:
        """把一段合并转发压成摘要（含里面的图）：带缓存，失败返回空串。

        调用方拿到空串时保持原来的做法（只在聊天记录里落一句"这是转发"）。
        """

        body = " ".join(str(digest or "").split())
        if not body or not self.enabled:
            return ""
        cached = await self._cached_forward(fingerprint)
        if cached:
            return cached
        refs = [
            str(item).strip()
            for item in list(images or [])[:MAX_FORWARD_IMAGES]
            if str(item).strip()
        ]
        try:
            response = await self.plugin.context.llm_generate(
                chat_provider_id=self.provider_id,
                system_prompt=self._forward_prompt(),
                prompt=f"这段转发的内容：\n{body}",
                image_urls=refs or None,
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 转发摘要失败：{exc}")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            self.last_error = "摘要模型没有返回文字"
            return ""
        summary = _clean_forward_summary(text, self._forward_max_chars())
        if not summary:
            return ""
        await self._store_forward(fingerprint, summary)
        return summary

    def _forward_prompt(self) -> str:
        """当前生效的转发摘要提示词：设置里填了就用它，没填用内置默认。"""

        try:
            prompt = str(self.plugin.engine.world.vision.forward_prompt or "").strip()
        except Exception:
            prompt = ""
        return prompt or DEFAULT_FORWARD_PROMPT

    def _forward_max_chars(self) -> int:
        try:
            limit = int(self.plugin.engine.world.vision.forward_max_chars or 0)
        except Exception:
            limit = 0
        return max(80, limit or 300)

    async def _cached_forward(self, fingerprint: str) -> str:
        cache = self._cache_config()
        if not fingerprint or not bool(cache.get("enabled")):
            return ""
        entry = await self._cache_get_forward(fingerprint, float(cache.get("max_age") or 0))
        if entry is None:
            return ""
        self.hits += 1
        try:
            await self.plugin.db.call("touch_forward_summary", fingerprint=fingerprint)
        except Exception:
            pass
        return str(entry.get("summary") or "")

    async def _store_forward(self, fingerprint: str, summary: str) -> None:
        if not fingerprint or not summary:
            return
        cache = self._cache_config()
        try:
            await self.plugin.db.call(
                "put_forward_summary",
                fingerprint=fingerprint,
                summary=summary,
                model=self.provider_id,
            )
            await self.plugin.db.call(
                "trim_forward_cache",
                keep=int(cache.get("max") or 500),
                max_age_seconds=float(cache.get("max_age") or 0),
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 写转发缓存失败：{exc}")

    async def _cache_get_forward(
        self, fingerprint: str, max_age: float
    ) -> dict[str, Any] | None:
        try:
            return await self.plugin.db.call(
                "get_forward_summary", fingerprint=fingerprint, max_age_seconds=max_age
            )
        except Exception:
            return None

    async def _cached_look(self, source: str) -> str:
        """命中缓存就返回"看图结果"（不含关系）。"""

        fingerprint = _image_fingerprint(source)
        cache = self._cache_config()
        if not fingerprint or not bool(cache.get("enabled")):
            return ""
        entry = await self._cache_get(fingerprint, float(cache.get("max_age") or 0))
        if entry is None:
            return ""
        self.hits += 1
        await self._cache_touch(fingerprint)
        caption = str(entry.get("caption") or "")
        prefix = str(entry.get("prefix") or "")
        # 老行存的是含关系段的旧格式：只取"关系之前"的部分
        if "与话题的关系" in caption:
            return prefix or _caption_prefix(caption)
        return caption

    async def _store_look(self, source: str, caption: str) -> None:
        fingerprint = _image_fingerprint(source)
        cache = self._cache_config()
        if not fingerprint or not bool(cache.get("enabled")) or not caption:
            return
        await self._cache_put(fingerprint, caption=caption, prefix=caption)

    async def _describe_batch(self, sources: list[str], scene: str) -> list[str]:
        """一次看多张图：要求逐行输出，解析不出来返回空列表。"""

        if len(sources) < 2:
            return []
        prompt = (scene + "\n\n" if scene else "") + _MULTI_IMAGE_RULE.format(
            count=len(sources)
        ).strip()
        try:
            response = await self.plugin.context.llm_generate(
                chat_provider_id=self.provider_id,
                system_prompt=self._system_prompt(),
                prompt=prompt,
                image_urls=list(sources),
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 多图转述失败：{exc}")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            self.last_error = "转述模型没有返回文字（可能不支持图片输入）"
            return []
        return _split_multi_caption(text, len(sources))

    async def _relate(self, looks: list[str], scene: str) -> list[str]:
        """关系分析：纯文本、不带图；失败就返回空串（另一半照样能用）。"""

        lines = [f"图{index}：{text}" for index, text in enumerate(looks, start=1) if text]
        if not lines:
            return ["" for _ in looks]
        provider = self._relation_provider()
        prompt = (scene + "\n\n" if scene else "") + "图片转述：\n" + "\n".join(lines)
        try:
            response = await self.plugin.context.llm_generate(
                chat_provider_id=provider,
                system_prompt=self._relation_prompt(),
                prompt=prompt,
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 图片关系分析失败：{exc}")
            return ["" for _ in looks]
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            return ["" for _ in looks]
        if len(lines) == 1:
            # 只有一张图时它也可能顺手写「图1：」——剥掉，免得拼出双份标签
            single = _strip_image_index(text)
            return [single if len(looks) == 1 else "" for _ in looks]
        parts = _split_multi_caption(text, len(looks))
        if not parts:
            return ["" for _ in looks]
        return parts

    def _relation_provider(self) -> str:
        """关系分析用哪个模型：打杂模型（纯文本）；没配就跟随看图模型。"""

        configured = str(getattr(self.plugin, "utility_provider_id", "") or "").strip()
        return configured or self.provider_id

    async def _describe_cached(self, source: str, scene: str) -> str:
        """看单张图（缓存由调用方先查过）：带并发去重、失败不落盘。"""

        if not source:
            return ""
        fingerprint = _image_fingerprint(source)
        if not self.enabled:
            return ""
        # 同一张图正在识别：等它，别再调一次模型
        pending = self._inflight.get(fingerprint) if fingerprint else None
        if pending is not None:
            try:
                return str(await asyncio.shield(pending))
            except Exception:
                return ""
        failed_at = float(self._negative.get(fingerprint) or 0.0)
        if failed_at and time.time() - failed_at < 60:
            return ""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        if fingerprint:
            self._inflight[fingerprint] = future
        try:
            caption = await self._describe_one(source, scene)
        finally:
            if fingerprint:
                self._inflight.pop(fingerprint, None)
        if not caption:
            # 失败不写持久缓存：偶发超时不该被永久记成"看不出"
            if fingerprint:
                self._negative[fingerprint] = time.time()
            if not future.done():
                future.set_result("")
            return ""
        await self._store_look(source, caption)
        if not future.done():
            future.set_result(caption)
        return caption

    def _cache_config(self) -> dict[str, Any]:
        try:
            vision = self.plugin.engine.world.vision
            days = max(0, int(getattr(vision, "cache_days", 30) or 0))
            return {
                "enabled": bool(getattr(vision, "cache_enabled", True)),
                "max": max(1, int(getattr(vision, "cache_max", 500) or 500)),
                "max_age": days * 86400 if days else 0,
            }
        except Exception:
            return {"enabled": True, "max": 500, "max_age": 30 * 86400}

    async def _cache_get(self, fingerprint: str, max_age: float) -> dict[str, Any] | None:
        try:
            return await self.plugin.db.call(
                "get_image_caption", fingerprint=fingerprint, max_age_seconds=max_age
            )
        except Exception:
            return None

    async def _cache_touch(self, fingerprint: str) -> None:
        try:
            await self.plugin.db.call("touch_image_caption", fingerprint=fingerprint)
        except Exception:
            pass

    async def _cache_put(self, fingerprint: str, *, caption: str, prefix: str) -> None:
        cache = self._cache_config()
        try:
            await self.plugin.db.call(
                "put_image_caption",
                fingerprint=fingerprint,
                caption=caption,
                prefix=prefix,
                model=self.provider_id,
            )
            await self.plugin.db.call(
                "trim_image_cache",
                keep=int(cache.get("max") or 500),
                max_age_seconds=float(cache.get("max_age") or 0),
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 写图片缓存失败：{exc}")

    @staticmethod
    def _scene_text(
        *,
        question: str = "",
        quoted: str = "",
        context_lines: list[str] | None = None,
    ) -> str:
        parts: list[str] = []
        if question:
            parts.append(f"这条消息里说的话：{question}")
        if quoted:
            parts.append(f"引用的消息：{quoted}")
        lines = [str(item).strip() for item in (context_lines or []) if str(item).strip()]
        if lines:
            parts.append("最近群里在聊：\n" + "\n".join(lines[-6:]))
        return "\n".join(parts)

    async def _describe_one(self, source: str, scene: str) -> str:
        try:
            response = await self.plugin.context.llm_generate(
                chat_provider_id=self.provider_id,
                system_prompt=self._system_prompt(),
                prompt=(scene + "\n\n" if scene else "") + "请描述这张图片。",
                image_urls=[source],
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 图片转述失败：{exc}")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            self.last_error = "转述模型没有返回文字（可能不支持图片输入）"
        return " ".join(text.split())[:200]


async def _caption_result_images(
    plugin: "VirtualWorldPlugin",
    images: list[str],
    *,
    scene: str = "",
) -> tuple[list[str], list[str]]:
    """工具 / 指令返回的图片：配了转述模型就先转述，没配就原样带回去。

    返回 ``(转述后的描述, 还需要交给多模态模型的图片)``——转述成功就不必再传图片，
    失败或没配转述模型时把地址原样交上去，由 ``llm_generate`` 那条路处理。
    """

    if not images:
        return [], []
    limit = 3
    try:
        limit = max(1, int(plugin.engine.world.context.image_max))
    except Exception:
        pass
    picked = images[:limit]
    vision = getattr(plugin, "vision", None)
    if vision is None or not vision.enabled:
        return [], picked
    try:
        captions = await vision.describe(picked, question=scene)
    except Exception as exc:
        plugin.logger.debug(f"[virtual_world] 返回内容里的图片转述失败：{exc}")
        captions = []
    described = [
        f"图片{index + 1}：{caption}"
        for index, caption in enumerate(captions or [])
        if caption
    ]
    if not described:
        return [], picked
    return described, []


class AstrBotCommands:
    """把「指令触发」型动作转成真正的指令，交给别的插件执行。

    寻找方式与 AstrBot 的指令分发一致：从 handler 注册表里按指令名（含别名）匹配，
    借用这个会话最近一条真实事件，把消息文本换成指令本身，再调用它的处理器。
    处理器 yield 出来的文本会被收集起来交回给大模型。
    """

    def __init__(self, plugin: "VirtualWorldPlugin") -> None:
        self.plugin = plugin

    def session_with_context(self, candidates: list[str] | None = None) -> str:
        """最近收到过消息的那条会话（在 ``candidates`` 里挑，没给就全局挑）。"""

        events = getattr(self.plugin, "_last_events", None) or {}
        if not events:
            return ""
        if candidates is None:
            return next(reversed(events), "")
        wanted = {str(item) for item in candidates}
        for session_id in reversed(events):
            if session_id in wanted:
                return session_id
        return ""

    async def trigger(
        self, session_id: str, command: str, *, event: Any = None
    ) -> ToolCallResult:
        text = " ".join(str(command or "").split())
        if not text:
            return ToolCallResult(ok=False, error="指令是空的")
        if not text.startswith("/"):
            text = "/" + text
        word = text.split()[0].lstrip("/")
        matched = self._find(text)
        if matched is None:
            return ToolCallResult(ok=False, error=f"没找到指令「{word}」")
        record, command_filter = matched
        real_event = event or self.plugin.last_event(session_id) or self.plugin.last_event_any()
        if real_event is None:
            return ToolCallResult(
                ok=False, error="还没有收到过这个会话的消息，指令没有上下文可用"
            )
        try:
            params = self._params(record, command_filter, text)
        except Exception as exc:
            return ToolCallResult(ok=False, error=str(exc))
        restore = self._swap_text(real_event, text)
        try:
            result = record.handler(real_event, **params)
            texts: list[str] = []
            images: list[str] = []
            if inspect.isasyncgen(result):
                async for item in result:
                    body, item_images = await _result_text(item)
                    if body:
                        texts.append(body)
                    images.extend(item_images)
            elif inspect.isawaitable(result):
                body, item_images = await _result_text(await result)
                if body:
                    texts.append(body)
                images.extend(item_images)
            else:
                body, item_images = await _result_text(result)
                if body:
                    texts.append(body)
                images.extend(item_images)
        except Exception as exc:
            # 指令跑挂时把"最后落在哪个文件哪一行"写进日志与返回值：
            # 报错来自别的插件（例如它拿到的参数类型不对）时，一眼能看出是谁的锅
            where = _exception_where(exc)
            detail = f"{type(exc).__name__}: {exc}"
            if where:
                detail += f"（{where}）"
            self.plugin.logger.warning(
                f"[virtual_world] 指令「{text}」执行失败：{detail}",
                exc_info=True,
            )
            return ToolCallResult(ok=False, error=detail, tool=word)
        finally:
            restore()
        captions, pending_images = await _caption_result_images(
            self.plugin, images, scene=text
        )
        if captions:
            texts.append("；".join(captions))
        body = "\n".join(part for part in texts if part).strip()
        if not body:
            body = (
                "（指令执行了，返回了一张图片）"
                if pending_images
                else f"（指令「{word}」执行了，但没有返回内容）"
            )
        return ToolCallResult(
            ok=True,
            text=body,
            tool=word,
            image_urls=pending_images,
            # 原图另存一份：转述成功时 image_urls 会空，但这张图还是得发出去
            attachments=list(images),
        )

    def _find(self, text: str) -> tuple[Any, Any] | None:
        """按指令名找处理器（含别名）；跳过本插件自己的指令，避免绕回自己。"""

        try:
            from astrbot.core.star.star_handler import (
                EventType,
                star_handlers_registry,
            )
        except Exception:
            return None
        try:
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.AdapterMessageEvent
            )
        except Exception:
            return None
        for record in handlers:
            module = str(getattr(record, "handler_module_path", "") or "")
            if PLUGIN_NAME in module:
                continue
            for event_filter in list(getattr(record, "event_filters", []) or []):
                names = getattr(event_filter, "get_complete_command_names", None)
                if not callable(names):
                    continue
                try:
                    candidates = [str(name) for name in names()]
                except Exception:
                    continue
                for name in candidates:
                    if event_filter.equals(text) or text.split()[0].lstrip("/") == name.lstrip("/"):
                        return record, event_filter
        return None

    @staticmethod
    def _params(record: Any, command_filter: Any, text: str) -> dict[str, Any]:
        """按处理器签名解析参数（和 AstrBot 指令分发同一套转换）。

        参数类型优先取 ``CommandFilter.handler_params``——那是 AstrBot 自己解析好的
        （它用 ``inspect.signature(..., eval_str=True)``，会把
        ``from __future__ import annotations`` 留下的**字符串注解**还原成真类型）。

        以前这里是自己再 inspect 一遍、**没有 eval_str**：注解是字符串时，
        `validate_and_convert_params` 会把它当成"默认值"原样透传，
        于是需要 ``MessageChain`` 这类对象的指令收到一个 str，接着就报
        ``'str' object has no attribute 'chain'``。
        """

        param_type = dict(getattr(command_filter, "handler_params", None) or {})
        if not param_type:
            try:
                signature = inspect.signature(record.handler, eval_str=True)
            except (TypeError, ValueError):
                signature = inspect.signature(record.handler)
            for name, parameter in signature.parameters.items():
                if name in ("self", "event"):
                    continue
                param_type[name] = (
                    parameter.annotation
                    if parameter.annotation is not inspect.Parameter.empty
                    else parameter.default
                )
        if not param_type:
            return {}
        args = text.split()[1:]
        return command_filter.validate_and_convert_params(args, param_type)

    @staticmethod
    def _swap_text(event: Any, text: str):
        """临时把事件的消息文本换成指令本身，返回还原用的回调。"""

        message_obj = getattr(event, "message_obj", None)
        previous_str = getattr(event, "message_str", None)
        previous_obj_str = getattr(message_obj, "message_str", None)
        previous_chain = getattr(message_obj, "message", None)
        try:
            event.message_str = text
        except Exception:
            pass
        if message_obj is not None:
            try:
                message_obj.message_str = text
                message_obj.message = [Plain(text=text)]
            except Exception:
                pass

        def restore() -> None:
            if previous_str is not None:
                try:
                    event.message_str = previous_str
                except Exception:
                    pass
            if message_obj is not None:
                if previous_obj_str is not None:
                    try:
                        message_obj.message_str = previous_obj_str
                    except Exception:
                        pass
                if previous_chain is not None:
                    try:
                        message_obj.message = previous_chain
                    except Exception:
                        pass

        return restore


class AstrBotMessenger:
    """MessagePort 实现。"""

    # 发送失败后，这个会话多少秒内不再尝试发送。
    # 协议端（NapCat）掉线/卡住时，一次发送可能要等 AstrBot 那边的超时才失败（默认 180 秒），
    # 期间不断重试只会把更多消息塞进协议端队列，恢复后一次性喷出来刷屏。
    SEND_FAIL_COOLDOWN_SECONDS = 180

    def __init__(self, plugin: "VirtualWorldPlugin") -> None:
        self.plugin = plugin
        self._blocked_until: dict[str, float] = {}
        self._fail_notes: dict[str, str] = {}

    # ---------------- 失败冷却（不重试） ----------------

    def blocked(self, session_id: str) -> bool:
        """这个会话现在是不是处于"刚发送失败"的冷却里。"""

        return self._blocked_until.get(session_id, 0.0) > time.time()

    def blocked_seconds(self, session_id: str) -> int:
        """还要等多少秒才会重新尝试发送（没在冷却里就是 0）。"""

        remain = self._blocked_until.get(session_id, 0.0) - time.time()
        return max(0, int(remain))

    def take_fail_note(self, session_id: str) -> str:
        """取走一次「发送失败」的说明（每个冷却窗口只会有一条）。"""

        return self._fail_notes.pop(session_id, "")

    def mark_sent(self, session_id: str) -> None:
        self._blocked_until.pop(session_id, None)
        self._fail_notes.pop(session_id, None)

    def mark_failed(self, session_id: str, exc: Any) -> None:
        """记一次失败：本会话进入冷却，日志每个窗口只打一条。"""

        self._blocked_until[session_id] = time.time() + self.SEND_FAIL_COOLDOWN_SECONDS
        note = f"{type(exc).__name__}: {exc}"
        if session_id in self._fail_notes:
            return
        self._fail_notes[session_id] = note
        self.plugin.logger.warning(
            f"[virtual_world] 发送失败，{self.SEND_FAIL_COOLDOWN_SECONDS} 秒内不再尝试"
            f"（失败就失败，不重发）：{note}"
        )

    async def send_text(self, session_id: str, messages: list[str]) -> bool:
        texts = [m for m in messages if m and str(m).strip()]
        if not texts:
            return False
        if self.blocked(session_id):
            return False  # 刚失败过：直接放弃这一批，连平台都不碰
        # 一条消息一个消息链：平台侧才会显示成多条（分段回复）。
        # 全部塞进同一个 chain 的话，多数平台会拼成一条发出去。
        ok = False
        for index, text in enumerate(texts):
            try:
                result = await self.plugin.context.send_message(
                    session_id, MessageChain(chain=[Plain(text=str(text))])
                )
                ok = bool(result) or ok
            except Exception as exc:
                # 失败就失败：不重试、不补发，后面的几条也不再试（平台已经不通了）
                self.mark_failed(session_id, exc)
                break
            if index == len(texts) - 1:
                break  # 最后一条不用再等
            delay = self.plugin.typing_delay_for(text)
            if delay > 0:
                # 分段之间停一下，群里看起来像她在一句句打字
                await asyncio.sleep(delay)
        if ok:
            self.mark_sent(session_id)
        return ok

    async def send_images(self, session_id: str, images: list[str]) -> bool:
        """把工具 / 指令生成出来的图片发到会话里。

        多张图放**同一条消息**：群里显示成一组，不会被拆成刷屏的连发。
        地址可以是 http、file://、base64://，AstrBot 发送时会自己转成平台要的格式。
        """

        refs = _sendable_images(images)
        if not refs:
            return False
        if self.blocked(session_id):
            return False  # 刚失败过：连同图片一起放弃这一批
        chain = [Image(file=ref) for ref in refs]
        try:
            result = await self.plugin.context.send_message(
                session_id, MessageChain(chain=chain)
            )
        except Exception as exc:
            self.mark_failed(session_id, exc)
            return False
        if result:
            self.mark_sent(session_id)
        return bool(result)

    async def set_group_card(self, session_id: str, card: str) -> CardResult:
        """设置群名片。只有 aiocqhttp（OneBot）这类支持 call_action 的平台才生效。"""

        if not card:
            return CardResult(ok=False, reason="算出来的名片是空的")
        if ":GroupMessage:" not in session_id:
            return CardResult(ok=False, reason="这不是群聊，没有群名片")
        event = self.plugin.last_event(session_id)
        if event is None:
            # 这个群还没来过消息时，拿别处收到过的事件取机器人句柄也行——
            # 句柄是同一个 Bot，真正决定改哪个群的还是上面的 session_id。
            event = self.plugin.last_event_any()
        if event is None:
            return CardResult(ok=False, reason="还没收到过这个群的消息，拿不到机器人句柄")
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return CardResult(ok=False, reason="当前平台不支持改群名片（需要 OneBot/aiocqhttp）")
        group_id = session_id.split(":")[-1]
        self_id = event.get_self_id()
        try:
            await bot.call_action(
                "set_group_card",
                group_id=int(group_id) if str(group_id).isdigit() else group_id,
                user_id=int(self_id) if str(self_id).isdigit() else self_id,
                card=card,
            )
            return CardResult(ok=True, card=card)
        except Exception as exc:
            # 常见原因：机器人不是群管理/群主、群号不对、协议端不支持这个接口
            # 退避由引擎负责，这里只留 debug 日志，避免协议端挂掉时每分钟刷一条
            self.plugin.logger.debug(f"[virtual_world] 设置群名片失败：{exc}")
            return CardResult(ok=False, reason=f"平台拒绝了：{exc}", card=card)

    async def fetch_group_card(self, session_id: str) -> str:
        """读她当前在群里的名片，用作"原名"的兜底。"""

        if ":GroupMessage:" not in session_id:
            return ""
        event = self.plugin.last_event(session_id)
        if event is None:
            return ""
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return ""
        group_id = session_id.split(":")[-1]
        self_id = event.get_self_id()
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id) if str(group_id).isdigit() else group_id,
                user_id=int(self_id) if str(self_id).isdigit() else self_id,
                no_cache=True,
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[virtual_world] 读取群名片失败：{exc}")
            return ""
        if not isinstance(info, dict):
            return ""
        return str(info.get("card") or info.get("nickname") or "").strip()

    async def poke(self, session_id: str, user_id: str) -> PokeResult:
        """戳一戳某个人。

        群聊和私聊走的是**两条不同的路**：

        - **群聊**：先发 OneBot 的 ``poke`` 消息段（群里的标准做法），不通再退到
          ``send_poke`` / ``group_poke`` 接口；
        - **私聊**：``poke`` 消息段是给群聊用的，私聊里发出去客户端会显示成一个认不出的
          占位（红叉破图），所以这里**不试消息段**，直接调 ``friend_poke``。

        每次都会把"走了哪条路"记进 ``PokeResult.route``：两条都不通就把原因带回去，
        她会退化成一句话说，而日志里要能看出究竟是为什么。
        """

        target = str(user_id or "").strip()
        if not target:
            return PokeResult(False, "没有指定要戳谁")
        if self.blocked(session_id):
            return PokeResult(False, "上一次发送失败，正在冷却")
        group_id = ""
        parts = str(session_id).split(":")
        if len(parts) >= 3 and "group" in parts[1].lower():
            group_id = parts[-1]
        errors: list[str] = []
        # 路线一：消息段。**只在群聊用**——私聊里这个段协议端不认。
        if group_id:
            try:
                from astrbot.api.message_components import Poke

                await self.plugin.context.send_message(
                    session_id, MessageChain(chain=[Poke(id=target)])
                )
                return PokeResult(True, route="poke 消息段")
            except Exception as exc:
                errors.append(f"消息段：{type(exc).__name__}: {exc}")
        # 路线二：直接调协议端接口（私聊只有这一条：friend_poke）
        event = getattr(self.plugin, "_last_events", {}).get(session_id)
        bot = getattr(event, "bot", None)
        if bot is not None:
            payload: dict[str, Any] = {
                "user_id": int(target) if target.isdigit() else target
            }
            if group_id.isdigit():
                payload["group_id"] = int(group_id)
            names = (
                ("send_poke", "group_poke")
                if group_id
                else ("friend_poke", "send_poke")
            )
            for name in names:
                try:
                    await bot.call_action(name, **payload)
                    return PokeResult(True, route=name)
                except Exception as exc:
                    errors.append(f"{name}：{type(exc).__name__}: {exc}")
        else:
            errors.append("拿不到协议端连接（这条会话还没收到过消息）")
        return PokeResult(False, "；".join(errors) or "没有可用的戳一戳通道")


class AstrBotTools:
    """ToolPort 实现。"""

    def __init__(self, plugin: "VirtualWorldPlugin") -> None:
        self.plugin = plugin

    def has_context(self) -> bool:
        """手上有没有一条真实消息事件：工具调用要拿它当上下文，没有就只能等。"""

        return self.plugin.last_event_any() is not None

    def list_tools(self) -> list[ToolInfo]:
        manager = self._manager()
        if manager is None:
            return []
        tools: list[ToolInfo] = []
        seen: set[str] = set()
        for tool in list(getattr(manager, "func_list", []) or []):
            name = getattr(tool, "name", "") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            tools.append(
                ToolInfo(
                    name=name,
                    description=str(getattr(tool, "description", "") or ""),
                    parameters=dict(getattr(tool, "parameters", {}) or {}),
                    source="plugin",
                    plugin=_tool_owner(tool),
                )
            )
        # AstrBot 自带的内置工具不在 func_list 里（官方明确把它们单独放），
        # 但搜索、知识库这些正好是最常用的，所以一并列出来，标成「官方」。
        for tool in self._builtin_tools(manager):
            name = getattr(tool, "name", "") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            tools.append(
                ToolInfo(
                    name=name,
                    description=str(getattr(tool, "description", "") or ""),
                    parameters=dict(getattr(tool, "parameters", {}) or {}),
                    source="official",
                    plugin=_tool_owner(tool),
                )
            )
        return tools

    @staticmethod
    def _builtin_tools(manager) -> list[Any]:
        """AstrBot 的内置工具列表；老版本没有这个接口时返回空。"""

        iterator = getattr(manager, "iter_builtin_tools", None)
        if not callable(iterator):
            return []
        try:
            return list(iterator() or [])
        except Exception:
            return []

    def _manager(self):
        try:
            return self.plugin.context.get_llm_tool_manager()
        except Exception:
            return None

    async def call_tool(
        self, name: str, params: dict[str, Any], session_id: str = ""
    ) -> ToolCallResult:
        """调用工具。返回结构化结果，失败时带上原因（会写进事件日志，方便排查）。"""

        manager = self._manager()
        if manager is None:
            return ToolCallResult(ok=False, error="拿不到 AstrBot 的工具管理器", tool=name)
        tool = None
        getter = getattr(manager, "get_func", None)
        if callable(getter):
            try:
                tool = getter(name)
            except Exception:
                tool = None
        if tool is None:
            for candidate in list(getattr(manager, "func_list", []) or []):
                if getattr(candidate, "name", "") == name:
                    tool = candidate
                    break
        if tool is None:
            return ToolCallResult(ok=False, error=f"工具「{name}」没有注册", tool=name)
        event = self.plugin.last_event(session_id)
        if event is None:
            # 拿不到这个会话的事件时用最近一次见过的兜底：工具大多只需要
            # 「谁在什么群说的」，没有事件的话连 call() 都进不去。
            event = self.plugin.last_event_any()
        if event is None:
            # 插件刚启动 / 刚重载、群里还没人说话时，一个真实事件都没有。
            # 这时候硬调只会让工具内部炸出「'NoneType' object has no attribute ...」，
            # 看不出到底为什么失败——直接说清楚，日志里一眼能懂。
            return ToolCallResult(
                ok=False,
                error=(
                    "刚启动还没收到过消息，工具需要一条消息当上下文；"
                    "等群里有人说一句，或者先让她做别的事"
                ),
                tool=name,
            )
        call_params = self._filter_params(name, params, tool)
        # AstrBot 的工具其实有三种写法：@filter.llm_tool 的 handler、新版 call()、
        # 以及老的 run()。MCP 工具和新式工具都没有 handler，只能走 call()。
        # 这里跟 AstrBot 自己的执行器保持同一套优先级，否则只会报「没有可调用的 handler」。
        invoke = _tool_invoker(tool)
        if invoke is None:
            return ToolCallResult(
                ok=False, error=f"工具「{name}」没有可调用的 handler", tool=name
            )
        try:
            # 工具前后也补一遍钩子：有些插件靠它们显示"正在用工具 / 用完了"
            await self.plugin._fire_hook(
                event, "OnUsingLLMToolEvent", tool, dict(call_params)
            )
            result = invoke(event, call_params, self.plugin)
            images: list[str] = []
            if inspect.isasyncgen(result):
                last: Any = None
                async for item in result:
                    last = item
                text, images = await _result_text(last)
            elif inspect.isawaitable(result):
                text, images = await _result_text(await result)
            else:
                text, images = await _result_text(result)
            await self.plugin._fire_hook(
                event,
                "OnLLMToolRespondEvent",
                tool,
                dict(call_params),
                _as_tool_result(text),
            )
        except Exception as exc:
            self.plugin.logger.warning(f"[virtual_world] 工具 {name} 调用失败：{exc}")
            # 工具的报错经常是「event 是 None」这种内部细节，补一句人话更好排查
            hint = ""
            if isinstance(exc, AttributeError) and "NoneType" in str(exc):
                hint = "（工具拿不到消息上下文：插件刚启动或刚重载过，等群里有人说一句再试）"
            return ToolCallResult(
                ok=False, error=f"调用出错：{exc}{hint}", tool=name, params=call_params
            )
        captions, pending_images = await _caption_result_images(
            self.plugin, images, scene=str(name)
        )
        if captions:
            text = "\n".join([part for part in (text, "；".join(captions)) if part])
        text = (text or "").strip()
        if not text and not pending_images:
            return ToolCallResult(
                ok=False, error="工具返回了空结果", tool=name, params=call_params
            )
        if not text:
            text = "（工具返回了一张图片）"
        return ToolCallResult(
            ok=True,
            text=text,
            tool=name,
            params=call_params,
            image_urls=pending_images,
            # 原图另存一份：转述成功时 image_urls 会空，但这张图还是得发到群里
            attachments=list(images),
        )

    def _filter_params(
        self, name: str, params: dict[str, Any], tool: Any = None
    ) -> dict[str, Any]:
        """只把工具自己声明过的参数传给它。

        参数是辅助模型按 schema 填的，难免多塞一两个没声明的键；
        直接透传会让工具的 handler 抛 TypeError，最后表现为"工具没返回任何东西"。
        """

        given = dict(params or {})
        if not given:
            return {}
        if tool is None:
            manager = self._manager()
            for candidate in list(getattr(manager, "func_list", []) or []):
                if getattr(candidate, "name", "") == name:
                    tool = candidate
                    break
        schema = dict(getattr(tool, "parameters", {}) or {})
        properties = dict(schema.get("properties") or {})
        if not properties:
            return given
        return {key: value for key, value in given.items() if key in properties}


class AstrBotPersona:
    """PersonaPort 实现。拿不到人格时降级为空字符串。"""

    def __init__(self, plugin: "VirtualWorldPlugin") -> None:
        self.plugin = plugin

    def _config(self):
        engine = getattr(self.plugin, "engine", None)
        world = getattr(engine, "world", None) if engine is not None else None
        return getattr(world, "persona", None)

    async def astrbot_persona_text(self, session_id: str) -> str:
        """AstrBot 给这个会话选的那份人格（按会话 / 配置文件解析）。"""

        manager = getattr(self.plugin.context, "persona_manager", None)
        if manager is None:
            return ""
        persona = None
        resolver = getattr(manager, "get_default_persona_v3", None)
        if callable(resolver):
            try:
                persona = await resolver(session_id)
            except Exception:
                persona = None
        if persona is None:
            return ""
        for field in ("prompt", "system_prompt", "text"):
            value = getattr(persona, field, None)
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(persona, dict):
            for field in ("prompt", "system_prompt", "text"):
                value = persona.get(field)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    async def get_persona_text(self, session_id: str) -> str:
        """实际交给她的人设：按配置里的模式决定用哪一份。"""

        config = self._config()
        mode = str(getattr(config, "mode", "astrbot") or "astrbot")
        own = str(getattr(config, "text", "") or "").strip()
        if mode == "plugin" and own:
            return own
        base = await self.astrbot_persona_text(session_id)
        if mode == "append" and own:
            return f"{base}\n\n{own}".strip() if base.strip() else own
        # astrbot 模式，或者 plugin 模式但没填：都用 AstrBot 那份
        return base


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for attr in ("completion_text", "text", "message"):
        found = getattr(value, attr, None)
        if isinstance(found, str) and found:
            return found
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _tool_result_text(value: Any) -> str:
    """把工具返回值变成文本。

    MCP 工具和 AstrBot 的部分内置工具返回的是 ``CallToolResult``（内容是若干 TextContent），
    直接 ``str()`` 会得到一串对象表示，这里把里面的文本挑出来。
    """

    if value is None:
        return ""
    content = getattr(value, "content", None)
    if isinstance(content, (list, tuple)) and content:
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if parts:
            return "\n".join(parts)
    return _stringify(value)


MAX_RESULT_CHARS = 800
"""工具 / 指令返回内容进提示词前的截断长度（日志里另有一份更短的）。"""


async def _chain_text_and_images(value: Any) -> tuple[str, list[str]] | None:
    """把 ``MessageChain`` / ``MessageEventResult`` 拆成「人话」和「图片地址」。

    直接 ``str()`` 会得到一坨 dataclass 表示：里面虽然夹着正文，但也塞满了
    ``Plain(type=...)`` 之类的噪音，既费 token，模型还容易照抄。这里按组件类型
    老老实实还原——文本归文本、@ 归 @、图片挑出来单独交给多模态模型看。

    返回 ``None`` 表示"这个值不是消息链"，交给别的分支处理。
    """

    chain = getattr(value, "chain", None)
    if not isinstance(chain, (list, tuple)):
        return None
    texts: list[str] = []
    images: list[str] = []
    for component in chain:
        if _component_named(component, "Plain"):
            text = str(getattr(component, "text", "") or "")
            if text:
                texts.append(text)
        elif _component_named(component, "At"):
            who = str(getattr(component, "name", "") or "").strip()
            qq = str(getattr(component, "qq", "") or "").strip()
            texts.append(f"@{who or qq}")
        elif _component_named(component, "Image"):
            ref = await _resolve_image_ref(component)
            if ref and ref not in images:
                images.append(ref)
            texts.append(f"［图片{len(images)}］" if images else "［图片］")
        elif _component_named(component, "Face"):
            texts.append("［表情］")
        elif _component_named(component, "Reply"):
            continue
        else:
            # 其它组件（语音、文件、转发…）只报个类型名，别把对象表示塞进提示词
            name = type(component).__name__
            if name:
                texts.append(f"［{name}］")
    return "".join(texts).strip(), images


def _exception_where(exc: BaseException) -> str:
    """异常最后落在哪个文件:行（哪个函数）：给别人看的"这是谁的锅"。"""

    tb = exc.__traceback__
    last = None
    while tb is not None:
        last = tb
        tb = tb.tb_next
    if last is None:
        return ""
    frame = last.tb_frame
    name = os.path.basename(str(frame.f_code.co_filename or ""))
    return f"{name}:{last.tb_lineno} {frame.f_code.co_name}"


async def _result_text(value: Any) -> tuple[str, list[str]]:
    """工具 / 指令的返回值 → (文字, 图片地址)。"""

    if value is None:
        return "", []
    parts = await _chain_text_and_images(value)
    if parts is not None:
        text, images = parts
        return _clip_brief(text), images
    if _component_named(value, "Image"):
        ref = await _resolve_image_ref(value)
        return "", ([ref] if ref else [])
    return _clip_brief(_tool_result_text(value)), []


def _clip_brief(text: Any) -> str:
    """返回内容截断：进提示词和进日志都用它，别让一坨 base64 撑爆上下文。"""

    body = str(text or "").strip()
    if len(body) <= MAX_RESULT_CHARS:
        return body
    return body[:MAX_RESULT_CHARS] + "…（内容过长已截断）"


def _as_tool_result(text: str) -> Any:
    """把工具返回的文字包装成 AstrBot 钩子认的 ``CallToolResult``。"""

    try:
        from mcp.types import CallToolResult, TextContent
    except Exception:
        return None
    try:
        return CallToolResult(
            content=[TextContent(type="text", text=str(text or ""))],
            isError=False,
        )
    except Exception:
        return None


class _ToolRunContext:
    """新版工具的 ``call(context, **kwargs)`` 需要的运行上下文。

    等价于 AstrBot 的 ``ContextWrapper[AstrAgentContext]``：工具真正用到的只有
    ``context.context.event``、``context.context.context`` 和 ``context.tool_call_timeout``。
    拿得到 AstrBot 自己的类时用真的，拿不到（老版本 / 单测环境）就用这个替身。
    """

    def __init__(self, plugin: "VirtualWorldPlugin", event: Any) -> None:
        try:
            from astrbot.core.agent.run_context import ContextWrapper
            from astrbot.core.astr_agent_context import AstrAgentContext

            inner = AstrAgentContext(context=plugin.context, event=event)
            self._wrapper = ContextWrapper(context=inner, tool_call_timeout=120)
        except Exception:
            self._wrapper = None
        self.context = types.SimpleNamespace(context=plugin.context, event=event)
        self.messages: list[Any] = []
        self.tool_call_timeout = 120

    def __getattr__(self, name: str) -> Any:
        wrapper = self.__dict__.get("_wrapper")
        if wrapper is not None:
            return getattr(wrapper, name)
        raise AttributeError(name)


def _tool_invoker(tool: Any):
    """按 AstrBot 的优先级挑出调用方式，返回 ``fn(event, params, plugin)``。"""

    handler = getattr(tool, "handler", None)
    if callable(handler):

        def call_handler(event: Any, params: dict[str, Any], plugin: Any):
            return handler(event, **params)

        return call_handler

    call = getattr(tool, "call", None)
    if callable(call) and _is_call_override(call):

        def call_method(event: Any, params: dict[str, Any], plugin: Any):
            return call(_ToolRunContext(plugin, event), **params)

        return call_method

    run = getattr(tool, "run", None)
    if callable(run):

        def call_run(event: Any, params: dict[str, Any], plugin: Any):
            return run(event, **params)

        return call_run

    return None


def _is_call_override(call: Any) -> bool:
    """判断 ``call`` 是工具自己实现的，而不是 ``FunctionTool`` 那个只抛错的默认版。"""

    func = getattr(call, "__func__", None)
    if func is None:
        return True
    try:
        from astrbot.core.agent.tool import FunctionTool

        return func is not FunctionTool.call
    except Exception:
        return not str(getattr(func, "__qualname__", "")).endswith("FunctionTool.call")


# ======================================================================
# 编辑器访问密码（AstrBot Dashboard 之外的二次保护，可选）
# ======================================================================


class EditorAuth:
    """独立访问密码：密码哈希存 sqlite，token 只在内存里，含失败锁定。"""

    def __init__(self, plugin: "VirtualWorldPlugin", config_password: str = "") -> None:
        self.plugin = plugin
        self.config_password = (config_password or "").strip()
        self.tokens: dict[str, float] = {}
        self.failures: list[float] = []
        self.lock_until = 0.0

    # ---------------- 密码 ----------------

    def stored_hash(self) -> dict[str, str]:
        return self.plugin.db.raw.kv_get("editor_password", {}) or {}

    def has_password(self) -> bool:
        return bool(self.config_password or self.stored_hash().get("hash"))

    def set_password(self, raw: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            self.plugin.db.raw.kv_delete("editor_password")
            return
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt.encode("utf-8"), 100_000)
        self.plugin.db.raw.kv_set(
            "editor_password", {"salt": salt, "hash": digest.hex()}
        )

    def verify(self, raw: str) -> bool:
        raw = (raw or "").strip()
        if not raw:
            return False
        stored = self.stored_hash()
        if stored.get("hash"):
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                raw.encode("utf-8"),
                str(stored.get("salt", "")).encode("utf-8"),
                100_000,
            ).hex()
            if hmac.compare_digest(digest, str(stored["hash"])):
                return True
        if self.config_password:
            return hmac.compare_digest(raw, self.config_password)
        return False

    # ---------------- 锁定与 token ----------------

    def locked(self) -> int:
        if self.lock_until and time.time() < self.lock_until:
            return int(self.lock_until - time.time())
        return 0

    def note_failure(self) -> None:
        now = time.time()
        self.failures = [ts for ts in self.failures if now - ts < LOCKOUT_SECONDS]
        self.failures.append(now)
        if len(self.failures) >= MAX_FAILED_ATTEMPTS:
            self.lock_until = now + LOCKOUT_SECONDS
            self.failures.clear()

    def issue_token(self) -> str:
        token = secrets.token_urlsafe(24)
        self.tokens[token] = time.time() + TOKEN_TTL_SECONDS
        self._prune()
        return token

    def revoke(self, token: str) -> None:
        self.tokens.pop(token, None)

    def check(self, token: str) -> bool:
        if not self.has_password():
            return True
        if not token:
            return False
        expires = self.tokens.get(token)
        if not expires or expires < time.time():
            self.tokens.pop(token, None)
            return False
        return True

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, exp in self.tokens.items() if exp < now]:
            self.tokens.pop(token, None)


# ======================================================================
# 插件主体
# ======================================================================


@register(
    PLUGIN_NAME,
    "exneverbur",
    "给 Bot 一个私有空间、动作、日程、场景记忆和工具能力，让 ta 像住在群里一样生活。",
    "v2.0.1",
)
class VirtualWorldPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config

        self.enabled = _cfg_bool(config.get("enabled"), True)
        self.web_enabled = _cfg_bool(config.get("web_enabled"), True)
        self.llm_provider_id = str(config.get("llm_provider_id") or "").strip()
        # 打杂模型（文本、便宜）：补工具参数、压上下文、判断图和话题的关系
        self.vision_provider_id = str(config.get("vision_provider_id") or "").strip()
        self.helper_provider_id = (
            str(config.get("helper_provider_id") or "").strip()
            # 老配置里"上下文压缩"和"关系分析"是两个独立模型：并进打杂模型，
            # 让用户只需要理解两个便宜档（看图 / 文本）
            or str(config.get("context_provider_id") or "").strip()
        )
        self.utility_provider_id = self.helper_provider_id
        # 判断模型：只接"挑一个 / 打分 / 抽字段"这类纯判断。留空跟随打杂模型。
        self.judge_provider_id = (
            str(config.get("judge_provider_id") or "").strip() or self.helper_provider_id
        )
        self.consolidate_provider_id = (
            str(config.get("consolidate_provider_id") or "").strip()
            or self.utility_provider_id
        )
        # 事件模型：写"这件事的结果"那个模型。留空跟随打杂模型。
        self.event_provider_id = (
            str(config.get("event_provider_id") or "").strip() or self.utility_provider_id
        )
        # 内容生成模型：只在编辑器里"批量生成动作 / 地点"时用，留空回落到主模型
        self.creator_provider_id = str(config.get("creator_provider_id") or "").strip()
        self._sampling_config = {
            "enabled": _cfg_bool(config.get("sampling_enabled"), False),
            "temperature": config.get("sampling_temperature"),
            "top_p": config.get("sampling_top_p"),
            "frequency_penalty": config.get("sampling_frequency_penalty"),
            "presence_penalty": config.get("sampling_presence_penalty"),
        }
        self.tick_interval = max(5, _cfg_int(config.get("tick_interval"), 60))
        self.decider_interval = max(
            self.tick_interval, _cfg_int(config.get("decider_interval"), 300)
        )
        self.debug = _cfg_bool(config.get("debug"), False)
        # 接管回复也要过一遍 AstrBot 的回复钩子，别的插件（好感度之类）才看得见
        self.reply_hook_bridge = _cfg_bool(config.get("reply_hook_bridge"), True)
        self.log_level = str(config.get("log_level") or "INFO").upper()

        data_dir = _resolve_data_dir(os.path.dirname(os.path.abspath(__file__)))
        self.store = ConfigStore(data_dir)
        created = self.store.ensure_files()
        self.db = AsyncDatabase(self.store.db_path)
        self.auth = EditorAuth(self, str(config.get("web_password") or ""))

        self._last_events: "OrderedDict[str, AstrMessageEvent]" = OrderedDict()
        self._llm_responses: "OrderedDict[str, Any]" = OrderedDict()
        """最近一次主模型调用的原始响应（按会话存，供回复钩子使用）。"""
        self._tick_task: asyncio.Task | None = None
        self._self_initiated_depth = 0
        self._self_initiated_tasks: set[asyncio.Task] = set()
        """正在调模型的那几条协程（我们自己的调用）。"""
        self._self_initiated_untracked = 0
        self._reply_locks: dict[str, asyncio.Lock] = {}
        """每个会话一条：同一条会话的接管排队执行，避免两条回复互相不知道对方。"""
        self._live_say_tail: dict[str, str] = {}
        """刚即时发出去的那一句：连着说下一句时按它的字数停一下，保持打字的节奏。"""
        # 发送方持有「失败冷却」，所以自己留一份（回退响应路径也要用它判断）
        self.messenger = AstrBotMessenger(self)
        # 看图（多模态）：图片转述、天气工具返回的图都走它
        self.vision = AstrBotVision(self, self.vision_provider_id)
        # 人设：默认沿用 AstrBot 给这个会话选的那份，也可以在「全局设置 → 她是谁」里
        # 换成插件自己的一份（跟着预设走，不再随会话组漂移）
        self.persona_port = AstrBotPersona(self)

        self.engine = VirtualWorldEngine(
            store=self.store,
            db=self.db,
            llm=AstrBotLLM(self, sampling=self.sampling_params()),
            helper_llm=AstrBotLLM(self, self.helper_provider_id, sampling=self.sampling_params()),
            judge_llm=AstrBotLLM(self, self.judge_provider_id, sampling=self.sampling_params()),
            context_llm=AstrBotLLM(self, self.utility_provider_id, sampling=self.sampling_params()),
            creator_llm=AstrBotLLM(self, self.creator_provider_id, sampling=self.sampling_params()),
            event_llm=AstrBotLLM(self, self.event_provider_id, sampling=self.sampling_params()),
            consolidate_llm=AstrBotLLM(self, self.consolidate_provider_id, sampling=self.sampling_params()),
            describer=self.vision,
            messenger=self.messenger,
            tools=AstrBotTools(self),
            commands=AstrBotCommands(self),
            persona=self.persona_port,
            clock=None,
            tick_seconds=float(self.tick_interval),
            decider_interval=float(self.decider_interval),
            debug=self.debug,
            logger=self.logger,
        )
        # 调试回显实时发：工具/指令调用在发生的那一下就走这条通道，
        # 不再等整轮动作跑完才一起推给群里
        self.engine.debug_sink = self._send_debug_live
        # 即时发言：慢动作（联网检索、生图、录视频这类）开始之前她已经说出口的那几句，
        # 当场发出去，不再等整段流水线跑完才一起冒出来
        self.engine.say_sink = self._send_live_say
        # 正在处理「推进 tick」的会话：连点时只认第一次，别攒成一串 tick
        self._advancing: set[str] = set()

        self._register_web_apis()

        for name in created:
            self.logger.info(f"[virtual_world] 首次启动，已生成默认配置 {name}")
        for warning in self.engine.load_warnings:
            self.logger.warning(f"[virtual_world] 配置提醒：{warning}")
        if not self.engine.enabled_session_ids():
            self.logger.info(
                "[virtual_world] 会话白名单为空，请在群聊里发送「/vw session add」"
                "或在 WebUI 的虚拟世界编辑器里添加会话。"
            )

    # ================= 生命周期 =================

    async def initialize(self) -> None:
        if not self.enabled:
            self.logger.info("[virtual_world] 插件已禁用（enabled=false）")
            return
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick_loop())
        self.logger.info(
            f"[virtual_world] 世界时钟已启动：每 {self.tick_interval} 秒 1 tick，"
            f"决策间隔 {self.decider_interval} 秒"
        )
        if self.web_enabled:
            self.logger.info(
                "[virtual_world] 网页编辑器：WebUI 插件详情页 → 「虚拟世界编辑器」"
            )

    async def terminate(self) -> None:
        # 宿主重载插件时是「先调 terminate()，再解绑事件处理器」，
        # 这个窗口里进来的消息还会走到这个实例上。标记一下，让所有入口直接装死。
        self._retired = True
        task, self._tick_task = self._tick_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self.db.close()
        except Exception:
            pass
        self.logger.info("[virtual_world] 已停止世界时钟")

    @property
    def retired(self) -> bool:
        """这个实例已经被终止（重载/卸载）了。"""

        return bool(getattr(self, "_retired", False))

    async def _tick_loop(self) -> None:
        # 固定节拍：按「上一次唤醒 + 间隔」推算下一次唤醒，而不是跑完再睡 60 秒。
        # 后者会被每轮 tick 自身的耗时（调模型可能要好几秒）一直往后推，
        # 攒着攒着就整分钟跳过去，那一分钟到点的日程永远等不到。
        next_at = time.time() + self.tick_interval
        while True:
            try:
                await asyncio.sleep(max(0.5, next_at - time.time()))
                next_at += self.tick_interval
                if next_at < time.time():
                    # 落后太多（宿主机卡顿 / 长时间睡眠）就重新对齐，别追赶
                    next_at = time.time() + self.tick_interval
                outcomes = await self.engine.tick()
                if self.debug and outcomes:
                    for outcome in outcomes:
                        self.logger.info(
                            f"[virtual_world] tick {outcome.session_id}: "
                            f"{'; '.join(outcome.notes)} "
                            f"messages={outcome.messages}"
                        )
                for session_id in self.engine.enabled_session_ids():
                    await self.engine.maybe_decide(session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(f"[virtual_world] 世界时钟出错：{exc}")

    # ================= 事件记录 =================

    def last_event(self, session_id: str) -> AstrMessageEvent | None:
        return self._last_events.get(session_id)

    async def pending_intervention(self, session_id: str) -> float:
        """联动接口：她正在等群友拿主意吗？返回等待截止时间戳（0 = 不在等）。

        「意图路由」在等待期间会把这个会话的消息**合并放行、跳过回复冷却**，
        让群友的回应尽快交到她手里；查不到或出错一律返回 0，路由照旧工作。
        """

        if not session_id:
            return 0.0
        try:
            return float(await self.engine.pending_intervention(session_id))
        except Exception:
            return 0.0

    def last_event_any(self) -> AstrMessageEvent | None:
        """最近一次见过的消息事件（拿不到会话 id 时给工具调用兜底）。"""

        if not self._last_events:
            return None
        return next(reversed(self._last_events.values()))

    def remember_llm_response(self, session_id: str, response: Any) -> None:
        """记下主模型的原始响应（回复钩子要拿它给别的插件看）。"""

        if response is None or not session_id:
            return
        self._llm_responses[session_id] = response
        self._llm_responses.move_to_end(session_id)
        while len(self._llm_responses) > 50:
            self._llm_responses.popitem(last=False)

    def take_llm_response(self, session_id: str) -> Any:
        response = self._llm_responses.pop(session_id, None)
        return response

    def _pronoun(self) -> str:
        """由全局设置里的性别决定称呼：她 / 他 / ta。"""

        return pronoun_for(self.engine.world.gender)

    def _can_admin(self, event: AstrMessageEvent) -> bool:
        """能不能执行管理类指令。

        AstrBot 自己的管理员永远可以；另外「全局设置 → 管理员 QQ」里列出来的人也可以
        （有些群不希望为了用几条指令就去动 AstrBot 的管理员配置）。
        """

        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        listed = {str(item).strip() for item in (self.engine.world.admin_ids or [])}
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        return bool(sender) and sender in listed

    # ================= 给其他插件用的只读接口 =================

    async def social_snapshot(self, session_id: str) -> dict[str, Any] | None:
        """某个会话当前的状态快照，供其他插件联动（例如意图路由按社交欲调阈值）。

        会话没启用本插件时返回 None，调用方应当回落到自己的默认行为。
        """

        if not self.enabled or not session_id:
            return None
        try:
            if not self.engine.is_enabled(session_id):
                return None
            state = await self.engine.load_state(session_id, cold_start=False)
        except Exception:
            return None
        return {
            # affect 是「心潮」；social 是老名字，保留给还没更新的联动方
            "affect": round(state.affect, 4),
            "social": round(state.affect, 4),
            # 效价：心情好坏（0.5 中性）。联动方可以据此区分"孤独想聊"和"恼火想怼"
            "valence": round(state.valence, 4),
            "storm": bool(state.storm),
            "loneliness": round(state.loneliness, 4),
            "energy": round(state.energy, 4),
            "curiosity": round(state.curiosity, 4),
            "boredom": round(state.boredom, 4),
            "willingness": round(self.engine.willingness(state), 4),
            "mood": state.mood,
            "state": state.state,
            "style_cell": state.last_style_cell,
            # 睡着时联动方最好直接不判断（意图路由会拿它当硬门）
            "sleeping": bool(self.engine.is_asleep(state)),
            "node_id": state.node_id,
            "world_time": state.world_time,
            "interject_enabled": bool(self.engine.world.decider.enabled),
        }

    async def reply_willingness(self, session_id: str) -> float | None:
        """她对「现在该不该开口」的整体意愿（0~1）。

        综合孤独感、无聊、好奇、心潮与疲惫、被冷落等因素，
        不启用本插件的会话返回 None，调用方应回落到自己的默认行为。
        """

        snapshot = await self.social_snapshot(session_id)
        if snapshot is None:
            return None
        return float(snapshot.get("willingness") or 0.0)

    def _remember_event(self, event: AstrMessageEvent) -> None:
        session_id = event.unified_msg_origin
        self._last_events[session_id] = event
        self._last_events.move_to_end(session_id)
        while len(self._last_events) > 200:
            self._last_events.popitem(last=False)

    async def _annotate_message(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        sources: list[str] | None = None,
        summarize_forward: bool = False,
    ) -> str:
        """给消息补上模型看不到的部分：图片里有什么、这是不是一条转发。

        ``sources``：图片地址已经取过了就传进来，省得同一个组件解析两遍
        （NapCat 这类协议端解析一次可能要真去取图）。

        ``summarize_forward``：这条转发值得读一遍时（她要处理的 @ / 私聊 / 唤醒），
        用多模态模型把转发内容（含里面的图）压成摘要替换正文；关掉或读不出来
        就退回原来那句「这是一条转发的聊天记录」。
        """

        notes: list[str] = []
        if _is_forwarded(event):
            summary = await self._forward_summary_text(event) if summarize_forward else ""
            if summary:
                text = _with_forward_summary(text, summary)
                notes.append(
                    "这是一条转发消息，上面那段是它的内容摘要，不是当前群里正在说的话"
                )
            else:
                notes.append("这是一条转发的聊天记录，不是当前群里正在说的话")
        mention = _mention_note(event)
        if mention:
            notes.append(mention)
        quoted = await self._quoted_note(event)
        if quoted:
            notes.append(quoted)
        sticker = _sticker_note(event)
        if sticker:
            notes.append(sticker)
        notes.extend(await self._annotate_images(event, text, sources=sources))
        if not notes:
            return text
        return f"{text}\n［{'；'.join(notes)}］".strip()

    async def _quoted_note(self, event: AstrMessageEvent) -> str:
        """他引用了哪一条：聊天记录里还看得到就给个开头，看不到才写原文。

        引用内容本来完全没进正文（只喂给了看图那一步），于是「如何评价」这种
        靠引用才有意义的话，她只能对着聊天记录里被掐成半句的旧消息猜。
        """

        who, text = _quoted_parts(event)
        body = " ".join(str(text or "").split())
        if not body:
            return ""
        someone = who or "有人"
        context = self.engine.world.context
        line_limit = max(40, int(getattr(context, "chat_line_chars", 500) or 500))
        found = await self.engine.quote_in_records(event.unified_msg_origin, body)
        # 记录里就有**而且那一行没被截断**时，只点出开头让她自己往上对照；
        # 那条本身已经被截断（或者干脆不在留档里）时，还是把原文写全——
        # 否则她对着"还有 N 字没显示"的那半句，根本不知道被引用的是哪一段。
        if found and len(body) <= line_limit:
            head = body[:40] + ("…" if len(body) > 40 else "")
            return (
                f"他引用的是上面聊天记录里的那条（{someone}：「{head}」）"
                "——照上面那条看，这不是他这次打的字"
            )
        limit = max(0, int(getattr(context, "quote_chars", 1000) or 0))
        if limit <= 0:
            return f"他引用了一条更早的消息（{someone}），那条已经不在聊天记录里了"
        shown = body if len(body) <= limit else clip_line(body, limit)
        where = "上面聊天记录里那条太长，只显示了开头" if found else "聊天记录里已经没有了"
        return (
            f"他引用了一条更早的消息（{someone}），{where}，"
            f"原文：「{shown}」——这是引用，不是他这次打的字"
        )

    async def _forward_summary_text(self, event: AstrMessageEvent) -> str:
        """把这条转发读成摘要：关掉、没配多模态模型、读不出来都返回空串。"""

        vision = getattr(self, "vision", None)
        if vision is None or not vision.enabled:
            return ""
        try:
            settings = self.engine.world.vision
            if not bool(getattr(settings, "forward_summary", True)):
                return ""
            digest, images, forward_ids = await _forward_digest(event)
            if not digest.strip() and not images:
                return ""
            summary = await vision.summarize_forward(
                digest,
                images,
                fingerprint=_forward_fingerprint(forward_ids, digest, images),
            )
            if summary:
                await self.engine.note_vision(
                    event.unified_msg_origin,
                    ok=True,
                    images=len(images),
                    detail=f"转发聊天记录已压成摘要（{len(summary)} 字）",
                )
            return summary
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 转发摘要失败：{exc}")
            return ""

    async def _annotate_images(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        sources: list[str] | None = None,
    ) -> list[str]:
        """图片这一段的处理：转了述就写描述，没配转述模型就交给多模态主模型。

        图片本来在消息里是看不见的，出了任何岔子都要留下一条日志——
        否则用户只会看到"模型好像没看到图"，却没有任何线索。
        """

        components = _image_components(event)
        resolved = (
            [str(item) for item in sources if str(item).strip()]
            if sources is not None
            else await _image_sources(event)
        )
        if not components and not resolved:
            return []
        session_id = event.unified_msg_origin
        sources = resolved
        vision = getattr(self, "vision", None)
        if not sources:
            detail = "收到图片但拿不到可用地址"
            debug = _image_components_debug(event)
            if debug:
                detail = f"{detail}（组件字段：{debug}）"
            await self.engine.note_vision(
                session_id, ok=False, images=len(components), detail=detail
            )
            # QQ 表情包（大表情）几乎都取不到图：别让她以为"什么都没收到"，
            # 后面 _sticker_note 会把它的文案写进消息里。
            if components and all(
                _component_named_any(item, _FACE_COMPONENT_NAMES)
                for item in components
            ):
                return ["对方发的是 QQ 表情 / 表情包，插件取不到图（已记进日志）"]
            return ["对方发了一张图片，但插件没能取到图片地址（已记进日志）"]

        captions: list[str] = []
        if vision is None or not vision.enabled:
            return [f"这条消息带了 {len(sources)} 张图片，图片会一起发给你"]
        # 收到的图**现在就**看看是不是已经超过「一次带几张」的上限了：
        # 超出的那几张（先来的）立刻转成文字，不等调主模型那一刻。
        notes = await self._caption_overflow(event, sources)
        head = f"这条消息带了 {len(sources)} 张图片，会一起发给你"
        return [head, *notes]

    async def _caption_overflow(
        self, event: AstrMessageEvent, sources: list[str]
    ) -> list[str]:
        """收到的图已经超过「一次带几张」：把更早、还没转述的那几张先转成文字。

        上限之内那几张会随这次请求交给主模型看，不必转述（同一张图只看一遍）；
        超出的那几张反正不会被带上，趁**收到图的时候**就转掉——等调主模型那一刻
        再转，她已经在等回复了。返回要写进这条消息注解的那几句。
        """

        session_id = event.unified_msg_origin
        cap = max(1, int(self.engine.world.context.image_max))
        history = await self.engine.chat_images_for_reply(session_id, PENDING_IMAGE_KEEP)
        candidates = list(
            dict.fromkeys(
                [
                    *[
                        str(item.get("url") or "").strip()
                        for item in history
                        if str(item.get("url") or "").strip()
                    ],
                    *[str(item).strip() for item in sources if str(item).strip()],
                ]
            )
        )
        keep = set(candidates[-cap:])
        older = [url for url in candidates if url not in keep]
        if not older:
            return []
        return await self._caption_old_images(event, older, sources=sources)

    async def _reply_images(
        self, event: AstrMessageEvent, sources: list[str]
    ) -> tuple[list[str], dict[str, str], list[str]]:
        """这一轮直接交给模型看的图、聊天记录里那几张图的编号，以及要转成文字的那几张。

        转述只在这里做，而且只转"不会随这次请求交给主模型"的那几张：

        - 主模型能吃图：最多带 ``image_max`` 张过去，**超出的旧图**（先来的那几张）
          打包转述一次，描述挂回它们各自那条聊天记录；
        - 主模型看不见图：这一轮该给她看的图全部打包转述一次（一条消息一次 →
          一整轮一次），转完只交文字，不再附图；
        - 没配转述模型：这条消息自己的图 + 「自上次回复以来收到的图」都发过去。

        三种情况下"转述"都只发生一次：图片本来就要交给主模型看时再转述一遍，
        等于同一张图看两遍（白花钱），连发几张还会连着调好几次。
        """

        session_id = event.unified_msg_origin
        context = self.engine.world.context
        cap = max(1, int(context.image_max))
        vision = getattr(self, "vision", None)
        captioned = bool(vision is not None and vision.enabled)
        inline = await self._chat_images_inline(session_id)
        pending = await self.engine.take_pending_images(session_id)
        # 按时间排好（先来的在前）：被搭话的那几条消息里的图排前面，最新那张在最后
        own = list(
            dict.fromkeys(
                [str(ref).strip() for ref in [*pending, *sources] if str(ref).strip()]
            )
        )
        # 这一轮"看得到"的全部图（旧的在前、最新那张在最后）：
        # 留档里更早的图也算，它们同样占"这一轮看了几张"的账
        history = await self.engine.chat_images_for_reply(session_id, PENDING_IMAGE_KEEP)
        all_urls = list(
            dict.fromkeys(
                [
                    *[
                        str(item.get("url") or "").strip()
                        for item in history
                        if str(item.get("url") or "").strip()
                    ],
                    *own,
                ]
            )
        )
        # 会被交给主模型的那几张：这一轮被搭话的那几张优先，剩下的名额给留档里更早的图
        own_refs = own[-cap:]
        room = max(0, cap - len(own_refs))
        # 留档里的图另有上限（``chat_image_max``）：附太多旧图会把她这次要看的东西冲淡
        want = min(max(1, int(context.chat_image_max)), room)
        picks = history[-want:] if (inline and want) else []
        refs: list[str] = []
        marks: dict[str, str] = {}
        for item in picks:
            url = str(item.get("url") or "").strip()
            if url and url not in refs:
                refs.append(url)
                marks[url] = f"图{len(marks) + 1}"
        for ref in own_refs:
            if ref not in refs:
                refs.append(ref)
        refs = refs[:cap]
        marks = {url: label for url, label in marks.items() if url in refs}
        if captioned and not inline:
            # 主模型看不见图：这一轮的所有图都转成文字（一次打包），也就不附图了
            refs, marks = [], {}
        notes: list[str] = []
        if captioned:
            dropped = [
                url for url in all_urls if url and url not in set(refs)
            ]
            notes = await self._caption_old_images(event, dropped, sources=sources)
        if not refs:
            return [], {}, notes
        detail = f"把 {len(refs)} 张图交给多模态主模型"
        if marks:
            detail += f"（聊天记录里的 {len(marks)} 张已编号）"
        await self.engine.note_vision(
            session_id, ok=True, images=len(refs), detail=detail
        )
        return refs, marks, notes

    async def _caption_old_images(
        self,
        event: AstrMessageEvent,
        urls: list[str],
        *,
        sources: list[str] | None = None,
    ) -> list[str]:
        """把这几张图打包转述一次，返回要写进这条消息注解的那几句。

        挂在哪：属于**这条消息**的图（她正看着的那几张）写进这条消息的注解；
        更早的图把描述挂回它们原来那条聊天记录上——不然描述会串到别人头上，
        下一轮她也对不上"哪张图是什么"。
        """

        session_id = event.unified_msg_origin
        vision = getattr(self, "vision", None)
        # 已经转述过的（同一张图在群里发两遍、或者上一轮转过了）不再转第二遍
        targets = self.engine.uncaptioned_images(
            [str(url).strip() for url in urls if str(url).strip()]
        )
        if vision is None or not vision.enabled or not targets:
            return []
        try:
            captions = await vision.describe(
                targets,
                question=str(event.get_message_str() or ""),
                quoted=_quoted_text(event),
                context_lines=await self._recent_chat_lines(event),
            )
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 图片转述异常：{exc}")
            captions = []
        pairs = [
            (url, str(caption).strip())
            for url, caption in zip(targets, captions or [])
            if str(caption or "").strip()
        ]
        if not pairs:
            reason = str(getattr(vision, "last_error", "") or "转述没有返回内容")
            await self.engine.note_vision(
                session_id, ok=False, images=len(targets), detail=reason
            )
            return ["有几张图没能转述成功，只当看过了（已记进日志）"]
        here = {str(item).strip() for item in (sources or [])}
        notes: list[str] = []
        elsewhere: dict[str, str] = {}
        for url, caption in pairs:
            if url in here:
                notes.append(caption)
            else:
                elsewhere[url] = caption
        attached = await self.engine.attach_image_captions(session_id, elsewhere)
        self.engine.mark_images_captioned([url for url, _text in pairs])
        notes = [
            f"图片{index + 1}：{text}" for index, text in enumerate(notes) if text
        ]
        await self.engine.note_vision(
            session_id,
            ok=True,
            images=len(pairs),
            detail=(
                f"转述了 {len(pairs)} 张（没随这次请求交给主模型的那几张）"
                + (f"，其中 {attached} 张挂回了它们原来那条消息" if attached else "")
                + "："
                + "；".join(text for _url, text in pairs)
            ),
        )
        return notes

    async def _chat_images_inline(self, session_id: str) -> bool:
        """聊天记录里的图要不要直接发给主模型（配置 + Provider 勾选的模态）。"""

        mode = str(
            getattr(self.engine.world.context, "chat_image_inline", "auto") or "auto"
        ).lower()
        if mode == "never":
            return False
        if mode == "always":
            return True
        return bool(await self._main_model_sees_images(session_id))

    async def _main_model_sees_images(self, session_id: str) -> bool | None:
        """主模型能不能吃图：读 AstrBot 里这个 Provider 勾选的模态。

        读不到（老版本、没有这个字段、不是 AstrBot 的 Provider）返回 ``None``——
        调用方按"不能"处理，用户可以把它改成 ``always`` 强制打开。
        """

        try:
            provider_id = (getattr(self, "llm_provider_id", "") or "").strip()
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(session_id)
            if not provider_id:
                return None
            provider = self.context.get_provider_by_id(provider_id)
            return _provider_supports_images(provider)
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 读主模型的模态失败：{exc}")
            return None

    async def _recent_chat_lines(self, event: AstrMessageEvent) -> list[str]:
        """最近几句群聊，用来给图片转述提供话题背景。"""

        session_id = event.unified_msg_origin
        try:
            state = await self.engine.load_state(session_id, cold_start=False)
            items = self.engine.chat_context(state)
        except Exception:
            return []
        lines: list[str] = []
        for item in items:
            who = "她" if item.get("is_self") else (item.get("name") or item.get("user_id") or "")
            content = " ".join(str(item.get("text") or "").split())
            if content:
                lines.append(f"{who}: {content[:80]}")
        return lines

    def set_self_initiated(self) -> int:
        """标记"接下来这段是我们自己发起的调用"。"""

        self._self_initiated_depth += 1
        task = self._current_task()
        if task is not None:
            self._self_initiated_tasks.add(task)
            # 防御：万一某次调用没走到 reset（异常被吞、任务被取消……），
            # 集合里堆着死任务会让后面的钩子判断出现意外，定期清一次只留当前任务。
            if len(self._self_initiated_tasks) > 8:
                self._self_initiated_tasks.clear()
                self._self_initiated_tasks.add(task)
        return self._self_initiated_depth

    def reset_self_initiated(self, _token: int = 0) -> None:
        self._self_initiated_depth = max(0, self._self_initiated_depth - 1)
        task = self._current_task()
        if task is not None:
            self._self_initiated_tasks.discard(task)

    @staticmethod
    def _current_task() -> asyncio.Task | None:
        try:
            return asyncio.current_task()
        except RuntimeError:
            # 没有正在跑的事件循环（同步收尾之类），当作"不在任务里"
            return None

    def in_self_initiated_call(self) -> bool:
        """当前这条协程是不是"我们自己正在调模型"的那一条。

        按协程判断（而不是"只要有调用在跑"）：我们自己的请求依旧不会被自己注入 / 接管，
        但同一时间到达的真人消息走的是另一条协程，照常处理。
        """

        task = self._current_task()
        if task is None:
            # 没有正在跑的事件循环（同步收尾之类）：只能按"有没有调用在飞"粗判
            return self._self_initiated_depth > 0
        return task in self._self_initiated_tasks

    def _should_expect_reply(self, event: Any) -> bool:
        """这条消息"看起来会走到回复"吗（用来提前登记连发合并）。

        只有会被回复的消息才登记：群里没点名她的闲聊不登记，
        否则群一直有人说话，她的安静期就永远刷新不完。
        """

        text = str(event.get_message_str() or "")
        if _is_command(text):
            return False
        return bool(
            event.is_wake_up() or _is_at_bot(event) or event.is_private_chat()
        )

    def _reply_lock(self, session_id: str) -> asyncio.Lock:
        # 锁按"她"分（会话组的代表）：同一个她的两个会话不能同时开两条回复管线，
        # 否则两边会轮流改同一份状态，计划和动作互相踩
        key = self.engine.merge_scope(session_id) if self.engine else session_id
        lock = self._reply_locks.get(key)
        if lock is not None:
            return lock
        if len(self._reply_locks) > 128:
            # 会话多起来以后顺手回收没在用的那些，别一直攒着
            for key in [
                key
                for key, value in self._reply_locks.items()
                if not value.locked()
            ]:
                self._reply_locks.pop(key, None)
        lock = asyncio.Lock()
        self._reply_locks[key] = lock
        return lock

    @contextlib.asynccontextmanager
    async def _reply_turn(self, session_id: str):
        """同一会话里，接管按到达顺序排队执行。

        前一条还在等她开口时，后一条先等着——不然两条回复都是照着"回复前"的状态写出来的，
        会互相不知道对方说了什么、重复回应同一件事。等太久（前一条卡住了）就不再等，
        宁可多说一句，也别一直不说话。
        """

        lock = self._reply_lock(session_id)
        acquired = False
        try:
            await asyncio.wait_for(lock.acquire(), timeout=REPLY_TURN_WAIT_SECONDS)
            acquired = True
        except asyncio.TimeoutError:
            if self.debug:
                self.logger.info(
                    f"[virtual_world] 上一条回复还没结束，这条不再排队 session={session_id}"
                )
        try:
            yield
        finally:
            if acquired:
                lock.release()

    # ================= 回复路径（接管 / 注入） =================

    @filter.on_llm_request(priority=LLM_HOOK_PRIORITY)
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """消息走到大模型时触发。

        - ``reply_mode=takeover``（默认）：本插件接管这次回复，自己调模型、按 JSON 动作执行并发送，
          然后 ``stop_event()`` 阻止主人格重复回复；
        - 接管失败（没配模型 / 调用出错 / 模型没给动作）时自动降级为注入模式，用户仍然会收到回复；
        - 如果消息在这之前已经被别的插件处理掉（事件已 stop、或根本没走到大模型），我们什么都不做。
        """

        if not self.enabled:
            return
        if self.in_self_initiated_call():
            # 正常情况下这里只该拦下"我们自己发起的请求"。
            # 真有人 @ 她却被拦在这里，说明那道标记泄漏了——留一条日志，好排查。
            if event.get_sender_id() and event.get_sender_id() != event.get_self_id():
                self.logger.warning(
                    f"[virtual_world] 这条消息被「插件自己在调模型」的标记拦下了"
                    f"（depth={self._self_initiated_depth}）："
                    f"{str(event.get_message_str() or '')[:30]}"
                )
            return
        if event.is_stopped():
            return
        if self.retired:
            return
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return
        self._remember_event(event)
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return

        # `req.prompt` 是管线处理过的用户消息（可能已带上图片描述等），优先用它
        user_text = (getattr(req, "prompt", "") or "").strip() or (
            event.get_message_str() or ""
        )
        sources = await _image_sources(event)
        user_text = await self._annotate_message(
            event, user_text, sources=sources, summarize_forward=True
        )
        image_urls, image_marks, image_notes = await self._reply_images(event, sources)
        if image_notes:
            user_text = f"{user_text}\n［{'；'.join(image_notes)}］".strip()
        ctx = MessageContext(
            session_id=session_id,
            user_id=event.get_sender_id(),
            user_name=event.get_sender_name(),
            text=user_text,
            is_wake=bool(event.is_wake_up()),
            is_mentioned=_is_at_bot(event) or bool(event.is_private_chat()),
            is_soft_wake=_is_soft_wake(event),
            is_private=bool(event.is_private_chat()),
            group_name=_group_name(event),
            persona_id=_event_persona_id(event),
            # 其他插件（上下文理解、图片转文字、记忆…）写进 system_prompt 的内容原样带过去
            other_context=(getattr(req, "system_prompt", "") or "").strip(),
            image_urls=image_urls,
            chat_images=sources,
            image_marks=image_marks,
        )

        # 睡觉时的门禁：没被明确叫醒就只回固定文案（或保持安静），
        # 不调大模型、不执行动作，也不交给主人格替她熬夜聊天。
        sleep_reply = await self.engine.sleep_gate(ctx)
        if sleep_reply is not None:
            echo = self.engine.take_pending_echo(session_id)
            if sleep_reply.messages or echo:
                # 睡觉时的固定文案也是在回这条消息，同样按开关带引用
                await self._send_reply(
                    event, list(sleep_reply.messages) + echo, quote=True
                )
                await self._fire_hook(event, "OnAfterMessageSentEvent")
            if self.debug:
                self.logger.info(
                    f"[virtual_world] 睡觉门禁 {session_id}："
                    f"{sleep_reply.mode}（{sleep_reply.reason}）"
                )
            event.stop_event()
            return

        injection = await self.engine.handle_incoming(ctx)
        if not injection:
            return  # 会话未启用或命中内容安全，本插件完全不干预

        # ---- 接管模式：自己回复，并阻止主人格重复回复 ----
        if (self.engine.world.reply_mode or "takeover") == "takeover":
            # AstrBot 会在「准备调模型」时发一次这个钩子（贴表情的小插件靠它开始处理）。
            # 我们自己调模型，这里补一遍；被拦下就整条消息都不接管。
            if await self._fire_hook(event, "OnWaitingLLMRequestEvent"):
                event.stop_event()
                return
            # 同一条会话同一时间只跑一条接管：连发的第二条会排队，
            # 等前一条说完再开口，这样它能看到对方刚说过什么
            # 排队期间如果前一条还在调模型、而且这条来得够快，会被**并进那一次回复**，
            # 那就轮到自己时直接跳过（不能同一句话答两遍）
            # 「收到消息」那一刻就登记过了（见 on_any_message）：这里只取回来，
            # 拿不到（老事件 / 被别的插件重建过）就补登记一条
            incoming = event.get_extra(VW_INCOMING_EXTRA, None)
            if not isinstance(incoming, dict):
                incoming = self.engine.register_incoming(session_id, ctx.text)
            async with self._reply_turn(session_id):
                if self.engine.was_absorbed(incoming):
                    if self.debug:
                        self.logger.info(
                            f"[virtual_world] 这条被合并进上一次回复了，不单独回 session={session_id}"
                        )
                    event.stop_event()
                    return
                outcome = await self.engine.handle_reply(
                    ctx, history=list(getattr(req, "contexts", None) or [])
                )
                echo = list(getattr(outcome, "debug_messages", []) or [])
                # 这一轮工具 / 指令生成出来的图（生图动作走的就是这里）
                made_images = list(getattr(outcome, "images", []) or [])
                # 她这一轮要说的话：当前会话一份，另外可能还有"只说给别处"的
                routed = {
                    str(key): list(value)
                    for key, value in (getattr(outcome, "routed", {}) or {}).items()
                    if list(value)
                }
                routed_images = {
                    str(key): list(value)
                    for key, value in (getattr(outcome, "routed_images", {}) or {}).items()
                    if list(value)
                }
                # 慢动作之前她已经说的那几句已经当场发出去了（见 _send_live_say）：
                # 这一轮算成立，但只发"还没发出去的部分"
                live_lines = [
                    str(item)
                    for item in (getattr(outcome, "live_messages", []) or [])
                    if str(item).strip()
                ]
                pending_lines = [
                    str(item) for item in (outcome.messages or []) if str(item).strip()
                ]
                if outcome.ok and (pending_lines or live_lines or routed or routed_images):
                    # 先让别的插件的回复钩子过一遍（它们可能在回复里读写标记）
                    # 钩子看的是她这一轮说过的**全部**话（含已经发出去的那几句），
                    # 不然"等我两分钟"之后再接结果时，靠标记工作的插件会少读到前半句
                    heard = live_lines + pending_lines
                    bridged = await self._bridge_reply_hooks(
                        event,
                        heard,
                        str(getattr(outcome, "tail", "") or ""),
                    )
                    if len(bridged) == len(heard):
                        bridged = bridged[len(live_lines) :]
                    else:
                        # 钩子改了条数：位置对不上，就用原样的"还没发的部分"
                        bridged = list(pending_lines)
                    # 再按换行拆段：一段一条消息，别把好几句挤成一坨；
                    # 调试回显按它实际发生的位置插进去（先调工具、再说话，群里也是这个顺序）
                    payload = self._merge_with_echo(bridged, outcome, echo)
                    if payload or made_images:
                        await self._send_reply(
                            event,
                            payload,
                            # 前面已经即时说过话时，这一条是接着往下讲，不再引用触发她的那条
                            quote=not live_lines,
                            quote_mode_hint=str(getattr(outcome, "quote_hint", "") or ""),
                            images=made_images,
                        )
                    # 说给别处的那些：逐条投到那个会话（不带引用——那条消息不在这里）
                    for target, texts in routed.items():
                        try:
                            await self.messenger.send_text(target, list(texts))
                        except Exception as exc:
                            self.logger.debug(
                                f"[virtual_world] 发到 {target} 失败：{exc}"
                            )
                    for target, refs in routed_images.items():
                        try:
                            await self.messenger.send_images(target, list(refs))
                        except Exception as exc:
                            self.logger.debug(
                                f"[virtual_world] 发图到 {target} 失败：{exc}"
                            )
                    # 真发出去了才算"回过这批消息"（水位线在这里推进）
                    try:
                        await self.engine.mark_chat_replied_by_session(session_id)
                        # 别处那几句也一样：她在那边也算回应过了
                        for target in list(routed) + list(routed_images):
                            await self.engine.mark_chat_replied_by_session(target)
                    except Exception as exc:
                        self.logger.debug(f"[virtual_world] 推进水位线失败：{exc}")
                    # 「发完了」也要补一遍：靠它摘掉"处理中"标记的插件才不会一直挂着
                    await self._fire_hook(event, "OnAfterMessageSentEvent")
                    if self.debug:
                        self.logger.info(
                            f"[virtual_world] 接管回复 {session_id}："
                            f"reasoning={outcome.reasoning} "
                            f"messages={live_lines + pending_lines}"
                        )
                    event.stop_event()
                    return
                if str(getattr(outcome, "error", "")) == REPLY_INTERRUPTED:
                    # 这一轮被新消息打断：作废，不补一句也不交给主人格——
                    # 新那条自己的回复管线马上会接手（它能看到刚才这几条）。
                    if echo:
                        await self._send_reply(event, echo)
                    event.stop_event()
                    return
                if str(getattr(outcome, "error", "")) == REPLY_MUTED:
                    # 本小时被动回复触顶：接管但静默（连调试回显也不发）
                    event.stop_event()
                    return
                # 接管没成功：**这一条就不回了**（不交回主人格——那是另一个人设，
                # 顶下来的话用户会看到"她"突然换了个说法）。调试回显照样发，
                # 原因写进日志页，方便排查到底是模型挂了还是没给出动作。
                if echo or made_images:
                    await self._send_reply(event, echo, images=made_images)
                    await self._fire_hook(event, "OnAfterMessageSentEvent")
                await self._note_takeover_fallback(session_id, outcome)
                event.stop_event()
                return

        # ---- 注入模式：只把世界认知写进主人格的提示词，不接管回复 ----
        try:
            req.system_prompt = (req.system_prompt or "") + injection
        except Exception as exc:
            self.logger.warning(f"[virtual_world] 注入提示词失败：{exc}")
        # 注入模式下主人格会替她说话，同样算"已经回应过这批群聊"
        try:
            await self.engine.mark_chat_replied_by_session(session_id)
        except Exception:
            pass
        # 注入模式不会自己发消息，但门禁（被叫醒等）攒下的回显要补上
        echo = self.engine.take_pending_echo(session_id)
        if echo:
            await self._send_reply(event, echo)
        if self.debug:
            self.logger.info(f"[virtual_world] 注入 {session_id}：\n{injection}")
        await self._trim_tools(session_id, req)

    async def _trim_tools(self, session_id: str, req) -> None:
        """按当前区域裁剪 req.func_tool。"""

        if not self.engine.world.tool_filter_enabled:
            return
        tool_set = getattr(req, "func_tool", None)
        if tool_set is None or not getattr(tool_set, "tools", None):
            return
        try:
            state = await self.engine.load_state(session_id, cold_start=False)
            node_id = state.node_id or self.engine.default_node_id()
        except Exception:
            node_id = self.engine.default_node_id()
        allowed = self.engine.allowed_tool_names(node_id)
        kept = [tool for tool in tool_set.tools if getattr(tool, "name", "") in allowed]
        if len(kept) == len(tool_set.tools):
            return
        tool_set.tools = kept

    async def _send_debug_live(self, session_id: str, message: str) -> bool:
        """把调试行立刻发到群里（引擎那边一发生就调它）。"""

        try:
            return bool(await self.messenger.send_text(session_id, [str(message)]))
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 调试回显发送失败：{exc}")
            return False

    async def _send_live_say(self, session_id: str, message: str) -> bool:
        """把她说出口的这一句立刻发出去（引擎在慢动作开始前调它）。

        检索、生图、录视频这类要花一会儿的动作排在后面时，「坐好等我两分钟～」先到群里，
        群里才是"她说了一句 → 过一会儿带着结果回来"，而不是半天不出声、然后一口气说三句。
        发失败返回 False：引擎会把这几句留到收尾按原顺序一起发，不会丢内容。
        """

        body = str(message or "").strip()
        if not body:
            return False
        # 连着的两句之间按字数停一下：和收尾发送时的节奏保持一致，
        # 免得"先说的这一句"排成一串同一瞬间冒出来
        previous = self._live_say_tail.get(session_id, "")
        if previous:
            delay = self.typing_delay_for(previous)
            if delay > 0:
                await asyncio.sleep(delay)
        try:
            sent = bool(await self.messenger.send_text(session_id, [body]))
        except Exception as exc:
            self.logger.debug(f"[virtual_world] 即时发言发送失败：{exc}")
            sent = False
        self._live_say_tail[session_id] = body if sent else ""
        return sent

    async def _send_reply(
        self,
        event: AstrMessageEvent,
        messages: list[str],
        *,
        quote: bool = False,
        quote_mode_hint: str = "",
        images: list[str] | None = None,
    ) -> None:
        """把接管产生的消息发回当前会话（逐条发送 = 天然分段回复）。

        分段之间按上一条的字数停一下：群里看起来就像她在一条条打字，
        而不是一整坨同时冒出来。停顿有上限，长句子不会等到天荒地老。
        ``quote=True`` 时第一条引用触发她的那条消息（能不能引用见 ``_quote_target``）。
        ``images`` 是这一轮工具 / 指令生成出来的图：说完再贴上去（一次一条消息，多张放一起）。
        """

        session_id = event.unified_msg_origin
        pending = [str(item).strip() for item in messages if str(item).strip()]
        refs = _sendable_images(images)
        if not pending and not refs:
            return
        quote_id = self._quote_target(event, quote_mode_hint) if quote else ""
        for index, content in enumerate(pending):
            if self.messenger.blocked(session_id):
                # 刚发送失败过：不再往下试，免得把队列越堆越长
                break
            chain: list[Any] = [Plain(text=content)]
            if quote_id and index == 0:
                # 引用段必须排在前面，平台才认得出"这条是在回哪一句"
                chain.insert(0, Reply(id=quote_id))
            try:
                await event.send(MessageChain(chain))
                self.messenger.mark_sent(session_id)
            except Exception as exc:
                # 失败就失败：以前这里会立刻换一条通道再发一次，遇到"超时但其实发出去了"
                # 的情况就会重复刷屏，所以只保留一次尝试。
                self.messenger.mark_failed(session_id, exc)
                continue
            if index == len(pending) - 1:
                break  # 最后一条不用再等
            delay = self.typing_delay_for(content)
            if delay > 0:
                await asyncio.sleep(delay)

        if refs and not self.messenger.blocked(session_id):
            # 只在"她什么都没说、只发了图"时，让图片这条去引用（第一条才引用）
            await self._send_images(
                event,
                refs,
                quote=quote and not pending,
                quote_mode_hint=quote_mode_hint,
            )

    async def _send_images(
        self,
        event: AstrMessageEvent,
        images: list[str],
        *,
        quote: bool = False,
        quote_mode_hint: str = "",
    ) -> bool:
        """把结果图贴到群里（多张放同一条消息，不会拆成连发刷屏）。"""

        refs = _sendable_images(images)
        if not refs:
            return False
        session_id = event.unified_msg_origin
        if self.messenger.blocked(session_id):
            return False
        chain: list[Any] = [Image(file=ref) for ref in refs]
        quote_id = self._quote_target(event, quote_mode_hint) if quote else ""
        if quote_id:
            chain.insert(0, Reply(id=quote_id))
        try:
            await event.send(MessageChain(chain))
            self.messenger.mark_sent(session_id)
            return True
        except Exception as exc:
            # 和文字一样：失败就失败，不换通道重发（重发容易变成两条）
            self.messenger.mark_failed(session_id, exc)
            return False

    def _quote_target(self, event: AstrMessageEvent, mode_hint: str = "") -> str:
        """这条回复要引用哪条消息：触发她的那一条（用不了就返回空串）。

        档位在「说话节奏」里：关闭 / 总是 / 智能（默认）。智能档由引擎判断——
        "这一轮回的是一串消息"才引用，单独一句对答不顶引用；``mode_hint`` 就是那个判断。
        引用是 QQ / OneBot（aiocqhttp）这类平台才有的段，别的平台不认，
        硬塞过去只会让这条发失败——所以这里先按平台名挡一道。
        """

        style = getattr(self.engine.world, "reply_style", None)
        mode = str(getattr(style, "quote_mode", "smart") or "smart")
        if mode not in ("always", "smart"):
            return ""
        # 智能档由调用方判断（"这一轮回的是一串消息"才引用），这里只认 always
        if mode == "smart" and mode_hint != "always":
            return ""
        platform = ""
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            try:
                platform = str(getter() or "").lower()
            except Exception:
                platform = ""
        if "cqhttp" not in platform and "onebot" not in platform:
            return ""
        message_obj = getattr(event, "message_obj", None)
        return str(getattr(message_obj, "message_id", "") or "").strip()

    def typing_delay_for(self, text: str) -> float:
        """按字数算这一次的「打字」停顿（秒）。0 表示不等待。"""

        style = getattr(self.engine.world, "reply_style", None)
        if style is None or not bool(getattr(style, "typing_delay_enabled", True)):
            return 0.0
        per_char = max(0.0, float(getattr(style, "typing_delay_per_char", 0.0) or 0.0))
        cap = max(0.0, float(getattr(style, "typing_delay_max", 0.0) or 0.0))
        if per_char <= 0 or cap <= 0:
            return 0.0
        return min(cap, len(str(text)) * per_char)

    @staticmethod
    def split_messages(messages: list[str], *, limit: int = 8) -> list[str]:
        """把每条消息按换行拆开：一段一条。

        模型经常把几句话、甚至「话 + 动作文案」写在同一个字符串里，
        整段发出去在群里就是一坨；拆开之后读起来才像人一句一句说的。
        """

        result: list[str] = []
        for item in messages:
            for line in str(item or "").splitlines():
                text = line.strip()
                if text:
                    result.append(text)
        return result[:limit] if result else [str(item).strip() for item in messages if str(item).strip()]

    def _merge_with_echo(
        self, bridged: list[str], outcome, echo: list[str]
    ) -> list[str]:
        """把正式回复和调试回显按"实际发生的先后"合成一串消息。

        她这一轮可能是"先调工具、拿到结果才开口"：那种情况下调试行本来就该排在
        她的话前面。没有回显时就是普通的分段回复。
        """

        if not echo:
            return self.split_messages(bridged)
        raw = list(getattr(outcome, "messages", []) or [])
        if len(bridged) != len(raw):
            # 钩子改过条数，位置对不上了：宁可把回显放在后面，也不要错位
            return self.split_messages(bridged) + list(echo)
        positions = list(getattr(outcome, "debug_positions", []) or [])
        buckets: dict[int, list[str]] = {}
        for index, line in enumerate(echo):
            slot = positions[index] if index < len(positions) else len(raw)
            buckets.setdefault(min(int(slot), len(raw)), []).append(line)
        result: list[str] = []
        for slot in range(len(raw) + 1):
            result.extend(buckets.get(slot, []))
            if slot < len(raw):
                result.extend(self.split_messages([raw[slot]]))
        return result

    async def _bridge_reply_hooks(
        self, event: AstrMessageEvent, messages: list[str], tail: str = ""
    ) -> list[str]:
        """让别的插件也能"看到"这次接管的回复。

        本插件接管时是自己调模型、自己发送的，AstrBot 的回复钩子轮不到跑，
        那些靠在回复里解析标记的插件（好感度、统计、改写语气之类）就永远收不到内容。
        这里把回复补送进同一条钩子链：先过 ``on_llm_response``，
        再过 ``on_decorating_result``（发送前的最后一道），拿回它们改过的文本再发。

        ``tail`` 是模型写在 JSON 之后的那一小截（例如 `[好感度 持平]`）：只跟这次钩子调用走，
        给靠标记工作的插件解析用，**不会**跟着她的话发到群里。
        """

        if not self.reply_hook_bridge or not messages:
            return messages
        try:
            from astrbot.core.pipeline.context_utils import call_event_hook
            from astrbot.core.provider.entities import LLMResponse
            from astrbot.core.star.star_handler import EventType
        except Exception:
            return messages

        joined = "\n".join(messages)
        # 标记只加在"给钩子看的文本"里：插件解析完（通常会把标记抹掉）再返回，
        # 真正发到群里的 messages 不会带上它。
        hook_text = f"{joined}\n{tail}".strip() if tail else joined
        try:
            response = self._hook_response(event, hook_text, LLMResponse)
            await call_event_hook(event, EventType.OnLLMResponseEvent, response)
            edited = str(getattr(response, "completion_text", "") or "")
            if edited and edited != hook_text:
                lines = [line.strip() for line in edited.splitlines() if line.strip()]
                # 插件可能把标记那一行抹掉了：只用它给的内容，标记本身不发
                cleaned = [line for line in (lines or [edited]) if line != tail.strip()]
                messages = cleaned or lines or [edited]
        except Exception as exc:
            self.logger.warning(f"[virtual_world] 回复钩子（LLM 响应）执行出错：{exc}")

        try:
            from astrbot.core.message.message_event_result import (
                MessageEventResult,
                ResultContentType,
            )

            previous = event.get_result()
            result = MessageEventResult().message("\n".join(messages))
            result.set_result_content_type(ResultContentType.LLM_RESULT)
            event.set_result(result)
            await call_event_hook(event, EventType.OnDecoratingResultEvent)
            current = event.get_result()
            joined_result = "\n".join(messages)
            texts = (
                [
                    str(component.text)
                    for component in list(getattr(current, "chain", []) or [])
                    if isinstance(component, Plain) and str(component.text).strip()
                ]
                if current is not None
                else []
            )
            # 没有插件改动内容时，保持原来的分段：合并成一条会把
            # 「say 的话」和「（…抱了你一下）」这类动作文案挤进同一条消息里。
            if texts and (len(texts) > 1 or texts[0] != joined_result):
                messages = texts
            if previous is None:
                event.clear_result()
            else:
                event.set_result(previous)
        except Exception as exc:
            self.logger.warning(f"[virtual_world] 回复钩子（发送前）执行出错：{exc}")
        return messages

    def _hook_response(self, event: AstrMessageEvent, text: str, response_cls: Any) -> Any:
        """给回复钩子准备一个「像真的」的模型响应。

        别的插件可能读 ``usage`` / ``id`` / ``role`` 这些字段，自己拼一个空的响应它们
        会直接抛异常（异常被宿主吞掉，表现就是"收不到钩子"）。所以优先拿主模型
        真实响应的浅拷贝，只把文本换成最终要说的话。
        """

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        raw = self.take_llm_response(session_id) if session_id else None
        if raw is not None:
            try:
                response = copy.copy(raw)
                response.completion_text = text
                if hasattr(response, "reasoning_content"):
                    response.reasoning_content = ""
                return response
            except Exception:
                pass
        return response_cls(role="assistant", completion_text=text)

    # ---------------- 把接管的流程补进 AstrBot 的钩子链 ----------------

    @staticmethod
    def _hook_type(name: str) -> Any:
        """按名字取 AstrBot 的钩子类型（老版本没有的钩子就跳过）。"""

        try:
            from astrbot.core.star.star_handler import EventType
        except Exception:
            return None
        return getattr(EventType, name, None)

    async def _fire_hook(self, event: Any, name: str, *args: Any) -> bool:
        """补发一个 AstrBot 钩子；返回事件是否被别的插件拦下。

        本插件接管时是自己调模型、自己调工具、自己发消息的，AstrBot 管线里那些
        「等 LLM」「用完工具」「发完消息」的钩子轮不到跑——靠这些钩子工作的小插件
        （比如处理中贴表情、用完摘掉）就会漏掉一半。这里在同样的时机补一遍。
        """

        if not self.reply_hook_bridge or event is None:
            return False
        hook_type = self._hook_type(name)
        if hook_type is None:
            return False
        try:
            from astrbot.core.pipeline.context_utils import call_event_hook
        except Exception:
            return False
        try:
            return bool(await call_event_hook(event, hook_type, *args))
        except Exception as exc:
            self.logger.warning(f"[virtual_world] 钩子 {name} 执行出错：{exc}")
            return False

    async def _note_takeover_fallback(self, session_id: str, outcome) -> None:
        """接管没生效：这一条她不说话，但**必须留下痕迹**。

        以前只在开着调试时打一行 stdout，日志页里什么都没有——用户只看到
        "她没回"，完全猜不到是模型挂了、还是模型没给出能执行的动作。
        """

        reason = str(getattr(outcome, "error", "") or "没有对外输出")
        warnings = [str(item) for item in (getattr(outcome, "warnings", None) or [])]
        self.logger.warning(
            f"[virtual_world] 接管没生效（{reason}），这一条保持安静 "
            f"session={session_id}"
        )
        detail = {"reason": reason}
        if warnings:
            detail["warnings"] = warnings[:4]
        if getattr(outcome, "tail", ""):
            detail["model_tail"] = str(outcome.tail)[:200]
        try:
            async with self.engine.session_state(session_id) as state:
                await self.engine._log_event(state, "takeover_failed", detail)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[virtual_world] 写接管失败日志失败：{exc}")

    # ================= 旁观记录（不产生 LLM 调用） =================

    @filter.event_message_type(
        filter.EventMessageType.ALL, priority=SLEEP_GUARD_PRIORITY
    )
    async def on_sleep_guard(self, event: AstrMessageEvent) -> None:
        """睡觉门禁：挡在其它插件（含意图路由）之前。

        优先级的取值比意图路由的 100 更高，所以 ``stop_event()`` 之后它不会被执行——
        既不浪费它那次判断，也不会把睡着的她拖进对话。
        """

        if not self.enabled:
            return
        if self.in_self_initiated_call():
            return
        if self.retired:
            return
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        text = event.get_message_str() or ""
        if _is_command(text):
            return  # 指令照常走（/vw status、/vw 叫醒 这些要能用）
        sources = await _image_sources(event)
        if _is_forwarded(event) and self._should_expect_reply(event):
            # 这条转发是冲她来的：先把内容压成摘要，她醒来看到的才是"事情"而不是占位符
            summary = await self._forward_summary_text(event)
            if summary:
                text = _with_forward_summary(text, summary)
        if _image_components(event):
            # 她在睡觉：这里不额外调转述模型（醒来后那条消息会照常转述），
            # 但留档里要能看出"有人发了张图"，否则这条消息会是空的。
            text = f"{text}\n［对方发了一张图片］".strip()
        ctx = MessageContext(
            session_id=session_id,
            user_id=event.get_sender_id(),
            user_name=event.get_sender_name(),
            text=text,
            is_wake=bool(event.is_wake_up()),
            is_mentioned=_is_at_bot(event) or bool(event.is_private_chat()),
            is_private=bool(event.is_private_chat()),
            # 记下这条消息带的图片，等下次回复时一起给多模态主模型
            image_urls=list(sources),
            chat_images=list(sources),
        )
        if not await self.engine.should_block_sleep(ctx):
            return
        # 留档已经由门禁自己记好了（她醒来还得知道群里发生过什么），这里只负责截住事件
        if self.debug:
            self.logger.info(
                f"[virtual_world] 她在睡觉，挡下这条消息 session={session_id}：{text[:30]}"
            )
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        if not self.enabled or self.retired:
            return
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        # 「连发合并」要在这里就登记：落到 on_llm_request 那一钩时，同一会话的
        # 上一条回复可能还占着管线（AstrBot 里那条管线是串行的），等她登记时
        # 早就过了安静期，两条消息就各回一次了。
        if self._should_expect_reply(event):
            try:
                event.set_extra(
                    VW_INCOMING_EXTRA,
                    self.engine.register_incoming(session_id, event.get_message_str() or ""),
                )
            except Exception as exc:
                self.logger.debug(f"[virtual_world] 登记待回消息失败：{exc}")
        self._remember_event(event)
        sources = await _image_sources(event)
        text = await self._annotate_message(
            event,
            event.get_message_str() or "",
            sources=sources,
            summarize_forward=self._should_expect_reply(event),
        )
        await self.engine.note_presence(
            MessageContext(
                session_id=session_id,
                user_id=event.get_sender_id(),
                user_name=event.get_sender_name(),
                text=text,
                is_wake=bool(event.is_wake_up()),
                is_private=bool(event.is_private_chat()),
                group_name=_group_name(event),
                chat_images=list(sources),
            )
        )

    # ================= 用户 / 管理员命令 =================

    @filter.command("vw", alias={"世界", "virtualworld"})
    async def cmd_vw(self, event: AstrMessageEvent):
        if self.retired:
            return
        args = _command_args(event)
        action = args[0].lower() if args else "help"
        rest = args[1:]

        if action in ("help", "帮助", "?"):
            yield event.plain_result(_help_text(self._pronoun()))
            return

        if action in ("status", "状态", "where", "在哪"):
            session_id = event.unified_msg_origin
            if not self.engine.is_enabled(session_id):
                yield event.plain_result("这个会话还没有启用虚拟世界。用 /vw session add 启用。")
                return
            snapshot = await self.engine.snapshot(session_id)
            yield event.plain_result(_render_status(snapshot, self._pronoun()))
            return

        if action in ("recall", "回忆"):
            if not rest:
                yield event.plain_result("用法：/vw recall <话题>")
                return
            session_id = event.unified_msg_origin
            topic = " ".join(rest)
            memories = self.engine.memory.recall(
                session_id=session_id,
                persona_id=_event_persona_id(event),
                node_id=(await self.engine.load_state(session_id, cold_start=False)).node_id,
                limit=5,
            )
            hits = [
                item
                for item in self.engine.memory.db.query_memories(session_id=session_id, limit=300)
                if topic in str(item.get("content", ""))
            ]
            lines = [f"关于「{topic}」我记得："]
            lines += [f"· {item['content']}" for item in hits[:5]]
            if not hits:
                lines.append("· 好像没什么印象……")
            if memories:
                lines.append("此刻想起的：")
                lines += [f"· {item.content}" for item in memories[:3]]
            yield event.plain_result("\n".join(lines))
            return

        if action in ("memory", "记忆"):
            session_id = event.unified_msg_origin
            user_id = event.get_sender_id()
            rows = self.engine.memory.export_user(
                session_id=session_id, persona_id=_event_persona_id(event), user_id=user_id
            )
            if not rows:
                yield event.plain_result("我暂时没有关于你的场景记忆。")
                return
            lines = [f"我记得关于你的 {len(rows)} 件事："]
            lines += [f"· {item['content']}" for item in rows[:10]]
            lines.append("想让我忘掉，可以发 /vw forget me")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("forget", "忘记"):
            session_id = event.unified_msg_origin
            user_id = event.get_sender_id()
            topic = " ".join(rest) if rest and rest[0].lower() not in ("me", "我") else ""
            removed = self.engine.memory.forget_user(
                session_id=session_id,
                persona_id=_event_persona_id(event),
                user_id=user_id,
                topic=topic,
            )
            yield event.plain_result(f"好，我忘记了 {removed} 条相关的记忆。")
            return

        if action in ("schedule", "日程"):
            if rest and rest[0].lower() in ("run", "执行", "立即"):
                if not self._can_admin(event):
                    yield event.plain_result("只有管理员可以让日程立刻执行。")
                    return
                if len(rest) < 2:
                    yield event.plain_result("用法：/vw schedule run <日程 id>")
                    return
                result = await self.engine.run_schedule_now(
                    event.unified_msg_origin, rest[1]
                )
                yield event.plain_result(
                    result.get("note") or result.get("reason") or "已执行"
                )
                return
            lines = ["当前日程："]
            for schedule in (self.engine.schedules.schedules if self.engine.schedules else []):
                mark = "✅" if schedule.enabled else "⛔"
                lines.append(
                    f"{mark} {schedule.time} {schedule.id} → "
                    f"{'/'.join(str(getattr(step, 'type', '')) for step in schedule.action_chain)}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("map", "地图"):
            world = self.engine.world
            lines = ["虚拟世界地图（按区域）："]
            for zone in world.zones:
                here = world.nodes_in_zone(zone.id)
                lines.append(f"【{zone.name}】{len(here)} 个地点")
                for node in here:
                    names = [
                        action.name or action.id
                        for action in world.actions_in(node.id)
                        if action.id != "walk_to"
                    ]
                    lines.append(
                        f"· {node.name}（{node.id}）：{'/'.join(names) or '只有通用动作'}"
                    )
            lines.append("区域内连线：")
            for edge in world.edges:
                lines.append(f"· {edge.from_} ↔ {edge.to}（{edge.ticks} tick）")
            if world.zone_edges:
                lines.append("跨区连线：")
                for edge in world.zone_edges:
                    lines.append(
                        f"· {node_label(world, edge.from_node)} → {node_label(world, edge.to_node)}"
                        f"（{edge.ticks} tick）"
                    )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("nickname", "名片"):
            yield event.plain_result(await self._handle_nickname_command(event, rest))
            return

        if action in ("debug", "调试"):
            yield event.plain_result(await self._handle_debug_command(event, rest))
            return

        if action in ("session", "会话"):
            yield event.plain_result(await self._handle_session_command(event, rest))
            return

        if action in ("reload", "重载"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员可以重载配置。")
                return
            warnings = self.engine.reload_config()
            message = "配置已热加载。"
            if warnings:
                message += "\n提醒：" + "；".join(warnings)
            yield event.plain_result(message)
            return

        if action in ("reset", "重置"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员可以重置会话状态。")
                return
            target = rest[0] if rest else event.unified_msg_origin
            await self.db.call("delete_state", target)
            yield event.plain_result(f"已重置 {target} 的世界状态（记忆保留）。")
            return

        if action in ("restore-default", "恢复默认"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员可以恢复默认配置。")
                return
            restored = self.store.restore_default("all")
            self.engine.reload_config()
            yield event.plain_result("已恢复默认配置：" + "、".join(restored))
            return

        if action in ("event", "事件"):
            # 成功时**不回话**：她自己会在群里说这件事，指令再回一句就是噪音
            text = await self._cmd_event(event, rest)
            if text:
                yield event.plain_result(text)
            return

        if action in ("ability", "能力", "能力值"):
            # 能力值不公开：只有管理员能看（群友要看只能翻日志页）
            if not self._can_admin(event):
                yield event.plain_result("这项不公开，只有管理员可以看。")
                return
            yield event.plain_result(await self._cmd_ability(event))
            return

        if action in ("thread", "线索", "未了"):
            if not self._can_admin(event):
                yield event.plain_result("这项只有管理员可以看。")
                return
            yield event.plain_result(await self._cmd_thread(event))
            return

        yield event.plain_result(f"未知指令：{action}\n\n{_help_text(self._pronoun())}")

    async def _cmd_event(self, event: AstrMessageEvent, rest: list[str]) -> str:
        """`/vw event [要发生的事]`：用户直接给她安排一件事。"""

        if not self._can_deliver_event(event):
            return "只有管理员可以投递事件（可以在全局设置里改成所有人）。"
        session_id = event.unified_msg_origin
        sub = (rest[0] if rest else "").lower()
        if sub in ("list", "列表"):
            overview = await self.engine.event_overview(session_id)
            lines = ["最近的事件线索："]
            rows = [item for item in (overview.get("threads") or []) if item.get("steps")]
            if not rows:
                lines.append("· 还没有发生过什么")
            for item in rows:
                mark = "进行中" if str(item.get("status")) == "open" else "已结束"
                lines.append(f"· [{mark}] {item.get('title') or '一件事'}")
                for step in list(item.get("steps") or [])[:4]:
                    lines.append(f"   - {step}")
            return "\n".join(lines)
        if sub in ("help", "用法", "?"):
            return (
                "/vw event <一句话>\n"
                "　给她安排一件「会发生的事」，她会自己决定怎么处理。\n"
                "　例子：/vw event 出门忘了带伞\n"
                "　/vw event list  看最近的事件线索"
            )
        text = " ".join(rest)
        note = await self.engine.submit_event(session_id, text)
        # 成功（note 为空）时不回话：她马上就会在群里说这件事
        return note

    def _can_deliver_event(self, event: AstrMessageEvent) -> bool:
        """谁能用 `/vw event` 投递事件（默认只有管理员）。"""

        if self._can_admin(event):
            return True
        return str(getattr(self.engine.world.events, "event_actor", "admin") or "admin") == "all"

    async def _cmd_ability(self, event: AstrMessageEvent) -> str:
        overview = await self.engine.event_overview(event.unified_msg_origin)
        rows = overview.get("abilities") or {}
        if not rows:
            return "能力值没有启用。"
        detail = "、".join(
            f"{item.get('label')}{item.get('hint')}" for item in rows.values()
        )
        spent = overview.get("ability_spent_today") or {}
        if spent:
            detail += "\n今天已经变化：" + "、".join(
                f"{key} {value:+.3f}" for key, value in spent.items()
            )
        return f"她现在的本事：{detail}"

    async def _cmd_thread(self, event: AstrMessageEvent) -> str:
        overview = await self.engine.event_overview(event.unified_msg_origin)
        pending = overview.get("pending_help") or {}
        lines: list[str] = []
        if str(pending.get("state") or "") == "active":
            lines.append(f"她正在等群友拿主意：{pending.get('title') or '一件事'}")
        elif str(pending.get("state") or "") == "idle":
            lines.append(f"这件事还没解决，她没再提：{pending.get('title') or '一件事'}")
        rows = [item for item in (overview.get("threads") or []) if str(item.get("status")) == "open"]
        if not rows and not lines:
            return "她手头没有没完的事。"
        for item in rows:
            lines.append(f"· {item.get('title') or '一件事'}")
            for step in list(item.get("steps") or [])[:5]:
                lines.append(f"   - {step}")
            if item.get("pending_followup"):
                lines.append(f"   → 还有下一步：{item.get('pending_followup')}")
        return "\n".join(lines)

    # ---------------- 给主人格 / 别的插件用的日程工具 ----------------

    @filter.llm_tool(name="vw_schedule_list")
    async def tool_schedule_list(self, event: AstrMessageEvent) -> str:
        """查看虚拟世界里 Bot 自己的日程表（什么时候会自动做什么）。

        没有参数，直接调用。
        """

        return self.engine.schedule_text()

    @filter.llm_tool(name="vw_schedule_add")
    async def tool_schedule_add(
        self,
        event: AstrMessageEvent,
        time: str,
        actions: str,
        days: str = "",
    ) -> str:
        """给虚拟世界里的 Bot 加一条日程：到点她会自动做一串动作。

        Args:
            time(string): 触发时间，24 小时制，例如 07:30
            actions(string): 动作链，用 > 表示先后顺序，例如 say>walk_to>search_web；需要先去某地时写成 walk_to@书房 id
            days(string): 星期，用逗号分隔，可选 mon,tue,wed,thu,fri,sat,sun；留空表示每天
        """

        chain: list[dict[str, Any]] = []
        for raw in str(actions or "").replace("，", ",").split(">"):
            part = raw.strip()
            if not part:
                continue
            node = ""
            if "@" in part:
                part, node = (piece.strip() for piece in part.split("@", 1))
            step: dict[str, Any] = {"type": part}
            if node:
                step["target_node"] = node
            chain.append(step)
        ok, note = await self.engine.schedule_add(
            {
                "time": time,
                "days": [day.strip() for day in str(days or "").replace("，", ",").split(",") if day.strip()],
                "action_chain": chain,
                "auto_travel": True,
            }
        )
        return note

    @filter.llm_tool(name="vw_schedule_remove")
    async def tool_schedule_remove(
        self,
        event: AstrMessageEvent,
        schedule_id: str = "",
        time: str = "",
        keyword: str = "",
    ) -> str:
        """删掉虚拟世界里 Bot 自己加的一条日程（用户手动配的日程删不掉）。

        Args:
            schedule_id(string): 日程 id，最准；不知道就留空
            time(string): 按时间匹配，例如 07:30
            keyword(string): 按里面出现的动作名匹配，例如 新闻
        """

        ok, note = await self.engine.schedule_remove(
            {"id": schedule_id, "time": time, "keyword": keyword}
        )
        return note

    async def _handle_nickname_command(self, event: AstrMessageEvent, rest: list[str]) -> str:
        sub = rest[0].lower() if rest else ""
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return "这个会话还没有启用虚拟世界。"
        if not self._can_admin(event):
            return "只有管理员可以控制群名片。"
        text = " ".join(rest[1:]).strip()
        async with self.engine.session_state(session_id) as state:
            if sub in ("", "status", "状态"):
                node = self.engine.node(state.node_id)
                desired = compute_nickname(
                    self.engine.world, state, node, base=state.bot_base_nickname
                )
                lines = [
                    f"原名（base）：{state.bot_base_nickname or '（空，还没抓到）'}",
                    f"当前名片：{state.bot_current_nickname or '（未知）'}",
                    f"按当前状态应该显示：{desired or '（算不出来）'}",
                    f"状态：{state.state}　地点：{state.node_id}　改名冷却：{int(self.engine.world.nickname_sync.cooldown_seconds)} 秒",
                    f"锁定：{'是' if state.bot_nickname_locked else '否'}　"
                    f"按地点/状态改名片：{'开' if self.engine.world.nickname_sync.enabled else '关'}",
                ]
                if not desired:
                    lines.append(
                        "⚠ 算不出名片：去「全局设置 → Bot 名称」填一个名字，"
                        "或者在群名片同步里给当前状态/地点配一条文案。"
                    )
                return "\n".join(lines)
            if sub in ("lock", "锁定"):
                state.bot_nickname_locked = True
                return "已锁定群名片，不再自动修改。"
            if sub in ("unlock", "解锁"):
                state.bot_nickname_locked = False
                return "已解锁群名片，会随状态自动变化。"
            if sub in ("set", "设置"):
                if not text:
                    return "用法：/vw nickname set <文本>"
                state.bot_current_nickname = text
                state.bot_base_nickname = text
                state.bot_nickname_locked = True
                card = await self.engine.messenger.set_group_card(session_id, text)
                if not card.ok:
                    return f"已记录为「{text}」并锁定，但改名片失败：{card.reason}"
                return f"已把群名片设为「{text}」并锁定。"
            if sub in ("reset", "重置"):
                state.bot_nickname_locked = False
                state.bot_current_nickname = state.bot_base_nickname
                if state.bot_base_nickname:
                    card = await self.engine.messenger.set_group_card(
                        session_id, state.bot_base_nickname
                    )
                    if not card.ok:
                        return f"已解锁，但恢复原名失败：{card.reason}"
                return "已恢复原名并解锁。"
        return "用法：/vw nickname status|lock|unlock|set <文本>|reset"

    async def _handle_debug_command(self, event: AstrMessageEvent, rest: list[str]) -> str:
        if not self._can_admin(event):
            return "只有管理员可以使用调试命令。"
        sub = rest[0].lower() if rest else "state"
        session_id = rest[1] if len(rest) > 1 else event.unified_msg_origin
        if sub in ("on", "开"):
            self.debug = True
            self.engine.debug = True
            return "调试模式已开启（注入内容与动作校验会写入日志）。"
        if sub in ("off", "关"):
            self.debug = False
            self.engine.debug = False
            return "调试模式已关闭。"
        if sub in ("state", "状态"):
            snapshot = await self.engine.snapshot(session_id)
            return json.dumps(snapshot, ensure_ascii=False, indent=2)
        if sub in ("plan", "计划"):
            state = await self.engine.load_state(session_id, cold_start=False)
            from .core import planner as planner_module

            return planner_module.describe(state)
        if sub in ("stop", "停", "停下", "打断"):
            # 不依赖大模型的兜底：立刻停手 + 放弃剩下的安排
            stopped = await self.engine.interrupt(session_id, force=True)
            cleared = await self.engine.clear_plan(session_id)
            parts = []
            parts.append("停掉了手头的动作" if stopped else "她本来就没在做事")
            if cleared:
                parts.append("放弃了还没做的安排")
            return "，".join(parts) + "。"
        if sub in ("memories", "记忆"):
            rows = self.engine.memory.db.query_memories(session_id=session_id, limit=20)
            if not rows:
                return "（没有记忆）"
            return "\n".join(
                f"[{row['id']}] {row['scope']}/{row['node_id']} "
                f"w={row['weight']:.2f} {row['content']}"
                for row in rows
            )
        if sub in ("session", "会话"):
            ids = self.engine.enabled_session_ids()
            return "启用中的会话：\n" + "\n".join(f"· {item}" for item in ids)
        if sub in ("tools", "工具"):
            available = self.engine.available_tools()
            state = await self.engine.load_state(session_id, cold_start=False)
            allowed = self.engine.allowed_tool_names(state.node_id)
            return (
                f"当前位置 {state.node_id} 能用：{sorted(allowed)}"
                f"（= 通用工具 + 这里的动作绑定的工具）\n"
                f"AstrBot 已注册工具：{sorted(available)}"
            )
        if sub in ("prompt", "提示词"):
            return await self._prompt_report(session_id, "inject")
        if sub in ("autoprompt", "自主提示词"):
            return await self._prompt_report(session_id, "autonomous")
        if sub in ("tick", "推进"):
            outcomes = await self.engine.tick()
            return "\n".join(
                f"{item.session_id}: {'; '.join(item.notes)} {item.messages}"
                for item in outcomes
            ) or "（无输出）"
        if sub in ("decide", "决策"):
            outcome = await self.engine.maybe_decide(session_id)
            if outcome is None:
                return "决策器跳过（未到间隔 / 正在忙）。"
            return f"{'; '.join(outcome.notes)} {outcome.messages}"
        return (
            "用法：/vw debug on|off|state|plan|stop|memories|session|tools|prompt|autoprompt|tick|decide"
        )

    async def _prompt_report(self, session_id: str, mode: str) -> str:
        """把提示词写成文件，并回一份「分段索引」。

        提示词三四千字，直接丢进聊天窗口会被平台截掉尾巴（正好是群聊上下文那几段）。
        这里只发索引 + 文件路径，原文让用户去编辑器或文件里看。
        """

        if mode == "autonomous":
            text = await self.engine.preview_autonomous_prompt(session_id)
            name = "prompt_autonomous.txt"
            label = "自主提示词"
        else:
            text = await self.engine.preview_injection(session_id)
            name = "prompt_inject.txt"
            label = "注入内容"
        path = Path(self.store.data_dir) / name
        try:
            path.write_text(text, encoding="utf-8")
            written = str(path)
        except Exception as exc:
            written = f"（写文件失败：{exc}）"
        lines = [f"{label}：{len(text)} 字符，共 {len(prompt_section_index(text))} 段"]
        for item in prompt_section_index(text):
            lines.append(f"· {item['title']}：{item['chars']} 字符")
        lines.append(f"完整内容已写入：{written}")
        lines.append("（编辑器「调试 → 预览注入内容 / 预览自主提示词」里能看全文）")
        return "\n".join(lines)

    async def _handle_session_command(self, event: AstrMessageEvent, rest: list[str]) -> str:
        if not self._can_admin(event):
            return "只有管理员可以管理会话白名单。"
        sub = rest[0].lower() if rest else "list"
        if sub in ("list", "列表"):
            sessions = self.engine.sessions.sessions if self.engine.sessions else []
            if not sessions:
                return "白名单为空。"
            lines = [
                f"{'✅' if item.enabled else '⛔'} {item.session_id} ({item.type})"
                + (f" - {item.note}" if item.note else "")
                for item in sessions
            ]
            groups = list(getattr(self.engine.sessions, "groups", None) or [])
            if groups:
                lines.append("")
                lines.append("会话组（同一个她，共享状态 / 记忆 / 上下文）：")
                lines.extend(
                    f"🔗 {group.name or group.id}："
                    + "、".join(group.sessions or [])
                    + (f"（代表：{group.main_session}）" if group.main_session else "")
                    for group in groups
                )
            return "会话白名单：\n" + "\n".join(lines)
        if sub in ("add", "添加"):
            target = rest[1] if len(rest) > 1 else event.unified_msg_origin
            session_type = "private" if event.is_private_chat() and target == event.unified_msg_origin else (
                "group" if ":GroupMessage:" in target else "private"
            )
            added = self.store.add_session(target, session_type=session_type)
            self.engine.reload_config()
            return f"{'已添加' if added else '已在白名单中'}：{target}"
        if sub in ("remove", "移除", "delete"):
            if len(rest) < 2:
                return "用法：/vw session remove <会话 ID>"
            removed = self.store.remove_session(rest[1])
            self.engine.reload_config()
            return "已移除。" if removed else "没找到这个会话。"
        if sub in ("enable", "启用", "disable", "禁用"):
            if len(rest) < 2:
                return "用法：/vw session enable|disable <会话 ID>"
            enabled = sub in ("enable", "启用")
            ok = self.store.set_session_enabled(rest[1], enabled)
            self.engine.reload_config()
            return ("已更新。" if ok else "没找到这个会话。")
        return "用法：/vw session list|add [ID]|remove <ID>|enable|disable <ID>"

    # ================= Web API =================

    def sampling_params(self) -> dict[str, float]:
        """给适配器用的采样参数（关着就返回空字典＝不覆盖 Provider 自己的设置）。"""

        config = dict(getattr(self, "_sampling_config", None) or {})
        if not config.get("enabled"):
            return {}
        result: dict[str, float] = {}
        for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            try:
                number = float(config.get(key))
            except (TypeError, ValueError):
                continue
            result[key] = number
        return result

    def _register_web_apis(self) -> None:
        register = self.context.register_web_api
        p = PLUGIN_NAME
        register(f"/{p}/auth/status", self.api_auth_status, ["GET"], "编辑器鉴权状态")
        register(f"/{p}/auth/login", self.api_auth_login, ["POST"], "编辑器登录")
        register(f"/{p}/auth/logout", self.api_auth_logout, ["POST"], "退出登录")
        register(f"/{p}/auth/password", self.api_auth_password, ["POST"], "修改访问密码")
        register(f"/{p}/config", self.api_get_config, ["GET"], "读取全部配置")
        register(f"/{p}/config/world", self.api_put_world, ["POST"], "保存世界配置")
        register(f"/{p}/config/schedules", self.api_put_schedules, ["POST"], "保存日程")
        register(f"/{p}/config/sessions", self.api_put_sessions, ["POST"], "保存会话白名单")
        register(f"/{p}/tools", self.api_tools, ["GET"], "可用工具列表")
        register(f"/{p}/generate/actions", self.api_generate_actions, ["POST"], "按区域生成动作")
        register(f"/{p}/generate/nodes", self.api_generate_nodes, ["POST"], "按区域生成地点")
        register(f"/{p}/reload", self.api_reload, ["POST"], "热加载配置")
        register(f"/{p}/restore-default", self.api_restore, ["POST"], "恢复默认配置")
        register(f"/{p}/state", self.api_state, ["GET"], "实时状态")
        register(f"/{p}/states", self.api_states, ["GET"], "所有会话的简要状态")
        register(f"/{p}/state/action", self.api_state_action, ["POST"], "手动触发动作")
        register(f"/{p}/logs", self.api_logs, ["GET"], "事件日志")
        register(f"/{p}/logs/export", self.api_logs_export, ["GET"], "导出事件日志")
        register(f"/{p}/memories", self.api_memories, ["GET"], "记忆列表")
        register(f"/{p}/memories/create", self.api_memory_create, ["POST"], "新增记忆")
        register(f"/{p}/memories/update", self.api_memory_update, ["POST"], "修改记忆")
        register(f"/{p}/memories/delete", self.api_memory_delete, ["POST"], "删除记忆")
        register(f"/{p}/memories/delete-batch", self.api_memory_delete_batch, ["POST"], "批量删除记忆")
        register(f"/{p}/memories/clear", self.api_memory_clear, ["POST"], "清空记忆")
        register(f"/{p}/logs/delete-batch", self.api_logs_delete_batch, ["POST"], "批量删除日志")
        register(f"/{p}/logs/clear", self.api_logs_clear, ["POST"], "清空日志")
        register(f"/{p}/memories/stats", self.api_memory_stats, ["GET"], "记忆统计")
        register(f"/{p}/memories/export", self.api_memory_export, ["GET"], "导出记忆")
        register(f"/{p}/memories/import", self.api_memory_import, ["POST"], "导入记忆")
        register(f"/{p}/prompt", self.api_prompt, ["GET"], "预览提示词")
        # 注意：配置改动历史占用了 /history（还有 /history/item 等子路径），
        # 数值历史必须另起一条路径，否则两条会互相顶掉。
        register(f"/{p}/state/history", self.api_state_history, ["GET"], "数值历史")
        register(f"/{p}/defaults", self.api_defaults, ["GET"], "内置默认文案")
        register(f"/{p}/backup", self.api_backup, ["POST"], "备份配置")
        register(f"/{p}/presets", self.api_presets, ["GET"], "预设列表")
        register(f"/{p}/presets/save", self.api_preset_save, ["POST"], "把当前配置存成预设")
        register(f"/{p}/presets/new-default", self.api_preset_new_default, ["POST"], "用内置默认世界新建预设")
        register(f"/{p}/presets/apply", self.api_preset_apply, ["POST"], "应用预设")
        register(f"/{p}/presets/delete", self.api_preset_delete, ["POST"], "删除预设")
        register(f"/{p}/presets/rename", self.api_preset_rename, ["POST"], "重命名预设")
        register(f"/{p}/presets/json", self.api_preset_json, ["GET", "POST"], "读写预设 JSON")
        register(f"/{p}/history", self.api_history, ["GET"], "配置改动历史")
        register(f"/{p}/history/item", self.api_history_item, ["GET"], "看一条历史")
        register(f"/{p}/history/restore", self.api_history_restore, ["POST"], "恢复到这个版本")
        register(f"/{p}/history/delete", self.api_history_delete, ["POST"], "删除一条历史")
        register(f"/{p}/history/clear", self.api_history_clear, ["POST"], "清空历史")
        register(f"/{p}/events", self.api_events, ["GET"], "事件线索与能力值")
        register(f"/{p}/persona-brief", self.api_persona_brief, ["GET", "POST"], "简易人设")
        register(f"/{p}/voice-samples", self.api_voice_samples, ["GET"], "声音样例")
        register(
            f"/{p}/voice-samples/generate",
            self.api_voice_samples_generate,
            ["POST"],
            "生成声音样例候选",
        )
        register(
            f"/{p}/voice-samples/from-chat",
            self.api_voice_samples_from_chat,
            ["POST"],
            "从近期聊天里挑样例候选",
        )
        register(
            f"/{p}/voice-samples/save",
            self.api_voice_samples_save,
            ["POST"],
            "保存挑中的声音样例",
        )
        register(f"/{p}/persona/review", self.api_persona_review, ["POST"], "体检人设")
        register(
            f"/{p}/persona/apply", self.api_persona_apply, ["POST"], "应用挑中的改动"
        )
        register(
            f"/{p}/eval-script", self.api_eval_script, ["POST"], "生成测评剧本"
        )
        register(f"/{p}/eval-run", self.api_eval_run, ["POST"], "跑测评（并发）")
        register(f"/{p}/persona-source", self.api_persona_source, ["GET"], "读 AstrBot 当前给这个会话的人设")
        register(f"/{p}/profile/people", self.api_profile_people, ["GET"], "通讯录：认识的人")
        register(f"/{p}/profile/person", self.api_profile_person, ["GET", "POST"], "通讯录：一个人的画像")
        register(f"/{p}/profile/fact", self.api_profile_fact, ["POST"], "通讯录：改事实")
        register(f"/{p}/profile/bond", self.api_profile_bond, ["POST"], "通讯录：改关系")
        register(f"/{p}/profile/affinity", self.api_profile_affinity, ["POST"], "通讯录：改好感度")
        register(
            f"/{p}/profile/grudge",
            self.api_profile_grudge,
            ["POST"],
            "通讯录：她记着的一笔账",
        )
        register(f"/{p}/profile/forget", self.api_profile_forget, ["POST"], "通讯录：忘掉一个人")
        register(f"/{p}/profile/consolidate", self.api_profile_consolidate, ["POST"], "立刻整理一次（记忆 + 画像）")

    # ---------------- 通讯录（用户画像）----------------

    def _profile_scope(self, value: Any) -> str:
        return self._scope(value)

    async def _profile_person_payload(self, session_id: str, user_id: str) -> dict[str, Any]:
        """一个人的完整画像：字段 + 事实 + 关系（含历史与"他自称"）+ 好感日志。"""

        store = getattr(self.engine, "profiles", None)
        payload: dict[str, Any] = {"session": session_id, "user_id": user_id}
        if store is None:
            return payload
        payload["person"] = self.engine.person_payload(session_id, user_id)
        payload["facts"] = store.facts(session_id, user_id, statuses=["active", "candidate", "past"])
        # 垫底的"陌生人"和同类里被顶掉的旧关系都不往外露（老数据靠这一层收干净）
        bonds = store.visible_bonds(session_id, user_id)
        for item in bonds:
            item["since_text"] = self.engine._date_text(item.get("since"))
            item["until_text"] = self.engine._date_text(item.get("until"))
        payload["bonds"] = bonds
        payload["affinity_logs"] = store.affinity_logs(session_id, user_id, limit=50)
        # 「她想不想这个人」：通讯录里一眼能看到，不用去状态页翻
        state = await self.engine.load_state(session_id, cold_start=False)
        # 「她记着他一笔账」：记的账是分人的，这里只回这个人的那条
        grudge = self.engine.grudge_for(state, user_id)
        payload["grudge"] = (
            {
                **grudge,
                "at_text": self.engine._date_text(grudge.get("at")),
                "until_text": self.engine._date_text(grudge.get("until")),
                "days": max(
                    0.0,
                    (self.engine._now() - float(grudge.get("at") or 0.0)) / 86400.0,
                ),
            }
            if grudge
            else None
        )
        # 「她自己记着的事」：只列跟这个人相关的（没写对谁的就是她自己的打算）
        payload["own_topics"] = [
            dict(item)
            for item in (state.own_topics or [])
            if isinstance(item, dict)
            and str(item.get("who") or "") in ("", user_id)
        ]
        overview = self.engine.miss_overview(state)
        payload["miss"] = next(
            (item for item in overview.get("people", []) if item.get("user_id") == user_id),
            {
                "user_id": user_id,
                "value": 0.0,
                "waiting": False,
                "ready_in_minutes": 0,
                "will_reach_out": False,
            },
        )
        payload["miss_threshold"] = overview.get("threshold")
        config = self.engine.world.profile
        payload["bonds_config"] = [
            {
                "name": bond.name,
                "slot": bond.slot,
                "group": bond.group,
                "cap": bond.cap,
                "unique": bool(bond.unique),
                "negative": bool(bond.negative),
                "aliases": list(bond.aliases or []),
            }
            for bond in config.bonds
        ]
        payload["levels_config"] = [
            {
                "name": level.name,
                "min_affinity": level.min_affinity,
                "max_affinity": level.max_affinity,
                "deny": [store.action_label(item) for item in (level.deny or [])],
                "prompt": level.prompt,
            }
            for level in config.levels
        ]
        return payload

    async def api_profile_people(self):
        """通讯录列表：这个人叫啥、什么关系、好感多少、最近说过话没有。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._profile_scope(request.query.get("session", ""))
        if not session_id:
            return error_response("缺少 session 参数")
        store = getattr(self.engine, "profiles", None)
        if store is None:
            return json_response({"people": [], "enabled": False})
        people: list[dict[str, Any]] = []
        for row in store.list_people(session_id, limit=200):
            user_id = str(row.get("user_id") or "")
            view = store.view(session_id, user_id)
            if view is None:
                continue
            people.append(
                {
                    "user_id": user_id,
                    "name": view.name,
                    "qq_name": view.qq_name,
                    "affinity": view.affinity,
                    "level": view.level.name,
                    "bonds": list(view.affinities),
                    "claims": [str(item.get("type") or "") for item in view.claims],
                    "digest": row.get("digest") or "",
                    "message_count": int(row.get("message_count") or 0),
                    "last_seen_at": float(row.get("last_seen_at") or 0.0),
                    "first_seen_at": float(row.get("first_seen_at") or 0.0),
                    "days": len(
                        [
                            item
                            for item in ((row.get("payload") or {}).get("days") or [])
                            if str(item)
                        ]
                    ),
                }
            )
        return json_response(
            {
                "people": people,
                "enabled": bool(self.engine.world.profile.enabled),
                "consolidate_enabled": bool(
                    self.engine.world.profile.consolidate_enabled
                ),
                "digest_limit": int(self.engine.world.profile.digest_limit),
            }
        )

    async def api_profile_person(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(
            payload.get("session") or request.query.get("session", "")
        )
        user_id = str(payload.get("user_id") or request.query.get("user_id", "") or "")
        if not session_id or not user_id:
            return error_response("缺少 session 或 user_id 参数")
        store = getattr(self.engine, "profiles", None)
        if store is None:
            return error_response("画像功能还没就绪")
        method = str(getattr(request, "method", "") or "").upper()
        if method == "POST" or str(request.query.get("_method", "")).upper() == "POST":
            if "note" in payload:
                store.set_note(session_id, user_id, str(payload.get("note") or ""))
            call_me = str(payload.get("call_me") or "").strip()
            call_him = str(payload.get("call_him") or "").strip()
            if call_me or call_him:
                result = store.set_call_names(
                    session_id,
                    user_id,
                    call_me=call_me,
                    call_him=call_him,
                    explicit=True,
                )
                if result.get("rejected"):
                    reason = result["rejected"][0].get("reason") or "这个称呼不能用"
                    return error_response(reason)
            if "digest" in payload:
                store.set_digest(session_id, user_id, str(payload.get("digest") or ""))
        return json_response(await self._profile_person_payload(session_id, user_id))

    async def api_profile_fact(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        action = str(payload.get("action") or "add")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        db = store.db
        group_id = store.group_key(session_id)
        if action == "add":
            result = store.note_fact(
                session_id,
                user_id,
                text=str(payload.get("text") or ""),
                kind=str(payload.get("kind") or "other"),
                evidence=str(payload.get("evidence") or "主人手动加的"),
                context=str(payload.get("context") or ""),
                confidence=1.0,
                status=str(payload.get("status") or "active"),
                pinned=bool(payload.get("pinned")),
            )
            if not result.get("ok"):
                return error_response(str(result.get("reason") or "没记下来"))
        elif action == "update":
            fact_id = int(payload.get("id") or 0)
            if not fact_id:
                return error_response("缺少 id")
            db.update_user_fact(
                fact_id=fact_id,
                text=str(payload.get("text")) if "text" in payload else None,
                kind=str(payload.get("kind")) if "kind" in payload else None,
                status=str(payload.get("status")) if "status" in payload else None,
                pinned=bool(payload["pinned"]) if "pinned" in payload else None,
            )
        elif action == "delete":
            db.delete_user_fact(fact_id=int(payload.get("id") or 0))
        else:
            return error_response("不认识的操作")
        return json_response(
            {
                "ok": True,
                "facts": db.list_user_facts(group_id=group_id, user_id=user_id),
                "person": self.engine.person_payload(session_id, user_id),
            }
        )

    async def api_profile_bond(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        action = str(payload.get("action") or "note")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        if action == "note":
            result = store.note_bond(
                session_id,
                user_id,
                type=str(payload.get("type") or ""),
                evidence=str(payload.get("evidence") or "主人手动设的"),
                confidence=1.0,
                asserted_by=str(payload.get("asserted_by") or "她的判断"),
                policy=str(payload.get("policy") or ""),
            )
            if not result.get("ok"):
                return error_response(str(result.get("reason") or "没记下来"))
        elif action == "close":
            store.close_bond(session_id, user_id, bond_id=int(payload.get("id") or 0))
        elif action == "accept":
            result = store.accept_claim(
                session_id,
                user_id,
                bond_id=int(payload.get("id") or 0),
                policy=str(payload.get("policy") or ""),
            )
            if not result.get("ok"):
                return error_response(str(result.get("reason") or "认不下来"))
        elif action == "delete":
            store.db.delete_user_bond(bond_id=int(payload.get("id") or 0))
        else:
            return error_response("不认识的操作")
        return json_response(
            {
                "ok": True,
                "person": self.engine.person_payload(session_id, user_id),
            }
        )

    async def api_profile_affinity(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        if "value" in payload:
            row = store.profile(session_id, user_id)
            before = float((row or {}).get("affinity") or 0.0)
            delta = float(payload.get("value") or 0.0) - before
        else:
            delta = float(payload.get("delta") or 0.0)
        result = store.adjust_affinity(
            session_id,
            user_id,
            delta,
            reason=str(payload.get("reason") or "主人调的"),
            source="manual",
        )
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "没改成"))
        return json_response(
            {
                "ok": True,
                "affinity": result.get("value"),
                "person": self.engine.person_payload(session_id, user_id),
            }
        )

    async def api_profile_grudge(self):
        """通讯录里手动管她记着的账：加一笔 / 算了 / 删掉。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        action = str(payload.get("action") or "add")
        if not session_id or not user_id:
            return error_response("缺少参数")
        async with self.engine.session_state(session_id) as state:
            if action == "add":
                reason = str(payload.get("reason") or payload.get("text") or "").strip()
                if not reason:
                    return error_response("写一句她记着的事")
                ok = self.engine.note_grudge(
                    state,
                    reason,
                    user_id=user_id,
                    session_id=session_id,
                    # 手动加的不吃"每天最多一笔"那道闸（那是拦模型的）
                    force=True,
                )
                if not ok:
                    return error_response("这笔账没能记下来（记仇关着吗？）")
            elif action == "resolve":
                self.engine.resolve_grudge(state, user_id=user_id)
            elif action == "delete":
                state.grudges = [
                    item
                    for item in (state.grudges or [])
                    if not (
                        isinstance(item, dict)
                        and str(item.get("user_id") or "") == user_id
                    )
                ]
            else:
                return error_response("不认识的操作")
        return json_response(await self._profile_person_payload(session_id, user_id))

    async def api_profile_forget(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        removed = store.db.delete_user_profile(
            group_id=store.group_key(session_id), user_id=user_id
        )
        return json_response({"ok": True, "removed": removed})

    async def api_profile_consolidate(self):
        """立刻整理一次（编辑器上的"现在整理一次"）：消化这段时间的经历。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session 参数")
        mode = str(payload.get("mode") or "full")
        dry_run = bool(payload.get("dry_run"))
        state_key = self.engine.state_key(session_id)
        async with self.engine.session_state(state_key) as state:
            result = await self.engine.consolidate_now(
                state, mode=mode, dry_run=dry_run
            )
        if result is None:
            return error_response("整理没跑起来：先确认「睡眠整理模型」可用")
        if not result.get("ok"):
            return json_response({"ok": False, "note": result.get("note") or "没整理出东西"})
        return json_response(result)

    async def api_events(self):
        """事件系统概览：能力值、未了的事、最近的线索。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = str(request.query.get("session", "") or "")
        if not session_id:
            return error_response("缺少 session 参数")
        session_id = self._scope(session_id)
        overview = await self.engine.event_overview(session_id)
        return json_response(overview)

    async def api_persona_source(self):
        """编辑器「从 AstrBot 导入」用：把 AstrBot 给这个会话的人设读出来。

        插件默认就是用它（``world.persona.mode = astrbot``）；这里只是给个入口，
        让用户能把手上那份人格一键抄进配置里，从此跟着预设走。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        persona = getattr(self, "persona_port", None)
        if persona is None or not hasattr(persona, "astrbot_persona_text"):
            return error_response("人设通道还没就绪")
        text = await persona.astrbot_persona_text(session_id)
        return json_response(
            {
                "session": session_id,
                "text": text,
                "chars": len(text),
                "mode": str(getattr(self.engine.world.persona, "mode", "astrbot")),
            }
        )

    async def api_persona_brief(self):
        """简易人设：按人格缓存。POST 时 ``generate=true`` 表示用模型从主人设生成。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = str(
            payload.get("session") or request.query.get("session", "") or ""
        )
        if not session_id:
            return error_response("缺少 session 参数")
        if request.method == "POST" or str(request.query.get("_method", "")).upper() == "POST":
            if payload.get("generate"):
                # 编辑器刚改过、还没保存的角色卡优先：向导里写完人设就点生成时，
                # 读配置只会拿到旧那份。
                brief = await self.engine.generate_persona_brief(
                    session_id, persona_text=str(payload.get("persona") or "")
                )
                if not brief:
                    return error_response(
                        "生成失败：先确认「生成器模型」可用，而且这个会话能读到人设"
                    )
            else:
                await self.engine.save_persona_brief(
                    session_id, str(payload.get("brief") or "")
                )
            state = await self.engine.persona_brief_state(session_id)
            return json_response({"ok": True, "persona_brief": state})
        return json_response({"persona_brief": await self.engine.persona_brief_state(session_id)})

    def _voice_sample_payload(self) -> dict[str, Any]:
        persona = self.engine.world.persona
        return {
            "samples": self.engine.persona_samples(),
            "candidates": [
                dict(item)
                for item in list(getattr(persona, "sample_candidates", None) or [])
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ],
            "scenes": [
                {"id": key, "label": label}
                for key, label in self.engine.prompts.VOICE_SCENES
            ],
            "per_turn": int(getattr(persona, "samples_per_turn", 3) or 3),
            "max": int(getattr(persona, "samples_max", 12) or 12),
            "candidates_max": int(getattr(persona, "candidates_max", 80) or 80),
        }

    async def api_voice_samples(self):
        """看样例库 + 可用的场景清单。"""

        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(self._voice_sample_payload())

    async def api_voice_samples_generate(self):
        """让内容生成模型按场景出候选（草稿，不落库）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session（要拿它的角色卡去生成）")
        scenes = payload.get("scenes")
        picked = [str(item) for item in scenes] if isinstance(scenes, list) else None
        result = await self.engine.generate_voice_samples(
            session_id, picked, persona_text=str(payload.get("persona") or "")
        )
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "生成失败"))
        return json_response({**result, **{"scenes_meta": self._voice_sample_payload()["scenes"]}})

    async def api_voice_samples_from_chat(self):
        """从近期聊天留档里挑她说过、而且被接住了的话，当样例候选（不落库）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        try:
            limit = max(1, min(40, int(payload.get("limit") or 12)))
        except (TypeError, ValueError):
            limit = 12
        async with self.engine.session_state(session_id) as state:
            found = self.engine.voice_sample_candidates_from_chat(state, limit=limit)
        return json_response(
            {"ok": True, "candidates": found, "note": "" if found else "这段时间没挑出合适的"}
        )

    async def api_voice_samples_save(self):
        """把挑中的样例写进世界配置（落库前先留一份历史）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        raw_samples = payload.get("samples")
        if not isinstance(raw_samples, list):
            return error_response("samples 必须是数组")
        persona = self.engine.world.persona
        limit = max(1, int(getattr(persona, "samples_max", 12) or 12))
        candidates_limit = max(1, int(getattr(persona, "candidates_max", 80) or 80))
        cleaned: list[dict[str, Any]] = []
        for index, item in enumerate(raw_samples):
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            cleaned.append(
                {
                    "id": str(item.get("id") or f"s{index + 1}"),
                    "scene": str(item.get("scene") or ""),
                    "label": str(item.get("label") or ""),
                    "move": " ".join(str(item.get("move") or "").split())[:20],
                    "text": text[:120],
                    "source": str(item.get("source") or "model"),
                }
            )
        if len(cleaned) > limit:
            return error_response(f"最多留 {limit} 条，先删掉几条再存")
        # 候选池：可选一起存。攒着不动，只有「采用」才会挪进上面的样例库
        raw_candidates = payload.get("candidates")
        candidates: list[dict[str, Any]] | None = None
        if isinstance(raw_candidates, list):
            candidates = []
            for index, item in enumerate(raw_candidates):
                if not isinstance(item, dict):
                    continue
                text = " ".join(str(item.get("text") or "").split())
                if not text:
                    continue
                candidates.append(
                    {
                        "id": str(item.get("id") or f"c{index + 1}"),
                        "scene": str(item.get("scene") or ""),
                        "label": str(item.get("label") or ""),
                        "move": " ".join(str(item.get("move") or "").split())[:20],
                        "text": text[:120],
                        "context": " ".join(str(item.get("context") or "").split())[:60],
                        "source": str(item.get("source") or "model"),
                    }
                )
            candidates = candidates[-candidates_limit:]
        world = self.store.raw_world()
        persona_raw = dict(world.get("persona") or {})
        persona_raw["samples"] = cleaned
        if candidates is not None:
            persona_raw["sample_candidates"] = candidates
        if payload.get("per_turn") is not None:
            try:
                persona_raw["samples_per_turn"] = max(1, min(6, int(payload["per_turn"])))
            except (TypeError, ValueError):
                pass
        world["persona"] = persona_raw
        warnings = self.store.save_world(world, reason="改动声音样例")
        self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings, **self._voice_sample_payload()})

    async def api_eval_run(self):
        """把剧本并发跑一遍：带着完整提示词，收集她的真实回复（不写任何状态）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        rounds = payload.get("rounds")
        if not isinstance(rounds, list) or not rounds:
            return error_response("缺少 rounds（要按剧本跑的问题）")
        if len(rounds) > 30:
            return error_response("一次最多跑 30 轮")
        try:
            concurrency = int(payload.get("concurrency") or 3)
        except (TypeError, ValueError):
            concurrency = 3
        provider_id = str(payload.get("provider_id") or "").strip()
        llm = AstrBotLLM(self, provider_id or self.llm_provider_id, sampling=self.sampling_params())
        director_id = str(payload.get("director_provider_id") or "").strip()
        result = await self.engine.run_eval(
            session_id,
            [dict(item) for item in rounds if isinstance(item, dict)],
            llm=llm,
            concurrency=concurrency,
            variant=str(payload.get("variant") or "full"),
            director_llm=AstrBotLLM(
                self,
                director_id or self.judge_provider_id,
                sampling=self.sampling_params(),
            ),
        )
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "跑失败"))
        return json_response(result)

    async def api_eval_script(self):
        """生成测评剧本：同一个剧本拿去跑不同模型，比"谁更像她"。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        try:
            rounds = int(payload.get("rounds") or 20)
        except (TypeError, ValueError):
            rounds = 20
        result = await self.engine.generate_eval_script(session_id, rounds)
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "生成失败"))
        return json_response(result)

    async def api_persona_review(self):
        """体检人设：返回问题清单 + 可逐条采纳的改动（**不落库**）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        result = await self.engine.review_persona(session_id)
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "体检失败"))
        return json_response(result)

    async def api_persona_apply(self):
        """把挑中的改动写进角色卡（写前自动留一份历史）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        rewrites = payload.get("rewrite") if isinstance(payload.get("rewrite"), list) else []
        adds = payload.get("add") if isinstance(payload.get("add"), list) else []
        result = self.engine.apply_persona_changes(rewrites, adds)
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "应用失败"))
        return json_response(result)

    def _guard(self, payload: dict[str, Any] | None = None) -> Any:
        """统一鉴权：返回错误响应或 None。"""

        token = request.query.get("token", "") or ""
        if not token and isinstance(payload, dict):
            token = str(payload.get("token", "") or "")
        if self.auth.check(token):
            return None
        return error_response("需要先登录编辑器", status_code=401)

    def _scope(self, value: Any) -> str:
        """编辑器里选的可能是会话，也可能是会话组。

        会话组要落到它的代表会话上（她的状态、日志、日程都挂在那儿）。
        """

        text = str(value or "").strip()
        if not text:
            return ""
        engine = getattr(self, "engine", None)
        if engine is None:
            return text
        try:
            return engine.scope_session(text)
        except Exception:
            return text

    def _memory_sessions(self, value: Any) -> list[str]:
        """记忆按会话各存一份；查的时候把同组的会话一起当条件。

        编辑器里可能选的是会话组：那就展开成组里的所有会话。
        """

        engine = getattr(self, "engine", None)
        if engine is None:
            text = str(value or "").strip()
            return [text] if text else []
        try:
            return engine.group_sessions(self._scope(value))
        except Exception:
            text = str(value or "").strip()
            return [text] if text else []

    async def api_auth_status(self):
        return json_response(
            {
                "password_required": self.auth.has_password(),
                "locked_seconds": self.auth.locked(),
                "llm_provider_id": self.llm_provider_id,
                "tick_interval": self.tick_interval,
                "decider_interval": self.decider_interval,
            }
        )

    async def api_auth_login(self):
        payload = await request.json(default={}) or {}
        wait = self.auth.locked()
        if wait > 0:
            return error_response(f"失败次数过多，请 {wait} 秒后再试", status_code=429)
        if not self.auth.has_password():
            return json_response({"token": "", "password_required": False})
        if not self.auth.verify(str(payload.get("password", ""))):
            self.auth.note_failure()
            return error_response("密码不正确", status_code=401)
        return json_response({"token": self.auth.issue_token(), "password_required": True})

    async def api_auth_logout(self):
        payload = await request.json(default={}) or {}
        self.auth.revoke(str(payload.get("token", "") or ""))
        return json_response({"ok": True})

    async def api_auth_password(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        new_password = str(payload.get("new_password", "") or "")
        if self.auth.has_password() and not self.auth.verify(
            str(payload.get("old_password", ""))
        ):
            return error_response("旧密码不正确", status_code=403)
        self.auth.set_password(new_password)
        return json_response({"ok": True, "password_required": self.auth.has_password()})

    async def api_get_config(self):
        guard = self._guard()
        if guard is not None:
            return guard
        # 老版本写下的配置文件里可能缺新增的键（例如心潮回落速度），
        # 直接返回原始 JSON 会让编辑器把它们显示成 0，一保存就把 0 写回磁盘。
        # 这里用校验后的配置补齐已知键，同时保留用户自己加的未知键。
        # 必须 by_alias：连线端点在模型里叫 from_（from 是 Python 关键字），
        # 但文件里和前端读的都是 from——按字段名 dump 会让地图连线整个消失。
        world = normalize_legacy_keys(
            normalize_edge_keys(
                {
                    **self.store.raw_world(),
                    **self.engine.world.model_dump(mode="json", by_alias=True),
                }
            )
        )
        schedules = self.store.raw_schedules()
        if self.engine.schedules is not None:
            schedules = {
                **schedules,
                **self.engine.schedules.model_dump(mode="json", by_alias=True),
            }
        sessions = self.store.raw_sessions()
        if self.engine.sessions is not None:
            sessions = {
                **sessions,
                **self.engine.sessions.model_dump(mode="json", by_alias=True),
            }
        return json_response(
            {
                "world": world,
                "schedules": schedules,
                "sessions": sessions,
                "warnings": self.engine.load_warnings,
                # 模型槽位当前用的是哪个（向导里做"模型配了吗"的检查用；只有 id，没有 key）
                "providers": self._provider_slots(),
                # 图片转述缓存的使用情况（编辑器里显示"省了多少次识别"）
                "vision_cache": await self._vision_cache_stats(),
                # 当前天气：地图页顶部横幅直接用它
                "weather": await self.engine.weather_payload(),
            }
        )

    def _provider_slots(self) -> dict[str, str]:
        """六个模型槽位现在指向哪个 Provider（只给 id，不含 key）。

        向导里要能一眼看出"哪个槽位还没配"——这些槽位在 AstrBot 的插件配置里，
        编辑器本身改不了，所以只做展示 + 告诉用户去哪配。
        """

        def show(value: str) -> str:
            return str(value or "").strip()

        return {
            "llm": show(self.llm_provider_id) or "（跟会话默认）",
            "helper": show(self.helper_provider_id) or "（跟看图模型）",
            "judge": show(self.judge_provider_id) or "（跟打杂模型）",
            "vision": show(self.vision_provider_id) or "（不转述图片）",
            "creator": show(self.creator_provider_id) or "（跟插件用模型）",
            "event": show(self.event_provider_id) or "（跟打杂模型）",
            "consolidate": show(self.consolidate_provider_id) or "（跟打杂模型）",
        }

    async def _vision_cache_stats(self) -> dict[str, Any]:
        try:
            stats = await self.db.call("image_cache_stats")
        except Exception:
            stats = {"entries": 0, "hits": 0}
        vision = getattr(self, "vision", None)
        stats["session_hits"] = int(getattr(vision, "hits", 0) or 0)
        return stats

    async def api_put_world(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        world = payload.get("world")
        if not isinstance(world, dict):
            return error_response("world 必须是对象")
        try:
            warnings = self.store.save_world(world)
        except ValidationError as exc:
            return self._save_error("世界配置", exc)
        self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings})

    async def api_put_schedules(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        schedules = payload.get("schedules")
        if not isinstance(schedules, dict):
            return error_response("schedules 必须是对象")
        # 工具 / 指令型步骤缺「想干什么」时，让内容生成模型补一句写进配置：
        # 运行时就不用再猜，也不会因为缺意图被跳过。
        schedules, filled = await self.engine.fill_schedule_intents(schedules)
        try:
            warnings = self.store.save_schedules(schedules)
        except ValidationError as exc:
            return self._save_error("日程", exc)
        self.engine.reload_config()
        return json_response(
            {"ok": True, "warnings": warnings, "filled": filled, "schedules": schedules}
        )

    async def api_put_sessions(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            return error_response("sessions 必须是对象")
        try:
            warnings = self.store.save_sessions(sessions)
        except ValidationError as exc:
            return self._save_error("会话列表", exc)
        self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings})

    @staticmethod
    def _save_error(label: str, exc: Exception):
        """保存没过校验：给一句人话 + 具体是哪一项，别把英文校验串直接丢给用户。"""

        problems: list[str] = []
        try:
            for item in exc.errors():  # type: ignore[attr-defined]
                where = " / ".join(str(part) for part in (item.get("loc") or []))
                message = str(item.get("msg") or "").strip()
                problems.append(f"{where}：{message}" if where else message)
        except Exception:
            problems = []
        detail = "；".join(part for part in problems if part)[:500]
        return error_response(
            f"{label}没通过校验，这次没有保存（改动还在编辑器里，改完再存一次）。"
            + (f"\n{detail}" if detail else f"\n{exc}")
        )

    async def api_generate_actions(self):
        """批量生成动作草稿（只返回草稿，用户在编辑器里勾选后才保存）。

        传 ``node`` 就是"只给这个地点生成"，否则按 ``zone`` 里的每个地点生成。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        zone_id = str(payload.get("zone", "") or "").strip()
        node_id = str(payload.get("node", "") or "").strip()
        if not zone_id and node_id:
            zone_id = self.engine.world.zone_of(node_id)
        if not zone_id:
            return error_response("缺少 zone 参数")
        result = await self.engine.generate_actions_for_zone(
            zone_id,
            payload.get("per_node"),
            node_ids=[node_id] if node_id else None,
        )
        return json_response(result)

    async def api_generate_nodes(self):
        """按区域批量生成地点草稿（含自动摆位与自动连线）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        zone_id = str(payload.get("zone", "") or "").strip()
        if not zone_id:
            return error_response("缺少 zone 参数")
        result = await self.engine.generate_nodes_for_zone(
            zone_id, payload.get("count")
        )
        return json_response(result)

    async def api_tools(self):
        guard = self._guard()
        if guard is not None:
            return guard
        tools = self.engine.available_tools()
        schemas = self.engine.tool_schemas()
        sources = self.engine.tool_sources()
        plugins = self.engine.tool_owners()
        return json_response(
            {
                "tools": [
                    {
                        "name": name,
                        "description": desc,
                        "parameters": schemas.get(name, {}),
                        "param_text": self.engine.tool_param_text(name),
                        "source": sources.get(name, "plugin"),
                        "plugin": plugins.get(name, ""),
                    }
                    for name, desc in tools.items()
                ],
                "global_allowed": self.engine.world.global_allowed_tools,
            }
        )

    async def api_reload(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        warnings = self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings})

    async def api_restore(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        scope = str(payload.get("scope", "all"))
        restored = self.store.restore_default(scope)
        self.engine.reload_config()
        return json_response({"ok": True, "restored": restored})

    async def api_state(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        if not session_id:
            return error_response("缺少 session 参数")
        snapshot = await self.engine.snapshot(session_id)
        return json_response(snapshot)

    async def api_state_action(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        action = str(payload.get("action", "") or "")
        if not action:
            return error_response("缺少 action")
        if action == "refresh_weather":
            # 天气本身不分会话，但「查天气」配成指令型时要借一条真实消息当上下文，
            # 所以前端选中的那条会话优先，没带就按白名单第一条
            note = await self.engine.maybe_refresh_weather(
                force=True, session_id=self._scope(payload.get("session"))
            )
            return json_response(
                {
                    "ok": True,
                    "note": note,
                    "weather": await self.engine.weather_payload(),
                }
            )
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        if action == "wake":
            try:
                was = await asyncio.wait_for(
                    self.engine.wake_up(session_id), timeout=STATE_ACTION_TIMEOUT
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "她正在忙，稍等一下再叫醒"}
                )
            return json_response({"ok": True, "was_sleeping": was})
        if action == "interrupt":
            try:
                done = await asyncio.wait_for(
                    self.engine.interrupt(session_id, force=True),
                    timeout=STATE_ACTION_TIMEOUT,
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "她正在忙，稍等一下再打断"}
                )
            return json_response({"ok": True, "interrupted": done})
        if action == "reset_tools":
            # 手动解除工具熔断（状态页上的「立即重试」）
            name = str(payload.get("tool") or "").strip()
            cleared = self.engine.reset_tool_breakers(name)
            return json_response({"ok": True, "cleared": cleared})
        if action == "event":
            # 编辑器上「给她来件事」：等于用户投递一个事件
            note = await self.engine.submit_event(
                session_id, str(payload.get("text") or "")
            )
            return json_response(
                {
                    "ok": True,
                    "note": note,
                    "events": await self.engine.event_overview(session_id),
                }
            )
        if action in ("advance_event", "close_event"):
            # 编辑器上「立即推进一幕」/「立刻完结」：手动改事件节奏
            if self.engine.is_busy(session_id):
                return json_response(
                    {"ok": False, "note": "她正在忙（动作或检索还没结束），等这一轮跑完再点"}
                )
            thread_id = str(payload.get("thread") or "")
            handler = (
                self.engine.advance_thread
                if action == "advance_event"
                else self.engine.close_thread
            )
            try:
                note = await asyncio.wait_for(
                    handler(session_id, thread_id), timeout=STATE_ACTION_TIMEOUT
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "这件事还在演（模型还没回），稍等一下再看"}
                )
            return json_response(
                {
                    "ok": True,
                    "note": note,
                    "events": await self.engine.event_overview(session_id),
                }
            )
        if action == "tick":
            # 连点几下不能攒成一串 tick：同一会话同一时刻只放行一次推进，
            # 后来的这几次直接忽略（前端也有同样的防护，这里是兜底）。
            # 推进是**全局**的（一次 tick 走所有会话），所以别按会话判重。
            if self._advancing:
                return json_response(
                    {"ok": False, "note": "上一次推进还在跑，这次点击已忽略"}
                )
            self._advancing.add(session_id)
            tick_task = asyncio.ensure_future(self.engine.tick())

            def _tick_done(task: asyncio.Task) -> None:
                self._advancing.discard(session_id)
                if task.cancelled():
                    return
                error = task.exception()
                if error is not None:
                    self.logger.warning(f"[virtual_world] 推进 tick 失败：{error}")

            tick_task.add_done_callback(_tick_done)
            try:
                # 她正在调模型（例如刚走到新地点要就地决定）时不会直接失败，
                # 而是等这一轮跑完再推进——这段时间本来就该发生一个 tick。
                # 用 shield：等不到就把它留在后台跑完，**不中途取消**
                # （取消会把这一轮砍在半路，日志和状态都会变得莫名其妙）。
                await asyncio.wait_for(
                    asyncio.shield(tick_task), timeout=STATE_TICK_WAIT_SECONDS
                )
            except asyncio.TimeoutError:
                return json_response(
                    {
                        "ok": False,
                        "note": "这一轮还在跑（她在调模型或检索），没有打断它；"
                        "过几秒刷新状态页就能看到结果",
                    }
                )
            except Exception as exc:
                return json_response({"ok": False, "note": f"推进失败：{exc}"})
            outcomes = tick_task.result()
            return json_response(
                {
                    "ok": True,
                    "outcomes": [
                        {
                            "session_id": item.session_id,
                            "messages": item.messages,
                            "notes": item.notes,
                        }
                        for item in outcomes
                    ],
                }
            )
        if action == "decide":
            if self.engine.is_busy(session_id):
                return json_response(
                    {
                        "ok": False,
                        "note": "她正在忙（动作或检索还没结束），这次决策已跳过",
                    }
                )
            # 手动触发：绕过评估间隔，但仍然尊重"正在忙 / 在睡觉"这些硬条件
            try:
                outcome = await asyncio.wait_for(
                    self.engine.maybe_decide(
                        session_id, force=bool(payload.get("force"))
                    ),
                    timeout=STATE_ACTION_TIMEOUT,
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "她正在忙（上一个动作还没结束），稍等一下再点"}
                )
            return json_response(
                {
                    "ok": True,
                    "notes": outcome.notes if outcome else [],
                    "messages": outcome.messages if outcome else [],
                }
            )
        if action == "set_values":
            applied = await self.engine.set_values(
                session_id, dict(payload.get("values") or {})
            )
            return json_response({"ok": True, "applied": applied})
        if action == "run_schedule":
            result = await self.engine.run_schedule_now(
                session_id,
                str(payload.get("schedule_id") or ""),
                force=bool(payload.get("force", True)),
            )
            return json_response(result)
        if action == "set_nickname":
            text = str(payload.get("text") or "").strip()
            if not text:
                return error_response("名片不能为空")
            async with self.engine.session_state(session_id) as state:
                state.bot_current_nickname = text
                state.bot_base_nickname = text
                state.bot_nickname_locked = True
            result = await self.messenger.set_group_card(session_id, text)
            return json_response(
                {"ok": result.ok, "reason": result.reason, "card": result.card}
            )
        if action in ("lock_nickname", "unlock_nickname", "reset_nickname"):
            async with self.engine.session_state(session_id) as state:
                if action == "lock_nickname":
                    state.bot_nickname_locked = True
                    return json_response({"ok": True, "note": "已锁定群名片"})
                if action == "unlock_nickname":
                    state.bot_nickname_locked = False
                    state.last_nickname_update_at = 0.0
                    return json_response({"ok": True, "note": "已解锁，会随状态自动变"})
                base = state.bot_base_nickname or self.engine.world.bot_name
                if not base:
                    return error_response("还不知道她原来的名片，先在群里让她说过话")
                state.bot_nickname_locked = True
                state.bot_current_nickname = base
            result = await self.messenger.set_group_card(session_id, base)
            note = "已改回原名" if result.ok else f"改回原名失败：{result.reason}"
            return json_response({"ok": result.ok, "note": note})
        if action == "refresh_nickname":
            return json_response(await self.engine.refresh_nickname(session_id))
        if action == "reset_state":
            # 清掉这个会话的全部状态（位置、数值、计划、留档）。名字里必须带 state：
            # 以前它就叫 "reset"，和编辑器的「恢复原名」按钮撞名，一点就把状态删了。
            await self.db.call("delete_state", session_id)
            return json_response({"ok": True})
        if action == "clear_context":
            result = await self.engine.clear_chat_context(session_id)
            return json_response({"ok": True, **result})
        if action == "nickname":
            snapshot = await self.engine.load_state(session_id, cold_start=False)
            cards = self.engine.messenger
            ok = await cards.set_group_card(
                session_id, snapshot.bot_current_nickname or snapshot.bot_base_nickname
            )
            return json_response({"ok": ok})
        return error_response(f"未知动作：{action}")

    async def api_states(self):
        guard = self._guard()
        if guard is not None:
            return guard
        return json_response({"sessions": await self.engine.overview()})

    async def api_logs(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        limit = min(1000, max(1, request.query.get("limit", 200, type=int) or 200))
        event_type = request.query.get("type", "") or ""
        keyword = request.query.get("q", "") or ""
        before_id = request.query.get("before", 0, type=int) or 0
        events = await self.db.call(
            "query_events",
            session_id=session_id or None,
            limit=limit,
            event_type=event_type or None,
            keyword=keyword or None,
            before_id=before_id or None,
        )
        types = await self.db.call("event_types", session_id=session_id or None)
        return json_response(
            {
                "events": build_timeline(events, self.engine.world),
                "types": types,
                "has_more": len(events) >= limit,
            }
        )

    async def api_logs_export(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        limit = min(5000, max(1, request.query.get("limit", 2000, type=int) or 2000))
        events = await self.db.call(
            "query_events", session_id=session_id or None, limit=limit
        )
        payload = {
            "session": session_id,
            "exported_at": time.time(),
            "events": build_timeline(events, self.engine.world),
        }
        return json_response(
            payload,
            headers={
                "Content-Disposition": 'attachment; filename="virtual-world-logs.json"'
            },
        )

    async def api_memories(self):
        guard = self._guard()
        if guard is not None:
            return guard
        # 记忆按会话各存一份，但会话组要一起看（组里几个群共用同一段经历）
        session_ids = self._memory_sessions(request.query.get("session", ""))
        node_id = request.query.get("node_id", "") or None
        memory_type = request.query.get("type", "") or None
        scope = request.query.get("scope", "") or None
        keyword = request.query.get("q", "") or ""
        rows = await self.db.call(
            "query_memories",
            session_ids=session_ids or None,
            node_id=node_id,
            memory_type=memory_type,
            scope=scope,
            limit=500,
        )
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("content", ""))]
        return json_response({"memories": rows})

    async def api_memory_create(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session_id"))
        content = str(payload.get("content", "") or "").strip()
        if not session_id or not content:
            return error_response("缺少 session_id 或 content")
        memory_id = self.engine.memory.remember(
            session_id=session_id,
            persona_id=str(payload.get("persona_id", "") or ""),
            node_id=str(payload.get("node_id", "") or ""),
            content=content,
            memory_type=str(payload.get("type", "scene") or "scene"),
            related_users=[str(u) for u in payload.get("related_users", []) or []],
            emotion=str(payload.get("emotion", "") or ""),
            weight=float(payload.get("weight", 0.5) or 0.5),
            scope=payload.get("scope") or None,
            source="manual",
        )
        return json_response({"ok": True, "id": memory_id})

    async def api_memory_update(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        memory_id = payload.get("id")
        if not memory_id:
            return error_response("缺少 id")
        fields = {
            key: payload[key]
            for key in ("content", "emotion", "weight", "node_id", "type", "scope")
            if key in payload
        }
        # 空作用域在召回时匹配不上任何模式，等于把这条记忆"藏起来"，
        # 所以编辑器选了「用默认作用域」时不要真的写空串。
        if not str(fields.get("scope", "") or "").strip():
            fields.pop("scope", None)
        await self.db.call("update_memory", int(memory_id), **fields)
        return json_response({"ok": True})

    async def api_memory_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        memory_id = payload.get("id")
        if not memory_id:
            return error_response("缺少 id")
        removed = await self.db.call("delete_memories", memory_id=int(memory_id))
        return json_response({"ok": True, "removed": removed})

    async def api_memory_delete_batch(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("缺少 ids")
        removed = await self.db.call("delete_memories_by_ids", [int(item) for item in ids])
        return json_response({"ok": True, "removed": removed})

    async def api_memory_clear(self):
        """清空记忆。带 session 只清这个会话，不带则清全部。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        # 记忆按会话各存一份：选了会话组就把组里每个会话的都清掉
        sessions = self._memory_sessions(payload.get("session"))
        if sessions:
            removed = 0
            for item in sessions:
                removed += int(await self.db.call("clear_memories", item) or 0)
        else:
            removed = int(await self.db.call("clear_memories", None) or 0)
        return json_response({"ok": True, "removed": removed})

    async def api_logs_delete_batch(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("缺少 ids")
        removed = await self.db.call("delete_events_by_ids", [int(item) for item in ids])
        return json_response({"ok": True, "removed": removed})

    async def api_logs_clear(self):
        """清空事件日志。带 session 只清这个会话，不带则清全部。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        removed = await self.db.call("clear_events", session_id or None)
        return json_response({"ok": True, "removed": removed})

    async def api_memory_stats(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = request.query.get("session", "") or None
        stats = await self.db.call(
            "memory_stats",
            session_ids=self._memory_sessions(session_id) or None,
        )
        return json_response(stats)

    async def api_memory_export(self):
        guard = self._guard()
        if guard is not None:
            return guard
        data = {
            "world": self.store.raw_world(),
            "schedules": self.store.raw_schedules(),
            "sessions": self.store.raw_sessions(),
            "memories": await self.db.call("query_memories", limit=100000),
        }
        return json_response(data)

    async def api_memory_import(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        memories = payload.get("memories")
        if not isinstance(memories, list):
            return error_response("memories 必须是数组")
        imported = 0
        for item in memories:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            self.engine.memory.remember(
                session_id=str(item.get("session_id", "") or ""),
                persona_id=str(item.get("persona_id", "") or ""),
                node_id=str(item.get("node_id", "") or ""),
                content=str(item.get("content")),
                memory_type=str(item.get("type", "scene") or "scene"),
                related_users=[str(u) for u in item.get("related_users", []) or []],
                emotion=str(item.get("emotion", "") or ""),
                weight=float(item.get("weight", 0.5) or 0.5),
                scope=item.get("scope") or None,
                source="import",
            )
            imported += 1
        return json_response({"ok": True, "imported": imported})

    async def api_prompt(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = request.query.get("session", "") or ""
        mode = request.query.get("mode", "inject") or "inject"
        if not session_id:
            return error_response("缺少 session 参数")
        session_id = self._scope(session_id)
        if mode == "autonomous":
            text = await self.engine.preview_autonomous_prompt(session_id)
        else:
            text = await self.engine.preview_injection(session_id)
        return json_response(
            {
                "prompt": text,
                "chars": len(text),
                # 分段索引：编辑器里直接显示，避免"某一段是不是没进去"只能靠肉眼找
                "sections": prompt_section_index(text),
                # 状态槽：调试页要能看出"槽里有东西但没进提示词"（过期了）这种情形
                "state_slots": await self.engine.preview_state_slots(session_id),
                # 这一轮抽到哪几条声音样例：提示词两万字，不点出来根本找不着
                "voice_samples": await self.engine.preview_voice_samples(session_id),
            }
        )

    # ---------------- 预设：成套的世界配置 ----------------

    async def api_state_history(self):
        """数值历史：给编辑器画「她最近过得怎么样」。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = request.query.get("session", "") or ""
        if not session_id:
            return error_response("缺少 session 参数")
        session_id = self._scope(session_id)
        try:
            hours = int(request.query.get("hours", 24) or 24)
        except (TypeError, ValueError):
            hours = 24
        return json_response(await self.engine.state_history(session_id, hours=hours))

    async def api_defaults(self):
        """内置默认文案：内置动作的说明/名片文案，以及两段图片转述提示词。

        编辑器里的「恢复默认」图标都从这里取值——默认文案只有 core/defaults.py 一份。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        actions: dict[str, dict[str, str]] = {}
        for item in DEFAULT_WORLD.get("actions") or []:
            action_id = str(item.get("id") or "")
            if not action_id:
                continue
            actions[action_id] = {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "nickname_text": str(item.get("nickname_text") or ""),
            }
        return json_response(
            {
                "actions": actions,
                # 配置字段的中文说明：滑块明细和旧默认值提醒都用它，
                # 免得同一份字段名要在前端再抄一遍。
                "field_labels": dict(FIELD_LABELS),
                "captions": {
                    "look": DEFAULT_CAPTION_PROMPT,
                    "relation": DEFAULT_CAPTION_RELATION_PROMPT,
                    "forward": DEFAULT_FORWARD_PROMPT,
                },
            }
        )

    async def api_presets(self):
        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(
            {
                "presets": self.store.list_presets(),
                "active": self.store.active_preset(),
            }
        )

    async def api_preset_save(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        if not preset_id:
            return error_response("预设 id 不能为空")
        try:
            path, warnings = self.store.save_preset(
                preset_id,
                name=str(payload.get("name") or "").strip(),
                note=str(payload.get("note") or "").strip(),
            )
        except Exception as exc:
            return error_response(f"保存预设失败：{exc}")
        return json_response({"ok": True, "id": path.stem, "warnings": warnings})

    async def api_preset_new_default(self):
        """用内置的默认世界新建一份预设（不动当前配置）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip() or "default_world"
        try:
            clean, warnings = self.store.new_default_preset(
                preset_id,
                name=str(payload.get("name") or "").strip(),
                note=str(payload.get("note") or "").strip(),
            )
        except Exception as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "id": clean, "warnings": warnings})

    async def api_preset_apply(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        if not preset_id:
            return error_response("缺少预设 id")
        blocks = payload.get("blocks")
        if blocks is not None and not isinstance(blocks, list):
            return error_response("blocks 必须是数组")
        # 默认**不清状态**：切预设本来只想换世界，上下文留着更符合直觉
        clear_state = bool(payload.get("clear_state", False))
        try:
            result = self.store.apply_preset(preset_id, blocks=blocks)
        except Exception as exc:
            return error_response(f"应用预设失败：{exc}")
        warnings = list(result.get("warnings") or [])
        warnings.extend(self.engine.reload_config())
        repairs: list[str] = []
        if clear_state:
            cleared = await self.engine.clear_all_states()
        else:
            cleared = 0
            # 不清状态就得把状态修一遍：地图/动作换过之后，旧的地点与计划可能已经不存在
            repairs = await self.engine.repair_states_after_config_change()
        return json_response(
            {
                "ok": True,
                "id": preset_id,
                "blocks": list(result.get("blocks") or []),
                "cleared_sessions": cleared,
                "repairs": repairs,
                "warnings": warnings,
            }
        )

    async def api_preset_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        if not self.store.delete_preset(preset_id):
            return error_response("找不到这个预设")
        return json_response({"ok": True})

    async def api_preset_rename(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        data = self.store.read_preset(preset_id)
        if not data:
            return error_response("找不到这个预设")
        data["name"] = str(payload.get("name") or "").strip() or preset_id
        if payload.get("note") is not None:
            data["note"] = str(payload.get("note") or "")
        warnings = self.store.write_preset(preset_id, data)
        return json_response({"ok": True, "warnings": warnings})

    async def api_preset_json(self):
        """GET：读出预设原文；POST：整段写回（导入 / 直接编辑都走这里）。"""

        if request.method == "POST":
            payload = await request.json(default={}) or {}
            guard = self._guard(payload)
            if guard is not None:
                return guard
            preset_id = str(payload.get("id") or "").strip()
            if not preset_id:
                return error_response("缺少预设 id")
            raw = payload.get("payload")
            if isinstance(raw, dict):
                data = raw
            else:
                text = str(payload.get("json") or "").strip()
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError as exc:
                    return error_response(f"JSON 格式不对：{exc}")
            try:
                warnings = self.store.write_preset(preset_id, data)
            except Exception as exc:
                return error_response(f"写入预设失败：{exc}")
            return json_response({"ok": True, "id": preset_id, "warnings": warnings})
        guard = self._guard()
        if guard is not None:
            return guard
        preset_id = request.query.get("id", "") or ""
        data = self.store.read_preset(preset_id)
        if not data:
            return error_response("找不到这个预设")
        return json_response({"preset": data})

    async def api_backup(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        path = self.store.backup(str(payload.get("tag", "") or ""))
        return json_response({"ok": True, "file": path.name})

    # ---------------- 改动历史 ----------------

    async def api_history(self):
        """改动历史列表（新的在前）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(
            {
                "items": self.store.list_history(),
                "keep": HISTORY_KEEP,
            }
        )

    async def api_history_item(self):
        """一条历史：快照内容 + 当前配置 + 逐块差异（前端据此画左右对比）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        snapshot_id = request.query.get("id", "") or ""
        if not snapshot_id:
            return error_response("缺少 id")
        try:
            return json_response(self.store.read_history(snapshot_id))
        except Exception as exc:
            return error_response(str(exc))

    async def api_history_restore(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        snapshot_id = str(payload.get("id") or "").strip()
        if not snapshot_id:
            return error_response("缺少 id")
        blocks = payload.get("blocks")
        picked = (
            [str(item) for item in blocks] if isinstance(blocks, list) else None
        )
        try:
            result = self.store.restore_history(snapshot_id, blocks=picked)
        except Exception as exc:
            return error_response(f"恢复失败：{exc}")
        self.engine.reload_config()
        return json_response({"ok": True, **result})

    async def api_history_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        snapshot_id = str(payload.get("id") or "").strip()
        if not snapshot_id:
            return error_response("缺少 id")
        return json_response({"ok": self.store.delete_history(snapshot_id)})

    async def api_history_clear(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return json_response({"ok": True, "removed": self.store.clear_history()})


# ======================================================================
# 小工具
# ======================================================================


def _help_text(pronoun: str = "她") -> str:
    """指令帮助文案。pronoun 由全局设置里的性别决定（她/他/ta）。"""

    return (
        "虚拟世界指令：\n"
        f"· /vw status            {pronoun}现在在哪、在做什么、心情如何\n"
        f"· /vw recall <话题>     让{pronoun}回忆某个话题\n"
        f"· /vw memory            看看{pronoun}记得你什么\n"
        f"· /vw forget me [话题]  让{pronoun}忘记关于你的记忆\n"
        f"· /vw event <一句话>   给她安排一件会发生的事（看她怎么处理）\n"
        f"· /vw event list       看最近的事件线索\n"
        f"· /vw ability          看{pronoun}的本事（能力值，管理员）\n"
        f"· /vw thread           看{pronoun}手头没完的事（管理员）\n"
        "· /vw schedule          查看日程\n"
        "· /vw schedule run <id> 立刻跑一遍某条日程（管理员）\n"
        "· /vw map               查看地图\n"
        "· /vw nickname lock|unlock|set <文本>|reset   群名片控制（管理员）\n"
        "· /vw session list|add [ID]|remove <ID>|enable|disable <ID>   会话白名单（管理员）\n"
        "· /vw reload            热加载配置（管理员）\n"
        "· /vw reset [会话]      重置某个会话的状态（管理员）\n"
        "· /vw restore-default   恢复默认配置（管理员）\n"
        "· /vw debug on|off|state|plan|stop|memories|session|tools|prompt|autoprompt|tick|decide   调试（管理员）"
    )


def _cfg_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "开启", "是")
    return bool(value)


def _cfg_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _command_args(event: AstrMessageEvent) -> list[str]:
    text = (event.get_message_str() or "").strip()
    if not text:
        return []
    parts = text.split()
    if parts and parts[0].lstrip("/").lower() in ("vw", "世界", "virtualworld"):
        parts = parts[1:]
    return parts


def _event_persona_id(event: AstrMessageEvent) -> str:
    for attr in ("persona_id",):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value:
            return value
    session = getattr(event, "session", None)
    value = getattr(session, "persona_id", None) if session is not None else None
    return value if isinstance(value, str) else ""


def _render_status(snapshot: dict[str, Any], pronoun: str = "她") -> str:
    values = snapshot.get("values", {})
    action = snapshot.get("current_action") or {}
    action_text = action.get("desc") or action.get("type") or "没在做什么"
    travel = snapshot.get("travel") or []
    lines = [
        f"{pronoun}现在在【{snapshot.get('node_name') or snapshot.get('node_id')}】",
        f"状态：{snapshot.get('state')}　心情：{snapshot.get('mood')}",
        f"正在做：{action_text}",
        "数值："
        f"精力 {values.get('energy', 0):.2f}　孤独 {values.get('loneliness', 0):.2f}　"
        f"好奇 {values.get('curiosity', 0):.2f}　心潮 {values.get('affect', 0):.2f}　"
        f"无聊 {values.get('boredom', 0):.2f}",
        f"世界时间：{snapshot.get('world_time')} tick"
        f"（1 tick = {int(snapshot.get('tick_seconds', 60))} 秒）",
    ]
    if travel:
        lines.append(
            "从这里出发："
            + "、".join(f"{item['name']} {item['ticks']} tick" for item in travel)
        )
    if snapshot.get("unanswered_count"):
        lines.append(f"连续无人回应：{snapshot['unanswered_count']} 次")
    return "\n".join(lines)





