"""Read-only routing context snapshot primitives.

This module intentionally has no AstrBot or engine imports.  Owners of the VW
host can feed confirmed incoming messages and cooperative send receipts into
:class:`RoutingContextStore`; consumers receive detached, bounded JSON data.
"""
from __future__ import annotations

import hashlib
import json
import time
import math
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

API_VERSION = 1

@dataclass
class RoutingMessage:
    event_id: str
    origin_session_id: str
    text: str = ""
    message_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    is_bot: bool = False
    occurred_at: float = 0.0
    sequence: int = 0
    reply_to: dict[str, str] = field(default_factory=dict)
    media: list[dict[str, Any]] = field(default_factory=list)
    capture_source: str = "incoming"
    delivery: str = "received"
    send_operation_id: str = ""
    truncated: bool = False

    def __post_init__(self) -> None:
        self.event_id = str(self.event_id or "").strip()
        self.origin_session_id = str(self.origin_session_id or "").strip()
        self.text = str(self.text or "")
        self.occurred_at = float(self.occurred_at or 0.0)
        self.sequence = int(self.sequence or 0)
        self.reply_to = {str(k): str(v or "") for k, v in (self.reply_to or {}).items()}
        self.media = [dict(x) for x in (self.media or []) if isinstance(x, dict)]
        self.capture_source = str(self.capture_source or "incoming")
        self.delivery = str(self.delivery or "received")

    @property
    def confirmed(self) -> bool:
        if not self.is_bot:
            return True
        return self.delivery in {"accepted", "delivered"} and self.capture_source == "cooperative_send"

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("send_operation_id", None)
        data.pop("confirmed", None)
        return data

class RoutingContextStore:
    """Bounded per-session event store with identity-only deduplication."""
    def __init__(self, *, generation: str = "", max_messages: int = 80, max_age_seconds: float = 1800.0):
        self.generation = str(generation or f"g-{time.time_ns()}")
        self.max_messages = max(1, int(max_messages))
        self.max_age_seconds = max(0.0, float(max_age_seconds))
        self._sessions: dict[str, list[RoutingMessage]] = {}
        self._events: set[tuple[str, str]] = set()
        self._operations: set[tuple[str, str]] = set()
        self.capture_paths = {"incoming", "cooperative_send"}
        self.missing_paths = {"host_hook_success_unknown", "streaming", "uncooperative_send"}

    def record(self, message: RoutingMessage | dict[str, Any]) -> bool:
        msg = deepcopy(message) if isinstance(message, RoutingMessage) else RoutingMessage(**deepcopy(message))
        if not msg.event_id or not msg.origin_session_id:
            return False
        if type(msg.is_bot) is not bool or not math.isfinite(msg.occurred_at) or msg.occurred_at > time.time()+300:
            return False
        msg.truncated = msg.truncated or len(msg.text) > 800
        msg.text = msg.text[:800]
        msg.sender_id = str(msg.sender_id or "")[:200]
        msg.sender_name = str(msg.sender_name or "")[:100]
        msg.reply_to = {k: str(v)[:200] for k, v in msg.reply_to.items() if k in {"event_id", "message_id", "sender_id"}}
        safe_media = []
        for item in msg.media[:4]:
            description = str(item.get("description", ""))
            if any(token in description.lower() for token in ("file://", "c:\\", "c:/", "/c/users/", "/users/", "/home/", "/tmp/", "token=", "api_key=", "https://", "http://")):
                description = "[媒体描述不可安全展示]"
            msg.truncated = msg.truncated or len(description) > 300
            safe_media.append({"kind": str(item.get("kind", ""))[:24], "description": description[:300]})
        msg.media = safe_media
        if msg.is_bot and not msg.confirmed:
            return False
        identity = (msg.origin_session_id, msg.event_id)
        operation = (msg.origin_session_id, msg.send_operation_id)
        if identity in self._events:
            return False
        if msg.send_operation_id and operation in self._operations:
            return False
        self._events.add(identity)
        if msg.send_operation_id:
            self._operations.add(operation)
        rows = self._sessions.setdefault(msg.origin_session_id, [])
        rows.append(msg)
        rows.sort(key=lambda x: (x.occurred_at, x.sequence, x.event_id))
        if self.max_age_seconds:
            cutoff = time.time() - self.max_age_seconds
            rows[:] = [x for x in rows if not x.occurred_at or x.occurred_at >= cutoff]
        del rows[:-self.max_messages]
        if len(self._events) > 8000:
            self._events = {(x.origin_session_id, x.event_id) for values in self._sessions.values() for x in values}
            self._operations = {(x.origin_session_id, x.send_operation_id) for values in self._sessions.values() for x in values if x.send_operation_id}
        if len(self._sessions) > 200:
            oldest = min(self._sessions, key=lambda key: max((x.occurred_at for x in self._sessions[key]), default=0))
            self._sessions.pop(oldest, None)
        return True

    def snapshot(self, session_id: str, *, exclude_event_ids: Iterable[str] = (), max_messages: int = 20, max_chars: int = 8000) -> dict[str, Any]:
        target = str(session_id or "")
        excluded = {str(x) for x in (exclude_event_ids or []) if str(x)}
        cutoff = time.time() - self.max_age_seconds
        rows = [x for x in self._sessions.get(target, []) if x.event_id not in excluded and (not self.max_age_seconds or not x.occurred_at or x.occurred_at >= cutoff)]
        eligible_count = len(rows)
        rows = [x for x in rows if not x.is_bot or x.confirmed]
        rows = rows[-max(0, int(max_messages)):] if int(max_messages) > 0 else []
        budget = max(0, int(max_chars))
        kept: list[RoutingMessage] = []
        used = 0
        truncated = False
        for row in reversed(rows):
            text_cost = len(json.dumps(row.public(), ensure_ascii=False, sort_keys=True))
            if used + text_cost > budget:
                truncated = True
                break
            kept.append(row)
            used += text_cost
        kept.reverse()
        incoming = any(not x.is_bot for x in rows)
        bot = any(x.is_bot for x in rows)
        payload = {
            "api_version": API_VERSION, "status": "ok", "session_id": target,
            "instance_generation": self.generation,
            "messages": [deepcopy(x.public()) for x in kept],
            "summary": None, "truncated": truncated or len(kept) < eligible_count, "omitted": max(0, eligible_count-len(kept)),
            "coverage": {"incoming": "partial" if incoming else "none", "bot_sent": "partial" if bot else "none", "capture_paths": sorted(self.capture_paths), "missing_paths": sorted(self.missing_paths), "continuation_ready": any(x.is_bot and x.confirmed for x in kept)},
        }
        payload["snapshot_version"] = snapshot_fingerprint(payload)
        return deepcopy(payload)

def snapshot_fingerprint(payload: dict[str, Any]) -> str:
    body = dict(payload)
    body.pop("snapshot_version", None)
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]

def routing_context_snapshot(store: RoutingContextStore, session_id: str, **kwargs: Any) -> dict[str, Any]:
    return store.snapshot(session_id, **kwargs)
