"""宿主能力端口（seam）。

引擎只依赖这里的 Protocol，AstrBot 的真实实现放在 main.py 的适配器里，
测试用 tests/stub_ports.py 里的内存实现。这样引擎逻辑可以在没有 AstrBot 的环境下被验证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolInfo:
    """工具的元信息（给提示词和 WebUI 用）。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    source: str = "plugin"
    """来源：``official`` = AstrBot 自带的内置工具，``plugin`` = 插件 / MCP 注册的工具。"""

    plugin: str = ""
    """提供这个工具的插件名（拿得到时填，编辑器的工具列表按它分组）。"""


@dataclass
class LLMReply:
    """LLM 返回结果。"""

    text: str = ""
    ok: bool = True
    error: str = ""


@dataclass
class ToolCallResult:
    """一次工具调用的结果。

    ``ok=False`` 时 ``error`` 说明原因（没找到工具 / 没接上 handler / 抛异常 / 返回空），
    调用方据此决定是"把结果讲给大家听"还是"记一条失败日志、别硬编"。
    """

    ok: bool = False
    text: str = ""
    error: str = ""
    tool: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    """实际传给工具的参数（已按工具 schema 过滤），日志里记这个才准。"""

    image_urls: list[str] = field(default_factory=list)
    """工具 / 指令一起返回的图片（http、file://、base64:// 都可能）。

    转述模型可用时会在适配层就换成文字（不会出现在这里）；只有"直接交给多模态主模型"
    这条路才会把地址带上来。
    """


@dataclass
class CardResult:
    """一次群名片操作的结果。``reason`` 说明为什么没成功，会进事件日志。"""

    ok: bool = False
    reason: str = ""
    card: str = ""


@dataclass
class PokeResult:
    """一次「戳一戳」的结果。

    协议端不一定支持（支持的只有 QQ 系），不支持时 ``reason`` 会写清原因，
    调用方据此退化成一句文案，而不是让这次动作凭空消失。
    """

    ok: bool = False
    reason: str = ""


@runtime_checkable
class LLMPort(Protocol):
    """单次 LLM 调用的入口。"""

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
    ) -> LLMReply: ...


@runtime_checkable
class MessagePort(Protocol):
    """向会话发送消息。"""

    async def send_text(self, session_id: str, messages: list[str]) -> bool: ...

    async def set_group_card(self, session_id: str, card: str) -> CardResult: ...

    async def fetch_group_card(self, session_id: str) -> str: ...
    """读一下她当前在群里的名片（拿不到就返回空串）。"""

    async def poke(self, session_id: str, user_id: str) -> PokeResult: ...
    """戳一戳某个群友（QQ 系平台独有，失败时在 ``reason`` 里说明）。"""


@runtime_checkable
class CommandPort(Protocol):
    """把别的插件的指令转发出去（「指令触发」型动作用）。"""

    async def trigger(
        self, session_id: str, command: str, *, event: Any = None
    ) -> ToolCallResult: ...


@runtime_checkable
class ToolPort(Protocol):
    """查询与调用 AstrBot 工具。"""

    def list_tools(self) -> list[ToolInfo]: ...

    async def call_tool(
        self, name: str, params: dict[str, Any], session_id: str = ""
    ) -> ToolCallResult: ...


@runtime_checkable
class PersonaPort(Protocol):
    """读取会话当前人格。"""

    async def get_persona_text(self, session_id: str) -> str: ...


@runtime_checkable
class ClockPort(Protocol):
    """时间来源，便于测试注入固定时间。"""

    def now(self) -> float: ...

    def now_struct(self) -> Any: ...
