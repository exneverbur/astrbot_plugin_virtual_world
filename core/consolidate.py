"""睡眠整理：把这段时间的经历消化成记忆与画像，顺手把细节折叠掉。

对着人类的记忆机制做的三件事：

1. **消化**（慢波睡眠的系统巩固）：原始聊天 + 现有记忆 + 画像现状 → 要点、事实、关系、缩略版；
2. **重组**：同一个人的同一件事合并、被确认过的旧记忆更新、回访时间按间隔重复往后排；
3. **遗忘**：只"折叠"（raw → gist，原文进 ``context``），**不删除**——忘的是细节，不是那件事。

小睡只做轻整理：要点化 + 缩略版 + 一条梦，不碰关系与事实（小睡短，做重判断容易记错）。
"""

from __future__ import annotations

import json
import logging
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .consolidate_jobs import _match_source, _material_rows
from .json_actions import extract_json_object
from .memory import INTERACTION, MemoryEngine, normalize_for_memory
from .profile import (
    TRUST_ADVICE_WINDOW_DAYS,
    TRUST_CHANNEL_A_STEP,
    TRUST_MAX,
    TRUST_MIN,
    ProfileStore,
)


def _captions_of(item: dict[str, Any]) -> dict[str, str]:
    """这条消息的图片转述（延迟导入 prompt，避免模块环）。"""

    from .prompt import image_captions_of

    try:
        return image_captions_of(item)
    except Exception:  # 转述坏了不该让整理整段失败
        return {}


PAYLOAD_SEP = "；"
logger = logging.getLogger(__name__)

REVIEW_STEPS = (1.0, 3.0, 7.0, 30.0)
"""间隔重复：一条值得记住的事，隔多久回访一次（天）。"""

TRUST_KINDS = ("promise", "advice", "help")
"""信任判断的三种来源：他答应的事 / 他给的建议 / 他帮过的忙。"""


_TRUST_QUOTES = dict.fromkeys(map(ord, "\"'“”‘’「」『』【】"), None)
"""证据里那些"只是排版"的符号：比对时全当没有。"""


def _trust_norm(text: Any) -> str:
    """证据比对的归一化：去掉所有空白与引号、全角转半角（标点差别不算差别）。"""

    return "".join(
        unicodedata.normalize("NFKC", str(text or "")).translate(_TRUST_QUOTES).split()
    )


