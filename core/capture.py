"""阶段A3：发言捕获的覆盖账（coverage）。

回答两个问题：

1. 这个会话里**别人**说的话，她的世界真的看见了吗？
2. **别人的话**有没有被算成"她自己选的动作"？

设计原则（计划 §5 A3）：

- **不 monkey-patch 平台发送**。只认四条受支持的入口——
  ``standard_reply`` 宿主标准回复链（靠 ``OnAfterMessageSentEvent`` 钩子）、
  ``extension`` 扩展的 ``say`` 与给合作插件用的 ``record_outbound``、
  ``command`` 指令动作**真正返回**的输出、
  ``unknown_direct`` 在受管会话里被发出但来源不明的消息。
- **按身份去重**：平台消息 id 优先 → 显式 ``operation_id`` → 都没有才退回正文指纹。
  本插件自己会补发宿主钩子（``main.py::_fire_hook``），同一条话会经过两条入口，
  去重是硬要求（X08「无双重记账」）。
- 来源不明的一律标 ``bot_external``：留痕可以，**绝不**当成她自己主动选择的动作。

这个模块只做账本与身份归一，不碰网络、不碰数据库、不猜时间窗口。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .source_events import REALITY_BOT_EXTERNAL, REALITY_REAL

__all__ = [
    "CAPTURE_KINDS",
    "CAPTURE_LABELS",
    "CAPTURE_COMMAND",
    "CAPTURE_EXTENSION",
    "CAPTURE_REPLY",
    "CAPTURE_UNKNOWN_DIRECT",
    "CaptureLedger",
    "CaptureRecord",
    "capture_identity",
    "capture_reality",
]


#: 宿主标准回复链（别的插件 / 宿主自己发的那一刻）。
CAPTURE_REPLY = "standard_reply"
#: 扩展的 say，以及合作插件显式调 record_outbound。
CAPTURE_EXTENSION = "extension"
#: 指令动作真正返回的输出（不是 intent，是拿回来的结果）。
CAPTURE_COMMAND = "command"
#: 受管会话里被发出来、但谁发的说不清。
CAPTURE_UNKNOWN_DIRECT = "unknown_direct"

CAPTURE_KINDS = (
    CAPTURE_REPLY,
    CAPTURE_EXTENSION,
    CAPTURE_COMMAND,
    CAPTURE_UNKNOWN_DIRECT,
)

#: 面板上的中文说法（跟 docs 里那张表一一对应）。
CAPTURE_LABELS = {
    CAPTURE_REPLY: "标准回复链",
    CAPTURE_EXTENSION: "扩展接口",
    CAPTURE_COMMAND: "指令输出",
    CAPTURE_UNKNOWN_DIRECT: "未知直发",
}


def capture_reality(kind: str) -> str:
    """这一类发言该按什么现实身份记。"""

    return REALITY_BOT_EXTERNAL if kind == CAPTURE_UNKNOWN_DIRECT else REALITY_REAL


def _text_fingerprint(texts: Iterable[Any]) -> str:
    lines = [str(item).strip() for item in texts if str(item or "").strip()]
    if not lines:
        return ""
    raw = "\n".join(lines).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


def capture_identity(
    *,
    message_ids: Iterable[Any] = (),
    operation_id: str = "",
    session_id: str = "",
    texts: Iterable[Any] = (),
) -> str:
    """归一出一条发言的身份；一个要素都没有就返回空串（不记）。

    优先平台消息 id——那是唯一能对齐"对方到底收到了哪一条"的东西；
    没有 id 时用显式 ``operation_id``（我们自己发的都有）；再没有才退回
    正文指纹（同会话同正文算同一条，够用于去重，但不当作事件身份）。
    """

    for item in message_ids or ():
        text = str(item or "").strip()
        if text:
            return f"id:{text}"
    op = str(operation_id or "").strip()
    if op:
        return f"op:{op}"
    digest = _text_fingerprint(texts)
    if digest:
        return f"text:{str(session_id or '').strip()}:{digest}"
    return ""


@dataclass
class CaptureRecord:
    """一条被捕获的发言。``count`` 只在同一身份重复出现时累加。"""

    kind: str
    identity: str
    session_id: str = ""
    reality_kind: str = REALITY_REAL
    origin: str = ""
    text: str = ""
    count: int = 1
    duplicates: int = 0
    first_at: float = 0.0
    last_at: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "kind_text": CAPTURE_LABELS.get(self.kind, self.kind),
            "identity": self.identity,
            "session_id": self.session_id,
            "reality_kind": self.reality_kind,
            "origin": self.origin,
            "text": self.text,
            "count": self.count,
            "duplicates": self.duplicates,
            "at": self.last_at,
            "detail": dict(self.detail),
        }


class CaptureLedger:
    """内存账本：按身份去重，按类目计数。

    ``max_records`` 只是防内存涨爆（老记录先丢），计数不会因此翻倍——
    去重靠的是一张独立的身份表 ``_seen``。
    """

    def __init__(
        self,
        *,
        clock: Any = time.time,
        max_records: int = 500,
    ) -> None:
        self.clock = clock
        self.max_records = max(1, int(max_records))
        self._seen: dict[str, CaptureRecord] = {}
        #: 同一条发言的别名身份（正文指纹等）→ 主身份。只用于认人，不再计数。
        self._aliases: dict[str, str] = {}
        self._order: list[str] = []
        self._counts: dict[str, int] = {kind: 0 for kind in CAPTURE_KINDS}
        self.duplicates = 0
        self.dropped = 0

    # ---------- 记账 ----------

    def note(
        self,
        kind: str,
        *,
        session_id: str = "",
        message_ids: Iterable[Any] = (),
        operation_id: str = "",
        texts: Iterable[Any] = (),
        reality_kind: str = "",
        origin: str = "",
        text: str = "",
        count: int = 1,
        detail: Mapping[str, Any] | None = None,
        aliases: Iterable[Any] = (),
    ) -> bool:
        """记一条；返回是否为**新的**一条（重复返回 False）。

        认不出的类目归到 ``unknown_direct``：宁可标"说不清"，
        也不要悄悄漏掉一条真实发言。

        ``aliases`` 是同一条发言的其它身份（比如正文指纹）：登记后
        :meth:`seen` 也认得它们，但**不计数**——用来回答"这句是不是我们自己发的"。
        """

        name = str(kind or "")
        if name not in CAPTURE_KINDS:
            name = CAPTURE_UNKNOWN_DIRECT
        identity = capture_identity(
            message_ids=message_ids,
            operation_id=operation_id,
            session_id=session_id,
            texts=(text,) if text else texts,
        )
        if not identity:
            return False
        step = max(1, int(count or 1))
        now = float(self.clock())
        known = self._seen.get(identity)
        if known is None:
            alias_target = self._aliases.get(identity, "")
            if alias_target:
                known = self._seen.get(alias_target)
        if known is not None:
            known.count += step
            known.duplicates += 1
            known.last_at = now
            self.duplicates += 1
            # 类目计数只数**不同的**发言：同一条被两条入口看见（比如我们自己补发的
            # 宿主钩子）不会再给这一类 +1 —— X08 要的就是"无双重记账"。
            self._register_aliases(known, aliases)
            return False
        record = CaptureRecord(
            kind=name,
            identity=identity,
            session_id=str(session_id or ""),
            reality_kind=str(reality_kind or capture_reality(name)),
            origin=str(origin or ""),
            text=str(text or ""),
            count=step,
            first_at=now,
            last_at=now,
            detail=dict(detail or {}),
        )
        self._seen[identity] = record
        self._order.append(identity)
        self._counts[name] = self._counts.get(name, 0) + step
        self._register_aliases(record, aliases)
        self._trim()
        return True

    def _register_aliases(self, record: CaptureRecord, aliases: Iterable[Any]) -> None:
        for item in aliases or ():
            alias = str(item or "").strip()
            if not alias or alias == record.identity or alias in self._seen:
                continue
            self._aliases[alias] = record.identity

    def _trim(self) -> None:
        while len(self._order) > self.max_records:
            oldest = self._order.pop(0)
            self._seen.pop(oldest, None)
            for alias, target in list(self._aliases.items()):
                if target == oldest:
                    self._aliases.pop(alias, None)
            self.dropped += 1

    # ---------- 读账 ----------

    def seen(self, identity: str) -> bool:
        """这条发言是不是**已经**记过了（主身份或别名都算）。"""

        key = str(identity or "")
        return key in self._seen or key in self._aliases

    def records(self, *, kind: str = "", session_id: str = "") -> list[CaptureRecord]:
        rows = list(self._seen.values())
        if kind:
            rows = [row for row in rows if row.kind == kind]
        if session_id:
            rows = [row for row in rows if row.session_id == session_id]
        rows.sort(key=lambda row: row.last_at, reverse=True)
        return rows

    def coverage(self) -> dict[str, Any]:
        """给验收与面板看的四类计数（顺序固定，便于比对）。"""

        counts = {kind: int(self._counts.get(kind, 0)) for kind in CAPTURE_KINDS}
        samples = [
            row.to_row()
            for row in self.records(kind=CAPTURE_UNKNOWN_DIRECT)[:5]
        ]
        return {
            "counts": counts,
            "labels": dict(CAPTURE_LABELS),
            "total": sum(counts.values()),
            "records": len(self._seen),
            "duplicates": int(self.duplicates),
            "dropped": int(self.dropped),
            "unknown_direct_samples": samples,
            "external_kinds": sorted(
                {
                    row.reality_kind
                    for row in self._seen.values()
                    if row.reality_kind == REALITY_BOT_EXTERNAL
                }
            ),
        }

    def snapshot(self, *, session_id: str = "", limit: int = 20) -> dict[str, Any]:
        payload = self.coverage()
        payload["recent"] = [
            row.to_row() for row in self.records(session_id=session_id)[: max(0, int(limit))]
        ]
        return payload

    def reset(self) -> None:
        self._seen.clear()
        self._aliases.clear()
        self._order.clear()
        self._counts = {kind: 0 for kind in CAPTURE_KINDS}
        self.duplicates = 0
        self.dropped = 0
