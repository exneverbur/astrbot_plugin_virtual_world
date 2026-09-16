"""测试替身：用内存实现 core/ports.py 里的 Protocol。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.ports import CardResult, LLMReply, PokeResult, ToolInfo, ToolCallResult


class StubClock:
    """可注入的时间。"""

    def __init__(self, now: float | None = None, struct: datetime | None = None) -> None:
        self._now = now if now is not None else 1_700_000_000.0
        self._struct = struct

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def set_struct(self, struct: datetime) -> None:
        self._struct = struct

    def now_struct(self) -> Any:
        return self._struct if self._struct is not None else datetime.fromtimestamp(self._now)


class StubLLM:
    """按顺序返回预设文本的 LLM。"""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []
        self.default_reply = ""

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
        self.calls.append(
            {
                "session_id": session_id,
                "system_prompt": system_prompt,
                "prompt": prompt,
                "contexts": list(contexts or []),
                "image_urls": list(image_urls or []),
            }
        )
        text = self.replies.pop(0) if self.replies else self.default_reply
        return LLMReply(text=text, ok=True)


class StubMessenger:
    """记录所有对外发送。"""

    def __init__(self, *, card_result: bool = True, card: str = "小鲸鱼") -> None:
        self.sent: list[tuple[str, list[str]]] = []
        self.cards: list[tuple[str, str]] = []
        self.pokes: list[tuple[str, str]] = []
        self.card_result = card_result
        self.card = card
        self.poke_result = True

    async def send_text(self, session_id: str, messages: list[str]) -> bool:
        self.sent.append((session_id, list(messages)))
        return True

    async def set_group_card(self, session_id: str, card: str) -> CardResult:
        self.cards.append((session_id, card))
        if self.card_result:
            return CardResult(ok=True, card=card)
        return CardResult(ok=False, reason="测试里指定失败", card=card)

    async def fetch_group_card(self, session_id: str) -> str:
        return self.card

    async def poke(self, session_id: str, user_id: str) -> PokeResult:
        self.pokes.append((session_id, str(user_id)))
        if self.poke_result is False:
            return PokeResult(False, "测试里指定戳不动")
        return PokeResult(True)

    @property
    def flat_messages(self) -> list[str]:
        return [message for _session, group in self.sent for message in group]


class StubTools:
    """假的工具集合。"""

    def __init__(
        self,
        tools: dict[str, str] | None = None,
        results: dict[str, str] | None = None,
        schemas: dict[str, dict] | None = None,
    ) -> None:
        self._tools = tools if tools is not None else {"web_search": "搜索网页"}
        self.results = results or {}
        self.failures: dict[str, str] = {}
        self.schemas = schemas or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(
                name=name,
                description=desc,
                parameters=dict(self.schemas.get(name, {})),
            )
            for name, desc in self._tools.items()
        ]

    async def call_tool(
        self, name: str, params: dict[str, Any], session_id: str = ""
    ) -> ToolCallResult:
        self.calls.append((name, dict(params)))
        if name in self.failures:
            return ToolCallResult(ok=False, error=self.failures[name], tool=name)
        text = self.results.get(name, f"{name} 的结果")
        if not text:
            return ToolCallResult(ok=False, error="工具返回了空结果", tool=name)
        return ToolCallResult(ok=True, text=text, tool=name)


class StubPersona:
    """固定人格。"""

    def __init__(self, text: str = "你是一个温柔的少女。") -> None:
        self.text = text

    async def get_persona_text(self, session_id: str) -> str:
        return self.text
