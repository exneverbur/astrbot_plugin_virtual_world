"""AstrBot 虚拟世界插件（Star 入口）。

两条互斥的路径：
- **注入模式**：用户 @Bot 时，AstrBot 主人格正常回复，本插件只把「她此刻在哪、什么状态、
  想起什么」追加进 system_prompt，并顺带按区域裁剪可用工具（`on_llm_request`）。
- **接管模式**：自主行为（日程、发呆、搜索、主动搭话）由插件自己调 LLM 并用 JSON 约束输出，
  自己发送消息。本插件自己发起的请求会带 `SELF_INITIATED_FLAG`，绝不重复注入。

配置热加载、Web 编辑器、状态与记忆都在 core/ 里实现，本文件只做 AstrBot 适配。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import secrets
import time
import types
from collections import OrderedDict
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request

from .core.config_store import ConfigStore
from .core.db import AsyncDatabase
from .core.engine import SELF_INITIATED_FLAG, MessageContext, VirtualWorldEngine
from .core.models import normalize_edge_keys, normalize_legacy_keys, pronoun_for
from .core.nickname import compute_nickname
from .core.ports import CardResult, LLMReply, ToolCallResult, ToolInfo
from .core.prompt import prompt_section_index
from .core.timeline import build_timeline

PLUGIN_NAME = "astrbot_plugin_virtual_world"
# 钩子优先级：比默认 0 低，让其他插件先写完 system_prompt / 先决定要不要接管。
# AstrBot 按 priority 从高到低执行钩子，并且一旦某个钩子 stop 了事件，后面的就不再执行。
LLM_HOOK_PRIORITY = -100
# 睡觉门禁要抢在「意图路由」这类消息级插件前面：它们通常注册在 100 左右，
# 取值比它们高才会先执行；一旦这里 stop_event()，后面的处理器都不会跑。
SLEEP_GUARD_PRIORITY = 200
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


def _is_command(text: str) -> bool:
    """看起来是一条指令（/xxx、！xxx 这类）。指令永远不该被她睡觉挡住。"""

    stripped = (text or "").lstrip()
    return bool(stripped) and stripped[0] in "/!！.。"


def _image_sources(event: Any) -> list[str]:
    """挑出这条消息里的图片，返回可以喂给多模态模型的地址。"""

    message_obj = getattr(event, "message_obj", None)
    sources: list[str] = []
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ != "Image":
            continue
        source = str(
            getattr(component, "url", "") or getattr(component, "file", "") or ""
        ).strip()
        if source:
            sources.append(source)
    return sources


def _is_forwarded(event: Any) -> bool:
    """这条消息是不是「合并转发」。"""

    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ in ("Forward", "Node", "Nodes"):
            return True
    text = str(getattr(message_obj, "message_str", "") or "")
    return "[合并转发]" in text or "[聊天记录]" in text


def _quoted_text(event: Any) -> str:
    """这条消息引用了谁说的什么（引用消息的文本）。"""

    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ != "Reply":
            continue
        text = " ".join(str(getattr(component, "message_str", "") or "").split())
        if not text:
            continue
        who = str(getattr(component, "sender_nickname", "") or "").strip()
        return f"{who}：{text}" if who else text
    return ""


class AstrBotLLM:
    """LLMPort 实现。"""

    def __init__(self, plugin: "VirtualWorldPlugin", provider_id: str = "") -> None:
        self.plugin = plugin
        self.provider_id = provider_id

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
        text = getattr(response, "completion_text", "") or ""
        return LLMReply(text=text, ok=True)


CAPTION_SYSTEM_PROMPT = (
    "你要帮一个群聊机器人看懂图片。只看图片本身是不够的——还要说清这张图和当前话题的关系，"
    "这样机器人才知道该怎么接话。\n"
    "输出一行中文，格式固定为：画面描述｜与话题的关系：…\n"
    "画面描述：画面里有什么、在做什么、有没有值得注意的文字或表情，40 字以内。\n"
    "与话题的关系：这张图在回应什么、和正在聊的事有什么关联；确实看不出关系就写「看不出直接关系」。\n"
    "不要客套、不要分点、不要写「这张图片」、不要编造看不到的内容。"
)


class AstrBotVision:
    """把图片转成一段文字，让不带视觉能力的模型也能"看到"。"""

    def __init__(self, plugin: "VirtualWorldPlugin", provider_id: str = "") -> None:
        self.plugin = plugin
        self.provider_id = provider_id
        self._cache: dict[str, str] = {}
        self.last_error: str = ""
        """最近一次转述失败的原因（写进日志，方便排查"模型看不见图片"）。"""

    @property
    def enabled(self) -> bool:
        return bool(self.provider_id.strip())

    async def describe(
        self,
        image_sources: list[str],
        *,
        question: str = "",
        quoted: str = "",
        context_lines: list[str] | None = None,
    ) -> list[str]:
        """把图片逐个转述成文字。失败或未配置时返回空串（调用方自己降级）。"""

        scene = self._scene_text(question=question, quoted=quoted, context_lines=context_lines)
        results: list[str] = []
        for source in image_sources:
            # 同一张图在不同话题下要说的话不一样，所以缓存键带上当时的对话背景
            cache_key = f"{source}|{scene}"
            if cache_key in self._cache:
                results.append(self._cache[cache_key])
                continue
            caption = ""
            if self.enabled and source:
                caption = await self._describe_one(source, scene)
            self._cache[cache_key] = caption
            if len(self._cache) > 200:
                self._cache.clear()
            results.append(caption)
        return results

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
                system_prompt=CAPTION_SYSTEM_PROMPT,
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


class AstrBotCommands:
    """把「指令触发」型动作转成真正的指令，交给别的插件执行。

    寻找方式与 AstrBot 的指令分发一致：从 handler 注册表里按指令名（含别名）匹配，
    借用这个会话最近一条真实事件，把消息文本换成指令本身，再调用它的处理器。
    处理器 yield 出来的文本会被收集起来交回给大模型。
    """

    def __init__(self, plugin: "VirtualWorldPlugin") -> None:
        self.plugin = plugin

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
            if inspect.isasyncgen(result):
                async for item in result:
                    texts.append(_tool_result_text(item))
            elif inspect.isawaitable(result):
                texts.append(_tool_result_text(await result))
            else:
                texts.append(_tool_result_text(result))
        except Exception as exc:
            return ToolCallResult(ok=False, error=f"{type(exc).__name__}: {exc}", tool=word)
        finally:
            restore()
        body = "\n".join(part for part in texts if part).strip()
        if not body:
            body = f"（指令「{word}」执行了，但没有返回文字）"
        return ToolCallResult(ok=True, text=body, tool=word)

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
        """按处理器签名解析参数（和 AstrBot 指令分发同一套转换）。"""

        signature = inspect.signature(record.handler)
        param_type: dict[str, Any] = {}
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
        for text in texts:
            try:
                result = await self.plugin.context.send_message(
                    session_id, MessageChain(chain=[Plain(text=str(text))])
                )
                ok = bool(result) or ok
            except Exception as exc:
                # 失败就失败：不重试、不补发，后面的几条也不再试（平台已经不通了）
                self.mark_failed(session_id, exc)
                break
        if ok:
            self.mark_sent(session_id)
        return ok

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


class AstrBotTools:
    """ToolPort 实现。"""

    def __init__(self, plugin: "VirtualWorldPlugin") -> None:
        self.plugin = plugin

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
            result = invoke(event, call_params, self.plugin)
            if inspect.isasyncgen(result):
                last: Any = None
                async for item in result:
                    last = item
                text = _tool_result_text(last)
            elif inspect.isawaitable(result):
                text = _tool_result_text(await result)
            else:
                text = _tool_result_text(result)
        except Exception as exc:
            self.plugin.logger.warning(f"[virtual_world] 工具 {name} 调用失败：{exc}")
            return ToolCallResult(
                ok=False, error=f"调用出错：{exc}", tool=name, params=call_params
            )
        text = (text or "").strip()
        if not text:
            return ToolCallResult(
                ok=False, error="工具返回了空结果", tool=name, params=call_params
            )
        return ToolCallResult(ok=True, text=text, tool=name, params=call_params)

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

    async def get_persona_text(self, session_id: str) -> str:
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
    "Codex",
    "给 Bot 一个私有空间、动作、日程、场景记忆和工具能力，让 ta 像住在群里一样生活。",
    "v1.2.1",
)
class VirtualWorldPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config

        self.enabled = _cfg_bool(config.get("enabled"), True)
        self.web_enabled = _cfg_bool(config.get("web_enabled"), True)
        self.llm_provider_id = str(config.get("llm_provider_id") or "").strip()
        self.helper_provider_id = str(config.get("helper_provider_id") or "").strip()
        self.vision_provider_id = str(config.get("vision_provider_id") or "").strip()
        self.context_provider_id = str(config.get("context_provider_id") or "").strip()
        # 内容生成模型：只在编辑器里"批量生成动作 / 地点"时用，留空回落到主模型
        self.creator_provider_id = str(config.get("creator_provider_id") or "").strip()
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
        self._tick_task: asyncio.Task | None = None
        self._self_initiated_depth = 0
        # 发送方持有「失败冷却」，所以自己留一份（回退响应路径也要用它判断）
        self.messenger = AstrBotMessenger(self)

        self.engine = VirtualWorldEngine(
            store=self.store,
            db=self.db,
            llm=AstrBotLLM(self),
            helper_llm=AstrBotLLM(self, self.helper_provider_id),
            context_llm=AstrBotLLM(self, self.context_provider_id),
            creator_llm=AstrBotLLM(self, self.creator_provider_id),
            messenger=self.messenger,
            tools=AstrBotTools(self),
            commands=AstrBotCommands(self),
            persona=AstrBotPersona(self),
            clock=None,
            tick_seconds=float(self.tick_interval),
            decider_interval=float(self.decider_interval),
            debug=self.debug,
            logger=self.logger,
        )
        self.vision = AstrBotVision(self, self.vision_provider_id)

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

    async def _tick_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.tick_interval)
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

    def last_event_any(self) -> AstrMessageEvent | None:
        """最近一次见过的消息事件（拿不到会话 id 时给工具调用兜底）。"""

        if not self._last_events:
            return None
        return next(reversed(self._last_events.values()))

    def _pronoun(self) -> str:
        """由全局设置里的性别决定称呼：她 / 他 / ta。"""

        return pronoun_for(self.engine.world.gender)

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
            "loneliness": round(state.loneliness, 4),
            "energy": round(state.energy, 4),
            "curiosity": round(state.curiosity, 4),
            "boredom": round(state.boredom, 4),
            "willingness": round(self.engine.willingness(state), 4),
            "mood": state.mood,
            "state": state.state,
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

    async def _annotate_message(self, event: AstrMessageEvent, text: str) -> str:
        """给消息补上模型看不到的部分：图片里有什么、这是不是一条转发。"""

        notes: list[str] = []
        if _is_forwarded(event):
            notes.append("这是一条转发的聊天记录，不是当前群里正在说的话")
        sources = _image_sources(event)
        if sources:
            captions: list[str] = []
            if getattr(self, "vision", None) is not None and self.vision.enabled:
                try:
                    captions = await self.vision.describe(
                        sources,
                        question=text,
                        quoted=_quoted_text(event),
                        context_lines=await self._recent_chat_lines(event),
                    )
                except Exception as exc:
                    self.logger.debug(f"[virtual_world] 图片转述异常：{exc}")
                    captions = []
            described = [
                f"图片{index + 1}：{caption}"
                for index, caption in enumerate(captions or [])
                if caption
            ]
            session_id = event.unified_msg_origin
            if described:
                # 转述结果同时进日志与调试输出：成功就显示描述
                await self.engine.note_vision(
                    session_id,
                    ok=True,
                    images=len(sources),
                    detail="；".join(described),
                )
            else:
                reason = "没配图片转述模型"
                if getattr(self, "vision", None) is not None and self.vision.enabled:
                    reason = str(getattr(self.vision, "last_error", "") or "转述没有返回内容")
                # 失败也记一条：失败原因是排查"模型看不见图片"的唯一线索
                await self.engine.note_vision(
                    session_id, ok=False, images=len(sources), detail=reason
                )
            notes.append(
                "；".join(described) if described else "对方发了一张图片，看不清内容"
            )
        if not notes:
            return text
        return f"{text}\n［{'；'.join(notes)}］".strip()

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
        self._self_initiated_depth += 1
        return self._self_initiated_depth

    def reset_self_initiated(self, _token: int = 0) -> None:
        self._self_initiated_depth = max(0, self._self_initiated_depth - 1)

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
        if self._self_initiated_depth > 0:
            return
        if event.get_extra(SELF_INITIATED_FLAG):
            return
        if event.is_stopped():
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
        user_text = await self._annotate_message(event, user_text)
        image_urls: list[str] = []
        if not (getattr(self, "vision", None) is not None and self.vision.enabled):
            # 没配转述模型时，直接把图片交给多模态主模型：
            # 「自上次回复以来收到的图片」+ 这条消息自己的图，按配置的上限截断。
            pending = await self.engine.take_pending_images(session_id)
            current = _image_sources(event)
            limit = max(1, int(self.engine.world.context.image_max))
            merged = list(dict.fromkeys([*pending, *current]))[-limit:]
            if merged:
                image_urls = merged
                await self.engine.note_vision(
                    session_id,
                    ok=True,
                    images=len(merged),
                    detail=f"没配转述模型，直接把 {len(merged)} 张图交给多模态主模型",
                )
        ctx = MessageContext(
            session_id=session_id,
            user_id=event.get_sender_id(),
            user_name=event.get_sender_name(),
            text=user_text,
            is_wake=bool(event.is_wake_up()),
            is_mentioned=_is_at_bot(event) or bool(event.is_private_chat()),
            is_private=bool(event.is_private_chat()),
            persona_id=_event_persona_id(event),
            # 其他插件（上下文理解、图片转文字、记忆…）写进 system_prompt 的内容原样带过去
            other_context=(getattr(req, "system_prompt", "") or "").strip(),
            image_urls=image_urls,
        )

        # 睡觉时的门禁：没被明确叫醒就只回固定文案（或保持安静），
        # 不调大模型、不执行动作，也不交给主人格替她熬夜聊天。
        sleep_reply = await self.engine.sleep_gate(ctx)
        if sleep_reply is not None:
            echo = self.engine.take_pending_echo(session_id)
            if sleep_reply.messages or echo:
                await self._send_reply(event, list(sleep_reply.messages) + echo)
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
            outcome = await self.engine.handle_reply(
                ctx, history=list(getattr(req, "contexts", None) or [])
            )
            echo = list(getattr(outcome, "debug_messages", []) or [])
            if outcome.ok and outcome.messages:
                # 先让别的插件的回复钩子过一遍（它们可能在回复里读写标记）
                bridged = await self._bridge_reply_hooks(event, list(outcome.messages))
                # 再按换行拆段：一段一条消息，别把好几句挤成一坨
                await self._send_reply(event, self.split_messages(bridged) + echo)
                if self.debug:
                    self.logger.info(
                        f"[virtual_world] 接管回复 {session_id}："
                        f"reasoning={outcome.reasoning} messages={outcome.messages}"
                    )
                event.stop_event()
                return
            # 接管没成功、会交回主人格：调试信息照样发出去，不然就看不到了
            if echo:
                await self._send_reply(event, echo)
            self._log_takeover_fallback(session_id, outcome)

        # ---- 注入模式（也是接管失败后的兜底）----
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

    async def _send_reply(self, event: AstrMessageEvent, messages: list[str]) -> None:
        """把接管产生的消息发回当前会话（逐条发送 = 天然分段回复）。"""

        session_id = event.unified_msg_origin
        for text in messages:
            content = str(text).strip()
            if not content:
                continue
            if self.messenger.blocked(session_id):
                # 刚发送失败过：不再往下试，免得把队列越堆越长
                break
            try:
                await event.send(MessageChain([Plain(text=content)]))
                self.messenger.mark_sent(session_id)
            except Exception as exc:
                # 失败就失败：以前这里会立刻换一条通道再发一次，遇到"超时但其实发出去了"
                # 的情况就会重复刷屏，所以只保留一次尝试。
                self.messenger.mark_failed(session_id, exc)

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

    async def _bridge_reply_hooks(
        self, event: AstrMessageEvent, messages: list[str]
    ) -> list[str]:
        """让别的插件也能"看到"这次接管的回复。

        本插件接管时是自己调模型、自己发送的，AstrBot 的回复钩子轮不到跑，
        那些靠在回复里解析标记的插件（好感度、统计、改写语气之类）就永远收不到内容。
        这里把回复补送进同一条钩子链：先过 ``on_llm_response``，
        再过 ``on_decorating_result``（发送前的最后一道），拿回它们改过的文本再发。
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
        try:
            response = LLMResponse(role="assistant", completion_text=joined)
            await call_event_hook(event, EventType.OnLLMResponseEvent, response)
            edited = str(getattr(response, "completion_text", "") or "")
            if edited and edited != joined:
                lines = [line.strip() for line in edited.splitlines() if line.strip()]
                messages = lines or [edited]
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

    def _log_takeover_fallback(self, session_id: str, outcome) -> None:
        if not self.debug:
            return
        self.logger.info(
            f"[virtual_world] 接管未生效（{outcome.error or '没有对外输出'}），"
            f"改用注入模式 session={session_id}"
        )

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
        if self._self_initiated_depth > 0:
            return
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        text = event.get_message_str() or ""
        if _is_command(text):
            return  # 指令照常走（/vw status、/vw 叫醒 这些要能用）
        ctx = MessageContext(
            session_id=session_id,
            user_id=event.get_sender_id(),
            user_name=event.get_sender_name(),
            text=text,
            is_wake=bool(event.is_wake_up()),
            is_mentioned=_is_at_bot(event) or bool(event.is_private_chat()),
            is_private=bool(event.is_private_chat()),
            # 记下这条消息带的图片，等下次回复时一起给多模态主模型
            image_urls=_image_sources(event),
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
        if not self.enabled:
            return
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        self._remember_event(event)
        text = await self._annotate_message(event, event.get_message_str() or "")
        await self.engine.note_presence(
            MessageContext(
                session_id=session_id,
                user_id=event.get_sender_id(),
                user_name=event.get_sender_name(),
                text=text,
                is_wake=bool(event.is_wake_up()),
                is_private=bool(event.is_private_chat()),
            )
        )

    # ================= 用户 / 管理员命令 =================

    @filter.command("vw", alias={"世界", "virtualworld"})
    async def cmd_vw(self, event: AstrMessageEvent):
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
            if not event.is_admin():
                yield event.plain_result("只有管理员可以重载配置。")
                return
            warnings = self.engine.reload_config()
            message = "配置已热加载。"
            if warnings:
                message += "\n提醒：" + "；".join(warnings)
            yield event.plain_result(message)
            return

        if action in ("reset", "重置"):
            if not event.is_admin():
                yield event.plain_result("只有管理员可以重置会话状态。")
                return
            target = rest[0] if rest else event.unified_msg_origin
            await self.db.call("delete_state", target)
            yield event.plain_result(f"已重置 {target} 的世界状态（记忆保留）。")
            return

        if action in ("restore-default", "恢复默认"):
            if not event.is_admin():
                yield event.plain_result("只有管理员可以恢复默认配置。")
                return
            restored = self.store.restore_default("all")
            self.engine.reload_config()
            yield event.plain_result("已恢复默认配置：" + "、".join(restored))
            return

        yield event.plain_result(f"未知指令：{action}\n\n{_help_text(self._pronoun())}")

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
        if not event.is_admin():
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
        if not event.is_admin():
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
        if not event.is_admin():
            return "只有管理员可以管理会话白名单。"
        sub = rest[0].lower() if rest else "list"
        if sub in ("list", "列表"):
            sessions = self.engine.sessions.sessions if self.engine.sessions else []
            if not sessions:
                return "白名单为空。"
            return "会话白名单：\n" + "\n".join(
                f"{'✅' if item.enabled else '⛔'} {item.session_id} ({item.type})"
                + (f" - {item.note}" if item.note else "")
                for item in sessions
            )
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
        register(f"/{p}/backup", self.api_backup, ["POST"], "备份配置")
        register(f"/{p}/presets", self.api_presets, ["GET"], "预设列表")
        register(f"/{p}/presets/save", self.api_preset_save, ["POST"], "把当前配置存成预设")
        register(f"/{p}/presets/apply", self.api_preset_apply, ["POST"], "应用预设")
        register(f"/{p}/presets/delete", self.api_preset_delete, ["POST"], "删除预设")
        register(f"/{p}/presets/rename", self.api_preset_rename, ["POST"], "重命名预设")
        register(f"/{p}/presets/json", self.api_preset_json, ["GET", "POST"], "读写预设 JSON")

    def _guard(self, payload: dict[str, Any] | None = None) -> Any:
        """统一鉴权：返回错误响应或 None。"""

        token = request.query.get("token", "") or ""
        if not token and isinstance(payload, dict):
            token = str(payload.get("token", "") or "")
        if self.auth.check(token):
            return None
        return error_response("需要先登录编辑器", status_code=401)

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
            }
        )

    async def api_put_world(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        world = payload.get("world")
        if not isinstance(world, dict):
            return error_response("world 必须是对象")
        warnings = self.store.save_world(world)
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
        warnings = self.store.save_schedules(schedules)
        self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings})

    async def api_put_sessions(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            return error_response("sessions 必须是对象")
        warnings = self.store.save_sessions(sessions)
        self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings})

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
        session_id = request.query.get("session", "") or ""
        if not session_id:
            return error_response("缺少 session 参数")
        snapshot = await self.engine.snapshot(session_id)
        return json_response(snapshot)

    async def api_state_action(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = str(payload.get("session", "") or "")
        action = str(payload.get("action", "") or "")
        if not session_id or not action:
            return error_response("缺少 session 或 action")
        if action == "wake":
            was = await self.engine.wake_up(session_id)
            return json_response({"ok": True, "was_sleeping": was})
        if action == "interrupt":
            done = await self.engine.interrupt(session_id, force=True)
            return json_response({"ok": True, "interrupted": done})
        if action == "tick":
            outcomes = await self.engine.tick()
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
            # 手动触发：绕过评估间隔，但仍然尊重"正在忙 / 在睡觉"这些硬条件
            outcome = await self.engine.maybe_decide(
                session_id, force=bool(payload.get("force"))
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
        session_id = request.query.get("session", "") or ""
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
        session_id = request.query.get("session", "") or ""
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
        session_id = request.query.get("session", "") or None
        node_id = request.query.get("node_id", "") or None
        memory_type = request.query.get("type", "") or None
        scope = request.query.get("scope", "") or None
        keyword = request.query.get("q", "") or ""
        rows = await self.db.call(
            "query_memories",
            session_id=session_id,
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
        session_id = str(payload.get("session_id", "") or "")
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
        session_id = str(payload.get("session", "") or "")
        removed = await self.db.call("clear_memories", session_id or None)
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
        session_id = str(payload.get("session", "") or "")
        removed = await self.db.call("clear_events", session_id or None)
        return json_response({"ok": True, "removed": removed})

    async def api_memory_stats(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = request.query.get("session", "") or None
        stats = await self.db.call("memory_stats", session_id)
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
            }
        )

    # ---------------- 预设：成套的世界配置 ----------------

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
            path = self.store.save_preset(
                preset_id,
                name=str(payload.get("name") or "").strip(),
                note=str(payload.get("note") or "").strip(),
            )
        except Exception as exc:
            return error_response(f"保存预设失败：{exc}")
        self.store.set_active_preset(path.stem)
        return json_response({"ok": True, "id": path.stem})

    async def api_preset_apply(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        if not preset_id:
            return error_response("缺少预设 id")
        clear_state = bool(payload.get("clear_state", True))
        try:
            result = self.store.apply_preset(preset_id)
        except Exception as exc:
            return error_response(f"应用预设失败：{exc}")
        warnings = list(result.get("warnings") or [])
        warnings.extend(self.engine.reload_config())
        cleared = await self.engine.clear_all_states() if clear_state else 0
        return json_response(
            {
                "ok": True,
                "id": preset_id,
                "cleared_sessions": cleared,
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
        "· /vw schedule          查看日程\n"
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





