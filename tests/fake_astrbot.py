"""最小 AstrBot 桩模块，用于在没有 AstrBot 的环境里验证 main.py 能否被正确导入与实例化。

只实现插件真正用到的接口；行为尽量简单（装饰器直接返回原函数），
目的是抓出拼写错误、导入错误和注册顺序问题，而不是模拟 AstrBot 的运行时。
"""

from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass, field
from typing import Any


class AstrBotConfig(dict):
    """AstrBot 插件配置对象（就是个 dict-like）。"""


class _Manager:
    def __init__(self) -> None:
        self.func_list: list[Any] = []


@dataclass
class _MessageChain:
    chain: list[Any] = field(default_factory=list)


class _AstrMessageEvent:
    def __init__(self) -> None:
        self.unified_msg_origin = "aiocqhttp:GroupMessage:1"
        self._extra: dict[str, Any] = {}
        self.role = "member"
        self.bot = None
        self.sent: list[Any] = []
        self._stopped = False

    def get_message_str(self) -> str:
        return "/vw status"

    def get_sender_id(self) -> str:
        return "42"

    def get_self_id(self) -> str:
        return "1"

    def get_sender_name(self) -> str:
        return "tester"

    def get_group_id(self) -> str:
        return "1"

    def is_wake_up(self) -> bool:
        return True

    def is_private_chat(self) -> bool:
        return False

    def is_admin(self) -> bool:
        return True

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._extra.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._extra[key] = value

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, chain) -> None:
        self.sent.append(chain)

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped


class _EventMessageType:
    GROUP_MESSAGE = 1
    PRIVATE_MESSAGE = 2
    OTHER_MESSAGE = 4
    ALL = 7


class _Filter:
    """装饰器集合，全部返回原函数。"""

    EventMessageType = _EventMessageType

    @staticmethod
    def _decorator(*_args, **_kwargs):
        def wrapper(func):
            return func

        return wrapper

    on_llm_request = _decorator
    on_llm_response = _decorator
    after_message_sent = _decorator
    event_message_type = _decorator
    command = _decorator
    command_group = _decorator
    permission_type = _decorator
    regex = _decorator
    llm_tool = _decorator


class _RequestProxy:
    """`astrbot.api.web.request` 的桩。"""

    class _Query:
        def __init__(self, data: dict[str, Any] | None = None) -> None:
            self._data = data or {}

        def get(self, key: str, default: Any = None, type=None):  # noqa: A002
            value = self._data.get(key, default)
            if type is not None and value is not None:
                try:
                    return type(value)
                except (TypeError, ValueError):
                    return default
            return value

        def getlist(self, key: str) -> list[Any]:
            value = self._data.get(key)
            return list(value) if isinstance(value, list) else []

    def __init__(self) -> None:
        self.query = self._Query()
        self.username = "tester"
        self.plugin_name = "astrbot_plugin_virtual_world"
        self._json: dict[str, Any] = {}

    async def json(self, default: Any = None) -> Any:
        return self._json if self._json else default


def install() -> None:
    """把桩模块注册进 sys.modules（幂等）。"""

    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")
    web = types.ModuleType("astrbot.api.web")

    api.AstrBotConfig = AstrBotConfig
    api.logger = logging.getLogger("astrbot")
    api.__all__ = ["AstrBotConfig", "logger"]

    event.AstrMessageEvent = _AstrMessageEvent
    event.MessageChain = _MessageChain
    event.filter = _Filter

    components.Plain = lambda text: {"type": "plain", "text": text}
    components.At = lambda qq: {"type": "at", "qq": qq}
    components.Reply = lambda id=None: {"type": "reply", "id": id}
    components.Image = lambda file=None: {"type": "image", "file": file}
    components.Face = lambda id=None: {"type": "face", "id": id}
    components.Poke = lambda id=None, **_: {"type": "poke", "id": id}

    class _Context:
        def __init__(self) -> None:
            self.registered_web_apis: list[tuple] = []
            self.persona_manager = None
            self._tools = _Manager()
            self.sent_chains: list[Any] = []

        def register_web_api(self, route, handler, methods, desc) -> None:
            self.registered_web_apis.append((route, handler, methods, desc))

        def get_llm_tool_manager(self):
            return self._tools

        async def get_current_chat_provider_id(self, umo: str) -> str:
            return "fake-provider"

        async def llm_generate(self, **_kwargs):
            class _Response:
                completion_text = '{"actions":[]}'

            return _Response()

        async def send_message(self, session, chain) -> bool:
            self.sent_chains.append(chain)
            return True

    class _Star:
        def __init__(self, context=None, config=None) -> None:
            self.context = context
            self.config = config
            self.logger = logging.getLogger("astrbot")

        async def initialize(self) -> None: ...

        async def terminate(self) -> None: ...

    star.Context = _Context
    star.Star = _Star

    def register(name, author, desc, version):
        def wrapper(cls):
            cls.registered_name = name
            cls.registered_version = version
            return cls

        return wrapper

    star.register = register

    def json_response(data=None, status_code: int = 200, headers=None):
        return {"status_code": status_code, "data": data}

    def error_response(message, status_code: int = 400, data=None, headers=None):
        return {"status_code": status_code, "message": message}

    web.json_response = json_response
    web.error_response = error_response
    web.file_response = lambda *args, **kwargs: {}
    web.stream_response = lambda *args, **kwargs: {}
    web.request = _RequestProxy()

    astrbot.api = api
    astrbot.logger = api.logger

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.message_components"] = components
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.web"] = web
