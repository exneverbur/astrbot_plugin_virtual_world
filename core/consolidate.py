"""睡眠整理：把这段时间的经历消化成记忆与画像，顺手把细节折叠掉。

对着人类的记忆机制做的三件事：

1. **消化**（慢波睡眠的系统巩固）：原始聊天 + 现有记忆 + 画像现状 → 要点、事实、关系、缩略版；
2. **重组**：同一个人的同一件事合并、被确认过的旧记忆更新、回访时间按间隔重复往后排；
3. **遗忘**：只"折叠"（raw → gist，原文进 ``context``），**不删除**——忘的是细节，不是那件事。

小睡只做轻整理：要点化 + 缩略版 + 一条梦，不碰关系与事实（小睡短，做重判断容易记错）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .json_actions import extract_json_object
from .memory import INTERACTION, MemoryEngine
from .profile import ProfileStore

REVIEW_STEPS = (1.0, 3.0, 7.0, 30.0)
"""间隔重复：一条值得记住的事，隔多久回访一次（天）。"""

DREAM_TYPE = "dream"
"""梦：低权重的记忆，只进"想起"那一段，不进画像。"""

MAX_FACTS_PER_RUN = 8
"""一次整理最多记几条"关于他的事"（模型一次给太多时，多半是在凑数）。"""

MAX_BONDS_PER_RUN = 4
"""一次整理最多改几条关系（关系本来就该慢）。"""


@dataclass
class ConsolidateResult:
    """一次整理做了什么（写日志与调试输出用）。"""

    mode: str = "full"
    ok: bool = False
    note: str = ""
    memories: int = 0
    merged: int = 0
    folded: int = 0
    facts: int = 0
    relations: int = 0
    digests: int = 0
    affinity: int = 0
    facts_merged: int = 0
    dream: str = ""
    skipped: list[str] = field(default_factory=list)
    raw: str = ""
    """模型的原始返回（截断一份）：整理结果不对时，日志里能直接看到它写了什么。"""

    parsed: dict[str, Any] = field(default_factory=dict)
    """解析出来的 JSON（预览时给编辑器看）。"""

    preview: dict[str, Any] = field(default_factory=dict)
    """``{"system": …, "user": …}``：预览模式下把"喂进去的东西"也带出来。"""

    def summary(self) -> str:
        bits = []
        if self.memories:
            bits.append(f"要点 {self.memories} 条")
        if self.merged:
            bits.append(f"合并 {self.merged} 条")
        if self.folded:
            bits.append(f"折叠 {self.folded} 条")
        if self.facts:
            bits.append(f"关于他们的事 {self.facts} 条")
        if self.facts_merged:
            bits.append(f"合并重复说法 {self.facts_merged} 条")
        if self.relations:
            bits.append(f"关系 {self.relations} 条")
        if self.digests:
            bits.append(f"缩略版 {self.digests} 份")
        if self.affinity:
            bits.append(f"好感变化 {self.affinity} 次")
        if self.dream:
            bits.append("做了一个梦")
        return "、".join(bits) or "没什么可整理的"


class Consolidator:
    """睡眠整理器：拿到（世界 + 记忆 + 画像 + 一个模型通道）就能跑。"""

    def __init__(
        self,
        *,
        world: Any,
        memory: MemoryEngine,
        profiles: ProfileStore,
        prompts: Any = None,
        sessions_of: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.world = world
        self.memory = memory
        self.profiles = profiles
        self.prompts = prompts
        self._sessions_of = sessions_of or (lambda session_id: [session_id])

    def set_world(self, world: Any) -> None:
        self.world = world

    # ---------------- 输入 ----------------

    def chat_lines(
        self, records: list[dict[str, Any]], *, limit: int = 400
    ) -> list[str]:
        """这段时间的原始聊天记录（带时间、谁说的）；**这是整理的上下文**。"""

        rows: list[str] = []
        for item in records:
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            who = str(item.get("name") or item.get("user_id") or "有人")
            uid = str(item.get("user_id") or "")
            stamp = time.strftime(
                "%m-%d %H:%M", time.localtime(float(item.get("at") or 0.0))
            )
            tag = "（她自己）" if item.get("is_self") else f"（QQ {uid}）" if uid else ""
            rows.append(f"[{stamp}] {who}{tag}: {text}")
        return rows[-max(1, int(limit)) :]

    def memory_lines(self, session_id: str, *, limit: int = 120) -> list[str]:
        """现有记忆（只给相关的、最近的）：带 id，让模型按 id 合并或折叠。"""

        rows = self.memory.db.query_memories(
            session_ids=self._sessions_of(session_id),
            limit=max(1, int(limit)),
        )
        lines: list[str] = []
        for item in rows:
            stamp = time.strftime(
                "%m-%d", time.localtime(float(item.get("created_at") or 0.0))
            )
            tier = "要点" if str(item.get("tier")) == "gist" else "原文"
            people = ",".join(str(entry) for entry in (item.get("participants") or []))
            tail = f"（{people}）" if people else ""
            lines.append(
                f"#{item['id']} [{stamp}·{tier}] {str(item.get('content') or '')[:120]}{tail}"
            )
        return lines

    def profile_lines(self, session_id: str, *, limit: int = 40) -> list[str]:
        """她现在认识的人：给整理模型当"已知"，免得重复记同一件事。"""

        rows: list[str] = []
        for row in self.profiles.list_people(session_id, limit=limit):
            user_id = str(row.get("user_id") or "")
            payload = dict(row.get("payload") or {})
            names = [str(item) for item in (payload.get("names") or []) if str(item)]
            bonds = self.profiles.bonds(session_id, user_id, statuses=["current"])
            view = self.profiles.view(session_id, user_id)
            bits = [
                f"QQ {user_id}",
                names[-1] if names else user_id,
                "、".join(str(bond.get("type") or "") for bond in bonds) or "没定关系",
                f"好感 {round(float(row.get('affinity') or 0.0))}",
            ]
            if view is not None and view.digest:
                bits.append(f"印象：{view.digest}")
            rows.append("｜".join(bits))
        return rows

    def known_ids(
        self, session_id: str, records: list[dict[str, Any]] | None = None
    ) -> set[str]:
        """这一轮允许写进画像的人：**真的出现过**的那些 QQ 号。

        来源有两处——通讯录里已经建过档的人，以及这段时间的聊天记录里发过言的人。
        整理模型偶尔会顺着昵称编一个号出来（例如把「主人」写成某个不存在的数字），
        不挡的话通讯录里会凭空多出一个从没说过话的人。
        """

        ids: set[str] = set()
        for row in self.profiles.list_people(session_id, limit=500):
            user_id = str(row.get("user_id") or "").strip()
            if user_id:
                ids.add(user_id)
        for item in records or []:
            user_id = str(item.get("user_id") or "").strip()
            if user_id:
                ids.add(user_id)
        return ids

    # ---------------- 跑一次整理 ----------------

    async def run(
        self,
        *,
        session_id: str,
        llm: Callable[[str, str], Awaitable[str | None]] | None,
        mode: str = "full",
        chat_records: list[dict[str, Any]] | None = None,
        persona_text: str = "",
        pending: list[str] | None = None,
        now: float | None = None,
        dry_run: bool = False,
    ) -> ConsolidateResult:
        """跑一次整理。``llm(system, prompt)`` 由调用方提供（独立整理模型）。

        ``dry_run=True``：照常组装输入、调模型、解析，但**一个字都不写库**，
        把提示词与模型原话一起返回——用来先看"它整理得像不像样"。
        """

        result = ConsolidateResult(mode=str(mode or "full"))
        if llm is None or self.prompts is None:
            result.note = "没有可用的整理模型"
            return result
        stamp = float(now if now is not None else time.time())
        max_items = 12 if result.mode == "full" else 6
        records = list(chat_records or [])
        known = self.known_ids(session_id, records)
        system, prompt = self.prompts.build_consolidate_prompt(
            persona_text=persona_text,
            chat_lines=self.chat_lines(records),
            memories=self.memory_lines(session_id) if result.mode == "full" else [],
            profiles=self.profile_lines(session_id),
            pending=list(pending or []),
            date_text=time.strftime("%Y-%m-%d", time.localtime(stamp)),
            mode=result.mode,
            max_items=max_items,
        )
        try:
            reply = await llm(system, prompt)
        except Exception as exc:
            result.note = f"整理调用失败：{exc}"
            return result
        payload = extract_json_object(reply or "")
        result.raw = str(reply or "")[:2000]
        if dry_run:
            result.ok = bool(reply)
            result.note = "预览：只跑模型，没有写库"
            result.parsed = payload if isinstance(payload, dict) else {}
            result.preview = {"system": system, "user": prompt}
            if not isinstance(payload, dict) or not payload:
                result.note = "预览：模型没给出可用的 JSON"
            return result
        if not isinstance(payload, dict) or not payload:
            result.note = "整理模型没有给出可用的 JSON"
            return result
        result.ok = True
        if result.mode == "nap":
            self._apply_nap(session_id, payload, result, now=stamp, known=known)
        else:
            self._apply_full(session_id, payload, result, now=stamp, known=known)
        return result

    # ---------------- 落库：整觉 ----------------

    def _apply_full(
        self,
        session_id: str,
        payload: dict[str, Any],
        result: ConsolidateResult,
        *,
        now: float,
        known: set[str] | None = None,
    ) -> None:
        for item in self._rows(payload.get("memories")):
            if self._write_memory(session_id, item, result, now=now):
                result.memories += 1
        for item in self._rows(payload.get("merge")):
            keep = self._int(item.get("keep_id"))
            drops = [self._int(value) for value in (item.get("drop_ids") or [])]
            drops = [value for value in drops if value]
            text = str(item.get("text") or "").strip()
            if keep and text:
                self.memory.db.update_memory(keep, content=text[:200], tier="gist")
            for drop in drops:
                self.memory.fold(drop, text=text, when=now)
                result.folded += 1
            if keep and (text or drops):
                result.merged += 1
        touched_facts: set[str] = set()
        for item in self._rows(payload.get("facts"))[:MAX_FACTS_PER_RUN]:
            # 没带"他的原话"的事实一律不记：这是最容易被模型编出来的那一类
            if not str(item.get("evidence") or "").strip():
                result.skipped.append(f"没带原话的事实被跳过：{item.get('text')}")
                continue
            user_id = str(item.get("user_id") or "").strip()
            if not self._known(user_id, known):
                result.skipped.append(f"材料里没这个人的事实被跳过：{user_id}")
                continue
            outcome = self.profiles.note_fact(
                session_id,
                user_id,
                text=str(item.get("text") or ""),
                kind=str(item.get("kind") or "other"),
                evidence=str(item.get("evidence") or ""),
                context=str(item.get("context") or ""),
                confidence=float(item.get("confidence") or 0.6),
                source_session=session_id,
            )
            if outcome.get("ok"):
                result.facts += 1
                touched_facts.add(user_id)
            else:
                result.skipped.append(str(outcome.get("reason") or "事实没记下"))
        # 顺手把同一个人的重复说法合并掉：同一件事换个措辞记六遍，
        # 提示词里那一类（每类最多 6 条）就全是废话了。
        # 除了这一轮写到的人，材料里出现的人一起过一遍——这样早先攒下来的重复
        # 也能在她第一次睡觉整理时清掉。
        for user_id in sorted(set(touched_facts) | set(list(known)[:12])):
            merged = self.profiles.dedupe_facts(session_id, user_id)
            if merged:
                result.facts_merged += merged
        for item in self._rows(payload.get("relations"))[:MAX_BONDS_PER_RUN]:
            if not str(item.get("evidence") or "").strip():
                result.skipped.append(f"没带原话的关系被跳过：{item.get('type')}")
                continue
            user_id = str(item.get("user_id") or "").strip()
            if not self._known(user_id, known):
                result.skipped.append(f"材料里没这个人的关系被跳过：{user_id}")
                continue
            outcome = self.profiles.note_bond(
                session_id,
                user_id,
                type=str(item.get("type") or ""),
                evidence=str(item.get("evidence") or ""),
                confidence=float(item.get("confidence") or 0.6),
                asserted_by=str(item.get("asserted_by") or "她的判断"),
                now=now,
            )
            if outcome.get("ok"):
                result.relations += 1
            else:
                result.skipped.append(str(outcome.get("reason") or "关系没记下"))
        result.digests = self._apply_digests(
            session_id, payload.get("digests"), known=known
        )
        for item in self._rows(payload.get("affinity")):
            user_id = str(item.get("user_id") or "").strip()
            if not self._known(user_id, known):
                result.skipped.append(f"材料里没这个人的好感变化被跳过：{user_id}")
                continue
            outcome = self.profiles.adjust_affinity(
                session_id,
                user_id,
                float(item.get("delta") or 0.0),
                reason=str(item.get("reason") or "睡着的时候想了想"),
                source="consolidate",
                now=now,
            )
            if outcome.get("ok"):
                result.affinity += 1
        for item in self._rows(payload.get("forget")):
            memory_id = self._int(item.get("id"))
            if memory_id:
                self.memory.fold(memory_id, text=str(item.get("text") or ""), when=now)
                result.folded += 1
        result.dream = self._apply_dream(session_id, payload.get("dream"), now=now)

    # ---------------- 落库：小睡 ----------------

    def _apply_nap(
        self,
        session_id: str,
        payload: dict[str, Any],
        result: ConsolidateResult,
        *,
        now: float,
        known: set[str] | None = None,
    ) -> None:
        for item in self._rows(payload.get("notes")):
            if self._write_memory(session_id, item, result, now=now):
                result.memories += 1
        result.digests = self._apply_digests(
            session_id, payload.get("digests"), known=known
        )
        result.dream = self._apply_dream(session_id, payload.get("dream"), now=now)

    # ---------------- 小工具 ----------------

    @staticmethod
    def _rows(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _known(user_id: str, known: set[str] | None) -> bool:
        """这个号在材料里出现过没有；对不上就不写进画像。"""

        return bool(user_id) and user_id in (known or set())

    def _write_memory(
        self,
        session_id: str,
        item: dict[str, Any],
        result: ConsolidateResult,
        *,
        now: float,
    ) -> bool:
        text = " ".join(str(item.get("text") or "").split())[:200]
        if not text:
            return False
        people = [
            str(value)
            for value in (item.get("participants") or [])
            if str(value).strip()
        ]
        days = float(item.get("review_in_days") or 0.0)
        next_at = now + days * 86400 if days > 0 else 0.0
        memory_id = self.memory.remember(
            session_id=session_id,
            persona_id="",
            node_id=str(item.get("node_id") or ""),
            content=text,
            memory_type=str(item.get("type") or INTERACTION),
            related_users=people,
            participants=people,
            emotion=str(item.get("emotion") or ""),
            weight=float(item.get("weight") or 0.55),
            source="consolidate",
            tier="gist",
            context=str(item.get("context") or ""),
            keywords=[
                str(value) for value in (item.get("keywords") or []) if str(value)
            ],
            next_review_at=next_at,
            pinned=bool(item.get("pinned")),
        )
        return bool(memory_id)

    def _apply_digests(
        self, session_id: str, value: Any, *, known: set[str] | None = None
    ) -> int:
        if not isinstance(value, dict):
            return 0
        count = 0
        for user_id, digest in value.items():
            text = " ".join(str(digest or "").split())
            if not text or not str(user_id).strip():
                continue
            if known and str(user_id).strip() not in known:
                continue
            self.profiles.set_digest(session_id, str(user_id), text)
            count += 1
        return count

    def _apply_dream(self, session_id: str, value: Any, *, now: float) -> str:
        text = " ".join(str(value or "").split())[:200]
        if not text:
            return ""
        self.memory.remember(
            session_id=session_id,
            persona_id="",
            node_id="",
            content=f"做了个梦：{text}",
            memory_type=INTERACTION,
            emotion="迷糊",
            weight=0.25,
            source="dream",
            tier="gist",
            context=text,
            participants=[],
        )
        return text


def next_review_at(*, now: float, step: int = 0) -> float:
    """按间隔重复算下一次回访时间（第 0 步 = 1 天后）。"""

    days = REVIEW_STEPS[max(0, min(int(step), len(REVIEW_STEPS) - 1))]
    return float(now) + days * 86400


def dump_payload(payload: dict[str, Any]) -> str:
    """调试用：把整理模型返回的 JSON 压成一行。"""

    return json.dumps(payload, ensure_ascii=False)[:400]