def _fallback_session_label(session_id: str) -> str:
    """没有引擎侧标签时的兜底：「群 123」/「私聊 456」。

    整理模型要能看出「这句话是在哪儿说的」——组里的群和私聊共用一份留档，
    不标来源的话它会把两个地方的话揉成一件"当面发生的事"。
    """

    text = str(session_id or "").strip()
    if not text:
        return "说不清是哪儿"
    number = text.split(":")[-1] or text
    kind = ""
    for mark, name in (("PrivateMessage", "私聊"), ("FriendMessage", "私聊"), ("GroupMessage", "群")):
        if mark in text:
            kind = name
            break
    return f"{kind} {number}" if kind else f"会话 {number}"

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
    trust: int = 0
    """这次整理落下的信赖值变化条数（T15-S3 的日常通道 A）。"""
    facts_merged: int = 0
    superseded: int = 0
    """这次整理把多少条旧记忆标成了"旧说法"（``supersede``，路 A）。"""
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
        if self.superseded:
            bits.append(f"旧说法作废 {self.superseded} 条")
        if self.relations:
            bits.append(f"关系 {self.relations} 条")
        if self.digests:
            bits.append(f"缩略版 {self.digests} 份")
        if self.affinity:
            bits.append(f"好感变化 {self.affinity} 次")
        if self.trust:
            bits.append(f"信赖变化 {self.trust} 次")
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
        session_label: Callable[[str], str] | None = None,
    ) -> None:
        self.world = world
        self.memory = memory
        self.profiles = profiles
        self.prompts = prompts
        self._sessions_of = sessions_of or (lambda session_id: [session_id])
        # 「这是在哪儿说的 / 这条记忆是哪儿的」：默认按 id 现推一个人话标签，
        # 引擎会传自己的 session_label 进来（群 123「名字」/ 私聊 456）。
        self._session_label = session_label or _fallback_session_label

    def set_world(self, world: Any) -> None:
        self.world = world

    # ---------------- 输入 ----------------

    def chat_lines(
        self,
        records: list[dict[str, Any]],
        *,
        limit: int = 400,
        session_labels: dict[str, str] | None = None,
        current_session: str = "",
    ) -> list[str]:
        """这段时间的原始聊天记录（带时间、谁说的、**在哪说的**）；这是整理的上下文。

        素材**不按会话过滤**（组里的话共享同一段经历），但必须标出来源：
        少了一条来源标签，模型会把"私聊里说的悄悄话"和"群里当众说的话"
        压成同一条要点，以后再拿出来用就说错地方了。
        """

        labels = dict(session_labels or {})
        rows: list[str] = []
        for item in records:
            said = " ".join(str(item.get("text") or "").split())
            # 图片转述只活在聊天记录里，整理时以前被整个丢掉：那一轮她看得见，
            # 睡一觉之后就再也想不起来"他发过什么"。带进素材，模型才写得出物件。
            captions = _captions_of(item)
            if not said and not captions:
                continue
            text = f"{said}（图片：{PAYLOAD_SEP.join(captions.values())}）" if said and captions else (
                f"（图片：{PAYLOAD_SEP.join(captions.values())}）" if captions else said
            )
            who = str(item.get("name") or item.get("user_id") or "有人")
            uid = str(item.get("user_id") or "")
            stamp = time.strftime(
                "%m-%d %H:%M", time.localtime(float(item.get("at") or 0.0))
            )
            where = str(item.get("origin") or current_session or "")
            tag = "（她自己）" if item.get("is_self") else f"（QQ {uid}）" if uid else ""
            rows.append(f"[{stamp}·{self._where(where, labels)}] {who}{tag}: {text}")
        return rows[-max(1, int(limit)) :]

    def _where(self, session_id: str, labels: dict[str, str]) -> str:
        """来源会话的人话标签（引擎给的优先，缺了按 id 现推）。"""

        key = str(session_id or "")
        known = str(labels.get(key) or "")
        if known:
            return known
        return self._session_label(key)

    def memory_lines(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        session_labels: dict[str, str] | None = None,
        persona_id: str = "",
    ) -> list[str]:
        """现有记忆（只给相关的、最近的）：带 id 与**来源会话**，让模型按 id 合并或折叠。

        ``persona_id`` 是身份过滤，默认空串 = 只看「无主」的记忆：整理这条链路
        目前拿不到 persona（引擎只传了 persona_text），所以宁可按空身份看，
        也不能把别人格的记忆塞进提示词里当合并素材。要找具名人格的记忆，
        调用方必须自己把 persona_id 传进来。

        ``limit`` 默认取 ``memory.consolidate_memory_window``（240）：以前写死 120，
        库里有几百条时早先的记忆根本进不了"可以合并"的清单，模型只能看着最近一段
        说"没有可折叠的项"。
        """

        limit = self._memory_window() if limit is None else max(1, int(limit))
        rows = self.memory.db.query_memories(
            session_ids=self._sessions_of(session_id),
            persona_id=str(persona_id or ""),
            limit=max(1, int(limit)),
        )
        labels = dict(session_labels or {})
        lines: list[str] = []
        for item in rows:
            stamp = time.strftime(
                "%m-%d", time.localtime(float(item.get("created_at") or 0.0))
            )
            tier = "要点" if str(item.get("tier")) == "gist" else "原文"
            people = ",".join(str(entry) for entry in (item.get("participants") or []))
            tail = f"（{people}）" if people else ""
            where = self._where(str(item.get("session_id") or ""), labels)
            lines.append(
                f"#{item['id']} [{stamp}·{tier}·{where}] "
                f"{str(item.get('content') or '')[:120]}{tail}"
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
            trust = (view.trust if view is not None else 0.0)
            if abs(trust) >= 0.05:
                # 只有真的形成判断（|trust|>=0.05）才写出来：一串"+0.0"会稀释提示词。
                # 这是给模型"参考谁更靠谱"的信息，不是"必须听谁的"。
                bits.append(f"信赖 {trust:+.1f}")
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
        session_labels: dict[str, str] | None = None,
        current_session: str = "",
        current_node: str = "",
    ) -> ConsolidateResult:
        """跑一次整理。``llm(system, prompt)`` 由调用方提供（独立整理模型）。

        ``dry_run=True``：照常组装输入、调模型、解析，但**一个字都不写库**，
        把提示词与模型原话一起返回——用来先看"它整理得像不像样"。

        ``current_node``：她整理时所在的地点，只用来把写记忆的可选 ``node_id`` 枚举里
        排第一（见 :meth:`build_prompt`）。
        """

        result = ConsolidateResult(mode=str(mode or "full"))
        if llm is None or self.prompts is None:
            result.note = "没有可用的整理模型"
            return result
        stamp = float(now if now is not None else time.time())
        records = list(chat_records or [])
        known = self.known_ids(session_id, records)
        system, prompt = self.build_prompt(
            session_id=session_id,
            records=records,
            mode=result.mode,
            persona_text=persona_text,
            pending=pending,
            session_labels=session_labels,
            current_session=current_session,
            now=stamp,
            current_node=current_node,
        )
        if not system:
            result.note = "没有可用的整理模型"
            return result
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
        # ``trust`` 是 T15-S3 的日常通道 A：先摘出来自己核验，再交给 validate_proposal——
        # 那个函数不认识这个字段，留着它只会记一条"不认识的字段 trust"。
        trust_raw = self.take_trust(payload)
        self.apply_proposal(session_id, payload, result=result, now=stamp, known=known)
        items = self.validate_trust_items(
            trust_raw,
            session_id=session_id,
            material=records,
            now=stamp,
            known=known,
            skipped=result.skipped,
        )
        if items:
            self.apply_trust_items(
                session_id,
                items,
                now=stamp,
                source_id=f"manual:{int(stamp)}",
                result=result,
            )
        return result

    # ---------------- 提示词与落库（分开：增量作业要"锁里冻结、锁外调模型"） ----------------

    def build_prompt(
        self,
        *,
        session_id: str,
        records: list[dict[str, Any]] | None = None,
        mode: str = "full",
        persona_text: str = "",
        pending: list[str] | None = None,
        session_labels: dict[str, str] | None = None,
        current_session: str = "",
        now: float | None = None,
        current_node: str = "",
    ) -> tuple[str, str]:
        """组装一次整理的提示词。

        阶段F 的增量作业要在**短锁里**把素材冻结成提示词（连同现有记忆与画像），
        再到锁外慢慢调模型；所以组装这一步单独拿出来了。返回 ``("", "")`` 表示
        没有可用的整理通道。

        ``current_node``：她整理时人在哪个地点。写记忆时可选 ``node_id``，这份地点
        枚举把它排在第一个（其余是当前 world 的全部地点）——素材里没有"这句话发生在
        哪个房间"的字段，能给的只有"她今天可能在的地方"。
        """

        if self.prompts is None:
            return "", ""
        stamp = float(now if now is not None else time.time())
        value = str(mode or "full")
        max_items = 12 if value == "full" else 6
        rows = list(records or [])
        here = str(current_session or session_id or "")
        return self.prompts.build_consolidate_prompt(
            persona_text=persona_text,
            # 素材带来源标签（群 / 私聊）：模型才知道"这是哪里说的话"，
            # 免得把两处的对话压成同一件事。素材本身不按会话过滤。
            chat_lines=self.chat_lines(
                rows,
                session_labels=session_labels,
                current_session=here,
            ),
            memories=(
                self.memory_lines(session_id, session_labels=session_labels)
                if value == "full"
                else []
            ),
            profiles=self.profile_lines(session_id),
            pending=list(pending or []),
            date_text=time.strftime("%Y-%m-%d", time.localtime(stamp)),
            mode=value,
            max_items=max_items,
            # 写记忆时可选的 node_id 枚举：她此刻所在的地点排第一，其余是 world 里的地点。
            # 模型不给就落空串 = 全局记忆（P0 之后空节点在召回复核里对所有节点可见）。
            nodes=self._node_options(current_node),
        )

    def apply_proposal(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        result: ConsolidateResult | None = None,
        mode: str = "full",
        now: float = 0.0,
        known: set[str] | None = None,
        persona_id: str = "",
    ) -> ConsolidateResult:
        """把（已经校验过的）提案落库。

        调用方负责把它包在**一个事务**里（见 ``core/db.py::Database.transaction``）：
        计划 §10 F3 要求"新增记忆、facts、merge、画像更新与游标同一事务，出错整体回滚，
        不许部分成功却标全成功"。

        ``persona_id``（B5/WP6 收尾）：整理出来的记忆要落进**当时那个人格**的桶。
        以前这里写死空桶，于是具名人格整理完的东西它自己都读不回来（读取按人格精确
        分桶），而无主身份反而看得到本该属于某个人格的整理结果。默认空串 = 维持
        "无主"语义（还没接上人格解析的老调用方行为一个字节都不变）。
        """

        outcome = result if result is not None else ConsolidateResult(mode=str(mode or "full"))
        stamp = float(now or time.time())
        payload = self._claim_operations(session_id, payload, outcome, now=stamp)
        if str(outcome.mode) == "nap":
            self._apply_nap(
                session_id, payload, outcome, now=stamp, known=known, persona_id=persona_id
            )
        else:
            self._apply_full(
                session_id, payload, outcome, now=stamp, known=known, persona_id=persona_id
            )
        # P2：整理成功就顺手跑一次温和衰减（只降权重、绝不删行）。以前 ``decay`` 全仓
        # 没有一个调用点，命中 +0.02 只升不降，权重等于永久记忆；放在这里是因为整理
        # 是"她睡着时回想"的时机——低频、已经在事务里、失败会整体回滚。
        try:
            self.memory.decay(session_id=session_id)
        except Exception as exc:  # 衰减不该把一次成功的整理拖成失败
            outcome.skipped.append(f"衰减没跑成：{exc}")
        outcome.ok = True
        return outcome

    # ---------------- 信赖值（T15-S3）：日常通道 A ----------------
    #
    # 只在**夜间整理**里评估：同一次模型调用顺手给出"这几天谁更靠谱一点"，
    # 不再额外打一次模型（这就是"低成本"的全部含义）。规矩：
    # 1. 每人每次最多 ±0.05，单日净变化也 ±0.05（要攒 2–3 周才看得出来）；
    # 2. **必须能引用原文**：拿模型写的原话去素材里核，核不上就丢（同 F3）；
    # 3. 同一个作业重复提交只算一次（``source_kind/source_id`` 幂等）；
    # 4. 信赖**只进提示词**，不参与"要不要采纳"的判定。

    @staticmethod
    def take_trust(payload: dict[str, Any]) -> Any:
        """把 ``trust`` 从提案里摘出来（``validate_proposal`` 不认识这个字段）。"""

        if isinstance(payload, dict):
            return payload.pop("trust", None)
        return None

    @staticmethod
    def _trust_material(rows: Any) -> list[tuple[str, str, float]]:
        """证据核对的素材：来源事件与聊天记录行两种形状都吃。"""

        normalised = _material_rows(rows)
        if normalised:
            return normalised
        out: list[tuple[str, str, float]] = []
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            out.append(
                (
                    str(item.get("message_id") or item.get("source_event_id") or ""),
                    text,
                    float(item.get("at") or 0.0),
                )
            )
        return out

    def validate_trust_items(
        self,
        items: Any,
        *,
        session_id: str,
        material: Any = None,
        now: float = 0.0,
        known: set[str] | None = None,
        skipped: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """核验模型给的信赖增减，返回"能落库的那几条"。

        过不了的每条都往 ``skipped`` 里记一句人话（编辑器预览与日志里看得到）。
        主要拦三类：不认识的人、越界的数字、**找不到原文的证据**。
        """

        stamp = float(now or time.time())
        window = TRUST_ADVICE_WINDOW_DAYS * 86400.0
        rows = [
            row
            for row in self._trust_material(material)
            # 建议类证据只认 3 天内的话；没有时间戳的行（极少）留着不过滤
            if not row[2] or stamp - row[2] <= window
        ]
        raw = items if isinstance(items, list) else ([items] if isinstance(items, dict) else [])
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("person_id") or item.get("user_id") or "").strip()
            if not self._known(uid, known):
                self._trust_skip(skipped, f"不认识的 QQ {uid or '（空）'}")
                continue
            try:
                delta = float(item.get("delta") or 0.0)
            except (TypeError, ValueError):
                self._trust_skip(skipped, f"QQ {uid} 的信赖增量不是数字")
                continue
            if delta != delta or delta in (float("inf"), float("-inf")):
                self._trust_skip(skipped, f"QQ {uid} 的信赖增量不是数字")
                continue
            delta = max(-TRUST_CHANNEL_A_STEP, min(TRUST_CHANNEL_A_STEP, delta))
            if abs(delta) < 1e-6:
                self._trust_skip(skipped, f"QQ {uid} 的信赖增量太小（0）")
                continue
            quote = str(item.get("evidence") or item.get("quote") or "").strip()
            hit_at = self._trust_evidence(quote, rows)
            if hit_at is None:
                # 核不上原文 = 无证据，**不计分**（宁可少记，也不凭空给她一个印象）
                self._trust_skip(
                    skipped,
                    f"QQ {uid} 的信赖变化找不到原文证据" + (f"（{quote[:20]}）" if quote else ""),
                )
                continue
            kind = str(item.get("kind") or "advice").strip()
            if kind not in TRUST_KINDS:
                kind = "advice"
            if (uid, kind) in seen:
                self._trust_skip(skipped, f"QQ {uid} 的同一条判断重复出现")
                continue
            seen.add((uid, kind))
            value = max(TRUST_MIN, min(TRUST_MAX, delta))
            reason = " ".join(str(item.get("reason") or "").split())[:60] or quote[:60]
            out.append(
                {
                    "person_id": uid,
                    "delta": value,
                    "kind": kind,
                    "evidence": quote,
                    "at": hit_at,
                    "reason": reason,
                }
            )
        return out

    @staticmethod
    def _trust_evidence(quote: str, rows: list[tuple[str, str, float]]) -> float | None:
        """这句原话在哪条素材里（返回那条的时刻；核不上返回 None）。

        先用 F3 的 ``_match_source``（它还有"前 12 个字"的容错），再退一步做
        归一化子串比对——模型抄原话时经常把全角引号、空格换个样子。
        """

        text = str(quote or "").strip()
        if not text or not rows:
            return None
        hits, at = _match_source([], text, rows)
        if hits:
            return float(at or 0.0)
        target = _trust_norm(text)
        if len(target) < 2:
            return None
        for _event_id, body, stamp in rows:
            body_norm = _trust_norm(body)
            if not body_norm:
                continue
            if target in body_norm or (len(target) >= 4 and target[:12] in body_norm):
                return float(stamp or 0.0)
        return None

    @staticmethod
    def _trust_skip(skipped: list[str] | None, note: str) -> None:
        if skipped is not None and note not in skipped:
            skipped.append(note)

    def apply_trust_items(
        self,
        session_id: str,
        items: list[dict[str, Any]],
        *,
        now: float = 0.0,
        source_id: str = "",
        result: ConsolidateResult | None = None,
    ) -> int:
        """把核验过的信赖增减落库，返回真正生效的条数。

        幂等键 ``source_kind="consolidate"`` + ``source_id="{source_id}:{kind}"``：
        同一个作业重试、同一个人同一种判断，只会加一次分。写失败只记一句
        ``skipped``，**绝不**把一次成功的整理拖成失败。
        """

        stamp = float(now or time.time())
        applied = 0
        for item in items or []:
            uid = str(item.get("person_id") or "")
            if not uid:
                continue
            kind = str(item.get("kind") or "advice")
            try:
                outcome = self.profiles.adjust_trust(
                    session_id,
                    uid,
                    float(item.get("delta") or 0.0),
                    reason=str(item.get("reason") or ""),
                    source="consolidate",
                    channel="consolidate",
                    kind=kind,
                    evidence=str(item.get("evidence") or ""),
                    source_kind="consolidate" if source_id else "",
                    source_id=f"{source_id}:{kind}" if source_id else "",
                    now=stamp,
                )
            except Exception as exc:  # 信赖是"顺手记一笔"，不该拖垮整理
                self._trust_skip(
                    result.skipped if result is not None else None,
                    f"信赖没写进去（QQ {uid}）：{exc}",
                )
                continue
            if outcome.get("applied"):
                applied += 1
        if result is not None:
            result.trust += applied
        return applied

    _OPERATION_SECTIONS = ("memories", "facts", "relations", "affinity", "notes")

    def _claim_operations(
        self,
        session_id: str,
        payload: dict[str, Any],
        outcome: ConsolidateResult,
        *,
        now: float,
    ) -> dict[str, Any]:
        """同一件事只结算一次（阶段F3 / X28）：抢不到 operation key 的条目直接丢掉。

        增量提案里的"好感 +1"这种**增量**没有天然的主键，重放一次就会多算一次。
        所以每条新条目都带一个 operation key（作业 id + 类型 + 序号），提交前先往
        ``applied_operations`` 里插一次：插进去的才算数。**这一步必须在调用方的事务里**
        （``apply_proposal`` 就是被这么用的）——否则事务回滚会留下"已经结算过"的假账，
        重试时那段经历就永远补不上了。
        """

        rows: dict[str, list[dict[str, Any]]] = {}
        keys: list[str] = []
        for section in self._OPERATION_SECTIONS:
            items = [item for item in self._rows(payload.get(section)) if isinstance(item, dict)]
            if not items:
                continue
            rows[section] = items
            for item in items:
                key = str(item.get("operation_key") or "")
                if key:
                    keys.append(key)
        if not keys:
            return payload
        claimed = self.memory.db.record_operations(
            keys, kind="consolidate", session_id=str(session_id), now=now
        )
        if len(claimed) == len(set(keys)):
            return payload
        cleaned = dict(payload)
        for section, items in rows.items():
            kept: list[Any] = []
            for item in items:
                key = str(item.get("operation_key") or "")
                if key and key not in claimed:
                    outcome.skipped.append(f"这一条已经结算过了：{section}")
                    continue
                kept.append(item)
            cleaned[section] = kept
        return cleaned

    def memory_ids(
        self, session_id: str, *, limit: int | None = None, persona_id: str = ""
    ) -> list[int]:
        """当前范围内能合并 / 折叠的记忆 id（F3 校验提案时的白名单）。

        ``persona_id`` 同 :meth:`memory_lines`：默认空串 = 白名单只收「无主」的记忆，
        免得模型拿着别人格的记忆 id 来改这里的账。

        ``limit`` 与 :meth:`memory_lines` 用同一个窗（``consolidate_memory_window``）：
        提示词里看得到的 id 才在白名单里，两边不能对不上。
        """

        limit = self._memory_window() if limit is None else max(1, int(limit))
        rows = self.memory.db.query_memories(
            session_ids=self._sessions_of(session_id),
            persona_id=str(persona_id or ""),
            limit=max(1, int(limit)),
        )
        ids: list[int] = []
        for item in rows:
            try:
                number = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in ids:
                ids.append(number)
        return ids

    # ---------------- 落库：整觉 ----------------

    def _apply_full(
        self,
        session_id: str,
        payload: dict[str, Any],
        result: ConsolidateResult,
        *,
        now: float,
        known: set[str] | None = None,
        persona_id: str = "",
    ) -> None:
        known = set(known or ())
        written: set[int] = set()
        for item in self._rows(payload.get("memories")):
            new_id = self._write_memory(
                session_id, item, result, now=now, persona_id=persona_id
            )
            if new_id:
                written.add(int(new_id))
                result.memories += 1
        # 同主题覆盖（路 A）：模型在整理时顺手指出"这条新说法让哪条旧说法作废了"。
        # 零新增模型调用——它本来就在看这些记忆。旧条目只改状态、不删行：
        # status='superseded' + superseded_by + superseded_at，事件记 memory_superseded。
        # 拿不准就别填（提示词里写死了）；这里对坏数据一律"忽略 + WARN"，绝不崩：
        # 不存在的 id、自指、成环、已经作废过的，都跳过。
        allowed = set(self.memory_ids(session_id, persona_id=persona_id)) | written
        links = self.memory.db.supersede_links()
        for item in self._rows(payload.get("supersede")):
            old = self._int(item.get("old"))
            new = self._int(item.get("by"))
            why = str(item.get("why") or "").strip()
            if not old or not new or old == new:
                logger.warning("整理给的作废指向自己 / 坏 id，忽略：%s", item)
                result.skipped.append(f"作废指向自己：{item}")
                continue
            if old not in allowed or new not in allowed:
                logger.warning("整理给的作废引用了不存在的记忆，忽略：%s", item)
                result.skipped.append(f"作废引用了不存在的记忆：{old} ← {new}")
                continue
            if self._supersede_cycle(old, new, links):
                logger.warning("整理给的作废会成环，忽略：%s", item)
                result.skipped.append(f"作废会成环：{old} ← {new}")
                continue
            try:
                hit = self.memory.supersede(old, by=new, why=why, when=now, via="consolidate")
            except Exception as exc:  # noqa: BLE001 - 一条作废失败不该拖垮整次整理
                logger.warning("作废旧记忆 %s 失败：%s", old, exc)
                result.skipped.append(f"作废 {old} 失败：{exc}")
                continue
            if hit:
                links[str(old)] = str(new)
                result.superseded += 1
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
        result.dream = self._apply_dream(
            session_id, payload.get("dream"), now=now, persona_id=persona_id
        )

    # ---------------- 落库：小睡 ----------------

    def _apply_nap(
        self,
        session_id: str,
        payload: dict[str, Any],
        result: ConsolidateResult,
        *,
        now: float,
        known: set[str] | None = None,
        persona_id: str = "",
    ) -> None:
        for item in self._rows(payload.get("notes")):
            if self._write_memory(session_id, item, result, now=now, persona_id=persona_id):
                result.memories += 1
        # 小睡的提示词里根本没有 supersede 这个字段：模型要是自己多给了一条，
        # 那就是它在猜，宁可不动（作废是"旧说法退位"，猜错会让她忘掉对的事）。
        if self._rows(payload.get("supersede")):
            logger.warning(
                "小睡整理给了 %d 条旧说法作废，小睡不处理（要作废请等整觉）：%s",
                len(self._rows(payload.get("supersede"))),
                payload.get("supersede"),
            )
        result.digests = self._apply_digests(
            session_id, payload.get("digests"), known=known
        )
        result.dream = self._apply_dream(
            session_id, payload.get("dream"), now=now, persona_id=persona_id
        )

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

    def _memory_window(self) -> int:
        """整理时看多少条旧记忆（``memory.consolidate_memory_window``，默认 240）。"""

        config = getattr(self.world, "memory", None) if self.world is not None else None
        try:
            value = int(getattr(config, "consolidate_memory_window", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else 240

    def _node_options(self, current_node: str = "") -> list[tuple[str, str]]:
        """整理模型写记忆时可选的地点：``(node_id, 名字)``，当前地点排第一。

        素材是会话维度的事件，没有"这句话发生在哪个房间"的字段，所以这里给的是
        当前 world 的地点表（上限 40 条）——提示词里写明"只在材料真的提到这个地点时
        才填，不确定就留空"，空串 = 全局记忆。
        """

        nodes = list(getattr(self.world, "nodes", None) or []) if self.world is not None else []
        ordered: list[tuple[str, str]] = []
        seen: set[str] = set()

        def push(value: str) -> None:
            key = str(value or "").strip()
            if not key or key in seen:
                return
            seen.add(key)
            ordered.append((key, self._node_name(key)))

        push(str(current_node or ""))
        for node in nodes:
            push(str(getattr(node, "id", "") or ""))
        return ordered[:40]

    def _node_name(self, node_id: str) -> str:
        """地点 id → 显示名（找不到就原样给 id，模型照样看得懂）。"""

        key = str(node_id or "").strip()
        if not key or self.world is None:
            return key
        for node in list(getattr(self.world, "nodes", None) or []):
            if str(getattr(node, "id", "") or "") == key:
                return str(getattr(node, "name", "") or key)
        return key

    def _valid_node_id(self, value: Any) -> str:
        """整理模型给的 node_id 必须是当前 world 里真实存在的地点，否则落空串。

        「空串」不是错误，而是**全局记忆**：P0 之后空节点对所有节点可见。模型编一个
        不存在的地点（或老存档里已经删掉的地点）才是错误——那种条目会谁也召不回。
        """

        key = str(value or "").strip()
        if not key or self.world is None:
            return ""
        for node in list(getattr(self.world, "nodes", None) or []):
            if str(getattr(node, "id", "") or "") == key:
                return key
        return ""

    def _write_memory(
        self,
        session_id: str,
        item: dict[str, Any],
        result: ConsolidateResult,
        *,
        now: float,
        persona_id: str = "",
    ) -> int:
        """写一条整理出来的记忆；返回新 id（没写成返回 0）。"""

        text = " ".join(str(item.get("text") or "").split())[:200]
        if not text:
            return 0
        # 整理这一步是模型自己写的，提示词拦不住它写"明天/下周"。落库前统一换成
        # 绝对日期（原话留在句末），用的是本次整理的 ``now``（世界时钟）。
        text = normalize_for_memory(text, now=now)
        people = [
            str(value)
            for value in (item.get("participants") or [])
            if str(value).strip()
        ]
        days = float(item.get("review_in_days") or 0.0)
        next_at = now + days * 86400 if days > 0 else 0.0
        memory_id = self.memory.remember(
            session_id=session_id,
            # 整理结果落**当次那个人格**的桶（默认空串 = 无主，见 apply_proposal）
            persona_id=str(persona_id or ""),
            # 编出来的 / 已删掉的地点落空串（全局记忆），别写成谁也召不回的孤儿条目
            node_id=self._valid_node_id(item.get("node_id")),
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
        return int(memory_id or 0)

    @staticmethod
    def _supersede_cycle(old: int, new: int, links: dict[str, str]) -> bool:
        """``old → new`` 会不会成环（沿着现成的 superseded_by 链往上走一圈看看）。

        ``links`` 是库里已有的 ``{旧 id: 覆盖它的新 id}``；已应用的条目会被加进去，
        所以同一批里的 A→B→C→A 也能拦住。绕圈上限 64 步，防坏数据里已经有环时死循环。
        """

        current = str(new)
        seen: set[str] = set()
        for _ in range(64):
            if current == str(old):
                return True
            if current in seen:  # 现成的链自己就有环：再挂上去只会更乱
                return True
            seen.add(current)
            nxt = links.get(current)
            if not nxt:
                return False
            current = str(nxt)
        return True

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

    def _apply_dream(
        self, session_id: str, value: Any, *, now: float, persona_id: str = ""
    ) -> str:
        text = " ".join(str(value or "").split())[:200]
        if not text:
            return ""
        self.memory.remember(
            session_id=session_id,
            # 梦也属于做它的那个人格（默认空串 = 无主，老调用方不变）
            persona_id=str(persona_id or ""),
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
