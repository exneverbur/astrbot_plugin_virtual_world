"""能力经验（P3-1）：从**真实做成功过的事**里攒下来的可复用方法。

和别的记忆分开存是有原因的：

* **记忆**（`node_memory`）记的是"发生过什么"，给她自己回味用；
* **画像**（profile）记的是"这个人是谁"；
* **谈资**（topics）记的是"能说给别人听的事"；
* **经验**记的是"**这类事怎么做成的，以及哪里会翻车**"——用来下次少走弯路。

四条纪律（每条都有用例钉住）：

1. **只有证据支撑的才算**：一条经验必须带 ``evidence``（真实工具回执 / 动作终态），
   模型嘴上的"我做成了"不算（K01）；
2. **身份稳定**：``situation + method`` 决定 id；同一件事学会两遍只加证据，不新增两条；
3. **作用域隔离**：私聊里学到的经验不许进全局召回（``scope`` + ``persona_id`` 双重过滤）；
4. **会失效**：依赖（工具名/动作定义）变了（``dependency_hash`` 对不上）→ 不可用；
   到期也会自动不用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

STATUS_CANDIDATE = "candidate"
STATUS_ACTIVE = "active"
STATUS_DEPRECATED = "deprecated"
STATUS_REVOKED = "revoked"

STATUSES = (STATUS_CANDIDATE, STATUS_ACTIVE, STATUS_DEPRECATED, STATUS_REVOKED)

STATUS_LABELS = {
    STATUS_CANDIDATE: "候选",
    STATUS_ACTIVE: "可用",
    STATUS_DEPRECATED: "过时",
    STATUS_REVOKED: "已撤销",
}

#: 只有这两档会被召回
USABLE_STATUSES = (STATUS_ACTIVE,)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _blob(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity_of(situation: Any, method: Any) -> str:
    """同一件事 + 同一个做法 = 同一条经验（改做法才算新经验）。"""

    body = _text(situation) + "\u0000" + _text(method)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return f"exp_{digest}"


def dependency_hash(*parts: Any) -> str:
    """依赖指纹：工具名 / 动作定义变了，旧经验就不该再拿来用。"""

    body = "\u0000".join(sorted(_text(item) for item in parts if _text(item)))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Experience:
    """一条经验（形状与库里的行一致，便于直接读写）。"""

    experience_id: str = ""
    scope: str = ""
    persona_id: str = ""
    situation: str = ""
    method: str = ""
    limitations: str = ""
    evidence: tuple[str, ...] = ()
    dependency: str = ""
    status: str = STATUS_CANDIDATE
    revision: int = 0
    confidence: float = 0.0
    source: str = "agent"
    note: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status in USABLE_STATUSES

    def as_dict(self) -> dict[str, Any]:
        return {
            "experience_id": self.experience_id,
            "scope": self.scope,
            "persona_id": self.persona_id,
            "situation": self.situation,
            "method": self.method,
            "limitations": self.limitations,
            "evidence_refs": json.dumps(list(self.evidence), ensure_ascii=False),
            "dependency_hash": self.dependency,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "revision": int(self.revision),
            "confidence": float(self.confidence),
            "source": self.source,
            "note": self.note,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "expires_at": float(self.expires_at),
        }


def build(
    *,
    situation: Any,
    method: Any,
    limitations: Any = "",
    evidence: Iterable[Any] = (),
    dependency: Any = "",
    scope: Any = "",
    persona_id: Any = "",
    source: Any = "agent",
    confidence: Any = 0.5,
    now: float = 0.0,
    ttl_seconds: float = 0.0,
) -> Experience:
    """造一条**候选**经验（没有证据的一律不造：返回空 id）。"""

    refs = tuple(_text(item) for item in (evidence or ()) if _text(item))
    if not _text(situation) or not _text(method) or not refs:
        return Experience()
    stamp = _float(now)
    ttl = max(0.0, _float(ttl_seconds))
    return Experience(
        experience_id=identity_of(situation, method),
        scope=_text(scope),
        persona_id=_text(persona_id),
        situation=_text(situation),
        method=_text(method),
        limitations=_text(limitations),
        evidence=refs,
        dependency=_text(dependency),
        status=STATUS_CANDIDATE,
        revision=1,
        confidence=max(0.0, min(1.0, _float(confidence))),
        source=_text(source) or "agent",
        created_at=stamp,
        updated_at=stamp,
        expires_at=(stamp + ttl) if ttl > 0 else 0.0,
    )


def from_row(row: Mapping[str, Any] | None) -> Experience | None:
    """从库里的行读回；读不懂就 ``None``（不抛）。"""

    if not row:
        return None
    try:
        raw = row.get("evidence_refs")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = []
        else:
            parsed = raw or []
        return Experience(
            experience_id=_text(row.get("experience_id")),
            scope=_text(row.get("scope")),
            persona_id=_text(row.get("persona_id")),
            situation=_text(row.get("situation")),
            method=_text(row.get("method")),
            limitations=_text(row.get("limitations")),
            evidence=tuple(_text(item) for item in parsed if _text(item)),
            dependency=_text(row.get("dependency_hash")),
            status=_text(row.get("status")) or STATUS_CANDIDATE,
            revision=int(row.get("revision") or 0),
            confidence=_float(row.get("confidence")),
            source=_text(row.get("source")) or "agent",
            note=_text(row.get("note")),
            created_at=_float(row.get("created_at")),
            updated_at=_float(row.get("updated_at")),
            expires_at=_float(row.get("expires_at")),
        )
    except Exception:
        return None


def usable(row: Mapping[str, Any] | None, *, dependency: str = "", now: float = 0.0) -> bool:
    """这条经验现在能不能用：状态对、没过期、依赖没变。"""

    item = from_row(row) if isinstance(row, Mapping) else row
    if not isinstance(item, Experience) or not item.usable:
        return False
    expires = _float(item.expires_at)
    if expires > 0 and _float(now) >= expires:
        return False
    if item.dependency and _text(dependency) and item.dependency != _text(dependency):
        return False
    return True


def scope_allows(row: Mapping[str, Any] | None, *, scope: str = "", persona_id: str = "") -> bool:
    """作用域过滤：经验只对"学到它的那个范围"可见。

    空 ``scope`` 的经验视为"哪里都别用"——比"哪里都用"安全（宁可不召回，也不越界）。
    """

    item = from_row(row) if isinstance(row, Mapping) else row
    if not isinstance(item, Experience):
        return False
    if not item.scope:
        return False
    if _text(scope) and item.scope != _text(scope):
        return False
    if item.persona_id and _text(persona_id) and item.persona_id != _text(persona_id):
        return False
    return True


def promote(item: Experience, *, confidence: Any = None, now: float = 0.0) -> Experience:
    """候选 → 可用（发布）。撤销过的**不许复活**。"""

    if item.status == STATUS_REVOKED:
        return item
    value = item.confidence if confidence is None else max(0.0, min(1.0, _float(confidence)))
    return Experience(
        **{
            **item.__dict__,
            "status": STATUS_ACTIVE,
            "confidence": value,
            "revision": int(item.revision) + 1,
            "updated_at": _float(now) or item.updated_at,
        }
    )


def deprecate(item: Experience, *, note: str = "", now: float = 0.0) -> Experience:
    if item.status == STATUS_REVOKED:
        return item
    return Experience(
        **{
            **item.__dict__,
            "status": STATUS_DEPRECATED,
            "note": _text(note) or item.note,
            "revision": int(item.revision) + 1,
            "updated_at": _float(now) or item.updated_at,
        }
    )


def revoke(item: Experience, *, note: str = "", now: float = 0.0) -> Experience:
    return Experience(
        **{
            **item.__dict__,
            "status": STATUS_REVOKED,
            "note": _text(note) or item.note,
            "revision": int(item.revision) + 1,
            "updated_at": _float(now) or item.updated_at,
        }
    )


def with_evidence(item: Experience, refs: Iterable[Any], *, now: float = 0.0) -> Experience:
    """追加证据（去重；同一条经验不会因为学了两遍变成两条）。"""

    merged = list(item.evidence)
    for raw in refs or ():
        text = _text(raw)
        if text and text not in merged:
            merged.append(text)
    if len(merged) == len(item.evidence):
        return item
    return Experience(
        **{
            **item.__dict__,
            "evidence": tuple(merged),
            "revision": int(item.revision) + 1,
            "updated_at": _float(now) or item.updated_at,
        }
    )


def prompt_line(item: Experience, *, limit: int = 120) -> str:
    """给提示词的一行（一行说清"什么时候、怎么做、注意什么"）。"""

    text = f"- {item.situation}：{item.method}"
    if item.limitations:
        text += f"（注意：{item.limitations}）"
    return text if len(text) <= limit else text[: limit - 1] + "…"
